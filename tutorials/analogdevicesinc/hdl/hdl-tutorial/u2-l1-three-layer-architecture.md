# 三层工程架构：载板 / 评估板 / 系统

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚一个 ADI HDL 参考设计在逻辑上由**哪三层**叠加而成，以及每一层各自负责什么。
- 解释为什么要把「载板（carrier）相关」的设计和「评估板（evaluation board）相关」的设计**分开**存放，这种分离带来了什么好处。
- 打开任意一个 `system_bd.tcl`，认出其中那两行关键的 `source` 语句，并说明它们分别对应三层中的哪一层、为何必须**先 source 载板、再 source 评估板**。
- 理解顶层 `system_wrapper` 模块其实是由这三层**共同**描述出来的。

本讲不要求你写 Verilog，重点是建立「文件放在哪里、为什么放在那里」的空间感。承接上一讲「构建第一个工程」中提到的 `system_project.tcl → system_bd.tcl` 入口，本讲把 `system_bd.tcl` 这一层彻底拆开。

## 2. 前置知识

阅读本讲前，建议你已经知道（这些在 u1 系列讲义中已建立）：

- **参考设计（reference design）**：ADI HDL 仓库最终交付的是一个能烧进 FPGA 的设计，而不是一个可执行程序。每个参考设计对应「某块 ADI 评估板 + 某块 FPGA 载板」的一个具体组合。
- **载板（carrier）**：FPGA 开发板本身，例如 Xilinx ZCU102、ZedBoard。它上面有处理器（如 Zynq 的 PS7/PS8）、DDR、电源、连接器等。载板通常**不是** ADI 制造的。
- **评估板（evaluation board / 子卡）**：ADI 制造的、插在载板上的子板，上面焊着你要评估的器件（如 AD9361 射频收发器、某款 ADC/DAC）。子卡一般通过 **FMC**（FPGA Mezzanine Card）连接器插到载板上。
- **块设计（block design，BD）**：Vivado 里用图形化方式把若干 IP 拼接、连线而成的画布；在本仓库里这块画布是用 **Tcl 脚本**描述的（`*_bd.tcl`），最终会生成一个名为 `system_wrapper` 的顶层模块。
- **`ad_hdl_dir`**：指代本仓库根目录的 Tcl 变量；`source $ad_hdl_dir/...` 就是从仓库根出发的绝对路径。

一个关键直觉：**一个载板可以插很多种子卡，一个子卡也可以插很多种载板**。如果把「每种载板 × 每种子卡」都单独写一份完整设计，组合数会爆炸。本讲要讲的三层架构，正是 ADI 用来避免这种组合爆炸的工程手法。

## 3. 本讲源码地图

本讲围绕四个文件展开，它们正好对应三层架构的三个层 + 一个把它们组装起来的入口：

| 文件 | 在三层模型中的角色 | 主要内容 |
| --- | --- | --- |
| `docs/user_guide/architecture.rst` | 官方架构说明（权威定义） | 用文字定义了三层模型、给出标准示例、列出每层应包含的文件清单 |
| `projects/common/zcu102/zcu102_system_bd.tcl` | **第一层：载板基设计** | 例化 ZynqMP 处理器 `sys_ps8`、定义时钟/复位/SPI/GPIO/中断、设置 `CACHE_COHERENCY` 等全局变量 |
| `projects/fmcomms2/common/fmcomms2_bd.tcl` | **第二层：评估板基设计** | 例化 `axi_ad9361` 及 ADC/DAC 数据通路（FIFO、pack/unpack、DMA），与载板无关 |
| `projects/fmcomms2/zcu102/system_bd.tcl` | **第三层：系统特化设计（组装入口）** | 依次 `source` 第一层、第二层，再对「fmcomms2+zcu102」这一具体组合做参数微调 |

> 提醒：`projects/common/zcu102/` 目录下还有一个 `system_bd.tcl`（不同于 `zcu102_system_bd.tcl`）。它是一个**模板骨架**（只 source 载板层，不含任何评估板），用于新建工程时复制参考，不是被别人 source 的载板基设计。本讲关注的载板基设计是 `zcu102_system_bd.tcl`。

## 4. 核心概念与源码讲解

### 4.1 三层设计模型

#### 4.1.1 概念说明

官方文档 `architecture.rst` 开篇就给出定义：**每一个参考设计的 HDL 都可以划分成三层**：

1. **载板基设计（carrier base design）**——描述「这块载板上有什么」：处理器（软核或硬核）、运行 Linux 所必需的全部外设 IP。它**与载板强绑定**（carrier dependent），存放在 `projects/common/$CARRIER/`。Zynq 的 PS 配置、SPI、I2C、GPIO 基本都在这一层完成。
2. **评估板基设计（evaluation board base design）**——描述「这块评估板（子卡）上有什么」：例化所有用来控制和该子卡收发数据所必需的 IP。它定义的数据通路**跨多种载板通用**（carrier independent），存放在 `projects/$EVAL_BOARD/common/`。
3. **系统特化设计（system specific design）**——针对「某块评估板 + 某块载板」这一**具体组合**的设计。它先把第 1 层 source 进来，再把第 2 层 source 进来，最后做一些只对当前组合有意义的参数微调。

详见官方原文对这三层的逐条定义：[docs/user_guide/architecture.rst:6-37](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/architecture.rst#L6-L37)（其中 L6-L20 是第 1 层、L22-L31 是第 2 层、L33-L37 是第 3 层）。

三层共同描述出同一个 `system_wrapper` 模块——也就是说，你最终在 Vivado 块设计画布上看到的那个庞然大物，是被这三个文件**拼**出来的，而不是写在某一个文件里的。

#### 4.1.2 核心流程

可以用「叠积木」来理解三层的关系——第三层在最上面，把下面两层压在一起：

```
┌──────────────────────────────────────────────────────┐
│ 第三层：系统特化设计   projects/fmcomms2/zcu102/       │
│   source 第一层  +  source 第二层  +  参数微调           │
├──────────────────────────────────────────────────────┤
│ 第二层：评估板基设计   projects/fmcomms2/common/        │
│   axi_ad9361 + ADC/DAC 数据通路   （carrier 无关）       │
├──────────────────────────────────────────────────────┤
│ 第一层：载板基设计     projects/common/zcu102/          │
│   sys_ps8 处理器 + 时钟/复位/SPI/GPIO   （carrier 相关） │
└──────────────────────────────────────────────────────┘
            ▲ system_bd.tcl 按 1→2→3 顺序依次叠加
```

执行时的关键约束是**顺序**：

```
进入 system_bd.tcl（第三层入口）
   │
   │ 步骤 1：source 载板基设计（第一层）
   │         → 建好 sys_ps8、sys_cpu_clk、CACHE_COHERENCY 等变量与处理器
   │
   │ 步骤 2：source 评估板基设计（第二层）
   │         → 读取第一层留下的变量（时钟、CACHE_COHERENCY）来适配连线
   │
   │ 步骤 3：执行本文件内的特化语句
   │         → system ID、ADC_INIT_DELAY 等只对这个组合生效的微调
   │
   ▼
system_wrapper 块设计组装完成 → 交给综合/实现生成比特流
```

这个「先载板、后评估板」的固定顺序，官方文档在 *How they're instantiated* 一节中明确写出了规则：[docs/user_guide/architecture.rst:39-44](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/architecture.rst#L39-L44)。

#### 4.1.3 源码精读

第三层的入口文件 `projects/fmcomms2/zcu102/system_bd.tcl` 一共只有 20 行左右，核心就是文件开头的三行 `source`：

```tcl
source $ad_hdl_dir/projects/common/zcu102/zcu102_system_bd.tcl   ;# 第一层：载板
source ../common/fmcomms2_bd.tcl                                  ;# 第二层：评估板
source $ad_hdl_dir/projects/scripts/adi_pd.tcl                    ;# 工具辅助脚本
```

参见 [projects/fmcomms2/zcu102/system_bd.tcl:6-8](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_bd.tcl#L6-L8)。注意两行 `source` 的路径写法不同，这恰好对应两层不同的定位方式：

- 第一层用**仓库根绝对路径** `$ad_hdl_dir/projects/common/zcu102/zcu102_system_bd.tcl`：载板基设计属于「公共资产」，任何评估板只要用 zcu102，都从同一个地方取它。
- 第二层用**相对路径** `../common/fmcomms2_bd.tcl`：当前文件位于 `projects/fmcomms2/zcu102/`，`../common/` 解析为 `projects/fmcomms2/common/`，即本评估板的公共目录。这种写法让评估板设计总是「就近」找到自己的第二层。

source 完两层之后，文件剩下的几行就是第三层的特化微调，例如设置 system ID、给 `util_ad9361_divclk` 指定 `SIM_DEVICE`、给 `axi_ad9361` 调整 `ADC_INIT_DELAY`，见 [projects/fmcomms2/zcu102/system_bd.tcl:10-20](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_bd.tcl#L10-L20)。

官方文档对 `system_bd.tcl` 这个文件的职责描述也与源码完全吻合——它「先 source 载板基设计，再 source 评估板基设计，然后在被 source 的文件之上补上只属于这个 carrier+board 组合的例化与连线」：[docs/user_guide/architecture.rst:1090-1094](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/architecture.rst#L1090-L1094)。

#### 4.1.4 代码实践

**实践目标**：亲手在源码中「点」出三层，确认三层模型不是抽象概念，而是写在文件里的具体行。

**操作步骤**：

1. 打开 [projects/fmcomms2/zcu102/system_bd.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_bd.tcl)（第三层入口）。
2. 找到第 6 行 `source $ad_hdl_dir/projects/common/zcu102/zcu102_system_bd.tcl`——点进这个链接，确认它带你进入的是载板基设计（第一层），里面例化了 `sys_ps8` 处理器。
3. 回到第三层，找到第 7 行 `source ../common/fmcomms2_bd.tcl`——点进相对路径解析后的目标，确认它带你进入的是评估板基设计（第二层），里面例化了 `axi_ad9361`。
4. 数一数第 8 行之后还剩多少行——那几行就是第三层专属的微调。

**需要观察的现象**：第三层文件本身极短（约 20 行），却组装出了一个包含处理器 + 射频收发器 + DMA 的完整设计。这说明绝大部分内容都被抽到了第一、二层复用。

**预期结果**：你会得到一张「行号 → 所属层」的对照表，例如「L6 = 第一层、L7 = 第二层、L10-20 = 第三层微调」。这是纯源码阅读型实践，无需运行任何工具，结论可直接验证。

#### 4.1.5 小练习与答案

**练习 1**：如果要把 fmcomms2 这块子卡从 zcu102 换到 zed 载板，第三层入口文件的哪一行会变？

> **答案**：第 6 行那行 source 载板基设计的语句会从 `.../zcu102/zcu102_system_bd.tcl` 换成 `.../zed/zed_system_bd.tcl`；而第 7 行 source 评估板基设计的 `../common/fmcomms2_bd.tcl` 保持不变。可对照真实文件 [projects/fmcomms2/zed/system_bd.tcl:6-7](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zed/system_bd.tcl#L6-L7) 验证。

**练习 2**：`system_wrapper` 这个模块是写在哪个单独文件里的吗？

> **答案**：不是。官方说明 `system_wrapper` 是工具根据块设计**自动生成**的，它的内容由载板基设计（第一层）、评估板基设计（第二层）和 `system_bd.tcl`（第三层）**共同**描述，没有任何一个单独的源文件手写它。参见 [docs/user_guide/architecture.rst:1107-1114](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/architecture.rst#L1107-L1114)。

---

### 4.2 carrier 与 eval 基设计的分离原则

#### 4.2.1 概念说明

第二层之所以叫「carrier independent（与载板无关）」，是因为它只关心「fmcomms2 这块子卡需要哪些 IP、这些 IP 之间怎么连数据」，而完全不关心子卡插在哪块载板上。同理，第一层只关心「zcu102 这块载板上有什么」，完全不关心上面插的是 fmcomms2 还是别的子卡。

这是一种典型的「**关注点分离**」。把两个互相独立的变量（载板、子卡）拆到两套文件里各自维护，而不是为每个组合写一份耦合在一起的设计。

用组合数学看更直观：假设有 \(N\) 种载板、\(M\) 种子卡。

- 若每组合写一份完整设计，需要维护 \(N \times M\) 份高度重复的文件。
- 拆成两层后，只需维护 \(N\) 份载板基设计 + \(M\) 份评估板基设计（外加少量组合级微调）：

\[
\underbrace{N \times M}_{\text{耦合写法}} \;\longrightarrow\; \underbrace{N + M}_{\text{三层分离写法}}
\]

这正是 ADI 用一套代码支持「几十块载板 × 几十块子卡」的根本原因。

#### 4.2.2 核心流程

分离原则落到代码上有两条具体规则：

1. **第一层只放「跟载板绑死」的东西**：处理器例化（`sys_ps8`/`sys_ps7`）、PS 引脚配置、板载时钟与复位发生器、SPI/I2C/GPIO、中断汇总 concat、system ID。这些东西换了载板就完全不同。
2. **第二层只放「跟子卡绑死、但跨载板通用」的东西**：子卡主芯片（如 `axi_ad9361`）、它的数据通路（FIFO、pack/unpack、DMA）、子卡对外接口（LVDS 数据/帧/时钟引脚的 `create_bd_port`）。这些东西换载板也不该变。

但第二层终究要连到处理器的地址空间和内存口上，而处理器是第一层才有的——两层怎么对话？答案是**通过 Tcl 全局变量**：第一层先把变量（如 `sys_cpu_clk`、`CACHE_COHERENCY`、`sys_ps8`）定义好，第二层直接引用它们。这就解释了为什么 source 顺序必须是「先第一层、后第二层」——变量得先存在，才能被引用。

```
第一层（carrier）            第二层（eval）
─────────────────            ─────────────────
set CACHE_COHERENCY true  ──┐
例化 sys_ps8               ──┼──▶ 第二层读取这些变量来决定：
set sys_cpu_clk ...        ──┤      - DMA 是否 cache 一致
                              │      - 连到 sys_ps8 (HPC) 还是 sys_ps7 (HP)
                              ▼      - 用哪个时钟域
```

#### 4.2.3 源码精读

**第一层定义全局变量**：载板基设计 [projects/common/zcu102/zcu102_system_bd.tcl:6](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/zcu102/zcu102_system_bd.tcl#L6) 一上来就 `set CACHE_COHERENCY true`，并在随后例化 `sys_ps8` 处理器、定义三套系统时钟（`sys_cpu_clk`/`sys_250m_clk`/`sys_500m_clk`）与对应复位，见 [projects/common/zcu102/zcu102_system_bd.tcl:27-102](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/zcu102/zcu102_system_bd.tcl#L27-L102)。

**第二层读取这些变量**：评估板基设计 [projects/fmcomms2/common/fmcomms2_bd.tcl:225-239](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L225-L239) 用 `if {$CACHE_COHERENCY}` 分两条路走：

```tcl
if {$CACHE_COHERENCY} {
  ad_mem_hpc0_interconnect $sys_cpu_clk sys_ps8/S_AXI_HPC0   ;# ZynqMP：一致缓存，用 HPC 口
  ...
} else {
  ad_mem_hp1_interconnect  $sys_cpu_clk sys_ps7/S_AXI_HP1    ;# Zynq-7000：非一致，用 HP 口
  ...
}
```

这就是分离原则最精彩的一处：第二层用**同一份代码**同时兼容 ZynqMP（`sys_ps8`）和 Zynq-7000（`sys_ps7`）两类载板，到底走哪条路完全由第一层留下的 `CACHE_COHERENCY` 决定。换句话说，第二层「不知道」自己插在哪种载板上，但它通过读取变量**自适应**了。

与之相对，第二层里真正「跟子卡绑死」的部分——`axi_ad9361` 的例化与 LVDS 引脚定义——则与载板毫无关系，见 [projects/fmcomms2/common/fmcomms2_bd.tcl:8-56](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L8-L56)。

#### 4.2.4 代码实践

**实践目标**：验证「第二层只通过变量与第一层对话，自身不出现载板专属写法」这一分离原则。

**操作步骤**：

1. 打开第二层 [projects/fmcomms2/common/fmcomms2_bd.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl)。
2. 搜索 `sys_ps8` 和 `sys_ps7`，确认它们**只**出现在 L225-239 这个 `if {$CACHE_COHERENCY}` 分支里——也就是说，子卡设计里出现的处理器符号是被「变量选择」出来的，而非写死的。
3. 反向到第一层 [projects/common/zcu102/zcu102_system_bd.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/zcu102/zcu102_system_bd.tcl)，确认其中**没有**任何 `axi_ad9361`、`util_cpack2` 这类子卡专属 IP——载板层对子卡一无所知。

**需要观察的现象**：在第一层搜不到子卡 IP，在第二层搜不到处理器的「硬编码」（只有变量分支）。

**预期结果**：你会确认两层的耦合点只有少数几个 Tcl 变量（`sys_cpu_clk`、`sys_cpu_resetn`、`CACHE_COHERENCY`、`sys_ps8/ps7`）。这是源码阅读型实践，结论可立即验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `CACHE_COHERENCY` 必须在第一层设置，而不能放在第二层？

> **答案**：因为 cache 一致性是**处理器（载板）**的属性——ZynqMP 的 PS8 支持 HPC 一致口、Zynq-7000 的 PS7 不支持。它属于「跟载板绑死」的信息，按分离原则就该放在第一层；第二层只负责读取它来适配连线。

**练习 2**：如果新增一块载板，需要改动第二层（评估板基设计）吗？

> **答案**：通常不需要。只要新载板的第一层正确提供了第二层所依赖的那些变量（`sys_cpu_clk`、`CACHE_COHERENCY`、`sys_ps*` 等），第二层这份 carrier-independent 代码就能原样复用。这正是 \(N+M\) 优于 \(N\times M\) 的体现。

---

### 4.3 system_bd.tcl 的 source 顺序

#### 4.3.1 概念说明

第三层文件 `system_bd.tcl` 的核心职责就是「按正确顺序把前两层 source 进来，再做微调」。这里的「顺序」不是风格偏好，而是**功能正确性的硬要求**：

- 第一层必须**先**执行，因为它定义了处理器、时钟、以及第二层赖以连线的全局变量。若第二层先执行，`$CACHE_COHERENCY`、`$sys_cpu_clk` 都还不存在，`if {$CACHE_COHERENCY}` 会报错或走到错误分支。
- 第二层必须在第一层之后、第三层微调之前执行，因为第三层的微调（如 `ad_ip_parameter axi_ad9361 CONFIG.ADC_INIT_DELAY 11`）要修改的 IP 正是第二层才例化出来的。

官方文档把这条规则写得非常直白：[docs/user_guide/architecture.rst:39-44](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/architecture.rst#L39-L44)（「先 source 载板基设计，再 source 评估板基设计」），并用 fmcomms2+zed 给了标准示例 [docs/user_guide/architecture.rst:53-56](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/architecture.rst#L53-L56)。

#### 4.3.2 核心流程

`system_bd.tcl` 的执行流程可以总结为「3 步组装」：

```
1. source 载板基设计    →  画布上出现：sys_ps8、时钟、复位、SPI、GPIO、中断 concat
                          （副作用：定义 sys_cpu_clk、CACHE_COHERENCY 等变量）
2. source 评估板基设计  →  画布上追加：axi_ad9361、wfifo/rfifo、cpack/upack、axi_dmac
                          （读取上一步的变量完成与处理器的连线）
3. 第三层自身语句       →  设置 system ID、调整 ADC_INIT_DELAY 等
                          （只对「该子卡 + 该载板」组合生效的微调）
```

为什么第三层这么短？因为「搭建」工作已经由前两层做完了，第三层只剩下「装配 + 调参」。这也解释了上一讲看到的反直觉现象：决定整个设计长什么样的 `system_bd.tcl` 反而是工程里**最短**的文件之一。

#### 4.3.3 源码精读

回到第三层入口 [projects/fmcomms2/zcu102/system_bd.tcl:6-8](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_bd.tcl#L6-L8)，对照三步流程：

```tcl
# 步骤 1（第一层）
source $ad_hdl_dir/projects/common/zcu102/zcu102_system_bd.tcl
# 步骤 2（第二层）
source ../common/fmcomms2_bd.tcl
# 工具辅助（platform designer 相关，非本讲重点）
source $ad_hdl_dir/projects/scripts/adi_pd.tcl
```

随后的步骤 3（第三层微调）就在同一个文件里 [projects/fmcomms2/zcu102/system_bd.tcl:10-20](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_bd.tcl#L10-L20)：

```tcl
#system ID
ad_ip_parameter axi_sysid_0 CONFIG.ROM_ADDR_BITS 9
...
ad_ip_parameter util_ad9361_divclk CONFIG.SIM_DEVICE ULTRASCALE   ;# 仅 zcu102(UltraScale) 需要
ad_ip_parameter axi_ad9361 CONFIG.ADC_INIT_DELAY 11              ;# 这个组合下的延迟校准值
```

注意 `SIM_DEVICE ULTRASCALE` 和 `ADC_INIT_DELAY 11` 正是「zcu102 + fmcomms2」这一组合才需要的微调——换到 zed（Zynq-7000，非 UltraScale）就不再设 `ULTRASCALE`，`ADC_INIT_DELAY` 也变成 23，见 [projects/fmcomms2/zed/system_bd.tcl:17-19](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zed/system_bd.tcl#L17-L19)。这恰好印证了第三层「只为某个具体组合而存在」的定位。

> 对照参考：官方文档里的示例用的是 fmcomms2+zed，写法与本讲的 zcu102 版本完全同构，只是载板路径不同：[docs/user_guide/architecture.rst:53-56](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/architecture.rst#L53-L56)。

#### 4.3.4 代码实践

**实践目标**：把本讲的实践任务（在 `system_bd.tcl` 中找出 source 两层基设计的两行 Tcl，并说明各属哪一层）完整做一遍，并横向对比两个载板。

**操作步骤**：

1. 打开 zcu102 版第三层 [projects/fmcomms2/zcu102/system_bd.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_bd.tcl)：
   - 第 **6 行** `source .../zcu102/zcu102_system_bd.tcl` → **第一层（载板基设计）**，用仓库根绝对路径定位。
   - 第 **7 行** `source ../common/fmcomms2_bd.tcl` → **第二层（评估板基设计）**，用相对路径定位到本子卡的 common 目录。
2. 再打开 zed 版第三层 [projects/fmcomms2/zed/system_bd.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zed/system_bd.tcl) 做同样标注。
3. 把两个文件并排对比，圈出**相同**的行（第 7 行 source 评估板、system ID 块）和**不同**的行（第 6 行的载板路径、第 17 行起的微调参数）。

**需要观察的现象**：换载板时，只有第 6 行的载板路径和第三层微调参数变化；第 7 行的评估板 source 与 `fmcomms2_bd.tcl` 完全不动。

**预期结果**：得到一张两列对比表，直观显示「载板相关的内容集中在第一层路径 + 第三层微调，评估板相关的内容被完整复用」。这是源码阅读型实践，结论可立即验证。

> 进阶（可选）：如果你想动手验证 source 顺序的依赖关系，可以在本地用 Vivado 交互模式打开工程，在 `system_bd.tcl` 里把第 6、7 两行 source 顺序对调后重新运行，观察 Tcl 是否因 `$CACHE_COHERENCY` 未定义而报错。**该现象待本地验证**（需要装有对应版本 Vivado 与授权）。

#### 4.3.5 小练习与答案

**练习 1**：把 `system_bd.tcl` 里第 6、7 行的 source 顺序对调，会发生什么？

> **答案**：第二层会先执行，此时 `$CACHE_COHERENCY`、`$sys_cpu_clk` 都还没被第一层定义，`if {$CACHE_COHERENCY}` 会把未定义变量当作空值/0 处理，从而错误地走到 `else` 分支（连到不存在的 `sys_ps7`），或直接报变量未定义错误。所以顺序不能颠倒。

**练习 2**：第三层文件里那几行 `ad_ip_parameter ... CONFIG.xxx` 为什么不能挪到第二层（`fmcomms2_bd.tcl`）里？

> **答案**：因为那些参数（如 `ADC_INIT_DELAY 11`、`SIM_DEVICE ULTRASCALE`）是针对「fmcomms2 + zcu102」这一**特定组合**的校准值，换载板就变（zed 版的 `ADC_INIT_DELAY` 是 23）。把它们放进 carrier-independent 的第二层，就会污染所有载板的复用，违背分离原则。它们只属于第三层。

## 5. 综合实践

**任务**：给「fmcomms2 子卡 + zcu102 载板」画一张**三层归属图**，并据此预测换载板时的改动量。

要求：

1. 打开 [projects/fmcomms2/zcu102/system_bd.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_bd.tcl)（第三层）、[projects/common/zcu102/zcu102_system_bd.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/zcu102/zcu102_system_bd.tcl)（第一层）、[projects/fmcomms2/common/fmcomms2_bd.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl)（第二层）三个文件。
2. 从每个文件里各挑 2 个「最具代表性」的代码点，填入下表（示例骨架，请用真实行号与符号补全）：

   | 层 | 文件 | 代表代码点（符号 + 行号） | 为什么属于这一层 |
   | --- | --- | --- | --- |
   | 第一层 载板 | `zcu102_system_bd.tcl` | 例：`ad_ip_instance zynq_ultra_ps_e sys_ps8` (L27) | 处理器是载板的固有资产 |
   | 第二层 评估 | `fmcomms2_bd.tcl` | 例：`ad_ip_instance axi_ad9361 axi_ad9361` (L33) | （请你补全理由） |
   | 第三层 系统 | `system_bd.tcl` | 例：`source ../common/fmcomms2_bd.tcl` (L7) | （请你补全理由） |

3. 回答一个预测题：如果把 fmcomms2 从 zcu102 换到一个全新的 ZynqMP 载板 `myboard`，按三层模型，你需要**新建/修改**哪些文件、**复用**哪些文件？（提示：新建 `projects/common/myboard/myboard_system_bd.tcl` 第一层 + `projects/fmcomms2/myboard/system_bd.tcl` 第三层；第二层 `fmcomms2_bd.tcl` 原样复用。）

**预期产出**：一张填好的三层归属表 + 一段换板改动预测。这个练习把「三层是什么、各放什么、为什么这样放、换板会动哪里」一次性串起来，是本讲的核心收尾。

## 6. 本讲小结

- 每个参考设计在逻辑上分**三层**：载板基设计（第一层，`projects/common/$CARRIER`）、评估板基设计（第二层，`projects/$EVAL/common`）、系统特化设计（第三层，`projects/$EVAL/$CARRIER`）。
- 分离原则：第一层 carrier-dependent（处理器/时钟/外设），第二层 carrier-independent（子卡 IP + 数据通路）。把 \(N\times M\) 份重复设计降为 \(N+M\) 份。
- 第三层 `system_bd.tcl` 是组装入口，按**先载板、后评估板**的固定顺序 `source` 两层，再做组合级微调；顺序不能颠倒，因为第二层依赖第一层定义的 Tcl 变量。
- 两层之间通过**全局 Tcl 变量**对话：第一层设 `CACHE_COHERENCY`/`sys_cpu_clk`/`sys_ps8`，第二层据此自适应（如 `if {$CACHE_COHERENCY}` 选 HPC 还是 HP 口）。
- `system_wrapper` 顶层模块不是手写的，而是由三层**共同**描述、由工具自动生成。
- 换载板只动第一层路径 + 第三层微调；换子卡只动第二层——这是三层架构带来的可维护性。

## 7. 下一步学习建议

- **下一篇 u2-l2（单个工程的文件剖析）**会把视角从「块设计 Tcl」扩展到工程五件套——重点读 `system_top.v` 如何例化 `system_wrapper` 与 IO 缓冲、`system_constr.xdc` 如何做引脚约束，把本讲的「逻辑三层」与「物理文件」对应起来。
- 想深入理解第二层里那些 `ad_connect`/`ad_cpu_interconnect` 连线原语，可提前浏览 [projects/scripts/adi_board.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_board.tcl)，这会在 u3-l4 专题讲解。
- 想理解第一层处理器地址如何映射到软件看到的地址，可阅读 [docs/user_guide/architecture.rst:162-196](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/architecture.rst#L162-L196) 的 CPU/Memory interconnect addresses 一节，这是后续 u4-l5（寄存器映射）的伏笔。
