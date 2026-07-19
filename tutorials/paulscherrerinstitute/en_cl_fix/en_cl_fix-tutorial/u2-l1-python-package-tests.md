# Python 包结构与单元测试

## 1. 本讲目标

前几讲我们建立了定点格式 `[S,I,F]`、舍入 `FixRound`、饱和 `FixSaturate` 的概念基础，并且反复提到「Python/MATLAB/VHDL 三语言位真一致」。从本讲开始，我们真正进入**某种具体语言**的实现内部，看清楚这些概念在代码里到底长什么样、怎么跑起来。

本讲聚焦 Python 实现，学完后你应当能够：

- 说清 `python/src/en_cl_fix_pkg` 这个包由哪几个模块组成，以及 `__init__.py` 如何用一个统一门面（facade）把它们对外暴露。
- 理解 Python 实现对 `numpy` 的依赖，以及函数如何对整个数组做**向量化**运算，而不是逐元素循环。
- 认识两个 **Python 独有、VHDL 中不存在**的仿真辅助函数 `cl_fix_random` 与 `cl_fix_write_formats`，理解它们在协同仿真中的用途。
- 在 `python/unittest` 目录里运行 `en_cl_fix_pkg_test.py`，看懂「按被测函数分组」的 `unittest.TestCase` 组织方式，并能仿照它新增一个测试方法。

本讲不深入某个定点函数的算法细节（那是 Unit 3、Unit 4 的事），只关心 Python 这一实现的**工程骨架**与**测试方法学**。

## 2. 前置知识

阅读本讲前，你需要具备：

- **Python 基础**：会写 `import`、`def`、`class`，知道「包（package）= 含 `__init__.py` 的目录」「模块（module）= 一个 `.py` 文件」。
- **numpy 最少概念**：知道 `numpy.ndarray`（下称 ndarray）是一个可以装很多同类型数值的数组，对它做 `a + b`、`np.floor(a)` 会**逐元素**作用在每一个数上。本讲你只需要这一个直觉。
- **标准库 `unittest` 最少概念**：知道「写一个继承 `unittest.TestCase` 的类，里面每个 `test_` 开头的方法就是一个测试用例，用 `self.assertEqual(期望, 实际)` 做断言」。本讲会顺带复习。
- 本手册 **u1-l2 / u1-l3** 引入的 `FixFormat(Signed, IntBits, FracBits)` 与 `cl_fix_width` 的位宽公式 \( W = S + I + F \)；以及 **u1-l4 / u1-l5** 的 `FixRound`、`FixSaturate` 枚举。

一个关键术语来自 u1-l1：**位真（bit-true）**——三套实现必须对同一输入产生逐位相同的输出。要做到这点，三套实现首先要共享同一套**类型语义**。本讲就来看看 Python 是如何把这套语义组织成代码的。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [python/src/en_cl_fix_pkg/\_\_init\_\_.py](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/__init__.py) | 包的入口，仅 3 行，用星号导入把三个子模块对外统一暴露。 |
| [python/src/en_cl_fix_pkg/en_cl_fix_types.py](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_types.py) | 类型定义：`FixFormat` 类（含 `width()` 及 `ForAdd/ForSub/...` 静态方法）、`FixRound`/`FixSaturate` 枚举。 |
| [python/src/en_cl_fix_pkg/en_cl_fix_pkg.py](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py) | 主体函数库：`cl_fix_width`、`cl_fix_from_real`、`cl_fix_resize`、加减乘、以及本讲重点的 `cl_fix_random` 与 `cl_fix_write_formats`。 |
| [python/src/en_cl_fix_pkg/wide_fxp.py](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py) | 大位宽（>53 位）任意精度实现。本讲只需知道它的存在，详解见 Unit 6。 |
| [python/unittest/en_cl_fix_pkg_test.py](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/unittest/en_cl_fix_pkg_test.py) | 唯一的 Python 单元测试文件，按被测函数分成若干 `TestCase` 类。 |
| [README.md](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/README.md) | 第 26 行声明 numpy 依赖；第 30–32 行给出 Python 测试运行方式。 |

## 4. 核心概念与源码讲解

### 4.1 包结构：三模块与 `__init__.py` 统一门面

#### 4.1.1 概念说明

Python 端没有像 VHDL 那样把所有东西塞进一个 `en_cl_fix_pkg.vhd` 巨型文件，而是把实现**按职责拆成三个模块**，再放进一个同名**包目录**里：

```
python/src/en_cl_fix_pkg/        ← 包（目录）
├── __init__.py                  ← 包入口（门面）
├── en_cl_fix_types.py           ← 纯类型定义
├── en_cl_fix_pkg.py             ← 主体函数库
└── wide_fxp.py                  ← 大位宽任意精度实现
```

这种「拆模块 + 门面统一导出」的好处是：内部各司其职（类型归类型、算法归算法、大位宽归大位宽），而**外部使用者只需要一行** `from en_cl_fix_pkg import *`，就能同时拿到 `FixFormat`、`cl_fix_width`、`cl_fix_from_real` 等所有名字，不必关心它们究竟定义在哪个子模块里。

> 注意命名上的小陷阱：**包**（目录）和**其中一个模块**（`en_cl_fix_pkg.py`）同名。这正是门面模式的用意——对使用者而言，「包」和「主体库」是同一个对外身份。

#### 4.1.2 核心流程

门面靠三行星号导入实现：

1. `from .en_cl_fix_types import *` —— 把 `FixFormat`、`FixRound`、`FixSaturate` 等类型名字纳入包命名空间。
2. `from .wide_fxp import *` —— 把 `wide_fxp` 类纳入。
3. `from .en_cl_fix_pkg import *` —— 把所有 `cl_fix_*` 函数纳入。

因为 `en_cl_fix_pkg.py` 内部又 `from .en_cl_fix_types import *`、`from .wide_fxp import wide_fxp`，所以这三个子模块的公开名字最终都汇聚到包顶层，构成一个扁平的对外接口。

#### 4.1.3 源码精读

整个门面只有三行：

[\_\_init\_\_.py:1-3](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/__init__.py#L1-L3) —— 包入口按「类型 → 大位宽 → 主体函数」顺序星号导入，把三个子模块的公开名字合并到包命名空间。

`en_cl_fix_pkg.py` 顶部的导入则说明了主体函数依赖哪些子模块：

[en_cl_fix_pkg.py:14-15](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L14-L15) —— 主体库引入类型定义（拿到 `FixFormat` 等）与 `wide_fxp` 类（用于大位宽派发）。

而 `en_cl_fix_types.py` 里的 `FixFormat` 类正是 u1-l2 所讲 `[S,I,F]` 三元组的 Python 落地，其中 `width()` 直接实现位宽公式：

[en_cl_fix_types.py:55-56](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_types.py#L55-L56) —— `width()` 返回 `int(Signed) + IntBits + FracBits`，即 \( W = S + I + F \)，正负位一视同仁地相加（承接 u1-l3）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「一行导入即可拿到全部名字」的门面效果。

**操作步骤**（在仓库根目录执行）：

```bash
cd python/src
python3 -c "from en_cl_fix_pkg import *; print(FixFormat(True,3,2)); print(cl_fix_width(FixFormat(True,3,2)))"
```

**需要观察的现象**：第一条打印出 `(True, 3, 2)`（`FixFormat.__str__` 的输出），第二条打印出 `5`（= 1+3+2）。

**预期结果**：尽管 `FixFormat` 定义在 `en_cl_fix_types.py`、`cl_fix_width` 定义在 `en_cl_fix_pkg.py`，它们都能通过包顶层 `from en_cl_fix_pkg import *` 直接使用。若报 `ModuleNotFoundError`，请确认当前在 `python/src` 目录（包目录的**父目录**才能让 `en_cl_fix_pkg` 作为包被解析）。运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果不写 `__init__.py`，`en_cl_fix_pkg` 还能被当作包导入吗？

**答案**：在 Python 3.3+ 中存在「命名空间包（namespace package）」，即使没有 `__init__.py` 也能导入目录，但本项目的门面逻辑（三行统一星号导入）必须写在 `__init__.py` 里才会执行。删掉它之后 `from en_cl_fix_pkg import *` 将拿不到任何 `cl_fix_*` 函数，门面失效。

**练习 2**：为什么门面里 `en_cl_fix_types` 要最先导入？

**答案**：因为 `en_cl_fix_pkg.py` 与 `wide_fxp.py` 都依赖 `FixFormat` 等类型。先导入类型可以保证后续模块在被导入（即被执行顶层代码）时，这些类型名字已经可用；同时也是清晰的依赖顺序：类型 → 大位宽 → 主体算法。

---

### 4.2 numpy 依赖与向量化运算

#### 4.2.1 概念说明

README 明确写道：**Python 实现依赖 `numpy` 包**。这不只是「用它做点数学」，而是整套函数都**按数组设计**：你传一个标量也行，传一个含一万个数的 ndarray 也行，函数会在**一条调用里同时对所有元素**完成量化、舍入、饱和。这就是 numpy 的「向量化（vectorization）」——用 C 层的逐元素循环代替 Python 层的显式 `for`，既快又简洁。

这一点是 Python 实现区别于 VHDL/MATLAB 的工程特征：VHDL 操作的是硬件信号，MATLAB 天然按矩阵运算，而 Python 选择把 numpy ndarray 作为统一的「数据容器」，使得算法评估可以一次性处理整段波形。

#### 4.2.2 核心流程

向量化靠 numpy 的「逐元素（element-wise）ufunc」实现。几个在本库中反复出现的模式：

- `np.floor(a)`：对 `a` 中每个元素向下取整。
- `np.where(cond, x, y)`：对每个元素，条件为真取 `x`、为假取 `y`——这是「按元素做选择」的核心工具，用于饱和夹紧、符号判断等。
- `np.max(a)` / `np.min(a)`：对整个数组求最大/最小，用于越界告警。

因此库里的函数几乎从不写 `for i in range(len(a))`，而是用一个 `np.where` 表达式同时处理整段数据。

#### 4.2.3 源码精读

主体库顶部一次性引入 numpy 及两个标准库：

[en_cl_fix_pkg.py:10-12](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L10-L12) —— `import numpy as np`（数值与向量化）、`import warnings`（饱和告警，承接 u1-l5）、`import random`（供 `cl_fix_random` 用）。

`cl_fix_from_real` 是展示向量化的最佳样本——它用 `np.max/np.min` 检查整段数据是否越界，用 `np.floor` 一次性量化所有元素，用 `np.where` 一次性夹紧：

[en_cl_fix_pkg.py:156-164](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L156-L164) —— 先 `np.max/np.min` 扫描整段数据决定是否告警，再用一行 `np.floor(...) ` 对**整个数组**做 half-up 量化（承接 u1-l5：`cl_fix_from_real` 的量化恒为 half-up，不受 `FixRound` 控制）。

[en_cl_fix_pkg.py:167-169](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L167-L169) —— 饱和分支用两个 `np.where` 把越界元素夹紧到 `fmtMax/fmtMin`，整段数据一次完成。

符号提取函数更简洁，整段逻辑就是一个 `np.where`：

[en_cl_fix_pkg.py:58-62](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L58-L62) —— `np.where(a < 0, 1, 0)` 对每个元素判断是否为负，返回与 `a` 同形状的 0/1 数组。

#### 4.2.4 代码实践

**实践目标**：直观感受「一次调用处理整个数组」。

**操作步骤**：

```bash
cd python/src
python3 -c "
from en_cl_fix_pkg import *
import numpy as np
a = np.array([-0.52, 1.2, 4.2, 0.5])          # 一段含越界值的波形
r = cl_fix_from_real(a, FixFormat(False, 2, 2), FixSaturate.Sat_s)
print(r)
"
```

**需要观察的现象**：四组输入**在同一次调用里**各自得到结果——负值与超上限的 4.2 被 `Sat_s` 夹紧（4.2 → 3.75），符合 u1-l5 讲过的饱和行为。

**预期结果**：类似 `[0.   1.25 3.75 0.5 ]`（`-0.52` 夹紧到无符号下界 0，`1.2` half-up 量化到 1.25，`4.2` 饱和到 3.75，`0.5` 不变）。把 `FixSaturate.Sat_s` 换成 `FixSaturate.SatWarn_s` 还会额外打印一条 `Warning`。运行结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：若把 `a` 从 ndarray 换成普通 Python list（如 `[1.2, 4.2]`），上面的 `cl_fix_from_real` 还能工作吗？

**答案**：多数情况下能。因为 `np.max`、`np.floor`、`np.where` 都接受 list 并在内部转成 ndarray。但返回值会变成 ndarray 而非 list，且某些分支（如 `cl_fix_resize` 的 narrow 分支显式 `np.array(a)`）会主动转换。库的契约是「返回 ndarray」，所以调用方应按 ndarray 处理返回值。

**练习 2**：为什么告警检查用 `np.max(a)`/`np.min(a)` 而不是逐元素比较后聚合？

**答案**：一次 `np.max` 扫描就能判断「整段数据里是否存在任何越界」，这比逐元素 `for` 循环再 `any()` 简洁且快得多，正是向量化的收益。

---

### 4.3 仿真辅助函数 `cl_fix_random`（仅 Python 提供）

#### 4.3.1 概念说明

做协同仿真时，常常需要一段**均匀覆盖某定点格式整个动态范围**的随机数据来喂给算法模型。VHDL 端没有提供这样的工具（硬件 testbench 通常用确定性激励），但 Python 端作为「高层评估语言」，额外提供了 `cl_fix_random` 来生成这种随机数据。源码注释明确把它归类为 **Simulation utility functions (not available in VHDL)**——这是 Python 实现的功能超集的一部分。

#### 4.3.2 核心流程

`cl_fix_random(n, fmt)` 生成 `n` 个落在 `[min_value(fmt), max_value(fmt)]` 内、且对齐到该格式网格点上的随机定点数：

1. 取该格式的实数下界 `fmtLo = cl_fix_min_value(fmt)` 与上界 `fmtHi = cl_fix_max_value(fmt)`。
2. 把它们换算成**整数网格坐标**：`intLo = fmtLo * 2^F`、`intHi = fmtHi * 2^F`（乘以 \( 2^F \) 就把小数点移到最右，每个网格点对应一个整数）。
3. 用 `np.random.randint(intLo, intHi+1, ...)` 在**整数区间**里均匀取 `n` 个整数（上界 +1 是因为 `randint` 区间左闭右开）。
4. 再除以 \( 2^F \) 还原成定点实数值。

关键直觉：定点格式的可表示值就是「整数 ÷ \( 2^F \)」，所以在整数坐标上等概率取样，等价于在该格式的**全部可表示网格点**上均匀取样。

#### 4.3.3 源码精读

注释把这类函数显式标注为 VHDL 不提供：

[en_cl_fix_pkg.py:459-461](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L459-L461) —— 段落注释「Simulation utility functions (not available in VHDL)」，说明下面这些工具是 Python 仿真专用的附加功能。

`cl_fix_random` 的 narrow（双精度浮点）分支即上述四步流程：

[en_cl_fix_pkg.py:463-477](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L463-L477) —— 取格式上下界，换算到整数网格，`np.random.randint` 在闭区间 `[intLo, intHi]` 取样，再除以 \( 2^{FracBits} \) 还原。`cl_fix_is_wide(fmt)` 为真时（位宽 > 53）改走大整数 `random.randrange` 路径（详解见 Unit 6）。

#### 4.3.4 代码实践

**实践目标**：生成覆盖整个动态范围的随机定点数据，并验证它确实落在格式范围内。

**操作步骤**：

```bash
cd python/src
python3 -c "
from en_cl_fix_pkg import *
r = cl_fix_random(10, FixFormat(False, 4, 0))
print('samples:', r)
print('min/max:', cl_fix_min_value(FixFormat(False,4,0)), cl_fix_max_value(FixFormat(False,4,0)))
"
```

**需要观察的现象**：10 个样本都是 `[0, 15]` 内的整数（`(false,4,0)` 的网格点就是 0–15 这些整数）；多次运行，样本会变化但始终落在 `[0, 15]` 内。

**预期结果**：每次输出 10 个 0–15 的整数，且都不超出 `cl_fix_min_value`/`cl_fix_max_value` 给出的边界。把格式换成 `FixFormat(True,2,2)` 后，样本应落在 `[-4, 3.75]` 且步长为 0.25。运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `randint` 的上界要写成 `intHi+1`？

**答案**：`np.random.randint(low, high)` 是**左闭右开**区间 `[low, high)`，不含 `high`。而格式的最大值 `fmtHi`（如 `(false,4,0)` 的 15）是一个合法的可表示点，应当能被取到，所以上界传 `intHi+1` 使区间变成闭区间 `[intLo, intHi]`。

**练习 2**：如果 `FracBits` 是负数（如 `(true,4,-2)`，承接 u1-l2 的负小数位），这套整数坐标取样还成立吗？

**答案**：仍然成立。负 `FracBits` 只意味着网格间距是 \( 2^{|F|} \) 的倍数（更粗粒度）；`intLo/intHi` 仍由实数边界乘以 \( 2^F \)（此时 \( 2^F < 1 \)）得到整数坐标，取样后再除以 \( 2^F \) 还原，数学上完全一致。

---

### 4.4 文件 IO 与 `cl_fix_write_formats`

#### 4.4.1 概念说明

位真协同仿真的另一个常见需求是**跨语言交换数据**：在 Python 里算好一段定点数据，写到文件，再让 VHDL testbench 读进来逐位比对（或反之）。要正确读取，首先得让读方知道数据的定点格式。`cl_fix_write_formats` 就是用来把**一个或多个格式定义**写成一个文本文件的辅助函数——它写出的是「格式清单」，配合后续（Unit 5 详讲）的 `cl_fix_write_int/real/bin/hex` 数据写函数，构成完整的数据交换链路。

和 `cl_fix_random` 一样，文件 IO 这一组函数也属于 Python 仿真侧的附加能力。

#### 4.4.2 核心流程

`cl_fix_write_formats(fmts, names, filename)` 的写出格式很简单：

1. 打开目标文件（覆盖写）。
2. 第一行写**注释头**：`# ` 后跟用逗号连接的 `names`（每个格式的名字）。
3. 之后**每个格式占一行**，调用 `cl_fix_string_from_format(fmt)`（u1-l3 讲过的序列化函数）得到 `(Signed, IntBits, FracBits)` 字符串。

输入 `fmts` 既可以是单个标量格式，也可以是数组；标量会被提升成一维，保证循环统一处理。

#### 4.4.3 源码精读

文件 IO 段落以段落注释分隔：

[en_cl_fix_pkg.py:441-443](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L441-L443) —— 段落标题「File I/O」，标识下面是文件读写相关函数。

`cl_fix_write_formats` 实现即上述三步：

[en_cl_fix_pkg.py:445-457](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L445-L457) —— 写注释头 `# name1,name2`；用 `np.ndim(fmts)==0` 判断标量并提升为一维；逐行调用 `cl_fix_string_from_format` 写出格式字符串。

它依赖的序列化函数承接 u1-l3：

[en_cl_fix_pkg.py:40-41](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L40-L41) —— `cl_fix_string_from_format` 直接 `return str(fmt)`，即 `FixFormat.__str__` 输出的 `(Signed, IntBits, FracBits)`，例如 `(False, 4, 0)`。

#### 4.4.4 代码实践

**实践目标**：写出一个格式定义文件，并查看其文本内容。

**操作步骤**：

```bash
cd python/src
python3 -c "
from en_cl_fix_pkg import *
cl_fix_write_formats([FixFormat(False,4,0), FixFormat(True,2,2)], ['data_a', 'data_b'], '/tmp/formats.txt')
print(open('/tmp/formats.txt').read())
"
```

**需要观察的现象**：文件第一行是 `# data_a,data_b`，随后两行分别是 `(False, 4, 0)` 与 `(True, 2, 2)`。

**预期结果**：文件内容正好三行（一个头 + 两个格式）。注意 Python 输出的是**大写带空格**的 `(False, 4, 0)`，与 VHDL/MATLAB 输出的小写无空格 `(false,4,0)` 不同（这是 u1-l3 提过的跨语言序列化差异，做跨语言交换时要留意）。运行结果待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么需要 `np.ndim(fmts) == 0` 这一步判断？

**答案**：为了让函数同时接受「单个格式」和「格式列表」两种输入。传单个 `FixFormat` 时 `np.ndim` 返回 0，函数用 `np.array(fmts, ndmin=1)` 把它包成长度为 1 的一维数组，使后续 `for fmt in fmts` 循环统一工作，不必为标量单独写一条分支。

**练习 2**：这个写出的文件能直接被 VHDL 的 `cl_fix_read_int` 读取吗？

**答案**：不能直接读。`cl_fix_write_formats` 写的是**格式清单**（描述每个数据的 `[S,I,F]`），而 `cl_fix_read_int` 读取的是**数据本身**（按某约定格式排列的数值）。二者是数据交换链路里前后相接的两类文件：先用前者公布格式，再用数据写函数（Unit 5）按该格式写出数值。官方如今更推荐改用 `en_cl_bittrue_pkg` 做位真交换（Unit 5 详讲）。

---

### 4.5 unittest 测试组织：按函数分组的 TestCase

#### 4.5.1 概念说明

光有实现不算完，还得能验证它「位真」。Python 端用标准库 `unittest` 把验证自动化：整个仓库只有**一个**测试文件 `python/unittest/en_cl_fix_pkg_test.py`，里面按「**每个被测函数一个 `TestCase` 子类**」的方式组织。这样测试报告会清楚地告诉你「`cl_fix_resize` 的哪些场景过了、`cl_fix_mult` 的哪些场景过了」，定位回归非常方便。

这个文件还是理解整个库行为的**最佳活文档**：每个测试方法都是一个「输入 → 期望输出」的微型样例，当你拿不准某函数在某边界的行为时，先去这里查测试往往比读算法实现更快。

#### 4.5.2 核心流程

测试文件自身的启动逻辑有三步：

1. `sys.path.append("../src")` —— 把源码目录加进导入搜索路径。
2. `from en_cl_fix_pkg import *` —— 通过包门面一次性拿到所有被测函数与类型。
3. `import unittest`，并在文件末尾 `unittest.main()` —— 由 unittest 自动发现所有 `TestCase` 子类里的 `test_*` 方法并执行。

由于步骤 2 的星号导入，**`numpy`（`en_cl_fix_pkg.py` 里 `import numpy as np`）也会被一并带入测试文件**——所以测试文件里直接用 `np.array(...)` 而无需自己 `import numpy`（你会在 `cl_fix_sneg_Test`、`cl_fix_addsub_Test` 等用例里看到这一点）。这是一个依赖门面「再导出」的隐式约定，值得留意。

每个 `TestCase` 内部则是若干 `test_XXX` 方法，用 `self.assertEqual(期望, 实际)` 断言。

#### 4.5.3 源码精读

测试文件顶部即上述三步启动：

[en_cl_fix_pkg_test.py:6-10](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/unittest/en_cl_fix_pkg_test.py#L6-L10) —— `sys.path.append("../src")` 修正搜索路径，`from en_cl_fix_pkg import *` 拿到全部被测对象（连带 `np`），再 `import unittest`。注意 `../src` 是**相对路径**，所以测试必须在 `python/unittest` 目录下运行。

「按函数分组」的第一组样例——`cl_fix_width` 的测试类，覆盖了正/负整数位、正/负小数位、有/无符号等边界：

[en_cl_fix_pkg_test.py:16-38](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/unittest/en_cl_fix_pkg_test.py#L16-L38) —— `### cl_fix_width ###` 注释作为分组标题，其后 `class cl_fix_width_Test(unittest.TestCase)` 内每个 `test_` 方法断言一个 `cl_fix_width` 场景，例如 `test_NegativeInt` 验证 `FixFormat(True,-2,3)` 位宽为 2，`test_NegativeFract` 验证 `FixFormat(True,3,-2)` 位宽为 2。

文件末尾的标准启动入口：

[en_cl_fix_pkg_test.py:780-784](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/unittest/en_cl_fix_pkg_test.py#L780-L784) —— `if __name__ == "__main__": unittest.main()` 让 unittest 自动收集本文件所有 `TestCase`、运行其中全部 `test_*` 方法并打印统计。README 第 30–32 行给出的运行指引对应的就是这一入口。

被测函数本身只是一个对 `FixFormat.width()` 的薄封装，这也是位宽公式在三语言间逐字符对应（u1-l3 所述位真一致性）的体现：

[en_cl_fix_pkg.py:20-21](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L20-L21) —— `cl_fix_width` 直接 `return fmt.width()`，即 \( W = S + I + F \)。

#### 4.5.4 代码实践（本讲主实践）

**实践目标**：跑通整套 Python 测试，并仿照现有用例为 `cl_fix_width` 新增一个针对 `FixFormat(True,5,-3)` 的测试方法。

**操作步骤**：

1. **运行现有测试**（README 第 31–32 行的官方方式）：

   ```bash
   cd python/unittest
   python3 en_cl_fix_pkg_test.py
   ```

   注意必须 `cd` 到 `python/unittest`，因为测试文件用相对路径 `sys.path.append("../src")`（见上 [en_cl_fix_pkg_test.py:6-8](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/unittest/en_cl_fix_pkg_test.py#L6-L8)）。若想看每个用例的逐项结果，加 `-v`：`python3 en_cl_fix_pkg_test.py -v`。

2. **新增一个测试方法**。在 `cl_fix_width_Test` 类内（上 [en_cl_fix_pkg_test.py:16-38](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/unittest/en_cl_fix_pkg_test.py#L16-L38) 所示位置），仿照 `test_NegativeFract` 的写法，新增：

   ```python
   # 示例代码：新增到 cl_fix_width_Test 类中
   def test_NegativeFract_Large(self):
       # IntBits + FracBits = 5 + (-3) = 2 >= 1，合法格式
       # width = S + I + F = 1 + 5 + (-3) = 3
       self.assertEqual(3, cl_fix_width(FixFormat(True, 5, -3)))
   ```

3. **再次运行**测试，确认新方法被执行且通过。

**需要观察的现象**：第 1 步应输出类似 `OK` 以及通过的用例计数（具体数字待本地验证）。第 3 步的报告中应能看到新增的 `test_NegativeFract_Large` 被列出且通过，总通过用例数比第 1 步多 1。

**预期结果**：新增方法断言 `cl_fix_width(FixFormat(True, 5, -3)) == 3`，该结果由位宽公式 \( W = 1 + 5 + (-3) = 3 \) 确定（见 [en_cl_fix_types.py:55-56](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_types.py#L55-L56)），且 `IntBits+FracBits = 2 ≥ 1` 是合法格式，故测试必然通过。若误写成 `FixFormat(True, 5, -6)`（此时 `I+F = -1 < 1`），则触及 u1-l3 提过的退化格式——Python 不做检查、会静默返回 `width = 0`，测试反而会失败，可作为对照实验。完整运行结果待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么测试文件用 `sys.path.append("../src")` 而不是「把包装到 site-packages」？

**答案**：这是「源码树内就地测试」的常见做法——不修改系统环境、不要求 `pip install`，只要从仓库检出就能在 `python/unittest` 里直接跑，便于 CI 和协作者。代价是运行目录被固定（必须在该目录下执行，因为 `../src` 是相对路径）。

**练习 2**：测试文件里直接用了 `np.array(...)`，却没写 `import numpy`，为什么不会 `NameError`？

**答案**：因为 `from en_cl_fix_pkg import *` 把主体库里 `import numpy as np` 绑定的名字 `np` 也一并「再导出」了（该模块没有定义 `__all__`，所以所有公开名字都会被星号导入带上）。这是隐式依赖门面的约定；若哪天主体库改用 `__all__` 限制导出，测试文件就会报 `NameError`，届时需要显式补一行 `import numpy as np`。

---

## 5. 综合实践

把本讲四个知识点（门面导入、numpy 向量化、`cl_fix_random`、`cl_fix_write_formats`）与测试方法学串起来，完成下面这个**小型协同仿真数据准备脚本**：

**任务**：写一个脚本 `gen_test_data.py`（放在任意目录均可，只要能 `sys.path` 到 `python/src`），完成：

1. 用 `cl_fix_random` 为格式 `FixFormat(True, 3, 4)`（位宽 8、范围 `[-8, 7.9375]`、步长 0.0625）生成 20 个随机定点样本。
2. 用 `cl_fix_write_formats` 把该格式以名字 `samples` 写入 `/tmp/fmt.txt`。
3. 用 numpy 的向量化能力（如 `np.min/np.max`）打印这 20 个样本的最小值、最大值，并断言它们都落在 `cl_fix_min_value`/`cl_fix_max_value` 之内。
4. 再为这个脚本配一个 `unittest` 用例：断言 `cl_fix_random` 返回的数组**长度**等于请求的 `n`，且每个值都是 `0.0625` 的整数倍（即落在网格点上）。

参考骨架（示例代码，非项目原有文件）：

```python
# 示例代码
import sys
sys.path.append("../src")
from en_cl_fix_pkg import *
import numpy as np
import unittest

FMT = FixFormat(True, 3, 4)

# 1-3: 生成、写出、校验
samples = cl_fix_random(20, FMT)
cl_fix_write_formats(FMT, "samples", "/tmp/fmt.txt")
print("min/max:", np.min(samples), np.max(samples))
assert np.all(samples >= cl_fix_min_value(FMT))
assert np.all(samples <= cl_fix_max_value(FMT))

# 4: 配套测试
class RandomGrid_Test(unittest.TestCase):
    def test_OnGrid(self):
        s = cl_fix_random(50, FMT)
        self.assertEqual(50, len(s))
        # 每个值乘以 2^F 后应是整数（落在网格点上）
        scaled = np.round(s * 2**FMT.FracBits)
        self.assertTrue(np.all(scaled == s * 2**FMT.FracBits))

if __name__ == "__main__":
    unittest.main()
```

**验收**：脚本运行后打印的 min/max 应在 `[-8, 7.9375]` 内；`/tmp/fmt.txt` 第一行为 `# samples`、第二行为 `(True, 3, 4)`；`unittest` 报告 `OK`。这一实践同时用到了门面导入、向量化、随机生成、文件 IO 与测试编写，是本讲内容的综合闭环。完整运行结果待本地验证。

## 6. 本讲小结

- Python 实现把职责拆成 `en_cl_fix_types`（类型）、`en_cl_fix_pkg`（主体算法）、`wide_fxp`（大位宽）三个模块，由 `__init__.py` 用三行星号导入构成**统一门面**，外部只需 `from en_cl_fix_pkg import *`。
- Python 实现依赖 **numpy**，几乎所有函数都按 **ndarray 向量化**设计：用 `np.floor`、`np.where`、`np.max` 等一次处理整段数据，而非逐元素循环。
- `cl_fix_random` 是 **Python 独有**的仿真辅助函数，通过在整数网格坐标上 `randint` 取样，生成均匀覆盖某格式整个动态范围的随机定点数据。
- `cl_fix_write_formats` 负责把格式定义写成文本清单（注释头 + 每行一个 `(S,I,F)`），是跨语言位真数据交换链路的「格式公布」环节，数据本身写出留待 Unit 5。
- 唯一的测试文件 `en_cl_fix_pkg_test.py` 采用「**每个被测函数一个 `TestCase` 子类**」的组织，是理解库行为的活文档；它靠 `sys.path.append("../src")` 就地导入，必须在 `python/unittest` 目录下运行。
- 星号导入会顺带把 `numpy`（`np`）等名字「再导出」到测试文件，因此测试里可直接用 `np.array` 而无需显式 `import numpy`——这是一个隐式约定。

## 7. 下一步学习建议

本讲只看了 Python 的**工程骨架**，尚未进入任何定点函数的算法内部。建议下一步：

- **横向对照三语言测试流程**：先读 u2-l2（VHDL 仿真与 testbench）和 u2-l3（MATLAB 模型），体会「同一套位真语义、三套截然不同的测试基础设施」——VHDL 用 `sim.tcl` + `Check*` 断言、MATLAB 暂无测试、Python 用 `unittest`。
- **纵向深入核心函数**：进入 Unit 3，从 [u3-l1 数值与字符串转换函数](u3-l1-conversion-functions.md) 开始，真正打开 `cl_fix_from_real`、`cl_fix_resize` 的算法实现。届时你会发现，本讲看到的「向量化 + `np.where` 夹紧」模式正是它们内部的真实写法。
- **大位宽迷题的伏笔**：本讲多处提到 `cl_fix_is_wide` 与 `wide_fxp` 分支（如 `cl_fix_random` 的两条路径）。若你想知道「为什么是 53 位这条分界线」「大整数是怎么存的」，直接跳到 Unit 6 的 [u6-l1 narrow/wide 双实现与自动派发](u6-l1-narrow-wide-dispatch.md)。
