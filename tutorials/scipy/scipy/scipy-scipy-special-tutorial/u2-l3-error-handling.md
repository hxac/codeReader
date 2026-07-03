# 错误处理：`seterr` / `geterr` / `errstate` 与特殊函数告警

## 1. 本讲目标

u2-l1 讲了「special 里的函数**怎么算**（ufunc、类型分发、广播）」，u2-l2 讲了「**算的是什么**（函数家族分类）」。本讲要回答第三个基本问题：**算出问题时怎么办？**

特殊函数在数值上有大量「灰色地带」：输入落进函数的定义域外（比如对负数取对数）、结果溢出、算法收敛太慢、精度丢失……这些都不是 Python 意义上的「程序错误」，而是「数值事件」。如果每一次都抛异常，那么对一个大数组逐元素求值时，只要有一个元素触发事件，整个调用就崩了——这对以「批量逐元素」为生命的 ufunc 是灾难。

所以 `scipy.special` 选择了一套独特的错误处理哲学：

> **默认情况下静默地返回一个占位值（通常是 `NaN` 或 `inf`），不抛异常、甚至不告警；只有「内存分配失败」这一类才默认抛异常。** 其余错误类型，由用户通过 `seterr` / `geterr` / `errstate` 三件套自行决定是「忽略 / 告警 / 抛异常」。

学完本讲，你应该能够：

- 说出 special 错误处理的**默认行为**：哪 10 类错误、哪一类默认就抛异常、其余默认如何处理。
- 掌握**三件套** `seterr`（设）、`geterr`（查）、`errstate`（上下文管理器：临时设、用完恢复）的用法与作用域。
- 认识两个 Python 类 `SpecialFunctionWarning` 与 `SpecialFunctionError`，并理解它们是如何由 C 内核「隔着 GIL」触发出来的（深层的 C→Python 桥接留给 u7，本讲只建立直觉）。

> 一句话定位：special 的错误不是「抛或不抛」的二选一，而是「**按错误类别分别配置 ignore / warn / raise**」的三态开关，默认几乎全静默。

## 2. 前置知识

- **ufunc 与逐元素求值**（承接 u2-l1）：special 绝大多数函数是 NumPy ufunc，对整个数组批量求值。这意味着「一个数组里某个元素触发错误」是常态而非意外——这正是 special 选择「默认静默」的根本原因。
- **`NaN` / `inf` 占位**：浮点数有两个特殊的「非数值」——`NaN`（Not a Number，表示「算不出来」）和 `inf`（无穷大，表示「结果太大」）。special 在域错误时通常返回 `NaN`，在溢出时通常返回 `inf`。
- **Python 的 `warnings` 模块与异常**：Python 区分「警告（Warning）」和「异常（Exception）」。警告默认只是打印一行信息、**不中断**程序；异常会**中断**程序并沿调用栈向上抛，除非被 `try/except` 捕获。本讲的 `SpecialFunctionWarning` 继承自 `Warning`，`SpecialFunctionError` 继承自 `Exception`。
- **上下文管理器（`with` 语句）**：`with foo() as x:` 会在进入代码块前调用 `foo.__enter__()`、退出时（无论是否出错）调用 `foo.__exit__()`。本讲的 `errstate` 正是用这个机制实现「临时改、用完恢复」。

> 名词速查：**GIL（Global Interpreter Lock）** 是 CPython 的全局解释器锁。special 的数值内核跑在 C/C++ 层、常常不持有 GIL（为了能并行/释放 GIL 的热循环），但「发告警、抛异常」必须回到 Python 世界，这就需要「获取 GIL」。本讲末尾会点到为止，细节在 u7。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲怎么用它 |
|------|------|--------------|
| [`_ufuncs_extra_code.pxi`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs_extra_code.pxi) | **三件套的真正实现**。`seterr`/`geterr`/`errstate` 三个 Python 函数就定义在这里，构建时被拼进生成的 `_ufuncs.pyx` | 4.2 精读三件套；含「字符串↔错误码」「字符串↔动作码」两张映射表，以及把状态同步到 5 个扩展模块的关键循环 |
| [`_sf_error.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_sf_error.py) | 两个 Python 类：`SpecialFunctionWarning`、`SpecialFunctionError` | 4.3 精读这两个类，以及让告警「每次都显示」的 `simplefilter` |
| [`sf_error.pxd`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/sf_error.pxd) | Cython 对 C 层错误机制的声明：错误类型枚举 `sf_error_t`、动作枚举 `sf_action_t`、以及测试用 ufunc `_sf_error_test_function` | 4.1 列出 10 类错误 + 3 种动作的「权威清单」；4.1 的实践用它逐类触发 |
| [`sf_error.cc`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/sf_error.cc) | C 层实现：默认动作表、错误信息字符串表、以及把 C 错误「隔 GIL 桥」到 Python 告警/异常的 `sf_error_v` | 4.1 论证「为何只有 memory 默认 raise」；4.3 给出 warn/raise 如何跨越 C→Python 边界（点到为止，深挖在 u7） |
| [`_ufuncs.pyi`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi) | 类型桩，声明 `geterr`/`seterr`/`errstate` 的签名与 `_sf_error_test_function` 的 ufunc 身份 | 4.2 / 4.1 确认这些名字是 `_ufuncs` 扩展模块对外暴露的公开符号 |
| [`__init__.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py) | 把两个错误类拼进 `special` 命名空间 | 4.3 说明 `SpecialFunctionWarning/Error` 从哪里来、为何能在顶层直接用 |

> 说明：本讲聚焦**用户侧的 Python 接口**。C 内核如何用「线程局部存储」保存动作、如何检测浮点异常（FPE）、`_ufuncs_extra_code_common.pxi` 如何把硬件浮点异常翻译成 `sf_error`，这些底层机制是 **u7（错误处理：C 层 sf_error 的贯通）** 的主题，本讲只在 4.3 给出直觉、不做深入。

## 4. 核心概念与源码讲解

### 4.1 错误类型分类：10 类错误 × 3 种处理动作

#### 4.1.1 概念说明

数值特殊函数在计算过程中可能遇到多种「异常状况」。special 把它们归成 **10 类**（外加一个表示「无错误」的 `OK`）。你不需要背下来，但要知道每类大致指什么，才能在配置时对号入座：

| 错误类别（key） | 含义 | 典型场景 |
|------|------|----------|
| `singular` | 奇点 | `gammaln(0)`：Gamma 函数在 0 处有极点 |
| `underflow` | 下溢 | 结果太小，落入浮点下溢区间 |
| `overflow` | 上溢 | 结果太大，超出浮点上限，得 `inf` |
| `slow` | 收敛太慢 | 迭代算法步数过多 |
| `loss` | 精度丢失 | 有效数字损失严重 |
| `no_result` | 没算出结果 | 算法彻底失败，无可用返回值 |
| `domain` | 域错误 | 输入不在函数定义域内，如 `spence(-1)`、`log(-1)` |
| `arg` | 参数错误 | 传入的参数（非输入值本身）非法 |
| `other` | 其他 | 无法归类的错误 |
| `memory` | 内存分配失败 | 内核里 `malloc` 失败 |

而对**每一类**错误，你可以选择 **3 种动作**之一：

| 动作 | 行为 |
|------|------|
| `ignore` | 什么都不做，函数照常返回一个占位值（如 `NaN` / `inf`） |
| `warn` | 通过 Python `warnings` 模块发出一条 `SpecialFunctionWarning`，函数仍返回占位值，**不中断** |
| `raise` | 抛出 `SpecialFunctionError`，**中断**当前调用 |

于是完整的配置空间是「10 类 × 3 动作」。special 出厂时的默认值是：**9 类全部 `ignore`，唯独 `memory` 默认 `raise`**。理由很朴素：域错误、溢出之类可以用 `NaN`/`inf` 占位并继续算；但内存都分配失败了，连占位值都给不出，只能抛异常。

#### 4.1.2 核心流程

当 C 内核在计算中检测到错误，它会带着「错误码」调用 `sf_error(...)`。最终这条错误如何处置，取决于一张**默认动作表**。流程如下：

```
C 内核检测到错误
      │
      ▼
sf_error(func_name, code, ...)          # code ∈ {SINGULAR, OVERFLOW, ...}
      │
      ▼
查「默认动作表」sf_error_actions[code]   # 读出 IGNORE / WARN / RAISE
      │
      ├─ IGNORE ──► 直接返回，内核继续返回占位值 (NaN/inf)
      ├─ WARN   ──► 跨进 Python，发 SpecialFunctionWarning
      └─ RAISE  ──► 跨进 Python，置 SpecialFunctionError
```

关键在于这张「默认动作表」的初值——它就是上一节说的「9 类 ignore + memory raise」。而 `seterr`/`errstate` 的工作，本质上就是**改写这张表**。

#### 4.1.3 源码精读

错误类别的「权威清单」是 C 层的枚举 `sf_error_t`，Cython 侧通过 [`sf_error.pxd`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/sf_error.pxd#L4-L16) 声明出来——11 个成员对应 10 类错误 + `OK`（外加哨兵 `_LAST`）：

```cython
cdef extern from "sf_error.h":
    ctypedef enum sf_error_t:
        OK "SF_ERROR_OK"
        SINGULAR "SF_ERROR_SINGULAR"
        UNDERFLOW "SF_ERROR_UNDERFLOW"
        OVERFLOW "SF_ERROR_OVERFLOW"
        SLOW "SF_ERROR_SLOW"
        LOSS "SF_ERROR_LOSS"
        NO_RESULT "SF_ERROR_NO_RESULT"
        DOMAIN "SF_ERROR_DOMAIN"
        ARG "SF_ERROR_ARG"
        OTHER "SF_ERROR_OTHER"
        MEMORY "SF_ERROR_MEMORY"
        LAST "SF_ERROR__LAST"
```

注意 Cython 这里用了 `OK "SF_ERROR_OK"` 的语法：等号左边是 Cython 里用的名字，引号里是真正的 C 宏名。

3 种动作则定义在 [`sf_error.h`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/sf_error.h#L9-L13) 的 `sf_action_t` 枚举里：

```c
typedef enum {
    SF_ERROR_IGNORE = 0,  /* Ignore errors */
    SF_ERROR_WARN,        /* Warn on errors */
    SF_ERROR_RAISE        /* Raise on errors */
} sf_action_t;
```

「默认动作表」的初值在 [`sf_error.cc`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/sf_error.cc#L11-L24)，逐行注释标出了每个槽位对应的错误类别——只有 `MEMORY` 那一行是 `SF_ERROR_RAISE`，其余全是 `IGNORE`：

```c
static volatile SCIPY_TLS sf_action_t sf_error_actions[] = {
    SF_ERROR_IGNORE, /* SF_ERROR_OK */
    SF_ERROR_IGNORE, /* SF_ERROR_SINGULAR */
    SF_ERROR_IGNORE, /* SF_ERROR_UNDERFLOW */
    SF_ERROR_IGNORE, /* SF_ERROR_OVERFLOW */
    SF_ERROR_IGNORE, /* SF_ERROR_SLOW */
    SF_ERROR_IGNORE, /* SF_ERROR_LOSS */
    SF_ERROR_IGNORE, /* SF_ERROR_NO_RESULT */
    SF_ERROR_IGNORE, /* SF_ERROR_DOMAIN */
    SF_ERROR_IGNORE, /* SF_ERROR_ARG */
    SF_ERROR_IGNORE, /* SF_ERROR_OTHER */
    SF_ERROR_RAISE,  /* SF_ERROR_MEMORY */   ← 唯一默认抛异常的
    SF_ERROR_IGNORE  /* SF_ERROR__LAST */
};
```

> 这里有个细节名词 `SCIPY_TLS`：它表示「线程局部存储（Thread-Local Storage）」。也就是说这张动作表是**每个线程一份**的，这样不同线程可以有不同的错误策略、互不干扰。`seterr` 改的是当前线程的副本。完整细节在 u7。

旁边还有一张 [`sf_error_messages`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/sf_error.cc#L26-L39) 字符串表，它把错误码翻译成人类可读的短句（`"domain error"`、`"singularity"`……），最终会拼进告警/异常的消息里。

为了让你能在 Python 里**逐类触发**这 10 类错误来观察，special 还专门提供了一个测试用 ufunc [`_sf_error_test_function`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/sf_error.pxd#L30-L42)——它接收一个错误码（1~10），就调用 `sf_error` 触发对应类别。它对应的类型桩声明见 [`_ufuncs.pyi:L275`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L275)，标为 `np.ufunc`。

#### 4.1.4 代码实践

**实践目标**：用 `_sf_error_test_function` 把 10 类错误各触发一遍，配合 `geterr()` 直观确认「默认行为」。

**操作步骤**：

```python
import warnings
import scipy.special as sc
from scipy.special._ufuncs import _sf_error_test_function

# 1. 看默认配置：10 类里 9 类 ignore，只有 memory 是 raise
for k, v in sorted(sc.geterr().items()):
    print(f"{k:10s}: {v}")

# 2. 在默认（ignore）状态下逐类触发，应全程无告警、无异常
names = {1:'singular', 2:'underflow', 3:'overflow', 4:'slow', 5:'loss',
         6:'no_result', 7:'domain', 8:'arg', 9:'other', 10:'memory'}
with warnings.catch_warnings():
    warnings.simplefilter("error")   # 把任何告警升级成异常，以验证「确实没告警」
    for code in [1, 2, 3, 4, 5, 6, 7, 8, 9]:
        print(f"  code={code:2d} ({names[code]:10s}) ->",
              _sf_error_test_function(code))   # 预期：静默返回 0
```

**需要观察的现象**：

- 第 1 步打印出 10 行，其中只有 `memory: raise`，其余都是 `ignore`。
- 第 2 步循环 9 个非 memory 的码（1~9），在 `simplefilter("error")` 下**不应**抛异常——证明默认确实是静默的。

**预期结果**：

- 循环顺利跑完、打印 9 行 `-> 0`，没有 `SpecialFunctionWarning`、没有 `SpecialFunctionError`。
- 注意**故意没**触发 `code=10`（memory）：因为 memory 默认 `raise`，会直接抛 `SpecialFunctionError`。你可以单独试 `_sf_error_test_function(10)` 来验证。

> 「待本地验证」：上面假设 `simplefilter("error")` 下循环不抛异常。如果你观察到某类错误反而告警了，说明该类的默认动作可能不是 `ignore`——对照 `sc.geterr()` 输出排查。

#### 4.1.5 小练习与答案

**练习 1**：为什么 special 选择让 `memory` 默认 `raise`，而 `domain`/`overflow` 默认 `ignore`？

> **参考答案**：域错误、溢出可以用 `NaN`/`inf` 这样的占位值「假装算完了」继续向下算（调用者拿到 `NaN` 自行处理）；但内存分配失败意味着连占位值都无从而出、后续计算也无法进行，硬撑下去只会引发更难定位的崩溃，所以必须立刻抛异常。

**练习 2**：`sf_error_actions` 数组有 12 个槽位（下标 0~11），但错误类别只有 10 类（不含 `OK`）。多出来的两个槽位分别对应什么？

> **参考答案**：下标 0 对应 `SF_ERROR_OK`（「无错误」，虽不会触发但占位）；下标 11 对应 `SF_ERROR__LAST`（哨兵，标记枚举末尾，用于边界检查，如 `sf_error_v` 里 `code >= SF_ERROR__LAST` 就回退为 `OTHER`）。参见 [`sf_error.cc:L11-L24`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/sf_error.cc#L11-L24)。

### 4.2 错误处理三件套：`seterr` / `geterr` / `errstate`

#### 4.2.1 概念说明

知道了「10 类错误 × 3 动作」之后，如何**实际去配置**它们？special 提供了三个 Python 函数，命名刻意与 NumPy 的浮点错误三件套（`numpy.seterr/geterr/errstate`）对齐，方便记忆：

| 函数 | 作用 | 是否改变全局状态 |
|------|------|------------------|
| `geterr()` | **查询**当前 10 类错误各自的处理动作，返回 dict | 否，只读 |
| `seterr(**kwargs)` | **设置**某些（或全部）类别的动作，返回**修改前的旧配置** | 是，改变当前线程的默认动作表 |
| `errstate(**kwargs)` | **上下文管理器**：进入时 `seterr`、退出时恢复旧配置 | 临时改变，`with` 块结束后自动复原 |

三者共用同一套关键词参数：`all`（一次性设全部 10 类）、以及 `singular/underflow/overflow/slow/loss/no_result/domain/arg/other/memory` 中的任意组合，取值都是 `'ignore'/'warn'/'raise'`。

**作用域**很关键：`seterr` 改的是**当前线程**的全局动作表（回想 4.1 的 `SCIPY_TLS`），一旦设置就对**之后所有的** special 调用生效，直到你再次改它。这意味着 `seterr` 是「带状态的全局副作用」——如果在某个函数里 `seterr(domain='raise')` 却忘了恢复，会污染调用方后续的所有计算。因此**推荐优先用 `errstate` 上下文管理器**，它保证「用完即恢复」。

#### 4.2.2 核心流程

三件套围绕「读—改—恢复」这张状态表运作。`errstate` 的生命周期最能体现这套设计：

```
进入 with errstate(domain='raise'): ──► __enter__ 调 seterr(domain='raise')
                                           │  seterr 先 geterr() 存下旧状态 oldstate
                                           │  再改写动作表
                                           ▼
                                   with 块内的 special 调用按新动作执行
                                           │
退出 with（无论正常退出还是抛异常）──► __exit__ 调 seterr(**oldstate) 恢复
```

`errstate` 之所以能可靠恢复，关键在 `seterr` **总是返回旧配置**：`__enter__` 把它存起来，`__exit__` 原样喂回去。

还有一条不那么显眼但很重要的工程细节：**special 由多个独立编译的扩展模块组成**（`_ufuncs`、`_ufuncs_cxx`、`_special_ufuncs`、`_gufuncs`、`_ellip_harm_2`，见 u1-l3），**每个模块都链接了各自的一份 `sf_error.cc`**，因而各有一份独立的动作表。`seterr` 必须**同步改写全部 5 份**，否则会出现「在 `spence` 上生效、在 Boost 实现的 `betainc` 上不生效」的诡异不一致。

#### 4.2.3 源码精读

三件套的真正实现不在某个 `.py` 里，而在 [`_ufuncs_extra_code.pxi`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs_extra_code.pxi)——这是一段构建期被代码生成器拼进生成的 `_ufuncs.pyx` 的 Cython 片段（拼接机制见 u3、深层在 u7）。

首先是两张映射表，负责在「人类可读字符串」与「C 枚举整数」之间转换。[错误码表 `_sf_error_code_map`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs_extra_code.pxi#L8-L20) 把 10 个字符串 key 映成 1~10：

```python
_sf_error_code_map = {
    # skip 'ok'
    'singular': 1, 'underflow': 2, 'overflow': 3, 'slow': 4, 'loss': 5,
    'no_result': 6, 'domain': 7, 'arg': 8, 'other': 9, 'memory': 10
}
```

[动作表 `_sf_error_action_map`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs_extra_code.pxi#L22-L29) 是个**双向**映射，既能 `'raise' -> 2`（设置时用），也能 `2 -> 'raise'`（查询时用）：

```python
_sf_error_action_map = {
    'ignore': 0, 'warn': 1, 'raise': 2,
    0: 'ignore', 1: 'warn', 2: 'raise'
}
```

[`geterr`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs_extra_code.pxi#L32-L79) 就是对 10 类逐一查表翻译：

```python
def geterr():
    err = {}
    for key, code in _sf_error_code_map.items():
        action = sf_error.get_action(code)        # 调 C 函数读当前动作
        err[key] = _sf_error_action_map[action]   # 整数 -> 字符串
    return err
```

[`seterr`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs_extra_code.pxi#L82-L178) 的核心是处理 `all` 关键字（先全量设、再用具体类别覆盖），以及那段**同步 5 个扩展模块**的关键循环：

```python
olderr = geterr()                       # ① 存旧状态，函数末尾返回它
...
for error, action in kwargs.items():
    action = _sf_error_action_map[action]   # 字符串 -> 整数
    code = _sf_error_code_map[error]        # 字符串 -> 错误码
    # Error handling state must be set for all relevant
    # extension modules in synchrony, since each carries
    # a separate copy of this state.
    _set_action(code, action)                              # _ufuncs 自己
    scipy.special._ufuncs_cxx._set_action(code, action)    # Boost C++ 那批
    scipy.special._special_ufuncs._set_action(code, action)
    scipy.special._gufuncs._set_action(code, action)
    scipy.special._ellip_harm_2._set_action(code, action)
return olderr
```

> 这 5 个 `_set_action` 各自调到对应扩展模块里、由 [`_ufuncs_extra_code_common.pxi`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs_extra_code_common.pxi#L29-L31) 提供的同名小函数，最终落到 C 的 `sf_error_set_action` 改写那张 TLS 动作表。

最后是 [`errstate`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs_extra_code.pxi#L181-L234)，它本身极薄——`__enter__` 调 `seterr` 存旧状态、`__exit__` 用旧状态恢复：

```python
class errstate:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __enter__(self):
        self.oldstate = seterr(**self.kwargs)   # 进入：设新值，存旧值

    def __exit__(self, exc_type, exc_value, traceback):
        seterr(**self.oldstate)                 # 退出：恢复旧值
```

注意 `__exit__` **不带条件**地恢复——哪怕 `with` 块里抛了 `SpecialFunctionError`，退出时也会先把全局状态还原，再把异常继续向上抛。这正是 `errstate` 比「手动 seterr + try/finally」更省心之处。

#### 4.2.4 代码实践

**实践目标**：完整走一遍本讲规格要求的那条主线——默认静默 → `errstate` 临时升为 raise → `geterr` 对比前后状态。

**操作步骤**：

```python
import scipy.special as sc

# ① 查看默认状态：domain 应为 ignore
print("进入前:", sc.geterr()['domain'])

# ② 默认状态下，spence(-1) 是 domain 错误，静默返回占位值（NaN）
val = sc.spence(-1)
print("默认 spence(-1) =", val)        # 预期：NaN（待本地确认具体值）

# ③ 用 errstate 临时把「所有类别」升为 raise，捕获异常
print("进入前(全量):", sc.geterr())
try:
    with sc.errstate(all='raise'):
        print("  with 块内:", sc.geterr()['domain'])   # 预期：raise
        sc.spence(-1)                                   # 预期：抛 SpecialFunctionError
except sc.SpecialFunctionError as e:
    print("  捕获到:", e)

# ④ with 块退出后，状态应已恢复
print("退出后:", sc.geterr()['domain'])                # 预期：回到 ignore
```

**需要观察的现象**：

- ② 默认调用不抛异常，`val` 是 `NaN`（域错误的占位值）。
- ③ 进入 `with errstate(all='raise')` 后，`geterr()` 里**每一类**都变成 `raise`；再次调用 `spence(-1)` 抛出 `SpecialFunctionError`，消息里含 `"domain error"`。
- ④ 退出 `with` 后，`domain` 又回到 `ignore`——证明 `errstate` 的「自动恢复」生效。

**预期结果**：

```
进入前: ignore
默认 spence(-1) = nan
进入前(全量): {'singular':'ignore', ..., 'domain':'ignore', 'memory':'raise', ...}
  with 块内: raise
  捕获到: scipy.special/...: domain error ...
退出后: ignore
```

> 「待本地验证」：`spence(-1)` 的默认返回值。本讲按域错误约定记为 `NaN`，请以本地实际输出为准（重点是「不抛异常」这一行为，而非具体数值）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `seterr` 要把同一个动作写到 5 个不同的扩展模块里？如果只写 `_ufuncs` 一个会怎样？

> **参考答案**：因为 special 的 5 个扩展模块各自链接了一份 `sf_error.cc`、各持一份 TLS 动作表。若只改 `_ufuncs`，那么由 `_ufuncs_cxx`（Boost 实现，如 `betainc`）或 `_special_ufuncs` 注册的函数仍用旧动作，导致「同一个错误策略在不同函数上表现不一致」。参见 [`_ufuncs_extra_code.pxi:L169-L176`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs_extra_code.pxi#L169-L176) 的注释与循环。

**练习 2**：`errstate(all='raise', singular='ignore')` 这句里，`singular` 最终是 `raise` 还是 `ignore`？为什么？

> **参考答案**：是 `ignore`。因为 `seterr` 在处理 `all` 时，先用 `all` 的值把**所有**类别设一遍，**然后**再用后续出现的具体类别覆盖（参见 [`seterr` 中 `for key, value in kwargs.items(): newkwargs[key] = value`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs_extra_code.pxi#L159-L164)）。所以「除了某类之外全部 raise」的惯用写法是 `errstate(all='raise', 某类='ignore')`。

### 4.3 警告/异常类：`SpecialFunctionWarning` 与 `SpecialFunctionError`

#### 4.3.1 概念说明

三件套里的 `warn` 和 `raise` 分别对应两个具体的 Python 对象：

- [`SpecialFunctionWarning`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_sf_error.py#L5-L7)：继承自 `Warning`。当某类错误动作设为 `warn` 时，C 内核会发出它的一个实例。**警告不中断程序**，函数仍返回占位值。
- [`SpecialFunctionError`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_sf_error.py#L13-L15)：继承自 `Exception`。当动作设为 `raise`（或 `memory` 错误默认触发）时抛出，**中断当前调用**。

这两个类都定义在 [`_sf_error.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_sf_error.py)，再经 [`__init__.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L786) 的 `from ._sf_error import SpecialFunctionWarning, SpecialFunctionError` 拼进 `special` 顶层命名空间，并写进 [`__all__`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L827-L828)，所以你能直接写 `sc.SpecialFunctionError`。

这里有一个值得注意的工程细节：`SpecialFunctionWarning` 定义之后，紧接着有一行 [`warnings.simplefilter("always", category=SpecialFunctionWarning)`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_sf_error.py#L10)。Python 的 `warnings` 模块默认会**对同一位置的重复告警只显示一次**（去重），这对「逐元素求值的 ufunc」很不友好——一个数组里可能有成千上万个元素触发同一个域错误，但用户希望至少看到一次提示。`simplefilter("always", ...)` 就是把这条告警改成「每次都显示」，避免被去重吞掉。

#### 4.3.2 核心流程

真正有意思的问题是：**这两个 Python 类是如何被 C 内核「触发」出来的？** 数值内核跑在 C/C++ 里、常常不持有 GIL，但「创建 Python 异常对象」必须在持有 GIL 的 Python 世界里完成。这套「C→Python 跨界」的逻辑在 [`sf_error.cc` 的 `sf_error_v`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/sf_error.cc#L51-L123)，流程（本讲只给直觉，细节在 u7）：

```
C 内核 sf_error(func_name, code, ...)
      │
      ▼  读动作表
   action = IGNORE / WARN / RAISE
      │  （IGNORE 直接 return，下面只看 WARN/RAISE）
      ▼  ① 拼 "scipy.special/<func>: <错误消息>" 字符串
      ▼  ② PyGILState_Ensure()        ← 跨进 Python 世界（拿 GIL）
      ▼  ③ import scipy.special
      ▼  ④ 按动作取类：
      │       WARN  → getattr(scipy.special, "SpecialFunctionWarning")
      │       RAISE → getattr(scipy.special, "SpecialFunctionError")
      ▼  ⑤ 发信号：
      │       WARN  → PyErr_WarnEx(Warning类, msg)      ← 不中断
      │       RAISE → PyErr_SetString(Error类, msg)     ← 设置挂起异常
      ▼  ⑥ PyGILState_Release()       ← 还 GIL
```

关键洞察有二：

1. **类是用名字「按需取回」的**：C 代码并不在编译期持有 Python 类的指针，而是运行时 `import scipy.special` 再 `getattr(..., "SpecialFunctionWarning")`。这就是为什么两个类必须挂在 `scipy.special` 顶层命名空间上——C 层要靠这个名字找到它们。
2. **WARN 与 RAISE 的「中断性」差异来自 Python 信号机制本身**：`PyErr_WarnEx` 只是登记一条告警（除非被 `simplefilter` 升级，否则不中断）；`PyErr_SetString` 则是设置一个「挂起的异常」，等控制权回到 Python 时真正抛出。

#### 4.3.3 源码精读

两个类的定义极简（[`_sf_error.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_sf_error.py#L1-L16) 全文）：

```python
"""Warnings and Exceptions that can be raised by special functions."""
import warnings


class SpecialFunctionWarning(Warning):
    """Warning that can be emitted by special functions."""
    pass


warnings.simplefilter("always", category=SpecialFunctionWarning)


class SpecialFunctionError(Exception):
    """Exception that can be raised by special functions."""
    pass
```

C→Python 桥接的关键片段在 [`sf_error_v`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/sf_error.cc#L80-L119)：先 `PyGILState_Ensure()` 拿锁，再 `PyImport_ImportModule("scipy.special")` 取回模块，按动作 `getattr` 到对应的类，最后用 `PyErr_WarnEx`（告警）或 `PyErr_SetString`（异常）发信号：

```c
save = PyGILState_Ensure();                                     // 拿 GIL
...
scipy_special = PyImport_ImportModule("scipy.special");          // ① 取回模块
...
if (action == SF_ERROR_WARN) {
    warning_or_error_class = PyObject_GetAttrString(scipy_special,
                                  "SpecialFunctionWarning");     // ② WARN 取 Warning 类
} else if (action == SF_ERROR_RAISE) {
    warning_or_error_class = PyObject_GetAttrString(scipy_special,
                                  "SpecialFunctionError");      //    RAISE 取 Error 类
}
...
if (action == SF_ERROR_WARN) {
    PyErr_WarnEx(warning_or_error_class, msg, 1);               // ③a 登记告警
} else if (action == SF_ERROR_RAISE) {
    PyErr_SetString(warning_or_error_class, msg);               // ③b 设置挂起异常
}
...
PyGILState_Release(save);                                        // 还 GIL
```

> 这段代码注释里有一句很关键：「**For ufuncs the return value is ignored! We rely on the fact that the Ufunc loop will call `PyErr_Occurred()` later on.**」——即 ufunc 的内层循环会先 `PyErr_SetString` 设置异常、继续跑完，等整轮循环结束后 NumPy 框架再统一检查 `PyErr_Occurred()` 把异常抛出来。这套与 ufunc 框架的协作细节，连同 `sf_error_check_fpe` 如何把**硬件浮点异常**（除零、溢出……）翻译成 `sf_error`，都是 u7 的主题。

类型桩侧，[`_ufuncs.pyi`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L245-L256) 把三件套声明为 `_ufuncs` 模块导出的公开 Python 符号（不是 ufunc，是普通函数/类）：

```python
def geterr() -> dict[str, str]: ...
def seterr(**kwargs: str) -> dict[str, str]: ...

class errstate:
    def __init__(self, **kargs: str) -> None: ...
    def __enter__(self) -> None: ...
    def __exit__(self, exc_type, exc_value, traceback) -> None: ...
```

#### 4.3.4 代码实践

**实践目标**：对比 `warn` 与 `raise` 两种动作在「是否中断」「如何被捕获」上的差异，并确认 `SpecialFunctionWarning` 因 `simplefilter("always")` 而每次都显示。

**操作步骤**：

```python
import warnings
import scipy.special as sc

# gammaln(0) 是 singular（奇点）错误，默认返回 inf、不告警
print("默认 gammaln(0) =", sc.gammaln(0))            # 预期：inf，无告警

# ① 升级为 warn：应每次都打印 SpecialFunctionWarning，但函数仍返回 inf
with sc.errstate(singular='warn'):
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        val = sc.gammaln(0)
        print("warn 模式返回值 =", val)              # 预期：inf（没中断）
        print("收到告警数 =", len(w),
              "类型 =", w[0].category.__name__ if w else None)

# ② 升级为 raise：应抛 SpecialFunctionError，函数被中断
with sc.errstate(singular='raise'):
    try:
        sc.gammaln(0)
    except sc.SpecialFunctionError as e:
        print("raise 模式捕获:", repr(e))
```

**需要观察的现象**：

- 默认：打印 `inf`，无告警。
- ① warn 模式：`val` 仍是 `inf`（说明**没中断**），且 `w` 里至少收到一条 `SpecialFunctionWarning`。
- ② raise 模式：抛出 `SpecialFunctionError`，被 `except` 捕获。

**预期结果**：warn 不中断、raise 中断；两者用的分别是 `SpecialFunctionWarning` 与 `SpecialFunctionError` 两个不同的类，正是 `_sf_error.py` 里定义的那一对。

#### 4.3.5 小练习与答案

**练习 1**：`SpecialFunctionWarning` 定义后为什么紧跟一行 `warnings.simplefilter("always", ...)`？不加会怎样？

> **参考答案**：Python `warnings` 默认对「同一调用位置」的重复告警去重（只显示一次）。但 special 的函数多为逐元素 ufunc，一个数组里可能有大量元素触发同一错误，用户理应至少看到提示；且「每次都显示」便于调试。所以专门把这一类告警设为 `always`。参见 [`_sf_error.py:L10`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_sf_error.py#L10)。

**练习 2**：C 内核 `sf_error_v` 是用「字符串名字」`getattr(scipy.special, "SpecialFunctionError")` 来取类的，而不是在编译期缓存类指针。这暗示了 `SpecialFunctionError` 必须满足什么条件？

> **参考答案**：它必须**挂在 `scipy.special` 顶层命名空间上、且可被 `getattr` 找到**。这正是 [`__init__.py` 的 `from ._sf_error import ...`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L786) 把两个类提升到顶层的原因——如果某天有人把它们改成只在 `_sf_error` 子模块里、不在顶层暴露，C 层的 `getattr` 就会取不到类、告警/异常会静默失效。

## 5. 综合实践

**任务**：写一个小工具 `check_special_errors(fun, args)`，在三种动作下分别调用同一个 special 函数，报告「返回值 / 是否告警 / 是否抛异常」，从而**一眼看清 ignore/warn/raise 三态的区别**，并验证 `errstate` 的状态隔离。

```python
import warnings
import scipy.special as sc

def check_special_errors(fun, args, category='all'):
    """在 ignore / warn / raise 三种动作下分别调用 fun(*args)，打印结果对比。"""
    results = {}
    for action in ['ignore', 'warn', 'raise']:
        with sc.errstate(**{category: action}):
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                try:
                    val = fun(*args)
                    results[action] = ("返回", val, f"{len(w)} 条告警", "无异常")
                except sc.SpecialFunctionError as e:
                    results[action] = ("抛异常", None, "-", repr(e)[:50])
    # 验证 errstate 退出后状态已恢复
    assert sc.geterr() == sc.geterr(), "状态未恢复（不应发生）"
    return results

# 试用：spence(-1) 触发 domain 错误
for action, info in check_special_errors(sc.spence, (-1,), 'domain').items():
    print(f"{action:7s} -> {info}")
```

**期望你观察到**：

- `ignore`：返回 `NaN`、0 告警、无异常。
- `warn`：返回 `NaN`、有 `SpecialFunctionWarning`、无异常。
- `raise`：抛 `SpecialFunctionError`、无返回值。
- 三次调用结束后，`sc.geterr()` 与初始一致——`errstate` 的恢复可靠。

**延伸思考**：把 `category='domain'` 换成 `category='all'`，再对 `sc.gammaln(0)`（singular）调用，体会「`all` 一次性覆盖全部类别」的便利。如果想深入到「C 内核到底如何检测到这些错误并跨界触发」，请进入 u7。

## 6. 本讲小结

- special 的错误处理哲学是「**默认静默**」：10 类错误中 9 类默认 `ignore`（返回 `NaN`/`inf` 占位），**只有 `memory` 默认 `raise`**——因为内存失败给不出占位值。依据是 [`sf_error.cc` 的默认动作表](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/sf_error.cc#L11-L24)。
- 错误分 **10 类**（`singular`/`underflow`/`overflow`/`slow`/`loss`/`no_result`/`domain`/`arg`/`other`/`memory`），每类可选 **3 种动作**（`ignore`/`warn`/`raise`），共 10×3 的配置空间；类别清单来自 `sf_error_t` 枚举、动作来自 `sf_action_t` 枚举。
- **三件套**：`geterr()` 查、`seterr(**kw)` 设（返回旧配置）、`errstate(**kw)` 上下文管理器（进入设、退出自动恢复）。三者实现在 [`_ufuncs_extra_code.pxi`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs_extra_code.pxi)，靠两张字符串↔整数映射表驱动。
- `seterr` 会把动作**同步写到 5 个扩展模块**（`_ufuncs`/`_ufuncs_cxx`/`_special_ufuncs`/`_gufuncs`/`_ellip_harm_2`），因为它们各持一份动作表副本。
- 两个 Python 类 `SpecialFunctionWarning`（不中断）与 `SpecialFunctionError`（中断）定义在 [`_sf_error.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_sf_error.py)，由 C 层 [`sf_error_v`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/sf_error.cc#L51-L123) 隔着 GIL 用 `getattr` 按名取回并触发。
- 实践上**优先用 `errstate`** 而非裸 `seterr`，以避免全局状态污染；`with errstate(all='raise', 某类='ignore')` 是「除某类外全部抛异常」的惯用写法。

## 7. 下一步学习建议

- **横向巩固**：回头对比 NumPy 的 `numpy.seterr/geterr/errstate`（管的是**浮点**错误 FE_INVALID/FE_DIVBYZERO…），special 的三件套在 API 形状上刻意模仿它，但管的「错误类别」完全不同。本讲的 `errstate` 就常与 numpy 的配套使用。
- **向下一层（重点推荐）→ u7「错误处理：C 层 sf_error 的贯通」**：本讲把 C 层当作黑盒——「C 内核检测到错误就调 `sf_error`」。u7 会打开这个黑盒：`SCIPY_TLS` 线程局部存储如何工作、`sf_error_v` 如何在「不持有 GIL 的数值循环」里安全跨界告警、`sf_error_check_fpe` 与 `_ufuncs_extra_code_common.pxi` 如何把**硬件浮点异常**翻译成 `sf_error` 信号、`.pxi` 片段又是如何被代码生成器拼进 `_ufuncs.pyx` 的。
- **顺着管线**：三件套所在的 `_ufuncs_extra_code.pxi` 是「代码生成管线」的产物之一，理解它如何被拼进 `_ufuncs.pyx`，请进 u3（代码生成管线）。
