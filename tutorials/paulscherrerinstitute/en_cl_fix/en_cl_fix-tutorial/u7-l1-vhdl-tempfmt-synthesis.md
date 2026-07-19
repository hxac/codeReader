# VHDL TempFmt 全精度中间格式与综合考量

## 1. 本讲目标

本讲是专家层（Unit 7）的第一篇，从**架构层面**俯瞰整个 `en_cl_fix_pkg.vhd`，把前面所有讲义里散见的设计决策收拢成一条统一的主线。

读者学完后应该能够：

1. 用一句话说清贯穿全库的统一架构模式：**「中间全精度 TempFmt → 精确运算 → `cl_fix_resize` 舍入/饱和」**，并理解为什么所有运算最终都汇聚到 `cl_fix_resize`。
2. 为 `cl_fix_add` / `cl_fix_sub` / `cl_fix_mult` / `cl_fix_shift` / `cl_fix_mean` 各自写出 `TempFmt_c` 的构造式，并解释每条位增长规则的来源。
3. 读懂 `CarryBit_c` / `Saturate_c` / `Grow_c` / `AddSignBit_c` 这几个布尔常量如何**按需**给中间格式加位，从而在「精确」与「省硬件」之间取得平衡。
4. 解释 `-- synthesis translate_off` 注释为什么只包住 `Warn_s` 分支，而 `assert ... severity warning` 却不需要包，理解二者在综合时的本质差别。
5. 掌握 `s` 变体（`cl_fix_sneg` / `cl_fix_sabs` / `cl_fix_saddsub`）用「按位取反替代补码 +1」换取面积/时序、代价是 1 LSB 误差的设计取舍。

本讲不重复 [u3-l2](u3-l2-resize-rounding.md)、[u3-l3](u3-l3-resize-saturation.md) 对 `cl_fix_resize` 内部位级算法的拆解，也不重复 [u4-l1](u4-l1-add-sub.md)、[u4-l2](u4-l2-multiply.md) 对各运算语义的讲解；本讲只从**综合导向的架构取舍**角度把它们重新串起来。

## 2. 前置知识

本讲默认读者已经掌握以下概念（若生疏请先回看对应讲义）：

- **定点格式 `[S, I, F]`** 与位宽公式 `W = S + I + F`（[u1-l2](u1-l2-fixformat-type.md)）。
- **舍入模式 `FixRound`** 与 **饱和模式 `FixSaturate`**（[u1-l4](u1-l4-rounding-modes.md)、[u1-l5](u1-l5-saturation-modes.md)）。
- `cl_fix_resize` 的内部算法：先加偏移再截断做舍入、用 `CutIntSignBits_c` 窗口检测溢出做饱和/回绕（[u3-l2](u3-l2-resize-rounding.md)、[u3-l3](u3-l3-resize-saturation.md)）。
- `ForAdd` / `ForSub` / `ForMult` 的格式增长规则（[u4-l1](u4-l1-add-sub.md)、[u4-l2](u4-l2-multiply.md)）。
- VHDL 的 `record`、`constant`、`signed` / `unsigned` 类型，以及 `std_logic_vector` 的切片与拼接。

还需要两个本讲要用到的小术语：

- **综合（synthesis）**：把 VHDL 描述翻译成 FPGA/ASIC 真实硬件（查找表、触发器、进位链）的过程。综合工具只关心「能映射成什么硬件」，会忽略纯仿真结构。
- **进位位（carry bit）/ 符号位扩展**：两个 N 位数相加最多需要 N+1 位才能装下结果，多出来的那一位就是进位位；它是本讲反复出现的「+1」。

## 3. 本讲源码地图

本讲几乎全部来自同一个文件，但会从不同函数切入：

| 文件 | 作用 |
| --- | --- |
| `vhdl/src/en_cl_fix_pkg.vhd` | 唯一的 VHDL 实现包。本讲聚焦其中 `cl_fix_resize`、`cl_fix_add`、`cl_fix_sub`、`cl_fix_mult`、`cl_fix_shift`、`cl_fix_mean`、`cl_fix_neg`/`cl_fix_sneg`、`cl_fix_abs`/`cl_fix_sabs`、`cl_fix_addsub_internal`、`cl_fix_saddsub`，以及辅助函数 `toInteger`、`max`。 |

读者可以打开该文件，按本讲给出的行号定位每个函数。所有永久链接基于当前 HEAD `7f7aa80f79caf9eefcbb9946feabc882b98bb4aa`。

## 4. 核心概念与源码讲解

### 4.1 TempFmt 全精度中间格式与统一架构模式

#### 4.1.1 概念说明

回顾前面讲义你会发现一个现象：无论是加、减、乘、移位、均值、取反、绝对值，每个运算函数的函数体都长得几乎一样——

1. 先把输入 `cl_fix_resize` 到一个**中间格式 `TempFmt_c`**；
2. 在 `TempFmt_c` 上做一次**精确的、无精度损失的**硬件运算（一次加法、一次乘法、一次取反……）；
3. 再把结果 `cl_fix_resize` 到用户要的 `result_fmt`，**所有舍入和饱和都只在这一步发生**。

这就是 `en_cl_fix` 全库的统一架构模式：

> **中间全精度 `TempFmt_c` → 精确运算 → `cl_fix_resize` 舍入/饱和。**

`TempFmt_c` 的设计目标是：**恰好能无损装下这次运算的精确结果**。它由当前运算的「位增长规则」决定（加法 +1 整数位、乘法整数位相加再 +1、移位把位在整数侧/小数侧之间搬运……）。因为中间这一步没有任何量化，所以**运算本身永远是精确的**，全部误差都被推迟、并集中到最后那一次 `cl_fix_resize`。

这带来三个工程上的好处：

- **正确性可推理**：只要 `TempFmt_c` 足够宽，运算就是精确的；误差只来自一个地方（最后的 resize），便于验证与对齐三种语言的位真结果。
- **代码高度统一**：所有运算函数都是同一个「三段式」骨架，维护成本低。
- **所有路径汇聚到一点**：舍入和饱和的复杂逻辑只写在 `cl_fix_resize` 里写一次，其余函数复用，避免重复造轮子。

#### 4.1.2 核心流程

「三段式」骨架的伪代码：

```
function cl_fix_<op>(a, a_fmt, ..., result_fmt, round, saturate) is
    constant TempFmt_c : FixFormat_t := <本运算的位增长规则>;
begin
    -- 第一段：无损扩展/对齐到 TempFmt_c（截断舍入 + 不饱和 = 纯扩展）
    a_v := cl_fix_resize(a, a_fmt, TempFmt_c, Trunc_s, None_s);
    [b_v := cl_fix_resize(b, b_fmt, TempFmt_c, Trunc_s, None_s);]  -- 二元运算才有
    -- 第二段：在 TempFmt_c 上做一次精确运算
    temp_v := <精确的 op>(a_v[, b_v]);
    -- 第三段：唯一的舍入/饱和出口
    return cl_fix_resize(temp_v, TempFmt_c, result_fmt, round, saturate);
end;
```

注意三个细节：

- 第一段扩展用的是 `Trunc_s`（不四舍五入）+ `None_s`（不饱和）。因为 `TempFmt_c` 是为「装下精确结果」而设计的，向它扩展时**只可能加位、不可能丢位**，所以 `Trunc_s` 在这里等价于无损的符号/零扩展，`None_s` 保证不会被误夹紧。
- 第二段的运算用 VHDL 原生的 `signed` / `unsigned` 算术（`+`、`-`、`*`、`not`），结果位宽等于 `TempFmt_c` 的位宽。
- 第三段把 `TempFmt_c` 当作「源格式」交给 `cl_fix_resize`，由它统一完成舍入与饱和。

#### 4.1.3 源码精读

最干净的例子是 [`cl_fix_shift`](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2566-L2574)：它甚至没有「第二段运算」，整个函数就是「构造 `TempFmt_c` + 一次 resize」：

- [vhdl/src/en_cl_fix_pkg.vhd:L2566-L2574](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2566-L2574) — `TempFmt_c` 把 `result_fmt` 的整数位/小数位按 `shift` 重新分配（`IntBits => result_fmt.IntBits - shift, FracBits => result_fmt.FracBits + shift`），然后直接 `return cl_fix_resize(a, a_fmt, TempFmt_c, ...)`。移位本身不丢精度，精度只由最后这次 resize 决定。

更有代表性的是 [`cl_fix_mult`](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2578-L2615) 的三段式：

- [vhdl/src/en_cl_fix_pkg.vhd:L2586-L2592](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2586-L2592) — `TempFmt_c` 用乘法增长规则：`Signed => a.Signed or b.Signed`、`IntBits => Ia+Ib+(有符号?1:0)`、`FracBits => Fa+Fb`。
- [vhdl/src/en_cl_fix_pkg.vhd:L2600-L2612](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2600-L2612) — 第二段：按 a/b 是否有符号分四支，用 `signed`/`unsigned` 的 `*` 做精确乘法（混合符号时用 `"0" & signed(...)` 把无符号数零扩展成非负补码数）。
- [vhdl/src/en_cl_fix_pkg.vhd:L2613](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2613) — 第三段：`result_v := cl_fix_resize(temp_v, TempFmt_c, result_fmt, round, saturate);`，唯一的舍入/饱和出口。

`cl_fix_mean` 则展示了一个运算如何**复用**别的运算 + resize 的三段式：它先调 `cl_fix_add` 算精确和，再调 `cl_fix_shift(..., -1, ...)` 做除以 2，两次内部各自又走一遍三段式：

- [vhdl/src/en_cl_fix_pkg.vhd:L2495-L2507](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2495-L2507) — `TempFmt_c` 给加法结果预留 `+1` 整数位（吸收两个大正数相加的进位），`temp_v := cl_fix_add(...)`，`result_v := cl_fix_shift(temp_v, TempFmt_c, -1, ...)`。

> 💡 关键结论：`cl_fix_resize` 是全库唯一的「舍入 + 饱和」实现点。每个运算函数都只是「构造 `TempFmt_c` + 精确运算 + 调一次 resize」。这就是为什么本讲义反复强调「所有运算最终都汇聚到 `cl_fix_resize`」——改舍入/饱和逻辑只需改这一处。

#### 4.1.4 代码实践

**实践目标**：亲手在三个运算上验证「三段式」骨架，并画出数据流图。

**操作步骤（源码阅读型实践）**：

1. 打开 `vhdl/src/en_cl_fix_pkg.vhd`，定位 `cl_fix_add`（L2348）、`cl_fix_sub`（L2384）、`cl_fix_mult`（L2578）。
2. 对每个函数，在纸上画出下面这张数据流图（以 add 为例）：

   ```
   a (aFmt) ──resize(Trunc,None)──► a_v ─┐
                                         ├─ addsub_internal(add=1) ─► temp_v ─resize(round,sat)─► result
   b (bFmt) ──resize(Trunc,None)──► b_v ─┘
                          〔全部在 TempFmt_c 中〕
   ```
3. 标出三段式在源码里的起止行：第一段 resize、第二段运算、第三段 resize。
4. 对 `cl_fix_sub`，注意它的「第三段」resize 用的源格式不是 `SubFmt_c` 而是 `ReszFmt_c`（见 4.2.3），在图上标出这个差异。

**需要观察的现象**：三个函数的骨架完全同构；`cl_fix_mult` 没有第一段 resize（输入直接进乘法器），因为它不需要先对齐小数点。

**预期结果**：你应当得到一张统一的「三段式」模板图，三个函数的区别只在「`TempFmt_c` 怎么构造」和「第二段做什么运算」。

#### 4.1.5 小练习与答案

**练习 1**：`cl_fix_shift` 只有「一段」（构造 `TempFmt_c` + 一次 resize），没有第二段运算。为什么它仍能实现「移位」？

> **答案**：因为定点数的移位**只改变 `[S,I,F]` 格式、不改变位串本身**。左移一位意味着「小数点右移一位」，等价于把 `FracBits` 减 1、`IntBits` 加 1，数值 `V = N × 2^(-F)` 自动变大 2 倍。所以 `cl_fix_shift` 只需构造一个新的 `TempFmt_c`（把位在整数侧/小数侧重新分配），位串原封不动交给 resize 即可。精度只由最后那次 resize 决定。

**练习 2**：`cl_fix_mean` 内部调了 `cl_fix_add` 再调 `cl_fix_shift(-1)`，相当于走了两遍「三段式」。这种「用高层运算拼装」的做法有什么好处和代价？

> **答案**：好处是代码复用、正确性易保证（加法和移位各自的 `TempFmt_c` 已经各自保证精确）。代价是中间多了一次 resize（`cl_fix_add` 末尾那次），理论上可能比「手写一次加法 + 一次除 2」多一拍或多位中间信号；但因为是 `Trunc_s/None_s` 无损扩展，结果仍位真。这是「可读性/复用」对「极致面积」的典型取舍。

---

### 4.2 位增长控制：CarryBit_c / Saturate_c / Grow_c / AddSignBit_c

#### 4.2.1 概念说明

`TempFmt_c` 必须「恰好够宽」：太窄会丢精度（违背精确运算的前提），太宽会浪费硬件（综合出更大的加法器/乘法器）。于是每个运算函数都用一两个**布尔常量**来「按需加位」：

- `CarryBit_c`：是否给整数位 +1，吸收加法的进位。
- `Saturate_c`：是否因为要做饱和而需要额外的检测位。
- `Grow_c`：结果格式是否比输入「长出来了」。
- `AddSignBit_c`：`cl_fix_resize` 内部一个「无符号饱和」专用的额外符号位。

这些布尔量最终通过一个小辅助函数 [`toInteger`](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1058-L1066)（`true→1, false→0`）加到 `IntBits` 上，再配合 [`max`](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1020-L1028) 取两操作数的较大值，得到 `TempFmt_c`。理解这些「开关」什么时候打开，就理解了「精确」与「省硬件」的平衡点。

#### 4.2.2 核心流程

以 `cl_fix_add` 为例，`TempFmt_c.IntBits` 的构造式是：

\[
\text{TempFmt.IntBits} = \max(I_a, I_b) + \text{toInteger}(\text{CarryBit\_c})
\]

其中 `CarryBit_c` 在三种情况下为真：

1. **结果在增长**：`result_fmt.IntBits > max(Ia, Ib)`。结果格式比两个输入都宽，说明用户要保留进位填到更高位，必须留出 +1。
2. **要做饱和（夹紧）**：`saturate = Sat_s`。
3. **要做饱和告警**：`saturate = SatWarn_s`（以及仿真时的 `Warn_s`，见 4.3）。

后两种情况需要 +1 的原因相同：饱和/告警要先**检测**有没有溢出，而检测需要看到「进位是否冒出了结果位宽」。如果不预留这个 +1，进位会在进入 resize 之前就被丢弃（回绕），resize 再也看不到溢出标志，也就无法夹紧或告警。

> 用一句话概括：「**回绕（None_s）且结果不增长**时不需要 +1（最省硬件的回绕加法器）；其余情况都要 +1。」

#### 4.2.3 源码精读

**`cl_fix_add` 的 CarryBit_c 与 TempFmt_c**：

- [vhdl/src/en_cl_fix_pkg.vhd:L2356-L2362](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2356-L2362) — `CarryBit_c` 的定义。注意第 2358–2360 行的 `-- synthesis translate_off/on` 包住了 `saturate = Warn_s or` 这一行（详见 4.3）。
- [vhdl/src/en_cl_fix_pkg.vhd:L2363-L2368](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2363-L2368) — `TempFmt_c`：`Signed => a.Signed or b.Signed`、`IntBits => max(Ia,Ib) + toInteger(CarryBit_c)`、`FracBits => max(Fa,Fb)`。
- [vhdl/src/en_cl_fix_pkg.vhd:L2374-L2379](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2374-L2379) — 三段式函数体：先各自 resize 到 `TempFmt_c`，再 `cl_fix_addsub_internal(..., '1')` 做加法，最后 resize 到 `result_fmt`。

**`cl_fix_sub` 的 Saturate_c / Grow_c / SubFmt_c / ReszFmt_c**：减法比加法多一个细节——它区分了「做减法用的中间格式 `SubFmt_c`」和「最后 resize 用的源格式 `ReszFmt_c`」。

- [vhdl/src/en_cl_fix_pkg.vhd:L2392-L2397](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2392-L2397) — `Saturate_c`（同样把 `Warn_s` 包在 translate_off 里）与 `Grow_c`（`result_fmt.IntBits > max(Ia,Ib)`）。
- [vhdl/src/en_cl_fix_pkg.vhd:L2399-L2411](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2399-L2411) — `SubFmt_c.IntBits = max(Ia,Ib) + toInteger(Grow_c or Saturate_c)`；而 `ReszFmt_c` 在 `Saturate_c` 为真时把 `Signed` 强制置真。原因是：**无符号减法可能出现「借位」使结果变负**，而负数必须用有符号格式才能被 resize 正确检测溢出与夹紧。所以一旦要饱和，就把交给 resize 的源格式改成有符号。
- [vhdl/src/en_cl_fix_pkg.vhd:L2418-L2422](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2418-L2422) — 函数体：resize 到 `SubFmt_c`，做减法 `cl_fix_addsub_internal(..., '0')`，最后用 `ReszFmt_c` 作为源格式 resize 到 `result_fmt`。

**`cl_fix_resize` 自己的 CarryBit_c / AddSignBit_c / TempFmt_c**：resize 作为终点函数，它内部也维护自己的中间格式（这点在 [u3-l3](u3-l3-resize-saturation.md) 已详细讲过，这里只做架构视角的回顾）。

- [vhdl/src/en_cl_fix_pkg.vhd:L2033-L2038](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2033-L2038) — `DropFracBits_c`、`NeedRound_c`、`CarryBit_c`（`NeedRound_c and saturate /= None_s`，为舍入进位预留位）、`AddSignBit_c`（仅无符号→无符号且要饱和时为真，源码注释坦承「undocumented」）。
- [vhdl/src/en_cl_fix_pkg.vhd:L2053-L2058](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2053-L2058) — resize 内部的 `TempFmt_c`：`IntBits => max(a.IntBits + CarryBit_c, result.IntBits) + AddSignBit_c`。这正是 4.1 所说「所有路径汇聚于此」的落点。

#### 4.2.4 代码实践

**实践目标**：手算 `TempFmt_c`，体会「开关」如何改变中间位宽。

**操作步骤**：

1. 取 `a_fmt = (true, 3, 5)`、`b_fmt = (false, -2, 8)`（沿用 [u4-l1](u4-l1-add-sub.md) 的例子），`result_fmt = (true, 4, 8)`。
2. 对 `cl_fix_add`，分别按 `saturate = None_s` 和 `saturate = Sat_s` 计算 `CarryBit_c` 与 `TempFmt_c.IntBits`：
   - `max(Ia,Ib) = max(3, -2) = 3`。
   - `result_fmt.IntBits > max` → `4 > 3` → 真，所以 `CarryBit_c` 恒真（无论 saturate）。
   - `TempFmt_c = (true, 3+1=4, max(5,8)=8)`。
3. 再把 `result_fmt` 改成 `(true, 3, 8)`（不增长），重算：
   - `None_s` 时 `CarryBit_c` 假 → `TempFmt_c.IntBits = 3`（最省硬件的回绕加法器）。
   - `Sat_s` 时 `CarryBit_c` 真 → `TempFmt_c.IntBits = 4`（多一位用于溢出检测）。

**需要观察的现象**：同一个 `result_fmt`，仅 `saturate` 不同就会让中间加法器位宽差 1 位。

**预期结果**：手算结果应与源码 `L2363-L2368` 的公式逐字对应。若想验证数值正确性，可用 Python（位真等价）跑一遍：`待本地验证`（需要 `python3` 与 `numpy`）。

#### 4.2.5 小练习与答案

**练习 1**：`cl_fix_sub` 为什么要额外引入 `ReszFmt_c`，而 `cl_fix_add` 不需要？

> **答案**：无符号减法 `a - b`（`a < b`）会产生借位，结果「应当」是负数，但无符号格式无法表示负数。如果还要做饱和，就必须让 resize 「看到」这个负值才能正确夹紧到下界。因此 `cl_fix_sub` 在 `Saturate_c` 为真时把交给 resize 的源格式 `ReszFmt_c.Signed` 强制置真，把减法结果当作有符号数处理。加法不会产生「无符号下的负数」，所以不需要这一步。

**练习 2**：`cl_fix_resize` 里的 `AddSignBit_c` 只在「无符号→无符号且 saturate≠None_s」时为真。结合 [u3-l3](u3-l3-resize-saturation.md) 的讲解，它实际上是干什么用的？

> **答案**：它是为「无符号饱和」预留的一个恒为 0 的「假符号位」。无符号数本身没有符号位，溢出检测（高位是否非全 0）需要一个额外的最高位作为「是否超出上界」的判定位。加上这个位后，无符号饱和的检测窗口就和有符号情形统一了。源码注释「undocumented」反映它早期未被显式说明，但功能上就是无符号饱和的检测位。

---

### 4.3 综合导向的取舍：synthesis translate_off 与 assert 告警

#### 4.3.1 概念说明

这是本讲最精妙的综合取舍。`en_cl_fix` 的四种饱和模式中，`Warn_s` 很特别：它**只在仿真时报告越界、不产生任何夹紧硬件**。这意味着：

- **仿真**时，`Warn_s` 需要看到溢出，才能触发告警 → 中间格式需要 +1 位用于检测。
- **综合**时，`Warn_s` 既不夹紧、告警硬件也不存在 → 那个 +1 位纯属浪费，会让加法器无谓地宽一位。

于是源码用 `-- synthesis translate_off` / `-- synthesis translate_on` 这对**综合指令注释**，把 `Warn_s` 这个分支「只留给仿真」。综合工具见到 `translate_off` 就跳过中间的代码，仿佛 `saturate = Warn_s or` 这一行不存在；仿真器则照常执行。

> 💡 关键区分：`Warn_s` 在综合时「行为上等价于 `None_s`」（都回绕、都不夹紧），但仿真时仍会告警。`translate_off` 让两种工具看到不同的 `CarryBit_c`/`Saturate_c`，从而仿真保精度、综合省硬件。

#### 4.3.2 核心流程

`CarryBit_c`（`cl_fix_add`）的求值在两种工具下不同：

```
仿真（translate_off 不生效）:
  CarryBit_c := result.IntBits > max(Ia,Ib)
             or (saturate = Sat_s or saturate = Warn_s or saturate = SatWarn_s);

综合（translate_off 生效，Warn_s 行被删）:
  CarryBit_c := result.IntBits > max(Ia,Ib)
             or (saturate = Sat_s or saturate = SatWarn_s);
```

差别仅在 `Warn_s`：仿真多一种触发 +1 的情形，综合少一种。`Sat_s` / `SatWarn_s` 两种模式因为确实会生成夹紧硬件，必须保留 +1，所以不被包住。

#### 4.3.3 源码精读

全文件共有 **3 对** `-- synthesis translate_off/on`，全部出现在加/减类运算里，包住的都恰好是 `saturate = Warn_s or` 这一行：

- [vhdl/src/en_cl_fix_pkg.vhd:L2358-L2360](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2358-L2360) — `cl_fix_add` 的 `CarryBit_c` 中，包住 `saturate = Warn_s or`。
- [vhdl/src/en_cl_fix_pkg.vhd:L2393-L2395](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2393-L2395) — `cl_fix_sub` 的 `Saturate_c` 中，同样包住 `Warn_s`。
- [vhdl/src/en_cl_fix_pkg.vhd:L2459-L2461](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2459-L2461) — `cl_fix_saddsub` 中同款处理。

**对比：`assert` 告警为什么不需要 translate_off？**

- [vhdl/src/en_cl_fix_pkg.vhd:L2109](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2109) 与 [vhdl/src/en_cl_fix_pkg.vhd:L2117](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2117) — `cl_fix_resize` 里两条 `assert saturate = Sat_s report "cl_fix_resize : Saturation Warning!" severity warning;`，**没有** `translate_off` 包裹。

  原因：`assert` 语句是纯仿真结构，综合工具天然忽略它（不会生成任何硬件），所以无需保护。而 4.3 讨论的 `Warn_s` 分支不同——它参与的是一个**布尔常量表达式，该常量又决定了信号位宽**（`TempFmt_c → TempWidth_c → std_logic_vector` 的实际宽度），位宽是会被综合成真实硬件（更宽的加法器/进位链）的，所以必须用 `translate_off` 显式剔除。

> 用一张表总结两种「仿真专用」结构的处理差异：

| 结构 | 综合时会被忽略吗？ | 需要 `translate_off` 吗？ | 原因 |
| --- | --- | --- | --- |
| `assert ... severity warning` | 是（天然忽略） | **否** | 纯仿真语句，综合不生成硬件 |
| `saturate = Warn_s` 进位位分支 | **否**（会改变位宽→硬件） | **是** | 常量喂给信号宽度，综合会生成更宽的加法器 |

#### 4.3.4 代码实践

**实践目标**：确认 `translate_off` 的作用范围与影响。

**操作步骤（源码阅读型实践）**：

1. 在 `en_cl_fix_pkg.vhd` 中搜索 `synthesis translate`，确认全文件只有 3 对、且都包住 `Warn_s`。
2. 对 `cl_fix_add`（L2356-L2362），分别写出「仿真」与「综合」两种工具下 `CarryBit_c` 的化简布尔表达式。
3. 构造一个场景：`a_fmt = b_fmt = (false, 4, 0)`，`result_fmt = (false, 4, 0)`，`saturate = Warn_s`。
   - 仿真：`CarryBit_c` 真 → `TempFmt_c.IntBits = 5`，能检测到 `15 + 1 = 16` 的溢出并告警。
   - 综合：`CarryBit_c` 假 → `TempFmt_c.IntBits = 4`，加法器只有 4 位，`15 + 1` 直接回绕成 0（与 `None_s` 行为一致）。

**需要观察的现象**：同一个 VHDL 函数，仿真和综合看到的中间位宽不同；但**只要用户不依赖 `Warn_s` 去改变数值结果**（`Warn_s` 本就不夹紧），两者的数值输出在「未越界」时完全一致，越界时综合版按 `None_s` 回绕、仿真版按 `Warn_s` 回绕并打印告警。

**预期结果**：能口述「为什么 `Warn_s` 必须包进 `translate_off`，而 `Sat_s`/`SatWarn_s` 不能包」。**待本地验证**：若装了 Modelsim/Vivado，可分别跑仿真与综合，对比加法器位宽报告。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `-- synthesis translate_off` 这对注释**删掉**（让 `Warn_s` 分支在综合时也生效），功能会出错吗？会有什么后果？

> **答案**：数值功能不会错（`Warn_s` 不夹紧，多一位进位位只会让中间加法器宽一位，结果不变）。后果是**面积/时序变差**：所有用 `Warn_s`（库的默认饱和模式之一）的加法器都会综合出比必要宽 1 位的加法器。这正是加这对注释要避免的。

**练习 2**：`cl_fix_resize` 里的 `assert ... severity warning`（L2109/L2117）为什么**不**需要 `translate_off`？

> **答案**：`assert` 是 VHDL 的仿真断言，综合工具不会把它映射成任何硬件，天然被忽略。它不像 `Warn_s` 分支那样会通过位宽影响真实硬件，所以无需保护。

---

### 4.4 s 变体的资源/时序优化：sneg / sabs / saddsub

#### 4.4.1 概念说明

`en_cl_fix` 为取反、绝对值、加减三类运算各提供了一个带 `s` 前缀的「资源优化变体」：`cl_fix_sneg`、`cl_fix_sabs`、`cl_fix_saddsub`。它们的共同设计思想是：

> **用「按位取反 `not`」替代「二进制补码取反 `not + 1`」，省掉那个 +1 加法器（以及为它预留的整数位），代价是结果最多偏差 1 LSB。**

回顾补码：一个数 `x` 的相反数是 `-x = not(x) + 1`（按位取反再加 1）。那个「+1」需要一个加法器/进位链来实现。`s` 变体直接省掉「+1」，只做 `not(x)`，于是 `not(x) = -x - 1`，比真正的取反少了 1 个 LSB。

- 对**正数**路径（如 `sabs` 中正数不取反、`saddsub` 的加法分支），不涉及取反，**结果与精确版完全一致，零误差**。
- 对**负数/减法**路径，结果比精确值少 1 LSB。

这是典型的「面积/时序 ↔ 精度」取舍：1 LSB 的量化噪声在大多数信号处理应用里可以接受，换来的是更小的电路、更短的关键路径。

#### 4.4.2 核心流程

| 函数 | 精确版核心运算 | `s` 变体核心运算 | 误差 |
| --- | --- | --- | --- |
| `cl_fix_neg` / `cl_fix_sneg` | `-signed(x)`（= `not x + 1`） | `not x` | 负数结果差 1 LSB |
| `cl_fix_abs` / `cl_fix_sabs` | 负数时 `unsigned(not x) + 1` | 负数时 `not x` | 负数结果差 1 LSB |
| `cl_fix_addsub` / `cl_fix_saddsub` | 减法时 `a - b`（内部含 +1） | 减法时 `a + not b`（省 +1） | 减法结果差 1 LSB |

`s` 变体的 `TempFmt_c` 也相应更窄：因为不再需要为「+1」预留进位位，整数位不必 +1。

> 💡 一句话：`s` 变体「**永远只做加法**」——`sneg` 把取反变成「加 nothing」（纯 `not`），`sabs` 把取绝对值变成「条件 `not`」，`saddsub` 把减法变成「先 `not b` 再加」。三者都用 `not` 把「需要 +1 的运算」降级为「不需要 +1 的运算」。

#### 4.4.3 源码精读

**取反：`cl_fix_neg`（精确）vs `cl_fix_sneg`（s 变体）**：

- [vhdl/src/en_cl_fix_pkg.vhd:L2278-L2285](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2278-L2285) — `cl_fix_neg`：先用 `AFullFmt_c = (true, Ia + Signed?1:0, Fa)`（多一个整数位，容纳最负值 `-2^I` 取反后的 `+2^I`），再 `Neg_v := std_logic_vector(-signed(AFull_v));`（真正的补码取反，含 +1）。
- [vhdl/src/en_cl_fix_pkg.vhd:L2296-L2316](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2296-L2316) — `cl_fix_sneg`：`TempFmt_c.IntBits = a.IntBits`（**不** +1），核心运算只有 `temp_v := not temp_v;`（L2312），没有 +1。注意它还有一个 `assert a_fmt.Signed ... severity failure`（L2306-L2308）：对无符号数取反没有意义，直接报错。

**绝对值：`cl_fix_abs`（精确）vs `cl_fix_sabs`（s 变体）**：

- [vhdl/src/en_cl_fix_pkg.vhd:L2218-L2236](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2218-L2236) — `cl_fix_abs`：`TempFmt_c.IntBits = Ia + Signed?1:0`（多一位装 `+2^I`），负数时 `temp_v := std_logic_vector(unsigned(not temp_v) + 1);`（L2231，含 +1）。
- [vhdl/src/en_cl_fix_pkg.vhd:L2247-L2267](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2247-L2267) — `cl_fix_sabs`：`TempFmt_c.IntBits = a.IntBits`（不 +1），负数时只有 `temp_v := not temp_v;`（L2260）。正数直接 resize，零误差。

**加减：`cl_fix_saddsub`（s 变体）**：

- [vhdl/src/en_cl_fix_pkg.vhd:L2457-L2482](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2457-L2482) — 与 `cl_fix_add` 共享同一个 `CarryBit_c`/`TempFmt_c`（L2457-L2468），区别在函数体 L2477-L2480：当 `add = '0'`（要做减法）时，先 `b_v := not b_v;`，然后**恒定调用 `cl_fix_addsub_internal(..., '1')` 做加法**。于是 `a - b` 变成 `a + not(b) = a + (-b-1) = (a-b) - 1`，省掉了减法器隐含的 +1，代价是减法结果少 1 LSB。加法分支（`add = '1'`）不取反，与精确 `cl_fix_add` 完全一致。

> 注意：`s` 变体也复用 [`cl_fix_addsub_internal`](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2320-L2344)（L2320-L2344），它按 `IsSigned_c` 选用 `signed`/`unsigned` 的 `+`/`-`，注释 L2328-L2329 特别提醒「综合工具要求加法必须用正确的 signed/unsigned 类型」。

#### 4.4.4 代码实践

**实践目标**：直观看到「s 变体在负数/减法路径上差 1 LSB，正数/加法路径零误差」。

**操作步骤（源码阅读 + 可选 Python 验证）**：

1. 取 `a_fmt = (true, 3, 4)`，输入值 `-3.0`（即定点表示 `-3.0 × 2^4 = -48` 的位串）。`result_fmt = (true, 3, 4)`。
2. 阅读 [vhdl/src/en_cl_fix_pkg.vhd:L2278-L2285](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2278-L2285) 与 [vhdl/src/en_cl_fix_pkg.vhd:L2296-L2316](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2296-L2316)：
   - `cl_fix_neg(-3.0)` = `+3.0`（精确）。
   - `cl_fix_sneg(-3.0)` = `not(位串)` = `-(-3.0) - 1 LSB = +3.0 - 0.0625 = +2.9375`。
3. （可选，`待本地验证`）用 Python 位真实现验证：

   ```python
   from en_cl_fix_pkg import *           # 示例代码：依赖 numpy
   a = cl_fix_from_real(-3.0, FixFormat(True, 3, 4))
   r_fmt = FixFormat(True, 3, 4)
   print(cl_fix_to_real(cl_fix_neg(a, FixFormat(True,3,4), r_fmt)))   # 期望 +3.0
   # cl_fix_sneg 在 Python 端名为同款 s 变体；若存在则比较差值
   ```

   注意：Python 端的 s 变体命名以实际 `__init__.py` 导出为准；若不确定请先 `dir()` 查看。

**需要观察的现象**：负数取反时，`sneg` 比 `neg` 小 1 LSB；正数取反（如有符号正数）两者一致。

**预期结果**：能用一句话解释「为什么 `s` 变体省了一个加法器」——因为它把 `not + 1` 简化成了 `not`。

#### 4.4.5 小练习与答案

**练习 1**：`cl_fix_sneg` 对**正数**取反，结果会比精确版少 1 LSB 吗？

> **答案**：不会。`not` 是对**整个位串**按位取反，与输入正负无关；误差恒为「1 个结果的 LSB」。但要注意：对正数 `x`，`not(x)` 在补码下等于 `-x - 1`，所以 `sneg(+3.0)` 得到的是 `-3.0 - 1 LSB`，同样差 1 LSB——误差方向取决于符号，但**幅度恒为 1 LSB**。区别于 `sabs`/`saddsub`：`sabs` 对正数不取反（零误差），`saddsub` 加法分支不取反（零误差）。

**练习 2**：`cl_fix_saddsub` 在做**加法**（`add = '1'`）时，结果会与精确的 `cl_fix_add` 不同吗？

> **答案**：不会。加法分支里 `b_v` 不做 `not`，直接进入 `cl_fix_addsub_internal(..., '1')`，与 `cl_fix_add` 的加法路径逐位一致。`s` 变体的 1 LSB 误差**只出现在减法分支**（`add = '0'`，对 `b` 取反再相加）。这是「s 变体」的普遍特征：省略发生在「需要 +1 的那条路径」，另一条路径保持精确。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「架构俯瞰」任务。

**任务**：选取 `cl_fix_add`、`cl_fix_sub`、`cl_fix_mult` 三个函数，完成三件事。

1. **画数据流图**。对每个函数，画出「`TempFmt_c` → 精确运算 → `cl_fix_resize`」的三段式数据流，标注：
   - `TempFmt_c` 的构造式（含 `CarryBit_c` / `Saturate_c` / `Grow_c`）；
   - 第二段用的 VHDL 运算（`+` / `-` / `*`，以及是否经过 `cl_fix_addsub_internal`）；
   - 第三段 resize 的源格式与目标格式（注意 `cl_fix_sub` 用的是 `ReszFmt_c` 而非 `SubFmt_c`）。

2. **标注 `synthesis translate_off` 的影响范围**。在三张图上用不同颜色标出：
   - 哪个布尔量（`CarryBit_c` / `Saturate_c`）受 `translate_off` 影响；
   - 仿真与综合两种工具下，`TempFmt_c.IntBits` 各是多少（给定一组具体 `a_fmt`/`b_fmt`/`result_fmt`/`saturate`）。

3. **写一句总结**：用一句话回答「**为什么所有运算最终都汇聚到 `cl_fix_resize`？**」

**参考答案要点**：

- 三张图都呈「三段式」同构骨架，区别仅在 `TempFmt_c` 构造与第二段运算。
- `translate_off` 只影响加/减类里 `Warn_s` 这一项；给定具体参数后，仿真版 `TempFmt_c.IntBits` 可能比综合版大 1。
- 一句话总结：**因为 `TempFmt_c` 已经无损装下了精确结果，全部量化误差（舍入）与范围处理（饱和/回绕）都被推迟并集中到最后一次 `cl_fix_resize`；舍入和饱和的复杂逻辑只在 `cl_fix_resize` 写一次，所有运算复用它，既保证三种语言的位真一致性，又让综合后的硬件只在唯一出口处付出舍入/饱和代价。**

> 若想验证数据流图中的数值，可用 Python 位真实现（`from en_cl_fix_pkg import *`）对照，**待本地验证**。

## 6. 本讲小结

- 全库统一架构模式是：**中间全精度 `TempFmt_c` → 精确运算 → `cl_fix_resize` 舍入/饱和**。所有运算函数都是这个「三段式」骨架。
- `TempFmt_c` 由「位增长规则」+ 一组按需加位的布尔开关构造：`CarryBit_c`（加法进位）、`Saturate_c`/`Grow_c`（饱和检测/结果增长）、`AddSignBit_c`（无符号饱和检测位）。
- `cl_fix_resize` 是全库**唯一**的舍入+饱和实现点，所有运算最终都汇聚到它。
- `-- synthesis translate_off/on` 只包住 `Warn_s` 分支：仿真时 `Warn_s` 触发 +1 以检测溢出告警，综合时该分支被剔除以省掉无谓的宽加法器——因为 `Warn_s` 不生成夹紧硬件。
- `assert ... severity warning` 不需要 `translate_off`，因为断言是纯仿真结构，综合天然忽略；而 `Warn_s` 分支会通过位宽影响真实硬件，必须显式剔除。
- `s` 变体（`sneg`/`sabs`/`saddsub`）用「按位取反 `not`」替代「补码取反 `not + 1`」，省掉 +1 加法器与额外整数位，代价是负数/减法路径最多差 1 LSB；正数/加法路径零误差。

## 7. 下一步学习建议

本讲把「统一架构」从架构视角讲完了，接下来建议：

1. **[u7-l2](u7-l2-compare-mean-angle.md) 比较与 mean_angle 模运算**：剖析两个更精巧的算法——`cl_fix_compare` 如何用「翻转符号位」把有符号比较转成无符号偏移比较（与本讲 4.3 的综合取舍呼应），以及 `cl_fix_mean_angle` 如何处理角度跨象限的均值修正。
2. **[u7-l3](u7-l3-string-parsing-generics.md) 字符串解析与 generic 传参**：看 VHDL 内部的字符串解析工具链如何把 `[S,I,F]` 以字符串 generic 传入仿真，理解 Modelsim 只支持 integer/string/boolean generic 的工程背景。
3. **回头对照 [u6-l2](u6-l2-wide-fxp-class.md)**：Python 的 `wide_fxp` 用「未归一化大整数 + 整数位运算」实现了与 VHDL 完全同构的 resize（右移做截断、加偏移做舍入、取模做回绕），可作为本讲「所有路径汇聚到 resize」在 Python 侧的镜像印证。
