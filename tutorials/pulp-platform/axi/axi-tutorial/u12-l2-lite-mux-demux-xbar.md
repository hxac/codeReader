# axi_lite_mux / demux / xbar

## 1. 本讲目标

本讲是 U12「AXI-Lite 子系统」的第二篇，承接 u12-l1（Lite 接口与 `axi_lite_join` 连接器），把多路复用族讲透。读完本讲你应当能够：

- 说清楚 **AXI-Lite 互联为什么不需要 ID 跟踪**，以及它用什么机制（select FIFO 链）替代了完整 AXI4 的 `axi_id_prepend` 与 `axi_demux_id_counters`。
- 读懂 `axi_lite_mux`（N 合 1）、`axi_lite_demux`（1 拆 N）两个内核，能复述它们的「AW 决策 → W 跟随 → B 回收」「AR 决策 → R 回收」FIFO 链路。
- 看懂 `axi_lite_xbar` 如何用「每 slave 端口一个 demux + cross 矩阵 + 每 master 端口一个 mux」拼出全连接交叉开关，并掌握它的地址映射、译码错误与默认端口机制。
- 能独立写出一个 2×2 的 `axi_lite_xbar` 测试台，配置地址映射并验证两个 master 各自访问到正确的 slave。

## 2. 前置知识

本讲默认你已经掌握以下概念（均在更早的讲义中建立）：

- **AXI4-Lite 是 AXI4 的严格子集**（u12-l1）：每个事务恒为单拍，信号集合只剩 `addr/prot/data/strb/resp`，**没有 `id`、没有 `atop`、没有 `burst/len/size/last`**。这是本讲一切简化的根源。
- **valid/ready 握手与「valid 不可撤」铁律**（u1-l3、u2-l3）：valid 一旦拉高，在握手（valid 与 ready 同高）发生前不可撤回，且载荷必须保持稳定。
- **完整 AXI4 的 mux / demux / xbar**（u5-l1、u5-l3、u6-l1）：`axi_mux` 用 `axi_id_prepend` 把来源端口号拼进 ID 高位、再解码高位路由响应；`axi_demux` 用 `axi_demux_id_counters` 跟踪每个 ID 的在途事务以保序；`axi_xbar` = demux 阵列 + cross 矩阵 + mux 阵列。本讲会反复与它们对照。
- **`xbar_cfg_t` 与 `xbar_latency_e`**（u2-l2）：交叉开关的配置结构体与「在哪条通道插 spill 寄存器」的 10 位单热点位掩码。
- **spill_register 与 FIFO 切路径**（u7-l1）：cut 组合路径、增加一拍延迟的基础设施。

一个关键直觉先记在心里：AXI4-Lite 没有 ID，但又**强制保序**（响应必须按请求顺序返回）。这两条性质合起来，决定了 Lite 互联只能用「按到达顺序排列的路由决策 FIFO」来回放响应方向的路由——这就是本讲的全部核心。

## 3. 本讲源码地图

| 文件 | 作用 |
|:-----|:-----|
| [src/axi_lite_mux.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mux.sv) | AXI-Lite 多路汇聚器内核 `axi_lite_mux` 及接口外壳 `axi_lite_mux_intf`，把 `NoSlvPorts` 个 slave 端口合成 1 个 master 端口。 |
| [src/axi_lite_demux.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_demux.sv) | AXI-Lite 多路拆分器内核 `axi_lite_demux` 及接口外壳 `axi_lite_demux_intf`，把 1 个 slave 端口按外部 select 拆到 `NoMstPorts` 个 master 端口。 |
| [src/axi_lite_xbar.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_xbar.sv) | 全连接 AXI-Lite 交叉开关 `axi_lite_xbar` 及接口外壳 `axi_lite_xbar_intf`，组合 demux 阵列、cross 矩阵与 mux 阵列。 |
| [doc/axi_lite_xbar.md](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/doc/axi_lite_xbar.md) | 交叉开关的官方文档：配置字段表、地址映射、译码错误、流水线与时序、保序与停顿。 |
| [test/tb_axi_lite_xbar.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_xbar.sv) | 6 主 8 从的定向随机验证测试台，是综合实践的缩放范本。 |
| [src/axi_pkg.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv) | 提供 `xbar_cfg_t`、`xbar_latency_e`、`xbar_rule_64_t/32_t`、`RESP_DECERR` 等公共类型与常量。 |

## 4. 核心概念与源码讲解

### 4.1 为什么 AXI-Lite 互联不需要 ID 跟踪

#### 4.1.1 概念说明

回忆完整 AXI4 的两个多路模块（u5-l1、u5-l3）是如何把响应路由回去的：

- `axi_mux`（N→1）：每个 slave 端口用 `axi_id_prepend` 把端口号拼进 ID 最高位，于是 master 端口的 ID 比 slave 端口宽。B/R 响应回来时，只需**解码 ID 的高位**就能知道该把响应送回哪个 slave 端口。路由信息天然藏在 ID 里。
- `axi_demux`（1→N）：因为同一个 ID 的事务可能被地址译码拆到不同 master 端口，而 AXI 又要求同 ID 同方向保序，所以它必须用 `axi_demux_id_counters`（一张以 ID 为索引的在途计数表）来记住「这个 ID 的在途事务去了哪个端口」，并在同 ID 去往不同端口时**主动停顿**第二笔。

这两套机制**都依赖 `id` 字段的存在**。而 AXI4-Lite 没有 `id` 字段（u12-l1），所以两条路都走不通。但 AXI4-Lite 有两条补偿性质：

1. **强制保序**：没有 ID 就无法区分乱序，规范规定同一方向的响应必须**严格按请求顺序**返回。
2. **单拍事务**：每个 AW 恰好配一个 W、回一个 B；每个 AR 回一个 R。不存在「一个 AW 配多拍 W」的复杂配对。

这两条性质合起来给出了 Lite 专用的路由回放机制：**用一个深度为 `MaxTrans` 的 FIFO 把每次请求方向的路由决策（`select`）按顺序记下来，响应方向再按 FIFO 队头把响应送回正确的端口**。因为强制保序，FIFO 的入队顺序与出队顺序严格一致，所以不需要任何 ID、不需要任何 per-ID 的查表——这就是 Lite 互联「无需 ID 跟踪」的真正含义。

#### 4.1.2 核心流程：select FIFO 链

Lite 互联（无论 mux 还是 demux）的请求/响应方向都遵循同一条「select FIFO 链」范式：

```
请求方向（AW/AR）：
  做出路由决策 select  ──push──►  select FIFO（深度 MaxTrans）
  同时把请求送到目标端口

响应方向（B/R）：
  从目标端口收到响应
  select FIFO ──pop/peek──►  得到当初的 select
  按 select 把响应送回原端口
```

- 写通路是 **两级 FIFO**：AW 决策先进 `w_fifo`（决定 W 拍走哪个端口），W 拍转发时再把 `w_select` 推进 `b_fifo`（决定 B 响应回哪个端口）。
- 读通路是 **一级 FIFO**：AR 决策进 `r_fifo`，R 响应直接按 `r_fifo` 队头回送。

#### 4.1.3 与完整 AXI4 的对照

| 维度 | 完整 AXI4 | AXI-Lite（本讲） |
|:-----|:----------|:-----------------|
| 有无 `id` | 有 | **无** |
| mux 响应路由 | 解码 ID 高位（`axi_id_prepend`） | **select FIFO**（`b_fifo`/`r_fifo`） |
| demux 保序 | `axi_demux_id_counters` 在途计数表 | **select FIFO**（强制保序，无需 per-ID 表） |
| master 端口 ID 宽度 | 比 slave 宽 ⌈log₂ N⌉ | **与 slave 相同**（无 ID 可扩展） |
| ATOP 原子操作 | 需特殊处理（inject 计数） | **不支持**（Lite 无 atop） |
| 译码错误从端 | `axi_err_slv`（RESP_DECERR） | 同样是 `axi_err_slv`，但经 `axi_lite_to_axi` 适配 |

一句话总结：**Lite 用「FIFO 保序」换掉了「ID 路由」，于是 mux 和 demux 都退化为同一套 select-FIFO 机制，面积和复杂度都更低。**

#### 4.1.4 代码实践

**实践目标**：在脑子里完成一次「ID 路由」与「FIFO 路由」的对照，巩固核心概念。

**操作步骤**：

1. 打开 u5-l3 讲过的 `axi_mux` 源码，找到它例化 `axi_id_prepend` 的位置；再打开本讲的 `axi_lite_mux`，确认它**没有任何 `id_prepend`**，而是例化三个 `fifo_v3`。
2. 打开 u5-l2 讲过的 `axi_demux_id_counters`，确认它是一张以 ID 低位为索引的表；再打开本讲的 `axi_lite_demux`，确认它**没有任何 id_counters**，只有 select FIFO。

**需要观察的现象**：两份 Lite 源码里搜不到 `id`、`atop`、`id_counter`，但都能看到 `w_fifo` / `b_fifo` / `r_fifo` 三个 `fifo_v3`。

**预期结果**：你能用一句话向同事解释「Lite 没有 ID，凭什么还能把响应送回正确的端口」——答案是「靠 select FIFO 按请求顺序回放」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 Lite 的 `MaxTrans` 设为 1，select FIFO 退化为什么样的结构？对吞吐有什么影响？

> **答案**：FIFO 深度为 1，退化为「单槽寄存器」，任一时刻每个方向只允许一笔在途事务，请求方向必须等响应回来才能发下一笔，吞吐最低但面积最小、无任何并发。

**练习 2**：完整 AXI4 的 mux「master 端口 ID 更宽」，而 Lite mux「master 与 slave 端口宽度相同」。为什么 Lite 不需要加宽？

> **答案**：Lite 根本没有 ID 字段，也就无从加宽；路由信息不靠 ID 携带，而靠 select FIFO。加宽只对「把端口号编码进 ID」的方案有意义。

### 4.2 axi_lite_mux：N→1 汇聚与 select FIFO 链

#### 4.2.1 概念说明

`axi_lite_mux` 把 `NoSlvPorts` 个 slave 端口（接上游 master 模块）汇聚成 1 个 master 端口（接下游 slave 模块）。请求方向用**轮询仲裁**（round-robin）从多个 slave 端口里挑一个放行；响应方向因为只有 1 个 master 端口，不需要仲裁，只需要按 select FIFO 把 B/R 送回当初发起请求的那个 slave 端口。

它和完整 `axi_mux` 一样有「AW→W 依赖可能死锁」的问题，所以也保留了 `lock_aw_valid` 锁存机制来解耦。

#### 4.2.2 核心流程

写通路（两级 FIFO）：

```
AW 仲裁(rr_arb_tree, LockIn=1) ──► aw_select(哪个 slave 端口)
        │ push aw_select
        ▼
      w_fifo ──► w_select ──► 选通 W 拍送到 master 端口
        │ W 拍握手时 pop，并把 w_select push
        ▼
      b_fifo ──► b_select ──► B 响应送回 b_select 指向的 slave 端口
```

读通路（一级 FIFO）：

```
AR 仲裁(rr_arb_tree, LockIn=1) ──► ar_select(哪个 slave 端口)
        │ push ar_select
        ▼
      r_fifo ──► r_select ──► R 响应送回 r_select 指向的 slave 端口
```

关键约束：

- `w_fifo` 满则压住新 AW（`aw_valid` 不放出），防止路由决策丢失。
- `b_fifo` 满则压住 W 拍（`mst_w_valid` 置 0），防止 B 路由决策丢失。
- `lock_aw_valid_q`：AW 仲裁已决、w_fifo 已 push，但 master 侧没握手时，锁住 valid 不撤，同时阻止下一次 push——这切断了「AW 握手依赖 W FIFO」的组合环，是防死锁的关键（与 u5-l3 的 `lock_aw_valid` 同源）。

#### 4.2.3 源码精读

**端口与参数**（[axi_lite_mux.sv:23-54](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mux.sv#L23-L54)）：注意 `slv_reqs_i`/`slv_resps_o` 是长度 `NoSlvPorts` 的数组，`mst_req_o`/`mst_resp_i` 是单端口；`MaxTrans` 决定三个 FIFO 的深度；`Spill*` 是各通道的可选 spill 寄存器开关。

**退化分支**（[axi_lite_mux.sv:56-122](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mux.sv#L56-L122)）：当 `NoSlvPorts==1` 时无需仲裁，五个通道各退化成一个 `spill_register` 直通，连 FIFO 都不需要。

**AW 轮询仲裁**（[axi_lite_mux.sv:199-216](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mux.sv#L199-L216)）：`rr_arb_tree` 开了 `LockIn=1`，意味着一旦选中某路就锁到它握手完成，`idx_o` 给出 `aw_select`（胜出的 slave 端口号）。

**AW 锁存与 w_fifo push 控制**（[axi_lite_mux.sv:219-250](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mux.sv#L219-L250)）：这段是防死锁的核心。当仲裁胜出且 w_fifo 不满，先 push 路由决策、再尝试在 master 侧握手；若 master 侧当拍不收，则置 `lock_aw_valid_d=1` 并锁住，下一拍只把 valid 送出、不再 push，直到握手完成。注释明确写着「This FF removes AW to W dependency」。

**w_fifo：存 AW 的路由决策**（[axi_lite_mux.sv:252-268](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mux.sv#L252-L268)）：`data_i` 是 `aw_select`，输出 `w_select` 用来选通对应的 W 拍（见 [axi_lite_mux.sv:288-294](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mux.sv#L288-L294)）。

**b_fifo：把 W 的路由决策传给 B**（[axi_lite_mux.sv:296-312](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mux.sv#L296-L312)）：W 拍握手（`w_fifo_pop`）时把 `w_select` push 进 b_fifo；B 响应到来时按 `b_select`（b_fifo 队头）把 valid 只点亮对应 slave 端口（[axi_lite_mux.sv:332-337](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mux.sv#L332-L337)）。

**读通路**（AR 仲裁 + r_fifo + R 回送）：AR 仲裁在 [axi_lite_mux.sv:362-379](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mux.sv#L362-L379)，r_fifo 在 [axi_lite_mux.sv:387-403](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mux.sv#L387-L403)，R 按 `r_select` 回送在 [axi_lite_mux.sv:423-428](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mux.sv#L423-L428)。

#### 4.2.4 代码实践

**实践目标**：跟踪 `axi_lite_mux` 一次完整写事务的 select FIFO 流转。

**操作步骤**：

1. 假设 `NoSlvPorts=2`，slave 端口 1 发起一笔写。在 [axi_lite_mux.sv:199-216](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mux.sv#L199-L216) 处确认 `aw_select` 取值为 1。
2. 跟到 [axi_lite_mux.sv:252-268](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mux.sv#L252-L268)，`aw_select=1` 被 push 进 `w_fifo`，`w_select` 随后变为 1。
3. 跟到 [axi_lite_mux.sv:288-294](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mux.sv#L288-L294)，确认 master 端口的 W 拍取自 `slv_reqs_i[w_select]`（即端口 1 的 W），且 `w_fifo_pop` 把 `w_select=1` push 进 b_fifo。
4. 跟到 [axi_lite_mux.sv:332-337](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mux.sv#L332-L337)，确认 B 响应只点亮 `b_select==1` 的那个 slave 端口。

**预期结果**：你能画出 `aw_select(1) → w_fifo → w_select(1) → b_fifo → b_select(1) → B 回端口 1` 这条链路，验证「响应按请求顺序回放」成立。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `b_fifo` 的 push 时机是「W 拍握手」而不是「AW 握手」？

> **答案**：因为 B 响应在 W 拍完成之后才产生。把 b_fifo 的 push 放在 W 握手时刻，保证 B 路由决策的入队顺序与「W 拍实际完成顺序」一致；而 W 拍的顺序又由 w_fifo 保证与 AW 一致，整条链于是严格保序。

**练习 2**：`lock_aw_valid_q` 在什么情况下会被置位？置位后下一拍的行为是什么？

> **答案**：当 AW 仲裁已胜出、w_fifo 已 push，但 master 侧当拍没有握手（`mst_aw_ready==0`）时置位。下一拍进入 `if (lock_aw_valid_q)` 分支，只持续送出 `mst_aw_valid` 而不再 push 新决策，直到 master 侧收下、握手完成后清零。

### 4.3 axi_lite_demux：1→N 路由与外部 select

#### 4.3.1 概念说明

`axi_lite_demux` 是 `axi_lite_mux` 的对偶：把 1 个 slave 端口按**外部喂入的 `slv_aw_select_i`/`slv_ar_select_i`** 路由到 `NoMstPorts` 个 master 端口之一。这与 u5-l1 讲过的 `axi_demux_simple` 同样遵循「**译码在外、路由在内**」的分工——demux 自己不算地址，地址到端口号的映射由调用方（在 xbar 里是 `addr_decode`）算好后再喂进来。

由于同样没有 ID，demux 也用 select FIFO 链保序回送响应；区别只在于方向相反（1→N），所以 FIFO 存的是「目标 master 端口号」，响应回来时按队头端口号选通对应 master 的 B/R。

#### 4.3.2 核心流程

写通路：

```
slv_req_i.aw + slv_aw_select_i ──spill(打包成 aw_chan_select_t)──► slv_aw_chan
        │ w_fifo push select
        ▼
      w_fifo ──► w_select ──► 只点亮 w_select 指向的 master 端口的 W valid
        │ W 拍握手时把 w_select push
        ▼
      b_fifo ──► b_select ──► 从 b_select 指向的 master 端口取 B，送回 slave
```

读通路：

```
slv_req_i.ar + slv_ar_select_i ──spill──► slv_ar_chan
        │ r_fifo push select
        ▼
      r_fifo ──► r_select ──► 从 r_select 指向的 master 端口取 R，送回 slave
```

注意一个细节：AW 和 AR 通道的 select 被打包进结构体 `{aw/ar; select}` 一起进 spill 寄存器（[axi_lite_demux.sv:62-69](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_demux.sv#L62-L69)），这样 select 与通道载荷一起被切断组合路径、一起满足「valid 期间稳定」的约束。

#### 4.3.3 源码精读

**端口**（[axi_lite_demux.sv:27-57](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_demux.sv#L27-L57)）：相比 mux，它多了 `slv_aw_select_i` / `slv_ar_select_i` 两个输入（位宽 ⌈log₂ NoMstPorts⌉），`mst_reqs_o`/`mst_resps_i` 是长度 `NoMstPorts` 的数组。

**select 与通道打包**（[axi_lite_demux.sv:62-69](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_demux.sv#L62-L69)）：定义 `aw_chan_select_t = {aw_chan_t aw; select_t select}`，让 select 随通道一起进 spill（[axi_lite_demux.sv:206-220](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_demux.sv#L206-L220)）。这里还有一段 Questa 工具的 `$bits` 兼容 workaround（[axi_lite_demux.sv:195-203](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_demux.sv#L195-L203)），把结构体扁平化成 logic 向量再喂给 spill，是工程上的小细节。

**AW 路由与锁存**（[axi_lite_demux.sv:223-268](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_demux.sv#L223-L268)）：把 AW 载荷广播到所有 master 端口，但只点亮 `select` 指向的那一个的 `aw_valid`；同样有 `lock_aw_valid_q` 防 AW→W 依赖死锁；w_fifo 在 AW 决策时 push select。

**W/B/R 通路**：W 按队头 `w_select` 选通（[axi_lite_demux.sv:306-312](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_demux.sv#L306-L312)），B 按队头 `b_select` 从对应 master 取回（[axi_lite_demux.sv:349-355](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_demux.sv#L349-L355)），R 按队头 `r_select` 取回（[axi_lite_demux.sv:429-441](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_demux.sv#L429-L441)）。结构与 mux 完全对称。

**断言**（[axi_lite_demux.sv:446-468](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_demux.sv#L446-L468)）：`assume` 要求 select 必须落在 `[0, NoMstPorts)` 内；`assert` 要求 valid 期间 select 与载荷稳定。这些是 demux 对调用方的契约——在 xbar 里由 `addr_decode` 的输出满足。

#### 4.3.4 代码实践

**实践目标**：理解 demux 的 select 必须由外部正确驱动。

**操作步骤**：

1. 阅读 [axi_lite_demux.sv:447-454](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_demux.sv#L447-L454) 的 `aw_select`/`ar_select` 两条 `assume property`：它们要求 `slv_aw_select_i < NoMstPorts` 且 `slv_ar_select_i < NoMstPorts`。
2. 思考：如果外部（例如某个错误的地址译码器）给了一个越界的 select，仿真会发生什么？

**预期结果**：仿真会在该 `assume` 处 `$fatal`，提示「selected a slave that is not defined」。这说明 **select 的合法性责任在调用方**，demux 只负责忠实路由。

**待本地验证**：若手头有仿真器，可构造一个故意给越界 select 的极小测试台，观察是否如预期触发 fatal。

#### 4.3.5 小练习与答案

**练习 1**：demux 把 AW 载荷广播到所有 master 端口（`mst_reqs_o[i].aw = slv_aw_chan.aw`），但只点亮一个 valid。为什么不只连一个？

> **答案**：这是 u5-l1 讲过的「全广播载荷 + 选择性拉 valid」范式：广播载荷让综合工具看到所有端口的 AW 是同一个信号副本（面积小、多路选择器更简单），只用 valid 选通，避免为每路各做一个 mux。读 `b_ready`/`r_ready` 也用同样的 `(select == i)` 逐路选通。

**练习 2**：`MaxTrans` 在 demux 里同时约束了 w_fifo、b_fifo、r_fifo 三个深度。如果设得过小会怎样？

> **答案**：FIFO 深度小 → 在途事务上限低 → 上游请求更快被反压（`slv_aw_ready`/`slv_ar_ready` 更易被压住）→ 吞吐下降，但功能仍正确。需匹配上游 master 的最大并发度。

### 4.4 axi_lite_xbar：全连接交叉开关

#### 4.4.1 概念说明

`axi_lite_xbar` 是 Lite 多路复用族的「整机」，把 mux 和 demux 组合成一个**全连接**交叉开关：每个 slave 端口（接上游 master）都能访问每个 master 端口（接下游 slave），拓扑上任意一对主从之间都有直连路径。它的组装方式和完整 `axi_xbar`（u6-l1）同构，但去掉了所有 ID 相关逻辑：

```
                 ┌─ demux (每 slave 端口 1 个) ─┐   ┌─ mux (每 master 端口 1 个) ─┐
slave_port[i] ──► addr_decode ──► axi_lite_demux ──cross 矩阵──► axi_lite_mux ──► master_port[j]
                                  (NoMstPorts+1 路)              (NoSlvPorts 路)
                                        │
                                        └─ 最后一路接 axi_err_slv（译码错误兜底）
```

每个 slave 端口例化一个 demux（输出 `NoMstPorts+1` 路，多出的一路给译码错误从端），每个 master 端口例化一个 mux（输入 `NoSlvPorts` 路），中间用一组 `assign` 把 demux 输出与 mux 输入交叉相连（cross 矩阵）。

#### 4.4.2 核心流程

一次 Lite 读写穿越 xbar 的流程：

1. 上游 master 在 `slave_port[i]` 发起 AW/AR，地址为 `addr`。
2. 该 slave 端口的 `addr_decode`（读写各一个）查 `addr_map_i`，得到目标 master 端口号 `dec_aw`/`dec_ar`；若所有规则都不命中且未启用默认端口，则 `dec_*_error=1`，select 被改写为 `NoMstPorts`（译码错误从端）。
3. `axi_lite_demux` 按 select 把请求送到对应 master 端口的 demux 输出槽。
4. cross 矩阵 `assign mst_reqs[j][i] = slv_reqs[i][j]` 把请求搬到 mux 的第 j 个输入。
5. `axi_lite_mux` 轮询仲裁所有 slave 端口的请求，送到 `master_port[j]`（下游 slave）。
6. 响应反向回流：B/R 经 mux 的 select FIFO、cross 矩阵、demux 的 select FIFO，按请求顺序回到 `slave_port[i]`。

**译码错误处理**：未命中且无默认端口时，请求落到 demux 的第 `NoMstPorts` 路，经 `axi_lite_to_axi` 协议转换后送给 `axi_err_slv`，后者无条件吸收事务并回 `RESP_DECERR`，读响应每拍数据为 `0xBADCAB1E`（见 doc/axi_lite_xbar.md 第 18-22 行）。

#### 4.4.3 源码精读

**顶层与派生参数**（[axi_lite_xbar.sv:22-45](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_xbar.sv#L22-L45)）：`Cfg` 是 `axi_pkg::xbar_cfg_t`；`MstIdxWidth`（[axi_lite_xbar.sv:33](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_xbar.sv#L33)）只用于默认端口索引的位宽，**与 ID 无关**——注意它和完整 xbar「master 端口 ID 加宽」的含义完全不同。

**多出一路给译码错误**（[axi_lite_xbar.sv:51](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_xbar.sv#L51)，[axi_lite_xbar.sv:63-64](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_xbar.sv#L63-L64)）：`mst_port_idx_t` 位宽是 `$clog2(NoMstPorts+1)`，内部 `slv_reqs`/`slv_resps` 数组的第二维是 `[NoMstPorts:0]`，比真实 master 端口多一格。

**每 slave 端口的地址译码**（[axi_lite_xbar.sv:79-107](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_xbar.sv#L79-L107)）：AW、AR 各例化一个 `addr_decode`，查同一张 `addr_map_i`；`dec_error` 命中时把 select 改写为 `NoMstPorts`（[axi_lite_xbar.sv:109-112](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_xbar.sv#L109-L112)）。

**默认端口的运行期稳定性断言**（[axi_lite_xbar.sv:118-141](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_xbar.sv#L118-L141)）：与完整 xbar 一样，要求在任一 AW/AR 处于 `valid && !ready` 期间，`en_default_mst_port_i` 与 `default_mst_port_i` 必须保持稳定，否则 `$fatal`。

**demux 例化（NoMstPorts+1 路）**（[axi_lite_xbar.sv:142-168](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_xbar.sv#L142-L168)）：注意 `NoMstPorts(Cfg.NoMstPorts + 1)`，五个 `Spill*` 取自 `Cfg.LatencyMode` 的高 5 位（bit 9..5，对应 demux 侧）。

**译码错误从端**（[axi_lite_xbar.sv:172-202](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_xbar.sv#L172-L202)）：先用 `axi_lite_to_axi` 把 Lite 请求升格为完整 AXI（因为 `axi_err_slv` 是按完整 AXI 写的），再例化 `axi_err_slv`（`Resp=RESP_DECERR`、`ATOPs=0`、`MaxTrans=1`，因 Lite 单拍）。

**cross 矩阵**（[axi_lite_xbar.sv:206-211](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_xbar.sv#L206-L211)）：两层 `for` 循环，`assign mst_reqs[j][i] = slv_reqs[i][j]` 与 `assign slv_resps[i][j] = mst_resps[j][i]`，纯组合、零逻辑，把 demux 阵列与 mux 阵列转置相连。

**mux 例化（每 master 端口 1 个）**（[axi_lite_xbar.sv:213-239](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_xbar.sv#L213-L239)）：五个 `Spill*` 取自 `Cfg.LatencyMode` 的低 5 位（bit 4..0，对应 mux 侧）。

**LatencyMode 位映射**：可对照 [axi_pkg.sv:451-479](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L451-L479) 的 `DemuxAw..MuxR` 单热点定义——高 5 位（bit 9..5）给 demux、低 5 位（bit 4..0）给 mux，与完整 xbar 完全一致。

#### 4.4.4 代码实践

**实践目标**：在源码里数清楚 xbar 一共例化了多少个 demux、多少个 mux、多少个译码错误从端。

**操作步骤**：

1. 在 [axi_lite_xbar.sv:70](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_xbar.sv#L70) 的 `gen_slv_port_demux` 循环确认：循环 `NoSlvPorts` 次，每次例化 1 个 demux + 1 个 `axi_lite_to_axi` + 1 个 `axi_err_slv` + 2 个 `addr_decode`。
2. 在 [axi_lite_xbar.sv:213](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_xbar.sv#L213) 的 `gen_mst_port_mux` 循环确认：循环 `NoMstPorts` 次，每次例化 1 个 mux。
3. 对照 doc/axi_lite_xbar.md 的框图（第 10 行）核对。

**预期结果**：demux 数 = `NoSlvPorts`，mux 数 = `NoMstPorts`，译码错误从端数 = `NoSlvPorts`（每 slave 端口一个），`addr_decode` 数 = `2 × NoSlvPorts`（读写各一）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `axi_err_slv` 要经 `axi_lite_to_axi` 转换才能接 Lite demux？

> **答案**：`axi_err_slv` 是按完整 AXI4 写的（端口是 `full_req_t`/`full_resp_t`，带 id/user 字段），而 demux 的第 `NoMstPorts` 路是 Lite 类型。`axi_lite_to_axi`（u13-l1 会详讲）把 Lite 请求升格为完整 AXI（id/user 补 0），让类型匹配；多余字段稳定为常数，综合时会被优化掉。

**练习 2**：doc/axi_lite_xbar.md 第 75-77 行说「xbar 内部不插流水线，理由同 axi_xbar」。结合本讲的 FIFO 链，说明这句话与 4.2 节的 select FIFO 是否矛盾？

> **答案**：不矛盾。「内部不插流水线」指的是 demux 输出与 mux 输入之间（cross 矩阵处）不能插 spill/FIFO，否则会破坏 W 通道的保序、形成死锁环（与 u6-l3 的 Coffman 分析一致）。而 select FIFO 是模块**内部**用于路由回放的逻辑，位于 demux/mux 各自内部，不在 cross 矩阵处，二者是不同层面的东西。

## 5. 综合实践

设计并实现一个 **2 主 2 从的 `axi_lite_xbar` 测试台**，验证两个 master 能各自访问到正确的 slave，并把第 4 节的 mux/demux/xbar 串起来。

### 5.1 实践目标

- 用 `axi_lite_xbar_intf` 搭一个 2×2 全连接 Lite 互联。
- 配置一张 2 条规则的地址映射，把地址空间平分给两个 slave。
- 用 `axi_lite_rand_master` 发定向写/读，验证 master 0 写到 slave 0、master 1 写到 slave 1，并观察访问未映射地址时返回 `DECERR`。

### 5.2 操作步骤

范本是 [test/tb_axi_lite_xbar.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_xbar.sv)（6 主 8 从）。把它缩成 2×2，核心改动如下（**示例代码**，非项目原有文件）：

1. **改参数**（对照 [tb_axi_lite_xbar.sv:26-27](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_xbar.sv#L26-L27)）：

```systemverilog
localparam int unsigned NoMasters = 32'd2;
localparam int unsigned NoSlaves  = 32'd2;
localparam int unsigned NoWrites  = 32'd100;
localparam int unsigned NoReads   = 32'd100;
```

2. **配置 `xbar_cfg`**（对照 [tb_axi_lite_xbar.sv:40-51](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_xbar.sv#L40-L51)）：`NoSlvPorts=NoMasters`、`NoMstPorts=NoSlaves`，`LatencyMode` 仍用推荐的 `axi_pkg::CUT_ALL_AX`、`FallThrough=0`，`NoAddrRules=2`。

3. **定义 2 条地址映射规则**（对照 [tb_axi_lite_xbar.sv:57-66](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_xbar.sv#L57-L66)）：

```systemverilog
localparam rule_t [1:0] AddrMap = '{
  '{idx: 32'd1, start_addr: 32'h0000_8000, end_addr: 32'h0001_0000}, // slave 1
  '{idx: 32'd0, start_addr: 32'h0000_0000, end_addr: 32'h0000_8000}  // slave 0
};
```

   规则区间前闭后开（u6-l2）：`0x0000_0000..0x0000_8000` 归 slave 0，`0x0000_8000..0x0001_0000` 归 slave 1。

4. **例化 master 与 slave**（对照 [tb_axi_lite_xbar.sv:127-149](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_xbar.sv#L127-L149)）：每个 master 端口接一个 `axi_lite_rand_master`，每个 slave 端口接一个 `axi_lite_rand_slave`（MAPPED 模式，保证写后读一致）。

5. **发定向事务验证路由**（对照 [tb_axi_lite_xbar.sv:135-136](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_xbar.sv#L135-L136)）：

```systemverilog
// master 0 写 slave 0 区间，master 1 写 slave 1 区间
lite_axi_master[0].write(32'h0000_1234, prot, 64'hA5A5, 8'hFF, resp); // 落 slave 0
lite_axi_master[1].write(32'h0000_9ABC, prot, 64'h5A5A, 8'hFF, resp); // 落 slave 1
// 访问未映射地址（如 0x000F_FFFF）应回 DECERR
```

6. **运行仿真**：把改好的 TB 命名为 `tb_my_lite_xbar.sv` 放入 `test/`，执行（参考 u1-l4）：

```bash
make sim-tb_my_lite_xbar.log
```

### 5.3 需要观察的现象

- master 0 的写到达 slave 0（地址 `0x1234` 在 slave 0 区间），master 1 的写到达 slave 1。
- 用 `axi_lite_rand_slave` 的 MAPPED 模式做写后读自检，或外接 `axi_scoreboard`/`axi_sim_mem` 比对（参考 u3-l2）。
- 访问 `0x000F_FFFF`（未映射）时，`resp` 为 `axi_pkg::RESP_DECERR`，读回数据含 `0xBADCAB1E`。

### 5.4 预期结果

- 定向写/读全部返回 `RESP_OKAY`（除刻意触发的 DECERR）。
- 随机段（`run(NoReads, NoWrites)`）结束后，日志出现 `Simulation stopped as all Masters transferred their data, Success.`，且 `Errors: 0`。
- **待本地验证**：具体仿真输出依赖本机工具链与随机种子；若无法运行，至少完成源码侧的路由推演——确认 `0x1234` 经 `addr_decode` 命中 `idx=0`、`0x9ABC` 命中 `idx=1`。

## 6. 本讲小结

- **AXI-Lite 互联不需要 ID 跟踪**：Lite 没有 `id` 字段，但强制保序、单拍事务，于是用一条**深度为 `MaxTrans` 的 select FIFO** 替代了完整 AXI4 的 `axi_id_prepend`（mux）与 `axi_demux_id_counters`（demux）。
- **`axi_lite_mux`（N→1）**：请求方向用 `rr_arb_tree`（`LockIn=1`）轮询选源，响应方向按 select FIFO 队头回送；写通路是 `w_fifo`→`b_fifo` 两级，读通路是 `r_fifo` 一级；`lock_aw_valid` 切断 AW→W 组合依赖防死锁。
- **`axi_lite_demux`（1→N）**：遵循「译码在外、路由在内」，select 由外部（xbar 里是 `addr_decode`）喂入，与通道载荷一起打包进 spill；FIFO 结构与 mux 完全对称，方向相反。
- **`axi_lite_xbar`**：每 slave 端口一个 demux（输出 `NoMstPorts+1` 路，多一路给译码错误）、每 master 端口一个 mux，中间用 cross 矩阵 `assign` 转置相连；译码错误经 `axi_lite_to_axi` 升格后交给 `axi_err_slv` 回 `RESP_DECERR`、数据 `0xBADCAB1E`。
- **配置复用 `xbar_cfg_t`**：`LatencyMode` 高 5 位给 demux、低 5 位给 mux，推荐 `CUT_ALL_AX` + `FallThrough=0`；两个 xbar 双向互连时必须用 `CUT_*_PORTS` 以免成环。
- **选型依据**：若总线无 ID、单拍、需轻量互联（寄存器配置、低速外设），选 Lite 族，面积与复杂度显著低于完整 AXI4；若需要突发、多 ID 并发、原子操作，则必须用完整 `axi_xbar`。

## 7. 下一步学习建议

- **u12-l3 `axi_lite_regs`**： Lite 互联最典型的下游从端——把一片寄存器暴露成 AXI-Lite 从端，可与本讲的 xbar 直接拼成「master → xbar → 多个 lite_regs」的寄存器配置网络。
- **u13-l1 `axi_to_axi_lite` / `axi_lite_to_axi`**：本讲已遇到 `axi_lite_to_axi` 被用作译码错误从端的类型适配，下一单元会讲清两个方向的桥接原理。
- **回顾 u6 全连接 xbar**：对照阅读 `axi_xbar` 与 `axi_lite_xbar`，体会「删掉 ID 后整套设计如何同构简化」是理解本库设计哲学的最佳练习。
- **阅读 doc/axi_lite_xbar.md 第 66-77 行**的 Ordering/Stalls 与「No Pipelining Inside Crossbar」一节，结合 u6-l3 的 Coffman 死锁分析，理解 Lite 版同样遵守的内部不流水约束。
