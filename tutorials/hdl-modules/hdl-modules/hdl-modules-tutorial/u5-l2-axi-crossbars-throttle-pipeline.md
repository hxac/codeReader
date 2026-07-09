# AXI 交叉栏、节流与流水线

## 1. 本讲目标

本讲在 u5-l1（AXI-Stream 记录化接口）的基础上，进入完整 AXI4 总线。读完本讲你应当能够：

- 读懂 `axi_pkg` 里把 AXI 五个通道（AR/R/AW/W/B）拆成 m2s/s2m 双向 record 的方式，并理解「字段按最大宽度声明、打包只取实际位宽」的取向。
- 说清 `axi_simple_read/write_crossbar` 为什么叫「simple」：它用锁定式状态机做 N-to-1 仲裁，面积小但永远到不了满吞吐，且存在强输入饿死弱输入的问题。
- 解释 `axi_read/write_throttle` 如何让一个 AXI master「守规矩」，从而在交叉栏场景下不把别的端口饿死。
- 理解 `axi_read/write_pipeline` 如何按通道切分、各自套一个 `handshake_pipeline` 来改善时序，以及 `full_*_throughput` generic 如何在面积与吞吐之间取舍。

## 2. 前置知识

- **AXI4 五通道**：完整 AXI（ARM IHI 0022）把一次事务拆成五个独立握手通道。读路径有 AR（读地址，master→slave）和 R（读数据+响应，slave→master）；写路径有 AW（写地址）、W（写数据）、B（写响应）。每个通道都是独立的 ready/valid 握手，彼此可并行。
- **m2s / s2m 方向**：master-to-slave 是主设备驱向从设备的信号（valid、data、addr 等）；slave-to-master 是从设备回给主设备的信号（ready、resp 等）。本项目用 `_m2s` / `_s2m` 后缀显式标注方向。
- **outstanding 事务**：AR 已被 slave 接受（ARREADY&ARVALID）但对应 R 数据尚未全部返回的状态。AXI 允许多个 outstanding 事务交错，这是高吞吐的来源，也是 simple crossbar 主动放弃的能力。
- **handshake_pipeline 的三种模式**（见 u2-l1）：`full_throughput`、`pipeline_control_signals`、`pipeline_data_signals` 三个 generic 组合出满吞吐 skid buffer（面积大、时序最好）到 1/3 吞吐（面积小）等多种模式。本讲的 pipeline 实体就是逐通道套用它的薄封装。
- **record 与 to_slv 互转**（见 u5-l1）：把一组信号捆成 record 方便端口书写；要进 FIFO 缓冲时再用 `to_slv` 拍平成向量、出 FIFO 后用反向函数还原。本讲会反复用到这一套路。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [modules/axi/src/axi_pkg.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_pkg.vhd) | AXI4 数据类型包：五通道的 record 定义、宽度子类型、burst/resp 常量、record↔slv 转换函数 |
| [modules/axi/src/axi_simple_read_crossbar.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_simple_read_crossbar.vhd) | N-to-1 读交叉栏：锁定式状态机仲裁多 master 到单 slave |
| [modules/axi/src/axi_simple_write_crossbar.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_simple_write_crossbar.vhd) | N-to-1 写交叉栏：四态状态机覆盖 AW/W/B 全流程 |
| [modules/axi/src/axi_read_throttle.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_read_throttle.vhd) | 读节流器：按 R FIFO 余量限制 outstanding 拍数，避免 R 通道 stall |
| [modules/axi/src/axi_write_throttle.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_write_throttle.vhd) | 写节流器：保证 AWVALID 与首个 WVALID 同拍、整段 W 突发无洞 |
| [modules/axi/src/axi_read_pipeline.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_read_pipeline.vhd) | 读总线流水线：对 AR、R 两通道各套一个 handshake_pipeline |
| [modules/axi/module_axi.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/module_axi.py) | 仿真 generic 矩阵与 netlist 资源回归断言（量化各实体面积） |
| [modules/axi/test/tb_axi_simple_crossbar.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/test/tb_axi_simple_crossbar.vhd) | 交叉栏仿真：4 个 master BFM + 1 个 slave BFM，随机交错事务 |

## 4. 核心概念与源码讲解

### 4.1 axi_pkg：用 record 把 AXI 五通道类型化

#### 4.1.1 概念说明

AXI4 有五个通道，每个通道又分主→从和从→主两个方向，手写端口会变成一长串 `arvalid/arready/araddr/arlen/...`。`axi_pkg` 的做法是把每个通道、每个方向都做成一个 record，再把读/写两个方向的通道聚合成「读总线」「写总线」，最后聚合成一条完整 AXI 总线。这样端口声明从几十行缩到一行，且类型检查能防止把 AR 的 ready 接到 R 的 valid 上。

和 u5-l1 的 `axi_stream_pkg` 一样，本包遵循两条取向：**字段按最大宽度声明**（`axi_data_sz=128`、`axi_id_sz=24`、`axi_a_addr_sz=64`），综合时只用实际声明的位宽；**打包时排除 `valid`/`ready` 这类握手控制位**（它们不进 RAM，单独走）。

#### 4.1.2 核心流程

类型层次如下（自底向上聚合）：

```
通道级 record                 方向级聚合              总线级聚合
axi_m2s_a_t (valid/id/addr/  ┐
             len/size/burst) ├─ axi_read_m2s_t  ┐
axi_m2s_r_t (ready)          ┘                   ├─ axi_m2s_t
axi_s2m_a_t (ready)          ┐                   │
axi_s2m_r_t (valid/id/data/  ├─ axi_read_s2m_t  ┤
             resp/last)      ┘                   │
axi_m2s_a_t  ┐                                   │
axi_m2s_w_t (valid/data/strb/├─ axi_write_m2s_t ┤
             last/id)        │                   │
axi_m2s_b_t (ready)          ┘                   │
axi_s2m_a_t  ┐                                   │
axi_s2m_w_t (ready)          ├─ axi_write_s2m_t ┘
axi_s2m_b_t (valid/id/resp)  ┘
```

每个 record 都配一个 `_init` 常量（valid 默认 `'0'`、ready 默认 `'0'`，即悬空端口选「不发起/不接收」的安全侧），以及 `to_slv` / `to_xxx` 一对互逆函数用于 FIFO 打包。

#### 4.1.3 源码精读

地址通道的 master→从 record，注释里写明排除了 lock/cache/prot/region（这些信号通常不逐事务变化，省掉以减小打包宽度）：

[axi_pkg.vhd:131-140](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_pkg.vhd#L131-L140) —— 定义 `axi_m2s_a_t`，含 valid/id/addr/len/size/burst 六个字段。

读数据通道的 slave→master record，包含响应 `resp` 与包尾 `last`：

[axi_pkg.vhd:294-300](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_pkg.vhd#L294-L300) —— 定义 `axi_s2m_r_t`（valid/id/data/resp/last）。

读/写两条总线把通道两两聚合，得到一行就能声明一条总线的便利：

[axi_pkg.vhd:330-344](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_pkg.vhd#L330-L344) —— `axi_read_m2s_t`/`axi_read_s2m_t` 把 AR 与 R 通道合在一起。

`to_slv` 函数把 record 拍平成连续向量，注意它跳过 `valid`（注释 `Excluded member: valid`），并按 id→addr→len→size→burst 顺序拼接：

[axi_pkg.vhd:447-480](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_pkg.vhd#L447-L480) —— `to_slv(axi_m2s_a_t)` 的逐段拼接实现。

响应码常量遵循 AXI 标准（OKAY/EXOKAY/SLVERR/DECERR），`combine_response` 在多段路径汇总响应时取「最坏」优先级：

[axi_pkg.vhd:777-809](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_pkg.vhd#L777-L809) —— 错误优先级 DECERR > SLVERR > OKAY > EXOKAY。

#### 4.1.4 代码实践

1. **实践目标**：直观感受 record 把端口声明缩短多少。
2. **操作步骤**：打开 `tb_axi_simple_crossbar.vhd`，看它如何用一行 `axi_read_m2s_vec_t(0 to num_inputs-1)` 声明 4 条读总线的全部 m2s 信号。
3. **需要观察的现象**：若不用 record，单条读总线的 m2s 侧就要手写 ar.valid/ar.id/ar.addr/ar.len/ar.size/ar.burst/r.ready 共 7 个信号，4 条总线 28 个。
4. **预期结果**：用 record 后端口块只有 `inputs_read_m2s` 一个数组信号，配合 [tb_axi_simple_crossbar.vhd:54-59](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/test/tb_axi_simple_crossbar.vhd#L54-L59) 的初始化即可。
5. 待本地验证：可在自己的 testbench 里把同一组端口分别用 record 与裸信号写一遍，对比行数。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `axi_m2s_a_t` 排除了 `lock`/`cache`/`prot`/`region`，却不排除 `len`/`size`/`burst`？

**答案**：后三者每个事务都可能不同（突发长度、每拍字节数、突发类型），必须随地址一起传；前四者通常整个设计固定不变，逐事务传递会无谓增加 record 宽度和 FIFO 打包代价，需要时由用户在顶层单独接线。

**练习 2**：`to_slv(axi_m2s_a_t)` 里 `valid` 被排除。如果某实体要把整条 AR 通道（含 valid）存进 FIFO，valid 该怎么处理？

**答案**：valid 是握手控制位，不随数据进 RAM。FIFO 自身的 `input_valid`/`output_valid` 就是这条 AR 的 valid，数据位只存 `to_slv` 拍平的其余字段（u5-l1 的 axi_stream_fifo、本讲的 axi_read_pipeline 都是这套做法）。

---

### 4.2 axi_simple_read/write_crossbar：锁定式 N-to-1 仲裁

#### 4.2.1 概念说明

交叉栏（crossbar）解决「多个 master 想访问同一个 slave」的复用问题。本模块的「simple」体现在：它**不分离通道**——没有为 AR 和 R 分别建队列，而是选中一个输入端口后**整体锁定**，直到这个端口的整条读事务（AR + 全部 R 拍）跑完，才放手去服务（可能不同的）下一个端口。

代价与收益都很明确：面积极小（4 输入读交叉栏仅 120 LUT），但数据通道永远到不了满吞吐——因为它无法在 R 突发进行中提前排队下一个 AR。要更高吞吐就得换成「分离通道、各自排队」的交叉栏。

仲裁用最朴素的固定优先级扫描：从下标 0 往上找第一个 `ar.valid`，因此**一个持续发事务的强输入会饿死其余弱输入**。这正是下一节 throttle 要解决的问题。

#### 4.2.2 核心流程

读交叉栏的三态状态机（写交叉栏多一态，思路相同）：

```
idle ──(扫描到 input_ports_m2s(k).ar.valid)──> wait_for_ar_done
        选定 input_select <= k
        │
        ▼
wait_for_ar_done ──(output_s2m.ar.ready)──> wait_for_r_done
        (AR 被slave接受)
        │
        ▼
wait_for_r_done ──(r.ready & r.valid & r.last)──> idle
        (本突发最后一拍读完)
```

数据面（`assign_bus`）的逻辑是「先广播再屏蔽」：

1. 把所有输入端口的 s2m 默认赋成 output_s2m（广播 slave 的回话）。
2. 再把每个端口的 `ar.ready`/`r.valid` 用 `(input_select == input_idx) and let_*_through` 屏蔽，只有被选中的端口且处于对应状态才真正通。
3. 输出 m2s 取 `input_ports_m2s(input_select)`，并把其 valid/ready 再用 `let_*_through` 门控，确保非选中状态下不漏传。

满吞吐为何不可达：设突发长度为 \(B\) 拍、从 AR 被接受到首个 R 返回的响应延迟为 \(L\) 拍。simple crossbar 必须等当前突发的 `r.last` 跑完才回 idle 发下一个 AR，于是两个突发之间至少插入了 \(1 + L\) 拍空档（1 拍 AR 握手 + 响应延迟）。数据通道利用率上限约为：

\[
\eta_{\max} \approx \frac{B}{B + 1 + L}
\]

要逼近 1，必须在 R 突发进行中就排队下一个 AR（outstanding > 1），这正是 simple crossbar 主动放弃的能力。

#### 4.2.3 源码精读

实体只有一个 generic `num_inputs`，端口用本节 4.1 的向量 record 一行声明：

[axi_simple_read_crossbar.vhd:36-49](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_simple_read_crossbar.vhd#L36-L49) —— N 个读总线输入、1 个读总线输出。

状态机在 `idle` 态用 for 循环从低下标往高扫描，第一个 `ar.valid` 即被选中（固定优先级）：

[axi_simple_read_crossbar.vhd:67-74](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_simple_read_crossbar.vhd#L67-L74) —— 选源逻辑，`input_select_next` 命中即记录并切到 `wait_for_ar_done`。

`wait_for_r_done` 要等到 `r.last` 才回 idle——这就是「锁定到整包结束」：

[axi_simple_read_crossbar.vhd:83-86](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_simple_read_crossbar.vhd#L83-L86) —— 等待最后一拍读完。

`let_ar_through`/`let_r_through` 是状态的组合译码，注释说明若改成寄存器可再省一级逻辑深度：

[axi_simple_read_crossbar.vhd:94-95](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_simple_read_crossbar.vhd#L94-L95) —— 把状态翻译成「放行」电平。

数据面广播 + 屏蔽：默认全端口拿 output_s2m，再按选中号与放行位门控：

[axi_simple_read_crossbar.vhd:99-117](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_simple_read_crossbar.vhd#L99-L117) —— `assign_bus` 进程，注意 `output_m2s <= input_ports_m2s(input_select)` 后再单独改写 valid/ready。

写交叉栏多一个 B（写响应）通道，状态机四态，逐通道门控 `let_aw/let_w/let_b`：

[axi_simple_write_crossbar.vhd:58-97](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_simple_write_crossbar.vhd#L58-L97) —— `wait_for_aw_done`→`wait_for_w_done`→`wait_for_b_done`→`idle`。

面积对比（4 输入，来自 netlist 回归）：读交叉栏 120 LUT、5 FF、逻辑级数 4；写交叉栏 298 LUT、5 FF、逻辑级数 4。写比读贵一倍多，因为它要管 AW/W/B 三个通道而非 AR/R 两个：

[module_axi.py:126-154](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/module_axi.py#L126-L154) —— 两个交叉栏的 `build_result_checkers` 断言。

#### 4.2.4 代码实践

1. **实践目标**：观察 simple crossbar 的「锁定到整包结束」与「无法满吞吐」。
2. **操作步骤**：
   - 阅读 [tb_axi_simple_crossbar.vhd:100-158](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/test/tb_axi_simple_crossbar.vhd#L100-L158) 的 `send_bursts` 过程，理解它用随机 `input_select` 与短突发（`FavorSmall(1,16)`）制造交错。
   - 运行 `tools/simulate.py axi.tb_axi_simple_crossbar`（若本地已装 VUnit 与 tsfpga）。
   - 在 `dut_read` 实体的 `wait_for_r_done` 分支加一行 `report "locked on input " & to_string(input_select);`，重跑 `test_random_read`。
3. **需要观察的现象**：每次锁定会持续到 `r.last` 才切换；当某个输入连续发事务时，报告里几乎只有它的下标。
4. **预期结果**：4 个输入总吞吐低于单 slave 带宽，验证上面 \(\eta_{\max}\) 公式；强输入饿死弱输入的现象可复现。
5. 待本地验证：若本地无 Vivado/VUnit，至少完成「阅读 + 加 report + 人工推演状态机」部分。

#### 4.2.5 小练习与答案

**练习 1**：把 `assign_bus` 里 `output_m2s.ar.valid <= ... and let_ar_through` 去掉 `let_ar_through`，会发生什么？

**答案**：非 `wait_for_ar_done` 状态（如 idle、wait_for_r_done）下 AR 也会被放行到 slave，破坏锁定语义——可能在 R 突发未完成时就发起新 AR，导致 slave 把数据混到错误的 master。`let_ar_through` 是锁定的闸门。

**练习 2**：写交叉栏 298 LUT 约为读交叉栏 120 LUT 的 2.5 倍，但两者 FF 都是 5、逻辑级数都是 4。为什么 FF 不随通道数增长？

**答案**：FF 几乎全用在 `input_select` 与状态寄存器上（数量与通道数无关，只随 `num_inputs` 的位宽缓慢变化）；多出来的 LUT 主要是 AW/W/B 三套「广播+屏蔽」组合逻辑，属于数据面多路选择，不新增寄存器。

---

### 4.3 axi_read/write_throttle：让 master「守规矩」

#### 4.3.1 概念说明

在交叉栏场景里，simple crossbar 的固定优先级会让一个「不守规矩」的 master（持续拉高 ARVALID、乱发长突发）饿死其他端口。throttle 的职责是给每个 master 加上自我约束，使其不会发出超出下游处理能力的事务，从而让交叉栏的公平性有保障。

读节流器 `axi_read_throttle` 的思路：在 master 与 slave 之间串一个 R 数据 FIFO，节流器盯着这个 FIFO 的 `level`，**在 AR 发出前就预判**「这一突发回来会不会把 FIFO 写爆」。会爆就不发 AR。这样 `throttled_s2m.r` 永远不会因为 FIFO 满而 stall，master 始终 well-behaved。

写节流器 `axi_write_throttle` 的思路不同但目标一致：保证 `AWVALID` 与对应突发的首个 `WVALID` 同拍出现、且整段 W 突发无洞——这是 AXI 写 master 最严格的「规矩」形态，确保下游不浪费任何一拍。

两者都强调：**如果下游 slave 自带 AR/W 通道 FIFO（如 DDR4 控制器或 Zynq 硬 AXI 口），就不必用 throttle**——throttle 只在交叉栏这种需要约束每端口行为的场景才有意义。

#### 4.3.2 核心流程

读节流器维护一个计数 `num_beats_negotiated_but_not_sent`（已通过 AR 约定但尚未返回的 R 拍数），并读取下游 R FIFO 的 `data_fifo_level`。判定式为：

```
block_address_transactions :=
  burst_length_beats >= num_empty_words_in_fifo_that_have_not_been_negotiated
```

其中 `num_empty_words_in_fifo_that_have_not_been_negotiated = (FIFO 空位) - (已约定未发送拍数)`。一旦本次突发会把「未约定」的空位用光，就拉低 `throttled_m2s.ar.valid`。

这里有个关键时序细节：`data_fifo_level` 是在 `RREADY&RVALID` 那个上升沿更新的，而 AR 的算术必须立刻生效（不能等一拍，否则可能多发）。因此 R 拍计数被故意延迟一拍（`data_transaction_p1`），让算术与 level 对齐，避免「以为有空位→发 AR→下一拍 level 更新后又撤掉」的抖动。

写节流器则是两态状态机：`wait_for_input_valid`（等 AW 与 W 同时有效）→ `let_w_burst_pass`（放行整段 W 突发到 `w.last`）→ 回 `wait_for_input_valid`。

#### 4.3.3 源码精读

读节流器先对 AR 通道做一级 `handshake_pipeline`（改善 ARVALID 时序），`full_ar_throughput` generic 控制是每拍可发一次还是每三拍一次：

[axi_read_throttle.vhd:85-90](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_read_throttle.vhd#L85-L90) —— generic 注释说明 false=每三拍一次（面积小）、true=每拍一次（面积大）。

[axi_read_throttle.vhd:146-165](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_read_throttle.vhd#L146-L165) —— AR 通道的 pipeline 块，注意 `pipeline_control_signals => true` 硬编码（时序优先）。

为了缩短从 ARLEN 到 ARVALID 的关键路径，作者把 `burst_length = ARLEN + 1` 改写成 `-inv(ARLEN)`，并把减号挪到等式右侧（用补码取反替代加法），代价是信号命名变晦涩：

[axi_read_throttle.vhd:180-181](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_read_throttle.vhd#L180-L181) —— `minus_burst_length_beats <= not u_signed('0' & pipelined_m2s_ar.len(len_range));` 即 `-(len+1) = -burst_length`。

[axi_read_throttle.vhd:194-216](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_read_throttle.vhd#L194-L216) —— 注释完整记录了原公式与改写后的公式，以及为何要这样换。

计数进程里 `data_transaction_p1` 的延迟与对齐逻辑，注释解释得很清楚：

[axi_read_throttle.vhd:236-258](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_read_throttle.vhd#L236-L258) —— R 拍计数延迟一拍以匹配 FIFO level 更新时刻。

写节流器的状态机保证 AWVALID 与首个 WVALID 同拍：

[axi_write_throttle.vhd:102-118](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_write_throttle.vhd#L102-L118) —— 两态机，`wait_for_input_valid` 同时检查 `input_m2s.aw.valid and input_m2s.w.valid`。

面积量化（netlist 回归）：`axi_write_throttle` 仅 5 LUT、2 FF、逻辑级数 2（极小）；`axi_read_throttle`（`full_ar_throughput=False`，depth=1024）41 LUT、76 FF、逻辑级数 8：

[module_axi.py:90-124](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/module_axi.py#L90-L124) —— 两个 throttle 的 generic 与资源断言。

仿真侧用 `check_well_behaved` 进程断言「`throttled_s2m.r.valid` 时 `throttled_m2s.r.ready` 必为 1」，即被节流后的 R 通道永不 stall：

[tb_axi_read_throttle.vhd:162-168](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/test/tb_axi_read_throttle.vhd#L162-L168) —— 「Should never stall」断言。

#### 4.3.4 代码实践

1. **实践目标**：理解节流器为何要配合 R FIFO、以及 `data_fifo_depth` 与 `max_burst_length_beats` 的关系。
2. **操作步骤**：
   - 阅读 [tb_axi_read_throttle.vhd:44-79](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/test/tb_axi_read_throttle.vhd#L44-L79)，注意 `data_fifo_depth = 2 * max_burst_length_beats`，且 master 的 `r_stall_config` 故意高概率 stall（触发「block AR」条件）。
   - 把 `data_fifo_depth` 改成等于 `max_burst_length_beats`（深度刚好一个突发），预测节流器还能不能正常工作、是否会出现 R stall。
3. **需要观察的现象**：原配置下 `check_well_behaved` 不报错；改浅后可能在 stall 概率高时出现边界抖动。
4. **预期结果**：深度减半会压缩余量，极端情况下 FIFO 趋于满、节流器更频繁地 block AR，吞吐下降但 `check_well_behaved` 仍应成立（因为节流器正是为此而存在）。
5. 待本地验证：若无法运行，请人工推演「FIFO 空位 = depth - level」「未约定空位 = 空位 - outstanding」两个量在突发进行中的变化曲线。

#### 4.3.5 小练习与答案

**练习 1**：为什么节流器要求下游 R FIFO 的 `level` 必须在写事务同一上升沿更新（见文件头 warning）？

**答案**：节流器的算术用 `data_transaction_p1`（延迟一拍的 R 拍）去对齐 `data_fifo_level`。若 level 滞后于写事务一拍，两者就错位，可能出现「以为还有空位→发 AR→level 下一拍才反映刚写入→撤掉 AR」的抖动，甚至溢出。

**练习 2**：写节流器要求 `input.b.ready` 静态为 `'1'`、且 W 侧 FIFO 开 packet 模式。删掉任一前提会怎样？

**答案**：若 `b.ready` 不恒为 1，B 响应可能 stall，而节流器已放行 W 突发，下游会浪费拍子；若 W FIFO 不开 packet 模式，`WVALID` 可能在突发中间被打断（出现洞），违背「整段无洞」前提，节流器的 well-behaved 保证失效。

---

### 4.4 axi_read/write_pipeline：按通道切分的握手流水线

#### 4.4.1 概念说明

当一条 AXI 总线路径太长、时序不收敛时，最直接的修法是插寄存器。`axi_read_pipeline` / `axi_write_pipeline` 就是把上一讲的 `handshake_pipeline`（u2-l1）按通道套到 AXI 上：**每个通道独立一个 handshake_pipeline**，互不影响。

这样设计有两个好处：一是各通道时序独立可调（AR 路径长就单独流水 R 不动）；二是 `handshake_pipeline` 的三个 generic（`full_throughput`/`pipeline_control_signals`/`pipeline_data_signals`）原封不动暴露出来，让用户在「满吞吐 skid buffer（面积大）」与「降吞吐（面积小）」之间按通道单独取舍。

#### 4.4.2 核心流程

读流水线把 AR（m2s，左→右）与 R（s2m，右→左）各套一个 pipeline：

```
        AR通道                      R通道
left_m2s.ar ──> to_slv ──> handshake_pipeline ──> to_record ──> right_m2s.ar
                                                (full_address_throughput)
right_s2m.r <── to_record <── handshake_pipeline <── to_slv <── left_s2m.r
                                                (full_data_throughput)
```

写流水线同理，只是通道变成 AW、W（m2s 左→右）与 B（s2m 右→左）三个。

每个通道的封装套路完全一致：

1. `to_slv` 把该通道 record 拍平成向量。
2. `handshake_pipeline` 对向量做寄存器插入（`pipeline_control_signals=true`、`pipeline_data_signals=true`，即控制位与数据位都流水）。
3. 反向 `to_record` 还原成 record，再单独接上 `valid`/`ready`（因为 valid 被排除在打包之外，由 pipeline 的 `output_valid` 承载）。

#### 4.4.3 源码精读

读流水线实体暴露 `full_address_throughput`/`full_data_throughput` 两个 generic，分别对应 AR、R 通道的 `full_throughput`：

[axi_read_pipeline.vhd:22-41](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_read_pipeline.vhd#L22-L41) —— 两个吞吐 generic 均默认 `true`（满吞吐、面积大）。

AR 通道块：record→slv→handshake_pipeline→record，valid 由 pipeline 的 `output_valid` 驱动：

[axi_read_pipeline.vhd:48-85](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_read_pipeline.vhd#L48-L85) —— `ar_block`，注意 `pipeline_control_signals=>true, pipeline_data_signals=>true`。

R 通道块方向相反（数据从 right 流向 left），但套路相同：

[axi_read_pipeline.vhd:89-126](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_read_pipeline.vhd#L89-L126) —— `r_block`，`input_data` 取自 `right_s2m.r`，输出回 `left_s2m.r`。

写流水线的 W 通道块假设 AXI4（无 WID），故 `to_slv` 的 `id_width` 取 0：

[axi_write_pipeline.vhd:89-128](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/src/axi_write_pipeline.vhd#L89-L128) —— `w_block`，注释 `Assume AXI4 (no WID)`。

注意读/写 pipeline 都用 `use work.axi_pkg.all`（`library common` 之外没有 `library axi`），因为它们就编译在 `axi` 库里，`work` 即本库——这是「库名即模块名」约定的一个小细节（见 u1-l2）。

#### 4.4.4 代码实践

1. **实践目标**：体会「按通道独立调吞吐」的灵活性。
2. **操作步骤**：
   - 阅读 [tb_axi_pipeline.vhd:190-204](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/test/tb_axi_pipeline.vhd#L190-L204)，看 `axi_read_pipeline` 如何用随机的 `addr_width`/`id_width`/`data_width` 实例化。
   - 在自己的 testbench 里实例化 `axi_read_pipeline`，先两个 generic 都设 `true`，跑通读事务；再把 `full_data_throughput` 设 `false`，对比 R 通道吞吐变化。
3. **需要观察的现象**：`full_data_throughput=false` 时 R 通道每三拍才能传一拍（见 handshake_pipeline 的降吞吐模式），整体读带宽下降。
4. **预期结果**：功能（数据正确性）不变，吞吐下降、面积减小——这正是 generic 取舍的意义。
5. 待本地验证：吞吐量化需实际仿真波形；若无法运行，可阅读 `handshake_pipeline.vhd` 的 `full_throughput=false` 分支确认每三拍一发的行为。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `axi_read_pipeline` 的 AR 块和 R 块要分开实例化两个 `handshake_pipeline`，而不是合并成一个？

**答案**：AR 是 m2s（左→右）、R 是 s2m（右→左），方向相反且位宽不同；更关键的是要让用户能对地址路径与数据路径分别选吞吐模式（地址稀疏、数据密集，两者时序压力不同），合并就丧失了这种逐通道可调的灵活性。

**练习 2**：`pipeline_control_signals` 和 `pipeline_data_signals` 在 pipeline 实体里都硬编码为 `true`，只把 `full_throughput` 暴露成 generic。这样设计合理吗？

**答案**：合理。这两个 generic 都为 `true` 才能保证 valid 与 data 都被寄存、切断组合路径，这是「插流水线改善时序」的本意；若允许关掉会退化成不流水，违背实体用途。而 `full_throughput` 直接决定面积/吞吐，是用户真正需要取舍的维度，故单独暴露。

---

## 5. 综合实践

把本讲四个模块串起来，搭建一个「多 master 访问单 DDR slave」的典型拓扑，并解释每个模块的位置与作用。

**目标拓扑**（数据流）：

```
master0 ─┐
master1 ─┼─[axi_read_throttle ×N]─[axi_read_pipeline ×N]─┬─[axi_simple_read_crossbar]─ DDR
master2 ─┤                                                  │
master3 ─┘                                                  └─(可选 axi_read_cdc, 见 u5-l3)
```

**任务**：

1. 在 [tb_axi_simple_crossbar.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi/test/tb_axi_simple_crossbar.vhd) 基础上，给每个 master 输入串一个 `axi_read_throttle`（配一个 R FIFO，深度取 `2 * max_burst_length_beats`），再串一个 `axi_read_pipeline`。
2. 用 `tools/simulate.py axi.tb_axi_simple_crossbar` 跑随机交错读事务，确认数据正确（`check_expected_was_written` 通过）。
3. 对比「加 throttle/pipeline 前」与「加之后」两种配置下，各 master 的吞吐分布：解释 throttle 如何避免 master0 持续发长突发时饿死 master3。
4. 写一段结论说明：simple crossbar 本身不解决公平性，公平性由每端口 throttle 提供；pipeline 则纯粹改善时序、不改变仲裁行为。

**预期结果**：加 throttle 后四个 master 的吞吐更均衡；功能层面所有数据仍正确。时序层面 pipeline 让 crossbar 到 slave 的长路径被寄存器打断（逻辑级数下降）。若本地无仿真环境，请至少完成拓扑图绘制与每个模块职责的文字说明，并标注「待本地验证」。

## 6. 本讲小结

- `axi_pkg` 把 AXI 五通道拆成 m2s/s2m 双向 record，再逐层聚合成读/写/完整总线，字段按最大宽度声明、打包时排除 valid/ready 控制位。
- `axi_simple_read/write_crossbar` 用「锁定到整包结束」的状态机做 N-to-1 仲裁，面积小（读 120 LUT / 写 298 LUT）但永远到不了满吞吐，且固定优先级会饿死弱输入。
- 满吞吐上限 \(\eta_{\max} \approx B/(B+1+L)\)，要逼近 1 必须 outstanding > 1，而 simple crossbar 主动放弃了这一点。
- `axi_read_throttle` 盯着下游 R FIFO 余量预判溢出、在 AR 发出前 block，使被节流后的 R 通道永不 stall；为缩短关键路径把 `ARLEN+1` 改写成 `-inv(ARLEN)`。
- `axi_write_throttle` 用两态机保证 AWVALID 与首个 WVALID 同拍、整段 W 突发无洞，是 AXI 写 master 最严格的 well-behaved 形态。
- `axi_read/write_pipeline` 按通道各套一个 `handshake_pipeline`，把 `full_throughput` 暴露成 generic，让用户逐通道在面积与吞吐间取舍。

## 7. 下一步学习建议

- 下一讲 **u5-l3（AXI 跨时钟域与通道 FIFO）** 会把本讲的通道流水线推广到跨时钟域，讲解 `axi_read_cdc`/`axi_write_cdc` 如何复用 `asynchronous_fifo` 与 `resync`，以及 `axi_address_fifo`/`axi_b_fifo`/`axi_r_fifo`/`axi_w_fifo` 如何按通道拆缓冲。建议先回顾 u4-l1（同步 FIFO）与 u3-l1（resync）。
- 若想深入「分离通道、各自排队」的高吞吐交叉栏，可阅读 `axi_lite_simple_read_crossbar.vhd`（u5-l4）对比，理解 Lite 子系统如何处理 mux/cdc/pipeline 的组合。
- 想验证本讲行为，可直接跑 `tools/simulate.py axi.tb_axi_simple_crossbar`、`... tb_axi_read_throttle`、`... tb_axi_pipeline`，并在 `module_axi.py` 的 `get_build_projects` 里查看每个实体的 netlist 资源回归断言。
