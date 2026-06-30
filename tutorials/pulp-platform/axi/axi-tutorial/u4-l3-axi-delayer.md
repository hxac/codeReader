# axi_delayer：随机延迟通道

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 `axi_delayer` 为什么「能延迟但不破坏 AXI 协议」——它把每个通道都包进一个合规的 valid/ready 流式原语 `stream_delay`，自己只做接线。
- 区分四个延迟参数 `FixedDelayInput / FixedDelayOutput / StallRandomInput / StallRandomOutput` 的作用，并解释为何把五个通道分成**请求方向（AW/AR/W）**与**响应方向（B/R）**两组分别配置。
- 看懂 `axi_delayer`（结构体内核）与 `axi_delayer_intf`（接口外壳）这套标准的「外壳 + 内核」范式。
- 读懂测试台 `tb_axi_delayer`，并知道如何把 delayer 插进 `rand_master → DUT → axi_sim_mem` 拓扑，用它放大时序 bug、暴露死锁与协议违例。

## 2. 前置知识

本讲承接两篇讲义：

- **u4-l1（axi_join / axi_cut / axi_multicut）**：你已经知道 `axi_cut` 用 `spill_register` **确定性地**切断组合路径、固定多加 1 拍延迟，目的是「用延迟换时序收敛」。本讲的 `axi_delayer` 形态相似（都是给通道插延迟），但目的完全不同：它要的是**可变 / 随机**延迟，用来在验证里制造握手抖动，**不是**为了综合时序收敛。
- **u3-l2（随机主从、scoreboard 与 sim_mem）**：你已经熟悉「rand_master 产生随机激励 → DUT → axi_sim_mem，scoreboard 在 master 侧旁路自检」的标准自检拓扑。本讲的实践任务，就是把 delayer 插进这条链路。

还需回忆两条 AXI 铁律（来自 u1-l3 / u2-l3）：

1. **valid 在握手前不可撤、载荷须稳定**：一旦 `valid` 拉高，在 `valid && ready` 同时为高的那次握手发生之前，`valid` 与载荷都不能变。
2. **五通道彼此独立握手**：AW/W/B/AR/R 各走各的 valid/ready，跨通道没有强制的拍级锁定（例如 W 拍允许在 AW 握手之前到达）。

delayer 的全部价值，就在于「在不违反上述铁律的前提下，随机扰动每个通道各自的握手时刻」。

> 一个术语澄清：本讲说「停顿（stall）」指 valid 已拉高但 ready 故意延迟若干拍才拉高，即 u2-l3 里定义的 **pending** 状态被人为拉长。

## 3. 本讲源码地图

| 文件 | 作用 | 编译层级 |
|------|------|----------|
| [src/axi_delayer.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv) | 定义 `axi_delayer`（结构体内核）与 `axi_delayer_intf`（接口外壳）。内核为五通道各实例化一个 `stream_delay`。 | Level 2 |
| [test/tb_axi_delayer.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_delayer.sv) | delayer 自检测试台：master 端连发 200 次写、slave 端按序回 200 个 B 响应，请求方向全程开启随机停顿。 | test target |

> **关于 `stream_delay`**：它是外部依赖 `common_cells` 提供的流式原语，**不在本仓库源码内**。依赖版本由 [Bender.yml:24](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Bender.yml#L24) 钉死为 `1.39.0`。因此本讲无法给出它在本仓库内的永久链接，也**不臆测其门级实现**，只描述它在 `axi_delayer` 里暴露的**可观察接口与协议契约**。

## 4. 核心概念与源码讲解

### 4.1 定位：delayer 是「验证用的随机抖动器」，不是时序修复器

#### 4.1.1 概念说明

先把它和 `axi_cut` 放在一起对比，建立正确的心智模型：

| 模块 | 延迟是否可变 | 主要目的 | 是否为验证专用 |
|------|--------------|----------|----------------|
| `axi_cut` | 否，固定 1 拍 | 切断组合路径，改善综合关键路径 | 否，可综合、上芯片 |
| `axi_delayer` | 是，可固定 + 可随机 | 在仿真里制造握手抖动，暴露 bug | 可综合，但价值在验证 |

`axi_delayer` 头部注释写得很清楚——它是一个 **synthesizable module that (randomly) delays AXI channels**（[src/axi_delayer.sv:16](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L16)）。它「可综合」只是顺带属性，真正的用途是**验证**：当你的 DUT 在「上游立刻给 ready、下游立刻给 ready」的顺滑时序下跑得过，但在「上游偶尔卡 3 拍、下游偶尔卡 5 拍」的真实时序下就死锁或乱序时，delayer 就是用来在回归里**复现**这类 bug 的放大器。

为什么需要随机抖动？因为 AXI 五通道独立握手，很多 bug（死锁、W 拍错配、B/R 顺序错乱、ID 路由错）只在特定的**相对时序**下才触发。定向用例很难枚举所有时序组合，而 delayer 配合随机种子可以低成本地覆盖大量时序窗口。

#### 4.1.2 核心流程

delayer 对**单条通道**做的事，可以抽象成下面的伪代码（描述可观察契约，不是门级实现）：

```
对每个周期 clk：
  // 输入侧：决定何时给上游回 ready
  if (FixedDelay 计数未满 或 StallRandom 本拍命中) :
      ready_o = 0            // 把上游按在 pending 状态若干拍
  else :
      ready_o = ready_i      // 把下游的 ready 透传给上游（或内部已就绪）
  // 输出侧：把载荷原样送到下游
  payload_o = payload_i      // 数据内容、顺序、拍数都不变
  valid_o   = valid_i （按 FixedDelay/StallRandom 决定的时刻）
契约：
  - 不丢拍、不重排、不改载荷
  - valid 与 payload 在握手前保持稳定（满足 AXI 铁律）
```

关键点：**它只改变「握手在哪个周期发生」，不改变「握了什么、握了几拍、先后顺序」**。这就是它能插入任意链路而不破坏功能的根本原因。把这条契约乘以 5 个通道，就是 `axi_delayer`。

#### 4.1.3 源码精读

模块声明与参数列表在 [src/axi_delayer.sv:17-41](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L17-L41)。内核端口全部用**结构体**表达——`slv_req_i / mst_req_o` 是 `axi_req_t`，`slv_resp_o / mst_resp_i` 是 `axi_resp_t`，这正是 u2-l4 讲过的「内核只面对 req_t/resp_t」范式：

```systemverilog
module axi_delayer #(
  parameter type  aw_chan_t = logic, ...   // 五个通道结构体类型
  parameter type  axi_req_t = logic,
  parameter type axi_resp_t = logic,
  // 延迟参数
  parameter bit          StallRandomInput  = 0,
  parameter bit          StallRandomOutput = 0,
  parameter int unsigned FixedDelayInput   = 1,
  parameter int unsigned FixedDelayOutput  = 1
) (
  input  logic      clk_i,  input  logic rst_ni,
  input  axi_req_t  slv_req_i,  output axi_resp_t slv_resp_o,  // slave 侧
  output axi_req_t  mst_req_o,  input  axi_resp_t mst_resp_i   // master 侧
);
```

注意四个延迟参数的**默认值**：两个 `FixedDelay*` 默认 `1`（默认就延迟 1 拍），两个 `StallRandom*` 默认 `0`（默认不随机停顿）。也就是说，开箱即用的 `axi_delayer` 是一个「每通道固定延迟 1 拍」的器件——这正是它名字里 `delay` 的本意；随机停顿是可选加成。

整个模块体只有一件事：为五通道各实例化一个 `stream_delay`。以 AW 通道为例（[src/axi_delayer.sv:42-56](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L42-L56)）：

```systemverilog
stream_delay #(
  .StallRandom ( StallRandomInput ),
  .FixedDelay  ( FixedDelayInput  ),
  .payload_t   ( aw_chan_t        )
) i_stream_delay_aw (
  .clk_i, .rst_ni,
  .payload_i ( slv_req_i.aw        ),  // 来自 slave 端口的 AW 载荷
  .ready_o   ( slv_resp_o.aw_ready ),  // 向 slave 端口回的 ready
  .valid_i   ( slv_req_i.aw_valid  ),
  .payload_o ( mst_req_o.aw        ),  // 送到 master 端口的 AW 载荷
  .ready_i   ( mst_resp_i.aw_ready ),  // 来自 master 端口的 ready
  .valid_o   ( mst_req_o.aw_valid  )
);
```

可以看到 `axi_delayer` 自己一行逻辑都没写，纯粹是「把 slave 端口某个通道的 valid/payload/ready 与 master 端口对应通道，通过一个 `stream_delay` 连起来」。把这段复制五遍、换通道名，就是整个内核（AW/AR/W 见 [src/axi_delayer.sv:42-88](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L42-L88)，B/R 见 [src/axi_delayer.sv:90-120](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L90-L120)）。`endmodule` 在 [src/axi_delayer.sv:121](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L121)。

#### 4.1.4 代码实践

**目标**：用眼睛追踪一次「slave 端口 → master 端口」的 AW 握手在 `axi_delayer` 内部的信号走向，确认它确实只是一层薄薄的延迟包装。

**步骤**：

1. 打开 [src/axi_delayer.sv:42-56](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L42-L56) 的 `i_stream_delay_aw`。
2. 假设 slave 端在某个周期拉高 `slv_req_i.aw_valid` 并给出 `slv_req_i.aw` 载荷。沿端口映射追：`valid_i ← slv_req_i.aw_valid`、`payload_i ← slv_req_i.aw`、`ready_o → slv_resp_o.aw_ready`。
3. 再追输出侧：`valid_o → mst_req_o.aw_valid`、`payload_o → mst_req_o.aw`、`ready_i ← mst_resp_i.aw_ready`。
4. 同样追踪 B 通道实例（[src/axi_delayer.sv:90-104](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L90-L104)），注意它的方向**反过来**：`payload_i ← mst_resp_i.b`（B 响应从 master 端口进来）、`payload_o → slv_resp_o.b`（送到 slave 端口）。

**需要观察的现象**：AW/AR/W 三个实例的 `payload_i` 都来自 `slv_req_i.*`（请求方向，slave→master）；B/R 两个实例的 `payload_i` 都来自 `mst_resp_i.*`（响应方向，master→slave）。

**预期结果**：你能口述出「数据流方向决定了哪个端口喂 `payload_i`」，并能解释为何如此接线不改变功能。

**待本地验证**：若想看真实波形，可按 u1-l4 的方法 `make sim-axi_delayer.log` 跑测试台后用波形窗口观察 AW 通道 valid 与 ready 之间的停顿拍数。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `axi_delayer` 的四个延迟参数全部保持默认（`FixedDelay*=1`、`StallRandom*=0`），它在功能上等价于本单元前面讲过的哪个原语？区别在哪里？

**参考答案**：行为上接近「每通道都插了一拍延迟」的 `axi_cut`（u4-l1）。区别有二：① `axi_cut` 用的是 `spill_register`，目的是切断组合路径以利综合；`axi_delayer` 用的是 `stream_delay`，参数可调成随机停顿，目的是验证抖动。② `axi_delayer` 暴露了 per-channel 的延迟控制（下一节会看到请求/响应方向分组），`axi_cut` 只有 Bypass 开关。

**练习 2**：为什么 delayer 「只改握手时刻、不改载荷与顺序」这一点，对它能安全插入任意链路如此关键？

**参考答案**：AXI 的很多不变量（同 ID 同方向保序、W 拍与 AW 的归属关系、B/R 的配对）都依赖「事务内容与顺序不被中间环节篡改」。delayer 若改了载荷或重排了拍，就会破坏这些不变量，DUT 出错时就分不清是 DUT 的 bug 还是 delayer 的 bug，验证就失去意义。所以「不丢、不改、不重排」是它作为验证器件的立身之本。

---

### 4.2 请求方向 vs 响应方向：四个参数为何分成两组

#### 4.2.1 概念说明

`axi_delayer` 有四个延迟参数，名字带 `Input` / `Output` 后缀。这里的 Input/Output **不是**指某个端口方向，而是指**事务的行进方向**——把 delayer 当作一个「slave 设备」来看：

- **请求方向（Input 组）**：上游 master 发来的 AW / AR / W，从 slave 端口流进、向 master 端口流出。用 `FixedDelayInput` / `StallRandomInput` 配置。
- **响应方向（Output 组）**：下游 slave 回送的 B / R，从 master 端口流进、向 slave 端口流出（「输出」给上游）。用 `FixedDelayOutput` / `StallRandomOutput` 配置。

为什么分组？因为现实里**请求方向和响应方向的抖动来源往往不同**：请求侧的停顿来自上游 master 的行为，响应侧的停顿来自下游 slave 的处理延迟。把它们拆成两组参数，就能在验证里独立控制两侧的时序压力——例如「上游请求很顺、但下游响应经常卡顿」这种典型场景。

#### 4.2.2 核心流程

| 通道 | 数据流向 | 用哪组参数 | 源码实例 |
|------|----------|-----------|----------|
| AW | slave → master（请求） | `*Input` | [src/axi_delayer.sv:43-46](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L43-L46) |
| AR | slave → master（请求） | `*Input` | [src/axi_delayer.sv:59-62](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L59-L62) |
| W  | slave → master（请求） | `*Input` | [src/axi_delayer.sv:75-78](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L75-L78) |
| B  | master → slave（响应） | `*Output` | [src/axi_delayer.sv:91-94](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L91-L94) |
| R  | master → slave（响应） | `*Output` | [src/axi_delayer.sv:107-110](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L107-L110) |

#### 4.2.3 源码精读

对照 B 通道实例（[src/axi_delayer.sv:91-94](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L91-L94)）和 AW 通道实例（[src/axi_delayer.sv:43-46](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L43-L46)），唯一的不同就是参数名：

```systemverilog
// AW（请求方向）用 Input 组
.StallRandom ( StallRandomInput ),  .FixedDelay ( FixedDelayInput )
// B（响应方向）用 Output 组
.StallRandom ( StallRandomOutput ), .FixedDelay ( FixedDelayOutput )
```

这就是「分组」的全部实现——没有任何 if 分支，纯粹是例化时把不同通道接到不同参数上。简洁、可综合、零额外逻辑。

#### 4.2.4 代码实践

**目标**：阅读测试台对 delayer 的参数配置，反推它在验证什么。

**步骤**：

1. 看 [test/tb_axi_delayer.sv:46-58](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_delayer.sv#L46-L58) 的例化：`FIXED_DELAY_INPUT(0)`、`STALL_RANDOM_INPUT(1)`，其余两个参数没写（取 intf 默认 `FIXED_DELAY_OUTPUT=1`、`STALL_RANDOM_OUTPUT=0`，见 [src/axi_delayer.sv:135-136](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L135-L136)）。
2. 据此回答：这个 TB 里，请求方向（AW/AR/W）是「固定 0 拍延迟 + 随机停顿」，响应方向（B/R）是「固定 1 拍延迟 + 不随机停顿」。

**需要观察的现象 / 预期结果**：你能说清——该 TB 故意只在**请求方向**施加随机抖动，响应方向只给固定 1 拍。这是一种典型的「单向施压」配置。

**待本地验证**：把 `STALL_RANDOM_OUTPUT` 也改成 `1` 重跑，观察仿真是否仍通过（功能上应仍通过，因为 delayer 不破坏协议；但跑的时间会更长）。

#### 4.2.5 小练习与答案

**练习 1**：若你想专门压测「下游 slave 响应很慢导致 B 通道堆积」的场景，应该调哪个参数？

**参考答案**：调响应方向，即 `FixedDelayOutput`（拉大固定延迟）和 / 或 `StallRandomOutput`（置 1 引入随机停顿）。两者作用于 B/R 通道。

**练习 2**：把四个参数全设成「`FixedDelay=0` 且 `StallRandom=0`」，delayer 退化成什么？

**参考答案**：退化成一条无延迟、无停顿的透传通路——功能上等价于 `axi_join`（u4-l1）的纯连线，只是多绕了一层 `stream_delay` 的例化。这印证了 delayer 是 join/cut 这一连续谱上的「可调延迟档」。

---

### 4.3 axi_delayer_intf：接口外壳范式

#### 4.3.1 概念说明

`axi_delayer`（内核）只认 `req_t`/`resp_t` 结构体，而大多数顶层设计和测试台用的是 `AXI_BUS` 接口（u2-l3）。于是库提供了一个**接口外壳** `axi_delayer_intf`：它对外暴露 `AXI_BUS.Slave` 和 `AXI_BUS.Master` 两个 modport 端口，对内用 u2-l4 的 `AXI_TYPEDEF_*` 声明结构体、用 `AXI_ASSIGN_*` 在接口与结构体之间搬运，再例化内核。这就是 u2-l4 / u4-l1 反复出现的「接口外壳 + 结构体内核」标准范式。

#### 4.3.2 核心流程

外壳的接线流程：

```
AXI_BUS.Slave slv ──AXI_ASSIGN_TO_REQ──► slv_req (axi_req_t) ─┐
                                                              ├──► axi_delayer (内核)
AXI_BUS.Master mst ◄──AXI_ASSIGN_FROM_REQ── mst_req (axi_req_t) ─┘
   ▲                                  ▲
   └──AXI_ASSIGN_TO_RESP── slv_resp   └──AXI_ASSIGN_FROM_RESP── mst_resp
        (内核 slv_resp_o)                   (内核 mst_resp_i)
```

#### 4.3.3 源码精读

外壳模块声明在 [src/axi_delayer.sv:127-142](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L127-L142)，端口是两个接口：

```systemverilog
module axi_delayer_intf #(
  parameter int unsigned AXI_ID_WIDTH   = 0, ...   // 用扁平宽度参数（Synopsys DC 要求默认值）
  parameter bit          STALL_RANDOM_INPUT  = 0,
  parameter bit          STALL_RANDOM_OUTPUT = 0,
  parameter int unsigned FIXED_DELAY_INPUT   = 1,
  parameter int unsigned FIXED_DELAY_OUTPUT  = 1
) (
  input  logic    clk_i,  input  logic rst_ni,
  AXI_BUS.Slave   slv,
  AXI_BUS.Master  mst
);
```

注意外壳用的是**扁平宽度参数**（`AXI_ID_WIDTH` 等，大写），而不是类型参数——因为接口版要面向「只给位宽」的普通用户；注释也点明 Synopsys DC 要求参数有默认值（[src/axi_delayer.sv:128](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L128)）。

接着用一组 `AXI_TYPEDEF_*` 宏从位宽生成五个通道结构体与 req/resp（[src/axi_delayer.sv:144-156](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L144-L156)），声明内部结构体线网（[src/axi_delayer.sv:158-159](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L158-L159)），再用 `AXI_ASSIGN_*` 把接口拆成结构体（[src/axi_delayer.sv:161-165](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L161-L165)），最后例化内核并把大写参数映射到内核的小写参数（[src/axi_delayer.sv:167-186](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L167-L186)）。

最后一段是综合保护下的参数断言（[src/axi_delayer.sv:188-197](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L188-L197)）：用 `// pragma translate_off` + `` `ifndef VERILATOR `` 包起来，确保位宽至少为 1，否则 `$fatal`。这是 pulp-platform 库里接口外壳的标准收尾。

#### 4.3.4 代码实践

**目标**：在测试台里找到「接口三明治」接线，理解 DV 接口、可综合接口与外壳如何串起来。

**步骤**：读 [test/tb_axi_delayer.sv:31-58](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_delayer.sv#L31-L58)。TB 声明了带时钟的 `axi_slave_dv`/`axi_master_dv`（`AXI_BUS_DV`）和不带时钟的 `axi_slave`/`axi_master`（`AXI_BUS`），用 `AXI_ASSIGN` 互连，然后把 `axi_master` 接到 delayer 的 `slv`、`axi_slave` 接到 `mst`。

**需要观察的现象**：driver 操作的是 DV 接口（带 clk），delayer 外壳用的是普通 `AXI_BUS`，二者靠 `AXI_ASSIGN` 桥接——这正是 u3-l3 讲过的「双接口三明治」。

**预期结果**：你能画出 `axi_master_drv → axi_master_dv →(AXI_ASSIGN)→ axi_master(=delayer.slv) → delayer → axi_slave(=delayer.mst) →(AXI_ASSIGN)→ axi_slave_dv → axi_slave_drv` 这条链。

#### 4.3.5 小练习与答案

**练习 1**：为什么外壳用扁平位宽参数，内核却用类型参数？

**参考答案**：接口版面向只关心位宽的顶层用户，扁平参数更直观、且 Synopsys DC 对参数默认值有要求；内核用类型参数是为了与库内其他模块（xbar、demux 等）共享同一套 `req_t`/`resp_t` 类型，避免类型不匹配。两层各取所需，是 u2-l4 范式的体现。

**练习 2**：[src/axi_delayer.sv:188-197](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L188-L197) 的断言为什么用 `pragma translate_off` 包起来？

**参考答案**：因为这些 `assert ... else $fatal` 是仿真期检查，综合工具看不懂（或不该综合成电路）。`translate_off` 告诉综合工具忽略这段，保证可综合性，同时仿真期仍能挡住非法位宽。

---

### 4.4 tb_axi_delayer：用手搓 axi_driver 自检

#### 4.4.1 概念说明

要强调一个**容易误解**的点：`tb_axi_delayer` **没有**使用 u3-l2 的 `axi_rand_master`/`axi_sim_mem`/`axi_scoreboard`，而是用 u3-l1 的底层 `axi_test::axi_driver` **手搓**了主从两端。它的自检策略很朴素：master 端连发 200 次写事务，slave 端把收到的 AW 的 id 排进队列、再按序回 200 个 B 响应；master 端最后 `recv_b` 收满 200 个就算通过。它不比对数据内容，只验证「在请求方向随机停顿下，200 次写事务的握手都能最终完成、不卡死」。

> 这与 spec 里要求的「rand_master + sim_mem + scoreboard」实践是**两套拓扑**。本节先吃透仓库自带的手搓 TB；把 delayer 插进 rand_master/sim_mem 拓扑的任务放在第 5 节综合实践。

#### 4.4.2 核心流程

```
master 进程 (initial, 行 77-101):
  reset_master() → repeat(200):
      随机化 ax_beat → send_aw → 构造 w_beat(data='hcafelatte) → send_w
  → repeat(200): recv_b          // 必须能收到 200 个 B，否则卡死
  → done = 1                     // 触发时钟停摆

slave 进程 (initial, 行 103-121):
  reset_slave() → repeat(200):
      recv_aw → (打印) → recv_w → (打印) → b_id_queue.push_back(ax_id)
  → while 队列非空: pop_front → send_b   // 按 AW 到达顺序回 B
```

两个 `initial` 块并发跑：master 一边猛发，slave 一边收并按序回 B。中间隔着开了随机停顿的 delayer。只要 200 个 B 都能被 master 收到，就证明 delayer 在请求方向随机抖动下没有死锁、没有丢拍。

#### 4.4.3 源码精读

driver 句柄的创建见 [test/tb_axi_delayer.sv:60-61](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_delayer.sv#L60-L61)，两个 driver 都用 `TA=200ps`、`TT=700ps`（u3-l1 讲过的 application/test time，满足 `0<TA<TT<tCK=1ns`）。

master 进程发写事务（[test/tb_axi_delayer.sv:77-101](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_delayer.sv#L77-L101)）：

```systemverilog
repeat (200) begin
    @(posedge clk);
`ifdef XSIM
    rand_success = std::randomize(ax_beat); assert(rand_success);   // XSIM 特殊处理
`else
    rand_success = ax_beat.randomize(); assert(rand_success);
`endif
    axi_master_drv.send_aw(ax_beat);
    w_beat.w_data = 'hcafelatte;        // 写数据固定，不参与比对
    axi_master_drv.send_w(w_beat);
end
repeat (200) axi_master_drv.recv_b(b_beat);
```

注意 [test/tb_axi_delayer.sv:86-92](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_delayer.sv#L86-L92) 的 `` `ifdef XSIM `` 分支：Xcelium(XSIM) 下 `std::randomize(obj)` 与 `obj.randomize()` 在受限范围上行为不同，故为 XSIM 单独留了一支——这是 pulp-platform 跨 EDA 工具兼容的典型细节（呼应 u16-3）。

slave 进程按序回 B（[test/tb_axi_delayer.sv:103-121](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_delayer.sv#L103-L121)）：用 `b_id_queue` 把每个收到的 AW 的 id 压栈，最后 `pop_front` 逐个回 B。这保证了 B 的 id 与 AW 一一对应、且按 AW 到达顺序返回。

时钟与复位发生器在 [test/tb_axi_delayer.sv:63-75](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_delayer.sv#L63-L75)：先拉低复位两拍再拉高，然后 `while (!done)` 产生时钟，`done` 由 master 进程在收满 200 个 B 后置 1 来停摆。

#### 4.4.4 代码实践

**目标**：跑通仓库自带的 delayer 测试台，确认「请求方向随机停顿下不卡死」。

**步骤**：

1. 按 u1-l4 介绍的方法，在仓库根目录执行 `make sim-axi_delayer.log`（或直接 `bash scripts/run_vsim.sh tb_axi_delayer`，具体命令以本地 `make help` 输出为准）。
2. 打开生成的 `sim-axi_delayer.log`，定位到仿真器统计行。

**需要观察的现象**：日志里应能看到大量 `AXI AW: addr ...` 与 `AXI W: data ...` 的 `$info` 打印（来自 [test/tb_axi_delayer.sv:112-114](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_delayer.sv#L112-L114)），以及末尾的 `Errors: 0,` 统计。

**预期结果**：仿真以 `Errors: 0` 结束，证明 200 次写事务在 delayer 随机停顿下全部握手完成。

**待本地验证**：本环境未必装了 QuestaSim/vsim，若无法运行，则改为「源码阅读型实践」——追 `send_aw`（u3-l1）内部如何用 `<= #TA` 驱动 valid 并等待 ready，解释为何即使 delayer 随机停顿若干拍，`send_aw` 也不会误撤 valid。

#### 4.4.5 小练习与答案

**练习 1**：这个 TB 为什么不验证写数据的正确性（`w_data` 固定为 `'hcafelatte` 且不比对）？

**参考答案**：因为 DUT 是 delayer，按设计它**不改变载荷**，验证重点是「握手能否在随机停顿下完成、不卡死」。数据正确性应由 delayer 所在链路的下游（如 sim_mem + scoreboard）负责，而非 delayer 自身。这个 TB 只做「活性（liveness）」检查。

**练习 2**：master 进程先 `repeat(200)` 发完所有 AW/W，再 `repeat(200)` 收 B。为什么 slave 必须用 `b_id_queue` 按 AW 到达顺序回 B，而不是来一个回一个？

**参考答案**：AXI 规定同一 ID 的响应须按请求顺序返回。把 AW 的 id 入队再按 `pop_front` 顺序回 B，就强制了 B 的顺序与 AW 一致；若乱序回 B，可能触发协议断言或与 master 的 `recv_b` 期望不符。队列在这里实现了「保序」。

**练习 3**：把 TB 里 master 的 `repeat(200)` 改成 `repeat(2000)`、slave 同步改大，delayer 还应该通过。这说明了什么？

**参考答案**：说明 delayer 的「不卡死」是**无界**保证（只要下游最终给 ready），不依赖某个特定事务量。增大事务量只是提高回归置信度，不能改变正确性结论。这也是 directed random verification 用大事务量 + 多种子做回归的动机（u16-l1）。

---

## 5. 综合实践

**任务**：把 `axi_delayer` 插进 u3-l2 的标准自检拓扑，对比「有延迟」与「无延迟」两种情况下 scoreboard 是否都能通过，并解释 delayer 的价值。

**目标拓扑**：

```
axi_rand_master ──► axi_delayer ──► axi_sim_mem
      │  (AXI_ASSIGN_MONITOR)            ▲
      └────────────► axi_scoreboard ─────┘   (旁路监听 master 侧，黄金模型自检)
```

**操作步骤**（这是「扩展型」实践，需参考 u3-l2/u3-l3 的范式自行拼装，本仓库没有现成的这个 TB）：

1. 仿照 `tb_axi_delayer.sv` 的接口三明治（[test/tb_axi_delayer.sv:31-44](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_delayer.sv#L31-L44)），准备好 master 侧与 slave 侧的 `AXI_BUS` + `AXI_BUS_DV` 接口对。
2. 用 `axi_delayer_intf`（[src/axi_delayer.sv:127-142](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_delayer.sv#L127-L142)）把 master 接口和 slave 接口连起来，参数先全用默认（每通道 1 拍固定延迟）。
3. master 侧例化 `axi_rand_master`、slave 侧例化 `axi_sim_mem`，并按 u3-l2 把 `axi_scoreboard` 挂到 master 侧做旁路监听（`AXI_ASSIGN_MONITOR`）。
4. 跑一轮随机读写，确认 scoreboard 报告无失配（`Errors: 0`）。
5. **对比实验**：把 delayer 的 `STALL_RANDOM_INPUT=1`、`STALL_RANDOM_OUTPUT=1` 都打开，再用不同 `sv_seed` 跑若干轮回归。

**需要观察的现象与预期结果**：无论是否开启随机延迟、无论哪个种子，scoreboard 都应通过——因为 delayer 不改载荷、不重排、不丢拍，功能等价。如果某个种子下**失败**了，那几乎可以肯定是 DUT 链路（而非 delayer）里存在只在特定时序下暴露的 bug，这正是 delayer 的价值所在。

**解释其价值**（写在实践报告里）：delayer 把「上游/下游握手时刻的相对快慢」这一原本固定的维度变成了随机变量，让一次回归等效于成百上千种时序组合，从而低成本地放大死锁、W 拍错配、B/R 顺序错乱等只在特定时序下触发的 bug。它不改功能，只改时序——所以「带 delayer 仍通过」是功能正确性的强信号，「带 delayer 偶发失败」则是真实 bug 的灵敏探测器。

**待本地验证**：本实践需要自行编写测试台并在本地仿真器运行；若本地无仿真器，可降级为「源码阅读 + 拓扑画图」——画出上述拓扑并标注每个 `AXI_ASSIGN*` 宏的接线位置，口头推演 delayer 插入后 scoreboard 仍能自检的原因。

## 6. 本讲小结

- `axi_delayer` 是**验证用的随机抖动器**：可综合，但价值在于制造握手时序抖动以放大死锁与协议违例，与 `axi_cut`（综合时序修复）目的不同。
- 它的内核**自己不写逻辑**，只为五通道各实例化一个外部 `stream_delay`（来自 `common_cells 1.39.0`），靠该原语「只改握手时刻、不改载荷与顺序」的契约保证协议安全。
- 四个延迟参数按**事务方向**分组：`*Input`（`FixedDelayInput`/`StallRandomInput`）管请求方向 AW/AR/W，`*Output` 管响应方向 B/R，可独立施压。
- `axi_delayer`（结构体内核）+ `axi_delayer_intf`（接口外壳）是标准的「外壳 + 内核」范式：外壳用扁平位宽参数 + `AXI_TYPEDEF/ASSIGN` 宏，内核用 `req_t`/`resp_t`。
- 仓库自带的 `tb_axi_delayer` 用**手搓 `axi_driver`**（非 rand_master/sim_mem）做活性自检：请求方向随机停顿下连发 200 次写不死锁即通过；其中含一处 XSIM 随机化兼容分支。
- 把 delayer 插进 `rand_master → axi_sim_mem + scoreboard` 拓扑后，功能应不受影响——这正是用它做时序回归探测器的前提。

## 7. 下一步学习建议

- **进入多路复用核心**：下一单元 U5 讲 `axi_demux`/`axi_mux`，它们同样依赖 valid/ready 握手和保序，delayer 是压测它们仲裁与路由逻辑的理想「时序扰动源」。
- **对比 `axi_fifo`**：U7-l1 的 `axi_fifo` 用 FIFO 做确定性缓冲，与 delayer 的「随机延迟」互补——理解两者差异后再看 `axi_isolate`（U7-l2）会更顺。
- **回看 `stream_delay`**：若你能在本地 Bender 拉取的 `common_cells` 里找到 `stream_delay.sv`，对照阅读它的门级实现，验证本讲描述的「固定延迟 + 随机停顿 + 协议安全」契约，并弄清 `FixedDelay` 究竟是延迟 ready 还是延迟 valid。
- **随机验证方法学**：U16-l1 会系统讲 directed random verification 与多种子回归，本讲的 delayer 正是该方法在「时序维度」上的标准工具。
