# 公共工具库 library/common

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `library/common` 在整个 ADI HDL 仓库中的「角色」：它不是某个可被单独打包的 IP，而是一个被各处 IP「按需引用源文件」的公共底层 RTL 池。
- 读懂 `ad_iobuf.v` 这个三态双向 IO 缓冲，并理解为什么几乎所有工程的 `system_top.v` 都会例化它。
- 读懂 `ad_mem.v`（等宽双口 RAM）与 `ad_mem_asym.v`（非对称双口 RAM）这两类存储原语，理解它们的端口语义、`ram_style` 综合属性，以及「读写位宽不等」时地址如何拼接。
- 看懂一个 library IP（以 `axi_dmac` 为例）的 `Makefile` 是如何把 `common/` 下的 `.v` 文件分别装进 `GENERIC_DEPS` / `INTEL_DEPS` / `LATTICE_DEPS` 这些「依赖桶」的，并与第 u4-l1、u4-l3 讲的「多厂商打包」知识串联起来。

## 2. 前置知识

本讲默认你已经理解：

- **IP 库与工程的分工**（u1-l2）：`library/` 是可复用 IP 积木，`projects/` 是拼装好的整板参考设计。
- **库的多厂商依赖桶**（u4-l1）：单个库 IP 的 `Makefile` 用 `GENERIC_DEPS`（三家共用）/ `XILINX_DEPS` / `INTEL_DEPS` / `LATTICE_DEPS` 四个桶来分组源文件与打包资产。
- **厂商打包差异**（u4-l3）：Xilinx 走「跨库引用已打包 IP」，Intel / Lattice 走「把 util 源码扁平嵌入」。

下面补充几个本讲会用到的 Verilog / FPGA 小概念，供不熟悉的者快速对齐：

- **三态（tristate）IO**：FPGA 的物理引脚可以配置成「输入 / 输出 / 高阻」三种状态。当方向控制位为 1 时，引脚对外呈现高阻（`z`），相当于「断开」，此时外部器件可以驱动这根线；方向为 0 时，FPGA 把内部信号驱动到引脚上。这就是双向总线（如 GPIO、I²C）能在同一根物理线上既收又发的原理。
- **双口 RAM（dual-port memory）**：一块存储同时暴露两套独立的端口（各自有时钟、地址、读/写控制）。ADI 这里用的是「一个端口写、一个端口读」的简单双口结构，常用于跨时钟域或缓冲数据。
- **`ram_style` 综合属性**：写在 `reg` 声明前的 `(* ram_style = "block" *)` 是给综合工具的提示，意思是「请把这片存储推断成片上块 RAM（BRAM）」，而不是分布式查找表（LUT RAM）。这是让一段纯 RTL 行为代码在不同工具上都落地成 BRAM 的常用技巧。
- **indexed part-select `+:`**：`data[base +: WIDTH]` 表示从 `base` 位开始、向上取 `WIDTH` 位，等价于 `data[base+WIDTH-1 : base]`，但 `base` 可以是变量，这在 `for` 循环里切片非常方便。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `library/common/ad_iobuf.v` | 三态双向 IO 缓冲，按位用 `generate` 展开，是物理引脚方向控制的标准封装。 |
| `library/common/ad_mem.v` | 等宽简单双口 RAM（写口 A / 读口 B 位宽相同），带 BRAM 综合属性与初始化。 |
| `library/common/ad_mem_asym.v` | 非对称双口 RAM：读写口位宽可以不等，靠地址拼接实现「宽窄转换」。 |
| `library/axi_dmac/Makefile` | 典型库 IP 的依赖清单，演示 `common/` 文件如何被装进各厂商依赖桶。 |
| `projects/pluto/system_top.v` | 工程顶层，演示 `ad_iobuf` 的真实例化方式。 |
| `library/axi_dmac/axi_dmac_burst_memory.v` | DMA 突发缓存，演示 `ad_mem_asym` 的真实例化方式。 |
| `library/util_rfifo/util_rfifo.v` | 跨时钟域 FIFO，演示 `ad_mem` 的真实例化方式。 |

## 4. 核心概念与源码讲解

### 4.1 library/common 的角色

#### 4.1.1 概念说明

把 `library/` 想成一座「零件厂」，里面有两类东西：

1. **被正式打包的 IP**（如 `axi_dmac`、`util_axis_fifo`）：每个都有 `Makefile`、`*_ip.tcl` / `*_hw.tcl` / `*_ltt.tcl`，会被打包成三家工具链可复用的 IP-XACT / Qsys / Radiant 组件，能被「拖进块设计」。
2. **`library/common/`**：一个 **纯 RTL 源文件池**。它**没有** `Makefile`、**没有** `*_ip.tcl`，本身不会被「打包成 IP」。

`common/` 的角色是「乐高积木的最底层颗粒」：别的 IP 在自己的 `Makefile` 里，按需把 `common/` 下某个 `.v` 文件**作为源文件**引用进来（`../common/xxx.v`），一起编译进自己的 IP 里。它是被「嵌入」，而不是被「引用为子 IP」。这样一份底层 RTL 就能在近百个 IP 之间复用，而无需各自重复实现。

#### 4.1.2 核心流程

`common/` 下目前有约 **57 个 `.v` 文件**，大致可分成几族：

| 族 | 代表文件 | 作用 |
|----|----------|------|
| 存储原语 | `ad_mem`、`ad_mem_asym`、`ad_mem_dual` | 行为级 RAM，靠 `ram_style` 落地成 BRAM |
| IO 缓冲 | `ad_iobuf` | 三态双向引脚控制 |
| 时钟 / 复位 / 同步 | `ad_rst`、`ad_pps_receiver`、`ad_sysref_gen`、`ad_clock_mon` | 复位释放、时间戳、SYSREF、时钟监测 |
| 数据格式化 | `ad_datafmt`、`ad_pack`、`ad_upack`、`ad_perfect_shuffle`、`ad_ss_422to444` | 拼包/解包、子采样、通道重排 |
| DSP | `ad_dds_*`、`ad_csc_*`、`ad_iqcor`、`ad_addsub` | DDS、色彩空间转换、IQ 校正、加减法 |
| 微处理器接口 | `up_axi`、`up_clkgen`、`up_adc_common`、`up_dac_channel`… | AXI-Lite 寄存器桥（详见 u4-l5） |
| 杂项小工具 | `ad_mux`、`ad_bus_mux`、`ad_edge_detect`、`util_delay`、`util_pipeline_stage` | 多路选择、边沿检测、延时、流水线寄存 |

> 注意：`up_*` 系列属于寄存器映射主题，本讲不展开，留到 u4-l5「up_axi 与寄存器映射」专门讲。本讲聚焦于**存储原语**与 **IO 缓冲**这两族最常被复用的底层颗粒。

一个库 IP 引入 `common/` 文件的标准动作，就是把对应 `.v` 加进 `Makefile` 的依赖桶。以 `axi_dmac/Makefile` 为例：

```make
GENERIC_DEPS += ../common/ad_mem_asym.v   # 三家都用到
GENERIC_DEPS += ../common/up_axi.v        # 三家都用到
```

这两行说明：`axi_dmac` 内部需要 `ad_mem_asym`（做突发缓存）和 `up_axi`（做寄存器桥），且**三家厂商都要编译它们**，所以放进 `GENERIC_DEPS`。

#### 4.1.3 源码精读

`axi_dmac` 把 `common/` 文件装进依赖桶：[library/axi_dmac/Makefile:L9-L10](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile#L9-L10) —— 这两行把 `ad_mem_asym.v` 与 `up_axi.v` 放进三家共用的 `GENERIC_DEPS`，意味着无论用哪家工具链构建 `axi_dmac`，这两个文件都会被一起编译。

而在 Lattice 分桶里，还**额外**多了一行 `ad_mem.v`：[library/axi_dmac/Makefile:L66-L72](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile#L66-L72) —— 注意这里把 `../common/ad_mem.v` 放进了 `LATTICE_DEPS`，但 `INTEL_DEPS` 与 `XILINX_DEPS` 里都没有它。原因正是 u4-l3 讲的「Lattice 扁平嵌入 util 源码」：Xilinx 侧 `util_axis_fifo` 是一个独立打包的 IP（通过 `XILINX_LIB_DEPS += util_axis_fifo` 跨库引用，见下一节），它的 BRAM 由 Xilinx 工具自行推断；而 Lattice 侧 `util_axis_fifo` 是直接用 `ad_mem` 搭出来的，所以在 Lattice 流程里必须把 `ad_mem.v` 一起扁平嵌入。

Xilinx 侧「跨库引用已打包 IP」的写法：[library/axi_dmac/Makefile:L53-L54](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile#L53-L54) —— `util_axis_fifo` 与 `util_cdc` 以库名形式出现在 `XILINX_LIB_DEPS`，引用的是它们打包好的 `component.xml`，而非源码。

对应地，Intel 侧把 `util_axis_fifo` / `util_cdc` 的源码**逐个文件**扁平嵌入：[library/axi_dmac/Makefile:L58-L64](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile#L58-L64)。

> 可验证的事实：`util_axis_fifo.v` 内部确实例化了 `ad_mem`（见 [library/util_axis_fifo/util_axis_fifo.v:L312](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_axis_fifo/util_axis_fifo.v#L312)）。这就是为什么 Lattice 流程必须额外带上 `ad_mem.v`，而 Xilinx 流程不必——后者直接复用了已经把 BRAM 推断好的 `util_axis_fifo` IP。

#### 4.1.4 代码实践

**实践目标**：亲手统计 `common/` 在仓库里的复用广度，建立「它被到处引用」的直观印象。

**操作步骤**：

1. 统计 `library/common/` 下的源文件数量：

   ```bash
   ls library/common/*.v | wc -l
   ```

2. 选中一个底层模块（例如 `ad_mem.v`），检索仓库里有哪些库 IP / 工程引用了它：

   ```bash
   git grep -l "ad_mem " -- '*.v' '*.tcl'
   ```

3. 再换一个（例如 `ad_mem_asym.v`）做同样检索，对比两者的引用方数量与分布（库 vs 工程）。

**需要观察的现象**：

- `library/common/` 本身没有 `Makefile`、没有 `*_ip.tcl`，确认它「不被单独打包」。
- `ad_mem` 的命中文件大多在 `library/`（库内部 IP），`ad_iobuf` 的命中文件大多在 `projects/`（工程顶层）。

**预期结果**：

- `ls library/common/*.v | wc -l` 约 **57**。
- 引用 `ad_mem` 的文件约 45 个、`ad_mem_asym` 约 37 个、`ad_iobuf` 则遍布几乎所有工程的 `system_top.v` / `system_project.tcl`（数量级上百，远多于前两者）。

> 注：本实践为只读检索型操作，不修改任何源码；若命令在你的环境行为略有差异，以本地实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `library/common/` 没有 `Makefile` 和 `*_ip.tcl`？

**参考答案**：因为它不是「一个 IP」，而是「被各 IP 按需引用源文件的底层池」。它存在的意义是被别的 IP 的 `Makefile` 用 `../common/xxx.v` 嵌入编译，而不是被工具链单独打包成可复用组件。

**练习 2**：`axi_dmac` 在 Xilinx 与 Lattice 流程下，引入 `ad_mem` 的方式有何不同？

**参考答案**：Xilinx 流程不直接引入 `ad_mem`——它通过 `XILINX_LIB_DEPS += util_axis_fifo` 复用已经打包好的 `util_axis_fifo` IP（其 BRAM 由工具推断）；Lattice 流程则需要把 `ad_mem.v` 显式写进 `LATTICE_DEPS` 扁平嵌入，因为 Lattice 侧的 `util_axis_fifo` 是基于 `ad_mem` 搭建的。

---

### 4.2 ad_iobuf IO 缓冲

#### 4.2.1 概念说明

`ad_iobuf` 解决的是一个极其常见的需求：**让一组物理引脚既能当输入、又能当输出**。

很多 ADI 评估板的控制信号（如收发器的使能、增益控制、状态回读）都走 PS（处理器系统）的 GPIO。PS 的一根 GPIO 线常常需要根据软件配置在「输出控制信号」与「回读外部状态」之间切换，也就是**双向三态**。`ad_iobuf` 就是把这个三态行为封装成一个可参数化位宽的标准模块，避免每个工程的 `system_top.v` 都手写一遍 `assign x = t ? 1'bz : i;`。

它几乎出现在每一个带 GPIO 的工程顶层里——这也是为什么它的引用数量远超 `ad_mem` 等存储原语。

#### 4.2.2 核心流程

`ad_iobuf` 的端口模型（一个 `DATA_WIDTH` 位的双向引脚组）：

| 端口 | 方向 | 含义 |
|------|------|------|
| `dio_t` | 输入 | 方向控制（tristate）：1 = 高阻（输入模式），0 = 驱动（输出模式） |
| `dio_i` | 输入 | FPGA 内部要输出到引脚上的数据 |
| `dio_o` | 输出 | 从引脚读回 FPGA 内部的数据 |
| `dio_p` | **inout** | 物理双向引脚 |

对每一位 `n` 的行为只有两行逻辑：

```
dio_o[n] = dio_p[n];                          // 永远把引脚电平回读进来
dio_p[n] = (dio_t[n]==1) ? 1'bz : dio_i[n];   // t=1 放手（高阻），t=0 把 dio_i 推出去
```

要点：

- 当 `dio_t=1`（高阻）时，`dio_p` 不被 FPGA 驱动，外部器件可以驱动这根线，于是 `dio_o` 读到的就是外部电平——这就是「输入」。
- 当 `dio_t=0`（驱动）时，FPGA 把 `dio_i` 推到 `dio_p` 上——这就是「输出」；此时 `dio_o` 读回的会和 `dio_i` 一致。
- 用 `generate` + `for` 把这个一位逻辑复制 `DATA_WIDTH` 次，得到一组独立可控的双向引脚。

#### 4.2.3 源码精读

模块声明与端口，参数 `DATA_WIDTH` 默认 1 位：[library/common/ad_iobuf.v:L38-L46](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/ad_iobuf.v#L38-L46) —— 一个参数 `DATA_WIDTH`、四个端口（方向控制、入、出、双向引脚），注意 `dio_p` 是 `inout`。

核心三态逻辑，用 `generate` 逐位展开：[library/common/ad_iobuf.v:L48-L54](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/ad_iobuf.v#L48-L54) —— `assign dio_o[n] = dio_p[n]` 持续回读引脚；`assign dio_p[n] = (dio_t[n]==1'b1) ? 1'bz : dio_i[n]` 是三态驱动，`z` 即高阻。

一个真实工程里的例化——pluto 的 `system_top.v`：[projects/pluto/system_top.v:L109-L118](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/pluto/system_top.v#L109-L118) —— 这里例化了一个 14 位的 `ad_iobuf`：`dio_t/dio_i/dio_o` 接到块设计里 GPIO 控制器的三态/出/入总线，`dio_p` 则把 14 根物理引脚（`gpio_resetb`、`gpio_en_agc`、`gpio_ctl`、`gpio_status`）打包成一组双向 IO。同一文件里还有一个 1 位的例化：[projects/pluto/system_top.v:L128-L134](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/pluto/system_top.v#L128-L134)。

> 回顾 u2-l2：差分 `LVDS` 缓冲由工具按 `IOSTANDARD` 自动推断，**不需要**显式例化 `ad_iobuf`；只有这种**单端、双向三态**的引脚才需要手写 `ad_iobuf`。这就是为什么 `ad_iobuf` 几乎只出现在 `system_top.v`，而与 `IOSTANDARD`/`PACKAGE_PIN` 的差分约束无关。

#### 4.2.4 代码实践

**实践目标**：把 `ad_iobuf` 的端口语义和一段真实例化对应起来，确认你理解了「方向、入、出、引脚」四者的连接对象。

**操作步骤**：

1. 打开 [projects/pluto/system_top.v:L109-L118](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/pluto/system_top.v#L109-L118)。
2. 找到 `i_iobuf` 的四个端口连接，分别回答：
   - `.dio_t(gpio_t[13:0])` 里的 `gpio_t` 来自哪里？（提示：往上找 `system_wrapper` 的例化，`gpio_t` 是块设计 GPIO 控制器的三态控制输出。）
   - `.dio_p({...})` 里那 14 个信号名，分别是哪些物理功能引脚？
3. 把 `DATA_WIDTH` 从 14 改成 1（仅做阅读推演，**不要真的改源码**），推演：此时 `dio_t/dio_i/dio_o/dio_p` 各应连到哪一根单线？

**需要观察的现象**：`dio_t`、`dio_i`、`dio_o` 一律来自/去往 `system_wrapper`（块设计），而 `dio_p` 一律对应真实物理引脚名（如 `gpio_resetb`）。这正是 `ad_iobuf` 的连接规律。

**预期结果**：你能画出「块设计 GPIO 三态控制 → `ad_iobuf` → 物理引脚」的数据通路；并理解「软件把某根 GPIO 配成输出，等于把对应 `dio_t` 置 0」。

#### 4.2.5 小练习与答案

**练习 1**：如果 `dio_t[n]=1`，`dio_o[n]` 读到的是什么？

**参考答案**：`dio_o[n] = dio_p[n]` 始终成立。当 `dio_t[n]=1` 时 FPGA 放手（`dio_p` 呈高阻），引脚电平由外部器件决定，所以 `dio_o[n]` 读到的是**外部器件驱动进来的电平**。

**练习 2**：为什么 `ad_iobuf` 用 `generate` 而不是直接写一个向量级的 `assign dio_p = dio_t ? 'bz : dio_i`？

**参考答案**：逐位用 `generate` 可以让每一根引脚独立决定方向（有的在输出、有的在输入），更贴近真实 GPIO「位方向可配」的语义；也便于综合工具为每一位推断独立的 IOB（IO Block）三态缓冲。

---

### 4.3 ad_mem 系列存储原语

#### 4.3.1 概念说明

ADI 把「一段行为级 RAM」抽象成两个可复用模块：

- **`ad_mem`**：**等宽**双口 RAM。写口 A 与读口 B 的数据位宽相同，只是分别由 `clka` / `clkb` 两个独立时钟驱动——天然适合做跨时钟域的小缓冲。
- **`ad_mem_asym`**：**非对称**双口 RAM。读写口的数据位宽**可以不相等**，常用于「宽内部数据通路 ↔ 窄外部存储」的位宽转换，例如 DMA 把 256 位的内部突发写入，再以 64 位读出给外部 AXI 总线。

两者的共同点：都是用纯 RTL 的 `reg` 数组 + `always @(posedge clk)` 行为来描述存储，再靠 `(* ram_style = "block" *)` 综合属性，让三家工具链都把它推断成片上 BRAM，而不是吃掉宝贵的 LUT 资源。这样一段代码就能在 Vivado / Quartus / Radiant 上都落地成 BRAM，体现了 `common/` 的「厂商无关」本性。

#### 4.3.2 核心流程

**`ad_mem`（等宽）的端口**：

| 端口 | 时钟域 | 含义 |
|------|--------|------|
| `clka / wea / addra / dina` | A | 写口：时钟、写使能、写地址、写数据 |
| `clkb / reb / addrb / doutb` | B | 读口：时钟、读使能、读地址、读数据（寄存输出） |

行为很直白：

```
posedge clka: if (wea) m_ram[addra] <= dina;   // 同步写
posedge clkb: if (reb) doutb <= m_ram[addrb];  // 同步读，输出寄存一拍
```

注意 `doutb` 是 `reg`（寄存输出），所以读数据比地址晚一拍——这是 BRAM 的典型时序。

**`ad_mem_asym`（非对称）的关键约束**：

读写两侧的总容量（比特数）必须相等：

\[
2^{\text{A\_ADDRESS\_WIDTH}} \times \text{A\_DATA\_WIDTH} \;=\; 2^{\text{B\_ADDRESS\_WIDTH}} \times \text{B\_DATA\_WIDTH}
\]

也就是说，宽的一侧地址少、窄的一侧地址多，但总比特数相同。内部统一按**最窄的那一侧**来切分存储颗粒：

- 设 \(\text{MIN\_WIDTH} = \min(\text{A\_DATA},\text{B\_DATA})\)，\(\text{MEM\_RATIO} = \max / \min\)。
- 存储颗粒宽度 = `MIN_WIDTH`，颗粒数 = \(2^{\max(\text{A\_ADDR},\text{B\_ADDR})}\)。
- 「宽的一侧」一次访问会触达 `MEM_RATIO` 个连续颗粒。地址拼接方式为 \(\{\text{addr}, \text{lsb}\}\)，其中 `lsb` 是 `MEM_RATIO_LOG2` 位的子选择位。

于是分四种情况（由 `generate if` 选择其中两种互斥分支）：

| 情况 | 写侧 | 读侧 | 行为 |
|------|------|------|------|
| A 写更宽 | 一次写 → `MEM_RATIO` 个颗粒 | 单颗粒读 | `m_ram[{addra, lsb}] <= dina[i*MIN +: MIN]` |
| A 写更窄 | 单颗粒写 | 一次读 → `MEM_RATIO` 个颗粒拼装 | `doutb[i*MIN +: MIN] <= m_ram[{addrb, lsb}]` |

其中 `+:` 是「从某位起取 WIDTH 位」的可变起点切片。这样无论宽窄，都复用同一片最细颗粒的 BRAM。

#### 4.3.3 源码精读

**`ad_mem`：**端口与存储声明：[library/common/ad_mem.v:L38-L55](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/ad_mem.v#L38-L55) —— 参数 `DATA_WIDTH` / `ADDRESS_WIDTH`；存储数组 `m_ram` 前的 `(* ram_style = "block" *)` 把它推断为 BRAM；`doutb` 声明为 `reg`（寄存输出）。

写口与读口的两个 `always` 块：[library/common/ad_mem.v:L76-L86](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/ad_mem.v#L76-L86) —— A 时钟域同步写、B 时钟域同步读，正是简单双口 RAM 的标准写法。注意 [L58-L70](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/ad_mem.v#L58-L70) 的初始化 `initial` 块：当 `ADDRESS_WIDTH > 10` 时把循环拆成「外层 1024 × 内层」两级，避免对超大数组直接 `2**N` 一次展开导致仿真启动极慢——一个面向仿真的小心思。

**`ad_mem_asym`：**端口与约束：[library/common/ad_mem_asym.v:L41-L58](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/ad_mem_asym.v#L41-L58) —— 文件头注释明确写了「读写空间总容量必须相等」的约束；端口分 A（写）口与 B（读）口，各自有独立的地址 / 数据位宽参数，还有一个 `CASCADE_HEIGHT` 用于控制 BRAM 级联高度。

关键 localparam（存储颗粒化）：[library/common/ad_mem_asym.v:L76-L82](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/ad_mem_asym.v#L76-L82) —— `MEM_DATA_WIDTH = MIN_WIDTH`（按最窄侧切颗粒）、`MEM_RATIO = MAX/MIN`、`MEM_RATIO_LOG2 = clog2(MEM_RATIO)`，这正是上一节公式里那几个量。`clog2` 自定义函数见 [L63-L74](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/ad_mem_asym.v#L63-L74)。

四种情况的选择逻辑：[library/common/ad_mem_asym.v:L94-L145](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/ad_mem_asym.v#L94-L145) —— 「A 写更宽」分支（[L104-L116](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/ad_mem_asym.v#L104-L116)）用一个 `for` 把宽 `dina` 切成 `MEM_RATIO` 段、写到 `{addra, lsb}`；「B 读更宽」分支（[L133-L145](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/ad_mem_asym.v#L133-L145)）反向地把多个颗粒读出并拼装进宽 `doutb`。

**真实例化**：

- `ad_mem_asym` 在 DMA 突发缓存里做位宽转换：[library/axi_dmac/axi_dmac_burst_memory.v:L378-L386](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_burst_memory.v#L378-L386) —— A 口（`src_*`）与 B 口（`dest_*`）各有独立地址 / 数据位宽，正是「源侧宽、目标侧窄」（或反之）的缓冲场景。
- `ad_mem` 在跨时钟域 FIFO 里做存储核：[library/util_rfifo/util_rfifo.v:L392-L399](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_rfifo/util_rfifo.v#L392-L399) —— 写口在 `din_clk`、读口在读时钟，等宽双口充当 FIFO 的存储体。

#### 4.3.4 代码实践

**实践目标**：体会 `ad_mem_asym` 的「位宽不等但容量相等」约束，并能验证一个具体配置是否合法。

**操作步骤**：

1. 阅读 [library/common/ad_mem_asym.v:L36-L37](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/ad_mem_asym.v#L36-L37) 的约束注释。
2. 看 `axi_dmac_burst_memory.v` 里那次例化（[L378-L386](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_burst_memory.v#L378-L386)）的四个位宽参数 `ADDRESS_WIDTH_SRC / DATA_WIDTH_MEM_SRC / ADDRESS_WIDTH_DEST / DATA_WIDTH_MEM_DEST`，在文件中向上找到它们的来源（通常由 DMA 的 `BYTES_PER_BURST`、`SRC_DATA_WIDTH`、`DEST_DATA_WIDTH` 等参数推算）。
3. 代入约束公式做一次手工验算：

   \[
   2^{\text{A\_ADDR}} \times \text{A\_DATA} \;\stackrel{?}{=}\; 2^{\text{B\_ADDR}} \times \text{B\_DATA}
   \]

   如果某组配置不满足，说明该 DMA 配置非法（突发缓冲容量两侧不平衡）。

4. （可选）在一张纸上画出：当 `DATA_WIDTH_MEM_SRC = 256`、`DATA_WIDTH_MEM_DEST = 64` 时，一次 256 位写如何被切成 4 个 64 位颗粒写入 `{addra, 2'bxx}`。

**需要观察的现象**：源侧与目标侧位宽不相等时，宽侧地址位更少、窄侧地址位更多，但二者相乘的总比特数必须一致；这正是 `ad_mem_asym` 能用同一片最细颗粒 BRAM 桥接两侧的原因。

**预期结果**：你能口述「为什么 `axi_dmac` 用 `ad_mem_asym` 而不是 `ad_mem`」——因为 DMA 的源 / 目标总线位宽常常不同（例如 AXI-Stream 256 位 ↔ AXI-MM 64 位），需要位宽转换。具体的突发缓冲容量数值**待本地验证**（取决于 `axi_dmac` 顶层参数化取值）。

#### 4.3.5 小练习与答案

**练习 1**：`ad_mem` 的读数据 `doutb` 相对于读地址 `addrb` 晚几拍？为什么？

**参考答案**：晚一拍。因为 `always @(posedge clkb)` 里 `doutb <= m_ram[addrb]`，`doutb` 是寄存输出，这是 BRAM「同步读」的标准时序模型——读地址在时钟沿给出，数据在下一个时钟沿才出现在 `doutb`。

**练习 2**：给定 `ad_mem_asym` 配置 A 口 256 位 × 8 深度，B 口应是多少位 × 多少深度才合法？写出至少两种解。

**参考答案**：总容量 \(= 256 \times 2^8 = 65536\) 比特。合法的 B 口只要满足 \(2^{B\_ADDR} \times B\_DATA = 65536\) 且 `B_DATA` 能整除（或被整除）256。例如：
- B 口 64 位 × \(2^{10}\) 深度（\(64 \times 1024 = 65536\)）；
- B 口 128 位 × \(2^{9}\) 深度；
- B 口 256 位 × \(2^{8}\) 深度（此时退化为等宽）。

**练习 3**：为什么 `ad_mem` / `ad_mem_asym` 都要写 `(* ram_style = "block" *)`？

**参考答案**：这两段存储是用 `reg` 数组 + `always` 描述的，不写属性时，不同工具链可能把它推断成分布式 LUT RAM（消耗宝贵 LUT）而非 BRAM。`ram_style = "block"` 是给三家工具的统一提示，让它尽量落地成 BRAM，保证「同一段 RTL 在 Vivado / Quartus / Radiant 上行为与资源一致」。

## 5. 综合实践

把本讲的三个模块串起来，做一次「从依赖声明到 RTL 例化」的追踪：

1. 打开 [library/axi_dmac/Makefile](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile)。
2. 找到 `ad_mem_asym.v` 所在的依赖桶（`GENERIC_DEPS`，[L9-L10](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile#L9-L10)），说明它对三家厂商都生效；再找到 `ad_mem.v` 所在的桶（`LATTICE_DEPS`，[L66-L72](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile#L66-L72)），说明它只对 Lattice 生效。
3. 解释「为什么 `ad_mem_asym` 三家共用，而 `ad_mem` 只 Lattice 需要」：`ad_mem_asym` 是 `axi_dmac` 自己的突发缓存（`axi_dmac_burst_memory.v`）直接用的；`ad_mem` 则是因为 Lattice 侧的 `util_axis_fifo` 基于它搭建，被 Lattice 流程间接拉入。
4. 最后挑一个带 GPIO 的工程顶层（如 [projects/pluto/system_top.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/pluto/system_top.v)），找出 `ad_iobuf` 的例化，把「块设计 GPIO 三态控制 → `ad_iobuf` → 物理引脚」这条通路和上面两块存储原语并列写进一张「`common/` 复用全景图」：存储原语被库 IP 内部消化（`library/`），IO 缓冲被工程顶层消化（`projects/`）。

> 这个练习把「依赖桶机制（u4-l1/u4-l3）」与「底层 RTL 复用」打通：同一个 `common/` 文件，因厂商流程不同，可能落在不同的桶里；同一个 `ad_iobuf`，因用途不同，几乎只出现在 `system_top.v`。理解这两点，你就真正看清了 `library/common` 在仓库里的「底层公共池」地位。

## 6. 本讲小结

- `library/common/` 是一个**不会被单独打包**的纯 RTL 源文件池（约 57 个 `.v`），各 IP 通过 `Makefile` 的依赖桶按需把其中文件「嵌入编译」。
- `ad_iobuf` 是可参数化位宽的**三态双向 IO 缓冲**，靠 `generate` 逐位实现「`t=1` 高阻输入、`t=0` 驱动输出」，几乎所有带 GPIO 的工程顶层都会例化它。
- `ad_mem` 是**等宽简单双口 RAM**（同步写、寄存同步读），用 `ram_style="block"` 落地成 BRAM；对超大深度的初始化做了两级循环优化。
- `ad_mem_asym` 是**非对称双口 RAM**，遵守「读写总容量相等」约束，按最窄侧切颗粒、用 `{addr, lsb}` 地址拼接实现宽窄转换，常用于 DMA 的源 / 目标位宽桥接。
- 同一个 `common/` 文件可能因厂商流程不同而落入不同依赖桶：`ad_mem_asym` 三家共用（`GENERIC_DEPS`），`ad_mem` 仅 Lattice 需要（`LATTICE_DEPS`，因 Lattice 侧 `util_axis_fifo` 基于它）——这是 u4-l3「Lattice 扁平嵌入 util 源码」的具体体现。
- 复用广度排序：`ad_iobuf`（遍布 `projects/`，上百处） ≫ `ad_mem`（约 45 处，多在 `library/`） ＞ `ad_mem_asym`（约 37 处）。

## 7. 下一步学习建议

- **接 u4-l5「寄存器映射与 up_axi」**：本讲只点了 `common/up_axi.v` 是「寄存器桥」，下一讲会拆开讲它如何把 AXI4-Lite 翻译成内部寄存器读写，并结合 `*_regmap.v` 与 `docs/regmap` 的寄存器表对齐。
- **接 u5-l1「axi_dmac 深入」**：本讲看到 `ad_mem_asym` 在 `axi_dmac_burst_memory.v` 里做位宽转换，下一讲会从 `axi_dmac` 顶层完整看清 src → burst memory → dest 的数据通路。
- **接 u5-l3「util 工具 IP」**：本讲提到 `util_axis_fifo` 在 Lattice 侧基于 `ad_mem`，下一讲会把 `util_axis_fifo` / `util_cdc` / `util_pack` 等胶水 IP 系统讲一遍，看清它们如何在数据通路里「缝合」各 IP。
- 想动手验证者，可先做本讲的「综合实践」，把一张「`common/` 复用全景图」画出来，作为后续读 IP 内部数据通路的索引。
