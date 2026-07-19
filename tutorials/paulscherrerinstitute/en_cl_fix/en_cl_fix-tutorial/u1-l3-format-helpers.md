# 格式查询、边界值与字符串转换函数

## 1. 本讲目标

上一讲（u1-l2）我们搞清楚了「定点格式是什么」——一个 `(Signed, IntBits, FracBits)` 三元组决定了位串如何被解释成实数。本讲继续围绕这个格式对象，学一组**只读**的工具函数：给定一个格式，我们能从中提取出哪些有用的信息？

学完本讲后，你应该能够：

- 用 `cl_fix_width` 计算任意格式（含负整数位、负小数位）的总位宽，并说清它的实现公式 `toInteger(Signed) + IntBits + FracBits`。
- 说清楚 `cl_fix_zero_value / max_value / min_value` 这三个「边界值」函数返回什么，以及它们在不同语言里有一个**非常容易踩坑的差异**：VHDL 返回的是**位串（bit pattern）**，而 Python / MATLAB 返回的是**实数值**。
- 掌握实数范围的闭式公式：最大值 \( V_{\max} = 2^I - 2^{-F} \)，有符号最小值 \( V_{\min} = -2^I \)，并理解二进制补码「负数能多走一步」的不对称性。
- 知道 VHDL 额外提供了 `cl_fix_max_real / min_real`，而 Python / MATLAB 没有这组函数——背后的原因是上一条的差异。
- 用 `cl_fix_string_from_format` 把格式序列化成形如 `(true,3,2)` 的字符串，并意识到三套实现的字符串输出**并不完全一致**。

本讲**只查询格式、不改变数值**，依然不涉及舍入（u1-l4）、饱和（u1-l5）和真正的数值转换（u3）。这些函数是后续所有讲义里用来声明信号宽度、判断是否溢出、打印调试信息的基础工具。

## 2. 前置知识

在进入源码前，先用最朴素的方式把几个概念串起来。

**位宽（width）。** 一个格式到底要占几根线（几个 bit）？上一讲已经给过公式：符号位（0 或 1 个）加上整数位，再加上小数位。当整数位或小数位是负数时，位宽会相应**缩小**——负的位数相当于「这一段不存在」。所以位宽公式对正位、零位、负位一视同仁，直接相加即可。

**边界值（boundary value）。** 给定一个格式，它「最远能走到哪里」？每个格式都有三个标志性的值：

- **零值**：全 0 位串，永远就是 0。
- **最大值**：能表示的最大的实数。注意有符号格式的最大值最高位（符号位）必须是 `0`，所以比「全 1」要小一截。
- **最小值**：能表示的最小的实数。有符号格式的最小值是「符号位为 1、其余全 0」——也就是二进制补码里那个「最负」的数，它在绝对值上比最大值还大一个最小步长（LSB）。

**二进制补码的不对称性。** 这是一个反复出现的关键直觉。以 4 位有符号数为例，范围是 `−8 … +7`：负数能精确走到 `−8`，正数只能走到 `+7`。原因就是 `1000` 解释成补码是 `−8`，而 `0111` 才是 `+7`。本讲的 `max_value / min_value` 公式正是这种不对称的直接体现。

**「位串」与「实数」是两件事。** 同一个边界值，既可以表达成一串 0/1（给硬件信号赋值用），也可以表达成一个实数（在 MATLAB / Python 里做算法评估用）。VHDL 作为硬件描述语言更贴近位串，Python / MATLAB 作为算法语言更贴近实数——这正是本讲跨语言差异的根源。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [vhdl/src/en_cl_fix_pkg.vhd](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd) | VHDL 实现。本讲涉及的 `cl_fix_width`、`cl_fix_string_from_format`、`cl_fix_zero_value/max_value/min_value`、`cl_fix_max_real/min_real` 全部集中在此文件的中后段（约 1320–1431 行）。 |
| [python/src/en_cl_fix_pkg/en_cl_fix_pkg.py](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py) | Python 实现。本讲函数集中在文件开头（20–56 行），多数是薄薄的封装。 |
| [python/src/en_cl_fix_pkg/en_cl_fix_types.py](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_types.py) | Python 的 `FixFormat` 类，`width()` 与 `__str__`（字符串化的真正逻辑）定义在这里。 |
| [matlab/src/cl_fix_width.m](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_width.m) | MATLAB 的位宽函数，一个 `.m` 文件对应一个函数。 |
| [matlab/src/cl_fix_max_value.m](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_max_value.m)、[cl_fix_min_value.m](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_min_value.m)、[cl_fix_zero_value.m](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_zero_value.m) | MATLAB 的边界值函数。 |
| [matlab/src/cl_fix_string_from_format.m](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_string_from_format.m) | MATLAB 的格式序列化函数。 |

> 跨语言总览（本讲的「一图流」，下面会逐项展开）：
>
> | 函数 | VHDL 返回 | Python 返回 | MATLAB 返回 |
> |------|-----------|-------------|-------------|
> | `cl_fix_width` | 整数 `positive` | `int` | 标量 `double` |
> | `cl_fix_zero_value` | 全 0 **位串** | ✗ 不存在 | 实数 `0` |
> | `cl_fix_max_value` | **位串** | **实数** | **实数** |
> | `cl_fix_min_value` | **位串** | **实数** | **实数** |
> | `cl_fix_max_real` | 实数 | ✗ 不存在 | ✗ 不存在 |
> | `cl_fix_min_real` | 实数 | ✗ 不存在 | ✗ 不存在 |
> | `cl_fix_string_from_format` | `"(true,3,2)"` | `"(True, 3, 2)"` | `"(true,3,2)"` |
>
> 记住这张表，本讲的全部「坑」都在其中。

## 4. 核心概念与源码讲解

### 4.1 位宽查询 cl_fix_width

#### 4.1.1 概念说明

`cl_fix_width` 是整个库里被调用次数最多的函数之一——只要你想声明一个能放下某格式数值的信号/变量，就需要先用它算出位宽。例如 VHDL 里几乎每一处信号声明都会写成 `std_logic_vector(cl_fix_width(Fmt)-1 downto 0)`。

它的语义极简：**给定格式，返回它需要多少 bit**。难点不在语义，而在三套实现各自怎么处理那条硬约束 `IntBits + FracBits >= 1`（上一讲讲过的「避免退化格式」约束）。

#### 4.1.2 核心流程

位宽的计算公式只有一个：

\[
W = \text{toInteger}(\textit{Signed}) + \textit{IntBits} + \textit{FracBits}
\]

其中 `toInteger(Signed)` 把布尔转成 `0/1`：无符号格式贡献 0 位、有符号格式贡献 1 个符号位。由于 `IntBits` 和 `FracBits` 都可能是负数，三项直接相加就能自动处理负位的情况，无需特判。

三种语言对约束 `IntBits + FracBits >= 1` 的处理方式不同（承接 u1-l2 已埋下的伏笔）：

- **VHDL**：在函数体内用 `assert ... severity failure` 检查，违反时仿真直接终止。
- **MATLAB**：在函数体内用 `error(...)` 检查，并且**还多了一条 `<= 52` 的上限**。
- **Python**：完全**不检查**，直接返回可能退化的位宽。

#### 4.1.3 源码精读

先看 VHDL 实现。`FixFormat_t` 这个 record 类型定义在 [en_cl_fix_pkg.vhd:153-157](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L153-L157)，三个字段就是三元组：

```vhdl
type FixFormat_t is record
    Signed   : boolean;
    IntBits  : integer; -- can be negative, IntBits+FracBits must be at least 1.
    FracBits : integer; -- can be negative, IntBits+FracBits must be at least 1.
end record;
```

`cl_fix_width` 的实现只有几行，见 [en_cl_fix_pkg.vhd:1321-1329](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1321-L1329)：

```vhdl
function cl_fix_width ( fmt : FixFormat_t) return positive is
begin
    assert (fmt.IntBits+fmt.FracBits) > 0
        report "cl_fix_width : The sum of 'IntBits' and 'FracBits' must be at least 1!"
        severity failure;
    return toInteger(fmt.Signed)+fmt.IntBits+fmt.FracBits;
end;
```

注意 `return` 那一行：`toInteger(fmt.Signed)` 把布尔转成 0/1（实现在 [en_cl_fix_pkg.vhd:1058-1066](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1058-L1066)，就是 `if bool then return 1; else return 0;`），再加上两个可能为负的整数。`assert ... severity failure` 表示一旦约束被违反，仿真器会立即报错停止。

Python 实现是一层薄封装，见 [en_cl_fix_pkg.py:20-21](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L20-L21)：

```python
def cl_fix_width(fmt : FixFormat) -> int:
    return fmt.width()
```

真正干活的是 `FixFormat.width()`，定义在 [en_cl_fix_types.py:55-56](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_types.py#L55-L56)：

```python
def width(self):
    return int(self.Signed) + self.IntBits + self.FracBits
```

`int(True)` 就是 1、`int(False)` 就是 0，与 VHDL 的 `toInteger` 完全对应。注意这里**没有任何 assert**——这就是 u1-l2 提到的「Python 漏检」：传一个退化格式进来，它会静默返回一个可能为 0 甚至负的位宽。

MATLAB 实现在 [cl_fix_width.m:16-25](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_width.m#L16-L25)：

```matlab
function bits = cl_fix_width (fmt)
if (fmt.IntBits+fmt.FracBits) < 1
    error ('cl_fix_width : "IntBits"+"FracBits" must be at least 1!');
end
if (fmt.IntBits+fmt.FracBits) > 52
    error ('cl_fix_width : "IntBits"+"FracBits" must be at most 52!');
end
bits = fmt.Signed + fmt.IntBits + fmt.FracBits;
```

公式 `fmt.Signed + fmt.IntBits + fmt.FracBits` 与另两种语言一致（MATLAB 里 `true` 参与算术运算时就是 1）。这里多出的 `> 52` 上限值得留意：它源自 MATLAB 用双精度浮点（double）在内部存放定点值，而 IEEE754 双精度只有 52 位尾数——这正好对应 u1-l1 提到的「1.2.0 才在 Python 中加入 >53 位支持」，**MATLAB 至今受这条 52 位限制约束**。

#### 4.1.4 代码实践

这是一个「读测试 + 自己验证」的实践。

1. **实践目标**：确认三种语言的位宽公式一致，并亲眼看到负位如何缩小位宽。
2. **操作步骤**：
   - 打开 Python 测试 [en_cl_fix_pkg_test.py:17-38](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/unittest/en_cl_fix_pkg_test.py#L17-L38)，阅读 `cl_fix_width_Test` 这一组用例，特别注意 `test_NegativeInt` 和 `test_NegativeFract`。
   - 手算 `FixFormat(True, -2, 3)` 与 `FixFormat(True, 3, -2)` 的位宽：前者 `1 + (-2) + 3 = 2`，后者 `1 + 3 + (-2) = 2`，两个都应是 2。
   - 在 `python/unittest` 目录运行 `python3 en_cl_fix_pkg_test.py`，确认这两个用例通过。
3. **需要观察的现象**：测试输出 `OK`（或全部用例通过），证明负位的位宽计算与公式吻合。
4. **预期结果**：两个负位用例都返回位宽 2，与测试断言 `assertEqual(2, ...)` 一致。

#### 4.1.5 小练习与答案

**练习 1**：计算 `FixFormat(False, 4, -2)` 的位宽。

**参考答案**：`toInteger(False) + 4 + (-2) = 0 + 4 - 2 = 2`。位宽为 2。负的小数位意味着这个格式的步长是 \(2^{2} = 4 \)（粗粒度），只能表示 4 的倍数。

**练习 2**：如果把 `FixFormat(True, 0, 0)` 分别传给三套实现的 `cl_fix_width`，会发生什么？

**参考答案**：`IntBits+FracBits = 0`，违反硬约束。VHDL 会触发 `assert ... severity failure` 仿真终止；MATLAB 抛出 `error`；Python **不报错**，返回 `1 + 0 + 0 = 1`（静默给出一个退化结果）。这正是三语言行为不一致的典型例子。

---

### 4.2 边界值函数：zero_value / max_value / min_value

#### 4.2.1 概念说明

这一组函数回答三个问题：给定一个格式，它的零、最大、最小分别是什么？它们在「初始化信号」「判断是否溢出」「饱和到边界」等场景里都会用到。

这里有一个**全讲义最重要的跨语言差异**，务必先记住：

- 在 **VHDL** 里，`cl_fix_zero_value / max_value / min_value` 返回的是 **`std_logic_vector` 位串**——也就是一串 0/1，可以直接赋给硬件信号。
- 在 **Python / MATLAB** 里，同名函数返回的是 **实数值**（如 `7.75`、`-8.0`）——因为这两个语言内部用浮点数表示定点值，返回位串没有意义。

换句话说，**VHDL 的 `max_value`（位串）和 Python 的 `max_value`（实数）同名却不同类**。要拿到「实数形式的最大值」，VHDL 需要另用 `cl_fix_max_real`（见 4.3）；而 Python / MATLAB 的 `max_value` 本身就已经是实数了，所以它们**没有** `max_real` 这个函数。这是上一节总览表里那两个「✗ 不存在」的来源。

#### 4.2.2 核心流程

先看实数值的闭式公式（Python / MATLAB 的 `max_value / min_value`、VHDL 的 `max_real / min_real` 都遵循它）：

\[
V_{\max} = 2^{I} - 2^{-F}
\]

\[
V_{\min} =
\begin{cases}
-2^{I} & \text{有符号} \\
0 & \text{无符号}
\end{cases}
\]

零值恒为 0。

注意 \( V_{\max} \) 公式里那个 `− 2^{−F}`：最大值不是整齐的 \( 2^{I} \)，而是差一个最小步长（LSB，即 \( 2^{-F} \)）。这正是补码不对称的体现——负方向能精确走到 \( -2^{I} \)，正方向只能走到 \( 2^{I} - 2^{-F} \)。

再看 VHDL **位串**版本的构造逻辑：

- **zero_value**：全 `'0'`。
- **max_value**：先全 `'1'`；若为有符号格式，再把最高位（符号位）改回 `'0'`——所以有符号最大值是 `011...1`，无符号最大值是 `111...1`。
- **min_value**：若为有符号格式，全 `'0'` 再把最高位（最左位）置 `'1'`，得到 `100...0`（补码里最负的数）；若为无符号格式，就是全 `'0'`（即 0）。

把上述位串按格式解释回实数，正好等于上面的闭式公式——位串版本与实数版本描述的是同一个边界值，只是表达形式不同。

#### 4.2.3 源码精读

VHDL 的三个位串函数紧紧相邻，先看 `cl_fix_zero_value`，[en_cl_fix_pkg.vhd:1372-1378](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1372-L1378)：

```vhdl
function cl_fix_zero_value ( fmt : FixFormat_t) return std_logic_vector is
    variable result_v : std_logic_vector (cl_fix_width (fmt)-1 downto 0);
begin
    result_v := (others => '0');
    return result_v;
end;
```

返回值类型是 `std_logic_vector`，宽度正是 `cl_fix_width(fmt)`，内容全 0。

再看 `cl_fix_max_value`，[en_cl_fix_pkg.vhd:1382-1391](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1382-L1391)：

```vhdl
function cl_fix_max_value ( fmt : FixFormat_t) return std_logic_vector is
    variable result_v : std_logic_vector (cl_fix_width (fmt)-1 downto 0);
begin
    result_v := (others => '1');
    if fmt.Signed then
        result_v (result_v'high) := '0';
    end if;
    return result_v;
end;
```

关键就是 `if fmt.Signed then result_v(result_v'high) := '0';`：有符号时把最高位清零，得到 `011...1`。`result_v'high` 是这个 `downto 0` 向量的最左下标，也就是 MSB（符号位）。

然后是 `cl_fix_min_value`，[en_cl_fix_pkg.vhd:1395-1406](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1395-L1406)：

```vhdl
function cl_fix_min_value ( fmt : FixFormat_t) return std_logic_vector is
    variable result_v : std_logic_vector (cl_fix_width (fmt)-1 downto 0);
begin
    if fmt.Signed then
        result_v := (others => '0');
        result_v(result_v'left) := '1';
    else
        result_v := (others => '0');
    end if;
    return result_v;
end;
```

有符号时得到 `100...0`（`result_v'left` 与 `'high` 在这里指向同一位，即 MSB），无符号时全 0。

对比 Python 的同名函数，[en_cl_fix_pkg.py:43-56](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L43-L56)：

```python
def cl_fix_max_value(rFmt : FixFormat):
    if cl_fix_is_wide(rFmt):
        return wide_fxp.MaxValue(rFmt)
    else:
        return 2.0**rFmt.IntBits-2.0**(-rFmt.FracBits)

def cl_fix_min_value(rFmt : FixFormat):
    if cl_fix_is_wide(rFmt):
        return wide_fxp.MinValue(rFmt)
    else:
        if rFmt.Signed:
            return -2.0**rFmt.IntBits
        else:
            return 0.0
```

注意两件事：第一，返回的是**浮点实数**，不是位串；第二，普通位宽走 `2.0**IntBits - 2.0**(-FracBits)` 这条公式（与 4.2.2 完全一致），而**超过 53 位**的「wide」格式会派发给 `wide_fxp.MaxValue/MinValue`（大位宽话题在 u6 详讲，本讲只要知道有这么一个分支即可）。Python **没有** `cl_fix_zero_value`，因为 0 就是 0，没必要单独包装。

MATLAB 的实现同样返回实数，且极其简短。`cl_fix_max_value` 见 [cl_fix_max_value.m:15-17](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_max_value.m#L15-L17)：

```matlab
function result = cl_fix_max_value (fmt)
result = 2^fmt.IntBits-2^-fmt.FracBits;
```

`cl_fix_min_value` 见 [cl_fix_min_value.m:15-21](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_min_value.m#L15-L21)，对有符号返回 `-2^IntBits`、无符号返回 `0`。`cl_fix_zero_value` 见 [cl_fix_zero_value.m:16-18](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_zero_value.m#L16-L18)，直接 `result = 0;`。三套实数公式逐字符对应。

Python 测试用例正好把这套差异钉死了，见 [en_cl_fix_pkg_test.py:627-641](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/unittest/en_cl_fix_pkg_test.py#L627-L641)：

```python
def test_Unsigned(self):
    self.assertEqual(3.75, cl_fix_max_value(FixFormat(False,2,2)))
def test_Signed(self):
    self.assertEqual(1.75, cl_fix_max_value(FixFormat(True, 1, 2)))
...
def test_Signed(self):
    self.assertEqual(-2.0, cl_fix_min_value(FixFormat(True, 1, 2)))
```

`(false,2,2)` 的最大值是 `2^2 - 2^-2 = 4 - 0.25 = 3.75`；`(true,1,2)` 的最大值是 `2 - 0.25 = 1.75`、最小值是 `-2^1 = -2`。这些断言同时验证了公式，也再次说明 Python 的 `max_value` 返回的是实数。

#### 4.2.4 代码实践

1. **实践目标**：用具体格式验证「位串版本」与「实数版本」描述同一个边界值。
2. **操作步骤**：
   - 取格式 `(true,3,2)`，手算：位宽 `1+3+2 = 6`；实数最大值 `2^3 - 2^-2 = 7.75`；实数最小值 `-2^3 = -8`。
   - 推导 VHDL 位串：`max_value` 为 `011111`（最高位清零），按 6 位补码解释是 `+31`，再乘步长 `2^-2 = 0.25` 得 `7.75`；`min_value` 为 `100000`，6 位补码是 `-32`，乘 `0.25` 得 `-8`。两者与实数公式吻合。
   - 在 `python/unittest` 目录运行 `python3 en_cl_fix_pkg_test.py`，确认 `cl_fix_max_value_Test` / `cl_fix_min_value_Test` 通过。
3. **需要观察的现象**：位串解释后的实数（`7.75`、`-8`）与 Python/MATLAB 实数函数的返回值完全相等。
4. **预期结果**：`(true,3,2)` 的 `max_value = 7.75`、`min_value = -8.0`；`(false,3,2)` 的 `max_value = 7.75`、`min_value = 0.0`。

#### 4.2.5 小练习与答案

**练习 1**：为什么有符号格式的 `min_value` 在绝对值上比 `max_value` 大一个 LSB？

**参考答案**：因为二进制补码的最负值 `100...0` 的绝对值是 \( 2^{I} \)，而最正值 `011...1` 只到 \( 2^{I} - 2^{-F} \)，两者差一个最小步长 \( 2^{-F} \)。这就是 \( V_{\min} = -2^{I} \)、\( V_{\max} = 2^{I} - 2^{-F} \) 的几何含义。

**练习 2**：在 Python 里调用 `cl_fix_max_value(FixFormat(True,3,2))` 与在 VHDL 里调用 `cl_fix_max_value((true,3,2))`，得到的「东西」有什么本质区别？

**参考答案**：Python 返回浮点实数 `7.75`；VHDL 返回 6 位的 `std_logic_vector` 位串 `"011111"`。它们描述同一个边界值，但一个是算法层的实数、一个是硬件层的位串——这就是本节反复强调的跨语言差异。

---

### 4.3 实数范围函数：max_real / min_real（VHDL 独有）

#### 4.3.1 概念说明

上一节已经埋下伏笔：VHDL 的 `max_value / min_value` 返回位串，那么如果 VHDL 代码里想拿到「实数形式」的范围（比如在 `real` 域判断一个输入会不会溢出、或做 `cl_fix_from_real` 的饱和夹紧），就需要另一组函数——这就是 `cl_fix_max_real` 和 `cl_fix_min_real`。它们**只在 VHDL 中存在**，Python / MATLAB 没有对应函数，因为后两者的 `max_value / min_value` 本身就已经返回实数了。

事实上，你会在 u3 看到 `cl_fix_from_real` 的饱和逻辑正是用 `cl_fix_max_real / min_real` 来夹紧输入的（见 [en_cl_fix_pkg.vhd:1662-1665](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1662-L1665) 的那段 `if a > cl_fix_max_real(...) then ... elsif a < cl_fix_min_real(...)`）。

#### 4.3.2 核心流程

公式与 4.2.2 完全相同，只是这里强调「实数返回」：

\[
V_{\max}^{\text{real}} = 2^{I} - 2^{-F}, \qquad
V_{\min}^{\text{real}} =
\begin{cases}
-2^{I} & \text{有符号} \\
0 & \text{无符号}
\end{cases}
\]

VHDL 里用 `real` 类型的 `2.0**IntBits` 来计算（注意是浮点 `2.0` 而非整数 `2`，因为 VHDL 的 `**` 对 `integer` 指数为负时需要 `real` 底数）。

#### 4.3.3 源码精读

`cl_fix_max_real` 见 [en_cl_fix_pkg.vhd:1410-1417](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1410-L1417)：

```vhdl
function cl_fix_max_real( fmt : FixFormat_t) return real is
    variable Range_v, Lsb_v : real;
begin
    Range_v := 2.0**fmt.IntBits;
    Lsb_v   := 2.0**(-fmt.FracBits);
    return Range_v-Lsb_v;
end function;
```

`Range_v = 2^I` 是「上界开区间端点」，`Lsb_v = 2^{-F}` 是最小步长，最大值就是两者之差。`cl_fix_min_real` 见 [en_cl_fix_pkg.vhd:1421-1431](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1421-L1431)：有符号返回 `-2.0**IntBits`、无符号返回 `0.0`。

把这两段与 4.2.3 里 Python 的 `cl_fix_max_value / min_value`（普通位宽分支）逐行对照，你会发现**公式完全相同**。这就从源码层面印证了本节开头那句话：VHDL 的 `max_real` ≡ Python/MATLAB 的 `max_value`，二者只是「住在不同的函数名里」。

> 小历史：Changelog 里 [1.1.2](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/Changelog.md) 记录过「Fixed bug in cl_fix_max_real that led to problems with GHDL」——说明这组函数在第三方仿真器 GHDL 上曾经出过 bug，是库演进中真实被反复打磨过的角落。

#### 4.3.4 代码实践

1. **实践目标**：把 VHDL 的 `max_real/min_real` 与 Python 的 `max_value/min_value` 对照，确认它们是同一组公式。
2. **操作步骤**：
   - 在 Python 中对 `FixFormat(True,3,2)` 调用 `cl_fix_max_value` 与 `cl_fix_min_value`，记录结果（应为 `7.75` 与 `-8.0`）。
   - 打开 VHDL 源码 [en_cl_fix_pkg.vhd:1410-1431](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1410-L1431)，把 `2.0**3 - 2.0**(-2)` 与 `-2.0**3` 代入手算，得到 `7.75` 与 `-8.0`。
3. **需要观察的现象**：两侧结果逐位一致。
4. **预期结果**：VHDL `max_real((true,3,2)) = 7.75`、`min_real((true,3,2)) = -8.0`，与 Python `max_value/min_value` 相同；这也说明 `cl_fix_from_real` 用 `max_real/min_real` 夹紧输入是正确的。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Python 不需要 `cl_fix_max_real`？

**参考答案**：因为 Python 的 `cl_fix_max_value` 已经返回实数（`2.0**IntBits - 2.0**(-FracBits)`），没有「位串 vs 实数」的二分。`max_real` 是 VHDL 为了在 `real` 域拿到范围而专门提供的，在 Python/MATLAB 里属于冗余。

**练习 2**：格式 `(true,4,-2)` 的 `max_real` 是多少？它的「步长」有多大？

**参考答案**：\( V_{\max} = 2^{4} - 2^{-(-2)} = 16 - 2^{2} = 16 - 4 = 12 \)。负小数位使步长变成 \( 2^{2} = 4 \)，所以这个格式只能表示 4 的倍数（…, -4, 0, 4, 8, 12），最大到 12。这与 README 真值表里 `(true,4,-2)` 范围 `-16 … 12` 一致。

---

### 4.4 字符串转换 cl_fix_string_from_format

#### 4.4.1 概念说明

`cl_fix_string_from_format` 把一个格式序列化成可读字符串，形如 `(true,3,2)`。它的典型用途是**调试打印**和**跨语言/跨工具传递格式**——比如配合 `en_cl_bittrue` 库做位真数据交换时，把格式写进文件头。它还有一个反向函数 `cl_fix_format_from_string`（把字符串解析回格式），后者是 u7-l3「字符串解析与 generic 传参」的主角，本讲只点一下它的存在。

和前几节一样，这里也藏着一个跨语言小坑：**三套实现输出的字符串并不完全相同**。

#### 4.4.2 核心流程

序列化规则：把三元组按 `(Signed, IntBits, FracBits)` 顺序拼到一对圆括号里。差异在于布尔和整数的「文本表示」：

- **VHDL**：用 `boolean'image` 得到全小写 `true`/`false`、用 `integer'image` 得到整数，**无空格**：`(true,3,2)`。
- **MATLAB**：手动拼字符串，布尔写成全小写 `true`/`false`、`sprintf('%i',...)` 写整数，**无空格**：`(true,3,2)`。
- **Python**：直接 `str(fmt)`，而 Python 的布尔打印成首字母大写的 `True`/`False`、且 f-string 默认元素间**有空格**：`(True, 3, 2)`。

所以 VHDL 与 MATLAB 的输出一致，Python 的输出在大小写和空格上都不同——如果要做跨语言「字符串级别」的严格比对，这一点必须留意。

#### 4.4.3 源码精读

VHDL 实现只有一行 `return`，见 [en_cl_fix_pkg.vhd:1333-1337](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1333-L1337)：

```vhdl
function cl_fix_string_from_format ( fmt : FixFormat_t) return string is
begin
    return "(" & boolean'image(fmt.Signed) & "," & integer'image(fmt.IntBits)
           & "," & integer'image(fmt.FracBits) & ")";
end;
```

`boolean'image(true)` 在 VHDL 里返回小写字符串 `"true"`，`integer'image(3)` 返回 `"3"`，用 `&` 拼接、用逗号分隔、不加空格。

它的反向函数 `cl_fix_format_from_string` 就在紧挨着的下方，[en_cl_fix_pkg.vhd:1341-1368](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1341-L1368)：逐个找 `(`、`,`、`)` 分隔符，调用 `string_parse_boolean` / `string_parse_int` 解析三段。它的工程背景是「Modelsim 的 generic 只支持 integer/string/boolean，所以格式只能以字符串传入仿真」（详见 u7-l3）。本讲你只要记住：`string_from_format` 与 `format_from_string` 是一对互逆工具。

Python 实现又是一层薄封装，见 [en_cl_fix_pkg.py:40-41](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L40-L41)：

```python
def cl_fix_string_from_format(fmt : FixFormat) -> str:
    return str(fmt)
```

真正逻辑在 `FixFormat.__str__`，见 [en_cl_fix_types.py:49-50](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_types.py#L49-L50)：

```python
def __str__(self):
    return f"({self.Signed}, {self.IntBits}, {self.FracBits})"
```

f-string 里 `{self.Signed}` 会打印成 `True`/`False`（首字母大写），且逗号后带空格，所以输出是 `(True, 3, 2)`。顺带一提，`__repr__`（[en_cl_fix_types.py:46-47](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_types.py#L46-L47)）会带上类名，输出 `FixFormat(True, 3, 2)`，调试时两者会出现在不同场景。

MATLAB 实现是手动拼接，见 [cl_fix_string_from_format.m:19-28](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_string_from_format.m#L19-L28)：

```matlab
function str = cl_fix_string_from_format(fmt)
str = '(';
if fmt.Signed
    str = [str 'true'];
else
    str = [str 'false'];
end
str = [str ',' sprintf('%i',fmt.IntBits)];
str = [str ',' sprintf('%i',fmt.FracBits) ')'];
```

布尔用 `if` 选 `true`/`false`（小写），整数用 `sprintf('%i',...)`，逗号分隔无空格，结果与 VHDL 一致：`(true,3,2)`。

#### 4.4.4 代码实践

1. **实践目标**：亲眼看到三套实现的字符串输出差异。
2. **操作步骤**：
   - 在 Python 中打印 `cl_fix_string_from_format(FixFormat(True,3,2))`，记录输出。
   - 对照 VHDL（`boolean'image` 拼接）与 MATLAB（手动拼接）的源码，推断它们会输出什么。
3. **需要观察的现象**：Python 输出 `(True, 3, 2)`（大写 T、带空格）；VHDL 与 MATLAB 输出 `(true,3,2)`（小写、无空格）。
4. **预期结果**：三套实现的字符串「语义」相同（都描述 `(true,3,2)` 这个格式），但文本不完全一致。如果要做跨语言字符串严格相等比较，需先统一大小写与空格（VHDL 的 `format_from_string` 解析器在 1.2.0 修复大小写问题后是大小写不敏感的，详见 u7-l3）。

#### 4.4.5 小练习与答案

**练习 1**：`cl_fix_string_from_format(FixFormat(False,4,0))` 在 Python 和 VHDL 中分别输出什么？

**参考答案**：Python 输出 `(False, 4, 0)`；VHDL 输出 `(false,4,0)`。差异在 `False`/`false` 的大小写与逗号后的空格。

**练习 2**：为什么需要 `cl_fix_string_from_format` 这个函数？直接打印三元组不行吗？

**参考答案**：因为 VHDL 的 `FixFormat_t` 是 record，不能直接作为 Modelsim 的 generic 传递（generic 只支持 integer/string/boolean）。把格式序列化成字符串后，既能作为 generic 传入仿真、又能写进 `en_cl_bittrue` 的数据交换文件头，反向再用 `cl_fix_format_from_string` 解析回来。所以这个「看似只是打印」的函数其实是跨工具协作的关键桥梁。

---

## 5. 综合实践

把本讲四个模块串起来，完成规格要求的核心实践：**用 Python 计算 `(true,3,2)` 的位宽与边界值并打印字符串，再到 VHDL 包体里比对实现**。

**实践目标**：验证三套实现对「同一个格式」给出一致的位宽与边界值，并亲身确认「VHDL 返回位串、Python 返回实数」这一关键差异。

**操作步骤**：

1. 在 `python/unittest` 目录运行 `python3 en_cl_fix_pkg_test.py`，确认所有用例通过（本讲相关的 `cl_fix_width_Test`、`cl_fix_max_value_Test`、`cl_fix_min_value_Test` 都在其中）。
2. 写一个最小脚本（**示例代码**，非项目原有文件）：

   ```python
   # 示例代码：需先把 python/src 加入 sys.path 或 PYTHONPATH
   import sys; sys.path.insert(0, "../src")
   from en_cl_fix_pkg import *

   fmt = FixFormat(True, 3, 2)
   print("width      =", cl_fix_width(fmt))            # 预期 6
   print("max_value  =", cl_fix_max_value(fmt))        # 预期 7.75
   print("min_value  =", cl_fix_min_value(fmt))        # 预期 -8.0
   print("as string  =", cl_fix_string_from_format(fmt))  # 预期 (True, 3, 2)
   ```

3. 打开 VHDL 包体，定位四个函数的实现：`cl_fix_width`（[L1321](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1321)）、`cl_fix_max_value`（[L1382](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1382)）、`cl_fix_min_value`（[L1395](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1395)）、`cl_fix_string_from_format`（[L1333](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1333)）。
4. 逐项比对：VHDL `cl_fix_width` 的 `return toInteger(fmt.Signed)+IntBits+FracBits` 与 Python `width()` 是否同一公式？VHDL `max_value` 返回的位串 `011111` 解释成实数是否等于 Python 的 `7.75`？

**需要观察的现象**：

- Python 脚本输出 `width = 6`、`max_value = 7.75`、`min_value = -8.0`、`as string = (True, 3, 2)`。
- VHDL 的位宽公式与 Python 完全一致；VHDL `max_value` 位串 `011111` 按 6 位补码（=+31）乘步长 0.25 = `7.75`，与 Python 实数一致；VHDL `min_value` 位串 `100000`（=−32）乘 0.25 = `-8.0`，与 Python 一致。
- VHDL `cl_fix_string_from_format` 输出 `(true,3,2)`，与 Python 的 `(True, 3, 2)` 在大小写和空格上不同。

**预期结果**：四项查询在三套实现里「语义一致、形式有别」——位宽与边界实数值完全相同，而返回类型（位串 vs 实数）和字符串文本（大小写/空格）因语言而异。如果你无法在本地运行 Python（缺 numpy 等），可改用「源码阅读型实践」：仅依据本讲引用的源码行号，手算 `(true,3,2)` 的全部四项结果并填表，标注「待本地验证」。

## 6. 本讲小结

- `cl_fix_width` 的实现就是 `toInteger(Signed) + IntBits + FracBits`，三语言公式一致；对负位直接相加即可，无需特判。
- 硬约束 `IntBits+FracBits >= 1` 的执行因语言而异：VHDL 用 `assert severity failure`、MATLAB 用 `error`（且额外有 `<= 52` 上限）、Python **不检查**（静默返回退化位宽）。
- **关键跨语言差异**：VHDL 的 `cl_fix_zero_value/max_value/min_value` 返回**位串**，Python/MATLAB 的同名函数返回**实数**——同名却不同类，是最容易踩的坑。
- 边界值闭式公式：\( V_{\max} = 2^{I} - 2^{-F} \)，有符号 \( V_{\min} = -2^{I} \)、无符号 \( V_{\min} = 0 \)；二进制补码使负方向比正方向多走一个 LSB。
- VHDL 额外提供 `cl_fix_max_real/min_real`（返回实数），其公式与 Python/MATLAB 的 `max_value/min_value` 完全相同；后两者因此不需要 `max_real/min_real`。
- `cl_fix_string_from_format` 把格式序列化为 `(signed,int,frac)`：VHDL/MATLAB 输出 `(true,3,2)`（小写无空格），Python 输出 `(True, 3, 2)`（大写带空格）；它的反向函数 `cl_fix_format_from_string` 是 u7-l3 的内容。

## 7. 下一步学习建议

本讲只「查询」格式、不改变数值。接下来建议：

- **u1-l4 舍入模式 FixRound**：当数值要从一个格式放进另一个（位宽更小）的格式时，多出来的低位怎么处理？七种舍入模式是理解 `cl_fix_resize` 的前提。
- **u1-l5 饱和模式 FixSaturate**：数值超出 `max_value/min_real` 划定的范围时怎么办？回绕还是夹紧？这直接用到了本讲的边界值概念。
- 之后再进入 **u3 核心转换与 resize 管线**，那里你会看到 `cl_fix_width`、`cl_fix_max_real/min_real` 被大量用来声明信号宽度和夹紧输入——本讲这些「小工具」正是 resize 这个「大心脏」的零件。

如果想提前感受这些工具的真实用法，可以先去 VHDL 源码里搜一下 `cl_fix_max_real` 的调用点（如 [L1662](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1662) 的 `cl_fix_from_real` 饱和段），体会「边界值函数如何支撑饱和逻辑」。
