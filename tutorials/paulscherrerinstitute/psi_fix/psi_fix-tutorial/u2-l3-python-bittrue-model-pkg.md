# Python 位真模型包

## 1. 本讲目标

本讲是「psi_fix_pkg 定点包」单元的第三讲，从 VHDL 侧（u2-l1、u2-l2）跨到 Python 侧。学完后你应当能够：

- 说清 `model/psi_fix_pkg.py` 如何把 VHDL 的 `psi_fix_fmt_t / psi_fix_rnd_t / psi_fix_sat_t` 三个核心类型在 Python 里镜像出来，并保证两侧**一一对应**。
- 解释每个 `psi_fix_*` 运算函数为什么是「薄封装」——它只翻译格式，真正的数学交给外部库 `en_cl_fix`。
- 掌握 `enable_range_check` / `with_range_check_disabled` 这对开关的用途，以及 `psi_fix_fmt_t` 为什么在总位宽超过 **53 位** 时会抛 `BittruenessNotGuaranteed`。
- 理解「位真模型对中间结果精度有要求」这一约束的来源（IEEE 754 双精度只有 53 位有效位），并能判断一段 Python 模型在何处可能悄悄丢位。

本讲**不**重复 u2-l1 已经讲过的 `[s,i,f]` 三元组与 round/sat 枚举的含义，只在需要时引用；重点放在「Python 侧如何实现、如何与 VHDL 对齐、如何保证位真」。

## 2. 前置知识

阅读本讲前，建议你已经掌握（来自 u1-l1、u2-l1、u2-l2）：

- **位真双模型**：每个可综合 VHDL 组件必须配套一个逐位一致的 Python 模型，由自检测试台逐位比对（u1-l1）。
- **定点格式三元组** `[s,i,f]`：符号位、整数位、小数位，总位宽 `W = s+i+f`，由 `psi_fix_size()` 计算（u1-l4、u2-l1）。
- **VHDL 包的「壳 + 内核」分层**：psi_fix 自己不实现定点数学，而是通过转换桥 `psi_fix2_cl_fix / cl_fix2_psi_fix` 把类型翻译给 `en_cl_fix`，真正的运算由 `cl_fix_*` 完成（u2-l1、u2-l2）。
- **舍入/饱和映射**：`round → NonSymPos_s`、`trunc → Trunc_s`、`sat → Sat_s`、`wrap → None_s`（u2-l1）。

补充一个本讲要用到的 Python 基础概念：

- **IEEE 754 双精度浮点数**用 1 位符号、11 位指数、52 位尾数表示一个实数。因为规格化数隐含一个最高位的 `1`，所以它能**精确表示的整数范围**是 \([-2^{53},\ 2^{53}]\)。超过 53 位的整数无法一一精确表示——这正是本讲「精度限制」模块的物理根源。

## 3. 本讲源码地图

本讲主要涉及两个文件：

| 文件 | 作用 |
|------|------|
| [model/psi_fix_pkg.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py) | Python 位真模型包：定义三个核心类型、`PsiFix2ClFix/ClFix2PsiFix` 转换桥，以及全部 `psi_fix_*` 运算函数（薄封装 en_cl_fix）。 |
| [doc/files/tips.md](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/tips.md) | 设计技巧文档，其中 *Precision of intermediate results* 一节解释了双精度中间结果丢位的问题。 |

辅助参考（用于看 API 在真实组件里如何被调用）：

| 文件 | 作用 |
|------|------|
| [model/psi_fix_mov_avg.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_mov_avg.py) | 滑动平均组件的 Python 位真模型，综合使用了 `from_real / sub / resize / shift_right / mult`。 |
| [testbench/psi_fix_mov_avg_tb/Scripts/preScript.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/Scripts/preScript.py) | 协同仿真脚本：用 Python 模型生成输入/输出的**整数位表示**文本，供 VHDL 测试台读回逐位比对。 |

> 提醒：`model/psi_fix_pkg.py` 自身并不独立——它在第 16–17 行把外部库 `en_cl_fix` 加入 `sys.path` 并 `from en_cl_fix_pkg import *`。没有这个并排摆放的同级仓库，本文件无法运行（详见模块 4.3）。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**Python 类型与 API 镜像** → **位真与精度限制** → **与 en_cl_fix 的复用**。

### 4.1 Python 类型与 API 镜像

#### 4.1.1 概念说明

`psi_fix` 的位真双模型要求「同一个组件」在 VHDL 与 Python 两侧行为逐位一致。要做到这一点，两侧必须**用同一套词汇**描述定点世界：相同的格式类型、相同的舍入/饱和枚举、相同名字的运算函数。

`model/psi_fix_pkg.py` 就是这套词汇在 Python 侧的落地。它镜像了 VHDL `hdl/psi_fix_pkg.vhd` 中的三个核心类型与一组运算函数：

- 类型 `psi_fix_fmt_t`（格式）、`psi_fix_rnd_t`（舍入）、`psi_fix_sat_t`（饱和）——名字与 VHDL 完全一致。
- 运算函数 `psi_fix_size / psi_fix_from_real / psi_fix_from_bits_as_int / psi_fix_get_bits_as_int / psi_fix_resize / psi_fix_add / psi_fix_sub / psi_fix_mult / psi_fix_abs / psi_fix_neg / psi_fix_shift_left / psi_fix_shift_right / psi_fix_upper_bound / psi_fix_lower_bound / psi_fix_in_range`——与 VHDL 函数同名（4.0.0 起 VHDL 与 Python 统一改用 snake_case，见 u1-l1 的版本说明）。

注意「一一对应」是指**核心算术/格式 API**，并非 100% 完全相同：VHDL 侧多了一些综合辅助函数（如 `psi_fix_compare`、`psi_fix_choose_fmt`、`psi_fix_to_real`、字符串解析等），Python 侧则多了两个只为协同仿真/调试服务的助手函数（`psi_fix_write_formats`、`psi_fix_to_hex`）。建模时两侧都用得到的那些函数才是严格对齐的。

#### 4.1.2 核心流程

Python 包的组织可以用下面这张「分层图」概括：

```
┌──────────────────────────────────────────────┐
│  组件模型 (如 psi_fix_mov_avg.Process)        │  ← 用户写的位真模型
│      调用 psi_fix_add / psi_fix_mult / ...    │
├──────────────────────────────────────────────┤
│  psi_fix_* 运算函数 (本文件)                  │  ← 薄封装：翻译格式
│      PsiFix2ClFix(...)  →  cl_fix_*(...)      │
├──────────────────────────────────────────────┤
│  en_cl_fix_pkg (外部库)                       │  ← 真正的定点数学
│      cl_fix_add / cl_fix_mult / ...           │
└──────────────────────────────────────────────┘
```

一次典型运算（以乘法为例）的流程：

1. 调用方传入数值 `a, b` 与各自的 `psi_fix_fmt_t` 格式、结果格式 `r_fmt`、舍入/饱和模式。
2. `psi_fix_mult` 用 `PsiFix2ClFix` 把每个 `psi_fix_*` 类型的参数翻译成 `en_cl_fix` 的类型（`FixFormat / FixRound / FixSaturate`）。
3. 把翻译后的参数整体交给 `en_cl_fix` 的 `cl_fix_mult`，由它完成真正的定点乘法。
4. 返回 `cl_fix_mult` 的结果（仍是数值，但语义上已是 `r_fmt` 格式的定点数）。

这与 VHDL 侧「壳 + 内核」的分层（u2-l1）**完全对称**——两侧都把数学外包给 en_cl_fix，差异只在语言。

#### 4.1.3 源码精读

**(a) 三个核心类型的 Python 实现**

格式的定义在 [model/psi_fix_pkg.py:26-53](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py#L26-L53)。它是一个普通类，持有 `s / i / f` 三个整数属性，并实现了 `__str__`（打印成 `(s, i, f)`）和 `__eq__`（按三元组判等，方便测试断言）：

```python
class psi_fix_fmt_t:
    __enable_range_check = False
    def __init__(self, s : int, i : int, f : int):
        self.s = s; self.i = i; self.f = f
        if psi_fix_size(self) > 53 and self.__enable_range_check:
            raise BittruenessNotGuaranteed(...)
```

> 第 28 行的 `__enable_range_check` 和第 34 行的 53 位检查属于「精度限制」模块（4.2），这里先记住：默认是**关闭**的，所以平时构造大格式不会报错。

舍入与饱和是两个枚举，见 [model/psi_fix_pkg.py:55-61](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py#L55-L61)：

```python
class psi_fix_rnd_t(Enum):
    round = 0
    trunc = 1
class psi_fix_sat_t(Enum):
    wrap = 0
    sat = 1
```

这两个枚举的取值与 VHDL 的 `psi_fix_rnd_t / psi_fix_sat_t` 一一对应（u2-l1）。

**(b) 转换桥 PsiFix2ClFix**

所有运算函数都要先把 psi_fix 类型翻译成 en_cl_fix 类型。这个翻译集中在 [model/psi_fix_pkg.py:66-78](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py#L66-L78)。注意格式翻译的细节——`psi_fix_fmt_t` 用整数 `s`（0 或 1）表示有无符号位，而 en_cl_fix 的 `FixFormat` 用布尔 `Signed`：

```python
def PsiFix2ClFix(arg):
    if type(arg) is psi_fix_fmt_t:
        return FixFormat(arg.s == 1, arg.i, arg.f)   # s==1 → Signed=True
    elif type(arg) is psi_fix_rnd_t:
        if arg == psi_fix_rnd_t.round: return FixRound.NonSymPos_s
        elif arg == psi_fix_rnd_t.trunc: return FixRound.Trunc_s
    elif type(arg) is psi_fix_sat_t:
        if arg == psi_fix_sat_t.wrap: return FixSaturate.None_s   # wrap = 不饱和
        elif arg == psi_fix_sat_t.sat: return FixSaturate.Sat_s
```

这套映射（`round→NonSymPos_s`、`trunc→Trunc_s`、`sat→Sat_s`、`wrap→None_s`）与 u2-l1 讲过的 VHDL 侧映射**完全相同**，这是两侧能位真的前提。反方向翻译 `ClFix2PsiFix` 在 [model/psi_fix_pkg.py:80-95](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py#L80-L95)。

**(c) 一个典型运算函数：psi_fix_mult**

看一个完整的运算函数，[model/psi_fix_pkg.py:143-149](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py#L143-L149)：

```python
def psi_fix_mult(a, a_fmt, b, b_fmt, r_fmt,
                 rnd=psi_fix_rnd_t.trunc, sat=psi_fix_sat_t.wrap):
    return cl_fix_mult(a, PsiFix2ClFix(a_fmt),
                          PsiFix2ClFix(b_fmt),
                          PsiFix2ClFix(r_fmt), PsiFix2ClFix(rnd), PsiFix2ClFix(sat))
```

函数体只有一行：把六个 psi_fix 参数分别翻译，整体交给 `cl_fix_mult`。注意两点：

1. **默认值是 `trunc / wrap`**（最省资源、最易溢出），与组件层默认的 `round / sat`（偏安全）相反——这点 u2-l2 已强调，Python 包沿用了库级默认。
2. 结果格式 `r_fmt` 完全由调用者指定，函数**不会自动位增长**。

`psi_fix_add / sub / resize / abs / neg`（[model/psi_fix_pkg.py:121-159](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py#L121-L159)）都是同样的「翻译 + 委托」骨架。

**(d) 查询与整数互转函数**

`psi_fix_size` 直接问 en_cl_fix 要位宽，[model/psi_fix_pkg.py:101-102](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py#L101-L102)。`from_real / from_bits_as_int / get_bits_as_int` 见 [model/psi_fix_pkg.py:104-119](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py#L104-L119)。其中 `get_bits_as_int` 把一个定点数导出为它的**原始位模式的整数**——这正是协同仿真把浮点结果写成整数文本的底座（见 preScript.py）。

**(e) API 在真实组件里的用法**

`psi_fix_mov_avg.Process` 是把这套 API 串起来用的活样本，[model/psi_fix_mov_avg.py:66-91](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_mov_avg.py#L66-L91)：先 `psi_fix_from_real` 把浮点输入定点化，再 `psi_fix_sub` 做差分、`psi_fix_shift_right` 粗校正、`psi_fix_mult` 精校正，最后输出。每个调用的格式都与 VHDL 侧同名常量一致，这就是「镜像 API」带来的可读性与可对齐性。

#### 4.1.4 代码实践

**实践目标**：亲手做一次「浮点 → 定点 → 取整数位表示」的往返，确认 `psi_fix_from_real` 与 `psi_fix_get_bits_as_int` 的行为，并体会 `psi_fix_fmt_t` 的构造与 `__str__/__eq__`。

**操作步骤**：

1. 确认 `en_cl_fix` 已作为同级仓库存在（与 `psi_fix` 并排摆放，见 u1-l1）。在仓库根目录新建一个临时脚本 `tmp_probe.py`：

   ```python
   # 示例代码（非项目原有文件，实践结束后可删除）
   import sys, os
   sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/model")
   from psi_fix_pkg import *
   import numpy as np

   fmt = psi_fix_fmt_t(0, 1, 7)          # 无符号, 1 整数位, 7 小数位, 共 8 位, 范围 [0, 2)
   print("fmt =", str(fmt))              # 期望: (0, 1, 7)
   print("size =", psi_fix_size(fmt))    # 期望: 8

   x = np.array([0.25, 1.0])
   xFix = psi_fix_from_real(x, fmt)       # 定点化
   print("bits =", psi_fix_get_bits_as_int(xFix, fmt))   # 期望: [32, 128]
   ```

2. 运行 `python3 tmp_probe.py`（**待本地验证**：本环境无法访问同级 `en_cl_fix` 目录，需在你按 u1-l1 摆好依赖的机器上运行）。

**需要观察的现象**：

- `str(fmt)` 打印成 `(0, 1, 7)`，证明 `__str__` 生效。
- `psi_fix_size` 返回 8。
- `get_bits_as_int` 输出 `[32, 128]`。

**预期结果（手算）**：

- 格式 `[0,1,7]` 的量化步长是 \(2^{-7} = 1/128\)。
- \(0.25 \times 128 = 32\)，\(1.0 \times 128 = 128\)。两者都在 `[0, 2)` 范围内，不触发 `from_real` 的 `err_sat` 检查，整数位表示即为 `[32, 128]`。

如果输出与手算一致，就说明你已经掌握「浮点 ↔ 定点位表示」的往返——这是后续协同仿真逐位比对的基石。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Python 包里的运算函数默认 `trunc / wrap`，而组件模型（如 `psi_fix_mov_avg`）构造时却常用 `round / sat`？

**参考答案**：库级运算函数追求「最省资源、把量化决策权交给调用者」，所以默认最激进的 `trunc / wrap`；组件面向真实信号，默认偏安全的 `round / sat` 以避免意外溢出。两者默认值相反是有意为之，建模时必须显式传参对齐两侧。

**练习 2**：`PsiFix2ClFix` 把 `psi_fix_fmt_t(s=1,...)` 翻译成 `FixFormat` 时，第二个参数（`Signed`）取什么值？为什么？

**参考答案**：取 `arg.s == 1`，即 `True`。因为 psi_fix 用整数 `s` 表示符号位个数（0 无符号、1 有符号），而 en_cl_fix 用布尔 `Signed` 表示是否有符号，所以 `s==1` 时 `Signed=True`。

---

### 4.2 位真与精度限制

#### 4.2.1 概念说明

「位真（bittrue）」听起来是个二值概念——要么逐位一致、要么不一致。但在 Python 侧实现位真模型时，有一个容易踩的暗坑：**Python 用 IEEE 754 双精度浮点数承载中间结果，而双精度只有 53 位有效精度**。

这意味着：如果一个定点格式的总位宽 `W = s+i+f > 53`，那么这个格式能表示的整数就无法被双精度一一精确表示，Python 模型就**无法保证**它与 VHDL（任意精度整数运算）逐位一致。psi_fix 把这个限制显式化：提供一个可开关的「范围检查」，一旦格式超过 53 位就抛 `BittruenessNotGuaranteed` 提醒你。

注意这个限制不只针对单个格式——**每一个中间结果**都受其约束。`doc/files/tips.md` 的 *Precision of intermediate results* 一节专门警告：哪怕输入输出都在安全范围内，中间累加（如 `cumsum`）也可能让数值增长到突破 52 位尾数，导致不可恢复的精度损失。

#### 4.2.2 核心流程

53 位检查的触发逻辑：

```
psi_fix_fmt_t(s, i, f) 构造时
   │
   ├── 计算 W = psi_fix_size(self) = s + i + f
   │
   ├── 若 W > 53 且 __enable_range_check == True
   │       └── raise BittruenessNotGuaranteed(...)
   │
   └── 否则正常构造
```

关键点：`__enable_range_check` 是**类属性**（不是实例属性），默认 `False`。也就是说：

- 平时（默认关闭）：可以自由构造任意大格式，库不拦你——但你要自己对位真性负责。
- 打开后：构造超过 53 位的格式会立即报错，把隐患前置到建模阶段。

这个开关可以通过两种方式控制：

- `psi_fix_fmt_t.enable_range_check(True/False)`：类方法，全局开关（[model/psi_fix_pkg.py:43-45](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py#L43-L45)）。
- `with psi_fix_fmt_t.with_range_check_disabled():`：上下文管理器，仅在代码块内临时关闭，退出后自动恢复（[model/psi_fix_pkg.py:47-53](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py#L47-L53)）。

为什么需要「临时关闭」？因为有些组件内部确实会出现超过 53 位的**无损中间格式**（如 FIR 累加链、移位函数里的 `FullFmt_c`），它们在数学上不会丢位（最终会被 resize 回小格式），但若开着检查就会误报。此时用上下文管理器局部豁免，既保留全局保护，又不影响这些已知安全的中间步骤。

#### 4.2.3 源码精读

**(a) 异常类型**

[model/psi_fix_pkg.py:24](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py#L24) 定义了专用异常，名字本身就说明了语义：

```python
class BittruenessNotGuaranteed(Exception): pass
```

**(b) 53 位检查**

检查写在构造函数里，[model/psi_fix_pkg.py:30-35](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py#L30-L35)。阈值 53 正对应双精度的有效位数（52 位显式尾数 + 1 位隐含最高位）：

\[ W_{\max} = 53 \quad\Rightarrow\quad |N| \le 2^{53} \text{ 可被精确表示} \]

**(c) 开关与上下文管理器**

`enable_range_check` 是 `classmethod`，修改的是类属性，[model/psi_fix_pkg.py:43-45](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py#L43-L45)。`with_range_check_disabled` 用 `contextlib.contextmanager` 装饰成上下文管理器，先记下旧值、关闭、`yield`、最后恢复，[model/psi_fix_pkg.py:47-53](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py#L47-L53)。

**(d) 文档对中间结果精度的警告**

`tips.md` 给了一个具体例子：用 `out = mod(cumsum(input), 1.0)` 实现一个累加取模电路，[doc/files/tips.md:112-128](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/tips.md#L112-L128)。当输入接近 1、向量长度超过 16 时，`cumsum` 的结果就超过 52 位尾数能精确表示的范围，`mod` 虽然能把数值拉回小区间，但**累加过程引入的误差无法被 mod 修复**，结果悄悄出错。文档给出的对策是：对这种场景改用 `int64`（或必要的显式循环）来保精度。

#### 4.2.4 代码实践

**实践目标**：亲手触发一次 53 位保护，再用上下文管理器临时绕过，体会「全局保护 + 局部豁免」的用法。

**操作步骤**（在刚才的 `tmp_probe.py` 里追加，**示例代码**）：

```python
# 1) 打开全局检查
psi_fix_fmt_t.enable_range_check(True)
try:
    big = psi_fix_fmt_t(1, 40, 20)   # W = 61 > 53
    print("未报错（不应走到这里）")
except BittruenessNotGuaranteed as e:
    print("捕获预期异常:", e)

# 2) 上下文管理器内临时关闭
with psi_fix_fmt_t.with_range_check_disabled():
    big = psi_fix_fmt_t(1, 40, 20)   # 此处不报错
    print("块内构造成功, size =", psi_fix_size(big))

# 3) 退出块后自动恢复（仍开启），再构造会再次报错
try:
    psi_fix_fmt_t(1, 40, 20)
except BittruenessNotGuaranteed:
    print("块外再次捕获异常, 说明开关已恢复")
```

**需要观察的现象**：第 1 步抛异常；第 2 步在 `with` 块内成功构造；第 3 步出块后再次抛异常。

**预期结果**：依次打印「捕获预期异常」「块内构造成功, size = 61」「块外再次捕获异常, 说明开关已恢复」。**待本地验证**（同样依赖 en_cl_fix 在位）。

#### 4.2.5 小练习与答案

**练习 1**：为什么阈值是 53 而不是 52？

**参考答案**：IEEE 754 双精度的尾数字段是 52 位，但规格化浮点数隐含一个最高位的 `1`，所以有效精度是 53 位，能精确表示 \([-2^{53}, 2^{53}]\) 内的全部整数。因此 53 是「整数能逐位精确表示」的真正上界。

**练习 2**：假设你在写一个 FIR 的 Python 模型，累加链中间格式会到 60 位、但最终 resize 回 36 位。你应该怎么做？

**参考答案**：最终 resize 回小格式说明中间大格式在数学上无损、不会丢位。若全局开了范围检查，应在构造该中间格式的那段代码外包一层 `with psi_fix_fmt_t.with_range_check_disabled():`，既豁免这个已知安全的中间步骤，又保留对其它格式的全局保护。绝不要简单地全局关掉检查。

**练习 3**：默认情况下（`__enable_range_check = False`），构造一个 80 位的格式会发生什么？这意味着什么？

**参考答案**：不会报错，正常构造。这意味着库默认信任使用者——你可以建超大格式，但位真性由你自己负责；一旦用它做协同仿真比对，可能出现 VHDL 与 Python 不一致却不自知。所以建议在建模/测试阶段主动 `enable_range_check(True)`。

---

### 4.3 与 en_cl_fix 的复用

#### 4.3.1 概念说明

模块 4.1 已经看到：每个 `psi_fix_*` 运算函数都只是把参数翻译后交给 `cl_fix_*`。这不是巧合，而是 psi_fix 的核心设计——**不在 Python 侧重复实现定点数学，而是直接复用 en_cl_fix**。

这样做有三个直接好处：

1. **单一真相源**：真正的舍入、饱和、位增长规则只在 en_cl_fix 里实现一次，VHDL 侧和 Python 侧都调用它，两侧行为天然一致——这正是「位真双模型」能成立的工程基础。
2. **少写代码、少出错**：psi_fix 的 Python 包只有约 200 行，绝大部分是薄封装。
3. **聚焦增值部分**：psi_fix 只在 en_cl_fix 不满足需求处自己实现，例如 `from_real` 的 `err_sat` 参数、移位函数的 `max_shift` 签名。

#### 4.3.2 核心流程

复用的接线发生在文件头部，[model/psi_fix_pkg.py:15-17](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py#L15-L17)：

```python
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/../../en_cl_fix/python/src")
from en_cl_fix_pkg import *
```

这条 `sys.path.insert` 解释了 u1-l1 强调的「并排摆放目录结构」：`model/psi_fix_pkg.py` 用相对路径 `../../en_cl_fix/python/src` 去找同级仓库 `en_cl_fix` 的 Python 源码。如果目录布局不对，`from en_cl_fix_pkg import *` 会失败，整个 Python 模型层都无法运行。

`from en_cl_fix_pkg import *` 把 `FixFormat / FixRound / FixSaturate` 类型和 `cl_fix_width / cl_fix_from_real / cl_fix_resize / cl_fix_add / cl_fix_mult / cl_fix_shift / cl_fix_max_value / cl_fix_min_value / cl_fix_in_range / cl_fix_from_bits_as_int / cl_fix_get_bits_as_int / cl_fix_write_formats` 等函数全部引入当前命名空间，于是本文件的运算函数能直接调用它们。

psi_fix 「增值」（即不直接复用、自己加料）的地方有两类：

- **加参数**：`psi_fix_from_real` 多了 `err_sat` 开关（默认 True，超范围直接抛 `ValueError`），见 [model/psi_fix_pkg.py:104-113](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py#L104-L113)。
- **改签名 + 加校验**：`psi_fix_shift_left/right` 多了 `max_shift` 参数，并在调用前检查 `shift` 是否落在 `[0, max_shift]`，见 [model/psi_fix_pkg.py:161-181](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py#L161-L181)（这对应 VHDL 侧为 Vivado 动态移位可综合性所做的特殊处理，详见 u2-l2）。

#### 4.3.3 源码精读

**(a) from_real 的 err_sat 增值**

[model/psi_fix_pkg.py:104-113](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py#L104-L113) 是少数有自己逻辑的函数。`err_sat=True` 时，先用 `psi_fix_upper_bound / psi_fix_lower_bound` 检查输入是否落在格式可表示范围内，超出就抛 `ValueError`；然后才委托 `cl_fix_from_real`（注意它硬编码用 `FixSaturate.Sat_s` 做饱和量化）：

```python
def psi_fix_from_real(a, r_fmt, err_sat=True):
    if err_sat:
        if np.max(a) > psi_fix_upper_bound(r_fmt):  raise ValueError(...)
        if np.min(a) < psi_fix_lower_bound(r_fmt):  raise ValueError(...)
    return cl_fix_from_real(a, PsiFix2ClFix(r_fmt), FixSaturate.Sat_s)
```

真实组件里常用 `err_sat=False` 来跳过这个检查——例如 preScript 生成随机刺激时，[testbench/psi_fix_mov_avg_tb/Scripts/preScript.py:39](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/Scripts/preScript.py#L39) 就是 `psi_fix_from_real(sigAll, inFmt, err_sat=False)`，因为随机信号可能略超范围，应当被静默饱和而不是中断脚本。

**(b) 移位函数的签名差异与校验**

[model/psi_fix_pkg.py:161-181](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py#L161-L181) 里，`psi_fix_shift_left` 在调用 `cl_fix_shift` 前先校验 `shift` 不能为负、不能超过 `max_shift`；`psi_fix_shift_right` 则把 `shift` 取负后传给同一个 `cl_fix_shift`（en_cl_fix 用正负号区分左右移）。这套签名与 VHDL 侧的 `psi_fix_shift_left/right` 对齐，是 u2-l2 讲过的「动态移位可综合性」在 Python 侧的镜像。

**(c) Python 专属助手**

[model/psi_fix_pkg.py:198-203](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py#L198-L203) 有两个 Python 专属函数，VHDL 侧没有对应物：

```python
def psi_fix_write_formats(fmts, names, filename):
    cl_fix_write_formats(fmts, names, filename)   # 把格式信息写文件, 供 TB 读取
def psi_fix_to_hex(a, a_fmt):
    return "0x{:x}".format(psi_fix_get_bits_as_int(a, a_fmt))  # 调试用: 位表示转十六进制
```

它们再次体现了「能复用就复用」：连写格式文件也是直接转给 `cl_fix_write_formats`。

#### 4.3.4 代码实践

**实践目标**：通过**源码阅读**确认「薄封装」事实——统计每个 `psi_fix_*` 运算函数实际调用的 `cl_fix_*`，体会复用程度。

**操作步骤**：

1. 打开 [model/psi_fix_pkg.py:121-192](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py#L121-L192)。
2. 列一张表，左边是 `psi_fix_*` 函数名，右边是它 `return` 的 `cl_fix_*` 调用，并标注哪些函数在 `return` 之外还有额外逻辑。
3. 对照 `from en_cl_fix_pkg import *`（第 17 行）确认这些 `cl_fix_*` 名称都来自 en_cl_fix。

**需要观察的现象**：

- `resize/add/sub/mult/abs/neg/in_range/upper_bound/lower_bound` 的函数体都**只有一行 return**，且都形如 `return cl_fix_xxx(a, PsiFix2ClFix(...), ...)`。
- 只有 `from_real`（多了 `err_sat` 分支）和 `shift_left/shift_right`（多了 `shift` 范围校验）在 `return` 之外有可见的额外代码。

**预期结果**：你会得到一张表，证明绝大多数运算函数是纯薄封装，psi_fix 真正自己加逻辑的只有 `from_real` 与两个移位函数——这就是「与 en_cl_fix 复用」最直观的证据。无需运行，纯阅读即可完成。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `model/psi_fix_pkg.py` 用 `sys.path.insert` 而不是 `pip install en_cl_fix`？

**参考答案**：psi_fix 把 `en_cl_fix` 当作并排摆放的同级源码仓库（见 u1-l1 的目录约定），用相对路径 `../../en_cl_fix/python/src` 直接引用其 Python 源。这样 VHDL 侧和 Python 侧共用同一份 en_cl_fix，版本完全锁定，避免 pip 版本漂移导致两侧不一致。

**练习 2**：`psi_fix_from_real` 的 `err_sat=True` 与 `err_sat=False` 分别适用于什么场景？

**参考答案**：`err_sat=True`（默认）适合建模真实数据流——输入超范围通常意味着上游设计有错，应尽早抛错暴露问题。`err_sat=False` 适合生成测试刺激（如 preScript 里的高斯随机数），刺激可能略超格式范围，应当被静默饱和量化、而不是中断数据生成脚本。

**练习 3**：假如 en_cl_fix 未来新增了一个 `cl_fix_div`，psi_fix 想暴露 `psi_fix_div`，最少要改哪里？

**参考答案**：在 `model/psi_fix_pkg.py` 仿照 `psi_fix_mult` 加一个薄封装函数：用 `PsiFix2ClFix` 翻译各 `psi_fix_*` 参数，整体委托 `cl_fix_div`，默认 `trunc / wrap`。同时要在 VHDL 侧 `hdl/psi_fix_pkg.vhd` 加同名函数并通过转换桥委托，才能保持两侧位真对应——这正说明了「单一真相源」的双面性：扩展也要两侧同步。

---

## 5. 综合实践

把三个模块串起来，完成规格里指定的核心实践：**用 `model/psi_fix_pkg.py` 实现一个简单定点乘法，对比 `psi_fix_from_real → psi_fix_mult → psi_fix_get_bits_as_int` 的输出与手算结果是否一致**。这个任务同时检验「类型构造」「API 镜像」「en_cl_fix 复用」三件事。

**实践目标**：验证 Python 包的乘法流水在每一步的数值都与手算一致，从而建立对「Python 模型是黄金参考」的信心。

**操作步骤**：

1. 确认 `en_cl_fix` 已按 u1-l1 并排摆放。在仓库根目录新建 `tmp_mult_probe.py`（**示例代码**，非项目原有文件）：

   ```python
   import sys, os
   sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/model")
   from psi_fix_pkg import *
   import numpy as np

   # 两个无符号 [0,1,14] 输入（16 位, 范围 [0, 2)）
   aFmt = psi_fix_fmt_t(0, 1, 14)
   # 乘积按位增长规则: [0,a,b] x [0,c,d] = [0, a+c, b+d], 取 r=[0,2,14] 并截断
   rFmt = psi_fix_fmt_t(0, 2, 14)

   a = np.array([0.5])
   b = np.array([0.625])

   aFix = psi_fix_from_real(a, aFmt)                                   # 0.5  定点化
   bFix = psi_fix_from_real(b, aFmt)                                   # 0.625 定点化
   prod = psi_fix_mult(aFix, aFmt, bFix, aFmt, rFmt,
                       psi_fix_rnd_t.trunc, psi_fix_sat_t.wrap)        # 乘法
   bits = psi_fix_get_bits_as_int(prod, rFmt)                          # 取整数位表示

   print("a bits  =", psi_fix_get_bits_as_int(aFix, aFmt))   # 期望 8192  (0x2000)
   print("b bits  =", psi_fix_get_bits_as_int(bFix, aFmt))   # 期望 10240 (0x2800)
   print("prod    =", prod)                                  # 期望 0.3125
   print("p bits  =", bits)                                  # 期望 5120  (0x1400)
   ```

2. 运行 `python3 tmp_mult_probe.py`（**待本地验证**：本环境无法访问同级 `en_cl_fix`，请在依赖就位的机器上运行）。
3. 实践结束后删除 `tmp_mult_probe.py` 与上一节的 `tmp_probe.py`（它们不在版本控制里，不应残留）。

**需要观察的现象**：四行打印依次为 `8192`、`10240`、`0.3125`、`5120`。

**预期结果（手算验证）**：

- `[0,1,14]` 的步长为 \(2^{-14} = 1/16384\)。
- \(0.5 \times 16384 = 8192 = \text{0x2000}\)；\(0.625 \times 16384 = 10240 = \text{0x2800}\)。
- 数学乘积 \(0.5 \times 0.625 = 0.3125\)，恰好可被 `[0,2,14]` 精确表示（截断不丢位）。
- \(0.3125 \times 16384 = 5120 = \text{0x1400}\)。

若四行输出与上述一致，则说明 `from_real → mult → get_bits_as_int` 的整条链路与手算逐位吻合——这正是这套 Python 包被当作「黄金参考」去比对 VHDL 输出的底气。若 `prod` 不是干净的 `0.3125`，请检查是否误用了有符号格式或舍入模式。

---

## 6. 本讲小结

- `model/psi_fix_pkg.py` 在 Python 侧镜像了 VHDL 的三个核心类型（`psi_fix_fmt_t / psi_fix_rnd_t / psi_fix_sat_t`）和一组同名运算函数，构成位真双模型的「共同词汇」。
- 每个运算函数都是**薄封装**：用 `PsiFix2ClFix` 把 psi_fix 类型翻译成 en_cl_fix 类型，再整体委托 `cl_fix_*`；库级默认 `trunc / wrap`。
- psi_fix 自己只在必要处增值：`from_real` 加了 `err_sat` 范围检查，`shift_left/right` 加了 `max_shift` 签名与校验，其余几乎全是单行 return。
- **53 位精度限制**源于 IEEE 754 双精度只有 53 位有效位；`psi_fix_fmt_t` 提供 `enable_range_check` 开关与 `with_range_check_disabled` 上下文管理器，把超宽格式的隐患前置到建模阶段。
- 复用 en_cl_fix 通过 `sys.path.insert("../../en_cl_fix/python/src")` + `from en_cl_fix_pkg import *` 实现，依赖并排摆放的目录结构；这也是 VHDL 与 Python 两侧能天然位真的工程基础。
- 注意「一一对应」针对核心算术 API；VHDL 侧有额外的综合辅助函数，Python 侧有 `write_formats / to_hex` 专属助手，两侧并非 100% 对称。

## 7. 下一步学习建议

- **进入测试方法论（u3-l1、u3-l2）**：本讲只讲了 Python 包本身；下一篇会讲它如何被装进 `preScript.py`、生成 `Data/*.txt` 整数文本，再被 VHDL 测试台读回逐位比对，形成完整的协同仿真闭环。建议接着读 [testbench/psi_fix_mov_avg_tb/Scripts/preScript.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/Scripts/preScript.py) 作为预习。
- **看一个完整组件模型**：精读 [model/psi_fix_mov_avg.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_mov_avg.py)，观察它如何用本讲的 API 串出差分-累加-增益校正流水，并对照 VHDL 侧 `hdl/psi_fix_mov_avg.vhd` 验证两侧格式常量一致。
- **想深入精度陷阱**：重读 [doc/files/tips.md](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/tips.md) 的 *Precision of intermediate results* 与 *Built-in Python Functions* 两节，理解为何位真模型常常要避免直接用 `np.cumsum / scipy.signal.lfilter` 的全精度内置函数。
