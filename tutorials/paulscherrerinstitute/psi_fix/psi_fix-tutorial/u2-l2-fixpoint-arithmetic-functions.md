# 定点运算函数

## 1. 本讲目标

本讲紧接 [u2-l1](u2-l1-pkg-types-formats.md)。上一讲我们看清了 `psi_fix_pkg` 的「壳 + 内核」分层：它定义类型，再把运算委托给外部库 `en_cl_fix`。本讲要回答的核心问题是——**作为一个写 VHDL 的人，我到底有哪些可综合的定点运算函数可以直接调用？**

学完后你应当能够：

- 说出 `resize / add / sub / mult / abs / neg` 这一族「基本运算函数」的签名、默认 round/sat 行为，以及位增长如何体现。
- 解释 `shift_left / shift_right` 为何没有直接复用 `en_cl_fix`，而是自己重写了一份——为了在 Xilinx Vivado 下让**动态移位**可综合。
- 使用 `compare / in_range / upper_bound / lower_bound` 这族「比较与边界函数」做范围判断。
- 看懂 `tips.md` 里的 Manual Splitting（手工拆分流水）技巧，并亲手用 `psi_fix_add` + `psi_fix_resize` 组合出一条「先加法、再舍入、再饱和」的三级流水。

## 2. 前置知识

在进入源码前，先用三句话把上两讲的关键结论搬过来（细节见 [u1-l4](u1-l4-fixpoint-format-handshaking.md) 与 [u2-l1](u2-l1-pkg-types-formats.md)）：

1. **定点格式三元组** `[s, i, f]`：`s` 符号位（0 或 1）、`i` 整数位、`f` 小数位，总位宽 \(W = s + i + f\)，在源码里就是 record `psi_fix_fmt_t`。
2. **位增长三规则**：
   - 加/减法：整数位 +1（因为两个最大值相加会进位）。
   - 舍入：整数位再 +1（舍入是「加上一个舍入常数」，可能进位）。
   - 两个有符号数相乘：整数位相加后再 +1，即 \([1,a,b] \times [1,c,d] = [1, a+c+1, b+d]\)。
3. **默认值的两个世界**：`psi_fix_pkg` 里这些**库级运算函数默认 `trunc / wrap`**（最省资源、最容易溢出）；而**组件层**（如 `mov_avg`）对外暴露的 `round_g / sat_g` 默认偏安全。这个差别贯穿本讲，务必记住。

另外，本讲函数全部声明在 `psi_fix_pkg.vhd` 注释 `-- Bittrue available in Python` 那一段之下，意思是它们在 Python 侧 `model/psi_fix_pkg.py` 有一一对应的位真实现（见 [u2-l3](u2-l3-python-bittrue-model-pkg.md)）。而 `-- VHDL Only` 段下的 `to_real`、各种 `*_from_string` 则没有 Python 镜像——它们只是仿真/调试用的便利函数，不参与位真比对。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [hdl/psi_fix_pkg.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd) | 本讲主角。声明并实现全部定点运算函数（声明在包头，实现在包体）。 |
| [doc/files/tips.md](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/tips.md) | 官方技巧集。其中「Heavy Pipelining → Manual Splitting」与本讲实践任务直接相关，「Fixed-point design」一节则补充了位增长的非显然陷阱。 |
| [hdl/psi_fix_mov_avg.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd) | 综合实践的真实样板：它把 `sub → add → resize/shift_right/mult` 串成一条完整的差分-累加-增益校正流水。 |
| [hdl/psi_fix_sqrt.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_sqrt.vhd)、[hdl/psi_fix_cordic_rot.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd) | 提供 `dynamic=True` 动态移位的真实调用例，用于讲清移位函数的特殊实现。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**基本运算函数**、**移位函数**、**比较/范围/边界函数**。

### 4.1 基本运算函数

#### 4.1.1 概念说明

这一族函数是写定点 DSP 时用得最多的「积木」：

- `psi_fix_resize` —— 纯格式变换，不做算术。用来放缩位宽、改变小数位对齐、施加舍入/饱和。
- `psi_fix_add / psi_fix_sub` —— 两操作数加减，结果格式 `r_fmt` 由调用者指定。
- `psi_fix_mult` —— 定点乘法。
- `psi_fix_abs / psi_fix_neg` —— 取绝对值 / 取负（单操作数）。

它们的共同设计哲学是：**调用者必须显式声明每一个操作数和结果的格式**。函数不会「自动」猜你想要几位——这既是定点设计的纪律（位宽必须人工规划），也是 `en_cl_fix` 的 API 风格。

#### 4.1.2 核心流程

每个函数的执行流程都是同一条三步走：

1. 用 `psi_fix2_cl_fix` 把本库的 `psi_fix_fmt_t / psi_fix_rnd_t / psi_fix_sat_t` 翻译成 `en_cl_fix` 的 `FixFormat_t / FixRound_t / FixSaturate_t`（见 [u2-l1](u2-l1-pkg-types-formats.md) 的转换桥）。
2. 调用对应的 `cl_fix_*` 内核完成真正的数学运算。
3. 把 `cl_fix_*` 返回的 `std_logic_vector` 直接交回调用者。

关键点：**结果格式 `r_fmt` 完全由调用者决定**，函数本身不自动应用位增长规则。也就是说，如果你把两个 `[1,8,8]` 相加却把 `r_fmt` 也写成 `[1,8,8]`，溢出的那一个整数位就会被直接丢掉（默认 `wrap`）——这是初学者最常踩的坑。正确做法是手动让 `r_fmt` 比操作数多一个整数位。

位增长在数学上可写成：

- 加法/减法：\([s_a,i_a,f_a] \pm [s_b,i_b,f_b]\) 的「无损结果」整数位取 \(\max(i_a,i_b)+1\)，小数位取 \(\max(f_a,f_b)\)。
- 乘法（双有符号）：\([1,a,b] \times [1,c,d] = [1, a+c+1, b+d]\)。

#### 4.1.3 源码精读

先看声明。下面这一段是六个基本运算函数的签名，全部带默认参数 `rnd := psi_fix_trunc`、`sat := psi_fix_wrap`：

- [hdl/psi_fix_pkg.vhd:L92-L138](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L92-L138) —— 依次声明了 `resize / add / sub / mult / abs / neg`。注意它们都被归在注释 `-- Bittrue available in Python` 之下，因此 Python 侧有位真镜像。

以 `psi_fix_add` 为例看实现，它最典型地体现了「翻译 + 委托」：

```vhdl
function psi_fix_add(a    : std_logic_vector;
                     a_fmt : psi_fix_fmt_t;
                     b    : std_logic_vector;
                     b_fmt : psi_fix_fmt_t;
                     r_fmt : psi_fix_fmt_t;
                     rnd  : psi_fix_rnd_t := psi_fix_trunc;
                     sat  : psi_fix_sat_t := psi_fix_wrap)
return std_logic_vector is
begin
  return cl_fix_add(a, psi_fix2_cl_fix(a_fmt),
                    b, psi_fix2_cl_fix(b_fmt),
                    psi_fix2_cl_fix(r_fmt), psi_fix2_cl_fix(rnd), psi_fix2_cl_fix(sat));
end function;
```

- [hdl/psi_fix_pkg.vhd:L391-L403](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L391-L403) —— `psi_fix_add` 包体，三步走一目了然：六个 `psi_fix2_cl_fix` 把类型翻译完，直接交给 `cl_fix_add`。`sub / mult / abs` 的包体结构完全相同（[L406-L418](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L406-L418)、[L421-L433](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L421-L433)、[L436-L444](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L436-L444)）。

`psi_fix_neg` 有一个小细节值得指出——它在调用 `cl_fix_neg` 时多传了一个 `'1'`：

- [hdl/psi_fix_pkg.vhd:L447-L455](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L447-L455) —— 第三个实参 `'1'` 是 `cl_fix_neg` 的使能位（`en_cl_fix` 的取负带一个 enable 端口）。`psi_fix` 层把它固定拉高，意味着 `psi_fix_neg` 是**无条件取负**，把 en_cl_fix 那套可选使能简化掉。

> 提示：取负有一个非显然的溢出陷阱——有符号格式 `[1,0,x]` 能表示 \(-1.0\) 却不能表示 \(+1.0\)，所以对 \(-1.0\) 取负会溢出。`tips.md` 把这一点列为定点设计的常见陷阱（见 4.1.4 后的练习）。

来看真实组件如何把这几个积木串成流水。`psi_fix_mov_avg`（滑动平均）的差分-累加核心：

```vhdl
-- Stage 0: 差分 (当前样本 - 延迟样本)
v.Diff_0 := psi_fix_sub(dat_i, in_fmt_g, DataDel, in_fmt_g, DiffFmt_c, psi_fix_trunc, psi_fix_wrap);
-- Stage 1: 累加 (Running sum)
if r.Vld(0) = '1' then
  v.Sum_1 := psi_fix_add(r.Sum_1, SumFmt_c, r.Diff_0, DiffFmt_c, SumFmt_c, psi_fix_trunc, psi_fix_wrap);
end if;
```

- [hdl/psi_fix_mov_avg.vhd:L100-L106](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L100-L106) —— `sub` 算差分送入 stage 0，`add` 在 stage 1 做 running sum。注意两处都显式写了 `psi_fix_trunc, psi_fix_wrap`：中间级刻意用最省资源的量化，把 round/sat 留到流水末端。

增益校正阶段则会按模式在 `resize / shift_right / mult` 之间选择（见本讲综合实践）。

#### 4.1.4 代码实践

**实践目标**：亲手用 `psi_fix_add` + `psi_fix_resize` 实现一条「先加法、再舍入、再饱和」的三级流水，体会 `tips.md` 所说的 Manual Splitting（手工拆分）。

**背景**：`tips.md` 指出，把「加法 + 舍入 + 饱和」三件事挤在一个时钟周期里（一行 `PsiFixAdd(..., Round, Sat)`）会在高频下变差。解决办法之一就是把它们拆到三个独立流水级，每一级只做一件事，并为每一级挑选**恰好不丢信息**的中间格式。原例如下（注意：`tips.md` 用的是 4.0 之前的 camelCase 旧名 `PsiFixAdd/PsiFixRound`，当前库已全面改成 snake_case `psi_fix_add/psi_fix_round`，原理完全一致）：

- [doc/files/tips.md:L79-L105](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/tips.md#L79-L105) —— Manual Splitting 的完整说明与代码。

**操作步骤**：

1. 阅读 `tips.md` 的 Manual Splitting 小节，理解四个常量格式的来历：
   - `addFmt_c = (1, 9, 8)`：相对输入 `(1,8,8)` 整数位 +1，承载加法的位增长。
   - `rndFmt_c = (1, 10, 8)`：再 +1 整数位，承载「加舍入常数」可能带来的进位。
   - `rFmt_c = (1, 8, 0)`：最终输出格式。
2. 用当前 snake_case API 重写这三行流水（**示例代码**，非项目原有）：

   ```vhdl
   -- 示例代码：Manual Splitting 的 snake_case 版本
   constant a_fmt_c   : psi_fix_fmt_t := (1, 8, 8);
   constant b_fmt_c   : psi_fix_fmt_t := (1, 8, 8);
   constant add_fmt_c : psi_fix_fmt_t := (1, 9, 8);  -- +1 整数位：加法
   constant rnd_fmt_c : psi_fix_fmt_t := (1, 10, 8); -- +1 整数位：舍入常数进位
   constant r_fmt_c   : psi_fix_fmt_t := (1, 8, 0);
   ...
   p : process(Clk)
   begin
     if rising_edge(Clk) then
       -- 第一级：只做加法，不量化（wrap+trunc 等价于不施加量化）
       add_r <= psi_fix_add(a, a_fmt_c, b, b_fmt_c, add_fmt_c, psi_fix_trunc, psi_fix_wrap);
       -- 第二级：只做舍入（round），仍允许 wrap
       rnd_r <= psi_fix_resize(add_r, add_fmt_c, rnd_fmt_c, psi_fix_round, psi_fix_wrap);
       -- 第三级：只做饱和（sat），舍入保持 trunc（不再改变低位）
       r_r   <= psi_fix_resize(rnd_r, rnd_fmt_c, r_fmt_c, psi_fix_trunc, psi_fix_sat);
     end if;
   end process;
   ```

3. **中间格式如何选取**（关键观察）：每一级的 `r_fmt` 都要做到「这一级操作不会丢信息」——
   - 加法级：整数位必须 +1，所以 `add_fmt_c` 是 `(1,9,8)`，配合 `trunc+wrap` 实际上不施加任何量化（输入小数位已是 8，输出也是 8，整数位只增不减）。
   - 舍入级：`(1,10,8)` 多一个整数位是为了吸收「舍入时加 0.5 LSB 可能进位」的极端情况；这一级才真正开启 `round`。
   - 饱和级：把宽度压回目标 `(1,8,0)`，并在这里才开启 `sat`。

**需要观察的现象 / 预期结果**：综合后，三个操作落在三个不同的寄存器边界，关键路径里不再同时出现「加法器 + 舍入加法 + 比较器」。如果工具开启了寄存器重定时（retiming），它还能进一步把这些寄存器搬到最优位置（`tips.md` 的 Solution 1 即纯靠 retiming，Solution 2 即本实践的手工拆分，二者可叠加）。

> 本实践为源码阅读 + 改写型，不强制要求跑仿真；若要验证位真，可参考 [u3-l2](u3-l2-testbench-cosimulation.md) 的 preScript 协同仿真方法把这三个中间格式也喂给 Python 模型比对。

#### 4.1.5 小练习与答案

**练习 1**：两个 `[1,8,8]` 的数相加，若把 `r_fmt` 也写成 `(1,8,8)`，会发生什么？正确的 `r_fmt` 应该是什么？
**答案**：默认 `sat=wrap`，溢出的最高位被丢弃，结果可能错误翻转。正确的无损 `r_fmt` 是 `(1,9,8)`（整数位 +1）。

**练习 2**：`psi_fix_neg` 对输入 `-1.0`（格式 `[1,0,17]`）取负，结果会怎样？为什么？
**答案**：会溢出。因为 `[1,0,17]` 能表示 \(-1.0\) 却表示不了 \(+1.0\)（范围是 \([-1.0,\ 1.0-2^{-17}]\)）。对 \(-1.0\) 取负得到 \(+1.0\) 超出上界，在 `wrap` 下回绕成 \(-1.0\)，在 `sat` 下被钳到 \(1.0-2^{-17}\)。这正是 `tips.md` 提醒的「negation of signed numbers can lead to overflow」。

---

### 4.2 移位函数

#### 4.2.1 概念说明

`psi_fix_shift_left / psi_fix_shift_right` 表面看只是「定点数左移/右移」，但它们是本讲**唯一没有直接复用 `en_cl_fix`** 的函数——包体里那两行注释说得很直白：

> PsiFix specific implementation since cl_fix implementation is not synthesizable for dynamic shifts when using Xilinx Vivado tools

也就是说，`en_cl_fix` 自己的移位实现里含有「移位量是变量」的写法，Xilinx Vivado 综合不出来。于是 `psi_fix` 重写了一份，专门解决「移位量在运行时才知道」也能综合的问题。

为此，这两个函数比其它函数多了两个独有参数：

- `shift` —— 本次实际移多少位。
- `maxShift` —— 移位量的**上界**（设计期就要知道最多移几位）。
- `dynamic` —— 布尔，默认 `False`。`False` 表示 `shift` 是编译期常量；`True` 表示 `shift` 是运行时变量（但必须 ≤ `maxShift`）。

#### 4.2.2 核心流程

两个函数的策略一致——**先放到一个足够大的「无损中间格式」`FullFmt_c`，在那里做纯二进制移位，最后再 `resize` 回 `r_fmt`**：

1. 计算 `FullFmt_c`：它大到能装下「移位前后 + 舍入」的所有可能取值。
2. `psi_fix_resize` 把输入 `a` 放大到 `FullFmt_c`（对齐、补符号位/零）。
3. 用 `numeric_std` 的 `shift_left / shift_right` 在 `FullFmt` 内做移位。
4. `psi_fix_resize` 把移位结果量化回 `r_fmt`（这里才施加 `rnd/sat`）。

左右移的 `FullFmt_c` 不一样，这正是位增长规则的体现：

- **左移**（`shift_left`，相当于乘 \(2^{shift}\)）会让整数范围变大：
  \[\text{FullFmt} = (\max(s_a, s_r),\ \max(i_a + \text{maxShift},\ i_r),\ \max(f_a, f_r))\]
- **右移**（`shift_right`，相当于除 \(2^{shift}\)）会让小数精度变高，且舍入需要额外一位：
  \[\text{FullFmt} = (\max(s_a, s_r),\ \max(i_a, i_r),\ \max(f_a + \text{maxShift},\ f_r + 1))\]
  末尾 `+1` 对应源码注释「Additional bit for rounding」。

#### 4.2.3 源码精读

先看声明，注意 `shift / maxShift / dynamic` 三个独有参数：

- [hdl/psi_fix_pkg.vhd:L140-L158](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L140-L158) —— `shift_left` 与 `shift_right` 的签名。

`psi_fix_shift_left` 包体的核心是「`dynamic` 分叉」：

```vhdl
FullA_v := psi_fix_resize(a, a_fmt, FullFmt_c);
if not dynamic then
  FullOut_v := shift_left(FullA_v, shift);          -- shift 是常量，直接综合
else
  for i in 0 to maxShift loop                       -- 用循环把变量 shift “常数化”
    if i = shift then
      FullOut_v := shift_left(FullA_v, i);
    end if;
  end loop;
end if;
return psi_fix_resize(FullOut_v, FullFmt_c, r_fmt, rnd, sat);
```

- [hdl/psi_fix_pkg.vhd:L459-L485](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L459-L485) —— `psi_fix_shift_left` 包体。`FullFmt_c` 定义在 L468。
- [hdl/psi_fix_pkg.vhd:L489-L523](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L489-L523) —— `psi_fix_shift_right` 包体。`FullFmt_c` 定义在 L498，末尾 `+1` 即舍入保护位。

这个 `dynamic` 分叉是全讲最精妙之处，值得展开：

- `dynamic=False` 时，`shift` 是常量，`shift_left(FullA_v, shift)` 综合成一根固定连线（纯移位接线），零成本。
- `dynamic=True` 时，`shift` 是运行时变量。直接写 `shift_left(FullA_v, shift)` 会让 Vivado 看到一个变量移位量，综合失败或资源爆炸。**解法是写一个 `for i in 0 to maxShift loop` + `if i = shift then`**：循环展开后，每个 `i` 都是编译期常量，于是综合器看到的是 `maxShift+1` 个「常量移位结果」再被一个 one-hot 选择器挑出——这才是 Vivado 能消化的动态移位结构。源码注释 `-- make a loop to ensure the shift is a constant (required by the tools)` 说得很清楚（[L512](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L512)）。

另外，`shift_right` 对有符号/无符号做了区分（算术移位 vs 逻辑移位）：

```vhdl
if a_fmt.S = 1 then
  FullOut_v := shift_right(FullA_v, shift, FullA_v(FullA_v'left));  -- 算术移位：填符号位
else
  FullOut_v := shift_right(FullA_v, shift, '0');                    -- 逻辑移位：填 0
end if;
```

- 见 [L505-L510](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L505-L510)（非 dynamic 分支）与 [L511-L521](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L511-L521)（dynamic 分支）。`numeric_std` 的 `shift_right(arg, count, fill)` 第三参数决定空出来的高位填什么——有符号填符号位（`FullA_v'left`），无符号填 `'0'`。

来看真实组件如何调用动态移位。`psi_fix_sqrt` 用 `dynamic=True` 实现逐级归一化：

```vhdl
v.OutSft(stg + 1) := psi_fix_shift_right(
    r.OutSft(stg), OutFmtNorm_c,
    to_integer(r.OutCnt(stg)(2*StgIdx_v+1 downto 2*StgIdx_v)) * SftStepAfter_v,  -- 运行时算出来的移位量
    3 * SftStepAfter_v,                                                            -- maxShift
    OutFmtNorm_c, psi_fix_trunc, psi_fix_wrap, true);                             -- dynamic=True
```

- [hdl/psi_fix_sqrt.vhd:L147](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_sqrt.vhd#L147) —— 真实的 `dynamic=True` 调用。第三个实参 `to_integer(...)` 是运行时计算的移位量，所以必须开 `dynamic`。`psi_fix_cordic_rot` 的 [L97](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L97) 也是同理。

#### 4.2.4 代码实践

**实践目标**：通过对比 `dynamic=False` 与 `dynamic=True` 两种调用，理解为何移位函数要重写。

**操作步骤**：

1. 打开 [hdl/psi_fix_mov_avg.vhd:L113](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L113)。这里 `psi_fix_shift_right` 的 `shift` 实参是常量 `AdditionalBits_c`，且**没传** `dynamic`（取默认 `False`）。
2. 再打开 [hdl/psi_fix_sqrt.vhd:L147](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_sqrt.vhd#L147)，它的 `shift` 实参是 `to_integer(...)`（运行时变量），显式传了 `true`。
3. 对照包体 [L475-L483](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L475-L483)：把 `mov_avg` 的调用代入 `not dynamic` 分支（直接 `shift_left`），把 `sqrt` 的调用代入 `else` 分支（for 循环常数化）。

**需要观察的现象 / 预期结果**：

- `mov_avg` 的 ROUGH 增益校正：`shift` 是常量，综合后只是一根重新接线，零 LUT 成本。
- `sqrt` 的逐级归一化：`shift` 是变量，综合后展开成 `maxShift+1` 条常量移位支路 + 一个 one-hot MUX。**预期**：如果有人误把 `sqrt` 这处的 `dynamic` 改成 `False`（或删掉），Vivado 综合会报错或推断出巨型桶形移位器——这正好印证了源码注释里「not synthesizable for dynamic shifts」的警告。

> 本实践为源码阅读型，「待本地验证」的是综合后资源占用对比（可分别在 `dynamic` 真/假下综合 `sqrt` 观察Slice/DSP 差异）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `shift_right` 的 `FullFmt_c` 在小数部分要 `+1`，而 `shift_left` 不需要？
**答案**：右移会暴露更多小数位（精度提高），最后 `resize` 回 `r_fmt` 时往往要丢掉低位——如果调用者指定了 `round`，就需要先加舍入常数，这个加法可能进位，因此预留一个额外整数/小数保护位（源码注释写作「Additional bit for rounding」）。左移是把数放大、低位补 0，不涉及「丢低位前的舍入」，所以不需要这一位。

**练习 2**：把 `psi_fix_shift_right` 用在有符号数上，空出来的高位填什么？为什么？
**答案**：填符号位（`FullA_v(FullA_v'left)`），即算术右移。这样才能保证负数右移后仍是负数（向 \(-\infty\) 方向舍入），而不是被当成无符号数填 0 变成正数。无符号格式则填 `'0'`（逻辑右移）。

---

### 4.3 比较/范围/边界函数

#### 4.3.1 概念说明

这一族函数不做数据处理，而是**回答关于定点数的问题**，常用于控制路径（溢出检测、范围保护、门限判断）：

- `psi_fix_compare` —— 比较两个不同格式的定点数的大小/相等性，返回 `boolean`。
- `psi_fix_in_range` —— 判断 `a`（格式 `a_fmt`）在施加 `rnd` 舍入后能否无损装入 `r_fmt`，返回 `boolean`。
- `psi_fix_upper_bound_stdlv / lower_bound_stdlv` —— 返回某格式能表示的最大/最小值的位向量。
- `psi_fix_upper_bound_Real / lower_bound_Real` —— 同上，但返回 `real`（仿真/断言用）。

#### 4.3.2 核心流程

`compare` 与 `in_range` 同样走「翻译 + 委托」：

1. `psi_fix2_cl_fix` 翻译格式（`compare` 还会把比较运算符字符串原样透传给 `cl_fix_compare`）。
2. 调 `cl_fix_compare / cl_fix_in_range`，返回 `boolean`。

`compare` 支持六个运算符字符串：`"a=b"`, `"a<b"`, `"a>b"`, `"a<=b"`, `"a>=b"`, `"a!=b"`（见声明上方注释 [L178](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L178)）。注意它**不需要 `r_fmt`**——比较本身不产生数值结果，`en_cl_fix` 内部会先把两数对齐到公共格式再比。

#### 4.3.3 源码精读

声明（归在 `-- Bittrue available in Python` 段，故 Python 侧也有镜像）：

- [hdl/psi_fix_pkg.vhd:L160-L183](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L160-L183) —— 四个 bound 函数 + `in_range` + `compare` 的签名。

包体：

- [hdl/psi_fix_pkg.vhd:L526-L551](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L526-L551) —— 四个 bound 函数，分别委托 `cl_fix_max_value / cl_fix_min_value / cl_fix_max_real / cl_fix_min_real`。
- [hdl/psi_fix_pkg.vhd:L554-L561](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L554-L561) —— `psi_fix_in_range` 包体，委托 `cl_fix_in_range`，默认 `rnd=trunc`。
- [hdl/psi_fix_pkg.vhd:L591-L598](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L591-L598) —— `psi_fix_compare` 包体，把比较字符串和两个数（带各自格式）原样交给 `cl_fix_compare`。

一个典型用法（**示例代码**）——判断累加结果是否会溢出目标格式，从而决定是否需要饱和：

```vhdl
-- 示例代码：用 in_range 做溢出预判
if psi_fix_in_range(accu, AccuFmt_c, OutFmt_c, psi_fix_round) then
  dat_o <= psi_fix_resize(accu, AccuFmt_c, OutFmt_c, psi_fix_round, psi_fix_wrap);  -- 安全，不必饱和
else
  dat_o <= psi_fix_resize(accu, AccuFmt_c, OutFmt_c, psi_fix_round, psi_fix_sat);   -- 会溢出，施加饱和
end if;
```

bound 函数则常用于生成测试激励或常量，例如用 `psi_fix_upper_bound_Real` 拿到某格式的数学最大值去构造「最坏情况」输入。

#### 4.3.4 代码实践

**实践目标**：用 `psi_fix_compare` 与 bound 函数，验证本讲 4.1.5 练习 2 中「\(-1.0\) 取负溢出」的结论。

**操作步骤**：

1. 设 `neg_fmt = (1, 0, 17)`。用 `psi_fix_from_real(-1.0, neg_fmt)` 得到位向量 `neg_one`。
2. 对它取负：`res := psi_fix_neg(neg_one, neg_fmt, neg_fmt, psi_fix_round, psi_fix_sat);`
3. 用 `psi_fix_compare("a=b", res, neg_fmt, psi_fix_from_real(1.0, neg_fmt), neg_fmt)` 比较——注意 `(1,0,17)` 装不下 `+1.0`，`from_real(1.0,...)` 会被饱和钳到上界。
4. 改用 `psi_fix_upper_bound_Real(neg_fmt)` 打印该格式真正的数学上界。

**需要观察的现象 / 预期结果**：`compare` 会显示 `res`（`-1.0` 取负、`sat` 下钳到上界）等于上界 \(1 - 2^{-17}\)，而不等于 `1.0`；`upper_bound_Real` 返回的也正是 \(1 - 2^{-17}\)。从而亲眼看到「\(-1.0\) 取负本应得 \(+1.0\)，但格式装不下」这个陷阱。

> 本实践为源码阅读 + 仿真型，可在任意 VHDL 测试台顶层进程里跑断言；若无仿真器，结论可由数学推导直接得出（见练习答案）。

#### 4.3.5 小练习与答案

**练习 1**：`psi_fix_in_range(a, a_fmt, r_fmt)` 返回 `false` 意味着什么？调用者通常据此做什么决策？
**答案**：意味着把 `a` 从 `a_fmt` 装入 `r_fmt`（施加给定 `rnd`）后**会**丢信息（溢出或大幅截断）。调用者通常据此切换到 `sat` 模式，或上报溢出标志。

**练习 2**：`psi_fix_compare("a>b", x, x_fmt, y, y_fmt)` 中 `x_fmt` 和 `y_fmt` 可以不同吗？比较是如何进行的？
**答案**：可以不同。`en_cl_fix` 内部会把两个数对齐到一个公共格式（按符号、整数位、小数位的最大值对齐）再做比较，所以比较的是两个定点数的**数学值**而非原始位模式。这也是 `compare` 不需要 `r_fmt` 的原因。

---

## 5. 综合实践

把本讲三个模块串起来，分析 `psi_fix_mov_avg` 的**三种增益校正模式**如何分别选用 `resize / shift_right / mult`——这是一个真实的「资源 vs 精度」权衡案例。

**任务**：阅读 [hdl/psi_fix_mov_avg.vhd:L108-L123](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L108-L123)，回答下列问题并把结论整理成一张表。

```vhdl
-- Stage 2/3: 三种增益校正
if gain_corr_g = "NONE" then
  CalcOut_v := psi_fix_resize(r.Sum_1, SumFmt_c, out_fmt_g, round_g, sat_g);
elsif gain_corr_g = "ROUGH" then
  CalcOut_v := psi_fix_shift_right(r.Sum_1, SumFmt_c, AdditionalBits_c, AdditionalBits_c, out_fmt_g, round_g, sat_g);
else  -- "EXACT"
  v.RoughCorr_2 := psi_fix_shift_right(r.Sum_1, SumFmt_c, AdditionalBits_c, AdditionalBits_c, GcInFmt_c, psi_fix_trunc, psi_fix_wrap);
end if;
-- Stage 3 (EXACT)
if gain_corr_g = "EXACT" then
  CalcOut_v := psi_fix_mult(r.RoughCorr_2, GcInFmt_c, Gc_c, GcCoefFmt_c, out_fmt_g, round_g, sat_g);
end if;
```

请思考并填写：

| 模式 | 使用的函数 | 是否用乘法器 | 增益校正精度 | 适用场景 |
| --- | --- | --- | --- | --- |
| `NONE` | `psi_fix_resize` | 否 | 无校正（直接取窗口平均，不补偿 \(1/N\)） | 对绝对增益不敏感 |
| `ROUGH` | `psi_fix_shift_right` | 否 | 近似（移位只能补偿 2 的幂） | 系数恰为 2 的幂时零成本 |
| `EXACT` | `psi_fix_shift_right` + `psi_fix_mult` | 是 | 精确（先粗移位再乘以补偿系数 `Gc_c`） | 需要精确增益 |

**进一步追问**：

1. 为什么 `EXACT` 模式里**先**做一次 `shift_right`（`trunc/wrap`，[L116](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L116)）再 `mult`（[L121](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L121)），而不是直接一次 `mult`？
   **提示**：先把累加和右移到接近目标量级，缩小送入乘法器的位宽，从而省乘法器位宽/资源；移位是零成本接线，乘法才是昂贵资源。
2. 三处末端量化都把 `round_g / sat_g`（组件层默认偏安全）传给了 `resize / shift_right / mult`，而中间级（stage 0/1、EXACT 的第一次移位）一律用 `trunc / wrap`。这与本讲反复强调的「**中间级用 trunc/wrap 省资源，末端才 round/sat**」是否一致？

> 这个综合实践同时覆盖了本讲的三个模块：`resize`（基本运算）、`shift_right`（移位，且 `mov_avg` 这里用 `dynamic=False` 常量移位）、`mult`（基本运算），并自然衔接 [u4-l1](u4-l1-resize-pipe-mov-avg.md) 对 `mov_avg` 的完整剖析。

## 6. 本讲小结

- `resize / add / sub / mult / abs / neg` 一族基本运算函数全部走「`psi_fix2_cl_fix` 翻译 → `cl_fix_*` 委托」三步走，**结果格式 `r_fmt` 完全由调用者指定**，函数不自动应用位增长。
- 这些库级函数**默认 `trunc / wrap`**（最省资源、最易溢出），与组件层偏安全的默认值相反；位增长须由人工按「加法整数位 +1、舍入再 +1、双有符号乘法整数位相加再 +1」规划。
- `shift_left / shift_right` 是**唯一不直接复用 `en_cl_fix`** 的函数，因为 `en_cl_fix` 的动态移位在 Vivado 下不可综合；`psi_fix` 用「先放大到无损 `FullFmt_c` → 移位 → `resize` 回去」重写，并用 `for i in 0 to maxShift` 循环把运行时移位量「常数化」实现 `dynamic=True`。
- `compare / in_range / upper_bound / lower_bound` 是控制路径用的判断函数，比较时按数学值对齐，不产生数值结果故无需 `r_fmt`。
- 本讲所有函数都归在 `-- Bittrue available in Python` 段下，在 Python 侧有逐位一致的镜像（见 [u2-l3](u2-l3-python-bittrue-model-pkg.md)），这是后续位真协同仿真的前提。
- `tips.md` 的 Manual Splitting 技巧把「加法 + 舍入 + 饱和」拆成三级、每级只做一件事并选恰好的中间格式，是高频设计里把本讲函数组合成流水线的标准范式。

## 7. 下一步学习建议

- **接着读 Python 侧**：进入 [u2-l3 Python 位真模型包](u2-l3-python-bittrue-model-pkg.md)，看 `model/psi_fix_pkg.py` 如何把本讲的 `add / sub / mult / shift_*` 一一镜像，以及它特有的 53 位双精度精度限制。
- **看测试台如何用这些函数**：跳到 [u3-l2 测试台与协同仿真](u3-l2-testbench-cosimulation.md)，理解 `psi_fix_from_real / psi_fix_get_bits_as_int`（上一讲）如何与本讲的运算函数配合，生成位真参考文本。
- **看一个完整组件**：直接读 [u4-l1 resize_pipe 与 moving average](u4-l1-resize-pipe-mov-avg.md)，那里会把本讲的 `sub → add → resize/shift_right/mult` 在 `mov_avg` 里的完整数据流讲透。
- **延伸阅读**：`doc/files/tips.md` 的「Fixed-point design」一节（[L156-L194](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/tips.md#L156-L194)）补充了「舍入本身会溢出」「按理论最大值而非格式来省位」等进阶位增长陷阱，值得在本讲之后细读。
