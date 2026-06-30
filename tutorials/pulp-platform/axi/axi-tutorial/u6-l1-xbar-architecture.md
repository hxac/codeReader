# xbar 架构与配置

## 1. 本讲目标

本讲把前几讲学过的「零件」——`axi_demux`（1 拆 N）、`axi_mux`（N 合 1）——组装成一台完整的**全连接交叉开关** `axi_xbar`。读完本讲你应该能够：

1. 画出 `axi_xbar = axi_xbar_unmuxed(demux 阵列 + cross 矩阵) + axi_mux 阵列` 的整体结构，并说清楚请求/响应在其中的流向。
2. 解释为什么 **master 端口的 AXI ID 宽度 = slave 端口 ID 宽度 + \( \lceil \log_2(\text{NoSlvPorts}) \rceil \)**，并能指出这条公式在源码里的出处。
3. 看懂 `axi_pkg::xbar_cfg_t` 的每一个字段是如何被分发到内部 `demux`/`mux` 的参数上的，并理解 `ATOPs`、`Connectivity`、`LatencyMode` 三个顶层开关的作用。

本讲只讲**架构与配置**；地址映射的匹配规则、译码错误、保序与死锁等细节留给下一讲（u6-l2、u6-l3）。

## 2. 前置知识

本讲是「先零件后整机」路线里第一次「装整机」，需要你先带上这几块零件的心智模型（均来自前置讲义）：

- **`axi_demux`（u5-l1）**：一个 slave 端口拆成 N 个 master 端口。它自己**不译码地址**，而是吃一个外部喂进来的 `slv_aw_select_i`/`slv_ar_select_i`（位宽 \( \lceil \log_2 N \rceil \)）来决定路由；内部用 `axi_demux_id_counters` 保证同 ID 同方向事务保序。
- **`axi_mux`（u5-l3）**：N 个 slave 端口合成 1 个 master 端口。它的关键机制是 **ID 扩展路由**：用 `axi_id_prepend` 把「这个请求来自第几个 slave 端口」拼进 AXI ID 的最高位，这样响应回来时只靠解码 ID 高位就能路由回正确源。
- **`axi_pkg::xbar_cfg_t` 与 `xbar_latency_e`（u2-l2）**：把十几个参数打包成一个 struct，并用一个 10 位单热点位掩码 `LatencyMode` 表达「在哪些通道插 spill 寄存器」。
- **AXI 五通道与 ID（u1-l3）**：AW/W/B/AR/R；ID 挂在 AW/AR/B/R 上用于保序与路由。

如果你对上面任何一块还陌生，建议先回看对应讲义再继续。

## 3. 本讲源码地图

| 文件 | 角色 |
|:--|:--|
| [src/axi_xbar.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv) | 交叉开关**顶层**：实例化 1 个 `axi_xbar_unmuxed` + `NoMstPorts` 个 `axi_mux`；并提供接口外壳 `axi_xbar_intf`。 |
| [src/axi_xbar_unmuxed.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv) | **「未合并」内核**：每 slave 端口一个 `addr_decode` + `axi_demux` + `axi_err_slv`，再用 `axi_multicut` 阵列把每个 demux 输出交叉连到对应 mux 输入。 |
| [doc/axi_xbar.md](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md) | 官方文档：架构总览、地址映射、译码错误、配置字段表、流水线/延迟、保序与「为何内部不插寄存器」的死锁分析。 |
| [src/axi_pkg.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv) | 提供 `xbar_cfg_t`、`xbar_latency_e`、`xbar_rule_64_t/32_t` 三类配置类型（本讲只引用，定义细节见 u2-l2）。 |

## 4. 核心概念与源码讲解

### 4.1 全连接交叉开关的整体结构

#### 4.1.1 概念说明

**交叉开关（crossbar / xbar）** 是一种「任意 slave 端口都能在同一时刻连到任意 master 端口」的互连拓扑。所谓 **fully-connected（全连接）**，是指每个 slave 端口到每个 master 端口之间都有一条独立通路，理论上任意一对 slave↔master 都可以并行传输，互不抢占（除非抢同一个 master 端口）。

`axi_xbar` 是一个支持**任意数量 slave/master 端口**、且完整实现 AXI4 + AXI5 原子操作（ATOPs）的全连接交叉开关。源码开头一行注释就是它的自我定位：

> `axi_xbar`: Fully-connected AXI4+ATOP crossbar with an arbitrary number of slave and master ports.

见 [src/axi_xbar.sv:16-17](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L16-L17)。

#### 4.1.2 核心流程

为什么不直接写一个「大模块」？因为「组合优于配置」——把职责单一的 demux 和 mux 背靠背拼起来，比堆参数清晰得多。整体结构可以概括为两阶段：

```text
               ┌──────────────── axi_xbar_unmuxed ────────────────┐    ┌── axi_mux 阵列 ──┐
slave port 0 ─▶│ demux[0] (按地址 select) ─┐                        │    │                   │
slave port 1 ─▶│ demux[1] (按地址 select) ─┼─▶ cross 矩阵 ─▶ ──┐    │ ──▶│ mux[0] ─▶ master 0 │
slave port 2 ─▶│ demux[2] (按地址 select) ─┘                    │    │    │ mux[1] ─▶ master 1 │
               │  + 每 port 一个 err_slv 兜底译码失败            │    │    │ mux[2] ─▶ master 2 │
               └──────────────────────────────────────────────────┼────┼──▶ mux[3] ─▶ master 3 │
                                                                    │    └───────────────────┘
                          （unmuxed 输出仍是「窄 ID」的 slv_req_t）──┘
```

- **请求方向（AW/W/AR）**：每个 slave 端口先进 `axi_xbar_unmuxed`，由 demux 按地址路由到「目标 master 端口对应的横向那一列」；cross 矩阵把这些请求摆成「每 master 端口一堆」喂给对应的 `axi_mux`；mux 把同一 master 端口上来自不同 slave 端口的请求合并成一路输出。
- **响应方向（B/R）**：master 端口的响应先进 mux，mux 按 ID 高位路由回正确的 slave 端口列，再经 cross 矩阵回到对应 demux，demux 按 ID（见 u5-l1 的 id_counters）回到正确的 slave 端口。

注意一个关键点：**unmuxed 内部所有信号都还是 slave 端口侧的窄 ID 类型**（`slv_req_t`），ID 扩展是在 mux 阶段才发生的。这就是模块名「unmuxed（尚未做 mux 合并）」的由来。

#### 4.1.3 源码精读

顶层的内部互联信号声明带了一句点睛注释——「送给 mux 的信号是 slave 类型，因为 mux 会扩展 ID」：

[src/axi_xbar.sv:93-95](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L93-L95) —— 声明二维数组 `mst_reqs[NoMstPorts][NoSlvPorts]`，第一维是 master 端口号、第二维是来源 slave 端口号。

随后顶层只做了两件事。**第一件**：例化唯一一个 `axi_xbar_unmuxed`，把所有 slave 端口喂进去，把「每 master 端口一堆」的请求阵列 `mst_reqs` 拿出来：

[src/axi_xbar.sv:97-120](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L97-L120) —— 例化 `i_xbar_unmuxed`，输出 `.mst_ports_req_o(mst_reqs)`、输入 `.mst_ports_resp_i(mst_resps)`。

**第二件**：用 `for` 循环给每个 master 端口配一个 `axi_mux`，把 `mst_reqs[i]`（第 i 个 master 端口收到的、来自全部 slave 端口的请求阵列）合并成最终输出 `mst_ports_req_o[i]`：

[src/axi_xbar.sv:122-155](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L122-L155) —— `gen_mst_port_mux` 循环，共 `NoMstPorts` 个 `i_axi_mux`。

所以**顶层直接实例化的子模块就两类**：1 个 `axi_xbar_unmuxed` + `NoMstPorts` 个 `axi_mux`。`axi_xbar` 整个 `endmodule` 之前的可综合正文，除了这两段例化和一组连线，几乎没有别的逻辑——这正是「组合优于配置」的活样本（与 u1-l1 讲的设计哲学呼应）。

#### 4.1.4 代码实践

**目标**：用源码确认「两类子模块、各多少个」。

1. 打开 [src/axi_xbar.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv)，定位到第 97 行的 `axi_xbar_unmuxed` 与第 122 行的 `for ... gen_mst_port_mux`。
2. 数一下 `module axi_xbar`（第 18 行）到第一个 `endmodule`（第 157 行）之间，一共出现了几次模块例化关键字。
3. 记录：`axi_xbar_unmuxed` 出现 1 次；`axi_mux` 出现在一个循环里，循环上界是 `Cfg.NoMstPorts`。

**预期结果**：顶层正文只有 1 个 `i_xbar_unmuxed` 和 `NoMstPorts` 个 `i_axi_mux`，没有任何其它用户逻辑。这也意味着 `axi_xbar` 的复杂度几乎全部「外包」给了 unmuxed 和 mux 两个子模块。

#### 4.1.5 小练习与答案

**Q1**：如果把 `axi_xbar` 看成黑盒，它对外的 master 端口请求是「窄 ID」还是「宽 ID」类型？为什么？

**答案**：是**宽 ID**类型（`mst_req_t`，字段名带 `mst_` 前缀，见顶层端口 [src/axi_xbar.sv:78](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L78)）。因为 master 端口位于 mux **之后**，ID 已经被 mux 扩展过了；而 unmuxed 内部用的是窄 ID 的 `slv_req_t`。

**Q2**：顶层二维数组 `mst_reqs[NoMstPorts][NoSlvPorts]` 的两个维度各代表什么？

**答案**：第一维 `NoMstPorts` 是目标 master 端口号，第二维 `NoSlvPorts` 是来源 slave 端口号。`mst_reqs[m][s]` 表示「来自 slave 端口 s、要去 master 端口 m」的那一路请求；mux `m` 把 `mst_reqs[m][*]` 这一整行合并成单路输出。

---

### 4.2 axi_xbar_unmuxed：demux 阵列、地址译码与 cross 矩阵

#### 4.2.1 概念说明

`axi_xbar_unmuxed` 是「交叉开关里还没做 mux 合并的那一半」。它的职责是：**对每个 slave 端口，把进来的事务按地址路由到正确的 master 端口那一列**。它完成的是「路由（routing）」而非「合并（multiplexing）」。

这里出现了一个 u5-l1 里强调过的分工原则的实例：**译码在外、路由在内**。`axi_demux` 自己不会算地址该去哪，所以 unmuxed 为每个 slave 端口单独例化一个 `addr_decode`（来自 `common_cells`）来算 select，再把 select 喂给 demux。

#### 4.2.2 核心流程

对每一个 slave 端口 `i`，unmuxed 重复下面这套结构：

```text
                                addr_map_i (全局共享)
                                      │
slave port i ──┬──▶ addr_decode(AW) ──▶ dec_aw ──┐
               │                                  ├─▶ slv_aw_select ─▶ axi_demux ──▶ slv_reqs[i][0..NoMstPorts-1]
               └──▶ addr_decode(AR) ──▶ dec_ar ──┘                       │
                              │  dec_error?                           │  slv_reqs[i][NoMstPorts] ─▶ axi_err_slv (DECERR)
                              └─▶ 选 NoMstPorts（即第 NoMstPorts 路）─▶ 译码错误兜底从端
```

- 每个 slave 端口配 **两个 `addr_decode`**（AW、AR 各一个，因为读写地址要分别路由）。
- demux 被例化成 `NoMstPorts + 1` 路：多出来的第 `NoMstPorts` 路**专门接一个 `axi_err_slv`**，当地址译码失败（`dec_error`）时把事务送到这里，由它返回 `RESP_DECERR`——这就是文档说的「每 slave 端口一个内部译码错误从端」。
- 全部 slave 端口译码完之后，用一层 **cross 矩阵**（`gen_xbar_slv_cross` × `gen_xbar_mst_cross` 双重循环）把 `slv_reqs[i][j]`（slave i 去 master j）摆到 `mst_ports_req_o[j][i]`（master j 收到来自 slave i）的位置上，交给顶层的 mux 阵列。

#### 4.2.3 源码精读

先看「多出一路」的关键类型与数组定义。为了容纳译码错误从端，端口索引要比真实 master 端口数多 1：

[src/axi_xbar_unmuxed.sv:84-90](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L84-L90) —— `MstPortsIdxWidthOne = $clog2(NoMstPorts + 1)`，并据此声明 `slv_reqs[NoSlvPorts][NoMstPorts+1]`（最后一格给 err_slv）。

每个 slave 端口的译码 + demux + 错误从端，都包在一个 `for` 循环里：

[src/axi_xbar_unmuxed.sv:95-193](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L95-L193) —— `gen_slv_port_demux` 循环体。其中第 101-129 行例化两个 `addr_decode`；第 131-134 行把译码结果转成 select，**译码失败时 select 指向 `NoMstPorts`**（即错误从端那一格）：

[src/axi_xbar_unmuxed.sv:131-134](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L131-L134) —— `slv_aw_select = dec_aw_error ? NoMstPorts : dec_aw`（AR 同理）。

[src/axi_xbar_unmuxed.sv:164-193](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L164-L193) —— 例化 `axi_demux`，注意 `.NoMstPorts(Cfg.NoMstPorts + 1)`；它吃 unmuxed 算好的 `slv_aw_select_i`/`slv_ar_select_i`，自己不算地址。

[src/axi_xbar_unmuxed.sv:195-211](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L195-L211) —— 例化 `axi_err_slv`，固定返回 `axi_pkg::RESP_DECERR`，`MaxTrans=4`（注释说明：事务在这里就被终结，所以只需少量并发槽以省资源）。

译码与分拆完成后，cross 矩阵把每条 (slave i, master j) 链路摆到位，并可在此处插入若干级流水线寄存器：

[src/axi_xbar_unmuxed.sv:215-254](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L215-L254) —— 双重循环 `gen_xbar_slv_cross`/`gen_xbar_mst_cross`。`if (Connectivity[i][j])` 成立时用 `axi_multicut`（级数由 `Cfg.PipelineStages` 决定，见 [src/axi_xbar_unmuxed.sv:218-219](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L218-L219)）连通；否则把该链路**断开**：对应 mux 输入置零，请求转送到一个本地 err_slv。

> 关于这段 cross 矩阵里「内部不插 spill 寄存器、只允许在 cross 处插 `axi_multicut`」的死锁原因，是 u6-l3 的主题，本讲先记住结论：**spill 只能加在 cross 这一层的 `axi_multicut`，不能加在 demux 与 mux 之间**。

#### 4.2.4 代码实践

**目标**：跟踪一次「译码失败」请求的去向。

1. 打开 [src/axi_xbar_unmuxed.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv)，跟随 `dec_aw_error` 这个布尔量。
2. 沿第 131-134 行看它如何把 select 改写成 `NoMstPorts`。
3. 再看 demux 的 `.NoMstPorts(Cfg.NoMstPorts + 1)`（第 174 行）与第 195 行的 `axi_err_slv`，确认第 `NoMstPorts` 路输出正好连到 err_slv。
4. 对照文档 [doc/axi_xbar.md:31-35](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L31-L35) 关于「Decode Errors」的描述。

**预期结果**：你会看到一个清晰的「错误专用通道」——译码失败的事务不会污染任何真实 master 端口，而是被 err_slv 吸收并返回 `RESP_DECERR`（读数据为 `32'hBADCAB1E`，按数据位宽补零或截断）。**待本地验证**：可后续在 u6-l2 用 `tb_axi_xbar` 构造一个未映射地址读，观察仿真波形里响应码与读数据。

#### 4.2.5 小练习与答案

**Q1**：为什么 demux 的 `NoMstPorts` 参数是 `Cfg.NoMstPorts + 1` 而不是 `Cfg.NoMstPorts`？

**答案**：因为多出的第 `NoMstPorts` 路专门留给译码错误从端 `axi_err_slv`。真实 master 端口占 `0..NoMstPorts-1`，错误从端占索引 `NoMstPorts`，所以 demux 要能输出 `NoMstPorts+1` 路。

**Q2**：unmuxed 里每个 slave 端口为什么需要**两个** `addr_decode`？

**答案**：因为读（AR）和写（AW）的地址要分别、独立地路由——一个 master 端口完全可能「可写不可读」或反之，而且 AW/AR 通道是独立握手的，必须各自译码各自喂 select。

---

### 4.3 master 端口 ID 为什么更宽：ID 扩展路由

#### 4.3.1 概念说明

这是交叉开关最容易被初学者忽略、却最关键的设计点：**master 端口的 AXI ID 比 slave 端口宽**，多出来的高位**不是浪费，而是用来路由响应的「回信地址」**。

回顾 u5-l3：`axi_mux` 在合并 N 个 slave 端口时，用 `axi_id_prepend` 把「这个请求来自第几个 slave 端口」拼进 ID 最高位。于是一旦响应（B/R）从 master 端口回来，mux 只需解码 ID 的这几位高位，就能 one-hot 点亮正确 slave 端口的 valid——**无需任何在途计数器或查表**。

交叉开关有 `NoSlvPorts` 个 slave 端口，所以要区分它们就需要 \( \lceil \log_2(\text{NoSlvPorts}) \rceil \) 位。这就是公式的来源：

\[
\text{AxiIdWidthMstPorts} \;=\; \text{AxiIdWidthSlvPorts} \;+\; \lceil \log_2(\text{NoSlvPorts}) \rceil
\]

#### 4.3.2 核心流程

- **请求方向**：slave 端口 `s` 上的事务带着「窄 ID」进入 unmuxed；经过 demux、cross 矩阵后到达 mux `m`；mux `m` 在把请求合到 master 端口 `m` 输出时，把 `s` 的编号**前置**到 ID 最高位，ID 变宽。
- **响应方向**：master 端口 `m` 的 B/R 响应带着「宽 ID」回到 mux `m`；mux 解码高位得到 `s`，把这几位**剥离**后把窄 ID 的响应送回 slave 端口 `s` 那一列；再经 cross、demux 回到 slave 端口 `s`。
- **关键不变量**：因为每个 slave 端口的编号在 ID 高位天然互不相同，**不同 slave 端口发出来的事务即便窄 ID 完全相同，在 master 端口侧也不会冲突**——这正是 mux 不需要 demux 那套 id_counters 的根本原因（对照 u5-l2/u5-l3）。

#### 4.3.3 源码精读

文档把这条公式作为一条**硬性约束**写明：

[doc/axi_xbar.md:13-15](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L13-L15) —— “The ID width of the master ports must be `AxiIdWidthSlvPorts + $clog_2(NoSlvPorts)`.”

在接口外壳 `axi_xbar_intf` 里，这条公式被直接写成 localparam，并据此声明 master 端口的 ID 类型：

[src/axi_xbar.sv:183-186](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L183-L186) —— `AxiIdWidthMstPorts = Cfg.AxiIdWidthSlvPorts + $clog2(Cfg.NoSlvPorts)`，并用它定义 `id_mst_t`；随后第 192、195、197、199、201 行用 `id_mst_t` 声明 master 侧各通道的 ID 字段，用 `id_slv_t` 声明 slave 侧。

而在顶层例化 mux 时，喂给 mux 的 `SlvAxiIDWidth` 就是 **slave 端口的窄 ID 宽度**，mux 内部自行算出要前置几位：

[src/axi_xbar.sv:122-125](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L122-L125) —— `i_axi_mux` 的 `.SlvAxiIDWidth(Cfg.AxiIdWidthSlvPorts)`，注释写明这是“slave 端口的 ID 宽度”。

#### 4.3.4 代码实践

**目标**：亲手算一次 ID 宽度，并验证源码一致性。

1. 假设 `Cfg.NoSlvPorts = 4`、`Cfg.AxiIdWidthSlvPorts = 6`，按公式计算 master 端口 ID 宽度。
2. 打开 [src/axi_xbar.sv:183](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L183) 核对表达式。
3. 解释：如果用户给的 `mst_*_chan_t` 类型里 ID 字段宽度不等于这个值，会发生什么？

**预期结果**：\( \lceil \log_2 4 \rceil = 2 \)，master ID 宽度 = 6 + 2 = 8 位。第 3 问的答案是：类型宽度不一致会导致 mux 内部 `axi_id_prepend` 的位拼接错位，综合/仿真会报位宽不匹配；所幸 unmuxed 末尾有一段断言会兜底（见 [src/axi_xbar_unmuxed.sv:259-266](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L259-L266) 校验 slave 侧 ID 宽度，推荐你顺手读一眼）。

#### 4.3.5 小练习与答案

**Q1**：为什么 mux 不需要像 demux 那样维护「同 ID 在途计数器」？

**答案**：因为 mux 用 ID 高位（slave 端口号）天然区分了来源；不同 slave 端口的请求即便窄 ID 相同，宽 ID 也不同，永远不会在 master 端口侧发生「同 ID 抢占」的保序冲突，所以无需 id_counters。

**Q2**：若 `NoSlvPorts = 1`，公式给出 master ID 宽度 = slave ID 宽度 + \( \lceil \log_2 1 \rceil \) = slave ID 宽度 + 0。这合理吗？

**答案**：合理但要注意实现细节。\( \lceil \log_2 1 \rceil = 0 \)，意味着无需任何路由位，master 与 slave 等宽。源码在顶层用 `MstPortsIdxWidth = (NoMstPorts==1) ? 1 : $clog2(NoMstPorts)`（[src/axi_xbar.sv:64-65](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L64-L65)）给默认 master 端口索引预留了至少 1 位，那是另一个端口（`default_mst_port_i`）的需要，与 mux 的 ID 扩展不冲突。

---

### 4.4 配置 xbar：xbar_cfg_t、LatencyMode、ATOPs 与 Connectivity

#### 4.4.1 概念说明

`axi_xbar` 几乎全靠一个 `Cfg` 参数（类型 `axi_pkg::xbar_cfg_t`）来配置，再加上 `ATOPs` 和 `Connectivity` 两个独立开关。这种「把十几个相关参数打包进一个 struct、按字段名赋值」的风格，正是 u2-l2 讲过的配置结构体模式的典型应用——好处是例化时不会写错参数顺序。

`Cfg` 之外还有两个**顶层布尔/位矩阵开关**：

- **`ATOPs`**（默认 `1'b1`）：是否支持 AXI5 原子操作。它会被透传给内部的 demux（`.AtopSupport`）和 err_slv（`.ATOPs`）。若你的下游不支持 ATOP，应配合 `axi_atop_filter` 使用（见 u15-l1）。
- **`Connectivity`**（默认 `'1` 全连接）：一个 `[NoSlvPorts-1:0][NoMstPorts-1:0]` 的位矩阵，`Connectivity[i][j]=1` 表示 slave 端口 i **允许**访问 master 端口 j。置 0 即物理剪掉这条链路（见 4.2.3 的 `gen_no_connection`），可省面积。

#### 4.4.2 核心流程

`Cfg` 字段到内部模块参数的映射很有规律：

| `xbar_cfg_t` 字段 | 去向 | 作用 |
|:--|:--|:--|
| `NoSlvPorts` / `NoMstPorts` | 顶层维度 | 决定 demux 数（=NoSlvPorts）、mux 数（=NoMstPorts）、cross 矩阵大小 |
| `MaxMstTrans` | demux `.MaxTrans` | 每 slave 端口最大在途事务数（id_counters 容量） |
| `MaxSlvTrans` | mux `.MaxWTrans` | 每 master 端口 W FIFO 深度 |
| `AxiIdWidthSlvPorts` | demux `.AxiIdWidth` / mux `.SlvAxiIDWidth` | slave 端口窄 ID 宽度；master 宽度由此推导 |
| `AxiIdUsedSlvPorts` | demux `.AxiLookBits` | 参与保序判定的低位 ID 位数（见 u5-l2） |
| `UniqueIds` | demux `.UniqueIds` | 在途 ID 唯一时省掉 id_counters |
| `AxiAddrWidth` / `AxiDataWidth` / `NoAddrRules` | 类型/译码器 | 地址宽度、数据宽度、地址规则条数 |
| `FallThrough` | mux `.FallThrough` | AW 路由是否直通到 W（影响 W 组合路径） |
| `LatencyMode` | 拆成两段给 demux/mux 的 `Spill*` | 在哪些通道插 spill 寄存器 |
| `PipelineStages` | cross 矩阵的 `axi_multicut` 级数 | demux 与 mux 之间可插的流水线级数 |

其中最巧妙的是 `LatencyMode` 的拆分。它是一个 10 位单热点位掩码（u2-l2 已讲），高 5 位（`[9:5]`）管 demux 五通道、低 5 位（`[4:0]`）管 mux 五通道：

```text
LatencyMode[9:5] = {DemuxAw, DemuxW, DemuxB, DemuxAr, DemuxR}  -> 给 axi_demux 的 SpillAw/W/B/Ar/R
LatencyMode[4:0] = {MuxAw,   MuxW,   MuxB,   MuxAr,   MuxR  }  -> 给 axi_mux   的 SpillAw/W/B/Ar/R
```

文档推荐的高频配置是 `CUT_ALL_AX`（AW/AR 双侧都切，延后到 2 拍）且 `FallThrough=0`，因为 AW/AR 通道组合逻辑最重；若两个 xbar 互联成环，必须改用 `CUT_SLV_PORTS`/`CUT_MST_PORTS`/`CUT_ALL_PORTS` 之一，否则未切的通道会形成时序环路（见 [doc/axi_xbar.md:59-65](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L59-L65)）。

#### 4.4.3 源码精读

配置类型与延迟枚举的定义在 axi_pkg（细节见 u2-l2），本讲只标注位置以便查阅：

[src/axi_pkg.sv:450-479](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L450-L479) —— 10 个单热点位 `DemuxAw..MuxR` 与 `xbar_latency_e` 枚举（含 `NO_LATENCY`、`CUT_ALL_AX`、`CUT_SLV_PORTS` 等）。

[src/axi_pkg.sv:481-522](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L481-L522) —— `xbar_cfg_t` 结构体，字段含义与文档 [doc/axi_xbar.md:40-55](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L40-L55) 的表格一一对应。

顶层把 `Cfg` 字段分发到两个子模块的方式一目了然。给 demux 的一段（注意 `LatencyMode[9:5]`）：

[src/axi_xbar_unmuxed.sv:164-182](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L164-L182) —— demux 取 `Cfg.MaxMstTrans` 作 `MaxTrans`、`Cfg.AxiIdUsedSlvPorts` 作 `AxiLookBits`、`Cfg.LatencyMode[9]` 作 `SpillAw`……直到 `[5]` 作 `SpillR`。

给 mux 的一段（注意 `LatencyMode[4:0]`）：

[src/axi_xbar.sv:138-145](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L138-L145) —— mux 取 `Cfg.MaxSlvTrans` 作 `MaxWTrans`、`Cfg.FallThrough` 直传，`Cfg.LatencyMode[4]` 作 `SpillAw`……`[0]` 作 `SpillR`。

`ATOPs` 的透传见 demux 的 `.AtopSupport(ATOPs)`（[src/axi_xbar_unmuxed.sv:166](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L166)）与两处 err_slv 的 `.ATOPs(ATOPs)`（第 200、243 行）。`Connectivity` 的消费点就是 4.2.3 那段 cross 矩阵的 `if (Connectivity[i][j])`（[src/axi_xbar_unmuxed.sv:217](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L217)）。

#### 4.4.4 代码实践

**目标**：为一个具体拓扑构造合法的 `Cfg`。

设想要搭一个「2 个 slave 端口、3 个 master 端口」的交叉开关，slave 端口 ID 宽 4 位、地址 32 位、数据 32 位，地址规则共 3 条。按字段名写出配置（**示例代码**，非项目原有文件）：

```systemverilog
// 示例代码：仅为说明字段写法
localparam axi_pkg::xbar_cfg_t MyXbarCfg = '{
  NoSlvPorts:         32'd2,
  NoMstPorts:         32'd3,
  MaxMstTrans:        32'd8,   // 每 slave 端口最多 8 笔在途
  MaxSlvTrans:        32'd4,   // 每 master 端口 W FIFO 深 4
  FallThrough:        1'b0,    // 关闭 AW->W 直通，缩短 W 组合路径
  LatencyMode:        axi_pkg::CUT_ALL_AX,  // 推荐配置
  PipelineStages:     32'd0,   // cross 矩阵不额外加流水线
  AxiIdWidthSlvPorts: 32'd4,
  AxiIdUsedSlvPorts:  32'd4,   // 用满 ID 位以避免误冲突
  UniqueIds:          1'b0,
  AxiAddrWidth:       32'd32,
  AxiDataWidth:       32'd32,
  NoAddrRules:        32'd3
};
```

操作步骤：

1. 对照 [doc/axi_xbar.md:40-55](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L40-L55) 的字段表，逐行确认每个字段含义。
2. 用 4.3 的公式手算 master 端口 ID 宽度（应为 4 + \( \lceil \log_2 2 \rceil \) = 5 位）。
3. 思考：把 `LatencyMode` 改成 `NO_LATENCY`、`FallThrough` 改成 `1` 会得到什么特性的 xbar？

**预期结果**：得到一个 master ID 宽 5 位的 2×3 全连接 xbar；`NO_LATENCY + FallThrough=1` 会得到一个**全组合、零延迟**的 xbar（文档明说可行，见 [doc/axi_xbar.md:63](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L63)），适合低频紧凑场景，但 W 通道组合路径最长。

#### 4.4.5 小练习与答案

**Q1**：`MaxMstTrans` 和 `MaxSlvTrans` 这两个名字容易混淆，它们分别喂给了哪类内部模块？

**答案**：名字里的「Mst/Slv」指的是**事务到达的端口侧**。`MaxMstTrans` 喂给 demux 的 `MaxTrans`（约束每 slave 端口能发往某 master 端口的在途数，即 id_counters 容量）；`MaxSlvTrans` 喂给 mux 的 `MaxWTrans`（约束每 master 端口 W FIFO 深度）。详见 [src/axi_xbar_unmuxed.sv:175](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L175) 与 [src/axi_xbar.sv:139](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L139)。

**Q2**：把 `Connectivity[0][2]` 设为 0（其余为 1）后，slave 端口 0 发往 master 端口 2 的事务会怎样？

**答案**：该链路被 `gen_no_connection` 剪断（[src/axi_xbar_unmuxed.sv:236-252](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L236-L252)）：对应 mux 输入置零，请求被一个本地 `axi_err_slv`（`MaxTrans=1`）吸收并返回 `RESP_DECERR`。注意这与「地址译码失败」是两条独立的错误通路。

**Q3**：`LatencyMode = CUT_ALL_AX` 时，哪些通道被切？为什么文档推荐它？

**答案**：切的是 `DemuxAw|DemuxAr|MuxAw|MuxAr`（[src/axi_pkg.sv:475](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L475)），即 demux 和 mux 两侧的 AW、AR 通道。因为 AW/AR 上挂着地址译码与仲裁，组合逻辑最重，切这两级收益最大；同时配合 `FallThrough=0` 避免把 AW 的逻辑蔓延到 W 通道（见 [doc/axi_xbar.md:62-63](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L62-L63)）。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「拆解一台真实 xbar」的小任务。

**场景**：一个 `Cfg.NoSlvPorts = 4`、`Cfg.NoMstPorts = 5`、`Cfg.AxiIdWidthSlvPorts = 8` 的 `axi_xbar`，`Connectivity` 与 `ATOPs` 取默认值。

请完成：

1. **数子模块**：通读 [src/axi_xbar.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv) 与 [src/axi_xbar_unmuxed.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv)，列出整个 xbar（含 unmuxed 内部）最终例化了多少个 `axi_demux`、多少个 `axi_mux`、多少个 `axi_err_slv`、多少个 `axi_multicut`（按 `PipelineStages=0`、全连接计）。
2. **算 ID**：写出 master 端口的 ID 宽度，并指出公式出处。
3. **画拓扑**：画出 4×5 的 demux/mux 阵列示意图，标注请求方向（slave→demux→cross→mux→master）与响应方向（master→mux→cross→demux→slave）。
4. **解释一句**：用一句话向同事解释「为什么 master 端口 ID 要更宽」。

**参考答案要点**：

1. `axi_demux` = NoSlvPorts = **4** 个（每 slave 端口 1 个，[src/axi_xbar_unmuxed.sv:95](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L95)）；`axi_mux` = NoMstPorts = **5** 个（[src/axi_xbar.sv:122](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L122)）；`axi_err_slv` = NoSlvPorts×(1 个译码错误从端 + 0 个断链从端，因为全连接) = **4** 个译码错误从端（[src/axi_xbar_unmuxed.sv:195](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L195)）；`axi_multicut` = NoSlvPorts×NoMstPorts = **20** 个（全连接，[src/axi_xbar_unmuxed.sv:216-217](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar_unmuxed.sv#L216-L217)）。
2. master ID 宽度 = 8 + \( \lceil \log_2 4 \rceil \) = **10** 位；公式见 [doc/axi_xbar.md:15](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_xbar.md#L15) 与 [src/axi_xbar.sv:183](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L183)。
3. 示意图参考 4.1.2 的文字版，把它扩成 4 行（slave）×5 列（master）的网格即可。
4. 「master 端口 ID 多出的高位，是 mux 用来记录『这个响应该回哪个 slave 端口』的回信地址，这样响应路由无需任何在途查表。」

> 第 1 题中各类子模块的精确数量随 `Connectivity` 与 `PipelineStages` 变化：每条断链会多 1 个 `axi_err_slv` 并少 1 个 `axi_multicut`；`PipelineStages>0` 时每个 `axi_multicut` 内部还会展开成相应级数的寄存器（见 u4-l1）。

## 6. 本讲小结

- `axi_xbar` 顶层只做两件事：例化 **1 个 `axi_xbar_unmuxed`** + **`NoMstPorts` 个 `axi_mux`**，是「组合优于配置」的活样本。
- `axi_xbar_unmuxed` 负责**路由**：每 slave 端口一对 `addr_decode` + 一个 `axi_demux`（拆成 `NoMstPorts+1` 路，多出一路接 `axi_err_slv`），再用 `axi_multicut` 阵列摆成 cross 矩阵交给 mux。
- **master 端口 ID 比 slave 端口宽** \( \lceil \log_2(\text{NoSlvPorts}) \rceil \) 位，这高位是 mux 用来路由响应的「来源端口标签」，使 mux 无需在途计数器。
- 全部配置收口在 `axi_pkg::xbar_cfg_t`：`MaxMstTrans→demux`、`MaxSlvTrans→mux`、`LatencyMode` 高 5 位给 demux / 低 5 位给 mux；外加 `ATOPs`（原子操作开关）与 `Connectivity`（链路剪裁矩阵）两个独立开关。
- 推荐配置是 `CUT_ALL_AX` + `FallThrough=0`；两个 xbar 互联成环时必须改用 `CUT_*_PORTS` 之一以免成环。
- 本讲只讲架构与配置，**没有**展开地址映射匹配规则、保序停顿与「内部不插 spill」的死锁证明——这些是 u6-l2、u6-l3 的内容。

## 7. 下一步学习建议

- **u6-l2（地址映射、译码错误与默认端口）**：深入 `addr_decode` 的规则匹配（前闭后开、高位优先、重叠处理）与每 slave 端口的「默认 master 端口」运行期切换机制。
- **u6-l3（排序、停顿与无死锁设计）**：精读 `doc/axi_xbar.md` 的 *Ordering and Stalls* 与 *Design Rationale*，搞懂同 ID 跨端口停顿、`AxiIdUsedSlvPorts` 折中，以及 W 通道四条死锁条件。
- 想立刻跑一台真实 xbar，可直接读 [test/tb_axi_xbar.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_xbar.sv)（u16-l1 会专门讲这个测试台的定向随机验证方法学），对照本讲的架构图看它如何例化 `axi_xbar_intf`。
