# AXI-Stream 模块与包定义

## 1. 本讲目标

本讲是 AXI 总线系列（第 5 单元）的第一讲，承接 u2-l1 建立的 ready/valid 握手约定，把「流式接口」从项目自定义约定推进到工业标准 **AMBA AXI4-Stream**。

学完后你应该能够：

1. 说清 AXI-Stream 各信号（`TVALID`/`TREADY`/`TDATA`/`TLAST`/`TKEEP`/`TSTRB`/`TID`/`TDEST`/`TUSER`）的语义，以及一个 beat 与一个 packet 的区别。
2. 读懂 `axi_stream_pkg` 中的记录类型 `axi_stream_m2s_t` / `axi_stream_s2m_t`、最大宽度常量、初值常量，以及 `to_slv` / `to_axi_stream_m2s` 三个打包函数，并解释「为什么 `valid` 不进打包向量」。
3. 读懂 `axi_stream_fifo` 如何把记录接口在边界处压平为一个最优宽度的 `std_ulogic_vector`，再复用第 4 单元的 `fifo_wrapper`，从而零重复地获得同步/异步缓冲能力。
4. 用 `bfm` 模块提供的 `axi_stream_master` / `axi_stream_slave` 总线功能模型（BFM）写出一个验证 `axi_stream_fifo` 数据与 `last` 透传的 testbench。

## 2. 前置知识

本讲默认你已掌握以下内容（在前序讲义中建立）：

- **ready/valid 握手**（u2-l1）：`valid` 不得组合依赖 `ready`，`ready` 可组合依赖 `valid`；只有同一拍 `valid` 与 `ready` 同为 1 才完成一次 beat。
- **`handshake_pipeline` 的 skid buffer 思想**（u2-l1）：数据通路与控制通路可分别流水。
- **`types_pkg` 的数组类型**（u2-l2）：如 `slv_vec_t` 这种「数组的数组」，本讲的 `axi_stream_m2s_vec_t` 沿用同一套路。
- **`attribute_pkg` 的 `ram_style_t`**（u2-l2）：`axi_stream_fifo` 的 `ram_type` generic 正是这个强类型枚举。
- **同步/异步 FIFO 与 `fifo_wrapper`**（u4-l1、u4-l2）：`fifo_wrapper` 用 `depth=0` / `use_asynchronous_fifo` 在直通/同步/异步三种模式间切换；本讲的 FIFO 完全复用它。
- **VUnit testbench 与 `module_*.py`**（u1-l4、u8-l2 预告）：`setup_vunit` 用 `add_vunit_config` / `add_config` 枚举 generic 矩阵。

几个本讲要用到的术语：

- **AXI-Stream**：ARM AMBA 体系中面向「数据流」的总线协议，常用于把数据从源头（如 ADC、DMA 读通道）搬到终点（如 DDR、加速器）。规范文档是 *ARM IHI 0051A*，`axi_stream_pkg` 的文件头明确引用了它。
- **beat**：一次握手传递的数据单元（一个时钟周期里 `valid&&ready` 的一拍）。
- **packet**：由若干 beat 组成的数据包，用 `TLAST` 标记最后一拍。
- **BFM（总线功能模型）**：仿真专用的 master/slave 模型，能按协议驱动/检查事务并注入随机背压，详细用法见 u8-l1。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [modules/axi_stream/src/axi_stream_pkg.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_stream/src/axi_stream_pkg.vhd) | 定义 AXI-Stream 的记录类型、最大宽度常量、初值常量与打包函数。是本模块所有实体 `use` 的公共基础。 |
| [modules/axi_stream/src/axi_stream_fifo.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_stream/src/axi_stream_fifo.vhd) | 在 AXI-Stream 记录接口上提供缓冲，内部复用 `fifo_wrapper`。 |
| [modules/fifo/src/fifo_wrapper.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo_wrapper.vhd) | （u4-l1/u4-l2 讲过）按 generic 在直通/同步/异步 FIFO 间切换的统一封装，本讲的 FIFO 直接调用它。 |
| [modules/axi_stream/test/tb_axi_stream_fifo.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_stream/test/tb_axi_stream_fifo.vhd) | 项目自带的 FIFO testbench，手驱动记录接口，是本讲实践的重要参照。 |
| [modules/axi_stream/module_axi_stream.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_stream/module_axi_stream.py) | 该模块的 VUnit 配置，展示如何为 `tb_axi_stream_pkg` / `tb_axi_stream_fifo` 生成 generic 矩阵。 |
| [modules/bfm/sim/axi_stream_master.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/sim/axi_stream_master.vhd) / [axi_stream_slave.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/sim/axi_stream_slave.vhd) | master/slave BFM，本讲实践用它驱动与检查 AXI-Stream 事务。 |
| [modules/bfm/test/tb_axi_stream_bfm.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/test/tb_axi_stream_bfm.vhd) | 项目里把两个 BFM 对接的范例 testbench，本讲实践参照它的写法。 |

> 提示：本模块的库名就是 `axi_stream`（裸名，无 `lib` 后缀，见 u1-l2 的约定）。引用时写 `library axi_stream; use axi_stream.axi_stream_pkg.all;`。

## 4. 核心概念与源码讲解

本讲按三个最小模块推进：先讲协议信号语义，再讲 `axi_stream_pkg` 如何用记录类型封装这些信号，最后讲 `axi_stream_fifo` 如何在记录接口上复用通用 FIFO。

### 4.1 AXI-Stream 协议信号语义

#### 4.1.1 概念说明

AXI4-Stream 是 AMBA 家族里最轻量的流式协议：它只解决「把一串数据从 A 搬到 B」这一件事，不像完整 AXI 那样有地址通道。正因为它轻，FIFO、DMA、数据通路里几乎到处都是它的身影。

一个 AXI-Stream 接口由主到从（master→slave，简称 m2s）和从到主（slave→master，简称 s2m）两组信号组成：

| 信号 | 方向 | 含义 |
|------|------|------|
| `TVALID` | m2s | 主设备本拍数据有效 |
| `TDATA` | m2s | 数据负载 |
| `TLAST` | m2s | 标记一个 packet 的最后一拍 |
| `TSTRB` | m2s | 字节有效位：该字节是「数据」还是「空洞」 |
| `TKEEP` | m2s | 字节保留位：该字节是否要写进最终目的（常用于非对齐尾拍） |
| `TUSER` | m2s | 旁路用户数据（如错误标志、自定义元数据） |
| `TID` | m2s | 流标识，用于多流交织 |
| `TDEST` | m2s | 目的路由标识 |
| `TREADY` | s2m | 从设备本拍可以接收 |

其中只有 `TVALID`/`TREADY`/`TDATA` 是必选，其余都是可选。hdl-modules 的设计取向是「够用就好、不开的功能零资源」（u1-l1），所以 `axi_stream_pkg` 只实现了实际项目最常用的子集，可选信号按需再加。

#### 4.1.2 核心流程

握手规则与 u2-l1 完全一致（AXI-Stream 本质就是带 `TLAST`/`TKEEP` 的 ready/valid）：

1. 主设备拉高 `TVALID` 并给出 `TDATA`/`TLAST`，**在握手完成前必须保持稳定**。
2. 从设备按自己的节奏拉高 `TREADY`（可组合依赖 `TVALID`，但反之不行，避免组合环）。
3. 某拍 `TVALID && TREADY` 同时为 1 → 传递一个 **beat**。
4. 若干 beat 组成一个 **packet**，由 `TLAST=1` 的那一拍收尾。

一个 packet 的例子（4 拍）：

```
cycle :  1    2    3    4
TVALID:  1    1    1    1
TREADY:  1    0    1    1   ← 第 2 拍被背压，数据保持
TLAST :  0    0    0    1   ← 第 4 拍是包尾
TDATA : d0   d1   d1   d2   ← 第 2 拍没握成，第 3 拍重发 d1
```

`TSTRB`/`TKEEP` 处理「非对齐包」：当一个包的字节数不是 `TDATA` 字节宽度的整数倍时，最后一拍只有部分字节有效，用它们指明哪些字节算数。本讲的 `axi_stream_fifo` 不搬运 `TSTRB`/`TKEEP`（见 4.2），因此实践里我们用对齐包。

#### 4.1.3 源码精读

`axi_stream_pkg` 的文件头明确标注它依据的是 ARM 官方规范，这是理解后续类型取舍的依据：

[modules/axi_stream/src/axi_stream_pkg.vhd:9-11](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_stream/src/axi_stream_pkg.vhd#L9-L11) — 声明本包基于 *ARM IHI 0051A (ID030610) AMBA 4 AXI4-Stream Protocol Specification*，并给出官方文档链接。

包里先给每个信号定义了一个「最大宽度」常量，并附注释说明这只是上限、实际只用真正需要的位：

[modules/axi_stream/src/axi_stream_pkg.vhd:21-49](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_stream/src/axi_stream_pkg.vhd#L21-L49) — 定义 `axi_stream_id_sz=8`、`axi_stream_dest_sz=4`、`axi_stream_data_sz=128`、`axi_stream_strb_sz=data_sz/8`、`axi_stream_keep_sz=data_sz/8`、`axi_stream_user_sz=data_sz/8`。每条都注释「The width value below is a max value, implementation should only take into regard the bits that are actually used」。

这段注释是全包的设计基调：**记录里的字段按最大宽度声明，实际实例只用低位的若干比特**，这样不同位宽的实例能共享同一记录类型，又不在综合时浪费资源（打包函数只取真正用到的位，见 4.2）。

#### 4.1.4 代码实践（阅读型）

1. **目标**：在真实 testbench 里辨认 AXI-Stream 信号。
2. **步骤**：打开 [modules/axi_stream/test/tb_axi_stream_fifo.vhd:66-82](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_stream/test/tb_axi_stream_fifo.vhd#L66-L82) 的 `test_single_transaction` 测试用例。
3. **观察**：`input_m2s.valid <= '1'` 对应 `TVALID`；`input_m2s.data(data'range) <= data` 对应 `TDATA`；`input_s2m.ready`（由 FIFO 驱动）对应 `TREADY`；`wait until rising_edge(clk_input) and input_s2m.ready = '1'` 正是在等一次 beat。
4. **预期结果**：你能把 testbench 里的每一行映射到 4.1.2 流程里的某一步。注意此例没设 `last`，所以是单 beat 的退化包。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `TVALID` 不能组合依赖 `TREADY`，而反过来可以？
**答案**：若 `TVALID` 组合依赖 `TREADY`、`TREADY` 又组合依赖 `TVALID`，会形成组合环，导致死锁或时序崩溃。让 `TVALID` 先稳定、`TREADY` 可看 `TVALID` 决定是否接收，是 AXI 规定的合法且无环的方向（u2-l1 的同一规则）。

**练习 2**：一个 32 位 `TDATA` 的接口传一个 10 字节的包，需要几拍？最后一拍 `TKEEP`/`TSTRB` 应是怎样的？
**答案**：32 位 = 4 字节/拍，10 字节需要 3 拍（4+4+2）。最后一拍只有低 2 字节有效，`TKEEP`/`TSTRB` 低 2 位为 1、高 2 位为 0。本讲的 FIFO 不搬运 `TKEEP`/`TSTRB`，所以实践里我们用 12 字节（3 拍全满）的对齐包。

### 4.2 axi_stream_pkg：用记录类型封装 AXI-Stream 接口

#### 4.2.1 概念说明

裸用 `TVALID`/`TDATA`/`TLAST`/`TREADY` 这些标量信号写端口，接口稍宽就会列出一长串信号，易错且难读。VHDL 的 **record（记录）** 类型能把一组相关信号捆成一个整体，端口只需写一行。

`axi_stream_pkg` 做的事就是：

1. 把 m2s 方向的信号捆成 `axi_stream_m2s_t`，s2m 方向捆成 `axi_stream_s2m_t`；
2. 提供初值常量，让「未驱动」有安全默认；
3. 提供打包函数，把记录里**真正用到的位**压平成一个 `std_ulogic_vector`，供底层按位宽优化的 FIFO 使用。

#### 4.2.2 核心流程

记录的定义遵循「最大宽度容器 + 实际取用低位」的模式：

- m2s 记录包含 `valid`、`data`（128 位）、`last`、`user`（16 位）；**显式排除** `tkeep`/`tstrb`/`tid`/`tdest`，注释说这些可选信号需要时再加。
- s2m 记录只含 `ready`。
- 打包宽度公式：

\[
\text{bus\_width} = \text{data\_width} + \text{user\_width} + 1
\]

其中 `+1` 是给 `last` 的，而 `valid` **被排除**——因为 `valid` 是握手控制信号，不进入 FIFO 存储（见 4.3）。打包顺序为低位到高位：`data` → `last` → `user`。

#### 4.2.3 源码精读

m2s 记录与数组类型：

[modules/axi_stream/src/axi_stream_pkg.vhd:51-60](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_stream/src/axi_stream_pkg.vhd#L51-L60) — 定义 `axi_stream_m2s_t`（含 `valid`/`data`/`last`/`user`，注释标明排除了 `tkeep`/`tstrb`/`tid`/`tdest`）及其数组类型 `axi_stream_m2s_vec_t`。`data` 字段用满 `axi_stream_data_sz-1 downto 0`（128 位），是「最大宽度容器」。

s2m 记录与初值常量：

[modules/axi_stream/src/axi_stream_pkg.vhd:62-75](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_stream/src/axi_stream_pkg.vhd#L62-L75) — `axi_stream_m2s_init` 把 `valid` 默认为 `'0'`（未驱动的主机保持静默，不会误发数据），`last`/`data`/`user` 为 `'-'`（don't-care）；`axi_stream_s2m_init` 把 `ready` 默认为 `'1'`（未驱动的从机默认可收，不会把上游卡死）。这两个默认值是防「悬空端口导致仿真/综合意外」的安全网。

打包宽度函数：

[modules/axi_stream/src/axi_stream_pkg.vhd:99-107](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_stream/src/axi_stream_pkg.vhd#L99-L107) — `axi_stream_m2s_sz` 返回 `data_width + user_width + 1`，注释明确「Excluded member: valid」「The 1 is for 'last'」。

记录 → 扁平向量的打包：

[modules/axi_stream/src/axi_stream_pkg.vhd:109-132](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_stream/src/axi_stream_pkg.vhd#L109-L132) — `to_slv` 依次把 `data.data` 的低 `data_width` 位、`data.last`、`data.user` 的低 `user_width` 位拼进 `result`，并用 `assert hi = result'high` 在仿真期断言「正好填满、无错位」。注意它只取 `data_width`/`user_width` 位，而不是记录里全宽的 128/16 位——这就是「最优打包、不浪费」。

扁平向量 → 记录的解包：

[modules/axi_stream/src/axi_stream_pkg.vhd:134-161](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_stream/src/axi_stream_pkg.vhd#L134-L161) — `to_axi_stream_m2s` 用 `data'low` 作偏移，支持输入向量不必从 0 开始切片（这一点在 u4-l1/u4-l3 的硬核 FIFO 拼位里也常见）；末尾 `result.valid := valid` 单独把 `valid` 填进去——因为 `valid` 不在向量里，需由调用方另行传入。

#### 4.2.4 代码实践（阅读 + 手算型）

1. **目标**：验证 `to_slv` 与 `to_axi_stream_m2s` 互为逆运算，并手算打包宽度。
2. **步骤**：
   - 读 [modules/axi_stream/test/tb_axi_stream_pkg.vhd:38-64](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_stream/test/tb_axi_stream_pkg.vhd#L38-L64) 的 `test_slv_conversion`：它把随机向量经 `to_axi_stream_m2s` 再 `to_slv` 回去，断言结果相等，且故意用非零偏移 `lo` 切片以验证偏移处理。
   - 手算：`data_width=24, user_width=8` 时 `bus_width` 是多少？`data_width=32, user_width=0` 时又是多少？
3. **预期结果**：24+8+1=33；32+0+1=33。两者巧合相等，说明「加 user 减 data」可抵消。`module_axi_stream.py` 里 `tb_axi_stream_pkg` 的 generic 矩阵见 [module_axi_stream.py:26-30](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_stream/module_axi_stream.py#L26-L30)（`data_width ∈ {24,32,64}` × `user_width ∈ {8,16}`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `axi_stream_m2s_init` 把 `valid` 设为 `'0'`，而 `axi_stream_s2m_init` 把 `ready` 设为 `'1'`？
**答案**：`valid='0'` 让未连接/未驱动的主机不擅自发数据，避免误事务；`ready='1'` 让未连接/未驱动的从机默认放行，避免上游被无限背压死锁。两者都是「悬空时选安全侧」。

**练习 2**：`to_slv` 末尾的 `assert hi = result'high` 在检查什么？什么情况会触发它失败？
**答案**：检查「按 data_width/last/user 顺序拼完后，恰好填满整个向量，没有错位或漏位」。若有人改了字段顺序却忘了调整 `lo/hi` 推进逻辑，导致总长与 `axi_stream_m2s_sz` 不一致，该断言会在仿真期报错。

### 4.3 axi_stream_fifo：在 AXI-Stream 上复用 fifo

#### 4.3.1 概念说明

有了记录类型，写一个「AXI-Stream 缓冲 FIFO」其实不必重新实现一遍 FIFO 逻辑——第 4 单元已经做过了。`axi_stream_fifo` 的定位是一个**薄适配层**：

- 对外暴露 AXI-Stream 记录接口（`input_m2s`/`input_s2m`/`output_m2s`/`output_s2m`）；
- 对内把记录压平成最优宽度的 `std_ulogic_vector`，交给通用的 `fifo_wrapper`（u4-l1/u4-l2）；
- 通过 `asynchronous` generic 一键切换同步/异步模式，异步时复用 `asynchronous_fifo` 的跨时钟域与约束方案。

这体现了 hdl-modules 一贯的「组合优于重写」与「面积优先」：记录里没用的字段不进打包向量，底层 RAM 宽度刚好等于 `data_width + user_width + 1`，一位都不浪费。

#### 4.3.2 核心流程

`axi_stream_fifo` 的数据通路可以画成：

```
input_m2s (record) ──to_slv──> write_data (slv, bus_width 位) ─┐
                                                                │
                                                  fifo_wrapper  │  (同步 fifo / 异步 fifo / 直通)
                                                                │
output_m2s (record) <──to_axi_stream_m2s── read_data (slv) <────┘
                                       (valid <= read_valid)
```

关键点：

1. `bus_width = axi_stream_m2s_sz(data_width, user_width)`，即 `data_width + user_width + 1`。
2. **写侧**：`to_slv` 把 `input_m2s` 压平成 `write_data`；`input_m2s.valid` 与 `input_s2m.ready` 直接连到 `fifo_wrapper` 的 `write_valid`/`write_ready`——`valid` 走握手通路，**不进 RAM**。
3. **读侧**：`fifo_wrapper` 的 `read_data` 经 `to_axi_stream_m2s` 解包成 `output_m2s`，其 `valid` 字段由 `fifo_wrapper.read_valid` 填入。
4. `asynchronous=True` 时 `clk_output` 作读时钟，`fifo_wrapper` 内部例化 `asynchronous_fifo`（需配 u4-l2 的约束）；`asynchronous=False` 时 `clk` 同时用于读写，例化同步 `fifo`。

#### 4.3.3 源码精读

实体声明：

[modules/axi_stream/src/axi_stream_fifo.vhd:29-48](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_stream/src/axi_stream_fifo.vhd#L29-L48) — generic 有 `data_width`（限 1..128）、`user_width`（限 0..16）、`asynchronous`、`depth`、`ram_type`（`ram_style_t`，来自 `attribute_pkg`，见 u2-l2）；端口用记录类型表达输入/输出两路 AXI-Stream，`clk_output` 仅异步时需要赋值。文件头 [axi_stream_fifo.vhd:9-15](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_stream/src/axi_stream_fifo.vhd#L9-L15) 点明：可通过 `asynchronous` generic 作时钟域跨越，且宽度 generic 会最优打包、不浪费资源；异步模式须使用 `asynchronous_fifo` 的约束。

打包宽度与中间信号：

[modules/axi_stream/src/axi_stream_fifo.vhd:52-57](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_stream/src/axi_stream_fifo.vhd#L52-L57) — `bus_width` 由 `axi_stream_m2s_sz` 算出；`write_data`/`read_data` 是该宽度的扁平向量，`read_valid` 是读侧有效信号。

边界处的记录↔向量转换：

[modules/axi_stream/src/axi_stream_fifo.vhd:61-65](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_stream/src/axi_stream_fifo.vhd#L61-L65) — 写侧 `write_data <= to_slv(input_m2s, ...)`；读侧 `output_m2s <= to_axi_stream_m2s(read_data, ..., valid=>read_valid)`。注意 `valid` 单独由 `read_valid` 注入，印证 4.2 所说「`valid` 不进打包向量」。

复用 fifo_wrapper：

[modules/axi_stream/src/axi_stream_fifo.vhd:69-88](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_stream/src/axi_stream_fifo.vhd#L69-L88) — 把 `use_asynchronous_fifo => asynchronous`、`width => bus_width`、`depth`、`ram_type` 传给 `fifo.fifo_wrapper`，并把记录握手映射到 `write_ready/write_valid/read_ready/read_valid`。`fifo_wrapper` 本身按 [fifo_wrapper.vhd:79-174](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo_wrapper.vhd#L79-L174) 三分支（`depth=0` 直通 / `use_asynchronous_fifo` 异步 / 否则同步）选择底层 FIFO——`axi_stream_fifo` 一行底层逻辑都没写，全靠组合复用。

项目的自测配置：

[modules/axi_stream/module_axi_stream.py:32-34](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_stream/module_axi_stream.py#L32-L34) — 为 `tb_axi_stream_fifo` 生成 `synchronous` 与 `asynchronous` 两组配置（`depth=16`），用同一 testbench 覆盖两种时钟模式。这也示范了本讲实践中该如何登记一个新的 BFM testbench。

#### 4.3.4 代码实践（完整仿真实践）

**实践目标**：实例化 `axi_stream_fifo`，用 `axi_stream_master` BFM 写入一包带 `last` 的数据，用 `axi_stream_slave` BFM 读出并自动比对，验证数据与 `last` 标记一致。

**关键认识**：BFM 的端口是**标量**（`valid`/`ready`/`last`/`data`/`strobe`），而 FIFO 的端口是**记录**。因此 testbench 需要用并发赋值在两者间桥接；又因为记录里没有 `strobe` 字段（FIFO 不搬运 `strobe`），需把从机 BFM 的 `enable_strobe` 关掉。下面是示例代码（**非项目原有文件**，是本讲为练习编写）：

```vhdl
-- 示例代码：用 BFM 验证 axi_stream_fifo 的数据与 last 透传
-- 假设放置于 modules/axi_stream/test/，故 FIFO 与 pkg 用 work 库；
-- BFM 来自 bfm 模块，需在仿真工程中编译 bfm 库（见 u8-l1）。

library ieee;
use ieee.std_logic_1164.all;

library vunit_lib;
use vunit_lib.check_pkg.all;
use vunit_lib.integer_array_pkg.all;
use vunit_lib.queue_pkg.all;
use vunit_lib.run_pkg.all;

library bfm;

use work.axi_stream_pkg.all;


entity tb_axi_stream_fifo_bfm is
  generic (
    runner_cfg : string
  );
end entity;

architecture tb of tb_axi_stream_fifo_bfm is

  constant data_width : positive := 32;  -- 必须是 8 的倍数（BFM 按字节处理）
  constant user_width : natural := 0;    -- 本练习不使用 user 字段

  constant clk_period : time := 10 ns;
  signal clk : std_ulogic := '0';

  -- FIFO 侧：记录类型
  signal input_m2s : axi_stream_m2s_t := axi_stream_m2s_init;
  signal input_s2m : axi_stream_s2m_t := axi_stream_s2m_init;
  signal output_m2s : axi_stream_m2s_t := axi_stream_m2s_init;
  signal output_s2m : axi_stream_s2m_t := axi_stream_s2m_init;

  -- BFM 侧：标量信号，用并发赋值与记录桥接
  signal m_valid, m_last : std_ulogic;
  signal m_data : std_ulogic_vector(data_width - 1 downto 0);
  signal s_ready, s_valid, s_last : std_ulogic;
  signal s_data : std_ulogic_vector(data_width - 1 downto 0);

  constant data_queue, reference_data_queue : queue_t := new_queue;
  signal num_packets_checked : natural := 0;

begin

  test_runner_watchdog(runner, 2 ms);
  clk <= not clk after clk_period / 2;

  -- 主机 BFM（标量） -> FIFO 输入记录（只填用到的低位）
  input_m2s.valid <= m_valid;
  input_m2s.last <= m_last;
  input_m2s.data(data_width - 1 downto 0) <= m_data;

  -- FIFO 输出记录 -> 从机 BFM（标量）；从机 ready 回灌到 FIFO
  s_valid <= output_m2s.valid;
  s_last <= output_m2s.last;
  s_data <= output_m2s.data(data_width - 1 downto 0);
  output_s2m.ready <= s_ready;

  main : process
    variable data_packet, data_packet_copy : integer_array_t := null_integer_array;
  begin
    test_runner_setup(runner, runner_cfg);

    if run("test_packet_with_last") then
      -- data_width=32 -> 每拍 4 字节；发一个 12 字节（3 拍）的对齐包
      data_packet := new_1d(length=>12, bit_width=>8, is_signed=>false);
      for idx in 0 to 11 loop
        set(arr=>data_packet, idx=>idx, value=>idx);  -- 字节内容 0,1,2,...,11
      end loop;
      data_packet_copy := copy(data_packet);

      push_ref(data_queue, data_packet);            -- 主机发送
      push_ref(reference_data_queue, data_packet_copy);  -- 从机期望

      wait until num_packets_checked = 1 and rising_edge(clk);
      check_equal(num_packets_checked, 1);
    end if;

    test_runner_cleanup(runner);
  end process;

  -- 主机 BFM：从 data_queue 取整数数组，按字节驱动 data/last/strobe
  axi_stream_master_inst : entity bfm.axi_stream_master
    generic map (
      data_width => data_width,
      data_queue => data_queue
    )
    port map (
      clk => clk,
      ready => input_s2m.ready,   -- 读 FIFO 给出的 ready
      valid => m_valid,
      last => m_last,
      data => m_data,
      strobe => open              -- 记录无 strobe 字段，FIFO 不搬运它
    );

  -- 被测件：同步 FIFO，深度 16
  axi_stream_fifo_inst : entity work.axi_stream_fifo
    generic map (
      data_width => data_width,
      user_width => user_width,
      asynchronous => false,
      depth => 16
    )
    port map (
      clk => clk,
      input_m2s => input_m2s,
      input_s2m => input_s2m,
      output_m2s => output_m2s,
      output_s2m => output_s2m
    );

  -- 从机 BFM：逐拍比对 data，并在最后一拍检查 last='1'
  axi_stream_slave_inst : entity bfm.axi_stream_slave
    generic map (
      data_width => data_width,
      reference_data_queue => reference_data_queue,
      enable_strobe => false     -- FIFO 不搬运 strobe，关闭 strobe 检查
    )
    port map (
      clk => clk,
      ready => s_ready,
      valid => s_valid,
      last => s_last,
      data => s_data,
      num_packets_checked => num_packets_checked
    );

end architecture;
```

**操作步骤**：

1. 把上述文件放到 `modules/axi_stream/test/tb_axi_stream_fifo_bfm.vhd`。
2. 在 [module_axi_stream.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_stream/module_axi_stream.py) 的 `setup_vunit` 末尾登记它：
   ```python
   tb = vunit_proj.library(self.library_name).test_bench("tb_axi_stream_fifo_bfm")
   self.add_vunit_config(tb, generics={})
   ```
3. 确保仿真工程已编译 `bfm` 库（BFM 的来源与用法见 u8-l1）。
4. 按 u1-l3 的方式运行：`python tools/simulate.py axi_stream.tb_axi_stream_fifo_bfm`。

**需要观察的现象**：

- 主机 BFM 把 12 字节分成 3 拍发出，第 3 拍 `last='1'`。
- FIFO 输入侧 `input_s2m.ready` 在未满时为 1，数据进入 FIFO。
- FIFO 输出侧 `output_m2s.valid` 在有数据时为 1，从机 BFM 逐拍接收。
- 从机 BFM 逐字节比对 `data`，并在第 3 拍检查 `last='1'`；全部通过后 `num_packets_checked` 变为 1。

**预期结果**：仿真通过，`check_equal(num_packets_checked, 1)` 不报错，VUnit 打印 `pass`。若把 `last` 故意接反或改写期望数据，应看到从机 BFM 报 `'last' check` 或 `'data' check` 失败——这正是 BFM 的自检价值。

**待本地验证**：本环境未运行 Vivado/GHDL，上述运行结果为依据源码与项目 BFM 用法推导所得，请在本地仿真确认。

#### 4.3.5 小练习与答案

**练习 1**：把 `asynchronous` 改为 `true` 后，需要额外做哪两件事？
**答案**：① 给 `clk_output` 接一个与 `clk` 不同频率的读时钟；② 按 u4-l2 为异步 FIFO 添加 `scoped_constraints`（`set_bus_skew` + `set_max_delay -datapath_only` 指针路径，数据路径 `false_path`），否则跨域指针不安全。`axi_stream_fifo` 文件头 [axi_stream_fifo.vhd:13-15](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_stream/src/axi_stream_fifo.vhd#L13-L15) 已明确提示这一点。

**练习 2**：如果同时启用 `user_width=16`，FIFO 内部 RAM 的位宽会变成多少？`valid` 会不会占 RAM？
**答案**：`bus_width = 32 + 16 + 1 = 49` 位。`valid` 不会占 RAM——它是握手控制信号，走 `fifo_wrapper` 的 `write_valid`/`read_valid` 通路，被 `to_slv` 显式排除（见 4.2.3）。

**练习 3**：为什么从机 BFM 要设 `enable_strobe => false`？
**答案**：`axi_stream_m2s_t` 记录里没有 `strobe`/`keep` 字段，`axi_stream_fifo` 不搬运它们，输出侧没有 strobe 可供检查。若不关闭，从机 BFM 会因 strobe 默认值与期望不符而误报失败。

## 5. 综合实践

把本讲三个最小模块串起来，做一个「手算宽度 + 异步跨域 + 随机背压」的综合任务：

1. **手算宽度**：给定 `data_width=24, user_width=8, asynchronous=true, depth=64`，先在纸上算出 `bus_width`（答案：33），并说明这 33 位如何排布（低 24 位 data、第 25 位 last、高 8 位 user）。
2. **改造 4.3.4 的 testbench**：把 `asynchronous` 设为 `true`，新增一个 7 ns 周期的 `clk_output`，分别驱动主机/写侧与从机/读侧；参照 [tb_axi_stream_fifo.vhd:49-55](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_stream/test/tb_axi_stream_fifo.vhd#L49-L55) 的双时钟写法。
3. **注入随机背压**：仿照 [tb_axi_stream_bfm.vhd:68-78](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/bfm/test/tb_axi_stream_bfm.vhd#L68-L78) 给 master/slave 各设一个非零 `stall_config`，发 10 个随机长度的对齐包。
4. **验证**：确认 `num_packets_checked = 10`，所有包的数据与 `last` 都正确跨时钟域透传。
5. **对照资源**：用 `tools/synthesize.py`（见 u9-l2）对 `axi_stream_fifo` 以 `data_width=24,user_width=8` 与 `data_width=64,user_width=0` 两组 generic 各综合一次，观察 RAM 位宽/深度是否符合手算的 `bus_width`，体会「最优打包」带来的资源节约。

这个任务同时覆盖了：协议语义（beat/packet/last）、`axi_stream_pkg` 的打包宽度公式与字段排布、`axi_stream_fifo` 对 `fifo_wrapper` 的复用、异步跨域约束、以及 BFM 随机验证方法论。

## 6. 本讲小结

- AXI-Stream 是 AMBA 的轻量流式协议，核心是 `TVALID`/`TREADY`/`TDATA`，加 `TLAST` 标记包尾，加 `TSTRB`/`TKEEP` 处理非对齐；握手规则与 u2-l1 的 ready/valid 完全一致。
- `axi_stream_pkg` 用 `axi_stream_m2s_t`（valid/data/last/user）和 `axi_stream_s2m_t`（ready）两个记录把一束信号捆成一行端口，并提供 `*_vec_t` 数组类型与安全的 `*_init` 初值（`valid='0'`、`ready='1'`）。
- 记录字段按「最大宽度」声明（data 128 位、user 16 位），但打包函数 `to_slv`/`to_axi_stream_m2s` 只取实际用到的 `data_width`/`user_width` 位，打包宽度为 `data_width + user_width + 1`。
- `valid` 被显式排除在打包向量之外——它是握手控制信号，不进 FIFO 存储，由调用方在读侧另行注入。
- `axi_stream_fifo` 是一层薄适配：边界处记录↔向量转换，中间把 `bus_width` 宽的向量交给 `fifo_wrapper`，用 `asynchronous` generic 一键切换同步/异步，零重复实现底层 FIFO。
- 用 master/slave BFM 验证时，需在标量 BFM 端口与记录 FIFO 端口之间用并发赋值桥接，并因记录不含 strobe 而关闭从机的 strobe 检查。

## 7. 下一步学习建议

- **u5-l2（AXI 交叉栏、节流与流水线）**：进入完整 AXI 总线，看 `axi_pkg` 的多通道记录定义与交叉栏仲裁，是 AXI-Stream 的「重量级兄弟」。
- **u5-l3（AXI 跨时钟域与通道 FIFO）**：看 AXI 各通道如何用异步 FIFO 跨域，会再次复用本讲的打包/解包思路与 u4-l2 的 `asynchronous_fifo`。
- **u8-l1（BFM 仿真模型）**：深入 `axi_stream_master`/`axi_stream_slave` 的 stall 随机化、queue/memory 支撑包，把本讲实践里的 BFM 用到底。
- **继续阅读源码**：`modules/axi_stream/src/` 下目前只有 `axi_stream_pkg` 与 `axi_stream_fifo` 两个文件，通读它们即可掌握本模块全部可综合源码；再对照 `modules/axi_stream/test/` 的两个 testbench 巩固理解。
