# 总线比较与回归工具

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清「等价性检查（equivalence checking）」在 AXI 验证中的两类典型场景：把**同一激励**同时喂给两个从端比对响应、或把**同一条总线**在变换前后的两段做逐拍比对。
- 区分本库提供的两套工具：**可综合**的 `axi_bus_compare` / `axi_slave_compare`（可在 FPGA 上跑）与**仅仿真**的 `axi_chan_compare`（用 SystemVerilog 动态队列做黄金模型）。
- 读懂它们「请求方向入队、到达方向出队比对」的共同内核，以及可综合版本用「每 ID 一个 FIFO + 两侧 valid 同时有效才比对」来实现同步的原因。
- 用 `axi_dumper` 把一次事务落盘成类 Python 字典日志，并用 `scripts/axi_dumper_interpret.py` 离线重放、校验读写的 AR↔R、AW↔W↔B 配对。

本讲是 U16 验证方法学的一环，承接 u3-l2（随机主从 / scoreboard / sim_mem）与 u16-l1（定向随机验证）。u3-l2 的 scoreboard 是「旁路监听 + 黄金内存模型」；本讲的工具则是「**两条活总线逐拍对拍**」与「**事务落盘离线分析**」，补齐了等价性检查与事后排错两块拼图。

## 2. 前置知识

阅读本讲前，请先具备以下认知（均来自前置讲义）：

- **AXI 五通道与握手**（u1-l3 / u2-l3）：一次握手 = `valid && ready` 同周期成立；请求方向是 AW/W/AR，响应方向是 B/R。
- **req_t / resp_t 结构体**（u2-l4）：req_t 装请求方驱动信号（AW/W/AR 载荷 + 三个 valid、B/R 的 ready），resp_t 装响应方驱动信号；`AXI_ASSIGN_*` / `AXI_SET_*` 宏在接口与结构体之间搬数据。
- **stream 接口原语**（u4-l1 / u7-l1）：本库的比较器大量复用外部依赖 `common_cells` 的两个原语——`stream_fork`（把一路 valid/ready 复制成 N 路同时握手）与 `stream_fifo`（标准 valid/ready FIFO，`T` 参数指定载荷类型）。它们都用「valid/ready」语义，和 AXI 通道天然对齐。
- **定向随机验证与自检**（u3-l2 / u16-l1）：验证组件分「激励」「自检」「停止」三类；通过判据常落在仿真日志的 `Errors: 0,` 行。

下面三个直觉性概念贯穿全讲，先在这里建立：

| 概念 | 含义 |
|---|---|
| **AB 比对（two-way compare）** | 取两条同类型 AXI 链路 A、B，喂相同激励，逐拍检查它们握手出的 beat 是否一致。 |
| **抽头（tap）** | 比较器像电流表一样「串接」在链路里，对正常数据流透明，只是顺手把每个 beat 抄一份去比对。 |
| **逐拍 vs 逐事务** | `*_compare` 系列做**逐 beat** 比对（粒度最细，定位精确）；`axi_dumper` + 脚本做**逐事务**重放（把多个 beat 重新拼回一笔读写，便于人看）。 |

> **命名小提示**：模块叫 `axi_chan_compare`，但它比较的其实是「两路完整 AXI（req+resp）」；`axi_bus_compare` 同理。这里的 chan/bus 都指「一条 AXI 链路」，不要望文生义以为只比「单个通道」。README 把它们分别归在「可综合验证件」与「仅仿真件」两张表里（见第 3 节地图）。

## 3. 本讲源码地图

| 文件 | 角色 | 可综合性 |
|---|---|---|
| [src/axi_chan_compare.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_chan_compare.sv) | 仅仿真的「黄金队列」比对器：用 SV 无界队列 `[$]` 缓存期望 beat，到达即弹即比，失配 `$error` 并打印对照表。 | ❌ 仅仿真 |
| [src/axi_bus_compare.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_bus_compare.sv) | 可综合的逐 ID FIFO 比对器：两条总线各自抽头，按 ID 进独立 `stream_fifo`，两侧同 ID 都有效时组合逻辑比对。 | ✅ 可综合 |
| [src/axi_slave_compare.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_slave_compare.sv) | `axi_bus_compare` 的「一主驱双从」包装：把一个 master 的请求 fork 给 reference / test 两个 slave，比对其响应。 | ✅ 可综合 |
| [src/axi_dumper.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dumper.sv) | 仅仿真的事务落盘器：把每个握手 beat 写成类 Python 字典行；含接口外壳 `axi_dumper_intf`。 | ❌ 仅仿真（需 `+define+TARGET_SIMULATION`） |
| [scripts/axi_dumper_interpret.py](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/axi_dumper_interpret.py) | 离线分析脚本：把 dumper 日志重放成「逐笔读写」，校验 AR↔R、AW↔W↔B 配对与 `last` 标志。 | Python 脚本 |
| [test/tb_axi_slave_compare.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_slave_compare.sv) | `axi_slave_compare` 的范本测试台：master → slave_compare → {参考 `axi_sim_mem`，测试 `axi_multicut(8)` + `axi_sim_mem`}。 | 测试台 |
| [test/tb_axi_bus_compare.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_bus_compare.sv) | `axi_bus_compare` 的范本测试台：在外层手工 fork 出 A/B 两路再送入比较器。 | 测试台 |

README 也明确把前两者归入「verification purposes only but synthesizable」（可上 FPGA），后两者归入「Simulation-Only Modules」。

---

## 4. 核心概念与源码讲解

### 4.1 axi_chan_compare：基于 SV 队列的仿真专用比对器

#### 4.1.1 概念说明

`axi_chan_compare` 是四个工具里**最易读**的一个，适合用来建立「请求方向入队、到达方向出队比对」的直觉。它的定位是：**仅仿真**的「抽头式」等价检查器。你把它像探针一样夹在 master（A 侧）与 slave（B 侧）之间——A 侧发出的请求 beat 进队列，等它到达 B 侧时弹出来逐一比对；B 侧产生的响应 beat 进队列，等它回到 A 侧时再比对。

它**不可综合**，因为它用到了三类仿真专用构造：无界队列 `[$]`、`$error`、`$display`。换来的是表达力：失配时直接 `$error` 让仿真失败，并打印一张「期望 vs 收到」的对照表，定位极快。这跟 u16-l1 里「让仿真自己判对错」的思路一脉相承，只是这里判的不是「数据对不对」而是「两条路是否等价」。

三个参数控制比对粒度：

- `IgnoreId`：比对前把 `id` 字段抹成 `'X`。当下游有 `axi_id_remap` / `axi_mux`（u4-l2、u5-l3）这类会改写 ID 的模块时，两路的 ID 天然不同，必须忽略。
- `AllowReordering`：用「每 ID 一个队列」（共 \(2^{\text{IdWidth}}\) 个）替代单个队列，允许不同 ID 的响应合法乱序；**与 `IgnoreId` 互斥**。
- `IdWidth`：ID 位宽，决定 `AllowReordering` 时的队列数。

#### 4.1.2 核心流程

把它想成一个双向探针，请求向右流、响应向左流：

```
   master (A)                              slave (B)
   ───────────► 请求(AW/W/AR) ─────────────►
     │  入队 clk_a                       出队+比对 clk_b
     ◄────────── 响应(B/R) ◄─────────────
        出队+比对 clk_a                入队 clk_b
```

伪代码：

```
# A 侧每个时钟沿：A 侧握手的请求 beat → push 进期望队列
on clk_a (AW/W/AR handshake at A):  queue.push(beat_A)

# B 侧每个时钟沿：B 侧握手的请求 beat → pop 期望并比对
on clk_b (AW/W/AR handshake at B):  exp = queue.pop(); compare(exp, beat_B)

# B 侧产生、A 侧消费的响应方向对称：B 入队、A 出队比对
on clk_b (B/R handshake at B):      resp_queue.push(resp_B)
on clk_a (B/R handshake at A):      exp = resp_queue.pop(); compare(exp, resp_A)
```

关键性质：**比对发生在「到达侧」**，因此能天然吸收两路的时序差（队列是无界的）。只要两路最终握手出的 beat 序列（按 ID）一致，就静默；一旦某个字段用四态 `!==` 比对失败，立刻 `$error`。

#### 4.1.3 源码精读

模块端口与三个关键参数：[src/axi_chan_compare.sv:L17-L39](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_chan_compare.sv#L17-L39)。注意它有 `clk_a_i` / `clk_b_i` 两个时钟——A、B 两路可以处在不同时钟域（同频异步即可，因为是抽头监听而非数据通路）。

队列声明，`AllowReordering` 决定队列数 \(N = 2^{\text{IdWidth}}\) 或 1：[src/axi_chan_compare.sv:L131-L138](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_chan_compare.sv#L131-L138)。W 通道没有 id，故 `w_queue` 永远是单个队列。

A 侧入队（请求方向）：[src/axi_chan_compare.sv:L141-L153](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_chan_compare.sv#L141-L153)，在 `aw_valid & aw_ready` 等握手成立时把整条 `aw` 结构体压入对应 ID 的队列。

到达侧（B）出队 + 比对，以 AW 为例：[src/axi_chan_compare.sv:L168-L188](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_chan_compare.sv#L168-L188)。核心三步——空队报警、按 ID 弹出期望、`IgnoreId` 时把两侧 id 都置 `'X` 后用 `!==` 四态比对：

```systemverilog
if (axi_b_req.aw_valid & axi_b_res.aw_ready) begin
    automatic aw_chan_t aw_exp, aw_recv;
    ...                                       // 弹出期望 (AllowReordering 按_id_弹出)
    aw_recv = axi_b_req.aw;
    if (IgnoreId) begin aw_exp.id = 'X; aw_recv.id = 'X; end
    if (aw_exp !== aw_recv) begin
        $error("AW mismatch!");
        print_aw(aw_exp, aw_recv);            // 打印逐字段对照表
    end
end
```

响应方向（B 入队、A 出队比对）结构完全对称，见 [src/axi_chan_compare.sv:L223-L264](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_chan_compare.sv#L223-L264)。

`print_aw` 等函数（[src/axi_chan_compare.sv:L41-L62](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_chan_compare.sv#L41-L62)）就是失配时那张「expected | received」对照表的来源，逐字段（id/addr/len/size/burst/.../atop）两列对齐打印。

#### 4.1.4 代码实践

**实践目标**：通过阅读 + 改参数，体会 `IgnoreId` 与 `AllowReordering` 的作用，而不是真去跑一个新 TB（`axi_chan_compare` 没有专属测试台）。

1. 打开 [src/axi_chan_compare.sv:L131-L138](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_chan_compare.sv#L131-L138)，确认 `NumIds` 在 `AllowReordering=1` 时等于 \(2^{\text{IdWidth}}\)，否则为 1。
2. 跟踪一次 AW 比对：从 L168 进入，到 L184 的 `!==` 判定，再到 L186 的 `print_aw`。在脑海里走一遍：若两路 `addr` 不同，会在哪一行触发 `$error`？
3. 假想实验（**待本地验证**）：若你在某 TB 里把 `axi_chan_compare` 接在 `axi_mux` 两侧（上游 ID 2 位、下游 ID 3 位），两路 id 必然不等。请说明应设 `IgnoreId=1` 还是 `AllowReordering=1`，并解释为什么二者不能同时开（提示：`AllowReordering` 靠 id 选队列，`IgnoreId` 把 id 抹掉）。

**预期结果**：能口述「ID 被改写 → 用 `IgnoreId`；ID 不变但响应可能乱序 → 用 `AllowReordering`；两者互斥因为一个要靠 id 分桶、一个要抹掉 id」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `w_queue` 只有一个，而 `aw_queue/b_queue/ar_queue/r_queue` 在 `AllowReordering` 时可以有多个？

> **答**：W 通道在 AXI 协议里**没有 id 字段**，且必须跟随其 AW 的顺序（u5-l1 的「W 突发队列」），因此永远单队列、严格保序即可；其余四个通道都带 id，按 id 分桶才能合法容纳不同 ID 的乱序。

**练习 2**：比对用 `!==` 而非 `!=`，有什么区别？

> **答**：`!==` 是四态比较，`'X`/`'Z` 参与比较；`!=` 是二态语义（`X` 与任意都「相等」）。这里要的是「逐比特严格不同才报警」，故必须用 `!==`，配合 `IgnoreId` 把 id 置 `'X`（与任何值都「相等」）来跳过该字段。

---

### 4.2 axi_bus_compare：可综合的逐 ID FIFO 比对器

#### 4.2.1 概念说明

`axi_chan_compare` 好用但不能上 FPGA。`axi_bus_compare` 把同一个「入队/出队比对」的内核**用可综合元件重写**一遍：用 `stream_fifo`（外部 `common_cells`）替代 SV 无界队列，用组合逻辑 `!=` 替代 `$error`。它的输出不再是「让仿真失败」，而是**一组 mismatch 信号**——`aw_mismatch_o`（按 ID 展开成位向量）、`w_mismatch_o`、…、汇总的 `mismatch_o`、以及 `busy_o`。你需要在测试台（或 FPGA 逻辑）里自己采样这些信号来判断。

它是**抽头式**的：模块本身是个透明直通器（A 有完整的 req in/out + rsp in/out，B 同理），数据照常流向下游，只是每个握手 beat 被额外抄一份进比对 FIFO。所以你可以把它串在任何一段链路上对比「变换前后」。

#### 4.2.2 核心流程

每条总线（A、B）对五个通道各自做三件事：

1. **Fork**：`stream_fork(N_OUP=2)` 把一个 beat 的握手复制成两路——一路继续送往下游（正常数据流），一路送进比对 FIFO。
2. **按 ID 入队**：请求/响应 beat 按其 `id` 选 \(2^{\text{AxiIdWidth}}\) 个 FIFO 中的一个压入（W 通道无 id，用单个 FIFO）。这一步把不同 ID 的事务分流，使比对能容忍合法乱序。
3. **两侧对齐后比对**：某个 ID 的 FIFO 只有在 **A、B 两侧该 ID 的 FIFO 头都 valid** 时才弹出（`ready_i = valid_a & valid_b`），同拍组合逻辑比较两个 `data_o`，不等则点亮该 ID 的 mismatch 位。

同步条件是理解本模块的钥匙：`FifoDepth` 必须 ≥ 两路的最大时序差（skew）。设想 A 路先握手了 5 拍、B 路才慢悠悠跟上来——A 的 FIFO 暂存这 5 拍直到 B 逐拍追平。若 skew 超过 `FifoDepth`，A 路 FIFO 会满并反压上游，改变被测行为，所以深度要留够。

`UseSize` 参数处理**窄传输**：当 `size < $clog2(DataWidth/8)`（一拍不足整总线宽）时，beat 里只有部分字节车道有效，其余车道可能含陈旧数据而「合法不等」。开启 `UseSize` 后只比对 `[lower, lower + 2^size)` 区间内的字节，否则全宽比对。

#### 4.2.3 源码精读

模块端口与参数：[src/axi_bus_compare.sv:L18-L80](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_bus_compare.sv#L18-L80)。`id_t` 由 `AxiIdWidth` 派生（`logic [2**AxiIdWidth-1:0]`），故 `AxiIdWidth` 在测试台里通常很小（范本 TB 用 6 → 每 ID 维度 64）。

载荷直通（透明抽头）——请求/响应 payload 原样从 in 拷到 out：[src/axi_bus_compare.sv:L88-L103](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_bus_compare.sv#L88-L103)。

A 侧 AW 的 fork：一份送下游、一份送比对 FIFO：[src/axi_bus_compare.sv:L167-L176](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_bus_compare.sv#L167-L176)。

按 ID 入队的握手分发——`fifo_valid_aw_a[aw.id]` 只点亮当前 id 那一位：[src/axi_bus_compare.sv:L407-L440](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_bus_compare.sv#L407-L440)。

每个 ID 的 AW FIFO 实例，注意 `ready_i` 是「两侧同 id 都 valid 才弹」：[src/axi_bus_compare.sv:L228-L245](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_bus_compare.sv#L228-L245)：

```systemverilog
stream_fifo #(.T(axi_aw_chan_t)) i_stream_fifo_aw_a (
    .data_i  ( axi_a_req_i.aw                          ),
    .valid_i ( fifo_valid_aw_a[id]                     ),
    .ready_o ( fifo_ready_aw_a[id]                     ),
    .data_o  ( fifo_cmp_data_aw_a[id]                  ),
    .valid_o ( fifo_cmp_valid_aw_a[id]                 ),
    // 关键：A、B 两侧该 id 都 valid 才允许弹出（即「比对并消耗」）
    .ready_i ( fifo_cmp_valid_aw_a[id] & fifo_cmp_valid_aw_b[id] )
);
```

比对逻辑（generate 每个 id），两侧 valid 同高时比较 data，不等则置位：[src/axi_bus_compare.sv:L646-L680](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_bus_compare.sv#L646-L680)。R 通道的 `r_data_mismatch` 在 `UseSize` 时只覆盖活动字节车道（[src/axi_bus_compare.sv:L650-L665](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_bus_compare.sv#L650-L665)）。

汇总输出：`busy_o`（任一比对 FIFO 非空，[src/axi_bus_compare.sv:L714-L718](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_bus_compare.sv#L714-L718)）与 `mismatch_o`（任一通道任一 id 失配，[src/axi_bus_compare.sv:L721-L722](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_bus_compare.sv#L721-L722)）。

#### 4.2.4 代码实践

**实践目标**：跑现成的 `tb_axi_bus_compare`，理解它如何制造时序差来压测比对 FIFO。

1. 编译：`make compile.log`（u1-l4 已讲过，Bender 生成按 Level 排序的编译脚本）。
2. 跑 TB：`make sim-axi_bus_compare.log`（`sim-%.log` 是 Makefile 的模式规则， stem 即 TB 名；该 TB 不在默认 `TBS` 列表里，但模式规则照常匹配）。
3. 等价地，直接调脚本：`./scripts/run_vsim.sh axi_bus_compare`。由于 `run_vsim.sh` 的 `case` 里没有它的专属分支，会落到 `*)` 默认分支 `call_vsim tb_axi_bus_compare ...`（见 [scripts/run_vsim.sh:L244-L246](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L244-L246)）。

**需要观察的现象**：日志里出现 `Errors: 0,`（`call_vsim` 的判据，[scripts/run_vsim.sh:L33](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L33)）。

**TB 做了什么**（[test/tb_axi_bus_compare.sv:L64-L168](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_bus_compare.sv#L64-L168)）：驱动一笔随机写 + 读；A 路直连 `axi_sim_mem`，B 路先过 `axi_multicut(NoCuts=8)`（8 级寄存器，u4-l1）再接另一个 `axi_sim_mem`。这 8 拍延迟就是被 `FifoDepth=16` 的比对 FIFO 吸收的 skew。两侧功能等价（都是 sim_mem），故 `mismatch_o` 全程为 0。

**预期结果（待本地验证）**：仿真通过、`Errors: 0,`。若你把 `FifoDepth` 改成小于 8（例如 4），B 路 FIFO 会因 skew 过大而反压，行为可能变化——这是体会「深度 ≥ skew」的好实验（在你自己的工作副本上改）。

#### 4.2.5 小练习与答案

**练习 1**：为什么比对要「两侧同 id 都 valid 才弹」，而不是「A 侧 valid 就弹、缓存 B 的结果」？

> **答**：因为 A、B 两路是独立握手的，到达时刻不同。若 A 一 valid 就弹，当时 B 对应 beat 可能还没到，没有可比对象。「两侧都 valid」是唯一确定的「同一次逻辑 beat 在两路都已就绪」的时刻，此时比较才有意义；FIFO 正是用来把早到的一方暂存到这一刻。

**练习 2**：`AxiIdWidth=6` 时，单条总线、五个通道各需要多少个 `stream_fifo`？

> **答**：AW/B/AR/R 四个带 id 的通道各 \(2^6=64\) 个，W 通道 1 个（无 id），合计 \(64\times4+1=257\) 个；A、B 两路翻倍。这正是可综合版本「按 id 展开成 FIFO 阵列」的面积代价，也是 `AxiIdWidth` 必须保持小的根本原因。

---

### 4.3 axi_slave_compare：一主驱双从的可综合包装

#### 4.3.1 概念说明

`axi_bus_compare` 比的是「两条独立总线」，需要你在外部自己把激励 fork 成两路（`tb_axi_bus_compare` 就是这么手工搭的）。`axi_slave_compare` 把这套「一主驱双从」的 fork 逻辑**收进模块**：你给它一个 master 端口和两个 slave 端口（reference / test），它自动把请求复制给两个 slave、比对其响应。

最典型的用途是 **DUT vs 参考模型**：reference 接一个已知正确的 `axi_sim_mem`，test 接你新写的存储控制器 / 桥 / 转换器，用同一套激励同时打两边，看响应是否一致。模块头部说得很直白：「参考响应总是回送给 master，测试响应握手后丢弃」。

#### 4.3.2 核心流程

1. **请求 fork**：用三个 `stream_fork` 把 master 的 AW/W/AR 各复制成两路（ref / test），分别送往两个 slave。
2. **响应选取**：master 收到的 B/R 来自 **reference** slave（`AXI_SET_RESP_STRUCT(axi_mst_rsp_o, axi_ref_rsp_in)`）；test slave 的 `b_ready = '1; r_ready = '1`——**测试响应被无条件接收后丢弃**，既不让 test slave 无限背压，也不让它的数据污染 master。
3. **内部比对**：内部例化一个 `axi_bus_compare`，把 ref（端口 A）与 test（端口 B）做 4.2 描述的逐 ID FIFO 比对，产出同样的 `mismatch_o` / `busy_o`。

也就是说，`axi_slave_compare` = 「请求 fork 外壳」+ 「响应只取 ref」+ 「一个 `axi_bus_compare` 内核」。它复用了 4.2 的全部机制，只是替你搭好了最常见的接线。

#### 4.3.3 源码精读

模块端口：一个 master 端口（`axi_mst_req_i` / `axi_mst_rsp_o`）、两个 slave 端口（ref、test 各一对 req/rsp）加 mismatch 输出，见 [src/axi_slave_compare.sv:L20-L78](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_slave_compare.sv#L20-L78)。

请求方向三个 fork（AW/W/AR）：[src/axi_slave_compare.sv:L95-L126](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_slave_compare.sv#L95-L126)。

响应路由与 test 侧丢弃——这段 `always_comb` 是本模块的灵魂：[src/axi_slave_compare.sv:L129-L156](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_slave_compare.sv#L129-L156)：

```systemverilog
`AXI_SET_RESP_STRUCT(axi_mst_rsp_o, axi_ref_rsp_in)   // master 只看 reference 响应
axi_mst_rsp_o.aw_ready = aw_ready_mst;                // fork 汇总的 ready
...
axi_test_req_in.r_ready = '1;                         // test 响应无条件接收
axi_test_req_in.b_ready = '1;                         // 之后丢弃
```

内部 `axi_bus_compare` 例化（ref=端口 A，test=端口 B）：[src/axi_slave_compare.sv:L158-L189](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_slave_compare.sv#L158-L189)。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：跑通 `tb_axi_slave_compare`，确认「同激励双从」无 mismatch；再设计一个「注入差异」的假想，理解 mismatch 如何被发现。

**步骤 1——跑现成 TB（可直接运行）**：

```bash
make compile.log
make sim-axi_slave_compare.log        # 或 ./scripts/run_vsim.sh axi_slave_compare
```

**观察**：日志出现 `Errors: 0,`。TB 拓扑（[test/tb_axi_slave_compare.sv:L72-L177](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_slave_compare.sv#L72-L177)）是 `master → slave_compare → {ref: axi_sim_mem, test: axi_multicut(8) → axi_sim_mem}`。注意 TB 把 `mismatch_o` 等输出全部悬空（`(... )`，[test/tb_axi_slave_compare.sv:L92-L98](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_slave_compare.sv#L92-L98)）——它只靠 driver 自己 `assert(r_data == exp)` 来判数据正确，slave_compare 在这里是「在通路里挂着但不主动报警」。要让比较器真正发挥作用，你应当把 `mismatch_o` 接到一个 `assert final (!mismatch_o)` 或在 `end_of_sim` 处检查。

**步骤 2——假想注入差异（待本地验证，在你自己的副本上）**：把 test 支路的 `axi_sim_mem` 换成一个会返回错误数据的简单从端（或对读数据异或一个常数），保持 reference 不变。重跑后预期：
- master 仍只看到 reference 的正确响应，故 driver 的 `assert` 不炸；
- 但 `axi_bus_compare` 内核会发现 ref 与 test 的 R beat 不等，`r_mismatch_o` / `mismatch_o` 被点亮。
- 若你加了 `assert final (!mismatch_o)`，仿真会在结束时报错——这正是「DUT 偷偷出错也能被抓到」的价值。

**预期结果**：步骤 1 `Errors: 0,`；步骤 2 的 `mismatch_o` 在差异 beat 握手当拍拉高。

#### 4.3.5 小练习与答案

**练习 1**：为什么 test 侧要设 `b_ready='1; r_ready='1`，而不是像 ref 一样把 ready 回送给 master？

> **答**：master 只应消费一份响应（来自 reference）。若 test 的 ready 也回送 master，master 的握手就会被两个 slave 中较慢的那个拖住，且会接到两份响应造成语义混乱。把 test 的 ready 钉死为 1，让 test 响应「尽快被吸走丢弃」，既隔离了 test slave 的背压，又保证 master 看到的唯一真相是 reference。

**练习 2**：`axi_slave_compare` 与 `axi_bus_compare` 是什么关系？能否用前者替代后者？

> **答**：前者 = 请求 fork 外壳 + 响应只取 ref + 一个后者作内核。前者解决「一个 master 比对两个 slave」的特定场景；后者更通用，比对任意两条独立总线（例如同一总线在某个转换模块前后的两段）。前者不能替代后者，因为它强加了「一主二从 + ref 响应回主」的结构。

---

### 4.4 axi_dumper + axi_dumper_interpret.py：事务落盘与离线分析

#### 4.4.1 概念说明

前三个模块是**在线**自检（仿真/FPGA 运行时就报警）。`axi_dumper` 走另一条路：**把每个握手 beat 原样落盘成日志**，事后用 Python 脚本重放分析。它适合那些「仿真跑通了但行为可疑」「想看真实事务流」「要做覆盖率/性能统计」的场景。

它有两个关键设计：

1. **整体被 `ifdef TARGET_SIMULATION` 包住**。若编译时未定义该宏，模块体为空——所以你可以放心地在可综合顶层里常驻例化它，综合时自动消失，仿真时只要加了宏就生效。**重要陷阱**：本仓库的 `scripts/compile_vsim.sh` / `run_vsim.sh` / `Bender.yml` **都不会自动定义 `TARGET_SIMULATION`**（全仓 grep 仅 `axi_dumper.sv` 自身出现）。要用 dumper，你必须自行加 `+define+TARGET_SIMULATION`（见实践步骤）。
2. **日志格式是类 Python 字典**。每行一个 beat，写成 `{'type': "AW", 'time': 1234, 'id': 0x3, 'addr': 0x4000, ...}`。这样脚本能用标准库 `ast.literal_eval` 直接解析成 dict，无需手写词法分析。

`axi_dumper_interpret.py` 则把零散的 beat **重新拼回完整事务**：把每个 AR 与它的 `len+1` 个 R 配对、每个 AW 与它的 W 拍和 B 响应配对，校验 `last` 标志是否自洽，再把突发展开成逐拍地址、合并相邻同地址访问，最后打印统计。

#### 4.4.2 核心流程

**落盘侧（axi_dumper）**，每个时钟沿、对每个被打开（`LogAW` 等）的通道：

```
if (该通道 valid & ready):           # 一次真实握手
    构造 dict {type, time, 各字段: "0x.."}
    把 dict 拼成 '{"type": "AW", "time": .., ...}' 字符串
    $fwrite(f, 字符串 + 换行)
```

**离线侧（interpret 脚本）**：

```
1. 逐行 ast.literal_eval → dict，按 type 分到 aw/ar/w/r/b 五个 list
2. validate_read:  每个 AR 配对其 len+1 个 R（按 id 顺序匹配），校验 last
3. validate_write: 每个 AW 配对其 W 拍 + 一个同 id 的 B，校验 last
4. burst_splitter: 把每笔突发按 2^size 步进展开成逐拍地址
5. recombine_identical: 合并相邻同地址、互补 strobe 的访问
6. 打印统计（AR/R/AW/W/B 数量、收缩后访问数）
```

其中校验逻辑会主动报错：例如某个 AR 说 `len=3` 但收到的 R 提前/推迟出现 `last`，脚本会打印 `R last and length mismatch` 并中止该方向的重放。

#### 4.4.3 源码精读

dumper 模块与参数（`BusName` 决定文件名、`LogAW/AR/W/B/R` 选通道）：[src/axi_dumper.sv:L19-L33](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dumper.sv#L19-L33)。

`TARGET_SIMULATION` 守卫与文件打开——文件名 `axi_trace_<BusName>.log`：[src/axi_dumper.sv:L35-L43](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dumper.sv#L35-L43)。

构造 AW 的 dict（每个字段用 `$sformatf("0x%0x", ...)` 格式化成十六进制字面量，`type` 用带引号字符串）：[src/axi_dumper.sv:L59-L74](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dumper.sv#L59-L74)。

条件落盘（以 AW 为例，`LogAW && aw_valid && aw_ready` 才写）：[src/axi_dumper.sv:L114-L120](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dumper.sv#L114-L120)。`final` 块关闭文件：[src/axi_dumper.sv:L151-L153](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dumper.sv#L151-L153)。

接口外壳 `axi_dumper_intf`（用 `AXI_BUS_DV.Monitor` 监听、`AXI_TYPEDEF_*` 生成类型、`AXI_ASSIGN_TO_REQ/RESP` 搬运）：[src/axi_dumper.sv:L163-L215](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dumper.sv#L163-L215)。

脚本侧——把 strobe 按字节展开成比特掩码：[scripts/axi_dumper_interpret.py:L21-L26](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/axi_dumper_interpret.py#L21-L26)。把一笔突发按 `2**size` 步进拆成逐拍地址：[scripts/axi_dumper_interpret.py:L30-L44](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/axi_dumper_interpret.py#L30-L44)。

读校验 `validate_read`：每个 AR 按 id 在 R 列表里顺序找齐 `len+1` 个响应，校验 `last` 出现在最后一拍：[scripts/axi_dumper_interpret.py:L67-L121](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/axi_dumper_interpret.py#L67-L121)。

写校验 `validate_write`：W 无 id，按 AW 顺序配对 W 拍，末拍再按 id 找 B：[scripts/axi_dumper_interpret.py:L125-L176](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/axi_dumper_interpret.py#L125-L176)。

入口与统计打印：[scripts/axi_dumper_interpret.py:L254-L264](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/axi_dumper_interpret.py#L254-L264)。

#### 4.4.4 代码实践

**实践目标**：在一个已有 TB 里挂一个 dumper，生成日志并用脚本解析。

> 本仓库没有任何 TB 现成例化 `axi_dumper`，且 `TARGET_SIMULATION` 不会被脚本自动定义，所以以下为「源码阅读 + 在你自己的副本上动手」型实践，预期结果标注**待本地验证**。

1. **阅读格式约定**：看 [src/axi_dumper.sv:L114-L120](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dumper.sv#L114-L120) 与脚本 [scripts/axi_dumper_interpret.py:L213-L232](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/axi_dumper_interpret.py#L213-L232)，确认一行日志就是 `ast.literal_eval` 可解析的 dict，`type` 字段是字符串 `"AW"`/`"AR"`/...。
2. **挂探针（在你的副本上）**：在 `tb_axi_lite_regs` 或任意带 `AXI_BUS_DV` 的 TB 里，加一个 monitor 接口并例化：
   ```systemverilog
   axi_dumper_intf #(.BusName("dut"), .LogAW(1'b1), .LogAR(1'b1),
                     .LogW(1'b1), .LogB(1'b1), .LogR(1'b1),
                     .AXI_ID_WIDTH(..), .AXI_ADDR_WIDTH(..),
                     .AXI_DATA_WIDTH(..), .AXI_USER_WIDTH(..))
     i_dumper (.clk_i(clk), .rst_ni(rst_n), .axi_bus(your_dv_if));
   ```
3. **带宏编译**：在编译命令里加 `+define+TARGET_SIMULATION`。最省事的做法是给 `compile_vsim.sh` 的 bender 调用追加一行 `--vlog-arg="+define+TARGET_SIMULATION"`（该脚本在 [scripts/compile_vsim.sh:L21-L25](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/compile_vsim.sh#L21-L25)）。
4. **跑仿真**，确认控制台打印 `[Tracer] Logging axi accesses to axi_trace_dut.log`，且工作目录生成了该文件。
5. **离线解析**：
   ```bash
   python scripts/axi_dumper_interpret.py axi_trace_dut.log 8   # 8 = 数据宽度/8 字节
   ```

**需要观察的现象**：脚本打印 `Successfully processed:` 及读写的 AR/R/AW/W/B 计数，并给出收缩后的访问数；若某笔突发的 `last` 与 `len` 不自洽，会打印 `R last and length mismatch`（**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 dumper 要用 `ifdef TARGET_SIMULATION` 整体包起来，而不是像 `axi_test` 那样直接放在仿真专用的 `simulation` target 里？

> **答**：为了让用户能**在可综合顶层里常驻例化**它。DUT 顶层通常一份 RTL 同时用于综合与仿真；若 dumper 是仿真专用文件，综合时要么报错要么得用宏删例化。用 `ifdef` 包住模块体后，综合时（未定义宏）模块为空壳，例化语句无害；仿真时（定义宏）自动激活——一份代码、两种用途，零维护。

**练习 2（细心阅读）**：看 [scripts/axi_dumper_interpret.py:L48-L63](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/axi_dumper_interpret.py#L48-L63) 的 `recombine_identical`，第 53 行的条件是 `if ~(new_list[-1]['strb'] & stat['strb']):`，用的是按位取反 `~` 而非逻辑非 `not`。在 Python 里 `~0` 等于多少？它作为布尔条件是真还是假？这会对「相邻访问 strobe 有重叠时」的分支选择造成什么影响？

> **答**：Python 中 `~0 = -1`，`bool(-1)` 为 `True`；而对任意非负整数 `x`，`~x` 都是负数，`bool` 恒为 `True`。`strb & stat['strb']` 必非负，故 `~(...)` 恒为真——也就是说这个 `if` 永远成立，第 56 行的 `else`（strobe 有重叠时新起一项）**永远不会执行**。功能上意味着：相邻同地址访问总是被合并，而不论 strobe 是否冲突。这是一个值得向上游反馈的疑似缺陷（写讲义时 HEAD 为 `e55ae2a7`）；结论**待本地用带重叠 strobe 的用例验证**。

---

## 5. 综合实践

设计一个「DUT vs 参考模型」的最小等价性验证台，把本讲四件工具串起来：

**拓扑**：

```
                 ┌──────────── axi_slave_compare ────────────┐
axi_rand_master │  ref 口 ── axi_sim_mem (参考模型)          │
   (u3-l2)   ──┤                                             ├─→ mismatch_o
                │  test 口 ── axi_multicut(4) ── 你的 DUT ──┘   busy_o
                └──────────────────────────────────────────┘
                          │ (在 master 与 slave_compare 之间,
                          │  另挂一个 axi_dumper_intf 做事务落盘)
```

**任务**：

1. 用 u3-l2 / u3-l3 的范式搭出 `axi_rand_master`，接到 `axi_slave_compare` 的 master 口。
2. reference 支路接 `axi_sim_mem`；test 支路接你正在验证的模块（入门可先用 `axi_multicut` 或 `axi_delayer` 代替，它们功能透明，应当零 mismatch）。
3. 把 `mismatch_o` 接到 `assert final (!mismatch_o);`，让任何差异直接判仿真失败。
4. 在 master 与 slave_compare 之间挂一个 `axi_dumper_intf`（开 AW/AR/W/B/R 全通道），按 4.4.4 加 `+define+TARGET_SIMULATION` 编译。
5. 跑若干随机种子（参考 u16-l1 的 `--random-seed`），收集 `axi_trace_*.log`，用 `python scripts/axi_dumper_interpret.py <log> <num_bytes>` 离线统计每笔读写。

**验收**：

- 功能透明 DUT（multicut/delayer）：所有种子 `mismatch_o == 0`、仿真 `Errors: 0,`，脚本统计的 AR 数 = R 数、AW 数 = B 数。
- 故障注入（把 test 支路换成会改数据的从端）：`mismatch_o` 在差异 beat 当拍拉高，`assert final` 触发；dumper 日志里对应事务可被脚本定位。
- 能用一句话说出四个工具的分工：`slave_compare` 在线判异、`bus_compare`/`chan_compare` 给更通用的两路比对（前者可综合、后者仿真带详细打印）、`dumper`+脚本做事后重放与统计。

> 本实践需要改动/新增测试台文件，请在你的工作副本上进行；仿真与脚本输出**待本地验证**。

## 6. 本讲小结

- 本讲的四件工具都服务于「**等价性检查与事务可观测性**」，分两条技术路线：可综合（`axi_bus_compare` / `axi_slave_compare`，能上 FPGA）与仅仿真（`axi_chan_compare` / `axi_dumper`，表达力强、带 `$error` 与详细打印）。
- 共同内核是「**请求方向入队、到达方向出队比对**」：`chan_compare` 用 SV 无界队列最直白地体现这一点；`bus_compare` 用「每 ID 一个 `stream_fifo` + 两侧同 id 都 valid 才弹」把它做成可综合硬件。
- `axi_slave_compare` 是「一主驱双从」的常用包装：请求 fork 给 reference / test 两个 slave，master 只看 reference 响应，test 响应握手后丢弃，内部用一个 `axi_bus_compare` 比对。
- 比对粒度是**逐 beat**；`IgnoreId` / `AllowReordering`（chan_compare）与 `UseSize` / `FifoDepth`（bus_compare）分别处理「ID 被改写」「响应乱序」「窄传输」「时序差」四类合法差异。
- `axi_dumper` 把每个握手 beat 落盘成类 Python 字典日志，整体被 `ifdef TARGET_SIMULATION` 包住（**仓库脚本不会自动定义该宏，需手动 `+define+`**）；`axi_dumper_interpret.py` 用 `ast.literal_eval` 解析，把 beat 重拼成事务并校验 AR↔R、AW↔W↔B 的 `last` 自洽性。
- 这些工具与 u3-l2 的 scoreboard 互补：scoreboard 是「旁路黄金内存模型」，`*_compare` 是「两条活总线逐拍对拍」，dumper 是「事后离线重放」——三者覆盖了从在线自检到事后排错的不同需求。

## 7. 下一步学习建议

- **继续 U16**：下一讲 u16-l3（时序、流水线策略与 EDA 兼容）会讨论 `spill_register` / `LatencyMode` 的取舍与「为何 xbar 内部不插寄存器」的死锁边界——本讲提到的「比对 FIFO 深 ≥ skew」「fork 切断组合依赖」都是同源问题，可对照阅读。
- **上手贡献**：u16-l4（贡献流程与 CI）讲解一次 PR 必须通过的编译/lint/仿真/综合检查；本讲的 `make sim-axi_slave_compare.log` 即仿真检查的一个实例，试着把它纳入你的回归。
- **深读相关源码**：若对「按 ID 分桶 + 两侧对齐」的同步机制感兴趣，可对照 u5-l2 的 `axi_demux_id_counters`（同样是用每 ID 计数器跟踪在途）与 u7-l1 的 `axi_fifo` / `spill_register`，体会 stream 原语在本库中的复用方式。
- **工具链扩展**：尝试给 `axi_dumper_interpret.py` 的 `recombine_identical` 修掉 4.4.5 练习 2 指出的 `~`/`not` 问题（先写一个带重叠 strobe 的最小日志用例复现，再改成 `not`），这会是一次有价值的小型贡献。
