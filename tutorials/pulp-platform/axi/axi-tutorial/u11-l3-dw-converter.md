# axi_dw_converter：自动选择数据宽度转换器

## 1. 本讲目标

学完本讲，读者应该能够：

- 说清 `axi_dw_converter` 在数据宽度转换族里的角色——它是一个**编译期分发器（dispatcher）**，自身不写任何转换逻辑。
- 根据「slave 端口宽度」与「master 端口宽度」的大小关系，判断它会例化哪一条分支（等宽直通 / upsize / downsize）。
- 理解为什么等宽情况下能用零开销的 `assign` 直通，且不会因两端类型不同而报错。
- 看懂接口外壳 `axi_dw_converter_intf` 为何要声明两套 W/R 通道类型，并会用扁平宽度参数把 `AXI_BUS` 接口接到内核。
- 在异构片上网络里，用 `axi_dw_converter`（或其 `_intf` 外壳）把两个数据宽度不同的子网安全接起来。

本讲是 U11 单元（数据宽度转换）的收口篇，承接 u11-l1（`axi_dw_downsizer`）与 u11-l2（`axi_dw_upsizer`）。前面两讲已经把「宽↔窄」两个方向的内核拆得很细，本讲只回答一个问题：**当两端宽度关系尚不确定、或希望上层代码写一份就能通吃三种情况时，该用哪个模块？** 答案就是 `axi_dw_converter`。

## 2. 前置知识

在进入正文前，先确认几个前面讲义已经建立、本讲会反复用到的概念：

- **slave 端口 / master 端口的方向**：在本库里，一个夹在总线中间的模块，其 **slave 端口（slv）面向上游发起方（AXI Master）**，其 **master 端口（mst）面向下游目标（AXI Slave）**。所以「slave 端口宽」=「上游发起方用的总线宽」。
- **`req_t` / `resp_t` 结构体内核范式**：可综合内核只认请求/响应结构体（u2-l4），接口外壳负责在 `AXI_BUS` 与结构体之间搬数据。
- **转换比 conv_ratio**：宽度转换时一个宽 beat 拆成多少个窄 beat（或反之），详见 u11-l1。
- **`AxiMaxReads`**：在途（outstanding）读事务最大数，决定内核并行引擎个数。
- **编译层级 Level**：Bender 用依赖拓扑给文件分层（u1-l2）。`axi_dw_downsizer` 与 `axi_dw_upsizer` 都在 Level 2，`axi_dw_converter` 在 Level 3——它依赖这两个 Level 2 内核。

如果你对上面任何一项还陌生，建议先回看对应讲义。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 角色 |
| --- | --- |
| [src/axi_dw_converter.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_converter.sv) | 本讲主角。包含结构体内核 `axi_dw_converter`（编译期分发器）与接口外壳 `axi_dw_converter_intf`。 |
| [src/axi_dw_downsizer.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_downsizer.sv) | 宽→窄内核（u11-l1 已精读）。本讲只引用其头部说明与参数契约，不重复内部 FSM。 |
| [src/axi_dw_upsizer.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_upsizer.sv) | 窄→宽内核（u11-l2 已精读）。本讲同样只引用头部说明与参数契约。 |
| [Bender.yml](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Bender.yml) | 用 Level 0–6 层级证明「converter = 纯组合、无新协议逻辑」。 |
| [test/tb_axi_dw_downsizer.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_dw_downsizer.sv) | 验证 downsize 分支的测试台——注意它例化的正是 `axi_dw_converter_intf`。 |
| [test/tb_axi_dw_upsizer.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_dw_upsizer.sv) | 验证 upsize 分支的测试台——同样例化 `axi_dw_converter_intf`。 |

一句话定位：本库没有 `tb_axi_dw_converter.sv`，converter 是**借** downsize / upsize 两个测试台间接验证的。这一点会在 4.4 节详细解释。

## 4. 核心概念与源码讲解

### 4.1 为什么需要一个「统一入口」

#### 4.1.1 概念说明

回顾 u11-l1 与 u11-l2：

- `axi_dw_downsizer` 只能接「宽 slave 端口 → 窄 master 端口」。
- `axi_dw_upsizer` 只能接「窄 slave 端口 → 宽 master 端口」。

但在真实异构网络里，上层设计者往往**在写 RTL 时还不知道两端宽度谁大谁小**——子网的数据宽度可能来自一组顶层参数，组合后可能相等、可能上转、也可能下转。如果让上层用 `if` 自己挑模块，就得手写三份例化代码，极易写错或漏掉等宽分支。

`axi_dw_converter` 正是为消除这种样板代码而生的「门面」：它对外暴露与两个内核**完全一致**的参数与端口，内部根据宽度关系自动挑一个内核例化；等宽时甚至连内核都不例化，直接两根线接通。README 把它描述为「任意数据宽度之间的转换器」：

> [`axi_dw_converter`](src/axi_dw_converter.sv) — A data width converter between AXI interfaces of **any** data width.

它的存在也是本库「组合优于配置」哲学（u1-l1）的又一个范例：把「挑哪个内核」这件纯判断工作，单独封进一个零逻辑的薄壳，而把真正的转换算法留在两个职责单一的内核里。

#### 4.1.2 核心流程

converter 的全部行为可以用一句话概括：

```
比较 AxiMstPortDataWidth 与 AxiSlvPortDataWidth
  ├─ 相等  → 两根 assign 直通（零开销）
  ├─ mst > slv（窄→宽）→ 例化 axi_dw_upsizer
  └─ mst < slv（宽→窄）→ 例化 axi_dw_downsizer
```

注意三件事：

1. **这是编译期（elaboration-time）判断**，不是运行期判断。比较的是两个 `parameter int unsigned`，在综合时就已经定死，所以三条分支里**只有一条会被展开（elaborate）**，另外两条对工具完全不可见。
2. **选中哪条分支，就等价于直接用那个内核**——参数是原样透传的（见 4.2）。
3. converter 不引入任何额外的状态机、FIFO 或握手逻辑，因此**不会改变两个内核的时序与死锁边界**。

#### 4.1.3 源码精读

先看 converter 内核模块的端口——它和两个内核的端口**逐字段一致**，这是「无缝替换」的前提：

[axi_dw_converter.sv:18-44](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_converter.sv#L18-L44) — 内核模块声明：`AxiMaxReads`、`AxiSlvPortDataWidth`、`AxiMstPortDataWidth`、`AxiAddrWidth`、`AxiIdWidth` 五个标量参数，外加 `aw_chan_t`、`mst_w_chan_t`、`slv_w_chan_t`、`b_chan_t`、`ar_chan_t`、`mst_r_chan_t`、`slv_r_chan_t`、`axi_mst_req_t`、`axi_mst_resp_t`、`axi_slv_req_t`、`axi_slv_resp_t` 一组通道/请求/响应类型，最后是 `clk_i/rst_ni/slv_req_i/slv_resp_o/mst_req_o/mst_resp_i` 这一组端口。

需要特别留意的是：**W 通道与 R 通道各有两个类型**（`mst_w_chan_t`/`slv_w_chan_t`、`mst_r_chan_t`/`slv_r_chan_t`）。这是数据宽度转换族的「签名」——因为两端数据宽度不同，W/R 这两条携带数据的通道在两侧的字段宽度也不同，所以必须用两个类型分别描述。AW/B/AR 通道不携带变长数据载荷，所以共用同一组类型。这一设计在三个文件里完全一致。

接着是三条分发分支的源码，4.2、4.3 两节会分别精读。

#### 4.1.4 代码实践

**实践目标**：用静态阅读验证「converter 端口 == 两个内核端口」。

**操作步骤**：

1. 打开 `src/axi_dw_converter.sv` 第 18–44 行的 `axi_dw_converter` 模块声明。
2. 打开 `src/axi_dw_downsizer.sv` 第 22–48 行、`src/axi_dw_upsizer.sv` 第 21–47 行的模块声明。
3. 逐参数对照三者的 `parameter` 列表与端口列表。

**需要观察的现象**：三者的参数名、类型参数名、端口名完全一一对应（`AxiMaxReads`、`AxiSlvPortDataWidth`、`AxiMstPortDataWidth`、`AxiAddrWidth`、`AxiIdWidth`、`aw_chan_t`……`axi_slv_resp_t`，以及 `clk_i/rst_ni/slv_req_i/slv_resp_o/mst_req_o/mst_resp_i`）。

**预期结果**：converter 的端口是两个内核端口的**精确并集**（实际上三者完全相同），这就是为什么 converter 能把参数原样「转发」给被选中的内核。

> 本实践为源码阅读型，无需运行仿真即可确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 converter 需要两个 W 通道类型（`mst_w_chan_t` 与 `slv_w_chan_t`），却只需要一个 AW 通道类型（`aw_chan_t`）？

**参考答案**：W 通道携带可变宽的 `data`/`strb` 字段，两端宽度不同，故需两个类型；AW 通道只携带地址、ID、突发控制等定长字段，不随数据宽度变化，故两端共用一个 `aw_chan_t` 即可。R 通道同理需要两个类型（`mst_r_chan_t`/`slv_r_chan_t`）。

**练习 2**：如果直接用 `axi_dw_upsizer` 去接「宽 slave → 窄 master」的链路，会发生什么？

**参考答案**：upsizer 内部按「窄→宽」假设工作（例如把多拍窄 W 合并成一拍宽 W），方向接反后数据拼装/地址计算都会出错。这正是需要 converter 按宽度关系自动分发的原因。

---

### 4.2 编译期三路分发：upsize / downsize 分支

#### 4.2.1 概念说明

converter 内核用三段「模块级 `if`」做分发。这里的 `if` 不是 `always` 里的运行期判断，而是 SystemVerilog 的**生成块条件（conditional generate）**——条件必须是常量表达式，综合时只有满足条件的那个 `begin...end` 块会被展开成真实电路，其余两个块整体消失。因此：

- 三条分支对工具而言是**互斥**的，不会同时存在；
- 哪怕某条分支里的代码在「另一种宽度关系下」是非法的（例如等宽 `assign` 在宽度不等时会类型不匹配），也**不会**导致编译失败——因为那条分支根本不会被展开。

这是 converter 设计中最巧妙、也最容易被初学者忽略的一点。

#### 4.2.2 核心流程

设 \( W_s = \text{AxiSlvPortDataWidth} \)（slave 端口宽度，面向上游 master）、\( W_m = \text{AxiMstPortDataWidth} \)（master 端口宽度，面向下游 slave），则三条分支的判据为：

\[
\begin{cases}
W_m = W_s & \Rightarrow \text{gen\_no\_dw\_conversion（直通）} \\
W_m > W_s & \Rightarrow \text{gen\_dw\_upsize（窄 }\to\text{ 宽）} \\
W_m < W_s & \Rightarrow \text{gen\_dw\_downsize（宽 }\to\text{ 窄）}
\end{cases}
\]

注意方向读法：

- upsize 分支对应 `mst` 端口比 `slv` 端口**宽**——即上游 master 发来的窄数据被合并成宽带宽送给下游 slave（`axi_dw_upsizer` 的注释：*Connects a narrow master to a wider slave*）。
- downsize 分支对应 `mst` 端口比 `slv` 端口**窄**——即上游 master 发来的宽数据被拆成窄带宽送给下游 slave（`axi_dw_downsizer` 的注释：*Connects a wide master to a narrower slave*）。

无论命中哪条，被例化的内核都把 converter 的全部参数原样吃下，端口也一一对接。

#### 4.2.3 源码精读

upsize 分支：当 master 端口更宽时，例化 `axi_dw_upsizer`，参数逐字段透传，端口名一一对应：

[axi_dw_converter.sv:51-79](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_converter.sv#L51-L79) — `gen_dw_upsize` 块：例化 `i_axi_dw_upsizer`，`.clk_i(clk_i)/.rst_ni(rst_ni)/.slv_req_i(slv_req_i)/.slv_resp_o(slv_resp_o)/.mst_req_o(mst_req_o)/.mst_resp_i(mst_resp_i)`，参数列表与 converter 顶部声明完全同序透传。

downsize 分支：当 master 端口更窄时，例化 `axi_dw_downsizer`，结构完全对称：

[axi_dw_converter.sv:81-109](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_converter.sv#L81-L109) — `gen_dw_downsize` 块：例化 `i_axi_dw_downsizer`，端口与参数同样逐字段透传。

两个内核的头部说明可以互为参照：

[axi_dw_downsizer.sv:14-21](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_downsizer.sv#L14-L21) — downsizer 的描述：*Connects a wide master to a narrower slave*；并声明不支持 WRAP 突发（回 SLVERR），多拍 FIXED 突发也不支持。

[axi_dw_upsizer.sv:14-19](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_upsizer.sv#L14-L19) — upsizer 的描述：*Connects a narrow master to a wider slave*；同样不支持 WRAP 突发。

> 重要推论：因为 converter 命中哪条分支就等价于那个内核，所以两个内核「不支持 WRAP、部分不支持 FIXED、用 `resp_precedence` 合并响应」等**所有协议限制与行为都原样继承**到 converter（u11-l1、u11-l2 已详述）。converter 本身不增不减任何协议能力。

#### 4.2.4 代码实践

**实践目标**：用具体宽度配置，确认 converter 命中 downsize 分支。

**操作步骤**：

1. 设想配置 `AxiSlvPortDataWidth = 64`、`AxiMstPortDataWidth = 32`（即 64 位主域接到 32 位从域，这正是本讲综合实践的设定）。
2. 代入判据 \( W_m = 32 < W_s = 64 \)，应命中 `gen_dw_downsize`。
3. 打开 `test/tb_axi_dw_downsizer.sv`，看它的默认参数与 DUT 例化。

**需要观察的现象**：`tb_axi_dw_downsizer` 的默认参数恰好是 `TbAxiSlvPortDataWidth = 64`、`TbAxiMstPortDataWidth = 32`（见下文 4.4 节引用），而它例化的 DUT 是 `axi_dw_converter_intf`。

**预期结果**：说明这个测试台实际上就是在「64 位主域 → 32 位从域」下、经 converter 间接驱动 downsize 内核——因此「用 converter」与「直接用 downsize」功能等价。

> 本实践为源码阅读型，结论可从源码直接确认，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：converter 用的是模块级 `if`（生成块条件），而不是 `always_ff` 里的 `if`。这两者最本质的区别是什么？

**参考答案**：模块级 `if` 的条件必须是**编译期常量**，只有满足条件的分支会被展开成电路，其余分支对综合工具完全不存在；`always_ff` 里的 `if` 是**运行期**判断，所有分支都会被综合成实际的多路选择逻辑。converter 要的是前者——宽度关系在综合时已定死，不需要运行期硬件。

**练习 2**：假设有人误把 `axi_dw_upsizer` 直接用在了「宽→窄」链路上，converter 能帮你避免这种错误吗？

**参考答案**：能。converter 会根据实际宽度关系自动选 `gen_dw_downsize`，使用者无需也不能手动指定内核方向，从而从源头杜绝「内核方向接反」这类错误。

---

### 4.3 等宽直通：零开销的 passthrough

#### 4.3.1 概念说明

当 \( W_m = W_s \) 时，根本没有数据宽度转换要做——两端每个 beat 的数据、strobe、地址都对齐一致。converter 此时连内核都不例化，只用两根 `assign` 把 slave 端口的请求/响应与 master 端口直连：

```systemverilog
assign mst_req_o  = slv_req_i ;
assign slv_resp_o = mst_resp_i;
```

这是**零开销**的：没有寄存器、没有 FIFO、没有组合逻辑（连一个 `spill_register` 都没有），时序与面积和「直接把两根总线焊在一起」完全等价。

#### 4.3.2 核心流程

等宽分支的关键不是「它做了什么」，而是「它为什么不会报错」。注意端口类型：

- `slv_req_i` 的类型是 `axi_slv_req_t`，`mst_req_o` 的类型是 `axi_mst_req_t`。
- 一般情况下，二者内部的 W/R 数据字段宽度不同（`slv_data_t` ≠ `mst_data_t`），**结构体不可直接赋值**。

那为什么 `assign mst_req_o = slv_req_i` 在等宽分支里合法？原因有二：

1. **只有等宽时这条分支才会被展开**。当宽度不等时，`gen_no_dw_conversion` 的守护条件为假，整块代码对编译器不可见，类型不匹配的赋值永远不会被检查到。
2. **当宽度相等时，两侧类型在结构上完全一致**。以 4.4 节的 `axi_dw_converter_intf` 为例，`slv_data_t` 与 `mst_data_t` 都按各自宽度参数生成；宽度相等时两者位宽相同、字段相同，结构体赋值天然兼容。

#### 4.3.3 源码精读

等宽分支源码极简：

[axi_dw_converter.sv:46-49](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_converter.sv#L46-L49) — `gen_no_dw_conversion` 块：两行 `assign` 把 `slv_req_i` 直接连到 `mst_req_o`、`mst_resp_i` 直接连到 `slv_resp_o`，无任何实例化。

这条分支存在的工程价值：

- 让上层代码可以**无条件地**插入 converter，而不用担心等宽时白白付出面积/延迟代价；
- 也让「是否需要转换」这件事成为 converter 的内部决策，上层无需关心。

#### 4.3.4 代码实践

**实践目标**：验证等宽直通不会引入任何逻辑。

**操作步骤**：

1. 假想一份顶层设计，把 `AxiSlvPortDataWidth` 与 `AxiMstPortDataWidth` 都设为 64。
2. 在脑中走一遍 converter 的三条 `if`：等宽分支命中，其余两分支不展开。
3. （可选）若有综合工具，把 converter 在等宽配置下做一次 elaborate。

**需要观察的现象**：综合网表里 converter 应**没有任何寄存器或组合单元**，`slv_req_i` 与 `mst_req_o` 在网表中被直接合并为同一根线。

**预期结果**：等宽时 converter 是「透明」的。**待本地验证**（综合网表的形态取决于具体工具与优化等级，建议用你自己的 EDA 流程确认）。

#### 4.3.5 小练习与答案

**练习 1**：为什么等宽分支的 `assign` 不能写在「宽度可能不等」的通用模块里？

**参考答案**：当宽度不等时，`axi_slv_req_t` 与 `axi_mst_req_t` 的 W/R 数据字段宽度不同，结构体赋值会触发类型不匹配错误。converter 靠「等宽时才展开这条分支」规避了这一点。

**练习 2**：等宽直通没有 `spill_register`。如果你恰好想在等宽链路上切断组合路径，该怎么做？

**参考答案**：在 converter 之外另加一个 `axi_cut`（u4-l1）或 `axi_multicut`。converter 的设计目标是「宽度转换的零开销门面」，时序修复的职责不属于它。

---

### 4.4 axi_dw_converter_intf：接口外壳与验证策略

#### 4.4.1 概念说明

和大多数内核一样，`axi_dw_converter` 也是结构体内核，端口是 `req_t`/`resp_t`，使用时要自己声明一堆通道类型。为了省去这套样板，库提供了接口外壳 `axi_dw_converter_intf`：它对外用 `AXI_BUS.Slave` / `AXI_BUS.Master` 两个接口（u2-l3），内部用扁平宽度参数（`AXI_SLV_PORT_DATA_WIDTH`、`AXI_MST_PORT_DATA_WIDTH`、`AXI_ID_WIDTH`、`AXI_ADDR_WIDTH`、`AXI_USER_WIDTH`、`AXI_MAX_READS`）自动生成所有通道类型，再用 `AXI_TYPEDEF_*` / `AXI_ASSIGN_*` 宏（u2-l4）在接口与结构体之间搬数据，最后例化结构体内核。

外壳特别值得注意的一点：它**用两个不同的宽度参数分别生成 slv 侧与 mst 侧的 W/R 类型**，从而把「两端数据宽度不同」这一事实编码进类型系统。

#### 4.4.2 核心流程

外壳的内部数据流：

```
AXI_BUS.Slave slv  ──AXI_ASSIGN_TO_REQ──▶  slv_req  ┐
                                                     ├─ axi_dw_converter ─┐
AXI_BUS.Master mst ──AXI_ASSIGN_FROM_REQ─▶ mst_req  ┘                    │
                                                                         │
AXI_BUS.Master mst ──AXI_ASSIGN_TO_RESP── mst_resp ◀─ axi_dw_converter ◀┤
AXI_BUS.Slave slv  ──AXI_ASSIGN_FROM_RESP▶ slv_resp ◀───────────────────┘
```

外壳生成的类型分两组：

| 类型 | 宽度来源 | 用途 |
| --- | --- | --- |
| `aw_chan_t` / `ar_chan_t` / `b_chan_t` | 与数据宽度无关 | 两端共用 |
| `slv_w_chan_t` / `slv_r_chan_t` | `AXI_SLV_PORT_DATA_WIDTH` | slave 端口（上游 master）侧数据 |
| `mst_w_chan_t` / `mst_r_chan_t` | `AXI_MST_PORT_DATA_WIDTH` | master 端口（下游 slave）侧数据 |

于是 `slv_req_t` 用 `slv_w_chan_t`，`mst_req_t` 用 `mst_w_chan_t`，二者结构体在宽度不同时天然不同——这与 4.3 节「等宽时才直通」的设计严丝合缝。

#### 4.4.3 源码精读

外壳的端口与参数列表：

[axi_dw_converter.sv:118-130](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_converter.sv#L118-L130) — `axi_dw_converter_intf` 声明：扁平参数 `AXI_ID_WIDTH`、`AXI_ADDR_WIDTH`、`AXI_SLV_PORT_DATA_WIDTH`、`AXI_MST_PORT_DATA_WIDTH`、`AXI_USER_WIDTH`、`AXI_MAX_READS`，端口为 `AXI_BUS.Slave slv` 与 `AXI_BUS.Master mst`。

类型生成：注意 `mst_data_t`/`mst_strb_t` 与 `slv_data_t`/`slv_strb_t` 用不同宽度，并据此生成两套 W/R 通道：

[axi_dw_converter.sv:132-149](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_converter.sv#L132-L149) — typedef 区：先声明 `id_t/addr_t/user_t`，再用 `AXI_MST_PORT_DATA_WIDTH` 生成 `mst_data_t/mst_strb_t`、用 `AXI_SLV_PORT_DATA_WIDTH` 生成 `slv_data_t/slv_strb_t`，随后 `AXI_TYPEDEF_*` 产出 `aw_chan_t`、`mst_w_chan_t`、`slv_w_chan_t`、`b_chan_t`、`ar_chan_t`、`mst_r_chan_t`、`slv_r_chan_t`，最后打包成 `mst_req_t/mst_resp_t/slv_req_t/slv_resp_t`。

接口↔结构体搬运，再例化内核：

[axi_dw_converter.sv:156-188](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_converter.sv#L156-L188) — 四个 `AXI_ASSIGN_*` 宏把 `AXI_BUS` 接口与 `slv_req/slv_resp/mst_req/mst_resp` 结构体互连，随后用扁平参数例化 `axi_dw_converter`（`AxiSlvPortDataWidth`/`AxiMstPortDataWidth` 等于外壳参数），把宽度分发决策交给内核。

**验证策略：converter 没有专属测试台**。本库只有 `tb_axi_dw_downsizer` 与 `tb_axi_dw_upsizer`，而它们例化的 DUT 都是 `axi_dw_converter_intf`：

[tb_axi_dw_downsizer.sv:21-22,145-157](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_dw_downsizer.sv#L145-L157) — downsize 测试台默认 `TbAxiSlvPortDataWidth = 64`、`TbAxiMstPortDataWidth = 32`，DUT 例化 `axi_dw_converter_intf`（`.slv(master), .mst(slave)`）。由于 64 > 32，converter 内部命中 `gen_dw_downsize`，所以这个 TB 实际就是在「经 converter」验证 downsize 内核。

[tb_axi_dw_upsizer.sv:21-22,115-127](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_dw_upsizer.sv#L115-L127) — upsize 测试台默认 `TbAxiSlvPortDataWidth = 32`、`TbAxiMstPortDataWidth = 64`，DUT 同样是 `axi_dw_converter_intf`；32 < 64 命中 `gen_dw_upsize`。

两个 TB 的自检黄金模型（`tb_axi_dw_pkg.sv` 里的 `axi_dw_downsizer_monitor` / `axi_dw_upsizer_monitor`）在 master 与 slave 两侧同时监听，逐 beat 比对 AW/W/B/AR/R，用 `8'hxx` 通配容忍合法不确定位（u11-l1、u11-l2 已述）。**等宽分支没有专门的 TB**——它只是一行 `assign`，由综合与等价性论证覆盖。

> 结论：converter 的三个分支都被现有 TB 覆盖——downsize/upsize 各一个 TB，等宽分支靠「无逻辑」论证。这就是本库不需要 `tb_axi_dw_converter.sv` 的原因。

#### 4.4.4 代码实践

**实践目标**：跑一遍现有的 downsize 测试台，体验「经 converter 间接验证内核」。

**操作步骤**：

1. 在仓库根目录执行（参见 u1-l4 的构建方式）：`make sim-tb_axi_dw_downsizer.log`。
2. 打开生成的日志，定位到仿真器输出的统计行。

**需要观察的现象**：日志结尾应出现 monitor 打印的 `Tests Expected / Conducted / Failed` 三行统计，以及脚本层 `Errors: 0,` 判据行（u1-l4）。

**预期结果**：`Tests Failed: 0` 且 `Errors: 0`，说明在「64 位主域 → 32 位从域」配置下，converter 命中 downsize 分支后功能正确。**待本地验证**（仿真是否可用取决于本机是否装有 vsim/VCS 等工具与 Bender 环境；若无可降级为下文的源码阅读实践）。

**降级实践（纯阅读，无需工具）**：对照 4.4.3 节引用的两段 TB 源码，确认两个 TB 都把 `axi_dw_converter_intf` 当 DUT，并据此说明「直接用 downsize」与「经 converter 用 downsize」在功能上为何等价。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `axi_dw_converter_intf` 要为 W 和 R 各生成两个通道类型，而 AW/AR/B 只生成一个？

**参考答案**：只有 W/R 通道携带随数据宽度变化的数据/strobe 字段；AW/AR/B 的字段（地址、ID、len、size、resp 等）与数据宽度无关，所以两端共用同一类型即可。这与 4.1 节练习 1 是同一事实的接口侧体现。

**练习 2**：如果要给 converter 加一个等宽分支的回归测试，最低成本的做法是什么？

**参考答案**：复制 `tb_axi_dw_downsizer`，把 `TbAxiSlvPortDataWidth` 与 `TbAxiMstPortDataWidth` 改成相等，并改用一个对等宽不敏感的黄金模型（或直接用 `axi_sim_mem` + scoreboard，u3-l2）做自检，验证 converter 在等宽下透明直通。

---

## 5. 综合实践

**任务**：用 `axi_dw_converter_intf` 连接一个 64 位主域（上游 AXI Master 数据宽度 64）与一个 32 位从域（下游 AXI Slave 数据宽度 32），确认 converter 内部例化了 downsize 分支，并论证其功能等价于直接使用 `axi_dw_downsizer`。

**参考做法**：

1. **确定方向**。上游 master 数据宽度 64，接 converter 的 **slave 端口**；下游 slave 数据宽度 32，接 converter 的 **master 端口**。故配置应为 `AXI_SLV_PORT_DATA_WIDTH = 64`、`AXI_MST_PORT_DATA_WIDTH = 32`。

2. **复用现成 TB**。本库已经提供了恰好这个配置的测试台 `tb_axi_dw_downsizer`（[tb_axi_dw_downsizer.sv:145-157](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_dw_downsizer.sv#L145-L157)），其 DUT 就是 `axi_dw_converter_intf`，上游接随机 master、下游接随机 slave，并配 `tb_axi_dw_pkg::axi_dw_downsizer_monitor` 做双端口黄金模型自检。直接运行（待本地验证）：
   ```
   make sim-tb_axi_dw_downsizer.log
   ```
   日志若出现 `Tests Failed: 0` / `Errors: 0`，即说明该配置通过。

3. **论证等价性**。对照三条分发分支（4.2、4.3 节）：本配置下 \( W_m = 32 < W_s = 64 \)，converter 命中 [axi_dw_converter.sv:81-109](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_converter.sv#L81-L109) 的 `gen_dw_downsize`，把全部参数原样透传给 `i_axi_dw_downsizer`，端口一一对接。由于 converter 在这条分支里**没有任何额外逻辑**（既无寄存器也无 FIFO），所以「经 converter 用 downsize」与「直接用 `axi_dw_downsizer`」在功能与时序上完全等价——这正是 converter 作为零开销门面的设计意图。

4. **（选做）反向验证**。把配置改为 `AXI_SLV_PORT_DATA_WIDTH = 32`、`AXI_MST_PORT_DATA_WIDTH = 64`，跑 `make sim-tb_axi_dw_upsizer.log`，确认此时命中 `gen_dw_upsize` 分支，upsize 内核被例化。

**预期产出**：一段结论，说明在 64→32 配置下 converter 命中 downsize 分支、与直接用 downsize 功能等价；并能说出 converter 相对直接用内核的两点好处——(a) 上层无需手写方向判断，(b) 等宽时零开销直通。

## 6. 本讲小结

- `axi_dw_converter` 是数据宽度转换族的**统一入口**，本质是一个**零逻辑的编译期分发器**，按 `AxiMstPortDataWidth` 与 `AxiSlvPortDataWidth` 的大小关系三选一。
- 三条分支互斥展开：等宽 → 两根 `assign` 直通（零开销）；master 更宽 → 例化 `axi_dw_upsizer`；master 更窄 → 例化 `axi_dw_downsizer`。
- 被选中分支把 converter 的全部参数**原样透传**给内核，converter 端口与两个内核端口逐字段一致，因此 converter 命中哪条就等价于哪个内核，协议限制（不支持 WRAP 等）也一并继承。
- 等宽直通之所以合法，靠的是「只有等宽时这条分支才被展开」——避免了两端结构体类型不同时的赋值冲突。
- `axi_dw_converter_intf` 是接口外壳，用两个不同宽度参数生成 slv/mst 两侧的 W/R 通道类型，再用 `AXI_TYPEDEF_*` / `AXI_ASSIGN_*` 宏在 `AXI_BUS` 与结构体之间搬数据。
- converter 没有专属测试台：downsize/upsize 两个分支由 `tb_axi_dw_downsizer` / `tb_axi_dw_upsizer`（均例化 `axi_dw_converter_intf`）间接覆盖，等宽分支靠「无逻辑」论证。

## 7. 下一步学习建议

- **横向对照 ID 宽度转换**：`axi_iw_converter`（u10-l3）与本讲的 converter 是同一思路的「自动分发门面」，只不过它分发的是 ID 宽度关系（remap / serialize / prepend / 直通）。两讲对照阅读，能巩固「编译期分发器」这一通用模式。
- **进入异构网络综合**：U15-l4「异构网络设计实战」会把 `axi_xbar + axi_cdc + axi_dw_converter + axi_iw_converter + axi_isolate` 组装成真实跨域网络，本讲的 converter 正是其中负责数据宽度子网互连的胶水模块。
- **补一个等宽回归**：若你负责维护本库，可参考 4.4.5 练习 2，为等宽分支补一个最小回归测试，把目前仅靠论证覆盖的分支也纳入 CI（u16-l4）。
- **继续阅读源码**：重读 [src/axi_dw_converter.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_converter.sv) 全文（仅 190 行），并结合 [Bender.yml](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Bender.yml) 中 Level 2（downsizer/upsizer）与 Level 3（converter）的层级关系，体会「纯组合、无新协议逻辑」如何反映在依赖层级里。
