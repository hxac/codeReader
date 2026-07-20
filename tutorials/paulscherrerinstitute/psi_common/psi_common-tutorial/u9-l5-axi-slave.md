# AXI 从机 axi_slave_ipif 与 axilite_slave_ipif

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 psi_common 的 AXI 从机如何把一段 AXI 地址空间切分成「寄存器区 + 存储区」，并推导出存储区起始地址。
- 看懂面向用户的「寄存器接口」与「存储接口」两组信号（读脉冲、写脉冲、写数据、字节使能、读数据），并理解为何读回写值需要用户自己回环。
- 区分 32 位（`axi_slave_ipif`）、64 位（`axi_slave_ipif64`）与 AXI-Lite（`axilite_slave_ipif`）三个变体的差异与各自的选型理由。
- 解释从机如何用一个 5 态 FSM 串行处理读/写事务，以及为何在 R（读数据）通道插入一级 `psi_common_pl_stage` 来实现符合 AXI 规范的「节流 / 反压」（throttling）。
- 自己实例化 `axilite_slave_ipif` 暴露 4 个寄存器，并预测其读写时序。

## 2. 前置知识

本讲是 **u9 接口单元**的收尾篇，承接 u9-l3（`axi_master_simple`）与 u9-l4（`axi_master_full`）。阅读前请确认你已理解：

- **AXI 五通道**：读地址（AR）、读数据（R）、写地址（AW）、写数据（W）、写响应（B）；每个通道都是独立的 `valid`/`ready` 握手（见 u1-l4 的 VLD/RDY 语义）。
- **方向命名约定**：`ms` = Master→Slave，`sm` = Slave→Master；本讲的组件是**从机**，因此 AR/AW/W 通道的 `valid`/`addr`/`data` 由主机送进来，`ready` 由本组件送出；R/B 通道反过来（见 u2-l4）。
- **二进程 record 设计法**与 `psi_common_pl_stage`（u7-l1）：本讲的「节流」机制直接例化了 `pl_stage`。
- **`t_aslv32` / `t_aslv64` 数组类型**（u2-l3）：寄存器接口用它们存放「一组寄存器字」。

补充两个本讲要用到的术语：

- **IPIF（IP Interface）**：Xilinx 早期为「把用户 IP 核挂到 AXI/PLB 总线」而提出的一组标准化用户侧信号（寄存器读写脉冲、存储端口）。psi_common 借用了这个名字，但实现是纯 VHDL、厂商无关的。
- **突发（burst）**：一次 AR/AW 握手之后连续传多拍数据。`arlen`/`awlen` 携带「拍数 − 1」；AXI-Lite 不支持突发，每拍数据都带一次地址握手。

> ⚠️ 一句话提醒：本讲的从机组件本身**端口是扁平信号**（`s_axi_araddr`、`s_axi_awvalid`……），并不使用 u2-l4 里 `psi_common_axi_pkg` 的 record。那个 record 是给**顶层集成者**在边界上打包用的——这一点和 u9-l3/u9-l4 的主机完全一致，本讲末尾会再点一次。

## 3. 本讲源码地图

| 文件 | 角色 |
|:--|:--|
| [hdl/psi_common_axi_slave_ipif.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd) | **32 位 AXI4 全功能从机**，本讲的主线（IPIF 映射模型、FSM、节流都在这里）。 |
| [hdl/psi_common_axi_slave_ipif64.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif64.vhd) | **64 位变体**，结构与 32 位几乎一致，差异集中在位宽常量与字节使能宽度。 |
| [hdl/psi_common_axilite_slave_ipif.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axilite_slave_ipif.vhd) | **AXI-Lite 包装器**，纯结构（`struct`），把全功能从机的 AXI4 信号钉死成单拍 Lite 形态。 |
| [hdl/psi_common_axi_pkg.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd) | record 类型包，仅供集成者在顶层把扁平信号收拢成 `rec_axi_ms`/`rec_axi_sm`（u2-l4）。 |
| testbench/psi_common_axilite_slave_ipif_tb/psi_common_axilite_slave_ipif_tb.vhd | AXI-Lite 从机的自校验测试平台，是本讲代码实践的参照样板。 |

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：**IPIF 映射模型 → 寄存器/存储接口 → 32/64 位与 Lite 变体 → AXI FSM 与 R 通道节流**。

---

### 4.1 IPIF 映射模型：地址空间如何切分

#### 4.1.1 概念说明

一个 IP 核通常有两类「软件可见」的资源：

1. **控制/状态寄存器**：少量 32 位（或 64 位）字，软件读它知状态、写它下命令。
2. **存储区**：一块较大的 RAM（如 BRAM），软件批量读写采样数据、波形、查找表。

`psi_common_axi_slave_ipif` 把这两类资源拼成**一段连续的 AXI 地址空间**：低地址放寄存器，寄存器区结束后立刻接存储区。组件对软件屏蔽了「这是寄存器还是存储」——它根据地址自动把访问路由到 `o_reg_*` 端口或 `o_mem_*` 端口。这就是「IPIF 映射模型」。

#### 4.1.2 核心流程

设寄存器数为 \(N\)（即 `num_reg_g`），每个寄存器宽 \(B\) 字节（32 位时 \(B=4\)，64 位时 \(B=8\)）。地址空间切分如下：

\[
\text{REG\_ADDR\_INDEX\_LOW} = \log_2 B \quad(\text{32 位}=2,\ \text{64 位}=3)
\]

\[
\text{REG\_ADDR\_WIDTH} = \lceil\log_2 N\rceil + \text{REG\_ADDR\_INDEX\_LOW}
\]

\[
\text{MEM\_ADDR\_START} = 2^{\text{REG\_ADDR\_WIDTH}}
\]

- 寄存器区：AXI 地址 \(0\) 到 \(N\cdot B - 1\)，每个寄存器占 \(B\) 字节。
- 存储区：从 `MEM_ADDR_START` 开始，向上一路延伸到地址空间顶端。
- 取寄存器号：丢掉地址低 `REG_ADDR_INDEX_LOW` 位（字内字节偏移），取接下来 \(\lceil\log_2 N\rceil\) 位。

用伪代码描述地址译码：

```text
if addr >= N*B then          -- 落在存储区
    路由到 o_mem_*（存储地址 = addr - MEM_ADDR_START）
else                          -- 落在寄存器区
    路由到 o_reg_*（寄存器号 = addr[REG_ADDR_WIDTH-1 : REG_ADDR_INDEX_LOW]）
end if
```

以 `num_reg_g = 8`、32 位为例：\(\lceil\log_2 8\rceil=3\)，`REG_ADDR_WIDTH = 5`，`MEM_ADDR_START = 2^5 = 32 = 0x20`。寄存器在 `0x00, 0x04, …, 0x1C`，存储区从 `0x20` 开始——与官方文档的例子完全吻合。

#### 4.1.3 源码精读

四个关键常量集中声明在 32 位从机的架构头：

[hdl/psi_common_axi_slave_ipif.vhd:107-114](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L107-L114) — 定义字内偏移位数、寄存器地址宽度、存储区起始地址与四种 RRESP/BRESP 编码（OKAY=00、EXOKAY=01、SLVERR=10、DECERR=11）。

地址译码只用一行组合逻辑分别判读/写：

[hdl/psi_common_axi_slave_ipif.vhd:323](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L323) — 读地址 `axi_raddr_sel`：若 FSM 正在读且地址中「REG_ADDR_WIDTH 以上」的位不全为 0，则判定为存储区访问。写侧 `axi_waddr_sel` 的写法完全对称（[L489](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L489)）。

存储端口地址会减去偏移，让用户拿到「从 0 开始」的本地地址：

[hdl/psi_common_axi_slave_ipif.vhd:615](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L615) — `o_mem_addr <= axi_xxxaddr - MEM_ADDR_START`（按当前是读还是写选 `araddr`/`awaddr`）。所以访问 AXI 地址 `0x24`（8 寄存器、存储区起点 `0x20`）时，用户存储端口看到的是 `0x04`。

当 `use_mem_g = false` 却访问了存储区，组件回 `DECERR`：

[hdl/psi_common_axi_slave_ipif.vhd:288-290](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L288-L290) — 读路径在地址超出 `num_reg_g*4` 且未启用存储时把 `axi_rresp` 置为 `RESP_DECERR_c`；写路径在 [L457-459](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L457-L459) 把 `axi_bresp` 置为 `DECERR`。

> 📌 **源码阅读小贴士（断言文本与实际条件相反）**：[hdl/psi_common_axi_slave_ipif.vhd:164](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L164) 的 `assert not (not use_mem_g and num_reg_g = 0)` 实际拦截的是「**既不启用存储、寄存器数又为 0**」的空配置；但其 `report` 字符串写的是 *"num_reg_g must be > 0 if use_mem_g = true"*，与真实条件相反。阅读时以代码条件为准。此外 [L163](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L163) 用 `isLog2` 强制 `num_reg_g` 必须是 2 的幂（因为寄存器号位宽靠 `log2ceil` 推导，非 2 的幂会浪费/错位）。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：验证你对地址映射公式的理解。
2. **步骤**：在 [hdl/psi_common_axi_slave_ipif.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd) 中找到 [L107-L110](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L107-L110) 的四个常量；分别令 `num_reg_g = 4` 与 `num_reg_g = 16`，手算 `REG_ADDR_WIDTH` 与 `MEM_ADDR_START`。
3. **观察**：对照官方文档 `doc/files/psi_common_axi_slave_ipif.md` 中「4 个寄存器 → 存储区从 0x10 起」的说法。
4. **预期结果**：
   - `num_reg_g = 4`：`REG_ADDR_WIDTH = 2 + 2 = 4`，`MEM_ADDR_START = 0x10`。✓
   - `num_reg_g = 16`：`REG_ADDR_WIDTH = 4 + 2 = 6`，`MEM_ADDR_START = 0x40`。✓
5. 运行结果为手算，无需仿真；若要复核可写一个最小 TB 用 `axi_master_simple` 对这些地址做单拍读，**待本地验证**。

#### 4.1.5 小练习与答案

- **练习 1**：若把 `num_reg_g` 设成 6（非 2 的幂），综合期会发生什么？
  - **答案**：[L163](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L163) 的 `assert isLog2(num_reg_g)` 会打印 `###ERROR###` 报告（severity error）。应改成 8（向上取到 2 的幂）。

- **练习 2**：32 位从机里，AXI 地址 `0x08` 命中第几个寄存器？
  - **答案**：寄存器号 = `addr[REG_ADDR_WIDTH-1 : 2]` = `addr[4:2]` = `"010"` = 2，即第 2 号寄存器。

---

### 4.2 寄存器 / 存储用户接口

#### 4.2.1 概念说明

地址译码之后，访问要落到具体的用户端口。本组件暴露两组完全解耦的接口：

- **寄存器接口**：每个寄存器各一根**读脉冲**（`o_reg_rd`）、一根**写脉冲**（`o_reg_wr`），外加一组用户回送的当前值（`i_reg_rdata`）和组件写下的新值（`o_reg_wdata`）。
- **存储接口**：地址（`o_mem_addr`，已减偏移）、字节写使能（`o_mem_wr`）、写数据（`o_mem_wdata`）、用户回送的读数据（`i_mem_rdata`）。

最关键的一条**设计约定**：组件**不自动**把写入寄存器的值回读出来。读写寄存器是两套独立通路——`o_reg_wdata` 只在「写」时更新，`i_reg_rdata` 完全由用户驱动。若软件要「写一个寄存器再读回确认」，**用户逻辑必须自己把 `o_reg_wdata` 回环接到 `i_reg_rdata`**。

#### 4.2.2 核心流程

**寄存器写**（一拍）：

```text
AW/W 握手完成 → axi_waddr_sel=0（寄存器区）& axi_wready=1
             → o_reg_wr(regNr) 拉高一拍（写脉冲）
             → o_reg_wdata(regNr) 按 s_axi_wstrb 字节使能更新对应字节
```

**寄存器读**（一拍，再打一拍出数据）：

```text
AR 握手 → axi_raddr_sel=0（寄存器区）& axi_rready=1
       → o_reg_rd(regNr) 拉高一拍（读脉冲，可用于 FIFO 出队等应答）
       → 同一时钟沿把 i_reg_rdata(regNr) 采样进 reg_rdata
       → 经 R 通道流水线送到 s_axi_rdata
```

**存储读**的硬约束：用户必须在 `o_mem_addr` 有效后的**恰好一个时钟周期**内把 `i_mem_rdata` 准备好（即必须挂同步 RAM，不能额外插流水线）。这是该组件最主要的局限。

#### 4.2.3 源码精读

寄存器读脉冲——每个寄存器一拍，由 `reg_rvalid` 触发，用 `axi_araddr_last` 取寄存器号：

[hdl/psi_common_axi_slave_ipif.vhd:559-573](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L559-L573) — 复位清零；否则默认全 0，仅当 `reg_rvalid='1'` 时把对应位拉高（实现一拍脉冲）。

寄存器读数据——在 `reg_rvalid` 那拍把用户送来的 `i_reg_rdata` 选中的字寄存一拍。为防止越界，用一个 `num_reg_g+2` 长度的扩展数组 `rd_data_ext` 兜底：

[hdl/psi_common_axi_slave_ipif.vhd:547-557](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L547-L557) — 寄存器号取自 `axi_araddr[REG_ADDR_INDEX_HIGH : REG_ADDR_INDEX_LOW]`。

寄存器写脉冲与写字节合并——写脉冲逻辑与读对称；写字节按 `s_axi_wstrb` 逐字节合并，复位时写入 `rst_val_g`：

[hdl/psi_common_axi_slave_ipif.vhd:594-608](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L594-L608) — 复位分支先把整组清零，再用 `o_reg_wdata(rst_val_g'range) <= rst_val_g` **只覆盖数组前 `rst_val_g'length` 个字**；所以 `rst_val_g` 数组长度不必等于 `num_reg_g`，超出部分寄存器复位为 0。正常运行分支用 `for reg_byte_index in 0 to 3` 按 `wstrb` 字节使能合并写入。

读数据多路选择——寄存器读与存储读二选一送入 R 通道流水线：

[hdl/psi_common_axi_slave_ipif.vhd:538-540](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L538-L540) — `reg_rvalid` 优先，其次 `mem_rvalid`（且 `use_mem_g`），否则送 0。

存储读写端口——只在 `use_mem_g=true` 时 `generate` 出来，否则全置 0：

[hdl/psi_common_axi_slave_ipif.vhd:614-626](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L614-L626) — `o_mem_wr` 四位分别对应四个字节，每位的条件都包含 `s_axi_wstrb(k)='1'`，实现字节级写使能。

#### 4.2.4 代码实践（源码阅读型 + 跟踪调用链）

1. **目标**：理解「写后读回」为何需要用户回环。
2. **步骤**：打开 [testbench/psi_common_axilite_slave_ipif_tb/psi_common_axilite_slave_ipif_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axilite_slave_ipif_tb/psi_common_axilite_slave_ipif_tb.vhd)，定位到 IP 侧进程的 Case 1（约 [L273-L279](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axilite_slave_ipif_tb/psi_common_axilite_slave_ipif_tb.vhd#L273-L279)）。
3. **观察**：TB 先 `i_reg_rdata(1) <= X"66665555"`（**手动**给读值），再等待 `o_reg_wr(1)='1'`（写脉冲），再等 `o_reg_rd(1)='1'`（读脉冲）。注意 TB 是**主动驱动 `i_reg_rdata`**，而不是依赖组件回读 `o_reg_wdata`。
4. **预期结果**：你会看到「读到的值」由 TB 经 `i_reg_rdata` 决定，与「写入的值 `o_reg_wdata(1)`」是两条独立路径。这正说明：**要做写后读回，用户必须把 `o_reg_wdata` 接到 `i_reg_rdata`**。
5. 运行该 TB 的命令登记在 `sim/config.tcl`（`create_tb_run "psi_common_axilite_slave_ipif_tb"`，见 u1-l3），**待本地验证**。

#### 4.2.5 小练习与答案

- **练习 1**：若软件写寄存器 3 时只想改最高字节，`wstrb` 应该是多少？`o_reg_wdata(3)` 的其余字节会怎样？
  - **答案**：32 位字有 4 字节，`wstrb` 高位对应数据高位字节，故 `wstrb = "1000"`。[L601-L605](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L601-L605) 只更新 `wstrb='1'` 的字节，其余字节**保持原值不变**（寄存器性质）。

- **练习 2**：为什么存储读必须「恰好一拍延迟」？如果用户的 BRAM 是两拍延迟会怎样？
  - **答案**：组件在 `o_mem_addr` 有效的下一拍直接采样 `i_mem_rdata`（[L539](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L539)），没有对齐缓冲。两拍延迟的 RAM 会让数据错位一拍；需在用户侧先做一级流水对齐，或换用支持可变延迟的从机。

---

### 4.3 32 / 64 位变体与 AXI-Lite 包装器

#### 4.3.1 概念说明

三个变体的关系一目了然：

| 变体 | 数据位宽 | 突发 | 实现方式 | 典型用途 |
|:--|:--|:--|:--|:--|
| `axi_slave_ipif` | 固定 32 位 | 支持 | 独立 behav | 32 位 AXI4 寄存器/存储映射 |
| `axi_slave_ipif64` | 64 位（generic） | 支持 | 独立 behav（几乎复制粘贴 32 位） | 64 位数据通路 IP |
| `axilite_slave_ipif` | 32 位 | **不支持** | **纯结构包装器**，内部例化 32 位从机 | 轻量控制平面（PS↔PL 寄存器） |

`axilite_slave_ipif` 是最省心的选择：它把 AXI4 全功能从机那些「Lite 用不到」的信号（ID、`arlen`、`arsize`、`wlast`……）**钉死成单拍常量**，对外只暴露 AXI-Lite 子集。这正是它的全部实现——一个 `port map`。

#### 4.3.2 核心流程

AXI-Lite 相对 AXI4 的简化：

```text
arlen/awlen = X"00"   -- 每次只传 1 拍（无突发）
arsize/awsize = "010" -- 每拍 4 字节
arburst/awburst = "01"-- incremental（单拍时无实质影响）
arid/awid = "0"       -- 不用 ID
wlast = '1'           -- 永远是最后一拍
```

把这些常量接到 32 位从机的对应端口，就完成了「AXI4 → AXI-Lite」的降级。64 位从机则没有 Lite 包装器——需要 Lite 的场合几乎都用 32 位。

64 位变体与 32 位的差异则集中在「每字节数」与「地址对齐」：

- `REG_ADDR_INDEX_LOW` 从 2 改成 3（每字 8 字节 = \(2^3\)）。
- `arsize`/`awsize` 多支持一个编码 `"011"`（8 字节）。
- 字节使能宽度从固定的 4 改成 generic `axi_byte_width_g`（默认 8），相关 `for` 循环用 `0 to axi_byte_width_g-1`。
- 存储区判定阈值从 `num_reg_g*4` 改成 `num_reg_g*8`。
- 默认 `axi_addr_width_g` 从 8 改成 9（因为同样寄存器数下，64 位每字占的地址位更多）。

#### 4.3.3 源码精读

**AXI-Lite 包装器**——整段就是一个 `port map`，关键在那些被钉死的常量：

[hdl/psi_common_axilite_slave_ipif.vhd:73-90](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axilite_slave_ipif.vhd#L73-L90) — 例化 `psi_common_axi_slave_ipif`，把 `axi_id_width_g=>1`、`arlen=>X"00"`、`arsize=>"010"`、`arburst=>"01"` 等钉死。

[hdl/psi_common_axilite_slave_ipif.vhd:112](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axilite_slave_ipif.vhd#L112) — `s_axi_wlast => '1'`：Lite 每拍都是最后一拍。

> 注意 [hdl/psi_common_axilite_slave_ipif.vhd:1-5](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axilite_slave_ipif.vhd#L1-L5)：这个文件由 Enclustra GmbH 捐赠（2020），与 PSI 的全功能从机（2019）同源，所以两者用户侧接口完全一致，只是 Lite 版去掉了 ID 与突发。

**64 位变体**的差异点：

[hdl/psi_common_axi_slave_ipif64.vhd:32-35](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif64.vhd#L32-L35) — 新增 `axi_data_width_g := 64` 与 `axi_byte_width_g := 64/8` 两个 generic，使数据/字节宽度可参数化（虽然实际只用于 64）。

[hdl/psi_common_axi_slave_ipif64.vhd:114-117](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif64.vhd#L114-L117) — `REG_ADDR_INDEX_LOW := 3`，存储区起点相应变大。

[hdl/psi_common_axi_slave_ipif64.vhd:236-237](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif64.vhd#L236-L237) — `arsize "011"` 解码为 8 字节步长（32 位版没有这个分支）。

[hdl/psi_common_axi_slave_ipif64.vhd:627-631](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif64.vhd#L627-L631) 与 [L642-L644](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif64.vhd#L642-L644) — 字节循环与存储写使能都用 `0 to axi_byte_width_g-1` 的 `for`/`generate`，泛化成 8 字节。

#### 4.3.4 代码实践（对比阅读型）

1. **目标**：用 `diff` 思维量化 32 位与 64 位的差异。
2. **步骤**：并排打开 `axi_slave_ipif.vhd` 与 `axi_slave_ipif64.vhd`，搜索 `REG_ADDR_INDEX_LOW`、`axi_byte_width_g`、`num_reg_g * 4`（vs `* 8`）、`arsize` 的 case 分支。
3. **观察**：除位宽常量与字节循环上界外，FSM、地址译码、寄存器读写脉冲、R 通道流水线的结构**逐行对应**——64 位版几乎是 32 位版的「批量替换」。
4. **预期结果**：你会得出结论——两者的「行为模型」完全相同，只是字宽从 4 字节变 8 字节，所以选型只看「IP 核内部数据通路是 32 还是 64 位」。
5. 无需运行，纯源码对比。

#### 4.3.5 小练习与答案

- **练习 1**：`axilite_slave_ipif` 把 `arlen` 钉成 `X"00"`，那主机如果发起 4 拍突发读会怎样？
  - **答案**：从机的 [L328 ARREADY](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L328) 只在 idle→rd_data 转换的那一拍为高，且内部 `axi_arlen` 被赋成 `X"00"`，`axi_rlast` 在 `arlen=0` 时就拉高（[L371](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L371)）。即第一拍就回 `rlast`，相当于只服务第一拍、忽略后续——Lite 本就不允许突发，主机不该这么做。

- **练习 2**：64 位从机里，AXI 地址 `0x08` 命中第几个寄存器？
  - **答案**：`REG_ADDR_INDEX_LOW=3`，寄存器号 = `addr[?:3]`。`0x08` = 二进制 `...1000`，低 3 位是字节偏移，故 `addr>>3 = 1`，命中第 1 号寄存器。

---

### 4.4 AXI FSM 与 R 通道节流（throttling）

#### 4.4.1 概念说明

本组件用一个**5 态有限状态机**串行处理事务：同一时刻**只服务一个读或写**，不做并发、不流水化多个 outstanding 事务（这点与支持 outstanding 的主机 u9-l3 形成对比）。状态流转：

```text
idle ──AR──▶ rd_data ──(rlast & rready)──▶ idle
  │
  └──AW──▶ wr_data ──(wlast & wvalid)──▶ wr_resp_delay ──▶ wr_done ──(bready)──▶ idle
```

关于「throttling（节流）」：这里**没有**名为 `axi_throttling` 的 generic（不要被本讲主题里的词误导）。从机侧的「节流」指的是——**主机可以在 R 通道临时不接收数据（反压）**，从机必须能合规地暂存并继续工作。本组件通过在 R 通道插入一级 `psi_common_pl_stage`（`use_rdy_g=>true`）来吃掉主机的反压，这正是 u7-l1 影子寄存器机制的标准用法。

#### 4.4.2 核心流程

**FSM 组合次态**（关键转换条件）：

- `idle → rd_data`：`s_axi_arvalid='1'`（读优先于写，`if arvalid elsif awvalid`）。
- `idle → wr_data`：`s_axi_awvalid='1'`。
- `rd_data → idle`：内部 `axi_rlast='1'` 且 R 流水线 `rpl_rready='1'`（主机愿意收最后一拍）。
- `wr_data → wr_resp_delay`：内部 `axi_wlast='1'` 且 `s_axi_wvalid='1'`。
- `wr_resp_delay → wr_done`：无条件一拍（给寄存器/存储写生效留时间）。
- `wr_done → idle`：`s_axi_bready='1'`。

**R 通道节流**的关键思路：被移植的旧逻辑只在 `RREADY` 出现后才拉 `RVALID`——这违反 AXI 规范（`VALID` 不允许依赖对端的 `READY`）。修复办法是在 R 通路上插一个 `pl_stage`：它对内核侧**永远准备好**（`rpl_rready`），把数据收进影子寄存器，再按规范与主机做独立的 `vld/rdy` 握手。于是主机不论怎样反压，内核都能继续往前走一格。

#### 4.4.3 源码精读

FSM 组合进程（次态逻辑，注意读优先于写）：

[hdl/psi_common_axi_slave_ipif.vhd:169-200](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L169-L200) — 复位（`s_axi_aresetn='0'`）强制 `idle`；否则按当前状态与握手信号算次态。

ARREADY 只在「idle 即将进入 rd_data」的那一拍为高——这就是「一次只接一个 AR」的原因：

[hdl/psi_common_axi_slave_ipif.vhd:328](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L328) — `s_axi_arready <= '1' when (axi_fsm=idle) and (axi_fsm_comb=rd_data)`。AWREADY 写法对称（[L494](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L494)）。

写响应时序——`wr_resp_delay` 一拍 + `wr_done` 等 `bready`：

[hdl/psi_common_axi_slave_ipif.vhd:190-195](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L190-L195) — `bvalid` 在 `wr_done` 态拉高（[L533](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L533)），主机回 `bready` 后回 idle。

**R 通道节流的核心——插入 pl_stage**：

[hdl/psi_common_axi_slave_ipif.vhd:631-659](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L631-L659) — 把 `rdata/rresp/rlast/rid` 拼成一根宽位总线（`pl_in_data`），送进 `psi_common_pl_stage`（`use_rdy_g=>true, rst_pol_g=>'0'`）；输出端拆回 `s_axi_r*`。注释 [L631-L632](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L631-L632) 明确写了这么做的原因：旧逻辑违反 AXI 规范，用流水线解耦后「PL 级永远拉 READY」，问题得以解决。这就是从机处理「主机节流/反压」的全部机制。

> 📌 这段 `pl_stage` 同时带来一个副作用：R 通道数据相对内核的 `reg_rvalid`/`mem_rvalid` **多了一拍延迟**。但因为 `i_mem_rdata` 本就要求「一拍后有效」，两者叠加后读数据出现在总线上的总延迟需要结合波形核对（**待本地验证**）。

#### 4.4.4 代码实践（源码阅读型 + 时序预测）

1. **目标**：追踪一次 4 拍突发读在 FSM 与 R 通道里的完整流动。
2. **步骤**：
   - 在 [hdl/psi_common_axi_slave_ipif.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd) 中找到 ARADDR 进程 [L214-L318](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L214-L318)：注意它支持三种 burst（fixed/incremental/wrapping），incremental 时 `axi_araddr <= axi_araddr + axi_arsize`（[L297](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L297)），且每输出一拍 `axi_arlen` 自减（[L312](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L312)）。
   - 找到 `axi_rlast` 的产生（[L371](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L371)）：`arlen=0` 那拍才拉高。
3. **观察**：`arlen` 从 3（4 拍突发）逐拍减到 0，最后一拍 `rlast` 随之拉高，FSM 才回 idle。
4. **预期结果**：若主机在 R 通道持续反压（`s_axi_rready='0'`），`pl_stage` 的影子寄存器暂存当前拍，`rpl_rready` 也会被压低，进而 `axi_rready`、`mem_rvalid/reg_rvalid` 全部冻结——读地址指针停在 `axi_araddr_last`（[L309](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L309)），数据不丢。
5. 用 `axi_master_simple`（u9-l3）当主机、本从机当 DUT 搭一个最小 TB 验证上述反压行为，**待本地验证**。

#### 4.4.5 小练习与答案

- **练习 1**：为什么 FSM 里读优先于写（`if arvalid elsif awvalid`）？这会带来什么副作用？
  - **答案**：这是简化实现，保证状态机无歧义。副作用是：若主机持续发读，写会被无限延后（写饥饿）。该从机定位为「简单 IP 接口」，假设上层仲裁/软件不会这样滥用。

- **练习 2**：去掉 R 通道的 `pl_stage`（直接把 `rpl_r*` 接到 `s_axi_r*`）会违反什么？
  - **答案**：旧内核逻辑「先看 `RREADY` 再拉 `RVALID`」会让 `VALID` 依赖 `READY`，违反 AXI「`VALID` 一旦拉高、握手完成前不可因 `READY` 而变化」的规范（[L631-L632 注释](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_slave_ipif.vhd#L631-L632)）。某些 AXI 互联/校验器会报错或死锁。

---

## 5. 综合实践：用 axilite_slave_ipif 暴露 4 个可读回寄存器

把本讲四个模块串起来：实例化 AXI-Lite 从机、配置 4 个寄存器、**做写后读回环**，并预测读写时序。

**任务**：设计一个最小 IP 片段——4 个 32 位寄存器，软件可写、可读回；不使用存储区。

**示例代码**（**本片段非项目原有代码，仅为教学示例**，命名遵循库的 snake_case 规范）：

```vhdl
-- 教学示例：4 个可读回寄存器的最小封装
i_axilite : entity work.psi_common_axilite_slave_ipif
  generic map (
    num_reg_g        => 4,                 -- 4 个寄存器，是 2 的幂 ✓
    rst_val_g        => (X"00000001",      -- reg0 复位值
                         X"00000002",      -- reg1 复位值
                         X"00000000",      -- reg2
                         X"00000000"),     -- reg3（数组长度=4=num_reg_g）
    use_mem_g        => false,             -- 不用存储区
    axi_addr_width_g => 8                  -- 8 位地址空间足够（寄存器区仅占 0x00..0x0F）
  )
  port map (
    s_axilite_aclk    => clk_i,
    s_axilite_aresetn => rstn_i,
    -- AXI-Lite 五通道（AR/R/AW/W/B）按 u9-l3 的主机或 PS 的 M_AXI 接线
    s_axilite_araddr  => araddr,
    s_axilite_arvalid => arvalid,
    s_axilite_arready => arready,
    s_axilite_rdata   => rdata,
    s_axilite_rresp   => rresp,
    s_axilite_rvalid  => rvalid,
    s_axilite_rready  => rready,
    s_axilite_awaddr  => awaddr,
    s_axilite_awvalid => awvalid,
    s_axilite_awready => awready,
    s_axilite_wdata   => wdata,
    s_axilite_wstrb   => wstrb,
    s_axilite_wvalid  => wvalid,
    s_axilite_wready  => wready,
    s_axilite_bresp   => bresp,
    s_axilite_bvalid  => bvalid,
    s_axilite_bready  => bready,
    -- 寄存器接口
    o_reg_rd    => reg_rd,      -- 4 位读脉冲
    o_reg_wr    => reg_wr,      -- 4 位写脉冲
    o_reg_wdata => reg_wdata,   -- 4×32 写值
    i_reg_rdata => reg_wdata,   -- ★ 关键回环：读值 = 写值，实现写后读回
    -- 存储接口（use_mem_g=false 时仍需接线，内部会置 0）
    o_mem_addr  => open,
    o_mem_wr    => open,
    o_mem_wdata => open,
    i_mem_rdata => (others => '0')
  );
```

**预期读写时序（结合 4.2 与 4.4）**：

1. **写 reg1**：主机在 AW/W 完成握手 → FSM 进入 `wr_data`，`awlen` 自减到 0 触发内部 `wlast` → 经 `wr_resp_delay` 一拍 → `wr_done` 态 `bvalid` 拉高 → 主机回 `bready`，回 idle。写脉冲 `reg_wr(1)` 在握手那拍拉高一拍，`reg_wdata(1)` 更新。
2. **读 reg1**：主机 AR 握手 → FSM 进入 `rd_data`，`reg_rd(1)` 拉高一拍，同拍把 `i_reg_rdata(1)`（=回环的 `reg_wdata(1)`）采样进 `reg_rdata` → 经 R 通道 `pl_stage` 多一拍 → `s_axi_rvalid/rdata/rlast` 上送主机。
3. **读回值**：因为做了 `i_reg_rdata => reg_wdata` 回环，读到的就是此前写入的值；若不接这层回环，读到的将始终是 `i_reg_rdata` 的默认 0。

**验证步骤**：

1. 把上述片段放进一个顶层，用 `axi_master_simple`（u9-l3）或现成的 `axilite_slave_ipif_tb` 当激励。
2. 参考 [testbench/psi_common_axilite_slave_ipif_tb/psi_common_axilite_slave_ipif_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axilite_slave_ipif_tb/psi_common_axilite_slave_ipif_tb.vhd) 的 Case 1（[L273-L279](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axilite_slave_ipif_tb/psi_common_axilite_slave_ipif_tb.vhd#L273-L279)）：先写、再读、用 `StdlvCompareStdlv` 比较读回值。
3. 在 `sim/config.tcl` 里 `create_tb_run "psi_common_axilite_slave_ipif_tb"` 已登记，按 u1-l3 的 `run.tcl` 流程跑回归，出错会打印 `###ERROR###`。
4. 运行结果**待本地验证**。

## 6. 本讲小结

- `psi_common_axi_slave_ipif` 把 AXI 地址空间切成**低地址寄存器区 + 高地址存储区**，分界点 `MEM_ADDR_START = 2^(log2ceil(num_reg_g)+REG_ADDR_INDEX_LOW)`；`num_reg_g` 必须是 2 的幂。
- 用户接口分两组：**寄存器**（`o_reg_rd`/`o_reg_wr` 脉冲 + `o_reg_wdata`/`i_reg_rdata`）与**存储**（`o_mem_addr/wr/wdata` + `i_mem_rdata`，读数据必须一拍后有效）；**写后读回需要用户自己把 `o_reg_wdata` 回环到 `i_reg_rdata`**。
- 三个变体：32 位全功能（`axi_slave_ipif`）、64 位（`axi_slave_ipif64`，差异仅在字宽/字节使能/地址对齐）、AXI-Lite（`axilite_slave_ipif`，纯结构包装器，钉死 ID/len/size/last）。
- 一个 5 态 FSM **串行**处理读/写（读优先，无 outstanding）；所谓「throttling/节流」靠在 R 通道插入一级 `psi_common_pl_stage`（`use_rdy_g=>true`）吃主机反压，并修复旧逻辑「`VALID` 依赖 `READY`」的规范违规——并没有名为 `axi_throttling` 的 generic。
- 组件本身用**扁平** AXI 信号；u2-l4 的 `psi_common_axi_pkg` record 留给集成者在顶层边界收拢方向（`ms`/`sm`）。

## 7. 下一步学习建议

- **横向对照主机**：回到 u9-l3（`axi_master_simple`）与本讲的从机，比较「outstanding 事务 + throttling generic（主机）」与「串行 FSM + R 通道 pl_stage 节流（从机）」的设计差异，体会主从两侧对 AXI 规范的不同取舍。
- **动手联调**：用 `axi_master_simple`（主机）+ `axilite_slave_ipif`（从机）搭一个「PS 侧写寄存器、PL 侧读寄存器」的最小环回，跑通 `sim/config.tcl` 里已登记的两个 TB。
- **进入工程化单元**：u10（仲裁与杂项）会用到这里学的 AXI-S/AXI 握手与时序思路；u11（贡献与工具链）会讲如何为新增的 AXI 组件编写符合 `###ERROR###` 约定的自校验测试平台。
- **进阶阅读**：若需要支持可变存储延迟或并发 outstanding 的从机，可对比 Xilinx `axi_interconnect` / AXI Traffic Generator 的思路，理解本组件「简单但不流水化」的定位边界。
