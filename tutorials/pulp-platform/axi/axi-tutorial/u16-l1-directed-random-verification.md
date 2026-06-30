# 定向随机验证方法学

## 1. 本讲目标

本讲是专家层「验证方法学」的开篇。读完本讲，你应当能够：

- 说清楚什么是 **directed random verification（定向随机验证）**，以及它相对纯定向用例在覆盖率和工时上的优势；
- 把 `test/tb_axi_xbar.sv` 这台「验证机器」拆成四类并发块（时钟复位、随机激励、自检器、停止控制），并解释每一块的工作；
- 理解 `axi_test` 包里 `axi_rand_master`/`axi_rand_slave` 的随机约束（地址区间、4 KiB 页、在途计数、ID 合法化）是怎么保证激励合法的；
- 看懂本库专用的 `axi_xbar_monitor` 如何用 FIFO/ID 队列旁路建模「事务应该去哪个端口」，从而实现自检；
- 用 `scripts/run_vsim.sh` 的种子机制（`SEEDS`、`-sv_seed`、`--random-seed`）对同一个测试台跑多种子回归，并说明随机种子的作用。

> 本讲承接 u3-l2（随机主从、scoreboard、sim_mem）与 u6-l1（xbar 架构），不再重复协议与 xbar 内部细节，而是聚焦「怎么验证」。

## 2. 前置知识

阅读本讲前，请确保你已经掌握以下概念（前序讲义已建立）：

- **AXI4+ATOP 五通道与握手**：AW/W/B/AR/R，`valid && ready` 同高才算一次握手（u1-l3）。
- **在途（in flight / outstanding）**：地址拍已握手而响应未握手的事务；这是本讲随机主端做并发反压的对象（u1-l3、u3-l2）。
- **xbar 的路由本质**：xbar 不改数据，只按地址把事务路由到正确的 master 端口，并在响应方向按 ID 路由回去；master 端口 ID 比 slave 端口宽 ⌈log₂(NoSlvPorts)⌉ 位（u6-l1）。
- **rand_master / scoreboard / sim_mem 通用自检拓扑**：`rand_master → DUT → axi_sim_mem`，旁路挂 `axi_scoreboard` 比对数据（u3-l2）。

本讲要回答一个关键问题：xbar 是「组合爆炸」型模块——端口数、ID 组合、突发长度、保序与 ATOP 交错——手写定向用例根本写不全。**库怎么验证它？**

答案就是标题里的 **directed random verification**：用受约束的随机激励在有限时间内打满大量场景，再用一个旁路监听的「黄金模型」自动比对结果。下面逐块拆解它的实现。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲解读重点 |
| --- | --- | --- |
| `test/tb_axi_xbar.sv` | xbar 的测试台本体（DUT 例化 + 激励 + 监听 + 停止） | 参数化、拓扑、四类并发块、`end_of_sim` 协调 |
| `test/tb_axi_xbar_pkg.sv` | 专用检查器 `axi_xbar_monitor` 类 | 用 FIFO/ID 队列建模「期望路由」，自检与统计 |
| `src/axi_test.sv` | 随机激励类 `axi_rand_master`/`axi_rand_slave` 及通用 `axi_scoreboard` | 随机约束、`legalize_id`、并发反压 |
| `scripts/run_vsim.sh` | 仿真回归脚本 | `SEEDS` 数组、`call_vsim`、`-sv_seed`、参数矩阵 |
| `Makefile` | 顶层入口 | `sim-%.log` 目标如何把 `--random-seed` 传给脚本 |

## 4. 核心概念与源码讲解

### 4.1 定向随机验证：思想与为什么是它

#### 4.1.1 概念说明

硬件验证里有两种极端：

- **定向验证（directed）**：人手写每一条用例，精确指定「地址 X、ID Y、突发长度 Z，期望响应 W」。优点是可读、好调试；缺点是**写不完**——一个 6 主 8 从的 xbar，光是 ID 交错、跨端口保序、ATOP 与普通写并存的组合就指数级膨胀。
- **纯随机（pure random）**：完全随机发激励。覆盖广，但**多数激励非法**（比如突发跨越 4 KiB 页、同 ID 乱序），会触发大量「假错误」，反而淹没真 bug。

**定向随机验证（directed random）** 是两者的折中，也是本库（以及业界 UVM）采用的方法：

- **随机**：地址、ID、突发类型/长度、停顿拍数都由 `std::randomize` 随机生成，覆盖人工想不到的 corner case；
- **受约束（constrained / directed）**：在随机的基础上加约束，保证每一条激励都**合法**——地址落在有效区间、突发不跨 4 KiB 页、同 ID 同方向保序、并发数不超上限。

一句话：**让随机去探索，让约束去兜底合法性，让自检器去判对错**。

#### 4.1.2 核心流程

一个定向随机验证台由四个并发部分组成，它们在同一时钟下同时运行：

```text
                 ┌───────────────────────────┐
   时钟/复位 ───▶│  clk_rst_gen              │
                 └───────────────────────────┘
                 ┌───────────────────────────┐
   随机激励 ───▶│  axi_rand_master × N      │──▶  打满 DUT（饱和）
                 │  axi_rand_slave  × M      │◀──  随机响应 + 反压
                 └───────────────────────────┘
                              │ 旁路监听（不驱动）
                 ┌───────────────────────────┐
   自检器   ───▶│  monitor（黄金模型）       │──▶  期望 vs 实际 → Failed 计数
                 └───────────────────────────┘
                 ┌───────────────────────────┘
   停止控制 ───▶│  end_of_sim == '1 → $stop │
                 └───────────────────────────┘
```

执行顺序：

1. 复位释放后，每个 master 调 `run(n_reads, n_writes)`，并发发起固定数量的读写；
2. 每完成一个 master 的事务量，置位对应的 `end_of_sim[i]`；
3. 监听器每拍比对路由是否正确，累计 `tests_failed`；
4. 当 `end_of_sim` 全 1，监听器打印统计、`$stop` 结束仿真；
5. 脚本层用日志里的 `Errors: 0,` 判通过。

#### 4.1.3 关键设计取舍：为什么 xbar 用 monitor 而不是 scoreboard

u3-l2 讲过的通用自检闭环是 `rand_master → DUT → axi_sim_mem + axi_scoreboard`：sim_mem 忠实存数据，scoreboard 比对**数据值**。

但 `tb_axi_xbar` **没有**用这套。原因很本质：**xbar 不碰数据，只路由**。`axi_rand_slave` 返回的是任意随机数据，比对数据值毫无意义；真正要验证的是「事务有没有被送到正确的端口、ID 有没有被正确路由回来、保序有没有被破坏」。

所以本库为 xbar 专门写了一个 `axi_xbar_monitor`（在 `tb_axi_xbar_pkg.sv`），它比对的是**路由契约**（端口、ID、addr、len、last 标志），而不是数据值。这是同一思想（旁路监听 + 期望模型比对）的另一种实例——**验证什么就建模什么**。后续 4.4 会精读它。

### 4.2 tb_axi_xbar：一台完整的验证机器

#### 4.2.1 参数化：一切皆可覆盖

测试台本身是个带大量 `parameter` 的模块，事务量、拓扑规模、特性开关全可由命令行 `-g` 覆盖：

[test/tb_axi_xbar.sv:27-53](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_xbar.sv#L27-L53) — 定义了 `TbNumMasters`、`TbNumSlaves`、`TbNumWrites`、`TbNumReads`、`TbAxiIdWidthMasters`、`TbAxiIdUsed`、`TbEnAtop`、`TbEnExcl`、`TbUniqueIds` 等参数。这正是「参数化事务量与配置」的入口：同一份 TB 源码，命令行传不同 `-g` 即覆盖不同规模与特性。

其中 `TbAxiIdUsed`（默认 3）≤ `TbAxiIdWidthMasters`（默认 5）这一约束值得注意：xbar 实际「使用」的 ID 位数可以小于物理 ID 位宽，用来制造「稀疏 ID」场景，测试 `AxiIdUsedSlvPorts` 折中（见 u6-l3）下的误冲突行为。

#### 4.2.2 自动派生的 xbar 配置

TB 把上面的参数自动组装成 `axi_pkg::xbar_cfg_t`，这是 u2-l2 讲过的「按字段名赋值」配置结构体：

[test/tb_axi_xbar.sv:66-80](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_xbar.sv#L66-L80) — 注意 `LatencyMode: axi_pkg::CUT_ALL_AX`（文档推荐配置）、`FallThrough: 1'b0`、`AxiIdWidthSlvPorts: TbAxiIdWidthMasters`，以及 `UniqueIds` 直接透传 TB 参数。slave 端口 ID 宽度等于 master 端 ID 宽度（因为 TB 里「master」连的是 xbar 的 slave 端口）。

#### 4.2.3 地址映射自动生成

每个下游 slave 端口分到一段等长地址区间，由一个函数自动生成规则数组：

[test/tb_axi_xbar.sv:106-117](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_xbar.sv#L106-L117) — `addr_map_gen()` 用 `for` 循环给第 `i` 个端口划出 `[i*0x2000, (i+1)*0x2000)` 的前闭后开区间（u6-l2 讲过前闭后开与高位优先规则）。把映射写成函数而非硬编码，是为了让 `NoAddrRules` 随参数缩放——改 `TbNumSlaves` 映射自动跟着变。

#### 4.2.4 随机主从的例化与 run

每个 master 端例化一个 `axi_rand_master`，复位后调用 `run(TbNumReads, TbNumWrites)`：

[test/tb_axi_xbar.sv:216-229](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_xbar.sv#L216-L229) — 三步：`new(master_dv[i])` 绑定虚接口、`add_memory_region(...)` 限定随机地址落在整个映射区间内（约束！）、`reset()` 后 `run(n_reads, n_writes)`。注意 `add_memory_region` 把区间标成 `DEVICE_NONBUFFERABLE`——这会影响 master 生成的 `cache` 属性位（u2-l1 的 `get_awcache`/`get_arcache`）。完成后置 `end_of_sim[i] <= 1'b1`。

从端则例化 `axi_rand_slave`，提供随机响应与随机反压：

[test/tb_axi_xbar.sv:231-239](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_xbar.sv#L231-L239) — slave 的 `run()` 无参，意味着它「永远运行」直到仿真被 master 的 `end_of_sim` 触发 `$stop` 停下。slave 不限定事务量，只负责忠实响应并随机插入停顿制造背压。

#### 4.2.5 监听器的挂载与停止协调

监听器在独立的 `initial` 块里启动，用 `#0` 延迟一拍让虚接口绑定先完成：

[test/tb_axi_xbar.sv:266-296](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_xbar.sv#L266-L296) — `fork` 里跑两个并发进程：`monitor.run()` 持续每拍比对；另一个 `do...while` 进程每拍检查 `end_of_sim == '1`，一旦全 1 就 `monitor.print_result()` 并 `$stop()`。这就是「停止控制」块——仿真长度由激励发生器决定，不由固定时长决定。

#### 4.2.6 时序三参数

[test/tb_axi_xbar.sv:56-58](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_xbar.sv#L56-L58) — `CyclTime=10ns`、`ApplTime=2ns`、`TestTime=8ns`，满足 u3-l3 讲过的 `0 < TA < TT < T_clk`：TA（施加激励）和 TT（采样）都落在时钟沿之后，留出 setup 余量。这套时序被透传进 `axi_rand_master`/`axi_rand_slave` 和 monitor。

### 4.3 axi_rand_master / axi_rand_slave：随机激励与约束

随机激励的合法性由 `axi_test` 包里的类保证。这里聚焦三道「约束闸门」，它们正是「directed」那一半的含义。

#### 4.3.1 约束闸门一：地址区间

[src/axi_test.sv:811](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L811) — `add_memory_region(addr_begin, addr_end, mem_type)` 把允许访问的地址区间登记进 master。随机生成的地址会被钳制在这些区间内，避免发到完全无意义的地方。`new_rand_burst` 还会进一步保证**单个突发不跨越 4 KiB 对齐边界**（AXI 规范要求），越界则重抽。

#### 4.3.2 约束闸门二：在途计数反压

并发不是无限的。master 用两个全局计数器 `tot_r_flight_cnt`/`tot_w_flight_cnt` 限制同时在途的读/写事务数：

[src/axi_test.sv:1174-1182](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1174-L1182) — 发 AR 前先 `while (tot_r_flight_cnt >= MAX_READ_TXNS) rand_wait(...)`，即在途读达到上限（TB 里设为 20）就**软件层面**主动等待，而不是硬发出去被下游反压。这是「软件反压」：从源头节流，保证 outstanding 不失控。写方向在 `create_aws` 里有对称的 `MAX_WRITE_TXNS` 检查（[src/axi_test.sv:1224-1227](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1224-L1227)）。

#### 4.3.3 约束闸门三：ID 合法化

这是最精妙的一道闸门。AXI 要求**同 ID 同方向事务保序**，但纯随机生成的 ID 可能违反在途唯一性约束（尤其涉及 ATOP 时）。`legalize_id` 在每笔事务发出前循环检查并重抽，直到 ID 合法：

[src/axi_test.sv:1127-1166](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1127-L1166) — 取信号量（防并发竞争计数器）→ `id_is_legal` 判定 → 不合法就释放信号量、随机等一拍、重新 `$randomize(id)`（独占访问的 `ax_lock` 事务除外，其 ID 不可改）→ 合法后登记在途、置 ATOP 标志。关键：合法化不是「丢弃非法事务」，而是「换一个合法 ID 重试」，所以事务总量不丢、只是 ID 被调整。

#### 4.3.4 run：六路并发

`run(n_reads, n_writes)` 把读、写两条链路各自拆成「创建—发送—接收」三段，全部 `fork...join` 并发：

[src/axi_test.sv:1298-1317](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1298-L1317) — 读链路 `send_ars`/`recv_rs`、写链路 `create_aws`/`send_aws`/`send_ws`/`recv_bs`，六个进程同时跑。`ar_done`/`aw_done` 两个标志协调「发送完毕」与「等待所有响应回收」——`recv_rs` 要等到所有在途读清零、且没有待发的 ATOP 读响应后才退出。这种结构让单个 master 内部就实现了读写并发与 outstanding 管理。

> 小结：`axi_rand_master` 的「directed」体现在三道闸门（地址区间、在途上限、ID 合法化），「random」体现在闸门之内的随机选择。`axi_rand_slave`（[src/axi_test.sv:1321](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1321) 起）则是镜像：随机响应、随机反压，无事务量约束。

### 4.4 axi_xbar_monitor：路由自检黄金模型

这是本讲最值得精读的部分——它展示了「旁路监听 + 期望模型」如何具体落地。

#### 4.4.1 监听契约

monitor 持有两组虚接口数组，分别监听所有 master 端口和 slave 端口，**只读不写**：

[test/tb_axi_xbar_pkg.sv:73-84](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_xbar_pkg.sv#L73-L84) — `masters_axi[NoMasters]` 与 `slaves_axi[NoSlaves]`，对应 TB 里通过 `assign` 逐根搬运出来的 `master_monitor_dv`/`slave_monitor_dv`（见 [test/tb_axi_xbar.sv:401-494](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_xbar.sv#L401-L494)）。注意它监听的是 **DUT 两侧**，因此能同时看到「master 发了什么」和「slave 收到了什么」，两边对照即可判路由对错。

#### 4.4.2 期望队列：建模路由

monitor 的核心数据结构是一组 FIFO 与「按 ID 索引的随机队列」`rand_id_queue`：

[test/tb_axi_xbar_pkg.sv:86-96](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_xbar_pkg.sv#L86-L96) — 写方向用 `exp_aw_queue[NoSlaves]`（每下游一个）、`exp_w_fifo`、`exp_b_queue[NoMasters]`；读方向用 `exp_ar_queue[NoSlaves]`、`exp_r_queue[NoMasters]`。`rand_id_queue` 的妙处在于：它能按 ID 乱序 `pop_id(id)`，因此即便响应被 xbar 重排（同 ID 内仍保序，不同 ID 可乱序），monitor 也能正确匹配。

#### 4.4.3 监听 AW：押入期望

以写地址通道为例，看 monitor 如何在 master 端口捕获一笔 AW 并押入下游的期望队列：

[test/tb_axi_xbar_pkg.sv:163-204](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_xbar_pkg.sv#L163-L204) — 关键步骤：

1. 检测 `aw_valid && aw_ready`（一次真实握手）；
2. 用与 xbar 相同的规则遍历 `AddrMap`，算出这笔 AW **应该去**哪个 slave（`to_slave_idx`），未命中则标记 `decerr`；
3. 构造期望 ID：`exp_aw_id = {idx_mst_t'(i), aw_id}`——**把 master 端口号拼进 ID 高位**，这正是 xbar 内部 `axi_mux` 用 `axi_id_prepend` 做的事（u5-l3）。monitor 用同样的拼法，从而能在 slave 侧核对扩展后的 ID；
4. 把期望 `{id, addr, len}` 押进 `exp_aw_queue[to_slave_idx]`，并累计 `tests_expected`；
5. 无条件押一笔期望 B；若 `aw_atop[5]`（即 `ATOP_R_RESP`，u15-l1）置位，再按 `len+1` 押若干期望 R 拍。

#### 4.4.4 核对 slave 侧：pop 并比对

当 slave 端口真的收到一笔 AW 时，monitor 从期望队列按 ID 弹出并逐字段比对：

[test/tb_axi_xbar_pkg.sv:216-235](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_xbar_pkg.sv#L216-L235) — 比对 `slv_axi_id`、`slv_axi_addr`、`slv_axi_len` 三字段，任一不符即 `incr_failed_tests(1)` 并 `$warning`。注意它**不比对数据**——再次印证 4.1.3 的结论：xbar 验的是路由不是数据。

W 通道的 `last` 标志比对更讲究：因为 AXI 允许「W 拍先于 AW 到达 slave」，monitor 把期望 W 和实际 W 分别入两个 FIFO，再在 `check_slv_w` 里**对位弹出**比对 `last`（[test/tb_axi_xbar_pkg.sv:261-275](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_xbar_pkg.sv#L261-L275)），规避了时序敏感的误报。

#### 4.4.5 每拍的调度：先 push 后 pop

`run()` 任务把所有监听任务按「先押入、后弹出」的顺序调度，避免同拍内 pop 到还没押入的期望：

[test/tb_axi_xbar_pkg.sv:430-488](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_xbar_pkg.sv#L430-L488) — 每个 `cycle_start`（即 `#TestTime` 采样点）后，先跑 push 类任务（`monitor_mst_aw/ar`），再跑既 push 又 pop 的 `monitor_slv_aw/w`，最后跑 pop 类任务（`monitor_mst_b/slave_ar/mst_r`）和 W 比对。这个顺序保证了「同一拍内 master 端押入的期望，slave 端能在同一拍核对」。

#### 4.4.6 统计与判据

仿真结束时（由 TB 的 `$stop` 触发）打印三类计数：

[test/tb_axi_xbar_pkg.sv:490-501](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_xbar_pkg.sv#L490-L501) — `Tests Expected`（期望比对次数）、`Tests Conducted`（实际比对次数）、`Tests Failed`（失败次数）。判据有两条：`tests_failed > 0` 报 `$error`（有错路由）；`tests_conducted == 0` 也报 `$error`（**一个比对都没做，说明激励根本没打起来， equally 是失败**——这条防的是「空跑误判通过」）。三个计数器用 `std::semaphore` 保护，防多进程竞争（[test/tb_axi_xbar_pkg.sv:408-424](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_xbar_pkg.sv#L408-L424)）。

> 这套 `Expected/Conducted/Failed` 三计数 + 信号量保护，是本库所有自检类的通用范式；u3-l2 的 `axi_scoreboard` 也是同构思路，只是它比对的是字节级数据值。

### 4.5 run_vsim.sh：参数矩阵与随机种子回归

#### 4.5.1 种子数组与 call_vsim

脚本用一个 `SEEDS` 数组控制跑哪些种子，`call_vsim` 对每个种子调一次仿真：

[scripts/run_vsim.sh:25-35](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L25-L35) — `SEEDS=(0)` 默认只有种子 0（注释说明：0 永远保留以保持「回归基线一致」）。`call_vsim` 循环每个 seed 执行 `vsim -sv_seed $seed ...`，并用 `grep "Errors: 0," vsim.log` 判该次仿真无错。`-sv_seed` 是 vsim 的开关：它决定 SystemVerilog `$random`/`std::randomize` 的初始随机种子。

**随机种子的作用**：相同种子 → 相同随机序列 → 完全可复现的激励（便于调试复现 bug）；不同种子 → 不同的地址/ID/突发/停顿组合 → 覆盖不同的 corner case。这就是「多种子回归」的价值：同一段 TB 源码，用 N 个种子跑 N 遍，等于免费扩展了 N 倍的覆盖。

#### 4.5.2 --random-seed 开关

要追加一个「真随机」种子（每次运行都不同），用 `--random-seed`：

[scripts/run_vsim.sh:253-264](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L253-L264) — 该开关把字符串 `random` 追加进 `SEEDS`，于是 `call_vsim` 会用 `vsim -sv_seed random`（vsim 会自己生成一个随机种子并打印在日志里）多跑一次。其余位置参数被当作测试名。

#### 4.5.3 tb_axi_xbar 的参数矩阵

`axi_xbar` 在 `case` 里有一个参数矩阵，把端口数与特性开关笛卡尔积展开：

[scripts/run_vsim.sh:178-192](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L178-L192) — 五重循环 `NumMst ∈ {1,6}` × `NumSlv ∈ {1,8}` × `Atop ∈ {0,1}` × `Exclusive ∈ {0,1}` × `UniqueIds ∈ {0,1}` = **32 个配置**，每个配置按 `SEEDS` 跑（默认种子 0）。注意默认**不**给 tb_axi_xbar 追加固定随机种子（对比 `axi_lite_regs` 在 [scripts/run_vsim.sh:147-157](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L147-L157) 里 `SEEDS+=(10 42)`）；要让 xbar 跑随机种子，必须显式传 `--random-seed`。

> ⚠️ **源码冷知识（待你自行核实）**：`run_vsim.sh` 里其实有**两个** `axi_xbar)` 分支（第二个在 [scripts/run_vsim.sh:214-236](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L214-L236)，参数更丰富，含 `DATA_WIDTH ∈ {64,256}`、`PIPE ∈ {0,1}` 等）。但 bash 的 `case` 只执行**第一个**匹配模式，所以第二段是**不可达的冗余代码**，实际回归跑的是 178–192 这一组。读脚本时若发现「怎么参数对不上」，原因就在此。

#### 4.5.4 Makefile 如何一键带上随机种子

最常用的入口是 `make sim-axi_xbar.log`，它自动把测试名和 `--random-seed` 一起传给脚本：

[Makefile:82-86](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Makefile#L82-L86) — 模式规则 `sim-%.log` 中 `$*` 是茎（即 `axi_xbar`），故实际执行 `run_vsim.sh --random-seed axi_xbar`：`--random-seed` 把 `random` 加进 `SEEDS`，`axi_xbar` 作为位置参数限定只跑这一个 TB。因此**一次 `make sim-axi_xbar.log` 会跑 32 配置 × {种子 0, 随机种子} = 64 次仿真**。判据是日志里不含 `Error:`/`Fatal:`。

这就形成了完整的回归闭环：源码（TB）→ 参数矩阵（脚本）→ 种子（`-sv_seed`）→ 日志判据（`Errors: 0,` / `Error:`）。

## 5. 综合实践

**实践目标**：亲手用随机种子跑 `tb_axi_xbar` 回归，观察「同一种子可复现、不同种子覆盖不同」的现象，并读懂日志里的统计与判据。

> 环境说明：本实践需要 Mentor Questa/ModelSim（`vsim`）与 `bender`。若当前环境未安装，则标注为**待本地验证**，但不影响你完成「阅读理解 + 预期分析」部分。

### 步骤 1：先编译（一次性）

```bash
make compile.log
```

这会调用 `scripts/compile_vsim.sh`，用 `bender script vsim -t test -t rtl` 按 Level 0–6 顺序编译全库（u1-l4）。

### 步骤 2：跑一次基线（种子 0，单配置，便于快速观察）

直接调脚本，只跑 tb_axi_xbar、只用默认种子 0：

```bash
cd build && ../scripts/run_vsim.sh axi_xbar 2>&1 | tee ../xbar_seed0.log
```

> 注意：这仍会跑 32 个配置（因为 `axi_xbar` 的 `case` 是参数矩阵）。若想只跑一个最小配置以加速，可临时绕过脚本，直接对编译好的库调 vsim（命令较长，**待本地验证**）。

### 步骤 3：跑三次「带随机种子」回归

利用 Makefile 自动加 `--random-seed` 的特性，连跑三次：

```bash
make sim-axi_xbar.log    # 第 1 次（含种子 0 + 一个随机种子）
make clean && make sim-axi_xbar.log    # 第 2 次
make clean && make sim-axi_xbar.log    # 第 3 次
```

每次结束后，在 `build/vsim.log` 里找两样东西：

1. vsim 启动行里的 `-sv_seed <值>`——记录**随机种子那次**的种子值（三次应不同）；
2. monitor 打印的统计行：

```text
# Tests Expected:  <N1>
# Tests Conducted: <N2>
# Tests Failed:    <N3>
```

以及脚本层的 `Errors: 0,` 行。

### 步骤 4：记录与观察

把三次结果填入下表（**具体数值待本地验证**）：

| 回归 | 随机种子值 | Tests Expected | Tests Conducted | Tests Failed | `Errors:` |
| --- | --- | --- | --- | --- | --- |
| 第 1 次 | ______ | ______ | ______ | ______ | 0? |
| 第 2 次 | ______ | ______ | ______ | ______ | 0? |
| 第 3 次 | ______ | ______ | ______ | ______ | 0? |

**预期现象**：

- 三次的**种子 0 部分**应完全一致（`Tests Expected/Conducted` 相同）——这就是「种子 0 作回归基线」的意义，可复现；
- 三次的**随机种子部分**，`Tests Expected/Conducted` 会**略有不同**——因为随机 master 生成了不同的突发长度与 ID 组合，触发比对次数不同；
- 三次的 `Tests Failed` 都应为 **0**，`Errors:` 都应为 **0**——xbar 功能正确时，任何合法随机激励都不应产生错路由。

### 步骤 5：用一句话回答

> 「随机种子的作用是：__________。所以同一段 TB 用多个种子跑，相当于在不增加手写用例的前提下成倍扩展了覆盖。」

（参考答案：随机种子决定 `$random`/`std::randomize` 的初始状态，不同种子生成不同的地址/ID/突发/停顿序列，从而覆盖不同的协议 corner case；相同种子则保证完全可复现，便于定位偶发 bug。）

## 6. 本讲小结

- **定向随机 = 受约束的随机**：`axi_rand_master` 用「地址区间、在途计数上限、ID 合法化」三道闸门保证激励合法，闸门之内随机探索，兼顾覆盖率与合法性。
- **xbar 的自检不用通用 scoreboard**：因为 xbar 只路由不碰数据，故用专用的 `axi_xbar_monitor` 比对路由契约（端口、ID、addr、len、last），它监听 DUT 两侧、用 `rand_id_queue` 容忍同 ID 保序下的乱序。
- **`Expected/Conducted/Failed` 三计数 + 信号量**是本库自检类的通用范式；`Conducted==0` 同样判失败，防「空跑误通过」。
- **随机种子是覆盖放大器**：`run_vsim.sh` 用 `SEEDS` 数组 + `-sv_seed`，默认保留种子 0 作可复现基线，`--random-seed` 追加真随机；`make sim-<tb>.log` 自动带上 `--random-seed`。
- **回归 = 源码 × 参数矩阵 × 种子 × 日志判据**：`tb_axi_xbar` 的 32 配置 × 多种子，用 `Errors: 0,` 与 `Error:`/`Fatal:` 兜底判通过。
- **读源码要警惕冗余**：`run_vsim.sh` 里第二个 `axi_xbar)` 分支因 bash `case` 语义不可达，实际回归以第一个为准。

## 7. 下一步学习建议

- **u16-l2（总线比较与回归工具）**：本讲的 monitor 是「手写黄金模型」；下一讲介绍更通用的 `axi_bus_compare`/`axi_slave_compare`/`axi_chan_compare`（AB 两路比对）与 `axi_dumper` + `axi_dumper_interpret.py`（事务日志离线分析），是定位本讲 monitor 报出的 `$warning` 的利器。
- **u16-l3（时序、流水线与 EDA 兼容）**：本讲多次提到 `CUT_ALL_AX`、`FallThrough`、内部不插寄存器；下一讲从时序与死锁角度系统解释这些选择的依据。
- **动手延伸**：试着给 `tb_axi_xbar` 临时加一个固定种子（仿照 `axi_lite_regs` 的 `SEEDS+=(10 42)`），观察某个特定种子下 monitor 是否能稳定复现某个边界场景；或减小 `TbNumWrites/TbNumReads` 做「快速冒烟」回归。
- **源码延伸阅读**：对比 `axi_scoreboard`（[src/axi_test.sv:1951](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_test.sv#L1951) 起）与 `axi_xbar_monitor`，体会「验证数据值」与「验证路由」两类黄金模型在设计上的取舍。
