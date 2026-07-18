# 板卡移植与 ADI/Vivado 升级

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 openwifi-hw 如何用一个字符串 `BOARD_NAME` 驱动「板卡 → FPGA 器件型号 → 一组编译开关」的整条适配链路，并指出 `fpga_size_flag`、`ultra_scale_flag` 各自控制什么。
- 看懂 `parse_board_name.tcl` 这张「板名查表」，并解释它如何被 `openwifi.tcl`、`ip_repo_gen.tcl` 以及各 IP 的 `<ip>.tcl` 共享 `source`。
- 掌握 README「Migrate」小节给出的两种升级/移植思路：Vivado 自动升级法与「导出 openwifi_ip 层级再 source 进新 ADI 参考」法，并能动手实施第二种。
- 区分普通 Zynq（PS7）与 UltraScale+（PS8）两套 Tcl（`openwifi_ip.tcl` / `connect_openwifi_ip.tcl` 对 `*_ultra_scale.tcl`）在 IP 数量、DMA 位宽、PS 端口命名上的差异。
- 理解 `ultra_scale_tcl_gen.sh` 这个「批量 sed 生成器」是如何从 zc706 基线 Tcl 自动衍生出 UltraScale 版本的。

本讲是 **advanced** 阶段的「跨板卡/跨版本」专题。它不教新算法，而是回答一个工程问题：**当一块新板卡出现，或 ADI/Vivado 升级了一个大版本，openwifi 这套设计怎么搬过去？**

## 2. 前置知识

在学习本讲前，建议你已具备以下认知（这些都在前置讲义中建立）：

- **PS 与 PL**：Xilinx Zynq 是「ARM 处理系统（PS）+ FPGA 可编程逻辑（PL）」的 SoC。openwifi 的物理层与低层 MAC 跑在 PL，驱动跑在 PS（u1-l2、u2-l1）。
- **PS7 与 PS8**：经典 Zynq（7 系列，如 7020/7035/7045）的处理器系统叫 PS7；UltraScale+ MPSoC（如 zcu102 的 xczu9eg）的处理器系统叫 PS8。两者的 AXI 端口命名、时钟、中断控制器都有差别——这正是本讲要处理的核心差异。
- **block design 与层级单元（hierarchical cell）**：openwifi 把六个自研 IP 打包成一个叫 `openwifi_ip` 的层级子设计，对外只暴露 AXI/ADC/DAC/中断接口（u2-l2）。这个层级单元可以用 `write_bd_tcl` 导出成一段 Tcl，再在别的工程里 `source` 重建——这是移植的关键招式。
- **`BOARD_NAME` 是构建总开关**：它既是环境变量、又是目录名，还会被脚本反推并写成 Verilog 条件编译宏（u1-l3、u1-l4、u7-l2）。
- **`*_pre_def.v` 条件编译体系**：构建期现场生成的 `` `define `` 快照，用 `` `ifdef SMALL_FPGA`` 等宏裁剪代码（u7-l2）。

本讲会在这些基础上，把「板卡名如何分流到不同 Tcl 路径」与「层级如何跨工程搬运」两件事讲透。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用来讲什么 |
|------|------|----------------|
| [README.md](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md) | 项目说明书 | 「Migrate」小节给出的两种升级/移植方法 |
| [ip/parse_board_name.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/parse_board_name.tcl) | 板名 → 器件/规模查表 | 板名解析；`fpga_size_flag`、`ultra_scale_flag` 的来源 |
| [ip/openwifi_ip_ultra_scale.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip_ultra_scale.tcl) | UltraScale+（PS8）版层级蓝图 | UltraScale 适配：6 IP、128bit DMA、内部 xlconcat |
| [ip/openwifi_ip.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl) | 经典 Zynq（PS7）版层级蓝图（对照基准） | 与 ultra_scale 版逐项对比 |
| [ip/connect_openwifi_ip_ultra_scale.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/connect_openwifi_ip_ultra_scale.tcl) | PS8 版层级↔PS 接线脚本 | `_FPD` 端口、`pl_clk2`、被注释的中断行 |
| [ip/connect_openwifi_ip.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/connect_openwifi_ip.tcl) | PS7 版接线脚本（对照基准） | 与 ultra_scale 版对比 |
| [ip/ultra_scale_tcl_gen.sh](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/ultra_scale_tcl_gen.sh) | 批量 Tcl 衍生脚本 | 从 zc706 基线 sed 出 UltraScale 版 Tcl |

此外会引用两个「消费者」脚本来说明 `parse_board_name.tcl` 的产出如何落地：

- [boards/openwifi.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/openwifi.tcl)（顶层工程脚本）
- [boards/ip_repo_gen.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl)（IP 仓生成脚本）

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**板名解析**（4.1）、**层级迁移**（4.2）、**UltraScale 适配**（4.3）。三者环环相扣——先靠板名查表得到「器件型号 + 规模 + 是否 UltraScale」，再据此选用对应的层级蓝图与接线脚本，把 openwifi_ip 搬进目标板卡的 block design。

### 4.1 板名解析：一个字符串如何驱动整条适配链

#### 4.1.1 概念说明

openwifi-hw 支持十几块板卡，从 30 元级别的 Zynq 7020 小板到 zcu102 这种 UltraScale+ 大卡。如果每块板都维护一份独立源码，工程会立刻失控。openwifi 的做法是：

- 用 **一个字符串 `BOARD_NAME`**（如 `zc706_fmcs2`、`zcu102_fmcs2`、`adrv9364z7020`）唯一标识一块板；
- 用 **一张查表** `parse_board_name.tcl`，把 `BOARD_NAME` 翻译成四个派生量：器件型号 `part_string`、板卡 VLNV `board_part_string`、板 ID `board_id_string`、**规模标志 `fpga_size_flag`**、**UltraScale 标志 `ultra_scale_flag`**；
- 让所有需要这些信息的 Tcl 脚本都 `source` 这同一张表，做到「改一处，全工程生效」。

这里有两个关键标志：

- **`fpga_size_flag`**：0=小（7020 级），1=大（7035/7045/UltraScale）。它不改变 IP 列表，而是控制 **资源深度**——小器件上把 DMA FIFO 与 BRAM 深度砍半（`SMALL_FPGA`、`SIDE_CH_LESS_BRAM`），让设计塞得下。
- **`ultra_scale_flag`**：0=经典 Zynq（PS7），1=UltraScale+（PS8）。它决定走哪一套层级/接线 Tcl（见 4.3）。

> 一句话区分：`fpga_size_flag` 管「砍不砍资源」，`ultra_scale_flag` 管「走 PS7 还是 PS8 那套 Tcl」。

#### 4.1.2 核心流程

```
                   当前所在目录名（如 .../boards/zcu102_fmcs2）
                              │  [lindex [split [exec pwd] /] end]
                              ▼
                        BOARD_NAME = "zcu102_fmcs2"
                              │  source parse_board_name.tcl
                              ▼
        ┌─────────────────────┴──────────────────────┐
        │  part_string        = "xczu9eg-ffvb1156-2-e"
        │  board_part_string  = "xilinx.com:zcu102:part0:3.4"
        │  fpga_size_flag     = 1   (大器件，不砍资源)
        │  ultra_scale_flag   = 1   (PS8，走 ultra_scale Tcl)
        └─────────────────────┬──────────────────────┘
                              │
   ┌──────────────────────────┼───────────────────────────┐
   ▼                          ▼                           ▼
openwifi.tcl             ip_repo_gen.tcl            各 IP 的 <ip>.tcl
(建顶层工程,part)        (生成 clock_speed.v/       (打包单个 IP,
                          fpga_scale.v 宏)           打印 flag 供检查)
```

三处 `source` 共享同一张表，保证 `part_string`、`fpga_size_flag` 等取值在顶层工程、IP 仓、单 IP 工程里完全一致。

#### 4.1.3 源码精读

**① `BOARD_NAME` 来自当前目录名，而不是命令行参数。** 这是 openwifi 的一个重要约定：

[boards/openwifi.tcl:16-18](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/openwifi.tcl#L16-L18) 把 `pwd` 按 `/` 切片取最后一段当 `BOARD_NAME`，随后 `source` 查表：

```tcl
set BOARD_NAME [lindex [split [exec pwd] /] end]
puts "openwifi.tcl BOARD_NAME $BOARD_NAME"
source ../../ip/parse_board_name.tcl
```

[boards/ip_repo_gen.tcl:9-11](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl#L9-L11) 用完全相同的两行取得 `BOARD_NAME` 并 `source` 同一张表。这就是 u1-l3 提到的「`create_ip_repo.sh` 靠当前目录名反推 `BOARD_NAME`」的真正落点。

**② 查表本身：一长串 `if/elseif`，每块板四个标志。** 顶部注释点明 `fpga_size_flag` 的含义：

[ip/parse_board_name.tcl:5](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/parse_board_name.tcl#L5)

```tcl
# fpga_size_flag: 0 small; 1 big
```

以 zcu102（本讲主角，UltraScale+）为例：

[ip/parse_board_name.tcl:13-18](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/parse_board_name.tcl#L13-L18)

```tcl
} elseif {$BOARD_NAME=="zcu102_fmcs2"} {
   set ultra_scale_flag 1
   set part_string "xczu9eg-ffvb1156-2-e"
   set board_part_string "xilinx.com:zcu102:part0:3.4"
   set board_id_string "zcu102"
   set fpga_size_flag 1
```

对比经典 Zynq 中等规模板 zc706（7045，大器件但非 UltraScale）：

[ip/parse_board_name.tcl:19-24](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/parse_board_name.tcl#L19-L24)

```tcl
} elseif {$BOARD_NAME=="zc706_fmcs2"} {
   set ultra_scale_flag 0
   set part_string "xc7z045ffg900-2"
   set board_part_string []
   set board_id_string "zc706"
   set fpga_size_flag 1
```

注意 zc706 是「大器件（`fpga_size_flag=1`）但非 UltraScale（`ultra_scale_flag=0`）」——这两个标志彼此独立，不要混为一谈。再看小器件典型 zed（7020）：

[ip/parse_board_name.tcl:7-12](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/parse_board_name.tcl#L7-L12) —— `fpga_size_flag 0`、`ultra_scale_flag 0`、`part_string "xc7z020clg484-1"`。

如果传入了不认识的板名，查表落到 `else` 分支并把所有标志置空、打印报错：

[ip/parse_board_name.tcl:73-80](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/parse_board_name.tcl#L73-L80)

```tcl
} else {
   set ultra_scale_flag []
   set part_string []
   set fpga_size_flag []
   ...
   puts "$BOARD_NAME is not valid!"
}
```

**③ `fpga_size_flag` 如何变成 Verilog 宏。** 顶层脚本拿到 `fpga_size_flag` 后，把它写进 `clock_speed.v` 的 `SMALL_FPGA` 宏：

[boards/openwifi.tcl:21-27](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/openwifi.tcl#L21-L27)

```tcl
set NUM_CLK_PER_US 100
set  fd  [open  "./ip_repo/clock_speed.v"  w]
puts $fd "`define NUM_CLK_PER_US $NUM_CLK_PER_US"
if {$fpga_size_flag == 0} {
  puts $fd "`define SMALL_FPGA 1"
}
close $fd
```

`ip_repo_gen.tcl` 里还有一处并行的逻辑，把小器件的 side_ch BRAM 也砍掉：

[boards/ip_repo_gen.tcl:38-43](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl#L38-L43)

```tcl
set  fd  [open  "./ip_repo/fpga_scale.v"  w]
if {$fpga_size_flag == 0} {
  puts $fd "`define SIDE_CH_LESS_BRAM 1"
}
close $fd
```

于是得到本模块最重要的结论之一：**`fpga_size_flag` 影响的是 `SMALL_FPGA`（给 tx_intf/rx_intf/xpu，砍 DMA FIFO 深度）和 `SIDE_CH_LESS_BRAM`（给 side_ch，砍 BRAM 深度），即「资源深度」，而不改 IP 列表。** IP 列表（5 个还是 6 个）的差异由 4.3 的 PS7/PS8 之分决定。

#### 4.1.4 代码实践

> **实践目标**：亲手验证 `BOARD_NAME → {part_string, fpga_size_flag, ultra_scale_flag}` 的映射，并追踪 `fpga_size_flag` 落到了哪个宏。

1. **阅读型步骤（无需 Vivado）**：打开 [ip/parse_board_name.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/parse_board_name.tcl)，按下表逐行核对四块代表板的取值：

   | BOARD_NAME | part_string | fpga_size_flag | ultra_scale_flag |
   |------------|-------------|----------------|------------------|
   | `zed_fmcs2` | `xc7z020clg484-1` | 0 | 0 |
   | `zc706_fmcs2` | `xc7z045ffg900-2` | 1 | 0 |
   | `adrv9361z7035` | `xc7z035ifbg676-2L` | 1 | 0 |
   | `zcu102_fmcs2` | `xczu9eg-ffvb1156-2-e` | 1 | 1 |

2. **追踪宏落地**：在 [boards/ip_repo_gen.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl) 中搜索 `fpga_size_flag`，确认它共出现 3 处（L39、L49 各驱动一个宏）。
3. **现象/预期结果**：你会看到 `fpga_size_flag==0` 时生成 `` `define SIDE_CH_LESS_BRAM 1`` 与 `` `define SMALL_FPGA 1``；`==1` 时这两个宏都不生成（即不裁剪）。这是「同一份源码跨 7020 到 zcu102 都塞得下」的关键。
4. 若想本地跑通完整构建（需 Vivado 2022.2），按 README「Build FPGA」在 `boards/<BOARD_NAME>/` 下执行 `../create_ip_repo.sh $XILINX_DIR`，构建日志里会打印 `BOARD_NAME ...` 与（在单 IP Tcl 里）`ultra_scale_flag ... / fpga_size_flag ...` 行，可与你的核对结果比对。**待本地验证。**

#### 4.1.5 小练习与答案

**练习 1**：为什么 `zc706_fmcs2` 的 `fpga_size_flag=1` 但 `ultra_scale_flag=0`？这两个标志分别决定什么？

> **答案**：zc706 用的是 7z045，逻辑资源充足故 `fpga_size_flag=1`（不裁剪 FIFO/BRAM）；但它仍是经典 Zynq 7 系列而非 UltraScale+，故 `ultra_scale_flag=0`（走 PS7 的 `openwifi_ip.tcl`/`connect_openwifi_ip.tcl`）。前者管资源深度，后者管 PS7/PS8 Tcl 选型。

**练习 2**：如果有人新增了一块「Zynq 7020 的新板」，需要在哪里登记，才能让 `openwifi.tcl` 正确识别？

> **答案**：在 [ip/parse_board_name.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/parse_board_name.tcl) 的 `if/elseif` 链中新增一条分支，设 `part_string`（应为 `xc7z020clg400-1` 之类）、`fpga_size_flag 0`、`ultra_scale_flag 0`；同时要在 `boards/` 下建同名目录、在 README 板卡表里登记。

---

### 4.2 层级迁移：把 openwifi_ip 搬到新设计

#### 4.2.1 概念说明

ADI 的 HDL 参考设计每个版本都会变（新增 IP、改端口、升级 Vivado）。openwifi 是「长在 ADI 参考设计之上」的——它把 ADI 提供的 AD9361 数据通路当作底座，再把自己的 `openwifi_ip` 层级接进去。当底座换了版本，就产生了「怎么把 openwifi_ip 搬到新底座」的迁移问题。

README 的「Migrate」小节给出两种官方推荐方法：

- **Method 1（Vivado 自动升级）**：让新版本 Vivado 自己升级旧工程，再 `write_bd_tcl` 导出新脚本，与原 `openwifi.tcl` 做 diff，人工把差异补回去。适合「只是升 Vivado 小版本」。
- **Method 2（从新 ADI 参考设计起步，重新塞入 openwifi_ip 层级）**：把 openwifi_ip 这个**层级单元**单独导出成 Tcl，再 `source` 进一个全新的 ADI 参考设计工程。适合「换板卡 / 换大版本 / 自定义底座」。

Method 2 是更通用、更值得掌握的招式，它的核心是 Vivado IP Integrator 的 `write_bd_tcl -hier_blks` 命令（详见 Xilinx UG994）。而本仓库里的 `ip/openwifi_ip.tcl` 与 `ip/openwifi_ip_ultra_scale.tcl`，**本质就是 Method 2 中「导出的层级 Tcl」的一份手工维护版**——它们定义了 `create_hier_cell_openwifi_ip` 这个过程，能在任何已打开的 block design 里凭空重建出 openwifi_ip 层级。

#### 4.2.2 核心流程

Method 2 的标准三步（README 原文）：

```
# 1) 在旧/当前 Vivado 工程里，把 openwifi_ip 层级导出成 Tcl
write_bd_tcl -hier_blks [get_bd_cells /hier_mig] ./mig_hierarchy.tcl

# 2) 打开新的 ADI 参考设计（新版 Vivado）后，载入这段 Tcl
source ./mig_hierarchy.tcl

# 3) 在当前 block design 顶层创建出该层级实例
create_hier_cell_hier_mig / my_new_hierarchy
```

落到 openwifi 的语境：

```
ip/openwifi_ip.tcl (PS7)          ip/openwifi_ip_ultra_scale.tcl (PS8)
        │                                  │
        │  内含 proc create_hier_cell_openwifi_ip { parentCell nameHier }
        │  ——这就是上面第 3 步的 create_hier_cell_hier_mig 的 openwifi 版
        ▼
  在目标 block design 里 source 后调用：
  source ip/openwifi_ip.tcl
  create_hier_cell_openwifi_ip / openwifi_ip
        │
        ▼
  openwifi_ip 层级出现在新设计中 → 再用 connect_openwifi_ip*.tcl 接 PS
```

换句话说，`create_hier_cell_openwifi_ip` 这条 proc 就是「openwifi_ip 层级的可移植胶水」——它不依赖某个具体板卡的 `system.bd`，只要求当前 block design 里有 `user.org:user:*` 这批自研 IP 在 IP 仓里可用。

#### 4.2.3 源码精读

**① 仓库里的层级 Tcl 就是一份「导出脚本」。** 文件头明确写道它是 Vivado 基于某设计生成的脚本（`generated script based on design: system`），用途是帮助学习 IP Integrator Tcl：

[ip/openwifi_ip_ultra_scale.tcl:2-8](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip_ultra_scale.tcl#L2-L8)（PS8 版本，注释里同样有这句）。

**② 它定义了 README 第 3 步所需的 `create_hier_cell_*` 过程。** proc 名与 README 的 `create_hier_cell_hier_mig` 同构：

[ip/openwifi_ip_ultra_scale.tcl:86-118](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip_ultra_scale.tcl#L86-L118) 关键片段（校验父单元是层级块 → 进入父实例 → 建子层级 → 切到子实例）：

```tcl
# Hierarchical cell: openwifi_ip
proc create_hier_cell_openwifi_ip { parentCell nameHier } {
  ...
  set parentObj [get_bd_cells $parentCell]
  ...
  set parentType [get_property TYPE $parentObj]
  if { $parentType ne "hier" } { ... }     ;# 父必须是 hier 类型
  set oldCurInst [current_bd_instance .]
  current_bd_instance $parentObj
  set hier_obj [create_bd_cell -type hier $nameHier]
  current_bd_instance $hier_obj
```

这正是 UG994 描述的「在 parentCell 下创建名为 nameHier 的层级单元」的标准范式。proc 内后续几百行（创建接口针脚、实例化各 IP、连 `connect_bd_net`）就是把 openwifi_ip 内部连线「一键复刻」出来。

**③ 文件末尾还贴心地打印了可用过程清单**，提示用户去调用它：

[ip/openwifi_ip_ultra_scale.tcl:419-428](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip_ultra_scale.tcl#L419-L428)

```tcl
proc available_tcl_procs { } {
   ...
   puts "#    create_hier_cell_openwifi_ip parentCell nameHier"
   ...
}
available_tcl_procs
```

**④ Method 1 的官方描述（对照阅读）。** README 把两种方法并列写出：

[README.md:158-183](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L158-L183) 节选 Method 2 的核心三行命令：

```
- Use write_bd_tcl to write openwifi_ip Hierarchy to a .tcl
  write_bd_tcl -hier_blks [get_bd_cells /hier_mig] ./mig_hierarchy.tcl
- Create/open the new (or your own) ADI HDL reference design ..., then:
  source ./mig_hierarchy.tcl
  create_hier_cell_hier_mig / my_new_hierarchy
```

README 还提醒：openwifi 是叠加在 ADI HDL 参考设计之上的，通用问题先查 ADI wiki（[README.md:192](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L192)）。

#### 4.2.4 代码实践

> **实践目标**：不依赖具体板卡，理解用 `create_hier_cell_openwifi_ip` 重建层级的调用形式。

1. 打开 [ip/openwifi_ip_ultra_scale.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip_ultra_scale.tcl)，定位 `proc create_hier_cell_openwifi_ip`（L87），通读它「校验父单元 → 建层级 → 切实例」的前 30 行。
2. 对照 README Method 2 的三行命令（[README.md:175-182](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L175-L182)），把 `hier_mig` 替换成 `openwifi_ip`，写出 openwifi 版的迁移命令序列。
3. **预期结果（答案）**：

   ```tcl
   # 在旧工程导出（仓库已替你做好，即 ip/openwifi_ip_ultra_scale.tcl）
   # write_bd_tcl -hier_blks [get_bd_cells /openwifi_ip] ./openwifi_ip_ultra_scale.tcl

   # 在新 ADI 参考设计里
   source ./openwifi_ip_ultra_scale.tcl
   create_hier_cell_openwifi_ip / openwifi_ip
   ```

4. **现象观察**：proc 内部第一步会检查 `parentObj` 的 `TYPE` 是否为 `hier`。这说明调用前当前 `current_bd_instance` 必须是顶层（顶层 block design 的根天然是 hier）。若不满足，会打印 `Parent ... has TYPE = ... Expected to be <hier>` 并 `return`——这是迁移时最常见的报错之一，务必留意。
5. 完整跑通需 Vivado 2022.2 与已 `create_ip_repo.sh` 生成的 `ip_repo/`（保证 IP 仓里有自研 IP）。**待本地验证。**

#### 4.2.5 小练习与答案

**练习 1**：Method 1 与 Method 2 各适合什么场景？

> **答案**：Method 1（让 Vivado 自动升级旧工程再 diff）适合只升 Vivado 小版本、底座改动小的场景；Method 2（从新 ADI 参考设计起步，`source` 层级 Tcl）适合换板卡、换大版本、或要在自研底座上加 openwifi 的场景，因为它是「只搬 openwifi_ip 这一个层级」，与底座解耦。

**练习 2**：为什么 `create_hier_cell_openwifi_ip` 在动手创建层级前要先 `current_bd_instance $parentObj`？

> **答案**：Vivado 的 `create_bd_cell`、`create_bd_pin`、`connect_bd_net` 等命令都是相对「当前实例」操作的。只有先切到父实例，新建的子层级和它内部的针脚/连线才会挂到正确的父节点下；这也是 proc 末尾 `current_bd_instance $oldCurInst` 要恢复原实例的原因。

---

### 4.3 UltraScale 适配：PS7 与 PS8 两套 Tcl 的差异

#### 4.3.1 概念说明

`ultra_scale_flag` 在 4.1 里只是查表里的一个 0/1，但它真正的作用是 **分流到两套不同的 Tcl**：

- `ultra_scale_flag==0`（经典 Zynq / PS7）：用 `ip/openwifi_ip.tcl` + `ip/connect_openwifi_ip.tcl`。
- `ultra_scale_flag==1`（UltraScale+ / PS8，目前仅 zcu102）：用 `ip/openwifi_ip_ultra_scale.tcl` + `ip/connect_openwifi_ip_ultra_scale.tcl`。

为什么需要两套？因为 PS7 与 PS8 在三个层面不一样：

1. **处理器系统的 AXI 端口命名**：PS7 是 `sys_ps7/S_AXI_ACP`、`S_AXI_HP3`、`M_AXI_GP1`、时钟 `FCLK_CLK2`；PS8 是 `sys_ps8/S_AXI_ACP_FPD`、`S_AXI_HP3_FPD`、`M_AXI_HPM0_FPD`、时钟 `pl_clk2`（`_FPD` = Full Power Domain）。
2. **DMA 数据位宽与互连规模**：PS8 版用了 128 bit 的 DMA 数据通路、`axi_interconnect_1` 有 8 个主口（多一路给 side_ch）；PS7 版是 64 bit、7 个主口。
3. **IP 列表与中断拼接**：PS8 版层级内含全部 6 个自研 IP（含 side_ch）并自带 `xlconcat`；PS7 版只含 5 个（无 side_ch），中断由顶层 `connect_openwifi_ip.tcl` 一条条接到 `sys_concat_intc`。

此外，仓库还提供了一个「批量生成器」[ip/ultra_scale_tcl_gen.sh](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/ultra_scale_tcl_gen.sh)：它扫描所有 Tcl，凡是以 zc706（`xc7z045ffg900-2` + `zc706:part0:1.4`）为基线的，就 sed 替换成 zcu102 器件，自动产出 `*_ultra_scale.tcl`。这是「从 PS7 基线衍生 PS8 版本」的半自动化捷径，也解释了为什么仓库里会有一堆 `*_ultra_scale.tcl` 文件。

#### 4.3.2 核心流程

```
                   ultra_scale_flag (来自 parse_board_name.tcl)
                          │
            ┌─────────────┴─────────────┐
            ▼ 0 (PS7)                   ▼ 1 (PS8, zcu102)
   openwifi_ip.tcl               openwifi_ip_ultra_scale.tcl
   connect_openwifi_ip.tcl       connect_openwifi_ip_ultra_scale.tcl
            │                           │
            │  5 个自研 IP              │  6 个自研 IP (含 side_ch)
            │  DMA 64bit, NUM_MI=7      │  DMA 128bit, NUM_MI=8
            │  中断由 connect 脚本      │  中断在层级内用 xlconcat 拼好
            │  逐条接 sys_concat_intc   │  connect 脚本里中断行被注释
            ▼                           ▼
          sys_ps7.*                   sys_ps8.*_FPD, pl_clk2
```

辅助生成关系：

```
ultra_scale_tcl_gen.sh  --sed-->  把 zc706 基线 Tcl 衍生为 *_ultra_scale.tcl
   (xc7z045ffg900-2 → xczu9eg-ffvb1156-2-e)
   (xilinx.com:zc706:part0:1.4 → xilinx.com:zcu102:part0:3.1)
```

#### 4.3.3 源码精读

**① 两套层级 Tcl 的 IP 列表对比（5 vs 6）。** PS8 版的 IP 校验清单含 `side_ch` 和 `xlconcat`：

[ip/openwifi_ip_ultra_scale.tcl:46-57](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip_ultra_scale.tcl#L46-L57)

```tcl
set list_check_ips "\
xilinx.com:ip:axi_dma:7.1\
user.org:user:openofdm_rx:1.0\
user.org:user:openofdm_tx:1.0\
user.org:user:rx_intf:1.0\
user.org:user:side_ch:1.0\        ;# ← PS8 多了 side_ch
xilinx.com:ip:proc_sys_reset:5.0\
user.org:user:tx_intf:1.0\
xilinx.com:ip:xlconcat:2.1\       ;# ← PS8 自带中断拼接器
xilinx.com:ip:xlslice:1.0\
user.org:user:xpu:1.0\
"
```

对照 PS7 版 [ip/openwifi_ip.tcl:46-55](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L46-L55)——里面**没有** `side_ch`、也**没有** `xlconcat`。这就是 u2-l2 提到的「普通 Zynq 版实例化 5 个自研 IP，UltraScale+ 版含全部 6 个」的源头。

> 这也回应了任务里的一个问题——**IP 列表的差异（5 vs 6）由 PS7/PS8 之分（即 `ultra_scale_flag`）决定，而不是 `fpga_size_flag`。** `fpga_size_flag` 只改资源深度，不改 IP 数量。

**② DMA 数据位宽与互连规模差异。** PS8 版 DMA 用 128 bit、40 位地址：

[ip/openwifi_ip_ultra_scale.tcl:166-176](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip_ultra_scale.tcl#L166-L176)

```tcl
set_property -dict [ list \
 CONFIG.c_addr_width {40} \
 CONFIG.c_include_mm2s_dre {1} \
 ...
 CONFIG.c_m_axi_mm2s_data_width {128} \
 CONFIG.c_m_axis_mm2s_tdata_width {64} \
 ...
```

PS7 版则是 64 bit（[ip/openwifi_ip.tcl:147-156](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L147-L156) 里 `c_m_axi_mm2s_data_width {64}`）。寄存器互连的主口数也对应不同：PS8 是 `NUM_MI {8}`（[ip/openwifi_ip_ultra_scale.tcl:202-205](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip_ultra_scale.tcl#L202-L205)），PS7 是 `NUM_MI {7}`（[ip/openwifi_ip.tcl:181-184](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L181-L184)）——多出的一路 `M07_AXI` 正是给 side_ch 的寄存器口。

**③ 版本标记不同（Vivado 版本痕迹）。** PS8 版锁 2022.2：

[ip/openwifi_ip_ultra_scale.tcl:23-28](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip_ultra_scale.tcl#L23-L28)

```tcl
set scripts_vivado_version 2022.2
...
   catch {common::send_gid_msg ... "This script was generated using Vivado <$scripts_vivado_version> ..."
```

而 PS7 基线版仍写着较老的 2018.3（[ip/openwifi_ip.tcl:23](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L23)）。这也佐证了 git 提交 `3281788 Move to Vivado 2022.2 for openwifi_ip_ultra_scale.tcl`——PS8 版被单独升级到了 2022.2。

**④ 两套接线脚本的 PS 端口命名差异。** PS8 版接的是 `_FPD` 端口、用 `pl_clk2`：

[ip/connect_openwifi_ip_ultra_scale.tcl:1-4](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/connect_openwifi_ip_ultra_scale.tcl#L1-L4)

```tcl
connect_bd_intf_net -boundary_type upper [get_bd_intf_pins openwifi_ip/M00_AXI]  [get_bd_intf_pins sys_ps8/S_AXI_ACP_FPD]
connect_bd_intf_net -boundary_type upper [get_bd_intf_pins openwifi_ip/M00_AXI1] [get_bd_intf_pins sys_ps8/S_AXI_HP3_FPD]
...
connect_bd_intf_net -boundary_type upper [get_bd_intf_pins openwifi_ip/S00_AXI]  [get_bd_intf_pins sys_ps8/M_AXI_HPM0_FPD]
```

时钟统一接 `pl_clk2`（[ip/connect_openwifi_ip_ultra_scale.tcl:11-14](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/connect_openwifi_ip_ultra_scale.tcl#L11-L14)）。对比 PS7 版接的是无后缀的 `sys_ps7` 端口、用 `FCLK_CLK2`（[ip/connect_openwifi_ip.tcl:1-2](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/connect_openwifi_ip.tcl#L1-L2)、[L19-22](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/connect_openwifi_ip.tcl#L19-L22)）。这正是 u2-l3 讲过的「寄存器/DMA/中断」三类通路在 PS8 上的端口名变化。

**⑤ 中断拼接方式的差异（最微妙的一点）。** PS7 版的接线脚本会**逐条**删除旧中断网、再重连到 `sys_concat_intc` 的各位：

[ip/connect_openwifi_ip.tcl:23-36](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/connect_openwifi_ip.tcl#L23-L36) 节选：

```tcl
delete_bd_objs [get_bd_nets ps_intr_04_1]
connect_bd_net [get_bd_pins sys_concat_intc/In4] [get_bd_pins openwifi_ip/tx_itrpt0]
...
connect_bd_net [get_bd_pins sys_concat_intc/In1] [get_bd_pins openwifi_ip/rx_pkt_intr]
```

而 PS8 版里这些中断重连行**全部被注释掉了**：

[ip/connect_openwifi_ip_ultra_scale.tcl:16-29](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/connect_openwifi_ip_ultra_scale.tcl#L16-L29)

```tcl
# delete_bd_objs [get_bd_nets ps_intr_04_1]
# connect_bd_net [get_bd_pins sys_concat_intc_0/In4] [get_bd_pins openwifi_ip/tx_itrpt0]
...
# connect_bd_net [get_bd_pins sys_concat_intc_0/In1] [get_bd_pins openwifi_ip/rx_pkt_intr]
```

原因正是 ①里看到的——PS8 版的 openwifi_ip 层级**内部已经用 `xlconcat_0`/`xlconcat_1` 把多路中断拼成了少数几根**（如 `rx_pkt_intr`、`s2mm_introut`），层级对外暴露的已是拼接后的中断线，于是顶层不再需要逐位重连。这是 PS7/PS8 两版在设计上的一个本质区别。

**⑥ 批量衍生器 `ultra_scale_tcl_gen.sh`。** 这个 bash 脚本扫描仓库所有 Tcl，把「以 zc706 为基线」的脚本自动 sed 成 zcu102 版：

[ip/ultra_scale_tcl_gen.sh:5-23](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/ultra_scale_tcl_gen.sh#L5-L23) 核心：

```bash
for i in $(find . -name '*.tcl'); do
    ...
    if grep -q "xilinx.com:zc706:part0:1.4" "$i" && grep -q "xc7z045ffg900-2" "$i" ; then
        ...
        cp "$i" $filename_new
        sed -i 's/xc7z045ffg900-2/xczu9eg-ffvb1156-2-e/g' $filename_new
        sed -i 's/"xilinx.com:zc706:part0:1.4"/"xilinx.com:zcu102:part0:3.1"/g' $filename_new
```

它有两个保护条件（[ip/ultra_scale_tcl_gen.sh:9](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/ultra_scale_tcl_gen.sh#L9)）：跳过目录层级不对的、以及已经是 `*_ultra_scale.tcl` 的（避免重复衍生）。这便是「半自动生成 PS8 Tcl」的工具——它只换器件型号与板卡 VLNV，其余结构沿用 PS7 基线，因此两套文件高度相似、差异集中在 ①~⑤ 那几点。

#### 4.3.4 代码实践

> **实践目标**：用一张表把 PS7 与 PS8 两套 Tcl 的差异钉死，并验证 `ultra_scale_tcl_gen.sh` 的替换规则。

1. **对照阅读**：左右开 [ip/openwifi_ip.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl) 与 [ip/openwifi_ip_ultra_scale.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip_ultra_scale.tcl)，按下表逐项核对：

   | 维度 | PS7 `openwifi_ip.tcl` | PS8 `openwifi_ip_ultra_scale.tcl` |
   |------|------------------------|-----------------------------------|
   | Vivado 版本标记 | 2018.3（L23） | 2022.2（L23） |
   | 自研 IP 数 | 5（无 side_ch） | 6（含 side_ch） |
   | `xlconcat` | 无（层级内不拼中断） | 有（层级内拼好中断） |
   | `axi_interconnect_1` NUM_MI | 7（L183） | 8（L204） |
   | DMA `c_m_axi_mm2s_data_width` | 64（L151） | 128（L171） |
   | PS 端口示例 | `sys_ps7/S_AXI_ACP` | `sys_ps8/S_AXI_ACP_FPD` |
   | 时钟 | `FCLK_CLK2` | `pl_clk2` |

2. **验证衍生规则**：打开 [ip/ultra_scale_tcl_gen.sh](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/ultra_scale_tcl_gen.sh#L13-L22)，确认它只在文件**同时**含 `xilinx.com:zc706:part0:1.4` 与 `xc7z045ffg900-2` 时才动手，且只替换这两个字符串。
3. **现象/预期**：因为衍生器只换器件/板 VLNV、不换 IP 列表与 DMA 位宽，所以**真正可用的 PS8 层级 Tcl（`openwifi_ip_ultra_scale.tcl`）是手工在衍生结果上再改了 IP 列表/DMA/xlconcat 的**——这也是它和「纯 sed 衍生产物」会有 IP 数量差别的根因。理解这一点能避免误以为「跑一遍 `ultra_scale_tcl_gen.sh` 就能得到完整 PS8 设计」。
4. 若有 zcu102 硬件与 Vivado 2022.2，可在 `boards/zcu102_fmcs2/` 下跑 `../create_ip_repo.sh $XILINX_DIR`，观察其 `src/system.bd` 是否正是基于 PS8 端口（`sys_ps8`、`_FPD`、`pl_clk2`）连线的。**待本地验证。**

#### 4.3.5 小练习与答案

**练习 1**：PS8 版 `connect_openwifi_ip_ultra_scale.tcl` 为什么把一堆中断重连行注释掉，而 PS7 版却必须保留？

> **答案**：PS8 版的 openwifi_ip 层级内部已用 `xlconcat_0/xlconcat_1` 把众多子中断拼成了少数几根（如 `rx_pkt_intr`），对外暴露的已是拼接后的中断线，顶层无需再逐位重连到 `sys_concat_intc_0`；PS7 版层级内没有 xlconcat，中断是散的，必须由顶层接线脚本逐条 `delete_bd_objs` + `connect_bd_net` 接到 `sys_concat_intc/In1..In7`。

**练习 2**：`ultra_scale_tcl_gen.sh` 能否仅凭自身生成完整可用的 `openwifi_ip_ultra_scale.tcl`？为什么？

> **答案**：不能。它只做器件型号（`xc7z045ffg900-2`→`xczu9eg-ffvb1156-2-e`）与板 VLNV 的字符串替换，不会把 side_ch、xlconcat、128bit DMA、`NUM_MI 8`、`_FPD` 端口这些结构性差异加进去。这些是人工在衍生结果上进一步修改的。它更适合衍生「结构与 PS7 几乎相同、只换器件」的辅助 Tcl，而不是 openwifi_ip 这种有结构性差异的核心层级。

**练习 3**：如果要支持一块「UltraScale+ 的新板」（比如 RFSoC），按本讲思路要动哪些地方？

> **答案**：(a) 在 `parse_board_name.tcl` 加分支，设 `ultra_scale_flag 1`、正确的 `part_string`/`board_part_string`、`fpga_size_flag`；(b) 以 `openwifi_ip_ultra_scale.tcl` + `connect_openwifi_ip_ultra_scale.tcl` 为模板（必要时用 `write_bd_tcl` 重新导出），把 PS8 端口名改成新板实际的名字；(c) 用 README Method 2 把 openwifi_ip 层级 `source` 进新板的 ADI 参考设计；(d) 在 `boards/<新板名>/` 建目录与 `system_top.v`/`system.xdc`/`set_files.tcl`，并跑 `create_ip_repo.sh` 验证。

## 5. 综合实践

**任务：为一块假想的新板 `zcu102_fmcs8`（zcu102 底板 + 八通道射频，仍为 UltraScale+）梳理完整的移植清单，并指出 `fpga_size_flag` 与 `ultra_scale_flag` 在每个环节的作用。**

请按下列步骤完成（以源码阅读 + 写迁移清单为主，不要求真实编译）：

1. **板名登记**：在 [ip/parse_board_name.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/parse_board_name.tcl) 里仿照 `zcu102_fmcs2` 分支（L13-18）写出 `zcu102_fmcs8` 分支。`part_string` 应与 zcu102 相同（`xczu9eg-ffvb1156-2-e`），`ultra_scale_flag` 与 `fpga_size_flag` 各取何值？写出你的选择与理由。
   > 参考答案：同为 UltraScale+ 大器件，故 `ultra_scale_flag 1`、`fpga_size_flag 1`（与 zcu102 一致）。

2. **选 Tcl**：说明该板应选哪一套层级/接线 Tcl，并列出三条 PS8 特征（`_FPD` 端口、`pl_clk2`、层级内 xlconcat）作为判断依据。
   > 参考答案：选 `openwifi_ip_ultra_scale.tcl` + `connect_openwifi_ip_ultra_scale.tcl`。

3. **迁移路径**：用 README Method 2（[README.md:171-183](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/README.md#L171-L183)）写出在新 ADI 参考设计中塞入 openwifi_ip 的三行 Tcl 命令（把示例里的 `hier_mig` 替换成 openwifi 的名字）。

4. **宏落地核对**：说明由于 `fpga_size_flag==1`，[boards/ip_repo_gen.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl#L38-L43) 与 [boards/openwifi.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/openwifi.tcl#L21-L27) 不会生成哪两个宏，对应 meaning 是「不裁剪 DMA FIFO 与 side_ch BRAM 深度」。
   > 参考答案：不生成 `` `define SMALL_FPGA 1`` 与 `` `define SIDE_CH_LESS_BRAM 1``。

5. **自检**：回头确认——本任务里 `fpga_size_flag` 改变了 IP 列表吗？改变了资源深度吗？`ultra_scale_flag` 又决定了什么？
   > 参考答案：`fpga_size_flag` 只改资源深度（SMALL_FPGA/SIDE_CH_LESS_BRAM），不改 IP 列表；`ultra_scale_flag` 决定走 PS7 还是 PS8 那套 Tcl（从而影响 IP 数 5/6、DMA 位宽、端口命名、中断拼接方式）。

完成这份清单后，你就把本讲的三个最小模块（板名解析、层级迁移、UltraScale 适配）串成了一条完整的「新增一块 UltraScale+ 板卡」的工作流。

## 6. 本讲小结

- **板名解析是单一事实源**：`BOARD_NAME`（取自当前目录名）经 [ip/parse_board_name.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/parse_board_name.tcl) 查表得到 `part_string`、`fpga_size_flag`、`ultra_scale_flag` 等，被顶层工程、IP 仓、单 IP 三处 `source` 共享。
- **两个标志分工明确**：`fpga_size_flag` 管「砍不砍资源」（`SMALL_FPGA`、`SIDE_CH_LESS_BRAM`），`ultra_scale_flag` 管「走 PS7 还是 PS8 那套 Tcl」；二者彼此独立（zc706 就是大器件但非 UltraScale）。
- **层级迁移有官方两法**：Method 1 让 Vivado 自动升级再 diff；Method 2 用 `write_bd_tcl -hier_blks` 导出 openwifi_ip 层级，到新设计里 `source` + `create_hier_cell_openwifi_ip` 重建——仓库里的 `openwifi_ip*.tcl` 就是这条 proc 的一份手工维护版。
- **PS7 与 PS8 两套 Tcl 差异集中在**：IP 数（5 vs 6，关键看 side_ch）、DMA 位宽（64 vs 128）、`NUM_MI`（7 vs 8）、PS 端口（`sys_ps7/*` vs `sys_ps8/*_FPD`、`FCLK_CLK2` vs `pl_clk2`）、中断拼接（PS7 顶层逐位接、PS8 层级内 xlconcat 拼好故接线行被注释）。
- **`ultra_scale_tcl_gen.sh` 是半自动衍生器**：只替换器件型号与板 VLNV，不处理结构性差异，因此不能单靠它生成完整 PS8 核心层级，结构改动仍需手工。
- **移植一块新 UltraScale+ 板的清单**：改 `parse_board_name.tcl` → 选 `*_ultra_scale.tcl` → 用 Method 2 把层级 source 进新 ADI 设计 → 建 `boards/<新板>/` 目录与约束 → 跑 `create_ip_repo.sh` 验证。

## 7. 下一步学习建议

- **接着学可观测性**：本讲的 PS8 版多了 side_ch 这一可观测通路，建议进入 u6-l1 看 side_ch 如何捕获 CSI/RSSI/IQ 并经 DMA 上报。
- **深入寄存器契约**：移植完成后，PS8 多出的 `M07_AXI`（给 side_ch）等寄存器口如何映射成软件可见的地址，详见 u7-l1「AXI 寄存器映射与软件交互」。
- **条件编译进阶**：本讲的 `SMALL_FPGA`/`SIDE_CH_LESS_BRAM` 是板级派生宏的代表，完整的宏体系（含 `*_ENABLE_DBG`、`HAS_SIDE_CH` 等）见 u7-l2。
- **动手目标**：若你有 zcu102 或其它 UltraScale+ 板，可尝试按「综合实践」的清单真的建一个 `boards/zcu102_fmcs8/`（或你自己的板名）目录，跑到 `create_ip_repo.sh` 不报错为止——那是检验你是否真正掌握本讲的最好方式。
