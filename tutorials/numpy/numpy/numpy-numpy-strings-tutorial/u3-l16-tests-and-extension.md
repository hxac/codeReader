# 测试体系与新增字符串 ufunc 的端到端实践

## 1. 本讲目标

本讲是专家层（第 3 单元）的收尾篇。前面三讲（u3-l12 ~ u3-l15）已经把 `numpy.strings` 从 Python 包装层一路下探到了 C/C++ 的 ufunc 循环、字符处理原语、StringDType 专用循环与 `_vec_string` 这座「桥」。本讲换一个视角，回答两个工程层面的终极问题：

1. **这么多函数、三种 dtype、还要兼容 NA 缺失值，测试是怎么组织的？** 我们将以 `test_strings.py` 与 `test_stringdtype.py` 为样本，看清 NumPy 用「按 dtype 参数化」+「按行为归类」+「StringDType 与 str_ 交叉验证」三套手段，把一个庞大而琐碎的函数族压缩成可维护的测试矩阵。
2. **如果我想把一个 `_vec_string` 函数（如 `split`）升级成真正的 ufunc，到底要动哪些文件、走哪些步骤？** 我们将以 `split` 为例，对照已经「毕业」的 `replace`，输出一份端到端的迁移清单。

读完本讲，你应该能够：

1. 说出 `test_strings.py` 的测试类划分逻辑（`TestMethods` / `TestMethodsWithUnicode` / `TestMixedTypeMethods` / `TestUnicodeOnlyMethodsRaiseWithBytes` / `TestOverride`），并理解 `@pytest.mark.parametrize("dt", ["S","U","T"])` 如何用一个装饰器覆盖三种 dtype。
2. 读懂 `test_stringdtype.py` 的「交叉验证 + NA 笛卡尔积」框架：用同一份数据在 StringDType 与 str_ 上跑同一个函数并断言相等，再通过 `dtype` fixture 把 NA 哨兵 × coerce 组合成 12 种变体逐一覆盖。
3. 学会阅读 `strings.py` 中 `__all__` 的三段注释，据此判断任意一个函数的「演化方向」（已经是 ufunc / 迟早会变 ufunc / 大概不会变 ufunc / 行为未定型已从命名空间移除）。
4. 产出一份「把 `split` 从 `_vec_string` 迁移成 ufunc」的全链路清单，理解 Python 包装、C 循环注册、`__all__`、`.pyi` 存根、测试五处改动之间的关系。

## 2. 前置知识

本讲需要你已经掌握以下认知（来自前置讲义）：

- **门面与三种 dtype**（u1-l1、u1-l2）：`numpy.strings` 是 `numpy._core.strings` 的门面；字符串有三种 dtype——`bytes_`（`'S'`）、`str_`（`'U'`）、变长 `StringDType`（`'T'`）。
- **`__all__` 与演化判据**（u2-l8、u3-l15）：`__all__` 注释按「输出宽度能否仅由输入 dtype 决定」把函数分成「会变 ufunc」与「大概不会变 ufunc」两类。
- **装饰器两件套**（u2-l4）：`@set_module` 管「身份」（改写 `__module__`），`@array_function_dispatch` 管「行为」（NEP-18 `__array_function__` 分发），裸 ufunc 则靠 `_override___module__` 就地改写。
- **C++ ufunc 循环注册**（u3-l12、u3-l14）：一个真正的 ufunc 在 C 层有 `init_string_ufuncs`（定长）与 `init_stringdtype_ufuncs`（变长）两条注册链，Python 包装层只负责预算宽度与开缓冲区。

补充一点 pytest 基础（不熟悉可略读）：

- **`@pytest.mark.parametrize`**：把同一个测试函数按给定参数组合「展开」成多个独立用例。本讲里它被用来一行覆盖三种 dtype。
- **`@pytest.fixture`**：测试夹具，把「准备数据」抽成可复用的函数，测试函数把它当作参数注入即可。本讲里 `dtype` 这个夹具是 NA 覆盖的核心。
- **`assert_array_equal`**：NumPy 提供的断言，逐元素比较两个数组，形状或内容不一致即报错并打印差异。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [numpy/_core/tests/test_strings.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_strings.py) | `numpy.strings` 的主测试文件：模块级比较/类型测试 + 按行为归类的若干测试类，用 `dt ∈ {S,U,T}` 参数化覆盖三种 dtype。 |
| [numpy/_core/tests/test_stringdtype.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_stringdtype.py) | StringDType 专用测试：交叉验证框架（StringDType vs str_）+ NA 笛卡尔积（`dtype` 夹具）+ 按函数行为归类的清单驱动测试。 |
| [numpy/_core/strings.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py) | Python 包装层。含带三段注释的 `__all__`、已「毕业」的 ufunc 包装（`replace`）与尚未定型的 `_split`/`_rsplit`/`_join`/`_splitlines`。 |
| [numpy/_core/defchararray.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/defchararray.py) | `numpy.char` 实现：通过 `from numpy._core.strings import _split as split` 把私有函数借为公共接口。 |
| [numpy/strings/__init__.pyi](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/strings/__init__.pyi) | 门面类型存根：逐个显式列出 `numpy.strings` 的公开接口，是新增/移除函数时必须同步的「接口账本」。 |

> 说明：仓库给出的永久链接 base 指向 `numpy/strings/` 子目录，但本讲引用的源码大多位于 `numpy/_core/` 下。为使链接可点击且正确，下面一律使用从仓库根算起的完整路径。

## 4. 核心概念与源码讲解

### 4.1 `test_strings.py` 的用例分组：按 dtype 参数化 + 按行为归类

#### 4.1.1 概念说明

`numpy.strings` 有 40 多个函数，每个都要在 `bytes_`/`str_`/`StringDType` 三种 dtype 上正确工作。如果每个函数 × 每个 dtype 都手写一个测试，用例数会爆炸。`test_strings.py` 用两条原则把爆炸的用例收敛成一张清爽的表：

1. **按 dtype 参数化**：把 `dt` 作为一个参数，用 `@pytest.mark.parametrize("dt", ["S", "U", "T"])` 一行展开成三个用例。绝大多数函数的测试因此只写一遍。
2. **按行为归类**：把「所有 dtype 都适用」的函数放进 `TestMethods`；把「只有 unicode 才有意义」（如 `isdecimal` 对 `①` ① 这类字符）的放进 `TestMethodsWithUnicode`；把「两种 dtype 混用」的放进 `TestMixedTypeMethods`；把「确认 bytes 上应当报错」的放进 `TestUnicodeOnlyMethodsRaiseWithBytes`。

此外还有两个专门类：`TestReplaceOnArrays`（验证 `replace` 的输出宽度预算）和 `TestOverride`（验证 NEP-18 分发）。这套划分的潜台词是——**函数之间的差异不在 dtype，而在「行为类别」**，所以测试也按行为类别组织。

#### 4.1.2 核心流程

```text
test_strings.py
  │
  ├─ 模块级常量：COMPARISONS（6 个比较算子）、MAX 哨兵
  │
  ├─ 模块级测试函数（不归任何类）
  │     比较算子族、float→str 转换、dtype 尺寸错误、超大字符串溢出
  │
  └─ 测试类（按行为归类）
        TestMethods            dt ∈ {S,U,T}   —— 主力：绝大多数函数
        TestMethodsWithUnicode dt ∈ {U,T}     —— unicode 专属语义
        TestMixedTypeMethods   （无参数化）    —— S 与 U 混用（fillchar 跨编码）
        TestUnicodeOnlyMethodsRaiseWithBytes  —— 确认 bytes 上报 TypeError
        TestReplaceOnArrays    dt ∈ {S,U,T}   —— replace 宽度预算
        TestOverride           （无参数化）    —— NEP-18 分发归属
```

`TestMethods` 是全文件最大的类，它把 `add/multiply/isalpha/...` 等所有「三 dtype 通用」的函数集中起来，每个函数一个 `test_xxx`，内部再用 `@parametrize` 喂入多组 `(输入, 输出)` 样例。两层 `@parametrize` 相乘（外层 `dt` × 内层样例），用很紧凑的代码覆盖了大量组合。

#### 4.1.3 源码精读

模块顶部的常量与第一个测试，体现了「比较算子族用一张表驱动」：

[COMPARISONS 表与首个比较测试](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_strings.py#L11-L31) — `COMPARISONS` 把 `(operator.eq, np.equal, "==")` 等 6 个三元组列成表，再用 `@parametrize(["op","ufunc","sym"], COMPARISONS)` 展开。注意它同时验证了「`S` 与 `U` 混用应当报 `did not contain a loop`」，这正对应 u2-l6 讲过的「比较类没有 S↔U 循环」。

主力类 `TestMethods` 的声明与第一个方法：

[TestMethods 类声明（dt ∈ {S,U,T}）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_strings.py#L203-L219) — 类级 `@parametrize("dt", ["S","U","T"])` 会被该类**所有**方法继承，于是 `test_add` 自动跑 3 种 dtype × 7 组样例 = 21 个用例。方法体里 `np.array(in1, dtype=dt)` 把同一份输入转成三种 dtype，再断言 `np.strings.add(in1, in2)` 与预期 `out` 相等。

`test_multiply_raises` 集中体现了「边界与异常也是一等测试对象」：

[multiply 的异常测试](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_strings.py#L232-L237) — 它专门测两件事：`3.14` 这种浮点重复次数要抛 `TypeError`（match="unsupported type"）；`sys.maxsize` 这种会溢出的重复次数要抛 `OverflowError`。这正是 u2-l6 讲过的 `sys.maxsize` 溢出保护的可执行证据。

unicode 专属类（只测 `U` 与 `T`，因为 `S` 装不下这些字符）：

[TestMethodsWithUnicode：isdecimal 对 ① ¼ 等](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_strings.py#L1080-L1095) — `dt ∈ {U,T}`，测试用例里出现 `①`（①）、`\xbc`（¼）、`٠`（阿拉伯数字零）这类只有 unicode 才能表示的字符，验证 `isdecimal`/`isnumeric` 的码点分类。

混合 dtype 类（`S` 与 `U` 混用，验证 ASCII fillchar 配 UTF32 输入）：

[TestMixedTypeMethods：center 的跨编码填充](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_strings.py#L1333-L1348) — 这里没有 `dt` 参数化，而是**显式**给 `buf` 和 `fill` 指定不同 dtype（如 `buf="U"`、`fill="S"`），验证 `np.strings.center(buf, 3, fill)` 能正确填充。第三段还断言「用非 ASCII 的 emoji 作 fill 去填 `S` 数组」必须抛 `'ascii' codec can't encode`——这正是 u3-l12 讲过的「ASCII fillchar 配 UTF32 输入」能成立、而反向不行的根因。

NA 哨兵在定长 dtype 上的「字段宽度」换算帮助函数：

[check_itemsize：把字符数换算成 itemsize](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_strings.py#L1395-L1401) — `U` 的 itemsize = 字符数 × 4、`S` = 字符数、`T` 直接取 itemsize。这正是 u1-l2 讲过的 `_get_num_chars` 的「逆运算」，`TestReplaceOnArrays` 用它来核对 `replace` 预算出的输出宽度对不对：

[TestReplaceOnArrays：replace 的宽度预算核对](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_strings.py#L1403-L1426) — 它断言 `r1.dtype.itemsize == check_itemsize(3*10 + 3*4, dt)`，即「10 字符串 × 3 段 + 4 字符替换 × 3 次」算出的字段宽度。这条测试直接守护了 u2-l6/replace 的「路径 A」预算逻辑。

最后一个类是本讲最值得反复读的 `TestOverride`，它把整个 `numpy.strings` 的「分发归属」编码成一张可执行表：

[TestOverride：function 分支 vs ufunc 分支](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_strings.py#L1441-L1506) — 它定义一个伪类型 `Override`，分别实现 `__array_function__`（返回字符串 `"function"`）和 `__array_ufunc__`（返回 `"ufunc"`）。随后用两张 `@parametrize` 表把 `numpy.strings` 的全部函数分成两栏：凡是被 `@array_function_dispatch` 包装的（`center/capitalize/mod/...`）应当返回 `"function"`，凡是无 dispatcher 的裸 ufunc（`add/lstrip/find/isdigit/...`）应当返回 `"ufunc"`。**这张表就是 u2-l4 那套装饰器机制是否生效的可执行校验**——你新增一个函数并选错装饰器，这条测试立刻会失败。

#### 4.1.4 代码实践

**实践目标**：亲手运行 `TestMethods.test_multiply_raises` 的逻辑，验证 `dt ∈ {S,U,T}` 三种 dtype 都触发了同样的溢出保护，从而体会「一个测试方法 × 三种 dtype」的覆盖力。

**操作步骤**（示例代码）：

```python
import sys
import numpy as np

# 模拟 test_multiply_raises 的核心断言（待本地验证）
for dt in ["S", "U", "T"]:
    a = np.array("abc", dtype=dt)

    # 1) 浮点重复次数 -> TypeError
    try:
        np.strings.multiply(a, 3.14)
        print(dt, "未抛 TypeError（异常）")
    except TypeError as e:
        print(dt, "TypeError:", e)

    # 2) sys.maxsize 重复 -> OverflowError
    try:
        np.strings.multiply(a, sys.maxsize)
        print(dt, "未抛 OverflowError（异常）")
    except OverflowError:
        print(dt, "OverflowError: 符合预期")
```

**需要观察的现象**：三种 dtype 都应抛出 `TypeError` 与 `OverflowError`，说明这一份用例的 3 个参数化变体确实各自走到了 `multiply` 内部的同一处溢出检查（参见 u2-l6）。

**预期结果**：`S`/`U`/`T` 三行都打印 `TypeError` 与 `OverflowError: 符合预期`。

**如果无法确定运行结果**：具体异常消息文案「待本地验证」，但「三种 dtype 都抛同样的两类异常」是稳定的。

#### 4.1.5 小练习与答案

**练习 1**：`TestMethodsWithUnicode` 为什么把 `dt` 限制成 `["U","T"]` 而不含 `"S"`？

**参考答案**：因为这一类测试针对的是只有 unicode 才能表达的语义，例如 `isdecimal("①")`（①）、`isnumeric("\xbc")`（¼）。`bytes_`（`S`）按 ASCII 存储，根本无法表示这些码点，自然也不在 `isdecimal`/`isnumeric` 的能力范围内——`TestUnicodeOnlyMethodsRaiseWithBytes` 进一步断言 `bytes_` 输入会给 `isdecimal`/`isnumeric` 抛 `TypeError`。

**练习 2**：`TestOverride.test_override_function` 与 `test_override_ufunc` 两张表分别守护了 u2-l4 的哪个机制？

**参考答案**：`test_override_function` 守护「带 `@array_function_dispatch` 的函数会触发 NEP-18 的 `__array_function__` 分发」（返回 `"function"`）；`test_override_ufunc` 守护「无 dispatcher 的裸 ufunc 走的是 `__array_ufunc__` 协议」（返回 `"ufunc"`）。若你给一个新函数装了 dispatcher 却忘了把它加进第一张表，或装错成裸 ufunc，对应断言会失败。

---

### 4.2 `test_stringdtype.py`：交叉验证与 NA 笛卡尔积

#### 4.2.1 概念说明

`test_stringdtype.py` 专门针对变长 `StringDType`（`'T'`）。它面临一个比 `test_strings.py` 更棘手的局面：StringDType 还有一个**可配置的 NA（缺失值）机制**——`StringDType(na_object=...)` 可以把缺失值设成 `None`、`pd_NA`、`np.nan`、甚至字符串 `"__nan__"`，并且有一个 `coerce` 开关决定非字符串输入是否强制转换。如果每个函数 × 每种 NA × 每个 coerce 都手写，用例数会再次爆炸。

这个文件用两招化解：

1. **交叉验证（cross-check）**：用同一份测试数据 `UFUNC_TEST_DATA`，分别在 StringDType 数组 `string_array` 和 `str_` 数组 `unicode_array` 上跑**同一个函数**，然后断言两者结果相等。潜台词是——`str_` 是经过长期验证的「参考实现」，StringDType 只要和它一致就算对。这把「为 StringDType 手写预期值」的负担转嫁给了 `str_`。
2. **NA 笛卡尔积**：用一个 `dtype` 夹具把 `na_object`（6 种）× `coerce`（2 种）= 12 种 StringDType 变体展开，凡是用到 `dtype` 的测试自动跑 12 遍。再配上一组**行为分类清单**（哪些函数保留 NA、哪些输出 bool、哪些对非字符串 NA 报错），用清单驱动「每个函数在每种 NA 下的预期行为」。

#### 4.2.2 核心流程

```text
test_stringdtype.py 的 ufunc 测试骨架
  │
  ├─ dtype 夹具 = get_dtype(na_object, coerce)
  │     na_object ∈ {unset, None, pd_NA, np.nan, float('nan'), "__nan__"}  (6)
  │     coerce    ∈ {True, False}                                          (2)
  │     ⇒ 12 种 StringDType 变体
  │
  ├─ 数据夹具
  │     UFUNC_TEST_DATA   一份含 unicode/换行/制表符的固定数据
  │     string_array     = UFUNC_TEST_DATA 以 dtype 存储
  │     unicode_array    = UFUNC_TEST_DATA 以 str_ 存储（参考实现）
  │
  ├─ 行为分类清单（决定每个函数在 NA 下的预期）
  │     NAN_PRESERVING_FUNCTIONS    NA 原样穿透
  │     BOOL_OUTPUT_FUNCTIONS       输出 bool，NA→False
  │     UNIMPLEMENTED_VEC_STRING_FUNCTIONS  _vec_string 无 NA 支持
  │     ONLY_IN_NP_CHAR             join/split/rsplit/splitlines（仍走 np.char）
  │     NULLS_ARE_FALSEY / NULLS_ALWAYS_ERROR / PASSES_THROUGH_NAN_NULLS
  │
  └─ 清单驱动的测试
        test_unary(func ∈ UNARY_FUNCTIONS)
           ① func(string_array) == func(unicode_array)   ← 交叉验证
           ② 在 na_arr 上按清单断言 NA 行为
        test_binary(func ∈ BINARY_FUNCTIONS)  —— 同上，但带参数
```

关键直觉：**「对不对」交给 `str_` 判断，「NA 怎么处理」交给清单判断**。两套独立机制各管一摊，互不耦合。

#### 4.2.3 源码精读

`dtype` 夹具——整个 NA 矩阵的源头：

[dtype 夹具：na_object × coerce 的笛卡尔积](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_stringdtype.py#L42-L54) — `na_object` 取 6 种缺失值哨兵，`coerce` 取 2 种，组合成 `get_dtype(na_object, coerce)`。注意 `get_dtype` 对 `pd_NA` 做了特殊判断（因为 `pd_NA != "unset"` 会返回 `pd_NA` 而非 bool）。任何依赖 `dtype` 的测试都会被 pytest 自动展开成 12 个用例。

交叉验证的数据基础：

[UFUNC_TEST_DATA 与 string_array/unicode_array 夹具](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_stringdtype.py#L1464-L1479) — 同一份 4 条数据（含 `"Ae¢☃€ 😊" * 20` 这种多字节 unicode、含换行与制表符），一份存成 StringDType，一份存成 `str_`。后面所有 ufunc 测试都拿这两份做对照。

行为分类清单（NA 处理的「说明书」）：

[函数行为分类清单](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_stringdtype.py#L1482-L1549) — 这里把函数分成几组：`NAN_PRESERVING_FUNCTIONS`（NA 原样穿透，如 `upper/strip`）、`BOOL_OUTPUT_FUNCTIONS`（输出 bool，如 `isdigit`）、`UNIMPLEMENTED_VEC_STRING_FUNCTIONS`（走 `_vec_string` 因而没有 NA 支持的函数）、以及本讲的关键 `ONLY_IN_NP_CHAR = ["join","split","rsplit","splitlines"]`。这四份清单就是「每个函数 NA 行为」的单一事实来源（single source of truth）。

清单驱动的核心测试 `test_unary`：

[test_unary：交叉验证 + NA 分支](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_stringdtype.py#L1552-L1605) — 读这段代码要看三层：①`func = getattr(np.char, ...) if function_name in ONLY_IN_NP_CHAR else getattr(np.strings, ...)`，即对 `split` 这类函数它**主动**去 `np.char` 取实现；②`assert_array_equal(sres, ures)` 是交叉验证的主断言，必要时把 `ures` 转成 StringDType 再比；③随后一大段 `if/elif` 按 `UNIMPLEMENTED_VEC_STRING_FUNCTIONS`/`BOOL_OUTPUT_FUNCTIONS`/`is_nan`/`is_str` 决定 NA 输入的预期（穿透、返回 False、还是抛错）。

二参函数版 `test_binary` 与它的参数表：

[BINARY_FUNCTIONS 表与 test_binary](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_stringdtype.py#L1613-L1638) — `BINARY_FUNCTIONS` 用 `(函数名, 参数元组)` 的形式给每个二参函数喂默认参数（如 `("find", (None, "A"))` 表示「对数组找 'A'」），`None` 占位代表「数组本身」。`call_func` 辅助函数按 `None` 的位置决定参数怎么塞。

NA 行为的三分类：

[NULLS_ARE_FALSEY / NULLS_ALWAYS_ERROR / SUPPORTS_NULLS](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_stringdtype.py#L1654-L1669) — 把二参函数按 NA 行为再分三类：`startswith/endswith` 的 NA 当 False；`count/find/rfind` 的 NA 总是报错；其余支持 NA 的归入 `SUPPORTS_NULLS`。`test_binary` 的后半段正是用这三类决定 NA 输入的预期。

一个揭示「StringDType 与 str_ 行为差异」的精细测试：

[test_non_default_start_stop：start/stop 的整数 dtype](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_stringdtype.py#L1733-L1744) — 它特意用 `np.int8(1)`、`np.array([1,1],'u2')` 等不同整数 dtype 传 `start/stop`，验证 find/startswith 在 StringDType 上对非默认整数类型也正确。这类「看似吹毛求疵」的用例，正是把「StringDType 循环与 str_ 循环语义一致」钉死的证据。

#### 4.2.4 代码实践

**实践目标**：手工复现「交叉验证」思想——用同一份数据，分别在 StringDType 与 `str_` 上调用同一个函数，断言两者完全相等，体会这套框架如何省去为 StringDType 手写预期值。

**操作步骤**（示例代码）：

```python
import numpy as np

# 复刻 UFUNC_TEST_DATA 的思路：含多字节 unicode、换行、制表符
DATA = ["hello" * 5, "Ae¢☃€ 😊" * 3, "a\tb\nc", "  pad  "]

s_arr = np.array(DATA, dtype="T")     # StringDType
u_arr = np.array(DATA, dtype=np.str_) # str_ 参考实现

for name in ["upper", "strip", "center"]:
    func = getattr(np.strings, name)
    # center 需要宽度参数，这里统一传 20 仅为演示
    s_res = func(s_arr) if name != "center" else func(s_arr, 20)
    u_res = func(u_arr) if name != "center" else func(u_arr, 20)
    # 交叉验证：StringDType 结果应与 str_（转 T 后）一致
    np.testing.assert_array_equal(s_res, u_res.astype("T"))
    print(name, "交叉验证通过")
```

**需要观察的现象**：四个函数在 StringDType 与 `str_` 上的输出逐元素相等。这正是 `test_unary` 第一条断言在做的事——一旦 StringDType 的 C 循环写错（比如码点计数差 1），这条断言会立刻失败。

**预期结果**：依次打印 `upper 交叉验证通过`、`strip 交叉验证通过`、`center 交叉验证通过`。

**如果无法确定运行结果**：具体渲染「待本地验证」，但「两者相等」是稳定预期。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `test_unary` 里要先 `getattr(np.char, function_name)` 再 `getattr(np.strings, function_name)`，而不能直接从 `np.strings` 取所有函数？

**参考答案**：因为 `split/rsplit/splitlines/join` 目前**不在** `numpy.strings` 的 `__all__` 里（见 4.3 节），`np.strings` 上根本没有这些名字。它们只存在于 `numpy.char`（由 `defchararray.py` 借私有函数暴露）。所以测试框架用 `ONLY_IN_NP_CHAR` 这张表来分流：在表里的去 `np.char` 取，不在表里的才去 `np.strings` 取。

**练习 2**：`dtype` 夹具把 NA 笛卡尔积展开成 12 种变体。如果一个新加的 StringDType ufunc 循环忘了处理 NA（直接把 NA 当普通字符串读），哪条断言最可能先失败？

**参考答案**：`test_unary`/`test_binary` 的 NA 分支断言。具体说，当 `na_object` 是 `np.nan`（`is_nan=True`）且函数在 `NAN_PRESERVING_FUNCTIONS` 里时，测试断言 `res[0] is dtype.na_object`（NA 原样穿透）；若循环没处理 NA，`res[0]` 就不会是那个 NA 哨兵，断言失败。这也是为什么 StringDType 循环骨架里必须有 NA 传播三分支（u3-l14）。

---

### 4.3 `__all__` 的三段注释：函数演化方向的判据

#### 4.3.1 概念说明

`numpy/_core/strings.py` 顶部的 `__all__` 不是一个普通的导出清单——它是一份**带注释的「演化路线图」**。注释把全部函数分成四档：

| 注释 | 含义 | 典型函数 |
| --- | --- | --- |
| `# UFuncs` | 已经是真正的 C ufunc | `equal/add/multiply/find/strip/center/replace/slice` |
| `# _vec_string - Will gradually become ufuncs as well` | 现在走 `_vec_string`，但**迟早**会被改写成 ufunc | `upper/lower/swapcase/capitalize/title` |
| `# _vec_string - Will probably not become ufuncs` | 现在走 `_vec_string`，且**大概率**不会变 ufunc | `mod/decode/encode/translate` |
| `# Removed from namespace until behavior has been crystallized` | 行为尚未定型，已从公开命名空间移除 | `join/split/rsplit/splitlines`（注释掉） |

这份注释的判据，正是 u2-l8/u3-l15 反复强调的那条准则：**一个函数能否变成 ufunc，取决于「输出宽度能否仅由输入 dtype（或可在循环前算出的量）决定」**。

- `upper` 输出宽度不变 → 能变 ufunc（「迟早」）。
- `encode` 输出字节数强依赖数据（同一段文字不同编码长度不同）→ 难变 ufunc（「大概率不会」）。
- `split` 输出是**变长列表**（每个元素切出的段数都不同）→ 连「输出 dtype」都无法统一指定 → 行为未定型，移出命名空间。

#### 4.3.2 核心流程

```text
判断一个字符串函数的「演化方向」
  │
  ├─ 输出宽度是否与输入 dtype 无关/可预算？
  │     是（如 find 输出 int64、strip 输出不超输入）  → # UFuncs
  │
  ├─ 输出宽度数据相关，但循环前可算出（如 center 的最大宽度、replace 的预算）？
  │     是                                              → # UFuncs（Python 层预算 + C ufunc）
  │
  ├─ 输出宽度不变，但目前仍逐元素调 Python 方法？
  │     是（如 upper）                                  → Will gradually become ufuncs
  │
  ├─ 输出宽度强数据相关、难以写成定宽 ufunc？
  │     是（如 encode/translate）                        → Will probably not become ufuncs
  │
  └─ 输出连统一 dtype 都没有（变长列表/不规则结构）？
        是（如 split 返回 object 数组 of lists）          → Removed from namespace
```

#### 4.3.3 源码精读

带四段注释的 `__all__` 全貌：

[`__all__` 的四段注释](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L73-L90) — 第 74–80 行是已经是 ufunc 的那一档；第 82–83 行是「迟早会变」的五件套（大小写族）；第 85–86 行是「大概率不会变」的四件套（`mod/decode/encode/translate`）；第 88–89 行是**被注释掉**的 `join/split/rsplit/splitlines`，明确标注 `until behavior has been crystallized`（等行为定型再放回）。

`_override___module__` 的名单与 `__all__` 的呼应：

[`_override___module__` 名单](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L61-L70) — 这里列的 10 个裸 ufunc（`isalnum/isalpha/.../str_len`）正是 `__all__` 第一档里那些「无 dispatcher、靠改写 `__module__` 归属」的函数。两处名单在概念上互为印证：`__all__` 说「它们是 ufunc」，`_override___module__` 说「它们的 `__module__` 要手动改」。

「已毕业」uFunc 的范本——`replace`：

[replace 的 Python 包装（路径 A 预算 + set_module）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1285-L1351) — 读这段对照本节判据：`replace` 的输出宽度**数据相关但可预算**——它先用 `_count_ufunc` 数出 `old` 出现次数，再算 `buffersizes = str_len(arr) + counts*(str_len(new)-str_len(old))`，取 `.max()` 拼 `out_dtype`，最后 `out = np.empty_like(...)` 开缓冲区并调 `_replace(...)` ufunc。正因为它能在循环前算出统一宽度，它稳坐 `# UFuncs` 一档。注意它还有 `char=="T"` 的 StringDType 快速分流。

「未定型」的范本——`_split`：

[`_split`：无 set_module、不在 __all__、返回 object](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1395-L1441) — 与 `replace` 形成鲜明对照：①只有 `@array_function_dispatch(_split_dispatcher)`，**没有** `@set_module`；②函数名带下划线（私有）；③不在 `__all__`；④函数体直接 `return _vec_string(a, np.object_, 'split', ...)`，输出是 `np.object_` 数组。注释道破原因：`This will return an array of lists of different sizes, so we leave it as an object array`（每个元素切出的列表长度不同，只能装进 object 数组）。**正是这条「输出无统一 dtype」的特性，把它钉在了「行为未定型」一档。** 注意它的 docstring 里给的示例 `np.strings.split(x, " ")` 还带着 `# doctest: +SKIP`——因为 `np.strings.split` 这个名字根本不存在。

`_split` 与 `partition` 的命名级对照：

[partition（公开 ufunc 包装）](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1532-L1538) — `partition` 同样是「把字符串切成几段」，但它用**结构化 dtype**（`f0/f1/f2` 三字段，固定三段）承载输出（见 u2-l11），所以它有完整的 `@set_module("numpy.strings")` + `@array_function_dispatch`，是公开 ufunc。对比 `_split`（变长段数、无结构化 dtype 可装）只能退守 object 数组——同样是「切分」，输出结构决定了它是 ufunc 还是 `_vec_string`。

`numpy.char` 如何「借走」被移除的函数：

[defchararray.py 的别名导入](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/defchararray.py#L23-L30) — `from numpy._core.strings import (_split as split, _rsplit as rsplit, _join as join, _splitlines as splitlines)`，把私有函数改名成公共名字；紧接着 `from numpy.strings import *` 复用其余函数。于是这四个「未定型」函数在 `numpy.char` 里仍可用。

`numpy.char` 的 `__all__` 确认它们是 char 的公共接口：

[defchararray.__all__ 含 split/join/rsplit/splitlines](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/defchararray.py#L40-L50) — 与 `strings.__all__` 相比，这里多了 `join/rsplit/split/splitlines` 以及 `array/asarray/compare_chararrays/chararray`（后四个自 NumPy 2.5 起已废弃，见 u1-l3）。

`test_defchararray.py` 对 split 的现存测试：

[test_split：断言输出是 object 数组](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_defchararray.py#L628-L635) — `assert_(issubclass(A.dtype.type, np.object_))` 明确锁死了「split 当前返回 object 数组」这一行为。任何迁移方案都必须正视这条测试：要么改它，要么提供向后兼容的 object 输出路径。

#### 4.3.4 代码实践

**实践目标**：验证「`np.strings.split` 不存在、`np.char.split` 存在且返回 object 数组」这一现状，从而把 `__all__` 注释「Removed from namespace」落到可观察的事实上。

**操作步骤**（示例代码）：

```python
import numpy as np

# 1) np.strings 没有 split（被移出命名空间）
print("np.strings 有 split 吗：", hasattr(np.strings, "split"))   # 预期 False

# 2) np.char 有 split，且返回 object 数组
a = np.array(["a,b,c", "x-y-z"])
res = np.char.split(a, ",")
print("np.char.split dtype:", res.dtype)              # 预期 object
print("结果:", res.tolist())                          # 预期 [['a','b','c'], ['x-y-z,x-y-z'... 不对]]
```

**需要观察的现象**：`hasattr(np.strings, "split")` 为 `False`；`np.char.split` 的 `dtype` 是 `object`；结果是一个「装着 list」的数组（每个 list 长度可能不同）。

**预期结果**：第 1 行 `False`；第 2 行 `object`；第 3 行形如 `[['a', 'b', 'c'], ['x-y-z']]`（第二条按 `,` 切只有一段）。具体内容「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：同样是「切分字符串」，为什么 `partition` 是公开 ufunc、而 `split` 是「未定型」的 `_vec_string` 函数？

**参考答案**：因为输出结构不同。`partition` 永远切成固定的三段（前、分隔符、后），可以用结构化 dtype（`f0/f1/f2` 三字段）承载，宽度还能在循环前预算，所以能写成 ufunc。`split` 切出的段数随数据变化（`"a,b,c"` 切 3 段、`"abc"` 切 1 段），既没有统一的输出 dtype，也无法预算宽度，只能装进 object 数组——这正是它「未定型」的根因。

**练习 2**：`replace` 的输出宽度也是数据相关（替换次数与新旧串长度都影响结果），为什么它仍是 ufunc，而 `encode` 却被标为「大概率不会变 ufunc」？

**参考答案**：区别在于「宽度是否可在循环前算出」。`replace` 可以先用 `_count_ufunc` 数出替换次数，再用 `str_len` 算出每个元素的输出宽度并取 `max`，从而预算出统一的 `out_dtype`——它满足 u2-l5 的「路径 A」。而 `encode` 的输出字节数依赖编码过程本身（同一字符串不同编码长度不同，且难以用一个轻量公式预算），无法在循环前确定统一宽度，故难以写成定宽 ufunc。

---

### 4.4 端到端实践：把 `split` 从 `_vec_string` 迁移成 ufunc 的全链路

#### 4.4.1 概念说明

前三个模块分别讲了「测试怎么组织」「StringDType 怎么交叉验证」「`__all__` 怎么标注演化方向」。本模块把它们串起来，回答一个工程问题：**如果决定让 `split`「毕业」成真正的 ufunc（或至少推进它的演化），要动哪些文件、走哪些步骤？**

先说结论——`split` 之所以难，根本不在 Python 包装层，而在**它的输出没有合适的 dtype**。`_split` 现在返回 `np.object_` 数组（每个元素是一个长度不一的 `list`），这违反了 ufunc「输入定长 dtype、输出定长 dtype」的契约。所以迁移的核心障碍是：**先为「变长字符串列表」设计一个可表示的输出 dtype**（例如一种「变长数组的数组」/ ragged array dtype），否则后面所有步骤都无从谈起。

这恰好呼应 `__all__` 的注释 `until behavior has been crystallized`——「行为定型」指的就是「输出如何表示」这件事达成共识。

#### 4.4.2 核心流程（迁移清单）

下面是一份按依赖顺序排列的清单。注意：这不是一个可以机械照搬的菜谱，而是一张「需要决策点」的地图——每一步都依赖上一步的决策。

```text
把 split 迁移成 ufunc 的全链路
  │
  ├─ 0. 前置决策（最难，卡住整个迁移）
  │     为「变长字符串序列」定义输出 dtype（如 ragged string array dtype）
  │     —— 若无此 dtype，split 永远只能返回 object 数组
  │
  ├─ 1. Python 包装层（numpy/_core/strings.py）
  │     a. 把 _split 改名 split，加 @set_module("numpy.strings")
  │     b. 用预算/分流逻辑替换 _vec_string 调用（参考 replace）
  │     c. 取消 __all__ 第 88-89 行的注释，把 "split" 放回 __all__
  │
  ├─ 2. C/C++ 循环注册
  │     a. 定长 dtype：在 string_ufuncs.cpp 的 init_string_ufuncs 加 loop（参考 u3-l12）
  │     b. StringDType：在 stringdtype_ufuncs.cpp 的 init_stringdtype_ufuncs 加 loop（参考 u3-l14）
  │     c. 写 resolve_descriptors 决定输出 dtype（受步骤 0 约束）
  │
  ├─ 3. 门面与存根
  │     a. numpy/strings/__init__.pyi：新增 split 的类型签名
  │     b. numpy/strings/__init__.py：靠 import * 自动带上，无需改（确认即可）
  │
  ├─ 4. 测试
  │     a. test_strings.py：在 TestMethods 加 test_split（dt ∈ {S,U,T}）
  │     b. test_stringdtype.py：把 "split" 从 ONLY_IN_NP_CHAR 移出，
  │        加入 UNARY/BINARY_FUNCTIONS 与对应 NA 行为清单
  │     c. test_strings.py 的 TestOverride：把 split 加进分发归属表
  │     d. test_defchararray.py：处理向后兼容（char.split 仍要返回 object？）
  │
  └─ 5. 文档与兼容
        a. 更新 docstring（去掉 # doctest: +SKIP）
        b. 决定 numpy.char.split 的去留与行为（向后兼容窗口）
```

#### 4.4.3 源码精读（用三个「锚点」串清单）

**锚点一：Python 包装层的目标样子（照抄 `replace`）。** 把 4.3.3 节引用过的 [`replace` 包装](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1327-L1351) 当模板：它有 `@set_module`、有 `char=="T"` 分流、有「预算宽度 + 开 out + 调 ufunc」三段。`split` 若要毕业，Python 层至少要长成这个结构（差异在于输出 dtype 的处理，受步骤 0 约束）。

**锚点二：取消 `__all__` 注释的位置。** 就在 [`__all__` 第 88-89 行](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L88-L89)：把 `# "split",` 这行的注释去掉，`split` 即可经 `from numpy._core.strings import *` 进入门面。这是「公开化」最便宜的一步，但**只有在步骤 0–2 完成后才能做**，否则会把一个半成品暴露成公共 API。

**锚点三：测试改动的「分流表」。** 4.2.3 节引用过的 [`ONLY_IN_NP_CHAR`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_stringdtype.py#L1544-L1549) 是关键：只要 `"split"` 还在这张表里，`test_unary`/`test_binary` 就会去 `np.char` 取实现。迁移完成后必须把它从这张表移除，并视输出形态加入 `UNARY_FUNCTIONS`/`BINARY_FUNCTIONS` 以及 NA 行为清单。同时，[`test_split` 锁死的 `issubclass(A.dtype.type, np.object_)`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_defchararray.py#L628-L635) 必须被改写或提供兼容路径——它是迁移最直接的「挡路测试」。

> 一个现实提醒：这份清单里步骤 0（为变长输出定义 dtype）是 NumPy 社区层面尚未完全解决的开放问题，所以 `split` 至今留在 `# Removed` 一档。本模块的价值在于让你看清「为什么它还没毕业」以及「真要毕业要付多大代价」，而不是给出一个现成补丁。

#### 4.4.4 代码实践

**实践目标**：不改任何源码，仅通过阅读与脚本，产出一份**针对当前代码**的「split 迁移影响面」清单，标注每个需要改动的文件、行号与改动类型。这是纯源码阅读型实践。

**操作步骤**：

1. 打开 [`numpy/_core/strings.py` 的 `__all__`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L73-L90)，确认 `split` 在第 89 行被注释；记录「取消注释」这一改动点。
2. 打开 [`_split` 实现](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1399-L1441)，记录「改名 + 加 `@set_module` + 替换 `_vec_string` 调用」三个改动点。
3. 打开 [`defchararray.py` 的别名导入](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/defchararray.py#L23-L30)，记录「`numpy.char.split` 是否仍要保留 object 输出」这一兼容决策。
4. 打开 [`test_stringdtype.py` 的 `ONLY_IN_NP_CHAR`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_stringdtype.py#L1544-L1549) 与 [`test_defchararray.py` 的 test_split](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_defchararray.py#L628-L635)，记录两个测试改动点。
5. 用下面的脚本，自动枚举仓库里所有「提到 split」的潜在影响点（只读不改）：

```python
# 示例代码：枚举 split 相关影响点（纯阅读用，不在本讲执行）
import subprocess
# 这条命令在你本地仓库根目录运行（待本地验证）：
#   grep -rn "split" numpy/_core/strings.py numpy/_core/defchararray.py \
#             numpy/_core/tests/test_strings.py numpy/_core/tests/test_stringdtype.py \
#             numpy/_core/tests/test_defchararray.py numpy/strings/__init__.pyi
# 把命中行填进下表。
```

把结果整理成一张表（示例模板）：

| 文件 | 行号 | 当前状态 | 迁移改动 | 类型 |
| --- | --- | --- | --- | --- |
| `strings.py` | 89 | 注释掉 | 取消注释 | 公开化 |
| `strings.py` | 1399-1441 | `_split`+`_vec_string` | 改名+`set_module`+预算逻辑 | Python 包装 |
| `string_ufuncs.cpp` | — | 无 split 循环 | 新增 loop+resolve_descriptors | C 循环 |
| `stringdtype_ufuncs.cpp` | — | 无 split 循环 | 新增 StringDType loop | C 循环 |
| `__init__.pyi` | — | 无 split | 新增类型签名 | 存根 |
| `test_stringdtype.py` | 1544-1549 | `ONLY_IN_NP_CHAR` 含 split | 移出该表，加入 UNARY/BINARY | 测试 |
| `test_defchararray.py` | 628-635 | 断言 object 输出 | 改写或加兼容路径 | 测试 |
| `test_strings.py` | TestOverride | 无 split | 加入分发归属表 | 测试 |

**需要观察的现象**：你能仅凭阅读，把「一个函数的公开化」拆成至少 6 个文件、8 个改动点，并指出其中「为变长输出定义 dtype」是卡住全局的前置决策。

**预期结果**：产出一张如上的影响面清单表。

**如果无法确定运行结果**：grep 命令的精确命中行号「待本地验证」，但表格结构是稳定的。

#### 4.4.5 小练习与答案

**练习 1**：在迁移清单里，为什么「取消 `__all__` 第 89 行的注释」必须放在最后做，而不能第一步就做？

**参考答案**：因为 `__all__` 是门面公开 API 的「账本」。一旦取消注释，`np.strings.split` 立刻对用户可见。如果此时 C 循环、resolve_descriptors、测试都还没就位，等于把一个半成品/会出错的功能发布成了公共 API，后续修改还会受向后兼容约束。所以正确顺序是先完成底层（dtype 决策 → C 循环 → Python 包装 → 测试），最后才「开闸」放行 `__all__`。

**练习 2**：迁移完成后，`numpy.char.split` 应当保留还是删除？两种选择各有什么代价？

**参考答案**：保留则需维护「`char.split` 返回 object 数组、`strings.split` 返回新 dtype」两套语义，增加长期维护负担，但向后兼容好；删除则破坏依赖 `np.char.split` 的旧代码，但能让两套命名空间行为统一。NumPy 的惯例（见 u1-l3）是给 `char` 留一个兼容窗口、在新文档里推荐 `strings`，因此「保留 + 标注」通常是更稳妥的选择。

---

## 5. 综合实践

本实践贯穿四个最小模块，目标是你亲手把「测试组织 → 交叉验证 → `__all__` 判据 → 迁移影响面」这条链走通，最终产出一份**可评审的 `split` 迁移设计草案**。全程只读不改源码。

### 步骤 1：用 `TestOverride` 的思路，给任意函数归类（源码阅读型）

1. 打开 [`TestOverride` 的两张 parametrize 表](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_strings.py#L1454-L1506)。
2. 任选 `numpy.strings` 的一个函数（如 `replace`、`capitalize`），判断它应当出现在 `test_override_function`（带 dispatcher）还是 `test_override_ufunc`（裸 ufunc）表里。
3. 用 [`__all__` 注释](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L73-L90) 验证你的判断：它在哪一档？这一档是否与「有无 dispatcher」一致？

> 参考答案：`replace` 在 `# UFuncs` 档，且有 `@array_function_dispatch`，故应在 `test_override_function` 表（实际表中没有它，是因为该表只列了部分代表函数，但按规则它属于 function 分支）。`capitalize` 在 `Will gradually become ufuncs` 档，当前走 `_vec_string`，但有 dispatcher，故也属 function 分支。可见「`__all__` 档位」与「dispatcher 有无」是两个独立维度——前者看输出形态，后者看是否参与 NEP-18。

### 步骤 2：复现交叉验证并定位「行为差异」（脚本型）

```python
import numpy as np

DATA = ["a,b,c", "x,y", "p"]
s = np.array(DATA, dtype="T")
u = np.array(DATA, dtype=np.str_)

# split 当前只能走 np.char，返回 object 数组
print("char.split(T) :", np.char.split(s, ",").tolist())
print("char.split(U) :", np.char.split(u, ",").tolist())
# 两者应一致 —— 这就是 test_unary 交叉验证的简化版
assert np.char.split(s, ",").tolist() == np.char.split(u, ",").tolist()
print("交叉验证通过")
```

**需要观察的现象与预期**：StringDType 与 `str_` 的 `split` 结果逐元素相等。这验证了「即便 split 还没毕业，它在两种 dtype 上的语义仍被测试框架对齐」。具体渲染「待本地验证」。

### 步骤 3：产出迁移设计草案（综合型）

结合步骤 1、2 与 4.4 节的清单，写一份不超过一页的草案，回答：

1. `split` 当前为什么不是 ufunc？（引用 `__all__` 注释与 `_split` 的 object 输出）
2. 若要让它毕业，最大的技术障碍是什么？（变长输出的 dtype 表示）
3. 列出至少 5 个需要改动的文件，并标注每个文件的改动类型（Python 包装 / C 循环 / 存根 / 测试 / 文档）。
4. 指出哪一步是「最后做」的，为什么。（取消 `__all__` 注释，避免半成品暴露）

> 自检：如果你的草案里没有提到「为变长字符串序列定义输出 dtype」这一前置决策，说明你还没抓住 `split` 迁移的核心难点——回头重读 4.4.1。

## 6. 本讲小结

- `test_strings.py` 用「按 dtype 参数化（`dt ∈ {S,U,T}`）+ 按行为归类」两招把 40+ 函数 × 3 dtype 的测试矩阵收敛成几个测试类；`TestMethods` 是主力，`TestMethodsWithUnicode`/`TestMixedTypeMethods`/`TestUnicodeOnlyMethodsRaiseWithBytes` 处理 dtype 差异，`TestOverride` 用两张表守护 NEP-18 分发归属。
- `test_stringdtype.py` 面对额外的 NA 机制，用两招应对：**交叉验证**（同一份数据在 StringDType 与 `str_` 上跑同一函数并断言相等，把「对不对」交给参考实现）+ **NA 笛卡尔积**（`dtype` 夹具把 `na_object` × `coerce` 展开成 12 种变体，再用行为分类清单驱动 NA 预期）。
- `strings.py` 的 `__all__` 是一份带注释的演化路线图，分四档：`# UFuncs`、`Will gradually become ufuncs`、`Will probably not become ufuncs`、`Removed from namespace`；判据是「输出宽度/结构能否仅由输入 dtype 决定或循环前预算」。
- `replace`（输出宽度可预算）与 `_split`（输出为变长列表、只能装 object 数组）是这一判据的两个极端对照，解释了为什么前者是公开 ufunc、后者至今「未定型」。
- 把 `split` 迁移成 ufunc 是一个跨 6 文件、8 改动点的工程，其**前置障碍是为变长输出设计 dtype**；正确顺序是先底层（dtype 决策 → C 循环 → Python 包装 → 测试），最后才取消 `__all__` 注释「开闸」。
- 本系列（u3-l12 ~ u3-l16）从 C++ 循环注册、字符原语、StringDType 循环、`_vec_string` 一路到测试与扩展实践，闭合了「Python 门面 → C/C++ 实现 → 工程化测试与演进」的完整学习回路。

## 7. 下一步学习建议

- **动手跑一遍测试**：在本地 NumPy 源码树上执行 `python -m pytest numpy/_core/tests/test_strings.py -k "TestMethods and multiply"`，亲眼看 `dt ∈ {S,U,T}` 如何把一个方法展开成多组用例；再跑 `test_stringdtype.py::test_unary`，观察 NA 笛卡尔积带来的庞大用例数。
- **追踪一次真实的「毕业」**：在 NumPy 的 git 历史里搜索把某个 `_vec_string` 函数（如 `replace` 或大小写族）改写成 ufunc 的 PR，对照本讲的迁移清单，看真实社区改动落在哪几个文件上。可从 `git log -- numpy/_core/src/umath/string_ufuncs.cpp` 切入。
- **回看 u2-l8 与 u3-l15**：用本讲「输出宽度能否由输入 dtype 决定」的判据，重新审视那两讲里 `_vec_string` 函数清单，你会对「会变 ufunc」与「大概不会变」的边界有更扎实的理解。
- **延伸阅读**：通读 `numpy/_core/tests/test_stringdtype.py` 的 NA 相关清单（`NAN_PRESERVING_FUNCTIONS`/`BOOL_OUTPUT_FUNCTIONS`/`NULLS_ALWAYS_ERROR` 等），理解 StringDType 的 NA 语义如何在测试层被精确规约——这是写出健壮 ufunc 循环的必要前提。
