# 为什么需要预测结果格式：add/sub

## 1. 本讲目标

本讲是 U3「结果格式预测」的第一讲。学完后你应当能够：

- 说清楚**为什么**在写 HDL 之前就必须知道 `a + b` / `a - b` 的结果格式，以及 en_cl_fix 用什么函数来算它。
- 理解「保守最坏情况原则」：结果格式必须**充分**（能装下任意输入下的精确结果）且**最小**（不比必要更宽）。
- 手算加法何时多出 1 位整数位、减法何时新增符号位，并用 `FixFormat.for_add` / `for_sub` 验证。
- 解释 `for_sub` 在「无符号减有符号」时的特殊处理（结果居然可能仍是**无符号**）。
- 在 Python 与 VHDL 两种镜像实现之间自由切换，理解 `for_addsub = union(for_add, for_sub)` 的含义。

本讲只讲**格式预测**（算出结果的 `[S,I,F]`），不讲真正做加减法的 `cl_fix_add` / `cl_fix_sub` 的位运算实现（那是 U5 的事）。但本讲会顺便指出：格式预测函数的产物，正是加减法函数的「全精度中间格式」。

## 2. 前置知识

本讲承接两篇前置讲义，不再重复其细节：

- **u2-l1 FixFormat [S,I,F] 定点表示**：你需要记得格式三元组 `[S,I,F]` 的含义——`S` 为符号位（0 或 1），`I` 为整数位，`F` 为小数位；位宽 \(W = S+I+F\)；可表示范围为最大值 \(2^{I}-2^{-F}\)、有符号最小值 \(-2^{I}\)、无符号最小值 \(0\)。
- **u2-l4 位宽、极值与格式工具函数**：你需要记得 `cl_fix_width` 与 `union`。`union(a_fmt, b_fmt)` 对 `S/I/F` 各取最大值，得到「能同时装下两个格式的最小公共超集」。本讲的 `for_addsub` 就建立在它之上。

另外，承接 **u1-l2（三语言镜像架构）**：Python 的 `FixFormat.for_add` 与 VHDL 的 `cl_fix_add_fmt` 是**同名同语义**的镜像，连注释都几乎逐字相同。所以本讲会把两者并排讲，你只需理解一遍逻辑。

> 一个关键直觉：补码定点数做加减，**位运算本身是无损的**——只要给足够宽的位宽，结果一个比特都不会错。真正决定「会不会丢信息」的是你给结果分配多宽。所以「预测结果格式」不是数值计算问题，而是**最坏情况下的位宽推导问题**。

## 3. 本讲源码地图

本讲涉及的核心文件只有两个（镜像关系）：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py) | Python 参考模型的类型与格式推导 | `FixFormat.for_add` / `for_sub` / `for_addsub` / `union` |
| [hdl/en_cl_fix_pkg.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd) | 可综合 VHDL 主包 | `cl_fix_add_fmt` / `cl_fix_sub_fmt` / `cl_fix_addsub_fmt` / `union` |

辅助理解（格式函数如何被真正使用）：

| 文件 | 作用 |
|------|------|
| [bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py) | Python 主接口，`cl_fix_add_fmt = FixFormat.for_add` 等别名，以及 `cl_fix_add` 中的用法 |

## 4. 核心概念与源码讲解

### 4.1 动机：硬件为何必须先知道结果格式

#### 4.1.1 概念说明

在软件里写 `c = a + b`，你不需要提前告诉编译器 `c` 有多少位——Python 的整数甚至可以无限大。但在 FPGA/ASIC 的 RTL 里，**每一根线（signal）的位宽都在综合阶段（elaboration time）就被钉死**。你不能写一段「结果可能 8 位、也可能 9 位」的逻辑。

于是出现一个根本问题：当你要把两个定点数相加、并声明结果信号 `c` 的位宽时，`c` 到底该多宽？

- 给窄了：最坏情况下结果溢出，数值错误。
- 给宽了：浪费寄存器/LUT/DSP 资源，时序也变差。

en_cl_fix 的回答是：提供一个**纯函数** `cl_fix_add_fmt(a_fmt, b_fmt)`，输入两个操作数的格式，输出一个**保证能精确装下任意输入下相加结果**的最小格式。这个函数：

- 不接触任何具体数值，只看格式；
- 基于「最坏情况」推导，因此对任意合法输入都安全；
- 同时做到**最小**（minimal），不浪费哪怕一位。

这就是「结果格式预测（format prediction）」。它是 en_cl_fix 整套设计范式的第一步——回顾 u4-l1 的三段式：「预测全精度中间格式 `mid_fmt` → 在 `mid_fmt` 下无损运算 → `cl_fix_resize` 收敛到目标格式」。本讲解的就是三段式的第一段。

#### 4.1.2 核心流程

「保守最坏情况」推导有一个统一的方法论，叫 **rmax / rmin 双侧夹逼**：

1. 写出两个输入格式的**可表示范围** `[amin, amax]`、`[bmin, bmax]`。
2. 算出结果的两个极端：
   - \(r_{\max}\)：结果可能的最大值；
   - \(r_{\min}\)：结果可能的最小值。
3. 用 `rmax` 决定需要多少**整数位 / 是否扩整数位**；用 `rmin` 决定是否需要**符号位 / 是否因负向溢出再扩一位**。
4. 小数位 `F` 永远取 `max(a.F, b.F)`（加减不改变小数精度，对齐到更精细的那个即可）。

对于加法：

\[ r_{\max} = a_{\max} + b_{\max}, \qquad r_{\min} = a_{\min} + b_{\min} \]

对于减法：

\[ r_{\max} = a_{\max} - b_{\min}, \qquad r_{\min} = a_{\min} - b_{\max} \]

「整数位是否 +1」就转化为：这两个极端值会不会顶破 `max(a.I, b.I)` 所能表示的整数范围。后续 4.2、4.3 就是把这个判断化简成几行整数表达式。

#### 4.1.3 源码精读

先看预测函数如何被消费。在 VHDL 的真正加法 `cl_fix_add` 里，第一行就用到了 `cl_fix_add_fmt`：

[cl_fix_add 中用预测格式作为全精度中间格式 hdl/en_cl_fix_pkg.vhd:1156-1168](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1156-L1168) —— `mid_fmt_c := cl_fix_add_fmt(a_fmt, b_fmt)`，随后把 `a`、`b` 都 `convert` 到这个 `mid_fmt_c` 下做 `signed` 加法，最后 `cl_fix_resize` 收敛。

而 `result_fmt` 参数默认值是哨兵 `NullFixFormat_c`，下面这行说明「不指定结果格式时，就用预测出来的 `mid_fmt_c`」：

[默认结果格式回退到预测格式 hdl/en_cl_fix_pkg.vhd:1157](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1157) —— `choose(result_fmt = NullFixFormat_c, mid_fmt_c, result_fmt)`。

Python 侧完全镜像，且更直白地揭示了「预测就是三段式的第一步」：

[Python cl_fix_add 用预测格式作 mid_fmt bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py:324](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L324) —— `mid_fmt = cl_fix_add_fmt(a_fmt, b_fmt)`，紧接着 `if r_fmt is None: r_fmt = mid_fmt`。

而 `cl_fix_add_fmt` 在 Python 里只是静态方法的别名（零开销）：

[格式预测函数即 FixFormat 静态方法别名 bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py:62-64](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix.py#L62-L64) —— `cl_fix_add_fmt = FixFormat.for_add`、`cl_fix_sub_fmt = FixFormat.for_sub`、`cl_fix_addsub_fmt = FixFormat.for_addsub`。

#### 4.1.4 代码实践

**目标**：在源码中确认「预测格式 = 加法函数的默认输出位宽」。

1. 打开 [hdl/en_cl_fix_pkg.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd)，定位 `cl_fix_add`（约 1150 行起）。
2. 找到 `constant mid_fmt_c : FixFormat_t := cl_fix_add_fmt(...)` 这一行。
3. 观察它如何把 `a`、`b` `convert` 到 `mid_fmt_c` 后再相加。
4. **现象/预期**：你会看到 `cl_fix_add` 内部根本没有重新推导位宽，它完全信任 `cl_fix_add_fmt` 给出的格式。这就是「预测」与「运算」的职责分离。
5. （无需运行，源码阅读型实践。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cl_fix_add_fmt` 是纯函数（只依赖两个输入格式、不看任何运行期数值）？

> **答案**：因为硬件位宽必须在综合期确定，而综合期看不到运行期的具体数值；格式预测必须对「所有可能的数值」都安全，所以只能基于格式的**最坏情况范围**推导，自然与具体数值无关。

**练习 2**：`result_fmt` 缺省时，`cl_fix_add` 的结果位宽由谁决定？

> **答案**：由 `cl_fix_add_fmt(a_fmt, b_fmt)` 决定；哨兵 `NullFixFormat_c` 触发回退到预测的 `mid_fmt`（见 [hdl/en_cl_fix_pkg.vhd:1157](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1157)）。

---

### 4.2 加法格式推导：for_add / cl_fix_add_fmt 的整数位增长

#### 4.2.1 概念说明

两个定点数相加，结果小数位显然是 `F = max(a.F, b.F)`（对齐到更精细的那一个，低位补零即可，无损）。符号位是 `S = max(a.S, b.S)`（只要有一个是有符号，结果就可能为负）。唯一需要仔细推的是**整数位 `I` 是否比 `max(a.I, b.I)` 多 1 位**。

整数位增长的来源有两个，分别来自 `rmax` 和 `rmin`：

- **`rmax` 增长（正向溢出）**：两个最大值相加可能顶破 `max(a.I, b.I)`。经典例子：两个 `[1,7,0]`（范围 \([-128,127]\)）相加，\(127+127=254\)，需要 8 个整数位（\[-256,255\] 才装得下 254），所以多 1 位。
- **`rmin` 增长（负向溢出）**：只有当**两个输入都有符号**时，两个最负值 \(-2^{a.I} + (-2^{b.I})\) 才会比 \(-2^{\max(a.I,b.I)}\) 更负一位，从而需要 +1 位。

最终整数位取 `max(a.I, b.I) + max(rmax_growth, rmin_growth)`——两种增长只要命中其一就 +1。

#### 4.2.2 核心流程

`for_add` 的推导流程（伪代码）：

```
F = max(a.F, b.F)
S = max(a.S, b.S)

# rmax 增长：化简后的闭式条件
rmax_growth = 1  if min(a.I, b.I) + min(a.F, b.F) > 0  else 0

# rmin 增长：只有双方都有符号
rmin_growth = 1  if a.S == 1 and b.S == 1  else 0

I = max(a.I, b.I) + max(rmin_growth, rmax_growth)
return [S, I, F]
```

`rmax_growth` 那个看起来「天外飞来」的闭式条件 `min(a.I,b.I) + min(a.F,b.F) > 0`，是源码注释里一长串代数化简的终点。直觉上：它判断「两个格式在量级上是否重叠到足以让最大值之和越过较宽格式的最高位」。注意 `for_add` 开头有断言 `a_fmt.width > 0 and b_fmt.width > 0`，所以不会出现 width=0 的非法格式让该条件误判。

#### 4.2.3 源码精读

Python 实现，`rmax_growth` 闭式条件与 `rmin_growth` 符号判断：

[for_add 的两个增长判断 bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py:112-119](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L112-L119) —— `rmax_growth = 1 if min(a_fmt.I, b_fmt.I) + min(a_fmt.F, b_fmt.F) > 0 else 0` 与 `rmin_growth = 1 if a_fmt.S == 1 and b_fmt.S == 1 else 0`。其上方 83–111 行是完整的代数推导注释，值得通读一遍。

返回三元组，整数位 = `max(a.I,b.I) + max(rmin_growth, rmax_growth)`：

[for_add 返回结果格式 bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py:121](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L121)。

VHDL `cl_fix_add_fmt` 与 Python **逐字对应**，只是把 Python 的三元运算符换成 VHDL 的 `choose(...)`、`min/max` 换成 `minimum/maximum`：

[cl_fix_add_fmt 的增长常量与返回 hdl/en_cl_fix_pkg.vhd:422-435](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L422-L435) —— `rmax_growth_c`、`rmin_growth_c` 与返回的 record 字面量 `(maximum(...), maximum(...)+maximum(...), maximum(...))`。

> 这正是「镜像架构」的价值：算法只在 Python（易于运行验证）里推导一遍，VHDL 照抄，二者由 cosim 流程（U7）逐拍比对保证一致。

#### 4.2.4 代码实践

**目标**：手算两个格式的加法结果格式，再用 Python 验证。

操作步骤：在 `bittrue/models/python/` 目录下运行下面脚本（依赖见 u1-l3，需先 `pip install -r requirements.txt`，本脚本只用纯 Python，无需 numpy）。

```python
# 示例代码：验证 for_add
import sys; sys.path.insert(0, "bittrue/models/python")
from en_cl_fix_pkg import FixFormat

a = FixFormat(1, 1, 1)   # 有符号，范围 [-2, 1.5]，步长 0.5
b = FixFormat(0, 7, 0)   # 无符号，范围 [0, 127]

print("for_add  =", FixFormat.for_add(a, b))   # 预期 (1, 8, 1)
```

手算验证：

- `rmax_growth`：`min(I)+min(F) = min(1,7)+min(1,0) = 1+0 = 1 > 0` → 1。
- `rmin_growth`：`a.S==1 and b.S==1`？`b.S=0` → 0。
- `S = max(1,0)=1`；`I = max(1,7) + max(0,1) = 7+1 = 8`；`F = max(1,0)=1`。
- 结果 `[1,8,1]`。

**需要观察的现象**：和的真值范围是 `[-2+0, 1.5+127] = [-2, 128.5]`。`[1,8,1]` 范围 `[-256, 255.5]` 装得下，且 `I=8` 是最小的（`2^7=128 < 128.5`，7 位不够）。预期打印 `FixFormat(1, 8, 1)`。

> 提示：若运行环境/路径不同导致 import 失败，请按 u1-l3 的方式调整 `PYTHONPATH`。结果应与手算一致；若不一致请先核对格式三元组的含义（u2-l1）。

#### 4.2.5 小练习与答案

**练习 1**：`a = b = FixFormat(1,7,0)`（范围 \([-128,127]\)）相加，`for_add` 返回什么？为什么多 1 位？

> **答案**：返回 `[1,8,0]`（范围 \([-256,255]\)）。因为 `rmax_growth`：`min(I)+min(F)=7+0=7>0` → 1；`127+127=254` 顶破 7 位整数（最大 127），需要 8 位。`rmin_growth` 同样为 1（双方有符号，\(-128-128=-256\)），`max(1,1)=1`，共 +1 位。

**练习 2**：举一个 `for_add` **不产生整数位增长**的合法例子。

> **答案**：例如 `a=FixFormat(1,0,3)`（\([-1, 0.875]\)）、`b=FixFormat(0,5,0)`（\([0,31]\)）。`min(I)+min(F)=0+0=0` → `rmax_growth=0`；`rmin_growth=0`（b 无符号）。和的范围 \([-1, 31.875]\)，`max(I)=5` 的 `[1,5,3]`（\([-32, 31.875]\)）正好够，整数位不增长。

---

### 4.3 减法格式推导：for_sub / cl_fix_sub_fmt 的符号处理

#### 4.3.1 概念说明

减法比加法棘手，因为 `a - b` 可能让**符号性发生翻转**：两个无符号数相减，结果可能为负，于是凭空多出一个符号位。`for_sub` 仍用 rmax/rmin 双侧夹逼，但两侧都要单独推导。

- **`rmax = amax - bmin`**：若 `b` 无符号，`bmin = 0`，于是 `rmax = amax`，整数位直接取 `a.I`、不增长；若 `b` 有符号，`bmin = -2^{b.I}`，相当于 `amax + 2^{b.I}`，可能增长 1 位。
- **`rmin = amin - bmax`**：这一侧决定是否需要符号位、以及负向是否增长 1 位。若 `a` 无符号，`amin = 0`，`rmin = -(2^{b.I} - 2^{-b.F}) < 0`，结果会变负——**通常**因此需要符号位 `S=1`。但有**两个特例**能让结果仍为无符号或只需更窄的整数位（见下）。

「无符号减有符号」的特殊处理（本讲实践任务的重点）有两种特例，都在 `a.S == 0` 分支里：

1. **`b` 是 1 位有符号**（`b.width==1 and b.S==1`，即 `b ∈ {0, -1}`）：`a - b` 只可能是 `a` 或 `a+1`，永远非负，结果**仍为无符号**（`S=0`）。
2. **`b.I == -b.F + 1`**（`rmin` 恰为 2 的幂）：负向边界刚好落在一个 2 的幂上，整数位可少取一位。

#### 4.3.2 核心流程

`for_sub` 的推导流程（伪代码，省略部分特例注释）：

```
F = max(a.F, b.F)

# ---- rmax = amax - bmin ----
if b.S == 0:                  # b 无符号 → rmax = amax
    rmaxI = a.I
else:                         # b 有符号 → rmax = amax + 2^b.I
    rmax_growth = 1 if min(a.I, b.I) >= -a.F else 0
    rmaxI = max(a.I, b.I) + rmax_growth

# ---- rmin = amin - bmax ----
if a.S == 0:                  # a 无符号 → 结果可能为负
    if b.width == 1 and b.S == 1:        # 特例 1：b 是 1 位有符号
        S, I = 0, rmaxI                  # 结果仍无符号
    elif b.I == -b.F + 1:                # 特例 2：rmin 恰为 2 的幂
        S, I = 1, max(rmaxI, -b.F)
    else:                                # 一般：结果变负，需符号位
        S, I = 1, max(rmaxI, b.I)
else:                         # a 有符号
    S = 1
    rmin_growth = 1 if min(a.I, b.I) > -b.F else 0
    rminI = max(a.I, b.I) + rmin_growth
    I = max(rmaxI, rminI)

return [S, I, F]
```

注意两处比较符的**非对称**：`rmax` 侧用 `>= -a.F`，`rmin` 侧（有符号 a）用 `> -b.F`。这对应「正向边界恰好顶到 2 的幂」与「负向边界恰好顶到 2 的幂」时是否算增长的细微差别，源码注释里有逐项推导。

#### 4.3.3 源码精读

Python `for_sub` 的 `rmax` 侧：

[for_sub 的 rmaxI 推导 bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py:146-150](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L146-L150) —— `b_fmt.S == 0` 时直接取 `a_fmt.I`；否则按 `min(a_fmt.I, b_fmt.I) >= -a_fmt.F` 判断是否 +1。

`a` 无符号时的三个分支（含两个特例）：

[for_sub 无符号 a 的符号位决策 bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py:163-175](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L163-L175) —— 注意第一个分支 `if b_fmt.width == 1 and b_fmt.S == 1:` 把结果符号位设为 `0`（仍无符号），这正是「无符号减 1 位有符号」的特殊处理。

`a` 有符号时的 `rmin` 增长与最终 `I`：

[for_sub 有符号 a 分支 bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py:176-181](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L176-L181) —— `rmin_growth = a_fmt.S if min(a_fmt.I, b_fmt.I) > -b_fmt.F else 0`，再 `I = max(rmaxI, rminI)`。

VHDL `cl_fix_sub_fmt` 同样逐字镜像。无符号 `a` 的特例分支：

[cl_fix_sub_fmt 无符号 a 特例 hdl/en_cl_fix_pkg.vhd:475-488](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L475-L488) —— `if cl_fix_width(b_fmt) = 1 and b_fmt.S = 1 then S_v := 0; I_v := rmaxI_v;` 与 Python 第 164 行完全对应。

有符号 `a` 分支：

[cl_fix_sub_fmt 有符号 a 分支 hdl/en_cl_fix_pkg.vhd:489-495](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L489-L495)。

#### 4.3.4 代码实践

**目标**：验证 `a=[1,1,1]` 减 `b=[0,7,0]` 的结果格式，并单独演示「无符号减 1 位有符号」特例。

```python
# 示例代码：验证 for_sub 与无符号减有符号特例
import sys; sys.path.insert(0, "bittrue/models/python")
from en_cl_fix_pkg import FixFormat

a = FixFormat(1, 1, 1)   # 有符号 [-2, 1.5]
b = FixFormat(0, 7, 0)   # 无符号 [0, 127]

print("for_sub =", FixFormat.for_sub(a, b))   # 预期 (1, 8, 1)

# --- 「无符号 a 减 1 位有符号 b」特例 ---
au = FixFormat(0, 7, 0)   # 无符号 [0, 127]
bs = FixFormat(1, 0, 0)   # 1 位有符号，取值 {0, -1}（width = 1）
print("unsigned - 1bit_signed =", FixFormat.for_sub(au, bs))   # 预期 (0, 8, 0)，结果仍无符号！
```

手算第一个：`a-b` 的真值范围 `[-2-127, 1.5-0] = [-129, 1.5]`。`a.S=1` 走有符号分支：`rmaxI`：`b.S=0` → `rmaxI = a.I = 1`；`rmin_growth`：`min(I)=min(1,7)=1 > -b.F=0` → 1，`rminI = max(1,7)+1 = 8`；`I = max(1,8) = 8`；`S=1`；`F=1`。结果 `[1,8,1]`，范围 `[-256, 255.5]` 装得下 `[-129, 1.5]`，且 `-129` 需要 `I=8`（`2^7=128 < 129`）。

第二个特例：`au - bs`，`bs ∈ {0,-1}`，所以 `au - bs ∈ [0, 128]`，**永远非负** → 无符号 `[0,8,0]`（范围 `[0,255]`，装得下 `[0,128]`，且 `128` 需要 `I=8`）。

**需要观察的现象**：第一行打印 `FixFormat(1, 8, 1)`；第二行打印 `FixFormat(0, 8, 0)`——注意第二个的 `S=0`，它**没有**因为「减了一个有符号数」就变成有符号。这正是特例分支的功劳。

> 待本地验证：上述预期由手算与源码逻辑推出，请实际运行确认打印一致。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `a` 无符号、`b` 是 1 位有符号时，`a - b` 仍可保持无符号？

> **答案**：1 位有符号数 `b` 只能取 `0` 或 `-1`（补码下符号位权重 \(-2^{0}=-1\)）。`a - b` 因此只能是 `a` 或 `a+1`，而 `a` 非负，故结果恒非负，无需符号位。见 [en_cl_fix_types.py:164-167](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L164-L167)。

**练习 2**：`a = FixFormat(0,4,0)`（无符号 `[0,15]`）减 `b = FixFormat(0,4,0)`，结果格式是什么？为什么会多出符号位？

> **答案**：`for_sub` 返回 `[1,4,0]`。因为 `a.S=0` 走无符号分支，`b` 不是 1 位有符号、也不满足 `b.I == -b.F+1`（`4 != -0+1`），落入一般分支：`S=1`，`I=max(rmaxI, b.I)`。`rmaxI = a.I = 4`（`b.S=0`），`I = max(4,4)=4`。结果范围需容纳 `0-15 = -15` 到 `15-0 = 15`，`[1,4,0]`（`[-16,15]`）正好。两个无符号数相减结果可能为负，于是凭空多出符号位。

**练习 3**：`rmax` 侧判断用 `>= -a.F`，`rmin` 侧（有符号 a）用 `> -b.F`，为何一个带等号一个不带？

> **答案**：对应「极端值恰好等于某个 2 的幂」时是否触发额外整数位。正向 `rmax` 顶到 2 的幂（`amax + 2^{b.I}` 恰好等于 `2^{max+1}`）时仍需要那一位来表示，故 `>=`；负向 `rmin` 恰为 \(-2^{k}\) 时可用更紧的表示（见特例 2 的思路），故用严格的 `>`。详见源码注释 [en_cl_fix_types.py:159-162](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L159-L162)。

---

### 4.4 addsub：取 add 与 sub 的并集

#### 4.4.1 概念说明

很多数据通路里，同一级硬件既要能做 `a + b`、又要能做 `a - b`（典型如 FIR 滤波器、复数运算、二选一 ALU）。这时中间结果格式必须**同时**装得下加法结果和减法结果。

`for_addsub` 的实现极其简洁：分别算出 `for_add` 和 `for_sub`，再取二者的 `union`（u2-l4 学过的「最小公共超集」）。因为 `union` 对 `S/I/F` 各取最大值，所以结果是「同时满足加法和减法最坏情况」的最小格式。

为什么不能图省事只用 `for_add`？因为减法常常引入 `for_add` 不需要的符号位或更大的整数位（见 4.3 练习 2：两个无符号数 `for_add` 是无符号，`for_sub` 却要变成有符号）。反之亦然。所以必须并集。

#### 4.4.2 核心流程

```
add_fmt = for_add(a_fmt, b_fmt)
sub_fmt = for_sub(a_fmt, b_fmt)
return union(add_fmt, sub_fmt)     # S/I/F 各取 max
```

#### 4.4.3 源码精读

Python `for_addsub` 三行实现：

[for_addsub = union(for_add, for_sub) bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py:196-198](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L196-L198)。

它依赖的 `union`，对三个字段各取最大：

[union 各字段取最大 bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py:359-361](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L359-L361)。

VHDL `cl_fix_addsub_fmt` 同样是「两预测 + union」：

[cl_fix_addsub_fmt 实现 hdl/en_cl_fix_pkg.vhd:500-506](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L500-L506) —— `return union(add_fmt_c, sub_fmt_c)`。

VHDL 的 `union`（注意它是包体里的私有可见函数，与 Python 静态方法镜像）：

[VHDL union 实现 hdl/en_cl_fix_pkg.vhd:353-360](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L353-L360) —— 同样对 `S/I/F` 取 `maximum`。

#### 4.4.4 代码实践

**目标**：验证 addsub 是 add 与 sub 的并集。

```python
# 示例代码：验证 for_addsub = union(for_add, for_sub)
import sys; sys.path.insert(0, "bittrue/models/python")
from en_cl_fix_pkg import FixFormat

a = FixFormat(0, 4, 0)   # 无符号 [0,15]
b = FixFormat(0, 4, 0)   # 无符号 [0,15]

fa = FixFormat.for_add(a, b)        # 预期 (0, 5, 0)：15+15=30 需 5 位
fs = FixFormat.for_sub(a, b)        # 预期 (1, 4, 0)：见 4.3 练习 2
fab = FixFormat.for_addsub(a, b)    # 预期 union = (1, 5, 0)

print("add   =", fa)
print("sub   =", fs)
print("addsub=", fab, " == union?", fab == FixFormat.union(fa, fs))
```

**需要观察的现象**：`for_add` 给 `(0,5,0)`（无符号，因为两个无符号相加不会变负），`for_sub` 给 `(1,4,0)`（有符号，因为相减可能为负）。`addsub` 取并集得 `(1,5,0)`——同时容纳 `30` 与 `-15`。最后一行应打印 `== union? True`。

> 待本地验证：请运行确认最后一行为 `True`。

#### 4.4.5 小练习与答案

**练习 1**：何时 `for_addsub` 会比 `for_add` 多出一个符号位？

> **答案**：当 `for_sub` 因减法结果可能为负而引入符号位、但 `for_add` 不需要时（典型如两个无符号数）。`union` 对 `S` 取 `max`，于是把 `for_sub` 的 `S=1` 带进结果。见 [en_cl_fix_types.py:198](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L198)。

**练习 2**：能否说「`for_addsub` 的整数位 = `max(for_add 的 I, for_sub 的 I)`」？

> **答案**：能。因为 `union` 对 `I` 取 `maximum`。等价地，它是加法与减法各自最坏情况所需整数位的较大者。

---

## 5. 综合实践

**任务：为一个「二选一加减法器」规划结果格式，并解释设计取舍。**

设想一个数据通路：输入 `a`（`[1,7,8]`，有符号定点）与 `b`（`[1,7,8]`），控制位 `op` 为 0 时做 `a+b`、为 1 时做 `a-b`，结果先进入全精度中间格式，之后才（在下一级）做舍入/饱和。

请完成：

1. 用 `FixFormat.for_add`、`for_sub`、`for_addsub` 分别算出三种「结果格式」。
2. 回答：本级中间结果信号应声明为哪种格式？为什么不能用 `for_add` 的结果？
3. 手算 `a=b=[1,7,8]` 时 `for_add` 与 `for_sub` 的差异点（提示：看 `rmin` 增长与符号位），并用 Python 核对。
4. 草图：画出 `a, b → mid_fmt 寄存器 → 后级 resize` 的数据通路，标注 `mid_fmt` 用 `cl_fix_addsub_fmt` 预测（本任务只画格式与选型，不写 RTL——真正的可流水线化组件在 U6 讲）。

**参考思路与预期**：

- `a=b=[1,7,8]`：`for_add` 中 `rmax_growth`：`min(I)+min(F)=7+8=15>0` → 1；`rmin_growth`：双方有符号 → 1；`I = 7+1 = 8`。`S=1, F=8` → `[1,8,8]`。
- `for_sub`：`a.S=1` 走有符号分支。`rmax`：`b.S=1`，`growth = (min(I)>=−a.F ? 1 : 0) = (7 >= -8 ? 1:0)=1`，`rmaxI = max(7,7)+1 = 8`。`rmin`：`growth = (min(I) > -b.F ? 1:0) = (7 > -8 ? 1:0)=1`，`rminI = 8`。`I = max(8,8)=8`。`S=1, F=8` → `[1,8,8]`。
- 二者都是 `[1,8,8]`，故 `for_addsub = [1,8,8]`。
- **结论**：本级 `mid_fmt` 应声明为 `cl_fix_addsub_fmt(a_fmt, b_fmt)` 给出的 `[1,8,8]`。此例中 `for_add` 与 `for_sub` 恰好相同，但**设计上必须用 `addsub`**，因为一旦 `a`、`b` 改为无符号（如 `[0,7,8]`），`for_sub` 会引入 `for_add` 没有的符号位，只有 `addsub` 的并集才对两种 `op` 都安全。用 `for_add` 会在 `op=1` 时漏掉符号位，导致减法结果被错误截断。

> 待本地验证：请用脚本算出 `for_add/for_sub/for_addsub(FixFormat(1,7,8), FixFormat(1,7,8))` 与上述对照，再把两输入换成 `FixFormat(0,7,8)` 观察 `for_sub` 多出的符号位。

## 6. 本讲小结

- **动机**：硬件位宽在综合期就钉死，加减结果必须**提前**用纯函数 `cl_fix_add_fmt` / `cl_fix_sub_fmt` 预测，产物即三段式中的全精度中间格式 `mid_fmt`。
- **方法论**：统一用 **rmax / rmin 双侧夹逼**——`rmax=amax±bmin`、`rmin=amin±bmax`，再据此判断整数位是否 +1、是否新增符号位；`F` 恒取 `max(a.F,b.F)`。
- **加法 `for_add`**：整数位 = `max(a.I,b.I) + max(rmax_growth, rmin_growth)`。`rmax_growth` 的闭式条件为 `min(a.I,b.I)+min(a.F,b.F) > 0`；`rmin_growth` 仅当双方都有符号时为 1。
- **减法 `for_sub`**：`a-b` 可能让符号性翻转。`a` 无符号时一般会新增符号位，但有两个特例（`b` 为 1 位有符号、`rmin` 恰为 2 的幂）可保持更紧的格式。
- **镜像**：Python `FixFormat.for_add/for_sub` 与 VHDL `cl_fix_add_fmt/cl_fix_sub_fmt` 逐字对应（`choose/maximum/minimum` 对应 Python 三元/`max/min`），是「三语言镜像架构」的典型样本。
- **addsub**：`for_addsub = union(for_add, for_sub)`，对 `S/I/F` 各取最大，保证同一通路对加减都安全。

## 7. 下一步学习建议

- **横向**：本讲只覆盖加减。下一讲 **u3-l2** 把同样的 rmax/rmin 方法用到乘法（`for_mult`）以及 `neg/abs/shift`，其中乘法的整数位推导（含「两个负数相乘得 +1 位」「1 位有符号相乘变无符号」等特例）比加减更绕，建议紧接着读。
- **纵向（格式预测的其余部分）**：**u3-l3** 讲 `for_round`（非 Trunc 舍入可能让整数位 +1）与 `cl_fix_in_range`，补齐「结果格式预测」全家桶。
- **落到运算实现**：等 U5（VHDL 包内部实现）你会看到 `cl_fix_add` / `cl_fix_sub` 如何**消费**本讲预测出的 `mid_fmt`，并理解那行著名的「为规避 Vivado DSP bug 而统一用 `signed`」的注释（[hdl/en_cl_fix_pkg.vhd:1164-1168](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1164-L1168)）。
- **源码练习**：通读 [en_cl_fix_types.py:73-198](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L73-L198) 的代数注释，那是本讲所有闭式条件的推导出处。
