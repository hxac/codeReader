# 速率核与 PHY 家族拆分（Gig/TenGig）

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 `GigEthCore`（1GbE）与 `TenGigEthCore`（10GbE）这两个「速率核」内部 `core/` 通用目录与各家族 PHY 目录的职责划分。
- 看懂 `GigEthReg` / `TenGigEthReg` 这两个 AXI-Lite 寄存器块：它们用什么模式暴露配置/状态、二者寄存器布局的相同点与差异点。
- 理解为什么这两个速率核都复用同一个 MAC（`EthMacTop`），而真正「家族相关」的只有 PHY（GTX7/GTH7/GTH UltraScale/GTH UltraScale+/GTY UltraScale+ 等收发器）。
- 读懂 `ruckus.tcl` 如何用 `getFpgaArch` 在编译期把通用 `core/` 无条件加载、把家族 PHY 目录有条件加载，并用 `.dcp` 网表把厂商 PHY IP 接进工程。

## 2. 前置知识

本讲承接 u6-l1（以太网 MAC 核心 `EthMacCore`）与 u1-l3（目录结构与文件分类约定），并用到 u3-l2（AXI-Lite 寄存器端点 helper 模式）与 u1-l5（双进程风格）。如果你还不熟悉下面这些概念，先回顾对应讲义：

- **以太网速率等级**：1GbE（千兆）走 1000BASE-X，串行速率为 1.25 Gbps；10GbE（万兆）走 10GBASE-R（如 -SR/-LR），串行速率为 10.3125 Gbps。两者都需要把并行 MAC 数据（GMII/XGMII）经**串行器/解串器（SerDes）**搬到高速串行对（gtTxP/gtRxP 差分对）上。
- **收发器（GT）家族**：Xilinx 不同 FPGA 家族内置的 SerDes 名字不同——7 Series（artix7/kintex7/virtex7/zynq）是 GTP/GTX/GTH，UltraScale 是 GTH，UltraScale+ 是 GTH/GTY。它们逻辑功能相似，但原语（primitive）、时钟结构、复位时序都不一样，必须按家族分别封装。
- **PHY vs MAC**：MAC（Media Access Control）负责成帧、地址过滤、PAUSE 流控等协议逻辑，与家族无关；PHY（Physical Layer）负责把 MAC 的并行接口（如 GMII 8 位）经 SerDes 变成串行比特流，家族强相关。本讲的核心结论之一就是：SURF 让 MAC 只写一遍、PHY 按家族各写一份。
- **`ruckus.tcl`**：每个目录下的 Tcl 构建清单，回答「这个目录哪些 HDL 进工程」，靠 `loadRuckusTcl`（下钻子目录）、`loadSource -lib surf`（登记源文件）、`getFpgaArch`（读当前 FPGA 家族）三个原语工作（见 u1-l2、u1-l3）。
- **`.dcp` 网表**：Design Checkpoint，Vivado 把一个 IP 核综合/实现后产出的「半成品」网表。厂商 PHY IP（如 1000BASE-X 的 `GigEthGtx7Core`）通常以 `.dcp` 形式预置在仓库里，`ruckus.tcl` 用 `loadSource -path xxx.dcp` 把它直接接进工程，而不是每次重新跑 IP 生成。

> 一个直觉：如果把一个速率核比作「电脑主板」，那么 `core/` 是与 CPU 插槽无关的 BIOS/寄存器层（写一遍通用），`gtx7`/`gth7`/`gtyUltraScale+` 这些家族目录是不同 CPU 插槽的供电与时序胶水（按型号各做一份），而 `.dcp` 则是焊死在插槽里的 PHY 芯片。

## 3. 本讲源码地图

本讲涉及的关键文件（Gig 与 TenGig 严格对称）：

| 文件 | 作用 |
|------|------|
| `ethernet/GigEthCore/ruckus.tcl` | 1GbE 顶层清单：无条件加载 `core/`，按 `getFpgaArch` 选择家族 PHY 目录 |
| `ethernet/GigEthCore/core/rtl/GigEthPkg.vhd` | 1GbE 配置/状态记录 + 初值常量（`PAUSE_512BITS_C` 等） |
| `ethernet/GigEthCore/core/rtl/GigEthReg.vhd` | 1GbE AXI-Lite 寄存器块（含 `EN_AXI_REG_G` 直通模式、看门狗复位） |
| `ethernet/GigEthCore/gtx7/rtl/GigEthGtx7.vhd` | GTX7 家族集成胶水：串 AxiLiteAsync→Crossbar→MAC→Reg + 厂商 PHY IP |
| `ethernet/GigEthCore/gtx7/rtl/GigEthGtx7Wrapper.vhd` | GTX7 顶层：差分参考时钟 + MMCM + 一个 QUAD（最多 4 路）1GbE |
| `ethernet/TenGigEthCore/ruckus.tcl` | 10GbE 顶层清单：结构与 1GbE 平行 |
| `ethernet/TenGigEthCore/core/rtl/TenGigEthPkg.vhd` | 10GbE 配置/状态记录（多出 PCS/PMA 控制位与 GT/QPLL 状态） |
| `ethernet/TenGigEthCore/core/rtl/TenGigEthReg.vhd` | 10GbE AXI-Lite 寄存器块（多出 PCS/PMA 配置寄存器） |
| `ethernet/TenGigEthCore/core/rtl/TenGigEthRst.vhd` | 10GbE 专用 GT 复位/时钟时序机（1GbE 不需要） |

观察这张地图你会发现一个规律：`core/` 里只有 `Pkg` 与 `Reg`（TenGig 多一个 `Rst`），**完全不出现任何收发器原语**；所有 `gtx7`/`gth7`/`gthUltraScale` 等家族目录里才有 `*Wrapper.vhd`、家族胶水 `.vhd` 与 `.dcp` 网表。这正是本讲要讲清的「通用 vs 家族」拆分。

## 4. 核心概念与源码讲解

本讲按「自顶向下、由抽象到具体」拆成四个最小模块：先看 `core/` 的家族无关抽象（4.1），再看它对外暴露的 AXI-Lite 寄存器接口（4.2），接着进入家族 PHY 封装（4.3），最后看 `ruckus.tcl` 如何把它们按家族拼装进工程（4.4）。

### 4.1 通用速率核：core/ 的家族无关抽象

#### 4.1.1 概念说明

`core/` 目录存放「与具体 GT 收发器家族无关」的部分：配置/状态记录（`Pkg`）和把这些记录映射到 AXI-Lite 的寄存器块（`Reg`）。它的设计目标是——**无论你最终把 1GbE 跑在 GTX7 还是 GTY UltraScale+ 上，软件看到的寄存器布局、配置语义都一样**。这样 PyRogue 软件镜像（见 u9-l4）也只需写一份。

为什么能把家族相关的部分切干净？因为家族相关的东西只有两类：①厂商 PHY IP（1000BASE-X / 10GBASE-R 子层 + SerDes），②它的时钟与复位时序。这两类都封装在家族目录里，并通过两条「窄接口」与 `core/` 互通：

- **配置总线**：`config : out <Speed>ConfigType`——`Reg` 算出的配置（MAC 配置、PHY 控制）经此流向家族 PHY。
- **状态总线**：`status : in <Speed>StatusType`——家族 PHY 采集的状态（phyReady、GT 复位完成、core_status 等）经此回流给 `Reg` 暴露给软件。

#### 4.1.2 核心流程

一个速率核在运行期的数据与控制流如下（以 1GbE 为例）：

```text
                   软件 (PyRogue / CPU)
                          │ AXI-Lite
                          ▼
        ┌─────────────────────────────────────────┐
        │  core/: GigEthReg                        │   ← 4.1 / 4.2 讲这里
        │  - 把 AXI-Lite 读写 → GigEthConfigType   │
        │  - 把 GigEthStatusType → AXI-Lite 只读   │
        └───────────┬─────────────────┬───────────┘
            config  │                 │ status
                    ▼                 ▼
        ┌─────────────────────────────────────────┐
        │  家族目录/: GigEthGtx7 (集成胶水)        │   ← 4.3 讲这里
        │  ┌──────────┐  ┌─────────┐  ┌─────────┐ │
        │  │ GigEthReg│→│EthMacTop│↔│ PHY IP  │ │
        │  │  (MAC配) │  │(共享MAC)│  │(.dcp网表)│ │
        │  └──────────┘  └─────────┘  └────┬────┘ │
        └──────────────────────────────────┼──────┘
                                           │ gtTx/RxP/N (差分串行对)
                                           ▼
                                      线缆/光纤
```

关键点：`EthMacTop`（u6-l1 讲的 MAC）属于家族无关层，但它**不在 `core/` 里例化**，而是在家族胶水里例化——因为 MAC 的 PHY 接口（GMII/XGMII）要直接连到厂商 PHY IP。`core/` 只负责把「软件想怎么配 MAC/PHY」翻译成 `config` 记录，家族胶水再把它分发下去。

#### 4.1.3 源码精读

先看 1GbE 的配置/状态记录定义：

[GigEthPkg.vhd:29-43](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/core/rtl/GigEthPkg.vhd#L29-L43) 定义了 `GigEthConfigType` 与 `GigEthStatusType` 两个记录，并给出 `GIG_ETH_CONFIG_INIT_C` 初值。注意 `macConfig` 字段直接复用 `EthMacConfigType`（来自 u6-l1 的 `EthMacPkg`），`coreConfig : slv(4 downto 0)` 则是写给 1000BASE-X PHY IP 的 `configuration_vector`（5 位），`coreStatus : slv(15 downto 0)` 是 PHY IP 回读的 `status_vector`。这五位的家族无关性是「约定出来的」——所有家族的 1GbE PHY IP 都遵守同一个 5 位配置向量语义。

`GigEthPkg.vhd` 顶部还有一个常量 [GigEthPkg.vhd:25](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/core/rtl/GigEthPkg.vhd#L25)：`PAUSE_512BITS_C : positive := 64`。它把 IEEE 802.3x 的「512 比特 = 1 个 pause quanta」换算成 1GbE 在 8 位 GMII、125 MHz 下的时钟周期数（512 bit ÷ 8 bit = 64 拍）。注意它是按 **1GbE 数据周期**算的，10GbE 不会复用这个值（10GbE 的 quanta 换算在 `EthMacTop` 里按 `PHY_TYPE_G` 自适应，见 u6-l1）。

再看 10GbE 的对应记录：

[TenGigEthPkg.vhd:25-57](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/TenGigEthCore/core/rtl/TenGigEthPkg.vhd#L25-L57) 定义 `TenGigEthConfig` / `TenGigEthStatus`。对比 1GbE，差异恰好反映「10GbE 的 PCS/PMA 层更复杂、GT 时序更讲究」：

- `TenGigEthConfig` 多出 5 个 PHY 控制位：`pma_pmd_type`（3 位，默认 `"111"` 即 10GBASE-SR）、`pma_loopback`、`pma_reset`、`pcs_loopback`、`pcs_reset`。1GbE 没有这些，因为 1000BASE-X 把它们塞进了那 5 位 `coreConfig` 向量。
- `TenGigEthStatus` 多出一串 GT/QPLL 状态：`txDisable`、`sigDet`、`txFault`、`gtTxRst`、`gtRxRst`、`rstCntDone`、`qplllock`、`txRstdone`、`rxRstdone`、`txUsrRdy`，以及 8 位的 `core_status`。这些是 10GBASE-R 子层 + GT 复位时序机（`TenGigEthRst`）才有的状态量。

这两个包都不 `use` 任何 `unisim`（厂商原语库），是纯家族无关的 VHDL——因此可以被 GHDL 仿真分析，不依赖 Vivado。

#### 4.1.4 代码实践

**目标**：验证「`core/` 完全家族无关、可独立分析」这一论断。

**操作步骤**：

1. 打开 `ethernet/GigEthCore/core/rtl/GigEthPkg.vhd` 与 `ethernet/TenGigEthCore/core/rtl/TenGigEthPkg.vhd`。
2. 在两个文件里搜索 `library unisim` 或 `use unisim`。
3. 对比 `GigEthConfigType` 与 `TenGigEthConfig` 的字段，列出 TenGig 多出的字段。

**需要观察的现象**：两个 `Pkg` 都不引用 `unisim`；`macConfig` 字段两边类型相同（`EthMacConfigType`）。

**预期结果**：`core/` 只依赖 `StdRtlPkg` + `EthMacPkg` + 自身，与 GT 家族完全解耦。

### 4.2 AXI-Lite 寄存器接口：GigEthReg / TenGigEthReg

#### 4.2.1 概念说明

`*Reg` 是 `core/` 对外的门面：它把软件的 AXI-Lite 读写翻译成 `config` 记录，把 `status` 记录回流成只读寄存器。它严格沿用 u3-l2 讲过的「helper 进程四步骨架」：`axiSlaveWaitTxn` 解码事务 → `axiSlaveRegister(R)` 逐行绑地址 → `axiSlaveDefault` 兜底回 `DECERR`。

它还有一个对硬件工程师很重要的设计：`EN_AXI_REG_G : boolean`。当上层不需要软件配置（例如一个固定配置的嵌入式链路），把它设为 `false`，整个 AXI-Lite 从机退化成「直通模式」——寄存器块变成一段纯组合逻辑，配置直接从 `localMac` 端口与（1GbE 才有的）看门狗复位推出，AXI 总线直接回空的 `DECERR`。这样省掉一整套寄存器与状态计数器的逻辑资源。

#### 4.2.2 核心流程

`*Reg` 内部有两种互斥实现，由 `EN_AXI_REG_G` 在编译期 `generate` 二选一：

```text
EN_AXI_REG_G = false  →  GEN_BYPASS（直通）
   config ← combinationally from localMacSync (+ wdtRst for Gig)
   axiReadSlave/WriteSlave ← EMPTY_DECERR  (软件访问必回 DECERR)

EN_AXI_REG_G = true   →  GEN_REG（完整寄存器）
   comb 进程（u3-l2 四步骨架）:
     axiSlaveWaitTxn(regCon,...)
     for i in 0..31: axiSlaveRegisterR(regCon, 4*i, ..., cntOut[i])  ← 状态计数器
     axiSlaveRegisterR(regCon, 0x100, ..., statusOut)                ← 状态位汇总
     axiSlaveRegisterR(regCon, 0x108, ..., core_status/coreStatus)   ← PHY 状态向量
     axiSlaveRegister (regCon, 0x200/0x204, ..., macAddress)         ← MAC 地址(可读写)
     ... (MAC 配置 / pauseThresh / 复位控制)
     axiSlaveDefault(regCon, ..., AXI_RESP_DECERR_C)
   seq 进程: rising_edge(clk) → r <= rin after TPD_G
   另例化 SyncStatusVector: 把 status 位同步+计数到 cntOut
```

`SyncStatusVector`（见 u2-l1）在这里扮演「状态计数器农场」：32 个状态位各自维护一个 32 位计数器，软件读地址 `0x000~0x07C`（每 4 字节一个）即可得到每个事件的发生次数，`0xF00` 的 `rollOverEn` 决定计数器是「饱和」还是「回卷」。

#### 4.2.3 源码精读

两个 `Reg` 的实体声明几乎一模一样，差异只在 `config`/`status` 的类型：

[GigEthReg.vhd:27-45](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/core/rtl/GigEthReg.vhd#L27-L45) 声明 `GigEthReg`：端口含 `localMac`、时钟复位、四通道 AXI-Lite，以及 `config : out GigEthConfigType` / `status : in GigEthStatusType`。`EN_AXI_REG_G : boolean := false` 是默认直通。[TenGigEthReg.vhd:27-45](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/TenGigEthCore/core/rtl/TenGigEthReg.vhd#L27-L45) 是 10GbE 对应声明，类型换成 `TenGigEthConfig`/`TenGigEthStatus`。

先看直通模式（1GbE）：

[GigEthReg.vhd:96-110](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/core/rtl/GigEthReg.vhd#L96-L110) 是 `GEN_BYPASS`。注意三点：①AXI 从机直接赋 `AXI_LITE_READ_SLAVE_EMPTY_DECERR_C`，软件任何访问都立刻拿到 `DECERR`；②`config` 由一段组合进程从 `GIG_ETH_CONFIG_INIT_C` + `localMacSync` 推出；③`retVar.softRst := wdtRst`——即使不要寄存器，也保留看门狗保护。10GbE 的直通 [TenGigEthReg.vhd:86-99](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/TenGigEthCore/core/rtl/TenGigEthReg.vhd#L86-L99) 结构相同，但没有 `wdtRst`（10GbE 的复位保护由 `TenGigEthRst` 负责）。

那 1GbE 的看门狗从哪来？看模块顶部：

[GigEthReg.vhd:78-85](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/core/rtl/GigEthReg.vhd#L78-L85) 例化 `WatchDogRst`，监测 `status.phyReady`，超时 `getTimeRatio(125.0E+6, 0.5)` 拍（即 125 MHz 下 0.5 秒）没跳变就拉 `wdtRst`，最终喂给 `config.softRst` 把整个核软复位一遍——这是 1GbE 防链路卡死的自愈机制。

寄存器主体（1GbE）：

[GigEthReg.vhd:164-194](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/core/rtl/GigEthReg.vhd#L164-L194) 是 `comb` 进程的地址映射核心。这段用 `for` 循环把 32 个计数器铺到 `0x000~0x07C`，再把状态汇总铺到 `0x100`/`0x108`，MAC 地址铺到 `0x200/0x204`（可读写），MAC 配置铺到 `0x21C/0x228/0x22C/0x800`，最后 `0xF00`(rollOverEn)/`0xFF4`(cntRst)/`0xFF8`(softRst)/`0xFFC`(hardRst) 是控制位。结尾 `axiSlaveDefault(..., AXI_RESP_DECERR_C)` 给所有未映射地址回 `DECERR`——这正是 u3-l2 的兜底约定。

10GbE 的寄存器主体几乎照抄，但多出一段 PCS/PMA 配置：

[TenGigEthReg.vhd:185-189](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/TenGigEthCore/core/rtl/TenGigEthReg.vhd#L185-L189) 把 `pma_pmd_type`/`pma_loopback`/`pma_reset`/`pcs_loopback`/`pcs_reset` 映射到 `0x230~0x240`。这是 10GbE 相对 1GbE 在寄存器层最显著的差异——因为 10GBASE-R 的 PCS/PMA 是独立可配/可环回的子层。

两个 `Reg` 在状态采集上还有一个细节差异，反映「时钟域是否同源」：

- 1GbE 的 `SyncStatusVector` 设 `COMMON_CLK_G => true`（[GigEthReg.vhd:103-121 附近](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/core/rtl/GigEthReg.vhd#L114-L121)），因为 1GbE 的 MAC 与寄存器都在 125 MHz `sysClk125` 域。
- 10GbE 的 `SyncStatusVector` 设 `COMMON_CLK_G => false`（[TenGigEthReg.vhd:103-110 附近](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/TenGigEthCore/core/rtl/TenGigEthReg.vhd#L103-L110)），因为 10GbE 的 GT 状态可能来自不同时钟域，需要真正的跨域同步。

把两者的寄存器布局并列成一张表（节选）：

| 地址 | 字段 | 1GbE (GigEthReg) | 10GbE (TenGigEthReg) |
|------|------|:---:|:---:|
| `0x000~0x07C` | 32 个状态计数器 | ✔ `SyncStatusVector` | ✔ `SyncStatusVector` |
| `0x100` | status 位汇总 | ✔ | ✔ |
| `0x108` | PHY 状态向量 | `coreStatus`(16b) | `core_status`(8b) |
| `0x200/0x204` | MAC 地址(低/高) | 读写 | 只读+读写混 |
| `0x21C/0x228/0x22C` | pauseTime/filtEnable/pauseEnable | ✔ | ✔ |
| `0x230~0x240` | PCS/PMA 配置 | ✘ | ✔（10GbE 专有）|
| `0x800` | pauseThresh | ✔ | ✔ |
| `0xF00/0xFF4/0xFF8/0xFFC` | rollOverEn/cntRst/softRst/hardRst | ✔ | ✔ |

> 这张表也是 PyRogue 软件镜像（u9-l4）的「地址契约」：RTL 改了某个寄存器偏移，对应 `python/surf/...` 下的设备模型必须同步改，否则软件读到的就是错位的数据。

#### 4.2.4 代码实践

**目标**：通过阅读源码，重建 `GigEthReg` 与 `TenGigEthReg` 的寄存器布局差异，并解释 `EN_AXI_REG_G` 的资源影响。

**操作步骤**：

1. 在 `GigEthReg.vhd` 的 `comb` 进程里数清楚 `axiSlaveRegister` / `axiSlaveRegisterR` 各调用了多少次、对应哪些地址。
2. 在 `TenGigEthReg.vhd` 里做同样的事，重点看 `0x230~0x240` 这段。
3. 分别找到两个文件的 `GEN_BYPASS` 块，确认 `EN_AXI_REG_G = false` 时 `SyncStatusVector` 是否还被例化。

**需要观察的现象**：`GEN_BYPASS` 块里没有任何 `SyncStatusVector`、没有 `comb`/`seq` 进程——这些只在 `GEN_REG` 里。

**预期结果**：`EN_AXI_REG_G = false` 会把 32 个 32 位计数器、状态同步器、整套寄存器全部省掉，只剩一段组合逻辑推 `config`。

**待本地验证**：如果你有 Vivado 工程，可对同一核分别设 `EN_AXI_REG_G=true/false` 综合，对比 LUT/FF/BRAM 占用差值（预期直通模式显著更小）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `GigEthReg` 在 `EN_AXI_REG_G=false` 时仍保留 `WatchDogRst`，而 `TenGigEthReg` 连直通模式都没有看门狗？

参考答案：1GbE 把「链路卡死后软复位自愈」的职责放在 `Reg` 里，靠 `WatchDogRst` 监测 `phyReady` 触发 `softRst`；10GbE 的 GT/QPLL 复位时序更复杂，由专门的 `TenGigEthRst` 状态机负责（见 4.3），所以 `Reg` 不再重复这份保护。

**练习 2**：软件往 `0xFFC` 写 1 会发生什么？写 `0x300` 又会怎样？

参考答案：`0xFFC` 是 `hardRst`，写 1 会在 `comb` 里置 `v.hardRst := '1'`，触发 [GigEthReg.vhd:197-205](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/core/rtl/GigEthReg.vhd#L197-L205) 的同步复位分支，把 `config` 复位成 `GIG_ETH_CONFIG_INIT_C`。`0x300` 没有任何 `axiSlaveRegister` 绑定，会被 `axiSlaveDefault` 兜底回 `AXI_RESP_DECERR_C`。

### 4.3 家族 PHY 封装：Wrapper + 集成胶水 + 厂商 IP

#### 4.3.1 概念说明

家族目录（如 `gtx7/`、`gth7/`、`gthUltraScale+/`、`gtyUltraScale+/`）存放「家族强相关」的三样东西：

1. **集成胶水**（`GigEthGtx7.vhd` 这类）：把 `core/` 的 `Reg`、共享的 `EthMacTop`、AXI-Lite 异步桥与交叉开关、（10GbE 的）复位时序机、以及厂商 PHY IP 全部连起来。这是「主集成」文件。
2. **顶层 Wrapper**（`GigEthGtx7Wrapper.vhd` 这类）：处理差分参考时钟（`IBUFDS_GTE2`）、MMCM 时钟管理、以及「一个 QUAD 最多 4 路」的批量例化。它对外的 `gtTxP/gtRxP` 就是 FPGA 引脚上的高速串行对。
3. **厂商 PHY IP 网表**（`.dcp`）：Vivado 预生成的 1000BASE-X / 10GBASE-R 子层 + SerDes，以 `component` 声明 + `.dcp` 形式接入。

本模块最重要的结论是：**所有家族的胶水里例化的 MAC 都是同一个 `EthMacTop`**（u6-l1）。这正是「MAC 写一遍、PHY 各写一份」的体现——你可以用一条命令验证（见 4.3.4）。

#### 4.3.2 核心流程

以 GTX7 家族为例，集成胶水 `GigEthGtx7` 的内部拓扑：

```text
axiLiteClk/Rst (软件域)
     │
     ▼
AxiLiteAsync  ── 跨到 sysClk125 域 ──►  syncAxil*
     │
     ▼
AxiLiteCrossbar (1→2)
   ├─ ETH_AXIL_C (base+0x0000, 12bit) ─► GigEthReg ─► config/status
   └─ DRP_AXIL_C (base+0x1000, 12bit) ─► AxiLiteToDrp ─► GT 的 DRP 端口
                                                        (软件可直接读写收发器寄存器)

config.macConfig ─┐
                  ▼
              EthMacTop  ◄───► dmaObMaster/dmaIbMaster (AXI-Stream 数据)
                  │
                  ▼ GMII (8 位并行)
            GigEthGtx7Core  ◄─── .dcp 厂商 PHY IP
                  │
                  ▼ gtTxP/gtRxP (高速串行差分对)
```

两个值得注意的设计：

- **交叉开关把 PHY 的 DRP 也挂上 AXI-Lite**：`AxiLiteCrossbar` 把一段地址空间切给 `GigEthReg`（MAC/PHY 配置），另一段切给 `AxiLiteToDrp`（GT 收发器的动态重配置端口）。于是软件通过一个 AXI-Lite 从机，既能配 MAC，又能直接读写 GTX 的预加重/差分摆幅等寄存器。
- **`EthMacTop` 的 `PHY_TYPE_G`**：在 1GbE 里设成 `"GMII"`（8 位并行），PHY IP 把 GMII ↔ 串行；10GbE 里设成 `"XGMII"`。MAC 自身对家族无感。

10GbE 的胶水结构平行，但多一个 `U_TenGigEthRst`（例化 `core/` 里的 `TenGigEthRst`）来 sequencing GT 与 QPLL 的复位，并把状态机的输出回填到 `TenGigEthStatus`。

#### 4.3.3 源码精读

先看顶层 Wrapper 怎么处理时钟与多通道：

[GigEthGtx7Wrapper.vhd:30-85](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/gtx7/rtl/GigEthGtx7Wrapper.vhd#L30-L85) 是 `GigEthGtx7Wrapper` 的实体。注意 `NUM_LANE_G : natural range 1 to 4`——一个 GT QUAD 有 4 个通道，这个 Wrapper 可以一次例化 1~4 路 1GbE，端口都用 `Slv48Array`/`AxiStreamMasterArray` 按 lane 索引。

[GigEthGtx7Wrapper.vhd:110-160](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/gtx7/rtl/GigEthGtx7Wrapper.vhd#L110-L160) 处理参考时钟：`IBUFDS_GTE2` 把差分 `gtClkP/N` 转成单端，经 `BUFG` 后作为 `refClk`；`PwrUpRst` 产生上电复位；`ClockManager7`（一个 MMCM）从 `refClk` 生成 `sysClk125`（MAC/PHY 主时钟）与 `sysClk62`（GT user clock）。这一段是 7 Series 家族特有的时钟结构——UltraScale+ 家族的 Wrapper 会换用 `ClockManagerUltraScale` 等不同原语，这正是「家族相关」的典型例子。

[GigEthGtx7Wrapper.vhd:165-211](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/gtx7/rtl/GigEthGtx7Wrapper.vhd#L165-L211) 的 `GEN_LANE` for-generate 按 `NUM_LANE_G` 批量例化 `GigEthGtx7`，把每一路的 DMA 流、AXI-Lite、`gtTx/RxP/N` 分别连出去。

接着进入集成胶水 `GigEthGtx7` 内部。先看厂商 PHY IP 的 `component` 声明：

[GigEthGtx7.vhd:80-129](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/gtx7/rtl/GigEthGtx7.vhd#L80-L129) 声明 `GigEthGtx7Core`（即 `.dcp` 网表对应的黑盒）。端口能看到典型的 1000BASE-X IP 接口：GMII 收发（`gmii_txd/rxd` 等）、DRP（`drpaddr/drpdi/drpdo`）、GT 物理引脚（`txp/rxp`）、QPLL 输入、以及 `configuration_vector(4 downto 0)` / `status_vector(15 downto 0)`——后者正是 `GigEthStatusType.coreStatus` 的来源。

[GigEthGtx7.vhd:134-146](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/gtx7/rtl/GigEthGtx7.vhd#L134-L146) 定义 AXI-Lite 交叉开关配置：两个窗口 `ETH_AXIL_C`（base+`0x0000`，`addrBits=>12`，即 4 KiB）与 `DRP_AXIL_C`（base+`0x1000`，`addrBits=>12`）。这正是 4.3.2 图里「一段给 Reg、一段给 DRP」的出处，也复用了 u3-l3 的 `AxiLiteCrossbar` 配置范式。

共享 MAC 的例化：

[GigEthGtx7.vhd:237-267](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/gtx7/rtl/GigEthGtx7.vhd#L237-L267) 例化 `EthMacTop`，关键泛型 `PHY_TYPE_G => "GMII"`、`PAUSE_512BITS_G => PAUSE_512BITS_C`（用 4.1.3 讲的常量）。它的 `ethConfig`/`ethStatus` 直接连到 `config.macConfig`/`status.macStatus`——也就是说，`GigEthReg` 算出的 MAC 配置经此进入 MAC，MAC 状态经此回流。GMII 信号（`gmiiTxd/gmiiRxd` 等）则双向连到下面的 PHY IP。

PHY IP 的例化：

[GigEthGtx7.vhd:272-325](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/gtx7/rtl/GigEthGtx7.vhd#L272-L325) 例化 `GigEthGtx7Core`，把 GMII 与 PHY IP 对接，`configuration_vector => config.coreConfig`、`status_vector => status.coreStatus`，并把 `gtTxP/gtRxP` 引到顶层。注意 `gt0_qplloutclk_in => '0'`——1GbE 单通道用 CPLL，不用 QPLL（10GbE 才用 QPLL，见 `TenGigEthRst`）。

DRP 桥接：

[GigEthGtx7.vhd:331-352](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/gtx7/rtl/GigEthGtx7.vhd#L331-L352) 例化 `AxiLiteToDrp`，把交叉开关的 `DRP_AXIL_C` 窗口翻译成 GT 的 DRP 握手（`drpAddr`/`drpDi`/`drpDo`/`drpWe`/`drpEn`）。于是软件可以经 `base+0x1000` 直接调收发器寄存器。

最后是 `core/` 寄存器块的例化：

[GigEthGtx7.vhd:357-374](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/gtx7/rtl/GigEthGtx7.vhd#L357-L374) 例化 `GigEthReg`，把交叉开关的 `ETH_AXIL_C` 窗口接给它，`config`/`status` 与本模块的内部信号互连。至此，「软件 AXI-Lite → Reg → config → MAC/PHY」与「PHY/MAC → status → Reg → 软件」两条环路闭合。

再看 10GbE 专有的复位时序机：

[TenGigEthRst.vhd:27-46](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/TenGigEthCore/core/rtl/TenGigEthRst.vhd#L27-L46) 是 `TenGigEthRst` 的实体。它处理 10GbE 必需的 GT/QPLL 复位握手：输入 `qplllock`，输出 `gtTxRst/gtRxRst/qpllRst/txUsrRdy`，并管理 322 MHz `txClk322` 到 `txUsrClk/txUsrClk2` 的用户时钟。这个模块虽然源码放在 `core/`，但它操作的是 GT 家族相关的复位时序，因此只被家族胶水例化。它由家族胶水例化（例如 `ethernet/TenGigEthCore/gtx7/rtl/TenGigEthGtx7.vhd` 的 `U_TenGigEthRst`），把 `rstCntDone/qplllock` 等状态回填到 `TenGigEthStatus`——这正是 4.1.3 看到 10GbE 状态记录多出一串 GT 状态位的原因。

#### 4.3.4 代码实践

**目标**：用证据确认「所有家族胶水都复用同一个 `EthMacTop`，家族差异只在 PHY IP/时钟/复位」。

**操作步骤**：

1. 在仓库根执行（只读检索）：

   ```bash
   grep -rl "entity surf.EthMacTop" ethernet/GigEthCore ethernet/TenGigEthCore | sort
   ```

2. 对比 1GbE 与 10GbE 各自家族目录的文件清单，看哪些文件名成对出现（`*Wrapper.vhd`、家族胶水 `.vhd`），哪些是单边独有的（如 `TenGigEth*Clk.vhd`、`TenGigEth*Rst.vhd`）。

3. 打开任意一个 10GbE 家族胶水（如 `ethernet/TenGigEthCore/gtx7/rtl/TenGigEthGtx7.vhd`），定位 `U_TenGigEthRst` 与 `U_MAC` 两个例化，确认它们分别连了什么。

**需要观察的现象**：步骤 1 应列出 7 个 1GbE 家族 + 5 个 10GbE 家族共 12 个文件——每一个都例化了 `EthMacTop`。

**预期结果**：MAC 层（`EthMacTop`）在两个速率核、所有家族里都是同一个；家族目录之间真正不同的是参考时钟原语、PHY IP 网表（`.dcp`）与复位时序。

### 4.4 ruckus.tcl 的架构选择：getFpgaArch 分流

#### 4.4.1 概念说明

前面三个模块讲的是「源码怎么拆」，本模块讲「构建时怎么拼」。每个速率核顶层的 `ruckus.tcl` 做两件事：

1. **无条件加载 `core/`**——因为它家族无关，任何工程都要。
2. **按 `getFpgaArch` 有条件加载家族目录**——`getFpgaArch` 返回当前工程的 FPGA 家族字符串（如 `artix7`、`kintex7`、`zynquplus`、`zynquplusRFSOC`、`virtexuplus` 等），`ruckus.tcl` 用一串 `if { ${family} eq {...} }` 把对应家族目录 `loadRuckusTcl` 进来。

而家族目录自己的 `ruckus.tcl` 还做第三件事：**按 Vivado 版本门控，并加载 `.dcp` 网表**。因为厂商 PHY IP 的 `.dcp` 是用特定 Vivado 版本生成的，版本不够就只打印警告、不进工程。

这套机制让同一份 SURF 源码树能同时支撑 7 Series、UltraScale、UltraScale+ 多代 FPGA，而使用者只需在工程里设好 `PRJ_PART`（器件型号），`ruckus` 就能自动挑出正确的 PHY 源。

#### 4.4.2 核心流程

```text
顶层 ruckus.tcl (GigEthCore/ 或 TenGigEthCore/)
 │
 ├─ loadRuckusTcl "$::DIR_PATH/core"        # 无条件：Pkg + Reg (+Rst)
 │      └─ core/ruckus.tcl: loadSource -lib surf -dir rtl
 │
 └─ set family [getFpgaArch]                # 读家族
         if family == kintex7    → loadRuckusTcl gtx7
         if family == virtex7    → loadRuckusTcl gth7
         if family == zynquplus  → loadRuckusTcl gthUltraScale+ , gtyUltraScale+
         ... (见 4.4.3 表)

家族 ruckus.tcl (如 gtx7/ruckus.tcl)
 │
 └─ if $VIVADO_VERSION >= 2016.4:
        loadSource -lib surf -dir rtl                 # Wrapper + 胶水
        loadSource -lib surf -path images/*.dcp       # 厂商 PHY IP 网表
     else:
        puts "WARNING: 需要 Vivado 2016.4+"
```

#### 4.4.3 源码精读

先看 1GbE 顶层清单的全貌：

[GigEthCore/ruckus.tcl:4-48](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/ruckus.tcl#L4-L48) 是 1GbE 的家族分流表。第 5 行 `loadRuckusTcl "$::DIR_PATH/core"` 无条件加载通用 `core/`；第 8 行 `set family [getFpgaArch]` 取家族；随后是一串 `if`。注意几个细节：

- 同一家族可加载多个 PHY 目录：如 `kintexuplus`/`zynquplus`/`zynquplusRFSOC` 同时加载 `gthUltraScale+`、`gtyUltraScale+`、`lvdsUltraScale`（[GigEthCore/ruckus.tcl:36-42](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/ruckus.tcl#L36-L42)）。这是因为 UltraScale+ 器件同时有 GTH 与 GTY 两种收发器，还支持 LVDS GPIO 模式的 1GbE。
- 家族选择还能按器件型号细分：`zynq` 家族里再用正则 `XC7Z(015|012)` 区分小封装的 Zynq 7007S/7014S（用 `gtp7`）与其余（用 `gtx7`）（[GigEthCore/ruckus.tcl:18-24](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/ruckus.tcl#L18-L24)）。`$::env(PRJ_PART)` 是工程设定的器件型号字符串。

10GbE 的分流表结构完全平行，但家族集合更小（10GbE 不支持 GTP/LVDS 这些低端或 GPIO 通路）：

[TenGigEthCore/ruckus.tcl:4-34](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/TenGigEthCore/ruckus.tcl#L4-L34) 是 10GbE 顶层清单。同样第 5 行先加载 `core/`，再按 `getFpgaArch` 选 `gtx7`/`gth7`/`gthUltraScale`/`gthUltraScale+`/`gtyUltraScale+`。对比 1GbE，你会发现它没有 `artix7`/`gtp7`/`lvdsUltraScale` 这些分支——10GbE 只跑在够档次的器件上。

通用 `core/` 的清单极其简单：

[GigEthCore/core/ruckus.tcl:5](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/core/ruckus.tcl#L5) 只有一行 `loadSource -lib surf -dir "$::DIR_PATH/rtl"`——把 `core/rtl/` 下的 `Pkg`、`Reg`（TenGig 还有 `Rst`）登记进 `surf` 库。没有任何家族判断、没有版本判断、没有 `.dcp`，所以它可以被 GHDL 纯仿真分析（见 u9-l1 的回归栈）。

家族目录的清单则多了版本门控与 `.dcp`：

[GigEthCore/gtx7/ruckus.tcl:5-9](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/gtx7/ruckus.tcl#L5-L9) 在 `VIVADO_VERSION >= 2016.4` 时既加载 `rtl/` 目录，又加载 `images/GigEthGtx7Core.dcp`；版本不够只 `puts` 警告。不同家族要求的最小 Vivado 版本不同——例如 `gthUltraScale+` 要求 `>= 2017.3`（[GigEthCore/gthUltraScale+/ruckus.tcl:5](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/gthUltraScale+/ruckus.tcl#L5)），`gtyUltraScale+` 要求 `>= 2018.3`（10GbE 的 [TenGigEthCore/gtyUltraScale+/ruckus.tcl:5-9](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/TenGigEthCore/gtyUltraScale+/ruckus.tcl#L5-L9)）。版本门槛对应「这个 PHY IP 的 `.dcp` 是用哪版 Vivado 生成的」。

把两套分流表并列对照（节选）：

| FPGA 家族 (`getFpgaArch`) | 1GbE 加载的 PHY 目录 | 10GbE 加载的 PHY 目录 |
|------|------|------|
| `artix7` | `gtp7` | ✘（不支持）|
| `kintex7` | `gtx7` | `gtx7` |
| `zynq`（非 015/012）| `gtx7` | `gtx7` |
| `zynq`（XC7Z015/012）| `gtp7` | ✘ |
| `virtex7` | `gth7` | `gth7` |
| `kintexu`/`virtexu` | `gthUltraScale` + `lvdsUltraScale` | `gthUltraScale` |
| `kintexuplus`/`zynquplus`/`zynquplusRFSOC` | `gthUltraScale+` + `gtyUltraScale+` + `lvdsUltraScale` | `gthUltraScale+` + `gtyUltraScale+` |
| `virtexuplus`/`virtexuplusHBM` | `gtyUltraScale+` + `lvdsUltraScale` | `gtyUltraScale+` |

#### 4.4.4 代码实践

**目标**：亲手读懂「同一份顶层清单，不同家族加载不同子集」的分发逻辑，并解释为什么 `core/` 没有任何 `if`。

**操作步骤**：

1. 打开 `ethernet/GigEthCore/ruckus.tcl` 与 `ethernet/TenGigEthCore/ruckus.tcl`，对照上表逐行核对 `if` 分支。
2. 假设你的工程器件是 `xczu28dr`（Zynq UltraScale+ RFSoC），手动推断：1GbE 会加载哪些目录？10GbE 呢？
3. 打开任一家族 `ruckus.tcl`（如 `gthUltraScale+/ruckus.tcl`），找到它要求的最小 Vivado 版本与加载的 `.dcp` 文件名。
4. （可选，待本地验证）如果你装了 ruckus + Vivado，在一个 UltraScale+ 工程里 `source` 这个顶层 `ruckus.tcl`，观察日志里实际 `loadSource` 了哪些 `rtl` 与 `.dcp`。

**需要观察的现象**：`core/ruckus.tcl` 里完全没有 `getFpgaArch` 与 `VIVADO_VERSION`；所有家族判断都在顶层 `ruckus.tcl`，所有版本判断都在家族 `ruckus.tcl`。

**预期结果**：对 `xczu28dr`（`zynquplusRFSOC`），1GbE 会加载 `core` + `gthUltraScale+` + `gtyUltraScale+` + `lvdsUltraScale`；10GbE 会加载 `core` + `gthUltraScale+` + `gtyUltraScale+`。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `core/ruckus.tcl` 不需要 `getFpgaArch`，而顶层 `ruckus.tcl` 必须 `getFpgaArch`？

参考答案：`core/` 是家族无关的（只含 `Pkg`/`Reg`/`Rst`，不引用 `unisim`），任何工程都要加载、加载内容也一样，所以无需判断；顶层必须按家族挑 PHY 目录，因为同一份源码要服务多代 FPGA，每代用的收发器与 `.dcp` 不同。

**练习 2**：如果你把一个 `zynquplus` 工程的 Vivado 从 2018.2 降到 2017.2，`gtyUltraScale+` 目录会发生什么？

参考答案：因为 `gtyUltraScale+/ruckus.tcl` 要求 `VIVADO_VERSION >= 2018.3`，降到 2017.2 后条件不满足，会走 `else` 分支只 `puts` 一条 `WARNING: ... requires Vivado 2018.3 (or later)`，既不加载 `rtl/` 也不加载 `.dcp`——对应家族的 10GbE PHY 源不会进工程，综合时该家族模块会缺失。

## 5. 综合实践

把本讲四个模块串起来，完成下面这份「速率核拆分审计」小任务：

1. **目录层**：列出 `GigEthCore` 与 `TenGigEthCore` 各自的 `core/` 文件清单，标注哪些是两者共有（同名同语义，如 `*Pkg.vhd`/`*Reg.vhd`）、哪些是 10GbE 独有（`TenGigEthRst.vhd`）。
2. **MAC 层**：用 `grep` 证据说明两个速率核的所有家族胶水都例化了同一个 `EthMacTop`，并指出 1GbE 与 10GbE 给 `EthMacTop` 的 `PHY_TYPE_G` 泛型分别是什么值（提示：1GbE 在 [GigEthGtx7.vhd:244](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ethernet/GigEthCore/gtx7/rtl/GigEthGtx7.vhd#L244)；10GbE 在对应家族胶水里找 `PHY_TYPE_G`）。
3. **寄存器层**：对照 4.2.3 的寄存器表，解释为什么只有 10GbE 有 `0x230~0x240` 这段 PCS/PMA 配置寄存器。
4. **构建层**：任选一个 FPGA 器件型号，手动推断它在 1GbE 与 10GbE 下分别会加载哪些 `ruckus.tcl` 目录与 `.dcp`，并说明 `core/` 一定在其中。

**产出**：一张四列的总结表（层级 / 1GbE 内容 / 10GbE 内容 / 共享 vs 差异原因）。完成后，你应该能用一句话向同事解释：「SURF 的速率核把家族无关的 MAC+寄存器放在 `core/` 写一遍，家族相关的 PHY IP+时钟+复位按 GT 型号各写一份，再用 `ruckus.tcl` 的 `getFpgaArch` 在编译期把它们拼起来。」

## 6. 本讲小结

- `core/` 是速率核的「家族无关抽象层」：只放 `*Pkg.vhd`（配置/状态记录 + INIT_C）与 `*Reg.vhd`（AXI-Lite 寄存器），不引用任何 `unisim` 原语，因此可被 GHDL 纯仿真。
- `GigEthReg`/`TenGigEthReg` 都用 u3-l2 的 helper 四步骨架，并用 `EN_AXI_REG_G` 在「完整寄存器」与「直通模式」间二选一；二者布局高度相似，但 10GbE 多出 `0x230~0x240` 的 PCS/PMA 配置寄存器。
- 所有家族胶水都例化同一个 `EthMacTop`（共享 MAC），家族差异集中在参考时钟原语、`.dcp` 厂商 PHY IP 与复位时序（10GbE 多一个 `TenGigEthRst`）。
- 家族胶水用一个 1→2 `AxiLiteCrossbar` 把地址空间切成「MAC/Reg 段」与「GT DRP 段」，让软件通过一个 AXI-Lite 从机既能配 MAC 又能直接读写收发器寄存器。
- 顶层 `ruckus.tcl` 无条件加载 `core/`，再用 `getFpgaArch`（必要时配合 `$::env(PRJ_PART)` 细分）有条件加载家族目录；家族 `ruckus.tcl` 用 `VIVADO_VERSION` 门控并把 `.dcp` 网表接进工程。
- 1GbE 支持的家族/PHY 比 10GbE 更广（多出 `gtp7`、`lvdsUltraScale` 等），反映了「速率越高，能承载的器件越高端」这一硬件现实。

## 7. 下一步学习建议

- 想看「同一种拆分模式用到其它速率」，去读 `ethernet/XauiCore/`（XAUI，4×3.125G）、`ethernet/XlauiCore/`（XLGMII/40G）、`ethernet/Caui4Core/`（CAUI-4/100G）——它们同样遵循 `core/` + 家族 PHY 目录 + `.dcp` 的范式，对照本讲会很容易读懂。
- 想深入 MAC 内部，回到 u6-l1 的 `EthMacTop`/`EthMacRx`/`EthMacTx`，重点是 `PHY_TYPE_G` 如何让同一 MAC 适配 GMII/XGMII/XLGMII。
- 想理解软件侧如何与 4.2.3 的寄存器布局对齐，进入 u9-l4（PyRogue 设备模型），看 `python/surf/ethernet/...` 下的设备类如何按相同偏移镜像这些寄存器。
- 想搞清 `.dcp` 是怎么生成的，可查阅 Xilinx PG047（1G/2.5G Ethernet SWAT）与 PG051（10G Ethernet MAC/Subsystem）文档，它们对应 `GigEthGtx7Core` 与 `TenGigEthGtx7Core` 这些黑盒内部。
