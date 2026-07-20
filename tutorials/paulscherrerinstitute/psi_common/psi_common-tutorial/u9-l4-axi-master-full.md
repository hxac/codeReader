# AXI 完整主机 axi_master_full 与 axi_multi_pl_stage

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `psi_common_axi_master_full` 相对 `psi_common_axi_master_simple`（u9-l3）多做了什么，以及它**内部其实复用了 simple 主机**这一关键事实。
- 理解 full 主机如何把「按字节计的任意大小传输」拆解为若干 AXI 突发事务，并完成「非对齐 + 宽度不等」的数据对齐。
- 读懂 `psi_common_axi_multi_pl_stage`：一个把 5 条 AXI 通道各打包成一根宽位总线、再串多级流水线的纯结构包装器。
- 判断何时该在 AXI 长路径上插入 `axi_multi_pl_stage`，以及它如何帮助时序收敛。
- 厘清「扁平端口 vs record 端口」的取舍：为什么这两个组件本身坚持用扁平信号，而 `psi_common_axi_pkg` 的 record 留给集成者在边界上打包。

## 2. 前置知识

本讲建立在 u9-l3（`axi_master_simple`）之上，并复用前面多讲已建立的概念。开始前请确认你熟悉：

- **AXI4 五通道握手**（AR/R/AW/W/B），以及 valid/ready「同高一拍才传输」的规则（u1-l4、u9-l3）。
- **二进程 record 设计法**：`r`/`r_next` 表现态/次态，`p_comb` 算次态、`p_seq` 只打拍与复位（u7-l1）。
- **`psi_common_axi_pkg` 的 record 建模**：`rec_axi_ms`（Master→Slave）、`rec_axi_sm`（Slave→Master），`ms`/`sm` 描述的是信号流向而非角色（u2-l4）。
- **`psi_common_multi_pl_stage`**：用 `for generate` 串联 `stages_g` 个 `pl_stage`，每级把 ready 寄存一拍以打断 ready 长组合链，`use_rdy_g=>true` 时靠影子寄存器保证反压下不丢数据（u7-l2）。
- **`psi_common_wconv_n2xn`**：把 N 位窄字聚合成 n×N 位宽字（u8-l1）。

补充几个本讲会用到的术语：

- **beat（拍）**：AXI 一次突发传输中的一个数据周期。`arlen/awlen` 字段 = 拍数 − 1。
- **非对齐传输（unaligned transfer）**：用户给出的起始地址不是 AXI 数据宽度的整数倍。AXI 物理总线要求地址对齐到总线宽度，所以「非对齐」必须由主机用字节使能（strobe）和移位来「模拟」。
- **outstanding 事务**：已发地址、但尚未收到全部数据/响应的在途事务数。

## 3. 本讲源码地图

| 文件 | 作用 |
|:-----|:-----|
| [hdl/psi_common_axi_master_full.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_full.vhd) | 完整版 AXI 主机：支持非对齐传输、AXI 宽度大于用户宽度、按字节指定传输大小。内部例化 simple 主机 + FIFO + 宽度转换。 |
| [hdl/psi_common_axi_multi_pl_stage.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_multi_pl_stage.vhd) | AXI 接口的「多级流水线插入器」：5 条通道各串一个 `multi_pl_stage`。 |
| [hdl/psi_common_axi_pkg.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd) | AXI record 类型与默认常量（u2-l4 已详述，本讲从「集成端口」角度引用）。 |
| [testbench/psi_common_axi_master_full_tb/](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axi_master_full_tb/psi_common_axi_master_full_tb.vhd) | full 主机的自校验测试平台，含 simple_tf / axi_hs / user_hs / large 四个用例。 |
| [testbench/psi_common_axi_multi_pl_stage_tb/psi_common_axi_multi_pl_stage_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axi_multi_pl_stage_tb/psi_common_axi_multi_pl_stage_tb.vhd) | 多级流水线测试平台：一次 AXI 单拍写 + 单拍读回期望值。 |

## 4. 核心概念与源码讲解

### 4.1 full 主机特性：在 simple 之上增加了什么

#### 4.1.1 概念说明

`psi_common_axi_master_full`（作者 Oliver Bruendler）是本库功能最强的 AXI 主机。理解它的关键是**一句话：full 主机并不是从零重写，而是在 `axi_master_simple` 外面包了一层「地址对齐 + 宽度转换 + 字节计数」的前端逻辑**。这一点源码头部的描述与实体内的例化都能直接证明（见 4.1.3）。

full 主机相对 simple 主机多出三件事：

1. **非对齐与奇数字节传输**：simple 主机要求用户地址对齐到 AXI 总线宽度；full 主机允许任意字节地址，由自己用 strobe + 数据移位来对齐。
2. **AXI 数据宽度可以大于用户数据宽度**：例如 AXI 总线 32 位、用户侧 16 位，full 主机内部用 `wconv_n2xn` 自动聚合。
3. **传输大小按字节指定**：simple 主机的 `size` 是「拍数」，full 主机的 `size` 是「字节数」，这样才能表达奇数长度的传输。

这三项便利的代价是：**每条命令有几拍固定开销**（文档原话 "some clock cycles of overhead per command"），所以大传输很高效、极小传输（只有几拍突发）的性能被这层开销拉低。读写两条路径完全独立、可并发，且命令与数据之间无时序约束（写数据可以早于、晚于或随命令一起给）。

#### 4.1.2 核心流程

full 主机把一条「用户命令」翻译成「若干 AXI 事务」的整体流程：

```text
用户命令 (addr, size_bytes, low_lat)
        │
        ▼
[Command FSM] 算出对齐起始地址、AXI 拍数、首尾字节使能
        │   AxiWrCmd/AxiRdCmd (地址对齐、size=AXI字数)
        ▼
[宽度转换]  wr: sync_fifo → wconv_n2xn (用户宽→AXI宽)
        │   rd: AXI宽 → 对齐移位 → sync_fifo (用户宽)
        ▼
 psi_common_axi_master_simple  ←── 真正发 AXI 五通道握手
        │
        ▼
   AXI 物理总线 (m_axi_*)
```

关键派生关系（以写为例，读同理）：

- 用户给出字节数 `size_bytes`，用户侧字宽 `DataBytes = data_width_g/8`，AXI 总线字宽 `AxiBytes = axi_data_width_g/8`。
- 用户侧需要消费/生产的**用户字数**（向上取整）：

\[
\text{WrDataWordsCmd} = \left\lceil \frac{\text{size\_bytes}}{\text{DataBytes}} \right\rceil
\]

- AXI 侧需要传输的**AXI 字数**（用对齐后的首末地址差除以 AXI 字宽再加 1）：

\[
\text{AxiCmdSize} = \frac{\text{AlignedAddr}(\text{last}) - \text{AlignedAddr}(\text{first})}{\text{AxiBytes}} + 1
\]

最终 `m_axi_awlen = AxiCmdSize − 1`（由内部 simple 主机计算，与 u9-l3 一致）。

#### 4.1.3 源码精读

先看 generic 与断言，它把 full 主机的「约束」写得很清楚：

- [hdl/psi_common_axi_master_full.vhd:26-38](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_full.vhd#L26-L38)：generic 列表。注意 `data_width_g`（用户宽度）与 `axi_data_width_g`（总线宽度）是**两个独立参数**——这正是「宽度可不等」特性的入口；`impl_read_g`/`impl_write_g` 可关掉读或写以省资源。
- [hdl/psi_common_axi_master_full.vhd:227-229](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_full.vhd#L227-L229)：三条 `assert`，强制「两种宽度都是 8 的倍数」且 `axi_data_width_g mod data_width_g = 0`——即**AXI 宽度必须 ≥ 用户宽度且为整数倍**（不能反过来，文档也明确「The AXI interface can be wider than the data interface but not vice versa」）。

其次，内部例化 simple 主机是 full 主机最核心的复用关系：

- [hdl/psi_common_axi_master_full.vhd:578-654](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_full.vhd#L578-L654)：例化 `psi_common_axi_master_simple`。full 主机把算好的 `AxiWrCmd_Addr/AxiWrCmd_Size` 等喂给 simple 主机的命令接口，AXI 五通道端口直接「打穿」到顶层。**注意**：simple 主机的 `data_fifo_depth_g` 这里映射到 `axi_fifo_depth_g`（[L585](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_full.vhd#L585)），即 simple 内部那层 FIFO 用 AXI 宽度，而 full 主机自己在用户侧另挂了一层用户宽度的 FIFO。

源码头部描述里有一处**值得提醒的笔误**：[hdl/psi_common_axi_master_full.vhd:10-15](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_full.vhd#L10-L15) 写着 "In contrast to **psi_common_axi_master_full**, this entity can do unaligned transfers..."——这里显然应为 `psi_common_axi_master_simple`（自己跟自己对比没有意义）。读源码时按 simple 理解即可。

full 主机沿用了二进程 record 设计法，但状态机数量明显多于 simple（因为要管对齐与宽度转换）：

- [hdl/psi_common_axi_master_full.vhd:119-123](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_full.vhd#L119-L123)：定义了 5 个 FSM 类型——写命令、写宽度转换、写对齐（三态：Idle/Transfer/Last）、读命令（含 WaitDataFsm）、读数据。
- [hdl/psi_common_axi_master_full.vhd:139-191](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_full.vhd#L139-L191)：`two_process_r` record，把所有寄存器（含 `WrAlignReg`、`RdAlignReg` 这两个双倍宽度的对齐移位寄存器）收敛到一处，配合 [L234](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_full.vhd#L234) 的 `p_comb` 与 [L540](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_full.vhd#L540) 的 `p_seq`。

地址对齐靠一个编译期函数实现，它把地址的低位清零（对齐到 AXI 字宽）：

```vhdl
-- hdl/psi_common_axi_master_full.vhd:128-134
function AlignedAddr_f(Addr : in unsigned(axi_addr_width_g-1 downto 0))
return unsigned is
  variable Addr_v : unsigned(Addr'range) := (others => '0');
begin
  Addr_v(Addr'left downto log2(AxiBytes_c)) := Addr(Addr'left downto log2(AxiBytes_c));
  return Addr_v;
end function;
```

即只复制 `log2(AxiBytes_c)` 位以上的高位，低位置零。`WrLastBe`/`RdLastBe`/`RdFirstBe`（[L279-285](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_full.vhd#L279-L285)、[L433-447](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_full.vhd#L433-L447)）则用地址低位比较生成首尾字的字节使能掩码——这就是「非对齐」落在 strobe 上的实现。

> **源码阅读小提示**：读命令端口里有一个命名与方向不一致的「坑」——[hdl/psi_common_axi_master_full.vhd:51](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_full.vhd#L51) 的 `cmd_rd_size_o` 名字带 `_o` 后缀，实际方向却是 `in`（与同行的 `cmd_rd_addr_i` 方向相同）。按 u1-l4 的命名规范它应当叫 `cmd_rd_size_i`。读源码与连线时以 `in` 方向为准，不要被后缀误导。

#### 4.1.4 代码实践

**实践目标**：用源码追踪验证「full = 前端对齐 + simple 主机」这一结构判断。

**操作步骤**：

1. 打开 [hdl/psi_common_axi_master_full.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_full.vhd)。
2. 在文件中找到三处例化：`i_axi`（simple 主机，[L578](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_full.vhd#L578)）、`i_fifo_wr_data`（写 FIFO，[L661](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_full.vhd#L661)）、`i_wc_wr`（写宽度转换 `wconv_n2xn`，[L685](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_full.vhd#L685)）。
3. 顺着 `wr_dat_i → i_fifo_wr_data → i_wc_wr → AxiWrDat_Data → i_axi` 这条写数据通路画一张框图。

**需要观察的现象**：

- 写数据先入用户宽度的 `sync_fifo`，再经 `wconv_n2xn` 聚合成 AXI 宽度，最后才喂给 simple 主机的写数据口。
- 读通路则没有 `wconv`：因为读方向 AXI 宽度 ≥ 用户宽度，靠 `RdAlignReg` 的移位对齐直接降宽（[L506-529](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_full.vhd#L506-L529)），再入用户宽度 `sync_fifo`（[L708](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_full.vhd#L708)）。

**预期结果**：得到一张「用户侧 FIFO（用户宽）→ 宽度转换/对齐 → simple 主机内部 FIFO（AXI 宽）→ AXI 总线」的分层图，印证 full 在 simple 之外多出的正是「对齐 + 宽度转换 + 用户侧缓冲」三层。

#### 4.1.5 小练习与答案

**练习 1**：如果应用只需要「AXI 宽度 = 用户宽度」且地址天然对齐，应该选 full 还是 simple？为什么？
**答案**：选 simple。full 的绝大多数逻辑（5 个 FSM、`WrAlignReg`/`RdAlignReg` 双倍宽度移位、`wconv`）都是为「非对齐 + 宽度不等」服务的；不需要这些特性时，full 只会带来每命令数拍的开销和更多资源。文档也建议：仅需宽度转换时可考虑 `axi_master_simple` + 外接 `wconv_n2xn`。

**练习 2**：`axi_data_width_g=64`、`data_width_g=16` 是否合法？反过来呢？
**答案**：正向合法（64 是 16 的整数倍，且 64 ≥ 16）。反向（`axi_data_width_g=16`、`data_width_g=64`）非法——[L229](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_full.vhd#L229) 的 `assert axi_data_width_g mod data_width_g = 0` 会在仿真启动时以 `failure` 终止。

**练习 3**：为什么 full 主机在「AXI 宽度 > 用户宽度」时不推荐使用 low-latency 模式？
**答案**：宽度转换使实际带宽低于 AXI 满带宽（见 u8-l1 的「位宽×速率守恒」）。若再用 low-latency 立即发命令，总线会被一个未攒满的窄包占住、产生空洞、阻塞更久（u9-l3 的 throttling 语义）。文档明确建议此时用高延迟模式。

### 4.2 AXI 流水线插入：axi_multi_pl_stage 的结构

#### 4.2.1 概念说明

`psi_common_axi_multi_pl_stage`（由 Enclustra GmbH 的 Eduardo del Castillo 贡献，版权与 full 主机不同）解决的是另一个完全不同的问题：**在已有的 AXI 主从路径上「凭空」插入若干级寄存器**，用来改善时序，而**不改变 AXI 协议可见的行为**。

它面向的是 AXI **slave** 接口（实体注释 "multiple pipeline stages for an axi mm slave interface"），即插在被驱动方一侧。做法非常直白：AXI 五条通道（AW/W/B/AR/R）每条都串一个 `psi_common_multi_pl_stage`（u7-l2）。因为 `multi_pl_stage` 处理的是「一根 `dat` 总线 + vld/rdy 握手」，而 AXI 每条通道恰好就是「若干字段 + 一个 valid + 一个 ready」，所以核心技巧是**把一条通道的所有字段拼接（concatenate）成一根宽位总线，过完流水线后再切片（slice）还原**。

#### 4.2.2 核心流程

每条 AXI 通道的处理都遵循同一个套路：

```text
in_<通道各字段> ──(拼接)──► <通道>DataIn (一根宽位 slv)
                                   │
                                   ▼
                     psi_common_multi_pl_stage
                     (use_rdy_g=>true, stages_g 级)
                                   │
                                   ▼
                              <通道>DataOut
                                   │
                              ──(切片)──► out_<通道各字段>
```

五条通道的方向不同，拼接/切片的「源端口」也要跟着调：

- **AW / W / AR 通道**：数据从主机流向从机，故 `in_*` 是输入、`out_*` 是输出，`vld` 随数据同向流动，`rdy` 反向回流（`in_*ready` 是输出、`out_*ready` 是输入）。
- **B / R 通道**：数据从从机流向主机，方向相反，故这两个通道把 `out_*` 当源、`in_*` 当目的——`dat_i` 接 `out_*` 信号、`dat_o` 接 `in_*` 信号（见 [L185-190](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_multi_pl_stage.vhd#L185-L190) 与 [L231-234](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_multi_pl_stage.vhd#L231-L234)）。这正是 u2-l4「数据发往哪边、`valid` 跟数据走、`ready` 跟接收方走」约定的具体落地。

每级流水线给数据带来 1 拍延迟（无反压时），`stages_g` 级即 `stages_g` 拍；但 AXI 协议本身对「中间多几拍延迟」是透明的（只要握手规则不被破坏），所以插入它不会改变功能正确性，只改变时序。

#### 4.2.3 源码精读

实体接口很简洁，generic 只有 4 个：

- [hdl/psi_common_axi_multi_pl_stage.vhd:18-24](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_multi_pl_stage.vhd#L18-L24)：`addr_width_g`、`data_width_g`、`stages_g`、`rst_pol_g`。注意它**没有** AXI 的 `size/lock/cache/prot` 等字段宽度作 generic，而是用写死的常量。

通道字段宽度被钉死为常量（遵循 AXI4 规范的固定宽度）：

```vhdl
-- hdl/psi_common_axi_multi_pl_stage.vhd:113-118
constant LenWidth_c   : positive := 8;   -- awlen/arlen
constant SizeWidth_c  : positive := 3;   -- awsize/arsize
constant BurstWidth_c : positive := 2;   -- awburst/arburst
constant CacheWidth_c : positive := 4;   -- awcache/arcache
constant ProtWidth_c  : positive := 3;   -- awprot/arprot
constant RespWidth_c  : positive := 2;   -- bresp/rresp
```

以写地址通道为例，看完整的「拼接 → multi_pl_stage → 切片」三步：

- [hdl/psi_common_axi_multi_pl_stage.vhd:128](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_multi_pl_stage.vhd#L128)：`AwDataIn <= in_awaddr & in_awlen & in_awsize & in_awburst & in_awlock & in_awcache & in_awprot;` ——把整条 AW 通道的所有字段按固定顺序拼成一根总线。
- [hdl/psi_common_axi_multi_pl_stage.vhd:129-143](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_multi_pl_stage.vhd#L129-L143)：例化 `multi_pl_stage`，`width_g` 取所有字段宽度之和，`use_rdy_g => true`（必须——AXI 通道有反压），`stages_g` 直接透传。
- [hdl/psi_common_axi_multi_pl_stage.vhd:145-151](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_multi_pl_stage.vhd#L145-L151)：把 `AwDataOut` 按同样的位段切片还原成 `out_awprot/out_awcache/.../out_awaddr`。

其余四条通道结构完全对称：写数据通道 [L154-173](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_multi_pl_stage.vhd#L154-L173)（拼 `wdata & wstrb & wlast`）、写响应通道 [L176-190](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_multi_pl_stage.vhd#L176-L190)（只有 `bresp`，方向反向）、读地址通道 [L193-216](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_multi_pl_stage.vhd#L193-L216)、读数据通道 [L219-239](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_multi_pl_stage.vhd#L219-L239)（拼 `rdata & rresp & rlast`，方向反向）。

> **源码阅读小提示**：实体上方的描述注释 [hdl/psi_common_axi_multi_pl_stage.vhd:9-11](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_multi_pl_stage.vhd#L9-L11) 末尾 "It is based on" 戛然而止，是一处未写完的注释，不影响代码本身。

#### 4.2.4 代码实践

**实践目标**：跑通 `axi_multi_pl_stage_tb` 回归，并验证「插入 3 级流水线后功能等价」。

**操作步骤**：

1. 打开 [testbench/psi_common_axi_multi_pl_stage_tb/psi_common_axi_multi_pl_stage_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axi_multi_pl_stage_tb/psi_common_axi_multi_pl_stage_tb.vhd)，确认 generic：`addr_width_g=16`、`data_width_g=32`、`stages_g=3`（[L36-38](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axi_multi_pl_stage_tb/psi_common_axi_multi_pl_stage_tb.vhd#L36-L38)）。
2. 看 `p_master` 进程（[L197-209](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axi_multi_pl_stage_tb/psi_common_axi_multi_pl_stage_tb.vhd#L197-L209)）：先 `axi_single_write(16#1234#, 16#37654321#, ...)` 写一个字，再 `axi_single_expect(16#12AB#, 16#3456CDEF#, ...)` 期望从另一个地址读回固定值。
3. 对照 `p_slave` 进程（[L212-228](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axi_multi_pl_stage_tb/psi_common_axi_multi_pl_stage_tb.vhd#L212-L228)）：从机用 `axi_expect_aw/axi_expect_wd_single/axi_apply_bresp` 接住写、用 `axi_expect_ar/axi_apply_rresp_single` 应答读。
4. 按 u1-l3 的方式运行该 TB（在 `sim/config.tcl` 中它注册于 [L446-447](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L446-L447)）。

**需要观察的现象**：写命令发出后，约 3 拍后才在从机侧看到 `awvalid/wvalid`；读数据同理滞后。但最终写值 `0x37654321` 被正确接收、期望值 `0x3456CDEF` 被正确读回——功能与「不插流水线」完全等价。

**预期结果**：仿真通过（无 `###ERROR###`），证明插入了 3 级流水线的 AXI 路径在功能上透明。**若本地未配置 PsiSim/psi_tb 工作副本结构**，可退化为纯源码阅读：对照 master/slave 两进程的地址与数据，确认它们经流水线后仍一一对应。

#### 4.2.5 小练习与答案

**练习 1**：为什么写响应通道（B）和读数据通道（R）的 `multi_pl_stage` 例化里，`dat_i` 接的是 `out_*` 而不是 `in_*`？
**答案**：B/R 通道的数据流向是从机→主机，即从 `out_*`（靠从机一侧）流向 `in_*`（靠主机一侧）。按 u2-l4「数据发往哪边、数据字段就在那一边」的约定，源端是 `out_*`，所以 `dat_i <= out_*`、`dat_o => in_*`。

**练习 2**：若把某条通道的 `use_rdy_g` 改成 `false` 会怎样？
**答案**：该通道将退化为「朴素寄存器、不处理反压」（u7-l1 的 `use_rdy_g=false` 分支）。一旦下游在该通道上反压（如 `bready` 拉低），数据会被直接覆盖丢失，破坏 AXI 握手语义。所以本组件对所有 5 条通道都强制 `use_rdy_g => true`。

**练习 3**：`stages_g=0` 时这个组件还存在延迟吗？
**答案**：注意——本实体的 generic 声明是 `stages_g : positive`（[L22](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_multi_pl_stage.vhd#L22)），`positive` 不允许 0，所以最小是 1 级、至少 1 拍延迟。这与 u7-l2 介绍的 `multi_pl_stage`（其 `stages_g` 是 `natural`、可为 0）不同——AXI 版做了更保守的约束。

### 4.3 record 端口：扁平信号与 record 类型的取舍

#### 4.3.1 概念说明

学完 u2-l4 后，读者自然会问：既然 `psi_common_axi_pkg` 提供了 `rec_axi_ms`/`rec_axi_sm` 把整条 AXI 接口收敛成两个 record，那 `axi_master_full` 和 `axi_multi_pl_stage` 为什么不直接用 record 当端口？

答案是**有意为之的可移植性取舍**：

- **这两个组件的端口全部是扁平的 `std_logic_vector`/`std_logic`**（如 `m_axi_awaddr`、`m_axi_awvalid`、`in_awaddr`、`out_awvalid`……）。
- record 留给**集成者**在边界上打包。`psi_common_axi_pkg` 的角色就是「胶水」：在顶层把扁平信号聚合成 record，从而简化顶层连线的可读性。

为什么不直接在组件端口上用 record？因为 `psi_common_axi_pkg` 里的 record 大量依赖**无约束（unconstrained）record 字段**（u2-l4），而**并非所有综合器/仿真器都支持**。本库的回归测试就留下了直接证据：`axi_master_full_tb` 在 Vivado 下被显式跳过（见 4.3.3）。

#### 4.3.2 核心流程

集成时的两种典型打包方式：

```text
方式 A（RTL 顶层用 axi_pkg record 收拢扁平端口）:
   signal axi_mst_o : rec_axi_ms := C_AXI_MS_DEF;
   signal axi_mst_i : rec_axi_sm := C_AXI_SM_DEF;
   -- 组件端口仍是扁平的 m_axi_awaddr 等
   axi_mst_o.aw.addr <= m_axi_awaddr;   -- 逐字段搬运
   ...

方式 B（测试平台用 psi_tb_axi_pkg record 驱动扁平端口）:
   signal axi_ms_m : axi_ms_r(...);     -- 来自外部库 psi_tb
   in_awaddr => axi_ms_m.awaddr;        -- 见 multi_pl_stage_tb
```

方向命名上，无论哪种方式都遵循同一约定：`ms` = Master→Slave、`sm` = Slave→Master（描述流向，不描述角色），与 u2-l4、u9-l3 完全一致。

#### 4.3.3 源码精读

先看组件端口确实是扁平的：

- [hdl/psi_common_axi_master_full.vhd:68-103](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_full.vhd#L68-L103)：AXI 五通道全部展开成 `m_axi_awaddr / m_axi_awlen / ... / m_axi_rready` 等独立端口，没有任何 record。
- [hdl/psi_common_axi_multi_pl_stage.vhd:30-107](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_multi_pl_stage.vhd#L30-L107)：`in_*`/`out_*` 同样全部扁平。

再看 record 的「定义在本库、却不在组件端口上用」：

- [hdl/psi_common_axi_pkg.vhd:131-145](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L131-L145)：`rec_axi_ms`/`rec_axi_sm` 把五通道各做成子 record 再聚合（字段 `ar/dr/aw/dw/b`），u2-l4 已详述。
- [hdl/psi_common_axi_pkg.vhd:167-181](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L167-L181)：全零默认常量 `C_AXI_MS_DEF`/`C_AXI_SM_DEF`，供 record 信号初始化，避免仿真出现 'U'。

「为什么不在组件端口用 record」的最直接证据在回归脚本里：

- [sim/config.tcl:388-390](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L388-L390)：
  ```tcl
  create_tb_run "psi_common_axi_master_full_tb"
  #Vivado does not support unconstrained records as required by this TB
  tb_run_skip Vivado
  ```
  full 主机的 TB **使用了** record（来自 `psi_tb_axi_pkg`，见 TB 头 [L26](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axi_master_full_tb/psi_common_axi_master_full_tb.vhd#L26)），而 Vivado 不支持这种无约束 record，于是整条 TB 在 Vivado 下被跳过。组件本身坚持扁平端口，正是为了**不把这种工具链限制传染给所有下游使用者**。

测试平台里 record 实际驱动扁平端口的写法，可以看 multi_pl_stage_tb：

- [testbench/psi_common_axi_multi_pl_stage_tb/psi_common_axi_multi_pl_stage_tb.vhd:67-75](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axi_multi_pl_stage_tb/psi_common_axi_multi_pl_stage_tb.vhd#L67-L75)：声明 record 信号 `axi_ms_m`/`axi_ms_s`/`axi_sm_m`/`axi_sm_s`（类型来自外部 psi_tb 库的 `axi_ms_r`/`axi_sm_r`）。
- [testbench/psi_common_axi_multi_pl_stage_tb/psi_common_axi_multi_pl_stage_tb.vhd:90-152](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axi_multi_pl_stage_tb/psi_common_axi_multi_pl_stage_tb.vhd#L90-L152)：例化时把 `axi_ms_m.awaddr`、`axi_ms_m.awvalid` 等 record 字段逐个连到组件的扁平端口——这就是「record 在边界、扁平在组件」的活样本。

> 注意区分两套 record：`psi_common_axi_pkg`（本库，RTL 可综合）提供 `rec_axi_ms`/`rec_axi_sm`；`psi_tb_axi_pkg`（外部仿真库 psi_tb）提供 `axi_ms_r`/`axi_sm_r` 并附带 `axi_single_write` 等过程。后者只用于仿真，不要混入可综合 RTL。

#### 4.3.4 代码实践

**实践目标**：体会「扁平端口 + record 胶水」的连线工作量，并理解它的可移植性收益。

**操作步骤**：

1. 打开 [testbench/psi_common_axi_multi_pl_stage_tb/psi_common_axi_multi_pl_stage_tb.vhd:87-152](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axi_multi_pl_stage_tb/psi_common_axi_multi_pl_stage_tb.vhd#L87-L152)。
2. 统计 port map 里有多少行是在做「record 字段 → 扁平端口」的一对一搬运（AW/W/B/AR/R 五通道共数十行）。
3. 设想：如果组件端口本身就是一个 `rec_axi_ms`，这几十行会塌缩成约 5 行（每通道一行）。

**需要观察的现象**：扁平端口让 port map 冗长但**对任何综合器都安全**；record 能极大简化连线，但引入工具链依赖。

**预期结果**：理解本库的设计取向——**组件保持最大兼容性（扁平），把 record 的便利留给集成者按自己的工具链选择**。

#### 4.3.5 小练习与答案

**练习 1**：在 RTL 顶层，想把 full 主机的扁平 AXI 端口接成 `rec_axi_ms` record 信号，应该用 `psi_common_axi_pkg` 还是 `psi_tb_axi_pkg`？
**答案**：用 `psi_common_axi_pkg`（提供 `rec_axi_ms`/`rec_axi_sm` 与默认常量 `C_AXI_MS_DEF`/`C_AXI_SM_DEF`，可综合）。`psi_tb_axi_pkg` 只用于仿真。

**练习 2**：`axi_master_full_tb` 为什么在 Vivado 下被 `tb_run_skip`？
**答案**：因为该 TB 使用了无约束 record（`psi_tb_axi_pkg` 的 `axi_ms_r`/`axi_sm_r`），而 Vivado 不支持这类无约束 record，故在回归中显式跳过（[config.tcl:389-390](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L389-L390)）。这也反向解释了组件本身为何坚持扁平端口。

**练习 3**：`psi_common_axi_pkg` 的宽度常量（`C_S_AXI_DATA_WIDTH=32` 等）是 generic 化的吗？能否直接用它给 full 主机接 64 位 record？
**答案**：不能。如 u2-l4 所述，`axi_pkg` 的宽度常量是**写死**的（ID=1、DATA=32、ADDR=32），非 generic。full 主机的 `axi_data_width_g` 可为 64，但 record 的字段宽度固定为 32，二者不匹配。需要 64 位 record 时不能直接套用 `axi_pkg`。

### 4.4 时序收敛：为什么要在 AXI 路径上插流水线

#### 4.4.1 概念说明

把 `axi_multi_pl_stage` 放在 4.4 单独讲，是因为它的**全部存在意义就是时序收敛（timing closure）**。

AXI 是「valid/ready 双向握手」的总线，初学者容易忽略一个隐患：**ready 信号是一条贯穿多级的组合逻辑链**。考虑一个主机经过长布线连到远端的从机——如果从机直接用组合逻辑产生 `ready`（例如 `ready = not fifo_full and ...`），那么 `ready` 会一路组合传播回主机，与数据路径上的布线延迟叠加，极易成为关键路径，拖低可达到的最高时钟频率。

这正是 u7-l1/pl_stage 引入「把 ready 寄存一拍」的动机：把一条长的 ready 组合链**切成若干短段**。代价是反压延迟一拍，故 pl_stage 用影子寄存器 `DataShad` 兜底，保证反压下不丢数据（u7-l1、u7-l2）。

`axi_multi_pl_stage` 把这件事**一次性应用到整条 AXI 接口的 5 条通道**上：每条通道串 `stages_g` 级 `pl_stage`，每级都寄存 ready，于是从机侧的 ready 不再长距离组合回传，而是逐级打拍。对于「主机和从机物理上离得很远」（例如 SoC FPGA 中 PS 端 AXI 跨半个芯片连到 PL 端某 IP）的场景，这是收时序的利器。

#### 4.4.2 核心流程

时序收敛的收益可以粗略量化。设原始 ready 组合链深度为 \(D\)（门级）、布线延迟为 \(T_{wire}\)、目标周期为 \(T_{clk}\)。若 \(D + T_{wire} > T_{clk}\) 即违例。插入 \(N\) 级流水线后，每段 ready 逻辑深度约为：

\[
D_{seg} \approx \frac{D}{N} + D_{\text{reg}}
\]

其中 \(D_{\text{reg}}\) 是一级寄存器 + 影子寄存器选择逻辑的固定开销。只要 \(D_{seg} + T_{wire,seg} < T_{clk}\)，违例即消除。代价是 ready 路径多了 \(N\) 拍延迟（数据路径也多 \(N\) 拍），但 AXI 协议对此透明。

整体效果一览：

| 维度 | 不插流水线 | 插入 `axi_multi_pl_stage`（N 级） |
|:-----|:-----------|:----------------------------------|
| ready 组合链 | 一条长链（主机↔从机全程） | 被切成 N 段短逻辑 |
| 关键路径 | 易违例（长布线 + 组合） | 显著缩短，利于高频 |
| 数据延迟 | 0 | +N 拍 |
| 反压下丢数据？ | — | 否（`use_rdy_g=>true`，影子寄存器兜底） |
| AXI 功能可见行为 | — | 不变（协议透明） |

#### 4.4.3 源码精读

`use_rdy_g => true` 是时序收敛能成立的前提——它让每条通道都启用带影子寄存器的完整握手：

- [hdl/psi_common_axi_multi_pl_stage.vhd:129-134](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_multi_pl_stage.vhd#L129-L134)：AW 通道例化，`use_rdy_g => true`、`stages_g => stages_g`。其余四条通道的例化（[L155-160](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_multi_pl_stage.vhd#L155-L160)、[L176-181](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_multi_pl_stage.vhd#L176-L181)、[L194-199](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_multi_pl_stage.vhd#L194-L199)、[L220-225](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_multi_pl_stage.vhd#L220-L225)）参数完全一致。

影子寄存器与 ready 寄存的实现不在本文件，而在它例化的 `multi_pl_stage` → `pl_stage` 中（u7-l1、u7-l2 已精读）。本组件的价值是**把这一机制批量、对称地铺到 5 条通道**，省去使用者手写 5 份拼接/切片代码。

时序约束方面，本组件是**单时钟、纯同步**的（`clk_i` 单一时钟，[L27](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_multi_pl_stage.vhd#L27)），不存在跨时钟域，因此**不需要** `ASYNC_REG` 或 `set_max_delay` 这类异步约束（对照 u4-l2 的 `async_fifo`、u5-l1 的 `pulse_cc`）。它的时序收益完全来自「寄存器打断组合链」这一同步手段。

> 注意 full 主机本身也面临时序压力（`p_comb` 里 5 个 FSM + 双倍宽度移位的组合逻辑较深）。在很高的时钟频率下，可在 full 主机与下游从机之间插入一级 `axi_multi_pl_stage` 来分担。但 full 主机**内部**并未自带流水线插入点——需要时由集成者外挂。

#### 4.4.4 代码实践

**实践目标**：在「长布线 AXI 路径」场景下，论证插入 `axi_multi_pl_stage` 的收益（对应本讲指定的实践任务）。

**操作步骤（源码阅读 + 推理型）**：

1. 假设一个场景：某 SoC FPGA 中，PS 端的 AXI 主机经约 3000 个 LUT 的布线连到 PL 端的 `axi_master_full`，再连到远端的 `axi_slave_ipif`（u9-l5 将讲）。综合后时序报告显示 `m_axi_rready` / `m_axi_awready` 路径负 slack。
2. 在 `axi_master_full` 与 `axi_slave_ipif` 之间插入一个 `axi_multi_pl_stage`，初值 `stages_g=2`。
3. 对照 4.4.2 的表格与公式，列出预期的三方面变化。

**需要观察的现象（重新综合后）**：

- ready 关键路径被切成 2 段，slack 由负转正（具体数值**待本地综合验证**）。
- 数据通路多 2 拍延迟；首字到达从机的时间推迟 2 拍，但稳态吞吐不受影响（背靠背突发仍连续）。
- 反压（从机偶发拉低 ready）不会丢数据，因为 `use_rdy_g=>true`。

**预期结果**：用约两倍于单级的寄存器资源（影子寄存器，u7-l2）换取关键路径深度大幅下降，是高频设计里典型的「面积换时序」交易。具体 slack 改善幅度依赖工具与器件，**需本地综合确认**。

> 若有仿真环境，可先复用 4.2.4 的 `axi_multi_pl_stage_tb`，把 `stages_g` 从 3 改为 1/2/4 各跑一遍，观察写命令到从机 `awvalid` 的延迟周期数随 `stages_g` 线性增长，从而直观验证「延迟可控、功能不变」。

#### 4.4.5 小练习与答案

**练习 1**：能否用 `delay`（u7-l3）代替 `axi_multi_pl_stage` 来给 AXI 路径打拍？
**答案**：不能。`delay` 是单向数据延迟线，不处理 valid/ready 双向握手；把它插进 AXI 通道会破坏反压语义（ready 无法正确回传）。AXI 路径必须用带 `use_rdy_g=>true` 的 `pl_stage`/`multi_pl_stage` 系组件。

**练习 2**：`axi_multi_pl_stage` 需要像 `async_fifo` 那样加 `set_max_delay` 约束吗？
**答案**：不需要。它是单时钟同步组件，所有路径都可被标准 STA 分析；`set_max_delay`/`ASYNC_REG` 只用于跨时钟域（如 u4-l2、u5-l1）。

**练习 3**：插入流水线后，AXI 突发的「最高吞吐」会下降吗？
**答案**：稳态吞吐不会下降。流水线寄存器在背靠背突发下每拍都能推进一级，最终稳态仍是每拍一个 beat；受影响的只是「首字延迟」与反压响应延迟（各 +`stages_g` 拍）。

## 5. 综合实践

把本讲四块知识串起来，完成下面这个**集成设计推理任务**：

> 场景：你要把一个 `psi_common_axi_master_full`（AXI 64 位、用户 16 位、需要从非对齐地址搬若干奇数字节）连到一片远端的 `psi_common_axi_slave_ipif`（u9-l5），时钟 200 MHz，综合预估 ready 路径违例。

请按顺序回答并给出**源码依据**：

1. **选型论证**：为什么用 full 而非 simple？（引用 4.1 的三条额外能力与 [assert L227-229](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_full.vhd#L227-L229)）
2. **端口打包**：在 RTL 顶层想用 record 收拢 full 主机的扁平端口，应选哪个包、用哪两个 record 类型与默认常量？（引用 4.3 与 [axi_pkg L131-181](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_pkg.vhd#L131-L181)）——并指出 64 位宽度能否直接套用该 record。
3. **时序修复**：在主机与从机之间插入 `axi_multi_pl_stage`，说明它对 5 条通道分别做了什么、为什么 `use_rdy_g=>true` 是时序收敛成立的前提、为何无需异步约束。（引用 4.2、4.4 与 [multi_pl_stage L129-L143](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_multi_pl_stage.vhd#L129-L143)）
4. **验证计划**：说明你会复用哪个 TB、`stages_g` 怎么扫、用什么判据判定「功能不变」（引用 4.2.4 的 `axi_multi_pl_stage_tb` 与 `###ERROR###` 约定）。

完成后再回到 [hdl/psi_common_axi_master_full.vhd:578](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_full.vhd#L578)，确认你的设计里 full 主机最终仍把 AXI 五通道交给内部的 simple 主机去发——这就是本讲的贯穿性结论：**full 是 simple 的功能增强外壳，multi_pl_stage 是 AXI 路径的时序增强外壳**。

## 6. 本讲小结

- `axi_master_full` 不是重写，而是在 `axi_master_simple` 外包了「非对齐对齐 + 宽度转换（`wconv_n2xn`）+ 用户侧 FIFO」三层前端；simple 主机的 AXI 五通道逻辑被完整复用。
- full 的三件额外能力：非对齐/奇数字节传输、AXI 宽度 > 用户宽度、传输大小按字节计；代价是每命令数拍开销，小传输性能受限；约束为 `axi_data_width_g ≥ data_width_g` 且为整数倍。
- full 主机沿用二进程 record 设计法，但有 5 个 FSM 与双倍宽度的 `WrAlignReg`/`RdAlignReg` 移位寄存器；`AlignedAddr_f` 把地址低位清零实现字对齐，首尾 strobe 由地址低位比较生成。
- `axi_multi_pl_stage` 是纯结构包装器：把 AXI 5 条通道各拼成一根宽位总线，过 `multi_pl_stage`（`use_rdy_g=>true`）后再切片还原；B/R 通道方向与 AW/W/AR 相反。
- 这两个组件端口全部扁平（不用 record），record（`rec_axi_ms`/`rec_axi_sm`）由 `psi_common_axi_pkg` 供集成者在边界打包，以避开无约束 record 的工具链限制（Vivado 跳过 full TB 即为证据）。
- `axi_multi_pl_stage` 的全部价值在于时序收敛：靠寄存 ready 把长组合链切段，单时钟同步故无需异步约束，代价是数据/反压各 +`stages_g` 拍延迟、寄存器约翻倍，AXI 功能行为不变。

## 7. 下一步学习建议

- 下一讲 **u9-l5** 将讲 AXI **从机**侧：`axi_slave_ipif` 与 `axilite_slave_ipif` 如何把寄存器/存储映射到 AXI。学完后，你就能把本讲的 full 主机与从机接成一条完整的 AXI 链路，并用 `axi_multi_pl_stage` 在中间收时序。
- 想深入 full 主机的握手细节，可阅读 [testbench/psi_common_axi_master_full_tb/psi_common_axi_master_full_tb_case_axi_hs.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axi_master_full_tb/psi_common_axi_master_full_tb_case_axi_hs.vhd)（AXI 侧握手用例）与 `..._case_user_hs.vhd`（用户侧握手用例），对照波形理解 throttling 与 outstanding。
- 若关心 full 主机内部各 FSM 的状态跳转，重读 [hdl/psi_common_axi_master_full.vhd:234-535](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_axi_master_full.vhd#L234-L535) 的 `p_comb`，并回到 u7-l1/u7-l2 巩固影子寄存器与 `multi_pl_stage` 的串联原理。
- 进入 **U11 工程化** 后，可结合 u11-l1（自校验 TB）重看本讲的两个测试平台，理解 `TbRunning`/`ProcessDone` 多进程协调与 `###ERROR###` 自检约定如何在这两个 AXI TB 中落地。
