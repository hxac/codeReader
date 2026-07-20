# 寄存器映射与配置表

## 1. 本讲目标

软件要驱使 `vivadoIP_axi_mm_reader` 工作，唯一手段就是经 `s00_axi`（AXI 从机）读写它暴露出来的一组「寄存器」。本讲只解决一个问题：**这些寄存器到底有哪些、各自在哪个地址、每一位是什么含义、用什么方式读写**。学完本讲你应当能够：

- 看懂 `hdl/definitions_pkg.vhd` 中的地址常量，并能把它换算成软件看到的字节地址。
- 默写出五个配置寄存器（`Ena`/`RegCnt`/`RdData`/`RdLast`/`Level`）的地址、位宽、读写模式与作用。
- 解释 `Addr[]` 配置表所在的「内存区」为什么从 `0x20` 开始，它和 `MemOffs_c`、寄存器区大小是什么关系。
- 说清楚在 AXIMM 输出模式下，为什么软件必须**先读 `RdLast`、再读 `RdData`**。

本讲不展开 FSM 与 AXI 主机细节（那是 u2-l3、u2-l6 的事），只把「软件视角的寄存器空间」这张地图彻底讲透。

## 2. 前置知识

在进入源码前，先建立三个朴素概念。

**1）存储器映射的寄存器（memory-mapped registers）。**
CPU（或 AXI 主机）不能直接拉一根线去翻转 IP 内部的某个标志，而是把整个 IP 的配置/状态「铺」成一段连续的地址空间。每个地址对应一个 32 位的字，软件往某个地址写 0/1 就等于设置某个开关，读某个地址就等于查询某个状态。这种「用地址代替引脚」的做法就是寄存器映射。

**2）字地址与字节地址。**
AXI4 的地址是**字节地址**（每个字节一个编号），但寄存器是 32 位 = 4 字节一个。所以第 N 个寄存器的字节地址是 \( N \times 4 \)。本讲会反复出现「寄存器索引（字地址）」和「字节地址」两种说法，记住乘 4 即可互转：

\[ \text{字节地址} = \text{寄存器索引} \times 4 \]

例如索引 `3` → 字节地址 `0x0C`。

**3）寄存器的「模式」。**
文档定义了五种模式，理解 `RV` 是本讲的关键：

| 模式 | 含义 | 本 IP 是否用到 |
|:----:|:------|:--------------|
| R    | 只读（普通读，无副作用） | 是（`RdLast`、`Level`） |
| W    | 只写 | 否 |
| RW   | 可读可写 | 是（`Ena`、`RegCnt`、`Addr[]`） |
| RV   | **带副作用的读**——读一次就会改变 IP 状态 | 是（`RdData`：读一次就弹出 FIFO 一个值） |
| RCW1 | 读，写 1 清零 | 否（仅作通用说明） |

注意 `RV`：这是本 IP 最「反直觉」也最重要的一个模式。读 `RdData` 不是无害的——它会把内部 FIFO 的当前值「消费」掉。

承接 [u2-l1 整体架构与数据流](u2-l1-architecture-dataflow.md)：IP 有两种输出模式 `AXIS`（数据从 `m_axis` 端口直出）与 `AXIMM`（默认，数据映射到寄存器空间的 FIFO，由软件读出）。本讲的 `RdData`/`RdLast` **只在 AXIMM 模式下存在**，这点会贯穿全讲。

## 3. 本讲源码地图

| 文件 | 在本讲的作用 |
|:-----|:------------|
| [hdl/definitions_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/definitions_pkg.vhd) | **核心**。用一组 `constant` 把所有寄存器索引与位号定义成名字，是整张寄存器地图的「单一事实来源」。 |
| [doc/Documentation.md](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md) | **核心**。面向使用者的寄存器表（地址/名称/模式/位/描述）与「先读 RdLast」的使用约定。 |
| [hdl/axi_mm_reader_wrp.vhd](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd) | 印证常量如何被使用：寄存器区的 2 的幂对齐、`RegCount`/`Enable` 的位字段提取、AXIMM 下 `RdData` 弹 FIFO 的接线。 |
| [tb/top_tb.vhd](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd) | 印证 `MemOffs_c` 的字节寻址用法，以及「先读 RdLast、再读 RdData」的实测顺序。 |

> 说明：本讲引用的 wrapper 与 testbench 片段是为了「坐实」寄存器表，不展开它们的内部机制（那是 u2-l5、u2-l7、u3-l2 的内容）。

---

## 4. 核心概念与源码讲解

### 4.1 definitions_pkg 中的地址常量

#### 4.1.1 概念说明

一个 IP 有十几个寄存器，如果在 RTL 各处、测试台、驱动里都硬编码 `0x0C`、`0x10` 这样的「魔法数字」，一旦寄存器顺序调整，就要满仓库改数字，极易出错。工业级做法是：**把所有地址集中定义在一个 package 里，给每个地址起一个名字，全工程只用名字**。本项目的 `definitions_pkg.vhd` 就是这个角色——它本身不含任何逻辑，只是一张「地址名册」。

理解这张名册有一个关键：里面存的是**寄存器索引（字地址）**，不是字节地址。换算成软件看到的字节地址要自己乘 4。

#### 4.1.2 核心流程

寄存器索引 → 软件字节地址的换算流程：

```text
definitions_pkg 里的常量 (字索引 N)
        │  ×4
        ▼
软件在 AXI 上使用的字节地址 (N×4)
        │  例: RegIdx_Level_c = 4  →  0x10
        ▼
寄存器表里写的那一栏 Address
```

常量分两类：
- `RegIdx_*_c`：**寄存器索引**（第几个 32 位寄存器）。
- `BitIdx_*_c`：**位号**（寄存器内部的第几位）。
- `RegCount_c`：寄存器**总数**，由最后一个索引 +1 派生，避免「加寄存器后忘了改总数」。
- `MemOffs_c`：内存区（`Addr[]` 配置表）的起始**字索引**——见 4.3。

#### 4.1.3 源码精读

整张名册非常短：

[hdl/definitions_pkg.vhd:25-35](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/definitions_pkg.vhd#L25-L35) —— 把每个寄存器/位的编号定义成可读名字，`RegCount_c` 由 `RegIdx_Level_c+1` 派生，`MemOffs_c := 8` 标记内存区起点。

逐行翻译成软件视角：

| 常量 | 值 | 含义 | 软件字节地址 |
|:-----|:--:|:-----|:------------:|
| `RegIdx_Ctrl_c` | 0 | 控制寄存器（含使能位） | `0x00` |
| `BitIdx_Ctrl_Ena_c` | 0 | Ctrl 寄存器的第 0 位 = 使能 | — |
| `RegIdx_RegCnt_c` | 1 | 本周期要读的寄存器个数 | `0x04` |
| `RegIdx_RdData_c` | 2 | 读数据 FIFO 出口（AXIMM 专用） | `0x08` |
| `RegIdx_RdLast_c` | 3 | 当前值是否是本周期最后一个 | `0x0C` |
| `BitIdx_RdLast_c` | 0 | RdLast 寄存器的第 0 位 | — |
| `RegIdx_Level_c` | 4 | 内部 FIFO 当前水位 | `0x10` |
| `RegCount_c` | 5 | 实际使用的寄存器数 | — |
| `MemOffs_c` | 8 | `Addr[]` 内存区起始字索引 | `0x20` |

注意两点工程细节：
- `RegCount_c` 不是硬编码 `5`，而是 `RegIdx_Level_c+1`。这样将来若在末尾新增寄存器，总数会自动更新——**派生优于字面量**。
- `MemOffs_c = 8` 是**字索引**，对应的字节地址是 \( 8 \times 4 = 32 = \texttt{0x20} \)，正好是文档里 `Addr[0]` 的地址（见 4.3）。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：验证「常量 → 字节地址」的换算与文档一致。
2. **步骤**：
   - 打开 `hdl/definitions_pkg.vhd`，记下五个 `RegIdx_*_c` 的值。
   - 打开 `doc/Documentation.md` 的寄存器表（见 4.2.3），逐行比对 `地址 = 索引 × 4`。
   - 打开 `tb/top_tb.vhd`，看测试台访问寄存器时是否也用 `RegIdx_*_c*4` 的形式。
3. **现象**：测试台里到处是 `RegIdx_Level_c*4`、`RegIdx_Ctrl_c*4` 这样的写法，说明工程内一致用「索引名 ×4」拼字节地址，从不写裸数字。
4. **预期结果**：例如 `axi_single_write(RegIdx_Ctrl_c*4, 1, ...)` 就是往 `0x00` 写 1（使能 IP）；`axi_single_expect(RegIdx_Level_c*4, ...)` 就是读 `0x10` 的 FIFO 水位。两处与文档完全吻合。
5. 待本地验证（若你手头有 Vivado/Vitis，可读回 `0x10` 确认）。

#### 4.1.5 小练习与答案

- **练习**：如果要在 `Level` 之后新增一个只读寄存器 `Version`，应该在 `definitions_pkg.vhd` 里改哪几行？它的字节地址会是什么？
- **参考答案**：新增 `constant RegIdx_Version_c : natural := 5;`，并把 `RegCount_c` 的派生改为 `RegIdx_Version_c+1`（或保持 `RegIdx_Level_c+1` 但改成以新最后一个为准）。字节地址为 \( 5 \times 4 = \texttt{0x14} \)。注意：若新增到使 `RegCount_c` 超过 8，则 `MemOffs_c` 也必须同步增大（见 4.3.5 的陷阱）。

---

### 4.2 寄存器表详解（Ena / RegCnt / RdData / RdLast / Level）

#### 4.2.1 概念说明

五个配置寄存器按职责分成两组：

- **控制组**（软件写下去指挥 IP）：`Ena`（开关）、`RegCnt`（这轮读几个）、`Addr[]`（读哪些地址，见 4.3）。
- **数据组**（软件读出来拿结果）：`RdData`（读回的值）、`RdLast`（是不是本轮最后一个）、`Level`（FIFO 还积了多少）。

其中 `RdData`/`RdLast` **只在 AXIMM 输出模式下存在**：因为只有 AXIMM 模式才把读回值映射到寄存器空间让软件读；AXIS 模式下数据直接从 `m_axis` 端口流走，根本不需要这两个寄存器。`Level` 两种模式都有，因为两种模式背后都用了同一个 FIFO 做缓冲。

`RdData` 是全表唯一的 `RV`（带副作用读）寄存器，这是理解「先读 RdLast」约定的钥匙——见 4.2.3 末尾与综合实践。

#### 4.2.2 核心流程

软件配置一次读周期的「寄存器操作序列」（AXIMM 模式）：

```text
1. 写 Ena    = 0          # 先关掉 IP，才能安全改配置
2. 写 RegCnt = N          # 告诉 IP 这一轮读 N 个寄存器
3. 写 Addr[0..N-1]        # 填好要读的 N 个目标地址（内存区）
4. 写 Ena    = 1          # 开启 IP
5. (等触发/超时启动一轮读周期；DoneIrq 提示完成)
6. 读 Level               # 看 FIFO 里积了多少值
7. 循环 N 次:
     读 RdLast            # 先看：当前值是不是本轮最后一个
     读 RdData            # 后读：取出值，同时弹出 FIFO（RV 副作用）
```

步骤 7 的**顺序绝不能反**——下面用源码解释为什么。

#### 4.2.3 源码精读

权威的寄存器表来自文档：

[doc/Documentation.md:70-79](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md#L70-L79) —— 列出 `Ena`/`RegCnt`/`RdData`/`RdLast`/`Level` 与 `Addr[]` 的地址、模式、位宽与描述，并标注 `RdData`/`RdLast` 仅 AXIMM 模式存在。

整理成中文一表（**本讲核心产出**）：

| 地址 | 名称 | 模式 | 位宽 | 用途 |
|:----:|:----:|:----:|:----:|:-----|
| `0x00` | `Ena` | RW | bit 0 | 使能 IP 核（1=开，0=关）。改 `RegCnt`/`Addr[]` 前必须先置 0 |
| `0x04` | `RegCnt` | RW | 31:0（实际仅低位有效，见下） | 本周期要读的寄存器个数，取 `Addr[]` 前 N 项 |
| `0x08` | `RdData` | **RV** | 31:0 | 读回值 FIFO 出口；**读一次就弹出一个值**。仅 AXIMM |
| `0x0C` | `RdLast` | R | bit 0 | 1 = `RdData` 当前值是本轮读周期最后一个。仅 AXIMM |
| `0x10` | `Level` | R | 31:0 | 内部读值缓冲 FIFO 的当前水位 |
| `0x20` | `Addr[0]` | RW | 31:0 | 要读的第 0 个目标寄存器地址（内存区，见 4.3） |
| `0x24` | `Addr[1]` | RW | 31:0 | 要读的第 1 个目标寄存器地址 |
| … | … | … | … | 依次类推，最多到 `MaxRegCount_g-1` |

> 旁注：文档 [doc/Documentation.md:58-66](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md#L58-L66) 的模式表里列了 `RCW1`，但本 IP 没有任何一个寄存器用到它，那只是 IPIC 寄存器 bank 的通用模式说明，别误以为本工程存在「写 1 清零」的寄存器。

**位字段提取**——文档说 `RegCnt` 是 31:0，但 RTL 并不会用满 32 位。看 wrapper 怎么取这个值：

[hdl/axi_mm_reader_wrp.vhd:326-327](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L326-L327) —— `RegCount` 只取 `reg_wdata(RegIdx_RegCnt_c)` 的低 `log2ceil(MaxRegCount_g)` 位；`Enable` 取 Ctrl 寄存器的 `BitIdx_Ctrl_Ena_c` 位（即 bit 0）。

含义：
- `Ena`：32 位寄存器只用了 bit 0，软件写 `0x01` 即可使能，其余位被忽略。
- `RegCnt`：虽然模式写 31:0，但只有低 \(\lceil \log_2(\text{MaxRegCount\_g}) \rceil\) 位有意义。例如 `MaxRegCount_g=1024` 时只看 bit 9..0（10 位），写超过 1023 的部分被截断。这是因为一次最多读 `MaxRegCount_g` 个寄存器。

**为什么「先读 RdLast、再读 RdData」**——这是文档明确强调的使用约定：

[doc/Documentation.md:82](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md#L82) —— AXIMM 模式下，因为读 `RdData` 会把该值从 FIFO 弹出，所以必须先读 `RdLast`。

根因在 wrapper 的 AXIMM 分支（`g_naxis` generate 块）：

[hdl/axi_mm_reader_wrp.vhd:180-183](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L180-L183) —— `AxiS_Rdy`（FIFO 读使能/弹出）接到 `reg_rd(RegIdx_RdData_c)`；`RdData` 与 `RdLast` 的读回值分别接到 FIFO 头部的数据与 Last 标志。

把这三行翻译成因果链：

- **读 `RdData`** → AXI 从机置 `reg_rd(RegIdx_RdData_c)` 一拍 → 这一位驱动 `AxiS_Rdy` → FIFO 弹出一个值。这正是 `RV`（带副作用读）的物理实现：**读即消费**。
- **读 `RdLast`** → `reg_rd(RegIdx_RdLast_c)` 这一拍**没有**接到任何 FIFO 弹出信号，它只是把 FIFO 头部那个值自带的 Last 标志「**看一眼**」（peek）回送。**读 `RdLast` 不弹 FIFO**。

于是顺序就唯一了：你必须**先**读 `RdLast`（此时它反映的还是当前 FIFO 头部的 Last 标志），**再**读 `RdData`（取出值并弹出）。如果反过来先读 `RdData`，FIFO 已经弹到下一个值，这时再读 `RdLast` 看到的是「下一个值」的 Last 标志，张冠李戴——你将无法可靠地知道「刚才取出的那个值是不是本轮最后一个」，从而无法把数据正确切分成一个个读周期。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：在测试台里亲眼确认「先 RdLast、后 RdData」的读取顺序。
2. **步骤**：打开 `tb/top_tb.vhd`，定位到读取 FIFO 数据的循环（处理 14 个寄存器的一轮）。
3. **现象**：

   [tb/top_tb.vhd:130-131](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L130-L131) —— 同一个循环里，先 `axi_single_expect(RegIdx_RdLast_c*4, ...)` 断言 Last 标志，紧接着 `axi_single_expect(RegIdx_RdData_c*4, ...)` 断言数据值。

   另一处 [tb/top_tb.vhd:384-385](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L384-L385) 也是同样的「先 Last、后 Data」次序。

4. **预期结果**：测试台在两个不同用例里都严格遵循「先读 RdLast、再读 RdData」，因为作者清楚知道反序会导致 Last 标志与数据错配。这正是文档约定的可执行印证。
5. 待本地验证（有兴趣可把这两行在本地仿真里故意对调，观察断言失败）。

#### 4.2.5 小练习与答案

- **练习 1**：软件连续读 3 次 `RdData` 但一次也没读 `RdLast`，会出什么问题？
- **参考答案**：能拿到 3 个数据值（FIFO 被弹出 3 次），但完全不知道这三个值分别属于哪个读周期、哪一个是「本轮最后一个」，无法把流切分成完整的周期包。如果下游依赖包边界（例如每轮 N 个值组成一组），就会错位。
- **练习 2**：为什么 `Level` 寄存器两种输出模式都存在，而 `RdData`/`RdLast` 只在 AXIMM 存在？
- **参考答案**：两种模式背后共用同一个内部 FIFO 做缓冲，软件都需要知道 FIFO 积压程度（避免溢出丢数），故 `Level` 都有。而 `RdData`/`RdLast` 是「把 FIFO 映射到寄存器空间让软件读」的产物；AXIS 模式下数据直接从 `m_axis` 端口流出，软件不经寄存器取数，自然不需要这两个寄存器。

---

### 4.3 RegTable 内存区与 MemOffs

#### 4.3.1 概念说明

`Addr[]`（要读哪些目标地址）这一项很特别：它不是一两个寄存器，而是一张可长达上千项的**地址表**（`RegTable`）。本项目把它实现成一段「内存区」（memory region），和前面 5 个「寄存器」（register region）并排放在同一个 `s00_axi` 地址空间里。

于是软件看到的地址空间被切成两段：

```text
s00_axi 地址空间
┌──────────────────────────┬──────────────────────────────┐
│  寄存器区 (Register)      │       内存区 (RegTable)        │
│  Ena..Level + 少量padding │       Addr[0], Addr[1], ...   │
│  0x00 .. 0x1F (8 个字)    │       0x20 .. (随配置增长)     │
└──────────────────────────┴──────────────────────────────┘
        ▲                                ▲
        └── MemOffs_c 之前的 8 个字       ┘ 内存区从字索引 8 开始
```

`MemOffs_c` 就是这条分界线——它标记「内存区从第几个字开始」。本 IP 里 `MemOffs_c = 8`，即内存区从字索引 8 开始，字节地址 \( 8 \times 4 = \texttt{0x20} \)，与文档 `Addr[0] @ 0x20` 完全一致。

#### 4.3.2 核心流程

为什么分界线偏偏是 8？因为 AXI 从机对「寄存器区」的地址解码要求其大小是 **2 的幂**，这样只需看地址的高几位就能区分寄存器区与内存区。流程：

```text
实际寄存器个数 RegCount_c = 5
        │  向上取整到 2 的幂 (2**log2ceil(5) = 2**3 = 8)
        ▼
寄存器区槽位数 USER_SLV_NUM_REG = 8   ←  寄存器区占 8 个字 = 0x20 字节
        │  寄存器区之后紧接着就是内存区
        ▼
内存区起始字索引 = 8 = MemOffs_c        ←  字节地址 0x20
```

也就是说：**`MemOffs_c` 在数值上等于「向上取整到 2 的幂之后的寄存器槽位数」**。寄存器区被补齐成 8 个字（其中索引 5、6、7 是不用但保留的 padding），内存区正好从补齐后的边界 `0x20` 开始。这样地址解码极其干净：看字节地址的某一位（这里是 bit 5，因为 \( 2^5 = 32 = \texttt{0x20} \)）即可判定落在哪个区。

这个 2 的幂对齐也直接决定了 GUI 里 `s00_axi` 地址宽度的最小值。文档给出公式：

\[ \text{AddrWidth} \ge \lceil \log_2(\text{MaxRegisters} \times 4 + 32) \rceil \]

[doc/Documentation.md:42-43](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md#L42-L43) —— s00_axi 地址宽度至少为 `ceil(log2(MaxRegisters x 4 + 32))`。

式中两项正是两段空间的大小：
- `32` = 寄存器区固定占 32 字节（8 个字，与 `MemOffs_c` 对应）。
- `MaxRegisters × 4` = 内存区最大字节数（每项 4 字节）。

例如默认 `MaxRegCount_g=1024`：\( \lceil \log_2(1024 \times 4 + 32) \rceil = \lceil \log_2(4128) \rceil = 13\) 位。GUI 默认 `AxiSlaveAddrWidth_g=14`，留了 1 位余量，故默认配置够用。

#### 4.3.3 源码精读

**① 寄存器区的 2 的幂对齐**：

[hdl/axi_mm_reader_wrp.vhd:133](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L133) —— `USER_SLV_NUM_REG := 2**log2ceil(RegCount_c)`，把 5 个真实寄存器向上取整到 8 个槽位，交给 AXI 从机 IPIC 做地址解码。

`log2ceil` 来自 `psi_common_math_pkg`，语义是「向上取整的 log2」：`log2ceil(5)=3`，故 `USER_SLV_NUM_REG = 8`。

**② 内存区地址 → RegTable 索引的换算**（软件写 `Addr[i]` 时，wrapper 怎么算出落在 RAM 的第几项）：

[hdl/axi_mm_reader_wrp.vhd:328](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L328) —— `RegCfg_Idx => mem_addr(log2ceil(MaxRegCount_g)+1 downto 2)`，把字节地址右移 2 位（除 4）转成字索引，并截取需要的位数作为 RegTable 的 RAM 地址。

解读：`mem_addr` 是 AXI 从机给出的字节地址。`downto 2` 等于**除以 4**（丢掉低 2 位字节偏移），得到字索引；取到 `log2ceil(MaxRegCount_g)+1` 位则覆盖最多 `MaxRegCount_g` 项的寻址范围。对 `MaxRegCount_g=1024`，即取 bit 11 downto 2（10 位 = 1024 项）。

**③ `MemOffs_c` 的实测用法**（关键印证——它确实是个**字索引**）：

[tb/top_tb.vhd:267](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L267) —— 测试台填 `Addr[]` 时用 `axi_single_write((MemOffs_c+i)*4, ...)`，即第 i 项的字节地址 = `(8+i)*4`。i=0 → `0x20`，正是 `Addr[0]`。

**④ 一个重要事实：`MemOffs_c` 并不被 RTL 使用**。
全仓库搜索可知，`MemOffs_c` 只出现在测试台（4.3.3 ③），**不出现在任何 RTL 文件里**。硬件上寄存器区/内存区的真实分界是由 `psi_common_axi_slave_ipif` 根据 `USER_SLV_NUM_REG` 内部算出的，`MemOffs_c` 只是一个**给软件和测试台用的「文档型常量」**，用来镜像「内存区从 0x20 开始」这个事实。

#### 4.3.4 代码实践（源码阅读型 + 计算）

1. **目标**：手算几个 `Addr[]` 地址与 `s00_axi` 地址宽度，验证对地址空间的理解。
2. **步骤**：
   - 用 `MemOffs_c=8` 计算 `Addr[0]`、`Addr[3]`、`Addr[10]` 的字节地址。
   - 假设 `MaxRegCount_g=1024`、`AxiSlaveAddrWidth_g=14`，验证地址宽度公式是否满足。
3. **计算**：
   - `Addr[0]` = `(8+0)*4` = `0x20`
   - `Addr[3]` = `(8+3)*4` = `11*4` = `44` = `0x2C`
   - `Addr[10]` = `(8+10)*4` = `18*4` = `72` = `0x48`
   - 地址宽度下限 \( \lceil \log_2(1024 \times 4 + 32) \rceil = \lceil \log_2(4128) \rceil = 13 \le 14 \)，默认配置满足。
4. **预期结果**：计算结果与文档「`Addr[0]=0x20`、每项 +4」一致；14 位地址可寻址 \( 2^{14}=16384 \) 字节 > 4128 字节，留有余量。
5. 待本地验证（在 Vivado 里改 `MaxRegCount_g` 为更大值，重新算地址宽度下限，确认 GUI 提示与之吻合）。

#### 4.3.5 小练习与答案

- **练习 1**：为什么寄存器区要补齐成 2 的幂（8 个字）而不是就用 5 个字？
- **参考答案**：AXI 从机的地址解码需要寄存器区大小是 2 的幂，才能用地址的某一位作为「区选择」开关（`0x20` 正是 \( 2^5 \)，看 bit 5 即可区分寄存器区/内存区）。5 不是 2 的幂，强行使用会让地址解码逻辑变复杂且产生非法地址空洞，故向上取整到 8。
- **练习 2（维护陷阱）**：假设有人把寄存器扩到 `RegCount_c=9`（即 `USER_SLV_NUM_REG` 变成 16），但忘了改 `MemOffs_c`，会发生什么？
- **参考答案**：`MemOffs_c` 仍为 8，但硬件内存区实际从字索引 16（字节 `0x40`）开始。于是软件/测试台按 `(MemOffs_c+i)*4 = (8+i)*4` 写下去的 `Addr[]` 会落在 `0x20..0x3C`——这其实是新增的寄存器区槽位（padding），而不是 RegTable 内存区，配置写不到正确的 RAM 里，IP 行为错误。这正是 `MemOffs_c` 作为**手维护常量**的隐患：它没有从 `USER_SLV_NUM_REG` 派生，扩寄存器时必须人工同步。修复方向是让 `MemOffs_c := 2**log2ceil(RegCount_c)` 自动派生（当前代码未这么做，属于可改进点）。

---

## 5. 综合实践

把本讲三块知识串起来，完成下面这张「软件驱动卡」的设计。

**任务背景**：你要写一段运行在 Zynq PS 上的 C 代码，配置 IP 读 3 个外部寄存器（地址 `0x4000_0000`、`0x4000_0004`、`0x4000_0008`），使能后触发一次读周期，然后把读回的 3 个值正确取出来（AXIMM 模式）。

**要求**：

1. **填出完整寄存器表**：列出本讲涉及的所有寄存器的「地址 / 名称 / 模式 / 位宽 / 用途」（直接抄录 4.2.3 的表，作为你的速查卡）。
2. **写出操作步骤**（用伪代码或 C 风格），按顺序给出对每个地址的读/写动作，包括：
   - 关 IP（写 `Ena=0`）
   - 写 `RegCnt=3`
   - 往 `Addr[0..2]` 写 3 个目标地址（**自己算出** `(MemOffs_c+i)*4` 的字节地址）
   - 开 IP（写 `Ena=1`）
   - 循环 3 次，**先读 `RdLast`、再读 `RdData`**
3. **解释**两个关键点（一句话各一条）：
   - 为什么改 `RegCnt` 和 `Addr[]` 之前要先写 `Ena=0`？（提示：见文档对 `RegCnt` 的说明 [doc/Documentation.md:73](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md#L73)）
   - 为什么取数循环里必须「先 `RdLast` 后 `RdData`」？（提示：回顾 4.2.3 的因果链——读 `RdData` 会驱动 `AxiS_Rdy` 弹 FIFO，读 `RdLast` 不会）。

**参考骨架**（`Xil_Out32`/`Xil_In32` 是 Xilinx 的 32 位寄存器读写原语，u3-l1 会详讲）：

```c
// 示例代码（非项目原有，仅供本练习）
#define BASE 0x43C00000UL
#define ENA    (BASE + 0x00)
#define REGCNT (BASE + 0x04)
#define RDDATA (BASE + 0x08)
#define RDLAST (BASE + 0x0C)
#define LEVEL  (BASE + 0x10)
// Addr[i] = BASE + (8+i)*4

Xil_Out32(ENA, 0);                 // 先禁用
Xil_Out32(REGCNT, 3);              // 读 3 个
Xil_Out32(BASE + (8+0)*4, 0x40000000);
Xil_Out32(BASE + (8+1)*4, 0x40000004);
Xil_Out32(BASE + (8+2)*4, 0x40000008);
Xil_Out32(ENA, 1);                 // 使能
// ...等待 DoneIrq 或读 Level==3 ...
for (int i = 0; i < 3; i++) {
    int isLast = Xil_In32(RDLAST) & 0x1;   // 先读（peek，不弹）
    int val    = Xil_In32(RDDATA);          // 后读（取值并弹 FIFO）
    // process(val, isLast)
}
```

> 待本地验证：上述骨架的真实 API（`SetRegTable`/`ReadFifoPacket` 等）封装在 C 驱动里，将在 [u3-l1 C 软件驱动](u3-l1-c-driver.md) 展开；本练习只要求把寄存器层的地址与顺序理清。

## 6. 本讲小结

- IP 把配置与状态铺成一段 `s00_axi` 地址空间：**寄存器区**（`Ena`/`RegCnt`/`RdData`/`RdLast`/`Level`）+ **内存区**（`Addr[]` 即 RegTable），两段共享同一地址空间。
- 所有地址以可读名字集中定义在 `hdl/definitions_pkg.vhd`，`RegCount_c` 由最后一个索引派生；软件看到的字节地址 = 字索引 × 4。
- `Ena`(bit0) 与 `RegCnt`(低位有效) 是控制字；`Level` 反映 FIFO 水位；`RdData`/`RdLast` **仅 AXIMM 模式存在**。
- `RdData` 是唯一的 `RV`（带副作用读）寄存器——读它即弹 FIFO（`AxiS_Rdy <= reg_rd(RegIdx_RdData_c)`）；`RdLast` 只是 peek 不弹。因此 AXIMM 取数必须**先读 `RdLast`、再读 `RdData`**，测试台两处印证了该顺序。
- 寄存器区大小被 `USER_SLV_NUM_REG = 2**log2ceil(RegCount_c)` 向上取整成 2 的幂（=8），内存区从 `0x20` 开始；`MemOffs_c=8` 是与之对应的字索引常量，但**仅被测试台使用、不被 RTL 使用**，扩寄存器时需人工同步（隐患）。
- GUI 的 `s00_axi` 地址宽度下限为 \( \lceil \log_2(\text{MaxRegisters}\times 4 + 32) \rceil \)，其中 32 来自寄存器区、`MaxRegisters×4` 来自内存区。

## 7. 下一步学习建议

寄存器地图是「软件视角」，但要真正理解这些寄存器在硬件里如何被消费，建议接着读：

- **[u2-l3 核心 FSM：双进程状态机](u2-l3-core-fsm.md)**：看 `RegCount`、`Enable` 如何驱动状态机遍历 `Addr[]` 表逐个发起读取，以及 `DoneIrq` 如何在读完最后一个后拉高。
- **[u2-l5 AXI 从机配置接口（wrapper）](u2-l5-axi-slave-wrapper.md)**：深入 `psi_common_axi_slave_ipif` 如何把 AXI 事务解码成本讲这些 `reg_rd/reg_wr/mem_addr` 信号，彻底打通「软件写 → 硬件信号」一整条路径。
- **[u3-l1 C 软件驱动](u3-l1-c-driver.md)**：看驱动如何把本讲的裸地址操作封装成 `SetEnable`/`SetRegTable`/`ReadFifoPacket` 等 API，以及错误码如何对应「IP 未禁用」「FIFO 空」等本讲提到的约束。
