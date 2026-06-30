# axi_isolate：总线隔离

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚「总线隔离（isolation）」到底隔离什么、为什么必须在掉电/复位前做；
- 复述 `axi_isolate` 的两个控制信号 `isolate_i` / `isolated_o` 的握手含义，以及「优雅排空在途事务」的完整过程；
- 读懂 `axi_isolate_inner` 里 AW/AR 各一套 `Normal → Hold → Drain → Isolate` 状态机，以及三个在途计数器 `pending_aw / pending_w / pending_ar` 是如何协同的；
- 解释 `TerminateTransaction` 参数取 `0` 或 `1` 时，隔离期间新到达事务的两种截然不同的命运（无限阻塞 vs. 立即返回错误响应）；
- 看懂 `tb_axi_isolate` 用「随机主从 + 随机翻转 isolate」做压力测试的结构，并能动手比对两种终止模式的行为。

## 2. 前置知识

本讲是 U7「流控与缓冲」的第二篇，承接 u7-l1 的缓冲思想（`spill_register` / `axi_fifo` 用寄存器切路径、吸收抖动）。在进入正文前，请确认你已理解以下概念（它们在前序讲义中已建立，这里只做最小回顾）：

- **在途事务（in flight / outstanding）**：地址拍已经握手、但响应拍尚未握手的事务。一个 AXI 写事务在 AW 握手后就「在途」，直到 B 握手才结束；读事务在 AR 握手后在途，直到最后一拍 R 握手才结束（见 u1-l3、u2-l3）。
- **valid/ready 铁律**：`valid` 一旦拉高，在握手（`valid && ready` 同周期）完成前**不允许撤下**，且其载荷必须保持稳定。本讲里的 `Hold` 状态就是为遵守这条铁律而存在的。
- **ATOP_R_RESP**：AXI5 原子操作中，`aw_atop` 的第 5 位（`ATOP_R_RESP`）置位表示这次原子写**还会产生读响应**（R 通道），而不仅仅是写响应（B 通道）。这一点会让「读通道在途计数」出现一个没有对应 AR 的 R，必须特殊处理（见 u2-l1、u15-l1）。
- **接口外壳 + 结构体内核**范式：`axi_isolate_intf` 用扁平位宽参数与 `AXI_TYPEDEF_*` / `AXI_ASSIGN_*` 宏做接口外壳，内核 `axi_isolate` / `axi_isolate_inner` 只面对 `axi_req_t` / `axi_resp_t` 结构体（见 u2-l4）。
- **axi_demux / axi_err_slv**：`axi_demux` 根据 `select` 把一个 slave 端口路由到多个 master 端口之一；`axi_err_slv` 是永远回错误响应的兜底从端（见 u5-l1、u6-l2）。本讲的 `TerminateTransaction` 模式正是复用了这两个模块。

如果你对「为什么要隔离」还没有直觉，记住一句话：**总线就像一根通着电的电线，你不能在电器还在工作（有在途事务）时直接剪断它**。`axi_isolate` 就是一个「先通知、等电器都停稳、再断电」的安全开关。

## 3. 本讲源码地图

本讲只涉及两个文件，它们都在仓库的常规位置：

| 文件 | 作用 | 本讲角色 |
|------|------|----------|
| [src/axi_isolate.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv) | 三个模块：顶层 `axi_isolate`（结构体内核）、`axi_isolate_inner`（排空状态机内核）、`axi_isolate_intf`（接口外壳） | 精读主体 |
| [test/tb_axi_isolate.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_isolate.sv) | 随机压测测试台：随机主从 + 随机翻转 `isolate` | 验证与动手实践 |

文件内部的模块布局速览（行号便于你跳读）：

- `axi_isolate`（顶层，结构体端口）：根据 `TerminateTransaction` 选择「demux + err_slv」或「直通」，再统一接到 `axi_isolate_inner`。
- `axi_isolate_inner`（排空引擎）：三组在途计数器 + AW/AR 两套四状态机，是全部隔离逻辑的所在。
- `axi_isolate_intf`（接口外壳）：把 `AXI_BUS.Slave` / `AXI_BUS.Master` 翻译成结构体后调用顶层。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先讲「隔离是什么、接口长什么样」，再讲「如何数清楚在途事务」，接着讲「排空的状态机」，最后讲「两种终止模式的差异及其测试台」。

### 4.1 隔离的需求、接口与参数

#### 4.1.1 概念说明

在很多 SoC 场景里，我们需要在运行过程中把一段 AXI 子网「断开」：

- **电源门控（power gating）**：某块子域要掉电省功耗，掉电前必须保证没有事务还「卡」在通往该域的总线上，否则恢复供电后会读到脏数据或死锁。
- **复位门控（reset gating）**：要复位某个 IP，但它正挂在总线上服务若干在途事务；得先让这些事务平安结束，再复位。
- **动态重构**：运行期要重新配置地址映射或切换互联拓扑，切换瞬间不允许有半截事务。

直接「剪断」总线的危险在于：可能存在 master 已经发出 AW、slave 还没回 B 的写事务；如果此时下游突然消失，master 会永远等不到 B，整个系统挂死。因此一个合格的隔离器必须做到两件事：

1. **优雅排空（drain）**：收到隔离请求后，不再向下游**发起**新事务，但**继续把已经在途的事务服务完**，等下游侧计数归零。
2. **可观测的完成信号**：排空结束后给一个明确输出 `isolated_o`，告诉系统「现在可以安全断电/复位了」。

`axi_isolate` 正是这样一个模块。它对**主从两侧完全对称地透明**——不隔离时就是一根直通线，隔离时才介入。

#### 4.1.2 核心流程

隔离的生命周期可以画成一条单向主线：

```text
                  isolate_i=0 (正常运行)
 master <======> [ axi_isolate ] <======> slave     直通，全程计数在途事务
                       |
            主干期间持续维护 pending_aw / pending_w / pending_ar
                       |
                  isolate_i 拉高 (请求隔离)
                       v
            不再接受/转发新事务，只让老事务排空
                       |
            pending 计数器陆续归零
                       v
            isolated_o 拉高 (已隔离，可安全断电)
                       |
                   isolate_i 拉低 (取消隔离)
                       v
            回到直通，恢复对新事务的服务
```

关键点是：`isolated_o` **不是** `isolate_i` 的简单延迟复制，而是「在途事务真的全部排空」的逻辑判定结果——即使 `isolate_i` 拉高，只要还有一笔 B 或 R 没回来，`isolated_o` 就不会拉高。

#### 4.1.3 源码精读

模块的接口与参数集中在顶部，先看参数表：

[src/axi_isolate.sv:L40-L59](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv#L40-L59) —— 顶层 `axi_isolate` 的参数列表，中文逐条说明：

- `NumPending`：每个通道允许的**最大在途事务数**，默认 16。它同时决定了计数器位宽与背压阈值。
- `TerminateTransaction`：本讲的主角开关，默认 `1'b0`。置 1 时隔离期间新事务立即返回错误响应；置 0 时新事务被无限阻塞。
- `AtopSupport`：是否支持原子操作，默认开。
- 四个 `AxiXxxWidth` 与两个 `axi_req_t` / `axi_resp_t` 类型参数：标准的「位宽 + 结构体类型」成对声明（见 u2-l4）。

[src/axi_isolate.sv:L60-L77](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv#L60-L77) —— 端口列表：一对 slave 侧的 `slv_req_i` / `slv_resp_o`、一对 master 侧的 `mst_req_o` / `mst_resp_i`，外加隔离握手 `isolate_i` / `isolated_o`。注意 master 端是「输出请求、输入响应」，说明本模块从 slave 侧看是一个 slave、从 master 侧看是一个 master，处于总线中间。

模块开头那段注释是全库少有的「行为契约」级文档，强烈建议先读：

[src/axi_isolate.sv:L19-L39](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv#L19-L39) —— 用英文写明了：不隔离时两端直连；`isolate_i` 置位后优雅终止在途事务，全部排空后 `isolated_o` 置位；`isolated_o` 期间 master 侧输出全部静默为 `'0`；并预告了 `TerminateTransaction=1` 时会回错误响应、数据为 `1501A7ED`（hexspeak，读作 “isolated”）。

#### 4.1.4 代码实践（源码阅读型）

**目标**：在打开仿真器之前，先把「模块对外的承诺」吃透。

**步骤**：

1. 打开 [src/axi_isolate.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv)，只读 L19–L77 这一段注释 + 参数 + 端口，**先不看实现**。
2. 在纸上回答三个问题：
   - 如果我永远不拉 `isolate_i`，这个模块对外行为像什么？（提示：和 `axi_join` 有何异同？）
   - `isolated_o` 拉高的充要条件里，是否包含「`isolate_i` 必须仍然为 1」？
   - `NumPending=16` 意味着 master 侧最多能同时看到多少笔未完成的写？

**需要观察的现象 / 预期结果**：

- 不拉 `isolate_i` 时，模块应等价于一个会做在途计数与背压的「受控直通」——它不是纯连线，因为它会用 `NumPending` 限制在途并发。
- `isolated_o` 的产生**必须**以 `isolate_i` 为前提（状态机只有在 `isolate_i` 驱动下才会进入 Isolate 态）。
- 写在途上限受 `NumPending` 约束（见 4.2.3 的容量判定）。

本步骤无须运行仿真，结论「待本地验证」的部分仅指你自己推导出的具体数值。

#### 4.1.5 小练习与答案

**练习 1**：模块文档说 `isolated_o` 期间「all output signals in `mst_req_o` are silenced to `'0`」。请结合 AXI 协议思考：把 `mst_req_o` 全部置零，会不会违反「valid 不能凭空撤下」的规则？

**参考答案**：不会。因为进入 `Isolate` 态的**前提**是所有在途事务已经排空（pending 计数归零），此时 master 侧没有任何一笔已经发出但未被下游接受的事务，所以把 `mst_req_o` 的 valid 清零不会丢掉任何已承诺的握手。这正是「先排空、再静默」顺序的意义。

**练习 2**：`TerminateTransaction` 是编译期参数还是运行期信号？这意味着什么？

**参考答案**：它是 `parameter bit`，即**编译期/参数化**的，不能在运行中切换。两种模式会综合出不同的硬件（一个有 demux+err_slv，一个没有），所以选型必须在例化时决定。

---

### 4.2 在途事务计数器（含 ATOP 注入）

#### 4.2.1 概念说明

「优雅排空」的前提是「数得清当前有多少事务在途」。`axi_isolate_inner` 维护了**三个**独立计数器：

- `pending_aw`：已发往 master、尚未收到 B 响应的**写事务**数。
- `pending_w`：已发往 master、但写数据（W 通道）还没传完的**写突发**数（用于决定 W 通道何时该被切断）。
- `pending_ar`：已发往 master、尚未收到最后一拍 R 响应的**读事务**数。

为什么要单独有一个 `pending_w`？因为 AXI 的写数据 W 是独立通道，AW 握手后 W 拍可能还要好几个周期才流完。隔离器需要知道「还有哪些 W 突发没传完」，以免在切断时漏掉数据拍或放进来路不明的 W。

#### 4.2.2 核心流程

每个计数器都是「加一 / 减一」的离散计数，驱动事件如下（以写方向为例）：

```text
pending_aw:  AW 在 Normal 态被 master 接受  --> +1
             B  响应被 master 返回且本端 ready --> -1

pending_w:   AW 在 Normal 态被 master 接受  --> +1   (与 pending_aw 同步 +1)
             W  最后一拍(last) 握手          --> -1

pending_ar:  AR 在 Normal 态被 master 接受  --> +1
             原子写 AW 带 ATOP_R_RESP        --> +1   (注入！没有对应 AR)
             R  最后一拍(last) 握手          --> -1
```

最值得注意的就是 `pending_ar` 的**第三行**：原子写（AW 带 `ATOP_R_RESP`）会绕过 AR 通道直接产生若干拍 R 响应。如果不提前在 AR 计数器里「预存」一笔，这些 R 拍到来时会让 `pending_ar` 出现下溢（变成负数/环绕），排空判定就会出错。因此模块在 AW 握手的那一刻，**向 AR 计数器注入（inject）一个 +1**，把这次原子写「伪装」成一笔普通读事务去等待 R 排空。

计数器的位宽也专门为此留了余量：

\[
\text{CounterWidth} = \lceil \log_2(\text{NumPending}+1) \rceil + 1
\]

公式右边第一项 `$clog2(NumPending+1)` 让计数器能表示 `0 .. NumPending`（含「无在途」的 0），额外 `+1` 位则是给 AR 计数器预留的原子注入余量——因为极端情况下 AR 计数器可能同时持有 `NumPending` 笔真读 + `NumPending` 笔原子注入，上限可达 `2*NumPending`。

#### 4.2.3 源码精读

计数器的位宽与寄存器声明：

[src/axi_isolate.sv:L181-L183](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv#L181-L183) —— 注释 `plus 1 in clog for accounting no open transaction, plus one bit for atomic injection` 正好对应上面的公式；`cnt_t` 是三个计数器共用的类型。

[src/axi_isolate.sv:L194-L207](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv#L194-L207) —— 三个 `_q` 寄存器与对应 `_d` 组合逻辑、`update_*` 使能，以及用 `FFLARN` 宏（带 load/load-enable 的异步复位寄存器，来自 `common_cells`）实例化的寄存器组。注意 AW/AR 的**状态机**寄存器复位值是 `Isolate`（L206-L207），即上电默认就处于已隔离态——这是个安全默认：上电过程中在 `rst_n` 释放前不会把总线误连出去。

计数器的全部加减逻辑集中在一个 `always_comb` 里：

[src/axi_isolate.sv:L219-L229](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv#L219-L229) —— AW 被接受时 `pending_aw++` 且 `pending_w++`（一个 AW 对应一个待传的 W 突发），并置 `connect_w=1`（允许 W 通道本周期接通）。最关键的是 L225-L228：若该 AW 的 `atop[ATOP_R_RESP]` 置位，则同时 `pending_ar++`——这就是**原子注入**，让后续的 R 拍有计数可抵消。

[src/axi_isolate.sv:L230-L237](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv#L230-L237) —— W 通道在 `w.last` 拍握手时 `pending_w--`；B 通道握手时 `pending_aw--`。两者共同决定一笔写事务的两个阶段何时分别结束。

[src/axi_isolate.sv:L239-L246](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv#L239-L246) —— AR 被接受时 `pending_ar++`；R 通道在 `r.last` 拍握手时 `pending_ar--`。注意只有「最后一拍」才减——读突发可能有多拍 R，必须等全部到齐。

计数器的值随后被两处使用：一是 4.3 里状态机的容量判定与排空判定，二是顶层一组断言防止溢出/下溢：

[src/axi_isolate.sv:L395-L409](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv#L395-L409) —— `aw_overflow / ar_overflow / aw_underflow / ar_underflow` 四条 `assert property`，分别保证计数器不会从全 1 翻到 0（溢出）、不会从 0 翻到全 1（下溢）。这是计数逻辑正确性的形式化兜底。

#### 4.2.4 代码实践（源码阅读型）

**目标**：验证你对「原子注入」的理解，确认它能保证 `pending_ar` 不下溢。

**步骤**：

1. 阅读 [src/axi_isolate.sv:L219-L246](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv#L219-L246)。
2. 假想一个场景：master 端发了一笔「原子读改写」（AW 带 `ATOP_R_RESP`，假设读响应只有 1 拍即 `r.last` 立即成立），期间没有任何普通 AR。在纸上列出从 AW 握手到 R 握手每个事件对 `pending_ar` 的影响：
   - AW 握手（带 ATOP_R_RESP）：`pending_ar` = ?
   - R 的最后一拍握手：`pending_ar` = ?
3. 再假想「关闭 AtopSupport、却来了一笔带 ATOP_R_RESP 的 AW」会发生什么（提示：看 L225 的条件是否还成立，以及下游会不会真的回 R）。

**需要观察的现象 / 预期结果**：

- 正常原子读改写：AW 注入 +1，R 握手 −1，`pending_ar` 回到 0，**不下溢**。
- 若 `AtopSupport=0`，本模块不再处理注入逻辑；但此时上游也不应产生 ATOP（这是契约），否则下游的 R 拍会让 `pending_ar` 持续为 0 时被减——这是上游必须遵守的前提，不是本模块的 bug。具体仿真波形「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `pending_w` 的 +1 和 `pending_aw` 的 +1 是在**同一个事件**（AW 被接受）下发生的，而不是 W 第一拍握手时才 +1？

**参考答案**：因为 AW 一旦被 master 接受，就**承诺**了随后会有一个完整的 W 突发到来；这个 W 突发属于「已经在途」的状态，必须立刻计入 `pending_w`，隔离器才能知道「还有 W 数据没传完」。如果等到 W 第一拍才计数，那么 AW 握手后、W 第一拍到来前的若干周期里，隔离器会误以为没有待传 W，从而可能在排空时把后续 W 拍错误地切断。

**练习 2**：容量判定里有一行 `pending_ar_q >= cnt_t'(2*NumPending)`（见 4.3.3），但 `pending_aw` 和 `pending_w` 的阈值都只是 `NumPending`。为什么 AR 的阈值要翻倍？

**参考答案**：因为 AR 计数器除了普通读事务（最多 `NumPending` 笔），还可能累积原子写注入的 `NumPending` 笔，理论最大值是 `2*NumPending`。写方向的容量判定必须把 AR 也算进来，才能避免「AW 容量没满、但 AR 已经被原子注入撑满」时继续接受新的原子写而导致计数器溢出。

---

### 4.3 排空状态机：Normal / Hold / Drain / Isolate

#### 4.3.1 概念说明

光有计数器还不够，还需要一套状态机来决定「何时停止接受新事务、何时判定排空完成」。`axi_isolate_inner` 给 **AW 通道**和 **AR 通道**各配了一套完全对称的四状态机（写多了一个 W 通道的小处理）：

- **Normal**：正常运行。新事务正常转发，同时持续做容量检查；一旦 `isolate_i` 拉高，准备进入排空。
- **Hold**：保持 valid。专门用来守住 AXI「valid 不能撤」的铁律（见 4.3.2）。
- **Drain**：排空。切断本通道的新事务转发，等待 `pending` 计数器归零。
- **Isolate**：已隔离。本通道彻底静默；当 AW 与 AR **都**进入此态时，`isolated_o` 才置位。

#### 4.3.2 核心流程

以 AW 状态机为例，状态转移如下（AR 几乎一致，只是没有 W 的联动）：

```text
               +-------------------+
               |      Normal       |  isolate_i=0 且未到容量上限：正常转发
               |  容量满 OR isolate |--+
               +-------------------+  |
                 |              |    |
          slv.aw_valid          |    |
          && !mst.aw_ready      |    | isolate_i (且无悬空 valid)
                 v              v    v
            +---------+    +---------+
            |  Hold   |    |  Drain  |
            | 强制     |    | 切新AW  |
            | aw_valid |-->| 等pending|
            | =1，等   |   | _aw==0   |
            | mst接受  |    +---------+
            +---------+         |
                                v
                          +-----------+
                          |  Isolate  |  本通道静默
                          | 等        |
                          | !isolate_i|
                          +-----------+
                                |
                                v
                            回到 Normal
```

**Hold 态存在的理由**（本讲最重要的一个细节）：在 Normal 态，模块默认把 `mst_req_o.aw_valid` 直接连到 `slv_req_i.aw_valid`（透传）。一旦某周期 slave 拉高了 `aw_valid` 而 master 当周期没接受（`aw_ready=0`），模块就在该周期把这笔 AW **计入 `pending_aw`**（见 4.2.3）——也就是说，模块已经「认领」了这笔 AW，对 master 做出了「我会把 valid 维持住」的承诺。下一个周期如果直接切到 Drain、把 `mst_req_o.aw_valid` 清零，就违背了 valid 铁律、且会丢掉已经计数的事务。所以模块先进入 **Hold**：自己接管 `aw_valid` 强制为 1，直到 master 真正接受（`aw_ready=1`），再进入 Drain（若仍请求隔离）或回到 Normal。

换句话说，**Hold 把「slave 已经喊出 valid、master 还没收」这个易碎的中间态，安全地缝合到排空流程里**。

#### 4.3.3 源码精读

状态枚举与隔离完成信号：

[src/axi_isolate.sv:L185-L190](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv#L185-L190) —— `isolate_state_e` 定义了四个状态，AW 与 AR 各持有一对 `_d/_q`。

[src/axi_isolate.sv:L387-L388](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv#L387-L388) —— `isolated_o` 的判定：**AW 与 AR 状态机同时处于 Isolate 态**才置位。这保证读写两个方向都排空了，缺一不可。

AW 状态机的四个分支：

[src/axi_isolate.sv:L264-L288](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv#L264-L288) —— **Normal 态**。先做容量判定（L268-L269）：`pending_aw >= NumPending` 或 `pending_ar >= 2*NumPending` 或 `pending_w >= NumPending` 任一成立，就切断 AW 握手并（若 `isolate_i`）进 Drain；否则若 slave 已喊 valid 而 master 未接受，进 **Hold**；否则若 `isolate_i` 则进 Drain。注意容量判定里 `2*NumPending` 正是 4.2 练习 2 提到的 AR 翻倍阈值。

[src/axi_isolate.sv:L289-L296](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv#L289-L296) —— **Hold 态**：强制 `mst_req_o.aw_valid = 1'b1`，等 `mst_resp_i.aw_ready` 一拉高（master 收下），就依 `isolate_i` 决定去 Drain 还是 Normal。

[src/axi_isolate.sv:L297-L305](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv#L297-L305) —— **Drain 态**：把 AW 通道载荷与 valid/ready 全部切断，等 `pending_aw_q == '0` 后进 Isolate。

[src/axi_isolate.sv:L306-L317](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv#L306-L317) —— **Isolate 态**：把 AW、B 两侧的载荷与 valid/ready 全部清零（slave 侧 B 也静默），直到 `!isolate_i` 才回 Normal。

W 通道的切断独立于状态机，只看计数器：

[src/axi_isolate.sv:L321-L326](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv#L321-L326) —— 当 `pending_w_q == 0` 且本周期没有新 AW 接通（`!connect_w`）时，把 W 通道切断。这防止「没有任何待传突发」时 stray 的 W 拍漏到 master 侧。

AR 状态机结构完全对称（无 W 联动）：

[src/axi_isolate.sv:L332-L382](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv#L332-L382) —— AR 的 Normal/Hold/Drain/Isolate 四态，逻辑与 AW 镜像，容量判定只看 `pending_ar >= NumPending`（读方向不需要考虑原子注入对自身容量的双倍影响——注入是「AW 往 AR 计数器」单向的）。

#### 4.3.4 代码实践（源码阅读型）

**目标**：在脑中完整跑一遍「拉高 isolate_i 到 isolated_o 拉高」的全过程，验证你对四个状态的理解。

**步骤**：

1. 假设某一时刻 AW 与 AR 都在 Normal，且 `pending_aw=2`、`pending_ar=1`（有几笔在途）。
2. `isolate_i` 在 T 周期拉高。请在纸上推演：
   - T 周期：AW 状态机进入哪个态？（看 L282-L285）AR 呢？
   - 后续若干周期：master 陆续回完 B 和最后一拍 R，`pending_aw`、`pending_ar` 何时归零？
   - 两个状态机分别在 `pending` 归零后进入哪个态？（看 L301-L304、L366-L369）
   - `isolated_o` 在哪个周期才拉高？（看 L388）
3. 然后 `isolate_i` 拉低：两个状态机各自从 Isolate 回到 Normal（L313-L316、L378-L381），`isolated_o` 随之拉低。

**需要观察的现象 / 预期结果**：

- AW、AR 是**各自独立排空**的：哪个方向先归零，哪个先进入 Isolate；`isolated_o` 要等**两者都**到 Isolate。
- 排空期间若有悬空的 valid（slave 喊了、master 没收），会先经过 Hold 兜一道，再进 Drain。
- 具体波形「待本地验证」，但状态转移的先后顺序是确定的。

#### 4.3.5 小练习与答案

**练习 1**：假如没有 Hold 态，直接从 Normal 跳到 Drain，会在什么场景下违反 AXI 协议？

**参考答案**：当 slave 在 Normal 态拉高了 `aw_valid` 但 master 当周期没接受时，模块已经把这笔 AW 计入 `pending_aw` 并向 master 透传了 `aw_valid=1`。若下一周期直接进 Drain 把 `mst_req_o.aw_valid` 清零，从 master 视角就是「valid 在握手前被撤下」，违反 AXI valid 铁律；同时这笔已计数的事务会丢失，导致 `pending_aw` 永远减不回 0、`isolated_o` 永远不拉高。Hold 态通过「接管 valid 并等 master 收下」避免了这两个问题。

**练习 2**：`isolated_o` 何时拉低？是 `isolate_i` 一拉低就立刻拉低吗？

**参考答案**：不是立刻。`isolate_i` 拉低后，AW/AR 状态机要分别在下一个被 `update_*_state` 触发的时钟沿从 Isolate 态切回 Normal 态（L313-L316、L378-L381）；只有当两者都离开 Isolate 后，`isolated_o = (aw==Isolate && ar==Isolate)` 才为 0。所以 `isolated_o` 的下降沿通常比 `isolate_i` 的下降沿晚若干个时钟周期。

---

### 4.4 TerminateTransaction 两种模式与测试台

#### 4.4.1 概念说明

到目前为止，我们描述的都是 `TerminateTransaction=0`（默认）的行为：隔离期间 slave 侧新到达的事务会被**无限阻塞**——它的 valid 被切断、ready 不给，直到 `isolate_i` 拉低才放行。这对上游 master 来说是「挂起」，master 会一直等。

但在某些系统里，挂起 master 是不可接受的（比如 master 没有超时机制，或隔离可能持续很久）。这时可以设 `TerminateTransaction=1`：隔离期间新到达的事务**不再阻塞**，而是立刻收到一个错误响应（`RESP_DECERR`），数据为 `0x1501A7ED`。这样 master 能马上知道「这笔失败了」，可以自行重试或上报，而不是死等。

两种模式的对比：

| 维度 | `TerminateTransaction=0`（阻塞） | `TerminateTransaction=1`（终止） |
|------|----------------------------------|----------------------------------|
| 隔离期间新事务的命运 | valid 被卡住，无限等待 | 立刻收到 DECERR + 数据 `0x1501A7ED` |
| 额外硬件开销 | 无（纯直通分支） | 多一个 `axi_demux` + 一个 `axi_err_slv` |
| 适用场景 | 隔离时间短、master 能容忍挂起 | master 不能死等、需要快速失败 |
| 对 master 的要求 | 必须能容忍长时间无响应 | 必须能正确处理 DECERR |

#### 4.4.2 核心流程

`TerminateTransaction=1` 的实现思路非常巧妙——**复用现成的 `axi_demux` + `axi_err_slv`**：

```text
                slave 侧新事务
                     |
         +---------------------------+
         |        axi_demux          |   select = isolated_o
         |   NoMstPorts = 2          |
         +---------------------------+
            | port 0              | port 1
            | (未隔离时走这里)     | (隔离时走这里)
            v                      v
     axi_isolate_inner         axi_err_slv
     (通往真实 master)         (立刻回 DECERR,
                               数据 0x1501A7ED)
```

`axi_demux` 的 `select` 信号直接接到 `isolated_o`：

- **未隔离**（`isolated_o=0`）：新事务走 port 0，进入 `axi_isolate_inner`，正常通往 master。
- **已隔离**（`isolated_o=1`）：新事务走 port 1，进入 `axi_err_slv`，立刻被错误响应吃掉。

为什么这个分发是安全的？因为 `isolated_o=1` 意味着 `axi_isolate_inner` 里两个方向都已排空、pending 全归零，port 0 这条路上已经没有任何在途事务；此时把新事务改道到 port 1，不会和正在排空的老事务混在一起。`axi_demux` 自带的 W 通道突发队列（u5-l1）还能保证：即便某笔写突发的 AW 在改道前已经进了 port 0，它的 W 拍也会跟到 port 0 走完，不会被错路由到 err_slv。

#### 4.4.3 源码精读

顶层的 `if (TerminateTransaction)` 分支用 `generate` 式的条件在编译期二选一：

[src/axi_isolate.sv:L94-L125](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv#L94-L125) —— 终止模式：例化一个 `NoMstPorts=2` 的 `axi_demux`，`slv_aw_select_i` / `slv_ar_select_i` 都接 `isolated_o`（L120-L121）。注释 L107 解释 `AxiLookBits=1`：因为绝大多数情况下事务应走 port 0（直通），ID 查找不需要太精细，省面积。

[src/axi_isolate.sv:L127-L141](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv#L127-L141) —— port 1 接 `axi_err_slv`，响应码 `RESP_DECERR`、数据 `RespData = 'h1501A7ED`（L132-L133），正好对上模块文档承诺的 hexspeak。注意它**覆盖**了 `axi_err_slv` 默认的 `0xBADCAB1E`（u6-l2 见过默认值），换成专属的 “isolated” 标记，便于在波形里一眼认出「这笔是被隔离器终止的」。

[src/axi_isolate.sv:L142-L148](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv#L142-L148) —— 阻塞模式（`else`）：port 0 直接 `assign` 连到 slave，port 1 静默为 `'0`。没有任何 demux/err_slv，零额外开销。新事务的阻塞完全由 `axi_isolate_inner` 的 Isolate 态（切断 ready）实现。

[src/axi_isolate.sv:L150-L163](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv#L150-L163) —— 无论哪种模式，port 0 都统一接到同一个 `axi_isolate_inner`。这是「组合优于配置」的又一个例子：终止逻辑复用 demux+err_slv，排空逻辑始终是那一套状态机。

测试台 `tb_axi_isolate` 的关键结构：

[test/tb_axi_isolate.sv:L18-L38](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_isolate.sv#L18-L38) —— 参数：每主端 5 万写、3 万读；`MaxAW=MaxAR=30` 限制在途并发；`EnAtop=1` 开原子操作；时序三件套 `CyclTime=10ns / ApplTime=2ns / TestTime=8ns`（满足 `0<TA<TT<T_clk`，见 u3-l3）。注意 DUT 的 `NoPendingDut=16`。

[test/tb_axi_isolate.sv:L118-L131](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_isolate.sv#L118-L131) —— DUT 例化 `axi_isolate_intf`，**没有**显式传 `TERMINATE_TRANSACTION`，因此取默认值 `1'b0`（阻塞模式）。这也是本讲实践里你要亲手改成 `1` 来对比的地方。

[test/tb_axi_isolate.sv:L154-L161](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_isolate.sv#L154-L161) —— `proc_sim_ctl` 是整个压测的灵魂：在一个 `forever` 循环里，`isolate` 随机置低若干周期、再随机置高若干周期（用 `$urandom_range(100000,1)`），从而在整个 8 万笔事务的运行过程中**反复触发隔离与恢复**，充分覆盖 Normal/Hold/Drain/Isolate 的来回切换。

[test/tb_axi_isolate.sv:L205-L220](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_isolate.sv#L205-L220) —— 五条 `assert property` 监督 pending 期间各通道载荷稳定（AW/W/B/AR/R 的 `_unstable` 断言），这是协议级自检，确保隔离器在反复切换中不破坏 AXI 铁律。

#### 4.4.4 代码实践（可运行 + 对照实验）

这是本讲的核心实践，对应规格里要求的手动场景：**隔离期间仍有新读请求到达，分别用 `TerminateTransaction=0/1` 观察行为，记录 `isolated_o` 何时拉高**。

**目标**：亲手跑通阻塞模式，再做一个最小改动跑通终止模式，比对两者对「隔离期间新读请求」的处理差异。

**操作步骤**：

1. **先跑现成的阻塞模式**（无需改任何源码）：

   ```bash
   make sim-axi_isolate.log
   ```

   这会调用 `scripts/run_vsim.sh`，对 `tb_axi_isolate` 用默认种子与一个随机种子各跑一次（见 `run_vsim.sh` 的 `axi_isolate` 走默认 `*)` 分支）。`proc_sim_ctl` 会在运行中反复翻转 `isolate`。

2. **打开生成的日志**（`build/vsim.log` 或 `sim-axi_isolate.log`），确认：

   - 出现 `Errors: 0,`（这是 `run_vsim.sh` 的通过判据，见 u1-l4）；
   - 日志里能看到 `Transmit AW ... of 50000.` / `Transmit AR ... of 30000.` 的进度行；
   - 仿真结束前出现 `All transactions completed.`（来自 `proc_sim_progress`，L197-L200）。

3. **做终止模式的对照实验**——由于不能改仓库源码，请在**你自己的临时目录**里复制一份测试台来改（保持原文件不动）：

   ```bash
   # 在仓库外或你的 scratch 目录
   cp test/tb_axi_isolate.sv /tmp/tb_axi_isolate_term.sv
   ```

   然后把例化处的参数补上一行 `TERMINATE_TRANSACTION`：

   ```systemverilog
   // 在 /tmp/tb_axi_isolate_term.sv 的 i_dut 例化里加：
   //   .TERMINATE_TRANSACTION ( 1'b1 ),
   ```

   用你惯用的仿真器编译运行这份改过的 TB（编译方式参考 `scripts/compile_vsim.sh` 的 `bender script vsim -t test -t rtl`）。

4. **在波形里抓三个信号**：`isolate`、`isolated`、以及 slave 侧的 `ar_valid` / `ar_ready` / 读返回的 `r_resp` / `r_data`。

**需要观察的现象 / 预期结果**：

- **阻塞模式（步骤 1-2）**：`isolated` 在每次 `isolate` 拉高后、经过若干周期（等在途排空）才跟着拉高；在 `isolated` 有效的区间里，slave 侧新到达的 `ar_valid` 会被卡住（`ar_ready` 保持低），直到 `isolate` 拉低才放行。整个仿真应无错通过。
- **终止模式（步骤 3-4）**：`isolated` 的拉高时序与阻塞模式**一致**（排空逻辑相同）；但 `isolated` 有效期间，slave 侧新到达的读请求**不再被卡住**——它会很快收到一拍 `r_resp == RESP_DECERR` 且 `r_data == 32'h1501A7ED` 的读响应，master 侧因此不会死等。
- **共同点**：两种模式下 `isolated_o` 拉高的**条件完全相同**（都要求 AW/AR 状态机进入 Isolate），差异只在于「隔离期间新事务」是被阻塞还是被终止。

> 说明：步骤 1-2 的「`Errors: 0,` 通过」是仓库 CI 的回归判据，可以预期；步骤 3-4 涉及你本地改 TB 与波形观察，具体周期级时序「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：在终止模式下，进入 `Isolate` 态**之前**的 Drain 阶段（`isolated_o` 还为 0），新到达的事务走 demux 的哪个端口？会被立即终止吗？

**参考答案**：Drain 阶段 `isolated_o` 仍为 0，所以 demux 的 `select=0`，新事务仍走 port 0 进入 `axi_isolate_inner`。但此时 inner 处于 Drain 态，会切断新事务的 ready，所以**不会立即终止，而是被短暂阻塞**，直到 inner 进入 Isolate、`isolated_o` 拉高后，demux 才把后续新事务改道到 port 1 的 err_slv 立即终止。也就是说，终止模式并非「全程不阻塞」，而是「排空完成后的隔离稳态才立即终止」。

**练习 2**：为什么 `axi_err_slv` 的 `RespData` 要特意从默认的 `0xBADCAB1E` 改成 `0x1501A7ED`？

**参考答案**：为了**可观测性与可区分性**。`0x1501A7ED` 是 “isolated” 的 hexspeak；在波形或日志里看到这个数据，调试者就能立刻断定「这笔读响应来自 `axi_isolate` 的终止路径，而非系统中其他 err_slv（后者多用默认的 `0xBADCAB1E`）」。这是一个低成本但高收益的调试友好设计。

---

## 5. 综合实践

把本讲四个最小模块串起来，做一个「带隔离的安全掉电」端到端小任务。

**场景**：你有一个 master 通过 `axi_isolate` 连到下游子网，下游子网即将掉电。请设计一个最小验证序列，演示「安全掉电」协议：

1. **搭拓扑**：`axi_rand_master` → `axi_isolate_intf` → `axi_rand_slave`（可仿照 `tb_axi_isolate` 的例化，但把随机翻转 `isolate` 的 `proc_sim_ctl` 换成**你手动驱动**的定向序列）。
2. **定向序列**（在你的 TB 里用 `initial` 块手写）：
   - 先让 master 发起若干笔读写（混合普通读、普通写、带 `ATOP_R_RESP` 的原子读改写），等它们部分在途；
   - 拉高 `isolate_i`，**观察**：master 是否还能继续拿到新事务的响应？（不能，新事务被卡）已经在途的老事务是否正常完成？（是，pending 计数陆续归零）
   - 等 `isolated_o` 拉高，**此刻**断言「下游可以安全掉电」（用一条 `$info` 打印）；
   - 维持 `isolate_i` 一段周期模拟掉电窗口；
   - 拉低 `isolate_i` 恢复，观察 master 被卡住的新事务是否恢复放行。
3. **加检查**：在 `isolated_o` 拉高**之前**，断言 `pending_aw == 0 && pending_ar == 0` 不成立（说明确实等到了排空）；在 `isolated_o` 拉高**之后**，断言这两个计数器都为 0。为此你需要从 DUT 引出（或用层次化引用 `tb.i_dut.i_axi_isolate.i_axi_isolate.pending_aw_q` 等）观察内部计数器。
4. **对照**：把 DUT 的 `TERMINATE_TRANSACTION` 分别设 0 和 1 各跑一次，在报告里说明：掉电窗口期间新事务在两种模式下分别是什么命运，以及 `isolated_o` 的时序是否相同。

**预期结果**：两种模式下 `isolated_o` 的拉高时序一致（都由排空状态机决定）；区别仅在掉电窗口内新事务是被阻塞（模式 0）还是被 DECERR 终止（模式 1）。计数器断言应全部通过，证明「排空完成」是 `isolated_o` 的真实前提。

> 本任务需要你自建一个定向 TB，仓库未提供现成版本；具体信号层级名与时序「待本地验证」。

## 6. 本讲小结

- `axi_isolate` 是一个「先排空在途事务、再静默总线」的安全开关，靠 `isolate_i` 请求隔离、`isolated_o` 报告隔离完成，常用于掉电/复位门控之前。
- 核心引擎 `axi_isolate_inner` 维护 `pending_aw / pending_w / pending_ar` 三个在途计数器；其中原子写（`ATOP_R_RESP`）会向 AR 计数器**注入** +1，避免 R 拍下溢。
- AW/AR 各一套 `Normal → Hold → Drain → Isolate` 状态机：`Hold` 专门守住「valid 不能撤」铁律，`Drain` 等计数器归零，`Isolate` 静默本通道；`isolated_o` 仅当两个方向**都**进入 Isolate 才置位。
- `TerminateTransaction=0`（默认）时隔离期间新事务被无限阻塞；`=1` 时复用 `axi_demux`（select=`isolated_o`）+ `axi_err_slv`（回 `RESP_DECERR`、数据 `0x1501A7ED`）立即终止新事务，两种模式排空时序完全一致。
- `tb_axi_isolate` 用随机主从 + 随机反复翻转 `isolate` 做压力测试，默认跑阻塞模式；`make sim-axi_isolate.log` 是它的运行入口，以 `Errors: 0,` 判通过。

## 7. 下一步学习建议

- **横向对比流控三件套**：回到 u7-l1 的 `axi_fifo` 与本讲的 `axi_isolate`、再加 u7-l3 的 `axi_throttle`，画一张表对比三者——`fifo` 管「缓冲与切路径」、`isolate` 管「安全断开」、`throttle` 管「限流」，弄清何时用哪个。
- **向下游延伸**：本讲的 `TerminateTransaction=1` 复用了 `axi_err_slv` 与 `axi_demux`，如果你对这两个模块的内部还不熟，建议回头读 u5-l1（demux 的 W 突发队列在这里保证了改道安全）和 u6-l2（err_slv 的响应行为）。
- **进入时钟域跨越**：隔离常被部署在时钟域/电源域边界，下一篇 u8-l1 的 `axi_cdc` 正好处理跨时钟域；把 `axi_isolate` 与 `axi_cdc` 串联是真实 SoC 里非常常见的组合，可以提前思考两者的先后顺序与死锁风险。
- **协议深化**：本讲反复出现的 `ATOP_R_RESP` 将在 u15-l1（ATOPs 与 `axi_atop_filter`）全面展开，届时你会更完整地理解原子操作对 ID 唯一性与响应通道的影响。
