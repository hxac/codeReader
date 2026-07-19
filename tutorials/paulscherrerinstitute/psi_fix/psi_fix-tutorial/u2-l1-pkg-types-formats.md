# psi_fix_pkg 类型与格式定义

## 1. 本讲目标

本讲是进入 psi_fix 源码内部的第一讲。读完本讲，你应当能够：

1. 说出 `psi_fix_pkg` 里三个核心类型 `psi_fix_fmt_t`、`psi_fix_rnd_t`、`psi_fix_sat_t` 的字段与取值含义。
2. 解释 psi_fix 为什么不自己实现定点运算，而是通过 `psi_fix2_cl_fix` / `cl_fix2_psi_fix` 一组转换函数把工作「外包」给外部包 `en_cl_fix`，并能列出这组函数的全部重载。
3. 说清楚 `psi_fix_round → NonSymPos_s`、`psi_fix_sat → Sat_s` 这两处关键映射背后的语义，以及 `psi_fix_size` / `psi_fix_from_real` / `psi_fix_get_bits_as_int` 等基础查询函数的作用。

本讲只聚焦「类型定义 + 转换桥 + 基础查询函数」这三个最小模块。真正的运算函数（add/sub/mult/resize/shift 等）留到下一讲 u2-l2。

## 2. 前置知识

在进入源码前，先建立三个直觉。如果你已学完 u1-l4，下面的内容是复习。

### 2.1 定点格式三元组 [s, i, f]

psi_fix 用一个三元组描述任意一个定点数：

- `s`：符号位个数，只能取 0（无符号）或 1（有符号）。
- `i`：整数位个数，可以为负（表示没有整数位、甚至整体被放大）。
- `f`：小数位个数，也可以为负。

总位宽为：

\[
W = s + i + f
\]

这个求和就是后面要讲的 `psi_fix_size` 函数。例如格式 `(1, 0, 17)` 表示「1 位符号、0 位整数、17 位小数」，总位宽 18，范围是 \([-1, +1-2^{-17}]\)。

### 2.2 舍入与饱和是两个独立的「量化」操作

当一个定点数要被放进另一个更窄的格式时，会有两种损失：

- **精度损失**：小数位变少，需要**舍入（round）**或**截断（trunc）**。
- **范围损失**：整数位变少（或符号位变化），可能溢出，需要**饱和（sat）**或**回绕（wrap）**。

psi_fix 把这两件事分别建模成两个枚举类型 `psi_fix_rnd_t` 与 `psi_fix_sat_t`。记住：它们是正交的，每个运算函数都同时接受一个 `rnd` 和一个 `sat` 参数。

### 2.3 psi_fix 的「壳 + 内核」分层

psi_fix 并不从零实现定点运算。真正做二进制舍入、饱和、位增长数学的是一个更底层的库 `en_cl_fix`（来自 Enclustra，是 psi_fix 的外部依赖之一，见 u1-l1）。psi_fix 的角色是一层「友好外壳」：

- 它提供了一套命名更简洁、默认值更省资源的 API（例如默认 `trunc`/`wrap`）。
- 它在每个运算函数内部，先把自己的类型「翻译」成 en_cl_fix 的类型，调用 en_cl_fix，再把结果带回来。

这个「翻译」就是本讲的核心机制：**转换桥**。理解了它，你就能看懂后续所有运算函数的函数体——它们几乎都是一行 `return cl_fix_xxx(psi_fix2_cl_fix(...), ...)`。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到哪里 |
|------|------|--------------|
| `hdl/psi_fix_pkg.vhd` | 定点包的 VHDL 声明与实现，是本讲的主战场 | 类型定义、转换桥、基础查询函数 |
| `model/psi_fix_pkg.py` | 与 VHDL 逐位一致的 Python 位真模型 | 对照说明转换桥在 Python 侧的镜像实现 |
| `unittest/psi_fix_pkg_test.py` | 针对 Python 位真模型的单元测试 | 给出 `size` / `from_real` 等函数的可运行断言 |
| `hdl/psi_fix_mov_avg.vhd` | 一个真实组件，作为类型/函数的「使用样例」 | 展示类型如何出现在 generic 与端口里 |
| `doc/files/psi_fix_pkg.md` | 文档占位（当前为空模板） | 说明该 md 由 `hdl2md` 自动生成，目前无内容 |

> 说明：`en_cl_fix_pkg`（提供 `FixFormat_t`、`FixRound_t`、`FixSaturate_t` 及 `cl_fix_*` 系列函数）是**外部依赖**，不在本仓库内，需要按 u1-l1 所述与 psi_fix 并排摆放。本讲不会编造它的内部实现，只依据 psi_fix 侧的转换代码推断其语义。

## 4. 核心概念与源码讲解

### 4.1 类型定义

#### 4.1.1 概念说明

`psi_fix_pkg` 在包头部一次性定义了全库通用的三类「描述符」：描述一个数长什么样的 **格式**、描述如何丢精度的 **舍入模式**、描述如何处理溢出的 **饱和模式**。所有组件的 generic、内部 constant、端口位宽，最终都由这几个类型推导出来。

先看包头的库引用，确认它确实依赖 en_cl_fix：

[hdl/psi_fix_pkg.vhd:15-17](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L15-L17) —— 这里 `use work.en_cl_fix_pkg.all;` 把外部定点内核引入作用域，后面所有 `FixFormat_t`、`cl_fix_*` 都来自这一行。

#### 4.1.2 核心流程

类型定义本身没有「流程」，但有一个清晰的组织层次：

```text
psi_fix_fmt_t        (record: s, i, f)   ← 描述一个数的格式
   └─ psi_fix_fmt_array_t (数组)          ← 一组格式，用于多输入/多通道
psi_fix_rnd_t        (枚举: round/trunc) ← 描述精度量化方式
psi_fix_sat_t        (枚举: wrap/sat)    ← 描述溢出处理方式
```

在真实组件里，它们通常这样出现：

1. 设计者在 `generic` 里用 `psi_fix_fmt_t` 声明输入/输出格式；
2. 架构体里用 `(s, i, f)` 字面量或基于输入格式做算术推导出若干 `constant ... : psi_fix_fmt_t`；
3. 端口位宽用 `psi_fix_size(fmt) - 1 downto 0` 计算。

#### 4.1.3 源码精读

三个核心类型集中在包头的开头：

[hdl/psi_fix_pkg.vhd:27-37](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L27-L37) —— 这里定义了：
- `psi_fix_fmt_t`：record，三个字段 `s`（限定 0..1）、`i`（integer，可负）、`f`（integer，可负）。注释直接写明 `Sign bit` / `Integer bits` / `Fractional bits`。
- `psi_fix_fmt_array_t`：`psi_fix_fmt_t` 的非约束数组，用来一次表达多个格式（例如 FIR 的多个系数字段）。
- `psi_fix_rnd_t`：枚举 `psi_fix_round` / `psi_fix_trunc`。
- `psi_fix_sat_t`：枚举 `psi_fix_wrap` / `psi_fix_sat`。

注意命名：psi_fix 自 4.0.0 起（见 u1-l1）统一采用 `snake_case`，类型名则保留 `psi_fix_` 前缀加 `_t` 后缀，枚举字面量也带 `psi_fix_` 前缀（`psi_fix_round` 而不是裸 `round`），这是为了在 `use` 多个包时避免与其他库的同名字面量冲突。

这些类型在真实组件里长什么样？看 `psi_fix_mov_avg` 的 generic 区：

[hdl/psi_fix_mov_avg.vhd:24-30](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L24-L30) —— `in_fmt_g : psi_fix_fmt_t` 和 `out_fmt_g : psi_fix_fmt_t` 让使用者在外面声明数据格式；`round_g : psi_fix_rnd_t := psi_fix_round`、`sat_g : psi_fix_sat_t := psi_fix_sat` 把舍入/饱和作为可配置项。注意组件自己给的**默认值是「舍入 + 饱和」**（更安全），而 pkg 里运算函数的默认值是「截断 + 回绕」（更省资源，下面 4.2 会讲）——两处默认值不同，是有意为之。

端口位宽则用 `psi_fix_size` 计算：

[hdl/psi_fix_mov_avg.vhd:36-38](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L36-L38) —— `dat_i` 的宽度是 `psi_fix_size(in_fmt_g) - 1 downto 0`。这就是「格式 → 位宽」的标准写法，全库统一。

架构体里还会基于输入格式算出中间格式（位增长）：

[hdl/psi_fix_mov_avg.vhd:50-53](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L50-L53) —— `DiffFmt_c` 的整数位是 `in_fmt_g.I + 1`（差分 = 减法，整数位 +1，正是 u1-l4 讲过的位增长规则），`SumFmt_c` 的整数位是 `in_fmt_g.I + AdditionalBits_c`（累加 `taps_g` 个样本，需要 \(\lceil\log_2 taps\rceil\) 位）。这就是「类型定义 + 位增长规则 + size」三者联动的典型样例。

#### 4.1.4 代码实践

**实践目标**：亲手验证「格式 → 位宽」的推导，并看一眼 Python 侧的同款类型。

**操作步骤**：

1. 打开 `model/psi_fix_pkg.py`，看 Python 如何镜像 VHDL 的类型。对照 [model/psi_fix_pkg.py:26-61](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py#L26-L61)：`psi_fix_fmt_t` 是个普通类，构造函数同样接收 `s, i, f`；`psi_fix_rnd_t` / `psi_fix_sat_t` 是 `Enum`，取值 `round/trunc`、`wrap/sat`。注意 Python 侧多了一条 53 位限制（见 4.3.2）。
2. 在仓库根目录运行已有的单元测试，观察 `psi_fix_size` 的断言：
   ```bash
   cd unittest && python3 psi_fix_pkg_test.py
   ```
   （需要先把 `en_cl_fix` 按目录结构摆好，否则 `from en_cl_fix_pkg import *` 会失败。）

**需要观察的现象**：测试里 `PsiFixSizeTest` 用 `psi_fix_size(psi_fix_fmt_t(1, 3, 3))` 期望得到 `7`，对应 \(1+3+3=7\)；`psi_fix_size(psi_fix_fmt_t(1, -2, 3))` 期望 `2`，对应 \(1+(-2)+3=2\)（负整数位也合法，总位宽仍为代数和）。见 [unittest/psi_fix_pkg_test.py:17-38](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/unittest/psi_fix_pkg_test.py#L17-L38)。

**预期结果**：负整数位/负小数位不会报错，总位宽始终等于 \(s+i+f\)。如果本机没有 Modelsim/GHDL 但装了 Python + en_cl_fix，这个测试是验证类型定义最快的方式。

**待本地验证**：若 `en_cl_fix` 未就位，`python3 psi_fix_pkg_test.py` 会抛 `ModuleNotFoundError`，这属于环境问题，不影响对源码的理解。

#### 4.1.5 小练习与答案

**练习 1**：格式 `(1, 0, 17)` 的总位宽是多少？它能表示 +1.0 吗？
> **答案**：位宽 \(1+0+17=18\)。它能表示 \(-1.0\)，但**不能**表示 \(+1.0\)（最大值是 \(1-2^{-17}\)，这是 u1-l4 讲过的有符号范围不对称性）。

**练习 2**：为什么 `psi_fix_mov_avg` 把 `round_g/sat_g` 默认设成 `psi_fix_round/psi_fix_sat`，而 pkg 里运算函数默认却是 `psi_fix_trunc/psi_fix_wrap`？
> **答案**：组件层面默认「安全」（舍入+饱和，避免用户忘记设导致精度/溢出问题）；库层面运算函数默认「省资源」（截断+回绕不做额外处理，综合出最少逻辑）。使用时谁负责量化，就把量化参数显式写在谁那里。

**练习 3**：`psi_fix_fmt_array_t` 解决什么问题？
> **答案**：当一次运算涉及多个格式（例如 FIR 要把一组系数格式、一组数据格式批量传给底层），用数组而不是逐个参数传递，既简化签名，也便于循环处理。本讲 4.2 会看到它的转换函数也支持数组。

---

### 4.2 en_cl_fix 转换桥

#### 4.2.1 概念说明

这是本讲最关键的设计。psi_fix 的作者**不重造定点运算的轮子**，而是复用 `en_cl_fix` 这个经过验证的内核。但两个库的类型系统不同：

| psi_fix 侧 | en_cl_fix 侧 | 关系 |
|------------|--------------|------|
| `psi_fix_fmt_t`（record: s, i, f） | `FixFormat_t`（含 Signed 布尔、Intbits、FracBits） | 格式表示法不同 |
| `psi_fix_rnd_t`（round/trunc 两值） | `FixRound_t`（多种舍入模式） | psi_fix 只用其中两种 |
| `psi_fix_sat_t`（wrap/sat 两值） | `FixSaturate_t`（多种饱和模式） | psi_fix 只用其中两种 |
| `psi_fix_fmt_array_t` | `FixFormatArray_t` | 数组版 |

转换桥就是一组**同名重载函数**：`psi_fix2_cl_fix` 把 psi_fix 的类型翻译成 en_cl_fix 的，`cl_fix2_psi_fix` 反过来。它们用 VHDL 的「按参数类型重载（overloading）」特性，让一个函数名同时处理 fmt/rnd/sat/数组四种参数。

> 为什么 psi_fix 只暴露 round/trunc 两种舍入、wrap/sat 两种饱和？因为这两种组合已经覆盖了 DSP 组件的绝大多数需求，简化了用户心智；en_cl_fix 内部其实支持更多模式（例如不同的对称/非对称舍入），但 psi_fix 选择不暴露，转换函数遇到不支持的模式会 `report ... severity error` 报错。

#### 4.2.2 核心流程

任意一个 psi_fix 运算函数的调用链都是这样：

```text
psi_fix_xxx(a, a_fmt, ..., rnd, sat)        ← 用户调用，用 psi_fix 类型
   │
   ├─ psi_fix2_cl_fix(a_fmt) → FixFormat_t  ← 把每个格式/模式翻译成 en_cl_fix
   ├─ psi_fix2_cl_fix(rnd)   → FixRound_t
   ├─ psi_fix2_cl_fix(sat)   → FixSaturate_t
   │
   └─ cl_fix_xxx( ... 全部 en_cl_fix 类型 ... )   ← 真正的定点数学在 en_cl_fix 里完成
            │
            └─ 返回 std_logic_vector（位模式，两边通用，无需再翻译）
```

关键点：**位模式（`std_logic_vector` / 整数）本身不需要翻译**——二进制就是二进制。需要翻译的只有「如何解释这些位」的格式描述符和量化模式。

#### 4.2.3 源码精读

转换桥的声明在包头，四个 `psi_fix2_cl_fix` 重载 + 四个 `cl_fix2_psi_fix` 重载：

[hdl/psi_fix_pkg.vhd:50-72](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L50-L72) —— 注意 VHDL 按参数类型区分重载：同名函数分别接收 `psi_fix_rnd_t`、`psi_fix_sat_t`、`psi_fix_fmt_t`、`psi_fix_fmt_array_t`，返回对应的 en_cl_fix 类型。

实现体里最值得逐行看的是两个枚举映射：

[hdl/psi_fix_pkg.vhd:257-277](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L257-L277) —— 两个 `case` 语句完成了核心映射：

- **舍入**：`psi_fix_round => NonSymPos_s`，`psi_fix_trunc => Trunc_s`。
  - `NonSymPos_s` = Non-Symmetric Positive，即「非对称、向正方向」的舍入，俗称「四舍五入半向上」：遇到正好 0.5 时向 \(+\infty\) 方向取整。它之所以「非对称」，是因为正数和负数在 0.5 处的行为不对称（这是 en_cl_fix 的命名约定，psi_fix 直接复用）。
  - `Trunc_s` = 截断，直接丢弃多余低位。
- **饱和**：`psi_fix_sat => Sat_s`，`psi_fix_wrap => None_s`。
  - `Sat_s` = 饱和到可表示范围。
  - `None_s` = 不做饱和处理，即回绕（溢出后绕回）。

`when others` 分支用 `report ... severity error` 兜底：如果将来 en_cl_fix 引入了 psi_fix 不打算支持的新模式，反向转换会直接报错而不是悄悄误译。

格式（fmt）的转换是纯字段搬运：

[hdl/psi_fix_pkg.vhd:279-283](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L279-L283) —— `psi_fix2_cl_fix(fmt)` 返回 `((fmt.S = 1), fmt.I, fmt.F)`：把 psi_fix 的整数 `S`（0/1）变成 en_cl_fix 的布尔 `Signed`，`I`/`F` 原样传递。反向 `cl_fix2_psi_fix` 用 `choose(fmt.Signed, 1, 0)` 把布尔变回 0/1，见 [hdl/psi_fix_pkg.vhd:317-321](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L317-L321)（`choose` 来自 `psi_common_logic_pkg`）。

数组版的转换就是一个 for 循环逐个翻译：

[hdl/psi_fix_pkg.vhd:285-293](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L285-L293) —— 对 `fmts'range` 里每个元素调用单元素版的 `psi_fix2_cl_fix`，结果存进一个等长的 `FixFormatArray_t`。

转换桥在 Python 侧有一份几乎一一对应的实现（名字采用 camelCase 的 `PsiFix2ClFix` / `ClFix2PsiFix`）：

[model/psi_fix_pkg.py:66-95](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py#L66-L95) —— Python 用 `type(arg)` 判断分支，映射关系与 VHDL 完全一致：`round → FixRound.NonSymPos_s`、`trunc → FixRound.Trunc_s`、`sat → FixSaturate.Sat_s`、`wrap → FixSaturate.None_s`。这种「VHDL 与 Python 两侧转换逻辑同构」正是位真模型的基石——只要两边的翻译表一致，VHDL 综合出的硬件行为就能和 Python 模型逐位对上。

#### 4.2.4 代码实践

**实践目标**：亲手列出转换桥的全部重载，并解释两处关键映射。

**操作步骤**：

1. 在 `hdl/psi_fix_pkg.vhd` 里搜索 `function psi_fix2_cl_fix` 和 `function cl_fix2_psi_fix`，分别数它们的重载数量与参数类型。
2. 打开 [hdl/psi_fix_pkg.vhd:257-315](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L257-L315)，对照下面的「映射表」逐行核对。

**需要观察的现象 / 预期结果**（这是本讲要求的实践产出）：

`psi_fix2_cl_fix` 共 **4 个重载**：

| 参数类型 | 返回类型 | 关键映射 |
|----------|----------|----------|
| `psi_fix_rnd_t` | `FixRound_t` | `round → NonSymPos_s`，`trunc → Trunc_s` |
| `psi_fix_sat_t` | `FixSaturate_t` | `sat → Sat_s`，`wrap → None_s` |
| `psi_fix_fmt_t` | `FixFormat_t` | `S=1 → Signed=true`，`I`/`F` 直传 |
| `psi_fix_fmt_array_t` | `FixFormatArray_t` | 逐元素调用上面的 fmt 重载 |

`cl_fix2_psi_fix` 同样 **4 个重载**，方向反过来。`round → NonSymPos_s` 的含义是「半值向 \(+\infty\) 舍入的非对称舍入」；`sat → Sat_s` 的含义是「溢出时钳位到最近的可表示值，而不是回绕」。

3. （可选）对照 Python 侧 [model/psi_fix_pkg.py:66-95](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py#L66-L95)，确认两张映射表完全一致。

**预期结果**：VHDL 与 Python 两边的转换表逐项相同，这正是「位真」能成立的前提。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `psi_fix_wrap` 映射到 `None_s`（字面意思是「不做饱和」），而不是一个叫 `Wrap_s` 的模式？
> **答案**：「回绕」本质上就是「不做任何溢出处理」——让二进制自然截断高位。所以 en_cl_fix 用 `None_s`（不饱和）表示回绕，psi_fix 用更直白的名字 `psi_fix_wrap` 对应它。两者描述的是同一种行为。

**练习 2**：如果一个 psi_fix 运算函数被传入了 en_cl_fix 支持但 psi_fix 未暴露的舍入模式（经反向转换），会发生什么？
> **答案**：`cl_fix2_psi_fix(rnd)` 的 `when others` 分支会 `report "Unsupported Rounding Mode" severity error`，并返回 `psi_fix_trunc` 作为兜底。即「显式报错 + 安全默认」，避免静默误译。

**练习 3**：为什么 `psi_fix_neg` 的函数体（4.3 会看到）里出现 `'1'` 这样的 en_cl_fix 风格参数，而 `psi_fix_add` 没有？
> **答案**：这是 en_cl_fix 不同函数签名差异决定的（例如 `cl_fix_neg` 多一个符号控制位）。本讲只需记住：psi_fix 把 en_cl_fix 的复杂签名封装成了统一风格，差异都藏在函数体内部。

---

### 4.3 基础查询函数

#### 4.3.1 概念说明

除了「翻译」，psi_fix 还提供一组「查询/构造」函数，它们是写组件时最常用的工具：

- `psi_fix_size(fmt)`：格式 → 总位宽。
- `psi_fix_from_real(real, fmt)`：浮点常数 → 定点位模式（常用于在 generic/constant 里把 `2.0**n` 这种实数固化成定点系数）。
- `psi_fix_from_bits_as_int(int, fmt)` / `psi_fix_get_bits_as_int(slv, fmt)`：整数位模式与定点值之间的互转，是协同仿真把结果写成文本的核心（见 u3-l2）。

它们在包头里和运算函数放在一起，但本讲只看这组「基础」的（运算函数留给 u2-l2）。

#### 4.3.2 核心流程

这组函数的共同特点是「瘦封装」：每个函数体都只有一两行，核心逻辑全部委托给 en_cl_fix，前面套一层 `psi_fix2_cl_fix` 翻译。以 `psi_fix_size` 为例：

```text
psi_fix_size(fmt)
  → cl_fix_width( psi_fix2_cl_fix(fmt) )   ← 翻译格式，再调 en_cl_fix 求宽度
  → 返回整数
```

位宽公式就是第 2.1 节的 \(W = s + i + f\)，但 psi_fix 不自己写这个加法，而是交给 en_cl_fix 的 `cl_fix_width`，保证「格式 → 位宽」的规则与运算侧完全同源。

> **Python 侧的 53 位限制**：Python 用 IEEE 754 双精度实现位真模型，尾数只有 53 位，因此 `psi_fix_fmt_t` 在 Python 侧构造时，若 `psi_fix_size(self) > 53` 会抛 `BittruenessNotGuaranteed`（见 [model/psi_fix_pkg.py:34-35](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py#L34-L35)）。VHDL 侧没有这个限制。这是位真模型的已知边界，u2-l3 会详细讲。

#### 4.3.3 源码精读

先看 `psi_fix_size`：

[hdl/psi_fix_pkg.vhd:337-341](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L337-L341) —— 一行 `return cl_fix_width(psi_fix2_cl_fix(fmt))`，就是转换桥 + en_cl_fix 的标准组合拳。

再看 `psi_fix_from_real`，它是少数几个 psi_fix **加了自有逻辑**的函数：

[hdl/psi_fix_pkg.vhd:344-352](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L344-L352) —— 这里多了一条 `assert`：如果是无符号格式（`r_fmt.S = 1` 为假）却传入了负数，就报错。这是 psi_fix 在 en_cl_fix 之上增加的「防呆」检查。注意它与 Python 侧的区别：VHDL 侧用 `assert` 报告，Python 侧 `psi_fix_from_real` 多了一个 `err_sat` 参数（见 [model/psi_fix_pkg.py:104-113](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py#L104-L113)），可以关掉范围检查——这是 Python 做大规模随机仿真时需要的灵活度。

`psi_fix_from_real` 在真实组件里用来把数学常数固化成定点：

[hdl/psi_fix_mov_avg.vhd:56](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L56) —— 增益校正系数 `Gc_c := psi_fix_from_real(2.0**real(AdditionalBits_c) / real(Gain_c), GcCoefFmt_c)`：把一个实数除法结果直接编译期转换成 `GcCoefFmt_c` 格式的位模式常量。这是 `from_real` 最典型的用法——在综合期就把系数「烤」进硬件。

再看整数互转的两个函数，它们是协同仿真的命脉：

[hdl/psi_fix_pkg.vhd:364-377](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L364-L377) —— `psi_fix_from_bits_as_int` 把一个整数（按二进制补码解释）展开成指定位宽的 `std_logic_vector`；`psi_fix_get_bits_as_int` 反过来把位向量压缩成一个整数。为什么要有它们？因为测试台之间的数据交换媒介是**整数文本文件**（见 u1-l3、u3-l2）：Python 模型算出浮点结果 → 用 `get_bits_as_int` 转成整数 → 写进 `Data/*.txt` → VHDL 测试台读回 → 用 `from_bits_as_int` 重建位向量喂给 DUT。整数是「跨语言、可读、可逐位比对」的最小公约数。

Python 侧的对应实现完全同构：

[model/psi_fix_pkg.py:101-119](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py#L101-L119) —— `psi_fix_size` / `psi_fix_from_bits_as_int` / `psi_fix_get_bits_as_int` 都是「先 `PsiFix2ClFix` 翻译，再调 `cl_fix_*`」。

单元测试对 `get_bits_as_int` 给了清晰的断言：

[unittest/psi_fix_pkg_test.py:81-90](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/unittest/psi_fix_pkg_test.py#L81-L90) —— 例如 `psi_fix_get_bits_as_int(-1.5, (1,2,1))` 期望得到 `-3`：因为 \(-1.5\) 在 `(1,2,1)`（1 符号+2 整数+1 小数）下的定点编码是二进制补码 `11101`... 经量化后整数表示为 \(-3\)。这条断言正好说明「值 ↔ 整数位模式」的转换是有符号补码语义。

> 小贴士：包里还有一个 `psi_fix_choose_fmt(sel, fmt_a, fmt_b)` 辅助函数（声明 [hdl/psi_fix_pkg.vhd:42-45](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L42-L45)，实现 [hdl/psi_fix_pkg.vhd:212-222](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L212-L222)），用来在编译期根据布尔条件选格式，常用于 generic 条件化设计。

#### 4.3.4 代码实践

**实践目标**：跟踪一个浮点数如何经过 `from_real` 与 `get_bits_as_int` 在「值」与「整数位模式」之间往返。

**操作步骤**：

1. 读 [unittest/psi_fix_pkg_test.py:40-62](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/unittest/psi_fix_pkg_test.py#L40-L62) 的 `PsiFixFromRealTest`：注意 `psi_fix_from_real(1.2, (0,2,2))` 期望 `1.25`——`1.2` 无法被 2 位小数精确表示，最近的格点是 `1.25`，所以「四舍五入」到了 `1.25`。
2. 读 [unittest/psi_fix_pkg_test.py:64-90](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/unittest/psi_fix_pkg_test.py#L64-L90)：`from_bits_as_int(3, (1,2,1))` 得到 `1.5`，`get_bits_as_int(1.5, (1,2,1))` 得到 `3`，两者互逆。
3. 在仓库根目录尝试运行（环境就绪时）：
   ```bash
   cd unittest && python3 psi_fix_pkg_test.py -v 2>&1 | head -40
   ```

**需要观察的现象**：`from_real` 的 `test_OutOfRangeError` 用例表明，传一个超出格式范围的数（如 `4.2` 进 `(0,2,2)`，范围 \([0, 3.75]\)）会抛 `ValueError`——这是 Python 侧的范围检查在起作用。

**预期结果**：`from_bits_as_int` 与 `get_bits_as_int` 互为逆运算；`from_real` 会做最近舍入并做范围检查。

**待本地验证**：若无 `en_cl_fix`，第 3 步会因导入失败而无法运行；可改为纯源码阅读完成第 1、2 步。

#### 4.3.5 小练习与答案

**练习 1**：`psi_fix_size` 为什么不直接写 `return fmt.S + fmt.I + fmt.F`，而要绕一圈调 `cl_fix_width`？
> **答案**：为了保证「格式 → 位宽」的规则与 en_cl_fix 运算侧**同源**。如果 psi_fix 自己写加法、en_cl_fix 内部另有规则，两边可能出现细微不一致；统一委托给 en_cl_fix 就消除了这种风险。这也是「壳 + 内核」分层的核心收益。

**练习 2**：`psi_fix_from_real` 的 VHDL 版用 `assert`，Python 版却多了一个 `err_sat` 参数。为什么 Python 需要这个参数？
> **答案**：Python 模型常用于大规模随机仿真，有时需要故意灌入超出范围的刺激来测试饱和行为（期望它被钳位而不是报错）。`err_sat=False` 关掉范围检查，就能让 `cl_fix_from_real` 用饱和方式处理越界值，便于构造「最坏情况」刺激（见 u3-l1 的刺激设计）。

**练习 3**：为什么协同仿真用「整数」作为 Python 与 VHDL 之间的文本交换格式，而不是直接写浮点数？
> **答案**：浮点数无法逐位精确再现（存在打印精度问题），而整数是精确的。用 `get_bits_as_int` 把定点值映射成其底层的整数补码表示，再以整数文本传输，就能做到真正的逐位比对——这正是 psi_fix 位真验证的底座。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「源码阅读 + 推导」任务（无需仿真器即可完成）：

**任务**：仿照 `psi_fix_mov_avg`，为一个假想的「定点常数增益」组件（输出 = 输入 × 常数增益）推导它的类型与位宽。

**要求**：

1. 在 [hdl/psi_fix_mov_avg.vhd:24-56](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L24-L56) 中找到：generic 如何用 `psi_fix_fmt_t` 声明、端口如何用 `psi_fix_size` 求宽、constant 如何用 `psi_fix_from_real` 固化系数——记录这三处写法当模板。
2. 假设输入格式 `in_fmt_g = (1, 0, 17)`，增益系数 `0.123` 用格式 `(0, 1, 16)` 存放。请：
   - 用 `psi_fix_from_real(0.123, (0,1,16))` 这种写法表达系数常量（参照 [hdl/psi_fix_mov_avg.vhd:56](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd#L56)）。
   - 依据 u1-l4 的乘法位增长规则，推导 `(1,0,17) × (0,1,16)` 的全精度结果格式（提示：有符号 × 无符号，结果应取有符号；整数位与乘法相关）。
   - 写出输出端口位宽的表达式 `psi_fix_size(out_fmt_g) - 1 downto 0`。
3. 解释：在乘法之后做一次 `psi_fix_resize(..., rnd=>psi_fix_round, sat=>psi_fix_sat)` 时，这两个枚举值会经过 [hdl/psi_fix_pkg.vhd:257-277](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pkg.vhd#L257-L277) 的转换桥，分别变成 en_cl_fix 的 `NonSymPos_s` 与 `Sat_s`，并被传给 `cl_fix_resize`。

**预期产出**：一段不超过 15 行的「伪 VHDL」（generic + 端口 + 系数 constant + 一行乘法+resize），并附上每处类型/位宽/转换的推导依据（引用本讲给出的行号）。

**待本地验证**：乘法结果格式的具体整数位推导若你拿不准，可标注「待本地验证」，先写出你的推导思路（这正是 u2-l2 会用 `psi_fix_mult` 的函数体印证的内容）。

## 6. 本讲小结

- `psi_fix_pkg` 用三个类型统一描述全库的定点世界：`psi_fix_fmt_t`（格式 record）、`psi_fix_rnd_t`（舍入枚举）、`psi_fix_sat_t`（饱和枚举），外加数组版 `psi_fix_fmt_array_t`。
- psi_fix 不自己实现定点运算，而是通过 `psi_fix2_cl_fix` / `cl_fix2_psi_fix` 两族共 8 个重载函数，把类型翻译给外部内核 `en_cl_fix`，再由 `cl_fix_*` 完成真正的数学。
- 两处关键映射：`psi_fix_round → NonSymPos_s`（半值向 \(+\infty\) 的非对称舍入）、`psi_fix_sat → Sat_s`（饱和）；`psi_fix_wrap → None_s`（不饱和即回绕）。
- 基础查询函数 `psi_fix_size` / `psi_fix_from_real` / `psi_fix_from_bits_as_int` / `psi_fix_get_bits_as_int` 都是「转换桥 + en_cl_fix」的瘦封装，`from_real` 额外加了无符号负数防呆。
- VHDL 与 Python 两侧的转换表与 API 同构，这是「位真双模型」能逐位对齐的前提；Python 侧额外有 53 位精度边界。
- 组件层默认值（round/sat，偏安全）与库运算函数默认值（trunc/wrap，偏省资源）不同，使用时需显式声明量化策略。

## 7. 下一步学习建议

- **下一讲 u2-l2《定点运算函数》**：本讲只看了「壳」，下一讲进入 `psi_fix_resize/add/sub/mult/abs/neg/shift_left/shift_right/compare/in_range/upper_bound` 等组合可综合运算函数的实现，重点看它们如何利用转换桥、以及 shift 函数为 Vivado 动态移位可综合性所做的特殊实现。
- **u2-l3《Python 位真模型包》**：深入 `model/psi_fix_pkg.py`，理解 53 位精度限制、`enable_range_check` / `with_range_check_disabled` 等只在 Python 侧存在的机制。
- **延伸阅读**：复习 u1-l4 的位增长规则（本讲 4.1.3、第 5 节都直接用到），它能让你在看任何组件的 constant 格式推导时不再犯迷糊。若想看一个完整组件如何把本讲的类型、转换桥、查询函数三者串成端到端流水，可直接读 `hdl/psi_fix_mov_avg.vhd` 全文，它是 u4-l1 的主题。
