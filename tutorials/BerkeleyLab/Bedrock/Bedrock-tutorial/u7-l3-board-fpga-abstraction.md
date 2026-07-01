# 板级支持与 FPGA 厂家抽象

> 本讲属于专家层（u7），承接 [u1-l3 目录结构与代码导航](u1-l3-directory-structure.md) 与 [u2-l1 基于 Make 的 HDL 仿真测试方法](u2-l1-make-hdl-testing.md)。

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 Bedrock 用哪两个子目录把「板卡差异」和「FPGA 厂家差异」从应用 RTL 里剥离出去，让同一份 DSP/RF 设计可以在 Marble、KC705、VC705、BMB7 等不同板卡、不同器件上综合。
- 看懂 `board_support/<板卡>/` 下的三件套：`rules.mk`（器件型号 `PART` 与家族 `FPGA_FAMILY`）、`base.xdc`/`base.ucf`（板载固定引脚约束）、`fmc-*.xdc`（FMC 连接器约束）。
- 读懂 Bedrock 的**两套引脚映射机制**：Marble 板专用的 `pin_map.csv` + `meta-xdc.py`，以及 FMC 子卡通用的 `fmc.map` + `XDC_MAP`/`FMC_MAP` awk 规则。
- 理解 `fpga_family/` 如何用「可仿真原语包装 + `` `ifndef SIMULATE `` 守卫 + `generate` 分派」让 iverilog/Verilator 在没有 Xilinx UNISIM 库的情况下也能仿真含原语的设计。
- 理解 `build-tools/top_rules.mk` 如何用 `XILINX_TOOL` 在 VIVADO / PLANAHEAD / ISE 三套工具链之间分发，并把厂家生成物统一收进 `_xilinx/` 目录。

## 2. 前置知识

本讲会用到一些 FPGA 工程术语，先对齐：

- **约束文件**：告诉综合/实现工具「某个端口连到哪个物理引脚、用什么电平」的文件。Vivado 用 **XDC**（Xilinx Design Constraints），格式是 `set_property -dict {PACKAGE_PIN A8 IOSTANDARD LVCMOS25} [get_ports 端口名]`；老的 ISE 用 **UCF**，格式是 `NET 端口名 LOC = A8 | IOSTANDARD = LVCMOS25;`。两种格式并存，正是后面 `XDC_MAP`/`FMC_MAP` 两套 awk 的由来。
- **PACKAGE_PIN / IOSTANDARD**：前者是芯片封装上的物理引脚号（如 `A8`、`AD23`）；后者是电平标准（`LVCMOS25` 单端 2.5V、`LVDS_25` 差分、`SSTL15` DDR3 等）。
- **FMC（FPGA Mezzanine Card，VITA 57）**：一种标准子卡连接器。它规定了一组**与载板无关**的信号名，例如 `FMC_LA00_CC_P/N`（0 号时钟使能差分对）、`FMC_CLK0_M2C_P/N`（子卡到载板的时钟）。同一对 `FMC_LA00_CC_P` 在 KC705 载板上可能连到 FPGA 的 `AD23`，在 BMB7 载板上却连到 `R21`——这正是 FMC 抽象要解决的问题。
- **UNISIM 原语**：Xilinx 的底层硬件单元库，如 `BUFG`（全局时钟缓冲）、`IBUF`/`IBUFDS`（输入缓冲/差分输入）、`MMCM`（混合模式时钟管理器）、`ODDR`（双倍数据率输出寄存器）。它们是厂家特定的，综合时会被映射到真实硅片资源。
- **开源仿真器没有 UNISIM 库**：iverilog、Verilator 不认识 `BUFG`、`MMCM` 这些名字，仿真含原语的设计时会报「找不到模块」。Bedrock 的对策就是 `fpga_family/xilinx/` 里那一堆同名 `.v` 文件。
- **`` `SIMULATE `` 宏**：Bedrock 仿真时一律加 `-DSIMULATE` 编译（见 `top_rules.mk` 的 `VG_ALL = -DSIMULATE`），用它把厂家原语块整段替换成行为模型。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [board_support/rules.mk](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/rules.mk) | 板级公共规则：拼装 `system_top.xdc`、定义 `%_$(DAUGHTER).xdc/ucf` 的 FMC 映射模式规则 |
| [board_support/marble/rules.mk](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marble/rules.mk) | Marble 板的「身份卡」：`PART`、`FPGA_FAMILY`，以及用 `meta-xdc.py` 生成约束的规则 |
| [board_support/marble/Marble.xdc](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marble/Marble.xdc) | Marble 板的原始硬件 XDC（来自 BerkeleyLab/Marble 仓库），把硬件引脚名映射到 PACKAGE_PIN |
| [board_support/marble/pin_map.csv](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marble/pin_map.csv) | Marble 板的引脚重映射表 + 字面量输出区，喂给 `meta-xdc.py` |
| [board_support/kc705/fmc-lpc.xdc](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/kc705/fmc-lpc.xdc) | KC705 载板的 FMC-LPC 连接器约束：FMC 标准引脚名 → 该载板上的 PACKAGE_PIN |
| [board_support/fmc112/fmc.map](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/fmc112/fmc.map) | FMC112 子卡的映射表：子卡信号名 → FMC 标准引脚名 + IOSTANDARD |
| [badger/tests/meta-xdc.py](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/tests/meta-xdc.py) | Marble 专用的「meta-XDC」重写脚本：读原始 XDC + 映射表，产出应用级 XDC |
| [build-tools/top_rules.mk](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/top_rules.mk) | 顶层公共规则：定义 `XDC_MAP`/`FMC_MAP` awk、`XILINX_TOOL` 工具分发、`UNISIM_CRAP` 原语清单 |
| [fpga_family/pll.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/pll.v) | PLL 包装器：用 `DEVICE` 参数 + `SIMULATE` 守卫在 Spartan6/7-series 原语与仿真模型间切换 |
| [fpga_family/ds_clk_buf.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/ds_clk_buf.v) | 差分时钟缓冲包装器：按 `GTX`/`USE_BUF` 参数选择 `IBUFDS_GTE2`/`IBUFDS`/`BUFG`/`BUFH` |
| [fpga_family/ddr_cells.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/ddr_cells.v) | DDR 输出寄存器包装器：`ODDR` 原语块及其仿真行为模型 |
| [fpga_family/xilinx/BUFG.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/xilinx/BUFG.v) | 最简单的 UNISIM 原语仿真桩：`BUFG` 用一个 `buf` 实现 |
| [fpga_family/xilinx/MMCME2_BASE.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/xilinx/MMCME2_BASE.v) | 占位桩：仅保留参数端口，供依赖推导/lint 使用 |

## 4. 核心概念与源码讲解

本讲分四个最小模块：先看一块板卡的「身份卡」是怎么定义的（4.1），再分别讲两套引脚映射机制（4.2、4.3），最后看厂家原语如何被可仿真地包装起来（4.4）。

### 4.1 board_support：板卡的「身份卡」与三件套

#### 4.1.1 概念说明

`board_support/` 下每个子目录代表一块载板：`marble`、`marblemini`、`kc705`、`vc707`、`bmb7_kintex`、`ac701`、`qf2pre_kintex`、`ml605`、`sp605`，以及若干 FMC 子卡目录（`fmc112`/`fmc116`/`fmc120`/`fmc150` 等）。一块载板要被工程复用，需要回答三个问题：

1. **这是什么器件？** → `PART`（完整型号）和 `FPGA_FAMILY`（家族，决定用哪套原语）。
2. **板载固定资源（时钟、LED、DDR、配置 Flash）接在哪些引脚？** → `base.xdc`（Vivado）或 `base.ucf`（ISE）。
3. **FMC 连接器的标准引脚接在哪些 FPGA 引脚？** → `fmc-lpc.xdc` / `fmc-hpc.xdc` 等。

这三个答案就是板卡的「身份卡」。工程顶层 Makefile 通过变量 `HARDWARE` 指定用哪块板（例如 comms_top 工程写 `HARDWARE = qf2pre_kintex`），随后 `include $(BOARD_SUPPORT_DIR)/$(HARDWARE)/rules.mk` 把对应身份卡拉进来。

#### 4.1.2 核心流程

```
工程 Makefile
   │  HARDWARE = marble   (或 kc705 / vc707 ...)
   ▼
include board_support/$(HARDWARE)/rules.mk   → 得到 PART / FPGA_FAMILY
   │
   └─ base.xdc + $(COMMUNICATION).xdc  ──cat──▶  system_top.xdc   (板载约束合并)
        (COMMUNICATION 通常是 gmii / gtp / gtx 等通信相关约束)
```

最终综合时，`system_top.xdc`（Vivado 流程）或 `system_top.ucf`（ISE 流程）就是交给厂家工具的完整约束。

#### 4.1.3 源码精读

Marble 板的身份卡只有两行，但信息量很大：

- [board_support/marble/rules.mk:1-2](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marble/rules.mk#L1-L2) 定义 `PART = xc7k160t-ffg676-2`（Kintex-7，160K 逻辑单元，676 引脚 ffg 封装，速度等级 -2）和 `FPGA_FAMILY = 7series`。家族名 `7series` 决定了后面 `pll.v` 等包装器选 `PLLE2_ADV` 而非 Spartan6 的 `PLL_BASE`。

对比其它板卡，可见器件跨度很大（都是同一份应用 RTL 在跑）：

| 板卡 | PART | FPGA_FAMILY |
| --- | --- | --- |
| marble | xc7k160t-ffg676-2 | 7series |
| marblemini | xc7a100t-fgg484-2 | 7series |
| kc705 | xc7k325t-ffg900-2 | 7series |
| vc707 | xc7vx485t-ffg1761-2 | 7series（Virtex-7） |
| sp605 | xc6slx45t-fgg484-3 | spartan6 |
| ml605 | xc6vlx240t-ff1156-1 | virtex6 |

> 这些值来自各 `board_support/<板>/rules.mk`，例如 `grep -rn "PART\|FPGA_FAMILY" board_support/*/rules.mk` 即可全部列出。

板载约束的合并规则在 [board_support/rules.mk:3-4](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/rules.mk#L3-L4)：`system_top.xdc` 就是把 `base.xdc`（板载固定资源）和 `$(COMMUNICATION).xdc`（通信相关，如 `gmii.xdc`/`gtx.xdc`）简单 `cat` 在一起。这样板载约束与通信约束分文件维护，按工程需要组合。

一个简洁的板载约束样本见 [board_support/ac701/base.xdc](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/ac701/base.xdc)：它声明 `CFGBVS`/`CONFIG_VOLTAGE` 配置电压、200 MHz 差分时钟 `SYS_CLK_P/N`、4 个 LED，以及 DDR bank 的 `INTERNAL_VREF`——这些都是「焊死在板上的资源」，与你的应用 RTL 无关，故归板卡所有。

#### 4.1.4 代码实践

**目标**：理解板卡身份卡的查找与作用。

**步骤**：

1. 运行 `grep -rn "PART\|FPGA_FAMILY" board_support/*/rules.mk`，把所有板卡的器件型号列成一张表。
2. 运行 `grep -n "HARDWARE" projects/comms_top/Makefile projects/test_marble_family/Makefile`，看真实工程把 `HARDWARE` 设成了什么板。
3. 任选一块板，打开它的 `base.xdc`（或 `base.ucf`），数一数里面约束了哪些「板载固定资源」。

**预期结果**：你会发现 `HARDWARE` 变量是工程与板卡之间的唯一耦合点——换板只需改这一个变量（并确保新板的 `base.xdc` 覆盖了你用到的引脚）。

**待本地验证**：步骤 1/2 是纯文本检索，任何机器都能跑；步骤 3 取决于你选的板。

#### 4.1.5 小练习与答案

**练习 1**：`FPGA_FAMILY = spartan6` 的板卡是哪一块？为什么它和 `7series` 的板不能共用同一份 `pll.v` 配置？

> **答案**：是 `sp605`（`PART = xc6slx45t...`，`FPGA_FAMILY = spartan6`）。因为 Spartan6 的锁相环原语是 `PLL_BASE`，而 7-Series 是 `PLLE2_ADV`，二者端口与参数都不同，必须靠 4.4 讲的 `DEVICE` 参数分派。

**练习 2**：`system_top.xdc` 为什么要由 `base.xdc` 和 `$(COMMUNICATION).xdc` 两段拼成，而不是写成一个文件？

> **答案**：关注点分离。`base.xdc` 描述「这块板焊了什么」（时钟/LED/DDR/Flash），与通信方式无关；`$(COMMUNICATION).xdc`（如 `gmii.xdc`/`gtp.xdc`）描述「这次设计走哪种通信链路」。同一块板可能跑不同通信方案，分开维护才能按需组合，避免一份巨型约束文件难以复用。

---

### 4.2 引脚映射机制一：Marble 的 pin_map.csv + meta-xdc.py

#### 4.2.1 概念说明

Marble 板的约束生成比「直接写 base.xdc」多一层间接。原因是 Marble 是一个**独立硬件项目**（`git@github.com:BerkeleyLab/Marble.git`），它会产出一份把「硬件引脚名」（如 `RGMII_RX_CLK`、`FMC1_LA_24_N`）映射到 PACKAGE_PIN 的原始 XDC。但应用 RTL 里的端口名未必和硬件引脚名一致（应用层可能叫 `RGMII_RXD[0]` 而硬件层叫 `RGMII_RXD0`）。于是 Bedrock 用一个映射表 `pin_map.csv` 把两者对上，再用脚本 `meta-xdc.py` 重写 XDC。

这是一种「**用一份硬件 XDC + 一份映射表，生成一份应用 XDC**」的 meta-XDC 思路。

#### 4.2.2 核心流程

```
Marble.xdc  (硬件引脚名 → PACKAGE_PIN/IOSTANDARD，来自 Marble 硬件仓库)
        +
pin_map.csv (硬件引脚名  →  应用端口名；外加「字面量输出区」)
        │
        ▼  meta-xdc.py
应用级 XDC   (set_property ... [get_ports 应用端口名])
```

`pin_map.csv` 有两种行：普通映射行（两列）和「字面量输出」区（原样透传的 XDC）。

#### 4.2.3 源码精读

先看映射表的格式约定，见 [board_support/marble/pin_map.csv:1-8](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marble/pin_map.csv#L1-L8)：第一列是 Marble.xdc 里出现的引脚名，第二列是应用顶层 Verilog 里的端口名。例如 [pin_map.csv:9-15](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marble/pin_map.csv#L9-L15) 把硬件名 `RGMII_RXD0` 映射成应用名 `RGMII_RXD[0]`、`RGMII_RX_DV` 映射成 `RGMII_RX_CTRL`。

遇到无法用映射表达的复杂约束（差分时钟对、MGT 参考时钟、`create_clock`、特殊属性），就在 [pin_map.csv:89](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marble/pin_map.csv#L89) 的 `# Literal output follows` 标记之后**整段原样输出**，例如 [pin_map.csv:92-93](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marble/pin_map.csv#L92-L93) 直接写 `DDR_REF_CLK_P/N` 的 PACKAGE_PIN。

`meta-xdc.py` 的逻辑分三步：

- [badger/tests/meta-xdc.py:14-19](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/tests/meta-xdc.py#L14-L19) `absorb_xdc`：读入原始 XDC，按最后一列（引脚名）建索引 `xdc_map[引脚名] = 该行去掉引脚名后的前缀`（即 `PACKAGE_PIN ... IOSTANDARD ...` 部分）。
- [badger/tests/meta-xdc.py:45-63](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/tests/meta-xdc.py#L45-L63) `absorb_map`：逐行处理映射表——遇到 `# Literal output follows` 之后的内容原样打印；遇到普通映射行就查 `xdc_map`，把前缀里的引脚名替换成应用端口名。
- [badger/tests/meta-xdc.py:22-43](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/tests/meta-xdc.py#L22-L43) `merge`：还有个巧妙的电平转换——`DIFF_HSTL_II_25` 在 Marble 硬件层是 Bank 默认值，但应用层若端口名以 `_N`/`_P` 结尾（差分）就转成 `LVDS_25`，否则转成 `LVCMOS25`（转换表见 [meta-xdc.py:8](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/tests/meta-xdc.py#L8)）。

调用方式见 [meta-xdc.py:67-75](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/tests/meta-xdc.py#L67-L75)：第一个参数是原始 XDC，后续参数是一到多个映射表。真实工程里的调用见 [projects/test_marble_family/Makefile:152](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/projects/test_marble_family/Makefile#L152)：`meta-xdc.py Marble.xdc pin_map.csv pin_map_fmc.csv ...` 生成最终约束。

Marble 子目录里还有一个独立小规则 [board_support/marble/rules.mk:6-7](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marble/rules.mk#L6-L7) 演示了同样的生成（产出 `marble2.xdc`），可当作「最小用法」样例。

#### 4.2.4 代码实践

**目标**：用阅读理解 meta-XDC 的重写过程，不实际综合。

**步骤**：

1. 打开 [board_support/marble/Marble.xdc](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marble/Marble.xdc)，随便找一行，比如 `set_property -dict {PACKAGE_PIN A17 IOSTANDARD LVCMOS25} [get_ports I2C_FPGA_SDA]`。
2. 在 [pin_map.csv:41](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/marble/pin_map.csv#L41) 找到映射 `I2C_FPGA_SDA  TWI_SDA`。
3. 推断 `meta-xdc.py` 会输出哪一行。

**预期结果**：输出应为 `set_property -dict {PACKAGE_PIN A17 IOSTANDARD LVCMOS25} [get_ports TWI_SDA]`——硬件引脚名 `I2C_FPGA_SDA` 被替换成应用端口名 `TWI_SDA`，PACKAGE_PIN/IOSTANDARD 不变。端口名 `TWI_SDA` 不以 `_P/_N` 结尾，故不触发差分转换。

#### 4.2.5 小练习与答案

**练习 1**：`pin_map.csv` 里 `# Literal output follows` 之后的内容为什么不走映射、直接原样输出？

> **答案**：这些行（如 `create_clock`、MGT 参考时钟引脚、`IOB TRUE` 等特殊属性）要么不针对单一可重命名端口（如 `create_clock` 作用于已约束的端口），要么是硬件层独有的特殊设置，无法用「换端口名」表达，所以脚本选择原样透传。

**练习 2**：为什么 `merge` 函数要靠「端口名是否以 `_P`/`_N` 结尾」来决定电平转换？

> **答案**：Marble 硬件层在 FMC Bank 上统一标 `DIFF_HSTL_II_25`，但应用层究竟用单端还是差分，取决于端口命名约定（差分对用 `_P/_N` 后缀，见 u1-l4 RTL 规范）。脚本没有更可靠的信号源，只能用这个启发式（heuristic）猜测，作者注释里也承认它「fragile」。

---

### 4.3 引脚映射机制二：FMC 子卡的 fmc.map + XDC_MAP/FMC_MAP

> ⚠️ 这是本讲的重点，也是指定的代码实践任务。

#### 4.3.1 概念说明

FMC 子卡机制解决的是一个更普遍的复用问题：**同一块载板可以插不同的 FMC 子卡，同一张 FMC 子卡可以插不同的载板**。如果每次组合都手写一份约束，组合数会爆炸。Bedrock 的做法是把信息拆成两个正交维度：

- **载板侧**（`board_support/<载板>/fmc-lpc.xdc`）：FMC 标准引脚名 → 本载板 FPGA 上的 PACKAGE_PIN。回答「`FMC_LA00_CC_P` 在这块板上接哪个引脚」。
- **子卡侧**（`board_support/<子卡>/fmc.map`）：子卡信号名 → FMC 标准引脚名 + IOSTANDARD。回答「子卡的 `DCO_P[0]` 信号走了 FMC 的哪一对、用什么电平」。

把这两份表 join 一下，就能直接得到「子卡信号 → FPGA 物理引脚」的约束。这个 join 由 `top_rules.mk` 里两条 awk 单行完成：`XDC_MAP`（产出 Vivado XDC）和 `FMC_MAP`（产出 ISE UCF）。

#### 4.3.2 核心流程

```
载板 fmc-lpc.xdc            子卡 fmc.map
 (FMC标准名 → PACKAGE_PIN)   (子卡信号 → FMC标准名, IOSTANDARD)
        │                          │
        └─────── XDC_MAP (awk) ────┘
                      │
                      ▼
   set_property PACKAGE_PIN <物理引脚> IOSTANDARD <子卡电平> [get_ports <子卡信号>]
```

关键点：**PACKAGE_PIN 取自载板表（物理位置由载板决定），IOSTANDARD 取自子卡表（电平由子卡决定）**。

对应的 Make 模式规则（Vivado 分支）见 [build-tools/top_rules.mk:168-172](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/top_rules.mk#L168-L172)：目标 `fmc-lpc_$(DAUGHTER).xdc` 由载板的 `fmc-lpc.xdc` 加上子卡的 `fmc.map` 经 `XDC_MAP` 生成。另一份更通用的版本（与载板无关、用 stem `%.xdc`）在 [board_support/rules.mk:11-14](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/rules.mk#L11-L14)。

#### 4.3.3 源码精读

`XDC_MAP` 是一条 awk，定义在 [build-tools/top_rules.mk:72](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/top_rules.mk#L72)（注意 Makefile 里写作 `$$`，make 会折叠成单个 `$` 传给 shell）：

```awk
# 字段分隔符 = 一个或多个 空格/双引号/Tab
awk -F'[ "\t]+' '
  NR==FNR { gsub(/]/,"",$8); a[$8]=$4; next }   # 第一个文件(载板xdc)：建表 a[FMC名]=PACKAGE_PIN
  ($3 in a){ printf "set_property -dict \"PACKAGE_PIN %-4s IOSTANDARD %s\" [get_ports %s]\n", a[$3], $4, $2 }
'  载板fmc-lpc.xdc  子卡fmc.map
```

逐字段拆解（这是理解本机制的关键）：

**第一个文件 = 载板 `fmc-lpc.xdc`**，典型行（[board_support/kc705/fmc-lpc.xdc:22](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/kc705/fmc-lpc.xdc#L22)）：

```
set_property -dict "PACKAGE_PIN AD23 IOSTANDARD LVCMOS25" [get_ports FMC_LA00_CC_P]
```

以 `[ "\t]+` 切分（引号也是分隔符，引号与相邻空格合并为一段），字段为：

| $1 | $2 | $3 | $4 | $5 | $6 | $7 | $8 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| set_property | -dict | PACKAGE_PIN | **AD23** | IOSTANDARD | LVCMOS25 | [get_ports | **FMC_LA00_CC_P]** |

`gsub(/]/,"",$8)` 去掉末尾 `]`，于是 `a["FMC_LA00_CC_P"] = "AD23"`。**建表完成。**

**第二个文件 = 子卡 `fmc.map`**，典型行（[board_support/fmc112/fmc.map:4](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/fmc112/fmc.map#L4)）：

```
"FMC112_DCO_P[0]"       "FMC_LA00_CC_P"         "LVDS_25"
```

行首是引号（分隔符），故 $1 为空，字段为：

| $1 | $2 | $3 | $4 |
| --- | --- | --- | --- |
| (空) | **FMC112_DCO_P[0]** | **FMC_LA00_CC_P** | **LVDS_25** |

条件 `($3 in a)`：`FMC_LA00_CC_P` 在表里（值 `AD23`），命中。按 `printf` 模板代入 `a[$3]=AD23`、`$4=LVDS_25`、`$2=FMC112_DCO_P[0]`，输出：

```
set_property -dict "PACKAGE_PIN AD23 IOSTANDARD LVDS_25" [get_ports FMC112_DCO_P[0]]
```

**这一行的含义**：FMC112 子卡的 `DCO_P[0]` 信号（子卡把它布在 FMC 连接器的 `LA00_CC_P` 对上，电平 LVDS_25），在 KC705 载板上落到 FPGA 物理引脚 `AD23`。PACKAGE_PIN 来自载板，IOSTANDARD 来自子卡——完美体现「位置与电平正交分离」。

对比 `FMC_MAP`（UCF 旧格式），见 [build-tools/top_rules.mk:71](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/top_rules.mk#L71)：它用 `-F\"`（仅以双引号切分），产出 `NET ... LOC = ... | IOSTANDARD = ...;`，逻辑同构，只是输出格式与字段编号不同。两条规则按 [board_support/rules.mk:16-17](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/rules.mk#L16-L17) 的 `XILINX_TOOL` 判断择一清理（VIVADO 清 `.xdc`，否则清 `.ucf`）。

#### 4.3.4 代码实践（指定任务）

**目标**：亲手把一份 `fmc.map` 通过 `XDC_MAP` 转成具体引脚约束 XDC，验证「载板表 join 子卡表」的理解。

**操作步骤**：

1. 阅读载板约束 [board_support/kc705/fmc-lpc.xdc](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/kc705/fmc-lpc.xdc)，确认它把 FMC 标准名（`FMC_LA00_CC_P` 等）映射到 KC705 的 PACKAGE_PIN。
2. 阅读子卡映射 [board_support/fmc112/fmc.map](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/fmc112/fmc.map)，确认它把子卡信号（`FMC112_DCO_P[0]` 等）映射到 FMC 标准名并标注 IOSTANDARD。
3. 在仓库根目录跑下面这条命令（与 `top_rules.mk:72` 等价，已把 `$$` 还原为 `$`，整段单引号保护 `$` 不被 shell 展开）：

```bash
awk -F'[ "\t]+' \
  'NR==FNR{gsub(/]/,"",$8);a[$8]=$4;next}($3 in a){
     printf "set_property -dict \"PACKAGE_PIN %-4s IOSTANDARD %s\" [get_ports %s]\n",a[$3],$4,$2
   }' \
  board_support/kc705/fmc-lpc.xdc board_support/fmc112/fmc.map | head
```

**需要观察的现象**：每行输出都是一条合法的 Vivado `set_property` 约束，端口名是**子卡信号名**（`FMC112_*`），PACKAGE_PIN 是**载板给的引脚**，IOSTANDARD 是**子卡给的电平**（`LVDS_25`）。

**预期结果**（按 awk 字段切分手算，第一行对应 `FMC_LA00_CC_P` 对）：

```
set_property -dict "PACKAGE_PIN AD23 IOSTANDARD LVDS_25" [get_ports FMC112_DCO_P[0]]
set_property -dict "PACKAGE_PIN AE24 IOSTANDARD LVDS_25" [get_ports FMC112_DCO_N[0]]
...
```

**待本地验证**：上述输出是依据 [kc705/fmc-lpc.xdc:21-22](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/kc705/fmc-lpc.xdc#L21-L22) 与 [fmc112/fmc.map:3-4](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/board_support/fmc112/fmc.map#L3-L4) 手算的结果，请你实跑命令核对；若把第二个输入换成 `board_support/bmb7_kintex/fmc-lpc.xdc`，同一张 `fmc.map` 应产出**不同的 PACKAGE_PIN**（因为 BMB7 把 `FMC_LA00_CC_P` 布到了 `R21` 而非 `AD23`），这正是 FMC 抽象的价值。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `XDC_MAP` 第一个文件必须是载板 xdc、第二个必须是 `fmc.map`，顺序不能反？

> **答案**：awk 的 `NR==FNR{...;next}` 块只在读**第一个文件**时执行，用来建查找表 `a`；第二个文件才走 `($3 in a)` 的输出逻辑。Make 规则里 `$^` 的顺序是 `载板xdc fmc.map`（依赖列表顺序），故载板表先建、子卡表后查。顺序反了，`a` 会建成子卡信号名索引，`$3`（载板的 FMC 名）查不到，输出为空。

**练习 2**：`XDC_MAP` 输出里的 IOSTANDARD 为什么来自 `fmc.map`（子卡）而不是载板 xdc？

> **答案**：因为电平标准由子卡上的器件决定（FMC112 的 LVDS ADC 输出就是 LVDS_25），与载板把它布到哪个引脚无关。载板 xdc 里的 `LVCMOS25` 只是载板的默认上拉/电平提示，被子卡的实际电平覆盖。awk 里输出用的是第二个文件（子卡）的 `$4`，正好体现这一分工。

**练习 3**：如果某对 FMC 引脚在载板 xdc 里**没有**约束（即载板没把那对引脚连出来），会怎样？

> **答案**：建表时该 FMC 名不会进入 `a`，子卡 `fmc.map` 里用到这对的行其 `$3` 不满足 `($3 in a)`，被 awk 静默跳过——该子卡信号不会出现在输出约束里。这是一种「能力协商」：只有载板和子卡都支持的对才会被约束。

---

### 4.4 fpga_family：厂家原语的可仿真包装

#### 4.4.1 概念说明

`fpga_family/` 隔离的是「FPGA 厂家差异」。它干两件事：

1. **给 Xilinx UNISIM 原语提供仿真桩**：在 `fpga_family/xilinx/` 下放一组同名 `.v`（`BUFG.v`、`IBUF.v`、`MMCM...v` 等），让 iverilog/Verilator 仿真含原语的设计时不会因为「找不到模块」而失败。
2. **写参数化的「原语包装器」**：把「同一个功能在不同器件家族上的不同原语」封装成一个统一接口的模块（如 `pll`、`ds_clk_buf`、`ddr_cells`），用参数 + `generate` 在家族间分派，再用 `` `ifndef SIMULATE `` 守卫在「真实原语」与「行为模型」间切换。

`fpga_family/xilinx/README.md` 把这层称为 "Simple generic Xilinx primitives"——「simple」是因为它们只是让仿真跑起来的替身，不是行为精确的模型。

#### 4.4.2 核心流程

```
应用 RTL 实例化 pll / ds_clk_buf / ddr_cells  (统一接口，与家族无关)
        │
        ├─── 仿真 (iverilog -DSIMULATE) ──▶ `else 分支：行为模型 (reg/assign)
        │
        └─── 综合 (Vivado/ISE, 无 SIMULATE) ──▶ 真实原语块
                                                  generate 按 DEVICE 选 PLL_BASE / PLLE2_ADV
        +
独立原语桩 (BUFG.v/IBUF.v/...) ──▶ 同时被仿真器当作模块定义吸收
                                  (UNISIM_CRAP 清单：top_rules.mk:204)
```

#### 4.4.3 源码精读

**最简单的桩**——[fpga_family/xilinx/BUFG.v:3-7](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/xilinx/BUFG.v#L3-L7)：全局时钟缓冲 `BUFG` 在仿真里就是一个普通 `buf`，因为它对仿真语义无影响（只是综合时映射到全局时钟树）。`README.md` 列出的 `BUFG/IBUF/IDDR/ODDR/...` 都属此类「行为上正确」的替身。

**占位桩**——[fpga_family/xilinx/MMCME2_BASE.v:1](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/xilinx/MMCME2_BASE.v#L1) 开头直言 `// !!! Placeholder only !!!`：它只保留参数和端口、没有行为。它的存在仅为依赖推导和 lint 不报错（README 也说这类 "may or may not have some utility other than acting as a placeholder in dependency generation and linting"）。

**参数化包装器范例 1——`pll`**：[fpga_family/pll.v:5](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/pll.v#L5) 用 `parameter DEVICE="KINTEX 7"` 选家族。[pll.v:44](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/pll.v#L44) `` `ifndef SIMULATE `` 守卫分三路：

- [pll.v:49-90](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/pll.v#L49-L90)：`DEVICE=="SPARTAN 6"` 时实例化 `PLL_BASE`；
- [pll.v:92-143](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/pll.v#L92-L143)：`DEVICE=="KINTEX 7"` 时实例化 `PLLE2_ADV`（还带 DRP 动态重配端口）；
- [pll.v:145-175](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/pll.v#L145-L175)：`` `else ``（即仿真）用 `always` 按 `clkin_period*div/mult` 翻转生成方波，`locked` 在一拍后拉高。三种实现共用同一组端口 `rst/clkin/locked/clk0..clk5`，调用方完全无感。

**参数化包装器范例 2——`ds_clk_buf`**：[fpga_family/ds_clk_buf.v:2-5](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/ds_clk_buf.v#L2-L5) 用两个参数 `GTX`（是否给 GTX 收发器用）和 `USE_BUF`（0 不加缓冲/1 BUFG/2 BUFH）做组合选择。[ds_clk_buf.v:14-35](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/ds_clk_buf.v#L14-L35) 在 `` `ifndef SIMULATE `` 下按 `GTX` 选 `IBUFDS_GTE2`（GTX 专用差分输入，带 ODIV2/CEB）或普通 `IBUFDS`；仿真分支则 `assign clk_out_i = clk_p`。再加 [ds_clk_buf.v:37-51](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/ds_clk_buf.v#L37-L51) 一层 `generate` 决定是否串 `BUFG`/`BUFH`。一个模块覆盖了「普通差分时钟」与「GTX 参考时钟」两种用法。

**DDR 输出包装器——`ddr_cells`**：[fpga_family/ddr_cells.v:24](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/ddr_cells.v#L24) 同样用 `` `ifndef SIMULATE `` 切换：[ddr_cells.v:25-42](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/ddr_cells.v#L25-L42) 用 `generate for` 给每位实例化一个 `ODDR` 原语；[ddr_cells.v:43-51](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/ddr_cells.v#L43-L51) 仿真分支用两个 `always`（一个采样 data0/data1，一个按时钟边沿交替输出）模拟 DDR 行为。这与 u3-l3 讲的 `afterburner`/`ssb_out` 喂 DDR DAC 直接相关。

**桩如何被「吸收」**：[build-tools/top_rules.mk:204](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/top_rules.mk#L204) 定义了 `UNISIM_CRAP`——一个正则，列出所有 Bedrock 提供桩的原语名。`.bit` 依赖推导规则 [top_rules.mk:207-208](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/build-tools/top_rules.mk#L207-L208) 故意用 `grep -Ee $(UNISIM_CRAP) -v` 把这些原语**从依赖列表里剔除**——因为它们由仿真器通过 `fpga_family/xilinx/` 桩吸收，不应当作工程源码参与综合依赖。

#### 4.4.4 代码实践

**目标**：观察 `` `SIMULATE `` 宏如何切换同一个模块的两种实现。

**步骤**：

1. 读 [fpga_family/pll.v:44-176](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/fpga_family/pll.v#L44-L176)，数清楚三段：Spartan6 原语、7-Series 原语、仿真行为模型。
2. 用 iverilog 预处理直观看到分支选择（不仿真，只展开宏）：

```bash
iverilog -E -DSIMULATE fpga_family/pll.v | grep -nclki0   # 看是否只剩行为模型的 always
iverilog -E fpga_family/pll.v | grep -nc PLLE2_ADV         # 看是否保留真实原语
```

**预期结果**：加 `-DSIMULATE` 时 `PLLE2_ADV`/`PLL_BASE` 原语块被 `` `ifndef `` 排除，只剩下 `always` 翻转的行为模型；不加时则相反，原语块保留、行为模型被 `` `else `` 排除。

**待本地验证**：取决于是否装了 iverilog；若没有，可改为人工通读两段分支确认。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `BUFG` 的桩可以用一个 `buf` 实现，而 `MMCME2_BASE` 只能做成「占位桩」？

> **答案**：`BUFG` 是纯组合的时钟缓冲，对功能仿真语义无影响（仿真里时钟就是普通信号），一个 `buf` 就够了。`MMCM` 是带反馈、分频、相移的复杂模拟单元，精确建模既困难也没必要（多数仿真只关心它「输出几路时钟、locked 何时拉高」），所以 Bedrock 选择把它做成只占参数端口的空壳，让依赖推导/lint 通过即可——真要用它跑时钟行为仿真，得用 `pll.v` 这类包装器的 `` `else `` 行为分支。

**练习 2**：`pll`、`ds_clk_buf`、`ddr_cells` 三个包装器都用 `` `ifndef SIMULATE ``，而不是写两套独立文件。这样做的好处是什么？

> **答案**：单一事实源。同一个模块的「综合用」与「仿真用」实现写在同一个文件里、共用同一组端口，靠一个宏切换。好处是：(1) 端口签名只维护一份，不会两边漂移；(2) 切换综合/仿真只需改一个编译宏（`VG_ALL = -DSIMULATE`），不动代码；(3) 阅读者一眼能看到「真实硬件长什么样、仿真替身长什么样」的对照。

---

## 5. 综合实践

把本讲四条主线串起来，完成一次「**换板不改 RTL**」的思想实验与小验证。

**任务**：假设你有一份面向 KC705（载板）+ FMC112（子卡）的设计，现在要把它迁到 BMB7（载板）+ 同一张 FMC112 子卡上。请回答：

1. **器件层**：KC705 与 BMB7 的 `PART`/`FPGA_FAMILY` 各是什么？`FPGA_FAMILY` 是否变化？这会不会影响 `pll.v` 的 `DEVICE` 参数取值？（查 `board_support/kc705/rules.mk` 与 `board_support/bmb7_kintex/rules.mk`。）
2. **板载约束层**：系统时钟、配置电压等板载资源约束在两块板上是否相同？需要替换哪个文件？（提示：`base.xdc`。）
3. **FMC 子卡约束层**：你的应用端口名（`FMC112_DCO_P[0]` 等）需要改吗？把 [4.3.4](#434-代码实践指定任务) 的命令里第一个输入从 `board_support/kc705/fmc-lpc.xdc` 换成 `board_support/bmb7_kintex/fmc-lpc.xdc` 重跑，观察 PACKAGE_PIN 是否变化、IOSTANDARD 是否变化。
4. **厂家原语层**：你的 RTL 里实例化的 `pll`/`ds_clk_buf`/`ddr_cells` 需要改吗？为什么？

**预期结论**：

- 第 1 问：KC705 是 `xc7k325t`、BMB7 是 `xc7k160t`，但 `FPGA_FAMILY` 都是 `7series`，所以 `pll.v` 的 `DEVICE` 仍是 `KINTEX 7`，**不用改 RTL**。
- 第 2 问：板载资源（时钟频率、DDR、配置电压）不同，需把工程的板载约束从 KC705 的 `base.xdc` 换成 BMB7 的。
- 第 3 问：应用端口名**完全不变**（`FMC112_DCO_P[0]` 还是它），但 PACKAGE_PIN 会从 `AD23` 变成 BMB7 上的对应引脚（`R21` 一带），IOSTANDARD 仍是 `LVDS_25`（子卡决定，与载板无关）。
- 第 4 问：**不用改**，因为 `fpga_family` 包装器已经把家族差异吸收进统一接口与参数分派。

这就是 Bedrock 板级支持与厂家抽象的全部价值——**应用 RTL 与具体板卡/器件解耦，迁移成本被压缩到改几个变量、换几份约束表**。

**待本地验证**：第 3 问的 PACKAGE_PIN 变化请实跑 [4.3.4](#434-代码实践指定任务) 的 awk 命令（换载板 xdc）核对。

## 6. 本讲小结

- Bedrock 用 `board_support/`（板卡差异）和 `fpga_family/`（厂家差异）两个子目录把硬件细节从应用 RTL 里剥离，换板/换器件主要改变量与约束表，不动 DSP/RF 代码。
- 每块载板的「身份卡」是 `board_support/<板>/rules.mk`（`PART`/`FPGA_FAMILY`）+ `base.xdc`（板载资源）+ `fmc-*.xdc`（FMC 连接器），其中 `system_top.xdc = base.xdc + communication.xdc` 直接 `cat` 合并。
- 引脚映射有两套机制：Marble 专用的 `pin_map.csv` + `meta-xdc.py`（硬件引脚名 → 应用端口名，含电平转换与字面量透传）；FMC 子卡通用的 `fmc.map` + `XDC_MAP`/`FMC_MAP` awk（载板表 join 子卡表，PACKAGE_PIN 取自载板、IOSTANDARD 取自子卡）。
- `fpga_family/` 用「同名仿真桩 + 参数化包装器 + `` `ifndef SIMULATE `` 守卫 + `generate` 家族分派」让开源仿真器无需 Xilinx 库也能仿真含原语的设计；`UNISIM_CRAP` 清单让依赖推导把这些桩剔除出综合依赖。
- `top_rules.mk` 用 `XILINX_TOOL` 在 VIVADO/PLANAHEAD/ISE 间分发综合，ISE 流程把生成物放进 `_xilinx/` 目录再移出（`mv _xilinx/$@ $@`），与 u1-l1 讲的「厂家生成物收进 `_<VENDOR_NAME>/`」一脉相承。

## 7. 下一步学习建议

- 学完本讲后，自然过渡到 [u7-l4 工程集成实战](u7-l4-projects-integration.md)：看 `projects/test_marble_family/marble_top.v` 如何把 localbus、Packet Badger、外设与本讲的板级/厂家抽象组装成一个可上板的完整设计，并跑 `make -C projects/ctrace`。
- 若对高速串行感兴趣，回顾 [u5-l3 TCL 驱动的 MGT 配置流程](u5-l3-mgt-tcl-flow.md)，结合本讲的 `fpga_family/mgt/` 与 `ds_clk_buf(GTX=1)` 理解 GTX 参考时钟如何进入器件。
- 想深入约束与时序，建议阅读 Xilinx UG903（XDC 写作）与 UG471（IO 标准），再回头看 `board_support/marble/pin_map.csv` 的字面量区里那些 `create_clock`/`set_property IOB TRUE` 的真实含义。
- 若要做二次开发（加一块新板），最小步骤是：在 `board_support/` 下建目录，写 `rules.mk`（设 `PART`/`FPGA_FAMILY`）、`base.xdc`（板载资源）、`fmc-*.xdc`（若用 FMC），然后在工程 Makefile 里把 `HARDWARE` 指过去即可。
