# 总线互联 Crossbar 与 Socket

## 1. 本讲目标

学完本讲，你应当能够：

- 理解 CoralNPU 如何用一份**纯数据**（`CrossbarConfig`）描述整个 SoC 的总线拓扑：有哪些主机、哪些从机、各自的地址区间与时钟域、谁连谁。
- 看懂 `CoralNPUXbar` 这个**数据驱动的装配器**如何「读配置 → 生成 IO → 实例化 Socket → 解码地址 → 自动连线」五步把一张总线网搭起来。
- 掌握两类基础 Socket 的分工：`TlulSocket1N`（一主多从，按**地址译码**选设备）与 `TlulSocketM1`（多主一从，按**仲裁**选主机）。
- 说出 `TlulWidthBridge`、`TlulFifoAsync`、`TlulToSram` 等适配器在互联中的位置与作用。
- 自己画出一条「CPU 发出一次 DTCM 访问」从主机端口一直走到 DTCM 的完整总线路径。

本讲是第 3 单元（SoC 与总线集成）的第四篇，承接 [u3-l3](u3-l3-tlul-axi-bridge.md) 讲过的 TileLink-UL（TL-UL）协议本身，把视角从「单条协议」拉到「多主机 × 多从机的整张网」。

## 2. 前置知识

在进入源码前，先用三段话把「为什么需要 Crossbar」讲清楚。

**主机（host/master）与从机（device/slave）。** 总线上发起读写的一方叫主机，被动响应的一方叫从机。CoralNPU 的 SoC 里同时存在多个主机：标量核 `coralnpu_core`、SPI 桥 `spi2tlul`、`dma`、两个 ISP 主机 `ispyocto_m1/m2`、上电启动器 `autoboot`；也存在很多从机：SRAM、ROM、UART、SPI、GPIO、CLINT、PLIC、DDR、以及核自身的 `coralnpu_device`（含 ITCM/DTCM/CSR）。任意一对「主机想访问某个从机」都需要一条物理通路。

**全连接 vs. 互联（fabric）。** 最朴素的做法是把每个主机和每个从机两两拉一根线，但这样连线数是 \(M\times N\)，随模块数平方膨胀，物理上不可实现。现实做法是引入**互联（crossbar fabric）**：用一组共享的交换节点（Socket）做「按需接通」，让「逻辑上全连通、物理上稀疏」。CoralNPU 用的是 OpenSocket 风格的 **1×N + M×1 Socket 组合**，而不是真正的全交叉矩阵（full crossbar）——这一点很关键，后面会展开。

**两类 Socket。** 把多对多的连接拆成两种基本积木：

- **Socket1N（一主多从）**：一个主机面对 N 个从机。问题是「**这次请求该发给哪个从机？**」——按**地址译码**选。
- **SocketM1（多主一从）**：M 个主机面对同一个从机。问题是「**多个主机同时来抢，谁先？**」——按**仲裁（arbiter）**选。

任意「多主机 → 多从机」的网，都可以分解为「先 1N 选从机，再 M1 仲裁主机」的两级结构。CoralNPU 正是这么做的。

> 术语提示：本讲里的 TL-UL 通道（A 通道发请求、D 通道收响应）、`Decoupled` 握手（valid/ready/bits）、`source` 字段（事务 ID）都在 [u3-l3](u3-l3-tlul-axi-bridge.md) 讲过，这里直接复用。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `hdl/chisel/src/soc/CrossbarConfig.scala` | **拓扑的单一真相源**：用 case class 列出所有主机、从机、地址区间、时钟域，以及「主机→可连从机」的连接表；还带一个可独立运行的配置校验器。 |
| `hdl/chisel/src/soc/CoralNPUXbar.scala` | **数据驱动的装配器**：读取 `CrossbarConfig`，生成 IO、实例化 Socket、做地址译码、自动连线；并统一处理宽度转换、跨时钟域与 ECC 完整性。 |
| `hdl/chisel/src/bus/TlulSocket1N.scala` | 一主多从 Socket：地址译码选设备，内部带 FIFO、outstanding 跟踪、错误响应器。 |
| `hdl/chisel/src/bus/TlulSocketM1.scala` | 多主一从 Socket：用 Arbiter 仲裁主机，靠扩展 source ID 回传响应。 |
| `hdl/chisel/src/bus/TlulWidthBridge.scala`（辅助） | 宽度适配器：把窄通道（如 32b）与宽通道（如 128b）互转。 |
| `hdl/chisel/src/bus/TlulToSram.scala`（辅助） | TL-UL → SRAM 接口适配器，是把 SRAM 挂到总线上的「最后一公里」。 |

本讲对应的最小模块是 `CoralNPUXbar.scala`、`CrossbarConfig.scala`、`TlulSocket1N.scala`，外加做仲裁用的 `TlulSocketM1.scala`。

## 4. 核心概念与源码讲解

### 4.1 CrossbarConfig：用纯数据描述整张总线网

#### 4.1.1 概念说明

如果让你设计一个「能随配置变化」的互联，第一直觉可能是把主机/从机/连线写死在 Verilog 里。但 CoralNPU 的模块很多（十几个从机、六个主机），且未来还会增减，写死意味着每次改拓扑都要手改大量连线和地址译码。

`CrossbarConfig` 的思路是**把拓扑变成数据**：用 Scala 的 `case class` 把「主机是谁、多宽、什么时钟域」「从机叫什么、占哪些地址、多宽」「哪个主机允许连哪些从机」全部列成三张表。硬件生成器（`CoralNPUXbar`）只是「解释这些数据」的程序。这样新增一个外设只需在表里加一行，装配逻辑自动跟上。这是「**配置即真相源（single source of truth）**」的典型用法——和 [u3-l1](u3-l1-soc-subsystem.md) 里 `SoCChiselConfig` 是同一套思想。

#### 4.1.2 核心流程

`CrossbarConfig` 对外暴露四样东西：

1. `hosts(enableTestHarness)`：主机清单（名字、位宽、时钟域）。
2. `devices`：从机清单（名字、地址区间序列、时钟域、位宽）。
3. `connections(enableTestHarness)`：一张 `主机 -> Seq[从机]` 的映射表，描述允许的连接。
4. `coralnpu_ranges`：核自身从机（`coralnpu_device`）的地址区间，随 ITCM/DTCM 大小变化。

地址匹配用一个最朴素的范围判断：地址落在 `[base, base+size)` 内即命中。配置正确性的核心约束是**任意两个从机的地址区间不得重叠**——否则地址译码会同时命中两个从机，行为未定义。仓库提供了一个独立校验器 `CrossbarConfigValidator` 来静态检查这一点。

#### 4.1.3 源码精读

先看地址区间如何定义与匹配。`AddressRange` 是一个普通 Scala `case class`，`contains` 把硬件地址 `UInt` 与软件常量比较，返回 Chisel `Bool`：

[CrossbarConfig.scala:27-36](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CrossbarConfig.scala#L27-L36) —— `AddressRange(base, size)` 用 `base.U`/`(base+size).U` 把 Scala 大整数变成硬件常量，`contains(addr)` 即「地址是否落在该区间」。

主机与从机的参数化定义如下，字段都很直白：

[CrossbarConfig.scala:43-57](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CrossbarConfig.scala#L43-L57) —— `HostConfig`（名字/位宽/时钟域）与 `DeviceConfig`（名字/**地址区间序列**/时钟域/位宽）。注意 `addr` 是 `Seq[AddressRange]`：一个从机可以占**多段**地址，例如下面的 `coralnpu_device`。

最具代表性的是 `coralnpu_device` 这一段——核自身作为从机，同时占着 ITCM、DTCM、外设 CSR 三段地址：

[CrossbarConfig.scala:83-102](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CrossbarConfig.scala#L83-L102) —— 默认配置下 ITCM 在 `0x00000000`（8KB）、DTCM 在 `0x00010000`（32KB）、外设 CSR 在 `0x00030000`（4KB），与 [u2-l1](u2-l1-toolchain-linker-tcm.md) 的链接脚本完全对应；若 ITCM/DTCM 用了非默认大小，则切到 `0x00100000`/`0x00200000` 的「highmem」布局。

完整的从机地址表如下（节选），每个外设各占一段 4KB：

[CrossbarConfig.scala:105-122](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CrossbarConfig.scala#L105-L122) —— 注意 `ispyocto_ctrl`、`ddr_ctrl`、`ddr_mem` 都标了非 `main` 的 `clockDomain`，这是装配器要做**跨时钟域处理（CDC）**的依据；位宽也各异（DDR 控制器 32b、DDR 内存 128b）。

最后是「谁可以连谁」的连接表：

[CrossbarConfig.scala:125-139](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CrossbarConfig.scala#L125-L139) —— 例如 `coralnpu_core` 几乎能连所有从机（16 个），而 `autoboot` 只能连 `coralnpu_device`（最小权限，仅够上电启动）。`enableTestHarness=true` 时会额外加一个 32 位的测试主机，用于验证宽度转换。

#### 4.1.4 代码实践

**实践目标**：跑通仓库自带的配置校验器，亲眼看到三张表被打印出来，并确认无地址冲突。

**操作步骤**：

1. 在仓库根目录执行（这是一个真实的 `chisel_binary` 目标）：

   ```bash
   bazel run //hdl/chisel/src/soc:validate_crossbar_config
   ```

2. 观察输出中的 `Hosts`、`Devices`、`Connections` 三段。

**需要观察的现象**：终端会先打印一段 `--- Crossbar Configuration (TestHarness: false) ---`，逐行列出主机、从机（带时钟域与地址区间）、连接表；随后再打印一份 `TestHarness: true` 的版本（多一个 `test_host_32`）；最后以 `Validation successful: No address range collisions found.` 结束。

**预期结果**：两个版本都通过校验、无 `FATAL: Address range collision detected!`。若你人为改动 `devices` 让两段地址重叠，再跑该命令，应当看到异常抛出——这正是 [CrossbarConfig.scala:154-182](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CrossbarConfig.scala#L154-L182) 里那段双重循环碰撞检测在起作用。

> 若本机没有 Bazel/Chisel 工具链，可改为纯源码阅读：直接读 `CrossbarConfigValidator.printConfig`（[CrossbarConfig.scala:186-207](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CrossbarConfig.scala#L186-L207)）理解它会打印什么，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：地址 `0x00010000`（DTCM 起址）落在哪个从机的哪段区间？  
**答案**：落在 `coralnpu_device` 的 `coralnpu_ranges` 第二段 `[0x00010000, 0x00018000)`，即 DTCM。

**练习 2**：为什么 `autoboot` 主机只连了 `coralnpu_device` 一个从机？  
**答案**：`autoboot` 是上电启动器，只需把首条程序写进核的 ITCM/CSR 并启动核，不需要访问其它外设；最小连接范围既省 socket 端口，也符合「最小权限」的安全设计。

**练习 3**：若两个从机的地址区间重叠，`CoralNPUXbar` 的地址译码会怎样？  
**答案**：译码用的是 `MuxCase`（见 4.2.3），按连接表顺序取**第一个命中**的设备，后面的被屏蔽，行为不可预期；因此必须在装配前用 `validate_crossbar_config` 静态拦截。

---

### 4.2 CoralNPUXbar：数据驱动的总线装配器

#### 4.2.1 概念说明

有了 `CrossbarConfig` 这份「拓扑说明书」，还需要一个「施工队」把它变成 RTL。`CoralNPUXbar` 就是这个施工队。它是一个 `Module`（其实是带显式时钟的 `RawModule` 风格），核心工作是把配置里的三张表翻译成五件事：

1. **生成 IO**：主机端口、从机端口、异步时钟域的 clock/reset 端口，全部按配置动态生成。
2. **标准化接口**：把每个主机/从机端口统一「拉到 main 时钟域 + 统一 128 位宽」，这是后续能用同一套 Socket 处理的前提。这一步会按需插入 CDC FIFO、宽度桥、ECC 完整性包装。
3. **实例化 Socket**：给每个主机配一个 `Socket1N`（一主多从），给每个「被多主机共享」的从机配一个 `SocketM1`（多主一从）。
4. **地址译码**：根据地址区间表，组合逻辑地算出「当前请求该选哪个从机」。
5. **自动连线**：主机→1N Socket→（M1 Socket）→从机，全部按名字字典自动接通。

这种「数据驱动 + 反射 + 程序化实例化」的写法是 CoralNPU 顶层装配的统一风格，和 [u3-l1](u3-l1-soc-subsystem.md) 里 `CoralNPUChiselSubsystem` 用 `populatePorts` 拍平 IO 树是同一脉。

#### 4.2.2 核心流程

整体流程可以用下面这段伪代码概括：

```
读 CrossbarConfig:
  hosts[]      = cfg.hosts(...)        # 主机清单
  devices[]    = cfg.devices           # 从机清单
  connections  = cfg.connections(...)  # 主机 -> [可连从机]
  deviceFanIn  = 反查：每个从机被几个主机连  # 用于决定是否需要 M1

# 第一阶段：标准化（在 main 时钟域内统一到 128 位）
for h in hosts:
  h' = PortIntegrity.wrapHost(h)      # ECC：入口生成 A 完整性
  if h.clockDomain != "main": h' = AsyncFIFO(h')   # 跨时钟域
  if h.width  < 128:        h' = WidthBridge(h')    # 升宽
  hostInterfaces[h.name] = h'

for d in devices:
  standardized = Wire(128b, main)
  if d.clockDomain != "main": standardized = AsyncFIFO(standardized)  # 跨到设备域
  if d.width   != 128:        standardized = WidthBridge(standardized) # 降/升宽
  PortIntegrity.wrapDevice(standardized -> io.devices[d])             # ECC：出口生成 D 完整性
  deviceInterfaces[d.name] = standardized

# 第二阶段：实例化 Socket
for h in hosts:  hostSockets[h] = Socket1N(N=len(connections[h]))     # 一主多从
for d in devices if deviceFanIn[d] > 1:
                 deviceSockets[d] = SocketM1(M=deviceFanIn[d])        # 多主一从

# 第三阶段：地址译码（组合逻辑）
for h in hosts:
  hostSockets[h].dev_select = MuxCase(默认=errorIdx, 区间命中->idx)

# 第四阶段：连线
host -> hostSocket.tl_h
hostSocket.tl_d[portIdx] -> (deviceSocket.tl_h[fanInIdx] if fanIn>1 else deviceInterfaces[d])
deviceSocket.tl_d -> deviceInterfaces[d]
```

几个关键点先记住：**所有 Socket 都用统一的 128 位 `commonParams` 实例化**；**宽度/时钟域/ECC 的处理集中在标准化阶段**，让 Socket 本身保持简单；**1N 负责选从机，M1 负责仲裁主机**，二者级联构成多对多。

#### 4.2.3 源码精读

**（1）图分析：算出每个从机的扇入数。** 装配前先反查「每个从机被几个主机连」，这决定了它要不要一个 M1 仲裁器：

[CoralNPUXbar.scala:80-83](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUXbar.scala#L80-L83) —— `deviceFanIn` 把「从机名 → 所有连它的主机列表」建好索引，后续判断 `fanIn > 1` 就用它。

**（2）统一内部总线参数。** 所有 Socket 都跑在同一套参数下——128 位数据、8 位 source ID——这极大地简化了实例化与连线：

[CoralNPUXbar.scala:91-97](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUXbar.scala#L91-L97) —— `commonParams` 固定 `lsuDataBits=128`、`axi2IdBits=8`，`commonWidth=128`。这就是整条 fabric 的「普通话」。

**（3）主机侧标准化（三步：ECC → CDC → 升宽）。** 每个主机端口依次套三层包装，最终输出一个「main 域、128 位、带完整性」的接口：

[CoralNPUXbar.scala:113-134](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUXbar.scala#L113-L134) —— 先用 `PortIntegrity.wrapHost` 在入口生成 A 通道完整性（让主机只需发「干净」的 TL-UL），若主机在非 main 域（如 ISP 的 `isp_axi_clk`）则插一个 `TlulFifoAsync` 做异步跨域。

[CoralNPUXbar.scala:138-142](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUXbar.scala#L138-L142) —— 若主机窄于 128 位（如 `autoboot` 是 32 位、ISP 是 64 位），再用 `TlulWidthBridge` 升到 128 位。

> 旁注：`PortIntegrity` 不是独立文件，它定义在 [TlulIntegrity.scala:278-328](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TlulIntegrity.scala#L278-L328)。`wrapHost` 在入口生成 A 完整性、静默校验 D 完整性；`wrapDevice` 反过来。这就是注释里「xbar generates A-integrity on ingress / D-integrity on ingress」的实现。ECC 细节留到 [u9-l3](u9-l3-secded-integrity.md)，本讲只需知道它是「让 Socket 内部不用关心 ECC」的包装层。

**（4）从机侧标准化（CDC → 降宽 → ECC，顺序与主机侧镜像）。** 从机侧是「先在 main 域得到标准化接口，再向设备域/设备宽度过渡」：

[CoralNPUXbar.scala:150-198](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUXbar.scala#L150-L198) —— 注意从机侧的 WidthBridge 落在**设备自己的时钟域**里（`withClockAndReset(domainPorts.clock, ...)`），最后用 `wrapDevice` 把「干净 TL-UL」交还给设备。

**（5）实例化 Socket。** 这是最关键的一步——按拓扑自动决定每个端口配哪种 Socket：

[CoralNPUXbar.scala:202-212](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUXbar.scala#L202-L212) —— 每个主机配一个 `TlulSocket1N`，其 `N` = 该主机可连的从机数；**仅当某从机的扇入 `> 1`**（被多个主机共享）时，才给它配一个 `TlulSocketM1`，`M` = 扇入数。1:1 的从机不配 M1，后面会直接相连。

**（6）程序化地址译码。** 给每个主机 Socket 算 `dev_select`，纯组合逻辑、当拍有效：

[CoralNPUXbar.scala:219-238](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUXbar.scala#L219-L238) —— 用 `MuxCase` 遍历该主机可连的从机，命中谁的地址区间（`devConfig.addr.map(_.contains(address)).reduce(_ || _)`）就选谁；都不命中则选 `errorIdx`（= 从机数，即「多出来的那一档」），路由到 Socket 内部的错误响应器（见 4.3.3）。

**（7）三级自动连线。** 标准化之后，连线只剩下「按名字查表」：

[CoralNPUXbar.scala:244-268](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUXbar.scala#L244-L268) —— A 段把主机接到它的 1N Socket；B 段把每个 1N Socket 的某个从机输出口，**按该从机是否被多主机共享**，决定接 M1 Socket（`fanIn > 1`）还是直连设备接口；C 段把 M1 Socket 的输出交给设备。

#### 4.2.4 代码实践

**实践目标**：在源码里跟踪「`coralnpu_core` 主机的请求如何被分发到它的某个从机」，验证你对五步装配的理解。

**操作步骤**：

1. 在 `CrossbarConfig.scala` 的 `connections`（[L125-L139](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CrossbarConfig.scala#L125-L139)）里数 `coralnpu_core` 连了几个从机（应为 16）。
2. 在 `CoralNPUXbar.scala` 的 Socket 实例化（[L202-L206](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUXbar.scala#L202-L206)）确认：`coralnpu_core` 会得到一个 `TlulSocket1N(N=16)`，实例名 `coralnpu_core_socket`。
3. 在地址译码段（[L219-L238](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUXbar.scala#L219-L238)）跟踪地址 `0x40030000`（GPIO）：它会被 `gpio` 的区间命中，得到 `idx` = 它在连接表里的下标。

**需要观察的现象**：你会看到 `dev_select` 在 `0 ~ 15` 之间取值，未命中时取 `16`（= `errorIdx`）。

**预期结果**：能口述出「主机地址 → MuxCase 命中某 idx → 该 idx 对应 1N Socket 的 `tl_d(idx)` 输出端口」这条链路。运行验证命令 `bazel run //hdl/chisel/src/soc:validate_crossbar_config` 时**不会**改变这条链路（它只校验地址不重叠），但能保证译码不会「同时命中两个从机」。

#### 4.2.5 小练习与答案

**练习 1**：为什么所有 Socket 都用同一套 `commonParams`（128 位）实例化，而不是各用各的宽度？  
**答案**：统一宽度后，Socket 之间、Socket 与标准化接口之间的连线变成「同类型直连」，无需逐对插宽度桥；宽度差异被集中推到「标准化阶段」的 `TlulWidthBridge`，复杂度局部化。

**练习 2**：一个「只被一个主机连」的从机（如 `rom` 只被 `coralnpu_core` 与 `dma` 连——实际被两个主机连），会不会有 M1 Socket？请以 `autoboot → coralnpu_device` 之外的 1:1 例子思考。  
**答案**：是否配 M1 只看**实际扇入数**（`deviceFanIn`），不看连接表条数。`coralnpu_device` 被 6 个主机连，扇入 6 > 1，故有 M1；若某从机真的只被一个主机连（`deviceFanIn == 1`），则 [L255-L261](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUXbar.scala#L255-L261) 会直接把 1N Socket 的输出连到设备接口，省掉 M1。

**练习 3**：地址译码为什么必须「纯组合、当拍有效」？  
**答案**：`dev_select` 要在 `a.valid` 拉起的同一拍就稳定，否则 Socket1N 内部的 FIFO 与 outstanding 跟踪会拿错设备号；作者特意把原来的 `when` 块改写成 `MuxCase`（见 [L228-L230](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUXbar.scala#L228-L230) 注释）正是为此。

---

### 4.3 TlulSocket1N：一主多从，按地址译码选从机

#### 4.3.1 概念说明

`TlulSocket1N` 解决「一个主机面对 N 个从机，这次请求该发给谁」。它的核心是**地址译码 + 数据分发**：主机侧进一条 A 通道请求，根据外部送来的 `dev_select_i` 选通到 N 个从机端口之一；从机侧回来的 D 通道响应，再按「当初发给谁」原路送回主机。

它源自 OpenTitan 的 `tlul_socket_1n`，CoralNPU 做了简化与改造。它的几个设计要点：

- **`dev_select` 由外部决定**：Socket 自己不译码，译码逻辑放在 `CoralNPUXbar` 里（见 4.2.3），Socket 只接收一个 `dev_select_i` 输入。这样 Socket 是纯通用的、与地址表无关。
- **outstanding 串行化**：为了避免「设备 0 的请求还没回来，又发去设备 1」造成响应错配，Socket 一旦有未完成请求，就**hold 住**新请求，直到当前请求完成或新请求指向同一设备。
- **内置错误响应器**：当 `dev_select` 指向「不存在的设备档」（即 `errorIdx`）时，由一个 `TlulErrorResponder` 立即回一个 `error=true` 的 D 响应，避免总线挂死。

#### 4.3.2 核心流程

```
输入: io.tl_h (主机), io.dev_select_i (选哪个从机)
输出: io.tl_d[0..N-1] (N 个从机端口)

A 通道（请求）:
  fifo_h 缓存主机请求，并把 dev_select 随请求一起 spare 传递 -> dev_select_t
  若 hold_all_requests: 不往下发
  否则按 dev_select_t 选通: tl_u_o(dev_select_t).a.valid = ...
  未命中(>=N): 发给 TlulErrorResponder

outstanding 跟踪:
  num_req_outstanding ++ (有请求 fire 且无响应 fire)
  dev_select_outstanding := 上一次接受的 dev_select_t
  hold_all_requests = (outstanding != 0) && (新选 != 旧选)

D 通道（响应）:
  按 dev_select_outstanding 从对应从机端口收 D，送回主机 fifo_h
```

关键约束是「**outstanding 为 1 时禁止切换设备**」，这就把跨设备的乱序简化成了「同一设备内可乱序，跨设备必须串行」。

#### 4.3.3 源码精读

**（1）IO 与错误档位。** Socket 对外暴露主机口、N 个从机口、以及一个 `dev_select_i`。`NWD` 多算一档用来容纳「错误响应」：

[TlulSocket1N.scala:55-59](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TlulSocket1N.scala#L55-L59) —— `dev_select_i` 位宽 `log2Ceil(N+1)`，即能表示 `0..N-1` 共 N 个设备外加一个「错误档」。

**（2）主机侧 FIFO 把 dev_select 随请求一起搬。** 这是把「外部的组合译码结果」延迟一拍、与请求对齐的关键：

[TlulSocket1N.scala:62-76](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TlulSocket1N.scala#L62-L76) —— `TlulFifoSync` 带了一个 `SpareReqW` 位宽的 `spare_req` 通道，把 `dev_select_i` 当作「伴随请求的行李」一起存进 FIFO，输出端叫 `dev_select_t`。

**（3）outstanding 跟踪与 hold 逻辑。** 这段是 Socket 正确性的命门：

[TlulSocket1N.scala:79-96](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TlulSocket1N.scala#L79-L96) —— `dev_select_outstanding` 记住「当前在飞的请求发给了谁」；只要还有在飞请求、且新请求想换一个设备，就 `hold_all_requests`，拒绝往下发。

**（4）设备侧选通。** 每个从机端口各自带一个 FIFO，只在被选中时才收到 valid：

[TlulSocket1N.scala:108-139](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TlulSocket1N.scala#L108-L139) —— `dev_select = (dev_select_t === i.U) && !hold_all_requests`，只有被选中的那一档把 `a.valid` 透传给对应从机；其余档 `a.valid=0`。

**（5）错误响应器。** 当 `dev_select_t >= N`（即地址没命中任何从机），请求被送给内置的 `TlulErrorResponder`：

[TlulSocket1N.scala:142-165](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TlulSocket1N.scala#L142-L165) —— 错误响应器（定义在同文件 [L10-L32](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TlulSocket1N.scala#L10-L32)）对任何请求都立刻回一个 `opcode=AccessAck, error=true` 的 D 响应。这就是「访问不存在的地址会拿到一个错误响应而非挂死」的实现。

**（6）响应回送。** 按 `dev_select_outstanding` 把对应从机的 D 通道选回主机：

[TlulSocket1N.scala:179-192](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TlulSocket1N.scala#L179-L192) —— 响应按「当初发给谁」回选，默认档指向错误响应器。

#### 4.3.4 代码实践

**实践目标**：理解「访问一个不存在的地址」会发生什么。

**操作步骤**：

1. 阅读 [TlulSocket1N.scala:10-32](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TlulSocket1N.scala#L10-L32) 的 `TlulErrorResponder`，注意它的 `a.ready := true.B`、`d.bits.error := true.B`。
2. 在 `CoralNPUXbar` 的地址译码（[L219-L238](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUXbar.scala#L219-L238)）里确认：地址不在任何区间时 `dev_select = errorIdx = N`。
3. 推演：若 `coralnpu_core` 读地址 `0xDEAD_BEEF`（不在任何从机区间），`dev_select_t` 会等于 16（= 连接的 16 个从机数），落入 Socket1N 的错误档。

**需要观察的现象**：请求被 `TlulErrorResponder` 立即接收（`a.ready=true`），下一拍返回一个 `error=true` 的 D 响应。

**预期结果**：主机拿到一个错误响应，总线不会因「无人响应」而挂死。这是裸机程序访问非法地址能被感知到（进而触发 fault）的前提。若想真实观测，可在 cocotb 测试（[u2-l4](u2-l4-cocotb-testbench-intro.md)）里向一个未映射地址发读，观察返回的 D 通道 `error` 位——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Socket1N 不自己译码，而要外部送 `dev_select_i`？  
**答案**：解耦：Socket1N 只做「按选通分发 + outstanding 控制」，与具体地址表无关；译码依赖 `CrossbarConfig` 的地址区间，放在装配器里更合适，也便于复用到不同拓扑。

**练习 2**：`hold_all_requests` 在什么条件下为真？为何需要它？  
**答案**：当 `num_req_outstanding != 0` 且新请求的 `dev_select_t` 与 `dev_select_outstanding` 不同时为真。它防止「发给设备 A 的请求还在飞，又把新请求发给设备 B」导致 D 通道响应按错的 `dev_select_outstanding` 回选——即保证跨设备串行、避免响应错配。

**练习 3**：`NWD = log2Ceil(N+1)` 而不是 `log2Ceil(N)`，多出的那一档用来干什么？  
**答案**：多出的一档（值为 `N`）专门路由「地址未命中任何从机」的请求，把它送给 `TlulErrorResponder`，确保无效访问也能拿到确定的错误响应。

---

### 4.4 TlulSocketM1：多主一从，按仲裁选主机

#### 4.4.1 概念说明

`TlulSocketM1` 解决相反的问题：「M 个主机都要访问同一个从机，从机只有一组端口，先服务谁？」答案是**仲裁（arbitration）**。

CoralNPU 的实现非常精巧：它**不改协议、不存响应路由表**，而是利用 TL-UL 自带的 `source` 字段（事务 ID）来「记住请求是谁发的」。具体做法：

- **入口扩展 source**：每个主机进来的请求，把自己的 host 编号 `i` 拼到 `source` 的低位，形成 `source' = {source, i}`。这样从机看到的 source 唯一地编码了「原始 source + 主机号」。
- **仲裁**：用一个 `Arbiter` 在 M 个主机里选一个，把它的请求送给从机（0 拍延迟）。
- **出口按 source 回选**：从机回来的 D 响应里带着 `source'`，Socket 取其低 `StIdW` 位当 `host_index`，把响应只送给对应主机，并把 source 还原成原始宽度。

这种「借 source 字段做路由标签」的技巧，让 M1 Socket 几乎是无状态的（不需要存「在飞请求属于谁」的表），面积很小。代价是：从机侧的 source 位宽被扩展了 `log2Ceil(M)` 位（见 `p_d`），需要从机能容纳更宽的 source——CoralNPU 在 `commonParams` 里把 `axi2IdBits` 设到 8 位，余量充足。

#### 4.4.2 核心流程

```
M 个主机 -> 1 个从机

A 通道（请求）:
  for i in 0..M-1:
    hreq_a(i).bits.source = Cat(主机i.source, i)   # 拼主机号到低位
  arb = Arbiter(hreq_a, M)                          # 选一个
  io.tl_d.a = arb.io.out                            # 0 拍送从机

D 通道（响应）:
  host_index = 从机D.source 的低 StIdW 位
  for i in 0..M-1:
    主机i.d.valid = D.valid && (host_index == i)     # 只点亮一个主机
    主机i.d.bits.source = D.source >> StIdW          # 还原原始 source
  从机.d.ready = 被选中主机的 d.ready
```

`StIdW = log2Ceil(M)`，即编码 M 个主机所需位数。

#### 4.4.3 源码精读

**（1）扩展从机侧 source 位宽。** 从机侧的 TL-UL 参数 `p_d` 比 host 侧 `p` 多 `StIdW` 位 source：

[TlulSocketM1.scala:25-36](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TlulSocketM1.scala#L25-L36) —— `p_d` 用 `axi2IdBits = p.o + StIdW` 构造，`tl_d` 端口因此比 `tl_h` 宽出 `StIdW` 位 source。

**（2）入口拼接主机号。** 每个主机的请求在送进仲裁器前，先把 host 编号拼到 source 低位：

[TlulSocketM1.scala:40-46](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TlulSocketM1.scala#L40-L46) —— `hreq_a(i).bits.source := Cat(io.tl_h(i).a.bits.source, i.U(StIdW.W))`。这就是「打路由标签」。

**（3）仲裁。** 用 Chisel 标准库的 `Arbiter`，固定优先级（编号小者优先）：

[TlulSocketM1.scala:49-55](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TlulSocketM1.scala#L49-L55) —— `arb.io.out` 直接驱动 `io.tl_d.a`，**0 拍延迟**（注释明确写了 "Drive device request directly (0 latency)"）。

**（4）出口按 source 回选。** 从机响应回来后，按 source 低位把响应只送给对应主机，并还原 source：

[TlulSocketM1.scala:59-70](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TlulSocketM1.scala#L59-L70) —— `host_index = rsp_bits.source(StIdW-1, 0)`；只有 `host_index === i` 的主机才拿到 `d.valid`，且其 `source` 被右移还原；从机的 `d.ready` 取被选中主机的 ready。

#### 4.4.4 代码实践

**实践目标**：确认「多主机同时抢同一从机」时的仲裁与响应回送。

**操作步骤**：

1. 在 `CrossbarConfig`（[L125-L139](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CrossbarConfig.scala#L125-L139)）里找出所有连 `coralnpu_device` 的主机，确认共 6 个：`coralnpu_core`、`spi2tlul`、`dma`、`ispyocto_m1`、`ispyocto_m2`、`autoboot`。
2. 在 `CoralNPUXbar`（[L208-L212](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUXbar.scala#L208-L212)）确认：`coralnpu_device` 因扇入 6 > 1，会得到一个 `TlulSocketM1(M=6)`，实例名 `coralnpu_device_socket`。
3. 推演：若 `coralnpu_core`（host 0）与 `dma`（按 `deviceFanIn` 顺序，host 编号由 `cfg.hosts` 列表决定）同拍都向 `coralnpu_device` 发请求，`Arbiter` 会选 host 编号小者先服务；另一主机被反压（`a.ready=0`），下拍再试。

**需要观察的现象**：从机侧 `source` 比主机侧宽 3 位（`StIdW = log2Ceil(6) = 3`），低 3 位是主机编号；响应按这 3 位精确回送到唯一一个主机。

**预期结果**：能口述「仲裁 → 拼标签 → 从机处理 → 按标签回选 → 还原 source」的完整一圈。注意：host 编号是 `cfg.hosts(enableTestHarness)` 列表里的下标（[CoralNPUXbar.scala:67](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUXbar.scala#L67) 的 `hostMap`），而 M1 内部的 `fanInIndex`（[L256](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUXbar.scala#L256)）是「连该从机的主机子集」里的下标，二者不同——具体对应关系**待本地对照生成的 SystemVerilog 确认**。

#### 4.4.5 小练习与答案

**练习 1**：M1 Socket 为什么「几乎无状态」？它靠什么记住响应该回给谁？  
**答案**：它不维护「在飞请求 → 主机」的表，而是把主机号拼进 TL-UL 的 `source` 字段，让从机原样回带；响应回来时取 source 低位即得主机号。状态被「外包」给了 source 字段。

**练习 2**：从机侧 source 被加宽了 `log2Ceil(M)` 位，这对系统有什么要求？  
**答案**：从机（及其下游）必须能容纳加宽后的 source 位宽；CoralNPU 通过把 `commonParams.axi2IdBits` 设为 8（[CoralNPUXbar.scala:91-96](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUXbar.scala#L91-L96)）预留了足够余量，使最多 256 个 source ID 可用，拼几位主机号后仍不溢出。

**练习 3**：若两个主机同拍都 valid，`Arbiter` 怎么选？被拒主机当拍会发生什么？  
**答案**：`Arbiter` 固定优先级——编号小者赢，其 `a` 被送出；编号大者当拍 `a.ready=0`（被反压），需保持 `a.valid` 等待下拍仲裁。这就是「多个主机同时访问同一从机」时 M1 的串行化效果。

---

### 4.5 适配器三件套：宽度桥、异步 FIFO、SRAM 桥（拓展）

这一节不是本讲的「最小模块」，但学习目标里要求「了解 `TlulToSram`/`TlulWidthBridge` 等适配器在互联中的位置」，因此简要串联。

**TlulWidthBridge（宽度适配）。** 当主机与从机位宽不同时使用。CoralNPU 的内部总线统一 128 位，但 `autoboot`（32 位）、ISP（64 位）、`ddr_ctrl`（32 位）端口宽度不同，于是在标准化阶段插入此桥。它支持三个方向：宽→窄（把一条 128 位请求拆成多条窄请求，靠扩展 source 的低位当 beat 索引）、窄→宽（把数据移位拼进宽通道）、等宽（直通）。详见 [TlulWidthBridge.scala:43-297](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TlulWidthBridge.scala#L43-L297)。它被 `CoralNPUXbar` 在主机侧（[L138-L142](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUXbar.scala#L138-L142)）与从机侧（[L171-L182](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUXbar.scala#L171-L182)）按需插入。

**TlulFifoAsync（跨时钟域）。** 当主机/从机处在非 `main` 时钟域（如 ISP 的 `isp_axi_clk`、DDR 的 `ddr`）时，用异步 FIFO 做 CDC。主机侧 FIFO 输入在主机域、输出在 main 域（[CoralNPUXbar.scala:125-134](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUXbar.scala#L125-L134)）；从机侧反之（[L158-L167](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUXbar.scala#L158-L167)）。

**TlulToSram（TL-UL → SRAM）。** 这是把 SRAM 挂到 TL-UL 总线上的「最后一公里」：把 A 通道请求翻成 SRAM 的 `enable/write/addr/wdata/wmask`，把 SRAM 读数据经 1 拍 skid buffer 拼成 D 通道响应。它本身不在 `CoralNPUXbar` 里实例化（Xbar 只负责把请求送到 `coralnpu_device`/`sram` 等设备端口），而是由各 SRAM 类型的设备在自己的模块里使用。详见 [TlulToSram.scala:31-90](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TlulToSram.scala#L31-L90)，相关存储子系统在 [u6-l2](u6-l2-tcm-sram.md) 展开。

## 5. 综合实践

把本讲知识串起来，完成下面这个「**画出 CPU 到 DTCM 的完整总线路径**」的任务。

**背景**：标量核 `coralnpu_core` 要读写 DTCM 里的一个全局变量（地址 `0x00012345`，落在 DTCM 区间 `[0x00010000, 0x00018000)`）。请按下面步骤把这条请求的旅程一步步标出来。

**步骤 1：定位地址归属。**  
在 [CrossbarConfig.scala:83-102](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CrossbarConfig.scala#L83-L102) 确认 `0x00012345` 落在 `coralnpu_device` 的 DTCM 段。所以这次访问的目标从机是 `coralnpu_device`（不是 `sram`！）。

**步骤 2：主机侧标准化。**  
`coralnpu_core` 是 128 位、main 域，所以走 [CoralNPUXbar.scala:113-142](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUXbar.scala#L113-L142) 时：`wrapHost` 加 ECC、**不插 CDC FIFO**（已是 main）、**不插 WidthBridge**（已是 128 位）。

**步骤 3：进入主机 Socket1N。**  
请求进入 `coralnpu_core_socket`（`TlulSocket1N(N=16)`）。地址译码（[L219-L238](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUXbar.scala#L219-L238)）命中 `coralnpu_device`，`dev_select` = `coralnpu_device` 在连接表里的下标。

**步骤 4：经 M1 仲裁。**  
由于 `coralnpu_device` 被 6 个主机共享（扇入 6），1N Socket 的这一路输出不直连设备，而是进入 `coralnpu_device_socket`（`TlulSocketM1(M=6)`，[L208-L212](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUXbar.scala#L208-L212)）。若当拍没有别的主机抢，仲裁器放行，把 `coralnpu_core` 的 host 编号拼进 source 低位（[TlulSocketM1.scala:40-46](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TlulSocketM1.scala#L40-L46)）。

**步骤 5：从机侧标准化与送达。**  
M1 输出接到 `deviceInterfaces("coralnpu_device")`（[L266-L268](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUXbar.scala#L266-L268)），再经 `wrapDevice` 还原成干净 TL-UL，送到 `io.devices("coralnpu_device")`，即核自身的设备端口——核内部再用 `TlulToSram` 之类访问 DTCM SRAM（见 [u6-l2](u6-l2-tcm-sram.md)）。

**步骤 6：响应原路返回。**  
DTCM 的读数据经 D 通道回到 M1，Socket 按 source 低位识别出 `coralnpu_core`、还原 source（[TlulSocketM1.scala:59-70](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TlulSocketM1.scala#L59-L70)）；再经 1N Socket 的响应回选（[TlulSocket1N.scala:179-192](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TlulSocket1N.scala#L179-L192)）回到 `coralnpu_core`。

**交付物**：把上述 6 步画成一张数据通路图，标注每一跳经过的模块名、位宽、是否经过 CDC/WidthBridge/ECC。再用一句话回答：**这条路径上一共经过几个 Socket？分别是什么类型？**（答案：2 个——一个 Socket1N 做地址选从机，一个 SocketM1 做多主机仲裁。）

> 进阶（可选）：运行 `bazel run //hdl/chisel/src/soc:validate_crossbar_config`，把输出里 `coralnpu_core -> [...]` 的连接表抄下来，在你画的图上标出「除 `coralnpu_device` 外，`coralnpu_core` 还能直达哪些从机」，体会「一个 1N Socket 如何同时服务 16 个从机」。

## 6. 本讲小结

- CoralNPU 的总线互联是「**数据驱动装配**」：`CrossbarConfig` 用三张表（主机/从机/连接）描述拓扑，`CoralNPUXbar` 读这些表自动生成 IO、Socket、译码与连线——改拓扑只需改数据。
- 多对多互联被分解为两级 Socket：**`TlulSocket1N`**（一主多从，按**地址译码**选从机）在前，**`TlulSocketM1`**（多主一从，按**仲裁**选主机）在后；1:1 直连不配 M1。
- 所有 Socket 统一跑在 128 位、8 位 source 的 `commonParams` 上；位宽/时钟域/ECC 的差异被集中推到「标准化阶段」的 `TlulWidthBridge` / `TlulFifoAsync` / `PortIntegrity`。
- `TlulSocketM1` 的精巧之处是**借 TL-UL 的 `source` 字段当路由标签**（拼主机号到低位），几乎无状态地完成响应回送。
- `TlulSocket1N` 靠 **outstanding 串行化**（跨设备不乱序）+ 内置 **`TlulErrorResponder`**（未命中地址返回错误响应）保证正确性与鲁棒性。
- 一条 CPU→DTCM 的访问会依次经过：主机端口 → wrapHost →（可选 CDC/Width）→ Socket1N 地址译码 →（扇入>1 时）SocketM1 仲裁 → wrapDevice → `coralnpu_device`。

## 7. 下一步学习建议

- **向外看 AXI 边界**：本讲只讲了 SoC **内部**用 TL-UL 的互联；外部 AXI 如何进来、核的取指/访存如何出去，见 [u3-l2](u3-l2-axi-integration.md) 与 [u3-l3](u3-l3-tlul-axi-bridge.md)。
- **向下看存储**：请求到达 `coralnpu_device`/`sram` 后，如何真正访问 SRAM？见 [u6-l2（TCM 与 SRAM）](u6-l2-tcm-sram.md)，那里会展开 `TlulToSram` 与 `SramNx128` 的细节。
- **看 ECC 完整性**：本讲里 `PortIntegrity` 把 ECC 包装在了 Socket 外侧；这套 SECDED 机制详见 [u9-l3（总线完整性与 SECDED）](u9-l3-secded-integrity.md)。
- **动手验证**：学完 [u2-l4（cocotb）](u2-l4-cocotb-testbench-intro.md) 后，可以写一个最小 cocotb 测试，让 `coralnpu_core` 访问一个未映射地址，观察 `TlulErrorResponder` 返回的 `error=true` 响应，把本讲的结论在仿真里证实。
