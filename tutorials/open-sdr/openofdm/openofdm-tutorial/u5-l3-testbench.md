# 仿真测试台 dot11_tb.v

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `dot11_tb.v` 在 OpenOFDM 项目中扮演的「激励发生器 + 探针」双重角色，并指出它**不是**可综合 RTL，而是一段只为仿真存在的驱动代码。
- 解释 `$readmemh` 如何把一个 hex 文本样本文件整块灌入 `ram` 数组，以及样本的 32 位 I/Q 排布约定。
- 推导出 100 MHz 时钟与 20 MSPS 采样率之间的 **5:1 关系**，并用 `clk_count == 4` 这一行代码逐拍还原「每 5 拍喂一个样本」的节拍发生器。
- 看懂 `$dumpfile/$dumpvars` 生成波形、`$fwrite` 把各阶段 strobe 信号落盘到 `sim_out/`，并理解这些格式串与 `test.py` 解析端构成的「对账契约」。
- 能够自己在测试台里加一个新探针，重新仿真，并验证它和既有输出一致。

## 2. 前置知识

在读本讲之前，请确认你已经了解（这些在依赖讲义中已建立）：

- **u1-l2**：仿真三件套（`iverilog` 编译、`vvp` 运行、`gtkwave` 看波形）；`verilog/Makefile` 里真正可用的目标是 `compile`/`simulate`/`all`/`clean`，注释里的 `make check`/`make display` 并未实现；默认 `NUM_SAMPLE=3000`，默认样本是 24 Mbps 的 dot11a 包。
- **u1-l4**：顶层 `dot11.v` 的端口分组，尤其是「数据 + strobe」握手风格——`sample_in[31:0]`（高 16 位 I、低 16 位 Q）配 `sample_in_strobe`，以及 `byte_out` 配 `byte_out_strobe`。
- **u5-l2**：`scripts/test.py` 的三段式编排（准备 → Python 期望 → 仿真对账），以及「第一失败阶段之前可信、之后不可信」的定位口诀。

本讲不会再重复上面这些结论，而是钻进 `dot11_tb.v` 这一个文件，回答：**样本是怎么一段一段喂进 DUT 的？各阶段信号又是怎么一段一段落盘的？**

几个本讲会用到的术语：

- **DUT（Design Under Test）**：被测设计，这里指 `dot11` 顶层模块实例。
- **测试台（testbench）**：一段只为仿真而写、不上板的 Verilog，负责产生时钟/复位/激励、观测并落盘内部信号。
- **strobe（选通）**：一个时钟周期宽的有效脉冲，表示「这一拍的数据有效」。OpenOFDM 全流水线靠它单向握手。
- **$readmemh**：Verilog 系统任务，把十六进制文本文件按行读入一个 memory 数组。
- **VCD（Value Change Dump）**：波形文件格式，`gtkwave` 直接打开。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|------|------|-----------|
| `verilog/dot11_tb.v` | 测试台本体，唯一精读对象 | 时钟/复位/激励发生、样本加载、5:1 节拍、波形与落盘探针 |
| `verilog/Makefile` | 构建脚本 | `compile`/`simulate` 如何调用 iverilog/vvp，`-DDEBUG_PRINT` 从哪来 |
| `verilog/common_params.v` | 全局参数 | `SR_SKIP_SAMPLE` 地址、`S_DECODE_DATA` 状态码 |
| `scripts/test.py` | 交叉验证驱动（u5-l2 已讲） | 它如何用 `-D` 覆盖样本与停止点、如何解析 `sim_out/*.txt` |
| `verilog/dot11_modules.list` | 编译清单（u1-l2/u1-l3 已讲） | 注意：测试台 `dot11_tb.v` 不在清单里，由命令行显式传入 |

> 注意一个易混淆点：`make` 流程和 `test.py` 流程**是两条独立的仿真入口**。`make simulate` 用测试台里写死的默认样本和 `NUM_SAMPLE=3000`；`test.py` 则用命令行 `-D` 覆盖这两者。本讲第 4 节会反复对照这两条入口。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：①测试台整体架构与职责；②样本加载（`$readmemh` 与 `ram` 数组）；③5:1 喂样节拍（`clk_count==4`）；④探针矩阵（波形与落盘）。

### 4.1 测试台的整体架构与职责

#### 4.1.1 概念说明

一个数字设计的「正确」要靠两件事来确认：一是**喂给它合适的输入**（激励），二是**观测它的内部和输出**（探针）。在 FPGA 上板时，激励来自天线、观测靠示波器/逻辑分析仪；在仿真里，这两件事都由一段叫做**测试台**的代码完成。

`dot11_tb.v` 就是 OpenOFDM 的测试台。它做三件事：

1. **造世界**：产生 100 MHz 时钟、上电复位序列、并通过配置总线写一个寄存器（把「跳过样本」关掉）。
2. **喂样本**：把一段抓包得到的 I/Q 样本，按 20 MSPS 的节奏一段段送进 `dot11` 实例。
3. **看里面**：把流水线各阶段的 strobe 信号和数据写到 `sim_out/` 下的文本文件，供 `test.py` 逐阶段比对；同时把全部信号 Dump 成 VCD 波形供人眼查看。

测试台本身**不可综合**，它只活在 `iverilog`/`vvp` 里，不会进 FPGA。正因如此，它可以用 `$readmemh`、`$fwrite`、`$dumpvars`、`#20` 这类「仿真专用」的系统任务和延迟语句——这些在真实硬件里没有对应物。

#### 4.1.2 核心流程

测试台的结构是经典的「两块并发」：

```
┌─────────────────────────────── module dot11_tb ───────────────────────────────┐
│                                                                               │
│  initial 块（顺序执行一次）          always 块（反复触发）                     │
│  ┌─────────────────────────┐        ┌────────────────────────────────────┐    │
│  │ $dumpfile/$dumpvars     │        │ 时钟发生: #5 clock=!clock  (100MHz)│    │
│  │ $readmemh(样本, ram)    │        │                                    │    │
│  │ reset=1; enable=0       │        │ @(posedge clock):                   │    │
│  │ #20 reset=0; enable=1   │        │   reset 分支: 清零各寄存器          │    │
│  │ 写 SR_SKIP_SAMPLE=0     │        │   enable 分支:                      │    │
│  │ $fopen 一堆 sim_out 文件│        │     - clk_count==4 → 喂一个样本     │    │
│  └─────────────────────────┘        │     - 各 $fwrite 探针按 strobe 落盘 │    │
│                                     │     - addr==NUM_SAMPLE → $finish    │    │
│                                     └────────────────────────────────────┘    │
│                                                                               │
│  dot11 dot11_inst ( ... 端口连线 ... );   ← 被测设计（DUT）                    │
└───────────────────────────────────────────────────────────────────────────────┘
```

要点：`initial` 块负责「开机一次性准备」，`always @(posedge clock)` 负责每个时钟沿的「喂样 + 探测」，两者通过共享的 `reg`（如 `clock`、`reset`、`sample_in`、`addr`）协作。最底下例化了被测的 `dot11`，把测试台的 `reg` 接到它的输入端口、把它的输出端口接到测试台的 `wire`。

#### 4.1.3 源码精读

测试台用一个 `initial` 块完成全部「开机准备」：先开波形、灌样本，再做复位/使能时序、写一个配置寄存器，最后打开一批落盘文件——见 [verilog/dot11_tb.v#L90-L134](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L90-L134)（`initial` 块，含 `$dumpfile`/`$readmemh`/复位序列/`$fopen` 矩阵）。

被测设计的例化在文件末尾，测试台把激励 `reg` 接输入、把观测 `wire` 接输出：[verilog/dot11_tb.v#L233-L281](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L233-L281)（`dot11 dot11_inst` 例化，注意它把 `clock/reset/enable/set_*/sample_in*` 接成激励，把 `state/power_trigger/.../byte_out*` 接成观测）。

观察例化列表可以发现一个重要事实：**测试台只引出了「便于交叉验证」的那部分调试端口**，例如 `demod_out`、`deinterleave_out`、`conv_decoder_out`、`descramble_out`、`byte_out` 都被引出来落盘了；而 FCS 相关的 `fcs_out_strobe`/`fcs_ok` 并没有接到测试台——所以本讲的实践任务如果要观测「包结束」，得另想办法（见第 5 节）。

#### 4.1.4 代码实践

**目标**：建立「测试台 = 激励 + 探针」的直觉。

**步骤**：

1. 打开 [verilog/dot11_tb.v#L233-L281](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L233-L281)。
2. 把端口分成两列：左列接 `reg`（激励，由测试台驱动）、左列接 `wire`（观测，由 DUT 驱动）。
3. 对照 u1-l4 的端口分组，标出哪些观测端口被引出、哪些没有（如 `fcs_out_strobe`、`status_code`）。

**需要观察的现象**：你会发现「流水线中间级（demod/deinter/conv/descramble）的输出都被引出了」，这正是为了支撑 u5-l2 的逐阶段对账——没有这些探针，`test.py` 就只能比最终字节、无法定位是哪一步出错。

**预期结果**：得到一张「端口 → 方向（激励/观测）→ 是否落盘」的三列表。

#### 4.1.5 小练习与答案

**练习 1**：测试台里为什么没有 `always @(negedge clock)`？  
**答案**：所有寄存器更新和探针采样都钉在 `posedge clock` 上，与 DUT 内部时序一致；`negedge` 只用于波形/保持时间裕量分析，本测试台不需要。

**练习 2**：如果要把 `fcs_ok` 也观测到，需要改测试台的哪两处？  
**答案**：① 在测试台顶部声明 `wire fcs_ok;`；② 在 `dot11_inst` 例化里加一行 `.fcs_ok(fcs_ok)`（前提是 `dot11.v` 确实有这个输出端口——见 u4-l5）。

---

### 4.2 样本加载：$readmemh 与 ram 数组

#### 4.2.1 概念说明

OpenOFDM 的输入是「一段已经数字化的基带 I/Q 样本流」。在真实场景里，它来自 USRP N210；在仿真里，我们提前把抓包得到的样本存成一个**文本文件**，每行一个 32 位十六进制数（高 16 位 I、低 16 位 Q，由 `scripts/bin_to_mem.py` 从 `.dat` 二进制转换而来，见 u5-l5）。

`$readmemh` 是 Verilog 内置系统任务：把一个 hex 文本文件**一次性**读入一个 memory 数组。它是仿真专属的「批量装填」——比逐拍 `$fscanf` 快得多，也更适合「样本即数据」的模型。

#### 4.2.2 核心流程

```
1. 声明一个大数组:  reg [31:0] ram [0:RAM_SIZE-1];   // RAM_SIZE = 1<<25 = 33,554,432
2. 声明读指针:      reg [31:0] addr;
3. initial 里:      $readmemh(SAMPLE_FILE, ram);     // 文件第 1 行 → ram[0], 第 2 行 → ram[1], ...
4. 喂样时:          sample_in <= ram[addr]; addr <= addr+1;
```

文件路径由宏 `SAMPLE_FILE` 指定，宏可被命令行 `-D` 覆盖；若不覆盖，则用一个写死的默认样本。读指针 `addr` 从 0 开始，每喂一个样本自增 1，整块 ram 就像一个「样本磁带」被顺序播放。

#### 4.2.3 源码精读

样本数组的规模声明见 [verilog/dot11_tb.v#L58-L61](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L58-L61)：`RAM_SIZE = 1<<25`（约 3355 万个 32 位字），`ram` 是这个大小的 memory，`addr` 是读指针。这容量远大于单个包（一个包通常几千样本），留足余量。

两个关键宏的默认值见 [verilog/dot11_tb.v#L82-L88](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L82-L88)：`` `ifndef SAMPLE_FILE `` 保护——只有当命令行没有 `-DSAMPLE_FILE=...` 时才用默认的 24 Mbps dot11a 样本路径；`NUM_SAMPLE` 同理默认 3000。这正是「`make` 用默认、`test.py` 用 `-D` 覆盖」的实现机制。

真正读文件的那一行在 [verilog/dot11_tb.v#L96-L96](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L96)：`` $readmemh(`SAMPLE_FILE, ram); ``——把整份 hex 文件灌入 `ram`。注意它前面有两行 `$display`（[L94-L97](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L94-L97)），仿真开始时会在终端打印正在读的文件名，方便确认样本没选错。

> 旁支：样本文件本身怎么来的？`testing_inputs/conducted/*.txt` 是用同轴直连抓的干净样本（见 `testing_inputs/conducted/readme.txt`），由 `bin_to_mem.py` 从 `.dat` 转成 hex 文本。这条工具链在 u5-l5 详讲。

#### 4.2.4 代码实践

**目标**：体会「换样本 = 换宏」。

**步骤**：

1. 查看 `testing_inputs/conducted/` 下还有哪些 `.txt` 样本（如 `dot11a_6mbps_...txt`、`dot11n_6.5mbps_...txt`）。
2. 用命令行覆盖默认样本编译运行（**不改源码**）：

   ```bash
   cd verilog
   iverilog -DDEBUG_PRINT \
            -DSAMPLE_FILE=\"../testing_inputs/conducted/dot11a_6mbps_qos_data_e4_90_7e_15_2a_16_e8_de_27_90_6e_42.txt\" \
            -DNUM_SAMPLE=3000 \
            -c dot11_modules.list dot11_tb.v -o dot11.out
   vvp -n dot11.out
   ```

3. 仿真开始时观察终端打印的文件名是否切换成功。

**需要观察的现象**：终端应打印你新指定的样本路径；`sim_out/byte_out.txt` 的内容会随速率变化（6 Mbps 包每符号承载的比特更少，字节数也不同）。

**预期结果**：终端第一行打印新样本路径，说明 `-D` 覆盖生效。这就是 `test.py` 能针对任意样本跑交叉验证的底层机制（test.py 在 [scripts/test.py#L64-L72](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L64-L72) 里做的就是这件事）。

> 若你对转义引号 `\"` 不熟：命令行里 `-DSAMPLE_FILE=\"...\"` 展开后，Verilog 看到的是字符串字面量 `"..."`，这才能喂给 `$readmemh` 当文件名。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `RAM_SIZE` 取 `1<<25`，而不是刚好等于样本数？  
**答案**：取 2 的整数次幂便于地址译码且留余量；样本长度不固定（包长可变），固定上限避免越界。代价是占用仿真内存，但 3355 万字对现代主机无压力。

**练习 2**：`$readmemh` 读到的 `ram[0]` 对应波形的哪一拍？  
**答案**：对应第一个 `sample_in_strobe` 有效拍（见 4.3）。在该拍 `sample_in <= ram[0]`，下一拍 DUT 才看到这个样本。

---

### 4.3 5:1 喂样节拍：clk_count==4 的由来

#### 4.3.1 概念说明

这是本讲最关键的一处设计，也是初学者最容易看漏的一行。

OpenOFDM 的 DUT 跑在 **100 MHz** 时钟上，但它的输入采样率是 **20 MSPS**（每秒 2000 万个样本）。两者之比：

\[
\frac{100\,\text{MHz}}{20\,\text{MSPS}} = \frac{100\times10^{6}}{20\times10^{6}} = 5
\]

也就是说，**每 5 个时钟周期才来一个有效样本**。DUT 的 `sample_in_strobe` 每 5 拍拉高一次，其余 4 拍为 0，告诉流水线「这拍的数据无效，别动」（见 u1-l4 的握手约定）。

测试台的任务就是**忠实地复现这个 5:1 节拍**：用一个模 5 计数器 `clk_count`，数到第 5 拍（计数值 == 4）时喂一个样本并拉高 strobe，否则保持低电平。

#### 4.3.2 核心流程

时钟先由一条独立的 `always` 生成：

```
always begin #5 clock = !clock; end   // 每 5ns 翻转一次 → 周期 10ns → 100MHz
```

然后在每个 `posedge clock` 里跑一个模 5 状态机：

```
if (clk_count == 4) begin          // 第 5 拍（计数值走完 0,1,2,3,4）
    sample_in_strobe <= 1;         //   → 拉高 strobe
    sample_in       <= ram[addr];  //   → 喂一个样本
    addr            <= addr + 1;   //   → 磁带前进一格
    clk_count       <= 0;          //   → 计数器归零
end else begin
    sample_in_strobe <= 0;         //   其余 4 拍: strobe 低，数据无效
    clk_count       <= clk_count + 1;
end
```

`clk_count` 取值序列是 `0→1→2→3→4→(命中)→0→1→...`，正好 5 个值一轮，strobe 每 5 拍有效一次。计数值「到 4 才命中」是因为从 0 开始数，第 5 拍的计数值是 4。

#### 4.3.3 源码精读

时钟发生器见 [verilog/dot11_tb.v#L137-L139](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L137-L139)：`always begin #5 clock = !clock; end`——`#5` 是 5ns（`timescale 1ns/1ps`，见 [L1](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L1)），每 5ns 翻转一次，完整周期 10ns，即 100 MHz。

5:1 节拍的核心逻辑见 [verilog/dot11_tb.v#L141-L156](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L141-L156)：`reset` 分支先把 `clk_count`/`sample_in_strobe`/`addr` 清零；`enable` 分支就是上面那段模 5 计数器。注意 `clk_count==4` 这一判定的位置——它决定了整个 DUT 的「心跳」。

复位/使能的时序见 [verilog/dot11_tb.v#L99-L114](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L99-L114)：`#20 reset=0; enable=1;` 释放复位并使能；紧接着一段是**通过配置总线写 `SR_SKIP_SAMPLE=0`**——见 [L107-L114](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L107-L114)。这一步非常关键：`power_trigger` 模块默认要跳过 `SR_SKIP_SAMPLE = 5,000,000` 个样本（避开上电毛刺，见 u2-l1），仿真里若不覆盖成 0，就要白等 500 万拍才能开始检测。`SR_SKIP_SAMPLE` 的地址定义在 [verilog/common_params.v#L17-L23](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v#L17-L23)（值为 5），写法遵循 u4-l4 讲过的 `set_stb/set_addr/set_data` 配置总线约定。

仿真结束条件见 [verilog/dot11_tb.v#L180-L182](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L180-L182)：`if (addr == \`NUM_SAMPLE) $finish;`。注意它嵌在 `if (sample_in_strobe && power_trigger)` 块内（[L161-L183](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L161-L183)），意味着只有「在 power_trigger 有效期间、且 addr 恰好走到 NUM_SAMPLE」时才停。由于包一旦被触发 `power_trigger` 会保持高（见 u2-l1），这条通常能命中；但若 `NUM_SAMPLE` 设得离谱（比如远小于包起点），仿真可能不停——这是一个易踩的坑。

> 关于 `$time/2` 时间戳：[L162-L166](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L162-L166) 里 `$fwrite(..., $time/2, ...)` 把仿真时间（ns）除以 2 当作行标签。它的作用是**让 `sample_in` 行和各检测信号行在同一拍共享相同标签，便于横向对齐**——注意它并不严格等于样本序号（每两个相邻 strobe 之间 `$time/2` 相差 25），所以只把它当「对齐用时间戳」，别当索引。

#### 4.3.4 代码实践

**目标**：在波形里亲手验证「每 5 拍一个 strobe」。

**步骤**：

1. 按 u1-l2 的方法 `make compile && make simulate`（用默认 24 Mbps 样本即可）。
2. `gtkwave dot11.vcd`，把 `clock`、`clk_count[15:0]`、`sample_in_strobe`、`addr[31:0]` 拖进视图。
3. 把光标移到某个 `sample_in_strobe` 的上升沿，数一下到下一个上升沿之间 `clock` 翻了几次、`clk_count` 走了 `0→1→2→3→4→0` 一轮。

**需要观察的现象**：两个相邻 strobe 之间正好 5 个时钟周期；`clk_count` 在 strobe 拍等于 4、下一拍归 0；`addr` 在每个 strobe 拍 +1。

**预期结果**：测得 strobe 周期 = 50 ns（5 × 10 ns），换算成采样率 = 1/50ns = 20 MSPS，与设计一致。

**待本地验证**：若你的机器上 `gtkwave` 不可用，可改为在 [L176-L178](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L176-L178) 的 `(addr % 100)==0` 分支里临时加一行 `$display("strobe at t=%0d", $time);`，从终端读周期。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `clk_count == 4` 改成 `clk_count == 9`，会发生什么？  
**答案**：变成每 10 拍一个样本，等效采样率降到 10 MSPS——低于 802.11 OFDM 的 20 MSPS 要求，DUT 会因样本不足而无法正确同步/解码，`byte_out.txt` 大概率全错或为空。这反向证明了 5:1 是硬约束。

**练习 2**：为什么 strobe 在 `clk_count==4` 而不是 `==0` 时拉高？  
**答案**：计数从 0 开始，第 5 拍的计数值是 4。若改成 `==0` 且初值为 0，复位后第一拍就触发、之后每 5 拍一次，语义上也可行，但当前代码选择「数满 5 个再发」，与复位时 `clk_count<=0` 配合，保证释放复位后先静默 4 拍再发第一个样本。

---

### 4.4 探针矩阵：$dumpvars 波形与 $fwrite 落盘

#### 4.4.1 概念说明

测试台的「探针」分两路输出：

- **波形（VCD）**：用 `$dumpfile`/`$dumpvars` 把**所有**信号在每个时刻的值变化记下来，存成 `dot11.vcd`，供 `gtkwave` 人眼浏览。优点是全、直观；缺点是文件大、机器不好解析。
- **落盘（文本）**：用 `$fwrite` 在特定 strobe 有效时，把**关键阶段的标量数据**按固定格式写到 `sim_out/*.txt`，每行一个采样。优点是小、结构化、便于 `test.py` 逐行 diff。

这两路是互补的：波形给人看、落盘给程序对账。OpenOFDM 的交叉验证（u5-l2）几乎完全依赖后者——`test.py` 读 `sim_out/*.txt`，与 Python 浮点参考解码器（u5-l1）的期望逐行比对。因此，**`$fwrite` 的格式串和 `test.py` 的解析逻辑构成一份隐式契约**：改了一端，必须同步另一端，否则对账会假错。

#### 4.4.2 核心流程

每个探针的模板是「门控 + 写入 + 刷新」：

```
if (<某状态条件> && <某 strobe>) begin
    $fwrite(fd, "<格式串>", <信号们>);
    $fflush(fd);          // 立即刷盘，防止 $finish 时缓冲丢失
end
```

「门控」决定**什么时候记**：前端检测信号（power_trigger 等）在 `sample_in_strobe && power_trigger` 时记（包到达后才有意义）；解码阶段信号（demod/deinter/conv/descramble/byte）额外要求 `dot11_state == S_DECODE_DATA`（只在解数据时记，避免把 SIGNAL 符号的解码混进来）。`S_DECODE_DATA = 11`，定义在 [verilog/common_params.v#L38-L38](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v#L38-L38)。

#### 4.4.3 源码精读

波形探针见 [verilog/dot11_tb.v#L91-L92](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L91-L92)：`$dumpfile("dot11.vcd"); $dumpvars;`——注意 `$dumpvars` 不带参数，表示 **dump 全设计所有信号**，文件会比较大但排查最方便。

落盘文件的打开矩阵见 [verilog/dot11_tb.v#L116-L133](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L116-L133)：每个 `sim_out/*.txt` 用 `$fopen(..., "w")` 打开，句柄存到对应的 `integer *_fd`（句柄声明在 [L63-L80](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L63-L80)）。前提是 `sim_out/` 目录已存在——仓库里它由一个 `.gitignore` 占位保证存在，否则 `$fopen` 会返回 0、后续 `$fwrite` 静默失败。

各探针的门控与写入集中在 [verilog/dot11_tb.v#L161-L228](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L161-L228)。下面这张表把每个 `$fwrite` 与 `test.py` 的解析端一一对应，就是「对账契约」的实物：

| 落盘文件 | TB 写入语句（行） | TB 格式串 | test.py 解析（行） | 对齐处理 |
|---------|------|-----------|------|---------|
| `byte_out.txt` | [L225-L228](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L225-L228) | `%02x\n` | [test.py#L199-L200](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L199-L200) `int(b,16)` | 无 |
| `signal_out.txt` | [L199-L203](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L199-L203) | `%04b %b %012b %b %06b` | [test.py#L83-L84](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L83-L84) | 每个 token `c[::-1]` 反转 |
| `demod_out.txt` | [L205-L208](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L205-L208) | `%06b\n` | [test.py#L130-L132](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L130-L132) | 取末 `n_bpsc` 位再 `[::-1]` |
| `deinterleave_out.txt` | [L210-L213](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L210-L213) | `%b%b\n`（先 bit0 后 bit1） | [test.py#L87-L88](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L87-L88) | 去换行直接拼接 |
| `conv_out.txt` | [L215-L218](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L215-L218) | `%b\n` | [test.py#L126-L127](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L126-L127) | 拼接 |
| `descramble_out.txt` | [L220-L223](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L220-L223) | `%b\n` | [test.py#L90-L91,L183](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L183-L183) | 前面补 7 个 0（LFSR 直装期无输出） |
| `sync_long_out.txt` | [L189-L192](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L189-L192) | `%d %d\n`（I, Q） | 仅波形/人工 | — |
| `equalizer_out.txt` | [L194-L197](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L194-L197) | `%d %d\n`（I, Q） | 仅波形/人工 | — |

读这张表的方式：**左端的格式串必须让右端能解析出期望值**。例如 `signal_out.txt` 写的是 `%04b`（4 位二进制，MSB 在前），但 802.11 的 SIGNAL 字段是「bit0 先发」，所以 test.py 端必须 `c[::-1]` 把字符串反转再比对——这就是 u5-l2 强调的「对齐细节」的来源，根因就在这张 TB 格式串表里。

> 关于 `$fflush`：几乎每个 `$fwrite` 后都跟一句 `$fflush(fd)`（如 [L168-L173](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L168-L173)）。这是因为 iverilog 默认缓冲写盘，若仿真中途 `$finish` 或崩溃，缓冲里未落盘的行会丢，导致 `test.py` 读到截断文件假报错。`$fflush` 强制立即刷盘，是用一点速度换确定性。

#### 4.4.4 代码实践

**目标**：读懂「门控条件」为何这样设。

**步骤**：

1. 对比 [L205-L208](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L205-L208)（demod 探针，门控 `dot11_state==S_DECODE_DATA`）和 [L189-L192](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L189-L192)（sync_long 探针，无状态门控）。
2. 回答：为什么 demod/deinter/conv/descramble/byte 都要加 `dot11_state==S_DECODE_DATA`，而 sync_long/equalizer 不用？
3. 设想：如果去掉 demod 探针的 `dot11_state==S_DECODE_DATA` 门控，`demod_out.txt` 会多出什么？

**需要观察的现象/推理**：解码子流水线在 `S_DECODE_SIGNAL` 阶段也会跑 demod→...→byte（用来解 SIGNAL 字段，见 u4-l2），那段时间的输出**不应该**进 `demod_out.txt`，否则会和 DATA 阶段的解调结果混在一起，对账必错。所以门控 `S_DECODE_DATA` 是为了**只留 DATA 阶段的输出**。前端 sync_long/equalizer 只在前端同步阶段有有效输出，无需此门控。

**预期结果**：去掉门控后 `demod_out.txt` 行数会变多（混入 SIGNAL 符号的解调），`test.py` 的 DEMOD 比对会报错。这是「门控即过滤器」的体现。

#### 4.4.5 小练习与答案

**练习 1**：`signal_out.txt` 为什么**不**加 `dot11_state==S_DECODE_DATA` 门控（见 [L199-L203](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L199-L203)）？  
**答案**：SIGNAL 字段是在 `S_DECODE_SIGNAL` 阶段解出的（u4-l2），那时状态还不是 `S_DECODE_DATA`。`legacy_sig_stb` 本身就只在 SIGNAL 解出那一拍有效，自带精确门控，不需要再叠状态条件。

**练习 2**：如果把 `byte_out_fd` 的格式串从 `%02x\n` 改成 `%d\n`（十进制），`test.py` 还能对账吗？  
**答案**：不能。test.py 端是 `int(b, 16)`（按十六进制解析，[L200](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/test.py#L200-L200)），TB 改成十进制后两端口径不一致，会假报 BYTE 错误。这演示了「对账契约」——改 TB 格式串必须同步改 test.py 解析。

---

## 5. 综合实践

**任务**：在 `dot11_tb.v` 里新增一个探针，统计 `S_DECODE_DATA` 期间 `byte_out_strobe` 的总次数，写入 `sim_out/byte_count.txt`，然后重新仿真并用 `test.py` 验证：① 整条流水线仍能正确解码（你的改动没破坏任何东西）；② 你统计的字节总数和既有 `byte_out.txt` 的行数完全一致。

这个任务把本讲四个模块串起来：要新增探针（4.4 模板）、要复用 `S_DECODE_DATA` 门控（4.4 对账契约）、要正确处理 `byte_out_strobe` 的时序（4.1 探针采样）、还要靠 `test.py` 的 `-D` 流程重新仿真（4.2/4.3）。

### 操作步骤

**第 1 步：声明计数器与文件句柄。** 在 [L80 附近](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L80-L80)（其它 `integer *_fd;` 旁边）加：

```verilog
integer byte_count_fd;
reg [15:0] byte_strobe_count;
```

**第 2 步：复位时清零计数器。** 在 reset 分支（[L142-L146](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L142-L146)）里加一行：

```verilog
byte_strobe_count <= 0;
```

**第 3 步：打开文件。** 在 `$fopen` 矩阵（[L133 附近](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L133-L133)）加：

```verilog
byte_count_fd = $fopen("./sim_out/byte_count.txt", "w");
```

**第 4 步：在既有 byte 探针里计数并落盘。** 把 [L225-L228](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11_tb.v#L225-L228) 的 byte 探针块改成：

```verilog
if (dot11_state == S_DECODE_DATA && byte_out_strobe) begin
    $fwrite(byte_out_fd, "%02x\n", byte_out);
    $fflush(byte_out_fd);

    byte_strobe_count <= byte_strobe_count + 1;          // 累加
    $fwrite(byte_count_fd, "%0d\n", byte_strobe_count);  // 每来一个字节写一行运行计数
    $fflush(byte_count_fd);
end
```

> 设计说明：这里采用「每来一个字节就写一行运行计数」的写法，最后一行即总数。这样**不需要**额外检测「包结束」（测试台没引出 `fcs_out_strobe`，见 4.1），也避开了 SystemVerilog `final` 块在 iverilog 上的兼容性问题。如果你愿意多接一根线，也可以在 `S_DECODE_DATA → S_DECODE_DONE` 跳变那一拍只写一次总数——两种都行，本方案改动最小。

**第 5 步：重新仿真。** 任选一条入口：

```bash
# 入口 A：用 test.py（推荐，它会自动 -D 覆盖样本与 stop，并跑完整对账）
cd /path/to/openofdm
python scripts/test.py testing_inputs/conducted/dot11a_24mbps_qos_data_e4_90_7e_15_2a_16_e8_de_27_90_6e_42.dat

# 入口 B：纯 make（用默认样本与 NUM_SAMPLE=3000）
cd verilog && make clean && make simulate
```

### 需要观察的现象与预期结果

1. **流水线未被破坏**：`test.py` 输出里 `DEMOD works!`/`DEINTER works!`/`CONV works!`/`DESCRAMBLE works!`/`BYTE works!` 全部出现——说明你加的探针只是「旁路观测」，没干扰数据通路。**这一点务必确认**，否则后面的计数对账无意义。
2. **计数自洽**：在 `verilog/sim_out/` 下执行

   ```bash
   wc -l sim_out/byte_out.txt sim_out/byte_count.txt
   ```

   两个文件的行数应当**完全相等**（因为你在和 byte_out_strobe 完全相同的条件下、同样次数地写了两个文件）。
3. **总数正确**：`tail -n 1 sim_out/byte_count.txt` 的值应等于 `wc -l < sim_out/byte_out.txt`。例如 24 Mbps 那个 QoS Data 包，`byte_out.txt` 通常是几十字节，你的计数器末值应与之相同。

**待本地验证**：不同速率/不同包长的样本，字节总数会不同；但「`byte_count.txt` 行数 == `byte_out.txt` 行数 == 末行计数值」这条不变式对所有样本都成立——这正是探针「与既有输出一致」的含义。

### 如果对不上怎么排查

- 行数不等 → 多半是第 4 步的 `if` 门控写错了（比如漏了 `dot11_state==S_DECODE_DATA`，混入了 SIGNAL 阶段的字节）。
- 计数永远是 0 → 多半是忘了在第 2 步复位清零，或第 3 步 `$fopen` 路径写错（`sim_out/` 不存在时句柄为 0，`$fwrite` 静默失败）。
- `test.py` 报 BYTE 错 → 检查你是否不小心改动了**既有** `byte_out_fd` 那行的格式串（应仍为 `%02x\n`）。

## 6. 本讲小结

- `dot11_tb.v` 是一段**不可综合**的仿真驱动代码，扮演「激励发生器 + 探针」双重角色：`initial` 块做开机准备，`always @(posedge clock)` 喂样本并落盘，末尾例化被测的 `dot11`。
- 样本靠 `$readmemh` 把 hex 文本整块灌入 `ram[0..2^25-1]` 数组，文件路径由 `SAMPLE_FILE` 宏决定，可被命令行 `-D` 覆盖（`make` 用默认、`test.py` 覆盖）。
- **5:1 节拍**是全讲核心：100 MHz ÷ 20 MSPS = 5，由模 5 计数器 `clk_count==4` 实现，每 5 拍拉一次 `sample_in_strobe`、喂一个 `ram[addr]`；测试台还通过配置总线把 `SR_SKIP_SAMPLE` 改成 0，避免仿真白等 500 万拍。
- 探针分两路：`$dumpvars` 生成全信号 VCD 波形供人看；`$fwrite` 在门控条件下把各阶段标量数据落盘到 `sim_out/*.txt` 供 `test.py` 对账，二者格式串构成必须同步维护的「对账契约」。
- 解码阶段探针（demod/deinter/conv/descramble/byte）统一加 `dot11_state==S_DECODE_DATA` 门控，**只留 DATA 阶段输出**、剔除 SIGNAL 阶段输出；`$fflush` 保证 `$finish` 前数据不丢。
- 新增探针的固定套路：声明句柄与计数 reg → 复位清零 → `$fopen` → 在合适门控里 `$fwrite` + `$fflush`，且**绝不触碰既有数据通路与格式串**。

## 7. 下一步学习建议

- **横向**：本讲只讲了「测试台怎么喂样、怎么落盘」。要理解喂进去的样本从哪来，接着读 **u5-l5（样本数据处理与测试样本集）**，看 `bin_to_mem.py` 如何把 `.dat` 二进制 I/Q 转成这里的 hex `.txt`，以及 `condense.py` 如何裁掉静默段缩短仿真时间。
- **纵向**：本讲的落盘探针是 u5-l2 交叉验证的「数据源」。建议回看 **u5-l2** 的对账顺序与对齐细节，把本讲的「对账契约」表和 test.py 的解析逻辑互相对照，理解为什么有些 token 要反转、有些要补 0。
- **进阶**：想给 OpenOFDM 加新特性时，往往要先在测试台加对应探针。可参考 **u6-l6（随机序列生成与自检 rand_gen.v）**，学习如何用 LFSR 产生伪随机激励做边界自检——那是比「回放抓包样本」更主动的测试思路。
- **动手**：完成第 5 节的综合实践后，尝试再接一根 `fcs_ok` 线（见 4.1.5 练习 2），在 `S_DECODE_DONE` 那一拍把 FCS 结果写进 `sim_out/fcs_ok.txt`，作为对 u4-l5（FCS 与 CRC32）的仿真验证。
