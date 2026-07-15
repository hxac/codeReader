# MATLAB 薄封装与 Python 互操作

## 1. 本讲目标

学完本讲，你应当能够：

- 看懂 `bittrue/models/matlab/` 下任意一个 `.m` 封装函数的统一结构，并复述「调用 Python 前后做的两次数据转换」。
- 理解 MATLAB 如何通过 `py.en_cl_fix_pkg.*` 直接调用 Python 包，以及 `matlab_example.m` 中 `pyenv`、路径设置、模块加载各步的作用。
- 讲清楚 narrow（≤53 位）数据与 wide（>53 位）数据在 MATLAB↔Python 之间走的是两条完全不同的转换路径。
- 解释 `matlab_interface.to_uint64_array` 为什么要把**有符号整数先重解释为无符号、再按 64 位切片**，并能从「Python 大整数是无限精度有符号数」这一事实推出原因。

本讲承接 u4-l1（Python 主接口）所建立的认知：Python 端 `cl_fix_*` 函数以 53 位为界，把内部计算分发到 `NarrowFix`（双精度浮点）或 `WideFix`（任意精度整数），对外只返回裸数据——narrow 返回 `float64`、wide 返回 `object` 整数数组。本讲要回答的问题是：**当这些裸数据要被 MATLAB 使用时，谁来负责「翻译」？**

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 MATLAB 能直接调用 Python

MATLAB 自带 Python 互操作能力：在 MATLAB 里写 `py.<模块>.<函数>(...)`，就会调用对应 Python 模块的函数，参数与返回值由 MATLAB 自动在两种语言之间做基本类型的桥接（例如 MATLAB 的 `double` 数组会变成 Python 的 `numpy.ndarray`）。这背后的环境由 `pyenv` 配置。本讲要讲的 `.m` 文件，本质就是「在 `py.` 调用的前后，手动加一层定点数据转换」。

### 2.2 为什么需要「手动加一层转换」

MATLAB 自动桥接能处理 `double`、字符串、`numpy` 数组这些**普通类型**，但它不认识 en_cl_fix 的两件事：

1. **定点格式对象 `FixFormat`**：这是 Python 类的实例，必须由 Python 端构造，MATLAB 只能持有它的引用并传递。
2. **wide 定点数据**：Python 端用 `dtype=object` 的**任意精度整数**（Python `int`）保存，而 MATLAB 没有原生任意精度整数类型，只有 `fi()`（Fixed-Point Designer 工具箱的对象）。

所以封装函数的职责就是：在调用 Python 前，把 MATLAB 数据「翻译」成 Python 认识的形态；调用完再把返回值「翻译」回 MATLAB 形态。

### 2.3 两种数据形态，两条翻译路径

| 数据形态 | Python 端表示 | MATLAB 端表示 | 是否需要工具箱 | 翻译方式 |
|---|---|---|---|---|
| narrow（位宽 ≤ 53） | `float64` / numpy 数组 | `double` 数组 | 否 | 几乎是恒等转换（同为 IEEE754 双精度） |
| wide（位宽 > 53） | `object` 整数数组（任意精度 `int`） | `fi()` 对象 | **是**（Fixed-Point Designer） | 经 **uint64 数组**中转打包/解包 |

> 关于 53 这个分界，见 u4-l1 与 u4-l3：`cl_fix_is_wide(fmt)` 当且仅当 `cl_fix_width(fmt) > NarrowFix.MAX_WIDTH`（`MAX_WIDTH = 53`）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [`bittrue/models/matlab/cl_fix_round.m`](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/matlab/cl_fix_round.m) | 典型薄封装，体现 `mat2py → py → py2mat` 三段式。所有算术/转换封装都长一个样。 |
| [`bittrue/models/matlab/wide.m`](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/matlab/wide.m) | 静态方法类，提供 `mat2py`/`py2mat` 的**带格式分发**版本，以及 wide 专用的 `fi↔py` 私有实现。 |
| [`bittrue/models/matlab/mat2py.m`](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/matlab/mat2py.m) / [`py2mat.m`](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/matlab/py2mat.m) | 通用 MATLAB↔Python 类型桥接（第三方代码），处理 narrow 路径的 `double↔numpy`。 |
| [`bittrue/models/python/en_cl_fix_pkg/matlab_interface.py`](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/matlab_interface.py) | Python 侧的 `to_uint64_array`/`from_uint64_array`，wide 数据打包/解包的核心。 |
| [`bittrue/tests/matlab/matlab_example.m`](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/matlab/matlab_example.m) | narrow 示例脚本：演示 `pyenv`、路径、模块加载与封装函数的完整调用。 |
| [`bittrue/tests/matlab/matlab_wide_example.m`](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/matlab/matlab_wide_example.m) | wide 示例脚本：演示需要 Fixed-Point Designer 工具箱的 >53 位通路。 |
| [`bittrue/models/matlab/cl_fix_format.m`](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/matlab/cl_fix_format.m) / [`cl_fix_constants.m`](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/matlab/cl_fix_constants.m) | 辅助封装：构造 Python `FixFormat` 对象、预生成 Python 枚举常量。 |

## 4. 核心概念与源码讲解

### 4.1 MATLAB 薄封装的统一调用模式：三段式

#### 4.1.1 概念说明

`bittrue/models/matlab/` 下的三十多个 `cl_fix_*.m` 文件，几乎都是同一副骨架：**它们本身不做任何定点计算**，算法完全在 Python 的 `en_cl_fix_pkg` 里。每个 `.m` 文件只做三件事——

1. 调用 Python 前：把输入数据从 MATLAB 形态翻译成 Python 形态。
2. 调用 `py.en_cl_fix_pkg.<同名函数>(...)`。
3. 调用 Python 后：把返回值从 Python 形态翻译回 MATLAB 形态。

这就是「薄封装」（thin wrapper）的含义：MATLAB 只是 Python 实现的一层语法外壳，目的是让习惯 MATLAB 的工程师用熟悉的函数名调用同一套算法。这也呼应了 u1-l2 讲过的「三语言镜像架构」——MATLAB 不维护独立算法，它只是 Python 参考模型的 MATLAB 门面。

#### 4.1.2 核心流程

以 `cl_fix_round.m` 为例，三段式流程如下：

```
MATLAB 调用 cl_fix_round(a, a_fmt, r_fmt, round)
        │
        ▼
[第 1 段] wide.mat2py(a, a_fmt)   ── 数据：MATLAB → Python
        │   （按 a_fmt 是否 wide 分发到不同转换）
        ▼
[第 2 段] py.en_cl_fix_pkg.cl_fix_round(a_py, a_fmt, r_fmt, round)
        │   （真正算舍入，在 Python 里完成）
        ▼
[第 3 段] wide.py2mat(r, r_fmt)   ── 数据：Python → MATLAB
        │   （按 r_fmt 是否 wide 分发到不同转换）
        ▼
MATLAB 得到结果 r（double 或 fi）
```

注意 `a_fmt`、`r_fmt`、`round` 这些参数本身就是 Python 对象（由 `cl_fix_format.m`、`cl_fix_constants.m` 构造），所以它们在三个阶段之间原样透传，不需要翻译；真正需要翻译的只有**承载数值的数据** `a` 和 `r`。

#### 4.1.3 源码精读

先看 `cl_fix_round.m` 的三段式主体（注释行已对应到三段）：

[cl_fix_round.m:27-39](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/matlab/cl_fix_round.m#L27-L39) —— 这是整篇讲义的核心片段：

```matlab
% 第 0 步：记录输入的向量方向（见 4.1.3 末尾的形状补救）
is_column = iscolumn(varargin{1});
is_row    = isrow(varargin{1});

% 第 1 段：a = mat2py(a, a_fmt)   —— 调用 Python 前
varargin{1} = wide.mat2py(varargin{1}, varargin{2});

% 第 2 段：r = cl_fix_round(a, a_fmt, r_fmt, [round])
r = py.en_cl_fix_pkg.cl_fix_round(varargin{:});

% 第 3 段：r = py2mat(r, r_fmt)   —— 调用 Python 后
r = wide.py2mat(r, varargin{3});
```

这里有三个要点：

1. **第 1 段调用的是 `wide.mat2py`，不是独立的 `mat2py.m`。** `wide.mat2py` 是带格式参数 `a_fmt` 的「分发版」，它根据 `cl_fix_is_wide(a_fmt)` 决定走 narrow 还是 wide 路径。第 3 段同理调用 `wide.py2mat`。
2. **第 2 段用 `varargin{:}` 展开所有参数。** MATLAB 的 `varargin` 把可变参数收集成 cell 数组，`varargin{:}` 再展开回逗号分隔的实参列表，于是 Python 侧看到的就是 `(a, a_fmt, r_fmt, round)` 四个参数（`round` 可选）。
3. **翻译只发生在数据上，格式对象不动。** `varargin{2}`（`a_fmt`）、`varargin{3}`（`r_fmt`）、`varargin{4}`（`round`）在第 1、2、3 段里都是同一个 Python 对象引用，从不经过 `mat2py`。

接下来看分发逻辑。`wide.mat2py` / `wide.py2mat` 用 `cl_fix_is_wide` 在两条路径间二选一：

[wide.m:15-43](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/matlab/wide.m#L15-L43) —— 两个分发函数：

```matlab
function y = py2mat(x, x_fmt)
    if cl_fix_is_wide(x_fmt)
        y = wide.py2fi(x, x_fmt);   % wide：经 uint64 数组重建 fi()
    else
        y = py2mat(x);              % narrow：委托通用桥接（double）
    end
end

function y = mat2py(x, x_fmt)
    if cl_fix_is_wide(x_fmt)
        y = wide.fi2py(x);          % wide：fi() 拆成 uint64 数组再交给 Python
    else
        y = mat2py(x);              % narrow：委托通用桥接（numpy）
    end
end
```

> 注意命名重载：`wide.mat2py(x, x_fmt)`（两个参数，带格式分发）会进一步调用只接受一个参数的通用 `mat2py(x)`（即独立的 `mat2py.m`）。它们同名但签名不同，靠参数个数区分。

最后是一个真实工程细节——**向量形状补救**。封装作者发现 MATLAB↔Python 接口在处理向量时偶尔会出现形状错位，所以在转换前记录原始方向，转换后再强制还原：

[cl_fix_round.m:41-46](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/matlab/cl_fix_round.m#L41-L46)：

```matlab
if is_column
    r = r(:);               % 还原为列向量
elseif is_row
    r = reshape(r, 1, []);  % 还原为行向量
end
```

`cl_fix_random.m` 也有同源的补救（对标量输入一律压成列向量），见 [cl_fix_random.m:30-34](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/matlab/cl_fix_random.m#L30-L34)。这类「绕开宿主语言互操作 bug」的小补丁，正是薄封装比直连 `py.*` 多出来的价值所在。

#### 4.1.4 代码实践

**实践目标**：亲手验证「薄封装的三段式」与「直连 Python」的差别。

**操作步骤**：

1. 打开 [cl_fix_round.m:1-47](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/matlab/cl_fix_round.m#L1-L47)，把它的三段式用一句话分别标注在第 33、36、39 行旁边。
2. 再打开任意一个二元算术封装，例如 [`cl_fix_add.m`](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/matlab/cl_fix_add.m)，确认它也是同样的三段式骨架（只是输入多了 `b` 和 `b_fmt`，第 1 段要翻译两份数据）。
3. 思考：如果**跳过** `wide.mat2py` 和 `wide.py2mat`，直接写 `r = py.en_cl_fix_pkg.cl_fix_round(a, a_fmt, r_fmt, round)`，对 narrow 数据可能恰好能跑通（因为 MATLAB 会自动把 `double` 桥接成 numpy），但对 wide 数据会失败——为什么？（提示：MATLAB 不认识 Python 的 `object` 整数数组，且返回值无法变回 `fi()`。）

**需要观察的现象 / 预期结果**：你会看到所有 `cl_fix_*.m` 算术/转换封装高度同构，差异仅在「翻译几份数据」和「调用哪个 `py.en_cl_fix_pkg.*` 函数」。封装层本身没有任何定点算法逻辑。

> 本实践为源码阅读型，不需要运行 MATLAB；若要运行，需先按 4.2 完成环境初始化。

#### 4.1.5 小练习与答案

**练习 1**：`cl_fix_round.m` 里第 2 段 `py.en_cl_fix_pkg.cl_fix_round(varargin{:})` 的返回值 `r`，在进入第 3 段之前是什么类型（narrow 情况下）？

**参考答案**：是 Python 的 `numpy.ndarray`（`float64`）或 Python 标量。因为 Python 侧 narrow 数据用双精度浮点存储（见 u4-l1）。它还不是 MATLAB 类型，必须经第 3 段 `wide.py2mat` 翻译成 MATLAB `double` 才能被后续 MATLAB 代码使用。

**练习 2**：为什么 `a_fmt` 不需要经过 `mat2py` 翻译？

**参考答案**：因为 `a_fmt` 已经是 Python `FixFormat` 对象（由 `cl_fix_format.m` 调用 `py.en_cl_fix_pkg.FixFormat(...)` 构造，见 4.2.3）。MATLAB 持有的是该 Python 对象的引用，Python 函数本就认识它，无需任何类型转换。

---

### 4.2 环境初始化：pyenv、Python 路径与模块加载

#### 4.2.1 概念说明

三段式封装假设 Python 包 `en_cl_fix_pkg` 已经「就绪」——但这个就绪状态不会凭空产生。任何一个 MATLAB 脚本在第一次调用 `py.en_cl_fix_pkg.*` 之前，必须完成三件事：

1. **选定 Python 执行模式**：`pyenv` 决定 MATLAB 用「进程内（InProcess）」还是「进程外（OutOfProcess）」方式跑 Python。
2. **把 Python 源码目录加入 Python 路径**：让 `import en_cl_fix_pkg` 能找到仓库里的源码。
3. **导入模块**：`py.importlib.import_module('en_cl_fix_pkg')` 真正加载它。

此外还要把 MATLAB 源码目录加入 MATLAB 自己的 `path`，让 MATLAB 能找到那些 `.m` 封装。`matlab_example.m` 把这套环境初始化写得很完整，是理解「MATLAB 怎么连上 Python」的最佳样本。

#### 4.2.2 核心流程

```
pyenv 设置 (InProcess / OutOfProcess)
        │
        ▼
insert(py.sys.path, 0, python_src_path)   ── Python 能找到源码
        │
        ▼
py.importlib.import_module('en_cl_fix_pkg')  ── 加载模块
        │
        ▼
addpath(matlab_src_path)                   ── MATLAB 能找到 .m 封装
        │
        ▼
cl_fix_constants                           ── 预生成 Python 枚举常量
        │
        ▼
正常调用 cl_fix_*(...)
```

#### 4.2.3 源码精读

**执行模式选择**。MATLAB 调用 Python 有两种模式：`InProcess`（快，但 Python 模块加载后无法在同一个 MATLAB 会话里热重载）、`OutOfProcess`（慢，但可以重载，便于调试）。`matlab_example.m` 用一个开关 `RELOAD_PYTHON_MODULES` 来切换，默认走快的进程内模式：

[matlab_example.m:34-53](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/matlab/matlab_example.m#L34-L53)：

```matlab
RELOAD_PYTHON_MODULES = false;
pe = pyenv;
if RELOAD_PYTHON_MODULES
    ... terminate(pe); pe = pyenv(ExecutionMode='OutOfProcess');  % 调试用
else
    if strcmp(pe.Status, 'NotLoaded')
        pe = pyenv(ExecutionMode='InProcess');   % 默认：进程内
    else
        if strcmp(pe.ExecutionMode, 'OutOfProcess')
            error('MATLAB must be restarted to change the Python environment');
        end
    end
end
```

注释点明了关键约束：Python 环境**一旦加载就不能在进程内改**，所以切换模式需要重启 MATLAB。随后是一个健全性检查，确认 MATLAB 确实探测到了 Python：

[matlab_example.m:55-57](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/matlab/matlab_example.m#L55-L57)：`assert(pe.Version ~= "", ...)` 保证 `py.Version` 非空。

**Python 路径与模块加载**。这是让 `py.en_cl_fix_pkg` 变得可调用的关键两步：

[matlab_example.m:59-65](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/matlab/matlab_example.m#L59-L65)：

```matlab
root = fileparts(mfilename('fullpath'));
python_src_path = fullfile(root, '..', '..', 'models', 'python');
insert(py.sys.path, int32(0), python_src_path);     % 插到 Python sys.path 最前
py.importlib.import_module('en_cl_fix_pkg');         % 真正 import
```

`mfilename('fullpath')` 返回当前脚本绝对路径，`fileparts` 取其目录（即 `tests/matlab/`），再 `..\..\models\python` 回到 Python 源码根。`insert(py.sys.path, int32(0), ...)` 等价于 Python 的 `sys.path.insert(0, ...)`，把仓库源码目录置于最前，保证加载的是本仓库的 `en_cl_fix_pkg` 而非已安装的同名包。

> `int32(0)` 而非 `0`：因为 MATLAB 默认 `double` 传给 Python 会被当成浮点，而 `sys.path.insert` 要的是整数下标，所以显式转 `int32`。这与 `cl_fix_format.m` 里用 `int64(s)` 包裹是同一类「防止 MATLAB 把整数当成浮点传过去」的处理。

**MATLAB 路径与常量预生成**：

[matlab_example.m:67-76](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/matlab/matlab_example.m#L67-L76)：

```matlab
addpath(fullfile(root, '..', '..', 'models', 'matlab'));   % MATLAB 找得到 .m
...
cl_fix_constants;   % 预生成 Python 枚举常量
```

`cl_fix_constants.m` 提前在 MATLAB 里造好 Python 枚举对象，存进 struct `Sat` / `Round`：

[cl_fix_constants.m:28-40](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/matlab/cl_fix_constants.m#L28-L40)：

```matlab
Sat.None_s    = py.en_cl_fix_pkg.FixSaturate(0);
Sat.SatWarn_s = py.en_cl_fix_pkg.FixSaturate(3);
...
Round.Trunc_s     = py.en_cl_fix_pkg.FixRound(0);
Round.ConvEven_s  = py.en_cl_fix_pkg.FixRound(5);
```

这样脚本后续就能直接写 `round = Round.ConvEven_s`（见 [matlab_example.m:191](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/matlab/matlab_example.m#L191)），拿到一个现成的 Python 枚举对象传给封装函数。它的存在正是为了让 4.1 三段式里的 `round` 参数「拿来即用」。

**格式对象的构造**也遵循同一思路——`cl_fix_format.m` 直接调 Python 构造器：

[cl_fix_format.m:27](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/matlab/cl_fix_format.m#L27)：

```matlab
fmt = py.en_cl_fix_pkg.FixFormat(int64(s), int64(i), int64(f));
```

于是 `a_fmt = cl_fix_format(1, 0, 15)`（见 [matlab_example.m:78](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/matlab/matlab_example.m#L78)）得到的 `a_fmt` 就是一个 Python `FixFormat` 对象，符合 Python 侧构造器对 `S∈{0,1}`、`I+F≥0` 的断言（见 [en_cl_fix_types.py:61-70](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L61-L70)）。

#### 4.2.4 代码实践

**实践目标**：在 MATLAB 中跑通环境初始化，观察每一步的状态变化。

**操作步骤**：

1. 用 MATLAB 打开 `bittrue/tests/matlab/matlab_example.m`。
2. 在第 38 行 `pe = pyenv;` 后打断点，查看 `pe.Status` 与 `pe.ExecutionMode`。
3. 单步执行到第 65 行 `py.importlib.import_module(...)` 之后，在命令窗口执行 `py.en_cl_fix_pkg.cl_fix_is_wide`，确认模块确实已加载（应返回一个 Python 函数对象，而非报「未定义」）。
4. （可选）把 `RELOAD_PYTHON_MODULES` 改为 `true`，重新运行，观察它如何切到 `OutOfProcess`，并体会注释所说的「慢但可热重载」。

**需要观察的现象 / 预期结果**：脚本最终打印 `Detected Python version: ...` 与 `Success: All tests passed.`。若 `pe.Version` 为空则第 56 行 assert 失败——这说明 MATLAB 没探测到任何 Python，需先用 `pyenv('Version', '...')` 指定 Python 可执行文件。

> 待本地验证：实际运行需要 MATLAB R2023b 及配套 Python 环境；本环境无法执行 MATLAB，请读者在本地确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `insert(py.sys.path, int32(0), python_src_path)` 要用 `int32(0)` 而不是直接写 `0`？

**参考答案**：MATLAB 数值字面量默认是 `double`。若直接传 `0`，Python 侧收到的是浮点 `0.0`，而 `sys.path.insert` 的第一个参数（位置下标）必须是整数，会引发类型错误。用 `int32(0)` 显式表明这是整数下标，MATLAB 才会把它桥接成 Python `int`。

**练习 2**：`cl_fix_constants.m` 注释说「这种加载常量的方式在 MATLAB 里很慢」。既然慢，为什么还要这么做？

**参考答案**：因为封装函数需要的 `round`/`saturate` 参数必须是 Python `FixRound`/`FixSaturate` 枚举对象。提前一次性造好并存进 `Sat`/`Round` struct，后续每次调用直接引用即可，避免在每次调用时重复构造 Python 对象（跨语言构造开销大）。这是一次性付出换长期复用。

---

### 4.3 wide 数据的打包与解包：uint64 数组作为跨语言「线缆格式」

#### 4.3.1 概念说明

narrow 数据的翻译几乎是恒等映射（MATLAB `double` ↔ Python `float64`，都是 IEEE754 双精度），交给通用桥接 `mat2py.m`/`py2mat.m` 即可。wide 数据则棘手得多：Python 端用 **任意精度整数**（`dtype=object` 的 Python `int`，理论上无位数上限）保存 wide 定点数，而 MATLAB 没有原生任意精度整数——只有 Fixed-Point Designer 工具箱的 `fi()` 对象。

两边都无法直接理解对方的 wide 表示，于是设计者引入了一个**中间表示**：**uint64 数组**。它既是 MATLAB 原生类型（`uint64`），又能被 Python 当成 `numpy.uint64` 数组处理，是两侧都认识的「线缆格式」（wire format）。整个 wide 翻译就是两次「表示切换」：

- MATLAB `fi()` ⇄ **uint64 数组** ⇄ Python 任意精度整数数组

这正好对应 `matlab_interface.py` 的两个函数：`to_uint64_array`（Python 大整数 → uint64 数组，发给 MATLAB）和 `from_uint64_array`（uint64 数组 → Python 大整数，收回 MATLAB）。`wide.m` 里的私有 `py2fi`/`fi2py` 则负责 `fi()` ⇄ uint64 数组 这一侧。

#### 4.3.2 核心流程

wide 数据从 MATLAB 进 Python 再回 MATLAB 的完整往返（以一次 `cl_fix_round` wide 调用为例）：

```
MATLAB fi(a)
   │  wide.fi2py：reinterpretcast 成无符号，每 64 位 quantize 切片
   ▼
uint64 数组 (MATLAB)
   │  mat2py：桥接成 numpy.uint64 数组
   ▼
uint64 数组 (Python/numpy)
   │  py.en_cl_fix_pkg.from_uint64_array：加权求和 + 处理符号位
   ▼
Python 任意精度整数数组   ──►  cl_fix_round 真正计算  ──►
   │
   ▼  （返回时反向）
Python 任意精度整数数组
   │  py.en_cl_fix_pkg.to_uint64_array：重解释为无符号 + 按 64 位切片
   ▼
uint64 数组 (Python/numpy)
   │  uint64() + fi 累加 + reinterpretcast（wide.py2fi）
   ▼
MATLAB fi(r)
```

#### 4.3.3 源码精读

**`to_uint64_array`：为什么先重解释为无符号、再按 64 位切片**

这是本讲最需要想清楚的一段代码。先看全貌：

[matlab_interface.py:6-38](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/matlab_interface.py#L6-L38)：

```python
def to_uint64_array(data, fmt : FixFormat):
    ...
    # 计算每个元素需要几个 uint64
    n_ints = (fmt.width + 63) // 64          # ceil(width / 64)

    # 把有符号数据重解释为无符号（处理符号位）
    if fmt.S == 1:
        data = np.where(data < 0, data + 2**fmt.width, data)

    # 填充 uint64 数组
    result = np.empty(data.shape + (n_ints,), dtype=np.uint64)
    for i in range(n_ints):
        result[..., i] = data % 2**64        # 取低 64 位
        data >>= 64                          # 右移 64 位
    return result
```

逐行解读：

1. **`n_ints = (fmt.width + 63) // 64`** 是向上取整的位宽÷64，给出每个元素要切成几段 64 位「肢（limb）」。例如位宽 65 → `n_ints = 2`，位宽 4 → `n_ints = 1`。
2. **重解释符号位**（关键）：若格式有符号（`fmt.S == 1`），把所有负数 `d` 改写成 `d + 2**fmt.width`。
3. **切片循环**：反复「取低 64 位、再右移 64 位」，把一个大整数分解成一组 uint64。

**为什么第 2 步「重解释为无符号」是必须的？** 这正是练习要回答的核心。原因在于 **Python 的 `int` 是无限精度有符号数**，它的 `%` 与 `>>` 都按「数学上有符号」语义运作，而不是按「定宽补码」语义。

- Python 的 `%` 对负数返回**非负**结果：\((-1) \bmod 2^{64} = 2^{64}-1\)。
- Python 的 `>>` 是**算术右移**（符号扩展）：\((-1) \gg 64 = -1\)。

设想一个位宽为 4 的有符号格式，值是 \(-1\)（4 位补码应为 `1111`，即无符号 15）。若**不做**重解释，直接进切片循环：\(n\_ints=1\)，于是 `result[0] = (-1) % 2**64 = 2**64-1`，得到 `0xFFFFFFFFFFFFFFFF`——这是「64 位全 1」，根本不是 4 位的 `0xF`。错的根因是 Python 把 \(-1\) 当成了「无限多个前导 1」的数。

做了重解释后：\(-1 + 2^4 = 15\)，再 `15 % 2**64 = 15 = 0xF`，高位全零，恰好是 4 位补码 `1111` 装进 64 位 limb 的正确结果。

用公式说更清楚。设位宽 \(W\)，一个有符号值 \(v\) 的 \(W\) 位补码无编码（即它对应的 \([0, 2^W)\) 区间无符号整数）为：

\[
u(v) = v \bmod 2^W =
\begin{cases}
v, & v \ge 0 \\
v + 2^W, & v < 0
\end{cases}
\]

`to_uint64_array` 第 2 步做的正是 \(v \mapsto u(v)\)（仅对负数加 \(2^W\)）。完成这一步后，数据进入 \([0, 2^W)\)，再按 64 位切片就只是把**确定位宽**的无编码拆成肢体，高位自然为零，不会发生符号扩展。

> 一句话总结：**重解释是为了把「无限精度有符号 Python 整数」固定成「\(W\) 位补码的无符号编码」，使后续按 64 位切片得到正确的、定宽的位串。**

**`from_uint64_array`：反向还原**

[matlab_interface.py:40-58](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/python/en_cl_fix_pkg/matlab_interface.py#L40-L58)：

```python
def from_uint64_array(data, fmt : FixFormat):
    ...
    # 加权求和：把若干 uint64 重新组合成一个大无符号整数
    weights = 2**(64*np.arange(data.shape[-1]).astype(object))
    result = np.matmul(data, weights.T)

    # 处理符号位：把符号位置位的值还原为负数
    if fmt.S == 1:
        result = np.where(result >= 2**(fmt.I+fmt.F),
                          result - 2**(fmt.I+fmt.F+1), result)
    return result
```

这是 `to_uint64_array` 的逆运算：

- 加权求和：第 \(k\) 个 limb 的权重是 \(2^{64k}\)，矩阵乘法把它们重新叠加成一个无符号大整数，对应公式 \(u = \sum_k \text{limb}_k \cdot 2^{64k}\)。
- 还原符号：对有符号格式，若 \(u \ge 2^{I+F}\)（即最高位/符号位为 1，注意 \(W = S+I+F\)，故 \(I+F = W-1\)，\(2^{I+F}\) 正是符号位的权重），则减去 \(2^{I+F+1} = 2^W\)，把 \(u\) 从 \([0, 2^W)\) 映射回 \((-2^{W-1}, 2^{W-1}]\) 的有符号区间。这与 `to_uint64_array` 的 \(+2^W\) 互为逆操作，符号门槛 \(2^{I+F}=2^{W-1}\) 与 limb 数无关，是按格式的位宽定的。

**MATLAB 侧的 `fi()` ⇄ uint64 数组**。对称地，`wide.m` 的两个私有方法负责另一侧：

- `wide.fi2py`（[wide.m:131-161](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/matlab/wide.m#L131-L161)）：先用 `reinterpretcast` 把 `fi()` 当成无符号整数，再用 `quantize` 每次取 64 位、`pow2(x,-64)` 等价右移 64，填出一个 MATLAB `uint64` 数组，最后交给 `from_uint64_array`。
- `wide.py2fi`（[wide.m:93-129](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/models/matlab/wide.m#L93-L129)）：先调 `to_uint64_array` 拿到 uint64 数组，转成 MATLAB `uint64`，用 `SumMode='KeepLSB'` 把多个 64 位切片按 \(2^{64(k-1)}\) 权重累加成一个宽 `fi`，最后 `reinterpretcast` 回有符号 `numerictype(s,w,f)`。

两侧通过 **uint64 数组**这个共同语言对接，各自只需关心「自己类型 ⇄ uint64 数组」的转换，互不耦合。

#### 4.3.4 代码实践

**实践目标**：用纯 Python 复现 `to_uint64_array` 的核心逻辑，亲手验证「重解释符号位」的必要性。

**操作步骤**（在装了 numpy 与本仓库 Python 源码的环境里）：

1. 把仓库 `bittrue/models/python` 加入 `PYTHONPATH`，进入 Python。
2. 运行下面这段**示例代码**（非项目原有代码）：

```python
# 示例代码：手动复现 to_uint64_array 的关键两步
import numpy as np
from en_cl_fix_pkg import FixFormat, cl_fix_is_wide
from en_cl_fix_pkg.matlab_interface import to_uint64_array, from_uint64_array

fmt = FixFormat(1, 0, 3)          # 有符号，位宽 W = 1+0+3 = 4
print("is_wide =", cl_fix_is_wide(fmt))   # 预期 False（4 <= 53），仅用于演示切片逻辑

# 构造一个 wide 格式才能真正调用 to_uint64_array（它要求 cl_fix_is_wide 为真）
wide_fmt = FixFormat(1, 0, 64)    # 位宽 65，wide
data = np.array([-1], dtype=object)

# 关键对比：不重解释 vs 重解释（位宽 4 的 -1）
W = 4
v = -1
no_reinterpret = v % (2**64)              # Python 直接取模：2**64-1（错！）
reinterpret   = (v + 2**W) % (2**64)       # 先加 2**W 再取模：15（对，= 0xF）
print("不重解释:", no_reinterpret, " 重解释:", reinterpret)
```

3. 把 `wide_fmt` 与 `data` 喂给真正的 `to_uint64_array`，再用 `from_uint64_array` 读回，断言往返无损：

```python
# 示例代码：往返测试
packed = to_uint64_array(data, wide_fmt)
restored = from_uint64_array(packed, wide_fmt)
assert restored[0] == -1, "往返失真！"
print("往返成功，packed shape =", packed.shape)
```

**需要观察的现象 / 预期结果**：

- 「不重解释」打印 `18446744073709551615`（即 \(2^{64}-1\)），「重解释」打印 `15`。前者是 64 位全 1，后者才是 4 位补码 `1111`。
- 往返测试通过，`packed.shape` 末维等于 `n_ints`（位宽 65 时为 2）。

> 待本地验证：本环境未安装 MATLAB/numpy 运行时，以上为依据源码推演的预期结果，请读者本地确认。

#### 4.3.5 小练习与答案

**练习 1**：位宽 \(W=4\)、有符号、值为 \(-2\) 时，`to_uint64_array` 重解释后会得到什么无编码？它的 4 位补码位串是什么？

**参考答案**：\(-2 + 2^4 = 14\)，即无编码 14；4 位补码位串为 `1110`（\(-2 = -8+4+2\)）。验证：`14 % 2**64 = 14`，切片后得到单个 uint64 `14`，正确。

**练习 2**：`from_uint64_array` 里符号还原的门槛为什么是 `2**(fmt.I+fmt.F)` 而不是 `2**fmt.width`？

**参考答案**：对有符号格式 \(W = S+I+F = 1+I+F\)，故 \(I+F = W-1\)，\(2^{I+F} = 2^{W-1}\) 正是**符号位**的权重。值落在 \([2^{W-1}, 2^W)\) 即「符号位置 1」，需要减 \(2^W\) 还原为负。若误用 \(2^W\) 作门槛，则没有任何值会触发还原，所有数都会被当成非负——这与 `to_uint64_array` 的编码不对称，往返就会失真。

**练习 3**：为什么 narrow 路径不需要这套 uint64 打包机制？

**参考答案**：narrow 数据位宽 ≤ 53，完全装得进 IEEE754 双精度浮点的有效位（mantissa 52 位 + 隐含 1 位 = 53 位）。MATLAB 的 `double` 与 Python 的 `float64` 同构，通用桥接 `mat2py.m`/`py2mat.m` 直接搬运即可，无需任何重打包，也不需要 Fixed-Point Designer 工具箱。uint64 机制专门为「超出 53 位、必须用任意精度整数」的 wide 情形而设。

---

## 5. 综合实践

**任务**：跟踪一次 wide 定点舍入的**完整跨语言往返**，把本讲三个最小模块串起来。

背景数据：复用 `matlab_wide_example.m` 的设置，输入格式 `a_fmt = cl_fix_format(1, 50, 65)`（位宽 \(1+50+65=116\)，明显 wide），对它做一次收敛舍入 `cl_fix_round(a, a_fmt, round_fmt, Round.ConvEven_s)`，其中 `round_fmt = cl_fix_round_fmt(a_fmt, a_fmt.F - 4, round)`。

请按下列步骤完成：

1. **环境层（对应 4.2）**：列出 `matlab_wide_example.m` 在调用任何 `cl_fix_*` 之前必须完成的 4 件事（执行模式、Python 路径、模块加载、MATLAB 路径+常量），并指出它和 narrow 版 `matlab_example.m` 在环境初始化上**有无差别**。（提示：完全相同；差别只在测试用的格式位宽。）
2. **封装层（对应 4.1）**：写出 `cl_fix_round.m` 对这次 wide 调用所做的两次数据转换的「类型轨迹」：
   - 调用 Python 前：`a`（MATLAB `fi`）→ `wide.mat2py` → 因 `cl_fix_is_wide(a_fmt)` 为真 → `wide.fi2py` → …… → Python 任意精度整数数组。
   - 调用 Python 后：Python 任意精度整数数组 → `wide.py2mat` → 因 `cl_fix_is_wide(round_fmt)` 为真 → `wide.py2fi` → …… → MATLAB `fi`。
3. **打包层（对应 4.3）**：计算 `a_fmt`（位宽 116）需要几个 uint64 limb；并解释在这次往返中，`to_uint64_array` 对负的 `a` 元素具体做了什么（加 \(2^{116}\)），`from_uint64_array` 又如何用门槛 \(2^{I+F}=2^{115}\) 把它们还原。
4. **验证**：解释为什么 `matlab_wide_example.m` 里 [matlab_wide_example.m:142-143](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/matlab/matlab_wide_example.m#L142-L143) 的 `assert(cl_fix_is_wide(a_fmt), ...)` 是这次往返走 wide 路径（而非 narrow）的判据，并由此说明 narrow/wide 两条路径的切换完全由**格式位宽**自动决定、对调用者透明。

**预期产出**：一张标注了「MATLAB 类型 ⇄ uint64 数组 ⇄ Python 类型」三栏、含 limb 数与符号位公式的往返数据流图。

> 待本地验证：完整运行需要 MATLAB + Fixed-Point Designer 工具箱 + Python 环境；本实践以源码追踪为主，可在不运行的情况下完成 1–4 的推理。

## 6. 本讲小结

- en_cl_fix 的 MATLAB 端是**纯薄封装**：每个 `cl_fix_*.m` 都遵循 `wide.mat2py → py.en_cl_fix_pkg.* → wide.py2mat` 三段式，自身不含任何定点算法。
- 真正承载数值的数据在调用前后各被翻译一次；而 `FixFormat`、`FixRound`、`FixSaturate` 等 Python 对象原样透传，无需翻译。
- `wide.mat2py`/`wide.py2mat` 用 `cl_fix_is_wide(fmt)` 在 narrow/wide 两条路径间自动分发，**切换对调用者透明**。
- narrow 路径几乎恒等（MATLAB `double` ↔ Python `float64`），交给通用桥接 `mat2py.m`/`py2mat.m`，**不需要** Fixed-Point Designer 工具箱。
- wide 路径以 **uint64 数组**为中间「线缆格式」，连接 MATLAB `fi()` 与 Python 任意精度整数，由 `matlab_interface.to_uint64_array`/`from_uint64_array` 与 `wide.py2fi`/`wide.fi2py` 协同完成。
- `to_uint64_array` 之所以「先把有符号整数重解释为无符号、再按 64 位切片」，是因为 Python `int` 是**无限精度有符号数**，其 `%`/`>>` 会符号扩展；只有先加 \(2^W\) 把值固定到 \([0,2^W)\) 的定宽补码无编码，切片才能得到正确位串。
- 环境初始化（`pyenv`、`sys.path` 插入、`import_module`、`addpath`、`cl_fix_constants`）是任何 `py.en_cl_fix_pkg.*` 调用的前提，`matlab_example.m` 是其标准模板。

## 7. 下一步学习建议

- **横向对比 narrow 与 wide**：精读 [`matlab_wide_example.m`](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/matlab/matlab_wide_example.m)，与 narrow 版逐段对照，体会「同一套封装、两条数据路径」的设计。
- **向下深入 Python 表示层**：回顾 u4-l2（NarrowFix，双精度浮点实现）与 u4-l3（WideFix，任意精度整数实现），理解本讲搬运的「裸数据」在 Python 内部究竟如何参与运算。
- **向旁扩展到验证闭环**：本讲讲的是「MATLAB 调 Python 做参考计算」，可与 u7 系列讲的「Python cosim 生成黄金数据 + VHDL 测试台比对」对照——两者都把 Python 当作金标准参考模型，只是消费方不同（MATLAB 工程师 vs. VHDL 仿真器）。
- **动手改造**：尝试为某个尚无 MATLAB 封装的辅助函数（如有）按三段式补一个 `.m` 薄封装，借此检验你是否真正掌握了「翻译数据 + 透传格式对象」的模式。
