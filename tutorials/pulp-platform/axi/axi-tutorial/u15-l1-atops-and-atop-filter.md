# ATOPs 原子操作与 axi_atop_filter

## 1. 本讲目标

本讲聚焦 AXI5 的原子操作（ATOPs，Atomic Operations）以及本库用来「消化」它的模块 `axi_atop_filter`。学完本讲你应当能够：

- 解读 `aw_atop` 这 6 个比特的编码：哪两位决定操作大类、哪一位决定是否产生读响应、低三位又是哪个运算符。
- 说清楚为什么「带 `ATOP_R_RESP` 的原子读改写」会让一个不支持 ATOP 的普通从端死锁，从而理解 `axi_atop_filter` 孅在的必要性。
- 跟踪 `axi_atop_filter` 内部的两个有限状态机：它如何把原子写的 AW/W「吃掉」，再凭空「注入」一个 `RESP_SLVERR` 的 B 响应（必要时再注入一串 R 响应），让上游主端体面地收到一个错误而不是无限挂起。
- 看懂 `axi_atop_filter` 在系统中的部署位置——它永远插在「会发 ATOP 的主端」与「不懂 ATOP 的从端」之间。

本讲属于专家层（advanced），前置知识是 u1-l3（AXI4 协议回顾）与 u6-l1（`axi_xbar` 架构）。我们会频繁引用 `axi_pkg` 中的常量与 `axi_atop_filter` 的真实源码。

## 2. 前置知识

在进入源码前，先用通俗语言建立两个直觉。

**原子操作要解决什么问题。** 假设 CPU 想把内存里某个计数器加 1。最朴素的做法是「读—改—写」三步：先发一次读拿到旧值，在 CPU 里加 1，再发一次写回新值。问题是在「读」和「写」之间，别的主端可能也插进来读了同一个地址，于是两个主端都基于同一个旧值加 1、写回，最终计数器只加了 1 而不是 2——这就是经典的「丢失更新」。软件层面通常用锁来回避，但锁慢且容易死锁。AXI5 的 ATOPs 把这件事下沉到总线：主端在**一次写事务**里告诉从端「请你把发去的数据和地址里的旧值做某个运算（比如加），结果存回地址，并把旧值返回给我」。因为整个过程是单笔事务、由从端原子完成，别的.master 无法插入，丢失更新自然消失。常见的 ATOP 操作有 `ATOP_ATOMICSWAP`（交换）、`ATOP_ATOMICCMP`（比较并交换，即 CAS）、`ATOP_ADD`（原子加）等。

**为什么 ATOP 会把普通从端搞坏。** 关键在于：一类原子操作（`ATOP_ATOMICLOAD`、SWAP、CMP）不仅要像普通写那样在 **B 通道**回一个写响应，还要在 **R 通道**回若干拍读数据（把旧值/原值返回给主端）。也就是说，**一笔原子写会同时产生 B 响应和 R 响应**。可一个不懂 ATOP 的普通从端收到这笔 AW 时，会把它当成普通写——只回一个 B，**完全不回 R**。而主端这一侧正掐着表等 R 拍数到齐……于是主端永远等不到，总线死锁。这就是 `axi_atop_filter` 要堵的漏洞：它把原子写拦下，自己冒充从端给主端回一个「错误」的 B（必要时再回一串「错误」的 R），让主端收到一个确定的 `SLVERR` 而不是无限等待。

> 术语提示：本讲反复出现 `ATOP_R_RESP`，它是 `axi_pkg` 里定义的一个**位下标常量**（值为 5），用来索引 `aw_atop[5]` 这一位。这一位为 1 就代表「该原子操作需要在 R 通道返回数据」。务必和「读事务的 AR/R 通道」区分开——ATOP 没有 AR，它的读响应是由 AW 触发的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/axi_pkg.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv) | 全库共享的 `package`，定义 `atop_t` 类型与全部 `ATOP_*` 常量，是 ATOP 编码的「字典」。 |
| [src/axi_atop_filter.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_atop_filter.sv) | 本讲主角。结构体内核 + 接口外壳 `axi_atop_filter_intf`，含 W/R 两个 FSM 与一个在途写计数器。 |
| [test/tb_axi_atop_filter.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_atop_filter.sv) | 随机验证测试台。上游用会发 ATOP 的 `axi_rand_master`，下游用不懂 ATOP 的 `axi_rand_slave`，并用一个完整的参考模型逐拍比对。 |
| [README.md](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/README.md) | 「Atomic Operations」一节给出了 AXI4+ATOPs 的工程契约与系统设计者的责任。 |

## 4. 核心概念与源码讲解

### 4.1 ATOP 原子操作的编码与语义

#### 4.1.1 概念说明

AXI4 本身没有原子操作，ATOPs 是 AXI5（AMBA 5 规范 E1.1 节）追加的特性。本库把它叫做「AXI4+ATOPs」——即在完整 AXI4 之上叠加一组原子操作。ATOPs 只挂在**写地址通道 AW** 上（读地址 AR 没有 atop 字段），靠一个 6 位的 `aw_atop` 字段编码。当 `aw_atop == 0` 时，这就是一笔普普通通的写；非零时则是一笔原子写，从端必须按 ATOP 语义处理。

这 6 个比特被划分成三个字段，理解了字段划分就理解了全部 ATOP 语义。

#### 4.1.2 核心流程：6 位 atop_t 的三段编码

`aw_atop[5:0]` 分三段，由高位到低位：

| 字段 | 含义 | 取值 |
| --- | --- | --- |
| `[5:4]` | **操作大类**（2 位） | `00`=NONE（非原子）、`01`=ATOMICSTORE、`10`=ATOMICLOAD、`11`=SWAP/CMP |
| `[3]` | **端序**（仅算术运算有效） | `0`=小端、`1`=大端 |
| `[2:0]` | **运算符**（仅 STORE/LOAD 有效） | ADD/CLR/EOR/SET/SMAX/SMIN/UMAX/UMIN |

最关键的是**最高位 `[5]`（即 `ATOP_R_RESP`）**：它为 1 表示这笔原子写除了产生 B 响应，**还要在 R 通道产生至少一拍读响应**。把大类按 `[5:4]` 展开：

- `ATOP_NONE = 2'b00`：`[5]=0`，普通写，无 R 响应。
- `ATOP_ATOMICSTORE = 2'b01`：`[5]=0`，原子存——从端做运算并存结果，**只回 B，不回 R**。
- `ATOP_ATOMICLOAD = 2'b10`：`[5]=1`，原子载——从端做运算、存结果、**并回 R 把原值返回**。
- SWAP / CMP（`[5:4]=2'b11`）：`[5]=1`，交换 / 比较并交换，**回 B 也回 R**。

于是有一条极其重要的判别规则：

\[ \text{需要 R 响应} \iff aw\_atop[5]=1 \iff aw\_atop[5{:}4] \in \{2'b10,\ 2'b11\} \]

这正是 `axi_atop_filter` 决定「要不要顺带注入 R 响应」的依据。SWAP 和 CMP 是两个写满 6 位的「整体」常量（`6'b110000` 与 `6'b110001`），它们的 `[5:4]` 恰好是 `11`，因此天然落入「需要 R 响应」一类。

#### 4.1.3 源码精读

先看类型与位宽。`atop_t` 是 6 位逻辑向量，宽度常量 `AtopWidth = 6`：

- [src/axi_pkg.sv:43-43](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L43-L43) 定义 `AtopWidth = 6`。
- [src/axi_pkg.sv:64-64](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L64-L64) 定义 `typedef logic [5:0] atop_t;`。

再看编码常量。源码用注释把每个字段的作用写得非常清楚，分四组：

- [src/axi_pkg.sv:387-397](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L387-L397) 定义整体 6 位的 SWAP（`6'b110000`）与 CMP（`6'b110001`）。注意它们的最高两位都是 `11`。
- [src/axi_pkg.sv:400-415](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L400-L415) 定义 `[5:4]` 大类：`ATOP_NONE`、`ATOP_ATOMICSTORE`、`ATOP_ATOMICLOAD`。
- [src/axi_pkg.sv:421-423](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L421-L423) 定义 `[3]` 端序：`ATOP_LITTLE_END`、`ATOP_BIG_END`。
- [src/axi_pkg.sv:426-444](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L426-L444) 定义 `[2:0]` 运算符：`ATOP_ADD/CLR/EOR/SET/SMAX/SMIN/UMAX/UMIN`。

最后是本讲最关键的一个常量——**位下标** `ATOP_R_RESP`：

```systemverilog
// ATOP[5] == 1'b1 indicated that an atomic transaction has a read response
// Ussage eg: if (req_i.aw.atop[axi_pkg::ATOP_R_RESP]) begin
localparam ATOP_R_RESP = 32'd5;
```

它来自 [src/axi_pkg.sv:445-447](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L445-L447)。注意它**不是位掩码，而是位下标**（值为 5），所以用法是 `aw.atop[axi_pkg::ATOP_R_RESP]` 而不是 `aw.atop & ATOP_R_RESP`。注释里给出的用法示例正是 `axi_atop_filter` 里实际的写法。

#### 4.1.4 代码实践

**目标**：亲手用 `axi_pkg` 的常量拼出几种 ATOP 编码，验证你对字段划分的理解。

1. 操作步骤：打开 [src/axi_pkg.sv:380-447](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L380-L447)，对照常量定义。
2. 在纸上（或一段 SystemVerilog 注释里）回答：一个「大端、原子加、需要返回旧值」的原子操作，其 `aw_atop` 的 6 个比特分别是什么？写出推导：
   - 需要返回旧值 → `ATOMICLOAD` → `[5:4]=2'b10`；
   - 大端 → `[3]=1'b1`；
   - 加法 → `[2:0]=ATOP_ADD=3'b000`；
   - 拼起来 = `6'b10_1_000` = `6'b101000`。
3. 再验证：`ATOP_ATOMICSWAP = 6'b110000`，它的 `[5]` 是 1 吗？`[5:4]` 是不是 `11`？是否落进「需要 R 响应」一类？
4. 预期结果：你应能仅凭字段定义推导出任意 ATOP 编码，并判断它是否带 R 响应。这一步**待本地验证**（无需运行仿真，纯字段演算）。

#### 4.1.5 小练习与答案

**练习 1**：`ATOP_ATOMICSTORE`（`[5:4]=01`）会不会在 R 通道产生响应？为什么？
**答案**：不会。`ATOP_ATOMICSTORE` 的最高位 `[5]=0`，即 `aw_atop[ATOP_R_RESP]` 为 0，只产生 B 响应；从端做运算并存结果，不返回旧值。

**练习 2**：为什么 ATOP 没有挂在 AR（读地址）通道上？
**答案**：因为 ATOP 本质是「写驱动」的：主端通过 AW 下发操作数与运算符，从端据此修改地址处的值。需要返回旧值时，读数据走 R 通道，但触发它的仍是 AW 而非 AR，所以 AR 不需要 atop 字段。

**练习 3**：AXI 标准要求「ATOP 不得与任何其他在途事务共用同一个 ID」。结合 `axi_atop_filter` 注入响应的过程，想想这条约束为 filter 带来了什么便利？
**答案**：因为该 ID 在途唯一，filter 注入 B/R 响应时无需考虑与同 ID 其他事务的排序问题，可以直接、立即注入（源码注释明确指出这一点）。详见 4.3.3。

### 4.2 axi_atop_filter 的部署位置与两大协议保证

#### 4.2.1 概念说明

真实 SoC 里，主端和从端的 ATOP 能力常常不匹配：新出的 CPU 核会发 ATOP，而某个 legacy 外设或简易 SRAM 控制器根本不懂。如果让原子写直达这种从端，就会发生 4.1 里说的死锁——带 `ATOP_R_RESP` 的原子写等不到 R 拍。

`axi_atop_filter` 就是这个不匹配的「隔离层」。它是一个**对称的双端口模块**：slave 端收主端来的事务，master 端发给下游从端。它的职责不是「支持」ATOP，而是「消化」ATOP——把原子写拦截下来，对上游伪装成一个会回 SLVERR 的从端，对下游则保证「你永远看不到 atop」。这样系统设计者就可以放心地把不懂 ATOP 的从端接在它后面。

README 把这条工程契约写得很明确：

> 系统设计者必须保证：(1) 只要可能收到 ATOP 的、不支持 ATOP 的从端，前面都要加一个 `axi_atop_filter`；(2) 本仓库内任何（非 AXI4-Lite）模块的输入端，`aw_atop` 信号都必须是良好定义的。

#### 4.2.2 核心流程：两条保证

模块顶部注释把功能收成两条硬保证：

1. **master 端口的 `aw_atop` 永远为 0**——所有原子写都被拦在 filter 内部，绝不透传到下游。
2. **slave 端口上非零 `aw_atop` 的写事务，会按 AXI 标准被完整响应**——filter 自己回 B（必要时再回 R），响应码恒为 `RESP_SLVERR`。注释专门说明：用 SLVERR 是本库的实现选择，AXI 标准并未规定「被过滤」该回什么码。

由此得到部署准则：**filter 永远插在「会发 ATOP 的主端」一侧、紧贴在「不懂 ATOP 的从端」之前**。在 `axi_xbar` 这类互联里，如果某个下游从端不支持 ATOP，就要在它前面挂一个 filter。

#### 4.2.3 源码精读

模块文档头把上述两条保证白纸黑字写出来：

- [src/axi_atop_filter.sv:15-31](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_atop_filter.sv#L15-L31) 给出功能概述、两条保证与「预期部署位置」（Intended usage）。

模块的端口与参数如下（结构体内核版本）：

```systemverilog
module axi_atop_filter #(
  parameter int unsigned AxiIdWidth = 0,
  parameter int unsigned AxiMaxWriteTxns = 0,
  parameter type axi_req_t  = logic,
  parameter type axi_resp_t = logic
) (
  input  logic      clk_i,
  input  logic      rst_ni,
  input  axi_req_t  slv_req_i,
  output axi_resp_t slv_resp_o,
  output axi_req_t  mst_req_o,
  input  axi_resp_t mst_resp_i
);
```

- [src/axi_atop_filter.sv:37-59](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_atop_filter.sv#L37-L59)：端口定义。

两个参数值得记住：

- `AxiIdWidth`：AXI ID 宽度，filter 需要它来缓存被拦原子写的 ID，以便回 B/R 时填回去。断言要求 ≥ 1。
- `AxiMaxWriteTxns`：filter 能同时「消化」的、在途（已收 AW 尚未回 B）的原子写上限，同时也用于跟踪下游普通写在途数。断言要求 ≥ 1。这个参数决定了内部计数器位宽，也是测试台重点扫描的参数（取 1、3、12）。

模块还遵循 u2-l4 建立的「接口外壳 + 结构体内核」范式：[src/axi_atop_filter.sv:379-448](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_atop_filter.sv#L379-L448) 的 `axi_atop_filter_intf` 用扁平位宽参数和 `AXI_TYPEDEF_*`/`AXI_ASSIGN_*` 宏包出一份接口版，测试台直接例化的就是它。

#### 4.2.4 代码实践

**目标**：在源码里定位「两大保证」各自的实现点，建立「注释 ↔ 代码」的对应。

1. 操作步骤：打开 `src/axi_atop_filter.sv`。
2. 找「保证 1（master 端 atop 恒为 0）」的代码：定位到 [src/axi_atop_filter.sv:249-254](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_atop_filter.sv#L249-L254)，你会看到 `mst_req_o.aw = slv_req_i.aw; mst_req_o.aw.atop = '0;`——即 AW 其余字段照抄，**唯独把 atop 强制清零**再发给下游。
3. 找「保证 2（回 SLVERR）」的代码：定位到 B 响应注入 [src/axi_atop_filter.sv:220-223](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_atop_filter.sv#L220-L223) 与 R 响应注入 [src/axi_atop_filter.sv:288-290](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_atop_filter.sv#L288-L290)，两处都把 `resp` 设成 `axi_pkg::RESP_SLVERR`。
4. 需要观察的现象：两段代码正好是模块头注释两条保证的落地，一一对应。
5. 预期结果：能复述「保证 1 落在 AW 透传的 always_comb 块、保证 2 落在 INJECT_B / INJECT_R 两个状态」。**待本地验证**（纯源码阅读）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 filter 选择回 `RESP_SLVERR` 而不是 `RESP_DECERR`？
**答案**：模块注释明说这是「实现选择，AXI 标准未定义」。语义上 SLVERR 表示「事务到达了从端但出错」，比 DECERR（「无法路由到从端」）更贴切——事务确实到了 filter 这个「假从端」，只是它不支持该原子操作。

**练习 2**：filter 的 AR 通道需要做任何过滤吗？
**答案**：不需要。ATOP 只挂在 AW 上，AR 永远不会携带原子信息。源码里 AR 是无条件直通的（见 4.3.3 的 AR 三连 assign）。

**练习 3**：如果一个系统里**所有**从端都支持 ATOP，还需要 `axi_atop_filter` 吗？
**答案**：不需要。filter 只为「混合能力」系统而存在。全支持时直接让 ATOP 透传即可，README 的契约也只要求「不支持 ATOP 的从端前加 filter」。

### 4.3 过滤状态机：吸收请求与注入错误响应

#### 4.3.1 概念说明

保证「master 端看不到 atop、slave 端收到完整响应」看似简单，实则要处理 AXI 的并发握手细节：AW 和 W 是两个独立通道，原子写的 W 拍可能和别的普通写的 W 拍交错到达；B/R 响应也要在合适的时机注入，既不能丢拍也不能违反「valid 一旦拉高在握手前不可撤」的铁律。

为此 `axi_atop_filter` 用了**两个有限状态机**：

- **W 侧 FSM**（管理 AW/W/B 三通道）：识别原子写、吸收它的 AW 与 W 拍、择机注入 B 响应。
- **R 侧 FSM**（管理 R 通道）：在收到 W 侧「需要注入 R」的命令后，注入对应拍数的 R 响应。

两者之间用一个 1 深寄存器（`stream_register`）传递「R 响应命令」（其实就是把被拦原子写的 `len` 带过去，告诉 R 侧要注几拍）。

此外还有一个**在途写计数器 `w_cnt`**：它跟踪「已转发到下游、但 W 突发还没收尾」的普通写数量。它的作用是让 filter 在「拦一笔原子写」的同时，仍能让之前未完成的普通写的 W 拍顺利流向下游，并在合适时机门控新的 AW，避免下游 AW/W 配对混乱。

#### 4.3.2 核心流程

**W 侧 FSM** 有 7 个状态，主线流程是：

```
W_RESET → W_FEEDTHROUGH ──(检测到原子 AW)──┐
                                            │
   ┌────────────────────────────────────────┘
   │ 若下游还有未完成的普通写 W 突发 → BLOCK_AW（先让它们走完）
   │ 否则 → 直接开始吸收本原子写的 W 拍
   ▼
 ABSORB_W（逐拍吸收 W，直到 w.last）→ INJECT_B（注入一拍 SLVERR 的 B）
   │                                          ▲
   │ 若注入 B 时上游正好有一个未握手的 B ──────┘ HOLD_B（先让那个 B 走完）
   ▼
 注入完 B 后，若 R 侧还没注完所有 R 拍 → WAIT_R
 否则 → 回到 W_FEEDTHROUGH
```

**R 侧 FSM** 有 4 个状态，主线是 `R_RESET → R_FEEDTHROUGH → INJECT_R → R_FEEDTHROUGH`，外加一个 `R_HOLD` 处理「下游 R valid 但上游暂时不 ready」的背压。

判别逻辑的两条命脉：

1. **是不是原子写**：`slv_req_i.aw.atop[5:4] != axi_pkg::ATOP_NONE`（任何非零大类都算）。
2. **要不要顺带注入 R**：`slv_req_i.aw.atop[axi_pkg::ATOP_R_RESP]`（即 bit 5）。

**在途写计数器** 用一个带 `underflow` 标志的饱和计数器实现，详见 4.3.3。

#### 4.3.3 源码精读

**(a) 状态定义与计数器类型**

W 侧与 R 侧的状态枚举：

- [src/axi_atop_filter.sv:69-71](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_atop_filter.sv#L69-L71) 定义 `w_state_e`：`W_RESET, W_FEEDTHROUGH, BLOCK_AW, ABSORB_W, HOLD_B, INJECT_B, WAIT_R`。
- [src/axi_atop_filter.sv:74-74](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_atop_filter.sv#L74-L74) 定义 `r_state_e`：`R_RESET, R_FEEDTHROUGH, INJECT_R, R_HOLD`。

计数器特意做到「最小宽度 2」以便检测下溢：

```systemverilog
// Minimum counter width is 2 to detect underflows.
localparam int unsigned COUNTER_WIDTH = (AxiMaxWriteTxns == 1) ? 2 : $clog2(AxiMaxWriteTxns+1);
typedef struct packed {
  logic                     underflow;
  logic [COUNTER_WIDTH-1:0] cnt;
} cnt_t;
```

来自 [src/axi_atop_filter.sv:61-66](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_atop_filter.sv#L61-L66)。

**(b) 在途写计数器：`w_cnt` 与两个派生信号**

计数器只在「真正转发到下游」时增减——`mst_req_o.aw_valid && mst_resp_i.aw_ready` 时 +1，`mst_req_o.w_valid && mst_resp_i.w_ready && mst_req_o.w.last` 时 -1。被拦的原子写既不 +1（AW 没透传）也不 -1（W 没透传），互不干扰：

- [src/axi_atop_filter.sv:317-330](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_atop_filter.sv#L317-L330)：增减与下溢判定逻辑。

由它派生出两个布尔信号，状态机据此决策：

```systemverilog
// An AW without a complete W burst is in-flight downstream if the W counter is > 0 and not underflowed.
assign aw_without_complete_w_downstream = !w_cnt_q.underflow && (w_cnt_q.cnt > 0);
// A complete W burst without AW is in-flight downstream if the W counter is -1.
assign complete_w_without_aw_downstream = w_cnt_q.underflow && &(w_cnt_q.cnt);
```

来自 [src/axi_atop_filter.sv:94-97](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_atop_filter.sv#L94-L97)。`aw_without_complete_w_downstream` 为真表示「下游还有没收尾 W 突发的普通写」——这时即便要拦原子写，也得先让那些 W 拍走完（进入 `BLOCK_AW`）。`complete_w_without_aw_downstream`（下溢态）则用来门控新 AW，防止下游 AW/W 配对错乱。

**(c) W_FEEDTHROUGH：识别并拦截原子写**

这是状态机的心脏。在透传默认值之上，它做三件事——拦 AW、吸收 W、择机注入 B：

```systemverilog
// Filter out AWs that are atomic operations.
if (slv_req_i.aw_valid && slv_req_i.aw.atop[5:4] != axi_pkg::ATOP_NONE) begin
  mst_req_o.aw_valid  = 1'b0; // Do not let AW pass to master port.
  slv_resp_o.aw_ready = 1'b1; // Absorb AW on slave port.
  id_d = slv_req_i.aw.id; // Store ID for B response.
  // Some atomic operations require a response on the R channel.
  if (slv_req_i.aw.atop[axi_pkg::ATOP_R_RESP]) begin
    r_resp_cmd_push_valid = 1'b1;   // 通知 R 侧 FSM 注入 R 响应
  end
  ...
```

来自 [src/axi_atop_filter.sv:138-170](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_atop_filter.sv#L138-L170)。注意三个动作：

1. `mst_req_o.aw_valid = 0`：AW 不透传（保证 1）。
2. `slv_resp_o.aw_ready = 1`：在上游把 AW「吃掉」，让主端以为从端收下了。
3. `id_d = slv_req_i.aw.id`：把 ID 存进 `id_q`，稍后注入 B/R 时原样填回。
4. 若 `aw.atop[ATOP_R_RESP]` 为真，向 R 侧命令寄存器 push 一条「需要注入 R」的命令。

后续分支决定下一状态：若下游还有未完成普通写（`aw_without_complete_w_downstream`）→ `BLOCK_AW`（[src/axi_atop_filter.sv:173-193](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_atop_filter.sv#L173-L193)，先放行那些 W 拍）；否则进入吸收模式，逐拍吃 W 直到 `w.last`，然后跳到 `INJECT_B`（中途若撞上未握手 B 则先去 `HOLD_B` / `ABSORB_W` 等待，见 [src/axi_atop_filter.sv:195-212](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_atop_filter.sv#L195-L212)）。

**(d) INJECT_B / WAIT_R：注入 B 响应并等 R 收尾**

```systemverilog
INJECT_B: begin
  mst_req_o.b_ready = 1'b0;          // 暂停下游 B 的转发
  slv_resp_o.b = '0;
  slv_resp_o.b.id = id_q;            // 用缓存的 ID
  slv_resp_o.b.resp = axi_pkg::RESP_SLVERR;
  slv_resp_o.b_valid = 1'b1;
  if (slv_req_i.b_ready) begin
    if (r_resp_cmd_pop_valid && !r_resp_cmd_pop_ready) begin
      w_state_d = WAIT_R;            // R 侧还没注完，去等
    end else begin
      w_state_d = W_FEEDTHROUGH;
    end
  end
end
```

来自 [src/axi_atop_filter.sv:214-233](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_atop_filter.sv#L214-L233)。注释点出关键：「B 通道有 ID，而原子写在途唯一，所以无需排序、可立即注入」。`WAIT_R`（[src/axi_atop_filter.sv:235-241](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_atop_filter.sv#L235-L241)）负责等到 R 侧把所有 R 拍注完（`r_resp_cmd_pop_valid` 变 0）再回 `W_FEEDTHROUGH`，避免下一笔事务抢在 R 注入完成前插队。

**(e) R 侧 FSM：INJECT_R 注入读响应**

```systemverilog
INJECT_R: begin
  mst_req_o.r_ready  = 1'b0;
  slv_resp_o.r       = '0;
  slv_resp_o.r.id    = id_q;
  slv_resp_o.r.resp  = axi_pkg::RESP_SLVERR;
  slv_resp_o.r.last  = (r_beats_q == '0);
  slv_resp_o.r_valid = 1'b1;
  if (slv_req_i.r_ready) begin
    if (slv_resp_o.r.last) begin
      r_resp_cmd_pop_ready = 1'b1;   // 最后一拍，弹出命令
      r_state_d = R_FEEDTHROUGH;
    end else begin
      r_beats_d -= 1;                // 还没注完，倒计数
    end
  end
end
```

来自 [src/axi_atop_filter.sv:285-300](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_atop_filter.sv#L285-L300)。`r_beats_q` 初始化自命令里的 `len`（[src/axi_atop_filter.sv:280-281](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_atop_filter.sv#L280-L281)），每注一拍减 1，到 0 时拉 `r.last`。于是主端收到的是「ID 正确、resp 全 SLVERR、拍数与 len+1 一致」的一串合法 R 响应——体面的失败。

**(f) W/R 之间的命令寄存器与 AR 直通**

W 侧把「要注 R」连同 `len` push 进一个 1 深 `stream_register`，R 侧 pop 出来用：

- [src/axi_atop_filter.sv:348-362](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_atop_filter.sv#L348-L362)：`stream_register` 实例，数据就是 `r_resp_cmd_t`（只含 `len`）。push 端不需要看 ready，因为 W 侧会一直停在 `WAIT_R` 直到它被排空（注释见 [src/axi_atop_filter.sv:144-148](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_atop_filter.sv#L144-L148)）。

AR 通道完全无条件直通，因为 ATOP 与 AR 无关：

```systemverilog
assign mst_req_o.ar        = slv_req_i.ar;
assign mst_req_o.ar_valid  = slv_req_i.ar_valid;
assign slv_resp_o.ar_ready = mst_resp_i.ar_ready;
```

来自 [src/axi_atop_filter.sv:312-314](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_atop_filter.sv#L312-L314)。

**(g) 参数合法性断言**

- [src/axi_atop_filter.sv:366-370](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_atop_filter.sv#L366-L370)：`AxiIdWidth >= 1` 且 `AxiMaxWriteTxns >= 1`，否则 `$fatal`。

#### 4.3.4 代码实践

**目标**：跑通自带测试台，亲眼看 filter 把随机生成的原子写（含 `ATOP_R_RESP` 类）「过滤」成 SLVERR；再通过源码阅读理解「不接 filter 会怎样」。

1. **实践目标**：验证「下游永远看不到 `aw_atop`」+「上游的原子写收到 SLVERR 的 B 与 R」。
2. **操作步骤**：
   - 编译：`make compile.log`（会调用 `compile_vsim.sh`，按 Level 0–6 排序编译全库）。
   - 跑 filter 仿真：`make sim-axi_atop_filter.log`。该目标会执行 `scripts/run_vsim.sh`，其中 `axi_atop_filter` 分支会循环 `TB_AXI_MAX_WRITE_TXNS = 1 3 12` 三种配置，每种发 1000 笔随机事务（见 [scripts/run_vsim.sh:43-47](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L43-L47)）。
   - 测试台细节：上游是 `axi_rand_master` 且 `AXI_ATOPS=1'b1`（会发各类 ATOP，见 [test/tb_axi_atop_filter.sv:115-127](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_atop_filter.sv#L115-L127)），下游是**不懂 ATOP** 的 `axi_rand_slave`（[test/tb_axi_atop_filter.sv:142-156](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_atop_filter.sv#L142-L156)），中间是 DUT `axi_atop_filter_intf`。
   - 监视器断言「下游 atop 恒 0」：[test/tb_axi_atop_filter.sv:211-213](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_atop_filter.sv#L211-L213) 里 `if (downstream.aw_valid) assert (downstream.aw_atop == '0);`。
3. **需要观察的现象**：每跑完一个 seed，日志里出现 `Errors: 0,`（`run_vsim.sh` 用它判通过）。监视器会把每一笔原子写建模成「AW/W 被吸收 + 注入 SLVERR 的 B +（若非 ATOMICSTORE）注入 SLVERR 的 R」，再逐拍比对，任何不匹配都会 `$fatal`。
4. **预期结果**：三种 `MAX_WRITE_TXNS` 配置全部通过、日志无 `Error:`/`Fatal:`。
5. **对比实验（源码阅读型）**：要理解「不接 filter」的后果，设想把 DUT 换成纯直连 `axi_join`：带 `ATOP_R_RESP` 的原子写直达 `axi_rand_slave`，后者把它当普通写、只回 B 不回 R；主端在 `recv_r` 里永远等不到那几拍 R，仿真将挂死（或超时）。这正是 filter 要避免的死锁。你**不必真的去改 TB**——只要读懂 [test/tb_axi_atop_filter.sv:249-261](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_atop_filter.sv#L249-L261) 这段参考模型（它对 `thru=0` 的原子写既注入 B、又按 `len+1` 注入 R）就能看出：没有 filter 提供这层「假响应」，主端就少了一整串 R 拍。**待本地验证**（取决于本机是否装有 Questasim/Bender）。

#### 4.3.5 小练习与答案

**练习 1**：filter 在 `INJECT_B` 里注入 B 时，为什么「可以立即注入、不必观察排序」？
**答案**：因为 AXI 规定原子写在途期间该 ID 唯一（不与任何其他写/读突发共用 ID）。既然该 ID 此刻没有别的在途事务，注入 B 就不存在「插队破坏同 ID 保序」的风险。源码注释在 [src/axi_atop_filter.sv:217-219](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_atop_filter.sv#L217-L219) 明确指出了这一点。

**练习 2**：`BLOCK_AW` 状态存在的意义是什么？
**答案**：当检测到原子写时，若下游还有「已转发 AW 但 W 突发没收尾」的普通写，filter 必须先让那些 W 拍继续流向下游（`mst_req_o.w_valid` 透传），否则它们会被饿死。`BLOCK_AW` 就是「挡住新的 AW、放行在途 W」的过渡状态，等在途 W 清空后再开始吸收原子写的 W 拍。见 [src/axi_atop_filter.sv:173-193](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_atop_filter.sv#L173-L193)。

**练习 3**：为什么 `AxiMaxWriteTxns == 1` 时计数器宽度要特判成 2？
**答案**：计数器需要能表达「下溢（-1）」态来标记 `complete_w_without_aw_downstream`。若按 `$clog2(1+1)=1` 位，则无法同时区分 0、1 和下溢；强制最小 2 位（[src/axi_atop_filter.sv:61-62](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_atop_filter.sv#L61-L62)）配合 `underflow` 标志才能正确检测下溢。这也是测试台专门扫描 `MAX_WRITE_TXNS=1` 这一极端配置的原因。

## 5. 综合实践

把本讲的三块知识串起来，完成下面这个「端到端」的小任务。

**任务**：为一个虚拟的小型 SoC 设计 ATOP 隔离方案，并用工具有效验证。

1. **拓扑设计**：画一张框图：一个支持 ATOP 的 CPU 核（`axi_rand_master`，`AXI_ATOPS=1`）经一个 `axi_xbar`，分别接往 (a) 一个支持 ATOP 的内存控制器（可用 `axi_sim_mem` 近似）、(b) 一个**不支持** ATOP 的 legacy 寄存器外设。请在 (b) 的支路上标出 `axi_atop_filter` 的插入位置，并说明为什么 (a) 支路不需要加。
2. **编码演算**：CPU 发起一笔「原子比较并交换（CAS）」事务，写出它的 `aw_atop` 完整 6 位值（提示：用 `ATOP_ATOMICCMP`），并判断它是否带 R 响应、filter 会注入几拍 R（给定 `aw.len`）。
3. **验证执行**：运行 `make sim-axi_atop_filter.log`，确认三种 `MAX_WRITE_TXNS` 配置均报 `Errors: 0,`；然后在 [test/tb_axi_atop_filter.sv:196-387](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_atop_filter.sv#L196-L387) 的参考模型里找到「为带 R_RESP 的原子写预生成 `len+1` 拍 SLVERR R 响应」的循环，把它和你在第 2 步的演算对照。
4. **反思**：把 filter 的 `AxiMaxWriteTxns` 设成 1，对系统吞吐有什么影响？为什么 filter 仍能正确工作（提示：联系练习 3 与 `WAIT_R` 状态）。

预期产物：一张带 filter 标注的拓扑图、一份 CAS 的 `aw_atop` 演算、一份仿真通过截图/日志摘录。其中第 3 步的仿真运行**待本地验证**。

## 6. 本讲小结

- ATOPs 是 AXI5 叠加在 AXI4 之上的原子操作，只挂在 AW 通道的 6 位 `aw_atop` 上；`[5:4]` 是大类、`[3]` 是端序、`[2:0]` 是运算符，最高位 `[5]`（即 `ATOP_R_RESP`）决定是否产生 R 响应。
- `ATOP_NONE`/`ATOP_ATOMICSTORE` 不带 R 响应；`ATOP_ATOMICLOAD` 与 SWAP/CMP 带 R 响应——这由 `aw_atop[5]` 一锤定音。
- 不支持 ATOP 的普通从端收到带 R 响应的原子写时只回 B、不回 R，会让主端死锁；`axi_atop_filter` 正是为消化这种不匹配而存在。
- `axi_atop_filter` 给出两条保证：master 端 `aw_atop` 永远为 0；slave 端的原子写被回以 `RESP_SLVERR` 的完整 B（必要时加 R）响应。
- 内部用 W/R 两个 FSM 协作：W 侧吸收 AW 与 W 拍并注入 B，R 侧按 `len+1` 注入 SLVERR 的 R 拍，中间用一个 1 深 `stream_register` 传递「注 R 命令」。
- 一个带 `underflow` 标志的在途写计数器让 filter 在拦截原子写的同时仍能放行未完成的普通写；AXI「原子写 ID 在途唯一」的约束使响应注入无需考虑排序。

## 7. 下一步学习建议

- **axi_inval_filter（u15-l2）**：同属「监听 AW + 产生副作用」一类模块，对照阅读能巩固本讲的状态机套路。
- **axi_xbar 中的 ATOP 支持（u6-l1 / u6-l3）**：`xbar_cfg_t` 有 `ATOPs` 开关，建议回看 xbar 如何在 demux/mux 之间正确搬运带 R 响应的原子写，以及为何内部不能随意插 spill 寄存器。
- **tb_axi_atop_filter 的参考模型（本讲 test/）**：如果你想深入「如何为一个非平凡模块写自检测试台」，这个 TB 的 `w_cmd_queue`/`b_inject_queue`/`r_inject_queue` 多队列模型是非常好的范本，可结合 u3-l2、u16-l1 的验证方法学一起读。
- **AMBA 5 规范 E1.1 节**：本库的 ATOP 常量与 ID 唯一性约束都源自此处，遇到语义疑问时回到规范原文最可靠。
