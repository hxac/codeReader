# 仲裁器：优先级 / 轮询 / 加权轮询

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清「仲裁器（arbiter）」要解决的问题，以及它在一拍内只能挑一个获胜者的硬约束。
- 读懂 Open Logic 三种仲裁器的真实源码：固定优先级 `olo_base_arb_prio`、轮询 `olo_base_arb_rr`、加权轮询 `olo_base_arb_wrr`。
- 理解三种策略在**公平性、饥饿风险、延迟**上的取舍，并按带宽分配需求选型。
- 用 `olo_base_arb_wrr` 设计一个 4:2:1 权重的三请求者实验，统计授权次数比并验证。

## 2. 前置知识

本讲是讲义 u5（时序、仲裁、CRC 与杂项 base）的一篇，依赖 u2-l1（base 包体系）和 u1-l5（编码规范）。开始前请确认你理解下面几个概念：

- **请求者（requester）与授权（grant）**：多个模块（例如多个 DMA 通道、多个 AXI 主机）争抢同一个共享资源（一条总线、一个 RAM 端口、一个发送口）。每个请求者拉起一根 `Req` 信号表示「我现在要用」，仲裁器每拍至多把一根 `Grant` 信号置 1，表示「这一拍轮到你用」。
- **one-hot（独热）**：一根多位向量里**最多只有一位是 1**。仲裁器的授权输出必须是 one-hot（或全 0，表示无人获批），因为资源一拍只能给一个人。
- **优先级（priority）**：给每个请求者排个名次，名次高的先拿。问题是名次低的可能永远拿不到（**饥饿，starvation**）。
- **轮询（round-robin, RR）**：不讲名次，轮流来，每人一次，谁都不吃亏。
- **AXI-S 握手 / 两进程法 / record**：这些在 u1-l5、u2-l2 已建立。本讲的 `arb_rr`、`arb_wrr` 都用两进程法 + record 组织状态。
- **并行前缀或（parallel-prefix OR, `ppcOr`）**：u2-l1 介绍过的 `olo_base_pkg_logic.ppcOr`，它对输入向量做「从最高位向最低位的前缀或」，输出每一位表示「本位及所有更高位里是否至少有一个 1」。它是优先级仲裁器又快又省的关键，本讲会反复用到。

> 约定：Open Logic 的位向量约定是 **最高位（left-most, 下标最大）优先级最高**。本讲说「bit4」就是下标为 4 的那一位（最左边），「bit0」是最右边。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| [src/base/vhdl/olo_base_arb_prio.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_arb_prio.vhd) | 固定优先级仲裁器。用 `ppcOr` + 边沿检测一拍选出最高优先级请求者；是另外两个仲裁器的「积木」。 |
| [src/base/vhdl/olo_base_arb_rr.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_arb_rr.vhd) | 轮询仲裁器。内部实例化**两个** `arb_prio`（一个带掩码、一个不带），靠一个掩码寄存器实现「轮流」。 |
| [src/base/vhdl/olo_base_arb_wrr.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_arb_wrr.vhd) | 加权轮询仲裁器。内部实例化一个 `arb_rr`，再加一个权重计数器，让每个请求者连续拿若干拍后才换人。 |
| [src/base/vhdl/olo_base_pkg_logic.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_logic.vhd) | 提供 `ppcOr`（前缀或）与 `getLeadingSetBitIndex`（取最高置 1 位下标），被三个仲裁器共用。 |
| [src/base/vhdl/olo_base_pkg_array.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_array.vhd) | 提供 `unflattenStlvArray`，`arb_wrr` 用它把扁平的权重向量切成逐个请求者的权重。 |
| [test/base/olo_base_arb_wrr/olo_base_arb_wrr_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/base/olo_base_arb_wrr/olo_base_arb_wrr_tb.vhd) | `arb_wrr` 的 VUnit 测试台，含静态权重、请求变化、动态权重等多组用例，是本讲实践的参考。 |

依赖关系：`arb_wrr` →（实例化）`arb_rr` →（实例化两个）`arb_prio` →（调用）`ppcOr`。三者层层叠加，所以本讲按 **prio → rr → wrr** 的顺序讲解。

## 4. 核心概念与源码讲解

### 4.1 优先级仲裁（olo_base_arb_prio）

#### 4.1.1 概念说明

最简单的仲裁策略是**固定优先级**：给位向量里每一位一个固定名次，**最高位（下标最大）名次最高**。任意一拍，仲裁器在所有置 1 的请求位中，挑出**下标最大的那一位**授权，其余请求者这一拍落空。

它的优点是**实现极简、时序极好**（一条并行前缀或 + 一拍边沿检测就出结果），最高优先级的请求者几乎「零等待」。代价是**可能饥饿**：如果高优先级请求者一直占着不放，低优先级者可能永远拿不到授权。因此它适合「请求者天然有主次」的场景——比如一个「紧急中断通道」对「普通数据通道」，紧急的必须随到随服务。

#### 4.1.2 核心流程

算法分两步，全组合逻辑，一拍完成：

1. **前缀或**：对请求向量 `In_Req` 做 `ppcOr`，得到一个形如 `"0001111"` 的向量——从最高个被置 1 的请求位开始，向低位方向全部填 1。这个「台阶」标记了「最高优先级请求位」的位置。
2. **边沿检测**：在台阶的「0→1 跳变处」就是胜出者。代码用 `OredRequest and not ('0' & OredRequest(high downto 1))` 把向量右移一位、取反、再与原向量相与——唯一留下来为 1 的那一位，就是最高优先级请求位。

例如 `In_Req = "0010100"`（bit4 与 bit2 同时请求）：

```
ppcOr("0010100")        = "0011111"   ← 从 bit4 起向低填 1
shift & not             = "0001111" 取反 = "1110000"
Grant = 0011111 & 1110000 = "0010000"  ← 仅 bit4 获授权
```

胜出者永远是**最高位**的那个请求者（bit4），即使 bit2 也在请求。

`Latency_g` 控制是否在输出端加寄存器：`0` 为纯组合输出，`≥1` 为寄存器流水线（默认 1）。

#### 4.1.3 源码精读

实体声明只有两个泛型与一个请求/授权对，接口极其精简：

[olo_base_arb_prio.vhd:34-45](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_arb_prio.vhd#L34-L45) —— `Width_g` 为请求者个数，`Latency_g` 默认 1（带一级输出寄存）。注意它**没有任何握手信号**：每拍都根据当拍 `In_Req` 直接给出 `Out_Grant`，是一拍一拍的组合/寄存关系。

核心组合进程只有两行实质逻辑——先前缀或、再边沿检测：

[olo_base_arb_prio.vhd:64-72](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_arb_prio.vhd#L64-L72) —— `OredRequest_v := ppcOr(In_Req)` 算出「台阶」，`Grant_I <= OredRequest_v and not ('0' & OredRequest_v(OredRequest_v'high downto 1))` 在台阶跳变处切出唯一的一位。

输出寄存器用 `for` 移位的写法实现可变级数流水线，复位写在进程末尾的覆盖里（符合 u1-l5 的同步高有效复位约定）：

[olo_base_arb_prio.vhd:75-95](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_arb_prio.vhd#L75-L95) —— `Latency_g > 0` 时把 `Grant_I` 推过 `RdPipe` 流水线；`Latency_g = 0` 时直接组合输出。整段逻辑包在 `g_non_zero : if Width_g > 0 generate` 里，避免 `Width_g=0` 时出现非法位宽范围。

`ppcOr` 本身用「分stage 倍增跨度」的并行前缀网络实现，深度只有 \(\lceil \log_2 N \rceil\) 级，所以即便请求者很多也很快：

[olo_base_pkg_logic.vhd:189-221](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_logic.vhd#L189-L221) —— `Stages_c := log2ceil(inp'length)`，每个 stage 把跨度翻倍（`idx/(2**stage)`），最终每个输出位 = 它及以上所有位的或。

#### 4.1.4 代码实践

**目标**：亲手验证「最高位胜出」与 `Latency_g` 的差别。

**操作步骤**：

1. 打开 `olo_base_arb_prio_tb`，它针对 `Latency_g ∈ {0, 1, 3}` 各注册了一组用例：

   [olo_base.py:188-192](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py#L188-L192) —— 三组 `named_config` 对应三种延迟。

2. 在 `sim/` 下运行该实体的测试：

   ```bash
   cd sim
   python3 run.py --ghdl "*olo_base_arb_prio*"
   ```

3. 想观察波形，可在 `olo_base_arb_prio_tb` 里临时加一组用例：令 `In_Req = "0010100"` 持续若干拍，分别用 `Latency_g=0` 与 `Latency_g=1` 跑，导出波形。

**需要观察的现象**：

- 无论 bit2 是否请求，只要 bit4 在请求，`Out_Grant` 永远只有 bit4 为 1。
- `Latency_g=0` 时 `Out_Grant` 与 `In_Req` 同拍变化（组合）；`Latency_g=1` 时晚一拍。

**预期结果**：授权位始终等于请求向量里最高置 1 位。**待本地验证**（仿真器不同，波形导出方式略不同）。

#### 4.1.5 小练习与答案

**练习 1**：`In_Req = "0000000"` 时 `Out_Grant` 是什么？为什么？

**参考答案**：`Out_Grant = "0000000"`。`ppcOr` 全 0，台阶不存在，边沿检测后没有任何位为 1，即「无人请求则无人获批」。

**练习 2**：把 `arb_prio` 用在一个中断控制器上，紧急中断接 bit4、普通事件接 bit0。如果普通事件**持续**请求，而紧急中断**偶发**请求，普通事件会不会饥饿？

**参考答案**：不会。只要紧急中断没有请求（bit4=0），`ppcOr` 的台阶就从 bit0 开始，bit0 会被授权。饥饿只发生在「更高优先级者**持续不松手**」时；这里紧急中断是偶发的，普通事件在空闲拍照样能拿到授权。

---

### 4.2 轮询仲裁（olo_base_arb_rr）

#### 4.2.1 概念说明

固定优先级有饥饿风险，轮询（round-robin）就是为了「谁都不吃亏」：**每个请求者在一轮里只被服务一次**，服务完一圈再从头来。这样无论谁多频繁，都保证每个请求者隔一段时间就能拿到一拍授权，**不会饥饿**。

代价是：当前拍没有被选中的请求者，最坏要等这一圈其它人都轮完才能轮到自己，**最坏等待延迟与请求者个数 N 成正比**。轮询适合「请求者地位平等、需要公平共享带宽」的场景——比如多个同构的数据源分时复用一条链路。

#### 4.2.2 核心流程

Open Logic 的 `arb_rr` 用的是经典的「**双优先级仲裁器 + 一个掩码**」结构：

1. 内部实例化**两个** `arb_prio`：
   - **masked（带掩码）仲裁器**：输入是 `In_Req and Mask`，只在「本轮还没服务过」的请求者里挑最高的。
   - **unmasked（不带掩码）仲裁器**：输入是原始 `In_Req`，从所有请求者里挑最高的——作为「本轮已全部服务完，回绕重开」的兜底。
2. **选择**：如果 masked 结果非 0，就用它；否则说明本轮剩余无人请求，用 unmasked 结果（回绕到最高位）。
3. **更新掩码**：每当一次授权被下游**真正接受**（`Out_Ready='1'`），就把掩码更新成「排除刚授权位及本轮已服务位」，让下一拍去服务**下标更低**的请求者；下标走到最低后回绕到最高位。

旋转方向（经测试台 `MultiBit` 用例确认）：先服务最高位，然后依次走向**更低**位，走到最低后再回绕到最高位。例如 `In_Req` 一直为 `"10111"`（bit4,2,1,0 请求）时，授权序列为 `bit4 → bit2 → bit1 → bit0 → bit4 → …`。

`arb_rr` 带 AXI-S 风格的握手 `Out_Valid`/`Out_Ready`，但**状态（掩码）只在 `Out_Ready='1'` 时才推进**——下游没收，掩码不动，等于这一拍授权被「冻结」保留。

#### 4.2.3 源码精读

实体声明相比 `arb_prio` 多了 `Out_Ready`（入）和 `Out_Valid`（出）两个握手信号：

[olo_base_arb_rr.vhd:33-45](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_arb_rr.vhd#L33-L45) —— 注意它**没有 `Latency_g`**，输出永远是组合的（文档建议尽早在外部加一拍寄存器）。

状态用两进程法的 record 收纳，这里 record 只有一个字段：掩码 `Mask`：

[olo_base_arb_rr.vhd:50-59](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_arb_rr.vhd#L50-L59) —— `r`/`r_next` 是状态，`RequestMasked`/`GrantMasked`/`GrantUnmasked` 是与两个内部 `arb_prio` 的连线。

组合进程做三件事：算 masked 请求、在 masked/unmasked 授权间二选一、按握手更新掩码：

[olo_base_arb_rr.vhd:67-101](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_arb_rr.vhd#L67-L101) —— `RequestMasked <= In_Req and r.Mask`；当 `GrantMasked=0` 时回绕用 `GrantUnmasked`；`Out_Valid` 在授权非 0 时拉高。

掩码更新是轮询的精髓：把刚授权向量丢掉最低位、做前缀或、再在最高端补 0，得到下一拍的掩码：

[olo_base_arb_rr.vhd:85-87](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_arb_rr.vhd#L85-L87) —— 仅在「本轮有授权且下游 `Out_Ready='1'`」时更新。`'0' & ppcOr(Grant_v(high downto 1))` 的效果是：把掩码的 1 集中到「比刚授权位**更低**的位」上，从而强制下一拍往下一位走。

时序进程只做打拍与复位（同步高有效，末尾覆盖式复位，仅复位状态 `Mask`）：

[olo_base_arb_rr.vhd:104-112](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_arb_rr.vhd#L104-L112) —— 复位后 `Mask` 全 0，所以第一拍 masked 结果必为 0，自然回绕到 unmasked，从最高位开始服务。

内部实例化的两个 `arb_prio`（都设 `Latency_g=0`）：

[olo_base_arb_rr.vhd:115-137](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_arb_rr.vhd#L115-L137) —— 一个吃 `RequestMasked`，一个吃原始 `In_Req`。

#### 4.2.4 代码实践

**目标**：用测试台里的 `MultiBit` 用例，确认轮询顺序与「下游不收则冻结」两个行为。

**操作步骤**：

1. 阅读 `olo_base_arb_rr_tb` 的 `MultiBit` 用例：

   [olo_base_arb_rr_tb.vhd:121-170](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_arb_rr/olo_base_arb_rr_tb.vhd#L121-L170) —— `In_Req` 保持 `"10111"`，逐拍检查 `Out_Grant` 依次为 `10000 → 00100 → 00010 → 00001 → 10000`。

2. 运行该测试台（它只注册了一组配置，无需额外参数）：

   ```bash
   cd sim
   python3 run.py --ghdl "*olo_base_arb_rr*"
   ```

3. 再阅读 `ReadyLow` 用例：把 `Out_Ready` 拉低，观察 `Out_Grant` 是否保持不变。

**需要观察的现象**：

- 授权序列严格按 `bit4 → bit2 → bit1 → bit0 → bit4` 往复（位 3 未请求，被跳过）。
- `Out_Ready='0'` 期间，`Out_Grant` 保持上一拍值，掩码不前进。

**预期结果**：测试通过；授权序列与上面一致。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `arb_rr` 需要**两个** `arb_prio`，一个不行吗？

**参考答案**：一个 `arb_prio` 永远挑最高位，没法「记住」本轮已经服务过谁，会反复服务最高位。masked 仲裁器负责「在本轮剩余者里挑」，unmasked 仲裁器负责「本轮没人了就回绕重挑最高位」，两者配合才能实现「轮流一圈再回头」。

**练习 2**：`arb_rr` 的最坏等待延迟（某请求者从提出请求到被服务）大概是多少拍？

**参考答案**：最坏要等本轮其它 \(N-1\) 个请求者各被服务一拍，约 \(N-1\) 拍（\(N\) 为请求者个数）。这是轮询换取「无饥饿」所付出的延迟代价。

---

### 4.3 加权轮询（olo_base_arb_wrr）

#### 4.3.1 概念说明

轮询是「每人一拍」的绝对公平，但很多场景需要**按比例分配带宽**：请求者 A 业务重，应拿 4 份；B 次之，拿 2 份；C 轻，拿 1 份。加权轮询（weighted round-robin, WRR）给每个请求者配一个**权重** \(w_i\)，仲裁器让请求者 **\(w_i\) 连续拍**拿到授权后才换下一位，转一圈再回来。

长期来看，请求者 \(i\) 分得的带宽份额为

\[
\text{份额}_i = \frac{w_i}{\sum_j w_j}
\]

对 4:2:1 而言，分母 \(\sum w = 7\)，所以三者份额为 \(4/7 : 2/7 : 1/7\)。注意 WRR 是**突发式（bursty）**的——一个请求者会连续占用 \(w_i\) 拍，期间别人都得等；权重大时这一点更明显。另外，权重为 0 的请求者**永远拿不到授权**（相当于禁用），这是有意为之的资源节约。

#### 4.3.2 核心流程

`arb_wrr` 内部实例化一个 `arb_rr` 负责「换人」，自己再用一个权重计数器决定「换人时机」：

1. **零权重屏蔽**：用 `generateRequestWeightsMask` 把权重为 0 的请求者在请求向量里直接 mask 掉，`arb_rr` 永远不会挑到它们。
2. **连续授权计数**：状态里保存「当前被授权者下标 `GrantIdx`」与「已经给它的拍数 `WeightCnt`」。
3. **换人判定（switchover）**：满足下列任一条件就换人——
   - 已经给当前请求者授权达到它的权重：`WeightCnt >= Weight[GrantIdx]`；
   - 当前请求者**不再请求**了：`(Grant and In_Req) = 0`。
4. **换人执行**：拉一拍 `RrGrantReady` 给内部 `arb_rr`，让它给出下一个获胜者（轮询意义下的下一位），用 `getLeadingSetBitIndex` 把 one-hot 授权转成下标存入 `GrantIdx`，并把 `WeightCnt` 重置为 1。
5. **不换人**：否则 `WeightCnt` 每拍 `+1`，授权保持不变。

`In_Valid` 是输入侧握手：只有 `In_Valid='1'` 的拍才推进。`Latency_g` 选 0（组合，用 `r_next` 直出）或 1（寄存，用 `r`，频率更高）。

#### 4.3.3 源码精读

实体多了 `Weights`（静态权重向量）、`In_Valid`（输入握手）和 `Out_Valid`（输出握手）：

[olo_base_arb_wrr.vhd:34-56](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_arb_wrr.vhd#L34-L56) —— `GrantWidth_g` 是请求者个数，`WeightWidth_g` 是单个权重的位宽；`Weights` 是把所有权重首尾相接的扁平向量，请求者 \(i\) 的权重占据 `Weights((i+1)*WeightWidth_g-1 downto i*WeightWidth_g)`（请求者 0 在最低位）。

零权重屏蔽函数——逐位检查权重是否非 0，生成一个掩码与请求相与：

[olo_base_arb_wrr.vhd:64-81](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_arb_wrr.vhd#L64-L81) —— 权重为 0 的位，对应请求被屏蔽。该屏蔽在外部直接相与：

[olo_base_arb_wrr.vhd:103](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_arb_wrr.vhd#L103) —— `RrReq <= In_Req and generateRequestWeightsMask(...)`，喂给内部 `arb_rr`。

状态 record——除了权重计数，还保存了**锁存的权重副本 `Weights`** 与换人标志 `Switchover`：

[olo_base_arb_wrr.vhd:84-91](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_arb_wrr.vhd#L84-L91) —— `WeightCnt` 是 `unsigned`，`GrantIdx` 限定在 `0..GrantWidth_g-1`。

内部实例化 `arb_rr`（注意把 `arb_wrr` 自己的 `Out_Ready` 喂给 `arb_rr`，仅在换人那一拍为 1）：

[olo_base_arb_wrr.vhd:106-116](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_arb_wrr.vhd#L106-L116) —— 平时 `RrGrantReady='0'`，`arb_rr` 的掩码冻结，授权不变；换人拍才放行让掩码前进。

换人判定——把锁存的权重切片、取当前请求者权重做比较，同时检测「当前请求者掉线」：

[olo_base_arb_wrr.vhd:137-142](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_arb_wrr.vhd#L137-L142) —— `unflattenStlvArray` 把扁平权重切成数组（见 [olo_base_pkg_array.vhd:137-147](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_array.vhd#L137-L147)），再取 `r.GrantIdx` 那一项与 `WeightCnt` 比较。

换人执行——拉一拍 `RrGrantReady`，用 `getLeadingSetBitIndex` 把 one-hot 授权转成下标，权重计数重置为 1；不换人则计数 `+1`：

[olo_base_arb_wrr.vhd:144-158](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_arb_wrr.vhd#L144-L158) —— `getLeadingSetBitIndex` 定义于 [olo_base_pkg_logic.vhd:332-335](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_logic.vhd#L332-L335)，返回向量里最高置 1 位的下标。

输出与复位——`Latency_g` 决定取 `r` 还是 `r_next`，复位时把 `Switchover` 置 1（保证复位后第一拍就发起首次授权）：

[olo_base_arb_wrr.vhd:164-188](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_arb_wrr.vhd#L164-L188)

#### 4.3.4 代码实践（本讲主实践）

**目标**：用 `arb_wrr` 配置 3 个请求者、权重 4:2:1，持续请求下统计各通道授权次数比，验证接近 4:2:1。

> 先跑现成测试台确认环境通：`Static_AllHighReq_RandomNonZeroWeights` 用例（[olo_base_arb_wrr_tb.vhd:197-226](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/base/olo_base_arb_wrr/olo_base_arb_wrr_tb.vhd#L197-L226)）演示了「全请求 + 非零权重」下的逐拍授权序列，可作为参照。
>
> ```bash
> cd sim
> python3 run.py --ghdl "*olo_base_arb_wrr*"
> ```

**操作步骤（在仿真沙箱里新建一个最小测试台）**：

下面是**示例代码**（读者自行创建为 `my_wrr_ratio_tb.vhd`，不是项目原有文件）。它实例化 `GrantWidth_g=3`、`WeightWidth_g=3`（最大权重 4 需要 3 位）、`Latency_g=0` 的 `arb_wrr`，权重设为 requester2=4、requester1=2、requester0=1，所有请求常高，统计若干拍后三个通道的授权计数。

```vhdl
-- 示例代码：读者自行创建的最小测试台（非项目原有文件）
library ieee;
    use ieee.std_logic_1164.all;
    use ieee.numeric_std.all;
library olo;
    use olo.olo_base_pkg_math.all;

entity my_wrr_ratio_tb is end entity;

architecture sim of my_wrr_ratio_tb is
    constant ClkPeriod_c : time := 10 ns;
    signal Clk   : std_logic := '0';
    signal Rst   : std_logic := '1';
    signal W     : std_logic_vector(8 downto 0);          -- 3*3 = 9 bit
    signal Vi    : std_logic;
    signal Req   : std_logic_vector(2 downto 0);
    signal Go    : std_logic;
    signal Gnt   : std_logic_vector(2 downto 0);
    signal Cnt0, Cnt1, Cnt2 : integer := 0;
begin
    -- requester2=4("100") requester1=2("010") requester0=1("001")
    W   <= "100" & "010" & "001";   -- = "100010001"
    Req <= "111";                   -- 三路持续请求
    Vi  <= '1';
    Clk <= not Clk after ClkPeriod_c/2;

    dut : entity olo.olo_base_arb_wrr
        generic map ( GrantWidth_g => 3, WeightWidth_g => 3, Latency_g => 0 )
        port map ( Clk => Clk, Rst => Rst, Weights => W,
                   In_Valid => Vi, In_Req => Req,
                   Out_Valid => Go, Out_Grant => Gnt );

    -- 仅在 Out_Valid='1' 时计数（统计满若干轮后打印）
    process(Clk)
        variable Samples_v : integer := 0;
    begin
        if rising_edge(Clk) then
            if Rst = '1' then
                Cnt0 <= 0; Cnt1 <= 0; Cnt2 <= 0; Samples_v := 0;
            elsif Go = '1' then
                if    Gnt = "001" then Cnt0 <= Cnt0 + 1;
                elsif Gnt = "010" then Cnt1 <= Cnt1 + 1;
                elsif Gnt = "100" then Cnt2 <= Cnt2 + 1;
                end if;
                Samples_v := Samples_v + 1;
                -- 统计满 7 轮（每轮 7 次授权）后打印
                if Samples_v = 7*7 then
                    report "Cnt2:Cnt1:Cnt0 = " &
                           integer'image(Cnt2) & ":" &
                           integer'image(Cnt1) & ":" &
                           integer'image(Cnt0);
                end if;
            end if;
        end if;
    end process;

    stim : process
    begin
        wait for 1 us; Rst <= '0';
        wait for 10 us;            -- 跑足够多拍
        std.env.stop;
    end process;
end architecture;
```

**需要观察的现象**：

- 复位释放后，授权序列以「4 拍 requester2 → 2 拍 requester1 → 1 拍 requester0」为一组循环（共 7 拍一轮）。
- 打印的 `Cnt2:Cnt1:Cnt0` 接近 `28:14:7`（7 轮 × 4:2:1），即比值接近 4:2:1。

**预期结果**：三个计数比例约为 4:2:1（数值上 \(4k:2k:k\)）。由于 `Weights` 是「5 拍内生效」的静态信号、且换人判定有组合路径，前几拍可能有瞬态，统计足够多拍后比值会收敛到 4:2:1。**待本地验证**（不同仿真器打印方式略异）。

#### 4.3.5 小练习与答案

**练习 1**：把 requester1 的权重改成 0，`Req="111"` 不变。授权序列会变成什么样？

**参考答案**：requester1 被零权重 mask 掉，等效于只有 requester2(4) 和 requester0(1) 在争。序列变为「4 拍 requester2 → 1 拍 requester0」循环，requester1 永远拿不到授权。

**练习 2**：`arb_wrr` 为什么要在状态里**锁存**一份 `Weights`（`r.Weights`），而不是直接用输入端口 `Weights`？

**参考答案**：换人判定要把「当前请求者的权重」与「已授权拍数」比较，必须用**发起本轮授权那一刻**的权重值，保证一轮内阈值稳定。直接用端口 `Weights` 会在中途被改动而提前/延后换人。锁存副本让权重变化在换人边界生效（文档说明权重变化在 5 拍或一个输入样本内反映）。

---

### 4.4 公平性对比与选型

#### 4.4.1 概念说明

三种仲裁器本质是在「**响应速度**」与「**公平性**」之间做不同取舍。选型时要回答三个问题：请求者地位是否平等？是否需要按比例分配带宽？能否容忍低优先级饥饿？

- **饥饿**：某请求者长期拿不到授权。优先级仲裁在高优先级持续占用时会饥饿；轮询与加权轮询（权重>0）不会。
- **公平性**：长期带宽分配。优先级无公平可言；轮询是绝对平均（每轮每人 1 拍）；加权轮询按权重比例。
- **延迟**：优先级对最高者几乎 0 延迟；轮询最坏 \(O(N)\)；加权轮询最坏可达 \(O(\sum_{j\ne i} w_j)\)（要等别人把权重用完）。

#### 4.4.2 核心流程（选型决策）

按下表把需求映射到实体：

| 需求 | 选择 | 理由 |
| :--- | :--- | :--- |
| 有明确主次、最高者优先随到随服务 | `arb_prio` | 时序最好，1 拍（或 `Latency_g`）出结果 |
| 请求者平等、要无饥饿的公平共享 | `arb_rr` | 每轮每人一拍，带宽均分 |
| 要按比例分配带宽 | `arb_wrr` | 权重决定份额，\(w_i/\sum w_j\) |
| 高优先级偶发、低优先级可常驻 | `arb_prio` 也可 | 高优先级不持续时低优先级不饥饿 |
| 需要反压/握手 | `arb_rr`/`arb_wrr` | 二者带 `Valid/Ready`（`arb_prio` 无握手） |

注意接口差异：`arb_prio` 无握手、`Latency_g` 可任选；`arb_rr` 带 `Out_Ready/Out_Valid` 但无输出寄存、需外部加拍；`arb_wrr` 带 `In_Valid/Out_Valid`、`Latency_g` 仅 0 或 1。

#### 4.4.3 源码精读（结构复用关系）

三者是层层复用的，选型时也应意识到它们的能力包含关系：

- `arb_prio` 是地基，提供 [olo_base_arb_prio.vhd:64-72](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_arb_prio.vhd#L64-L72) 的前缀或仲裁。
- `arb_rr` 复用两个 `arb_prio`：[olo_base_arb_rr.vhd:115-137](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_arb_rr.vhd#L115-L137)。
- `arb_wrr` 复用一个 `arb_rr`：[olo_base_arb_wrr.vhd:106-116](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_arb_wrr.vhd#L106-L116)。

即能力越强，资源/延迟开销越大。能力需求最小就别上 `arb_wrr`。

#### 4.4.4 代码实践

**目标**：为本讲的三种仲裁器画一张「公平性 vs 延迟」对比图，并为一组典型需求选型。

**操作步骤**：

1. 仿照 4.3.4 的测试台，分别实例化 `arb_prio`、`arb_rr`、`arb_wrr`（请求者数相同），所有 `In_Req` 全 1 持续请求，各跑 1000 拍，统计每个请求者的授权次数。
2. 把三者的「每请求者授权次数」画成柱状图（`arb_wrr` 用均等权重 1:1:1，便于与 `arb_rr` 对比）。

**需要观察的现象**：

- `arb_prio`：只有最高位请求者拿到全部授权，其余为 0（饥饿）。
- `arb_rr`：各请求者次数几乎相等（约 1000/3）。
- `arb_wrr`（1:1:1）：与 `arb_rr` 接近；改成 4:2:1 后按比例。

**预期结果**：柱状图直观显示「优先级偏斜 → 轮询均分 → 加权按比例」。**待本地验证**。

#### 4.4.5 小练习与答案

**练习**：一个系统有 1 个「配置寄存器写口」（偶发、但写了就要立刻生效）和 4 个「数据搬运口」（常驻、要平均分带宽）。该怎么选？

**参考答案**：写口用 `arb_prio` 接最高位（偶发且需即时响应，数据口不会持续抢占写口导致其饥饿）；4 个数据口共用一个 `arb_rr`（均分带宽、无饥饿）。两者可级联：写口的授权与 `arb_rr` 的授权再进一级仲裁，或按优先级 mux。

---

## 5. 综合实践

**任务**：搭建一个「3 路数据复用器」，把三路 AXI-S 风格的请求复用到一个共享输出，并验证不同仲裁策略下的带宽分布。

**要求**：

1. 三路请求者各自常驻请求（`Req="111"`），每路用计数器记录自己被授权的次数。
2. 分别用三种仲裁器实现「选一路」逻辑：
   - 用 `arb_prio`（`Width_g=3, Latency_g=1`）；
   - 用 `arb_rr`（`Width_g=3`，`Out_Ready` 常高）；
   - 用 `arb_wrr`（`GrantWidth_g=3, WeightWidth_g=3`，权重 4:2:1）。
3. 各跑 1000 个有效拍，打印三路授权计数。
4. 对照理论值：优先级 = `1000:0:0`（最高位通吃）、轮询 ≈ `333:333:333`、加权 ≈ `571:286:143`（即 \(4/7, 2/7, 1/7\) of 1000）。

**提示**：`arb_prio` 无握手，直接每拍读 `Out_Grant`；`arb_rr` 计 `Out_Valid='1'` 且 `Out_Grant` 对应位为 1 的拍；`arb_wrr` 计 `Out_Valid='1'` 的拍。注意 `arb_wrr` 权重为静态信号、前几拍有瞬态，统计应跳过复位后最初几拍或拉长统计区间。

**预期结果**：三种实现的实际计数与理论值在小误差范围内吻合，从而亲眼看到「优先级偏斜 / 轮询均分 / 加权按比例」三种公平性。**待本地验证**。

## 6. 本讲小结

- 仲裁器每拍至多挑一个获胜者，输出必须是 one-hot；Open Logic 约定**最高位（下标最大）优先级最高**。
- `arb_prio` 用 `ppcOr` 前缀或 + 边沿检测，一拍选出最高位请求者，时序最优但**可能饥饿**；`Latency_g` 控制输出寄存级数。
- `arb_rr` 用「两个 `arb_prio` + 一个掩码」实现轮询：masked 在剩余者里挑、unmasked 兜底回绕，授权被 `Out_Ready` 接受后才推进掩码；**无饥饿**，最坏延迟 \(O(N)\)。旋转方向为「最高位起，逐次走低，再回绕」。
- `arb_wrr` 在 `arb_rr` 之上加权重计数器，每个请求者连续拿 \(w_i\) 拍才换人，长期带宽份额为 \(w_i/\sum w_j\)；权重 0 的请求者被 mask 禁用。
- 选型口诀：**主次分明→prio；均分公平→rr；按比例→wrr**。三者层层复用，能力越强资源/延迟越大。
- 接口差异要记牢：`arb_prio` 无握手；`arb_rr` 带 `Out_Ready/Out_Valid`、无输出寄存；`arb_wrr` 带 `In_Valid/Out_Valid`、`Latency_g` 仅 0 或 1、`Weights` 为 5 拍内生效的静态信号。

## 7. 下一步学习建议

- **继续 base 区**：本讲是 u5 单元的一篇。下一讲 u5-l3 讲 **CRC 引擎与包校验**（`olo_base_crc` / `crc_append` / `crc_check`），其中 `crc_check` 同样要「按规则丢弃」，与本讲「按规则选人」思路相通，可对比阅读。
- **配合时序实体**：若想把仲裁器接进真实数据流，先读 u5-l1 的 `olo_base_rate_limit`（限速）与 `olo_base_pl_stage`（u2-l2，寄存一拍），它们常与仲裁器串联使用。
- **进阶验证**：u10-l1 会系统讲 VUnit 测试台与验证组件（VC）。本讲实践中用到的 `axi_stream_master`/`axi_stream_slave` VC（见 `arb_wrr` 测试台）正是那篇的主题，学完后可把本讲的「计数统计」改写成 VC 自动比对的形式。
- **源码延伸阅读**：想加深对前缀或网络的理解，精读 `olo_base_pkg_logic.vhd` 里 `ppcOr` 的 stage 循环（[L189-221](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_logic.vhd#L189-L221)）；想理解 one-hot 转下标，看 `getLeadingSetBitIndex`/`getSetBitIndex`（[L332-335](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_logic.vhd#L332-L335)）。
