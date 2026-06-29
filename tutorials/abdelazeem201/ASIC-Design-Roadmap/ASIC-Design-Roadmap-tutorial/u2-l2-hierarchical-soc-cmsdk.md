# 层次化设计与真实 SoC 示例

## 1. 本讲目标

在上一讲（u2-l1）里，我们读懂了一个只有几十行的小设计 `MY_DESIGN`——它有顶层模块、两个子模块、几个 5 位宽的端口。本讲要迈出一大步：**从「教科书式的小模块」走到「真实芯片里的片上系统（SoC）」**。

我们选用的真实样本是 ARM 的 **Cortex-M0 DesignStart（CMSDK）** 示例系统，它的核心 RTL 就放在仓库的 `cmsdk/` 目录里。读完本讲，你应当能够：

1. 理解**参数化 Verilog 模块**（`parameter`）的作用，并区分它与 `define` 宏、`` `ifdef `` 条件编译这三种「可配置」手段。
2. 看懂一个真实 **SoC 顶层**是如何由「CPU 内核 + 总线 + 一堆外设」层层例化拼装出来的。
3. 认识 **AHB-LITE 总线接口**的标准信号（`HADDR`/`HWDATA`/`HREADY`/`HSEL` …），知道 master 和 slave 各自提供什么。
4. 量化体会「简单设计 → 真实 SoC」的**复杂度跃迁**——端口数、子模块数、时钟域数都上了数量级。

> 本讲只读 RTL、讲结构，不涉及综合与时序约束（那是后续 u2-l3 与 U3/U4 的事）。

---

## 2. 前置知识

本讲建立在上一讲 `MY_DESIGN` 的概念之上，假设你已经熟悉：

- **模块（module）与端口（port）声明**、**`reg` 与 `wire`** 的区别。
- **层次化例化**：用 `子模块名 实例名 ( .端口(信号) );` 把一个模块嵌进另一个模块。
- **时序 `always @(posedge clk)`** 与组合 `always @(...)` 的差别。

本讲会新引入几个真实工程里才常见的概念，先做通俗预热：

| 概念 | 一句话解释 |
| --- | --- |
| **SoC（System-on-Chip，片上系统）** | 把 CPU、存储、总线、各种外设（GPIO/UART/定时器…）全部做到同一颗芯片上。 |
| **IP 核（Intellectual Property core）** | 可复用的、预先设计好的电路模块，比如一颗 CPU 内核。通常由厂商以加密/黑盒形式提供，你只看接口、看不到内部 RTL。 |
| **总线（Bus）** | SoC 内部各模块之间传数据的「高速公路」，有一套约定的信号协议。本讲遇到的是 ARM 的 **AHB-LITE**。 |
| **总线 master / slave** | master 是发起读写的一方（如 CPU），slave 是被动响应的一方（如一片内存、一个 GPIO）。 |
| **参数化（parameter）** | 在模块上开「旋钮」，例化时给不同取值，就能得到面积/功能不同的硬件。 |
| **时钟域 / 复位域** | 由不同时钟或不同复位信号驱动的电路区域。真实 SoC 往往有多个时钟和多个复位。 |
| **DFT（Design for Test）** | 为了方便芯片出厂后做测试而预留的端口/逻辑。 |

---

## 3. 本讲源码地图

本讲围绕 `cmsdk/` 目录展开，对照 `MY-Design/` 做对比。涉及的关键文件：

| 文件 | 作用 | 本讲用途 |
| --- | --- | --- |
| [cmsdk/cmsdk_mcu_system_zed.v](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v) | **真实 SoC 的系统顶层**，定义模块 `cmsdk_mcu_system`，例化 CPU、总线译码器、外设。 | 主角：参数化、顶层结构、AHB 接口都在这里。 |
| [cmsdk/cmsdk_mcu_defs.v](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_defs.v) | 用 `` `define `` 宏定义的**全局编译期选项**（调试协议、存储器等待周期）。 | 与 `parameter` 对比，讲「另一种可配置手段」。 |
| [MY-Design/MY_DESIGN.v](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v) | 上一讲的小设计，**无参数、端口少、只有算术/逻辑**。 | 复杂度跃迁的「低复杂度」参照物。 |

此外会点到几个佐证用的周边文件：测试台 [cmsdk/tb_cmsdk_mcu_zed.v](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/tb_cmsdk_mcu_zed.v)、引脚复用 [cmsdk/cmsdk_mcu_pin_mux.v](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_pin_mux.v)、LED 闪烁 [cmsdk/led_1second.v](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/led_1second.v)。

> ⚠️ **先记一个真实工程的「坑」**：文件名 `cmsdk_mcu_system_zed.v` 里定义的模块叫 `cmsdk_mcu_system`，而测试台 `tb_cmsdk_mcu_zed.v` 例化的是另一个名字 `cmsdk_mcu_zed`（见 [tb_cmsdk_mcu_zed.v:77-102](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/tb_cmsdk_mcu_zed.v#L77-L102)）。**文件名 ≠ 模块名**，而且 `cmsdk_mcu_zed` 这个顶层包装模块并不在本仓库里——它是更外层的 FPGA/封装 wrapper。真实 SoC 就是这么一层套一层：`testbench → 顶层 wrapper → 系统核心(cmsdk_mcu_system) → CPU+外设`。读代码时永远以 `module` 关键字后的名字为准。

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：**4.1 参数化模块**、**4.2 SoC 顶层结构**、**4.3 AHB 总线接口信号**、**4.4 复杂度跃迁对比**。

### 4.1 参数化模块：从「硬编码」到「可配置」

#### 4.1.1 概念说明

`MY_DESIGN` 的一切都是写死的：端口宽度固定 `[4:0]`、功能固定（加减与逻辑运算）。这种「一个模块只能做一件事」的写法在教学里没问题，但在真实工程里会带来浪费——你想做一个 8 位版本，就得复制粘贴改一遍。

**参数化（parameterization）** 的思路是：把「会变的量」抽成模块的**参数**，例化时再填具体数值。同一个模块源码，配上不同参数，就能综合出不同大小/功能的电路。这在 SoC 里极其常见：CPU 要不要调试单元、支持几个中断、用几个断点比较器——都用参数开关。

Verilog 里有**三种**「可配置」手段，初学者很容易混淆，先记清：

| 手段 | 关键字 | 作用范围 | 改变时机 | 能否每个实例不同 |
| --- | --- | --- | --- | --- |
| **参数** | `parameter` | 单个模块实例 | 例化时（`#(.X(值))`） | ✅ 可以，每个实例独立 |
| **宏定义** | `` `define `` | 整个编译单元（全局） | 编译前 | ❌ 全局统一 |
| **条件编译** | `` `ifdef … `endif `` | 整个编译单元（全局） | 编译前 | ❌ 有/无整段代码 |

关键差别：**`parameter` 是「每实例一个值」的旋钮；`` `define ``/`` `ifdef `` 是「全局一刀切」的开关**。下面分别看真实代码。

#### 4.1.2 核心流程

参数化模块的典型使用链路：

1. **定义**：在模块名后用 `#( parameter X = 默认值, … )` 列出所有参数。
2. **内部使用**：在模块体内，把参数当普通常数用（例如决定位宽 `reg [WIDTH-1:0]`、决定循环次数、决定某个子模块开/关）。
3. **例化覆盖**：上层用 `模块名 #(.X(新值)) 实例名 (…);` 改写默认值；不写则用默认值。
4. **综合**：综合器把每个实例的参数代入，展开成具体的硬件。

而 `` `define ``/`` `ifdef `` 的链路是：在某个 `.v` 里 `` `define X `` 或在命令行加 `+define+X` → 凡是 `` `include `` 了定义文件、或编译进同一单元的代码，都看到这个宏 → `` `ifdef X `` 包起来的代码段被编译进去（或被剔除）。

#### 4.1.3 源码精读

**(a) `MY_DESIGN` 完全没有参数**——对比的基准。看它的模块头与端口全是写死的字面量：

[MY-Design/MY_DESIGN.v:2-6](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v#L2-L6) —— 模块头没有任何 `#(parameter …)`，端口位宽直接写 `[4:0]`。想换个位宽就得改源码。

**(b) `cmsdk_mcu_system` 是典型的参数化模块**——模块名后紧跟一大串 `parameter`：

[cmsdk/cmsdk_mcu_system_zed.v:39-55](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L39-L55) —— 定义了 `BE`（大小端）、`BKPT`（断点比较器数量）、`DBG`（调试配置）、`NUMIRQ`（中断数，默认 32）、`SMUL`（乘法器配置）、`SYST`（SysTick 定时器）、`WIC`（唤醒中断控制器）、`WPT`（数据观察点比较器）等 CPU 特性参数，还有 `BASEADDR_GPIO0/GPIO1/SYSROMTABLE` 等地址基址参数。每个都带默认值，例如 `parameter BE = 0`。

这正是「同一份 RTL，配出不同档次的 CPU」的实现方式。注意这些参数会层层向下传递——例如地址基址 `BASEADDR_GPIO0` 后来原样传给了地址译码器（见 4.3 节）。

**(c) `define` 宏：另一种全局开关**。`cmsdk_mcu_defs.v` 用宏定义编译期选项：

[cmsdk/cmsdk_mcu_defs.v:38-59](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_defs.v#L38-L59) —— 注释里的 `` `define ARM_CMSDK_INCLUDE_JTAG ``（默认被注释掉，即默认用 SWD 而非 JTAG 调试），以及 `ARM_CMSDK_BOOT_MEM_WS_N`、`ARM_CMSDK_RAM_MEM_WS_S` 等存储器「等待周期（wait state）」宏。

为什么要用宏而不是参数？因为注释明说：「These options … cannot be controlled purely by parameters due to impact on I/O ports」——**这些选项会改变模块的对外端口数量**（比如开了 JTAG 会多出几个调试端口），而 `parameter` 无法改变端口列表的形状，所以只能用全局宏 + 条件编译。

**(d) `ifdef` 条件编译：按需裁剪外设**。回到 SoC 顶层，多处用 `` `ifdef ARM_DESIGNSTART_FPGA `` 把「只在 FPGA 版本里才有的外设」包起来：

[cmsdk/cmsdk_mcu_system_zed.v:69-71](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L69-L71) —— 仅当定义了 `ARM_DESIGNSTART_FPGA` 时，才会出现 `zbt_boot_ctrl`（从 ZBT SRAM 启动）这个端口。ASIC 版本编译时这一段被整段剔除，端口就不存在。

#### 4.1.4 代码实践

**实践目标**：亲手感受「参数 → 改变硬件」。

**操作步骤**（源码阅读 + 局部改写，不修改原文件）：

1. 打开 [cmsdk/cmsdk_mcu_system_zed.v:39-55](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L39-L55)，把所有 `parameter` 抄成一张表，标注：名字、默认值、你认为它控制的硬件特性。
2. 在你自己的草稿文件里，仿照这个写法，定义一个参数化的小模块，例如一个位宽可配的寄存器堆：

```verilog
// 示例代码（非项目原有，仅供练习）
module my_regfile #(parameter WIDTH=8, parameter DEPTH=16) (
    input  wire             clk,
    input  wire [$clog2(DEPTH)-1:0] addr,
    input  wire [WIDTH-1:0] din,
    input  wire             we,
    output wire [WIDTH-1:0] dout
);
    reg [WIDTH-1:0] mem [0:DEPTH-1];
    always @(posedge clk) if (we) mem[addr] <= din;
    assign dout = mem[addr];
endmodule
```

3. 再分别用 `#(.WIDTH(32), .DEPTH(64))` 和不填参数两种方式例化它，体会「同一份源码、两份不同硬件」。

**需要观察的现象**：参数出现在端口位宽表达式 `[WIDTH-1:0]` 和数组规模 `[0:DEPTH-1]` 里；改变例化参数，位宽与深度随之改变。

**预期结果**：你能说出「把 `WIDTH` 从 8 改成 32，综合后的寄存器位数变成 4 倍」。

> 待本地验证：若你手头有仿真器（如 iverilog），可对练习模块跑一个写—读测试，确认参数生效。

#### 4.1.5 小练习与答案

**练习 1**：`MY_DESIGN` 想支持可配位宽，最少要改哪几处？
**答案**：在模块头加 `#(parameter WIDTH=5)`，把所有 `[4:0]` 改成 `[WIDTH-1:0]`，并把子模块 `ARITH`/`COMBO` 也同步参数化（或顶层用 `#(.WIDTH(WIDTH))` 传下去）。

**练习 2**：为什么 `cmsdk_mcu_defs.v` 里「是否包含 JTAG」用 `` `define `` 而不用 `parameter`？
**答案**：因为该选项会**增删 I/O 端口**，而 `parameter` 只能改数值、不能改变端口列表的形状，所以必须用全局宏配合 `` `ifdef ``。

**练习 3**：`` `define `` 与 `parameter` 哪个能让「同一个顶层里两个实例取不同值」？
**答案**：只有 `parameter`。`` `define `` 是全局的，整个编译单元只有一个值。

---

### 4.2 SoC 顶层结构：CPU + 总线 + 外设的层次化集成

#### 4.2.1 概念说明

`MY_DESIGN` 的层次只有两层：顶层 `MY_DESIGN` 例化 `ARITH`/`COMBO`，`COMBO` 又例化了一个 `ARITH`。真实 SoC 的层次要丰富得多，但**拼装套路是相通的**——都是「顶层把一堆子模块用线连起来」。

一个典型微控制器 SoC 的顶层，通常包含这几类东西：

- **CPU 内核**：执行指令的大脑（这里是 Cortex-M0，以 IP 核 `CORTEXM0INTEGRATION` 形式提供）。
- **总线互连**：把 CPU（master）的数据通路接到各个存储/外设（slave），包括**地址译码器**（根据地址选中某个 slave）和**slave 多路选择器**（把被选中 slave 的读数据回送给 CPU）。
- **存储器接口**：Flash、SRAM、Boot ROM 的选中与数据线。
- **外设（peripheral）**：GPIO、UART、定时器、系统控制器……
- **胶水逻辑（glue logic）**：中断汇总、时钟分频、状态输出等 `assign` 连线。

> 关键认知：本仓库**只放出了顶层集成 RTL**（`cmsdk_mcu_system_zed.v`）。CPU 内核 `CORTEXM0INTEGRATION` 以及 `cmsdk_mcu_addr_decode`、`cmsdk_ahb_slave_mux`、`cmsdk_ahb_gpio`、`cmsdk_apb_subsystem` 等子模块**在本目录里都只有「例化」、没有「定义」**——它们来自 ARM CMSDK 的 IP 库，是黑盒。这在真实项目里是常态：你拿到的是「集成层」，IP 内部你看不到。

#### 4.2.2 核心流程

CPU 发起一次访存的典型数据流（自顶向下看 `cmsdk_mcu_system` 内部）：

1. **CPU 输出请求**：`CORTEXM0INTEGRATION` 给出 `cm0_haddr`（地址）、`cm0_htrans`、`cm0_hwrite`、`cm0_hwdata` 等总线信号。
2. **直连到系统总线**：因为没有 DMA，顶层用一串 `assign` 把 `cm0_*` 直接接到 `cm_*`、再接到 `sys_*`（系统总线）。
3. **地址译码**：`cmsdk_mcu_addr_decode` 根据 `sys_haddr` 产出各 slave 的片选 `xxx_hsel`（flash / sram / boot / apbsys / gpio0 / gpio1 / sysctrl / sysrom / defslv）。
4. **slave 响应**：被选中的外设回送 `xxx_hreadyout`、`xxx_hrdata`、`xxx_hresp`。
5. **多路选择回送**：`cmsdk_ahb_slave_mux` 把被选中 slave 的读数据/响应汇总成 `sys_hrdata`/`sys_hreadyout`/`sys_hresp`，再经 `assign` 回送到 CPU 的 `cm0_hrdata`。
6. **中断/状态汇总**：各外设的中断经 `assign` 拼成 `intisr_cm0`，回送给 CPU。

#### 4.2.3 源码精读

**(a) 端口规模先看一眼**：`cmsdk_mcu_system` 的端口列表极长，覆盖时钟/复位、AHB、调试、UART、GPIO、定时器等。

[cmsdk/cmsdk_mcu_system_zed.v:57-67](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L57-L67) —— 仅时钟与复位就有 `FCLK/HCLK/DCLK/SCLK/PCLK/PCLKG` 六个时钟和 `HRESETn/PORESETn/DBGRESETn/PRESETn` 四个复位。这和 `MY_DESIGN` 只有一个 `clk` 形成鲜明对比（见 4.4 节）。

**(b) CPU 内核例化**——整个 SoC 的核心：

[cmsdk/cmsdk_mcu_system_zed.v:300-373](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L300-L373) —— 例化 `CORTEXM0INTEGRATION u_cortexm0integration`，把时钟复位、AHB master 端口、调试端口（SWD/JTAG）、电源管理信号逐一连上。注意若干端口被**常量驱动**：`.NMI(1'b0)`、`.IRQ(32'h00000000)`（中断实际在下面用 `assign intisr_cm0` 汇总，这里先示意性接 0，真实中断连接见 (e)）、`.SE(1'b1)`（扫描使能，DFT 用）。这就是「IP 核只露接口，你按手册连线」。

**(c) 总线互连：地址译码 + slave 多路选择 + 默认 slave**：

[cmsdk/cmsdk_mcu_system_zed.v:427-452](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L427-L452) —— `cmsdk_mcu_addr_decode u_addr_decode`：输入 `sys_haddr`，输出一组片选 `boot_hsel/flash_hsel/sram_hsel/apbsys_hsel/gpio0_hsel/gpio1_hsel/sysctrl_hsel/sysrom_hsel/defslv_hsel`。地址基址参数 `BASEADDR_GPIO0/GPIO1/SYSROMTABLE` 在这里被传递使用。

[cmsdk/cmsdk_mcu_system_zed.v:456-517](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L456-L517) —— `cmsdk_ahb_slave_mux u_ahb_slave_mux_sys_bus`：10 个 slave 端口（`PORT0..PORT9`，其中 2/9 被禁用），每个端口的 `HSELx/HREADYOUTx/HRESPx/HRDATAx` 汇总成总的 `sys_hreadyout/sys_hresp/sys_hrdata`。这是 SoC 里典型的「多 slave 读数据选通」结构。

[cmsdk/cmsdk_mcu_system_zed.v:520-530](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L520-L530) —— `cmsdk_ahb_default_slave u_ahb_default_slave_1`：当地址不匹配任何外设时，由「默认 slave」应答，保证总线不会悬空（`defslv_hrdata = 32'h00000000`）。

**(d) 外设例化**：GPIO 与 APB 子系统（UART/定时器等）。

[cmsdk/cmsdk_mcu_system_zed.v:604-635](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L604-L635) —— `cmsdk_ahb_gpio u_ahb_gpio_0`：挂 AHB 的 GPIO，参数 `ALTERNATE_FUNC_MASK` 控制引脚复用，对外的 `p0_in/p0_out/p0_outen/p0_altfunc` 连到芯片物理引脚。

[cmsdk/cmsdk_mcu_system_zed.v:675-769](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L675-L769) —— `cmsdk_apb_subsystem u_apb_subsystem`：用一串 `INCLUDE_APB_*` 参数开关决定要不要包含 UART0/1/2、定时器、看门狗等。这正是 4.1 节「参数化裁剪外设」的实战场景。注意它跨的是 **APB 总线**（通过 AHB→APB 桥接入），与 GPIO 所在的 AHB 不同——这就是 SoC 的「多总线层次」。

**(e) 胶水逻辑：中断汇总**：

[cmsdk/cmsdk_mcu_system_zed.v:785-795](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L785-L795) —— 用 `assign` 把 `apbsubsys_interrupt`、`gpio0_combintr`、`spi_interrupt` 等按位拼进 CPU 的 32 位中断向量 `intisr_cm0`，看门狗中断则接到 NMI（`intnmi_cm0`）。这种「散落各处的中断 → 拼成一张向量表」是 SoC 顶层最典型的 glue logic。

#### 4.2.4 代码实践

**实践目标**：把 `cmsdk_mcu_system` 的例化清单整理成一张「SoC 结构表」，建立全局画面。

**操作步骤**：

1. 在 [cmsdk/cmsdk_mcu_system_zed.v](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v) 里检索所有形如 `模块名 实例名 (` 的例化点（用编辑器搜 `u_` 前缀的实例名很高效）。
2. 列一张表，每行写：**子模块名 / 实例名 / 它属于哪一类（CPU / 总线互连 / 存储 / 外设 / glue）**。预期至少能列出 8~10 个：`CORTEXM0INTEGRATION`、`cmsdk_mcu_addr_decode`、`cmsdk_ahb_slave_mux`、`cmsdk_ahb_default_slave`、`cmsdk_ahb_cs_rom_table`、`cmsdk_mcu_sysctrl`、`cmsdk_ahb_gpio`(×2)、`cmsdk_apb_subsystem`、`cmsdk_mcu_stclkctrl`。
3. 画一张方框图：CPU 在中间偏左，地址译码器+多路选择器在它右侧，右侧再分出各外设分支。

**需要观察的现象**：CPU 端口名是 `cm0_*`，系统总线端口名是 `sys_*`，二者之间靠一长串 `assign` 直连（见 [L395-L424](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L395-L424)）。

**预期结果**：你能指着方框图说清「CPU 发地址 → 译码器选中外设 → 外设回数据 → 多路选择器回送 CPU」这条主路径。

#### 4.2.5 小练习与答案

**练习 1**：本仓库里能看到 `CORTEXM0INTEGRATION` 的内部 RTL 吗？为什么？
**答案**：看不到。它是 ARM 提供的 IP 核，本目录只有例化、没有定义，属于黑盒；真实 SoC 常以这种形式集成第三方 CPU。

**练习 2**：`cmsdk_ahb_default_slave` 的作用是什么？
**答案**：当地址没命中任何外设时由它应答，给出 `HREADYOUT`/`HRESP` 并把读数据置 0，避免总线悬空导致死锁或 X 态传播。

**练习 3**：`cmsdk_apb_subsystem` 用什么机制决定包含哪些 UART/定时器？
**答案**：用一组 `INCLUDE_APB_*` **参数**开关（见 [L682-L689](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L682-L689)），值为 1 则包含、为 0 则裁掉，综合出面积不同的外设子系统。

---

### 4.3 AHB 总线接口信号：master / slave 与地址译码

#### 4.3.1 概念说明

**AHB-LITE** 是 ARM 定义的一种片上总线协议（Lite 表示「单 master」简化版）。它的核心思想：用一组**标准命名的信号**，让任何 master 和任何 slave 都能即插即用。你只要认得这套信号名，就能读懂绝大多数 ARM SoC 的总线连接。

AHB-LITE 最常见的信号（H 开头 = AHB）：

| 信号 | 方向（master 视角） | 含义 |
| --- | --- | --- |
| `HADDR[31:0]` | master → slave | 32 位地址 |
| `HTRANS[1:0]` | master → slave | 传输类型（IDLE/BUSY/NONSEQ/SEQ） |
| `HSIZE[2:0]` | master → slave | 传输字节宽（8/16/32…） |
| `HBURST[2:0]` | master → slave | 突发类型（单次/4 拍/8 拍…） |
| `HPROT[3:0]` | master → slave | 保护控制（缓存性/特权/数据或指令） |
| `HWRITE` | master → slave | 1=写，0=读 |
| `HWDATA[31:0]` | master → slave | 写数据 |
| `HSEL` | 译码器 → slave | 该 slave 被选中 |
| `HRDATA[31:0]` | slave → master | 读数据 |
| `HREADYOUT` / `HREADY` | slave → master | 传输完成（高有效） |
| `HRESP` | slave → master | 响应（OKAY/ERROR/RETRY/SPLIT） |

> **直觉**：master 把「地址 + 类型 + 写数据」摆上台面 → 译码器根据地址拉高某个 `HSEL` → 被选中的 slave 在下一拍给出「读数据 + 完成 + 响应」→ 多路选择器把它的回答汇总回 master。

#### 4.3.2 核心流程

地址译码的原理本质上是**地址范围匹配**。每个 slave 分到一段地址区间，用「基址 + 长度」描述。译码器把 `HADDR` 落在哪个区间，就拉高对应的 `HSEL`。若 slave 的窗口大小是 \(S\) 字节、基址为 \(B\)，则被选中的条件可写成：

\[
B \le \text{HADDR} < B + S
\]

工程上常以基址的高位做比较（因为窗口都是 2 的幂次对齐）。例如 GPIO0 基址参数 `BASEADDR_GPIO0 = 32'h4001_0000`，意味着 16 位以下为片内偏移、高位是基地址标签。注意 [cmsdk/cmsdk_mcu_system_zed.v:584](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L584) 里 GPIO 的 `HADDR` 只接了低 12 位 `sys_haddr[11:0]`——说明每个外设只关心自己窗口内的低位偏移地址。

#### 4.3.3 源码精读

**(a) 顶层对外的 AHB 存储端口**——CPU 经总线去访问外部存储块：

[cmsdk/cmsdk_mcu_system_zed.v:72-93](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L72-L93) —— `HADDR/HTRANS/HSIZE/HWRITE/HWDATA/HREADY`（输出到存储块）以及每个存储设备的 `xxx_hsel`（选中）、`xxx_hreadyout`/`xxx_hrdata`/`xxx_hresp`（从存储块返回）。这就是完整的 AHB master↔slave 信号组。

**(b) CPU 侧的 AHB master 信号（内部）**：

[cmsdk/cmsdk_mcu_system_zed.v:150-163](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L150-L163) —— 内部连线 `cm0_haddr/cm0_htrans/cm0_hsize/cm0_hburst/cm0_hprot/cm0_hwrite/cm0_hwdata/cm0_hrdata/cm0_hready/cm0_hresp`，正是上一节表格里那套信号，只不过加了 `cm0_` 前缀表示「来自 Cortex-M0」。

**(c) CPU 与系统总线之间的 assign 直连**——因为只有一个 master，无需多 master 仲裁：

[cmsdk/cmsdk_mcu_system_zed.v:411-424](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L411-L424) —— 代码注释写得很直白：「No DMA controller - no need to have master multiplexer / direct connection from cpu to system bus」。`cm_*` 信号直接赋给 `sys_*`，反向的读数据/响应也直接回连。理解了这段，你就理解了「单 master AHB-LITE 的互连其实可以简单到全是 assign」。

**(d) 地址译码器与多路选择器**（已在 4.2 节引用）正是 AHB 协议落地的两件套：译码器发 `HSEL`、多路选择器收 `HRDATA/HREADYOUT/HRESP`。

#### 4.3.4 代码实践

**实践目标**：在源码里追一次完整的「CPU 写一个 GPIO 寄存器」的信号路径。

**操作步骤**：

1. 找到 CPU 输出 `cm0_haddr/cm0_hwrite/cm0_hwdata` 的位置（[L315-L322](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L315-L322)）。
2. 顺着 [L411-L419](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L411-L419) 的 `assign`，确认它们变成 `sys_haddr/sys_hwrite/sys_hwdata`。
3. 进入译码器 [L427-L452](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L427-L452)，确认当 `sys_haddr` 落在 `BASEADDR_GPIO0` 区间时会拉高 `gpio0_hsel`。
4. 进入 GPIO 实例 [L604-L635](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L604-L635)，确认 `gpio0_hsel/HWRITE/HWDATA` 被它接收，并回送 `gpio0_hreadyout/gpio0_hrdata`。
5. 最后看多路选择器 [L493-L496](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L493-L496)，确认 GPIO 的应答汇入 `sys_hrdata/sys_hreadyout`，再回 CPU。

**需要观察的现象**：整条路径上信号只是被改名、选通，没有任何「指令执行」逻辑——总线只负责把数据搬到正确的端口。

**预期结果**：你能画出 `cm0_* → sys_* → 译码 → gpio0 → 多路选 → 回 sys_* → cm0_*` 的闭环。

#### 4.3.5 小练习与答案

**练习 1**：为什么 GPIO 实例的 `HADDR` 只接 `sys_haddr[11:0]`？
**答案**：因为高位已经由地址译码器用来选中这个外设（命中 `gpio0_hsel`），外设内部只需关心自己窗口内的低位偏移（4KB 空间）。

**练习 2**：`HREADY` 与 `HREADYOUT` 有何区别？
**答案**：`HREADYOUT` 是单个 slave 自己给出的「我完成了」；`HREADY` 是经多路选择器汇总后、整条总线的「当前传输完成」信号，送给 master 和所有 slave。

**练习 3**：为什么本设计里 master 到系统总线全是 `assign` 直连，没有仲裁器？
**答案**：因为这是 AHB-**LITE**（单 master），且没有 DMA 等第二 master，无需仲裁；多 master 的完整 AHB 才需要仲裁器和 master 多路选择器。

---

### 4.4 复杂度跃迁对比：MY_DESIGN vs cmsdk_mcu_system

#### 4.4.1 概念说明

读懂结构之后，我们退一步，用数字量化「简单设计」与「真实 SoC」之间的鸿沟。这不是为了吓退你，而是让你建立一个**合理的预期**：真实工程的代码量、端口数、子模块数都是教学例子的几十到几百倍，读它的策略也要随之调整——不能再逐行读，而要「先抓顶层例化、再按模块分块」。

#### 4.4.2 核心流程

复杂度可以从几个维度量化。设一个模块的端口数为 \(P\)、子模块例化数为 \(I\)、源码行数为 \(L\)，则从 `MY_DESIGN` 到 `cmsdk_mcu_system` 的跃迁可粗略表示为各维度的比值：

\[
R_P = \frac{P_{\text{cmsdk}}}{P_{\text{MYD}}}, \quad
R_I = \frac{I_{\text{cmsdk}}}{I_{\text{MYD}}}, \quad
R_L = \frac{L_{\text{cmsdk}}}{L_{\text{MYD}}}
\]

不必精确，量级感即可。下面用真实数字填。

#### 4.4.3 源码精读

**(a) 端口数对比**：

- [MY-Design/MY_DESIGN.v:2-6](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v#L2-L6) —— `MY_DESIGN` 顶层共 **10 个端口**（`Cin1/Cin2/Cout/data1/data2/sel/clk/out1/out2/out3`），且位宽统一是 5 位。
- [cmsdk/cmsdk_mcu_system_zed.v:57-143](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L57-L143) —— `cmsdk_mcu_system` 顶层有**几十个端口声明**（仅时钟/复位就 10 个，加上 AHB、调试、3 路 UART、2 组 16 位 GPIO、定时器、各类状态输出，单文件内 `input/output` 声明即达数十处）。

> 把端口按功能归类，至少能分出**三类以上接口**，这正是本讲的综合实践任务。

**(b) 子模块例化数对比**：

- `MY_DESIGN` 顶层只例化 **2 个**子模块（`ARITH`、`COMBO`），`COMBO` 再例化 1 个 `ARITH`，总共 3 处例化、2 种模块。
- `cmsdk_mcu_system` 顶层例化了 **8~10 个**不同子模块（见 4.2 节清单），且其中 CPU `CORTEXM0INTEGRATION` 本身就是一颗含数万门的处理器。

**(c) 时钟域与复位域对比**：

- `MY_DESIGN`：**1 个时钟 `clk`**、**无显式复位**。
- `cmsdk_mcu_system`：**6 个时钟**（`FCLK/HCLK/DCLK/SCLK/PCLK/PCLKG`）+ **4 个复位**（`HRESETn/PORESETn/DBGRESETn/PRESETn`），见 [L57-L67](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L57-L67)。多时钟域意味着后续要做**跨时钟域同步**与多组时序约束——这是 U2-l3（SDC）要解决的真问题。

**(d) 配置手段对比**：

- `MY_DESIGN`：**零参数、零宏**，完全硬编码。
- `cmsdk_mcu_system`：**12 个 `parameter`**（[L39-L54](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L39-L54)）+ `` `ifdef ARM_DESIGNSTART_FPGA `` 条件编译 + `cmsdk_mcu_defs.v` 里的 `` `define `` 宏，三套可配置机制并用。

把上面四项整理成一张总表：

| 维度 | `MY_DESIGN` | `cmsdk_mcu_system` | 跃迁倍数（量级） |
| --- | --- | --- | --- |
| 顶层端口数 | ~10 | 数十个 | 数倍 |
| 子模块例化数 | 2 | 8~10 | ~4–5× |
| 时钟域 / 复位域 | 1 / 0 | 6 / 4 | 数倍 |
| 配置手段 | 无 | parameter + define + ifdef | 质变 |
| 是否含第三方 IP 黑盒 | 否 | 是（CPU 内核） | 质变 |
| 对外总线协议 | 无 | AHB-LITE + APB | 质变 |

#### 4.4.4 代码实践

**实践目标**：亲手算出复杂度跃迁的量级，建立「读真实 SoC 要换策略」的直觉。

**操作步骤**：

1. 用编辑器统计行数：`wc -l MY-Design/MY_DESIGN.v cmsdk/cmsdk_mcu_system_zed.v`，计算行数比 \(R_L\)。
2. 数端口：在两个文件里分别数 `input`/`output`（顶层端口）的个数。
3. 数例化：分别数形如 `模块名 实例名 (` 的例化点数量。
4. 把三类接口归类：在 `cmsdk_mcu_system` 的端口里，至少挑出**时钟类、复位类、AHB 总线类**各一组信号（可参考 [L57-L67](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L57-L67) 与 [L72-L93](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L72-L93)）。

**需要观察的现象**：`MY_DESIGN.v` 一屏就能看完；`cmsdk_mcu_system_zed.v` 需要折叠/跳转才能驾驭。

**预期结果**：你会得到一张类似上表的量化对比，并得出结论——读真实 SoC 必须「**先读顶层例化与端口、再按子模块分块深入**」，而不是逐行通读。

> 待本地验证：行数与端口数以你本机 `wc -l` 与实际计数为准；上表数字供量级参考。

#### 4.4.5 小练习与答案

**练习 1**：举出两个「质变」维度（不是简单变多，而是 `MY_DESIGN` 根本没有的东西）。
**答案**：① 引入了标准总线协议（AHB-LITE/APB）；② 集成了第三方 IP 黑盒（CPU 内核 `CORTEXM0INTEGRATION`）。

**练习 2**：为什么真实 SoC 需要多个时钟域？
**答案**：不同模块跑在不同速度（CPU 高速总线 HCLK、慢速外设 APB 用 PCLK、调试用 DCLK…），用多个时钟可在不影响性能的前提下给慢速部分省功耗；代价是需要处理跨时钟域与多组时序约束。

**练习 3**：面对行数几十倍的真实 SoC RTL，第一步该读什么？
**答案**：先读 `module` 头（端口 + 参数）建立外部视图，再读顶层所有例化点画出结构图，最后才按需深入某个子模块——而不是从第 1 行逐行读到末尾。

---

## 5. 综合实践

**任务**：对比 `MY_DESIGN` 与 `cmsdk_mcu_system` 的端口规模，并给真实 SoC 的接口分类。

请完成下面这份「SoC 接口清单」小报告（纯阅读 + 整理，不改源码）：

1. **端口计数**：分别列出 `MY_DESIGN`（[MY-Design/MY_DESIGN.v:2-6](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v#L2-L6)）与 `cmsdk_mcu_system`（[cmsdk/cmsdk_mcu_system_zed.v:57-143](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L57-L143)）的顶层端口数量。
2. **三类接口归类**（至少完成以下三类，可再加）：
   - **时钟接口**：列出 `cmsdk_mcu_system` 的所有时钟端口（`FCLK/HCLK/DCLK/SCLK/PCLK/PCLKG`），各写一句推测用途。
   - **复位接口**：列出 `HRESETn/PORESETn/DBGRESETn/PRESETn`，指出哪些是「上电复位」、哪些是「子系统复位」。
   - **总线接口**：列出 AHB master 侧的 `HADDR/HTRANS/HSIZE/HWRITE/HWDATA/HREADY` 与各 slave 的 `xxx_hsel/xxx_hreadyout/xxx_hrdata/xxx_hresp`，说明 master/slave 各自的方向。
3. **结构图**：基于 4.2 节的例化清单，画一张 `cmsdk_mcu_system` 的方框图（CPU → 译码器/多路选择器 → 各外设），标注 AHB 与 APB 两条总线。
4. **一句话结论**：用复杂度跃迁的量级，说明读真实 SoC 时为什么要换策略。

**预期产出**：一份不到一页的 Markdown/手写笔记，包含两张表（端口计数、三类接口）和一张方框图。完成后，你应该能在不看源码的情况下，复述「CPU 发起访存 → 译码选中外设 → 数据回送」的完整路径，并能解释 `parameter`/`` `define ``/`` `ifdef `` 三者的差别。

---

## 6. 本讲小结

- **参数化**是真实工程的基础：`cmsdk_mcu_system` 用 12 个 `parameter` 把 CPU 配成不同档次；而 `` `define ``/`` `ifdef `` 用于「会改变端口形状」的全局开关——二者不能互相替代。
- 一个真实 **SoC 顶层** = CPU 内核（黑盒 IP）+ 总线互连（地址译码 + slave 多路选择 + 默认 slave）+ 一堆外设（GPIO/UART/定时器…）+ 中断/时钟等 glue logic，全靠例化与 `assign` 拼装。
- **AHB-LITE** 用一套 `H` 前缀的标准信号（`HADDR/HWDATA/HREADY/HSEL/HRDATA/HRESP`…）让 master 与 slave 即插即用；单 master 时互连可简化为纯 `assign` 直连。
- 从 `MY_DESIGN` 到 `cmsdk_mcu_system`，端口数、例化数、时钟域数都上了量级，并出现「总线协议」和「第三方 IP 黑盒」两个**质变**——读真实 SoC 要改用「先抓顶层例化、再分块深入」的策略。
- 本仓库只提供 CMSDK 的**集成层** RTL；`CORTEXM0INTEGRATION` 与各 `cmsdk_*` 子模块是 ARM 提供的黑盒，看不到内部——这是真实项目的常态。
- **文件名 ≠ 模块名**：`cmsdk_mcu_system_zed.v` 定义的是 `cmsdk_mcu_system`，而测试台例化的 `cmsdk_mcu_zed` 是另一层 wrapper，不在本仓库内。

---

## 7. 下一步学习建议

- **横向加深 RTL 阅读力**：可以把 `cmsdk/` 里看得见内部 RTL 的小模块当练手对象，例如引脚复用 [cmsdk/cmsdk_mcu_pin_mux.v](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_pin_mux.v)（三态 IO 与功能复用）、LED 闪烁 [cmsdk/led_1second.v](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/led_1second.v)（异步复位 + 多级计数分频），巩固 `always`/`assign`/三态缓冲。
- **进入时序约束**：本讲的 SoC 有 6 个时钟、4 个复位，正是下一讲 **u2-l3（SDC 时序约束）**要解决的问题——学会用 `create_clock`、`set_input_delay` 等约束把多时钟域行为告诉综合与 STA 工具。
- **往物理设计走**：等读完 u2-l3，U3 会讲标准单元库与物理数据（NDM/LEF），U4 会进入 ICC2 物理设计主流程，届时你会看到本讲的 RTL/网表是如何被「摆到芯片上」的。
