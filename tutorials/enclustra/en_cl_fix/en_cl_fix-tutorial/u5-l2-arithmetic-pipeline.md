# 算术运算链路：convert → compute → resize

## 1. 本讲目标

本讲把前面几讲学过的「格式推导（mid_fmt）」「舍入」「饱和」串成一条完整的算术执行链路。读完本讲，你应当能够：

- 说出 `cl_fix_add` / `cl_fix_sub` / `cl_fix_mult` / `cl_fix_neg` / `cl_fix_abs` / `cl_fix_shift` 共同遵守的**三段式骨架**：先算全精度中间格式 `mid_fmt`，再在其中无损运算，最后 `resize` 到目标格式 `r_fmt`。
- 解释为什么两个操作数必须先 **convert（对齐）** 到 `mid_fmt` 才能相加减，以及 VHDL `convert` 如何靠补零与符号扩展对齐小数点。
- 理解 `cl_fix_mult` 在 VHDL 里如何用**局部重载的 `*` 算子**解决 `signed * unsigned` 这一语言未定义情形，并用 `resize_sensible` 收缩位宽。
- 理解 `cl_fix_shift` 如何用一个巧妙的 **dummy_fmt（哑格式）** 把「移位」转化成一次 `resize`，从而做到「移位本身不丢任何位」。

本讲是单元 5 的第二讲，承接 u5-l1（浮点/整数与定点互转）和 u3-l2（乘法/取反/移位的结果格式推导）。

## 2. 前置知识

本讲默认你已经掌握以下概念（若不熟悉，请先阅读对应讲义）：

- **定点格式 `[S, I, F]`**：S 符号位、I 整数位、F 小数位，总位宽 `S+I+F`（见 u1-l2）。
- **mid_fmt（全精度中间格式）**：运算结果在「不溢出、不浪费」前提下的最小格式，由 `for_add / for_sub / for_mult / for_neg / for_abs / for_shift` 等「保守推导」函数算出（见 u3-l1、u3-l2）。
- **resize = round + saturate**：先按舍入模式对齐 LSB（小数位），再按饱和模式收拢 MSB（整数/符号位），顺序不可对调（见 u4-l3）。
- **NarrowFix / WideFix 双表示**：位宽 ≤ 53 走 NarrowFix（float64），否则走 WideFix（任意精度整数）（见 u6 系列，本讲只需知道有这条分发即可）。
- **归一化 vs 非归一化**：定点值 = 整数 × 2^(-F)（见 u5-l1）。

一个贯穿全讲的关键直觉：**两数相加前必须先把小数点对齐**。十进制里算 `1.5 + 2.25` 你会把它们写成 `1.50 + 2.25`；定点数也一样，两个格式 F 不同的操作数，在二进制世界里小数点位置不同，必须先对齐（补 LSB 零）才能逐位相加。这个「对齐」动作就是 `convert`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py) | Python 主接口。本讲重点关注 `cl_fix_add/sub/addsub/mult/shift` 与 `cl_fix_resize`，它们共同体现三段式骨架。 |
| [hdl/en_cl_fix_pkg.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd) | VHDL 镜像。本讲精读内部函数 `convert`、`resize_sensible`，以及 `cl_fix_mult`（含局部 `*` 重载）、`cl_fix_shift`（含 dummy_fmt）的函数体。 |
| [bittrue/models/python/en_cl_fix_pkg/narrow_fix.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py) | NarrowFix 类。本讲引用其 `add/mult/shift` 方法与运算符重载，说明 Python 侧「运算」这一段如何落地。 |
| [bittrue/tests/python/en_cl_fix_pkg_test.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py) | Python 单元测试。本讲的代码实践以其中 `cl_fix_mult_Test` 的用例为蓝本。 |

---

## 4. 核心概念与源码讲解

### 4.1 统一三段式骨架：mid_fmt → 运算 → resize

#### 4.1.1 概念说明

en_cl_fix 的每一个算术函数（加、减、加减、乘、取反、绝对值、移位）都长着同一副骨架。这副骨架把「算得对」和「放得下」两件事彻底解耦：

1. **算得对**：先算出一个「全精度」中间格式 `mid_fmt`（由 u3 系列的 `for_*` 函数给出），在这个格式里做运算，保证**任何合法输入都不会溢出、也不会丢精度**。这一段纯粹是数学运算，不关心结果要存成什么样。
2. **放得下**：把全精度结果 `mid` 用 `cl_fix_resize` 收拢到调用者真正想要的目标格式 `r_fmt`（舍入 + 饱和）。

这样做的好处是：运算逻辑只写一遍（在 `mid_fmt` 里），所有「截断/进位/饱和」的细节统一交给 `resize`，不会在每个算术函数里重复实现舍入和饱和。

#### 4.1.2 核心流程

以两操作数运算（add/sub/mult）为例，伪代码如下：

```
def cl_fix_XXX(a, a_fmt, b, b_fmt, r_fmt=None, rnd=Trunc_s, sat=None_s):
    # 第一段：算全精度中间格式
    mid_fmt = cl_fix_XXX_fmt(a_fmt, b_fmt)        # 保守推导，见 u3
    if r_fmt is None:
        r_fmt = mid_fmt                            # 缺省即「要全精度」，零成本默认

    # 第二段：在 mid_fmt 里无损运算（先把两操作数对齐到 mid_fmt）
    a = <NarrowFix 或 WideFix>(a, a_fmt)
    b = <NarrowFix 或 WideFix>(b, b_fmt)
    mid = a <op> b                                 # +, -, *

    # 第三段：收拢到目标格式
    return cl_fix_resize(mid._data, mid_fmt, r_fmt, rnd, sat)
```

三段式里有一个贯穿全库的**重要约定**：`r_fmt` 缺省（Python 传 `None`、VHDL 传 `NullFixFormat_c`）时，就等于 `mid_fmt`。也就是说，「我要全精度」是零成本的默认行为；想要截断或饱和，必须**显式**给出更窄的 `r_fmt` 并配上 `rnd` / `sat`。这让「精度」成为默认，「有损」必须 opt-in。

#### 4.1.3 源码精读

先看 Python 侧 `cl_fix_add` 的完整函数体，它是三段式最标准的样板：

[en_cl_fix.py:313-342](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L313-L342) —— `cl_fix_add`：先用 `cl_fix_add_fmt(a_fmt, b_fmt)` 算 `mid_fmt`（第一段）；`r_fmt is None` 时回退到 `mid_fmt`（缺省约定）；按 `cl_fix_is_wide` 把两操作数包成 `NarrowFix` 或 `WideFix`（第二段的「对齐」在类构造时完成）；执行 `a+b`（真正的运算）；最后 `cl_fix_resize(...)` 收拢（第三段）。

```python
# 第一段：全精度中间格式
mid_fmt = cl_fix_add_fmt(a_fmt, b_fmt)
if r_fmt is None:
    r_fmt = mid_fmt

# 第二段：对齐 + 运算（这里 a/b 已是 NarrowFix 或 WideFix）
a = NarrowFix(a, a_fmt, copy=False)
b = NarrowFix(b, b_fmt, copy=False)
mid = a+b

# 第三段：收拢到目标格式
return cl_fix_resize(mid._data, mid_fmt, r_fmt, rnd, sat)
```

`cl_fix_sub` 与之**逐行同构**，只是把 `cl_fix_add_fmt` 换成 `cl_fix_sub_fmt`、把 `a+b` 换成 `a-b`：

[en_cl_fix.py:345-374](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L345-L374) —— `cl_fix_sub`。

`cl_fix_addsub`（同一套数据按 `add` 标志位选择加或减）甚至不自己实现运算，而是**同时**调用 `cl_fix_add` 和 `cl_fix_sub`，再用 `np.where(add, radd, rsub)` 按位选择：

[en_cl_fix.py:377-389](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L377-L389) —— `cl_fix_addsub`：注意它复用 `r_fmt`，因此 `r_fmt` 必须能同时容纳加和减两种结果，这正是 u3-l1 里 `for_addsub = union(for_add, for_sub)` 的用途。

第三段的 `cl_fix_resize` 本身就是 u4-l3 讲过的「先 round 后 saturate」：

[en_cl_fix.py:240-253](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L240-L253) —— `cl_fix_resize`：先用 `FixFormat.for_round` 算出「舍入后的中间格式」`rounded_fmt`，`cl_fix_round` 对齐 LSB，再 `cl_fix_saturate` 收拢 MSB。所有算术函数的「收口」都汇聚到这两个函数。

> 补充：Python 侧的 narrow/wide 分发逻辑（`cl_fix_is_wide` 判定、`from_narrowfix` 互转）将在 u6-l3 专讲。本讲你只需注意 `cl_fix_add` 里这一段：
> [en_cl_fix.py:329-339](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L329-L339) —— 只要 `a_fmt`、`b_fmt`、`mid_fmt` 任一是 wide，就把两个操作数都抬升到 `WideFix`，保证两操作数类型一致后再相加。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认 add/sub/mult/shift/neg/abs 六个函数真的共用同一副三段式骨架。

**步骤**：

1. 打开 [en_cl_fix.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py)，定位 `cl_fix_add`（L313）、`cl_fix_sub`（L345）、`cl_fix_mult`（L392）、`cl_fix_neg`（L286）、`cl_fix_abs`（L259）、`cl_fix_shift`（L424）。
2. 逐个对照，确认它们都包含这三段：①`mid_fmt = cl_fix_XXX_fmt(...)`；②`if r_fmt is None: r_fmt = mid_fmt`；③结尾 `return cl_fix_resize(mid._data, mid_fmt, r_fmt, rnd, sat)`。

**需要观察的现象**：六个函数的差异**仅在三处**——算 `mid_fmt` 用的格式函数不同、运算符不同（`+`/`-`/`*`/`-a`/`abs`/`<<`）、操作数个数不同（一元 vs 二元）。骨架本身完全一致。

**预期结果**：你会得到一张表，每行一个函数，三段填空都能对上。这说明 en_cl_fix 把「运算」和「定标（resize）」做成了正交的两层。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cl_fix_addsub` 不自己写运算，而是同时调用 `cl_fix_add` 和 `cl_fix_sub` 再用 `np.where` 选？

**参考答案**：因为 `add` 是一个**逐元素**的布尔数组（每个元素独立决定该位置做加还是减），无法在 `mid_fmt` 里用一次算术调用同时表达「部分加部分减」。同时调用两个函数、再按 `add` 选择，可以复用 `cl_fix_add` / `cl_fix_sub` 已经验证过的三段式实现，避免重复逻辑；代价是计算了两个完整结果再丢弃一半（仿真模型追求正确与简洁，不在意这点冗余）。

**练习 2**：如果把 `r_fmt` 传成比 `mid_fmt` 还宽的格式，会发生什么？

**参考答案**：`resize` 会先用 `for_round` 算 `rounded_fmt`（向更宽对齐时通常等于 `mid_fmt`），再 `saturate`（向更宽收拢时不会触发饱和）。净效果是结果被扩展到更宽的 `r_fmt`，高位补符号位或零——数值不变，只是换了个更宽的容器。这是合法但通常无意义的用法。

---

### 4.2 convert：无损对齐到 mid_fmt（VHDL）

#### 4.2.1 概念说明

三段式的「运算」那一段，在 VHDL 里需要一个明确的「对齐」步骤：把两个操作数从各自格式搬到 `mid_fmt`，让它们的小数点对齐、位宽一致，然后才能用 `numeric_std` 的 `+` / `-` / `*`。

这个对齐动作由**内部函数 `convert`** 完成。它的契约很明确：**只搬位、不舍入、不饱和**——

- 支持 `rFmt.F >= aFmt.F`（小数位变多 → 在 LSB 侧补零，无损）。
- 支持 `rFmt` 的整数/符号位比 `aFmt` 少（高位变少 → 直接截断高位，等价于 `None_s` 饱和/回绕，但**不检测、不告警**）。
- **不支持** `rFmt.F < aFmt.F`（小数位变少需要舍入，必须走 `cl_fix_round`）。

换句话说，`convert` 是「无脑对齐」：能补就补、多出来就砍，绝不偷偷帮你舍入。

#### 4.2.2 核心流程

`convert` 的核心是把输入比特按 `rFmt.F - aFmt.F` 的偏移量写入结果向量：

- `offset = rFmt.F - aFmt.F`（非负，因为不支持减小 F）。
- 结果的 `result_v(offset ... high)` 这一段填入输入值（必要时符号扩展），`result_v(0 ... offset-1)` 这一段（即新增的低位小数位）保持为 `'0'`。

用位权语言描述：同一个比特，在 `aFmt` 里权重为 2^k，搬到 `rFmt` 后权重为 2^(k - offset)。因为 `offset = rFmt.F - aFmt.F`，恰好把小数点对齐到 `rFmt` 的位置。低位补零对应新增的小数精度（值为 0，不影响数值）。

#### 4.2.3 源码精读

先看 `resize_sensible`，它是 `convert` 截断/扩展时的「明智截断」工具：

[en_cl_fix_pkg.vhd:313-327](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L313-L327) —— `resize_sensible`：当目标更宽时调用标准库 `resize`（做符号扩展）；当目标更窄时**直接取低位 `a_c(n-1 downto 0)`**，而不像 `numeric_std.resize` 那样保留符号位。注释解释了原因——标准库的 `resize` 在截断时会保留符号位，这通常不是定点收缩想要的语义，所以这里改用「朴素截断」。

```vhdl
if n >= a'length then
    v := resize(a_c, n);              -- 符号扩展（变宽）
else
    v := a_c(n-1 downto 0);           -- 朴素截断（变窄），不保符号位
end if;
```

再看 `convert` 本体：

[en_cl_fix_pkg.vhd:329-351](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L329-L351) —— `convert`：`offset_c := rFmt.F - aFmt.F` 是对齐偏移；无符号输入用 `resize(unsigned(...))`，有符号输入用 `resize_sensible(signed(...))`；写入区间 `result_v(r_width_c-1 downto offset_c)`，低位 `offset_c` 位默认 `'0'`。注释明确写出两条契约：不支持 `rFmt.F < aFmt.F`；支持整数位减少但**不**做饱和（即实现了 `None_s` 模式的饱和）。

```vhdl
constant offset_c   : natural := rFmt.F - aFmt.F;     -- 对齐偏移（非负）
...
if aFmt.S = 0 then
    result_v(r_width_c-1 downto offset_c) :=
        std_logic_vector(resize(unsigned(a_c), r_width_c - offset_c));
else
    result_v(r_width_c-1 downto offset_c) :=
        std_logic_vector(resize_sensible(signed(a_c), r_width_c - offset_c));
end if;
```

`convert` 在算术函数里被反复用来「把操作数搬到 `mid_fmt`」。以 `cl_fix_add` 为例：

[en_cl_fix_pkg.vhd:1149-1172](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L1149-L1172) —— VHDL `cl_fix_add`：`a_v := convert(a, a_fmt, mid_fmt_c); b_v := convert(b, b_fmt, mid_fmt_c);` 把两操作数对齐到同一个 `mid_fmt_c`，然后 `mid_v := std_logic_vector(signed(a_v) + signed(b_v))`。注释提到一个工程细节：补码下有符号/无符号加减在位级完全相同，但 Vivado 的 DSP 切片对 `numeric_std.unsigned` 有长期 bug，所以这里**一律用 `signed`** 做加法。

> 为什么 VHDL 加法不像 Python 那样依赖运行时类型分发？因为补码加法的位级行为与符号无关——只要先把两个操作数符号扩展到同一宽度，`signed(a) + signed(b)` 的结果比特就是正确的全精度和（`mid_fmt` 已经为最坏情况预留了进位位）。`convert` 恰好负责这个「符号扩展到 `mid_fmt`」的对齐。

#### 4.2.4 代码实践（源码阅读型）

**目标**：用具体数字验证 `convert` 的对齐偏移逻辑。

**步骤**：

1. 设 `a_fmt = [0, 2, 1]`（无符号，LSB=0.5），`mid_fmt = [0, 3, 3]`（LSB=0.125）。则 `offset_c = 3 - 1 = 2`。
2. 设输入值 `a = 1.5`，在 `[0,2,1]` 里是比特 `11`（1.5/0.5=3=`11`）。
3. 推演 `convert`：`result_v` 宽 6 位，把 `11` 写入 `result_v(5 downto 2)`，`result_v(1 downto 0)` = `"00"`，得到 `"110000"`。
4. 把 `"110000"` 按 `[0,3,3]` 读回：整数 48 × 2^(-3) = 6.0... 等等，这与 1.5 不符？

**需要观察的现象**：上面第 4 步算出来不是 1.5，说明推演有误——请重新核对 `result_v` 的高位区间与符号扩展。提示：`resize(unsigned("11"), r_width - offset) = resize(unsigned("11"), 6-2=4)` 会把 `"11"` 零扩展成 4 位 `"0011"`，写入 `result_v(5 downto 2)` 得到 `"001100"`，读回 = 12 × 2^(-3) = 1.5。✓

**预期结果**：修正后数值一致。这个练习说明 `convert` 的对齐 = 「按 `offset` 把比特摆到正确位置 + 多余低位补零 + 多余高位按符号扩展」。**待本地验证**：你可以在 testbench 里用 `to_string(cl_fix_to_real(convert(...), mid_fmt))` 打印确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `convert` 声明 `offset_c : natural`（非负），从而拒绝 `rFmt.F < aFmt.F`？

**参考答案**：因为减小小数位必须舍入（丢弃的低位可能进位），而 `convert` 的设计目标是无损对齐、不做任何舍入判断。把 `offset_c` 设为 `natural` 让类型系统在编译期就拦截「想用 convert 减小 F」的误用；减小 F 的正确入口是 `cl_fix_round`（或 `cl_fix_resize`，它内部先 round）。

**练习 2**：`convert` 在整数位变少时直接截断高位，这等价于哪种饱和模式？

**参考答案**：等价于 `None_s`（回绕、不告警）。所以 `cl_fix_saturate` 实现饱和时正是先调 `convert`（完成回绕），再叠一层 `cl_fix_compare` 比较与钳位（实现真正的饱和）——见 u4-l2。

---

### 4.3 cl_fix_mult：signed×unsigned 局部算子与位宽收缩

#### 4.3.1 概念说明

乘法的三段式骨架与加法完全一致，但「运算」那一段在 VHDL 里有个语言层面的障碍：**`numeric_std` 只定义了 `signed*signed` 和 `unsigned*unsigned`，没有定义 `signed*unsigned`**。而定点的 signed×unsigned 乘法是完全合法且常见的（比如有符号系数乘无符号样本）。

en_cl_fix 的解决办法很优雅：在 `cl_fix_mult` 函数体内部**局部重载** `*` 算子，仅在该函数作用域内为 `signed*unsigned` 和 `unsigned*signed` 提供定义。这样既不污染全局命名空间，又能让四种符号组合走统一的 `*` 写法。

另一个乘法特有的点是**位宽收缩**：两个 N 位、M 位的数相乘，全精度积是 N+M 位（甚至更多），但 `mid_fmt` 由 `cl_fix_mult_fmt` 保守推导后可能比「朴素 N+M 位」更窄（例如 1 位有符号特例、或某一边 `I+F<=1` 时省 1 位，见 u3-l2）。所以乘法结果需要用 `resize_sensible` 收缩到 `mid_fmt` 的宽度。

#### 4.3.2 核心流程

```
mid_fmt = cl_fix_mult_fmt(a_fmt, b_fmt)        # F_mid = a.F + b.F；整数位按符号组合推导
mid_width = cl_fix_width(mid_fmt)

按 (a_fmt.S, b_fmt.S) 四种组合选择乘法：
  (0,0): unsigned * unsigned  → resize(...)           到 mid_width（只能变窄或等宽）
  (0,1): unsigned * signed    → resize_sensible(...)  （可能需要收缩+保符号）
  (1,0): signed * unsigned    → resize_sensible(...)  （用局部重载的 *）
  (1,1): signed * signed      → resize_sensible(...)

return cl_fix_resize(mid_v, mid_fmt, r_fmt, round, saturate)
```

注意四种分支里，只有 `(0,0)` 用标准库 `resize`，其余三种都用 `resize_sensible`。原因：`signed*signed`、`signed*unsigned` 的全宽度积在 `numeric_std` 里是「带符号位」的，朴素 `resize` 截断会保符号位导致错误，必须用 `resize_sensible` 的「砍低位」语义。

#### 4.3.3 源码精读

先看 Python 侧 `cl_fix_mult`，确认它和 `cl_fix_add` 同构（三段式骨架）：

[en_cl_fix.py:392-421](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L392-L421) —— Python `cl_fix_mult`：`mid_fmt = cl_fix_mult_fmt(a_fmt, b_fmt)`（第一段）；narrow/wide 分发后 `mid = a*b`（第二段，Python 的 `*` 天然支持混合类型，无语言障碍）；`cl_fix_resize(...)`（第三段）。Python 里 `a*b` 调用的是 NarrowFix 的 `__mul__`：

[narrow_fix.py:317-326](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L317-L326) —— `NarrowFix.mult`：直接 `self._data * b._data`（float 乘法，天然全精度），打上 `mid_fmt` 标签后 `resize`。

再看 VHDL `cl_fix_mult` 的精华——局部 `*` 重载：

[en_cl_fix_pkg.vhd:1216-1257](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L1216-L1257) —— VHDL `cl_fix_mult`。重点看声明区的两个局部函数：

```vhdl
-- VHDL 没有定义 signed*unsigned / unsigned*signed。
-- 仅在 cl_fix_mult 内部定义它们是安全的。
function "*"(x : signed; y : unsigned) return signed is
begin
    return x * ('0' & signed(y));     -- 给无符号数前补一个 '0' 当符号位
end function;

function "*"(x : unsigned; y : signed) return signed is
begin
    return y * x;                      -- 复用上一个重载
end function;
```

技巧解读：`signed * unsigned` 通过 `'0' & signed(y)` 把无符号数 `y` 前面补一个 `'0'`（即当成非负的有符号数），于是转化为合法的 `signed * signed`。第二个重载直接复用第一个（交换参数）。这两个定义只在 `cl_fix_mult` 函数体可见，不会泄漏到全局。

接着看四分支运算体（[L1246-L1254](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L1246-L1254)）：

```vhdl
if a_fmt.S = 0 and b_fmt.S = 0 then
    mid_v := std_logic_vector(resize(unsigned(a_c) * unsigned(b_c), mid_width_c));
elsif a_fmt.S = 0 and b_fmt.S = 1 then
    mid_v := std_logic_vector(resize_sensible(unsigned(a_c) * signed(b_c), mid_width_c));
elsif a_fmt.S = 1 and b_fmt.S = 0 then
    mid_v := std_logic_vector(resize_sensible(signed(a_c) * unsigned(b_c), mid_width_c));  -- 用局部 *
else
    mid_v := std_logic_vector(resize_sensible(signed(a_c) * signed(b_c), mid_width_c));
end if;
return cl_fix_resize(mid_v, mid_fmt_c, r_fmt_c, round, saturate);
```

只有 `(0,0)` 用 `resize`，其余三种用 `resize_sensible`——因为后三种结果都是带符号语义的，朴素截断会保符号位而出错。最后统一 `cl_fix_resize` 收口，与加法一致。

> 小贴士：`mid_fmt` 的宽度 `mid_width_c` 来自 `cl_fix_mult_fmt`（u3-l2）。例如两输入都满足 `I+F<=1` 时积会少 1 个整数位，`resize_sensible` 负责把 `numeric_std` 产出的「朴素 N+M 位」积收缩到这个更窄的 `mid_fmt`。

#### 4.3.4 代码实践（可运行）

**目标**：用一个**无符号 × 有符号**的乘法，亲手验证完整链路 `convert(对齐) → 乘 → resize`，并观察「缺省 r_fmt = mid_fmt（全精度）」「截断」「饱和」三种行为的差异。

**前置**：按 u1-l4 安装好 `numpy`，并能 `import en_cl_fix_pkg`。示例脚本建议放在 `bittrue/tests/python/` 目录下运行（与官方测试相同的路径约定），或在脚本顶部加 `sys.path` 指向 `bittrue/models/python`。

**操作步骤**（示例代码，保存为 `mult_chain_demo.py`）：

```python
# 示例代码：演示 cl_fix_mult 的 convert → 乘 → resize 链路
import sys
sys.path.append("../../models/python")          # 与官方测试一致的路径约定
from en_cl_fix_pkg import FixFormat, FixSaturate
from en_cl_fix_pkg import cl_fix_mult, cl_fix_mult_fmt, cl_fix_to_real

a_fmt = FixFormat(0, 2, 1)   # 无符号，范围 [0, 3.5]，LSB = 0.5
b_fmt = FixFormat(1, 1, 2)   # 有符号，范围 [-2, 1.75]，LSB = 0.25
# 注：FixFormat 也接受 FixFormat(True/False, I, F)，True==1、False==0

# ① 第一段：看全精度中间格式 mid_fmt
mid_fmt = cl_fix_mult_fmt(a_fmt, b_fmt)
print("mid_fmt =", mid_fmt)                       # 预期 (1, 3, 3)

# ② 全精度乘法（r_fmt 缺省 = mid_fmt）
full = cl_fix_mult(2.5, a_fmt, 1.25, b_fmt)
print("full precision 2.5 * 1.25 =", full)        # 预期 3.125

# ③ 截断：丢弃 2 个小数位（mid_fmt.F=3 → r_fmt.F=1），默认 Trunc_s
trunc = cl_fix_mult(2.5, a_fmt, 1.25, b_fmt, FixFormat(1, 3, 1))
print("truncated to [1,3,1]      =", trunc)       # 预期 3.0

# ④ 饱和：同时砍整数位（mid_fmt.I=3 → r_fmt.I=1），显式 Sat_s
sat = cl_fix_mult(2.5, a_fmt, 1.25, b_fmt,
                  FixFormat(1, 1, 3), FixSaturate.Sat_s)
print("saturated  to [1,1,3]      =", sat)        # 预期 1.875（3.125 超过 1.875 被钳到上限）
```

**需要观察的现象与预期结果**：

- `mid_fmt = (1, 3, 3)`：F_mid = 1+2 = 3；signed×unsigned 的整数位按 u3-l2 推导为 3；符号位取 max(0,1)=1。
- 全精度结果 `3.125`：2.5×1.25 的精确值，落进 `[1,3,3]` 的范围 [-8, 7.875] 内，无舍入无饱和。
- 截断结果 `3.0`：3.125 在 LSB=0.5 下做 `Trunc_s`（向 −∞ 取整）：3.125/0.5=6.25，floor=6，6×0.5=3.0。这正是 `resize` 内部「先 round」一步的产物。
- 饱和结果 `1.875`：目标 `[1,1,3]` 上限 = 2^1 − 2^(-3) = 1.875；3.125 > 1.875，被 `Sat_s` 钳到上限。这正是 `resize` 内部「后 saturate」一步的产物。

把 ③ 和 ④ 对比起来看，就完整还原了 `cl_fix_resize`（= round 后 saturate）的两步：③ 只触发 round、④ 只触发 saturate。**待本地验证**：在你的环境里运行脚本，确认四行输出与预期一致。

#### 4.3.5 小练习与答案

**练习 1**：把上面例子里的 `a_fmt` 改成 `FixFormat(1, 2, 1)`（有符号），即变成 signed×signed。`mid_fmt` 会变成什么？全精度结果还是 3.125 吗？

**参考答案**：`mid_fmt` 仍是 `(1, 3, 3)`。因为两输入都 `I+F=3>1` 且都是 2 位以上有符号，按 u3-l2 的 `cl_fix_mult_fmt`：两个有符号相乘时整数位 `a.I+b.I+1`（最大正积恰为 2 的幂，需多 1 位）= 2+1+1... 注意这里 a.I=2、b.I=1，得 `rmaxI = 2+1+1 = 4`？实际上需用脚本核对。**结论待本地验证**：运行 `cl_fix_mult_fmt(FixFormat(1,2,1), FixFormat(1,1,2))` 确认。全精度数值结果仍是 3.125（数值与符号无关，只取决于输入值），只是 `mid_fmt` 的整数位推导会因「双有符号」而 +1。

**练习 2**：为什么局部 `*` 重载里 `signed * unsigned` 要写成 `x * ('0' & signed(y))`，前补一个 `'0'`？

**参考答案**：`signed` 类型最高位是符号位。无符号数 `y` 本身没有符号位、其值恒非负。要把它当作「非负的有符号数」参与 `signed*signed` 乘法，必须在最高位前面补一个 `'0'` 作为符号位，这样 `signed(y)` 才表示与 `unsigned(y)` 相同的非负数值。直接 `signed(y)` 会把 `y` 的最高比特误读成符号位，导致负数。

---

### 4.4 cl_fix_shift：用 dummy_fmt 实现无损移位

#### 4.4.1 概念说明

`cl_fix_shift(a, a_fmt, shift, r_fmt, ...)` 实现「左移 `shift` 位」（等价于乘 2^shift；`shift<0` 为右移）。它有一个反直觉但极其精巧的设计：**移位本身不丢任何位**——既不截断高位、也不舍入低位。所有「有损」都推迟到最后对 `r_fmt` 的 `resize` 里。

这是怎么做到的？答案是用一个**哑格式 dummy_fmt** 把「移位」改写成「换一种格式读同一组比特」。

#### 4.4.2 核心流程

核心观察：同一个比特向量 B，按格式 `fmt1` 读出的值是 `B × 2^(-fmt1.F)`，按格式 `fmt2` 读出的值是 `B × 2^(-fmt2.F)`。若 `fmt2.F = fmt1.F + shift`，则「按 fmt2 读」=「按 fmt1 读」× 2^(-shift)。

反过来想：我们想要 `result = a × 2^shift`，并且结果用 `r_fmt` 读。如果能造一个 `dummy_fmt`，使得「B 按 dummy_fmt 读 = a」（即 B 是 a 在 dummy_fmt 下的表示），同时「B 按 r_fmt 读 = a × 2^shift」，那只要把 a 转换成 B（即 `cl_fix_resize(a, a_fmt, dummy_fmt)`），再声明结果格式是 `r_fmt`，就完成了移位。

联立两个条件可解出 `dummy_fmt`：

\[
\text{dummy\_fmt}.F = r\_fmt.F + shift
\]

再要求 dummy_fmt 与 r_fmt **总位宽相同**（同一组比特），且符号位相同，得：

\[
\text{dummy\_fmt} = (\,r\_fmt.S,\ \ r\_fmt.I - shift,\ \ r\_fmt.F + shift\,)
\]

于是 `cl_fix_shift` 的全部实现就是一行：`return cl_fix_resize(a, a_fmt, dummy_fmt, round, saturate)`。移位 = 一次以 dummy_fmt 为目标的 resize。

为什么说「移位本身不丢位」？因为把 a 从 `a_fmt` 转成 `dummy_fmt` 是一个**真转换**（dummy_fmt 是按需设计的合法容器），round/saturate 只在「a 的精度/范围超过 dummy_fmt」时才发生——而那正是「结果放不进 r_fmt」的情形，属于预期的输出格式化，不是移位本身的损失。

#### 4.4.3 源码精读

VHDL `cl_fix_shift` 极其简洁：

[en_cl_fix_pkg.vhd:1259-1274](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L1259-L1274) —— VHDL `cl_fix_shift`。整个函数体只有两行实质内容：

```vhdl
-- 用 resize 到一个哑格式来「隐式移位」，再把结果按 result_fmt 重解释
constant dummy_fmt_c : FixFormat_t :=
    (result_fmt.S, result_fmt.I - shift, result_fmt.F + shift);
begin
    -- 注意：执行的是无损移位（等价 *2.0**shift），然后再 resize 到输出格式。
    -- 初始移位不会截断任何位。shift 为左移；shift<0 为右移。
    return cl_fix_resize(a, a_fmt, dummy_fmt_c, round, saturate);
```

`dummy_fmt_c` 正是上面推导的 `(r.S, r.I-shift, r.F+shift)`。返回的比特向量 `cl_fix_resize(...)` 产出的结果是「按 dummy_fmt 摆好的 a」，调用者按 `result_fmt` 读它就得到 `a × 2^shift`。

Python 侧 `cl_fix_shift` 走的是**另一条等价路径**：它先算出真正的全精度中间格式 `mid_fmt = cl_fix_shift_fmt(a_fmt, min_shift, max_shift)`，在 NarrowFix 上直接做 `a << shift`（即 `self._data * 2.0**shift`，浮点乘法天然无损），最后 resize：

[en_cl_fix.py:424-452](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L424-L452) —— Python `cl_fix_shift`。关键行：

```python
mid_fmt = cl_fix_shift_fmt(a_fmt, np.min(shift), np.max(shift))
...
mid = a << shift                      # NarrowFix.__lshift__ → shift() → data * 2.0**shift
return cl_fix_resize(mid._data, mid_fmt, r_fmt, rnd, sat)
```

[narrow_fix.py:328-341](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L328-L341) —— `NarrowFix.shift`：`self._data * 2.0 ** shift`，浮点域里左移 = 乘 2^shift，无损。

> 为什么 Python 和 VHDL 用了两套不同写法（Python 直接乘 2^shift；VHDL 用 dummy_fmt）？因为 Python 有任意精度浮点/整数，直接乘最自然；而 VHDL 是面向综合的，不能在电路上「乘 2^shift」（那会综合出乘法器），dummy_fmt 技巧把移位变成「重新布线 + resize」，综合后就是纯连线和少量 round/saturate 逻辑，零乘法器。两者结果 bit-true 一致（经 cosim 对拍，见 u8）。

#### 4.4.4 代码实践（源码阅读 + 手算）

**目标**：用一个左移 2 位的例子，手算 dummy_fmt，验证「移位 = 换格式读比特」。

**步骤**：

1. 设 `a_fmt = [1, 3, 3]`（有符号，值 a），`shift = 2`，`result_fmt = [1, 3, 3]`。
2. 按 `dummy_fmt = (r.S, r.I-shift, r.F+shift)` 算出 `dummy_fmt = (1, 3-2, 3+2) = (1, 1, 5)`。注意 dummy_fmt 宽度 = 1+1+5 = 7，与 result_fmt 宽度 1+3+3 = 7 相同 ✓（同一组比特）。
3. `cl_fix_resize(a, [1,3,3], [1,1,5])`：F 从 3 增到 5（补 2 个 LSB 零，无损），整数位从 3 减到 1（截 2 个 MSB，可能饱和/回绕）。设 a=1.5 未溢出，则结果比特 B 在 `[1,1,5]` 下表示 1.5。
4. 调用者把 B 按 `result_fmt=[1,3,3]` 读：B 整数值 × 2^(-3)。而 B 在 `[1,1,5]` 下 = B_int × 2^(-5) = 1.5，故 B_int × 2^(-3) = 1.5 × 2^2 = 6.0。即 result = 1.5 × 4 = a << 2 ✓。

**需要观察的现象**：dummy_fmt 把「左移 2 位」编码成「整数位减 2、小数位加 2」；同一组比特换格式读，数值正好放大 4 倍。

**预期结果**：result = a × 2^shift。**待本地验证**：用 Python `cl_fix_shift(1.5, FixFormat(1,3,3), 2, FixFormat(1,3,3))` 确认结果为 6.0；再试 `shift=-1` 确认右移（1.5 → 0.75）。

#### 4.4.5 小练习与答案

**练习 1**：`cl_fix_shift` 的 `r_fmt` 在 Python 签名里**没有默认值**（必须显式传），而其他算术函数的 `r_fmt` 都有默认值（None / NullFixFormat_c）。为什么？

**参考答案**：因为移位的「自然全精度结果格式」依赖于具体的 `shift` 值（不同的 shift 给出不同的 `mid_fmt`），而且移位常用于「把数据搬进一个设计者指定的物理格式」（如把小数对齐到某个 DSP 输入位宽）。强制显式传 `r_fmt` 能避免「缺省值在不同 shift 下含义不一致」的歧义。Python 侧 `cl_fix_shift_fmt` 仍可用来算这个 shift 对应的全精度格式，但调用者必须主动传入。

**练习 2**：dummy_fmt 技巧要求 `dummy_fmt` 是合法格式（`I+F>=0`）。若 `shift` 很大、`result_fmt.I - shift` 变得很负，会怎样？

**参考答案**：只要 `dummy_fmt.S + dummy_fmt.I + dummy_fmt.F = result_fmt 总宽` 且满足 `I+F>=0`（即 `(r.I-shift)+(r.F+shift) = r.I+r.F >= 0`，恒成立），格式就合法。dummy_fmt 的位宽恒等于 result_fmt 位宽，所以「移位多少」都不会让 dummy_fmt 变宽或非法；变化的只是整数位与小数位的分配比例。

---

## 5. 综合实践

**任务**：实现一个迷你的「有符号系数 × 无符号样本」定点乘法器观察台，完整跟踪 `convert → 乘 → resize` 三段，并对比 NarrowFix 路径与 numpy 独立计算的参考值。

**要求**：

1. 选定 `a_fmt = FixFormat(1, 2, 1)`（有符号系数，范围 [-4, 3.5]）、`b_fmt = FixFormat(0, 1, 2)`（无符号样本，范围 [0, 1.75]）。
2. 用 `cl_fix_mult_fmt` 打印 `mid_fmt`，并**手工**用 u3-l2 的规则推导一遍，核对一致（重点：signed×unsigned 的整数位 = `max(a.I+b.I, a.I + neg_fmt(b).I)`）。
3. 取一组系数 `a ∈ {-2.5, 1.5, 3.0}` 与样本 `b ∈ {0.25, 1.0, 1.75}`，用 `cl_fix_mult`（不传 `r_fmt`，全精度）算出 9 个结果。
4. 用 numpy 直接算 `a*b` 作为独立参考，逐个对比，确认完全相等。
5. 再选一个**截断目标** `r_fmt = FixFormat(1, 3, 1)` 和一个**饱和目标** `r_fmt = FixFormat(1, 1, 3)`（配 `FixSaturate.Sat_s`），分别重算，观察哪些值发生了变化、为什么。

**预期结论**：

- 第 2 步：`mid_fmt` 的 F = 1+2 = 3；符号位 = 1；整数位由 `cl_fix_mult_fmt` 给出（**待本地验证**具体值，重点理解它不简单等于 `a.I+b.I`）。
- 第 4 步：全精度下 NarrowFix 与 numpy 逐值相等（因为全精度无损）。
- 第 5 步：截断目标下，原本带 2^-2、2^-3 小数的结果被向 −∞ 取整；饱和目标下，超过 `[1,1,3]` 上限 1.875 的结果（如 1.5×1.75=2.625）被钳到 1.875。这直观展示了 `resize` 的两步（round / saturate）如何独立作用。

**交付物**：一个可运行脚本及其输出，附一段文字说明你在哪一步看到了「对齐」、哪一步看到了「运算」、哪一步看到了「resize」。

---

## 6. 本讲小结

- **三段式骨架**：所有算术函数（add/sub/addsub/mult/neg/abs/shift）都遵循 `mid_fmt → 运算 → resize`，把「算得对」与「放得下」彻底解耦。
- **缺省即全精度**：`r_fmt` 缺省（Python `None` / VHDL `NullFixFormat_c`）时等于 `mid_fmt`，「要精度」是零成本默认，「有损」必须显式 opt-in。
- **convert 负责对齐**：VHDL 的 `convert` 把操作数按 `rFmt.F - aFmt.F` 的偏移搬位、补零、符号扩展，做到「只搬位、不舍入、不饱和」；它就是 `cl_fix_saturate` 在 `None_s` 模式下的实现。
- **resize_sensible 的「明智截断」**：收缩位宽时直接取低位而不保符号位，区别于 `numeric_std.resize`，是带符号结果收缩的正确语义。
- **mult 的局部 `*` 重载**：在 `cl_fix_mult` 函数体内局部定义 `signed*unsigned`，用 `'0' & signed(y)` 把无符号数当非负有符号数，既解决语言缺失又不污染全局。
- **shift 的 dummy_fmt 技巧**：移位 = `(r.S, r.I-shift, r.F+shift)` 的一次 resize，把「移位」变成「换格式读同一组比特」，综合后是纯连线、零乘法器，且移位本身不丢位。

## 7. 下一步学习建议

- **深入 Narrow/Wide 分发**：本讲的算术函数在 narrow/wide 之间的抬升与互转逻辑（`cl_fix_is_wide`、`from_narrowfix`）只在注释里带过，完整机制见 u6-l3。
- **验证链路如何对拍**：本讲反复提到「Python 与 VHDL bit-true 一致」，这个断言是由 cosim 流程保证的——见 u8-l1（cosim 总览）和 u8-l3（testbench 文件 I/O）。
- **可综合 RTL 实现**：本讲讲的是「函数级」算术。若想知道这些算术在硬件上如何带流水线地实现，见 u7-l1（round/saturate/resize 实体）和 u7-l2（推荐流水线与 RegisterMode）。
- **建议继续阅读的源码**：[en_cl_fix.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py) 的 `cl_fix_abs`（L259）/ `cl_fix_neg`（L286）是一元算术，可用来巩固三段式；[en_cl_fix_pkg.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd) 的 `cl_fix_compare`（L1276）展示了 `union` 如何用于「把两数对齐到同一格式再比较」，与 `convert` 思路一脉相承。
