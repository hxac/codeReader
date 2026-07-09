# CDC 基础：电平、脉冲、计数器的同步

## 1. 本讲目标

本讲进入 `resync` 模块，学习 hdl-modules 处理**时钟域跨越（Clock Domain Crossing, CDC）**的基础构件。读完本讲，你应当能够：

1. 说清楚**亚稳态（metastability）**是怎么产生的，为什么「两级 `async_reg` 寄存器 + 把它们放进同一个 slice」能显著提升平均无故障时间（MTBF）。
2. 区分三类信号的同步方式——**电平（level）**、**脉冲（pulse）**、**多比特计数器（counter）**——并能针对每种信号选用正确的实体，知道用错会发生什么（漏脉冲、读到乱码等）。
3. 读懂 `resync_level`、`resync_pulse`、`resync_counter`、`resync_cycles` 四个实体的真实源码，理解它们各自插入 `async_reg` 链的位置与配套 `.tcl` 约束的意图。
4. 在两个异步时钟域的 testbench 里实例化并验证这些实体。

本讲是后续 `asynchronous_fifo`（u4-l2）与 AXI CDC（u5-l3）的前置——异步 FIFO 的读/写指针同步，本质上就是本讲的 `resync_counter`。

---

## 2. 前置知识

本讲依赖 u2-l2 中讲过的 `common` 基础包，先回顾三个关键点：

- **`attribute_pkg` 里的两个综合属性**（[modules/common/src/attribute_pkg.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/src/attribute_pkg.vhd)）：
  - `dont_touch`：告诉综合/布局布线工具「不要把这个寄存器优化掉、不要把逻辑搬进来或挤出去」。CDC 同步链的输入必须由真实 FF 驱动，这个属性用来锁死它。
  - `async_reg`：Xilinx 专属属性，作用是「这两个寄存器不要被优化，并且要被布局到**同一个 slice**里」，以最大化两级寄存器之间可用于恢复亚稳态的时间，从而最大化 MTBF。
- **`common_pkg.if_then_else`**：充当 VHDL 的三目运算符，在常量/属性计算里到处用。
- **`types_pkg.to_int` / `to_sl`**：布尔与 `std_ulogic`/整数之间的胶水转换，testbench 里常用。

另外需要一点直觉：

- **建立/保持时间（setup/hold）**：每个 FF 要求输入数据在时钟沿前后的一段时间内保持稳定。若违反，FF 输出可能停在 0 和 1 之间的「中间电平」，并经过一段不定时间才随机塌缩到 0 或 1——这就是**亚稳态**。
- **跨时钟域**：当发送时钟 `clk_in` 与采样时钟 `clk_out` 异步时，`clk_in` 域里变化的信号相对 `clk_out` 的时钟沿几乎必然偶尔违反 setup/hold，所以必须经过「同步器」才能在 `clk_out` 域里使用。
- **MTBF（Mean Time Between Failure）**：衡量同步器「多久才漏采一次」的指标。级数越多、留给亚稳态恢复的时间越长，MTBF 越大（按指数改善）。

> 通俗比方：同步器像是一个「等泥水沉淀」的水池。第一级 FF 舀进来的可能是浑水（亚稳态），让它在第二级 FF 前多沉淀一个时钟周期，等它澄清（塌缩成确定的 0/1）再用。两级比一级稳得多。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [modules/resync/src/resync_level.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_level.vhd) | 同步**单比特电平**信号；双 `async_reg` 链，可选输入寄存器 |
| [modules/resync/src/resync_pulse.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_pulse.vhd) | 同步**单周期脉冲**；脉冲↔电平转换 + 反馈门控防过载 |
| [modules/resync/src/resync_counter.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_counter.vhd) | 用**格雷码**安全同步**多比特计数器** |
| [modules/resync/src/resync_cycles.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_cycles.vhd) | 让输出在 `clk_out` 域被置位**相同拍数**；复用 `resync_counter` |
| [modules/math/src/math_pkg.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/math_pkg.vhd) | 提供 `to_gray`/`from_gray`/`hamming_distance`（u2-l3 已讲） |
| [modules/resync/scoped_constraints/*.tcl](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_level.tcl) | 三个实体各自的作用域约束（`set_max_delay`/`set_bus_skew`/`set_false_path`） |
| [modules/resync/test/tb_resync_*.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_counter.vhd) | VUnit 自检式 testbench，是本讲实践的模板 |

记忆口诀：**电平用 level，脉冲用 pulse，多比特用 counter，按拍数用 cycles**。

---

## 4. 核心概念与源码讲解

### 4.1 resync_level：电平信号与双寄存器同步链

#### 4.1.1 概念说明

`resync_level` 处理的是**「准静态电平」**信号——比如一个慢速的使能开关、一个配置位。它的特点是用 `clk_in` 域里的 FF 驱动一个电平，这个电平变化得很慢，远比 `clk_out` 的时钟周期慢。

它用两条级联的 FF 构成同步链：第一级采样异步输入（这一级可能进入亚稳态），第二级给亚稳态留出一整个时钟周期去塌缩，第二级的输出才被当作可靠信号使用。关键在于：通过 `async_reg` 属性，工具会把这两级 FF 布局到**同一个 slice**，使两级之间的布线延迟最小、可用于恢复的时间最大，从而把 MTBF 拉到可接受的水平。

一个广为使用的（近似）MTBF 模型表达了这种指数关系：

\[
\text{MTBF} \;\approx\; \frac{\exp\!\left(t_r / \tau\right)}{T_0 \cdot f_{\text{clk}} \cdot f_{\text{data}}}
\]

其中 \(t_r\) 是留给亚稳态恢复的时间（两级同步链里就是大约一个 `clk_out` 周期减去布线/建立开销），\(\tau\)、\(T_0\) 是工艺相关常数。可见 \(t_r\) 越大，MTBF 按**指数**增长——这正是「多一级寄存器、并放近一点」价值巨大的原因。

> ⚠️ 重要限制（来自源码头注释）：`resync_level` **只适合电平，不适合脉冲**。如果输入是一个单周期脉冲，`clk_out` 域可能根本采样不到它，脉冲会被静默丢掉。

#### 4.1.2 核心流程

```
clk_in 域                       clk_out 域
--------                        ----------
data_in ──(可选输入寄存器)──> data_in_int
                                 │
                          data_in_p1   ← async_reg（第一级，可能亚稳态）
                                 │
                          data_out_int ← async_reg（第二级，已恢复）
                                 │
                            data_out
```

- 若 `enable_input_register = true`：先用一个 `clk_in` 时钟的 FF 把 `data_in` 寄存成 `data_in_int`，再送入同步链。
- 若 `enable_input_register = false`：`data_in_int` 只是 `data_in` 的直通连线。
- 同步链 `data_in_p1` / `data_out_int` 永远存在，两级都标 `async_reg`，都由 `clk_out` 时钟驱动。

#### 4.1.3 源码精读

先看实体声明与 generic。`enable_input_register` 决定要不要在 `clk_in` 域加一级寄存器；`default_value` 决定上电后、输入尚未传过来之前输出的初值（避免输出是 `'U'`）：

[modules/resync/src/resync_level.vhd:L68-L85](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_level.vhd#L68-L85) — 实体定义，`clk_in` 端口默认值 `'U'`（即「不强制连接」），`data_out` 初值取自 `default_value`。

接着是三个属性声明，这是本实体的灵魂：

[modules/resync/src/resync_level.vhd:L91-L97](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_level.vhd#L91-L97) — `data_in_int` 标 `dont_touch`（保住输入寄存器/连线不被优化），`data_in_p1` 与 `data_out_int` 标 `async_reg`（锁死两级同步链并要求同 slice 布局）。

输入寄存器是 generic 控制的 generate 块：

[modules/resync/src/resync_level.vhd:L105-L129](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_level.vhd#L105-L129) — 开启时有一个 `clk_in` 时钟进程把 `data_in` 打一拍；并且带一条仅在仿真里触发的断言「用了输入寄存器就必须连 `clk_in`」。关闭时 `data_in_int <= data_in` 是纯连线。

最后是同步链本体——一个简单到不能再简单的两级移位寄存器，但正因为简单，全部可靠性都压在 `async_reg` 属性和 `.tcl` 约束上：

[modules/resync/src/resync_level.vhd:L133-L139](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_level.vhd#L133-L139) — `clk_out` 时钟进程，每拍 `data_out_int <= data_in_p1; data_in_p1 <= data_in_int;`，构成两级同步。

#### `enable_input_register` 的两个独立作用

源码注释把这件事讲得很细，值得单独记住（这是初学者最容易混淆的点）：

1. **抗毛刺（glitch）**：如果 `data_in` 由组合逻辑（LUT）驱动，它可能在跳变瞬间出现短暂的错误电平（glitch），被第一级 FF 直接采到。开启输入寄存器后，送入同步链的是 `clk_in` 域一个干净 FF 的输出，不会再有毛刺。
2. **确定性延迟（deterministic latency）**：只有当 `enable_input_register = true` **且** `clk_in` 端口被连接时，约束脚本才能找到一个 `clk_in` 域的起始 FF，从而用 `set_max_delay -datapath_only` 给出一个确定的延迟上界。否则只能退化成 `set_false_path`，延迟由布局布线随意决定。

> 注释明确：如果你的输入已经由一个 FF 驱动（无毛刺风险），可以设 `false` 省一个寄存器；但代价是失去确定性延迟。

#### 4.1.4 代码实践

**目标**：在两个异步时钟域里验证 `resync_level` 能稳定传递一个慢电平。

仓库里没有 `tb_resync_level.vhd`（只有针对向量版本 `resync_slv_level` 的 [tb_resync_slv_level.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_slv_level.vhd)），所以下面是一段**示例代码**，仿照项目里 [tb_resync_counter.vhd:L29-L45](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_counter.vhd#L29-L45) 的双时钟写法：

```vhdl
-- 示例代码：最小化的 resync_level testbench（VUnit 风格）
library ieee;
use ieee.std_logic_1164.all;
library vunit_lib;
use vunit_lib.check_pkg.all;
use vunit_lib.run_pkg.all;

entity tb_resync_level_demo is
  generic (runner_cfg : string);
end entity;

architecture tb of tb_resync_level_demo is
  constant clk_in_period  : time := 3.3 ns;  -- 与 tb_resync_counter 取不同的异步周期
  constant clk_out_period : time := 4.0 ns;
  signal clk_in, clk_out : std_ulogic := '0';
  signal data_in, data_out : std_ulogic;
begin
  clk_in  <= not clk_in  after clk_in_period / 2;
  clk_out <= not clk_out after clk_out_period / 2;
  test_runner_watchdog(runner, 10 ms);

  dut : entity work.resync_level
    generic map (enable_input_register => true, default_value => '0')
    port map (clk_in => clk_in, data_in => data_in,
              clk_out => clk_out, data_out => data_out);

  main : process is
  begin
    test_runner_setup(runner, runner_cfg);
    data_in <= '0';
    wait for 100 ns;  -- 等初值传播
    check_equal(data_out, '0');

    data_in <= '1';                       -- 慢电平拉高
    wait until rising_edge(clk_out) and data_out = '1' for 50 * clk_out_period;
    check_equal(data_out, '1');           -- 电平稳定传到对端

    data_in <= '0';
    wait until rising_edge(clk_out) and data_out = '0' for 50 * clk_out_period;
    check_equal(data_out, '0');
    test_runner_cleanup(runner);
  end process;
end architecture;
```

**操作步骤**：
1. 把上面片段存为 `modules/resync/test/tb_resync_level_demo.vhd`（仅本地实验，勿提交）。
2. 按 u1-l3/u8-l2 的方式用 `tools/simulate.py` 指向该 testbench（库名必须是 `resync`）。
3. 把 `enable_input_register` 改成 `false` 重跑，对比综合后两级同步链是否一致。

**需要观察的现象**：`data_out` 在若干 `clk_out` 周期后跟随 `data_in` 变化；由于两时钟异步，采样延迟在 1~2 拍之间抖动属正常。

**预期结果**：两条 `check_equal` 通过。实际是否能在你的机器上一键跑通，**待本地验证**（取决于 VUnit/Vivado 环境）。

#### 4.1.5 小练习与答案

**练习 1**：把 `data_in` 换成一个只持续一个 `clk_in` 周期的脉冲（`'1'` 一拍后立刻 `'0'`），`data_out` 会怎样？
**答案**：很可能观察不到任何变化——脉冲被静默丢掉。这正是源码头注释警告「本实体不能处理 pulse」的含义。

**练习 2**：为什么 `data_in_p1` 和 `data_out_int` 必须用 `async_reg` 而不是普通寄存器？
**答案**：`async_reg` 既防止工具优化/挪动这两个寄存器，又强制把它们布局到同一个 slice，最大化两级之间可用于亚稳态恢复的时间 \(t_r\)，从而最大化 MTBF。

**练习 3**：`default_value => '0'` 时，仿真刚开始 `data_out` 为什么不会是 `'U'`？
**答案**：三个内部信号在声明时都初始化为 `default_value`，`data_out` 端口也以 `default_value` 为初值，所以在第一个输入值传到之前输出就是确定值。

---

### 4.2 resync_pulse：脉冲跨域与反馈门控

#### 4.2.1 概念说明

`resync_pulse` 解决的是「把一个**单周期脉冲**从一个时钟域送到另一个」的问题。思路是**脉冲↔电平转换**：

- 在 `clk_in` 域，每收到一个脉冲就把一个电平 `level_in` **翻转**一次（脉冲 → 电平跳变沿）。
- 用 `resync_level` 同款的双 `async_reg` 链把这个电平同步到 `clk_out` 域。
- 在 `clk_out` 域做**边沿检测**：当同步后的电平与上一拍不同时，输出一个单周期脉冲（电平 → 脉冲）。

但裸的脉冲 CDC 有个固有缺陷——**脉冲过载（pulse overload）**：如果两个输入脉冲挨得太近（距离显著小于两个 `clk_out` 周期），第二脉冲到来时电平可能还没被翻转处理完，导致**脉冲被吞掉**。源码给出两种安全条件：要么 `clk_out` 显著快于 `clk_in` 的两倍以上，要么应用层保证脉冲稀疏。

为此实体提供**反馈门控（feedback level）**机制（`enable_feedback` 默认 `true`）：把同步到 `clk_out` 的电平再用一条反向 `async_reg` 链回送到 `clk_in` 域，`clk_in` 域**只有在上一次翻转确认已经抵达对端后才接受下一次翻转**。这保证了「即便多个脉冲挤在一起，输出至少产生一个脉冲」，但**不保证脉冲数量精确**——所以它适合「有没有发生过」的语义，不适合「发生了几次」的语义。

#### 4.2.2 核心流程

```
clk_in 域                                   clk_out 域
---------                                   ----------
pulse_in ──>(翻转 level_in, 门控)──> level_in ──async_reg 链──> level_out
   ↑                                                          │
   │                                          边沿检测 level_out vs level_out_p1
   │                                                          │
   └──── 反馈 async_reg 链 ◀── level_out_feedback ◀──── pulse_out
```

- 正向：`level_in` → `level_out_m1`（`async_reg`）→ `level_out`（`async_reg`）→ 边沿检测得 `pulse_out`。
- 反向（可选）：`level_out` → `level_out_feedback_m1`（`async_reg`）→ `level_out_feedback` → 回到 `clk_in` 域做门控。
- 门控条件：仅当 `level_in = level_out_feedback`（上一翻转已确认）才允许再次翻转 `level_in`。

#### 4.2.3 源码精读

generic 与端口。注意 `enable_feedback` 默认 `true`、`active_level` 默认 `'1'`：

[modules/resync/src/resync_pulse.vhd:L78-L99](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_pulse.vhd#L78-L99) — `overload_has_occurred` 是一个粘住就不清的「曾经发生过载」监视信号。

信号与属性。注意 `async_reg_feedback` 这个常量用 `if_then_else`（u2-l2 讲过的三目）按 generic 决定字符串——若不启用反馈，反馈链的两个寄存器虽然存在但不标 `async_reg`，避免工具报无用告警：

[modules/resync/src/resync_pulse.vhd:L101-L122](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_pulse.vhd#L101-L122) — `level_in` 与 `level_out` 标 `dont_touch`（保证送入 async_reg 链的是真实 FF），正向链 `level_out_m1`/`level_out` 标 `async_reg`。

`clk_in` 域进程：翻转电平 + 过载检测 + 反馈采样：

[modules/resync/src/resync_pulse.vhd:L126-L164](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_pulse.vhd#L126-L164) — 关键门控 `if level_in = level_out_feedback or not enable_feedback`（第 132 行）决定是否翻转；过载判断在第 139-156 行，会把 `overload_has_occurred` 置 1 并按 `assert_false_on_pulse_overload` 决定是否断言失败。

`clk_out` 域进程与电平→脉冲转换：

[modules/resync/src/resync_pulse.vhd:L167-L180](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_pulse.vhd#L167-L180) — `level_out_p1 <= level_out` 用于边沿检测；第 180 行的并发赋值 `pulse_out <= (not active_level) when level_out = level_out_p1 else active_level` 就是「电平变了就出一拍脉冲」。

约束侧，正向链与（可选）反馈链各需一条 `set_max_delay -datapath_only`：

[modules/resync/scoped_constraints/resync_pulse.tcl:L52-L64](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_pulse.tcl#L52-L64) — 对 `level_in_reg → level_out_m1_reg` 施加约束，并在反馈路径存在时同样施加。

#### 4.2.4 代码实践

**目标**：用一个单拍 `pulse_in`，验证 `clk_out` 域确实产生一个单拍 `pulse_out`；再故意制造过载，观察反馈机制如何「兜底」。

仓库已有现成 testbench [tb_resync_pulse.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_pulse.vhd)，它已经把「快/慢/同频 × 是否过载 × 是否反馈」组合枚举好了。建议**直接读它而不是重写**：

[modules/resync/test/tb_resync_pulse.vhd:L76-L92](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_pulse.vhd#L76-L92) — `test_pulse` 过程发一个脉冲；当 `input_pulse_overload` 为真时紧接着再发一个。

[modules/resync/test/tb_resync_pulse.vhd:L105-L127](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_pulse.vhd#L105-L127) — 期望值检查：反馈开启时输出脉冲数 == 输入脉冲数；反馈关闭且慢时钟时，断言输出数严格小于输入（脉冲被丢）但大于 20（至少没全丢）。

**操作步骤**：
1. 在仓库根目录配好 `PYTHONPATH`（见 u1-l3），运行针对该 testbench 的仿真。
2. 阅读 [module_resync.py:L53-L69](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/module_resync.py#L53-L69) 中的 `setup_resync_pulse_tests`，看 generic 矩阵如何由四重循环展开。

**需要观察的现象**：开启反馈时，即便输入过载，输出脉冲数仍精确等于输入脉冲数；关闭反馈且 `clk_out` 较慢时，输出脉冲数少于输入。

**预期结果**：所有断言通过。**待本地验证**（依赖 VUnit + 仿真器）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `resync_pulse` 源码说「输入可以由 LUT 驱动」，而 `resync_level` 却建议用输入寄存器防毛刺？
**答案**：`resync_pulse` 在 `clk_in` 域里把 `level_in` 实现成一个带 `dont_touch` 的真实 FF，送入 async_reg 链的永远是干净的 FF 输出，所以不怕上游是 LUT。`resync_level` 的输入可能直通进链，故需输入寄存器防毛刺。

**练习 2**：反馈机制保证的是「脉冲数量精确」还是「至少有一个脉冲」？
**答案**：后者。反馈保证一簇挤在一起的脉冲至少产生一个输出，但会把多个折叠成一个，所以**数量不精确**，只适合「是否发生」语义。

**练习 3**：`async_reg_feedback` 常量为何要用 `if_then_else` 而不是写死 `"true"`？
**答案**：若反馈被禁用，反馈链的两个寄存器在综合后并不被使用；写死 `"true"` 会让工具保留无用告警/约束，故按 generic 动态取值以保持报告干净。

---

### 4.3 resync_counter：格雷码多比特计数器同步

#### 4.3.1 概念说明

`resync_counter` 解决的是「把一个**多比特计数器值**安全地从一个时钟域搬到另一个」。**不能**简单地把 `resync_level` 逐比特套上去：多根线各自经过同步链后到达时间不一致，采样瞬间可能拿到「有的位是新值、有的位是旧值」的杂凑组合，差出几十几百都有可能。

解法是**格雷码（Gray code）**。格雷码里相邻两个数只差 1 个比特。所以只要计数器每次只 ±1，它对应的格雷码每次也只有 1 个比特在跳变；那么无论这个跳变位在采样瞬间是否完成，采样结果要么是「跳变前」要么是「跳变后」的合法格雷码，绝不会是杂凑值——解码回二进制后，要么是 \(N\)、要么是 \(N\pm1\)。

二进制 ↔ 格雷的转换（`math_pkg` 里实现）：

\[
\text{gray} = \text{binary} \oplus (\text{binary} \gg 1)
\]

即格雷码的第 \(i\) 位 \(g_i = b_i \oplus b_{i+1}\)，最高位 \(g_{\text{MSB}} = b_{\text{MSB}}\)。反向解码是从高位向低位做累积异或：\(b_i = b_{i+1} \oplus g_i\)。

> ⚠️ 源码警告：本实体**假设计数器每次只 ±1**。若一次跳多个值，可能多位同时变化，格雷码的安全性失效，输出会出现错误值。源码用一条断言保护这一点。

#### 4.3.2 核心流程

```
clk_in 域                                   clk_out 域
---------                                   ----------
counter_in (二进制)
   │ to_gray
   ▼
counter_in_gray  ──async_reg 链──>  counter_out_gray
   (dont_touch FF)                    │ from_gray
                                      ▼
                                  counter_out (二进制)
```

- `clk_in` 进程：把 `counter_in` 转 `to_gray`，存入 `counter_in_gray`（一个 `clk_in` FF，标 `dont_touch`）。同时断言「Gray 码与上一拍相比汉明距离 ≤ 1」。
- `clk_out` 进程：两级 `async_reg` 链 `counter_in_gray_p1` / `counter_out_gray` 同步这个格雷码向量。
- 输出：`from_gray(counter_out_gray)` 解码回二进制，可选再打一拍（`pipeline_output`）。

#### 4.3.3 源码精读

generic。`width` 必填，决定计数器位宽；`pipeline_output` 控制是否在 `from_gray` 后多一级输出寄存器（改善时序、多一拍延迟）；`assert_false_on_counter_jumps` 可关闭「跳变>1」断言：

[modules/resync/src/resync_counter.vhd:L47-L66](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_counter.vhd#L47-L66) — 注意 `counter_in : u_unsigned(default_value'range)`，位宽由 `default_value` 推导；`u_unsigned` 是本项目通用的无符号向量类型，用于可综合算术。

属性声明。和前两个实体一样，送入 async_reg 链的源 FF 标 `dont_touch`，链上两级标 `async_reg`：

[modules/resync/src/resync_counter.vhd:L70-L80](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_counter.vhd#L70-L80) — `counter_in_gray_p1` 与 `counter_out_gray` 是 async_reg 链。

`clk_in` 进程：转格雷码 + 跳变断言：

[modules/resync/src/resync_counter.vhd:L84-L95](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_counter.vhd#L84-L95) — 第 90 行 `assert hamming_distance(to_gray(counter_in), counter_in_gray) <= 1` 正是「每次只 ±1」的守卫。

`clk_out` 进程（async_reg 链）与输出：

[modules/resync/src/resync_counter.vhd:L98-L123](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_counter.vhd#L98-L123) — `pipeline_output` 为真时把 `from_gray` 结果再寄存一拍。

支撑函数在 `math_pkg`：

[modules/math/src/math_pkg.vhd:L331-L348](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/math_pkg.vhd#L331-L348) — `to_gray` 用 `value xor ("0" & value_shifted_right_1)`；`from_gray` 从 MSB 向下累积异或。

[modules/math/src/math_pkg.vhd:L350-L356](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/math/src/math_pkg.vhd#L350-L356) — `hamming_distance` 即两个向量异或后 `count_ones`（`count_ones` 来自 u2-l2 的 `types_pkg`）。

约束侧，多比特 CDC 要同时约束两件事：① 整条路径的延迟上界（`set_max_delay`）；② **字内偏斜（bus skew）**——保证采样瞬间「最多只有一个比特在跳变」。这正是格雷码拓扑能可靠工作的物理保证：

[modules/resync/scoped_constraints/resync_counter.tcl:L33-L50](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_counter.tcl#L33-L50) — `set_bus_skew` 取 `clk_in` 周期为字内偏斜上限；`set_max_delay -datapath_only` 限定路径延迟；最后对 CDC-6「多比特被 ASYNC_REG 同步」告警建豁免（因为格雷码 + 正确约束已保证安全）。

#### 4.3.4 代码实践

**目标**：让一个 8 位计数器在 `clk_in` 域连续递增，验证 `clk_out` 域读到的值始终是合法的「当前值或上一拍值」，绝不出现乱码。

直接用现成的 [tb_resync_counter.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_counter.vhd)：

[modules/resync/test/tb_resync_counter.vhd:L52-L61](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_counter.vhd#L52-L61) — `apply_and_check` 把 `counter_in` 设成某个值，等待不超过 `max_resync_time`，然后断言 `counter_out` 等于该值，并确认之后保持稳定。

[modules/resync/test/tb_resync_counter.vhd:L66-L74](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_counter.vhd#L66-L74) — 测试用例既「向上数到顶再回绕两次」，也「向下数到底」，覆盖 ±1 两个方向。

**操作步骤**：
1. 配好环境后仿真 `tb_resync_counter`（generic `pipeline_output` 取 `true`/`false` 两组，见 [module_resync.py:L35-L39](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/module_resync.py#L35-L39)）。
2. 阅读 `max_resync_time` 的公式：`clk_in_period + 2*clk_out_period + pipeline_output*clk_out_period`，体会「两级 async_reg 链 + 可选流水」为什么对应这个延迟上界。

**需要观察的现象**：每设一个 `counter_in`，`counter_out` 在限定时间内收敛到该值，且收敛前不会读到非法大数。

**预期结果**：所有 `check_equal(counter_out, value)` 通过。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果把计数器值直接逐比特用 `resync_level` 同步，最坏会读出什么样的错误？
**答案**：各位到达时间不同步，采样瞬间可能拿到「部分位新、部分位旧」的组合。例如从 `0111`→`1000`（8 个比特位全变），可能采到任意中间态，差值可达数十以上。

**练习 2**：为什么 `resync_counter` 的输入允许由 LUT 驱动，不像 `resync_level` 那样建议输入寄存器？
**答案**：`clk_in` 进程把 `to_gray(counter_in)` 存进带 `dont_touch` 的 FF `counter_in_gray`，送入 async_reg 链的永远是干净 FF 输出，所以上游是 LUT 也无妨。

**练习 3**：`hamming_distance(to_gray(counter_in), counter_in_gray) <= 1` 这条断言在保护什么？
**答案**：保证相邻两拍 `counter_in` 的格雷码至多差 1 个比特，等价于「二进制计数器每次只 ±1」。一旦违反，多位同时跳变会破坏格雷码安全性，输出会出错。

---

### 4.4 resync_cycles：按拍数同步（周期级延迟测量）

#### 4.4.1 概念说明

`resync_cycles` 解决一个更刁钻的需求：输入信号在 `clk_in` 域被置位**多少拍**，输出就在 `clk_out` 域被置位**多少拍**——即便两个时钟频率不同，也要保证「拍数守恒」。典型用途是把一个慢时钟域里的「持续 N 拍有效」事件，在快时钟域里还原成同样次数的有效脉冲，或者反过来。

它的做法很巧妙：在 `clk_in` 域数输入被置位的拍数（一个计数器），用 `resync_counter` 把这个**计数值**同步过去，然后在 `clk_out` 域用一个本地计数器「追平」这个同步过来的目标值——只要本地还没追上，就持续置位输出并自增，直到相等。

因为复用了 `resync_counter`，它天然继承了「输入计数器每次只 ±1」的约束——而这里的输入计数器正是「每拍最多 +1」，刚好满足。

#### 4.4.2 核心流程

```
clk_in 域                          clk_out 域
---------                          ----------
data_in (高电平期间)
   │ 每拍 +1
   ▼
counter_in ──resync_counter──> counter_in_resync
                                   │
                          counter_out（本地追平计数器）
                                   │
                          if counter_out != counter_in_resync:
                              data_out <= active; counter_out <= counter_out + 1
                          else:
                              data_out <= inactive
```

- `clk_in` 侧：`data_in` 有效时 `counter_in` 每拍 +1（记录输入置位了多少拍）。
- 中间：实例化 `resync_counter` 把 `counter_in` 同步到 `clk_out` 域得 `counter_in_resync`。
- `clk_out` 侧：本地 `counter_out` 每拍比较，未追平就置位输出并 +1，追平就撤销输出。于是 `data_out` 被置位的总拍数 == `data_in` 被置位的总拍数。

#### 4.4.3 源码精读

generic：`counter_width` 决定能容忍的最大「未追平」差值（见下方限制）；`active_level` 默认 `'1'`：

[modules/resync/src/resync_cycles.vhd:L44-L56](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_cycles.vhd#L44-L56)。

`clk_in` 侧计数：

[modules/resync/src/resync_cycles.vhd:L66-L74](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_cycles.vhd#L66-L74) — `data_in` 有效即 `counter_in <= counter_in + 1`。

内部直接实例化 `resync_counter`（这就是「本讲依赖关系」的体现——`resync_cycles` 建立在 `resync_counter` 之上）：

[modules/resync/src/resync_cycles.vhd:L77-L88](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_cycles.vhd#L77-L88) — 注意它没有自己的 scoped_constraints，而是复用 `resync_counter.tcl`，头注释也强调了这一点。

`clk_out` 侧「追平」逻辑与仿真专用断言：

[modules/resync/src/resync_cycles.vhd:L91-L102](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_cycles.vhd#L91-L102) — 追平则撤销输出，否则置位并自增。

[modules/resync/src/resync_cycles.vhd:L105-L123](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_cycles.vhd#L105-L123) — 用 `in_simulation`（u2-l2）包裹的断言，检查「输入过密导致输出会丢」的情况。

> **counter_width 的限制**：当 `clk_out` 比 `clk_in` 慢、且输入连续置位很多拍时，`counter_in` 增长可能快于 `counter_out` 能追平的速度，差值一旦超过计数范围就丢失输出。增大 `counter_width` 提高容限。

#### 4.4.4 代码实践

**目标**：在 `clk_in` 域把 `data_in` 置位 100 拍，验证 `clk_out` 域 `data_out` 恰好被置位 100 拍（哪怕 `clk_out` 频率不同）。

直接用现成 [tb_resync_cycles.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_cycles.vhd)：

[modules/resync/test/tb_resync_cycles.vhd:L64-L86](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_cycles.vhd#L64-L86) — `test(num_cycles)` 过程：在 `clk_in` 域把 `data_in` 拉高 `num_cycles` 拍，然后断言 `clk_out` 域计数到的 `num_data_out == num_cycles`。

[modules/resync/test/tb_resync_cycles.vhd:L91-L101](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_cycles.vhd#L91-L101) — 当 `clk_out` 较慢时，测试特意取 `num_cycles = 2**counter_width`（即 8），这正是「恰好不溢出」的边界。

**操作步骤**：仿真该 testbench 的三组配置（`output_clock_is_faster`/默认/`output_clock_is_slower`，见 [module_resync.py:L41-L51](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/module_resync.py#L41-L51)）。

**需要观察的现象**：无论 `clk_out` 快或慢，`data_out` 置位总拍数都等于 `data_in` 置位拍数。

**预期结果**：`check_equal(num_data_out, num_cycles)` 通过。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`resync_cycles` 内部为何实例化的是 `resync_counter` 而不是 `resync_level`？
**答案**：它要跨域传递的是一个**多比特计数值**，必须用格雷码同步；`resync_level` 只能同步单比特电平，会读到乱码。

**练习 2**：当 `clk_out` 比 `clk_in` 慢很多、输入又长时间持续高电平，会发生什么？
**答案**：`counter_in` 增长快于 `counter_out` 追平的速度，差值可能超过 `counter_width` 范围，导致输出拍数丢失。仿真中会被「Too dense inputs, outputs will be lost!」断言抓到；增大 `counter_width` 可提高容限。

**练习 3**：为什么 `resync_cycles` 没有自己的 `.tcl` 约束文件？
**答案**：它把跨域工作完全委托给了内部的 `resync_counter`，后者已有 `resync_counter.tcl`。约束随实例层级自动应用（`read_xdc -ref resync_counter`），所以无需重复。

---

## 5. 综合实践

**任务**：为四种信号各选对同步实体，并解释「选错会怎样」。请填写下表（先自己想，再对照答案）：

| 待同步信号 | 应选实体 | 选错（用 `resync_level`）的后果 |
| --- | --- | --- |
| 一个慢速软件可读的「使能开关」 | ？ | ？ |
| 每秒最多一次的「按键按下」单拍脉冲 | ？ | ？ |
| 异步 FIFO 的 12 位读指针 | ？ | ？ |
| 慢时钟域里持续 50 拍的「忙」标志，要在快时钟域还原成 50 拍 | ？ | ？ |

**参考答案**：

| 待同步信号 | 应选实体 | 用错 `resync_level` 的后果 |
| --- | --- | --- |
| 慢速使能开关（准静态电平） | `resync_level` | （本就正确） |
| 稀疏单拍脉冲 | `resync_pulse` | 脉冲大概率被静默丢掉，开关不翻转 |
| 12 位读指针 | `resync_counter` | 各位到达不同步，采到杂凑值，FIFO 深度判断完全错乱 |
| 持续 50 拍的忙标志（拍数守恒） | `resync_cycles` | 只能得到一个跟随的电平，无法保证对端也是 50 拍 |

**进阶**：阅读 [module_resync.py:L127-L213](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/module_resync.py#L127-L213) 的 `get_build_projects`，观察 `resync_counter` 与 `resync_cycles` 在不同位宽下的 LUT/FF/逻辑级数回归断言。例如 `resync_counter` width=8 时断言 `LutRams=0, TotalLuts=11, Ffs=24, MaximumLogicLevel=3`——试着解释：**为什么 FF 数大约是位宽的 3 倍？**（提示：`clk_in` 一级格雷 FF + 两级 async_reg 链 ≈ 3×width。）这一步把「电路结构」与「资源数字」对应起来，是本讲的收口练习。

---

## 6. 本讲小结

- **亚稳态不可消除，只能靠同步器把 MTBF 做到可接受**；hdl-modules 统一用「两级 `async_reg` FF + 布局到同 slice + `dont_touch`」的拓扑，再用 `.tcl` 约束限定路径延迟。
- **`resync_level`** 处理准静态电平，最省资源；`enable_input_register` 同时承担「抗毛刺」和「确定性延迟」两个独立职责。
- **`resync_pulse`** 用「脉冲↔电平转换」传送脉冲；反馈门控（默认开启）保证一簇脉冲至少产出一个，但**不保证脉冲数精确**。
- **`resync_counter`** 用格雷码让「每次只 ±1 的多比特计数器」可安全跨域；断言「汉明距离 ≤ 1」守护这一前提，`.tcl` 用 `set_bus_skew` 保证字内最多一位在跳。
- **`resync_cycles`** 复用 `resync_counter` 实现「拍数守恒」，适合慢→快或快→慢的持续有效事件传递。
- **四者的共同契约**：送入 async_reg 链的必须是带 `dont_touch` 的真实 FF；都要配合各自（或被复用实体）的 scoped_constraints 才能可靠工作。

---

## 7. 下一步学习建议

- **下一步学 u3-l2**（总线与握手的跨域同步及约束）：本讲只处理「单比特电平/脉冲/计数器」，下一讲进入**多比特数据总线**的跨域，看 `resync_twophase` / `resync_twophase_handshake` 如何用两相握手安全传整组向量，并精读 `resync_level.tcl` / `resync_counter.tcl` 的约束写法。
- **横向预习 u4-l2**（异步 FIFO）：异步 FIFO 用格雷码读写指针 + `resync_counter` 实现安全跨域，是本讲计数器同步的最重要应用，学完会有「原来如此」的闭环感。
- **源码延伸阅读**：仓库里还有 `resync_slv_level`（向量版电平同步）、`resync_sticky_level`（粘性电平）、`resync_rarely_valid`（稀疏有效握手）等变体，思路都建立在本讲四个实体之上，可按需选读。
