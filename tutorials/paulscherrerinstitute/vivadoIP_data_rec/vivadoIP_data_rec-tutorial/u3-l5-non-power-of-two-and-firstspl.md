# 非二次幂深度与 FirstSplAddr 起始地址

## 1. 本讲目标

本讲聚焦一个看似不起眼、却差点成为 bug 源头的细节：**当存储深度不是 2 的整数次幂时，记录器的地址运算要怎么写才正确**。学完后你应当能够：

- 说清 `NonPwr2MemDepth_c` 这个布尔常量如何用 `log2` 与 `log2ceil` 判定 `MemoryDepth_g` 是不是二次幂；
- 解释为什么**二次幂深度的地址运算是"天生免费"的**，而**非二次幂深度必须显式处理借位（borrow）**；
- 写出核心记录器中 `FirstSpl_3` 在两种情况下的计算公式，并指出代码里负责借位的那几行；
- 说明封装层 `g_pwr2mem` / `g_npwr2mem` 两个 generate 块如何把软件给出的线性读地址映射回环形 RAM 的物理地址；
- 理解 `FirstSplAddr` 为何是把"环形缓冲"读成"线性波形"的关键纽带。

本讲承接 [u3-l4（地址/采样计数器）](u3-l4-address-and-sample-counters.md)：那里讲了触发时刻 `AdrCnt_2`、`SplCnt_2` 的取值，本讲则用这些值算出"录制窗口里第一个样本落在环形缓冲的哪个地址"。

## 2. 前置知识

在进入源码前，先用通俗语言澄清四个概念。

**（a）环形缓冲（circular buffer / ring buffer）。** 记录器把样本不停地写进一块固定大小的 RAM，写指针一直往前走，走到末尾就回到 0 重新开始，像一个首尾相接的环。读出时，"时间上最早的那一个样本"可能落在环上任意位置，不一定是地址 0。

**（b）二次幂带来的好处。** 当 RAM 深度恰好是 \(2^k\)（如 32、64、128）时，地址总线正好 \(k\) 位，无符号加减法在溢出时会**自动对 \(2^k\) 取模**——也就是自动回绕，等价于对深度取模。这条"免费"性质让二次幂深度的地址代码极其简洁。

**（c）无符号减法的回绕（borrow / 下溢）。** 在 VHDL 的 `unsigned` 类型上做 `a - b`，当 \(a < b\) 时结果不是负数，而是回绕成一个大数：相当于 \((a - b) \bmod 2^N\)（\(N\) 为位宽）。对二次幂深度这恰好就是 \((a-b) \bmod \text{depth}\)；对非二次幂深度则**不是**，必须人工把深度加回来修正。

**（d）`log2` 与 `log2ceil`。** 这是 `psi_common_math_pkg` 提供的两个函数：`log2(x)` 给出"最高有效位的位置"（对二次幂是精确值，对非二次幂是向下取整），`log2ceil(x)` 给出 \(\lceil \log_2 x \rceil\)（向上取整，即"容纳 x 至少需要多少位地址"）。两者**当且仅当 x 是二次幂时相等**——这正是判定非二次幂的钥匙。

> 承接 [u3-l4](u3-l4-address-and-sample-counters.md)：触发时刻 `SplCnt_2` 恒为 `PreTrigSpls+1`，`AdrCnt_2` 则取决于在 `WaitTrig` 状态等了多久（可能已经绕环一圈）。本讲直接复用这两个结论。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `hdl/data_rec.vhd` | 核心记录器 RTL | `NonPwr2MemDepth_c` 定义、`FirstSpl_3` 计算的两个分支、`FirstSplAddr` 输出端口 |
| `hdl/data_rec_vivado_wrp.vhd` | Vivado 封装层 | `NonPwr2MemDepth_c`（同一份定义）、`g_pwr2mem` / `g_npwr2mem` 读地址生成、每通道 TDP RAM 读端口 |
| `hdl/data_rec_register_pkg.vhd` | 寄存器/地址地图 | `Mem_Addr_c` 存储区起点、`MemAddr()` 函数揭示通道间距与寻址布局 |
| `Changelog.md` | 版本变更 | v2.3.2 修复非二次幂回绕 bug 的记录 |
| `sim/config.tcl` | 仿真配置 | 用 `MemoryDepth_g=32` 与 `=30` 各跑一次，专门覆盖非二次幂路径 |

## 4. 核心概念与源码讲解

### 4.1 NonPwr2MemDepth_c：如何判定"非二次幂深度"

#### 4.1.1 概念说明

记录器的存储深度由 generic `MemoryDepth_g` 决定（见 [u3-l1](u3-l1-data-rec-entity.md)）。它可以是任意正整数，比如 32（二次幂），也可以是 30（非二次幂）。

这两种值在地址运算上**性质完全不同**：

- 二次幂深度 \(= 2^k\)：地址总线 \(k\) 位，无符号运算自动以 \(2^k\) 为模回绕，等价于对深度取模。地址代码可以写得很简单。
- 非二次幂深度（如 30）：地址总线需要 \(\lceil \log_2 30 \rceil = 5\) 位（范围 0..31），但环形缓冲实际只用 0..29，地址 30、31 是"空洞"。此时无符号运算以 \(2^5 = 32\) 为模回绕，**不等于**以 30 为模，必须显式修正。

为了让 RTL 在编译期就选择正确的地址运算分支，代码用一个布尔常量 `NonPwr2MemDepth_c` 来标记"当前深度不是二次幂"。

#### 4.1.2 核心判定原理

判定逻辑只有一行，但背后的数学很干净：

\[
\text{NonPwr2MemDepth\_c} \;=\; \big(\,\log_2(\text{MemoryDepth\_g}) \;\neq\; \log_2\lceil\rceil(\text{MemoryDepth\_g})\,\big)
\]

为什么这能判定？因为 `log2` 与 `log2ceil` **当且仅当参数是二次幂时才相等**：

| `MemoryDepth_g` | `log2`（向下/精确） | `log2ceil`（向上） | 是否相等 | 结论 |
| --- | --- | --- | --- | --- |
| 32 | 5 | 5 | 相等 | 二次幂 |
| 128 | 7 | 7 | 相等 | 二次幂 |
| 30 | 4 | 5 | 不等 | **非二次幂** |
| 100 | 6 | 7 | 不等 | **非二次幂** |

于是"两者不等" ⟺ "非二次幂"，一个不等式就完成了判定，无需递归或位扫描。

#### 4.1.3 源码精读

这个常量在**核心记录器与封装层里各定义了一次**，内容完全相同——这是有意为之：两个文件都需要在编译期做同一套分支选择，又都不想依赖对方，于是各自本地定义。

核心记录器中的定义（注释直接点明了用途）：

[hdl/data_rec.vhd:83-84](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L83-L84) —— 注释"More complex logic is required to support non-power-of-two recorder depth"说明了它存在的全部理由；常量值就是 `log2 /= log2ceil`。

封装层中一字不差的同一行：

[hdl/data_rec_vivado_wrp.vhd:112-113](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L112-L113) —— 封装层读出地址同样需要按深度是否二次幂走不同分支（见 4.3）。

> 提示：`log2ceil` 还决定了很多端口的位宽，例如 [hdl/data_rec.vhd:71](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L71) 中 `Mem_Adr` 宽度就是 `log2ceil(MemoryDepth_g)-1 downto 0`。深度 30 时它是 5 位（0..31），这正是"空洞"30、31 存在的根因。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `log2` 与 `log2ceil` 在四种深度下的取值，确认判定逻辑。

**操作步骤**（源码阅读型）：

1. 打开 [hdl/data_rec.vhd:84](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L84)。
2. 假装自己是综合器，对下表四个 `MemoryDepth_g` 取值分别心算 `log2`、`log2ceil` 与 `NonPwr2MemDepth_c`。
3. 再打开 [sim/config.tcl:57-61](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/sim/config.tcl#L57-L61)，确认仿真确实用 32 与 30 两种深度各跑一次。

**需要观察的现象**：32 时 `log2=log2ceil=5` → 二次幂分支；30 时 `log2=4, log2ceil=5` → 非二次幂分支。两条路径都被仿真覆盖。

**预期结果**：填表如下。

| `MemoryDepth_g` | `log2` | `log2ceil` | `NonPwr2MemDepth_c` |
| --- | --- | --- | --- |
| 16 | 4 | 4 | false |
| 32 | 5 | 5 | false |
| 30 | 4 | 5 | **true** |
| 48 | 5 | 6 | **true** |

#### 4.1.5 小练习与答案

**练习 1**：如果把 `MemoryDepth_g` 设成 1，`NonPwr2MemDepth_c` 是 true 还是 false？
**答案**：false。\(1 = 2^0\) 是二次幂，`log2(1)=0`、`log2ceil(1)=0`，两者相等。

**练习 2**：能否把判定改写成"检查 `MemoryDepth_g` 的二进制里是否只有 1 个 1"？两种写法等价吗？
**答案**：等价。"只有 1 个 1"正是二次幂的定义；而 `log2 == log2ceil` 是它的另一种判别式。项目选择后者是因为 `psi_common_math_pkg` 已提供这两个函数，一行即可。

---

### 4.2 FirstSpl_3：触发时刻算出"第一个样本"的地址

#### 4.2.1 概念说明：为什么需要 FirstSplAddr

记录器向环形 RAM 不停写入，写指针在环上一直转。录制完成后，软件经 AXI 总线希望**按时间顺序线性读出**整段波形：读"第 0 个样本"应得到时间最早的样本，"第 1 个"是次早的，依此类推。

但环形缓冲里"时间最早的样本"几乎不会停在地址 0——它取决于触发那一刻写指针转到了哪里。因此记录器必须在触发瞬间**记下"第一个样本"落在环上的地址**，把这个地址通过端口 `FirstSplAddr` 交给封装层，封装层读出时再把线性序号换算成物理地址。

换句话说：

- **写入侧**是环形的（地址随时间绕环增长）；
- **读出侧**是线性的（软件要 0,1,2,… 顺序）；
- `FirstSplAddr` 就是连接两者的"对齐基准"。

#### 4.2.2 核心流程

触发发生时（状态机从 `WaitTrig` 迁入 `PostTrig`，产生 `Trigger_2` 脉冲，见 [u3-l2](u3-l2-recorder-state-machine.md)），记录器要计算：

\[
\text{FirstSpl} \;=\; \big(\text{AdrCnt}\_2 \;-\; \text{PreTrigSpls}\big) \;\bmod\; \text{MemoryDepth\_g}
\]

直觉：触发样本当前写到 `AdrCnt_2`，而整段录制里最早的样本比它早 `PreTrigSpls` 个位置，往回数 `PreTrigSpls` 步就是第一个样本的地址；往回走可能越过地址 0，于是要"绕回"到环的另一端——这就是取模的物理含义。

取模的实现，二次幂与非二次幂截然不同：

- **二次幂**（`not NonPwr2MemDepth_c`）：直接写 `AdrCnt_2 - PreTrigSpls`，让 `unsigned` 减法**自动**以 \(2^k\) 为模回绕。因为深度就是 \(2^k\)，自动回绕等价于对深度取模，**一行搞定，无需 if**。
- **非二次幂**（`NonPwr2MemDepth_c`）：自动回绕以 \(2^{\lceil\log_2\text{depth}\rceil}\) 为模，**不等于**对深度取模，所以必须**判断是否借位**：若 `AdrCnt_2` 够减就直接减；若不够减（下溢），就把 `MemoryDepth_g` 加回来。

#### 4.2.3 源码精读

`FirstSpl_3` 是 `data_rec_r` record 里的一个字段，宽度与 `Mem_Adr` 相同（`unsigned(Mem_Adr'range)`）：

[hdl/data_rec.vhd:113](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L113) —— `FirstSpl_3 : unsigned(Mem_Adr'range);`。带 `_3` 后缀表示它在 Stage3（对齐存储器写端口的那一级，见 [u3-l3](u3-l3-two-process-and-pipeline.md)）。

计算逻辑全部集中在一处，关键是那个 `if not NonPwr2MemDepth_c then ... else ...`：

[hdl/data_rec.vhd:308-321](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L308-L321) —— 整段逐行解读：

- **L309** `if r.Trigger_2 = '1' then`：只在触发那一拍（`Trigger_2` 为 1）才更新 `FirstSpl_3`，其它拍保持原值。注意用的是**已寄存的** `r.Trigger_2`（这是触发脉冲在流水线中的对齐点，见 [u3-l3](u3-l3-two-process-and-pipeline.md) 中"命名数字后缀即流水级"）。
- **L311-312** `if not NonPwr2MemDepth_c then v.FirstSpl_3 := r.AdrCnt_2 - unsigned(PreTrigSpls);`：**二次幂分支**，直接相减，靠 `unsigned` 自动回绕完成取模。
- **L314-319** `else ...`：**非二次幂分支**，先比大小：
  - L315 `if r.AdrCnt_2 > unsigned(PreTrigSpls) then`：够减（严格大于），L316 直接减；
  - L317-318 `else`：不够减（含相等），`v.FirstSpl_3 := r.AdrCnt_2 - unsigned(PreTrigSpls) + MemoryDepth_g;` —— **把深度加回来修正借位**。这就是 v2.3.2 那个 bug 修复的核心代码点。

算出来的 `FirstSpl_3` 通过端口送出：

[hdl/data_rec.vhd:353](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L353) —— `FirstSplAddr <= std_logic_vector(r.FirstSpl_3);`。

端口本身在 entity 里这样声明（宽度同样是 `log2ceil(MemoryDepth_g)` 位）：

[hdl/data_rec.vhd:73](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L73) —— 注释"Address of the first sample in the recording buffer"。

> 关于借位分支的边界：代码用**严格大于** `>` 判断。意图是把"无借位"（够减）与"有借位"（不够减）两种情形分开。在正常录制（触发样本的写指针 `AdrCnt_2` 尚未绕环回到很小值）时走减法分支；只有当缓冲在触发前已绕过一圈、`AdrCnt_2` 变得很小时才走 `+MemoryDepth_g` 分支。这一句就是把二次幂"免费"得到的模运算，手工补回到非二次幂上。

#### 4.2.4 代码实践

**实践目标**：对比两个分支，亲手用非二次幂数字验证借位修正。

**操作步骤**（手算型，可立即验证）：

1. 读 [hdl/data_rec.vhd:311-319](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L311-L319) 两个分支。
2. 设 `MemoryDepth_g = 30`（非二次幂），分别对下面两组触发时刻取值手算 `FirstSpl_3`：
   - 场景 A（够减）：`AdrCnt_2 = 12`，`PreTrigSpls = 5`。
   - 场景 B（借位）：`AdrCnt_2 = 3`，`PreTrigSpls = 5`。
3. 再用二次幂分支的公式（直接相减、靠回绕）算一遍，体会"为什么二次幂不需要 if"。

**需要观察的现象**：场景 A 直接减即得正数；场景 B 出现 `3 - 5` 的下溢，必须 `+30` 才能得到环上正确地址。

**预期结果**：

- 场景 A：`12 > 5` → `12 - 5 = 7`。（第一个样本在地址 7。）
- 场景 B：`3 ≤ 5` → `3 - 5 + 30 = 28`。（往回数 5 步越过 0，绕到环另一端的 28。）
- 校验：\( (3 - 5) \bmod 30 = (-2) \bmod 30 = 28 \)，与代码一致。
- 若误用二次幂分支直接做 `unsigned(3) - unsigned(5)`（5 位宽），会得到 \( (3-5) \bmod 32 = 30 \)，落在"空洞"地址上——这正是修复前会踩的坑。

> 说明：上表用 `AdrCnt_2` 的取值是**为说明借位算术而设的假设值**，并非断言某一拍的真实寄存器值；触发时刻 `AdrCnt_2` 的真实取值见 [u3-l4](u3-l4-address-and-sample-counters.md)。

#### 4.2.5 小练习与答案

**练习 1**：`MemoryDepth_g = 64`（二次幂），`AdrCnt_2 = 2`，`PreTrigSpls = 10`。走哪个分支？结果是多少？
**答案**：走二次幂分支（直接相减）。`unsigned(2) - unsigned(10)` 在 6 位宽下自动回绕：\((2-10) \bmod 64 = 56\)。无需 `if`。

**练习 2**：把练习 1 的深度换成 `MemoryDepth_g = 60`（非二次幂），其余不变，结果应是多少？
**答案**：走非二次幂分支，`2 ≤ 10` → `2 - 10 + 60 = 52`。校验 \((2-10) \bmod 60 = 52\)。

**练习 3**：为什么二次幂分支"敢于"不判断借位？
**答案**：因为深度 \(= 2^k\) 时，地址正好 \(k\) 位，`unsigned` 减法的自动回绕就是以 \(2^k\) 为模，等价于以深度为模，结果天然落在合法范围 \(0 .. \text{depth}-1\)。非二次幂时地址位宽向上取整（如 30 用 5 位），自动回绕以 32 为模，会落到 30、31 的空洞，所以必须显式修正。

---

### 4.3 封装层 g_pwr2mem / g_npwr2mem：读出时再做一次对齐

#### 4.3.1 概念说明：读出侧的对称问题

`FirstSplAddr` 只是"第一个样本的物理地址"。软件读数据时给的是**线性序号**（第 0 个、第 1 个……），封装层必须把"线性序号 `spl`"换算成"环形 RAM 物理地址"。换算公式是 4.2 那个取模的**镜像版本**——这次是加法：

\[
\text{RAM_addr} \;=\; \big(\text{spl} \;+\; \text{FirstSplAddr}\big) \;\bmod\; \text{MemoryDepth\_g}
\]

直觉：从"第一个样本"的地址开始，每读一个样本地址 \(+1\)，越过末尾就绕回 0。这又一次遇到"以 \(2^k\) 为模 vs 以 depth 为模"的分歧，于是封装层同样用两个 generate 块分别处理。

#### 4.3.2 核心流程

先看软件给出的 AXI 地址如何被拆解（地址布局见 [u2-l2](u2-l2-register-and-memory-map.md) 与 `MemAddr()` 函数 [hdl/data_rec_register_pkg.vhd:80-86](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L80-L86)）：

- 低 2 位是字节偏移（每样本 4 字节，丢弃）；
- 中间 `log2ceil(MemoryDepth_g)` 位是**样本序号** `spl`；
- 再往上 3 位是**通道选择** `AxiMemSel`（最多 8 通道）。
- 通道间距 \(= 2^{\lceil\log_2\text{depth}\rceil}\) 个样本（向上取整到二次幂），所以通道号可以直接从地址高位截取——这跟 [u2-l2](u2-l2-register-and-memory-map.md) 里 `MemAddr` 的 `ChannelSpacing_c := 2**log2ceil(memdepth)` 完全对应。

拿到 `spl` 后：

- **二次幂**：`AxiMemAdr := spl + FirstSplAddr`，加法自动以 \(2^k\) 为模回绕 = 以深度为模，一行。
- **非二次幂**：先把 `FirstSplAddr` 扩展一位（`'0' & FirstSplAddr`）再做加法，得到一个**不会提前回绕**的"未回绕地址"；若它 ≥ `MemoryDepth_g` 就减去一次深度。因为 `spl` 与 `FirstSplAddr` 都小于深度，其和 \(< 2 \times \text{depth}\)，最多减一次深度即可落在合法范围。

#### 4.3.3 源码精读

封装层里负责读地址生成的两个 generate 块，正好与核心层的两个分支**一一对应**：

**二次幂情形**（注释直言"address logic is relatively simple"）：

[hdl/data_rec_vivado_wrp.vhd:511-514](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L511-L514) —— `g_pwr2mem : if not NonPwr2MemDepth_c generate`。把 `mem_addr` 中截出的样本序号 `mem_addr(log2ceil(MemoryDepth_g)+1 downto 2)` 直接加上 `FirstSplAddr`，结果宽度不变，靠加法自动回绕。与核心层 [hdl/data_rec.vhd:312](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L312) 的"直接相减"是同一思想：二次幂时模运算免费。

**非二次幂情形**（注释说明"more complex ... prone to slow timing, therefore it is only implemented if required"——只在需要时才综合，省面积、保时序）：

[hdl/data_rec_vivado_wrp.vhd:516-525](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L516-L525) —— `g_npwr2mem : if NonPwr2MemDepth_c generate`，逐行解读：

- **L518** `signal AxiMemAddrUnwrapped : std_logic_vector(AxiMemAdr'high+1 downto 0);`：比 `AxiMemAdr` **多一位**的中间信号，用来承载"加法后但未回绕"的值，避免提前溢出。
- **L521** `AxiMemAddrUnwrapped <= ... unsigned('0' & FirstSplAddr)`：把 `FirstSplAddr` 前面补一个 `'0'` 扩展一位再相加，和的位数足够，**不会**在加法阶段错误回绕。
- **L522-523** `MemAddrFull <= AxiMemAddrUnwrapped when unsigned(AxiMemAddrUnwrapped) < MemoryDepth_g else std_logic_vector(unsigned(AxiMemAddrUnwrapped) - MemoryDepth_g);`：**关键判断**——若未回绕地址 < 深度，直接用；否则减去一次深度。这与核心层 [hdl/data_rec.vhd:315-318](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L315-L318) 的借位判断互为镜像（一个减法借位 `+depth`，一个加法溢出 `-depth`）。
- **L524** `AxiMemAdr <= MemAddrFull(AxiMemAdr'range);`：截回 `AxiMemAdr` 的位宽送进 RAM 读端口。

算出的 `AxiMemAdr` 送给每通道 TDP RAM 的 B 口（读端口）：

[hdl/data_rec_vivado_wrp.vhd:545-568](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L545-L568) —— `g_mem` 为每个通道实例化一块 `psi_common_tdp_ram`：A 口在**数据时钟域** `Clk` 接收记录器的写（`RecMemAdr/RecMemWr/RecMemData`），B 口在 **AXI 时钟域** `s00_axi_aclk` 用 `AxiMemAdr` 读出（`b_addr_i => AxiMemAdr`）。这正是双端口 RAM 跨时钟域读出的典型用法（跨时钟域策略详见 [u5-l2](u5-l2-clock-domain-crossing.md)）。

而 `FirstSplAddr` 这根信号，是从核心记录器端口一路接到这里的：

[hdl/data_rec_vivado_wrp.vhd:508](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L508) —— `FirstSplAddr => FirstSplAddr`（记录器实例的端口映射）。

通道选择则由 `mem_read_mux` 进程从地址高位截取：

[hdl/data_rec_vivado_wrp.vhd:528-533](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L528-L533) —— `AxiMemSel <= mem_addr(log2ceil(MemoryDepth_g)+4 downto log2ceil(MemoryDepth_g)+2);` 取出 3 位通道号，再用它从 8 路读出 `AxiMemOut(0..7)` 中 mux 出当前通道的数据（mux 与符号扩展见 [hdl/data_rec_vivado_wrp.vhd:534-542](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L534-L542)，完整读出机制将在 [u5-l3](u5-l3-recording-memory-and-readout.md) 详讲）。

#### 4.3.4 代码实践

**实践目标**：用一个完整的非二次幂读地址链路，验证 `g_npwr2mem` 的回绕逻辑。

**操作步骤**（手算型）：

1. 读 [hdl/data_rec_vivado_wrp.vhd:517-525](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L517-L525)。
2. 设 `MemoryDepth_g = 30`，假设某次录制后 `FirstSplAddr = 28`。软件依次读样本 `spl = 0, 1, 2, 3, ..., 19`，逐个算出送进 RAM 的物理地址 `AxiMemAdr`。
3. 同时用二次幂公式 `spl + FirstSplAddr`（不加判断）算一遍，对比哪里会出错。

**需要观察的现象**：从 `spl = 2` 开始，`28 + spl` 超过 30，必须减 30 才回到合法地址；若不减，会读到 30、31 的空洞。

**预期结果**（节选）：

| `spl` | `AxiMemAddrUnwrapped = 28 + spl` | 是否 ≥ 30？ | `AxiMemAdr`（送入 RAM） |
| --- | --- | --- | --- |
| 0 | 28 | 否 | 28 |
| 1 | 29 | 否 | 29 |
| 2 | 30 | 是 → −30 | 0 |
| 3 | 31 | 是 → −30 | 1 |
| … | … | … | … |
| 19 | 47 | 是 → −30 | 17 |

读出的物理地址序列 `28, 29, 0, 1, …, 17` 正是**从 `FirstSplAddr` 开始、绕环展开的线性波形**——这就把环形缓冲"拉直"了。校验：测试平台 `CheckData` 按 `spl = 0..samples-1` 顺序读并期望值 `= startValue + spl`（[testbench/top_tb/top_tb_pkg.vhd:142-147](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_pkg.vhd#L142-L147)），能通过就证明这条回绕链路正确。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `g_npwr2mem` 里要先用 `'0' & FirstSplAddr` 把地址扩展一位，而不是直接相加？
**答案**：直接相加会让和落在原来的 \(k\) 位里，一旦超过 \(2^k-1\) 就被自动截断（以 \(2^k\) 为模回绕），后续的"是否 ≥ 深度"判断就失去了真实和值。扩展一位后，和最多 \(2 \times \text{depth} - 2 < 2^{k+1}\)，能完整保存，再判断一次减深度即可。

**练习 2**：`g_pwr2mem` 与 `g_npwr2mem` 为什么必须互斥（一个 `not NonPwr2MemDepth_c`、另一个 `NonPwr2MemDepth_c`）？
**答案**：两者都驱动同一个信号 `AxiMemAdr`，若同时生效会造成多驱动（multiple drivers）综合错误。`NonPwr2MemDepth_c` 是编译期常量，综合时只有一个 generate 块会被保留。

**练习 3**：核心层用"减法 + 借位加深度"，封装层用"加法 + 溢出减深度"，为什么一个是加、一个是减？
**答案**：方向相反。核心层是"从触发样本**往回**数 `PreTrigSpls` 步"找起点（减法，可能借位）；封装层是"从起点**往后**数 `spl` 步"找物理地址（加法，可能溢出）。两者都是对深度取模，只是加减方向不同。

---

## 5. 综合实践

把本讲三块知识串起来，做一个**端到端的非二次幂手算推演**。

**场景**：`MemoryDepth_g = 30`，`PreTrigSpls = 5`，`TotalSpls = 20`。触发时刻记录器内部 `AdrCnt_2 = 3`（一个会发生借位的取值）。

**任务**：

1. **判定**：`NonPwr2MemDepth_c` 取值？核心层与封装层分别走哪个分支？
2. **算 FirstSplAddr**：用 [hdl/data_rec.vhd:314-319](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L314-L319) 的公式算出 `FirstSplAddr`。
3. **算读地址**：软件读 `spl = 0..19`，用 [hdl/data_rec_vivado_wrp.vhd:521-524](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L521-L524) 的逻辑列出每个 `spl` 对应的物理 RAM 地址。
4. **反思**：若有人误把核心层的二次幂分支（[hdl/data_rec.vhd:312](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L312)）用到深度 30 上，`FirstSplAddr` 会算成多少？它会怎样破坏后续读出？

**参考答案**：

1. `log2(30)=4 ≠ log2ceil(30)=5` → `NonPwr2MemDepth_c = true`。核心层走 `else` 借位分支，封装层走 `g_npwr2mem` 块。
2. `AdrCnt_2=3 ≤ PreTrigSpls=5` → `FirstSplAddr = 3 - 5 + 30 = 28`。
3. 读地址 = `(28 + spl) mod 30`：`spl=0→28, 1→29, 2→0, 3→1, …, 19→17`（即 `28,29,0,1,…,17`）。这条序列把环从地址 28 起线性展开，共 20 个样本。
4. 误用二次幂分支：`unsigned(3) - unsigned(5)` 在 5 位宽下 = `(3-5) mod 32 = 30`。`FirstSplAddr = 30` 落在合法范围（0..29）之外的空洞；读出时 `g_npwr2mem` 还会拿这个 30 去做加法，导致整段波形**错位、读到陈旧/未初始化样本**。这正是 v2.3.2 修复的 bug 形态（见 [Changelog.md:5-7](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/Changelog.md#L5-L7)）。

> 提示：若你本地装了 Modelsim/Questa 与 PsiSim，可按 [u1-l3](u1-l3-run-simulation.md) 跑回归仿真。`sim/config.tcl` 已经把 `MemoryDepth_g=30` 这条非二次幂路径编进 CI（[sim/config.tcl:58-60](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/sim/config.tcl#L58-L60)），跑通即等价于验证了上述整条链路。若暂无仿真环境，上述手算即为可复现的"待本地验证"项。

## 6. 本讲小结

- `NonPwr2MemDepth_c` 用 `log2(MemoryDepth_g) /= log2ceil(MemoryDepth_g)` 一行判定非二次幂；两者相等当且仅当深度是二次幂。
- 二次幂深度时，地址位宽正好 \(k=\log_2\text{depth}\) 位，`unsigned` 加减法自动以 \(2^k\) 为模回绕，等价于对深度取模——地址代码"天生免费"。
- 非二次幂深度时，地址位宽向上取整到 \(k=\lceil\log_2\text{depth}\rceil\)，自动回绕以 \(2^k\) 为模（≠ 以深度为模），会出现 30、31 这样的空洞，必须显式处理借位/溢出。
- 核心记录器在 [hdl/data_rec.vhd:308-321](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L308-L321) 计算 `FirstSpl_3`：二次幂直接 `AdrCnt_2 - PreTrigSpls`；非二次幂判断借位后 `+ MemoryDepth_g`。
- 封装层在 [hdl/data_rec_vivado_wrp.vhd:511-525](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L511-L525) 用 `g_pwr2mem` / `g_npwr2mem` 做镜像处理：二次幂直接 `spl + FirstSplAddr`；非二次幂扩展一位相加再判断 `≥ depth` 减回。
- `FirstSplAddr` 是把"环形写入"翻译成"线性读出"的对齐基准；它由核心层在触发瞬间产出，由封装层在读出时消费，两者必须用同一套模运算才能对齐。这正是 v2.3.2 修复（[Changelog.md:5-7](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/Changelog.md#L5-L7)）所保护的关键链路。

## 7. 下一步学习建议

- 进入 [u4-l1（触发源总览与 TrigEna 掩码）](u4-l1-trigger-sources-and-masking.md)，从"地址/长度"转向"触发"主题，理解 `Trigger_2` 脉冲是如何由三类触发源合成的——而本讲的 `FirstSpl_3` 正是在这个脉冲触发下才算出的。
- 若想更完整理解读出侧，可先跳读 [u5-l3（录制存储：每通道双端口 RAM 与读出）](u5-l3-recording-memory-and-readout.md)，它会展开本讲提到的 `mem_read_mux` 通道 mux 与符号扩展细节。
- 想验证本讲结论的读者，建议结合 [u6-l2（六个测试用例的覆盖设计）](u6-l2-test-cases-coverage.md) 中的 case0，对照 `MemoryDepth_g=30` 这条非二次幂仿真运行，确认 `CheckData` 的期望值与本讲手算一致。
