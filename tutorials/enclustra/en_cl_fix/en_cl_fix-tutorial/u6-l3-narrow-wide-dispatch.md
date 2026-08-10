# cl_fix_is_wide 分发与 Narrow↔Wide 互转

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `cl_fix_is_wide` 的判定依据，以及它和 `NarrowFix.MAX_WIDTH = 53` 的关系。
- 解释 `en_cl_fix.py` 里每个 `cl_fix_*` 函数共用的「窄输入 / 宽输入 / 宽结果」三分支分发骨架，并懂得为什么判定条件是 `a_wide or r_wide`（或算术里的 `a_wide or b_wide or mid_wide`）。
- 理解 `WideFix.from_narrowfix` 这个「无损桥梁」为什么不做量化、不做边界检查。
- 看懂「宽中间结果回转窄」的那一行 `NarrowFix(r.to_real(), r_fmt)`，并能用返回数组的 `dtype`（`object` 还是 `float64`）作为「走了哪条路」的可观测信号。
- 自己构造一个「窄输入、宽结果」的运算，跟踪分发路径并验证。

## 2. 前置知识

本讲是单元 6（Narrow/Wide 双表示架构）的收尾课，默认你已经读完：

- **u6-l1 NarrowFix**：用 float64 存放归一化 real 值，最大精确表示 53 位，`MAX_WIDTH = 53` 是这条快速路径的硬上限。
- **u6-l2 WideFix**：用 Python 任意精度整数（`dtype == object`）存放非归一化整数 `real × 2^F`，无位宽上限、永远精确，但慢。

本讲要回答的核心问题是：**这两套表示摆在一起，调用者根本不关心，那么 `cl_fix_*` 主接口如何在二者之间自动切换、并且让切换对调用者完全透明？**

先记住一个关键事实（来自 u6-l1/u6-l2）：同一个定点值，两种内部表示长得很不一样。

| 表示 | 存储类型 | 存的是 | 例：`1.25` 在 `[0,2,4]` 下 |
|---|---|---|---|
| NarrowFix | `np.float64` | 归一化 real | `1.25` |
| WideFix | Python `int`（`object` 数组） | 非归一化整数 | `1.25 × 2^4 = 20` |

正因为「同一个值、两种长相」，分发机制必须保证：**对调用者而言，给进去的是什么类型、出来的又是什么类型，完全由格式 `fmt` 唯一决定，与走了哪条内部路径无关。** 这就是本讲全部设计的出发点。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py) | 主接口，所有 `cl_fix_*` 函数所在，本讲的「分发调度中心」。 |
| [bittrue/models/python/en_cl_fix_pkg/wide_fix.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py) | WideFix 类，含本讲要精读的 `from_narrowfix`、`to_real`。 |
| [bittrue/models/python/en_cl_fix_pkg/narrow_fix.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py) | NarrowFix 类，提供 `MAX_WIDTH = 53` 这一分发边界常量。 |

---

## 4. 核心概念与源码讲解

### 4.1 分发的动机与判定：cl_fix_is_wide 与 53 位边界

#### 4.1.1 概念说明

`en_cl_fix.py` 顶部有一段把整个分发哲学讲透了的说明文字（[en_cl_fix.py:20-33](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L20-L33)）。它的核心是三句话：

1. 内部计算用 NarrowFix / WideFix 两套类。
2. 但模块对外的所有数据 I/O，给的都是「原始内部数据」——也就是说 narrow 数据是 `float64`、wide 数据是 `object`（Python int），两者长得不一样。
3. **给定一个格式 `fmt`，该用哪种表示是唯一确定的**，判定函数就是 `cl_fix_is_wide(fmt)`。

这带来一条极其重要的、贯穿全讲的「唯一表示约定」：

> 对同一个 `fmt`，`cl_fix_*` 进出的原始数据类型是固定的——要么永远是 `float64`（narrow），要么永远是 `object`（wide）。调用者拿到结果后，不需要被告知「这条走了哪条路」，看一眼 `fmt` 就知道该怎么解读。

#### 4.1.2 核心流程

判定函数本身极其简单（[en_cl_fix.py:79-84](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L79-L84)）：

```python
def cl_fix_is_wide(fmt : FixFormat) -> bool:
    return cl_fix_width(fmt) > NarrowFix.MAX_WIDTH
```

边界常量 `MAX_WIDTH = 53` 来自 NarrowFix（[narrow_fix.py:40-52](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L40-L52)），其来历在 u6-l1 已详述：float64 的 53 位尾数可精确表示整数，再为「有符号回绕需临时加 2^I」预留 1 位，于是有符号 / 无符号共用同一个 53 位上限。于是分界线就是一个简单不等式：

\[
\text{is\_wide} \iff \text{width}(fmt) > 53
\]

注意是**严格大于**：`width == 53` 仍算 narrow（`53 > 53` 为假）。NarrowFix 构造函数里的断言也用 `<=` 配合这个边界（[narrow_fix.py:58](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L58)），保证「凡是通过 `cl_fix_is_wide` 判为 narrow 的格式，一定能安全构造 NarrowFix」。

#### 4.1.3 源码精读

分发判定还出现在好几个「按格式取极值」的工具函数里，模式完全一致——先用 `cl_fix_is_wide(fmt)` 选路，再调对应类的同名静态方法（[en_cl_fix.py:87-104](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L87-L104)）：

```python
def cl_fix_max_value(fmt : FixFormat):
    if cl_fix_is_wide(fmt):
        return WideFix.max_value(fmt)._data
    else:
        return NarrowFix.max_value(fmt)._data
```

这就是分发的最朴素形态：**一个格式、两条路、返回类型随格式走**。`cl_fix_min_value` 紧跟其后，结构相同。注意它们返回的是 `._data`，即剥掉对象外壳、只给原始内部数据——wide 时是 `object`，narrow 时是 `float64`，正好对应「唯一表示约定」。

#### 4.1.4 代码实践

1. **实践目标**：亲手感受 53 这条分界线。
2. **操作步骤**（在仓库根目录，确保已 `pip install -r requirements.txt`）：

   ```python
   import sys; sys.path.append("bittrue/models/python")
   from en_cl_fix_pkg import *

   for w_target in [52, 53, 54]:
       fmt = FixFormat(0, w_target, 0)   # 无符号纯整数，width == w_target
       print(f"width={fmt.width:3d}  is_wide={cl_fix_is_wide(fmt)}")
   ```

3. **需要观察的现象**：`width=52` 与 `width=53` 都打印 `is_wide=False`，`width=54` 才变成 `True`。
4. **预期结果**：分界线确实卡在「严格大于 53」。本结论已由源码不等式直接保证。

#### 4.1.5 小练习与答案

**练习 1**：`FixFormat(1, 30, 22)` 是 narrow 还是 wide？把它的 `width` 算出来。

**参考答案**：`width = 1 + 30 + 22 = 53`，`53 > 53` 为假，所以是 **narrow**。它正好压在边界上。

---

### 4.2 Narrow→Wide 的无损桥梁：WideFix.from_narrowfix

#### 4.2.1 概念说明

一旦判定「需要走 WideFix」，但输入数据本身是 narrow（`float64`），就必须先把 `float64` 翻译成 wide 的整数表示。这件事由 `WideFix.from_narrowfix` 完成。它名字里的 `from_narrowfix` 已经点明了它的定位：**把一个 NarrowFix 对象无损地搬成 WideFix 对象**。

关键在于「无损」二字。我们已经在 u4/u5 见过 `from_real`——它要做半进位量化、要做饱和、越界要告警，因为它的输入是「随便一个浮点数」，可能根本不在格式范围内、可能精度不够。而 `from_narrowfix` 的输入是一个**已经合法的、已经量化好的 NarrowFix**，所以它什么检查都不用做，只做一次纯表示转换。

#### 4.2.2 核心流程

转换公式就一条（归一化 ↔ 非归一化的标准换算，u5-l1 已建立）：

\[
\text{integer\_value} = \left\lfloor \text{real} \times 2^{F} \right\rfloor
\]

因为 NarrowFix 存的 `real` 已经是某定点值的精确浮点表示（且 `real × 2^F` 一定是整数），所以这里的 `floor` 理论上不改变任何值，只是把浮点结果强制钉成 Python 整数对象。

#### 4.2.3 源码精读

[wide_fix.py:103-114](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L103-L114)：

```python
@staticmethod
def from_narrowfix(a : "NarrowFix"):
    """
    Converts from NarrowFix to WideFix, without quantization or bounds checks.
    """
    int_data = np.floor((a._data*2.0**a._fmt.F).astype(object))

    # Numpy behavior is not clearly defined. Sometimes we get np.float64.
    if isinstance(int_data, np.float64):
        int_data = int(int_data)

    return WideFix(int_data, a._fmt, copy=False)
```

几个要点：

- **docstring 明说「without quantization or bounds checks」**——没有量化、没有边界检查，这是它区别于 `from_real` 的本质。
- `.astype(object)` 把 `float64` 数组转成 `object` 数组（元素是 Python int），这是 WideFix 构造函数硬性要求的 `dtype`（见 [wide_fix.py:55-56](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L55-L56) 的断言）。
- `isinstance(int_data, np.float64)` 那段是在给标量输入兜底：numpy 对单个值的规约行为不明确，偶尔会残留 `np.float64`，这里手动转成 `int`，保证最终落进 WideFix 的全是整数。
- 格式 `a._fmt` 原样透传：格式没变，变的只是「同一份值」的存储方式。

#### 4.2.4 代码实践

1. **实践目标**：验证 `from_narrowfix` 确实是「换皮不换值」。
2. **操作步骤**：

   ```python
   import sys; sys.path.append("bittrue/models/python")
   from en_cl_fix_pkg import *
   import numpy as np

   fmt = FixFormat(0, 2, 4)          # narrow
   n = NarrowFix(1.25, fmt)          # 内部 _data = 1.25 (float64)
   w = WideFix.from_narrowfix(n)     # 搬到 wide

   print("narrow _data :", n._data, type(n._data.flat[0]))
   print("wide   _data :", w._data, type(w._data.flat[0]))
   print("数值一致 :", np.array_equal(w.to_real(warn=False), n._data))
   ```

3. **需要观察的现象**：narrow 内部是 `1.25`（float），wide 内部是 `20`（int，即 `1.25 × 2^4`），但 `to_real` 还原后两者完全相等。
4. **预期结果**：`20` 与 `1.25` 表示同一个定点值，仅存储类型不同。

#### 4.2.5 小练习与答案

**练习 2**：既然 `from_real` 也能把浮点变成 WideFix，为什么分发代码里非要用 `from_narrowfix`，而不是 `WideFix.from_real(a, a_fmt, ...)`？

**参考答案**：因为分发时的输入 `a` 已经是「narrow 路径产出的、合法且量化好的定点数据」。`from_real` 会再做一次半进位量化与饱和，可能**改变本不该改变的值**（例如对刚好在平局点的值再舍一次），还会因越界告警。`from_narrowfix` 不量化、不检查边界，是纯粹的无损表示搬运，语义上才正确。

---

### 4.3 统一分发骨架：以 cl_fix_round 为例

#### 4.3.1 概念说明

`cl_fix_round` 是最能体现分发设计的一个函数：它只有一个数据输入 `a`、一个输入格式 `a_fmt`、一个结果格式 `r_fmt`，输入和结果的宽窄可能**各自独立**。它用 `a_wide`、`r_wide` 两个布尔把所有情况收进一个三分支结构里。理解了它，`cl_fix_saturate` 以及所有算术函数的分发都触类旁通。

#### 4.3.2 核心流程

分发骨架（伪代码）：

```
a_wide = cl_fix_is_wide(a_fmt)     # 输入宽不宽？
r_wide = cl_fix_is_wide(r_fmt)     # 结果宽不宽？

if a_wide or r_wide:               # 只要有一头是宽，就走 WideFix
    a = WideFix(a, a_fmt)                    如果 a 本身宽，直接用
       否则 WideFix.from_narrowfix(...)       如果 a 窄，无损搬成宽
    r = a.round(r_fmt, rnd)                  在整数域完成舍入
    if not r_wide:                           结果要求窄？
        r = NarrowFix(r.to_real(), r_fmt)    再无损搬回 float64
else:                              # 两头都窄，全程 float64
    r = NarrowFix(a, a_fmt).round(r_fmt, rnd)

return r._data                     # 永远只返回原始数据
```

这里有三个设计决策值得专门记住：

1. **判定用 `or`，不是只看输入**。即使输入是窄的，只要**结果格式宽**，也必须走 WideFix——否则 float64 装不下结果。反过来，即使输入宽，只要结果窄，算完后还要搬回 float64（见回转那一行）。
2. **结果回转 narrow 的方式**是 `NarrowFix(r.to_real(), r_fmt)`：先 `to_real()` 把整数域值除以 `2^F` 还原成 real，再用它构造 NarrowFix。这一步依赖「结果格式 r_fmt 确实是窄的」，所以前面 `r_wide` 判定为假时才执行，构造时的 `width <= MAX_WIDTH` 断言自然成立。
3. **返回的永远是 `r._data`**，从不返回对象本身。这就是「唯一表示约定」的落地：调用者拿到的，wide 时是 `object` 数组、narrow 时是 `float64` 数组。

#### 4.3.3 源码精读

完整实现见 [en_cl_fix.py:190-212](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L190-L212)：

```python
def cl_fix_round(a, a_fmt : FixFormat, r_fmt : int, rnd : FixRound):
    assert r_fmt == cl_fix_round_fmt(a_fmt, r_fmt.F, rnd), "..."
    a = _clean_input(a)

    a_wide = cl_fix_is_wide(a_fmt)
    r_wide = cl_fix_is_wide(r_fmt)

    if a_wide or r_wide:
        # WideFix
        a = WideFix(a, a_fmt) if a_wide else WideFix.from_narrowfix(NarrowFix(a, a_fmt, copy=False))
        # Round
        r = a.round(r_fmt, rnd)
        # Convert to narrow if required
        if not r_wide:
            r = NarrowFix(r.to_real(), r_fmt)
    else:
        # NarrowFix
        r = NarrowFix(a, a_fmt, copy=False).round(r_fmt, rnd)

    return r._data
```

逐行对应伪代码：

- 第 197-198 行计算两个布尔，是整段分发的「开关」。
- 第 202 行是 4.2 节讲的那座桥——`a_wide` 为真就直接 `WideFix(a, a_fmt)`（输入本来就是 `object`），否则走 `from_narrowfix` 把 `float64` 搬成整数。
- 第 206-207 行就是「结果回转 narrow」：仅当 `not r_wide` 时执行 `NarrowFix(r.to_real(), r_fmt)`。
- `cl_fix_saturate` 的分发与这一字不差，只是把 `round` 换成 `saturate`（[en_cl_fix.py:222-237](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L222-L237)）。

#### 4.3.4 代码实践

1. **实践目标**：用返回数组的 `dtype` 作为「走了哪条路」的可观测信号。
2. **操作步骤**：

   ```python
   import sys; sys.path.append("bittrue/models/python")
   from en_cl_fix_pkg import *
   import numpy as np

   a_fmt = FixFormat(0, 30, 0)          # width 30, narrow
   a = cl_fix_from_real(1.0, a_fmt)

   # 情况 A：把 F 加到 30，结果格式 [0,30,30] → width 60 → wide
   r_fmt_wide = cl_fix_round_fmt(a_fmt, 30, FixRound.Trunc_s)
   rA = cl_fix_round(a, a_fmt, r_fmt_wide, FixRound.Trunc_s)

   # 情况 B：结果格式仍是窄的 a_fmt
   rB = cl_fix_round(a, a_fmt, a_fmt, FixRound.Trunc_s)

   print("A is_wide(a)=", cl_fix_is_wide(a_fmt),
         "is_wide(r)=", cl_fix_is_wide(r_fmt_wide),
         "dtype=", rA.dtype)
   print("B is_wide(a)=", cl_fix_is_wide(a_fmt),
         "is_wide(r)=", cl_fix_is_wide(a_fmt),
         "dtype=", rB.dtype)
   ```

3. **需要观察的现象**：A 的 `dtype` 为 `object`（输入窄、结果宽，被迫走 WideFix）；B 的 `dtype` 为 `float64`（全程 narrow）。
4. **预期结果**：`dtype` 随结果格式走，正好印证「唯一表示约定」。`dtype` 的精确打印形式待本地验证，但 object / float64 的归属由源码保证。

#### 4.3.5 小练习与答案

**练习 3**：`cl_fix_round` 的判定为什么是 `a_wide or r_wide`，而不是只判 `a_wide`？请给出「输入窄、结果必须宽」的一个具体场景。

**参考答案**：因为结果格式可能与输入格式不同。例如 `a_fmt = [0,30,0]`（narrow），但把小数位加到 30 得到结果格式 `[0,30,30]`（width 60，wide）——float64 装不下 60 位整数，必须走 WideFix。只判 `a_wide` 会错误地走 NarrowFix 路径从而丢精度。对称地，「输入宽、结果窄」也需要 `or r_wide` 之外的回转逻辑把结果搬回 float64。

---

### 4.4 多操作数算术的混合宽度分发

#### 4.4.1 概念说明

算术函数（`add`/`sub`/`mult` 等）比 `round` 多一层复杂度：它有**两个**操作数 `a`、`b`，还多出一个**中间格式 `mid_fmt`**（全精度结果格式，见单元 3）。于是布尔从两个变成三个：`a_wide`、`b_wide`、`mid_wide`。

这里会出现一种本讲最值得玩味的情况——**「窄输入、宽结果」**：`a` 和 `b` 各自都是窄的，但它们相乘后的全精度 `mid_fmt` 超过 53 位。此时虽然输入是 `float64`，运算却必须搬到整数域完成，否则乘积会丢精度。

#### 4.4.2 核心流程

算术分发骨架（以 `cl_fix_add` 为代表）：

```
mid_fmt = cl_fix_add_fmt(a_fmt, b_fmt)      # 先算全精度中间格式
if r_fmt is None: r_fmt = mid_fmt            # 缺省即全精度

a_wide = cl_fix_is_wide(a_fmt)
b_wide = cl_fix_is_wide(b_fmt)
mid_wide= cl_fix_is_wide(mid_fmt)

if a_wide or b_wide or mid_wide:             # 三者任一为宽 → WideFix
    a = WideFix(a, a_fmt)  if a_wide  else WideFix.from_narrowfix(NarrowFix(a, a_fmt, copy=False))
    b = WideFix(b, b_fmt)  if b_wide  else WideFix.from_narrowfix(NarrowFix(b, b_fmt, copy=False))
else:                                        # 三者都窄 → NarrowFix
    a = NarrowFix(a, a_fmt, copy=False)
    b = NarrowFix(b, b_fmt, copy=False)

mid = a + b                                  # 用重载的运算符完成实际运算
return cl_fix_resize(mid._data, mid_fmt, r_fmt, rnd, sat)   # 统一经 resize 出口
```

注意最后一步：算术函数不直接返回，而是把 `mid._data` 连同 `mid_fmt`、`r_fmt` 交给 `cl_fix_resize`。而 `cl_fix_resize` 内部又是 `cl_fix_round` + `cl_fix_saturate`（见 u4-l3），二者各自带 4.3 节那套 `a_wide/r_wide` 分发。所以**「结果回转 narrow」的工作被委托给了 resize→round/saturate**：只要 `r_fmt` 是窄的，最终那一层会自动把 wide 的中间结果搬回 `float64`。这是一个很漂亮的解耦——算术只负责「算对」，宽窄回交由公共出口负责。

单操作数算术（`abs`/`neg`/`shift`）则是上面去掉 `b` 的简化版，判定条件变为 `a_wide or mid_wide`（例如 [en_cl_fix.py:272-283](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L272-L283) 的 `cl_fix_abs`）。

#### 4.4.3 源码精读

`cl_fix_mult` 的分发段（[en_cl_fix.py:407-421](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L407-L421)）：

```python
# Handle narrow/wide internal representation
a_wide = cl_fix_is_wide(a_fmt)
b_wide = cl_fix_is_wide(b_fmt)
mid_wide = cl_fix_is_wide(mid_fmt)
if a_wide or b_wide or mid_wide:
    # WideFix
    a = WideFix(a, a_fmt) if a_wide else WideFix.from_narrowfix(NarrowFix(a, a_fmt, copy=False))
    b = WideFix(b, b_fmt) if b_wide else WideFix.from_narrowfix(NarrowFix(b, b_fmt, copy=False))
else:
    # NarrowFix
    a = NarrowFix(a, a_fmt, copy=False)
    b = NarrowFix(b, b_fmt, copy=False)

mid = a*b
return cl_fix_resize(mid._data, mid_fmt, r_fmt, rnd, sat)
```

对照双操作数 `cl_fix_add`（[en_cl_fix.py:328-342](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L328-L342)），分发结构完全同构，差别只在 `mid_fmt` 由 `cl_fix_add_fmt` 还是 `cl_fix_mult_fmt` 推导、以及实际运算符是 `+` 还是 `*`。

`cl_fix_from_real` / `cl_fix_to_real` 这类转换函数的分发则是「单格式」最简形态：只看 `r_fmt`（或 `a_fmt`）一个布尔（[en_cl_fix.py:130-142](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L130-L142) 与 [en_cl_fix.py:173-184](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L173-L184)）。

#### 4.4.4 代码实践（本讲主实践）

1. **实践目标**：构造一个「窄输入、宽结果」的乘法，跟踪分发路径，并用 `dtype` 验证结果确实经过了 WideFix；再把它 resize 回窄格式，验证结果回转 `float64`。
2. **操作步骤**：

   ```python
   import sys; sys.path.append("bittrue/models/python")
   from en_cl_fix_pkg import *
   import numpy as np

   # 两个 narrow 输入格式：width = 30，都 <= 53
   a_fmt = FixFormat(0, 30, 0)
   b_fmt = FixFormat(0, 30, 0)
   print("a is_wide =", cl_fix_is_wide(a_fmt), " | b is_wide =", cl_fix_is_wide(b_fmt))

   # 全精度乘积格式：[0,60,0]，width=60 > 53 → wide
   mid_fmt = cl_fix_mult_fmt(a_fmt, b_fmt)
   print("mid_fmt =", mid_fmt, " width =", cl_fix_width(mid_fmt),
         " is_wide =", cl_fix_is_wide(mid_fmt))

   # 构造两个窄输入数据（值取 10^6，落在 [0,30,0] 范围内）
   a = cl_fix_from_real(1_000_000.0, a_fmt)
   b = cl_fix_from_real(1_000_000.0, b_fmt)

   # (1) 不给 r_fmt → 默认等于 mid_fmt（wide）→ 结果应为 object 数组
   r_wide = cl_fix_mult(a, a_fmt, b, b_fmt)
   print("(1) wide 结果  dtype =", r_wide.dtype,
         " 值 =", r_wide.flat[0], type(r_wide.flat[0]))

   # (2) 给一个窄 r_fmt [0,40,0] → 经 resize 回转 narrow → 结果应为 float64
   r_narrow = cl_fix_mult(a, a_fmt, b, b_fmt, FixFormat(0, 40, 0))
   print("(2) narrow 结果 dtype =", r_narrow.dtype,
         " 值 =", r_narrow.flat[0], type(r_narrow.flat[0]))

   # (3) 数值自检：两条路算的应当相等
   print("(3) 数值一致 :", np.array_equal(
       np.array(r_wide.flat[0], dtype=object),
       int(r_narrow.flat[0])))
   ```

3. **需要观察的现象**：
   - `a`、`b` 的 `is_wide` 都是 `False`（窄输入），但 `mid_fmt` 的 `is_wide` 是 `True`（宽结果）——这正是「窄输入、宽结果」。
   - 第 (1) 步 `r_wide.dtype` 为 `object`，元素类型是 Python `int`；第 (2) 步 `r_narrow.dtype` 为 `float64`，元素类型是 `float`。
   - 两条路算出的数值相等（`10^6 × 10^6 = 10^12`）。
4. **预期结果**：分发路径为 `a_wide=F, b_wide=F, mid_wide=T → if 分支成立 → 两个输入各经 from_narrowfix 搬成 wide → 整数域相乘 → cl_fix_resize`。第 (1) 步因 `r_fmt=mid_fmt` 仍宽，结果保持 `object`；第 (2) 步 `r_fmt` 窄，resize→round 里的 `NarrowFix(r.to_real(), r_fmt)` 把它搬回 `float64`。具体打印文本待本地验证，但 dtype 与数值一致性由源码逻辑保证。

#### 4.4.5 小练习与答案

**练习 4**：把上面主实践里的输入格式换成 `FixFormat(1, 30, 22)`（width=53，narrow），求它自乘的 `mid_fmt`，并判断分发走哪条路。

**参考答案**：两个有符号输入、各自的 `I+F = 52 > 1`，按 `for_mult` 规则整数位 `+1`，故 `mid_fmt = [1, 30+30+1, 22+22] = [1, 61, 44]`，`width = 1+61+44 = 106 > 53` → wide。虽然两个输入都是 narrow，`mid_wide` 为真，所以仍走 WideFix 分支（两个输入都经 `from_narrowfix` 搬运）。

**练习 5**：为什么算术函数最后统一 `return cl_fix_resize(...)`，而不是各自直接返回 `mid._data`？

**参考答案**：因为 `r_fmt` 可能比 `mid_fmt` 窄（用户要截断/饱和）。把宽窄回转、舍入、饱和全部收口到 `cl_fix_resize`（进而 `cl_fix_round`/`cl_fix_saturate`）这一处，避免在每个算术函数里重复实现「结果回转 narrow」的逻辑，做到算术只管「算对」、出口统一管「放得下且表示正确」。

---

## 5. 综合实践

把本讲三条主线串起来做一个小诊断工具：写一个函数 `trace_dispatch(a, a_fmt, b, b_fmt, r_fmt=None)`，它不真的调用 `cl_fix_mult`，而是**仅依据格式**预测分发路径与返回 `dtype`，再调用 `cl_fix_mult` 验证预测是否属实。

要求：

1. 用 `cl_fix_mult_fmt(a_fmt, b_fmt)` 算 `mid_fmt`。
2. 打印三个布尔：`a_wide`、`b_wide`、`mid_wide`，并据此判定走 WideFix 还是 NarrowFix（条件 `a_wide or b_wide or mid_wide`）。
3. 判定最终 `r_fmt`（缺省即 `mid_fmt`），预测返回 `dtype`：`cl_fix_is_wide(r_fmt)` 为真则 `object`，否则 `float64`。
4. 调用 `cl_fix_mult` 取真实 `dtype`，与预测比对。

至少用两组用例自测：(a) `a_fmt=b_fmt=FixFormat(0,8,8)`（两窄、结果也窄）；(b) `a_fmt=b_fmt=FixFormat(0,30,0)`（两窄、结果宽，主实践的例子）。若预测与实际 `dtype` 全部一致，说明你已彻底掌握分发机制。精确的运行输出待本地验证。

## 6. 本讲小结

- **唯一表示约定**：给定 `fmt`，`cl_fix_*` 进出的原始数据类型唯一——narrow 是 `float64`、wide 是 `object`（Python int），与走了哪条内部路径无关（[en_cl_fix.py:26-32](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L26-L32)）。
- **判定边界**：`cl_fix_is_wide(fmt) = width(fmt) > NarrowFix.MAX_WIDTH`，严格大于 53（[en_cl_fix.py:79-84](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L79-L84)）。
- **无损桥梁**：`WideFix.from_narrowfix` 只做 `floor(real × 2^F)` 的纯表示搬运，不量化、不检查边界，因为输入已是合法量化值（[wide_fix.py:103-114](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L103-L114)）。
- **三分支骨架**：`round`/`saturate` 用 `a_wide or r_wide` 判定，任一为宽即走 WideFix；结果要求窄时用 `NarrowFix(r.to_real(), r_fmt)` 回转（[en_cl_fix.py:197-212](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L197-L212)）。
- **混合宽度算术**：`add`/`mult` 等用 `a_wide or b_wide or mid_wide`，能正确处理「窄输入、宽结果」（[en_cl_fix.py:407-421](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L407-L421)）；宽窄回转统一委托给 `cl_fix_resize` 出口。
- **可观测信号**：返回数组的 `dtype`（`object` ↔ `float64`）就是「走了 WideFix 还是 NarrowFix」的外部指纹。

## 7. 下一步学习建议

本讲讲完了 Python 侧的窄/宽分发，它是单元 6 的句号。接下来有两条自然延伸：

- **走向验证侧（单元 8）**：阅读 [bittrue/cosim/cl_fix_mult/cosim.py:117-118](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/cosim/cl_fix_mult/cosim.py#L117-L118) 等 cosim 脚本，你会看到它们**手动复刻**了本讲的分发模式（`WideFix.from_narrowfix(NarrowFix(...))`），目的是在 Python 里独立算一份「宽路径黄金参考」去和 `cl_fix_*` 的窄路径逐位对拍——这正是分发正确性的终极证明。
- **走向 MATLAB 桥接（单元 9）**：阅读 [matlab_interface.py:6-58](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/matlab_interface.py#L6-L58) 的 `to_uint64_array`/`from_uint64_array`，看 wide 数据如何按 64 位分块跨越 Python↔MATLAB 边界——那里同样用 `cl_fix_is_wide` 做前置断言，是本讲分发判定在跨语言场景的延续。
