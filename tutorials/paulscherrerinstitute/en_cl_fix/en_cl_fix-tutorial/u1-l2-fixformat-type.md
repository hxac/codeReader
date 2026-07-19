# 定点数格式 [S,I,F] 与 FixFormat 类型

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `(Signed, IntBits, FracBits)` 这个三元组的每一项代表什么。
- 用公式 `W = S + IntBits + FracBits` 计算任意定点格式的**总位宽**。
- 解释**整数位、小数位都可以是负数**意味着什么，以及负位如何改变位宽与可表示范围。
- 在 VHDL、Python、MATLAB 三套实现中分别认出"定点格式"这个类型是怎么定义的。
- 知道库的一条硬约束：`IntBits + FracBits` 必须 ≥ 1，以及它在三种语言里分别是如何被检查的（其中 Python 有一个值得注意的"漏检"细节）。

本讲只讲"格式"本身，**还不涉及如何把数值放进去、如何舍入、如何饱和**——那些是后续讲义（u1-l4 舍入、u1-l5 饱和、u3 转换函数）的内容。

## 2. 前置知识

在进入源码之前，先用最朴素的方式理解三个概念。

**定点数（fixed-point number）。** 计算机里的小数，本质是一串 0/1 位。如果我们约定"某一根线"是二进制小数点（binary point），那么小数点左边的位表示整数部分、右边的位表示小数部分。所谓"定点"，就是**小数点的位置固定下来不再移动**。这与"浮点（floating-point）"相对——浮点数的小数点会随指数移动。

**位真（bit-true）。** 这是上一讲（u1-l1）建立的核心概念：同一个定点算法，在 VHDL / MATLAB / Python 三种语言里实现出来的**每一位输出都完全相同**。要做到这一点，三种语言必须先对"定点格式"有**完全一致的定义**——这正是本讲要讲的内容。

**三元组 [S, I, F]。** 一个定点格式由三个量决定：

- `S`（Signed）：是否有符号位。`true` 表示有符号（用二进制补码表示，可表示负数），`false` 表示无符号（只能表示非负数）。
- `I`（IntBits）：整数位的个数。
- `F`（FracBits）：小数位的个数。

一个关键直觉：**这三个量唯一决定了"一串位如何被解释成一个实数"**。同样 4 位的 `0101`，在 `[false,4,0]` 里是 5，在 `[false,2,2]` 里是 1.25——位模式没变，变的只是格式（小数点的位置和符号约定）。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/README.md) | 项目顶层的"Fixed Point Number Format"章节，给出了格式定义、位权示意和示例真值表。 |
| [vhdl/src/en_cl_fix_pkg.vhd](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd) | VHDL 实现。定义了 `FixFormat_t` record 类型，以及 `cl_fix_width` 等函数。是三套实现里文档注释最详尽的一份。 |
| [python/src/en_cl_fix_pkg/en_cl_fix_types.py](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_types.py) | Python 实现。`FixFormat` 类及其 `width()` 方法、以及 `ForAdd/ForSub/ForMult` 等格式增长规则都定义在这里。 |
| [matlab/src/cl_fix_format.m](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_format.m) | MATLAB 实现。一个 `.m` 文件对应一个 `cl_fix_format` 函数，返回一个结构体作为格式。 |

## 4. 核心概念与源码讲解

### 4.1 定点格式 [S,I,F]：符号位、整数位与小数位

#### 4.1.1 概念说明

README 用一行话给出了格式的定义：

> The fixed point number format used in this library is defined as follows: `[s, i, f]`

三个字段的含义在 README 的格式章节里直接列出（详见下面的源码精读）。要理解它，最直接的方式是看位权（每一根位线代表多少）。

README 顶部给出了一条位权轴：

```
... [4][2][1]**.**[0.5][0.25][0.125] ...
```

这条轴的含义是：

- 小数点 `.` **左边**是整数位，从近到远权重依次是 `1, 2, 4, 8, ...`（即 \(2^0, 2^1, 2^2, \dots\)）。
- 小数点 `.` **右边**是小数位，从近到远权重依次是 `0.5, 0.25, 0.125, ...`（即 \(2^{-1}, 2^{-2}, 2^{-3}, \dots\)）。
- 如果是有符号数（`Signed=true`），则**最左边那一位是符号位**，按二进制补码解释，其权重为负。

`IntBits` 决定小数点左边有几位，`FracBits` 决定小数点右边有几位，`Signed` 决定最左边是否多出一位符号位。

#### 4.1.2 核心流程

给定一个格式 `[S, I, F]` 和一段位串，把它解释成实数值的过程可以总结为一个公式（这个公式对负的 `I`、`F` 同样成立，后面会验证）：

\[
V = N \times 2^{-F}
\]

其中 \(N\) 是把存储的 \(W\) 个位**按有符号 / 无符号解释成一个整数**得到的值，\(W\) 是总位宽。

由此可以推出一个格式的**可表示范围**：

- 有符号：\(\;V_{\min} = -2^{I},\quad V_{\max} = 2^{I} - 2^{-F}\)
- 无符号：\(\;V_{\min} = 0,\quad V_{\max} = 2^{I} - 2^{-F}\)

> 这两个公式之所以对负的 `I`、`F` 也成立，是因为 \(2^x\) 对负指数同样有定义（例如 \(2^{-2} = 0.25\)）。下一个小节会用 README 的例子逐一验证。

#### 4.1.3 源码精读

README 的"Fixed Point Number Format"章节给出了定义和示例真值表，是本讲最重要的参考资料：

[README.md#L41-L68](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/README.md#L41-L68) — 这段代码定义了 `[s, i, f]` 三元组、总位宽 `s+i+f`、位权轴，并用一张表给出了 6 个典型格式的范围、位模式与示例值。

README 表里的几个例子值得逐个用上面的公式验算（这是后续练习的基础）：

| 格式 | 总位宽 W | 范围（README） | 用公式算的范围 |
|:----:|:----:|:----|:----|
| `[true,2,2]` | 1+2+2=5 | −4 … +3.75 | 有符号：\( -2^2=-4 \)，\( 2^2-2^{-2}=4-0.25=3.75 \) ✓ |
| `[false,4,2]` | 0+4+2=6 | 0 … 15.75 | 无符号：\( 2^4-2^{-2}=16-0.25=15.75 \) ✓ |
| `[true,4,-2]` | 1+4−2=3 | −16 … 12 | 有符号：\( -2^4=-16 \)，\( 2^4-2^{2}=16-4=12 \) ✓ |
| `[true,-2,4]` | 1−2+4=3 | −0.25 … +0.1875 | 有符号：\( -2^{-2}=-0.25 \)，\( 2^{-2}-2^{-4}=0.25-0.0625=0.1875 \) ✓ |

可以看到，**整数位或小数位为负时，公式依然精确成立**：

- `FracBits = -2`：\( 2^{-F} = 2^{2} = 4 \)，意味着最低位的权重是 4，整个数只能是 4 的倍数（粗粒度）。
- `IntBits = -2`：\( 2^{I} = 2^{-2} = 0.25 \)，整个可表示范围被压缩到 0 附近的一个小分数（细粒度、小数值）。

VHDL 包里也有等价的位权示意图（Doxygen 注释），用 `I` 表示整数位、`F` 表示小数位、`S` 表示符号位：

[vhdl/src/en_cl_fix_pkg.vhd#L126-L142](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L126-L142) — 分别列出无符号与有符号情况下，不同 `IntBits/FracBits`（含 0、负值）对应的位模式与位宽。例如注释里的 `SIII00.000` 对应 `IntBits=5, FracBits=-2, Width=4`，其中 `00` 表示 2 个**隐含的不存储的低位**（因为 `FracBits=-2`，数值必为 4 的倍数，那两位恒为 0，所以不存）。

> 小提示：VHDL 注释里出现的 `--`（如 README 示例 `0.--10`、`110--.`）表示"概念上存在、但实际不存储"的位。初学时**不必纠结这种 ASCII 画法的精确位置**，直接用本节的数值公式 \(V=N\times2^{-F}\) 计算即可，绝不会被误导。

#### 4.1.4 代码实践

**实践目标**：用本节的公式手算 README 表中的例子，建立对 `[S,I,F]` 的直觉。

**操作步骤**：

1. 打开 README 的格式表（上面的永久链接）。
2. 任取一行，例如 `[true,4,-2]`，示例值 `-8`，位模式 `110--.`。
3. 用公式验算：`W = 1+4-2 = 3`，存储 3 位 `110`。把 `110` 按 3 位有符号补码解释：\(N = -4+2 = -2\)。再乘以 \(2^{-F} = 2^{2} = 4\)：\(V = -2 \times 4 = -8\)。
4. 对 `[true,-2,4]` 的示例值 `0.125`（位模式 `0.--10`）重复一遍：存储 3 位 `010`，\(N = 2\)，\(2^{-F} = 2^{-4} = 1/16\)，\(V = 2/16 = 0.125\)。

**需要观察的现象**：位模式里写出来的 `0`/`1` 个数，恰好等于总位宽 `W`；多出来的 `--` 是不存储的隐含位。

**预期结果**：两个例子的手算值都与 README 表中的"Example Int"一致（-8 与 0.125）。

#### 4.1.5 小练习与答案

**练习 1**：格式 `[false,4,0]` 的总位宽和可表示范围是多少？示例值 `5` 的位模式 `0101.` 如何解释？

> **答案**：\(W = 0+4+0 = 4\)。无符号范围 \(0 \dots 2^4 - 2^{0} = 16 - 1 = 15\)。`0101` 按 4 位无符号解释 \(N=5\)，\(2^{-F}=2^0=1\)，故 \(V=5\)。

**练习 2**：为什么 `[true,4,-2]` 能表示的最大值是 12 而不是 15？

> **答案**：因为 `FracBits = -2` 使最低位权重为 \(2^2=4\)，所有值都是 4 的倍数。3 位有符号最大整数 \(N=3\)（`011`），\(V = 3 \times 4 = 12\)。能表示的值是 \(\{-16,-12,-8,-4,0,4,8,12\}\)，跳着走，所以达不到 15。

---

### 4.2 三种语言中的 FixFormat 类型定义

#### 4.2.1 概念说明

上一节讲了 `[S,I,F]` 的数学含义。在代码里，这个三元组需要被表示成某种**数据类型**。由于 VHDL、Python、MATLAB 三种语言的风格差异很大，同一个概念用了三种不同的语法来表达，但**语义完全一致**——这是位真一致性的前提。

- **VHDL**：用 `record`（记录体），三个字段分别是 `boolean` 和两个 `integer`。
- **Python**：用一个 `FixFormat` 类，构造函数 `__init__` 接收三个参数。
- **MATLAB**：没有类，用一个函数 `cl_fix_format` 返回一个结构体 `fmt`，结构体里塞三个字段。

#### 4.2.2 核心流程

三种语言里"造一个格式对象"的调用方式对比：

```
VHDL:   constant Fmt_c : FixFormat_t := (true, 3, 4);      -- 位置聚合
Python: Fmt = FixFormat(True, 3, 4)                          # 构造函数
MATLAB: fmt = cl_fix_format(1, 3, 4)                         % 函数返回结构体
```

> 注意 MATLAB 调用里第一个参数是 `1` 而不是 `true`——因为 MATLAB 的 `cl_fix_format` 内部用 `signed > 0` 来判断真假（详见源码精读），传非零正数即视为有符号。

#### 4.2.3 源码精读

**VHDL 的 record 定义。** 这是三套实现里最"正式"的定义，字段类型明确，注释也最完整：

[vhdl/src/en_cl_fix_pkg.vhd#L153-L157](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L153-L157) — 定义 `FixFormat_t` 为含三个字段的 record：`Signed : boolean`，`IntBits : integer`，`FracBits : integer`。注释里明确写出 `IntBits`、`FracBits 都可以为负，且 IntBits+FracBits 至少为 1`。

**Python 的类定义。** `FixFormat` 是一个普通 Python 类，构造函数把三个参数分别做强类型转换后存为成员：

[python/src/en_cl_fix_pkg/en_cl_fix_types.py#L13-L16](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_types.py#L13-L16) — `__init__(self, Signed, IntBits, FracBits)` 把 `Signed` 转 `bool`、`IntBits/FracBits` 转 `int`。这个强转很关键：即便你误传了字符串 `"true"` 或浮点 `3.0`，也会被规整成 `bool`/`int`，避免后续数值运算出错。

同一个文件里还定义了 `__eq__`（两个格式相等当且仅当三字段全等）、`__repr__`/`__str__`（打印成 `(Signed, IntBits, FracBits)`），以及一组 `ForAdd/ForSub/ForMult/ForNeg/ForShift` 静态方法——它们表达"运算后格式如何增长"，是 Unit 4 运算讲义的主题，本讲先知道有这些方法即可：

[python/src/en_cl_fix_pkg/en_cl_fix_types.py#L12-L56](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_types.py#L12-L56) — 整个 `FixFormat` 类的定义。

**MATLAB 的函数式定义。** MATLAB 端没有类，而是约定"一个 `.m` 文件 = 一个 `cl_fix_` 函数"。`cl_fix_format` 接收三个标量，返回带三个字段的结构体：

[matlab/src/cl_fix_format.m#L15-L27](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_format.m#L15-L27) — 函数体里先把 `signed > 0` 的结果赋给 `fmt.Signed`（所以传 `1` 表示有符号、`0` 表示无符号），再把 `intBits/fracBits` 直接赋给 `fmt.IntBits/fmt.FracBits`。开头那段注释就是 MATLAB 的"help 文档"——在 MATLAB 命令行输入 `help cl_fix_format` 会显示这段说明。

#### 4.2.4 代码实践

**实践目标**：在三种语言里各构造同一个格式 `(true, 3, 4)`，体会三种写法的等价性。

**操作步骤**：

1. **Python（可直接运行）**：

   ```python
   from en_cl_fix_pkg.en_cl_fix_types import FixFormat
   f = FixFormat(True, 3, 4)
   print(f)            # 期望输出: (True, 3, 4)
   print(repr(f))      # 期望输出: FixFormat(True, 3, 4)
   ```

2. **VHDL（待本地用 Modelsim 验证）**：在 testbench 里写

   ```vhdl
   constant Fmt_c : FixFormat_t := (true, 3, 4);
   ```

   并用 `report cl_fix_string_from_format(Fmt_c);` 打印（该函数下一讲 u1-l3 详讲）。

3. **MATLAB（待本地验证）**：

   ```matlab
   fmt = cl_fix_format(1, 3, 4);   % 注意第一个参数是 1，不是 true
   disp(fmt.Signed)                % 期望输出: 1
   ```

**需要观察的现象**：三种写法产出的"逻辑格式"完全相同（都是"有符号、3 个整数位、4 个小数位"），只是语法外壳不同。

**预期结果**：Python 的 `print(f)` 输出 `(True, 3, 4)`；MATLAB 的 `fmt.Signed` 为 `1`。

#### 4.2.5 小练习与答案

**练习 1**：在 VHDL 里，`FixFormat_t` 的三个字段分别是什么类型？为什么 `IntBits` 用 `integer` 而不是 `natural`？

> **答案**：`Signed : boolean`，`IntBits : integer`，`FracBits : integer`。用 `integer`（可负）而非 `natural`（非负）是因为整数位允许为负（如 `[true,-2,4]`），`natural` 无法表示负值。

**练习 2**：为什么 MATLAB 的 `cl_fix_format(1, 3, 4)` 用 `1` 而不是 `true` 表示有符号？

> **答案**：函数体内用 `fmt.Signed = signed > 0` 做判断，只要传入的标量大于 0 就视为有符号。这是 MATLAB 端的约定，传 `1` 是最常见的写法。

---

### 4.3 位宽计算 width() 与负整数位 / 负小数位

#### 4.3.1 概念说明

给定一个格式，我们最常要问的第一个问题就是：**它需要多少位来存储？** 答案就是总位宽公式：

\[
W = S + I + F
\]

其中 \(S\) 在有符号时取 1、无符号时取 0。这个公式对负的 `I`、`F` 同样直接适用——这也是为什么负位会"减小"位宽：例如 `[true,-2,4]` 只有 \(1 + (-2) + 4 = 3\) 位，因为那 2 个"本该是整数位"的位置被压缩掉了。

#### 4.3.2 核心流程

位宽计算的流程极其简单，就一行：

1. 取 `Signed`，转成整数（`true → 1`，`false → 0`）。
2. 加上 `IntBits`。
3. 加上 `FracBits`。
4. 结果就是总位宽 `W`，也就是对应 `std_logic_vector(W-1 downto 0)` 的宽度。

#### 4.3.3 源码精读

**Python 的 `width()`。** 这是三套实现里最直白的一个：

[python/src/en_cl_fix_pkg/en_cl_fix_types.py#L55-L56](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_types.py#L55-L56) — `width(self)` 直接 `return int(self.Signed) + self.IntBits + self.FracBits`。`int(True)` 在 Python 里就是 `1`，`int(False)` 就是 `0`，与公式完全对应。

在 `en_cl_fix_pkg.py` 里还有一个等价的模块级函数 `cl_fix_width`，它只是转发到 `fmt.width()`，目的是让 Python 端的 API 命名与 VHDL/MATLAB 保持一致：

[python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L20-L21](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L20-L21) — `def cl_fix_width(fmt): return fmt.width()`。

**VHDL 的 `cl_fix_width`。** 逻辑完全一样，但多了一道断言保护（断言的内容是下一节 4.4 的主题）：

[vhdl/src/en_cl_fix_pkg.vhd#L1321-L1329](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1321-L1329) — 先 `assert (fmt.IntBits+fmt.FracBits) > 0 ... severity failure`，再 `return toInteger(fmt.Signed)+fmt.IntBits+fmt.FracBits`。这里的 `toInteger(fmt.Signed)` 等价于 Python 的 `int(self.Signed)`：把布尔转成 0/1。

> 三套实现的表达式逐字符对应：`toInteger(Signed) + IntBits + FracBits`（VHDL）≡ `int(Signed) + IntBits + FracBits`（Python）。这就是位真一致性的一个微观体现——同一公式，三种语言各写一遍，结果必然相同。

#### 4.3.4 代码实践

**实践目标**：用 Python 构造包含负位的边界格式，调用 `width()` 并手算验证。

**操作步骤**：

```python
from en_cl_fix_pkg.en_cl_fix_types import FixFormat

# 边界格式 1：负整数位
f1 = FixFormat(True, -2, 3)
print(f1.width())   # 手算: 1 + (-2) + 3 = 2

# 边界格式 2：负小数位
f2 = FixFormat(False, 3, -1)
print(f2.width())   # 手算: 0 + 3 + (-1) = 2

# 对照 README 表里的例子
print(FixFormat(True,  4, -2).width())   # 期望 3
print(FixFormat(True, -2,  4).width())   # 期望 3
print(FixFormat(True,  2,  2).width())   # 期望 5
```

**需要观察的现象**：负的 `IntBits` 或 `FracBits` 会从总位宽里"扣掉"相应的位数；最终位宽可以小到 2 甚至 1。

**预期结果**：依次输出 `2`、`2`、`3`、`3`、`5`。

> 待本地验证：上述输出依赖你已把 `python/src` 加入 Python 路径（或用 `pip install -e` 安装）。若提示找不到 `en_cl_fix_pkg`，请先解决包路径。

#### 4.3.5 小练习与答案

**练习 1**：`FixFormat(True, -2, 3)` 只有 2 位，却号称是"有符号且带 3 个小数位"的格式，这怎么可能？

> **答案**：因为 `IntBits = -2` 表示"小数点位于存储位的最左侧之外 2 位"，2 个本该是整数位的高位被压缩为隐含的 0，不占用存储。所以实际只存 `1（符号）+ 1（数据）= 2` 位，而这 2 位都落在小数点右侧的小数区域里。

**练习 2**：不运行代码，口算 `FixFormat(False, 0, 5)` 和 `FixFormat(True, 5, 0)` 的位宽。

> **答案**：前者 \(0+0+5=5\)；后者 \(1+5+0=6\)。

---

### 4.4 约束：IntBits + FracBits 必须 ≥ 1

#### 4.4.1 概念说明

库有一条硬性约束：**`IntBits + FracBits` 必须至少为 1**。

为什么必须有这个约束？因为如果 `IntBits + FracBits = 0`，那么总位宽 \(W = S + 0\)——无符号时 \(W=0\)（一个位都没有，无法表示任何数），有符号时 \(W=1\)（只有一位符号位，能表示的只有 0 和 -1 两个值，且语义退化）。为了避免这种退化、无意义的格式，库强制要求 `IntBits + FracBits ≥ 1`。

注意：这个约束只限制 `IntBits + FracBits` 的**和**，不限制各自的符号。所以 `[true, -2, 4]`（和为 2）合法，但 `[true, -4, 4]`（和为 0）非法。

#### 4.4.2 核心流程

三种语言对这个约束的**检查位置和检查力度并不相同**，这是本讲一个重要的实战细节：

| 语言 | 检查位置 | 检查方式 | 触发后果 |
|------|----------|----------|----------|
| VHDL | `cl_fix_width` 函数内部 | `assert ... severity failure` | 仿真立即终止 |
| MATLAB | `cl_fix_format` 构造函数内部 | `error('...')` | 抛出错误 |
| Python | **无检查**（构造函数和 `width()` 都不校验） | —— | 不报错，可能得到退化的位宽 |

也就是说，**Python 是三套实现里唯一不会主动拦截这个非法格式的**。这是真实存在的跨语言差异，写代码时需要留意。

#### 4.4.3 源码精读

**MATLAB 的检查（构造期）**：在 `cl_fix_format` 函数最开头就拦截：

[matlab/src/cl_fix_format.m#L17-L19](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_format.m#L17-L19) — `if (intBits+fracBits) < 1, error('cl_fix_format : "intBits"+"fracBits" must be at least 1!'); end`。MATLAB 是三套实现里检查得最早的——在创建格式对象的那一刻就拒绝非法输入。

> 顺带一提：紧接着还有一段被注释掉的 `if (intBits+fracBits) > 52 ... error(...)`，原本想限制最大 52 位（受 MATLAB `double` 精度限制），但被注释掉了，所以当前 MATLAB 端实际不限上限。这属于历史遗留，了解即可。

**VHDL 的检查（求位宽期）**：放在 `cl_fix_width` 函数里，用 `assert ... severity failure`：

[vhdl/src/en_cl_fix_pkg.vhd#L1324-L1326](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1324-L1326) — `assert (fmt.IntBits+fmt.FracBits) > 0 report "cl_fix_width : ... must be at least 1!" severity failure;`。`severity failure` 会让 Modelsim 立即中止仿真。注意 VHDL 的 `FixFormat_t` record 本身不校验——校验发生在第一次调用 `cl_fix_width` 时。

**Python 的"不检查"**：对照前面读过的两段源码——`FixFormat.__init__`（[en_cl_fix_types.py#L13-L16](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_types.py#L13-L16)）只做类型转换、无任何 `assert`；`width()`（[en_cl_fix_types.py#L55-L56](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_types.py#L55-L56)）也只是返回三字段之和。所以 `FixFormat(True, -4, 4)` 在 Python 里能被正常构造，`width()` 还会返回 `1`——一个退化的结果。

#### 4.4.4 代码实践

**实践目标**：亲手验证三种语言对非法格式 `IntBits+FracBits = 0` 的不同反应。

**操作步骤**：

1. **Python（可直接运行）**：

   ```python
   from en_cl_fix_pkg.en_cl_fix_types import FixFormat
   bad = FixFormat(True, -4, 4)   # IntBits+FracBits = 0
   print(bad.width())             # 不会报错，输出 1（退化）
   ```

2. **MATLAB（待本地验证）**：

   ```matlab
   fmt = cl_fix_format(1, -4, 4);   % 期望: 抛出 error
   ```

3. **VHDL（待本地验证）**：在 testbench 中

   ```vhdl
   constant Bad_c : FixFormat_t := (true, -4, 4);
   -- 当某处调用 cl_fix_width(Bad_c) 时，期望: severity failure 终止仿真
   ```

**需要观察的现象**：Python 静默地接受了非法格式并返回退化的位宽 1；MATLAB 在构造时即报错；VHDL 在调用 `cl_fix_width` 时才报错并中止仿真。

**预期结果**：Python 输出 `1`（不报错）；MATLAB/VHDL 报错。这正是上表所列的跨语言差异。

#### 4.4.5 小练习与答案

**练习 1**：为什么约束是"`IntBits+FracBits ≥ 1`"，而不是"`IntBits ≥ 0 且 FracBits ≥ 0`"？

> **答案**：因为库允许整数位或小数位单独为负（如 `[true,-2,4]`、`[true,4,-2]` 都合法且有用），只要两者之和至少为 1 即可保证格式非退化。限制各自非负会丢掉这些合法且实用的格式。

**练习 2**：如果在 Python 里误用了 `FixFormat(True, -4, 4)` 并继续做运算，最可能在后续哪一步暴露出问题？

> **答案**：由于位宽退化到 1，后续任何真正需要解释位串的操作（如 `cl_fix_from_real`、`cl_fix_resize`）都会基于错误的位宽工作，可能产生无意义的结果。Python 不会在构造期提醒你，所以要靠自己对 `IntBits+FracBits ≥ 1` 保持警觉。

## 5. 综合实践

把本讲的知识串起来完成下面这个任务。

**任务**：用 Python 构造两个边界格式 `FixFormat(True, -2, 3)` 与 `FixFormat(False, 3, -1)`，完成以下四步。

1. 调用 `width()` 计算它们的位宽，并与手算结果对照。
2. 用本节的数值公式 \(V = N \times 2^{-F}\) 和范围公式，分别写出这两个格式能表示的最小值、最大值。
3. 为每个格式**手绘一张二进制小数点位置图**（参照 VHDL 注释 `SIII.FFF` / `III00.000` 的画法），标出哪些位被存储、哪些位是隐含的（用 `--` 或 `0` 标注）。
4. 把你画出的图与 README 格式表、VHDL Doxygen 注释（[vhdl/src/en_cl_fix_pkg.vhd#L126-L142](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L126-L142)）对比，确认小数点位置一致。

**参考答案**：

- `FixFormat(True, -2, 3)`：位宽 \(1+(-2)+3 = 2\)。范围 \( -2^{-2}=-0.25 \dots 2^{-2}-2^{-3}=0.25-0.125=0.125 \)。小数点在存储位左侧之外 2 位，画法形如 `0.--SF`（`S` 为符号位，`F` 为 1 个存储小数位，`--` 为 2 个隐含高位）。
- `FixFormat(False, 3, -1)`：位宽 \(0+3+(-1) = 2\)。范围 \( 0 \dots 2^{3}-2^{1}=8-2=6 \)。最低位权重为 2（数值必为偶数），画法形如 `II-`（2 个存储整数位，`-` 为 1 个隐含的低位 0）。
- （上面的 `S`/`F`/`I` 标注是为了讲清位置关系；具体存储位的取值由你要表示的数决定。）

> 待本地验证：第 3、4 步的图请以 VHDL Doxygen 注释的画法为权威参照来核对你自己的图。

## 6. 本讲小结

- 定点格式由三元组 `[Signed, IntBits, FracBits]` 唯一决定；`Signed` 决定是否有符号位，`IntBits/FracBits` 决定小数点左右各有多少位。
- 总位宽公式 \(W = S + I + F\) 在三种语言里逐字符对应（VHDL `toInteger(Signed)+...` ≡ Python `int(Signed)+...`）。
- `IntBits`、`FracBits` 都**可以为负**：负 `FracBits` 使数值成为某个 2 的幂的倍数（粗粒度），负 `IntBits` 把可表示范围压缩到 0 附近（小数值）；二者都会减小位宽。
- 数值公式 \(V = N \times 2^{-F}\) 与范围公式对正、零、负的位都成立，是理解所有格式的统一工具。
- 同一个概念在三种语言里有三种语法外壳：VHDL `record`、Python `class`、MATLAB 结构体函数，但语义完全一致——这是位真一致性的前提。
- 硬约束 `IntBits+FracBits ≥ 1` 在 MATLAB（构造期 `error`）、VHDL（`cl_fix_width` 内 `assert severity failure`）中被强制检查，但 **Python 不检查**——这是实战中需要留意的跨语言差异。

## 7. 下一步学习建议

本讲只讲了"格式"本身，还没有讲"如何把一个实数塞进这个格式"。建议接下来按顺序学习：

- **u1-l3 格式查询与字符串转换**：围绕 `FixFormat` 的工具函数，包括 `cl_fix_width`（已在本讲遇到）、`cl_fix_zero_value/max_value/min_value`、`cl_fix_max_real/min_real`，以及格式与 `(signed,int,frac)` 字符串之间的互转。
- **u1-l4 舍入模式 FixRound**：当把高位宽的中间结果塞回低位宽格式时，多余的低位怎么处理——七种舍入模式的语义。
- **u1-l5 饱和模式 FixSaturate**：当数值超出格式可表示范围时，是回绕（wrap）还是截断到最大/最小（clip）。

读完这三篇，你就可以进入 Unit 2，在三种语言里真正跑通测试，亲身体会"位真一致性"了。
