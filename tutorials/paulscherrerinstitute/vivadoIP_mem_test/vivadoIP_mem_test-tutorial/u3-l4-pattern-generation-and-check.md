# Pattern 生成、数据校验与首个错误地址

> 本讲是核心实体 `mem_test` 的第三篇，承接 [u3-l3 主状态机](u3-l3-main-fsm.md)。u3-l3 只讲了控制流（七个状态怎么走），本讲回答两个被刻意留下的问题：**每一拍写到存储器/从存储器读回的数据到底是什么？读到错的数据后，硬件如何数错、如何记住第一个错在哪里？**

---

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `InitPattern` 与 `UpdatePattern` 这两个「触发点」分别在状态机的什么时候发生、为什么这样设计。
- 写出四种 pattern（Counter / Walking-1 / OwnAddress / PseudoRandom）的初始化值与逐拍更新公式，并手算前几拍。
- 解释读回比对逻辑：`RdDat_Data /= r.Pattern` 触发后，`Errors` 怎么累加、`FirstErrAddr` 怎么由「beat 计数」换算回「字节地址」。
- 拿到一个真实测试场景（例如 testbench 里 OwnAddr 注入 15 个错误、首错 0xEC），反推出硬件每一拍在比对什么。

---

## 2. 前置知识

本讲默认你已读过 u2-l2（四种 pattern 的语义选型）和 u3-l2/u3-l3（two-process 设计法与主状态机）。下面补三个本讲会反复用到、但前面没展开的概念。

### 2.1 beat（数据拍）与字节地址

AXI4 数据以 **beat** 为单位传输，一个 beat = 一个 `AxiDataWidth_g` 位宽的数据字。对最常见的 32 位配置：

\[
B = \text{AxiDataWidth\_g} / 8 = 4 \text{ 字节/beat}, \quad s = \log_2 B = \log_2 4 = 2
\]

所以一个 beat 覆盖 4 个字节地址。把字节地址右移 \(s\) 位就得到 **beat 地址**；把 beat 地址左移 \(s\) 位（低位补 0）就还原成字节地址。本讲讲首个错误地址时，核心就是这步来回换算。

### 2.2 LFSR（线性反馈移位寄存器）

伪随机 pattern 用的是一个 16 位 **LFSR**：把寄存器里的若干位（叫 tap）异或起来，作为新的最低位，同时整个寄存器左移一位。选定一组「本原多项式」对应的 tap，序列就会在 \(2^{16}-1 = 65535\) 拍里几乎不重复地走完一圈，看起来很像随机，但完全确定——这正是内存测试想要的：写阶段和读阶段用同一个序列，硬件正常时比对必然相等。

### 2.3 two-process 里「只写变化量」的惯例

[u3-l2](u3-l2-core-entity-and-two-process.md) 讲过：组合进程 `p_comb` 开头有 `v := r`，之后只给**会变化**的字段赋值。本讲看到 `v.Pattern := ...` 时要意识到——只有在「这一拍 Pattern 确实要变」的分支里才会出现这行；没写的拍，Pattern 原样保持。两个布尔变量 `InitPattern_v` / `UpdatePattern_v` 就是用来在 case 分支里**标记**「本拍要不要改 Pattern」，真正的改写集中在 case 之后的共享代码段里完成。

---

## 3. 本讲源码地图

本讲几乎全部围绕一个文件：

| 文件 | 作用 |
| --- | --- |
| [hdl/mem_test.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd) | 核心实体。本讲聚焦其中三段：`InitPattern` 播种块、`UpdatePattern` 推进块、`Read_s` 里的比对/错误统计块。 |
| [hdl/mem_test_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_pkg.vhd) | 提供 `C_PATTERN_SEL_*` 四个 pattern 编号常量与 `RNG_PATTERN_SEL` 字段范围。 |
| [tb/top_tb.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd) | 仿真平台。本讲用它「OwnAddr 注入 15 个错误、首错 0xEC」这条用例给公式做对照证据。 |

---

## 4. 核心概念与源码讲解

### 4.1 InitPattern：进入命令态时「播种」

#### 4.1.1 概念说明

pattern 是一段**确定性数据序列**：第 0 拍写什么、第 1 拍写什么……完全由算法决定。要把序列跑起来，首先得有一个**起点**（种子）。`InitPattern` 就是「把 Pattern 寄存器装填成序列起点」的动作。

它只在两个地方被触发：进入 `WrCmd_s`（准备写）和进入 `RdCmd_s`（准备读）。为什么读阶段也要重新播种？因为四种 pattern 都依赖一个确定的起点，而写阶段可能已经把序列推进了很远；读阶段必须从**同一个起点**重新出发，否则写进去的序列和读回时期待的序列对不上，好端端的存储器也会被误报成全错。

注意一个关键设计：播种发生在**命令态**，而不是数据态的第一拍。这样等真正进入 `Write_s` / `Read_s` 开始送/收数据时，`r.Pattern` 已经稳定地持有第 0 拍的正确值，第一拍数据直接取 `r.Pattern` 即可。

#### 4.1.2 核心流程

```
进入 WrCmd_s 或 RdCmd_s:
    PatternCnt := 0                 # beat 计数清零
    InitPattern_v := true           # 标记：本拍要播种
    └─ 共享代码段读到 true，按 PATTERN_SEL 选择种子写入 v.Pattern
命令被接受 → 进入 Write_s / Read_s:
    WrDat_Data / 期待读值 = r.Pattern（即第 0 拍种子）
```

四种 pattern 的种子如下（\(W = \text{AxiDataWidth\_g}\)，\(B = W/8\)，基地址 `A`）：

| Pattern | 种子（第 0 拍） |
| --- | --- |
| Counter | 全 0 |
| Walking-1 | 仅 bit0 = 1，其余 0 |
| OwnAddress | 基地址 `A` 的低 \(W\) 位 |
| PseudoRandom | 低 16 位 = `0x6D3F`，高位 0 |

#### 4.1.3 源码精读

播种的「触发点」在命令态里，先把 `InitPattern_v` 置真：

[mem_test.vhd:224-234 — WrCmd_s 里清零 PatternCnt 并请求播种](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L224-L234)

读命令态 `RdCmd_s` 做的事完全对称（[L256-266](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L256-L266)），同样 `PatternCnt := 0` + `InitPattern_v := true`。

真正的播种逻辑集中在 case 之后的共享段，按 `RegPatternSel_v` 四选一：

[mem_test.vhd:312-327 — InitPattern 四选一种子](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L312-L327)

四个分支里值得点出三处细节：

- **OwnAddress** 用 `resize(RegAddr_v, v.Pattern'length)`——`RegAddr_v` 是 64 位基地址，截到数据宽度 \(W\)。所以第 0 拍数据就是「这块存储区的起始字节地址」。
- **PseudoRandom** 只把低 16 位设成 `0x6D3F`，高位保持 0。LFSR 的状态只活在低 16 位（见 4.2）。
- **`when others => v.Fsm := IntError_s`**：如果软件写了一个不存在的 pattern 编号（4..7），直接跳内部错误陷阱。这正是 u3-l3 讲的 `IntError_s` 的来源之一。

pattern 编号常量定义在 package 里，软硬共享：

[mem_test_pkg.vhd:49-54 — C_PATTERN_SEL_* 四个编号](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_pkg.vhd#L49-L54)

#### 4.1.4 代码实践

**目标**：确认「播种发生在命令态、第一拍数据等于种子」这件事。

**步骤**（源码阅读型）：

1. 打开 [mem_test.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd)，定位 `WrCmd_s`（L224）与共享 `InitPattern` 块（L312）。
2. 回答：在 `Write_s` 的第一拍，`WrDat_Data` 的值是从哪个信号来的？它又是在哪一拍被赋成种子的？
3. 对照 testbench 的 Counter 用例（[top_tb.vhd:480-491](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L480-L491)）：`axi_expect_wd_burst(16, Cnt_v, 1, ...)` 里 `Cnt_v` 初值是 0、步进 1。这与 RTL「Counter 种子 = 0、每拍 +1」是否吻合？

**预期结果**：`WrDat_Data <= std_logic_vector(r.Pattern)`（[L367](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L367)），种子在 `WrCmd_s` 期间（命令被接受的前一拍）由 `InitPattern` 写入；testbench 期望的第 0 拍数据 0 与 RTL 种子 0 一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么读阶段（`RdCmd_s`）必须重新播种，而不能直接接着写阶段末尾的 Pattern 继续推？

**答案**：写阶段把序列从第 0 拍推到了最后一拍；读阶段期待的是「从第 0 拍开始的同一个序列」。若不重新播种，读回时期待的第 0 拍会变成写阶段的最后一拍，整段比对全部错位，好存储器也会被报成「几乎全错」。

**练习 2**：软件把 `REG_PATTERN_SEL` 写成 `4`（不在 0..3 内），硬件会怎样？

**答案**：`InitPattern` 的 `case` 落到 `when others => v.Fsm := IntError_s`，状态机进入不可恢复的内部错误陷阱，`STATUS` 寄存器随后报 `C_STATUS_INTERR`(6)，只有复位能退出。

---

### 4.2 UpdatePattern：每拍握手后「推进序列」

#### 4.2.1 概念说明

种子只解决第 0 拍。从第 1 拍起，每一拍数据都要由「上一拍的 Pattern」按算法推一步得到——这就是 `UpdatePattern`。它和数据握手严格绑定：**只有当一拍数据真正被送走/接收（valid && ready 同时拉高），才推进一次**。这保证了「PatternCnt 第 N 拍」与「序列第 N 项」永远一一对应，无论中间有没有插入等待周期。

#### 4.2.2 核心流程

```
在 Write_s / Read_s 里，检测到一次数据握手（r.*Vld='1' and *_Rdy='1'）:
    if 这是最后一拍（PatternCnt = Size-1）:
        跳转状态，不再更新
    else:
        PatternCnt := PatternCnt + 1
        UpdatePattern_v := true      # 标记：本拍推进 Pattern
        └─ 共享段按 PATTERN_SEL 四选一，由 r.Pattern 算出 v.Pattern
```

四种 pattern 的更新公式（记当前拍为 \(p_n\)，下一拍为 \(p_{n+1}\)，数据宽度 \(W\)，\(B=W/8\)）：

| Pattern | 更新公式 | 直觉 |
| --- | --- | --- |
| Counter | \(p_{n+1} = p_n + 1\) | 递增计数，覆盖所有数据位组合 |
| Walking-1 | 循环左移 1 位（最低位补原最高位） | 单个 1 在所有位上轮流「走」一遍 |
| OwnAddress | \(p_{n+1} = p_n + B\) | 数据 == 自己所在的字节地址 |
| PseudoRandom | 16 位 LFSR 左移，新 bit0 = bit15⊕bit13⊕bit12⊕bit10 | 伪随机序列 |

#### 4.2.3 源码精读

写阶段的推进点（读阶段 `Read_s` 对称）：

[mem_test.vhd:237-253 — Write_s 里握手后推进 PatternCnt 与 Pattern](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L237-L253)

注意两个判据：

- 最后一拍用 `r.PatternCnt = r.CmdWr_Size-1`，而 `CmdWr_Size` 是把字节大小右移 \(s\) 后的 **beat 数**（[L227](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L227)：`shift_right(RegSize_v(...), log2(AxiDataWidth_g/8))`）。所以 `PatternCnt` 是 beat 索引，范围 \(0 \sim \text{Size}/B - 1\)。
- 只有「不是最后一拍」才 `PatternCnt+1` 并触发更新；最后一拍直接跳状态，不再浪费一次更新。

真正的四选一推进逻辑：

[mem_test.vhd:330-345 — UpdatePattern 四选一公式](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L330-L345)

逐条解读：

- **Counter**：`unsigned(r.Pattern) + 1`，标准递增。
- **Walking-1**：
  ```vhdl
  v.Pattern(0) := r.Pattern(r.Pattern'high);                       -- 新 bit0 = 旧最高位
  v.Pattern(high downto 1) := r.Pattern(high-1 downto 0);          -- 其余左移
  ```
  这是**循环左移**：那个唯一的 1 从 bit0 走到 bit1、…、bit31、再回到 bit0。testbench 里 `axi_expect_wd_walk1` 用同样的式子（[top_tb.vhd:115](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L115)）生成期望序列。
- **OwnAddress**：`unsigned(r.Pattern) + AxiDataWidth_g/8`，即每拍加 \(B\)。结合种子 = 基地址，第 \(n\) 拍数据正好等于 \(\text{基地址} + n\cdot B\)，也就是该 beat 的字节地址。地址译码错了，数据就错位，立刻暴露。
- **PseudoRandom**：
  ```vhdl
  v.Pattern(0) := r.Pattern(15) xor r.Pattern(13) xor r.Pattern(12) xor r.Pattern(10);
  v.Pattern(high downto 1) := r.Pattern(high-1 downto 0);
  ```
  16 位 Fibonacci LFSR，tap 在 15/13/12/10（对应本原多项式 \(x^{16}+x^{14}+x^{13}+x^{11}+1\)），左移、反馈进 bit0。数据宽度 >16 时高位也跟着左移，但反馈只看低 16 位，所以「随机源」始终是这 16 位。

#### 4.2.4 代码实践：手推 PRBN 前 4 拍

**目标**：用纸笔复算 PseudoRandom pattern 前 4 拍，验证你对 LFSR 推移的理解。

**步骤**（纯手算，无需运行）：

种子 \(p_0 = \texttt{0x6D3F}\)（仅低 16 位，按 16 位算）。反馈函数：

\[
\text{fb} = b_{15} \oplus b_{13} \oplus b_{12} \oplus b_{10}
\]

每次更新：整体左移 1 位，新的 \(b_0 = \text{fb}\)。

1. \(p_0 = \texttt{0x6D3F} = \texttt{0110 1101 0011 1111}\)
   - fb = \(0\oplus1\oplus0\oplus1 = 0\)
   - 左移后低位补 fb → \(p_1 = \texttt{1101 1010 0111 1110} = \texttt{0xDA7E}\)
2. \(p_1 = \texttt{0xDA7E}\)
   - fb = \(1\oplus0\oplus1\oplus0 = 0\)
   - \(p_2 = \texttt{1011 0100 1111 1100} = \texttt{0xB4FC}\)
3. \(p_2 = \texttt{0xB4FC}\)
   - fb = \(1\oplus1\oplus1\oplus1 = 0\)
   - \(p_3 = \texttt{0110 1001 1111 1000} = \texttt{0x69F8}\)

**前 4 拍序列**：

| beat | 值（低 16 位） |
| --- | --- |
| 0（种子） | `0x6D3F` |
| 1 | `0xDA7E` |
| 2 | `0xB4FC` |
| 3 | `0x69F8` |

> 若数据宽度是 32 位，整个 32 位字一起左移，高 16 位会被低位的移位逐步填上。前 4 拍的完整 32 位字为 `0x00006D3F`、`0x0000DA7E`、`0x0001B4FC`、`0x000369F8`——低 16 位仍是上表的 LFSR 序列。

**预期结果**：你在 testbench 里如果加一条 PRBN 用例并打印写通道数据，前 4 拍应严格等于上表（待本地验证：当前 top_tb.vhd 未单独覆盖 PRBN 的逐拍期望，可自行扩展）。

#### 4.2.5 小练习与答案

**练习 1**：Walking-1 在 32 位数据下，第 32 拍的 Pattern 是什么？

**答案**：循环左移 32 次等于转一整圈，回到种子，即 bit0=1、其余 0，值 = 1。第 33 拍才又走到 bit1。

**练习 2**：为什么 `UpdatePattern` 要和 valid/ready 握手绑定，而不是每个时钟沿都推？

**答案**：握手才代表「这一拍数据真的被对方收/发了」。若每拍都推，遇到 master 暂时没准备好（ready=0）时，序列会多走一步，导致 PatternCnt 与「实际传输的第 N 项」错位，比对全部失败。

---

### 4.3 错误检测与首个错误地址计算

#### 4.3.1 概念说明

写阶段只管送数据；**所有判定都在读阶段 `Read_s` 里发生**。每收到一个 beat，硬件就拿「读回数据」和「当前期待值 `r.Pattern`」逐位比对：

- 不相等 → 错一个，`Errors` 加 1。
- 如果这是**第一个**错，还要把「这个错所在的字节地址」记进 `FirstErrAddr`，并置 `FirstErrFound` 标志，之后再有错也不覆盖这个地址。

这里最绕的一步是「地址换算」：硬件手上的只有 `PatternCnt`（这是第几个 beat）和基地址。要把「第 N 个 beat」翻译成「字节地址」，需要做一次移位。

#### 4.3.2 核心流程

```
Read_s, 检测到一次读握手（r.RdDat_Rdy='1' and RdDat_Vld='1'）:
    # (a) 比对
    if RdDat_Data /= r.Pattern:
        Errors := Errors + 1
        FirstErrFound := '1'                 # 下拍起 v.FirstErrFound=1
        if r.FirstErrFound = '0':            # 仅本拍（首个错）记录地址
            # (b) 把 beat 索引换算回字节地址
            beat地址 = PatternCnt + (基地址 右移 s)     # s = log2(B)
            FirstErrAddr = beat地址 左移 s             # 低位补 0，还原字节地址
    # (c) 推进序列（与 4.2 相同的 UpdatePattern）
```

数学上（基地址已按 \(B\) 对齐，低位为 0）：

\[
\text{FirstErrAddr} = (\text{PatternCnt} + \lfloor \text{base}/B \rfloor) \cdot B = \text{base} + \text{PatternCnt} \cdot B
\]

即「基地址 + 第 N 拍相对基地址的字节偏移」。

#### 4.3.3 源码精读

比对与地址记录的完整逻辑：

[mem_test.vhd:287-295 — Read_s 里的读回比对、错误累加与首个错误地址记录](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L287-L295)

逐行拆解那两行最关键的地址换算：

```vhdl
AddrBeats_v := resize(r.PatternCnt, AddrBeats_v'length)
             + RegAddr_v(AxiAddrWidth_g-1 downto log2(AxiDataWidth_g/8));
v.FirstErrAddr := shift_left(to_unsigned(0, log2(AxiDataWidth_g/8)) & AddrBeats_v,
                             log2(AxiDataWidth_g/8));
```

- 第一行右半部分 `RegAddr_v(AxiAddrWidth_g-1 downto log2(B))`：取基地址**去掉低 \(s\) 位**的部分，即「基地址的 beat 地址」。加上 `PatternCnt`（当前 beat 索引），得到出错 beat 的 beat 地址 `AddrBeats_v`。
- 第二行 `to_unsigned(0, s) & AddrBeats_v`：在高位前面拼 \(s\) 个 0（撑满地址宽度），再 `shift_left(..., s)`：整体左移 \(s\) 位，低位补 0。等价于 \(\text{AddrBeats\_v} \times B\)，把 beat 地址还原成字节地址。

几个配合点要注意：

- `Errors` / `FirstErrAddr` 在 `Idle_s` 收到 START 时被清零（[L217-220](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L217-L220)），`p_reg` 复位时**不清**它们——这是 u3-l2 讲的「部分复位」。
- 出错判定用的是 `r.Pattern`（寄存后的当前期待值），与写阶段送出的 `WrDat_Data` 共用同一个信号（[L367](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L367)），保证读写序列同源。
- testbench 的实锤用例：OwnAddr、地址 0xA8、大小 0x100 字节、32 位数据。`p_axi` 在第二个 burst（起址 0xE8）把读数据增量从 4 改成 1（[top_tb.vhd:443-447](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L443-L447)）。于是该 burst 第 0 拍（0xE8）数据仍对，第 1 拍起全错，共 15 个错误，首错在该 burst 第 1 拍。
  - 按公式：首错 beat 索引 = 16（第一 burst 全部）+ 1（第二 burst 第 1 拍）= 17。
  - \(\text{FirstErrAddr} = \texttt{0xA8} + 17 \times 4 = \texttt{0xA8} + \texttt{0x44} = \texttt{0xEC}\)。
  - testbench 期望值正是 `0xEC`（[top_tb.vhd:312](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L312)）。公式与实际仿真一致。

#### 4.3.4 代码实践：验证 FirstErrAddr 换算

**目标**：用 testbench 已有用例，确认 `FirstErrAddr = 基地址 + PatternCnt × B`。

**步骤**（源码阅读 + 手算）：

1. 取 Walking-1 注错用例（[top_tb.vhd:349-363](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L349-L363)）：基地址 0x000、大小 0x200 字节、32 位数据。`p_axi` 在起址 0x1C0 的 burst 处把 walking-1 起点加 1（[L516-518](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L516-L518)）。
2. 手算：首错 beat 索引 = 0x1C0 / 4 = 112；\(\text{FirstErrAddr} = 0 + 112 \times 4 = \texttt{0x1C0}\)。
3. 与 testbench 期望（[L362](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L362)）对照，应为 `0x1C0`；错误数应为「0x1C0 到 0x1FF 共 16 个 beat」= 16（[L361](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd#L361)）。

**预期结果**：手算 `FirstErrAddr = 0x1C0`、`Errors = 16`，与 testbench 断言完全一致。如果你改了基地址或数据宽度，套同一个公式即可预测新值——可在本地跑 `sim/run.tcl` 验证（详见 [u1-l3](u1-l3-running-simulation.md)）。

#### 4.3.5 小练习与答案

**练习 1**：64 位数据（\(B=8\)）、基地址 0x100、第一个错发生在读阶段第 5 拍，`FirstErrAddr` 是多少？

**答案**：\(\texttt{0x100} + 5 \times 8 = \texttt{0x100} + \texttt{0x28} = \texttt{0x128}\)。注意 beat 索引 5 在这里指读阶段从 0 起算的第 5 拍，基地址 0x100 本身已是 8 字节对齐。

**练习 2**：如果 `FirstErrFound` 标志不存在（即每个错都覆盖 `FirstErrAddr`），后果是什么？

**答案**：`FirstErrAddr` 会一直被刷新成「最后一个错」的地址，丢失「第一个错」的信息。定位硬件故障时，第一个错往往最关键（后续错误可能是连锁反应），所以硬件用 `FirstErrFound` 锁住首个地址、之后不再覆盖。

**练习 3**：为什么 `AddrBeats_v` 用 `RegAddr_v` 的高位（去掉低 \(s\) 位）而不是直接用完整基地址？

**答案**：因为要把「字节地址」和「beat 索引」放到同一量纲（beat 地址）上才能相加。基地址右移 \(s\) 得到它的 beat 地址，加上 `PatternCnt` 才是出错 beat 的 beat 地址；最后再左移 \(s\) 还原成字节地址。直接相加会把字节地址和 beat 索引混在一起，量纲错。

---

## 5. 综合实践：当一回「人肉内存测试器」

把本讲三块知识串起来，模拟一次完整的 OwnAddress 测试，**全程手算**，最后和 testbench 对账。

**场景**：32 位数据（\(B=4, s=2\)）、基地址 `0xA8`、大小 `0x40` 字节（即 16 个 beat）、Single 模式、OwnAddr pattern。

1. **播种**（`WrCmd_s`）：写出第 0 拍 Pattern 值。
2. **写阶段推进**：写出第 1、2、3 拍的 Pattern 值（套 4.2 的 OwnAddr 公式）。
3. **重新播种**（`RdCmd_s`）：读阶段第 0 拍期待值是多少？
4. **注错**：假设读回时第 5 拍（beat 索引 5）数据出错，其余都对。计算 `Errors` 与 `FirstErrAddr`。
5. **对账**：把 1~4 的结果与 [mem_test.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd) 的公式逐项对应。

**参考答案**：

1. 种子 = `0xA8`（基地址低 32 位）。
2. 第 1 拍 = `0xA8+4 = 0xAC`；第 2 拍 = `0xB0`；第 3 拍 = `0xB4`。
3. 读阶段重新播种，第 0 拍期待值 = `0xA8`（与写阶段同起点）。
4. `Errors = 1`；`FirstErrAddr = 0xA8 + 5×4 = 0xBC`；`FirstErrFound` 锁存，之后再有错也不改 `FirstErrAddr`。
5. 播种见 [L312-327](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L312-L327)，推进见 [L330-345](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L330-L345)，比对/地址见 [L287-295](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L287-L295)。全部吻合。

---

## 6. 本讲小结

- Pattern 的生命周期由两个触发点驱动：`InitPattern`（命令态播种，给出第 0 拍）与 `UpdatePattern`（数据态每次握手后推进一拍）。
- 四种 pattern 都有确定的种子与更新公式：Counter 递增、Walking-1 循环左移、OwnAddress 等于字节地址、PRBN 是 tap 在 15/13/12/10 的 16 位 LFSR。
- 比对只在读阶段 `Read_s` 发生：`RdDat_Data /= r.Pattern` 即错一次，`Errors` 累加。
- 首个错误地址由 `PatternCnt`（beat 索引）换算回字节地址：`FirstErrAddr = 基地址 + PatternCnt × B`，且只用 `FirstErrFound` 锁住首个、之后不覆盖。
- testbench「OwnAddr、0xA8、15 错、首错 0xEC」是这套公式的活证据：beat 索引 17 × 4 + 0xA8 = 0xEC。
- 非法 pattern 编号会落到 `when others => IntError_s`，这是内部错误陷阱的来源之一。

---

## 7. 下一步学习建议

- 想看「这些命令/数据握手是怎么变成真实 AXI4 burst 的」→ [u4-l2 AXI4 主机：命令、burst 与数据流](u4-l2-axi4-master.md)。
- 想看「寄存器侧 START/SIZE/ADDR 是怎么译码进来的」→ [u4-l1 AXI-Lite 从机与寄存器译码](u4-l1-axi-lite-slave.md)。
- 想自己加一条 PRBN 逐拍注错用例、亲手验证 4.2.4 的序列 → [u5-l1 仿真平台：testbench 与 AXI 仿真过程](u5-l1-testbench-and-axi-emulation.md)，在那里你会学到如何用 `axi_apply_rresp_burst` 之类的 psi_tb 辅助过程构造期望数据流。
