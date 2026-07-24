# 器件、收发器、Xilinx 家族封装与贡献流程

## 1. 本讲目标

本讲是单元九（验证方法论与软件集成）的收尾篇，也是整本手册的收尾篇。前面几讲我们分别看过 RTL 基石、总线、协议、协同仿真、cocotb 测试与 PyRogue 模型。本讲要把视角拉到「整个仓库如何组织那些与具体硬件芯片绑定的部分」上，并讲清楚一个工程师改了一行 HDL 后，到底要同步动哪些地方才能让 CI 全绿。

学完后你应该能够：

- 说清 `devices/` 为什么按「厂商」而不是按「板卡」组织，以及一个器件核内部 `rtl/` + 本地 `ruckus.tcl` + 对应 PyRogue 模型这三者如何逐字段对齐。
- 说清 `xilinx/` 如何用「家族目录 + dummy 兼容」把厂商原语封装成统一名字，并理解 GHDL 回归测试为何能在没有 Vivado 的情况下编译这些家族相关代码。
- 复述一次 HDL 改动的完整贡献闭环：改 RTL → 改 `ruckus.tcl` → 改 PyRogue → 加测试 → 本地验证 → PR → CI（lint/test/docs）→ 发版门禁。

## 2. 前置知识

本讲是进阶收尾，假设你已经读过：

- **u1-l1 / u1-l3**：SURF 顶层子树职责表，以及 `rtl/`、`sim/`、`tb/`、`wrappers/`、家族目录（`core/` + 家族 PHY）的分类约定。
- **u1-l2**：ruckus 清单 + Makefile/GHDL + CI 三件套的闭环。
- **u3-l4 / u9-l4**：AXI-Lite 寄存器布局与 PyRogue 镜像「名称/偏移/位宽/读写属性四者一致」的铁律。

几个本讲会用到的关键术语，先做个最简回顾：

- **ruckus.tcl**：每个目录下的构建清单，回答「哪些 HDL 文件进 Vivado/GHDL 构建」。叶子用 `loadSource -lib surf -dir ...`，中间节点用 `loadRuckusTcl "$::DIR_PATH/<subdir>"` 下钻子目录。详见 u1-l2。
- **getFpgaArch**：ruckus 提供的函数，返回当前工程的目标 FPGA 家族字符串（如 `kintex7`、`zynquplusRFSOC`），清单据此有条件加载家族特化源码。
- **PyRogue**：把 RTL 的 AXI-Lite 寄存器布局镜像成 Python 软件节点（`pr.RemoteVariable` 等）的框架，详见 u9-l4。
- **dummy（占位兼容）**：用一个同名但 `assert(false)` 的空壳实体，让缺少厂商原语的环境（GHDL）仍能解析实体名。

> 如果你还不熟悉「记录类型 + AXI-Lite 寄存器端点」的模式，建议先看 u3-l1/u3-l2，本讲在讲器件核时会反复引用。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
| --- | --- |
| [devices/README.md](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/devices/README.md) | `devices/` 子树导航：按厂商组织、`transceivers/` 放可插拔光模块、寄存器映射须与厂商手册及 PyRogue 对齐。 |
| [devices/ruckus.tcl](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/devices/ruckus.tcl) | 器件子树总清单：加载各厂商目录，并用 `VIVADO_VERSION` 门控。 |
| [devices/transceivers/rtl/Sff8472.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/devices/transceivers/rtl/Sff8472.vhd) | 典型器件核：把 AXI-Lite 经 `AxiI2cRegMaster` 桥接到 SFP/QSFP 的 I2C EEPROM。 |
| [python/surf/devices/transceivers/_Sfp.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/devices/transceivers/_Sfp.py) | 对应的 PyRogue 设备类，镜像 SFF-8472 EEPROM 寄存器。 |
| [xilinx/README.md](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/xilinx/README.md) | `xilinx/` 子树导航：家族目录、`general/`、`xvc-udp/`、`dummy/`。 |
| [xilinx/ruckus.tcl](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/xilinx/ruckus.tcl) | Xilinx 家族总清单：`VIVADO_VERSION` 门控 + `getFpgaArch` 家族分发。 |
| [xilinx/7Series/ruckus.tcl](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/xilinx/7Series/ruckus.tcl) | 7 系内部再按 `artix7/kintex7/virtex7/zynq` 选 `gtp7/gtx7/gth7`。 |
| [xilinx/dummy/ClkOutBufDiffDummy.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/xilinx/dummy/ClkOutBufDiffDummy.vhd) | dummy 兼容范例：同名实体 + `assert(false)`。 |
| [.github/workflows/surf_ci.yml](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/.github/workflows/surf_ci.yml) | CI：lint / test / docs 三条并行 job + `gen_release`/`conda_build_lib` 发版门禁。 |
| [ruckus.tcl](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ruckus.tcl) | 仓库根清单：加载 axi/base/dsp/devices/ethernet/protocols/xilinx 七大 HDL 子树。 |
| [AGENTS.md](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md) | 贡献者「宪法」：ruckus 约定、生成代码处理、RTL 评审清单、PyRogue 对齐。 |

---

## 4. 核心概念与源码讲解

本讲按规格拆成三个最小模块：**器件核与收发器**、**Xilinx 家族封装**、**贡献与 CI 流程**。三者其实是同一个故事的三面——「与具体硬件绑定的那部分 SURF 代码，如何被组织、被构建、被验证、被发布」。

### 4.1 器件核与收发器

#### 4.1.1 概念说明

`devices/` 子树装的是「与具体外部芯片对话」的 RTL 支持块。它有两个鲜明的组织原则：

1. **按厂商（manufacturer）组织，而不是按板卡组织。** 顶层目录是一串厂商名：`AnalogDevices/`、`Microchip/`、`Micron/`、`Silabs/`、`Ti/`、`Xilinx/`、`Marvell/`、`Maxim/`、`Amphenol/`、`Linear/`、`Nxp/`。这和 SURF 整体「按能力而非按板卡划分」的哲学一致（见 u1-l1）：同一颗 ADC 会被多块板卡用，把它放在 `Ti/ads54j60/` 下，任何板卡都能复用。

2. **`transceivers/` 单独拎出来放可插拔光模块。** SFP/QSFP 这类「插在前面板上的、与具体板卡解耦的」模块，归在厂商无关的 `transceivers/` 下。

每个器件核文件夹通常遵循一个固定的小结构：

```
devices/Ti/Lmk048Base/
├── rtl/            # 可综合的寄存器/控制逻辑
├── (sim/)          # 可选：仅仿真用的外部芯片模型
├── (家族目录/)      # 可选：FPGA 家族特化实现
└── ruckus.tcl      # 本地构建清单
```

`devices/README.md` 把这条铁律写得很清楚：

> Keep register maps and control names aligned with vendor data sheets and with the matching PyRogue modules under `python/surf/devices` when they exist. Add new device sources to the nearest `ruckus.tcl`.
> —— [devices/README.md:11](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/devices/README.md#L11)

也就是说，一个器件核其实有**三层**必须保持一致：

| 层 | 位置 | 描述什么 |
| --- | --- | --- |
| RTL | `devices/<厂商>/<器件>/rtl/*.vhd` | 提供总线访问通道（通常把 AXI-Lite 桥接到 I2C/SPI）+ 可能的本地寄存器 |
| 构建清单 | `devices/<厂商>/<器件>/ruckus.tcl` | 用 `loadSource` 把上面的 RTL 登记进构建 |
| 软件 | `python/surf/devices/<厂商>/_*.py` | 把寄存器语义镜像成 PyRogue 节点 |

#### 4.1.2 核心流程：以 SFF-8472 光模块为例的三层对齐

`transceivers/Sff8472` 是最干净的范例。SFP/QSFP 光模块内部有两片 I2C EEPROM，地址分别是 A0h（存厂商/型号等 ID 信息）和 A2h（存诊断信息）。SURF 的做法是：RTL 只负责「把 AXI-Lite 总线透明地桥接到这两片 EEPROM」，真正的寄存器语义全部由 PyRogue 按 SFF-8472 工业标准来镜像。

数据流如下：

```
软件读写 AXI-Lite 寄存器
        │
        ▼
  Sff8472.vhd  ──例化──▶  AxiI2cRegMaster  ──I2C──▶  SFP 模块 EEPROM (A0h/A2h)
  （配置 DEVICE_MAP_G：           （u8-l3 讲过的 I2C 寄存器桥）
   哪些 I2C 设备、地址/数据宽度、字节序）
        ▲
        │ 镜像同样的字偏移
_Sfp.py（PyRogue 设备类，按 SFF-8472 标准把 A0h 的字节布局翻译成 RemoteVariable）
```

关键点：**RTL 不定义 EEPROM 里有什么寄存器**——那是 SFF-8472 标准和具体光模块决定的。RTL 只定义「桥的配置」（哪些 I2C 设备、怎么寻址、什么字节序）。PyRogue 则把外部芯片的寄存器布局逐字段翻译成软件节点。两者的耦合点是**字节序与地址映射规则**，必须对齐。

#### 4.1.3 源码精读

**(1) 器件总清单：`VIVADO_VERSION` 门控。** `devices/ruckus.tcl` 先无条件加载三个纯数字/协议器件（`Marvell/Maxim/Microchip`），其余厂商全部包在一个 Vivado 版本守卫里：

```tcl
# devices/ruckus.tcl:4-20
loadRuckusTcl "$::DIR_PATH/Marvell"
loadRuckusTcl "$::DIR_PATH/Maxim"
loadRuckusTcl "$::DIR_PATH/Microchip"

# Check for non-zero Vivado version (in-case non-Vivado project)
if {  $::env(VIVADO_VERSION) > 0.0} {
   loadRuckusTcl "$::DIR_PATH/AnalogDevices"
   ...
   loadRuckusTcl "$::DIR_PATH/Ti"
   loadRuckusTcl "$::DIR_PATH/transceivers"
   loadRuckusTcl "$::DIR_PATH/Xilinx"
}
```

> 见 [devices/ruckus.tcl:4-20](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/devices/ruckus.tcl#L4-L20)。`$::env(VIVADO_VERSION) > 0.0` 表示当前是 Vivado 工程；非 Vivado（如 GHDL 仿真）时这个环境变量为 0，这些依赖厂商 IP 的器件核就不进构建。这条守卫直接解释了「为什么 CI 的 GHDL 回归里看不到 devices 测试」（见 4.3）。

**(2) RTL：配置 I2C 设备映射 + 例化桥。** `Sff8472.vhd` 的全部「寄存器语义」就集中在一个常量数组里：

```vhdl
-- devices/transceivers/rtl/Sff8472.vhd:48-60
constant SFF8472_I2C_CONFIG_C : I2cAxiLiteDevArray(0 to 1) := (
   0 => MakeI2cAxiLiteDevType(
      i2cAddress  => "1010000",   -- 2 wire address 1010000X (A0h)
      dataSize    => 8,           -- in units of bits
      addrSize    => 8,           -- in units of bits
      endianness  => '0',         -- Little endian
      repeatStart => '1'),        -- Repeat Start
   1 => MakeI2cAxiLiteDevType(
      i2cAddress  => "1010001",   -- 2 wire address 1010001X (A2h)
      ...));
```

> 见 [Sff8472.vhd:48-60](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/devices/transceivers/rtl/Sff8472.vhd#L48-L60)。两片 EEPROM（A0h、A2h）各对应一项；`dataSize=8`/`addrSize=8` 表示每次读写一个 8 位数据、用 8 位地址寻址；`endianness='0'`（小端）决定 AXI-Lite 的 32 位字与 I2C 字节流的拼装顺序。

接着把这份配置交给 `AxiI2cRegMaster`（u8-l3 讲过的 I2C 寄存器桥）：

```vhdl
-- devices/transceivers/rtl/Sff8472.vhd:64-82
U_AxiI2C : entity surf.AxiI2cRegMaster
   generic map (
      TPD_G           => TPD_G,
      DEVICE_MAP_G    => SFF8472_I2C_CONFIG_C,   -- ← 桥的「寄存器布局」全在这里
      I2C_SCL_FREQ_G  => I2C_SCL_FREQ_G,
      I2C_MIN_PULSE_G => I2C_MIN_PULSE_G,
      AXI_CLK_FREQ_G  => AXI_CLK_FREQ_G)
   port map (
      scl            => scl,
      sda            => sda,
      axiReadMaster  => axiReadMaster,
      ...);
```

> 见 [Sff8472.vhd:64-82](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/devices/transceivers/rtl/Sff8472.vhd#L64-L82)。注意这个实体本身**没有任何 `axiSlaveRegister` 调用**——它是纯结构性的，把全部寄存器访问透传给 I2C 桥。`Sff8472Core.vhd` 是它的「带 `i2ci/i2co` 端口」变体（[Sff8472Core.vhd:62-70](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/devices/transceivers/rtl/Sff8472Core.vhd#L62-L70) 例化 `AxiI2cRegMasterCore`），区别仅在端口形态。

**(3) 构建清单：登记 RTL。** 叶子清单只有一行实质内容：

```tcl
# devices/transceivers/ruckus.tcl:4-5
source $::env(RUCKUS_PROC_TCL)
loadSource -lib surf -dir "$::DIR_PATH/rtl"
```

> 见 [devices/transceivers/ruckus.tcl:4-5](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/devices/transceivers/ruckus.tcl#L4-L5)。`-lib surf` 把这些文件挂到 `surf` 库下，`-dir` 整目录登记。这就是 u1-l2 讲过的「在叶子处用 `loadSource` 登记真实 `.vhd`」。

**(4) PyRogue：镜像同样的字偏移。** `_Sfp.py` 用 `offset`、`bitSize`、`base`、`stride` 把 A0h 的字节布局翻译成软件节点：

```python
# python/surf/devices/transceivers/_Sfp.py:41-77
self.add(pr.RemoteVariable(
    name        = 'Identifier',
    offset      = (0 << 2),          # ← 字节地址 0 ÷ 4 = AXI-Lite 字偏移 0
    bitSize     = 8,
    mode        = 'RO',
    enum        = transceivers.IdentifierDict,
))
self.add(pr.RemoteVariable(
    name        = 'Connector',
    offset      = (2 << 2),          # ← 字节地址 2 ÷ 4 = 字偏移 0... 实为按字对齐
    ...))
self.addRemoteVariables(
    name        = 'VendorNameRaw',
    offset      = (20 << 2),         # ← SFF-8472 规定厂商名从字节 20 开始
    bitSize     = 8,
    base        = pr.String,
    number      = 16,
    stride      = 4,                 # ← 每个字节占一个 32 位 AXI-Lite 字
    hidden      = True,
)
```

> 见 [_Sfp.py:41-77](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/devices/transceivers/_Sfp.py#L41-L77)。

这里有一个需要算清楚的地址换算。AXI-Lite 是按 32 位字寻址的总线，而 SFF-8472 的 EEPROM 是按字节编号的。`AxiI2cRegMaster` 在小端、`dataSize=8` 配置下，把每个 I2C 字节单独映射到一个 AXI-Lite 字的低字节。于是字节地址 `b` 对应的 AXI-Lite 字偏移为

\[
\text{wordOffset}(b) = \left\lfloor \frac{b}{4} \right\rfloor \quad\text{但代码里写成 } (b \ll 2)
\]

代码里 `(20 << 2)` 其实是把「字节地址 × 4」当成字节的「字内字节号」再用 `stride=4` 逐字推进——本质是「一个 I2C 字节独占一个 32 位字」。这正是为什么 `endian='0'`（小端）和 `stride=4` 必须与 RTL 的 `MakeI2cAxiLiteDevType` 配置一致：一旦 RTL 改了 `endianness` 或 `dataSize`，PyRogue 的 `offset`/`stride`/`base` 就全部失效。这就是「三层对齐」的物理含义。

`_Sfp.py` 顶部的 `from surf.devices import transceivers`（[_Sfp.py:19](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/devices/transceivers/_Sfp.py#L19)）引入的是同包 `__init__.py` 里定义的 `IdentifierDict`、`ConnectorDict` 等枚举表（[transceivers/__init__.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/devices/transceivers/__init__.py)），把 EEPROM 的原始字节值翻译成人可读字符串——这是 u9-l4 讲过的「私有 `_Xxx.py` + `__init__.py` 再导出」约定。

#### 4.1.4 代码实践：核对 SFF-8472 的三层一致性

这是一个**源码阅读型实践**（无需运行工具链）。

1. **实践目标**：验证 `Sff8472` 的 RTL 配置与 PyRogue 偏移是否在「A0h 设备、8 位数据、小端」这一点上自洽。
2. **操作步骤**：
   - 打开 [Sff8472.vhd:48-60](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/devices/transceivers/rtl/Sff8472.vhd#L48-L60)，记下 A0h 设备的 `i2cAddress`、`dataSize`、`addrSize`、`endianness`。
   - 打开 [_Sfp.py:41-77](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/devices/transceivers/_Sfp.py#L41-L77)，记下 `Identifier`/`Connector`/`VendorNameRaw` 的 `offset` 与 `stride`。
   - 翻 SFF-8472 标准摘要（`_Sfp.py` 顶部注释 [L3-L5](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/devices/transceivers/_Sfp.py#L3-L5) 指向的文档）：Identifier 在字节 0、Connector 在字节 2、VendorName 在字节 20~35。
3. **需要观察的现象**：PyRogue 的 `offset = (字节号 << 2)` + `stride = 4` 恰好让每个 SFF-8472 字节落在一个独立的 AXI-Lite 字里，与 RTL 的 `dataSize=8`、小端配置一致。
4. **预期结果**：三层在「每字节一个 32 位字、小端、A0h 设备」上完全吻合；若有人把 RTL 的 `endianness` 改成 `'1'`，PyRogue 的字符串会变成乱序——这说明三层必须同改。
5. 本实践为静态阅读，不涉及运行命令。

#### 4.1.5 小练习与答案

**练习 1**：`devices/ruckus.tcl` 为什么把 `Marvell/Maxim/Microchip` 放在 `VIVADO_VERSION` 守卫之外，而把 `Ti/transceivers/...` 放在守卫之内？

**参考答案**：守卫之外的器件核不依赖任何 Vivado 原语/IP，纯 RTL 即可综合或被 GHDL 仿真；守卫之内的器件核（含 `transceivers/` 的 I2C 桥所需底层、各厂商 PHY）依赖 Vivado 工具链，非 Vivado 工程下不应加载，否则清单会引用不存在的资源。

**练习 2**：假如要给 `Sff8472` 增加读取 A2h EEPROM 里「温度」寄存器的软件节点，需要改哪几层？RTL 里的 `SFF8472_I2C_CONFIG_C` 要不要改？

**参考答案**：A2h 设备**已经在** `SFF8472_I2C_CONFIG_C` 的第 1 项里登记（[Sff8472.vhd:55-60](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/devices/transceivers/rtl/Sff8472.vhd#L55-L60)），所以 RTL 与 `ruckus.tcl` 都不用改；只需在 PyRogue 侧按 SFF-8472 标准的 A2h 温度字节偏移新增一个 `pr.RemoteVariable`（实际上 `_Sfp.py` 同包里已有 `getTemp` 这类换算函数，见 [transceivers/__init__.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/devices/transceivers/__init__.py) 的 `getTemp`）。这是「透明桥」型器件核的好处：外部芯片寄存器变化通常只动软件层。

---

### 4.2 Xilinx 家族封装

#### 4.2.1 概念说明

`xilinx/` 子树装的是「与 Xilinx FPGA 强绑定」的 RTL：时钟缓冲、IO 缓冲、收发器（GTP/GTX/GTH/GTY）、DNA/USR_ACCESS、XADC、SEM 等厂商原语的封装与 helper。它的目录结构是：

```
xilinx/
├── 7Series/        # artix7/kintex7/virtex7/zynq：gtp7/gtx7/gth7/xadc/sem
├── Virtex5/
├── UltraScale/     # kintexu/virtexu
├── UltraScale+/    # kintexuplus/virtexuplus/zynquplus(RFSOC)：gthUs+/gtyUs+/clocking
├── Versal/
├── general/        # 不绑定单一家族的 Xilinx helper（含 microblaze/sdk）
├── xvc-udp/        # Xilinx Virtual Cable over UDP（JTAG over UDP）
└── dummy/          # 占位/兼容实体（同名 + assert(false)）
```

这里有两个互相配合的设计，理解了它们就理解了整个 `xilinx/`：

1. **家族封装**：不同家族的同一个功能（比如「把时钟差分输出到引脚」）用的原语不一样（7 系用 OBUFDS 的某种配置、UltraScale 用另一些）。SURF 给它们起一个统一名字（如 `ClkOutBufDiff`），把家族差异藏进家族目录。高层 RTL 只调用统一名字，构建时按家族选具体实现。

2. **dummy 兼容**：GHDL 没有 Xilinx 原语库。如果高层 RTL 引用了 `ClkOutBufDiff`，GHDL 编译时找不到这个实体就报错。解决办法是提供一个**同名**的空壳实体，里面只放一句 `assert(false) ... severity failure`。这样 GHDL 能解析实体名（编译通过），但若真去例化它就会在 elaboration 失败。从而**家族无关的核心 RTL 可以被 GHDL 回归测试**，而真正用到原语的部分自动被排除。

> `xilinx/README.md` 的告诫是：能复用 SURF 通用封装就不要在高层直接例化原语——见 [xilinx/README.md:12](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/xilinx/README.md#L12)。

#### 4.2.2 核心流程：两层选择

`xilinx/ruckus.tcl` 用两层条件把源码切分进构建：

```
第一层：VIVADO_VERSION 门控（区分 Vivado 工程 vs GHDL 仿真）
  ├─ > 0.0（Vivado）→ 加载 general/（真原语封装）+ xvc-udp/
  └─ == 0.0（GHDL） → 只加载 dummy/（占位）+ SelectIoRxGearboxAligner.vhd（无原语依赖）

第二层：getFpgaArch 家族分发（仅 Vivado 工程有意义）
  ├─ artix7/kintex7/virtex7/zynq → 7Series/
  │     └─ 7Series/ruckus.tcl 再按具体型号选 gtp7/gtx7/gth7
  ├─ kintexu/virtexu              → UltraScale/
  ├─ kintexuplus/virtexuplus/
  │   virtexuplusHBM/zynquplus/
  │   zynquplusRFSOC              → UltraScale+/（gthUs+/gtyUs+/clocking）
  └─ versal                       → Versal/
```

第一层保证「GHDL 也能编译」；第二层保证「Vivado 工程只拿到本家族的原语」。两者合起来，就是 SURF 能在 GHDL 上跑家族无关回归、同时在 Vivado 上综合真实硬件的根本机制。

#### 4.2.3 源码精读

**(1) 第一层：Vivado 门控与 dummy 分流。**

```tcl
# xilinx/ruckus.tcl:4-12
if {  $::env(VIVADO_VERSION) > 0.0} {
   # Load the Core
   loadRuckusTcl "$::DIR_PATH/general"
   loadRuckusTcl "$::DIR_PATH/xvc-udp"
} else {
   loadSource -lib surf -path "$::DIR_PATH/general/rtl/SelectIoRxGearboxAligner.vhd"
   loadSource -lib surf -dir  "$::DIR_PATH/dummy"
}
```

> 见 [xilinx/ruckus.tcl:4-12](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/xilinx/ruckus.tcl#L4-L12)。注意 `else` 分支里特意单独挑出 `SelectIoRxGearboxAligner.vhd`（一个不依赖 Xilinx 原语、可在 GHDL 仿真的文件），其余家族相关源码全部换成 `dummy/`。

**(2) 第二层：家族分发。**

```tcl
# xilinx/ruckus.tcl:14-39
set family [getFpgaArch]

if { ${family} eq {artix7}  || ${family} eq {kintex7} ||
     ${family} eq {virtex7} || ${family} eq {zynq} } {
   loadRuckusTcl "$::DIR_PATH/7Series"
}
...
if { ${family} eq {kintexuplus} || ${family} eq {virtexuplus} ||
     ${family} eq {virtexuplusHBM} || ${family} eq {zynquplus} ||
     ${family} eq {zynquplusRFSOC} } {
   loadRuckusTcl "$::DIR_PATH/UltraScale+"
}
```

> 见 [xilinx/ruckus.tcl:14-39](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/xilinx/ruckus.tcl#L14-L39)。`getFpgaArch` 返回的家族字符串列表正是 AGENTS.md 钉死的那些（[AGENTS.md:80](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L80)）。

7 系内部还要再细分一次，甚至用到 `PRJ_PART`（具体型号）做正则：

```tcl
# xilinx/7Series/ruckus.tcl（节选）
if { ${family} == "zynq" } {
   if { [ regexp "XC7Z(015|012).*" [string toupper "$::env(PRJ_PART)"] ] } {
      loadRuckusTcl "$::DIR_PATH/gtp7"
   } else {
      loadRuckusTcl "$::DIR_PATH/gtx7"
   }
}
```

> 见 [xilinx/7Series/ruckus.tcl](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/xilinx/7Series/ruckus.tcl)。同样是 `zynq` 家族，`XC7Z015/012` 这类小封装用 `gtp7`，其余用 `gtx7`——这就是 u1-l3 提到「必要时按 `PRJ_PART` 细分」的真实例子。`UltraScale+/ruckus.tcl` 则一次性加载 `gthUs+`、`gtyUs+`、`clocking`（[UltraScale+/ruckus.tcl:3-7](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/xilinx/UltraScale+/ruckus.tcl#L3-L7)），因为这些家族常多 PHY 共存。

**(3) dummy 兼容实体。** 真 `ClkOutBufDiff` 在 [xilinx/general/rtl/ClkOutBufDiff.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/xilinx/general/rtl/ClkOutBufDiff.vhd)（仅 Vivado 加载）；占位的同名实体在：

```vhdl
-- xilinx/dummy/ClkOutBufDiffDummy.vhd:21-41
entity ClkOutBufDiff is
   generic (
      TPD_G          : time    := 1 ns;
      XIL_DEVICE_G   : string  := "7SERIES";  -- Either "7SERIES" or "ULTRASCALE" ...
      ...);
   port (
      ...);
end ClkOutBufDiff;

architecture mapping of ClkOutBufDiff is
begin
   assert (false)
      report "surf.xilinx: ClkOutBufDiff not supported" severity failure;
end mapping;
```

> 见 [ClkOutBufDiffDummy.vhd:21-41](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/xilinx/dummy/ClkOutBufDiffDummy.vhd#L21-L41)。端口表与真实体保持一致（这样高层 port map 不用改），但架构体里只有一句 `assert(false)`。`DeviceDnaDummy.vhd` 是同款做法（[DeviceDnaDummy.vhd:39-43](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/xilinx/dummy/DeviceDnaDummy.vhd#L39-L43)）。

这就是「dummy 兼容」的全部秘密：**同名实体 + 端口一致 + 架构体 assert-fail**。它让 GHDL 能把整个 `surf` 库elaborate 出来（实体都可解析），而真用到原语的家族核因为从不被 GHDL 加载，自然不会触发那条 assert。

#### 4.2.4 代码实践：画出家族选择树

这是一个**源码阅读 + 画图型实践**。

1. **实践目标**：把 `getFpgaArch` 在 `xilinx/` 下的完整分发路径画成一棵树。
2. **操作步骤**：
   - 从 [xilinx/ruckus.tcl:14-39](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/xilinx/ruckus.tcl#L14-L39) 读出四个家族大组（7Series / UltraScale / UltraScale+ / Versal）各自对应的家族字符串。
   - 进入 [xilinx/7Series/ruckus.tcl](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/xilinx/7Series/ruckus.tcl)，读出 `artix7/kintex7/virtex7/zynq` 各自选哪个 GT 目录（`gtp7/gtx7/gth7`），以及 `zynq` 内部按 `PRJ_PART` 的二次细分。
   - 进入 [xilinx/UltraScale+/ruckus.tcl](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/xilinx/UltraScale+/ruckus.tcl)，确认它一次性加载了哪几个子目录。
3. **需要观察的现象**：分发是「家族字符串 → 目录 → （可选）型号正则」的逐级细化。
4. **预期结果**：得到一棵以 `getFpgaArch` 返回值为根、叶子为 `gtp7/gtx7/gth7/gthUs+/gtyUs+/clocking/...` 的选择树。
5. 本实践为静态阅读，不涉及运行命令。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `xilinx/dummy/ClkOutBufDiffDummy.vhd` 里的 `assert(false) ... severity failure` 删掉，只留一个空架构体，会有什么后果？

**参考答案**：编译仍能通过，但语义变了——原本「一旦真例化就 elaboration 失败、从而把家族相关核挡在 GHDL 回归之外」的安全网失效。某个误被加载的家族核可能静默地例化一个什么都不做的 `ClkOutBufDiff`，仿真看起来「通过」却毫无意义。`assert(false) severity failure` 是 dummy 故意「会响的报警器」。

**练习 2**：为什么 `UltraScale+/ruckus.tcl` 同时加载 `gthUs+` 和 `gtyUs+` 两个 GT 目录，而 7 系每次只加载一个？

**参考答案**：UltraScale+ 家族的同一颗芯片常常同时具备 GTH 和 GTY 两种收发器 Bank（或同一设计混用），需要两套封装都可用；7 系的 artix7/kintex7/virtex7 各自只有一种 GT（GTP/GTX/GTH），所以按家族二选一即可。这体现了「家族目录的粒度由硬件能同时存在几种 PHY 决定」。

---

### 4.3 贡献与 CI 流程

#### 4.3.1 概念说明

前面两个模块讲的是「代码长什么样」，这一节讲「改了代码之后要做什么」。SURF 的贡献流程有一个核心信条，AGENTS.md 反复强调：

> 改 HDL 必须同步更新最近的 `ruckus.tcl`、匹配的 PyRogue 模型、相关测试，并经 CI（lint/test/docs）验证。

这条信条之所以重要，是因为 SURF 是「可复用基础设施」——你改的一个寄存器偏移，下游可能有几十个板卡工程和 Python 脚本依赖它。所以贡献流程不是「改完提交」，而是一个**四件同步 + 三道校验**的闭环。

#### 4.3.2 核心流程：一次 HDL 改动的完整闭环

```
① 改 RTL（含寄存器布局）
        │
        ▼
② 更新最近的 ruckus.tcl（新文件用 loadSource 登记，家族码用 getFpgaArch 守卫）
        │
        ▼
③ 更新匹配的 PyRogue 模型（offset/bitSize/mode 逐字段对齐，见 u9-l4）
        │
        ▼
④ 加/改 cocotb 测试（覆盖副作用、边界、错误用例）
        │
        ▼
⑤ 本地验证：make MODULES=$PWD import、vsg、focused pytest
        │
        ▼
⑥ 提 PR（目标分支 pre-release）
        │
        ▼
⑦ CI 三条并行 job：lint / test / docs
        │
        ▼
⑧ 三 job 全绿 → 触发发版门禁 gen_release + conda_build_lib
```

CI 这部分由 `.github/workflows/surf_ci.yml` 定义。它有 5 个 job：`lint`、`test`、`docs` 三条并行跑校验；`gen_release` 和 `conda_build_lib` 用 `needs: [lint, test, docs]` 串成发版门禁——前三条任何一条红，就不发版。

> 本讲承接 u1-l2 的「lint/test/docs 三条流水线」，但那一讲侧重工具链本身；这里侧重「贡献者视角下每一步要满足什么」。

#### 4.3.3 源码精读

**(1) lint job：静态检查 + GHDL 语法分析。** lint job 依次做：trailing whitespace/tab 检查、Python `flake8`、C/C++ `cpplint`、VHDL 风格 `vsg`、最后用 GHDL 做全仓库语法分析：

```yaml
# .github/workflows/surf_ci.yml:76-78
- name: VHDL Syntax Checking
  run: |
    make MODULES=$PWD analysis
```

> 见 [surf_ci.yml:76-78](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/.github/workflows/surf_ci.yml#L76-L78)。`make ... analysis` 是 u1-l2 讲过的 GHDL 语法分析目标（`--std=08 --ieee=synopsys`）。这一步能抓住「新加了 RTL 却没登记进 ruckus.tcl」这类错误——因为没登记的文件根本不会被分析。

**(2) test job：import 缓存 + 并行回归 + 覆盖率。**

```yaml
# .github/workflows/surf_ci.yml:107-115
- name: Parallel Regression Tests
  run: |
    make MODULES=$PWD import
    python -m pytest --cov -v -n auto --dist=worksteal tests/axi tests/base tests/dsp tests/protocols
- name: Code Coverage
  run: |
    codecov
    coverage report -m
```

> 见 [surf_ci.yml:107-115](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/.github/workflows/surf_ci.yml#L107-L115)。`make ... import` 生成 `build/SRC_VHDL` 源缓存（u9-l1）；`pytest -n auto --dist=worksteal` 是 u9-l1 讲过的工作偷取并行回归。

这里有一个**非常值得注意的细节**：test job 只跑 `tests/axi tests/base tests/dsp tests/protocols`——**没有 `tests/ethernet`，更没有 `tests/devices`**。原因正是前两个模块讲的：`devices/` 和大部分 `ethernet/` 的家族核依赖 Vivado 原语/IP，GHDL 无法仿真。这和 `devices/ruckus.tcl` 的 `VIVADO_VERSION` 守卫、`xilinx/` 的 dummy 机制是同一件事的三面：**SURF 的 GHDL 回归只覆盖家族无关的核心**，家族相关核留给 Vivado 仿真/上板验证。

**(3) docs job + 发版门禁。**

```yaml
# .github/workflows/surf_ci.yml:119-141（docs）+ 144-161（门禁）
docs:
  name: Documentation
  ...
  - name: Generate Documentation
    run: |
      doxygen Doxyfile
  - name: Deploy Documentation
    if: startsWith(github.ref, 'refs/tags/')
    ...

gen_release:
  needs: [lint, test, docs]
  uses: slaclab/ruckus/.github/workflows/gen_release.yml@main
  ...

conda_build_lib:
  needs: [lint, test, docs]
  uses: slaclab/ruckus/.github/workflows/conda_build_lib.yml@main
  ...
```

> 见 [surf_ci.yml:119-141](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/.github/workflows/surf_ci.yml#L119-L141) 与 [surf_ci.yml:144-161](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/.github/workflows/surf_ci.yml#L144-L161)。docs job 用 doxygen 生成文档，仅在打 tag（`refs/tags/`）时部署到 GitHub Pages；`gen_release` 与 `conda_build_lib` 复用 ruckus 仓库的可复用 workflow，`needs: [lint, test, docs]` 把它们钉成「前三条全绿才执行」的门禁。

**(4) AGENTS.md 把贡献规则写成「宪法」。** 几节直接对应上面的闭环：

- **Ruckus Conventions**：`ruckus.tcl` 是构建清单，改 HDL 同步改最近清单，用 `getFpgaArch` 做家族选择，改完结构跑 `make MODULES="$PWD" import`——[AGENTS.md:74-82](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L74-L82)。
- **Generated And Vendor Code**：厂商/生成代码（含 Xilinx stub、XCI/DCP、第三方协议）当外部代码，不重格式化、不改 license 头——[AGENTS.md:195-199](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L195-L199)。这条直接管着 4.1/4.2 里的器件核与家族封装。
- **PyRogue Register Maps**：RTL 寄存器布局与 PyRogue 必须 `offset/bitOffset/bitSize/mode/字节序` 逐字段同步——[AGENTS.md:186-192](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L186-L192)。
- **RTL Review Checklist**：提交前的体检表，含「新 HDL 已进正确 ruckus.tcl 且家族码有守卫」「寄存器变化已反映到 PyRogue/测试/文档」——[AGENTS.md:212-224](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L212-L224)。

最后，仓库根 [ruckus.tcl](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ruckus.tcl) 把 `axi/base/dsp/devices/ethernet/protocols/xilinx` 七大 HDL 子树挂进构建（[ruckus.tcl:15-21](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ruckus.tcl#L15-L21)），并用 `SubmoduleCheck {ruckus} {4.9.0}` 校验外部 ruckus 工具版本（[ruckus.tcl:5-6](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ruckus.tcl#L5-L6)）——这是整个构建图的入口与版本锁。

#### 4.3.4 代码实践：阅读 CI 并定位 devices 为何缺席回归

1. **实践目标**：把 CI 的每个 step 与「它在校验什么」对应起来，并解释 `tests/devices` 为何不在 test job 里。
2. **操作步骤**：
   - 打开 [surf_ci.yml](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/.github/workflows/surf_ci.yml)，在 lint job 里逐条标注：whitespace/tab 检查、`flake8`、`cpplint`、`vsg`、`make ... analysis` 各自的职责。
   - 在 test job 里确认 `pytest` 命令覆盖的子系统列表。
   - 回到 [devices/ruckus.tcl:9-20](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/devices/ruckus.tcl#L9-L20) 与 [xilinx/ruckus.tcl:4-12](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/xilinx/ruckus.tcl#L4-L12)，对照「`VIVADO_VERSION` 守卫」与「pytest 不含 devices/ethernet」。
3. **需要观察的现象**：CI 的 test job 子系统列表与「能在 GHDL 下加载的家族无关子树」高度吻合。
4. **预期结果**：能用自己的话解释「为什么 SURF 的 CI 回归不覆盖器件核与家族 PHY 核——因为它们被 ruckus 守卫挡在 GHDL 构建之外，只能由 Vivado/上板验证」。
5. **可选运行（待本地验证）**：若本地已装 GHDL + ruckus，可运行 `make MODULES="$PWD" analysis`，预期 `devices/` 与家族目录不会被分析（因 `VIVADO_VERSION` 未设置）；若未装工具链，则止步于阅读。

#### 4.3.5 小练习与答案

**练习 1**：你给 `AxiVersion` 加了一个新寄存器（改了 RTL 偏移表）。按 AGENTS.md，同一个 PR 里至少还要同步改哪几处？

**参考答案**：① 对应的 PyRogue 模型 `python/surf/axi/_AxiVersion.py`（offset/bitSize/mode 对齐，见 [AGENTS.md:186-192](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L186-L192)）；② 相关 cocotb 测试/寄存器 helper（[AGENTS.md:212-224](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L212-L224)）；③ 若引入了新文件，还要更新最近的 `ruckus.tcl`（[AGENTS.md:74-82](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L74-L82)）。寄存器偏移变化属公共接口变更，应格外谨慎。

**练习 2**：`gen_release` 与 `conda_build_lib` 为什么用 `needs: [lint, test, docs]` 而不是直接和它们并列？

**参考答案**：`needs` 建立「依赖+顺序」关系——被依赖的 job 必须先成功，依赖方才执行。这样把发版/发 conda 钉成门禁：lint/test/docs 任一失败就不发版。若并列，则无论前三条结果如何都会发版，失去质量门禁意义。这是 u1-l2 提到的「前三条全绿才发版」的 YAML 落地形式。

---

## 5. 综合实践

把本讲三个模块串起来，做一个「器件核一致性巡检」。

**任务**：在 `devices/` 下任选一个器件核（推荐 `transceivers/Sff8472` 或 `Ti/Lmk048Base`），按下表逐项核对，并写一份简短的一致性报告。

| 核对项 | 查找位置 | 期望 |
| --- | --- | --- |
| ① RTL 存在 | `devices/<厂商>/<器件>/rtl/*.vhd` | 有可综合的寄存器/控制逻辑 |
| ② 本地 ruckus.tcl | `devices/<厂商>/<器件>/ruckus.tcl` | 用 `loadSource -lib surf -dir rtl` 登记了① |
| ③ 父级清单已挂载 | `devices/<厂商>/ruckus.tcl` 与 `devices/ruckus.tcl` | 经 `loadRuckusTcl` 链路可达根 `ruckus.tcl`；若是 Vivado 依赖核，应在 `VIVADO_VERSION` 守卫内 |
| ④ PyRogue 模型 | `python/surf/devices/<厂商>/_<器件>.py` | 寄存器 offset/bitSize/mode 与 RTL（或外部芯片手册）逐字段对齐 |
| ⑤ 测试 | `tests/` 下是否有对应 cocotb | 若是家族无关核可能有测试；若是器件/家族核，解释为何没有（呼应 4.3） |

**以 `transceivers/Sff8472` 为例的参考结论**：

- ① [Sff8472.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/devices/transceivers/rtl/Sff8472.vhd) 存在，配置 A0h/A2h 两片 I2C EEPROM。
- ② [transceivers/ruckus.tcl:5](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/devices/transceivers/ruckus.tcl#L5) 登记了 `rtl/`。
- ③ [devices/ruckus.tcl:18](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/devices/ruckus.tcl#L18) 在 `VIVADO_VERSION` 守卫内加载 `transceivers/`，根 [ruckus.tcl:18](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/ruckus.tcl#L18) 加载 `devices/`——链路完整。
- ④ [_Sfp.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/python/surf/devices/transceivers/_Sfp.py) 按 SFF-8472 标准镜像 A0h 字节布局，offset/stride 与 RTL 小端/8 位配置自洽。
- ⑤ 该核依赖 I2C 桥与外部光模块硬件，属器件核，故不在 GHDL 回归里（与 4.3 结论一致）。

完成报告后，尝试回答一个拔高问题：**如果要让这个器件核也能被 GHDL 回归覆盖，最小改动是什么？**（提示：参考 `xilinx/` 的 dummy 机制——给依赖的外部 IP 提供同名空壳，或把核拆出家族无关的纯逻辑部分。）

## 6. 本讲小结

- **器件核按厂商组织**：`devices/` 顶层是一串厂商名 + 一个 `transceivers/`；每个器件核 = `rtl/` + 本地 `ruckus.tcl` + 对应 `python/surf/devices` PyRogue 模型，三层在寄存器映射上必须逐字段对齐。
- **透明桥型器件核**：`Sff8472` 这类核 RTL 不定义寄存器语义，只配置 I2C 桥（`DEVICE_MAP_G`），真正的寄存器布局是外部芯片（SFF-8472 EEPROM）决定的，由 PyRogue 镜像；耦合点是字节序与地址映射规则。
- **Xilinx 家族封装 = 家族目录 + dummy 兼容**：家族差异藏进 `7Series/UltraScale/UltraScale+/Versal` 等目录，统一名字；`dummy/` 用同名 `assert(false)` 实体让 GHDL 能编译家族相关代码而不会真去仿真原语。
- **两层 ruckus 选择**：`VIVADO_VERSION` 门控区分 Vivado/GHDL，`getFpgaArch` 分发家族；`devices/ruckus.tcl` 与 `xilinx/ruckus.tcl` 都用这套机制。
- **CI 三 job + 发版门禁**：lint（whitespace/flake8/cpplint/vsg/GHDL analysis）、test（import + pytest 并行回归，仅覆盖 axi/base/dsp/protocols）、docs（doxygen）；`gen_release`/`conda_build_lib` 用 `needs` 钉成「全绿才发版」。
- **贡献闭环**：改 RTL → 改最近 `ruckus.tcl` → 改 PyRogue → 加测试 → 本地 `make import`/`vsg`/pytest 验证 → PR 到 `pre-release` → CI。AGENTS.md 的 Ruckus/Generated Code/PyRogue/RTL Review Checklist 各节正是这条闭环的成文规则。

## 7. 下一步学习建议

本讲是整本手册的收尾。如果你一路从单元一读到此处，已经具备从「读懂一行 RTL」到「改动一个模块并让它通过 CI 全套校验」的完整能力。后续建议：

1. **动手改一个小模块走通 CI**：在 `base/general/` 选一个简单原语，做一处无害改动（如调整注释或一个参数默认值），本地跑 `make MODULES=$PWD import`、`./.venv/bin/vsg -c vsg-linter.yml <file>.vhd`、`./.venv/bin/python -m pytest -q tests/base/general`，体会贡献闭环的每一步。这是把本讲知识转化为肌肉记忆的最快路径。
2. **阅读一个真实器件核的完整链路**：挑 `devices/Ti/Lmk048Base`（时钟分布芯片）或 `devices/AnalogDevices/ad9249`（ADC），从 RTL → ruckus → PyRogue 走一遍，对照厂商数据手册核验寄存器映射。
3. **扩展到家族 PHY 核**：回到 u6-l4 / u7 讲过的 `GigEthCore`、`Pgp3Core`，结合本讲的 `getFpgaArch` 分发与 dummy 机制，理解「core/ 通用 RTL + 家族 PHY 目录」在真实协议核里如何落地。
4. **回读 AGENTS.md 全文**：此时再读 AGENTS.md，你会发现它的每一节都对应手册里某一篇讲义——它就是这本手册的高度浓缩版，值得作为日常贡献的检查清单长期使用。
