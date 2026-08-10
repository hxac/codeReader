# MATLAB→Python 桥接：matlab_interface 与 uint64 分块

## 1. 本讲目标

学完本讲，你应当能够：

- 画出任意一个 `cl_fix_*.m` 包装器的数据流：`mat2py` → `py.en_cl_fix_pkg.*` 调用 → `py2mat`，并能解释每一步在做什么。
- 区分两条数据通道：**narrow 通道**（≤53 位，走 `double`）与 **wide 通道**（>53 位，走 MATLAB `fi()` + uint64 分块）。
- 讲清 `matlab_interface.py` 中 `to_uint64_array` / `from_uint64_array` 如何用 **64 位分块 + 符号位重解释** 在两语言间搬运任意精度定点数。
- 理解 `FixFormat`、`FixRound`、`FixSaturate` 这些格式/模式对象如何以「Python 对象引用」的方式跨边界传递，而不是被序列化。
- 能够精读 `cl_fix_add.m`、`cl_fix_from_real.m`、`wide.m`、`matlab_interface.py` 并解释任意一行的用途。

## 2. 前置知识

本讲是专家层，假设你已经学过：

- **u2-l2**（Python 主接口）：知道所有 `cl_fix_*` 函数遵循「先算全精度中间格式 `mid_fmt` → 运算 → resize」的统一骨架。
- **u6-l2 / u6-l3**（Narrow/Wide 双表示）：知道 narrow 用 `float64` 存归一化 real、wide 用 Python 任意精度整数（`object` dtype）存非归一化整数；且 `cl_fix_*` 函数返回的是**原始 `_data` 数组**（narrow→`float64`、wide→`object`），而非 `NarrowFix`/`WideFix` 对象。

本讲用到但需要先建立的几个概念：

| 概念 | 一句话解释 |
|------|-----------|
| MATLAB Python 接口 | MATLAB 内置的 `py.*` 命名空间，可直接调用已安装的 Python 解释器里的模块与函数。 |
| `fi()` 对象 | MATLAB Fixed-Point Designer 工具箱里的定点对象，用 `[s, w, f]`（符号、字长、小数位）描述。 |
| bit-true | 两语言对同一输入产生逐位相同的结果。 |
| uint64 分块 | 把一个超长整数切成若干段 64 位无符号整数来传输。 |
| 符号位重解释 | 用「补码取模」在有符号整数与无符号整数之间无损切换比特模式。 |

一个关键直觉：**MATLAB 和 Python 是两个独立的运行时**，它们之间只能交换「原生类型」（数值、数组、字符串、对象引用）。定点数本身不是任何一方的原生类型——Python 用 numpy 数组模拟、MATLAB 用 `fi()` 模拟。桥接的全部难点就在于：如何把一种模拟表示，无损地翻译成另一种。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用法 |
|------|------|---------|
| [bittrue/models/matlab/cl_fix_add.m](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/cl_fix_add.m) | 加法包装器 | 包装器统一骨架的范本（双操作数） |
| [bittrue/models/matlab/cl_fix_from_real.m](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/cl_fix_from_real.m) | 浮点→定点包装器 | 包装器骨架的范本（单操作数，强制 narrow 输入） |
| [bittrue/models/matlab/mat2py.m](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/mat2py.m) | MATLAB→Python 原生类型转换（第三方） | narrow 通道的入方向 |
| [bittrue/models/matlab/py2mat.m](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/py2mat.m) | Python→MATLAB 原生类型转换（第三方） | narrow 通道的出方向 |
| [bittrue/models/matlab/wide.m](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/wide.m) | wide 定点支持类（`classdef`） | narrow/wide 分流 + `fi()`↔uint64 互转 |
| [bittrue/models/python/en_cl_fix_pkg/matlab_interface.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/matlab_interface.py) | uint64 分块的 Python 端 | wide 数据的打包/解包 |
| [bittrue/models/matlab/cl_fix_format.m](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/cl_fix_format.m) | 构造 `FixFormat` 的包装器 | 演示格式对象如何过边界 |
| [bittrue/models/matlab/cl_fix_constants.m](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/cl_fix_constants.m) | 装载 `Round.*` / `Sat.*` 常量 | 演示枚举对象如何过边界 |
| [bittrue/models/python/en_cl_fix_pkg/__init__.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/__init__.py) | Python 包门面 | 暴露 `to/from_uint64_array` 给 MATLAB |
| [bittrue/tests/matlab/matlab_example.m](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/matlab/matlab_example.m) | MATLAB 端可运行示例 | 本讲代码实践的依据 |

> 说明：`mat2py.m` 与 `py2mat.m` 文件头注明它们来自 Apress 出版社《Python for MATLAB Development》一书（MIT 许可）。en_cl_fix 复用这两个第三方函数处理「普通数值」的原生转换，自己只在其上叠加 wide 定点的特殊处理。

## 4. 核心概念与源码讲解

### 4.1 包装器的统一骨架：mat2py → py 调用 → py2mat

#### 4.1.1 概念说明

en_clustra 没有把定点算法用 MATLAB 重写一遍，而是让每个 `cl_fix_*.m` 函数都成为一个**极薄的转发壳**：它只负责把 MATLAB 侧的输入翻译成 Python 能懂的原生类型，调用真正的 Python 实现 `py.en_cl_fix_pkg.cl_fix_*`，再把返回值翻译回 MATLAB 类型。

这样做的好处是：**算法只有一份实现**（Python 的 `en_cl_fix.py`），VHDL、Python、MATLAB 三语言因此天然 bit-true（MATLAB 只是把活儿外包给 Python）。代价是每次调用都要跨一次 MATLAB↔Python 边界，有性能开销。

每个包装器都遵循同一个三段式骨架：

1. **记录输入形状**（column / row / 标量），用于事后还原——这是为了规避 MATLAB↔Python 接口在向量形状上的已知不一致。
2. **入方向转换** `mat2py` / `wide.mat2py`：MATLAB 数据 → Python 原生数据。
3. **调用** `py.en_cl_fix_pkg.cl_fix_*(...)`。
4. **出方向转换** `wide.py2mat`：Python 原生结果 → MATLAB 数据。
5. **还原形状**。

#### 4.1.2 核心流程

以双操作数的 `cl_fix_add(a, a_fmt, b, b_fmt, r_fmt, round, saturate)` 为例：

```
记录 a 的形状 (is_column / is_row)
↓
a      = wide.mat2py(a, a_fmt)      # MATLAB→Python
b      = wide.mat2py(b, b_fmt)
r      = py.en_cl_fix_pkg.cl_fix_add(a, a_fmt, b, b_fmt, r_fmt, ...)
r      = wide.py2mat(r, r_fmt)       # Python→MATLAB
↓
按记录的形状还原 r（列→列、行→行）
```

注意：**格式对象 `a_fmt`/`b_fmt`/`r_fmt` 不参与 `mat2py` 转换**——它们本身就是 Python 对象（见 4.2），直接原样传给 Python 函数。只有「数据」`a`/`b`/`r` 需要翻译。

#### 4.1.3 源码精读

[cl_fix_add.m:27-49](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/cl_fix_add.m#L27-L49) 是骨架的完整范本。关键几行：

- 第 29-30 行记录输入形状，注释明说这是为了规避接口的形状不一致 bug：
  ```matlab
  is_column = iscolumn(varargin{1});
  is_row = isrow(varargin{1});
  ```
- 第 33、36 行分别把两个操作数经 `wide.mat2py` 翻译成 Python 原生类型（`varargin{2}`、`varargin{4}` 是对应的格式，用来判定走 narrow 还是 wide 通道）。
- 第 39 行是真正的计算，把所有参数（含未翻译的格式对象）透传给 Python：
  ```matlab
  r = py.en_cl_fix_pkg.cl_fix_add(varargin{:});
  ```
- 第 42 行用 `wide.py2mat(r, varargin{5})` 把结果翻译回 MATLAB，`varargin{5}` 是结果格式。
- 第 45-49 行按记录的形状还原（列向量 `r(:)`、行向量 `reshape(r,1,[])`）。

`cl_fix_from_real` 是单操作数版本，[cl_fix_from_real.m:32-36](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/cl_fix_from_real.m#L32-L36) 有一个**重要差别**：

```matlab
% The input must be narrow, so use mat2py() directly - not wide.mat2py().
a = mat2py(a);
r = py.en_cl_fix_pkg.cl_fix_from_real(a, r_fmt, saturate);
r = wide.py2mat(r, r_fmt);
```

为什么入方向用裸 `mat2py` 而非 `wide.mat2py`？因为 `from_real` 的输入是**普通浮点数**（real），不是定点数，根本没有 wide/narrow 之分，永远走 narrow 的 `double` 通道。但**出方向仍用 `wide.py2mat`**——因为结果格式 `r_fmt` 可能是 wide（例如把一个 real 量化成 60 位定点），此时 Python 返回 `object` 数组，必须走 wide 通道翻译回 `fi()`。这是一个典型的不对称：输入恒 narrow，输出看 `r_fmt`。

#### 4.1.4 代码实践

**实践目标**：通过精读确认所有包装器共用同一骨架。

**操作步骤**：

1. 打开 `cl_fix_add.m`、`cl_fix_from_real.m`、`cl_fix_random.m` 三个文件。
2. 在每个文件里找出三件事：① 是否记录了输入形状；② 入方向用的是 `mat2py` 还是 `wide.mat2py`；③ 出方向用的是 `py2mat` 还是 `wide.py2mat`。
3. 把结果填入下表。

**需要观察的现象**：

| 包装器 | 记录形状？ | 入方向转换 | 出方向转换 |
|--------|:---:|--------|--------|
| `cl_fix_add` | 是 | `wide.mat2py` | `wide.py2mat` |
| `cl_fix_from_real` | 是 | `mat2py`（裸） | `wide.py2mat` |
| `cl_fix_random` | ? | ? | ? |

**预期结果**：`cl_fix_random.m` 第 27-28 行同样遵循骨架（入 `py.en_cl_fix_pkg.cl_fix_random` 直接构造、出 `wide.py2mat`）。三者结构一致，差别只在入方向是否需要 wide 分流。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cl_fix_add` 的入方向必须用 `wide.mat2py(a, a_fmt)`，而 `cl_fix_from_real` 的入方向却用裸 `mat2py(a)`？

**答案**：`cl_fix_add` 的输入 `a` 本身就是定点数，其格式 `a_fmt` 可能是 wide（>53 位），此时 MATLAB 侧用 `fi()` 存放，必须经 `wide.mat2py` 走 uint64 分块通道；`cl_fix_from_real` 的输入是 real（普通浮点），没有 wide/narrow 之分，恒为 `double`，故用裸 `mat2py` 走 narrow 通道即可。

**练习 2**：如果删掉 `cl_fix_add.m` 第 29-30 行和第 45-49 行的形状处理，对一个 `1×100` 的行向量调用会发生什么？

**答案**：MATLAB↔Python 接口存在已知形状不一致 bug，行向量可能被翻转为列向量返回，导致后续维度断言失败或结果错位。形状处理就是为了「记住进来的形状、强制还原回去」。

---

### 4.2 FixFormat 与常量如何跨边界：对象引用，而非序列化

#### 4.2.1 概念说明

跨语言传数据有两条路：**序列化**（把对象变成字符串/字节流，对端再解析）与**对象引用**（一端把对象句柄直接交给另一端持有）。en_cl_fix 选择后者：MATLAB 侧的 `fmt` 变量其实就是一个**被 MATLAB 持有的 Python `FixFormat` 对象**，它在所有 `py.*` 调用中原样传递，Python 侧拿到的就是同一个对象，无需任何编码/解码。

这之所以可行，是因为 MATLAB Python 接口本身就支持「MATLAB 持有 Python 对象」。于是格式（`FixFormat`）、舍入模式（`FixRound`）、饱和模式（`FixSaturate`）这些「元数据」全部以对象引用方式流通，只有「定点数据本身」才需要 4.3、4.4 讲的原生类型翻译。

#### 4.2.2 核心流程

```
MATLAB 构造格式：  cl_fix_format(1, 4, 8)
        ↓ 内部调用
        py.en_cl_fix_pkg.FixFormat(int64(1), int64(4), int64(8))
        ↓
MATLAB 得到一个 py.FixFormat 引用，存入变量 fmt
        ↓
后续 cl_fix_add(a, fmt, ...) 把 fmt 原样透传给 py.en_cl_fix_pkg.cl_fix_add
```

同理，舍入/饱和常量由 `cl_fix_constants.m` 装载进结构体 `Round` / `Sat`，每个常量也是一个 Python 枚举对象引用。

#### 4.2.3 源码精读

[cl_fix_format.m:27](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/cl_fix_format.m#L27) 是格式对象的诞生地：

```matlab
fmt = py.en_cl_fix_pkg.FixFormat(int64(s), int64(i), int64(f));
```

两个细节值得注意：

- 它直接 `py.en_cl_fix_pkg.FixFormat(...)` 构造 Python 类的实例，MATLAB 拿到的 `fmt` 是一个 `py.` 引用。
- 参数用 `int64(s)` 等强制成整数类型。MATLAB 默认数值是 `double`，若不转换会变成 Python `float`，而 `FixFormat` 的运算（如 `width = S+I+F`）期望整数——这是为了类型正确性。

[cl_fix_constants.m:28-40](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/cl_fix_constants.m#L28-L40) 装载枚举常量，例如：

```matlab
Sat.SatWarn_s = py.en_cl_fix_pkg.FixSaturate(3);
Round.ConvEven_s = py.en_cl_fix_pkg.FixRound(5);
```

每个常量是一个 Python 枚举对象引用，存进 MATLAB 结构体字段。文件头注释（第 4-5 行）还提醒：用结构体字段传常量比每次现造要快，但即便如此 MATLAB 跨语言调用仍很慢。

正因格式对象是引用，`cl_fix_is_wide` 这种「纯查询」包装器可以极简——[cl_fix_is_wide.m:27-28](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/cl_fix_is_wide.m#L27-L28)：

```matlab
r = py.en_cl_fix_pkg.cl_fix_is_wide(fmt);   % fmt 原样透传，返回 Python bool
r = py2mat(r);                              % Python bool → MATLAB logical
```

注意这里返回值（一个 `bool`）用裸 `py2mat` 转回 MATLAB `logical`，因为它不是定点数据、无需 wide 通道。

#### 4.2.4 代码实践

**实践目标**：验证「格式对象是引用、可被多个 Python 函数共享识别」。

**操作步骤（需 MATLAB，否则精读）**：

1. 在 MATLAB 中执行（先按 `matlab_example.m` 第 60-65 行的方式装载 Python 包与 MATLAB 源路径）：
   ```matlab
   fmt = cl_fix_format(1, 4, 8);
   class(fmt)              % 期望显示含 py.en_cl_fix_pkg.FixFormat
   cl_fix_width(fmt)       % 期望 13
   cl_fix_is_wide(fmt)     % 期望 false (logical)
   ```
2. 观察同一个 `fmt` 被传给 `cl_fix_width`、`cl_fix_is_wide` 两个不同 Python 函数都正确识别。

**预期结果**：`class(fmt)` 显示这是一个 Python 对象引用；`cl_fix_width(fmt)` 返回 `13`（=1+4+8）；`cl_fix_is_wide(fmt)` 返回 MATLAB `logical` 的 `false`。若无法运行 MATLAB，则「待本地验证」，但可由 [cl_fix_format.m:27](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/cl_fix_format.m#L27) 与 [cl_fix_is_wide.m:27](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/cl_fix_is_wide.m#L27) 的源码逻辑直接推断。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `cl_fix_format` 要用 `int64(s)` 而不是直接传 `s`？

**答案**：MATLAB 数值默认是 `double`，直接传会变成 Python `float`，破坏 `FixFormat` 内部的整数运算。`int64()` 强制成整数类型，确保 Python 侧收到的是 `int`。

**练习 2**：`cl_fix_is_wide` 的返回值为什么用裸 `py2mat(r)` 而不是 `wide.py2mat(r, fmt)`？

**答案**：返回值是 Python `bool`（普通逻辑值），不是定点数据，没有 wide/narrow 之分，走 narrow 的原生转换即可。

---

### 4.3 narrow 通道：mat2py / py2mat 的原生类型转换

#### 4.3.1 概念说明

当定点格式 ≤53 位（narrow），定点值在 Python 侧就是一个 `float64`（归一化 real，见 u6-l1）。`float64` 是 Python 原生类型，MATLAB 对应 `double`——两者都是 IEEE 754 双精度浮点，**逐位同构**。所以 narrow 通道的翻译本质上就是「numpy `float64` 数组 ↔ MATLAB `double` 数组」，由第三方函数 `mat2py.m` / `py2mat.m` 完成。

关键点：`py2mat.m` **只认识 numpy 的标准数值 dtype**（`float64`、`int32`、`uint64`……），**不认识 wide 用的 `object` dtype**（Python 任意精度整数）。这就是为什么 wide 必须另起一条通道（4.4、4.5）。

#### 4.3.2 核心流程

入方向 `mat2py`（MATLAB→Python），见 [mat2py.m:66-92](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/mat2py.m#L66-L92)：

```
标量数值      →  原样透传（MATLAB 标量即 Python 标量）
实数组        →  py.numpy.array(x_mat)        # 成为 numpy float64 数组
稀疏/复数/... →  对应 numpy 结构
```

出方向 `py2mat`（Python→MATLAB），见 [py2mat.m:77-125](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/py2mat.m#L77-L125)：

```
numpy float64 数组  →  x_py.double        # MATLAB double 数组
numpy int32 数组    →  int32(...)
numpy uint64 数组   →  uint64(...)
object dtype 数组   →  落入 otherwise 分支，打印 "not recognized" 并返回 []
```

最后一行正是 wide 必须绕开 `py2mat` 的根本原因。

#### 4.3.3 源码精读

入方向的核心分支在 [mat2py.m:85-91](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/mat2py.m#L85-L91)：

```matlab
if numel(x_mat) == 1
    x_py = x_mat;              % 标量：直接透传
elseif isreal(x_mat)
    x_py = py.numpy.array(x_mat);   % 实数组：包成 numpy 数组
else
    x_py = py.numpy.array(real(x_mat)) + 1j*py.numpy.array(imag(x_mat));
end
```

出方向对 `float64` 的处理在 [py2mat.m:79-80](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/py2mat.m#L79-L80)：

```matlab
case "float64"
    x_mat = x_py.double;        % numpy float64 → MATLAB double
```

注意 `py2mat.m` 整个 `switch`（[py2mat.m:77-125](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/py2mat.m#L77-L125)）枚举了所有标准 dtype，唯独没有 `object`——而 wide 数据正是 `object` dtype（Python int 数组）。这是一个「能力边界」：`py2mat` 天生处理不了任意精度整数。

#### 4.3.4 代码实践

**实践目标**：验证 narrow 通道下，MATLAB `double` 与 Python `float64` 是逐位等价的。

**操作步骤（需 MATLAB，否则精读）**：

```matlab
x = [1.5, 2.25, -3.0];
xp = mat2py(x);          % → numpy float64 数组
class(xp)                % 期望 py.numpy.ndarray
string(xp.dtype.name)    % 期望 "float64"
xr = py2mat(xp);         % → MATLAB double
isequal(x, xr)           % 期望 true（逐位一致）
```

**需要观察的现象**：往返转换后 `isequal` 为 `true`，证明 narrow 通道无损。

**预期结果**：`isequal(x, xr)` 返回 `true`。若没有 MATLAB 环境，则「待本地验证」，但可由 `float64`↔`double` 的 IEEE 754 同构性从源码推断。

#### 4.3.5 小练习与答案

**练习 1**：如果把一个 wide 结果（`object` dtype 的 Python int 数组）误传给裸 `py2mat`，会发生什么？

**答案**：会落入 [py2mat.m:119-124](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/py2mat.m#L119-L124) 的 `otherwise` 分支，打印 `py2mat: <dtype> not recognized` 并返回空 `[]`，丢失数据。这正是 `wide.py2mat` 存在的意义。

**练习 2**：为什么 narrow 通道能保证 bit-true？

**答案**：narrow 值在 Python 侧是 `float64`、MATLAB 侧是 `double`，两者都是 IEEE 754 双精度，比特表示完全一致；`mat2py`/`py2mat` 只是搬运比特，不做任何量化或重编码。

---

### 4.4 wide.m：narrow↔wide 分流与 fi() 互转

#### 4.4.1 概念说明

`wide.m` 是一个 MATLAB `classdef`，全部是静态方法，扮演**分流调度器**的角色。它的公共方法 `wide.py2mat(x, x_fmt)` 和 `wide.mat2py(x, x_fmt)` 先用 `cl_fix_is_wide(x_fmt)` 判定格式宽度，再决定走 narrow 通道（委托给 4.3 的裸 `mat2py`/`py2mat`）还是 wide 通道（委托给私有的 `wide.py2fi`/`wide.fi2py`）。

文件头注释（[wide.m:1-9](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/wide.m#L1-L9)）明确点出：wide 通道依赖 MATLAB 的 **Fixed-Point Designer 工具箱**（用到 `fi()`、`numerictype`、`reinterpretcast`、`quantize`），而 narrow 通道不需要。这是窄/宽通道在**工具链依赖**上的另一道分界。

#### 4.4.2 核心流程

`wide.py2mat`（Python→MATLAB，[wide.m:15-28](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/wide.m#L15-L28)）：

```
cl_fix_is_wide(x_fmt)?
  是 → wide.py2fi(x, x_fmt)     # object int 数组 → fi()
  否 → py2mat(x)                # float64 → double（走 4.3 通道）
```

`wide.mat2py`（MATLAB→Python，[wide.m:30-43](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/wide.m#L30-L43)）方向相反，判定逻辑相同。

wide 通道的两端对接 `fi()`：

```
Python object int 数组  ──to_uint64_array──▶  uint64 数组  ──wide.py2fi──▶  MATLAB fi()
MATLAB fi()            ──wide.fi2py──▶  uint64 数组  ──from_uint64_array──▶  Python object int 数组
```

中间的 uint64 数组是两语言的「共同语言」——`uint64` 是双方都有的原生类型（不像任意精度整数）。`wide.py2fi`/`fi2py` 负责 `fi()`↔uint64，`matlab_interface.py` 的 `to/from_uint64_array` 负责 uint64↔Python 任意精度整数（见 4.5）。

#### 4.4.3 源码精读

公共调度器 [wide.m:15-28](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/wide.m#L15-L28)：

```matlab
function y = py2mat(x, x_fmt)
    if cl_fix_is_wide(x_fmt)
        y = wide.py2fi(x, x_fmt);   % wide
    else
        y = py2mat(x);              % narrow，委托给 4.3 的裸函数
    end
end
```

格式互转工具 `fmt2swf` / `fi2swf` / `swf2fmt` / `fi2fmt`（[wide.m:45-86](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/wide.m#L45-L86)）负责在 en_cl_fix 的 `[s, i, f]`（符号位、整数位、小数位）与 MATLAB `fi()` 的 `[s, w, f]`（符号、**字长**、小数位）之间换算。核心关系是 `w = s + i + f`，即 [wide.m:51-54](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/wide.m#L51-L54)：

```matlab
s = double(fmt.S);
w = double(cl_fix_width(fmt));   % = S+I+F
f = double(fmt.F);
i = double(fmt.I);
```

私有的 `py2fi`（[wide.m:93-129](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/wide.m#L93-L129)）把 Python wide 数据还原成 `fi()`，关键三步：

1. [wide.m:101](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/wide.m#L101) 先调 Python 把任意精度整数打包成 uint64 数组：
   ```matlab
   x = py.en_cl_fix_pkg.to_uint64_array(x, x_fmt);
   ```
2. [wide.m:114-124](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/wide.m#L114-L124) 把多段 uint64 按 `2^(64·(k-1))` 权重累加回一个 `w` 位的 `fi()`（`SumMode='KeepLSB'` 保证只保留低 `w` 位）：
   ```matlab
   for k = 2:n_ints
       idx{end} = k;
       y = y + pow2(x(idx{:}), (k-1)*64);
   end
   ```
3. [wide.m:127](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/wide.m#L127) 用 `reinterpretcast` 把无符号比特模式重解释成带正确 `[s,w,f]` 的定点：
   ```matlab
   y = reinterpretcast(y, numerictype(s,w,f));
   ```

注意 `idx{end} = k`（[wide.m:117](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/wide.m#L117)、[wide.m:122](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/wide.m#L122)）索引的是**最后一维**——这决定了它期望 uint64 沿末维堆叠，正是 4.5 中 `matlab_interface.py` 的约定。

反方向 `fi2py`（[wide.m:131-161](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/wide.m#L131-L161)）：

- [wide.m:144](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/wide.m#L144) 先把 `fi()` 重解释成无符号 `w` 位：
  ```matlab
  x = reinterpretcast(x, numerictype(0,w,0));
  ```
- [wide.m:151-156](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/wide.m#L151-L156) 用 `quantize` 每次取低 64 位写入 `y`，再用 `pow2(x,-64)` 右移 64 位（`RoundingMethod='Floor'` 保证向下取整，匹配 Python 的 `>>`）：
  ```matlab
  for k = 1:n_ints
      idx{end} = k;
      y(idx{:}) = uint64(quantize(x, numerictype(0, 64, 0)));
      x = pow2(x, -64);   % 等价于 x >>= 64
  end
  ```
- [wide.m:160](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/wide.m#L160) 调 Python 把 uint64 数组解包回任意精度整数：
  ```matlab
  y = py.en_cl_fix_pkg.from_uint64_array(y, fmt);
  ```

#### 4.4.4 代码实践

**实践目标**：理解 `fmt2swf` 的换算关系，验证 `w = s+i+f`。

**操作步骤**：

1. 精读 [wide.m:45-55](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/wide.m#L45-L55)。
2. 手算下列格式的 `[s, w, f]`：

| en_cl_fix 格式 `[S,I,F]` | `s` | `w = S+I+F` | `f` |
|--------------------------|:---:|:---:|:---:|
| `[1, 8, 60]`（wide，69 位） | 1 | ? | 60 |
| `[0, 4, 50]`（wide，54 位） | 0 | ? | 50 |
| `[1, 4, 8]`（narrow，13 位） | 1 | 13 | 8 |

**预期结果**：`[1,8,60]`→`w=69`；`[0,4,50]`→`w=54`；`[1,4,8]`→`w=13`。前两个 `w>53` 故 `cl_fix_is_wide` 为真、走 wide 通道；第三个 `w=13≤53` 走 narrow 通道。

#### 4.4.5 小练习与答案

**练习 1**：`wide.py2fi` 第 3 步为什么要 `reinterpretcast`？

**答案**：前两步累加得到的是一个**无符号** `w` 位整数（因为 uint64 是无符号的）。但目标格式可能是有符号的，且需要正确的小数位 `f`。`reinterpretcast` 不改变比特，只改变解释方式，把同一组比特重新当作 `[s,w,f]` 定点数读出。

**练习 2**：`fi2py` 中 `x.RoundingMethod = 'Floor'`（[wide.m:151](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/matlab/wide.m#L151)）若改成 `'Round'` 会怎样？

**答案**：`pow2(x,-64)` 右移 64 位时若向上舍入，会让每一段 64 位块之间进位错位，重组出的整数与原始值不符。`'Floor'` 保证向下取整（截断低位），与 Python 端 `data >>= 64` 的语义一致，从而 bit-true。

---

### 4.5 matlab_interface.py：uint64 分块与符号位重解释

#### 4.5.1 概念说明

这是整条桥接链最核心、也最巧妙的一环。问题：Python wide 定点数据是**任意精度有符号整数**（numpy `object` dtype，元素是 Python `int`），而 MATLAB 没有任意精度整数原生类型，且跨语言边界也不认 `object` 数组。怎么搬？

解法分两步：

1. **分块**：把一个超长整数切成若干段 64 位，每段是一个 `uint64`（双方都有的原生类型）。一个 `w` 位的整数需要 \(n = \lceil w/64 \rceil \) 段。
2. **符号位重解释**：Python 存的是**有符号**整数（可能是负数），但 `uint64` 是**无符号**的。传输前用补码取模把负数变成等比特模式的无符号数，对端再用同样规则还原。

#### 4.5.2 核心流程

**打包 `to_uint64_array(data, fmt)`**（Python wide → uint64）：

```
断言 fmt 是 wide、data 是 object dtype 的 int 数组
n_ints = ceil(width / 64)
若 fmt.S == 1（有符号）：负数 data += 2^width   # 补码取模，转无符号
for i in 0..n_ints-1:
    result[..., i] = data % 2^64     # 取低 64 位
    data >>= 64                      # 右移，准备取下一段
返回 uint64 数组（沿末维堆叠 n_ints 段）
```

**解包 `from_uint64_array(data, fmt)`**（uint64 → Python wide）：

```
断言 fmt 是 wide、data 是 uint64 数组
weights = [2^0, 2^64, 2^128, ...]        # 各段权重
result = data · weights                    # 加权求和重组无符号整数
若 fmt.S == 1：result >= 2^(I+F) 的元素 -= 2^(I+F+1)   # 还原负数
返回 object dtype 的 int 数组
```

#### 4.5.3 数学原理

设原始有符号整数 \(v\)，位宽 \(w = S+I+F\)（有符号时 \(S=1\)，故 \(w=I+F+1\)）。

**打包**（负数转无符号）：对 \(v < 0\)，
\[ u = v + 2^{w} \pmod{2^{w}} \]
这把 \([-2^{w-1},\, 2^{w-1}-1]\) 映射到 \([0,\, 2^{w}-1]\)，即补码的无符号解读。

**分块**：把 \(u\) 写成 64 位一段：
\[ u = \sum_{k=0}^{n-1} c_k \cdot 2^{64k}, \quad c_k = \left\lfloor \frac{u}{2^{64k}} \right\rfloor \bmod 2^{64} \]
其中段数 \(n = \lceil w/64 \rceil = (w+63)//64\)（见 [matlab_interface.py:26](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/matlab_interface.py#L26)）。

**解包**：加权求和还原 \(u = \sum_k c_k \cdot 2^{64k}\)。

**还原符号**：无符号 \(u\) 的最高位（第 \(w-1 = I+F\) 位）是符号位。若该位为 1（即 \(u \ge 2^{w-1} = 2^{I+F}\)），则真实值为
\[ v = u - 2^{w} = u - 2^{I+F+1} \]
见 [matlab_interface.py:55-56](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/matlab_interface.py#L55-L56)。

> 一个细节：打包用 `2**fmt.width`（[matlab_interface.py:30](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/matlab_interface.py#L30)），解包用 `2**(fmt.I+fmt.F)` 与 `2**(fmt.I+fmt.F+1)`（[matlab_interface.py:56](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/matlab_interface.py#L56)）。两者在有符号格式下数值相等（\(w = I+F+1\)），只是写法不同。

#### 4.5.4 源码精读

[matlab_interface.py:6-38](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/matlab_interface.py#L6-L38) 是打包函数。三个要点：

```python
# 第 14 行：入口断言——必须是 wide
assert cl_fix_is_wide(fmt), "Fixed-point format must be wide fixed-point"

# 第 26 行：段数 = ceil(width/64)
n_ints = (fmt.width + 63) // 64

# 第 29-30 行：负数补码取模，转无符号
if fmt.S == 1:
    data = np.where(data < 0, data + 2**fmt.width, data)

# 第 33-36 行：逐段取低 64 位、右移
for i in range(n_ints):
    result[..., i] = data % 2**64
    data >>= 64
```

注意 `result = np.empty(data.shape + (n_ints,), ...)`（[matlab_interface.py:33](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/matlab_interface.py#L33)）：uint64 段堆叠在**最后一维**，与 `wide.py2fi` 的 `idx{end}=k` 约定一致。文档字符串（[matlab_interface.py:10-12](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/matlab_interface.py#L10-L12)）明确说明了这一点。

[matlab_interface.py:40-58](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/matlab_interface.py#L40-L58) 是解包函数：

```python
# 第 51-52 行：加权求和重组无符号整数
weights = 2**(64*np.arange(data.shape[-1]).astype(object))
result = np.matmul(data, weights.T)

# 第 55-56 行：还原符号位
if fmt.S == 1:
    result = np.where(result >= 2**(fmt.I+fmt.F), result - 2**(fmt.I+fmt.F+1), result)
```

这里 `data.shape[-1]` 取最后一维作为 uint64 段轴，`np.matmul(data, weights.T)` 做加权求和——`weights.T` 把权重列向量与末维对齐，能正确处理任意形状的 N-D 数组。

> **为什么有两个版本？** `wide_fix.py` 里也有同名方法 `WideFix.to_uint64_array()`（实例方法）和 `WideFix.from_uint64_array()`（静态方法），但它们把 uint64 段堆叠在**第一维**（`wide_fix.py:205` 的 `(n_ints,) + val.shape`），与 `matlab_interface.py` 的「末维」约定相反。MATLAB 桥接专门用 `matlab_interface.py` 的版本，因为 MATLAB 的 N-D 数组天然在末尾追加维度，与 `idx{end}=k` 的索引方式匹配。两套实现服务于不同调用方，不可混用。

这两个函数经 [__init__.py:24](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/__init__.py#L24) 的 `from .matlab_interface import *` 暴露到包顶层，故 MATLAB 能用 `py.en_cl_fix_pkg.to_uint64_array(...)` 直接调用。

#### 4.5.5 代码实践

**实践目标**：用纯 Python 复现 `to_uint64_array` 对一个负数的打包，验证符号位重解释正确。

**操作步骤**（仅需 Python，无需 MATLAB）：

```python
import numpy as np
import sys, os
sys.path.insert(0, "bittrue/models/python")
from en_cl_fix_pkg import FixFormat, to_uint64_array, from_uint64_array

# 一个 wide 有符号格式：69 位，I=8, F=60
fmt = FixFormat(1, 8, 60)
v = -1                                # 取一个负数
packed = to_uint64_array(np.array([v], dtype=object), fmt)
print("packed uint64 段:", packed)    # 期望末段（高位）全 1，体现补码

# 解包回来
restored = from_uint64_array(packed, fmt)
print("restored:", restored)          # 期望 [-1]
assert int(restored[0]) == v
```

**需要观察的现象**：`packed` 应为一个形状 `(1, 2)` 的 uint64 数组（69 位需 2 段），两段都应是 `0xFFFF...FF`（全 1），因为 \(-1\) 的补码是全 1；`restored` 应还原为 `-1`。

**预期结果**：`restored[0] == -1` 断言通过，证明打包-解包往返无损。若未装 numpy，则「待本地验证」，但符号位重解释的逻辑可由 4.5.2、4.5.3 的公式手算验证：\(-1 + 2^{69}\) 的低 64 位与第 65-69 位均为全 1。

#### 4.5.6 小练习与答案

**练习 1**：对格式 `[0, 4, 50]`（无符号，54 位）的值 \(2^{53}\)，`to_uint64_array` 会产生几段、各是多少？

**答案**：\(w=54\)，\(n=\lceil 54/64\rceil=2\) 段。因 `S=0` 无符号位重解释。\(2^{53}\) 的低 64 位 = \(2^{53}\)，第 2 段（高位）= 0（因为 \(2^{53} < 2^{64}\)，右移 64 位后为 0）。所以两段为 \([2^{53}, 0]\)。

**练习 2**：如果把一个 narrow 格式（如 `[1,4,8]`）误传给 `to_uint64_array`，会发生什么？

**答案**：[matlab_interface.py:14](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/matlab_interface.py#L14) 的断言 `assert cl_fix_is_wide(fmt)` 会立即抛 `AssertionError`，拒绝处理 narrow 格式。这是桥接的边界保护——narrow 数据根本不该走这条通道。

---

## 5. 综合实践

**任务**：用窄通道走完一条完整的 `from_real → add` 链路，并对照 Python 验证结果 bit-true。这把 4.1（包装器骨架）、4.2（格式对象引用）、4.3（narrow 通道）三个模块串起来。

**场景**：取两个 `[1,4,8]`（有符号、4 整数位、8 小数位）的定点数 \(a=1.5\)、\(b=2.25\)，相加。两输入相加的保守结果格式为 `cl_fix_add_fmt([1,4,8],[1,4,8]) = [1,5,8]`（整数位 +1 容纳进位，见 u3-l1），期望实数值 \(3.75\)。

**操作步骤（需 MATLAB，否则对照 Python 精读）**：

1. 先按 [matlab_example.m:60-68](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/matlab/matlab_example.m#L60-L68) 装载 Python 包与 MATLAB 源路径，并 `cl_fix_constants;` 装载常量。
2. 在 MATLAB 执行：
   ```matlab
   a_fmt = cl_fix_format(1, 4, 8);
   r_fmt = cl_fix_add_fmt(a_fmt, a_fmt);     % 期望 [1,5,8]
   a = cl_fix_from_real(1.5, a_fmt, Sat.SatWarn_s);
   b = cl_fix_from_real(2.25, a_fmt, Sat.SatWarn_s);
   r = cl_fix_add(a, a_fmt, b, a_fmt, r_fmt); % 期望 3.75
   assert(isequal(r, 3.75), 'add 结果不符');
   ```
3. 在 Python 中执行等价链路对照：
   ```python
   from en_cl_fix_pkg import *
   a_fmt = FixFormat(1, 4, 8)
   r_fmt = cl_fix_add_fmt(a_fmt, a_fmt)
   a = cl_fix_from_real(1.5, a_fmt, FixSaturate.SatWarn_s)
   b = cl_fix_from_real(2.25, a_fmt, FixSaturate.SatWarn_s)
   r = cl_fix_add(a, a_fmt, b, a_fmt, r_fmt)
   print(r_fmt, cl_fix_to_real(r, r_fmt))    # 期望 [1,5,8] 3.75
   ```

**需要观察的现象**：

- `r_fmt` 在两端都应显示为 `[1,5,8]`（格式对象引用传递，4.2）。
- MATLAB 侧 `r` 是 `double`（narrow 通道，4.3），Python 侧 `r.dtype == float64`（u6-l3 唯一表示约定）。
- 两端实数值都应是 `3.75`，证明 MATLAB 经桥接调用 Python 与直接调 Python 结果一致，即 bit-true。

**预期结果**：`r_fmt = [1,5,8]`，`r = 3.75`，断言通过。若 MATLAB 环境不可用，则 MATLAB 侧「待本地验证」；但 Python 侧可立即运行确认，且由 4.1 的骨架（`cl_fix_from_real.m:35`、`cl_fix_add.m:39` 最终都调用同名 Python 函数）可推断 MATLAB 结果必然与 Python 一致。

**进阶（可选）**：把 `a_fmt` 改成 wide 格式 `[1,4,60]`（65 位），重跑上述链路，观察 MATLAB 侧 `a`/`r` 变成 `fi()` 对象（走 4.4、4.5 的 wide 通道），而 Python 侧 `r.dtype` 变为 `object`。此时 `isequal(r, ...)` 需改用 `cl_fix_to_real` 比较。这一步需要 Fixed-Point Designer 工具箱。

## 6. 本讲小结

- 每个 `cl_fix_*.m` 都是极薄转发壳，统一骨架为：**记录形状 → `mat2py`/`wide.mat2py` 入方向转换 → `py.en_cl_fix_pkg.cl_fix_*` 调用 → `wide.py2mat` 出方向转换 → 还原形状**。算法只有 Python 一份实现，三语言因此天然 bit-true。
- `FixFormat`/`FixRound`/`FixSaturate` 等元数据以 **Python 对象引用**方式跨边界，原样透传、不序列化；`cl_fix_format` 用 `int64()` 确保整数类型正确过界。
- 数据有两条通道：**narrow**（≤53 位，`float64`↔`double`，由第三方 `mat2py`/`py2mat` 处理）与 **wide**（>53 位，`object int`↔`fi()`，经 uint64 分块）。`wide.py2mat`/`wide.mat2py` 用 `cl_fix_is_wide` 分流。
- wide 通道的核心是 `matlab_interface.py` 的 `to/from_uint64_array`：把任意精度整数切成 \( \lceil w/64 \rceil \) 段 `uint64`，并用**补码取模** \( v+2^w \)（打包）与 \( u-2^w \)（解包）做有符号↔无符号的符号位重解释。
- uint64 段在 `matlab_interface.py` 中沿**末维**堆叠，匹配 MATLAB N-D 数组的 `idx{end}=k` 索引习惯；这与 `wide_fix.py` 内部「首维」堆叠的版本不同，二者不可混用。
- narrow 通道不需要额外工具箱；wide 通道依赖 MATLAB Fixed-Point Designer（`fi()`、`reinterpretcast`、`quantize`）。

## 7. 下一步学习建议

- **u9-l2（Python 单元测试体系）**：本讲聚焦 MATLAB 桥接的「机制」，下一篇聚焦 Python 端如何用 unittest + 穷举对拍来**保证**这条桥接（以及 narrow/wide 双路径）真的 bit-true，建议接着读 `cl_fix_round_test.py` 看三份实现两两比对的写法。
- **深读 `wide_fix.py`**：本讲提到了 `wide_fix.py` 里另一套「首维」堆载的 `to/from_uint64_array`，建议精读 [wide_fix.py:116-127](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L116-L127) 与 [wide_fix.py:190-210](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/wide_fix.py#L190-L210)，对比两套实现的轴约定差异。
- **扩展阅读**：若你对 MATLAB↔Python 原生类型转换的边界细节感兴趣，可通读第三方 `mat2py.m` / `py2mat.m`（来自《Python for MATLAB Development》），理解它们如何处理 struct、cell、稀疏矩阵、datetime 等类型，这能帮助你判断哪些数据类型能直接过界、哪些不能。
