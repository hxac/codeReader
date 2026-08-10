# NarrowFix：float64 内部表示与 53 位上限

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 en_cl_fix 为什么能用一个双精度浮点数（float64）来存放一个定点数，以及这个选择带来的位宽上限 `MAX_WIDTH = 53` 是怎么推导出来的。
- 解释「有符号数为什么额外预留一个整数位」这一关键设计取舍，并理解它让有符号/无符号共用同一个 53 位上限。
- 看懂 `NarrowFix` 如何把 round / saturate / resize 在浮点域里实现，特别是它在「有符号回绕」时为什么会临时退回到整数域（wide）计算。
- 理解运算符重载（`+ - * <<`、比较）的约定与限制，明白为什么跨格式比较会被断言挡住。

本讲只聚焦 **NarrowFix 的表示方式与它的能力边界**。舍入、饱和、resize 的数学理论已经在 u4-l1 / u4-l2 / u4-l3 讲透，本讲不再重新推导，只看 NarrowFix 是「用什么数据、在哪一步可能不够用」。

---

## 2. 前置知识

本讲承接 u2-l2（Python 主接口与 narrow/wide 透明分发）。在进入源码前，先用三段话建立直觉。

**定点值的两种读法。** 一个 `[S, I, F]` 格式的定点数，在内存里本质上是一串比特。这串比特有两种等价的读法（u5-l1 已详述）：

- 非归一化读法：把比特当普通整数看，记作整数 \(n\)。
- 归一化读法：把整数再乘以比例因子 \(2^{-F}\)，得到真实物理值 \(x = n \cdot 2^{-F}\)。

NarrowFix 选择把 **归一化值** \(x\) 直接存进一个 float64。也就是说，`NarrowFix._data` 里放的就是「真实物理值」，而不是原始整数比特。

**float64 能精确装下多少位整数。** IEEE 754 双精度浮点的尾数有 53 位有效精度（下面 4.1 会推导），所以任何一个绝对值不超过 \(2^{53}\) 的整数都能被 float64 一位不差地装下。NarrowFix 的全部能力都建立在这个事实之上。

**narrow 与 wide 的分界线。** en_cl_fix 主接口根据格式宽度自动在两种内部表示间分发（u2-l2）：

```python
def cl_fix_is_wide(fmt : FixFormat) -> bool:
    return cl_fix_width(fmt) > NarrowFix.MAX_WIDTH   # 即 > 53
```

只要 `S+I+F <= 53`，就走 NarrowFix（快）；否则走 WideFix（任意精度，慢）。本讲要回答的核心问题就是：**这个 53 是怎么来的？为什么不是 54？**

---

## 3. 本讲源码地图

本讲几乎全部围绕一个文件：

| 文件 | 作用 |
| --- | --- |
| [bittrue/models/python/en_cl_fix_pkg/narrow_fix.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py) | `NarrowFix` 类全部实现：构造、归一化存储、round/saturate/resize、算术、运算符重载 |

为说明「narrow 内部存的就是归一化 real 值」，还会引用一行主接口：

| 文件 | 作用 |
| --- | --- |
| [bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py) | `cl_fix_is_wide` 分发、`cl_fix_to_real` 对 narrow 直接原样返回 |

---

## 4. 核心概念与源码讲解

### 4.1 float64 内部表示与 MAX_WIDTH = 53 的由来

#### 4.1.1 概念说明

NarrowFix 的核心赌注是：**「一个定点数，只要位宽不太大，就能用一个 float64 精确装下。」** 这句话是否成立，完全取决于 IEEE 754 双精度浮点的精度。

一个 IEEE 754 binary64（即 Python/numpy 的 `float64`）由三部分组成：

- 1 个符号位；
- 11 个指数位（范围足够大，定点数场景下基本不会溢出）；
- 52 个显式尾数位，加上 1 个「隐含的最高位 1」，共 **53 位有效尾数**。

关键结论是：**凡是绝对值不超过 \(2^{53}\) 的整数，float64 都能精确表示**，因为它的尾数有 53 位，而正规化浮点的隐含最高位固定为 1，正好覆盖这个量级以下的所有整数。

用公式写就是：

\[
\text{float64 可精确表示的整数范围} = [-2^{53},\ +2^{53}]
\]

这等价于：**54 位有符号数（53 位幅度 + 1 位符号）和 53 位无符号数，都保证能被精确装下。**

#### 4.1.2 核心流程

那么上限到底该设成 53 还是 54？en_cl_fix 的选择写在源码注释里，推导链是：

1. 理论上，无符号数最多 53 位、有符号数最多 54 位（含符号位）都能精确装下，所以朴素的判据本应是「有符号看 `I+F > 53`、无符号看 `I+F > 53`」——但这样有符号数其实可以再多 1 位，两边判据不一致。
2. 然而有符号数在「关闭饱和、做回绕」时，NarrowFix 需要临时把数值加上 \(2^{I}\) 再取模（见 4.3）。这个中间加法会让结果多占 1 个整数位。
3. 为了让「回绕」这个中间步骤也不丢精度，作者干脆 **给有符号数额外预留 1 个整数位**。
4. 预留之后，有符号和无符号的精确上限就统一了：**都是 53 位**。

于是得到一个干净、一致、对两类格式都成立的判据：

\[
\text{narrow 可用} \iff S + I + F \leq 53
\]

这就是 `MAX_WIDTH = 53` 的完整来历。它不是随手取的整数，而是「float64 精度上限」减去「有符号回绕预留位」之后的保守结果。

#### 4.1.3 源码精读

整段设计意图写在类定义开头的注释里，是最值得逐句读的源码：

注释解释 IEEE 754 double 的位构成，并给出精确整数范围结论（L40-L48）；随后点明「理论上本应是 53，但为了简化有符号回绕，预留一个整数位，得到对有符号/无符号都一致的 53 位上限」（L49-L51）；最后落地为常数（L52）：

[bittrue/models/python/en_cl_fix_pkg/narrow_fix.py:L40-L52](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L40-L52) — 用注释记录「float64 有 53 位尾数 → 整数 \([-2^{53},2^{53}]\) 精确 → 有符号回绕预留 1 位 → 统一上限 53」的完整推导。

构造函数把这条上限变成一条硬断言（L54-L65）：

[bittrue/models/python/en_cl_fix_pkg/narrow_fix.py:L54-L65](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L54-L65) — `__init__` 做两件事：① 断言 `fmt.width <= MAX_WIDTH`，否则提示改用 WideFix；② 断言 `data` 必须是 `float64`，然后把归一化数据存入 `self._data`、把格式复制一份存入 `self._fmt`。

注意 `data` 被统一包成 numpy `float64` 数组（L55-L56），所以 NarrowFix 既支持标量也支持数组运算。

这条 53 位上限同时也是主接口的分发边界：

[bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py:L79-L84](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L79-L84) — `cl_fix_is_wide(fmt)` 直接返回 `cl_fix_width(fmt) > NarrowFix.MAX_WIDTH`，所有 `cl_fix_*` 函数都靠它决定走 NarrowFix 还是 WideFix。

#### 4.1.4 代码实践

**实践目标：** 亲手触发 NarrowFix 的宽度断言，看清 53/54 位这条线画在哪里。

**操作步骤：**

1. 安装依赖（若未装）：`pip install -r requirements.txt`。
2. 在仓库根目录启动 Python，执行下面这段 **示例代码**：

```python
import sys
sys.path.insert(0, "bittrue/models/python")
from en_cl_fix_pkg import FixFormat, NarrowFix
import numpy as np

# 53 位（恰好在上限内）
fmt53 = FixFormat(0, 0, 53)
n53 = NarrowFix(np.array(1.0), fmt53)
print("53 位构造成功:", n53.fmt, "width =", fmt53.width)

# 54 位（越界，应触发断言）
fmt54 = FixFormat(0, 0, 54)
try:
    NarrowFix(np.array(1.0), fmt54)
    print("54 位构造成功 —— 与预期不符")
except AssertionError as e:
    print("54 位被断言挡下:", e)
```

**需要观察的现象：** 53 位格式能正常构造；54 位格式在 `__init__` 里立即抛出 `AssertionError`，提示 `Use WideFix`。

**预期结果：**

```
53 位构造成功: (0, 0, 53) width = 53
54 位被断言挡下: NarrowFix: Requested format is too wide. Use WideFix.
```

**说明：** 把 `fmt54` 换成有符号的 `FixFormat(1, 0, 53)`（也是 54 位）同样会被挡下——这正体现了「有符号/无符号共用 53 位上限」的一致性。若确实需要 54 位，应通过 `cl_fix_*` 主接口（会自动分发到 WideFix），而不是直接构造 NarrowFix。

#### 4.1.5 小练习与答案

**练习 1：** 为什么作者不把上限设成 54（毕竟有符号数理论上有 53 位幅度 + 1 位符号 = 54 位都能精确）？

**参考答案：** 因为关闭饱和时的「有符号回绕」需要临时计算 \(x + 2^{I}\)，这个中间结果会比原数多占 1 个整数位。若上限放到 54，回绕的中间步就可能超出 float64 的 53 位精度而悄悄丢位。预留 1 位后，有符号与无符号共用同一个 53 位上限，既保证精度，也让 `cl_fix_is_wide` 的判据对两类格式完全一致。

**练习 2：** 格式 `(1, 30, 23)`（符号 1 + 整数 30 + 小数 23 = 54 位）能走 NarrowFix 吗？

**参考答案：** 不能。`width = 1+30+23 = 54 > 53`，`cl_fix_is_wide` 返回 `True`，主接口会分发到 WideFix；若直接构造 `NarrowFix` 则触发宽度断言。

---

### 4.2 归一化浮点存储：_data 就是 real 值

#### 4.2.1 概念说明

理解 NarrowFix 的第二条关键认知是：**它存的不是定点数的原始比特整数，而是归一化后的真实物理值。**

也就是说，对于一个 `[0, 2, 1]`（1 位小数）格式下的定点数，如果它代表的物理值是 2.5，那么 `NarrowFix._data` 里存的就是浮点数 `2.5` 本身，而不是原始整数 `5`。

这条认知带来两个直接后果：

- 对 NarrowFix 而言，`cl_fix_to_real`（取归一化值）是 **恒等操作**——值已经存成 real 了，原样返回即可；
- 反过来，`from_integer`（从原始整数构造）必须先把整数除以 \(2^{F}\) 转成归一化值再存。

这也是 NarrowFix 「快」的根本原因：运算直接发生在真实数值上，省去了整数↔浮点之间的来回换算。

#### 4.2.2 核心流程

NarrowFix 的存储与转换流程：

```
外部 real 值 x ──from_real(量化+饱和)──> _data = x（float64，归一化）
                                          │
                                          ├── to_real(): 直接返回 _data（恒等）
                                          ├── to_integer(): round(_data * 2^F)
                                          └── from_integer(n): _data = n / 2^F
```

注意四个入口的语义差异：

- `from_real`：从浮点 real 构造，**固定做半进位（half-up）量化并强制饱和**，不支持回绕（传 `None_s`/`Warn_s` 会抛错）——这是 NarrowFix 特有的限制。
- `to_real`：对 narrow 而言是恒等。
- `from_integer`：把非归一化整数除以 \(2^{F}\) 转成归一化值。
- `to_integer`：把归一化值乘以 \(2^{F}\) 并 round 回整数。

#### 4.2.3 源码精读

先看 `from_real` 如何量化并强制饱和（L67-L96）：

[bittrue/models/python/en_cl_fix_pkg/narrow_fix.py:L67-L96](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L67-L96) — `from_real` 用 `np.floor(a*2^F + 0.5)/2^F` 做 half-up 量化（L86）；饱和分支用 `np.where` 钳到 `[min_value, max_value]`（L89-L91）；**回绕分支直接 `raise NotImplementedError`**（L93-L94），注释明说「为了不在浮点域实现全部舍入模式，from_real 只支持 half-up + 饱和，其余需求请用 resize」。

范围端点由两个静态方法给出（L110-L123）：

[bittrue/models/python/en_cl_fix_pkg/narrow_fix.py:L110-L123](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L110-L123) — `max_value = 2^I - 2^{-F}`；`min_value = -2^I`（有符号）或 `0`（无符号）。这与 u4-l2 推导的端点公式完全一致，只是在浮点域里直接写出来。

而「to_real 对 narrow 是恒等」这一关键事实，要回到主接口里看（L173-L184）：

[bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py:L173-L184](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L173-L184) — 对 narrow 格式，`cl_fix_to_real` 直接 `return a`，根本不做任何换算。这是「_data 就是 real 值」最直接的证据。

反过来，`from_integer` / `to_integer` 必须做除以 / 乘以 \(2^{F}\) 的换算（L99-L108 与 L139-L145）：

[bittrue/models/python/en_cl_fix_pkg/narrow_fix.py:L99-L108](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L99-L108) — `from_integer` 把整数 `a` 除以 `2^F` 得到归一化值，再交给 NarrowFix 存储，并断言结果落在格式范围内。

#### 4.2.4 代码实践

**实践目标：** 用源码证据验证「NarrowFix.\_data 存的是归一化 real 值」。

**操作步骤（示例代码）：**

```python
import sys
sys.path.insert(0, "bittrue/models/python")
from en_cl_fix_pkg import FixFormat, NarrowFix, cl_fix_to_real, cl_fix_from_integer
import numpy as np

fmt = FixFormat(0, 2, 1)   # 1 位小数

# 从原始整数 5 构造：归一化值应为 5 / 2^1 = 2.5
nf = NarrowFix(cl_fix_from_integer(5, fmt), fmt)
print("内部 _data =", nf._data)
print("to_real    =", cl_fix_to_real(nf._data, fmt))
```

**需要观察的现象：** `_data` 直接就是 `2.5`；`cl_fix_to_real` 原样返回 `2.5`，没有任何换算。

**预期结果：**

```
内部 _data = 2.5
to_real    = 2.5
```

#### 4.2.5 小练习与答案

**练习 1：** 为什么 `from_real` 不支持 `None_s`（回绕）模式？

**参考答案：** 因为回绕需要在整数域做模运算，而 NarrowFix 存的是归一化浮点值。注释（L85）明说：为了避免在浮点域重新实现全部舍入模式，from_real 只固定做 half-up 量化 + 饱和。需要回绕或其它舍入模式时，应先用 from_real 量化，再调用 resize（resize 内部的 saturate 才支持回绕，见 4.3）。

**练习 2：** 同一个原始整数 `5`，在 `FixFormat(0,2,1)` 和 `FixFormat(0,2,2)` 下，NarrowFix.\_data 分别是多少？

**参考答案：** 前者 \(5 \cdot 2^{-1} = 2.5\)；后者 \(5 \cdot 2^{-2} = 1.25\)。可见 `_data` 依赖格式的 F，这正说明 `_data` 存的是归一化值而非裸整数。

---

### 4.3 round / saturate / resize 在浮点域的实现（含向 wide 回退）

#### 4.3.1 概念说明

round / saturate / resize 的数学理论已在 u4-l1 / u4-l2 / u4-l3 讲透。本节只关注 **NarrowFix 在浮点域里是怎么实现它们的，以及它独有的一个能力边界：有符号回绕会临时退回到整数域。**

核心思想很朴素：

- **round**：在浮点域里「加偏移再向下取整」。七种舍入模式只是偏移量不同，NarrowFix 用一组 `if/elif` 为每种模式算出偏移，最后统一 `np.floor`（截断小数位）。
- **saturate**：在浮点域里用取模公式实现回绕（无符号 `x mod 2^I`，有符号 `(x+2^I) mod 2^{I+1} - 2^I`）；饱和则用 `np.where` 钳到端点。
- **resize**：固定顺序 = 先 round（对齐 LSB）再 saturate（收拢 MSB）。

但 saturate 的有符号回绕藏着一个陷阱：计算 \((x + 2^{I}) \bmod 2^{I+1}\) 时，中间量 \(x + 2^{I}\) 会比原数多占 1 个整数位。如果原格式本身已经很宽（接近 53 位），这个中间量就会 **顶破 float64 的 53 位精度**。此时 NarrowFix 不能在浮点域硬算，否则会静默丢位。

en_cl_fix 的解决办法是：**检测到中间格式会超宽时，临时把数据转成 Python 任意精度整数（object dtype）算完，再转回浮点。** 这就是 NarrowFix 唯一会「碰」到整数域的地方，也是它通往下一讲 WideFix（u6-l2）的桥梁。

#### 4.3.2 核心流程

NarrowFix.saturate 的有符号回绕决策流程：

```
要做的有符号回绕：(x + 2^I) mod 2^{I+1} - 2^I
        │
        ├── 算出中间加法格式 add_fmt = for_add(原格式, offset_fmt)
        │       （offset_fmt 表示那个 2^I 偏移项）
        │
        ├── add_fmt.width > 53 ?
        │       ├── 是：退回整数域（object dtype）精确取模，再 /2^F 转回浮点
        │       └── 否：直接在 float64 里取模
```

对无符号回绕，没有这个「加 2^I」的步骤，不会变宽，所以始终在浮点域算。

#### 4.3.3 源码精读

先看 round 的浮点实现（L157-L188）。它先把七种模式映射到不同的浮点偏移量，再做一次统一的 `np.floor` 截断：

[bittrue/models/python/en_cl_fix_pkg/narrow_fix.py:L157-L188](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L157-L188) — 先断言结果格式合法（L161）；只有 `r_fmt.F < fmt.F`（真的在减少小数位）时才加偏移（L167）；七种 `FixRound` 各自给出浮点偏移表达式（L168-L183），其中 `2.0**(-r_fmt.F-1)` 就是「半进位点」half，`2.0**-fmt.F` 是输入 LSB（与 u4-l1 的 half/unit 概念一一对应）；最后 `np.floor(data*2^rF)*2^-rF` 截断到目标小数位（L186）。

再看 saturate 里 NarrowFix 独有的「向 wide 回退」逻辑（L190-L244），这是本节重点：

[bittrue/models/python/en_cl_fix_pkg/narrow_fix.py:L207-L232](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L207-L232) — 回绕分支里，对有符号数先构造偏移格式 `offset_fmt`（表示 \(2^{I}\) 这一项，L214-L217），再用 `for_add` 算中间加法格式 `add_fmt`（L218），并判定 `add_fmt.width > MAX_WIDTH`（L219）。若超宽，则 `data.astype(object)` 把浮点数组转成 Python 大整数数组精确取模（L225-L232）；否则直接在 float64 里取模（L235-L238）。无符号数不需要这步，`convert_to_wide` 恒为 `False`（L220-L221）。

这段代码的含义是：**NarrowFix 默认在快路径（float64）上算，只有当有符号回绕的中间量会破坏精度时，才借用一次任意精度整数运算来保住正确性。** 它没有切换到完整的 WideFix 对象，而是就地用 `object` dtype 临时算一下，算完立刻除以 \(2^{F}\) 转回浮点（L232）。

最后，resize 把 round 和 saturate 串起来（L246-L255）：

[bittrue/models/python/en_cl_fix_pkg/narrow_fix.py:L246-L255](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L246-L255) — `resize` 先用 `for_round` 推中间格式 `rounded_fmt` 并 round，再 saturate 到 `r_fmt`。顺序固定为 round → saturate（理由见 u4-l3：saturate 要求 F 不变，且 round 的进位可能诱发溢出，必须先 round 后 saturate 才能捕获）。

#### 4.3.4 代码实践

**实践目标：** 触发一次「有符号回绕退回整数域」的路径，并确认即使格式很宽，回绕结果仍然精确。

**操作步骤（示例代码）：**

```python
import sys
sys.path.insert(0, "bittrue/models/python")
from en_cl_fix_pkg import FixFormat, NarrowFix, FixSaturate
import numpy as np

# 一个接近 53 位上限的有符号格式：1 + 26 + 26 = 53 位
fmt_wide = FixFormat(1, 26, 26)
# 收窄整数位到 25 位（1 + 25 + 26 = 52 位），关闭饱和 => 触发有符号回绕
fmt_narrow = FixFormat(1, 25, 26)

x = NarrowFix(np.array([2.0**25 - 1]), fmt_wide)   # 接近上限的正值
# 回绕中间格式 add_fmt 会比 53 还宽，应走 object dtype 精确路径
y = x.saturate(fmt_narrow, FixSaturate.None_s)
print("回绕结果 _data =", y._data)
print("预期（取模）   =", ((2.0**25 - 1) + 2.0**25) % 2.0**26 - 2.0**25)
```

**需要观察的现象：** 不抛异常；回绕后的 `_data` 与手算取模结果一致。这说明即便中间量超出 float64 精度，NarrowFix 仍给出正确值（因为它临时退到了整数域）。

**预期结果：** 两个打印值相等（具体数值待本地验证，但应当完全相同）。

**说明：** 若想确认确实走了 `convert_to_wide` 分支，可在 narrow_fix.py 的 L223 处临时加一行 `print("convert_to_wide triggered")`（仅用于观察，验证完记得还原，本讲不得修改源码做提交）。能否触发取决于 `add_fmt.width` 是否大于 53，可先用 `FixFormat.for_add(fmt_wide, FixFormat(0, 26, 0)).width` 手算确认。

#### 4.3.5 小练习与答案

**练习 1：** 为什么无符号回绕不需要「退回整数域」的分支？

**参考答案：** 无符号回绕只是 `x mod 2^I`，没有「加 \(2^{I}\)」这一步，中间量不会变宽，因此不会突破 float64 的 53 位精度，直接在浮点域算即可。代码里 `else: convert_to_wide = False`（L220-L221）正对应这一点。

**练习 2：** resize 为什么是「先 round 后 saturate」，而不是反过来？

**参考答案：** 两点约束（详见 u4-l3）：① saturate 硬性要求小数位 F 不变，必须先 round 把 F 对齐好；② round 的进位可能把原本合法的值顶出范围（如 7.5 进位成 8.0 超界），只有先 round 后 saturate 才能捕获这种「进位诱发溢出」。反过来会先丢掉溢出信息。

---

### 4.4 运算符重载与比较限制

#### 4.4.1 概念说明

为了让定点运算写得像普通数学公式，NarrowFix 重载了 Python 的算术与比较运算符。但要理解它的约定与边界。

**算术运算符** `+ - * <<`（以及一元 `-`）各自映射到一个具名方法（`add / sub / mult / shift / neg`）。这些具名方法都遵循 u5-l2 讲过的三段式：自动推导全精度中间格式 `mid_fmt`、在其中无损运算、再 resize。所以 `a + b` 这种写法的 **结果格式是自动推导出来的**（缺省即全精度），这一点和「显式传 r_fmt」完全等价。

**比较运算符** `== != < <= > >=` 则有一条硬性限制：**只能两个 NarrowFix 之间比较**，并且只比 `_data`（归一化值），不看格式。如果你拿 NarrowFix 和一个普通 float 比，或者想跨格式比较，会被断言挡下。代码里的提示是 `Try _data.`——意思是「要比数值，请显式取 `_data` 再比」。

还有一个容易被忽略的副作用：定义了 `__eq__` 的类在 Python 里 **不可哈希**（因为默认 `__hash__` 会被置为 `None`），所以 NarrowFix 对象不能放进 `set`、不能当 `dict` 的键。

#### 4.4.2 核心流程

运算符 → 具名方法的映射，以及比较的限制：

```
算术（结果格式自动推导为 mid_fmt）：
    a + b   ──__add__──>  a.add(b)   ──> for_add ──> 全精度加
    a - b   ──__sub__──>  a.sub(b)
    a * b   ──__mul__──>  a.mult(b)
    -a      ──__neg__──>  a.neg()
    a << n  ──__lshift__> a.shift(n)

比较（只允许 NarrowFix vs NarrowFix，只比 _data）：
    a == b  ──__eq__──>  断言 isinstance(b, NarrowFix)，再 a._data == b._data
    a < b   ──__lt__──>  同上
    ...（!= <= > >= 同构）
```

#### 4.4.3 源码精读

算术运算符只是对具名方法的薄包装（L343-L360）：

[bittrue/models/python/en_cl_fix_pkg/narrow_fix.py:L343-L360](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L343-L360) — `__add__/__sub__/__neg__/__mul__/__lshift__` 各自一行，把运算转给 `add/sub/neg/mult/shift`。而这些具名方法（如 L279-L288 的 `add`）内部都会先算 `mid_fmt = FixFormat.for_add(...)`，在 `mid_fmt` 里做无损运算，再 resize。所以 `a + b` 的结果格式是 `for_add` 推导出的全精度格式。

比较运算符则有显式的类型断言（L362-L390）：

[bittrue/models/python/en_cl_fix_pkg/narrow_fix.py:L362-L390](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L362-L390) — 六个比较运算符全部以 `assert isinstance(other, NarrowFix)` 开头，提示信息统一为 `Try _data.`；断言通过后只比较 `self._data` 与 `other._data`。这意味着：跨格式、或与裸 float 比较都会失败；要比纯数值，应写 `a._data < b._data`。

`__repr__` / `__str__` 把格式和数据一起打印，方便调试（L397-L407）：

[bittrue/models/python/en_cl_fix_pkg/narrow_fix.py:L397-L407](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/narrow_fix.py#L397-L407) — 输出形如 `NarrowFix: (S, I, F)` 换行后跟 numpy 数组。

> **读源码的小提示（待本地验证）：** L393-L395 的 `__len__` 里有一行 `assert isinstance(other, NarrowFix)`，但 `__len__` 的参数列表里并没有 `other` 这个名字。这看起来是从比较运算符复制过来、忘了删掉的遗留代码——调用 `len(nf)` 时大概率会抛 `NameError` 而非工作正常。本讲不修改源码，仅作为「读源码要带着批判眼光」的一个真实例子提请你留意，行为请以本地运行为准。

#### 4.4.4 代码实践

**实践目标：** 体验运算符重载的便利，并亲手撞上比较限制与不可哈希副作用。

**操作步骤（示例代码）：**

```python
import sys
sys.path.insert(0, "bittrue/models/python")
from en_cl_fix_pkg import FixFormat, NarrowFix, cl_fix_from_real
import numpy as np

fa = FixFormat(1, 4, 8)
fb = FixFormat(1, 4, 8)
a = NarrowFix(cl_fix_from_real(np.array([1.5]), fa), fa)
b = NarrowFix(cl_fix_from_real(np.array([2.25]), fb), fb)

# 1) 运算符重载：a+b 等价于 a.add(b)，结果格式自动推导
c = a + b
print("a + b =", c._data, " fmt =", c.fmt)

# 2) 比较限制：和普通 float 比较应被断言挡下
try:
    print(a == 1.5)
except AssertionError as e:
    print("与 float 比较被挡下:", e)

# 3) 不可哈希副作用
try:
    {a}   # 放进 set
    print("可哈希 —— 与预期不符")
except TypeError as e:
    print("不可哈希:", e)
```

**需要观察的现象：**

1. `a + b` 能直接算出 `3.75`，且 `c.fmt` 是 `for_add` 推导出的全精度格式（整数位可能比输入多 1）。
2. `a == 1.5` 抛 `AssertionError`，提示 `Try _data.`。
3. 把 NarrowFix 放进 `set` 抛 `TypeError: unhashable type`。

**预期结果：**

```
a + b = [3.75]  fmt = FixFormat(1, 5, 8)
与 float 比较被挡下: NarrowFix can only be compared with NarrowFix. Try _data.
不可哈希: unhashable type: 'NarrowFix'
```

（`c.fmt` 的整数位是否 +1 取决于 `for_add` 对 `[1,4,8]+[1,4,8]` 的推导，可自行用 `FixFormat.for_add(fa, fb)` 确认；具体待本地验证。）

#### 4.4.5 小练习与答案

**练习 1：** 为什么 `a == 1.5` 会被断言挡下，而不是静默返回 `False`？

**参考答案：** NarrowFix 的 `__eq__` 显式断言 `isinstance(other, NarrowFix)`。设计意图是：跨类型比较很可能是用户写错了（把定点数当裸 float 比），静默返回 `False` 会掩盖这种 bug，所以宁可报错，并提示用 `_data` 取出裸数值再比。

**练习 2：** 既然定义了 `__eq__`，NarrowFix 还能当 dict 的键吗？为什么？

**参考答案：** 不能。Python 规定：若类定义了 `__eq__` 却未定义 `__hash__`，则 `__hash__` 被设为 `None`，实例变为不可哈希。NarrowFix 正是这种情况，所以它不能放进 `set`、不能当 `dict` 键。

---

## 5. 综合实践

把本讲四条主线串起来，做一个「NarrowFix 能力边界探测」小任务。

**任务：** 写一段脚本，构造一组从 50 位到 55 位、有符号与无符号各一份的格式，逐个回答下列问题，并把结论整理成一张表：

1. 哪些格式能直接构造 NarrowFix、哪些会被宽度断言挡下？（对应 4.1）
2. 对能构造的格式，用 `from_real` 灌入一个接近上限的值，确认 `_data` 等于归一化后的 real 值。（对应 4.2）
3. 选一个接近 53 位的有符号格式，做一次「收窄整数位 + 关闭饱和」的 resize，验证有符号回绕结果与手算取模一致，并判断它是否触发了「退回整数域」路径。（对应 4.3）
4. 用运算符 `+` 把两个 NarrowFix 相加，打印结果格式，确认它等于 `FixFormat.for_add` 的推导值；再尝试把结果放进 `set`，确认会触发不可哈希错误。（对应 4.4）

**预期产出：** 一张表，列出每个格式在第 1~4 步的行为；一段对「53 位上限」「归一化存储」「有符号回绕退回 wide」「运算符结果格式自动推导」四个现象的解释。

> 提示：第 3 步要判断是否走 `convert_to_wide` 分支，可先用 `FixFormat.for_add(原格式, FixFormat(0, 原格式.I+1, 0)).width` 算出中间加法宽度，再与 53 比较。结论待本地验证。

---

## 6. 本讲小结

- NarrowFix 用一个 float64 存放定点数的 **归一化值**（real 值），而不是原始整数比特；所以 `cl_fix_to_real` 对 narrow 是恒等，`from_integer` 需要除以 \(2^{F}\)。
- `MAX_WIDTH = 53` 来自 float64 的 53 位尾数精度：整数 \([-2^{53},2^{53}]\) 可精确表示；为简化有符号回绕再预留 1 位，得到有符号/无符号一致的 53 位上限。
- 主接口靠 `cl_fix_is_wide(fmt) = width > 53` 在 NarrowFix（快）与 WideFix（任意精度）间分发。
- round 在浮点域用「加偏移再 `np.floor`」实现七种模式；saturate 用取模公式实现回绕、用 `np.where` 实现钳位；resize 固定为 round→saturate。
- NarrowFix 的唯一能力裂缝是「有符号回绕」：中间量 \(x+2^{I}\) 可能顶破 53 位精度，此时它临时退回 Python 任意精度整数（object dtype）算完再转回浮点——这是通往下一讲 WideFix 的桥梁。
- 运算符 `+ - * << -` 映射到具名方法、结果格式自动推导为全精度；比较运算符只允许 NarrowFix 之间、只比 `_data`；定义 `__eq__` 使其不可哈希。

---

## 7. 下一步学习建议

- **下一讲 u6-l2（WideFix：任意精度整数表示）**：本讲反复提到「超出 53 位就交给 WideFix」。下一讲将完整打开 WideFix 的整数内部表示，看它如何用 Python 任意精度整数摆脱 53 位限制，以及它的 round/saturate/resize 如何在整数域实现。
- **u6-l3（cl_fix_is_wide 分发与 Narrow↔Wide 互转）**：本讲只看了 NarrowFix 内部，下一讲之后建议读 u6-l3，理解 `cl_fix_*` 主接口如何根据格式宽度在 narrow/wide 间透明分发、以及 `WideFix.from_narrowfix` 的无损转换。
- **源码延伸阅读**：对照 [en_cl_fix.py 的 cl_fix_round/cl_fix_saturate](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L190-L237)，看主接口如何在 `a_wide`/`r_wide` 判定后决定用 NarrowFix 还是 WideFix，把本讲的「分发边界」放进完整调用链中理解。
