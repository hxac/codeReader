# 五级流水线数据流串讲

## 1. 本讲目标

前面三讲（u3-l1~u3-l4）我们已经把每一块"零件"拆开看过了：顶层 `FFT.v`、蝶形单元 `radix2.v`、延时线 `shift_N`、状态/旋转因子 `ROM_N`。本讲要做的事情恰好相反——**把这些零件装回一台完整的机器**，回答一个核心问题：

> 一个 32 点样本，从 `din_r/din_i` 进入，到第 5 级 `out_r/out_i` 输出，中间到底走了哪条路？各级之间靠什么信号"对齐拍子"？

学完本讲你应当能够：

1. 画出 5 级流水线的级联拓扑，并指出每一级的「蝶形 + 移位 + ROM」反馈回路在哪里闭合。
2. 说清 `in_valid` 如何逐级演化为 `radix_no1_outvalid → radix_no2_outvalid → …`，构成驱动整条流水线的 valid 菊花链。
3. 解释第 5 级为什么没有 ROM、为什么旋转因子被写死成 `256+j0`，以及它带来的数值后果。
4. 估算这条流水线的吞吐与填充时延，并与 testbench 的 68 拍看门狗对应起来。

## 2. 前置知识

本讲默认你已经读过 u3 的四篇讲义。如果有些术语已经模糊，先回忆下面几条：

- **radix2 蝶形单元（PE）是纯组合逻辑、自身无状态**。它靠外部送来的 2 位 `state` 信号分时复用：`2'b00` 等待、`2'b01` first half（算"和"与"差"）、`2'b10` second half（把"差"乘旋转因子）。进入 `01/10` 时 `outvalid` 拉高（见 [radix2.v:L37-L72](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L37-L72)）。
- **shift_N 是 N 拍 FIFO 延时线**，深度逐级减半 `16/8/4/2/1`（见 [shift_1.v:L31-L53](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/shift_1.v#L31-L53)）。
- **ROM_N 身兼两职**：用计数器分段生成 `state` 驱动蝶形三态机，同时用 `case` 查表在 second half 输出旋转因子。
- **顶层端口**：12 位有符号输入、16 位有符号输出，内部 24 位数据通路（×256 定点尺度）。

一个关键认知要带着往下读：**radix2 没有时钟，也没有 `valid` 输入**。它什么时候吐有效数据，完全由 ROM/顶层喂给它的 `state` 决定；而它吐出的 `outvalid`，又会变成下一级的"发车信号"。这条"由 state 控制、由 outvalid 传递"的链，就是本讲的主线。

## 3. 本讲源码地图

| 文件 | 在本讲扮演的角色 |
| --- | --- |
| [RTL/FFT.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v) | **接线图**。5 级实例化、valid 连线、第 5 级 `no5_state` 生成都在这里。 |
| [RTL/radix2.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v) | **流水线的运算节点**。本讲只看它的端口契约（`din_a/din_b/op/delay/outvalid/state`），不再重复算法细节。 |
| [SIM/FFT_tb.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v) | **时延判据来源**。`latency_limit = 68` 给出流水线填充时延的上限参考。 |

## 4. 核心概念与源码讲解

### 4.1 级联拓扑与反馈回路

#### 4.1.1 概念说明

SDC（Single-path Delay Commutator，单路延迟换向器）流水线的精髓是：**只用一条数据通路、一套运算单元，靠"延时线 + 状态分时"把 32 点 FFT 的 5 级蝶形折叠进 5 个级联的处理元里**。

每一级（第 1~4 级）都是同一个"三件套"：

- 1 个 `radix2` 蝶形单元——做运算；
- 1 条 `shift_N` 延时线——把 first half 算出的"差"延迟 N 拍后送回蝶形的 `din_a`；
- 1 个 `ROM_N`——给蝶形送旋转因子 + `state`。

三者构成一个**反馈回路**：蝶形的 `delay`（差分支）→ shift 延时 → 蝶形的 `din_a`。N 拍之后，当初算出的"差"会从 shift 出口回流，正好赶上蝶形进入 second half 去乘旋转因子。这就是 SDC 用一条路实现"先存后用"的办法。

第 5 级是简化版：只有 `radix2 + shift_1`，**没有 ROM**——原因下一节（4.3）讲。

#### 4.1.2 核心流程

一条样本从输入到第 5 级输出的主路径可以概括为：

```text
din_r/din_i (12bit 有符号)
   │  寄存 + 符号扩展 + 左移8位 (= ×256)
   ▼
din_r_reg/din_i_reg (24bit) ──► radix_no1.din_b
                                   │ op1 (和分支, 直通)
                                   ▼
                                radix_no2.din_b ──op2──► radix_no3.din_b ──op3──► radix_no4.din_b ──op4──► radix_no5.din_b
                                                                                                                  │
                                                                                                                  ▼ out_r/out_i (24bit)
                                                                                                                  │ [23:8] 截位 = ÷256
                                                                                                                  ▼ 16bit → SORT 排序
```

每一级内部还有一个"支路"在并行工作——差分支走的是延时回路：

```text
                ┌─────────── radix_non ───────────┐
   din_b ──────▶│                          op ────┼──▶ 下一级 din_b（主路径，和分支/二半结果）
                │   ▲                      │
                │   │ din_a                │ delay（差分支）
                │   │                      ▼
                │   └──── dout ◀──── shift_N ◀── delay（延时 N 拍后回流为 din_a）
                │
                └── state, w_r/w_i ◀── ROM_N（第5级改为常数 + 顶层 no5_state）
```

把上面两段拼起来，第 1 级到第 5 级的完整级联如下（细节端口名见 4.1.3）：

```text
din ──×256──▶ radix_no1 ──op1──▶ radix_no2 ──op2──▶ radix_no3 ──op3──▶ radix_no4 ──op4──▶ radix_no5 ──out
              │  ▲ delay1         │  ▲ delay2         │  ▲ delay3         │  ▲ delay4         │  ▲
              │  └─shift_16       │  └─shift_8        │  └─shift_4        │  └─shift_2        │  └─shift_1
              │       (16拍)      │       (8拍)        │       (4拍)       │       (2拍)       │    (1拍)
              └ ROM_16            └ ROM_8             └ ROM_4            └ ROM_2             └ w=256+0j, no5_state
```

注意延时深度 `16/8/4/2/1` 正好逐级减半，等于 radix-2 DIF 各级的蝶形配对距离（这点 u3-l3、u3-l4 已确认）。

#### 4.1.3 源码精读

**输入端的对齐。** 输入先打一拍寄存器，同时完成 12→24 位、×256 的定点对齐：

[FFT.v:L273-L275](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L273-L275) —— `{{4{din_r[11]}},din_r,8'b0}` 即"4 位符号扩展 + 低 8 位补零"，等价于把 12 位有符号数放大 256 倍放进 24 位通路；`in_valid` 也同步打一拍成 `in_valid_reg`，给第 1 级当发车信号。

**第 1 级三件套。** 这是整套拓扑的"模板"，看懂它就懂了后三级：

[FFT.v:L95-L126](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L95-L126) —— `radix_no1`、`shift_16`、`ROM_16` 三个实例。三处连线读法：
- 蝶形 `din_b ← din_r_wire`（新输入），`din_a ← shift_16_dout`（延时反馈），`op → radix_no1_op`（送往第 2 级），`delay → radix_no1_delay`（送进 shift_16）。
- `shift_16` 的 `din ← radix_no1_delay`，`dout → shift_16_dout`——这就是 4.1.1 说的反馈回路闭合点。
- `ROM_16` 给蝶形送 `w_r/w_i` 和 `state`。

**第 2~4 级只是"错位复制"。** 以第 2 级为例：

[FFT.v:L128-L159](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L128-L159) —— `radix_no2.din_b ← radix_no1_op`（接上一级 op），`din_a ← shift_8_dout`，`delay → shift_8`；`shift_8/ROM_8` 各就各位。第 3、4 级（[L162-L227](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L162-L227)）结构与第 2 级完全同构，只是 shift/ROM 的数字后缀递减（`8→4→2`）。

**第 5 级把 op 直接顶到模块输出。**

[FFT.v:L230-L252](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L230-L252) —— `radix_no5.op_r/op_i → out_r/out_i`（24 位 wire），再配合 `shift_1` 的反馈回路。注意这里旋转因子和 state 都不是来自 ROM，详见 4.3。

#### 4.1.4 代码实践

**实践目标**：把 `FFT.v` 的实例化区翻译成一张可读的数据流框图，确认"主路径串行级联、支路逐级反馈"两个结构特征。

**操作步骤**：

1. 打开 [FFT.v:L95-L252](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L95-L252)，逐个实例抄下"端口 ← 连线"对应关系。
2. 用三种颜色（或三种线型）分别画：① 主数据路径 `din → op1 → op2 → … → out`；② 反馈支路 `delay_N → shift_N → din_a`；③ 控制路径 `ROM_N/顶层 → state/w`。
3. 在每个反馈支路上标注 shift 的延时拍数（16/8/4/2/1）。

**需要观察的现象**：

- 五个 `radix2` 实例沿主路径一字排开，`op` 接下一级 `din_b`。
- 每个 `shift_N` 的输入端接的是**同级**蝶形的 `delay`，输出端接的是**同级**蝶形的 `din_a`——反馈回路完全在级内闭合，不跨级。

**预期结果**：得到一张与本节 4.1.2 几乎一致的框图；能指出"第 5 级缺一个 ROM 框、`outvalid` 端口空接"两处例外（这两处留到 4.2、4.3 解释）。

> 本实践为"源码阅读型实践"，不依赖仿真器即可完成；行号经源码核对。

#### 4.1.5 小练习与答案

**练习 1**：如果把第 2 级的 `shift_8` 误连成接第 1 级的 `radix_no1_delay`（而不是第 2 级的 `radix_no2_delay`），会发生什么？

**参考答案**：反馈回路被接到错误的级上。第 2 级蝶形 `din_a` 收到的将是第 1 级算出的"差"，而不是自己 first half 算出的"差"，second half 的乘旋转因子就乘错了对象，整条 FFT 结果会全错。这正是为什么反馈回路必须严格"级内闭合"。

**练习 2**：为什么延时深度是 `16/8/4/2/1` 递减，而不是 5 级都一样？

**参考答案**：每级的延时深度 = 该级蝶形的配对距离 = radix-2 DIF 该级的"组大小/2"。第 1 级 32 点分成 1 组、配对距离 16；第 2 级组大小 16、距离 8……逐级减半。延时深度跟着组大小走，所以递减（详见 u2-l1、u3-l3）。

---

### 4.2 valid 菊花链传递

#### 4.2.1 概念说明

5 级流水线里，`radix2` 是组合逻辑、不会自己"知道"数据到了没有；`shift_N` 和 `ROM_N` 是时序逻辑、需要一个使能信号决定何时开始计数/移位。这个使能信号就是 `in_valid`。

由于数据是一级一级往后流的，**使能信号也必须一级一级地往后传**——上一级算出有效结果（`outvalid` 拉高）了，下一级的 shift/ROM 才该启动。这条"上一级 outvalid → 下一级 in_valid"的串联，就叫 **valid 菊花链**（daisy chain）。它让流水线在没有数据时保持安静，有数据时逐级点亮。

#### 4.2.2 核心流程

菊花链的"接力棒"传递顺序：

```text
in_valid ──(打一拍)──▶ in_valid_reg ──▶ shift_16 / ROM_16          （启动第1级）
                         ↑
                  radix_no1 产出 radix_no1_outvalid ──▶ shift_8 / ROM_8     （启动第2级）
                                                           ↑
                                                    radix_no2_outvalid ──▶ shift_4 / ROM_4   （第3级）
                                                                                  ↑
                                                                           radix_no3_outvalid ──▶ shift_2 / ROM_2  （第4级）
                                                                                                         ↑
                                                                                                  radix_no4_outvalid ──▶ shift_1 + r4_valid（第5级）
```

要点：

1. 第 1 级由外部 `in_valid`（经 `in_valid_reg` 打一拍对齐）启动。
2. 第 2~4 级的 shift/ROM 全都由**上一级** `radix_noX_outvalid` 启动。
3. 第 5 级没有 ROM，`radix_no4_outvalid` 一路同时送给 `shift_1.in_valid` 和顶层 `r4_valid`（后者用于生成 `no5_state`，见 4.3）。
4. 注意 `outvalid` **不进 shift/ROM 的数据通路**，它纯粹是控制信号——这点容易看走眼。

#### 4.2.3 源码精读

**起点：`in_valid` 打拍对齐第 1 级。**

[FFT.v:L275](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L275) —— `in_valid_reg <= in_valid;` 把外部握手打一拍，与 `din_r_reg` 同节拍，避免组合冒险。

**第 1 级的 shift/ROM 用 `in_valid_reg` 启动。**

[FFT.v:L110-L126](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L110-L126) —— `shift_16.in_valid ← in_valid_reg`、`ROM_16.in_valid ← in_valid_reg`。

**接力：第 1 级 outvalid 启动第 2 级。**

[FFT.v:L143-L159](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L143-L159) —— `shift_8.in_valid ← radix_no1_outvalid`、`ROM_8.in_valid ← radix_no1_outvalid`。这就是菊花链的第一棒交接。

**整条链一气呵成。**

[FFT.v:L177-L227](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L177-L227) —— `shift_4/ROM_4.in_valid ← radix_no2_outvalid`、`shift_2/ROM_2.in_valid ← radix_no3_outvalid`。

**终点：第 5 级 + 顶层状态生成。**

[FFT.v:L245-L252](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L245-L252) —— `shift_1.in_valid ← radix_no4_outvalid`。

[FFT.v:L292](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L292) —— `next_r4_valid = radix_no4_outvalid;`，第 4 级 outvalid 在这里被"采集"成 `r4_valid`，供 4.3 的 `no5_state` 使用。

**outvalid 是怎么被拉高的？**

[radix2.v:L42-L71](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L42-L71) —— 只有 `state==2'b01`（first half）或 `state==2'b10`（second half）时 `outvalid=1`，`2'b00`/`2'b11` 时为 0。也就是说，**蝶形吐 outvalid 的节奏，完全由 ROM 喂给它的 state 节奏决定**，而 ROM 又由上一级 outvalid 启动——因果环环相扣。

#### 4.2.4 代码实践

**实践目标**：在仿真波形里"看见"菊花链逐级点亮的过程。

**操作步骤**：

1. 在仿真器（QuestaSim/ModelSim/Verilator 等）里把 `RTL/FFT.v` 与 `SIM/FFT_tb.v` 编译在一起并跑一组数据集。
2. 把下列信号加入波形窗口并按从上到下排序：`in_valid`、`in_valid_reg`、`radix_no1_outvalid`、`radix_no2_outvalid`、`radix_no3_outvalid`、`radix_no4_outvalid`、`out_valid`。
3. 在 `in_valid` 拉高后，用光标测量每个 outvalid 相对外部 `in_valid` 的上升时刻差。

**需要观察的现象**：

- 5 个 outvalid 应像"多米诺骨牌"一样**依次**拉高，且彼此间距相对稳定，没有两个同时跳变。
- `in_valid` 一旦撤销，各级 outvalid 会在残留数据冲刷完后依次回落。

**预期结果**：得到一张阶梯状展开的 valid 时序图；若某级 outvalid 提前或滞后很多，说明对应级的 ROM 等待/前半/后半阈值（u3-l4）或 shift 延时（u3-l3）配置有问题。

> 本实践依赖仿真器；若本地暂无环境，可改为"阅读型实践"——在 [FFT.v:L110-L252](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L110-L252) 里手工标注每个 `in_valid(...)` 端口接的是哪一级的 outvalid，验证与本节 4.2.2 的链一致。具体波形时刻为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `radix2` 没有 `in_valid` 输入端口，却仍然能产出 `outvalid`？

**参考答案**：`radix2` 是纯组合逻辑，它的"何时有效"由外部 `state` 决定。当 `state` 进入 `01/10`，组合逻辑自然算出有效结果并把 `outvalid` 拉高。真正掌握时序节奏的是 ROM（它数拍子生成 state），而 ROM 的启动又靠上一级 outvalid——所以 valid 的"源头"是 ROM，不是蝶形本身。

**练习 2**：第 5 级 `radix_no5` 的 `outvalid` 端口写成 `.outvalid()`（空接）。那第 5 级"算完了"的信号从哪里取？

**参考答案**：从 `radix_no4_outvalid` 经 `r4_valid` 间接得到（[FFT.v:L292](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L292)）。第 5 级的 state 由顶层用 `r4_valid` + `s5_count` 自己生成（见 4.3），所以它的 outvalid 不再需要外接。

---

### 4.3 第 5 级固定旋转因子

#### 4.3.1 概念说明

第 5 级是整条流水线最"省事"的一级——**它没有 ROM**。原因来自 radix-2 DIF 的数学性质：

在 32 点 FFT 的第 5 级（最后一级），蝶形配对距离为 1，需要的旋转因子只有 \(W_2^0\) 这一种，而

\[
W_2^0 = e^{-j\,2\pi\cdot 0/2} = e^{0} = 1 + j0.
\]

也就是说，**最后一级的旋转因子恒为 1**。既然是常数，就没必要用一个 ROM 去查表，直接在实例化时把 `w_r/w_i` 写死即可；同时把原本由 ROM 生成的 `state` 改由顶层几行组合逻辑产生。

回忆 u2-l2：旋转因子在硬件里用 ×256 的定点表示，所以 `1.0` 对应 `256`。于是第 5 级的旋转因子被写成 `w_r = 24'd256, w_i = 24'd0`，即"定点尺度下的 \(1+j0\)"。

#### 4.3.2 核心流程

**数值后果**：把 `w_r=256, w_i=0` 代入 `radix2` 的 second half 公式（u3-l2 的 3 乘 5 加），结果会退化。设 second half 输入为 `a=din_a_r`（实部）、`b=din_a_i`（虚部）：

\[
\begin{aligned}
\text{inter} &= b\,(w_r - w_i) = b(256 - 0) = 256\,b,\\
\text{mul\_r} &= w_r\,(a-b) + \text{inter} = 256(a-b) + 256b = 256\,a,\\
\text{mul\_i} &= w_i\,(a+b) + \text{inter} = 0 + 256b = 256\,b.
\end{aligned}
\]

再取 `op = mul[31:8]`（右移 8 位 = 除以 256），得

\[
\text{op\_r} = a,\qquad \text{op\_i} = b.
\]

也就是说，**第 5 级 second half 退化成"恒等"——输出就等于反馈回来的差分支 `din_a`**。这与"乘以 1"完全吻合：最后一级只做蝶形的加减，不再旋转。

**state 的替代生成**：因为没有 ROM，第 5 级的 `state` 由顶层组合逻辑 `no5_state` 产生。它以 `r4_valid`（第 4 级 outvalid 的采集）为触发，用一个 1 位计数器 `s5_count` 数两拍，依次输出 `01`（first half）、`10`（second half），完整复刻 ROM 的"等待→前半→后半"节奏。

#### 4.3.3 源码精读

**第 5 级实例：旋转因子写死、state 来自顶层、outvalid 空接。**

[FFT.v:L230-L243](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L230-L243) —— 关键三行：`.w_r(24'd256)`、`.w_i(24'd0)`、`.state(no5_state)`、`.outvalid()`。注意 `.op_r(out_r)` 把第 5 级结果直接顶到模块级 wire `out_r/out_i`。

**`no5_state` 的生成逻辑。**

[FFT.v:L290-L298](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L290-L298) —— 这段组合逻辑的读法：
- `next_r4_valid = radix_no4_outvalid`：采集第 4 级 outvalid；
- `r4_valid` 有效时 `s5_count` 递增，否则保持；
- `s5_count==0` 时 `no5_state=01`（first half），`s5_count==1` 时 `no5_state=10`（second half），其余为 `00`（等待）。

这正是把一个"等待 1 拍 + 前半 1 拍 + 后半 1 拍"的最简状态机手写出来的样子，等价于一个退化到极致的 `ROM_2`。

**second half 的退化在 radix2 里发生。**

[radix2.v:L58-L72](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L58-L72) —— `inter/mul_r/mul_i` 三式。把 `w_r=256,w_i=0` 代入（如 4.3.2 推导），可得 `op_r=din_a_r, op_i=din_a_i`，验证恒等退化。

#### 4.3.4 代码实践

**实践目标**：用纸笔验证第 5 级 second half 的"恒等退化"，理解常数旋转因子为什么不会引入误差。

**操作步骤**：

1. 任取一组 24 位有符号数作为 `din_a_r=a`、`din_a_i=b`（例如 `a=100, b=-50`）。
2. 按 [radix2.v:L65-L67](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L65-L67) 的公式，用 `w_r=256, w_i=0` 手算 `inter`、`mul_r`、`mul_i`。
3. 对 `mul_r/mul_i` 取 `[31:8]`（即除以 256），看是否等于 `a`、`b`。

**需要观察的现象**：

- 经过乘法和截位后，结果恰好等于输入 `a/b`，没有数值损失。
- 这说明第 5 级省掉 ROM **不会牺牲精度**，纯粹是面积/功耗的净收益。

**预期结果**：`op_r = a`、`op_i = b`，恒等成立。若不等，多半是截位或符号位算错——注意 `mul` 是有符号乘、`[31:8]` 取的是高位窗口。

> 本实践为纸笔推导，无需仿真器即可完成验证。

#### 4.3.5 小练习与答案

**练习 1**：如果把第 5 级的旋转因子错写成 `w_r=24'd256, w_i=24'd256`（即 \(1+j\)），second half 输出会变成什么？

**参考答案**：代入 `inter = b(256-256)=0`，`mul_r = 256(a-b)+0 = 256(a-b)`，`mul_i = 256(a+b)+0 = 256(a+b)`，截位后 `op_r = a-b, op_i = a+b`。这不再是恒等，相当于乘了 \(1+j\)，FFT 结果会全错。可见把 `w_i` 写成 0 是正确性的关键。

**练习 2**：既然第 5 级旋转因子是常数，为什么第 1~4 级不能也省掉 ROM？

**参考答案**：第 1~4 级每级都需要**多种**旋转因子（第 1 级 16 个、第 2 级 8 个……见 u3-l4），必须用 ROM 查表；只有第 5 级退化成单一旋转因子 \(W_2^0=1\)，才具备写死成常数的条件。

---

### 4.4 流水线吞吐与时延

#### 4.4.1 概念说明

评估一条流水线有两个核心指标：

- **吞吐（throughput）**：稳定后每拍能处理多少样本。SDC 流水线的卖点是 **100% 硬件利用率**——每个处理元在有效数据流过时都在干活，没有空转，所以稳态吞吐 = 1 样本/拍。
- **时延（latency）**：从第一个样本进入到第一个结果出来，需要多少拍。这是"填充"流水线的代价。

#### 4.4.2 核心流程

**吞吐**：输入端连续 32 拍喂入 32 个样本（见 testbench 的 `for(j=0;j<32)` 喂数循环），输出端在填充完成后也连续 32 拍吐出 32 个频域样本。稳态下每个时钟周期进出各一个样本——这就是 100% 利用率的来源。

**时延**：填充时延由两部分累积——

\[
T_{\text{fill}} \approx \sum_{N\in\{16,8,4,2,1\}} (\text{shift 延时 } N) + (\text{各级蝶形 first/second half 处理拍数}) + (\text{输入/输出寄存器节拍}).
\]

延时线深度之和 \(16+8+4+2+1 = 31\)，是填充时延的主要来源。testbench 用 `latency_limit = 68`（[FFT_tb.v:L14](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L14)）作为看门狗上限：从 `in_valid` 撤销到 `out_valid` 首次拉高，以及后续每组输出之间的间隔，都不应超过 68 拍，否则判失败。

需要强调：68 是**上限阈值**，不是设计的真实时延。真实时延需在仿真里测量（见实践），本讲记为「待本地验证」。

#### 4.4.3 源码精读

**吞吐的来源：连续喂入 + 连续读出。**

[FFT_tb.v:L95-L116](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L95-L116) —— `for(j=0;j<32)` 连续 32 拍 `in_valid=1` 喂入，之后撤销 `in_valid`，然后 `while(!out_valid)` 等待第一个结果。这段既是吞吐的体现（连续进出），也是填充时延的测量窗口。

**时延上限：68 拍看门狗。**

[FFT_tb.v:L109-L116](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L109-L116) —— `latency > latency_limit` 即 `$finish` 判失败。

**填充时延的结构来源：延时线深度之和。**

[FFT.v:L110-L252](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L110-L252) —— 五条 shift 的深度 `16/8/4/2/1` 累加 = 31，构成填充时延的主体；加上各级蝶形 first half→second half 的处理拍数和输入/输出寄存节拍，总填充时延落在 68 拍以内即合格。

#### 4.4.4 代码实践

**实践目标**：实测本设计的填充时延，并验证它落在 68 拍看门狗之内。

**操作步骤**：

1. 编译并运行 `SIM/FFT_tb.v`（方法见 u1-l3）。
2. 关注仿真日志里 testbench 自己打印的两类信息：等待首拍 `out_valid` 的 `latency` 计数，以及最终 `Average latency = ... cycles`。
3. 把测得的平均时延填入下表。

| 数据集 | 首拍填充时延（拍） | 是否 < 68 |
| --- | --- | --- |
| 01 | | |
| 02~05 | | |
| 平均 | | |

**需要观察的现象**：

- 所有数据集的首拍时延都 < 68，否则 testbench 会 `$finish` 并报 "Latency too long"。
- 平均时延是稳定值，不随数据内容变化（流水线时延只取决于结构，与数据无关）。

**预期结果**：得到一个固定的平均时延数值（「待本地验证」），它 = 延时线深度之和（31）+ 各级处理拍数 + 寄存节拍，整体远小于 68。

> 本实践依赖仿真器与可读的目录结构（testbench 用 `../Test_pattern/...` 相对路径，需先把 `SIM/Test_cases/` 摆成期望布局，详见 u1-l3 的提醒）。

#### 4.4.5 小练习与答案

**练习 1**：如果把设计从 32 点扩展到 64 点（u7-l3 会详细讨论），填充时延大致会变成多少？看门狗 68 还够用吗？

**参考答案**：64 点有 6 级，延时线深度变为 `32/16/8/4/2/1`，之和 = 63（比 32 点的 31 翻倍）。再加上各级处理拍数，填充时延会明显超过 68，所以扩展时必须同步把 `latency_limit` 调大（如调到 100+），否则 testbench 会误判失败。

**练习 2**：为什么稳态吞吐能达到 1 样本/拍，而填充时延却有几十拍？这两者矛盾吗？

**参考答案**：不矛盾。吞吐看的是"稳态"——流水线填满后每拍进一个出一个；时延看的是"启动"——第一次填满需要的时间。就像水管：接通后水流连续不断（高吞吐），但第一滴水从龙头流到出口需要时间（时延）。SDC 架构的优点正是稳态吞吐满载（100% 利用率），代价是要承受一次性的填充时延。

## 5. 综合实践

把本讲四个模块串起来，完成一张"全景数据流 + 控制流"分析图，并配一段文字说明：

1. **画图**：在一张图里同时画出
   - 5 级蝶形的主路径 `din → op1 → … → out`（4.1）；
   - 每级 `delay → shift_N → din_a` 的反馈回路，标注深度 `16/8/4/2/1`（4.1）；
   - valid 菊花链 `in_valid_reg → radix_no1_outvalid → … → radix_no4_outvalid → r4_valid`（4.2），用虚线区别于数据线；
   - 第 5 级的两处特殊：`w=256+0j` 常数、`state=no5_state`（4.3）。
2. **标注**：在第 5 级旁注明 second half 退化为恒等（`op=din_a`）；在延时线旁注明深度之和 = 31。
3. **文字**：写一段不超过 150 字的说明，解释"一个样本要走过哪些路径、各级靠什么信号同步、为什么第 5 级可以省 ROM、稳态吞吐与时延分别是多少"。

完成后，这张图就是你理解整颗 FFT 处理器数据流的"一张图总览"，也是进入 u4-l2（输出排序）和 u4-l3（控制时序）的基础。

## 6. 本讲小结

- **级联拓扑**：5 个 `radix2` 沿主路径 `din→op1→…→out` 串联；每级（1~4）的 `radix2 + shift_N + ROM_N` 构成级内闭合的反馈回路 `delay → shift_N → din_a`，延时深度 `16/8/4/2` 逐级减半。
- **valid 菊花链**：外部 `in_valid`（打拍成 `in_valid_reg`）启动第 1 级；之后每级 shift/ROM 的 `in_valid` 都接**上一级** `radix_noX_outvalid`，第 5 级由 `radix_no4_outvalid` 经 `r4_valid` 启动。
- **第 5 级特例**：无 ROM，旋转因子写死 `w_r=256,w_i=0`（即定点 \(1+j0\)），second half 退化为恒等；`state` 改由顶层 `no5_state`（`r4_valid` + `s5_count`）生成，`outvalid` 空接。
- **吞吐与时延**：稳态吞吐 1 样本/拍（100% 利用率）；填充时延主要来自延时线深度之和 31，testbench 用 68 拍看门狗兜底。
- **核心心智模型**：`radix2` 是被 `state` 驱动的组合运算节点，`outvalid` 既是它"算完了"的标志，又是下一级的发车信号——这一因果环就是整条流水线的节拍来源。

## 7. 下一步学习建议

本讲只追踪到 `out_r/out_i`（第 5 级 op），还没有解释这些结果如何变成最终顺序的 `dout_r/dout_i`。接下来：

- **u4-l2 输出排序模块：硬件位反转还原**：精读 `FFT.v` 里那段大型 `case(y_1)`（SORT 模块），看它如何把第 5 级乱序输出按位反转写入 `result_r[0:31]`，并在 32 拍后顺序读出。这与本讲的 `out_r/out_i` 直接衔接。
- **u4-l3 控制时序与握手信号**：聚焦 `FFT.v` 的两个 `always` 块，把本讲提到的 `no5_state`、`count_y`、`out_valid`、`over` 等控制信号的生成时序讲透。
- **回看建议**：如果对某级 `radix2` 内部运算仍有疑问，回到 u3-l2；对 shift/ROM 的实现细节有疑问，回到 u3-l3、u3-l4。本讲假定这些"零件级"知识已经掌握。
