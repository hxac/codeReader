# 快速上手：运行 Python 与 MATLAB 测试

## 1. 本讲目标

本讲是单元 1 的最后一课，目标只有一个：**把环境跑起来**。

学完本讲，你应该能够：

1. 用 `pip` 根据 `requirements.txt` 安装 en_cl_fix 的 Python 依赖。
2. 在本地运行 `bittrue/tests/python/` 下的两类测试脚本，并读懂它们的输出。
3. 看懂 `bittrue/tests/matlab/matlab_example.m` 这个 MATLAB 示例，了解 MATLAB 如何经 Python 接口调用同一个定点库。

本讲**不深入定点算法本身**（舍入、饱和的实现留在单元 4），只关心「怎么装、怎么跑、跑出来的结果怎么读」。

---

## 2. 前置知识

在进入本讲之前，你应当已经具备（来自 u1-l1 ~ u1-l3）：

- **三语言同构**：en_cl_fix 的 VHDL / Python / MATLAB 三套实现，函数名一一对应（如 `cl_fix_round`、`cl_fix_add`）。本讲主要接触 Python 与 MATLAB 两侧。
- **Python 包结构**：所有 `cl_fix_*` 函数都从 Python 包 `en_cl_fix_pkg` 导出，测试脚本靠 `from en_cl_fix_pkg import *` 拿到它们（见 u1-l3 的 `__init__.py` 门面）。
- **`[S, I, F]` 格式**：符号位、整数位、小数位，总位宽 `S+I+F`（见 u1-l2）。本讲会看到诸如 `FixFormat(True, 2, 2)` 这样的构造，不必纠结其内部算法。

此外需要一点 Python 基础：

- **`unittest`**：Python 标准库的测试框架，用 `assertEqual(期望值, 实际值)` 断言，失败抛 `AssertionError`。
- **`numpy`**：数值计算库，en_cl_fix 用它做向量化运算（一个函数同时处理成千上万个数）。
- **`sys.path`**：Python 查找模块的搜索路径列表，往里 `append` 一个目录就能 `import` 到该目录下的包。

> 如果你对「定点数」这个词还陌生，只需知道：它是一种小数点位置固定、用整数+比例因子表示小数的方式，本讲把它当成一个能加减乘的「数字」即可。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `requirements.txt` | 钉死（pin）Python 依赖的精确版本，一条命令安装全部依赖。 |
| `bittrue/tests/python/en_cl_fix_pkg_test.py` | **unittest 风格**测试，按运算分类、逐个断言，覆盖 `cl_fix_width`/`cl_fix_resize`/`cl_fix_add` 等几十个用例。 |
| `bittrue/tests/python/cl_fix_round_test.py` | **穷举对拍风格**测试，遍历所有格式组合，把 `cl_fix_round` 的结果与独立 numpy 参考实现逐位比对。 |
| `bittrue/tests/python/cl_fix_saturate_test.py` | 与 round 测试同风格的饱和测试（本讲略读，留作扩展阅读）。 |
| `bittrue/tests/matlab/matlab_example.m` | MATLAB 示例脚本，演示如何在 MATLAB 里经 Python 接口调用 en_cl_fix 并自检结果。 |
| `README.md` | 顶层说明，含「Running Tests」一节，给出官方运行命令。 |

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：依赖管理、Python 的 unittest 风格测试、Python 的穷举对拍测试、MATLAB 测试入口。

### 4.1 依赖管理：requirements.txt 与一键安装

#### 4.1.1 概念说明

要让一个 Python 项目「跑起来」，第一步通常是**安装依赖**——也就是项目用到了哪些第三方库。en_cl_fix 把依赖写在一个叫 `requirements.txt` 的文件里，这是一种被 `pip` 广泛支持的约定：文件里每一行写一个包名（可带版本号），用一条命令就能批量安装。

这里有两个容易混淆的词先说清楚：

- **numpy**：科学计算库，en_cl_fix 用它实现向量化定点运算（一个操作同时作用于整个数组）。
- **vunit-hdl**：VHDL 仿真框架，**只在跑 VHDL 仿真时才需要**（单元 8 会用到）。本讲只跑纯 Python 测试，理论上 numpy 是必须的，vunit-hdl 不是必须，但按 `requirements.txt` 一起装最省事。

#### 4.1.2 核心流程

安装依赖的流程是：

1. 确认已装 Python 3（README 标注测试过 `>= 3.10`）。
2. 在**仓库根目录**执行 `python -m pip install -r requirements.txt`。
3. `pip` 读取文件，逐行解析，安装 `numpy` 与 `vunit-hdl`。

```
仓库根目录
  ├── requirements.txt   ← pip 读取它
  └── python -m pip install -r requirements.txt   ← 一键安装
```

#### 4.1.3 源码精读

`requirements.txt` 全文只有两行，每行钉死（pin）一个精确版本：

[requirements.txt:1-2](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/requirements.txt#L1-L2) —— 用 `==` 钉死 `numpy==2.3.2` 和 `vunit-hdl==5.0.0.dev6`。版本号被「钉死」是为了保证库的开发者与你本地跑出**完全一致**的行为；`5.0.0.dev6` 是一个开发预发布版本（dev release）。

> README 同时给出了**最低版本**说明（而非钉死版本）：

[README.md:46-49](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/README.md#L46-L49) —— Python 3（`>= 3.10`）、numpy（`>= 1.24.3`）、vunit-hdl（`>= 5.0.0.dev6`）。也就是说：`requirements.txt` 是「精确复现」，README 这段是「最低门槛」。

README 的安装命令在这里：

[README.md:51-54](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/README.md#L51-L54) —— 官方推荐 `python -m pip install -r requirements.txt`。

#### 4.1.4 代码实践

1. **实践目标**：确认依赖安装成功，Python 能 `import numpy`。
2. **操作步骤**：
   - 在仓库根目录运行：`python -m pip install -r requirements.txt`
   - 再运行一行自检（一行 Python）：`python -c "import numpy; print('numpy', numpy.__version__)"`
3. **需要观察的现象**：第一条命令打印安装/已满足的日志；第二条打印出 numpy 版本号。
4. **预期结果**：第二条应打印 `numpy 2.3.2`（与你刚装的版本一致）。若报 `ModuleNotFoundError`，说明 `pip` 装到的 Python 与你运行脚本用的 Python 不是同一个，需检查 `python` 指向。
5. **运行结果**：待本地验证（不同机器的 `pip` 输出不同，以本机实际为准）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `requirements.txt` 里的 `==` 改成 `>=`，安装行为会有什么不同？

**参考答案**：`==` 要求精确版本，`pip` 只装那一个版本；`>=` 允许装任何「大于等于」的版本，`pip` 会挑当前能满足的最新版。后者更灵活，但可能因新版本行为变化而导致测试结果不一致——这正是 en_cl_fix 用 `==` 钉死版本的原因。

**练习 2**：为什么 `vunit-hdl` 是开发预发布版（`5.0.0.dev6`）也能被 `pip` 正常安装？

**参考答案**：`pip` 默认只装正式版，但 `requirements.txt` 里写明了带 `dev` 的完整版本号，`pip install -r` 会按文件指定的精确版本安装，包括预发布版。若用 `pip install vunit-hdl`（不带版本）则默认不会选预发布版。

---

### 4.2 Python 测试入口（一）：unittest 风格逐例断言

#### 4.2.1 概念说明

`bittrue/tests/python/` 下有两种风格的测试脚本，先讲第一种：**unittest 风格**，代表文件是 `en_cl_fix_pkg_test.py`。

它的特点是「**人工挑选典型用例 + 逐个断言**」：测试人员针对每个函数（如 `cl_fix_resize`）手写若干个「输入 + 期望输出」对，用 `assertEqual(期望, 实际)` 一条条核对。这跟多数 Python 项目的测试长得很像，容易读、容易定位是哪个用例挂了。

#### 4.2.2 核心流程

```
脚本入口 __main__
   │
   ├── sys.path.append("../../models/python")   # 把模型源码目录加入搜索路径
   ├── from en_cl_fix_pkg import *              # 拿到所有 cl_fix_* 函数与类型
   │
   └── unittest.main()                          # 自动发现并运行所有 *Test 类
            │
            ├── cl_fix_width_Test         (7 个 test_ 方法)
            ├── cl_fix_resize_Test        (40+ 个 test_ 方法)
            ├── cl_fix_add_Test / sub / mult / neg / abs / shift ...
            └── 每个方法内 assertEqual(期望, 实际) → 全过则 OK，一个失败即抛 AssertionError
```

#### 4.2.3 源码精读

测试脚本如何找到模型源码？靠的是改 `sys.path`：

[en_cl_fix_pkg_test.py:23-25](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py#L23-L25) —— `sys.path.append("../../models/python")` 把模型目录加进搜索路径，随后 `from en_cl_fix_pkg import *` 拿到全部公开符号。

> ⚠️ **一个真实的坑**：这里的 `"../../models/python"` 是**相对于当前工作目录（CWD）**的路径，不是相对于脚本文件位置。也就是说，这个脚本必须**在 `bittrue/tests/python/` 目录内运行**（或 CWD 恰好能让该相对路径指向模型目录），否则 `import` 会失败。这是它与下一个脚本的显著差别（见 4.3）。

测试以「一个运算 = 一个测试类」的方式组织：

[en_cl_fix_pkg_test.py:34-37](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py#L34-L37) —— `cl_fix_width_Test` 类验证位宽函数：`cl_fix_width(FixFormat(False, 3, 0))` 应等于 `3`（无符号 3 整数位 = 3 位）。

最典型的「输入 + 期望」对，看 resize 类里一个用例：

[en_cl_fix_pkg_test.py:118-119](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py#L118-L119) —— `test_RemoveFracBit1_Round`：把 `2.5`（格式 `[1,2,1]`）resize 到 `[1,2,0]`（砍掉 1 位小数位）、用 `NonSymPos_s` 舍入，期望得到 `3.0`（因为 0.5 向正向进位为 1，2+1=3）。

入口处把控制权交给 unittest：

[en_cl_fix_pkg_test.py:758-759](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py#L758-L759) —— `unittest.main()` 自动扫描当前文件里所有继承 `unittest.TestCase` 的类，运行以 `test_` 开头的方法。

#### 4.2.4 代码实践

1. **实践目标**：用 unittest 风格脚本验证库可用，并读懂一个断言。
2. **操作步骤**：
   - 进入测试目录：`cd bittrue/tests/python`（注意：必须在此目录，原因见上面的「坑」）。
   - 运行：`python en_cl_fix_pkg_test.py`
3. **需要观察的现象**：终端会打印 `unittest` 风格的报告，形如 `Ran N tests in X.Xs` 以及 `OK` 或 `FAILED (failures=...)`。
4. **预期结果**：末尾出现 `OK`，表示全部断言通过；`N` 为该文件中所有 `test_` 方法的总数。
5. **运行结果**：待本地验证（具体测试数量以本机实际打印为准）。

> 选读断言：挑 `cl_fix_resize_Test.test_RemoveFracBit1_Round`（[L118-L119](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/en_cl_fix_pkg_test.py#L118-L119)）来读——它体现了「砍小数位 → 舍入」这一核心操作，是单元 4 舍入机制的预告。

#### 4.2.5 小练习与答案

**练习 1**：为什么不写 `cd` 直接在仓库根目录运行 `python bittrue/tests/python/en_cl_fix_pkg_test.py` 很可能失败？

**参考答案**：因为脚本第 24 行用的是 CWD 相对路径 `"../../models/python"`。从仓库根目录运行时，CWD 是根目录，`../../models/python` 会指向根目录的上两级，找不到 `en_cl_fix_pkg` 包，`from en_cl_fix_pkg import *` 抛 `ModuleNotFoundError`。

**练习 2**：`unittest.main()` 是怎么知道要跑哪些方法的？

**参考答案**：它通过反射（reflection）扫描当前模块，找出所有继承 `unittest.TestCase` 的类，再在每个类里找名字以 `test_` 开头的方法依次运行；非 `test_` 前缀的方法（如辅助用的 `RunGetTest`）默认不会被当作测试用例执行。

---

### 4.3 Python 测试入口（二）：穷举对拍风格（本讲主实践）

#### 4.3.1 概念说明

第二种风格更「暴力」也更严谨，代表是 `cl_fix_round_test.py`（**本讲的主实践脚本**）。它的思路是：

> 既然定点数取值范围有限，那就**把每种格式下所有可能的值都试一遍**，并且把 en_cl_fix 的输出和一份**用 numpy 独立写出来的参考实现**逐位比对。

这叫**穷举对拍（exhaustive differential testing）**：用两个相互独立的实现做同一件事，若结果完全一致，就极大地提高了可信度。它不靠人工挑用例，而是靠「全覆盖」。

文件头部点明了目的：

[cl_fix_round_test.py:21-24](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_round_test.py#L21-L24) —— 说明本脚本「用标准 Python（numpy）实现来测试 `cl_fix_round`」。

#### 4.3.2 核心流程

```
1. 配置测试网格（Config）
      aS ∈ {0,1}                 # 无符号 / 有符号
      aI ∈ [-4, +4]              # 整数位扫描
      aF ∈ [-4, +4]              # 小数位扫描
      rF ∈ [-4, +4]              # 结果小数位扫描
      rnd ∈ 7 种 FixRound        # 七种舍入模式

2. 三层 aS/aI/aF 循环
      ├─ 跳过非法格式 (S+I+F <= 0)
      ├─ get_data(a_fmt)：枚举该格式下所有可能取值（从 min 到 max 的计数器）
      │
      └─ 两层 rF/rnd 循环
            ├─ r_fmt = FixFormat.for_round(...)  # 推导合法结果格式，非法就 continue
            ├─ r      = cl_fix_round(a, ...)     # 待测函数（被验证方）
            ├─ r_wide = WideFix 路径同算一次     # 第二份独立实现
            ├─ expected = round_check(a, ...)    # numpy 参考实现（第三份独立实现）
            └─ assert 三者两两相等              # 任意一个不一致就抛 AssertionError
3. 全部通过后打印 "Completed N tests."
```

它实际上用了**三套相互独立的实现**互相核对：en_cl_fix 的 NarrowFix 路径、WideFix 路径、以及 numpy 手写的参考——三者结果必须完全一致。

#### 4.3.3 源码精读

**与上一个脚本的关键差别——健壮的路径处理**：

[cl_fix_round_test.py:29-34](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_round_test.py#L29-L34) —— 这里用 `root = dirname(__file__)` 取**脚本自身所在目录**，再拼 `../../models/python`。因此**无论你从哪个 CWD 运行它都能正确找到模型包**。这就是为什么本讲的主实践命令 `python bittrue/tests/python/cl_fix_round_test.py`（从仓库根目录运行）能直接成功。

**生成「该格式下全部取值」**：

[cl_fix_round_test.py:42-47](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_round_test.py#L42-L47) —— `get_data` 把格式最小值到最大值做成一个计数器（`np.arange(int_min, 1+int_max)`），再转回定点。这就是「穷举」：小格式下每个值都不遗漏。

**numpy 参考实现——七种舍入的对照表**：

[cl_fix_round_test.py:61-78](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_round_test.py#L61-L78) —— `round_check` 为每种 `FixRound` 模式用 numpy 的 `floor/ceil/around` 写一份独立的舍入逻辑。例如 `Trunc_s`（截断）= `floor`，`ConvEven_s`（向偶收敛）= `np.around`。这 7 个名字（`Trunc_s / NonSymPos_s / NonSymNeg_s / SymInf_s / SymZero_s / ConvEven_s / ConvOdd_s`）就是单元 4 要详讲的七种舍入模式，这里先混个脸熟。

**测试网格配置**：

[cl_fix_round_test.py:84-90](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_round_test.py#L84-L90) —— 用 `np.arange(-4, 1+4)` 生成整数/小数位的扫描范围，配合 `aS_values=[0,1]` 和 7 种舍入，构成一个多维测试网格。

**核心对拍循环**：

[cl_fix_round_test.py:122-134](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_round_test.py#L122-L134) —— 对每个合法的 `(a_fmt, r_fmt, rnd)` 组合：调用待测的 `cl_fix_round`（[L122](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_round_test.py#L122)）、用 WideFix 再算一遍（[L125](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_round_test.py#L125)）、用 numpy 参考验算（[L128](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_round_test.py#L128)），最后两条 `assert np.array_equal(...)`（[L131-L132](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_round_test.py#L131-L132)）要求三者完全相等，`test_count` 加一。

**收尾输出**：

[cl_fix_round_test.py:96](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_round_test.py#L96) 与 [cl_fix_round_test.py:135](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_round_test.py#L135) —— `test_count` 计数，最后 `print(f"Completed {test_count} tests.")`。注意它**不用 unittest**：没有 `OK/FAILED`，靠「能跑到最后一行打印 = 全过；中途任何 `assert` 失败 = 立即抛异常中断」来判定。

#### 4.3.4 代码实践（本讲主实践）

> 这正是本讲规格里要求的实践任务。

1. **实践目标**：运行穷举对拍脚本，记录通过/失败情况，并解释其中一个断言用例的含义。
2. **操作步骤**：
   - 在仓库根目录运行：`python bittrue/tests/python/cl_fix_round_test.py`
     （该脚本路径处理健壮，无需 `cd`。）
   - 等待运行结束（穷举，会比 unittest 那个慢一些）。
3. **需要观察的现象**：
   - 成功：最后一行打印 `Completed <N> tests.`，进程退出码为 0。
   - 失败：中途打印 `AssertionError: Numerical error detected.`（或 `(WideFix)` 版本），并带堆栈，进程退出码非 0。
4. **预期结果**：
   - 通过数量：`Completed` 行里的 `N`（即所有合法 `(a_fmt, r_fmt, rnd)` 组合数）；失败数量：0（脚本能跑到最后一行即代表 0 失败）。
   - 由于「失败即中断」，**它不会同时报告多个失败**——一旦某格不一致就立刻停下来。
5. **运行结果**：待本地验证（具体 `N` 取决于 `for_round` 接受了多少组合，以本机打印为准）。

**解释其中一个断言用例**：

聚焦 [L131](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_round_test.py#L131) 这一条：

```python
assert np.array_equal(r, expected), "Numerical error detected."
```

它的含义是：把待测函数 `cl_fix_round` 对**整个取值数组** `a` 的输出 `r`，与 numpy 参考实现 `round_check` 算出的 `expected` 做**逐元素比较**（`np.array_equal` 要求形状和每个元素都相等）。若任意一个元素不一致，就抛出 `"Numerical error detected."`。换句话说，这一行不是在测「某一个数」，而是在一次断言里把「该格式 × 该舍入模式」下的**全部取值**一网打尽——这就是穷举对拍的威力。

#### 4.3.5 小练习与答案

**练习 1**：同样是 Python 测试，为什么 `cl_fix_round_test.py` 能从仓库根目录直接运行，而 `en_cl_fix_pkg_test.py`（4.2）一般不行？

**参考答案**：前者用 `dirname(__file__)` 取脚本自身目录来拼接模型路径，与当前工作目录无关；后者直接 `sys.path.append("../../models/python")`，路径相对的是 CWD，所以必须在测试目录内运行。

**练习 2**：脚本里同时比对了 `r`（NarrowFix 路径）和 `r_wide`（WideFix 路径），为什么要比两次？

**参考答案**：为了交叉验证。`cl_fix_round` 内部在位宽 ≤ 53 时走 NarrowFix（float64 表示）、否则走 WideFix（任意精度整数表示）。穷举对拍同时核对这两条独立实现，确保「无论走哪条路径结果都一致」，这是单元 6 Narrow/Wide 双表示架构正确性的早期验证。

**练习 3**：如果运行中途抛出 `AssertionError` 而非打印 `Completed`，你能从输出定位是哪个组合出错吗？

**参考答案**：能。Python 会在堆栈里显示抛异常时的调用栈，结合外层循环变量（`aS/aI/aF/rF/rnd`）即可知道出错的格式与舍入模式；也可以在 `assert` 前临时加一行 `print(a_fmt, r_fmt, rnd)` 来显式打印当前组合以便定位（这属于「给关键函数添加日志」的源码阅读型实践，注意只改测试脚本、不改库源码）。

---

### 4.4 MATLAB 测试入口：matlab_example.m

#### 4.4.1 概念说明

en_cl_fix 的 MATLAB 实现并不是「用 MATLAB 重写一遍算法」，而是**一层薄包装**：MATLAB 脚本通过 MATLAB 自带的 Python 接口，去调用**同一份** Python 模型 `en_cl_fix_pkg`。这意味着 MATLAB 侧和 Python 侧天然位级一致（bit-true），维护成本也低。

`bittrue/tests/matlab/matlab_example.m` 就是一个面向「narrow（≤53 位）」定点数的演示脚本：它生成随机数据、调用各种 `cl_fix_*` 运算，并用 MATLAB 原生算术自检结果是否正确。

#### 4.4.2 核心流程

```
1. 环境准备
   ├─ 选择 Python 执行模式（InProcess 快 / OutOfProcess 便于热重载）
   ├─ 把模型 Python 源码目录加入 py.sys.path
   ├─ py.importlib.import_module('en_cl_fix_pkg')   # 载入 Python 包
   └─ addpath 模型 MATLAB 源码目录

2. 示例设置
   ├─ cl_fix_constants          # 载入简写常量（如 Round.ConvEven_s、Sat.Sat_s）
   ├─ 指定输入格式 a_fmt / b_fmt
   └─ DATA_SHAPES = {标量, 行向量, 列向量, 二维, 三维}  # 测多种数据形状

3. 对每种形状循环
   ├─ cl_fix_random 生成随机数据 a, b
   ├─ 算术：add/sub/addsub/mult/abs/neg/shift，结果格式由 cl_fix_*_fmt 推导（无损）
   ├─ 用 MATLAB 原生 a+b、a.*b 等自检（因 narrow 且无损，可直接比对）
   └─ 各类杂项函数（round/saturate/resize/from_real/to_integer/...）调用一次确认不报错

4. 全部通过 → disp('Success: All tests passed.')
```

#### 4.4.3 源码精读

**Python 执行模式的选择**——脚本一开头就处理一个实际工程问题：

[matlab_example.m:37-53](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/matlab/matlab_example.m#L37-L53) —— `RELOAD_PYTHON_MODULES` 开关：默认 `false` 用 `InProcess`（快），若想在改 Python 源码后**不重启 MATLAB** 就生效，则切到 `OutOfProcess`（慢但便于调试）。这段注释点明了 MATLAB 重载 Python 模块的局限。

**把 Python 模型路径喂给 MATLAB 的 Python 接口**：

[matlab_example.m:60-65](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/matlab/matlab_example.m#L60-L65) —— `insert(py.sys.path, 0, python_src_path)` 把模型目录插到 Python 搜索路径最前，再 `py.importlib.import_module('en_cl_fix_pkg')` 载入。这就是「MATLAB 调 Python」的关键两步。

**载入 MATLAB 侧的简写常量**：

[matlab_example.m:74-79](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/matlab/matlab_example.m#L74-L79) —— `cl_fix_constants` 载入简写（如 `Round`、`Sat`），随后用 `cl_fix_format(S, I, F)` 构造格式（MATLAB 版的 `FixFormat`）。注意 MATLAB 里 `cl_fix_format(1, 0, 15)` 第一个参数 `1` 表示有符号，对应 Python 的 `FixFormat(True, 0, 15)`。

**算术 + MATLAB 原生自检**：

[matlab_example.m:106-107](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/matlab/matlab_example.m#L106-L107) —— 先用 `cl_fix_add_fmt` 推导「无损结果格式」`add_fmt`，再 `cl_fix_add` 做加法。

[matlab_example.m:149-150](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/matlab/matlab_example.m#L149-L150) —— 因为格式是无损的、且都落在 double 精度内，可直接用 MATLAB 原生 `a + b` 当期望值，`assert(isequal(...))` 比对。这就是「MATLAB 经 Python 接口调库」与「MATLAB 自己算」的互验。

**收尾**：

[matlab_example.m:230](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/matlab/matlab_example.m#L230) —— 全程无 `assert` 失败则打印 `Success: All tests passed.`。

> 旁注：同目录还有 `matlab_wide_example.m`，用于演示 >53 位的「wide」定点（配套单元 6 的 WideFix），本讲不展开。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：不依赖 MATLAB 也能理解数据流，画出「MATLAB → Python 接口 → en_cl_fix_pkg → 返回 MATLAB」的调用链。
2. **操作步骤**：
   - 精读 [matlab_example.m:60-65](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/matlab/matlab_example.m#L60-L65)（载入 Python 包）与 [matlab_example.m:106-107](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/matlab/matlab_example.m#L106-L107)（调用 `cl_fix_add`）。
   - 在纸上画出调用链：MATLAB 变量 `a` → 经 MATLAB Python 接口 → Python 的 `cl_fix_add` → 返回值再转回 MATLAB 数组 → 与 `a + b` 比对。
3. **需要观察的现象**：确认你能指出「哪一行是把 Python 模型挂进来」「哪一行是真正调用定点运算」「哪一行是自检」。
4. **预期结果**：三条分别是 L65（`import_module`）、L107（`cl_fix_add`）、L150（`assert(isequal(...))`）。
5. **运行结果**：若本机有 MATLAB（README 标注测试过 R2023b），可直接运行 `matlab_example.m` 并看到 `Success: All tests passed.`；否则本实践为纯阅读型，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：为什么 MATLAB 侧不需要重新实现一套定点算法？

**参考答案**：因为 MATLAB 通过自带 Python 接口直接调用 Python 模型 `en_cl_fix_pkg`，二者共享同一份算法实现，天然 bit-true，既省维护成本又保证三语言结果一致。MATLAB 层主要是参数转换与形状保持的薄包装（详见单元 9 的 `matlab_interface`）。

**练习 2**：脚本里为什么能用 MATLAB 原生 `a + b` 当作加法的期望值，而不怕精度问题？

**参考答案**：因为示例刻意用 `cl_fix_add_fmt` 推导了「无损结果格式」，且所有数据都落在 double（float64）能精确表示的 narrow 范围内（脚本先用 `assert(~cl_fix_is_wide(...))` 确认）。在这种前提下，定点加法等价于普通实数加法，所以 MATLAB 原生算术可直接作对照。

---

## 5. 综合实践

把本讲三处要点串起来，完成下面这个「环境验收」小任务：

1. **装依赖**：`python -m pip install -r requirements.txt`，并用 `python -c "import numpy, en_cl_fix_pkg"`（需把 `bittrue/models/python` 加到 `PYTHONPATH`，或先 `cd` 到测试目录）确认两个包都能导入。
2. **跑两类 Python 测试**：
   - `cd bittrue/tests/python && python en_cl_fix_pkg_test.py`（unittest 风格，记录 `Ran N tests` 与 `OK`）。
   - 回到仓库根目录 `python bittrue/tests/python/cl_fix_round_test.py`（穷举对拍，记录 `Completed N tests.`）。
3. **对比两种输出风格**：写一句话总结——unittest 风格告诉你「多少个用例通过」，穷举对拍风格告诉你「多少种格式×模式组合被覆盖且全一致」。
4. **（可选）MATLAB**：若有 MATLAB，运行 `matlab_example.m`，确认打印 `Success: All tests passed.`。
5. **填一张验收表**（待本地验证）：

| 检查项 | 命令 | 结果 |
| --- | --- | --- |
| 依赖安装 | `pip install -r requirements.txt` | （填写） |
| unittest 测试 | `python en_cl_fix_pkg_test.py` | Ran __ tests, OK |
| 穷举对拍 | `python cl_fix_round_test.py` | Completed __ tests |
| MATLAB 示例 | `matlab_example.m` | Success（可选） |

完成这张表，就意味着你的 en_cl_fix 学习环境已经就绪，可以进入单元 2 的源码学习了。

---

## 6. 本讲小结

- **依赖**：`requirements.txt` 用 `==` 钉死 `numpy` 与 `vunit-hdl` 版本，`pip install -r requirements.txt` 一键安装；README 另给「最低版本」门槛。
- **Python 测试有两种风格**：`en_cl_fix_pkg_test.py` 是 **unittest 逐例断言**（人工挑用例、`unittest.main()` 驱动）；`cl_fix_round_test.py` 是 **穷举对拍**（枚举所有取值，与 numpy/WideFix 多份独立实现逐位比对）。
- **路径处理的坑**：`en_cl_fix_pkg_test.py` 用 CWD 相对路径，须在测试目录内运行；`cl_fix_round_test.py` 用 `dirname(__file__)`，从任意目录运行均可——本讲主实践用后者。
- **穷举对拍的判定**：能打印 `Completed N tests.` 即代表 0 失败；任一 `assert` 失败则立即中断。
- **MATLAB 是薄包装**：`matlab_example.m` 经 MATLAB Python 接口调用同一份 `en_cl_fix_pkg`，再用 MATLAB 原生算术自检，三语言天然 bit-true。
- **环境就绪标志**：两类 Python 测试都能跑通（MATLAB 可选），即可进入单元 2。

---

## 7. 下一步学习建议

本讲只是「点亮环境」，尚未触及任何算法实现。建议按以下顺序继续：

1. **单元 2（核心类型与 API 地图）**：本讲你已经在测试里见过 `FixFormat(True,2,2)`、`FixRound.NonSymPos_s`、`cl_fix_resize` 等符号——单元 2 会系统讲解 `FixFormat / FixRound / FixSaturate` 三大类型，以及 `cl_fix_*` 函数地图。
2. **先读 `en_cl_fix_types.py`**：对照本讲用到的 `FixFormat` 构造与断言，理解 `S/I/F` 的合法性约束。
3. **回看本讲的断言**：当你学完单元 4 的舍入/饱和机制后，再回头读 `cl_fix_round_test.py` 的 `round_check`（[L61-L78](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/cl_fix_round_test.py#L61-L78)），会有「原来这七种模式是这样实现」的顿悟。
4. **（可选）VHDL 仿真**：如果你关心硬件侧，单元 8 会讲 `sim/run.py` 如何用 VUnit + GHDL/NVC 跑 VHDL testbench——那是本讲 `vunit-hdl` 依赖真正派上用场的地方。
