# BFM：总线功能模型仿真

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚什么是「总线功能模型（Bus Functional Model, BFM）」，它在验证流程里替代什么、为什么不能被综合。
- 区分 hdl-modules 里 BFM 的两种驱动方式：基于 VUnit 验证组件（VC）的 `bus_handle` 方式，以及基于 VUnit `queue` + `integer_array` 的队列方式。
- 看懂 `axi_master` / `axi_lite_master` / `axi_slave` 如何把 VUnit 原语封装成项目自有的 record 接口，并附上协议检查器。
- 理解 `stall_configuration_t` 如何用 OSVVM 随机数制造 ready/valid 的背压抖动，以及这种随机化对验证覆盖率为什么至关重要。
- 自己动手用 `axi_master` 与 `axi_slave` 搭一个带随机背压的回环测试。

## 2. 前置知识

本讲是「专家层·验证方法论」的第一篇，假定你已经具备下列认知（均来自前序讲义）：

- **ready/valid 握手约定**（u2-l1）：`valid` 由主设备驱动、不得组合依赖 `ready`；`ready` 由从设备驱动、可组合依赖 `valid`；二者同拍同时为 1 才完成一次 beat。本讲的 BFM 之所以叫「符合 AXI-Stream 标准」，正是遵守这条规则。
- **AXI / AXI-Lite / AXI-Stream 的 record 化**（u5-l1、u5-l2、u5-l4）：项目用 `axi_pkg`、`axi_lite_pkg` 把五条通道（AR/R/AW/W/B）打包成 `*_m2s_t` / `*_s2m_t` 记录。本讲的 BFM 端口全部用这些 record，而不是裸信号。
- **`sim/` 目录只仿真不综合**（u1-l2）：BFM 全部位于 `modules/bfm/sim/`，进仿真工程、绝不进综合工程。这是 BFM 能放心使用 `wait`、`integer`、动态内存等「不可综合」写法的前提。
- **module_*.py 的 `setup_vunit` 钩子**（u1-l4）：本讲的测试台通过 `module_bfm.py` 的 `setup_vunit` 登记到 VUnit，并用 generic 矩阵批量跑配置。

> 名词速查：**VC（Verification Component，验证组件）** 是 VUnit 自带的、可复用的仿真模型（如 `vunit_lib.axi_lite_master`）。**BFM** 在本项目里指「把 VUnit VC 或自写逻辑封装成项目 record 接口、并加上协议检查与随机化」的那一层薄壳。

## 3. 本讲源码地图

本讲涉及的关键文件全部位于 `modules/bfm/`：

| 文件 | 角色 |
| --- | --- |
| `modules/bfm/sim/axi_master.vhd` | 完整 AXI 主设备 BFM，**封装 VUnit 的 `axi_lite_master` VC**，只能做单拍事务，常用于顶层寄存器读写。 |
| `modules/bfm/sim/axi_lite_master.vhd` | AXI-Lite 主设备 BFM，同样封装 VUnit `axi_lite_master` VC，默认 `bus_handle` 为 `register_bus_master`。 |
| `modules/bfm/sim/axi_read_master.vhd` | 队列驱动的 AXI 读主设备，支持突发，自带 R 通道数据自检。 |
| `modules/bfm/sim/axi_stream_master.vhd` | 队列驱动的 AXI-Stream 主设备，把 `integer_array` 按字节推向数据流。 |
| `modules/bfm/sim/axi_slave.vhd` | 完整 AXI 从设备 BFM，封装 VUnit `axi_read_slave` / `axi_write_slave`，事务落到 VUnit memory 模型。 |
| `modules/bfm/sim/axi_read_slave.vhd` | AXI 读从设备，封装 VUnit `axi_read_slave` VC 并加协议检查。 |
| `modules/bfm/sim/handshake_master.vhd` / `handshake_slave.vhd` | 最底层的纯握手抖动器：分别按概率翻转 `valid` / `ready`，可独立用于任意 record 接口。 |
| `modules/bfm/sim/stall_bfm_pkg.vhd` | 定义 `stall_configuration_t` 与 `random_stall` 过程，是所有随机背压的公共源头。 |
| `modules/bfm/sim/queue_bfm_pkg.vhd` / `memory_bfm_pkg.vhd` | 批量创建 VUnit 句柄（`queue_t` / `memory_t`）的便捷函数，解决仿真器可移植性问题。 |
| `modules/bfm/sim/axi_bfm_pkg.vhd` | AXI BFM 的公共类型：`axi_master_bfm_job_t`（address/length_bytes/id）、默认 stall 配置、4KB 边界裁剪函数。 |
| `modules/bfm/sim/axi_slave_bfm_pkg.vhd` | 定义 `axi_slave_init` 空常量，用于 `axi_slave` 实体按 generic 是否提供来选通读/写从设备。 |
| `modules/bfm/test/tb_axi_read_bfm.vhd` / `tb_handshake_bfm.vhd` | 真实测试台范例，演示如何把 BFM、memory 模型、随机数据串成自检流水线。 |
| `modules/bfm/module_bfm.py` | 在 `setup_vunit` 中用 generic 矩阵登记本模块所有测试台。 |

## 4. 核心概念与源码讲解

### 4.1 什么是 BFM：用仿真模型替代真实主从设备

#### 4.1.1 概念说明

设想你要验证一个 AXI-Lite 寄存器文件（u6-l1）。在真实芯片上，读写它的是 CPU；但在仿真里，你既没有 CPU 的 RTL，也不希望为了跑一次写寄存器就仿真整个 SoC。**BFM 就是用来扮演 CPU（主设备）或外设（从设备）的「精简演员」**：它只懂总线协议的那几个信号，能按你的指令发起读/写事务，或被动响应事务、把数据存进一个软件内存模型。

BFM 的关键属性：

1. **只仿真、不综合**。它可以用 `wait until rising_edge(clk)`、动态分配的 `integer_array`、`real` 概率运算——这些在综合里都非法，但在 `sim/` 目录里天经地义（承接 u1-l2 的目录约定）。
2. **协议正确**。一个好 BFM 自己就遵守 ready/valid 规则（valid 一旦拉高就保持到握手），这样它产出的激励才是「合法」的，被测件（DUT）出错时可以确信是 DUT 的问题而非激励的问题。
3. **带检查**。BFM 不仅驱动，还监督：握手是否合规、字段在事务中途是否被非法改动、返回数据是否匹配预期。

hdl-modules 的 BFM 在实现上有两条路线，本讲会分别拆解：

- **VC 包装路线**（`axi_master`、`axi_lite_master`、`axi_slave`）：薄薄一层，把 VUnit 自带的 VC 包成项目 record 接口，事务由 VUnit 的 `bus_handle` + `net` 调用驱动。
- **队列驱动路线**（`axi_read_master`、`axi_write_master`、`axi_stream_master/slave`）：测试台把「作业」和数据推进 VUnit `queue`，BFM 自己消费队列、自己注入随机抖动、自己比对结果。

#### 4.1.2 核心流程

一个典型 BFM 验证回环的数据流（以读为例）：

```text
测试台进程                 主设备 BFM                 DUT              从设备 BFM
   |  推 job 到 job_queue      |                        |                   |
   |-------------------------->|                        |                   |
   |                           | 按 job 驱动 AR 通道 ---->|                   |
   |                           |                        | 处理（如交叉栏）--->| 落到 memory 模型
   |                           |<--- R 通道返回数据 ------|<------------------|
   |                           | 与 reference_data 比对  |                   |
   |<--- num_bursts_checked ----|                        |                   |
```

主设备负责「发激励 + 比对」，从设备负责「响应 + 记录到 memory」。两端都注入随机 stall，于是 DUT 被迫面对各种 ready/valid 时序组合。

#### 4.1.3 源码精读

先看 `bfm` 模块自述定位。它明确说这些组件「用于高效 VHDL 仿真」，用途是「在你的测试台里发送激励或检查数据」：

[modules/bfm/doc/bfm.rst:1-5](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/doc/bfm.rst#L1-L5) — 模块文档说明 BFM 支持 AXI / AXI-Lite / AXI-Stream 三种协议，定位为「发激励 / 查数据」。

[modules/bfm/readme.rst:1-5](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/readme.rst#L1-L5) — readme 同样强调「efficient VHDL simulation」，并指向网站。

`axi_master.vhd` 的文件头注释把「VC 包装路线」讲得很直白：它包装 VUnit 的 `axi_lite_master` VC，因此**不能产生突发**，只适合单拍寄存器读写；如果要突发，得改用 `axi_read_master` / `axi_write_master`：

[modules/bfm/sim/axi_master.vhd:9-26](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/sim/axi_master.vhd#L9-L26) — 注释说明本实体包装 AXI-Lite VC、不支持突发，常用于顶层寄存器总线。

#### 4.1.4 代码实践

**实践目标**：从目录约定上确认 BFM 是「仿真专用件」。

**操作步骤**：

1. 在仓库根目录执行 `git ls-files modules/bfm/`，观察所有 `.vhd` 是否都落在 `sim/` 或 `test/` 下。
2. 对照 u1-l2 的规则，确认 `sim/` 下的文件不会进入综合工程。

**预期现象**：除 `module_bfm.py`、文档外，所有 VHDL 都在 `sim/`（BFM 本体）或 `test/`（测试台），没有任何 BFM 出现在 `src/`——因为它们不可综合。

#### 4.1.5 小练习与答案

**练习 1**：为什么 BFM 可以用 `wait until rising_edge(clk)` 和 `real` 类型的概率，而 `src/` 里的实体不行？

> **答案**：`src/` 里的代码要被综合成真实电路，`wait`、浮点、动态内存都没有对应的硬件原语；BFM 只在仿真器里运行，本质是一段「假装成硬件」的顺序程序，所以可以自由使用这些写法。这也正是它们被隔离在 `sim/` 目录的原因（承接 u1-l2）。

**练习 2**：`axi_master` 既然端口是「完整 AXI」，为什么说它不能发突发？

> **答案**：它的内部实现包装的是 VUnit 的 `axi_lite_master` VC，后者只产生单拍事务。`axi_master` 只是把 AXI-Lite 的单拍事务「升格」呈现成完整 AXI 接口（见 4.2.3 中 `len <= len` 固定为 1 拍），所以突发能力并不存在。

---

### 4.2 VC 包装路线：`axi_master` 与 `axi_lite_master`

#### 4.2.1 概念说明

VUnit 自带一批经验证的 VC（如 `vunit_lib.axi_lite_master`、`vunit_lib.axi_read_slave`），它们的接口是**裸的标量信号**，调用方式是测试台通过一个 `bus_master_t` 句柄（`bus_handle`）在 `net` 上发 `write_bus` / `read_bus` 这类消息。

hdl-modules 不想直接用裸信号（项目全用 record），也不想放弃 VUnit VC 的成熟度，于是采用**包装模式**：

- 端口用项目的 record 类型（`axi_read_m2s_t` 等），与 DUT 无缝对接；
- 内部例化 VUnit VC，把 record 字段拆成标量喂给 VC；
- 再额外挂上 `common.axi_stream_protocol_checker`，对每条通道做协议自检。

这样测试台既能用 VUnit 的 `bus_handle` 调用风格，又能享受 record 接口的整洁，还能自动发现协议违规。

#### 4.2.2 核心流程

```text
测试台: write_bus(net, bus_handle, addr, data)
            |
            v
VUnit axi_lite_master VC  --(标量信号)-->  record 转换  --(axi_*_m2s_t)--> DUT
                                                ^
                                  axi_stream_protocol_checker（每通道一个）
```

每条 AXI 通道（AR/R/AW/W/B）都对应一个协议检查器实例；检查器只连 `ready`（以及该通道的 `valid`/`data`/`last`/`id`/`user`），用来监督握手合法性与字段稳定性。

#### 4.2.3 源码精读

**实体端口**。`axi_master` 的 generic 只有一个 `bus_handle`，端口是完整的读、写 record 对：

[modules/bfm/sim/axi_master.vhd:53-71](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/sim/axi_master.vhd#L53-L71) — 实体声明：`bus_handle` 驱动一切，端口用 `axi_read_m2s_t` / `axi_read_s2m_t` / `axi_write_m2s_t` / `axi_write_s2m_t`。

**固定为单拍**。架构体里用 `bus_handle` 解析出 `data_width`，并把每次事务的 `len` 钉死为 1 拍、`size` 设为整字、`burst` 设为 INCR——这正是「包装 AXI-Lite VC」的体现：

[modules/bfm/sim/axi_master.vhd:75-110](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/sim/axi_master.vhd#L75-L110) — `len`/`size`/`burst` 在此被固化，record 字段经局部 `slv` 信号与 VC 桥接。

**例化 VUnit VC**。中间这块就是包装的核心：把 record 的每个字段一对一映射到 VUnit `axi_lite_master` 的标量端口，并设 `drive_invalid_val => '0'` 以避免大量 `'X'` 比较警告拖慢仿真：

[modules/bfm/sim/axi_master.vhd:114-145](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/sim/axi_master.vhd#L114-L145) — 例化 `vunit_lib.axi_lite_master`，五条通道逐信号对接。

**协议检查器阵列**。AR/AW/W 三个「请求类」通道各挂一个只连 `ready` 的检查器（监督对端是否合规地处理请求）；R/B 两个「响应类」通道挂带 `data`/`id`/`user` 的检查器，监督 DUT 返回的数据是否在事务中途乱跳：

[modules/bfm/sim/axi_master.vhd:148-218](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/sim/axi_master.vhd#L148-L218) — 五个 `axi_stream_protocol_checker` 实例，分别守 AR/R/AW/W/B。

`axi_lite_master` 的结构与 `axi_master` 几乎一模一样，区别只在端口是 AXI-Lite record，且 `bus_handle` 的默认值被设为 `register_file.register_operations_pkg.register_bus_master`——方便直接配合寄存器操作便捷函数（u6-l1、u7-l3 都用到）：

[modules/bfm/sim/axi_lite_master.vhd:47-59](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/sim/axi_lite_master.vhd#L47-L59) — `bus_handle : bus_master_t := register_bus_master`，默认句柄指向寄存器总线。

#### 4.2.4 代码实践

**实践目标**：理解 VC 包装如何把一条 `write_bus` 调用变成 record 上的电平变化。

**操作步骤**：

1. 打开 [modules/bfm/sim/axi_master.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/sim/axi_master.vhd)。
2. 从端口 `axi_write_m2s.aw.addr` 往回追：它由局部信号 `awaddr` 驱动，而 `awaddr` 来自 VUnit VC 的 `awaddr` 输出。
3. 想象测试台调用 `write_bus(net, bus_handle, addr, data)`：VUnit VC 收到消息后驱动 `awaddr`/`wdata`/`wstrb`，再经这几行赋值出现在 record 上。

**需要观察的现象**：record 的每个字段都「透明地」反映了 VUnit VC 的标量输出，没有任何额外时序逻辑——包装层是纯组合的桥接。

**预期结果**：能用自己的话说出「一条 `write_bus` 调用 → VUnit VC → 标量桥接 → record 端口」这条链路。

#### 4.2.5 小练习与答案

**练习 1**：`axi_master` 例化 VC 时为什么加 `drive_invalid_val => '0'`？

> **答案**：注释写明是为了避免大量 `NUMERIC_STD."=": meta value detected` 警告——VUnit VC 在不驱动时会输出 `'X'`，与 `std_logic` 比较时触发警告，会严重拖慢仿真。设成 `'0'` 让无效值是确定电平。

**练习 2**：R 通道检查器连了 `user => axi_read_s2m.r.resp`，为什么把 `resp` 当 `user`？

> **答案**：`axi_stream_protocol_checker` 是个通用 AXI-Stream 检查器，没有「resp」概念；而 R 通道的 `resp` 字段正是一个「随每拍数据出现的副带信息」，语义上等价于 AXI-Stream 的 `tuser`。把它接到 `user` 上，检查器就能监督「resp 在事务中途不能乱变」这一协议要求。

---

### 4.3 队列驱动路线：`axi_read_master` 与 `axi_stream_master`

#### 4.3.1 概念说明

VC 包装路线有个局限：事务是**阻塞式**的——测试台发一条 `read_bus`，要等它返回才能发下一条；而且激励要测试台自己一拍拍写，很难做大规模随机。

队列驱动路线换了个思路：**把「要做什么」打包成作业（job）推进队列，BFM 自己消费队列、自己发事务、自己比对结果**。测试台只管「喂作业 + 喂期望数据」，BFM 在后台并发地把随机化的事务铺满总线。这非常适合「跑 50 笔随机突发」这类回归测试。

这条路线下，两类 VUnit 数据结构是主角：

- **`queue_t`**：一个 FIFO，可以压入 `integer`、`slv`、或对象的引用（`push_ref`）。
- **`integer_array_t`**：一个动态整型数组，本项目中每个元素代表一个**无符号字节**，按小端序排列。

#### 4.3.2 核心流程

以 `axi_read_master` 为例：

```text
1. 测试台构造 job = {address, length_bytes, id}，push(job_queue)
   测试台构造期望数据 random_data（integer_array），push_ref(reference_data_queue)
2. BFM 的 set_ar 进程：pop(job_queue) -> 算出 len -> 驱动 AR 通道（带随机 stall）
3. DUT/从设备返回 R 通道数据
4. BFM 内部的 axi_stream_slave：逐拍把 R 数据与 reference_data 比对，OKAY 检查 resp
5. 收到 r.last -> num_bursts_checked + 1
6. 测试台 wait until num_bursts_checked = num_bursts_expected -> 结束
```

#### 4.3.3 源码精读

**作业类型**。`axi_bfm_pkg` 定义了读/写主设备共用的作业记录，并用 `to_slv` / `to_axi_bfm_job` 在 record 与定长 `slv` 间转换（因为 VUnit `queue` 只能压标量，所以把 job 序列化成 96 位向量再压队列）：

[modules/bfm/sim/axi_bfm_pkg.vhd:21-33](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/sim/axi_bfm_pkg.vhd#L21-L33) — `axi_master_bfm_job_t` 含 `address` / `length_bytes` / `id`，定长 96 位。

[modules/bfm/sim/axi_bfm_pkg.vhd:59-77](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/sim/axi_bfm_pkg.vhd#L59-L77) — `to_slv` / `to_axi_bfm_job` 把 job 打包/解包成三段 32 位。

**`axi_read_master` 实体**。它的 generic 就是两个队列加两个 stall 配置：

[modules/bfm/sim/axi_read_master.vhd:72-103](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/sim/axi_read_master.vhd#L72-L103) — `job_queue` 喂事务、`reference_data_queue` 喂期望数据，`ar_stall_config` / `r_stall_config` 控制两条通道的抖动。

**消费作业、驱动 AR**。`set_ar` 进程等待队列非空，pop 出作业，换算成 `len`（按字节向上取整到整拍），驱动地址/ID，并把这个 job 的 id 压进 `r_id_queue` 供 R 通道核对：

[modules/bfm/sim/axi_read_master.vhd:132-154](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/sim/axi_read_master.vhd#L132-L154) — `pop(job_queue)` → 设定 `addr/len/id` → 等 AR 握手完成。

**AR 通道复用 `handshake_master`**。注意 AR 的 `valid` 不是直接给，而是交给 `handshake_master`（4.4 节详述）按概率翻转，从而合法地注入 stall：

[modules/bfm/sim/axi_read_master.vhd:158-169](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/sim/axi_read_master.vhd#L158-L169) — AR 的 valid 由 `handshake_master` 产生；字段在 `valid` 为 0 时驱动 `'X'`。

**R 通道自检**。R 这边例化的是 `axi_stream_slave`（带 `reference_data_queue` 与 `reference_id_queue`），逐拍比对数据与 ID；另外 `check_r` 进程单独检查每拍 `resp = OKAY`，并在 `r.last` 时把 `num_bursts_checked` 加 1：

[modules/bfm/sim/axi_read_master.vhd:193-230](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/sim/axi_read_master.vhd#L193-L230) — `check_r` 校验响应码并计数；`axi_stream_slave` 做实际的数据/ID 比对。

**`axi_stream_master` 的字节级发送**。它的主体是一个进程：从 `data_queue` 用 `pop_ref` 取出一个 `integer_array`，然后**逐字节**填进当前 beat 的对应字节通道，凑满一拍或到包尾才发起一次握手。包长不必对齐位宽，最后一拍自动出现「部分字节有效」：

[modules/bfm/sim/axi_stream_master.vhd:139-190](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/sim/axi_stream_master.vhd#L139-L190) — 逐字节填充 `data_int` / `strobe_byte`，凑满 beat 才 `wait until ready and valid`。

**无效值驱动**。`assign_invalid` 进程确保：当 `valid` 为 0 时，`data`/`last`/`strobe` 全部驱动成 `'X'`（`drive_invalid_value`），这样 DUT 不会在错误的时钟周期采样到残留数据：

[modules/bfm/sim/axi_stream_master.vhd:227-240](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/sim/axi_stream_master.vhd#L227-L240) — `valid` 为真才输出真实数据，否则输出 `'X'`。

#### 4.3.4 代码实践

**实践目标**：读懂 `tb_axi_read_bfm` 如何用队列驱动 + memory 模型跑随机突发。

**操作步骤**：

1. 打开 [modules/bfm/test/tb_axi_read_bfm.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/test/tb_axi_read_bfm.vhd)。
2. 关注 `send_random_burst` 过程（L86-133）：它在 VUnit `memory` 里分配一块缓冲，用 `random_integer_array` 生成随机数据并 `write_integer_array` 预写进 memory，再把 job 压入 `job_queue`、把同一份随机数据压入 `data_queue` 作为期望值。
3. 关注 `axi_slave_t` 的构造（L64-72）：带 `address_stall_probability => 0.3`、`data_stall_probability => 0.5` 和响应延迟范围——这就是从设备侧的随机背压。
4. 主循环 `for i in 0 to 50 loop send_random_burst;`（L139-143）批量发 51 笔随机突发。

**需要观察的现象**：测试台自己**不做逐拍比对**，比对完全由 `axi_read_master` 内部的 `axi_stream_slave` 完成；测试台只等 `num_bursts_checked = num_bursts_expected`。

**预期结果**：能解释「memory 模型既是数据的源头（写进去）又是期望值的来源（同一份数据）」这一自检套路。

#### 4.3.5 小练习与答案

**练习 1**：为什么 job 要先 `to_slv` 再压队列，而不是直接 `push(job_queue, job)`？

> **答案**：VUnit 的 `queue_t` 只内置了对标量类型（`integer`、`slv` 等）的 push/pop，不会自动处理用户自定义 record。所以把 job 序列化成定长 `slv` 压入，取出时再用 `to_axi_bfm_job` 反序列化。

**练习 2**：`axi_stream_master` 要求每个 `integer_array` 元素是「一个无符号字节」，那如果要发 32 位宽的 beat，数据怎么排？

> **答案**：一个 32 位 beat 含 4 个字节，所以 `integer_array` 里连续 4 个元素拼成一拍（小端序，低字节在前）。BFM 的 `byte_idx mod bytes_per_beat` 决定每个字节落在 beat 的哪一段。

---

### 4.4 随机背压：`stall_bfm_pkg` 与 `stall_configuration_t`

#### 4.4.1 概念说明

确定性激励（永远 `ready=1`、永远 `valid=1`）只验证了「快乐路径」。但真实硬件 bug 往往藏在 ready/valid 的**时序角落**里：例如某模块错误地让 `valid` 组合依赖 `ready`（违反 AXI 规则），在永远握手时表现正常，一旦 ready 抖动就暴露——或者更糟，永远不暴露，直到流片后接到真实 CPU。

`stall_bfm_pkg` 提供了一个统一的随机抖动机制：每次握手前，按概率决定是否插入若干周期的 stall，插入多少周期用一个「偏向小值」的随机分布。这套机制被 `handshake_master` / `handshake_slave` 以及所有 AXI/AXI-Stream BFM 复用。

随机种子由 VUnit 的 seed 机制提供（每个测试用例独立可复现），可以用 `--seed` 命令行参数固定。

#### 4.4.2 核心流程

`stall_configuration_t` 是一个三字段记录：

\[ \text{stall\_config} = (\,p,\; n_{\min},\; n_{\max}\,) \]

其中 \(p \in [0,1]\) 是「本次要不要 stall」的概率，\(n_{\min}, n_{\max}\) 是 stall 周期数的范围。`random_stall` 的行为可写成：

```text
procedure random_stall(stall_config, rnd, clk):
    if rnd.Uniform(0,1) < stall_config.stall_probability:   # 以概率 p 触发
        n = rnd.FavorSmall(min_stall_cycles, max_stall_cycles)  # 偏向小值的随机周期数
        for i in 1..n:
            wait until rising_edge(clk)
```

`FavorSmall` 是 OSVVM 的分布，结果落在 \([n_{\min}, n_{\max}]\) 但更可能取小值——这样大多数 stall 很短、偶尔很长，既覆盖了「轻微背压」也覆盖了「长时间背压」，而仿真总时长不会爆炸。

从覆盖率角度看，一个包有 \(B\) 拍，每拍是否 stall 是一次独立伯努利试验，期望 stall 拍数约为：

\[ E[\text{stalls}] \approx B \cdot p \cdot \frac{n_{\min}+n_{\max}}{2} \]

但更重要的是**质性覆盖**：随机 stall 让 DUT 经历 `valid` 在 `ready` 之前/之后到达、`ready` 在事务中途掉落等各种组合，这些是用确定性激励打不出来的。

#### 4.4.3 源码精读

**`stall_configuration_t` 与零配置常量**。这个类型是 VUnit `axi_stream_pkg` 里同名类型的克隆——克隆它是因为不想为了一个 stall 类型把整个庞大的 `axi_stream_pkg` 拉进小型测试台（拖慢仿真启动）：

[modules/bfm/sim/stall_bfm_pkg.vhd:19-39](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/sim/stall_bfm_pkg.vhd#L19-L39) — 定义 `stall_configuration_t` 记录、`zero_stall_configuration` 与 `random_stall` 过程原型。

**`random_stall` 实现**。这就是 4.4.2 伪代码的真身，用 OSVVM `RandomPType`：

[modules/bfm/sim/stall_bfm_pkg.vhd:43-60](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/sim/stall_bfm_pkg.vhd#L43-L60) — 概率判定 + `FavorSmall` 取周期数 + 循环 `wait`。

**默认 stall 配置**。`axi_bfm_pkg` 给出了 AXI BFM 的默认值：地址通道 stall 概率 0.3、最多 30 周期（让 W 数据可能远早于 AWVALID 出现），数据通道概率 0.3、最多 4 周期（轻微抖动即可）：

[modules/bfm/sim/axi_bfm_pkg.vhd:36-47](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/sim/axi_bfm_pkg.vhd#L36-L47) — `default_address_stall_config` 与 `default_data_stall_config`。

**`handshake_master` 把 stall 接到 valid**。它的核心是 `valid <= data_is_valid and let_data_through`，而 `let_data_through` 由一个进程周期性地拉低（stall）再拉高，并只在握手完成后才进入下一轮——这保证 `valid` 一旦拉高就保持到握手，符合 AXI 规则：

[modules/bfm/sim/handshake_master.vhd:62-91](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/sim/handshake_master.vhd#L62-L91) — `valid` 由 `let_data_through` 门控；`toggle_stall` 进程用 `get_seed(salt=>...path_name)` 给每个实例独立随机序列。

注意 `get_seed` 的 `salt` 用了实体的 `'path_name`，这样**同一测试台里多个 `handshake_master` 实例**会得到不同的随机序列，避免它们同步抖动。

**`handshake_slave` 把 stall 接到 ready，并支持 well-behaved**。它多了一个 `well_behaved_stall` generic：若为真，则 `ready` 一旦拉高就保持到 `valid` 到来（即「不会在 valid 到来前掉 ready」），这对应很多省资源模块的「well-behaved 从设备」假设（承接 u5-l2 的 well-behaved master 概念）：

[modules/bfm/sim/handshake_slave.vhd:46-97](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/sim/handshake_slave.vhd#L46-L97) — `ready <= data_is_ready and let_data_through`；`well_behaved_stall` 改变 `wait` 条件。

#### 4.4.4 代码实践

**实践目标**：用 `tb_handshake_bfm` 观察不同 stall 概率下的吞吐与鲁棒性。

**操作步骤**：

1. 打开 [modules/bfm/test/tb_handshake_bfm.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/test/tb_handshake_bfm.vhd)。这个测试台把 `handshake_master` → `handshake_pipeline`（DUT）→ `handshake_slave` 串成回环。
2. 看 stall 配置如何由 generic 百分比换算（L38-48）：`stall_probability => real(percent) / 100.0`。
3. 看 `module_bfm.py` 如何为不同测试名定制 stall（见 4.5.3 引用的 L62-72）：`test_full_master_throughput` 主设备 stall 为 0、其余为 50%。

**需要观察的现象**：满吞吐用例里 `result_valid` 长时间保持为 1；50% stall 用例里握手被频繁打断，但 `transaction_count > 50` 的断言仍成立。

**预期结果**：能说出「stall 概率只影响吞吐和时序组合，不影响事务正确性——正确性由握手协议和协议检查器保证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `handshake_master` 的 `toggle_stall` 进程里用 `wait until (ready and valid)='1'` 而不是固定 stall 一拍？

> **答案**：因为它要保证「`valid` 拉高后保持到握手完成」。stall 是在 `valid` 还没拉高之前插入的（`let_data_through='0'` 时 `valid` 为 0）；一旦放行，就必须等到真正握手（`ready and valid`）才能进入下一轮 stall，否则 `valid` 会在握手前掉落，违反 AXI 规则。

**练习 2**：把 stall 概率从 0 调到 0.5，事务的**结果**会变吗？为什么？

> **答案**：不会。握手协议保证「只有 ready 与 valid 同拍为 1 才搬一字」。stall 只改变这个时刻何时到来，不改变搬什么、搬多少。所以正确实现下结果不变——这正是随机背压测试的价值：它能在不改变期望结果的前提下，挤压出时序相关的 bug。

---

### 4.5 从机、memory 模型与支撑包

#### 4.5.1 概念说明

完整回环还需要从设备那一半。hdl-modules 的 `axi_slave` 是个**分发型包装器**：根据你是否提供了 `axi_read_slave` / `axi_write_slave` 句柄，用 `generate` 选择性地例化读从设备和/或写从设备。真正的活儿由 VUnit 的 `axi_read_slave` / `axi_write_slave` VC 干——它们把进来的 AXI 事务**应用到一个 VUnit memory 模型**（`memory_t`）。于是测试台可以预先往 memory 写期望数据，或事后从 memory 读回 DUT 写进去的数据来核对。

此外还有一组「支撑包」，它们不实现协议，只解决两个工程问题：

1. **批量建句柄**：`queue_bfm_pkg` / `memory_bfm_pkg` 提供 `get_new_queues` / `get_new_memories`，用循环给数组的每个元素各建一个新句柄。
2. **空常量与可移植性**：`axi_slave_bfm_pkg` 提供 `axi_slave_init` 空常量，让 `axi_slave` 实体能用「generic 是否等于空常量」来判断要不要例化对应从设备。

#### 4.5.2 核心流程

```text
axi_slave 实体
   |-- axi_read_slave_gen:  if axi_read_slave /= axi_slave_init generate  -> 例化 axi_read_slave
   |-- axi_write_slave_gen: if axi_write_slave /= axi_slave_init generate -> 例化 axi_write_slave
            |
            v
     axi_read_slave / axi_write_slave（项目薄壳）
            |-- 例化 VUnit vunit_lib.axi_read_slave / axi_write_slave
            |-- 挂 axi_stream_protocol_checker（检查上游 master 是否合规）
            |-- VUnit VC 把事务应用到 memory_t
```

#### 4.5.3 源码精读

**`axi_slave` 的条件例化**。它只是个分发壳：读、写从设备各自一个 `generate`，条件是「对应的句柄不等于空常量」：

[modules/bfm/sim/axi_slave.vhd:56-100](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/sim/axi_slave.vhd#L56-L100) — 两个 generate 块按 generic 选通读/写从设备。

**空常量 `axi_slave_init`**。它的所有概率字段为 0、句柄为 null，专门用来表示「未配置」：

[modules/bfm/sim/axi_slave_bfm_pkg.vhd:20-37](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/sim/axi_slave_bfm_pkg.vhd#L20-L37) — `axi_slave_init` 把 stall 概率、延迟都置零，actor/memory/logger 为 null。

**`axi_read_slave` 把 record 喂给 VUnit VC**。与主设备对称，从设备把上游 record 拆成标量送给 `vunit_lib.axi_read_slave`，并挂协议检查器监督**上游主设备**是否合规（注意：主设备的检查器监督下游，从设备的检查器监督上游，两边互相盯）：

[modules/bfm/sim/axi_read_slave.vhd:69-136](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/sim/axi_read_slave.vhd#L69-L136) — 例化 VUnit VC + AR/R 通道协议检查器。

**批量建句柄**。`queue_bfm_pkg` 的 `get_new_queues` 解决一个仿真器可移植性陷阱：在某些仿真器（如 Modelsim）里 `(others => new_queue)` 只求值一次，导致数组所有元素共享同一个队列；用显式循环则每次都调用 `new_queue`，跨仿真器安全：

[modules/bfm/sim/queue_bfm_pkg.vhd:17-30](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/sim/queue_bfm_pkg.vhd#L17-L30) — 注释解释动机，`get_new_queues` 用循环逐个建队列。

[modules/bfm/sim/memory_bfm_pkg.vhd:17-33](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/sim/memory_bfm_pkg.vhd#L17-L33) — `memory_bfm_pkg` 同理，提供 `get_new_memories` 与 `memory_vec_t` / `buffer_vec_t`。

**测试台如何配出带背压的从设备**。回到 `tb_axi_read_bfm`，看 `new_axi_slave` 如何把 stall 概率与响应延迟写进从设备句柄：

[modules/bfm/test/tb_axi_read_bfm.vhd:64-72](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/test/tb_axi_read_bfm.vhd#L64-L72) — `address_stall_probability => 0.3`、`data_stall_probability => 0.5`、响应延迟 12–20 周期。

**`module_bfm.py` 用 generic 矩阵把这一切跑起来**。`setup_vunit` 为每种 BFM 测试台枚举 generic；`handshake` 测试还会按测试名给主/从设备分别配 0% 或 50% 的 stall：

[modules/bfm/module_bfm.py:21-31](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/module_bfm.py#L21-L31) — `setup_vunit` 分发到各 `setup_*_tests`。

[modules/bfm/module_bfm.py:62-72](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/module_bfm.py#L62-L72) — 按测试名把 stall 概率（0 或 50）写进 generic。

#### 4.5.4 代码实践

**实践目标**：体会「memory 模型既是数据源又是数据汇」的自检闭环。

**操作步骤**：

1. 重读 [modules/bfm/test/tb_axi_read_bfm.vhd:86-133](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/test/tb_axi_read_bfm.vhd#L86-L133) 的 `send_random_burst`。
2. 画一张图：`random_integer_array` 生成的数据同时流向（a）`write_integer_array` 写进 memory（供从设备读出返回），和（b）`push_ref(data_queue, ...)` 作为期望值（供主设备比对）。
3. 注意若 `burst_length_bytes` 不对齐 beat，会额外 `allocate` 一块 dummy 缓冲，避免读到未分配区域。

**需要观察的现象**：同一份随机数据被「写进 memory」和「作为期望值」双向使用，回环里没有任何硬编码的期望值。

**预期结果**：能解释为什么这种写法可以「发 51 笔随机突发」而测试台代码完全不关心具体数据——因为数据正确性由 BFM 自动比对。

#### 4.5.5 小练习与答案

**练习 1**：`axi_slave` 实体怎么知道用户想要「只读」「只写」还是「读写都要」？

> **答案**：通过两个 generic `axi_read_slave` 和 `axi_write_slave` 是否等于空常量 `axi_slave_init` 来判断。两个 `generate` 各自检查对应句柄，非空才例化对应的从设备。用户只提供需要的那一侧的句柄即可。

**练习 2**：为什么需要 `get_new_queues` 这种函数，而不能写 `constant qs : queue_vec_t(0 to 3) := (others => new_queue);`？

> **答案**：因为不同仿真器对聚集初始化中函数求值的时机处理不同。GHDL 会对每个元素各求值一次（得到 4 个不同队列），但 Modelsim 只求值一次，导致 4 个元素指向同一个队列，测试会诡异失败。显式循环函数在所有仿真器上都逐个调用 `new_queue`，规避了这个可移植性陷阱。

---

## 5. 综合实践

把本讲知识串起来，设计一个**带随机背压的 AXI 回环测试**：用 `axi_master`（VC 包装、单拍）作为主设备，`axi_slave`（落到 VUnit memory）作为从设备，跑若干笔写+读回事务，验证背压鲁棒性。

**实践目标**：亲手把「VC 包装主设备 + memory 模型从设备 + 随机 stall」三件事接到一起。

**操作步骤**（示例代码，待本地验证）：

1. 在 `modules/bfm/test/` 下新建一个测试台 `tb_axi_loopback.vhd`（示例代码，非项目原有文件），骨架如下：

   ```vhdl
   -- 示例代码：仅示意结构，未经验证
   library ieee;
   use ieee.std_logic_1164.all;

   library vunit_lib;
   use vunit_lib.run_pkg.all;
   use vunit_lib.bus_master_pkg.all;
   use vunit_lib.memory_pkg.all;
   use vunit_lib.axi_slave_pkg.all;
   use vunit_lib.logger_pkg.all;

   library axi;
   use axi.axi_pkg.all;

   entity tb_axi_loopback is
     generic (
       data_width : axi_data_width_t := 32;
       runner_cfg : string
     );
   end entity;

   architecture tb of tb_axi_loopback is
     signal clk : std_ulogic := '0';
     constant clk_period : time := 5 ns;

     signal axi_read_m2s : axi_read_m2s_t := axi_read_m2s_init;
     signal axi_read_s2m : axi_read_s2m_t := axi_read_s2m_init;
     signal axi_write_m2s : axi_write_m2s_t := axi_write_m2s_init;
     signal axi_write_s2m : axi_write_s2m_t := axi_write_s2m_init;

     -- 主设备句柄：地址/数据宽度由此决定
     constant bus_handle : bus_master_t := new_bus(
       address_length => 32, data_length => data_width
     );

     -- 从设备使用的 memory 模型与带背压的 axi_slave 句柄
     constant memory : memory_t := new_memory;
     constant slave_cfg : axi_slave_t := new_axi_slave(
       memory => memory,
       address_stall_probability => 0.3,   -- 随机背压
       data_stall_probability => 0.5,
       min_response_latency => 4 * clk_period,
       max_response_latency => 10 * clk_period
     );
   begin
     clk <= not clk after clk_period / 2;
     test_runner_watchdog(runner, 100 us);

     main : process
       variable read_data : std_ulogic_vector(data_width - 1 downto 0);
     begin
       test_runner_setup(runner, runner_cfg);

       if run("test_write_then_read") then
         -- 写一个值到地址 0x1000
         write_bus(net, bus_handle, 16#1000#, x"DEAD_BEEF");
         -- 读回并核对（从设备从同一 memory 取数）
         read_bus(net, bus_handle, 16#1000#, read_data);
         check_equal(read_data, x"DEAD_BEEF");
       end if;

       test_runner_cleanup(runner);
     end process;

     -- 主设备：VC 包装，单拍事务
     axi_master_inst : entity work.axi_master
       generic map (bus_handle => bus_handle)
       port map (
         clk => clk,
         axi_read_m2s => axi_read_m2s, axi_read_s2m => axi_read_s2m,
         axi_write_m2s => axi_write_m2s, axi_write_s2m => axi_write_s2m
       );

     -- 从设备：落到 memory，带随机背压
     axi_slave_inst : entity work.axi_slave
       generic map (
         axi_read_slave => slave_cfg, axi_write_slave => slave_cfg,
         data_width => data_width, id_width => 0
       )
       port map (
         clk => clk,
         axi_read_m2s => axi_read_m2s, axi_read_s2m => axi_read_s2m,
         axi_write_m2s => axi_write_m2s, axi_write_s2m => axi_write_s2m
       );
   end architecture;
   ```

   > 说明：`write_bus` / `read_bus` / `check_equal` / `new_bus` / `net` 来自 VUnit（`bus_master_pkg` / `check_pkg` / `communication`），`new_axi_slave` 来自 `axi_slave_pkg`。它们的精确签名以你本地 VUnit 版本为准。

2. 在 `module_bfm.py` 的 `setup_vunit` 里登记这个测试台（参照现有 `setup_axi_read_bfm_tests` 的写法），用 `self.add_vunit_config` 至少加一组配置。
3. 用 `tools/simulate.py` 跑这个测试，并用 `--seed` 多跑几个种子。

**需要观察的现象**：

- 由于从设备 `data_stall_probability => 0.5`，每笔事务的完成周期数是随机的、不固定的。
- 但 `check_equal(read_data, x"DEAD_BEEF")` 应始终通过——因为写进去的值落进了同一个 memory 模型，读回又从它取。
- 若把 DUT（这里主从直连，没有 DUT）换成真实模块（如 `axi_read_pipeline`），协议检查器会持续监督它的握手合规性。

**预期结果**：在多个随机种子下，写读回环均通过，证明即便存在随机背压，事务结果仍正确、协议仍合规。

> ⚠️ 上述测试台是**示例代码**，未在本地编译运行；具体端口、VUnit 过程签名请以仓库现有测试台（如 `tb_axi_read_bfm.vhd`）和你的 VUnit 版本为准。

## 6. 本讲小结

- **BFM 是仿真专用的总线演员**：扮演主/从设备，替代真实 CPU 或外设，不可综合，因此能自由使用 `wait`、随机数与动态内存；全部位于 `modules/bfm/sim/`。
- **两条驱动路线**：VC 包装路线（`axi_master` / `axi_lite_master` / `axi_slave`）用 `bus_handle` + `net` 调用，适合单拍寄存器访问；队列驱动路线（`axi_read_master` / `axi_stream_master`）用 `queue` + `integer_array`，适合大规模随机突发与自检。
- **包装层 = record 桥接 + 协议检查器**：BFM 把 VUnit VC 的标量信号桥接到项目 record，并为每条 AXI 通道挂一个 `common.axi_stream_protocol_checker`，主从双方的检查器互相监督。
- **随机背压是覆盖率的关键**：`stall_configuration_t` + OSVVM `random_stall` 在握手前按概率插入随机 stall，`handshake_master` / `handshake_slave` 把它接到 valid/ready，逼出 ready/valid 时序角落里的 bug；种子由 VUnit 统一管理、可复现。
- **memory 模型 + 支撑包构成闭环**：从设备把事务落到 VUnit `memory_t`，使「同一份数据既写进 memory 又作为期望值」的自检套路成为可能；`queue_bfm_pkg` / `memory_bfm_pkg` 解决批量建句柄的仿真器可移植性，`axi_slave_init` 空常量支持条件例化。
- **测试通过 `module_bfm.py` 的 generic 矩阵登记**：在 `setup_vunit` 里按测试名定制 stall 概率、枚举位宽等 generic，把随机化回归纳入 CI（承接 u1-l4 的 Python 入口模式）。

## 7. 下一步学习建议

- **u8-l2（VUnit 测试台模式与 generic 矩阵）**：本讲已经见到 `module_bfm.py` 如何用 `add_vunit_config` 枚举 generic，下一讲会系统讲解 `setup_vunit` 的嵌套循环写法、`tb_*.vhd` 的 `run("...")` 自检结构，以及 `test/conftest.py` 的 Python 路径约定。
- **u8-l3（资源占用回归）**：BFM 验证的是「功能正确」，下一阶段还会用 netlist 构建把「资源/时序」纳入 CI，形成功能 + 资源的双重回归。
- **继续阅读的源码**：本讲只精读了读路径与握手/流式主设备；建议接着读 `axi_write_master.vhd` / `axi_write_slave.vhd`（写路径与 AW/W/B 的顺序问题）、`axi_stream_slave.vhd`（带 ID/user 比对的从设备），以及 `modules/common/src/axi_stream_protocol_checker.vhd`（所有协议检查的真正实现）。
