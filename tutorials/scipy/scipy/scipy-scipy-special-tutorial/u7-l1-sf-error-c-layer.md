# sf_error.h/.cc/.pxd:错误码、TLS 动作与 C→Python 桥

> 本讲是 U7（错误处理:C 层 sf_error 的贯通）的第一讲。在 u2-l3 里你已经从**用户视角**用过 `seterr/geterr/errstate`,知道默认 9 类错误静默返 `NaN`、唯独 `memory` 默认抛异常。本讲下钻到 **C/C++ 内核层**,看清这套机制在源码里到底是怎么实现的——错误是怎么被分类的、默认动作存在哪里、C 代码又是如何「隔着 GIL」触发 Python 的告警与异常的。

---

## 1. 本讲目标

学完本讲你应该能够:

1. 说清 `sf_error_t`（错误**类型**）与 `sf_action_t`（错误**动作**）这两套枚举的区别,以及它们分别定义在哪个文件。
2. 解释 `sf_error_actions[]` 这张线程局部（TLS）默认动作表的结构,并说明**为何只有 `SF_ERROR_MEMORY` 默认 `RAISE`**。
3. 逐步追踪 `sf_error_v` 如何在 C 内核里获取 GIL、`import scipy.special`、按动作发出 `SpecialFunctionWarning` 或抛 `SpecialFunctionError`。
4. 说明 `sf_error_check_fpe` 如何把 CPU 硬件浮点异常翻译成软件错误码。
5. 理解 `sf_error.pxd` 给 Cython 提供的 `nogil` 视图,以及 `xsf::set_error` 钩子如何让外部 C++ 库的错误汇入同一套管线。

---

## 2. 前置知识

本讲同时涉及 C、C++、Cython 和 Python C-API,先用通俗语言把几个关键概念讲透。

- **枚举(enum)**:C 里给一组整数常量起名字的工具。本讲有两套枚举,一套描述「出了什么错」(`sf_error_t`),一套描述「该怎么处理」(`sf_action_t`)。把这两件事拆开是整个错误系统的设计核心。
- **线程局部存储(Thread-Local Storage, TLS)**:用 `thread_local`/`__thread` 修饰的变量,每个线程各有一份独立副本。本讲的默认动作表是 TLS 的,因此 `seterr` 的配置不会在线程间串扰。
- **GIL(全局解释器锁)**:CPython 的核心约束——任何线程在调用 Python C-API(如发告警、设异常、操作 Python 对象)之前,必须先「持有 GIL」。而 `scipy.special` 的 ufunc 内层循环和 `cython_special` 的 `nogil` 代码恰恰是**主动放开 GIL** 跑的,所以内核一旦发现错误,要触发 Python 告警就必须先把 GIL「借回来」。这个「借 GIL」的动作就是 `PyGILState_Ensure/Release`。
- **可变参数函数(va_list)**:`sf_error(const char *func_name, sf_error_t code, const char *fmt, ...)` 末尾的 `...` 让调用方像 `printf` 那样带任意格式参数。内部用 `va_list` 接收,`sf_error_v` 是真正干活的版本。
- **引用计数**:Python 对象用引用计数管理生命周期。拿到一个对象(如 `PyObject_GetAttrString` 返回的类对象)后用完要 `Py_DECREF`,否则内存泄漏。本讲的 GIL 桥代码会反复出现这个模式。
- **NumPy ufunc 的浮点异常**:CPU 浮点单元在运算中可能置位「除零/溢出/下溢/非法」等硬件标志。NumPy 提供 `PyUFunc_getfperr()` 读取这些标志,本讲的 `sf_error_check_fpe` 就是把它翻译成软件错误码。

承接认知:本讲假设你已读过 u2-l3（用户层 `errstate` 三件套）、u3-l3（7 个扩展模块与 `sf_error.cc` 被编译进多个模块的事实）。

---

## 3. 本讲源码地图

本讲精读 4 个核心源文件,外加 3 个佐证文件。

| 文件 | 语言 | 角色 |
|---|---|---|
| `sf_error.h` | C 头 | 声明 `sf_action_t` 枚举与 5 个函数原型;`#include <xsf/error.h>` 引入 `sf_error_t` |
| `sf_error.cc` | C++ 实现 | TLS 动作表、消息表、GIL 桥 `sf_error_v`、FPE 检查、`xsf::set_error` 钩子 |
| `sf_error.pxd` | Cython 声明 | 给 `.pyx` 用的 `nogil` 视图,重新导出两套枚举与 4 个函数 |
| `_sf_error.py` | Python | 定义 `SpecialFunctionWarning`/`SpecialFunctionError` 两个类 |

佐证文件(本讲会引用但不在主线上精读):

| 文件 | 作用 |
|---|---|
| `xsf/error.h`(外部 **xsf** 包,本工作树未 vendored) | `sf_error_t` 枚举的**权威定义**所在地;`sf_error.h` 只是 `#include` 它 |
| `scipy_config.h.in` | `SCIPY_TLS` 宏的定义(按编译器能力展开成 `thread_local` 等) |
| `_ufuncs_extra_code_common.pxi` | `wrap_PyUFunc_getfperr` 的定义,供 `sf_error_check_fpe` 调用 |

> ⚠️ 一个容易踩坑的事实:`sf_error_t`(「出了什么错」)的**枚举本体并不在** `scipy/special/` 目录内,而在外部的 `xsf/error.h`。但它在树内有**三面镜子**互相印证——`sf_error.cc` 的 TLS 数组注释、`sf_error.cc` 的消息表、`sf_error.pxd` 的枚举声明,三者完全一致。本讲据此还原它的全貌,不依赖外部文件。

---

## 4. 核心概念与源码讲解

### 4.1 两套枚举:错误类型 sf_error_t 与动作 sf_action_t

#### 4.1.1 概念说明

错误处理要回答两个独立的问题:

1. **出了什么错?** —— 由 `sf_error_t` 回答（领域错误? 溢出? 内存不足? ……）。
2. **该怎么处理?** —— 由 `sf_action_t` 回答（忽略? 告警? 抛异常?）。

把这两件事**解耦**是整个设计的精髓:同一类错误（比如 `DOMAIN`）在不同配置下可以走 `IGNORE`（返 `NaN`）、`WARN`（发 `SpecialFunctionWarning`）或 `RAISE`（抛 `SpecialFunctionError`）三条完全不同的路径,而内核代码只需调一句 `sf_error(...)`。这正是 u2-l3 里 `errstate(domain='raise')` 能临时改变行为的底层原因。

#### 4.1.2 核心流程

`sf_action_t` 只有三档,定义在 `sf_error.h` 自身:

```
SF_ERROR_IGNORE = 0   # 静默,什么都不做(默认)
SF_ERROR_WARN   = 1   # 发一个 Python 告警
SF_ERROR_RAISE  = 2   # 抛一个 Python 异常
```

`sf_error_t` 共 12 个值(0–11),权威定义在外部 `xsf/error.h`。综合三面镜子还原如下:

| 值 | 名字 | 消息字符串(`sf_error_messages`) | 含义 |
|---|---|---|---|
| 0 | `OK` | "no error" | 没出错 |
| 1 | `SINGULAR` | "singularity" | 奇点(如除零) |
| 2 | `UNDERFLOW` | "underflow" | 下溢 |
| 3 | `OVERFLOW` | "overflow" | 溢出 |
| 4 | `SLOW` | "too slow convergence" | 收敛太慢 |
| 5 | `LOSS` | "loss of precision" | 精度损失 |
| 6 | `NO_RESULT` | "no result obtained" | 没得到结果 |
| 7 | `DOMAIN` | "domain error" | 定义域错误 |
| 8 | `ARG` | "invalid input argument" | 非法参数 |
| 9 | `OTHER` | "other error" | 其他 |
| 10 | `MEMORY` | "memory allocation failed" | 内存分配失败 |
| 11 | `_LAST` | `NULL` | 哨兵:有效错误码的个数,本身不是错误 |

#### 4.1.3 源码精读

`sf_action_t` 三档枚举,以及 5 个函数原型,都声明在 `sf_error.h`:

- [scipy/special/sf_error.h:9-13](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/sf_error.h#L9-L13) —— 定义 `sf_action_t`（IGNORE/WARN/RAISE）。注意 `#include <xsf/error.h>` 在第 3 行,`sf_error_t` 就是从那里来的。

`sf_error_t` 的消息表在 `sf_error.cc`,顺序与上表完全对应,末尾以 `NULL` 收尾:

- [scipy/special/sf_error.cc:26-39](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/sf_error.cc#L26-L39) —— `sf_error_messages[]` 字符串表,索引即错误码。

Cython 侧的镜子在 `sf_error.pxd`,用 `OK "SF_ERROR_OK"` 这种「Cython 名 → C 名」的写法重新声明同一套枚举:

- [scipy/special/sf_error.pxd:4-16](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/sf_error.pxd#L4-L16) —— `sf_error_t` 的 Cython 视图（12 个名字与上表一致）。
- [scipy/special/sf_error.pxd:18-21](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/sf_error.pxd#L18-L21) —— `sf_action_t` 的 Cython 视图。

#### 4.1.4 代码实践（源码阅读型）

**目标**:确认三面镜子确实一致。

1. 打开 `sf_error.cc`,数 `sf_error_actions[]`（11–24 行）和 `sf_error_messages[]`（26–39 行）的条目数,确认都是 12 条（含末尾 `_LAST`/`NULL`）。
2. 打开 `sf_error.pxd` 的 4–16 行,数 `sf_error_t` 的成员数,确认也是 12 个。
3. 把三者的第 8 个条目（索引 7）对齐:数组注释是 `/* SF_ERROR_DOMAIN */`、消息是 `"domain error"`、`.pxd` 是 `DOMAIN "SF_ERROR_DOMAIN"`——三者描述同一个错误码。

**预期结果**:三个来源的顺序与命名完全吻合。这正是「权威定义在外部 `xsf/error.h`,树内三处镜像同步」的证据。

#### 4.1.5 小练习与答案

**练习 1**:`sf_error_t` 里 `_LAST`（索引 11）的用途是什么? 为什么它本身不算一个错误?

> **答案**:`_LAST` 是「哨兵」,值等于有效错误码的个数,用于上界检查（见 4.3 的 `code >= SF_ERROR__LAST` 判定）。它不对应任何真实错误,消息表里也是 `NULL`。

**练习 2**:如果内核代码写 `sf_error("foo", (sf_error_t)99, NULL)`,会发生什么?

> **答案**:`sf_error_v` 会发现 `99 >= SF_ERROR__LAST`,在第 61–63 行把它归约为 `SF_ERROR_OTHER`（索引 9）,消息变成 "other error"。

---

### 4.2 TLS 默认动作表:为何只有 MEMORY 默认 RAISE

#### 4.2.1 概念说明

光有两套枚举还不够,还需要一张「**每种错误当前用哪个动作**」的查找表。这张表就是 `sf_error_actions[]`,它有三个关键修饰:

1. **`static`**——文件内链接,意味着每个编译单元（每个扩展模块）各有一份独立副本。
2. **`SCIPY_TLS`**——线程局部,每个线程一份,配置不跨线程泄漏。
3. **`volatile`**——阻止 clang 等编译器把读取优化进寄存器(注释明确警告「If this isn't volatile clang tries to optimize it away」)。

这张表的**默认值**就是本讲的中心谜题:**只有 `MEMORY`（索引 10）默认 `RAISE`,其余全部 `IGNORE`**。

#### 4.2.2 核心流程

`SCIPY_TLS` 的展开由编译期能力决定,在 `scipy_config.h.in` 里:

```
C++         → thread_local
有 thread_local → thread_local
有 __thread   → __thread
MSVC         → __declspec(thread)
都没有        → 空(退化为普通全局,非线程安全)
```

默认动作表的查找与修改极其简单——纯数组下标读写:

```
action = sf_error_actions[code]   # 查(get_action)
sf_error_actions[code] = action   # 改(set_action)
```

#### 4.2.3 源码精读

TLS 默认动作表本体——注意第 22 行的 `SF_ERROR_RAISE` 是全表唯一一处:

- [scipy/special/sf_error.cc:10-24](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/sf_error.cc#L10-L24) —— `static volatile SCIPY_TLS sf_action_t sf_error_actions[]`,11 类错误全 `IGNORE`,唯独 `SF_ERROR_MEMORY` 是 `SF_ERROR_RAISE`,末尾 `_LAST` 也是 `IGNORE`。
- [scipy/special/scipy_config.h.in:1-19](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/scipy_config.h.in#L1-L19) —— `SCIPY_TLS` 宏按编译器能力展开;C++ 下（第 7–8 行）直接用 `thread_local`。

读写函数只是对下标操作:

- [scipy/special/sf_error.cc:43-49](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/sf_error.cc#L43-L49) —— `sf_error_set_action` 写、`sf_error_get_action` 读。

#### 4.2.4 代码实践（源码阅读型 + 可选运行）

**目标**:弄清「为何只有 MEMORY 默认 RAISE」与「多副本」两个事实。

**操作步骤**:

1. 在 `meson.build` 里搜索 `sf_error.cc`,数它被编译进几个扩展模块。预期命中:`_ufuncs`、`_ufuncs_cxx`、`_special_ufuncs`、`_gufuncs`、`_ellip_harm_2`、`cython_special`（见 [meson.build:17](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L17)、[23](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L23)、[35](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L35)、[45](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L45)、[147](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L147)、[162](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L162)）——共 **6 个**,只有 `_specfun` 没有。
2. （可选,需已编译 SciPy）运行:

```python
import scipy.special as sc
print(sc.geterr())          # 预期:除 memory 外全 'ignore',memory 为 'raise'
```

**需要观察的现象 / 预期结果**:

- 默认配置里 `memory` 唯一为 `'raise'`,其余 `'ignore'`——这正是 TLS 表的默认值。
- 因为 `sf_error.cc` 被 `static` 编译进 6 个模块,**每个模块各有一份独立的 `sf_error_actions[]`**。这意味着在 A 模块里改动作,B 模块不受影响。这就是为什么 u2-l3 的 `seterr/errstate` 在 Python 层必须**遍历所有扩展模块**同步状态（详见 u7-l2 的 `_set_action` 实现）。

> **为何只有 MEMORY 默认 RAISE**:数值类错误（domain/singular/overflow/underflow/loss）在科学计算里是常态——比如对负数取对数、Gamma 函数在极点附近,返回 `NaN`/`inf` 让调用方自己决定如何处理,是 `special` 的默认哲学（u2-l3 已建立）。但**内存分配失败**意味着内核无法继续,返回任何「占位值」都没有意义,所以默认就把它设为唯一致命错误。这是一个有意识的工程取舍:对常见数值错误宽容,对不可恢复错误严格。

#### 4.2.5 小练习与答案

**练习 1**:`sf_error_actions[]` 同时带 `static`、`SCIPY_TLS`、`volatile` 三个修饰,各自解决什么问题?

> **答案**:`static` 让符号文件内可见,使每个扩展模块各持一份副本;`SCIPY_TLS` 让每个线程各持一份,配置不跨线程泄漏;`volatile` 阻止编译器把运行时被 `set_action` 修改的读取优化掉（否则 clang 会缓存到寄存器）。

**练习 2**:如果 `sf_error.cc` 只被编译进 1 个扩展模块,会带来什么简化?

> **答案**:Python 层就无需遍历多模块同步状态——`seterr` 改一处即全局生效。现实中它被编进 6 个模块,所以必须同步,代价是 u7-l2 要写的跨模块广播逻辑。

---

### 4.3 GIL 桥:sf_error_v 如何从 C 触发 Python 告警与异常

#### 4.3.1 概念说明

`sf_error_v` 是整个错误系统的**咽喉**。所有内核函数（无论 Cephes、xsf 还是 Boost）发现错误后,最终都汇聚到它。它要解决的核心难题是:**当前线程很可能没有持有 GIL**（ufunc 内层循环、`cython_special` 的 `nogil` 段都是主动放开 GIL 跑的）,而要发 Python 告警/异常又必须持有 GIL。

因此 `sf_error_v` 的职责可以概括为三步:**(1) 查动作决定要不要上报;(2) 把 GIL 借回来;(3) 隔着 GIL 调 Python C-API 发出告警或异常。**

#### 4.3.2 核心流程

`sf_error_v(func_name, code, fmt, ap)` 的执行流程（行号对应 `sf_error.cc`）:

```
1. 边界检查:code < 0 或 code >= _LAST → 归为 OTHER            (61-63)
2. action = sf_error_get_action(code)                          (64)
3. 若 action == IGNORE → 直接 return(默认零开销路径)           (65-67)
4. func_name == NULL → 兜底为 "?"                              (69-71)
5. 用 PyOS_vsnprintf 拼消息 "scipy.special/<func>: (<msg>) <info>"  (73-78)
6. save = PyGILState_Ensure()          ← 【借 GIL】             (80)
7. 若 PyErr_Occurred() → 跳 skip_warn(已有异常,不叠加)         (82-84)
8. scipy_special = PyImport_ImportModule("scipy.special")      (86)
   若失败 → PyErr_Clear,跳 skip_warn                           (87-90)
9. 按 action 取类:
     WARN  → PyObject_GetAttrString(..., "SpecialFunctionWarning")  (92-93)
     RAISE → PyObject_GetAttrString(..., "SpecialFunctionError")    (94-95)
10. Py_DECREF(scipy_special)                                    (101)
11. 取类失败 → PyErr_Clear,跳 skip_warn                        (103-106)
12. 按 action 发信号:
     WARN  → PyErr_WarnEx(class, msg, 1)  ← 设挂起告警          (108-109)
     RAISE → PyErr_SetString(class, msg)  ← 设异常              (114-115)
13. Py_DECREF(class)                                            (119)
14. skip_warn: PyGILState_Release(save)  ← 【还 GIL】           (121-122)
```

外层 `sf_error` 只是把可变参数收成 `va_list` 再转发:

- [scipy/special/sf_error.cc:125-130](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/sf_error.cc#L125-L130) —— `sf_error` 是个薄包装,`va_start`/`va_end` 之间调用 `sf_error_v`。

#### 4.3.3 源码精读

`sf_error_v` 全文——本讲最重要的一段:

- [scipy/special/sf_error.cc:51-123](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/sf_error.cc#L51-L123) —— GIL 桥主体。重点行:
  - 第 64–67 行:`IGNORE` 早退——这是默认路径,数值错误在默认配置下几乎零开销。
  - 第 80 行:`PyGILState_Ensure()`——**跨 GIL 边界的关键一步**。
  - 第 86 行:`PyImport_ImportModule("scipy.special")`——运行时按名导入模块。
  - 第 93、95 行:`PyObject_GetAttrString` 分别取 `SpecialFunctionWarning`/`SpecialFunctionError`。
  - 第 109 行:`PyErr_WarnEx` 设置**挂起**告警;第 115 行 `PyErr_SetString` 设置异常。
  - 第 121–122 行:`skip_warn` 标签 + `PyGILState_Release`——用 `goto` 保证无论哪条分支退出,GIL 都被释放。

`PyObject_GetAttrString` 取到的两个 Python 类,真正定义在 `_sf_error.py`:

- [scipy/special/_sf_error.py:5-15](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_sf_error.py#L5-L15) —— `SpecialFunctionWarning(Warning)` 与 `SpecialFunctionError(Exception)`;第 10 行 `warnings.simplefilter("always", ...)` 确保告警每次都显示（不被「同位置只警告一次」机制吞掉）。
- 这两个类再被 [scipy/special/__init__.py:786](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/__init__.py#L786) 的 `from ._sf_error import ...` 提到 `scipy.special` 命名空间,所以 C 层 `PyImport_ImportModule("scipy.special")` 后能 `getattr` 到它们。

四个值得深究的设计点:

- **为什么不在扩展模块初始化时缓存类对象,而是每次 `import + getattr`?** 因为这能避开**循环导入/初始化顺序**问题——C 扩展加载时 `scipy.special` 可能尚未完全初始化。代价是每次报错多一次 import+getattr,但错误本就罕见,可接受。
- **`goto skip_warn` 的意义**:C 里用 `goto` 跳到统一的「释放 GIL」出口,保证 `PyGILState_Release(save)` 在任何成功/失败分支下都执行——这是 C 代码做资源清理的经典写法。
- **`PyErr_Occurred` 守卫(82–84 行)**:如果线程里已有挂起异常,就不再叠加新告警,避免错误雪崩。
- **WARN 与 RAISE 的不对称(108–118 行)**:`PyErr_WarnEx` 只设「挂起告警」;对 ufunc 而言内层循环的返回值会被忽略,告警真正送达要靠 ufunc 机制随后调 `PyErr_Occurred()`（见 109–113 行的注释）。而 `PyErr_SetString` 设的是异常,控制权一回到 Python 就抛出。

#### 4.3.4 代码实践（源码阅读型）

**目标**:追踪「借 GIL → 取类 → 发信号 → 还 GIL」的完整链路。

**操作步骤**:

1. 在 `sf_error.cc` 里定位 `sf_error_v`（第 51 行）,找到第 80 行 `PyGILState_Ensure()` 与第 122 行 `PyGILState_Release(save)`——确认它们成对出现,且所有早退分支（84、89、106、117）都走 `goto skip_warn` 落到 122 行。
2. 顺着第 86 行 `PyImport_ImportModule("scipy.special")` → 第 93/95 行 `PyObject_GetAttrString` → 打开 `_sf_error.py` 第 5、13 行确认两个类的定义,再打开 `__init__.py` 第 786 行确认它们已被提到命名空间——这条链解释了「C 代码怎么按名字拿到 Python 类」。
3. 阅读第 108–118 行,对比 `PyErr_WarnEx`（WARN）与 `PyErr_SetString`（RAISE）,并读 110–113 行的注释,理解为何 ufunc 下的告警依赖后续 `PyErr_Occurred()`。

**预期结果**:你会看到一张清晰的「C 内核 → sf_error_v → GIL → import scipy.special → getattr 类 → PyErr_* → 还 GIL」的桥接图。

#### 4.3.5 小练习与答案

**练习 1**:第 65–67 行的 `if (action == SF_ERROR_IGNORE) return;` 为什么对性能至关重要?

> **答案**:它是默认路径——绝大多数数值错误在默认配置下动作就是 `IGNORE`,这里直接返回,既不借 GIL 也不碰 Python 对象,开销接近一次数组读取。如果每次错误都走完整的 GIL 桥,ufunc 批量计算会被严重拖慢。

**练习 2**:如果 `PyImport_ImportModule("scipy.special")` 返回 `NULL`（极罕见的导入失败场景）,代码会怎样?

> **答案**:第 88 行 `PyErr_Clear()` 清掉导入异常,然后 `goto skip_warn` 直接释放 GIL 返回——既不上报也不崩溃,等价于把这次错误降级为静默。

**练习 3**:为什么用 `goto skip_warn` 而不是直接在每条分支里写 `PyGILState_Release`?

> **答案**:统一出口保证「借了 GIL 就一定还」,避免在多条 if/else 分支里漏写释放（漏写会导致死锁或 GIL 状态损坏）。这是 C 语言管理「获取/释放」配对资源的惯用法。

---

### 4.4 sf_error_check_fpe:把硬件浮点异常翻译成错误码

#### 4.4.1 概念说明

除了内核**主动**调 `sf_error(...)` 报告已知错误,CPU 浮点单元还会在运算中自动置位**硬件异常标志**（除零、溢出、下溢、非法值）。`sf_error_check_fpe` 的作用是:在一次计算后读取这些硬件标志,把它们**翻译**成软件错误码,再交给 `sf_error` 走 4.3 的同一套管线。

这样,即便内核代码没显式检查某个边界,硬件抓到的异常也能统一汇入 `special` 的错误体系。

#### 4.4.2 核心流程

```
status = wrap_PyUFunc_getfperr()        # 读 NumPy 维护的硬件 FPE 标志
若 status & NPY_FPE_DIVIDEBYZERO → sf_error(.., SINGULAR, "floating point division by zero")
若 status & NPY_FPE_OVERFLOW     → sf_error(.., OVERFLOW, ...)
若 status & NPY_FPE_UNDERFLOW    → sf_error(.., UNDERFLOW, ...)
若 status & NPY_FPE_INVALID      → sf_error(.., DOMAIN,   "floating point invalid value")
```

注意:**`NPY_FPE_INEXACT`（不精确）被刻意跳过**——它几乎在每次浮点运算都会触发,检查它毫无意义。

#### 4.4.3 源码精读

- [scipy/special/sf_error.cc:132-146](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/sf_error.cc#L132-L146) —— `sf_error_check_fpe`,把 4 个硬件标志映射成 `SINGULAR/OVERFLOW/UNDERFLOW/DOMAIN`。
- [scipy/special/sf_error.cc:41](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/sf_error.cc#L41) —— `extern "C" int wrap_PyUFunc_getfperr(void);` 声明,实现不在本文件。
- [scipy/special/_ufuncs_extra_code_common.pxi:13-20](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_ufuncs_extra_code_common.pxi#L13-L20) —— `wrap_PyUFunc_getfperr` 的真正定义:它只是把 NumPy 的 `PyUFunc_getfperr()`（来自 `numpy/ufuncobject.h`,第 13 行声明）包一层。注释点明原因——「避免 messing with the UNIQUE_SYMBOL #defines」,即在已初始化好 `PyUFunc_API` 表的上下文里调用,绕开符号宏冲突。

#### 4.4.4 代码实践（源码阅读型）

**目标**:理解「硬件异常 → 软件错误码 → sf_error 管线」的三段式。

**操作步骤**:

1. 在 `sf_error.cc` 第 132–146 行确认 `sf_error_check_fpe` 检查的 4 个标志,以及对应的错误码与提示语。
2. 跟到 `_ufuncs_extra_code_common.pxi` 第 15–20 行,确认 `wrap_PyUFunc_getfperr` 只是把 NumPy 的 `PyUFunc_getfperr` 转发——这就是 C 层能读到硬件 FPE 标志的根因。
3. 回忆 4.3:`sf_error_check_fpe` 里每个 `sf_error(...)` 调用最终都会进 `sf_error_v`,按 TLS 动作决定是静默/告警/抛异常。**整条链是闭合的**。

**预期结果**:你会看到 `sf_error_check_fpe` 是「硬件世界」与「sf_error 软件管线」之间的适配器。

#### 4.4.5 小练习与答案

**练习 1**:为什么 `sf_error_check_fpe` 不检查 `NPY_FPE_INEXACT`?

> **答案**:不精确异常几乎在每次浮点运算（如 `0.1 + 0.2`）都会触发,检查它会污染每一次计算,毫无诊断价值,所以刻意跳过。

**练习 2**:`sf_error_check_fpe` 把 `NPY_FPE_INVALID` 映射成哪个软件错误码? 为什么选它?

> **答案**:映射成 `DOMAIN`（领域错误）。「非法值」（如 `0/0`、负数开偶次方）的本质是「输入落在函数定义域外」,与显式报 `DOMAIN` 语义一致。

---

### 4.5 全模块衔接:.pxd 的 nogil 视图与 xsf::set_error 钩子

#### 4.5.1 概念说明

最后厘清两个「衔接点」:一是 `sf_error.pxd` 如何让 Cython 代码在 `nogil` 段里调用这些 C 函数;二是外部 C++ 库 **xsf** 如何通过一个编译期开关,把自己的错误汇入同一套 `sf_error_v` 管线。理解这两点,才能看清「所有后端（Cephes/xsf/Boost）的错误为什么最终都走同一条路」。

#### 4.5.2 核心流程

**衔接点 A:Cython 的 `nogil` 视图**

`sf_error.pxd` 把 C 函数声明成 `nogil`,这样 `_ufuncs.pyx`、`cython_special.pyx` 等生成的 Cython 代码可以在 `with nogil:` 块里直接调它们（因为 `sf_error`/`sf_error_check_fpe`/`set_action`/`get_action` 本身就不碰 Python 对象,符合 `nogil` 契约;真正碰 Python 对象的工作被推迟到 `sf_error_v` 内部的 `PyGILState_Ensure`）。

**衔接点 B:`xsf::set_error` 钩子（受 `SP_SPECFUN_ERROR` 宏控制）**

外部 xsf C++ 库有自己的错误上报接口 `xsf::set_error`。`sf_error.cc` 在 `#ifdef SP_SPECFUN_ERROR` 保护下**提供**了这个函数的定义,让它转发到 `sf_error_v`。于是凡是用 `-DSP_SPECFUN_ERROR` 编译的扩展模块,其 xsf 内核报错就会自动汇入 SciPy 的 Python 告警/异常体系。

#### 4.5.3 源码精读

- [scipy/special/sf_error.pxd:23-27](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/sf_error.pxd#L23-L27) —— 4 个函数都标 `nogil`,Cython 侧可在无 GIL 段调用。
- [scipy/special/sf_error.pxd:30-42](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/sf_error.pxd#L30-L42) —— `_sf_error_test_function`:一个内联测试工具,按整数 `code` 触发任意一类 `sf_error`,供测试体系（u9 的 `with_special_errors`）验证每种错误的告警/异常行为。
- [scipy/special/sf_error.cc:148-165](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/sf_error.cc#L148-L165) —— `#ifdef SP_SPECFUN_ERROR` 块内定义 `xsf::set_error`,转发到 `sf_error_v`。注释说明「其他用到 xsf 库的包可以提供自己的实现」——这是 xsf 作为可复用库留给宿主的钩子。
- [scipy/special/meson.build:32](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L32) —— `ufuncs_cpp_args = ['-DSP_SPECFUN_ERROR']`,把这个宏作为默认 C++ 编译参数（在 [98](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L98)、[131](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L131)、[149](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L149)、[166](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L166) 行被各 C++ 扩展模块采用）。

#### 4.5.4 代码实践（源码阅读型）

**目标**:确认「Cython 的 nogil 调用」与「xsf 钩子」两条衔接路径都成立。

**操作步骤**:

1. 在 `sf_error.pxd` 第 24–27 行确认 4 个函数声明都带 `nogil`;再回忆 4.3 里 `sf_error_v` 内部用 `PyGILState_Ensure` 借 GIL——两者并不矛盾:函数本身 `nogil`（调用方无需持 GIL 即可调）,内部按需借 GIL。
2. 在 `sf_error.cc` 第 148 行找到 `#ifdef SP_SPECFUN_ERROR`,读 150–163 行确认 `xsf::set_error` 只是 `va_list` 版的 `sf_error_v` 转发。
3. 在 `meson.build` 第 32 行确认 `-DSP_SPECFUN_ERROR` 被设为默认 C++ 参数,因此所有 C++ 扩展模块（`_special_ufuncs`/`_gufuncs`/`_ufuncs_cxx`/`cython_special`/`_ellip_harm_2`）里的 xsf 内核报错都会汇入本管线。

**预期结果**:你会得到一张「所有后端 → sf_error / xsf::set_error / sf_error_check_fpe → sf_error_v → Python 告警/异常」的统一收敛图。

#### 4.5.5 小练习与答案

**练习 1**:函数声明成 `nogil`,但 `sf_error_v` 里又调了 `PyGILState_Ensure`,这矛盾吗?

> **答案**:不矛盾。`nogil` 是对**调用方**的承诺——「你不必持有 GIL 就能调我」。函数体内可以按需自行借 GIL（`sf_error_v` 正是只在 `IGNORE` 早退失败后才借）。这种「外层 nogil、内部按需借 GIL」正是 ufunc/cython_special 热路径与 Python 错误上报能够共存的模式。

**练习 2**:如果不给扩展模块传 `-DSP_SPECFUN_ERROR`,`xsf::set_error` 会怎样?

> **答案**:`sf_error.cc` 第 148–165 行的 `#ifdef` 块不会被编译,`xsf::set_error` 由 xsf 库自身的默认实现提供（通常只打印或忽略）,不再汇入 SciPy 的 Python 告警/异常体系。所以这个宏是把 xsf「焊」到 SciPy 错误系统上的开关。

---

## 5. 综合实践

**任务**:用一个可运行的小实验,把本讲的「TLS 动作表 + GIL 桥」从 C 层贯穿到 Python 用户层,亲眼看到默认 `IGNORE` 返 `NaN`、`WARN` 发告警、`RAISE` 抛异常三种动作的切换。

> 这正好接上 u2-l3 的用户层练习,但现在你要能**用本讲的源码逻辑预测每种现象**。

**操作步骤**（需已安装可运行的 SciPy）:

```python
import warnings
import scipy.special as sc

# (1) 默认配置:DOMAIN 错误动作是 IGNORE
print("默认 geterr():", sc.geterr())
# 预期:除 memory='raise' 外全 'ignore'

# (2) 触发一个 DOMAIN 错误:spence 在 x<0 时定义域外
#     内核调 sf_error("spence", DOMAIN, ...) → 动作 IGNORE → sf_error_v 早退 → 返回 NaN
r = sc.spence(-1.0)
print("spence(-1) 默认返回:", r)   # 预期:nan,且无任何告警

# (3) 切到 WARN:TLS 表 DOMAIN 改为 WARN → sf_error_v 借 GIL 发 SpecialFunctionWarning
with sc.errstate(domain="warn"):
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        r = sc.spence(-1.0)
        print("WARN 下捕获到告警:", any(issubclass(x.category, sc.SpecialFunctionWarning) for x in w))

# (4) 切到 RAISE:动作改为 RAISE → sf_error_v 设异常 → 抛 SpecialFunctionError
try:
    with sc.errstate(domain="raise"):
        sc.spence(-1.0)
except sc.SpecialFunctionError as e:
    print("RAISE 下抛出:", type(e).__name__, "→", e)

# (5) 退出 errstate 后应自动恢复 IGNORE
print("退出后 geterr():", sc.geterr())
```

**需要观察的现象 / 预期结果**:

| 步骤 | 现象 | 对应源码逻辑 |
|---|---|---|
| (1) | `domain='ignore'` | TLS 表默认值（`sf_error.cc:18` 的 DOMAIN 行） |
| (2) | 返回 `nan`,无告警 | `sf_error_v` 在 [sf_error.cc:65-67](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/sf_error.cc#L65-L67) 早退 |
| (3) | 捕获到 `SpecialFunctionWarning` | [sf_error.cc:109](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/sf_error.cc#L109) 的 `PyErr_WarnEx` |
| (4) | 抛 `SpecialFunctionError` | [sf_error.cc:115](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/sf_error.cc#L115) 的 `PyErr_SetString` |
| (5) | 恢复 `domain='ignore'` | `errstate` 退出时复原 TLS 表（u7-l2 详解） |

> 说明:具体数值（`nan`）与告警文本以本地运行为准;若你的环境因编译选项导致行为偏差,标「待本地验证」。三条动作分支的**切换**本身是源码确定性逻辑,可放心预测。

---

## 6. 本讲小结

- **两套解耦的枚举**:`sf_error_t`（出了什么错,12 个码,权威定义在外部 `xsf/error.h`,树内三面镜子互证）与 `sf_action_t`（怎么处理,IGNORE/WARN/RAISE,定义在 `sf_error.h`）。内核只调一句 `sf_error(...)`,动作由配置决定。
- **TLS 默认动作表**:`static volatile SCIPY_TLS sf_action_t sf_error_actions[]`,默认 11 类全 `IGNORE`、唯独 `MEMORY` 是 `RAISE`——因为数值错误可返 `NaN` 容错,而内存失败不可恢复。`SCIPY_TLS` 保线程隔离,`volatile` 防优化,`static` 使每个扩展模块各持一份副本（共 6 份）。
- **GIL 桥 `sf_error_v`**:查动作 → `IGNORE` 早退（默认零开销）→ 否则 `PyGILState_Ensure` 借 GIL → `import scipy.special` → `PyObject_GetAttrString` 取 `SpecialFunctionWarning/Error` → `PyErr_WarnEx`/`PyErr_SetString` → `goto skip_warn` 统一释放 GIL。
- **硬件异常翻译**:`sf_error_check_fpe` 经 `wrap_PyUFunc_getfperr` 读 NumPy 的 FPE 标志,把 DIVIDEBYZERO/OVERFLOW/UNDERFLOW/INVALID 映射成 SINGULAR/OVERFLOW/UNDERFLOW/DOMAIN,刻意跳过 INEXACT。
- **两处衔接**:`sf_error.pxd` 把 C 函数声明为 `nogil`,让 Cython 热路径可调用（内部按需借 GIL）;`#ifdef SP_SPECFUN_ERROR` 下的 `xsf::set_error` 钩子,让外部 xsf C++ 库的错误汇入同一管线,实现「所有后端、同一种错误语言」。

---

## 7. 下一步学习建议

本讲讲清了 **C 层**的错误机制「是什么、默认值、如何跨 GIL」。下一讲 **u7-l2 `_ufuncs_extra_code.pxi:seterr/geterr/errstate 与 FPE 检测`** 将补齐**用户层与生成层**:

- 看 `_ufuncs_extra_code_common.pxi` 里的 `wrap_PyUFunc_getfperr` 如何被 ufunc 内层循环调用（本讲只看了它的定义,没看调用点）。
- 看 `_ufuncs_extra_code.pxi` 里的 `seterr/geterr/errstate` 如何在**字符串名**（`'domain'`）与 `sf_action_t` 枚举之间互转,并解决本讲埋下的伏笔——**遍历 6 个扩展模块同步各自的 TLS 副本**、`errstate` 如何用「进入时存旧值、退出时恢复」实现上下文管理。
- 看 `.pxi` 片段如何被 `_generate_pyx.py` 拼接进生成的 `_ufuncs.pyx`。

读完 u7-l2,你就能从 Python 用户层一路讲清楚到 CPU 硬件标志,形成完整的错误处理闭环。之后可继续 U8（C/C++ 后端深入）,看 Boost.Math 的 `SpecialPolicy` 如何用另一套机制（自定义 `user_*_error` 策略）也把错误汇入 `sf_error_v`。
