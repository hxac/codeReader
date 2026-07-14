# 异步 FIFO（olo_base_fifo_async）

## 1. 本讲目标

学完本讲后，读者应该能够：

- 说清楚「为什么把一个二进制地址指针直接从一个时钟域传到另一个时钟域是危险的」，以及格雷码如何化解这个危险。
- 对照源码讲出 `olo_base_fifo_async` 的双时钟域指针同步流程：写/读指针各自在本时钟域维护，转成格雷码后经同步器送给对侧，对侧再转回二进制用于判满/判空。
- 解释为什么 FIFO 深度必须是 2 的幂、为什么地址要多留 1 个最高位来区分「满」与「空」。
- 把 `olo_base_fifo_async` 当作一个通用的「时钟跨越（Clock Domain Crossing, CDC）」实体来使用，理解它的双复位 `RstIn/RstOut`、约束要求以及 `Optimization_g`、`SyncStages_g` 的含义。
- 在「带缓冲的流式 CDC」和「无 RAM 的逐拍握手 CDC」之间做取舍，知道何时该用异步 FIFO、何时该用 `olo_base_cc_handshake`。

## 2. 前置知识

本讲假设你已学过 **u2-l4（同步 FIFO）**，已经掌握：

- FIFO 的基本概念：写指针、读指针、填充度（level）、满/空标志。
- AXI-S 的 Valid/Ready 握手与反压（back-pressure）。
- fall-through（FWFT，首字直通）输出方式。
- 几乎满 / 几乎空（AlmFull / AlmEmpty）的用途。

本讲在这些之上，多出一个核心难点：**读写指针不在同一个时钟里**。为此需要补两个基础概念：

- **亚稳态（metastability）与同步器**：当一个信号被一个与其源不同步的时钟采样时，触发器可能输出一段时间的不确定电平（既不是稳定的 0 也不是稳定的 1），这叫亚稳态。工程上用「两级（或多级）触发器串接」的同步器把亚稳态传播到后级的概率压到极低。Open Logic 把这个同步器做成 [`olo_base_cc_bits`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_bits.vhd)，并带上各厂商的综合属性强制其不被优化。
- **格雷码（Gray code）**：一种「相邻两个数只有 1 位不同」的编码。它是异步 FIFO 跨时钟域安全传输多 bit 指针的关键，第 4.2 节会专门讲。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| [`src/base/vhdl/olo_base_fifo_async.vhd`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_async.vhd) | 本讲主角，异步 FIFO 的全部实现（指针、判满判空、RAM 例化、指针跨域同步、复位跨域）。 |
| [`doc/base/olo_base_fifo_async.md`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_fifo_async.md) | 官方文档，给出泛型/端口表与架构说明图。 |
| [`src/base/vhdl/olo_base_pkg_logic.vhd`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_logic.vhd) | 提供 `binaryToGray` / `grayToBinary` 两个格雷码转换函数。 |
| [`src/base/vhdl/olo_base_cc_bits.vhd`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_bits.vhd) | 多 bit 同步器实体，FIFO 内部用它把格雷码指针从一侧同步到另一侧。 |
| [`src/base/vhdl/olo_base_cc_reset.vhd`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_reset.vhd) | 复位跨域实体，FIFO 内部用它保证两时钟域同时进入/退出复位。 |
| [`src/base/vhdl/olo_base_cc_handshake.vhd`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_handshake.vhd) | 对照实体：无 RAM 的逐拍握手 CDC，第 4.4 节用于取舍对比。 |
| [`doc/base/clock_crossing_principles.md`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/clock_crossing_principles.md) | Open Logic 所有时钟跨越实体的通用原则：约束、复位穿越、选型表。 |
| [`test/base/olo_base_fifo_async/olo_base_fifo_async_tb.vhd`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_async/olo_base_fifo_async_tb.vhd) | VUnit 测试台，含「写满」「不同占空比」等用例，是本讲代码实践的依据。 |

## 4. 核心概念与源码讲解

### 4.1 双时钟域指针同步

#### 4.1.1 概念说明

同步 FIFO（u2-l4）的读写指针跑在同一个时钟里，判满判空只要比较两个指针即可。**异步 FIFO 的读写指针跑在两个互不相关（频率与相位都可能不同）的时钟里**，于是出现一个根本矛盾：

> 写侧要判「满」，必须知道读侧的读指针走到了哪里；读侧要判「空」，必须知道写侧的写指针走到了哪里。而这两个指针分别属于不同的时钟域，不能直接比较。

解决办法是 **各自只在本时钟域维护自己的指针，把指针的值「快照」一份送到对侧**：

- 写侧维护写指针 `WrAddr`，把它送给读侧（读侧据此判空、据此读 RAM）。
- 读侧维护读指针 `RdAddr`，把它送给写侧（写侧据此判满）。

这个「送过去」就是一次跨时钟域传输。但指针是一个**多 bit 信号**，不能像单 bit 那样直接打两拍同步——多位同时翻转时，同步器可能采样到错误的中间值。所以指针要先转成格雷码再传输（详见 4.2）。本节先看整体数据通路。

#### 4.1.2 核心流程

异步 FIFO 的整体结构可以用下面这张文字流程图概括：

```
        ┌──────────── 写时钟域 (In_Clk) ────────────┐   ┌──────── 读时钟域 (Out_Clk) ────────────┐
        │                                            │   │                                         │
 In_Data│  WrAddr(二进制, 本域自增)                   │   │              RdAddr(二进制, 本域自增)    │
 ──────►│     │                                      │   │      ▲                                  │
        │     │ binaryToGray                         │   │      │ grayToBinary                      │
        │     ▼                                      │   │      │                                  │
        │  WrAddrGray ──► [cc_bits 同步器] ─────────────┼──► WrAddrGray(读侧副本) ──► WrAddr(读侧)│
        │                                            │   │      │                                  │
        │  RdAddr(写侧副本) ◄── grayToBinary ◄────────┼──┤ [cc_bits 同步器] ◄── RdAddrGray ◄─────┤
        │      ▲                                     │   │      │ binaryToGray                     │
        │      │                                     │   │      │                                  │
        │  WrAddr - RdAddr → In_Level / Full / Empty │   │  WrAddr - RdAddr → Out_Level / Valid   │
        │                                            │   │                                         │
        │           RAM 写口 (Wr_Addr/Wr_Data)        │   │      RAM 读口 (Rd_Addr → Out_Data)      │
        └────────────────────────────────────────────┘   └─────────────────────────────────────────┘
                              │  olo_base_ram_sdp (IsAsync_g=true, 双时钟双口 RAM)  │
                              └────────────────────────────────────────────────────┘
                       复位：olo_base_cc_reset 把 In_Rst/Out_Rst 互送到两侧 → RstInInt/RstOutInt
```

要点：

1. 写指针在写时钟域自增，读指针在读时钟域自增——**各自只在本域更新**，永不对对侧指针直接写。
2. 每个指针经过 `binaryToGray → cc_bits 同步器 → grayToBinary` 三步到达对侧，成为对侧的「副本指针」。
3. 判满判空用的是「本域指针 − 对侧副本指针」得到的 level，两侧各自算一份，所以 `In_Level` 与 `Out_Level` 都存在且各自同步于本侧时钟。
4. 存储体是一个**双时钟双口 RAM**（`olo_base_ram_sdp` 的 `IsAsync_g=true`），写口在写时钟、读口在读时钟，数据本身不需要跨域握手——靠指针同步保证读口永远只读已写完的位置。

#### 4.1.3 源码精读

**实体与泛型**。异步 FIFO 的泛型与同步 FIFO 高度相似，多了两个与时钟跨越直接相关的项：`Optimization_g`（SPEED/LATENCY）和 `SyncStages_g`（2~4 级同步器）。

[olo_base_fifo_async.vhd:35-47](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_async.vhd#L35-L47) —— 泛型定义，其中 `Depth_g` 注释明确要求 2 的幂，`SyncStages_g` 范围 2..4。

[olo_base_fifo_async.vhd:48-75](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_async.vhd#L48-L75) —— 端口定义。注意它有**两组时钟/复位**：写侧 `In_Clk/In_Rst`、读侧 `Out_Clk/Out_Rst`，以及对应两侧的 `In_RstOut/Out_RstOut`（复位穿越输出，见 4.3.2）。

**两个 record 把「本域指针 + 对侧副本指针」收纳在一起**。这正是 u1-l5 讲过的「两进程法 + record」在跨时钟域场景下的应用——写侧与读侧各一个 record、各一组进程：

[olo_base_fifo_async.vhd:87-101](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_async.vhd#L87-L101) —— 写侧 `TwoProcessIn_r` 含 `WrAddr`（本域写指针）、`WrAddrGray`（其格雷码，准备送出）、`RdAddr`（**来自读侧的副本**）、`WrAddrReg/DataReg`（SPEED 模式的流水线寄存器）、`RamWr`；读侧 `TwoProcessOut_r` 含 `RdAddr`、`RdAddrGray`、`WrAddr`（**来自写侧的副本**）、`OutLevel`。

**地址位宽多留 1 个最高位**，这是异步 FIFO 区分「满」与「空」的关键（4.2.3 详述）：

[olo_base_fifo_async.vhd:84-85](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_async.vhd#L84-L85) —— `AddrWidth_c = log2ceil(Depth_g)+1`（多 1 位），`RamAddrWidth_c = log2ceil(Depth_g)`（真正寻址 RAM 的位数）。

**写侧判满 + level 计算**。level 就是「写指针 − 读指针副本」，满则用一个不依赖进位链的等价判据：

[olo_base_fifo_async.vhd:150-168](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_async.vhd#L150-L168) —— 先算 `InLevel_v := ri.WrAddr - ri.RdAddr`；判满时**不**写 `if InLevel_v = Depth_g`，而是比较「最高位不同且低位全等」。因为 `Depth_g` 是 2 的幂，二者等价，但后者只比一组触发器位、不依赖减法进位链，时序更优。不满才把 `In_Ready` 拉高、才真正执行写（`vi.WrAddr := ri.WrAddr + 1`、`vi.RamWr := '1'`）。

**读侧判空 + level 计算**，与写侧对称：

[olo_base_fifo_async.vhd:198-218](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_async.vhd#L198-L218) —— `OutLevel` 由读侧副本 `ro.WrAddr - ro.RdAddr` 得到；`OutLevel=0` 即空、`Out_Valid='0'`；非空则 `Out_Valid='1'` 且 `Out_Ready='1'` 时读指针自增，并把 `vo.RdAddr` 低位作为 RAM 读地址 `RamRdAddr`。

**两个时序进程各跟各的时钟**，复位只清状态指针（不清 RAM 内容）：

[olo_base_fifo_async.vhd:246-270](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_async.vhd#L246-L270) —— `p_seq_in` 跟 `In_Clk`，`p_seq_out` 跟 `Out_Clk`；复位用进程末尾覆盖写法（见 u1-l5），只把指针与 `RamWr` 清零。

#### 4.1.4 代码实践

**实践目标**：跑通官方测试台里的「写满」用例，亲眼看 `In_Full/Out_Full` 拉起、溢出数据被丢弃、读回顺序与写入一致。

**操作步骤**（参考 u1-l4 的 VUnit 运行方式）：

1. 进入 `sim/` 目录。
2. 运行异步 FIFO 的写满用例（默认 GHDL）：
   ```bash
   python run.py "*fifo_async*WriteFullFifo*"
   ```
   > 该 glob 模式用于在 VUnit 全名 `olo_tb.olo_base_fifo_async_tb.<config>.WriteFullFifo` 上做匹配。若你的 VUnit 版本对 glob 的处理不同，可改用 `python run.py --list` 先看全名再精确指定。具体匹配串待本地验证。
3. 若只想验证一组配置，可在 [`sim/test_configs/olo_base.py`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py#L76-L97) 里看到 `fifo_async_tb` 会枚举 `RamBehavior ∈ {RBW,WBR}`、`Depth ∈ {32,128}`、`SyncStages ∈ {2,4}`、`Optimization ∈ {SPEED,LATENCY}` 等组合，每个组合都会注册出 `WriteFullFifo` 用例。

**需要观察的现象**（对照测试台断言）：

[olo_base_fifo_async_tb.vhd:262-308](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_async/olo_base_fifo_async_tb.vhd#L262-L308) —— 该用例先写满 `Depth_g` 个字，断言 `In_Full='1'`、`Out_Full='1'`、`In_Level=Depth_g`、`Out_Level=Depth_g`；再继续写两个额外字（`0xABCD`、`0x8765`），断言 level 仍为满（说明溢出被丢弃）；最后顺序读回，断言每个 `Out_Data = i`（说明无丢失、无乱序）。

**预期结果**：全部 `check_equal` 通过，测试报 PASS。如果失败，说明满标志或数据完整性出了问题——这正是异步 FIFO 最不能出错的地方。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `In_Level` 用减法 `ri.WrAddr - ri.RdAddr` 而不是用一个单独的 up/down 计数器？
**答案**：因为 `ri.RdAddr` 是从读侧同步过来的「延迟副本」，用减法得到的是写侧视角下保守的填充度；单独的 up/down 计数器在双时钟域里无法安全维护（加减来自不同时钟），而减法只需同步一个只增不减的指针，天然安全。

**练习 2**：判满时代码没有用 `if InLevel_v = Depth_g`，而是比较「最高位不同且低位相等」。请说明这两种写法为什么在 `Depth_g` 为 2 的幂时等价。
**答案**：当深度为 2 的幂时，写满意味着写指针比读指针正好多走了一整圈，即二进制上「最高位（圈数位）翻转、其余低位完全相同」。这与 `level = Depth_g` 是同一个事实的两种表述；后者不需要做减法、不依赖进位链，时序更短。

---

### 4.2 格雷码计数器

#### 4.2.1 概念说明

4.1 留了一个问题：**为什么指针必须先转成格雷码再跨时钟域？**

考虑普通二进制计数器从 `0111`（7）加到 `1000`（8）：4 个 bit 同时翻转。如果对侧时钟恰好在翻转的中间时刻采样，由于各 bit 的走线延迟不同，同步器可能采到 `0000`、`1111`、`1011`……任何一个「中间垃圾值」。这对 FIFO 是致命的——错误的指针会误判满/空，导致**丢数据或重复读**。

格雷码（Gray code）是一种「任意两个相邻值只有 1 位不同」的编码。于是：

> 当指针递增时，跨域信号最多只有 1 位在变。同步器要么采到「旧值」，要么采到「新值」，都是合法的指针值，绝不会出现第三种垃圾值。

这就把「多位同时翻转的同步不可靠」问题，降级为「至多延迟一拍看到新指针」——而 FIFO 的判满/判空本来就是**保守的**（宁可早报满、早报空），所以晚一拍看到新指针只会让容量短暂地被低估一点点，不会出错。

#### 4.2.2 核心流程

二进制转格雷（并行，每个 bit 一个异或）：

\[
g_{n-1} = b_{n-1}, \qquad g_i = b_i \oplus b_{i+1}\ (i < n-1)
\]

格雷转二进制（带链式依赖，需要寄存一拍）：

\[
b_{n-1} = g_{n-1}, \qquad b_i = g_i \oplus b_{i+1}
\]

注意 `grayToBinary` 的每一位都依赖比它高一位的**二进制**结果，形成一条从高位到低位的链，所以它不能像 `binaryToGray` 那样一行并行算完，需要循环且通常落进寄存器。源码注释里「Gray->Bin involves some logic, needs additional FF」说的就是这个。

> **为什么深度必须是 2 的幂？** 格雷码「相邻值只差 1 位」的性质，只在计数器**在 2 的幂处自然回绕**时成立（例如 3 位格雷码 `000→001→011→010→110→111→101→100→000`，末尾 `100` 回到 `000` 也只差 1 位）。如果深度不是 2 的幂，回绕点处会一次翻转多位，跨域就不安全了。源码里那条 `assert` 就是在运行期把这个要求钉死。

#### 4.2.3 源码精读

**转换函数本体**（来自 base 包）：

[olo_base_pkg_logic.vhd:167-172](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_logic.vhd#L167-L172) —— `binaryToGray`：`Gray_v := binary xor ('0' & binary(high downto low+1))`，正是上面那个并行异或公式，一行实现。

[olo_base_pkg_logic.vhd:175-181](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_logic.vhd#L175-L181) —— `grayToBinary`：最高位直接取，其余位循环异或高一位的累计结果，体现链式依赖。

**FIFO 内的调用点**——两个方向的转换都在组合进程 `p_comb` 末尾完成，结果落进 record 寄存器：

[olo_base_fifo_async.vhd:231-238](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_async.vhd#L231-L238) —— `binaryToGray(本域指针)` 算出待送出的格雷码指针（`vi.WrAddrGray`/`vo.RdAddrGray`）；`grayToBinary(对侧同步来的格雷码)` 算出对侧副本的二进制值（`vi.RdAddr`/`vo.WrAddr`）。

**深度必须是 2 的幂的强制断言**：

[olo_base_fifo_async.vhd:128-130](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_async.vhd#L128-L130) —— `assert log2(Depth_g) = log2ceil(Depth_g)`，仅当 `Depth_g` 是 2 的幂时二者相等，否则用 `errorMessage` 报错。

**多 bit 同步器 `olo_base_cc_bits`**——把格雷码指针安全地送过时钟边界：

[olo_base_cc_bits.vhd:119-142](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_bits.vhd#L119-L142) —— 输入端先打一拍（`RegIn`），再用 `SyncStages_g` 级触发器在目的时钟域逐级同步，输出取最后一级。注意它对每一级寄存器都加了 `shreg_extract=suppress`、`async_reg`、`dont_merge` 等跨厂商综合属性，**防止综合工具把这些同步寄存器合并或抽成移位寄存器**——一旦合并，同步器就失效了。这正是 u2-l1 讲过的 `olo_base_pkg_attribute` 的典型用法。

**FIFO 内的两个同步器实例**——分别把写指针送到读侧、读指针送到写侧：

[olo_base_fifo_async.vhd:296-328](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_async.vhd#L296-L328) —— `i_cc_wr_rd` 把 `WrAddrGrayIn`（写指针格雷码）从 `In_Clk` 同步到 `Out_Clk`；`i_cc_rd_wr` 反向把 `RdAddrGrayIn` 从 `Out_Clk` 同步到 `In_Clk`。两者都用 `olo_base_cc_bits`、位宽都是 `AddrWidth_c`（含那个区分满/空的最高位）。

#### 4.2.4 代码实践

**实践目标**：用一个 3 位二进制/格雷码对照的小例子，亲手验证「二进制进位时多位翻转、格雷码每次只翻 1 位」。

**操作步骤**（纯阅读 + 推演型实践，可在纸上完成，也可写一个 8 行的 VHDL 打印）：

1. 写出 0..7 的 3 位二进制：`000,001,010,011,100,101,110,111`。
2. 用 `binaryToGray` 公式逐个算出对应格雷码。
3. 标出每次 +1 时**翻转的 bit 数**。

**需要观察的现象**：二进制 `011→100` 一次翻 4 位；格雷码序列每一步都只翻 1 位，且首尾相接（`100→000` 回到起点也只翻 1 位）。

**预期结果**：得到经典 3 位格雷码序列 `000,001,011,010,110,111,101,100`，每相邻两项汉明距离为 1。这正是「指针跨时钟域只可能被采成旧值或新值」的几何原因。

> 想用仿真验证可参考 `olo_base_pkg_logic_tb`（base 包自带测试台），它对 `binaryToGray/grayToBinary` 有往返一致性断言。

#### 4.2.5 小练习与答案

**练习 1**：如果有人把 `Depth_g` 设成 6（非 2 的幂），会怎样？
**答案**：仿真期 `assert` 会报 `only power of two Depth_g is allowed`（severity error）；即使绕过断言综合成功，回绕点处多位翻转会让跨域指针被采成错误值，可能导致数据丢失或重复——所以这是硬性约束。

**练习 2**：为什么 `binaryToGray` 可以纯组合直接用，`grayToBinary` 却「needs additional FF」？
**答案**：`binaryToGray` 每个 bit 只依赖输入的同位与高一位，完全并行，一行组合逻辑即可；`grayToBinary` 的每一位依赖更高一位的**二进制**结果（链式进位），形成长组合路径，故把它落进寄存器（record 字段）以切断路径、改善时序。

**练习 3**：异步 FIFO 用格雷码后，对侧看到的指针「可能滞后一拍」。这会让 FIFO 的满/空判断偏向哪个方向？会不会出错？
**答案**：会让满/空判断**偏保守**（早报满、早报空），即容量被短暂低估，但绝不会出错——读侧不会读到未写完的位置，写侧不会覆盖未读走的位置。代价仅是理论吞吐略低于物理上限。

---

### 4.3 作为通用时钟跨越实体使用

#### 4.3.1 概念说明

Open Logic 把 `olo_base_fifo_async` 不仅当作「缓存」，更当作一个**通用的时钟跨越（CDC）实体**。这一点很重要：当你的数据流需要从一个时钟域搬到另一个时钟域，且两边时钟完全异步时，异步 FIFO 通常是**最安全、吞吐最高**的选择，因为它自带：

- 双口 RAM 做缓冲（吸收读写速率差）；
- 格雷码指针做安全的多 bit 跨域；
- 内置复位跨域，保证两时钟域同步进入/退出复位；
- 标准 AXI-S 接口，自带反压。

官方文档开头就点明了这一身份：

[olo_base_fifo_async.md:27-28](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_fifo_async.md#L27-L28) —— 「An asynchronous FIFO is a clock-crossing and hence this block follows the general clock-crossing principles.」

#### 4.3.2 核心流程

把异步 FIFO 当 CDC 用，需要理解三件配套的事：

**(a) 双复位与复位穿越 `RstIn/RstOut`。** FIFO 有两个时钟域，两个域里的逻辑必须**同时**复位，否则会出现一个域在跑、另一个域还在复位的危险窗口。Open Logic 用 `olo_base_cc_reset` 保证两域至少共同保持复位一拍再同时释放。实体为此提供 `In_Rst/Out_Rst`（复位输入）和 `In_RstOut/Out_RstOut`（复位状态输出）——**外围需要随 FIFO 一起复位的逻辑，必须接到 `RstOut` 而不是各自的本地复位**。

**(b) 约束（constraints）。** 所有时钟跨越都需要告诉综合/实现工具「这两条路径是异步的，别按同周期时序去检查」，否则工具会报假违例。Open Logic 提供 AMD(Vivado) 的 scoped 约束自动应用，其它厂商需手动加 `set_max_delay`。

**(c) 对称位宽与位宽扩展。** 这个 FIFO 是**对称**的（读写同位宽）。若要 n:xn 或 xn:n，可在写/读侧外接 `olo_base_wconv_n2xn`/`olo_base_wconv_xn2n`。

#### 4.3.3 源码精读

**内置复位跨域**：

[olo_base_fifo_async.vhd:331-342](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_async.vhd#L331-L342) —— `i_rst_cc` 是一个 `olo_base_cc_reset`，输入两侧的 `In_Rst/Out_Rst`，输出 `RstInInt/RstOutInt`；这两个内部复位既驱动 `p_seq_in/p_seq_out`、又通过 `In_RstOut/Out_RstOut` 引出给外围。所以外围逻辑应接 `RstOut` 而非本地 `Rst`。

**双时钟双口 RAM**：

[olo_base_fifo_async.vhd:278-294](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_async.vhd#L278-L294) —— 例化 `olo_base_ram_sdp` 并把 `IsAsync_g` 设为 `true`，使写口用 `In_Clk`、读口用独立的 `Rd_Clk => Out_Clk`。这就是 u2-l3 讲过的简单双口 RAM 的异步变体。

**SPEED / LATENCY 取舍**——通过多路选择是否插入额外流水线寄存器：

[olo_base_fifo_async.vhd:272-276](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_fifo_async.vhd#L272-L276) —— `Optimization_g="SPEED"` 时，RAM 的写地址/写数据/写使能分别取自已经多寄存一拍的 `ri.WrAddrReg/ri.DataReg/ri.RamWr`，等于在 RAM 写口前多插了一级寄存器，换更高的 fmax、代价是写入到读侧可见的延迟更大；`"LATENCY"` 时直接用未额外寄存的信号（甚至 `ri_next` 组合值），换最小延迟、代价是 fmax 更低。

[olo_base_fifo_async.md:49](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_fifo_async.md#L49) —— 文档原文：`"LATENCY"` 最小化「写出到读可见」的延迟，`"SPEED"` 最大化时钟频率（以更多延迟为代价）。

**约束原则**（来自通用时钟跨越文档）：

[clock_crossing_principles.md:9-16](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/clock_crossing_principles.md#L9-L16) —— 给出两条 `set_max_delay -datapath_only` 约束模板（src↔dst 两个方向），并说明自动 scoped 约束目前仅 AMD 工具支持，其它工具需手动加。

#### 4.3.4 代码实践

**实践目标**：实例化一个 `olo_base_fifo_async`，接两个不同频率的时钟，验证它在「写快读慢」时正确反压（满）且不丢数据。

**操作步骤**：

1. 新建一个最小测试台（**示例代码**，非项目原有文件），核心是把写时钟设得比读时钟快：

   ```vhdl
   -- 示例代码：最小异步 FIFO 实例化（节选）
   constant CLK_IN_FREQ  : real := 100.0e6;   -- 写侧 100 MHz
   constant CLK_OUT_FREQ : real := 50.0e6;    -- 读侧 50 MHz（写快读慢）

   i_dut : entity work.olo_base_fifo_async
       generic map ( Width_g => 16, Depth_g => 32,
                     AlmFullOn_g => true, AlmFullLevel_g => 29 )
       port map (
           In_Clk => clk_in,  In_Rst  => rst_in,
           Out_Clk => clk_out, Out_Rst => rst_out,
           In_Data => in_data, In_Valid => in_valid, In_Ready => in_ready,
           Out_Data => out_data, Out_Valid => out_valid, Out_Ready => out_ready,
           In_Full => in_full, In_Level => in_level,
           others => open );
   ```
   > 这个骨架其实就是官方测试台的简化版。完整的、可立即运行的版本见 [`olo_base_fifo_async_tb.vhd`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_async/olo_base_fifo_async_tb.vhd#L50-L53)，它的写时钟 100 MHz、读时钟 83.333 MHz，本身就是「写快读慢」场景。

2. 直接复用官方测试台的 `DiffDutyCycle` 用例来验证「不丢数据」：

   ```bash
   python run.py "*fifo_async*DiffDutyCycle*"
   ```

**需要观察的现象**：在写快读慢、且写侧持续写若干字时，`In_Full`（或 `In_Level` 达到 `AlmFullLevel_g`）会被拉起，`In_Ready` 随之拉低形成反压；读侧最终收到的数据严格等于写入的数据、顺序一致。

[olo_base_fifo_async_tb.vhd:410-465](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_async/olo_base_fifo_async_tb.vhd#L410-L465) —— `DiffDutyCycle` 用例遍历写/读各种占空比组合，每次写 5 个字再读 5 个字，逐字断言 `Out_Data = i`，最后断言 `Out_Empty='1'`（数据全部读走、无残留也无丢失）。

**预期结果**：所有占空比组合下数据序号完全对齐、读完即空，测试 PASS。这就同时验证了「满/反压」与「无丢失」两件事。

#### 4.3.5 小练习与答案

**练习 1**：外围逻辑（比如写侧的一个状态机）应该接 `In_Rst` 还是 `In_RstOut`？为什么？
**答案**：接 `In_RstOut`。`In_Rst` 只是复位请求输入，可能只来自一个时钟域；`In_RstOut` 反映的是「经复位跨域后，写时钟域当前是否真的处于复位」，能保证外围逻辑与 FIFO 内部写域逻辑同时进/出复位，避免危险窗口。

**练习 2**：你的设计里写时钟 200 MHz、读时钟 100 MHz，应该选 `Optimization_g="SPEED"` 还是 `"LATENCY"`？
**答案**：若 200 MHz 写域时序紧张，选 `"SPEED"`（多一级寄存器换 fmax）；若更在乎写入后多快能在读侧看到，且时序余量充足，选 `"LATENCY"`。二者都正确，只是时序/延迟取舍。

---

### 4.4 与 olo_base_cc_handshake 的取舍

#### 4.4.1 概念说明

Open Logic 有不止一种「把 AXI-S 数据搬过时钟域」的实体。除了本讲的异步 FIFO，还有 `olo_base_cc_handshake`——一个**不用 RAM、只用寄存器和握手**的轻量 CDC。两者都能传带反压的多 bit 数据，但定位完全不同：

- **`olo_base_cc_handshake`**：逐拍（word-by-word）握手。写侧来一个有效拍，通过一次握手送到读侧，读侧确认后再让写侧送下一拍。没有缓冲，体积小，但**吞吐受限**（每传一拍都要等一次跨域往返确认）。
- **`olo_base_fifo_async`**：带 RAM 缓冲，读写可全速并行推进，**吞吐可达 100%**（无强制空闲拍），但要消耗 RAM 资源。

#### 4.4.2 核心流程

选型可以浓缩成一张表（来自官方时钟跨越选型表）：

[clock_crossing_principles.md:56-69](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/clock_crossing_principles.md#L56-L69) —— 选型表。关键两列是「100% Perf.（是否无强制空闲拍）」与「No RAM（是否不需要 RAM）」：

| 实体 | 异步时钟 | 多 bit | 反压 | 100% 性能 | 无 RAM |
| :--- | :---: | :---: | :---: | :---: | :---: |
| `olo_base_cc_handshake` | ✅ | ✅ | ✅ | ❌（有空闲拍） | ✅ |
| `olo_base_fifo_async` | ✅ | ✅ | ✅ | ✅ | ❌（需 RAM） |

一句话决策：

- 数据是**稀疏的、低吞吐的**（偶尔传一拍配置、状态、单个采样），且想省 RAM → 用 `cc_handshake`。
- 数据是**连续的流**（视频、ADC 数据流、大批量突发），需要满吞吐、需要吸收速率差 → 用 `fifo_async`。

#### 4.4.3 源码精读

**`cc_handshake` 的定位**——官方描述就写明它「不为高性能，而为简单安全」：

[olo_base_cc_handshake.vhd:9-13](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_handshake.vhd#L9-L13) —— 注释：「The clock crossing is not meant to achieve high-performance but to be simple and safe.」

[olo_base_cc_handshake.vhd:30-50](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_handshake.vhd#L30-L50) —— 它的泛型只有 `Width_g / ReadyRstState_g / SyncStages_g`，端口是单字 AXI-S。内部结构（见 [`i_scc`/`i_bcc`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_handshake.vhd#L101-L136)）是用 `cc_simple` 传数据、用 `cc_pulse` 把读侧 ACK 回传写侧——一次完整的「数据过去 + ACK 回来」跨域往返才能完成一个字的传输，这正是它做不到 100% 吞吐的根源，也是它不需要 RAM 的原因。

对比本讲的 FIFO：它没有这种「等 ACK 才发下一拍」的往返，写侧只要没满就一直写、读侧只要没空就一直读，中间靠 RAM 解耦，因此能跑满。

#### 4.4.4 代码实践

**实践目标**：在同一个「写快读慢、连续流」需求下，体会为什么选 FIFO 而不是 handshake。

**操作步骤**（阅读 + 推演型实践）：

1. 设想需求：写侧 200 MHz 连续每拍发一个采样，读侧 100 MHz 连续每拍收一个采样，要求**不丢任何一个采样**。
2. 推演用 `cc_handshake`：每发一个字都要等 ACK 跨域往返（至少几个写时钟周期），写侧 200 MHz 根本来不及每拍都完成一次握手 → 必然丢采样或被迫降到很低速率。
3. 推演用 `fifo_async`：写侧全速写入 RAM，读侧全速读出，RAM 深度只要 ≥ 速率差造成的积压即可（稳态下 200→100 的持续速率差会无限积压，需要上游间歇性发送或加反压——此时 `In_Ready` 反压会正确生效，这正是 FIFO 的价值）。

**需要观察的现象**：在持续流场景，`cc_handshake` 的 `In_Ready` 会出现大量「未就绪」周期，吞吐远低于线速；`fifo_async` 的 `In_Ready` 在未满时持续为 1，吞吐接近线速。

**预期结果（待本地验证）**：若把同一个持续激励分别接到两个实体上仿真，统计单位时间内成功跨域的字数，`fifo_async` 显著高于 `cc_handshake`。这正是选型表的「100% Perf.」一列差异的可视化。

#### 4.4.5 小练习与答案

**练习 1**：把一个 AXI-S 的配置寄存器写事务（偶发、一次一个字）跨到另一个时钟域，该用哪个？
**答案**：`olo_base_cc_handshake`。偶发事务不要求吞吐，用它省下一整块 RAM，且实现更简单。

**练习 2**：为什么选型表里 `olo_base_fifo_async` 的「100% Perf.」是 ✅，而 `cc_handshake` 是 ❌？
**答案**：FIFO 用 RAM 解耦读写，写侧只要没满就连续写、读侧只要没空就连续读，无强制空闲拍；`cc_handshake` 每个字都要等读侧 ACK 经 `cc_pulse` 跨域返回后才能发下一个，往返延迟强制插入空闲拍，故达不到 100% 吞吐。

**练习 3**：异步 FIFO 也内含握手（Valid/Ready），为什么它不强制空闲拍？
**答案**：FIFO 的反压（`In_Ready`）只取决于「是否满」，而「满」由本域写指针与**只增不减的读指针副本**比较得到，不需要等任何跨域往返确认；缓冲由 RAM 承担，所以写侧可以连续写满一整段再被反压，而不是每拍一停。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个综合任务：

> **任务**：为一个数据采集前端设计跨时钟域通路。ADC 在 200 MHz 时钟域每拍产生一个 16 bit 采样（`adc_data/adc_valid`，无反压），下游处理在 100 MHz 时钟域（`proc_clk`，带 `proc_ready` 反压）。要求：采样不能丢、顺序不能乱、下游反压能正确回传。

**建议步骤**：

1. **选型**：对照 4.4 的决策，这是「连续流 + 需缓冲 + 需反压」场景 → 选 `olo_base_fifo_async`。
2. **接口接线**（示例代码）：
   ```vhdl
   i_cdc : entity work.olo_base_fifo_async
       generic map ( Width_g => 16, Depth_g => 64, AlmFullOn_g => true, AlmFullLevel_g => 56 )
       port map (
           In_Clk => adc_clk,  In_Rst  => adc_rst,  In_RstOut => adc_rst_synced,
           In_Data => adc_data, In_Valid => adc_valid, In_Ready => adc_ready,
           Out_Clk => proc_clk, Out_Rst => proc_rst, Out_RstOut => proc_rst_synced,
           Out_Data => proc_data, Out_Valid => proc_valid, Out_Ready => proc_ready,
           In_Full => adc_full, In_AlmFull => adc_almfull, others => open );
   ```
3. **解释设计要点**：
   - 用 `In_RstOut/Out_RstOut` 给两侧外围逻辑复位（4.3.2）。
   - `AlmFullLevel_g=56` 让上游在接近满时提前收到 `adc_almfull`（ADC 无反压时，这个信号可用来在系统层节流，或至少做监控）。
   - `Depth_g=64` 是 2 的幂，满足 4.2 的硬约束。
4. **约束**：按 4.3.3 给 `adc_clk ↔ proc_clk` 加 `set_max_delay -datapath_only`（或用 Vivado 的 scoped 自动约束）。
5. **验证**：用持续激励驱动 `adc_*`，在 `proc_ready` 偶尔拉低下模拟下游反压，仿真断言「读出序列 == 写入序列、无丢失」。

> 这个任务把「为什么用格雷码（4.2）→ 指针如何同步（4.1）→ 当 CDC 用要接 RstOut/加约束（4.3）→ 为什么选 FIFO 而非 handshake（4.4）」全部串了起来。可参考官方测试台 [`olo_base_fifo_async_tb.vhd`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_async/olo_base_fifo_async_tb.vhd) 的激励风格来写仿真。

## 6. 本讲小结

- 异步 FIFO 的核心矛盾是「读写指针分属不同时钟域」，解法是：**本域指针本域维护、转成格雷码后经同步器送给对侧、对侧转回二进制做副本**。
- **格雷码**保证指针递增时只有 1 位变化，同步器最多采到「旧值或新值」，把跨域安全性从「可能出错」降级为「至多延迟一拍」，配合保守的满/空判断实现零错误。
- **深度必须是 2 的幂**（源码 `assert` 强制），否则回绕点多位翻转会破坏格雷码性质；地址多留 **1 个最高位**用于区分「满」与「空」。
- 它本质是一个**通用时钟跨越实体**：内置双口 RAM 缓冲、格雷码指针同步、`olo_base_cc_reset` 复位穿越，因此提供双 `RstIn/RstOut`，外围逻辑应接 `RstOut`。
- 时序上有两个旋钮：`SyncStages_g`（2~4 级同步器，级数越多越稳但跨域延迟越大）和 `Optimization_g`（SPEED 牺牲延迟换 fmax / LATENCY 反之）。
- 与 `olo_base_cc_handshake` 的取舍：**连续流/需缓冲/要满吞吐 → 用异步 FIFO（耗 RAM）；偶发/低吞吐/省资源 → 用 cc_handshake（无 RAM）**。

## 7. 下一步学习建议

- **横向对照其它 CDC 实体**：继续阅读 [`doc/base/clock_crossing_principles.md`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/clock_crossing_principles.md) 的选型表，并按 **u4 单元（跨时钟域）** 系统学习 `cc_pulse/cc_simple/cc_status/cc_handshake`，以及相位对齐整数倍时钟的 `cc_n2xn/cc_xn2n`。
- **纵向深入 FIFO 家族**：本讲之后可学 **u3-l2（包 FIFO `olo_base_fifo_packet`）**，看包边界（Last/Be）如何在存储转发中界定，以及写侧丢包/读侧跳过的机制。
- **RAM 基础回顾**：若对 `IsAsync_g` 双时钟双口 RAM 的 RBW/WBR 行为还不熟，回看 **u2-l3**。
- **读经典**：异步 FIFO 的经典理论是 Cliff Cummings 的 *Simulation and Synthesis Techniques for Asynchronous FIFO Design*，本讲的格雷码指针、多 1 位判满、保守满空判断都源自它，对照源码读会更通透。
