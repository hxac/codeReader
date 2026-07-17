# 布局代价与延迟模型

## 1. 本讲目标

上一讲（u5-l2）我们讲了模拟退火布局里的「移动生成器」——退火每一步如何扰动布局。但退火要判断一次移动「值不值得接受」，必须先算出这次移动让总代价变化了多少（即 ΔC）。在数百万次迭代里，每次都把全芯片代价从头算一遍是绝对不可行的。

本讲聚焦支撑「快速评估一次移动」的两块底层基石：

1. **布局延迟模型**：用一张预计算的查找表，以 O(1) 代价给出「任意两点之间布线延迟」的估计。我们讲清楚 `simple` / `delta` / `delta_override` 三种模型的差异、各自的精度与开销权衡，以及它们何时被自动切换。
2. **增量代价与事务回滚机制**：讲清楚 `move_transactions` 如何只记录「被这次移动影响的块与引脚」、`compressed_grid` 如何让移动生成始终落在合法瓦片上，以及 `BlkLocRegistry` 的 apply → commit/revert 三段式状态机如何「先试算、再决定提交或回滚」。

学完本讲，你应当能够：

- 说出三种布局延迟模型各自的数据来源、索引维度与适用场景，并能解释 VPR 默认为何经常把 `simple` 推断成 `delta`。
- 解释为什么延迟模型必须是 O(1) 查表，否则整个退火就无法承受。
- 画出一次退火移动从 `propose_move` 到 `apply / 评估 / commit | revert` 的完整时序，并指出每一步改写了哪些数据结构。
- 理解 `BlkLocRegistry` 用「正向表先改、反向表延后改」实现事务的思路。

## 2. 前置知识

本讲假设你已掌握 u5-l1、u5-l2 的内容，这里做最简回顾，并补充几个本讲要用到的概念。

- **模拟退火与 ΔC**：退火每次提议一个移动，计算代价变化 ΔC；变好（ΔC<0）必接受，变差按概率 \(P=\exp(-\Delta C / T)\) 接受（见 u5-l1）。因此「计算 ΔC 的速度」直接决定退火可行性。
- **布局代价的构成**：默认算法 `CRITICALITY_TIMING_PLACE` 下，总代价是「线长项 + 时序项（可选拥塞项）」的加权和：

\[
\Delta C = (1-\lambda)\,\Delta C_{\text{bb}}\cdot n_{\text{bb}} \;+\; \lambda\,\Delta C_{\text{tim}}\cdot n_{\text{tim}} \;+\; \dots
\]

  其中 \(\lambda\) 是 `timing_tradeoff`，\(n_{\text{bb}},n_{\text{tim}}\) 是归一化系数。**时序项**的关键原料是「每条点对点连接的延迟」，这正是布局延迟模型要提供的。
- **点对点连接（connection）**：一条网（net）有一个驱动引脚、若干接收引脚，每个「驱动块 → 某接收引脚」称为一个 connection。时序代价 ≈ Σ（criticality × delay）。
- **`g_vpr_ctx` 全局总线**（见 u3-l4）：device（含器件网格、RR 图）、clustering（聚簇网表）、routing 等子上下文经它共享；本讲大量代码读取 `device_ctx.grid`。
- **物理瓦片与逻辑块**（见 u2-l2 / u3-l3）：聚簇网表的块只携带逻辑块类型，落到哪个物理瓦片由 `equivalent_tiles` 决定。延迟查询以「物理瓦片位置」`t_physical_tile_loc{x,y,layer}` 为坐标。

> 术语提醒：本讲的「延迟模型（delay model）」专指**布局阶段**用的快速延迟估计，区别于布线阶段（u6）的精确延迟、以及时序分析（u7）里 Tatum 计算的真实延迟。布局延迟模型只追求「快且方向正确」，不追求精确。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `vpr/src/place/delay_model/place_delay_model.h` | 抽象基类 `PlaceDelayModel`，定义 `compute/delay/read/write` 接口；以及单连接延迟辅助函数。 |
| `vpr/src/place/delay_model/simple_delay_model.{h,cpp}` | `SimpleDelayModel`：基于路由前瞻（lookahead）的 5 维延迟表，按物理瓦片类型区分。 |
| `vpr/src/place/delay_model/delta_delay_model.{h,cpp}` | `DeltaDelayModel`：基于「位置差 Δx/Δy」的 4 维延迟表，值由真实布线采样得到。 |
| `vpr/src/place/delay_model/override_delay_model.{h,cpp}` | `OverrideDelayModel`：在 delta 模型之上叠加「直接连接」逐引脚类覆盖。 |
| `vpr/src/place/delay_model/PlacementDelayModelCreator.cpp` | 工厂：根据选项实例化三种模型之一，并触发 `compute`。 |
| `vpr/src/place/delay_model/compute_delta_delays_utils.cpp` | delta/override 模型采样的核心：选若干源点、跑真实布线、填表、修补空洞。 |
| `vpr/src/route/router_delay_profiling.{h,cpp}` | `RouterDelayProfiler`：simple 模型从前瞻取值、delta/override 模型跑真实布线的统一封装。 |
| `vpr/src/base/read_options.cpp` | `--place_delay_model` 选项注册、字符串↔枚举转换、`simple→delta` 的推断规则。 |
| `vpr/src/base/vpr_types.h` | 枚举 `PlaceDelayModelType {SIMPLE, DELTA, DELTA_OVERRIDE}`。 |
| `vpr/src/place/move_transactions.{h,cpp}` | `t_pl_blocks_to_be_moved`：记录一次移动涉及的块、被腾空/占据的位置、受影响引脚。 |
| `vpr/src/place/compressed_grid.{h,cpp}` | `t_compressed_block_grid`：按块类型压缩的坐标空间，O(1) 找合法邻居。 |
| `vpr/src/place/placer_state.{h,cpp}` | `PlacerTimingContext`：连接延迟/代价的「已提交」与「提议」两套缓冲，以及 `commit_td_cost/revert_td_cost`。 |
| `vpr/src/base/blk_loc_registry.cpp` | `apply_move_blocks / commit_move_blocks / revert_move_blocks`：位置表的事务三段式。 |
| `vpr/src/place/annealer.cpp` | 退火内层循环，把上述组件串成「提议→试算→提交/回滚」。 |

---

## 4. 核心概念与源码讲解

### 4.1 延迟模型类型：simple / delta / delta_override

#### 4.1.1 概念说明

布局退火每次评估一个移动，都要重新算受影响连接的延迟。如果每次都像布线器那样跑一遍迷宫搜索，单次移动就要毫秒级，百万次迭代根本跑不完。所以 VPR 在布局开始前**一次性预计算一张延迟查找表**，之后每次查询都是 O(1) 数组取值。

这张表「长什么样」、值「从哪来」，就区分出三种模型。先看它们的差异直觉：

- **simple**：表的值来自**路由前瞻（router lookahead）**——前瞻本身就是一张预计算的「代价地图」（见 u6-l4）。simple 把前瞻里的值搬进自己的表，**不跑任何布线**。表按「物理瓦片类型 + 层 + Δx + Δy」索引，所以能区分不同瓦片类型。
- **delta**：表的值来自**真实跑布线采样**——从若干源点出发，用 Dijkstra/A\* 扩展到全芯片，记录每个 (Δx,Δy) 的实测最小延迟。表只按「层 + Δx + Δy」索引，**不含瓦片类型维度**，隐含假设「布线结构规整，相同距离延迟相近」。
- **delta_override**：在 delta 表之上，再为架构里声明的**直接连接（`<direct>`，如进位链）**逐「引脚类」测量并覆盖，纠正 delta 表对特殊连线的低估。

一句话总结精度/开销权衡：

| 模型 | 值的来源 | 索引维度 | 延迟值精度 | 类型区分 | 建表开销 | 查询开销 |
| --- | --- | --- | --- | --- | --- | --- |
| simple | 路由前瞻（无布线） | 5D：类型×层×层×Δx×Δy | 近似（前瞻估计） | 区分瓦片类型 | 低（仅前瞻查表） | O(1) |
| delta | 真实布线采样 | 4D：层×层×Δx×Δy | 较准（实测路径） | 不区分类型 | 中高（跑布线） | O(1) |
| delta_override | delta + 直接连接实测 | 4D 表 + 逐引脚类覆盖表 | 最准（特殊连线纠正） | 部分区分 | 最高 | O(1)~O(log n) |

注意「精度」有两层含义：值的准确度（delta 实测更准）与索引粒度（simple 区分瓦片类型更细）。二者此消彼长——这也是本讲练习要对比的核心。

#### 4.1.2 核心流程

三种模型都实现同一个抽象基类接口，由工厂按选项实例化：

```text
PlacementDelayModelCreator::create_delay_model()
  ├── 取/缓存路由前瞻 RouterLookahead
  ├── 构造 RouterDelayProfiler(lookahead)        # simple 在构造期就把前瞻值搬进 min_delays_
  ├── 据 placer_opts.delay_model_type 实例化:
  │     SIMPLE         -> SimpleDelayModel
  │     DELTA          -> DeltaDelayModel(is_flat)
  │     DELTA_OVERRIDE -> OverrideDelayModel(is_flat)
  ├── 若有 read_placement_delay_lookup  -> model.read(file)   # 直接读盘
  │   否则                                -> model.compute(...)  # 现算建表
  └── 若有 write_placement_delay_lookup -> model.write(file)
```

查询时三者都实现 `delay(from_loc, from_pin, to_loc, to_pin)`，返回两点间延迟估计，内部都是一次表查询。布局主循环通过 `comp_td_single_connection_delay()` 调用它来填某条连接的延迟。

值的来源差异发生在**建表期**：

- **simple** 建表只查前瞻：`RouterDelayProfiler` 构造函数里，对每个 `[物理类型][from层][to层][Δx][Δy]` 调 `lookahead->get_opin_distance_min_delay(...)` 填入 `min_delays_`；`SimpleDelayModel::compute()` 再把这些值搬进自己的 5 维表。
- **delta** 建表要跑布线：`compute_delta_delay_model()` 选 6 个源点（避开边缘效应），对每个源点跑一次 Dijkstra 扩展（`generic_compute_matrix_dijkstra_expansion`）或逐 sink 的 A\*（`generic_compute_matrix_iterative_astar`），把实测延迟按 (Δx,Δy) 归桶，最后用 reducer（默认 `min`）合并多源结果。
- **delta_override** 先建一张 delta 表作为 base，再遍历所有 `<direct>` 直接连接，对每个引脚对跑布线测延迟，写进覆盖表。

#### 4.1.3 源码精读

**(a) 抽象接口与工厂。** 基类只规定四个纯虚方法：

[vpr/src/place/delay_model/place_delay_model.h:40-74](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/delay_model/place_delay_model.h#L40-L74) — 定义 `compute()`（建表）、`delay()`（查询）、`read()/write()`（落盘）。任何布局延迟模型只需实现这四个方法。

工厂按枚举三选一：

[vpr/src/place/delay_model/PlacementDelayModelCreator.cpp:63-89](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/delay_model/PlacementDelayModelCreator.cpp#L63-L89) — `if SIMPLE → SimpleDelayModel; else if DELTA → DeltaDelayModel; else if DELTA_OVERRIDE → OverrideDelayModel`，然后 `read` 或 `compute`。

枚举定义：

[vpr/src/base/vpr_types.h:952-956](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h#L952-L956) — `enum class PlaceDelayModelType { SIMPLE, DELTA, DELTA_OVERRIDE };`。

**(b) simple 模型：值来自前瞻，5 维表。** 头文件类注释直接点明它与 delta 的根本区别——「基于路由前瞻，而非跑布线」：

[vpr/src/place/delay_model/simple_delay_model.h:8-43](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/delay_model/simple_delay_model.h#L8-L43) — 私有成员 `delays_` 是 5 维 `NdMatrix<float,5>`，注释说明下标为 `[物理类型][from层][to层][Δx][Δy]`，并解释了为何区分目标层（层间互连可能不均匀）。

值的真正来源在 profiler 构造函数：

[vpr/src/route/router_delay_profiling.cpp:33-48](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/route/router_delay_profiling.cpp#L33-L48) — 对每个 (类型,层,层,Δx,Δy) 调 `lookahead->get_opin_distance_min_delay(...)` 填 `min_delays_`。注意循环规模是 `num_tile_types × num_layers² × width × height`，但每次只是查前瞻，无布线，所以**建表便宜**。

simple 的查询是一次纯数组取值（外加可选 interposer 项）：

[vpr/src/place/delay_model/simple_delay_model.cpp:56-69](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/delay_model/simple_delay_model.cpp#L56-L69) — `return delays_[from_tile_idx][from_layer][to_layer][delta_x][delta_y] + interposer_delay;`。

**(c) delta 模型：值来自真实布线，4 维表。** 类注释强调「基于位置差」：

[vpr/src/place/delay_model/delta_delay_model.h:6-41](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/delay_model/delta_delay_model.h#L6-L41) — 私有成员 `delays_` 是 4 维 `NdMatrix<float,4>`，下标 `[from层][to层][Δx][Δy]`，**没有瓦片类型维度**。查询只看距离：

[vpr/src/place/delay_model/delta_delay_model.cpp:26-31](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec62818c9db312e24967de9d6283410ab19/vpr/src/place/delay_model/delta_delay_model.cpp#L26-L31) — `return delays_[from_loc.layer_num][to_loc.layer_num][delta_x][delta_y];`，其中 `delta_x=|from.x-to.x|`、`delta_y=|from.y-to.y|`。

delta 的建表要跑布线，核心入口：

[vpr/src/place/delay_model/compute_delta_delays_utils.cpp:783-813](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/delay_model/compute_delta_delays_utils.cpp#L783-L813) — `compute_delta_delay_model()` 调 `compute_delta_delays()` 跑采样，再把未初始化点置无穷、用邻居均值修补空/不可达坐标（`fix_empty_coordinates`/`fill_impossible_coordinates`）、最后 `verify_delta_delays` 断言无负延迟。

采样为何选多个源点？为了消除**边缘效应**——靠近芯片边的源点，其布线选项被截断，测得的延迟偏大。源码用一张「九宫格」示意图选了 6 个源点（四条边的中点 + 四个内部点）：

[vpr/src/place/delay_model/compute_delta_delays_utils.cpp:189-341](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/delay_model/compute_delta_delays_utils.cpp#L189-L341) — 注释里画了 3×3 分区（A~I），并依次从 `low/low`、`high/high`、`high/low`、`low/high` 等点出发跑 `generic_compute_matrix(...)`，每个 (Δx,Δy) 收集到一组延迟，最后用 reducer 合并。每个 (Δx,Δy) 桶里多个采样值如何合并由 `--place_delay_model_reducer` 控制（默认 `min`，可选 `max/median/arithmean/geomean`），见 `delay_reduce()`（[delta_delay_model.cpp 同文件 compute_delta_delays_utils.cpp:692-723](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/delay_model/compute_delta_delays_utils.cpp#L692-L723)）。

**(d) delta_override：delta 为底 + 逐引脚类覆盖。**

[vpr/src/place/delay_model/override_delay_model.cpp:16-30](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/delay_model/override_delay_model.cpp#L16-L30) — `compute()` 先建一张 delta 表作为 `base_delay_model_`（注意 `measure_directconnect=false`，即 delta 底表**不含**直接连接），再调 `compute_override_delay_model_()` 专门测直接连接。

覆盖逻辑：遍历架构里所有 `<direct>`，对每对引脚找一组合法源/汇 RR 节点，跑布线测延迟，按 `(from_type, from_class, to_type, to_class, delta_x, delta_y)` 写入 `delay_overrides_`（一张 `flat_map2`）。查询时**先查覆盖表，命中则用；未命中回退 delta 底表**：

[vpr/src/place/delay_model/override_delay_model.cpp:112-142](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/delay_model/override_delay_model.cpp#L112-L142) — 注意 override 用的是**带符号**的 `delta_x = to_loc.x - from_loc.x`（注释说明：直接连接的 +Δ 与 −Δ 延迟可能不同，不能用绝对值）。

`override_delay_model.h` 里的 `t_override` 还藏着一个性能细节：为了在 `flat_map2` 里快速比较键，`operator<` 用 `ALWAYS_INLINE` + `std::lexicographical_compare` 把 6 个 `short` 当数组逐元素比较，注释称这能让布局时间下降约 5%（见 [override_delay_model.h:57-100](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/delay_model/override_delay_model.h#L57-L100) 与对应 `static_assert` 保证无填充字节）。

**(e) 选项与「simple→delta」推断。** CLI 注册：

[vpr/src/base/read_options.cpp:2925-2934](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L2925-L2934) — `--place_delay_model` 帮助文本：`'simple' uses map router lookahead`、`'delta' uses differences in position only`、`'delta_override' uses differences in position with overrides for direct connects`，`default_value("simple")`。

但默认值会被一条**推断规则**覆盖（呼应 u1-l5 的 Provenance/INFERRED 机制）：

[vpr/src/base/read_options.cpp:3902-3906](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/read_options.cpp#L3902-L3906) — 若用户**未显式指定** `--place_delay_model`，且 `router_lookahead_type != MAP`，则推断为 `DELTA`。原因很自然：simple 模型依赖 MAP（代价地图）前瞻来填表，前瞻不是 MAP 时 simple 就没有可靠数据源，只能退回 delta（自己跑布线建表）。这正是本讲练习里要验证的现象。

#### 4.1.4 代码实践

**实践目标**：在同一电路、同一架构上，对比 `simple` 与 `delta` 两种延迟模型在「建表耗时」与「布局结果」上的差异，并确认推断规则生效。

**操作步骤**：

1. 进入 VTR Python 环境（见 u1-l2）：`source .venv/bin/activate`。
2. 用 `run_vtr_flow.py` 跑一个固定设计（如 `doc/src/quickstart` 下的 `blink.v` 与一个示例架构），分别加 VPR 参数：
   - 显式 simple：`--place_delay_model simple`（前提：路由前瞻用 MAP，即默认）；
   - 显式 delta：`--place_delay_model delta`；
   - 不指定，但把 `--router_lookahead` 改成非 MAP（如 `classical`），观察日志里延迟模型是否被推断为 delta。
3. 在 VPR 日志中找到两行计时信息：`Computing placement delta delay look-up`（工厂里的 `ScopedStartFinishTimer`）以及 delta 专有的 `Computing delta delays`（`compute_delta_delay_model` 里的计时器）。对比两次运行的这两个耗时。
4. 对比最终布局的 `Place cost` 与关键路径延迟（布线后时序报告）。

**需要观察的现象**（待本地验证）：

- delta 模型建表明显慢于 simple（因为 delta 要跑布线采样，simple 只查前瞻）。
- 当 `router_lookahead != MAP` 且未指定延迟模型时，日志应出现 `place_delay_model` 被设为 `delta` 的推断痕迹。
- 两者最终时序结果通常接近，但 delta/delta_override 在含进位链等直接连接的架构上可能略优。

> 若本地暂未编译 VPR，可改为**源码阅读型实践**：在 `PlacementDelayModelCreator.cpp` 与 `read_options.cpp:3902` 处分别加一行日志（如 `VTR_LOG("chosen delay model: ...")`），编译后跑一次，直接验证三选一与推断路径。注意：改源码仅为本地实验，勿提交。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `DeltaDelayModel` 的表没有「物理瓦片类型」这一维，而 `SimpleDelayModel` 有？

**参考答案**：delta 模型的值是「在器件上真实布线采样得到的」，但为了控制建表开销，它只从少数源点采样，并**隐含假设 FPGA 布线结构规整**——相同 (层,Δx,Δy) 的延迟与具体瓦片类型无关（见 simple_delay_model.h 注释里「operating under the assumption that the FPGA fabric architecture is regular」）。simple 模型的值直接取自按瓦片类型组织的前瞻代价地图，天然带类型维度，所以能区分。代价是 simple 的值是前瞻估计、非实测。

**练习 2**：`delta_override` 用带符号 Δ 查覆盖表，而 `delta` 用绝对值 Δ 查底表。为什么不同？

**参考答案**：直接连接（`<direct>`）有固定方向（如从 (x,y) 的进位出到 (x+1,y) 的进位入），正负方向的延迟可能不同，故覆盖键用带符号 `to−from`；而 delta 底表刻画的是「一般布线」，假设对称，用绝对值即可减半表规模。源码注释在 override_delay_model.cpp:126-129 明确写了这点。

**练习 3**：把 `--place_delay_model_reducer` 从 `min` 改成 `median`，对 delta 表意味着什么？

**参考答案**：reducer 决定「同一 (Δx,Δy) 多个采样值如何合并」。`min` 取最乐观（最快路径）的延迟，倾向低估；`median` 取中位数，更抗离群点、更稳健，但建表多一次排序。布局用更乐观的延迟会让时序项偏小、退火对时序不够敏感；用 median 则时序估计更接近真实。这是精度与建表开销的又一处权衡。

---

### 4.2 增量代价计算：为何能每秒评估上百万次移动

#### 4.2.1 概念说明

退火内层循环里，每提议一个移动都要算 ΔC。**增量（incremental）**的核心思想是：一次移动只搬动少数几个块，因而只改变了少数网的线长和少数连接的延迟——只重算这些「受影响」部分即可，不必重算全芯片。

让增量代价成立的三个前提，恰好对应三个组件：

1. **延迟模型 O(1) 查表**（4.1 节）：受影响连接的新延迟只需一次数组取值。
2. **压缩网格 compressed_grid**：让移动生成在 O(1) 内落到「该块类型的合法瓦片」上，避免扫描整张网格。
3. **受影响集合的精确记录**（move_transactions，4.3 节）：只对记录在案的块/引脚/网重算。

本节聚焦第 1、2 点如何让「重算受影响部分」足够便宜。

> 关键直觉：全芯片代价 \(C=\sum_{\text{所有连接}}\text{criticality}\cdot\text{delay}+\sum_{\text{所有网}}\text{bb}\)。一次移动只牵动 \(k\) 个块，受影响连接数 \(m\ll\) 总连接数。于是 \(\Delta C\) 只需算 \(m\) 条连接与若干网的增量，复杂度从 \(O(\text{全芯片})\) 降到 \(O(m)\)。

#### 4.2.2 核心流程

一次移动评估的简化时序：

```text
propose_move()                       # 生成移动，record_block_move 记录受影响块
  │
apply_move_blocks()                  # 先把块的新位置写进「正向表」block_locs_（见 4.3）
  │
find_affected_nets_and_update_costs()
  ├── 对每个受影响网：算新 bb  → Δ(bb_cost)
  ├── 对每个受影响连接：调 delay_model->delay(...) → 新 delay
  │     经 comp_td_single_connection_delay() 拿到单连接延迟
  │     新 timing_cost = criticality × 新 delay  → Δ(timing_cost)
  └── 把「提议值」放进 proposed_connection_delay / proposed_connection_timing_cost
  │
delta_c = (1-λ)·Δbb·n_bb + λ·Δtim·n_tim + ...   # 见 annealer.cpp:631
  │
assess_swap_(delta_c, T) → ACCEPTED / REJECTED   # 决定提交还是回滚（见 4.3）
```

注意：评估阶段把结果写进的是 **`proposed_*`（提议）缓冲**，而不是已提交的 `connection_delay/connection_timing_cost`。只有移动被接受后才把提议值「转正」（见 4.3）。这种「双缓冲」是事务回滚能廉价实现的关键。

#### 4.2.3 源码精读

**(a) 单连接延迟 = 延迟模型一次查表。** 基类头里声明的辅助函数是延迟模型与增量代价之间的桥梁：

[vpr/src/place/delay_model/place_delay_model.h:30-34](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/delay_model/place_delay_model.h#L30-L34) — `comp_td_single_connection_delay(delay_model, block_locs, net_id, ipin)`：取驱动块位置与该接收引脚所在块位置，调 `delay_model->delay(from_loc, from_pin, to_loc, to_pin)` 得到这条连接的延迟。这正是「受影响连接的新延迟」的来源。

**(b) 受影响网与代价增量。** 退火主循环把这一切串起来：

[vpr/src/place/annealer.cpp:599-633](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/annealer.cpp#L599-L633) — 先 `apply_move_blocks`，再 `net_cost_handler_.find_affected_nets_and_update_costs(delay_model_, ...)` 算出 `cost_terms_delta.bb_cost` 与 `timing_delta_c`，最后套用归一化加权得到 `delta_c`。注意 `find_affected_nets_and_update_costs` 只遍历受 move 影响的网/引脚——增量性的源头。

**(c) 压缩网格：O(1) 找合法目标位置。** 器件网格上，某类块（如 DSP）可能只存在于零散的几列。若每次移动都「随机选一个 x、再检查这个 x 有没有该类块」，会大量空试。压缩网格把「该类块实际占据的坐标」单独抽成有序数组：

[vpr/src/place/compressed_grid.h:10-28](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/compressed_grid.h#L10-L28) — `compressed_to_grid_x[layer][cx]` 给出「第 cx 个压缩列对应的真实 x」。注释举了 DSP 的例子：若 DSP 只在第 2、3、5 列，则压缩 x 数组只有 3 个元素。于是「下一个合法列」就是数组下一个元素，无需扫描。

压缩坐标 ↔ 真实坐标互转是 O(1)/O(log n)（数组有序，二分）：

[vpr/src/place/compressed_grid.h:162-165](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/compressed_grid.h#L162-L165) — `compressed_loc_to_grid_loc`：`{compressed_to_grid_x[layer][cx], compressed_to_grid_y[layer][cy], layer}`。

移动生成器据此把「随机选压缩坐标」翻译成真实合法位置：

[vpr/src/place/move_utils.h:299-302](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_utils.h#L299-L302) — `compressed_grid_to_loc(blk_type, compressed_loc, to_loc, rng)`。

[vpr/src/place/move_utils.h:334-344](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_utils.h#L334-L344) — `find_compatible_compressed_loc_in_range(...)`：在给定 rlim 的压缩搜索范围内找一个兼容且（可选）空闲的位置。

当移动把一块搬到「另一类瓦片附近」时，还需近似对齐到最近的压缩点：

[vpr/src/place/compressed_grid.h:128-160](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/compressed_grid.h#L128-L160) — `grid_loc_to_compressed_loc_approx`：找最近的压缩坐标（处理该点不属于本类块的情形）。

> 串起来看：**延迟模型**让「受影响连接的新延迟」O(1) 可得；**压缩网格**让「新位置合法」O(1) 可得；**move_transactions**（下一节）精确圈定「受影响范围」。三者合力，使单次 ΔC 的代价正比于受影响连接数而非芯片规模——这是退火能跑数百万步的工程前提。

#### 4.2.4 代码实践

**实践目标**：通过阅读源码，跟踪「一次移动 → 新延迟 → Δ(timing_cost)」的增量链路，并理解压缩网格如何避免无效移动。

**操作步骤**：

1. 在 `annealer.cpp:607` 的 `find_affected_nets_and_update_costs` 处设断点或加日志，确认它只遍历 `blocks_affected_` 涉及的网，而非全部网。
2. 跟进 `net_cost_handler.cpp`（见 `net_cost_handler.h` 注释「update placement cost when a new move is proposed/committed」），定位它内部调用 `comp_td_single_connection_delay` 的位置，确认每条受影响连接的新延迟来自 `delay_model_->delay(...)`。
3. 在 `compressed_grid.h` 的 `grid_loc_to_compressed_loc_approx` 与 `compressed_loc_to_grid_loc` 处，对照一个含稀疏块类型（如 RAM/DSP）的架构，画一张「真实网格 ↔ 压缩网格」的映射示意。

**需要观察的现象 / 预期结果**：

- 受影响网集合的大小 ≈ 移动块数 × 平均扇出，远小于总网数（增量性）。
- 把一块从 CLB 区移到 DSP 区附近时，必须经 `grid_loc_to_compressed_loc_approx` 把目标对齐到真实 DSP 位置，否则会断言失败。

> 若无法运行，本实践为纯阅读型：重点是确认「重算范围 = 受影响集合」这一不变式在代码里成立。

#### 4.2.5 小练习与答案

**练习 1**：如果延迟模型的 `delay()` 不是 O(1) 查表，而是每次跑一次迷宫布线，退火会怎样？

**参考答案**：单次移动评估将从纳秒级飙升到毫秒级，百万次迭代将无法在合理时间完成。这正是 4.1 节三种模型都预计算成查找表的根本原因——布局阶段宁可接受「近似但 O(1)」的延迟，把精确延迟留给布线（u6）和时序分析（u7）。

**练习 2**：压缩网格的 `compressed_to_grid_x` 为何要按层（layer）再分一层？

**参考答案**：VPR 支持多层（3D/2.5D）器件，不同层上同一类块的分布可能不同（某层有 DSP、另一层没有）。按层分别压缩，才能在指定层上正确找到合法位置；层间互连延迟差异也由延迟模型的目标层维度体现（见 simple_delay_model.h 的 5 维设计）。

---

### 4.3 事务与回滚：apply → commit / revert

#### 4.3.1 概念说明

退火里大量移动会被拒绝（按 \(\exp(-\Delta C/T)\) 概率接受，温度低时大多数变差移动被拒）。被拒的移动必须把布局**完全恢复**到移动前，否则状态会被慢慢污染。

VPR 用一个轻量「事务（transaction）」机制实现「先试算、再决定」：

- **记录（record）**：移动生成器把「块从 old_loc 搬到 new_loc」记进 `t_pl_blocks_to_be_moved`，但**不立刻改全局**。
- **应用（apply）**：把块的新位置写进**正向表** `block_locs_`（块→位置），以便评估代价；但**反向表** `grid_blocks_`（位置→块）暂不动。
- **判定**：用 apply 后的状态算 ΔC，由 `assess_swap_` 决定接受/拒绝。
- **提交（commit）**：若接受，更新反向表 `grid_blocks_`，并把 `proposed_*` 延迟/代价转正。
- **回滚（revert）**：若拒绝，用记录里的 `old_loc` 把正向表改回去，并丢弃 `proposed_*`。

这套机制能廉价回滚，靠两个设计：**(1) 双缓冲**（已提交值 vs 提议值），回滚只需丢弃提议缓冲；**(2) 正向表先改、反向表延后改**，反向表在试算期间不被触碰，回滚时只需恢复正向表。

#### 4.3.2 核心流程

完整时序（`annealer.cpp` 内层一次 `try_swap`）：

```text
move_generator.propose_move(blocks_affected_)      # record_block_move 记录 (blk, old_loc, new_loc)
   │                                                 # 并把受影响引脚塞进 affected_pins
blk_loc_registry.apply_move_blocks(blocks_affected_)  # 正向表: block_locs_[blk].loc = new_loc
   │                                                 # 状态机: APPLY → COMMIT_REVERT
find_affected_nets_and_update_costs(...)            # 算 ΔC，结果写 proposed_*
delta_c = 加权归一化
move_outcome = assess_swap_(delta_c, T)
   │
   ├── ACCEPTED:
   │     costs_ += delta_c (各项)                      # 累加总代价
   │     commit_td_cost(blocks_affected_)            # proposed_* → connection_delay/cost
   │     net_cost_handler.update_move_nets()          # bb 转正
   │     blk_loc_registry.commit_move_blocks(...)     # 反向表 grid_blocks_ 更新
   │     (NoC/interposer 各自 commit)
   │     状态机: COMMIT_REVERT → APPLY
   │
   └── REJECTED:
         net_cost_handler.reset_move_nets()           # 丢弃 bb 提议
         blk_loc_registry.revert_move_blocks(...)     # 正向表: block_locs_[blk].loc = old_loc
         revert_td_cost(blocks_affected_)             # 丢弃 proposed_* (置 INVALID_DELAY)
         (SLACK 算法还要重算 connection_delay)
         状态机: COMMIT_REVERT → APPLY

clear_move_blocks()                                  # 清空记录，准备下一次移动
```

`BlkLocRegistry` 内部用一个 `expected_transaction_` 状态机（`APPLY ↔ COMMIT_REVERT`）以 debug 断言保证「apply 之后必须 commit 或 revert 之一，不能重复或遗漏」。

#### 4.3.3 源码精读

**(a) 移动记录：`t_pl_blocks_to_be_moved`。** 这是整个事务的「账本」：

[vpr/src/place/move_transactions.h:20-29](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_transactions.h#L20-L29) — `t_pl_moved_block`：`{block_num, old_loc, new_loc}`，正是回滚所需的最小信息。

[vpr/src/place/move_transactions.h:56-95](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec62818c9db312e24967de9d6283410ab19/vpr/src/place/move_transactions.h#L56-L95) — `t_pl_blocks_to_be_moved` 聚合：`moved_blocks`（被搬动的块）、`moved_from/moved_to`（被腾空/占据的位置集合）、`affected_pins`（受影响引脚，用于增量时序）。注释特别说明采用「数组的结构」（array of structs）而非「结构的数组」以利于缓存。

`record_block_move` 在记录时做合法性校验，遇冲突直接 ABORT 这次移动：

[vpr/src/place/move_transactions.cpp:19-46](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec62818c9db312e24967de9d6283410ab19/vpr/src/place/move_transactions.cpp#L19-L46) — 若 `to` 已被本移动占用（重复目标）或 `from` 重复，记录原因到 `MoveAbortionLogger` 并返回 `ABORT`；否则把 `(blk, old_loc, new_loc)` 追加进 `moved_blocks`。

`clear_move_blocks` 清场（注意它只 `resize(0)` 不释放容量，注释说明为避免反复分配）：

[vpr/src/place/move_transactions.cpp:70-81](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/move_transactions.cpp#L70-L81)。

**(b) 位置表的事务三段式。** `BlkLocRegistry` 维护正向表 `block_locs_`（块→位置）与反向表 `grid_blocks_`（位置→块）。关键设计：**评估期只改正向表，反向表延后**。

[vpr/src/base/blk_loc_registry.cpp:219-247](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/blk_loc_registry.cpp#L219-L247) — `apply_move_blocks`：把 `block_locs_[blk].loc = new_loc`（仅正向表），若新旧位置瓦片类型不同还要同步物理引脚；断言 `expected_transaction_ == APPLY`，随后置为 `COMMIT_REVERT`。

[vpr/src/base/blk_loc_registry.cpp:249-270](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/blk_loc_registry.cpp#L249-L270) — `commit_move_blocks`：此时才更新反向表 `grid_blocks_`（旧位置置 INVALID、新位置置 blk），断言 `expected_transaction_ == COMMIT_REVERT`。

[vpr/src/base/blk_loc_registry.cpp:272-302](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/blk_loc_registry.cpp#L272-L302) — `revert_move_blocks`：用 `old_loc` 把正向表改回去（同样处理瓦片类型变化的引脚同步），并安全断言「反向表从未被这次移动改过」。

**(c) 时序代价的双缓冲提交/回滚。** `PlacerTimingContext` 同时持有「已提交」与「提议」两套缓冲：

[vpr/src/place/placer_state.h:42-88](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/placer_state.h#L42-L88) — `connection_delay`（已提交）vs `proposed_connection_delay`（提议，未影响连接处为 `INVALID_DELAY`）；`connection_timing_cost` vs `proposed_connection_timing_cost`。`commit_td_cost/revert_td_cost` 的声明也在此。

提交：把受影响引脚的提议值拷进已提交值，再把提议位置 `INVALID_DELAY`：

[vpr/src/place/placer_state.cpp:39-54](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/placer_state.cpp#L39-L54) — 只遍历 `blocks_affected.affected_pins`，把 `proposed_*[net][ipin]` 赋给 `connection_*[net][ipin]`，随后置 `INVALID_DELAY`。

回滚：仅把提议位置 `INVALID_DELAY`（已提交值从未被改，天然无需恢复）：

[vpr/src/place/placer_state.cpp:56-72](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/placer_state.cpp#L56-L72)。

**(d) 退火主循环把三段式串起来。** 接受分支（提交）：

[vpr/src/place/annealer.cpp:705-753](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/annealer.cpp#L705-L753) — `costs_ += delta_c` 各项 → `commit_td_cost` → `update_move_nets`（bb 转正）→ `commit_move_blocks`（反向表）→ 各可选代价（interposer/NoC）commit。

拒绝分支（回滚）：

[vpr/src/place/annealer.cpp:755-793](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/place/annealer.cpp#L755-L793) — `reset_move_nets` → `revert_move_blocks`（正向表复原）→ `revert_td_cost`（丢弃提议）；SLACK 算法因评估期已改了时序，还需 `comp_td_connection_delays` 重算。

注意 588-597 行的注释点明了设计意图：「先把块挪到新位置算代价，接受才更新反向查找表，拒绝就退回原位」。

#### 4.3.4 代码实践

**实践目标**：用一个**思想实验 + 日志验证**说明 `move_transactions` 如何在一次移动中提交或回滚代价，并体会双缓冲的省力之处。

**操作步骤**：

1. 在 `placer_state.cpp:39` 的 `commit_td_cost` 入口与 `:56` 的 `revert_td_cost` 入口各加一行 `VTR_LOG(...)`，打印 `affected_pins.size()` 与是 commit 还是 revert。
2. 编译后跑一次小规模布局（可用 `run_vtr_flow.py` 加 `--place_effort 0` 或小电路缩短时间）。
3. 统计日志里 commit 次数与 revert 次数的比例，对照退火温度曲线（高温时接受多、低温时拒绝多）。
4. （思想实验）假设没有双缓冲、`apply` 直接改 `connection_delay`，问：回滚时需要什么额外信息？答案应是「需要保存旧 delay」，这正是双缓冲要避免的——旧值从未被覆盖，无需保存。

**需要观察的现象 / 预期结果**（待本地验证）：

- `commit_td_cost` 与 `revert_td_cost` 的调用次数之和 ≈ 有效移动次数（不含 ABORTED）。
- 随退火温度下降，revert 占比上升。
- `affected_pins.size()` 每次都很小（印证增量性）。

> 纯阅读型替代：对照 annealer.cpp 的 ACCEPTED/REJECTED 两个分支，逐行列出「提交时改了哪些表、回滚时改了哪些表」，验证「反向表只在提交时改、已提交延迟从不被提议覆盖」两条不变式。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `apply_move_blocks` 只更新正向表 `block_locs_`，而把反向表 `grid_blocks_` 的更新推迟到 `commit_move_blocks`？

**参考答案**：评估一次移动需要知道块的新位置（读正向表算 ΔC），但此时还不知道移动是否会被接受。若同时改了反向表，一旦被拒就要把反向表也回滚，增加额外记账。推迟到「确认接受」后才改反向表，回滚路径就只需恢复正向表——更简单、更不易错。源码注释（blk_loc_registry.h:137-151）明确：「commit/revert 必须在 apply 之后二选一，且反向表在评估期不可用」。

**练习 2**：`commit_td_cost` 里为何要把 `proposed_connection_delay[net][ipin]` 在拷贝后置为 `INVALID_DELAY`？

**参考答案**：这是双缓冲的清理动作，保证「提议缓冲只对本次移动受影响的连接有意义，其余一律是 `INVALID_DELAY`」。这样下一轮评估时，`find_affected_nets_and_update_costs` 不会读到上一轮残留的脏提议值；也使安全断言（如 `comp_td_connection_cost` 的健全性检查）能识别「未重算的提议值」。

**练习 3**：SLACK 算法（`SLACK_TIMING_PLACE`）被拒时为什么比 CRITICALITY 算法多做了 `comp_td_connection_delays` 重算？

**参考答案**：SLACK 算法在评估期就跑了**最新**的时序分析（更新了 setup slack），把 `connection_delay/cost` 直接动过了（见 annealer.cpp:654 的 `commit_td_cost` 在评估阶段就调用）。因此被拒时不能像 CRITICALITY 算法那样仅丢弃提议缓冲，而必须用延迟模型重算受影响连接的延迟与代价、再恢复时序，才能把状态完全退回。源码 annealer.cpp:768-786 注释标注了这一点（甚至留了 `//TODO: make this process incremental`）。

---

## 5. 综合实践

**任务：用三种延迟模型跑同一设计，画出「建表耗时—布局质量」对比，并解释事务机制如何让这成为可能。**

建议步骤：

1. 选一个含**直接连接**（如进位链 `<direct>`）的架构（可参考 `vtr_flow/arch` 下的架构文件，或文档 `doc/src/arch` 里的例子），配合一个会用到进位链的电路。
2. 分别用 `--place_delay_model simple`、`--place_delay_model delta`、`--place_delay_model delta_override` 跑 `run_vtr_flow.py`，记录：
   - 工厂计时 `Computing placement delta delay look-up` 与（delta 类的）`Computing delta delays`；
   - 最终 `Place cost` 与布线后关键路径延迟（时序报告）；
   - 布局耗时。
3. 用 4.3.4 的日志法统计该次运行里 commit/revert 次数与平均 `affected_pins.size()`，估算「平均一次移动重算的连接数」。
4. 写一段分析：把「三种模型的建表开销差异」与「它们提供 O(1) 延迟查询从而支撑百万次增量评估」联系起来；并说明若没有 apply/commit/revert 事务，退火在高温阶段（高拒绝率）会如何被回滚成本拖垮。

**预期结论**（待本地验证）：`simple` 建表最快但延迟近似；`delta` 建表较慢但延迟实测更准；`delta_override` 在含直接连接的架构上对进位链类路径估计最准，往往换来更好的时序。三者查询都是 O(1)，因而退火本身的耗时差异主要来自建表（一次性）而非每次移动。事务机制则保证了高拒绝率下的正确性与低开销。

> 说明：本实践涉及完整构建与运行 VPR，结果取决于本地机器与所选架构/电路，关键数字标注为「待本地验证」。若仅做源码阅读，可重点完成第 3、4 步的分析部分。

## 6. 本讲小结

- 布局延迟模型把「两点间延迟」预计算成 O(1) 查找表，是退火能跑数百万步的前提。`simple`（前瞻、5 维、区分瓦片类型、建表便宜）、`delta`（真实布线采样、4 维、不区分类型、值更准）、`delta_override`（delta 底表 + 直接连接逐引脚类覆盖、最准）三者构成精度/开销的递进权衡。
- 当未指定 `--place_delay_model` 且路由前瞻非 MAP 时，VPR 自动把 `simple` 推断为 `delta`——因为 simple 依赖 MAP 前瞻填表。
- 增量代价 = 只重算受影响网/连接。其成立靠三件事：延迟模型 O(1) 查表、压缩网格 O(1) 找合法位置、`move_transactions` 精确圈定受影响集合。
- `compressed_grid` 按块类型压缩坐标，让稀疏块类型（DSP/RAM）的移动生成与邻居查找都高效。
- 事务机制采用「apply（改正向表）→ 评估 → commit（改反向表 + 转正提议缓冲）| revert（恢复正向表 + 丢弃提议缓冲）」三段式，靠正向/反向表分离与时序代价双缓冲实现廉价回滚。
- `expected_transaction_` 状态机与各类安全断言（如「反向表评估期不可用」「提议缓冲未重算处为 INVALID_DELAY」）在 debug 构建下守护这些不变式。

## 7. 下一步学习建议

- **进入布线（u6 单元）**：本讲的延迟模型值最终要用真实布线验证。建议先读 u6-l1（RR Graph）与 u6-l4（Router Lookahead）——后者正是 `simple` 延迟模型与 delta 采样所依赖的「代价地图」来源，理解 lookahead 后回头看本讲 4.1 会豁然开朗。
- **时序分析（u7 单元）**：本讲的延迟只是布局用的快速估计；布线后的真实时序由 Tatum 计算（u7-l1/l2）。对比「布局延迟模型」与「RoutingDelayCalculator」能加深对「不同阶段需要不同精度延迟」的理解。
- **源码延伸阅读**：若对增量时序更新感兴趣，可读 `vpr/src/place/timing/place_timing_update.cpp`（`commit_setup_slacks` 等）与 `PlacerTimingCosts`，看 SLACK 算法如何把 setup slack 也纳入事务。
- **动手实验**：仿照 4.1.4 与第 5 节，尝试新增一种「只看曼哈顿距离 + 固定速度」的最简延迟模型（实现 `PlaceDelayModel` 接口即可），对比它与 `delta` 在规整架构上的布局质量差异，体会「延迟估计精度」对布局结果的影响。
