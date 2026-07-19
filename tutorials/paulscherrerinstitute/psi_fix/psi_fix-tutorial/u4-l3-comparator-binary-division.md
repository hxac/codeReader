# 比较器与二进制除法

## 1. 本讲目标

本讲在「简单处理组件实战」单元里继续走通「VHDL 实现 + 验证」闭环，但有意挑选两个**算术味更浓、却各自代表不同验证策略**的组件：

- `psi_fix_comparator`：定点窗口比较器（判断数据是否低于最小阈值或高于最大阈值）。
- `psi_fix_bin_div`：迭代式二进制除法器（计算 Num/Denom）。

学完后你应当能够：

1. 说清比较器如何把 `psi_fix_compare` 封装成「阈值窗口 + 选通脉冲对齐」的流水结构。
2. 用「恢复式除法（restoring division）」的原理讲清 `psi_fix_bin_div` 的迭代状态机，并解释为何要先取绝对值、最后再补符号。
3. **判断一个算术组件该用哪种验证方式**：什么时候一个纯 VHDL 自检测试台就够（比较器），什么时候必须上「Python 位真模型 + preScript 协同仿真」（除法器）。
4. 亲手跟踪 `bin_div` 测试台的 preScript 数据流，讲清「浮点期望值 → 定点位模式整数 → 文本 → VHDL 逐位比对」这条协同仿真链路。

## 2. 前置知识

本讲假设你已经掌握 u1-l4、u2-l1、u2-l2、u3-l2 的内容，特别是：

- **定点格式三元组 \([s,i,f]\)**：`s` 符号位、`i` 整数位、`f` 小数位，总位宽 \(W=s+i+f\)。
- **psi_fix_pkg 运算函数**：`psi_fix_resize / add / sub / abs / neg / shift_left / compare` 的签名与「库级默认 `trunc/wrap`、组件层默认 `round/sat`」的区别。
- **位真双模型与协同仿真套路**：Python 模型是黄金参考，经 `psi_fix_get_bits_as_int` 把定点值写成「位模式的有符号整数」文本，VHDL 测试台用 `###ERROR###` 约定逐位比对。

本讲用到的一条新结论先点明：**`psi_fix_compare` 是 VHDL-only 函数**，Python 侧 `model/psi_fix_pkg.py` 并没有同名镜像（Python 侧只有 `upper_bound / lower_bound / in_range`）。这看似是个小细节，却直接决定了两个组件为何采用截然不同的验证方式——这是第 4.3 节的核心。

## 3. 本讲源码地图

| 文件 | 作用 |
|:--|:--|
| [hdl/psi_fix_comparator.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_comparator.vhd) | 比较器组件实现：单进程、阈值窗口、选通对齐 |
| [hdl/psi_fix_bin_div.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_bin_div.vhd) | 除法器组件实现：状态机 + 两段式 + 恢复式除法 |
| [model/psi_fix_bin_div.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_bin_div.py) | 除法器 Python 位真模型（黄金参考） |
| [model/psi_fix_pkg.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py) | Python 定点包（提供 `from_real/abs/sub/shift_left/get_bits_as_int` 等） |
| [hdl/psi_fix_pkg.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd) | VHDL 定点包（提供 `psi_fix_compare`，注意无 Python 镜像） |
| [testbench/psi_fix_bin_div_tb/Scripts/preScript.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_bin_div_tb/Scripts/preScript.py) | 除法器协同仿真数据生成脚本 |
| [testbench/psi_fix_bin_div_tb/psi_fix_bin_div_tb.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_bin_div_tb/psi_fix_bin_div_tb.vhd) | 除法器自检测试台（含定点检查 + 位真文件比对） |
| [testbench/psi_fix_comparator_tb/psi_fix_comparator_tb.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_comparator_tb/psi_fix_comparator_tb.vhd) | 比较器纯 VHDL 自检测试台（无 preScript） |
| [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl) | 回归配置：声明源码、测试台与 preScript 挂载 |

---

## 4. 核心概念与源码讲解

### 4.1 定点比较器 psi_fix_comparator

#### 4.1.1 概念说明

比较器解决一个很常见的实时监控需求：**给定一个最小阈值 `set_min` 和一个最大阈值 `set_max`，每个到来的数据样本，告诉我是「低于最小值」「高于最大值」还是「在窗口内」**。组件输出两个 1 位标志：

- `max_o = '1'` 当且仅当 `data > set_max`；
- `min_o = '1'` 当且仅当 `data < set_min`；
- 两者都为 `'0'` 表示数据落在窗口内。

文件头注释把它的定位说得很直白：「basic block ... not that generic but convenient in some cases」（一个不那么通用但在某些场景很方便的基础块）。它的价值不在数学（比较运算完全委托给 `psi_fix_compare`），而在**结构**：

1. 把阈值与数据按定点格式 `fmt_g` 统一解释（三者必须同格式）。
2. 用一条选通（strobe）延迟链把 `vld_o / min_o / max_o` 三个输出对齐到同一拍。

#### 4.1.2 核心流程

比较器是一个**单时钟进程**组件（注意：它没有采用 u3-l3 讲的 two-process 两段式，而是把组合与时序混在一个 `process(clk_i)` 里，因为它没有复杂的组合反馈，单进程更紧凑）。数据流如下：

```text
                ┌──────────────────────────────────────────┐
data_i ────────▶│ reg(data_s)  ──┐                         │
set_min_i ─────▶│ reg(set_min_s) ├─ psi_fix_compare("a<b") ─▶ min_s ─┐
set_max_i ─────▶│ reg(set_max_s) ├─ psi_fix_compare("a>b") ─▶ max_s  │
                │                │                         │          │
vld_i ─────────▶│ str_s ─▶ str1_s ─▶ vld_o  (3 级选通延迟)   │          │
                └────────────────┼─────────────────────────┼──────────┘
                                   └── 对齐 ──▶ min_o, max_o
```

执行步骤：

1. **输入寄存**：`data_i / set_min_i / set_max_i` 各打一拍寄存器。
2. **选通延迟链**：`vld_i` 经过 `str_s → str1_s → vld_o` 三级移位，形成一个延迟了若干拍的「有效脉冲」。
3. **门控比较**：只在延迟后的选通脉冲 `str_s='1'` 那一拍做两次 `psi_fix_compare`，结果存入 `min_s / max_s`。
4. **输出对齐**：再用 `str1_s` 把 `min_s / max_s` 选通到输出端口，保证 `vld_o` 拉高的那一拍，`min_o / max_o` 正好是与之配对的标志。

#### 4.1.3 源码精读

实体与端口只暴露一个公共格式 `fmt_g`，三类输入（阈值与数据）和输出全部按它解释：

[hdl/psi_fix_comparator.vhd:19-35](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_comparator.vhd#L19-L35) — 实体声明。注意 `set_min_i / set_max_i / data_i` 的位宽都是 `psi_fix_size(fmt_g)-1 downto 0`，三者**必须同格式**，否则比较无意义。

核心是一个时钟进程，比较逻辑只有两行 `if`：

[hdl/psi_fix_comparator.vhd:62-78](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_comparator.vhd#L62-L78) — 门控比较。`psi_fix_compare("a>b", data_s, fmt_g, set_max_s, fmt_g)` 判断 `data > set_max`，`psi_fix_compare("a<b", ...)` 判断 `data < set_min`。比较的数学含义由 `psi_fix_compare` 保证（它内部委托给 `cl_fix_compare`，会自动按 \([s,i,f]\) 对齐两数的小数点后再比）。

选通对齐是结构上的关键：

[hdl/psi_fix_comparator.vhd:53-60](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_comparator.vhd#L53-L60) 与 [hdl/psi_fix_comparator.vhd:80-88](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_comparator.vhd#L80-L88) — 输入寄存 + 三级选通移位 + 输出对齐。`vld_i` 走 `str_s → str1_s → vld_o`，`min_o/max_o` 在 `str1_s='1'` 时才输出比较结果，否则清零，确保标志与 `vld_o` 同拍出现。

`psi_fix_compare` 本身只是个瘦封装：

[hdl/psi_fix_pkg.vhd:591-598](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L591-L598) — 把 psi_fix 格式翻译成 en_cl_fix 格式后委托 `cl_fix_compare`。注意它返回 `boolean`，**不产生数值结果**，所以没有 `r_fmt` 参数（与 u2-l2 讲的「比较/范围类函数无需 `r_fmt`」一致）。

> 提醒：`psi_fix_compare` 只存在于 VHDL 侧。这是后续第 4.3 节判定「比较器无需 Python 位真模型」的直接依据。

#### 4.1.4 代码实践

**实践目标**：通过阅读比较器测试台，理解「当一个组件的全部数学都委托给可信原语时，纯 VHDL 自检测试台如何完成验证」。

**操作步骤**：

1. 打开 [testbench/psi_fix_comparator_tb/psi_fix_comparator_tb.vhd:70-103](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_comparator_tb/psi_fix_comparator_tb.vhd#L70-L103)。
2. 观察激励进程 `proc_stim`（[L106-L134](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_comparator_tb/psi_fix_comparator_tb.vhd#L106-L134)）：它令 `set_min=-0.5`、`set_max=0.5` 固定，然后让 `data` 从 0 开始**线性斜坡上升**再下降，覆盖窗口内外。
3. 观察检查进程（[L87-L103](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_comparator_tb/psi_fix_comparator_tb.vhd#L87-L103)）：它在 `str_obs='1'` 时，用 `psi_fix_compare` 对**延迟 3 拍后的输入数据** `data_dly_s` 重新做一次比较，断言 `min_obs/max_obs` 与之一致，不符则打印 `###ERROR###`。延迟量 `3 * period_c`（[L85](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_comparator_tb/psi_fix_comparator_tb.vhd#L85)）正是为了对齐组件的选通延迟。

**需要观察的现象**：测试台里**没有 preScript、没有 Data 目录、没有任何 Python 调用**。配置侧也佐证了这一点：

[sim/config.tcl:471-472](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L471-L472) — `create_tb_run "psi_fix_comparator_tb"` 后直接 `add_tb_run`，**没有 `tb_run_add_pre_script`**。

**预期结果**：你会理解——比较器唯一的「数学」就是两次 `psi_fix_compare`，而这个原语由 en_cl_fix 保证正确。组件自己只贡献「阈值窗口 + 选通对齐」的结构逻辑。因此测试台只需验证「结构」是否正确（标志是否与选通对齐、是否覆盖窗口边界），用同一个可信原语当 oracle 即可，无需 Python 黄金模型。

**待本地验证**：若你在 Modelsim/GHDL 下跑通回归，应看到比较器测试台无 `###ERROR###` 输出。

#### 4.1.5 小练习与答案

**练习 1**：若把 `fmt_g` 改成无符号格式 `(0, 0, 15)`（`s=0`），`min_o/max_o` 的行为会变吗？

> **答案**：比较的「结构」不变，仍输出两个窗口标志。区别仅在数值解释：无符号格式下 `data` 恒 ≥ 0，只要 `set_min ≥ 0`，`min_o`（`data < set_min`）可能为 1；但若 `set_min` 设成负值，因无符号格式无法表示负数，`set_min` 会被量化成非负值，行为可能与预期不符。这正是组件要求三类输入同格式的原因。

**练习 2**：比较器为什么用三级选通（`str_s → str1_s → vld_o`）而不是直接 `vld_o <= vld_i`？

> **答案**：因为比较结果 `min_s/max_s` 需要一个时钟周期才能算出并稳定（比较发生在 `str_s` 那一拍，结果落到 `min_s/max_s` 又隔一拍）。选通链的作用是把 `vld_o` 延迟到与 `min_o/max_o` 完全对齐的那一拍，保证下游在 `vld_o='1'` 时采样到的标志一定是配对本次数据的。

---

### 4.2 二进制迭代除法 psi_fix_bin_div

#### 4.2.1 概念说明

除法器计算 \(Q = \text{Num}/\text{Denom}\)。FPGA 上没有现成的单周期除法指令，psi_fix 采用**迭代恢复式除法（restoring division）**：每个时钟周期产生商的 1 个比特，从最高位到最低位逐位确定。

直接对带符号数做逐位除法很麻烦（每一步都要处理符号）。`psi_fix_bin_div` 的策略是经典的「**先取绝对值，做无符号除法，最后补符号**」：

\[ Q = \frac{\text{Num}}{\text{Denom}} = \text{sign}(\text{Num} \cdot \text{Denom}) \cdot \frac{|\text{Num}|}{|\text{Denom}|} \]

这样核心迭代只需处理无符号数，符号在最后一步根据两数符号是否相异决定要不要取负。

#### 4.2.2 核心流程

**第一步：常量推导（综合期完成）**。组件用一组常量把「输出格式」翻译成「迭代需要多少步、中间位宽多大」：

- `first_shift_c = out_fmt_g.I`：把分母预放大 \(2^{I_{\text{out}}}\)，使商的整数部分在高比特自然产生。
- `num_abs_fmt_c / denom_abs_fmt_c`：去掉符号位后的无符号绝对值格式（整数位吸收原符号位，即 \(I_{\text{abs}} = I + S\)）。
- `result_int_fmt_c = (1, out\_fmt.I+1, out\_fmt.F+1)`：内部商的格式，整数位和小数位各留 1 个保护位。
- `denom_comp_fmt_c`：分母左移 `first_shift_c` 位后的比较格式。
- `num_comp_fmt_c`：分子放到与 `denom_comp_fmt_c` 对齐的公共格式上（取两者整数位/小数位的较大值）。
- `iterations_c = out_fmt_g.I + out_fmt_g.F + 2`：迭代次数 = 商的有效位数 + 2 个保护位。

**第二步：状态机（运行时）**。组件用五个状态的两段式状态机驱动：

```text
Idle_s ──vld_i=1──▶ Init1_s ──▶ Init2_s ──▶ Calc_s (×iterations_c) ──▶ Output_s ──▶ Idle_s
                    锁存符号     预放大       逐位恢复式除法              补符号+量化
                    取绝对值     初始化
```

- **Init1_s**：锁存分子/分母的符号位，计算两者的绝对值。
- **Init2_s**：把分母左移 `first_shift_c` 得到 `DenomComp`，把分子 resize 到公共比较格式 `NumComp`，商 `ResultInt` 清零，迭代计数器置为 `iterations_c-1`。
- **Calc_s**（核心迭代，执行 `iterations_c` 次）：

  ```text
  ResultInt ← ResultInt << 1                  # 商左移，给新比特腾位
  if DenomComp ≤ NumComp:                     # 当前余数能「装下」分母吗？
      ResultInt(0) ← 1                        #   能 → 本位商 1
      NumComp   ← NumComp − DenomComp         #   余数减去分母
  NumComp ← NumComp << 1                      # 余数左移，等价于「落下下一位」
  ```

- **Output_s**：若两数符号相异，对内部商取负（`psi_fix_neg`），否则直接 resize；最后量化到 `out_fmt_g`（按 `round_g/sat_g`），输出并回到 `Idle_s`。

**为什么 `ResultInt` 左移却仍代表正确的商？** 因为 `ResultInt` 存放在定点格式 `result_int_fmt_c`（含 \(F_{\text{out}}+1\) 个小数位）中。对位模式左移 1 位 = 数值 ×2，而每次迭代产生 1 个商比特从最低位填入。经过 `iterations_c` 次后，比特模式的定点解释正好等于商的真值（见 4.2.4 的数值追踪）。

#### 4.2.3 源码精读

实体声明三个独立的定点格式（分子、分母、输出可各自不同）：

[hdl/psi_fix_bin_div.vhd:23-43](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_bin_div.vhd#L23-L43) — 注意默认 `round_g = psi_fix_trunc`、`sat_g = psi_fix_sat`（组件层偏安全的默认值）。端口用 AXI-S 风格的 `vld_i/vld_o` 握手。

> 命名小提醒：端口 [`rdy_i : out std_logic`](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_bin_div.vhd#L37) 名字带 `_i` 后缀却是个**输出**（组件用它表示「我准备好接收下一个输入」）。这是库中一处历史命名不一致（`_i` 本应表示输入）；u5-l3 会讲到 `cordic_vect` 里同样的 `rdy_i` 已在近期被改名为 `rdy_o`。读除法器时记住 `rdy_i` 实为输出即可。

常量在综合期把格式算清楚：

[hdl/psi_fix_bin_div.vhd:48-55](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_bin_div.vhd#L48-L55) — 六个常量加迭代次数。以测试台格式 `out_fmt_g=(1,4,10)` 为例：`first_shift_c=4`，`result_int_fmt_c=(1,5,11)`，`iterations_c=16`。

两段式 record 把全部寄存器打包：

[hdl/psi_fix_bin_div.vhd:60-77](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_bin_div.vhd#L60-L77) — `two_process_r` 把状态机状态、缓存输入、绝对值、比较用数、迭代计数、内部商、输出握手全收进一个 record；这正是 u3-l3 讲的 record 流水封装范式。

组合进程里的状态机：

[hdl/psi_fix_bin_div.vhd:105-121](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_bin_div.vhd#L105-L121) — `Init1_s`：用 `r.Num(r.Num'left)`（最高位）取符号，用 `psi_fix_abs` 取绝对值。

[hdl/psi_fix_bin_div.vhd:123-130](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_bin_div.vhd#L123-L130) — `Init2_s`：`psi_fix_shift_left` 把分母预放大 `first_shift_c` 位，分子 resize 到公共格式，计数器与商清零。

[hdl/psi_fix_bin_div.vhd:132-148](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_bin_div.vhd#L132-L148) — `Calc_s`：恢复式除法的核心四步。注意比较用的是 `unsigned(r.DenomComp) <= unsigned(NumInDenomFmt_v)`——把两数对齐到同一比较格式后按无符号位模式比大小；若可减则用 `psi_fix_sub` 扣除分母。末尾 `psi_fix_shift_left(...,1,1,...)` 把余数左移 1 位。

[hdl/psi_fix_bin_div.vhd:149-162](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_bin_div.vhd#L149-L162) — `Output_s`：符号相异时 `psi_fix_neg`，否则 `psi_fix_resize`，统一按 `round_g/sat_g` 量化到 `out_fmt_g`。

VHDL 与 Python 两侧常量推导**完全同构**（这是位真的前提）：

[model/psi_fix_bin_div.py:31-37](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_bin_div.py#L31-L37) — Python 模型用与 VHDL 完全相同的算式算出 `numAbsFmt/denomAbsFmt/resultIntFmt/denomCompFmt/numCompFmt`。

[model/psi_fix_bin_div.py:52-58](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_bin_div.py#L52-L58) — Python 迭代循环与 VHDL `Calc_s` 一一对应：`resultInt *= 2`、`np.where(denomComp <= numInDenomFmt, resultInt+1, resultInt)`、减法、左移。`np.where` 是 VHDL `if/else` 的向量化等价物。

#### 4.2.4 代码实践

**实践目标**：用一组真实数字手算恢复式除法，验证「`ResultInt` 左移却给出正确商」这件事，建立对算法的直觉。

**测试台格式的参数**：`num_fmt=(1,2,5)`、`denom_fmt=(1,2,8)`、`out_fmt=(1,4,10)`（与测试台 [L33-L35](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_bin_div_tb/psi_fix_bin_div_tb.vhd#L33-L35) 一致）。取测试台的「简单除法」用例 Num=3.0、Denom=0.5，期望 \(Q=6.0\)。

**常量**：`first_shift_c=4`，故 `DenomComp = 0.5 × 2^4 = 8.0`；`NumComp` 初值 = 3.0；`ResultInt` 初值 = 0；`iterations_c = 16`。

**操作步骤**：按下表逐拍追踪 `ResultInt`、`NumComp`、本位商（`DenomComp ≤ NumComp?`）：

| 迭代 | ResultInt(左移后) | NumComp(比较时) | 8.0 ≤ NumComp? | 本位商 | NumComp(减后/左移后) |
|:-:|:-:|:-:|:-:|:-:|:-:|
| 1 | 0 | 3.0 | 否 | 0 | 6.0 |
| 2 | 0 | 6.0 | 否 | 0 | 12.0 |
| 3 | 0 | 12.0 | 是 | 1 | 4.0 → 8.0 |
| 4 | 1→2 | 8.0 | 是 | 1 | 0 → 0 |
| 5 | 2→4 | 0 | 否 | 0 | 0 |
| 6 | 4→8 | 0 | 否 | 0 | 0 |
| … | （此后 NumComp 恒 0，商位恒 0，ResultInt 每拍 ×2） | | | | |

到第 16 次迭代结束，本例只有第 3、4 次迭代置位（商位 `...11`），其后 12 次迭代 ResultInt 每拍 ×2、商位恒 0。故最终 `ResultInt` 的位模式整数值为 \(3 \times 2^{12} = 12288\)（二进制 `11000000000000`）。由于 `result_int_fmt_c=(1,5,11)` 含 11 个小数位，把该位模式按此格式解释：\(12288 / 2^{11} = 6.0\)，正好等于商的真值。

**预期结果**：内部商经 `Output_s` 的 `psi_fix_resize`（截断 + 饱和）量化到 `out_fmt=(1,4,10)`。6.0 落在 \([-16, 16)\) 范围内，不饱和，最终 `result_o` 的 `to_real` 值 = 6.0。这正是测试台 [L140](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_bin_div_tb/psi_fix_bin_div_tb.vhd#L140) 的 `CheckReal(6.0, ..., 0.01, "Simple Division")` 断言。

**对比饱和用例**：Num=1.0、Denom=0.001，\(Q=1000\)，远超 `(1,4,10)` 的上限 \(\approx 15.999\)，故 `psi_fix_resize` 饱和到上限 ≈ 16.0，对应测试台 [L150](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_bin_div_tb/psi_fix_bin_div_tb.vhd#L150) 的 `CheckReal(16.0, ..., "Saturation")`。

**待本地验证**：表中的中间值建议你在草稿上复算一遍第 3、4 次迭代，确认 `ResultInt` 的位模式经 `(1,5,11)` 解释确为 6.0。

#### 4.2.5 小练习与答案

**练习 1**：为什么迭代次数是 `out_fmt_g.I + out_fmt_g.F + 2`，而不是简单的「输出位宽」？

> **答案**：商的有效信息位数是 \(I_{\text{out}}+F_{\text{out}}\)（整数位 + 小数位）。`+2` 是为了给 `result_int_fmt_c` 多出的 1 个整数保护位和 1 个小数保护位各提供一次置位机会，保证最后量化到 `out_fmt_g` 时有足够的精度余量、减少末位误差。

**练习 2**：如果把 `denom_fmt_g` 设为无符号 `(0,0,17)`（`s=0`），`Init1_s` 里的符号处理会怎样？

> **答案**：[L114-L118](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_bin_div.vhd#L114-L118) 用 `if denom_fmt_g.S = 0` 判断：无符号时分母符号恒为 `'0'`，绝对值等于自身。最终 `Output_s` 里只要分子也非负，符号相异判断为假，直接 resize 输出。

**练习 3**：`Calc_s` 里比较为什么用 `unsigned(...)` 而不直接用 `psi_fix_compare`？

> **答案**：到了 `Calc_s`，`DenomComp` 与 `NumComp` 已经被对齐到**相同的比较格式**（`num_comp_fmt_c` 与 `denom_comp_fmt_c` 的整数/小数位对齐，且都是无符号）。格式相同意味着位模式的小数点对齐，直接比无符号位模式等价于比数学值，省去再次格式翻译的开销。这也是 `NumInDenomFmt_v` 那次 `psi_fix_resize(..., trunc, wrap)` 的用途——把 `NumComp` 重新解释到分母格式上做位模式比较。

---

### 4.3 算术组件测试：两种验证策略的抉择

#### 4.3.1 概念说明

本讲的两个组件恰好代表了 psi_fix 验证谱的两个端点，理解它们的差异比记住任何一个细节都重要：

| 维度 | psi_fix_comparator | psi_fix_bin_div |
|:--|:--|:--|
| 组件的「数学」来源 | 完全委托 `psi_fix_compare`（en_cl_fix 保证） | 自行实现迭代恢复式除法（**无可信原语**） |
| 是否有 Python 位真模型 | **无** | **有**（`model/psi_fix_bin_div.py`） |
| 测试台有无 preScript | 无 | 有 |
| oracle（判定基准） | 同一个 `psi_fix_compare` 原语 | Python 位真模型 |
| 比对方式 | VHDL 内 `assert`（结构正确性） | 文本文件逐位整数比对（位真） |

**抉择原则**：

- 若一个组件的全部算术都能归约到**已验证的 psi_fix_pkg 原语**（compare/resize/add/...），组件本身只贡献**控制与结构**（如选通对齐、阈值窗口），那么**纯 VHDL 自检测试台**就足够——用同一个原语当 oracle 验证结构即可。比较器属于此类。
- 若组件实现了**库中不存在的算术**（迭代除法、CORDIC 旋转、CIC 等），就必须按 u3-l1/u3-l2 的方法论**配套 Python 位真模型 + preScript 协同仿真**，用「最坏情况 + 固定随机种子」刺激逐位比对。除法器属于此类。

#### 4.3.2 核心流程

除法器的协同仿真链路（本讲实践任务的重点）是一条经典的「Python → 文本 → VHDL」闭环：

```text
preScript.py (仿真前由 PsiSim 调用一次)
   │  np.random.seed(0) 固定随机
   │  num/denom ← 随机刺激
   │  numF/denomF ← psi_fix_from_real 量化到定点
   │  res ← psi_fix_bin_div(...)        # Python 黄金参考
   ▼
   psi_fix_get_bits_as_int(...)          # 定点值 → 位模式有符号整数
   ▼
np.savetxt → Data/input.txt, Data/output.txt
   ════════════════════════════════════════
VHDL 测试台 p_control:
   读 input.txt → to_signed → 喂 DUT
   等 OutVld → to_integer(signed(OutQuot))
   与 output.txt 逐行整数比对 → 不符打印 ###ERROR###
```

关键点（承接 u3-l2）：**「整数即位模式」**——同一组二进制位按有符号整数解读相等，当且仅当每一位都相等。所以逐位比对被简化为整数比对。

#### 4.3.3 源码精读

preScript 的刺激生成与黄金参考计算：

[testbench/psi_fix_bin_div_tb/Scripts/preScript.py:39-52](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_bin_div_tb/Scripts/preScript.py#L39-L52) — 注意三处格式常量 `numFmt/denomFmt/outFmt` 与测试台 [L33-L35](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_bin_div_tb/psi_fix_bin_div_tb.vhd#L33-L35) 完全一致；`np.random.seed(0)` 保证可确定性重生；调用 Python 模型 `psi_fix_bin_div(...)` 得到位真期望 `res`。

位模式整数落盘：

[testbench/psi_fix_bin_div_tb/Scripts/preScript.py:67-70](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_bin_div_tb/Scripts/preScript.py#L67-L70) — `psi_fix_get_bits_as_int(numF, numFmt)` 把定点值转成位模式有符号整数；`input.txt` 每行写「分子整数 分母整数」两列，`output.txt` 每行写一个期望结果整数。

配置侧挂载 preScript：

[sim/config.tcl:244-248](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L244-L248) — `tb_run_add_pre_script "python3" "preScript.py" "..."` 在测试台运行前执行 preScript；`tb_run_add_arguments "-gdata_dir_g=$dataDir"` 把 Data 目录路径作为 generic 传给测试台。

VHDL 测试台读回比对：

[testbench/psi_fix_bin_div_tb/psi_fix_bin_div_tb.vhd:170-194](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_bin_div_tb/psi_fix_bin_div_tb.vhd#L170-L194) — 用 `textio` 逐行读 `input.txt` 的两个整数，`to_signed` 喂入 DUT；等 `OutVld='1'` 后把 `to_integer(signed(OutQuot))` 与 `output.txt` 的期望整数比对，不符即打印 `###ERROR###`。

注意测试台**同时**用了两种判定（这是混合策略的范本）：

- 手挑用例用容差判定：[L132-L150](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_bin_div_tb/psi_fix_bin_div_tb.vhd#L132-L150) 的 `CheckReal(..., 0.01, ...)`，借 VHDL-only 的 `psi_fix_to_real`（[hdl/psi_fix_pkg.vhd:355-361](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L355-L361)）把输出转回浮点后比数学值。这覆盖「简单除法 / 饱和 / 握手」等结构性场景。
- 随机批量用逐位判定：[L170-L194](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_bin_div_tb/psi_fix_bin_div_tb.vhd#L170-L194) 的 1000 个样本整数比对，覆盖算法精度。

#### 4.3.4 代码实践（本讲核心实践任务）

**实践目标**：完整跟踪 `bin_div` 测试台的 preScript 协同仿真链路，讲清「除法结果如何由 Python 模型生成并与 VHDL 输出逐位比对」。

**操作步骤**：

1. **读 preScript 的数据生成**。打开 [testbench/psi_fix_bin_div_tb/Scripts/preScript.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_bin_div_tb/Scripts/preScript.py)，按顺序回答：
   - [L43-L45](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_bin_div_tb/Scripts/preScript.py#L43-L45)：刺激如何生成？为什么用 `np.random.seed(0)`？
   - [L49-L52](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_bin_div_tb/Scripts/preScript.py#L49-L52)：浮点刺激如何量化为定点？谁计算期望结果 `res`（黄金参考）？
   - [L67-L70](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_bin_div_tb/Scripts/preScript.py#L67-L70)：定点值如何变成可写文本的整数？为什么 `input.txt` 是两列、`output.txt` 是一列？

2. **读配置挂载**。[sim/config.tcl:244-248](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L244-L248)：preScript 在测试台运行的哪个时机被调用？Data 目录路径如何传给测试台？

3. **读 VHDL 比对**。[testbench/psi_fix_bin_div_tb/psi_fix_bin_div_tb.vhd:170-194](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_bin_div_tb/psi_fix_bin_div_tb.vhd#L170-L194)：
   - 每行读回两个整数后，如何变成 `std_logic_vector` 喂入 DUT？
   - 等到 `OutVld='1'` 后，VHDL 用什么表达式把输出转回整数？与 `output.txt` 的期望如何比对？失败时打印什么字符串（CI 据此判定）？

4. **画出完整数据流图**：把第 1～3 步串成一张「Python 浮点 → 定点量化 → 位模式整数 → 文本 → VHDL to_signed → DUT → to_integer(signed) → 逐行整数比对」的链路图。

**需要观察的现象**：

- preScript 里 `numFmt/denomFmt/outFmt` 与测试台 `NumFmt_c/DenomFmt_c/OutFmt_c` **必须逐一相同**，否则两边的位模式解释不一致，比对必失败。
- preScript 调用的 `psi_fix_bin_div(...)` 传入了 `psi_fix_rnd_t.trunc, psi_fix_sat_t.sat`（[L52](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_bin_div_tb/Scripts/preScript.py#L52)），与 DUT 的 generic `round_g => psi_fix_trunc, sat_g => psi_fix_sat`（[L79-L80](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_bin_div_tb/psi_fix_bin_div_tb.vhd#L79-L80)）完全对齐——这是位真的硬性要求。

**预期结果**：你能用一段话讲清——「Python 模型是黄金参考，它先把随机刺激量化到与 DUT 相同的定点格式并算出期望商，再用 `psi_fix_get_bits_as_int` 把定点值压成位模式有符号整数写盘；VHDL 测试台读回这些整数、`to_signed` 喂入 DUT、在 `OutVld` 时用 `to_integer(signed(OutQuot))` 与期望逐行比对，任何一位不一致就打印 `###ERROR###`。因为『同符号整数相等 ⟺ 位模式每一位相等』，所以整数比对即逐位比对。」

**待本地验证**：若本地已装好 PsiSim + Modelsim/GHDL + Python 依赖，可在 `sim/` 下 `source ./run.tcl`（或 `runGhdl.tcl`）跑除法器回归，确认 1000 个样本全部通过、无 `###ERROR###`。

#### 4.3.5 小练习与答案

**练习 1**：为什么比较器测试台用 `psi_fix_compare` 当 oracle 不算「作弊」？

> **答案**：因为比较器组件本身就没有自创任何比较数学——它调用的就是 `psi_fix_compare`。测试台再用 `psi_fix_compare` 当 oracle，验证的是「组件的**结构**（选通对齐、阈值窗口）是否正确地把这个原语包了起来」，而不是验证原语本身。原语的正确性由 en_cl_fix 负责。这就像用加法验证一个累加器：不是循环论证，而是验证「累加」这个结构。

**练习 2**：假设你要给 `psi_fix_bin_div` 增加一个新的 `round_g = psi_fix_round` 选项，preScript 和测试台需要同步改什么？

> **答案**：preScript [L52](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_bin_div_tb/Scripts/preScript.py#L52) 调用 `psi_fix_bin_div` 时要把舍入参数改成 `psi_fix_rnd_t.round`；同时测试台 DUT 例化的 generic（[L79](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_bin_div_tb/psi_fix_bin_div_tb.vhd#L79)）也要设成 `psi_fix_round`。两侧必须一致，否则位真比对会因为末位差异而失败。更好的做法是在 `config.tcl` 用参数矩阵同时跑 trunc 和 round 两轮（参考 u1-l3 的 `tb_run_add_arguments`）。

---

## 5. 综合实践

把本讲三个最小模块串成一个任务：**为除法器补一个「最坏情况」位真测试，并解释它为何比纯随机刺激更严苛**。

1. **算法理解**：写出 `out_fmt=(1,4,10)` 下商的最大幅值（提示：饱和上限 ≈ 15.999），并说明当 Num 取最大正数、Denom 取最小正数时除法会触发饱和（对应测试台 [L142-L150](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_bin_div_tb/psi_fix_bin_div_tb.vhd#L142-L150) 的饱和用例）。

2. **刺激设计**（参考 u3-l1 的最坏情况思想）：在 preScript 里，除了现有的 `np.random.seed(0)` 随机段，再**显式追加**几组手挑的最坏情况样本，例如：
   - Num = +max、Denom = +min（正向饱和）；
   - Num = −max、Denom = +min（负向饱和，验证 `psi_fix_neg` 路径）；
   - Num = +max、Denom = −min（符号相异 + 饱和）。
   把这些样本拼到 `num/denom` 数组前面，重新生成 `input.txt/output.txt`。

3. **验证策略对照**：用一段话说明——为什么除法器**必须**用 Python 位真模型 + preScript 验证这些最坏情况，而比较器却可以用纯 VHDL 自检测试台过关？（提示：回到 4.3.1 的抉择原则，看组件的数学是否归约到可信原语。）

4. **跟踪比对**：对你追加的负向饱和样本，手动追踪 `Init1_s`（符号锁存、取绝对值）→ `Calc_s`（无符号除法）→ `Output_s`（符号相异 → `psi_fix_neg` → 饱和）的完整路径，确认 VHDL 与 Python 给出相同的位模式整数。

**预期结果**：你应当能清楚区分「组件数学是否自创」这一判定标准，并能独立为一个新的算术组件选择正确的验证策略。

**待本地验证**：修改后的 preScript 与测试台行为需在本地仿真环境中确认无 `###ERROR###`（本任务涉及修改测试数据，请在本地环境验证）。

## 6. 本讲小结

- **比较器** `psi_fix_comparator` 是一个单进程组件，把 `psi_fix_compare` 封装成「阈值窗口 + 三级选通对齐」结构；它的全部数学都委托给可信原语，因此**无需 Python 位真模型**，用纯 VHDL 自检测试台 + 同一原语当 oracle 即可验证。
- **除法器** `psi_fix_bin_div` 用「先取绝对值 → 无符号恢复式除法 → 最后补符号」的策略；核心是 `Calc_s` 状态机的「商左移、比较、条件减、余数左移」四步，迭代 `out_fmt.I + out_fmt.F + 2` 次。
- `ResultInt` 虽每拍左移，但因为它存放在含 \(F_{\text{out}}+1\) 小数位的 `result_int_fmt_c` 中，位模式的定点解释恰好等于商的真值。
- **VHDL 与 Python 两侧的常量推导与迭代循环完全同构**，这是位真双模型的前提；两侧的 `round/sat`、格式常量必须逐一相同。
- **验证策略抉择**：组件数学归约到 psi_fix_pkg 原语 → 纯 VHDL 自检够；组件自创算术 → 必须 Python 位真模型 + preScript 协同仿真。`psi_fix_compare` 仅存于 VHDL 侧，是这一抉择的关键判据。
- 除法器测试台示范了**混合判定**：手挑用例用 `CheckReal` 容差判定覆盖结构场景，1000 个随机样本用「整数即位模式」逐位判定覆盖算法精度，失败统一打印 `###ERROR###`。

## 7. 下一步学习建议

- **下一站：复数运算族（u5-l1）**。复数乘法/求模会在本讲的「位增长 + 取绝对值」之上引入复数实虚部的乘加结构，是 CORDIC 的前置。你会再次看到「Python 位真模型 + preScript」套路，届时可对照本讲除法器加深理解。
- **若对除法器的迭代算法意犹未尽**：可先读 u8-l3 的 `psi_fix_inv`（1/X 倒数）与 `psi_fix_sqrt`（平方根），它们同样用迭代/近似实现，并对照各自的 Python 位真模型，体会「非线性函数如何在定点硬件上落地」。
- **若想强化测试方法论的直觉**：回头重读 u3-l2（测试台与协同仿真流程），把它与本讲 4.3 的「两种验证策略」对照，建立「看到一个新组件 → 立刻判断它该用哪种验证方式」的反射。
