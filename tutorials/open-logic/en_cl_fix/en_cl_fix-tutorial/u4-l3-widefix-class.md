# WideFix：任意精度整数实现

## 1. 本讲目标

本讲承接 u4-l1（Python 主接口与「全精度中间格式 + resize」范式）和 u4-l2（NarrowFix 的 ≤53 位双精度浮点实现），专门讲解 en_cl_fix 在 Python 端的「另一条腿」——`WideFix` 类。

学完本讲后，读者应该能够：

1. 说出 `WideFix` 与 `NarrowFix` 在**内部数据表示**上的本质区别（未归一化整数 vs 归一化浮点）。
2. 解释为什么以 53 位作为 narrow / wide 的分界，并能用 `cl_fix_is_wide` 判断任意格式的内部表示。
3. 读懂 `from_real` / `from_narrowfix` / `to_real` 三个转换函数，并理解 `to_real` 何时会发出「精度损失」告警。
4. 看懂主接口 `cl_fix_*` 函数中 `a_wide or r_wide`（或 `a_wide or b_wide or mid_wide`）的**提升规则**：只要相关格式中有一个是 wide，整条计算就走 wide 路径，narrow 输入会被临时「升格」为 WideFix。
5. 理解 `cl_fix_random` 为何要对 wide 格式单独用 Python 的 `random.randrange` 与 `object` 数组。

> 本讲只讲 Python 参考模型内部的 wide 表示，**不**重复 u4-l1 的「三段式算术范式」、**不**重复 u4-l2 的舍入偏移推导，也**不**讲 VHDL 侧实现（留待 U5）。

---

## 2. 前置知识

- **定点格式 `[S,I,F]`**（u2-l1）：S 是符号位（0 或 1），I 是整数位，F 是小数位，总位宽 `width = S + I + F`。
- **窄表示 NarrowFix**（u4-l2）：把定点值**归一化**后存进 IEEE 754 双精度浮点（`float64`），快但只能精确表示 ≤53 位的数据。
- **主接口分发**（u4-l1）：`en_cl_fix.py` 里的 `cl_fix_*` 函数对外只暴露「裸数据」（narrow 是 `float64`、wide 是 `object` 整数数组），内部按格式自动选用 NarrowFix 或 WideFix。

如果你还没读过 u4-l2，请先理解一句话：**NarrowFix 用浮点存「真实的数值 1.25」，而 WideFix 用整数存「移位后的数值 20」**（对于 `FixFormat(0,2,4)`，\(1.25 \times 2^4 = 20\)）。本讲就是把这句话拆开讲透。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [bittrue/models/python/en_cl_fix_pkg/wide_fix.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/wide_fix.py) | `WideFix` 类的全部实现：构造、转换、round/saturate/resize、算术与运算符重载。 |
| [bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py) | 主接口：`cl_fix_is_wide` 分界判定，以及各 `cl_fix_*` 函数里的 narrow/wide 分发逻辑。 |
| [bittrue/models/python/en_cl_fix_pkg/narrow_fix.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py) | `NarrowFix`：`MAX_WIDTH = 53` 的由来，以及 saturate 中「narrow 不够算时降级到 wide」的代码路径。 |
| [bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py) | `FixFormat` 及其 `width` 属性、`for_round` 等静态方法（被分发逻辑频繁调用）。 |
| [bittrue/tests/python/en_cl_fix_pkg_test.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/en_cl_fix_pkg_test.py) | `test_Wide_Indexing`：用一个 101 位的 wide 格式验证主接口的索引行为。 |

---

## 4. 核心概念与源码讲解

### 4.1 WideFix 的内部整数表示与构造

#### 4.1.1 概念说明

`WideFix` 解决的是 NarrowFix 解决不了的问题：**当定点数据的位宽超过 53 位时，`float64` 的尾数装不下，必须改用「任意精度整数」来存。**

关键设计差异：

| 维度 | NarrowFix | WideFix |
|------|-----------|---------|
| 内部数据类型 | `float64`（归一化的真实数值） | Python 任意精度整数（`dtype == object`，**未归一化**） |
| 存储内容示例（值 1.25，格式 `(0,2,4)`） | `1.25` | `20`（即 \(1.25 \times 2^4\)） |
| 精度上限 | 53 位 | 无上限（受内存限制） |
| 速度 | 快 | 慢得多 |

「未归一化」是理解 WideFix 的核心：它**不**把小数点对齐到 0 位，而是把整个定点数当成一个**左移了 F 位的整数**来存。设真实值为 \(v\)、小数位为 \(F\)，则 WideFix 内部存的是整数

\[
d = v \cdot 2^{F}
\]

这样做的好处是：所有运算（加、减、乘、移位）都可以直接在整数上做，结果**精确无误**，不依赖任何浮点近似。代价是：两个 wide 数相加前，必须先把它们的**小数点对齐**（即把 F 较小的一方左移补零），这就是后面 `align_binary_points` 和 `add` 里 round 到 `mid_fmt` 的作用。

#### 4.1.2 核心流程

构造一个 WideFix 的流程：

1. 调用方传入「内部整数数据」`data` 和格式 `fmt`。
2. 若 `data` 是单个 Python `int`，包装成 `dtype=object` 的 numpy 数组。
3. **断言** `data.dtype == object`，且首个元素确实是 `int`——这是 WideFix 的「身份证」，防止误把 float64 数据塞进来。
4. 按 `copy` 参数决定是否复制数据；格式**总是**浅拷贝一份。

极值函数 `max_value` / `min_value` 也直接在「未归一化整数」空间里算：

\[
d_{\max} = 2^{I+F} - 1,\qquad d_{\min} = \begin{cases} -2^{I+F} & S=1 \\ 0 & S=0 \end{cases}
\]

注意它们返回的是 WideFix（内部整数），而不是浮点数——这与 NarrowFix 返回归一化浮点形成对照。

#### 4.1.3 源码精读

文件头部的描述注释把「未归一化」讲得很清楚，以 1.25 为例对比两种存储：

[wide_fix.py:28-34](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L28-L34) —— 说明 WideFix 把 \(1.25 \times 2^4 = 20\) 当整数存，而不是存 1.25。

构造函数强制 `dtype == object`，这是 narrow 与 wide 在数据层的根本分界：

[wide_fix.py:47-62](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L47-L62) —— 构造 WideFix：第 53-56 行用两条断言锁死「必须是任意精度整数」。

极值函数直接给出未归一化整数：

[wide_fix.py:129-146](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L129-L146) —— `max_value` 返回 `2**(I+F)-1`，`min_value` 有符号时返回 `-2**(I+F)`、无符号返回 0，全部是整数而非浮点。

对外属性 `data` / `fmt` 都返回**拷贝**，避免外部代码意外修改内部状态：

[wide_fix.py:165-177](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L165-L177)

#### 4.1.4 代码实践

**实践目标**：亲手构造一个 WideFix，确认它的内部数据是「未归一化整数」且类型为 `object`。

**操作步骤**（在仓库根目录执行，需已按 u1-l3 安装依赖）：

```python
# 示例代码：直接操作 WideFix 内部表示
import numpy as np
from en_cl_fix_pkg import FixFormat, WideFix

fmt = FixFormat(0, 2, 4)          # 值 1.25 的二进制是 01.0100
w = WideFix(np.array(20, dtype=object), fmt)   # 20 = 1.25 * 2**4

print("内部数据 =", w._data, "类型 =", w._data.dtype)   # 期望: 20, object
print("读回浮点 =", w.to_real(warn=False))              # 期望: 1.25
```

**需要观察的现象**：`w._data` 是 `20`（不是 1.25），`dtype` 为 `object`。

**预期结果**：打印出 `内部数据 = 20 类型 = object` 与 `读回浮点 = 1.25`，证明 WideFix 存的是移位后的整数。

> 若尚未配置 Python 环境，可仅做源码阅读：对照 [wide_fix.py:29-31](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L29-L31) 理解 1.25→20 的换算。

#### 4.1.5 小练习与答案

**练习 1**：值 \(-0.5\) 在格式 `(1,2,4)` 下的 WideFix 内部整数是多少？

**答案**：\(F=4\)，\(-0.5 \times 2^4 = -8\)，所以内部整数是 `-8`。

**练习 2**：为什么 WideFix 的构造函数要用 `assert data.dtype == object` 而不是 `assert isinstance(data, int)`？

**答案**：因为 WideFix 要支持**数组**（一批定点数），数据存放在 `dtype=object` 的 numpy 数组里，每个元素才是一个 Python `int`。所以先断言数组 dtype 是 object，再断言元素是 int（见第 55-56 行）。

---

### 4.2 浮点 ↔ 宽定点的转换：from_real / from_narrowfix / to_real

#### 4.2.1 概念说明

主接口对外只接受 / 返回「裸数据」。narrow 裸数据是 `float64`，wide 裸数据是 `object` 整数。于是需要在三种表示之间搭桥：

- `from_real(a, fmt)`：把**浮点**转成 WideFix 的内部整数（带 half-up 舍入与饱和）。
- `from_narrowfix(nf)`：把一个 **NarrowFix 对象**无损地「重解释」成 WideFix（不改数值，只换存储方式）。
- `to_real()`：把 WideFix 的内部整数**近似**回 float64，方便人看或回传给只懂浮点的代码。

三者的精度特性不同：`from_narrowfix` 是**无损**的（只是把 \(v\) 换算成 \(v \cdot 2^F\)），`from_real` 会做一次 half-up 量化（因此有舍入），`to_real` 则可能损失精度——因为把一个超过 53 位的整数塞回 float64 时，尾数会丢位。

#### 4.2.2 核心流程

**`from_real(a, r_fmt, saturate)`** 的步骤：

1. 若 `saturate` 含 `Warn`，先把输入极值换算成整数、与 `max/min_value` 比较，越界则 `warnings.warn`。
2. 量化（half-up）：\(\;x = a \cdot 2^{F} + 0.5\;\)，再 `np.floor` 取整，并强制转成 `object`（任意精度）。
3. 若 `saturate` 含 `Sat`，用 `np.where` 把越界值钳到 `max/min_value`；否则（None/Warn 即需要回绕）**直接抛 `NotImplementedError`**——WideFix 的 `from_real` 不支持回绕。
4. 用所得整数和 `r_fmt` 构造 WideFix。

**`from_narrowfix(a)`** 的步骤：

1. 取 NarrowFix 内部的归一化浮点 `a._data`，换算 \(d = \lfloor a._data \cdot 2^{a._fmt.F} \rfloor\)。
2. 处理 numpy 偶尔返回 `np.float64` 标量的边角情况，转成 Python `int`。
3. 用 `copy=False` 构造 WideFix（数据是新生成的，无需再拷）。

**`to_real(warn=True)`** 的步骤：

1. 若 `warn`，检查内部整数是否已超出 float64 精确整数范围（有符号 \([-2^{52}, 2^{52})\)、无符号 \([0, 2^{53})\)），超出则告警「可能损失精度」。
2. 返回 \(d / 2^{F}\) 的 `float64` 数组。

#### 4.2.3 源码精读

`from_real` 的量化与饱和——注意第 87-91 行强制转 `object` 以获得任意精度：

[wide_fix.py:64-101](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L64-L101) —— 第 94-99 行只实现了 `Sat` 分支，回绕直接 `raise NotImplementedError`。

`from_narrowfix` 的无损换算，关键是 `floor(data * 2**F)`：

[wide_fix.py:103-114](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L103-L114)

`to_real` 的精度守卫——这里的 \(2^{52}/2^{53}\) 阈值来自 float64 的 53 位尾数（详见 u4-l2 的 MAX_WIDTH 推导）：

[wide_fix.py:179-188](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L179-L188) —— 第 185-187 行发出精度告警，第 188 行做 \(d/2^{F}\) 的浮点近似。

主接口里 `cl_fix_from_real` / `cl_fix_to_real` 只是按 `cl_fix_is_wide` 选择走 WideFix 还是 NarrowFix，对外仍只返回裸数据：

[en_cl_fix.py:130-142](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L130-L142) —— wide 时返回 `WideFix.from_real(...)._data`（object 整数）。

[en_cl_fix.py:173-184](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L173-L184) —— wide 时返回 `WideFix(a, a_fmt, copy=False).to_real()`（float64）。

#### 4.2.4 代码实践

**实践目标**：验证 `from_real → to_real` 往返在小数值上是**无损**的，但 `to_real` 对超大整数会告警。

**操作步骤**：

```python
# 示例代码：往返精度实验
import warnings
from en_cl_fix_pkg import FixFormat, WideFix
import numpy as np

fmt = FixFormat(0, 2, 4)                       # 8 位无符号，4 位小数
a = np.array([1.5, -0.25, 3.0], dtype=float)
w = WideFix.from_real(a, fmt)                  # object 整数数组
print("内部整数 =", w._data)                    # 期望: [24, -4, 48]（注意 -0.25 越界，会被饱和到 0）
back = w.to_real(warn=False)
print("读回浮点 =", back)                        # 与饱和后的值一致
print("往返无损 =", np.array_equal(back, np.maximum(a, 0)))
```

**需要观察的现象**：`-0.25` 因格式 `(0,2,4)` 无符号、最小值为 0，会被 `from_real` 的 Sat 饱和到 0（内部整数 0）；其余值精确往返。

**预期结果**：内部整数为 `[24, 0, 48]`（`-0.25` 被饱和），读回为 `[1.5, 0.0, 3.0]`。

> 关于「构造 65 位格式做往返」的完整实践见第 5 节综合实践。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `from_real` 在 `saturate=None_s`（要求回绕）时直接抛异常，而不是实现回绕？

**答案**：从浮点构造时回绕的语义模糊且少用；WideFix 的回绕逻辑放在 `saturate()` 方法里（用整数模运算精确实现，见 4.4 节）。`from_real` 的设计定位是「带饱和的便捷构造」，回绕请改走 `resize`。见 [wide_fix.py:97-99](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L97-L99)。

**练习 2**：`to_real` 的精度告警阈值，有符号用 \(2^{52}\)、无符号用 \(2^{53}\)，为什么不统一用 \(2^{53}\)？

**答案**：与 u4-l2 的 `MAX_WIDTH=53` 同源——有符号数要预留一位给符号，保证回绕计算简单且一致，所以有符号精确整数范围更窄（\([-2^{52}, 2^{52})\)）。见 [wide_fix.py:185-187](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L185-L187)。

---

### 4.3 主接口的分发逻辑：cl_fix_is_wide 与「a_wide or r_wide」提升规则

#### 4.3.1 概念说明

主接口 `cl_fix_*` 函数**不**让调用者直接指定「用 NarrowFix 还是 WideFix」。它有一个铁律：**数据的内部表示由格式唯一决定**，判定函数就是 `cl_fix_is_wide`：

```python
def cl_fix_is_wide(fmt):
    return cl_fix_width(fmt) > NarrowFix.MAX_WIDTH   # MAX_WIDTH = 53
```

也就是说，`width = S + I + F > 53` 的格式自动用 wide 表示，否则用 narrow 表示。这条规则让对外接口保持简单：调用者只需提供格式，库自己选最快的、又能保精度的表示。

由此衍生出**提升规则（promotion rule）**：在一趟涉及多个格式的运算里，只要**任意一个相关格式**是 wide，整趟计算就必须走 wide 路径——否则中间结果会被 float64 截断而失真。具体到不同函数：

- 转换类（`cl_fix_round` / `cl_fix_saturate`）：相关格式是输入格式 `a_fmt` 和结果格式 `r_fmt`，判定条件 `a_wide or r_wide`。
- 二元算术（`cl_fix_add` / `cl_fix_sub` / `cl_fix_mult`）：相关格式是 `a_fmt`、`b_fmt` 和全精度中间格式 `mid_fmt`，判定条件 `a_wide or b_wide or mid_wide`。

当某个输入格式是 narrow、但整体需要走 wide 时，该输入会被临时「升格」：用 `WideFix.from_narrowfix(NarrowFix(...))` 把它的浮点表示无损换成整数表示，参与运算后再按结果格式决定是否换回 narrow。

> 注意一个细微但重要的点：对 `cl_fix_round` 而言，`r_fmt` 必须满足 `r_fmt == for_round(a_fmt, r_fmt.F, rnd)` 的断言。`for_round` 不会让一个 narrow 的 `a_fmt` 变成 wide 的 `r_fmt`（最多整数位 +1），所以 cl_fix_round 里 `from_narrowfix` 子分支通常只在「把 narrow 输入舍入到多得离谱的小数位」这种非常规调用时才走到。**真正频繁触发 narrow→wide 提升的是算术函数**：两个接近 53 位的 narrow 输入相加/相乘，`mid_fmt` 会越过 53 位变成 wide，此时两个 narrow 输入都会被 `from_narrowfix` 升格。

#### 4.3.2 核心流程

以 `cl_fix_round` 为典型，分发流程如下：

```
a_wide = cl_fix_is_wide(a_fmt)
r_wide = cl_fix_is_wide(r_fmt)
if a_wide or r_wide:                      # 提升规则
    if a_wide:
        a = WideFix(a, a_fmt)             # 输入已是 wide：直接包
    else:
        a = WideFix.from_narrowfix(NarrowFix(a, a_fmt))   # narrow→wide 升格
    r = a.round(r_fmt, rnd)               # 在 wide 整数域上舍入
    if not r_wide:
        r = NarrowFix(r.to_real(), r_fmt) # 结果要 narrow：换回浮点
else:
    r = NarrowFix(a, a_fmt).round(r_fmt, rnd)   # 全程 narrow
return r._data                            # 永远返回裸数据
```

对二元算术（如 `cl_fix_mult`），只是多了一个 `b_fmt` 的同样处理，以及 `mid_fmt` 也参与 `is_wide` 判定。

#### 4.3.3 源码精读

分界判定——一行决定表示方式：

[en_cl_fix.py:79-84](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L79-L84)

`cl_fix_round` 的完整分发，第 200-207 行就是上面流程图对应的代码：

[en_cl_fix.py:190-212](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L190-L212) —— 第 200 行 `if a_wide or r_wide` 是提升规则的落点；第 202 行的 `from_narrowfix(NarrowFix(...))` 是 narrow→wide 升格。

二元算术 `cl_fix_add` 的分发，注意 `mid_fmt` 也纳入判定：

[en_cl_fix.py:313-342](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L313-L342) —— 第 332 行 `if a_wide or b_wide or mid_wide`，第 334-335 行对 a、b 分别按需升格。

`MAX_WIDTH = 53` 的完整推导（IEEE 754 双精度：1 符号位 + 11 指数位 + 52 尾数位 + 1 隐含位，整数 \([-2^{53}, 2^{53}]\) 可精确表示；为让有符号回绕简单，有符号额外预留一位，得到对有/无符号统一的 53 位上限）：

[narrow_fix.py:40-52](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L40-L52)

一个反方向的「降级」佐证：即便在 NarrowFix 内部，当 `saturate` 的回绕计算会超出 53 位精度时，它也会临时切到整数（wide）域来算——`convert_to_wide` 分支：

[narrow_fix.py:219-232](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L219-L232) —— 这说明「wide = 任意精度整数」不只是一个类，而是一种**保精度的计算手段**，连 NarrowFix 在关键时刻也会借用。

#### 4.3.4 代码实践

**实践目标**：观察「两个 narrow 输入 → wide 中间结果」的真实提升场景，并定位升格发生在哪一行。

**操作步骤**：

```python
# 示例代码：触发 mid_fmt 越过 53 位
from en_cl_fix_pkg import FixFormat, cl_fix_mult, cl_fix_mult_fmt, cl_fix_is_wide

a_fmt = FixFormat(1, 26, 26)   # width = 53，刚好 narrow
b_fmt = FixFormat(1, 26, 26)
mid_fmt = cl_fix_mult_fmt(a_fmt, b_fmt)
print("a wide?", cl_fix_is_wide(a_fmt), " mid wide?", cl_fix_is_wide(mid_fmt))
print("mid_fmt =", mid_fmt, " width =", mid_fmt.width)   # 期望: (1,52,52) width=105 → wide
```

**需要观察的现象**：`a_fmt` 与 `b_fmt` 都是 narrow（`width=53`），但 `mid_fmt` 的 `width=105` 变成 wide。

**预期结果**：`a wide? False  mid wide? True`，`mid_fmt = (1, 52, 52)`。由此可知：当对这两个格式调用 `cl_fix_mult` 时，[en_cl_fix.py:411](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L411) 的 `a_wide or b_wide or mid_wide` 为真，a、b 会在第 413-414 行经 `from_narrowfix` 升格为 WideFix 后再相乘。

> 待本地验证：实际数值结果取决于 numpy 是否已正确安装；若仅做源码阅读，按上面行号跟踪即可。

#### 4.3.5 小练习与答案

**练习 1**：格式 `FixFormat(1, 50, 50)` 是 wide 还是 narrow？为什么？

**答案**：wide。`width = 1+50+50 = 101 > 53`，`cl_fix_is_wide` 返回 `True`。这正是测试 `test_Wide_Indexing` 用的格式（见 [en_cl_fix_pkg_test.py:749-751](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/en_cl_fix_pkg_test.py#L749-L751)）。

**练习 2**：主接口为什么坚持「对外只返回裸数据、由格式决定表示」，而不是直接返回 WideFix/NarrowFix 对象？

**答案**：为了让 Python 参考模型的接口与 VHDL 包**同形**（镜像架构，见 u1-l2）。VHDL 侧只有 `unsigned`/`signed` 位向量这一种「裸数据」，没有 narrow/wide 之分；Python 端用「格式决定表示」把窄/宽的差异藏在内部，对外仍像 VHDL 一样只传递原始数据。参见 [en_cl_fix.py:26-32](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L26-L32) 的模块说明。

---

### 4.4 WideFix 的运算模板与 cl_fix_random 的 wide 处理

#### 4.4.1 概念说明

WideFix 的算术方法（`add` / `sub` / `mult` / `shift` / `neg` / `abs`）与 u4-l1 讲的「三段式」完全同构：**预测全精度 `mid_fmt` → 在 `mid_fmt` 上做无损整数运算 → `resize` 收敛到 `r_fmt`**。差别仅在于运算发生在**任意精度整数**上而非浮点上。

本节聚焦两件 wide 特有的事：

1. **加/减前要对齐小数点**：两个未归一化整数只有 F 相同才能直接相加。`add`/`sub` 先把双方 round 到 `mid_fmt` 的 F（用 `Trunc` 零扩展，不丢位），再相加。
2. **`saturate` 的回绕是精确的整数模运算**：这是 WideFix 相对 NarrowFix 的另一优势——回绕（wrap）在整数域上一行模运算就精确完成，无需像 NarrowFix 那样担心 float64 越界。

而 `cl_fix_random` 对 wide 的特殊处理，是理解「为何 wide 不能用 numpy 原生随机」的最佳例子：当格式范围超过 64 位有符号整数时，`np.random.randint`（底层是固定宽度整数）会溢出，必须改用 Python 的 `random.randrange`（任意精度）逐个生成并填进 `object` 数组。

#### 4.4.2 核心流程

**`WideFix.add(b, r_fmt, rnd, sat)`** 流程：

1. `mid_fmt = for_add(a.fmt, b.fmt)`；`r_fmt` 缺省即为 `mid_fmt`。
2. 把 a、b 各自 round 到「`mid_fmt` 的 F 位、Trunc 模式」——这一步只做小数点对齐（低位补零或丢弃多余的、超出 mid_fmt 的位），不引入额外舍入误差。
3. 两个整数数组直接相加：`a_round._data + b_round._data`。
4. `resize(r_fmt, rnd, sat)` 收敛结果。

**`WideFix.saturate(r_fmt, sat)`** 的回绕分支（None/Warn）：

\[
\text{有符号：}\; v' = ((v + S) \bmod (2S)) - S,\quad S = 2^{I+F}
\]
\[
\text{无符号：}\; v' = v \bmod 2^{I+F}
\]

（Sat 分支则是 `np.where` 钳到 max/min。）

**`cl_fix_random(shape, fmt)`** 的 wide 分支：

1. 取 `fmt_min = cl_fix_min_value(fmt)`、`fmt_max = cl_fix_max_value(fmt)`（wide 时是任意精度整数）。
2. 若 wide：建一个 `dtype=object` 的空数组，**逐个**用 `random.randrange(int(fmt_min), int(fmt_max)+1)` 生成整数（`randrange` 支持任意大整数）。
3. reshape 后包成 WideFix 返回 `_data`。

#### 4.4.3 源码精读

`WideFix.mult` 是三段式的最简范例——乘法无需对齐小数点（整数直接相乘，F 自然为 a.F+b.F）：

[wide_fix.py:395-404](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L395-L404) —— `mid_fmt = for_mult(...)`，第 404 行 `self._data * b._data` 后立刻 `resize`。

`WideFix.add` 展示了「先对齐小数点再相加」：

[wide_fix.py:339-357](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L339-L357) —— 第 351-354 行把 a、b round 到 `mid_fmt.F`（Trunc，纯对齐），第 357 行整数相加。

`WideFix.saturate` 的回绕——纯整数模运算，精确：

[wide_fix.py:275-300](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L275-L300) —— 第 290-294 行就是上面的模运算公式；Sat 分支在第 297-298 行钳位。

`WideFix.round` 的偏移截断——与 u4-l2 / VHDL `cl_fix_round` 同构，只是作用在整数上（用 `>>` 截断低位、用 `2**(f-fr-1)` 作 half）：

[wide_fix.py:212-273](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L212-L273) —— 第 216 行的 assert 复用 `FixFormat.for_round` 作为「格式契约」。

运算符重载让 wide 对象能直接写 `a + b`、`a * b`、`a == b`：

[wide_fix.py:436-458](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L436-L458) —— `__eq__` 等比较运算会先 `align_binary_points` 对齐小数点再比整数。

`cl_fix_random` 的 wide 分支——逐元素 `random.randrange` 填 `object` 数组：

[en_cl_fix.py:484-505](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L484-L505) —— 第 494-500 行是 wide 专用逻辑；对照第 501-505 行 narrow 分支用的 `np.random.randint`，可见 wide 之所以「慢且特别」，根源就是 numpy 原生随机装不下超过 64 位的范围。

#### 4.4.4 代码实践

**实践目标**：生成一个 wide 格式的随机数据，确认它是 `object` 整数数组；并验证 saturate 的回绕在整数域精确成立。

**操作步骤**：

```python
# 示例代码：wide 随机数据与回绕
from en_cl_fix_pkg import FixFormat, cl_fix_random, cl_fix_is_wide
import numpy as np

fmt = FixFormat(1, 50, 50)                 # width=101，wide
print("is_wide?", cl_fix_is_wide(fmt))
r = cl_fix_random((3,), fmt)               # 返回 object 整数数组
print("dtype =", r.dtype, " 样例 =", r[0])
```

**需要观察的现象**：`r.dtype` 为 `object`，每个元素是一个很大的 Python `int`（正负皆有，因为是有符号格式）。

**预期结果**：`is_wide? True`，`dtype = object`，样例是一个约 \(\pm 2^{100}\) 量级的整数。

> 待本地验证：具体随机数值每次不同，重点观察 `dtype=object` 与数值的巨大动态范围。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `cl_fix_random` 对 wide 格式要用 Python 循环逐个生成，而不是 `np.random.randint`？

**答案**：`fmt_min`/`fmt_max` 对 101 位格式是约 \(\pm 2^{100}\) 的整数，远超 numpy 固定宽度整数（int64）能表示的范围；`np.random.randint` 会溢出。Python 的 `random.randrange` 原生支持任意大整数，所以必须逐个生成并塞进 `object` 数组。见 [en_cl_fix.py:494-500](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L494-L500)。

**练习 2**：两个 WideFix 相加前，代码为什么要先 `a.round(a_round_fmt)` 和 `b.round(b_round_fmt)`？这里的 round 会不会引入舍入误差？

**答案**：为了**对齐小数点**——只有 F 相同的两个未归一化整数才能直接相加。这里的 `a_round_fmt` / `b_round_fmt` 用的是 `Trunc` 模式且目标是 `mid_fmt.F = max(a.F, b.F)`：对 F 较小的一方是「低位补零」（无损），对 F 较大的一方…实际上 `mid_fmt.F` 取了 max，所以不会丢位，是无损的对齐，不引入舍入误差。见 [wide_fix.py:351-357](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L351-L357)。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成规格指定的任务：**构造一个 65 位有符号格式，用 `cl_fix_from_real` 写入若干值，用 `cl_fix_to_real` 读回验证精度无损；并跟踪 `cl_fix_round` 内部何时把 narrow 输入转成 WideFix 计算。**

### 任务 A：65 位格式的无损往返

```python
# 示例代码：综合实践 A
import numpy as np
from en_cl_fix_pkg import FixFormat, cl_fix_is_wide, cl_fix_from_real, cl_fix_to_real

fmt = FixFormat(1, 33, 31)        # width = 1+33+31 = 65，有符号 → wide
print("65位格式 is_wide?", cl_fix_is_wide(fmt))   # 期望: True

vals = np.array([1.5, -0.25, 3.0])
data = cl_fix_from_real(vals, fmt)               # 返回 object 整数数组
print("内部数据 dtype =", data.dtype)
print("内部整数 =", data)                          # 1.5*2**31=3221225472 等

back = cl_fix_to_real(data, fmt)                 # object→float64
print("读回 =", back)
print("往返无损(小数值) =", np.array_equal(back, vals))
```

**分析与预期**：

1. `cl_fix_is_wide(fmt)` 为 `True`（65 > 53）。于是 `cl_fix_from_real` 走 [en_cl_fix.py:139-140](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L139-L140) 的 wide 分支，返回 `WideFix.from_real(...)._data`。
2. 这些小数值的内部整数（如 \(1.5 \times 2^{31} = 3221225472\)）远小于 \(2^{52}\)，故 `to_real`（[wide_fix.py:185-188](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L185-L188)）不会告警，往返**无损**：`np.array_equal(back, vals)` 为 `True`。
3. 若想真正体现「wide 能存 53 位以上的值」，可改用整数通路验证（`to_real` 对超大整数会精度损失，故用 `cl_fix_to_integer`）：

```python
# 示例代码：验证 >53 位整数的精确存储（to_real 会失真，故用 to_integer）
from en_cl_fix_pkg import cl_fix_from_integer, cl_fix_to_integer
big_fmt = FixFormat(1, 64, 0)                 # 65 位纯整数
big = cl_fix_from_integer(2**63 - 1, big_fmt) # 直接注入大整数
print("2**63-1 存取无损 ?", cl_fix_to_integer(big, big_fmt) == 2**63 - 1)  # 期望: True
```

> 这一对比正好说明：WideFix 的**存储**是任意精度无损的；`to_real` 的**读出**受 float64 限制。区分二者是本讲的关键认知。

### 任务 B：跟踪 cl_fix_round 内部的 narrow→wide

由 4.3 节的分析，`cl_fix_round` 中 `from_narrowfix` 子分支在常规调用（narrow 输入、按 `for_round` 缩减小数位）下**不会**触发，因为 `for_round` 不会把 narrow 变 wide。因此分两步观察：

**步骤 B1：wide 输入走 wide 路径**（最常见）

```python
# 示例代码：wide 输入的 round
from en_cl_fix_pkg import (FixFormat, FixRound, cl_fix_from_integer,
                           cl_fix_round, cl_fix_round_fmt)
a_fmt = FixFormat(1, 33, 31)                    # wide，65 位
a = cl_fix_from_integer(2**60, a_fmt)           # 一个大整数
r_fmt = cl_fix_round_fmt(a_fmt, 0, FixRound.Trunc_s)   # (1,33,0)，narrow 结果格式
r = cl_fix_round(a, a_fmt, r_fmt, FixRound.Trunc_s)
```

跟踪：在 [en_cl_fix.py:197-204](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L197-L204)，`a_wide=True` → `if a_wide or r_wide` 成立 → 走第 202 行 `WideFix(a, a_fmt)`（注意是 a_wide 分支，**不是** from_narrowfix）。

**步骤 B2：narrow→wide 提升在算术中才频繁发生**

```python
# 示例代码：两个 narrow 输入产生 wide 中间格式（见 4.3.4）
from en_cl_fix_pkg import FixFormat, cl_fix_mult, cl_fix_mult_fmt, cl_fix_is_wide
a_fmt = FixFormat(1, 26, 26)   # width 53，narrow
mid_fmt = cl_fix_mult_fmt(a_fmt, a_fmt)
print("mid wide?", cl_fix_is_wide(mid_fmt))    # True（width=105）
```

跟踪：调用 `cl_fix_mult(a, a_fmt, b, b_fmt)` 时，[en_cl_fix.py:408-414](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L408-L414) 中 `mid_wide=True` → 提升成立 → 第 413-414 行的 `WideFix.from_narrowfix(NarrowFix(...))` **正是** narrow 输入被转成 WideFix 计算的落点。

**结论**：`from_narrowfix` 这条 narrow→wide 升格路径，**在 cl_fix_round 里近乎 dormant**，而在 `cl_fix_add/sub/mult`（当 `mid_fmt` 越过 53 位时）才是日常触发点。这是本讲最值得记住的「源码细节」。

> 待本地验证：以上脚本需在已安装 numpy 的环境中运行；若只做源码阅读，按给出的行号在 [en_cl_fix.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py) 与 [wide_fix.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/wide_fix.py) 之间跟踪即可。

---

## 6. 本讲小结

- **WideFix 用未归一化的任意精度整数存数据**：内部存 \(d = v \cdot 2^{F}\)（`dtype=object` 的 Python 大整数），与 NarrowFix 的归一化 `float64` 形成对照。
- **53 位是 narrow/wide 的分界**：`cl_fix_is_wide(fmt) = (fmt.width > NarrowFix.MAX_WIDTH)`，而 `MAX_WIDTH=53` 来自 IEEE 754 双精度的尾数位数（并为有符号回绕预留一位）。
- **三个转换函数精度特性不同**：`from_narrowfix` 无损重解释、`from_real` 带 half-up 量化与饱和（不支持回绕）、`to_real` 是 float64 近似（内部整数越过 \(\pm 2^{52}/2^{53}\) 会告警）。
- **主接口用「提升规则」分发**：相关格式中任意一个 wide，整趟计算就走 wide 路径；narrow 输入经 `WideFix.from_narrowfix(NarrowFix(...))` 临时升格，结果再按 `r_fmt` 决定是否换回 narrow。对外永远只返回裸数据，以保持与 VHDL 接口的镜像。
- **`from_narrowfix` 在 cl_fix_round 里近乎 dormant**，真正频繁触发 narrow→wide 提升的是算术函数（`mid_fmt` 越过 53 位时）；NarrowFix 自身的 saturate 回绕在精度不足时也会借用整数域计算。
- **WideFix 的算术仍是「mid_fmt → 无损整数运算 → resize」三段式**，回绕用精确整数模运算；`cl_fix_random` 对 wide 必须用 `random.randrange` 逐元素填 `object` 数组，因为 numpy 原生随机装不下 >64 位范围。

---

## 7. 下一步学习建议

- **横向对照 VHDL 实现**：本讲的 `WideFix.round` / `saturate` 是 Python 参考模型；对应的 RTL 实现在 u5-l2（`cl_fix_round` / `cl_fix_saturate` / `cl_fix_resize` 的 VHDL 实现）。建议对照阅读，体会「整数域偏移截断」如何映射到「位向量对齐 + 钳位」。
- **向上回看主接口**：若对 `mid_fmt + resize` 三段式仍不熟，复习 u4-l1；对 NarrowFix 的浮点实现不熟，复习 u4-l2——本讲的提升规则只有放在 narrow/wide 的整体框架里才完整。
- **向下进入验证流程**：WideFix 的精确性正是它被选作「黄金参考模型」的原因。接下来可学 u7-l1（cosim 用 Python 参考模型生成黄金数据）和 u8-l3（Python 单元测试如何对照标准实现验证正确性），看 wide 表示如何为 >53 位硬件通路提供可信验证基准。
- **建议继续阅读的源码**：[wide_fix.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/wide_fix.py) 的 `shift`（可变移位的逐元素处理）与 `to_uint64_array` / `from_uint64_array`（wide 数据打包成 uint64 数组跨语言传递，承接 u8-l1 的 MATLAB 互操作）。
