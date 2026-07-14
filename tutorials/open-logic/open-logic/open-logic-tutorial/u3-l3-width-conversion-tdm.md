# 位宽转换与 TDM（wconv/tdm_mux）

## 1. 本讲目标

学完本讲后，读者应该能够：

- 理解 Open Logic 中 **TDM（时分复用）** 的约定，并知道为什么用 `Last` 来标记通道边界。
- 掌握整数倍位宽转换 `olo_base_wconv_n2xn`（窄到宽 / TDM 到并行）与 `olo_base_wconv_xn2n`（宽到窄 / 并行到 TDM）的接口与核心流程。
- 理解任意位宽转换 `olo_base_wconv_n2m` 基于移位寄存器与“最大公约数（GCF）分块”的实现思路。
- 学会用 `olo_base_tdm_mux` 从一路 TDM 数据流中选取某一个通道，并能用 `Last` 重建通道映射。
- 能够把 `xn2n` 与 `tdm_mux` 串接起来，仿真验证输出顺序与 `Last` 标记。

## 2. 前置知识

本讲在已掌握下列概念的基础上展开（见前置讲义）：

- **AXI4-Stream（AXI-S）握手**：`Valid`/`Ready` 成对出现，二者同时为高的时钟沿完成一次数据传输（一次 beat）；数据线按功能命名（如 `In_Data`）。
- **反压（Back-pressure）**：下游用 `Ready='0'` 暂停接收，上游必须暂存或停止发送而不丢数据。
- **两进程法 + record**：组合进程 `p_comb` 只算下一拍状态 `r_next`，时序进程 `p_seq` 只打拍与复位，状态收进 record，复位写成进程末尾的覆盖（见 u1-l5）。
- **base 包工具函数**：`log2ceil`、`choose`、`greatestCommonFactor`、`zerosVector`、`onesVector` 等编译期纯函数（见 u2-l1）。

补充两个本讲要用到的术语：

- **采样率（sample rate）**：单位时间内有效数据 beat 的数量（`Valid` 脉冲的频率）。位宽转换会**改变位宽，同时反比例改变采样率**——把窄字拼成宽字，输出 beat 变少；把宽字拆成窄字，输出 beat 变多。
- **小端对齐（little-endian）**：先到的数据放在位向量的低位。Open Logic 的位宽转换一律采用小端对齐。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| [doc/Conventions.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/Conventions.md) | 全库约定，其中 “TDM” 一节定义了时分复用与 `Last` 标记规则 |
| [src/base/vhdl/olo_base_wconv_n2xn.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_wconv_n2xn.vhd) | 窄到宽（\(W_o = n\cdot W_i\)）位宽转换，也可做 TDM→并行 |
| [src/base/vhdl/olo_base_wconv_xn2n.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_wconv_xn2n.vhd) | 宽到窄（\(W_i = n\cdot W_o\)）位宽转换，也可做 并行→TDM |
| [src/base/vhdl/olo_base_wconv_n2m.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_wconv_n2m.vhd) | 任意 \(W_i \leftrightarrow W_o\) 位宽转换（移位寄存器实现） |
| [src/base/vhdl/olo_base_tdm_mux.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_tdm_mux.vhd) | 从 N 路 TDM 数据中选取某一路通道 |

配套文档：`doc/base/olo_base_wconv_n2xn.md`、`olo_base_wconv_xn2n.md`、`olo_base_wconv_n2m.md`、`olo_base_tdm_mux.md`；测试台：`test/base/olo_base_wconv_*` 与 `test/base/olo_base_tdm_mux/olo_base_tdm_mux_tb.vhd`。

---

## 4. 核心概念与源码讲解

### 4.1 TDM（时分复用）约定

#### 4.1.1 概念说明

当多个信号经**同一个接口**传输，且这些信号**采样率相同**时，Open Logic 不引入额外的“通道号”信号，而是让通道**隐式地轮流出现**。例如 3 个通道，数据顺序就是 `0-1-2-0-1-2-…`。这种复用方式称为 **TDM（Time Division Multiplexing，时分复用）**：把时间划分成小段，每段分给一个通道。

TDM 的好处是省引脚、省线；坏处是**运行时很难直接看出某个 beat 属于哪个通道**。为此 Open Logic 用 `Last` 来标记通道边界。

#### 4.1.2 核心流程

TDM 约定的核心规则（见 Conventions.md 的 TDM 一节）：

1. 各通道采样率相同，按 `0,1,2,…,N-1,0,1,…` 隐式循环，**不设通道号信号**。
2. 用 `Last` 标记**每个采样周期（sample）的最后一个通道**，从而把通道映射固定下来。
3. 对于**分包（packetized）**数据，整包最后一个采样的最后一个通道才置 `Last`，这样在包边界处可重建完整通道映射。

下面是 TDM 与 `Last` 标记的示意图说明（对应 Conventions.md 中的 TDM 配图）：

```
通道顺序(3通道): ch0 ch1 ch2 | ch0 ch1 ch2 | ...
Last 标记:                  ^                ^      <- 每个sample的最后一个通道(ch2)
```

Conventions.md 同时点明了 TDM 与位宽转换实体的关系：宽到窄的 `xn2n` 可做**并行→TDM**，窄到宽的 `n2xn` 可做**TDM→并行**。这就是本讲把“位宽转换”和“TDM”放在一起讲的原因。

#### 4.1.3 源码精读

TDM 约定的权威定义在 Conventions.md 的 “TDM (Time Division Multiplexing)” 小节：

- 规则与隐式通道循环（[Conventions.md:196-200](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/Conventions.md#L196-L200)）——多路同采样率信号隐式轮流，不设通道指示。
- 并行与 TDM 互转的实体映射（[Conventions.md:204-207](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/Conventions.md#L204-L207)）——明确 `xn2n` 做 Parallel→TDM，`n2xn` 做 TDM→Parallel。
- 用 `Last` 标记最后一个通道以重建通道映射（[Conventions.md:209-218](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/Conventions.md#L209-L218)）。

#### 4.1.4 代码实践

**实践目标**：用静态分析理解“隐式通道循环 + Last 标记”如何唯一确定通道映射。

**操作步骤**：

1. 假设 4 路通道 TDM，第 1 个 beat 复位后默认为通道 0。
2. 在纸上写出前 12 个 beat 的通道编号序列。
3. 假设只有最后通道（ch3）在每个采样周期置 `Last`，标出 `Last` 出现的位置。

**需要观察的现象**：每出现一次 `Last`，下一个 beat 必然回到 ch0，可作为通道计数器重新对齐的依据。

**预期结果**：序列为 `0,1,2,3,0,1,2,3,0,1,2,3`，`Last` 出现在第 4、8、12 个 beat。这恰好是 `tdm_mux` 用 `In_Last` 重新同步计数器的依据（见 4.4）。

#### 4.1.5 小练习与答案

**练习 1**：如果各通道采样率**不相同**，还能用 TDM 隐式约定吗？为什么？

> **答案**：不能。隐式循环的前提是“采样率相同”，否则通道在时间轴上不对齐，必须显式携带通道号或使用包边界标记。

**练习 2**：为什么把 `Last` 放在“每个采样的最后一个通道”，而不是第一个？

> **答案**：放在最后一个通道意味着“这一组通道已经全部到齐”，接收端可以在 `Last` 时刻一次性处理完整采样（例如 `tdm_mux` 在采样末尾才输出选中的那一路），逻辑更简单、对齐更可靠。

---

### 4.2 整数倍位宽转换：n2xn 与 xn2n

#### 4.2.1 概念说明

整数倍位宽转换是最高效的一类：输入与输出位宽成**整数倍**关系。Open Logic 提供两个互补实体：

- **`olo_base_wconv_n2xn`**：\(W_o = n\cdot W_i\)，把 \(n\) 个窄字拼成 1 个宽字，**采样率降为 1/n**。也可做 **TDM→并行**（把 \(n\) 个时分的窄通道聚合成一个并行宽字）。
- **`olo_base_wconv_xn2n`**：\(W_i = n\cdot W_o\)，把 1 个宽字拆成 \(n\) 个窄字，**采样率升为 n 倍**。也可做 **并行→TDM**（把一个含 \(n\) 个通道的并行宽字拆成时分序列）。

二者都采用小端对齐：**最先到达的数据放在位向量最低位**。因此 4 个并行通道 `{ch0,ch1,ch2,ch3}` 在宽字中的排列是 `ch3..ch2..ch1..ch0`（ch0 在最低位），经 `xn2n` 拆分后输出顺序恰是 `ch0,ch1,ch2,ch3`——与 TDM 约定一致。

为什么整数倍要用专门的实体而不是通用的 `n2m`？因为整数倍转换可以用简单的计数器/移位完成，**资源更省、时序更优**（官方文档明确建议整数比优先用这两个）。

#### 4.2.2 核心流程

**n2xn（窄到宽，聚合）核心流程**：

```
用计数器 Cnt 收集输入字，Cnt = 0..n-1
每来一个有效字，写入宽字 Data 的第 Cnt 段，DataVld(Cnt) := '1'
当 (Cnt = n-1) 或 (In_Last='1') 时：一拍"凑齐"完成
   -> 把 Data 输出（Out_Valid='1'），并给出 Out_WordEna=DataVld
   -> Cnt 归 0
若输出端被反压(Out_Ready=0)且本拍又凑齐 -> IsStuck -> In_Ready=0（暂停收数）
```

`Out_WordEna` 是“字使能”（一个比特对应一个输入字宽度），作用类似字节使能：正常情况下全 1；当一包数据末尾不足 \(n\) 个字、靠 `In_Last` 提前冲刷时，只有真正有数据的字对应位被置 1。

**xn2n（宽到窄，拆分）核心流程**：

```
输入一个宽字后，用 DataVld(DataLast) 数组保存"还有哪些窄字待输出"
每拍(Out_Ready=1)从最低位输出一个 OutWidth 切片，并整体右移
   -> Out_Data = Data(OutWidth-1 downto 0)
   -> Out_Valid = DataVld(0), Out_Last = DataLast(0)
In_WordEna 决定哪些窄字有效：未使能的字直接跳过不输出
最后一个使能字对应 Out_Last='1'
```

#### 4.2.3 源码精读

**n2xn 实体接口**（[olo_base_wconv_n2xn.vhd:35-53](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_wconv_n2xn.vhd#L35-L53)）——泛型 `InWidth_g`/`OutWidth_g`，输出含 `Out_WordEna`（宽度为 `OutWidth_g/InWidth_g`，见 L51）。

**整数倍断言**（[olo_base_wconv_n2xn.vhd:81-86](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_wconv_n2xn.vhd#L81-L86)）——用 `floor=ceil` 断言 `OutWidth_g/InWidth_g` 必须为整数，并要求 `OutWidth_g >= InWidth_g`。

**聚合与凑齐逻辑**（[olo_base_wconv_n2xn.vhd:99-132](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_wconv_n2xn.vhd#L99-L132)）——核心要点：

- `ShiftDone_v`：`Cnt` 到顶或 `In_Last` 到达，表示本拍凑齐（L100）。
- `IsStuck_v`：凑齐且输出被反压时拉高（L101-105）。
- 凑齐且输出可交接时，把 `Data` 推到输出，`Out_WordEna := DataVld`（L113-120）。
- 写入新数据到第 `Cnt` 段（L121-132），并在 `Cnt=n-1` 或 `In_Last` 时让 `Cnt` 归零。

**反压感知的 In_Ready**（[olo_base_wconv_n2xn.vhd:135](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_wconv_n2xn.vhd#L135)）——`In_Ready <= not IsStuck_v`，即只有在“凑齐却被卡住”时才暂停输入。

**等宽直通**（[olo_base_wconv_n2xn.vhd:161-168](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_wconv_n2xn.vhd#L161-L168)）——`OutWidth_g=InWidth_g` 时不做任何转换，直接连线并把 `Out_WordEna` 置全 1。

**xn2n 实体接口**（[olo_base_wconv_xn2n.vhd:35-53](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_wconv_xn2n.vhd#L35-L53)）——注意输入侧的 `In_WordEna`（L47），宽度为 `InWidth_g/OutWidth_g`，默认全 1。

**xn2n 反压与拆分逻辑**（[olo_base_wconv_xn2n.vhd:88-132](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_wconv_xn2n.vhd#L88-L132)）——核心要点：

- `IsReady_v`：尚未输出的窄字多于 1 个，或最低窄字待输出但下游不收时，不再接收新宽字（L96-101）。
- 接收新宽字时缓存 `Data`、令 `DataVld := In_WordEna`，并循环把 `In_Last` 安置到最后一个使能字（L104-116）。
- 输出一拍后整体右移：`Data` 左侧补零、`DataVld/DataLast` 高位移到低位（L118-122）。
- 输出取最低切片：`Out_Data=Data(OutWidth-1..0)`，`Out_Valid=DataVld(0)`，`Out_Last=DataLast(0)`（L125-128）。

#### 4.2.4 代码实践

**实践目标**：阅读官方 `xn2n` 测试台，理解它如何构造一个含多通道的宽字并验证拆分顺序。

**操作步骤**：

1. 打开 [olo_base_wconv_xn2n_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_wconv_xn2n/olo_base_wconv_xn2n_tb.vhd)。
2. 阅读 `counterValue` 函数（L59-69）：它把 `start, start+1, …` 填进宽字的各 4-bit 段，第 0 段放 `start`（最低位），这正是小端对齐。
3. 阅读 `checkCounerValue`（L71-89）：断言输出按 `start, start+1, …` 的顺序逐字出现，且 `Last` 只在最后一个字拉高。
4. 该测试台用 `WidthRatio_g`（1..3）参数化，在 VUnit 中以不同 generic 组合跑多个用例（见 `run("Basic")`、`run("FullThrottle")` 等）。

**需要观察的现象**：宽字里最低 4 位（`start`）第一个被输出，随后是 `start+1`，依此类推——验证了小端拆分与 TDM 通道顺序一致。

**预期结果**：`Basic` 用例中，`counterValue(1)` 被拆成 `1, 2, …` 依次出现在 `Out_Data`，最后一拍 `Out_Last` 与入参一致。完整仿真需在本地用 VUnit+GHDL 运行（**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：`n2xn` 中 `Out_WordEna` 与 `xn2n` 中 `In_WordEna` 的“粒度”分别是什么？

> **答案**：`n2xn` 的 `Out_WordEna` 一个比特对应一个 `InWidth_g` 字（输出侧）；`xn2n` 的 `In_WordEna` 一个比特对应一个 `OutWidth_g` 字（输入侧）。二者都以“对方那个较窄的字”为粒度，类似字节使能。

**练习 2**：为什么 `n2xn` 在 `OutWidth_g = InWidth_g` 时要用独立的 `g_equalwidth` 分支直接连线，而不是走通用逻辑？

> **答案**：等宽时无需任何缓存与打拍，直接连线既省资源又零延迟；通用分支里的计数器、`Data` 寄存器对等宽场景是纯浪费。

**练习 3**：在 `xn2n` 中，若输入宽字的 `In_WordEna = "0101"`（仅第 0、第 2 个窄字有效）且 `In_Last='1'`，输出会是什么？

> **答案**：只有第 0、第 2 个窄字会被输出，未使能的第 1 个被跳过；`Out_Last` 跟在第 2 个（最后一个使能字）之后。

---

### 4.3 任意位宽转换：n2m

#### 4.3.1 概念说明

当输入与输出位宽**不成整数倍**时（如 8↔7、24↔32），需要 `olo_base_wconv_n2m`。它的实现思路更通用：用一个**移位寄存器（shift register）**，把输入数据按“块（chunk）”塞进去，再从低位按输出位宽取出来。

关键数学工具是**最大公约数 GCF**（代码里叫 `greatestCommonFactor`）：

- 把输入/输出位宽都切成等长的小块，块大小取 \(\gcd(W_i, W_o)\)（若启用字节使能则块固定为 8）。
- 这样输入包含 \(W_i/\gcd\) 个块，输出包含 \(W_o/\gcd\) 个块，二者都是整数。
- 移位寄存器每塞入若干输入块、取出若干输出块，经过 \(\mathrm{lcm}(W_i,W_o)\) 比特后回到初始相位，实现任意比例的稳定转换。

\[ W_i = k_i \cdot g,\quad W_o = k_o \cdot g,\quad g = \gcd(W_i, W_o) \]

启用字节使能（`UseBe_g=true`）时，遵循 Open Logic 的 **Trailing-Only Byte-Enable** 约定：只有一包的最后一个 beat 允许出现非全 1 的字节使能，且有效字节必须从最低位起连续。

#### 4.3.2 核心流程

```
常量：ChunkSize = UseBe ? 8 : gcd(Wi,Wo)
      InChunks  = Wi / ChunkSize,  OutChunks = Wo / ChunkSize
      SrWidth   = 容纳一次输出所需的最大移位宽度

每拍：
  1) 输出侧：当 ChunkCnt >= OutChunks 或本包最后一块到位时，Out_Valid='1'
     -> Out_Data = ShiftReg 的低 Wo 位；Out_Ready 收下后整体右移 Wo 位
  2) 输入侧：当 ChunkCnt < OutChunks 且非末拍阻塞时，In_Ready='1'
     -> 把 In_Data 塞进 ShiftReg 的 ChunkCnt*ChunkSize 偏移处，ChunkCnt += InChunks
  3) LastChunk 数组（每块 1 比特）记录"包的最后一块"落在哪个块，用于 Out_Last 与 Out_Be
```

#### 4.3.3 源码精读

**实体接口与泛型**（[olo_base_wconv_n2m.vhd:35-55](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_wconv_n2m.vhd#L35-L55)）——`InWidth_g`/`OutWidth_g` 有默认值（16/24），`UseBe_g`（L39）控制是否启用字节使能；输入侧 `In_Be`（L47）默认全 1。

**GCF 分块常量**（[olo_base_wconv_n2m.vhd:64-68](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_wconv_n2m.vhd#L64-L68)）——`MaxChunkSize_c = greatestCommonFactor(InWidth_g, OutWidth_g)`，`ChunkSize_c` 由 `UseBe_g` 在 8 与 GCF 之间选择；`InChunks_c/OutChunks_c` 为各自的块数。

> 参考 `greatestCommonFactor` 实现（[olo_base_pkg_math.vhd:234-248](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_math.vhd#L234-L248)）：从 `min(a,b)` 递减试除，返回第一个能同时整除 a、b 的数。

**字节使能对齐断言**（[olo_base_wconv_n2m.vhd:83-85](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_wconv_n2m.vhd#L83-L85)）——`UseBe_g=true` 时要求 `InWidth_g`、`OutWidth_g` 均为 8 的倍数，否则仿真直接报错（`severity failure`）。

**输出事务与移位**（[olo_base_wconv_n2m.vhd:99-136](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_wconv_n2m.vhd#L99-L136)）——要点：

- `IsLastBeat_v = or_reduce(LastChunk(OutChunks-1..0))`，用 IEEE `std_logic_misc` 的 `or_reduce` 判断输出窗口内是否含包尾（L101）。
- 满足 `ChunkCnt >= OutChunks` 或 `IsLastBeat` 时置 `Out_Valid`（L102-104）。
- `Out_Data` 取 `ShiftReg` 低 `OutWidth` 位（L120）；右移时高位补零（L115）。
- `UseBe_g=true` 时，`Out_Be` 从最低字节起连续置 1，直到 `LastChunk` 标记处（L123-136）——即 Trailing-Only 约定。

**输入塞入与阻塞**（[olo_base_wconv_n2m.vhd:138-174](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_wconv_n2m.vhd#L138-L174)）——要点：

- `In_Ready` 仅在 `ChunkCnt < OutChunks` 且非末拍阻塞（`LastPending=0`）时拉高（L141-143）。
- 把 `In_Data` 写入 `ShiftReg` 的 `ChunkCnt*ChunkSize` 偏移处（L146-147），`ChunkCnt += InChunks`（L171）。
- 末拍字节使能校验（L152-154）：非末拍（`In_Last='0'`）时 `In_Be` 必须全 1，否则报错——Trailing-Only 的强制实现。

**等宽直通**（[olo_base_wconv_n2m.vhd:196-202](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_wconv_n2m.vhd#L196-L202)）——等宽时直接连线，与 `n2xn` 同理。

#### 4.3.4 代码实践

**实践目标**：用一个 8→7 的非字节对齐例子，手工走一遍移位寄存器的“塞入/取出”过程。

**操作步骤**：

1. 设 `InWidth_g=8`、`OutWidth_g=7`、`UseBe_g=false`。
2. 计算：\(\gcd(8,7)=1\)，故 `ChunkSize=1`，`InChunks=8`，`OutChunks=7`。
3. 假设输入连续两拍为 `0x__AB`、`0x__CD`（每个 8 位）。
4. 手工模拟：第 1 拍塞入 8 位 → 可输出低 7 位；第 2 拍再塞入 8 位，结合上一拍剩余的 1 位，又能输出 7 位……
5. 打开官方测试台 [olo_base_wconv_n2m_78_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_wconv_n2m/olo_base_wconv_n2m_78_tb.vhd) 对照你手工的顺序。

**需要观察的现象**：因为 7 不整除 8，输出相位会逐拍偏移，若干拍后回归——这正是需要移位寄存器而非简单计数器的原因。

**预期结果**：数据按小端、无丢失地连续流出，但 `Out_Valid` 的节拍模式不是简单的 1:1 或 1:n。**待本地验证**（用 VUnit 跑 `olo_base_wconv_n2m_78_tb`）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ChunkSize` 默认取 GCF 而非固定为 1？

> **答案**：取 GCF 让块数最少（`InChunks`、`OutChunks` 最小），移位寄存器最窄、`LastChunk` 数组最短，资源与时序都更优。GCF 是“既能整除 \(W_i\) 又能整除 \(W_o\)”的最大单位。

**练习 2**：若要用 `n2m` 做 16→24 且需要精确告知接收端“最后一拍哪些字节有效”，应如何配置？

> **答案**：令 `UseBe_g=true`（要求 16、24 都是 8 的倍数，成立），在包最后一拍用 `In_Be` 给出从最低位起连续的有效字节；实体会在 `Out_Be` 上输出对应的有效字节指示。

---

### 4.4 TDM 通道选取：olo_base_tdm_mux

#### 4.4.1 概念说明

`olo_base_tdm_mux` 解决一个很具体的问题：一路 TDM 数据流里轮流跑着 \(N\) 个通道，**我只想取出其中某一个通道**。它在内部维护一个 0..\(N-1\) 的通道计数器，把“被选中通道”那一拍的数据锁存下来，在一个采样周期结束时输出一次。

要点：

- 通道数 `Channels_g` 是**编译期固定**的，运行时不能变。
- 选择信号 `In_ChSel` 在**每个采样周期的第 0 通道**被采样；其它时刻的 `In_ChSel` 被忽略。
- 输出 `Out_Valid` 在**采样周期末尾**（最后一个通道到达时）脉冲一次——所以输出采样率是输入的 \(1/N\)。
- 计数器可由 `In_Last` 重新对齐；若不提供 `In_Last`，则复位后第一个 beat 视为通道 0，之后自由运行。

#### 4.4.2 核心流程

`tdm_mux` 是一个三级流水线（见源码注释 Stage 0/1/2）：

```
Stage 0/1（打一拍）:
  if In_Valid:                      -- 仅在有效数据上推进计数
     if Count_0 == 0: 锁存 SelLatched_1 = In_ChSel   -- 仅在第0通道采样选择
     Count_0 = (Count_0==N-1 或 In_Last) ? 0 : Count_0+1
  Data_1 <= In_Data; Vld_1 <= In_Valid; Count_1 <= Count_0; Last_1 <= In_Last

Stage 2（再打一拍）:
  if Count_1 == SelLatched_1:       -- 命中所选通道时锁存数据
     Data_2 <= Data_1
  Vld_2 <= '1' 当且仅当 Vld_1 且 Count_1==N-1   -- 采样末尾才输出一次
  Last_2 <= Last_1

输出: Out_Valid=Vld_2, Out_Data=Data_2, Out_Last=Last_2
```

注意 `In_ChSel` 的位宽是 `log2ceil(Channels_g)`（[olo_base_tdm_mux.vhd:39](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_tdm_mux.vhd#L39)）。因此 `Channels_g` 通常取 2 的幂，使选择位宽与通道数严格对应。

#### 4.4.3 源码精读

**实体接口**（[olo_base_tdm_mux.vhd:31-47](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_tdm_mux.vhd#L31-L47)）——`Channels_g`/`Width_g`；`In_ChSel` 宽度 `log2ceil(Channels_g)`；注意**没有 `Ready`**，它不实现反压（只做纯组合/打拍选取）。

**三级流水线信号**（[olo_base_tdm_mux.vhd:54-65](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_tdm_mux.vhd#L54-L65)）——`Count_0`（Stage 0 计数器）、`SelLatched_1/Data_1/Count_1/Vld_1/Last_1`（Stage 1）、`Data_2/Vld_2/Last_2`（Stage 2）。

**Stage 0/1：计数与选择锁存**（[olo_base_tdm_mux.vhd:73-89](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_tdm_mux.vhd#L73-L89)）——`Count_0=0` 时锁存 `In_ChSel`（L76-78）；`Count_0` 在到顶或 `In_Last` 时归零（L80-84），实现与 TDM 采样的同步。

**Stage 2：命中锁存与末尾输出**（[olo_base_tdm_mux.vhd:91-101](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_tdm_mux.vhd#L91-L101)）——`Count_1 == SelLatched_1` 时把 `Data_1` 锁进 `Data_2`（L93-95）；只有 `Vld_1` 且 `Count_1=Channels_g-1` 时才置 `Vld_2`（L97-100），即每采样周期输出一次。

**复位（进程末尾覆盖）**（[olo_base_tdm_mux.vhd:104-108](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_tdm_mux.vhd#L104-L108)）——只复位 `Count_0` 与两级 valid，符合“只复位状态寄存器”的约定。

#### 4.4.4 代码实践

**实践目标**：阅读官方 `tdm_mux` 测试台，理解 `EachChannel` 用例如何验证“每个通道都能被正确选中”。

**操作步骤**：

1. 打开 [olo_base_tdm_mux_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_tdm_mux/olo_base_tdm_mux_tb.vhd)，`Channels_c=5`、`Width_c=16`（L38-39）。
2. 看 `EachChannel`（L93-108）：外层循环选 `ch`，内层循环把 `ch*256+s`（\(s=0..4\)）依次喂入，并把 `In_ChSel` 经 `tuser` 设为 `ch`。
3. 断言（L103）：每个采样周期结束后，输出值应为 `ch*256+ch`——即第 `ch` 个通道的值。
4. 再看 `ResyncOnTlast`（L132-161）：验证 `In_Last` 能让偏离的计数器重新对齐，且 `Out_Last` 在包边界正确传递。

**需要观察的现象**：无论 `In_ChSel` 在一个采样周期内如何变化，只有第 0 通道时刻的值起作用（见 `SampleOnFirst` 用例 L111-130）。

**预期结果**：5 个通道逐一被选中，输出值与 `ch*256+ch` 完全一致；`Out_Last` 仅在末通道携带 `In_Last` 时出现。**待本地验证**（VUnit+GHDL）。

#### 4.4.5 小练习与答案

**练习 1**：`tdm_mux` 为什么没有 `Ready` 信号？

> **答案**：它只做“打拍 + 选取”，不缓存多拍数据，输出采样率是输入的 \(1/N\)，天然不会拥塞，因此无需反压。上游若有反压需求，应在更外层处理。

**练习 2**：`In_ChSel` 在第 0 通道以外的取值会影响输出吗？

> **答案**：不会。代码只在 `Count_0=0` 时锁存 `In_ChSel`（L76-78），其余时刻被忽略——这正是 `SampleOnFirst` 用例验证的行为。

---

## 5. 综合实践：并行→TDM→选取通道

**任务**：用 `olo_base_wconv_xn2n` 把 4 个并行通道（每通道 8 位）串行化为 TDM，再用 `olo_base_tdm_mux` 选出**索引为 2 的通道**，仿真验证输出顺序与 `Last` 标记一致。

### 5.1 数据通路设计

- 4 个并行通道 `ch0..ch3`，每通道 8 位，小端拼成 32 位宽字：`Wdata = ch3 & ch2 & ch1 & ch0`（`ch0` 在 bit 7..0）。
- **xn2n**：`InWidth_g=32`、`OutWidth_g=8`。每个宽字拆成 4 个 8 位 TDM 字，输出顺序 `ch0,ch1,ch2,ch3`，采样率 ×4。
- 入口给 `In_Last='1'` 标记一个采样（一包）的末尾 → xn2n 在最后一个输出字（`ch3`，即第 4 个 TDM 字）上置 `Out_Last='1'`，恰好是 TDM 约定里“每个采样的最后一个通道”。
- **tdm_mux**：`Channels_g=4`、`Width_g=8`、`In_ChSel=2`。它在 `ch0` 时刻锁存选择 `2`，在 `ch2` 时刻锁存数据，在 `ch3`（末通道）时刻输出一次 → `Out_Data = ch2 的值`。由于末通道带 `Last`，`Out_Last='1'`。

### 5.2 关键实例化（示例代码）

下面是把两个实体串接的最小连线，**仅供说明，非仓库已有代码**（标注为“示例代码”），基于官方 TB 的命名风格：

```vhdl
-- 示例代码：并行 -> TDM -> 选取通道 2
constant ChWidth_c  : natural := 8;
constant Channels_c : natural := 4;

signal Par_Data  : std_logic_vector(Channels_c*ChWidth_c-1 downto 0);
signal Par_Last  : std_logic;
signal Tdm_Data  : std_logic_vector(ChWidth_c-1 downto 0);
signal Tdm_Last  : std_logic;
signal Mux_Data  : std_logic_vector(ChWidth_c-1 downto 0);

-- 宽 32 -> 窄 8，并行转 TDM
i_ser : entity olo.olo_base_wconv_xn2n
    generic map ( InWidth_g => 32, OutWidth_g => 8 )
    port map (
        Clk => Clk, Rst => Rst,
        In_Valid => Par_Valid, In_Ready => Par_Ready,
        In_Data  => Par_Data,  In_Last  => Par_Last,
        In_WordEna => (others => '1'),       -- 4 个窄字全有效
        Out_Valid => Tdm_Valid, Out_Ready => Tdm_Ready,
        Out_Data  => Tdm_Data,  Out_Last  => Tdm_Last );

-- 从 4 路 TDM 中选第 2 通道（索引从 0 计）
i_mux : entity olo.olo_base_tdm_mux
    generic map ( Channels_g => Channels_c, Width_g => ChWidth_c )
    port map (
        Clk => Clk, Rst => Rst,
        In_ChSel => std_logic_vector(to_unsigned(2, log2ceil(Channels_c))),
        In_Valid => Tdm_Valid, In_Data => Tdm_Data, In_Last => Tdm_Last,
        Out_Valid => Mux_Valid, Out_Data => Mux_Data, Out_Last => Mux_Last );
```

> 说明：`Tdm_Ready` 在 `tdm_mux` 一侧不存在（它无反压），故 `i_ser.Out_Ready` 通常直接接 `'1'`；上游 `Par_*` 的反压由 `xn2n` 的 `In_Ready` 提供。

### 5.3 操作步骤

1. 参照 [olo_base_wconv_xn2n_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_wconv_xn2n/olo_base_wconv_xn2n_tb.vhd) 与 [olo_base_tdm_mux_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_tdm_mux/olo_base_tdm_mux_tb.vhd) 的 VUnit 框架，新建一个把上述两个 DUT 串接的 testbench（时钟、复位、`axi_stream_master`/`slave` VC 沿用官方写法）。
2. 令 `Par_Data = x"AA_BB_CC_DD"`（`ch0=0xDD`、`ch1=0xCC`、`ch2=0xBB`、`ch3=0xAA`），`Par_Last='1'`。
3. 用 `push_axi_stream` 喂入，用 `check_axi_stream` 期望 `Mux_Data = 0xBB`（通道 2）、`Mux_Last='1'`。
4. 在 `sim/` 目录用 `run.py` 跑 GHDL（见 u1-l4）：例如 `python run.py --ghdl <你的tb>.vhd`。

### 5.4 需要观察的现象

- `i_ser` 输出端连续 4 拍出现 `0xDD, 0xCC, 0xBB, 0xAA`，最后一拍 `Tdm_Last='1'`（通道顺序 0,1,2,3 与小端拆分一致）。
- `i_mux` 每 4 拍 TDM 输入只产生 1 个 `Mux_Valid` 脉冲，`Mux_Data` 恒为 `0xBB`（通道 2 的值），`Mux_Last` 与末通道的 `Tdm_Last` 一致。

### 5.5 预期结果

每送入一个 `Par_Data`，最终输出恰好一个 `Mux_Valid`，其数据等于宽字中通道 2 那一段（`0xBB`），且 `Mux_Last='1'`。这同时验证了三件事：xn2n 的小端拆分顺序、TDM 通道与 `Last` 的对齐、tdm_mux 对通道 2 的正确选取。**完整运行结果待本地验证**。

---

## 6. 本讲小结

- **TDM 约定**：同采样率多路信号在同一接口上隐式轮流（`0,1,…,N-1` 循环），用 `Last` 标记每个采样的最后一个通道以重建通道映射。
- **整数倍转换更高效**：`xn2n`（宽→窄 / 并行→TDM，采样率 ×n）与 `n2xn`（窄→宽 / TDM→并行，采样率 ÷n）应优先于通用方案使用。
- **小端对齐**贯穿所有转换：最先到达的数据放最低位，这决定了 TDM 通道的输出顺序。
- **字使能**：`n2xn` 的 `Out_WordEna` 与 `xn2n` 的 `In_WordEna` 都以“较窄字”为粒度，用于处理包尾不足一个完整宽字的情况。
- **任意位宽 `n2m`** 用移位寄存器 + GCF 分块 + `LastChunk` 数组实现，支持字节使能（Trailing-Only 约定）。
- **`tdm_mux`** 用三级流水线在一个采样周期里锁存所选通道、在末通道输出一次，`In_ChSel` 仅在第 0 通道采样，无反压。

## 7. 下一步学习建议

- 想了解**跨时钟域**的位宽转换（相位对齐的整数倍），可读 `olo_base_cc_n2xn` / `olo_base_cc_xn2n`（见 u4-l3），它们与 `xn2n`/`n2xn` 的关系正如文档所述：先做位宽转换再跨时钟域。
- 想理解带**缓冲与包边界**的更复杂场景，可结合 u3-l2 的包 FIFO，构造“包 FIFO + xn2n + tdm_mux”的数据通路。
- 继续精读 base 区域：本讲涉及的 `choose`/`greatestCommonFactor`/`log2ceil`/`zerosVector` 等工具函数定义在 [olo_base_pkg_math.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_math.vhd) 与 [olo_base_pkg_logic.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_logic.vhd)，建议对照 u2-l1 通读一遍。
