# mult / neg / abs / shift 的格式推导与特殊情况

## 1. 本讲目标

本讲承接 u3-l1（加减法结果格式预测），把「结果格式预测」推广到乘法、取反、绝对值、移位四类运算。学完后你应当能够：

- 说清乘法结果格式 `[S,I,F]` 的四个分量（小数位、rmaxI、rminI、符号位）分别如何推导，并能识别「双方有符号 → +1 整数位」「1 位幅值 → -1 整数位」「1 位有符号 × 1 位有符号 → 无符号」这三类特殊情况。
- 解释取反 `for_neg` 为什么对「1 位无符号」输入要做少 1 位的特殊处理，以及有符号取反因补码不对称而 +1 位的根因。
- 理解绝对值 `for_abs = union(a, neg(a))` 为何结果恒为有符号，并判断它在何种输入下是保守的。
- 用 `for_shift` 计算固定移位与可变移位的格式，并解释可变移位为何会让总位宽增长。
- 确认 Python 参考实现与 VHDL `cl_fix_mult_fmt`/`cl_fix_neg_fmt`/`cl_fix_abs_fmt`/`cl_fix_shift_fmt` 逐字镜像。

本讲只讲「格式预测」（纯函数，综合期可算），不讲 `cl_fix_mult`/`cl_fix_neg` 等位运算本身的实现（留待 U5）。

## 2. 前置知识

阅读本讲前，请确认你已经掌握以下概念（来自 u2-l1、u2-l4、u3-l1）：

- **定点格式 `[S,I,F]`**：`S` 为符号位（0 或 1），`I` 为整数位，`F` 为小数位，三者均可参与正负；总位宽 \(W = S+I+F\)。
- **位权重锚点模型**：最低位（LSB）权重恒为 \(2^{-F}\)；有符号时最高位（符号位）权重为 \(-2^{I}\)；无符号时最高位权重为 \(2^{I}-2^{F_{\text{?}}}\)。格式可表示范围为
  - 最大值 \(v_{\max} = 2^{I} - 2^{-F}\)（任意 `S`）；
  - 最小值 \(v_{\min} = -2^{I}\)（有符号）或 \(0\)（无符号）。
- **保守最坏情况原则**：格式预测函数假设输入「可取任意值」，给出**充分且最小**（既装得下、又不浪费）的结果格式。
- **rmax/rmin 双侧夹逼**：u3-l1 引入的方法——分别计算结果的最大值 `rmax` 与最小值 `rmin`，再用它们反推需要多少整数位。
- **union**：对若干格式的 `S/I/F` 各取最大，得到能容纳它们全部的最小公共超集（u2-l4）。

本讲会反复用到两个记号：把某格式 `a` 的「幅值位宽」记为 \(x = a.I + a.F\)（即不含符号位的位宽），它的取值范围记为 \([a_{\min}, a_{\max}]\)。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [en_cl_fix_types.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py) | Python 参考实现，`FixFormat` 类的 `for_mult`/`for_neg`/`for_abs`/`for_shift`/`union` 静态方法都在此文件。 |
| [en_cl_fix_pkg.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd) | VHDL 金标准。`cl_fix_mult_fmt`、`cl_fix_neg_fmt`、`cl_fix_abs_fmt`、`cl_fix_shift_fmt` 的包体实现，以及包头的公共 API 声明。 |
| [format_tests.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/format_tests.py) | Python 单元测试，用 numpy 穷举验证各 `for_*` 给出的格式「充分且必要」。本讲的代码实践会对照它。 |

## 4. 核心概念与源码讲解

### 4.1 乘法格式预测 for_mult / cl_fix_mult_fmt

#### 4.1.1 概念说明

乘法是所有算术中格式推导最微妙的，原因有三：

1. **小数位会叠加**：两个输入的 LSB 权重相乘，\(2^{-a.F} \times 2^{-b.F} = 2^{-(a.F+b.F)}\)，所以结果小数位**恒为** \(a.F + b.F\)，没有例外。
2. **整数位会增长，且增长量分情形**：两个 \(N\) 位幅值相乘，结果幅值最多 \(2N\) 位，但具体是 \(a.I+b.I\)、\(a.I+b.I+1\) 还是 \(a.I+b.I-1\)，取决于符号组合与幅值宽度。
3. **符号性可能反转**：有符号数相乘结果一般仍需符号位，但「1 位有符号 × 1 位有符号」的乘积恒非负，结果反而**无符号**。

与 u3-l1 的加减法一样，乘法格式预测也遵循「保守最坏情况」原则，用 rmax/rmin 双侧夹逼来确定整数位；但乘法的两个极端来自**四个乘积中的最值**：\(a_{\min}b_{\min}\)、\(a_{\min}b_{\max}\)、\(a_{\max}b_{\min}\)、\(a_{\max}b_{\max}\)。

#### 4.1.2 核心流程

`for_mult` 把结果格式拆成四个独立分量分别计算：

```text
结果 = ( S , I , F )
  F  = a.F + b.F                              # 小数位：无条件叠加
  I  = max(rmaxI, rminI_需求)                  # 整数位：取两侧夹逼的较大者
  S  = 1位有符号×1位有符号 ? 0 : max(a.S, b.S)  # 符号位：唯一特例
```

**第 1 步：rmaxI（由最大乘积反推整数位）**

最大乘积 `rmax` 取决于符号组合：

- **双方都有符号**（`a.S=1, b.S=1`）：两个最小值（最负）相乘反而最大：
  \[
  r_{\max} = a_{\min} \cdot b_{\min} = (-2^{a.I})(-2^{b.I}) = 2^{a.I+b.I}
  \]
  \(2^{a.I+b.I}\) 是一个**纯 2 的幂**，表示它需要 \(a.I+b.I+1\) 个整数位（因为 \(2^{k}\) 恰好顶到第 \(k+1\) 位）。所以 `rmaxI = a.I + b.I + 1`。这就是「两个负数相乘可能撑大整数位」的来源。
- **其余情况**（至少一方无符号）：
  \[
  r_{\max} = a_{\max} \cdot b_{\max} = (2^{a.I}-2^{-a.F})(2^{b.I}-2^{-b.F})
  \]
  通常需要 \(a.I+b.I\) 个整数位；但当乘积严格小于 \(2^{a.I+b.I-1}\) 时可以**省 1 位**。令 \(x=a.I+a.F\)、\(y=b.I+b.F\)（即双方的幅值位宽），源码把这个不等式化简为
  \[
  (2^{x}-2)(2^{y}-2) < 2 \quad\Longleftrightarrow\quad x \le 1 \ \text{或}\ y \le 1
  \]
  也就是说：**只要任一操作数的幅值只有 1 位宽**（如 `[0,1,0]`、`[1,0,0]`、`[0,0,1]`），乘积就够小，可省 1 个整数位，`rmaxI = a.I+b.I-1`。

**第 2 步：rminI（由最小乘积反推整数位）**

最小乘积同样分符号组合：

| a.S, b.S | rmin 来源 | 是否可能超过 rmaxI |
| --- | --- | --- |
| 0, 0 | \(0 \times 0 = 0\) | 否，忽略 |
| 1, 1 | \(\min(a_{\max}b_{\min}, a_{\min}b_{\max})\) | 否，永不超过 rmaxI，忽略 |
| 0, 1 | \(a_{\max} \cdot b_{\min} = -a_{\max}\cdot 2^{b.I}\) | **可能**，整数位需求 = `for_neg(a).I + b.I` |
| 1, 0 | \(a_{\min} \cdot b_{\max}\) | **可能**，整数位需求 = `a.I + for_neg(b).I` |

只有「一方有符号、一方无符号」时，rmin 侧才可能比 rmax 侧更宽；此时调用 `for_neg`（4.2 节）来算无符号那一方取负后的整数位。最终 `I = max(rmaxI, rmin_需求)`。

**第 3 步：符号位 S**

唯一的特例：**两个输入都是 1 位有符号**（`width==1` 且 `S==1`）时，结果恒非负，`S=0`；否则 `S = max(a.S, b.S)`（任一输入有符号则结果有符号）。

> 直觉：1 位有符号格式（如 `[1,0,0]`）只能表示 \(0\) 或 \(-1\)。四个乘积为 \(0,0,0,1\)，最小值 \(0\)、最大值 \(1\)，确实全非负，所以无需符号位。

#### 4.1.3 源码精读

Python 实现位于 `FixFormat.for_mult`，整体结构与上面的三分支一一对应：

- 小数位无条件叠加并返回：[en_cl_fix_types.py:269](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L269)（`return FixFormat(S, I, a_fmt.F+b_fmt.F)`）。
- rmaxI 的三分支（双方有符号 +1 / 幅值 1 位 -1 / 否则普通），含完整数学推导注释：[en_cl_fix_types.py:213-235](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L213-L235)。关键判断在
  ```python
  if a_fmt.S == 1 and b_fmt.S == 1:
      rmaxI = a_fmt.I + b_fmt.I + 1
  elif a_fmt.I+a_fmt.F <= 1 or b_fmt.I+b_fmt.F <= 1:
      rmaxI = a_fmt.I + b_fmt.I - 1
  else:
      rmaxI = a_fmt.I + b_fmt.I
  ```
- rminI 借助 `for_neg` 处理「有符号 × 无符号」：[en_cl_fix_types.py:254-259](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L254-L259)。
- 符号位特例（1 位有符号 × 1 位有符号 → 无符号）：[en_cl_fix_types.py:262-267](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L262-L267)。

VHDL `cl_fix_mult_fmt` 与 Python **逐字镜像**，连注释里的数学推导都完全一致：

- 三分支结构（注意 VHDL 先用 `cl_fix_neg_fmt` 算好两个取反格式备用）：[en_cl_fix_pkg.vhd:508-540](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L508-L540)。
- rmin 借助取反格式：[en_cl_fix_pkg.vhd:559-565](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L559-L565)。
- **符号位特殊判断**（本讲重点）：[en_cl_fix_pkg.vhd:567-574](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L567-L574)：
  ```vhdl
  if cl_fix_width(a_fmt) = 1 and a_fmt.S = 1 and cl_fix_width(b_fmt) = 1 and b_fmt.S = 1 then
      -- Special case: 1-bit signed * 1-bit signed is unsigned
      S_v := 0;
  else
      S_v := maximum(a_fmt.S, b_fmt.S);
  end if;
  ```
  这就是「VHDL `cl_fix_mult_fmt` 中的符号位特殊判断」——它用 `cl_fix_width(...) = 1` 同时约束「宽度等于 1」与「带符号」，精确锁定这一唯一会让乘积变无符号的角落。

公共 API 声明在包头：[en_cl_fix_pkg.vhd:90](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L90)（`cl_fix_mult_fmt` 的函数签名）。

#### 4.1.4 代码实践

**实践目标**：用 Python 验证三类边界情形下 `for_mult` 的符号位与整数位，亲手确认上面的推导。

**操作步骤**：在仓库根目录执行（依赖已在 u1-l3 安装）：

```bash
cd bittrue/models/python
python3 -c "
from en_cl_fix_pkg import FixFormat

# 情形 1：1 位有符号 × 1 位有符号 → 无符号（符号位特例）
print('1s*1s      :', FixFormat.for_mult(FixFormat(1,0,0), FixFormat(1,0,0)))

# 情形 2：有符号 × 无符号（多位无符号操作数）
print('[1,4,0]*[0,4,0]:', FixFormat.for_mult(FixFormat(1,4,0), FixFormat(0,4,0)))

# 情形 3：两个负数相乘（双方有符号）→ +1 整数位
print('[1,4,0]*[1,4,0]:', FixFormat.for_mult(FixFormat(1,4,0), FixFormat(1,4,0)))

# 情形 4：幅值 1 位 → -1 整数位
print('[0,1,0]*[0,1,0]:', FixFormat.for_mult(FixFormat(0,1,0), FixFormat(0,1,0)))
"
```

**需要观察的现象与预期结果**：

| 情形 | 预期输出 | 说明 |
| --- | --- | --- |
| 1 | `FixFormat(0, 1, 0)` | 符号位特例：S=0；双方有符号分支给 rmaxI=0+0+1=1；F=0+0=0 |
| 2 | `FixFormat(1, 8, 0)` | 普通有符号×无符号：rmaxI=4+4=8，rmin 侧=4+for_neg([0,4,0]).I=4+4=8，取 max=8 |
| 3 | `FixFormat(1, 9, 0)` | 双方有符号 → +1：rmaxI=4+4+1=9；结果需容纳 256=2^8，`[1,8,0]` 只到 255 装不下 |
| 4 | `FixFormat(0, 1, 0)` | 幅值 1 位 → -1：rmaxI=1+1-1=1；乘积最大 1×1=1 刚好装进 `[0,1,0]` |

> 若你的环境未装依赖，可改为「源码阅读型实践」：对照 [format_tests.py:182-223](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/format_tests.py#L182-L223) 的 mult 断言，手工把上述四个情形的 `rmax`/`rmin` 填入，验证 `rmax <= cl_fix_max_value(r_fmt)` 且 `rmin >= cl_fix_min_value(r_fmt)`。

#### 4.1.5 小练习与答案

**练习 1**：`[1,0,0] * [0,4,0]` 的结果格式是什么？为什么 rmaxI 算出来是 3，最终整数位却是 4？

> **答案**：结果是 `[1,4,0]`。`[1,0,0]` 只能取 0 或 -1，\(a_{\max}=0\)，所以 rmax 侧乘积为 0，rmaxI 走「幅值 1 位」分支得 \(0+4-1=3\)；但 rmin 侧 \(a_{\min}\cdot b_{\max}=(-1)\times 15=-15\)，需要 `for_neg([0,4,0]).I + a.I = 4 + 0 = 4`，取 `max(3,4)=4`。这正是「rmin 侧救援 rmaxI」的典型例子。

**练习 2**：为什么双方都有符号时 rmaxI 要 +1，而双方都无符号时从不 +1？

> **答案**：双方有符号时，最大乘积来自 \(a_{\min}\cdot b_{\min}=(-2^{a.I})(-2^{b.I})=2^{a.I+b.I}\)，是纯 2 的幂，刚好顶到第 \(a.I+b.I+1\) 位，所以 +1。双方无符号时最大乘积是 \((2^{a.I}-2^{-a.F})(2^{b.I}-2^{-b.F})\)，严格小于 \(2^{a.I+b.I}\)，至多需要 \(a.I+b.I\) 位，因而从不 +1（甚至幅值 1 位时还能 -1）。

### 4.2 取反 for_neg：补码不对称与 1 位无符号特例

#### 4.2.1 概念说明

取反 `-a` 看似简单，却藏着定点设计里一个经典坑：**补码的不对称性**。

对有符号格式 `[1,I,F]`，可表示范围是 \([-2^{I},\ 2^{I}-2^{-F}]\)——负方向能到 \(-2^{I}\)，正方向只能到 \(2^{I}-2^{-F}\)，**正负不对称**。所以把最小值 \(-2^{I}\) 取反得到 \(+2^{I}\)，它**无法**装回原格式（原格式正方向最多到 \(2^{I}-2^{-F}\)），必须多 1 个整数位。

而无符号格式 `[0,I,F]` 取反会变成**有符号**（因为结果可能为负），符号位从 0 变 1。

#### 4.2.2 核心流程

```text
结果 = for_neg(a)
  F  = a.F                          # 小数位不变
  S  = 1                            # 取反结果恒有符号
  I  = a.S==0 且 a 为 1 位 ? a.I + a.S - 1
       否则                      ? a.I + a.S
```

- **有符号输入**（`a.S=1`）：`I = a.I + 1`，即补码不对称带来的 +1 位。
- **多位无符号输入**（`a.S=0, width>1`）：`I = a.I + 0 = a.I`，仅增加符号位。
- **1 位无符号特例**（`a.S=0, width==1`）：`I = a.I + a.S - 1 = a.I - 1`，比常规公式少 1 位。

为什么 1 位无符号能少 1 位？因为 1 位无符号满足 `I+F=1`，其最大值
\[
v_{\max} = 2^{I} - 2^{-F} = 2^{I} - 2^{I-1} = 2^{I-1}
\]
恰好是**纯 2 的幂**。于是 \(-v_{\max} = -2^{I-1}\) 恰好等于符号位权重 \(-2^{I-1}\)，正好落在 `[1, I-1, F]` 的符号位上，只需 \(I-1\) 个整数位。而多位无符号的最大值 \(2^{I}-2^{-F}\) 不是 2 的幂，取负后 \(-(2^{I}-2^{-F})\) 落在两个 2 的幂之间，需要完整的 \(I\) 位。

> 这与 4.1.2 中「幅值 1 位 → 乘法 -1 位」、u3-l1 中「rmin 恰为 2 的幂 → 减法更紧」是同一类数学现象：**当某个极端值恰好是 2 的幂时，它能精确落在某个符号位边界上，从而省 1 位**。

#### 4.2.3 源码精读

Python 实现极其简短，特例在前、常规在后：[en_cl_fix_types.py:272-285](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L272-L285)：
```python
# 1-bit unsigned inputs are special (neg is 1-bit signed)
if a_fmt.S == 0 and a_fmt.width == 1:
    return FixFormat(1, a_fmt.I+a_fmt.S-1, a_fmt.F)
return FixFormat(1, a_fmt.I+a_fmt.S, a_fmt.F)
```
注意常规分支用 `a_fmt.I + a_fmt.S`：对有符号 `a.S=1` 自动 +1，对无符号 `a.S=0` 自动 +0，一条表达式兼顾两种情况。

VHDL `cl_fix_neg_fmt` 同样镜像，注释点明「1 位无符号取反得到 1 位有符号」与「补码不对称导致有符号 +1 位」：[en_cl_fix_pkg.vhd:579-588](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L579-L588)。注意它用 `cl_fix_width(a_fmt) = 1` 判定 1 位，与 `for_mult` 的符号位特例判定方式完全一致。

#### 4.2.4 代码实践

**实践目标**：观察三种输入下 `for_neg` 的整数位差异，验证「1 位无符号少 1 位」。

**操作步骤**：

```bash
cd bittrue/models/python
python3 -c "
from en_cl_fix_pkg import FixFormat
# 有符号取反：补码不对称 → +1 整数位
print('neg [1,4,0]:', FixFormat.for_neg(FixFormat(1,4,0)))   # 期望 (1, 5, 0)
# 多位无符号取反：仅加符号位，整数位不变
print('neg [0,4,0]:', FixFormat.for_neg(FixFormat(0,4,0)))   # 期望 (1, 4, 0)
# 1 位无符号特例：少 1 位
print('neg [0,1,0]:', FixFormat.for_neg(FixFormat(0,1,0)))   # 期望 (1, 0, 0)
print('neg [0,0,1]:', FixFormat.for_neg(FixFormat(0,0,1)))   # 期望 (1, -1, 1)
"
```

**预期结果**：`[1,4,0]→[1,5,0]`、`[0,4,0]→[1,4,0]`、`[0,1,0]→[1,0,0]`、`[0,0,1]→[1,-1,1]`。最后一个例子里 `[0,0,1]` 最大值 \(0.5=2^{-1}\)，取反 \(-0.5=-2^{-1}\) 落在 `[1,-1,1]` 的符号位上（宽度仍为 1）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `for_neg` 不需要像 `for_mult` 那样分别算 rmaxI 与 rminI？

> **答案**：取反是一元运算，极端只有两个：\(-a_{\min}=2^{a.I}\)（最大）与 \(-a_{\max}\)（最小）。对有符号输入，\(-a_{\min}=2^{a.I}\) 是主导约束（需要 \(a.I+1\) 位）；对无符号输入，\(a_{\min}=0\) 无约束，主导约束是 \(-a_{\max}\)。两种情况都已被 `a.I+a.S`（或特例）覆盖，无需双侧夹逼。

**练习 2**：`for_neg([1,-1,1])`（1 位有符号）的结果是什么？

> **答案**：是 `[1,0,1]`。`[1,-1,1]` 的可表示值为 \(\{0, -0.5\}\)，取反得 \(\{0, 0.5\}\)；\(0.5=2^{0}-2^{-1}\) 恰是 `[1,0,1]` 的最大值。走常规分支 `a.I+a.S = -1+1 = 0`，得 `[1,0,1]`。这里 1 位有符号**没有**触发减 1（特例只针对 1 位**无符号**）。

### 4.3 绝对值 for_abs：union(a, neg(a)) 的恒有符号特性

#### 4.3.1 概念说明

绝对值 \(|a|\) 的结果只可能是 \(a\)（当 \(a\ge 0\)）或 \(-a\)（当 \(a<0\)）。所以要容纳 \(|a|\) 的所有可能取值，结果格式必须**同时**装得下 \(a\) 的范围和 \(-a\) 的范围——这正是 `union` 的定义（u2-l4）。

于是 `for_abs` 的实现只有一行：`union(a_fmt, for_neg(a_fmt))`。

由此立刻得到一个重要性质：**`for_abs` 的结果恒为有符号（S=1）**。因为 `for_neg` 恒返回 `S=1`（4.2 节），`union` 对 `S` 取 `max`，所以无论输入是否有符号，`for_abs` 结果都带符号位。

- 对**有符号输入**，这是必要的：\(|-2^{I}|=2^{I}\) 必须多 1 个整数位，且正方向扩到 \(2^{I}\)，结果自然有符号。
- 对**无符号输入**，这是**保守的**：无符号值恒非负，\(|a|=a\)，结果本可保持无符号 `[0,I,F]`，但 `for_abs` 仍返回有符号 `[1,I,F]`。这是为换取「一行实现 + 与 `for_neg` 复用」而接受的代价，单元测试也按此保守语义编写。

#### 4.3.2 核心流程

```text
结果 = for_abs(a)
  = union( a , for_neg(a) )
  F = a.F                       # 两者 F 相同
  S = max(a.S, 1) = 1           # 恒有符号
  I = max(a.I, for_neg(a).I)    # 取较大整数位
```

典型结果：
- 有符号 `[1,4,0]`（范围 \([-16,15]\)）→ `for_neg=[1,5,0]` → `union=[1,5,0]`（需容纳 \(|{-16}|=16\)）。
- 无符号 `[0,4,0]`（范围 \([0,15]\)）→ `for_neg=[1,4,0]` → `union=[1,4,0]`（保守，本可 `[0,4,0]`）。

#### 4.3.3 源码精读

Python `for_abs` 一行委托给 `for_neg` 与 `union`：[en_cl_fix_types.py:288-299](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L288-L299)。
`union` 对 `S/I/F` 各取最大：[en_cl_fix_types.py:345-362](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L345-L362)。

VHDL `cl_fix_abs_fmt` 同样一行：[en_cl_fix_pkg.vhd:590-594](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L590-L594)；其调用的 VHDL `union`：[en_cl_fix_pkg.vhd:353-360](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L353-L360)。

#### 4.3.4 代码实践

**实践目标**：验证 `for_abs` 恒有符号，并亲手确认无符号输入下它是保守的。

**操作步骤**：

```bash
cd bittrue/models/python
python3 -c "
from en_cl_fix_pkg import FixFormat
# 有符号取绝对值：需容纳 +2^I，故 +1 整数位，仍为有符号
print('abs [1,4,0]:', FixFormat.for_abs(FixFormat(1,4,0)))   # 期望 (1, 5, 0)
# 无符号取绝对值：保守地返回有符号（不是 (0,4,0)！）
print('abs [0,4,0]:', FixFormat.for_abs(FixFormat(0,4,0)))   # 实际 (1, 4, 0)
"
```

**需要观察的现象**：第二条输出是 `(1, 4, 0)` 而非直觉上的 `(0, 4, 0)`。这正是「恒有符号」特性的体现。对照测试 [format_tests.py:254-280](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/format_tests.py#L254-L280)：注意它的 `rmin = min(amin, -amax)` 会算出负值（如 -3），从而**要求**结果格式能容纳负数——测试与实现共同维护了这一保守语义。

#### 4.3.5 小练习与答案

**练习**：如果要把 `for_abs` 改成「对无符号输入返回无符号」的更紧版本，需要改哪里？会有什么风险？

> **答案**：可在 `union` 前加判断：若 `a.S==0` 直接返回 `a`（因为无符号恒非负，\(|a|=a\)）。风险是：它将与现有 VHDL 实现、`format_tests.py` 的 `rmin` 断言（期望能容纳 \(-a_{\max}\)）都不一致，破坏「Python 与 VHDL 逐字镜像」的不变量。所以当前保守写法是刻意为之。

### 4.4 无损移位 for_shift：移动小数点而非搬数据

#### 4.4.1 概念说明

左移 `a << n` 在定点语境里是「乘以 \(2^{n}\)」。en_cl_fix 把它实现为**纯格式重标注**：数据位不变，只改变 `[S,I,F]` 的解释——等价于把小数点向左挪了 \(n\) 位。因此固定移位是**无损且零成本**的（综合后不产生任何逻辑），唯一要算的是新的格式标注。

当移位量在综合期未知（可变移位，落在 \([n_{\min}, n_{\max}]\)）时，结果必须用一个**固定宽度**的信号同时容纳所有可能的移位结果，于是总位宽会增长。

#### 4.4.2 核心流程

`for_shift(a, minShift, maxShift)`：

```text
结果 = ( a.S , a.I + maxShift , a.F - minShift )
```

- **整数位**用 `maxShift`：最大移位产生最大值，需要最多的整数位 \(a.I + n_{\max}\)。
- **小数位**用 `minShift`：最小移位（甚至负移位＝右移）产生最细的分辨率，需要 \(a.F - n_{\min}\) 个小数位。
- **符号位**不变。
- **总位宽** \(= a.W + (n_{\max} - n_{\min})\)。固定移位（\(n_{\min}=n_{\max}\))时位宽不变；可变移位时位宽增长 \(n_{\max}-n_{\min}\)，这正是「移位量未知」带来的额外位。

固定移位是可变移位的特例，因此 `for_shift(a, n)`（单参数版本）等价于 `for_shift(a, n, n)`，结果 \((a.S,\ a.I+n,\ a.F-n)\)，值放大 \(2^{n}\) 倍而位宽不变。

#### 4.4.3 源码精读

Python `for_shift` 含参数校验与一行计算：[en_cl_fix_types.py:302-315](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L302-L315)，单参数版本把 `maxShift` 默认设为 `minShift`：[en_cl_fix_types.py:312-315](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L312-L315)。

VHDL 提供两个重载：可变移位版本（含 `assert min_shift <= max_shift`）：[en_cl_fix_pkg.vhd:596-601](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L596-L601)，单参数版本委托给前者：[en_cl_fix_pkg.vhd:603-606](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L603-L606)。公共声明见 [en_cl_fix_pkg.vhd:96-97](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L96-L97)。

#### 4.4.4 代码实践

**实践目标**：用 `for_shift` 计算 `a=[0,7,0]` 左移 4 位的格式，并对比固定移位与可变移位的位宽差异。

**操作步骤**：

```bash
cd bittrue/models/python
python3 -c "
from en_cl_fix_pkg import FixFormat
a = FixFormat(0,7,0)                      # 无符号，范围 0..127
# 固定左移 4（两种写法等价）
print('shift 4        :', FixFormat.for_shift(a, 4))      # 期望 (0, 11, -4)
print('shift [4,4]    :', FixFormat.for_shift(a, 4, 4))   # 同上
# 可变移位 [2,5]：位宽增长 5-2=3
r = FixFormat.for_shift(a, 2, 5)
print('shift [2,5]    :', r, 'width =', r.width)          # 期望 (0, 12, -2) width 10
print('原位宽 =', a.width, ' 增量 =', r.width - a.width)  # 期望增量 3
"
```

**预期结果**：
- `shift 4` → `(0, 11, -4)`，宽度 \(0+11-4=7\)，与输入相同。值放大 \(2^{4}=16\) 倍：`[0,7,0]` 的 0..127 变成 `[0,11,-4]` 的 0..2032（\(2^{11}-2^{4}=2032\)）。
- `shift [2,5]` → `(0, 12, -2)`，宽度 \(0+12-2=10\)，比输入多 3 位（\(=5-2\)）。

> 若环境未装依赖，可阅读 [format_tests.py:282-315](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/format_tests.py#L282-L315)，对照其 `rmax = amax*2**max_shift`、`rmin` 分符号讨论的写法，手算上述两例的极端值并验证落入库给出的格式内。

#### 4.4.5 小练习与答案

**练习 1**：`for_shift([1,4,4], -2)`（右移 2）的结果是什么？值的范围如何变化？

> **答案**：`[1, 4+(-2), 4-(-2)] = [1, 2, 6]`，宽度不变（10）。右移 2 等于除以 4：原范围 \([-16, 15.9375]\)（步长 \(2^{-4}\)）变成 \([-4, 3.984375]\)（步长 \(2^{-6}\)），整数位少 2、小数位多 2，精度提高而总值缩小。

**练习 2**：为什么可变移位的位宽增量恰好是 \(n_{\max}-n_{\min}\)，而不是 \(n_{\max}\)？

> **答案**：固定宽度的输出信号要同时容纳所有移位量。整数位由 \(n_{\max}\) 决定（最高位），小数位由 \(n_{\min}\) 决定（最低位），总位宽 \(= (a.I+n_{\max}) + (a.F-n_{\min}) + a.S = a.W + (n_{\max}-n_{\min})\)。增量是「移位量未知区间」的宽度 \(n_{\max}-n_{\min}\)，而非单个 \(n_{\max}\)，因为输入本身的 \(a.W\) 位在所有移位下都被复用。

## 5. 综合实践

设计一条「**系数乘法 → 左移 2 位 → 取绝对值**」的定点数据通路，用本讲四个 `for_*` 函数逐级算出格式，体会它们如何串起来。

设输入采样 `a = [1,7,8]`（有符号 Q7.8，范围约 \([-128, 127.996]\)），系数 `c = [1,0,15]`（有符号 Q0.15，范围 \((-1, 1)\)）。

**操作步骤**：

```bash
cd bittrue/models/python
python3 -c "
from en_cl_fix_pkg import FixFormat
a = FixFormat(1,7,8); c = FixFormat(1,0,15)

# 第 1 级：乘法
m = FixFormat.for_mult(a, c)
print('mult  :', m)                       # 双方有符号 → +1 整数位

# 第 2 级：固定左移 2
s = FixFormat.for_shift(m, 2)
print('shift :', s)

# 第 3 级：绝对值
z = FixFormat.for_abs(s)
print('abs   :', z)
"
```

**预期结果与分析**：

1. `mult = [1, 8, 23]`：双方有符号，`rmaxI = 7+0+1 = 8`；小数位 `8+15 = 23`。
2. `shift = [1, 10, 21]`：固定左移 2，`I+2=10`、`F-2=21`，宽度不变。
3. `abs = [1, 11, 21]`：`for_abs` 恒有符号；`for_neg([1,10,21])=[1,11,21]`，`union` 后整数位取 `max(10,11)=11`。

**需要观察的现象**：通路上每级格式都由上一级格式经一个纯函数推出，**无需任何位运算**即可在综合期确定所有信号位宽。这正是 en_cl_fix「先算结果格式、再用全精度中间值 + resize 收敛」范式的体现（详见 u4-l1）。请把这条通路画成草图，标注每级的 `[S,I,F]` 与用到的 `for_*` 函数。

## 6. 本讲小结

- 乘法结果格式分四个独立分量计算：小数位恒为 `a.F+b.F`；整数位由 rmaxI（双方有符号 +1 / 幅值 1 位 -1 / 否则普通）与 rminI（仅「有符号×无符号」可能更宽，借助 `for_neg`）取 `max`；符号位有唯一特例「1 位有符号 × 1 位有符号 → 无符号」。
- 「极端值恰为 2 的幂」是一条贯穿全讲的暗线：乘法双方有符号的 \(2^{a.I+b.I}\)、1 位无符号取反的 \(2^{I-1}\) 都因落在符号位边界而改变位宽计数。
- 取反 `for_neg` 恒返回有符号；有符号输入因补码不对称 +1 整数位，1 位无符号因最大值是 2 的幂而 -1 位。
- 绝对值 `for_abs = union(a, neg(a))` 结果恒有符号（S=1）；对无符号输入是刻意的保守。
- 移位 `for_shift` 是无损的格式重标注：`I += maxShift`、`F -= minShift`，固定移位位宽不变，可变移位位宽增长 \(n_{\max}-n_{\min}\)。
- Python `FixFormat.for_*` 与 VHDL `cl_fix_*_fmt` 逐字镜像，连注释中的数学推导都一致；`format_tests.py` 用 numpy 穷举验证每个格式的「充分且必要」。

## 7. 下一步学习建议

- 本讲只讲了**格式预测**。要看到这些格式如何驱动真实位运算，请进入 U5：先读 [en_cl_fix_pkg.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd) 中 `cl_fix_mult`（乘法实现）与 `cl_fix_neg`/`cl_fix_abs`/`cl_fix_shift` 的包体，对照本讲的 `for_*` 看「预测格式」如何成为「全精度中间格式 `mid_fmt`」。
- 若想理解 `cl_fix_resize`（先 round 后 saturate 的精度收敛器）如何把本讲的乘法全精度结果收敛到更窄的目标格式，可衔接 u3-l3（round_fmt）与 U4（Python 主接口的三段式范式）。
- 建议动手扩展 [format_tests.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/format_tests.py)：为本讲综合实践里的「乘法 → 移位 → 绝对值」链路写一个穷举式最优性断言，检验每级格式是否「充分且必要」。
