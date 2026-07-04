# _ufuncs_extra_code.pxi:seterr/geterr/errstate 与 FPE 检测

> 本讲是 U7（错误处理:C 层 sf_error 的贯通）的第二讲，承接 u7-l1 讲清的 C 层错误机制（`sf_error_t` / `sf_action_t` 两套枚举、TLS 动作表、GIL 桥 `sf_error_v`），把视线从「C 内核如何报错」上移到「Python 用户如何配置错误，以及 ufunc 内层循环如何把硬件浮点异常接入这套机制」。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 ufunc 内层循环（一段 `noexcept nogil` 的 C 代码）在算完一批元素后，是如何把 CPU 硬件层面累积的浮点异常标志（FPE flags）「翻译」成 `sf_error` 软件信号的；
- 读懂 `_ufuncs_extra_code.pxi` 里 `geterr` / `seterr` / `errstate` 三件套的实现，掌握它们在「人类可读字符串」与「C 枚举整数」之间的双向映射，以及为何 `seterr` 要同时改写 5 个扩展模块的状态；
- 理解 `errstate` 上下文管理器「进入时快照、退出时恢复」的经典写法；
- 认识 `.pxi` 片段是如何被代码生成器 `_generate_pyx.py` 用 `include` 指令拼接到生成产物 `_ufuncs.pyx` 里的，从而让这些纯手写的 Python/Cython 代码住进编译产物。

## 2. 前置知识

本讲默认你已通过 u7-l1 建立以下认知（此处只做最简回顾）：

- **两套解耦的枚举**：`sf_error_t` 描述「出了什么错」（OK/SINGULAR/UNDERFLOW/OVERFLOW/SLOW/LOSS/NO_RESULT/DOMAIN/ARG/OTHER/MEMORY，共 12 个码）；`sf_action_t` 描述「怎么处理」（IGNORE/WARN/RAISE，共 3 个）。
- **TLS 动作表**：C 层有一个 `static volatile SCIPY_TLS sf_action_t sf_error_actions[]` 数组，11 类错误默认全 `IGNORE`、唯独 `MEMORY` 默认 `RAISE`。内核报错只调一句 `sf_error(...)`，具体走哪条路由由这张表决定。
- **GIL 桥**：`sf_error_v` 查动作 → 若 `IGNORE` 早退 → 否则 `PyGILState_Ensure` 借 GIL → `import scipy.special` 取 `SpecialFunctionWarning` / `SpecialFunctionError` → 发告警或抛异常。

如果你对这些细节不熟，请先读 u7-l1。本讲要回答的新问题是：

1. u7-l1 讲的 `sf_error(...)` 都是 C 内核**主动**调用（比如「这个参数越界了」）。可很多数值错误是 CPU 在算术运算中**硬件层面**产生的（除以零、溢出……），内核代码并不会逐条检查——这些硬件错误怎么进入 `sf_error` 管线？
2. 用户在 Python 里写 `special.seterr(singular='warn')`，这个字符串是怎么变成 C 层枚举、又怎么写进那张 TLS 表的？

此外你需要一点 NumPy ufunc 的背景（u2-l1）：ufunc 的逐元素计算由一段叫 **inner loop** 的 C 函数完成，签名形如 `void loop(char **args, npy_intp *dims, npy_intp *steps, void *data) noexcept nogil`。本讲大量涉及这段循环的生成细节。

> 名词速查：**FPE**（Floating-Point Exception，浮点异常），指 CPU 在执行浮点运算时硬件置位的错误标志，如除以零、上溢、下溢、非法操作（如 \(\sqrt{-1}\)、\(0/0\)）。注意它与 Python 的 `FloatingPointError` 不是一回事：NumPy 默认**关闭** FPE 硬件陷阱，即硬件标志会被静默置位而不中断程序，留待事后查询。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|------|------|-----------|
| [_ufuncs_extra_code_common.pxi](_ufuncs_extra_code_common.pxi) | 公共片段：被同时拼进 `_ufuncs.pyx` 与 `_ufuncs_cxx.pyx` | `wrap_PyUFunc_getfperr`（读硬件 FPE 标志的 nogil 包装）、`_set_action`（写 TLS 动作表的 nogil 包装） |
| [_ufuncs_extra_code.pxi](_ufuncs_extra_code.pxi) | 主体片段：只拼进 `_ufuncs.pyx` | 字符串↔枚举两张映射表、`geterr` / `seterr` / `errstate` 三件套 |
| [_generate_pyx.py](_generate_pyx.py) | 代码生成器 | `UFUNCS_EXTRA_CODE_COMMON` / `UFUNCS_EXTRA_CODE` / `UFUNCS_EXTRA_CODE_BOTTOM` 三个常量、`generate_loop` 在循环末尾插入 `sf_error.check_fpe`、`generate_ufuncs` 把片段按序写盘 |
| [sf_error.pxd](sf_error.pxd) | Cython 视图的 C 声明 | `error` / `check_fpe` / `set_action` / `get_action` 四个 `nogil` 函数签名 |
| [sf_error.cc](sf_error.cc) | C 层实现 | `sf_error_actions[]` TLS 表、`sf_error_check_fpe` 把 FPE 位翻译成 `sf_error` |
| [meson.build](meson.build) | 构建编排 | `fs.copyfile` 把两个 `.pxi` 静态源文件拷进构建目录，供 Cython `include` |

一个关键认知（u1-l2、u3-l2 已建立）：`_ufuncs.pyx` **不在源码树里**，而是构建时由 `_generate_pyx.py` 配合 `functions.json` 生成的。本讲两个 `.pxi` 文件则是**手写的静态源文件**，通过 Cython 的 `include` 指令被「文本粘贴」进生成的 `.pyx`。理解「谁生成、谁手写、怎么拼」是本讲第三模块的主线。

## 4. 核心概念与源码讲解

### 4.1 FPE 检测转换:把硬件浮点异常翻译成 sf_error

#### 4.1.1 概念说明

考虑一个具体场景：你调用 `special.spence(1e300)`，底层 Cephes 内核在做某个中间除法时发生了**硬件浮点上溢**。此时会发生什么？

- CPU 不会中断程序（NumPy 关闭了 FPE 陷阱）；
- CPU 只是在 FPU 状态寄存器里**把 OVERFLOW 这一位置 1**；
- 内核继续算，最终给你一个 `inf` 或一个数值不对的结果。

如果不做任何处理，用户根本无从知晓「这一批里有元素溢出了」。`scipy.special` 的做法是：在 ufunc 内层循环**算完整批元素之后**，统一查询一次 FPU 状态寄存器，把里面置位的标志翻译成对应的 `sf_error` 软件码，从而接入 u7-l1 讲的那套「按动作分流」的管线。

这里有一个**硬件层的核心特性**必须先理解：**FPE 标志是「粘性」（sticky）的**。一旦某次运算置位了 OVERFLOW，这个位会一直保持 1，直到软件显式清零。这意味着：

\[ \text{循环结束后读到的状态} = \bigvee_{i=1}^{n} \text{第 } i \text{ 个元素产生的 FPE} \]

即读出的是整批元素的「按位或」。所以检测只需在循环末尾做**一次**，而不必逐元素查询——这是性能与精度的权衡：你能知道「这批里发生过溢出」，但不知道具体是第几个元素。对「发个告警」这个目的来说足够了。

#### 4.1.2 核心流程

把硬件 FPE 接入 `sf_error` 的完整链路：

```
┌─────────────────────────────────────────────────────────────┐
│  生成的 ufunc inner loop（_generate_pyx.py 的 generate_loop） │
│                                                             │
│  for i in range(n):            ← 逐元素调用 C 内核          │
│      (<内核函数指针>)(输入..., &输出...)                       │
│      [若 DANGEROUS_DOWNCAST 失败: sf_error.error(DOMAIN)]    │
│                                                             │
│  sf_error.check_fpe(func_name) ← ★ 循环外，整批只调一次       │
└────────────────────────────┬────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│  sf_error_check_fpe（sf_error.cc）                           │
│                                                             │
│  status = wrap_PyUFunc_getfperr()   ← 读并清空 FPU 粘性标志  │
│  if status & NPY_FPE_DIVIDEBYZERO: sf_error(SINGULAR)       │
│  if status & NPY_FPE_OVERFLOW:     sf_error(OVERFLOW)       │
│  if status & NPY_FPE_UNDERFLOW:    sf_error(UNDERFLOW)      │
│  if status & NPY_FPE_INVALID:      sf_error(DOMAIN)         │
└────────────────────────────┬────────────────────────────────┘
                             │  （走 u7-l1 讲的 sf_error_v 管线）
                             ▼
              查 TLS 动作表 → IGNORE / WARN / RAISE
```

注意四个 FPE 位到 `sf_error` 码的映射是有语义考量的：硬件「除以零」对应数学上的「奇点」（SINGULAR，如 Gamma 在 0 处的极点），硬件「非法值」（如 \(0/0\)、\(\sqrt{-1}\)）对应「定义域」（DOMAIN）。

还有一个**与 FPE 无关、但同样由生成的循环触发**的错误源：**危险下转型**（DANGEROUS_DOWNCAST）。当 ufunc 的输入/输出类型与内核函数类型之间需要一次有损转换时（例如把 `double` 强转成 `int`），生成器会在循环里插一段运行时守卫：若强转前后值不等（说明精度已丢失），就直接调 `sf_error.error(func_name, sf_error.DOMAIN, ...)` 并把该输出写成 `NAN`。这是「软件主动报错」，与硬件 FPE 是两条不同的路径，但汇入同一个 `sf_error`。

#### 4.1.3 源码精读

**① 读 FPE 标志的 nogil 包装** —— [_ufuncs_extra_code_common.pxi:L12-L20]：

[_ufuncs_extra_code_common.pxi:L12-L20](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_ufuncs_extra_code_common.pxi#L12-L20)

这段声明并包装了 NumPy 私有 API `PyUFunc_getfperr()`。注释点明了为何要包一层：NumPy 的 C-API 表 `PyUFunc_API` 在 SciPy 里是用「UNIQUE_SYMBOL」机制（一处定义、各处 extern）来共享的，直接在任意位置 `cimport` 调用容易和这套宏冲突；包成一个 `cdef public ... noexcept nogil` 函数后，调用点就处在一个 `PyUFunc_API` 已正确初始化的编译单元里，回避了宏污染。`public` 关键字还让它能被 `_ufuncs_cxx` 等其他扩展模块的 C 代码以 `extern` 方式链接（`sf_error.cc` 里就有 `extern "C" int wrap_PyUFunc_getfperr(void);`，见 [sf_error.cc:L41](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/sf_error.cc#L41)）。`noexcept nogil` 是说它可以在不持有 GIL 的热路径里安全调用。

**② C 层的位翻译** —— `sf_error_check_fpe` 把四个硬件位分别翻译成软件码，见 [sf_error.cc:L132-L146]：

[sf_error.cc:L132-L146](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/sf_error.cc#L132-L146)

每读到一个置位标志就调一次 `sf_error(...)`，剩下的路由（要不要真的发告警）交给 u7-l1 讲的 `sf_error_v`。注意 `NPY_FPE_INEXACT`（不精确，即运算结果需要舍入）被**故意跳过**——否则几乎每次浮点运算都会报错，毫无信息量。

**③ 生成器在循环末尾插入检测点** —— `generate_loop` 把检测语句拼到循环体之外（4 空格缩进，属于函数体而非 `for` 体），见 [_generate_pyx.py:L536-L541]：

[_generate_pyx.py:L536-L541](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L536-L541)

最后那行 `body += "    sf_error.check_fpe(func_name)\n"` 就是前文流程图里打星号的那一步。对照同函数上方 `for i in range(n):`（[L491](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L491) 处 4 空格）及其循环体（8 空格缩进），可以确认 `check_fpe` 在循环**之外**，整批只执行一次。

**④ 危险下转型的软件守卫** —— 同一个 `generate_loop` 在逐元素运算两侧都插了运行时检查。输入侧见 [_generate_pyx.py:L511-L518]：

[_generate_pyx.py:L511-L518](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L511-L518)

逻辑是：若某个输入需要危险下转（`(ufunc_inputs[j], func_inputs[j]) in DANGEROUS_DOWNCAST`），就生成一段 `if 强转前后相等: 正常调用 else: sf_error.error(DOMAIN) 且输出 NAN` 的代码。输出侧（[L523-L535](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L523-L535)）同理。`DANGEROUS_DOWNCAST` 这个集合本身在 [_generate_pyx.py:L388-L397] 定义，枚举了所有有损转换对（如 `(d, i)`、`(D, f)`）。

> 把 ③ 和 ④ 放在一起看，就能概括出生成的 inner loop 里 `sf_error` 的**两个触发源**：循环内的 `sf_error.error(...)`（危险下转，逐元素）和循环末尾的 `sf_error.check_fpe(...)`（硬件 FPE，整批一次）。两者最终都汇入同一个 C 函数 `sf_error`。

#### 4.1.4 代码实践

**实践目标**：观察一次真实的硬件浮点上溢如何被翻译成 `sf_error` 的 `overflow` 类别，并验证 `check_fpe` 是「整批一次」而非「逐元素」。

**操作步骤**（这是一条「源码阅读 + 运行观察」型实践）：

1. 阅读 [_generate_pyx.py:L412-L543](_generate_pyx.py) 的 `generate_loop` 全函数，确认 `sf_error.check_fpe(func_name)` 出现在 `for i in range(n):` 循环**之外**（缩进为 4 空格）。
2. 在已安装 SciPy 的环境运行下面这段「示例代码」（非项目原有代码）：

```python
# 示例代码：观察 FPE→sf_error 的翻译
import warnings
import scipy.special as sc

# 默认 overflow 是 ignore，先打开成 warn 才能看到信号
with sc.errstate(overflow='warn'):
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        # exp10(1000) 必然硬件上溢，得到 inf
        sc.exp10(1000.0)
        # 再对一个数组批量调用，确认「整批一次」只会产生一条告警
        sc.exp10([1000.0, 1000.0, 1000.0])

for warning in w:
    print(type(warning.message).__name__, '-', str(warning.message))
print('overflow 当前动作:', sc.geterr()['overflow'])
```

**需要观察的现象**：

- 第一处标量调用应触发一条 `SpecialFunctionWarning`，消息里含 `floating point overflow`（这正是 [sf_error.cc:L138](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/sf_error.cc#L138) 写的字符串）。
- 第二处对一个长度为 3 的数组调用，**告警条数应远少于 3**（通常只 1 条），印证 `check_fpe` 在循环末尾整批只查一次、FPU 标志是粘性的。

**预期结果**：能在 `w` 里捕到 `SpecialFunctionWarning`，且消息匹配 `overflow`；退出 `errstate` 块后 `geterr()['overflow']` 恢复为 `ignore`（状态隔离的正确性来自 4.2 讲的 `errstate`）。

> 待本地验证：不同 NumPy 版本对「同一批多次同位告警」的去重策略不同，数组那次可能恰好只报 1 条，也可能因为 `warnings` 模块的去重而更少——重点是「不超过元素个数」，据此体会粘性标志 + 整批查询的设计。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `sf_error_check_fpe` 跳过了 `NPY_FPE_INEXACT`（不精确）标志？

**参考答案**：浮点运算几乎总会产生不精确（舍入），若把 INEXACT 也翻译成 `sf_error`，每次普通运算都会触发告警，毫无诊断价值。只保留 DIVIDEBYZERO / OVERFLOW / UNDERFLOW / INVALID 这四个真正表示「出问题」的位。

**练习 2**：`check_fpe` 放在循环外（整批一次）而非循环内（逐元素），代价是什么？

**参考答案**：你只能知道「这批里发生过溢出/除零/……」，但定位不到具体是第几个输入元素导致的。这是用「精度」换「性能」的刻意取舍——逐元素查询 FPU 状态寄存器代价过高，而告警只需要知道「有这事」。

**练习 3**：危险下转型（`DANGEROUS_DOWNCAST`）触发的 `sf_error` 属于哪个类别？为什么和硬件 FPE 走的不是同一条路径？

**参考答案**：属于 `DOMAIN`（见 [L515-L516](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L515-L516)）。它不是硬件在算术运算中自发置位的标志，而是生成器在 C 代码里**主动**比较「强转前后是否相等」后调用的 `sf_error.error(...)`，属于软件主动报错；但它和 FPE 检测最终都调用同一个 C 函数 `sf_error`，因此共享同一套路由逻辑。

---

### 4.2 seterr/geterr/errstate:字符串与枚举的双向桥

#### 4.2.1 概念说明

u7-l1 讲的 TLS 动作表 `sf_error_actions[]` 是 C 层的整数数组（键是 `sf_error_t` 枚举整数、值是 `sf_action_t` 枚举整数）。但用户在 Python 里写的是 `sc.seterr(singular='warn')` 这样的**字符串**。中间需要一个翻译层：

- 把 `'singular'` 翻译成 `sf_error_t` 的整数码；
- 把 `'warn'` 翻译成 `sf_action_t` 的整数码；
- 调 C 函数 `sf_error_set_action(code, action)` 写进 TLS 表；
- 反过来，`geterr()` 要把表里的整数读出来，翻译回字符串给用户。

这个翻译层就是 `_ufuncs_extra_code.pxi` 里手写的两张映射表和三个函数。除此之外，它还解决了 u7-l1 埋下的一个关键伏笔：**因为 `sf_error_actions[]` 带 `static`，它被编译进每一个扩展模块，各持一份独立副本**。所以「改错误配置」必须同时通知所有持有副本的模块，否则就会出现「`_ufuncs` 里改了、`_special_ufuncs` 里没改」的分裂状态。

#### 4.2.2 核心流程

`seterr(singular='warn')` 一次调用的完整流程：

```
seterr(singular='warn')
   │
   ├─ olderr = geterr()                      ← 先快照旧状态（用于返回）
   │
   ├─ 处理特殊键 'all'（若给了 all='raise'，先把 11 类全设成 raise，再用后续 kwargs 覆盖）
   │
   └─ for error, action in kwargs.items():   ← 对每个 (singular, warn)
          action_int = _sf_error_action_map[action]   'warn' -> 1
          code_int   = _sf_error_code_map[error]      'singular' -> 1
          _set_action(code_int, action_int)            ← 写 _ufuncs 的 TLS 副本
          _ufuncs_cxx._set_action(...)                 ← 写 _ufuncs_cxx 副本
          _special_ufuncs._set_action(...)             ← 写 _special_ufuncs 副本
          _gufuncs._set_action(...)                    ← 写 _gufuncs 副本
          _ellip_harm_2._set_action(...)               ← 写 _ellip_harm_2 副本
   │
   └─ return olderr
```

`errstate` 则是对 `seterr` 的薄封装，实现「上下文管理」：

```
with errstate(singular='warn'):
     │ __enter__：self.oldstate = seterr(singular='warn')   ← 进入时设新值，并保存旧值
     │   ... 用户代码（在 singular=warn 下运行）...
     │ __exit__：seterr(**self.oldstate)                    ← 退出时把旧值写回
```

这是一个放之四海皆准的「资源快照-恢复」模式（NumPy 的 `errstate`、文件打开/关闭、`np.errstate` 都是同一套思路）。

#### 4.2.3 源码精读

**① 两张映射表** —— [_ufuncs_extra_code.pxi:L8-L29]：

[_ufuncs_extra_code.pxi:L8-L29](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_ufuncs_extra_code.pxi#L8-L29)

两个设计要点：

- `_sf_error_code_map`（[L8-L20](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_ufuncs_extra_code.pxi#L8-L20)）的整数值 1~10 与 `sf_error.h` 里枚举的顺序**完全一致**（SINGULAR=1…MEMORY=10），并刻意跳过 `OK`（0）——「没出错」无须配置处理方式。它只有「字符串→整数」一个方向。
- `_sf_error_action_map`（[L22-L29](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_ufuncs_extra_code.pxi#L22-L29)）则是**双向**的：同一张 dict 里既存 `'warn': 1` 又存 `1: 'warn'`。于是 `seterr` 用字符串键取出整数、`geterr` 用整数键取出字符串，复用同一张表。Python dict 允许混合类型的键，正好成全了这种写法。

**② `geterr`：读 + 反向翻译** —— [_ufuncs_extra_code.pxi:L32-L79]，核心循环在 [L75-L79]：

[_ufuncs_extra_code.pxi:L75-L79](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_ufuncs_extra_code.pxi#L75-L79)

对 `_sf_error_code_map` 的每个条目，调 C 函数 `sf_error.get_action(code)`（即 `sf_error_get_action`，读 TLS 数组，见 [sf_error.cc:L47-L49](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/sf_error.cc#L47-L49)）拿到整数动作，再用反向映射变回字符串。注意这里只读了 `_ufuncs` 自己那份副本——因为各副本默认值相同且 `seterr` 总是同步写所有副本（见 ③），读任意一份都一致。

**③ `seterr`：翻译 + 跨模块同步** —— [_ufuncs_extra_code.pxi:L82-L178]，重点是同步块 [L166-L176]：

[_ufuncs_extra_code.pxi:L166-L176](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_ufuncs_extra_code.pxi#L166-L176)

注释一针见血：「Error handling state must be set for all relevant extension modules in synchrony, since each carries a separate copy of this state.」这正是 u7-l1 埋下的伏笔的回收：`sf_error_actions[]` 因 `static` 而每模块一份，`seterr` 必须对 5 个扩展模块（`_ufuncs` 自身、`_ufuncs_cxx`、`_special_ufuncs`、`_gufuncs`、`_ellip_harm_2`）逐一调用 `_set_action`。其中本模块的 `_set_action` 是 `_ufuncs_extra_code_common.pxi` 里那个 `cdef` 函数（见 4.3），其余 4 个是各扩展模块自己暴露的同名函数。

`all=` 关键字的特殊处理在 [L159-L164](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_ufuncs_extra_code.pxi#L159-L164)：先把 11 类全设成同一个动作，再用后续具体键覆盖，这样 `seterr(all='raise', singular='ignore')` 这种「除了奇点全抛异常」的写法才成立（docstring 里有这个示例）。

**④ `errstate`：快照-恢复上下文管理器** —— [_ufuncs_extra_code.pxi:L181-L235]，三个方法在 [L227-L234]：

[_ufuncs_extra_code.pxi:L227-L234](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_ufuncs_extra_code.pxi#L227-L234)

注意它是一个**普通类**（`class errstate:`），不是装饰器、也不用 `contextlib`。`__enter__` 调 `seterr(**self.kwargs)` 并把返回的旧状态字典存到 `self.oldstate`；`__exit__` 无条件（不管块里有没有抛异常）调 `seterr(**self.oldstate)` 恢复。这里 `seterr` 返回的就是 `geterr()` 拍下的旧配置（[L157](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_ufuncs_extra_code.pxi#L157)），所以「进入时存什么、退出时就写回什么」，状态被精确还原。`__exit__` 不返回 `True`，因此块内已抛出的异常不会被吞掉。

> `get_action` / `set_action` 的 Cython 声明在 [sf_error.pxd:L26-L27](sf_error.pxd)，都是 `nogil`，意味着即便在释放 GIL 的内层循环里也能查询/改写动作表——不过 `seterr`/`geterr` 本身是普通 Python 函数，运行在 GIL 下。

#### 4.2.4 代码实践

**实践目标**：亲手验证 `errstate` 的「进入快照、退出恢复」语义，以及 `seterr` 跨模块同步的必要性。

**操作步骤**：

1. 阅读 [_ufuncs_extra_code.pxi:L227-L234](_ufuncs_extra_code.pxi) 的 `errstate` 三个方法，对照 [L157](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_ufuncs_extra_code.pxi#L157) 处 `seterr` 的 `olderr = geterr()`，向自己讲一遍：进入时 `seterr(**kwargs)` 返回旧状态并存入 `self.oldstate`，退出时 `seterr(**self.oldstate)` 把旧状态写回。
2. 运行下面这段「示例代码」，观察一次会产生**奇点**（singular）的调用——`gammaln(0)`（Gamma 在 0 处有极点）：

```python
# 示例代码：验证 errstate 的状态隔离
import warnings, scipy.special as sc

print('默认 singular =', sc.geterr()['singular'])   # ignore
print('gammaln(0) =', sc.gammaln(0))                 # inf，无告警

with sc.errstate(singular='warn'):
    print('块内 singular =', sc.geterr()['singular'])  # warn
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        sc.gammaln(0)                                  # 此处应触发 SpecialFunctionWarning
    print('块内捕到告警数:', len(w),
          '| 类型:', type(w[0].message).__name__ if w else None)

print('退出后 singular =', sc.geterr()['singular'])  # 恢复成 ignore —— 状态隔离成功
```

**需要观察的现象**：

- 进入 `with` 块前后，`geterr()['singular']` 分别是 `ignore` → `warn` → `ignore`，证明「临时改、自动还」。
- 块内的 `gammaln(0)` 捕到一条 `SpecialFunctionWarning`；块外同样调用不报警。

**预期结果**：`gammaln(0)` 恒返回 `inf`（错误处理不影响返回值，只决定要不要额外发信号）；告警只在 `errstate(singular='warn')` 包裹下出现。

> 待本地验证：若你打开了 `warnings` 的 `'once'` 或 `'module'` 过滤，重复调用可能不重复报警，实践时请保持 `simplefilter("always")`。

#### 4.2.5 小练习与答案

**练习 1**：`_sf_error_action_map` 为什么要同时存 `'warn': 1` 和 `1: 'warn'` 两个方向？`_sf_error_code_map` 为什么只存一个方向？

**参考答案**：`seterr` 需要把用户给的字符串动作翻译成整数写进 C 层，`geterr` 又需要把 C 层读出的整数翻译回字符串——同一个动作表被双向使用，所以两向都存。而错误类别只在 `seterr` 里需要「字符串→整数」（`geterr` 遍历时用的是 dict 的 `.items()`，键本身就是字符串），所以 `_sf_error_code_map` 只存一个方向就够了。

**练习 2**：如果把 `seterr` 里那 5 个 `_set_action` 调用删到只剩本模块一个，会出现什么问题？给一个能暴露问题的场景。

**参考答案**：`_special_ufuncs` 等其他扩展模块的 TLS 副本不会被更新，于是 `seterr(singular='warn')` 后，调用走 `_special_ufuncs` 内核的函数（如新版 `airy`、`erf`）不会报警，而走 `_ufuncs` 内核的函数会报警——同一个 `errstate` 对不同函数表现不一致。这正是注释强调「in synchrony」的原因。

**练习 3**：`errstate.__exit__` 为什么「无条件」恢复状态，而不判断 `exc_type`？

**参考答案**：错误处理配置是一种「环境状态」，无论块内是正常结束还是抛了异常，离开块后都应回到进入前的状态，否则一次异常就会让全局配置永久漂移。`__exit__` 不返回 `True`，所以块内的异常仍会向外传播——它只负责「还原环境」，不负责「吞异常」。

---

### 4.3 .pxi 代码注入:生成器如何把片段拼进 _ufuncs.pyx

#### 4.3.1 概念说明

本模块回答一个工程问题：`geterr` / `seterr` / `errstate` 这些函数明明是手写的、住在 `_ufuncs_extra_code.pxi` 这个静态文件里，可它们最终要成为**编译产物 `_ufuncs` 扩展模块**的成员（你在 Python 里 `sc.seterr` 调到的就是它们）。这中间是怎么衔接的？

答案分两层：

1. **Cython 的 `include` 指令是「文本粘贴」**。`include "foo.pxi"` 会把 `foo.pxi` 的全部内容原样插入到 `include` 所在位置，就像 C 的 `#include`。所以只要生成的 `_ufuncs.pyx` 里写一行 `include "_ufuncs_extra_code.pxi"`，那份手写代码就「长」进了生成的 `.pyx`。
2. **生成器 `_generate_pyx.py` 控制拼接顺序**。它把整个 `_ufuncs.pyx` 拆成若干「片段常量」+「动态生成的注册代码」，按固定顺序写盘。两个 `.pxi` 的 `include` 就夹在这些片段之间。

之所以拆成 `_common` 与非 `_common` 两个 `.pxi`，是因为**公共片段要被两个不同的生成产物共享**：`_ufuncs.pyx`（C 内核）和 `_ufuncs_cxx.pyx`（C++/Boost 内核）都需要 `wrap_PyUFunc_getfperr` 和 `_set_action` 这两个底层工具，但只有 `_ufuncs.pyx` 需要 `geterr/seterr/errstate`（用户面对的统一入口只在一个模块里）。

#### 4.3.2 核心流程

`_ufuncs.pyx` 自上而下的拼接顺序（对应 `generate_ufuncs` 写盘的 5 次 `f.write`）：

```
┌──────────────────────────────────────────────────────────┐
│ _ufuncs.pyx 的最终内容（由 generate_ufuncs 拼接）          │
├──────────────────────────────────────────────────────────┤
│ ① UFUNCS_EXTRA_CODE_COMMON                              │
│      # 自动生成注释 + from libc.math cimport NAN         │
│      include "_ufuncs_extra_code_common.pxi"   ← 公共工具 │
│                                                          │
│ ② UFUNCS_EXTRA_CODE                                     │
│      include "_ufuncs_extra_code.pxi"          ← 三件套   │
│                                                          │
│ ③ module_all                                            │
│      __all__ = [...]                                    │
│                                                          │
│ ④ toplevel                                              │
│      所有 inner loop 函数 + 内核声明 + PyUFunc_FromFunc..│
│                                                          │
│ ⑤ UFUNCS_EXTRA_CODE_BOTTOM                             │
│      from ._special_ufuncs import (...)   ← C++ 直注册者 │
│      jn = jv                              ← 别名         │
└──────────────────────────────────────────────────────────┘
```

而 `_ufuncs_cxx.pyx` 只用了 ①（公共片段）+ 自己的导出声明（见 [L925-L928](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L925-L928)），因为它只负责把 C++ 函数指针导出给 `_ufuncs` 用，不直接面对用户。

构建侧，`meson.build` 用 `fs.copyfile` 把两个 `.pxi` 从源码树拷进构建目录（[meson.build:L10-L11](meson.build)），这样 Cython 编译生成出来的 `_ufuncs.pyx` 时，`include` 才能在同目录找到它们。

#### 4.3.3 源码精读

**① 三个片段常量** —— [_generate_pyx.py:L275-L295]：

[_generate_pyx.py:L275-L295](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L275-L295)

注意 `UFUNCS_EXTRA_CODE_COMMON`（[L275-L282](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L275-L282)）里除了 `include` 还顺手 `from libc.math cimport NAN`——因为 4.1 讲的 `NAN_VALUE` 在生成循环里会写出字面量 `NAN`（[L399-L409](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L399-L409)），需要这个 import 才能编译。`UFUNCS_EXTRA_CODE_BOTTOM`（[L288-L295](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L288-L295)）用 f-string 把 `special_ufuncs` 名单拼成一句 `from ._special_ufuncs import (...)`，并定义历史别名 `jn = jv`。

**② 按序写盘** —— `generate_ufuncs` 里写 `_ufuncs.pyx` 的 5 次 `f.write`，见 [_generate_pyx.py:L905-L911]：

[_generate_pyx.py:L905-L911](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L905-L911)

这 5 行与上面流程图的 ①~⑤ **一一对应、顺序严格一致**。`toplevel`（[L890](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L890)）是把所有 inner loop 函数体、内核函数声明、`PyUFunc_FromFuncAndData` 注册语句拼到一起的「动态部分」，它排在两个 `include` 之后——所以 `geterr/seterr/errstate`（来自 ②）在文件里出现在各 ufunc 注册代码（来自 ④）之前，但都在 `__all__`（③）之后。

**③ 公共片段也写进 `_ufuncs_cxx.pyx`** —— [_generate_pyx.py:L925-L928]：

[_generate_pyx.py:L925-L928](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L925-L928)

`_ufuncs_cxx.pyx` 的开头也是 `UFUNCS_EXTRA_CODE_COMMON`，于是 `wrap_PyUFunc_getfperr` / `_set_action` 在 C++ 扩展里也有一份，使 `sf_error.cc` 的 `extern "C" int wrap_PyUFunc_getfperr(void);` 能正确链接。但这里**没有** `include "_ufuncs_extra_code.pxi"`——C++ 扩展不需要 `geterr/seterr/errstate`，避免重复定义。

**④ `_set_action` 的定义来自公共片段** —— [_ufuncs_extra_code_common.pxi:L29-L31]：

[_ufuncs_extra_code_common.pxi:L29-L31](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_ufuncs_extra_code_common.pxi#L29-L31)

它只是 `sf_error.set_action(code, action)`（C 层 `sf_error_set_action`，[sf_error.cc:L43-L45](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/sf_error.cc#L43-L45)）的 `noexcept nogil` 包装。`seterr` 在 [L172](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_ufuncs_extra_code.pxi#L172) 调用的 `_set_action` 就是它。包成 `cdef ... nogil` 而非直接在 Python 层调 `sf_error.set_action`，是为了让内层循环（`noexcept nogil` 环境）也能复用同一条写入路径——尽管 `seterr` 本身在 GIL 下运行，但保持接口一致更整洁。

**⑤ 构建侧拷贝 `.pxi`** —— [meson.build:L1-L12]：

[meson.build:L1-L12](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L1-L12)

`_ufuncs_pxi_pxd_sources` 是一串 `fs.copyfile(...)`，把两个 `.pxi` 连同一批 `.pxd`（如 [sf_error.pxd](sf_error.pxd)、`_complexstuff.pxd`）从源码目录复制到构建目录。因为 `_generate_pyx.py` 产生的 `_ufuncs.pyx` 落在构建目录、Cython 也在构建目录里跑 `include`，所以这些被 include 的静态文件必须先被搬进构建目录。`fs.copyfile` 是 Meson 的文件复制原语，把这些静态依赖显式纳入构建图。

> 一条贯穿认知：本模块的三件套是**手写**的，但它们的「编译入口」是**生成**出来的。这是 special 模块「声明式生成 + 手写公共片段」混合工程的典型缩影——动态生成的部分（ufunc 注册）和手写的公共部分（错误处理、别名）通过 `include` 缝合在同一份 `.pyx` 里，最终编译成同一个 `_ufuncs.so`。

#### 4.3.4 代码实践

**实践目标**：亲手验证「`.pxi` 是文本粘贴、拼接顺序固定」，并理解改一个 `.pxi` 会级联影响生成产物。

**操作步骤**（源码阅读型）：

1. 在 [_generate_pyx.py:L905-L911](_generate_pyx.py) 数清写 `_ufuncs.pyx` 的 `f.write` 调用顺序，与 [_generate_pyx.py:L275-L295](_generate_pyx.py) 的三个常量、以及 `module_all`（[L903](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L903)）/`toplevel`（[L890](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L890)）一一对应，画出 `_ufuncs.pyx` 自上而下的章节顺序图。
2. 在 [meson.build:L1-L12](meson.build) 确认两个 `.pxi` 出现在 `_ufuncs_pxi_pxd_sources` 里，被 `fs.copyfile` 搬进构建目录；再用只读 `git` 命令确认这两个 `.pxi` 确实是源码树里的静态文件（而非生成物）：

```bash
# 确认 .pxi 是受版本控制的静态源文件
git ls-files scipy/special/_ufuncs_extra_code*.pxi
# 确认 _ufuncs.pyx 不在源码树（它是生成物）
git ls-files scipy/special/_ufuncs.pyx
```

**需要观察的现象**：

- 第一条应输出两个 `.pxi` 路径，证明它们是手写源文件；第二条应无输出（`_ufuncs.pyx` 不入库），证明它是构建期生成物。
- `_ufuncs.pyx` 的章节顺序为：公共 include → 三件套 include → `__all__` → ufunc 注册代码 → 底部 import + 别名。

**预期结果**：你能用自己的话讲清「`seterr` 这个函数体物理上住在 `_ufuncs_extra_code.pxi`，但逻辑上属于编译产物 `_ufuncs` 扩展模块，连接它们的桥梁是 `_generate_pyx.py` 写下的那行 `include`」。

> 待本地验证：若你有可写的 SciPy 开发环境（支持 `meson compile`），可在 `_ufuncs_extra_code.pxi` 的 `geterr` docstring 里加一个标记字符串，重新构建后 `import scipy.special; help(sc.geterr)` 应能看到该标记——这能最直观地证明「文本粘贴」。本实践不要求修改源码，仅作理解验证的建议。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `wrap_PyUFunc_getfperr` 和 `_set_action` 放在 `_common` 片段里，而 `geterr/seterr/errstate` 放在非 `_common` 片段里？

**参考答案**：前两者是底层工具，`_ufuncs.pyx`（C 内核）和 `_ufuncs_cxx.pyx`（C++ 内核）都要用——`sf_error.cc` 以 `extern "C"` 链接 `wrap_PyUFunc_getfperr`，`_ufuncs_cxx` 也需要 `_set_action` 让 `seterr` 能同步它的动作表副本。而 `geterr/seterr/errstate` 是面向用户的统一入口，只应在一个模块（`_ufuncs`）里定义一次，否则会造成重复定义。因此公共的进 `_common`，专一的进非 `_common`。

**练习 2**：如果删掉 `generate_ufuncs` 里写 `UFUNCS_EXTRA_CODE` 的那行 `f.write`（[L907](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L907)），编译 `_ufuncs.pyx` 会在哪个阶段失败？

**参考答案**：`include` 语句消失后，`_ufuncs.pyx` 里就不再有 `_sf_error_code_map`、`geterr`、`seterr`、`errstate` 的定义。但生成的 ufunc 注册代码和 `UFUNCS_EXTRA_CODE_BOTTOM` 仍可能引用相关名字，且 `__all__` 里仍列着 `'geterr'/'seterr'/'errstate'`（[L897](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L897)），导致 Cython 编译期或模块导入期报「未定义符号」失败。这反向印证了 `.pxi` 必须被拼接进来。

**练习 3**：`_ufuncs.pyx` 是生成物、`.pxi` 是手写静态文件，二者怎么在构建时「相遇」？

**参考答案**：`meson.build` 用 `fs.copyfile` 把 `.pxi`（及 `.pxd`）从源码目录复制到构建目录（[L10-L11](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L10-L11)）；`_generate_pyx.py` 也在构建目录产生 `_ufuncs.pyx`；于是 Cython 在构建目录编译 `_ufuncs.pyx` 时，`include "_ufuncs_extra_code.pxi"` 能在同目录找到目标文件，完成文本粘贴。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「从硬件 FPE 到 Python 告警」的全链路追踪。

**任务**：选定一个会同时触发**硬件 FPE** 和**软件 sf_error** 的调用，沿调用链逆向上溯，把每一层对应的源码位置标注出来。

**建议步骤**：

1. **选目标调用**：`scipy.special.spence(-1)`。它在 `x = -1` 处不在主定义域，会产生 `domain` 类错误（这正是 `seterr` docstring 示例里用的调用）。
2. **配置成可见**：用本讲学到的 `errstate` 包裹，让它抛异常而非静默：

```python
# 示例代码：全链路触发
import scipy.special as sc
try:
    with sc.errstate(domain='raise'):
        sc.spence(-1)
except sc.SpecialFunctionError as e:
    print('捕到:', e)
```

3. **沿链溯源**，逐层定位（每层都给出本讲引用过的源码位置）：
   - **Python 层**：`sc.errstate` → [_ufuncs_extra_code.pxi:L181-L235](_ufuncs_extra_code.pxi) 的 `errstate` 类，`__enter__` 调 `seterr(domain='raise')`。
   - **字符串→枚举翻译**：`seterr` 用 [_ufuncs_extra_code.pxi:L8-L29](_ufuncs_extra_code.pxi) 的两张表把 `'domain'` → `7`、`'raise'` → `2`。
   - **跨模块同步**：[_ufuncs_extra_code.pxi:L166-L176](_ufuncs_extra_code.pxi) 把动作写进 5 个扩展模块的 TLS 副本（经 [_ufuncs_extra_code_common.pxi:L29-L31](_ufuncs_extra_code_common.pxi) 的 `_set_action` → C 层 [sf_error.cc:L43-L45](sf_error.cc)）。
   - **内层循环触发**：`spence` 的 inner loop 由 [_generate_pyx.py:L412-L543](_generate_pyx.py) 的 `generate_loop` 生成，循环末尾有 `sf_error.check_fpe`（[L541](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L541)），内核本身也会就着越界参数调 `sf_error(DOMAIN)`。
   - **C 层路由**：`sf_error_v`（[sf_error.cc:L51-L123](sf_error.cc)）查 TLS 表发现 `domain=RAISE`，借 GIL 取 `SpecialFunctionError` 抛出。
4. **观察返回值与异常的关系**：把 `errstate` 改成 `domain='ignore'`（默认），同样的 `spence(-1)` 不再抛异常而是返回 `nan`——体会「错误处理只决定要不要发信号，不影响内核的占位返回值（nan/inf）」。

**交付物**：一张包含「Python 入口 → 字符串映射 → 跨模块同步 → inner loop 触发 → C 层 GIL 桥 → Python 异常」六栏的调用链表，每栏标注对应的源码文件与行号永久链接。

## 6. 本讲小结

- **FPE 检测是「整批一次」的硬件标志查询**：生成的 inner loop 在 `for` 循环**之外**调一次 `sf_error.check_fpe(func_name)`（[_generate_pyx.py:L541](_generate_pyx.py)），它经 `wrap_PyUFunc_getfperr`（[_ufuncs_extra_code_common.pxi:L15-L20](_ufuncs_extra_code_common.pxi)）读出粘性的 FPU 标志，由 `sf_error_check_fpe`（[sf_error.cc:L132-L146](sf_error.cc)）把四个硬件位翻译成 SINGULAR/OVERFLOW/UNDERFLOW/DOMAIN 四个软件码，跳过无信息量的 INEXACT。
- **危险下转是另一条软件触发路径**：`generate_loop` 对 `DANGEROUS_DOWNCAST` 的输入/输出插运行时守卫，值被破坏时主动调 `sf_error.error(DOMAIN, ...)` 并写 `NAN`（[_generate_pyx.py:L511-L535](_generate_pyx.py)），与硬件 FPE 殊途同归于同一个 `sf_error`。
- **三件套用两张表做字符串↔枚举双向桥**：`_sf_error_code_map`（单向，字符串→码，跳过 OK）与 `_sf_error_action_map`（双向，同一 dict 存两个方向）配合 `get_action`/`set_action` 完成 Python 字符串与 C 整数的互译（[_ufuncs_extra_code.pxi:L8-L79](_ufuncs_extra_code.pxi)）。
- **`seterr` 必须跨 5 个扩展模块同步写入**：因为 TLS 动作表 `sf_error_actions[]` 带 `static`，每模块一份副本，`seterr` 要逐一调用 `_ufuncs` / `_ufuncs_cxx` / `_special_ufuncs` / `_gufuncs` / `_ellip_harm_2` 的 `_set_action`（[_ufuncs_extra_code.pxi:L166-L176](_ufuncs_extra_code.pxi)），回收了 u7-l1 埋下的伏笔。
- **`errstate` 是经典的「快照-恢复」上下文管理器**：`__enter__` 调 `seterr` 存旧状态、`__exit__` 无条件写回（[_ufuncs_extra_code.pxi:L227-L234](_ufuncs_extra_code.pxi)），保证临时配置不泄漏到外层。
- **`.pxi` 是被 `include` 文本粘贴进生成产物的手写片段**：`_generate_pyx.py` 用 `UFUNCS_EXTRA_CODE_COMMON` / `UFUNCS_EXTRA_CODE` / `UFUNCS_EXTRA_CODE_BOTTOM` 三个常量把两个 `.pxi` 的 `include` 夹在固定位置（[_generate_pyx.py:L275-L295 与 L905-L911](_generate_pyx.py)），`meson.build` 用 `fs.copyfile` 把 `.pxi` 搬进构建目录供 Cython 找到（[meson.build:L10-L11](meson.build)）；公共片段还被 `_ufuncs_cxx.pyx` 复用。

## 7. 下一步学习建议

- **横向对照 NumPy 的同名机制**：本讲的 `seterr/geterr/errstate` 在 API 上刻意模仿 `numpy.seterr/geterr/errstate`。建议阅读 NumPy 文档与源码，对比二者：NumPy 管的是**通用浮点错误**，而 `scipy.special` 管的是**特殊函数语义错误**（多了 slow/loss/no_result/arg/other 等软件类别，这些 FPE 检测覆盖不到，只能由内核主动 `sf_error`）。
- **承接 U8（C/C++ 后端深入）**：本讲多次提到 `_special_ufuncs` / `_gufuncs` 这两条 C++ 直注册路径（它们也各自持有一份 TLS 动作表副本）。U8-l3「_special_ufuncs.cpp / _gufuncs.cpp:新的 ufunc 注册路径」会讲清楚这条不经 `functions.json` 的注册机制，以及它为何也要接入 `sf_error`。
- **回到 u7-l1 收尾**：如果你是从 u7-l1 直接跳来读 FPE 与三件套的，现在可以回去重读 `sf_error_v` 的 GIL 桥（[sf_error.cc:L51-L123](sf_error.cc)），结合本讲的「触发源」（硬件 FPE + 危险下转 + 内核主动）完整理解「错误从哪里来、到哪里去」。
- **动手验证（可选）**：在有 SciPy 开发环境的机器上，给 [_ufuncs_extra_code.pxi](_ufuncs_extra_code.pxi) 的某个 docstring 加一个标记字符串，重新 `meson compile` 后导入验证「`.pxi` 文本粘贴」的真实性——这是把本讲第 3 模块从「读懂」推进到「确信」的最直接方式。
