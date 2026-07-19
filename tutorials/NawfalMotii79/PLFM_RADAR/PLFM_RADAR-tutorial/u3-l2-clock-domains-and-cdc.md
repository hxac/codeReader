# 时钟域、复位同步与 CDC 基础

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `radar_system_top.v` 里**三个独立时钟域**（100MHz 系统 / 120MHz DAC / USB 接口时钟）各自的来源与用途，并理解为什么「不同时钟域之间的信号不能直接用一根线连过去」。
- 看懂「异步复位、同步释放」这一标准复位同步写法，理解 `(* ASYNC_REG = "TRUE" *)` 属性的作用，以及为什么 FT601 域要用 3 级同步而其它域用 2 级。
- 区分三类跨时钟域（CDC）场景各自该用什么电路：**单比特电平**用多级同步器、**多比特数据**用 Gray 码 + toggle 握手、**单周期脉冲**用 toggle-CDC，并理解「握手 CDC」的适用场合。
- 解释为什么 `new_chirp_frame`（120MHz 域的 1 拍脉冲）和 `cmd_valid`（USB 域的 1 拍脉冲）**绝不能用普通电平同步器**跨域——这是本讲的核心实践。
- 读懂 `cdc_modules.v` 提供的三个可复用 CDC 原语，并能在顶层例化点指出它们各自被用在哪条路径上。

本讲是 u3-l1（顶层全景）的紧接续篇：u3-l1 告诉你「跨域的地方有特殊电路」，本讲就专门拆开看这套「特殊电路」到底长什么样、为什么这么写。

## 2. 前置知识

先建立下面这些直觉（多数在 u3-l1 已铺垫，这里做一句话复习与补充）：

- **时钟域（clock domain）**：由同一棵时钟树驱动的所有触发器（flip-flop，简称 FF）构成一个域。同一域内的信号遵循同一节拍，时序分析工具能算清楚建立/保持时间；跨域的两个时钟彼此**异步**，采样时刻不可预测。
- **亚稳态（metastability）**：当一拍数据在违反建立/保持时间的时刻被某个 FF 采样，该 FF 的输出会卡在一个非 0 非 1 的中间电平上，停留一段随机时间后才随机地收敛到 0 或 1。这段时间里下游逻辑读到的值是不可靠的，可能导致整个状态机跑飞。CDC 电路的核心使命就是**把亚稳态收敛的概率压到可忽略**。
- **MTBF（平均故障间隔时间）**：衡量「亚稳态导致实际出错」的频率。CDC 同步器的级数越多，留给亚稳态收敛的时间越长，MTBF 按**指数**改善（粗略地说，残留概率随时间 \(t\) 按 \(e^{-t/\tau}\) 衰减）。所以「加一级」不是线性改善，而是几个数量级的改善。
- **BUFG**：Xilinx FPGA 的「全局时钟缓冲器」，把一个时钟送到芯片里**延迟最小、歪斜（skew）最一致**的全局时钟网络。综合时几乎每个真实时钟都要先过一颗 BUFG。
- **MMCM / PLL**：Xilinx 里的混合模式时钟管理器，能分频、倍频、移相、**清洗抖动**。它内部有锁相环，能把输入时钟的抖动「熨平」后再输出。
- **`reg` 与 `wire`**：`always` 块里赋值的是 `reg`（寄存器），连续赋值的是 `wire`。本讲大量出现用 `reg` 打的同步链。
- **`(* ASYNC_REG = "TRUE" *)`**：Vivado 的属性标记，告诉综合工具「这两个 FF 是跨时钟域同步链的一部分」，于是工具会把它们**摆在同一个 slice 里**（布线最短、采样窗口最稳）、且不会把它们优化掉。这是写 CDC 代码的「标配」。

> 名词小贴士：脉冲（pulse）指「只有效一拍」的信号；电平（level）指「持续有效直到被改变」的信号。这两者跨域的方式完全不同，是本讲反复强调的区分点。

## 3. 本讲源码地图

本讲围绕三个文件，它们是「顶层使用方 + CDC 原语库 + 时钟清洗器」的关系：

| 文件 | 作用 | 行数 |
|------|------|------|
| [`radar_system_top.v`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v) | 时钟域的「总调度」：缓冲三棵时钟、为每个域做复位同步、在跨域点例化 CDC 原语 | 1078 |
| [`cdc_modules.v`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/cdc_modules.v) | 三个可复用 CDC 原语：`cdc_single_bit`（单比特同步）、`cdc_adc_to_processing`（多比特 Gray+toggle）、`cdc_handshake`（握手） | 272 |
| [`adc_clk_mmcm.v`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/adc_clk_mmcm.v) | 用 MMCME2_ADV 给 400MHz ADC 数据时钟「清洗抖动」的封装模块（抖动清洗的示范） | 229 |

> 一个必须说清楚的边界：`adc_clk_mmcm.v` 在**当前顶层里并没有被例化**——它文件头明确写着自己是 `ad9484_interface_400m.v` 里那颗 BUFG 的「drop-in（直接替换）」升级件，用来在未来给 400MHz ADC 域做抖动清洗。本讲把它作为「时钟清洗」这个话题的真实范例来读，但不会假装它已经接进了 `radar_system_top`。真实的 400MHz ADC 时钟处理发生在接收机 `radar_receiver_final` → `ad9484_interface_400m` 内部（U4 系列）。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 时钟域与全局时钟缓冲（BUFG / MMCM）**、**4.2 复位同步（异步复位、同步释放）**、**4.3 CDC 同步器（单比特 / 多比特 Gray / 脉冲 toggle / 握手）**。三者层层递进：先有时钟，再有复位，最后才是跨域的数据/脉冲。

### 4.1 时钟域与全局时钟缓冲（BUFG / MMCM）

#### 4.1.1 概念说明

`radar_system_top` 同时活着三棵「各自为政」的时钟，它们来自不同的振荡器、彼此相位无关，所以构成了三个独立时钟域：

| 时钟端口 | 频率 | 来源 | 驱动的逻辑 |
|----------|------|------|-----------|
| `clk_100m` | 100 MHz | 板上系统时钟 | 主处理域：接收机 DDC/匹配滤波/Doppler、CFAR、自测试、命令译码 |
| `clk_120m_dac` | 120 MHz | DAC 采样时钟 | 发射域：chirp 波形生成、波束位置计数、帧脉冲 |
| `ft601_clk_in` | 100 MHz（FT601）或 60 MHz（FT2232H） | USB 控制器送来的时钟 | USB 接口域：数据包收发、主机命令解析 |

> 注意第三个时钟：FT601 模式下它名义上也是 100MHz，**和系统时钟同频**，但两者来自不同晶振，相位会缓慢漂移，仍属异步——这是后面解释「为什么 cmd_valid 也要 toggle CDC」的关键。

除此之外，芯片内部还有一颗 400MHz 的 ADC 数据时钟（`adc_dco`，来自 ADC 芯片自身），它在接收机内部被处理，并可用 `adc_clk_mmcm` 做抖动清洗。所以严格说全系统有「3 个顶层域 + 1 个 ADC 域」。

为什么要用 BUFG？因为一棵时钟要分发给成百上千个 FF，如果随便走普通布线，到达每个 FF 的时刻会参差不齐（clock skew），时序根本算不清。BUFG 走的是**专用全局时钟网络**，能保证全芯片「边沿对齐」。

#### 4.1.2 核心流程

顶层对待时钟的处理流程是：

1. 从外部引脚接入原始时钟（`clk_100m`、`clk_120m_dac`、`ft601_clk_in`）。
2. 每棵时钟过一颗 **BUFG**，得到低歪斜的全局时钟（`clk_100m_buf` 等），后续所有逻辑都用 `_buf` 版本。
3. 仿真模式下（`ifdef SIMULATION`）iverilog 没有 BUFG 原语，改用直接赋值直通。
4. 对「对抖动敏感」的高速时钟（400MHz ADC），可用 **MMCM** 先清洗抖动再分发。

伪代码：

```text
raw_clk ──► BUFG ──► clk_xxx_buf ──► 所有同域 FF

（可选，针对抖动敏感时钟）
raw_clk ──► MMCM(1:1, 抖动清洗) ──► BUFG ──► 干净的 clk
```

#### 4.1.3 源码精读

三颗 BUFG 的例化，文件头注释先讲清了三个域的频率：[radar_system_top.v:12-15](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L12-L15)。

顶层对时钟的缓冲用 `ifdef SIMULATION` 区分了仿真与综合两条路径：[radar_system_top.v:298-318](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L298-L318)。

```verilog
`ifdef SIMULATION
// iverilog 没有 BUFG，直接直通
assign clk_100m_buf     = clk_100m;
assign clk_120m_dac_buf = clk_120m_dac;
assign ft601_clk_buf    = ft601_clk_in;
`else
BUFG bufg_100m  ( .I(clk_100m),    .O(clk_100m_buf)    );
BUFG bufg_120m  ( .I(clk_120m_dac),.O(clk_120m_dac_buf));
BUFG bufg_ft601 ( .I(ft601_clk_in),.O(ft601_clk_buf)   );
`endif
```

这样做的好处是同一份 RTL 既能被 Vivado 综合上板，又能被 iverilog 跑回归仿真（u1-l4、u11-l1 会讲到这套双跑策略）。

抖动清洗的范例在 `adc_clk_mmcm.v`。它用 `MMCME2_ADV` 把 400MHz 输入配成 1:1 输出，靠内部锁相环把抖动从约 50ps 熨平到 20–30ps，目标是在 400MHz CIC 关键路径上多挤出 20–40ps 的时序裕量：[adc_clk_mmcm.v:1-41](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/adc_clk_mmcm.v#L1-L41)。

MMCM 的分频/倍频参数可以这样验算（VCO 必须落在 600–1200MHz 区间）：

\[
f_\text{VCO} = f_\text{in} \times \frac{\text{CLKFBOUT\_MULT\_F}}{\text{DIVCLK\_DIVIDE}} = 400 \times \frac{2.0}{1} = 800\,\text{MHz}
\]

\[
f_\text{CLKOUT0} = \frac{f_\text{VCO}}{\text{CLKOUT0\_DIVIDE\_F}} = \frac{800}{2.0} = 400\,\text{MHz}
\]

对应原语参数：[adc_clk_mmcm.v:112-127](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/adc_clk_mmcm.v#L112-L127)。它的输出再过一颗 `BUFG` 上全局网络，并用 `DONT_TOUCH` 阻止工具把这颗 BUFG 串成级联链（注释提到历史上出现过 4 颗 BUFG 串联、引入 243ps 延迟的真实教训）：[adc_clk_mmcm.v:204-223](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/adc_clk_mmcm.v#L204-L223)。它还输出 `mmcm_locked` 信号，供复位链判断「时钟稳了没有」（4.2 会用到这个思想）。

仿真路径同样做了直通处理：[adc_clk_mmcm.v:55-95](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/adc_clk_mmcm.v#L55-L95)，用一个计数器模拟「上电后约 4096 拍才锁定」，贴合真实 MMCM 的锁相时间。

#### 4.1.4 代码实践

**实践目标**：在源码里核对时钟频率与缓冲方式，建立「每个域一棵 BUFG」的直觉。

**操作步骤**：

1. 打开 [`radar_system_top.v` 第 298–318 行](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L298-L318)。
2. 数一数综合（`else` 分支）里例化了几颗 BUFG，分别叫什么名字、输入输出各是谁。
3. 打开 [`adc_clk_mmcm.v` 第 25–31 行](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/adc_clk_mmcm.v#L25-L31)的 MMCM 配置注释，用上面的公式手算 VCO 与输出频率。

**需要观察的现象**：综合分支里恰好有 3 颗 BUFG，与 3 个顶层时钟域一一对应；MMCM 算出的 VCO=800MHz 落在合法区间内。

**预期结果**：3 颗 BUFG（`bufg_100m` / `bufg_120m` / `bufg_ft601`）；MMCM 输出 400MHz，注释宣称抖动改善约 20–30ps。仿真分支则用 3 条 `assign` 直通。

#### 4.1.5 小练习与答案

**练习 1**：为什么仿真模式不用 BUFG，而是直接 `assign`？

**参考答案**：iverilog 等开源仿真器没有 Xilinx 的 BUFG/MMCM 原语行为模型，直接例化会报错或报 `x`。直通赋值在功能仿真里等价于「零延迟时钟」，足以验证逻辑正确性；真实的时序（歪斜、抖动）要靠 Vivado 综合后的时序仿真才看得到。

**练习 2**：`adc_clk_mmcm` 既然是「1:1」不改变频率，为什么还要加它？

**参考答案**：它的目的不是改频率，而是**清洗抖动**。ADC 数据时钟来自片外 ADC，带着 PCB 串扰和电源噪声；MMCM 的锁相环像个低通滤波器，把高频抖动衰减掉，输出一个更干净的 400MHz，从而降低采样不确定性、改善 CIC 关键路径的时序裕量（WNS）。它同时提供 `locked` 信号，便于复位时序判断时钟是否稳定。

---

### 4.2 复位同步：异步复位、同步释放

#### 4.2.1 概念说明

复位（reset）看起来简单，其实在多时钟域系统里是个大坑。核心矛盾是：复位信号 `reset_n`（低有效）本身是异步到达各个时钟域的，如果让它在**任意时刻**释放（从 0 回到 1），那么：

- 有些 FF 在这一拍看到复位释放、开始工作，有些 FF 在下一拍才看到——同一个域内的 FF「**不在同一拍退出复位**」，状态机可能直接进入非法状态。
- 释放时刻如果撞上时钟边沿，本身又会触发亚稳态。

业界标准解法叫 **「异步复位、同步释放」（async assert, sync deassert）**：

- **复位有效（assert）**：立刻生效，不等时钟——保证所有 FF 第一时间被强制到已知值。
- **复位释放（deassert）**：必须先过一条同步链，让释放动作**对齐到该域的时钟边沿**，保证域内所有 FF 同步退出复位、且不引入亚稳态。

每个时钟域都要有自己的复位同步器——因为「同步释放」是相对于**本域时钟**而言的，三棵时钟就要三套同步器。

#### 4.2.2 核心流程

复位同步器就是一个「移位寄存器」，在 `reset_n` 拉低时被异步清 0，在 `reset_n` 拉高后逐拍移入 1：

```text
reset_n ──(异步清0)──► [FF0] ──► [FF1] ──► sys_reset_n（取最后一级）

reset_n=0 期间：FF0=FF1=0 → sys_reset_n=0（复位持续生效）
reset_n 变 1 后：
  第 1 拍：FF0<=1, FF1<=0 → sys_reset_n=0
  第 2 拍：FF0<=1, FF1<=1 → sys_reset_n=1（复位同步释放）
```

注意 FF0 是唯一一个直面异步 `reset_n` 的触发器，它可能亚稳态；但只要给它一拍时间收敛，FF1 采样到的就是稳定值。所以「两级」是最低门槛，「三级」给 USB 这种最独立的时钟更多收敛时间。

#### 4.2.3 源码精读

100MHz 域的复位同步器，两级：[radar_system_top.v:320-329](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L320-L329)。

```verilog
(* ASYNC_REG = "TRUE" *) reg [1:0] reset_sync;
always @(posedge clk_100m_buf or negedge reset_n) begin
    if (!reset_n)
        reset_sync <= 2'b00;            // 异步清 0：立即生效
    else
        reset_sync <= {reset_sync[0], 1'b1}; // 同步释放：逐拍移入 1
end
assign sys_reset_n = reset_sync[1];     // 取最后一级作为域内复位
```

`always @(posedge clk or negedge reset_n)` 这个敏感列表里**同时有时钟上升沿和复位下降沿**，正是「异步复位」的标志——复位一到就生效，不等时钟。

120MHz 域如法炮制，得到 `sys_reset_120m_n`：[radar_system_top.v:331-342](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L331-L342)。

USB 域则用了**三级**，注释直说「3-stage for better MTBF」，因为 USB 时钟是片外 USB 控制器给的、与板上时钟最「不相关」，亚稳态窗口最大，多一级换取指数级的可靠性：[radar_system_top.v:344-355](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L344-L355)。

```verilog
(* ASYNC_REG = "TRUE" *) reg [2:0] reset_sync_ft601;  // 3 级，提升 MTBF
always @(posedge ft601_clk_buf or negedge reset_n) begin
    if (!reset_n)
        reset_sync_ft601 <= 3'b000;
    else
        reset_sync_ft601 <= {reset_sync_ft601[1:0], 1'b1};
end
assign sys_reset_ft601_n = reset_sync_ft601[2];
```

> 横向对比：100MHz/120MHz 域用 2 级、USB 域用 3 级——级数不是拍脑袋，而是按「该时钟与复位源的相关程度」决定的。级数越多，留给亚稳态收敛的时间越长，MTBF 越好，代价是多 1 拍延迟。

#### 4.2.4 代码实践

**实践目标**：用波形推演验证「同步释放」的效果。

**操作步骤**：

1. 读 [`radar_system_top.v:320-355`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L320-L355) 三段复位同步器。
2. 在纸上画一张时序图：横轴是 `clk_100m_buf` 的连续上升沿，假设 `reset_n` 在某两个边沿之间从 0 跳到 1。
3. 逐拍填写 `reset_sync[1:0]` 和 `sys_reset_n` 的值。

**需要观察的现象**：`reset_n` 跳 1 后，`sys_reset_n` 并不是立刻变 1，而是**延迟 2 拍**才变 1，且这个「变 1」严格发生在某个时钟上升沿。

**预期结果**：这是「同步释放」的核心证据——无论 `reset_n` 在一拍内的哪个时刻释放，`sys_reset_n` 都会在固定的时钟边沿上跳变，域内所有 FF 因此在**同一拍**退出复位。

> 待本地验证：若你装了 iverilog，可以写一个最小 testbench 给 `reset_n` 一个与时钟不对齐的释放沿，`$dumpvars` 看 `sys_reset_n` 是否恰好在某时钟沿跳 1。

#### 4.2.5 小练习与答案

**练习 1**：把复位同步器写成「同步复位」（敏感列表里只有 `posedge clk`，不要 `negedge reset_n`）会有什么问题？

**参考答案**：同步复位要求复位信号必须能被时钟「采到」才生效。如果上电时芯片还没稳定、或某棵时钟还没起振，复位就拿不进去，FF 可能停在随机值。异步复位保证「时钟还没来也能复位」，所以复位**assert**必须是异步的；只有 **deassert** 才需要同步。

**练习 2**：`(* ASYNC_REG = "TRUE" *)` 如果漏写会怎样？

**参考答案**：工具不知道这两个 FF 是 CDC 同步链，可能把它们摆得相距很远（布线长、采样窗口窄）、甚至因为「功能等价」把它们优化合并掉，MTBF 暴跌。写上后，Vivado 会把它们放在同一 slice、关闭优化，并把它们排除在普通时序约束之外（改用专门的 CDC 约束检查）。

---

### 4.3 CDC 同步器：单比特 / 多比特 Gray / 脉冲 toggle / 握手

#### 4.3.1 概念说明

复位解决「同一个域内如何干净地启动」，CDC 解决「信号如何安全地从一个域搬到另一个域」。按信号特征分四类，每类有且只有一种正确做法：

| 信号类型 | 例子 | 正确做法 | 本项目原语 |
|----------|------|----------|-----------|
| 单比特、慢变电平 | GPIO 使能、FIFO 满空标志 | 多级同步器 | `cdc_single_bit` |
| 单比特、窄脉冲 | 帧启动脉冲、命令有效脉冲 | **toggle-CDC**（脉冲↔电平） | `cdc_single_bit` + 边沿检测 |
| 多比特数据 | chirp 计数、状态字 | Gray 码 + toggle 握手 | `cdc_adc_to_processing` |
| 多比特、需应答 | 不规则突发传输 | 完整 req/ack 握手 | `cdc_handshake` |

**为什么多比特不能直接套单比特同步器？** 假设你把一个 6 位计数器每一位各过一级同步器，当源域在某一拍把计数从 `3'b011` 改成 `3'b100` 时，这 3 位同时翻转。由于各同步器的亚稳态收敛与布线延迟不同，目的域可能采到 `000`、`111`、`011`、`100` 中的任意一个——「数据撕裂」。

**Gray 码怎么救场？** Gray 码保证相邻两个数只有 1 位不同。源域先把二进制转 Gray 再过同步器，那么即使目的域刚好采在翻转瞬间，它读到的也只可能是「旧值或新值」之一（因为只有 1 位在变），绝不会是第三个乱码。再配合一个 toggle（每来一个新数据翻转一次），目的域靠「检测 toggle 变化」就知道有新数据到了。

**为什么脉冲要 toggle-CDC？** 这是本讲重头戏。`new_chirp_frame` 是 120MHz 域里**只亮一拍**（8.33ns）的脉冲；目的域是 100MHz，每 10ns 采样一次。一个 8.33ns 宽的脉冲很可能**整个夹在两次采样之间**被彻底漏掉，也可能被采到 1～2 次（取决于相位）。无论是漏采还是多采，结果都是灾难。toggle-CDC 的招数是：源域每来一个脉冲，就把一个电平「翻转」一次；电平是持久的，同步器一定能采到；目的域再做「边沿检测」——每检测到一次翻转就还原出一个脉冲。于是脉冲跨域变成了「电平跨域 + 边沿还原」，既不漏也不多。

#### 4.3.2 核心流程

四类 CDC 的统一心法是「在源域把信号整形成**对采样时刻不敏感**的形态，过同步链，再在目的域还原」。

**单比特同步器**：

```text
src_signal ──► [FF0] ──► [FF1] ──► [FF2] ──► dst_signal
              （可能亚稳） （收敛）  （稳定输出）
```

**toggle-CDC（脉冲跨域）**：

```text
源域：       每来一个 pulse，toggle = ~toggle
跨域：       toggle ──► 多级同步器 ──► toggle_sync
目的域：     pulse_out = toggle_sync ^ toggle_sync_prev（异或即边沿检测）
```

**多比特 Gray+toggle（`cdc_adc_to_processing`）**：

```text
源域：  data → binary_to_gray → gray_reg（寄存！）
        每来一个 valid：gray_reg 更新，toggle 计数器 +1
跨域：  gray_reg ──► STAGES 级同步链 ──► gray_sync
        toggle  ──► STAGES 级同步链 ──► toggle_sync
目的域： gray_to_binary(gray_sync) → data_out
        若 toggle_sync 变化 → dst_valid 拉一拍
```

**握手 CDC（`cdc_handshake`）**：

```text
源域：  valid 来且不忙 → 锁 data，置 busy(req)
        等 ack 同步回来 → 清 busy
跨域：  req（=busy） ──► 同步链 ──► 目的域
        ack          ◄── 同步链 ◄── 目的域
目的域： 见 req 且未持有 → 锁 data，置 valid，拉 ack
        消费后清 valid
```

#### 4.3.3 源码精读

**（1）单比特同步器 `cdc_single_bit`**——最简单也最常用，就是一个带同步复位的移位寄存器：[cdc_modules.v:145-167](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/cdc_modules.v#L145-L167)。

```verilog
(* ASYNC_REG = "TRUE" *) reg [STAGES-1:0] sync_chain;
always @(posedge dst_clk) begin
    if (!reset_n) sync_chain <= 0;
    else          sync_chain <= {sync_chain[STAGES-2:0], src_signal}; // 左移，新位进 LSB
end
assign dst_signal = sync_chain[STAGES-1]; // 取最后一级
```

注意它用的是**同步复位**（敏感列表只有 `posedge dst_clk`）——这是和「复位同步器」刻意区分的：复位信号在进入这个模块前，已经被 4.2 的同步器同步到本域了，所以这里只需同步复位即可，避免再引入一次异步路径。

**（2）多比特 Gray+toggle 同步器 `cdc_adc_to_processing`**。先看两个编解码函数。二进制转 Gray 只有一次异或：[cdc_modules.v:28-31](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/cdc_modules.v#L28-L31)。

```verilog
function [WIDTH-1:0] binary_to_gray;
    input [WIDTH-1:0] binary;
    binary_to_gray = binary ^ (binary >> 1);   // g[i] = b[i] ^ b[i+1]
endfunction
```

Gray 转二进制要逐位累加异或：[cdc_modules.v:33-44](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/cdc_modules.v#L33-L44)。

```verilog
binary[WIDTH-1] = gray[WIDTH-1];
for (i = WIDTH-2; i >= 0; i = i - 1)
    binary[i] = binary[i+1] ^ gray[i];   // 高位往低位逐位还原
```

源域一侧：把数据**先寄存一拍再转 Gray**（注释说这修复了「CDC-10 违例」——绝不让组合逻辑直接出现在第一级同步 FF 之前），并在每次 `src_valid` 时让 toggle 计数器 +1：[cdc_modules.v:62-72](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/cdc_modules.v#L62-L72)。

```verilog
always @(posedge src_clk) begin
    if (!src_reset_n) begin
        src_data_reg <= 0; src_data_gray <= 0; src_toggle <= 2'b00;
    end else if (src_valid) begin
        src_data_reg  <= src_data;
        src_data_gray <= binary_to_gray(src_data); // 寄存后的 Gray
        src_toggle    <= src_toggle + 1;            // 通知目的域「有新数据」
    end
end
```

目的域一侧：用 `generate` 生成 STAGES 级同步链，分别同步 Gray 数据和 toggle：[cdc_modules.v:78-108](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/cdc_modules.v#L78-L108)。最后把 Gray 转回二进制，并靠 toggle 是否变化来产生 `dst_valid`：[cdc_modules.v:111-128](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/cdc_modules.v#L111-L128)。

```verilog
dst_data_reg <= gray_to_binary(dst_data_gray[STAGES-1]);
if (dst_toggle_sync[STAGES-1] != prev_dst_toggle) begin
    dst_valid_reg <= 1'b1;                 // toggle 变了 = 新数据到了
    prev_dst_toggle <= dst_toggle_sync[STAGES-1];
end else dst_valid_reg <= 1'b0;
```

> 命名小贴士：模块名叫 `cdc_adc_to_processing`，但顶层实际用它来跨的是 **chirp 计数器**（120M→100M），并非 ADC。名字是历史遗留的通用多比特 CDC 语义，看实现别被名字误导。

**（3）握手 CDC `cdc_handshake`**。它把请求线和应答线分别各过一条 2 级同步链，源端用 `src_busy` 作请求、锁存数据，目的端看到请求后锁数据、回 `dst_ack`，源端看到 ack 后清 busy：[cdc_modules.v:173-271](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/cdc_modules.v#L173-L271)。这套机制适合「不连续、零星」的多字传输，代价是每次握手要来回跨域几拍，吞吐低。

**（4）顶层里真实使用这些原语的地方**。先看两个**单比特电平**同步：状态寄存器要采的 `stm32_mixers_enable`（异步 GPIO）和 `ft601_txe`（USB 域 FIFO 标志）都用 `cdc_single_bit` 2 级，因为它们是慢变电平，2 级够防亚稳态：[radar_system_top.v:357-373](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L357-L373)。

接着是**多比特数据**：chirp 计数器（6 位）从 120MHz 域搬到 100MHz 域，用 `cdc_adc_to_processing`（3 级），注意源端复位特意接了 `sys_reset_120m_n`（同步到**源**时钟域），注释解释了为什么：[radar_system_top.v:385-397](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L385-L397)。

最后是两条**脉冲 toggle-CDC**——本讲的实践主角。

`new_chirp_frame`（120M→100M），完整三步：源域把脉冲转成电平翻转 → 3 级同步 → 异或边沿检测还原脉冲：[radar_system_top.v:399-429](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L399-L429)。

```verilog
// 步骤1：源域（120M）每来一个脉冲就翻转
always @(posedge clk_120m_dac_buf or negedge sys_reset_120m_n)
    if (!sys_reset_120m_n) chirp_frame_toggle_120m <= 1'b0;
    else if (tx_new_chirp_frame) chirp_frame_toggle_120m <= ~chirp_frame_toggle_120m;

// 步骤2：把翻转后的电平过 3 级同步器到 100M 域
cdc_single_bit #(.STAGES(3)) cdc_new_chirp_frame (...);

// 步骤3：目的域异或边沿检测，还原成脉冲
assign tx_new_chirp_frame_sync = chirp_frame_toggle_100m ^ chirp_frame_toggle_100m_prev;
```

`cmd_valid`（USB 域→100M），**完全同样的三步模式**，注释里直说「same pattern as chirp_frame_toggle_120m above」：[radar_system_top.v:865-902](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L865-L902)。

> 关键点：`cmd_data/opcode/addr/value` 这些**多比特**字段并没有单独做 Gray 码同步——注释解释它们在 `cmd_valid` 脉冲之后「保持稳定」，所以等 toggle-CDC 把 valid 脉冲搬过来时，直接在目的域采样这些字段即可。这里的多比特安全性不是靠 Gray，而是靠「valid 先握手、数据随后稳定」的约定。一旦读 FSM 提前改了这些字段，就会出 bug——这正是 u11-l3「跨层契约测试」要盯的隐患。

#### 4.3.4 代码实践（本讲核心实践）

**实践目标**：在顶层定位 `new_chirp_frame` 与 `cmd_valid` 两条脉冲跨域路径，画出各自采用的 CDC 方式与同步器级数，并讲清楚「为什么不能用普通电平同步」。

**操作步骤**：

1. 打开 [`radar_system_top.v:399-429`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L399-L429)，定位 `new_chirp_frame` 路径（`tx_new_chirp_frame` → `tx_new_chirp_frame_sync`）。
2. 打开 [`radar_system_top.v:865-902`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L865-L902)，定位 `cmd_valid` 路径（`usb_cmd_valid` → `cmd_valid_100m`）。
3. 在纸上为每条路径画一张三段图：**源域翻转 → 同步器（写明级数）→ 目的域边沿检测**。
4. 对照 [`cdc_single_bit` 第 145–167 行](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/cdc_modules.v#L145-L167)，确认两条路径都复用了同一个原语、且都设了 `STAGES(3)`。

**需要观察的现象**：

- 两条路径结构**完全同构**——都是「toggle（源）→ `cdc_single_bit` 3 级（跨域）→ XOR 边沿检测（目的）」。
- 它们都没有用更重的 `cdc_adc_to_processing` 或 `cdc_handshake`，因为搬的是「事件脉冲」而非「数据字」。
- 命令字段（opcode/addr/value）没有自己的多比特同步，依赖「valid 先到、数据稳定」的约定。

**预期结果**（画出的图应长这样）：

```text
路径A: new_chirp_frame
  120M域: tx_new_chirp_frame(1拍脉冲) ─► 翻转 chirp_frame_toggle_120m
  跨域:   cdc_single_bit, STAGES=3 ─► chirp_frame_toggle_100m
  100M域: XOR(chirp_frame_toggle_100m, _prev) ─► tx_new_chirp_frame_sync(1拍脉冲)

路径B: cmd_valid
  USB域:  usb_cmd_valid(1拍脉冲) ─► 翻转 cmd_valid_toggle_ft601
  跨域:   cdc_single_bit, STAGES=3 ─► cmd_valid_toggle_100m
  100M域: XOR(cmd_valid_toggle_100m, _prev) ─► cmd_valid_100m(1拍脉冲)
```

**为什么不能用普通电平同步（关键回答）**：

1. `new_chirp_frame` 在 120MHz 域只有 **1 拍 = 8.33ns** 宽。目的域 100MHz 每 **10ns** 才采样一次。这个脉冲很可能**整个落在两次采样边沿之间**，电平同步器看到的是恒定的「没脉冲」，直接漏检。即便侥幸采到，相位漂移也会让相邻两次脉冲时而被采 0 次、时而被采 2 次，帧计数错乱。
2. `cmd_valid` 同理，是 USB 域的 1 拍脉冲。即使在 FT601 模式下 USB 时钟与系统时钟**同为 100MHz**，两者来自不同晶振、相位随机漂移，单拍脉冲仍可能撞在采样盲区。
3. toggle-CDC 把「短脉冲」翻译成「持久电平翻转」：电平会一直保持，直到下一次脉冲，所以**同步器一定能采到状态变化**；再用异或边沿检测，**每次翻转严格还原出 1 个脉冲**，既不漏也不重。这正是源码注释 [radar_system_top.v:399-402](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L399-L402) 所说的转换原理。

> 待本地验证：若你装了 iverilog，可以仿照 `9_2_FPGA/tb/` 下的 testbench 风格，给 120M 域一个 `tx_new_chirp_frame` 单拍脉冲、故意把 100M 时钟相位错开，dump 出 `chirp_frame_toggle_*` 与 `tx_new_chirp_frame_sync`，观察「不管相位怎么错，目的域每帧都恰好还原出一个脉冲」。

#### 4.3.5 小练习与答案

**练习 1**：`cdc_adc_to_processing` 里，为什么源域要先 `src_data_gray <= binary_to_gray(src_data)` **寄存一拍**，而不是直接把组合逻辑 `binary_to_gray(src_data)` 接到同步链第一级？

**参考答案**：CDC 铁律是「第一级同步 FF 之前不能有组合逻辑」。组合逻辑会产生毛刺（glitch），如果毛刺正好撞上目的域采样边沿，会被当成合法数据采进去。先把 Gray 结果寄存一拍，得到一个干净、对齐源时钟的信号再跨域，就消除了毛刺风险。注释里提到的「fixes CDC-10 violations」正是这条规则。

**练习 2**：假设你要把一个连续快速变化的 16 位计数器从 120M 域搬到 100M 域，用 `cdc_single_bit` 逐位同步可以吗？用 `cdc_adc_to_processing` 可以吗？为什么？

**参考答案**：逐位 `cdc_single_bit` 绝对不行——16 位同时翻转会被采到「撕裂」的中间值。`cdc_adc_to_processing` 也不适合「连续快速变化」：它靠 toggle 握手，目的域每次只在检测到 toggle 变化时更新，如果源域变得比目的域处理得还快，中间值会被覆盖丢失（它是「最新值采样器」，不是「无损队列」）。对这种场景，正确做法通常是异步 FIFO（本项目未在顶层使用，留作扩展）。Gray+toggle 只保证「采到的是某个真实曾出现过的值」，不保证不丢。

**练习 3**：`cmd_valid` 用了 toggle-CDC，但 `cmd_opcode/addr/value` 没有同步、也没用 Gray，为什么这样仍然安全？什么情况下会出问题？

**参考答案**：安全的前提是「读 FSM 在 `cmd_valid` 脉冲之后**保持**这些字段稳定，直到下一条命令」。这样当 toggle-CDC 把 valid 搬到 100M 域、目的域在 valid 那拍采样字段时，字段早已稳定，不存在多比特撕裂。会出问题的情况是：源域在 valid 脉冲后的几个周期内（即目的域同步链还在搬 valid 的那几拍里）就改写了 opcode/addr/value——此时目的域采样到的就是「半新半旧」的撕裂值。这正是为什么需要 u11-l3 的跨层契约测试去盯这种时序约定。

## 5. 综合实践

把本讲三个模块串起来，做一次「时钟/复位/CDC 全景体检」。

**任务**：通读 [`radar_system_top.v`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v) 第 294–429 行与第 865–902 行，绘制一张「跨域点登记表」，包含以下列：

| 跨域信号 | 源域 | 目的域 | 信号类型 | 采用的 CDC 方式 | 同步器级数 | 用到的原语/模式 |

至少填入以下 6 条路径：

1. `reset_n` → 各域复位（3 条：100M / 120M / ft601）
2. `stm32_mixers_enable` → `stm32_mixers_enable_100m`
3. `ft601_txe` → `ft601_txe_100m`
4. `tx_current_chirp` → `tx_current_chirp_sync`
5. `tx_new_chirp_frame` → `tx_new_chirp_frame_sync`
6. `usb_cmd_valid` → `cmd_valid_100m`

填完后，用一句话为每条路径解释「为什么用这种 CDC 而不是别的」，并把「级数差异」（2 级 vs 3 级）与「方式差异」（同步器 vs toggle vs Gray+toggle）背后的理由标注出来。

**参考要点（自检用）**：

- 3 条复位：异步复位+同步释放，USB 域 3 级、其余 2 级。
- mixers_enable、ft601_txe：单比特慢变电平 → `cdc_single_bit` 2 级。
- chirp 计数：6 位多比特数据 → `cdc_adc_to_processing`（Gray+toggle）3 级。
- new_chirp_frame、cmd_valid：单拍脉冲 → toggle-CDC（`cdc_single_bit` 3 级 + XOR 边沿检测）。

这张表就是你以后读任何多时钟域 FPGA 工程的「体检清单」。

## 6. 本讲小结

- 顶层有三个独立时钟域（100MHz 系统 / 120MHz DAC / USB 接口 100M 或 60M），每个域的时钟都先过一颗 **BUFG** 走全局低歪斜网络；400MHz ADC 域可用 `adc_clk_mmcm`（MMCME2_ADV）做抖动清洗，仿真路径与综合路径用 `ifdef` 分离。
- 复位采用 **「异步复位、同步释放」**：每个域各一套同步器（`reset_sync` / `reset_sync_120m` / `reset_sync_ft601`），USB 域因时钟最不相关而多一级到 **3 级**；`ASYNC_REG` 属性保证同步 FF 紧凑布局、提升 MTBF。
- CDC 按信号类型分四类对症下药：单比特电平用 `cdc_single_bit`；多比特数据用 `cdc_adc_to_processing`（Gray 码 + toggle，且 Gray 结果先寄存一拍挡毛刺）；单拍脉冲用 toggle-CDC；零星多字传输用 `cdc_handshake`。
- `new_chirp_frame`（120M→100M）和 `cmd_valid`（USB→100M）两条脉冲路径结构同构：**源域翻转电平 → 3 级 `cdc_single_bit` → 目的域 XOR 边沿检测还原脉冲**，正因为脉冲太窄（8.33ns）可能被 10ns 采样间隔漏掉，才必须用 toggle 而非电平同步。
- 命令字段（opcode/addr/value）本身不做多比特同步，依赖「valid 先到、数据随后稳定」的时序约定——这是跨层契约测试要盯的隐性契约。

## 7. 下一步学习建议

- **进入 U4（FPGA 接收信号处理链）**：现在你已经掌握「时钟域」的概念，U4-l1 讲 DDC 时会遇到最硬的一条跨域——400MHz ADC 域如何靠 CIC 抽取降到 100MHz，那正是「CDC 不能用同频 Gray」的真实场景。
- **阅读 `radar_receiver_final.v`**：看它内部如何把 400M ADC 域、100M 处理域串起来，验证本讲学到的 CDC 原语在子模块里是否被一致地使用。
- **预习 u11-l1 / u11-l3**：本讲提到的「CDC 规则」「跨层隐性契约」会在测试体系讲义里被反向验证——`run_regression.sh` 用 iverilog 跑 CDC 路径、`test_cross_layer_contract.py` 验证 opcode 在 Verilog/Python/C 三层一致。
- **预习 u14-l1（形式化验证）**：`formal/fv_cdc_handshake.sby` 用 SymbiYosys 对握手 CDC 做形式化证明，是从「仿真碰运气」走向「数学证明性质恒成立」的进阶。
