# 层次化设计与真实 SoC 示例

## 1. 本讲目标

在上一讲（u2-l1）里，我们读懂了一个只有几十行的教学小设计 `MY_DESIGN`——它有一个顶层模块、两个子模块、几个 5 位宽的端口。本讲要迈出一大步：**从「教科书式的小模块」走到「真实芯片里的片上系统（SoC）」**。

我们选用的真实样本是 ARM 的 **Cortex-M0 DesignStart（CMSDK）** 示例系统，核心 RTL 放在仓库的 `cmsdk/` 目录里。读完本讲，你应当能够：

1. 理解**参数化 Verilog 模块**（`parameter`）的作用，并能区分它与 `` `define `` 宏、`` `ifdef `` 条件编译这三种「可配置」手段。
2. 看懂一个真实 **SoC 顶层**是如何由「CPU 内核 + 总线 + 一堆外设」层层例化拼装出来的。
3. 认识 **AHB-LITE 总线接口**的标准信号（`HADDR`/`HWDATA`/`HREADY`/`HSEL` …），知道 master（主）和 slave（从）各自提供什么。
4. 量化体会「简单设计 → 真实 SoC」的**复杂度跃迁**——端口数、子模块数、时钟域数都上了数量级。

> 本讲只读 RTL、讲结构，不涉及综合与时序约束（那是后续 u2-l3 与 U3/U4 的事）。

---

## 2. 前置知识

本讲假定你已经掌握 u2-l1 的内容：模块（`module`）、端口（`input`/`output`）、`reg` 与 `wire` 的区别、`always` 时序块与组合块、以及用 `.端口(信号)` 的命名端口连接做层次化例化。在此基础上，我们先补三个本讲会用到的术语：

- **SoC（System on Chip，片上系统）**：把一整台「小电脑」——CPU 内核、存储控制器、总线、UART/GPIO/定时器等外设——全部塞进一颗芯片的顶层模块里。`MY_DESIGN` 只是一段「算术逻辑」，而 `cmsdk_mcu_system` 是一台「最小单片机」。
- **总线（Bus）**：芯片内部多个部件之间公用的数据通路。ARM 体系里常见的是 **AHB（Advanced High-performance Bus）**，本讲遇到的是简化版 **AHB-LITE**（单主、单从在同一时刻通信）。
- **参数化（Parameterization）**：在模块声明里用 `parameter` 给某些数值（位宽、地址、中断数）起个「名字 + 默认值」，让同一份 RTL 在例化时被「调成」不同配置，而不必复制多份代码。

> 名词小贴士：本讲文件名里的 `_zed` 指目标板卡 **ZedBoard**（一颗 Xilinx Zynq FPGA 开发板）。这个 CMSDK 例子的 RTL 既能用于真实 ASIC 流程，也能直接烧进 FPGA 跑起来，所以仓库里同时给了 RTL（`cmsdk_mcu_system_zed.v`）和对应的 testbench（`tb_cmsdk_mcu_zed.v`）。

---

## 3. 本讲源码地图

本讲围绕三个真实源码文件展开：

| 文件 | 作用 | 本讲中的角色 |
| --- | --- | --- |
| [cmsdk/cmsdk_mcu_system_zed.v](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v) | ARM Cortex-M0 DesignStart 示例系统的**顶层 RTL**（约 860 行） | 真实 SoC 样本：参数、端口、9 个子模块例化、AHB 总线全在这里 |
| [cmsdk/cmsdk_mcu_defs.v](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_defs.v) | 系统级**配置宏**定义文件 | 用来对比 `` `define `` 宏与 `parameter` 的差异 |
| [MY-Design/MY_DESIGN.v](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v) | 上一讲的教学小设计 | 作为「简单端」的对照组，量化复杂度跃迁 |

> 这三个文件互相独立、各自可读；本讲的每一处结论都会给出对应行号的永久链接，你可以点开逐行核对。

---

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：**参数化模块 → SoC 顶层结构 → AHB 总线接口 → 复杂度跃迁对比**。前三个模块各自解决 SoC RTL 的一个侧面，第四个把它们和 `MY_DESIGN` 放在一起做量化对比。

### 4.1 参数化模块（parameter）

#### 4.1.1 概念说明

在 `MY_DESIGN` 里，所有位宽都是写死的 `[4:0]`，端口名也是固定的一串。如果想让「同一个算术单元」有时做成 5 位、有时做成 8 位，你只能复制一份代码再改——这在真实项目里是不可维护的。

**参数化**就是为了解决这个问题：把那些「可能变化的数值」从代码里抽出来，赋予一个名字和默认值，这就是 `parameter`。它的三个关键性质是：

1. **编译期常量**：`parameter` 的值在综合/仿真前就固定了，不是运行时变量。
2. **每个实例可不同**：同一个模块被例化两次，可以各自传不同的参数值，得到两份「配置不同」的硬件。
3. **向下传递**：顶层模块可以用自己的参数，去覆盖它所例化的子模块的参数。

Verilog-2001 推荐的「ANSI 风格」写法是：

```verilog
module 模块名 #(
  parameter 参数A = 默认值A,
  parameter 参数B = 默认值B
) (
  input  wire ...,
  output wire ...
);
```

例化时用 `#(.参数名(值))` 来覆盖默认值。

#### 4.1.2 核心流程

一条参数从「声明」流到「生效」的路径如下：

```
模块声明里 parameter X = 默认值
        │
        ├──（若不覆盖）→ 综合时 X 取默认值
        │
        └──（若例化时写 #(.X(新值))）→ 综合时 X 取新值
                                        │
                                        └── X 还可以作为「实参」传给下一层子模块
                                            例如 .CHILD_PARAM(X)
```

换句话说：参数像函数参数一样，从顶层「往下」逐层传递，每一层可以选择沿用默认、改写、或转发给更下层。

#### 4.1.3 源码精读

`cmsdk_mcu_system` 的模块头就是一个教科书级的参数化声明。注意它用了 `module ... #(parameter ...) (...)` 的 Verilog-2001 ANSI 风格：

[cmsdk/cmsdk_mcu_system_zed.v:39-55](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L39-L55) —— 这是模块声明与参数列表。每个 `parameter` 都带注释说明含义，例如 `BE`（大/小端）、`BKPT`（断点比较器个数）、`NUMIRQ`（中断数）、`SMUL`（乘法器配置）等。这些就是「同一份 Cortex-M0 内核，可以配出不同规格」的旋钮。

参数真正被「用起来」的地方，是在子模块例化时被当作覆盖值或转发值。看地址译码器的例化：

[cmsdk/cmsdk_mcu_system_zed.v:427-432](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L427-L432) —— 此处 `cmsdk_mcu_addr_decode` 例化时，用顶层的参数 `BASEADDR_GPIO0`、`BASEADDR_GPIO1`、`BASEADDR_SYSROMTABLE` 去覆盖子模块的同名参数，而 `BOOT_LOADER_PRESENT` 则被直接写死成 `1'b0`。这正是「顶层参数向下传递」的实例。

> 完整的端口连接可见：
> [cmsdk/cmsdk_mcu_system_zed.v:427-452](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L427-L452) —— `cmsdk_mcu_addr_decode` 的参数覆盖与命名端口连接。

GPIO 外设的例化也是同理，而且展示了「同一模块例化两次、参数不同」：

[cmsdk/cmsdk_mcu_system_zed.v:604-608](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L604-L608) —— `cmsdk_ahb_gpio` 第一次例化（`u_ahb_gpio_0`）时 `ALTERNATE_FUNC_MASK = 16'h0000`（无引脚复用）；

[cmsdk/cmsdk_mcu_system_zed.v:638-642](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L638-L642) —— 同一个 `cmsdk_ahb_gpio` 第二次例化（`u_ahb_gpio_1`）时 `ALTERNATE_FUNC_MASK = 16'h002A`（带引脚复用）。**一份 RTL、两份不同配置的硬件**，这就是参数化的威力。

**对比：`` `define `` 宏是另一回事。** 看 `cmsdk_mcu_defs.v`：

[cmsdk/cmsdk_mcu_defs.v:48-59](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_defs.v#L48-L59) —— 这里用 `` `define ARM_CMSDK_BOOT_MEM_WS_N 0 `` 之类的宏定义了各类存储器的等待周期（wait state）。宏是**全局文本替换**，一旦 `` `include `` 进来，全工程都生效，而且**同一模块例化两次也无法取不同值**。这与 `parameter`「每实例可不同」截然不同。文件头部还提到：

[cmsdk/cmsdk_mcu_defs.v:33-38](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_defs.v#L33-L38) —— 注释解释调试协议可选 SWD 或 JTAG，并指出「这些选项无法纯靠 parameter 控制，因为会影响 I/O 端口」，于是用注释掉的 `` `define ARM_CMSDK_INCLUDE_JTAG `` 来开关。这是一个**很好的现实案例：什么时候必须用宏、什么时候用参数更合适**。

#### 4.1.4 代码实践

**实践目标**：亲手追踪一个参数从「默认值」到「被覆盖」的完整路径。

**操作步骤**：

1. 打开 [cmsdk/cmsdk_mcu_system_zed.v:39-55](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L39-L55)，记下 `NUMIRQ` 的默认值（应为 `32`）。
2. 假设有别人在更顶层用这样的方式例化了本系统（**示例代码，非仓库原有**）：

   ```verilog
   cmsdk_mcu_system #(.NUMIRQ(16), .BE(1)) u_my_mcu (...);
   ```

3. 回答：在这个实例里，`NUMIRQ` 和 `BE` 分别会取什么值？别处再例化一个不带 `#(...)` 的 `cmsdk_mcu_system`，它的 `NUMIRQ` 又是多少？

**需要观察的现象**：参数值「就近生效」——谁例化时覆盖了，谁就用新值；不覆盖的实例仍取模块头里的默认值。

**预期结果**：该实例 `NUMIRQ=16`、`BE=1`（大端）；另一个不覆盖的实例 `NUMIRQ=32`（默认值）。

**若想真正跑起来（待本地验证）**：用 Icarus Verilog 之类工具编译 `cmsdk_mcu_system_zed.v` + `tb_cmsdk_mcu_zed.v`，把 `NUMIRQ` 改成不同值后重新仿真，观察是否仍能通过编译（注意：本仓库的 CMSDK 依赖若干未随仓库提供的 IP 内核如 `CORTEXM0INTEGRATION`，纯开源仿真器可能因缺核而无法链接，**结果待本地验证**）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `NUMIRQ` 用 `parameter` 而不用 `` `define NUMIRQ 32 ``？

> **参考答案**：因为同一颗 SoC 可能被例化成「32 中断版」和「16 中断版」两份不同硬件；`parameter` 允许每个实例取不同值，而 `` `define `` 是全局替换，全工程只能有一个值，无法区分实例。

**练习 2**：参数 `` `define ARM_CMSDK_INCLUDE_JTAG``（见 cmsdk_mcu_defs.v:38）被注释掉了。如果取消注释，代码会怎样变化？

> **参考答案**：它会触发系统里 `` `ifdef ARM_CMSDK_INCLUDE_JTAG `` 包裹的 JTAG 相关端口与逻辑（条件编译），使调试接口从默认的 SWD 切换为 JTAG。这正说明：**当配置会改变端口拓扑时，往往只能用宏 + 条件编译，而不是参数**。

---

### 4.2 SoC 顶层结构

#### 4.2.1 概念说明

一个 SoC 顶层模块本质上是一个**「接线板」**：它自己几乎不写「计算逻辑」，而是声明一堆内部连线（`wire`），然后把 CPU 内核、总线译码器、存储接口、各种外设**例化**进来，用命名端口连接把它们接到一起。

`cmsdk_mcu_system` 这个顶层里，你能看到一条典型的「单主总线」结构：

- **1 个 master**：Cortex-M0 CPU 内核（`CORTEXM0INTEGRATION`），它是唯一发起读写的人。
- **1 个地址译码器**（`cmsdk_mcu_addr_decode`）：根据 CPU 给出的地址，决定「这次访问」要选中哪个外设/存储。
- **1 个从设备多路选择器**（`cmsdk_ahb_slave_mux`）：多个从设备都能返回读数据，需要用多路选择器把「被选中那一个」的数据送回 CPU。
- **若干 slave**：Flash、SRAM、Boot ROM、系统控制器、2 个 GPIO、APB 子系统（UART/定时器）、ROM Table、默认从设备（default slave，处理非法地址）。

这种「主—译码—多路选—从」的结构是 AMBA 总线的经典骨架，理解了它，你就能套用到绝大多数 ARM-based SoC。

#### 4.2.2 核心流程

一次「CPU 读外设寄存器」在顶层结构里的信号流转（先不看协议细节，只看「经过哪些块」）：

```
                ┌─────────────────────────────────────────┐
  CPU(主) ──地址/控制──►  系统总线 sys_h*（一组 wire）
                │                    │
                │                    ▼
                │          ┌────────────────────┐
                │          │ addr_decode 译码器  │ ──► 各 slave 的 hsel（片选）
                │          └────────────────────┘
                │                    │
                │          ┌────────────────────┐
                │          │ 被选中的 slave      │ ──► hrdata/hreadyout/hresp
                │          │ (GPIO/SRAM/UART…)   │
                │          └────────────────────┘
                │                    │
                │                    ▼
                │          ┌────────────────────┐
                ◄──读数据── │ ahb_slave_mux 多路选│ ◄── 选出被选中 slave 的返回值
                           └────────────────────┘
```

顶层模块要做的，就是把上面每一个方块的端口，用 `wire` 和命名连接 `.端口(信号)` 接到正确的连线上。CPU 与总线之间还插了一层「bit-band / 直连」assign（本例实际是直连），用来套用同一套 `cm_*` 命名规范。

#### 4.2.3 源码精读

**CPU 内核例化**是整个顶层的「心脏」：

[cmsdk/cmsdk_mcu_system_zed.v:299-373](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L299-L373) —— 例化 `CORTEXM0INTEGRATION`（实例名 `u_cortexm0integration`）。注意它的 AHB 主端口信号连到了 `cm0_*` 这组 wire（如 `.HADDR(cm0_haddr)`），时钟/复位连到了 `FCLK/HCLK/SCLK/DCLK/PORESETn/HRESETn` 等，调试口连到了 `SWDITMS/SWCLKTCK/TDO` 等。很多输入被直接接到常量（如 `.IRQ(32'h00000000)`、`.NMI(1'b0)`），表示这个示例系统暂不接外部中断。

CPU 出来的 `cm0_*` 总线先经过一组 `assign` 改名成 `cm_*`/`sys_*`：

[cmsdk/cmsdk_mcu_system_zed.v:395-423](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L395-L423) —— 注释说明「无 DMA，无需主多路选择器，CPU 直连系统总线」。这一段没有逻辑运算，纯粹是连线重命名，是 SoC 顶层最典型的「接线」代码。

**地址译码器**：

[cmsdk/cmsdk_mcu_system_zed.v:427-452](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L427-L452) —— `cmsdk_mcu_addr_decode`（`u_addr_decode`）。输入是系统地址 `sys_haddr`，输出是一堆片选：`flash_hsel/sram_hsel/boot_hsel/apbsys_hsel/gpio0_hsel/gpio1_hsel/sysctrl_hsel/sysrom_hsel/defslv_hsel`。**一个地址，只会有一个 `*_hsel` 为真**——这就是译码。

**从设备多路选择器**：

[cmsdk/cmsdk_mcu_system_zed.v:456-517](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L456-L517) —— `cmsdk_ahb_slave_mux`（`u_ahb_slave_mux_sys_bus`）。它的每个「PORTx」对应一个 slave：把那个 slave 的 `HREADYOUTx/HRESPx/HRDATAx` 接进来，最终输出唯一的 `sys_hreadyout/sys_hresp/sys_hrdata` 送回 CPU。注意 `PORT2_ENABLE(0)`、`PORT9_ENABLE(0)` 表示这两个端口被参数关掉了（boot 与 MTB 在此配置未用）。

**外设例化**：系统控制器、两个 GPIO、APB 子系统（内含 UART/定时器）依次例化：

- [cmsdk/cmsdk_mcu_system_zed.v:571-600](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L571-L600) —— `cmsdk_mcu_sysctrl`（系统控制器，产生复位/电源管理信号）。
- [cmsdk/cmsdk_mcu_system_zed.v:675-690](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L675-L690) —— `cmsdk_apb_subsystem`（APB 子系统），其参数 `INCLUDE_APB_UART0(1)` 而 `INCLUDE_APB_UART1/2/TIMER0/1/...` 多为 `0`，说明本配置只启用了一个 UART。**又是参数化在裁剪外设**。

> 小结：顶层一共例化了 **9 个子模块**（CPU 内核 + addr_decode + slave_mux + default_slave + ROM Table + sysctrl + 2×GPIO + apb_subsystem），它的工作几乎全是「声明 wire + 例化 + 连线」，几乎没有 `always` 逻辑——这与 `MY_DESIGN` 充满 `always` 的风格形成鲜明对比。

#### 4.2.4 代码实践

**实践目标**：把顶层 RTL 抽象成一张「实例清单 + 连线图」。

**操作步骤**：

1. 在 `cmsdk_mcu_system_zed.v` 中用编辑器搜索形如「模块名 实例名 (」的例化语句，列出全部「模块名 → 实例名」对照表（提示：本节已给出 9 个，逐一核对行号）。
2. 把 `CORTEXM0INTEGRATION`（主）画在最左，`cmsdk_ahb_slave_mux`（多路选）画在最右，中间把译码器、各 slave 排成一列，用箭头标出 `sys_haddr`、各 `*_hsel`、`sys_hrdata` 的走向。
3. 特别标注：哪些 slave 的返回数据会汇入 `slave_mux`，哪些信号是顶层直接对外输出（如 `HADDR`、`uart0_txd`、`p0_out`）。

**需要观察的现象**：顶层像一个「漏斗」——CPU 一侧只有一组总线，对外一侧却分叉出 Flash/SRAM/UART/GPIO 等多套接口。

**预期结果**：得到一张「1 主 → 译码 → N 从 → 多路选回主」的对称结构图，且对外端口大致按「时钟/复位/AHB/调试/UART/GPIO」分簇排列。

#### 4.2.5 小练习与答案

**练习 1**：为什么需要 `cmsdk_ahb_default_slave`（默认从设备）？

> **参考答案**：当 CPU 访问一个**没有任何外设对应的非法地址**时，`defslv_hsel` 会被译码器选中，默认从设备给出一个合规的错误响应（`HRESP` 表示错误），避免总线「悬空」无响应而死锁。它是总线的「兜底」。

**练习 2**：顶层模块里有大量 `assign cm_haddr = cm0_haddr;` 这种「改名」语句，为什么不直接用一组名字？

> **参考答案**：因为 Cortex-M0 内核的端口名（`cm0_*`）和系统总线规范名（`sys_*`）来自两套命名约定。中间留一组 `cm_*`/`sys_*` 连线并改名，是为了在「插不插 bit-band 包装、有没有 DMA 主多路选」等不同配置下都能灵活接线，是工程上常见的「解耦命名层」做法。

---

### 4.3 AHB 总线接口信号

#### 4.3.1 概念说明

**AHB-LITE** 是 ARM AMBA 总线家族里最常用的一种「高性能单主总线」。它规定了一组以 `H` 开头的标准信号，让任何遵从协议的 master 和 slave 都能即插即用。你只要记住下面这几类信号，就能读懂绝大多数 AHB 接口：

| 信号 | 方向（相对 master） | 含义 |
| --- | --- | --- |
| `HCLK` / `HRESETn` | 总线公共 | 总线时钟 / 低有效复位 |
| `HADDR[31:0]` | master→slave | 地址 |
| `HTRANS[1:0]` | master→slave | 传输类型（IDLE/BUSY/NONSEQ/SEQ） |
| `HSIZE[2:0]` | master→slave | 传输位宽（8/16/32…） |
| `HWRITE` | master→slave | 1=写，0=读 |
| `HWDATA[31:0]` | master→slave | 写数据 |
| `HRDATA[31:0]` | slave→master | 读数据 |
| `HREADY` | 双向 | 总线就绪（高有效） |
| `HRESP` | slave→master | 从设备响应（OKAY/ERROR…） |
| `HSEL` | 译码器→slave | 片选：「这次访问是不是给你」 |
| `HREADYOUT` | slave→多路选 | 本从设备自身就绪（参与多路选） |

**直觉记忆法**：master 永远是「发号施令」的一方（给地址、给写数据、说读还是写）；slave 是「应答」的一方（回读数据、说有没有准备好、有没有出错）；中间的**译码器**根据地址决定 `HSEL`，**多路选择器**根据 `HSEL` 把对应 slave 的应答挑出来送回 master。

#### 4.3.2 核心流程

一次 AHB 读传输的信号时序骨架（简化版，省略流水细节）：

```
周期1：master 驱动 HADDR=addr, HTRANS=NONSEQ, HWRITE=0(读)
         │
         ▼
       译码器：依 addr 置某 slave 的 HSEL=1
         │
         ▼
周期2：被选中 slave 驱动 HRDATA=data, HREADYOUT=1, HRESP=OKAY
         │
         ▼
       多路选择器：把该 slave 的 HRDATA/HREADYOUT 选到 sys_hrdata/sys_hreadyout
         │
         ▼
       master 在 HREADY=1 时采样 sys_hrdata，读到 data
```

关键点：`HREADY` 是「全局节拍」——任何一个 slave 没准备好，都可以把它拉低，让整个总线「等它一拍」，这就是所谓**等待状态（wait state）**，也正是 `cmsdk_mcu_defs.v` 里那些 `*_WS_N/*_WS_S` 宏在描述的东西。

#### 4.3.3 源码精读

**顶层对外的 AHB 接口**（连到外部存储器）：

[cmsdk/cmsdk_mcu_system_zed.v:72-77](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L72-L77) —— `HADDR/HTRANS/HSIZE/HWRITE/HWDATA/HREADY` 这一组，正是 AHB 标准信号；注释写明「AHB to memory blocks」，即这是把总线「引到片外/块外存储」的一组输出。

**片选是怎么产生的**（回到译码器）：

[cmsdk/cmsdk_mcu_system_zed.v:440-451](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L440-L451) —— 译码器输出端那一串 `.flash_hsel/.sram_hsel/.apbsys_hsel/.gpio0_hsel/...`。**每个 `*_hsel` 都是一条「这条地址归我管」的声明**。

**多路选择器把读数据收回来**：

[cmsdk/cmsdk_mcu_system_zed.v:514-516](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L514-L516) —— `slave_mux` 的输出 `.HREADYOUT(sys_hreadyout)/.HRESP(sys_hresp)/.HRDATA(sys_hrdata)`，这就是「所有 slave 里被选中那一个」的应答，统一送回 CPU 侧。

**作为对比，看 CPU 主端口**怎么连这些信号：

[cmsdk/cmsdk_mcu_system_zed.v:314-325](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L314-L325) —— `CORTEXM0INTEGRATION` 的「AHB-LITE MASTER PORT」一组端口，连到 `cm0_haddr/cm0_hwdata/cm0_hrdata/...`。和上面的对外接口、多路选输出对一对名字，你会发现它们是**同一套协议、不同实例**。

> 阅读技巧：在 SoC RTL 里看到 `H` 开头 + `sel/ready/resp/rdata/trans/size/write` 这几个词的组合，几乎可以立刻判定是 AHB 接口；看到 `P` 开头（如 `PCLK/PSEL/PENABLE`）则是 APB（慢速外设总线），看到 `M` 开头多为 AXI。本仓库后续 `cmsdk_apb_subsystem` 走的就是 APB。

#### 4.3.4 代码实践

**实践目标**：跟踪一个 AHB 信号「从 CPU 发出到回到 CPU」的完整通路。

**操作步骤**：

1. 从 [cmsdk_mcu_system_zed.v:315](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L315) 出发，CPU 的 `.HADDR(cm0_haddr)` 把地址给到 `cm0_haddr`。
2. 顺着 [L395](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L395) 的 `assign cm_haddr = cm0_haddr` → [L411](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L411) 的 `assign sys_haddr = cm_haddr`，地址最终叫 `sys_haddr`。
3. `sys_haddr` 进入译码器 [L435](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L435)，产生某个 `*_hsel`。
4. 该 slave 返回读数据，经 `slave_mux` 汇成 `sys_hrdata` [L516](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L516)。
5. `sys_hrdata` 经 [L421](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L421)/[L405](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L405) 回流成 `cm0_hrdata`，最终回到 CPU 的 `.HRDATA(cm0_hrdata)`。

**需要观察的现象**：一个信号名会经历 `cm0_* → cm_* → sys_* → (slave) → sys_* → cm_* → cm0_*` 的「改名接力」，但物理上始终是同一根线。

**预期结果**：你能用一句话讲清「CPU 给地址 → 译码选 slave → slave 回数据 → 多路选回流 → CPU 收数据」这条闭环，并指出每一跳对应的行号。

#### 4.3.5 小练习与答案

**练习 1**：`HREADY` 和 `HREADYOUT` 有什么区别？

> **参考答案**：`HREADYOUT` 是**单个 slave 自己**的「我准备好没有」信号；`HREADY` 是**整条总线**的最终就绪信号，由多路选择器综合所有 slave（及默认从设备）的 `HREADYOUT` 得到。master 只看 `HREADY`，slave 各自上报 `HREADYOUT`。

**练习 2**：`HTRANS` 取值 `NONSEQ`（非顺序）和 `SEQ`（顺序）分别表示什么？为什么要有这个区分？

> **参考答案**：`NONSEQ` 表示一次全新传输的开始（地址与上次无关）；`SEQ` 表示本次地址是上次的「顺序递增」（如突发读连续地址）。区分它们让 slave（尤其是存储器）能预判下一次地址、用更少周期完成突发传输，从而提升带宽。

---

### 4.4 复杂度跃迁对比

#### 4.4.1 概念说明

学到这里，我们已经把 `MY_DESIGN`（u2-l1）和 `cmsdk_mcu_system`（本讲）都过了一遍。把它们摆到一起，你会对「真实 SoC 比教学例子复杂多少」有直观感受。这是从「会写逻辑」到「能读系统」的关键一跃。

复杂度的差别体现在至少五个维度：**端口数、子模块数、参数化程度、总线/协议、时钟与复位域**。注意，复杂度上升并不只是「代码更长」，而是**设计关注点完全变了**——教学例子关心「这道算术对不对」，SoC 顶层关心「这一堆部件怎么协同、谁能中断谁、地址怎么分、总线会不会死锁」。

#### 4.4.2 核心流程

我们用一张对比表把两个设计量化摊开：

| 维度 | `MY_DESIGN`（u2-l1） | `cmsdk_mcu_system`（本讲） |
| --- | --- | --- |
| 顶层模块端口数 | **10** 个（`Cin1/Cin2/Cout/data1/data2/sel/clk/out1/out2/out3`） | **约 65** 个（基础配置，不含 FPGA 专用信号） |
| 子模块例化数 | **2** 个（`ARITH`、`COMBO`） | **9** 个（CPU + 译码 + 多路选 + 默认从 + ROM表 + sysctrl + 2×GPIO + APB子系统） |
| `parameter` 数 | **0**（位宽 `4:0` 全写死） | **12** 个（`BE/BKPT/DBG/NUMIRQ/SMUL/SYST/WIC/...`） |
| 总线协议 | 无（模块间直接 `wire` 连） | **AHB-LITE**（标准 `H*` 信号）+ APB |
| 时钟域 | **1** 个（`clk`） | **多** 个（`FCLK/HCLK/DCLK/SCLK/PCLK/PCLKG`） |
| 复位 | 无显式复位端口 | **4** 类（`HRESETn/PORESETn/DBGRESETn/PRESETn`） |
| 端口声明风格 | Verilog-1995 非 ANSI（端口名在头，类型在体） | Verilog-2001 ANSI（`input wire [31:0] X` 行内声明） |
| 条件编译 | 无 | 有（`` `ifdef ARM_DESIGNSTART_FPGA ``） |

端口规模的比值约为：

\[
\frac{\text{SoC 端口数}}{\text{MY\_DESIGN 端口数}} \approx \frac{65}{10} = 6.5
\]

\[
\text{子模块数比值} = \frac{9}{2} = 4.5
\]

也就是说，仅仅在「看得见的接线规模」上，SoC 顶层就大了半个数量级；而它真正难的地方——总线协议、地址映射、多时钟/复位域——是 `MY_DESIGN` 根本没有的维度。

#### 4.4.3 源码精读

先看「简单端」`MY_DESIGN` 的端口，体会它有多朴素：

[MY-Design/MY_DESIGN.v:2-7](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v#L2-L7) —— 一行列出 10 个端口名，下面再用 `input/output/reg` 分别声明类型。没有 `parameter`，没有 `wire [31:0]` 的总线，也没有时钟域/复位域的概念（只有一个 `clk`，连复位都没有）。

再看它的例化有多简单：

[MY-Design/MY_DESIGN.v:9-10](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v#L9-L10) —— 两行例化 `ARITH` 和 `COMBO`，端口一一对应，没有任何总线、译码、多路选。

「复杂端」`cmsdk_mcu_system` 的端口则是另一番景象：

[cmsdk/cmsdk_mcu_system_zed.v:57-67](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L57-L67) —— 仅「时钟 + 复位」相关的端口就列了 11 个（`FCLK/HCLK/DCLK/SCLK/HRESETn/PORESETn/DBGRESETn/PCLK/PCLKG/PRESETn/PCLKEN`）。在 `MY_DESIGN` 里，这部分只有 1 个 `clk`。

[cmsdk/cmsdk_mcu_system_zed.v:69-71](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L69-L71) —— 这里还出现了 `` `ifdef ARM_DESIGNSTART_FPGA `` 条件编译：只有定义了这个宏，才会多出 `zbt_boot_ctrl` 等 FPGA 专用端口。这种「同一份 RTL 服务多个目标」的手段，`MY_DESIGN` 里完全没有。

把两端并列，结论很清楚：**`MY_DESIGN` 教你「逻辑怎么写」，`cmsdk_mcu_system` 教你「系统怎么拼」**。后者多出来的所有复杂度，本质上都来自「把许多独立部件通过标准总线组织成一台可工作的计算机」这件事本身。

#### 4.4.4 代码实践（本讲主任务）

**实践目标**：量化对比两个设计的端口规模，并给 SoC 的对外接口做分类。

**操作步骤**：

1. 数 `MY_DESIGN` 的端口：打开 [MY-Design/MY_DESIGN.v:2-7](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v#L2-L7)，确认是 10 个端口名。
2. 数 `cmsdk_mcu_system` 的端口：打开 [cmsdk/cmsdk_mcu_system_zed.v:57-143](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L57-L143)，逐行统计 `input`/`output` 关键字，记下总数（基础配置约 65 个；若计入 `` `ifdef ARM_DESIGNSTART_FPGA `` 包裹的信号会更多）。
3. 把 SoC 的端口**按接口类型分组**，列出至少三类（建议分到六类以上）。参考分组：

   | 接口类型 | 代表信号 |
   | --- | --- |
   | 时钟 | `FCLK/HCLK/DCLK/SCLK/PCLK/PCLKG` |
   | 复位 | `HRESETn/PORESETn/DBGRESETn/PRESETn` |
   | AHB 总线 | `HADDR/HTRANS/HSIZE/HWRITE/HWDATA/HREADY` 及 `*_hsel/*_hrdata` |
   | 调试 (SWD/JTAG) | `nTRST/SWDITMS/SWCLKTCK/TDI/TDO/SWDO` |
   | UART | `uart0_rxd/uart0_txd/uart0_txen` … |
   | GPIO | `p0_in/p0_out/p0_outen/p0_altfunc`、`p1_*` |
   | 定时器 | `timer0_extin/timer1_extin` |
   | 状态/控制 | `SLEEPING/SLEEPDEEP/SYSRESETREQ/LOCKUP/PMUENABLE` |

**需要观察的现象**：`MY_DESIGN` 的端口几乎「无法分类」（全是数据 + 一个时钟），而 SoC 的端口天然按功能成簇。

**预期结果**：产出一张「端口数对比 + 接口分类表」，能清晰说出 SoC 至少包含「时钟、复位、AHB 总线」三类（以及调试、UART、GPIO 等更多类）接口；并量化得出端口数比值约 6.5。

#### 4.4.5 小练习与答案

**练习 1**：`MY_DESIGN` 没有复位端口，但 `cmsdk_mcu_system` 有 4 个复位。为什么真实 SoC 需要这么多复位？

> **参考答案**：因为 SoC 里有「内核/总线/调试/外设」等多个独立可复位的子域。例如 `PORESETn`（上电复位）复位整片，`HRESETn` 只复位 AHB 总线域，`DBGRESETn` 单独复位调试逻辑，`PRESETn` 复位外设域。分开复位是为了支持「调试时只复位内核而不动外设」等场景，是系统级电源/复位管理的需要。

**练习 2**：从「可综合性」角度，`MY_DESIGN` 和 `cmsdk_mcu_system` 哪个更适合直接拿来做教学综合练习？为什么？

> **参考答案**：`MY_DESIGN` 更适合。它规模小、无外部 IP 依赖、端口少，可以用任意开源综合器（如后续 u10-l1 的 yosys）轻松跑通 RTL→网表，便于观察综合结果。而 `cmsdk_mcu_system` 依赖 `CORTEXM0INTEGRATION` 等 ARM 授权 IP（仓库未随附核的 RTL），且端口/总线复杂，主要用于「读结构、学总线」，不适合作为综合入门练习对象。

**练习 3**：如果让你给 `MY_DESIGN` 也加上一个 `parameter WIDTH = 5`，需要改动哪几处？

> **参考答案**：把 `[4:0]` 全部换成 `[WIDTH-1:0]`（端口 `Cin1/Cin2/data1/data2/Cout/out1/out2/out3`、内部 `reg/wire`、子模块 `ARITH/COMBO` 的位宽），并在子模块也声明同名 `parameter`，例化时用 `#(.WIDTH(WIDTH))` 向下传递。这样就能从「5 位写死」升级为「任意位宽可配」，迈出参数化的第一步。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个**接口盘点**小任务（纯源码阅读型，无需工具）：

1. **建表**：为 `cmsdk_mcu_system` 制作一张完整的「外部接口清单表」，列四列——`接口类型 | 信号名 | 方向(input/output) | 位宽`。至少覆盖 **时钟、复位、AHB 总线、调试、UART、GPIO、定时器、状态/控制** 八类，每类列出全部相关信号（依据 [cmsdk_mcu_system_zed.v:57-143](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L57-L143)）。
2. **画图**：依据 4.2 节，画一张顶层结构图，标出 `CORTEXM0INTEGRATION(主)` → `addr_decode(译码)` → 各 `slave` → `ahb_slave_mux(多路选)` 的数据流向，并标注 `sys_haddr / *_hsel / sys_hrdata` 三组关键连线。
3. **追参数**：任选一个 `parameter`（如 `NUMIRQ`），写出它「声明于 L45 → 是否在某子模块被覆盖或转发」的完整路径（提示：可结合 [L427-452](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/cmsdk_mcu_system_zed.v#L427-L452) 的译码器例化观察地址类参数如何向下传）。
4. **写总结**：用 100~150 字回答——「相比 `MY_DESIGN`，SoC 顶层多出了哪些**系统级**关注点？」（提示词：总线协议、地址映射、多时钟/复位域、参数化裁剪、调试与中断、电源管理。）

**验收标准**：清单表信号无遗漏且方向/位宽正确；结构图能体现「主—译码—从—多路选」闭环；参数路径能说清「默认值 → 覆盖/转发」；总结里至少点到「总线 / 地址映射 / 多时钟域」三个系统级关键词。

---

## 6. 本讲小结

- **参数化**用 `parameter` 让一份 RTL 配出多种规格；它「每实例可不同」，区别于全局的 `` `define `` 宏；当配置会改变端口拓扑时（如 JTAG/SWD 切换），只能用宏 + `` `ifdef `` 条件编译。
- **SoC 顶层**是一块「接线板」：声明 `wire`、例化子模块、用命名端口连接把它们接到一起，自身几乎不含 `always` 计算逻辑。`cmsdk_mcu_system` 例化了 **9 个子模块**。
- 经典单主总线骨架是「**主 → 地址译码器 → 多个从 → 从设备多路选择器 → 主**」；译码器产 `HSEL`，多路选汇 `HRDATA/HREADYOUT/HRESP`。
- **AHB-LITE** 以 `H` 开头的标准信号通信：master 给 `HADDR/HTRANS/HWRITE/HWDATA`，slave 回 `HRDATA/HREADYOUT/HRESP`，`HREADY` 是全局就绪节拍。
- 复杂度跃迁是数量级的：端口 10→约 65（比值约 6.5），子模块 2→9，并新增了总线协议、地址映射、多时钟域、多复位域、参数化裁剪等 `MY_DESIGN` 完全没有的维度。
- 阅读技巧：看到 `H*` 想到 AHB、`P*` 想到 APB、`M*` 多为 AXI；信号改名接力（`cm0_*→cm_*→sys_*`）是 SoC 顶层解耦命名的常见手法。

---

## 7. 下一步学习建议

- **紧接着学 u2-l3（时序约束 SDC）**：本讲你见过了 SoC 的多时钟域（`HCLK/DCLK/SCLK...`），下一讲会用 `My_Design.cons` 讲 `create_clock`、时钟不确定性、`set_input_delay/set_output_delay` 等，正好回答「这些时钟在综合/STA 里怎么被描述和约束」。
- **后续 U3/U4 再回头**：本讲的 AHB 总线、地址映射、参数化，会在 ICC2 物理设计主流程里再次出现——届时你会看到这些 RTL 结构如何变成版图上的标准单元和宏单元。
- **延伸阅读（仓库内）**：想更细看外设，可读 `cmsdk/cmsdk_mcu_pin_mux.v`（引脚复用）和 `cmsdk/cmsdk_apb_subsystem` 相关逻辑；想验证理解，可对照 testbench [cmsdk/tb_cmsdk_mcu_zed.v](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/cmsdk/tb_cmsdk_mcu_zed.v) 看它如何驱动 `cmsdk_mcu_system` 的时钟、复位与 GPIO 端口。
