# cluster 到 L2 的互联

## 1. 本讲目标

本讲聚焦于「SM 核」与「L2 Cache」之间的那段连线网络。学完本讲后，你应该能够：

- 说清一条来自 SM 的访存请求，经过 `sm2cluster_arb`、`l2_distribute`、`cluster_to_l2_arb` 这三级模块最终到达 `Scheduler`（L2）的完整路径；
- 理解请求方向上的「汇聚—分发—再汇聚」三级接力结构，以及 `NUM_CLUSTER`、`NUM_L2CACHE` 两个规模参数如何决定每一级是「真仲裁」还是「直通」；
- 解释 `source` 字段如何在这一段路径上「逐层打标签」（贴 sm_id、贴 cluster_id），又在响应回送时「逐层剥标签」路由回原 SM；
- 能够对照 `GPGPU_top.v` 的例化代码，画出多 SM 到 L2 的互联拓扑。

本讲承接 u7-l1（TileLink 协议与 source 路由）和 u7-l2（L2 Scheduler 内部架构），是把 L1 侧的 source 标签一路「接力」送到 L2、再把 L2 响应原路送回 SM 的关键一跳。

## 2. 前置知识

在进入本讲前，读者应已具备以下认知（来自前面讲义）：

- **TileLink A/D 两通道与 source 路由**（u7-l1）：请求走 A 通道、响应走 D 通道，都用 valid/ready 握手；并发的多个在途事务靠 `source` 字段区分。`source` 是一个「分级编码的回信地址」。
- **source 字段的层层累加**（u6-l1、u6-l3、u7-l1）：L1 dcache 先填入最内层 `A_SOURCE`（tag + entry + set），`l1cache_arb` 在其高位贴上 cache_id 得到 `D_SOURCE`。本讲负责的，正是在 `D_SOURCE` 之上继续贴 sm_id、cluster_id。
- **L2 Scheduler 是四通道黑盒**（u7-l2）：L2 顶层 `Scheduler` 只通过 `sche_in_a_*`（收请求）、`sche_in_d_*`（发响应）、`sche_out_a_*`（发内存请求）、`sche_out_d_*`（收内存响应）对外。本讲只关心 `sche_in_a` / `sche_in_d` 这一对，即「L2 与 SM 之间的那一侧」。
- **公共单元库**（u8-l2 会详讲）：`fixed_pri_arb`（固定优先级仲裁器）、`one2bin`（独热转二进制）、`find_first`（找首个 1）、`stream_fifo`（流式 FIFO）在本讲被反复复用。

> 名词提醒：本项目中 **cluster（簇）= 一个或多个 SM 的编组**，由 `NUM_CLUSTER` 与 `NUM_SM_IN_CLUSTER` 描述；**CU（计算单元）= SM**，`NUMBER_CU` 即 SM 总数。这两个概念在不同讲义里会交替出现，指代同一对象。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `src/gpgpu_top/sm2cluster_arb.v` | **第一级**：把一个 cluster 内的 `NUM_SM_IN_CLUSTER` 个 SM 请求汇聚成一路 cluster 级流，做格式适配、贴 sm_id，并用 FIFO 切断时序路径。 |
| `src/gpgpu_top/l2_distribute.v` | **第二级**：把一路 cluster 级请求按地址分发到 `NUM_L2CACHE` 个 L2，响应方向做固定优先级选路。纯组合逻辑。 |
| `src/gpgpu_top/cluster_to_l2_arb.v` | **第三级**：把 `NUM_CLUSTER` 路 cluster 请求汇聚成一路送进某个 L2 Scheduler，贴 cluster_id，响应方向按 source 高位解复用回各 cluster。纯组合逻辑。 |
| `src/gpgpu_top/GPGPU_top.v` | 顶层，把上述三级（连同 `sm_wrapper`、`Scheduler`）例化并连线，是观察整条互联的「地图」。 |
| `src/define/define.v` | 规模参数（`NUM_CLUSTER`、`NUM_SM_IN_CLUSTER`、`NUM_L2CACHE`）与各级 source 位宽（`D_SOURCE`、`CLUSTER_SOURCE`、`SOURCE_BITS`、`L2C_BITS`）的定义。 |
| `src/common_cell/{fixed_pri_arb,one2bin,find_first,input_reverse}.v` | 三级模块复用的基础组合逻辑单元。 |

## 4. 核心概念与源码讲解

### 4.1 互联总览：三级接力与规模参数

#### 4.1.1 概念说明

为什么要在这中间放一整段「互联」？因为 SM 可能有多个，L2 也可能有多个，二者不是一一对应的：

- 多个 SM 会同时产生访存请求，需要**汇聚（N:1）**成一路才能共享同一条通往 L2 的通路；
- 当存在多个 L2 时，又需要按地址把请求**分发（1:N）**到正确的那个 L2；
- 汇聚到某个 L2 入口前，来自多个 cluster 的请求还需要再**仲裁一次（N:1）**。

于是这段路径天然呈现「汇聚 → 分发 → 再汇聚」的三级接力。Ventus 用三个独立模块分别承担这三级：

```
                 第①级                第②级                 第③级              L2
  SM[0..N-1] ──► sm2cluster_arb ──► l2_distribute ──► cluster_to_l2_arb ──► Scheduler
   (per cluster)   (per cluster)      (per L2)
   N:1 汇聚+贴sm_id   1:N 按地址分发     N:1 汇聚+贴cluster_id
```

每一级都由规模参数决定它是「真做事」还是「直通退化」。三个关键参数：

- `NUM_CLUSTER`：簇数（默认 1）。
- `NUM_SM_IN_CLUSTER` = `NUM_SM / NUM_CLUSTER`：每簇内 SM 数（默认 2）。
- `NUM_L2CACHE`：L2 个数（默认 1）。

#### 4.1.2 核心流程

整段互联是一个**双向**网络：请求自左向右（SM→L2），响应自右向左（L2→SM）。两个方向的处理思想对称但相反：

**请求方向（打标签，由内向外）：**

1. 每个 SM 的 `l1cache_arb` 已产出 `D_SOURCE` 宽的 source（含 cache_id）。
2. **`sm2cluster_arb`**：在簇内多 SM 间固定优先级仲裁，胜出者的 source 高位贴上 **sm_id** → 升级为 `CLUSTER_SOURCE` 宽。
3. **`l2_distribute`**：按地址中 L2C_BITS 位选目标 L2，**不改 source**，只做 1:N 选路。
4. **`cluster_to_l2_arb`**：在多 cluster 间固定优先级仲裁，胜出者的 source 高位贴上 **cluster_id** → 升级为 `SOURCE_BITS` 宽，送入 `Scheduler.sche_in_a`。

**响应方向（剥标签，由外向内）：**

1. `Scheduler.sche_in_d` 回送 `SOURCE_BITS` 宽的响应。
2. **`cluster_to_l2_arb`**：读 source 最高 `CLUSTER_BITS` 位（cluster_id）解复用到对应 cluster，并剥掉 cluster_id → 回到 `CLUSTER_SOURCE` 宽。
3. **`l2_distribute`**：在多 L2 响应间固定优先级选一路通过 → 仍为 `CLUSTER_SOURCE` 宽。
4. **`sm2cluster_arb`**：读 source 最高 `NUM_CLUSTER_DEPTH` 位（sm_id）解复用到对应 SM，并剥掉 sm_id → 回到 `D_SOURCE` 宽，送回 `sm_wrapper.mem_rsp_d_*`。

> 一句话记忆：**请求方向「逐层贴标签」，响应方向「逐层剥标签并按标签选路」**。这与 u7-l1 讲的「source 是分级回信地址」完全一致。

#### 4.1.3 源码精读

规模参数定义在 define.v 最开头一组：

[NUM_CLUSTER / NUM_SM_IN_CLUSTER](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L3-L7) 定义簇数与每簇 SM 数（`NUM_SM_IN_CLUSTER = NUM_SM / NUM_CLUSTER`）。

[NUM_L2CACHE](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L41) 定义 L2 个数。

各级 source 位宽逐级累加，定义在同一区段：

[define.v:107-115](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L107-L115) 给出 `NUM_CACHE_DEPTH`、`D_SOURCE`、`A_SOURCE`、`CLUSTER_SOURCE` 的派生关系。本讲新增的那一段是 `CLUSTER_SOURCE = D_SOURCE + NUM_CLUSTER_DEPTH`（即「D_SOURCE 再加 sm_id 的位宽」）。

[define.v:333](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L333) 定义最外层的 `SOURCE_BITS`，在默认配置下：

\[ \texttt{SOURCE\_BITS} = 3 + \lceil\log_2 \texttt{DCACHE\_MSHRENTRY}\rceil + \lceil\log_2 \texttt{DCACHE\_NSETS}\rceil + \lceil\log_2 \texttt{L2CACHE\_NUM\_SM\_IN\_CLUSTER}\rceil + \lceil\log_2 \texttt{L2CACHE\_NUM\_CLUSTER}\rceil + 1 = 3+2+5+1+0+1 = 12 \]

`l2_distribute` 用于选 L2 的地址位宽 `L2C_BITS`，以及与其配套的 `TAG_BITS`：

[define.v:365-367](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L365-L367) 定义 `L2C_BITS = $clog2(NUM_L2CACHE)`，并把 `TAG_BITS` 减去 `L2C_BITS`——即「从 tag 字段里切出 L2C_BITS 位」用来选 L2。

> 默认配置的退化现象：`NUM_CLUSTER=1`、`NUM_L2CACHE=1` 时，`L2C_BITS=0`、`CLUSTER_BITS=0`。因此第②级 `l2_distribute` 退化为直通（只有 1 个 L2 可选），第③级 `cluster_to_l2_arb` 也退化为直通（只有 1 个 cluster）。**默认配置下真正「在干活」的只有第①级 `sm2cluster_arb`**（2 个 SM 汇聚成 1 路）。第②③级的代码是为「多 L2 / 多 cluster」扩展预留的骨架。

source 字段在默认配置下的逐层布局（MSB 在左）：

| 阶段 | 宽度 | 布局（自高到低） | 由谁打标 |
| --- | --- | --- | --- |
| `A_SOURCE` | 10 | `[tag(3)] [entry(2)] [set(5)]` | L1 dcache（u6-l1） |
| `D_SOURCE` | 11 | `[cache_id(1)] [tag(3)] [entry(2)] [set(5)]` | l1cache_arb（u6-l3） |
| `CLUSTER_SOURCE` | 12 | `[sm_id(1)] [cache_id(1)] [tag(3)] [entry(2)] [set(5)]` | **sm2cluster_arb（本讲 4.2）** |
| `SOURCE_BITS` | 12 | `[cluster_id(0)] [sm_id(1)] [cache_id(1)] [tag(3)] [entry(2)] [set(5)]` | **cluster_to_l2_arb（本讲 4.4）** |

三级模块在 `GPGPU_top.v` 中的例化位置构成一张完整的「连线地图」。L2 与第③级在同一组 generate 中例化：

[GPGPU_top.v:402-483](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L402-L483)：循环 `NUM_L2CACHE` 次，每次例化一个 `Scheduler`（[L406](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L406)）和一个 `cluster_to_l2_arb`（[L445](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L445)）。

第①级与第②级在另一组 generate 中例化：

[GPGPU_top.v:485-561](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L485-L561)：循环 `NUM_CLUSTER` 次，每次例化一个 `sm2cluster_arb`（[L487](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L487)）和一个 `l2_distribute`（[L523](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L523)）。

可以看到，连接两个 generate 的「中间网线」正是 `mem_req_out_*` / `mem_rsp_in_*`（cluster 级，CLUSTER_SOURCE 宽）和 `mem_req_vec_out_*` / `mem_rsp_vec_in_*`（cluster×L2 二维）。

#### 4.1.4 代码实践

**实践目标**：在 `GPGPU_top.v` 中根据「网线命名」重建三级拓扑。

**操作步骤**：
1. 打开 `src/gpgpu_top/GPGPU_top.v`，定位 L487 的 `sm2cluster_arb` 例化，确认其输入 `mem_req_vec_in_*` 接的是 `mem_req_*[(k+1)*NUM_SM_IN_CLUSTER-1-:NUM_SM_IN_CLUSTER]`（即来自 SM 的 packed 向量），输出 `mem_req_out_*` 接 `mem_req_out_*[(k+1)*...]`。
2. 定位 L523 的 `l2_distribute` 例化，确认其输入正是上一步 `sm2cluster_arb` 的输出 `mem_req_out_*[k]`，输出为 `mem_req_vec_out_*[(k+1)*NUM_L2CACHE-1-:NUM_L2CACHE]`。
3. 定位 L445 的 `cluster_to_l2_arb` 例化，确认其输入 `mem_req_vec_in_*` 接的是 `mem_req_vec_out_*[(j+1)*NUM_CLUSTER-1-:NUM_CLUSTER]`，输出接 `Scheduler` 的 `sche_in_a_*`。

**需要观察的现象**：第①②级按 `NUM_CLUSTER` 循环（per cluster），第③级按 `NUM_L2CACHE` 循环（per L2）；二者通过 `mem_req_vec_out_*` 这组「cluster×L2 二维网线」拼接，这正是「分发」的物理体现。

**预期结果**：你应当能画出一张三个 generate 块、用两组中间网线串起来的拓扑图（见本讲「综合实践」的参考图）。

#### 4.1.5 小练习与答案

**练习 1**：若把 `NUM_L2CACHE` 从 1 改为 2，第②级 `l2_distribute` 的行为会发生什么变化？
**答案**：`L2C_BITS` 从 0 变为 1，`l2_distribute` 不再是直通，而是按地址中切出的那 1 位把请求分发到两个 L2 之一；`TAG_BITS` 同步减 1。

**练习 2**：默认配置下 `SOURCE_BITS` 与 `CLUSTER_SOURCE` 都等于 12，为什么 `cluster_to_l2_arb` 仍要把 source 「升级」到 `SOURCE_BITS`？
**答案**：因为默认 `NUM_CLUSTER=1`，`CLUSTER_BITS=$clog2(1)=0`，无需再贴 cluster_id，故二者恰好相等；当 `NUM_CLUSTER>1` 时 `SOURCE_BITS` 才会比 `CLUSTER_SOURCE` 更宽，多出的高位承载 cluster_id。

---

### 4.2 sm2cluster_arb：SM 请求汇聚、格式适配与 sm_id 打标

#### 4.2.1 概念说明

`sm2cluster_arb` 是「每簇一个」的模块（由 `NUM_CLUSTER` 循环例化）。它要同时完成三件事：

1. **汇聚**：把簇内 `NUM_SM_IN_CLUSTER` 个 SM 的请求（valid 向量）用固定优先级选出一个；
2. **格式适配 + 打标**：把 SM 侧「原始」接口字段（`a_addr/a_data/a_source` 等）重新打包成 TileLink 风格（`ADDRESS_BITS/DATA_BITS/CLUSTER_SOURCE` 等），并在 source 高位贴上 sm_id；
3. **时序切断**：用一颗深度为 2 的 `stream_fifo` 隔离 SM 侧与 cluster 侧的时序，避免长组合路径。

它是本段互联中**唯一带时钟的模块**（接口含 `clk/rst_n`），其余两级都是纯组合逻辑。

#### 4.2.2 核心流程

请求方向（伪代码）：

```
// 1. 仲裁：在 NUM_SM_IN_CLUSTER 个 valid 中选最低索引
grant_oh   = fixed_pri_arb(mem_req_vec_in_valid_i)
grant_bin  = one2bin(grant_oh)          // 胜出 SM 的编号 = sm_id

// 2. 数据选通 + source 打标
data_fields = mux(mem_req_vec_in_*_i, by=grant_bin)
source_out  = { grant_bin, source_in[grant_bin] }   // 高位贴 sm_id → CLUSTER_SOURCE 宽
size_out    = 'h0                                   // size 字段固定 0

// 3. 入 FIFO（切断时序），出口即 cluster 级请求
FIFO.push( {opcode, size, source, address, mask, data, param} )
mem_req_out_*  = FIFO.pop()
```

响应方向（伪代码）：

```
// 读 source 最高 NUM_CLUSTER_DEPTH 位 = sm_id，解复用到对应 SM
for each SM i:
    valid_o[i] = (source_i[top NUM_CLUSTER_DEPTH bits] == i) && rsp_valid_i
    source_o[i] = source_i[D_SOURCE-1:0]     // 剥掉 sm_id → D_SOURCE 宽
rsp_ready_o = ready_i[routed_sm_index]
```

#### 4.2.3 源码精读

仲裁与编号转换复用公共单元库：

[sm2cluster_arb.v:102-115](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm2cluster_arb.v#L102-L115)：`fixed_pri_arb`（参数 `ARB_WIDTH=NUM_SM_IN_CLUSTER`）产出独热 grant，`one2bin` 转成二进制 `in_valid_grant_bin`（即胜出 SM 编号 / sm_id）。

> `fixed_pri_arb` 的优先级取向见 [fixed_pri_arb.v:24-27](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/fixed_pri_arb.v#L24-L27)：`grant = req & ~pre_req`，其中 `pre_req` 屏蔽高位，使**最低位（编号最小的 SM）胜出**。

source 打标是本模块的核心一行：

[sm2cluster_arb.v:90-91](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm2cluster_arb.v#L90-L91)：当 `NUM_SM_IN_CLUSTER != 1` 时，`memReqBuf_in_source = {in_valid_grant_bin, 选中的 source}`——即在 `D_SOURCE` 宽的 source 最高位拼接 `NUM_CLUSTER_DEPTH` 位的 sm_id，得到 `CLUSTER_SOURCE` 宽。同时 `memReqBuf_in_size` 被硬编码为 `'h0`（[L92](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm2cluster_arb.v#L92)），完成格式适配。

打包/解包与 FIFO 切断时序：

[sm2cluster_arb.v:98-100](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm2cluster_arb.v#L98-L100)：把七个字段拼成 `FIFO_WIDTH` 宽的 `memReqBuf_data_in`，出口再拆回。

[sm2cluster_arb.v:117-131](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm2cluster_arb.v#L117-L131)：例化深度 2 的 `stream_fifo`（实例名 `memReqBuf`），`w_valid = |in_valid_grant_oh`（任一 SM 有效即压一次），`r_valid` 直接作为 `mem_req_out_valid_o`。

响应方向的解复用与剥标：

[sm2cluster_arb.v:83](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm2cluster_arb.v#L83)：`mem_rsp_vec_out_valid_o[i]` 由 `source_i[CLUSTER_SOURCE-1-:NUM_CLUSTER_DEPTH] == i` 决定——读 sm_id 选目标 SM。[L80](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm2cluster_arb.v#L80) 把回送给 SM 的 source 截取低 `D_SOURCE` 位（剥掉 sm_id）。[L87](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm2cluster_arb.v#L87) 按同一 sm_id 索引取出对应 SM 的 ready 作为对上游的 `mem_rsp_in_ready_o`。

> ⚠️ **值得在仿真中验证的点（ready 广播）**：[L78](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm2cluster_arb.v#L78) 把 `memReqBuf_w_ready` 广播给**所有** SM（`mem_req_vec_in_ready_o[i] = memReqBuf_w_ready`），而数据只按 `in_valid_grant_bin` 选通一个、FIFO 每拍也只压一项。其上方 [L77](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm2cluster_arb.v#L77) 注释保留了另一种写法（仅给被授权的 SM 反馈 ready）。当多个 SM 同一拍同时拉起 valid 时，当前广播写法下只有优先级最高的 SM 数据被真正写入 FIFO。这是否构成请求丢失，取决于上游 SM 是否会同时有效——请在仿真中专门验证（见 4.2.4）。

#### 4.2.4 代码实践

**实践目标**：考察 `sm2cluster_arb` 在「多 SM 同时请求」下的实际行为。

**操作步骤**（源码阅读 + 仿真验证型）：
1. 阅读 [sm2cluster_arb.v:76-85](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm2cluster_arb.v#L76-L85)，确认 ready 是广播、valid 选择是 `fixed_pri_arb`。
2. 在 `testcase/test_gpgpu_axi_top` 下用 `make run-vcs-4w4t` 跑通一个用例（参见 u1-l4）。
3. 用 `make verdi` 打开波形，把 `sm2clusterArb` 内部的 `mem_req_vec_in_valid_i[1:0]`、`in_valid_grant_oh`、`in_valid_grant_bin`、`memReqBuf_w_ready`、`mem_req_out_valid_o` 拉到同一窗口。
4. 触发两侧 dcache 同时缺失的场景（例如两个 SM 几乎同时发起 `mem_req_valid`），观察两个 valid 是否会出现同拍为 1。

**需要观察的现象**：当 `mem_req_vec_in_valid_i[0]` 与 `[1]` 同拍为 1 时，`in_valid_grant_bin` 是否稳定指向 0；两个 SM 是否都看到 ready=1 而同时撤销 valid。

**预期结果**：若同拍双有效，仅 SM0 的请求进入 FIFO，SM1 的请求在该拍未被存储。请据此判断上游（SM 的 dcache 出口）是否依赖「单拍单 SM 有效」的隐含约束。**若无法本地仿真，标注「待本地验证」**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `sm2cluster_arb` 要在 source 高位贴 sm_id，而 `l1cache_arb` 是贴 cache_id？
**答案**：两者都是「逐层打回信地址」的同一机制——`l1cache_arb` 区分 icache/dcache（cache_id），`sm2cluster_arb` 区分簇内不同 SM（sm_id）。响应回送时分别读各自的最高位解复用。

**练习 2**：`stream_fifo` 的深度为 2，它的作用是什么？去掉会怎样？
**答案**：切断「SM 侧组合输出 → cluster 侧仲裁」的长组合路径，并提供少量缓冲削峰；去掉后请求路径会变成跨模块的纯组合长链，影响时序收敛。

**练习 3**：`memReqBuf_in_size` 为何被硬编码为 `'h0`？
**答案**：SM 侧原始接口没有 `size` 字段（一次访存即一个 cache 块），而 TileLink 风格的 cluster 侧接口需要 `SIZE_BITS` 位；这里固定填 0 表示 beat 大小由其它参数（`L2CACHE_BEATBYTES`）隐式确定。

---

### 4.3 l2_distribute：按地址把请求分发到多个 L2

#### 4.3.1 概念说明

`l2_distribute` 是「每簇一个」的纯组合模块。它的职责单一：**一路 cluster 请求进，按地址选 `NUM_L2CACHE` 路中的一路出**。它既不改 source、也不仲裁（请求侧永远只有一路在动），只是把同一组字段扇出到所有 L2 的连线上，再用地址位决定哪一路的 valid 真正拉起。

响应方向则相反：多路 L2 响应进，固定优先级选一路出。因为默认 `NUM_L2CACHE=1`，这一级在默认配置下完全直通。

#### 4.3.2 核心流程

请求方向（按地址分发）：

```
l2_index = address[ ADDRESS_BITS-TAG_BITS-1 -: L2CACHE_BITS ]   // 从 tag 下方切 L2C_BITS 位
for each L2 i:
    valid_o[i]   = (L2C_BITS != 0) ? (valid_i && (i == l2_index)) : valid_i
    <其余字段全扇出>                                              // 所有 L2 共享同一组字段
ready_o          = ready_i[l2_index]   // 反压只看被选中那路 L2 的 ready
```

响应方向（固定优先级选一路）：

```
oh   = fixed_pri_arb( {1'b0, mem_rsp_vec_in_valid_i} )   // 最低有效 L2 优先
bin  = one2bin(oh)
rsp_out_*  = mux(mem_rsp_vec_in_*_i, by=bin)
ready_o[0] = rsp_out_ready_i ;  ready_o[j>0] = 前面都未占用 && rsp_out_ready_i
```

#### 4.3.3 源码精读

请求分发的关键一行：

[l2_distribute.v:84-95](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2_distribute.v#L84-L95)：generate 把字段扇出到每个 L2。其中 [L86](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2_distribute.v#L86) 是核心：

```verilog
assign mem_req_vec_out_valid_o[i] = (`L2C_BITS != 0)
    ? (mem_req_in_valid_i && (i == mem_req_in_address_i[`ADDRESS_BITS-`TAG_BITS-1-:L2CACHE_BITS]))
    : mem_req_in_valid_i;
```

即「多 L2 时按地址位选目标，单 L2 时直通」。地址切片 `\`ADDRESS_BITS-\`TAG_BITS-1 -: L2CACHE_BITS` 正是位于 set/offset 之上、tag 之下的那 `L2C_BITS` 位（因为 `TAG_BITS = ADDRESS_BITS - SET_BITS - OFFSET_BITS - L2C_BITS`），属于经典的「地址交错选 bank」。

`L2CACHE_BITS` 本地参数与退化处理：

[l2_distribute.v:58](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2_distribute.v#L58)：`L2CACHE_BITS = (NUM_L2CACHE==1) ? 1 : $clog2(NUM_L2CACHE)`——单 L2 时给 1 以避免零宽向量，但实际选择逻辑用 `\`L2C_BITS != 0`（[L113-114](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2_distribute.v#L113-L114)）判断，确保单 L2 直通。

[l2_distribute.v:113-114](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2_distribute.v#L113-L114)：`mem_req_in_ready_o` 取被选中那路 L2 的 ready，单 L2 时取整组（即唯一那一位）。

响应方向的固定优先级选路：

[l2_distribute.v:65-82](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2_distribute.v#L65-L82)：`fixed_pri_arb`（`ARB_WIDTH=NUM_L2CACHE+1`，高位补 0）选最低有效 L2，`one2bin` 转二进制作 MUX 选择信号。

[l2_distribute.v:97-103](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2_distribute.v#L97-L103)：按该二进制索引从 `mem_rsp_vec_in_*` 中 mux 出一路响应。[L104-111](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2_distribute.v#L104-L111) 处理各路 ready：第 0 路直接接 `mem_rsp_out_ready_i`，第 j 路仅在前面所有路都未被占用时才反馈 ready，实现「同一拍只放行一路」。

#### 4.3.4 代码实践

**实践目标**：算出多 L2 配置下，「哪个地址位」决定请求去哪个 L2。

**操作步骤**：
1. 在 `define.v` 查得默认 `L2CACHE_NSETS=2`（[L135](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L135)）、`L2CACHE_BLOCKBYTES = L2CACHE_BLOCKWORDS*4`，据此算出 `SET_BITS`、`OFFSET_BITS`。
2. 由 `TAG_BITS = 32 - SET_BITS - OFFSET_BITS - L2C_BITS`，计算「地址切片上界」`32 - TAG_BITS - 1 = SET_BITS + OFFSET_BITS + L2C_BITS - 1`。
3. 假设把 `NUM_L2CACHE` 改为 2（`L2C_BITS=1`），写出选择表达式 `address[?:1]` 的确切上下界。

**需要观察的现象**：选 L2 的那 1 位恰好紧贴在 set+offset 字段之上、tag 字段之下。

**预期结果**：例如若 `SET_BITS=1`、`OFFSET_BITS=3`、`L2C_BITS=1`，则选择位为 `address[4]`（即 `address[4 -: 1]`），地址第 4 位为 0 进 L2[0]、为 1 进 L2[1]。**确切数值取决于 `L2CACHE_BLOCKWORDS` 等，待按实际配置核算**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `l2_distribute` 请求侧不做仲裁，只做「扇出 + 地址选 valid」？
**答案**：因为请求输入永远只有一路（来自 `sm2cluster_arb` 的 cluster 级单流），不存在竞争；只需决定它落到哪个 L2，故用地址位选目标、字段全扇出即可。

**练习 2**：响应侧为什么要用固定优先级而不是轮询？
**答案**：响应路数等于 L2 数，同一拍最多有几路同时有效；固定优先级选最低索引那路放行、其余通过 ready 互斥排队，逻辑最简。响应按 source 路由的正确性由 source 字段保证，与这里选哪路无关（每路都自带正确 source）。

---

### 4.4 cluster_to_l2_arb：cluster 级仲裁、cluster_id 打标与响应回送

#### 4.4.1 概念说明

`cluster_to_l2_arb` 是「每个 L2 一个」的纯组合模块，是请求进入 `Scheduler` 前的最后一跳。它的工作与 `sm2cluster_arb` 高度对称：

- 请求侧：把 `NUM_CLUSTER` 路 cluster 请求用固定优先级仲裁成一路，在 source 高位贴 **cluster_id**，输出 `SOURCE_BITS` 宽送入 `Scheduler.sche_in_a`；
- 响应侧：从 `Scheduler.sche_in_d` 收到的 `SOURCE_BITS` 宽响应，按 source 最高 `CLUSTER_BITS` 位（cluster_id）解复用回对应 cluster，并剥掉 cluster_id。

注意它请求侧的仲裁实现方式与 `sm2cluster_arb` 略有不同：用 `input_reverse` + `find_first` 组合，而非直接 `fixed_pri_arb`。

#### 4.4.2 核心流程

请求方向（伪代码）：

```
// 1. 仲裁：reverse 后 find_first，等效「最低 cluster 索引优先」
rev        = input_reverse(mem_req_vec_in_valid_i)
grant_bin  = find_first(rev, target=1'b1)        // 胜出 cluster 编号 = cluster_id

// 2. 数据选通 + source 打标
fields_out = mux(mem_req_vec_in_*_i, by=grant_bin)
source_out = { grant_bin, source_in[grant_bin] } // 高位贴 cluster_id → SOURCE_BITS 宽
                                                                  （NUM_CLUSTER==1 时直接透传）
ready_o[所有 cluster] = mem_req_out_ready_i       // 广播 ready
```

响应方向（伪代码）：

```
cluster_id = source_i[ SOURCE_BITS-1 -: CLUSTER_BITS ]   // 读 source 最高 CLUSTER_BITS 位
for each cluster j:
    valid_o[j] = (NUM_CLUSTER==1) ? rsp_valid_i
                                  : ((j == cluster_id) && rsp_valid_i)
    source_o[j] = source_i[CLUSTER_SOURCE-1:0]            // 剥掉 cluster_id → CLUSTER_SOURCE 宽
rsp_ready_o = (NUM_CLUSTER==1) ? ready_i[0] : ready_i[cluster_id]
```

#### 4.4.3 源码精读

仲裁的「reverse + find_first」实现：

[cluster_to_l2_arb.v:84-103](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cluster_to_l2_arb.v#L84-L103)：`input_reverse` 把 valid 向量位序反转（最低位变最高位），`find_first(target=1)` 在反转后的向量里「自 MSB 找首个 1」。两者合起来等效于「选原始向量中编号最小的有效 cluster」——与 `fixed_pri_arb` 语义一致，只是实现路径不同。

> 辅助理解：[input_reverse.v:24-28](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/input_reverse.v#L24-L28) 做位反转；[find_first.v:30-36](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/find_first.v#L30-L36) 通过 generate 链式覆盖，最终保留最高索引处的 1，输出其位置。

source 打标与字段选通：

[cluster_to_l2_arb.v:105-114](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cluster_to_l2_arb.v#L105-L114)：`mem_req_out_valid_o = |mem_req_vec_in_valid_i`（任一 cluster 有效即有输出）；各字段按 `mem_req_vec_in_valid_bin` 选通；source 在 `NUM_CLUSTER!=1` 时拼接 `{mem_req_vec_in_valid_bin, 选中的 source}` 贴上 cluster_id。

[L119-125](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cluster_to_l2_arb.v#L119-L125)：ready 同样**广播**给所有 cluster（与 `sm2cluster_arb` 一致的写法，同样值得在多 cluster 配置下验证）。

响应方向的解复用与剥标：

[cluster_to_l2_arb.v:127-141](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cluster_to_l2_arb.v#L127-L141)：[L130-131](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cluster_to_l2_arb.v#L130-L131) 由 `source_i[SOURCE_BITS-1-:CLUSTER_BITS] == j` 决定响应送给哪个 cluster；[L139](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cluster_to_l2_arb.v#L139) 把回送的 source 截取低 `CLUSTER_SOURCE` 位（剥掉 cluster_id）。

[cluster_to_l2_arb.v:144-149](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cluster_to_l2_arb.v#L144-L149)：按 cluster_id 索引取出对应 cluster 的 ready 作为对 L2 的 `mem_rsp_in_ready_o`。

> 与 `Scheduler` 的对接见 [GPGPU_top.v:409-425](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L409-L425)：`cluster_to_l2_arb` 的 `mem_req_out_*` 直连 `Scheduler.sche_in_a_*`，`Scheduler.sche_in_d_*` 直连 `cluster_to_l2_arb` 的 `mem_rsp_in_*`——确认第③级是 L2 的「门前最后一跳」。

#### 4.4.4 代码实践

**实践目标**：跟踪响应从 L2 一路剥标签回到原 SM，验证「逐层剥标」的一致性。

**操作步骤**：
1. 在 [cluster_to_l2_arb.v:146](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/cluster_to_l2_arb.v#L146) 确认响应路由读的是 source 的「最高 `CLUSTER_BITS` 位」。
2. 在 [sm2cluster_arb.v:83](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm2cluster_arb.v#L83) 确认下一跳读的是 source 的「最高 `NUM_CLUSTER_DEPTH` 位」。
3. 对照 4.1.3 的 source 布局表，确认 cluster_id 位于 sm_id 之上，两层剥标不会互相干扰。

**需要观察的现象**：每剥一层，source 宽度从 `SOURCE_BITS` → `CLUSTER_SOURCE` → `D_SOURCE` 逐级收窄，且每层读的是当时 source 的最高有效段。

**预期结果**：响应最终回到发起请求的那个 SM 时，source 已恢复为 `D_SOURCE` 宽，可被 `l1cache_arb`（u6-l3）继续按 cache_id 解复用、最终回到 dcache 的 MSHR/WSHR 表项。

#### 4.4.5 小练习与答案

**练习 1**：`cluster_to_l2_arb` 请求侧用 `input_reverse + find_first`，而 `sm2cluster_arb` 用 `fixed_pri_arb`，二者优先级取向是否一致？
**答案**：一致，都是「编号最小者优先」。`input_reverse` 把最低位翻到最高位，`find_first` 取最高位的 1，合起来仍是选原始编号最小的有效者。

**练习 2**：为什么响应方向「读 source 最高位」就能正确路由，而不需要在请求时记录一张路由表？
**答案**：因为请求方向已经把路由信息（cluster_id、sm_id）逐层拼进 source 的高位；source 随响应原样带回，每层只要读自己当初贴的那段高位即可还原目标。这就是 u7-l1 所说的「source 是分级回信地址」。

**练习 3**：默认配置（`NUM_CLUSTER=1`）下，`cluster_to_l2_arb` 的请求与响应分别退化成什么？
**答案**：请求侧 `CLUSTER_BITS=0`，`mem_req_out_source_o` 直接取 `CLUSTER_SOURCE` 切片（不贴标签）；响应侧所有分支走 `NUM_CLUSTER==1` 三目，valid/source/ready 全部直通。整个模块对请求响应都近似一根导线，但仍承担字段宽度衔接的作用。

---

## 5. 综合实践

**任务**：在 `GPGPU_top.v` 中完整跟踪一次「SM 发起到 L2 接收、再到响应回送」的全旅程，画出互联拓扑并标注每一段的 source 宽度。

**步骤**：

1. **请求正向跟踪**。从某个 `sm_wrapper`（`GPGPU_top.v` L325 的 generate 内）的 `mem_req_valid_o` / `mem_req_a_source_o`（`D_SOURCE` 宽）出发：
   - 进入 `sm2cluster_arb`（[L487](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L487)），source 升级为 `CLUSTER_SOURCE`；
   - 进入 `l2_distribute`（[L523](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L523)），宽度不变、按地址选 L2；
   - 进入 `cluster_to_l2_arb`（[L445](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L445)），source 升级为 `SOURCE_BITS`；
   - 到达 `Scheduler.sche_in_a_*`（[L409](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L409)）。
2. **响应反向跟踪**。从 `Scheduler.sche_in_d_*`（[L418](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L418)）出发，反向经过 `cluster_to_l2_arb`（剥 cluster_id）→ `l2_distribute`（选路）→ `sm2cluster_arb`（剥 sm_id）→ 回到原 `sm_wrapper.mem_rsp_d_source_i`（`D_SOURCE` 宽）。
3. **画拓扑图**。参考下面的骨架补全 source 宽度与中间网线名：

```
 sm_wrapper[0..N-1]                sm2cluster_arb        l2_distribute        cluster_to_l2_arb        Scheduler
  mem_req_a_source ──(D_SOURCE)──►  ┌──────────┐  ──(CLUSTER_SOURCE)──►  ┌────────────┐  ──(SOURCE_BITS)──►  sche_in_a
  (per cluster, NUM_SM_IN_CLUSTER)  │ +sm_id   │   (per cluster)          │ +cluster_id│                       (per L2)
                                     │ FIFO     │                          │            │
  mem_rsp_d_source ◄──(D_SOURCE)───  │ -sm_id   │ ◄──(CLUSTER_SOURCE)───  │ -cluster_id│ ◄──(SOURCE_BITS)───  sche_in_d
                                     └──────────┘                          └────────────┘
```

4. **验证标签自洽**。在图上标注：请求方向每过一级 source 变宽（贴标），响应方向每过一级 source 变窄（剥标），首尾宽度均为 `D_SOURCE`。

**预期结果**：一张清晰的「三级接力」拓扑，能解释任意一个 SM 的请求为何能被 L2 正确接收、其响应为何能精确回到该 SM。若条件允许，用 `make run-vcs-4w4t`（u1-l4）仿真并在 Verdi 中抓 `sm2clusterArb` / `cluster2l2Arb` 边界信号验证 source 字段的「贴—剥」过程；若无法本地仿真，标注「待本地验证」。

## 6. 本讲小结

- 多 SM 到 L2 之间是「**汇聚 → 分发 → 再汇聚**」的三级接力：`sm2cluster_arb`（per cluster，N:1）→ `l2_distribute`（per cluster，1:N）→ `cluster_to_l2_arb`（per L2，N:1）→ `Scheduler`。
- 请求方向**逐层贴标签**（sm_id、cluster_id）使 source 不断变宽，响应方向**逐层剥标签并按标签选路**使 source 不断变窄，首尾回到 `D_SOURCE` 宽——这是 u7-l1「source 分级回信地址」在互联层的具体落地。
- `sm2cluster_arb` 是唯一带时钟的模块：固定优先级仲裁 + 字段重打包 + 深度 2 的 `stream_fifo` 切时序；它在 source 高位贴 sm_id。
- `l2_distribute` 纯组合、按地址位选 L2（从 tag 下方切 `L2C_BITS` 位），请求侧扇出、响应侧固定优先级选路。
- `cluster_to_l2_arb` 纯组合，用 `input_reverse + find_first` 实现「最低 cluster 优先」，在 source 高位贴 cluster_id，响应按 source 最高位解复用。
- 默认配置（`NUM_CLUSTER=1`、`NUM_L2CACHE=1`）下，第②③级退化为直通，只有第①级真正在 2 个 SM 间仲裁；多 L2 / 多 cluster 的扩展骨架已就位。
- `sm2cluster_arb` 与 `cluster_to_l2_arb` 的 ready 均为**广播**写法，多源同拍同时有效时只有最高优先级者的数据真正进入下游——建议在仿真中专门验证。

## 7. 下一步学习建议

- **u7-l4（AXI4 适配器与 host 接口）**：本讲到 `Scheduler.sche_in_a` 为止；L2 向外的 `sche_out_a` / `sche_out_d` 经 `GPGPU_top` 的 `out_a_*` / `out_d_*` 接到 `axi4_adapter`，转成 AXI4 对外，是自然的下一站。
- **u8-l2（公共单元库 common_cell）**：本讲反复出现的 `fixed_pri_arb`、`one2bin`、`find_first`、`input_reverse`、`stream_fifo` 都来自 `src/common_cell`，建议系统阅读以理解全项目复用的基础积木。
- **回看 u7-l1 / u6-l3**：把本讲的 source 「贴—剥」过程与 u7-l1 的 source 编码公式、u6-l3 的 `l1cache_arb` 响应解复用对照，能形成从 dcache MSHR 到 L2 的完整 source 链路闭环。
- 若对「多 SM 同拍请求」的 ready 广播行为感兴趣，可结合 u6-l1（dcache 出口的握手时序）分析上游是否提供「单拍单源有效」的隐含保证。
