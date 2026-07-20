# 硬件调试 TCL 与 EPICS 集成

## 1. 本讲目标

在前两篇（u5-l1、u5-l2）里，我们走完了「处理器上跑裸机程序、通过 `Xil_Out32`/`Xil_In32` 读写 fpga_base 寄存器」这条路。但真实工程里，fpga_base 暴露的版本号、固件/软件编译日期、项目名、设施名这些信息，往往要被**两类完全不同的角色**消费：

1. **FPGA 调试工程师**：在实验室里刚把比特流下载进器件，处理器软件可能还没跑起来，就想通过 JTAG 直接读出寄存器，确认烧进去的版本对不对、LED 能不能点。
2. **控制系统 / 运行人员**：器件已经装到加速器、束流站等装置上，需要把版本和日期**持续地**呈现到 EPICS 的控制台（archiver、phoebus 界面），供运维远程监控。

本讲就讲 fpga_base 仓库为这两类角色准备的两条**系统侧集成路径**，它们都不写 HDL、也不写 C 驱动，而是站在 fpga_base 寄存器映射（u2-l3）之上再叠一层工具：

- `jtag_to_axi_master_cmd.tcl`：在 Vivado 硬件管理器里，通过 **JTAG-to-AXI Master** 直接对 AXI 寄存器做读写调试。
- `epics/FPGA_BASE.template`：用 **regDev** 设备支持，把寄存器映射成 EPICS 记录并每秒扫描。

读完本讲，你应当能够：

1. 说清楚 `create_hw_axi_txn` / `run_hw_axi` / `report_hw_axi_txn` 三件套如何「无处理器」地完成一次 AXI 读写。
2. 解释 `report_hw_axi_txn` 的 `-t`（类型）/`-w`（位宽）选项如何把读回的原始字解释成数值或字节，并看懂脚本里**字节倒序**循环的来龙去脉。
3. 读懂 EPICS template 中 `seq` / `longin` / `scalcout` / `stringin` 四类记录如何串成一条「每秒读 5 个寄存器 → 拼出可读日期串」的链路，以及它们与 regDev 偏移地址的对应关系。
4. 把这两条路径都挂回 u2-l3 的寄存器映射表，意识到「同一份硬件/软件契约，被 C 驱动、JTAG 脚本、EPICS template 三方各自重复实现」这一现实及其风险。

## 2. 前置知识

本讲不涉及 HDL 电路设计，但站在 u2-l3 寄存器映射之上。下列概念会用到（不熟也无妨，下面顺带解释）：

- **AXI 寄存器映射（u2-l3）**：fpga_base 把 64 个 32 位寄存器铺在 `0x00`~`0xFF` 的 256 字节空间里。本讲反复用到这些偏移：版本 `0x00`、固件日期 `0x04`~`0x14`、软件日期 `0x18`~`0x28`、项目串 `0x40`~`0x4C`、设施串 `0x50`~`0x5C`、LED `0x60`、DIP 开关 `0x64`。
- **字符串的大端打包（u2-l3）**：项目/设施两个 16 字符字符串在硬件里是「第 0 个字符放在一个 32 位字的最高字节」的格式存的，所以读出来要按字节倒序还原。
- **JTAG**：FPGA 的调试访问口。Vivado 下载比特流、读寄存器都走它。
- **JTAG-to-AXI Master**：Xilinx 提供的一个调试 IP（`jtag_axi`）。它一端挂在 JTAG 上、受 Vivado 硬件管理器遥控，另一端是一个 AXI4 主机口，可以直接发起 AXI 读写事务——**等价于在芯片里塞了一个「由 PC 操控的临时处理器」**。
- **EPICS**：实验物理与工业控制领域广泛使用的分布式控制系统（Experimental Physics and Industrial Control System）。核心概念是「记录（record）」——一个带名字、带类型、会被定时或事件驱动处理的变量。
- **regDev**：EPICS 的一个设备支持（device support）模块，专门用来把「内存映射寄存器」包装成 EPICS 记录。它在 PSI 这类大量使用 VME / AXI 总线的装置里是事实标准之一。
- **TCL 基础**：`proc`、`set`、`expr`、`lindex`、`for`、`format`。u3-l2、u4-l1 已接触过 TCL。

> 一句话定位：本讲两条路径分别是「**人机调试**（JTAG，临时的、交互的）」和「**机器集成**（EPICS，常驻的、定时的）」，它们都把 fpga_base 的寄存器映射当作公共契约来消费。

## 3. 本讲源码地图

| 文件 | 行数 | 角色 | 谁来执行它 |
|------|------|------|------------|
| [jtag_to_axi_master_cmd.tcl](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/jtag_to_axi_master_cmd.tcl) | 148 | 4 个调试函数：写/读 LED、读项目串+固件/软件日期、读板上 XADC | Vivado 硬件管理器（`source` 后人工调用） |
| [epics/FPGA_BASE.template](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/epics/FPGA_BASE.template) | 181 | 一份 EPICS 记录模板：把版本/日期/串映射成记录并每秒扫描 | EPICS 的 `msi`/`dbLoadTemplate`（实例化时展开） |
| hdl/fpga_base_v1_0.vhd（寄存器映射） | — | 提供偏移与打包格式的真相之源（u2-l3 已讲） | — |
| drivers/fpga_base/src/fpga_base.c/.h | — | 软件侧契约的另一份实现（u5-l1 已讲） | — |

关键认知：

- JTAG 脚本里**每个函数第一行都是 `reset_hw_axi`**，每个读写都是「建事务→跑事务→（读回）报事务」三步，**没有循环缓冲、没有中断**，是纯同步的「问一句答一句」。
- EPICS template 顶部用 `$(DEV)`/`$(SYS)`/`$(BASE)`/`$(FPGABASE)` 四个宏参数化，**实例化时**才填进具体装置名、VME 基地址、fpga_base 偏移；模板本身和具体装置解耦。
- 两个文件的偏移地址（`0x60` LED、`0x40` 项目、`0x04` 固件年份……）**必须与 HDL、C 头文件完全一致**——这正是 u2-l3 强调的「三方重复契约」。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：（4.1）JTAG-to-AXI 调试三件套；（4.2）读回数据的解释（数值 vs 字符串 vs 字节倒序）；（4.3）EPICS regDev 映射与定时扫描链。

### 4.1 JTAG-to-AXI 调试：无处理器地读写寄存器

#### 4.1.1 概念说明

裸机 C 驱动（u5-l1）依赖一个前提：**处理器已经能跑、且把 fpga_base 映射到了自己的地址空间**。但在调试早期——刚把比特流 prog 进器件、`xparameters.h` 还没生成、BSP 还没建——这个前提并不成立。此时唯一可靠、且几乎零前提条件的访问通道，就是 JTAG。

Vivado 的 JTAG-to-AXI Master 把这件事变得很直接：你在 Block Design 里放一个 `axi_dbg_hub` + `jtag_axi` 调试核，综合实现后下载。之后在 Vivado 硬件管理器里，PC 通过 JTAG 给这个核下命令，核就在芯片内部以 AXI 主机身份发起一次真实的事务，穿过 AXI 互连，抵达 fpga_base 的从机口。**对 fpga_base 而言，这次访问和处理器发来的没有任何区别**。

本仓库的 `jtag_to_axi_master_cmd.tcl` 就是把这套命令封装成 4 个好用的 `proc`，免得调试人员每次都手敲一长串 Vivado 命令。文件头明确写道：这些函数面向 **Arty 板（XC7A35TICSG324-1L）**——见 [jtag_to_axi_master_cmd.tcl:10](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/jtag_to_axi_master_cmd.tcl#L10)，这是示例/起点代码，迁到别的板子时路径与外设要自行调整。

#### 4.1.2 核心流程

每个调试函数的骨架都是同一个三步走（写操作少一步「报事务」）：

```
reset_hw_axi [get_hw_axis hw_axi_1]      # ① 复位 JTAG-to-AXI 主机，清掉残留事务
set addr [format 0x%x [expr {$base + OFFSET}]]
create_hw_axi_txn 名字 <axi主机> -type {READ|WRITE} -address $addr -len N -data ...
run_hw_axi  <事务>                        # ② 真正在硬件上跑这一笔 AXI 事务
# 只读操作还需要第三步：
report_hw_axi_txn <事务>                  # ③ 把读回的数据取出来解释
```

几个要点：

- `hw_axi_1` 是 Vivado 给那个 JTAG-to-AXI 主机核的默认硬件 AXI 对象名；`get_hw_axis hw_axi_1` 选中它。
- `-len N` 指的是 **AXI 突发长度（beat 数）**，单位是「字（32 位）」而不是字节。例如读固件日期 5 个寄存器（年/月/日/时/分）就 `-len 5`，对应一次 5 拍的 AXI 突发读。
- `create_hw_axi_txn` 只是「定义」一个事务对象，**不会真的访问硬件**；真正发起是 `run_hw_axi`。这种「定义—执行」分离与 u3-l2 里 `set_property`（改属性）和综合时机的关系异曲同工。
- 一次函数调用 = 一笔（或一次突发）AXI 事务。**没有轮询、没有中断、没有缓存**，完全是「问一句答一句」的同步模型。

以「点亮 LED」为例，整体走向是：

```
调用 fpga_base_led_wr $base 0xAA
   │
   ▼ addr = base + 0x60（LED 寄存器，u2-l3 中寄存器下标 24）
create_hw_axi_txn write_txn ... -type WRITE -data 0xAA -len 1
   │
   ▼ run_hw_axi → JTAG→axi 核发起 AXI 写
fpga_base 的 reg_wdata(24) ← 0xAA → o_led ← 0xAA（HDL 直通）
   │
   ▼ 板上 8 个 LED 中低 8 位=0xAA 的那些点亮
```

#### 4.1.3 源码精读

**写 LED**（AXI 单拍写，`0x60` = 寄存器 24，见 u2-l3）：

[jtag_to_axi_master_cmd.tcl:16-24](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/jtag_to_axi_master_cmd.tcl#L16-L24)

```tcl
proc fpga_base_led_wr {base value} {
   reset_hw_axi [get_hw_axis hw_axi_1];
   set addr [format 0x%x [expr {$base + 0x00000060}]]
   create_hw_axi_txn write_txn [get_hw_axis hw_axi_1] -force -type WRITE -address $addr -len 1 -data $value;
   run_hw_axi -quiet [get_hw_axi_txns write_txn];
   return;
}
```

- `0x00000060` 即 LED 寄存器偏移，与 [hdl/fpga_base_v1_0.vhd:291](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L291) 里 `reg_rdata(24)` 对应：HDL 把 `reg_wdata(24)` 直通回显并驱动 `o_led`，所以「写 0x60」就能直接点亮物理 LED。
- `-force` 表示若同名事务已存在就覆盖；`-len 1` 是单拍写（一个 32 位字）。

**读 LED**（同样的地址，改成 `-type READ`，多一步 `report_hw_axi_txn`）：

[jtag_to_axi_master_cmd.tcl:29-39](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/jtag_to_axi_master_cmd.tcl#L29-L39)

```tcl
create_hw_axi_txn read_txn ... -type READ -address $addr -len 1;
run_hw_axi -quiet [get_hw_axi_txns read_txn];
set led_val [lindex [report_hw_axi_txn -quiet [get_hw_axi_txns read_txn]] 1];
puts "   $led_val";
```

- 这里 `report_hw_axi_txn` **不带** `-t`/`-w`，返回的就是默认格式的一个列表，取 `[1]` 即数据本身（索引 0 是表头/计数，详见 4.2）。
- 注意 LED 寄存器在 HDL 里**只回显低 8 位**（`reg_rdata(24)(7 downto 0)`），所以读回的高 24 位是 0。

**读固件日期**（一次 5 拍突发读，覆盖年/月/日/时/分 5 个寄存器）：

[jtag_to_axi_master_cmd.tcl:81-92](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/jtag_to_axi_master_cmd.tcl#L81-L92)

```tcl
set addr [format 0x%x [expr {$base + 0x00000004}]]
create_hw_axi_txn read_txn ... -type READ -address $addr -len 5;
run_hw_axi -quiet [get_hw_axi_txns read_txn];
set date_array [report_hw_axi_txn -quiet -t u4 -w 32 [get_hw_axi_txns read_txn]]
set year   [lindex $date_array 1];
...
puts "   FW date:  $year.$month.$day $hour:$minute";
```

- 起始地址 `0x04`（寄存器 1 = 年），`-len 5` 一次把年/月/日/时/分（`0x04/0x08/0x0C/0x10/0x14`）全读回来，**用一次突发而不是 5 次单拍**，效率更高也更原子。
- `-t u4 -w 32`：把读回数据解释成「无符号 4 字节、每元素 32 位」，于是每个寄存器就是一个整数，直接 `lindex 1..5` 取出。日期寄存器里存的就是纯整数（年=2026 这种），不是 BCD、也不是打包位段，所以**无需任何移位或倒序**。

软件日期用同样的手法，起始地址换成 `0x18`（寄存器 6）：

[jtag_to_axi_master_cmd.tcl:95-106](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/jtag_to_axi_master_cmd.tcl#L95-L106)

> 回顾 u2-l3 / u5-l1：`0x04`~`0x14` 的固件日期由 FDPE 触发器 INIT 烧死（只读），`0x18`~`0x28` 的软件日期是「直通回显」可写、由 `fpga_base_version()` 在处理器启动时写入。JTAG 脚本对两者一视同仁地读，正适合在**还没跑软件**时对照：若 SW date 全 0，说明处理器程序尚未执行 `fpga_base_version()`。

#### 4.1.4 代码实践

**实践目标**：在不依赖任何处理器软件的前提下，亲手用 JTAG 把 LED 点亮、再读回来核对。

**操作步骤（待本地验证，需要一块带 JTAG-to-AXI Master 的板子和 Vivado 硬件管理器）**：

1. 在 Vivado 里打开含 fpga_base + `jtag_axi` 调试核的 Block Design，综合实现并下载比特流（`program_hw_devices`）。
2. 打开 Vivado 的 Tcl Console（Window → Tcl Console）。
3. `cd` 到本仓库根目录，`source jtag_to_axi_master_cmd.tcl`——这会定义 4 个 `proc`。
4. 用 `get_hw_axis *` 确认 JTAG-to-AXI 主机对象名是否就是脚本里写死的 `hw_axi_1`；若不是，需要在脚本里改这一处或改名。
5. 设好 fpga_base 的基地址变量，例如 `set base 0x40000000`（按你 Block Design 里 fpga_base 实际分配的地址填）。
6. 调用 `fpga_base_led_wr $base 0xAA`，观察板上 LED；再调用 `fpga_base_led_rd $base`，观察 Tcl Console 打印。
7. 调用 `fpga_base_fw_date_rd $base`，观察打印的 FW/SW 日期与项目串。

**需要观察的现象**：

- 写 `0xAA`（二进制 `1010_1010`）后，板上应有一半 LED 亮、一半灭，呈间隔图案。
- `fpga_base_led_rd` 打印的值应为 `0x000000aa`（高 24 位为 0，因 HDL 只回显低 8 位）。
- `fpga_base_fw_date_rd` 打印的 FW date 应近似你编译比特流的时刻（u3-l1/u3-l2 机制），SW date 在你跑过 `fpga_base_version()` 之前应为 `0.0.0 0:0`。

**预期结果**：上述四条都对得上，即说明「PC → JTAG → axi 核 → AXI 互连 → fpga_base 寄存器」这条无处理器通路全程贯通。若 `create_hw_axi_txn` 报找不到 `hw_axi_1`，说明调试核对象名不一致或核未使能。

#### 4.1.5 小练习与答案

**练习 1**：为什么每个函数开头都要 `reset_hw_axi [get_hw_axis hw_axi_1]`？去掉会怎样？

**参考答案**：JTAG-to-AXI 主机内部会保留上一次创建的事务对象与可能的中断/错误状态。`reset_hw_axi` 把它清回干净状态，避免上一次失败的 `create_hw_axi_txn` 残留同名事务（即便有 `-force`，状态机层面也建议复位）。去掉后，连续调用时偶发「事务仍存在 / 总线挂死」等问题，尤其在读写混用、前一笔事务异常时更明显。

**练习 2**：`fpga_base_fw_date_rd` 读固件日期用 `-len 5`、读项目串用 `-len 4`，这两个数字分别指什么单位？

**参考答案**：单位都是「32 位字（AXI beat）」，不是字节。日期是 5 个寄存器（年/月/日/时/分）所以 `-len 5`；项目串是 4 个寄存器（每字 4 字符 × 4 字 = 16 字符）所以 `-len 4`。对应的字节数分别是 20 和 16。

---

### 4.2 寄存器数据解释：`-t`/`-w` 与字节倒序

#### 4.2.1 概念说明

`run_hw_axi` 跑完后，读回的原始数据就躺在那个事务对象里。但「数据是什么」取决于你怎么解释它：同样 32 位，可以看成 1 个整数、4 个字节、或 2 个半字。`report_hw_axi_txn` 的 `-t`（type，元素类型）和 `-w`（width，每元素的位宽）两个选项就是干这件事的「解释器」。

更关键的是：**字符串寄存器在硬件里是大端打包的**（u2-l3），而 `report_hw_axi_txn -t u1` 列出字节时是「字内低位在前」的顺序。两者方向相反，所以脚本必须用一个小循环把每个 32 位字内的 4 字节**倒序**，才能还原出人能读的字符串。这一节就把这个「为什么要倒、怎么倒」讲透——它也是本讲 practice_task 要求解释的核心点之一。

#### 4.2.2 核心流程

先看大端打包在 HDL 里长什么样（这是倒序的根本原因）：

[hdl/fpga_base_v1_0.vhd:280-286](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L280-L286)

```vhdl
c_version_major_gen_loop: for i in 0 to (C_VERSION_MAJOR'high - 1) generate
   reg_rdata(16 + (i / 4))(((3 - (i rem 4)) * 8 + 7) downto ((3 - (i rem 4)) * 8)) <= ...
```

- 字符 `i` 落在第 `(16 + i/4)` 个寄存器（即 `0x40` 起的字），字节位置由 `(3 - (i rem 4)) * 8` 决定：`i=0` → 字节 3（最高字节 bits 31..24），`i=1` → 字节 2，…… 即「**第 0 个字符在最高字节**」。
- 这两个 generic 在 HDL 里叫 `C_VERSION_MAJOR` / `C_VERSION_MINOR`，但在系统层（EPICS、JTAG 脚本）被重命名为「项目串 / 设施串」——同一份硬件资源，不同角色给的语义名不同，这是工程里常见的现象。

读字符串的整体走向：

```
report_hw_axi_txn -t u1 -w 16   →  返回 17 元素列表：[表头, b0, b1, ..., b15]
                                          （字内低位在前，即每字的 LSB 排在前面）
        │
        ▼  双层循环：word=0..3（4 个字），byte=0..3（每字 4 字节）
   取下标 = 4 + word*4 - byte      ← 关键：在每个字的 4 字节块内「倒着取」
        │
        ▼  逐字符 append（若 ASCII>31，过滤掉未用的 0/控制符）
   得到正确顺序的字符串
```

倒序的「直觉版」证明：我们**知道**脚本输出的是正确可读的项目名（它是仓库里的成品代码），也**知道** HDL 把 `char0` 放在最高字节。若工具按「最高字节在前」列出，那么 `char0` 应该排在最前、无需倒序；但代码偏偏在每个字内倒着取——这就反证出 `report_hw_axi_txn -t u1` 实际是「**字内低位在前**」列字节的，脚本用倒序循环把它纠正回「高位在前 = char0 在前」。

#### 4.2.3 源码精读

读「项目串 minor」（设施名，`0x50` 起 4 个字）：

[jtag_to_axi_master_cmd.tcl:49-62](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/jtag_to_axi_master_cmd.tcl#L49-L62)

```tcl
set addr [format 0x%x [expr {$base + 0x00000050}]]
create_hw_axi_txn read_txn ... -type READ -address $addr -len 4;
run_hw_axi -quiet [get_hw_axi_txns read_txn];
set character_array [report_hw_axi_txn -quiet -t u1 -w 16 [get_hw_axi_txns read_txn]];
set character_string ""
for {set word 0} {$word < 4} {incr word} {
   for {set byte 0} {$byte < 4} {incr byte} {
      set character_val [lindex $character_array [expr {4 + $word * 4 - $byte}]]
      if {$character_val > 31} {
         append character_string [format %c $character_val];
      }
   }
}
puts "   Project minor: $character_string";
```

逐行拆解：

- `-t u1`：把数据解释成「无符号 1 字节」序列；`-w 16`：共 16 个这样的字节元素（4 字 × 4 字节）。返回列表共 17 项，`[0]` 是表头/计数，`[1]..[16]` 是 16 个字节值。
- 双层循环的外层 `word` 走 4 个 32 位字，内层 `byte` 走每字内的 4 字节。
- 下标公式 `4 + $word*4 - $byte`：
  - `word=0` 时取 `4,3,2,1`（字 0 的 4 字节，倒序）
  - `word=1` 时取 `8,7,6,5`（字 1，倒序）
  - ……以此类推，整体读出顺序是每个字「MSB→LSB」，恰好对应 HDL 的「char0 在 MSB」。
- `if {$character_val > 31}`：ASCII ≤ 31 是不可打印控制符（含 `0`），跳过——这样未用满 16 字符的串不会被一堆 `\0` 污染。`format %c` 把整数转回字符。

「项目串 major」（`0x40` 起）是完全相同的循环，只是起始地址换 `0x40`、打印标签换 `Project major`：[jtag_to_axi_master_cmd.tcl:65-78](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/jtag_to_axi_master_cmd.tcl#L65-L78)。

对比：日期寄存器**不用**倒序。因为日期每寄存器存的是单个整数（`-t u4 -w 32`，一整个 32 位字就是一个数），不存在「字内多字节排列」问题，直接 `lindex 1..5`：[jtag_to_axi_master_cmd.tcl:84-90](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/jtag_to_axi_master_cmd.tcl#L84-L90)。

**关于 XADC 读取（诚实的观察）**：脚本里还有第四个函数 `fpga_base_xadc_rd`，读 `base + 0x84`：

[jtag_to_axi_master_cmd.tcl:114-143](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/jtag_to_axi_master_cmd.tcl#L114-L143)

```tcl
set addr [format 0x%x [expr {$base + 0x00000084}]]
...
set xadc_temp [lindex $xadc_array 1];
set xadc_temp [expr {$xadc_temp / 65536.0 * 503.975 - 273.15}];
```

这里有两点必须如实说明：

1. `0x84` 对应寄存器下标 33（`0x84/4`），落在 fpga_base 寄存器映射的**未分配区**（u2-l3：HDL 只对下标 0-10、16-25 赋了值，其余读复位默认 0）。也就是说，**`0x84` 并不是 fpga_base 的寄存器**。这套公式（`raw/65536*503.975-273.15` 是 Xilinx XADC/系统监视器温度的标准换算；电压 `raw/65536*3.0`）读的是板上的 **XADC（System Monitor）IP**，不是 fpga_base。它出现在这里，是因为作者把「fpga_base 调试」和「Arty 板 XADC 调试」两类便利函数塞进了同一个脚本。
2. 因此，只有当调用者传入的 `base` 使得 `base+0x84` 恰好落到板上 XADC IP 的地址窗口时，这个函数才有意义；若直接拿 fpga_base 的基地址去调，读回的将是 0（复位默认值）。**是否在你的板子上可用，待本地验证**——这是示例代码迁板时必须重新核对的一处。

#### 4.2.4 代码实践

**实践目标**：亲手验证「字节倒序」的必要性，而不依赖真实硬件——纯 TCL 推演。

**操作步骤（源码阅读 + 推演型实践，无需硬件）**：

1. 假设项目串为 `"PSI"`，按 HDL 大端打包规则（`char0` 在每字最高字节）写出 `0x40` 那个字（寄存器 16）的 32 位值。`'P'=0x50, 'S'=0x53, 'I'=0x49`，则该字应为 `0x50534900`（char0=P 在 MSB，末字节 0）。
2. 模拟 `report_hw_axi_txn -t u1` 的「字内低位在前」输出：对 `0x50534900`，字节序列是 `00,49,53,50`（LSB 在前）。四个字（只有第一个非零）拼成 16 字节列表：`[表头, 00,49,53,50, 00,00,00,00, 00,00,00,00, 00,00,00,00]`，共 17 项。
3. 套用脚本的下标公式 `4 + word*4 - byte`，`word=0` 时取下标 `4,3,2,1` → 对应字节 `50,53,49,00` → 即 `'P','S','I','\0'`。
4. `>31` 过滤掉 `00`，`format %c` 拼出 `"PSI"`。

**需要观察的现象**：

- 若**不**倒序（直接按下标 `1,2,3,4` 取），得到的是 `00,49,53,50` → 过滤后是 `"ISP"`（字符顺序反了），证明倒序不可或缺。
- 倒序后得到 `"PSI"`，与原始字符串一致。

**预期结果**：手动推演与脚本逻辑一致，即可确认「4-word×4-byte 且字内倒序」正是对 HDL 大端打包的对称还原。这也回答了 practice_task 的后半问。

#### 4.2.5 小练习与答案

**练习 1**：若把 `report_hw_axi_txn` 的 `-w 16` 改成 `-w 8`，脚本的字符循环还能正确工作吗？

**参考答案**：不能。`-w` 控制返回多少个元素；`-w 16` 对应 16 字节（4 字 × 4 字节），下标公式 `4 + word*4 - byte` 正是按 16 字节布局写的（最大下标到 16）。改成 `-w 8` 后列表只有 8 个字节元素，循环里 `word≥2` 时下标会越界取到空值，字符串被截断或出错。`-len 4`（读 4 个字=16 字节）与 `-w 16`（解释成 16 字节）必须配套。

**练习 2**：为什么日期寄存器读取完全不需要类似倒序的处理？

**参考答案**：日期每个寄存器存的是「一个完整整数」（年份 2026 就直接是 `0x000007EA`），用 `-t u4 -w 32` 把整个 32 位字当作一个无符号整数取出来即可，`lindex 1` 就是年份。它不是「多个字符拼在一个字里」的结构，因而不存在字内字节排列问题，也就无需倒序。倒序只针对「字节级打包的字符串」这类寄存器。

---

### 4.3 EPICS regDev 映射：从寄存器偏移到控制台记录

#### 4.3.1 概念说明

JTAG 调试是「人偶尔去看一眼」，EPICS 集成则是「让机器持续地把寄存器值呈现到控制系统」。EPICS 的基本单位是**记录（record）**：每条记录有一个名字、一个类型（如 `longin` 长整型输入、`stringin` 字符串输入、`scalcout` 带字符串输出的计算）、以及一个**设备支持（DTYP）**决定它的数据从哪儿来。

`FPGA_BASE.template` 用 `DTYP = "regDev"` 把记录绑定到内存映射寄存器。regDev 读 `INP` 字段里的地址描述（形如 `@<base>:<offset> T=uint32`），在记录被处理时去对应地址取数。所以这份 template 本质上是「**把 u2-l3 的寄存器映射表，再翻译成一份 EPICS 记录清单**」——这是同一份硬件契约的第三种实现（前两种是 C 头文件宏和 JTAG 脚本里的立即数偏移）。

template 顶部的宏让这份清单与具体装置解耦：

[epics/FPGA_BASE.template:1-8](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/epics/FPGA_BASE.template#L1-L8)

```tcl
# $(DEV):         Device name
# $(SYS):         System name
# $(FPGABASE):    Offset of the FPGA base component
# $(BASE):      VME Base address of the FPGA
```

实例化时（通常通过一个 `.substitutions` 文件）填入具体值，例如 `DEV=BLA, SYS=FPGA, BASE=0x..., FPGABASE=0x...`，于是 `$(DEV):$(SYS)-VERSION-FW-YEAR` 展开成 `BLA:FPGA-VERSION-FW-YEAR` 这样的全装置唯一记录名。

#### 4.3.2 核心流程

整个 template 由四类记录配合完成「每秒读一次、拼出可读日期串」：

```
┌─────────────────────────────────────────────────────────────┐
│ record(seq, "...VERSION-SCAN0")  SCAN="1 second"             │
│   每秒处理一次，按 LNK1..LNK5 依次「推」5 条目标记录 (PP)      │
└──────┬──────────────────────────────────────────────────────┘
       │ LNK1            │ LNK2            │ LNK3        │ LNK4      │ LNK5
       ▼                 ▼                 ▼             ▼           ▼
  FW-VERSION        FW-YEAR           SW-YEAR        PROJECT     FACILITY
  (longin 0x00)    (longin 0x04)     (longin 0x18)  (stringin   (stringin
       │           │FLNK 链向下         │FLNK 链向下    0x40 L=16)  0x50 L=16)
       │           ▼                   ▼
       │      FW-YEAR-S            SW-YEAR-S
       │     (scalcout %4d)       (scalcout %4d)
       │           │FLNK                │FLNK
       │      FW-MONTH(0x08)       SW-MONTH(0x1C)
       │           ▼ ...按月/日/时/分级联，逐段格式化
       │      FW-DATE              SW-DATE
       │     (scalcout 拼串)      (scalcout 拼串)
       │     "YYYY.MM.DD HH:MM"   "YYYY.MM.DD HH:MM"
```

要点：

- **`seq` 记录是唯一带 `SCAN="1 second"` 的**，其余记录都是被动（由链接驱动处理）。这样「每秒一次」的总线访问节流由 `seq` 统一掌管，避免每个寄存器各自扫描造成总线争用。
- **`LNK` 用 `PP`（passive processing）**：seq 处理时主动「推」目标记录一把，让它们立即处理。`LNK2 → FW-YEAR` 一推，`FW-YEAR` 就读 `0x04`，然后沿 `FLNK`（forward link，处理完顺次激活下一条）把整条固件日期链一路触发到 `FW-DATE`。
- **`longin` 负责取数**（`DTYP=regDev`、`INP=@...+偏移 T=uint32`），**`scalcout` 负责格式化**（`CALC=PRINTF('%02d',A)` 把整数压成定宽字符串存在 `SVAL`），**末端的 `FW-DATE` 也是 `scalcout`**，把年/月/日/时/分 5 个字符串拼成最终可读日期。

为什么用「seq 单点触发 + FLNK 级联」，而不是让每条 `longin` 都 `SCAN="1 second"`？两个好处：

1. **一致性**：同一次扫描内，5 个日期寄存器在一条 FLNK 链上依次读出，拼出的 `FW-DATE` 用的必是同一组值；若各自独立扫描，处理顺序不确定，`FW-DATE` 可能混用两次扫描的值。
2. **节流**：每秒只产生一次集中的寄存器读脉冲，对总线和 fpga_base 的 AXI 从机更友好。

#### 4.3.3 源码精读

**定时器：`seq` 记录**

[epics/FPGA_BASE.template:12-24](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/epics/FPGA_BASE.template#L12-L24)

```tcl
record(seq, "$(DEV):$(SYS)-VERSION-SCAN0") {
   field(SCAN, "1 second")
   field(DLY1, "0.1") ... field(DLY5, "0.1")
   field(LNK1, "$(DEV):$(SYS)-VERSION-FW-VERSION PP")
   field(LNK2, "$(DEV):$(SYS)-VERSION-FW-YEAR PP")
   field(LNK3, "$(DEV):$(SYS)-VERSION-SW-YEAR PP")
   field(LNK4, "$(DEV):$(SYS)-VERSION-PROJECT PP")
   field(LNK5, "$(DEV):$(SYS)-VERSION-FACILITY PP")
}
```

- `seq`（sequence）记录有 16 组 `DLYn`/`LNKn`/`OFFn`/`A..P` 等“链”。处理时按顺序、带 `DLYn` 秒延迟，把 `LNKn` 指向的记录以 `PP`（被动）方式推一下。
- 这里用了 5 条链，分别推：固件版本号、固件日期链头（FW-YEAR）、软件日期链头（SW-YEAR）、项目串、设施串。每条之间隔 `0.1s`，避免瞬时总线拥塞。

**取数 + 格式化的级联（以固件日期为例）**

[epics/FPGA_BASE.template:35-45](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/epics/FPGA_BASE.template#L35-L45)

```tcl
record(longin,"$(DEV):$(SYS)-VERSION-FW-YEAR"){
   field(DTYP, "regDev")
   field(INP,  "@$(BASE):$(FPGABASE)+0x04 T=uint32")
   field(FLNK, "$(DEV):$(SYS)-VERSION-FW-YEAR-S")
}
record(scalcout,"$(DEV):$(SYS)-VERSION-FW-YEAR-S"){
   field(INPA, "$(DEV):$(SYS)-VERSION-FW-YEAR")
   field(CALC, "PRINTF('%4d',A)")
   field(FLNK, "$(DEV):$(SYS)-VERSION-FW-MONTH")
}
```

- `longin FW-YEAR`：`INP = @$(BASE):$(FPGABASE)+0x04 T=uint32`，即 regDev 从「VME 基地址 + fpga_base 偏移 + 0x04」读一个 `uint32`（年份），与 JTAG 脚本的 `0x04`、C 头文件的 `FPGA_BASE_SLV_REG1_OFS`（u5-l1）是**同一个偏移**。
- `FLNK` 指向 `FW-YEAR-S`：读完后立刻激活那条 `scalcout`。
- `scalcout FW-YEAR-S`：`INPA` 取上游 `FW-YEAR` 的数值，`CALC=PRINTF('%4d',A)` 把它格式化成 4 位定宽字符串存进 `SVAL`（字符串值字段），再 `FLNK` 给 `FW-MONTH`。
- 之后 `FW-MONTH(0x08) → FW-MONTH-S(%02d) → FW-DAY(0x0C) → FW-DAY-S → FW-HOUR(0x10) → FW-HOUR-S → FW-MINUTE(0x14) → FW-MINUTE-S`，每段都「读一个寄存器、格式化成定宽串」，逐级 `FLNK` 向下。

**末端拼接：`FW-DATE`**

[epics/FPGA_BASE.template:95-102](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/epics/FPGA_BASE.template#L95-L102)

```tcl
record(scalcout,"$(DEV):$(SYS)-VERSION-FW-DATE"){
   field(INAA, "$(DEV):$(SYS)-VERSION-FW-YEAR-S.SVAL")
   field(INBB, "$(DEV):$(SYS)-VERSION-FW-MONTH-S.SVAL")
   field(INCC, "$(DEV):$(SYS)-VERSION-FW-DAY-S.SVAL")
   field(INDD, "$(DEV):$(SYS)-VERSION-FW-HOUR-S.SVAL")
   field(INEE, "$(DEV):$(SYS)-VERSION-FW-MINUTE-S.SVAL")
   field(CALC, "AA+'.'+BB+'.'+CC+' '+DD+':'+EE")
}
```

- 5 个输入显式取上游各 `-S` 记录的 `.SVAL`（字符串值），分别映射到 `scalcout` 的 `AA..EE`。
- `CALC` 用字符串拼接：`AA+'.'+BB+'.'+CC+' '+DD+':'+EE`，结果即 `"2026.07.20 14:30"` 这种可读日期串，存于 `FW-DATE` 的 `SVAL`，供运维界面直接显示。
- 软件日期链（`SW-YEAR` 起，`0x18`~`0x28`）结构完全对称，末端也是 `SW-DATE`：[epics/FPGA_BASE.template:104-171](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/epics/FPGA_BASE.template#L104-L171)。

**版本号与字符串记录**

[epics/FPGA_BASE.template:29-33](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/epics/FPGA_BASE.template#L29-L33)

```tcl
record (longin,"$(DEV):$(SYS)-VERSION-FW-VERSION"){
   field(DTYP, "regDev")
   field(INP,  "@$(BASE):$(FPGABASE)+0x00 T=uint32")
   field(PRIO, "HIGH")
}
```

- 版本号在 `0x00`（寄存器 0），`PRIO="HIGH"` 仅影响 EPICS 扫描线程优先级，不改变读哪个地址。

[epics/FPGA_BASE.template:173-181](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/epics/FPGA_BASE.template#L173-L181)

```tcl
record(stringin,"$(DEV):$(SYS)-VERSION-PROJECT"){
   field(DTYP, "regDev")
   field(INP,  "@$(BASE):$(FPGABASE)+0x40 L=16")
}
record(stringin,"$(DEV):$(SYS)-VERSION-FACILITY"){
   field(DTYP, "regDev")
   field(INP,  "@$(BASE):$(FPGABASE)+0x50 L=16")
}
```

- `stringin` + `L=16`：regDev 从 `0x40`（项目）/`0x50`（设施）连读 16 字节（4 个字）作为字符串。这里的字节序处理交给 regDev 自身的约定（regDev 可在硬件配置里设定字节交换策略），与 JTAG 脚本里手动倒序是**两种实现、同一目的**——把 HDL 的大端打包还原成可读串。具体 regDev 在你的装置上是否默认就能正确还原字符串，建议结合 regDev 文档与本地实测确认（待本地验证）。
- 注意映射关系：HDL 里这两个字符串 generic 叫 `C_VERSION_MAJOR`（`0x40`）和 `C_VERSION_MINOR`（`0x50`），而 EPICS 里把它们语义化为「PROJECT」和「FACILITY」——又一次看到「硬件命名 vs 系统层命名」的错位，改一边时务必同步另一边。

#### 4.3.4 代码实践

**实践目标**：把 practice_task 的前半问走一遍——说明 `seq` 如何每秒触发、最终拼出固件日期串。

**操作步骤（源码阅读型实践，无需 EPICS 运行环境）**：

1. 在 `FPGA_BASE.template` 里找到 `VERSION-SCAN0` 这条 `seq` 记录，确认它 `SCAN="1 second"`，且 `LNK2` 指向 `...VERSION-FW-YEAR PP`。
2. 从 `FW-YEAR`（`longin`，`0x04`）出发，沿 `FLNK` 一条条画下去：`FW-YEAR → FW-YEAR-S → FW-MONTH → FW-MONTH-S → FW-DAY → FW-DAY-S → FW-HOUR → FW-HOUR-S → FW-MINUTE → FW-MINUTE-S → FW-DATE`，数一下一共读了几个寄存器、做了几次格式化。
3. 在 `FW-DATE` 的 `CALC` 里核对：5 个输入（`AA..EE`）分别来自哪条 `-S` 记录的 `.SVAL`，拼接格式是 `年.月.日 时:分`。
4. 对照 u2-l3 寄存器映射表，确认链上每个 `longin` 的偏移（`0x04/0x08/0x0C/0x10/0x14`）与 HDL 的固件日期寄存器一一对应。

**需要观察的现象**：

- 一次 `seq` 处理 → 推 `FW-YEAR` → 触发 5 次 `longin` 读（年/月/日/时/分）+ 6 次 `scalcout`（5 个格式化 + 1 个末端拼接）。
- 整条链在**同一次扫描、同一个扫描线程**内串行完成，`FW-DATE.SVAL` 拿到的是同一组一致的值。

**预期结果**：能画出上述链路图，并解释「`seq` 单点触发 + `FLNK` 级联」相比「每条记录独立 1 秒扫描」的一致性与节流好处。若你有 EPICS 环境，可用 `dbl` 列出这些记录、用 `cainfo`/`camonitor` 观察 `FW-DATE` 每秒刷新（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`FW-YEAR` 用 `%4d`、`FW-MONTH` 用 `%02d`，为什么宽度不同？

**参考答案**：年份是 4 位（如 2026），用 `%4d` 保证至少 4 位宽；月/日/时/分都是 2 位范围（1-12、1-31、0-23、0-59），用 `%02d` 不足 2 位时前补 0（如 7 → `07`），这样拼出的 `2026.07.20 14:05` 才是定宽对齐、不会出现 `2026.7.20 14:5` 这种跳动。

**练习 2**：如果运维只想每 10 秒看一次版本，最少改哪里？

**参考答案**：只需把 `seq` 记录的 `field(SCAN, "1 second")` 改成 `field(SCAN, "10 second")`（或 `".1 second"` 之类的合法 EPICS 扫描周期）。因为所有下游记录都是被动（由 `LNK`/`FLNK` 驱动），扫描频率完全由 `seq` 这一处统管——这正是「单点节流」设计带来的好处。

**练习 3**：template 里没有任何 `record(... LED ...)` 或 `record(... SWITCH ...)`，但 HDL 明明有 LED（`0x60`）和 DIP（`0x64`）寄存器。这说明什么？

**参考答案**：说明这份 template 是**按运维监控需求裁剪过的**——只把「值得长期归档/显示」的版本与日期类信息映射进 EPICS，而 LED/DIP 这种调试用 IO 没必要进控制系统（它们由 JTAG 脚本或 C 驱动处理即可）。它不是 fpga_base 寄存器的全集映射，体现了「同一硬件，不同消费者各取所需」。

---

## 5. 综合实践

把本讲三条线索串起来，完成一次「**三份契约对账**」。

**背景**：fpga_base 的寄存器映射（u2-l3）被 C 头文件、JTAG 脚本、EPICS template 三方各自硬编码了一遍偏移地址。这是个典型的「无编译期联动、靠人工保持一致」的契约，最怕某次改 HDL 后忘了同步其中某一处。

**任务**：

1. 建一张对账表，列出下列 9 个寄存器，分四列填出它们在 **(a) HDL 寄存器下标**、**(b) C 头文件偏移宏**（参见 u5-l1 的 `fpga_base.h`）、**(c) JTAG 脚本立即数**、**(d) EPICS template 偏移**：版本号、固件年/月/日/时/分、软件年、项目串、设施串、LED。
2. 逐行核对四列是否两两相等。重点核对：固件月份在 (c) 应是 `0x08`、在 (d) 应是 `+0x08`、在 (a) 应是寄存器下标 2。
3. 找一处「命名错位」：HDL 把项目串叫 `C_VERSION_MAJOR`，EPICS 把它叫 `VERSION-PROJECT`，两者偏移是否一致（都应在 `0x40`）？
4. 写一段话回答：如果有人在 HDL 里把软件日期从 `0x18`~`0x28` 挪到了 `0x30`~`0x40`，但只改了 HDL，**哪三个文件会同时出错**？出错的现象分别是什么（C 程序读到错的日期、JTAG 读到旧地址、EPICS 日期串异常）？

**参考答案要点**：

- 四列偏移应完全一致（版本 `0x00`、固件日期 `0x04`~`0x14`、软件日期 `0x18`~`0x28`、项目 `0x40`、设施 `0x50`、LED `0x60`）。
- `C_VERSION_MAJOR`(HDL) ↔ `VERSION-PROJECT`(EPICS) ↔ `Project major`(JTAG) 偏移同为 `0x40`，是同一资源的三个语义名。
- 改 HDL 不同步的三处：`drivers/fpga_base/src/fpga_base.h` 的 `C_*_OFS` 宏（C 程序写错地址，软件日期写到旧位置→回读为 0 或错乱）、`jtag_to_axi_master_cmd.tcl` 的 `0x18` 立即数（JTAG 读到旧地址的复位值 0）、`epics/FPGA_BASE.template` 的 `+0x18` 等（EPICS 日期串异常）。这正是本讲反复强调的「三方重复契约」的真实风险。

## 6. 本讲小结

- fpga_base 为两类系统侧消费者各留了一条路：**JTAG-to-AXI**（人机调试，无处理器前提）与 **EPICS + regDev**（机器集成，常驻定时）。
- JTAG 调试三件套是 `create_hw_axi_txn`（定义事务）→ `run_hw_axi`（硬件执行）→ `report_hw_axi_txn`（解释读回）；一次调用即一笔 AXI 事务，纯同步、无中断。
- `report_hw_axi_txn` 的 `-t`/`-w` 决定如何解释原始字：日期用 `-t u4 -w 32`（每字一个整数，直接取）；字符串用 `-t u1 -w 16`（每字节一个元素）。
- 字符串寄存器因 HDL **大端打包**（`char0` 在最高字节）而需要「**4-word×4-byte、字内字节倒序**」的小循环还原——这是本讲 practice_task 的核心，可由「脚本输出正确字符串」反证工具的列字节顺序。
- XADC 函数读的 `0x84` **不是 fpga_base 寄存器**（落在未分配区，读 0），它针对的是板上独立的 XADC/System Monitor IP，迁板时需重新核对基地址（待本地验证）。
- EPICS template 用一条 `seq`（`SCAN="1 second"`）单点触发，经 `LNK`/`FLNK` 级联把 5 个日期寄存器读出，再用 `scalcout` 的 `PRINTF` 逐段格式化、末端拼成 `"YYYY.MM.DD HH:MM"` 可读串；单点节流带来一致性与低总线负担。
- 偏移地址在 HDL、C 头文件、JTAG 脚本、EPICS template 四处各自硬编码、无编译期联动——改 HDL 必须同步另外三处，这是本系列反复强调的硬件/软件契约风险。

## 7. 下一步学习建议

本讲是软件与系统集成单元（u5）的最后一篇，也是整本 fpga_base 学习手册的收尾。建议：

1. **回到契约本身做一次回归**：以本讲「综合实践」的对账表为模板，把 u2-l3 的寄存器映射、u5-l1 的 C 宏、本讲的 JTAG/EPICS 偏移做一次完整 diff，亲手验证「四方一致」。这是把前 14 篇串起来的最佳方式。
2. **扩展到 regDev 与 EPICS 运维**：若你会在加速器/束流装置上部署，建议阅读 regDev 的字节序配置文档，确认 `stringin ... L=16` 在你的硬件上是否需要额外的 swap 设置；并学习 EPICS `archiver`、`phoebus` 如何消费这些 `longin`/`scalcout` 记录做长期归档与告警。
3. **扩展到 Vivado 硬件调试**：本讲的 JTAG 函数只覆盖了 fpga_base 几个寄存器。可进一步学习 `create_hw_axi_txn` 的 `-len`/突发、ILA（集成逻辑分析仪）与 VIO 的配合，构建一套完整的「无处理器硬件 Bring-up」调试脚本。
4. **横向对照同库其他 IP**：PSI 的 HDL 库里许多 IP 都遵循与 fpga_base 相同的「HDL + C 驱动 + EPICS template + JTAG 脚本」四件套结构。掌握本讲这套「同一寄存器映射被三种消费者各自实现」的范式后，再去读同库其他 IP 的集成层会非常轻松。
