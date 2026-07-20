# TB 与综合 AXI 类型互转（psi_tb_axi_conv_pkg）

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清为什么 psi_tb 要把「TB AXI 类型 ↔ 综合 AXI 类型」的转换**单独**放在 `psi_tb_axi_conv_pkg` 里，而不是塞进 `psi_tb_axi_pkg`。
- 在 psi_tb 的扁平记录 `axi_ms_r` / `axi_sm_r` 与 psi_common 的嵌套记录 `axi_slv_inp` / `axi_slv_oup` 之间，做**逐字段**的对照与手工映射。
- 看懂 `axi_conv_tb_synth_master` 过程里 master→slave、slave→master 两段赋值，并理解它为何是一个**纯组合、无时钟、无 wait** 的并发接线盒。
- 在一个 testbench 里同时声明 TB 侧记录与综合侧记录，调用 `axi_conv_tb_synth_master` 把两者连起来，再用 `axi_single_write` 驱动一个综合侧的 slave 模型。

## 2. 前置知识

本讲承接 [u5-l1](u5-l1-axi-types-and-init.md)，默认你已经了解：

- **`axi_ms_r` / `axi_sm_r`**：psi_tb 的 AXI BFM 用的两条记录。约定是「按谁驱动分」，而不是「按通道分」——主机驱动的所有信号（含 `rready`、`bready`）进 `axi_ms_r`，从机驱动的所有信号进 `axi_sm_r`。其中向量字段是 **VHDL-2008 未约束（unconstrained）**的，宽度在声明信号时才给定。
- **AXI4-Lite / AXI4 五通道**：读地址（AR）、读数据（R）、写地址（AW）、写数据（W）、写响应（B）。每条通道由一对 valid/ready 握手。
- **方向缩写**：协议里习惯把 `MS`（Master→Slave）与 `SM`（Slave→Master）写成后缀，本讲两边类型都用这套缩写，含义一致。
- **综合 vs testbench**（见 [u1-l1](u1-l1-project-overview.md)）：综合代码进芯片，testbench 只跑仿真。psi_common 里放综合用的 AXI 类型，psi_tb 里放仿真用的 BFM 类型。

一个核心直觉先记在心里：**两套类型描述的是同一组 AXI 信号，只是「打包方式」不同**。psi_tb 用「扁平字段、宽度可变」，psi_common 用「嵌套子记录、宽度写死」。本讲就是写一个「拆包—重装」的转换器。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [hdl/psi_tb_axi_conv_pkg.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_conv_pkg.vhd) | 本讲主角。唯一过程 `axi_conv_tb_synth_master`，把 psi_tb 记录与 psi_common 记录逐字段互连。 |
| [hdl/psi_tb_axi_pkg.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd) | 提供 TB 侧记录 `axi_ms_r` / `axi_sm_r`（L42–L100）与 `axi_single_write`（L519）等 BFM 过程。注意：它**不**依赖 psi_common 的 AXI 包。 |
| `psi_common/hdl/psi_common_axi_pkg.vhd`（同级目录的 psi_common 仓库） | 提供综合侧嵌套记录 `rec_axi_ms` / `rec_axi_sm` 及其别名 `axi_slv_inp` / `axi_slv_oup`。字段宽度由包常量写死。链接见各小节，commit 以本地 checkout 为准。 |
| [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl) | PsiSim 编译清单。可验证：AXI 三个包**都没有**进 CI 编译列表，这是「可选依赖」设计的直接证据。 |

> 说明：`psi_common_axi_pkg.vhd` 属于**外部依赖 psi_common**（与 psi_tb 同级目录），不在本仓库 permalink 命中的 HEAD 内。下文给出的行号已对照本地 checkout，链接指向 psi_common 仓库 `master` 分支，具体 commit 以本地 checkout 为准。

## 4. 核心概念与源码讲解

### 4.1 为什么把转换独立成一个 package（设计动机）

#### 4.1.1 概念说明

`psi_tb_axi_pkg`（BFM）和 `psi_common_axi_pkg`（综合接口）服务的是**两类不同的人**：

- 大多数 psi_tb 用户只想用 BFM 仿真一个 AXI 接口，他们**并不**使用 psi_common 的综合 AXI 记录。
- 少数用户要在一个 testbench 里，用 psi_tb 的 BFM 去驱动一个**真正的综合 DUT**（DUT 的端口是 psi_common 的 `axi_slv_inp` / `axi_slv_oup`）。只有这部分人才需要「两边记录互转」。

如果把转换代码写进 `psi_tb_axi_pkg`，那么 `psi_tb_axi_pkg` 就不得不 `use work.psi_common_axi_pkg.all`，于是**所有**用 BFM 的人都被迫也编译 psi_common 的 AXI 包——一个他们根本用不到的强依赖。把转换单独拆成 `psi_tb_axi_conv_pkg`，依赖就被**隔离**到真正需要转换的那部分人身上。

这是一个典型的「依赖反转 / 最小耦合」取舍：**把可选的、跨库的胶水代码单独成包，保持核心 BFM 包的独立性。**

#### 4.1.2 核心流程

依赖关系（箭头表示 `use`）：

```
psi_common_math_pkg ─┐
psi_tb_compare_pkg ──┼──> psi_tb_axi_pkg (BFM, 不依赖综合 AXI 包)
psi_tb_txt_util ─────┘
                                        ┌──> psi_tb_axi_pkg
psi_tb_axi_conv_pkg ────────────────────┤
                                        └──> psi_common_axi_pkg   <-- 仅此处引入
```

要点：

1. `psi_tb_axi_pkg` 本身**只**依赖 `psi_common_math_pkg`（用 `log2`）、`psi_tb_compare_pkg`、`psi_tb_txt_util`，**不**碰 `psi_common_axi_pkg`。
2. `psi_tb_axi_conv_pkg` 同时 `use` 两个 AXI 包，是唯一一处「跨库 AXI」的接合点。
3. 因此「不用转换的人」连 `psi_common_axi_pkg` 都不必编译。

#### 4.1.3 源码精读

转换包顶部的设计意图注释（原话）：

- 包头注释：[hdl/psi_tb_axi_conv_pkg.vhd:L7-L11](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_conv_pkg.vhd#L7-L11)
  > 说明它在「综合友好的 psi_common AXI 包」与「testbench 友好的 psi_tb AXI 包」之间做转换，并明确「放在独立包里，是为了避免所有用 TB AXI 包的人都不得不把综合 AXI 包也 include 进来」。

- 转换包的 `use` 子句：[hdl/psi_tb_axi_conv_pkg.vhd:L20-L22](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_conv_pkg.vhd#L20-L22) 同时引入 `psi_tb_axi_pkg` 与 `psi_common_axi_pkg`。

- 对照：BFM 包的 `use` 子句 [hdl/psi_tb_axi_pkg.vhd:L14-L17](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L14-L17)，里面**没有** `psi_common_axi_pkg`。这正是「独立成包」的实证。

另一个旁证来自 CI 编译清单。`sim/config.tcl` 里：

- `lib` tag（psi_common 部分）：[sim/config.tcl:L21-L25](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L21-L25) 只编译了 `psi_common_array_pkg`、`psi_common_math_pkg`、`psi_common_logic_pkg`，**没有** `psi_common_axi_pkg`。
- `src` tag（psi_tb 部分）：[sim/config.tcl:L28-L33](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L28-L33) 只编译了 `txt_util`、`compare`、`activity`、`i2c`，**没有** `psi_tb_axi_pkg` 与 `psi_tb_axi_conv_pkg`。

也就是说，AXI 相关三个包全部是「按需引入」的，平时 psi_tb 自己的 CI 根本不编译它们——「独立成包、可选依赖」不是一句口号，而是写在编译清单里的事实。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：用证据链确认「BFM 包不依赖综合 AXI 包」。
2. **步骤**：
   - 打开 `hdl/psi_tb_axi_pkg.vhd`，看 L14–L17 的 `library/use`，确认只出现 `psi_common_math_pkg`。
   - 打开 `hdl/psi_tb_axi_conv_pkg.vhd`，看 L20–L22，确认多了 `psi_common_axi_pkg`。
   - 打开 `sim/config.tcl`，确认三个 AXI 包都不在 `add_sources` 列表里。
3. **观察/预期**：你会看到依赖只发生在转换包这一处；CI 不编译任何 AXI 包。
4. 结论：要使用转换包，必须**手动**把三个 AXI 包加进编译清单（见第 5 节综合实践的前置步骤）。

#### 4.1.5 小练习与答案

- **练习 1**：如果有人把 `axi_conv_tb_synth_master` 的过程体直接搬进 `psi_tb_axi_pkg`，会发生什么？
  - **答案**：`psi_tb_axi_pkg` 必须 `use work.psi_common_axi_pkg.all`，于是所有只用 BFM 的人都被迫编译 psi_common AXI 包，违背最小耦合，也会让 CI 必须新增一个原本不必要的依赖。
- **练习 2**：为什么转换包放在 `work` 库就能 `use work.psi_common_axi_pkg.all`？
  - **答案**：按 [u1-l2](u1-l2-repository-structure.md) 的约定，psi_common 的包与 psi_tb 的包编译进**同一个 `work` 库**，所以能用 `work.` 前缀互相引用。

---

### 4.2 两套 AXI 记录类型的字段对照

#### 4.2.1 概念说明

要做转换，先要把两套类型的「形状」对齐。它们有三点根本差异：

| 维度 | psi_tb（`axi_ms_r`/`axi_sm_r`） | psi_common（`axi_slv_inp`/`axi_slv_oup`） |
| --- | --- | --- |
| 组织方式 | **扁平**字段，一个记录摊平所有通道 | **嵌套**记录，按 `ar`/`dr`/`aw`/`dw`/`b` 五个子记录分组 |
| 向量宽度 | **未约束**，声明信号时给定 | 由包常量**写死**（ID=1, DATA=32, ADDR=32, 各 USER=1） |
| 方向命名 | 字段名里直接编码通道（如 `arvalid`、`araddr`） | 同一字母（如 `ar`）在 ms 与 sm 里含义不同：ms 侧带 `valid`+地址，sm 侧只有 `ready` |

方向上两边**一致**：`axi_ms_r`（主机驱动）对应 `rec_axi_ms` / `axi_slv_inp`；`axi_sm_r`（从机驱动）对应 `rec_axi_sm` / `axi_slv_oup`。所以转换是**方向保持**的逐字段搬运，不需要交叉。

#### 4.2.2 核心流程

两条记录各自承载「谁驱动」的一整束信号：

```
axi_ms_r (TB, 主机驱动)          rec_axi_ms / axi_slv_inp (综合, 从机的输入)
├─ AR 通道: arid..arvalid        ├─ ar : id,addr,len,size,burst,lock,cache,prot,qos,region,user,valid
├─ Rready: rready                ├─ dr : ready          (= rready)
├─ AW 通道: awid..awvalid        ├─ aw : id,addr,...,valid
├─ W 通道:  wdata,wstrb,..       ├─ dw : data,strb,last,user,valid
└─ Bready: bready                └─ b  : ready          (= bready)

axi_sm_r (TB, 从机驱动)          rec_axi_sm / axi_slv_oup (综合, 从机的输出)
├─ Arready                       ├─ ar : ready          (= arready)
├─ R 通道: rid,rdata,rresp,..    ├─ dr : id,data,resp,last,user,valid
├─ Awready                       ├─ aw : ready          (= awready)
├─ Wready                        ├─ dw : ready          (= wready)
└─ B 通道: bid,bresp,buser,bvalid└─ b  : id,resp,user,valid
```

关键：综合侧把 R/B 通道在 ms 方向**只保留 ready**（`dr.ready`、`b.ready`），在 sm 方向才是完整数据；TB 侧用扁平名 `rready`/`bready` 表示同样的两个 ready。这是对照时最容易看错的点。

#### 4.2.3 源码精读

- TB 侧 `axi_ms_r`（扁平、未约束向量）：[hdl/psi_tb_axi_pkg.vhd:L42-L79](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L42-L79)。注意 `arid`、`araddr`、`wdata`、`wstrb` 等都是无范围的 `std_logic_vector`，宽度留给使用方。
- TB 侧 `axi_sm_r`：[hdl/psi_tb_axi_pkg.vhd:L81-L100](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L81-L100)。
- 综合侧固定宽度常量：`psi_common_axi_pkg.vhd` 的常量定义（ID=1, DATA=32, ADDR=32, 各 USER=1）：[psi_common_axi_pkg.vhd:L36-L43](https://github.com/paulscherrerinstitute/psi_common/blob/master/hdl/psi_common_axi_pkg.vhd#L36-L43)。
- 综合侧 `rec_axi_ms`（嵌套 5 个子记录）：[psi_common_axi_pkg.vhd:L131-L137](https://github.com/paulscherrerinstitute/psi_common/blob/master/hdl/psi_common_axi_pkg.vhd#L131-L137)；`rec_axi_sm`：[psi_common_axi_pkg.vhd:L139-L145](https://github.com/paulscherrerinstitute/psi_common/blob/master/hdl/psi_common_axi_pkg.vhd#L139-L145)。
- 别名（legacy 但被转换包直接使用）：`axi_slv_inp is rec_axi_ms`、`axi_slv_oup is rec_axi_sm`：[psi_common_axi_pkg.vhd:L207-L208](https://github.com/paulscherrerinstitute/psi_common/blob/master/hdl/psi_common_axi_pkg.vhd#L207-L208)。
- 方向缩写约定（`sm`=Slave→Master，`ms`=Master→Slave）：[psi_common_axi_pkg.vhd:L13-L15](https://github.com/paulscherrerinstitute/psi_common/blob/master/hdl/psi_common_axi_pkg.vhd#L13-L15)。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：手工完成一个字段的映射，验证你对方向的理解。
2. **步骤**：在 `axi_ms_r` 里找到 `rready`（R 通道上主机驱动的 ready）。在 `rec_axi_ms` 里找到它对应到哪个子记录的哪个字段。
3. **预期**：`rready` → `dr.ready`（`dr` 是 ms 方向的「读数据」子记录，主机在此只贡献 ready）。
4. 思考：如果是 `bready`，对应到 `b.ready`；如果是 `arvalid`，对应到 `ar.valid`。

#### 4.2.5 小练习与答案

- **练习 1**：综合侧 `rec_axi_sm.dr` 含哪些字段？对应 TB 侧 `axi_sm_r` 的哪些扁平字段？
  - **答案**：`id,data,resp,last,user,valid` → TB 侧 `rid,rdata,rresp,rlast,ruser,rvalid`。
- **练习 2**：为什么 TB 侧 `wstrb` 要由使用方决定宽度，而综合侧 `dw.strb` 宽度写死？
  - **答案**：TB 侧记录是未约束的，宽度跟随数据位宽；综合侧由 `C_S_AXI_DATA_WIDTH` 写死成 `DATA_WIDTH/8`（32/8=4 字节）。两边要相连，TB 侧必须把 `wstrb` 也声明成 4 位。

---

### 4.3 `axi_conv_tb_synth_master` 过程逐字段映射

#### 4.3.1 概念说明

`axi_conv_tb_synth_master` 是转换包里**唯一**的过程。名字里的 `_master` 表示：它把**主机侧**的两套接口接起来——TB 用 BFM 扮演 AXI 主机，DUT 是一个综合的 AXI 从机。过程的职责只有一件事：在两条 TB 记录与两条综合记录之间做**纯组合的逐字段搬运**，没有时钟、没有握手、没有 `wait`、没有校验。

它的四个参数刚好是两条「方向相反」的信号对：

| 参数 | 模式 | 类型 | 物理含义 |
| --- | --- | --- | --- |
| `tb_ms` | `in` | `axi_ms_r` | BFM 主机**输出**（主机要发的信号） |
| `tb_sm` | `out` | `axi_sm_r` | BFM 主机**输入**（送回给 BFM 的从机响应） |
| `syn_ms` | `out` | `axi_slv_inp` | 综合 DUT 从机的**输入**端口 |
| `syn_sm` | `in` | `axi_slv_oup` | 综合 DUT 从机的**输出**端口 |

数据流向：

```
BFM 主机 --tb_ms--> [转换] --syn_ms--> 综合 DUT(slave)
BFM 主机 <--tb_sm-- [转换] <--syn_sm-- 综合 DUT(slave)
```

#### 4.3.2 核心流程

过程体内只有两段赋值，分别处理两个方向：

```
-- master -> slave：把 tb_ms 拆开，按子记录重新装进 syn_ms
对 AR/AW/W 通道的每个字段：syn_ms.<ch>.<field> <= tb_ms.<flat>
对两个 ready：        syn_ms.dr.ready <= tb_ms.rready
                      syn_ms.b.ready  <= tb_ms.bready

-- slave -> master：把 syn_sm 拆开，摊平回 tb_sm
对 R/B 通道数据：      tb_sm.<flat> <= syn_sm.<ch>.<field>
对三个 ready：         tb_sm.arready <= syn_sm.ar.ready
                      tb_sm.awready <= syn_sm.aw.ready
                      tb_sm.wready  <= syn_sm.dw.ready
```

因为只有信号赋值、没有任何控制流，整个过程在 testbench 里应当作为**并发过程调用（concurrent procedure call）**使用——等价于把 N 条并发连续赋值捆在一起。

#### 4.3.3 源码精读

- 过程声明（4 个信号参数 + 注释「for master side」）：[hdl/psi_tb_axi_conv_pkg.vhd:L29-L33](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_conv_pkg.vhd#L29-L33)。
- 过程体签名：[hdl/psi_tb_axi_conv_pkg.vhd:L42-L45](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_conv_pkg.vhd#L42-L45)。
- master→slave 段（注释 `-- master -> slave`，AR/AW/W + 两个 ready）：[hdl/psi_tb_axi_conv_pkg.vhd:L47-L78](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_conv_pkg.vhd#L47-L78)。要点：`syn_ms.dr.ready <= tb_ms.rready`、`syn_ms.b.ready <= tb_ms.bready`——R/B 通道在主机方向只剩 ready。
- slave→master 段（注释 `-- slave -> master`，R/B 数据 + 三个 ready）：[hdl/psi_tb_axi_conv_pkg.vhd:L79-L92](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_conv_pkg.vhd#L79-L92)。要点：`tb_sm.rid <= syn_sm.dr.id` 等把嵌套子记录摊平回扁平字段。

读这段代码时盯住「**子记录名**（`ar`/`dr`/`aw`/`dw`/`b`）」与「**扁平字段名**」的对应，所有映射都是一一对应的同名搬运，没有任何位运算或改写。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：跟踪一个写地址信号从 BFM 到 DUT 的完整路径。
2. **步骤**：
   - 在 `axi_single_write` 里（[hdl/psi_tb_axi_pkg.vhd:L519-L550](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L519-L550)）找到 `ms.awaddr <= ...`，这是 BFM 给 TB 记录 `tb_ms.awaddr` 赋值。
   - 在转换过程 master→slave 段找到 `syn_ms.aw.addr <= tb_ms.awaddr`。
   - 于是路径是：`axi_single_write → tb_ms.awaddr → syn_ms.aw.addr → DUT 端口`。
3. **预期**：DUT 在其综合侧端口 `axi_slv_i.aw.addr`（即 `syn_ms.aw.addr`）上看到 BFM 写入的地址。

#### 4.3.5 小练习与答案

- **练习 1**：为什么过程里没有 `wait until rising_edge(clk)`？
  - **答案**：它是纯组合的接线，只做信号到信号的连续搬运，不属于某个时钟域。握手与同步都在 BFM 过程（如 `axi_single_write`）和 DUT 内部完成。
- **练习 2**：`tb_sm.bresp` 的值来自哪个综合侧字段？
  - **答案**：来自 `syn_sm.b.resp`（见 L90），即 DUT 从机在 B 通道给出的写响应码。

---

### 4.4 在 testbench 中接线：并发过程调用 + BFM 驱动综合 DUT

#### 4.4.1 概念说明

要用上转换过程，testbench 的架构体里需要四样东西：

1. **两条 TB 记录** `tb_ms : axi_ms_r`（带宽度约束）、`tb_sm : axi_sm_r`（带宽度约束）——宽度必须与综合侧写死的宽度对齐（数据 32、地址 32、ID 1、各 USER 1、wstrb 4）。
2. **两条综合记录** `syn_ms : axi_slv_inp`、`syn_sm : axi_slv_oup`——这两条**不需要**额外约束，宽度已被 psi_common 包常量固定。
3. **一个并发过程调用** `axi_conv_tb_synth_master(tb_ms, tb_sm, syn_ms, syn_sm);`，写在架构体的并发语句区（与 `process`、元件例化同级，**不**放在某个 `process` 内部）。
4. **一个综合侧 slave 模型**（真实 DUT 或行为模型），端口连到 `syn_ms` / `syn_sm`。

为什么必须并发调用、不能放进普通 `process`？因为过程体里没有 `wait`。若放进无敏感表的 `process`，仿真器会在零时刻无限循环（仿真挂死）；作为并发过程调用，它等价于一个对所有读入信号敏感的隐式 `process`，每次输入变化就重新执行所有赋值，效果与 N 条独立连续赋值一致。

#### 4.4.2 核心流程

```
                 ┌─────────────────────┐
   刺激 process  │  axi_master_init    │ 初始化 tb_ms
   (用 BFM)   ──>│  axi_single_write   │ 写 tb_ms，读 tb_sm
                 └─────────┬───────────┘
                           │ tb_ms/tb_sm
                 ┌─────────▼───────────┐
                 │ axi_conv_tb_synth_  │ 并发过程调用（无时钟）
                 │ master (转换接线盒) │
                 └─────────┬───────────┘
                           │ syn_ms/syn_sm
                 ┌─────────▼───────────┐
                 │  综合 DUT / slave   │ 真实可综合 IP 或行为模型
                 │  (axi_slv_inp/oup)  │
                 └─────────────────────┘
```

#### 4.4.3 源码精读

- 过程签名回顾：[hdl/psi_tb_axi_conv_pkg.vhd:L29-L33](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_conv_pkg.vhd#L29-L33) —— 四个参数顺序就是调用顺序 `(tb_ms, tb_sm, syn_ms, syn_sm)`。
- BFM 写过程：[hdl/psi_tb_axi_pkg.vhd:L519-L550](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L519-L550)，参数为 `(address, value, ms, sm, clk)`，调用时把 `tb_ms` 当 `ms`、`tb_sm` 当 `sm`。
- 初始化过程：[hdl/psi_tb_axi_pkg.vhd:L467-L500](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L467-L500) `axi_master_init(signal ms : out axi_ms_r)`，调用前先把 `tb_ms` 归零。

> 注意一个宽度陷阱：综合侧 `axi_slv_inp` 的数据/地址宽度被 psi_common 常量写死成 32 位，所以 TB 侧 `tb_ms`/`tb_sm` 里那几个未约束向量也必须声明成 32 位（地址 32、数据 32、wstrb 4、各 ID/USER 1 位），否则转换里的 `<=` 会出现位宽不匹配。

#### 4.4.4 代码实践

1. **目标**：写一个最小 TB 骨架，验证「BFM 写 → 转换 → 综合侧 slave 收到正确地址与数据」。
2. **前置（编译）**：因为三个 AXI 包都没进 CI，先在 `sim/config.tcl` 的 `lib` tag 增加 `psi_common_axi_pkg.vhd`，`src` tag 增加 `psi_tb_axi_pkg.vhd` 与 `psi_tb_axi_conv_pkg.vhd`，并为你的 TB 新增 `create_tb_run`/`add_tb_run`（参见 [u1-l3](u1-l3-simulation-and-ci.md) 与 [u8-l1](u8-l1-ci-and-sim-scripts-deep.md)）。
3. **操作**：把下面的示例代码存成一个 TB 文件并编译运行。

> 下面是**示例代码**（非项目原有文件），用于演示接线结构。TB 侧未约束记录的子类型约束语法依赖 VHDL-2008 与具体工具，**待本地验证**；综合侧记录无需约束。

```vhdl
-- 示例代码：演示 axi_conv_tb_synth_master 的接线方式（待本地验证）
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
library work;
use work.psi_common_math_pkg.all;    -- log2 等
use work.psi_common_axi_pkg.all;     -- axi_slv_inp / axi_slv_oup
use work.psi_tb_txt_util.all;        -- print
use work.psi_tb_axi_pkg.all;         -- axi_ms_r / axi_single_write 等
use work.psi_tb_axi_conv_pkg.all;    -- axi_conv_tb_synth_master

entity axi_conv_demo_tb is
end entity;

architecture sim of axi_conv_demo_tb is
  signal Clk : std_logic := '0';

  -- 1) TB 侧记录：必须把未约束向量约束成与综合侧一致的宽度（数据 32、地址 32 等）
  --    语法为 VHDL-2008 记录子类型约束，具体写法待本地验证。
  subtype axi_ms_32_t is axi_ms_r(
    arid(0 downto 0), araddr(31 downto 0), aruser(0 downto 0),
    awid(0 downto 0), awaddr(31 downto 0), awuser(0 downto 0),
    wdata(31 downto 0), wstrb(3 downto 0), wuser(0 downto 0));
  subtype axi_sm_32_t is axi_sm_r(
    rid(0 downto 0), rdata(31 downto 0), ruser(0 downto 0),
    bid(0 downto 0), buser(0 downto 0));

  signal tb_ms : axi_ms_32_t;
  signal tb_sm : axi_sm_32_t;

  -- 2) 综合侧记录：宽度由 psi_common 包常量固定，无需约束
  signal syn_ms : axi_slv_inp;
  signal syn_sm : axi_slv_oup;

  -- 给行为 slave 用的捕获寄存器，用于事后核对
  shared variable captured_addr : integer := -1;
  shared variable captured_data : integer := -1;
begin
  -- 时钟 100 MHz
  Clk <= not Clk after 5 ns;

  -- 3) 并发过程调用：转换接线盒（写在并发语句区，不要放进 process）
  axi_conv_tb_synth_master(tb_ms, tb_sm, syn_ms, syn_sm);

  -- 4) 综合侧行为 slave：接受一次单拍写，回 OKAY
  p_synth_slave : process(Clk)
  begin
    if rising_edge(Clk) then
      -- 默认拉低 ready/valid，避免锁存
      syn_sm.aw.ready <= '0';
      syn_sm.dw.ready <= '0';
      syn_sm.b.valid  <= '0';
      syn_sm.b.resp   <= xRESP_OKAY_c;

      if syn_ms.aw.valid = '1' then
        syn_sm.aw.ready <= '1';
      end if;
      if syn_ms.dw.valid = '1' then
        syn_sm.dw.ready <= '1';
        captured_addr := to_integer(unsigned(syn_ms.aw.addr));
        captured_data := to_integer(signed(syn_ms.dw.data));
      end if;
      -- 数据被接受的下一拍给写响应（简化处理）
      if syn_ms.b.ready = '1' then
        syn_sm.b.valid <= '1';
      end if;
    end if;
  end process;

  -- 刺激：用 BFM 写一笔，再核对综合侧收到的值
  p_stim : process
  begin
    axi_master_init(tb_ms);
    wait until rising_edge(Clk);
    -- 写地址 0x10，数据 0x2A
    axi_single_write(address => 16#10#, value => 16#2A#,
                     ms => tb_ms, sm => tb_sm, clk => Clk);
    wait until rising_edge(Clk);
    -- 核对：综合侧 slave 应捕获到地址 0x10、数据 0x2A
    print("captured_addr = " & str(captured_addr) &
          ", captured_data = " & str(captured_data));
    assert captured_addr = 16#10# report "###ERROR###: 地址转换错误" severity error;
    assert captured_data = 16#2A# report "###ERROR###: 数据转换错误" severity error;
    print("AXI CONV DEMO DONE");
    wait for 50 ns;
    std.env.stop;   -- VHDL-2008；老工具可改用 assert ... severity failure
    wait;
  end process;
end architecture;
```

4. **观察/预期**：Transcript 打印 `captured_addr = 16, captured_data = 42` 与 `AXI CONV DEMO DONE`；不出现 `###ERROR###`。`16`/`42` 即 `0x10`/`0x2A` 的十进制。
5. **若行为 slave 简化握手导致仿真挂死或时序不准**：本例把 B 通道响应做了简化，可能与 `axi_single_write` 的严格握手顺序不完全吻合；如挂死，请把 slave 改成更贴合 AW→W→B 顺序的状态机。运行结果**待本地验证**。

#### 4.4.5 小练习与答案

- **练习 1**：把 `axi_conv_tb_synth_master(...)` 这行误放进 `p_stim` 进程里会怎样？
  - **答案**：进程内无 `wait` 的过程会在零时刻无限循环，仿真挂死；正确做法是放在架构体的并发语句区。
- **练习 2**：如果 DUT 是 AXI **主机**、TB 扮演 **从机**，还能直接用本过程吗？
  - **答案**：不能直接用。本过程名字是 `_master`，专门接线「TB 当主机」场景。TB 当从机需要把 tb_ms/tb_sm 与 syn_ms/syn_sm 的方向反过来映射（本项目当前未提供 `_slave` 版本，需自行编写，思路相同）。

---

## 5. 综合实践

把本讲三块知识串成一个完整任务：

**任务**：补全 4.4.4 的示例 TB，使其成为可跑通的端到端验证。

要求做到：

1. **依赖接入**：在 `sim/config.tcl` 的 `lib` tag 加入 `psi_common_axi_pkg.vhd`，`src` tag 加入 `psi_tb_axi_pkg.vhd` 与 `psi_tb_axi_conv_pkg.vhd`，并把示例 TB 注册为 `create_tb_run` + `add_tb_run`（参考 [u1-l3](u1-l3-simulation-and-ci.md) 的 PsiSim 流程）。
2. **接线**：保留并发过程调用 `axi_conv_tb_synth_master(tb_ms, tb_sm, syn_ms, syn_sm)`，并解释为什么它必须并发。
3. **驱动**：用 `axi_single_write` 在地址 `0x10` 写入 `0x2A`；再用一次 `axi_single_read`（或 `axi_single_expect`）从综合侧 slave 读回，验证数据通路双向都通。
4. **核对**：综合侧 slave 收到的地址/数据用 `print` 打印，并用带 `###ERROR###` 前缀的 `assert` 断言（与 [u3-l1](u3-l1-compare-basic.md)、[u1-l3](u1-l3-simulation-and-ci.md) 的 CI 约定一致）。
5. **回归 CI 约定**：若断言失败，Transcript 应出现 `###ERROR###`，被 `run_check_errors "###ERROR###"` 捕获（见 [u1-l3](u1-l3-simulation-and-ci.md)）。

完成标志：Transcript 打印 `AXI CONV DEMO DONE` 且无 `###ERROR###`。

> 提示：综合侧记录 `axi_slv_inp` / `axi_slv_oup` 的宽度被 psi_common 常量写死，所以 TB 侧未约束向量必须对齐成 32 位数据/地址；若编译报位宽不匹配，先检查 `tb_ms`/`tb_sm` 的子类型约束。

## 6. 本讲小结

- `psi_tb_axi_conv_pkg` 把「TB AXI 记录 ↔ 综合 AXI 记录」的转换**独立成包**，唯一目的是让核心 BFM 包 `psi_tb_axi_pkg` 不必拖上 `psi_common_axi_pkg` 这个强依赖——证据：BFM 包的 `use` 子句里没有综合 AXI 包，且三个 AXI 包都不在 `config.tcl` 的 CI 编译清单里。
- 两套类型描述同一组 AXI 信号：psi_tb 用**扁平 + 未约束**字段，psi_common 用**嵌套子记录 + 写死宽度**（数据/地址 32、ID/USER 1）。方向上两边一致：`axi_ms_r`↔`axi_slv_inp`，`axi_sm_r`↔`axi_slv_oup`。
- `axi_conv_tb_synth_master` 是一个**纯组合、无时钟、无 wait** 的接线盒：master→slave 段把 `tb_ms` 摊平装进 `syn_ms`（注意 R/B 通道只剩 `rready`/`bready`），slave→master 段把 `syn_sm` 摊平回 `tb_sm`。
- 它必须在 testbench 架构体里作为**并发过程调用**使用；放进普通进程会因无 `wait` 而挂死仿真。
- 要实际跑通，需先把三个 AXI 包加入 `config.tcl` 编译清单，并把 TB 侧未约束向量的宽度对齐到综合侧写死的 32 位。

## 7. 下一步学习建议

- 想看更完整的 AXI 验证套路：回到 [u5-l2](u5-l2-axi-single-transactions.md) 与 [u5-l3](u5-l3-axi-partial-and-burst.md)，把 `axi_single_expect`、`axi_apply_*` / `axi_expect_*` 突发积木接到本讲的转换接线盒上，构造一个综合 DUT 的突发读/写验证。
- 想深入 CI 与编译清单的改法：阅读 [u8-l1](u8-l1-ci-and-sim-scripts-deep.md)，搞清 `add_sources -tag`、`create_tb_run`/`add_tb_run` 与 `run_check_errors` 的协同。
- 想扩展（例如加一个 TB 当从机的 `_slave` 版本）：参考 [u8-l2](u8-l2-conventions-and-extending.md) 的编码约定，沿用本讲「逐字段、方向保持、独立成包」的思路自行实现。
- 跨包对照阅读：直接打开 `psi_common/hdl/psi_common_axi_pkg.vhd`，把 `rec_axi_ms`/`rec_axi_sm` 的每个子记录字段，逐个对照 `hdl/psi_tb_axi_pkg.vhd` 的扁平字段，这是巩固本讲最快的方式。
