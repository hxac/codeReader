# 饱和模式 FixSaturate

## 1. 本讲目标

本讲承接 [u2-l1 的定点格式 `[S,I,F]`](u2-l1-fixformat-representation.md) 与 [u2-l2 的舍入模式 `FixRound`](u2-l2-rounding-modes.md)。舍入解决的是「丢小数位」的问题，而本讲解决它的对偶问题：**丢整数位 / 符号位时该怎么办**。

学完本讲，你应当能够：

1. 说清楚「饱和（saturate）」与「告警（warn）」是两个相互独立的开关，因此组合出四种模式。
2. 区分 `None_s` / `Warn_s` / `Sat_s` / `SatWarn_s` 四种模式各自的输出行为与副作用。
3. 理解「不饱和时高位被直接丢弃」如何导致补码回绕（wrap），并会用模运算推算回绕后的值。
4. 读懂 VHDL 中 `cl_fix_saturate` 的三段式实现：`convert`（回绕）→ 告警 `assert` → 钳位 `if`。
5. 知道 `cl_fix_resize` 为什么必须是「先 round、后 saturate」的固定顺序。

本讲只讲饱和，不再重复舍入（见 u2-l2）与格式预测（见 u3）。

## 2. 前置知识

在进入源码前，先用三段话把背景说清楚。

**第一，定点格式的可表示范围由 `I` 和 `S` 决定。** 回顾 u2-l1：一个格式 `[S,I,F]` 的总位宽是 \(W = S+I+F \)。它最多能表示到（任意 `S`）

\[
v_{\max} = 2^{I} - 2^{-F},
\]

最小值在有符号时（\(S=1\)）为 \(v_{\min} = -2^{I}\)，无符号时（\(S=0\)）为 \(0\)。一旦 `I` 变小、或 `S` 从 1 变 0，这个范围就会被「挤窄」。

**第二，硬件里没有真正的溢出异常，只有「多余的位被扔掉」。** 把一个 9 位的数写进 5 位的寄存器，多余的高 4 位在物理上不存在，等价于直接丢弃。对补码来说，丢高位 = 对 \(2^{W}\) 取模，于是越界的值会「绕回」到合法区间的另一头，这种现象叫**回绕（wrap）**。

**第三，回绕在信号处理里通常是灾难。** 一个本该是 `+100` 的增益结果回绕成负数，会让后续整条数据通路彻底错乱。所以库提供了「饱和」选项：越界时不回绕，而是把输出**钳位（clamp）**到最近的可表示边界（`v_max` 或 `v_min`），同时可选地发出告警。本讲要回答的核心问题就是：**回绕、钳位、告警这三件事如何被两个开关组合成四种模式。**

## 3. 本讲源码地图

本讲涉及的真实源码文件如下（均位于当前 HEAD `e9123a9`）：

| 文件 | 作用 |
| --- | --- |
| `bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py` | Python 端 `FixSaturate` 枚举定义 |
| `bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py` | Python 主接口 `cl_fix_saturate` / `cl_fix_resize` / `cl_fix_in_range` |
| `bittrue/models/python/en_cl_fix_pkg/narrow_fix.py` | ≤53 位的 `NarrowFix.saturate` 真正实现（告警 + 回绕 + 钳位） |
| `hdl/en_cl_fix_pkg.vhd` | VHDL 端 `FixSaturate_t` 类型、`cl_fix_saturate`、内部 `convert`、`cl_fix_in_range`、`cl_fix_resize` |
| `README.md` | 官方 `Saturation Modes` 表格与文字说明 |

记住 u1-l2 的核心结论：**VHDL 是金标准语义，Python 同名同参数镜像之作参考模型**。所以你会看到 `FixSaturate_t`（VHDL）与 `FixSaturate`（Python）一一对应，下面的讲解会两相对照。

## 4. 核心概念与源码讲解

### 4.1 饱和问题：当整数位 / 符号位被压缩时

#### 4.1.1 概念说明

「饱和」这个词很容易让人误以为它总是发生。其实不是——饱和**只在结果格式的 `I` 和/或 `S` 比输入格式更小时才可能触发**：

- 减小 `I`：可表示的最大值变小，大正数会越上界。
- 把 `S` 从 1 降到 0（有符号转无符号）：负数在新格式里根本无法表示，会全部越下界。

如果 `I` 和 `S` 都没减小（甚至增大），那么任何输入值都能被新格式原样表示，饱和逻辑什么也不会做。这一点 README 说得很明确：

> Saturation behavior is relevant when the number of integer bits `I` is decreased and/or the number of sign bits `S` is decreased (signed to unsigned).

关键在于：**当范围被挤窄、输入值又落在新范围之外时，库给你两种处理方式**——回绕（扔掉高位）或钳位（钉死在边界）。`FixSaturate` 就是让你选这件事的开关。

#### 4.1.2 核心流程

设输入格式为 `aFmt`、结果格式为 `rFmt`，且 `rFmt.F == aFmt.F`（饱和阶段绝不动小数位，理由见 4.4）。把值 \(v\) 从 `aFmt` 搬进 `rFmt` 时，本质上是把它对齐到 `rFmt` 的位宽上：

- **回绕（wrap）**：只保留 `rFmt` 宽度内的低位，等价于模运算。对有符号目标：

  \[
  v_{\text{wrap}} = \big((v + 2^{I_r}) \bmod 2^{I_r+1}\big) - 2^{I_r},
  \]

  其中 \(I_r\) 是 `rFmt.I`。对无符号目标：\(v_{\text{wrap}} = v \bmod 2^{I_r}\)。

- **钳位（saturate / clamp）**：越上界取 `v_max`，越下界取 `v_min`：

  \[
  v_{\text{sat}} = \min(\max(v,\; v_{\min}),\; v_{\max}).
  \]

注意两者的本质差别：回绕改变值的「数量级」（+100 可能变成 −4），输出仍是某个合法编码但语义全错；钳位牺牲一点幅度但保证单调性，输出永远是「最接近的合法值」。

#### 4.1.3 源码精读：`convert` 就是「None_s 饱和」

回绕这件事在 VHDL 里并没有一个单独的 `wrap` 函数，而是复用了一个内部转换函数 `convert`。它的注释一语道破天机——它本身就是在做「不带饱和的格式转换」，也就是 `None_s` 模式下的饱和行为：

[convert（hdl/en_cl_fix_pkg.vhd:329-351）](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L329-L351) — 内部函数，按二进制小数点对齐把 `a` 写入 `result_v`，注释明说「this implements cl_fix_saturate, with None_s saturation mode」。

关键三行：

```vhdl
constant offset_c   : natural := rFmt.F - aFmt.F;          -- 小数点位偏移
...
if aFmt.S = 0 then
    result_v(...) := std_logic_vector(resize(unsigned(a_c), r_width_c - offset_c));
else
    result_v(...) := std_logic_vector(resize_sensible(signed(a_c), r_width_c - offset_c));
end if;
```

`resize` 把数据搬进 `r_width_c - offset_c` 位的目标：**目标更窄时它丢弃高位（= 回绕），目标更宽时它做符号扩展或零扩展**。所以「回绕」并不是某种特殊运算，而是「位宽不够就自然丢高位」这一物理事实的直接体现。

#### 4.1.4 代码实践：用 Python 观察回绕

1. **实践目标**：亲手确认「丢高位 = 模运算」这一直觉。
2. **操作步骤**：在仓库根目录启动 Python，构造一个落在 `[1,8,0]` 范围内、却越出 `[1,4,0]` 范围的值，用 `None_s` 饱和搬过去：

   ```python
   from en_cl_fix_pkg import en_cl_fix as fx
   from en_cl_fix_pkg.en_cl_fix_types import FixFormat, FixSaturate

   a_fmt = FixFormat(1, 8, 0)   # 范围 -256 .. 255
   r_fmt = FixFormat(1, 4, 0)   # 范围 -16  .. 15

   for v in (100, -100, 10):
       r = fx.cl_fix_saturate(v, a_fmt, r_fmt, FixSaturate.None_s)
       print(v, "->", fx.cl_fix_to_real(r, r_fmt))
   ```

3. **需要观察的现象**：`+10` 原样返回；`+100` 与 `−100` 都被「折回」到 `[−16, 15]` 区间内的小值。
4. **预期结果**：`+100 → +4`，`−100 → −4`（见 4.3.4 的手工推算），`+10 → +10`。这正是上面的模运算公式给出的结果。若结果不符，请确认 `r_fmt.F == a_fmt.F`（这里都是 0）。
5. 若本地未装 numpy，此步为「待本地验证」。

#### 4.1.5 小练习与答案

**练习**：把 `r_fmt` 改成无符号 `[0,4,0]`（范围 `0 .. 15`），输入 `−1`。用 `None_s` 模式，输出会是多少？

**答案**：`−1` 在补码里是全 1，丢到高位后低 5 位（含目标位宽）全 1，按无符号解释即为 `15`。回绕把负数变成了大正数——这正是「有符号转无符号且不饱和」最危险的坑。

---

### 4.2 FixSaturate 四种模式：两个独立开关

#### 4.2.1 概念说明

上一节看到，面对越界值，库可以做两件互相独立的事：

1. **是否钳位（Saturate?）**——把输出钉死在边界，还是放任它回绕。
2. **是否告警（Warn?）**——检测到越界时，是否向上层报告。

既然是两个独立的布尔开关，自然有 \(2 \times 2 = 4\) 种组合，这就是 `FixSaturate` 的四种模式。理解了「两个开关」，你就无需死记四种模式——把它们当笛卡尔积即可。

#### 4.2.2 核心流程

四种模式的真值表（与 README 完全一致）：

| 模式 | Saturate?（钳位） | Warn?（告警） | 越界时输出 | 越界时副作用 |
| --- | :---: | :---: | --- | --- |
| `None_s`   | 否 | 否 | 回绕 | 无 |
| `Warn_s`   | 否 | 是 | 回绕 | 发出告警 |
| `Sat_s`    | 是 | 否 | 钳位到边界 | 无 |
| `SatWarn_s`| 是 | 是 | 钳位到边界 | 发出告警 |

一句话记忆：**名字里带 `Sat` 的会钳位，带 `Warn` 的会告警，`None` 两者都不做。**

#### 4.2.3 源码精读：枚举的镜像定义

Python 端的定义（带注释说明四种模式的语义）：

[FixSaturate（en_cl_fix_types.py:43-50）](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L43-L50)

```python
class FixSaturate(Enum):
    None_s = 0          # No saturation, no warning.
    Warn_s = 1          # No saturation, only warning.
    Sat_s = 2           # Only saturation, no warning.
    SatWarn_s = 3       # Saturation and warning.
```

VHDL 端逐字镜像（连注释都对应），定义在包头：

[FixSaturate_t（hdl/en_cl_fix_pkg.vhd:60-66）](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L60-L66)

```vhdl
type FixSaturate_t is
(
    None_s,         -- No saturation, no warning.
    Warn_s,         -- No saturation, only warning.
    Sat_s,          -- Only saturation, no warning.
    SatWarn_s       -- Saturation and warning.
);
```

README 的官方表格（含「不饱和就回绕」「告警由仿真器/软件环境发出」两段文字说明）在这里：

[Saturation Modes（README.md:175-188）](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md#L175-L188) — 注意它强调：`If saturation is not enabled, then MSBs are simply discarded, causing any out-of-range values to "wrap".`

#### 4.2.4 代码实践：把四种模式打印成真值表

1. **实践目标**：建立「两个开关」与「四种枚举」之间的肌肉记忆。
2. **操作步骤**：

   ```python
   from en_cl_fix_pkg.en_cl_fix_types import FixSaturate
   for s in FixSaturate:
       do_sat = "Sat" in s.name
       do_warn = "Warn" in s.name
       print(f"{s.name:10s}  saturate={do_sat}  warn={do_warn}")
   ```

3. **需要观察的现象**：四行的两个布尔列正好组成 `00 / 01 / 10 / 11` 的完整组合。
4. **预期结果**：`None_s → F/F`、`Warn_s → F/T`、`Sat_s → T/F`、`SatWarn_s → T/T`。

#### 4.2.5 小练习与答案

**练习**：你在一个调试阶段的设计里，怀疑某级乘法偶尔溢出，但你暂时不想改变数据通路的数值行为（怕掩盖问题），只想让仿真器在越界时大叫一声。该选哪种模式？

**答案**：`Warn_s`。它只告警、不钳位，输出仍按回绕走，因此不会改变数值，只会暴露问题。等定位修复后，正式版本通常再换成 `SatWarn_s` 或 `Sat_s`。

---

### 4.3 cl_fix_saturate 的 VHDL 实现：告警与钳位

#### 4.3.1 概念说明

理解了四种模式的语义，现在看 VHDL 如何用一段紧凑的代码同时实现「回绕 + 告警 + 钳位」。核心设计很优雅：**回绕这一步永远执行**（它就是 4.1 里那个 `convert`），**钳位只是在检测到越界时把结果改写成边界值**，**告警只是独立的一条 `assert`**。三件事彼此正交，恰好对应上一节的两个开关。

#### 4.3.2 核心流程

`cl_fix_saturate(a, a_fmt, result_fmt, saturate)` 的执行流程：

```text
1. 断言 result_fmt.F == a_fmt.F          （小数位不可变，否则报 Failure）
2. result_v := convert(a, a_fmt, result_fmt)   ← 永远做：对齐 + 丢高位 = 回绕结果
3. 若模式带 Warn (Warn_s / SatWarn_s):
       assert cl_fix_in_range(a, a_fmt, result_fmt)  severity Warning  ← 越界才叫
4. 若模式带 Sat (Sat_s / SatWarn_s):
       若 a < min_value(result_fmt):  result_v := min_value
       若 a > max_value(result_fmt):  result_v := max_value             ← 覆盖回绕值
5. return result_v
```

注意第 4 步：即便在 `Sat_s` 模式下，第 2 步的 `convert` 也照跑，只是当值真的越界时被第 4 步覆盖掉。这样同一段 `convert` 逻辑被所有模式复用，没有分支爆炸。

#### 4.3.3 源码精读

主体实现（精简后保留三段）：

[cl_fix_saturate（hdl/en_cl_fix_pkg.vhd:978-1007）](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L978-L1007)

```vhdl
assert result_fmt.F = a_fmt.F report "cl_fix_saturate: Number of frac bits cannot change." severity Failure;

-- Saturation warning
if saturate = Warn_s or saturate = SatWarn_s then
    assert cl_fix_in_range(a, a_fmt, result_fmt)
        report "cl_fix_saturate : Saturation warning!" severity Warning;
end if;

-- Write the input value into result_v with correct binary point alignment.
result_v := convert(a, a_fmt, result_fmt);   -- 回绕（None_s 行为）

-- Saturate
if saturate = Sat_s or saturate = SatWarn_s then
    if cl_fix_compare("<", a, a_fmt, cl_fix_min_value(result_fmt), result_fmt) then
        result_v := cl_fix_min_value(result_fmt);
    elsif cl_fix_compare(">", a, a_fmt, cl_fix_max_value(result_fmt), result_fmt) then
        result_v := cl_fix_max_value(result_fmt);
    end if;
end if;
```

三个支撑函数：

- [cl_fix_in_range（hdl/en_cl_fix_pkg.vhd:1024-1039）](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1024-L1039) — 判断 `a` 经舍入后是否落在 `result_fmt` 的 `[min, max]` 内，告警 `assert` 就直接断言它的返回值（VHDL 中 `assert` 在条件为 **假** 时触发，所以越界才会报告）。
- [cl_fix_min_value（hdl/en_cl_fix_pkg.vhd:380-390）](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L380-L390) — 有符号时符号位置 1、其余位置 0（即 \(-2^{I}\)）；无符号时全 0。
- [cl_fix_max_value（hdl/en_cl_fix_pkg.vhd:370-378）](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L370-L378) — 全 1，再把符号位（若有）清 0。

另外注意 `cl_fix_compare` 比较的是「按各自格式解释后的数值大小」，所以即便 `a` 与边界值位宽不同也能正确比大小——这正是第 4 步钳位判定的可靠依据。

#### 4.3.4 代码实践：`[1,8,0]` 压到 `[1,4,0]` 的四种结局

这是本讲的主实践（对应规格要求）。

1. **实践目标**：把 README 表格落到一个具体例子上，并亲手在源码里找到「触发告警的那条 assert」。
2. **场景设定**：源格式 `[1,8,0]`（范围 \(-256 .. 255\)），目标格式 `[1,4,0]`（范围 \(-16 .. 15\)）。取三个代表值：`+100`（越上界）、`−100`（越下界）、`+10`（在界内）。
3. **操作步骤一（手工推算 + 表格）**：按 4.1.2 的回绕公式与钳位规则填写下表。

   | 输入值 | `None_s` | `Warn_s` | `Sat_s` | `SatWarn_s` |
   | :---: | :---: | :---: | :---: | :---: |
   | `+100` | +4（回绕） | +4 + 告警 | +15（钳位） | +15 + 告警 |
   | `−100` | −4（回绕） | −4 + 告警 | −16（钳位） | −16 + 告警 |
   | `+10`  | +10 | +10 | +10 | +10 |

   推算依据：\(100 \bmod 32 = 4\)，\((-100) \bmod 32 = 28\)，而 28 在 5 位有符号里解释为 \(28-32 = -4\)；钳位时 `+100 > 15` 取 15，`−100 < −16` 取 −16。

4. **操作步骤二（定位告警 assert）**：打开 [cl_fix_saturate（hdl/en_cl_fix_pkg.vhd:989-992）](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L989-L992)，确认告警由 `if saturate = Warn_s or saturate = SatWarn_s then assert cl_fix_in_range(...) ... severity Warning` 这一段触发——只有名字里带 `Warn` 的两种模式会进入这个分支，且仅在 `cl_fix_in_range` 返回 **假**（即越界）时才真正打印 `"Saturation warning!"`。
5. **需要观察的现象**：`Sat_s` 与 `SatWarn_s` 的输出数值相同（都是钳位值），区别仅在于后者多打一条告警；`None_s` 与 `Warn_s` 的输出数值相同（都是回绕值），区别同样仅是告警。
6. **预期结果**：与上表一致。若用 VHDL 仿真验证，`Warn_s`/`SatWarn_s` 两行应能在仿真日志里看到 `Warning` 级别的 `Saturation warning!`。
7. 若本地无仿真器，步骤一为「源码阅读型实践」，步骤二为「定位 assert」即可，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `cl_fix_saturate` 第一行要 `assert result_fmt.F = a_fmt.F`？如果允许改小数位会发生什么？

**答案**：饱和只负责处理整数位/符号位的范围收缩，**完全不碰小数位**。若允许 `F` 改变，就等于把「舍入」的职责塞进了饱和函数，职责混淆且无法选择舍入模式。需要改 `F` 时必须先 `cl_fix_round`（见 u2-l2）再 `cl_fix_saturate`。

**练习 2**：在 `Sat_s` 模式下，第 2 步的 `convert` 是否多余？

**答案**：不多余。当值**未越界**时（如 `+10`），第 4 步的 `if` 不命中，结果就来自第 2 步的 `convert`；只有越界时才被第 4 步覆盖。所以 `convert` 负责常态，`if` 负责异常态，两者协作。

---

### 4.4 Python 参考模型与 `cl_fix_resize` 的组合

#### 4.4.1 概念说明

承接 u1-l1 的核心范式：Python 端 `cl_fix_saturate` 与 VHDL 同名同语义，只是内部按位宽分发到 `NarrowFix`（≤53 位，双精度浮点，快）或 `WideFix`（任意精度整数）。最常见的是 `NarrowFix`，它的 `saturate` 方法把 4.3 的三段式用 Python/numpy 表达出来：用 `warnings.warn` 告警、用模运算回绕、用 `np.where` 钳位。

更重要的是，饱和几乎从不被单独调用——它总是作为 `cl_fix_resize` 的最后一步出现。`cl_fix_resize = 先 round、后 saturate`，这个顺序**不可交换**，是理解整个库精度模型的钥匙（详见 u4-l1）。

#### 4.4.2 核心流程

`cl_fix_resize` 三段式（两语言一致）：

```text
1. rounded_fmt = cl_fix_round_fmt(a_fmt, r_fmt.F, round)   ← 预测舍入后格式
2. rounded     = cl_fix_round(a, a_fmt, rounded_fmt, round) ← 无损对齐小数位
3. result      = cl_fix_saturate(rounded, rounded_fmt, r_fmt, sat) ← 再压整数位
```

为什么先 round？因为 `cl_fix_saturate` 要求 `result_fmt.F == a_fmt.F`（见 4.3.5）。要改 `F`，必须先用 round 把小数位对齐到目标 `F`，得到一个 `F` 已经正确的中间格式 `rounded_fmt`，再交给 saturate 去处理整数位。所以 **round 管 F、saturate 管 I/S**，职责通过 resize 串联。

#### 4.4.3 源码精读

VHDL 的 `cl_fix_resize`，三行正好对应三段式，最后一行直接调 `cl_fix_saturate`：

[cl_fix_resize（hdl/en_cl_fix_pkg.vhd:1009-1022）](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1009-L1022)

```vhdl
constant rounded_fmt_c : FixFormat_t := cl_fix_round_fmt(a_fmt, result_fmt.F, round);
constant rounded_c     : std_logic_vector := cl_fix_round(a, a_fmt, rounded_fmt_c, round);
begin
    return cl_fix_saturate(rounded_c, rounded_fmt_c, result_fmt, saturate);
```

Python 主接口的 `cl_fix_saturate`，结构是「分发到 NarrowFix / WideFix」：

[cl_fix_saturate（en_cl_fix.py:215-237）](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L215-L237) — 同样先 `assert r_fmt.F == a_fmt.F`，再按 `cl_fix_is_wide` 分发。

真正的告警/回绕/钳位逻辑在 NarrowFix：

[NarrowFix.saturate（narrow_fix.py:190-244）](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L190-L244) — 三段与 VHDL 完全对应：

```python
# 告警：只有 Warn_s / SatWarn_s
if sat == FixSaturate.Warn_s or sat == FixSaturate.SatWarn_s:
    if np.any(self > fmt_max) or np.any(self < fmt_min):
        warnings.warn("NarrowFix.saturate : Saturation warning!", Warning)

# 回绕（None_s / Warn_s）：有符号用 ((v+2^I) % 2^(I+1)) - 2^I
if sat == FixSaturate.None_s or sat == FixSaturate.Warn_s:
    if r_fmt.S == 1:
        sat_data = ((data + 2.0 ** r_fmt.I) % (2.0 ** (r_fmt.I + 1))) - 2.0 ** r_fmt.I
    ...
# 钳位（Sat_s / SatWarn_s）
else:
    sat_data = np.where(self > fmt_max, fmt_max._data, data)
    sat_data = np.where(self < fmt_min, fmt_min._data, sat_data)
```

把这段和 4.3.3 的 VHDL 并排看：`warnings.warn` 对应 VHDL 的 `assert ... severity Warning`；回绕的模运算对应 VHDL 的 `convert`；`np.where` 钳位对应 VHDL 的 `if ... := min/max_value`。**两份代码在数学上等价**，这正是「Python 作参考模型」能成立的根基。

> **跨语言小差异（请留意）**：`cl_fix_resize` 的饱和默认值在两语言里并不相同——VHDL 默认 `Warn_s`（[hdl:1014](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1014)），Python 默认 `None_s`（[en_cl_fix.py:242](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L242)）。把 Python 当参考模型比对 HDL 时，务必显式指定 `sat` 参数，避免因默认值不同而误判。这是源码中可验证的事实，建议本地再次核对。

#### 4.4.4 代码实践：用 `cl_fix_resize` 同时观察舍入与饱和

1. **实践目标**：在一个调用里同时触发舍入（改 `F`）与饱和（改 `I`），体会「先 round 后 saturate」。
2. **操作步骤**：

   ```python
   from en_cl_fix_pkg import en_cl_fix as fx
   from en_cl_fix_pkg.en_cl_fix_types import FixFormat, FixRound, FixSaturate

   a_fmt = FixFormat(1, 8, 8)   # 大范围、高精度
   r_fmt = FixFormat(1, 4, 2)   # 既砍整数位(8->4)，又砍小数位(8->2)

   for sat in (FixSaturate.None_s, FixSaturate.Sat_s):
       r = fx.cl_fix_resize(150.625, a_fmt, r_fmt,
                            rnd=FixRound.NonSymPos_s, sat=sat)
       print(sat.name, "->", fx.cl_fix_to_real(r, r_fmt))
   ```

3. **需要观察的现象**：`None_s` 下 `150.625` 会因回绕变成一个完全不同的小值；`Sat_s` 下则被钉在 `r_fmt` 的上界 `15.75`。
4. **预期结果**：`Sat_s → 15.75`；`None_s` 的具体回绕值可手算验证（`150.625` 先按 NonSymPos 舍入到 `F=2` 得 `150.75`，再回绕：\(((150.75 + 2^4) \bmod 2^5) - 2^4 = ((150.75+16) \bmod 32) - 16 = (166.75 \bmod 32) - 16 = (166.75 - 160) - 16 = 6.75 - 16 = -9.25\)）。若手算与程序不符，先检查 `for_round` 是否给中间格式 +1 了整数位。
5. 若本地未装 numpy，此步为「待本地验证」。

#### 4.4.5 小练习与答案

**练习**：把上面示例中的 `sat=FixSaturate.Warn_s` 也跑一遍，它的返回值会与哪种模式相同？为什么？

**答案**：与 `None_s` 相同（都是回绕值）。因为 `Warn_s` 不钳位，数值行为与 `None_s` 完全一致，只是会在越界时额外打印一条 `Saturation warning!`。这再次印证「告警与钳位是两个独立开关」。

---

## 5. 综合实践

**任务**：为一个「乘法后压缩位宽」的迷你数据通路选择饱和策略，并把整条链路在 Python 里跑通。

设想：两个输入 `a=[0,7,8]`（无符号、范围 \(0 .. 255.996\)）与 `b=[1,0,8]`（有符号、范围 \(-1 .. 0.996\)）相乘，结果想存进 `r=[1,4,8]`（范围 \(-16 .. 15.996\)）。由于乘积会超出 `r` 的范围，必须决定如何饱和。

请完成：

1. 用 `FixFormat.for_mult(a_fmt, b_fmt)` 算出全精度乘积格式（承接 u3），确认它比 `r` 宽很多。
2. 写一段 Python：`prod = cl_fix_mult(a_val, a_fmt, b_val, b_fmt)`，取 `a_val=200.5`、`b_val=-0.9`。
3. 分别用 `cl_fix_resize(prod, prod_fmt, r_fmt, rnd=NonSymPos_s, sat=None_s)` 与 `sat=SatWarn_s` 压缩，用 `cl_fix_to_real` 打印两者结果。
4. 解释：为什么 `None_s` 的结果是个「莫名其妙的小负数」，而 `SatWarn_s` 的结果是 `-16`？哪一种更适合作为最终交付的硬件行为？哪一种更适合调试？

**参考结论**：`None_s` 因回绕产生语义错误值，适合调试时故意暴露问题；`SatWarn_s` 钳位到 `-16`（下界）并告警，数值语义可控，是交付硬件的常规选择。这个练习把 for_mult（u3）、round（u2-l2）、saturate（本讲）三者串起来，构成一条完整的定点数据通路。

## 6. 本讲小结

- 饱和只在结果格式的 `I` 和/或 `S` 比输入更小时才可能触发；减小 `F` 属于舍入，不归饱和管。
- `FixSaturate` 的四种模式 = 「是否钳位」×「是否告警」两个独立开关的笛卡尔积：`None_s` / `Warn_s` / `Sat_s` / `SatWarn_s`。
- 不钳位时高位被直接丢弃，补码下表现为回绕（模运算），可能把 `+100` 变成 `+4`；钳位时输出钉在 `v_max` / `v_min`。
- VHDL `cl_fix_saturate` 用三段式实现：`convert`（永远回绕）+ 告警 `assert`（仅 `Warn` 模式）+ 钳位 `if`（仅 `Sat` 模式），三段正交。
- 告警在 VHDL 里是 `assert ... severity Warning`，在 Python 里是 `warnings.warn`，语义镜像。
- `cl_fix_resize = 先 round（对齐 F）后 saturate（对齐 I/S）`，顺序不可交换，因为 saturate 要求 `F` 不变。

## 7. 下一步学习建议

- **下一讲 [u2-l4](u2-l4-width-minmax-union-helpers.md)**：系统学习 `cl_fix_width` / `cl_fix_max_value` / `cl_fix_min_value` / `union` 等格式工具函数——本讲多次用到的 `min_value` / `max_value` 就在那里正式讲解。
- **进阶 [u3](../)**：进入「结果格式预测」单元，看 `cl_fix_add_fmt` / `cl_fix_mult_fmt` 如何在编译期算出最坏情况下的位宽增长，从而决定是否需要饱和。
- **深入 VHDL 实现 [u5-l2](u5-l2-round-saturate-resize-impl.md)**：从组件层面再读一遍 `convert` / `cl_fix_round` / `cl_fix_saturate` / `cl_fix_resize` 的内部协作。
- **建议阅读的源码**：动手对照 [NarrowFix.saturate（narrow_fix.py:190-244）](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L190-L244) 与 [cl_fix_saturate（hdl/en_cl_fix_pkg.vhd:978-1007）](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L978-L1007)，亲手验证「Python 参考模型与 VHDL 在饱和行为上逐拍等价」。
