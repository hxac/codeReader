# 可配置 TDM 与多路复用 cfg/tdm_mux

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `psi_common_par_tdm_cfg` / `psi_common_tdm_par_cfg` 相比 u8-l2 的非 cfg 版本多了什么——**运行时可配置的有效通道数** `enabled_ch_i`，以及它带来的握手简化。
- 读懂 `par_tdm_cfg` 用单个 `ChCnt` 计数器替代移位位图、并在 `ChCnt=1` 时拉 `last_o` 的实现。
- 读懂 `tdm_par_cfg` 如何用 `last_i` 提前结束一帧、并用 `partially_ones_vector` 生成通道使能掩码把未启用通道清零。
- 理解 `psi_common_tdm_mux` 实际是一个**帧内通道选通提取器**（按 `ch_sel_i` 从一帧 TDM 里挑出一个通道），并说清其帧对齐的输出时序。
- 正确解释 `str_del_g` 的作用——它是**测试平台级**的选通间隔参数，不是 DUT 的 generic。
- 了解 `generators/psi_common_par_tdm_wX.py` 代码生成器的角色：它给**非 cfg 的 `par_tdm`** 套一层「定宽数组端口」外壳，与 cfg 变体无关。

## 2. 前置知识

本讲默认你已经掌握：

- **TDM 与并行的等价表示**：N 路×W 位数据，并行是 `N×W` 位宽总线一拍传完，TDM 是 W 位窄总线连续 N 拍传完（见 u8-l2）。
- **隐式通道循环约定**：通道从 0 开始，放最低位且最先收发，速率相同时无需通道号旁路信号（见 u1-l4、u8-l2）。
- **二进程 record 设计法**：寄存器收进 record，`r` 现态 / `r_next` 次态，组合进程算次态、时序进程打拍复位（见 u7-l1）。
- `psi_common_logic_pkg` 的 `shift_right`（带填充逻辑右移）与 `partially_ones_vector`（生成「低 N 位为 1」的掩码，见 u2-l2）。

本讲会用到的术语：

- **有效通道数（enabled channels）**：一帧里真正参与收发的通道数，可小于硬件最大通道数 `ch_nb_g`。运行时由端口 `enabled_ch_i` 指定。
- **帧（frame）**：一次完整的 0..N-1 通道循环。
- **选通提取**：从一帧 TDM 流里，按地址挑出某一个通道的值并单独输出。

## 3. 本讲源码地图

| 文件 | 作用 |
|:-----|:-----|
| [hdl/psi_common_par_tdm_cfg.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_tdm_cfg.vhd) | 并行→TDM，**运行时可配置通道数**，单计数器实现，valid-only 握手。 |
| [hdl/psi_common_tdm_par_cfg.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdm_par_cfg.vhd) | TDM→并行，**运行时可配置通道数**，支持 `last_i` 提前结束，未启用通道清零。 |
| [hdl/psi_common_tdm_mux.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdm_mux.vhd) | TDM 帧内通道选通提取器：按 `ch_sel_i` 从一帧里挑出一个通道输出。 |
| [hdl/psi_common_logic_pkg.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_logic_pkg.vhd) | 提供 `partially_ones_vector`（通道使能掩码）与 `shift_right`（逐通道移位）。 |
| [generators/psi_common_par_tdm_wX.py](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/psi_common_par_tdm_wX.py) | 代码生成器：为非 cfg 的 `par_tdm` 按指定宽度生成带数组端口的外壳。 |
| testbench/psi_common_tdm_mux_tb/psi_common_tdm_mux_tb.vhd | `tdm_mux` 测试平台，含 `str_del_g` 选通间隔参数。 |
| sim/config.tcl | 回归注册表，登记了 `tdm_mux_tb` 的 `str_del_g=0` 与 `str_del_g=5` 两组运行。 |

---

## 4. 核心概念与源码讲解

### 4.1 cfg 变体：运行时可配置的有效通道数

#### 4.1.1 概念说明

u8-l2 的 `par_tdm` / `tdm_par` 把通道数 `ch_nb_g` 钉死在综合期：一帧永远是 `ch_nb_g` 个通道。但很多真实场景里，**一帧到底有几个有效通道是运行时才知道的**——例如一个采集系统今天采 8 路、明天只采 3 路，其余通道应当被跳过而不是照常串行化。

cfg 变体就是为此而生：硬件宽度（`ch_nb_g` × `ch_width_g`）仍在综合期固定，但**有效通道数**改由运行时端口 `enabled_ch_i` 指定（整数，范围 `0` 到 `ch_nb_g`，默认 `ch_nb_g`）。由此带来两个连带设计：

1. **握手简化**：因为有效通道数会变、且通常由上游节拍驱动，cfg 变体去掉了 `rdy`/`rdy_o` 反压与 `last_i`（`par_tdm_cfg` 侧）等端口，退化为 **valid-only** 握手，上游节拍频率须满足吞吐约束。
2. **掩码/计数替代位图**：`par_tdm_cfg` 用一个随 `enabled_ch_i` 重载的倒计数器替代了非 cfg 版的 `VldSr`/`LastSr` 移位位图；`tdm_par_cfg` 用 `partially_ones_vector` 直接算出「哪些通道有效」的位掩码。

#### 4.1.2 核心流程

**`par_tdm_cfg`（并行→TDM）**：

1. `vld_i='1'` 那拍：把整组并行数据装入 `ShiftReg`，并把通道计数 `ChCnt` 设为 `enabled_ch_i`（要发几个通道）。
2. 之后每拍把 `ShiftReg` 右移 `ch_width_g` 位，最低通道先送出；同时 `ChCnt` 减 1。
3. `vld_o` = (`ChCnt ≠ 0`)；`last_o` = (`ChCnt = 1`)，即最后一个要发的通道拉高。
4. `ChCnt` 减到 0 后停住，直到下一次 `vld_i` 再次重载。

**`tdm_par_cfg`（TDM→并行）**：

1. 每来一个 `vld_i` 样本，按当前位置 `ChCounter` 填进 `ParallelReg` 对应通道槽，`ChCounter` 自增。
2. 当 `ChCounter` 达到 `enabled_ch_i`，或收到 `last_i='1'`：把 `ParallelReg` 锁存到输出寄存器，用 `partially_ones_vector(ch_nb_g, enabled_ch_i)` 生成掩码，拉 `vld_o='1'`。
3. 输出端按掩码：启用通道输出真实数据，未启用通道输出 0。

#### 4.1.3 源码精读

`par_tdm_cfg` 的可配置通道端口——`enabled_ch_i` 直接出现在 entity 端口里，这是与非 cfg 版最显著的区别：

[hdl/psi_common_par_tdm_cfg.vhd:23-35](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_tdm_cfg.vhd#L23-L35) — `enabled_ch_i` 是 `integer range 0 to ch_nb_g` 的运行时端口，默认值就是最大通道数 `ch_nb_g`；同时注意它**没有** `rdy_o`/`rdy_i`/`last_i`，是 valid-only 接口。

倒计数器替代移位位图的核心逻辑（组合进程）：

```vhdl
if vld_i = '1' then
  v.ShiftReg := dat_i;
  v.ChCnt    := enabled_ch_i;          -- 重载：要发这么多通道
else
  v.ShiftReg := shift_right(r.ShiftReg, ch_width_g);
  if r.ChCnt /= 0 then
    v.ChCnt := r.ChCnt - 1;            -- 每发一个通道减 1
  end if;
end if;
```

[hdl/psi_common_par_tdm_cfg.vhd:53-61](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_tdm_cfg.vhd#L53-L61) — `vld_i` 一来即重载 `ShiftReg` 与 `ChCnt`；空闲拍则右移并发数。对比 u8-l2 的 `par_tdm` 用 `VldSr(0)` 当 `vld_o`，这里改用 `ChCnt/=0` 当 `vld_o`、`ChCnt=1` 当 `last_o`。

`last_o` 的产生：

[hdl/psi_common_par_tdm_cfg.vhd:70-74](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_tdm_cfg.vhd#L70-L74) — 当 `ChCnt=1`（即正在送出最后一个有效通道）时 `last_o='1'`，这正是 AXI-S 的 `TLAST` 语义，告诉下游「这一帧到这就结束了」。

再看 `tdm_par_cfg` 如何用掩码处理未启用通道。锁存与掩码生成：

[hdl/psi_common_tdm_par_cfg.vhd:74-79](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdm_par_cfg.vhd#L74-L79) — 当收满 `enabled_ch_i` 个通道或收到 `last_i` 时，锁存输出、把 `EnChannelsMask` 设为 `partially_ones_vector(ch_nb_g, enabled_ch_i)`。注意 `ChCounter` 被重置为 `to_integer(unsigned'('0' & vld_i))`——若此刻 `vld_i='1'`（背靠背无间隔的流），当前样本直接算作下一帧的通道 0，避免丢样本。

输出端按掩码分发：

```vhdl
parallel_assign : for i in 0 to ch_nb_g - 1 loop
  if r.EnChannelsMask(i) = '1' then
    dat_o(...) <= r.Odata(...);   -- 启用通道：真实数据
  else
    dat_o(...) <= (others => '0');-- 未启用通道：清零
  end if;
end loop;
```

[hdl/psi_common_tdm_par_cfg.vhd:82-88](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdm_par_cfg.vhd#L82-L88) — 用 `for` 循环逐通道判断掩码，启用位透传、未启用位补零。这就是「有效通道数可变」时输出总线宽度却始终是 `ch_nb_g*width_g` 的处理方式。

`partially_ones_vector` 本体（来自 logic_pkg，cfg 变体重度复用）：

[hdl/psi_common_logic_pkg.vhd:91-104](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_logic_pkg.vhd#L91-L104) — 返回长度 `size` 的向量，最低 `ones_nb` 位为 `'1'`、其余为 `'0'`。`partially_ones_vector(ch_nb_g, enabled_ch_i)` 恰好得到「通道 0..enabled_ch_i-1 有效」的掩码。

#### 4.1.4 代码实践

**实践目标**：理解 `enabled_ch_i` 如何改变 `par_tdm_cfg` 的输出长度，验证 `last_o` 的位置随有效通道数移动。

**操作步骤（源码阅读型 + 仿真型）**：

1. 打开 [testbench/psi_common_par_tdm_cfg_tb/psi_common_par_tdm_cfg_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_par_tdm_cfg_tb/psi_common_par_tdm_cfg_tb.vhd)，确认 DUT 实例化为 3 通道×8 位（`channel_count_g=3`）。
2. 定位「Do less channels that maximum」测试段：先设 `enabled_channels_i <= 2`，注入 `0A/0B/0C`；再设 `enabled_channels_i <= 1`，注入 `0D/0E/0F`。
3. 对照输出检查段（`p_outp` 中 `TestCase = 2`）：`enabled=2` 时期望只看到 `0A`、`0B` 两拍且 `last_o` 在 `0B` 那拍为 1、随后 `vld_o` 归零；`enabled=1` 时只看到 `0D` 一拍且 `last_o` 立即为 1。
4. 若有仿真器，用 `sim/run.tcl`（Modelsim）或 `sim/runGhdl.tcl`（GHDL）跑 `psi_common_par_tdm_cfg_tb`。

**需要观察的现象**：

- 有效通道数从 3 降到 2，`last_o` 提前一拍出现（在第 2 个输出字而非第 3 个）。
- `enabled=1` 时 `vld_o` 只高一拍就伴随着 `last_o`。
- 注入了 `0C`/`0E`/`0F` 但因为超出有效通道数，它们**不会**出现在输出中。

**预期结果**：TB 自检全部通过、无 `###ERROR###`。若无法本地运行，标注「待本地验证」，但上述通道计数与 `last_o` 位置关系可直接由源码 `ChCnt=1` 判据推出。

#### 4.1.5 小练习与答案

**练习 1**：`par_tdm_cfg` 的 `ChCnt` 为什么类型、范围多大？为什么 `enabled_ch_i` 允许取 0？

答案：`ChCnt` 是 `integer range 0 to ch_nb_g`（见 [hdl/psi_common_par_tdm_cfg.vhd:41](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_tdm_cfg.vhd#L41)）。`enabled_ch_i=0` 表示本帧不发任何通道，`vld_i` 重载后 `ChCnt` 立即为 0，于是 `vld_o` 一直为 0、不产生输出，是合法的「空帧」。

**练习 2**：`tdm_par_cfg` 同时支持「收满 `enabled_ch_i` 个通道」和「收到 `last_i`」两种结束条件，二者谁优先？

答案：二者是「或」关系（[hdl/psi_common_tdm_par_cfg.vhd:74](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdm_par_cfg.vhd#L74) `if r.ChCounter = enabled_ch_i or r.TdmLast_d = '1'`），任一满足即锁存输出。这意味着即便没到 `enabled_ch_i`，一个 `last_i` 也能提前结束本帧（短包）。

---

### 4.2 tdm_mux：TDM 帧内通道选通提取

#### 4.2.1 概念说明

`psi_common_tdm_mux` 的名字里带「mux（复用器）」，但它真正做的是**反向操作——从一路 TDM 帧里选通提取出一个通道**。源码头注释直言：

> This is a very basic mux for Time Division Multiplexed data input.

它解决的问题是：当数据以 TDM 形式到达（0,1,2,…,N-1 轮流），而下游只关心其中**某一个固定通道**时，如何低成本地把那一拍数据挑出来。典型用途是在多路 TDM 监测流里，按寄存器配置的 `ch_sel_i` 把「当前关注的通道」单独导出给某个处理单元。

> ⚠️ **澄清一个常见误解**：`tdm_mux` **不能**把两路独立的 TDM 流合并成一路。它只有一个 TDM 数据输入 `tdm_dat_i`，按 `ch_sel_i` 从**这一帧**里挑一个通道。若要真正合并两路流，应考虑在并行侧用 `wconv_*` 或外部仲裁，而非本组件。本讲第 5 节的实践会据此重新设计。

#### 4.2.2 核心流程

`tdm_mux` 内部有一个随输入选通自由运行的帧位置计数器 `count_s`：

1. 每个 `tdm_vld_i='1'` 脉冲使 `count_s` 自增，到 `ch_nb_g-1` 后回绕到 0——`count_s` 隐式地指明「当前这一拍是第几通道」。
2. 当 `count_s` 等于 `ch_sel_i` 且 `tdm_vld_i='1'`：把当前 `tdm_dat_i` 锁存进 `tdm_dat_s`，并置位捕获标志 `tdm_str_s`。
3. 当帧绕回 `count_s=0`（帧边界）且本帧捕获过（`tdm_str_s='1'`）：把锁存值送到 `tdm_dat_o`，拉一拍 `tdm_vld_o`，清 `tdm_str_s`。
4. 其余周期 `tdm_vld_o='0'`。

因此输出**每帧至多一个有效字**，且对齐到帧边界（`count_s=0`）发出，数据来自上一帧被选中的那一通道。

#### 4.2.3 源码精读

entity 与端口——注意只有单路 TDM 输入和一路选通输出：

[hdl/psi_common_tdm_mux.vhd:20-31](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdm_mux.vhd#L20-L31) — generics 为 `rst_pol_g`/`ch_nb_g`/`width_g`；`ch_sel_i` 位宽由 `log2ceil(ch_nb_g)` 推导。**没有 `str_del_g`**——它不存在于 DUT（这一点很重要，见 4.3）。

帧位置计数器随输入选通自增并回绕：

```vhdl
if tdm_vld_i = '1' then
  if count_s = ch_nb_g - 1 then
    count_s <= 0;
  else
    count_s <= count_s + 1;
  end if;
end if;
```

[hdl/psi_common_tdm_mux.vhd:52-58](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdm_mux.vhd#L52-L58) — `count_s` 是 free-running 的模 `ch_nb_g` 计数器，由 `tdm_vld_i` 驱动，隐式定义帧边界（每 `ch_nb_g` 个选通为一帧）。

帧边界处输出捕获值：

[hdl/psi_common_tdm_mux.vhd:61-67](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdm_mux.vhd#L61-L67) — 仅当 `count_s=0`（帧起点）且 `tdm_str_s='1'`（上一帧捕获过选中通道）时输出，否则 `vld_o` 保持 0。源码注释 "output data after last channel was latched (i.e. if counter = 0)" 与之一致。

通道匹配捕获：

[hdl/psi_common_tdm_mux.vhd:70-75](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdm_mux.vhd#L70-L75) — 当 `unsigned(ch_sel_i) = count_s` 且 `tdm_vld_i='1'`，锁存数据并置 `tdm_str_s`。注意此 `if` 在输出 `if` 之后、属同一时序进程的顺序语句，但因信号读取用的是本拍起始值，故「输出用的是上一帧的捕获、本帧的捕获下一帧才生效」，实现帧对齐。源码头注释 "Latency is two clock cycles after the falling edge strobe input" 是对这种帧对齐时序的简化描述。

#### 4.2.4 代码实践

**实践目标**：跟踪 `ch_sel_i` 指向的通道如何被提取并延迟到帧边界输出。

**操作步骤（源码阅读型）**：

1. 设想 `ch_nb_g=4`、`ch_sel_i=2`，输入每拍送入「通道号本身」作为数据（即第 0 拍送 0、第 1 拍送 1、……），与 TB 的激励手法一致。
2. 画出 `count_s` 序列：0→1→2→3→0→1→…
3. 标记捕获时刻：`count_s=2` 且 `tdm_vld_i='1'` 那拍，`tdm_dat_s` 装入「2」，`tdm_str_s` 置 1。
4. 标记输出时刻：下一个 `count_s=0` 那拍，`tdm_dat_o=2`、`tdm_vld_o='1'`。

**需要观察的现象**：

- 每完整一帧（4 个选通）输出恰好一个字，值等于 `ch_sel_i`。
- 改 `ch_sel_i` 到 0：捕获发生在帧首，输出在**下一帧**的帧首。
- `tdm_vld_o` 的占空比约为 \(1/\text{ch\_nb\_g}\)。

**预期结果**：输出值恒等于所选通道号（这正是 `tdm_mux_tb` 的自检断言——[testbench/psi_common_tdm_mux_tb/psi_common_tdm_mux_tb.vhd:206-211](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_tdm_mux_tb/psi_common_tdm_mux_tb.vhd#L206-L211) 断言 `InChSel_sti = OutTdmDat_obs`）。若运行仿真待本地验证，时序关系仍可由源码推出。

#### 4.2.5 小练习与答案

**练习 1**：若把 `ch_sel_i` 设为大于等于 `ch_nb_g` 的值会怎样？

答案：`ch_sel_i` 位宽是 `log2ceil(ch_nb_g)`（[hdl/psi_common_tdm_mux.vhd:26](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdm_mux.vhd#L26)），端口物理上无法表示 ≥ `ch_nb_g` 的值，所以这是「不可达输入」，不必处理。

**练习 2**：为什么 `tdm_mux` 的输出对齐到帧边界（`count_s=0`）而不是在捕获那一拍立即输出？

答案：因为捕获发生在 `count_s=ch_sel_i` 这一拍，若立即输出，下游需要知道「这是哪一帧的哪一通道」；而统一到帧边界（`count_s=0`）输出，使输出节拍固定、与帧节奏对齐，下游只需在 `vld_o` 高的那拍取值即可，时序规整。

---

### 4.3 str_del_g：选通间隔（测试平台级参数）

#### 4.3.1 概念说明

`str_del_g` 在大纲里被列为「延迟」相关参数，**但它不是 `tdm_mux` 这个 DUT 的 generic**。准确地说：

- `hdl/psi_common_tdm_mux.vhd` 的 generics 只有 `rst_pol_g`、`ch_nb_g`、`width_g` 三个，**没有 `str_del_g`**。
- `str_del_g` 是 [testbench/psi_common_tdm_mux_tb/psi_common_tdm_mux_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_tdm_mux_tb/psi_common_tdm_mux_tb.vhd) 的 generic（默认 0），控制 TB 激励里**相邻两个 TDM 选通脉冲之间插入多少个空闲时钟周期**。

它的意义在于给 `tdm_mux` 施压两种节拍场景：

- `str_del_g=0`：选通背靠背（连续每拍一个有效通道），最紧凑、最高吞吐。
- `str_del_g=5`：每个选通后空 5 拍，模拟稀疏、不连续的 TDM 流，检验 `count_s` 是否仍能在间隔下正确推进、捕获是否仍对齐帧边界。

#### 4.3.2 核心流程

TB 里的 `par_tdm_tb_proc` 过程在生成每个通道样本时检查 `str_del_g`：

1. 正常送出一拍 `str_o='1'` + 数据。
2. 若 `str_del_g > 0`：先 `wait` 一拍把 `str_o` 拉低，再循环 `wait` 到累计 `str_del_g-1` 拍，从而在两次有效选通间留出 `str_del_g` 个间隔周期。

DUT 本身不感知 `str_del_g`——它只看 `tdm_vld_i` 的上升/电平。间隔的存在与否不应改变 `tdm_mux` 的逻辑正确性（因为 `count_s` 只在 `tdm_vld_i='1'` 时推进）。

#### 4.3.3 源码精读

`str_del_g` 的声明——在 TB entity，不在 DUT：

[testbench/psi_common_tdm_mux_tb/psi_common_tdm_mux_tb.vhd:28-35](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_tdm_mux_tb/psi_common_tdm_mux_tb.vhd#L28-L35) — TB 的 generics 包含 `freq_clock_g`、`freq_str_g`、`num_channel_g`、`data_length_g` 以及 `str_del_g`。DUT 实例化（同文件 190–200 行）只映射了 `rst_pol_g`/`ch_nb_g`/`width_g`，**不映射 `str_del_g`**。

间隔插入逻辑：

```vhdl
str_o <= '1';
if str_del_g > 0 then
  wait until rising_edge(clk);
  str_o <= '0';
  for i in 1 to str_del_g - 1 loop
    wait until rising_edge(clk);
  end loop;
end if;
```

[testbench/psi_common_tdm_mux_tb/psi_common_tdm_mux_tb.vhd:88-95](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_tdm_mux_tb/psi_common_tdm_mux_tb.vhd#L88-L95) — 仅当 `str_del_g>0` 时，每送出一个有效选通后插入 `str_del_g` 个时钟间隔，模拟稀疏节拍。

回归注册表把两组都登记进来：

[sim/config.tcl:346-350](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L346-L350) — `psi_common_tdm_mux_tb` 用 `tb_run_add_arguments` 注册了 `"-gstr_del_g=0"` 与 `"-gstr_del_g=5"` 两个运行组合，回归会对两种节拍各跑一遍。

#### 4.3.4 代码实践

**实践目标**：对比 `str_del_g=0` 与 `str_del_g=5` 下 DUT 行为是否一致，理解该参数只影响激励节拍、不影响 DUT 正确性。

**操作步骤（仿真型）**：

1. 用 `sim/run.tcl`（或 GHDL 的 `sim/runGhdl.tcl`）执行回归；`config.tcl` 会自动对 `tdm_mux_tb` 跑两组。
2. 在波形上分别观察两次运行的 `InTdmVld_sti`：`str_del_g=0` 时连续每拍高；`str_del_g=5` 时每隔约 6 拍才高一次。
3. 同时观察 DUT 的 `count_s`（内部信号，仿真可见）与 `OutTdmVld_obs`。

**需要观察的现象**：

- 两次运行的 `count_s` 推进节奏不同（稀疏 vs 紧凑），但每完成一帧（`ch_nb_g` 个有效选通）都输出一个字。
- 输出值始终等于 `ch_sel_i`，两组都应无 `###ERROR###`。

**预期结果**：两组均通过自检，证明 `tdm_mux` 对选通间隔不敏感。若无法本地运行，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `str_del_g` 放在 TB 而不是 DUT？

答案：DUT 的 `count_s` 只依赖 `tdm_vld_i` 的有效沿，间隔是「激励的节拍特征」而非「DUT 的可配置行为」。把它放在 TB，可针对同一份 DUT 代码用不同节拍施压，验证鲁棒性。

**练习 2**：`str_del_g=5` 时，一帧（`ch_nb_g` 个有效通道）实际要多少个时钟周期？

答案：每个有效选通后跟 `str_del_g=5` 个间隔周期，故每通道约耗时 \(1+5=6\) 拍；一帧 `ch_nb_g` 个通道约耗时 \(6 \times \text{ch\_nb\_g}\) 拍（首拍边界略有差，量级如此）。

---

### 4.4 代码生成器角色

#### 4.4.1 概念说明

psi_common 在 `generators/` 目录下提供若干 Python 脚本，用「模板替换」为组件按指定位宽生成一个**外壳实体**。本讲关注的 [generators/psi_common_par_tdm_wX.py](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/psi_common_par_tdm_wX.py) 针对的是 **u8-l2 的非 cfg 版 `psi_common_par_tdm`**——**不是** cfg 变体。

为什么要生成器？因为 `par_tdm` 的并行输入是一根扁平的 `N×W` 位 `std_logic_vector`，连接时要把每路 W 位信号手动拼成大向量，啰嗦易错。生成器产出一个以**定宽数组类型**（`array of std_logic_vector(W-1 downto 0)`）为端口的包装实体，调用方直接用数组下标接每一路，内部再展开成扁平向量送给 `par_tdm`。

需要强调的是：cfg 变体**不需要**生成器，因为它的运行时可配置能力已经由 `enabled_ch_i` 端口提供；生成器解决的是「固定宽度下端口形态友好」的问题，与非 cfg 版配套。

#### 4.4.2 核心流程

1. 读模板 `generators/snippets/psi_common_par_tdm_wX.vhd`，其中宽度位置用占位符 `<WIDTH>` 标记。
2. 用 `re.sub` 把所有 `<WIDTH>` 替换成命令行传入的宽度整数。
3. 把替换后的内容写到输出文件 `psi_common_par_tdm_w<宽度>.vhd`（默认当前目录，可用 `-dir` 指定）。

生成的实体内部用 `for generate` 把数组逐路拼成扁平向量，再例化原 `psi_common_par_tdm`。

#### 4.4.3 源码精读

生成器脚本本体——逻辑极简：

[generators/psi_common_par_tdm_wX.py:15-21](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/psi_common_par_tdm_wX.py#L15-L21) — 读模板、`re.sub("<WIDTH>", str(width), content)`、写出。注意它生成的实例名映射用的是**非 cfg 版** `psi_common_par_tdm`（同文件 64–76 行的 `i_inst`）。

模板里定宽数组类型与占位符：

[generators/snippets/psi_common_par_tdm_wX.vhd:19-23](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/snippets/psi_common_par_tdm_wX.vhd#L19-L23) — 声明 `psi_common_par_tdm_w<WIDTH>_a is array (natural range <>) of std_logic_vector(<WIDTH>-1 downto 0)`，宽度由 `<WIDTH>` 占位符在生成时确定。

模板内部展开数组为例化 `par_tdm`：

[generators/snippets/psi_common_par_tdm_wX.vhd:60-68](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/snippets/psi_common_par_tdm_wX.vhd#L60-L68) — `g_merge : for i in ... generate` 把数组逐路拼成 `ParallelMerged`，再例化非 cfg 版 `psi_common_par_tdm`。

调用示例（Windows bat）：

[generators/examples/psi_common_par_tdm.bat:1](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/examples/psi_common_par_tdm.bat#L1) — `py -3 ..\psi_common_par_tdm_wX.py -width 12` 生成 12 位宽的外壳。

#### 4.4.4 代码实践

**实践目标**：亲手跑一次生成器，观察 `<WIDTH>` 占位符被替换的过程。

**操作步骤（命令型）**：

1. 进入 `generators/` 目录（或参照 `examples/psi_common_par_tdm.bat` 的相对路径）。
2. 执行 `py -3 psi_common_par_tdm_wX.py -width 12 -dir .`（Linux 下用 `python3 psi_common_par_tdm_wX.py -width 12`）。
3. 打开生成的 `psi_common_par_tdm_w12.vhd`，检查类型名应为 `psi_common_par_tdm_w12_a`、元素宽度为 `12`。

**需要观察的现象**：

- 生成文件里所有原本的 `<WIDTH>` 都变成 `12`。
- entity 名、package 名、类型名都带 `w12` 后缀，可与其他宽度的生成物共存于同一库。

**预期结果**：生成的 `.vhd` 可被 VHDL 工具直接编译，内部例化非 cfg 版 `par_tdm`。该实践依赖本地 Python 环境，若无可标注「待本地验证」，但替换逻辑可由脚本第 18 行直接读出。

#### 4.4.5 小练习与答案

**练习 1**：为什么没有 `psi_common_par_tdm_cfg_wX.py` 这样的 cfg 生成器？

答案：cfg 变体的通道数由运行时端口 `enabled_ch_i` 配置，宽度仍由 generic `ch_width_g` 在综合期定，已经足够灵活，无需再生成「定宽外壳」。生成器主要服务于非 cfg 版的「端口形态友好」需求。

**练习 2**：生成器为何用 `<WIDTH>` 这种文本占位符而不是模板引擎变量？

答案：库选了最轻量的 `re.sub` 字符串替换（[generators/psi_common_par_tdm_wX.py:18](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/psi_common_par_tdm_wX.py#L18)），零依赖、可读。代价是占位符必须全库唯一、不能与正常 VHDL 文本冲突，故用尖括号 `<WIDTH>` 这种 VHDL 里不会出现的形态。

---

## 5. 综合实践

**任务**：用 `tdm_mux` 从一路 TDM 帧中提取可配置的通道，并对照 `str_del_g` 观察节拍影响。

> 说明：原大纲实践描述为「用 tdm_mux 把两路 TDM 流合并」。经核对源码，`tdm_mux` 只有一路 TDM 输入、实为「帧内通道选通提取器」，并不具备合并两路流的能力。故本综合实践按组件真实行为重新设计，仍完整覆盖「使用 tdm_mux + 解释 str_del_g」两个目标。

**步骤**：

1. **复用现成 TB**：`tdm_mux_tb` 已把 64 通道×24 位、100 MHz、选通 0.1 MHz 的场景搭好（[testbench/psi_common_tdm_mux_tb/psi_common_tdm_mux_tb.vhd:29-34](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_tdm_mux_tb/psi_common_tdm_mux_tb.vhd#L29-L34)）。TB 的 `proc_stim` 会把 `ch_sel_i` 从 0 扫到 `num_channel_g-1`。
2. **跑两组节拍**：通过 `sim/config.tcl` 注册的 `-gstr_del_g=0` 与 `-gstr_del_g=5` 各跑一次回归。
3. **核对自检**：TB 在 `OutTdmVld_obs='1'` 时断言 `InChSel_sti = OutTdmDat_obs`（[testbench/psi_common_tdm_mux_tb/psi_common_tdm_mux_tb.vhd:206-211](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_tdm_mux_tb/psi_common_tdm_mux_tb.vhd#L206-L211)），即输出的数据值应等于当前所选通道号。
4. **解释 str_del_g**：写一两句结论——`str_del_g` 是 TB 级参数，控制相邻选通的间隔周期；`str_del_g=0` 为背靠背最高吞吐，`str_del_g=5` 为稀疏节拍；两组都应通过自检，证明 `tdm_mux` 的 `count_s` 帧计数与帧边界输出对节拍间隔不敏感。
5. **延伸思考（可选）**：若确实需要「合并两路 TDM 流」，写出你会选用的方案（如先各用 `tdm_par` 还原成并行，再在并行侧仲裁/拼接，或用 `wconv_n2xn` 聚合），并说明为何 `tdm_mux` 不胜任。

**预期结果**：两组 `str_del_g` 运行均无 `###ERROR###`；能用自己的话说清 `tdm_mux` 的真实功能与 `str_del_g` 的定位。仿真结果若无法本地获取，明确标注「待本地验证」，但结论可由源码与时序逻辑直接推出。

## 6. 本讲小结

- **cfg 变体**（`par_tdm_cfg` / `tdm_par_cfg`）把「有效通道数」从综合期 `ch_nb_g` 下放到运行时端口 `enabled_ch_i`，并把握手简化为 valid-only。
- `par_tdm_cfg` 用**单计数器 `ChCnt`**（重载于 `enabled_ch_i`）替代非 cfg 版的移位位图，在 `ChCnt=1` 时拉 `last_o` 标记帧尾。
- `tdm_par_cfg` 支持 `last_i` 提前结束短包，并用 `partially_ones_vector` 生成通道掩码、把未启用通道**清零**输出。
- `tdm_mux` 是**帧内通道选通提取器**（按 `ch_sel_i` 从一帧里挑一个通道、在帧边界输出），**不是**流合并器——这是本讲对大纲描述的一处重要澄清。
- `str_del_g` 是**测试平台级**参数（属 `tdm_mux_tb`，不在 DUT），控制激励里选通之间的间隔，用于在紧凑与稀疏两种节拍下回归验证。
- 代码生成器 `psi_common_par_tdm_wX.py` 用 `re.sub` 替换 `<WIDTH>` 占位符，为**非 cfg 版 `par_tdm`** 生成定宽数组端口外壳；cfg 变体因已运行时可配，不需要生成器。

## 7. 下一步学习建议

- **继续 TDM/位宽主题**：阅读 [hdl/psi_common_par_ser.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_ser.vhd) 与 `ser_par`，看并串/串并如何与 TDM 配合（u8-l4）。
- **回到节拍源**：若还没看，回顾 `psi_common_strobe_generator`（u6-l1），理解驱动这些 TDM 转换器与 `tdm_mux` 的节拍从何而来。
- **深入仲裁**：当你真正需要合并多路流时，进入 u10-l1 的 `arb_priority` / `arb_round_robin`，那是「多请求者共享一条总线」的正确工具。
- **工程化**：本讲的 cfg TB 是自校验测试平台的范本，可带着这些例子进入 u11-l1 学习 `psi_tb` 工具包与 `###ERROR###` 约定。
