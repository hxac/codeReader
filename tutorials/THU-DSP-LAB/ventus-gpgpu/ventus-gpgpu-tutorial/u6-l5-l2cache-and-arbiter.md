# L1Cache2L2 仲裁与 L2 缓存

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清一个 SM 内部的 ICache 与 DCache 请求是如何被 `L1Cache2L2Arbiter` 合并成一路发往 L2 的，以及回程响应是如何被拆回给 I/D 两个 cache 的。
- 复述「请求去程逐层贴标签、响应回程逐层剥标签」的 source 字段路由机制，并解释它如何让多 SM、多 cluster、多 L2 bank 的拓扑在默认规模下退化为直通。
- 描述 Ventus L2 缓存（`Scheduler`）的内部组成：`SinkA / Directory / MSHR / requests / SourceA / SinkD / SourceD / BankedStore` 各自的职责与协作关系。
- 解释 L2 作为「包容性（inclusive）目录式写回 cache」的命中、缺失、受害者替换与回填流程，并追踪一次 L2 read miss 的端到端数据通路。
- 读懂 L2 的关键参数（sets/ways/mshrs/source_bits 等）是如何从 `parameters.scala` 与 `InclusiveCacheParameters_lite` 推导出来的。

## 2. 前置知识

本讲是缓存单元（u6）的收尾，默认你已读过：

- **u2-l2**：`GPGPU_top` 的顶层组装与 `SM2clusterArbiter / l2Distribute / cluster2L2Arbiter` 三层互联的雏形，以及贯穿其中的 **source 字段**概念。本讲会把这条线接到 L2 入口。
- **u6-l2 / u6-l3**：L1 DCache 的写回/写不分配策略，以及 L1 侧 `a_source/d_source`（13 位 = 3 位 op + 2 位 MSHR 主条目号 + 8 位 setIdx）的编码方式。本讲会在这 13 位之上继续叠加更高位的路由标签。

几个本讲会用到的术语，先做最小解释：

- **TileLink（精简版）**：一种片上总线协议，用 A 通道（请求）和 D 通道（响应）传递读写。Ventus 的 L2 是 SiFive `block-inclusive-cache` 的「lite」简化版，**只保留了 A/D 两个通道**（去除了 B/C/E 一致性探查通道），本质是一个目录式写回 cache。
- **包容性 cache（inclusive cache）**：L1 里有的缓存行，L2 里一定也有一份拷贝。L2 维护一张**目录（directory）**记录每个 set/way 的 tag、有效位、脏位，据此判定命中、挑选受害者（victim）。
- **MSHR（Miss Status Holding Register）**：记录「未完成的 miss」的表项。本讲讲的是 **L2 自己的 MSHR**，请勿与 u6-l3 的 L1 MSHR、u5-l4 的 LSU MSHRv2 混淆——三者结构相似但处在不同层级。
- **source 字段**：随请求一起传递的一段 ID。Ventus 用它在多层互联里做「去程贴标、回程剥标」的路由，是本讲理解整个缓存层次互联的钥匙。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [ventus/src/L1Cache/L1Cache2L2Arbiter.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1Cache2L2Arbiter.scala) | 每 SM 内把 ICache/DCache 两路 memReq 仲裁成一路，并在 source 上贴 cache_id；回程按 cache_id 拆分响应。 |
| [ventus/src/top/GPGPU_top.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala) | 例化 L2 `Scheduler` 与三层互联（`SM2clusterArbiter/l2Distribute/cluster2L2Arbiter`），把 SM 集群接到 L2。 |
| [ventus/src/L2cache/Scheduler.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/Scheduler.scala) | L2 缓存主体：把 SinkA/Directory/MSHR/SourceA/SinkD/SourceD/BankedStore 连成整体。 |
| [ventus/src/L2cache/BankedStore.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/BankedStore.scala) | L2 的数据 SRAM 阵列，按 bank 组织，供 SourceD 读、SourceD/SinkD 写。 |
| [ventus/src/L2cache/MSHR.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/MSHR.scala) | 单个 L2 MSHR：跟踪一次 miss 的生命周期（发 Get→等回填→写目录→交付 SourceD）。 |
| [ventus/src/L2cache/Parameters.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/Parameters.scala) | L2 参数与地址切分定义（source_bits、parseAddress、mshrs 数量等）。 |
| [ventus/src/top/parameters.scala](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala) | 全局参数：默认规模（num_sm/num_cluster/num_l2cache）与 L2 规模配置。 |

辅助阅读（SourceA/SinkA/SinkD/SourceD 的实现细节）位于 `ventus/src/L2cache/` 下同名文件，本讲会引用关键行。

## 4. 核心概念与源码讲解

### 4.1 L1Cache2L2Arbiter：单 SM 内 I/D 合流

#### 4.1.1 概念说明

每个 SM 内部有两个 L1 cache：指令 cache（ICache，索引 0）和数据 cache（DCache，索引 1）。它们各自只暴露一路向下的 `memReq/memRsp` 接口，但一个 SM 对外（向 cluster 互联）只拉出**一路** `memReq/memRsp`。`L1Cache2L2Arbiter` 就是把这两路合并成一路的「二选一仲裁器」。

它要解决两个问题：

1. **去程合并**：ICache 和 DCache 同时发 miss 请求时，谁先走？合并后下游怎么知道某个请求来自 I 还是 D？
2. **回程拆分**：L2 返回的响应只有一路，怎么把它正确地送回 ICache 或 DCache？

答案都落在 **source 字段**上：去程给 I/D 各分配 1 位 cache_id 贴进 source 最高位，回程再读这一位做分发。

#### 4.1.2 核心流程

去程：

1. 用一个 `NCacheInSM`（=2）选一的 `Arbiter` 在 ICache/DCache 间仲裁。
2. 仲裁胜出者的 `a_source` 被改写为 `Cat(cache_id, 原始 a_source)`——即在最高位拼一位 cache_id。
3. 合并后的请求（类型 `L1CacheMemReqArb`）发向 `SM_wrapper.io.memReq`，再交给集群互联。

回程：

1. L2 回来的响应 `d_source` 里有那位 cache_id。
2. 用 `UIntToOH` + `Mux1H` 把响应按 cache_id 路由到 ICache 或 DCache 的 `memRspVecOut`。
3. 低位的 13 位 source 原样回到对应 cache，作为它找回自己 MSHR 主条目号的钥匙（见 u6-l3）。

#### 4.1.3 源码精读

仲裁器 IO 定义了入参是 `NCacheInSM` 路、出参是一路：

[L1Cache2L2Arbiter.scala:21-26](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1Cache2L2Arbiter.scala#L21-L26) 定义 `L1Cache2L2ArbiterIO`：`memReqVecIn` 是 I/D 两路入，`memReqOut` 是合并后的一路出，`memRspIn`/`memRspVecOut` 是回程拆分。

去程贴 cache_id 的核心一行：

[L1Cache2L2Arbiter.scala:31-38](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1Cache2L2Arbiter.scala#L31-L38) 用 `Arbiter` 仲裁两路请求；其中第 35 行 `memReqArb.io.in(i).bits.a_source := Cat(i.asUInt, io.memReqVecIn.get(i).bits.a_source)` 就是「把 cache_id `i` 拼到 source 最高位」。

回程按 cache_id 拆分：

[L1Cache2L2Arbiter.scala:41-48](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1Cache2L2Arbiter.scala#L41-L48) 第 44 行从 `d_source` 里抽取 `log2Up(NCacheInSM)` 位（即 cache_id 那一位），判断该响应该送往 `memRspVecOut(i)` 中的哪一个；第 46 行用 `Mux1H(UIntToOH(...))` 把下游 ready 信号反向选回 `memRspIn.ready`。

> 说明：`a_source` 在 I/D cache 侧本是 13 位（`l1cache_sourceBits = 3 + log2Up(dcache_MshrEntry) + log2Up(dcache_NSets) = 3+2+8 = 13`，见 [parameters.scala:111](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L111)）；合并后变成 `log2Up(NCacheInSM)+13 = 14` 位（见 `L1CacheMemReqArb` 的位宽 [L1Interfaces.scala:97-108](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1Interfaces.scala#L97-L108)）。多出来的这一位就是 cache_id。

#### 4.1.4 代码实践

**实践目标**：确认「贴标—剥标」的对称性，并验证 ICache 走 cache_id=0、DCache 走 cache_id=1。

**操作步骤**：

1. 打开 `ventus/src/top/GPGPU_top.scala` 中 `SM_wrapper` 的例化（[GPGPU_top.scala:365-389](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L365-L389)），注意 `memReqVecIn.get(0)` 接 ICache、`memReqVecIn.get(1)` 接 DCache。
2. 对照 [L1Cache2L2Arbiter.scala:34-36](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1Cache2L2Arbiter.scala#L34-L36) 的 `for(i <- 0 until NCacheInSM)`，确认 `i=0` 时 source 最高位为 0（ICache），`i=1` 时为 1（DCache）。
3. 在 [GPGPU_top.scala:315-326](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L315-L326) 有一段 `printf` 调试代码：当 L2 的 `in_a` 端口 fire 时，会按 source 字段解析出 `cache_id` 与 `sm_id` 打印 `[L1C] #周期 SM .. CACHE .. ADDR ..`。

**需要观察的现象**：在仿真波形或日志里，一条来自 DCache 的 miss 请求，其 source 的 cache_id 位应为 1；来自 ICache 的应为 0。

**预期结果**：你能用 source 的最高位（cache_id）反推出请求来自 I 还是 D，无需额外信号。**待本地验证**（需要跑通 sim-verilator 并开启该 printf）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 ICache 和 DCache 的索引对调（让 ICache 接 `memReqVecIn(1)`），回程路由会不会出错？
**答**：不会，因为去程的 `Cat(i, ...)` 和回程的「按第 44 行的位抽取 `i`」用的是同一个 `i`，天然对称。但对调后 cache_id 的语义会变（ICache 变成 1），仅影响日志可读性，不影响正确性。

**练习 2**：为什么用 `Arbiter` 而不是简单地把两路 `valid` 做「或」？
**答**：当 I/D 同拍都有请求时，「或」无法保证每拍只发一个；`Arbiter` 保证每拍只放行一路（固定优先级），并把未中选者反压住，符合下游单路 `memReqOut` 的吞吐限制。

---

### 4.2 集群互联与 source 字段路由（承接 u2-l2）

#### 4.2.1 概念说明

`L1Cache2L2Arbiter` 输出的一路 `memReq` 还没到 L2，中间隔着 `GPGPU_top` 里的**三层互联**：`SM2clusterArbiter → l2Distribute → cluster2L2Arbiter`。这一节不重复 u2-l2 的细节，只从「source 字段如何被逐层叠加」的角度把它接到 L2 入口，因为本讲综合实践要画端到端流程。

核心思想一句话：**每经过一层，就在 source 最高位拼上「本层的 id」；回程则剥掉自己的 id 并据此选路。** 而 L2 全程**不修改** source——它只是把请求里带来的 source 原样回填进响应里。

#### 4.2.2 核心流程

去程（请求自下而上）的 source 字段逐层生长：

```
L1 cache 自带:      [ op(3) | mshr(2) | setIdx(8) ]               = 13 位 (l1cache_sourceBits)
+ L1Cache2L2Arbiter:[ cache_id(1) | 上面 13 位 ]                    = 14 位
+ SM2clusterArbiter:[ sm_id_in_cluster(1) | 上面 14 位 ]            = 15 位
+ cluster2L2Arbiter:[ cluster_id(0) | 上面 15 位 ]  (默认 num_cluster=1，0 位)
                   ⇒ 进入 L2 的 source
```

回程（响应自上而下）则层层剥皮：`cluster2L2Arbiter` 按 cluster_id 位选路给对应 cluster，`SM2clusterArbiter` 按 sm_id 位选路给对应 SM，`L1Cache2L2Arbiter` 按 cache_id 位选路给 I/D。低 13 位始终不动，最终回到 L1 cache 当 MSHR 钥匙。

> 默认规模下 `num_cluster=1`、`num_l2cache=1`，所以 cluster_id 位宽为 0、L2 bank 选择退化为直通；但代码已为多 cluster、多 L2 留好了位域扩展点。

#### 4.2.3 源码精读

三层互联的例化与连线在 `GPGPU_top` 里：

[GPGPU_top.scala:166-169](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L166-L169) 例化 `NCluster` 个 `SM2clusterArbiter` 与 `l2Distribute`、`NL2Cache` 个 `cluster2L2Arbiter` 与 `Scheduler`（L2）。

[GPGPU_top.scala:202-213](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L202-L213)（MMU 关闭分支）把三层互联串起来：`l2cache(i).in_a <> cluster2l2Arb(i).memReqOut`，`l2cache(i).out_a/out_d` 接顶层 `io.out_a/out_d`。

每层的「贴标」实现：

- `SM2clusterArbiter` 第 [538](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L538) 行 `source := Cat(i.asUInt, ...)` 拼 sm_id；
- `cluster2L2Arbiter` 第 [618](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L618) 行 `source := Cat(i.asUInt, ...)` 拼 cluster_id。

`l2Distribute` 比较特殊——它**不贴标**，而是按地址里的 `l2cidx`（L2 bank 编号）把请求分发到 `NL2Cache` 路：

[GPGPU_top.scala:591-596](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L591-L596) 第 593 行用 `l2param.parseAddress(io.memReqIn.bits.address)._2`（即 l2cidx）选择发往哪个 L2；回程用一个 `Arbiter` 把多路 L2 响应合并。

回程剥标（以 `cluster2L2Arbiter` 为例）：

[GPGPU_top.scala:633-645](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L633-L645) 第 638 行把 `memRspIn.bits.source` 截掉高位 cluster/sm 标签、只保留下面的低有效位送回 cluster；第 643-644 行则用 source 里的 cluster_id 位段判断该响应属于哪个 cluster。

#### 4.2.4 代码实践

**实践目标**：用一个具体请求，把 source 字段在三层里的位宽变化算出来。

**操作步骤**：

1. 查默认规模：`num_sm=2, num_cluster=1, num_sm_in_cluster=2, num_l2cache=1, dcache_MshrEntry=4, dcache_NSets=256`（[parameters.scala:7-9,27-29,71,88,99-132](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L99-L132)）。
2. 计算各层 source 位宽：L1 = 13；+cache_id(1) = 14；+sm_id(1) = 15；+cluster_id(0) = 15。
3. 对照 L2 参数里的 `source_bits` 公式 [Parameters.scala:159](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/Parameters.scala#L159)：`3 + log2Up(NMshrEntry=4) + log2Up(NSets=256) + log2Ceil(num_sm_in_cluster=2) + log2Ceil(num_cluster=1) + 1 + 1 = 3+2+8+1+0+1+1 = 16`。

**预期结果**：L2 的 `source_bits=16`，比请求到达 L2 时实际携带的 15 位多 1 位——这多出来的最低 1 位是预留给 MMU 场景下区分「TLB 请求 / L1Cache 请求」的标志位（见 [Parameters.scala:157-159](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/Parameters.scala#L157-L159) 的注释，MMU 关闭时不使用）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 L2 不需要理解 cluster_id / sm_id / cache_id 的含义？
**答**：因为 L2 把请求里的 source 当作「不透明令牌」存进 MSHR，响应时原样回填（见 4.4）。所有「分层路由」都由互联层在请求侧贴标、响应侧剥标完成，L2 只负责 echo。

**练习 2**：若把 `num_cluster` 改成 2，source 字段哪一段会变长？
**答**：`cluster2L2Arbiter` 会多拼 `log2Ceil(2)=1` 位 cluster_id，L2 的 `source_bits` 公式里 `log2Ceil(num_cluster)` 项从 0 变 1，整体 +1 位。

---

### 4.3 L2 Scheduler 总体与目录机制（Scheduler + Directory）

#### 4.3.1 概念说明

`Scheduler` 是 L2 缓存的主体模块，名字沿用了 SiFive `block-inclusive-cache` 的命名（本仓库是它的精简移植，见 `Configs.scala` 里 `import sifive.blocks.inclusivecache._`）。它对外只暴露 4 个 TileLink 通道：

- `in_a`：来自 L1 的请求（Get 读 / PutFull、PutPartial 写 / Hint 刷新无效化）。
- `in_d`：返回给 L1 的响应。
- `out_a`：发往外部内存（经 AXI4Adapter 接 DDR）的请求。
- `out_d`：来自外部内存的响应（回填数据）。

内部由 8 个子模块协作，可以按数据流向分成「入口 → 查目录 → 分发处理 → 出口」四段：

| 子模块 | 职责 |
|--------|------|
| `SinkA` | 消费 `in_a`；带数据的写请求把数据暂存进 **putBuffer**；把请求整理成 `FullRequest` 交给目录。 |
| `Directory_test` | 标签目录（inclusive）：查 set/way，判命中，miss 时选受害者 way，维护有效位/脏位。 |
| `requests`（ListBuffer） | **二级 miss 合并**：把命中同一 MSHR 的后续 miss 挂到同一个 MSHR 上，避免重复发外部请求。 |
| `MSHR × mshrs` | 每个跟踪一次未完成 miss：发 Get、等回填、写目录、交付 SourceD。 |
| `SourceA` | 驱动 `out_a`：把 read miss 的 Get、dirty 受害者写回、write-no-allocate 的写发往外部内存。 |
| `SinkD` | 消费 `out_d`：收外部内存回填数据，按 source 找到对应 MSHR，把数据交给 MSHR 与 BankedStore。 |
| `SourceD` | 驱动 `in_d`：命中的读从 BankedStore 取数返回；miss 的回填数据转发给 L1；写命中写 BankedStore；dirty 受害者读出后交 SourceA。 |
| `BankedStore` | L2 的数据 SRAM 阵列（详见 4.4）。 |

「包容性」体现在 `Directory_test` 是 L2 的唯一真相源：L1 里持有的行，L2 目录里必有对应条目；L2 命中直接从 BankedStore 服务，L2 缺失则向外部内存取，并可能先驱逐一个脏受害者。

#### 4.3.2 核心流程

一次 **read miss** 在 Scheduler 内部的流转：

1. `SinkA` 收到 `in_a` 的 Get，整理成 `FullRequest`（`request` 信号）。
2. `Directory_test.read` 查目录，同拍把 set/way/tag 送进结果；判定 `hit=false`，并选出受害者 way（可能脏）。
3. 目录结果 miss 且允许分配（`alloc`）时，给一个空闲 MSHR 发 `allocate`，把 set/tag/way/source 等存入；同时把这条 miss 推进 `requests` ListBuffer。
4. 该 MSHR 在被轮询调度选中后，经 `SourceA` 把 Get 发到 `out_a`（地址由 `expandAddress(tag,l2cidx,set,offset)` 重建）。
5. 外部内存经 `out_d` 回来一拍数据，`SinkD` 按 `d.bits.source`（即 MSHR 编号）找到 MSHR，触发 MSHR 的 `sinkd`，数据同时写入 BankedStore（经 `bankedStore.io.sinkD_adr/dat`）。
6. MSHR 收齐后让 `schedule.d.valid` 拉高，`SourceD` 把回填数据组装成 `in_d` 响应（opcode=AccessAckData）发回 L1，source 原样回填。
7. MSHR 还会经 `schedule.dir` 把新 tag 写进目录（命中后该行变有效）。

命中路径则短得多：目录 `hit=true` → 结果进 `dir_result_buffer` → `SourceD` 直接读 BankedStore 经 `in_d` 返回，不分配 MSHR、不发外部请求。

#### 4.3.3 源码精读

Scheduler 的 4 个对外通道：

[Scheduler.scala:27-35](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/Scheduler.scala#L27-L35) 定义 `in_a/in_d/out_a/out_d`，注意方向：`in_a`/`out_d` 是 Flipped（外部驱动进来），`in_d`/`out_a` 是本模块驱动出去。

8 个子模块的例化：

[Scheduler.scala:39-91](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/Scheduler.scala#L39-L91) 例化 `sourceA/sourceD/sinkA/sinkD/directory/bankedStore/requests`，并用 `Seq.fill(params.mshrs){ Module(new MSHR(params)) }` 例化一组 MSHR。

MSHR 的**轮询调度器**（每拍选一个 MSHR 推进）：

[Scheduler.scala:97-121](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/Scheduler.scala#L97-L121) 先用 `mshr_request` 收集「有可执行动作（a/d/dir）」的 MSHR 位图，再用 `robin_filter` 配合 `leftOR` 实现公平的轮询优先级编码（`mshr_selectOH`），最后 `schedule = Mux1H(mshr_selectOH, ...)` 选出本拍服务的那一个 MSHR。这是 L2 吞吐的关键——多个 miss 可并行驻留在不同 MSHR，每拍挑一个推进。

目录 miss → MSHR allocate：

[Scheduler.scala:185-211](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/Scheduler.scala#L185-L211) 当目录结果 miss、`alloc` 为真、且选中某空闲 MSHR（`mshr_insertOH`）时，把 set/tag/way/opcode/source 等整套 `Status` 写进该 MSHR（`m.io.allocate`）。

二级 miss 合并（`tagMatches` / `requests`）：

[Scheduler.scala:174-183, 217-223](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/Scheduler.scala#L174-L183) `tagMatches` 检测「新 miss 是否命中某个已在跑的 MSHR（同 set 同 tag 且该 MSHR valid）」；命中则不新分配 MSHR（`alloc=false`），而是把这个 secondary miss 挂到 `requests` ListBuffer 的同一个 MSHR 槽下，复用同一次外部取数——这与 u6-l3 的 L1 primary/secondary miss 思想一致，只是挪到了 L2。

SourceA 出口（发外部请求）：

[SourceA.scala:44-54](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/SourceA.scala#L44-L54) 把 MSHR 要发的请求变成 `out_a`，地址用 `params.expandAddress(tag, l2cidx, set, offset)` 重建（[SourceA.scala:49](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/SourceA.scala#L49)）。

SinkD 入口（收回填）：

[SinkD.scala:59-70](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/SinkD.scala#L59-L70) `out_d` 的数据被寄存一拍后形成 `resp`，其 `source` 字段直接指出该回填属于哪个 MSHR（[SinkD.scala:61](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/SinkD.scala#L61)）；注意 SinkD 并不直接写 BankedStore，而是把 set/way 交给 Scheduler，由 Scheduler 经 `bankedStore.io.sinkD_adr/dat` 落库（[Scheduler.scala:276-281](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/Scheduler.scala#L276-L281)）。

#### 4.3.4 代码实践

**实践目标**：用源码确认「L2 命中不分配 MSHR、miss 才分配」这一设计。

**操作步骤**：

1. 在 [Scheduler.scala:217](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/Scheduler.scala#L217) 看到 `requests.io.push.valid := directory.io.result.valid && (!directory.io.result.bits.hit) && !flush`——只有 miss 才入 ListBuffer。
2. 在 [Scheduler.scala:244-252](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/Scheduler.scala#L244-L252) 看到 `dir_result_buffer` 只在 `hit || dirty || last_flush` 时入队——命中走这条短路给 SourceD，不经 MSHR。
3. 对照 [Scheduler.scala:257-268](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/Scheduler.scala#L257-L268)：SourceD 的输入在 `!schedule.d.valid`（命中）时取自 `dir_result_buffer`，否则取自 `schedule.d`（MSHR 交付的 miss）。

**需要观察的现象**：命中路径的数据来自 `dir_result_buffer`（目录→SourceD→BankedStore 读），miss 路径的数据来自 MSHR（外部内存→SinkD→MSHR→SourceD）。

**预期结果**：你能画出两条清晰分支——hit 走目录缓冲，miss 走 MSHR。

#### 4.3.5 小练习与答案

**练习 1**：L2 的目录除了判命中，还承担什么 u6-l3 里 L1 TagAccess 没有的职责？
**答**：选受害者 way（miss 时从 `victim_tag` 决定驱逐哪一路），并维护脏位以便驱逐前回写；还要响应 Hint 的 flush/invalidate 全表扫描（见 [Directory_test.scala:135-159](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/Directory_test.scala#L135-L159) 的 flushCount 扫描）。

**练习 2**：`mshr_request` 把三类动作（schedule.a / schedule.d / schedule.dir）「或」在一起（[Scheduler.scala:97-101](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/Scheduler.scala#L97-L101)），为什么？
**答**：因为一个 MSHR 在其生命周期里要依次完成「写目录（dir）→ 发外部 Get（a）→ 收齐后交付 SourceD（d）」等多个动作，每个动作都可能因下游未就绪而停顿；只要任一动作可执行，该 MSHR 就应当被调度，避免它被卡死。

---

### 4.4 MSHR、BankedStore 与 SourceD：miss 跟踪与数据通路

#### 4.4.1 概念说明

这一节深入 L2 处理 miss 的三个「执行零件」：

- **MSHR**：一个 miss 的一生。每个 MSHR 是一个小状态机，记住「我在等谁、我等到了没、下一步交给谁」。
- **BankedStore**：L2 的数据存储。它不关心 coherence、不关心 source 路由，只认 set/way/mask 读写数据。
- **SourceD**：把数据「送出去」的总出口。它用一个 8 状态的 FSM 区分命中读、命中写、miss 回填、dirty 受害者写回等不同情形，分别走不同的 BankedStore 读写序列。

理解这三者后，一次 L2 miss 的数据通路就完整了。

#### 4.4.2 核心流程

MSHR 的生命周期（状态由几个寄存器位驱动，而非显式枚举）：

1. **allocate**：目录 miss 时被写入 set/tag/way/source/opcode 等（`request` 寄存器）。
2. **发 Get**：`sche_a_valid` 置位，在被调度选中且 `SourceA` 就绪时，把 Get（opcode=`Get`）发往 `out_a`，fire 后清 `sche_a_valid`。
3. **等回填**：`SinkD` 收到外部数据，按 source 匹配到本 MSHR，触发 `sinkd`，`sink_d_reg` 置位，数据存进 `data_reg`。
4. **交付 SourceD**：`schedule.d.valid := io.valid && sink_d_reg && !sche_a_valid`，把回填数据交给 SourceD，由 SourceD 发 `in_d` 给 L1。
5. **写目录**：若该 miss 是 Get（读），还需经 `schedule.dir` 把新 tag 写进目录，使该行变有效。

BankedStore 的寻址很有特点——它把 (set, way) 二维地址**拍扁**成一维：

\[ \text{setIndex} = \text{set} \times \text{ways} + \text{way} \]

这样每个 (set, way) 组合对应 SRAM 里唯一一行，读写只需一个一维地址。BankedStore 有两套写来源（SinkD 的内存回填、SourceD 的 L1 写）与一路读（SourceD 的命中读），用 `sinkD_adr.valid` 优先仲裁。

SourceD 的 FSM（8 个 stage）大致分工：

- **stage_1**：空闲/判定。依据 `hit`、`dirty`、`opcode` 决定走哪条支路。
- **命中读 / miss 脏受害者**：先 `bs_radr` 读 BankedStore，再决定是否经 `io.a` 把脏数据交 SourceA 写回。
- **命中写**：经 `bs_wadr` 把 putBuffer 里的数据写进 BankedStore。
- **stage_4**：发出 `in_d` 响应（命中读返回 BankedStore 数据，miss 返回 MSHR 的 `data_reg`，写返回 AccessAck）。
- **stage_3/7/8**：处理「写响应与脏写回需同拍成」等边界情形。

#### 4.4.3 源码精读

MSHR 的 IO 与状态：

[MSHR.scala:42-54](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/MSHR.scala#L42-L54) 定义 `allocate`（目录分配）/`status`（对外暴露 set/tag/way 等）/`schedule`（a/d/dir 三路动作）/`sinkd`（收 SinkD 回填）/`merge`（合并 secondary 写）等端口。

发外部 Get：

[MSHR.scala:97-109](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/MSHR.scala#L97-L109) `schedule.a` 在 `sche_a_valid && !mshr_wait` 时有效，opcode 固定为 `Get`，source 取自 `request.source`（即原请求的 source，会被 L2 原样 echo 回去）。`mshr_wait` 用于 miss 脏受害者场景，阻塞过早的同地址 miss（见 [MSHR.scala:55,97](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/MSHR.scala#L55)）。

收 SinkD 回填 + 交付 SourceD：

[MSHR.scala:81-95](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/MSHR.scala#L81-L95) `sinkd.valid` 时把外部数据存进 `data_reg`（第 85-87 行）；`schedule.d.valid := io.valid && sink_d_reg && !sche_a_valid`（第 89 行）表示「外部 Get 已发、回填已收、可交付」三者齐备才向 SourceD 交数据，且 `schedule.d.bits.data := data_reg`。

BankedStore 的拍扁寻址与读写：

[BankedStore.scala:93-112](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/BankedStore.scala#L93-L112) 第 101 行 `set_index = set*ways + way` 拍扁地址；第 105-108 行读口接 `sourceD_radr`、写口在 `sourceD_wadr` 与 `sinkD_adr` 间选（`sinkD_adr` 优先，第 109 行 `sourceD_wadr.ready := !io.sinkD_adr.valid`）。bank 数 `numBanks = rowBytes/writeBytes`（第 90 行），默认 `writeBytes=1`、`beatBytes=128`，故 `numBanks=128`，每个写字节粒度一个 bank。

SourceD 的 FSM 与响应组装：

[SourceD.scala:129-255](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/SourceD.scala#L129-L255) 是 8 状态机的主体；其中 stage_1（第 130-203 行）依 `s1_need_r`（需读 BankedStore）/`s1_need_w`（需写 BankedStore）/`hit`/`dirty` 分流。

[SourceD.scala:264-271](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/SourceD.scala#L264-L271) 组装 `in_d` 响应：opcode 在 Get 时为 `AccessAckData`（带数据）、在 Hint last_flush 时为 `HintAck`、否则 `AccessAck`；数据在 Get 命中时取 `bs_rdat.data`，miss 时取 `s_final_req.data`（来自 MSHR 的回填）。

Scheduler 里 SinkD 回填落 BankedStore 的接线：

[Scheduler.scala:276-285](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/Scheduler.scala#L276-L285) `bankedStore.io.sinkD_adr` 由被选中的 MSHR 的 `schedule.dir` 驱动（set/way），`sinkD_dat.data := schedule.data`（MSHR 的回填数据），从而把外部回填写进正确的 (set, way)。

#### 4.4.4 代码实践

**实践目标**：用参数推导 L2 的容量与 MSHR 数量，理解 miss 并发度。

**操作步骤**：

1. 读 L2 规模：`l2cache_NSets=64, l2cache_NWays=16, l2cache_BlockWords=32, l2cache_memCycles=32`（[parameters.scala:99-109](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L99-L109)）。
2. 算容量：`blockBytes = l2cache_BlockWords<<2 = 128` 字节；总容量 = `sets × ways × blockBytes = 64 × 16 × 128 = 131072` 字节 = **128 KiB**。
3. 算 MSHR 数量：`mshrs = all_mshrs = out_mshrs = max(if dirReg(false) then 3 else 2, (memCycles + blockBeats - 1)/blockBeats)`（[Parameters.scala:424-432](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/Parameters.scala#L424-L432)）；`blockBeats = blockBytes/beatBytes = 128/128 = 1`，故 `mshrs = max(2, 32) = 32`。

**需要观察的现象**：L2 有 32 个 MSHR，意味着最多 32 个不同 cacheline 的 miss 可同时驻留、由轮询调度器（4.3）每拍推进一个。

**预期结果**：默认 L2 容量 128 KiB、16 路、64 组、32 个 MSHR、行大小 128 字节。

#### 4.4.5 小练习与答案

**练习 1**：BankedStore 为什么用 `set*ways + way` 把二维地址拍扁，而不像 L1 那样 set 和 way 分开寻址？
**答**：拍扁后每个 (set, way) 唯一映射到一维行地址，可用单个 SRAMTemplate（`set=rowEntries, way=numBanks`，[BankedStore.scala:93-94](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/BankedStore.scala#L93-L94)）一次访问整行，便于按 writeBytes 粒度做 bank 化的部分写。

**练习 2**：MSHR 的 `schedule.d.valid := io.valid && sink_d_reg && !sche_a_valid`（[MSHR.scala:89](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/MSHR.scala#L89)）里 `!sche_a_valid` 的作用？
**答**：确保外部 Get 请求（`sche_a_valid`）已经成功发出（fire 后清零）之后，才允许把回填数据交给 SourceD；避免在还没真正向外发请求时就「交付」数据，保证读 miss 的取数—回填顺序正确。

## 5. 综合实践

**任务**：追踪一次完整的 **L2 read miss**，画出从 L1 DCache miss 到外部内存回填、再到 L1 收数据的端到端流程图，并在每一跳标注传递的 source 字段与关键信号。

**建议步骤**：

1. **起点**：L1 DCache miss，发出 `memReq`，其 `a_source` 为 13 位 `[op|mshr|setIdx]`。经 [L1Cache2L2Arbiter.scala:35](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L1Cache/L1Cache2L2Arbiter.scala#L35) 贴 cache_id=1 → 14 位。
2. **集群层**：经 `SM2clusterArbiter`（[GPGPU_top.scala:538](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L538)）贴 sm_id → 15 位；`l2Distribute` 按地址 l2cidx 路由（默认 1 个 L2，直通）；`cluster2L2Arbiter` 贴 cluster_id（默认 0 位）→ 进入 L2 `in_a`。
3. **L2 入口**：`SinkA`（[SinkA.scala:78-92](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/SinkA.scala#L78-L92)）用 `parseAddress` 拆出 tag/set，整理成 `FullRequest` 交目录。
4. **查目录**：`Directory_test` 判 `hit=false`，选受害者 way（假设不脏，免回写）。
5. **分 MSHR**：Scheduler 给空闲 MSHR 发 `allocate`（[Scheduler.scala:190-210](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/Scheduler.scala#L190-L210)），source 原样存入。
6. **发外部 Get**：MSHR 被轮询选中后经 `SourceA`（[SourceA.scala:49](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/SourceA.scala#L49)）把 Get 发到 `out_a` → 经 `GPGPU_top.io.out_a` → `AXI4Adapter` → DDR。
7. **收回填**：DDR 数据经 `out_d` 回来，`SinkD`（[SinkD.scala:61](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/SinkD.scala#L61)）按 source 找到 MSHR，数据落 BankedStore（[Scheduler.scala:276-281](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/L2cache/Scheduler.scala#L276-L281)）并写目录。
8. **回 L1**：MSHR `schedule.d` 交 SourceD，SourceD 经 `in_d` 把 AccessAckData 发出，**source 字段 15 位原样回填**。
9. **回程剥标**：`cluster2L2Arbiter` → `SM2clusterArbiter` → `L1Cache2L2Arbiter` 逐层按各自 id 位选路、剥标，最终低 13 位回到 DCache 的 MSHR，命中它最初挂起的那个请求。

**产出**：一张包含上述 9 步的流程图，标出每一步的 source 位宽（13→14→15→15…→15→14→13）与方向（贴标/剥标/echo）。如果条件允许，可在 sim-verilator 跑 vecadd 时开启 [GPGPU_top.scala:315-326](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L315-L326) 的 `[L1C]` printf，核对日志里出现的 SM/CACHE/ADDR 是否与你的推算一致（**待本地验证**）。

## 6. 本讲小结

- `L1Cache2L2Arbiter` 把每 SM 内 ICache/DCache 两路 memReq 仲裁成一路，靠在 source 最高位贴 1 位 cache_id 区分；回程按该位分发。
- 三层互联 `SM2clusterArbiter → l2Distribute → cluster2L2Arbiter` 沿用 u2-l2 的 source 贴标/剥标机制；`l2Distribute` 不贴标，只按地址 l2cidx 选 L2 bank；L2 全程对 source 透明、只做 echo。
- L2 `Scheduler` 是 SiFive `block-inclusive-cache` 的精简移植，由 SinkA/Directory/MSHR/requests/SourceA/SinkD/SourceD/BankedStore 八件套组成，是包容性目录式写回 cache。
- 目录 miss 才分配 MSHR；二级 miss 经 `tagMatches`+`requests` ListBuffer 合并到同一 MSHR，复用同一次外部取数；命中则走 `dir_result_buffer` 短路给 SourceD。
- BankedStore 用 `set*ways+way` 拍扁寻址、按 writeBytes 分 bank；MSHR 用 `sche_a_valid/sink_d_reg` 等寄存器位驱动「发 Get→收回填→交付 SourceD→写目录」的生命周期；SourceD 用 8 状态 FSM 区分命中读/写、miss 回填、脏受害者写回。
- 默认 L2：128 KiB、16 路、64 组、128 字节行、32 个 MSHR；source_bits=16（含 1 位 MMU 预留）。

## 7. 下一步学习建议

- **u7-l1（MMU 与 TLB/PTW）**：本讲多次提到 source 字段最低 1 位预留给「TLB/L1Cache 区分」、以及 MMU 开启时 `GPGPU_top` 会插入 `tlb_req_arb` 与 `L2TlbToL2CacheXBar`，把 TLB 的页表遍历请求也送进 L2。下一讲会展开这条 TLB→L2→DDR 的缺页处理链路。
- **u7-l2（AXI 接口与 host 驱动）**：本讲的 `out_a/out_d` 在 `GPGPU_axi_top` 里经 `AXI4Adapter` 桥接成 AXI4 接 DDR；host 侧 kernel 派发则经 `AXI4Lite2CTA`。若想看清 L2 取数请求如何变成 AXI 的 AR/AW/W/R/B，可接着读这一讲。
- **延伸源码阅读**：`L2cache/SourceD.scala` 的 8 状态 FSM 是 L2 最复杂的零件，建议结合一次「脏受害者写回」场景逐状态走读；`L2cache/Directory_test.scala` 的 flush/invalidate 全表扫描逻辑（`flushCount`）也值得作为进阶练习。
