# AXI4 全功能主机（olo_axi_master_full）

## 1. 本讲目标

本讲承接 [u6-l3](u6-l3-axi-master-simple.md) 的 `olo_axi_master_simple`，讲解它的「升级版」`olo_axi_master_full`。学完本讲你应当能够：

- 理解 **非字对齐（unaligned）访问** 在 AXI 总线上为什么会成为问题，以及 `olo_axi_master_full` 如何用「地址对齐 + 数据移位 + 字节使能」解决它。
- 看懂 full 主机「套在 simple 主机外面」的**包装器架构**：对齐逻辑、位宽转换、再交给 simple 执行。
- 说出 full 与 simple 在**用户接口**上的四处关键差异（字节 vs 字、无 Wr_Be、命令先于数据、每条命令的额外开销）。
- 根据「是否需要对齐 / 是否需要位宽转换」判断该用 full、simple，还是 simple + 独立位宽转换实体。

> 本讲假设你已掌握 AXI4 五通道、AXI-S 握手、simple 主机的命令接口与高/低延迟模式（见 u6-l1、u6-l3），以及 `olo_base_wconv_n2xn` 的「窄到宽」位宽转换（见 [u3-l3](u3-l3-width-conversion-tdm.md)）。

## 2. 前置知识

### 2.1 字对齐（word alignment）与字节粒度

AXI4 的数据总线以**字（word）**为单位搬运，一个字的字节数 `AxiBytes = AxiDataWidth / 8`（如 32 位 = 4 字节）。一个「字对齐」的地址，其低 `log2(AxiBytes)` 位必须为 0：

- 32 位总线（4 字节）：对齐地址的低 2 位为 0，如 `0x100`、`0x104`、`0x108`。
- `0x102` 这样低 2 位非零的地址就是**非字对齐**地址。

AXI4 的地址通道（AW/AR）**只接受字对齐的起始地址**（配合 `AxSize` 描述每拍的字节宽度）。所以一次「从 0x102 读 5 字节」的请求，必须在硬件里被改造成「从 0x100 读 2 个字（8 字节），再把头尾多余的字节裁掉」。

### 2.2 字节使能 WStrb / Wr_Be

AXI4 写通道用 `WStrb`（write strobe，字节使能）逐字节标注「这一拍里哪些字节是有效的」。有了它，一次写一整个字但只让其中几个字节真正生效就成了可能——这正是实现非对齐写、不足一字节写的关键工具。在 Open Logic 内部，`Wr_Be` 是同样的字节使能信号。

### 2.3 数据右对齐（right-aligned / little-endian）

本实体约定：**用户数据的最低字节就是命令地址处的那个字节**。字节编号采用小端序——第 0 号字节位于数据向量的最低 8 位，对应最低地址。这一约定贯穿 full 主机的所有移位与字节使能计算。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| `src/axi/vhdl/olo_axi_master_full.vhd` | 本讲主角，full 主机的全部对齐 / 位宽转换 / 状态机逻辑，并在内部实例化 simple 主机。 |
| `src/axi/vhdl/olo_axi_master_simple.vhd` | 被 full 包装的「执行引擎」，负责真正的 AXI 突发拆分与总线时序。 |
| `doc/axi/olo_axi_master_full.md` | 官方文档，给出泛型、接口、读写时序图与设计取舍说明。 |
| `test/axi/olo_axi_master_full/olo_axi_master_full_tb.vhd` | 配套 VUnit 测试台，默认 `AxiDataWidth=32 / UserDataWidth=16`，覆盖大量非对齐场景。 |

---

## 4. 核心概念与源码讲解

### 4.1 非对齐访问

#### 4.1.1 概念说明

`olo_axi_master_full` 与 simple 主机最大的区别，就是它**接受任意字节地址、任意字节长度**的命令，并自动把这种「人类友好」的请求改造成「AXI 合法」的总线事务。

> 官方对它的定位（代码顶部注释）写得很直白：「In contrast to olo_axi_master_simple, this entity can do **unaligned transfers** and it supports **different width** for the AXI interface than for the data interface.」

要理解它为什么能做到，必须先看清它的**架构本质：full 主机本身不做任何 AXI 时序，它只是一个套在 simple 主机外面的「对齐 + 位宽转换」外壳**。所有真正的总线事务仍由内部的 `olo_axi_master_simple` 完成（见 [olo_axi_master_full.vhd:608-687](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_full.vhd#L608-L687)，full 把对齐后的「字对齐地址 + 字使能 + 字数据」喂给 simple）。

于是 full 的工作可以拆成三件事：

1. **地址对齐**：把用户给的非对齐起始地址，向下取整成字对齐地址交给 simple。
2. **数据移位 + 字节使能**：写方向把数据按地址偏移**左移**并生成 `WStrb`；读方向把读回的字**右移**，让目标字节落到用户数据的最低位。
3. **位宽转换**：当 `UserDataWidth < AxiDataWidth` 时，把若干个窄的用户字拼成宽的 AXI 字（反之由 simple 出来的宽字拆回窄字）。

#### 4.1.2 核心流程

给定一条读命令「从 `Addr` 读 `Size` 字节」，full 内部要算出四个量：

- **对齐起始地址**：把 `Addr` 的低 `log2(AxiBytes)` 位清零。
- **末字节地址**：`LastAddr = Addr + Size - 1`。
- **AXI 拍数**：把首末地址都对齐后，算跨度内包含多少个字。

\[ N_{\text{beats}} = \frac{\mathrm{align}(\text{LastAddr}) - \mathrm{align}(\text{Addr})}{\text{AxiBytes}} + 1 \]

- **首字节偏移 `Shift`**：`Addr` 的低 `log2(AxiBytes)` 位，即目标字节在第一个 AXI 字里的位置。

读方向用 `Shift` 把读回的数据右移，使目标字节成为用户字的最低字节；写方向则反过来左移，并用 `WStrb` 标出真正要写的字节。

#### 4.1.3 源码精读

**地址对齐函数 `alignedAddr`**——把低 `log2(AxiBytes)` 位清零：

```vhdl
function alignedAddr (Addr : ...) return unsigned is
    variable Addr_v : unsigned(Addr'range) := (others => '0');
begin
    Addr_v(Addr'left downto log2(AxiBytes_c)) := Addr(Addr'left downto log2(AxiBytes_c));
    return Addr_v;
end function;
```

见 [olo_axi_master_full.vhd:141-147](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_full.vhd#L141-L147)。它只拷贝高于字节选择位的那部分，低位保持 0，得到字对齐地址。

**读命令状态机 `RdCmdFsm`** 在 `Apply_s` 态一次性算出对齐地址、AXI 拍数、首/末字节使能：

```vhdl
v.AxiRdCmd_Addr := std_logic_vector(alignedAddr(unsigned(CmdRd_Addr)));          -- 对齐起始地址
...
v.AxiRdCmd_Size := std_logic_vector(resize(shift_right(alignedAddr(r.RdLastAddr)
                        - unsigned(r.AxiRdCmd_Addr), log2(AxiBytes_c)) + 1, ...));  -- AXI 拍数
```

见 [olo_axi_master_full.vhd:449-475](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_full.vhd#L449-L475)。其中 `AxiRdCmd_Size` 就是上面公式里的 `N_beats`。

**末字节使能 `RdLastBe`** 与 **首字节使能 `RdFirstBe`** 用两个循环生成，决定每个 AXI 字里哪些字节要保留：

```vhdl
-- 末字：地址 <= byte 的字节有效（裁掉超出末尾的高位字节）
if r.RdLastAddr(log2(AxiBytes_c) - 1 downto 0) >= byte then v.RdLastBe(byte) := '1'; ...
-- 首字：首偏移 <= byte 的字节有效（裁掉低于起始地址的低位字节）
if r.RdFirstAddrOffs <= byte then v.RdFirstBe(byte) := '1'; ...
```

见 [olo_axi_master_full.vhd:457-473](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_full.vhd#L457-L473)。

**读数据对齐**：把读回的 AXI 字按 `Shift` 右移，并在 `RdAlignReg`（宽度为 `2*AxiDataWidth` 的双倍缓冲）里用 `RdAlignByteVld` 记录哪些字节有效，凑满一个用户字就送出：

```vhdl
-- 消费掉最低的 UserDataWidth 位，整体右移
v.RdAlignReg     := zerosVector(UserDataWidth_g) & r.RdAlignReg(r.RdAlignReg'left downto UserDataWidth_g);
-- 新 AXI 字插入到偏移 RdLowIdx (= AxiBytes - Shift) 处
v.RdAlignReg(RdLowIdxInt_v*8 + AxiDataWidth_g - 1 downto RdLowIdxInt_v*8) := AxiRdDat_Data;
```

见 [olo_axi_master_full.vhd:539-555](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_full.vhd#L539-L555)。写方向的对齐逻辑（左移 + 生成 `WrAlignBe`）结构对称，见 [olo_axi_master_full.vhd:368-392](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_full.vhd#L368-L392)。

#### 4.1.4 代码实践：手算一次非对齐读

**实践目标**：用一个具体例子验证你对地址对齐、拍数、字节使能的理解。下面这个例子正好对应测试台的默认配置（`AxiDataWidth=32 / UserDataWidth=16`，见 [olo_axi_master_full_tb.vhd:34-36](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/axi/olo_axi_master_full/olo_axi_master_full_tb.vhd#L34-L36)）。

**操作步骤**（纯纸笔推导）：

1. 设命令为「从 `Addr = 0x00000102` 读 `Size = 5` 字节」，`AxiBytes = 4`。
2. 计算：
   - `LastAddr = 0x102 + 5 - 1 = 0x106`
   - 对齐起始地址 `align(0x102) = 0x100`
   - `align(LastAddr) = align(0x106) = 0x104`
   - AXI 拍数 `= (0x104 - 0x100)/4 + 1 = 2` 拍（即 `M_Axi_ArLen = 1`）
   - 首偏移 `Shift = 0x102 mod 4 = 2`
   - `RdFirstBe`：`FirstOffs(=2) <= byte` 成立的是 byte2、byte3 → `1100`（保留 `0x102`、`0x103`）
   - `RdLastBe`：`LastAddr mod 4 = 2 >= byte` 成立的是 byte0、byte1、byte2 → `1110`（保留 `0x104`、`0x105`、`0x106`）

**需要观察的现象 / 预期结果**：

- 两拍 AXI 读共取回 8 字节（`0x100..0x107`），但经字节使能裁剪后只剩 `0x102,0x103,0x104,0x105,0x106` 共 5 字节。
- 再经右移 2 字节，用户侧（16 位）应依次得到三拍：`[0x102,0x103]`、`[0x104,0x105]`、`[0x106, 任意]`，最后一拍带 `Rd_Last`。

> **待本地验证**：以上是按源码逐步推导的预期值。建议运行 4.1.5 中的仿真，在波形里核对 `M_Axi_ArAddr=0x100`、`M_Axi_ArLen=1` 以及用户侧三拍数据是否吻合。

#### 4.1.5 小练习与答案

**练习 1**：若把上面的命令改成「从 `0x100` 读 8 字节」（完全对齐），`N_beats`、`Shift`、`RdFirstBe/RdLastBe` 分别是多少？

**答案**：`LastAddr=0x107`，`align` 后首末都是 `0x100`/`0x104`，`N_beats = (0x104-0x100)/4+1 = 2`，`Shift = 0`，`RdFirstBe = RdLastBe = 1111`（全有效）。可见对齐访问不会引入任何裁剪开销。

**练习 2**：为什么读对齐寄存器 `RdAlignReg` 的宽度要做成 `2 * AxiDataWidth` 而不是 `AxiDataWidth`？

**答案**：因为一个 AXI 字经 `Shift` 右移后，目标字节可能横跨「当前字的高位 + 下一个字的低位」两部分；双倍宽度的缓冲能同时容纳尚未送出的尾部和刚刚到达的新字，避免跨字节数据丢失。

---

### 4.2 与 simple 主机的差异

#### 4.2.1 概念说明

虽然 full 是套在 simple 外面的外壳，但它对**用户暴露的接口**有几处本质不同。理解这些差异，才能在两台主机之间正确切换。官方文档把它们总结为三点（再加上一条「命令开销」），见 [olo_axi_master_full.md:31-39](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/doc/axi/olo_axi_master_full.md#L31-L39)。

#### 4.2.2 核心流程

下表是 simple 与 full 用户接口的逐项对比：

| 维度 | `olo_axi_master_simple` | `olo_axi_master_full` |
| :--- | :--- | :--- |
| `CmdXx_Size` 单位 | **字（beats）** | **字节（bytes）** |
| 起始地址 | 必须字对齐（否则被静默向下对齐） | 允许任意字节地址 |
| 写字节使能 | 用户需提供 `Wr_Be` | **无 `Wr_Be`**，由对齐逻辑自动生成 |
| 数据位宽 | `Wr_Data = AxiDataWidth` | `Wr_Data = UserDataWidth`（可窄于 AXI） |
| 命令与数据先后 | 无要求，可先于 / 后于命令 | **写数据通常须等命令**之后 |
| 单命令开销 | 每次写事务至少 4 拍 | **至少 4 拍**（对齐外壳叠加，小事务更吃亏） |

#### 4.2.3 源码精读

**差异一：Size 单位是字节。** full 的泛型约束放宽为「`UserTransactionSizeBits_g <= AxiAddrWidth_g`」（按字节计，覆盖整个地址空间），见 [olo_axi_master_full.vhd:245-247](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_full.vhd#L245-L247)；而 simple 因为按字计，约束更紧「`< AxiAddrWidth_g - log2(AxiDataWidth_g/8)`」，见 [olo_axi_master_simple.vhd:248-249](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_simple.vhd#L248-L249)。

**差异二：无 `Wr_Be`，数据位宽独立。** 对比两台主机的写数据端口——simple 有 `Wr_Be` 且数据宽等于 AXI：

```vhdl
-- simple（olo_axi_master_simple.vhd:69-71）
Wr_Data : in std_logic_vector(AxiDataWidth_g - 1 downto 0);
Wr_Be   : in std_logic_vector(AxiDataWidth_g / 8 - 1 downto 0);
```

而 full 用 `UserDataWidth_g` 且**完全没有 `Wr_Be`**：

```vhdl
-- full（olo_axi_master_full.vhd:71-73）
Wr_Data : in std_logic_vector(UserDataWidth_g - 1 downto 0);
Wr_Valid: in std_logic;
Wr_Ready: out std_logic;
```

字节使能改由内部的位宽转换 `olo_base_wconv_n2xn` 产出的 `Out_WordEna` 再展宽得到，见 [olo_axi_master_full.vhd:347-349](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_full.vhd#L347-L349) 与位宽转换实例 [olo_axi_master_full.vhd:714-731](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_full.vhd#L714-L731)。

**差异三：写数据一般不能先于命令。** 因为「数据要先按命令的地址偏移做对齐移位」，所以命令未知时无法把数据正确送进对齐逻辑。官方文档明确这一点，见 [olo_axi_master_full.md:150-158](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/axi/olo_axi_master_full.md#L150-L158)。注意文档也说明：出于时序优化，命令到来前**可能**仍被接受少数几个字——因此用户设计**不应**依赖「命令前绝对不收数据」这一行为；若数据先于命令到达且总量未知，应在 full 外面再加一级 FIFO 缓冲。

#### 4.2.4 代码实践：读 simple 的地址屏蔽函数

**实践目标**：用源码确认「simple 遇到非对齐地址会怎样」。

**操作步骤**：

1. 打开 [olo_axi_master_simple.vhd:129](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_simple.vhd#L129) 与 [olo_axi_master_simple.vhd:142-148](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_simple.vhd#L142-L148) 的 `addrMasked` 函数。
2. 阅读它如何把地址的低 `UnusedAddrBits_c = log2(AxiDataWidth/8)` 位**强制清零**。

**需要观察的现象 / 预期结果**：simple 没有报错，而是把 `0x102` 静默改成 `0x100`——也就是说，如果你误把非对齐地址喂给 simple，它会**读 / 写到错误的字**而不报警。这正是「simple 不支持非对齐」的真实含义，也是 full 必须存在的原因。

> **待本地验证**：可在 simple 的测试台里故意发一个非对齐地址，观察 `M_Axi_ArAddr` 是否被向下取整。

#### 4.2.5 小练习与答案

**练习 1**：把一段「从 `0x102` 读 5 字节」的命令误发给 simple，实际会发生什么？

**答案**：simple 的 `addrMasked` 把地址改成 `0x100`；又因为它的 `Size` 以字为单位，5 会被当成 5 个字（20 字节）而不是 5 字节。结果是从 `0x100` 起读 5 个字，与意图完全不符——既偏了地址，又错了长度。

**练习 2**：为什么 full 的用户接口去掉 `Wr_Be` 反而是「更易用」？

**答案**：因为对齐偏移完全由命令地址决定，字节使能可以由硬件自动算出；让用户手算 `Wr_Be` 既繁琐又容易出错。去掉它让用户只关心「要写哪些字节、放在数据的低位」，符合 right-aligned 约定。

---

### 4.3 能力边界

#### 4.3.1 概念说明

「full」并不意味着万能。它的能力边界由两组约束画定：一是 AXI 协议本身的硬性限制，二是 full 相对 simple 在性能上的代价。看清边界，才知道什么时候**不该**用它。

#### 4.3.2 核心流程

**能力上界（full 能做到的）：**

- 任意字节地址（非对齐）的读 / 写。
- 任意字节长度（不必是 AXI 字的整数倍）的传输。
- AXI 数据宽度**大于**用户数据宽度（`AxiDataWidth >= UserDataWidth` 且为整数倍）。

**能力下界（full 做不到 / 受限的）：**

- `UserDataWidth` **不能大于** `AxiDataWidth`——断言强制 `AxiDataWidth_g mod UserDataWidth_g = 0`，见 [olo_axi_master_full.vhd:242-244](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_full.vhd#L242-L244)。
- `UserDataWidth` 必须是 8 的整数倍，`AxiDataWidth/8` 必须是 2 的幂，见 [olo_axi_master_full.vhd:236-241](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_full.vhd#L236-L241)。
- **单条命令至少 4 拍开销**：对齐外壳的状态机（命令 FSM → 位宽转换 FSM → 对齐 FSM → simple 自身 FSM）层层串行，使每条命令都有固定开销。文档因此明确指出：「for cases where many very small (e.g. single-beat) transactions are required this entity is suboptimal」，见 [olo_axi_master_full.md:37-39](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/axi/olo_axi_master_full.md#L37-L39)。
- 当 `AxiDataWidth > UserDataWidth` 时，文档建议**不要**用低延迟模式，因为位宽转换会限制带宽、导致总线上的 stall 周期，见 [olo_axi_master_full.md:101](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/axi/olo_axi_master_full.md#L101) 与 [olo_axi_master_full.md:119](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/axi/olo_axi_master_full.md#L119)。

#### 4.3.3 源码精读

四条编译期断言把上述数学边界钉死在综合阶段：

```vhdl
assert isPower2(AxiDataWidth_g/8)            ...  -- AXI 字节数须为 2 的幂
assert UserDataWidth_g mod 8 = 0             ...  -- 用户宽度须为 8 的倍数
assert AxiDataWidth_g mod UserDataWidth_g = 0 ... -- AXI 须为用户宽度的整数倍
assert UserTransactionSizeBits_g <= AxiAddrWidth_g ... -- 字节计 size 不超地址范围
```

见 [olo_axi_master_full.vhd:236-247](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_full.vhd#L236-L247)。任何越界配置在 elaborate 阶段即 `failure`，不会带进仿真或综合。

「命令开销」则可从状态机的串行结构看出：写侧有 `WriteCmdFsm`、`WriteWconvFsm`、`WriteAlignFsm` 三段（见 [olo_axi_master_full.vhd:134-136](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_full.vhd#L134-L136)），读侧有 `ReadCmdFsm`、`ReadDataFsm` 两段（见 [olo_axi_master_full.vhd:137-138](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_full.vhd#L137-L138)），它们与 simple 内部的 FSM 串联，构成了固定延迟。

#### 4.3.4 代码实践：故意触发一条断言

**实践目标**：亲眼看到 full 的能力边界是被断言强制的。

**操作步骤**：

1. 复制测试台，把 DUT 的 `UserDataWidth_g` 设成大于 `AxiDataWidth_g`（例如 `AxiDataWidth=32, UserDataWidth=64`）。
2. 用 GHDL elaborate：`python sim/run.py --ghdl -ed ".*olo_axi_master_full.*"`（命令仅作参考，具体调用见 [u1-l4](u1-l4-run-first-simulation.md)）。

**需要观察的现象 / 预期结果**：elaborate 阶段即因 `AxiDataWidth_g must be a multiple of UserDataWidth_g` 断言失败而中止，根本进不了仿真。

> **待本地验证**：不同仿真器对 `severity failure` 的断言输出格式略有差异，但都会在 0 时刻报告并停止。

#### 4.3.5 小练习与答案

**练习 1**：你的应用是「每秒上万次单字节寄存器访问」，该用 full 吗？

**答案**：不该。full 每条命令至少 4 拍固定开销，对单拍事务极不划算；这种场景应优先 simple（且地址天然对齐到字）。

**练习 2**：`UserDataWidth = 24, AxiDataWidth = 32` 的组合合法吗？

**答案**：不合法。`UserDataWidth mod 8 = 0` 成立，但 `AxiDataWidth mod UserDataWidth = 32 mod 24 ≠ 0`，会触发 [olo_axi_master_full.vhd:242-244](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_full.vhd#L242-L244) 的断言失败。

---

### 4.4 选型取舍

#### 4.4.1 概念说明

Open Logic 在 AXI 主机这一档提供了 simple 与 full 两台，并非「full 一定更好」。它们是**面积 / 性能 / 易用性**的三方权衡。官方文档甚至给出了一条「退路」：如果你只需要位宽转换而不需要对齐，完全可以「simple + 独立位宽转换实体」获得更优的性能与更小的资源，见 [olo_axi_master_full.md:41-45](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/axi/olo_axi_master_full.md#L41-L45)。

#### 4.4.2 核心流程

选型决策树：

```text
命令地址是否需要非字对齐 / 字节级长度？
├─ 是 ──→ 必须用 olo_axi_master_full
└─ 否（地址天然对齐、长度按字计）
    │
    用户位宽是否等于 AXI 位宽？
    ├─ 是 ──→ 用 olo_axi_master_simple（最省、最快）
    └─ 否（仅需窄→宽位宽转换）
            │
            单事务很大（突发为主）？
            ├─ 是 ──→ simple + olo_base_wconv_n2xn/xn2n（更省资源、更高吞吐）
            └─ 否（极多小事务）──→ 仍优先 simple，避免 full 的 4 拍开销
```

补充：读 / 写两侧可分别用 `ImplRead_g` / `ImplWrite_g` 关闭，省下不用的方向（见 [olo_axi_master_full.vhd:50-51](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_full.vhd#L50-L51)），`g_nwrite` / `g_nread` 分支把对应输出恒定到安全值，见 [olo_axi_master_full.vhd:735-737](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_full.vhd#L735-L737) 与 [olo_axi_master_full.vhd:768-772](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_full.vhd#L768-L772)。

#### 4.4.3 源码精读

full 之所以能「按需省略读写」且仍实例化同一个 simple，是因为 simple 本身也有 `ImplRead_g / ImplWrite_g`，full 把它们原样透传，见 [olo_axi_master_full.vhd:616-617](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_full.vhd#L616-L617)。而位宽转换、对齐逻辑则分别包在 `g_write` / `g_read` 两个 `generate` 里，关闭某方向时整段逻辑都不综合，见 [olo_axi_master_full.vhd:690-733](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_full.vhd#L690-L733) 与 [olo_axi_master_full.vhd:740-766](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_full.vhd#L740-L766)。

注意写侧用了两个被包装的底层实体：一个 `olo_base_pl_stage` 做时序隔离，一个 `olo_base_wconv_n2xn` 把用户字拼成 AXI 字，见 [olo_axi_master_full.vhd:695-731](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_full.vhd#L695-L731)。这正是「simple + 独立 wconv」方案的内部实现——换句话说，full 的写通路 ≈ `pl_stage + wconv_n2xn + simple`，区别只在于它**额外**插入了 4.1 节的对齐移位逻辑。如果你不需要对齐，把这段 wconv 直接接在 simple 外面，就能省掉对齐逻辑那一层资源与延迟。

#### 4.4.4 代码实践：估算三种方案的资源

**实践目标**：建立对三种方案资源 / 延迟的量级直觉。

**操作步骤**：

1. 阅读三段实例化代码：
   - 纯 simple：[olo_axi_master_simple.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_simple.vhd)
   - full 内部的 wconv：[olo_axi_master_full.vhd:714-731](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_full.vhd#L714-L731)
   - full 额外的对齐外壳：`WriteAlignFsm` 全部寄存器 `WrAlignReg`（`2*AxiDataWidth` 位）等，见 [olo_axi_master_full.vhd:166-174](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_full.vhd#L166-L174)
2. 列表对比「simple」「simple+wconv」「full」三者各多出哪些寄存器与状态机。

**需要观察的现象 / 预期结果**：

| 方案 | 额外寄存器 | 是否支持非对齐 | 适合场景 |
| :--- | :--- | :--- | :--- |
| simple | 无 | 否 | 对齐、等宽、大数据量 |
| simple + wconv | wconv 的移位寄存器 | 否 | 对齐、需位宽转换、突发为主 |
| full | wconv + `2*AxiDataWidth` 对齐寄存器 + 多个 FSM | 是 | 非对齐 / 字节级长度 |

> **待本地验证**：精确的 LUT / FF 数字需在你目标器件上跑一次综合（可用 `tools/inference_test`，见 [u10-l4](u10-l4-lint-and-synthesis-test.md)）。

#### 4.4.5 小练习与答案

**练习 1**：一个图像处理通路要把 16-bit 像素流写入 32-bit AXI 内存，地址永远 4 字节对齐，应该怎么选？

**答案**：用 `olo_axi_master_simple` + `olo_base_wconv_n2xn`（16→32）。地址已对齐、只需位宽转换，不必为对齐逻辑买单。

**练习 2**：什么情况下 `CmdXx_LowLat` 在 full 里反而有害？

**答案**：当 `AxiDataWidth > UserDataWidth` 时。位宽转换（多个用户字拼一个 AXI 字）会限制瞬时带宽；低延迟模式下命令立即发出，数据供不上就会卡住总线，产生 stall 周期。此时应保持默认的高延迟模式。

---

## 5. 综合实践

**任务**：分别用 `olo_axi_master_simple` 与 `olo_axi_master_full` 发起一次**非字对齐读取**，对比行为差异，并说明 full 如何处理跨字节访问。

**推荐做法（基于已有测试台的源码阅读 + 仿真）**：

1. **准备环境**：按 [u1-l4](u1-l4-run-first-simulation.md) 进入 `sim/`，确认已 `codegen_generate()`（fix 区域代码生成，见 [u8-l4](u8-l4-python-codegen-pkg-writer.md)）。本实践只涉及 axi 区域，可只编译相关文件。

2. **跑 full 的测试台**（它默认就是 `AxiDataWidth=32 / UserDataWidth=16`，天然含非对齐用例）：

   ```bash
   python sim/run.py --ghdl -v "*.olo_axi_master_full_tb.*"
   ```

   （命令格式参考 [sim/run.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py)；具体用例过滤串以本地 VUnit 版本为准——**待本地验证**。）

3. **观察 full 侧**：在波形里挑一条非对齐读用例，核对：
   - `M_Axi_ArAddr` 是否为**向下对齐**后的地址（低 2 位为 0）；
   - `M_Axi_ArLen` 是否等于「对齐后的拍数 − 1」；
   - 用户侧 `Rd_Data` 的最低字节是否正好对应命令地址处的字节（right-aligned）。

4. **对照 simple 侧**：阅读 [olo_axi_master_simple.vhd:142-148](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_simple.vhd#L142-L148) 的 `addrMasked`，或在 simple 测试台里发一条 `Addr=0x102` 的命令，观察 `M_Axi_ArAddr` 被静默改成 `0x100`、且 `Size` 被当作「字数」而非「字节数」。

5. **写结论**：用 4.1.4 的手算例子说明 full 是如何用「**多读整字 → 用 `RdFirstBe/RdLastBe` 裁掉头尾 → 用 `Shift` 右移拼出用户字**」三步完成跨字节访问的；并指出 simple 因缺少这三步，会把非对齐请求错误地对齐到字边界。

> 若无法运行仿真，本实践可退化为纯源码追踪：把 4.1.4 的 `0x102 / 5 字节` 例子在 [olo_axi_master_full.vhd:433-559](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_full.vhd#L433-L559) 的读通路里逐拍走一遍，写出每个寄存器的值。

## 6. 本讲小结

- `olo_axi_master_full` 是套在 `olo_axi_master_simple` 外面的**对齐 + 位宽转换外壳**，自身不产生 AXI 时序，真正发总线事务的仍是内部的 simple 主机。
- 它的核心能力是**非对齐访问**：用 `alignedAddr` 把地址向下取整、用首/末字节使能裁剪头尾、用移位（写左移 / 读右移）把目标字节对齐到用户数据的最低位。
- 与 simple 的关键差异：`Size` 以**字节**计、用户接口**无 `Wr_Be`**（自动生成）、`UserDataWidth` 可窄于 `AxiDataWidth`、写数据一般须**等命令**之后、每条命令至少 **4 拍**开销。
- 能力边界由四条 elaborate 期断言强制：`UserDataWidth` 是 8 的倍数、`AxiDataWidth` 是 `UserDataWidth` 的整数倍、`AxiDataWidth/8` 是 2 的幂、`UserTransactionSizeBits_g <= AxiAddrWidth_g`。
- 选型口诀：**需要对齐 / 字节级长度 → full；仅需位宽转换 → simple + `wconv_n2xn/xn2n`；等宽对齐 → simple**。极多小事务时尤其要避开 full 的固定开销。

## 7. 下一步学习建议

- 想看 full 的对齐逻辑在真实波形里如何展开，可继续精读测试台 [olo_axi_master_full_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/axi/olo_axi_master_full/olo_axi_master_full_tb.vhd)，并对照 [u10-l1](u10-l1-vunit-tb-and-vcs.md) 学习 VUnit 测试台与 AXI 验证组件（VC）的用法。
- 位宽转换是对齐外壳的基础元件，建议回顾 [u3-l3](u3-l3-width-conversion-tdm.md) 中 `olo_base_wconv_n2xn` / `xn2n` 的 Last 与 WordEna 约定，理解 full 写通路如何把不足整字的尾部处理掉。
- 至此第 6 单元（AXI 区域）的 pl_stage、lite 从机、simple/full 主机已讲完。下一讲可进入第 7 单元 [u7-l1](u7-l1-sync-debounce-clkmeas.md)，学习 intf 区域的外部接口（同步器、消抖、时钟测量）。
