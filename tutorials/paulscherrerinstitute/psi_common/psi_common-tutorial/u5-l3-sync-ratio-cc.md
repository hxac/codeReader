# 同步整数比跨越 sync_cc_n2xn / sync_cc_xn2n

## 1. 本讲目标

学完本讲，读者应能够：

- 说清「同步（整数比）时钟域」与「异步时钟域」的本质区别，以及为什么前者可以省掉同步器、格雷码和手写时序约束。
- 根据「数据从低频走向高频」还是「从高频走向低频」，在 `sync_cc_n2xn` 与 `sync_cc_xn2n` 之间正确选型。
- 读懂这两个组件用「双计数器 + 跨域直接采样 + AXI-S 握手」实现的 2 级深度缓冲机制，并能解释 `InCnt - OutCnt` 在 2 位无符号运算下的回绕。
- 解释 `xn2n` 中 `InDataReg` / `InDataRegLast` 双寄存器为何能保证样本顺序，以及 ratio 为何不是 DUT 的 generic。
- 给定两个同步时钟（如 100 MHz 与 25 MHz），预测吞吐率上限、反压方向与 `vld_o` 的脉冲形态。

## 2. 前置知识

本讲是 CDC（Clock Domain Crossing，时钟域跨越）单元的第三篇，承接以下已有认知：

- **AXI-S 握手（u1-l4）**：传输只在 `VLD` 与 `RDY` 同为高的那一拍发生；源端一旦拉高 `VLD`，握手完成前不得撤回；宿端可以自由进出 `RDY`（反压）。本讲的 `vld_i/rdy_o`（输入侧）与 `vld_o/rdy_i`（输出侧）正是这套语义。
- **异步 CDC（u5-l1、u5-l2）**：`pulse_cc` 用「翻转 + 3 级同步器 + 异或」，`simple_cc/status_cc` 要么用同步器要么用请求-应答回环，`bit_cc` 自带双级同步器并贴 `ASYNC_REG`。它们面向的是**没有固定相位关系**的异步时钟。
- **异步 FIFO（u4-l2）**：跨域传递读写指针必须用「格雷码 + 两级同步器」，并需要 `ASYNC_REG` 属性与 `set_max_delay` 约束。
- **math_pkg（u2-l1）**：`is_int_ratio(a, b)` 判定两频率是否成整数倍——这正是本讲组件可用的**前提条件**。

本讲要回答的核心问题是：**如果两个时钟本来就来自同一个 PLL、且频率成整数比，CDC 还需要那么复杂吗？** 答案是：不需要。这正是 `sync_cc_n2xn` / `sync_cc_xn2n` 存在的理由。

> 名词速查
> - **同步时钟（synchronous clocks）**：由同一个 PLL/MMCM 生成、相位关系确定、频率成整数比的时钟。慢时钟的每个上升沿都与某个快时钟上升沿**重合**。
> - **整数比**：设 \( f_{\text{high}} = r \cdot f_{\text{low}} \)，其中 \( r \geq 2 \) 为整数（\( r=1 \) 即同频同相，无需跨越）。
> - **STA（Static Timing Analysis，静态时序分析）**：综合工具对每条寄存器到寄存器的路径检查建立/保持时间。同步时钟之间的路径可被 STA 分析，异步路径则不能。

## 3. 本讲源码地图

| 文件 | 作用 |
|:--|:--|
| [hdl/psi_common_sync_cc_n2xn.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_cc_n2xn.vhd) | 低频→高频（慢→快）同步跨越，AXI-S 接口。本讲核心之一。 |
| [hdl/psi_common_sync_cc_xn2n.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_cc_xn2n.vhd) | 高频→低频（快→慢）同步跨越，AXI-S 接口。本讲核心之二。 |
| [hdl/psi_common_math_pkg.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd) | 提供 `is_int_ratio`，用于在编译期校验「整数比」这一前提。 |
| [testbench/psi_common_sync_cc_n2xn_tb/psi_common_sync_cc_n2xn_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_sync_cc_n2xn_tb/psi_common_sync_cc_n2xn_tb.vhd) | n2xn 自校验测试平台，含 `ratio_g` 时钟比例与流式/反压用例。 |
| [testbench/psi_common_sync_cc_xn2n_tb/psi_common_sync_cc_xn2n_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_sync_cc_xn2n_tb/psi_common_sync_cc_xn2n_tb.vhd) | xn2n 自校验测试平台。 |
| [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl) | 回归注册表，登记了两个 TB 的 `ratio_g=2` 与 `ratio_g=4` 运行组合。 |

## 4. 核心概念与源码讲解

### 4.1 同步时钟前提：为什么能省掉同步器

#### 4.1.1 概念说明

`sync_cc_n2xn` 与 `sync_cc_xn2n` 的源码头注释各写了一句关键前提：

- n2xn：input clock period is an integer multiple of the output clock period（输入时钟周期是输出时钟周期的整数倍，即**输出更快**）。
- xn2n：output clock period is an integer multiple of the input clock period（输出时钟周期是输入周期的整数倍，即**输入更快**）。

两句话的共同点是「integer multiple（整数倍）」。这等价于：两个时钟来自**同一个 PLL**、频率成整数比、相位确定。在这种关系下：

- 慢时钟的每一个上升沿，都与快时钟的某一个上升沿**精确重合**。
- 任意两个寄存器之间的跨域路径，其发送沿与采样沿的相位差是确定的，因此**可被 STA 分析**。
- 既然 STA 能分析，就不存在「采样到亚稳态中间值」的风险，也就**不需要**格雷码指针、不需要双级同步器、不需要 `ASYNC_REG`、不需要手写 `set_max_delay`。

这就是两个组件在设计上与 `async_fifo`/`pulse_cc` 的根本分野。文档对两者的说明都明确写道：

> Constraints are derived by the tools automatically since the clocks are synchronous. Therefore no user constraints are required.
> （因两时钟同步，约束由工具自动推导，无需用户约束。）

注意：这个结论**只对同步整数比时钟成立**。若两时钟异步或频率比非整数，必须改用 `async_fifo`、`simple_cc`、`status_cc` 等带同步器的组件——否则会出现亚稳态与数据撕裂。

#### 4.1.2 核心流程

使用本组件前的判断流程：

```text
两个时钟来自同一 PLL？
  ├─ 否 ──▶ 不能用 sync_cc_*，改用异步 CDC 组件（u5-l1/u5-l2/u4-l2）
  └─ 是 ──▶ 频率成整数比？（可用 math_pkg 的 is_int_ratio 编译期校验）
              ├─ 否 ──▶ 不能用 sync_cc_*
              └─ 是 ──▶ 数据从低频流向高频？──▶ sync_cc_n2xn
                        数据从高频流向低频？──▶ sync_cc_xn2n
```

#### 4.1.3 源码精读

**前提校验工具**。`math_pkg` 提供了编译期判断整数比的函数，可用于在实体里加一道保险（注意它接收的是频率或周期值）：

[hdl/psi_common_math_pkg.vhd:117-118](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_math_pkg.vhd#L117-L118) —— 声明 `is_int_ratio` 的 real/integer 两个重载，编译期返回布尔，用于校验「整数比」前提。

**DUT 头注释里的前提**。两个组件把前提直接写在了文件头部，这是读懂它们的第一把钥匙：

[hdl/psi_common_sync_cc_n2xn.vhd:9-12](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_cc_n2xn.vhd#L9-L12) —— n2xn 注明「输入周期是输出周期的整数倍」（慢入快出）。

[hdl/psi_common_sync_cc_xn2n.vhd:9-12](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_cc_xn2n.vhd#L9-L12) —— xn2n 注明「输出周期是输入周期的整数倍」（快入慢出）。

#### 4.1.4 代码实践

1. **目标**：确认「整数比」是可编译期校验的前提，并区分同步与异步 CDC 的适用面。
2. **步骤**：
   - 打开 `hdl/psi_common_math_pkg.vhd`，找到 `is_int_ratio`（L117-118）与 `ratio`（L101-105）。
   - 在脑中（或在一个临时顶层里）写一句 `assert is_int_ratio(FastFreq, SlowFreq) report "not an integer ratio" severity failure;`，理解它如何在 elaborate 阶段挡掉不满足前提的设计。
3. **观察/预期**：当两频率比为 4.0（整数）时校验通过；比为 1.5（非整数）时 elaborate 报 failure。这一步不产生任何逻辑门，纯编译期检查。
4. 若无法本地 elaborate，明确标注「待本地验证」。

#### 4.1.5 小练习与答案

- **Q1**：同一个 PLL 同时输出 100 MHz 与 50 MHz，二者是否「同步整数比」？能否用 `sync_cc_*`？
  - **A**：是（比值 2，整数），能用。
- **Q2**：100 MHz 与 80 MHz（比值 1.25）能否用 `sync_cc_*`？应该改用哪个组件家族？
  - **A**：不能（非整数比）。若两时钟仍来自同一 PLL 且相位确定，部分工具可做同步分析但本组件未为此设计；最稳妥是按异步 CDC 处理，使用 `async_fifo` 或 `simple_cc/status_cc`。

---

### 4.2 低到高：sync_cc_n2xn（慢→快）

#### 4.2.1 概念说明

`n2xn` 的命名含义是「n 到 x·n」：输入侧是较低频 \( f_{\text{in}} \)，输出侧是较高频 \( f_{\text{out}} = r \cdot f_{\text{in}} \)（\( r \geq 2 \)）。数据从**慢域**进入、从**快域**送出。

这是两个方向里「自然」的那一个：输出侧时钟更快，会非常频繁地巡视内部缓冲，只要慢域写进一个样本，快域在不到一个慢周期内就能把它取走并发出去。因此：

- **不会丢样本**：快域消费速度 ≥ 慢域生产速度。
- **`vld_o` 是低占空的脉冲列**：慢域每个周期产 1 个样本，快域把它变成快时钟上的 1 拍脉冲，之后等下一个慢样本——所以在快时钟上 `vld_o` 大部分时间是低的。

关键认识：**样本不会被「聚合」成一个更宽的字**。`width_g` 在输入输出两端完全相同，本组件只搬时钟域、不改数据本身（这与 `wconv_n2xn/xn2n` 的「位宽转换」是两回事）。

#### 4.2.2 核心流程

以 \( r=4 \)（`clk_in` 25 MHz、`clk_out` 100 MHz）为例，慢域连续流式输入：

```text
慢时钟(clk_in) 周期编号:      0        1        2        3
                              ↓        ↓        ↓        ↓
快时钟(clk_in) 细分:    _|--|--|--|--|--|--|--|--|--|--|--|_
vld_i:                      ─▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔─  (慢域持续有效)
dat_i:                       1   2   3   4   ...
                              缓冲转发
vld_o (快时钟上):          ___▔___________________▔___________  (每慢周期 1 拍脉冲)
dat_o:                          1                  2    ...
```

每个慢样本在快时钟上仅占 1 拍 `vld_o` 脉冲，随后回到 0，等待下一个慢样本。吞吐率 = 慢时钟频率。

#### 4.2.3 源码精读

**实体与端口**。注意 `rst_out_i`/`rdy_i` 都给了默认值（可选连接），最简用法只接 `clk_in_i/rst_in_i/vld_i/dat_i/clk_out_i/vld_o/dat_o`：

[hdl/psi_common_sync_cc_n2xn.vhd:20-34](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_cc_n2xn.vhd#L20-L34) —— generic 仅 `width_g` 与两个复位极性；端口为标准 AXI-S 双侧（`vld/rdy/dat` + 两个时钟域的 `clk/rst`）。

**输入侧进程**（慢时钟域）：握手成功时把 `dat_i` 锁进 `InDataReg`、`InCnt` 自增。是否接受由 `InCnt - OutCnt /= 2` 决定（缓冲未满）：

[hdl/psi_common_sync_cc_n2xn.vhd:51-63](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_cc_n2xn.vhd#L51-L63) —— `p_input` 在 `clk_in_i` 上升沿，复位或 `vld_i` 握手时维护 `InCnt` 与数据寄存器。

**反压输出**（组合逻辑）：缓冲持有 2 个样本时拉低 `rdy_o`，构成 2 级深度的弹性缓冲：

[hdl/psi_common_sync_cc_n2xn.vhd:49](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_cc_n2xn.vhd#L49) —— `rdy_o <= '1' when InCnt - OutCnt /= 2 else '0';`。

**输出侧进程**（快时钟域）：两段 `if` 实现 fall-through 握手。第一段「有挂起样本且输出槽空闲或下游 ready 时，把 `InDataReg` 推到 `dat_o` 并拉高 `vld`」；第二段「当前样本被下游握手时，撤销 `vld` 并 `OutCnt+1`」：

[hdl/psi_common_sync_cc_n2xn.vhd:65-84](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_cc_n2xn.vhd#L65-L84) —— `p_output`，注意它直接读另一个时钟域的 `InCnt`，**无需同步器**（同步时钟前提保证安全，详见 4.4）。

`vld_o` 直接引出内部 `OutVld_I`：

[hdl/psi_common_sync_cc_n2xn.vhd:86](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_cc_n2xn.vhd#L86) —— `vld_o <= OutVld_I;`。

**TB 验证的脉冲形态**。n2xn 的检查进程在「Streaming」用例里，每收到一个样本后都断言下一拍 `vld_o='0'`，即印证了上面「低占空脉冲列」的形态：

[testbench/psi_common_sync_cc_n2xn_tb/psi_common_sync_cc_n2xn_tb.vhd:259-264](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_sync_cc_n2xn_tb/psi_common_sync_cc_n2xn_tb.vhd#L259-L264) —— 收到 val 后 `wait until rising_edge(clk_o)` 再 `assert vld_o = '0'`，确认快时钟上脉冲仅 1 拍。

#### 4.2.4 代码实践

1. **目标**：跟踪一个慢→快样本，预测 `vld_o` 在快时钟上的脉冲位置。
2. **步骤**：
   - 打开 n2xn TB，找到时钟生成：`ClkFreqIn = 100e6`，`clk_o` 频率 = `ClkFreqIn * ratio_g`（L99-117）。注意 TB 里 `clk_i` 是**慢**时钟、`clk_o` 是**快**时钟，ratio_g=4 时即 25 MHz→100 MHz。
   - 阅读「Single Beats」激励（L151-169）：每个样本 `vld_i` 只拉 1 拍，然后空几拍。
   - 对照检查进程（L242-252），确认每个样本在快时钟上以单拍脉冲出现。
3. **观察/预期**：每注入 1 个慢样本，`vld_o` 在快时钟上恰好出现 1 拍高电平，数据值与输入一致（`StdlvCompareInt` 比较）。
4. 运行结果「待本地验证」（需 PsiSim/psi_tb 环境，见 u1-l3）。

#### 4.2.5 小练习与答案

- **Q1**：n2xn 的 `rdy_o` 在什么条件下为 0？
  - **A**：当 `InCnt - OutCnt = 2`（内部缓冲持有 2 个未消费样本）时为 0，否则为 1。
- **Q2**：慢域连续流式输入时，为什么 `vld_o` 在快时钟上是「低占空」而非持续高？
  - **A**：因为生产率（慢）远低于消费率（快），快域取走一个样本后，要等约 \( r \) 个快周期才有下一个慢样本，期间 `vld_o` 自然为 0。

---

### 4.3 高到低：sync_cc_xn2n（快→慢）

#### 4.3.1 概念说明

`xn2n` 是反方向：输入侧较高频 \( f_{\text{in}} = r \cdot f_{\text{out}} \)，输出侧较低频 \( f_{\text{out}} \)。数据从**快域**进入、从**慢域**送出。

这个方向的难点在于：快域可能在两个慢时钟沿之间产出多个样本，而慢域每个慢周期只能送出 1 个。设计上用两招应对：

1. **2 级深度缓冲 + 反压**：快域最多可囤 2 个样本（`InCnt - OutCnt` 上限 2），到 2 就回拉 `rdy_o=0` 把快域生产者「掐住」。于是**数据不会被丢弃**，吞吐被限制到慢域频率。
2. **双数据寄存器保序**：用 `InDataReg`（最新样本）与 `InDataRegLast`（次新样本）两级移位，配合 `InCnt - OutCnt` 的差值选择输出哪一级，保证**先进先出**顺序。

与 n2xn 相反，这里的 `vld_o` 在慢时钟上**可以背靠背持续为高**（慢域连续送出囤积的样本），TB 也**不**断言 `vld_o` 必须回落。

#### 4.3.2 核心流程

xn2n 输出侧的核心判据是「当前挂了几个样本」，用差值 \( d = InCnt - OutCnt \)（2 位无符号，回绕）决定：

```text
d = 0  ── 没有挂起样本，输出空闲
d = 1  ── 挂 1 个（即 InDataReg），输出 dat_o <= InDataReg
d = 2  ── 挂 2 个（InDataReg 新、InDataRegLast 旧），输出 dat_o <= InDataRegLast（先送旧的，保序）
```

样本序列 A、B 连续到达时的处理：

```text
快域到达 A:  InDataReg=A,            InCnt=1   (d=1) ─▶ 慢域送 A
快域到达 B:  InDataReg=B, Last=A,    InCnt=2   (d=2) ─▶ 慢域送 InDataRegLast=A（旧的先走）
             慢域消费后 OutCnt++      ────────── (d=1) ─▶ 慢域送 InDataReg=B
```

顺序保持 A、B，无丢失、无乱序。

#### 4.3.3 源码精读

**输入侧进程**（快时钟域）：与 n2xn 相比多了一行 `InDataRegLast <= InDataReg;`，构成「新值进 `InDataReg`、旧值下移到 `InDataRegLast`」的移位：

[hdl/psi_common_sync_cc_xn2n.vhd:52-65](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_cc_xn2n.vhd#L52-L65) —— `p_input` 在握手成功时同时更新两级数据寄存器与 `InCnt`。

**输出侧进程**（慢时钟域）：先「消费当前样本」（若 `OutVld_I` 且 `rdy_i`），再「按差值选择推送哪一级」——差值为 1 取 `InDataReg`，否则（差值为 2）取 `InDataRegLast`，并 `OutCnt+1`：

[hdl/psi_common_sync_cc_xn2n.vhd:67-90](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_cc_xn2n.vhd#L67-L90) —— `p_output`，关键在 L80-84 的 `if InCnt - OutCnt = 1 then ... else ...` 分支。

**与 n2xn 的代码差异**。两段进程的「消费」与「推送」`if` 顺序在两个组件里**相反**：xn2n 把「推送」放在最后，使 `OutVld_I<='1'` 在两段都触发时占优，从而支持慢时钟上背靠背持续输出；n2xn 把「消费」放在最后，与其低占空脉冲形态一致。这是阅读时最值得对照的一处差异：

- n2xn 输出：先推送、后消费（[L72-81](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_cc_n2xn.vhd#L72-L81)）。
- xn2n 输出：先消费、后推送（[L74-87](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_cc_xn2n.vhd#L74-L87)）。

**TB 不要求 vld 回落**。xn2n 的「Normal Operation」检查只逐个 `wait until vld_o='1'` 并比较数据，**没有** `assert vld_o='0'`，正说明慢时钟上 `vld_o` 可连续为高：

[testbench/psi_common_sync_cc_xn2n_tb/psi_common_sync_cc_xn2n_tb.vhd:232-237](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_sync_cc_xn2n_tb/psi_common_sync_cc_xn2n_tb.vhd#L232-L237) —— 连续 8 个样本逐一比对，无回落断言。

#### 4.3.4 代码实践

1. **目标**：验证双寄存器在「快域连发 2 个样本」时的保序行为。
2. **步骤**：
   - 打开 xn2n TB，注意时钟设置与 n2xn 相反：`ClkFreqOut = 100e6`，`clk_i`（输入）频率 = `ClkFreqOut * ratio_g`，即**输入快、输出慢**（L63、L99-117）。
   - 阅读「Normal Operation」激励（L151-170）：每发一个样本后等待 `del*ratio_g - 1` 个快周期再发下一个，刻意让数据率 ≤ 慢域率，避免长期反压。
   - 在脑中把激励改成「连发 2 个样本」（`del=0` 极端情形），跟踪 `InDataReg/InDataRegLast` 与差值。
3. **观察/预期**：连发 A、B 后，慢域先送 A（`InDataRegLast`），再送 B（`InDataReg`）；若快域不顾 `rdy_o` 强行连发第 3 个，则会被 `rdy_o=0` 挡住（除非上游违反 AXI-S 契约）。
4. 运行结果「待本地验证」。

#### 4.3.5 小练习与答案

- **Q1**：为何 xn2n 比 n2xn 多一个 `InDataRegLast` 寄存器？
  - **A**：快→慢方向下两个慢沿之间可能囤 2 个样本，需要两级存储；输出时按差值选择「先送旧的」，保证 FIFO 顺序。n2xn 慢→快不会稳定囤 2 个，故不需要。
- **Q2**：差值为 2 时为何输出 `InDataRegLast` 而非 `InDataReg`？
  - **A**：`InDataRegLast` 是两个挂起样本里较早到达的那个，先送它才能保序；`InDataReg`（较新）留给下一次慢沿。

---

### 4.4 ratio 与握手：共享的双计数器缓冲机制

#### 4.4.1 概念说明

读到这里你会发现两个组件的骨架几乎一样：`InCnt`/`OutCnt` 两个 2 位无符号计数器、`rdy_o` 组合反压、跨域直接读取对方的计数器。本节把这套**共享机制**讲透，并回答三个问题：

1. **ratio 在哪里？** —— 答案：**ratio 不是 DUT 的 generic**。DUT 的 entity 只有 `width_g` 与两个复位极性，没有任何比例参数。ratio 完全隐含在「两时钟的同步整数比关系」里。组件对任意整数比 \( r \geq 2 \) 都正确工作，TB 用 `ratio_g=2` 与 `ratio_g=4` 两组运行覆盖（见 config.tcl）。这是它与「按 ratio 参数化」组件的一大区别。
2. **跨域读计数器为何安全？** —— 输入进程读 `OutCnt`、输出进程读 `InCnt`，都是「裸」的 2 位二进制值，**没有**格雷码、**没有**同步器。安全性来自三重保障：①同步时钟使跨域路径可被 STA 分析；②慢沿与快沿重合时，寄存器读到的是对端**上一拍**的稳定值；③判据用的是**不等式**（`/= 2`、`/= 0` 等），即便采样早一拍或晚一拍，结论也只是「反应迟一拍」，绝不会把「满」误判成「空」或反之。
3. **吞吐率由谁决定？** —— 由**较慢**的那个时钟决定。设 \( f_{\text{low}} = \min(f_{\text{in}}, f_{\text{out}}) \)，则吞吐上界为：

\[
\text{Throughput} \leq f_{\text{low}} \quad \text{(samples/sec)}
\]

反压总是流向「较快」的那一侧：n2xn 中若快域下游来不及取（`rdy_i=0`），反压经 `rdy_o` 传回慢域生产者；xn2n 中快域生产者天然比慢域消费者快，`rdy_o` 会常态化地把它节流到慢域频率。

#### 4.4.2 核心流程

**2 位计数器的回绕算术**。`InCnt`、`OutCnt` 都是 `unsigned(1 downto 0)`，取值 0..3，差值 `InCnt - OutCnt` 在 2 位无符号下自动 mod 4。由于设计保证差值始终 ∈ {0,1,2}（满即停），回绕不会产生歧义：

\[
\text{level} = (InCnt - OutCnt) \bmod 4 \in \{0,1,2\}
\]

```text
level=0 : 空     ── 输出侧不送；输入侧可写
level=1 : 1 个   ── 两端均可动作
level=2 : 满     ── rdy_o=0（输入侧停写）；输出侧继续送
```

整条数据通路的节拍：

```text
生产者 ──vld_i/rdy_o──▶ [输入进程: 锁存 + InCnt++] ──┐
                                                    │ 跨域直读计数器（同步，无同步器）
消费者 ◀──vld_o/rdy_i── [输出进程: 推送 + OutCnt++] ◀┘
```

#### 4.4.3 源码精读

**共享的反压式**（两文件逐字相同）：

[hdl/psi_common_sync_cc_n2xn.vhd:49](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_cc_n2xn.vhd#L49) 与 [hdl/psi_common_sync_cc_xn2n.vhd:50](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_cc_xn2n.vhd#L50) —— `rdy_o <= '1' when InCnt - OutCnt /= 2 else '0';`，定义了 2 级深度的缓冲上限。

**复位策略**。两个进程都把两侧复位「或」在一起：任一域在复位，就清零本域计数器与 `vld`。配合 `in_rst_pol_g/out_rst_pol_g` 两个极性 generic，可适配高/低有效复位的四种搭配：

[hdl/psi_common_sync_cc_n2xn.vhd:54](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_cc_n2xn.vhd#L54) —— `if (rst_in_i = in_rst_pol_g) or (rst_out_i = out_rst_pol_g) then ...`，xn2n 同构（[L55](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_cc_xn2n.vhd#L55)）。

**ratio 只在 TB 里**。DUT 实体不含 ratio（[n2xn L20-34](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_cc_n2xn.vhd#L20-L34) / [xn2n L20-34](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sync_cc_xn2n.vhd#L20-L34)）；ratio 仅作为 TB 的 generic，用来设置两个时钟的频率比：

[testbench/psi_common_sync_cc_n2xn_tb/psi_common_sync_cc_n2xn_tb.vhd:28-31](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_sync_cc_n2xn_tb/psi_common_sync_cc_n2xn_tb.vhd#L28-L31) —— `ratio_g : integer := 2`，TB 用它推导快时钟频率（L110）。

[sim/config.tcl:307-316](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L307-L316) —— 回归注册为两个 TB 各登记 `ratio_g=2`、`ratio_g=4` 两组运行。

#### 4.4.4 代码实践

1. **目标**：用 100 MHz 与 25 MHz 一对同步时钟，完成选型并预测行为。
2. **步骤**：
   - 确认前提：25 MHz 与 100 MHz 来自同一 PLL、比值 4（整数）——满足 sync_cc 前提。
   - 方向 A：数据 25 MHz→100 MHz。选 `sync_cc_n2xn`（`clk_in_i`=25 MHz，`clk_out_i`=100 MHz）。预测吞吐 ≤ 25 Msps；`vld_o` 在 100 MHz 上是约 1/4 占空的脉冲列。
   - 方向 B：数据 100 MHz→25 MHz。选 `sync_cc_xn2n`（`clk_in_i`=100 MHz，`clk_out_i`=25 MHz）。预测吞吐 ≤ 25 Msps；100 MHz 侧生产者若全速生产，`rdy_o` 会常态节流，`vld_o` 在 25 MHz 上可背靠背为高。
3. **观察/预期**：两个方向吞吐相同（都受 25 MHz 限制），差异在「哪一侧被反压」与「`vld_o` 脉冲形态」。
4. 波形「待本地验证」（可用 TB 的 `ratio_g=4` 运行近似复现）。

#### 4.4.5 小练习与答案

- **Q1**：DUT 没有 ratio 参数，为什么仍能对任意整数比正确工作？
  - **A**：因为握手与缓冲逻辑只依赖「样本计数差」与同步时钟下的稳定采样，与具体比值无关；比值只决定两时钟的边沿疏密，已被同步关系涵盖。
- **Q2**：跨域读取 `InCnt`/`OutCnt` 不加同步器，安全性的三重保障是什么？
  - **A**：①同步时钟使路径可被 STA 分析；②重合沿采样到的是对端上一拍稳定值；③判据用不等式，对早/晚一拍采样天然容忍。
- **Q3**：把 100 MHz→100 MHz（同频同相）用 `sync_cc_n2xn` 会怎样？
  - **A**：比值 \( r=1 \)，组件虽不报错但失去意义（`math_pkg.ratio` 对相等频率会打 warning）。同频同相本就无需 CDC，直接连线即可。

## 5. 综合实践

**任务**：为一个含两个同步时钟域（100 MHz 与 25 MHz，同 PLL）的小系统设计 CDC，并对比 `sync_cc_*` 与异步 CDC 的工程代价。

1. **场景设定**：ADC 采样数据以 100 MHz 字速率到达；后端处理模块跑在 25 MHz 时钟，每个慢周期可处理 1 个字。两时钟同源同步。
2. **选型与实例化**：
   - 选 `psi_common_sync_cc_xn2n`（快 100 MHz→慢 25 MHz），`width_g` 设为 ADC 位宽。
   - 写出端口连接草图（`clk_in_i`←100 MHz，`clk_out_i`←25 MHz，AXI-S 双侧接 ADC 与处理模块）。
3. **行为预测**：
   - 吞吐上界 = 25 Msps；若 ADC 真以 100 Msps 全速送数，`rdy_o` 会持续拉低把 ADC 节流到 25 Msps（数据不丢，但有效采样率下降——这在实际系统里意味着你需要前端降速或加更深 FIFO）。
   - `vld_o` 在 25 MHz 上可背靠背为高。
4. **工程代价对比**：列一张表，对比本组件与「改用 `async_fifo` 做同样跨越」的差异：

   | 维度 | sync_cc_xn2n | async_fifo（异步 FIFO） |
   |:--|:--|:--|
   | 适用前提 | 同步整数比时钟 | 任意（含异步）时钟 |
   | 同步器 / 格雷码 | 无 | 必须（格雷指针 + 2 级同步） |
   | 用户约束 | 无（工具自动推导） | 需 `ASYNC_REG` + `set_max_delay` |
   | 缓冲深度 | 固定 2 | 可配（`depth_g`，须 2 的幂） |
   | 复杂度 | 极低 | 较高 |

5. **验证**：若本地有 PsiSim 环境（见 u1-l3），按 `config.tcl` 中 `ratio_g=4` 跑 xn2n TB，观察 `vld_o/rdy_o` 波形与上表预测是否一致；否则标注「待本地验证」。

## 6. 本讲小结

- `sync_cc_n2xn` / `sync_cc_xn2n` 只适用于**同 PLL、频率成整数比**的同步时钟域；前提可用 `math_pkg.is_int_ratio` 编译期校验。
- 由于时钟同步、相位确定，跨域路径可被 STA 分析，因此**无需同步器、无需格雷码、无需手写约束**——这是它们相对 `async_fifo`/`pulse_cc` 的核心简化。
- `n2xn` = 慢→快：快域迅速取走慢域样本，`vld_o` 在快时钟上呈低占空脉冲列；`xn2n` = 快→慢：靠 2 级缓冲 + 反压把快域节流到慢域率，`vld_o` 在慢时钟上可背靠背为高。
- 两个组件共享「`InCnt`/`OutCnt` 2 位计数器 + `InCnt-OutCnt/=2` 反压」的 2 级深度缓冲；xn2n 额外用 `InDataReg/InDataRegLast` 双寄存器按差值保序。
- **ratio 不是 DUT 的 generic**，它隐含在时钟关系里，故组件对任意整数比 \( r \geq 2 \) 都正确；TB 用 `ratio_g` 仅用于设置仿真时钟比例。
- 吞吐率恒由**较慢**时钟决定：\( \text{Throughput} \leq f_{\text{low}} \)；反压总流向较快的一侧。

## 7. 下一步学习建议

- **对比宽度转换**：阅读 `hdl/psi_common_wconv_n2xn.vhd` / `wconv_xn2n.vhd`（u8-l1），体会「改时钟域」与「改位宽」两类「n2xn/xn2n」命名的区别——本讲只改时钟域、不动数据位宽。
- **回到异步 CDC 全景**：当你的时钟不再同步时，回到 u5-l1（`pulse_cc`）、u5-l2（`simple_cc/status_cc/bit_cc`）与 u4-l2（`async_fifo`），对照「需要同步器/格雷码/约束」的完整代价。
- **节拍与时钟生成**：结合 u6-l1（`strobe_generator/tickgenerator`），理解同步整数比时钟在系统中如何由同一 PLL 派生，以及选通信号如何在同步域间安全传递。
- **实践延伸**：仿照 `testbench/psi_common_sync_cc_*_tb` 的自校验结构（u11-l1 会系统讲解 psi_tb 用法），为本讲综合实践里的「ADC→处理」场景写一个最小 TB，复用 `ratio_g` 在不同整数比下回归。
