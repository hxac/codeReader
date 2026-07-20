# AXI 简化主机 axi_master_simple

## 1. 本讲目标

本讲讲解 psi_common 中**最常用的 AXI 主机**：`psi_common_axi_master_simple`。它把用户侧一组非常简单的「命令 + 数据流」接口，翻译成完整的 AXI4 五通道握手。读完本讲你应当能够：

- 说清 AXI 四个通道（AR/R/AW/W/B）各自承担什么、谁给谁 valid、谁给谁 ready；
- 看懂 simple 主机的用户命令接口（地址、长度、低延迟标志）与读写数据接口；
- 理解一次「用户命令」如何被自动拆成若干 AXI 突发（burst），以及 4KB 边界与最大突发长度的约束；
- 解释 high-latency / low-latency 两种 throttling 模式的差异，以及 outstanding transaction 数量的限制。

本讲承接 [u2-l4](u2-l4-axi-pkg.md)（AXI record 类型）与 [u7-l1](u7-l1-pl-stage.md)（二进程 record 设计法），是后续 [u9-l4](u9-l4-axi-master-full.md)（完整版主机）与 [u9-l5](u9-l5-axi-slave.md)（AXI 从机）的基础。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**第一，AXI 是什么。** AXI（Advanced eXtensible Interface）是 ARM AMBA 总线家族里用于高速存储器映射的协议。它把一次数据搬运拆成**五个独立通道**，每个通道都是一对 `valid`/`ready` 握手信号（即 [u1-l4](u1-l4-coding-conventions-handshaking.md) 讲过的 AXI-Stream 握手）。读路径有「读地址 AR」和「读数据 R」两个通道；写路径有「写地址 AW」「写数据 W」「写响应 B」三个通道。通道之间相互独立，因此读和写可以**同时**进行。

**第二，谁发送、谁接收。** 对**主机（Master）**而言：

- AR 通道：主机发地址 + `arvalid`，从机回 `arready`；
- R 通道：从机发数据 + `rvalid` + `rlast`，主机回 `rready`；
- AW 通道：主机发地址 + `awvalid`，从机回 `awready`；
- W 通道：主机发数据 + `wvalid` + `wlast`，从机回 `wready`；
- B 通道：从机发响应 + `bvalid`，主机回 `bready`。

记住一句话：**数据往哪边流，valid 就在哪边；ready 永远在接收方。**

**第三，突发（burst）与 len 字段。** AXI 不必一拍传一字。主机在 AR/AW 上给出起始地址、`arsize`/`awsize`（每拍字节数的 2 的幂）、`arlen`/`awlen`（突发长度），之后 R/W 通道就连传若干拍。AXI 规定 `len = 拍数 - 1`（即长度字段是「拍数减一」），最长突发 AXI3 为 16 拍、AXI4 为 256 拍，且一个突发**不得跨越 4KB 地址边界**。这两个约束是本讲后面「命令拆分」逻辑的出发点。

## 3. 本讲源码地图

| 文件 | 作用 |
|:--|:--|
| [hdl/psi_common_axi_master_simple.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_simple.vhd) | 被精读的主体：simple AXI 主机实体与 `rtl` 架构 |
| [hdl/psi_common_axi_pkg.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd) | AXI record 类型与默认常量（[u2-l4](u2-l4-axi-pkg.md) 已讲，本讲仅引用） |
| [testbench/psi_common_axi_master_simple_tb/psi_common_axi_master_simple_tb_pkg.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axi_master_simple_tb/psi_common_axi_master_simple_tb_pkg.vhd) | TB 的命令/数据/AXI 校验过程库，是本讲代码实践的依据 |
| [testbench/psi_common_axi_master_simple_tb/psi_common_axi_master_simple_tb_case_simple_tf.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axi_master_simple_tb/psi_common_axi_master_simple_tb_case_simple_tf.vhd) | 「简单传输」用例，含单字与突发读写的自检场景 |

源码头部的注释已经把组件定位说得很清楚——所谓 "simple"，是指它**不做非对齐访问、不做位宽转换**，只负责把用户请求执行掉，并在必要时拆成多个 AXI 事务以避免越过 4KB 边界、不超过最大事务长度：

[hdl/psi_common_axi_master_simple.vhd:L9-L14](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_simple.vhd#L9-L14) —— 注释说明 "simple" 的确切含义。

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：① AXI 四通道与「simple」定位；② 用户命令/数据接口；③ 突发传输与 4KB 边界自动拆分；④ throttling（高/低延迟与 outstanding 控制）。

### 4.1 AXI 四通道与「simple」主机的定位

#### 4.1.1 概念说明

`psi_common_axi_master_simple` 是一个**纯 VHDL、可综合、完全二进程 record 风格**的 AXI 主机。它的核心价值是**接口翻译**：用户只想说「从地址 A 读 N 拍」「向地址 B 写 M 拍」，不想关心 AR/R/AW/W/B 五通道的时序细节，这个组件替用户把每一拍 valid/ready 都管理好。

注意它和 [u2-l4](u2-l4-axi-pkg.md) 讲的 `psi_common_axi_pkg` 的关系：那个包提供的是**给集成者**用的 record 类型，用来简化顶层端口连线；而本组件**本身仍使用扁平的 AXI 信号**（`m_axi_araddr`、`m_axi_arvalid` ……），并没有用 record 端口。这是因为组件内部要逐个驱动这些信号，扁平信号写起来更直接。集成时再借助 axi_pkg 把它们打包成 record 即可。

#### 4.1.2 核心流程

组件内部用**三组状态机**分别管理写路径与读路径：

```text
用户命令 ──► TfGen FSM (拆命令) ──► AW/AR FSM (发地址) ──► AXI 从机
                                          │
写数据 FIFO ◄── W FSM (发数据) ◄──────────┤  (写路径)
                                          │
                       读数据 FIFO ◄── R 通道 (从机回数据)  (读路径)
                                          │
                            B 通道/rlast ──► 响应 ──► done/error 给用户
```

- **TfGen（Transfer Generation）FSM**：把一条用户命令拆成若干 AXI 事务；
- **AW/AR FSM**：在地址通道上做 valid/ready 握手，发出每个事务的地址与长度；
- **W FSM**（仅写路径）：按突发长度把写数据从 FIFO 里推到 W 通道，并在最后一拍拉 `wlast`；
- 读路径没有独立的 R FSM，因为 R 通道的接收由读数据 FIFO + `rready` 直接处理。

#### 4.1.3 源码精读

先看实体声明。所有 AXI 输出信号（`m_axi_*`）都集中在端口里，按 AW/W/B/AR/R 五通道分组，每个通道都严格遵循「valid 跟数据走、ready 在对侧」的约定：

[hdl/psi_common_axi_master_simple.vhd:L65-L100](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_simple.vhd#L65-L100) —— 五个 AXI 通道的扁平端口，注意 `awvalid/arvalid/wlast` 是 `out`，而 `awready/wready/bvalid/arready/rvalid/rlast` 是 `in`。

组件还沿用了 [u7-l1](u7-l1-pl-stage.md) 的**二进程 record 设计法**：所有寄存器收敛进一个 record `two_process_r`，组合进程 `p_comb` 算次态 `r_next`，时序进程 `p_reg` 只负责打拍与复位：

[hdl/psi_common_axi_master_simple.vhd:L144-L215](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_simple.vhd#L144-L215) —— record 定义，写相关与读相关寄存器分区存放。

[hdl/psi_common_axi_master_simple.vhd:L549-L591](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_simple.vhd#L549-L591) —— 时序进程，`rising_edge` 打拍，`aresetn='0'` 时把各 FSM 复位到 `Idle_s`、valid 清零、读 FIFO 空间初始化为满。

还有一些 AXI 属性是**常量输出**，组件不暴露给用户配置，而是按 AXI 参考指南固定下来：

[hdl/psi_common_axi_master_simple.vhd:L613-L623](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_simple.vhd#L613-L623) —— `arsize/awsize = log2(数据字节宽)`、突发类型固定为 INCR(`"01"`)、cache=`"0011"`、prot=`"000"`、不支持独占访问(lock=`'0'`)；`bready` 在实现写功能时恒为 `'1'`（主机总能接收写响应）。

#### 4.1.4 代码实践

**实践目标**：在实体声明里把五通道的 valid/ready 方向对应清楚。

1. 打开 [hdl/psi_common_axi_master_simple.vhd:L65-L100](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_simple.vhd#L65-L100)。
2. 对每个通道填一张小表：通道名 | 数据/控制信号(方向) | valid(方向) | ready(方向) | last(方向)。
3. **预期结果**：你会发现 AR/AW 通道 `valid` 是 `out`、`ready` 是 `in`；R 通道反过来 `valid/last` 是 `in`、`ready` 是 `out`；B 通道 `valid` 是 `in`、`ready` 是 `out`；W 通道 `valid/last` 是 `out`、`ready` 是 `in`。这正好印证「数据流方向决定 valid 方向」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `m_axi_bready` 是输出、而 `m_axi_bvalid` 是输入？
**答案**：B 通道是「从机 → 主机」的写响应，数据（响应）由从机发出，故从机给 `bvalid`（输入到主机），主机作为接收方给 `bready`（输出）。

**练习 2**：组件把 `m_axi_awburst` 固定为 `"01"`，这代表什么？
**答案**：`"01"` 是 INCR（递增）突发，即地址随每拍自动加上一个「每拍字节数」，是最常用的存储器访问类型；组件不支持 WRAP 等其它类型。

---

### 4.2 用户命令/数据接口

#### 4.2.1 概念说明

simple 主机给用户提供**三组 AXI-Stream 风格的接口**（每组都是 valid/ready 握手）：

- **命令接口**（读、写各一套）：地址 `cmd_xx_addr_i`、长度 `cmd_xx_size_i`（单位是「拍数」）、低延迟标志 `cmd_xx_low_lat_i`、`cmd_xx_vld_i`/`cmd_xx_rdy_o`。
- **写数据接口**：`wr_dat_i`、字节使能 `wr_data_be_i`、`wr_vld_i`/`wr_rdy_o`。
- **读数据接口**：`rd_dat_o`、`rd_vld_o`/`rd_rdy_i`。
- **响应**：`wr_done_o`/`wr_error_o`、`rd_done_o`/`rd_error_o`（单拍脉冲）。

关键设计点：**命令与数据之间没有时序约束**。写数据可以先于、后于、或同时于命令给出——组件内部用 FIFO 缓冲数据来解耦。读命令发出后，从机回的数据先进入**读数据 FIFO**，用户按自己的节奏读。

#### 4.2.2 核心流程

写路径的命令接收流程（`WriteTfGen` FSM 的 `Idle_s` → `MaxCalc_s`）：

```text
Idle_s:      cmd_wr_rdy_o='1'，等 cmd_wr_vld_i='1'
             锁存地址(对齐掩码)、拍数、低延迟标志
             ↓
MaxCalc_s:   计算本次事务最大可发拍数(受 4KB 边界与 axi_max_beats_g 限制)
             ↓
GenTf_s:     决定本次事务拍数 WrTfBeats、是否最后一段 WrTfIsLast
             ↓
WriteTf_s:   等地址通道 FSM 收下(AwFsmRdy)，扣减剩余拍数、推进地址
             若 WrTfIsLast 回 Idle_s，否则回 MaxCalc_s 继续拆下一段
```

读路径的 `ReadTfGen` FSM 结构完全对称。

#### 4.2.3 源码精读

用户接口端口如下，注意地址与长度位宽都由 generic 推导：

[hdl/psi_common_axi_master_simple.vhd:L39-L64](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_simple.vhd#L39-L64) —— 命令接口、写数据、读数据、响应端口；`cmd_xx_size_i` 位宽为 `user_transaction_size_bits_g`，因此用户可请求的拍数上限仅由这个 generic 决定。

命令接收时，地址会经过 `AddrMasked_f` 把低位清零，保证对齐：

[hdl/psi_common_axi_master_simple.vhd:L133-L139](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_simple.vhd#L133-L139) —— 把地址低 `log2(数据字节宽)` 位强制清零；这与「不做非对齐访问」的定位一致。

`Idle_s` 锁存命令、`MaxCalc_s`/`GenTf_s`/`WriteTf_s` 拆分事务：

[hdl/psi_common_axi_master_simple.vhd:L264-L308](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_simple.vhd#L264-L308) —— 写路径命令拆分 FSM，`Idle_s` 拉高 `cmd_wr_rdy_o` 接命令、扣拍数、按 `2**UnusedAddrBits_c * WrTfBeats` 推进地址。

写数据与读数据各用一个 `psi_common_sync_fifo` 缓冲（见 [u4-l1](u4-l1-sync-fifo.md)）。写数据 FIFO 把 `wr_dat_i` 与 `wr_data_be_i` 拼成一字存入；读数据 FIFO 直接缓冲 `m_axi_rdata`：

[hdl/psi_common_axi_master_simple.vhd:L654-L686](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_simple.vhd#L654-L686) —— 写数据 FIFO，`m_axi_wvalid` 由 FIFO 输出 valid **与** `WDataEna`（W FSM 允许）相与，`WrDataFifoORdy` 由 `m_axi_wready` 与 `WDataEna` 相与。

[hdl/psi_common_axi_master_simple.vhd:L721-L746](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_simple.vhd#L721-L746) —— 读数据 FIFO，从机 `rdata` 进 FIFO，用户侧 `rd_dat_o` 出 FIFO；`m_axi_rready` 直接由 FIFO 的 `rdy_o` 驱动。

此外，读/写功能可由 `impl_read_g`/`impl_write_g` 在综合期各自裁掉以省资源，裁掉的一侧把 AXI 信号接地：

[hdl/psi_common_axi_master_simple.vhd:L710-L715](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_simple.vhd#L710-L715) —— 不实现写时，`wdata/wstrb/wvalid` 全部置零、`wr_rdy_o` 恒 `'0'`（读侧对称处理见 L775-L779）。

#### 4.2.4 代码实践

**实践目标**：在测试平台里看清「一次命令 + 数据」是如何被施加的。

1. 打开 TB 包过程 [psi_common_axi_master_simple_tb_pkg.vhd:L175-L196](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axi_master_simple_tb/psi_common_axi_master_simple_tb_pkg.vhd#L175-L196)（`ApplyCommand`）与 L198-L248（`ApplyWrDataMulti`）。
2. 对照用例 [psi_common_axi_master_simple_tb_case_simple_tf.vhd:L113-L114](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axi_master_simple_tb/psi_common_axi_master_simple_tb_case_simple_tf.vhd#L113-L114) 中「先命令、后数据」的场景。
3. **需要观察的现象**：`ApplyCommand` 先置 `cmd_xx_vld_i='1'` 并等 `cmd_xx_rdy_o='1'` 完成握手；数据过程则在另一个并发进程里独立地驱动 `wr_vld_i`。两者没有时序绑定。
4. **预期结果**：你能解释「命令先于数据」为何不会丢数据——因为命令进入 TfGen FSM 后，AW 会受 throttling 门控等待数据入 FIFO（见 4.4）。

#### 4.2.5 小练习与答案

**练习 1**：`cmd_wr_size_i` 表示的是「字节数」还是「拍数」？
**答案**：拍数（beats）。它直接进入 `WrBeats` 计数器，并和 `axi_max_beats_g` 比较；最终 AXI 的 `awlen = 拍数 - 1`。

**练习 2**：为什么写数据要和字节使能 `wr_data_be_i` 一起存进同一个 FIFO？
**答案**：W 通道上 `wdata` 与 `wstrb` 是**同拍**发出的，二者必须保持一一对齐；把两者拼接存入同一 FIFO 项，能保证读出时天然对齐、不会错位。

---

### 4.3 突发传输与 4KB 边界自动拆分

#### 4.3.1 概念说明

用户请求的拍数 `N` 可能远超 AXI 单次突发上限，或者起始地址离 4KB 边界很近、装不下整个突发。AXI 规范要求：**单个突发不得越过 4KB（\(2^{12}\) 字节）边界**，且长度不超过协议上限（AXI3 16 拍、AXI4 256 拍）。

simple 主机不把这两个约束甩给用户，而是在内部**自动把一条用户命令拆成多个 AXI 事务**。每个事务的长度取三者最小值：剩余拍数、到 4KB 边界的剩余拍数、`axi_max_beats_g`。地址在事务之间按已发拍数自动递增。

#### 4.3.2 核心流程

设用户命令起始地址 \(A\)（已对齐）、拍数 \(N\)、每拍字节数 \(B=2^{\text{UnusedAddrBits}}\)。则到下一个 4KB 边界还能容纳的拍数为：

\[
\text{BeatsTo4k} = \left\lfloor \frac{(2^{12} - (A \bmod 2^{12}))}{B} \right\rfloor}
\]

每个 AXI 事务的拍数为：

\[
\text{TfBeats} = \min(\,N_{\text{剩余}},\ \text{BeatsTo4k},\ \text{axi\_max\_beats\_g}\,)
\]

发完一个事务后，\(N_{\text{剩余}} \leftarrow N_{\text{剩余}} - \text{TfBeats}\)，地址 \(A \leftarrow A + B \cdot \text{TfBeats}\)，直到剩余为 0。AXI 的 `len` 字段为 \(\text{TfBeats}-1\)。

#### 4.3.3 源码精读

`MaxCalc_s` 用 `not(x) + 1`（即「按位取反加一」，等价于求补/取负）来计算到 4KB 边界的剩余拍数，再与 `axi_max_beats_g` 取小：

[hdl/psi_common_axi_master_simple.vhd:L275-L282](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_simple.vhd#L275-L282) —— 写路径：`WrMax4kBeats_v := unsigned('0' & not r.WrAddr(11 downto UnusedAddrBits_c)) + 1`，再夹取到 `axi_max_beats_g`。读路径完全对称，见 [L442-L449](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_simple.vhd#L442-L449)。

`GenTf_s` 决定本事务拍数 `WrTfBeats` 与「是否最后一段」`WrTfIsLast`：

[hdl/psi_common_axi_master_simple.vhd:L284-L293](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_simple.vhd#L284-L293) —— 若 `WrMaxBeats < WrBeats` 则不是最后一段，取 `WrMaxBeats`；否则取剩余全部、标记 `IsLast`。

地址通道 FSM 把 `WrTfBeats - 1` 写进 `awlen`，这正是 AXI「len = 拍数 − 1」的约定：

[hdl/psi_common_axi_master_simple.vhd:L317-L327](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_simple.vhd#L317-L327) —— AW 通道：置 `m_axi_awaddr`、`awlen = WrTfBeats-1`、`awvalid='1'`，进入 `Wait_s` 等 `awready`。读路径对应 [L484-L494](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_simple.vhd#L484-L494)，`arlen = RdTfBeats-1`。

一段事务被地址通道收下后，`WriteTf_s` 扣减剩余拍数并按已发拍数推进地址：

[hdl/psi_common_axi_master_simple.vhd:L295-L305](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_simple.vhd#L295-L305) —— `WrBeats := WrBeats - WrTfBeats`、`WrAddr := WrAddr + 2**UnusedAddrBits_c * WrTfBeats`，然后回 `Idle_s` 或再回 `MaxCalc_s`。

写响应 `B` 通道与读响应的 `rlast` 各经过一个深度为 `axi_max_open_transactions_g` 的小 FIFO（存「是否最后一段」标志），用来在响应到达时判断「整条用户命令是否全部完成」：

[hdl/psi_common_axi_master_simple.vhd:L688-L706](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_simple.vhd#L688-L706) —— 写响应 FIFO，存 `WrTfIsLast`，深度等于最大 outstanding 事务数；读响应 FIFO 对称见 [L748-L770](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_simple.vhd#L748-L770)。

#### 4.3.4 代码实践

**实践目标**：用一条用户命令「读 12 拍」观察自动拆分行为（基于既有 TB）。

1. 打开用例 [psi_common_axi_master_simple_tb_case_simple_tf.vhd:L176-L179](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axi_master_simple_tb/psi_common_axi_master_simple_tb_case_simple_tf.vhd#L176-L179)（TestCase 9，高延迟 12 拍读）。
2. 注意 TB 配置：`axi_max_beats_g = 16`、`data_fifo_depth_g = 14`（见 [tb_pkg:L37-L43](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axi_master_simple_tb/psi_common_axi_master_simple_tb_pkg.vhd#L37-L43)）。
3. 因为 12 ≤ 16 且起点 `0x00020000` 离 4KB 边界足够远，整条命令**只会拆成 1 个 AXI 事务**，`arlen = 12 - 1 = 11`。
4. 对照 AXI 校验过程 [tb_pkg:L355-L372](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axi_master_simple_tb/psi_common_axi_master_simple_tb_pkg.vhd#L355-L372)（`AxiCheckRdBurst` 调用 `axi_expect_ar(Addr, size, Size-1, INCR, ...)`）。
5. **预期结果**：从机侧期望的 `arlen = Size - 1`。把 `Size` 改成大于 `axi_max_beats_g`（例如 20），你会看到它被拆成 `16 + 4` 两个事务（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：起始地址 `0x00000FF8`、数据宽 32 位、`axi_max_beats_g=256`，读 8 拍，会被拆成几段？
**答案**：32 位 = 4 字节，`0xFF8` 到 4KB 边界 `0x1000` 还有 8 字节 = 2 拍。故第一段最多 2 拍（跨 4KB 前停下），剩下 6 拍从 `0x1000` 起发第二段。共 2 段。

**练习 2**：为什么「拆分」逻辑要用 `not(addr[11:b]) + 1` 而不是直接做减法？
**答案**：`not(x)+1` 是求补运算，等价于求「到上溢边界还差几拍」的快速实现；它避免显式的除法/取模，只用取反与加一即可综合出廉价逻辑。

---

### 4.4 throttling：高/低延迟模式与 outstanding 控制

#### 4.4.1 概念说明

「throttling」指主机在什么条件下才允许把一个 AXI 事务真正发到总线上。simple 主机提供两种模式，由命令接口的 `cmd_xx_low_lat_i` 选择：

- **High Latency（低延迟标志 = 0，默认推荐用于读）**：只有当 FIFO 里**已有足够数据（写）**或**有足够空闲空间（读）**能装下整个事务时，才发地址命令。好处是**绝不阻塞 AXI 总线**——一旦发命令，数据/空间肯定就绪，可以连续突发。
- **Low Latency（低延迟标志 = 1）**：收到命令**立刻**发地址，不管 FIFO 状态。好处是**延迟最低**；坏处是若用户供不上数据（写）或取走不够快（读），`wvalid`/`rready` 会出现空洞，**临时阻塞总线**、浪费带宽。

此外，无论哪种模式，主机都限制**同时未完成（outstanding）的事务数**不超过 `axi_max_open_transactions_g`，防止总线被过多命令淹没、也限制内部 FIFO 的最大占用。

#### 4.4.2 核心流程

写路径在 `AwFsm` 的 `Idle_s` 用一个综合条件门控命令发出：

\[
\text{Gate}_{\text{wr}} = \big(\text{WrLowLat} \lor (\text{WrBeatsNoCmd} \geq \text{WrTfBeats})\big) \land \big(\text{WrOpenTrans} < \text{axi\_max\_open\_transactions\_g}\big) \land \text{WrTfVld}
\]

读路径在 `ArFsm` 的 `Idle_s` 用对称条件：

\[
\text{Gate}_{\text{rd}} = \big(\text{RdLowLat} \lor (\text{RdFifoSpaceFree} \geq \text{RdTfBeats})\big) \land \big(\text{RdOpenTrans} < \text{axi\_max\_open\_transactions\_g}\big) \land \text{RdTfVld}
\]

其中 `WrBeatsNoCmd` 是「已在写数据 FIFO 里、但还没在 AW 命令中声明」的拍数；`RdFifoSpaceFree` 是读数据 FIFO 的剩余可写空间。每发出一个地址命令，`OpenTrans` 加一；每收到一个写响应 `bvalid`（或读数据 `rlast`），`OpenTrans` 减一。

#### 4.4.3 源码精读

写路径 throttling 门（注意第一个分支就是「低延迟直接放行」）：

[hdl/psi_common_axi_master_simple.vhd:L312-L316](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_simple.vhd#L312-L316) —— `((r.WrLowLat = '1') or (r.WrBeatsNoCmd >= signed('0' & r.WrTfBeats))) and (r.WrOpenTrans < axi_max_open_transactions_g) and (r.WrTfVld = '1')`。

读路径 throttling 门（用读 FIFO 空闲空间代替数据计数）：

[hdl/psi_common_axi_master_simple.vhd:L480-L483](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_simple.vhd#L480-L483) —— `((r.RdLowLat = '1') or (r.RdFifoSpaceFree >= signed('0' & r.RdTfBeats))) and (r.RdOpenTrans < axi_max_open_transactions_g) and (r.RdTfVld = '1')`。

`WrBeatsNoCmd` 的维护逻辑（注释说明它「为时序优化」写法略奇怪——立即扣减、延一拍加回）：

[hdl/psi_common_axi_master_simple.vhd:L344-L352](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_simple.vhd#L344-L352) —— 发命令时按事务大小扣减，平时每写入一拍数据加一。读侧 `RdFifoSpaceFree` 维护对称，见 [L511-L519](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_simple.vhd#L511-L519)。

`OpenTrans` 的增减：地址被从机收下时 `+1`（写 [L330-L331](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_simple.vhd#L330-L331)、读 [L497-L498](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_simple.vhd#L497-L498)），收到响应时 `-1`（写 [L406-L408](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_simple.vhd#L406-L408)、读 [L524-L526](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_simple.vhd#L524-L526)）。

复位时 `RdFifoSpaceFree` 初值设为 `data_fifo_depth_g`（即 FIFO 全空、空间最大）：

[hdl/psi_common_axi_master_simple.vhd:L586](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_simple.vhd#L586) —— `r.RdFifoSpaceFree <= to_signed(data_fifo_depth_g, ...)`，保证复位后高延迟读能立即通过门控。

#### 4.4.4 代码实践

**实践目标**：用一次「4 拍突发读」画出 AR/R 两通道的完整握手时序（高延迟模式、FIFO 有空闲）。

下面是基于源码逻辑与 AXI 协议推导出的时序（4 拍 INCR 读，从机连续响应）：

```text
clk     ___|‾‾|__|‾‾|__|‾‾|__|‾‾|__|‾‾|__|‾‾|__|‾‾|__|‾‾|__|‾‾|__
arvalid  ______|‾‾‾‾‾‾‾‾|________________________________   (cyc1 拉高)
arready  ________________|‾‾‾‾‾‾|________________________   (cyc2 从机收)
araddr   ____|<A>________________________________________
arlen    ____|<3 = 4-1>__________________________________   (拍数-1)
arsize   ____|<log2(字节宽)>_____________________________
arburst  ____|<01 INCR>__________________________________
                                     ___________________
rvalid  ___________________________|‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾|   (cyc4..cyc7)
rready  ________________________________________________‾‾ (主机恒取)
rdata   ___________________________|<d0>|<d1>|<d2>|<d3>|
rlast   __________________________________________|‾‾‾‾|   (仅第4拍高)
rd_done_o(给用户) ________________________________|‾‾|__   (rlast+OKAY 那拍)
```

操作步骤：

1. 读 TB 的读校验过程 [tb_pkg:L355-L372](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axi_master_simple_tb/psi_common_axi_master_simple_tb_pkg.vhd#L355-L372)，确认从机侧期望 `arlen = Size - 1`、突发类型 INCR。
2. 在用例里把一次读改成「地址 `0x00020000`、4 拍、高延迟」：参考 [case_simple_tf.vhd:L177-L178](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axi_master_simple_tb/psi_common_axi_master_simple_tb_case_simple_tf.vhd#L177-L178) 的写法，把 `12` 改为 `4`。
3. 若要真实跑通，按 [u1-l3](u1-l3-dependencies-and-simulation.md) 的方法执行 `sim/run.tcl`（Modelsim）或 `sim/runGhdl.tcl`（GHDL），该 TB 已在 [sim/config.tcl:L379-L386](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L379-L386) 注册（注意 Vivado 因不支持无约束 record 而被 `tb_run_skip` 跳过）。
4. **需要观察的现象**：`arvalid` 一旦被从机收下（`arready` 同高）即拉低；随后 R 通道连续 4 拍 `rvalid`，第 4 拍 `rlast` 为高；`rd_done_o` 在最后一拍 OKAY 响应时给出单拍脉冲。
5. **预期结果**：因为高延迟且 FIFO 有空闲，`rready` 全程为高、R 通道背靠背无空洞、不阻塞总线。若改用低延迟且 FIFO 满（参考 TestCase 14/15），你会看到 `rready` 出现空洞（待本地验证）。

> 说明：上面的波形是根据源码门控逻辑与 AXI 协议推导的「示意时序」，未经过实际仿真器抓取；若你运行了步骤 3，请以仿真器波形为准修正各信号对齐关系。

#### 4.4.5 小练习与答案

**练习 1**：为什么官方文档建议读操作**默认用高延迟**模式？
**答案**：高延迟下读命令会等到 FIFO 有足够空间才发出，发出后主机就能连续 `rready` 接收，不阻塞总线；而读本身的首拍数据仍可被用户立即读走，所以高延迟**几乎不增加用户感知的延迟**，却换来不阻塞总线的好处。

**练习 2**：`axi_max_open_transactions_g = 3` 时，最多能同时有几条 AXI 命令「已发地址、未收响应」？
**答案**：最多 3 条。门控条件 `OpenTrans < axi_max_open_transactions_g` 在 `OpenTrans` 达到 3 时不再发新命令，要等某条事务的响应（`bvalid` 或 `rlast`）到达使 `OpenTrans` 减一后才放行下一条。

---

## 5. 综合实践

把四个最小模块串起来，做一个「读回校验」的小任务：用 simple 主机从某地址读 8 拍数据，并在用户侧校验返回值。

1. **配置**：参考 TB 的 [tb_pkg:L37-L43](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axi_master_simple_tb/psi_common_axi_master_simple_tb_pkg.vhd#L37-L43)（`axi_data_width_g=16`、`axi_max_beats_g=16`、`data_fifo_depth_g=14`）。
2. **发命令**：仿照 `ApplyCommand(0x00020000, 8, false, ...)`（高延迟），见 [case_simple_tf.vhd:L177-L178](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axi_master_simple_tb/psi_common_axi_master_simple_tb_case_simple_tf.vhd#L177-L178)。
3. **从机侧**：用 `AxiCheckRdBurst(0x00020000, 0x1000, 1, 8, xRESP_OKAY_c, ...)`（见 [tb_pkg:L355-L364](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axi_master_simple_tb/psi_common_axi_master_simple_tb_pkg.vhd#L355-L364)）回递增数据 `0x1000, 0x1001, ...`。
4. **用户侧校验**：用 `CheckRdDataMulti(0x1000, 1, 8, ...)`（见 [tb_pkg:L262-L286](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axi_master_simple_tb/psi_common_axi_master_simple_tb_pkg.vhd#L262-L286)）逐拍比对。
5. **完成后**：`WaitForCompletion(true, 1 us, rd_done_o, rd_error_o, Clk)`（见 [tb_pkg:L288-L301](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axi_master_simple_tb/psi_common_axi_master_simple_tb_pkg.vhd#L288-L301)）。
6. **画图**：画出 AR 通道一次握手（`arlen=7`）+ R 通道 8 拍（第 8 拍 `rlast`）+ `rd_done_o` 脉冲的时序。
7. **思考**：如果把地址改成离 4KB 边界只剩 4 拍空间的位置（例如 16 位宽数据下 `0x00020FF0`），8 拍会被拆成 `4 + 4` 两段，`arlen` 分别为 `3` 和 `3`，`rd_done_o` 只在第二段 `rlast` 时才出现一次——解释为什么（提示：响应 FIFO 里只有最后一段的 `IsLast=1`）。

这个任务把「命令接口 → 4KB 拆分 → 突发握手 → throttling → 响应」整条链路全部覆盖。

## 6. 本讲小结

- `psi_common_axi_master_simple` 是一个**接口翻译器**：把用户的「地址 + 拍数 + 数据流」翻译成 AXI 四通道（AR/R/AW/W，外加写响应 B）的完整握手，自身用扁平 AXI 信号、二进程 record 风格实现。
- "simple" 指**不做非对齐访问、不做位宽转换**；地址在入口被 `AddrMasked_f` 强制对齐，用户拍数即 AXI 拍数。
- 一条用户命令会被**自动拆分**成若干 AXI 事务，每段长度取「剩余拍数 / 到 4KB 边界 / `axi_max_beats_g`」三者最小值；`awlen/arlen = 拍数 − 1`。
- **throttling** 由 `cmd_xx_low_lat_i` 选择：高延迟等数据/空间就绪再发（不阻塞总线，读默认推荐），低延迟立刻发（最低延迟但可能阻塞总线）。
- outstanding 事务数受 `axi_max_open_transactions_g` 限制，由 `OpenTrans` 在发地址时 +1、收响应时 −1 维护。
- 读写数据各经一个 `psi_common_sync_fifo` 解耦，命令与数据之间无时序约束；读写路径完全独立、可同时进行。

## 7. 下一步学习建议

- 阅读 [hdl/psi_common_axi_master_full.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_full.vhd)，对照 [u9-l4](u9-l4-axi-master-full.md)，看完整版主机在 simple 之上加了哪些能力（如更丰富的命令、record 端口、AXI 流水线插入 `axi_multi_pl_stage`）。
- 阅读 [hdl/psi_common_axi_slave_ipif.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd)，进入 [u9-l5](u9-l5-axi-slave.md)，从「对侧」理解 AXI：一个寄存器/存储映射的从机如何接收本讲主机发出的 AR/AW/R/W/B。
- 回看 [testbench/psi_common_axi_master_simple_tb/](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axi_master_simple_tb/) 下的其余用例（`case_axi_hs`、`case_split`、`case_max_transact`），它们专门覆盖握手反压、跨 4KB 拆分、最大事务数等边界场景。
- 若要在一个真实 SoC 里用起来，建议结合 [u2-l4](u2-l4-axi-pkg.md) 的 record 类型，把本组件的扁平 AXI 端口在顶层打包成 `rec_axi_ms`/`rec_axi_sm`，简化连线。
