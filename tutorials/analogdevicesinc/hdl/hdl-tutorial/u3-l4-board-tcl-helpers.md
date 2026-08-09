# 板级连线助手 Tcl：adi_board.tcl

## 1. 本讲目标

本讲讲解 ADI HDL 工程里「拼装块设计（Block Design, BD）」时用到的 Tcl 助手原语库 `adi_board.tcl`。学完后你应当能够：

- 理解 `ad_connect` 为什么只需要两个名字就能完成连线，并能说出它自动推断出的对象类型与对应 Vivado 命令。
- 看懂 `ad_ip_instance` / `ad_ip_parameter` 如何把「实例化一个 IP」封装成一行调用。
- 掌握 `ad_cpu_interconnect` 如何把外设寄存器映射到 CPU 地址空间，`ad_mem_hp*_interconnect` 如何为 DMA 铺设高速存储通路，以及两者方向为何相反。
- 理解 `ad_cpu_interrupt` 如何把一个 IP 的中断挂到系统中断控制器。
- 能在真实的 `fmcomms2_bd.tcl` 里逐行解释每一处助手调用把「谁」连到了「哪里」。

## 2. 前置知识

阅读本讲前，建议你已经掌握以下概念（来自前置讲义）：

- **块设计（Block Design）**：Vivado 里用图形化方式把 IP「拖线」组成系统的视图，底层是一张网表，由若干 IP 实例（cell）、引脚（pin）、接口引脚（interface pin）、网络（net）与端口（port）构成。
- **三层工程架构**（u2-l1）：每个 `system_bd.tcl` 先 source 载板基设计（定义处理器、时钟、复位等全局 Tcl 变量），再 source 评估板基设计（消费这些变量来连线）。本讲的助手正是连接这两层的「语言」。
- **Tcl 工程助手**（u3-l3）：`adi_project_create` 会先根据器件串算出全局变量 `sys_zynq`（0/1/2/3 分别表示 Microblaze / Zynq-7000 / Zynq UltraScale+ / Versal），再 `source system_bd.tcl`。本讲所有地址映射助手都依赖这个变量来自适应不同芯片家族。
- **AXI 总线**：寄存器访问走 AXI4-Lite（轻量、地址映射），大数据搬运走 AXI3/AXI4 的 HP/HPC 高性能口（接 DDR）。两者方向相反，是本讲的重点之一。

如果对 Vivado 原生 Tcl（`create_bd_cell`、`connect_bd_net`、`connect_bd_intf_net`、`assign_bd_address`）完全陌生也没关系，本讲会对比讲解。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [projects/scripts/adi_board.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_board.tcl) | 本讲核心：所有块设计连线助手原语的实现，约 1260 行。 |
| [projects/fmcomms2/common/fmcomms2_bd.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl) | 评估板基设计：用助手原语拼出 ad9361 收发通路、DMA、中断，是本讲最主要的「调用样例」。 |
| [projects/common/zcu102/zcu102_system_bd.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/zcu102/zcu102_system_bd.tcl) | 载板基设计：建立 `sys_ps8`、时钟/复位、`sys_concat_intc` 中断拼接器，并定义 `sys_cpu_clk` 等供评估板层消费的全局变量。 |
| [projects/fmcomms2/zcu102/system_bd.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_bd.tcl) | 系统特化层：仅 3 行 source，演示三层叠加的固定模式。 |
| [projects/scripts/adi_project_xilinx.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl) | 提供 `sys_zynq`、`use_smartconnect` 等全局变量的来源，解释助手如何自适应芯片家族。 |

## 4. 核心概念与源码讲解

本讲按「从最基础到最上层」的顺序拆成四个最小模块：

1. `ad_ip_instance` / `ad_ip_parameter`：实例化一个 IP（最底层积木）。
2. `ad_connect`：把两个对象连起来（贯穿全篇的核心原语）。
3. `ad_cpu_interconnect` 与 `ad_mem_hp*_interconnect`：地址空间映射（寄存器通路 + DMA 通路）。
4. `ad_cpu_interrupt`：中断接入。

收发器相关助手 `ad_xcvrcon` / `ad_xcvrpll` 留到 u8-l3 详讲，本讲只在模块 4 末尾简要提及。

### 4.1 IP 实例化封装：ad_ip_instance / ad_ip_parameter

#### 4.1.1 概念说明

在 Vivado 里把一个 IP 放进块设计，原生写法是：

```tcl
create_bd_cell -type ip -vlnv analogdevicesinc:user:axi_ad9361:1.0 axi_ad9361
set_property -dict [list CONFIG.ID {0}] [get_bd_cells axi_ad9361]
```

这里需要写全冗长的 **VLNV**（Vendor:Library:Name:Version）四元组，还要手动维护 `CONFIG.` 前缀。ADI 把它压缩成两行：

```tcl
ad_ip_instance  axi_ad9361 axi_ad9361          ;# 实例化
ad_ip_parameter axi_ad9361 CONFIG.ID 0          ;# 设参数
```

`ad_ip_instance` 接收「IP 名」和「实例名」，自己去 IP 仓库里按 VLNV 通配查找；`ad_ip_parameter` 接收「实例名、参数名、值」，对底层 cell 调 `set_property`。这样设计者只需记 IP 的短名，不必关心版本号与厂商前缀。

#### 4.1.2 核心流程

`ad_ip_instance` 的执行过程：

1. 用 `get_ipdefs` 按通配 `*:${i_ip}:*` 查找 IP 定义，并过滤掉有 `UPGRADE_VERSIONS`（即待升级）的旧版。
2. 若匹配到的是 `inline_hdl`（ADI 的内联 HDL 模块），则把 cell 类型标为 `inline_hdl`，否则为 `ip`。
3. `create_bd_cell` 用查到的 VLNV 创建实例。
4. 若传入了参数列表 `{name value ...}`，则给每一项加 `CONFIG.` 前缀后整体 `set_property`。

注意一个不对称：`ad_ip_instance` 的第三参数会**自动补 `CONFIG.` 前缀**，而 `ad_ip_parameter` 的参数名要**由调用方写全 `CONFIG.xxx`**。所以在样例里你会看到 `ad_ip_instance util_cpack2 ... { NUM_OF_CHANNELS 4 }`（无前缀）与 `ad_ip_parameter axi_ad9361 CONFIG.ID 0`（有前缀）并存。

#### 4.1.3 源码精读

`ad_ip_instance` 的实现：

[projects/scripts/adi_board.tcl:31-47](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_board.tcl#L31-L47) — 用 `get_ipdefs` 查 VLNV、区分 `ip` 与 `inline_hdl`、创建 cell，并对可选参数列表批量补 `CONFIG.` 前缀后 `set_property`。

`ad_ip_parameter` 的实现：

[projects/scripts/adi_board.tcl:55-58](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_board.tcl#L55-L58) — 直接对 `[get_bd_cells ${i_name}]` 调 `set_property ${i_param} ${i_value}`，不做任何前缀处理。

在 `fmcomms2_bd.tcl` 里的真实调用：

[projects/fmcomms2/common/fmcomms2_bd.tcl:33-38](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L33-L38) — 实例化 `axi_ad9361`，随后用 `ad_ip_parameter` 设 `CONFIG.ID`、`CONFIG.DAC_DDS_TYPE`、`CONFIG.DAC_DDS_CORDIC_DW`。

[projects/fmcomms2/common/fmcomms2_bd.tcl:119-122](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L119-L122) — 用第三参数（列表形式，无 `CONFIG.` 前缀）一次性实例化并配置 `util_cpack2`。

#### 4.1.4 代码实践

**实践目标**：理解 `ad_ip_instance` 第三参数的前缀处理与 `ad_ip_parameter` 的差异。

**操作步骤**：

1. 打开 `fmcomms2_bd.tcl`，对比第 33 行（`ad_ip_instance axi_ad9361 axi_ad9361`，无第三参数）与第 119 行（`ad_ip_instance util_cpack2 util_ad9361_adc_pack { NUM_OF_CHANNELS 4 SAMPLE_DATA_WIDTH 16 }`，带第三参数）。
2. 打开 `adi_board.tcl` 第 39-46 行，确认第三参数的每一项都被加上了 `CONFIG.` 前缀。
3. 再看第 34-38 行连续的 `ad_ip_parameter axi_ad9361 CONFIG.ID 0` 等调用。

**需要观察的现象**：带第三参数的调用里写的是 `NUM_OF_CHANNELS`（无前缀），而 `ad_ip_parameter` 写的是 `CONFIG.ID`（有前缀）。

**预期结果**：两种写法最终都会生成 `CONFIG.NUM_OF_CHANNELS` 这样的属性键，区别只在于前缀由谁来补。如果你把 `ad_ip_parameter` 误写成 `ad_ip_parameter axi_ad9361 ID 0`（漏掉 `CONFIG.`），`set_property` 会找不到该属性而失效。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ad_ip_instance` 要过滤 `UPGRADE_VERSIONS == ""` 的 IP 定义？
**答案**：避免选中已被新版本取代的旧版 IP，保证拿到的是当前可用版本，防止实例化后立刻触发「需要升级」的警告。

**练习 2**：`inline_hdl` 类型的 cell 与普通 `ip` 类型在创建时有何不同？
**答案**：普通 IP 用 `-type ip` 创建，对应仓库里打包好的 IP；`inline_hdl` 用 `-type inline_hdl` 创建，对应 ADI 直接以内联 HDL 方式注册进块设计的模块。两者的 VLNV 来源不同，但后续连线方式一致。

### 4.2 连线原语：ad_connect

#### 4.2.1 概念说明

`ad_connect` 是整个助手库里最核心、也被调用最多的原语。它只接收两个名字，却要处理块设计里几乎所有连线场景：

- 把两个 IP 的信号引脚连起来（`pin ↔ pin`）。
- 把一根已有网络连到一个引脚（`net ↔ pin`）。
- 给一个尚不存在的网络「命名」并接上引脚（`newnet ↔ pin`）。
- 把接口（interface，如一整组 AXI 信号）连起来（`intf_pin ↔ intf_pin`）。
- 把引脚接到常量 `GND`/`VCC`（`const ↔ pin`）。

原生 Vivado 要根据场景分别调用 `connect_bd_net`、`connect_bd_intf_net`，还要自己判断是否加 `-net`、是否需要先建常量源。`ad_connect` 把这些判断全自动化了——这就是「自动类型推断」。

#### 4.2.2 核心流程

`ad_connect a b` 的判定流程（伪代码）：

```
type_a = 推断(a 的对象类型)     # bd_pin / bd_net / bd_intf_pin / const / newnet
type_b = 推断(b 的对象类型)
若 a、b 一个是接口、一个不是接口 → 报错（接口只能连接口）
switch (type_a, type_b):
  pin,pin        → connect_bd_net a b
  net,pin        → connect_bd_net -net a b
  pin,net        → connect_bd_net -net b a
  pin,newnet     → connect_bd_net -net <b 的名字> a   # 用 b 的名字新建网络
  intf,intf      → connect_bd_intf_net a b
  const,pin/net  → 自动建一个 ilconstant 常量源再连
  net,newnet、const,const、newnet,newnet → 报错
```

类型推断的关键是 `ad_connect_int_class`：它依次用 `get_bd_intf_pins`、`get_bd_pins`、`get_bd_intf_nets`、`get_bd_nets` 去查这个名字，命中哪个就返回哪个的 `CLASS`；都不命中则返回 `"newnet"`；名字等于 `GND`/`VCC` 则返回 `"const"`。

常量处理也值得一提：`ad_connect VCC sys_ps8/...` 会按目标引脚位宽自动实例化一个名为 `VCC_<width>` 的 `ilconstant` IP，全 1 接出；`GND_<width>` 则全 0。同一个位宽的常量源会被复用，不会重复创建。

#### 4.2.3 源码精读

类型推断函数 `ad_connect_int_class`：

[projects/scripts/adi_board.tcl:82-103](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_board.tcl#L82-L103) — 依次尝试各类 `get_bd_*` 命令，命中即返回 `CLASS`；命中 `GND`/`VCC` 返回 `"const"`；全不命中返回 `"newnet"`。

常量源自建 `ad_connect_int_get_const`：

[projects/scripts/adi_board.tcl:106-131](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_board.tcl#L106-L131) — 按位宽算出常量值（VCC = 全 1），用 `ad_ip_instance ilconstant` 创建并复用同名 cell。

主流程 `ad_connect`：

[projects/scripts/adi_board.tcl:170-256](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_board.tcl#L170-L256) — 先做「接口/非接口」异或校验（第 177 行），再 `switch $type_a,$type_b` 分派到不同的 `connect_bd_net` / `connect_bd_intf_net` 调用，并在每个分支 `puts` 出等价的原生命令便于调试。

三层联动的真实例子——载板层先把 `sys_cpu_clk` 建成一根网络：

[projects/common/zcu102/zcu102_system_bd.tcl:73](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/zcu102/zcu102_system_bd.tcl#L73) — `ad_connect sys_cpu_clk sys_ps8/pl_clk0`，此时 `sys_cpu_clk` 是 `newnet`、`pl_clk0` 是 `bd_pin`，于是新建一根名为 `sys_cpu_clk` 的网络接到 PL 时钟输出。

[projects/common/zcu102/zcu102_system_bd.tcl:93-95](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/zcu102/zcu102_system_bd.tcl#L93-L95) — 把这根网络对象捕获进 Tcl 变量 `sys_cpu_clk`，供评估板层使用。

评估板层直接复用这个变量来接时钟：

[projects/fmcomms2/common/fmcomms2_bd.tcl:62](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L62) — `ad_connect $sys_cpu_clk util_ad9361_tdd_sync/clk`，此时 `$sys_cpu_clk` 求值后是 `bd_net`，对端是 `bd_pin`，走 `net↔pin` 分支。

两个 IP 引脚直连的例子：

[projects/fmcomms2/common/fmcomms2_bd.tcl:39-40](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L39-L40) — `ad_connect $sys_iodelay_clk axi_ad9361/delay_clk` 与 `ad_connect axi_ad9361/l_clk axi_ad9361/clk`，后者两端都是同一 IP 的引脚（`pin↔pin`）。

接口级连线的例子（整组 AXI-Stream 打包数据）：

[projects/fmcomms2/common/fmcomms2_bd.tcl:151](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L151) — `ad_connect util_ad9361_adc_pack/packed_fifo_wr axi_ad9361_adc_dma/fifo_wr`，两端都是接口引脚（`intf_pin↔intf_pin`），走 `connect_bd_intf_net`。

#### 4.2.4 代码实践

**实践目标**：在不运行 Vivado 的前提下，预测若干 `ad_connect` 调用会走哪个 switch 分支。

**操作步骤**：

1. 在 `zcu102_system_bd.tcl` 找到第 73 行 `ad_connect sys_cpu_clk sys_ps8/pl_clk0`，确认此时数据库里还没有名为 `sys_cpu_clk` 的对象。
2. 在 `fmcomms2_bd.tcl` 找到第 40 行 `ad_connect axi_ad9361/l_clk axi_ad9361/clk`。
3. 在 `fmcomms2_bd.tcl` 找到第 151 行接口连线。
4. 对照 `adi_board.tcl` 第 181-249 行的 `switch` 表，为每条调用标注 `(type_a, type_b)`。

**需要观察的现象**：第 73 行会新建一根网络；第 40 行两端都是已存在引脚；第 151 行两端都是接口。

**预期结果**：

| 调用 | (type_a, type_b) | 走的分支 |
| --- | --- | --- |
| 第 73 行 | (newnet, bd_pin) | `connect_bd_net -net sys_cpu_clk <pl_clk0>` |
| 第 40 行 | (bd_pin, bd_pin) | `connect_bd_net <l_clk> <clk>` |
| 第 151 行 | (bd_intf_pin, bd_intf_pin) | `connect_bd_intf_net <...>` |

待本地验证：在 Vivado 里实际 source 这两段脚本后，可用 `get_bd_nets sys_cpu_clk` 确认网络已创建。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ad_connect` 第 177 行要做「接口/非接口」异或校验？
**答案**：接口（interface）是一组信号的集合，只能与另一个接口相连；若把接口接到单个信号引脚上属于语义错误，提前报错比让 Vivado 在后续校验阶段抛出晦涩提示更友好。

**练习 2**：`ad_connect VCC sys_ps8/emio_spi0_ss_i_n`（见 `zcu102_system_bd.tcl` 第 121 行）会触发什么副作用？
**答案**：`ad_connect_int_get_const` 会按目标引脚位宽创建（或复用）一个名为 `VCC_<width>` 的 `ilconstant` cell，值全 1，再把它接到该引脚。同一宽度的常量源只创建一次。

**练习 3**：若写出 `ad_connect new_a new_b`（两个名字都不存在），会发生什么？
**答案**：进入 `newnet,newnet` 分支，报错「Cannot create connection between two new nets」。必须至少有一端是已存在的引脚/网络，`ad_connect` 才能为另一端命名建网。

### 4.3 地址空间映射：ad_cpu_interconnect 与 ad_mem_hp*_interconnect

#### 4.3.1 概念说明

地址映射解决两类截然不同的需求，`adi_board.tcl` 用两套助手分别处理：

1. **寄存器访问通路（CPU → 外设）**：CPU 是 AXI 主机，要读写各 IP 的寄存器。用 `ad_cpu_interconnect <地址> <IP名>` 把 IP 的 AXI4-Lite 从机接口挂到一个「CPU 主端口 → 多个 IP 从机」的 interconnect 上，并在指定偏移处分配地址段。
2. **DMA 数据通路（外设 → DDR）**：DMA 是 AXI 主机，要把数据搬进/搬出 DDR。用 `ad_mem_hpc0_interconnect` / `ad_mem_hpc1_interconnect` / `ad_mem_hp0..3_interconnect` 把 DMA 的主机接口挂到一个「多个 DMA 主机 → PS DDR 从端口」的 interconnect 上。

两者的关键区别是**数据流方向相反**：寄存器通路里 PS 是主、IP 是从；DMA 通路里 DMA 是主、PS DDR 是从。这决定了 interconnect 的 S/M 端口朝向不同。

`ad_cpu_interconnect` 还会根据 `sys_zynq` 自适应选择挂在哪个 PS 主端口：ZynqMP（`sys_zynq==2`）走 `M_AXI_HPM0_LPD`，Zynq-7000（`sys_zynq==1`）走 `M_AXI_GP0`，Versal（`sys_zynq==3`）走 CIPS 的 `M_AXI_FPD`，Microblaze（`sys_zynq==0`）走 `sys_mb/M_AXI_DP`。

#### 4.3.2 核心流程

`ad_cpu_interconnect` 实际是个分发器，按 `sys_zynq` 调底层 `ad_hpmx_interconnect`：

[projects/scripts/adi_board.tcl:1190-1205](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_board.tcl#L1190-L1205) — 五个分支对应五种架构，传入不同的「端口选择名」。

`ad_hpmx_interconnect` 的流程（寄存器通路）：

```
interconnect_name = axi_<sel>_interconnect     # 如 axi_hpm0_lpd_interconnect
若该 interconnect 还不存在（首次调用，M00）：
    实例化 smartconnect（或 axi_interconnect），NUM_MI=1
    把 S00_AXI 连到 PS 主端口（如 sys_ps8/M_AXI_HPM0_LPD）
    连 aclk / aresetn
否则（后续调用，M01..Mnn）：
    NUM_MI += 1
    把 M<nn>_AXI 连到目标 IP 的 AXI4-Lite 从机接口
按 sys_zynq 重算地址偏移（ZynqMP 有 +0x4e6/... 平移）
create_bd_addr_seg 在 CPU 地址空间 @p_address 处划出一段给该 IP
```

地址平移规则（[projects/scripts/adi_board.tcl:1164-1171](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_board.tcl#L1164-L1171)）：ZynqMP 上 `0x4xxxxxxx` 段会平移到 `0x8xxxxxxx`，`0x7xxxxxxx` 段平移到 `0x9xxxxxxx`，因为 ZynqMP 的 PL 可寻址孔径与 Zynq-7000 不同。这就是为什么 `fmcomms2_bd.tcl` 里写的 `0x79020000` 在 ZynqMP 上实际落在 `0x99020000`。

`ad_mem_hpx_interconnect` 的流程（DMA 通路，[projects/scripts/adi_board.tcl:703-959](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_board.tcl#L703-L959)）：

```
首次调用（M00）：实例化 axi_hpc0_interconnect，M00_AXI 连到 PS 的 S_AXI_HPC0_FPD（从端口），开启 PSU__USE__S_AXI_GP0 与 AFI0_COHERENCY
后续调用（S01..Snn）：NUM_SI += 1，把 DMA 的主机接口（如 m_dest_axi）作为新的从端口接入，assign_bd_address 划出 DDR 段
```

注意这里的 `M00` 连的是 PS **从**端口，`S<nn>` 连的是 DMA **主**端口——与寄存器通路正好相反。

#### 4.3.3 源码精读

寄存器通路分发：

[projects/scripts/adi_board.tcl:1190-1205](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_board.tcl#L1190-L1205) — `ad_cpu_interconnect` 按 `sys_zynq` 选择 `HPM0_LPD` / `GP0` / `FPD` 等端口名转交 `ad_hpmx_interconnect`。

首次调用建 interconnect 并接 PS 主端口：

[projects/scripts/adi_board.tcl:984-1023](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_board.tcl#L984-L1023) — 当 `i_str eq "M00"` 时实例化 smartconnect，并按 `sys_zynq` 把 `S00_AXI` 接到对应 PS 主端口（如 `sys_zynq==2` 接 `sys_ps8/M_AXI_HPM0_LPD`）。

后续调用挂 IP 与划地址段：

[projects/scripts/adi_board.tcl:1130-1180](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_board.tcl#L1130-L1180) — 自增 `NUM_MI`，把 `M<nn>_AXI` 接到 IP 从机接口，按 `sys_zynq==2` 的平移规则重算地址后 `create_bd_addr_seg`。

DMA 通路首次调用接 PS 从端口：

[projects/scripts/adi_board.tcl:787-797](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_board.tcl#L787-L797) — `HPC0` + `sys_zynq==2` 分支：开启 `PSU__USE__S_AXI_GP0`、`PSU__AFI0_COHERENCY`，把 `M00_AXI` 接到 `sys_ps8/S_AXI_HPC0_FPD`。

DMA 通路后续调用接 DMA 主端口：

[projects/scripts/adi_board.tcl:897-914](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_board.tcl#L897-L914) — 自增 `NUM_SI`，把 DMA 的主机接口作为 `S<nn>_AXI` 接入，最后 `assign_bd_address` 划出 DDR 段。

`fmcomms2_bd.tcl` 里两条通路的真实调用：

[projects/fmcomms2/common/fmcomms2_bd.tcl:221-223](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L221-L223) — 寄存器通路：把 `axi_ad9361`（@0x79020000）、ADC DMA、DAC DMA 各挂一个地址段，CPU 经此读写它们的寄存器。

[projects/fmcomms2/common/fmcomms2_bd.tcl:225-239](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L225-L239) — DMA 通路：按 `$CACHE_COHERENCY`（载板层第 6 行设为 `true`）选择 ZynqMP 的 HPC0/HPC1 或 Zynq-7000 的 HP1/HP2，把两路 DMA 的 `m_dest_axi`/`m_src_axi`/`m_sg_axi` 接到 PS DDR。这段 `if/else` 正是 u2-l1 所说的「评估板层依据载板层变量自适应连线」的典型体现。

#### 4.3.4 代码实践

**实践目标**：理解寄存器通路与 DMA 通路的端口朝向差异，以及 `CACHE_COHERENCY` 如何改变连线。

**操作步骤**：

1. 读 `fmcomms2_bd.tcl` 第 221-223 行，确认三处 `ad_cpu_interconnect` 的第二个参数是 IP 名（即寄存器从机）。
2. 读第 225-239 行，注意 `if {$CACHE_COHERENCY}` 分支里第一个 `ad_mem_hpc0_interconnect` 的第二个参数是 `sys_ps8/S_AXI_HPC0`（PS 从端口），其后才是 DMA 的 `m_dest_axi` 等（DMA 主端口）。
3. 回到 `zcu102_system_bd.tcl` 第 6 行确认 `set CACHE_COHERENCY true`。
4. 对照 `adi_board.tcl` 第 787-797 行，确认 HPC0 分支里 `M00_AXI` 接的是 PS 从端口。

**需要观察的现象**：寄存器通路里 IP 在 M 端（从机），DMA 通路里 DDR/PS 在 M 端（从机）、DMA 在 S 端（主机）。

**预期结果**：同一份 `fmcomms2_bd.tcl` 在 ZynqMP 载板上走 HPC0/HPC1（接 `sys_ps8`），若 `CACHE_COHERENCY` 为假则改走 HP1/HP2（接 `sys_ps7`），这正是它能在 zcu102（ZynqMP）与 zed/zc702（Zynq-7000）之间复用的原因。

待本地验证：在 Vivado 中打开生成的块设计，查看 Address Editor，确认 `axi_ad9361` 的地址段在 ZynqMP 上落在 `0x9xxxxxxx` 范围。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ad_cpu_interconnect` 在 ZynqMP 上要把 `0x79020000` 平移到 `0x99020000`？
**答案**：ZynqMP 的 PS-PL 可寻址孔径与 Zynq-7000 不同，`0x7xxxxxxx` 段在 ZynqMP 上不属于 PL 可用窗口，必须平移到 `0x9xxxxxxx` 这个有效孔径，否则地址段会落在不可达区域。

**练习 2**：寄存器通路和 DMA 通路里，interconnect 的 `M00_AXI` 分别接什么？
**答案**：寄存器通路里 `M00_AXI` 接 IP 的 AXI4-Lite 从机接口（CPU 是主机，从 S00 进、从 Mnn 出到各 IP）；DMA 通路里 `M00_AXI` 接 PS 的 HP/HPC 从端口（DMA 是主机，从 Snn 进、从 M00 出到 DDR）。

**练习 3**：`use_smartconnect` 变量由谁、依据什么设置？
**答案**：由 `adi_project_xilinx.tcl` 的 `adi_project_create` 依据器件串设置（[projects/scripts/adi_project_xilinx.tcl:185-188](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L185-L188)）：`xc7z`（Zynq-7000）器件用 `axi_interconnect`（`use_smartconnect=0`），因为 SmartConnect 在老家族上资源占用更大、时序更难收敛；其余器件用 `smartconnect`。

### 4.4 中断接入与其它收尾封装：ad_cpu_interrupt 等

#### 4.4.1 概念说明

外设产生中断后，需要把中断信号接到处理器的中断输入。不同架构中断控制器形态不同：

- ZynqMP（`sys_zynq==2`）：PS 有两组中断输入 `pl_ps_irq0`（位 0-7）、`pl_ps_irq1`（位 0-7，对应 ps 索引 8-15）。载板层预先放好两个 8 位拼接器 `sys_concat_intc_0/1`，默认全接 `GND`。
- Zynq-7000 / Microblaze（`sys_zynq<=1`）：用单个拼接器 `sys_concat_intc`。
- Versal（`sys_zynq==3`）：直接接 `sys_cips/pl_ps_irq<n>`。

`ad_cpu_interrupt <ps索引> <mb索引> <中断端口名>` 把这些差异隐藏掉：调用方只需声明「在 PS 架构用哪个索引、在 Microblaze 架构用哪个索引」，函数自行选择。

#### 4.4.2 核心流程

```
按 sys_zynq 在 ps 索引与 mb 索引间二选一 → p_index
解析出纯数字 p_index
若 sys_zynq==3：直接 ad_connect <端口> sys_cips/pl_ps_irq<p_index>
若 sys_zynq==2 且 p_index<=7：把 sys_concat_intc_0/In<p_index> 原接的 GND 断开，再 ad_connect 该 In 脚到中断端口
若 sys_zynq==2 且 p_index>=8：m_index=p_index-8，对 sys_concat_intc_1/In<m_index> 做同样替换
若 sys_zynq<=1：对 sys_concat_intc/In<p_index> 做同样替换
```

关键动作是「先 `disconnect_bd_net` 把载板层预接的 GND 拆掉，再 `ad_connect` 接上真实中断」。

另一个常用收尾封装是收发器连线助手 `ad_xcvrcon` / `ad_xcvrpll`（[projects/scripts/adi_board.tcl:319-579](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_board.tcl#L319-L579) 与 [projects/scripts/adi_board.tcl:586-591](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_board.tcl#L586-L591)），它们一次性完成 JESD204 链路 IP、收发器配置 IP 与 GT 之间数十根 lane/时钟/复位信号的连线。本讲只需知道它存在，细节留待 u8-l3。

#### 4.4.3 源码精读

`ad_cpu_interrupt` 的 ZynqMP 分支：

[projects/scripts/adi_board.tcl:1230-1246](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_board.tcl#L1230-L1246) — `p_index<=7` 改写 `sys_concat_intc_0/In<p_index>`，`p_index>=8` 改写 `sys_concat_intc_1/In<m_index>`，均先 `disconnect_bd_net` 再 `ad_connect`。

载板层预置的拼接器与默认 GND：

[projects/common/zcu102/zcu102_system_bd.tcl:150-174](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/zcu102/zcu102_system_bd.tcl#L150-L174) — 创建两个 8 位 `ilconcat`（`sys_concat_intc_0/1`），`dout` 接 `sys_ps8/pl_ps_irq0/1`，所有 `In` 脚默认接 `GND`。这是 `ad_cpu_interrupt` 之所以要先断开再接上的原因。

`fmcomms2_bd.tcl` 里的真实调用：

[projects/fmcomms2/common/fmcomms2_bd.tcl:243-244](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L243-L244) — `ad_cpu_interrupt ps-13 mb-12 axi_ad9361_adc_dma/irq` 与 `ps-12 mb-13 axi_ad9361_dac_dma/irq`。在 zcu102（`sys_zynq==2`）上，`ps-13` → `p_index=13` → `m_index=5` → 接到 `sys_concat_intc_1/In5`；`ps-12` → `p_index=12` → `m_index=4` → 接到 `sys_concat_intc_1/In4`。

收发器助手（仅作了解）：

[projects/scripts/adi_board.tcl:319-322](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_board.tcl#L319-L322) — `ad_xcvrcon` 用一堆 `global` 计数器（`xcvr_index` 等）维护多 lane、多链接的命名，把 JESD204 与 GT 间的连线压缩成一次调用。

#### 4.4.4 代码实践

**实践目标**：追踪一个中断从 IP 端口到 PS 中断引脚的完整路径。

**操作步骤**：

1. 在 `fmcomms2_bd.tcl` 第 243 行找到 `ad_cpu_interrupt ps-13 mb-12 axi_ad9361_adc_dma/irq`。
2. 推断在 zcu102（`sys_zynq==2`）上 `ps-13` 落到哪个拼接器的哪一位。
3. 回到 `zcu102_system_bd.tcl` 第 156-157 行，确认 `sys_concat_intc_1/dout` 接到了 `sys_ps8/pl_ps_irq1`。

**需要观察的现象**：中断信号先进入拼接器 `sys_concat_intc_1` 的某一位，再由拼接器汇总成一根总线送进 PS。

**预期结果**：`ps-13` → `sys_concat_intc_1/In5` →（汇总）→ `sys_ps8/pl_ps_irq1` 的第 5 位。这样软件侧的 IRQ 号 13 就对应到 PS 的 `pl_ps_irq1` 第 5 位。待本地验证：在 Vivado 块设计里点开 `sys_concat_intc_1` 查看其 `In5` 的连接源。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ad_cpu_interrupt` 要先 `disconnect_bd_net` 再 `ad_connect`？
**答案**：载板层已把所有拼接器输入默认接 `GND`（占位），保证未使用的中断位有确定电平。接入真实中断前必须先把对应位的 GND 拆掉，否则一个引脚会被驱动两次，引发多驱动（multi-driver）错误。

**练习 2**：`ad_cpu_interrupt ps-13 mb-12` 里的两个索引分别在什么架构下生效？
**答案**：`sys_zynq>=1`（Zynq-7000 / ZynqMP / Versal）用第一个参数 `ps-13`；`sys_zynq<=0`（Microblaze）用第二个参数 `mb-12`。这样同一行调用可跨架构复用。

**练习 3**：`ad_xcvrcon` 为什么要用 `global xcvr_index` 等计数器？
**答案**：一个工程里可能有多个收发器实例、每个实例又分 RX/TX 与多条 lane，需要全局唯一地命名 `rx_data_*_p/n`、`sync_*`、`sysref_*` 等端口。计数器在多次调用间累加，保证命名不冲突。

## 5. 综合实践

本任务把四个模块串起来，要求你在真实的 `fmcomms2_bd.tcl` 里逐处解读连线语义。

**任务**：在 [projects/fmcomms2/common/fmcomms2_bd.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl) 中找出至少三处分别使用 `ad_connect`、`ad_cpu_interconnect`、`ad_mem_hp*_interconnect`、`ad_cpu_interrupt` 的代码，按下表填写并解释，最后总结这些助手相对手写 Vivado Tcl 的简化效果。

建议填写格式：

| 行号 | 助手调用（摘录） | 哪个 IP 的哪个接口 | 连到了哪里 | 所属模块 |
| --- | --- | --- | --- | --- |
| 40 | `ad_connect axi_ad9361/l_clk axi_ad9361/clk` | `axi_ad9361` 的 `l_clk` | 同一 IP 的 `clk` 引脚 | 4.2 ad_connect |
| 221 | `ad_cpu_interconnect 0x79020000 axi_ad9361` | `axi_ad9361` 的 AXI4-Lite 从机 | CPU 地址空间 @0x79020000 | 4.3 寄存器通路 |
| 227 | `ad_mem_hpc0_interconnect $sys_cpu_clk axi_ad9361_adc_dma/m_dest_axi` | ADC DMA 的 `m_dest_axi` 主机 | HPC0 interconnect → DDR | 4.3 DMA 通路 |
| 243 | `ad_cpu_interrupt ps-13 mb-12 axi_ad9361_adc_dma/irq` | ADC DMA 的 `irq` | `sys_concat_intc_1/In5` → `pl_ps_irq1` | 4.4 中断 |

**操作步骤**：

1. 通读 `fmcomms2_bd.tcl` 全文，按「ad9361 core → tdd-sync → 时钟分频 → adc wfifo → adc cpack → adc dma → dac rfifo → dac upack → dac dma → interconnects → interrupts」的顺序梳理数据流。
2. 对每个 IP 实例，记录其 `ad_ip_instance` 与紧随其后的 `ad_ip_parameter`。
3. 重点关注第 219-244 行的「收尾段」：这里是地址映射与中断集中发生的地方。
4. 对照 `adi_board.tcl` 的实现，验证你对每条调用走哪个分支的判断。

**需要观察的现象**：

- 整个评估板层没有任何一处手写 `connect_bd_net` / `connect_bd_intf_net` / `create_bd_addr_seg`，全部经由助手完成。
- ADC 通路（`axi_ad9361` → `util_wfifo` → `util_cpack2` → `axi_dmac`）与 DAC 通路（`axi_dmac` → `util_upack2` → `util_rfifo` → `axi_ad9361`）方向相反，但都只用 `ad_connect` 串联。
- 第 225-239 行的 `if {$CACHE_COHERENCY}` 让同一份脚本在 ZynqMP 与 Zynq-7000 之间无修改复用。

**预期结果**（简化效果总结）：

1. **屏蔽 VLNV 与命令选择**：`ad_ip_instance` 免去手写 VLNV；`ad_connect` 自动在 `connect_bd_net` / `connect_bd_intf_net` 间选择并决定 `-net` 参数。
2. **自动建常量与命名**：接 `GND`/`VCC` 自动建/复用 `ilconstant`；接不存在的新名字自动命名建网。
3. **跨架构自适应**：`ad_cpu_interconnect` / `ad_mem_hp*_interconnect` / `ad_cpu_interrupt` 都按 `sys_zynq` 自动选对 PS 端口、地址平移与中断拼接器，使评估板层脚本与具体载板解耦。
4. **可读性**：每条助手调用都是「语义化」的一行（如「把这个 DMA 的 dest 口接到 HPC0」），而等价的原生 Tcl 往往需要多行且充斥样板代码。

待本地验证：若有 Vivado 环境，可在工程目录执行 `make`，构建完成后用 `open_bd_design` 打开块设计，逐个 IP 核对地址、连线与中断是否与你的解读一致。

## 6. 本讲小结

- `adi_board.tcl` 是 ADI 块设计的「连线 DSL」，把高频、易错的 Vivado 原生 Tcl 封装成语义化的一行调用。
- `ad_ip_instance` / `ad_ip_parameter` 封装 IP 实例化与参数设置；注意前者自动补 `CONFIG.` 前缀、后者不补。
- `ad_connect` 是核心原语，通过 `ad_connect_int_class` 自动推断对象类型（引脚/网络/接口/常量/新网络），分派到正确的 Vivado 连线命令，并自动处理常量源与网络命名。
- `ad_cpu_interconnect`（经 `ad_hpmx_interconnect`）铺设**寄存器通路**：CPU 主、IP 从，按 `sys_zynq` 选 PS 主端口并做地址平移。
- `ad_mem_hpc0/hpc1/hp0..3_interconnect`（经 `ad_mem_hpx_interconnect`）铺设 **DMA 通路**：DMA 主、PS DDR 从，方向与寄存器通路相反。
- `ad_cpu_interrupt` 把 IP 中断挂到系统中断拼接器（先断开默认 GND 再接），同样按 `sys_zynq` 自适应。

## 7. 下一步学习建议

- **进入数据通路深读**：本讲只讲了「怎么连线」，下一单元 u5-l1 将深入 `axi_dmac` 内部，看 DMA 引擎如何使用这里铺设的 `m_dest_axi` / `fifo_wr` 通路搬运数据。
- **阅读寄存器映射**：u4-l5 讲解 `up_axi` 与 `*_regmap.v`，配合本讲的 `ad_cpu_interconnect` 地址段，可以理解软件经 `0x79020000` 读写 `axi_ad9361` 寄存器的完整链路。
- **收发器助手**：若你关注 JESD204 高速设计，可跳到 u8-l3 详读 `ad_xcvrcon` / `ad_xcvrpll` / `gtwizard_generator.tcl`。
- **动手建议**：尝试仿照 `fmcomms2_bd.tcl` 的结构，为一个假想的单通道 ADC 用助手原语写出「IP 实例化 → cpack → dmac → 地址映射 → 中断」的最小连线脚本，体会它相对手写 Tcl 的简化效果。
