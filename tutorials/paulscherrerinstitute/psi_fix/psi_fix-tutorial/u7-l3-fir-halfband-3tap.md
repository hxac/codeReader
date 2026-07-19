# 半带 FIR 3tap（psi_fix_fir_3tap_hbw_dec2）

## 1. 本讲目标

本讲聚焦 psi_fix FIR 家族中一个「特殊用途」成员——`psi_fix_fir_3tap_hbw_dec2`。它是一个固定系数 \(h=[0.25,\ 0.5,\ 0.25]\)、抽取比为 2 的半带（half-band）抽取滤波器。学完后你应该能够：

- 说清什么是半带滤波器，为什么它的截止频率天然落在 \(f_s/4\)，因而最适合抽取 2。
- 解释 \(0.25\) 与 \(0.5\) 是 2 的幂，因而整个 FIR 不需要任何乘法器，只用「移位 + 加法」即可实现。
- 读懂 RTL 里「3 个移位 + 2 个加法」的三级流水结构，以及为何中间格式 `IntFmt_c` 要多留 2 个小数位。
- 理解抽取 2 带来的「输入每拍喂 2 个样本、输出每拍出 1 个样本」的速率关系，以及 `separate_g` 如何在「多通道并行」与「单通道级联抽取」之间切换。
- 把本组件和 u7-l1/u7-l2 的通用 FIR 结论串起来：尽管 RTL 是专用无乘法器结构，它的位真黄金参考仍是同一个通用 `psi_fix_fir.py` 模型。

## 2. 前置知识

本讲假设你已掌握（见 u7-l1、u7-l2、u3-l3、u2-l2）：

- **FIR 基本原理**：\(y[n]=\sum_k h[k]\,x[n-k]\)，即抽头系数与输入样本的卷积。
- **psi_fix 命名模板**：`psi_fix_fir_<decimation>_<calc>_<channels>_<channel-handling>_<coef>_<arch>`。本组件名 `fir_3tap_hbw_dec2` 是一个**特例**，它不严格套用六字段模板，而是用 `3tap_hbw` 标明「3 抽头、半带宽」的专用结构、用 `dec2` 标明「抽取 2」。这点会在 4.1 再展开。
- **定点格式 \([s,i,f]\) 与位增长规则**：两个有符号数相乘整数位相加后再 +1；右移一位等价于除以 2。
- **两段式编码（two-process）**：组合进程 `p_comb` 写 `r_next`、时序进程 `p_seq` 打拍；valid 用数组住进 record 逐级平移。
- **位真双模型与协同仿真**：Python 模型是黄金参考，测试台用 `###ERROR###` 逐位比对。

两个本讲要用到的小知识：

- **右移 = 除以 2 的幂**。定点数右移 \(k\) 位即乘以 \(2^{-k}\)。所以乘 \(0.25=2^{-2}\) 等价于右移 2 位，乘 \(0.5=2^{-1}\) 等价于右移 1 位。这就是「无乘法器」的物理基础。
- **抽取（decimation）**：降低采样率。抽取 2 = 每两个输入样本产生一个输出样本，输出速率是输入的一半。

## 3. 本讲源码地图

| 文件 | 作用 |
|:---|:---|
| [hdl/psi_fix_fir_3tap_hbw_dec2.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_3tap_hbw_dec2.vhd) | 可综合 VHDL 实体：3 个移位 + 2 个加法的三级流水，多通道可配 |
| [model/psi_fix_fir.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_fir.py) | 通用 FIR 位真黄金模型（与 RTL 实现无关，所有 FIR 变体共用） |
| [testbench/psi_fix_fir_3tap_hbw_dec2_tb/Scripts/preScript.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_fir_3tap_hbw_dec2_tb/Scripts/preScript.py) | 协同仿真脚本：用 `h=[0.25,0.5,0.25]` 跑黄金模型，生成各通道/各模式下的输入输出文本 |
| [testbench/psi_fix_fir_3tap_hbw_dec2_tb/psi_fix_fir_3tap_hbw_dec2_tb.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_fir_3tap_hbw_dec2_tb/psi_fix_fir_3tap_hbw_dec2_tb.vhd) | 自检测试台：`ApplyTextfileContent` 喂激励、`CheckTextfileContent` 逐位比对 |
| [doc/files/psi_fix_fir_3tap_hbw_dec2.md](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/psi_fix_fir_3tap_hbw_dec2.md) | 组件文档（含 separate/非 separate 的数据排布图） |
| [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl) | 把测试台注册进回归，并声明 7 组 generics 参数矩阵 |

---

## 4. 核心概念与源码讲解

### 4.1 半带滤波特性

#### 4.1.1 概念说明

**半带滤波器（half-band filter）** 是一类特殊的低通 FIR 滤波器，它的截止频率天然落在采样率的四分之一处（\(f_s/4\)，即数字角频率 \(\omega=\pi/2\)）。这个位置恰好是「抽取 2 之后新采样率 \(f_s/2\) 的奈奎斯特频率」。因此半带滤波器是抽取 2 的天然搭档：先把带外成分滤掉，再把采样率减半，不会在通带内引入混叠。

半带滤波器有两个极其优美的数学性质，正是它们让本组件能做成固定系数、无乘法器的专用电路：

1. **系数对称且间隔为零**：除中心抽头外，**每隔一个系数必为零**。3 抽头时只有 \([h_{-1},\ h_0,\ h_{+1}]\)，对称性给出 \(h_{-1}=h_{+1}\)。
2. **中心抽头恒为 0.5**：半带条件 \(|H(e^{j\omega})|+|H(e^{j(\pi-\omega)})|=1\) 在 \(\omega=\pi/2\) 处要求 \(H=0.5\)，对奇数长对称 FIR 而言即中心抽头为 0.5。

本组件把这两个性质发挥到极致，选择最短的 3 抽头解：

\[
h = [\,0.25,\ 0.5,\ 0.25\,]
\]

验证：直流增益（所有系数之和）为

\[
\sum_k h[k] = 0.25 + 0.5 + 0.25 = 1.0
\]

即通带增益为 1，信号不会被放大或衰减；而在奈奎斯特频率 \(f_s/2\) 处，输入 \((-1)^n\) 使相邻样本反相相消，\(H(e^{j\pi})=0.25-0.5+0.25=0\)，阻带起始处正好衰减到 0。这就是「半带」一词的几何含义：通带与阻带关于 \(f_s/4\) 对称。

#### 4.1.2 核心流程

3 抽头半带 FIR 的差分方程为

\[
y[n] = 0.25\,x[n-1] + 0.5\,x[n] + 0.25\,x[n+1]
\]

抽取 2 后，只在偶数序号 \(n\) 上计算输出。伪代码：

```
每到来 2 个输入样本 (s0, s1):
    取上一次的「跨拍样本」prev 作为 x[n-1]
    x[n]   ← s0          (中心抽头，乘 0.5)
    x[n+1] ← s1          (边抽头，乘 0.25)
    y = 0.25*prev + 0.5*s0 + 0.25*s1
    输出 y，把 s1 记为下一次的 prev
```

关键点：因为每拍消费 2 个样本、而滤波窗口宽 3 个样本，窗口必然**跨越拍边界**——第一个抽头 \(x[n-1]\) 复用上一拍的最后一个样本。这一点直接决定了 4.3 节 RTL 里 `In3Sig` 的滑窗接线。

#### 4.1.3 源码精读

固定系数 \(h=[0.25,0.5,0.25]\) 在工程里只出现一处——协同仿真脚本 `preScript.py`，它把这套系数喂给黄金模型：

```python
h = np.array([0.25,0.5,0.25])
```

> 见 [preScript.py:33](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_fir_3tap_hbw_dec2_tb/Scripts/preScript.py#L33)：声明半带抽头。

随后实例化通用模型并以抽取比 2 跑出黄金输出：

```python
f = psi_fix_fir(inFmt, outFmt, coefFmt)
outData[ch] = f.Filter(inData[ch], 2, h)
```

> 见 [preScript.py:47-48](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_fir_3tap_hbw_dec2_tb/Scripts/preScript.py#L47-L48)：第二个参数 `2` 即抽取比 dec2。

注意：**VHDL 实体里根本没有出现 0.25/0.5 这两个系数字面量**。它们被「编译」成了移位量（见 4.2）。这正是本组件区别于通用 FIR 的核心：系数是固定的 2 的幂，所以不必走「乘法器 + 系数 RAM」的通用路径，而是直接固化为移位。

文档把这一点写得很直白：

> 见 [doc/files/psi_fix_fir_3tap_hbw_dec2.md:12](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/psi_fix_fir_3tap_hbw_dec2.md#L12)：「3 taps with fixed coefficients: 0.25, 0.5, 0.25. This enables efficient implementation based on bit shifting instead of multiplications.」

#### 4.1.4 代码实践

**目标**：用 SciPy 验证 \(h=[0.25,0.5,0.25]\) 的半带性质（直流增益为 1、\(f_s/2\) 处为 0、截止在 \(f_s/4\)）。

**操作步骤**（在工作目录下新建一个临时脚本，例如 `/tmp/hbw_check.py`，**不要**写进仓库）：

```python
# 示例代码：验证半带滤波特性
import numpy as np
from scipy.signal import freqz

h = np.array([0.25, 0.5, 0.25])
w, H = freqz(h, worN=4*1024)          # w 归一化到 [0, pi]
fs_over_4 = np.argmin(np.abs(w - np.pi/2))
print("直流增益 |H(0)|     =", abs(H[0]))            # 期望 1.0
print("fs/2 增益 |H(pi)|   =", abs(H[-1]))           # 期望 0.0
print("fs/4 增益 |H(pi/2)| =", abs(H[fs_over_4]))    # 期望 0.5
```

**需要观察的现象**：直流增益应打印 `1.0`（通带不放大）；\(f_s/2\) 处应打印 `0.0`（阻带起始衰减到 0）；\(f_s/4\) 处应打印 `0.5`（半带对称中心）。

**预期结果**：三处数值分别约为 \(1.0\)、\(0.0\)、\(0.5\)，与上面手算一致。若数值不符，说明系数取错。运行结果依赖本机 SciPy，若未安装则标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把中心抽头从 0.5 改成 0.4、边抽头仍是 0.25，它还是半带滤波器吗？直流增益变成多少？

**答案**：不是了。半带要求中心抽头恒为 0.5（由 \(H(e^{j\pi/2})=0.5\) 推出）。改后直流增益 \(=0.25+0.4+0.25=0.9\neq1\)，通带会有 0.9 倍衰减，且不再满足半带对称条件。

**练习 2**：为什么半带滤波器特别适合做抽取 2，而不是抽取 3？

**答案**：半带截止频率固定在 \(f_s/4\)，恰好等于「抽取 2 后新采样率 \(f_s/2\) 的奈奎斯特频率」。抽取 3 的目标通带边界是 \(f_s/6\)，与半带的 \(f_s/4\) 不匹配，无法直接复用。

---

### 4.2 无乘法器实现（移位 + 加法）

#### 4.2.1 概念说明

通用 FIR 的每个抽头都要做一次「样本 × 系数」的乘法，抽头数多时耗费大量 DSP48 slice（见 u7-l2）。但本组件的三个系数 \(0.25,\ 0.5,\ 0.25\) 恰好都是 2 的幂：

\[
0.25 = 2^{-2},\qquad 0.5 = 2^{-1}
\]

乘以 \(2^{-k}\) 在定点硬件里就是**右移 \(k\) 位**——一根线重接线而已，不需要乘法器。于是整个 3 抽头 FIR 退化成「3 个右移 + 2 个加法」，资源里只有 LUT/FF 与进位链，**零 DSP**。这是「专用结构换通用资源」的典型范例：放弃了系数可配置性（系数写死），换来了零乘法器的极简实现。

但要位真地实现「右移 = 乘 \(2^{-k}\)」有一个坑：右移会丢掉低位，若直接截断就引入误差，与黄金模型 `psi_fix_fir.py` 的舍入行为对不上。psi_fix 的 `psi_fix_shift_right` 函数（见 u2-l2）专门处理这点：它先放大到无损中间格式、移位、再按指定 `r_fmt` 舍入。本组件靠一个精心设计的中间格式 `IntFmt_c` 来留住精度。

#### 4.2.2 核心流程

RTL 的数据通路分三级流水（组合进程里描述，时序进程打拍）：

```
Stage 1（移位）:  MultSig = shift_right(in3Sig, Shifts_c)   -- 3 路右移: 2,1,2 位
Stage 2（加法1）: AddSig  = MultSig[0] + MultSig[1]          -- tap0+tap1
                  AddSigZ = MultSig[2]                        -- tap2 暂存
Stage 3（加法2）: OutSig  = AddSig + AddSigZ, resize 到 out_fmt_g   -- 求和并舍入/饱和
```

为什么中间格式多 2 个小数位？最大的移位量是 2（对应 0.25）。若移位后立刻截到原小数位 `F`，就会丢掉 2 位精度，导致后续加法与黄金模型不一致。因此 `IntFmt_c` 把小数位设成 `F+2`，把右移可能丢掉的 2 位先保留下来，只在最后一级（Stage 3，resize 到 `out_fmt_g`）才统一舍入。这样误差模式与通用模型 `psi_fix_fir.py` 里「累加器全精度 → 末端舍入」完全一致。

数学上，以输入 \(x\) 的小数位为 \(F\) 为例，乘 0.25 后的真值需要 \(F+2\) 位小数才能无损表示（因为乘 \(2^{-2}\) 等价于小数点右移 2 位）。`IntFmt_c` 的 `F+2` 正是这个道理。

#### 4.2.3 源码精读

两个常量定义了「移位量」与「中间精度」，它们就是「系数」的硬件化身：

```vhdl
constant Shifts_c : t_ainteger  := (2, 1, 2);
constant IntFmt_c : psi_fix_fmt_t := (in_fmt_g.S, in_fmt_g.I, in_fmt_g.F + 2);
```

> 见 [hdl/psi_fix_fir_3tap_hbw_dec2.vhd:49-50](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_3tap_hbw_dec2.vhd#L49-L50)：
> - `Shifts_c = (2,1,2)` 即右移 2、1、2 位，对应系数 \(0.25, 0.5, 0.25\)；
> - `IntFmt_c` 把小数位扩成 `F+2` 以留住移位精度。

Stage 1 用 `psi_fix_shift_right` 完成「无乘法器乘法」。其函数签名为 `(a, a_fmt, shift, maxShift, r_fmt, rnd, sat)`（见 [psi_fix_pkg.vhd:150-156](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L150-L156)）。调用处传 `maxShift=2`（因为最大移位就是 2），把移位量常数化以满足 Vivado 可综合性（这是 u2-l2 讲过的动态移位处理）：

```vhdl
for v_i in r.MultSig'range loop
  v.MultSig(v_i) := psi_fix_shift_right(r.in3Sig(v_i), in_fmt_g,
                   Shifts_c(v_i / channels_g), 2, IntFmt_c, rnd_g, sat_g);
end loop;
```

> 见 [hdl/psi_fix_fir_3tap_hbw_dec2.vhd:128-130](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_3tap_hbw_dec2.vhd#L128-L130)：`v_i / channels_g` 把 3×channels 个移位结果按 tap 归组到同一个移位量。结果格式统一为 `IntFmt_c`。

Stage 2 与 Stage 3 是两次加法，注意 tap2 是「暂存一拍」再参与第二次加法（`AddSigZ`），这样三级流水节拍对齐：

```vhdl
-- Stage 2: tap0 + tap1，并把 tap2 暂存到 AddSigZ
v.AddSig(v_i)  := psi_fix_add(r.MultSig(v_i), IntFmt_c,
                r.MultSig(v_i + channels_g), IntFmt_c, IntFmt_c, rnd_g, sat_g);
v.AddSigZ(v_i) := r.MultSig(v_i + 2 * channels_g);
-- Stage 3: (tap0+tap1) + tap2，并 resize 到 out_fmt_g
v.OutSig(v_i) := psi_fix_add(r.AddSig(v_i), IntFmt_c,
                r.AddSigZ(v_i), IntFmt_c, out_fmt_g, rnd_g, sat_g);
```

> 见 [hdl/psi_fix_fir_3tap_hbw_dec2.vhd:134-142](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_3tap_hbw_dec2.vhd#L134-L142)：两次 `psi_fix_add`，第二级的 `r_fmt` 直接是 `out_fmt_g`——舍入/饱和只在最终输出发生。

整个 record 把流水线寄存器与 valid 链打包在一起（两段式风格，见 u3-l3）：

```vhdl
type two_process_r is record
  Vld     : std_logic_vector(0 to 3);   -- 4 级 valid 流水
  InData  : InData_t;
  In3Sig  : In3_t;
  MultSig : Mult_t;
  AddSig  : Add_t;
  AddSigZ : Add_t;
  OutSig  : OutData_t;
end record;
```

> 见 [hdl/psi_fix_fir_3tap_hbw_dec2.vhd:60-68](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_3tap_hbw_dec2.vhd#L60-L68)：字段 `MultSig/AddSig/AddSigZ/OutSig` 正好对应三级流水，`Vld` 是 4 级有效流水。

注意加法没有显式给整数位 +1 防溢出——`psi_fix_add` 把结果格式交给调用者，这里仍用 `IntFmt_c` 作为中间和。安全边界来自「输入有界 + 系数和为 1」：两个边抽头各贡献 0.25、中心 0.5，求和幅度不会超过输入幅度，因此 `IntFmt_c` 与输入同整数位即够用，不会溢出。

#### 4.2.4 代码实践

**目标**：手算追踪一个样本序列穿过「移位 + 加法」三级流水，确认 RTL 与差分方程一致。

**操作步骤**：

1. 取 3 个连续输入样本 \(x=[0.5,\ 0.25,\ -0.25]\)（对应 \(x[n-1]=0.5,\ x[n]=0.25,\ x[n+1]=-0.25\)），格式假定为 \([1,0,17]\)。
2. 手算差分方程：
   \[
   y = 0.25(0.5) + 0.5(0.25) + 0.25(-0.25) = 0.125 + 0.125 - 0.0625 = 0.1875
   \]
3. 模拟 RTL 路径：三路右移（按 `Shifts_c=(2,1,2)`）得到 \(0.5{\gg}2=0.125\)、\(0.25{\gg}1=0.125\)、\(-0.25{\gg}2=-0.0625\)；先加前两个 \(=0.25\)，再加第三个 \(=0.1875\)。

**需要观察的现象**：步骤 2 的差分方程结果与步骤 3 的「移位 + 加法」结果完全相同（都是 0.1875），证明「右移替代乘法」在此系数下位等价。

**预期结果**：两条路径都得 \(0.1875\)。若不一致，检查移位方向是否搞反（应是右移）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `IntFmt_c` 的小数位改成 `in_fmt_g.F`（不 +2），会出什么问题？

**答案**：移位后立刻丢掉 2 位精度，`MultSig` 的低位被截断，Stage 3 求和后的结果会与黄金模型 `psi_fix_fir.py`（它在累加器里保留全精度 `accuFmt`、只在末端舍入）对不上，协同仿真会报 `###ERROR###`。

**练习 2**：为什么 `psi_fix_shift_right` 的 `maxShift` 参数这里填 2？

**答案**：`Shifts_c` 里的最大移位量就是 2。`maxShift` 告诉函数「运行时移位量的上界」，函数据此用 `for i in 0 to maxShift` 循环把动态移位常数化以满足 Vivado 可综合性（u2-l2 讲过的机制）。填大于 2 会多生成无用分支、浪费资源；填小于 2 则移位会被截断出错。

---

### 4.3 抽取与多通道处理

#### 4.3.1 概念说明

「dec2」带来两件事：速率减半与端口位宽翻倍。

**速率关系**：抽取 2 意味着每消费 2 个输入样本才产生 1 个输出样本。本组件把这个比例固化进端口——`dat_i` 每拍携带**每通道 2 个样本**，`dat_o` 每拍出**每通道 1 个样本**。所以输出速率严格等于输入速率的一半，无需外部计数器。

**多通道**：`channels_g` 个通道在组件内部**并行**处理（共享同一组 AXI-S 握手 `vld_i/vld_o`，没有 `rdy`，即无反压的简化握手）。`dat_i` 位宽是 `psi_fix_size(in_fmt_g) * 2 * channels_g`，把「每通道 2 样本 × 通道数」拼成一根总线。

**`separate_g` 的两种模式**（这是本组件最容易被忽视、却最关键的旋钮）：

- `separate_g = true`（**多通道独立**）：每个通道各自是一个独立信号源。每拍送来的 2 个样本属于同一通道，各通道并行滤波、互不相关。这是「一次抽取 2、多通道并发」的标准用法。
- `separate_g = false`（**单通道级联**）：所有样本都来自同一个信号源，本组件对它做一次抽取 2。因为输入位宽仍按 `channels_g` 份来排，多出来的「通道」位实际上承载的是同一源的相邻样本——于是可以把多个本组件**级联**，每级抽取 2，实现抽取 \(2^k\)（4、8、16……）。这正是文档里说的「decimate by N (where N is a power of two) by connecting more components in a chained structure」。

#### 4.3.2 核心流程

输入拆分与滑窗接线是核心。组合进程先把拼接总线拆成 `2×channels` 个标量样本：

```
dat_i (总线) ──拆分──> InDataS[0..2*channels-1]
```

然后在 Stage 0，对每个通道构造 3 抽头窗口 `In3Sig`。关键技巧（以 `separate_g=true` 为例）：

```
tap0 (x[n-1]) ← r.InData(...)    -- 上一拍寄存过的最后一个样本（跨拍复用）
tap1 (x[n])   ← InDataS(本拍第1个样本)
tap2 (x[n+1]) ← InDataS(本拍第2个样本)
```

`r.InData`（已寄存）提供「上一拍的样本」，使跨拍窗口成立。`In3Sig` 被组织成 3 个长度为 `channels_g` 的块：`[tap0 全通道 | tap1 全通道 | tap2 全通道]`，这样后面 Stage 1 的移位循环用 `v_i / channels_g` 就能选到正确的移位量。

输出端，每拍把 `channels_g` 个结果按通道拼回 `dat_o` 总线，`vld_o` 用流水线最后一级的有效信号驱动。

#### 4.3.3 源码精读

端口位宽直接体现「dec2」与「多通道」：

```vhdl
dat_i : in  std_logic_vector(psi_fix_size(in_fmt_g) * 2 * channels_g - 1 downto 0);
dat_o : out std_logic_vector(psi_fix_size(out_fmt_g) * channels_g - 1 downto 0);
```

> 见 [hdl/psi_fix_fir_3tap_hbw_dec2.vhd:39-41](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_3tap_hbw_dec2.vhd#L39-L41)：`dat_i` 多了一个 `* 2`（每通道 2 样本），`dat_o` 只有 `* channels_g`（每通道 1 样本）。速率关系一眼可见。

`separate_g` 是 generic（与 `channels_g` 一样导出供测试台配置）：

```vhdl
channels_g  : natural     := 2;
separate_g  : boolean     := true;
```

> 见 [hdl/psi_fix_fir_3tap_hbw_dec2.vhd:29-30](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_3tap_hbw_dec2.vhd#L29-L30)。

Stage 0 的 `separate_g=true` 分支构造滑窗（`r.InData` 提供跨拍样本）：

```vhdl
if separate_g then
  for v_i in 0 to inDataS'high / 2 loop
    v.in3Sig(v_i)                  := r.InData(v_i * 2 + 1);        -- tap0 = 上一拍第2样本
    v.in3Sig(v_i + 1 * channels_g) := InDataS(v_i * 2);             -- tap1 = 本拍第1样本
    v.in3Sig(v_i + 2 * channels_g) := InDataS(v_i * 2 + 1);         -- tap2 = 本拍第2样本
  end loop;
```

> 见 [hdl/psi_fix_fir_3tap_hbw_dec2.vhd:102-110](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_3tap_hbw_dec2.vhd#L102-L110)：tap0 来自寄存过的 `r.InData`，tap1/tap2 来自当拍 `InDataS`，构成跨拍 3 样本窗口。

`separate_g=false` 分支用不同的接线把同一源的连续样本映射进窗口（见 [hdl/psi_fix_fir_3tap_hbw_dec2.vhd:111-123](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_3tap_hbw_dec2.vhd#L111-L123)），用于级联抽取场景。

测试台用 `ApplyTextfileContent` 按 `vld_duty_cycle_g` 节拍重放 `input.txt`、用 `CheckTextfileContent` 逐通道比对 `output.txt`：

```vhdl
ApplyTextfileContent( Clk => Clk, Vld => InVld, Data => SigIn,
    Filepath => file_folder_g & "/" & in_file_g,
    ClkPerSpl => vld_duty_cycle_g, IgnoreLines => 1);
...
CheckTextfileContent( Clk => Clk, Vld => OutVld, Data => SigOut,
    Filepath => file_folder_g & "/" & out_file_g, IgnoreLines => 1);
```

> 见 [psi_fix_fir_3tap_hbw_dec2_tb.vhd:150-182](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_fir_3tap_hbw_dec2_tb/psi_fix_fir_3tap_hbw_dec2_tb.vhd#L150-L182)：`SigIn` 是 `0 to 2*channels-1`（每通道 2 样本），`SigOut` 是 `0 to channels-1`（每通道 1 样本），与 DUT 端口位宽一致；不符即由 psi_tb 打印 `###ERROR###`。

回归脚本用 7 组参数矩阵覆盖通道数、模式与吞吐节拍的组合：

```tcl
create_tb_run "psi_fix_fir_3tap_hbw_dec2_tb"
tb_run_add_pre_script "python3" "preScript.py" "..."
set dataDir [file normalize ".../Data"]
tb_run_add_arguments "-gchannels_g=1 ... -gin_file_g=inChannels1SeparateTrue.txt ..."  \
    "-gchannels_g=2 -gseparate_g=false ..." \
    "-gchannels_g=4 -gseparate_g=false ..." \
    "-gchannels_g=1 -gvld_duty_cycle_g=1 ..." \
    ...
add_tb_run
```

> 见 [sim/config.tcl:412-422](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L412-L422)：`channels_g ∈ {1,2,4}`、`separate_g ∈ {true,false}`、`vld_duty_cycle_g ∈ {1,5}`（满速与节流）的笛卡尔子集；每组对应 preScript 预生成的一份输入输出文件（如 `inChannels2SeparateTrue.txt`）。

#### 4.3.4 代码实践

**目标**：解释「dec2 后输出速率与输入速率的关系」，并说明 `separate_g` 两种模式下同一组 generics 的不同含义。

**操作步骤**（源码阅读型）：

1. 打开 [hdl/psi_fix_fir_3tap_hbw_dec2.vhd:39-41](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_3tap_hbw_dec2.vhd#L39-L41)，确认 `dat_i` 位宽含 `* 2`、`dat_o` 不含。回答：若 `channels_g=2`、`in_fmt_g=(1,0,17)`（位宽 18），`dat_i` 多少位？`dat_o` 多少位？
2. 打开 [sim/config.tcl:415-421](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L415-L421)，找到 `channels_g=2` 且 `separate_g=true` 与 `separate_g=false` 的两组参数，对照 [doc/files/psi_fix_fir_3tap_hbw_dec2.md:29-52](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/psi_fix_fir_3tap_hbw_dec2.md#L29-L52) 的排布图，说明这两种模式下 `dat_i` 的 4 个样本分别属于哪个通道。

**需要观察的现象**：

- 步骤 1：`dat_i = 18 × 2 × 2 = 72` 位；`dat_o = 18 × 2 = 36` 位。输出位宽恰为输入的一半，对应「每 2 个输入样本出 1 个输出样本」的速率减半。
- 步骤 2：`separate_g=true` 时 4 个样本是 `[A0,A1,B0,B1]`（两通道各 2 样本，各出 1 个）；`separate_g=false` 时 4 个样本是同一源的 `[s0,s1,s2,s3]`，出 2 个样本 `[y0,y1]`，可再喂给下一级做抽取 4。

**预期结果**：端口位宽计算为 72/36；两种模式下的样本归属如上。无法本地运行硬件时，依据源码与文档静态推导即可，标注「待本地验证」的是实际仿真波形。

#### 4.3.5 小练习与答案

**练习 1**：要实现抽取 8（即 \(2^3\)），最少需要几个 `psi_fix_fir_3tap_hbw_dec2` 级联？每级 `separate_g` 应设成什么？

**答案**：3 级，每级抽取 2，\(2^3=8\)。级联时除第一级可按实际通道数配置外，后续各级用 `separate_g=false` 把上一级的多路输出当作同一源继续抽取。

**练习 2**：为什么本组件没有 `rdy` 反压握手（只有 `vld`）？

**答案**：半带抽取滤波器是无反馈的 FIR 结构，每拍处理成本恒定、延迟固定。组件假定上游稳定供数、下游恒定接收，故省略 `rdy` 简化握手（与 u3-l3 讲过的「部分组件省略 rdy 做无反压简化」一致）。若系统需要反压，需在外部加 FIFO。

---

## 5. 综合实践

**任务**：用「黄金模型 + RTL 源码」对照，亲手验证一个多通道半带抽取场景，并把结论与 u7-l1/u7-l2 的通用 FIR 结论串起来。

**操作步骤**：

1. **读黄金模型**：打开 [model/psi_fix_fir.py:40-41](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_fir.py#L40-L41)，找到 `accuFmt` 与 `roundFmt` 的定义。注意 `accuFmt=(1, outFmt.i+1, inFmt.f+coefFmt.f)`——累加器保留全精度，舍入只在末端。这正是 4.2 节里 `IntFmt_c` 多留小数位的「位真对应物」。
2. **跑黄金模型**（临时脚本，**不要**写进仓库）：用 `h=[0.25,0.5,0.25]`、`decimRate=2`、随机输入跑 `psi_fix_fir.Filter`，画出输入与抽取后输出的时序，确认输出点数是输入的一半。
3. **对照 RTL**：在 [hdl/psi_fix_fir_3tap_hbw_dec2.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_3tap_hbw_dec2.vhd) 里定位三个关键事实：
   - 系数化身 `Shifts_c=(2,1,2)`（L49）；
   - 中间精度 `IntFmt_c` 的 `F+2`（L50）；
   - 端口位宽体现的 dec2（L39-41）。
4. **串联结论**：写一段话说明——尽管 RTL 是「零乘法器、固定系数」的专用结构，它与 ser/par/semi 那些通用 FIR 共用同一个黄金模型 `psi_fix_fir.py`，印证了 u7-l1 的核心结论「所有 FIR 命名变体只描述 RTL 怎么算，数学上共享同一个位真模型」。

**预期结果**：

- 黄金模型输出点数 = 输入点数 / 2。
- RTL 三处源码事实与 4.1–4.3 的讲解一一对应。
- 串联段落能清楚说明「专用 RTL + 通用黄金模型」的位真契约关系。

若本机无 SciPy/仿真器，步骤 2 标注「待本地验证」，其余步骤可纯靠源码静态完成。

---

## 6. 本讲小结

- `psi_fix_fir_3tap_hbw_dec2` 是固定系数 \(h=[0.25,0.5,0.25]\)、抽取 2 的半带滤波器，是 FIR 家族里的「专用特例」，名字里的 `3tap_hbw` 不严格套用通用六字段模板。
- 半带的几何含义是截止频率落在 \(f_s/4\)（抽取 2 后的奈奎斯特点）；直流增益恰为 1、\(f_s/2\) 处衰减为 0。
- \(0.25=2^{-2}\)、\(0.5=2^{-1}\) 都是 2 的幂，故整个滤波器用「3 个右移 + 2 个加法」实现，**零乘法器/DSP**。
- 中间格式 `IntFmt_c` 多留 2 个小数位以留住移位精度，舍入/饱和只在末端 resize 到 `out_fmt_g` 时发生，与黄金模型「累加器全精度 + 末端舍入」位等价。
- dec2 体现在端口：`dat_i` 每通道 2 样本、`dat_o` 每通道 1 样本，输出速率是输入的一半；窗口跨拍，第一个抽头复用寄存过的上一拍样本。
- `separate_g=true` 为多通道并行独立滤波；`separate_g=false` 为单通道级联，多级串接可实现抽取 \(2^k\)。
- 位真契约：尽管 RTL 是专用结构，黄金参考仍是通用 `psi_fix_fir.py`——这是 FIR 全族共享的「实现各异、模型唯一」设计。

## 7. 下一步学习建议

- 若想看「需要乘法器、系数可配置」的通用 FIR 如何用同一份黄金模型实现，回到 **u7-l2**（ser/par/semi 多通道可配置 FIR）对照阅读，体会「专用 vs 通用」的资源-灵活性权衡。
- 若对「抽取/插值的采样率变换」结构感兴趣，可进入 **u6（CIC 滤波器）**，比较无乘法器的 CIC 与本讲的无乘法器半带 FIR 在原理上的异同（CIC 靠积分-梳状，半带靠 2 的幂系数）。
- 若想动手贡献一个类似的专用组件，参考 **u10-l1（贡献新组件的完整流程）**：写 Python 黄金模型 → 两段式 VHDL → preScript 协同仿真 → 注册到 `config.tcl`。
- 继续精读 [doc/files/psi_fix_fir_3tap_hbw_dec2.md](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/psi_fix_fir_3tap_hbw_dec2.md) 的三张结构图（`_a/_b/_c.png`），把本讲的端口与滑窗叙述与图示对应起来。
