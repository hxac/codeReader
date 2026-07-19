# cl_fix_compare 比较与 mean_angle 模运算

## 1. 本讲目标

本讲是专家层（Unit 7）的第二篇，剖析 `en_cl_fix` 里两个**最精巧、也最少被注意**的算法。前面讲义里的加减乘、resize 都是「在一条直线（实数轴）上做运算」，而这两个函数处理的是**比较**与**模运算（圆周）**——它们都需要在位级别做一点「小手脚」才能得到正确答案。

读者学完后应该能够：

1. 解释 `cl_fix_compare` 为什么**先对齐到 `FullFmt`、再翻转最高符号位、最后用 `unsigned` 比较**，并证明「翻转 MSB 后的无符号比较」与「有符号比较」严格等价。
2. 手算一个「不翻转就会比错」的反例（如 `-1` 与 `+1`），亲眼看到 offset binary（偏移二进制）如何保序。
3. 说清 `cl_fix_mean`（普通算术均值）在角度/模运算下为什么会给出「绕远路」的错误均值，以及 `cl_fix_mean_angle` 如何用「检测跨象限 + 翻转结果 MSB」来修正。
4. 区分 `precise=false`（只看两操作数的高 2 位象限）与 `precise=true`（额外看求和结果的位）两种修正策略的代价与精度差异。
5. 了解 `cl_fix_compare` 与 `cl_fix_mean_angle` 的**跨语言覆盖现状**：前者是 VHDL 独有；后者 VHDL/MATLAB 有完整实现、Python 仅为 `NotImplementedError` 桩函数，且二者在 testbench 中均无专门覆盖——因此对数值结果应保持「读码 + 本地实测」的严谨态度。

本讲不重复 [u3-l4](u3-l4-helpers-compare-range.md) 对 `cl_fix_compare` 在 `cl_fix_in_range` 中的复用叙述，也不重复 [u4-l3](u4-l3-shift-mean-abs-neg.md) 对 `cl_fix_mean` 三段式实现的拆解；本讲只聚焦这两个函数**自身的算法技巧**。

## 2. 前置知识

本讲默认读者已经掌握以下概念（若生疏请先回看对应讲义）：

- **定点格式 `[S, I, F]`** 与位宽公式 `W = S + I + F`（[u1-l2](u1-l2-fixformat-type.md)）。
- **二进制补码（two's complement）**：W 位有符号数的取值范围为 \([-2^{W-1},\ 2^{W-1}-1]\)，最高位（MSB）是符号位、权重为 \(-2^{W-1}\)，其余位权重为正。
- **`cl_fix_resize` 的无损扩展语义**：用 `Trunc_s` + `None_s` 向更宽格式扩展等价于符号扩展/零扩展，不丢精度（[u3-l2](u3-l2-resize-rounding.md)、[u3-l3](u3-l3-resaturation.md)）。
- **`ForAdd` 格式增长规则**：`(S_a∨S_b,\ max(I_a,I_b)+1,\ max(F_a,F_b))`，那个 `+1` 吸收两个大正数相加的进位（[u4-l1](u4-l1-add-sub.md)）。
- **`cl_fix_mean = cl_fix_add → cl_fix_shift(-1)`** 的三段式实现（[u4-l3](u4-l3-shift-mean-abs-neg.md)）。

还需要两个本讲要用到的小术语：

- **offset binary（偏移二进制 / excess-K 码）**：把补码的最高位翻转一下得到的编码。它把「带符号的补码」整体平移成「无符号的正数」，且**保持大小顺序**——这是 `cl_fix_compare` 的核心技巧。
- **模运算 / 角度**：把定点数当作圆周上的点（如角度 \(-\pi\dots+\pi\) 循环）。在圆周上，`+max` 与 `-max` 是**相邻**的（越过边界就回绕），这与实数轴上「越正越大」的直觉不同，是 `cl_fix_mean_angle` 要解决的问题。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `vhdl/src/en_cl_fix_pkg.vhd` | VHDL 实现包。本讲聚焦 `cl_fix_compare`（声明 + 实现）、`cl_fix_mean_angle`（声明 + 实现），并对照 `cl_fix_mean`。 |
| `matlab/src/cl_fix_mean_angle.m` | MATLAB 端的完整 `cl_fix_mean_angle` 实现，含象限注释，是理解算法意图的最佳参照。 |
| `python/src/en_cl_fix_pkg/en_cl_fix_pkg.py` | Python 端。`cl_fix_mean_angle` 在此**仅为 `raise NotImplementedError()` 桩函数**；`cl_fix_compare` 不存在。 |
| `vhdl/tb/en_cl_fix_pkg_tb.vhd` | testbench。`cl_fix_compare` 在此有完整断言测试；`cl_fix_mean_angle` **没有**任何测试覆盖。 |

读者可以打开这些文件，按本讲给出的行号定位。所有永久链接基于当前 HEAD `7f7aa80f79caf9eefcbb9946feabc882b98bb4aa`。

## 4. 核心概念与源码讲解

### 4.1 cl_fix_compare：对齐 + 翻转符号位转无符号偏移比较

#### 4.1.1 概念说明

`cl_fix_compare` 要解决的问题是：**比较两个格式可能不同的定点数的大小**。比如 `a` 是 `(true,4,2)`、`b` 是 `(false,2,1)`，二者位宽不同、符号性不同、小数点位置也不同，怎么比？

朴素的难点有两个：

1. **格式不同**：得先把它们对齐到同一个格式才能逐位比。这一步用 `cl_fix_resize` 的无损扩展就能做到（见 [u3-l3](u3-l3-resize-saturation.md)）。
2. **有符号补码的位串顺序「是错的」**：补码把负数的 MSB 设成 `1`、正数设成 `0`，于是**把补码位串当成无符号整数看时，所有负数都比所有正数大**（`1111`=15 > `0001`=1，但作为补码是 `-1 < +1`）。如果直接用 `unsigned` 比较，符号性就错了。

第 2 点才是这个函数真正精巧的地方。解决办法是一个经典编码变换：**把补码的 MSB 翻转一下，得到 offset binary（偏移二进制）**。

为什么翻转 MSB 就行？因为补码里只有 MSB 的权重是负的（\(-2^{W-1}\)），其余位都是正的。把 MSB 取反，等价于把它的权重从 \(-2^{W-1}\) 换成 \(+2^{W-1}\)（在位串上 `1↔0`，正好差 \(2^{W-1}\)）。于是整个数的「无符号读数」恰好等于「补码值 \(+\ 2^{W-1}\)」：

\[
\text{unsigned}(\text{flipMSB}(x)) \;=\; x + 2^{W-1}
\]

而「加一个常数」是**严格单调递增**的映射，所以**补码值的大小顺序被完整保留**：

\[
x < y \;\iff\; x + 2^{W-1} < y + 2^{W-1}
\]

也就是说：**翻转 MSB 后，用 `unsigned` 比较就等价于有符号比较**。而且这个映射把范围 \([-2^{W-1},\ 2^{W-1}-1]\) 平移成了 \([0,\ 2^{W}-1]\)，恰好不溢出（最大值 \(2^{W-1}-1 + 2^{W-1} = 2^{W}-1\) 正好是 W 位无符号的上界）。

> 💡 一句话：**offset binary 是「把补码平移成全正数」的编码，保序且不溢出，于是无符号比较器可以直接拿来比有符号数。** 在硬件里，「无符号比较器」和「有符号比较器」其实是同一套减法电路，但用 offset binary 后就连「语义」都统一成了无符号，代码更简洁。

#### 4.1.2 核心流程

`cl_fix_compare` 的三步流程：

```
function cl_fix_compare(comparison, a, aFmt, b, bFmt) return boolean is
    1. 构造统一对齐格式 FullFmt := (aFmt.S or bFmt.S,
                                    max(aFmt.I, bFmt.I),
                                    max(aFmt.F, bFmt.F));
    2. AFull := resize(a, aFmt, FullFmt);   -- 无损扩展
       BFull := resize(b, bFmt, FullFmt);
    3. if FullFmt.Signed then               -- 有符号才需要翻转
           AFull(MSB) := not AFull(MSB);    -- 补码 -> offset binary
           BFull(MSB) := not BFull(MSB);
       end if;
    4. 用 unsigned 在 6 种运算符上比较 AFull、BFull
end;
```

四个要点：

- **`FullFmt` 取两数的「符号之或」与「整数位/小数位的较大值」**。这样 `resize` 到 `FullFmt` 时两数都只可能加位、不可能丢位（无损扩展），比较的是真实数值而非被截断后的值。
- **翻转 MSB 只在 `FullFmt.Signed` 为真时做**。若两数都无符号，补码问题不存在，直接 `unsigned` 比较就是对的。
- **6 种运算符**：`"a=b"`、`"a<b"`、`"a>b"`、`"a<=b"`、`"a>=b"`、`"a!=b"`，用一个字符串参数选择；非法字符串触发 `###ERROR###`（沿用 [u2-l2](u2-l2-vhdl-sim-testbench.md) 讲过的断言式校验约定）。
- 因为 offset binary 是保序双射，**6 种运算符在翻转后与翻转前的有符号语义完全一致**，不需要为每种运算符分别特判。

#### 4.1.3 源码精读

**函数声明与文档**：

- [vhdl/src/en_cl_fix_pkg.vhd:L983-L994](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L983-L994) — 声明 `cl_fix_compare(comparison, a, aFmt, b, bFmt) return boolean`，文档列出 6 种合法 `comparison` 字符串。注意它**只接受格式不同的两个数、返回布尔**，本身不涉及舍入/饱和（比较是无损的）。

**实现**（全函数仅 30 行，非常干净）：

- [vhdl/src/en_cl_fix_pkg.vhd:L2624](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2624) — 构造 `FullFmt_c`：`(aFmt.Signed or bFmt.Signed, max(aFmt.IntBits, bFmt.IntBits), max(aFmt.FracBits, bFmt.FracBits))`。这就是 4.1.2 第 1 步。
- [vhdl/src/en_cl_fix_pkg.vhd:L2630-L2631](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2630-L2631) — 两数各自 `cl_fix_resize` 到 `FullFmt_c`（默认 `Trunc_s`/`Warn_s`，但因 `FullFmt` 必然 ≥ 输入，不会真丢位）。
- [vhdl/src/en_cl_fix_pkg.vhd:L2633-L2636](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2633-L2636) — **核心两行**：仅当 `FullFmt_c.Signed` 时，把 `AFull_v` 和 `BFull_v` 的最高位取反（补码→offset binary）。
- [vhdl/src/en_cl_fix_pkg.vhd:L2638-L2646](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2638-L2646) — 6 个 `elsif` 分支用 `unsigned(...)` 做 `=`/`<`/`>`/`<=`/`>=`/`/=` 比较；非法 `comparison` 走 `report "###ERROR### ..." severity error`。

**已有测试**（`cl_fix_compare` 是本讲两个函数里唯一被 testbench 覆盖的）：

- [vhdl/tb/en_cl_fix_pkg_tb.vhd:L643-L739](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L643-L739) — 用 `CheckBoolean` 覆盖 6 种运算符 × 多种符号组合（有符号/无符号混比），例如 `a=-1.25 (true,4,2)` 与 `b=-1.0 (true,2,1)` 比 `a<b` 期望 `true`。

> ⚠️ **跨语言现状**：`cl_fix_compare` 是 **VHDL 独有**的函数。Python 与 MATLAB 都没有它——因为它们把定点数存在 `double` 实数域里（见 [u2-l1](u2-l1-python-package-tests.md)、[u2-l3](u2-l3-matlab-model.md)），直接用语言的 `<`/`>`/`==` 比浮点即可，不需要「位串对齐 + 翻转 MSB」这套技巧。这也是本讲把它作为「位级技巧」典型来讲的原因。

#### 4.1.4 代码实践

**实践目标**：亲手验证「不翻转 MSB 就会比错」，并证明翻转后结果正确。

**操作步骤（纸笔推导型实践）**：

1. 取 `aFmt = bFmt = (true, 2, 0)`（W=3，范围 `-4 … +3`），令 `a = -1`、`b = +1`。
2. 写出补码位串：`a = -1 → 111`，`b = +1 → 001`。
3. **不翻转**，直接当 `unsigned` 比：`111 (=7)` 与 `001 (=1)`，得 `7 > 1`，即「`a > b`」。但补码下 `-1 < +1`，**结论错误**。
4. **翻转 MSB** 后：`a → 011 (=3)`，`b → 101 (=5)`，`unsigned` 比 `3 < 5`，即「`a < b`」。**结论正确**。
5. 对照实数轴验算偏移量：`a + 2^{W-1} = -1 + 4 = 3 ✓`，`b + 4 = 1 + 4 = 5 ✓`，与翻转后的无符号读数完全吻合。

**需要观察的现象**：翻转前，所有负数（MSB=1）的无符号读数都被「抬高」到了正数之上，导致顺序颠倒；翻转后，`-4 → 0`、`-1 → 3`、`0 → 4`、`+3 → 7`，**随补码值单调递增**。

**预期结果**：你能用一句话解释「为什么 `cl_fix_compare` 必须翻转 MSB」——因为补码只有 MSB 权重为负，翻转它等价于全体 `+2^{W-1}`，把带符号数保序地映射成无符号数。

**（可选）跑真实测试**：`待本地验证`（需 Modelsim）。运行 [vhdl/tb/en_cl_fix_pkg_tb.vhd 的 `cl_fix_compare` 段](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L643-L739)，确认 6 种运算符的 `CheckBoolean` 全部通过、transcript 无 `###ERROR###`。

#### 4.1.5 小练习与答案

**练习 1**：若 `FullFmt_c.Signed` 为 `false`（两数都无符号），源码会跳过翻转（[L2633](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2633)）。为什么无符号时不需要翻转？

> **答案**：无符号数没有「权重为负」的位，所有位权重都是正的，位串的 `unsigned` 读数本身就等于真实值且单调递增，直接比即可。翻转 MSB 是专门为了抵消补码 MSB 的负权重，无符号数没有这个问题。

**练习 2**：`cl_fix_compare("a=b", ...)` 在两数格式不同（如 `(true,4,2)` 与 `(false,2,1)`）时，相等的判定会不会因为格式不同而误判？结合 `FullFmt` 说明。

> **答案**：不会。`FullFmt` 取两数符号之或与整数/小数位的较大值，`resize` 到 `FullFmt` 是无损扩展（只加位不丢位），所以两数的真实数值被原样对齐到同一格式。若两数真实值相等，扩展后位串逐位相同，翻转 MSB 后仍相同，`unsigned =` 返回 `true`；若不等则必在某位上分叉。testbench [L677-L680](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L677-L680) 正是用 `(true,4,2)` 的 `1.5` 与 `(false,2,1)` 的 `1.5` 验证 `a=b` 为 `true`。

---

### 4.2 cl_fix_mean_angle：模运算均值与 differentSigns 修正

#### 4.2.1 概念说明

`cl_fix_mean`（[u4-l3](u4-l3-shift-mean-abs-neg.md)）算的是普通算术均值 `(a+b)/2`。当 `a`、`b` 是**实数轴**上的点时它永远正确。但当它们被解释为**圆周上的点**（角度、相位等「模运算」量）时，算术均值会给出「绕远路」的错误答案。

经典反例：在范围 `-4 … +3`（W=4 位补码，圆周长 8）的角度系里，取 `a = +3`、`b = -4`。在圆周上，`+3` 与 `-4` 只隔 1 步（越过 `+max/-max` 边界），是**相邻**的；它俩的「角均值」应当落在边界附近（约 `+3.5 ≡ -4`）。但算术均值 `(3 + (-4))/2 = -0.5` 却跑到了圆周另一侧的 0 附近——这是「走了远路」的均值。

直观规律：**当 `a`、`b` 分处圆周两端、且其间隔超过半圈时，算术均值会取到远路中点**。修正办法是：把结果整体「转半圈」（在位级别等价于翻转 MSB，即加半个量程），让均值落到近路中点上。

`cl_fix_mean_angle` 就是干这件事的：它先像 `cl_fix_mean` 一样在 `ForAdd` 中间格式上求精确和，**但用一个「跨象限检测」判断是否需要把结果转半圈**，需要时翻转和的 MSB，最后再 resize + 右移一位除以 2。

「象限」怎么定？用每个数**最高 2 位**（MSB 与次高位）把圆周划成 4 块。MATLAB 源码顶部有一张极简的象限图：

```
quadrants:  1 0
            2 3
```

对补码而言，`(MSB, 次高位)` 决定该数落在圆周的哪一段：

| `(MSB, 次高位)` | 含义 | 位置 |
| --- | --- | --- |
| `(0, 1)` | 大正数 | 靠近 `+max`（上边界） |
| `(0, 0)` | 小正数 | 靠近 `0+` |
| `(1, 1)` | 小负数 | 靠近 `0-` |
| `(1, 0)` | 大负数 | 靠近 `-max`（下边界） |

关键观察：**`MSB ≠ 次高位` 当且仅当该数处于「外象限」**（`(0,1)` 大正 / `(1,0)` 大负，都贴近 `±max` 边界）；`MSB == 次高位` 则是「内象限」（贴近 0）。于是「两个数都处外象限且符号相反」就等价于「一个贴 `+max`、一个贴 `-max`，二者隔着边界相邻」——正是需要转半圈的情形。

#### 4.2.2 核心流程

`cl_fix_mean_angle` 的骨架（与 `cl_fix_mean` 同源，多了「跨象限检测 + 翻转」）：

```
function cl_fix_mean_angle(a, a_fmt, b, b_fmt, precise, result_fmt, round, saturate) is
    TempFmt_c := (a_fmt.S or b_fmt.S,
                  max(a_fmt.I, b_fmt.I) + 1,     -- 与 ForAdd 一致，留进位位
                  max(a_fmt.F, b_fmt.F));
begin
    -- 0. 约束检查（见 4.2.3）
    -- 1. 检测「符号相反」
    differentSigns := a(MSB) /= b(MSB);
    -- 2. 非精确修正：两数都处外象限且符号相反 -> 预处理（见 4.2.3 / 4.3）
    -- 3. 求精确和（与 cl_fix_mean 同款）
    temp := cl_fix_add(a, a_fmt, b, b_fmt, TempFmt_c, Trunc_s, None_s);
    -- 4. 精确修正（仅 precise=true）：再看和的高位决定是否翻转（见 4.3）
    -- 5. 除以 2 + 舍入/饱和（复用 resize）
    return cl_fix_resize(temp, TempFmt_c, result_fmt, round, saturate);
end;
```

三个要点：

- **`TempFmt_c` 与 `cl_fix_mean` 完全相同**（[u4-l3](u4-l3-shift-mean-abs-neg.md) 已讲），都是 `ForAdd` 带 `+1` 整数位，确保求和无损。
- **`differentSigns` 是所有修正的「总闸」**：只有两数符号相反（一个 MSB=0、一个 MSB=1）才可能需要转半圈；同号时算术均值天然正确，直接走 `cl_fix_mean` 路径。
- **两种修正精度**由 `precise` 开关选择（详见 4.3）：`precise=false` 只看两操作数的高 2 位象限（便宜）；`precise=true` 额外看求和结果的位（更准）。

#### 4.2.3 源码精读

**函数声明与文档**：

- [vhdl/src/en_cl_fix_pkg.vhd:L917-L941](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L917-L941) — 声明 `cl_fix_mean_angle(a, a_fmt, b, b_fmt, precise, result_fmt, round, saturate)`，文档明确「输入被当作角度或其它具有模性质的数」。注意它比 `cl_fix_mean` 多一个 `precise : boolean` 参数。
- 对照 [vhdl/src/en_cl_fix_pkg.vhd:L895-L915](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L895-L915)（`cl_fix_mean` 声明）——二者除 `precise` 外签名一致。

**实现**：

- [vhdl/src/en_cl_fix_pkg.vhd:L2521-L2526](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2521-L2526) — `TempFmt_c`，与 `cl_fix_mean` 逐字相同。
- [vhdl/src/en_cl_fix_pkg.vhd:L2534-L2539](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2534-L2539) — **两条硬约束**（`severity failure`）：
  - `a_fmt.Signed = b_fmt.Signed` 且 `a_fmt.IntBits = b_fmt.IntBits`：两数的符号性与整数位必须一致（只允许小数位不同）。这是为了让「同象限比较」有意义。
  - `cl_fix_width(a_fmt) >= 2` 且 `cl_fix_width(b_fmt) >= 2`：每个数至少 2 位，否则连「象限」都判不出来。
- [vhdl/src/en_cl_fix_pkg.vhd:L2543](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2543) — `differentSigns_v := a_v'high /= b_v'high`，即两数 MSB 不同 → 符号相反。这是修正总闸。
- [vhdl/src/en_cl_fix_pkg.vhd:L2544-L2547](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2544-L2547) — **非精确（象限级）预处理**：条件 `differentSigns AND (a 在外象限) AND (b 在外象限)`，其中「在外象限」就是 4.2.1 说的 `MSB ≠ 次高位`（`a_v'high /= a_v'high-1`）。命中时把工作副本 `a_v` 的 MSB 取反。
- [vhdl/src/en_cl_fix_pkg.vhd:L2548](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2548) — 求和：`temp_v := cl_fix_add(a, a_fmt, b, b_fmt, TempFmt_c, Trunc_s, None_s)`。
- [vhdl/src/en_cl_fix_pkg.vhd:L2549-L2552](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2549-L2552) — **精确修正分支**（仅 `precise=true`）：命中时翻转「和」`temp_v` 的 MSB（详见 4.3）。
- [vhdl/src/en_cl_fix_pkg.vhd:L2553](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2553) — `result_v := cl_fix_resize(temp_v, TempFmt_c, result_fmt, round, saturate)`。注意：和 `cl_fix_mean` 不同，这里**没有单独的 `cl_fix_shift(-1)`**——`mean_angle` 把「除以 2」隐含交给了 `result_fmt`（若结果小数位比 `TempFmt_c` 多 1 位，resize 的截断/舍入就实现了 `/2`）。这是它与 `cl_fix_mean` 实现上的一个细节差异。

**MATLAB 对照**（理解算法意图的最佳参照）：

- [matlab/src/cl_fix_mean_angle.m:L56-L57](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_mean_angle.m#L56-L57) — 象限图注释。
- [matlab/src/cl_fix_mean_angle.m:L60-L66](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_mean_angle.m#L60-L66) — 用 `cl_fix_get_msb` 取出两数的高 2 位，算 `different_signs` 与 `toggle`，再把翻转写回 `a`。
- [matlab/src/cl_fix_mean_angle.m:L69-L70](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_mean_angle.m#L69-L70) — 求和（与 VHDL 同款 `TempFmt`）。

> ⚠️ **跨语言与测试现状（重要）**：
> - `cl_fix_mean_angle` 在 **testbench 中没有任何测试覆盖**（搜索 `mean_angle` 仅命中源码、不命中 `en_cl_fix_pkg_tb.vhd`）。
> - **Python 端是桩函数**：[python/src/en_cl_fix_pkg/en_cl_fix_pkg.py:L386-L391](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L386-L391) 直接 `raise NotImplementedError()`，**不能在 Python 里运行**。
> - **VHDL 与 MATLAB 两个完整实现在「非精确预处理如何喂给加法」上结构不同**：VHDL 把 MSB 翻转作用于工作副本 `a_v`，但随后的 `cl_fix_add`（[L2548](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2548)）使用的是**原始参数 `a`**；而 MATLAB（[L66](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_mean_angle.m#L66)-[L70](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_mean_angle.m#L70)）把翻转写回 `a` 后再喂给加法。两者的非精确条件判定式也不同（VHDL 看「`a`、`b` 是否都在外象限」，MATLAB 看「`a`、`b` 次高位是否不同」）。
>
> 因此：**不要假定 `mean_angle` 在三种语言间逐位一致**。本讲对它的数值结果一律标 `待本地验证`，重点放在「读懂结构与意图」。

#### 4.2.4 代码实践

**实践目标**：在「外象限、符号相反」的典型跨边界场景下，手算象限判定，预测修正是否触发。

**操作步骤（纸笔推导型实践）**：

1. 取 `a_fmt = b_fmt = (true, 2, 1)`（W=4，范围 `-4 … +3.9375`，步长 0.5）。令 `a = +3.5`、`b = -3.5`（一个贴 `+max`、一个贴 `-max`，隔着边界相邻）。
2. 写出位串：`a = +3.5 → raw 7 → 0111`；`b = -3.5 → raw -7 → 1001`。
3. 判象限：`a` 的 `(MSB,次高) = (0,1)` → 外象限（大正）；`b` 的 `(1,0)` → 外象限（大负）。
4. 判 `differentSigns`：`0 ≠ 1` → `true`。
5. 套 VHDL 非精确条件（[L2544-L2545](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2544-L2545)）：`differentSigns AND a外象限 AND b外象限` → `true`。预测：非精确分支命中。
6. 算术均值对照：`cl_fix_mean` 会得 `(3.5 + (-3.5))/2 = 0`（远路中点）；角均值的期望应在 `±max` 边界附近。

**需要观察的现象**：算术均值 `0` 与两输入在圆周上相距甚远（一个在 `+3.5`、一个在 `-3.5`，算术均值却跳到 0），直观说明「为什么需要 `mean_angle`」。

**预期结果**：你能指出「`differentSigns` + 双外象限」是触发修正的信号。至于翻转后**具体的数值结果**（VHDL 与 MATLAB 是否一致、是否真的落到边界附近），`待本地验证`——建议按 4.3.4 在 MATLAB 里实测记录。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `cl_fix_mean_angle` 强制要求 `a_fmt.Signed = b_fmt.Signed` 且 `a_fmt.IntBits = b_fmt.IntBits`（[L2534-L2536](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2534-L2536)）？

> **答案**：象限判定完全依赖「两数最高 2 位的相对位置」。若两数符号性或整数位不同，它们最高位所对应的「数值权重」就不同，`(MSB,次高位)` 不再处在同一张象限图上，「外象限」「符号相反」这些判定就失去几何意义。强制符号性与整数位一致（只允许小数位不同），保证两数在同一圆周坐标系里比较象限。

**练习 2**：为什么 `cl_fix_mean_angle` 还要求位宽 `>= 2`（[L2537-L2539](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2537-L2539)）？

> **答案**：象限需要「最高 2 位」才能划分 4 块。位宽为 1 的数只有 1 位，连「次高位」都没有，无法判象限，算法无从谈起。故以 `severity failure` 直接拒绝。

---

### 4.3 precise 分支：象限级与全精度两种修正策略

#### 4.3.1 概念说明

`precise` 参数控制 `mean_angle` 用哪种「跨象限修正」策略，是**面积/时序 ↔ 精度**的取舍（与 [u7-l1](u7-l1-vhdl-tempfmt-synthesis.md) 讲的 `s` 变体同属一类设计哲学）：

- **`precise = false`（象限级）**：只看两操作数的高 2 位（象限），逻辑最简——几个 `MSB`/`次高位` 的比较即可判定。但它只能区分「两数各在哪个象限」，对落在同一象限对里的不同数值不做细分，修正可能不够精准。
- **`precise = true`（全精度）**：在象限判定的基础上，**额外看求和结果 `temp` 的高位**，从而在「象限相同但具体位置不同」的边界情形下也能正确决定是否转半圈。代价是多读了 `temp` 的若干位、逻辑稍复杂。

两种策略的**共同出口**都是「翻转和 `temp` 的 MSB」（在命中条件时），即把结果整体转半圈。差别只在「命中条件判得多准」。

> 💡 一句话：`precise=false` 是「只看输入象限」的粗修；`precise=true` 是「再看和的高位」的精修。两者命中时都靠翻转 `temp` 的 MSB 落实修正——这正是 4.1 讲的「翻转 MSB = 加半个量程 = 转半圈」的同款技巧在模运算里的应用。

#### 4.3.2 核心流程

精确分支的命中条件（VHDL，[L2549-L2550](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2549-L2550)）是四个条件的「与」：

\[
\text{toggle} \;=\; \textit{precise} \;\land\; \textit{differentSigns} \;\land\; (a_{\text{次高}} = b_{\text{次高}}) \;\land\; (\textit{temp}_{\text{bit}(\text{high}-2)} = a_{\text{次高}})
\]

逐项解读：

1. `precise`：开关本身。
2. `differentSigns`：仍是总闸（符号相反才有转半圈的可能）。
3. `a_次高 = b_次高`：两数次高位相同。配合符号相反（MSB 不同），意味着 `(a.MSB,a.次高)` 与 `(b.MSB,b.次高)` 只差 MSB——即「一个大一个小、符号相反」的非对称跨零情形（如 `(0,1)` 大正 vs `(1,1)` 小负）。
4. `temp` 的 `high-2` 位 = `a` 的次高位：用求和结果的第 3 高位做最终确认，判断和到底落在了圆周的哪一侧。

命中后执行 `temp_v(temp_v'high) := not temp_v(temp_v'high)`——翻转和的 MSB，把结果转半圈。

MATLAB 的对应分支（[L74-L79](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_mean_angle.m#L74-L79)）结构相同：读 `temp` 的 MSB 与 `bit2`，算 `toggle`，再 `cl_fix_set_msb` 写回。

#### 4.3.3 源码精读

**VHDL 精确分支**：

- [vhdl/src/en_cl_fix_pkg.vhd:L2549-L2552](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2549-L2552) — 四条件相与；命中时 `temp_v(temp_v'high) := not temp_v(temp_v'high)`。注意它读的是 `a_v(a_v'high-1)`（次高位，未被非精确块翻转影响），逻辑清晰。

**MATLAB 精确分支**（含注释说明它处理的两种情形）：

- [matlab/src/cl_fix_mean_angle.m:L72-L79](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_mean_angle.m#L72-L79) — 注释 `one point in quadrant 1 and one point in quadrant 3` 或 `one point in quadrant 0 and one point in quadrant 2`，正是「一大一小、符号相反」的两种非对称跨零情形。`toggle = different_signs & (AMsb1 == BMsb1) & (TempMsb2 == AMsb1)`，命中则 `cl_fix_set_msb(temp, 0, bitxor(TempMsb0, toggle))`。

**与 `cl_fix_mean` 的对照**：`cl_fix_mean`（[vhdl/src/en_cl_fix_pkg.vhd:L2486-L2508](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2486-L2508)）没有任何跨象限检测，直接 `add → shift(-1)`；`mean_angle` 的全部额外逻辑就是 4.2.3 + 4.3.3 这两段「检测 + 翻转」。

#### 4.3.4 代码实践

**实践目标**：在同一个跨边界角度对上，对比 `cl_fix_mean`（算术均值）与 `cl_fix_mean_angle`（`precise=true` / `false`）的差异，直观看到「转半圈」修正的效果。

**操作步骤（MATLAB 实测型实践，`待本地验证`）**：

> 选 MATLAB 是因为 Python 端是 `NotImplementedError` 桩（[L386-L391](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L386-L391)），VHDL 需要 Modelsim 工具链，MATLAB 最易跑通。

```matlab
% 示例代码：需先 cd 到 matlab/src 并运行过 cl_fix_constants
cl_fix_constants;
fmt  = cl_fix_format(true, 2, 1);        % W=4, 范围 -4..+3.9375
rfmt = cl_fix_format(true, 2, 2);        % 结果多 1 位小数，承载 /2
a = cl_fix_from_real(+3.5, fmt);
b = cl_fix_from_real(-3.5, fmt);

m_mean   = cl_fix_mean      (a, fmt, b, fmt,            rfmt, Round.Trunc_s, Sat.None_s);
m_np     = cl_fix_mean_angle(a, fmt, b, fmt, false,     rfmt, Round.Trunc_s, Sat.None_s);
m_p      = cl_fix_mean_angle(a, fmt, b, fmt, true,      rfmt, Round.Trunc_s, Sat.None_s);

disp(m_mean);  disp(m_np);  disp(m_p)   % 记录三者数值
```

**需要观察的现象**：

1. `m_mean`（算术均值）应落在 0 附近（远路中点）。
2. `m_np` 与 `m_p`（角均值）应与 `m_mean` 不同——若修正生效，它们会落在 `±max` 边界附近（近路中点）。
3. 比较 `m_np` 与 `m_p`：在某些边界情形下二者可能不同（这正是 `precise` 开关的意义）。

**预期结果**：能口头解释「`m_mean` 为何跑到 0、`mean_angle` 为何把它拉回边界」。**具体数值 `待本地验证`**——鉴于 4.2.3 指出的 VHDL/MATLAB 结构差异，请如实记录 MATLAB 的输出，不要假定与 VHDL 逐位一致。若手头有 Modelsim，可再跑一遍 VHDL 版本横向对照。

#### 4.3.5 小练习与答案

**练习 1**：`precise=true` 比 `precise=false` 多读了哪些位？为什么这部分逻辑在硬件里更贵？

> **答案**：`precise=true` 除输入的高 2 位外，还要读求和结果 `temp` 的高位（VHDL 读 `temp'high-2`，MATLAB 读 `temp` 的 `bit0` 和 `bit2`）。这意味着「是否翻转」的判定依赖于加法器的输出，形成一条从加法器到 MSB 选择端的组合路径，可能拉长关键路径时序；`precise=false` 只依赖输入位，翻转判定可以与加法并行、甚至提前算好。这是典型的「精度 ↔ 时序」取舍。

**练习 2**：精确分支命中时翻转的是 `temp`（求和结果）的 MSB，而非输入的 MSB。结合 4.1 的 offset binary 理论，这个「翻转 MSB」在模运算里几何上代表什么？

> **答案**：翻转 W 位数的 MSB 等价于「加半个量程 \(2^{W-1}\)」。在模运算（圆周）里，加半个量程就是把点沿圆周**整体转半圈**。算术均值落在了远路中点，把结果转半圈就落到近路中点——这正是 `mean_angle` 修正的几何含义。它和 `cl_fix_compare` 翻转 MSB 是同一个位操作，但用途不同：compare 用它来「保序」（把补码变成 offset binary 以便无符号比较），mean_angle 用它来「转半圈」（在圆周上平移半个量程）。

---

## 5. 综合实践

把本讲两个函数串起来，完成下面这个「比较 + 模均值」的综合任务。

**任务**：给定一对跨边界的角度定点数（如 `a_fmt = b_fmt = (true, 2, 1)`，`a = +3.5`、`b = -3.5`），完成三件事。

1. **用 `cl_fix_compare` 量化「算术均值偏离输入的程度」**。在 VHDL testbench 里，分别用 `cl_fix_compare("a<b", ...)` / `("a>b", ...)` 比较：
   - `m_mean`（`cl_fix_mean` 的结果）与 `a`、`b` 的大小关系；
   - `m_angle`（`cl_fix_mean_angle` 的结果）与 `a`、`b` 的大小关系。
   
   预期：算术均值 `m_mean ≈ 0` 会满足 `m_mean < a` 且 `m_mean > b`（落在两数之间，但这是「远路」中点）；角均值 `m_angle` 应贴近边界，关系可能不同。把每条 `cl_fix_compare` 的预期布尔值写下来。

2. **解释 `cl_fix_compare` 为何能正确比较 `m_mean` 与 `a`**（尽管二者格式可能不同）：复述 4.1 的 `FullFmt` 对齐 + 翻转 MSB 流程，指出它比较的是真实数值而非位串表面。

3. **写一句总结**：用一句话回答「**翻转 MSB 这个操作，在 `cl_fix_compare` 和 `cl_fix_mean_angle` 里分别起了什么作用？为什么同一个位操作能服务于两个看似无关的目的？**」

**参考答案要点**：

- 任务 1 的具体布尔值 `待本地验证`（按 4.3.4 在 MATLAB 算出 `m_mean`/`m_angle` 后回填），但定性结论是：算术均值落在两数之间的「远路」中点，角均值落在边界附近的「近路」中点，二者的比较关系会不同。
- 任务 2：`cl_fix_compare` 用 `FullFmt`（符号之或 + 整数/小数位取大）无损对齐两数，再翻转 MSB 把补码变成 offset binary，使无符号比较等价于有符号数值比较——所以即便 `m_mean` 与 `a` 格式不同，比的是真实值。
- 任务 3 一句话：**翻转 MSB = 加 \(2^{W-1}\)（半个量程）。`cl_fix_compare` 借它的「保序性」把补码映射成全正的 offset binary 以便无符号比较；`cl_fix_mean_angle` 借它的「平移性」把圆周上的结果转半圈以修正远路均值。同一个算术操作（加常数），一个用它的单调性、一个用它的模运算平移效果，殊途同归。**

> 若想验证比较结果，可在 testbench 的 `cl_fix_compare` 段（[L643-L739](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L643-L739)）后仿照 `CheckBoolean(cl_fix_compare("a<b", m_mean, rfmt, a, fmt))` 新增几条断言，`待本地验证`（需 Modelsim）。

## 6. 本讲小结

- `cl_fix_compare` 的核心是「**对齐到 `FullFmt` → 翻转 MSB → 用 `unsigned` 比较**」。翻转 MSB 把补码变成 offset binary，等价于全体加 \(2^{W-1}\)，是严格单调递增的保序映射，故无符号比较等价于有符号比较。
- 一个 W 位补码数翻转 MSB 后的无符号读数恰好等于「补码值 \(+\ 2^{W-1}\)」，范围从 \([-2^{W-1},\ 2^{W-1}-1]\) 平移到 \([0,\ 2^{W}-1]\)，不溢出；无符号数不需要翻转（无负权重位）。
- `cl_fix_mean_angle` 处理「模运算/角度」下的均值：当两数符号相反且都处「外象限」（`MSB ≠ 次高位`，即贴近 `±max` 边界）时，算术均值会走远路，需把结果转半圈（翻转和的 MSB）修正。
- `precise=false` 只看两操作数高 2 位象限（便宜、粗修）；`precise=true` 额外看求和结果的高位（更准、时序更贵）。两者命中时都靠翻转 `temp` 的 MSB 落实修正。
- **跨语言现状**：`cl_fix_compare` 是 VHDL 独有（Python/MATLAB 在实数域直接用 `<`/`>` 比）；`cl_fix_mean_angle` 在 VHDL/MATLAB 有完整实现、Python 仅 `NotImplementedError` 桩、且 testbench 无测试覆盖，VHDL 与 MATLAB 的非精确预处理在结构上还有差异——因此对 `mean_angle` 的数值结果应「读码 + 本地实测」，不应假定三语言逐位一致。
- 两个函数共享同一个底层位技巧——**翻转 MSB = 加半个量程**：compare 用它的保序性做比较，mean_angle 用它的平移性做模运算修正。

## 7. 下一步学习建议

本讲把「比较」与「模运算均值」两个精巧算法讲完了，接下来建议：

1. **[u7-l3](u7-l3-string-parsing-generics.md) 字符串解析与 generic 传参**：看 VHDL 内部的字符串解析工具链（`toLower`、`string_parse_boolean/int`）如何把 `[S,I,F]` 格式以字符串 generic 传入仿真，理解 Modelsim 只支持 `integer/string/boolean` generic 的工程背景——这与本讲 `cl_fix_compare` 用字符串 `"a<b"` 选择运算符是同一类「用字符串传枚举」的手法。
2. **回头对照 [u3-l4](u3-l4-helpers-compare-range.md)**：那里讲了 `cl_fix_in_range` 如何复用 `cl_fix_compare` 做边界判断，以及 `FullFmt` 对齐在范围检查中的另一种用法，可与本讲 4.1 互相印证。
3. **动手补测试**：本讲揭示 `cl_fix_mean_angle` 缺少 testbench 覆盖、Python 端是桩函数。若你想深入，可尝试为它设计一组 `CheckBoolean`/`CheckReal` 断言（跨边界与同号各几例），或为 Python 端按 MATLAB 逻辑补一个实现——这是检验你是否真正读懂 4.2/4.3 的最好方式（**待本地验证**）。
