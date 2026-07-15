# round_fmt 与 cl_fix_in_range

## 1. 本讲目标

本讲是 U3「结果格式预测」的收尾篇。u3-l1 讲了加减法结果格式，u3-l2 讲了乘法/取反/绝对值/移位的结果格式——它们回答的都是「**运算**之后结果有多大」。本讲要回答两个相关但不同的新问题：

1. **舍入本身会不会改变整数位？** 当我们把一个值从多位小数舍入到少位小数时，除了丢掉低位，还可能因为「进位」而让整数部分多出 1 位。`cl_fix_round_fmt` / `FixFormat.for_round` 就是用来在综合期提前算出这个「舍入后」的结果格式。
2. **某个值舍入后会不会越界（需要饱和）？** `cl_fix_in_range` 用来在真正做 `resize` 之前，预判一个值是否会在饱和阶段被钳位——而它的关键巧思在于：**必须先按相同的舍入模式做一次舍入，再去比范围**。

学完后你应当能够：

- 解释为什么**非 Trunc 舍入模式**（如 `NonSymPos_s`）可能让结果整数位 +1，而 `Trunc_s` 永远不会。
- 用 `FixFormat.for_round` / `cl_fix_round_fmt` 预测任意格式在任意舍入模式下的结果格式，并指出它何时与输入格式相同、何时多 1 个整数位。
- 说明 `cl_fix_in_range` 为什么不能直接拿原始值去比目标格式的范围，而必须「先舍入、后比范围」。
- 理解 `cl_fix_round` 用 `assert` 强制 `result_fmt == cl_fix_round_fmt(...)` 的「格式契约」机制，以及 `cl_fix_resize`、`cl_fix_saturate`、`cl_fix_recommended_pipelining` 如何围绕它协作。

本讲只讲**纯函数**（综合期可算、不含数据位运算实现）。`cl_fix_round` 内部如何加偏移、如何截断，在 u2-l2 已经讲过；`NarrowFix` / `WideFix` 的数值实现细节留待 U4。

## 2. 前置知识

阅读本讲前，请确认你已经掌握以下概念：

- **定点格式 `[S,I,F]` 与可表示范围**（u2-l1、u2-l4）：总位宽 \(W=S+I+F\)；最大值 \(v_{\max}=2^{I}-2^{-F}\)；最小值有符号为 \(-2^{I}\)、无符号为 \(0\)。本讲会反复用 `cl_fix_max_value` / `cl_fix_min_value`。
- **舍入机制 round(x)=trunc(x+offset)**（u2-l2）：除 `Trunc_s` 外的所有舍入模式，本质上都是「先加一个约等于半个 LSB 的偏移，再截断低位」。七种模式只在「平局（tie）」处理上有差别。引入过 `half_c`（平局位常量）、`sign_c`（符号位）等术语。
- **保守最坏情况原则**（u3-l1）：格式预测函数假设输入「可取任意值」，给出**充分且最小**（既装得下、又不浪费）的结果格式。本讲的 `for_round` 同样遵循这一原则。
- **resize = 先 round 后 saturate，不可交换**（u4-l1、u2-l3）：饱和阶段要求小数位 `F` 不变，所以必须先 round 把小数位对齐，再 saturate。这一条是理解 `cl_fix_in_range`「先舍入」步骤的根本原因。

一个贯穿全讲的关键直觉：**舍入不只是「丢低位」，它还可能「向高位进位」**。正是这个进位，让整数位有 +1 的可能，也让 `in_range` 必须先舍入再判界。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [en_cl_fix_types.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py) | Python 参考实现。`FixFormat.for_round` 静态方法在此（与 VHDL `cl_fix_round_fmt` 镜像）。`FixRound` 枚举定义七种舍入模式。 |
| [en_cl_fix.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py) | Python 主接口。`cl_fix_round_fmt = FixFormat.for_round` 的别名、`cl_fix_in_range`、`cl_fix_resize`、`cl_fix_round`（含格式契约 assert）都在此。 |
| [en_cl_fix_pkg.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd) | VHDL 金标准。`cl_fix_round_fmt`、`cl_fix_in_range`、`cl_fix_round`、`cl_fix_resize`、`cl_fix_saturate`、`cl_fix_recommended_pipelining` 的包体实现，以及包头的公共 API 声明。 |

## 4. 核心概念与源码讲解

### 4.1 round_fmt：预测舍入后的结果格式

#### 4.1.1 概念说明

先复习 u2-l2 的一个核心结论：**舍入只在「减小小数位」时发生**——当结果 `F` 小于输入 `F` 时，我们要丢掉若干低位；当结果 `F` 大于或等于输入 `F` 时，只是在低位补零，没有任何「舍入」动作。

那么舍入会不会影响**整数位** `I`？直觉上「丢小数位」似乎和整数位无关，但仔细想：非 Trunc 舍入要先加一个「约半个结果 LSB」的偏移。如果一个值的小数部分非常接近「下一个整数」（即二进制下 `0.1111...`，几乎等于 `1.0`），那么加完偏移后就会**向整数位产生一次进位（carry）**，这一次进位可能让整数部分多出 1 位。

举一个具体例子帮助建立直觉。考虑无符号格式 `[0,4,4]`，它的最大值是

\[
v_{\max}=2^{4}-2^{-4}=16-0.0625=15.9375.
\]

现在要把它舍入到 0 位小数（整数）：

- **`Trunc_s`（截断）**：直接丢掉小数位，\(15.9375 \to 15\)。15 用无符号表示只需 4 个整数位（\(2^{4}-1=15\) 刚好够），所以结果格式是 `[0,4,0]`，整数位**没有增长**。
- **`NonSymPos_s`（四舍五入、半值向上）**：\(15.9375 \to 16\)。而 16 用无符号表示需要 **5** 个整数位（\(2^{4}-1=15\) 装不下 16，必须 \(2^{5}-1=31\)），所以结果格式是 `[0,5,0]`，整数位**+1**。

这就是 `for_round` 要解决的核心问题：**预测「舍入之后」结果到底需要几个整数位**，从而在综合期就能把信号位宽定准。

注意这是「保守最坏情况」：并非每个值都会进位到 16，只有接近顶端的值（\(\geq 15.5\)）才会。但**格式必须能装下最坏情况**，所以只要用了非 Trunc 模式且确实在减小小数位，就**无条件**给整数位 +1。`Trunc_s` 永远只丢位、不加偏移，所以绝不可能进位，整数位永远不变。

> 与 u3-l1/u3-l2 的区别：`for_add`/`for_mult` 等预测的是「**运算**带来的位增长」；`for_round` 预测的是「**舍入**带来的位增长」。两者相互独立，可以叠加（一个数据通路可能先乘法增长、再舍入增长）。

#### 4.1.2 核心流程

`for_round` 接收三个参数：输入格式 `a_fmt`、目标小数位数 `rFracBits`、舍入模式 `rnd`。它先用一个三分支决策定出整数位 `I`，再用一个「保宽守卫」确保结果至少 1 位宽，最后返回 `(a_fmt.S, I, rFracBits)`。

```text
function for_round(a_fmt, rFracBits, rnd) -> FixFormat:

  # —— 第一步：定整数位 I ——
  if rFracBits >= a_fmt.F:
      # 没有减小小数位 → 不发生舍入 → 整数位不变
      I = a_fmt.I
  elif rnd == Trunc_s:
      # 截断只丢低位、不加偏移 → 绝不进位 → 整数位不变
      I = a_fmt.I
  else:
      # 非截断模式且确实减小了小数位 → 可能进位 → 整数位 +1（保守）
      I = a_fmt.I + 1

  # —— 第二步：保宽守卫（结果至少 1 位宽）——
  if a_fmt.S + I + rFracBits < 1:
      I = -a_fmt.S - rFracBits + 1     # 使 S+I+rFracBits 恰好等于 1

  return FixFormat(a_fmt.S, I, rFracBits)
```

三个要点：

1. **符号位 `S` 永远不变**。舍入只动小数位与（可能的）整数位，不会改变一个数的有符号性。
2. **小数位直接取 `rFracBits`**。这是「目标」小数位数，由调用者指定。
3. **+1 是充分且必要的**。充分：任何非 Trunc 模式最多进位 1 个整数位（因为偏移不超过 1 个结果 LSB，最多把 `…111` 翻成 `…000` 并向上进 1）；必要：确实存在会进位的值（如上例 15.9375）。「保宽守卫」则处理一些边角格式（例如把很宽的小数舍到负小数位时，避免出现 0 位宽的退化格式）。

#### 4.1.3 源码精读

**Python 参考实现**——`FixFormat.for_round`：

[en_cl_fix_types.py:L318-L342](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L318-L342) —— `for_round` 的完整实现。三个 `if/elif/else` 分支分别对应「不减小数位」「Trunc」「其他模式」，注释里直接写明「All other rounding modes can overflow into +1 int bit」。

```python
if rFracBits >= a_fmt.F:
    # If fractional bits are not being reduced, then nothing happens to int bits.
    I = a_fmt.I
elif rnd == FixRound.Trunc_s:
    # Crude truncation has no effect on int bits.
    I = a_fmt.I
else:
    # All other rounding modes can overflow into +1 int bit.
    I = a_fmt.I + 1

# Force result to be at least 1 bit wide
if a_fmt.S + I + rFracBits < 1:
    I = -a_fmt.S - rFracBits + 1

return FixFormat(a_fmt.S, I, rFracBits)
```

**VHDL 金标准**——`cl_fix_round_fmt`：

[en_cl_fix_pkg.vhd:L608-L628](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L608-L628) —— 与 Python 逐字镜像的三个分支和保宽守卫。注意 VHDL 用局部变量 `I_v`，最后返回 `(a_fmt.S, I_v, r_frac_bits)`。

```vhdl
if r_frac_bits >= a_fmt.F then
    -- If fractional bits are not being reduced, then nothing happens to int bits.
    I_v := a_fmt.I;
elsif rnd = Trunc_s then
    -- Crude truncation has no effect on int bits.
    I_v := a_fmt.I;
else
    -- All other rounding modes can overflow into +1 int bit.
    I_v := a_fmt.I + 1;
end if;

-- Force result to be at least 1 bit wide
if a_fmt.S + I_v + r_frac_bits < 1 then
    I_v := -a_fmt.S - r_frac_bits + 1;
end if;

return (a_fmt.S, I_v, r_frac_bits);
```

两份代码连注释都几乎一字不差——这正是 u1-l2 所说的「VHDL 金标准 + Python 镜像参考模型」架构。`for_round` 的 +1 整数位，也正好对应 `cl_fix_round` 实现里 `mid_fmt` 预留的进位空间：见 [en_cl_fix_pkg.vhd:L920-L929](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L920-L929)，其中 `mid_fmt_c` 的整数位直接取自 `result_fmt.I`（而 `result_fmt` 必须是 `cl_fix_round_fmt` 的产物，已经含 +1），`half_c` 偏移加完后产生的进位就落入这个多出来的整数位里，不会丢失。

> 公共 API 入口：在主接口里 `cl_fix_round_fmt = FixFormat.for_round`，见 [en_cl_fix.py:L69](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L69)，是个零开销别名。VHDL 端的包头声明见 [en_cl_fix_pkg.vhd:L99](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L99)。

#### 4.1.4 代码实践

**实践目标**：亲手验证「非 Trunc 模式会让整数位 +1，而 Trunc 不会」，并用最大值进位解释原因。

**操作步骤**：

1. 安装依赖（参见 u1-l3）：`python -m pip install -r requirements.txt`。
2. 在仓库根目录启动 Python，把 Python 模型加入路径：

   ```python
   # 示例代码
   import sys
   sys.path.append("bittrue/models/python")
   from en_cl_fix_pkg import *

   a_fmt  = FixFormat(0, 4, 4)          # 无符号，范围 0 .. 15.9375
   rFrac  = 0                           # 舍入到整数

   for rnd in [FixRound.Trunc_s, FixRound.NonSymPos_s, FixRound.NonSymNeg_s,
               FixRound.SymInf_s, FixRound.ConvEven_s]:
       rf = FixFormat.for_round(a_fmt, rFrac, rnd)
       print(f"{rnd.name:12s} -> {rf}  (I={rf.I}, width={rf.width})")
   ```

3. 同时打印出 `a_fmt` 的最大值，对照思考：

   ```python
   # 示例代码
   print("a_fmt max =", cl_fix_max_value(a_fmt))   # 15.9375
   ```

**需要观察的现象**：

- `Trunc_s` 的结果应为 `[0,4,0]`（整数位 4，width 4）。
- 其余五种模式的结果都应为 `[0,5,0]`（整数位 5，width 5）——即整数位 +1。

**预期结果与解释**：`a_fmt` 的最大值是 15.9375。截断得 15，用 4 个无符号整数位即可（\(2^4-1=15\)）；而非 Trunc 模式会把它进位到 16，需要 5 个整数位（\(2^5-1=31\) 才能装下 16）。这正是 `for_round` 在非 Trunc 分支无条件 `+1` 的现实依据。若你得到不同的整数位，请检查 `a_fmt.F` 是否确为 4、`rFracBits` 是否确为 0。

> 提示：本实践未替你运行命令；上面是「预期结果」。请在本地执行确认（若环境缺仿真器也无妨，本实践只依赖 numpy，不依赖 VHDL 仿真器）。

#### 4.1.5 小练习与答案

**练习 1**：输入格式 `[1,3,4]`（有符号），舍入到 2 位小数。请分别给出 `Trunc_s` 与 `NonSymPos_s` 的 `for_round` 结果。

**参考答案**：结果 `F=2 < a.F=4`，发生舍入。`Trunc_s` → `[1,3,2]`；`NonSymPos_s` → `[1,4,2]`（整数位 +1）。符号位不变。

**练习 2**：把 `[1,3,4]` 舍入到 **6** 位小数（即 `rFracBits=6`），`for_round` 结果是什么？为什么整数位不变？

**参考答案**：`rFracBits(6) >= a.F(4)`，进入第一分支「没有减小小数位」——此时只是低位补零，根本不发生舍入，自然没有进位，结果为 `[1,3,6]`。这印证了「舍入只在减小小数位时发生」。

---

### 4.2 cl_fix_in_range：先舍入，再判范围

#### 4.2.1 概念说明

`cl_fix_in_range` 回答的问题是：**给定一个处于 `a_fmt` 中的值，如果用 `cl_fix_resize` 把它搬到 `result_fmt`（含指定的舍入模式），它会不会触发饱和？** 返回布尔值（或布尔数组）：`True` 表示「在范围内，不会被钳位」，`False` 表示「会越界、会被饱和」。

这里有一个容易踩的坑：**不能直接拿原始值去和 `result_fmt` 的范围比**。原因有二，都来自 u4-l1 确立的「resize = 先 round 后 saturate」管线：

1. **舍入会改变数值**。一个原本在范围内的值，可能因为「向上进位」而超出 `result_fmt` 的最大值；一个原本略微越界的值，也可能因为「向零截断」而落回范围内。所以饱和阶段实际看到的，是**舍入之后**的值，而不是原始值。
2. **舍入可能让整数位增长**（4.1 节）。`for_round` 在非 Trunc 模式下给了 +1 整数位，这意味着舍入后的中间格式 `rounded_fmt` 可能比 `result_fmt` 更宽——比较时必须在 `rounded_fmt` 的域里进行，否则会低估能表示的范围。

因此 `cl_fix_in_range` 的做法是：**完整复现 resize 管线的「round 阶段」，然后用舍入后的值去比 `result_fmt` 的 `min_value`/`max_value`**。它本质上是 resize 的「只读探针」——只判断、不修改。

#### 4.2.2 核心流程

```text
function cl_fix_in_range(a, a_fmt, result_fmt, rnd) -> bool:

  # 第一步：算出「舍入后」的中间格式（非 Trunc 可能比 result_fmt 多 1 个整数位）
  rounded_fmt = cl_fix_round_fmt(a_fmt, result_fmt.F, rnd)

  # 第二步：把数据真正舍入到 rounded_fmt（这一步把进位算出来）
  rounded = cl_fix_round(a, a_fmt, rounded_fmt, rnd)

  # 第三步：用「舍入后的值」与「目标格式的范围」比较
  lo = rounded >= cl_fix_min_value(result_fmt)
  hi = rounded <= cl_fix_max_value(result_fmt)
  return lo AND hi
```

三个步骤一一对应 resize 的 round 阶段，然后加一次双侧边界比较。注意：

- 比较的**下界/上界用的是 `result_fmt` 的 min/max**（即饱和后能装下的范围），而**被比较的值 `rounded` 处在更宽的 `rounded_fmt` 域里**。这种「跨格式比较」在 VHDL 端由 `cl_fix_compare` 处理，在 Python 端因为数据是归一化浮点/整数，直接用 `>=`/`<=` 即可。
- 因为先做了舍入，**同一个值在不同 `rnd` 下可能得到不同的 in_range 结果**——这正是本节代码实践要演示的重点。

#### 4.2.3 源码精读

**Python 参考实现**——`cl_fix_in_range`：

[en_cl_fix.py:L114-L124](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L114-L124) —— 完整实现。`np.where` 用于支持数组化输入（对每个元素求布尔，再逻辑与）。

```python
def cl_fix_in_range(a, a_fmt, r_fmt, rnd=FixRound.Trunc_s):
    rounded_fmt = FixFormat.for_round(a_fmt, r_fmt.F, rnd)
    rounded = cl_fix_round(a, a_fmt, rounded_fmt, rnd)
    lo = np.where(rounded < cl_fix_min_value(r_fmt), False, True)
    hi = np.where(rounded > cl_fix_max_value(r_fmt), False, True)
    return np.where(np.logical_and(lo, hi), True, False)
```

**VHDL 金标准**——`cl_fix_in_range`：

[en_cl_fix_pkg.vhd:L1024-L1039](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1024-L1039) —— 与 Python 镜像，但有两处 VHDL 特有的处理：

```vhdl
function cl_fix_in_range(a, a_fmt, result_fmt, round := Trunc_s) return boolean is
    -- Note: If result_fmt.F /= a_fmt.F, then we need to know what rounding
    --       algorithm will be used when reducing the LSBs.
    constant rndFmt_c : FixFormat_t := cl_fix_round_fmt(a_fmt, result_fmt.F, round);
    constant Rounded_c : std_logic_vector := cl_fix_round(to01(a), a_fmt, rndFmt_c, round);
begin
    return cl_fix_compare(">=", Rounded_c, rndFmt_c, cl_fix_min_value(result_fmt), result_fmt) and
           cl_fix_compare("<=", Rounded_c, rndFmt_c, cl_fix_max_value(result_fmt), result_fmt);
end;
```

两处细节值得注意：

1. **`to01(a)`**：把含 `'U'/'X'/'Z'` 等 9 态逻辑值的输入归一化成 `'0'/'1'`，避免仿真期比较出现不可预期的结果。Python 端无此问题。
2. **`cl_fix_compare(">=", Rounded_c, rndFmt_c, cl_fix_min_value(result_fmt), result_fmt)`**：跨格式比较——左操作数在 `rndFmt_c`（更宽）里，右操作数（边界值）在 `result_fmt`（更窄）里。`cl_fix_compare` 内部会先把两者对齐到同一二进制小数点再做比较。

包头公共声明见 [en_cl_fix_pkg.vhd:L157-L162](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L157-L162)。

#### 4.2.4 代码实践

**实践目标**：构造一个「恰好处在饱和边界、且是平局（tie）」的值，证明同一个值在不同舍入模式下，`cl_fix_in_range` 的结果会不同。

**操作步骤**：

```python
# 示例代码
import sys
sys.path.append("bittrue/models/python")
from en_cl_fix_pkg import *

a_fmt = FixFormat(1, 3, 4)       # 有符号，范围 -8 .. 7.9375，LSB = 0.0625
r_fmt = FixFormat(1, 3, 1)       # 有符号，范围 -8 .. 7.5，  LSB = 0.5

# r_fmt 的最大值恰为 7.5；选一个恰为「平局」的值 7.75（= 124/16，在 a_fmt 中精确可表示）
v = 7.75
a = cl_fix_from_real(v, a_fmt)   # 把 7.75 编码进 a_fmt（narrow 下返回归一化浮点 7.75）

print("r_fmt max =", cl_fix_max_value(r_fmt))         # 7.5
for rnd in [FixRound.Trunc_s, FixRound.NonSymPos_s, FixRound.NonSymNeg_s]:
    print(f"{rnd.name:12s} in_range =",
          bool(cl_fix_in_range(a, a_fmt, r_fmt, rnd)))
```

**需要观察的现象与预期结果**：

- `r_fmt` 的最大值是 \(2^{3}-2^{-1}=7.5\)；而 `v=7.75` 恰好处在 7.5 与 8.0 的正中间，是一个**平局**。
- `Trunc_s`：向 \(-\infty\) 截断，\(7.75 \to 7.5\)，落在范围内 → **`True`**。
- `NonSymPos_s`（半值向上）：平局向上，\(7.75 \to 8.0\)，超过 7.5 → **`False`**（会饱和）。
- `NonSymNeg_s`（半值向下）：平局向下，\(7.75 \to 7.5\)，等于上界 → **`True`**。

**为什么会这样**：`in_range` 内部先调用 `cl_fix_round_fmt([1,3,4], 1, rnd)` 得到 `rounded_fmt`。对非 Trunc 模式它是 `[1,4,1]`（整数位 +1，能装下 8.0），对 Trunc 是 `[1,3,1]`。然后真正把 7.75 舍入进去——不同模式产生 7.5 或 8.0，再与 `r_fmt` 的上界 7.5 比较即得分歧结论。这恰好说明：**判断「是否越界」必须带上舍入模式，否则会错判**。

> 提示：上面是依据源码逻辑推出的「预期结果」。请在本地运行确认。若 `cl_fix_from_real` 因默认半向上舍入把 7.75 写成别的值，请确认 `v` 在 `a_fmt` 中精确可表示（\(7.75/0.0625=124\) 为整数，可表示）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `cl_fix_in_range` 的第三步比较，下界/上界用 `result_fmt` 的 min/max，而被比较的 `rounded` 却在更宽的 `rounded_fmt` 域里？如果反过来——把 `rounded` 强制塞进 `result_fmt` 再比——会出什么问题？

**参考答案**：因为饱和阶段判定「越界」用的就是 `result_fmt` 的范围，而饱和看到的值是**舍入后**的值；舍入后的值可能需要更宽的 `rounded_fmt` 才能精确表示（例如 8.0 装不进 `[1,3,1]`，但装得进 `[1,4,1]`）。若反过来先把 `rounded` 塞进 `result_fmt`，那个 8.0 会被截断/回绕成范围内的假值，从而**漏报越界**——把本该饱和的值误判成在范围内。

**练习 2**：如果把 `cl_fix_in_range` 里的 `cl_fix_round` 那一步去掉、直接用原始 `a` 去比 `result_fmt` 的范围，对 `v=7.75` 的 `NonSymPos_s` 会得到什么结果？这说明了什么？

**参考答案**：原始值 7.75 本身 \(>7.5\)，直接比会判为「越界」（False）。但若某个值是 7.6（在范围内），用 `NonSymPos_s` 舍入到 8.0 反而越界——直接比原始值会**漏判**这种「舍入后才越界」的情况。所以去掉舍入步骤既会误报也会漏报，必须先舍入再判界。

---

### 4.3 契约与联动：round_fmt 是 round 阶段的格式契约

#### 4.3.1 概念说明

`cl_fix_round_fmt` 不只是一个预测工具——它还被库**强制**用作 `cl_fix_round` 的「结果格式契约」。也就是说：你传给 `cl_fix_round` 的 `result_fmt`，**必须**等于 `cl_fix_round_fmt(a_fmt, result_fmt.F, rnd)`，否则会在仿真期直接 `assert` 报错。这条契约把「别忘了给非 Trunc 舍入预留 +1 整数位」从一条口头经验，升级成了**运行时硬约束**，避免设计师手算位宽时漏掉进位位。

围绕这条契约，几个核心函数形成清晰的联动：

| 函数 | 如何使用 `round_fmt` / `in_range` |
| --- | --- |
| `cl_fix_round` | 入口处 `assert result_fmt == cl_fix_round_fmt(...)`，强制契约。 |
| `cl_fix_resize` | 内部先 `rounded_fmt = cl_fix_round_fmt(...)`，再 round、再 saturate——**自动**处理 +1，调用者无需关心。 |
| `cl_fix_saturate` | 当 `result_fmt.F != a_fmt.F` 时（即需要先舍入），用 `cl_fix_in_range` 判断是否触发饱和告警。 |
| `cl_fix_recommended_pipelining` | 同样 `assert` 契约，并据此决定 round 是否需要插入寄存器（Trunc 无逻辑→0 拍）。 |
| `cl_fix_in_range` | 用 `round_fmt` 推导 `rounded_fmt`，是 resize 的「只读探针」。 |

关键结论：**只要你用 `cl_fix_resize`，就永远不需要手动算 `round_fmt`**——它内部已经帮你算好并把 +1 整数位考虑进去。只有在「绕过 resize、直接调用 `cl_fix_round` 或手搭数据通路」时，你才必须自己调 `cl_fix_round_fmt` 来定结果格式，否则会被 assert 拦下。

#### 4.3.2 源码精读（联动点）

**`cl_fix_round` 的契约 assert**——Python 端：

[en_cl_fix.py:L190-L194](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L190-L194)

```python
def cl_fix_round(a, a_fmt, r_fmt, rnd):
    assert r_fmt == cl_fix_round_fmt(a_fmt, r_fmt.F, rnd), \
        "cl_fix_round: Invalid result format. Use cl_fix_round_fmt()."
```

VHDL 端见 [en_cl_fix_pkg.vhd:L938-L942](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L938-L942)，`severity Failure` 会让仿真直接终止。两端的错误信息都明确指引你「Use cl_fix_round_fmt()」。

**`cl_fix_resize` 自动算 `rounded_fmt`**：

[en_cl_fix.py:L240-L253](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L240-L253)（Python）与 [en_cl_fix_pkg.vhd:L1009-L1022](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1009-L1022)（VHDL）——都先 `rounded_fmt = cl_fix_round_fmt(a_fmt, r_fmt.F, rnd)`，再 `cl_fix_round`，最后 `cl_fix_saturate`。注意饱和阶段传入的是 `rounded_fmt`（含 +1 整数位）而非 `a_fmt`，因为饱和要求 `F` 与 round 后一致。

**`cl_fix_saturate` 的告警依赖 `cl_fix_in_range`**：

[en_cl_fix_pkg.vhd:L988-L992](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L988-L992) —— `Warn_s` / `SatWarn_s` 模式下，用 `cl_fix_in_range` 判断是否越界，越界则 `assert ... severity Warning`。也就是说，饱和告警的判定本身就是 `in_range`，二者逻辑同源。

**`cl_fix_recommended_pipelining` 的契约与 0 拍优化**：

[en_cl_fix_pkg.vhd:L1048-L1059](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1048-L1059) —— 先 `assert` 契约（u6-l1 会详讲），再判断：`Trunc_s` 因为「无加法、无进位逻辑」返回 0（不需要寄存器）；其他模式因有加偏移逻辑可能需要寄存器。这条规则与 `for_round` 的「Trunc 不 +1」是同一个根因的两种表现。

#### 4.3.3 代码实践（源码阅读型）

**实践目标**：跟踪 `cl_fix_resize` 内部如何「自动」用 `round_fmt` 处理 +1 整数位，体会「用 resize 就不必手算」。

**操作步骤**：

1. 打开 [en_cl_fix.py:L240-L253](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L240-L253) 的 `cl_fix_resize`。
2. 回顾 4.1.4 的例子：`a_fmt=[0,4,4]`、目标小数位 0、`NonSymPos_s`。`cl_fix_resize` 内部会算出 `rounded_fmt = for_round([0,4,4], 0, NonSymPos) = [0,5,0]`。
3. 用 Python 直接验证这条链路（不必手算 `rounded_fmt`）：

   ```python
   # 示例代码
   a_fmt = FixFormat(0, 4, 4)
   r_fmt = FixFormat(0, 5, 0)        # 注意：故意给足 5 个整数位以容纳进位
   a = cl_fix_from_real(15.9375, a_fmt)   # a_fmt 的最大值
   y = cl_fix_resize(a, a_fmt, r_fmt, FixRound.NonSymPos_s, FixSaturate.SatWarn_s)
   print(cl_fix_to_real(y, r_fmt))   # 预期 16.0（进位后被保留，未饱和）
   ```

**需要观察的现象**：`resize` 因为 `r_fmt` 给了 5 个整数位（等于 `for_round` 的预测），15.9375 进位成 16.0 后被**保留**，输出 16.0，且不会触发饱和告警。

**思考题（预期结论）**：若把 `r_fmt` 改成 `[0,4,0]`（只给 4 个整数位，**少于** `for_round` 预测的 5），会发生什么？——`cl_fix_resize` 内部仍会先 round 到 `rounded_fmt=[0,5,0]`（得到 16.0），再 saturate 到 `[0,4,0]`（最大值 15），于是 16.0 被钳位成 15，并触发 `SatWarn` 告警。这说明：**目标格式 `r_fmt` 是你可以主动选择的（甚至可以比预测更窄，靠饱和兜底）；但 round 阶段的中间格式 `rounded_fmt` 必须由库按 `round_fmt` 自动确定，不能由你压窄**。

> 提示：思考题的结论是依据源码逻辑推导的「预期结论」，可在本地用 `warnings` 捕获 `SatWarn` 告警来验证（VHDL 端则观察仿真器的 Warning 输出）。

## 5. 综合实践

把本讲三个知识点（`round_fmt` 的 +1、`in_range` 先舍入后判界、resize 自动处理）串成一个小任务：**为一个「乘法 → 舍入 → 饱和」的定点通路预判是否会发生饱和**。

**任务背景**：两个输入相乘后得到一个很宽的中间格式，你要把它 resize 到一个较窄的输出格式。在真正搭建硬件（或跑 resize）之前，先用纯函数预判：取最坏情况的输入值，它舍入后会不会越界？

**操作步骤**：

```python
# 示例代码
import sys
sys.path.append("bittrue/models/python")
from en_cl_fix_pkg import *

# 1) 输入与乘积格式（用 u3-l2 的 for_mult 预测全精度乘积格式）
a_fmt = FixFormat(1, 7, 8)
b_fmt = FixFormat(0, 7, 8)
mid_fmt = FixFormat.for_mult(a_fmt, b_fmt)     # 全精度乘积格式
print("mid_fmt =", mid_fmt)                     # 预期 (1, 14, 16)

# 2) 输出格式：故意压窄整数位，制造潜在饱和
out_fmt = FixFormat(1, 3, 1)                    # 范围 -8 .. 7.5
rnd = FixRound.NonSymPos_s

# 3) 取乘积的最大值作为最坏情况，先算舍入后的中间格式
rounded_fmt = FixFormat.for_round(mid_fmt, out_fmt.F, rnd)
print("rounded_fmt =", rounded_fmt)             # 预期 (1, 15, 1)（整数位 +1）

# 4) 用 in_range 预判：乘积最大值舍入后是否越界？
prod_max = cl_fix_max_value(mid_fmt)            # 乘积最大值
in_rng = cl_fix_in_range(prod_max, mid_fmt, out_fmt, rnd)
print("prod_max in range?", bool(in_rng))       # 预期 False（会饱和）

# 5) 用 resize 实际跑一遍，对照结论（给足位宽以观察是否真被钳位到 out_fmt 上界）
y = cl_fix_resize(prod_max, mid_fmt, out_fmt, rnd, FixSaturate.Sat_s)
print("resized  =", cl_fix_to_real(y, out_fmt)) # 预期被钳位到 7.5
print("out max  =", cl_fix_max_value(out_fmt))  # 7.5
```

**需要观察与思考**：

1. `mid_fmt` 由 `for_mult` 给出（u3-l2），`rounded_fmt` 由 `for_round` 给出（本讲）——两者都是**综合期可算**的纯函数，不需要任何数据。
2. `in_range` 在「先 round 后判界」后正确预判出「乘积最大值会越界」。
3. `resize` 实跑结果被饱和到 7.5，与 `in_range` 的预判一致——证明 `in_range` 就是 resize 饱和阶段的「只读探针」。
4. 体会联动：整条链路里，**只有 `out_fmt` 是你主动设计的**；`mid_fmt`、`rounded_fmt` 全部由库的纯函数自动推导，`resize` 自动按 `rounded_fmt` 处理 +1 整数位。

> 提示：上述数值结果为依据源码逻辑推导的预期值。`prod_max` 是否恰好取到 `for_mult` 预测的最坏情况，取决于 `a_fmt`/`b_fmt` 的最大值乘积；若想严格复现「最坏情况」，可直接用 `cl_fix_max_value(a_fmt) * cl_fix_max_value(b_fmt)` 作为输入。请在本地运行确认。

## 6. 本讲小结

- **舍入可能让整数位 +1**：非 `Trunc_s` 模式会先加约半个 LSB 的偏移再截断，当小数部分接近「下一个整数」时会向整数位进位，使整数位最多增长 1 位；`Trunc_s` 只丢位不加偏移，整数位永不增长。
- **`for_round` / `cl_fix_round_fmt` 的三分支**：`rFracBits >= a_fmt.F`（不减小数位）或 `Trunc_s` → 整数位不变；其他情况 → 整数位 `+1`；再用「保宽守卫」确保结果至少 1 位宽。Python 与 VHDL 逐字镜像。
- **`cl_fix_in_range` 必须「先舍入、后判界」**：因为 resize = 先 round 后 saturate，饱和看到的是舍入后的值。它用 `round_fmt` 推导更宽的 `rounded_fmt`，把数据 round 进去，再与 `result_fmt` 的 min/max 跨格式比较。同一个值在不同舍入模式下可能得到不同的 in_range 结果。
- **`round_fmt` 是 `cl_fix_round` 的格式契约**：`cl_fix_round` 用 `assert` 强制 `result_fmt == cl_fix_round_fmt(...)`，把「别忘了 +1」变成运行时硬约束。
- **用 `cl_fix_resize` 即可免去手算**：`resize` 内部自动算 `rounded_fmt` 并处理 +1；只有绕过 resize 直接调 `cl_fix_round`、或自搭通路时，才必须自己调 `cl_fix_round_fmt`。
- **联动同源**：`cl_fix_saturate` 的告警判定就是 `cl_fix_in_range`；`cl_fix_recommended_pipelining` 中「Trunc 返回 0 拍」与 `for_round` 中「Trunc 不 +1」是同一根因的两种表现。

## 7. 下一步学习建议

至此 U3「结果格式预测」三讲（加减法 u3-l1、乘法等 u3-l2、舍入与越界本讲）全部完成，你已经掌握所有**综合期纯函数**的格式推导。接下来建议：

- **进入 U4（Python 参考实现）**：本讲反复用到 `cl_fix_round`、`cl_fix_resize`、`cl_fix_from_real`，但都没讲它们**内部**如何用 `NarrowFix`（≤53 位双精度浮点）和 `WideFix`（任意精度整数）做数值计算。u4-l2、u4-l3 会揭晓 `round` 的偏移实现与 narrow/wide 的分发。
- **进入 U5（VHDL 包内部实现）**：u5-l2 会逐行讲 `cl_fix_round` 的 `mid_fmt`、`half_c`、`convert` 与各模式 `case` 分支，以及 `cl_fix_saturate` 的钳位逻辑——本讲只引用了它们的接口与 assert，U5 才是真正的位运算实现。
- **若关心可综合组件**：u6-l1 会讲 `en_cl_fix_round` / `en_cl_fix_resize` 这三个可实例化组件如何把本讲的纯函数包成带 `clk/rst` 的流水线模块，并用 `cl_fix_recommended_pipelining` 决定寄存器插入。
- **推荐先做的复习**：重读 u2-l2 的「round(x)=trunc(x+offset)」与 u4-l1 的「resize = round ⟶ saturate」，本讲的全部结论都建立在这两条之上。
