# resize = round + saturate，以及 in_range

## 1. 本讲目标

本讲把前两讲（舍入 u4-l1、饱和 u4-l2）的两块拼图粘合成一个统一入口——`cl_fix_resize`。学完后你应当能够：

1. 说清 `cl_fix_resize` 的执行顺序：先算 `rounded_fmt`，再 `round`，最后 `saturate`。
2. 解释**为什么必须是「先舍入、后饱和」，顺序不能对调**——这是本讲最核心的设计点。
3. 读懂 `cl_fix_in_range` 如何用**与 resize 完全一致的口径**预测「这次转换会不会触发饱和」。
4. 对照 Python 与 VHDL 两份实现，确认它们语义 bit-true 一致。

---

## 2. 前置知识

本讲默认你已经掌握：

- **格式三字段 `[S, I, F]`**（u1-l2）：S 符号位、I 整数位、F 小数位，总位宽 `S+I+F`。
- **舍入 `cl_fix_round`**（u4-l1）：减少小数位 F 时的处理；统一框架是「加偏移再 floor」；除截断 `Trunc_s` 外，其余模式都可能向上进位，因此结果格式 `for_round` 会给整数位 +1。
- **饱和 `cl_fix_saturate`**（u4-l2）：减少整数位 I / 符号位 S 时的处理；有**硬性前提**——`r_fmt.F == a_fmt.F`（小数位不能变）；四模式 `None_s/Warn_s`（回绕）与 `Sat_s/SatWarn_s`（钳位）。
- **NarrowFix/WideFix 双表示**（u6 概念预告）：位宽 ≤ 53 走 float64 的 NarrowFix，否则走任意精度整数的 WideFix；`cl_fix_*` 主接口会按 `cl_fix_is_wide` 自动分发。

一个贯穿本讲的关键直觉：

> **舍入改的是 LSB（小数端），饱和改的是 MSB（整数/符号端）。** 而舍入在「进位」时会把数值往上顶，可能顶破 MSB 上限——这正是两步必须按特定顺序串联的根本原因。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲引用的关键位置 |
| --- | --- | --- |
| `bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py` | Python 主接口，所有 `cl_fix_*` 函数 | `cl_fix_resize`、`cl_fix_in_range`、以及它调用的 `cl_fix_round`/`cl_fix_saturate` |
| `bittrue/models/python/en_cl_fix_pkg/narrow_fix.py` | ≤53 位浮点域实现 | `NarrowFix.resize`、`NarrowFix.in_range` |
| `bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py` | 类型与格式推导 | `FixFormat.for_round`（算 `rounded_fmt` 的依据） |
| `hdl/en_cl_fix_pkg.vhd` | VHDL 镜像（可综合 RTL + TB 共用包） | `cl_fix_resize`/`cl_fix_in_range` 的签名与函数体、内部 `convert`/`cl_fix_saturate` |
| `bittrue/tests/python/en_cl_fix_pkg_test.py` | 人工挑选的断言测试 | `cl_fix_resize_Test` 用例集 |

---

## 4. 核心概念与源码讲解

### 4.1 resize 的统一骨架：`cl_fix_resize`（Python 主接口）

#### 4.1.1 概念说明

定点库里，「把数据从格式 A 放进格式 R」这件事在现实中往往**同时**要改小数位（LSB）和整数位（MSB）。可是：

- `cl_fix_round` 只会改 F（减少小数位），且**可能让数值变大**（进位）。
- `cl_fix_saturate` 只会改 I/S（减少整数/符号位），且**硬性要求 F 不变**。

两者单独都无法完成「F 和 I 同时改变」。于是库提供了一个组合函数 `cl_fix_resize`：先用 round 把 F 对齐到目标，再用 saturate 把 I/S 收拢到目标。它也是**所有算术运算的统一出口**——`cl_fix_add`/`cl_fix_mult` 等都在最后一步调用 `cl_fix_resize` 把全精度中间结果塞进用户要的目标格式。

> 一句话：**`resize = round（对齐 LSB）→ saturate（收拢 MSB）`**。

#### 4.1.2 核心流程

`cl_fix_resize(a, a_fmt, r_fmt, rnd, sat)` 的三步：

1. **算中间格式** `rounded_fmt = FixFormat.for_round(a_fmt, r_fmt.F, rnd)`
   - 它的 F 恒等于 `r_fmt.F`（把小数位先对齐到目标）。
   - 它的 I 在「减少小数位且非截断」时比 `a_fmt.I` 多 1（给可能的进位预留空间）。
2. **舍入** `rounded = cl_fix_round(a, a_fmt, rounded_fmt, rnd)`：把数据从 `a_fmt` 舍入到 `rounded_fmt`，此时小数位已是 `r_fmt.F`。
3. **饱和** `result = cl_fix_saturate(rounded, rounded_fmt, r_fmt, sat)`：因为 `rounded_fmt.F == r_fmt.F`，saturate 的前提成立，可以安全地把 I/S 收拢到 `r_fmt`。

伪代码：

```
function cl_fix_resize(a, a_fmt, r_fmt, rnd, sat):
    rounded_fmt = for_round(a_fmt, r_fmt.F, rnd)   # F = r_fmt.F，I 可能 +1
    rounded     = round(a, a_fmt, rounded_fmt, rnd)
    result      = saturate(rounded, rounded_fmt, r_fmt, sat)
    return result
```

#### 4.1.3 源码精读

Python 主接口里的 `cl_fix_resize` 就是上面三步的直接翻译，逻辑极其简洁：

[en_cl_fix.py:240-253](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L240-L253) —— `cl_fix_resize` 函数体：先 `cl_fix_round` 到 `rounded_fmt`，再 `cl_fix_saturate` 到 `r_fmt`，注释明确写着 "with rounding, then saturation"。

```python
def cl_fix_resize(a, a_fmt, r_fmt,
                  rnd=FixRound.Trunc_s, sat=FixSaturate.None_s):
    # Round
    rounded_fmt = FixFormat.for_round(a_fmt, r_fmt.F, rnd)
    rounded = cl_fix_round(a, a_fmt, rounded_fmt, rnd)
    # Saturate
    result = cl_fix_saturate(rounded, rounded_fmt, r_fmt, sat)
    return result
```

注意两个默认值：Python 的 `cl_fix_resize` 默认 `rnd=Trunc_s`（截断）、`sat=None_s`（回绕、静默）。这与 VHDL 默认值不同（见 4.4），是一个跨语言坑。

它调用的两个子函数分别承担一步：

- [en_cl_fix.py:190-212](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L190-L212) —— `cl_fix_round`：先断言 `r_fmt == cl_fix_round_fmt(...)`（即调用者传入的格式必须等于保守推导值），再按 narrow/wide 分发执行舍入。
- [en_cl_fix.py:215-237](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L215-L237) —— `cl_fix_saturate`：开头断言 `r_fmt.F == a_fmt.F`（小数位不能变），这正是 resize 必须先 round 对齐 F 的原因。

`rounded_fmt` 的来源 `FixFormat.for_round`：

[en_cl_fix_types.py:318-342](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L318-L342) —— `for_round(a_fmt, rFracBits, rnd)`：F 恒为 `rFracBits`；只有「减少小数位且非截断」时 I 才 +1。最后 `return FixFormat(a_fmt.S, I, rFracBits)`，保证了 `rounded_fmt.F == r_fmt.F`，让下一步 saturate 的前提自动满足。

> 设计巧思：`for_round` 的 F 永远等于目标 F，所以「round 之后再 saturate」天然合法——这是整个 resize 能用两步串联的基石。

#### 4.1.4 代码实践

**目标**：亲手验证 resize 的两步拆解，确认它等价于「先 round 再 saturate」。

**操作步骤**（在仓库根目录，已 `pip install -r requirements.txt`）：

```python
import sys
sys.path.insert(0, "bittrue/models/python")
from en_cl_fix_pkg import *

a_fmt = FixFormat(1, 3, 1)   # 有符号，3 整数位，1 小数位，范围 [-8, 7.5]
r_fmt = FixFormat(1, 3, 0)   # 目标：去掉小数位，范围 [-8, 7]

a = 7.5                       # 恰好是 a_fmt 的最大值

# 方式一：直接 resize（NonSymPos = 四舍五入，半值向上；None_s = 回绕）
print("resize wrap :", cl_fix_resize(a, a_fmt, r_fmt, FixRound.NonSymPos_s, FixSaturate.None_s))
print("resize sat  :", cl_fix_resize(a, a_fmt, r_fmt, FixRound.NonSymPos_s, FixSaturate.Sat_s))

# 方式二：手动复现两步
rounded_fmt = FixFormat.for_round(a_fmt, r_fmt.F, FixRound.NonSymPos_s)
print("rounded_fmt :", rounded_fmt)                                   # 应为 [1,4,0]（I 多了 1）
rounded = cl_fix_round(a, a_fmt, rounded_fmt, FixRound.NonSymPos_s)
print("after round :", rounded)
print("after satur :", cl_fix_saturate(rounded, rounded_fmt, r_fmt, FixSaturate.Sat_s))
```

**需要观察的现象**：

- `rounded_fmt` 打印为 `[1,4,0]`——整数位比 `a_fmt` 多 1，因为非截断舍入可能进位。
- 「方式一」与「方式二」结果完全一致。

**预期结果**：

- `resize wrap` = `-8.0`（7.5 进位成 8.0，8.0 超出 [-8,7] → 回绕到 -8.0）。
- `resize sat` = `7.0`（8.0 被钳位到最大值 7.0）。

如果本地未装 numpy/vunit-hdl，则标注「待本地验证」，但可先通过阅读 [en_cl_fix_pkg_test.py:170 与 173](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py#L170-L173) 的断言确认这两个期望值正是测试里写死的。

#### 4.1.5 小练习与答案

**练习 1**：`cl_fix_resize(2.5, FixFormat(1,2,1), FixFormat(1,2,1))`（源格式=目标格式）结果是多少？为什么 `rounded_fmt` 不会增长整数位？

> **答案**：结果是 `2.5`。因为目标 F（=1）≥ 源 F（=1），`for_round` 走 "fractional bits are not being reduced" 分支，I 不变，`rounded_fmt == a_fmt`，round 与 saturate 都是无操作。

**练习 2**：为什么 `cl_fix_resize` 的默认饱和模式是 `None_s`（回绕），而 `cl_fix_from_real` 默认是 `SatWarn_s`（钳位+告警）？

> **答案**：`resize` 是通用底层工具，默认「不动声色」地把结果塞进目标格式（回绕是硬件最自然的行为）；`from_real` 是高层「把浮点数存进定点」的便利函数，默认期望「宁可钳位也别悄悄回绕」，所以用 `SatWarn_s`。源码注释也提示：若需要别的舍入模式或不要饱和，应改用 `cl_fix_resize`（见 [en_cl_fix.py:130-142](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L130-L142)）。

---

### 4.2 `NarrowFix.resize` 与 `NarrowFix.in_range`：浮点域的两步实现

#### 4.2.1 概念说明

`cl_fix_resize` 是面向用户的「裸数据」接口（输入输出是 numpy 数组/标量）。真正在 float64 内部搬运数值的是 `NarrowFix.resize`，它的结构与 `cl_fix_resize` **逐行对应**。配套的 `NarrowFix.in_range` 则回答一个问题：

> 如果把这批数据 resize 到 `r_fmt`，**会不会触发饱和？**（注意：只判断、不修改数据。）

`in_range` 的口径必须与 resize 严格一致——否则它预测的「不会饱和」就会和 resize 实际行为对不上。所以它内部也先做一次 round，再比范围。

#### 4.2.2 核心流程

`NarrowFix.resize(r_fmt, rnd, sat)`：

1. `rounded_fmt = FixFormat.for_round(self._fmt, r_fmt.F, rnd)`
2. `rounded = self.round(rounded_fmt, rnd)`
3. `return rounded.saturate(r_fmt, sat)`

`NarrowFix.in_range(r_fmt, rnd)`：

1. `rounded_fmt = FixFormat.for_round(self._fmt, r_fmt.F, rnd)` —— 与 resize 同一个中间格式。
2. `rounded = self.round(rounded_fmt, rnd)` —— 先按相同舍入模式量化。
3. 判断 `rounded` 是否落在 `[min_value(r_fmt), max_value(r_fmt)]` 内，逐元素返回布尔数组。

关键点：**`in_range` 的 round 步骤不可省**——因为舍入可能把一个原本在范围内的值顶出范围（见 4.3）。不先 round 就比范围，会漏报这种「舍入诱发溢出」。

#### 4.2.3 源码精读

[NarrowFix.resize:246-255](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L246-L255) —— 与 `cl_fix_resize` 完全同构的两步：

```python
def resize(self, r_fmt, rnd=FixRound.Trunc_s, sat=FixSaturate.None_s):
    # Round
    rounded_fmt = FixFormat.for_round(self._fmt, r_fmt.F, rnd)
    rounded = self.round(rounded_fmt, rnd)
    # Saturate
    return rounded.saturate(r_fmt, sat)
```

[NarrowFix.in_range:147-155](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L147-L155) —— 先 round 到 `rounded_fmt`，再用 `min_value/max_value` 比 lo/hi：

```python
def in_range(self, r_fmt, rnd=FixRound.Trunc_s):
    rounded_fmt = FixFormat.for_round(self._fmt, r_fmt.F, rnd)
    rounded = self.round(rounded_fmt, rnd)
    lo = np.where(rounded < NarrowFix.min_value(r_fmt), False, True)
    hi = np.where(rounded > NarrowFix.max_value(r_fmt), False, True)
    return np.where(np.logical_and(lo, hi), True, False)
```

它调用的 [NarrowFix.round:157-188](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L157-L188) 与 [NarrowFix.saturate:190-244](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L190-L244) 已在 u4-l1/u4-l2 详述。注意 `NarrowFix.saturate` 在有符号回绕、且中间格式会突破 float64 的 53 位上限时会**回退到整数域**计算（[narrow_fix.py:52](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L52) 的 `MAX_WIDTH = 53`），这正是 `cl_fix_resize` 在窄路径下仍能保持精度的原因。

#### 4.2.4 代码实践

**目标**：用 `in_range` 预测 resize 是否会饱和，再与实际 resize 结果对照。

**操作步骤**：

```python
import sys; sys.path.insert(0, "bittrue/models/python")
from en_cl_fix_pkg import *

a_fmt = FixFormat(1, 3, 1)
r_fmt = FixFormat(1, 3, 0)
vals = [-8.0, -0.5, 0.5, 7.0, 7.5]           # 7.5 是源格式最大值

# 用与 resize 相同的舍入模式预测
will_fit = cl_fix_in_range(vals, a_fmt, r_fmt, FixRound.NonSymPos_s)
print("in_range   :", will_fit)               # 7.5 应为 False（会进位成 8.0 超界）

# 实际 resize（回绕），看哪些值被改变了
out = cl_fix_resize(vals, a_fmt, r_fmt, FixRound.NonSymPos_s, FixSaturate.None_s)
print("resize wrap:", out)
```

**需要观察的现象**：`in_range` 对 `7.5` 返回 `False`（预测会饱和），而实际回绕结果把 `7.5 → 8.0 → -8.0` 确实「出了范围」。

**预期结果**：`in_range` 数组里对应 7.5 的元素为 `False`；其余（-8.0、-0.5→0、0.5→1、7.0）为 `True`。若环境未就绪则「待本地验证」，但逻辑可由 4.2.3 的源码直接推出。

#### 4.2.5 小练习与答案

**练习 1**：如果给 `cl_fix_in_range` 传入的 `rnd` 与你之后 `cl_fix_resize` 用的 `rnd` 不一致，会发生什么？

> **答案**：预测会失准。`in_range` 用传入的 `rnd` 决定 `rounded_fmt`（是否 +1 整数位）和量化方式；若与 resize 实际用的模式不同，可能漏报或误报饱和。**正确用法是两处传同一个 `rnd`。**

**练习 2**：`in_range` 返回 `True` 是否意味着「resize 后数值不变」？

> **答案**：不是。`in_range` 只保证「不饱和」，但若 `r_fmt.F < a_fmt.F`，舍入仍会改变 LSB（例如 0.5 在 NonSymPos 下变 1.0）。`True` 仅表示「不会触发 saturate 的钳位/回绕」，不代表数值恒等。

---

### 4.3 为什么必须「先 round 后 saturate」：顺序不可对调

#### 4.3.1 概念说明

这是本讲要回答的核心问题：**为什么不能先 saturate 再 round？** 答案有两层：

1. **语法层（硬约束）**：`cl_fix_saturate` 要求 `r_fmt.F == a_fmt.F`。如果一开始就试图 saturate 到一个 F 不同的目标，断言直接失败——saturate 根本没法独立完成「改 F」的工作。必须先 round 把 F 对齐。

2. **语义层（数值正确）**：舍入会**进位**，进位可能把一个「原本在范围内」的值顶到「超出范围」。只有先 round 得到「舍入后的真实值」，再 saturate，才能正确捕获这种进位诱发的溢出。若反过来先 saturate：源值 7.5 在源格式里恰好是最大值、完全合法，saturate 不会动它；之后再 round 成 8.0，但 8.0 已不在目标格式内——结果要么悄悄错误、要么回绕，**饱和保护被绕过**。

用一张示意图理解「进位诱发溢出」：

```
源格式 [1,3,1]，范围 [-8, 7.5]        目标格式 [1,3,0]，范围 [-8, 7]
值 7.5 ──(NonSymPos 舍入, +0.5 进位)──> 8.0 ──> 超出 7，必须 saturate 钳到 7.0
```

数学上，舍入把值往「最近的格点」凑，最大可能进位量是半个结果 LSB：

\[
\text{舍入后值} \in [x,\ x + 2^{-(rF+1)}] \quad (\text{向上进位时})
\]

当源值已经贴着源格式上限时，这半个 LSB 的进位就可能越过目标格式上限 \(\,2^{I}-2^{-F}\)。

#### 4.3.2 核心流程

「先 saturate 再 round」错在哪（反例推演）：

```
错误顺序（假设能强行执行）：
  saturate(7.5, [1,3,1], [1,3,0])  →  断言失败(F 不同)，或若用中间格式则 7.5 仍合法、不动
  round(7.5, ...)                  →  8.0（已超界，但饱和已过，无人拦截）❌

正确顺序（resize 实际做的）：
  round(7.5, [1,3,1], [1,4,0], NonSymPos) → 8.0   # 进位后用 +1 整数位安全容纳
  saturate(8.0, [1,4,0], [1,3,0], Sat)    → 7.0   # 这一步才钳位 ✅
```

注意 `for_round` 给整数位 +1 的设计与此**直接配套**：进位后的 8.0 在中间格式 `[1,4,0]`（范围 [-16,15]）里完全合法，round 不会自己溢出；多出的这 1 位整数位随后交给 saturate 收回。两步各自合法、衔接无缝。

#### 4.3.3 源码精读

测试文件里恰好有这个「进位诱发溢出」的标准用例，分回绕与钳位两条：

[en_cl_fix_pkg_test.py:170](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py#L170) —— `bittrue/tests/python/en_cl_fix_pkg_test.py` 第 170 行：

```python
self.assertEqual(-8.0, cl_fix_resize(7.5, FixFormat(True,3,1), FixFormat(True,3,0), FixRound.NonSymPos_s, FixSaturate.None_s))
```

[en_cl_fix_pkg_test.py:173](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py#L173) 第 173 行（仅把饱和模式换成 `Sat_s`）：

```python
self.assertEqual(7.0, cl_fix_resize(7.5, FixFormat(True,3,1), FixFormat(True,3,0), FixRound.NonSymPos_s, FixSaturate.Sat_s))
```

> 同一个输入 `7.5`、同一个舍入 `NonSymPos_s`，只因饱和模式不同而得到 `-8.0`（回绕）与 `7.0`（钳位）。这恰恰说明：**round 把 7.5 顶成了 8.0，是 saturate 在第二步把它处理掉的**——顺序若是反过来，saturate 根本看不到这个 8.0。

完整的 `cl_fix_resize_Test` 类见 [en_cl_fix_pkg_test.py:109-179](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py#L109-L179)，其中第 134-149 行专门对比同一输入在 `None_s`（回绕）与 `Sat_s`（钳位）下的差异，可逐条体会。

#### 4.3.4 代码实践

**目标**：对比「只 round」与「round + saturate（即 resize）」的输出，直观看到饱和步骤的必要性。

**操作步骤**：

```python
import sys; sys.path.insert(0, "bittrue/models/python")
from en_cl_fix_pkg import *

a_fmt = FixFormat(1, 3, 1)
r_fmt = FixFormat(1, 3, 0)
a = 7.5

# 仅 round：得到 rounded_fmt=[1,4,0]（整数位 +1，所以 8.0 合法、不溢出）
rf = FixFormat.for_round(a_fmt, r_fmt.F, FixRound.NonSymPos_s)
only_round = cl_fix_round(a, a_fmt, rf, FixRound.NonSymPos_s)
print("round-only fmt :", rf, "value :", only_round)        # [1,4,0], 8.0

# resize：再 saturate 回 [1,3,0]
print("resize  (wrap) :", cl_fix_resize(a, a_fmt, r_fmt, FixRound.NonSymPos_s, FixSaturate.None_s))
print("resize  (sat)  :", cl_fix_resize(a, a_fmt, r_fmt, FixRound.NonSymPos_s, FixSaturate.Sat_s))
```

**需要观察的现象**：

- `round-only` 的中间格式是 `[1,4,0]`，值为 `8.0`——round 自己不溢出，因为 `for_round` 预留了 +1 整数位。
- `resize (wrap)` = `-8.0`、`resize (sat)` = `7.0`——saturate 步骤把 8.0 从「合法的中间值」收拢进目标范围。

**预期结果**：如上。若未装依赖，则「待本地验证」，但可由 4.3.3 的测试断言直接确认。

#### 4.3.5 小练习与答案

**练习 1**：把上面例子的舍入模式改成 `Trunc_s`（截断），`resize (wrap)` 和 `resize (sat)` 会变成什么？

> **答案**：截断不进位，7.5 → 7.0，恰好在目标范围 [-8,7] 内，saturate 不触发。所以两者都是 `7.0`。这也印证 `for_round` 对 `Trunc_s` **不**给整数位 +1（无进位风险）。

**练习 2**：若 `a_fmt` 与 `r_fmt` 的 F 相同（只减少整数位），resize 的 round 步骤还有意义吗？

> **答案**：F 不变时 `for_round` 走 "not being reduced" 分支，`rounded_fmt == a_fmt`，round 是无操作。此时 resize 退化为纯粹的 saturate。但代码路径统一——resize 不需要为「只改 I」单独写一条分支。

---

### 4.4 VHDL 镜像：`cl_fix_resize` 与 `cl_fix_in_range`

#### 4.4.1 概念说明

VHDL 包 `en_cl_fix_pkg.vhd` 提供与 Python 逐字对应的 `cl_fix_resize` 和 `cl_fix_in_range`，保证 RTL（可综合，VHDL-93）与软件参考模型 bit-true。VHDL 侧的 saturate 用「先 `convert`（=回绕）再 `cl_fix_compare`（=钳位）」两步实现（u4-l2 已述），而 `convert` 正是「不改 F、直接截断/符号扩展高位」的无舍入转换器——它是 saturate 内部的回绕引擎，也是理解 VHDL resize 的钥匙。

#### 4.4.2 核心流程

VHDL `cl_fix_resize(a, a_fmt, result_fmt, round, saturate)`：

1. `rounded_fmt_c := cl_fix_round_fmt(a_fmt, result_fmt.F, round)` —— 与 Python 的 `for_round` 同义。
2. `rounded_c := cl_fix_round(a, a_fmt, rounded_fmt_c, round)` —— 先舍入。
3. `return cl_fix_saturate(rounded_c, rounded_fmt_c, result_fmt, saturate)` —— 后饱和。

VHDL `cl_fix_in_range(a, a_fmt, result_fmt, round)` 返回 `boolean`：

1. `rndFmt_c := cl_fix_round_fmt(a_fmt, result_fmt.F, round)`。
2. `Rounded_c := cl_fix_round(to01(a), a_fmt, rndFmt_c, round)` —— 同样先量化（`to01` 把 `'X'/'Z'` 归一成 `'0'/'1'` 以便仿真比较）。
3. 返回 `Rounded_c >= min(result_fmt) and Rounded_c <= max(result_fmt)`（用 `cl_fix_compare`）。

注意默认值差异：VHDL `cl_fix_resize` 默认 `round := Trunc_s`、`saturate := Warn_s`（回绕且告警）；而 Python 默认 `sat = None_s`（回绕、静默）。**跨语言调用时务必显式传 `saturate`，别依赖默认值。**

#### 4.4.3 源码精读

[cl_fix_resize 签名:149-155](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L149-L155) 与 [cl_fix_in_range 签名:157-162](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L157-L162) —— 注意 `cl_fix_in_range` 只接受 `round`、没有 `saturate`（它只判「会不会饱和」，不关心饱和模式）。

VHDL `cl_fix_resize` 函数体几乎是 Python 的逐行镜像：

[en_cl_fix_pkg.vhd:1011-1024](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L1011-L1024) —— `cl_fix_resize` body：先 `cl_fix_round` 到 `rounded_fmt_c`，再 `cl_fix_saturate`。

```vhdl
function cl_fix_resize(...) return std_logic_vector is
    -- Round
    constant rounded_fmt_c : FixFormat_t := cl_fix_round_fmt(a_fmt, result_fmt.F, round);
    constant rounded_c     : std_logic_vector := cl_fix_round(a, a_fmt, rounded_fmt_c, round);
begin
    -- Saturate
    return cl_fix_saturate(rounded_c, rounded_fmt_c, result_fmt, saturate);
end;
```

[en_cl_fix_pkg.vhd:1026-1041](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L1026-L1041) —— `cl_fix_in_range` body：先 `cl_fix_round` 到 `rndFmt_c`，再用 `cl_fix_compare` 与 `cl_fix_min_value/cl_fix_max_value` 比较，返回两端都在范围内的逻辑与。

它依赖的 `cl_fix_saturate` 内部结构（u4-l2 详述）：

[en_cl_fix_pkg.vhd:980-1009](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L980-L1009) —— 先断言 `result_fmt.F = a_fmt.F`；告警模式调用 `cl_fix_in_range` 自检；然后用 `convert` 写入对齐值（=回绕），再在 `Sat_s/SatWarn_s` 时用 `cl_fix_compare` 钳到 `min/max`。

回绕引擎 `convert`：

[en_cl_fix_pkg.vhd:329-351](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L329-L351) —— 注释明确：它**不支持** `rFmt.F < aFmt.F`（要减少小数位须用 `cl_fix_round`），但**支持** `(rS+rI) < (aS+aI)` 即减少整数位——这就是 `cl_fix_saturate` 的 `None_s` 回绕语义。其内部的符号扩展由 [resize_sensible:313-327](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L313-L327) 完成（扩展用 `numeric_std.resize`，截断用直接切片，比保留符号位的默认 resize 更「合理」）。

> 把 `convert`（回绕）+ `cl_fix_compare`（钳位）合起来看，VHDL 的 saturate 与 Python 的 `NarrowFix.saturate` 在每种模式下都经穷举对拍逐位相等（见 u4-l2），因而 VHDL `cl_fix_resize` 与 Python `cl_fix_resize` 也天然 bit-true。

#### 4.4.4 代码实践

**目标**：用 VHDL 重现 4.1.4 的「7.5 进位诱发溢出」案例，确认两语言一致。

**操作步骤**（源码阅读型；若有 GHDL/NVC 也可实际编译）：

1. 打开 `hdl/en_cl_fix_pkg.vhd`，定位 `cl_fix_resize`（1011 行）与 `cl_fix_in_range`（1026 行）。
2. 在一个 VHDL-2008 testbench 里（或脑内推演）写出：

```vhdl
constant A_FMT   : FixFormat_t := (S => 1, I => 3, F => 1);
constant R_FMT   : FixFormat_t := (S => 1, I => 3, F => 0);
-- 7.5 在 [1,3,1] 下是 16#F# 的二进制 0_111_1 (= 15 / 2 = 7.5)
signal a_slv     : std_logic_vector(cl_fix_width(A_FMT)-1 downto 0) := "01111";
signal wrap_res  : std_logic_vector(cl_fix_width(R_FMT)-1 downto 0);
signal sat_res   : std_logic_vector(cl_fix_width(R_FMT)-1 downto 0);
...
wrap_res <= cl_fix_resize(a_slv, A_FMT, R_FMT, NonSymPos_s, None_s);   -- 期望 4 位回绕
sat_res  <= cl_fix_resize(a_slv, A_FMT, R_FMT, NonSymPos_s, Sat_s);    -- 期望 0111 = 7
```

**需要观察的现象**：

- `wrap_res` = `"1000"`（= -8，回绕结果），与 Python 的 `-8.0` 对应。
- `sat_res` = `"0111"`（= 7，钳位结果），与 Python 的 `7.0` 对应。
- 注意 VHDL 默认 `saturate := Warn_s`，所以若不传第 5 个参数，仿真会在回绕时打印 `cl_fix_saturate : Saturation warning!`（见 [980-1009](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L980-L1009) 的 assert）。

**预期结果**：如上。若无仿真器，则「待本地验证」，但结论可由 4.4.3 的源码与 u4-l2 的对拍保证直接推出。

#### 4.4.5 小练习与答案

**练习 1**：VHDL `cl_fix_in_range` 为什么没有 `saturate` 参数？

> **答案**：因为 `in_range` 只回答「舍入后是否落在目标范围内」，即「saturate 步骤**会不会**被触发」。是否真的钳位/回绕由调用者在后续 `cl_fix_resize`/`cl_fix_saturate` 时通过 `saturate` 参数决定，与「是否在范围内」这个判断本身无关。

**练习 2**：把 VHDL `cl_fix_resize` 的 `saturate` 留默认（`Warn_s`），与 Python `cl_fix_resize` 留默认（`None_s`）作用于同一输入，数值会一样吗？行为会一样吗？

> **答案**：数值一样（都是回绕），但**行为不同**：VHDL 会额外在仿真时发出 `Saturation warning!`（assert severity Warning），Python 的 `None_s` 则完全静默。所以「数值 bit-true」不等于「副作用一致」，跨语言迁移时要把告警纳入考量。

---

## 5. 综合实践

把本讲三件事（resize 两步顺序、in_range 预测、跨语言一致性）串起来：

**任务**：设计一个把 8 位有符号源 `[1,3,4]`（范围 [-8, 7.9375]，粒度 1/16）收成 4 位目标 `[1,1,2]`（范围 [-2, 1.75]，粒度 1/4）的转换，要求同时减少整数位和小数位。

1. **预测**：取若干代表值（如 -8.0、-1.9、1.6、7.9375），用 `cl_fix_in_range(..., FixRound.NonSymPos_s)` 判断哪些会饱和。
2. **转换**：用 `cl_fix_resize(..., FixRound.NonSymPos_s, FixSaturate.SatWarn_s)` 实际转换，观察告警何时出现、钳位值是多少。
3. **拆解**：手动打印 `rounded_fmt = for_round(a_fmt, r_fmt.F, NonSymPos_s)`，确认它先舍入到 F=2、I+1 的中间格式，再 saturate 到 `[1,1,2]`。
4. **反思**：把舍入模式换成 `Trunc_s` 重做一次，对比哪些值的「是否会饱和」结论发生了变化，并解释原因（进位 vs 截断）。

**验收**：

- 能指出至少一个「源值合法、但因舍入进位而触发饱和」的例子。
- 能解释为什么不能把 `cl_fix_saturate` 直接作用于原始 `a_fmt → r_fmt`（F 不同，断言失败）。
- 能说出 Python 与 VHDL 在默认饱和模式上的差异。

（若本地无运行环境，至少完成第 3、4 步的源码阅读与手工推演，并标注「待本地验证」。）

---

## 6. 本讲小结

- **`cl_fix_resize = round → saturate`**：先用 `for_round` 算出 F 对齐目标的 `rounded_fmt`，舍入后再饱和，是同时改 F 与 I/S 的唯一统一入口。
- **顺序不能对调**：saturate 要求 F 不变（硬约束）；且舍入会进位、可能把合法值顶出范围，只有「先 round 后 saturate」才能捕获这种进位诱发溢出。
- **`for_round` 给非截断模式整数位 +1**，正是为安全容纳进位值、再交给 saturate 收回而设计。
- **`cl_fix_in_range` 与 resize 同口径**：先按相同舍入模式量化，再比 min/max，因此能准确预测「会不会饱和」——使用时必须传与 resize 相同的 `rnd`。
- **NarrowFix.resize / in_range** 是 float 域的等价实现，结构与主接口逐行对应；有符号回绕在突破 53 位上限时回退整数域保精度。
- **VHDL 镜像 bit-true**：`cl_fix_resize`/`cl_fix_in_range` 与 Python 同构，saturate 内部用 `convert`（回绕）+ `cl_fix_compare`（钳位）；注意 VHDL 默认 `Warn_s`、Python 默认 `None_s`。

---

## 7. 下一步学习建议

- **进入单元 5**：本讲的 `cl_fix_resize` 是所有算术的统一出口。下一讲 [u5-l2 算术运算链路：convert → compute → resize](u5-l2-arithmetic-pipeline.md) 将展示 `cl_fix_add`/`cl_fix_mult` 等如何「算全精度 mid_fmt → resize 到目标」，把 resize 放回完整调用链中理解。
- **进阶 RTL**：若关心硬件实现，可跳到 [u7-l1 可综合 RTL 组件](u7-l1-rtl-components.md)，看 `en_cl_fix_resize.vhd` 实体如何把「round 组合核 + 可选寄存器」串联「saturate 组合核」，并用 `cl_fix_recommended_pipelining` 决定插几级寄存器。
- **建议精读源码**：重读 [en_cl_fix.py 的算术函数](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L313-L342)（每个都以 `return cl_fix_resize(...)` 结尾），体会「resize 是唯一漏斗」这一架构统一性。
