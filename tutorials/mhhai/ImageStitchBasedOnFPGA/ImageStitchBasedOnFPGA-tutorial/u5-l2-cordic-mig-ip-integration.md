# CORDIC与MIG/时钟IP的集成

> 前置讲义：[u2-l4 圆柱面投影的硬件实现：定点Verilog](u2-l4-cylindrical-projection-hardware.md)、[u3-l1 DDR3突发传输控制器 mem_burst](u3-l1-ddr3-mem-burst.md)、[u4-l1 动态规划法寻找最佳缝合线](u4-l1-dynamic-seam.md)、[u5-l1 定点数运算与位宽设计深入](u5-l1-fixed-point-arithmetic.md)。
>
> 前几讲都把 `cordic_0`、`clk_wiz_0`、`mig_7series_0` 当作「黑盒」：u2-l4 只说 CORDIC 把相位变成 sin/cos；u5-l1 推完了定点位宽，却把「sin/cos 的精确 Q 格式」**特意留给本讲**；u3-l1 讲清了 `mem_burst` 如何封装 MIG 的 `app_*`，却没讲 MIG 本体；u4-l1 列出了 DynamicSeam 的综合致命错误，也没展开它例化的那颗 MIG 到底连了哪些脚。本讲就是来「开盒」的——专门讲这三个 Xilinx IP 核**在 Verilog 里怎么例化、端口怎么连、时钟从哪来**。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `cordic_0`、`clk_wiz_0`、`mig_7series_0` 三个 IP 各自的**职责**与**典型端口形态**，并能在源码里逐脚认出它们。
- 读懂 CORDIC 的 **AXI4-Stream 接口**（`s_axis_phase_*` 输入相位、`m_axis_dout_*` 输出 sin/cos），解释 `phase_tdata[15:0]` 与 `dout_tdata[47:0]` 的打包关系。
- 读懂 Clocking Wizard 的**差分时钟输入**（`clk_in1_p/n`）与 `clk_out1→sys_clk` 输出，以及 `reset`/`locked` 控制位的含义。
- 读懂 MIG 的**三组信号**（物理 DDR3、应用层 `app_*`、系统/时钟），并把 `mem_burst`、`DynamicSeam` 里那些零散的 `app_*`、`sys_clk_i`、`ui_clk`、`init_calib_complete` 归位到 MIG 的端口图上。
- 画出 `sys_clk`、`ui_clk`、`mem_clk` 三者在系统中的**来源与频率关系**，并指出 DynamicSeam 在时钟域接线上的真实错误。

## 2. 前置知识

### 2.1 什么是 Xilinx IP 核

FPGA 厂商把一些「常用但难写」的硬件模块提前做好、参数化，打包成可复用的黑盒，称为 **IP 核（Intellectual Property core）**。在 Vivado 里用图形界面配置参数（位宽、频率、功能选项），工具会生成一份 `.xci` 配置和一份可综合的网表/源码，你在自己的 Verilog 里像调用函数一样**例化（instantiate）**它即可。

本项目用到三个 Xilinx IP：

| IP | 全称 | 作用 |
|----|------|------|
| `cordic_0` | CORDIC（COordinate Rotation DIgital Computer） | 只用移位与加法迭代计算三角函数/反三角函数，这里用来算 \(\sin/\cos\) |
| `clk_wiz_0` | Clocking Wizard | 用 MMCM/PLL 把外部晶振时钟变换成系统需要的各种频率时钟 |
| `mig_7series_0` | Memory Interface Generator（7 系列） | 7 系列 FPGA 的 DDR3 控制器，把底层 DDR3 物理时序封装成易用的应用接口 |

> 重要前提：这三个 IP 的**配置文件（`.xci`/`.xco`）和约束文件（`.xdc`）都不在仓库里**（见 u1-l2 的「源码片段集」结论）。所以凡是涉及**具体位宽、具体频率、具体功能选项**的数值，本讲都只能根据例化处的端口连线和注释**反推**，无法 100% 确认，相关结论会标注「待确认」。我们能确认的是**端口连接关系与时钟拓扑**——这恰好是「IP 集成」这一讲真正要教的内容。

### 2.2 AXI4-Stream：IP 之间传数据的标准握手

Xilinx 很多 IP（包括 CORDIC）用 **AXI4-Stream** 协议在模块间传数据。它最精简的形式只有两组信号：

- **TVALID**：发送方拉高，表示「我手上这份数据有效」。
- **TDATA**：承载数据的位宽可任意配置。
- （可选）**TLAST/TREADY**：本项目的 CORDIC 例化没用到，故从略。

握手规则：当 TVALID 为高时，接收方在本拍就吃下 TDATA。本项目把 CORDIC 的输入 TVALID 恒接 `1'b1`，等于「永远有相位喂进来」，是最简单的「单向喂数据」接法。

### 2.3 差分时钟与 MMCM

高端 FPGA 板卡的参考时钟常以**差分对**（一对极性相反的信号 `p`/`n`，如 LVDS 标准）提供，抗干扰能力比单端强。FPGA 内部的 **MMCM（Mixed-Mode Clock Manager）**或 PLL 可以对这份参考时钟做**分频、倍频、相移**，生成多路不同频率的系统时钟。Clocking Wizard 就是 MMCM 的图形化封装。

## 3. 本讲源码地图

本讲横跨三个文件，三个 IP 各自散落其中：

| 文件 | 例化了哪个 IP | 本讲关注点 |
|------|--------------|-----------|
| [圆柱面投影.v](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v) | `cordic_0` | 相位 `phase_tdata` 怎么来、sin/cos 怎么从 `dout_tdata` 拆出来 |
| [动态规划法寻找最佳缝合线/DynamicSeam.v](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v) | `clk_wiz_0` + `mig_7series_0` | 差分时钟进来、`sys_clk` 出去；MIG 的完整端口表与时钟接线 |
| [DDR3控制/mem_burst.v](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_burst.v) | （不例化 MIG，只暴露 `app_*`） | 作为 MIG 应用接口的「用户侧」，是 MIG 端口的镜像 |

一句话总览：`project` 模块用 `cordic_0` 算三角函数；`DynamicSeam` 用 `clk_wiz_0` 生成系统时钟、用 `mig_7series_0` 驱动 DDR3；`mem_burst` 不自己例化 MIG，而是把 MIG 的 `app_*` 引到顶层去连接。三者的**时钟拓扑**是本讲的主线。

## 4. 核心概念与源码讲解

### 4.1 cordic_0：用相位换 sin/cos 的三角函数 IP

#### 4.1.1 概念说明

圆柱面投影的反向映射 `mapBackward` 需要把圆柱面上的角度还原成光线方向，核心运算是 \(\sin/\cos\)（见 u2-l2）。但 FPGA 的 DSP 乘法器不会算三角函数——CORDIC 算法就是来补这个缺的。

**CORDIC（COordinate Rotation DIgital Computer）** 的巧妙之处：它通过**一连串只含移位和加法**的「旋转」迭代来逼近 \(\sin/\cos\)，**完全不用乘法表、不用泰勒展开**。给它一个角度（相位），它同时输出 \(\sin\) 和 \(\cos\)。Xilinx 把它做成 IP：输入一个相位值，输出一对 sin/cos，正是本项目需要的。

CORDIC 之所以在本项目里是「黑盒里的黑盒」，是因为它的输入/输出位宽、小数位格式都由 IP 配置决定，而配置文件没收录。所以本节我们把能确定的**端口接线**讲透，把不能确定的**数值格式**标清楚。

#### 4.1.2 核心流程

`project` 模块每个时钟处理一个目标像素，用到 CORDIC 的那一段数据通路是：

```
dst_tl_x  ─×coe─► u (Q13.22, 35b) ──[24:9]──► phase_tdata (16b) ──► ┌─────────┐
                                                                     │ cordic_0 │ ──► dout_tdata[47:24] = sin → x_
                                                                     └─────────┘ ──► dout_tdata[23:0]  = cos → z_
```

也就是三步：

1. 把目标像素坐标 `dst_tl_x` 乘焦距倒数 `coe`，得到角度 `u`（弧度，小角度近似 \(\approx x/f\)）。
2. 截取 `u` 的高 16 位喂给 CORDIC 当**相位输入**。
3. 从 CORDIC 的 48 位输出里，高 24 位当 \(\sin\)、低 24 位当 \(\cos\)。

#### 4.1.3 源码精读

**(a) 相位怎么来：坐标 × coe 后切片**

```verilog
reg [15:0] phase_tdata;
...
u  = $signed(dst_tl_x) * $signed(coe);   // 11+24 = 35
phase_tdata[15:0] = u[24:9];
```

[圆柱面投影.v:29](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L29) 声明 16 位相位；[圆柱面投影.v:78](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L78) 算出 `u`（Q13.22，35 位，推导见 u5-l1）；[圆柱面投影.v:80](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L80) 取 `u[24:9]` 共 16 位作相位。这 16 位保留 2 位整数（bit 24、23）与 14 位小数（bit 22~9），即 **Q2.14** 的弧度值。

角度范围核对：`dst_tl_x` ∈ [−543, 542]（[圆柱面投影.v:56-57](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L56-L57)），\(f=2707.47\)，故

\[
u=\frac{x}{f}\in[-0.2005,\,0.2002]\ \text{rad}\approx \pm 11.5^\circ
\]

量值远小于 \(\pi\)，相位高位全是符号扩展，切片安全。CORDIC 拿到这个小角度，内部把它当作弧度处理（具体定标取决于 IP 配置，**待确认**）。

**(b) sin/cos 怎么拆：48 位输出的打包格式**

```verilog
x_= dout_tdata[47:24];   // sin
y_= v;
z_= dout_tdata[23:0];    // cos
```

[圆柱面投影.v:81-83](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L81-L83) 把 CORDIC 输出拆成两半：高 24 位 `dout_tdata[47:24]` 当 \(\sin u\)（赋给 `x_`），低 24 位 `dout_tdata[23:0]` 当 \(\cos u\)（赋给 `z_`）。中间的 `y_= v` 是第三路光线分量（圆柱面「高度」方向），不走 CORDIC。

模块端口里 `dout_tdata` 声明成 48 位（[圆柱面投影.v:23](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L23) 的 `output [47:0] dout_tdata`），所以这个 IP 被配置成 **「sin + cos 各 24 位、拼接成 48 位」** 输出。这里要指出一处**源码注释与实际不符**：例化处的模板注释写 `// output wire [31 : 0] m_axis_dout_tdata`（[圆柱面投影.v:117](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L117)），是 Vivado 默认模板的残留（32 位 = 两个 16 位），与实际配置的 48 位（两个 24 位）不一致——以端口声明和拆位逻辑为准。

> sin/cos 各自的 24 位里「几位整数、几位小数」（Q 格式）取决于 CORDIC IP 配置，**仓库未收录 `.xci`，待确认**。u5-l1 推导累加器时假设过「22 位小数」或「14 位小数」两种可能，并指出两种假设会导致下游小数点对齐方式不同——这正是此处「待确认」会向下传递影响的节点。

**(c) CORDIC 的例化**

```verilog
cordic_0 uut(
    .s_axis_phase_tvalid(1'b1),   // 永远有效
    .s_axis_phase_tdata(phase_tdata),   // 输入相位 16 位
    .m_axis_dout_tvalid(dout_tvalid),   // 输出有效
    .m_axis_dout_tdata(dout_tdata)      // 输出 sin/cos 48 位
);
```

[圆柱面投影.v:113-118](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L113-L118)。这是一段标准的 AXI4-Stream 例化：

- `s_axis_phase_*` 是 **slave 侧相位输入**（s = slave，CORDIC 接收相位）。
- `m_axis_dout_*` 是 **master 侧数据输出**（m = master，CORDIC 送出结果）。
- 输入 `tvalid` 恒接 `1'b1`，即「永远有相位喂进来」，省掉了反压握手。

两个值得注意的工程细节：

1. **没接 `aclk`**：CORDIC IP 通常需要 `aclk`（工作时钟）。这里例化没显式连时钟脚——要么 IP 配置里把时钟脚隐式接到了模块的 `clk`，要么是一处遗漏。由于 IP 配置不在仓库，**待确认**；但从 [圆柱面投影.v:23](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L23) 的 `input clk` 看，意图显然是用模块主时钟驱动 CORDIC。
2. **多周期延迟**：CORDIC 是**多周期** IP（一次 sin/cos 要若干拍流水线），但本模块把 `phase_tdata → dout_tdata → 矩阵乘` 全塞进同一个 `posedge clk` 的阻塞赋值块（[圆柱面投影.v:72-111](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L72-L111)）。这在功能仿真里靠 `dout_tdata` 的旧值「碰巧」跑通，在真实时序里几乎不可能收敛——u5-l1 已把它定性为「教学原型，不可照抄」。

#### 4.1.4 代码实践

**实践目标**：亲手验证「坐标 → 相位 → sin/cos」三段连线的数值，确认接线无误。

**操作步骤**：

1. 读 [圆柱面投影.v:56](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L56)，记下扫描起点 `dst_tl_x = -543`。
2. 算第一个像素的相位：\(u = -543 / 2707.47 = -0.2006\) rad；再按 Q2.14 量化，\(-0.2006\times 2^{14}\approx -3290\)，写成 16 位二进制（应落在 `phase_tdata` 范围内）。
3. 读 [圆柱面投影.v:81-83](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L81-L83)，确认 CORDIC 输出的高 24 位流向 `x_`（sin）、低 24 位流向 `z_`（cos）。
4. 对照 IP 模板注释 [圆柱面投影.v:117](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L117)（`[31:0]`）与端口声明 [圆柱面投影.v:23](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L23)（`[47:0]`），指出哪一个是真实配置。

**需要观察的现象**：相位值是小角度（\(\pm 11.5^\circ\)）；sin 高 24 位、cos 低 24 位的拆位与端口位宽一致；模板注释是过时的。

**预期结果**：相位 Q2.14 量化值约 \(\pm 3290\)；输出按 48 位（24+24）拆分；模板注释 `[31:0]` 与实际 `[47:0]` 不符，以实际为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么输入 `s_axis_phase_tvalid` 恒接 `1'b1` 是「最省事」的接法？它在什么前提下才合法？

**参考答案**：恒接 1 表示「每个时钟都喂一个新相位」，省掉了维护 valid 的逻辑。它的前提是上游**每个时钟真的能产生一个有效相位**，且下游不在乎反压。本模块每个时钟确实算一个新 `phase_tdata`（阻塞赋值），所以可以这样接。但真实工程里 CORDIC 是多周期流水线，每拍喂一个相位、隔几拍才出结果，恒 1 会让「相位→结果」的对齐变得 tricky，通常还是要配 valid 计数。

**练习 2**：如果把 `phase_tdata` 改成取 `u[34:19]`（最高 16 位），CORDIC 算出的 sin/cos 还对吗？

**参考答案**：不对。`u[34:19]` 等于把相位再放大 \(2^{10}\) 后取整，送进去的角度完全错位，sin/cos 输出会对应到错误的角度上。这再次印证 u5-l1 的结论：**喂给 IP 的位切片区间必须与 IP 约定的 Q 格式严格匹配**，差一位都崩。

---

### 4.2 clk_wiz_0：差分时钟进来，系统时钟出去

#### 4.2.1 概念说明

FPGA 芯片自己不会「产生」时钟——时钟永远来自板子上的一颗**晶振**（参考时钟）。晶振的频率是固定的（如 50/100/200 MHz），而芯片里各模块需要的频率五花八门。**Clocking Wizard（时钟向导）** 就是夹在「晶振」和「模块」之间的变频器：它内部用 MMCM/PLL，把输入的一份参考时钟，**倍频/分频/相移**成若干路系统需要的频率输出。

本项目 `DynamicSeam` 用 `clk_wiz_0` 干两件事：把板载的**差分**晶振时钟接进来，再输出一路 `sys_clk` 给 MIG 和状态机用。

#### 4.2.2 核心流程

Clocking Wizard 的典型接线只有三组：

1. **时钟输入**：单端 `clk_in1` 或差分 `clk_in1_p/clk_in1_n`，接板载晶振。
2. **时钟输出**：`clk_out1`、`clk_out2` … 若干路派生时钟。
3. **控制/状态**：`reset`（复位 MMCM）、`locked`（MMCM 锁相完成、输出稳定的指示）。

启动流程：上电 → MMCM 复位释放 → MMCM 内部锁相环稳定 → `locked` 拉高 → `clk_out*` 可用。下游模块（MIG）应在 `locked` 有效后才动作。

#### 4.2.3 源码精读

**(a) 例化与端口连接**

```verilog
wire sys_clk;
//200MHz的差分时钟
clk_wiz_0 instance_name
 (
  // Clock out ports
  .clk_out1(sys_clk),     // output clk_out1
  // Status and control signals
  .reset(rst_n), // input reset
  .locked(),       // output locked
  // Clock in ports
  .clk_in1_p(cl_p),    // input clk_in1_p     ← 注意这行
  .clk_in1_n(clk_n));    // input clk_in1_n
```

[DynamicSeam.v:36-47](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L36-L47)。逐脚解读：

- **输入时钟**：`.clk_in1_p` / `.clk_in1_n` 是一对差分时钟脚，注释 [DynamicSeam.v:37](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L37) 写「200MHz 的差分时钟」。这是板载 200MHz 差分晶振（具体频率**待确认**，以注释为准）。
- **输出时钟**：`.clk_out1(sys_clk)` 把派生时钟命名为 `sys_clk`（[DynamicSeam.v:36](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L36) 声明的 wire）。这份 `sys_clk` 同时喂给 MIG 的 `sys_clk_i` 和本模块的所有状态机。
- **复位**：`.reset(rst_n)`——注意这里把**低有效**的 `rst_n` 直接接给 IP 的 `reset`。Clocking Wizard 的 `reset` 一般是**高有效**，这里极性是否匹配取决于 IP 配置（**待确认**）；若 IP 配置成高有效，那 `rst_n` 低电平时反而会让 MMCM 一直复位、`locked` 永远不起来。这是一处潜在接线隐患。
- **锁相指示**：`.locked()` 悬空，没有接任何信号——意味着下游（MIG、状态机）**拿不到「时钟已稳定」的信号**，上电时序完全靠 MIG 自己的 `init_calib_complete` 兜底。这是工程上的疏漏。

**(b) 一处真实的拼写 bug：`cl_p` 还是 `clk_p`？**

看 [DynamicSeam.v:46](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L46)：

```verilog
.clk_in1_p(cl_p),    // input clk_in1_p
```

连进去的信号名叫 `cl_p`，但模块端口列表里声明的是 `clk_p`（[DynamicSeam.v:26](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L26)）：

```verilog
(input rst_n, input clk_p, input clk_n, input read_request);
```

`cl_p` 在模块里**根本没有声明**。综合器会把它当成隐式 1 位 wire（旧版 Verilog 允许隐式声明，但这是 bug），导致差分时钟的正端实际悬空。正确的写法应是 `.clk_in1_p(clk_p)`。这和 u4-l1 指出的「外部代码不能综合」是同一类问题：作者写了草稿、没有过编译。差分时钟 `p` 端接错，`sys_clk` 根本不会正确产生。

> 小结：`clk_wiz_0` 的拓扑是「板载 200MHz 差分晶振 → MMCM → `sys_clk`」，但本例化有 `cl_p` 拼写错误、`reset` 极性存疑、`locked` 悬空三处问题，是教学草稿，不可直接用。

#### 4.2.4 代码实践

**实践目标**：在源码里追踪 `sys_clk` 的「来龙去脉」，并定位接线错误。

**操作步骤**：

1. 在 [DynamicSeam.v](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v) 里搜索所有 `sys_clk`，确认它被 `clk_wiz_0` 驱动（[L41](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L41)），又被 MIG 的 `sys_clk_i` 消费（[L230](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L230)）、还被所有状态机的 `posedge sys_clk` 消费（[L91](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L91)、[L121](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L121)）。
2. 比对 [L46](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L46) 的 `cl_p` 与 [L26](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L26) 的 `clk_p`，确认拼写不一致。
3. 检查 `.locked()` 是否接了信号，思考：如果 MMCM 还没锁相，`sys_clk` 会是什么状态？

**需要观察的现象**：`sys_clk` 一条线串起 clk_wiz、MIG、状态机三处；`cl_p`/`clk_p` 拼写不一致；`locked` 悬空。

**预期结果**：`sys_clk` 是全模块唯一的全局时钟，来源是差分晶振经 MMCM 变换；存在 `cl_p` 拼写 bug 与 `locked` 未接的疏漏。

#### 4.2.5 小练习与答案

**练习 1**：为什么晶振要做成差分（一对 p/n），而不是单根线？

**参考答案**：差分对两根线传输大小相等、极性相反的信号，接收端取两者之差。外界干扰（串扰、电源噪声）会同时耦合到两根线上，做差后被抵消，因此差分时钟的抖动和抗干扰能力远好于单端，适合 200MHz 这种较高频率的参考时钟。

**练习 2**：`.locked()` 悬空意味着什么？应该怎么改？

**参考答案**：`locked` 是 MMCM「锁相完成、输出时钟稳定」的指示。悬空意味着下游无法知道 `sys_clk` 是否已稳定——MIG 和状态机会在时钟还没稳好的瞬间就开始动作，行为不可预测。正确做法是把 `locked` 接到一个 wire，再用它（配合 `rst_n`）做整个模块的统一复位释放，确保「时钟稳定后才解复位」。

---

### 4.3 mig_7series_0：DDR3 控制器与应用接口

#### 4.3.1 概念说明

DDR3 SDRAM 芯片本身的物理时序极其苛刻（刷新、ZQ 校准、列行选通、DQS 训练……），让用户逻辑直接去「脚对脚」驱动 DDR3 几乎不可能。**MIG（Memory Interface Generator）** 就是 Xilinx 提供的 DDR3 控制器 IP：它在内部把所有底层物理时序处理好，对用户暴露一组**简单得多的「应用接口」**（`app_*` 信号），用户只要按这组接口写地址、读写数据即可，不必关心 DDR3 刷新与训练。

u3-l1 已经从「用户侧」讲了 `mem_burst` 如何封装这组 `app_*`；本节从「IP 侧」补上 MIG 本体——它有哪些脚、`app_*` 在哪个时钟域、`sys_clk_i`/`ui_clk` 怎么传递。

#### 4.3.2 核心流程

MIG 的端口可分三组：

1. **物理 DDR3 组（`ddr3_*`）**：直接连到 FPGA 引脚上的 DDR3 芯片（`ddr3_addr/ck_p/ck_n/cke/ras_n/cas_n/we_n/reset_n/dq/dqs_p/dqs_n/dm/cs_n/odt`）。这部分由 MIG 驱动，用户完全不碰。
2. **应用接口组（`app_*`）**：用户操作的入口，包括命令（`app_cmd/app_addr/app_en`）、写数据（`app_wdf_*`）、读数据（`app_rd_data*`）。**这些信号全部同步于 MIG 输出的 `ui_clk`**。
3. **系统/状态组**：参考时钟 `sys_clk_i`、复位 `sys_rst`、用户时钟 `ui_clk`、用户复位 `ui_clk_sync_rst`、校准完成 `init_calib_complete`。

启动与使用流程：

```
sys_clk_i(参考时钟) ─┐
sys_rst(复位)      ─┴─► mig_7series_0 ─► init_calib_complete=1 (校准完成)
                                     ─► ui_clk        (用户时钟，分给 app_* 域)
                                     ─► ui_clk_sync_rst(用户复位)
   之后，用户在 ui_clk 域按 app_* 协议发命令 ─► DDR3 读写
```

关键时序规则：**只有在 `init_calib_complete` 拉高后，`app_*` 接口才可用**；所有 `app_*` 操作必须在 `ui_clk` 上升沿采样。这两条是接 MIG 的铁律。

#### 4.3.3 源码精读

**(a) `mem_burst`：MIG 应用接口的「用户侧镜像」**

先看 `mem_burst`，它**不例化 MIG**，而是把 MIG 的 `app_*` 当成自己的端口转发出去：

```verilog
input rst,                                   // mig提供，当为低电平时表示ui_clk正在复位
input mem_clk,                               // 接口时钟
...
output[ADDR_BITS-1:0] app_addr,
output[2:0] app_cmd,
output app_en,
...
input ui_clk_sync_rst,
input init_calib_complete
```

[mem_burst.v:9-39](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_burst.v#L9-L39)。这段端口表就是 MIG 应用接口的镜像，逐条对应 MIG 的 `app_*`。三个与时钟/状态相关的脚尤其重要：

- `mem_clk`（[L10](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_burst.v#L10)）：`mem_burst` 自己的工作时钟。它应当接 MIG 的 `ui_clk`，因为 `app_*` 信号都在 `ui_clk` 域。
- `ui_clk_sync_rst`（[L38](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_burst.v#L38)）：MIG 给的、与 `ui_clk` 同步的复位。
- `init_calib_complete`（[L39](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_burst.v#L39)）：校准完成门控。`mem_burst` 内部所有状态跳转都被 `init_calib_complete === 1'b1` 把关（[L102](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_burst.v#L102)），正是「校准完成才允许操作」这条铁律的落地。

地址规则也能从端口注释看出：[mem_burst.v:6](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_burst.v#L6) 注释 `parameter ADDR_BITS = 24 //为29，top文件中对该参数进行了改变`——说明在顶层例化时 `ADDR_BITS` 会被改成 29（与 MIG 的 `app_addr` 位宽对齐）；而地址递增以「一个 64bit 字 = 8 字节」为单位（`app_addr_r <= {rd_burst_addr,3'd0}` 拼 3 位字节偏移，[L111](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_burst.v#L111)；每条命令 `app_addr_r + 8`，[L129](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_burst.v#L129)、[L178](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_burst.v#L178)）。

> 注意一个**位宽不一致**：`mem_burst` 的 `MEM_DATA_BITS` 默认 64（[mem_burst.v:5](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_burst.v#L5)），而 `DynamicSeam` 里那份 MIG 的 `APP_DATA_WIDTH` 算出来是 512（见下面 (c)）。这意味着两者假设的 MIG 配置**不一样**——`mem_burst` 配的是窄接口（64bit），`DynamicSeam` 配的是宽接口（512bit，nCK_PER_CLK=4）。仓库里没有 IP 配置文件，无法确认真实采用哪种，**待确认**。但能确定：这两份代码不可能直接共用同一颗 MIG。

**(b) `DynamicSeam` 里 MIG 的例化（以及它为什么是错的）**

```verilog
mig_7series_0 u_mig_7series_0 (
    // Memory interface ports
    .ddr3_addr          (ddr3_addr),
    .ddr3_ck_n          (ddr3_ck_n),
    .ddr3_ck_p          (ddr3_ck_p),
    ...
    .init_calib_complete(init_calib_complete),
    ...
    // Application interface ports
    .app_addr           (app_addr),
    .app_cmd            (app_cmd),
    ...
    .ui_clk             (ui_clk),
    .ui_clk_sync_rst    (ui_clk_sync_rst),
    ...
    // System Clock Ports
    .sys_clk_i          (sys_clk),
    .sys_rst            (sys_rst)
);
```

[DynamicSeam.v:192-232](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L192-L232) 给出了 MIG 的完整端口表，三组信号齐全：物理 DDR3（`ddr3_*`）、应用接口（`app_*`）、系统时钟（`sys_clk_i`、`sys_rst`、`ui_clk`、`ui_clk_sync_rst`）。但这段例化有**多处致命问题**，u4-l1 已定性为「不可综合」，本节从「IP 集成」角度补充三条与时钟/端口直接相关的：

1. **例化被写进了 `always` 块里**：这段 `mig_7series_0 u_mig_7series_0(...)` 出现在 [DynamicSeam.v:184-265](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L184-L265) 的 `always@(posedge sys_clk)` 内部（在 `READ` 状态的 `case` 分支里）。模块例化是**结构化语句**，只能写在模块体里、不能写在过程块（`always`/`initial`）里——这是语法级的错误，综合器会直接报错。正确位置是模块顶层、与 `clk_wiz_0` 并列。
2. **端口表里有一行「损坏」**：[DynamicSeam.v:195](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L195) 读作

   ```verilog
   `     (ddr3_ck_n),  // output [0:0]  ddr3_ck_n
   ```

   端口名 `.ddr3_ck_n` 丢失，只剩一个反引号 `` ` `` 和括号——显然是编辑/粘贴时把端口名抹掉了。对比相邻的 `.ddr3_ck_p`（[L196](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L196)），这行应当是 `.ddr3_ck_n(ddr3_ck_n),`。差分时钟的负端端口名缺失，又是一处语法错误。

3. **关键时钟/复位信号未声明**：例化里用了 `ui_clk`（[L226](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L226)）、`ui_clk_sync_rst`（[L227](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L227)）、`sys_rst`（[L231](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L231)），以及一堆 `ddr3_*`，但模块里**从未 `wire` 声明过它们**。MIG 输出的 `ui_clk` 没有 wire 承接，等于「MIG 给了用户时钟，却没人收」。

**(c) 一个注释/代码不一致：状态机到底跑在 `sys_clk` 还是 `ui_clk`？**

最隐蔽、但也最切中「IP 集成」主题的问题，是**时钟域**。看状态机的计数器块：

```verilog
always@(posedge sys_clk)  //定时器用的是ddr3提供的工用户使用的时钟，200MHz
    begin
        case(cstate) ...
```

[DynamicSeam.v:121](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L121)。**代码用的是 `posedge sys_clk`，但注释写的是「ddr3 提供的用户使用的时钟」**——也就是 `ui_clk`。作者的本意（按注释）是让状态机跑在 MIG 给的 `ui_clk` 上，这与 MIG 应用接口的铁律（`app_*` 必须在 `ui_clk` 域）一致；但实际代码写成了 `sys_clk`。于是出现一个**跨时钟域**的接法：状态机在 `sys_clk`（200MHz，来自 clk_wiz）里驱动 `app_addr`、采样 `app_rd_data`，而这两个信号其实属于 `ui_clk` 域。两个时钟频率不同、相位无关，直接跨域读写会**采样错位、数据错乱**。这正是「注释提醒了对、代码却写错」的典型集成 bug。

**(d) 宽度参数反推 MIG 配置**

```verilog
localparam nCK_PER_CLK           = 4;
localparam DQ_WIDTH              = 64;
localparam PAYLOAD_WIDTH         = 64;
localparam APP_DATA_WIDTH        = 2 * nCK_PER_CLK * PAYLOAD_WIDTH;  // = 512
```

[DynamicSeam.v:62-68](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L62-L68)。从这些 localparam 反推：这颗 MIG 配成 **DQ 宽 64bit、每时钟 4 个 DDR 时钟周期（nCK_PER_CLK=4）**，于是应用接口 `app_wdf_data`/`app_rd_data` 是 \(2\times4\times64=512\) 位宽（对应例化里 `app_wdf_data [511:0]`）。而 `mem_burst` 配的是 64 位（见 (a)），两者不一致——再次说明仓库里这两份代码来自不同配置的草稿，**待确认**真实采用哪份。

> 小结：MIG 的正确接法是「`sys_clk_i` 吃 clk_wiz 的 `sys_clk` → 内部校准 → `init_calib_complete` 有效后在 `ui_clk` 域操作 `app_*`」。`mem_burst`（u3-l1）示范了正确的用户侧封装；`DynamicSeam` 的 MIG 例化则是反面教材：例化位置错、端口损坏、时钟域接错。

#### 4.3.4 代码实践

**实践目标**：把 MIG 的三个时钟信号（`sys_clk_i`/`ui_clk`/`mem_clk`）与一个门控（`init_calib_complete`）在源码里追全。

**操作步骤**：

1. 在 [DynamicSeam.v](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v) 里找到 MIG 的 `.sys_clk_i(sys_clk)`（[L230](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L230)），确认它来自 clk_wiz 的 `clk_out1`。
2. 找到 `.ui_clk(ui_clk)`（[L226](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L226)），确认它是 MIG 的**输出**，按铁律应当驱动所有 `app_*` 操作。
3. 在 [mem_burst.v:10](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_burst.v#L10) 找到 `mem_clk`，理解它对应 MIG 的 `ui_clk`。
4. 在 [mem_burst.v:102](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_burst.v#L102) 找到 `init_calib_complete` 门控，确认「校准完成才允许状态跳转」。
5. 对比 [DynamicSeam.v:121](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L121) 的代码（`posedge sys_clk`）与注释（「用户时钟」），找出不一致。

**需要观察的现象**：`sys_clk_i` 是 MIG 输入、`ui_clk` 是 MIG 输出、`mem_clk`=`ui_clk`；`DynamicSeam` 的状态机错误地跑在 `sys_clk` 而非 `ui_clk`。

**预期结果**：能画出「clk_wiz → sys_clk → MIG(sys_clk_i) → ui_clk → mem_burst(mem_clk)」这条时钟链，并指出 `DynamicSeam` 跨域驱动的错误。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `app_*` 信号必须用 `ui_clk`，而不能用 `sys_clk`？

**参考答案**：`app_*` 是 MIG **内部**在用户时钟域（`ui_clk`）里采样的接口。`ui_clk` 是 MIG 把高速 DDR3 时钟分频后给用户的「慢速、友好」时钟。`sys_clk_i` 只是 MIG 内部 PLL 的**参考输入**，它的频率与 `ui_clk` 不同、与 `app_*` 的采样节拍无关。用 `sys_clk` 驱动 `app_*` 等于在一个 MIG 不认识的时钟域里写命令，MIG 会采样到亚稳态或错位数据。

**练习 2**：`init_calib_complete` 拉高之前，如果硬发一个 `app_en=1` 的读命令，会发生什么？

**参考答案**：校准未完成时，DDR3 的训练（读 DQS 训练、ZQ 校准等）还没结束，物理层不可用，MIG 不会响应命令（`app_rdy` 通常保持低）。`mem_burst` 用 `init_calib_complete === 1'b1` 把状态机锁在复位（[L102](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_burst.v#L102)）正是为了避免这种情况。硬发命令只会被丢弃。

---

## 5. 综合实践

把本讲三个 IP 串成一条完整的**时钟与数据通路**，这是本讲的核心实践任务（对应大纲练习）。

**任务**：阅读 [圆柱面投影.v](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v)、[DynamicSeam.v](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v)、[mem_burst.v](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_burst.v)，完成下面四问：

1. **三个 IP 的输入时钟来源**：分别写出 `cordic_0`、`clk_wiz_0`、`mig_7series_0` 各自的「输入时钟」从哪里来。
2. **三条时钟线的频率关系**：画出 `sys_clk`、`ui_clk`、`mem_clk` 三者的来源与驱动关系，说明它们的频率大小关系。
3. **门控时序**：说明 `init_calib_complete` 和 `locked` 各自门控了谁、为什么需要这个门控。
4. **错误诊断**：指出 `DynamicSeam` 在 IP 集成上的至少三处错误（提示：例化位置、端口损坏、时钟域），并写出每种错误的修复思路。

**参考答案要点**：

1. **输入时钟来源**：
   - `clk_wiz_0`：输入是板载 **200MHz 差分晶振** `clk_in1_p/n`（[DynamicSeam.v:37](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L37)、[L46-47](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L46-L47)）。
   - `mig_7series_0`：参考时钟 `sys_clk_i` **来自 `clk_wiz_0` 的 `clk_out1`**（`sys_clk`，[DynamicSeam.v:230](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L230)）。
   - `cordic_0`：例化未显式接 `aclk`，意图由 `project` 模块的 `input clk` 驱动（[圆柱面投影.v:23](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L23)、[L113-118](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/圆柱面投影.v#L113-L118)），**具体待确认**。

2. **频率关系**（拓扑确定、数值待确认）：
   ```
   200MHz 差分晶振 ──► clk_wiz_0 ──► sys_clk  ──► mig_7series_0.sys_clk_i
                                          │                      │
                                          │                      ▼
                                          │                  ui_clk ──► mem_burst.mem_clk
                                          │                      │
                                          ▼                      ▼
                                   (DynamicSeam 状态机          (app_* 接口域)
                                    错误地用 sys_clk)
   ```
   - `sys_clk` = clk_wiz 输出，按注释约 200MHz（**待确认**，取决于 clk_wiz 分频比）。
   - `ui_clk` = MIG 输出的用户时钟，由 MIG 内部 PLL 从 `sys_clk_i` 派生，**通常 `ui_clk` < `sys_clk`**（按 nCK_PER_CLK=4，`ui_clk` 通常是 `sys_clk` 的若干分之一，具体**待确认**）。
   - `mem_clk` = `ui_clk`（同一时钟域），因为 `mem_burst` 操作的就是 MIG 的 `app_*`。

3. **门控**：
   - `locked`（clk_wiz）：表示 MMCM 锁相完成、`sys_clk` 稳定。应门控「MIG 与状态机的复位释放」。本例 `locked` 悬空（[DynamicSeam.v:44](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L44)），是疏漏。
   - `init_calib_complete`（MIG）：表示 DDR3 物理校准完成、`app_*` 可用。应门控「所有 `app_*` 操作」。`mem_burst` 正确地在 [L102](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_burst.v#L102) 用它把关。

4. **`DynamicSeam` 的集成错误与修复**：
   - **例化写进 `always` 块**（[L184-265](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L184-L265)）：模块例化不能出现在过程块里。修复——把 `mig_7series_0 u_mig(...)` 提到模块顶层、与 `clk_wiz_0` 并列。
   - **端口 `ddr3_ck_n` 损坏**（[L195](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L195)）：端口名丢失只剩反引号。修复——改回 `.ddr3_ck_n(ddr3_ck_n),`。
   - **时钟域错**（[L121](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L121)）：状态机用 `posedge sys_clk` 驱动 `app_*`，但 `app_*` 在 `ui_clk` 域。修复——把状态机时钟改成 `posedge ui_clk`（与注释一致），并补上 `ui_clk`/`ui_clk_sync_rst`/`sys_rst` 的 `wire` 声明。
   - （附带）`cl_p` 拼写错误（[L46](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/动态规划法寻找最佳缝合线/DynamicSeam.v#L46)）应改为 `clk_p`。

> 说明：所有具体频率（200MHz、`ui_clk` 的确切值）都来自源码注释或 MIG 配置惯例，**仓库未收录 `.xci`/`.xdc`，数值待本地用 Vivado 打开 IP 后确认**。本实践的重点是「把时钟拓扑与端口接线画对」，而不是背数字。

## 6. 本讲小结

- **三个 IP 的分工**：`cordic_0` 用移位加法算 \(\sin/\cos\)；`clk_wiz_0` 把板载差分晶振变换成系统时钟；`mig_7series_0` 把 DDR3 物理时序封装成 `app_*` 应用接口。
- **CORDIC 接线**：相位走 AXI4-Stream slave（`s_axis_phase_tdata` 16 位，Q2.14 弧度），sin/cos 走 master（`m_axis_dout_tdata` 48 位 = 高 24 位 sin + 低 24 位 cos），`tvalid` 恒 1；sin/cos 各自的精确 Q 格式取决于 IP 配置，**待确认**——这是 u5-l1 推不动下游小数位对齐的根因。
- **Clocking Wizard 接线**：差分输入 `clk_in1_p/n`（200MHz，待确认）→ `clk_out1`→`sys_clk`；`reset` 极性存疑、`locked` 悬空、`cl_p` 拼写错误是三处草稿痕迹。
- **MIG 接线**：`sys_clk_i` 吃 clk_wiz 的 `sys_clk`，内部校准后从 `ui_clk` 输出用户时钟；`app_*` 全部在 `ui_clk` 域；`init_calib_complete` 是「才允许操作」的门控，`mem_burst` 在 [L102](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3控制/mem_burst.v#L102) 正确落地了它。
- **时钟拓扑**：`晶振 → clk_wiz → sys_clk → MIG(sys_clk_i) → ui_clk(=mem_clk)`；`sys_clk` 与 `ui_clk` 不同频，跨域必须做同步。
- **`DynamicSeam` 是反面教材**：MIG 例化写进 `always`、`ddr3_ck_n` 端口损坏、状态机在 `sys_clk` 域驱动 `app_*`（注释自己都写应该是 `ui_clk`）——读「有问题的 RTL」、识别并修复这类集成错误，是专家层的核心能力。

## 7. 下一步学习建议

- 想把定点、存储、缝合线、IP 集成**串成完整系统**，讨论资源占用与时序收敛取舍，请读本单元收尾篇 **u5-l3 系统集成、架构取舍与工程实践**。
- 想再回到 MIG 的「用户侧」、看一份**正确**的应用接口封装范本，请重读 **u3-l1 DDR3突发传输控制器 mem_burst** 和 **u3-l2 DDR3读写验证 mem_test**，把 `mem_burst` 的 `app_*` 端口与本讲 MIG 的端口表逐脚对照。
- 想深入 CORDIC 算法本身（为什么只靠移位加法就能逼近三角函数），建议在 Vivado 里打开 CORDIC IP 的产品指南（PG105），对照本讲的端口接线理解每个配置项的影响——这是补上「待确认」细节的唯一途径。
- 建议在 Vivado 里实际例化一次这三颗 IP（同型号 7 系列 FPGA），用 Block Design 或 HDL 把本讲的时钟拓扑连出来，用示波器/仿真观察 `locked`、`init_calib_complete`、`ui_clk` 的上电时序——把「待确认」变成「亲自确认过」。
