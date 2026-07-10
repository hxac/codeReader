# 重定时与 eperl 流水线插件

## 1. 本讲目标

本讲聚焦 NVDLA 仓库里两类「不太像业务逻辑」的代码：`vmod/nvdla/retiming/` 下的 `RT_` 流水寄存器模块，以及 `vmod/plugins/` 下的 eperl 代码生成插件。学完后你应该能够：

- 说清楚 `NV_NVDLA_RT_*` 模块是什么：它们是夹在两个引擎之间的「流水寄存器包装」，用来给跨分区长线做重定时（retiming），帮助时序收敛。
- 读懂 eperl 预处理器的工作方式：它在源码注释里识别内嵌 Perl 脚本，执行后把生成的 Verilog 回填到文件里。
- 区分并复述三大插件 `flop`、`pipe`、`retime` 各自生成什么样的 RTL：单个触发器、valid/ready 气泡坍缩流水级、多级移位重定时。
- 理解「模板化生成」对时序收敛与代码维护的意义，并能对照真实源码追踪一条 CSC→CMAC 通路是如何被自动插入流水寄存器的。

本讲依赖 [u6-l1 时钟域、复位与时钟门控](u6-l1-clock-reset-car.md)（核心时钟 `nvdla_core_clk`、复位 `nvdla_core_rstn` 的概念）。

## 2. 前置知识

- **流水线与流水寄存器（pipeline register / flop）**：组合逻辑算完一拍结果后，用一个触发器（flip-flop，简称 flop）把结果「锁住」一拍再传给下一级。插入 flop 可以把一条很长的组合路径切成几段，每段都能在一个时钟周期内跑完。
- **时序收敛（timing closure）**：综合后，每条逻辑路径的延时必须小于时钟周期，否则报告违例（violation）。路径越长、扇出越大，越难收敛。
- **重定时（retiming）**：在不改变功能的前提下，把寄存器位置前后移动、或在模块边界上「补」几级寄存器，来切短关键路径。NVDLA 用专门的 `RT_` 模块在引擎之间补寄存器。
- **valid/ready 握手与气泡（bubble）**：NVDLA 内部数据通道普遍用 `valid`（数据有效）+ `ready`（接收方就绪）握手。一次朴素的单级流水会在「接收后但还发不出去」时插入一个空拍（气泡），降低吞吐；「气泡坍缩（bubble collapse）」是一种让流水级在自身空闲时立即接收下一拍的技巧。
- **eperl（embedded Perl）**：把 Perl 脚本写进源码注释里、由预处理器执行并回填结果的一种代码生成手段。它在 NVDLA 里被用来批量生产结构高度重复的 RTL。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [vmod/nvdla/retiming/NV_NVDLA_RT_csc2cmac_a.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/retiming/NV_NVDLA_RT_csc2cmac_a.v) | CSC→CMAC（A 半）权重通路的两级重定时模块 |
| [vmod/nvdla/retiming/NV_NVDLA_RT_cmac_a2cacc.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/retiming/NV_NVDLA_RT_cmac_a2cacc.v) | CMAC（A 半）→CACC 部分和通路的两级重定时模块（带 mask 门控） |
| [vmod/nvdla/retiming/NV_NVDLA_RT_sdp2nocif.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/retiming/NV_NVDLA_RT_sdp2nocif.v) | SDP↔MCIF/CVIF 请求/响应通路的气泡坍缩流水级集合 |
| [vmod/plugins/eperl.pm](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/plugins/eperl.pm) | eperl 插件包入口，加载 flop/pipe/retime/assert |
| [vmod/plugins/flop.pm](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/plugins/flop.pm) | 生成单个触发器的插件 |
| [vmod/plugins/pipe.pm](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/plugins/pipe.pm) | 生成 valid/ready 气泡坍缩流水级的插件 |
| [vmod/plugins/retime.pm](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/plugins/retime.pm) | 生成多级移位重定时的插件 |
| [tools/bin/eperl](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/eperl) | eperl 预处理器主程序 |
| [tools/make/vmod_common.make](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/make/vmod_common.make) | 调用 eperl 的构建规则 |

补充事实：`vmod/nvdla/retiming/` 目录下一共有 **8 个** `RT_` 模块：`csc2cmac_a`、`csc2cmac_b`、`cmac_a2cacc`、`cmac_b2cacc`、`csb2cmac`、`csb2cacc`、`cacc2glb`、`sdp2nocif`。它们都被例化在中央枢纽 [vmod/nvdla/top/NV_NVDLA_partition_o.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_o.v) 里。

## 4. 核心概念与源码讲解

### 4.1 RT_ 重定时寄存器：模块间的流水缓冲包装

#### 4.1.1 概念说明

NVDLA 的卷积主流水线在物理上跨多个分区（回顾 [u3-l1](u3-l1-conv-pipeline-overview.md) 与 [u1-l5](u1-l5-top-rtl-partitions.md)）：CDMA/CBUF/CSC 在 `partition_c`、CMAC（ma/mb）在 `partition_m`、CACC 在 `partition_a`，而它们之间的连线又汇聚到中央枢纽 `partition_o`。这些跨分区连线动辄上百位宽、扇出很大，走线长 → 一拍内跑不完 → 时序违例。

解决办法是**重定时**：在这些长线上「补」几级流水寄存器，把一条长组合路径切成几段短路径。每个 `NV_NVDLA_RT_<src>2<dst>` 模块就是干这件事的「补寄存器包装」：

- 端口上成对出现 `*_src_*`（上游来）与 `*_dst_*`（往下游去）信号；
- 内部把每根信号延时若干拍（典型 2 级，命名后缀 `_d0/_d1/_d2`）；
- 对带握手的通道，用气泡坍缩保证补寄存器后吞吐不降。

注意：`RT_` 模块**不改变功能数据**，只增加延时。它纯粹是时序优化用的「垫片」。

#### 4.1.2 核心流程

一个 `RT_` 模块的执行过程可以概括为：

1. **声明同名 src/dst 端口对**：上游信号进 `*_src_*`，下游信号出 `*_dst_*`。
2. **逐级打拍**：用 `_d0`（直通组合别名）→ `_d1`（第 1 拍寄存器）→ `_d2`（第 2 拍寄存器）形成移位链。
3. **控制信号复位置位**：`pvld`（有效）类控制位带异步复位（复位时清 0，避免上电误发有效）；数据位不带复位（节省面积，且仅在有效时才采样）。
4. **可选门控**：对宽总线，按「该拍这块数据是否真的有效」做按段时钟使能（clock enable），只翻转有用的触发器以省功耗。
5. **末端 assign**：把最后一级寄存器（如 `_d2`）连到 `*_dst_*` 输出。

#### 4.1.3 源码精读

先看 `NV_NVDLA_RT_csc2cmac_a` 的端口：它对 CSC 送给 CMAC（A 半）的权重 `sc2mac_wt` 通路做重定时，包含一个有效位、一个 128 位 mask、以及 80 条 8 位权重数据。

端口声明与首条 src 信号（注意 `sc2mac_wt_src_pvld`、`sc2mac_wt_src_mask`、`sc2mac_wt_src_data0` 等 src 端口）：[NV_NVDLA_RT_csc2cmac_a.v:11-16](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/retiming/NV_NVDLA_RT_csc2cmac_a.v#L11-L16) —— 这里 `module NV_NVDLA_RT_csc2cmac_a` 只有时钟/复位与成对的 `sc2mac_wt_src_*` / `sc2mac_wt_dst_*`。

这个模块把权重打 **2 拍**，末端把第 2 拍寄存器接到输出：

[NV_NVDLA_RT_csc2cmac_a.v:7778](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/retiming/NV_NVDLA_RT_csc2cmac_a.v#L7778) —— `assign sc2mac_wt_dst_pvld = sc2mac_wt_pvld_d2;` 说明有效位被延时到 `_d2`，即两级重定时。（80 条 8 位数据各有对应的 `_d2` assign，所以文件很长。）

再看一个更精巧的例子 `NV_NVDLA_RT_cmac_a2cacc`：它重定时 CMAC→CACC 的部分和（partial sum），8 条 176 位数据 + 8 位 mask + 8 位 mode + 9 位 pd + pvld。它对数据做了**按段门控**：

[NV_NVDLA_RT_cmac_a2cacc.v:160-179](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/retiming/NV_NVDLA_RT_cmac_a2cacc.v#L160-L179) —— 低 44 位 `data0_d1[43:0]` 仅在 `mask_d0[0]==1` 时更新；高 132 位 `data0_d1[175:44]` 仅在 `mask_d0[0] & mode_d0[0]` 时更新。含义是：176 位部分和里，低半段在任何有效通道都有效，高半段只有当 `mode` 为 1（对应更宽精度，如 INT16/FP16）才携带有效数据；INT8 模式下高半段是无效位，不翻转对应触发器，省功耗。这是 retime 插件「带时钟使能（clock enable）」的典型产物。

控制位 `pvld` 与 `mask` 带异步复位（上电清 0），数据位则只在有效时采样、不带复位：

[NV_NVDLA_RT_cmac_a2cacc.v:126-159](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/retiming/NV_NVDLA_RT_cmac_a2cacc.v#L126-L159) —— `mac2accu_pvld_d1` 在 `!nvdla_core_rstn` 时清 0、否则跟随 `_d0`；`mac2accu_mask_d1` 同样带复位清 0。这与 [u6-l1](u6-l1-clock-reset-car.md) 讲的「控制位复位、数据位不复位」原则一致。

末端输出全部来自 `_d2`（两级）：

[NV_NVDLA_RT_cmac_a2cacc.v:522-533](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/retiming/NV_NVDLA_RT_cmac_a2cacc.v#L522-L533) —— `mac2accu_dst_pvld = mac2accu_pvld_d2` 等一组 assign，把第 2 拍寄存器接到 dst 输出。

最后看 `RT_` 模块在顶层如何被例化——`NV_NVDLA_partition_o` 里在 CSC 权重输出与 CMAC 输入之间插入了它：

[NV_NVDLA_partition_o.v:2529-2534](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_o.v#L2529-L2534) —— 例化 `NV_NVDLA_RT_csc2cmac_a u_NV_NVDLA_RT_csc2cmac_a`，把 `sc2mac_wt_a_src_pvld/mask/dataN` 接到 `*_src_*` 端口；对应的 `*_dst_*` 输出再连往 CMAC。这就是「在引擎之间补流水寄存器」的实物。

#### 4.1.4 代码实践

**实践目标**：确认 `RT_csc2cmac_a` 是两级重定时，并定位它的「上下游」。

**操作步骤**：

1. 打开 [NV_NVDLA_RT_csc2cmac_a.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/retiming/NV_NVDLA_RT_csc2cmac_a.v)，搜索 `_d1` 与 `_d2`，确认存在两级寄存器（`_d0` 是组合直通别名）。
2. 在 [NV_NVDLA_partition_o.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_o.v) 中搜索 `NV_NVDLA_RT_csc2cmac_a`，找到它的例化（约第 2529 行），观察 `src_*` 来自哪个 CSC 信号、`dst_*` 去往哪个 CMAC 信号。

**需要观察的现象**：`src` 端口的信号名前缀是 `sc2mac_wt_a_*`（CSC 发给 CMAC-A 的权重），`dst` 端口前缀相同但接到 CMAC 的输入；模块内部多出 2 拍延时。

**预期结果**：该 `RT_` 模块把 CSC→CMAC_A 权重通路整体延时 2 拍，不改变数据内容，只为切短跨分区长线。结果是否在波形里能直接看到 2 拍延时「待本地验证」（需跑仿真并对比 src/dst 时刻）。

#### 4.1.5 小练习与答案

**练习 1**：`RT_cmac_a2cacc` 里为什么把每条 176 位数据拆成 `[43:0]` 和 `[175:44]` 两段、用不同条件更新？
**答案**：低 44 位在任何有效通道都携带有效部分和（`mask[i]` 为 1 即更新）；高 132 位只在 `mode[i]` 为 1（宽精度模式）时才有效。拆段更新等于按通道、按精度做时钟使能，避免在 INT8 模式下无谓翻转高半段触发器，省功耗。

**练习 2**：为什么 `pvld`/`mask` 带 `negedge nvdla_core_rstn` 复位，而数据位 `data*_d1` 的 `always` 块不带复位？
**答案**：控制位（有效、掩码）必须在复位后立刻是确定值（0），否则下游会误以为有有效数据；数据位只在「有效且掩码命中」时才被采样，复位值无关紧要，省掉复位可减少复位树扇出与面积。

---

### 4.2 eperl 预处理器：注释里跑 Perl

#### 4.2.1 概念说明

`RT_` 模块内部高度重复（80 条数据 × 2 级，每条都要写一段几乎一样的 `always`）。手写既易错又难维护。NVDLA 用 **eperl** 来批量生成这类重复 RTL。

eperl 的思路：在源文件的**注释**里写一小段 Perl，由预处理器执行，把生成的 Verilog 回填到文件里「生成区」中。这样「逻辑」（一行插件调用）和「产物」（几十行寄存器）分离，维护时只改那一行调用即可。

> 注意一个重要事实：eperl 主程序在自身用法说明里强调它「不是为构建流程设计的，应被视为一个高级的编辑器按键」。也就是说，它本意是让设计者在本地把模板展开、把生成结果连同那条调用一起**提交进仓库**。同时构建规则里也挂了一道 eperl 步骤（见 4.2.3），保证任何带内嵌脚本的源都会在编译前被展开。两者并不矛盾：日常是「编辑器按键」式展开并提交产物；构建时再兜底跑一次。

#### 4.2.2 核心流程

1. **识别内嵌脚本**：eperl 扫描源文件，遇到「注释符 + 冒号」开头的行（`//: <perl>` 或 `#: <perl>`）就把它当成 Perl 代码。相邻的多行拼成一段脚本。
2. **包裹生成区**：脚本执行前输出 `//| eperl: generated_beg (DO NOT EDIT BELOW)`，执行后输出 `//| eperl: generated_end (DO NOT EDIT ABOVE)`，生成内容夹在中间。
3. **执行插件**：脚本里通常调用 `&eperl::<plugin>("<选项>")`（如 `&eperl::flop(...)`），插件 `print` 出来的 Verilog 就成了生成内容。
4. **回填/比较**：默认输出到 stdout；`-m` 为就地修改、`-o` 为指定输出文件，且会与原文件 `cmp` 比对，有差异才覆盖。

#### 4.2.3 源码精读

主程序对自身定位的说明（强调它是「编辑器按键」式的就地生成工具）：

[tools/bin/eperl:33-35](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/eperl#L33-L35) —— 「This tool is NOT intended to be used within a build flow. This tool should be viewed as a fancy editor keystroke...」。

内嵌脚本的识别语法与生成区标记：

[tools/bin/eperl:56](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/eperl#L56) —— 注释冒号语法 `//: <perl>` 或 `#: <perl>`；[tools/bin/eperl:61-63](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/eperl#L61-L63) —— 生成区由 `generated_beg` / `generated_end` 包裹。

插件的调用方式与一个完整例子（生成一个 10 位带使能的触发器）：

[tools/bin/eperl:88-96](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/eperl#L88-L96) —— 文档给出 `&eperl::flop("-wid 10 -en enable");` 并展示它生成的 `reg [9:0] q; always @(posedge clk or negedge rst) ...`。这正是 4.3 节 `flop.pm` 的输出。

扫描循环 `ProcessInput` 的核心：逐行读取，命中 `generated_beg..end` 标记时跳过旧生成区，命中 `//: ` 时累积脚本，遇到非脚本行就把累积脚本交给 `EvalScript` 执行：

[tools/bin/eperl:178-194](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/eperl#L178-L194) —— 这是「识别脚本 + 跳过旧生成区」的关键循环；[tools/bin/eperl:204-212](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/eperl#L204-L212) —— `EvalScript` 在执行前后打印 `generated_beg/end`，并用 `eval` 跑脚本。

构建规则里挂的 eperl 步骤（兜底展开）：

[tools/make/vmod_common.make:32-38](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/make/vmod_common.make#L32-L38) —— 规则把 `.v` 经 vcp 展开（`#ifdef`）成 `.vcp`，再用 `perl -I vmod/plugins -Meperl tools/bin/eperl -o $@ $<` 跑 eperl。`-I vmod/plugins` 让 Perl 能 `use` 到 `eperl.pm` 及其加载的 `flop/pipe/retime`。

`eperl.pm` 本身只是个加载器，把插件包 `use` 进来并提供打印 helper：

[vmod/plugins/eperl.pm:8-18](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/plugins/eperl.pm#L8-L18) —— `use flop; use pipe; use retime; use assert;` 并定义 `vprintl`/`vprinti` 两个打印函数。

#### 4.2.4 代码实践

**实践目标**：亲手跑一次 eperl，看一条调用如何变成 Verilog。

**操作步骤**：

1. 新建一个临时文件 `t.v`，内容只有两行（示例代码，非项目原有文件）：

   ```verilog
   //: &eperl::flop("-wid 4 -en en -rst rst -rval 0");
   ```
2. 在仓库根目录运行（需本地装有 `perl`）：

   ```bash
   perl -I vmod/plugins -Meperl tools/bin/eperl -o t.out.v t.v
   ```

**需要观察的现象**：`t.out.v` 里出现 `//| eperl: generated_beg ...` 与 `generated_end` 包裹的一段 Verilog，含 `reg [3:0] q;` 与一个带 `en`、带异步复位的 `always` 块。

**预期结果**：生成的代码与 [tools/bin/eperl:88-113](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/eperl#L88-L113) 文档示例结构一致。若本地无 perl 环境则「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么生成区要用 `generated_beg / generated_end` 包裹，并标注「DO NOT EDIT」？
**答案**：因为这块内容是脚本自动产出的，下次再跑 eperl 会被整段替换；如果人手改了其中内容，重跑就会丢失。标记把「可手写区」与「生成区」隔离开。

**练习 2**：eperl 的 `-m`（就地修改）模式在覆盖前会做什么？
**答案**：先用 `cmp -s` 比对生成结果与原文件；只有存在差异时才覆盖（并报告 modified），无差异则删除临时文件（报告 unchanged），避免无谓改写时间戳。

---

### 4.3 flop / pipe / retime 三大插件

#### 4.3.1 概念说明

`vmod/plugins/` 下三个插件对应三种最常用的「重复 RTL 图元」：

| 插件 | 生成物 | 典型用途 |
| --- | --- | --- |
| `flop` | 单个（或一组）触发器，可选使能、复位、复位值 | 单信号打一拍、状态位寄存 |
| `pipe` | 一级 valid/ready **气泡坍缩**流水级（可选输入/输出 skid） | 给带握手的数据通道补一拍而不降吞吐 |
| `retime` | 对一条总线做 N 级移位重定时，可选时钟使能 | 给宽总线补多拍（如 `RT_` 模块） |

它们都遵循同样的风格：选项化（`-wid`、`-clk`、`-rst`、`-stage`…）、用 `print` 输出 Verilog、带 VCS 覆盖率开关注释。

#### 4.3.2 核心流程

- **flop**：读 `-d/-q/-en/-clk/-rst/-rval/-wid` → 生成 `reg [wid-1:0] q;` + 一个 `always` 块：有 `-rst` 则异步复位到 `{wid{rval}}`；有 `-en` 则在使能为 1 时采样、为 0 时保持、为 x 时写 x（覆盖率用）。
- **pipe**：核心是「气泡坍缩」。设本级 valid=`vo`、ready=`ro`，下游 ready=`ri`、上游 valid=`vi`，则：

  \[ r_{o} = r_{i} \vee \neg v_{o} \]

  即「下游就绪，或者本级空着」时本级就向上游表示可接收。这样只要本级一腾空就立刻吃进下一拍，不产生气泡，保持满吞吐。`vo` 在 `ro` 时跟随 `vi`；数据 `do` 在 `ro & vi` 时锁存。可选 `-is`（输入侧 skid）/`-os`（输出侧 skid）进一步改善某一边的时序。
- **retime**：对输入总线 `sig_i` 循环 N 级，每级生成 `reg [...] sig_i_d{k};` 与 `always @(posedge clk) if (cg_en) sig_i_d{k} <= sig_i_d{k-1};`，末级 `assign sig_o = sig_cur;`。`-cg_en_i` 提供时钟使能，`-cg_en_rtm` 决定使能信号是否随总线一起重定时。

#### 4.3.3 源码精读

**flop.pm**：选项解析与生成的 `always` 块。

[vmod/plugins/flop.pm:52-61](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/plugins/flop.pm#L52-L61) —— 选项 `-d/-q/-en/-wid/-clk/-rst/-rval`；[vmod/plugins/flop.pm:78-97](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/plugins/flop.pm#L78-L97) —— 生成的 `reg [range] q;` 与带使能/复位的 `always` 块（使能为 x 时写 `{wid{1'bx}}` 并配 `VCS coverage off/on` 注释）。这正是 [tools/bin/eperl:88-113](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/bin/eperl#L88-L113) 文档示例的来源。

**pipe.pm**：气泡坍缩的内核。

[vmod/plugins/pipe.pm:200-203](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/plugins/pipe.pm#L200-L203) —— `assign $ro = $ri || !$vo;`，即上式 \( r_o = r_i \vee \neg v_o \)。文件头部的 ASCII 图也直观对比了「普通 pipe」「-is」「-os」三种接法：[vmod/plugins/pipe.pm:33-60](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/plugins/pipe.pm#L33-L60)。

**retime.pm**：多级移位循环。

[vmod/plugins/retime.pm:85-113](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/plugins/retime.pm#L85-L113) —— `foreach my $stage (1..$stages)` 每级生成一个 `sig_i_d{k}` 寄存器与 `always @(posedge clk) if (cg_en) ... <= ...`；[vmod/plugins/retime.pm:117-118](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/plugins/retime.pm#L117-L118) —— 末级 `wire [...] sig_o; assign sig_o = sig_cur;`。选项见 [vmod/plugins/retime.pm:56-66](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/plugins/retime.pm#L56-L66)（`-i/-o/-wid/-stage/-clk/-cg_en_i/-cg_en_o/-cg_en_rtm`）。

**生成产物的实证**：`NV_NVDLA_RT_sdp2nocif.v` 是 `pipe` 插件的典型产物。它把 SDP↔MCIF/CVIF 的 8 条请求/响应通道各补一级气泡坍缩流水，每条都生成一个独立子模块：

[NV_NVDLA_RT_sdp2nocif.v:386-398](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/retiming/NV_NVDLA_RT_sdp2nocif.v#L386-L398) —— 顶部注释 `// Generated by ::pipe -m -bc -rand none ...` 标明它由 pipe 插件生成（`-bc` 即 bubble-collapse），紧接着是生成的 `module NV_NVDLA_RT_SDP2NOCIF_pipe_p1`。

该生成模块的气泡坍缩逻辑与 `pipe.pm` 的内核完全对应：

[NV_NVDLA_RT_sdp2nocif.v:414-447](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/retiming/NV_NVDLA_RT_sdp2nocif.v#L414-L447) —— `p1_pipe_ready_bc = p1_pipe_ready || !p1_pipe_valid;`（坍缩条件），`p1_pipe_valid <= (p1_pipe_ready_bc)? src_valid_d0 : 1'd1;`，`p1_pipe_data <= (p1_pipe_ready_bc && src_valid_d0)? src_pd_d0 : p1_pipe_data;`，外加断言（`ASSERT_ON` 时检查 valid 不能在 ready 前撤销、控制信号不能为 x）。

顶层把这 8 个 pipe 子模块与 4 个单拍 `lat_fifo_pop` 串联起来：

[NV_NVDLA_RT_sdp2nocif.v:210-227](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/retiming/NV_NVDLA_RT_sdp2nocif.v#L210-L227) —— `// Valid Ready Pipe` 段：先把 src 直连到 `_d0`，例化 `pipe_p1`，再把 `_d1` 接到 dst；[NV_NVDLA_RT_sdp2nocif.v:358-379](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/retiming/NV_NVDLA_RT_sdp2nocif.v#L358-L379) —— `// Valid Only Pipe` 段对 `lat_fifo_pop` 这类无握手控制位只打一拍（`pop_d1 <= pop;`），是 `flop` 的简单用法。

> 关于「插件版本」的说明：仓库里这些 `RT_*.v` 的 `Generated by ::` 注释带 `-m -bc -rand none` 等参数，而当前 `vmod/plugins/*.pm` 暴露的选项集合略有不同——说明这些产物是由（内部更完整的）同族工具生成后连同结果一起提交进仓库的，开源出来的 `*.pm` 是与它们结构一致的等价生成器。两者的气泡坍缩、多级移位、按段门控等**模式完全一致**，读 `*.pm` 即可理解产物。

#### 4.3.4 代码实践

**实践目标**：把一个插件调用「手算」出来，再与真实生成产物对照。

**操作步骤**：

1. 阅读 [vmod/plugins/pipe.pm:184-228](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/plugins/pipe.pm#L184-L228) 的 `_pipe` 子过程，在纸上推导 `pipe("-wid 8")` 会生成哪几行 Verilog（重点写出 `ro`、`vo`、`do` 三段）。
2. 打开 [NV_NVDLA_RT_sdp2nocif.v:414-447](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/retiming/NV_NVDLA_RT_sdp2nocif.v#L414-L447)，把你推导的 `ready_bc`、`valid`、`data` 三式与生成代码逐行比对。

**需要观察的现象**：推导得到的 `ro = ri || !vo`（坍缩 ready）、`vo` 在 ready 时跟随 `vi`、`do` 在 `ro & vi` 时锁存，与生成模块的 `p1_pipe_ready_bc`/`p1_pipe_valid`/`p1_pipe_data` 一一对应。

**预期结果**：生成产物就是 `pipe.pm` 内核的一个展开实例，只是多套了一层模块封装与断言。

#### 4.3.5 小练习与答案

**练习 1**：气泡坍缩为什么能保持满吞吐？用 \( r_o = r_i \vee \neg v_o \) 解释。
**答案**：只要本级没数据（\( v_o=0 \)），\( \neg v_o=1 \) 使 \( r_o=1 \)，本级立刻能接收上游下一拍；只有当本级有数据（\( v_o=1 \)）且下游不就绪（\( r_i=0 \)）时才反压。于是除真正被下游反压的情况外，本级每拍都能吃进新数据，不产生额外空泡。

**练习 2**：`retime` 的 `-cg_en_rtm` 选项影响什么？
**答案**：决定时钟使能信号本身是否随总线一起逐级重定时。若开启，使能信号也打 N 拍，保证它与数据「同延时」到达下游；若关闭（默认），使能直接 `assign` 给输出，延时少于数据，需要设计者确认这样不会错配节拍。

---

### 4.4 模板化生成的工程意义

#### 4.4.1 概念说明

把「重复 RTL」交给模板生成器，表面上是省敲键盘，真正的价值在于三点：

1. **一致性**：所有流水寄存器、所有 valid/ready 流水级长得一样，断言、覆盖率注释、复位策略统一，综合工具与 lint 工具面对的模式更整齐。
2. **可演进**：要改流水级的实现（比如换一种更省功耗的门控），只改插件一处，重新生成即可全树更新，而不是去几十个文件里逐一手改。
3. **时序收敛友好**：重定时点的数量与深度可以根据物理时序反复调整（这条线补 2 拍、那条补 1 拍），生成器让「改深度」变成改一个 `-stage` 参数。

#### 4.4.2 核心流程

工程上典型的工作链：

1. 设计者在某条跨引擎长线上发现时序违例。
2. 决定补 N 级寄存器，写一条对应的 `&eperl::retime(...)` 或直接例化 `NV_NVDLA_RT_*` 模块。
3. 用 `tools/bin/eperl -m` 在本地展开，把生成产物与调用一起提交。
4. 构建时 `vmod_common.make` 的 eperl 步骤兜底再跑一次，保证源与产物一致。
5. 综合后看时序报告，若仍违例就调 `-stage` 重来。

#### 4.4.3 源码精读

构建规则把 vcp 与 eperl 串成一条「源 → 宏展开 → Perl 展开」的流水，且依赖 `$(EPERL)` 这个变量，意味着换 eperl 实现会触发重生成：

[tools/make/vmod_common.make:32-38](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/make/vmod_common.make#L32-L38) —— `.v` → `.vcp`（vcp 展开 `#ifdef`）→ eperl 展开 `//:` 内嵌脚本；`@rm $<` 删掉中间 `.vcp`。

`RT_` 模块在顶层被当作普通模块例化，说明「生成产物」与「手写 RTL」在构建视角下没有区别，都进同一套编译流：

[NV_NVDLA_partition_o.v:2529](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_o.v#L2529) —— 例化 `NV_NVDLA_RT_csc2cmac_a`，与其它引擎模块并列。

#### 4.4.4 代码实践

**实践目标**：盘点全仓库「`RT_` 重定时 + eperl 生成」的规模，建立量化印象。

**操作步骤**：

1. 统计 `vmod/nvdla/retiming/` 下 `RT_` 模块数量（应为 8 个）。
2. 在仓库内搜索生成痕迹：`grep -rn "Generated by ::" vmod/nvdla/retiming/`（或在 [NV_NVDLA_RT_sdp2nocif.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/retiming/NV_NVDLA_RT_sdp2nocif.v) 内看），观察哪些模块由 `pipe` 生成、哪些是手写风格的重定时。

**需要观察的现象**：`sdp2nocif` 等模块带 `Generated by ::pipe` 注释；`csc2cmac_a`/`cmac_a2cacc` 等模块没有该注释但结构同样是多级 retime（属同族工具产物）。

**预期结果**：能区分「明显由 pipe 生成」与「retime 风格」两类，并指出它们都服务于跨分区时序收敛这一共同目标。

#### 4.4.5 小练习与答案

**练习 1**：如果某条线综合后仍时序违例，在「模板化生成」框架下怎么调整？
**答案**：把对应 `RT_` 模块的重定时级数加深（如 2 级改 3 级），或在源模板里把 `retime` 的 `-stage` 调大，重新跑 eperl 生成、重新综合。改动集中在生成参数，不必逐信号手改。

**练习 2**：为什么把 eperl 生成产物也提交进仓库，而不是只在构建时生成？
**答案**：让仓库自带确定性产物，构建无需依赖设计者本地环境即可复现；同时便于 code review 直接看最终 RTL、便于追溯（git blame 生成结果）。构建里的 eperl 步骤只做兜底校验。

## 5. 综合实践

把本讲四块知识串起来，追踪一条完整的「CSC→CMAC_A 权重重定时通路」：

1. **定位通路**：在 [NV_NVDLA_partition_o.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_o.v) 找到 `NV_NVDLA_RT_csc2cmac_a` 的例化（约第 2529 行），确认它的 `sc2mac_wt_src_*` 来自 CSC、`*_dst_*` 去往 CMAC_A。
2. **判断级数**：在 [NV_NVDLA_RT_csc2cmac_a.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/retiming/NV_NVDLA_RT_csc2cmac_a.v) 中确认是两级（`_d0→_d1→_d2`，输出取 `_d2`，见第 7778 行）。
3. **归类插件**：判断它属于 `retime`（多级移位 + 可选门控）一类的产物，对照 [retime.pm:85-113](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/plugins/retime.pm#L85-L113) 的逐级生成循环理解其结构。
4. **解释生成链**：用一句话说明这条通路如何由「一条 `&eperl::retime(...)` 风格调用 → `tools/bin/eperl` 展开 → 生成 `RT_` 模块 → 在 `partition_o` 例化 → 经 [vmod_common.make:32-38](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/tools/make/vmod_common.make#L32-L38) 编译」最终落地。
5. **思考改进**：若该线仍违例，你会把级数从 2 改成几？改动点在哪一处参数？（答案：改 `RT_` 模块内 retime 级数 / 生成调用里的 stage 参数，无需改业务逻辑。）

完成后再对比 [NV_NVDLA_RT_sdp2nocif.v](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/retiming/NV_NVDLA_RT_sdp2nocif.v)（`pipe` 生成、带握手）与 `csc2cmac_a`（`retime` 风格、宽总线）的差异：前者保吞吐、后者补深度，二者都为时序收敛服务。

## 6. 本讲小结

- `NV_NVDLA_RT_*` 是夹在引擎之间的「流水寄存器垫片」，专为跨分区长线做重定时、帮助时序收敛，不改功能数据只加延时。
- 典型 `RT_` 模块做两级重定时（`_d0→_d1→_d2`）：控制位（pvld/mask）带异步复位，数据位只在有效时采样；宽总线还做按段时钟使能省功耗（如 `cmac_a2cacc` 的高 132 位仅 `mask&mode` 时更新）。
- eperl 是「注释里跑 Perl」的预处理器：识别 `//: <perl>` 内嵌脚本，执行后用 `generated_beg/end` 包裹回填；日常作「编辑器按键」展开并提交产物，构建规则里再兜底跑一次。
- 三大插件各管一类图元：`flop`（单触发器）、`pipe`（valid/ready 气泡坍缩流水级，\( r_o = r_i \vee \neg v_o \)）、`retime`（多级移位重定时 + 可选时钟使能）。
- 模板化生成的价值是一致性、可演进、时序收敛友好：改深度变成改一个 `-stage` 参数，全树统一更新。
- 生成产物（`RT_*.v`）与手写 RTL 在构建视角下无差别，都经 vcp→eperl→编译 同一条流水落地。

## 7. 下一步学习建议

- 顺着本讲往前，读 [u6-l2 FIFO 与 vlibs 库原语](u6-l2-fifo-vlibs-primitives.md)：vlibs 的 `sync3d`、`NV_BLKBOX` 等是与 eperl 插件并列的另一类「可复用 RTL 积木」，两者共同支撑全树一致性。
- 若关心这些重定时通路在数据流里的位置，回到 [u3-l1 卷积主流水线总览](u3-l1-conv-pipeline-overview.md) 与 [u3-l4 CSC](u3-l4-csc-slot-controller.md)，把 `RT_csc2cmac_a`/`RT_cmac_a2cacc` 摆回 CDMA→CBUF→CSC→CMAC→CACC 链路里理解。
- 想动手实验生成器，可仿照 4.2.4 在本地跑 `tools/bin/eperl`，尝试 `&eperl::pipe("-wid 16 -is")`、`&eperl::retime("-i din -o dout -wid 32 -stage 3 -cg_en_i en")`，观察不同参数如何改变产物，并与 `vmod/nvdla/retiming/` 下的真实 `RT_` 模块对照。
