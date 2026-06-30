# axi_fifo 与 spill_register 缓冲

## 1. 本讲目标

本讲承接 u4-l1（`axi_join`/`axi_cut`/`axi_multicut`），把「在两个 AXI 端口之间放一个缓冲」这件事讲透。读完本讲，你应该能够：

- 说清 `axi_fifo` 为什么给五条通道**各自独立**配一个 FIFO，以及它如何把 AXI 的 `valid/ready` 握手翻译成 FIFO 的 `push/pop`。
- 理解 `spill_register`（来自外部依赖 `common_cells`）这种「深度为 1 的最小缓冲」既能**切断组合路径**又能**增加一拍延迟**的双重作用，以及 `Bypass` 开关的含义。
- 看懂 `axi_mux`/`axi_demux` 暴露的 `SpillAw/W/B/Ar/R` 五个参数如何成为「在哪个通道插一级寄存器」的时序旋钮，并能解释为什么默认值是「AW/AR 开、W/B/R 关」。
- 动手测量：在一对 master/slave 之间插入 `axi_fifo` 后，背压下的**最大在途事务数**与 FIFO 深度的关系。

## 2. 前置知识

本讲默认你已经掌握以下概念（来自前置讲义）：

- **五通道与 `valid/ready` 握手**（u1-l3）：AW/W/B/AR/R 五条通道；同一时钟沿 `valid` 与 `ready` 同高才算一次握手（一个 beat）；`valid` 一旦拉高，在握手完成前其载荷不可改变。
- **组合路径 / 切断组合路径**（u4-l1）：`axi_cut` 通过在每条通道插一级 `spill_register`，把「输入到输出的纯组合连线」打断，用「一拍延迟 + 少量面积」换取时序裕量。
- **`req_t`/`resp_t` 结构体内核 + 接口外壳**范式（u2-l4）：可综合内核只认请求/响应结构体，外壳用 `AXI_TYPEDEF_*`/`AXI_ASSIGN_*` 宏对接 `AXI_BUS` 接口。
- **随机主从拓扑**（u3-l2）：`axi_rand_master` 与 `axi_rand_slave` 是搭建自检测试台的标准激励/响应组件。

两个本讲要用到的新术语：

- **在途事务（outstanding / in flight）**：地址拍已经握手、但响应拍尚未返回的事务。在途数衡量「同时挂在总线上的并发量」。
- **背压（backpressure）**：下游用 `ready=0` 告诉上游「我暂时接不住」，上游必须停住。缓冲的存在正是为了吸收这种速度差。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/axi_fifo.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_fifo.sv) | 本讲主角：为五条 AXI 通道各配一个 `fifo_v3`，提供可配置深度的独立缓冲。 |
| [src/axi_mux.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv) | 多路汇聚器，内部用 5 个 `spill_register`（由 `SpillAw/W/B/Ar/R` 控制）做时序可调，还用一个 `fifo_v3` 暂存 AW 的路由决策。 |
| [src/axi_demux.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux.sv) | 1 拆 N 路由器，与 mux 对称，同样暴露 `SpillAw/W/B/Ar/R`。 |
| [src/axi_cut.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cut.sv) | u4-l1 已讲，本讲作为 `spill_register` 的标准用法范本引用。 |
| [test/tb_axi_fifo.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_fifo.sv) | `axi_fifo` 的随机测试台，是本讲代码实践的基座。 |
| [Bender.yml](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Bender.yml) | 声明 `common_cells 1.39.0` 依赖，`spill_register` 与 `fifo_v3` 均来自此外部包。 |

> 说明：`spill_register` 与 `fifo_v3` 都来自外部依赖 `common_cells`（见 [Bender.yml:24](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Bender.yml#L24)），由 Bender 在编译期拉取，**不在本仓库源码内**。因此本讲不直接引用它们的源文件行号，而是通过本仓库内对它们的**例化**来反推其接口契约。

## 4. 核心概念与源码讲解

### 4.1 axi_fifo：按通道独立的 FIFO 缓冲

#### 4.1.1 概念说明

`axi_fifo` 解决的问题是：**让上游（master）和下游（slave）的速度解耦**。上游可能一阵猛灌若干事务，下游可能偶尔停顿；如果中间只有一根直通线（`axi_join`），下游一停上游就被迫同步停。在中间放一个 FIFO，上游就能在下游停顿时把数据先攒起来，等下游恢复再放，从而**吸收突发、平滑吞吐**。

注意它和 `axi_cut`（u4-l1）的定位区别：

- `axi_cut` 用 `spill_register`，深度恒为 1，主要目的是**切断组合路径**（修时序），缓冲能力几乎为零。
- `axi_fifo` 用 `fifo_v3`，**深度可配置**（参数 `Depth`），主要目的是**提供真正的缓冲容量**；顺带也切断了组合路径。

一个关键设计决策是：**五条通道各配一个独立 FIFO，而不是共用一个**。因为五条通道的时间行为差异巨大——AW 是每事务 1 拍、W 可能是几十拍的长突发、B 又是 1 拍、AR 1 拍、R 可能几十拍。各自独立缓冲才能让每条通道按自己的节奏吸收背压，互不掣肘。

#### 4.1.2 核心流程

`axi_fifo` 自己几乎不写逻辑，只做两件事：例化 5 个 `fifo_v3`，以及把 AXI 的 `valid/ready` 握手翻译成 `fifo_v3` 的 `push/pop`。

**握手翻译规则**（以上游→下游方向为例）：

| AXI 侧含义 | FIFO 侧信号 | 翻译 |
|---|---|---|
| 可以发给下游吗？ | `empty_o` | `valid_o = ~empty_o`（非空就能发） |
| 下游收了吗？ | `pop_i` | `pop_i = valid_o & ready_i`（一次输出握手弹一拍） |
| 能收上游吗？ | `full_o` | `ready_o = ~full_o`（非满就能收） |
| 收到上游了吗？ | `push_i` | `push_i = valid_i & ready_o`（一次输入握手压一拍） |

数据通路则是 `data_i → FIFO → data_o`，载荷在压入时进入、弹出时原样输出。

`axi_fifo` 还有一个**退化分支**：当 `Depth == 0` 时，FIFO 没有任何容量，直接退化为一根直通线（等价于 `axi_join`）。

执行流程伪代码：

```
if (Depth == 0):  mst_req_o = slv_req_i;  slv_resp_o = mst_resp_i   // 直通
else:
  for 每条通道 ch in {AW, AR, W, R, B}:
      valid_o[ch]  = ~empty[ch]
      ready_o[ch]  = ~full[ch]            // 注意方向：请求通道 ready 在 slv 侧、响应通道 ready 在 mst 侧
      push[ch]     = valid_i[ch] & ready_o[ch]
      pop[ch]      = valid_o[ch] & ready_i[ch]
      i_fifo[ch]:  fifo_v3(.DEPTH=Depth, .FALL_THROUGH=FallThrough)
```

关于 `FallThrough`（直通模式）的取舍：

- `FallThrough = 0`（默认）：压入的数据**下一拍**才出现在输出口，增加 1 拍延迟，但**完全切断**了输入到输出的组合路径。
- `FallThrough = 1`：当 FIFO 空时，压入的数据**当拍**就组合地出现在输出口（`empty_o` 随 `push_i` 组合变化），延迟近零，但留下了一条组合路径。

这与 `spill_register` 的 Bypass/非 Bypass 是同一组权衡，只是 `fifo_v3` 还额外提供深度。

#### 4.1.3 源码精读

**模块参数与端口**——`Depth`、`FallThrough`、五个通道结构体类型、请求/响应结构体类型：

这是 [axi_fifo.sv:21-43](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_fifo.sv#L21-L43)，定义了缓冲深度、是否直通，以及用结构体表达的 slave/master 端口。文件头注释点明用途：「AXI4 Fifo / Can be used to buffer transactions」。

**退化分支：`Depth == 0` 时直通**——

[axi_fifo.sv:45-48](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_fifo.sv#L45-L48) 用 `if (Depth == '0)` 把输入整块赋给输出，等价于零开销连线，无需任何 FIFO。

**握手翻译的核心 11 行**——这是全模块的「大脑」：

[axi_fifo.sv:53-63](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_fifo.sv#L53-L63) 把每个 FIFO 的 `empty/full` 翻译成 AXI 的 `valid/ready`。要特别注意方向：请求通道（AW/AR/W）的输出在 master 侧、输入握手在 slave 侧；响应通道（B/R）正好相反，所以 `r_ready`/`b_ready` 出现在 `mst_req_o`，而 `r_valid`/`b_valid` 出现在 `slv_resp_o`。

**五个独立的 `fifo_v3` 例化**——以 AW 通道为代表：

[axi_fifo.sv:66-82](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_fifo.sv#L66-L82) 例化 `i_aw_fifo`，`.DEPTH(Depth)`、`.FALL_THROUGH(FallThrough)`，`data_i` 接 `slv_req_i.aw`、`data_o` 接 `mst_req_o.aw`，`push_i`/`pop_i` 按上面的翻译规则驱动。其余四条通道在 [axi_fifo.sv:83-150](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_fifo.sv#L83-L150) 完全对称地各例化一个，唯一区别是数据方向（B/R 的 `data_i` 来自 `mst_resp_i`）。`flush_i` 恒接 `1'b0`，即本 FIFO 不支持运行期冲刷。

**接口外壳 `axi_fifo_intf`**——遵循「外壳用宏、内核用结构体」范式：

[axi_fifo.sv:188-223](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_fifo.sv#L188-L223) 先用 `AXI_TYPEDEF_*` 宏由扁平位宽（`ADDR_WIDTH` 等）生成五个通道与 req/resp 结构体，再用 `AXI_ASSIGN_*` 在 `AXI_BUS` 接口与结构体之间搬运，最后例化结构体内核 `axi_fifo`。这也是测试台里直接使用的形态。

#### 4.1.4 代码实践

**实践目标**：在已有的 `rand_master → axi_fifo → rand_slave` 拓扑上，测量背压下的最大写在途事务数，并验证它受 `Depth` 约束。

**操作步骤**：

1. 阅读测试台 [test/tb_axi_fifo.sv:104-117](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_fifo.sv#L104-L117)，确认 DUT 就是 `axi_fifo_intf`，左侧 `master` 接随机主、右侧 `slave` 接随机从。注意主端 `MaxAW = MaxAR = 30`（[tb_axi_fifo.sv:24-25](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_fifo.sv#L24-L25)），即主端自身允许最多 30 笔写在途；而 FIFO 的 `Depth` 默认 16（[tb_axi_fifo.sv:18](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_fifo.sv#L18)）。
2. 测试台已在 [tb_axi_fifo.sv:140-179](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_fifo.sv#L140-L179) 统计了 AW/AR 握手数，但**没有**统计在途数。请在 `proc_sim_progress` 里补一段在途计数（示例代码，仅展示思路，需自行并入现有变量声明）：

   ```systemverilog
   // 示例代码：追踪写在途数（AW 已握手 - B 已握手）
   int unsigned b_cnt = 0, outstanding = 0, max_outstanding = 0;
   // ... 在 forever 循环的 #TestTime; 之后追加：
   if (master.b_valid && master.b_ready) b_cnt++;
   outstanding = (aw >= b_cnt) ? (aw - b_cnt) : 0;
   if (outstanding > max_outstanding) max_outstanding = outstanding;
   // ... 仿真结束前打印：
   $info("Depth=%0d  max outstanding writes=%0d", Depth, max_outstanding);
   ```

   > 上面用到的 `aw` 计数器测试台里已有（[tb_axi_fifo.sv:151-153](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_fifo.sv#L151-L153)），只需新增 `b_cnt`。注意在 master 接口上数：`master.aw` 是压入 FIFO 的地址拍，`master.b` 是 FIFO 回给主端的写响应，两者之差正是挂在 FIFO（及下游）上的写在途数。
3. 用不同 `Depth` 跑回归（`Depth` 是 `tb_axi_fifo` 的 parameter，可在 vsim 用 `-gDepth=...` 覆盖，或直接改默认值后用 `make sim-axi_fifo.log` 仿真，方法见 u1-l4）。

**需要观察的现象**：随着下游随机停顿，写在途数会上下波动；当下游停顿较久时，在途数会被「顶」到某个上限后不再增长，同时主端的 `aw_ready` 开始掉（被 FIFO 的 `full` 反压）。

**预期结果**：最大写在途数近似等于 `Depth`（再加个别下游在途的常数项），且受主端 `MaxAW` 的双重封顶，即约为 \(\min(\text{Depth},\,\text{MaxAW})\)。因此：

- `Depth=16`、`MaxAW=30` 时，封顶由 FIFO 决定，最大在途 ≈ 16；
- 把 `Depth` 调到 `4`，最大在途应随之降到 ≈ 4；
- 把 `Depth` 调到 `32`（> MaxAW），最大在途改由主端 `MaxAW=30` 封顶，不再随 `Depth` 增长。

定量关系上，对单拍写（`len=0`），在途数受 AW-FIFO 容量约束：

\[ N_{\text{outstanding}}^{(\text{write})} \;\lesssim\; \text{Depth} \;+\; N_{\text{downstream}} \]

对长度为 \(L\)（即 \(L+1\) 拍）的突发写，一笔事务要占用 \(L+1\) 拍 W 缓冲，故 W-FIFO 反而可能成为瓶颈：

\[ N_{\text{outstanding}}^{(\text{write})} \;\lesssim\; \min\!\left(\text{Depth}_{\text{AW}},\; \left\lfloor \tfrac{\text{Depth}_{\text{W}}}{L+1} \right\rfloor,\; \text{Depth}_{\text{B}}\right) + N_{\text{downstream}} \]

`axi_fifo` 中五个通道共用同一个 `Depth`，所以单拍写下瓶颈在 AW/B 通道，长突发下瓶颈会转移到 W 通道。**具体的最大在途数值待本地验证**（取决于随机种子的停顿模式），但「随 `Depth` 增长、被 `MaxAW` 封顶」的定性关系是确定的。

#### 4.1.5 小练习与答案

**练习 1**：`axi_fifo` 把 `Depth` 设为 0 和直接用 `axi_join` 有何区别？
**答案**：功能上等价——都退化为一根零开销直通线（见 [axi_fifo.sv:45-48](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_fifo.sv#L45-L48) 的 `gen_no_fifo` 分支）。区别仅在表达意图：`Depth=0` 是「我参数化了深度但碰巧选了 0」，`axi_join` 是「我一开始就不想要任何缓冲」。

**练习 2**：为什么五条通道要各配一个 FIFO，而不是把五个通道的拍数共用一个大 FIFO？
**答案**：因为五条通道的拍数分布差异极大（AW/AR/B 每事务 1 拍，W/R 可能几十拍），且方向不同（请求向下游、响应向上游）。共用 FIFO 既无法区分方向，也会让某条长突发通道（如 W）把容量吃光、饿死其他通道。各自独立才能让每条通道按自身节奏吸收背压。

**练习 3**：`FallThrough=1` 相比默认值 `0`，牺牲了什么、换来了什么？
**答案**：换来近零延迟（空 FIFO 压入的数据当拍就出现在输出口），牺牲了「切断组合路径」这一性质——因为 `empty_o` 与 `data_o` 会随 `push_i`/`data_i` 组合变化，留下从输入到输出的组合路径。所以需要修时序的场合应保持 `FallThrough=0`。

---

### 4.2 spill_register：切断组合路径的最小积木

#### 4.2.1 概念说明

`spill_register` 来自外部依赖 `common_cells`，可以理解为一个**深度为 1 的 FIFO**：它内部只有一级寄存器，接口直接说 AXI 通用的 `valid/ready/data`（不需要像 `fifo_v3` 那样手动翻译成 `push/pop`）。它的两大用途：

1. **切断组合路径**：在两个模块之间插一级寄存器，输入到输出不再有当拍穿透的组合逻辑，从而改善关键路径时序。这正是 u4-l1 里 `axi_cut` 的全部工作。
2. **加一拍延迟**：被寄存的数据要下一拍才出现，给上下游多一个 clock cycle 的喘息。

它带一个 `Bypass` 参数：`Bypass=1` 时退化为纯组合直通（不寄存、不断路径），`Bypass=0` 时才真正寄存。于是 `spill_register` 是「要不要在这里断一刀」这个二元决策的最小实现。

`spill_register` 与 `fifo_v3` 的关系可以这样对照：

| 维度 | `spill_register` | `fifo_v3`（深度>1） |
|---|---|---|
| 容量 | 恒为 1 拍 | 可配置（`DEPTH`） |
| 接口 | 原生 `valid/ready/data` | `push/pop/full/empty`，需手动翻译 |
| 主要目的 | 切组合路径 / 加 1 拍 | 提供真正缓冲容量 |
| 在本库的典型用法 | `axi_cut`、mux/demux 的 `Spill*` | `axi_fifo`、mux 的 W 决策队列 |

#### 4.2.2 核心流程

`spill_register` 的接口是一组标准的 valid/ready 流（stream）信号：

```
输入侧：valid_i, data_i, ready_o   // 上游驱动 valid_i/data_i，模块回 ready_o
输出侧：valid_o, data_o, ready_i   // 模块驱动 valid_o/data_o，下游回 ready_i
参数： T（数据类型）, Bypass
```

行为（`Bypass=0` 时）：

```
若输出侧空闲或正被取走（valid_o & ready_i）且输入侧有数据：
    把 data_i 锁进内部寄存器，下一拍 valid_o=1
否则保持
```

其精髓是「先把数据**洒（spill）**进一级寄存器再对外呈现」，故得名 spill。`Bypass=1` 时直接 `data_o = data_i; valid_o = valid_i; ready_o = ready_i`，等价于一根线。

由于它原生 speaks valid/ready，往 AXI 通道上一接就行——这也是 `axi_cut` 能用区区几行覆盖五条通道的原因。

#### 4.2.3 源码精读

**范本：`axi_cut` 里的五级 `spill_register`**——

`axi_cut` 的文件头直白说明用途：「Breaks all combinatorial paths between its input and output」（[axi_cut.sv:17-19](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cut.sv#L17-L19)）。注释「a spill register for each channel」（[axi_cut.sv:48](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cut.sv#L48)）点明结构。AW 通道的例化见 [axi_cut.sv:49-61](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cut.sv#L49-L61)：

```systemverilog
spill_register #( .T(aw_chan_t), .Bypass(BypassAw) ) i_reg_aw (
    .valid_i(slv_req_i.aw_valid), .ready_o(slv_resp_o.aw_ready), .data_i(slv_req_i.aw),
    .valid_o(mst_req_o.aw_valid), .ready_i(mst_resp_i.aw_ready), .data_o(mst_req_o.aw) );
```

注意 `Bypass` 是按通道独立的（[axi_cut.sv:22-27](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cut.sv#L22-L27)）：有总开关 `Bypass`，还有每通道的 `BypassAw/W/B/Ar/R`，缺省跟随总开关。其余四通道在 [axi_cut.sv:63-117](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cut.sv#L63-L117) 对称例化，只有方向（请求 vs 响应）不同。

**对照：`axi_mux` 的退化分支也用 `spill_register`**——

当 `NoSlvPorts == 1`（只有一个输入端口，无需仲裁）时，`axi_mux` 退化为 5 个 `spill_register` 串联（[axi_mux.sv:72-137](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L72-L137)），等价于一个可按通道 bypass 的 `axi_cut`。这印证了 `spill_register` 是「最小缓冲」的通用积木。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：通过对比 `axi_cut` 与 `axi_join` 的关键路径，体会「切断组合路径」的物理含义。

**操作步骤**：

1. 打开 `axi_cut.sv`（[axi_cut.sv:49-117](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cut.sv#L49-L117)），确认每个 `spill_register` 的输入侧（`slv_*`）与输出侧（`mst_*`）之间只通过其内部寄存器相连。
2. 回顾 u4-l1 讲过的 `axi_join`：它仅一句 `AXI_ASSIGN` 把两端同名信号逐根直连，输入到输出是**纯组合**。
3. 在脑海中（或纸上）画出 `master → join → slave` 与 `master → cut → slave` 两条链路：前者任意一根信号都是 master 输出组合直穿到 slave 输入；后者中间必经一级寄存器，关键路径被腰斩。

**需要观察的现象**：`cut` 链路里，上游 `valid` 到下游 `ready` 的回路、上游 `data` 到下游 `data` 的通路，都多了一个寄存器边界。

**预期结果**：能说出「`Bypass=0` 的 `spill_register` 让组合路径长度从『输入端口→输出端口』缩短为『输入端口→寄存器』+『寄存器→输出端口』两段，每段都更短，故能跑更高频率，代价是延迟 +1 拍」。若把某通道 `Bypass=1`，则该通道退化为 join，路径恢复组合。

#### 4.2.5 小练习与答案

**练习 1**：`spill_register` 和深度为 1 的 `fifo_v3` 在功能上几乎等价，为什么本库仍要分别用两个原语？
**答案**：接口与定位不同。`spill_register` 原生 valid/ready、直接接 AXI 通道、语义就是「断一刀/加一拍」，代码极简（见 `axi_cut`）；`fifo_v3` 走 push/pop 接口、需手动翻译握手、但可配任意深度。用 `spill_register` 表达「我只要 1 拍缓冲且要切路径」更贴切，用 `fifo_v3` 表达「我要 N 拍真实容量」更贴切，可读性更好。

**练习 2**：`axi_cut` 把 `BypassAw=1` 而其余 `Bypass=0`，会对设计产生什么影响？
**答案**：只有 AW 通道恢复成组合直通（不断路径、不加延迟），其余四通道仍各有一级寄存器。结果是 AW 的时序变紧（组合路径更长），但 AW 延迟少 1 拍。这是一种「为关键通道单独让步」的细粒度时序调节。

**练习 3**：为什么说「切断组合路径」和「提供缓冲容量」是两件事，尽管缓冲也能顺带切路径？
**答案**：切路径只需 1 级寄存器（spill），目的是时序；提供容量需要深度 N 的 FIFO，目的是吸收突发、解耦上下游速度。混为一谈会导致要么为了切路径而堆了过深 FIFO（浪费面积），要么为了省面积而深度不足（吞吐被背压压垮）。

---

### 4.3 SpillAw/W/B/Ar/R：mux 与 demux 的时序旋钮

#### 4.3.1 概念说明

`axi_mux`（u5-l3 详讲）和 `axi_demux`（u5-l1 详讲）内部本来就有一堆组合逻辑（仲裁树 `rr_arb_tree`、ID 拼接、地址译码等），如果直接把它们的输入到输出全走组合路径，关键路径会很长，难以跑高频率。于是它们在每条通道的「边界」上各预留了一个可选的 `spill_register`，用五个参数控制：

```
SpillAw, SpillW, SpillB, SpillAr, SpillR   // 每个一个 bit：1=插寄存器，0=组合直通
```

这就是本讲标题所说的「cut/mux/demux 时序可调的基础」——`axi_cut` 用 `Bypass*`、mux/demux 用 `Spill*`，本质都是「在指定通道插一级 `spill_register`」的开关。

为什么默认值是「AW/AR 开、W/B/R 关」？因为请求方向的**地址通道（AW/AR）**通常位于关键路径起点，插一级寄存器收益最大；而 **W/B/R 是数据/响应通道**，要么本身流量大（W/R 长突发，寄存后面积/功耗代价高），要么不在最差路径上（B），所以默认不插，留给设计者按需打开。

#### 4.3.2 核心流程

以 `axi_mux` 为例，每个 `SpillX` 直接接到对应通道 `spill_register` 的 `Bypass` 端（取反）：

```
spill_register #(.T(...), .Bypass(~SpillX)) i_x_spill_reg (
    valid_i = <内部该通道的 valid>,  data_i = <内部该通道的 data>,
    valid_o = mst_req_o.<ch>_valid,  data_o = mst_req_o.<ch>,        // 输出推向 master 端口
    ready_i = mst_resp_i.<ch>_ready );
```

执行流程伪代码：

```
for 每条通道 ch in {AW, W, B, AR, R}:
    if (Spill_ch == 1):  在 ch 上插一级寄存存器（切路径 + 1 拍延迟）
    else:                ch 组合直通（零延迟，但路径更长）
```

在更上层的 `axi_xbar`（u6-l1）里，这五个开关被进一步打包进 `xbar_cfg_t` 的 `LatencyMode` 字段（见 u2-l2 的 `xbar_latency_e`），用一个枚举统一配置 demux 和 mux 两侧的所有 `Spill*`，文档推荐组合是 `CUT_ALL_AX`（所有地址通道都切）。所以本节的五个参数是「时序旋钮」的最底层，`LatencyMode` 是把它们批量预设的快捷方式。

#### 4.3.3 源码精读

**mux 的五个 `Spill*` 参数及默认值**——

[axi_mux.sv:50-55](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L50-L55) 声明 `SpillAw=1'b1`、`SpillW=1'b0`、`SpillB=1'b0`、`SpillAr=1'b1`、`SpillR=1'b0`，正对应「AW/AR 默认开、W/B/R 默认关」。

**非退化分支里五处 `spill_register` 例化**——

AW 通道：[axi_mux.sv:335-347](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L335-L347)；W 通道：[axi_mux.sv:369-381](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L369-L381)；B 通道：[axi_mux.sv:392-404](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L392-L404)；AR 通道：[axi_mux.sv:428-440](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L428-L440)；R 通道：[axi_mux.sv:451-463](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L451-L463)。每一处的 `.Bypass(~SpillX)` 都把对应参数翻译成「插不插寄存器」。

**mux 内部还混用了一个 `fifo_v3`**——

注意 mux 不只用 `spill_register`：它还用一个深度为 `MaxWTrans` 的 `fifo_v3` 暂存每次 AW 仲裁的「来源端口」决策（[axi_mux.sv:317-333](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L317-L333)），以便 W 通道知道当前数据拍该转发给哪个下游。这正好示范了 4.1/4.2 的分工——`fifo_v3` 承担「容量」（同时挂多笔 AW 决策），`spill_register` 承担「切路径」。

**demux 侧对称的五个参数**——

[axi_demux.sv:56-60](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux.sv#L56-L60) 声明完全相同的 `SpillAw/W/B/Ar/R` 且默认值一致，AW 通道的例化见 [axi_demux.sv:89-101](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_demux.sv#L89-L101)。这种 mux/demux 的对称设计，让 `axi_xbar` 可以用同一套 `LatencyMode` 同时配置两侧。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：读懂 `Spill*` 参数如何逐通道改变 mux 的延迟与路径，并定位它们在 xbar 配置中的上层入口。

**操作步骤**：

1. 在 [axi_mux.sv:50-55](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L50-L55) 找到五个参数，记下默认值。
2. 分别打开 AW（[L335-347](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L335-L347)）与 W（[L369-381](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_mux.sv#L369-L381)）两处 `spill_register`，确认 `.Bypass` 分别接 `~SpillAw` 与 `~SpillW`。
3. 回顾 u2-l2 讲过的 `xbar_cfg_t`/`xbar_latency_e`（`CUT_ALL_AX`、`NO_LATENCY` 等），在脑中建立映射：`LatencyMode` → demux 五个 `Spill*` + mux 五个 `Spill*`。

**需要观察的现象**：把 `SpillW` 从默认 0 改成 1，会多出一级 W 通道寄存器，W 的组合路径变短、但每拍 W 多 1 拍延迟。

**预期结果**：能口头说出「`SpillX=1` ⇔ 在 X 通道插一级 `spill_register` ⇔ 切断该通道组合路径、加 1 拍延迟；五个 `Spill*` 是 xbar `LatencyMode` 的底层实现」。若需在 xbar 层面统一改，应调 `LatencyMode` 而非逐个 `Spill*`（具体可选值见 u2-l2 的 `xbar_latency_e`）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 mux 默认 `SpillAw=1` 而 `SpillW=0`？
**答案**：AW 是每事务 1 拍的地址通道，处于关键路径起点，插一级寄存器对时序收益大、面积代价小；W 是长突发数据通道，拍数多，插寄存器会显著增加面积/功耗且每拍都加延迟，故默认不插，留给设计者按需开。

**练习 2**：`Spill*` 参数与 `axi_cut` 的 `Bypass*` 参数是什么关系？
**答案**：互为取反的同一件事。`spill_register` 的 `Bypass=1` 表示「不插寄存器、组合直通」，而 mux 的 `SpillX=1` 表示「要插寄存器」，故例化时写 `.Bypass(~SpillX)`。`axi_cut` 直接暴露 `Bypass*`（默认 0=插），mux/demux 暴露 `Spill*`（默认按通道定），两者都映射到 `spill_register` 的 `Bypass` 端。

**练习 3**：在 u6-l3 的死锁分析里提到「xbar 内部 demux 与 mux 之间不能插 spill 寄存器」。这与本节「mux 用 `Spill*` 插 spill」矛盾吗？
**答案**：不矛盾。`Spill*` 插的是 mux **对外输出边界**（master 端口侧）和 demux **对外输入边界**（slave 端口侧）的寄存器；而「不能插」指的是 demux 输出 → mux 输入这段**内部 cross 矩阵**上不能加 FIFO/spill，否则会与 W 通道的仲裁推进构成循环等待、触发死锁。两者位置不同。

---

## 5. 综合实践

把本讲三块知识串起来，完成一个「为一段 AXI 链路选缓冲策略」的小设计：

**场景**：你有一个 `axi_rand_master`，要经过一段较长的片上连线后接到一个偶尔停顿的 `axi_sim_mem`（u3-l2）。综合工具报告这条链路关键路径太长、频率上不去；同时下游停顿又导致吞吐受损。

**任务**：

1. **切路径**：在链路中段插入一个 `axi_cut`（或 `axi_multicut`，若一级不够），用 `spill_register` 把长组合路径腰斩。说明你为什么选 cut 而非 multicut（或反之）。
2. **加容量**：在靠近下游处插入一个 `axi_fifo`（选一个 `Depth`），让下游停顿时上游能继续灌若干拍。用 4.1.4 的在途计数法估一个合理的 `Depth`（提示：要让 `Depth` ≈ 下游最长停顿周期内可能累积的 AW 拍数，但别超过 master 的 `MaxAW`）。
3. **时序旋钮**：如果这段链路里用到了 `axi_mux`/`axi_demux`，说明你会把哪些 `Spill*` 打开、哪些保持默认，并解释理由。
4. **自检**：用 `axi_scoreboard` + `axi_sim_mem` 搭自检拓扑（u3-l2），随机读写若干笔，确认插入缓冲后功能仍正确（「带缓冲仍通过」是正确性强信号）。

**预期产出**：一张标注了 cut / fifo / mux 位置与各 `Spill*`/`Depth` 取值的拓扑草图，以及一段说明每个选择「切路径还是加容量」理由的文字。具体可综合性与频率提升**待本地综合验证**。

## 6. 本讲小结

- `axi_fifo` 为 AW/AR/W/B/R **五条通道各配一个独立 `fifo_v3`**，深度由 `Depth` 统一配置，`Depth=0` 退化为直通；它把 AXI 的 `valid/ready` 翻译成 FIFO 的 `push/pop`（`valid=~empty`、`ready=~full`）。
- 缓冲的两大目的要分清：**切组合路径**用 `spill_register`（深度 1、原生 valid/ready、`Bypass` 可关），**提供容量**用深度可配的 `fifo_v3`；`FallThrough` 控制零延迟（但留组合路径）与切路径（加 1 拍）的取舍。
- `axi_cut` 就是「五条通道各一个 `spill_register`」的范本；mux 在 `NoSlvPorts=1` 时也退化为同样的结构。
- `axi_mux`/`axi_demux` 用 `SpillAw/W/B/Ar/R` 五个 bit 把 `spill_register` 的插入做成**每通道可选**，默认「AW/AR 开、W/B/R 关」，并用 `.Bypass(~SpillX)` 接入；mux 内部还混用一个 `fifo_v3` 暂存 AW 路由决策，示范了两类原语的分工。
- 在途事务数受 FIFO 深度约束：单拍写下约 \(\min(\text{Depth},\,\text{MaxAW})\)，长突发下 W 通道可能成为瓶颈；这是 4.1.4 测量的理论依据。
- 上层 `axi_xbar` 的 `LatencyMode` 是这五个 `Spill*`（demux + mux 共 10 个）的批量预设，调时序应优先动 `LatencyMode`。

## 7. 下一步学习建议

- **继续流控族**：本讲是 U7（流控与缓冲）的第一篇。下一篇 u7-l2 讲 `axi_isolate`（在隔离前优雅排空在途事务），届时你会用到本讲的「在途计数」直觉去理解 isolate 如何等待 FIFO 与下游排空。
- **回到 mux/demux 内部**：若想看清 `Spill*` 插入的具体位置与 mux 内部那个 `fifo_v3` 的协作，可重读 u5-l3（axi_mux）与 u5-l1（axi_demux）。
- **跨时钟域**：U8 的 `axi_cdc` 会用 Gray 编码的 CDC FIFO（也是 `fifo_v3` 的变体）做异步缓冲，本讲的「五通道独立 FIFO + 握手翻译」是其同步版基础。
- **扩展阅读**：`common_cells` 仓库中的 `spill_register.sv`、`fifo_v3.sv` 源码（由 Bender 按 `common_cells 1.39.0` 拉取，不在本仓库内），对照阅读可验证本讲由例化反推的接口契约。
