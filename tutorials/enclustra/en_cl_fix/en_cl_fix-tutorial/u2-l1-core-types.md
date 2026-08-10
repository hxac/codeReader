# 核心类型：FixFormat、FixRound、FixSaturate

## 1. 本讲目标

本讲聚焦 en_cl_fix 的 Python 实现中三个最基础的数据类型。学完后你应该能够：

- 说出 `FixFormat` 三字段 `[S, I, F]` 的含义，并解释构造时的两条断言约束（`S` 只能取 0/1、`I+F >= 0`）。
- 判断一个给定的 `(S, I, F)` 组合是否合法、何时会触发断言、位宽是多少。
- 枚举 7 种 `FixRound` 舍入模式与 4 种 `FixSaturate` 饱和模式，并说明它们各自的语义。
- 理解 `width` 属性以及 `__repr__` / `__str__` / `__eq__` 这三个对象协议的作用。

本讲只讲「类型本身」，不讲类型上的运算逻辑。类型对应的舍入/饱和实现细节留到单元 4，结果格式推导（`for_add`/`for_mult` 等）留到单元 3。

## 2. 前置知识

在进入本讲前，你需要具备以下基础（来自 u1-l2）：

- **定点数格式 `[S, I, F]`**：`S` 是符号位个数（0 表示无符号，1 表示二进制补码有符号），`I` 是整数位个数，`F` 是小数位个数。总位宽是三者和。
- **位权（bit weight）**：第 \(k\) 位的权重为 \(2^{k-F}\)；有符号数的符号位权重是 \(-2^{I}\)。
- **负的 I 或 F 是允许的**：它表示小数点落在物理位之外，存在「隐含为零的缺失位」，会改变粒度与范围，但不改变位宽计算公式。
- **舍入与饱和分别在何时发生**：`F`（小数位）变少时需要舍入；`I`（整数位）或 `S`（符号位）变少时需要饱和。

此外，你需要一点点 Python 基础：

- **枚举（`enum.Enum`）**：一种带名字的整数常量集合，例如 `FixRound.Trunc_s` 的值是 `0`。
- **属性（`@property`）**：用「像字段一样访问」的方式调用一个方法，例如 `fmt.width` 实际上是调用一个函数。
- **断言（`assert`）**：当条件为假时立即抛出 `AssertionError` 并中止，常用于「构造时就拦截非法输入」。

## 3. 本讲源码地图

本讲几乎全部内容都集中在一个文件里：

| 文件 | 作用 |
| --- | --- |
| [bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py) | 定义三大核心类型：`FixRound`、`FixSaturate`、`FixFormat`。本讲的主角。 |

辅助参考（非精读对象，仅用来印证用法）：

| 文件 | 作用 |
| --- | --- |
| [bittrue/models/python/en_cl_fix_pkg/__init__.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/__init__.py) | 包门面，通过 `from .en_cl_fix_types import *` 把这三个类型导出，所以外部 `from en_cl_fix_pkg import *` 即可使用。 |
| [README.md](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/README.md) | Rounding Modes / Saturation Modes 两张表，是理解枚举语义的最佳权威说明。 |
| [bittrue/tests/python/en_cl_fix_pkg_test.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py) | 展示这三个类型在真实调用中的写法（如 `FixFormat(True, 3, 0)`）。 |

> 小提示：`en_cl_fix_types.py` 里还定义了一批 `FixFormat.for_add` / `for_mult` / `for_shift` 等**静态方法**，它们负责「推导运算结果的格式」，属于单元 3 的内容，本讲只会一带而过。

---

## 4. 核心概念与源码讲解

### 4.1 FixFormat：定点格式的载体与断言约束

#### 4.1.1 概念说明

`FixFormat` 是整个库的「格式身份证」。任何一个定点数，无论是输入、中间结果还是输出，都必须先声明它的格式 `[S, I, F]`，库才能正确解读它的二进制位。

它解决的问题是：**同样的 16 个比特，在 `[0,11,5]` 和 `[1,15,0]` 下代表完全不同的数值**。所以必须有一个类型把「这串比特怎么读」这件事明确写下来。

`FixFormat` 不存储数值本身，只存储「如何解读数值」的三个整数。

#### 4.1.2 核心流程

构造一个 `FixFormat(S, I, F)` 时，库会做两道校验，**任何一道不过就立刻断言失败**：

1. 检查 `S`：必须是 `0` 或 `1`。
2. 检查 `I + F`：必须 `>= 0`。

校验通过后，把三个参数转成 `int` 存下来。整个过程可以用伪代码表示：

```
FixFormat(S, I, F):
    若 S 不是 0 也不是 1  →  报错 "S must be 0 or 1"
    若 I + F < 0          →  报错 "I+F must be at least 0"
    self.S = int(S)
    self.I = int(I)
    self.F = int(F)
```

为什么是这两条约束？关键直觉是：**位宽 \(\text{width} = S + I + F\) 在物理上必须非负**，但库进一步收紧成 `I + F >= 0`。这样做的目的是排除一批「位宽虽然非负、但没有实际用途且会产生别扭边界」的格式，详见下一节的源码注释。

#### 4.1.3 源码精读

类型的文档字符串点明了三字段含义：

```python
class FixFormat:
    """
    Fixed-point number format, [S, I, F], where:
        S = Number of sign bits (0 or 1).
        I = Number of integer bits.
        F = Number of fractional bits.
    """
```

> 见 [en_cl_fix_types.py:53-59](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L53-L59)：定义 `FixFormat` 类并说明 `[S,I,F]`。

构造函数与两条断言如下：

```python
def __init__(self, S : int, I : int, F : int):
    assert S == 0 or S == 1, "S must be 0 or 1"
    # We allow unsigned null formats such as: (0,0,0) or (0,-5,5).
    # We allow signed sign-bit only such as:  (1,0,0) or (1,-5,5).
    # We do not allow signed null formats such as (1,-1,0) or negative widths such as (0,-1,0)
    # as they create awkward edge cases (e.g. in cl_fix_max_value) and have no practical use.
    assert I+F >= 0, "I+F must be at least 0"
    self.S = int(S)
    self.I = int(I)
    self.F = int(F)
```

> 见 [en_cl_fix_types.py:61-70](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L61-L70)：构造函数，含两条断言与三字段赋值。

这段注释把「允许 / 不允许」的边界讲得非常清楚，值得逐条对照：

| 组合 | 是否允许 | 原因 |
| --- | --- | --- |
| `(0,0,0)` | ✅ 允许 | 无符号空格式，`I+F=0` |
| `(0,-5,5)` | ✅ 允许 | 无符号空格式，`I+F=0`，位宽为 0 |
| `(1,0,0)` | ✅ 允许 | 仅一个符号位，位宽为 1 |
| `(1,-5,5)` | ✅ 允许 | 「符号位 + 负整数位」，`I+F=0`，位宽为 1 |
| `(1,-1,0)` | ❌ 拒绝 | 有符号空格式，`I+F=-1`，会产生别扭边界 |
| `(0,-1,0)` | ❌ 拒绝 | 负位宽，`I+F=-1` |

注意一个隐藏细节：断言写的是 `S == 0 or S == 1`，而赋值是 `self.S = int(S)`。由于 Python 中 `True == 1`、`False == 0`，所以传 `FixFormat(True, 3, 0)` 也是合法的——测试文件正是这么写的（见 [en_cl_fix_pkg_test.py:34-40](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py#L34-L40)），`int(True)` 会被规整成 `1`。

#### 4.1.4 代码实践

**实践目标**：亲手验证两条断言在「哪些组合」上触发、在「哪些组合」上放行。

**操作步骤**（示例代码，保存为 `try_fixformat.py`，放在仓库根目录运行）：

```python
# 示例代码：探索 FixFormat 的合法/非法边界
import sys
sys.path.append("bittrue/models/python")
from en_cl_fix_pkg import FixFormat

legal = [
    (0, 0, 0),    # 无符号空格式
    (0, -5, 5),   # 无符号空格式，I+F=0
    (1, 0, 0),    # 仅符号位
    (1, -2, 4),   # 负整数位，README 中范围 -0.25..+0.1875
    (1, 4, -2),   # 负小数位，README 中范围 -16..12
    (True, 3, 0), # 用 bool 当 S
]

illegal = [
    (2, 0, 0),    # S 不是 0/1
    (1, -1, 0),   # 有符号空格式，I+F=-1
    (0, -1, 0),   # 负位宽，I+F=-1
]

for s, i, f in legal:
    fmt = FixFormat(s, i, f)
    print(f"OK   FixFormat({s},{i},{f}) -> width={fmt.width}, repr={fmt!r}")

for s, i, f in illegal:
    try:
        FixFormat(s, i, f)
        print(f"???  FixFormat({s},{i},{f}) -> 未报错（意外）")
    except AssertionError as e:
        print(f"FAIL FixFormat({s},{i},{f}) -> 断言: {e}")
```

**需要观察的现象**：合法用例正常构造并打印位宽；非法用例各自命中对应的断言。

**预期结果**（基于源码推导；精确文本待本地验证）：

```
OK   FixFormat(0,0,0) -> width=0, repr=FixFormat(0, 0, 0)
OK   FixFormat(0,-5,5) -> width=0, repr=FixFormat(0, -5, 5)
OK   FixFormat(1,0,0) -> width=1, repr=FixFormat(1, 0, 0)
OK   FixFormat(1,-2,4) -> width=3, repr=FixFormat(1, -2, 4)
OK   FixFormat(1,4,-2) -> width=3, repr=FixFormat(1, 4, -2)
OK   FixFormat(True,3,0) -> width=4, repr=FixFormat(1, 3, 0)
FAIL FixFormat(2,0,0) -> 断言: S must be 0 or 1
FAIL FixFormat(1,-1,0) -> 断言: I+F must be at least 0
FAIL FixFormat(0,-1,0) -> 断言: I+F must be at least 0
```

> 注意：`True` 经 `int()` 后变成 `1`，所以 `repr` 显示 `FixFormat(1, 3, 0)`。

#### 4.1.5 小练习与答案

**练习 1**：`FixFormat(1, -3, 2)` 是否合法？位宽是多少？

**参考答案**：合法。`S=1` 满足第一条断言；`I+F = -3+2 = -1 < 0` ❌ —— 实际上**不合法**，会触发 `I+F must be at least 0`。位宽公式虽为 `1-3+2 = 0`，但库拒绝这种「有符号空格式」。**这是一道陷阱题**，请对照源码注释里的 `(1,-1,0)` 例子理解。

**练习 2**：`FixFormat(1, -3, 3)` 是否合法？位宽是多少？

**参考答案**：合法。`I+F = 0 >= 0` 通过。位宽 \(= 1 + (-3) + 3 = 1\)，属于「符号位 + 负整数位」一类。

---

### 4.2 FixRound：七种舍入模式

#### 4.2.1 概念说明

当定点数的**小数位 `F` 变少**时（例如把 `[1,2,2]` 截到 `[1,2,0]`），被丢掉的那几位携带的信息必须用某种规则处理，这就是「舍入」。en_cl_fix 把业界常用的舍入策略提炼成 7 个命名常量，集中在 `FixRound` 枚举里。

关键直觉：**舍入的差别只在「平局（tie，即恰好 0.5）怎么取舍」**。除截断外，所有模式在非平局时行为一致，只在 `0.5` 这种半数位上分道扬镳。这也正是为什么 README 说「其余模式与 `NonSymPos_s` 的区别仅在于平局处理」。

#### 4.2.2 核心流程

`FixRound` 是一个纯枚举，没有校验逻辑，构造流程就是「从 7 个名字里选一个」：

```
FixRound.Trunc_s      = 0   # 截断，等价 floor(x)
FixRound.NonSymPos_s  = 1   # 半数向上（最常用），等价 floor(x+0.5)
FixRound.NonSymNeg_s  = 2   # 半数向下
FixRound.SymInf_s     = 3   # 对称地向 ±∞
FixRound.SymZero_s    = 4   # 对称地向 0
FixRound.ConvEven_s   = 5   # 收敛到偶数
FixRound.ConvOdd_s    = 6   # 收敛到奇数
```

调用方（如后续的 `cl_fix_round`）拿到这个枚举后，用一个分支决定加多少偏移再截断。本讲只需记住「它是一个 7 选 1 的标签」。

#### 4.2.3 源码精读

枚举定义本身极其简洁：

```python
class FixRound(Enum):
    """
    Fixed-point rounding modes.
    """
    Trunc_s = 0         # Truncation (no rounding).
    NonSymPos_s = 1     # Non-symmetric positive (half-up).
    NonSymNeg_s = 2     # Non-symmetric negative (half-down).
    SymInf_s = 3        # Symmetric towards +/- infinity.
    SymZero_s = 4       # Symmetric towards 0.
    ConvEven_s = 5      # Convergent towards even number.
    ConvOdd_s = 6       # Convergent towards odd number.
```

> 见 [en_cl_fix_types.py:30-40](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L30-L40)：7 种舍入模式枚举。

每个名字的尾缀 `_s` 是为了和 VHDL 端的命名保持一致（VHDL 里同名常量也叫 `Trunc_s`），这正体现了「三语言 API 同构」的设计。语义最权威的解释在 README 的示例表里——把同一组值（2.2、2.7、-1.5、-0.5、0.5、1.5）舍入到 `[1,2,0]`，7 种模式给出不同结果：

> 见 [README.md:123-167](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/README.md#L123-L167)：舍入模式对照表。例如 `0.5` 在 `NonSymPos_s` 下变 `1`、在 `SymZero_s` 下变 `0`、在 `ConvEven_s` 下变 `0`。

从这张表能一眼看出：`Trunc_s` 误差最大但最省硬件；`NonSymPos_s` 是最常用的通用模式但带统计偏置；需要无偏舍入时通常选 `ConvEven_s` 或 `ConvOdd_s`。

#### 4.2.4 代码实践

**实践目标**：用 Python 内置的 `list(FixRound)` 把 7 个模式全部列出来，并验证它们确实是 `Enum` 成员。

**操作步骤**（示例代码）：

```python
# 示例代码：枚举 FixRound 的全部成员
import sys
sys.path.append("bittrue/models/python")
from en_cl_fix_pkg import FixRound

for r in FixRound:
    print(f"{r.name} = {r.value}")
```

**需要观察的现象**：打印出 7 行，名字与数值一一对应，顺序与源码定义顺序一致。

**预期结果**：

```
Trunc_s = 0
NonSymPos_s = 1
NonSymNeg_s = 2
SymInf_s = 3
SymZero_s = 4
ConvEven_s = 5
ConvOdd_s = 6
```

> 这一节我们不实际执行舍入运算——那需要 `cl_fix_round` 函数（单元 4）。本实践只是确认「7 个标签确实存在且可遍历」。

#### 4.2.5 小练习与答案

**练习 1**：README 表中，`0.5` 在哪几种模式下会舍入到 `1`？

**参考答案**：`NonSymPos_s`（→1）、`SymInf_s`（→1，向 +∞）、`ConvOdd_s`（→1，收敛到奇数）。其余模式（`Trunc_s`→0、`NonSymNeg_s`→0、`SymZero_s`→0、`ConvEven_s`→0）都舍到 0。

**练习 2**：为什么说所有非截断模式「只在平局上有差别」？

**参考答案**：当被丢弃部分严格大于 0.5 时，所有非截断模式都进位；严格小于 0.5 时都舍去；唯独恰为 0.5（tie）时，各模式按各自规则（向 +∞、向 0、向偶、向奇…）给出不同结果。

---

### 4.3 FixSaturate：四种饱和模式

#### 4.3.1 概念说明

当定点数的**整数位 `I` 或符号位 `S` 变少**时（例如把 `[1,4,2]` 压到 `[0,2,2]`，从有符号变成无符号且整数位变少），原值可能落到目标范围之外。这时有两种基本处理：

- **回绕（wrap）**：直接丢弃高位，让数值「溢出后绕回」，像整数取模。
- **饱和（saturate，钳位）**：把超界值强行钳到最近的可表示端点。

此外，无论回绕还是饱和，都可以选择**是否在超界时发出告警**。把「是否饱和」「是否告警」两个独立开关组合，就得到 4 种模式，集中在 `FixSaturate` 枚举里。

#### 4.3.2 核心流程

`FixSaturate` 同样是纯枚举，4 选 1：

```
FixSaturate.None_s    = 0   # 不饱和、不告警（纯回绕）
FixSaturate.Warn_s    = 1   # 不饱和、只告警
FixSaturate.Sat_s     = 2   # 饱和、不告警
FixSaturate.SatWarn_s = 3   # 饱和且告警
```

可以把它理解成一张二维真值表：

| 模式 | 饱和? | 告警? |
| --- | :---: | :---: |
| `None_s` | 否 | 否 |
| `Warn_s` | 否 | 是 |
| `Sat_s` | 是 | 否 |
| `SatWarn_s` | 是 | 是 |

#### 4.3.3 源码精读

```python
class FixSaturate(Enum):
    """
    Fixed-point saturation modes.
    """
    None_s = 0          # No saturation, no warning.
    Warn_s = 1          # No saturation, only warning.
    Sat_s = 2           # Only saturation, no warning.
    SatWarn_s = 3       # Saturation and warning.
```

> 见 [en_cl_fix_types.py:43-50](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L43-L50)：4 种饱和模式枚举。

README 的饱和小节给出了同样的真值表与适用场景说明：

> 见 [README.md:175-188](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/README.md#L175-L188)：饱和模式表。明确「不饱和则丢弃 MSB 造成回绕；启用告警则超界时由仿真器或软件环境发出警告」。

注意命名：`None_s` 中的 `None` 容易和 Python 内置的 `None` 混淆，但加上 `_s` 后缀它就是一个普通枚举成员，不会冲突。

#### 4.3.4 代码实践

**实践目标**：枚举 4 种饱和模式，并对照真值表，用「位运算」理解 `None_s` 与 `Sat_s` 在数值上的差别（不调用库函数）。

**操作步骤**（示例代码）：

```python
# 示例代码：枚举 FixSaturate，并演示回绕 vs 饱和的直觉
import sys
sys.path.append("bittrue/models/python")
from en_cl_fix_pkg import FixSaturate

for s in FixSaturate:
    print(f"{s.name} = {s.value}")

# 直觉演示（纯 Python，不依赖库）：
# 目标格式想象成 3 位无符号整数，范围 0..7
value = 9            # 超出范围
wrap   = value & 0b111      # None_s 风格：丢高位 = 1
clamp  = min(value, 7)      # Sat_s  风格：钳到上界 = 7
print(f"原值 {value} -> 回绕={wrap}, 饱和={clamp}")
```

**需要观察的现象**：枚举打印 4 行；演示中同一个超界值 `9`，回绕得 `1`、饱和得 `7`。

**预期结果**：

```
None_s = 0
Warn_s = 1
Sat_s = 2
SatWarn_s = 3
原值 9 -> 回绕=1, 饱和=7
```

> 真正的库内饱和实现（`cl_fix_saturate`，含符号位处理）在单元 4 讲解。

#### 4.3.5 小练习与答案

**练习 1**：如果你希望在溢出时「既不丢数据正确性、又能让仿真日志暴露问题」，该选哪种模式？

**参考答案**：`SatWarn_s`（饱和且告警）。饱和保证数值钳到最近端点（不回绕出错误值），告警让仿真日志记录下超界事件。

**练习 2**：`Warn_s` 与 `None_s` 在**数值结果**上有差别吗？

**参考答案**：没有。两者都不饱和，都丢弃高位（回绕）。差别仅在于 `Warn_s` 会发出告警、`None_s` 静默。

---

### 4.4 width 属性与对象协议（__repr__ / __str__ / __eq__）

#### 4.4.1 概念说明

`FixFormat` 除了存三字段，还提供几个「让它在 Python 里用起来顺手」的成员：

- **`width` 属性**：返回总位宽 \(S + I + F\)。这是全库最常被查询的属性——库用它判断走 NarrowFix（≤53 位）还是 WideFix 路径。
- **`__repr__`**：官方字符串表示，形如 `FixFormat(1, 4, 8)`，理想情况下能直接复制粘贴成代码。
- **`__str__`**：简短字符串，形如 `(1, 4, 8)`，便于日志阅读。
- **`__eq__`**：判等，三个字段全相等才相等。这让 `FixFormat(1,4,8) == FixFormat(1,4,8)` 成立，是测试断言（`assertEqual`）能工作的前提。

#### 4.4.2 核心流程

位宽公式：

\[
\text{width} = S + I + F
\]

判等规则：

\[
a == b \iff (a.S = b.S) \land (a.I = b.I) \land (a.F = b.F)
\]

注意 `__eq__` **只比较字段值，不比较对象身份**，所以两个独立构造的、字段相同的 `FixFormat` 会被判为相等——这对测试很重要。但定义了 `__eq__` 而未定义 `__hash__` 会使对象变得不可哈希（Python 默认行为），这是该实现的一个已知取舍（详见小练习）。

#### 4.4.3 源码精读

`__repr__` 与 `__str__`：

```python
def __repr__(self):
    return "FixFormat" + f"({self.S}, {self.I}, {self.F})"

def __str__(self):
    return f"({self.S}, {self.I}, {self.F})"
```

> 见 [en_cl_fix_types.py:365-370](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L365-L370)：`__repr__` 带 `FixFormat` 前缀，`__str__` 只有括号。

判等：

```python
def __eq__(self, other):
    return (self.S == other.S) and (self.I == other.I) and (self.F == other.F)
```

> 见 [en_cl_fix_types.py:373-374](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L373-L374)：三字段全等才判等。

位宽属性：

```python
@property
def width(self):
    """
    Returns the total bit-width of the FixFormat: S + I + F.
    """
    return self.S + self.I + self.F
```

> 见 [en_cl_fix_types.py:377-382](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L377-L382)：`width` 是 `@property`，访问时写 `fmt.width` 而非 `fmt.width()`。

#### 4.4.4 代码实践

**实践目标**：验证 `width`、两种字符串表示、以及 `__eq__` 的行为，并观察 `__eq__` 定义后对哈希的影响。

**操作步骤**（示例代码）：

```python
# 示例代码：观察 width / repr / str / eq / hash
import sys
sys.path.append("bittrue/models/python")
from en_cl_fix_pkg import FixFormat

a = FixFormat(1, 4, 8)
b = FixFormat(1, 4, 8)
c = FixFormat(0, 4, 8)   # 仅 S 不同

print("width   :", a.width)          # 13
print("repr    :", repr(a))          # FixFormat(1, 4, 8)
print("str    :", str(a))           # (1, 4, 8)
print("a == b  :", a == b)           # True（字段全等）
print("a == c  :", a == c)           # False（S 不同）
print("hashable:", end=" ")
try:
    hash(a); print("yes")
except TypeError as e:
    print(f"no ({e})")
```

**需要观察的现象**：`a == b` 为 `True`，证明判等按值而非身份；由于定义了 `__eq__` 却没定义 `__hash__`，对象应不可哈希。

**预期结果**（待本地验证哈希的确切报错文本）：

```
width   : 13
repr    : FixFormat(1, 4, 8)
str    : (1, 4, 8)
a == b  : True
a == c  : False
hashable: no (unhashable type: 'FixFormat')
```

#### 4.4.5 小练习与答案

**练习 1**：为什么 `FixFormat` 定义了 `__eq__` 之后，默认就不能再当字典的键用了？

**参考答案**：Python 规定，若类定义了 `__eq__` 而未定义 `__hash__`，则 `__hash__` 被隐式设为 `None`，对象变为不可哈希。这是因为「按值判等」与「默认基于身份的哈希」会矛盾（两个相等的对象哈希不同会破坏字典不变式），所以 Python 保守地禁用哈希，除非你显式实现一个与 `__eq__` 一致的 `__hash__`。

**练习 2**：`FixFormat(0, -5, 5).width` 等于多少？

**参考答案**：\(0 + (-5) + 5 = 0\)。这是一个合法的「无符号空格式」（`I+F = 0`），但位宽为 0，实际无法承载任何数值——它更多出现在理论推导中。

---

## 5. 综合实践

把本讲的三个类型串起来，完成一个「格式体检报告」小工具。

**任务**：写一个函数 `report(S, I, F)`，它尝试构造 `FixFormat(S, I, F)`；构造成功则打印 `width`、`repr`、`str`，并枚举所有 `FixRound` 和 `FixSaturate` 模式名；构造失败则捕获 `AssertionError` 并说明违反了哪条规则。然后用它扫描下面这组用例，观察输出：

```python
# 示例代码：综合实践——格式体检报告
import sys
sys.path.append("bittrue/models/python")
from en_cl_fix_pkg import FixFormat, FixRound, FixSaturate

def report(S, I, F):
    print(f"=== report({S}, {I}, {F}) ===")
    try:
        fmt = FixFormat(S, I, F)
    except AssertionError as e:
        print(f"  非法格式: {e}")
        return
    print(f"  width = {fmt.width}")
    print(f"  repr  = {fmt!r}")
    print(f"  str   = {fmt}")
    print(f"  可选舍入模式: {[r.name for r in FixRound]}")
    print(f"  可选饱和模式: {[s.name for s in FixSaturate]}")

# 扫描一组有代表性的用例
for args in [(1,4,8), (0,11,5), (1,-2,4), (1,4,-2), (0,0,0), (2,0,0), (1,-1,0)]:
    report(*args)
    print()
```

**需要观察的现象与预期结果**：

- 前 5 个用例合法：会打印各自的 `width`（分别为 13、16、3、3、0）以及 7 个舍入模式名、4 个饱和模式名。
- `(2,0,0)` 报 `S must be 0 or 1`。
- `(1,-1,0)` 报 `I+F must be at least 0`。
- 你应能在输出中清楚看到：**只要格式合法，它就统一对应同一套 7×4 的舍入/饱和选项**——这正是本讲的核心结论：格式描述「数值怎么放」，舍入/饱和描述「放不下时怎么办」，三者相互正交。

> 精确输出文本待本地验证；若 `from en_cl_fix_pkg import *` 报 `ModuleNotFoundError`，请确认 `sys.path` 指向 `bittrue/models/python`（脚本放在仓库根目录运行即可）。

## 6. 本讲小结

- `FixFormat(S, I, F)` 是定点格式身份证，构造时有两条断言：`S` 必须是 0/1、`I+F >= 0`；后者排除了「有符号空格式」和负位宽等无实用价值的别扭边界。
- `FixRound` 是 7 选 1 的舍入标签（`Trunc_s` … `ConvOdd_s`），差别主要在「平局（0.5）如何取舍」；`_s` 后缀与 VHDL 端命名保持一致。
- `FixSaturate` 是 4 选 1 的饱和标签（`None_s` / `Warn_s` / `Sat_s` / `SatWarn_s`），由「是否饱和」「是否告警」两个开关组合而成。
- `width` 属性返回 \(S+I+F\)，是决定走 NarrowFix 还是 WideFix 路径的关键。
- `__repr__` / `__str__` 提供两种字符串表示；`__eq__` 按三字段值判等，使测试断言可用，但同时令对象不可哈希。
- 三个类型相互正交：格式描述「怎么放」，舍入/饱和描述「放不下时怎么办」。

## 7. 下一步学习建议

本讲只讲了「类型长什么样」，还没讲「类型上能做什么」。建议接下来：

1. **学习 u2-l2（Python 主接口函数地图）**：看 `en_cl_fix.py` 如何用 `FixFormat` + `FixRound` + `FixSaturate` 组合出 `cl_fix_from_real`、`cl_fix_resize` 等真实可调用的函数。
2. **学习 u2-l3（VHDL 包镜像）**：对照 `hdl/en_cl_fix_pkg.vhd`，看这三个 Python 类型在 VHDL 里如何镜像成 `FixFormat_t` record、`FixRound_t` / `FixSaturate_t` 枚举，体会「三语言 API 同构」。
3. **若对结果格式推导好奇**：可直接进入单元 3（u3-l1），那里会逐行讲解 `FixFormat.for_add` / `for_sub` / `for_mult` 等本讲一笔带过的静态方法。
