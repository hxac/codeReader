# axi_join / axi_cut / axi_multicut

## 1. 本讲目标

本讲是「总线连接与组合原语」的第一讲，讲三个**最简单、却最常用**的 AXI 连接器。读完本讲，你应该能够：

- 区分 `axi_join`（纯连线）、`axi_cut`（一级寄存器）、`axi_multicut`（多级寄存器）三者的本质差异。
- 解释为什么 `axi_cut` 能改善关键路径（relax timing），以及它为此付出的代价（延迟与面积）。
- 在一个真实设计里，根据「要不要切断组合路径」「要切几级」正确地在这三者之间做选择。
- 读懂这三个模块的源码结构，并能复述「接口外壳 + 结构体内核」这一全库通用范式在本讲里的体现。

本讲不涉及任何 AXI 协议细节、不修改任何 RTL，只讲「如何把两段 AXI 总线背靠背接起来」。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自前置讲义）：

- **五通道与 valid/ready 握手**（u1-l3）：AW/W/B/AR/R 五个通道，每拍只有 `valid` 与 `ready` 同高才算一次握手；`in flight`（在途，地址已握而响应未握）、`pending`（挂起，valid 高 ready 低）两个术语。
- **`AXI_BUS` 接口与 modport**（u2-l3）：`AXI_BUS` 把约 43 根信号打包成一个 bundle，`Master`/`Slave` modport 预设了方向。
- **typedef / assign 宏体系**（u2-l4）：`req_t`/`resp_t` 结构体内核 + `AXI_BUS` 接口外壳的「双视图」范式，以及 `AXI_ASSIGN`、`AXI_TYPEDEF_*` 宏的用法。
- **编译层级**（u1-l2）：被 `import` 的 `package` 必须先编译，所以本库把文件按 Level 0–6 拓扑分层。

本讲会频繁用到两个**时序相关**的基础概念，先在这里统一说明：

- **组合路径（combinatorial / combinational path）**：信号从输入端经过若干纯组合逻辑（与、或、选择器、连线，不经过任何触发器）到达输出端，在**同一个时钟周期内**就能传到对端。路径越长，关键路径越长，能跑到的时钟频率就越低。
- **切断组合路径（break the combinatorial path）**：在路径中间插入一级**寄存器（触发器）**。信号到达寄存器后要等下一个时钟沿才能继续往后传，于是这条「同周期贯穿」的路径被切成了两段更短的路径。代价是：多了一个时钟周期的延迟，以及一小块寄存器面积。这正是 `axi_cut` 的全部工作。

## 3. 本讲源码地图

本讲涉及三个源码文件，都位于 `src/` 下：

| 文件 | 编译层级 | 角色 | 是否有结构体内核 |
| --- | --- | --- | --- |
| `src/axi_join.sv` | Level 2 | 把两个 `AXI_BUS` 接口**直连**（纯连线） | 否，只有 `axi_join_intf` |
| `src/axi_cut.sv` | Level 2 | 在输入输出之间插入**一级寄存器**，切断所有组合路径 | 是，`axi_cut` + 两个 `_intf` 外壳 |
| `src/axi_multicut.sv` | Level 3 | 把 `NoCuts` 个 `axi_cut` **串联**，用于很长的总线 | 是，`axi_multicut` + 两个 `_intf` 外壳 |

需要特别留意两点（后面会反复用到）：

1. **`axi_join` 只有接口版**，没有结构体版的 `axi_join` 模块。因为它本质就是一组连线，结构体版只会是 `assign mst_req = slv_req;` 这种一句话，没有存在的必要。
2. **`axi_multicut` 是 Level 3**，因为它内部实例化了 Level 2 的 `axi_cut`。层级直接反映了「multicut 建立在 cut 之上」的依赖关系。

此外，`axi_cut` 内部依赖一个来自外部依赖 **common_cells**（版本 1.39.0）的模块 `spill_register`。它不在本仓库里，我们会在 4.2 节单独说明它的角色与行为。

## 4. 核心概念与源码讲解

### 4.1 axi_join：零成本直连

#### 4.1.1 概念说明

`axi_join` 回答的问题是：「我手里有一个 master 端的 `AXI_BUS` 接口，旁边有个 slave 端的 `AXI_BUS` 接口，我想把它们直接接起来，中间什么都不做。」

答案就是 `axi_join`：它是一个**纯粹的连接器**，内部没有任何逻辑、没有寄存器、没有选择器，就是把同名信号一对一连上。从功能上看，它等价于一根扁平排线。它存在的意义不是「做事」，而是**给这种直连取一个名字、封成一个模块**，让顶层例化的代码读起来语义清晰（`axi_join_intf` 一眼就能看出「这里只是接一下」），并且能在接口层面享受到 modport 的方向保护。

#### 4.1.2 核心流程

`axi_join` 的「流程」简单到可以一句话概括：

```
in (Slave 视图)  ──AXI_ASSIGN──>  out (Master 视图)
   五通道信号、valid、ready 全部同名直连
```

- 对 AW/W/AR（master 发出的请求方向）：`in` 上驱动的信号，原样送到 `out`。
- 对 B/R（slave 发出的响应方向）：`out` 上回来的信号，原样送回 `in`。
- 没有时钟、没有复位，纯组合（其实纯连线）。延迟为 0，面积几乎为 0。

#### 4.1.3 源码精读

整个模块只有一行实质代码：

模块声明，端口是 `AXI_BUS.Slave in` 与 `AXI_BUS.Master out`：

[src/axi_join.sv:19-24](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_join.sv#L19-L24) —— 注意它**只有接口版** `axi_join_intf`，端口用 modport 直接声明方向。

唯一一行实质逻辑就是 `AXI_ASSIGN` 宏（来自 `include/axi/assign.svh`，u2-l4 讲过），它把两个接口的同名字段一对一搬过去：

[src/axi_join.sv:24](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_join.sv#L24) —— `\`AXI_ASSIGN(out, in)` 等价于把约 43 根 AW/W/B/AR/R 信号逐根 `assign`。

模块里还有一段仿真期的断言（被 `// pragma translate_off` 包住，综合时会被工具忽略），检查两端宽度一致：

[src/axi_join.sv:26-35](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_join.sv#L26-L35) —— 注意第 31 行是 `<=`：ID 宽度允许「in 比 out 窄」，其它三维度（地址/数据/user）必须严格相等。这对应 AXI 里「ID 可以向宽扩展」的常规用法。

> **为什么没有结构体版的 `axi_join`？** 因为直连在结构体视图下就是 `assign mst_req = slv_req; assign slv_resp = mst_resp;`，写在调用处一行即可，封成模块反而啰嗦。所以本库只为它保留了接口版。这是「接口视图负责对外接线、结构体视图负责内核 datapath」分工的一个边角例证。

#### 4.1.4 代码实践

**实践目标**：确认 `axi_join` 在综合后是零逻辑的纯连线。

1. 打开 [src/axi_join.sv:19-37](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_join.sv#L19-L37)，确认模块体内除 `AXI_ASSIGN` 与仿真断言外没有任何 `always`、没有寄存器。
2. 在你熟悉的综合工具（如 Yosys / Synopsys DC）里把 `axi_join_intf` 例化进一个顶层（任意 `ADDR/DATA/ID/USER_WIDTH`），做一次 elaborate / 综合。
3. 观察综合后的网表或资源报告。

**需要观察的现象**：
- 该模块不应消耗任何 LUT/FF（断言那部分不在综合网表里）。
- `in` 与 `out` 之间应是直接的 wire 连接。

**预期结果**：综合后 `axi_join_intf` 的逻辑面积为 0，仅是连线重命名。

> 待本地验证：不同综合工具对「纯连线模块」的汇报口径不同，有的会把它折叠进父模块而单独不显示——这同样是「零逻辑」的表现。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `axi_join` 不需要 `clk_i` / `rst_ni` 端口？
**参考答案**：因为它内部没有任何触发器，也没有任何需要时钟驱动的逻辑，纯组合连线，所以不需要时钟与复位。对比 `axi_cut`（4.2 节）就需要这两个端口。

**练习 2**：如果 `in` 的 ID 宽度是 4、`out` 的 ID 宽度是 6，`axi_join` 能用吗？
**参考答案**：能。第 31 行断言是 `in.AXI_ID_WIDTH <= out.AXI_ID_WIDTH`，允许 in 更窄。AXI 中低位补零即可；反方向（in 比 out 宽）则会被断言拦下。

---

### 4.2 axi_cut：用 spill 寄存器切断组合路径

#### 4.2.1 概念说明

`axi_join` 把两端「同周期贯穿」地接起来，这在物理上意味着：master 端组合逻辑的输出，**在同一时钟周期内**就能影响到 slave 端组合逻辑的输入，反之亦然。当总线很长、两端逻辑都很深时，这条贯穿全链的组合路径就会成为关键路径，让你的设计跑不到目标频率。

`axi_cut` 解决的就是这个问题。它的模块注释一针见血：

> Breaks all combinatorial paths between its input and output.
> （切断其输入与输出之间的所有组合路径。）

做法是：给 AXI 的**每一个通道**（AW/W/B/AR/R）都插一级寄存器。这样信号从 slave 端口进、到 master 端口出，**必须穿过一个触发器**，跨过一个时钟沿——原来那条同周期贯穿的长路径，就被切成了两段更短的路径。代价是：多了一级延迟，多了一小块寄存器面积。这正是「用延迟/面积换时序裕量」的经典权衡。

> **直觉小结**：`join` 是一根线，`cut` 是在线中间塞了一个触发器。塞了触发器，信号就要等一拍，路径就短了，时序就好满足了。

#### 4.2.2 核心流程

`axi_cut` 的内部结构可以用下面这张「五通道各一级寄存器」的示意图理解：

```
              ┌──────────────── axi_cut ────────────────┐
 slv_req_i ──▶│ AW ─▶[spill_register]─▶ AW   ──▶ mst_req_o │
              │ W  ─▶[spill_register]─▶ W              │
              │ B  ◀─[spill_register]◀─ B              │
              │ AR ─▶[spill_register]─▶ AR             │
              │ R  ◀─[spill_register]◀─ R              │
 slv_resp_o ◀─│                              mst_resp_i │
              └─────────────────────────────────────────┘
   请求方向(AW/W/AR): slave ──▶ master
   响应方向(B/R):     slave ◀── master
```

关键点：

- **每通道一个 `spill_register`**：共 5 个，方向随通道而变（AW/W/AR 是 slave→master，B/R 是 master→slave）。
- **`Bypass` 开关**：每个通道都能单独决定要不要绕过这级寄存器。默认 `Bypass=0`（真插寄存器）；置 1 则该通道退化为直连（详见 4.2.3）。
- **握手依然合法**：`spill_register` 是一个完整的 valid/ready 握手寄存器，它会在切断组合路径的同时保证 AXI 握手协议不被破坏（valid 一旦拉高、在握手前不撤等铁律依然成立）。

**关于 `spill_register`（来自外部 common_cells）**：它是一个 1 深度的「带旁通的握手寄存器」，端口是标准的 `valid_i/ready_o/data_i` 与 `valid_o/ready_i/data_o`，参数有数据类型 `T` 和 `Bypass`。它的两个核心性质：

- `Bypass=1`：退化为纯连线（`assign`），不插寄存器、不切断路径——和 `axi_join` 一模一样。
- `Bypass=0`（默认）：把 payload 寄存，从而切断 `data_i→data_o` 与 `valid_i→valid_o` 的组合路径。

> **关于延迟与吞吐（重要）**：插入了寄存器，意味着一个 beat 从 slave 端口进到 master 端口出，需要跨过一个时钟沿——这是「切断路径」必然带来的延迟代价，也正是 `axi_cut` 与 `axi_join` 的本质区别。至于每一拍在稳态下的精确握手时序（是否允许 fall-through、首拍与后续拍的具体延迟），由 common_cells 里 `spill_register` 的实现决定，不在本仓库内；做 cycle-accurate 的分析时请以 common_cells 1.39.0 的 `spill_register.sv` 为准。对选型而言，你只需记住：**`cut` 切断组合路径、换取时序裕量，代价是延迟与少量面积**。

#### 4.2.3 源码精读

**（1）结构体内核 `axi_cut`**：端口是 `axi_req_t`/`axi_resp_t`（u2-l4 讲过的请求/响应结构体），外加时钟复位。参数区先定义 6 个 `Bypass` 开关（一个全局 `Bypass` + 每通道默认取它），再定义 5 个通道结构体类型和 req/resp 类型：

[src/axi_cut.sv:17-46](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cut.sv#L17-L46) —— 第 17–19 行就是那句承诺「切断所有组合路径」的注释；第 22–27 行可见 `BypassAw/W/B/Ar/R` 默认都等于全局 `Bypass`。

模块体就是 5 个 `spill_register` 实例，每个对应一个通道。以 AW 通道为例：

[src/axi_cut.sv:48-61](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cut.sv#L48-L61) —— `valid_i/ready_o/data_i` 接 slave 侧（`slv_req_i.aw_valid` / `slv_resp_o.aw_ready` / `slv_req_i.aw`），`valid_o/ready_i/data_o` 接 master 侧（`mst_req_o.aw_valid` / `mst_resp_i.aw_ready` / `mst_req_o.aw`），`.Bypass(BypassAw)` 控制是否旁通。其余四个通道结构完全对称，只是方向不同：

[src/axi_cut.sv:63-117](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cut.sv#L63-L117) —— W、AR 与 AW 同向（slave→master）；B、R 反向（master→slave），所以它们的 `valid_i` 接的是 `mst_resp_i.*`、`valid_o` 接的是 `slv_resp_o.*`。

> **`Bypass` 的妙用**：把 `Bypass=1`（或单独把某通道的 `BypassX=1`），那个通道的 `spill_register` 就退化为连线。极端情况下六个 `Bypass` 全置 1，整个 `axi_cut` 在功能上就退化成了一个 `axi_join`——但代码结构（5 个 spill 实例）依然保留。这让你能用一个参数在「插寄存器」与「不插」之间切换，便于在参数化设计里做时序实验。

**（2）接口外壳 `axi_cut_intf` / `axi_lite_cut_intf`**：内核只认结构体，但顶层往往用 `AXI_BUS` 接口。外壳干两件事：用 `AXI_TYPEDEF_*` 宏由位宽生成通道/req/resp 类型，再用 `AXI_ASSIGN_*` 宏在接口与结构体之间搬运。这是 u2-l4 讲过的「接口外壳 + 结构体内核」标准范式：

[src/axi_cut.sv:147-168](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cut.sv#L147-L168) —— 先 `typedef` 出 `aw_chan_t…axi_resp_t`，声明 `slv_req/mst_req/slv_resp/mst_resp` 四个结构体，用 `AXI_ASSIGN_TO_REQ`/`AXI_ASSIGN_FROM_RESP` 把 `in` 接口拆进 `slv_req`/`slv_resp`、把 `mst_req`/`mst_resp` 拼回 `out` 接口。

[src/axi_cut.sv:170-191](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cut.sv#L170-L191) —— 然后例化结构体内核 `axi_cut`，把那四个结构体接上去。`axi_lite_cut_intf`（[src/axi_cut.sv:214-290](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cut.sv#L214-L290)）结构完全一样，只是用 `AXI_LITE_*` 宏和 `AXI_LITE` 接口，并且复用同一个 `axi_cut` 内核（Lite 的通道结构体字段更少而已）。

#### 4.2.4 代码实践

**实践目标**：亲手验证「`axi_cut` 切断了输入到输出的组合路径」，并对比 `axi_join`。

这是源码阅读 + 仿真型实践，步骤如下：

1. **画路径（分析）**：对照 [src/axi_cut.sv:48-61](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cut.sv#L48-L61) 的 AW 通道 `spill_register`，画一条路径 `slv_req_i.aw` → `i_reg_aw` → `mst_req_o.aw`。标出中间有一个触发器。再画 `axi_join` 的同一条路径（[src/axi_join.sv:24](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_join.sv#L24)），中间是空连线。两者对比，`cut` 多了一个寄存器节点。
2. **写最小测试台（可选，动手）**：仿照 u3-l3 里 `tb_axi_lite_regs` 的三明治结构，搭一个最小拓扑：`axi_lite_rand_master` → `axi_lite_cut_intf`（DUT）→ `axi_lite_rand_slave`，配一个 `axi_scoreboard` 自检，跑若干次随机读写。
3. **加对比实验**：把 DUT 换成 `axi_lite_join`（即 `join`），用相同激励再跑一次。

**需要观察的现象**：
- 两种 DUT 下，scoreboard 都应报告无错（功能上 cut 与 join 都是透传，数据不被改变）。
- 从波形上看，`cut` 的 `mst_req_o.aw_valid` 相对 `slv_req_i.aw_valid` 有寄存器带来的延迟关系；`join` 则完全同步。
- 综合后，`cut` 会多出 5 类通道寄存器（FF），`join` 是 0。

**预期结果**：功能自检双绿；时序上 cut 的路径被切成两段、join 保持贯穿。

> 待本地验证：具体延迟拍数取决于 `spill_register` 实现；本实践只要求你观察到「cut 有寄存器、join 没有」这一结构性差异即可。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `axi_cut` 需要每个通道**单独**一个 `spill_register`，而不是把五个通道的数据拼成一个大寄存器？
**参考答案**：因为五个通道各自独立握手、时序各异（AW 可能正等 W，B 可能正等 R）。每个通道用自己的 valid/ready 控制自己的寄存器，才能在切断路径的同时不破坏各通道独立的握手语义。合并会让一个通道的反压错误地影响其它通道。

**练习 2**：把 `axi_cut` 的 `BypassAw=1` 而其它 `BypassX=0`，会发生什么？
**参考答案**：只有 AW 通道被旁通（退化为连线，组合路径不被切断），W/B/AR/R 四个通道仍插寄存器。这在「只有地址通道时序紧张、数据通道还好」时是有用的精细调节手段。

---

### 4.3 axi_multicut：多级寄存器缓解长总线时序

#### 4.3.1 概念说明

当一段 AXI 总线**非常长**（比如在大型 SoC 里跨越半个芯片），单插一级 `axi_cut` 可能还不够——切断后的两段路径里，可能仍有一段太长、跑不到频率。这时候你需要**连续插好几级寄存器**。

`axi_multicut` 就是「N 个 `axi_cut` 串联」的封装。模块注释说得很直白：

> These can be used to relax timing pressure on very long AXI busses.
> （可用于缓解非常长的 AXI 总线上的时序压力。）

你只需要告诉它「我要几级」（`NoCuts`），它就用一个 `generate` 循环把那么多份 `axi_cut` 首尾相连，并处理好首尾的端口接线。这样你不用手写一长串 `axi_cut` 例化。

#### 4.3.2 核心流程

`axi_multicut` 把 `NoCuts` 个 `axi_cut` 串成一条链：

```
slv ─[cut #0]─[cut #1]─ … ─[cut #(NoCuts-1)]─ mst
```

内部用两个数组 `cut_req[0..NoCuts]`、`cut_resp[0..NoCuts]` 当这条链上的「节点」：

- 链的起点（index 0）接 slave 端口；终点（index NoCuts）接 master 端口。
- 第 `i` 个 `axi_cut` 的输入是节点 `i`、输出是节点 `i+1`，于是 N 个 cut 恰好把 `0..NoCuts` 这 `NoCuts+1` 个节点串起来。

还要处理两个**边界情况**：

- **`NoCuts == 0`**：退化情形，一个寄存器都不插，直接把 slave 连到 master——这时 `axi_multicut` 等价于 `axi_join`。
- **`NoCuts == 1`**：等价于单个 `axi_cut`。

另外有一个值得注意的设计决定：`axi_multicut` 内部每个 `axi_cut` 都**硬编码** `.Bypass(1'b0)`（不旁通）。也就是说，multicut 不像 cut 那样暴露 per-channel 旁通开关——你要么插整级、要么不插，没有「只切某个通道」的精细选项。

#### 4.3.3 源码精读

参数与端口：核心参数是 `NoCuts`（默认 1），端口同样是 `axi_req_t`/`axi_resp_t` 结构体：

[src/axi_multicut.sv:18-41](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_multicut.sv#L18-L41) —— 第 20 行注释点明用途「缓解长总线时序压力」；第 22 行 `NoCuts = 32'd1`。

退化情形 `NoCuts == 0`，直接 `assign` 打通，连结构体内核都不例化：

[src/axi_multicut.sv:43-46](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_multicut.sv#L43-L46) —— 这正是「multicut 在 NoCuts=0 时等价于 join」的代码证据。

正常情形：声明长度为 `NoCuts+1` 的 req/resp 数组作为链上节点，把 slave 接到最低下标，然后用 `for` generate 循环例化 `NoCuts` 个 `axi_cut`：

[src/axi_multicut.sv:47-80](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_multicut.sv#L47-L80) —— 第 49–50 行声明 `cut_req[NoCuts:0]`/`cut_resp[NoCuts:0]`；第 52–54 行把 slave 接到 `index 0`；第 57–75 行的循环里，第 `i` 个 cut 吃 `cut_req[i]`、吐 `cut_req[i+1]`（resp 反向），注意第 59 行 `.Bypass(1'b0)` 是写死的；第 77–79 行把 master 接到最高下标 `NoCuts`。

> **为什么 multicut 是 Level 3？** 因为它的 `generate` 循环里实例化了 `axi_cut`（Level 2）。被依赖的文件必须先编译，所以 multicut 排在 cut 的下一层。这从 `src_files.yml` / `Bender.yml` 的层级分组里可以直接看到（cut 在 Level 2、multicut 在 Level 3）。

和 `axi_cut` 一样，`axi_multicut` 也有接口外壳 `axi_multicut_intf` 与 `axi_lite_multicut_intf`，范式完全相同（typedef 生成类型 + assign 搬运 + 例化结构体内核），参数名换成了大写的 `NUM_CUTS`：

[src/axi_multicut.sv:132-148](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_multicut.sv#L132-L148) —— `.NoCuts(NUM_CUTS)` 把外壳的大写参数传给内核。

#### 4.3.4 代码实践

**实践目标**：通过参数变化，亲眼看到 `axi_multicut` 在 `NoCuts=0/1/N` 三种取值下的等价关系。

这是纯源码阅读型实践（不需要仿真器）：

1. 打开 [src/axi_multicut.sv:43-80](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_multicut.sv#L43-L80)。
2. 分别取 `NoCuts = 0`：走 `gen_no_cut` 分支，只有两句 `assign`（[L43-46](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_multicut.sv#L43-L46)）→ 等价 `axi_join`。
3. 取 `NoCuts = 1`：循环跑一次，例化 1 个 `axi_cut`，节点数组长度 2 → 等价单个 `axi_cut`。
4. 取 `NoCuts = 3`：例化 3 个 `axi_cut` 串联，节点数组长度 4 → 三级流水。

**需要观察/记录的现象**：填写下表（待本地验证列请你用静态阅读判断）：

| `NoCuts` | 实例化的 `axi_cut` 个数 | 节点数组长度 | 等价于 |
| --- | --- | --- | --- |
| 0 | 0（走 assign 分支） | 不使用 | `axi_join` |
| 1 | 1 | 2 | 单个 `axi_cut` |
| 3 | 3 | 4 | 3 级 `axi_cut` 链 |

**预期结果**：链上 `axi_cut` 个数 = `NoCuts`，节点数组长度 = `NoCuts + 1`，与上表一致。

> **何时该从 `axi_cut` 改用 `axi_multicut`**：当一次综合报告显示，即便插了一级 cut，某段总线的关键路径仍不满足频率约束，且这段总线物理上确实很长（跨距大、扇出多）时，就改用 `axi_multicut` 加到 2 级或更多。简言之：**一级 cut 不够用时升级为 multicut**。反之，短总线、时序本就富裕，用 `axi_join` 即可，不必无谓地堆寄存器增加延迟和面积。

#### 4.3.5 小练习与答案

**练习 1**：`axi_multicut` 的 `NoCuts` 设成 0 和设成 1，综合出的电路有什么区别？
**参考答案**：`NoCuts=0` 走 `gen_no_cut` 分支，只有两句 `assign`，零寄存器，等价 `axi_join`；`NoCuts=1` 例化一个 `axi_cut`，有 5 类通道寄存器，等价单个 `axi_cut`。

**练习 2**：为什么 `axi_multicut` 不像 `axi_cut` 那样提供 per-channel 的 `Bypass`？
**参考答案**：multicut 的定位是「在长总线上均匀地加若干整级流水」，目的是把一条长路径切成等长的若干段；暴露 per-channel 旁通会让每级的切断程度不一致、复杂化时序分析，也与「均匀切片」的意图相悖。需要精细控制某通道是否旁通时，直接用单个 `axi_cut` 即可。

## 5. 综合实践

把本讲三个原语串起来做一个综合任务：

**任务**：为一段「主设备 → 长距离布线 → 从设备」的总线设计连接方案。

1. 先用 `axi_join_intf` 把 master 直连到 slave，作为**基准**（baseline）。
2. 跑一次综合（可用 `make elab.log` 或本库的 `scripts/synth.sh`，见 u1-l4），记录关键路径长度 / 是否满足目标频率。
3. 若不满足，在 master 与 slave 之间换插一个 `axi_cut_intf`，再次综合，对比关键路径是否变短、是否新增了寄存器（FF）。
4. 若插一级仍不满足，改用 `axi_multicut_intf` 设 `NUM_CUTS=2`（甚至 3），再综合，记录关键路径与寄存器数量的变化曲线。
5. 写一段结论：在你的目标频率下，最少需要几级寄存器；并说明每多一级带来的「路径变短 vs 延迟/面积增加」的权衡。

**预期收获**：亲手体验「join → cut → multicut」这条升级路径，理解它们不是互斥的三个模块，而是「要不要切、切几级」这一个连续决策的三档选择。

> 待本地验证：具体关键路径数值取决于你的工艺库、布线长度与目标频率，本任务重在观察「切断组合路径后关键路径变短、寄存器变多」的趋势，而非某个绝对数字。

## 6. 本讲小结

- `axi_join` 是**纯连线**：内部只有一句 `AXI_ASSIGN`，零逻辑、零延迟，只有接口版（`axi_join_intf`）。
- `axi_cut` 给五个通道**各插一级 `spill_register`**，切断输入到输出的所有组合路径，代价是延迟与少量面积；支持 per-channel 的 `Bypass` 精细旁通。
- `spill_register` 来自外部 common_cells，`Bypass=1` 退化为连线、`Bypass=0` 寄存 payload 切断路径；cycle-accurate 行为以 common_cells 实现为准。
- `axi_multicut` 用 `generate` 循环把 `NoCuts` 个 `axi_cut` 串联，`NoCuts=0` 退化为 join、`NoCuts=1` 等价单个 cut；内部 `Bypass` 写死为 0。
- 三者是「要不要切组合路径、切几级」这一个决策的三个档位：不切用 join，切一级用 cut，切多级用 multicut。
- 三者都遵循「接口外壳 + 结构体内核」范式：内核只认 `req_t`/`resp_t`，外壳用 `AXI_TYPEDEF_*` + `AXI_ASSIGN_*` 在 `AXI_BUS` 与结构体之间搬运。

## 7. 下一步学习建议

本讲建立了「连接原语」的基础。后续建议：

- **继续本单元（U4）**：下一讲 u4-l2 讲 `axi_modify_address` 与 `axi_id_prepend`——它们在转发时**改写**地址或 ID，是比 join/cut「更有动作」的连接器，依赖关系同样建立在 u2-l4 的宏体系上。
- **理解 cut 在大模块里的真实用途**：`axi_mux`（u5-l3）和 `axi_demux` 内部大量实例化 `spill_register` 来切断多路选择的关键路径，这正是本讲 `axi_cut` 思想的工业化应用。阅读时可以把本讲的「五通道各一级寄存器」心智模型直接套上去。
- **进入流控与缓冲（U7）**：当你需要的不是「切断路径」而是「吸收突发、平滑背压」时，就该从 cut 升级到 `axi_fifo`（u7-l1）——它本质是「每通道一个可深度的 FIFO」，是 cut 的容量扩展版。
