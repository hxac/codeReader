# 优先级与轮询仲裁

## 1. 本讲目标

读完本讲，你应当能够：

- 说清楚「仲裁器（arbiter）」到底在解决什么问题，并能区分**固定优先级**与**轮询（round-robin）**两种公平性策略的本质差别。
- 逐行读懂 `priority_enc.sv` 如何**复用** `reverse_vector` + `leave_one_hot` + `pos2bin` 三个组合积木，拼出一个「MSB 优先」的固定优先级编码器。
- 读懂 `round_robin_enc.sv` 这个「朴素轮询版」的设计意图与缺陷：它的优先级指针是自由计数器，因而「平均公平」却「吞吐受限」。
- 读懂 `round_robin_performance_enc.sv` 如何用**双倍位宽拼接 + 掩码**这一经典技巧，把「找下一个有效请求并循环回绕」压成一次组合运算，从而做到「只要有请求就每拍都授权」。
- 能够自己写一个最小 testbench，对 8 路并发请求分别跑 `priority_enc` 与 `round_robin_enc`，观察并解释两者授权分布的差异。

本讲是 u6 单元里「行为差异最微妙」的一篇：三个模块端口几乎一样，内部策略却完全不同。我们把重点放在**对比**上，而不是逐字背诵代码。

## 2. 前置知识

本讲建立在 u6-l1《编码转换与位反转工具箱》之上，会直接复用那里的三个组合工具。先用一两句话复习：

**复习一：`leave_one_hot` —— 只留下最低的那一 hot 位。**

它扫描输入向量，输出里**只保留最低位的 1**，其余清零。例如 `8'b1101_0010` 变成 `8'b0000_0010`。实现就是对每一位 `out[i] = in[i] && ~( |in[i-1:0] )`，即「自己是 1，且比它低的所有位全是 0」。

**复习二：`pos2bin` —— one-hot 位置转二进制下标。**

把独热码转成它的位置编号。例如 `8'b0001_0000` 变成 `3'd4`。它还顺带给出两个错误标志：`err_no_hot`（没有有效位）和 `err_multi_hot`（多于一个有效位）。

**复习三：`reverse_vector` —— 物理反转位序。**

`in[7]` 变成 `out[0]`，`in[0]` 变成 `out[7]`。它在综合后**不占任何 FPGA 资源**，只是改了连线，但能让「最低位优先」的 `leave_one_hot` 变成「最高位优先」。

> 阅读提示：本讲三个仲裁器都是「从一根多位请求总线里，**每拍挑出一个**获胜者」的电路。区别只在于**按什么规则挑**。请始终带着这个问题读源码：给定同一组并发请求，谁会被选中？

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [priority_enc.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/priority_enc.sv) | **纯组合**的固定优先级编码器。反转输入后复用 `leave_one_hot`+`pos2bin`，MSB 永远最高优先。 |
| [round_robin_enc.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/round_robin_enc.sv) | **朴素轮询版**。内部一个自由计数器当「优先指针」逐拍轮转，平均公平但吞吐受限。 |
| [round_robin_performance_enc.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/round_robin_performance_enc.sv) | **性能优化版**。用双倍位宽拼接 + 掩码做到「有请求就每拍授权」，指针只在授权时推进。 |
| [round_robin_performance_enc_tb.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/round_robin_performance_enc_tb.sv) | 性能版的 testbench，演示随机激励 + 随机复位 + 抖动时钟的注入手法，是本讲实践的样板。 |
| [leave_one_hot.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/leave_one_hot.sv) / [pos2bin.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/pos2bin.sv) / [reverse_vector.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/reverse_vector.sv) | 三个被复用的组合工具（u6-l1 已详解）。 |

> 注意：仓库**只提供了 `round_robin_performance_enc_tb.sv` 一个 testbench**，`priority_enc` 与朴素 `round_robin_enc` 没有现成测试。因此本讲的对比实践需要你自己写一个最小 testbench，我们会在第 5 节给出框架。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，正好对应三种仲裁策略：先看固定优先级（4.1），再看朴素轮询（4.2），最后看性能优化版（4.3）。三者端口高度一致，建议边读边对比。

先统一约定三个模块**共同的输出含义**（这对理解代码至关重要）：

| 输出 | 含义 |
| --- | --- |
| `od_valid` | 本拍是否挑出了获胜者（即输入里至少有一位有效） |
| `od_filt[WIDTH-1:0]` | 「过滤后」的独热码：只有获胜的那一位是 1 |
| `od_bin[WIDTH_W-1:0]` | 获胜位的二进制下标（如第 5 位获胜则 `od_bin = 5`） |

也就是说，`od_filt` 与 `od_bin` 是同一个获胜者的两种表示：一种是 one-hot，一种是二进制。后面你会看到三个模块的差别**全在「怎么定获胜者」**，而输出端口几乎逐字相同。

### 4.1 固定优先级编码：priority_enc

#### 4.1.1 概念说明

「固定优先级」最直白：给输入总线的每一位**预先排好**优先级，谁优先级高谁赢，且这个顺序永远不变。`priority_enc` 采用的约定是 **MSB（最高位）优先**——`id[WIDTH-1]` 优先级最高，`id[0]` 最低。

举几个例子（`WIDTH=8`）：

| 输入 `id` | 获胜位 | `od_bin` |
| --- | --- | --- |
| `8'b0000_0000` | 无（全无效） | `od_valid=0` |
| `8'b0000_1000` | bit 3 | `3` |
| `8'b0001_1000` | bit 4（比 bit 3 高） | `4` |
| `8'b1001_1000` | bit 7（最高） | `7` |

关键观察：只要高位有效，低位永远轮不到。这是它的优点（确定性、可预测），也是它的缺点（低位可能长期「饿死」）。

#### 4.1.2 核心流程

`priority_enc` 自己**几乎不写新逻辑**，而是把活儿全派给 u6-l1 的三个积木。整个数据流是：

```
id[WIDTH-1:0]
   │
   ▼  reverse_vector          （把 MSB 搬到 LSB）
id_r[WIDTH-1:0]
   │
   ▼  leave_one_hot           （只留最低 hot 位 = 原来的最高优先位）
od_filt[WIDTH-1:0]            （获胜者的 one-hot 表示）
   │
   ▼  pos2bin                 （one-hot → 二进制下标）
od_bin[WIDTH_W-1:0]           （获胜者的二进制表示）

od_valid = ~err_no_hot        （pos2bin 报告「至少有一位有效」）
```

这里有一个巧思：`leave_one_hot` 的天然语义是「保留**最低**位」，也就是天然「LSB 优先」。想要「MSB 优先」，不必重写一个反向的 `leave_one_hot`，只要先用 `reverse_vector` 把总线物理翻转一次，那么原来的最高位就变成了最低位，`leave_one_hot` 自然就挑中了它。这就是作者把 `reverse_vector` 放在最前面的原因。

#### 4.1.3 源码精读

模块声明：纯组合，**没有 `clk`/`nrst`**，这是它与两个轮询版的第一个区别。

`WIDTH_W = $clogb2(WIDTH)` 用来算 `od_bin` 的位宽（能表示 0..WIDTH-1 的下标）。

[priority_enc.sv:L27-L36](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/priority_enc.sv#L27-L36) —— 模块端口。注意没有任何时钟/复位端口，确认它是纯组合电路。

第一步：反转输入，把「MSB 优先」改写成「LSB 优先」。

[priority_enc.sv:L41-L47](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/priority_enc.sv#L41-L47) —— 例化 `reverse_vector` 把 `id` 翻转成 `id_r`。注释明确写了「conventional operation of priority encoder is when MSB bits have a priority」（传统优先级编码器是 MSB 优先）。

第二步：在反转后的总线上「只留最低位」= 挑出最高优先位，直接得到 `od_filt`。

[priority_enc.sv:L49-L54](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/priority_enc.sv#L49-L54) —— `leave_one_hot` 的输入是 `id_r`，输出直接接 `od_filt`，省了一个中间信号。

第三步：用 `pos2bin` 的 `err_no_hot` 反推出 `od_valid`，并把 one-hot 转成二进制下标。

[priority_enc.sv:L56-L67](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/priority_enc.sv#L56-L67) —— `od_valid = ~err_no_hot`（输入里有任何一位有效即 valid），`pos2bin` 同时把 `od_filt` 转成 `od_bin`。

> 复用之美：`priority_enc` 的 module 体里只有 3 个例化 + 1 个 `assign`，没有一行「新」算法。这是 basic_verilog「积木复用」哲学的典型样本——把上一讲做好的小工具拼起来就是新模块。

#### 4.1.4 代码实践

**实践目标**：手算验证 `priority_enc` 的「MSB 优先」语义。

**操作步骤**：

1. 假设 `WIDTH=8`，对下面 4 组输入，按 4.1.2 的数据流**手算** `id_r`、`od_filt`、`od_bin`、`od_valid`：
   - `id = 8'b0000_0000`
   - `id = 8'b0000_0100`
   - `id = 8'b0010_0100`
   - `id = 8'b1010_0100`
2. 例如第三组：`id_r = reverse(0010_0100) = 0010_0100`（回文，反转不变），`leave_one_hot` 留下 bit 2 → `od_filt=0000_0100`，`od_bin=2`。

**需要观察的现象**：无论低位有多少个有效位，只要更高位有效，获胜者永远是最高那个。

**预期结果**（待本地验证）：四组的 `od_bin` 依次为 `—`(invalid)、`2`、`5`、`7`。其中第三、四组体现了「MSB 压制 LSB」。

#### 4.1.5 小练习与答案

**练习 1**：若想把 `priority_enc` 改成 **LSB 优先**，最少改动几处？

**答案**：删掉 `reverse_vector` 那一级，把 `leave_one_hot` 的输入从 `id_r` 改成 `id` 即可。因为 `leave_one_hot` 天然就是 LSB 优先。

**练习 2**：`od_filt` 和 `od_bin` 谁更「窄」？为什么两者都要保留？

**答案**：`od_bin` 更窄（只有 `WIDTH_W` 位）。两者是同一信息的两种编码：`od_filt` 是 one-hot（适合直接做掩码、片选），`od_bin` 是二进制（适合送进地址线或做比较）。不同下游用法各取所需，所以模块同时给出。

---

### 4.2 朴素轮询仲裁：round_robin_enc

#### 4.2.1 概念说明

固定优先级的毛病是「饥饿」：一个低优先级请求如果总被高优先级压着，可能永远得不到服务。「轮询（round-robin）」就是解药——**优先级顺序每拍都在变**，转着圈来，让每个请求「平均」都有机会。

`round_robin_enc` 是作者自述的「**尽可能简单**」版本（见文件头注释）。它的做法朴素到有点出人意料：内部放一个**自由计数器**当「优先指针」，指针每拍无条件 `+1`，在 `0 → 1 → … → WIDTH-1 → 0` 之间转圈；当指针恰好指到一位**有效**的请求时，就授权它。

这意味着它的「优先级」不是「上次授权后轮转」，而是「**不管授没授权，每拍都轮转**」。

#### 4.2.2 核心流程

状态机只有一个寄存器 `priority_bit`（优先指针），外加一段组合输出逻辑：

```
每拍（posedge clk）:
    if ~nrst:  priority_bit <= 0
    else if priority_bit == WIDTH-1:  priority_bit <= 0      // 回绕
    else:                             priority_bit <= priority_bit + 1

组合输出:
    if id[priority_bit] == 1:                                // 指针恰好落在有效请求上
        od_valid = 1
        od_filt  = 1 << priority_bit
        od_bin   = priority_bit
    else:                                                    // 指针落在无效位上
        od_valid = 0
        od_filt  = 0
        od_bin   = 0
```

**关键洞察（也是这个版本的缺陷）**：指针是自由轮转的，**不在乎这一拍有没有授权**。于是会出现：

- **全部请求同时有效**时：指针 0,1,2,…,WIDTH-1 转一圈，每拍落到一个有效位上，每拍授权一个不同的请求 → 完美的轮询，每拍都有授权。
- **只有一个请求持续有效**（比如只有 bit 3）时：指针照常每拍 `+1`，但只有当它转到 3 那一拍才授权，其余 7 拍 `od_valid=0`、什么也不做。也就是说，**一个孤单的请求要等满 WIDTH 拍才被服务一次**，吞吐只有理论值的 1/WIDTH。

这正是文件头那句「This module is meant to be as simple as possible. It is possible to make more efficient, but complicated circuit」（本模块力求尽可能简单；可以做得更高效，但电路会更复杂）所指的「不高效」——它换来了极简的电路（一个计数器 + 一个 mux），代价是单请求场景下的等待延迟。下一个模块（4.3）就是那个「更高效但更复杂」的版本。

> 一个常被忽略的点：`od_bin` 在「无效」分支里被赋成 `0`，这和「授权 bit 0」的二进制值冲突。但因为此时 `od_valid=0`，下游应当**只在 `od_valid=1` 时采信 `od_bin`**。三个模块都有这条隐含约定，使用时务必检查 `od_valid`。

#### 4.2.3 源码精读

模块声明：与 `priority_enc` 相比，**多了 `clk` 和 `nrst`**——因为它要保存 `priority_bit` 这个状态。

[round_robin_enc.sv:L36-L47](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/round_robin_enc.sv#L36-L47) —— 注意输出声明成了 `output logic ...`（因为后面用 `always_comb` 驱动它们），而 `priority_enc` 里是连续赋值/例化驱动。

自由计数器：每拍无条件推进，到顶回绕。

[round_robin_enc.sv:L50-L62](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/round_robin_enc.sv#L50-L62) —— `priority_bit` 的 `always_ff`。注意 `if` 条件**完全不依赖 `id`**，也不依赖是否发生过授权——这就是「自由轮转」。

组合输出：只在指针落到有效位时才授权。

[round_robin_enc.sv:L64-L74](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/round_robin_enc.sv#L64-L74) —— `if( id[priority_bit] )` 是一个**动态位选**（variable index part-select），综合后是一个 mux；选中的位为 1 才输出有效。

#### 4.2.4 代码实践

**实践目标**：在脑中（或纸上）模拟指针轮转，体会「单请求等待 WIDTH 拍」。

**操作步骤**：

1. 设 `WIDTH=4`，输入恒为 `id = 4'b0010`（只有 bit 1 持续有效）。
2. 从复位后 `priority_bit=0` 开始，逐拍写下 `priority_bit` 与 `od_valid`、`od_bin`：

| 拍 | priority_bit | id[priority_bit] | od_valid | od_bin |
| --- | --- | --- | --- | --- |
| 0 | 0 | id[0]=0 | 0 | 0 |
| 1 | 1 | id[1]=1 | **1** | **1** |
| 2 | 2 | id[2]=0 | 0 | 0 |
| 3 | 3 | id[3]=0 | 0 | 0 |
| 4 | 0 | id[0]=0 | 0 | 0 |
| 5 | 1 | id[1]=1 | **1** | **1** |

**需要观察的现象**：尽管 bit 1 全程有效、且没有竞争对手，它仍然**每 4 拍才被授权一次**。

**预期结果**（待本地验证）：授权发生在第 1、5、9… 拍，授权间隔恒为 `WIDTH=4`。这正是「平均公平但吞吐受限」的直观体现。

#### 4.2.5 小练习与答案

**练习 1**：为什么说这个模块「平均公平」（each input bit on average has equal chance）？

**答案**：因为指针在 `0..WIDTH-1` 上均匀轮转，每个位置被访问的频率完全相同（都是每 WIDTH 拍一次）。所以当所有请求等概率出现时，每个请求被授权的期望次数相等——这是「平均」意义下的公平。

**练习 2**：把 `WIDTH` 从 8 加到 32，单个持续请求的「最坏授权间隔」会变成多少？这对系统延迟意味着什么？

**答案**：最坏间隔 = `WIDTH` 拍，即从 8 拍恶化到 32 拍。这意味着在请求稀疏的场景下，位宽越大，单个请求的等待延迟越不可接受——这正是 4.3 性能版要解决的问题。

---

### 4.3 性能优化版：round_robin_performance_enc

#### 4.3.1 概念说明

朴素版的问题出在「指针自由轮转、不看请求」。性能版的核心改进就一句话：**指针只在「授权」时才推进，并且推进到「刚刚授权的那一位」**；同时用一次组合运算，**立刻**找出「比上次授权位更高的下一个有效请求，若没有则回绕到最低位」。

效果是：**只要还有任何请求挂着，每拍都授权一个**，绝不空转。文件头注释把动机说得很清楚——「performance boost motivated by skipping inactive inputs while performing round_robin」（通过在轮询时跳过无效输入，获得性能提升）。

#### 4.3.2 核心流程

它用了一个经典技巧——**双倍位宽拼接 + 掩码**——把「带回绕的『找下一个置位位』」压成一次 `leave_one_hot`。直觉如下：

> 想在一个环上找「当前位置之后的下一个有效位」。把总线**复制两份拼起来** `{id, id}`，然后把「当前位置及以下」全部用掩码清零，于是剩下的有效位里，**最低的那个**就是「环上的下一个有效位」（如果本半圈没有，就自然落到上半圈的回绕副本里）。最后把得到的下标对 `WIDTH` 取模，就折叠回原始范围。

形式化一点，设上次授权位为 \(p\)（`priority_bit`），请求向量为 \(r\)（`id`）：

\[
\text{id\_buf} = (\{r, r\})\ \&\ \text{mask}, \quad \text{mask}[i] = \begin{cases}1 & i > p \\ 0 & i \le p\end{cases}
\]

\[
\text{winner\_pos} = \min\{\,i \mid \text{id\_buf}[i]=1\,\} \quad\text{（由 leave\_one\_hot 完成）}
\]

\[
\text{od\_bin} = \text{winner\_pos} \bmod \text{WIDTH}
\]

随后把 `od_bin` 锁存进 `priority_bit`，作为下一拍的起点。这样指针**始终跟在上次授权位后面**，形成真正的「授权后轮转」。

> 为什么取模能正确回绕？双倍副本把环「剪开铺成直线」：下半圈（位置 \(p+1 \ldots \text{WIDTH}-1\)）是「同侧向后」，上半圈（位置 \(\text{WIDTH} \ldots 2\text{WIDTH}-1\)）是「回绕到开头」。两段拼起来覆盖了环上所有「\(p\) 之后」的位置，`leave_one_hot` 选最低位 = 选最近的下一个；`% WIDTH` 再把上半圈的位置映射回 \(0 \ldots \text{WIDTH}-1\)。

与朴素版的对比可以浓缩成一张表：

| 维度 | `round_robin_enc`（朴素） | `round_robin_performance_enc`（性能） |
| --- | --- | --- |
| 指针推进时机 | **每拍**无条件 `+1` | **仅授权时**跳到 `od_bin` |
| 单个持续请求 | 每 `WIDTH` 拍授权一次 | **每拍都授权** |
| 全部请求并发 | 每拍授权一个，轮流 | 每拍授权一个，轮流（行为一致） |
| 关键电路 | 计数器 + 动态位选 mux | 双倍位宽掩码 + `leave_one_hot` + `pos2bin` |
| 面积/时序 | 小、快 | 大（位宽翻倍的或树）、关键路径更长 |
| 适用场景 | 请求密集、位宽小、对单请求延迟不敏感 | 请求稀疏或位宽大、要求每拍都有授权 |

#### 4.3.3 源码精读

模块声明：同样有 `clk`/`nrst`，但内部比朴素版复杂得多。

[round_robin_performance_enc.sv:L35-L46](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/round_robin_performance_enc.sv#L35-L46) —— 端口与朴素版几乎一致，但 `id_buf` 等内部信号是 `2*WIDTH` 位宽。

第一步：构造双倍位宽的掩码与缓冲。注意 `mask[i] = (i > priority_bit)`，即「严格大于上次授权位」的位置才保留。

[round_robin_performance_enc.sv:L52-L65](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/round_robin_performance_enc.sv#L52-L65) —— `{2{id}} & mask` 得到 `id_buf`：两份 `id` 拼起来，下半圈「上次授权位及以下」被清零。

第二步：在 `2*WIDTH` 位宽上复用 `leave_one_hot`，挑出最低有效位（= 环上的下一个有效请求）。

[round_robin_performance_enc.sv:L67-L73](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/round_robin_performance_enc.sv#L67-L73) —— 例化 `leave_one_hot`，位宽参数为 `2*WIDTH`。这里 `id_buf_bin` 用 `WIDTH_W+1` 位（多一位才能编码 `0..2*WIDTH-1`）。

第三步：用 `pos2bin` 把独热转成下标，再 `% WIDTH` 折叠回原始范围，并据此重建 `od_filt`。

[round_robin_performance_enc.sv:L75-L98](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/round_robin_performance_enc.sv#L75-L98) —— `od_bin = id_buf_bin % WIDTH` 是回绕的关键；`od_filt = 1 << od_bin` 把二进制下标重新展成原始位宽的独热码。`od_valid = ~err_no_hot` 沿用 `priority_enc` 的写法。

第四步：指针**只在授权时**推进——锁存本次 `od_bin` 作为下次起点。

[round_robin_performance_enc.sv:L100-L111](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/round_robin_performance_enc.sv#L100-L111) —— `if( od_valid ) priority_bit <= od_bin;` 与朴素版的「无条件 `+1`」形成鲜明对比。这一行就是「授权后轮转」的物化。

> 三个模块的复用关系一目了然：`priority_enc` = reverse + leave_one_hot + pos2bin；`round_robin_performance_enc` = （双倍宽 mask）+ leave_one_hot + pos2bin。两者都把 `leave_one_hot`+`pos2bin` 当作「挑一个 + 转下标」的标准件，区别只在「喂给它的候选集合怎么生成」。

#### 4.3.4 代码实践

**实践目标**：跑通官方提供的 testbench，观察性能版在随机激励下的授权行为。

**操作步骤**：

1. 打开 [round_robin_performance_enc_tb.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/round_robin_performance_enc_tb.sv)，注意它把 `WIDTH_W` 宏定义为 3，即 `WIDTH = 2**3 = 8`。

   [round_robin_performance_enc_tb.sv:L98-L107](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/round_robin_performance_enc_tb.sv#L98-L107) —— 用 `bin2pos` 把随机数的低 3 位转成独热码；不过最终 DUT 的 `id` 接的是 `RandomNumber1[7:0]`（见下一块）。

2. DUT 例化：`id` 接 16 位随机数 `RandomNumber1` 的低 8 位，输出端口悬空（只观察不校验）。

   [round_robin_performance_enc_tb.sv:L109-L118](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/round_robin_performance_enc_tb.sv#L109-L118) —— 典型的「随机激励 + 波形观察」型 testbench，没有自校验。

3. 用 iverilog 编译运行（参考 u1-l3 的工具链）：
   ```bash
   iverilog -g2012 -o sim.vvp \
     round_robin_performance_enc_tb.sv \
     round_robin_performance_enc.sv leave_one_hot.sv pos2bin.sv \
     clk_divider.sv edge_detect.sv c_rand.sv bin2pos.sv
   vvp sim.vvp
   ```
   （若 `c_rand.sv` 在子目录，按实际路径补齐；`clogb2.svh` 需在 include 搜索路径里，可加 `-I.`。）

**需要观察的现象**：把 `RandomNumber1[7:0]`（=`id`）、`RE1.od_valid`、`RE1.od_bin`、`RE1.priority_bit` 拉进波形。只要 `id ≠ 0`，几乎每拍 `od_valid=1`；`od_bin` 始终「跟在」上一拍的 `priority_bit` 之后（带回绕）。

**预期结果**（待本地验证）：与朴素版最大的不同是——当 `id` 持续为某个固定非零值时，`od_valid` **每拍都为 1**，而不会出现朴素版那种「指针扫过无效位」的空转拍。

> 说明：该 testbench 的输出端口未连接，也没有 `$display`，所以「现象」需要靠波形（GTKWave）观察，而不是控制台打印。这符合 u1-l3 讲过的「testbench 是测试仪器」的定位。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `pos2bin` 在性能版里要用 `BIN_WIDTH = WIDTH_W+1`，而 `priority_enc` 里只用 `WIDTH_W`？

**答案**：性能版的候选向量是 `2*WIDTH` 位宽，下标范围是 `0 .. 2*WIDTH-1`，需要 `WIDTH_W+1` 位才能编码（因为 \(2 \times \text{WIDTH}\) 的下标上限比 `WIDTH` 多出一倍）。`priority_enc` 的候选向量只有 `WIDTH` 位，`WIDTH_W` 位足矣。

**练习 2**：若把性能版第 [L105](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/round_robin_performance_enc.sv#L105) 行的 `if( od_valid )` 去掉、改成无条件 `priority_bit <= od_bin;`，会出什么问题？

**答案**：当 `id == 0`（无请求）时，`od_bin` 走 else 分支为 `0`，指针会被不断重置到 0。这本身不致命，但破坏了「无请求时保持上次优先级」的语义——一旦请求恢复，下一拍总是从 0 开始扫描，而不是从上次断点续扫，会轻微损害公平性。当前的 `if( od_valid )` 保证了「无请求时指针原地不动」。

**练习 3**：在 `id` 全 1（所有请求同时持续有效）时，朴素版和性能版的输出序列是否相同？

**答案**：相同。两者都会 `0,1,2,…,WIDTH-1,0,1,…` 地轮流授权，每拍一个。性能版的优势**只在请求稀疏时**才显现；请求全满时两者表现一致，但性能版电路更贵。这是「要不要为边角场景付面积代价」的典型工程取舍。

---

## 5. 综合实践

把三个模块放在一起对比，是理解本讲最有效的办法。任务：**写一个最小 testbench，让 `priority_enc` 和 `round_robin_enc` 面对同一组并发请求，连续运行多拍，记录各自的授权分布并解释差异。**

**系统构成**：

```
        ┌─────────────── 同一个 req[7:0] ───────────────┐
        │                                               │
   priority_enc (WIDTH=8)                     round_robin_enc (WIDTH=8)
   → pe_od_bin, pe_od_valid                   → rr_od_bin, rr_od_valid
```

**操作步骤**：

1. 新建 `arbiter_compare_tb.sv`，参照 [round_robin_performance_enc_tb.sv:L14-L19](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/round_robin_performance_enc_tb.sv#L14-L19) 的写法生成一个时钟（例如 200 MHz），并写一个同步 `nrst`。
2. 同时例化 `priority_enc` 与 `round_robin_enc`（`WIDTH` 都设 8），把它们的 `id` 接到**同一个** `logic [7:0] req` 上。
3. 在 initial 块里施加两组场景，并用 `$display` 打印每一拍的对比：

   - **场景 A（请求稀疏）**：`req = 8'b0000_0010`（只有 bit 1），保持 16 拍。
   - **场景 B（请求并发）**：`req = 8'b1111_1111`（全部有效），保持 16 拍。

   建议每个有效时钟沿后打印：
   ```systemverilog
   $display("t=%0t  req=%b | PE valid=%b bin=%0d | RR valid=%b bin=%0d",
            $time, req, pe_valid, pe_bin, rr_valid, rr_bin);
   ```

**需要观察与解释的四个现象**：

1. **场景 A 下**：
   - `priority_enc` 应**每拍**都 `valid=1, bin=1`（bit 1 是唯一有效位，固定优先级立即选中）。
   - `round_robin_enc` 应**每 8 拍**才有一次 `valid=1, bin=1`，其余 7 拍 `valid=0`（指针自由轮转，只有转到 bit 1 才命中）。
   - 结论：稀疏请求下，固定优先级吞吐远高于朴素轮询。
2. **场景 B 下**：
   - 两者都应**每拍** `valid=1`。
   - `priority_enc` 的 `bin` 应**恒为 7**（MSB 永远压制所有低位 → 低 7 位全部「饿死」）。
   - `round_robin_enc` 的 `bin` 应按 `0,1,2,…,7,0,…` **轮流**出现（真正公平）。
   - 结论：并发请求下，轮询解决了固定优先级的「饥饿」问题。
3. 把 `round_robin_enc` 换成 `round_robin_performance_enc` 重跑场景 A，确认它**每拍** `valid=1`，从而验证 4.3 的性能改进。
4. （延伸）思考：如果你的系统里 8 个请求来自 8 个「重要性不同」的外设，该选哪个仲裁器？如果来自 8 个「地位平等」的 DMA 通道呢？

**预期结果**：上述四点均为「待本地验证」，但现象 1、2 是三个模块定义的直接推论，仿真结果应与之吻合；若不符，多半是 `od_valid` 没被正确检查（见 4.2.2 末尾的约定）。

---

## 6. 本讲小结

- **仲裁器的本质**是「从多位请求总线里，每拍挑出一个获胜者」，三种模块输出端口几乎一致（`od_valid` / `od_filt` / `od_bin`），差别全在「按什么规则挑」。
- **`priority_enc`** 是纯组合、固定优先级（MSB 优先）编码器，自身几乎不写新逻辑，靠 `reverse_vector` + `leave_one_hot` + `pos2bin` 三个积木拼出——这是 basic_verilog 复用哲学的样板。
- **`round_robin_enc`（朴素版）** 用一个**自由轮转**的计数器当优先指针，实现「平均公平」，但代价是单个稀疏请求要等满 `WIDTH` 拍才被服务一次。
- **`round_robin_performance_enc`（性能版）** 用**双倍位宽拼接 + 掩码 + `% WIDTH`** 的经典技巧，把「带回绕的找下一个置位位」压成一次组合运算，做到「有请求就每拍授权」；指针只在授权时推进。
- 三者构成了一个清晰的**工程取舍谱系**：确定性 vs 公平、面积 vs 吞吐、稀疏 vs 并发——选型时应根据请求的到达模式决定，而不是无脑选最复杂的那个。
- 使用任意一个仲裁器时，下游都**必须**先检查 `od_valid`，再采信 `od_bin`/`od_filt`，否则会把「无效」分支里的 `od_bin=0` 误当成「授权 bit 0」。

## 7. 下一步学习建议

- **横向对比**：回到 u6-l1，重新读一遍 `leave_one_hot` 和 `pos2bin`，现在你会看到它们不仅是「编码转换工具」，更是被 `priority_enc` 和 `round_robin_performance_enc` 当作标准件复用的核心积木——理解了这层复用关系，整个 u6 单元就串起来了。
- **纵向深入（仲裁理论）**：本讲的轮询都是「授权一位」的简单编码器。若你想了解**带锁定（locked）、带优先级掩码寄存器、矩阵式 round-robin** 等更接近真实总线（AXI/Wishbone）的仲裁器，可以结合 u5-l4《AXI/总线接口与 logger》里的 `axi4_if` 阅读，思考「主机发出的 `arvalid`/`awvalid` 该如何被一个仲裁器调度」。
- **实践延伸**：把综合实践的 testbench 扩展成**自校验**版（参考 u7-l1 的方法学）——用一个软件黄金模型计算「期望获胜位」，逐拍与 DUT 对比，不一致即 `$error`。这会把你从「看波形」推进到「自动化回归测试」，是 u7 单元的预演。
- **源码阅读**：仓库里与本讲同族的还有 [`priority_enc.sv`](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/priority_enc.sv) 的「兄弟」`encoder.v`（一个固定优先级的纯 Verilog 老式写法），对照阅读能体会「参数化 SystemVerilog 积木」相对「手写 case/if 优先级链」的可维护性优势。
