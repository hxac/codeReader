# PTP 时间分发：PHC 与 leaf

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 **为什么要做「时间分发」**：一个主时钟（PHC）把 PTP 时间通过一根串行线广播给一个或多个叶时钟（leaf），并理解它和上一讲 `ptp_clock_cdc`「重建+锁定」方案的取舍。
- 读懂主时钟 `ptp_td_phc` 的串行消息格式：14 个 17 位字、三类消息轮转、LSB 先发、空闲为 1。
- 读懂叶时钟 `ptp_td_leaf` 如何在**自己的目的时钟域**里用数字 PLL（DPLL）重建出 96 位 ToD 与 64 位相对时间，并理解三级 `locked` 与 `TD_SDI_PIPELINE` 流水线延迟补偿。
- 读懂 `ptp_td_rel2tod` 如何仅凭「截断的相对时间戳 + 广播来的共享小数纳秒与偏移量」**还原出完整的 96 位 ToD**，理解秒边界处的二选一消歧。

## 2. 前置知识

本讲建立在 u11-l1（`ptp_clock`）之上，请先确认你理解以下概念：

- **两种时间戳格式**：96 位 ToD（秒 + 纳秒 + 小数纳秒，到 10⁹ ns 进秒回卷）与 64 位相对（纳秒 + 小数纳秒，单调累加、不回卷）。
- **小数纳秒 fns**：一个定点小数字段（默认 16 位），把 `{ns, fns}` 当成一个宽整数相加即可获得亚纳秒分辨率，步长由时钟周期决定（默认 6.4 ns 对应 156.25 MHz）。
- **跨时钟域「不靠搬运、靠重建」**：u11-l2 的 `ptp_clock_cdc` 不是把时间戳搬过时钟域，而是在目的域重新生成本地自由时钟，再用闭环 PLL 锁到源域。本讲的 leaf 沿用完全相同的 DPLL 思路，区别在于「源」不是并行总线而是一根串行消息线。

本讲会反复用到「秒」「纳秒」「小数纳秒」三个尺度，以及一个关键事实：**ToD 的纳秒部分与相对时间的纳秒部分，每拍都按同一个步长前进**，因此它们之间只差一个缓慢变化的「偏移量」。抓住这一点，`ptp_td_rel2tod` 的全部魔法就豁然开朗。

## 3. 本讲源码地图

| 文件 | 角色 | 关键点 |
| --- | --- | --- |
| [rtl/ptp_td_phc.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_td_phc.v) | **主时钟（PHC）**：自身就是一台自由运行的 PTP 时钟，同时把完整时间状态串行化到 `ptp_td_sdo` 一根线上广播 | 既走时间又发消息；单个共享加法器 + 16 状态机；14 字消息格式 |
| [rtl/ptp_td_leaf.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_td_leaf.v) | **叶时钟**：从 `ptp_td_sdi` 串行解串，在目的时钟域用 DPLL 重建 96 位 ToD 与 64 位相对时间 | 三时钟域（ptp_clk/clk/sample_clk）；三级锁定；流水线延迟补偿 |
| [rtl/ptp_td_rel2tod.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_td_rel2tod.v) | **轻量还原器**：用截断相对时间戳 + 广播偏移/小数纳秒，还原完整 96 位 ToD | 不跑 DPLL；秒边界二选一消歧 |
| tb/ptp_td.py | cocotb 端的 `PtpTdSource`/`PtpTdSink` 参考模型，与 RTL 位级对齐 | 既是验证依据，也是理解消息格式的最佳旁证 |
| tb/ptp_td_phc, tb/ptp_td_leaf, tb/ptp_td_rel2tod | 三个现成 testbench | 提供可运行实践 |

## 4. 核心概念与源码讲解

### 4.1 串行时间分发（ptp_td_phc）

#### 4.1.1 概念说明

`ptp_td_phc` 解决的问题是：**「我有一个权威的 PTP 时间，如何把它送给很多个位于不同时钟域的使用者？」**

最直接的办法是给每个使用者都拉一组并行的 96 位 ToD 总线，再用 `ptp_clock_cdc` 逐个跨域——但使用者一多，连线爆炸。PHC 的思路是**串行广播**：把完整的时间状态（ToD 秒/纳秒、相对纳秒、共享小数纳秒、周期、漂移、偏移）打包成一条反复发送的消息流，从一根 `ptp_td_sdo` 线送出；任意多个 leaf 只需各自接上 `ptp_td_sdi` 即可重建。这是「一对多」的分发，代价是引入了串行化延迟——这条延迟随后由 leaf 的 DPLL 和 `TD_SDI_PIPELINE` 参数补偿。

需要特别强调：**PHC 自身就是一台自由运行的 PTP 时钟**。它不像 `ptp_clock_cdc` 那样接收一个外部 `ptp_clock` 的输入，而是把「走时间」和「发消息」融合在一个模块里：它内部维护 ToD 秒/纳秒、相对纳秒、小数纳秒、周期、漂移，按周期自行前进，并支持原子地加载或偏移这些值，同时把状态反复串行化出去。

#### 4.1.2 核心流程

PHC 每个更新周期做两件事，二者并行：

1. **走时间（单共享加法器 + 多状态机）。** 注意它没有为「fns 累加」「相对 ns 累加」「ToD ns 累加」「秒进位」「偏移计算」各配一个加法器，而是**复用同一个 48 位加法器**，靠一个 16 状态的 `update_state_reg` 状态机分拍依次完成。源码顶部的大段注释精确描述了这一串计算（[ptp_td_phc.v:249-267](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_td_phc.v#L249-L267)）：

   ```
   {ts_inc_ns, ts_fns} = drift_acc + offset_fns + {period_ns, period_fns}*256 + ts_fns
   ts_rel_ns  = ts_rel_ns  + ts_inc_ns + rel_offset_ns
   ts_tod_ns  = ts_tod_ns  + ts_inc_ns + tod_offset_ns   // 含进/借位到秒
   ts_tod_offset_ns = ts_tod_ns - ts_rel_ns              // 广播用的偏移量
   ```

2. **发消息（移位寄存器串行化）。** 把当前状态装进一个 `17*14` 位的移位寄存器，每拍右移 1 位、LSB 先发。

#### 4.1.3 源码精读

**端口与参数。** PHC 把「时间控制」做成多组 `valid/ready` 握手输入：可粗载 ToD（`input_ts_tod_*`）、粗载相对时间（`input_ts_rel_*`）、原子偏移（`input_ts_*_offset_*`）、改周期（`input_period_*`）、设漂移（`input_drift_*`）；唯一的时间输出是 `ptp_td_sdo` 与 PPS（[ptp_td_phc.v:34-93](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_td_phc.v#L34-L93)）。参数 `PERIOD_NS_NUM=32 / PERIOD_NS_DENOM=5` 给出默认步长：

\[ T_{\text{step}} = \frac{\text{PERIOD\_NS\_NUM}}{\text{PERIOD\_NS\_DENOM}} = \frac{32}{5} = 6.4\ \text{ns}\;(156.25\ \text{MHz}) \]

整数部分进 `period_ns_reg`，小数部分进 32 位 `period_fns_reg`，而除不尽的余数交给 `drift_num/drift_denom` 周期性补足，使**平均速率严格等于 32/5 ns**（[ptp_td_phc.v:99-102](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_td_phc.v#L99-L102)）。这正是 u11-l1 里「漂移补偿」的同款机制。

**串行化。** 输出取移位寄存器的最低位，每拍整体右移 1 位、高位补 1（空闲态）：

```verilog
assign ptp_td_sdo = td_shift_reg[0];          // L173  LSB 先发
...
td_shift_reg <= {1'b1, td_shift_reg} >> 1;    // L511  每拍右移，空闲填 1
```

复位时移位寄存器全 1（[ptp_td_phc.v:142](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_td_phc.v#L142) 与 L633），所以线路上电为高电平（空闲）。

**消息格式（14 个 17 位字）。** 每条消息由 14 个「字」组成，每个字在移位寄存器里占 17 位：最低 1 位是帧位（数据字为 0，空闲为 1），高 16 位是数据（[ptp_td_phc.v:513-608](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_td_phc.v#L513-L608)）。数据接收方（leaf 或 Python 的 `PtpTdSink`）的逻辑是：看到线变 0 → 接着收 16 位 → 组成一个字；14 个字后线回到 1（空闲），一条消息结束。这与 `tb/ptp_td.py` 里 `PtpTdSink._run` 的解串逻辑逐位对应，是理解格式的最佳旁证。

| 字 | 内容 | 出处 |
| --- | --- | --- |
| word 0 | 控制：`msg_i[1:0]`、`rel_updated`（bit8）、`tod_s[0]`（bit9） | L514-520 |
| word 1-5（随 msg_i 变） | msg0=当前 ToD 纳秒+秒；msg1=偏移+漂移；msg2=**备用**偏移+秒 | L522-582 |
| word 6-7 | 当前共享小数纳秒 fns（32 位） | L584-589 |
| word 8-10 | 当前相对时间纳秒（48 位） | L590-598 |
| word 11-13 | 当前周期（period_fns 32 位 + period_ns 8 位） | L599-607 |

**三类消息轮转。** `td_msg_i_reg` 在每发完一条消息后 `0→1→2→0` 循环（[ptp_td_phc.v:542,561,580](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_td_phc.v#L542)）。也就是说 word 1-5 三条消息轮流携带不同内容，而 word 6-13（fns/相对 ns/周期）每条都重复。**为什么要备用偏移（msg2）**？因为偏移量 `tod_ns − rel_ns` 在秒翻转的瞬间会突变，单凭一个偏移无法判断相对时间当前落在「上一秒的尾巴」还是「这一秒的开头」。PHC 因此同时算出「当前秒偏移」与「相邻秒偏移」两个版本（状态 10-15，[ptp_td_phc.v:437-502](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_td_phc.v#L437-L502)），由接收方按需取用——这是 `ptp_td_rel2tod` 能消歧的根。

**PPS 生成。** PHC 自己也产 PPS（单周期 `output_pps` 与展宽 `output_pps_str`）。关键细节是它**预先把 PPS 延后一段固定拍数**再输出（`pps_delay_reg <= 14*17 + 32 + 240`，[ptp_td_phc.v:218-243](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_td_phc.v#L218-L243)），其中 `14*17` 正是一整条消息的比特长度——即把 PPS 对齐到「这条消息所代表的时间」真正到达 sdo 线的那一刻，使主从之间的 PPS 相位一致。

#### 4.1.4 代码实践

**实践目标**：跑通现成的 PHC testbench，亲眼看到「主时钟走的时间」与「串行解出的时间」一致。

1. 配好 cocotb + iverilog（见 u1-l4）。进入 `tb/ptp_td_phc`，运行 `make`（或 `pytest tb/ptp_td_phc`）。
2. 该 testbench 把 `dut.ptp_td_sdo` 接到一个 Python 参考模型 `PtpTdSink`（[tb/ptp_td_phc/test_ptp_td_phc.py:58-63](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/ptp_td_phc/test_ptp_td_phc.py#L58-L63)），它就是上文解串逻辑的 Python 实现。
3. 用例 `run_default_rate` 取一段仿真区间，对比「仿真时间增量」与「Sink 解出的 ToD/相对时间增量」，断言二者差小于 1e-3 ns（同文件 L120-135）。
4. **观察现象**：日志会打印 `sim time delta`、`ToD ts delta`、`Rel ts delta` 三行，三者应几乎相等。
5. **预期结果**：所有断言通过。Makefile 里 `VERILOG_SOURCES += ../../rtl/ptp_td_phc.v`（[Makefile:32](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/ptp_td_phc/Makefile#L32)）只编译了 PHC 这一个文件——leaf/rel2tod 都不在编译列表里，因为这条用例只测主时钟 + Python Sink。

若本地未配好工具链，结果「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：消息里 word 0 的 bit9 为什么放 `ts_tod_s_reg[0]`（秒的最低位）？它随后被谁用？

**答案**：秒的最低位每过一秒翻转一次，天然是一个「当前秒奇偶」标记。`ptp_td_rel2tod` 把它当作 `ts_sel` 选择信号（[ptp_td_rel2tod.v:223-225](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_td_rel2tod.v#L223-L225)），用来判断广播的「当前偏移」与「备用偏移」哪个对应此刻，从而在秒边界正确消歧。

**练习 2**：为什么 PHC 用一个共享加法器配 16 状态机，而不是多个独立加法器？

**答案**：资源与时序权衡。所有时间运算都是串行的「先算 fns、再算 rel、再算 tod、再算秒、再算偏移」依赖链，彼此本来就不能并行；复用一个加法器省面积，也让单条关键路径更短、更容易跑高频。

### 4.2 叶时钟重建（ptp_td_leaf）

#### 4.2.1 概念说明

`ptp_td_leaf` 是 PHC 的对端：它吃 `ptp_td_sdi` 串行流，在**目的时钟域**（`clk`）里重建出 96 位 ToD 与 64 位相对时间，并给出 PPS 与 `locked` 状态。

它的核心策略和 u11-l2 的 `ptp_clock_cdc` 一脉相承——**跨域不靠搬运、靠重建**：leaf 不去把 PHC 的时间「搬」过来，而是在本地重新生成本地自由时钟，再用闭环 DPLL 锁到从消息里解出的「源时间」。区别只在于：`ptp_clock_cdc` 的源是并行的 `input_ts` 总线；leaf 的源是从一根串行线上解出来的、每隔一整条消息（14×17≈238 比特）才更新一次的快照。正因如此，leaf 必须在两次快照之间自行「走时间」（用消息里的周期字段），并在快照到达时重新对齐。

#### 4.2.2 核心流程

leaf 跨越**三个时钟域**，每个域各司其职：

1. **ptp_clk 域（解串 + 重建源时钟）。** 把 `ptp_td_sdi` 移位解出 16 位字，组装回 14 字消息；同时用消息里的周期字段（word 11-13）驱动一个本地「源时钟」`src_ns_reg` 在两次消息之间匀速前进，消息到达时（`td_tlast`）按固定 `SYNC_DELAY` 延迟后用 `src_load` 把当前快照（word 6-10 的 fns/相对 ns）重载进来。

2. **sample_clk 域（鉴频鉴相）。** 一个独立的、通常较慢的采样时钟，用边沿检测器比较「源同步标志」与「目的同步标志」的相位先后，累加出相位误差样本——和 `ptp_clock_cdc` 完全同构。

3. **clk 域（DPLL 主环路 + 时间输出）。** 把 sample_clk 域送来的误差样本做带增益调度的 PI 控制，微调本地周期 `period_ns_reg`；同时据消息重载 ToD 秒/纳秒与偏移，按 `tod_ns = rel_ns_lsb + tod_offset_ns` 合成 ToD，并在跨过 10⁹ ns 时进秒、发 PPS。

#### 4.2.3 源码精读

**参数与三域接口。** `TS_REL_EN`/`TS_TOD_EN` 决定是否分别输出相对/ToD 时间（可裁剪），`TD_SDI_PIPELINE`（默认 2）允许在 `ptp_td_sdi` 上插入若干级寄存器以缓解长布线时序（[ptp_td_leaf.v:36-75](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_td_leaf.v#L36-L75)）。

**输入流水线与延迟补偿。** 串行输入先过 `TD_SDI_PIPELINE` 级寄存器（`SHREG_EXTRACT=no` 防止综合成 SRL，[ptp_td_leaf.v:108-130](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_td_leaf.v#L108-L130)）。关键在于：流水线带来的额外延迟会被 `SYNC_DELAY` 自动吃掉：

```verilog
localparam SYNC_DELAY = 32-2-TD_SDI_PIPELINE;   // L77
```

也就是说，消息到达后重载源时钟的等待拍数会随 `TD_SDI_PIPELINE` **减少**同样多拍，使「消息里那一时刻」与「重载生效那一刻」的相位关系保持不变。这就是规格里说的「流水线延迟补偿」：你把 sdi 打几拍寄存器，leaf 自动把同步点往前提相同拍数，重建出的时间相位不变。

**解串与跨域握手。** 解串器在 ptp_clk 域工作：看到 sdi=0 开始收 16 位，收齐 14 字为一条消息；每收完一条，`td_sync_reg` 翻转一次（[ptp_td_leaf.v:145-180](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_td_leaf.v#L145-L180)，翻转见 L164）。这个「每消息翻一次」的 toggle 是跨域握手载体：clk 域用三级同步器 + 异或边沿检测确认「新消息到了」，从而安全地把 16 位 `td_tdata` 取过来（[ptp_td_leaf.v:187-212](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_td_leaf.v#L187-L212)）。toggle 握手比直接同步多位总线安全得多——这也是 u11-l2 已确立的手法。

**源时钟重建。** 在 ptp_clk 域，`src_ns_reg` 按周期前进，`src_load` 到来时把消息里的快照（word 6-8 等）重载（[ptp_td_leaf.v:229-280](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_td_leaf.v#L229-L280)）。它还在每次 `src_load` 时翻转 `src_marker_reg`，作为「绝对对齐」基准。

**sample_clk 域鉴相。** 边沿检测器比较 src/dst 同步标志的先后，累加出 `sample_acc_out_reg`（[ptp_td_leaf.v:366-398](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_td_leaf.v#L366-L398)）。这是 DPLL 的误差来源。

**clk 域 PI 控制与增益调度。** 收到误差样本后，按误差大小自动切换高低增益（`casez`，[ptp_td_leaf.v:432-442](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_td_leaf.v#L432-L442)）：大误差用高增益快收敛、小误差用低增益稳态精。积分项 `dst_err_int_reg` 把固定跨域延迟吸收为步长偏置，使稳态误差归零——与 `ptp_clock_cdc` 同源。

**时间合成（本模块的精华之一）。** ToD 并不是从消息里直接抄来的整值，而是用「相对时间 + 偏移」本地合成。共享的小数纳秒与相对 ns 的低位在本地每拍累加周期：

```verilog
{ts_rel_ns_lsb_next, ts_fns_lsb_next} =
    ({ts_rel_ns_lsb_reg, ts_fns_lsb_reg} + period_ns_reg);   // L746
```

而 ToD 纳秒由相对 ns 低位 + 偏移合成，跨过 10⁹ ns 即进秒并产 PPS：

```verilog
ts_tod_ns_next[8:0] = ts_rel_ns_lsb_reg + ts_tod_offset_ns_reg;  // L788
if (!ts_tod_ns_next[8] && ts_tod_ns_reg[8]) begin                 // L794 检测进位
    if (ts_tod_ns_reg >> 9 == NS_PER_S-1 >> 9) begin
        ts_tod_s_next = ts_tod_s_reg + 1;  pps_next = 1'b1; ...
```

消息里 word 1-5（随 msg_i 变）被收进影子寄存器 `dst_tod_*_shadow`，在合适的同步点（`dst_update_reg && !dst_sync_reg && ... && dst_load_cnt_reg==0`）才提交，并做「期望值 vs 本地推算值」的一致性比对；连续多次不一致（`ts_*_mismatch_cnt` 计满）才强制重载，避免偶发毛刺污染时间（[ptp_td_leaf.v:757-832](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_td_leaf.v#L757-L832)）。

**三级锁定。** `locked` 是三个独立判据的与：

```verilog
assign locked = ptp_locked_reg && freq_locked_reg && dst_sync_locked_reg;   // L641
```

`dst_sync_locked`（DPLL 环锁定）、`freq_locked`（粗相位锁定，误差连续多窗落在 ±4 内）、`ptp_locked`（细时间锁定，PI 增益落到低档并保持）。三者都置位才表示「重建时间可信」。

#### 4.2.4 代码实践

**实践目标**：跑通 leaf testbench，观察 DPLL 从未锁定到锁定的过程。

1. 进入 `tb/ptp_td_leaf`，运行 `make`。该用例用一个 Python `PtpTdSource`（`tb/ptp_td.py`）模拟 PHC，把串行流灌进 `dut.ptp_td_sdi`。
2. 在波形或日志里盯住 `dut.locked`：它应在仿真开始若干毫秒后由 0 跳到 1。
3. 锁定后，对比 leaf 输出的 `output_ts_tod` 与 `PtpTdSource` 自身的 `get_ts_tod_ns()`，二者应高度吻合。
4. **改参数观察**：把 leaf 实例的 `TD_SDI_PIPELINE` 从 2 改为 0 或 4（在 testbench 例化处），重新跑。**预期**：`locked` 仍能置位、锁定后时间相位与改前一致——验证 `SYNC_DELAY = 32-2-TD_SDI_PIPELINE` 的补偿生效。
5. 若本地未配好工具链，结果「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：leaf 为什么需要 sample_clk？只用 clk 和 ptp_clk 两个域行不行？

**答案**：DPLL 需要一个独立于被锁两端的「裁判」来无偏地鉴频鉴相。若用 clk 自己当裁判，会偏向目的域；用 ptp_clk 则偏向源域。sample_clk 是第三个独立时钟，提供公正的相位比对，与 `ptp_clock_cdc` 的三域结构一致。

**练习 2**：消息每条要 14×17≈238 比特才到达一次，期间 leaf 怎么保证时间连续不跳变？

**答案**：ptp_clk 域的 `src_ns_reg` 用消息里的周期字段在两次消息之间自行匀速前进；clk 域同样用本地周期每拍累加 fns/ns。消息到达时只做「对齐/校正」（且要连续多次 mismatch 才强制重载），所以输出时间是平滑的，不会每条消息跳一下。

### 4.3 相对→ToD 还原（ptp_td_rel2tod）

#### 4.3.1 概念说明

`ptp_td_rel2tod` 是一个**比 leaf 轻得多**的还原器。场景是：你的 MAC（见 u11-l3）在 `tuser` 旁带里只给了你一个**截断的相对时间戳**（比如 48 位 = 32 位 ns + 16 位 fns），而你需要的是完整 96 位 ToD。跑一个完整的 leaf（含三域 DPLL）太重了——能不能直接「算」出来？

答案是肯定的，因为存在一个恒等关系：ToD 的纳秒部分与相对时间的纳秒部分**每拍前进量相同**，所以它们只差一个缓慢变化的偏移量 \(o = \text{tod\_ns} - \text{rel\_ns}\)。PHC 恰好把这个偏移量（以及小数纳秒）广播在消息里。于是：

\[ \text{tod\_ns} = (\text{rel} \gg W_{\text{fns}}) + o \]

这个模块就做这件事：它**旁路监听**同一条 `ptp_td_sdi`（不跑 DPLL），从消息里抠出偏移量与共享 fns，再把你手上那个截断相对时间戳加偏移、拼 fns，还原出 96 位 ToD。

#### 4.3.2 核心流程

1. **解串 + 跨域**：和 leaf 的解串器同源（16 位移位、toggle 握手、三级同步），从 `ptp_td_sdi` 取出消息字。
2. **维护两套偏移/秒寄存器**：把 msg1 的「当前偏移」与 msg2 的「备用偏移」分别存进 set 0 / set 1 两组寄存器，靠 word0 的 bit9（`ts_tod_s[0]`）作 `ts_sel` 决定哪组是「当前」。
3. **组合还原**：把截断相对时间戳的 ns 部分（高位）分别加两个偏移，得到两个候选 `tod_ns`，按高位选落在 `[0, 10^9)` 内的那个；fns 直接取自相对时间戳低位（共享 fns）。
4. **输出**：`{秒, 2'b00, tod_ns(30), fns(16)}` = 96 位 ToD，原样透传 `tag` 与 `valid`。

#### 4.3.3 源码精读

**接口**：输入是一个截断的相对时间戳 `input_ts_rel`（宽 `TS_REL_NS_W+TS_FNS_W`，默认 48）和旁带 `ptp_td_sdi`；输出是 96 位 `output_ts_tod`（[ptp_td_rel2tod.v:34-64](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_td_rel2tod.v#L34-L64)）。它也带 `TD_SDI_PIPELINE` 输入流水线，原理同 leaf。

**两套寄存器与选择**：用 `ts_sel_reg`（取自 word0 的 bit9，秒的最低位）在 set 0 / set 1 之间切换，把 msg1/msg2 的偏移与秒分别归位（[ptp_td_rel2tod.v:177-182, 223-297](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_td_rel2tod.v#L177-L182)）。因为秒的最低位每秒翻转，它天然标明了「当前偏移」属于哪个秒。

**还原与二选一消歧**（本模块灵魂）：

```verilog
ts_tod_ns_0 = (input_ts_rel >> TS_FNS_W) + ts_tod_offset_ns_0_reg;  // L200
ts_tod_ns_1 = (input_ts_rel >> TS_FNS_W) + ts_tod_offset_ns_1_reg;  // L201
// 选高位落在合法范围 [0, 1e9) 内者；都合法则优先 2 MSB 全 0 的那个
if (ts_tod_ns_0[30:29] == 0 || (ts_tod_ns_0[30]==0 && ts_tod_ns_1[30:29]!=0))
    ...选 set 0
else
    ...选 set 1                                                                // L208-214
output_ts_fns_next = input_ts_rel;   // L215  共享 fns 直接取相对时间戳低位
```

为什么需要二选一？考虑秒翻转附近：此刻相对时间戳对应的 ToD 纳秒可能落在「上一秒末尾（接近 10⁹）」或「这一秒开头（接近 0）」。「当前偏移」算出的值在边界一侧可能 > 10⁹（溢出）或为负（下溢），「备用偏移」算出的则在另一侧。代码用最高两位 `ts_tod_ns[30:29]` 判断：全 0 表示落在 0~536M 的安全下半区，直接取；否则取另一组。注释（L204-207）把这套区间判据写得清清楚楚。

**输出拼装**：秒（48）+ 2 位保留 + tod_ns（30）+ fns（16）= 96 位（[ptp_td_rel2tod.v:193](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ptp_td_rel2tod.v#L193)）。

整段还原是**纯组合逻辑**（`always @*`），无状态机、无 DPLL——这正是它「轻」的原因：只需从串行流里慢慢攒齐偏移/fns，之后每一个到达的相对时间戳都能即时算出 96 位 ToD。

#### 4.3.4 代码实践

**实践目标**：跑通 rel2tod testbench，验证「截断相对时间戳 + 广播」能精确还原 96 位 ToD，尤其穿越秒边界。

1. 进入 `tb/ptp_td_rel2tod`，运行 `make`。该用例用 `PtpTdSource` 灌串行流，并经 `cocotbext.axi.stream.define_stream` 定义一组 `input_ts`/`output_ts` AXI-Stream 接口（[tb/ptp_td_rel2tod/test_ptp_td_rel2tod.py:50-74](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/ptp_td_rel2tod/test_ptp_td_rel2tod.py#L50-L74)）。
2. 用例 `run_test` 枚举多组 `(start_rel, start_tod)`，刻意包含 `1234`/`1234.9` 等秒边界附近的相对时间，再叠加 `0`/`+0.05`/`−0.9` 秒偏移（同文件 L98-148）。
3. 对每组，它发送截断相对时间戳、收回 96 位 ToD，断言 `|还原 ToD − 期望 ToD| < 1e-3 ns` 且纳秒部分 `< 10⁹`（L145-149）。
4. **观察重点**：在 `−0.9` 秒偏移（跨秒回退）的用例里，二选一逻辑必须挑对秒——这正是 L208-214 消歧的用武之地。
5. 若本地未配好工具链，结果「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 fns 不需要从消息里取，而是直接用 `input_ts_rel` 的低位？

**答案**：因为 PHC 设计上让 ToD 与相对时间**共享同一份小数纳秒**（README 明确说明，且 PHC 在 word 6-7 只发一份 fns）。MAC 截断相对时间戳时已经带上了这 16 位 fns，所以还原 ToD 时直接复用即可，无需另从消息取——这正是「共享 fns」设计带来的节省。

**练习 2**：如果删掉「备用偏移」（只留当前偏移），在什么时刻还原会出错？

**答案**：在秒翻转前后约半秒的窗口里出错。当相对时间戳对应的 ToD 纳秒跨过 0 或 10⁹ 时，单一偏移会算出 > 10⁹ 或为负的非法值；备用偏移提供另一侧的值，配合 `ts_tod_ns[30:29]` 区间判据才能选回合法范围。规格里 `−0.9` 秒偏移用例就是为覆盖此场景。

## 5. 综合实践

把三个模块串成一条完整的「分发—重建—还原」链，验证端到端一致性。

**任务 A：跑通现成三件套。** 分别运行 `tb/ptp_td_phc`、`tb/ptp_td_leaf`、`tb/ptp_td_rel2tod`，确认全绿。它们分别覆盖「主时钟↔Python Sink」「Python Source↔leaf」「Python Source↔rel2tod」。

**任务 B（进阶，可上板/仿真）：PHC ↔ leaf 的 RTL 直连。** 仓库现有 testbench 都用 Python 模型充当对端，并没有一份把 `ptp_td_phc` 的 `ptp_td_sdo` 直接接到 `ptp_td_leaf` 的 `ptp_td_sdi` 的纯 RTL 闭环。请你仿照 `tb/ptp_td_phc/Makefile` 的写法，新建一份 testbench：

1. 例化 `ptp_td_phc`（同源 `clk`，例如 6.4 ns），把 `input_ts_tod_*` 设一个初值后撤销，让其自由走时间。
2. 例化 `ptp_td_leaf`，`ptp_clk` 接到与 PHC **同频但相位不同**的另一时钟（验证跨域），`clk` 用第三个时钟，`sample_clk` 用一个慢时钟；`ptp_td_sdi` 接 PHC 的 `ptp_td_sdo`。
3. 等待 `leaf.locked` 置 1。
4. 断言：稳态下 `leaf.output_ts_tod` 与 PHC 内部 `ts_tod_*_reg`（经 `output_pps` 对齐相位后）一致到亚纳秒。
5. 再把 leaf 的 `TD_SDI_PIPELINE` 在 0/2/4 间切换，验证锁定后时间相位不变。

**需要观察的现象**：未锁定时输出时间不可信（可能大幅偏离）；`locked` 跳 1 后输出应平滑跟踪 PHC。由于本任务需要你自行编写 testbench，具体数值「待本地验证」。

> 提示：若想快速得到一条可信链路，可复用 `tb/ptp_td.py` 的 `PtpTdSource`/`PtpTdSink` 作为「PHC 的软件替身」分别对接 leaf 与 phc，这能省去手写完整 PHC 激励，且与 RTL 位级对齐。

## 6. 本讲小结

- **PHC（`ptp_td_phc`）= 时钟 + 串行分发器合一**：自身自由走时间（步长 32/5=6.4 ns，漂移补齐余数），并把完整状态打包成 14 字 ×17 位的消息流从 `ptp_td_sdo` 广播；用单个共享加法器 + 16 状态机完成所有时间运算；同时算「当前偏移」与「备用偏移」两个版本以支持秒边界消歧。
- **消息格式**：LSB 先发、空闲为 1、起始位为 0；word 0 为控制字（含 `msg_i`、`rel_updated`、`tod_s[0]`），word 1-5 随三类消息轮转（ToD / 偏移+漂移 / 备用偏移），word 6-13 每条重复（共享 fns / 相对 ns / 周期）。
- **leaf（`ptp_td_leaf`）= 三域 DPLL 重建**：ptp_clk 解串+重建源时钟、sample_clk 鉴相、clk 做 PI 主环与时间合成；`locked = ptp_locked && freq_locked && dst_sync_locked`；`SYNC_DELAY = 32-2-TD_SDI_PIPELINE` 自动补偿输入流水线延迟。
- **rel2tod（`ptp_td_rel2tod`）= 轻量纯组合还原**：靠恒等式 `tod_ns = (rel>>fns_w) + offset`，用广播的偏移/秒（两套，由 `tod_s[0]` 选择）与共享 fns，把截断相对时间戳还原为 96 位 ToD；用 `tod_ns[30:29]` 区间判据在秒边界二选一消歧。
- **共享小数纳秒**是贯穿三模块的设计枢纽：ToD 与相对时间共用同一份 fns，使「从相对时间还原 ToD」成为可能，也使 MAC 的短相对时间戳足以恢复完整 ToD。

## 7. 下一步学习建议

- 下一讲 **u11-l5（`ptp_perout`）** 讲如何「消费」这些时间戳——基于 PTP 时间按绝对起始/周期/脉宽精确产出周期脉冲，正好用到本讲重建出的 ToD。
- 若关心「时间戳如何搭车到以太网帧」，回看 **u11-l3**：MAC 的 TX/RX 时间戳正是以相对格式走 `tuser` 旁带，而 `ptp_td_rel2tod` 就是把它们还原为 ToD 的配套件。
- 想理解 leaf/rel2tod 共用的「解串 + toggle 握手 + 三级同步」跨域模板，可对照 **u11-l2（`ptp_clock_cdc`）** 的 DPLL 与 Gray/toggle 跨域论述。
- 实战角度，阅读 `tb/ptp_td.py` 中 `PtpTdSource`/`PtpTdSink` 的 Python 实现——它与 RTL 位级等价，是把本讲三模块当成「协议」来读的最佳参考实现。
