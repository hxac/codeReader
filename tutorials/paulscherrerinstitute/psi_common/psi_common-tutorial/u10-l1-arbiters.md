# 仲裁器 arb_priority / arb_round_robin

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚「仲裁器（arbiter）」在多请求者共享一条总线/资源时解决什么问题。
- 读懂 `psi_common_arb_priority` 的实现：如何用一行 `ppc_or` 加边沿检测完成固定优先级仲裁。
- 读懂 `psi_common_arb_round_robin` 的实现：它如何复用两个优先级仲裁器 + 一个掩码寄存器实现「轮流服务」的公平调度。
- 区分两种仲裁器的「请求/许可」接口：一个纯组合无握手，一个带 AXI-S 风格的 `vld/rdy` 握手。
- 在公平性、延迟、资源、饥饿风险之间做出合理的选型判断。

## 2. 前置知识

本讲会用到前面几讲已经建立的几条认知，这里只做最小回顾，不展开：

- **AXI-S 握手（u1-l4）**：一次传输发生在 `VLD` 与 `RDY` 同高那一拍；源端自主拉 `VLD`，宿端决定何时拉 `RDY`。本讲的轮询仲裁器正是用 `grant_vld_o / grant_rdy_o` 这一对信号来表示「许可是否有效」与「许可是否被消费」。
- **`ppc_or` 并行前缀或（u2-l2）**：对一个向量做「从最高位向低位的累积或」，结果向量的第 `i` 位 = 输入中所有 `>= i` 位的或。它是本讲两个仲裁器的核心算子。
- **二进程 record 设计法（u7-l1）**：把所有寄存器收进一个 record `r`，组合进程算次态 `r_next`，时序进程只打拍/复位。轮询仲裁器的掩码寄存器就是用这种方式管理的。

如果你对这些概念还不熟，建议先翻对应讲义再回来。

> 关键术语：**仲裁（arbitration）**——多个请求者（requester）同时想用同一个共享资源（总线、存储端口、DMA 通道……），需要一个组合/时序逻辑在每个时钟周期选出一个且仅一个请求者给予许可（grant）的过程。**优先级仲裁**总是选优先级最高的；**轮询（round-robin）仲裁**轮流选，保证每个请求者都能被服务到。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [hdl/psi_common_arb_priority.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_arb_priority.vhd) | 固定优先级仲裁器，最高位（最左 bit）优先级最高。纯组合核心，可选输出寄存。 |
| [hdl/psi_common_arb_round_robin.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_arb_round_robin.vhd) | 轮询仲裁器。内部例化**两个** `arb_priority`，加一个掩码寄存器实现轮流服务，带 `vld/rdy` 握手。 |
| [hdl/psi_common_logic_pkg.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_logic_pkg.vhd) | 提供 `ppc_or`，是两个仲裁器的共用算子。 |
| [testbench/psi_common_arb_priority_tb/psi_common_arb_priority_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_arb_priority_tb/psi_common_arb_priority_tb.vhd) | 优先级仲裁器自检测试平台，固定 `size_g=5`、`out_reg_g=true`。 |
| [testbench/psi_common_arb_round_robin_tb/psi_common_arb_round_robin_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_arb_round_robin_tb/psi_common_arb_round_robin_tb.vhd) | 轮询仲裁器自检测试平台，含「常驻请求轮流」与「`rdy` 反压保持」两组典型场景。 |

## 4. 核心概念与源码讲解

### 4.1 优先级仲裁 arb_priority

#### 4.1.1 概念说明

当一个共享资源同一拍收到多个请求时，最简单的裁决规则是「**固定优先级**」：给每个请求者规定一个静态的优先级，每拍总是把许可发给当前请求中优先级最高的那一个。

`psi_common_arb_priority` 把这件事做到了极致的简洁：它约定 **最高位（最左 bit，即 `req_i(width_g-1)`）优先级最高**，每拍输出一个 one-hot 的 `grant_o`，表示这一拍许可了谁。

它的特点是：

- 输入 `req_i`：每位代表一个请求者，「1」=有请求。
- 输出 `grant_o`：one-hot，同一拍至多一位为 1（没人请求时全 0）。
- **无握手**：只要请求在，许可就在；请求撤了许可立刻撤。它本身不记任何状态。
- 可选输出寄存（`out_reg_g`）：寄存一拍可以改善时序，代价是许可延迟一个时钟周期才出现。

固定优先级的代价是**饥饿（starvation）**：如果高优先级的请求者一直占着资源，低优先级者可能永远得不到服务。这正是 4.2 轮询仲裁要解决的问题。

#### 4.1.2 核心流程

核心思想只有两步，关键是把「找最高位的 1」转化为「对前缀或的结果做边沿检测」。

设输入向量 `req`，宽度 `W`，记 `P = ppc_or(req)`，即

\[
P_k \;=\; \bigvee_{j=k}^{W-1} \text{req}_j
\]

也就是「第 `k` 位或更高位中是否存在请求」。那么「第 `k` 位是最高请求位」等价于「`k` 或更高有请求，但 `k+1` 或更高没有请求」：

\[
\text{grant}_k \;=\; P_k \;\wedge\; \neg P_{k+1}, \qquad P_W \triangleq 0
\]

最高位 `k=W-1` 时 `P_{k+1}` 取常量 0，所以 `grant_{W-1} = P_{W-1} = req_{W-1}`，与直觉一致。

源码用一句移位实现了 `P_{k+1}`：把 `P` 左移一位并补 0（即 `'0' & P(high downto 1)`），再取反、与 `P` 相与即可。

流程伪代码：

```
P      := ppc_or(req_i)                       -- 前缀或
shifted := '0' & P(P'high downto 1)          -- P 下移一位 = P_{k+1}
grant  := P and (not shifted)                -- 边沿检测 → one-hot 最高位
```

#### 4.1.3 源码精读

实体声明很简洁，三个 generic、四个端口：

[hdl/psi_common_arb_priority.vhd:L21-L30](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_arb_priority.vhd#L21-L30) —— 实体声明。注意 `out_reg_g`（true=寄存输出，false=纯组合）与 `rst_pol_g`（复位极性）。注释明确「最左 bit 优先级最高」。

核心仲裁就在组合进程的两行里：

[hdl/psi_common_arb_priority.vhd:L41-L49](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_arb_priority.vhd#L41-L49) —— `p_comb`：先对请求做 `ppc_or`，再用 `and not ('0' & ...)` 做边沿检测得到 one-hot 许可。这两行就是整个仲裁算法。

输出寄存由 `out_reg_g` 在编译期二选一：

[hdl/psi_common_arb_priority.vhd:L52-L67](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_arb_priority.vhd#L52-L67) —— `g_reg` 分支把 `Grant_I` 打一拍（带同步复位）；`g_nreg` 分支直接组合输出。`out_reg_g` 在 u7-l1 中我们已经见过类似的 `if generate` 二选一套路。

> 小贴士：`g_non_zero : if width_g > 0 generate`（第 39 行）是为了避免 `width_g=0` 时 `std_logic_vector(-1 downto 0)` 这种非法范围声明。这是库内对边界 generic 的常见保护。

`ppc_or` 本身在 logic 包里用一个 `for` 循环按 log₂ 深度展开成并行前缀树：

[hdl/psi_common_logic_pkg.vhd:L162-L184](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_logic_pkg.vhd#L162-L184) —— `ppc_or` 函数体。综合后是一条深度 `O(log W)` 的或门链，比「每位都或掉自己上方所有位」的朴素写法（深度 `O(W)`）更适合宽仲裁器。

#### 4.1.4 代码实践

**目标**：用测试平台的断言确认「最高位优先」与「`out_reg_g` 的延迟效应」。

**操作步骤**：

1. 打开 [testbench/psi_common_arb_priority_tb/psi_common_arb_priority_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_arb_priority_tb/psi_common_arb_priority_tb.vhd)，定位到「Multi Bit」段落（约 L132-L161）。
2. 跟读这段激励（DUT 配置为 `size_g=5`、`out_reg_g=true`）：
   - L135：`req_i <= "10111"`。**预测**：最高请求位是 bit 4，所以下一拍 `grant_o` 应为 `"10000"`。
   - L142：`req_i <= "00111"`。**预测**：最高位是 bit 2，许可 `"00100"`。
   - L146：`req_i <= "00011"`。**预测**：最高位 bit 1，许可 `"00010"`。
   - L150：`req_i <= "10001"`。**预测**：最高位 bit 4，许可 `"10000"`。
3. 对照 `StdlvCompareStdlv(...)` 断言，确认你的预测与 TB 期望值（`"10000"`、`"00100"`、`"00010"`、`"10000"`、`"00001"`）逐条一致。
4. 再看 L119-L123：拉高 `req_i` 的**同一拍** `grant_o` 仍是 `"00000"`，**下一拍**才变成 `"01000"`——这正是 `out_reg_g=true` 带来的一拍寄存延迟。

**需要观察的现象**：许可信号比请求晚一拍出现；同一拍内无论请求如何变化，许可只反映上一拍的请求。

**预期结果**：上述 5 个预测全部命中 TB 断言。

**运行方式**：按 u1-l3 描述的回归流程，在 `sim/` 下 `source run.tcl`（或 GHDL 的 `runGhdl.tcl`）即可跑这个 TB，出错时会打印以 `###ERROR###` 开头的报文。若你暂时无法本地仿真，可直接把上面的「读 TB 断言」当作「源码阅读型实践」完成。**待本地验证**（实际仿真波形）。

#### 4.1.5 小练习与答案

**练习 1**：`req_i = "01010"`（5 位），`out_reg_g=false`，`grant_o` 是多少？

**答案**：`"01000"`。最高请求位是 bit 3（`req_i` 从高到低 `0,1,0,1,0`），故许可 bit 3。用公式验证：`P=ppc_or("01010")="01111"`，`shifted='0'&"0111"="00111"`，`grant = "01111" and not "00111" = "01111" and "11000" = "01000"`。✓

**练习 2**：为什么 `arb_priority` 不需要任何复位？把 `out_reg_g` 设为 `false` 时它有寄存器吗？

**答案**：仲裁核心是纯组合的 `ppc_or` + 边沿检测，不存任何状态，所以组合分支（`out_reg_g=false`）既无寄存器也无复位。只有 `out_reg_g=true` 的输出寄存器需要复位，复位时把 `grant_o` 清零（源码 L56-L57）。

---

### 4.2 轮询仲裁 arb_round_robin

#### 4.2.1 概念说明

固定优先级的致命问题是**低优先级饥饿**。轮询（round-robin）仲裁的思路是：**轮流**给每个请求者机会——服务完一个之后，下一次优先服务「位置更低」的请求者；都服务完一轮再回到最高位，循环往复。

`psi_common_arb_round_robin` 用一个非常优雅的工程技巧实现了这一点：**它不重新发明仲裁逻辑，而是例化两个 `arb_priority`**——一个看「被掩码后的请求」（只允许低于上一位许可者的请求），一个看「原始请求」（兜底）。再用一个寄存器 `Mask` 记住「上次许可到哪了」，每次成功握手后把掩码收缩到「低于本次许可位」的位置。

它与 `arb_priority` 的接口差异也很关键：

- 多了 `grant_vld_o`（输出，许可有效，即 `grant_o != 0`）和 `grant_rdy_o`（**输入**，许可消费方表示「我接收了这次许可」）这对 AXI-S 风格握手。
- 只有在 `grant_vld_o='1'` **且** `grant_rdy_o='1'`（即一次握手完成）时，内部 `Mask` 才会推进。消费方没准备好时，许可会**保持不变**。

> 命名提醒：`grant_rdy_o` 尽管带 `_o` 后缀，但在实体里声明为 `in std_logic`（[L26](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_arb_round_robin.vhd#L26)）。这里的 `_o` 是站在「许可消费方」的视角命名的（那是它的输出），读代码时要以方向 `in` 为准。

#### 4.2.2 核心流程

设请求 `req`，宽度 `W`，当前掩码 `Mask`（复位为全 0）。每一拍：

1. **掩码请求**：`RequestMasked = req and Mask`。`Mask` 的某位为 1 表示「这个位置在本轮仍可被许可」。
2. **两次优先级仲裁**：
   - `GrantMasked = arb_priority(RequestMasked)`：在「本轮尚未轮到过的低位」里选最高者；
   - `GrantUnmasked = arb_priority(req)`：在全体请求里选最高者（兜底）。
3. **选择**：若 `GrantMasked != 0`，用它；否则用 `GrantUnmasked`。即「低位还有没服务的就服务低位，否则回到最高位重新开始一轮」。
4. **掩码推进（仅握手时）**：当 `Grant_v != 0` 且 `grant_rdy_o='1'` 时，把掩码更新为「严格低于本次许可位」的位置全 1、其余 0：

\[
\text{Mask}_{\text{new}}[k] = 1 \;\Longleftrightarrow\; k < g
\]

其中 `g` 是本次许可的位。代码里用 `'0' & ppc_or(Grant_v(high downto 1))` 实现：先丢掉 bit 0 再前缀或，得到「`< g` 的位全 1」。

掩码推进后，低于 `g` 的位仍可被许可；许可到 bit 0 后掩码变全 0，下一拍回到 `GrantUnmasked`，完成「绕回最高位」。

用 5 位、常驻请求 `"10111"`（请求者在 bit 4、2、1、0）演示一轮（`grant_rdy_o` 恒为 1）：

| 拍 | Mask（许可前） | RequestMasked | GrantMasked | GrantUnmasked | grant_o | 说明 |
|---|---|---|---|---|---|---|
| 0 | `00000`（复位） | `00000` | `00000` | `10111`→`10000` | `10000` | 兜底选最高位 bit4 |
| 1 | `01111` | `00111` | `00100` | — | `00100` | 低位选 bit2 |
| 2 | `00111` | `00111` | `00010` | — | `00010` | 低位选 bit1 |
| 3 | `00001` | `00001` | `00001` | — | `00001` | 低位选 bit0 |
| 4 | `00000` | `00000` | `00000` | `10000` | `10000` | 绕回 bit4 |

这正是「4→2→1→0→4→…」的循环，所有请求者都被公平服务。该序列在测试平台的「Multi Bit」段被逐拍断言（见 4.2.4）。

#### 4.2.3 源码精读

实体声明，注意握手信号方向：

[hdl/psi_common_arb_round_robin.vhd:L19-L28](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_arb_round_robin.vhd#L19-L28) —— 实体声明。`grant_rdy_o` 是 `in`、`grant_vld_o` 是 `out`，构成一对许可握手。

掩码寄存器用二进程 record 法（承接 u7-l1）：

[hdl/psi_common_arb_round_robin.vhd:L33-L40](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_arb_round_robin.vhd#L33-L40) —— record `two_process_r` 只有一个字段 `Mask`，外加掩码/许可的中间信号。整个组件唯一的状态就是这个掩码。

组合进程算掩码请求、选许可、算掩码次态：

[hdl/psi_common_arb_round_robin.vhd:L53-L65](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_arb_round_robin.vhd#L53-L65) —— L53 算 `RequestMasked`；L56-L60 在 `GrantMasked` 非零时用它、否则用 `GrantUnmasked`；L63-L65 **仅当许可非零且 `grant_rdy_o='1'`** 时把掩码收缩到本次许可位之下。

许可有效信号与许可输出：

[hdl/psi_common_arb_round_robin.vhd:L68-L73](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_arb_round_robin.vhd#L68-L73) —— `grant_vld_o` 在 `Grant_v != 0` 时拉高，与 `grant_o` 同为组合输出（轮询仲裁器没有 `out_reg_g` 选项）。

时序进程只更新掩码并处理复位：

[hdl/psi_common_arb_round_robin.vhd:L80-L88](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_arb_round_robin.vhd#L80-L88) —— 复位把 `Mask` 清零（回到「从最高位开始」的初始状态）。

最精彩的部分——**两个优先级仲裁器实例**：

[hdl/psi_common_arb_round_robin.vhd:L90-L114](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_arb_round_robin.vhd#L90-L114) —— `i_prio_masked` 吃 `RequestMasked`、`i_prio_unmasked` 吃原始 `request_i`，两个都设 `out_reg_g=>false`（保持组合）。轮询的「公平」完全靠掩码 + 兜底仲裁实现，没有重写任何「找最高位」的逻辑。

#### 4.2.4 代码实践

**目标**：用测试平台的「常驻请求」段验证 4.2.2 表格里的轮流序列。

**操作步骤**：

1. 打开 [testbench/psi_common_arb_round_robin_tb/...vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_arb_round_robin_tb/psi_common_arb_round_robin_tb.vhd)。
2. 先看初始化：L108 `grant_rdy_o <= '1'`（消费方始终准备好），所以许可每拍都会推进掩码。
3. 跟读「Multi Bit」段（L131-L173）：
   - L132 `request_i <= "10000"` → 首拍许可 `"10000"`（兜底，掩码还是全 0）。
   - L137 改 `request_i <= "10111"`，之后请求保持不变。**预测**接下来许可依次为 `00100`→`00010`→`00001`→`10000`→`00100`…（与 4.2.2 表格一致，只是起点从 bit4 开始）。
4. 对照 L139/L144/L148/L152/L156 的断言（`"00100"`、`"00010"`、`"00001"`、`"10000"`、`"00100"`），确认轮流顺序。
5. 再看「Rdy Low」段（L179-L239）：把 `grant_rdy_o` 拉低后，观察 L194/L204 的 `"not kept"` 断言——许可在消费方未就绪时**保持不变**，掩码不推进。

**需要观察的现象**：常驻相同请求时许可按位递减循环；`grant_rdy_o='0'` 期间许可冻结、恢复后从断点继续。

**预期结果**：轮流序列与「not kept」保持行为均命中断言。**待本地验证**（实际波形）。

#### 4.2.5 小练习与答案

**练习 1**：复位后第一次有请求时，为什么总是最高位得到许可？

**答案**：复位把 `Mask` 清成全 0（L85），于是 `RequestMasked = req and 0 = 0`，`GrantMasked = 0`，选择逻辑（L56-L60）退到 `GrantUnmasked = arb_priority(req)`，即最高位。所以「起点」是固定优先级行为，之后才进入轮流。

**练习 2**：假设 4 个请求者常驻请求，但其中一个请求者（bit 1）一直没请求。轮询序列会怎样？

**答案**：仍然轮流，但 bit 1 永远不会被许可。例如请求常驻 `"1011"`（bit3,2,0 有请求，bit1 无），序列为 `1000`(bit3) → `0100`(bit2) → `0001`(bit0) → `1000`(bit3) → …。掩码照样经过 bit1 的位置（许可 bit0 时掩码已是 `0001`，再许可 bit0 后变全 0 绕回），bit1 因无请求被自然跳过——这正是轮询「无饥饿但按需服务」的特性。

---

### 4.3 请求/许可接口与握手

#### 4.3.1 概念说明

两个仲裁器的接口差异，本质上是「**有没有状态、需不需要被消费方确认**」的差异。

- `arb_priority` 是**无状态组合件**：请求在，许可就在；它不关心许可有没有被用掉。适合「许可只用来做组合选通，下一拍请求自然变化」的场景，比如组合多路选择器的选择信号。
- `arb_round_robin` 是**有状态时序件**：内部掩码记录「轮到谁了」，**必须**知道许可何时被消费，才能决定何时推进到下一位。所以它引入了 `grant_vld_o / grant_rdy_o` 这对握手，复用 u1-l4 的 AXI-S 语义。

#### 4.3.2 核心流程

轮询仲裁器的握手状态可以归纳为：

```
每一拍（组合）：
  grant_o     = 本拍选出的许可（one-hot，可能全 0）
  grant_vld_o = (grant_o != 0)

握手发生（vld=1 且 rdy=1 的那拍）：
  → 下一拍掩码 Mask 收缩到本次许可位之下（轮询推进）

握手未发生（rdy=0 或 vld=0）：
  → 掩码保持不变 → 同样的请求会再次得到同样的许可（许可「保持」）
```

注意一个**反直觉但正确**的设计：`grant_rdy_o='0'` 时许可**不会消失**，而是**保持**。这是因为掩码不动、请求不动，组合逻辑自然算出同一个许可。消费方什么时候准备好，什么时候接受这个许可，掩码才往前走一步。

#### 4.3.3 源码精读

握手推进条件就在掩码更新的 `if` 里：

[hdl/psi_common_arb_round_robin.vhd:L63-L65](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_arb_round_robin.vhd#L63-L65) —— 只有 `(Grant_v /= 0) and (grant_rdy_o = '1')` 才更新 `v.Mask`。这两个条件缺一个，掩码就保持 `v := r`（L50）不变。

许可有效信号的定义：

[hdl/psi_common_arb_round_robin.vhd:L68-L72](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_arb_round_robin.vhd#L68-L72) —— `grant_vld_o` 直接由 `Grant_v` 是否非零决定，与 `grant_o` 同为组合输出，没有额外寄存。

对比 `arb_priority`：它只有 `req_i → grant_o`，没有任何 `vld/rdy`。读 [hdl/psi_common_arb_priority.vhd:L25-L29](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_arb_priority.vhd#L25-L29) 即可确认端口表里没有握手信号。

#### 4.3.4 代码实践

**目标**：在测试平台里直接观察「`rdy` 拉低 → 许可保持」的握手行为。

**操作步骤**：

1. 在 round_robin TB 中定位「Rdy Low」段（L179-L239）。
2. L185 把 `grant_rdy_o <= '0'`，L187 设 `request_i <= "10011"`。
3. L189 断言此刻 `grant_o = "10000"`（最高位，兜底）。
4. L191 过一拍、L193 把 `grant_rdy_o` 拉高前，L194 断言 `grant_o` **仍是** `"10000"`（`"grant_o 12 not kept"`）——即消费方没就绪的两拍里许可被冻结在 bit4。
5. L197 再次拉低 `grant_rdy_o`，L199 断言许可推进到 `"00010"`——因为上一拍握手完成，掩码已推进。

**需要观察的现象**：`grant_rdy_o` 的每一次「低→高」翻转，恰好对应掩码的一次推进；翻转之间许可纹丝不动。

**预期结果**：所有 `"not kept"` 与推进断言命中。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `arb_round_robin` 的 `grant_rdy_o` 恒定接 `'1'`，它的「外部行为」和 `arb_priority` 有什么区别？

**答案**：`grant_rdy_o='1'` 时掩码每拍都推进，许可会按轮询顺序循环变化；而 `arb_priority` 在请求不变时许可恒定不变（总给最高位）。前者公平、后者固定优先级——这正是两者的根本差别，握手的有无只是表象。

**练习 2**：能否把 `arb_priority` 直接当一个「不需要握手的轮询仲裁器」用？

**答案**：不能。`arb_priority` 无状态，请求不变则许可不变，无法实现「轮流」。轮询必须靠 `arb_round_robin` 的掩码寄存器记住「上次许可到谁」。

---

### 4.4 选型权衡

#### 4.4.1 概念说明

选哪种仲裁器，是在四个维度上权衡：**公平性、最坏延迟、资源/时序、饥饿风险**。没有绝对的最优解，要看应用场景对哪一项敏感。

典型场景对照：

- **中断控制器 / 异常处理**：高优先级中断必须先响应 → 用 `arb_priority`，把最紧急的中断接到最高位。
- **多 DMA / 多主机共享一条 AXI 总线**：各主机地位平等，不能让某个主机饿死 → 用 `arb_round_robin`。
- **组合多路选择器的选择信号**（纯组合、无时钟）：用 `arb_priority` 配 `out_reg_g=false`，零状态、零延迟。
- **需要许可跨多拍稳定 / 被消费方确认**：用 `arb_round_robin`，靠 `vld/rdy` 握手精确控制掩码推进。

#### 4.4.2 核心流程（对比表）

| 维度 | `arb_priority` | `arb_round_robin` |
|---|---|---|
| 算法 | 固定优先级（最高位胜） | 掩码 + 两个优先级仲裁，轮流 |
| 状态 | 无（纯组合核心） | 有（掩码寄存器） |
| 握手 | 无 | `grant_vld_o` / `grant_rdy_o`（AXI-S 风格） |
| 公平性 | 不公平，低优先级可能饥饿 | 公平，每位都会被服务 |
| 最坏等待 | 低优先级可能无限等待 | 有限（最坏等 `W-1` 个其他请求者） |
| 输出寄存可选 | 是（`out_reg_g`） | 否（输出组合） |
| 资源 | 一个 `ppc_or` + 边沿检测 | 两个 `ppc_or` + 边沿检测 + 掩码寄存器 |
| 设计复用 | 基础件 | **内部例化两个 `arb_priority`** |

#### 4.4.3 源码精读

轮询仲裁器对优先级件的复用关系一目了然：

[hdl/psi_common_arb_round_robin.vhd:L90-L114](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_arb_round_robin.vhd#L90-L114) —— 轮询仲裁器**不自己算最高位**，而是例化两个 `arb_priority`（掩码版 + 兜底版）。这是库内「组合复用」的典范：复杂调度策略建立在简单基础件之上。

`arb_priority` 的资源代价就是 `ppc_or` 的深度：

[hdl/psi_common_logic_pkg.vhd:L162-L184](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_logic_pkg.vhd#L162-L184) —— `ppc_or` 用并行前缀树把深度压到 `O(log W)`，所以即便宽仲裁器（如 32 位）也能跑较高频率。

#### 4.4.4 代码实践

**目标**：在文档示例段里观察「请求者中途加入/退出」时轮询的自适应性。

**操作步骤**：

1. 打开 round_robin TB 的「Example from documentation」段（[L241-L304](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_arb_round_robin_tb/psi_common_arb_round_robin_tb.vhd#L241-L304)）。
2. 注意 L255 之前 `grant_rdy_o='0'`，所以 L247 设 `request_i <= "10110"` 后，L249/L252/L257 连续三拍许可都是 `"10000"`——掩码没推进（没握手）。
3. L255 拉高 `grant_rdy_o` 后，许可开始轮流：`10000`→`00100`→`00010`→`10000`（L261/L265/L269）。
4. L281 把请求改成 `"01100"`（bit3、bit2 有请求，bit4、bit1、bit0 退出），观察许可如何在新请求集合上继续轮询（`"01000"`→`"00100"`）。

**需要观察的现象**：请求集合变化后，轮询立即按新集合继续，不需要重新初始化。

**预期结果**：断言全部命中。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：一个系统有 8 个等价请求者共享一个资源，要求任何一个请求者最坏情况下等待时间有上界。选哪个仲裁器？为什么？

**答案**：选 `arb_round_robin`。固定优先级的 `arb_priority` 会让低优先级请求者在高优先级常驻时无限等待；轮询仲裁器保证每位最多等其余 7 位各服务一次，最坏等待有界。

**练习 2**：如果系统里有一个「绝对不能延迟」的关键请求者和几个普通请求者，该怎么做？

**答案**：可以单独把关键请求者接到 `arb_priority` 的最高位，保证只要有请求就立刻许可；其余请求者接低位。或者用两级结构：关键请求者用 `arb_priority` 直通，普通请求者之间用 `arb_round_robin` 公平调度，再把两路用一个小 `arb_priority` 合并（关键位优先）。这正是「混合优先级」常见做法。

---

## 5. 综合实践

**任务**：对 4 路同时请求，分别用两种仲裁器**预测**许可顺序，并用源码/测试平台验证。

设仲裁器宽度 `width_g = 4`，4 个请求者常驻请求，即 `req_i = "1111"`（bit3、bit2、bit1、bit0 都有请求），消费方始终就绪。

**步骤 1 — 优先级仲裁器预测**

`arb_priority`（`out_reg_g=true`）每拍都返回最高位：

| 拍 | req_i | grant_o |
|---|---|---|
| 0 | `1111` | `0000`（寄存延迟，本拍尚未生效） |
| 1 | `1111` | `1000` |
| 2 | `1111` | `1000` |
| 3 | `1111` | `1000` |

**结论**：许可恒为 `1000`，bit2/1/0 **饥饿**。

**步骤 2 — 轮询仲裁器预测**

`arb_round_robin`（`grant_rdy_o='1'`）每握手一次推进一位：

| 拍 | req_i | grant_o | grant_vld_o |
|---|---|---|---|
| 0 | `1111` | `1000` | 1 |
| 1 | `1111` | `0100` | 1 |
| 2 | `1111` | `0010` | 1 |
| 3 | `1111` | `0001` | 1 |
| 4 | `1111` | `1000` | 1（绕回） |

**结论**：许可在 4 位间循环 `1000→0100→0010→0001→1000→…`，无人饥饿。

**步骤 3 — 验证**

- 优先级行为：对照 [arb_priority_tb L135-L141](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_arb_priority_tb/psi_common_arb_priority_tb.vhd#L135-L141)（`req_i="10111"` 连续两拍都断言 `grant_o="10000"`），确认「常驻请求 → 恒定许可最高位」。
- 轮询行为：对照 [arb_round_robin_tb L131-L153](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_arb_round_robin_tb/psi_common_arb_round_robin_tb.vhd#L131-L153)（`request_i="10111"` 后许可 `00100→00010→00001→10000`），确认「常驻请求 → 逐位递减循环」。

**步骤 4 — 扩展思考**

如果把综合实践的 `req_i` 改成 `"1101"`（bit3、bit2、bit0 请求，bit1 无请求）：

- 优先级：恒为 `1000`。
- 轮询：`1000→0100→0001→1000→…`（bit1 被自然跳过）。

把这两种预测写下来，再去读 TB 断言核对，你就把本讲的两个核心算法彻底吃透了。

> 说明：以上许可序列是基于源码逻辑推导的预测；实际波形**待本地验证**（用 u1-l3 的回归流程跑两个 TB）。

## 6. 本讲小结

- 仲裁器解决「多请求者共享一资源」问题，每拍从请求向量中选出一个且仅一个给予 one-hot 许可。
- `arb_priority` 用 `ppc_or`（前缀或）加边沿检测，两行代码完成固定优先级仲裁——最高位优先级最高，无状态、可选输出寄存。
- `arb_round_robin` 不重写仲裁逻辑，而是**例化两个 `arb_priority`**（掩码版 + 兜底版），配一个掩码寄存器实现轮流服务；掩码在握手完成时收缩到本次许可位之下，到 bit 0 后绕回最高位。
- 接口差异即状态差异：`arb_priority` 无握手；`arb_round_robin` 用 `grant_vld_o / grant_rdy_o`（AXI-S 风格）决定何时推进掩码，`rdy='0'` 时许可保持不变。
- 公平性 vs 简单性：固定优先级简单但可能饥饿，适合有明确优先级的场景（中断）；轮询公平但有状态、资源略多，适合平等的多主机/多 DMA 共享总线。
- 两个仲裁器都建立在 u2-l2 的 `ppc_or` 与 u7-l1 的二进程 record 设计法之上，是这些基础范式的小型综合应用。

## 7. 下一步学习建议

- **u10-l2 脉冲/斜坡生成与整形**：继续本单元的「生成类」组件，看 `strobe` 如何驱动斜坡与脉冲整形。
- **回顾 u2-l2**：若你对 `ppc_or` 的并行前缀树细节还不够清楚，回去重读 logic 包的实现，体会它如何把 `O(W)` 深度压到 `O(log W)`。
- **回顾 u9-x AXI 主机/从机系列**：当你想知道仲裁器在真实 AXI 系统里「接在哪」时，结合 `axi_master_simple` / `axi_slave_ipif` 思考多主机共享一条 AXI 总线时轮询仲裁器的接入位置——这是本讲知识在系统级的应用。
- **动手扩展**：尝试基于 `arb_priority` 写一个「带掩码寄存器但用格雷码指针记录上次许可位」的变体，对比它与本讲掩码写法的资源差异（提示：本讲的掩码只需一次 `ppc_or`，格雷码方案需要二进制↔格雷码转换）。
