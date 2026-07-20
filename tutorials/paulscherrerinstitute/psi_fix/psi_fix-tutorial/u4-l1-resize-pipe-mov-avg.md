# resize_pipe 与 moving average

## 1. 本讲目标

本讲是「简单处理组件实战」的第一讲。在前几个单元里，我们已经掌握了 psi_fix 的三大基础设施——定点包（u2）、位真双模型方法论（u3）、两段式编码风格（u3-l3）。本讲要把这些基础设施**串成一个完整的端到端组件**，让读者第一次走通「VHDL 实现 + Python 位真模型 + 自检测试台」的完整闭环。

读完本讲，你应该能够：

- 说清 `psi_fix_mov_avg` 为何用**差分-累加**（而不是 N 个加法器）实现滑动平均，并推导出每一级的定点格式。
- 解释三种增益校正模式 **NONE / ROUGH / EXACT** 各自的资源-精度权衡，并能手算 `AdditionalBits`、`GcInFmt`、`GcCoefFmt`。
- 理解 `out_regs_g` 输出寄存器可配的用意，以及 `psi_fix_resize_pipe` 如何把一次 `resize` 拆成「舍入级 + 饱和级」两级流水并带反压。

本讲不重复两段式编码、协同仿真流程等已讲内容，只在用到时引用。

## 2. 前置知识

本讲假设你已经理解以下概念（若生疏请回看对应讲义）：

- **定点格式三元组 [s,i,f] 与位增长规则**（u1-l4）：加/减法整数位 +1，两个有符号数相乘整数位相加后再 +1。
- **psi_fix 运算函数**（u2-l2）：`resize / add / sub / mult / shift_right` 的签名与「结果格式 `r_fmt` 由调用者指定、函数不自动位增长」的约定，以及 **Manual Splitting**（把 round 与 sat 拆成多级流水）范式。
- **两段式编码风格**（u3-l3）：组合进程 `p_comb` 写 `r_next`、时序进程 `p_seq` 仅打拍；用 `record`（固定名 `two_process_r`）封装流水线寄存器，`Vld` 数组随数据逐级平移；`v := r` 让未赋值字段默认保持。
- **协同仿真套路**（u3-l2）：`preScript.py` 跑 Python 位真模型生成 `Data/*.txt`，测试台用 `ApplyTextfileContent`/`CheckTextfileContent` 逐位比对，不符即打印 `###ERROR###`。

补充两个本讲用到、但前面讲义未展开的小知识点：

- **滑动平均（moving average）的数学含义**：输出 \(y[n]\) 是最近 \(N\) 个输入的算术平均：

\[
y[n] = \frac{1}{N}\sum_{k=0}^{N-1} x[n-k]
\]

  先求「滑动和」\(\sum x[n-k]\)，再除以 \(N\) 即得平均。本讲组件的核心难点就在于「除以 \(N\)」如何在 FPGA 上廉价地实现。

- **FPGA 的 DSP 乘法器端口宽度**：Xilinx DSP48 slice 的乘法器输入端口典型为 25 位 × 18 位。当两个乘数分别 ≤25 位、≤18 位时，一次乘法正好映射到一个 DSP slice。本讲会看到这个事实如何反向决定定点格式。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|:--|:--|
| [hdl/psi_fix_mov_avg.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd) | 滑动平均的可综合 VHDL 实现（差分-累加 + 三模式增益校正 + 可配输出寄存器） |
| [model/psi_fix_mov_avg.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_mov_avg.py) | 与 VHDL 逐位一致的 Python 位真模型（黄金参考） |
| [hdl/psi_fix_resize_pipe.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_resize_pipe.vhd) | 纯流水的位宽变换组件，把 `resize` 拆成两级并带反压 |
| testbench/psi_fix_mov_avg_tb/psi_fix_mov_avg_tb.vhd | 自检测试台（`stim`/`check` 双进程） |
| testbench/psi_fix_mov_avg_tb/Scripts/preScript.py | 协同仿真数据生成脚本 |
| sim/config.tcl | 回归配置，声明 mov_avg 测试台的三组 generics |

> 提示：`psi_fix_mov_avg` 是全库最适合作为「第一个完整组件」学习的对象——它体量小、风格标准、同时示范了流水、条件累加、三分支、可配寄存器等典型手法。

## 4. 核心概念与源码讲解

### 4.1 滑动平均的差分-累加结构

#### 4.1.1 概念说明

实现「最近 \(N\) 个样本的平均」最直白的办法是：摆 \(N\) 个寄存器做延时线，再用一棵 \(N\) 输入加法树求和。这要消耗 \(N-1\) 个加法器，**资源随抽头数 \(N\) 线性增长**，对大 \(N\) 很不划算。

`psi_fix_mov_avg` 采用一种更聪明的**差分-累加**（也叫滑动和 / CIC 式）结构，其资源与 \(N\) **几乎无关**——无论 7 抽头还是 100 抽头，算术部分都只需要「1 个减法器 + 1 个加法器」。

直觉是这样的：相邻两个采样时刻的滑动和 \(S[n]\) 与 \(S[n-1]\) 之间，只差一个新进来的样本 \(x[n]\) 和一个离开窗口的旧样本 \(x[n-N]\)。于是：

\[
S[n] = S[n-1] + \underbrace{x[n] - x[n-N]}_{\text{差分}}
\]

展开递推即得 \(S[n] = \sum_{k=0}^{N-1} x[n-k]\)，正是滑动和。所以核心只需要：

1. 一条 **\(N\) 拍延时线**，拿到 \(x[n-N]\)；
2. 一个 **减法器**，算差分 \(x[n] - x[n-N]\)；
3. 一个 **累加器**，把差分不断累加成滑动和 \(S[n]\)。

> 这个结构与后续单元（u6）的 **CIC 滤波器**同源（积分 + 梳状），本讲是 CIC 的预热。

#### 4.1.2 核心流程

数据流可画成：

```
            ┌─────────────┐
 dat_i ────►│ delay (N拍) ├──────────────► DataDel = x[n-N]
    │       └─────────────┘                    │
    │                                          ▼
    │              ┌──────────┐        (减法) ┌──────────┐
    └─────────────►│  sub     │◄──────────────┤
                   │ x[n]-    │   Diff ──────►│          │
                   │ x[n-N]   │               │  累加器   │──► Sum[n] = 滑动和
                   └──────────┘               │ S+=Diff  │
                                              └──────────┘
```

每来一个有效样本，仅做一次减 + 一次加。注意累加是**有条件**的——只有在输入有效（`Vld(0)='1'`）时才更新，否则累加器保持（u3-l3 讲过的 valid 守卫反馈环路手法）。

**各级定点格式推导**（位增长规则的直接应用，u1-l4）：

- **差分级**：两数相减整数位 +1，所以

\[
\text{DiffFmt} = [1,\ \text{in\_i}+1,\ \text{in\_f}]
\]

  代码注释也点明：差分既不需要舍入也不会饱和（结果一定落在 DiffFmt 内），故用 `trunc/wrap`。

- **累加级**：累加器最多累加 \(N\) 个样本，整数位至多增长 \(\lceil\log_2 N\rceil\) 个。令

\[
\text{AdditionalBits} = \lceil\log_2 N\rceil
\]

  则

\[
\text{SumFmt} = [1,\ \text{in\_i}+\text{AdditionalBits},\ \text{in\_f}]
\]

  这样选择可保证累加器**内部永不溢出**（遵循 tips.md「用理论最大值定格式、绝不假设输入幅度」的纪律）。

#### 4.1.3 源码精读

先看延时线。`mov_avg` 不自己写移位寄存器，而是复用 `psi_common_delay`，让用户可在 SRL/BRAM 间选择：

[hdl/psi_fix_mov_avg.vhd:159-172](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L159-L172) —— 例化 `psi_common_delay`，把 `dat_i` 延时 `taps_g` 拍得到 `DataDel`（即 \(x[n-N]\)），`resource_g => "AUTO"` 让工具自动选 SRL 或 BRAM。

再看常量区里的两个核心格式（DiffFmt、SumFmt 与上述公式逐字对应）：

[hdl/psi_fix_mov_avg.vhd:46-51](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L46-L51) —— `Gain_c = taps_g`、`AdditionalBits_c = log2ceil(Gain_c)`，并据此定义 `DiffFmt_c`、`SumFmt_c`。

然后是组合进程里的两级：

[hdl/psi_fix_mov_avg.vhd:99-106](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L99-L106) —— **Stage 0** 做 `psi_fix_sub(dat_i - DataDel)` 得 `Diff_0`，并把 `vld_i` 存进 `Vld(0)`；**Stage 1** 仅在 `r.Vld(0)='1'` 时执行 `psi_fix_add(Sum_1 + Diff_0)`，否则累加器保持——这正是 u3-l3 讲过的「valid 守卫」。

Python 位真模型用 `numpy` 镜像同一逻辑，关键是 `np.cumsum` 直接算累加和：

[model/psi_fix_mov_avg.py:76-82](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_mov_avg.py#L76-L82) —— `dataDel` 用 `np.concatenate` 前补 \(N\) 个 0 实现 \(N\) 拍延时；`diff = psi_fix_sub(...)`；`sum = psi_fix_from_real(np.cumsum(diff), self.sumFmt)`。注释强调「既不舍入也不饱和，故位真」——这正是 tips.md 推荐的「尽量在满精度下用 `numpy` 内建函数」做法（u2-l3 也提过）。

> 两侧数学等价：VHDL 的「逐拍条件累加」与 Python 的「`cumsum` 一次性求前缀和」在位真前提下结果完全一致，这是协同仿真能逐位比对的前提。

#### 4.1.4 代码实践

**实践目标**：亲手验证差分-累加结构在两侧的等价性。

**操作步骤**（源码阅读型 + 可选运行）：

1. 打开 [model/psi_fix_mov_avg.py:66-82](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_mov_avg.py#L66-L82)，跟踪 `Process()` 一段：`dataFix → dataDel → diff → sum`。
2. 在仓库根目录用 Python 手算一个小例子（示例代码，非项目原有）：

   ```python
   import sys; sys.path.append("model")
   import numpy as np
   from psi_fix_mov_avg import psi_fix_mov_avg
   m = psi_fix_mov_avg(inFmt=(1,0,10), outFmt=(1,1,12), taps=7,
                       gaincorr=psi_fix_mov_avg.GAINCORR_NONE)
   x = np.array([1,1,1,1,1,1,1,1,0,0], dtype=float)  # 前7个1进窗后开始有样本离开
   print(m.Process(x))  # 关注前若干个输出的滑动和
   ```

3. 对照 [hdl/psi_fix_mov_avg.vhd:99-106](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L99-L106) 解释：为何前 6 个有效样本进入时累加器值分别是 1,2,3,4,5,6，第 7 个起开始有样本离开窗口。

**需要观察的现象**：`NONE` 模式下输出约等于「窗口内 1 的个数 × 输入幅度」，幅度随抽头数放大（这正是下一节要校正的增益）。

**预期结果**：在 `NONE` 模式下，对全 1 输入，稳态输出 ≈ 7（增益 = 抽头数 = 7）。

**待本地验证**：若本机已装 `numpy`/`scipy` 并按 u1-l1 摆好 `en_cl_fix` 等依赖，可直接运行；否则按跟踪法手动核验即可。

#### 4.1.5 小练习与答案

**练习 1**：为何差分级敢用 `trunc/wrap`（不担心溢出）？

> **答案**：两个同格式 `[1, in_i, in_f]` 的数相减，结果范围恰好被 `DiffFmt=[1, in_i+1, in_f]`（多 1 个整数位）完全覆盖，既不会溢出（无需 sat）也无需改变小数精度（无需 round）。代码注释「rounding not required, saturation cannot occur」即此意。

**练习 2**：`taps_g = 8` 时 `AdditionalBits` 是多少？`SumFmt` 比 `in_fmt` 多几个整数位？

> **答案**：`AdditionalBits = log2ceil(8) = 3`，`SumFmt` 比 `in_fmt` 多 3 个整数位。注意即使 \(N\) 是 2 的幂，`AdditionalBits` 仍按 \(\lceil\log_2 N\rceil\) 取（这里恰好 3），保证不溢出。

---

### 4.2 三种增益校正模式 NONE / ROUGH / EXACT

#### 4.2.1 概念说明

差分-累加得到的是**滑动和**，DC 增益为 \(N\)（输入恒为 1 时，\(N\) 个 1 求和 = \(N\)）。要变成**滑动平均**（增益 1），必须除以 \(N\)。但「除以任意整数 \(N\)」在 FPGA 上很贵——除法器资源大、时序差。

`mov_avg` 提供三种由简到精的近似，让用户在**资源、精度、增益误差**之间权衡：

| 模式 | 做法 | 实际增益 | 资源 | 适用场景 |
|:--|:--|:--|:--|:--|
| **NONE** | 不除，直接输出滑动和 | \(N\) | 最省（无乘法） | 后级会自己做归一化，或只关心相对形状 |
| **ROUGH** | 右移 `AdditionalBits` 位（≈除以 \(2^{\text{AdditionalBits}}\)） | \(N/2^{\text{AdditionalBits}}\in[0.5,1]\) | 省（仅移位，无乘法） | 可容忍 ≤2 倍增益误差 |
| **EXACT** | 先右移，再乘系数 \(2^{\text{AdditionalBits}}/N\) | \(1.0\) | 用 1 个乘法器（≈1 DSP） | 需要精确平均 |

直觉：

- **ROUGH** 用「右移」代替「除法」。右移 \(k\) 位 = 除以 \(2^k\)，是免费的（FPGA 上就是重新解读小数点位置）。代价是除数只能取 2 的幂，故 \(N=7\) 时只能除以 8，得到增益 \(7/8=0.875\)（偏小约 12.5%）。文档把 ROUGH 描述为「\(0.5 < \text{gain} < 1.0\)」正是此理。
- **EXACT** 在 ROUGH 的基础上再乘一个**修正系数** \(G_c = 2^{\text{AdditionalBits}}/N\) 把增益拉回 1.0。组合起来：

\[
\text{总缩放} = \frac{1}{2^{\text{AdditionalBits}}} \cdot \frac{2^{\text{AdditionalBits}}}{N} = \frac{1}{N}
\]

  乘以滑动和的增益 \(N\)，恰好得到 1.0。EXACT 用一个乘法器换来精确平均，是资源-精度的最优折中。

#### 4.2.2 核心流程

三种模式的实现分支在 Stage 2/3，但它们共用一组**中间格式常量**。这些常量的推导是本节重点，也是本讲的代码实践任务。

**Step 1 —— `AdditionalBits`**：

\[
\text{AdditionalBits} = \lceil\log_2 N\rceil
\]

**Step 2 —— 修正系数格式 `GcCoefFmt = [0, 1, 16]`**（17 位无符号）：

系数 \(G_c = 2^{\text{AdditionalBits}}/N\)。当 \(N\ge1\) 时 \(2^{\text{AdditionalBits}}\in[1, 2N)\)，故 \(G_c\in[1, 2)\)。`[0,1,16]` 表示范围 \([0,\ 2-2^{-16}]\)，刚好能装下 \([1,2)\) 的系数，且 **17 位正好适配 DSP48 乘法器的 18 位输入端口**（留 1 位余量）。

**Step 3 —— 粗校正后格式 `GcInFmt`**（EXACT 模式专用）：

EXACT 先把 `SumFmt` 右移 `AdditionalBits` 位得到 `RoughCorr`，再乘 `Gc`。右移 `AdditionalBits` 位等价于把同一些二进制位重新解读：整数位减少 `AdditionalBits`，小数位增加 `AdditionalBits`（总位宽不变）。所以「满精度」下粗校正后的格式本应是：

\[
[1,\ \text{in\_i},\ \text{in\_f}+\text{AdditionalBits}]
\]

但为了**把乘法器的一个输入压到 ≤25 位**（适配 DSP48 的 25 位输入端口，与 17 位系数合成一次 25×17 乘法、占用单个 DSP slice），代码对小数位封顶：

\[
\boxed{\text{GcInFmt} = [\,1,\ \text{in\_i},\ \min(24-\text{in\_i},\ \text{SumFmt.F}+\text{AdditionalBits})\,]}
\]

- 整数位 = `in_i`：右移 `AdditionalBits` 后的理论整数位。
- 小数位 = `min(24-in_i, in_f+AdditionalBits)`：尽量保留右移释放的全部精度（`in_f+AdditionalBits`），但封顶 `24-in_i` 使总宽 \(1+\text{in\_i}+(24-\text{in\_i})=25\) 位。

> 这就是源码里那个神秘「24」的来历——它让 `GcInFmt`（≤25 位）与 `GcCoefFmt`（17 位）的乘法恰好落进一个 DSP slice。

**Step 4 —— 三分支**（伪代码）：

```
if NONE:   out = resize(Sum, out_fmt, round, sat)                     # 仅重采样
if ROUGH:  out = shift_right(Sum, AdditionalBits, out_fmt, round, sat) # 移位=除法
if EXACT:
           RoughCorr = shift_right(Sum, AdditionalBits, GcInFmt, trunc, wrap) # 先粗校正
           out       = mult(RoughCorr, Gc, out_fmt, round, sat)                # 再精确修正
```

注意 EXACT 模式多一级流水（Stage 3），所以它的 valid 比另两种多延迟一拍（用 `r.Vld(2)` 而非 `r.Vld(1)`）。

#### 4.2.3 源码精读

常量区集中了上面推导的全部格式：

[hdl/psi_fix_mov_avg.vhd:52-56](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L52-L56) —— `GcInFmt_c`、`GcCoefFmt_c` 定义，以及 `Gc_c = psi_fix_from_real(2.0**AdditionalBits_c/Gain_c, GcCoefFmt_c)` 在**综合期**就把系数算成定点常量（运行时不需算除法）。

VHDL 的三分支在组合进程 Stage 2/3：

[hdl/psi_fix_mov_avg.vhd:108-123](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L108-L123) —— Stage 2 按 `gain_corr_g` 分三路：`NONE` 直接 `resize` 到 `out_fmt_g`；`ROUGH` 用 `psi_fix_shift_right` 右移 `AdditionalBits_c` 位；`EXACT` 先右移到 `GcInFmt_c`（仅 trunc/wrap）。Stage 3 仅 `EXACT` 执行 `psi_fix_mult(RoughCorr_2 × Gc_c)` 得最终输出。注意 `psi_fix_shift_right` 在本库中是动态移位可综合的特殊实现（u2-l2 讲过），这里 `shift` 与 `maxShift` 都传 `AdditionalBits_c`（常数），属于其常数移位用法。

`gain_corr_g` 取值合法性由 `assert` 守卫，违例即打印 `###ERROR###`：

[hdl/psi_fix_mov_avg.vhd:77](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L77) —— 这是 CI 失败判据约定的同一字符串（u1-l3、u3-l2）。

Python 模型的三分支与 VHDL **逐字对齐**：

[model/psi_fix_mov_avg.py:85-91](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_mov_avg.py#L85-L91) —— `NONE` 用 `psi_fix_resize`、`ROUGH` 用 `psi_fix_shift_right`、`EXACT` 用 `shift_right` + `psi_fix_mult`，且中间格式、round/sat 参数与 VHDL 完全一致。这正是位真双模型的核心要求（u3-l1）。

#### 4.2.4 代码实践（本讲核心任务）

**实践目标**：对照 VHDL 与 Python 两侧，说明 ROUGH 与 EXACT 的中间格式 `GcInFmt` / `GcCoefFmt` 是如何在两侧**一致地**推导出来的。

**操作步骤**：

1. 打开 VHDL 常量区 [hdl/psi_fix_mov_avg.vhd:46-56](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L46-L56) 与 Python 构造器 [model/psi_fix_mov_avg.py:56-61](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_mov_avg.py#L56-L61)，逐行比对。

2. 以测试台默认参数 `in_fmt=(1,0,10)`、`taps=7` 为例手算（两侧都应得到同一结果）：

   | 量 | 推导 | 值 |
   |:--|:--|:--|
   | `AdditionalBits` | \(\lceil\log_2 7\rceil\) | 3 |
   | `SumFmt` | \([1,\ 0+3,\ 10]\) | `[1,3,10]`（宽 14） |
   | `GcCoefFmt` | 固定 | `[0,1,16]`（宽 17） |
   | `GcInFmt` | \([1,\ 0,\ \min(24-0,\ 10+3)]\) | `[1,0,13]`（宽 14） |
   | `Gc` | \(2^3/7 = 8/7\) 量化到 `[0,1,16]` | ≈1.1428 |

3. 验证两侧表达式同构（这是「一致推导」的实质）：

   - `AdditionalBits`：VHDL `log2ceil(Gain_c)` ↔ Python `np.ceil(np.log2(gain))`
   - `GcInFmt.F`：VHDL `min(24-in_fmt_g.I, SumFmt_c.F+AdditionalBits_c)` ↔ Python `min(24-inFmt.i, self.sumFmt.f+self.additionalBits)`
   - `Gc`：VHDL `2.0**real(AdditionalBits_c)/real(Gain_c)` ↔ Python `2.0**self.additionalBits/gain`

**需要观察的现象**：两侧除函数名/语言差异外，公式完全镜像；`GcInFmt` 宽度恰为 14（=25 的约束在这里没绑住，因为 `in_i=0` 时 `24-in_i=24 > 13`）；`GcInFmt`×`GcCoefFmt` = 14×17 位乘法，落进一个 DSP slice。

**预期结果**：你会得出结论——`GcCoefFmt` 是固定常量 `[0,1,16]`（适配 DSP 18 位口），`GcInFmt` 由「右移后的理论整数位 `in_i`」+「封顶到 25 位总宽的小数位」共同决定；两侧用**完全相同的算式**计算，保证 VHDL 与 Python 位真。EXACT 比 ROUGH 多一级乘法流水，换来增益严格等于 1.0。

**待本地验证**：可在 Python 里 `print(m.gcInFmt, m.gcCoefFmt, m.gc)` 与上表核对。

#### 4.2.5 小练习与答案

**练习 1**：`taps_g=7` 时 ROUGH 模式的实际增益是多少？为什么文档说 ROUGH「\(0.5<\text{gain}<1.0\)」？

> **答案**：增益 \(=7/2^3=0.875\)。一般地 \(N/2^{\lceil\log_2 N\rceil}\)：当 \(N\) 是 2 的幂时为 1.0，否则介于 0.5 与 1.0 之间（因 \(N > 2^{\lceil\log_2 N\rceil-1}=2^{\lceil\log_2 N\rceil}/2\)，故比值 >0.5）。

**练习 2**：为何 EXACT 模式 Stage 2 的 `shift_right` 用 `trunc/wrap`，而最终输出才用 `round/sat`？

> **答案**：Stage 2 只是把数据重排到 `GcInFmt` 供 Stage 3 乘法使用，应尽量保留精度、不做有损量化（trunc/wrap 在此不丢信息，因为 `GcInFmt` 足够宽）；真正的有损量化（round/sat）推迟到 Stage 3 乘法之后一次性完成。这是 tips.md「尽量减少量化点」纪律的体现。

**练习 3**：把 `in_fmt_g` 改成 `(1,8,4)`、`taps_g=7`，求 `GcInFmt`。

> **答案**：`AdditionalBits=3`，`SumFmt=[1,11,4]`；`GcInFmt.F = min(24-8, 4+3) = min(16,7) = 7`，故 `GcInFmt=[1,8,7]`，宽 \(1+8+7=16\) 位。这里 `24-in_i=16` 的封顶没绑住，但若 `in_i` 更大就会绑住，把总宽限制在 25。

---

### 4.3 输出寄存器配置与 resize_pipe

#### 4.3.1 概念说明

**为何要可配输出寄存器？** mov_avg 的最后一级（增益校正 + resize）可能包含「乘法 + 舍入 + 饱和」三件事，在很高时钟频率下会成为关键路径。tips.md 给了两条解决思路：

- **Solution 1（寄存器重定时）**：在输出端多挂几级寄存器，让综合工具（Vivado retiming）自动把它们推进组合逻辑内部优化时序。
- **Solution 2（手动拆分，Manual Splitting）**：自己在 VHDL 里把 round/sat 拆成多级（u2-l2 已讲）。

`mov_avg` 的 `out_regs_g` 就是配合 Solution 1 的旋钮：用户可挂 0~N 级输出寄存器，让工具去做重定时。

**`psi_fix_resize_pipe` 是什么？** 它是 `psi_fix_resize` 的**带流水、带反压**版本，本质就是一次 `resize`（即「重采样」=舍入 + 饱和）。它把这一次 resize 显式拆成两级流水——**舍入级 + 饱和级**，这恰好是 tips.md Manual Splitting（Solution 2）的标准范式：先单独做舍入（多 1 个整数位容纳进位），再单独做饱和。额外地，它实现了完整的 AXI-S 握手（`rdy_o`/`rdy_i`），可在两级之间做反压（skid buffer 思想），所以叫 `_pipe`。

一句话区分：

- `out_regs_g`（在 mov_avg 内部）：**工具导向**的时序优化，靠重定时把寄存器推进逻辑。
- `resize_pipe`（独立组件）：**手动导向**的时序优化，靠代码显式拆级，并自带反压。

#### 4.3.2 核心流程

**mov_avg 的输出寄存器链**用一个数组 `OutRegs(0 to out_regs_g-1)` 逐级平移，valid 用 `VldOutRegs` 同步平移——手法与 u3-l3 讲的 `Vld` 数组完全一致：

```
out_regs_g = 0:  组合结果 CalcOut 直接送 dat_o（纯组合输出，0 拍延迟）
out_regs_g = k>0: CalcOut → OutRegs[0] → OutRegs[1] → ... → OutRegs[k-1] → dat_o（k 拍延迟）
```

**resize_pipe 的两级流水**（带反压）：

```
            ┌─────────────┐    ┌─────────────┐
 dat_i ────►│ 舍入级 Rnd  ├───►│ 饱和级 Sat  ├───► dat_o
 vld_i ────►│ (round,+1位)│    │ (trunc,sat) │────► vld_o
            └──────┬──────┘    └──────┬──────┘
                   │ RndRdy           │ SatRdy
            rdy_o ◄┘            rdy_i ◄┘
```

- **舍入级**：`resize(dat_i, in_fmt → RndFmt, round, wrap)`。`RndFmt` 比 `in_fmt` 多 1 个整数位（容纳舍入进位），小数位取 `out_fmt.F`。注意**只舍入、不饱和**——饱和要等舍入做完才正确。
- **饱和级**：`resize(RndReg, RndFmt → out_fmt, trunc, sat)`。小数位已与 `out_fmt.F` 相同（trunc 在此是小数位 no-op），主要做整数范围饱和。
- **反压**：每级用「`not 自身Vld` 或 下级Rdy」算出自己的 ready（`RndRdy`/`SatRdy`），上游只有在本级 ready 时才能写入，实现 AXI-S 风格的停顿。

#### 4.3.3 源码精读

mov_avg 的输出寄存器在 record 与组合进程里：

[hdl/psi_fix_mov_avg.vhd:67-68](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L67-L68) —— `OutRegs` 数组与 `VldOutRegs` 进入 `two_process_r` record（数组长度即 `out_regs_g`）。

[hdl/psi_fix_mov_avg.vhd:125-134](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L125-L134) —— `out_regs_g=0` 时组合结果直送端口；`out_regs_g>0` 时把 `CalcOut_v/CalcVld_v` 喂进 `OutRegs(0)/VldOutRegs(0)`，端口取数组最高级（见 L96-97 的逐级平移）。这样无论挂几级寄存器，输出时序都规整。

注意复位只清 valid 与累加器，不清数据通路寄存器（u3-l3 的「选择性同步复位」）：

[hdl/psi_fix_mov_avg.vhd:148-152](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L148-L152) —— 复位仅清 `Vld`、`VldOutRegs`、`Sum_1`。

再看 resize_pipe。先看它的舍入级格式与端口：

[hdl/psi_fix_resize_pipe.vhd:43](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_resize_pipe.vhd#L43) —— `RndFmt_c = (in_fmt_g.S, in_fmt_g.I+1, out_fmt_g.F)`：多 1 个整数位给舍入进位，小数位直接取目标。

[hdl/psi_fix_resize_pipe.vhd:32-35](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_resize_pipe.vhd#L32-L35) —— 端口含双向握手 `rdy_o`（输出，告诉上游本组件可收）与 `rdy_i`（输入，下游可收）。

两级流水与反压计算在组合进程：

[hdl/psi_fix_resize_pipe.vhd:67-79](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_resize_pipe.vhd#L67-L79) —— 舍入级 `RndRdy = (not RndVld) or SatRdy`（本级空 或 饱和级能收，才允许上游写入），饱和级 `SatRdy = (not SatVld) or rdy_i`（同理对接下游）。`rdy_o` 直接引出 `RndRdy`：

[hdl/psi_fix_resize_pipe.vhd:88](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_resize_pipe.vhd#L88) —— 这是 AXI-S 握手中 ready 由「从设备/接收方」给出的标准做法（对照 u5-l3 将提到的 `rdy_o` 命名修正：ready 是输出，因为它表达本组件是否就绪）。

#### 4.3.4 代码实践

**实践目标**：跑一次 mov_avg 回归，观察三种增益模式与不同 `out_regs_g` 的组合如何被同一测试台覆盖。

**操作步骤**：

1. 阅读 [sim/config.tcl:298-303](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L298-L303)。这里 `create_tb_run "psi_fix_mov_avg_tb"` 后挂了一个 pre_script 和**三组** `tb_run_add_arguments`：

   | 组 | `gain_corr_g` | `duty_cycle_g` | `out_regs_g` |
   |:--|:--|:--|:--|
   | 1 | NONE | 1 | 0 |
   | 2 | EXACT | 5 | 3 |
   | 3 | ROUGH | 3 | 1 |

   即同一个测试台换 generics 跑 3 轮（u1-l3 讲过的参数矩阵）。

2. 注意 check 进程按命名约定自动选输出文件：[testbench/psi_fix_mov_avg_tb/psi_fix_mov_avg_tb.vhd:165](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/psi_fix_mov_avg_tb.vhd#L165) 用 `to_lower(gain_corr_g)` 拼 `output_<mode>.txt`，与 preScript 的 `gc.lower()`（[preScript.py:60](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/Scripts/preScript.py#L60)）对齐——preScript 只跑一次，为三种模式各生成一份期望输出，3 轮测试各取所需。

3. （可选）按 u1-l3 在 `sim/` 下 `source ./run.tcl`（Modelsim）或 `runGhdl.tcl`（GHDL）跑回归。

**需要观察的现象**：三组参数分别覆盖了「无增益校正 + 无输出寄存器」「精确校正 + 3 级输出寄存器」「粗校正 + 1 级输出寄存器」，且 `duty_cycle_g` 不同（模拟输入有间隔/无间隔的握手场景）。

**预期结果**：三组均不打印 `###ERROR###`，回归通过。这证明：同一个 DUT 在不同 generics 下，与同一个 Python 模型在对应 `gaincorr` 下的输出逐位一致。

**待本地验证**：是否实际运行取决于本机是否装好 Modelsim/GHDL 与 PsiSim（u1-l1、u1-l3）。无条件运行时，按上面阅读法理解参数矩阵与命名约定链即可。

#### 4.3.5 小练习与答案

**练习 1**：`resize_pipe` 的 `RndFmt_c` 为何比 `in_fmt_g` 多 1 个整数位？

> **答案**：舍入（`round`）会在最坏情况下产生向上的进位（例如 `[0,0,17]` 的最大值四舍五入到 `[0,0,16]` 时会进到 1.0），需要多 1 个整数位承载这个进位，否则舍入级自身就会溢出。tips.md 的「定点设计」一节专门提醒了这一点。

**练习 2**：`resize_pipe` 把饱和放在第二级而不是和舍入合在一级，好处是什么？

> **答案**：把「舍入」和「饱和」拆到两级流水，每级逻辑更浅，时序更好（tips.md Manual Splitting）。同时两级各自有独立 ready，可在中间做反压停顿。

**练习 3**：若把 `mov_avg` 的 `out_regs_g` 设为 0，输出相对 `out_regs_g=3` 少多少拍延迟？valid 链如何随之变化？

> **答案**：少 3 拍。`out_regs_g=0` 时组合结果直送 `dat_o`/`vld_o`；`out_regs_g=3` 时数据要经 `OutRegs(0)→(1)→(2)` 三拍，`VldOutRegs` 同步平移 3 拍，故 valid 也延迟 3 拍。可见 `out_regs_g` 直接控制输出延迟，使用时需在后级时序对齐中考虑。

---

## 5. 综合实践

**任务**：为 `psi_fix_mov_avg` 当一次「格式审计员」，把一个完整配置下从输入到输出的**全部定点格式**串起来，并判断每处量化是否有损。

设 `in_fmt_g=(1,0,15)`、`out_fmt_g=(1,0,14)`、`taps_g=5`、`gain_corr_g=EXACT`、`round_g=round`、`sat_g=sat`。

请完成：

1. 计算 `AdditionalBits`、`DiffFmt`、`SumFmt`、`GcCoefFmt`、`GcInFmt`、`Gc`（数值）。
2. 标出数据流上每一处的格式：`dat_i → Diff_0 → Sum_1 → RoughCorr_2 → (乘 Gc) → out_fmt`。
3. 指出哪几处是「无损」（trunc/wrap 不丢信息）、哪几处是「有损」（round/sat）。
4. 对照源码 [hdl/psi_fix_mov_avg.vhd:46-56](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L46-L56) 与 [model/psi_fix_mov_avg.py:56-61](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_mov_avg.py#L56-L61) 核对你的答案。

**参考要点**：

- `AdditionalBits = log2ceil(5) = 3`。
- `DiffFmt = [1,1,15]`；`SumFmt = [1,3,15]`。
- `GcCoefFmt = [0,1,16]`；`Gc = 2^3/5 = 1.6`（在 `[0,1,16]` 范围 \([0,2)\) 内）。
- `GcInFmt = [1, 0, min(24-0, 15+3)] = [1,0,18]`（宽 19，封顶 `24-in_i=24` 未绑住）。
- 无损点：Stage 0 减法（`trunc/wrap`，DiffFmt 足够宽）、Stage 1 累加（`trunc/wrap`，SumFmt 足够宽）、EXACT Stage 2 右移到 `GcInFmt`（`trunc/wrap`，未丢位）。
- 有损点：EXACT Stage 3 乘法后到 `out_fmt`（`round/sat`，唯一的真正量化点）——符合 tips.md「量化只在必要处」的纪律。

完成本题后，你应当能独立审计任意 psi_fix 组件的定点格式链。

## 6. 本讲小结

- `psi_fix_mov_avg` 用**差分-累加**结构（延时线 + 减法器 + 累加器）实现滑动和，算术资源与抽头数 \(N\) 无关，仅用 1 加 1 减。
- 各级定点格式由位增长规则严格推导：`DiffFmt` 减法 +1 整数位，`SumFmt` 累加 \(\lceil\log_2 N\rceil\) 整数位，保证内部不溢出。
- 增益校正是本组件的灵魂：**NONE** 不校正（增益 \(N\)）、**ROUGH** 右移近似除法（增益 \(\in[0.5,1]\)、无需乘法）、**EXACT** 右移后乘修正系数（增益 1.0、用 1 个 DSP）。
- `GcCoefFmt=[0,1,16]`（17 位）与 `GcInFmt`（封顶 25 位）的设计让 EXACT 乘法恰好映射到一个 DSP48 slice；这两个格式在 VHDL 与 Python 两侧用**同一组算式**推导，是位真双模型一致性的典型范例。
- `out_regs_g` 提供可配输出寄存器链（配合综合工具重定时优化时序）；`psi_fix_resize_pipe` 则示范了「舍入级 + 饱和级」的手动拆分流水并带 AXI-S 反压。
- 组件严格遵守库规约：`assert` 用 `###ERROR###` 守卫非法 generic、复位只清 valid 与累加器、全 `snake_case` 与 `_i/_o/_g` 命名。

## 7. 下一步学习建议

- **继续本单元**：u4-l2 将讲 `psi_fix_param_ram`（真双口参数 RAM），它是后续可配置 FIR 在运行时存放系数的底座；u4-l3 讲比较器与二进制除法，继续积累算术组件。
- **走向滤波器**：本讲的差分-累加是 CIC 滤波器的雏形。学完 u4 后可直接进入单元 6（CIC），对比 `psi_fix_cic_dec_fix_1ch` 的积分-梳状结构如何扩展本讲的差分-累加思想。
- **深入移位与乘法**：若想彻底弄懂 EXACT 模式里 `psi_fix_shift_right` 的动态可综合性实现，回看 u2-l2 的移位函数精读；想了解乘法在 FIR 中的大规模调度，预习单元 7（FIR）。
- **建议阅读源码**：对照 [hdl/psi_fix_resize_pipe.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_resize_pipe.vhd) 与 tips.md 的 Manual Splitting 一节，体会「先舍入、后饱和」的两级流水范式——它会在后续几乎所有高吞吐组件中反复出现。
