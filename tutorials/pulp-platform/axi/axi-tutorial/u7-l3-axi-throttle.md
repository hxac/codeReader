# axi_throttle：限流

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚「总线限流（throttling）」要解决的问题：为什么要在 master 与下游之间限制**同时在途（outstanding）的事务数**，它保护的是谁；
- 复述 `axi_throttle` 的**双层阈值**设计——编译期参数 `MaxNumAwPending`/`MaxNumArPending` 决定计数器位宽与硬上限，运行期输入 `w_credit_i`/`r_credit_i` 决定当下实际允许的在途数；
- 读懂模块只例化两个外部 `stream_throttle` 原语、**只门控 AW/AR 的 valid/ready、其余通道整体透传**的极简结构，并解释为什么写计数看 B、读计数看 `R.last`；
- 把 `axi_throttle` 与本单元前两讲的 `axi_fifo`（u7-l1）、`axi_isolate`（u7-l2）放在一张「流控职责」表里对比，知道何时该用限流、何时该用缓冲、何时该用隔离；
- 动手搭一个 `rand_master → axi_throttle → axi_sim_mem` 的最小测试台，把在途上限设为 4，并用一个监听进程验证「任一时刻在途事务确实被约束在阈值内」。

## 2. 前置知识

本讲是 U7「流控与缓冲」的第三篇，承接 u7-l1（缓冲）与 u7-l2（隔离）。进入正文前，请确认你已理解以下概念（前序讲义已建立，这里只做最小回顾）：

- **在途事务（in flight / outstanding）**：地址拍已握手、响应拍尚未握手的事务。写事务在 AW 握手后「在途」，直到 B 握手结束；读事务在 AR 握手后在途，直到**最后一拍** R（`r.last`）握手结束（见 u1-l3、u2-l3）。
- **valid/ready 铁律**：`valid` 一旦拉高，在握手（`valid && ready` 同周期）完成前**不允许撤下**，载荷须稳定。限流器在「想挡住新事务」时不能简单清零 valid，而要靠 credit 计数从源头压住（见 u2-l3、u7-l2）。
- **接口外壳 + 结构体内核**范式：本库很多模块有成对的 `xxx`（结构体内核，吃 `axi_req_t`/`axi_rsp_t`）与 `xxx_intf`（接口外壳，吃 `AXI_BUS`）。`axi_throttle` **只有结构体内核、没有 `_intf` 外壳**，用时要自己用 `AXI_TYPEDEF_*`/`AXI_ASSIGN_*` 宏搭「接口三明治」（见 u2-l4）。
- **rand_master / axi_sim_mem / axi_scoreboard** 标准自检拓扑：随机主端发激励、`axi_sim_mem` 当无限忠实从端、`axi_scoreboard` 旁路监听做黄金模型比对。这是本讲综合实践要复用的骨架（见 u3-l2、u3-l3）。
- **stream_throttle / cf_math_pkg**：`axi_throttle` 不自己写计数逻辑，而是例化外部 `common_cells` 的 `stream_throttle` 原语，并用 `cf_math_pkg::idx_width` 推导计数器位宽。`Bender.yml` 把 `common_cells` 钉在 `1.39.0`（[Bender.yml:L24](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Bender.yml#L24)）。这两个依赖的源码不在本仓库工作树内，本讲只依据它们在 `axi_throttle.sv` 里的接线与文档化的行为契约来讲解，不臆造其内部实现。

如果对「为什么要限流」还没有直觉，记住一句话：**下游的「接待能力」是有限的——它只能同时招呼 N 桌客人。`axi_throttle` 就是门口的发号机，保证同时进门的客人数不超过下游能接住的上限。**

## 3. 本讲源码地图

本讲只涉及一个源文件（它没有专属测试台，综合实践里我们会自己搭一个）：

| 文件 | 作用 | 本讲角色 |
|------|------|----------|
| [src/axi_throttle.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_throttle.sv) | 单模块 `axi_throttle`：对一条 AXI4+ATOP 总线分别限制读/写在途事务数 | 精读主体 |

文件内部布局速览（行号便于跳读）：

- **L8–L11**：模块行为契约注释（双层阈值、运行期可调、有序/同 ID 假设）。
- **L12–L28**：参数表——编译期上限 `MaxNumAwPending`/`MaxNumArPending` + 四个「请勿覆盖」的派生位宽/类型。
- **L29–L47**：端口表——一对 slave 侧 `req_i`/`rsp_o`、一对 master 侧 `req_o`/`rsp_i`，外加运行期 credit 输入。
- **L50–L86**：两个 `stream_throttle` 实例（AW 方向、AR 方向各一）。
- **L88–L100**：两个 `always_comb`——把 AX 通道的 valid/ready 换成节流后的版本，其余信号整体透传。

> 模块在 `Bender.yml` / `src_files.yml` 中都位于 **Level 2**（[Bender.yml:L75](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Bender.yml#L75)），即只依赖 Level 0–1 的根基（`axi_pkg`、`axi_intf` 等）与外部 `common_cells`，是一个高度独立的小积木。

## 4. 核心概念与源码讲解

本讲把 `axi_throttle` 拆成四个最小模块来讲：先建立「信用（credit）限流」的直觉，再拆开双层阈值参数，接着精读「只门 AX、透视 B/W/R」的实现，最后把它和 `axi_fifo`/`axi_isolate` 摆在一起做选型对比。

### 4.1 限流的需求与信用（credit）机制

#### 4.1.1 概念说明

AXI 是一个**允许多事务并发**的协议：一个 master 可以连续发出多笔 AW/AR，而不必等上一笔的 B/R 回来再发下一笔。这种「在途并发」是 AXI 高带宽的来源。但下游并不总能承受任意大的在途并发：

- **窄 FIFO 下游**：下游入口只有一个深度为 4 的命令 FIFO，它最多只能「记住」4 笔未完成的事务。若 master 一次性塞进 10 笔，多余的会撑爆 FIFO 或被硬性反压到死。
- **外部存储控制器 / DDR**：这类下游的「命令队列」容量有限，过载会引发优先级倒置或不可预期的延迟。
- **服务质量（QoS）**：想给某条链路配一个「最多同时 8 笔」的额度，防止它独占下游带宽。

直接靠下游自己反压（ready 拉低）虽然最终也能挡住，但有两个问题：一是反压信号要逐级传回 master，链路长时延迟大、易抖动；二是有些下游（如协议桥）在过载时不是优雅反压而是直接出错。因此更稳妥的做法是**在靠近 master 的地方主动设一道闸门**，让「发往下游的在途事务数」永远不超过下游能接住的上限。这就是 `axi_throttle` 的职责。

`axi_throttle` 采用**信用（credit）**思路：把「允许同时在途的事务数」看作一种可消耗的信用额度——

- 每向下游**发走**一笔新事务（AW/AR 被下游接受），消耗 1 个信用；
- 每从下游**收回**一笔响应（写收到 B、读收到 `R.last`），归还 1 个信用；
- 信用耗尽时，新事务的 valid 被**压住**（不让 AW/AR 往下游走），直到有响应回来归还信用。

这与 `axi_isolate`（u7-l2）的「排空计数」同源，但目的完全不同：isolate 数在途是为了**安全断开**，throttle 数在途是为了**持续地把并发卡在一个上限**。

#### 4.1.2 核心流程

设 `N_out(t)` 为时刻 t 的在途事务数，`C(t)` 为当前信用额度（运行期可调）。节流规则可写成：

\[
N_{\text{out}}(t) = \#\text{AX 已被下游接受} - \#\text{响应已收回}
\]

\[
\text{放行新 AX} \iff N_{\text{out}}(t) < C(t) \le C_{\max}
\]

其中 `C_max` 是编译期硬上限（`MaxNumAwPending`/`MaxNumArPending`），`C(t)` 是运行期输入 `w_credit_i`/`r_credit_i`。写、读两个方向各自独立计数、互不影响：

```text
写方向：  AW 被下游接受  --> N_out_w +1     (消耗 1 个写信用)
          B  响应被收回    --> N_out_w -1     (归还 1 个写信用)

读方向：  AR 被下游接受  --> N_out_r +1     (消耗 1 个读信用)
          R.last 响应收回 --> N_out_r -1     (归还 1 个读信用)

任意周期：若 N_out == C(t)，则把对应 AX 的 valid 压住（不向下游发新事务）
```

注意「归还」的触发点：写方向是 **B 握手**（一个写事务对应一个 B），读方向是 **`R.last` 握手**（一个读事务对应一串 R、只有最后一拍才算「这笔读完成了」）。这正是后面 4.3 会看到的接线的依据。

#### 4.1.3 源码精读

模块开头那段英文注释是全模块的行为契约，先读它：

[src/axi_throttle.sv:L8-L11](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_throttle.sv#L8-L11) —— 逐句中文转述：

1. 「Throttles an AXI4+ATOP bus」：对一条 AXI4+ATOP 总线做节流。
2. 「The maximum number of outstanding transfers have to be set as a compile-time parameter」：**最大**允许在途数是**编译期**参数（`MaxNumAwPending`/`MaxNumArPending`，决定计数器位宽与硬天花板）。
3. 「whereas the number of outstanding transfers can be set during runtime」：而**当下**允许的在途数可在**运行期**通过 `w_credit_i`/`r_credit_i` 设定。
4. 「This module assumes either in-order processing of the requests or indistinguishability of the request/responses (all ARs and AWs have the same ID respectively)」：模块做的是**纯聚合计数**，没有按 ID 或按事务建跟踪表，因此要求下游要么**按序处理**、要么**请求/响应不可区分**（所有 AR 同 ID、所有 AW 同 ID）。这是「用一个标量计数就能正确刻画在途数」的前提，4.3 会展开。

`README.md` 的模块表对它的官方一句话描述也印证了职责：

[README.md:L68](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/README.md#L68) —— 「Limits the maximum number of outstanding transfers sent to the downstream logic.」（限制发往下游的最大在途事务数。）

#### 4.1.4 代码实践（源码阅读型）

**目标**：在打开仿真器前，先吃透「信用消耗/归还」的触发事件。

**步骤**：

1. 打开 [src/axi_throttle.sv:L8-L11](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_throttle.sv#L8-L11)，只读这段注释。
2. 在纸上回答三个问题：
   - 如果一笔**写突发**（AW + 多拍 W + 一个 B）穿过本模块，写方向的 `N_out_w` 何时 +1？何时 −1？净变化是多少？
   - 如果一笔**读突发**（AR + 多拍 R，`r.last` 收尾），读方向的 `N_out_r` 何时 +1？何时 −1？为什么是 `r.last` 而不是 R 的第一拍？
   - 假设 `w_credit_i` 恒为 1，master 连发两笔单拍写，第二笔会发生什么？

**需要观察的现象 / 预期结果**：

- 写：AW 被下游接受时 `N_out_w` +1，B 收回时 −1，一笔写事务净变化为 0（先借后还）。
- 读：AR 被下游接受时 +1，**`R.last`** 收回时 −1——因为只有最后一拍回来才算「这笔读真正结束、下游不再为它占资源」。用第一拍会少算在途、误判容量。
- `w_credit_i=1` 时，第一笔写消耗掉唯一信用；在它的 B 回来之前，第二笔写的 AW valid 会被压住，**等 B 到、信用归还后**才能放行。具体周期级时序「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：信用机制与「靠下游 ready 反压」相比，为什么更适合保护「容量有限的下游 FIFO」？

**参考答案**：下游 FIFO 满了才拉低 ready，是把反压点放在了最末端；反压要逐级传回 master，链路长时延迟大、且 FIFO 可能在反压传回前已被新的在途事务继续填入。信用机制把闸门设在**靠近 master** 处，按下游的**真实容量**预先发号，从源头保证「在途数 ≤ FIFO 深度」，下游 FIFO 永远不会被撑到溢出边缘。

**练习 2**：如果一个读突发的 `len=7`（共 8 拍 R），`N_out_r` 在这 8 拍期间会怎样变化？

**参考答案**：在 AR 被下游接受时 +1（变成 1），随后 R 的前 7 拍（`r.last=0`）**都不触发**归还，`N_out_r` 保持 1；直到第 8 拍（`r.last=1`）握手才 −1 归零。也就是说，整个突发期间这笔读一直占用 1 个读信用，与「它占用下游一个读通道槽位」的现实一致。

---

### 4.2 编译期上限与运行期阈值：双层参数

#### 4.2.1 概念说明

`axi_throttle` 最有特色的设计是**把「在途上限」拆成两层**：

- **编译期硬上限** `MaxNumAwPending` / `MaxNumArPending`（默认均为 1）：决定内部 credit 计数器的**位宽**，也决定了无论运行期怎么设都不可能超过的绝对天花板。这是综合时就固定的硬件资源。
- **运行期实际阈值** `w_credit_i` / `r_credit_i`：模块运行时由外部喂入，表示「此刻允许同时在途的事务数」。它可以在运行中被动态调整（比如根据下游负载自适应），但取值受编译期上限约束——计数器位宽只够表示到 `MaxNumAwPending`。

这种「编译期定资源、运行期调策略」的拆分很常见：硬件面积按最坏情况（编译期上限）预留，而实际工作点可以更保守（运行期阈值），既不浪费面积、又保留运行期灵活性。

此外要特别注意：`axi_throttle` **没有接口外壳**（不像 `axi_isolate` 有 `axi_isolate_intf`）。它的端口直接是 `axi_req_t`/`axi_rsp_t` 结构体，所以要把它接进一个用 `AXI_BUS` 接口搭的系统，得自己用 `AXI_TYPEDEF_*` 声明 req/resp 类型、用 `AXI_ASSIGN_*` 做「接口三明治」（见 u2-l4）。后面的综合实践会给出完整写法。

#### 4.2.2 核心流程

计数器位宽由 `cf_math_pkg::idx_width` 从编译期上限推导：

\[
\texttt{WCntWidth} = \texttt{cf\_math\_pkg::idx\_width}(\texttt{MaxNumAwPending})
\]

`idx_width` 是 `common_cells`（`cf_math_pkg`）里一个基于 `$clog2` 的位宽推导函数，为给定的最大计数值算出「恰好够用」的计数器位宽。由此派生出 `w_credit_t = logic [WCntWidth-1:0]`，即 `w_credit_i` 的类型。读写两侧完全对称。源码在这四个派生参数后都标了 `(*DO NOT OVERWRITE*)`——它们是**结果**而非输入，用户例化时不应手填，让默认表达式自动算即可。

双层阈值的约束关系：

```text
0 <= w_credit_i <= MaxNumAwPending     (运行期阈值不能超过编译期上限)
0 <= r_credit_i <= MaxNumArPending

效果：任一时刻在途写 <= w_credit_i <= MaxNumAwPending
      任一时刻在途读 <= r_credit_i <= MaxNumArPending
```

#### 4.2.3 源码精读

参数表集中在前部：

[src/axi_throttle.sv:L13-L16](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_throttle.sv#L13-L16) —— 编译期上限：`MaxNumAwPending`（最大允许在途写数，默认 1）、`MaxNumArPending`（最大允许在途读数，默认 1）。注释明确「maximum amount of allowable outstanding write/read requests」。

[src/axi_throttle.sv:L17-L20](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_throttle.sv#L17-L20) —— 标准的 `axi_req_t`/`axi_rsp_t` 类型参数（默认 `logic`，由调用方用 `AXI_TYPEDEF_*` 生成的具体类型覆盖，见 u2-l4）。

[src/axi_throttle.sv:L21-L28](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_throttle.sv#L21-L28) —— 四个**派生**参数，全部标 `(*DO NOT OVERWRITE*)`：`WCntWidth`/`RCntWidth` 由 `cf_math_pkg::idx_width(...)` 推导，`w_credit_t`/`r_credit_t` 再由位宽派生。它们不是给用户填的，而是让计数器位宽自动匹配编译期上限。

端口表里，credit 是普通输入端口（运行期可变）：

[src/axi_throttle.sv:L44-L47](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_throttle.sv#L44-L47) —— `w_credit_i`（写信用额度=允许的在途写数）、`r_credit_i`（读信用额度），类型是上面派生的 `w_credit_t`/`r_credit_t`。注释直接写明「number of outstanding write/read transfers」，确认它们就是运行期阈值。

[src/axi_throttle.sv:L35-L42](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_throttle.sv#L35-L42) —— 数据端口：slave 侧 `req_i`/`rsp_o`（上游 master 发来的请求、回给上游的响应），master 侧 `req_o`/`rsp_i`（发往下游的请求、下游回的响应）。注意它**没有** `isolate_i`/`isolated_o` 这类握手信号——限流是常态行为，不像隔离那样是一次性事件。

#### 4.2.4 代码实践（配置 + 接线阅读型）

**目标**：理解「设限为 4」时编译期与运行期分别要做什么，并看清因为没有 `_intf` 外壳而需要的接线。

**步骤**：

1. 假设要把写方向限到「最多 4 笔在途」。回答：
   - 编译期应把 `MaxNumAwPending` 设成几？（提示：它得 ≥ 你想要的运行期上限）
   - 运行期把 `w_credit_i` 驱动成几？
   - 如果运行期突然想让上限临时降到 2，需要重新综合吗？
2. 阅读 [src/axi_throttle.sv:L29-L48](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_throttle.sv#L29-L48)，确认端口全是结构体（`axi_req_t`/`axi_rsp_t`），没有 `AXI_BUS` 接口。
3. 下面是把 `axi_throttle` 接进 `AXI_BUS` 系统的标准「三明治」骨架（**示例代码**，非仓库原有，仿照 u2-l4 / u7-l2 的 `axi_isolate_intf` 写法）：

   ```systemverilog
   // 1. 用宏声明本设计的 req/resp 结构体类型
   `AXI_TYPEDEF_ALL_CT(req_t, resp_t, ...按你的位宽...)

   // 2. 上游用带时钟的 DV 接口驱动，DUT 侧用 AXI_BUS，中间用宏搬运
   AXI_BUS        up_bus();   // 接 rand_master
   AXI_BUS        dn_bus();   // 接下游 sim_mem
   req_t          throttle_req_i, throttle_req_o;
   resp_t         throttle_rsp_i, throttle_rsp_o;

   `AXI_ASSIGN_TO_REQ(throttle_req_i,  up_bus)   // 接口 -> 结构体（上游侧）
   `AXI_ASSIGN_FROM_RESP(up_bus,       throttle_rsp_o)
   `AXI_ASSIGN_FROM_REQ(dn_bus,        throttle_req_o)  // 结构体 -> 接口（下游侧）
   `AXI_ASSIGN_TO_RESP(throttle_rsp_i, dn_bus)

   axi_throttle #(
     .MaxNumAwPending ( 4      ),   // 编译期上限 = 4
     .MaxNumArPending ( 4      ),
     .axi_req_t       ( req_t  ),
     .axi_rsp_t       ( resp_t )
   ) i_throttle (
     .clk_i, .rst_ni,
     .req_i(throttle_req_i), .rsp_o(throttle_rsp_o),   // 上游侧
     .req_o(throttle_req_o), .rsp_i(throttle_rsp_i),   // 下游侧
     .w_credit_i( 4'd4 ),    // 运行期阈值 = 4（运行中可改为 1..4）
     .r_credit_i( 4'd4 )
   );
   ```

**需要观察的现象 / 预期结果**：

- 设限 4：编译期 `MaxNumAwPending=4`，运行期 `w_credit_i=4`。运行期降到 2 只需改 `w_credit_i` 的驱动值，**无需重新综合**——这正是双层拆分的意义。
- credit 端口位宽 `WCntWidth` 由 `idx_width(4)` 自动算出（具体位宽「待本地验证」），所以 `w_credit_i` 的字面量位宽要匹配该类型；上面示例里写 `4'd4` 仅为示意，实际应以 `w_credit_t'(4)` 或与派生位宽一致的字面量为准。
- 宏的具体名字/参数以 `include/axi/typedef.svh`、`include/axi/assign.svh` 为准（见 u2-l4），示例中的 `AXI_TYPEDEF_ALL_CT` 是库内常用的「一次声明全套类型」宏。

#### 4.2.5 小练习与答案

**练习 1**：为什么把 `MaxNumAwPending` 设得比实际需要的运行期上限大很多是不划算的？

**参考答案**：`MaxNumAwPending` 决定 credit 计数器的位宽（经 `idx_width` 派生），设得越大，计数器寄存器越宽、比较器越大，综合后面积与功耗都更高。运行期阈值再小，也省不回编译期多预留的那部分硬件。所以应让编译期上限尽量贴近「最坏情况下真的需要」的值，运行期阈值在这个天花板下灵活调。

**练习 2**：`w_credit_i` 与 `MaxNumAwPending` 谁是 `parameter`、谁是 `input` 端口？这意味着二者的可变性有何根本区别？

**参考答案**：`MaxNumAwPending` 是 `parameter int unsigned`（编译期/参数化，综合后固定，不能运行中改）；`w_credit_i` 是 `input w_credit_t` 端口（运行期信号，每个周期都可变）。前者定硬件资源与绝对天花板，后者定当下工作点。想让限流阈值在运行中动态变化（如自适应 QoS），就只能动 `w_credit_i`。

---

### 4.3 双 stream_throttle：只门 AX、透视 B/W/R

#### 4.3.1 概念说明

`axi_throttle` 的实现极简：它**不自己写任何计数/状态机逻辑**，而是例化两个外部原语 `stream_throttle`（来自 `common_cells`），分别管写方向（AW/B）和读方向（AR/R），然后把其余信号整体透传。理解它的关键是搞清「哪些信号被门控、哪些被观察、哪些纯透传」：

- **门控（gate）**：只有 AW、AR 两个**地址**通道的 `valid`/`ready` 被节流器接管——「想发新事务」的请求只有信用够时才放行。
- **观察（observe）**：B 通道（写响应）和 R 通道（读响应）的握手信号被节流器**观察但不修改**，用来归还信用——写收到 B 归还 1 个写信用，读收到 `R.last` 归还 1 个读信用。
- **透传（pass-through）**：所有通道的**载荷**（addr、data、strb、id、atop、resp……）、W 通道的 valid/ready、B/R 的 valid/ready，全部原样从 `req_i`/`rsp_i` 拷到 `req_o`/`rsp_o`，节流器对它们视而不见。

为什么 W 通道不需要单独门控？因为 AXI 协议里，**W 数据总是跟在某个已被下游接受的 AW 后面**。节流器已经在 AW 这道闸门上限制了「在途写事务数」，下游既然接受了 AW，就承诺了会接住对应的 W——所以只要 AW 被限住，W 自然不会过载，无需二次门控。同理 B/R 是响应，数量由发出去的 AW/AR 决定，限住了请求就限住了响应。

#### 4.3.2 核心流程

两个 `stream_throttle` 实例的接线完全对称，以写方向为例：

```text
                 上游 master                          下游 slave
   req_i.aw_valid ----->+------------------+-----> req_o.aw_valid (节流后)
                       | stream_throttle  |        (下游 aw_ready 决定何时接受)
   (下游) aw_ready ---->|  (AW 请求 / B 响应)|-----> (回上游) aw_ready
                       |  credit = w_credit|
   (下游) b_valid ----->|                  |
   (上游) b_ready ----->+------------------+

   规则：AW 放行后写信用 -1；B 握手时写信用 +1；写信用==0 时压住新 AW 的 valid
```

读方向把 AW/B 换成 AR/R，且「归还信用」的事件是 `r_valid && r.last && r_ready`（最后一拍）。两个实例各自维护一个独立的 credit 计数器，互不干扰。

**关于头部那段「假设」（有序 / 同 ID）的直觉**：节流器只做**纯标量计数**——它数「发出去几个 AX、收回来几个响应」，完全不区分这些事务的 ID、地址、身份。这种纯计数在两种前提下能正确刻画「在途事务数」：要么下游**按序**处理请求/响应（先发的先回，标量计数天然对应队列长度），要么**所有请求/响应不可区分**（所有 AR 同 ID、所有 AW 同 ID，具体哪笔对应哪个响应无所谓，只关心总数）。若你的流量是「多 ID 乱序」且下游对 ID 有特殊依赖，纯标量计数就不一定满足你的语义预期——这是使用本模块要遵守的契约（见 [src/axi_throttle.sv:L10-L11](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_throttle.sv#L10-L11)）。

> **边界提示（待本地验证）**：模块宣称支持 AXI4+ATOP。注意原子写（AW 带 `ATOP_R_RESP`）除了产生 B，还会产生若干拍 R 响应，而这些 R 会被**读方向**的 `stream_throttle` 观察（因为它接的是 `r_valid & r.last`）。若你的设计中大量使用 ATOP，建议在仿真中专门验证：读信用的会计是否仍与实际在途相符，是否会因 ATOP 的 R 响应而出现偏差。本讲不臆测 `stream_throttle` 内部是否对此做饱和处理，具体行为以仿真为准。

#### 4.3.3 源码精读

内部节流后的 valid/ready 信号声明：

[src/axi_throttle.sv:L50-L56](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_throttle.sv#L50-L56) —— `throttled_aw_valid/ar_valid`（节流后、即将送往下游的地址 valid）与 `throttled_aw_ready/ar_ready`（节流后、回给上游的地址 ready）。注意只有 AX 通道有节流版本，B/W/R 没有。

写方向实例：

[src/axi_throttle.sv:L58-L71](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_throttle.sv#L58-L71) —— `i_stream_throttle_aw`，参数 `MaxNumPending = MaxNumAwPending`。接线要点：
- `req_valid_i ← req_i.aw_valid`（上游想发 AW）、`req_valid_o → throttled_aw_valid`（节流后送往下游）；
- `req_ready_i ← rsp_i.aw_ready`（下游的 aw_ready）、`req_ready_o → throttled_aw_ready`（回给上游）；
- `rsp_valid_i ← rsp_i.b_valid`、`rsp_ready_i ← req_i.b_ready`——**用 B 通道握手作为「响应归还」事件**；
- `credit_i ← w_credit_i`（运行期写信用阈值）。

读方向实例：

[src/axi_throttle.sv:L73-L86](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_throttle.sv#L73-L86) —— `i_stream_throttle_ar`，结构完全镜像写方向，唯一不同在响应事件：`rsp_valid_i ← rsp_i.r_valid & rsp_i.r.last`（**只有最后一拍 R 才算「这笔读完成」**），`rsp_ready_i ← req_i.r_ready`。注释 L74「limit Ar requests -> wait for r.last」一句话点明了这点。

最后两个 `always_comb` 把节流结果缝回总线：

[src/axi_throttle.sv:L88-L93](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_throttle.sv#L88-L93) —— `gen_throttled_req_conn`：`req_o = req_i`（整体拷贝，含 W、B/R 的 ready、所有载荷），然后**只覆盖** `aw_valid`/`ar_valid` 为节流版本。注释 L88「a through connection - except for the ax valids」精确说明了这一点。

[src/axi_throttle.sv:L95-L100](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_throttle.sv#L95-L100) —— `gen_throttled_rsp_conn`：`rsp_o = rsp_i`（整体拷贝，含 W/B/R 的 valid 与载荷），然后**只覆盖** `aw_ready`/`ar_ready` 为节流版本。两个 `always_comb` 合起来，就是「除 AX 的 valid/ready 外，全部透传」。

#### 4.3.4 代码实践（源码阅读型）

**目标**：在不看 `stream_throttle` 内部的前提下，仅凭 `axi_throttle.sv` 的接线，验证你对「门控 AX、透视 B/R、透传其余」的理解。

**步骤**：

1. 阅读 [src/axi_throttle.sv:L58-L86](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_throttle.sv#L58-L86) 两个实例。
2. 回答：
   - W 通道的 `w_valid`/`w_ready`/`w_payload` 有没有被接进任何一个 `stream_throttle`？它们怎么从上游到下游？（提示：看 L88-L100 的两个 `always_comb`）
   - 写信用在哪个事件下被「归还」？读信用呢？请各举出对应的端口连线。
   - 假设写信用已耗尽，上游 master 拉高了 `aw_valid`。此时 `req_o.aw_valid`（送往下游）和 `rsp_o.aw_ready`（回给上游）分别会是什么状态？
3. 把 [src/axi_throttle.sv:L88-L100](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_throttle.sv#L88-L100) 的两个 `always_comb` 与 [src/axi_join.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_join.sv)（纯连线，见 u4-l1）对比：`axi_throttle` 在「不节流」（信用充足）时，行为是不是退化成接近 `axi_join` 的直连？

**需要观察的现象 / 预期结果**：

- W 通道**没有**进入 `stream_throttle`；它靠 `req_o = req_i` / `rsp_o = rsp_i` 整体拷贝实现透传。
- 写信用在 **B 握手**（`rsp_i.b_valid && req_i.b_ready`）时归还；读信用在 **`R.last` 握手**（`rsp_i.r_valid && rsp_i.r.last && req_i.r_ready`）时归还。
- 写信用耗尽且上游拉高 `aw_valid` 时：`req_o.aw_valid` 被 `stream_throttle` 压住（不送往下游，或送往下游但被门控为不下发新事务），`rsp_o.aw_ready` 也被压住（不向上游fake ready）。关键是不违反 valid 铁律——具体是压 valid 还是压 ready 由 `stream_throttle` 决定，波形「待本地验证」。
- 信用充足时，`axi_throttle` 对总线几乎透明（仅多一拍组合/寄存器延迟，取决于 `stream_throttle` 实现），接近 `axi_join` 行为。

#### 4.3.5 小练习与答案

**练习 1**：读方向的「归还事件」为什么必须用 `r.last`，而不能像写方向用 B 那样直接用一个通道信号？

**参考答案**：写事务的响应只有一个 B（一拍即完），所以 B 握手 = 这笔写完成。读事务的响应是一**串** R 拍（`len+1` 拍），只有最后一拍 `r.last` 才代表「这笔读的全部数据都回来了、下游为它占的资源可以释放」。如果用 R 的第一拍或任意一拍归还，会把一笔多拍读误当成「很快归还」，从而允许超过实际容量的在途读数。

**练习 2**：模块头部注释要求「有序或同 ID」假设。如果下游是一个会**按 ID 乱序返回响应**的器件，本模块的「标量计数」会出错吗？

**参考答案**：标量计数的**总数**不会错——发出去 N 个 AX、收回 M 个响应，在途数仍是 `N−M`，与顺序无关。问题在于「在途数 ≤ credit」这个**数值约束**对你的语义是否仍有意义。乱序场景下，某个 ID 的事务可能迟迟不返回，占着信用不放，导致别的 ID 事务被 credit 卡住（误冲突式停顿）；这并非计数错误，而是纯计数没有按 ID 区分能力带来的副作用。所以注释把「同 ID 或有序」列为前提：在这两种流量下，纯标量计数与「真实在途」语义最贴合。

---

### 4.4 与 axi_fifo / axi_isolate 的职责边界与选型

#### 4.4.1 概念说明

U7 的三个模块——`axi_fifo`（u7-l1）、`axi_isolate`（u7-l2）、`axi_throttle`（本讲）——都「碰」在途事务，但解决的问题完全不同，选型时极易混淆。用「餐厅」的比喻一次说清：

| 模块 | 餐厅比喻 | 解决的问题 | 核心机制 | 典型用途 |
|------|----------|------------|----------|----------|
| `axi_fifo` | 在门口加一个**候位厅** | 吸收突发、切断组合路径、平滑抖动 | 每通道一个 FIFO + `spill_register` 缓冲 | 长总线时序修复、跨背压吸收突发 |
| `axi_isolate` | **打烊前清场**：让在座客人吃完、不再接新客、然后锁门 | 安全断开总线（掉电/复位前优雅排空） | `isolate_i`/`isolated_o` + 排空状态机 | 电源/复位门控前的安全隔离 |
| `axi_throttle` | 门口**发号机**：同时进门的客人数 ≤ N | 限制同时在途事务数，保护容量有限的下游 | credit 计数，门控 AW/AR valid | 保护窄 FIFO/外部控制器、QoS 限速 |

三者可以共存：一条链路可以「`throttle` 限并发 → `fifo` 吸收突发 → `isolate` 在末端做安全断开」，各司其职。

#### 4.4.2 核心流程

从**对外接口**就能一眼区分三者的职责（这是选型时最快的判据）：

```text
axi_fifo:    depth 参数（每通道缓冲深度）            —— 没有「控制」信号，常态缓冲
axi_isolate: isolate_i / isolated_o 握手            —— 一次性「请求-完成」事件
axi_throttle:w_credit_i / r_credit_i 运行期阈值     —— 持续生效的并发额度
```

- 看到 **depth / FIFO 深度** → 想缓冲：用 `axi_fifo`。
- 看到 **isolate 请求/完成握手** → 想安全断开：用 `axi_isolate`。
- 看到 **credit / 在途上限** → 想限流：用 `axi_throttle`。

#### 4.4.3 源码精读

三者的接口差异直接体现在端口表里：

- `axi_throttle`：数据端口 + `w_credit_i`/`r_credit_i`（[src/axi_throttle.sv:L44-L47](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_throttle.sv#L44-L47)），**无隔离握手、无 depth**——它是常态限流器。
- `axi_isolate`：数据端口 + `isolate_i`/`isolated_o`（见 u7-l2，[src/axi_isolate.sv:L60-L77](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv#L60-L77)），**无 credit、无 depth**——它是一次性清场开关。
- `axi_fifo`：数据端口 + 每通道深度参数（[src/axi_fifo.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_fifo.sv)，见 u7-l1），**无控制信号**——它是无源缓冲。

> 三者还有一个有趣的共性：它们都**不改写地址/数据载荷**，只动握手或加缓冲。换言之，对数据通路而言它们都是「透明」的——这正是「组合优于配置」哲学的体现：每块积木只做一件小事，靠背靠背串联完成复杂流控。

#### 4.4.4 代码实践（选型练习）

**目标**：给定三个真实场景，选对模块。

**场景与步骤**：阅读上面的对比表，为每个场景选一个（或一组）模块，并说明理由：

1. 一个 master 通过 5cm 长走线连到下游，综合报关键路径违例，时序紧张。
2. 下游是一个外部 DDR 控制器，其命令队列只能容纳 8 笔未完成事务，超过会乱序丢命令。
3. 某子域即将掉电，必须在掉电前确保通往它的总线上没有任何半截事务。
4. （综合）同一个 master，既要保护 DDR 的 8 笔上限，又要在掉电前安全隔离它。

**需要观察的现象 / 预期结果**：

1. 时序违例 → `axi_fifo`（用 `spill_register`/FIFO 切组合路径）。
2. DDR 命令队列容量 → `axi_throttle`（`MaxNumAwPending`/`MaxNumArPending=8`，`w_credit_i`/`r_credit_i=8`）。
3. 掉电安全 → `axi_isolate`（用 `isolate_i`/`isolated_o` 做优雅排空）。
4. 综合：`master → axi_throttle(8) → axi_isolate → DDR/子域`。throttle 常态限并发保护 DDR；isolate 在掉电前发起清场。两者串联无冲突，因为 throttle 是常态、isolate 是事件。具体顺序与时序「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：把 `axi_throttle` 当作 `axi_isolate` 来用（即想靠 throttle 实现「安全排空后断开」）行不行？缺什么？

**参考答案**：不行。throttle 只能把在途数**压到 credit 设的值**，但它没有「排空到 0 并报告完成」的机制——它没有 `isolated_o` 这样的完成信号，也不会主动把在途数降到 0（除非把 credit 设为 0，但仍无法报告「此刻真的为 0」）。要安全断开，必须用 isolate 的排空状态机 + `isolated_o`。throttle 是「持续限流」，isolate 是「一次性清场」，职责不可互换。

**练习 2**：`axi_fifo` 能不能代替 `axi_throttle` 保护「下游 FIFO 深度 = 4」？

**参考答案**：不能等价代替。`axi_fifo` 是**缓冲**——它吸收突发、把抖动暂存起来，但本身不限制「同时在途事务数」；master 仍可以往下游塞超过 4 笔在途事务，fifo 只是让自己的缓冲跟着涨。要严格保证「发往下游的在途 ≤ 4」，必须用 throttle 的 credit 机制从源头卡住。fifo 管「吸收」，throttle 管「限额」，保护容量有限的下游是 throttle 的本职。

---

## 5. 综合实践

把本讲的内容串起来，完成规格里要求的核心任务：**在 `rand_master` 与下游之间插入 `axi_throttle` 设限为 4，观察任一时刻在途事务是否被约束在阈值内**。由于仓库没有提供 `tb_axi_throttle`，本任务需要你基于 u3-l2/u3-l3 与 u7-l2 的测试台骨架自建一个最小 TB。

**场景与拓扑**：

```text
   axi_rand_master ---> AXI_ASSIGN ---> axi_throttle ---> AXI_ASSIGN ---> axi_sim_mem
                                          (设限 4)                              |
   axi_scoreboard <--- AXI_ASSIGN_MONITOR (挂在 master 侧) ---------------------+
```

**操作步骤**：

1. **搭拓扑**（仿照 `test/tb_axi_isolate.sv` 的时序三件套 `CyclTime=10ns / ApplTime=2ns / TestTime=8ns` 与「DV 接口 + AXI_BUS + 三明治」结构，见 u3-l3、u7-l2）：
   - 例化 `axi_rand_master`（设较大的 `MaxReadTxns`/`MaxWriteTxns`，比如 16，让它**有意愿**并发超过 4 笔）；
   - 按 4.2.4 的骨架例化 `axi_throttle`，`MaxNumAwPending=MaxNumArPending=4`，`w_credit_i=r_credit_i=4`；
   - 例化 `axi_sim_mem` 当无限忠实从端；例化 `axi_scoreboard` 挂在 master 侧做黄金模型自检（用 `AXI_ASSIGN_MONITOR`）。

2. **写一个监听进程**验证在途上限（**示例代码**，放在 TB 里，用 `assert property` 持续检查）：

   ```systemverilog
   // 在 throttle 的下游侧（req_o / rsp_i）数在途事务
   // 写方向：每个 AW 握手 +1，每个 B 握手 -1
   // 读方向：每个 AR 握手 +1，每个 R.last 握手 -1
   int outstanding_w = 0, outstanding_r = 0;

   // 用 assert property 在每个时钟沿检查（伪代码，需按你的时钟块书写）
   // outstanding_w <= 4  且  outstanding_r <= 4
   ```

   关键观测点：tap `req_o.aw_valid && rsp_i.aw_ready`（下游接受的 AW）与 `rsp_i.b_valid && req_o.b_ready`（收回的 B）；读方向同理用 `req_o.ar_valid && rsp_i.ar_ready` 与 `rsp_i.r_valid && rsp_i.r.last && req_o.r_ready`。也可以用层次化引用直接窥探 DUT 内部（如 `tb.i_throttle.i_stream_throttle_aw.*`），但该路径名取决于 `common_cells` 版本，优先推荐自建监听进程。

3. **跑仿真**：用 `scripts/run_vsim.sh` 的方式编译运行（编译命令参考 `scripts/compile_vsim.sh` 的 `bender script vsim -t test -t rtl`，见 u1-l4），用 `-gTB_N_TXNS` 之类参数加大事务量，并换几个随机种子（`+sv_seed=...`）跑回归。

**需要观察的现象 / 预期结果**：

- `axi_scoreboard` 全程不报 mismatch（功能正确性自检通过）。
- 你的监听进程显示 `outstanding_w` 与 `outstanding_r` **任一时刻都不超过 4**——即使 master 有能力并发 16 笔，throttle 也会把在途数卡在 4。
- 对照实验：把 `w_credit_i`/`r_credit_i` 在运行中从 4 改成 2，应观察到在途上限**立刻**收紧到 2（无需重新综合），证明运行期阈值可动态调节。
- 对照实验：把 credit 调大到超过 `MaxNumAwPending`（编译期上限）应无意义/被位宽截断——验证双层阈值的约束。

> 本任务需自建 TB（仓库未提供 `tb_axi_throttle`），具体的监听进程写法、层次化信号名、周期级波形「待本地验证」。监听进程的核心思想（数 AX 握手减去响应握手 = 在途数）与 `axi_isolate` 的 pending 计数器（u7-l2）、`axi_scoreboard` 的字节栈维护（u3-l2）同源，可互相参照。

## 6. 本讲小结

- `axi_throttle` 是一个**限流器**：通过限制发往下游的同时在途事务数，保护容量有限的下游（窄 FIFO、外部存储控制器、QoS），靠「credit 信用」机制从源头压住新事务。
- 它采用**双层阈值**：编译期 `MaxNumAwPending`/`MaxNumArPending` 决定计数器位宽与硬上限（综合后固定），运行期 `w_credit_i`/`r_credit_i` 决定当下实际允许的在途数（可动态调）。
- 实现极简：例化两个外部 `stream_throttle` 原语（来自 `common_cells`），分别管写方向（信用在 B 握手归还）和读方向（信用在 `R.last` 握手归还）；**只门控 AW/AR 的 valid/ready，W/B/R 与所有载荷整体透传**。
- 头部契约要求流量「有序或同 ID」：因为模块做纯标量计数、不建 ID 跟踪表；ATOP 大量使用时读信用会计需在仿真中专门验证。
- 它与 `axi_fifo`（缓冲/切路径）、`axi_isolate`（安全排空断开）职责分明：看接口判模块——有 depth 用 fifo、有 isolate 握手用 isolate、有 credit 用 throttle；三者可串联共存。
- 它**没有 `_intf` 外壳**，端口是 `axi_req_t`/`axi_rsp_t`，用时要自己用 `AXI_TYPEDEF_*`/`AXI_ASSIGN_*` 搭接口三明治；仓库未提供专属测试台，验证需自建（可复用 `tb_axi_isolate` 的骨架）。

## 7. 下一步学习建议

- **横向收口 U7**：回到 u7-l1 的 `axi_fifo` 与 u7-l2 的 `axi_isolate`，结合本讲画一张「流控三件套」总表，确保你能对任意场景在三者间（及它们的串联组合）正确选型——这是 U7 的终极目标。
- **进入时钟域跨越**：限流器常被部署在时钟域/电源域边界，下一篇 u8-l1 的 `axi_cdc` 正好处理跨时钟域。思考「`throttle` 与 `cdc` 谁该靠近 master、谁该靠近域边界」——这与 u7-l2 末尾提出的 isolate/cdc 顺序问题是同一类设计取舍。
- **追溯 credit 的更复杂用法**：本讲的 credit 只限「在途数」。若想看「按 ID 跟踪在途」的更重型机制，可预习 u10-l1 的 `axi_id_remap`（维护 ID 映射表）与 u5-l2 的 `axi_demux_id_counters`（每 ID 计数阵列），对比它们与本讲「纯标量计数」的面积/能力差异。
- **协议深化**：本讲提到的 ATOP（`ATOP_R_RESP`）将在 u15-l1 全面展开；届时你会更清楚原子操作对在途计数、ID 唯一性的影响，也能更准确地判断 throttle 在 ATOP 流量下的会计行为。
