# Python 主接口：镜像 HDL 的 cl_fix_* 函数

## 1. 本讲目标

本讲聚焦 en_cl_fix 的 Python 主接口文件 `en_cl_fix.py`。学完后你应当能够：

- 说清楚 `en_cl_fix.py` 里每个 `cl_fix_*` 函数为什么能和 VHDL 包里的同名函数一一对应（镜像关系）。
- 理解并复述「**先算全精度中间格式 `mid_fmt`，再用 `cl_fix_resize` 收敛到目标格式**」这一贯穿全库的核心计算范式。
- 解释 `cl_fix_resize` 内部「先 round、后 saturate」的两步结构。
- 理解 `cl_fix_is_wide` 如何根据位宽把内部计算分发到 `NarrowFix`（≤53 位，快）或 `WideFix`（任意精度，慢）。
- 区分 `r_fmt=None`（默认值，得到无损全精度结果）与显式指定 `r_fmt`（触发舍入/饱和）两种用法。

## 2. 前置知识

阅读本讲前，建议你已掌握（对应前置讲义 u2-l1、u2-l4）：

- **定点格式 `[S, I, F]`**：`S` 为符号位（0 或 1），`I` 为整数位，`F` 为小数位，总位宽 `width = S + I + F`。
- **舍入 `FixRound`** 与 **饱和 `FixSaturate`** 两种枚举的含义：减小 `F` 时要舍入，减小 `I/S` 时要饱和。
- **格式工具函数**：如 `cl_fix_width`、`cl_fix_max_value`、`cl_fix_min_value`。

此外，本讲会用到两个内部类（它们在 u4-l2、u4-l3 详述，本讲只需知道结论）：

- `NarrowFix`：用 IEEE754 双精度浮点存储「归一化」定点数，**快**，但只能精确表示 ≤53 位的格式。
- `WideFix`：用 Python 任意精度整数存储「未归一化」定点数，**慢**，但支持任意位宽。

一个关键事实：`en_cl_fix.py` 对外只暴露「裸数据」（`numpy` 数组或整数数组），`NarrowFix`/`WideFix` 只是内部计算用的载体，用户感知不到它们的存在。这一点在文件头部的 Description 注释里写得很清楚。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py` | Python 主接口，提供与 VHDL 同名的 `cl_fix_*` 函数，是本讲绝对主角。 |
| `bittrue/models/python/en_cl_fix_pkg/__init__.py` | 包的导出入口，把各子模块的符号统一 `from ... import *` 出去。 |
| `bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py` | 定义 `FixFormat` 类及其静态方法 `for_add`/`for_mult`/...，主接口里的格式函数本质是它们的别名。 |
| `bittrue/models/python/en_cl_fix_pkg/narrow_fix.py` | `NarrowFix` 类，定义 `MAX_WIDTH = 53`，是 `cl_fix_is_wide` 的判别依据。 |
| `bittrue/models/python/en_cl_fix_pkg/wide_fix.py` | `WideFix` 类，任意精度整数实现，主接口在 wide 模式下会调用它。 |

## 4. 核心概念与源码讲解

### 4.1 函数别名：Python 接口如何镜像 VHDL 的 `cl_fix_*_fmt`

#### 4.1.1 概念说明

en_cl_fix 的设计哲学是「同一套定点语义，在 VHDL / Python / MATLAB 三种语言里**镜像实现**」。也就是说，三种语言里同名函数的签名、行为、甚至返回的格式都应当完全一致，这样 Python 模型算出来的「黄金参考」可以直接拿来验证 HDL。

在 Python 侧，「结果格式预测」类的函数（`cl_fix_add_fmt`、`cl_fix_mult_fmt` 等）其实**不需要新写算法**——所有逻辑都已经实现为 `FixFormat` 类的静态方法（如 `FixFormat.for_add`）。主接口只是给这些静态方法起一个「和 VHDL 同名」的别名，从而实现镜像。

#### 4.1.2 核心流程

```
VHDL 包: cl_fix_add_fmt(aFmt, bFmt)      ←  综合用
                ↕  同名、同语义（镜像）
Python:   cl_fix_add_fmt(a_fmt, b_fmt)   ←  参考模型用
                ↓  实际就是
          FixFormat.for_add(a_fmt, b_fmt) ←  真正的算法实现在类型层
```

主接口把这些静态方法「绑定」成短名字，调用 `cl_fix_add_fmt(...)` 等价于调用 `FixFormat.for_add(...)`，没有任何额外开销（Python 里函数别名就是赋值，不会多一层包装）。

#### 4.1.3 源码精读

别名定义集中在一处，紧挨着写在一起：

[bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py:L61-L70](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L61-L70)

这一段把 9 个格式预测函数全部绑定到 `FixFormat` 的静态方法上。例如 `cl_fix_mult_fmt = FixFormat.for_mult` 之后，调用 `cl_fix_mult_fmt(a_fmt, b_fmt)` 就是调用乘法结果格式预测。

真正干活的算法在类型文件里，以加法为例：

[bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py:L73-L121](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L73-L121)

`for_add` 用「保守最坏情况」推导出能**精确**容纳 `a+b` 的最小格式（考虑 `rmax` 与 `rmin` 两个极端，判断是否需要多 1 位整数增长）。注意它返回的格式是「充分且必要」的——既不溢出，也不过宽。这部分推导细节属于 u3-l1 的内容，本讲只需记住：**所有 `cl_fix_*_fmt` 都委托到这里的静态方法**。

> 小贴士：正因为是直接别名，你完全可以用 `FixFormat.for_mult(a, b)` 代替 `cl_fix_mult_fmt(a, b)`，二者是同一个函数对象。

#### 4.1.4 代码实践

**实践目标**：验证别名与静态方法是同一个函数对象，并体会镜像命名。

**操作步骤**：

1. 在仓库根目录启动 Python（需先 `pip install numpy`，参见 `requirements.txt`）。
2. 把 `bittrue/models/python` 加入搜索路径并导入包（这与测试文件 `en_cl_fix_pkg_test.py` 的导入方式一致）。
3. 比较两个名字是否指向同一对象。

```python
# 示例代码
import sys
sys.path.append("bittrue/models/python")
from en_cl_fix_pkg import *

a = FixFormat(1, 7, 8)
b = FixFormat(0, 7, 8)

# 1) 别名就是静态方法本身
print(cl_fix_mult_fmt is FixFormat.for_mult)   # 预期 True

# 2) 用别名预测乘法结果格式
mid = cl_fix_mult_fmt(a, b)
print(mid, "width =", cl_fix_width(mid))
```

**需要观察的现象**：第一行打印应为 `True`，说明别名没有额外包装。

**预期结果**：乘法 `[1,7,8] * [0,7,8]` 的全精度结果格式为 `(1, 14, 16)`，位宽 `31`（≤53，属于 narrow）。具体打印值待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 en_cl_fix 不直接让用户调用 `FixFormat.for_add`，而要再提供 `cl_fix_add_fmt` 这个别名？

**参考答案**：为了让 Python 接口与 VHDL 包的函数名一一对应（镜像）。这样同一段定点算法描述（用 `cl_fix_*` 写）可以在 Python 参考模型和 VHDL 综合代码之间无差别地对照，便于协同仿真验证。

**练习 2**：`cl_fix_union_fmt` 绑定到哪个静态方法？它解决什么问题？

**参考答案**：绑定到 `FixFormat.union`（见 [en_cl_fix.py:L70](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L70)）。它返回能同时精确表示多个输入格式的最小公共超集（对 `S/I/F` 分别取 `max`），用于 `for_addsub` 这类「两个候选格式取并集」的场景。

---

### 4.2 全精度中间格式 + resize：`cl_fix_add` / `cl_fix_mult` 的统一范式

#### 4.2.1 概念说明

算术函数（`cl_fix_add`、`cl_fix_sub`、`cl_fix_mult`、`cl_fix_abs`、`cl_fix_neg`、`cl_fix_shift`）都遵循**同一个三段式模板**：

1. **预测全精度中间格式** `mid_fmt = cl_fix_<op>_fmt(...)`——这个格式能**无损**容纳运算结果，不丢任何信息。
2. **在 `mid_fmt` 下做真实运算**（如 `a + b`、`a * b`），得到一个「中间结果」`mid`。
3. **用 `cl_fix_resize` 把 `mid` 收敛到用户想要的目标格式** `r_fmt`，这一步才允许发生舍入和饱和。

这套范式的好处是：**运算本身永远是无损的，所有精度损失都被推迟、并且集中在 `resize` 这一处**。这使得行为可预测、可验证，也和 VHDL 实现完全对齐。

#### 4.2.2 核心流程

以 `cl_fix_mult(a, a_fmt, b, b_fmt, r_fmt=None, rnd, sat)` 为例：

```
1. mid_fmt = cl_fix_mult_fmt(a_fmt, b_fmt)     # 全精度，无损
2. 若 r_fmt 为 None：r_fmt = mid_fmt            # 默认就返回无损结果
3. 把 a、b 统一成 NarrowFix 或 WideFix 对象（见 4.4）
4. mid = a * b                                   # 在 mid_fmt 下精确相乘
5. return cl_fix_resize(mid, mid_fmt, r_fmt, rnd, sat)   # 收敛到目标格式
```

关键点：**当 `r_fmt=None` 时，`r_fmt` 被设为 `mid_fmt`，于是 `resize` 不会改变任何位（既不减小 `F` 也不减小 `I`），结果完全无损**。只有当用户显式指定一个更窄的 `r_fmt` 时，才会触发舍入（`F` 变小）和/或饱和（`I/S` 变小）。

#### 4.2.3 源码精读

先看加法 `cl_fix_add`，它是最典型的三段式：

[bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py:L313-L342](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L313-L342)

重点看这几行：

- [L324](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L324) `mid_fmt = cl_fix_add_fmt(a_fmt, b_fmt)`：第 1 步，预测全精度格式。
- [L325-L326](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L325-L326) `if r_fmt is None: r_fmt = mid_fmt`：默认值语义——不指定目标格式就返回无损结果。
- [L341](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L341) `mid = a+b`：第 4 步，在 `mid_fmt` 下做真实加法（运算符重载，见 4.4）。
- [L342](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L342) `return cl_fix_resize(mid._data, mid_fmt, r_fmt, rnd, sat)`：第 5 步，收敛到目标格式。

再看乘法 `cl_fix_mult`，结构完全一致：

[bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py:L392-L421](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L392-L421)

其中 [L403](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L403) 用 `cl_fix_mult_fmt` 得到 `mid_fmt`，[L420](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L420) 做 `mid = a*b`，最后同样 `cl_fix_resize` 收敛。`cl_fix_sub`、`cl_fix_abs`、`cl_fix_neg`、`cl_fix_shift` 都是同一个模板，只是把 `+` 换成对应运算。

一个特例是 `cl_fix_addsub`（同时支持加/减，由 `add` 布尔数组选择），它**复用** `cl_fix_add` 和 `cl_fix_sub` 再用 `np.where` 合并：

[bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py:L377-L389](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L377-L389)

> 小贴士：`r_fmt=None` 这个默认值是整个接口「无损 vs 有损」的开关。如果你想要一个真实反映硬件位宽的结果，就显式传入一个更窄的 `r_fmt`，并配上 `rnd`/`sat`。

#### 4.2.4 代码实践

**实践目标**：对比 `r_fmt=None`（全精度）与显式窄 `r_fmt`（触发舍入+饱和）的差别。

**操作步骤**：

```python
# 示例代码（承接 4.1.4 的导入）
a_fmt = FixFormat(1, 7, 8)
b_fmt = FixFormat(0, 7, 8)
a = cl_fix_from_real(100.0, a_fmt)   # 100.0，在 [1,7,8] 范围内
b = cl_fix_from_real(100.0, b_fmt)   # 100.0，在 [0,7,8] 范围内

# 情况 A：不指定 r_fmt → 全精度，无损
mid_fmt = cl_fix_mult_fmt(a_fmt, b_fmt)
r_full = cl_fix_mult(a, a_fmt, b, b_fmt)
print("全精度格式:", mid_fmt, " 乘积(实数):", cl_fix_to_real(r_full, mid_fmt))

# 情况 B：指定一个窄得多的 r_fmt，开启舍入+饱和告警
r_narrow = FixFormat(1, 4, 4)
r_cut = cl_fix_mult(a, a_fmt, b, b_fmt,
                    r_fmt=r_narrow,
                    rnd=FixRound.NonSymPos_s,
                    sat=FixSaturate.SatWarn_s)
print("窄格式:", r_narrow, " 截断后(实数):", cl_fix_to_real(r_cut, r_narrow))
```

**需要观察的现象**：

- 情况 A：全精度格式应为 `(1, 14, 16)`，乘积实数值应为 `10000.0`，**没有**任何警告。
- 情况 B：目标格式 `(1,4,4)` 的最大值仅 `2**4 - 2**-4 = 15.9375`，远小于 10000，因此应当**饱和到 15.9375**，并且因为 `sat=SatWarn_s` 会**打印一条告警**（Warning）。

**预期结果**：情况 B 的输出值被钳位到 15.9375，控制台出现 saturation 警告。具体打印文本待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `cl_fix_add` 的 `r_fmt` 留作 `None`，结果会不会发生舍入或饱和？为什么？

**参考答案**：不会。因为 [L325-L326](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L325-L326) 会把 `r_fmt` 设为 `mid_fmt`，随后 `cl_fix_resize` 从 `mid_fmt` 变到 `mid_fmt`，`F` 和 `I/S` 都不变，既不需要舍入也不需要饱和，结果完全无损。

**练习 2**：`cl_fix_addsub` 为什么不自己实现一套加法和减法，而是分别调用 `cl_fix_add` 和 `cl_fix_sub`？

**参考答案**：见 [L387-L389](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L387-L389)。它先算出加法结果 `radd` 和减法结果 `rsub`，再用 `np.where(add, radd, rsub)` 按 `add` 布尔数组逐元素选择。复用单运算函数避免重复实现，也保证语义完全一致。

---

### 4.3 `cl_fix_resize` = 先 round、后 saturate

#### 4.3.1 概念说明

上一节反复出现的 `cl_fix_resize` 是整个库的「精度收敛器」：它把一个格式下的数据搬到另一个格式。搬迁可能同时涉及两类变化：

- `F` 变小 → 需要舍入（round）。
- `I` 或 `S` 变小 → 需要饱和（saturate）。

`cl_fix_resize` 的定义非常简洁：**先舍入，再饱和**。这两步不可交换——必须先把小数位舍入掉（得到一个整数位仍较宽的中间格式），再去判断是否越界并饱和。

#### 4.3.2 核心流程

```
cl_fix_resize(a, a_fmt, r_fmt, rnd, sat):
    1. rounded_fmt = FixFormat.for_round(a_fmt, r_fmt.F, rnd)
       # 把 a 的 F 压到 r_fmt.F，整数位按舍入模式可能 +1
    2. rounded = cl_fix_round(a, a_fmt, rounded_fmt, rnd)   # 舍入
    3. result  = cl_fix_saturate(rounded, rounded_fmt, r_fmt, sat)  # 饱和
    return result
```

这里有个精妙的细节：第 1 步用 `for_round` 算出的 `rounded_fmt`，其 `F` 已经等于 `r_fmt.F`（小数位对齐），但 `I/S` 仍可能比 `r_fmt` 宽（尤其非 Trunc 舍入会让整数位 +1）。这样第 2 步只做「舍入」、第 3 步只做「饱和」，职责分离，且 `cl_fix_saturate` 的前置条件「`F` 不能变」恰好被满足。

#### 4.3.3 源码精读

`cl_fix_resize` 的实现非常短：

[bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py:L240-L253](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L240-L253)

- [L247-L248](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L247-L248)：先算 `rounded_fmt`，再调用 `cl_fix_round` 完成舍入。
- [L251](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L251)：对舍入后的结果调用 `cl_fix_saturate`，收敛到最终 `r_fmt`。

它依赖两个底层函数。`cl_fix_round` 内部会把输入统一成 `NarrowFix`/`WideFix`，调用其 `round` 方法：

[bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py:L190-L212](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L190-L212)

注意 [L194](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L194) 的 `assert`：`cl_fix_round` 强制要求传入的 `r_fmt` 必须等于 `cl_fix_round_fmt(a_fmt, r_fmt.F, rnd)`，否则报错。这正是「结果格式必须先用格式函数预测」的硬约束——防止用户随手填一个非法格式。

`cl_fix_saturate` 同样有前置断言：

[bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py:L215-L237](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L215-L237)

[L219](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L219) 断言 `r_fmt.F == a_fmt.F`，即饱和阶段小数位不允许变化。这条约束解释了为什么 `resize` 必须先 round（把 `F` 对齐）再 saturate。

> 关系图：`cl_fix_resize = cl_fix_round ⟶ cl_fix_saturate`，三者都通过 `cl_fix_is_wide` 分发到 `NarrowFix`/`WideFix` 的同名方法。

#### 4.3.4 代码实践

**实践目标**：单独观察「舍入」与「饱和」两步各自的效果，理解为何不能交换顺序。

**操作步骤**：

```python
# 示例代码
a_fmt = FixFormat(1, 4, 4)      # 范围 [-16, 15.9375]
r_fmt = FixFormat(1, 2, 1)      # 范围 [-4, 3.5]，F 从 4 减到 1（舍入），I 从 4 减到 2（饱和）
a = cl_fix_from_real(3.4375, a_fmt)   # 接近上限

# 第 1 步：只舍入到 rounded_fmt（F=1，整数位可能 +1）
rounded_fmt = FixFormat.for_round(a_fmt, r_fmt.F, FixRound.NonSymPos_s)
rounded = cl_fix_round(a, a_fmt, rounded_fmt, FixRound.NonSymPos_s)
print("rounded_fmt =", rounded_fmt,
      " 舍入后 =", cl_fix_to_real(rounded, rounded_fmt))

# 第 2 步：再饱和到 r_fmt
result = cl_fix_saturate(rounded, rounded_fmt, r_fmt, FixSaturate.SatWarn_s)
print("最终 =", cl_fix_to_real(result, r_fmt))

# 等价地，一步到位：
print("resize =", cl_fix_to_real(
    cl_fix_resize(a, a_fmt, r_fmt, FixRound.NonSymPos_s, FixSaturate.SatWarn_s), r_fmt))
```

**需要观察的现象**：

- `for_round` 在非 Trunc 模式下会让整数位 +1（见 [en_cl_fix_types.py:L334-L336](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L334-L336)），所以 `rounded_fmt` 的 `I` 可能比 `a_fmt.I` 大。
- 分两步的结果与一步 `cl_fix_resize` 的结果应当完全相等。
- 若 `3.4375` 经舍入后超出 `r_fmt` 上限 `3.5`，最终会被饱和并触发告警；具体数值待本地验证。

**预期结果**：两段计算结果一致，证明 `resize` 就是 round→saturate 的组合。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `cl_fix_resize` 必须先 round 再 saturate，而不能反过来？

**参考答案**：因为 `cl_fix_saturate` 要求 `r_fmt.F == a_fmt.F`（[L219](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L219)），即饱和阶段不能改变小数位。只有先 round 把 `F` 对齐到目标，才能进入饱和阶段。此外，舍入本身可能让整数位 +1 从而更易越界，先舍入再饱和才能正确捕获这种由舍入引发的溢出。

**练习 2**：直接调用 `cl_fix_round(a, a_fmt, some_fmt, rnd)` 时，`some_fmt` 可以随便填吗？

**参考答案**：不行。[L194](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L194) 的 `assert` 要求 `some_fmt == cl_fix_round_fmt(a_fmt, some_fmt.F, rnd)`。正确做法是先用 `cl_fix_round_fmt`（即 `FixFormat.for_round`）预测合法格式，再传入。

---

### 4.4 `cl_fix_is_wide`：narrow / wide 内部表示的分发

#### 4.4.1 概念说明

`en_cl_fix.py` 对外只暴露裸数据，但内部计算必须选一个载体：位宽 ≤53 时用 `NarrowFix`（双精度浮点，快），位宽 >53 时用 `WideFix`（任意精度整数，慢但精确）。这个「53 位分界线」由 `cl_fix_is_wide` 判定。

判别规则极其简单：`width > NarrowFix.MAX_WIDTH` 即 wide，否则 narrow。`MAX_WIDTH = 53` 来源于 IEEE754 双精度浮点的尾数位数（52 位显式 + 1 位隐含），保证在该宽度内整数运算精确无误差。

#### 4.4.2 核心流程

每个会接触数据的函数（`cl_fix_round`、`cl_fix_saturate`、`cl_fix_add`、`cl_fix_mult` 等）都遵循同一套分发逻辑：

```
a_wide = cl_fix_is_wide(a_fmt)
[b_wide = cl_fix_is_wide(b_fmt)]
mid_wide = cl_fix_is_wide(mid_fmt)

if a_wide or b_wide or mid_wide:
    # 任一相关格式是 wide，就全程用 WideFix
    a = WideFix(...) 或 WideFix.from_narrowfix(NarrowFix(...))   # narrow→wide 提升
    ...
    mid = a 运算 b
    # 若结果格式其实是 narrow，再转回 narrow
else:
    # 全部 narrow，用 NarrowFix（快路径）
    a = NarrowFix(a, a_fmt, copy=False)
    ...
    mid = a 运算 b
```

一个重要规则：**只要参与运算的任一格式是 wide，整个运算就提升到 WideFix**；如果最终结果格式其实是 narrow，再把结果转回 `NarrowFix`。这是一种「向上兼容」的策略，保证精度永远不丢。

#### 4.4.3 源码精读

判别函数本身只有一行实质逻辑：

[bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py:L79-L84](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L79-L84)

它调用 `cl_fix_width`（[L72-L76](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L72-L76)，返回 `fmt.width`）并与 `NarrowFix.MAX_WIDTH` 比较。

53 这个魔数的来源在 `NarrowFix` 类注释里解释得很清楚：

[bittrue/models/python/en_cl_fix_pkg/narrow_fix.py:L52](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L52)

（其上方的注释说明：双精度浮点能精确表示 54 位有符号 / 53 位无符号整数；为简化有符号数回绕处理，统一保留 53 位上限。）

分发的典型实现在 `cl_fix_round` 里看得很清楚：

[bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py:L197-L212](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L197-L212)

- [L200](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L200) `if a_wide or r_wide:`：只要输入或输出有一个是 wide，就走 WideFix 分支。
- [L202](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L202) `a = WideFix(a, a_fmt) if a_wide else WideFix.from_narrowfix(NarrowFix(a, a_fmt, copy=False))`：narrow 输入会被「提升」成 WideFix 再参与运算。
- [L206-L207](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L206-L207) `if not r_wide: r = NarrowFix(r.to_real(), r_fmt)`：结果格式若是 narrow，再转回 NarrowFix 表示。

二元运算（如 `cl_fix_add`）多判一个 `b_wide` 和 `mid_wide`：

[bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py:L329-L339](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L329-L339)

`cl_fix_mult`、`cl_fix_sub` 等的分发代码与之几乎逐行相同（见 [L407-L418](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L407-L418)）。

> 小贴士：因为运算符 `+`、`*`、`-` 在 `NarrowFix` 和 `WideFix` 里都被重载了（见 [narrow_fix.py:L344-L357](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L344-L357)），所以 `mid = a+b` 这一行对两种载体都成立，主接口无需为 narrow/wide 写两份运算代码。

#### 4.4.4 代码实践

**实践目标**：观察 narrow 与 wide 两种格式下，主接口返回的「裸数据」类型不同。

**操作步骤**：

```python
# 示例代码
narrow_fmt = FixFormat(1, 7, 8)    # width = 16  → narrow
wide_fmt   = FixFormat(1, 40, 40)  # width = 81  → wide

print("narrow_fmt is wide?", cl_fix_is_wide(narrow_fmt))   # 预期 False
print("wide_fmt   is wide?", cl_fix_is_wide(wide_fmt))     # 预期 True

# 看返回的裸数据类型差异
n_data = cl_fix_from_real(1.25, narrow_fmt)
w_data = cl_fix_from_real(1.25, wide_fmt)
print("narrow 数据 dtype:", n_data.dtype)   # 预期 float64
print("wide   数据 dtype:", w_data.dtype)   # 预期 object（Python 大整数）
```

**需要观察的现象**：

- `cl_fix_is_wide` 对 16 位格式返回 `False`，对 81 位格式返回 `True`。
- narrow 数据是 `float64`，wide 数据是 `dtype=object`（存的是 Python 任意精度整数）。

**预期结果**：narrow 路径返回 `numpy.float64` 数组，wide 路径返回 `dtype=object` 的整数数组。具体 dtype 字符串待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `cl_fix_round` 里只要 `a_wide or r_wide` 为真就走 WideFix 分支，而不是只看 `a_wide`？

**参考答案**：因为结果格式 `r_fmt` 也可能很宽（>53 位）。即便输入是 narrow，若输出是 wide，中间的舍入计算也必须在任意精度下进行才能保证正确；反之若输入 wide、输出 narrow，同样需要先在 wide 下算再把结果转回 narrow。所以「任一相关格式 wide → 全程 wide」是最稳妥的策略。

**练习 2**：`MAX_WIDTH` 为什么是 53 而不是 52 或 54？

**参考答案**：见 [narrow_fix.py:L40-L52](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L40-L52) 的注释。IEEE754 双精度有 52 位显式尾数 + 1 位隐含位，理论上能精确表示 54 位有符号 / 53 位无符号整数；为了简化有符号数饱和回绕（wrap）的实现，统一对所有格式取 53 位上限，使有符号和无符号行为一致。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「定点乘法器参考模型」小任务。

**任务背景**：你要为一个 FPGA 乘法器写 Python 参考模型。输入 `a` 是有符号 `[1,7,8]`，输入 `b` 是无符号 `[0,7,8]`，硬件输出格式是 `[1,4,4]`，硬件采用 `NonSymPos_s` 舍入、`SatWarn_s` 饱和。

**要求**：

1. 用 `cl_fix_mult_fmt` 预测全精度中间格式，并打印它（应得 `(1,14,16)`）。
2. 用 `cl_fix_is_wide` 判断中间格式是 narrow 还是 wide。
3. 写两组测试输入：一组乘积落在 `[1,4,4]` 范围内（不饱和），一组乘积远超上限（应饱和并告警）。
4. 分别用 `r_fmt=None`（全精度）和 `r_fmt=FixFormat(1,4,4)` 两种方式调用 `cl_fix_mult`，对比输出。
5. 用 `cl_fix_to_real` 把结果转回浮点，人工核对饱和后的值是否等于 `2**4 - 2**-4 = 15.9375`。

**参考骨架**：

```python
# 示例代码
import sys
sys.path.append("bittrue/models/python")
from en_cl_fix_pkg import *
import numpy as np

a_fmt = FixFormat(1, 7, 8)
b_fmt = FixFormat(0, 7, 8)
out_fmt = FixFormat(1, 4, 4)
rnd = FixRound.NonSymPos_s
sat = FixSaturate.SatWarn_s

# 1) 全精度中间格式
mid_fmt = cl_fix_mult_fmt(a_fmt, b_fmt)
print("mid_fmt =", mid_fmt, " wide?", cl_fix_is_wide(mid_fmt))

# 2) 两组输入
a_in = cl_fix_from_real(np.array([1.5, 100.0]), a_fmt)
b_in = cl_fix_from_real(np.array([2.25, 100.0]), b_fmt)

# 3) 全精度结果（无损）
r_full = cl_fix_mult(a_in, a_fmt, b_in, b_fmt)
print("全精度:", cl_fix_to_real(r_full, mid_fmt))

# 4) 硬件格式结果（舍入+饱和）
r_hw = cl_fix_mult(a_in, a_fmt, b_in, b_fmt, r_fmt=out_fmt, rnd=rnd, sat=sat)
print("硬件格式:", cl_fix_to_real(r_hw, out_fmt))
```

**预期现象**：第一组 `1.5 * 2.25 = 3.375` 落在 `[1,4,4]` 内，两种调用结果接近；第二组 `100 * 100 = 10000` 远超上限，硬件格式结果被饱和到 `15.9375` 并打印告警。具体数值待本地验证。

## 6. 本讲小结

- `en_cl_fix.py` 是 Python 主接口，每个 `cl_fix_*` 函数都与 VHDL 包同名同语义，构成「镜像」。
- 所有 `cl_fix_*_fmt` 格式预测函数其实是 `FixFormat` 静态方法（`for_add`/`for_mult`/...）的**直接别名**，没有额外开销。
- 算术函数统一遵循「**全精度 `mid_fmt` → 真实运算 → `cl_fix_resize` 收敛**」三段式；运算本身无损，精度损失集中在 `resize`。
- `r_fmt=None` 表示返回无损全精度结果；显式指定更窄的 `r_fmt` 才会触发舍入与饱和。
- `cl_fix_resize = cl_fix_round ⟶ cl_fix_saturate`，且必须先舍入（对齐 `F`）再饱和（满足饱和阶段 `F` 不变的约束）。
- `cl_fix_is_wide` 以 53 位为界，把内部计算分发到 `NarrowFix`（快）或 `WideFix`（任意精度），「任一相关格式 wide 即全程 wide」。

## 7. 下一步学习建议

- **u4-l2（NarrowFix）**：深入 `narrow_fix.py`，看双精度浮点如何实现各舍入模式的「加偏移再截断」，以及饱和时回绕为何可能临时降级到 WideFix。
- **u4-l3（WideFix）**：深入 `wide_fix.py`，看任意精度整数如何存储未归一化定点数，以及 `from_narrowfix`/`to_real` 的 narrow↔wide 互转。
- **u3-l1 / u3-l2（格式预测）**：若想彻底理解 `for_add`/`for_mult` 的位宽增长推导，可回到格式预测讲义。
- **u5-l2 / u5-l3（VHDL 实现）**：对照 VHDL 包里的 `cl_fix_add`/`cl_fix_mult`/`cl_fix_resize`，体会「同一范式、两种语言」的镜像之美。
