# axi_delayer：随机延迟通道

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 `axi_delayer` 为什么「能延迟但不破坏 AXI 协议」——它把每个通道包进一个合规的 valid/ready 流式原语 `stream_delay`。
- 区分四个延迟参数 `FixedDelayInput / FixedDelayOutput / StallRandomInput / StallRandomOutput` 的作用，并理解为何把通道分成请求方向（AW/AR/W）与响应方向（B/R）两组。
- 看懂 `axi_delayer`（结构体内核）与 `axi_delayer_intf`（接口外壳）的标准「外壳 + 内核」范式。
- 读懂测试台 `tb_axi_delayer`，并知道如何把 delayer 插进 `rand_master → DUT → axi_sim_mem` 拓扑，用它放大时序 bug。

## 2. 前置知识

本讲承接两篇讲义：

- **u4-l1（axi_join / axi_cut / axi_multicut）**：你已经知道 `axi_cut` 用 spill_register **确定性地**切断组合路径、多加 1 拍延迟，目的是「用延迟换时序」。本讲的 `axi_delayer` 形态相似（都是给通道插延迟），但目的完全不同：它要的是**可变 / 随机**延迟，用来在验证里制造握手抖动，而不是为了综合时序收敛。
- **u3-l2（随机主从、scoreboard 与 sim_mem）**：你已经熟悉「rand_master 产生随机激励 → DUT → axi_sim_mem，scoreboard 在 master 侧旁路自检」的标准自检拓扑。本讲的实践任务就是把 delayer 插进这条链路。

还需要回忆两个 AXI 铁律（来自 u1-l3 / u2-l3）：

1. **valid 在握手前不可撤、载荷须稳定**：一旦 `valid` 拉高，在 `valid && ready` 同时为高的那次握手发生之前，`valid` 与载荷都不能变。
2. **五通道彼此独立**：AW/W/B/AR/R 各自握手，跨通道没有强制的时序锁定（例如 W 拍允许在 AW 握手之前到达）。

delayer 的全部价值，就在于「在不违反上述铁律的前提下，随机扰动每个通道各自的握手时刻」。

## 3. 本讲源码地图

| 文件 | 作用 | 编译层级 |
|------|------|----------|
| [src/axi_delayer.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv) | 定义 `axi_delayer`（结构体内核）与 `axi_delayer_intf`（接口外壳）。内核为五通道各实例化一个 `stream_delay`。 | Level 2 |
| [test/tb_axi_delayer.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_delayer.sv) | delayer 自检测试台：master 端发 200 次写、slave 端按序回 200 个 B 响应，全程开启请求方向随机停顿。 | test target |

> 说明：本讲反复提到的 `stream_delay` 是外部依赖 `common_cells`（`Bender.yml` 锁定 `1.39.0`）提供的原语，**不在本仓库源码内**，因此无法给出本仓库内的永久链接。我们只描述它在 `axi_delayer` 里的**可观察接口与契约**，不臆测其门级实现。

## 4. 核心概念与源码讲解

### 4.1 定位：delayer 是「验证用的随机抖动器」，不是时序修复器

#### 4.1.1 概念说明

很多协议 bug 只有在握手「不太顺」时才暴露：master 一口气把 `valid` 顶到天花板、slave 每拍都 `ready`，这种理想波形下，DUT 内部的状态机往往走的是最宽的大路；可一旦某拍 `ready` 突然变低、或某拍 `valid` 延迟几拍才到，DUT 可能就踩进死锁、丢拍或乱序的坑里。

`axi_delayer` 就是用来制造这种「不太顺」的工具：它串在一条 AXI 链路上，对每个通道施加**可配置、甚至随机**的延迟/停顿，让原本贴合的握手出现时序抖动，从而把隐藏的协议违例、死锁、保序错误逼出来。

它和 u4-l1 的 `axi_cut` 形似神不似：

| 维度 | `axi_cut`（u4-l1） | `axi_delayer`（本讲） |
|------|--------------------|------------------------|
| 目的 | 综合时序收敛：切断组合路径 | 验证：制造握手抖动暴露 bug |
| 延迟 | 确定地 +1 拍 | 可固定，更可**随机** |
| 何时用 | 流片前的真实设计 | 仿真激励、回归测试 |
| 是否可综合 | 是 | 是（标题就写了 Synthesizable），但价值在仿真 |

#### 4.1.2 核心流程

把 delayer 看作一根「会卡顿的延长线」：

```
master 端发起 ──► [ axi_delayer ] ──► slave 端接收
   slv_req_i          (每通道一个 stream_delay)      mst_req_o
   slv_resp_o  ◄──                          ◄──      mst_resp_i
```

- 请求方向（AW/AR/W）：从 `slv_req_i` 进，从 `mst_req_o` 出。
- 响应方向（B/R）：从 `mst_resp_i` 进，从 `slv_resp_o` 出。
- 每个通道都被一个 `stream_delay` 单独「卡」一下，卡多久由参数决定。

关键不变量：**载荷内容一字不改、通道内不重排、不丢拍**。delayer 只动「握手什么时候发生」，不动「握手搬了什么」。

### 4.2 五通道独立延迟与 Input / Output 划分

#### 4.2.1 概念说明

`axi_delayer` 的延迟参数有四个，名字里带 `Input` / `Output`：

| 参数 | 默认值 | 作用于 | 方向 |
|------|--------|--------|------|
| `FixedDelayInput`  | 1 | AW、AR、W | 请求方向（slave→master） |
| `FixedDelayOutput` | 1 | B、R | 响应方向（master→slave） |
| `StallRandomInput`  | 0 | AW、AR、W | 请求方向 |
| `StallRandomOutput` | 0 | B、R | 响应方向 |

为什么按 Input/Output 分组？因为请求和响应走的是相反方向：

- **Input（输入）组**：AW/AR/W 是上游 master「喂进」delayer slave 端口的请求，朝下游 master 端口流。
- **Output（输出）组**：B/R 是下游回送回来的响应，朝上游 slave 端口流。

通常我们想给「发起侧」（请求）和「回送侧」（响应）分别配抖动强度——例如只压请求、不压响应，或反之。分两组就给了这个自由度。

而**五通道彼此独立**是另一层更重要的独立性：即便请求组共用一组参数，AW、AR、W 三个通道各自的随机停顿是**独立随机**的。于是 AW 可能早到、W 可能晚到、或反过来——这种跨通道到达顺序的随机错位，正是 delayer 最能「挖 bug」的地方（很多 DUT 假设「W 一定紧跟 AW」，delayer 会打脸这种假设）。

#### 4.2.2 核心流程

设某通道输入端在某拍发生握手（`valid_i && ready_o` 为真），输出端对应握手发生在第 $t_{out}$ 拍。则：

\[
t_{out} - t_{in} \;\ge\; \text{FixedDelay}
\]

当 `StallRandom = 1` 时，$t_{out}$ 还会叠加一个**随机**的停顿拍数。把固定延迟与随机停顿分开理解很有用：

- **FixedDelay（延迟）**：抬高**延迟（latency）**，但不一定降低**吞吐（throughput）**——若内部是流水化的，稳态下仍可每拍吞吐一拍。
- **StallRandom（随机停顿）**：随机地让若干拍「卡住」，直接**降低吞吐**、抬高平均延迟。

\[ \text{平均吞吐} \;\propto\; \frac{1}{1 + \mathbb{E}[\text{随机停顿拍数}]} \]

（上式为概念性表达，精确分布取决于 `stream_delay` 的随机源，其门级实现在外部 `common_cells` 中，本仓库不包含。）

#### 4.2.3 源码精读

模块头部声明了 7 个**类型参数**（五个通道结构体 + req/resp）和 4 个延迟参数：

延迟参数定义见 [src/axi_delayer.sv:L28-L31](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L28-L31)——这就是「固定延迟 + 随机停顿」×「输入/输出」四元组的来源。

请求方向的三个通道实例化（以 AW 为例）把 `StallRandomInput / FixedDelayInput` 喂给 `stream_delay`：

[src/axi_delayer.sv:L42-L56](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L42-L56) 把 slave 端的 AW 通道（`slv_req_i.aw / .aw_valid`、`slv_resp_o.aw_ready`）接进 `stream_delay`，输出端接到 master 端的 AW（`mst_req_o.aw / .aw_valid`、`mst_resp_i.aw_ready`）。AR 通道（[L58-L72](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L58-L72)）与 W 通道（[L74-L88](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L74-L88)）结构完全一致，三处都用 `Input` 组参数。

响应方向的两个通道则换成 `Output` 组参数。以 B 通道为例：

[src/axi_delayer.sv:L90-L104](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L90-L104) 注意方向反过来了：输入是 `mst_resp_i.b`（下游回的响应），输出是 `slv_resp_o.b`（朝上游送），参数用的是 `StallRandomOutput / FixedDelayOutput`。R 通道见 [L106-L120](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L106-L120)。

把五个实例的「参数来源」画成表，就一目了然：

| 通道实例 | 方向 | `StallRandom` | `FixedDelay` |
|----------|------|---------------|--------------|
| `i_stream_delay_aw` | slave→master | Input | Input |
| `i_stream_delay_ar` | slave→master | Input | Input |
| `i_stream_delay_w`  | slave→master | Input | Input |
| `i_stream_delay_b`  | master→slave | Output | Output |
| `i_stream_delay_r`  | master→slave | Output | Output |

#### 4.2.4 代码实践

**实践目标**：亲手验证「五通道独立延迟」会改变跨通道到达顺序。

**操作步骤（源码阅读 + 参数推演型）**：

1. 打开 [src/axi_delayer.sv:L42-L120](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L42-L120)，确认 AW、AR、W 三处用的是 `StallRandomInput`，而 B、R 用的是 `StallRandomOutput`。
2. 设想一次写事务：master 同一拍发出 `aw_valid` 和 `w_valid`。
3. 因为 AW 与 W 走**两个独立**的 `stream_delay` 实例、各自随机停顿，回答：下游 master 端口**可能**先看到 `w_valid` 还是先看到 `aw_valid`？

**需要观察的现象 / 预期结果**：理论上两种顺序都可能出现（AXI 允许 W 早于 AW）。这正是 delayer 的「挖 bug」价值——若某 DUT 错误假设「W 必在 AW 之后」，在无 delayer 的理想波形下永远不暴露，加上 delayer 后会偶发失败。**待本地验证**：可用第 4.5 节的测试台加波形观察 `mst_req_o.aw_valid` 与 `mst_req_o.w_valid` 的相对先后。

#### 4.2.5 小练习与答案

**练习 1**：若只配 `FixedDelayInput=3, StallRandomInput=0`，请求通道的吞吐会被拉低吗？
**答案**：不一定。FixedDelay 抬高的是**延迟**；若 `stream_delay` 内部流水化，稳态下仍可每拍吞吐一拍，吞吐基本不变，只是每拍都晚 3 拍到达。降低吞吐的是 `StallRandom`。

**练习 2**：为什么 B、R 共用一组 `Output` 参数，而不是各自单独配？
**答案**：B 与 R 同属「下游回送的响应」方向，时序特性相近，分组配置已足够区分请求/响应两侧的抖动强度；再细分成每通道一参数会让端口表臃肿，收益有限——这也是「组合优于配置」哲学的体现。

### 4.3 stream_delay 原语与「延迟却不破坏协议」的机制

#### 4.3.1 概念说明

`axi_delayer` 自己不算延迟，真正的延迟逻辑在 `stream_delay` 里。从 [src/axi_delayer.sv:L42-L56](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L42-L56) 的实例化可以读出它的**对外契约**（这是它在本仓库里唯一可观察的形态）：

- 两个参数：`payload_t`（载荷类型，如 `aw_chan_t`）、`FixedDelay`、`StallRandom`。
- 两端各一组 valid/ready/payload：输入端 `valid_i / ready_o / payload_i`，输出端 `valid_o / ready_i / payload_o`，外加 `clk_i / rst_ni`。

也就是说，`stream_delay` 是一个**标准的 valid/ready 流式模块**：一端吞 stream、一端吐 stream，中间插延迟。模块注释也明说它是 "Synthesizable module that (randomly) delays AXI channels"（见 [src/axi_delayer.sv:L16](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L16)）。

> 重要：`stream_delay` 的门级实现（如何用移位寄存器做固定延迟、如何用随机源做停顿）位于外部 `common_cells` 依赖中，本仓库未包含，故此处只讲**契约**，不讲内部电路。

#### 4.3.2 核心流程：为什么包一层就能保住协议？

AXI 的握手铁律是「valid 在握手前不可撤、载荷稳定」。`axi_delayer` 之所以敢在每个通道插一个延迟器而不破坏协议，靠的是两点：

1. **`stream_delay` 本身是合规的 stream 原语**：它在输入端和输出端都遵守 valid/ready 的合法时序（不会提前撤 valid、不会在 valid 期间改载荷）。于是把 AXI 通道的 `valid/ready/payload` 直接接到 `stream_delay` 两端，握手合法性在 delayer 两个端口处都被保留。
2. **载荷原样透传、不重排不丢拍**：从实例化看，`payload_i → payload_o` 只是穿过 `stream_delay`，没有被改写、丢弃或重排。所以事务内容与通道内顺序都保持不变。

一句话总结：**delayer 改变的是「握手在时间轴上的位置」，不是「握手搬了什么」**。因此对功能正确的 DUT，加 delayer 后结果应当不变；对功能有缺陷的 DUT，delayer 会把缺陷晃出来。

#### 4.3.3 源码精读

回到 AW 实例 [src/axi_delayer.sv:L42-L56](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L42-L56)，注意端口映射体现的「载荷透传 + 双向握手」结构：

- 载荷：`slv_req_i.aw → payload_i`，`payload_o → mst_req_o.aw`（只过延迟，不改值）。
- 上游握手：`slv_req_i.aw_valid → valid_i`，`ready_o → slv_resp_o.aw_ready`。
- 下游握手：`mst_resp_i.aw_ready → ready_i`，`valid_o → mst_req_o.aw_valid`。

这组映射说明 `stream_delay` 把上游的 valid 与下游的 ready 「解耦」到自己的两侧——上游不必立刻看到下游的 ready，下游也不必立刻看到上游的 valid，中间隔了延迟/停顿逻辑。这正是它能制造抖动又不违规的根因。

#### 4.3.4 代码实践

**实践目标**：在波形上确认「载荷稳定」未被延迟器破坏。

**操作步骤**：

1. 跑 `make sim-axi_delayer.log`（见 4.5 节），用 `-voptargs=+acc` 打开信号可见性（测试台末行留有提示 `// vsim -voptargs=+acc work.tb_axi_delayer`，见 [test/tb_axi_delayer.sv:L122](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_delayer.sv#L122)）。
2. 在波形里追踪 `mst_req_o.aw_valid` 从拉高到握手（`aw_valid && aw_ready`）的整段区间。
3. 观察该区间内 `mst_req_o.aw`（含 addr/id）是否恒定不变。

**预期结果**：尽管 `aw_valid` 可能因随机停顿被拉长多拍，期间载荷应保持稳定——这验证了「延迟不破坏协议」。**待本地验证**（需要 vsim/Questa 仿真环境）。

#### 4.3.5 小练习与答案

**练习 1**：能否用 `axi_delayer` 给一条已经存在死锁的链路「解死锁」？
**答案**：不能。delayer 只能制造抖动、抬高延迟，不具备缓冲容量或重排能力，不能解开死锁；相反，它常被用来**诱发**潜在死锁以暴露问题。

**练习 2**：`stream_delay` 的 `payload_t` 参数为什么必须从 `axi_delayer` 往下传（如 `aw_chan_t`）？
**答案**：因为不同通道的载荷结构不同（AW 含 addr/atop，W 含 data/strb，B 含 resp……），`stream_delay` 是参数化原语，必须由上层告诉它「这次延迟的是哪种载荷」，才能正确声明内部寄存器宽度。

### 4.4 axi_delayer_intf：接口外壳

#### 4.4.1 概念说明

`axi_delayer`（结构体内核）的端口是扁平的 `axi_req_t / axi_resp_t` 结构体——这是内核 datapath 的友好形态。但在顶层连线和测试台里，人们更想直接用 `AXI_BUS` 接口（带 modport 方向保护）。于是库里照例提供了 `axi_delayer_intf` 这个「接口外壳」，遵循 u2-l4 讲过的「接口外壳 + 结构体内核」范式。

#### 4.4.2 核心流程

外壳做三件事：

1. 用 `AXI_TYPEDEF_*` 宏声明五个通道结构体与 req/resp 类型；
2. 用 `AXI_ASSIGN_*` 宏在 `AXI_BUS` 接口与 req/resp 结构体之间搬数据；
3. 例化内核 `axi_delayer`，只面对结构体。

注意外壳的参数名是**全大写**（`AXI_ID_WIDTH` 等），且每个都带默认值——注释点明这是 Synopsys DC 的要求（见 [src/axi_delayer.sv:L128](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L128)），是综合工具兼容性的常见约定。

#### 4.4.3 源码精读

类型声明 [src/axi_delayer.sv:L150-L156](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L150-L156) 用 `AXI_TYPEDEF_*` 宏生成 `aw_chan_t … axi_req_t / axi_resp_t`。

接口↔结构体互连 [src/axi_delayer.sv:L161-L165](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L161-L165)：`AXI_ASSIGN_TO_REQ / FROM_RESP` 把 slave 端 `AXI_BUS` 与 `slv_req/slv_resp` 互连，`AXI_ASSIGN_FROM_REQ / TO_RESP` 处理 master 端。

内核例化 [src/axi_delayer.sv:L167-L186](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L167-L186)：把大写参数映射到内核的小写参数（`STALL_RANDOM_INPUT → StallRandomInput` 等），内核只看到结构体端口。

外壳还带一组编译期断言 [src/axi_delayer.sv:L190-L195](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L190-L195)（包在 `pragma translate_off / ifndef VERILATOR` 里，仅仿真生效），强制各宽度 ≥ 1，防止退化配置。

#### 4.4.4 代码实践

**实践目标**：理解「外壳只是翻译层」。

**操作步骤**：对照 [src/axi_delayer.sv:L167-L186](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L167-L186)，数一下从 `AXI_BUS.Slave slv` 到内核 `slv_req_i` 之间经过了几层 `AXI_ASSIGN`。

**预期结果**：两层——`slv → slv_req`（`AXI_ASSIGN_TO_REQ`）进内核，内核输出 `mst_req → mst`（`AXI_ASSIGN_FROM_REQ`）。功能上等价于直接连线，零额外逻辑。

#### 4.4.5 小练习与答案

**练习**：为什么内核不直接用 `AXI_BUS` 接口、非要拆出结构体内核？
**答案**：结构体内核可参数化、可做数组、便于在更大的 datapath（如 xbar）里复用与组合；接口外壳只是为顶层连线与测试台提供便利。这套「内核面向 struct、外壳面向 interface」的分工是全库统一范式（见 u2-l4）。

### 4.5 测试台 tb_axi_delayer 解读与运行

#### 4.5.1 概念说明

`tb_axi_delayer` 是 delayer 的**自检**测试台：它不接任何复杂 DUT，只用 `axi_test::axi_driver`（u3-l1）在两端扮演 master 与 slave，跑 200 次写事务穿越 delayer，验证「在请求方向随机停顿下，事务仍能正确往返」。它的拓扑是：

```
axi_master_drv ── axi_master_dv ── AXI_ASSIGN ── axi_master (AXI_BUS)
                                                └─► delayer.slv
                     delayer.mst ── axi_slave (AXI_BUS) ── AXI_ASSIGN ── axi_slave_dv ── axi_slave_drv
```

> 命名易混：`axi_master_drv` 连到 delayer 的 **slave** 端口（它是发起方/master）；`axi_slave_drv` 连到 delayer 的 **master** 端口（它是应答方/slave）。这是「驱动器名按它扮演的角色命名、而非按所连 DUT 端口命名」的惯例。

#### 4.5.2 核心流程

测试台三个并发 `initial` 块：

1. **时钟/复位发生器** [test/tb_axi_delayer.sv:L63-L75](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_delayer.sv#L63-L75)：`tCK=1ns`，先复位（`rst<=0`）再拉高，然后循环产时钟直到 `done`。
2. **master 侧** [test/tb_axi_delayer.sv:L77-L101](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_delayer.sv#L77-L101)：循环 200 次，随机化 `ax_beat`（[L84-L96](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_delayer.sv#L84-L96)），`send_aw` + `send_w`（`w_data` 固定 `'hcafebabe`）；随后 `repeat(200) recv_b` 收回 200 个 B 响应，最后置 `done=1`。
3. **slave 侧** [test/tb_axi_delayer.sv:L103-L121](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_delayer.sv#L103-L121)：循环 200 次 `recv_aw` + `recv_w`，把 `ax_id` 推进 `b_id_queue`；循环结束后按队列顺序 `send_b` 回送响应。

这里**没有 scoreboard**，正确性是结构性的：master 按序发、slave 按序收、slave 按 master 发的 id 顺序回 B、master 按序收 B。若 delayer 在随机停顿下丢拍或乱序，收发就会对不上、仿真卡死或断言失败。

注意一个细节：delayer 实例 [test/tb_axi_delayer.sv:L46-L58](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_delayer.sv#L46-L58) 只配了 `FIXED_DELAY_INPUT(0)` 与 `STALL_RANDOM_INPUT(1)`——即**只压请求方向、固定延迟为 0**；响应方向用的是 intf 默认值（`STALL_RANDOM_OUTPUT=0, FIXED_DELAY_OUTPUT=1`）。所以这个测试台重点验证「请求方向随机停顿」下 delayer 自身的正确性。

另外，[test/tb_axi_delayer.sv:L86-L92](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_delayer.sv#L86-L92) 用 `` `ifdef XSIM `` 区分了两种随机化写法（`std::randomize` vs `ax_beat.randomize()`），仅为兼容 Xilinx 仿真器在有限范围内的随机化行为差异。

#### 4.5.3 源码精读

delayer 例化与驱动器绑定见 [test/tb_axi_delayer.sv:L46-L61](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_delayer.sv#L46-L61)：注意 `slv(axi_master)`、`mst(axi_slave)` 的接线方向，以及两个 driver 的 `TA=200ps, TT=700ps`（满足 u3-l1 讲过的 `0≤TA<TT<tCK`，这里 `tCK=1ns=1000ps`）。

接口三明治见 [test/tb_axi_delayer.sv:L31-L44](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_delayer.sv#L31-L44)：`AXI_BUS_DV`（带 clk，给 driver 与断言用）与 `AXI_BUS`（可综合，给 DUT 用）用 `AXI_ASSIGN` 桥接，正是 u2-l3/u3-l3 的标准三明治。

#### 4.5.4 代码实践

**实践目标**：把 delayer 测试台跑起来，确认随机停顿下事务正确往返。

**操作步骤**：

1. 在仓库根目录执行 `make sim-axi_delayer.log`（Makefile 把 `axi_delayer` 列为可仿真目标之一，见 `Makefile` 第 24 行；`scripts/run_vsim.sh` 对 `axi_delayer` 走最简单的 `call_vsim tb_axi_delayer`，无参数矩阵，见 [scripts/run_vsim.sh:L48-L50](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L48-L50)）。
2. 打开生成的 `sim-axi_delayer.log`，查找 `Errors:` 统计行（u1-l4 讲过的判据）。

**需要观察的现象 / 预期结果**：日志应出现 `Errors: 0,`，且 master 侧打印 200 行 `AXI AW: addr ...`、slave 侧能完成全部 200 次回 B。若 delayer 丢拍/乱序，仿真会因 `recv_aw/recv_b` 等待超时而卡住或报错。**待本地验证**（需 vsim/Questa 与 Bender 拉取 `common_cells` 依赖）。

#### 4.5.5 小练习与答案

**练习 1**：这个测试台没有 scoreboard，怎么知道它「通过」了？
**答案**：靠结构性收发对齐——master 发 200、slave 收 200、slave 按序回 200 个 B、master 收 200 个 B，全部完成且 `done=1`，且日志 `Errors: 0,`。若中途丢拍/乱序，`recv_*` 会无限等待或断言失败，仿真不会正常结束。

**练习 2**：如何用这个测试台也压一压**响应方向**的随机停顿？
**答案**：把 delayer 实例化 [test/tb_axi_delayer.sv:L46-L58](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_delayer.sv#L46-L58) 加上 `STALL_RANDOM_OUTPUT(1)`，重新仿真，观察 B 响应在随机停顿下仍能被 master 正确收回。

## 5. 综合实践

把 delayer 接进 u3-l2 的标准自检拓扑，亲手感受「它怎么放大 bug」。

**任务**：搭建 `axi_rand_master → axi_delayer → axi_sim_mem`，并在 master 侧挂 `axi_scoreboard` 自检，对比「有 delayer / 无 delayer」两种配置下 scoreboard 是否都通过。

**建议步骤（基于已有测试台改造）**：

1. 以 `test/tb_axi_delayer.sv` 的三明治接线为模板，但把 slave 端的 `axi_driver` 换成 u3-l2 讲过的 `axi_sim_mem`（无限忠实存储，作为下游从端）。
2. master 端把 `axi_driver` 换成 `axi_rand_master`（自动随机激励），并按 u3-l2 的做法挂 `axi_scoreboard`（旁路监听 master 侧，维护字节级黄金模型并比对 R/B）。
3. delayer 配置：先 `STALL_RANDOM_INPUT=0, STALL_RANDOM_OUTPUT=0`（近似无延迟基线），跑一轮确认 scoreboard 通过；再改成 `STALL_RANDOM_INPUT=1, STALL_RANDOM_OUTPUT=1`，用不同 `sv_seed`（u1-l4 讲过的随机种子回归）多跑几轮。
4. 因为 `axi_sim_mem` 本身是功能正确的忠实存储，delayer 又不改载荷/不丢拍，所以**预期两种配置下 scoreboard 都应通过**。

**解释其价值**：基线通过只说明「DUT（这里是 sim_mem）功能对」；加 delayer 后用多种子回归仍通过，才说明「DUT 在握手抖动、跨通道到达顺序错位等恶劣时序下依然稳健」。这正是 delayer 的核心用途——它不验证「能不能跑」，而验证「在恶劣时序下还能不能跑」。如果你把 sim_mem 换成一个**真实的有缺陷的 DUT**（例如某 demux/mux 的 W 通道处理假设了 W 必在 AW 之后），delayer 很可能就在某种子下把它晃失败。**待本地验证**（需自行编写该测试台）。

## 6. 本讲小结

- `axi_delayer`（Level 2）为 AW/AR/W/B/R 五个通道各实例化一个外部 `common_cells` 的 `stream_delay`，对通道施加**可固定、可随机**的延迟/停顿，价值在于验证而非时序收敛——这是它和 u4-l1 `axi_cut` 的根本区别。
- 通道按方向分两组：请求方向 AW/AR/W 用 `*Input` 参数，响应方向 B/R 用 `*Output` 参数；五通道延迟彼此独立，会随机错动跨通道到达顺序。
- 「延迟却不破坏协议」靠两点：`stream_delay` 是合规的 valid/ready 流式原语（两端都守握手铁律），且载荷原样透传、不重排不丢拍——delayer 只改「握手的时间位置」，不改「握手搬了什么」。
- `FixedDelay` 主要抬高**延迟**，`StallRandom` 才真正拉低**吞吐**。
- `axi_delayer`（结构体内核）+ `axi_delayer_intf`（接口外壳，大写参数带默认值以满足 Synopsys DC）遵循全库统一的「外壳 + 内核」范式。
- 测试台 `tb_axi_delayer` 是 delayer 自检：200 次写在请求方向随机停顿下往返，无 scoreboard、靠结构性收发对齐判通过；它只压请求方向，可扩展为压响应方向。

## 7. 下一步学习建议

- **下一讲 u5-l1（axi_demux_simple 与 axi_demux）**：进入多路复用族。建议把本讲的 delayer 当作「测试增强器」——在学 demux 时，往 demux 的输入或输出插一个 delayer，观察 select 路由在抖动下是否仍正确。
- **u7-l1（axi_fifo 与 spill_register 缓冲）**：对比 delayer 与 fifo——fifo 提供真正的缓冲容量（多拍在途），delayer 只是延迟/停顿，深度有限。理解两者的吞吐-面积取舍。
- **延伸阅读**：`stream_delay` 的门级实现（移位寄存器固定延迟 + 随机停顿源）在 `common_cells` 仓库中，建议结合本讲契约去读其源码，补全「延迟如何具体实现」这一环。
