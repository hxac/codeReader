# Python 主接口 en_cl_fix.py 函数地图

## 1. 本讲目标

本讲是 en_cl_fix 的 Python「函数索引课」。读完本讲，你应当能够：

- 按类别说出 `en_cl_fix.py` 暴露的全部 `cl_fix_*` 函数：格式函数别名、数据转换、格式转换、算术运算、仿真工具函数。
- 解释贯穿所有函数的**统一调用约定**：`r_fmt` 缺省时等于全精度 `mid_fmt`、`_clean_input` 如何规整输入、`cl_fix_is_wide` 如何在 NarrowFix/WideFix 之间分发。
- 独立写出一段「构造定点数 → 做运算 → 取回实数」的最小调用链。

本讲只画「函数地图」与「调用骨架」，**不深入**舍入偏移量、饱和回绕公式、分块算法等实现细节——那些留待单元 4（舍入/饱和/resize）和单元 5（转换/算术链路）。

## 2. 前置知识

本讲承接 [u2-l1 核心类型](u2-l1-core-types.md)，默认你已经掌握：

- **`FixFormat(S, I, F)`**：定点格式身份证，`width = S + I + F`。
- **`FixRound`**：7 选 1 舍入标签（如 `Trunc_s`、`NonSymPos_s`）。
- **`FixSaturate`**：4 选 1 饱和标签（`None_s` 回绕、`Sat_s` 钳位、`SatWarn_s` 钳位且告警等）。

还需两个来自 [u1-l3 仓库结构](u1-l3-repository-structure.md) 的背景概念：

- **NarrowFix / WideFix 双表示**：位宽 ≤ 53 位时用双精度浮点（NarrowFix，快），否则用任意精度整数（WideFix，准）。本讲只关心「主接口如何选择走哪条路」，内部表示细节留待单元 6。
- **bit-true**：软件参考模型与硬件逐位一致。本模块的 Python 接口正是这个参考模型的入口。

一个关键直觉（来自 [u1-l2 定点基础](u1-l2-fixed-point-basics.md)）：**定点值 = 把比特当整数看，再乘比例因子 `2^-F`**。本讲所有转换函数都建立在这条等式上。

## 3. 本讲源码地图

本讲只读两个文件：

| 文件 | 作用 |
| --- | --- |
| [bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py) | **主接口**。所有 `cl_fix_*` 函数都在这里，是本讲主角。文件头说明了它的定位（见下方源码精读）。 |
| [bittrue/models/python/en_cl_fix_pkg/\_\_init\_\_.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/__init__.py) | **包门面**。用 `from .xxx import *` 把五个子模块的符号聚合导出，让外部 `import en_cl_fix_pkg` 即可拿到全部 `cl_fix_*`。 |

此外会**少量引用**（只看一两行）：

- `en_cl_fix_types.py`：`for_add`/`for_mult`/`for_round`/`union` 等格式推导方法（其实现留待单元 3）。
- `narrow_fix.py`：`MAX_WIDTH = 53` 常量。

## 4. 核心概念与源码讲解

### 4.1 统一调用约定：r_fmt 缺省、_clean_input、narrow/wide 分发

#### 4.1.1 概念说明

`en_cl_fix.py` 里有二十多个函数，但它们**几乎都遵循同一套骨架**。掌握这套骨架，就能举一反三地读懂任何一个函数，而不必逐个死记。

骨架由三条约定组成：

1. **全精度中间格式 `mid_fmt`**：每个运算都先用格式推导函数（如 `cl_fix_add_fmt`、`cl_fix_mult_fmt`）算出一个「保证不丢任何精度」的结果格式 `mid_fmt`。运算先在 `mid_fmt` 下完成，再按需 `resize` 到用户想要的 `r_fmt`。
2. **`r_fmt` 缺省即 `mid_fmt`**：调用算术函数时若不传 `r_fmt`，则默认 `r_fmt = mid_fmt`，即「要全精度结果，不做任何舍入/饱和」。这是本讲最重要的一条约定。
3. **narrow/wide 自动分发**：根据 `cl_fix_is_wide(fmt)` 判断该格式走 NarrowFix 还是 WideFix，对调用者完全透明。
4. **`_clean_input` 规整输入**：把列表、MATLAB 数组等「可下标」对象统一转成 `np.ndarray`。

> 设计意图（来自文件头）：本模块是「镜像 HDL 实现」的主 Python 接口，内部用 NarrowFix/WideFix 算数，但对外只暴露「原始内部数据」（narrow 是 float64、wide 是 object 整数数组）。两种表示由 `cl_fix_is_wide(fmt)` 唯一决定。

#### 4.1.2 核心流程

一个典型算术函数（以 `cl_fix_add` 为例）的执行流程：

```text
cl_fix_add(a, a_fmt, b, b_fmt, r_fmt=None, rnd=Trunc_s, sat=None_s)
  │
  ├─ 1. a, b = _clean_input(a), _clean_input(b)        # 规整成 ndarray
  │
  ├─ 2. mid_fmt = cl_fix_add_fmt(a_fmt, b_fmt)         # 推导全精度格式
  ├─ 3. if r_fmt is None: r_fmt = mid_fmt               # ★ 缺省即全精度
  │
  ├─ 4. 判定 a_wide / b_wide / mid_wide                 # cl_fix_is_wide
  │     全 narrow  → 用 NarrowFix
  │     任一 wide  → 全部升格为 WideFix（narrow 用 from_narrowfix 转换）
  │
  ├─ 5. mid = a + b                                     # 在 mid_fmt 下运算
  │
  └─ 6. return cl_fix_resize(mid, mid_fmt, r_fmt, rnd, sat)   # 按需收口
```

注意第 6 步：即便 `r_fmt == mid_fmt`，`cl_fix_resize` 仍会被调用——此时既不减少小数位也不减少整数位，round/saturate 都成了「空操作」，直接原样返回。

#### 4.1.3 源码精读

**文件头**说明本模块的定位与 narrow/wide 数据表示的边界：

模块描述：本模块是 en_cl_fix 的主 Python 接口，镜像 HDL 实现；内部用 NarrowFix/WideFix 计算，但对外只暴露原始内部数据，narrow 与 wide 是两种不同表示，由 [cl_fix_is_wide(fmt) 唯一决定](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L20-L33)。

**`_clean_input`**：判断入参「是否可下标」（`__getitem__`），是则 `np.array(a)` 包一层，否则原样返回。这样 MATLAB 经 `varargin{:}` 传进来的混合入参也能被统一处理：

[_clean_input 实现](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L45-L55)

**`cl_fix_is_wide`**：一行判定——位宽超过 `NarrowFix.MAX_WIDTH`（=53）就 wide。这是整个分发的总开关：

[cl_fix_is_wide 实现](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L79-L84)

其中 `MAX_WIDTH = 53` 来自 NarrowFix，因为 IEEE 754 双精度浮点的尾数正好 53 位（见 [narrow_fix.py 的常量与注释](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L49-L58)，细节留待单元 6）。

**narrow/wide 分发块**（以 `cl_fix_add` 为代表，所有算术函数都长这样）：只要任一操作数或 `mid_fmt` 是 wide，就把所有 narrow 操作数用 `WideFix.from_narrowfix(...)` 升格成 wide，统一在 WideFix 域运算；否则全部 NarrowFix：

[cl_fix_add 的分发与运算](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L328-L342)

#### 4.1.4 代码实践

**目标**：亲手验证「`r_fmt` 缺省即 `mid_fmt`」这条约定。

**操作步骤**（在仓库根目录运行）：

```python
from en_cl_fix_pkg import FixFormat, cl_fix_from_real, cl_fix_add, cl_fix_add_fmt, cl_fix_to_real

a_fmt = FixFormat(1, 4, 8)
b_fmt = FixFormat(1, 4, 8)

# 1) 先看全精度加法格式
print("add_fmt =", cl_fix_add_fmt(a_fmt, b_fmt))   # 期望 (1, 5, 8)

# 2) 不传 r_fmt（用缺省）
a = cl_fix_from_real(1.5, a_fmt)
b = cl_fix_from_real(2.25, b_fmt)
r_default = cl_fix_add(a, a_fmt, b, b_fmt)         # r_fmt 缺省
print("to_real  =", cl_fix_to_real(r_default, cl_fix_add_fmt(a_fmt, b_fmt)))

# 3) 显式传 r_fmt = mid_fmt，结果应完全相同
r_explicit = cl_fix_add(a, a_fmt, b, b_fmt, r_fmt=cl_fix_add_fmt(a_fmt, b_fmt))
print("equal?   =", (r_default == r_explicit))
```

**预期结果**：`add_fmt = (1, 5, 8)`（两格式相加整数位增长 1）；两种调用方式的 `to_real` 都为 `3.75`，且 `equal? = True`。

> 注：`cl_fix_add` 默认返回的是 narrow 原始数据（float64），其「含义」必须配合一个格式才能解读；这里我们用 `cl_fix_add_fmt` 得到的格式去 `to_real`。**待本地验证**：若你的 numpy 版本输出数组形状不同，以 `to_real` 的标量值为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么不直接让所有函数都强制要求传 `r_fmt`？

**答案**：因为大多数情况下用户想要的就是「不丢精度的正确结果」。把 `r_fmt` 缺省设成全精度 `mid_fmt`，让「想要精确」成为零成本默认，把「想要截断/饱和」变成显式 opt-in，API 更难用错。

**练习 2**：`_clean_input` 用 `hasattr(a, "__getitem__")` 判断，那么传入一个 Python `int`（如 `5`）会发生什么？

**答案**：`int` 没有 `__getitem__`，所以 `_clean_input` 原样返回 `5`，不会包成 ndarray。这与「标量也是合法定点数据」的预期一致。

---

### 4.2 格式函数别名与格式查询函数

#### 4.2.1 概念说明

这一组函数回答两类问题：

- **「运算结果该用什么格式？」** —— 称为**格式推导函数**（format functions）。它们是纯函数：只看输入格式，不看具体数值，给出一个「保守的、保证不溢出」的结果格式。
- **「这个格式有多宽？能表示多大/多小？是不是 wide？」** —— 称为**格式查询函数**。

格式推导函数在源码里其实只是 `FixFormat` 上静态方法（如 `FixFormat.for_add`）的**别名**，一一对应到 VHDL 里同名函数（如 `cl_fix_add_fmt`）。这种「Python 别名 = FixFormat 方法 = VHDL 函数」的三重对齐，正是三语言 bit-true 的基础。

#### 4.2.2 核心流程

| 别名（本模块） | FixFormat 静态方法 | 语义 |
| --- | --- | --- |
| `cl_fix_add_fmt(a,b)` | `for_add` | `a+b` 的全精度格式 |
| `cl_fix_sub_fmt(a,b)` | `for_sub` | `a-b` |
| `cl_fix_addsub_fmt(a,b)` | `for_addsub` | `a±b`（add 与 sub 的 union） |
| `cl_fix_mult_fmt(a,b)` | `for_mult` | `a*b` |
| `cl_fix_neg_fmt(a)` | `for_neg` | `-a` |
| `cl_fix_abs_fmt(a)` | `for_abs` | `|a|` |
| `cl_fix_shift_fmt(a,min,max)` | `for_shift` | `a<<n`（n∈[min,max]） |
| `cl_fix_round_fmt(a,rF,rnd)` | `for_round` | 舍入后的格式 |
| `cl_fix_union_fmt(a,b)` | `union` | 能表示两者的最小格式 |

查询函数：`cl_fix_width`、`cl_fix_is_wide`、`cl_fix_max_value`、`cl_fix_min_value`、`cl_fix_format_to_string`。

#### 4.2.3 源码精读

**9 个别名**——注意它们都直接指向 `FixFormat` 上的方法，本模块一行实现都没有，纯粹是「换名字」以便和 VHDL 函数名一致：

[格式函数别名定义](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L61-L70)

**`cl_fix_width`** 与 **`cl_fix_format_to_string`** 都是单行转发，分别取 `fmt.width` 和 `str(fmt)`：

[cl_fix_width / format_to_string](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L72-L111)

**`cl_fix_max_value` / `cl_fix_min_value`**：按 `cl_fix_is_wide` 分发到 NarrowFix 或 WideFix 的同名方法，返回该格式可表示的极值（实数意义下）：

[max_value / min_value 分发](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L87-L104)

至于被别名的「真身」，例如 `for_add` 的保守推导逻辑（它就是 `cl_fix_add_fmt`），其完整实现见 [en_cl_fix_types.py 的 for_add](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L73-L121)——本讲只确认「别名指向它」，推导细节留待单元 3。

#### 4.2.4 代码实践

**目标**：用查询函数画出几个常见格式的「身份证」。

**操作步骤**：

```python
from en_cl_fix_pkg import FixFormat, cl_fix_width, cl_fix_is_wide, cl_fix_min_value, cl_fix_max_value

for fmt in [FixFormat(0,8,0), FixFormat(1,4,8), FixFormat(0,0,60)]:
    print(f"{fmt}: width={cl_fix_width(fmt)}, wide={cl_fix_is_wide(fmt)}, "
          f"range=[{cl_fix_min_value(fmt)}, {cl_fix_max_value(fmt)}]")
```

**预期结果**：

- `(0,8,0)`：width=8，wide=False，range=[0, 255]。
- `(1,4,8)`：width=13，wide=False，range=[-16, 15.99609375]。
- `(0,0,60)`：width=60，wide=True（>53），range=[0, ≈1.1e18]（`2^60-1` 个 ULP，ULP=`2^-60`）。

> **待本地验证**：`(0,0,60)` 是 wide 路径，`min/max_value` 返回的是 Python 大整数（`object` dtype）。

#### 4.2.5 小练习与答案

**练习 1**：`cl_fix_add_fmt` 为什么是「保守」的？

**答案**：因为它假设两个输入都可能取到自身格式范围内的任意值（见 `for_add` docstring 的 "This is a conservative calculation"），并据此覆盖「最坏情况」的位增长。若实际取值受限，更窄的格式也可能够用，但本函数不会利用这一点。

**练习 2**：`cl_fix_mult_fmt(FixFormat(1,4,8), FixFormat(1,4,8))` 会返回什么？

**答案**：返回 `(1, 9, 16)`。两个有符号数相乘，整数位 = `4+4+1=9`（有符号×有符号会多 1 位，对应 `-2^4 × -2^4 = 2^8` 需要第 9 位），小数位 = `8+8=16`。这一点在第 4.4 节和综合实践中会用到。

---

### 4.3 数据转换：from_real / to_real / from_integer / to_integer

#### 4.3.1 概念说明

定点数在内存里是「按比例因子缩放过的整数」。en_cl_fix 区分两种「读法」：

- **归一化（real）**：把定点数解读成它所代表的真实物理值，例如格式 `(0,2,1)` 下的整数 `5` 代表实数 `2.5`。
- **非归一化（integer）**：把定点数就当成裸整数看，例如上面那个 `5`。

对应四个函数：

| 函数 | 方向 | 含义 |
| --- | --- | --- |
| `cl_fix_from_real(x, r_fmt, sat=SatWarn_s)` | 实数 → 定点 | 把浮点 `x` 转成 `r_fmt` 下的定点原始数据（半进位舍入 + 饱和） |
| `cl_fix_to_real(a, a_fmt)` | 定点 → 实数 | 把 `a_fmt` 下的定点数据解读回浮点 |
| `cl_fix_from_integer(a, r_fmt)` | 整数 → 定点 | 把裸整数当作 `r_fmt` 下定点数据（直接当原始数据） |
| `cl_fix_to_integer(a, a_fmt)` | 定点 → 整数 | 取出定点数据的裸整数 |

`from_real` 是最常用的「构造」函数；`to_real` 是最常用的「检查」函数——本讲及后续所有实践都用它来打印人类可读的结果。

#### 4.3.2 核心流程

`cl_fix_from_real` 的流程：

```text
cl_fix_from_real(a, r_fmt, sat=SatWarn_s)
  ├─ a = _clean_input(a)
  ├─ if cl_fix_is_wide(r_fmt):  return WideFix.from_real(a, r_fmt, sat)._data
  └─ else:                      return NarrowFix.from_real(a, r_fmt, sat)._data
```

注意它**固定用半进位（half-up）舍入 + `SatWarn_s` 饱和**。docstring 明确：若需要别的舍入模式或不要饱和，请改用 `cl_fix_resize`。这是 `from_real` 与 `resize` 的分工：前者是「快速构造」，后者是「可控转换」。

`cl_fix_to_real` 对 narrow 格式几乎零成本——因为 narrow 内部本来就是 float64，定点值在数值上就等于该浮点数（`real == integer × 2^-F`，而 NarrowFix 直接把 `real` 存下来）。

#### 4.3.3 源码精读

**`cl_fix_from_real`**：固定 `SatWarn_s`、半进位；按 wide 分发：

[cl_fix_from_real](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L130-L142)

**`cl_fix_from_integer`** / **`cl_fix_to_integer`**：docstring 给了非常清晰的互逆示例（`5` ↔ `2.5`）。wide 路径下「整数就是原始数据」，直接返回 `a`；narrow 路径走 `NarrowFix`：

[from_integer / to_integer](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L145-L170)

**`cl_fix_to_real`**：narrow 直接返回 `a`（因为 float64 存的就是 real），wide 走 `WideFix.to_real()`：

[cl_fix_to_real](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L173-L184)

#### 4.3.4 代码实践

**目标**：验证 `from_real` ↔ `to_real`、`from_integer` ↔ `to_integer` 的互逆关系。

**操作步骤**：

```python
from en_cl_fix_pkg import FixFormat, cl_fix_from_real, cl_fix_to_real, cl_fix_from_integer, cl_fix_to_integer

fmt = FixFormat(0, 2, 1)   # 无符号，范围 [0, 3.5]，步长 0.5

# real 互逆
a = cl_fix_from_real(2.5, fmt)
print("to_real   =", cl_fix_to_real(a, fmt))        # 2.5

# integer 互逆（同一个数据 a，换种读法）
print("to_integer=", cl_fix_to_integer(a, fmt))     # 5

# from_integer 的反方向
b = cl_fix_from_integer(5, fmt)
print("b to_real =", cl_fix_to_real(b, fmt))        # 2.5
```

**预期结果**：`to_real=2.5`，`to_integer=5`，`b to_real=2.5`。可以看到「整数 5」与「实数 2.5」在 `(0,2,1)` 下是同一份定点数据，只是两种解读。

**需要观察的现象**：把 `cl_fix_from_real(2.6, fmt)` 打印出来——因为步长是 0.5，`2.6` 会被半进位到 `2.5`，体现 `from_real` 的「量化」行为。

#### 4.3.5 小练习与答案

**练习 1**：`cl_fix_from_real(3.9, FixFormat(0,2,1), FixSaturate.SatWarn_s)` 会得到什么实数？为什么？

**答案**：得到 `3.5`。`(0,2,1)` 最大值是 `3.5`，`3.9` 超出范围，按 `SatWarn_s` 饱和到上界 `3.5` 并发出告警（告警行为待单元 4 详述）。

**练习 2**：为什么 `cl_fix_to_real` 对 narrow 格式「几乎免费」？

**答案**：NarrowFix 用 float64 直接存储归一化后的实数值（即 `integer × 2^-F`），所以 `to_real` 只需原样返回该 float64；对 wide 格式才需要从大整数重新计算 `integer × 2^-F`。

---

### 4.4 算术运算：add / sub / addsub / mult / neg / abs / shift

#### 4.4.1 概念说明

算术函数是本模块的「重头戏」，但正因为它们都遵循 4.1 节的统一骨架，理解起来高度一致。每个算术函数都做三件事：

1. 推导全精度 `mid_fmt`；
2. 在 `mid_fmt` 下做运算；
3. `resize` 到用户指定的 `r_fmt`（缺省即 `mid_fmt`）。

| 函数 | 运算 | mid_fmt 推导 |
| --- | --- | --- |
| `cl_fix_add(a,a_fmt,b,b_fmt,...)` | `a+b` | `cl_fix_add_fmt` |
| `cl_fix_sub(...)` | `a-b` | `cl_fix_sub_fmt` |
| `cl_fix_addsub(...,add,...)` | `a+b` 或 `a-b`（按 bool 数组选择） | 复用 add+sub，再用 `np.where` 合并 |
| `cl_fix_mult(...)` | `a*b` | `cl_fix_mult_fmt` |
| `cl_fix_neg(a,a_fmt,...)` | `-a` | `cl_fix_neg_fmt` |
| `cl_fix_abs(a,a_fmt,...)` | `|a|` | `cl_fix_abs_fmt` |
| `cl_fix_shift(a,a_fmt,shift,r_fmt,...)` | `a<<shift`（`shift<0` 为右移） | `cl_fix_shift_fmt` |

#### 4.4.2 核心流程

以 `cl_fix_mult` 为例（其余同构）：

```text
cl_fix_mult(a,a_fmt,b,b_fmt, r_fmt=None, rnd=Trunc_s, sat=None_s)
  ├─ mid_fmt = cl_fix_mult_fmt(a_fmt, b_fmt)
  ├─ if r_fmt is None: r_fmt = mid_fmt          # ★ 缺省全精度
  ├─ narrow/wide 分发（见 4.1）
  ├─ mid = a * b                                 # 重载的运算符
  └─ return cl_fix_resize(mid._data, mid_fmt, r_fmt, rnd, sat)
```

两个值得注意的差异：

- **`cl_fix_addsub`** 不自己算，而是分别调用 `cl_fix_add` 和 `cl_fix_sub` 得到两个结果，再用 `np.where(add, radd, rsub)` 按 bool（或 bool 数组）逐元素挑选。这适合「同一套数据，有的元素加、有的元素减」的向量场景。
- **`cl_fix_shift`** 的 docstring 强调：它先做**无损移位**（等价于 `×2^shift`，不截断任何位），`mid_fmt` 已把移位后的位增长算进去，最后才 `resize` 到 `r_fmt`。也就是说「移位本身不丢精度，丢精度只发生在最后的 resize」。

#### 4.4.3 源码精读

**`cl_fix_mult`**：典型的算术骨架。注意 `mid = a*b` 用的是 NarrowFix/WideFix 重载过的运算符：

[cl_fix_mult](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L392-L421)

**`cl_fix_add`**：与 mult 结构完全相同，只是 `mid_fmt` 换成 `cl_fix_add_fmt`、运算换成 `a+b`：

[cl_fix_add](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L313-L342)

**`cl_fix_addsub`**：复用 add/sub，用 `np.where` 合并：

[cl_fix_addsub](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L377-L389)

**`cl_fix_shift`**：强调「无损移位再 resize」。注意 `mid_fmt` 用 `np.min(shift)`/`np.max(shift)` 支持 shift 为数组的情况：

[cl_fix_shift](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L424-L452)

#### 4.4.4 代码实践

**目标**：体会「缺省得全精度」与「显式 resize 得截断」的差别。

**操作步骤**：

```python
from en_cl_fix_pkg import (FixFormat, FixRound, FixSaturate,
    cl_fix_from_real, cl_fix_mult, cl_fix_mult_fmt, cl_fix_to_real)

a_fmt = FixFormat(1, 4, 8)
b_fmt = FixFormat(1, 4, 8)
mid   = cl_fix_mult_fmt(a_fmt, b_fmt)     # 全精度 = (1, 9, 16)

a = cl_fix_from_real(1.5,  a_fmt)
b = cl_fix_from_real(2.25, b_fmt)

# (1) 缺省 r_fmt → 全精度 (1,9,16)
r_full = cl_fix_mult(a, a_fmt, b, b_fmt)
print("full =", cl_fix_to_real(r_full, mid))     # 3.375

# (2) 显式 resize 到 (1,8,16)：丢 1 个整数位
r_trunc = cl_fix_mult(a, a_fmt, b, b_fmt, r_fmt=FixFormat(1, 8, 16))
print("trunc=", cl_fix_to_real(r_trunc, FixFormat(1, 8, 16)))   # 3.375（值小，未触发饱和）
```

**预期结果**：两次都得 `3.375`。因为 `1.5 × 2.25 = 3.375` 远小于 `(1,8,16)` 的上界 `2^8=256`，截断 1 个整数位并未引起饱和。

**需要观察的现象**：把输入换成 `cl_fix_from_real(15.0, a_fmt)` 和 `cl_fix_from_real(15.0, b_fmt)`，乘积 `225` 仍能放进 `(1,8,16)`（上界 256），但如果换成会超过 256 的值，`trunc` 路径在默认 `sat=None_s`（回绕）下会出现「环绕」的怪值——这正是 `resize` 顺序与饱和模式的课题，留待单元 4。

#### 4.4.5 小练习与答案

**练习 1**：`cl_fix_mult_fmt(FixFormat(1,4,8), FixFormat(1,4,8))` 为什么整数位是 9 而不是 8？

**答案**：因为两个**有符号**数相乘，最负值之积 `(-2^4)×(-2^4)=2^8` 需要用到第 9 个整数位才能无符号地表示（详见 `for_mult` 中 `a_fmt.S==1 and b_fmt.S==1 ⇒ rmaxI = I_a + I_b + 1`）。这是「保守推导」覆盖最坏情况的体现。

**练习 2**：`cl_fix_addsub` 为什么不直接写一个统一的加减电路，而是分别调 add 和 sub 再 `np.where`？

**答案**：这样能复用 `cl_fix_add`/`cl_fix_sub` 已经验证过的格式推导与 resize 逻辑，保证 bit-true；`np.where` 在向量场景下还能让同一批数据「有的加有的减」。代价是计算量略大，但参考模型优先正确性。

---

### 4.5 格式转换：round / saturate / resize / in_range

#### 4.5.1 概念说明

格式转换处理「把数据从一个格式搬到另一个格式」时不可避免的精度损失。回顾 [u1-l2](u1-l2-fixed-point-basics.md) 的两类处理：

- **F 变少（小数位减少）→ 舍入**：`cl_fix_round`。
- **I/S 变少（整数位/符号位减少）→ 饱和**：`cl_fix_saturate`。
- **同时变少 → `cl_fix_resize`**：先 round 后 saturate 的组合。
- **只问「会不会溢出」不动数据 → `cl_fix_in_range`**。

`cl_fix_resize` 是最常用的格式转换函数，也是所有算术函数的「收口」环节。

#### 4.5.2 核心流程

`cl_fix_resize` 的两段式（顺序至关重要）：

```text
cl_fix_resize(a, a_fmt, r_fmt, rnd=Trunc_s, sat=None_s)
  ├─ rounded_fmt = FixFormat.for_round(a_fmt, r_fmt.F, rnd)   # 舍入后的中间格式
  ├─ rounded = cl_fix_round(a, a_fmt, rounded_fmt, rnd)       # ① 先舍入：把 F 降到 r_fmt.F
  └─ result  = cl_fix_saturate(rounded, rounded_fmt, r_fmt, sat)  # ② 后饱和：把 I/S 压到 r_fmt
```

为什么**必须先 round 后 saturate**？因为舍入可能产生进位（让整数位 +1，见 `for_round`），从而改变是否需要饱和的判断。若先饱和再舍入，可能把「舍入后本应饱和」的值漏判，或把已经饱和的值再舍入一次导致二次误差。这一顺序在单元 4 会详细论证，这里先记住结论。

`cl_fix_in_range` 回答的是「`a` 量化到 `r_fmt.F` 后，是否落在 `r_fmt` 的 `[min,max]` 内」。它**不改数据**，只返回 bool（或 bool 数组），常用于仿真时判断「这次需不需要饱和」。

#### 4.5.3 源码精读

**`cl_fix_round`**：开头有一条 `assert`——要求传入的 `r_fmt` 必须正好等于 `cl_fix_round_fmt(a_fmt, r_fmt.F, rnd)`，防止用户给错结果格式。随后同样走 narrow/wide 分发。注意 wide 路径下若结果该是 narrow，会用 `NarrowFix(r.to_real(), r_fmt)` 转回：

[cl_fix_round](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L190-L212)

**`cl_fix_saturate`**：同样有 `assert`——要求 `r_fmt.F == a_fmt.F`（饱和阶段小数位不能变，变 F 是 round 的职责）：

[cl_fix_saturate](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L215-L237)

**`cl_fix_resize`**：组合上述两者，顺序为 round → saturate：

[cl_fix_resize](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L240-L253)

**`cl_fix_in_range`**：先按 `r_fmt.F` 和 `rnd` 量化（`cl_fix_round`），再与 `r_fmt` 的 `min/max_value` 比较，返回 bool 数组。注意它在源码里位于「Format functions」区块（紧随 `format_to_string`），而非「Format conversions」区块：

[cl_fix_in_range](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L114-L124)

#### 4.5.4 代码实践

**目标**：对比「只 round」与「resize（round+sat）」在同时减少 F 和 I 时的差别。

**操作步骤**：

```python
import numpy as np
from en_cl_fix_pkg import (FixFormat, FixRound, FixSaturate,
    cl_fix_from_real, cl_fix_round, cl_fix_round_fmt,
    cl_fix_resize, cl_fix_to_real, cl_fix_in_range)

a_fmt = FixFormat(1, 4, 8)       # 有较大小数位
r_fmt = FixFormat(1, 2, 4)       # 同时砍 2 个整数位、4 个小数位

a = cl_fix_from_real(np.array([1.5, 3.0, 5.25]), a_fmt)

# (1) 只 round 到 r_fmt.F=4（整数位不变，仍 5 位 → 这里 round_fmt 的 I 可能 +1）
rnd = FixRound.NonSymPos_s
rounded_fmt = cl_fix_round_fmt(a_fmt, r_fmt.F, rnd)
only_round = cl_fix_round(a, a_fmt, rounded_fmt, rnd)
print("round only =", cl_fix_to_real(only_round, rounded_fmt))

# (2) resize：round 后再 saturate 到 (1,2,4)
resized = cl_fix_resize(a, a_fmt, r_fmt, rnd=rnd, sat=FixSaturate.SatWarn_s)
print("resized    =", cl_fix_to_real(resized, r_fmt))

# (3) 顺带看 in_range：哪些值能放进 (1,2,4)
print("in_range   =", cl_fix_in_range(a, a_fmt, r_fmt, rnd=rnd))
```

**预期结果**：

- `round only`：小数位降到 4，整数位不变（甚至可能 +1）；数值精度变粗但范围不变。
- `resized`：进一步把整数位压到 2，超出 `[-2, 2-2^-4]` 的值（如 `3.0`、`5.25`）被饱和到上界 ≈ `1.9375`。
- `in_range`：`[True, False, False]`（`1.5` 在范围内；`3.0`、`5.25` 超出）。

> **待本地验证**：`5.25` 经 round 后可能进位到 `5.3125` 量级，`in_range` 仍为 `False`；具体 bool 数组以本地运行为准。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `cl_fix_saturate` 的 `assert` 要求 `r_fmt.F == a_fmt.F`？

**答案**：饱和只负责「砍整数位/符号位」，不负责「砍小数位」。改变小数位数（F）是 `cl_fix_round` 的职责。把两者职责分开，才能让 `cl_fix_resize` 通过「先 round 后 saturate」正确组合出任意格式转换。若在 saturate 里也改 F，就会出现职责重叠和顺序歧义。

**练习 2**：`cl_fix_round` 开头的 `assert` 在保护什么？

**答案**：它要求 `r_fmt` 必须等于 `cl_fix_round_fmt(a_fmt, r_fmt.F, rnd)`。因为某些舍入模式（非 Trunc）会让整数位 +1（见 `for_round`），用户若手算 `r_fmt` 时漏了这个 +1，就会得到「看似合理实则丢精度」的结果格式。这条断言强制用户用 `cl_fix_round_fmt` 推导结果格式，杜绝手算出错。

---

## 5. 综合实践

把本讲学到的「构造 → 运算 → resize → 检查」串成一条完整链路。任务来自本讲规格：

> 用 `cl_fix_from_real` + `cl_fix_mult` 完成 `[1,4,8] × [1,4,8] → [1,8,16]` 的乘法，并用 `cl_fix_to_real` 检查结果。

**关键提示（务必先想清楚）**：`[1,4,8] × [1,4,8]` 的**全精度** `mid_fmt` 是 `[1,9,16]`（有符号×有符号多 1 个整数位），**不是** `[1,8,16]`。因此 `[1,8,16]` 是一个**比全精度更窄的目标 `r_fmt`**——需要 `cl_fix_mult` 内部的 `resize` 砍掉 1 个整数位。

**操作步骤**：

```python
from en_cl_fix_pkg import (FixFormat, FixRound, FixSaturate,
    cl_fix_from_real, cl_fix_mult, cl_fix_mult_fmt, cl_fix_to_real)

a_fmt = FixFormat(1, 4, 8)
b_fmt = FixFormat(1, 4, 8)
target = FixFormat(1, 8, 16)

# 第 1 步：确认全精度格式（应为 (1,9,16)，比 target 多 1 个整数位）
print("mid_fmt   =", cl_fix_mult_fmt(a_fmt, b_fmt))

# 第 2 步：构造两个定点输入
a = cl_fix_from_real(1.5,  a_fmt)
b = cl_fix_from_real(2.25, b_fmt)

# 第 3 步：乘法，显式指定 target 作为 r_fmt（触发内部 resize）
r = cl_fix_mult(a, a_fmt, b, b_fmt, r_fmt=target,
                rnd=FixRound.Trunc_s, sat=FixSaturate.SatWarn_s)

# 第 4 步：用 to_real 检查（注意要用 target 格式解读！）
print("result    =", cl_fix_to_real(r, target))    # 3.375
print("expected  =", 1.5 * 2.25)                    # 3.375
```

**预期结果**：

1. `mid_fmt = (1, 9, 16)`。
2. `result = 3.375`，`expected = 3.375`，两者相等。

**延伸思考（可选）**：

- 把 `a`、`b` 都换成 `cl_fix_from_real(15.0, a_fmt)`，乘积 `225`。它仍小于 `(1,8,16)` 上界 `256`，所以 `result` 应为 `225.0`，`resize` 未触发饱和。
- 再换成 `cl_fix_from_real(16.0, a_fmt)`（注：`(1,4,8)` 的最大值约为 `15.996`，`16.0` 会在 `from_real` 阶段就被 `SatWarn_s` 饱和到上界，所以先确认输入被量化成了什么）——观察「构造阶段饱和」与「resize 阶段饱和」是两个独立的环节。

> 这一实践同时覆盖了 4.1（`r_fmt` 与 `mid_fmt`）、4.2（`cl_fix_mult_fmt`）、4.3（`from_real`/`to_real`）、4.4（`cl_fix_mult`）、4.5（`resize`）五个模块，是本讲的「收官」练习。

## 6. 本讲小结

- `en_cl_fix.py` 是主接口，所有 `cl_fix_*` 函数都在此；`__init__.py` 用 `from .xxx import *` 聚合五个子模块对外导出。
- **统一骨架**：算术函数都是「推导 `mid_fmt` → narrow/wide 分发 → 运算 → `resize` 到 `r_fmt`」。
- **`r_fmt` 缺省即全精度 `mid_fmt`**：想要精确是零成本默认，想要截断/饱和才需显式传参。
- **narrow/wide 透明分发**：`cl_fix_is_wide(fmt) = width > 53` 决定走 NarrowFix 还是 WideFix，调用者无需关心。
- 函数分五类：格式函数别名（=`FixFormat` 方法别名）、数据转换（`from_real`/`to_real`/`from_integer`/`to_integer`）、格式转换（`round`/`saturate`/`resize`/`in_range`）、算术（`add`/`sub`/`addsub`/`mult`/`neg`/`abs`/`shift`）、仿真工具（`random`/`zeros`/`write_formats`，仅 Python 有）。
- `cl_fix_resize = 先 round 后 saturate`，顺序不可对调；`cl_fix_in_range` 只判断不修改。

## 7. 下一步学习建议

本讲只画了「函数地图」和「调用骨架」，刻意回避了实现细节。建议下一步：

1. **单元 3（结果格式推导）**：精读 `FixFormat.for_add`/`for_mult`/`for_round`/`union` 的保守推导算法——理解了它们，你才算真正懂了 `mid_fmt` 是怎么来的。
2. **单元 4（舍入/饱和/resize）**：深入 `NarrowFix.round`/`saturate` 的偏移量与回绕公式，搞懂 7 种舍入和 4 种饱和到底差在哪。
3. **单元 6（Narrow/Wide 双表示）**：弄清 `MAX_WIDTH=53` 的来历、NarrowFix 的 float64 内部表示与 WideFix 的任意精度整数表示，以及二者如何互转。

如果想立刻动手，建议先把本讲的代码实践全部跑通，再带着「为什么 `for_mult` 要 +1 位」「`resize` 顺序为什么不能反」这两个问题进入单元 3 和单元 4。
