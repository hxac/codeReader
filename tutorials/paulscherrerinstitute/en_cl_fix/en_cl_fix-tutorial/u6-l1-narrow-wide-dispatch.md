# narrow/wide 双实现与自动派发

## 1. 本讲目标

本讲进入 en_cl_fix 的 Python 实现中一个「对用户几乎透明、但对正确性至关重要」的内部机制：**narrow 与 wide 两条计算路径，以及它们之间的自动派发**。

学完本讲后，你应当能够：

1. 说清楚为什么 Python 实现要区分 narrow（双精度浮点）与 wide（任意精度整数）两条路径，以及 53 位这个分界线从何而来。
2. 读懂 [`cl_fix_is_wide`](#) 这个一行函数的判定逻辑，并理解它为何用「总位宽 > 53」而不是理论上更宽松的条件。
3. 在 `cl_fix_from_real`、`cl_fix_resize`、`cl_fix_add/sub/mult` 等函数中，追踪到那几行「if 宽就走 wide_fxp」的派发代码，并理解「中间格式也要判宽」的细节。
4. 掌握 `wide_fxp` 的四种构造/转换方法 `FromFloat` / `FromNarrowFxp` / `FromFxp` / `to_narrow_fxp`，以及 wide_fxp 内部「未归一化大整数」的存储约定。

本讲是 Unit 6 的第一篇，承接 u3-l2（`cl_fix_resize` 的舍入机制，那里首次提到 Python 经 `cl_fix_is_wide` 派发到 wide_fxp）与 u2-l1（Python 包结构、numpy 向量化、`__init__.py` 门面），为 u6-l2（`wide_fxp` 类的运算符重载与 numpy 互操作）打下基础。

## 2. 前置知识

阅读本讲前，请先具备以下认知（前序讲义已建立）：

- **定点格式 `[S,I,F]`**：`S` 是否有符号、`I` 整数位、`F` 小数位，总位宽 \(W = S + I + F\)（见 u1-l2）。
- **Python 实现的工程骨架**：`en_cl_fix_types` 定义类型、`en_cl_fix_pkg` 是主体函数库、`wide_fxp` 是大位宽实现，三者由 `__init__.py` 用星号导入构成统一门面；几乎所有函数对 numpy `ndarray` 做向量化运算（见 u2-l1）。
- **`cl_fix_resize` 的舍入机制**：七种 `FixRound` 模式都遵循「先加偏移、再截断」，Python 实现内部会经 `cl_fix_is_wide` 判定走 narrow 浮点路径还是 wide 整数路径（见 u3-l2）。
- **IEEE 754 双精度浮点（float64）**：1 位符号、11 位指数、52 位小数尾数 + 1 位隐含的整数位 `1`，合计 **53 位有效位**。这是本讲最关键的外部知识。

一个本讲要反复用到的核心事实：**float64 最多只能精确表示 53 位二进制有效数字的整数**。超过这个规模的整数，float64 会「四舍五入」到最近的偶数倍，丢掉低位。这正是 wide 路径存在的根本原因。

## 3. 本讲源码地图

本讲涉及三个 Python 源文件，全部在 `python/src/en_cl_fix_pkg/` 下：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| `en_cl_fix_types.py` | 定义 `FixFormat`、`FixRound`、`FixSaturate` 等类型 | `FixFormat.width()` 位宽公式 |
| `en_cl_fix_pkg.py` | 主体函数库 | `cl_fix_is_wide`、`cl_fix_from_real` / `cl_fix_resize` 的 wide 分支、运算函数中的派发 |
| `wide_fxp.py` | 任意精度（>53 位）实现 | `FromFloat` / `FromNarrowFxp` / `FromFxp` / `to_narrow_fxp`，以及「未归一化大整数」存储约定 |

## 4. 核心概念与源码讲解

### 4.1 narrow 与 wide：为什么要两条路径

#### 4.1.1 概念说明

en_cl_fix 的 Python 实现默认在**实数域**上做计算：一个定点数就存成一个 `float64`（例如 `1.25`），舍入用 `np.floor`、饱和用 `np.where` clip、回绕用取模。这条路径称为 **narrow**（窄），因为它受限于 float64 的 53 位精度。

但当定点格式的位宽超过 53 位时，float64 装不下精确的整数值，narrow 路径就会悄悄丢精度，破坏位真一致性。于是 Python 实现提供了第二条路径 **wide**（宽）：把定点数存成一个**任意精度的 Python 大整数**（`numpy` 数组里 `dtype=object` 的 Python `int`），所有运算都在整数上做。`wide_fxp` 类就是这条路径的载体。

两条路径的关键差别在于**内部存储方式**，这一点 `wide_fxp.py` 开头的文档注释讲得最清楚：

> wide_fxp 内部数据**不按小数位归一化**。例如定点数 `1.25` 在 `FixFormat(0,2,4)` 下的二进制表示是 `01.0100`；在 wide_fxp 中它被存成整数值 `1.25 * 2**4 = 20`；而在 narrow 的 en_cl_fix_pkg 中，它被存成浮点值 `1.25`。

也就是说，同一个 `1.25`：

- narrow 存的是「真实数值」`1.25`（float64）。
- wide 存的是「把小数点右移 FracBits 位后的整数」`20`（大整数），二进制小数点的位置由 `fmt.FracBits` 隐含记录。

#### 4.1.2 核心流程

派发的总体思路可以用一段伪代码概括：

```
对任意一个接受 fmt 参数的函数 f(value, ..., fmt, ...):
    if cl_fix_is_wide(fmt):            # 位宽 > 53
        走 wide 路径：用 wide_fxp（大整数）计算
    else:
        走 narrow 路径：用 float64 计算
```

对于 `from_real`、`resize`、加减乘等函数，派发还多了一条规则：**只要参与运算的任一操作数已经是 `wide_fxp` 对象，整段计算就强制走 wide 路径**（见 4.3）。

wide 路径比 narrow 慢得多（大整数运算远慢于 float64 硬件指令），但能保证 >53 位时的位真正确性。这是「速度」与「精度」之间的工程取舍。

#### 4.1.3 源码精读

`wide_fxp.py` 文件头的注释是理解两条路径差异的最佳入口，明确给出了 `1.25 → 20` 的存储约定与「wide 慢但支持 >53 位」的定位：

- [python/src/en_cl_fix_pkg/wide_fxp.py:L5-L20](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L5-L20) —— wide_fxp 的设计说明，对比 narrow（浮点）与 wide（大整数）两种内部表示。

wide_fxp 的构造函数强制要求内部数据是 `dtype=object`（即任意精度 Python 整数），这是 wide 路径的「身份证」：

- [python/src/en_cl_fix_pkg/wide_fxp.py:L359-L363](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L359-L363) —— `__init__` 断言 `data.dtype == object`，注释再次强调「不要把已归一化的定点数（恰好是整数）当作内部数据传入」，例如 `3.0` 在 `(0,2,4)` 下内部数据是 `48` 而不是 `3`。

float64 的 53 位限制是 narrow 路径的天花板。一个 float64 的尾数有 52 位显式小数位 + 1 位隐含整数位 `1`：

\[ \text{有效位} = 52 + 1 = 53 \]

因此整数 \([-2^{53},\ 2^{53}]\) 都能被 float64 精确表示，超出这个范围就会丢低位。位宽公式则来自类型定义：

- [python/src/en_cl_fix_pkg/en_cl_fix_types.py:L55-L56](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_types.py#L55-L56) —— `FixFormat.width()` 返回 \(S + I + F\)，这是 `cl_fix_is_wide` 判定的输入。

#### 4.1.4 代码实践

**实践目标**：亲手验证「wide 存未归一化大整数、narrow 存浮点实数值」这一存储差异，并体会小格式下 `cl_fix_from_real` 走的是 narrow 路径。

**操作步骤**：

```python
from en_cl_fix_pkg import *

# 1) 直接用 wide_fxp.FromFloat 构造，即使是 6 位的小格式也会得到 wide_fxp 对象
w = wide_fxp.FromFloat(1.25, FixFormat(False, 2, 4))
print(type(w))              # <class '...wide_fxp'>
print(w.data)               # [20]   <- 1.25 * 2**4 = 20，未归一化的大整数
print(w.to_narrow_fxp())    # [1.25] 还原回实数

# 2) 对比：用 cl_fix_from_real 走 narrow 路径（宽度 6 <= 53）
n = cl_fix_from_real(1.25, FixFormat(False, 2, 4))
print(type(n))              # <class 'numpy.ndarray'>，里面是 float64 的 1.25
```

**需要观察的现象**：

- `w.data` 是 `[20]`，正是 \(1.25 \times 2^4 = 20\)；它是一个 `dtype=object` 的整数数组，而不是浮点。
- `cl_fix_from_real` 对 6 位格式返回的是普通 `numpy.ndarray`（float64），值就是 `1.25` 本身——说明派发器选择了 narrow。

**预期结果**：`w.data == [20]`，`type(n)` 是 `numpy.ndarray`。可见「是否走 wide」完全由格式的位宽决定，与数值大小无关。

#### 4.1.5 小练习与答案

**练习 1**：定点数 `-0.5` 在 `FixFormat(True, 2, 4)` 下，wide_fxp 内部存储的大整数是多少？narrow 存储的 float 是多少？

**答案**：wide 内部存 \(-0.5 \times 2^4 = -8\)；narrow 存 `-0.5`。

**练习 2**：为什么 wide_fxp 的注释特别警告「不要把已归一化的定点数（如 `3.0`）当作内部数据传入」？

**答案**：因为内部数据是**未归一化**的大整数。`3.0` 在 `(0,2,4)` 下的内部数据应是 \(3 \times 2^4 = 48\)，而不是 `3`。若误传 `3`，`wide_fxp` 会把它当成 `3/16 = 0.1875`，数值完全错误。所以用户应通过 `FromFloat` 等公开方法构造，而不是直接喂内部整数。

---

### 4.2 cl_fix_is_wide：53 位边界判定函数

#### 4.2.1 概念说明

整个派发机制的「裁判」是一个极其简短的函数 `cl_fix_is_wide`：它读入一个 `FixFormat`，返回 `True`/`False`，决定该格式该走哪条路径。理解它的判定逻辑与边界值，是读懂所有派发代码的钥匙。

#### 4.2.2 核心流程

判定逻辑可以浓缩为一行：

\[ \text{is\_wide}(\textit{fmt}) \iff \text{width}(\textit{fmt}) > 53 \iff S + I + F > 53 \]

但源码注释解释了「为什么是 53、为什么用总位宽」的两层推理：

1. **理论上**：float64 能精确表示 54 位有符号数（1 位符号 + 53 位幅度）和 53 位无符号数。所以仅就「能否精确表示」而言，幅度部分 \(I + F \le 53\) 即可，判定式本可以是 `I + F > 53`。
2. **实际上**：有符号数在**回绕**（`None_s`/`Warn_s`，关闭饱和时）需要做 \(\text{val} + 2^{I}\) 这样的偏移运算，多需要 1 个整数位的余量才不容易溢出 float64。为了给有符号回绕留出这个安全余量，并让有符号/无符号用**同一条统一的 53 位规则**，实现选择了更保守的「总位宽 > 53」。

代价是：有符号格式会比「理论极限」早 1 位就切到 wide 路径（略微牺牲一点速度），换来了回绕运算的安全性。即便如此，`cl_fix_resize` 的 narrow 回绕分支里还有一道二次保险（见 4.3.3）。

#### 4.2.3 源码精读

- [python/src/en_cl_fix_pkg/en_cl_fix_pkg.py:L23-L38](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L23-L38) —— `cl_fix_is_wide` 的完整实现与详细注释。注释依次说明了 float64 的位数构成、\([-2^{53}, 2^{53}]\) 的精确表示范围、理论上 `I+F > 53` 即可、以及为简化有符号回绕而改用 `width > 53` 的取舍。最后一行 `return cl_fix_width(fmt) > 53` 是真正的判定。

`cl_fix_is_wide` 在库里被反复调用，是一切派发的入口。例如 `cl_fix_max_value` / `cl_fix_min_value` 也用它决定返回 wide_fxp 还是 float：

- [python/src/en_cl_fix_pkg/en_cl_fix_pkg.py:L43-L56](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L43-L56) —— 宽格式时返回 `wide_fxp.MaxValue/MinValue`（大整数），否则返回 float64 实数值。这保证了后续比较/饱和逻辑在两条路径下类型一致。

#### 4.2.4 代码实践

**实践目标**：亲手验证 53 位边界的精确取值，特别是有符号格式比无符号「早 1 位」切到 wide。

**操作步骤**：

```python
from en_cl_fix_pkg import *

# 无符号：width = IntBits + FracBits
print(cl_fix_is_wide(FixFormat(False, 53, 0)))   # False  (width=53，恰好不宽)
print(cl_fix_is_wide(FixFormat(False, 54, 0)))   # True   (width=54)

# 有符号：width = 1 + IntBits + FracBits
print(cl_fix_is_wide(FixFormat(True, 52, 0)))    # False  (width=53)
print(cl_fix_is_wide(FixFormat(True, 53, 0)))    # True   (width=54)
```

**需要观察的现象**：无符号 53 位仍属 narrow、54 位才 wide；有符号「IntBits=52」(总宽 53) 是 narrow、「IntBits=53」(总宽 54) 就 wide——比无符号的 IntBits 阈值小 1。

**预期结果**：依次输出 `False / True / False / True`。

#### 4.2.5 小练习与答案

**练习 1**：`FixFormat(True, -2, 56)` 是 wide 还是 narrow？

**答案**：`width = 1 + (-2) + 56 = 55 > 53`，所以是 wide。注意负的 IntBits 照样计入总位宽（见 u1-l2）。

**练习 2**：为什么源码注释说「理论上 `return fmt.IntBits + fmt.FracBits > 53` 就够了」，但实际却用了 `cl_fix_width(fmt) > 53`？

**答案**：理论上 float64 能精确表示 54 位有符号数（符号位 + 53 位幅度），所以 `I + F > 53` 对「能否精确表示」已足够。但实际用总位宽（含符号位）`> 53` 是为了给有符号数的**回绕运算**（`val + 2^IntBits`）预留一个整数位余量，避免回绕本身在 float64 里溢出，并让有符号/无符号共用同一条规则。

---

### 4.3 cl_fix_from_real 与 cl_fix_resize 中的 wide 自动派发

#### 4.3.1 概念说明

`cl_fix_is_wide` 只是裁判，真正「派发」发生在每个公共函数的开头。本模块追踪两个最核心的入口——`cl_fix_from_real`（把实数装进定点）与 `cl_fix_resize`（改变定点格式，整个库的心脏）——看它们如何根据格式自动选择路径。

派发有三条触发规则：

1. **结果格式宽**（`cl_fix_is_wide(rFmt)`）→ 走 wide。
2. **任一操作数已是 `wide_fxp`**（`type(a) == wide_fxp`）→ 走 wide（运算函数才有）。
3. **中间计算可能丢精度**（如 narrow 回绕的偏移加法超出 53 位）→ 临时切到 wide 整数算完再切回 narrow。

#### 4.3.2 核心流程

`cl_fix_from_real` 的派发（最简单）：

```
def cl_fix_from_real(a, rFmt, saturate=SatWarn_s):
    if cl_fix_is_wide(rFmt):
        return wide_fxp.FromFloat(a, rFmt, saturate)   # wide 分支
    else:
        # narrow 分支：float64 上 half-up 量化 + 饱和
        ...
```

`cl_fix_resize` 的派发（多了一条「结果窄就切回 narrow」的收尾）：

```
def cl_fix_resize(a, aFmt, rFmt, rnd, sat):
    if type(a) == wide_fxp or cl_fix_is_wide(rFmt):
        a = wide_fxp.FromFxp(a, aFmt)      # 统一转成 wide_fxp
        result = a.resize(rFmt, rnd, sat)  # 在大整数上做舍入/饱和
        if not cl_fix_is_wide(rFmt):
            result = result.to_narrow_fxp() # 结果格式窄 -> 切回 float64
    else:
        ... # narrow 分支：float64 上做舍入/饱和
    return result
```

注意第二个 `if` 的精妙之处：**输入是 wide、但结果格式是 narrow**（例如把一个大数 resize 到小格式），会先在 wide 上算（保证中间不丢精度），最后用 `to_narrow_fxp` 切回 float64。这正是「该宽则宽、能窄则窄」。

#### 4.3.3 源码精读

`cl_fix_from_real` 的派发——一行 `if` 决定路径：

- [python/src/en_cl_fix_pkg/en_cl_fix_pkg.py:L149-L171](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L149-L171) —— 宽格式时第 151–152 行直接委托 `wide_fxp.FromFloat`；narrow 分支（153 行起）在 float64 上做 half-up 量化（`np.floor(a*2**F + 0.5)/2**F`）与饱和。两条路径的量化方式一致，是位真一致性的保证。

`cl_fix_resize` 的派发——典型的「转 wide → 算 → 按需切回 narrow」三段式：

- [python/src/en_cl_fix_pkg/en_cl_fix_pkg.py:L190-L200](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L190-L200) —— 第 193 行 `if type(a) == wide_fxp or cl_fix_is_wide(rFmt)` 是派发条件；195 行 `wide_fxp.FromFxp` 把任意输入统一成 wide_fxp；199–200 行在结果格式窄时用 `to_narrow_fxp` 切回 float64。

narrow 回绕分支里的「二次保险」——即便结果格式是 narrow，回绕的偏移加法若会溢出 53 位，也会临时切到 wide 整数算：

- [python/src/en_cl_fix_pkg/en_cl_fix_pkg.py:L245-L263](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L245-L263) —— 第 250 行 `convertToWide = cl_fix_is_wide(addFmt)` 判定回绕所需的中间加法格式 `addFmt` 是否超 53 位；若超，则第 254–263 行在 `dtype=object` 大整数上做取模回绕，再除回 float64。这是规则 3 的实例，也印证了 4.2 中「为有符号回绕留余量」的设计。

运算函数（加/减/乘）共用同一套派发模式——「任一操作数是 wide 或中间格式宽，就整段走 wide」：

- [python/src/en_cl_fix_pkg/en_cl_fix_pkg.py:L332-L334](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L332-L334) —— `cl_fix_add` 的派发条件 `type(a)==wide_fxp or type(b)==wide_fxp or cl_fix_is_wide(midFmt)`，命中则把两个操作数都转成 wide_fxp 再算。
- [python/src/en_cl_fix_pkg/en_cl_fix_pkg.py:L435-L437](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L435-L437) —— `cl_fix_mult` 同款派发。乘法的中间格式 `ForMult` 位宽 = 两操作数位宽之和，很容易超 53，故乘法比加减更频繁地走 wide。

#### 4.3.4 代码实践

**实践目标**：构造一个 60 位格式，用 `cl_fix_from_real` 写入大数，观察派发到 `wide_fxp.FromFloat`、打印内部 `data`，并与 `to_narrow_fxp` 的浮点值对比，说明精度差异。（本实践对应讲义规格指定的任务。）

**操作步骤**：

```python
from en_cl_fix_pkg import *

fmt = FixFormat(False, 40, 20)          # width = 0+40+20 = 60 > 53  -> wide
print("width:", cl_fix_width(fmt), "is_wide:", cl_fix_is_wide(fmt))

# 写入一个格式内的较大实数（无符号上限 ≈ 2**40）
x = cl_fix_from_real(2.0**40, fmt, FixSaturate.SatWarn_s)
print(type(x))           # <class '...wide_fxp'>  <- 经 cl_fix_is_wide 派发到 FromFloat
print(x.data)            # 大整数，例如 [1152921504606846976] (= 2**60)，精确
print(x.to_narrow_fxp()) # [1.099511627776e+12] (= 2**40)，float64 近似值
```

**需要观察的现象与精度差异说明**：

- `type(x)` 是 `wide_fxp`，证实派发发生：`cl_fix_is_wide(fmt)` 为 `True` → 第 151 行委托 `wide_fxp.FromFloat`。
- `x.data` 是精确的大整数 \(2^{40} \times 2^{20} = 2^{60} = 1152921504606846976\)（61 位）。**float64 无法精确表示这个整数**（上限是 \(2^{53}\)），这正是必须用 wide 存储的原因。
- `x.to_narrow_fxp()` 把 `data` 除以 \(2^{20}\) 还原为实数 \(2^{40} = 1099511627776.0\)。对这个特定格式，实数值 \(2^{40} < 2^{53}\)，所以 float64 仍能精确持有，看起来「没损失」。

**关键结论**：对 `(False,40,20)` 这类**实数值幅度 < 2^53** 的格式，`to_narrow_fxp` 恰好不丢精度；但**内部 `data` 仍需 wide 存储**，因为 `data` 本身（达 \(2^{60}\)）已远超 float64 精度。真正会在 `to_narrow_fxp` 丢精度的是**实数值幅度本身 ≥ 2^53** 的格式（见 4.4 的对比实践）。上述打印的具体整数值建议本地运行确认。

#### 4.3.5 小练习与答案

**练习 1**：`cl_fix_resize` 的 wide 分支里，为什么最后还要判断 `if not cl_fix_is_wide(rFmt): result = result.to_narrow_fxp()`？

**答案**：因为输入可能是 wide_fxp、但结果格式 `rFmt` 是 narrow。这时中间计算必须走 wide（保证精度），但最终结果应按结果格式的「宽度」返回——narrow 格式就该返回 float64，这样调用方拿到的类型与「直接用 narrow 算」一致。

**练习 2**：两个 narrow 的操作数相乘，结果会走 wide 吗？

**答案**：可能。`cl_fix_mult` 的派发条件是 `cl_fix_is_wide(midFmt)`，而乘积中间格式 `ForMult` 的位宽 = 两操作数位宽之和。例如两个 30 位格式相乘得到 60 位中间格式 > 53，就会触发 wide 路径，即使两个输入本身都是 narrow。

---

### 4.4 wide_fxp 的构造与转换桥：FromFloat / FromNarrowFxp / FromFxp / to_narrow_fxp

#### 4.4.1 概念说明

派发一旦决定走 wide，就需要在「float 实数域」与「wide_fxp 大整数域」之间来回搬运。`wide_fxp` 提供了四个静态/成员方法充当「桥梁」：

| 方法 | 方向 | 是否做量化/边界检查 | 典型用途 |
|------|------|------------------|---------|
| `FromFloat(a, rFmt, saturate)` | float → wide | 是（half-up 量化 + 饱和） | 用户入口，`cl_fix_from_real` 的 wide 后端 |
| `FromNarrowFxp(data, fmt)` | float 数组 → wide | 否（仅 `floor`） | 内部把 narrow 数组升级成 wide |
| `FromFxp(x, fmt)` | float 或 wide → wide | 否 | 派发代码的「统一入口」，自动判断输入类型 |
| `to_narrow_fxp()` | wide → float | 否（可能丢精度） | 把 wide 结果切回 float64 |

#### 4.4.2 核心流程

`FromFloat`（用户级，带量化与饱和）：

```
x = floor(a * 2**FracBits + 0.5)     # half-up 量化成大整数
if 需要 Sat:  x = clip(x, MinValue, MaxValue)
return wide_fxp(x, rFmt)
```

`FromNarrowFxp`（内部，无边界检查）：

```
int_data = floor(data * 2**FracBits)  # 直接放大成整数
return wide_fxp(int_data, fmt)
```

`FromFxp`（派发器统一入口）：

```
if type(x) == wide_fxp:
    assert x.fmt == fmt
    return x                  # 已是 wide，格式匹配就原样返回
else:
    return FromNarrowFxp(x, fmt)  # 否则当成 narrow 数组升级
```

`to_narrow_fxp`（wide → float，可能丢精度）：

```
return (data / 2**FracBits).astype(float)
```

#### 4.4.3 源码精读

`FromFloat`——量化、饱和、告警俱全的用户入口：

- [python/src/en_cl_fix_pkg/wide_fxp.py:L54-L80](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L54-L80) —— 第 71 行 `(a*(2.0**rFmt.FracBits)+0.5).astype('object')` 把 float 放大并转成 `object`（大整数）dtype，第 72 行 `np.floor` 完成 half-up 量化；第 76–78 行按 `saturate` 用 `np.where` clip 到 `MaxValue/MinValue`。这与 `cl_fix_from_real` narrow 分支的 `np.floor(a*2**F + 0.5)` 量化方式完全对应，保证两路径位真一致。

`FromNarrowFxp`——最朴素的 float→int 升级，无边界检查：

- [python/src/en_cl_fix_pkg/wide_fxp.py:L84-L90](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L84-L90) —— 断言输入是 `float`，`(data*2**FracBits).astype(object)` 后 `floor`，得到未归一化大整数。

`FromFxp`——派发器的统一入口，自动判别输入类型：

- [python/src/en_cl_fix_pkg/wide_fxp.py:L94-L100](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L94-L100) —— 若输入已是 `wide_fxp` 则断言格式匹配后原样返回（避免重复转换）；否则委托 `FromNarrowFxp`。`cl_fix_resize`、`cl_fix_add` 等的 wide 分支统一调用它。

`to_narrow_fxp`——wide→float 的出口，注释明确「不做范围/精度检查」：

- [python/src/en_cl_fix_pkg/wide_fxp.py:L168-L171](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L168-L171) —— `self._data / 2.0**self._fmt.FracBits` 后 `.astype(float)`。当 `data` 的有效位 > 53 时，这一步会丢低位。带精度告警的包装版本见 `to_float`（第 160–165 行），它在 `data` 超过 \(2^{52}\)/\(2^{53}\) 时发警告。

#### 4.4.4 代码实践

**实践目标**：用四个桥梁方法做一次完整的「float → wide → float」往返，并构造一个**实数值幅度 ≥ 2^53** 的场景，亲眼看到 `to_narrow_fxp` 丢精度。

**操作步骤**：

```python
from en_cl_fix_pkg import *
import numpy as np

# 1) FromNarrowFxp：把 float 数组升级成 wide（无边界检查）
narrow_arr = np.array([1.25, -0.5, 3.0])
fmt = FixFormat(True, 4, 4)
w = wide_fxp.FromNarrowFxp(narrow_arr, fmt)
print(w.data)               # [20, -8, 48]   <- 1.25*16, -0.5*16, 3.0*16

# 2) FromFxp：已是 wide 且 fmt 匹配 -> 原样返回；否则走 FromNarrowFxp
print(wide_fxp.FromFxp(w, fmt).data)         # [20, -8, 48]
print(wide_fxp.FromFxp(narrow_arr, fmt).data) # [20, -8, 48]（等价于 FromNarrowFxp）

# 3) to_narrow_fxp：除以 2**FracBits 还原为 float
print(w.to_narrow_fxp())    # [1.25, -0.5, 3.0]

# 4) 真正丢精度的场景：实数值幅度 >= 2**53，用 from_bits_as_int 注入 >53 位有效整数
big = cl_fix_from_bits_as_int(2**54 + 1, FixFormat(False, 60, 0))
print(big.data)             # 18014398509481985 (= 2**54 + 1)，精确
print(big.to_narrow_fxp())  # 18014398509481984.0 (= 2**54)，丢掉了 +1
```

**需要观察的现象**：

- 步骤 1：`FromNarrowFxp` 把 `[1.25, -0.5, 3.0]` 变成 `[20, -8, 48]`，正是各值乘以 \(2^4\)。
- 步骤 4：`big.data` 精确持有 \(2^{54}+1 = 18014398509481985\)；而 `to_narrow_fxp()` 输出 \(18014398509481984.0 = 2^{54}\)，`+1` 被 float64 吞掉了。这就是「wide 能精确、narrow 会丢」的最直观对照。

**预期结果**：步骤 4 中 `big.data` 末位是 `5`、`to_narrow_fxp()` 末位是 `4`，二者相差 1，证明 float64 在 \(2^{54}\) 量级无法分辨个位。

> 说明：步骤 4 用 `cl_fix_from_bits_as_int` 而非 `cl_fix_from_real` 注入大整数，是因为 float64 字面量本身无法精确表达 \(2^{54}+1\)（ULP 为 4），只能通过整数入口绕开 float。这也正是 wide 路径的价值所在。

#### 4.4.5 小练习与答案

**练习 1**：`FromFloat` 与 `FromNarrowFxp` 都把 float 转成 wide 大整数，它们的差别是什么？

**答案**：`FromFloat` 是用户级入口，做完整的 half-up 量化（`+0.5` 再 `floor`）、饱和夹紧与越界告警；`FromNarrowFxp` 是内部转换，只做 `floor`（向下取整），不做任何边界或饱和检查。前者用于「把任意实数安全装入定点」，后者用于「已知合法的 narrow 数据升级到 wide」。

**练习 2**：为什么 `FromFxp` 对已是 `wide_fxp` 的输入要求 `x.fmt == fmt`，不匹配就断言失败？

**答案**：因为 wide_fxp 的内部 `data` 是**未归一化**的大整数，其含义完全依赖 `fmt.FracBits`。如果输入的格式与目标 `fmt` 不同，小数点位置就不同，直接复用 `data` 会得到错误数值。所以必须格式一致才能原样返回，否则应让调用方先 `resize`。

---

## 5. 综合实践

把本讲四个模块串起来，做一次「同一数值、不同路径」的对照追踪。

**任务**：对实数 `a = 1.25`，分别在 narrow 与 wide 两种格式下走完 `from_real → resize → to_narrow_fxp` 的全链路，记录每一步的类型与内部值，并解释差异。

**操作步骤**：

```python
from en_cl_fix_pkg import *

a = 1.25

# 路径 A：narrow（6 位格式）
fmtN = FixFormat(False, 2, 4)
vN = cl_fix_from_real(a, fmtN)                       # float64 1.25
rN = cl_fix_resize(vN, fmtN, FixFormat(False, 2, 2))  # 仍 float64，Trunc 截 2 位小数
print("A type:", type(rN).__name__, "value:", rN)

# 路径 B：wide（56 位格式，width = 0+40+16 = 56 > 53）
fmtW = FixFormat(False, 40, 16)
vW = cl_fix_from_real(a, fmtW)                        # wide_fxp，data = 1.25*2**16 = 81920
print("B data:", vW.data)
rW = cl_fix_resize(vW, fmtW, FixFormat(False, 40, 8))  # wide_fxp，Trunc 截 8 位小数
print("B after resize data:", rW.data)
print("B to_narrow:", rW.to_narrow_fxp())             # 切回 float64
```

**需要观察与解释**：

1. 路径 A 全程是 `numpy.ndarray`（float64），因为 `cl_fix_is_wide(fmtN)` 为 `False`。
2. 路径 B 全程是 `wide_fxp`：`vW.data = [81920]`（\(1.25 \times 2^{16}\)）；resize 后小数位从 16 减到 8，`Trunc_s` 等价于右移 8 位，`data` 变为 `81920 >> 8 = 320`（即 \(1.25 \times 2^{8}\)，因 1.25 恰好截位无损）。
3. 最后 `to_narrow_fxp()` 把 `320 / 2**8 = 1.25` 还原回 float64。
4. 两条路径最终实数值一致（都还原到 `1.25`），印证 narrow 与 wide 的**位真一致性**——只是内部表示不同。

**预期结果**：路径 A 输出 float64 的 `1.25`；路径 B 的 `data` 从 `81920` 变为 `320`，`to_narrow_fxp()` 同样得到 `1.25`。具体整数值建议本地运行确认。

> 进阶：把路径 B 的 resize 改成 `FixRound.NonSymPos_s`，观察 `data` 是否多出 1 个 LSB 的偏移（对应 u3-l2 讲的「+半格再截断」）。

## 6. 本讲小结

- Python 实现有 **narrow（float64）** 与 **wide（任意精度大整数）** 两条路径；wide 因大整数运算而更慢，但能支持 >53 位格式，保证位真正确性。
- 裁判函数 **`cl_fix_is_wide(fmt)`** 的判定是 `width(fmt) > 53`，即 \(S+I+F > 53\)；用总位宽而非理论上的 `I+F > 53`，是为了给有符号回绕预留安全余量并统一规则。
- **`cl_fix_from_real`** 在结果格式宽时一行委托 `wide_fxp.FromFloat`；**`cl_fix_resize`** 用「转 wide → 算 → 按需 `to_narrow_fxp` 切回」三段式，加减乘则用「任一操作数宽或中间格式宽就整段走 wide」。
- 即便结果格式是 narrow，narrow 回绕分支仍会用 `cl_fix_is_wide(addFmt)` 二次判定，必要时临时切到大整数算取模，避免 float64 溢出。
- **wide_fxp 内部存「未归一化大整数」**（如 `1.25` 在 `(0,2,4)` 存为 `20`），小数点位置由 `fmt.FracBits` 隐含；四个桥梁方法 `FromFloat`（带量化+饱和）、`FromNarrowFxp`（仅 floor）、`FromFxp`（自动判别）、`to_narrow_fxp`（可能丢精度）在两域间搬运数据。
- float64 只能精确到 \(2^{53}\)：内部 `data` 超 \(2^{53}\) 时必须 wide 存储；当**实数值幅度本身** ≥ \(2^{53}\) 时，`to_narrow_fxp` 会真正丢低位。

## 7. 下一步学习建议

- **u6-l2 wide_fxp 任意精度类设计**：本讲只讲了 wide_fxp 的构造与转换桥，下一讲深入它的 `resize`（整数位运算实现舍入/回绕）、`__add__/__sub__/__mul__`（经 `AlignBinaryPoints` 对齐小数点）、`__array_function__`（让 `np.where`/`np.array_equal` 作用于 wide_fxp）以及 `to_uint64_array`/`FromUint64Array`（与 MATLAB 交换大位宽数据）。
- **重读 u3-l2 / u3-l3**：带着本讲的 narrow/wide 视角回看 `cl_fix_resize` 的舍入与饱和，会发现 narrow 分支用 `np.floor`、wide 分支用 `>>`，两套实现一一对应。
- **阅读 `wide_fxp.py` 的 `resize` 方法**（第 212–289 行）：对照本讲的派发逻辑，理解 wide 路径下舍入如何用「加偏移 + 右移」、回绕如何用「取模」在大整数上完成。
