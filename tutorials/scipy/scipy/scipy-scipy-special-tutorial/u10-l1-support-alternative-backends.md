# _support_alternative_backends.py:_FuncInfo 与后端分发

## 1. 本讲目标

学完本讲后,你应该能够:

- 说清 `scipy.special` 为什么要专门用一个 `_support_alternative_backends.py` 文件来「覆盖」`_ufuncs`/`_basic` 里已经定义好的函数。
- 读懂 `_FuncInfo` 这个数据类(dataclass)的每一个字段,并理解它如何用「元数据驱动」的方式描述一个函数在多后端下的全部行为差异。
- 解释 `SCIPY_ARRAY_API` 这个全局开关在「关闭」与「开启」两种状态下,`wrapper` 属性分别返回什么、调用链有何不同。
- 描述 `_wrapper_for(xp)` 的六级解析顺序:NumPy 直通 → 后端原生函数 → `generic_impl` 回退实现 → marray 解包 → dask 分块 → 最终 NumPy/SciPy 兜底(JAX 还有一条 JIT 专用支路)。
- 自己动手验证:`SCIPY_ARRAY_API` 关闭时拿到的就是裸 ufunc 本身(只是文档串被原地改写),开启时则拿到一个会按 `array_namespace` 分发的包装函数。

本讲是 U10「Array API 与多后端支持」的首讲,聚焦「分发机制」本身;`@xp_capabilities` 装饰器如何同时驱动文档矩阵与测试标记,留待 u10-l2 详讲。

## 2. 前置知识

本讲默认你已经掌握以下概念(均来自前置讲义):

- **ufunc 是什么**(u2-l1):`scipy.special` 里绝大多数函数是 NumPy 通用函数,按 dtype 分发、逐元素求值。本讲处理的几乎都是这类「逐元素(elementwise)」函数——这点非常关键,后面会看到多后端兜底的很多技巧**只对逐元素函数成立**。
- **命名空间拼装**(u1-l4):`scipy.special` 的统一命名空间是由 `from ._ufuncs import *`、`from ._basic import *` 等多条导入语句拼出来的,后导入的同名符号会**覆盖**先导入的。本讲的覆盖机制就建立在这一点上。
- **Array API**(u4-l3):Python 有一套「数组 API 标准」([Array API](https://data-apis.org/array-api/)),让 NumPy、PyTorch、JAX、CuPy、Dask 等不同数组库暴露一致的接口。`logsumexp` 等函数已经用 `array_namespace` / `xp_promote` 实现了跨后端;本讲把这套机制推广到一大批 ufunc 上。

还需要补充两个本讲用到、但前置讲义未细讲的概念:

- **后端(backend)**:指一种具体的数组库实现,如 NumPy、PyTorch(`torch`)、JAX(`jax.numpy`)、CuPy(`cupy`)、Dask(`dask.array`)。每种后端有自己的「命名空间」(namespace),即包含 `asarray`、`log`、`where` 等函数的模块对象。
- **原生实现(native implementation)**:指某后端**自己**已经提供的同名函数。例如 `torch.special.expit`、`jax.scipy.special.erf` 都是 `special.expit`/`special.erf` 在该后端下的原生实现。能走原生就尽量走原生(更快、可 JIT、可 GPU),走不通才回退。

## 3. 本讲源码地图

本讲涉及的关键文件如下:

| 文件 | 角色 |
| --- | --- |
| [_support_alternative_backends.py](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py) | 本讲主角。定义 `_FuncInfo` 元数据类、`_wrapper_for` 分发器、若干 `generic_impl` 回退实现,以及函数清单 `_special_funcs`,最后用 `globals().update` 把裸 ufunc 替换成分发包装。 |
| [__init__.py](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/__init__.py) | 命名空间总装。在 `from ._ufuncs import *` **之后**执行 `from ._support_alternative_backends import *`,使后者带来的包装函数覆盖前者。 |
| [_array_api.py](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/_lib/_array_api.py) | (位于 `scipy/_lib/`)提供 `array_namespace`、`scipy_namespace_for`、`is_numpy`/`is_dask`/`is_jax`/`is_marray`、`xp_promote`、`xp_capabilities` 等公共工具。 |
| [_array_api_override.py](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/_lib/_array_api_override.py) | (位于 `scipy/_lib/`)定义全局开关 `SCIPY_ARRAY_API`(读环境变量)与 `array_namespace` 的主体实现。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开:**(1)** `_FuncInfo` 元数据;**(2)** `array_namespace` 分发;**(3)** `generic_impl` 回退。

### 4.1 模块一:`_FuncInfo` 元数据——用数据描述「这个函数在每个后端上长什么样」

#### 4.1.1 概念说明

设想你要把 `scipy.special` 里的 80 多个函数都改造成「能吃 PyTorch/JAX/CuPy/Dask 数组」。最朴素的写法是给每个函数手写一个 `if is_torch(xp): ... elif is_jax(xp): ...` 的大分支。但函数一多,这种写法会迅速膨胀且难维护。

SciPy 的做法是**把「函数在各后端上的差异」抽成一组结构化字段**,塞进一个数据类 `_FuncInfo`。每个被纳入多后端分发的函数对应一个 `_FuncInfo` 实例,记录:它有几个参数、是不是 ufunc、哪些参数必须整数、哪些后端用别名、找不到原生实现时用什么回退……这样,真正干活的分发器 `_wrapper_for` 就只需要**一份通用代码**,根据这些字段做决策。这就是「元数据驱动(metadata-driven)」的设计:数据描述差异,代码只写一遍。

#### 4.1.2 核心流程

`_FuncInfo` 的生命周期可以画成这样:

```text
            ┌─────────────────────────────────────────┐
            │  _special_funcs: 一个 _FuncInfo 元组     │
            │  (在模块加载时静态写好,约 80 条)         │
            └──────────────────────┬──────────────────┘
                                   │ 遍历
                                   ▼
        globals().update({nfo.func.__name__: nfo.wrapper ...})
                                   │
                                   ▼
              把模块全局名 erf/gamma/... 绑定到 nfo.wrapper
                                   │
                                   ▼
        __init__.py: from ._support_alternative_backends import *
                                   │  (后导入,覆盖 _ufuncs 的同名 ufunc)
                                   ▼
              用户调 special.erf(...) 实际进入 nfo.wrapper(...)
                                   │
                                   ▼
              wrapper 按 SCIPY_ARRAY_API 决定:裸 ufunc 还是分发函数
                                   │
                                   ▼ (开启时)
              _wrapper_for(xp):按六级优先级解析出真正要调的内核
```

`_FuncInfo` 字段虽然多,但可分为三类:**身份字段**(`func`/`n_args`/`is_ufunc`)描述函数本身;**分发字段**(`alt_names_map`/`generic_impl`/`backends_with_func_in_xp`/`torch_native`)指导 `_wrapper_for` 怎么找内核;**测试字段**(`int_only`/`positive_only`/`python_int_only`/`scalar_or_0d_only`/`test_large_ints`/`xp_capabilities`)主要给测试套件用,决定怎么造测试输入、哪些后端要 skip/xfail。

#### 4.1.3 源码精读

`_FuncInfo` 用 `@dataclass` 声明,字段即元数据。先看身份与分发字段:

```python
@dataclass
class _FuncInfo:
    # NumPy-only function. IT MUST BE ELEMENTWISE.
    func: Callable
    # Number of arguments, not counting out=
    n_args: int
    ...
    # Generic implementation to fall back on if there is no native dispatch
    generic_impl: ... = None
    # Handle case where a backend uses an alternative name for a function.
    alt_names_map: dict[str, str] | None = None
    # Some functions only take integer arrays for some arguments.
    int_only: tuple[bool, ...] | None = None
    ...
    is_ufunc: bool = True
```
> 见 [_support_alternative_backends.py:25-60](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L25-L60)。注意 `func` 字段上方那句大写注释 **"IT MUST BE ELEMENTWISE"**——这是整套兜底机制(marray/dask)成立的前提。

几个值得单独点出的字段:

- `alt_names_map`:某些后端用不同的名字暴露同一函数。例如 PyTorch 把 `j0` 叫 `bessel_j0`、把 `k0` 叫 `modified_bessel_k0`,JAX 把 `psi` 叫 `digamma`。分发器查找原生函数前会先查这张表改名,见 [_support_alternative_backends.py:666](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L666) 与 [_support_alternative_backends.py:823](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L823)。
- `generic_impl`:「当原生实现不存在时,用一段手写的、跨后端的纯 Array API 代码来实现它」。这是模块三的主题,典型例子是 `_xlogy`、`_rel_entr`。
- `int_only` / `positive_only`:标出哪些参数只接受整数、哪些后端下需要限定为正值。它们主要被 `tests/test_support_alternative_backends.py` 用来生成合适的测试输入,避免在无定义域上比较数值。`positive_only` 特别灵活,可以是 `bool`、按参数位的 `tuple[bool,...]`,或是按后端再分叉的 `dict`,见 [_support_alternative_backends.py:54-57](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L54-L57)。
- `backends_with_func_in_xp`:少数函数在「数组命名空间 `xp`」里有,却在「scipy 命名空间」里没有(典型是 `jax.numpy.sinc` 有但 `jax.scipy.special.sinc` 没有),见 [_support_alternative_backends.py:83-86](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L83-L86) 与用法 [_support_alternative_backends.py:841-849](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L841-L849)。
- `torch_native`:标记是否真的用了 PyTorch 原生实现。因为 PyTorch 默认 dtype 会影响类型提升,「回落到 NumPy」与「用原生 torch」在 float32 输入下可能产出不同 dtype,测试需要知情,见 [_support_alternative_backends.py:74-82](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L74-L82)。

`_FuncInfo` 还提供了两个派生接口:`name` 取函数名,`wrapper` 是真正的入口(模块二精读),`__hash__`/`__eq__` 是为了配合 `@lru_cache`,见 [_support_alternative_backends.py:88-97](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L88-L97)。

最后看「函数清单」与「覆盖」这两步,它们发生在模块加载时:

```python
# Override ufuncs.
# When SCIPY_ARRAY_API is disabled, this exclusively updates the docstrings in place
# and populates the xp_capabilities table, while retaining the original ufuncs.
globals().update({nfo.func.__name__: nfo.wrapper for nfo in _special_funcs})
# digamma is an alias for psi. ...
digamma = psi  # type:ignore[name-defined]  # noqa: F821
__all__ = [nfo.func.__name__ for nfo in _special_funcs] + ["digamma"]
```
> 见 [_support_alternative_backends.py:923-931](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L923-L931)。

这条 `globals().update` 是整个机制的「总开关动作」:它把模块里 `erf`、`gamma`、`j0`…… 这些名字,从原先(由 `from . import _ufuncs` 间接持有的)裸 ufunc,改绑到对应的 `nfo.wrapper` 上。注释点破了关键:当 `SCIPY_ARRAY_API` 关闭时,这一步**只是原地改写了文档串、填充了能力表,而保留了原始 ufunc**(原因见 4.2.3)。`digamma = psi` 则顺手让 `psi` 的别名 `digamma` 也带上多后端能力。

#### 4.1.4 代码实践

**实践目标**:用源码阅读 + Python 内省,确认 `_special_funcs` 清单的存在与规模,并观察 `globals().update` 之后模块里名字的绑定情况。

**操作步骤**:

1. 打开 [_support_alternative_backends.py:361-921](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L361-L921),数一数 `_special_funcs` 元组里有几条 `_FuncInfo(...)`,挑出 `erf`、`gamma`、`j0`、`xlogy` 四条,分别记下它们各自用了哪些字段。
2. 在已装 SciPy 的环境里运行下面这段内省脚本:

```python
import scipy.special._support_alternative_backends as sab
# 1) 清单规模
infos = sab._special_funcs
print("纳入多后端分发的函数数:", len(infos))
# 2) 每个 _FuncInfo 的名字与关键字段
for nfo in infos:
    if nfo.name in {"erf", "gamma", "j0", "xlogy"}:
        print(nfo.name, "n_args=", nfo.n_args,
              "is_ufunc=", nfo.is_ufunc,
              "generic_impl=", nfo.generic_impl.__name__ if nfo.generic_impl else None,
              "alt_names_map=", nfo.alt_names_map)
# 3) 观察 globals().update 的效果:模块里的 erf 现在是 wrapper 还是 ufunc?
import os
print("SCIPY_ARRAY_API =", repr(sab.SCIPY_ARRAY_API))
print("type(sab.erf) =", type(sab.erf))
```

**需要观察的现象**:

- `len(infos)` 应是 80 多(具体数字以本地为准,记下来即可)。
- `erf`/`gamma`/`j0` 的 `generic_impl` 应为 `None`(它们依赖后端原生或兜底);`xlogy` 的 `generic_impl` 应是 `_xlogy`。
- `j0` 的 `alt_names_map` 应含 `{"torch": "bessel_j0"}`。
- 在 `SCIPY_ARRAY_API` 未设置时,`type(sab.erf)` 仍是 `numpy.ufunc`(因为 `wrapper` 关闭分支返回的是 `self.func` 本身,见 4.2)。

**预期结果**:你会看到 `_FuncInfo` 用很少的字段就精确刻画了每个函数的「后端差异面」,而真正干活的分发代码只有 `_wrapper_for` 一份。

> 待本地验证:具体函数计数、`SCIPY_ARRAY_API` 的环境取值依你的运行环境而定。

#### 4.1.5 小练习与答案

**练习 1**:`_FuncInfo.func` 字段上方为什么用大写注释强调「IT MUST BE ELEMENTWISE」?如果塞进一个非逐元素函数(例如带规约的 `logsumexp`)会怎样?

> **答案**:因为后面 `_wrapper_for` 对 marray(按掩码逐元素传播)和 dask(`map_blocks` 逐块套用)的兜底都**依赖「逐元素」这一性质**——逐元素函数对每个块独立求值、对掩码逐位传播,结果与整块计算一致。若是带规约的函数,`map_blocks` 会让结果随分块方式而变,掩码传播也无一般规则,因此该兜底框架只适用于逐元素函数。`logsumexp` 正因如此并不在 `_special_funcs` 里,而是在 `_logsumexp.py` 中独立实现。

**练习 2**:`alt_names_map={"torch": "bessel_j0"}` 这条元数据会在分发流程的哪一步生效?如果某后端既不改名、原生函数又存在,这条字段是否还需要?

> **答案**:它在 `_wrapper_for` 调用 `_get_native_func` 查找原生函数**之前**生效——`_get_native_func` 会先用 `alt_names_map` 把目标函数名从 `j0` 改成 `bessel_j0`,再去 `torch` 命名空间里 `getattr`。若后端不改名且原生函数存在,则 `alt_names_map` 可为 `None`,分发器直接用原函数名查找即可。

---

### 4.2 模块二:`array_namespace` 分发——`SCIPY_ARRAY_API` 开关与运行时分发

#### 4.2.1 概念说明

光有元数据还不够,还需要一个「入口函数」在每次调用时做两件事:第一,根据传入的数组推断它们属于哪个后端(即拿到命名空间 `xp`);第二,按 `_FuncInfo` 的元数据把调用路由到正确的内核。这个入口就是 `_FuncInfo.wrapper`,而推断后端的工具是 `array_namespace`。

这里有一个全局开关 `SCIPY_ARRAY_API`(读自同名环境变量)。它的设计哲学是**「默认零开销」**:绝大多数用户只用 NumPy,没必要每次调用都去推断后端、走分发逻辑。因此开关关闭时,`wrapper` 直接返回**原始 ufunc**,与不开多后端支持时完全一样(零额外开销),唯一副作用是借机把文档串和能力表原地补好。只有显式设置 `SCIPY_ARRAY_API=1` 开启时,才会真正插入一个分发包装。

#### 4.2.2 核心流程

`wrapper` 的决策逻辑可以写成这样:

```text
wrapper 属性被读取:
  ├─ 若 self.name 已在 globals()(通常单元测试中被 lazy_xp_function 覆盖):
  │     返回 scipy.special.<name>   # 让测试钩子优先生效
  ├─ 否则,若 SCIPY_ARRAY_API 为真:
  │     返回 wrapped(*args,**kwargs):
  │        xp = array_namespace(*args)      # 推断后端
  │        return self._wrapper_for(xp)(*args, **kwargs)
  └─ 否则(SCIPY_ARRAY_API 关闭):
        func = self.func                    # 直接用裸 ufunc
        # 但仍跑一遍 xp_capabilities()(原地改文档串、填能力表)
        return func
```

而 `array_namespace` 自身在 `SCIPY_ARRAY_API` 关闭时有一个**早退捷径**:直接返回 NumPy 命名空间,跳过一切合规检查。

`_wrapper_for(xp)` 的六级解析顺序(后面任一级成功就返回,不再继续)是:

1. **NumPy 直通**:`is_numpy(xp)` → 返回 `self.func`(裸 ufunc,最快路径)。
2. **后端原生函数**:在 `xp` 自身或对应的 scipy 命名空间(如 `jax.scipy.special`、`cupyx.scipy`)里 `getattr` 出同名(或 `alt_names_map` 改名后)函数。
3. **`generic_impl` 回退**:若 `_FuncInfo` 提供了 `generic_impl`,调用它得到一个跨后端的纯 Array API 实现(模块三)。
4. **marray 解包/重包**:对带掩码的 MArray,拆出 `.data` 计算、再合并掩码重包。
5. **dask 分块**:用 `map_blocks` 把函数套到每个分块上(依赖逐元素性质)。
6. **最终兜底**:转成 NumPy 算完再转回 `xp`;JAX ufunc 还有一条 JIT 友好的支路(`resolve_dtypes` + `lazy_apply`)。

#### 4.2.3 源码精读

先看入口 `wrapper` 属性(本模块的「心脏」):

```python
@property
def wrapper(self):
    if self.name in globals():
        # Already initialised. We are likely in a unit test.
        # Return function potentially overridden by xpx.testing.lazy_xp_function.
        import scipy.special
        return getattr(scipy.special, self.name)

    if SCIPY_ARRAY_API:
        @functools.wraps(self.func)
        def wrapped(*args, **kwargs):
            xp = array_namespace(*args)
            return self._wrapper_for(xp)(*args, **kwargs)

        # Allow pickling the function. ...
        wrapped.__module__ = "scipy.special"
        wrapped.__qualname__ = self.name
        func = wrapped
    else:
        func = self.func

    capabilities = self.xp_capabilities or xp_capabilities()
    # In order to retain a naked ufunc when SCIPY_ARRAY_API is
    # disabled, xp_capabilities must apply its changes in place.
    cap_func = capabilities(func)
    assert cap_func is func
    return func
```
> 见 [_support_alternative_backends.py:99-126](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L99-L126)。

读这段要抓住三个分支与一处断言:

- **测试钩子优先**(L101-105):若名字已在模块全局表里(典型场景:测试用 `xpx.testing.lazy_xp_function` 把函数换成「懒求值」版本),优先返回 `scipy.special.<name>`,让测试覆盖生效。
- **开启分支**(L107-117):返回的 `wrapped` 每次被调用时,先 `array_namespace(*args)` 推断后端,再 `self._wrapper_for(xp)(...)` 路由。注意 `wrapped.__module__ = "scipy.special"` 是为了 pickle——因为 `@functools.wraps` 对 ufunc 不生效,需手动修。
- **关闭分支**(L118-119):直接 `func = self.func`,即原始 ufunc。
- **能力装饰**(L121-126):无论哪个分支,都跑一遍 `xp_capabilities()`。关键在那行 `assert cap_func is func`——它强制 `xp_capabilities` 必须**原地**修改(改文档串、往能力表里登记),而**不能**包一层返回新对象。这正是注释所说「关闭时仅原地改文档串、保留原始 ufunc」的实现保证。

再看分发器 `_wrapper_for`(用 `@functools.lru_cache(1000)` 缓存,同一 `xp` 不重复解析):

```python
@functools.lru_cache(1000)
def _wrapper_for(self, xp):
    if is_numpy(xp):
        return self.func

    # If a native implementation is available, use that
    in_xp = get_native_namespace_name(xp) in self.backends_with_func_in_xp
    namespace = xp if in_xp else _special_namespace_for(xp)
    f = _get_native_func(
        xp, namespace, self.name, alt_names_map=self.alt_names_map
    )
    if f is not None:
        return f
    ...
    # If generic ArrayAPI implementation is available, use that
    if self.generic_impl is not None:
        f = self.generic_impl(xp, namespace)
        if f is not None:
            return f
    ...
    # As a final resort, use the NumPy/SciPy implementation
    _f = self.func
    if is_jax(xp) and self.is_ufunc:
        ... # JAX JIT 友好支路:resolve_dtypes + lazy_apply
    else:
        def f(*args, _f=_f, xp=xp, **kwargs):
            args = [
                np.asarray(arg) if is_array_api_obj(arg) else arg for arg in args
            ]
            out = _f(*args, **kwargs)
            return xp.asarray(out)
    return f
```
> 见 [_support_alternative_backends.py:128-228](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L128-L228)。NumPy 直通在 L130-131;原生查找在 L133-140;`generic_impl` 在 L153-156;最终兜底在 L189-228(JAX 支路 L192-217,普通兜底 L218-226)。

关于原生查找的两个细节:

- `_special_namespace_for(xp)` 把后端映射到它的「scipy 子命名空间」(CuPy→`cupyx.scipy`、JAX→`jax.scipy`、PyTorch→`xp` 自身),由 `scipy_namespace_for` 实现,见 [_support_alternative_backends.py:20-22](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L20-L22) 与 [_array_api.py:426-445](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/_lib/_array_api.py#L426-L445)。
- `_get_native_func` 用 `alt_names_map` 改名后 `getattr`,见 [_support_alternative_backends.py:231-240](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L231-L240)。

最后看 `array_namespace` 的「零开销捷径」与全局开关的定义:

```python
# To enable array API and strict array-like input validation
SCIPY_ARRAY_API: str | bool = os.environ.get("SCIPY_ARRAY_API", False)
```
> 见 [_array_api_override.py:26-27](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/_lib/_array_api_override.py#L26-L27)。默认 `False`(未设环境变量时)。

```python
def array_namespace(*arrays: Array, sparse_ok=False) -> ModuleType:
    ...
    if not SCIPY_ARRAY_API:
        # here we could wrap the namespace if needed
        return np_compat
    ...
```
> 见 [_array_api_override.py:111-113](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/_lib/_array_api_override.py#L111-L113)。关闭时直接返回 NumPy 命名空间,跳过所有合规检查。这意味着类型校验只有在「开启」时才会触发。

#### 4.2.4 代码实践

**实践目标**:亲手验证「`SCIPY_ARRAY_API` 关闭时 `wrapper` 返回裸 ufunc、开启时返回分发函数」,并观察分发函数确实在按后端路由。

**操作步骤**:

1. 关闭开关(默认状态)下运行:

```python
import os
# 确保未开启(注意:必须在 import scipy 之前设置才完全生效,这里只做内省对比)
import scipy.special as sc
import numpy as np
print("是 ufunc 吗?", isinstance(sc.erf, np.ufunc))
print("erf 的文档串里是否被 xp_capabilities 追加了后端支持表格:",
      "PyTorch" in (sc.erf.__doc__ or "") or "torch" in (sc.erf.__doc__ or ""))
```

2. 阅读源码 [_support_alternative_backends.py:107-119](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L107-L119),用一句话写下「关闭与开启两分支返回值的本质区别」。

3. (可选,需装 `jax`)在 `SCIPY_ARRAY_API=1` 下启动一个新解释器,验证 JAX 数组能被 `scipy.special.erf` 接受并路由到 `jax.scipy.special.erf`:

```bash
# 需另开一个设置了环境变量的进程
SCIPY_ARRAY_API=1 python -c "
import jax.numpy as jnp, scipy.special as sc
x = jnp.array([0.0, 0.5, 1.0])
y = sc.erf(x)
print(type(y))            # 应为 jax 数组类型
print(y)
"
```

**需要观察的现象**:

- 步骤 1 中 `isinstance(sc.erf, np.ufunc)` 为 `True`(关闭分支返回的就是原始 ufunc)。
- 文档串是否含后端表格取决于该函数是否挂了非默认 `xp_capabilities`(本讲 u10-l2 详述);`erf` 未挂非默认能力,可能没有表格——这正是「关闭时保留裸 ufunc、仅原地改文档」的体现。
- 步骤 3(若可运行)中,返回值是 JAX 数组而非 NumPy 数组,说明走了「后端原生函数」这一级(`jax.scipy.special.erf`)。

**预期结果**:你能清楚看到「同一名字 `special.erf`」在两种开关状态下行为不同:关闭时是零开销的裸 ufunc,开启时是一个会按 `array_namespace` 分发的智能包装。

> 待本地验证:步骤 3 需要 JAX 环境;无 JAX 时可用 `array_api_strict` 等其它后端替代观察。

#### 4.2.5 小练习与答案

**练习 1**:`wrapper` 里为什么一定要 `assert cap_func is func`?如果 `xp_capabilities` 返回了一个新对象(而非原地修改),在 `SCIPY_ARRAY_API` 关闭时会有什么后果?

> **答案**:关闭分支里 `func = self.func`(裸 ufunc)。断言强制 `xp_capabilities` 必须**原地**修改 `func`(改写它的 `__doc__`、往全局能力表登记),而**返回同一个对象**。若它返回了新对象,`wrapper` 就会把裸 ufunc 换成被包装的对象,违反「关闭时零开销、保留原始 ufunc」的设计契约——那么默认 NumPy 用户每次调用都会多一层 Python 包装开销,且 `isinstance(special.erf, np.ufunc)` 会变成 `False`,破坏既有 API 形态。

**练习 2**:`_wrapper_for` 为什么用 `@functools.lru_cache(1000)` 缓存?它缓存的键是什么、能省掉哪部分开销?

> **答案**:键是 `xp`(后端命名空间模块对象)。同一后端的「解析过程」(查原生、构造 generic_impl、定义兜底闭包)是确定且重复的——每次调用 `special.erf(jax_array)` 都会重新走一遍。缓存后,每个后端只在首次调用时解析一次,之后直接复用已构造好的内核函数对象,省掉反复 `getattr`、反复定义闭包的开销。1000 是对后端种类数量留下的充裕上限。

---

### 4.3 模块三:`generic_impl` 回退——当后端没有原生函数时,用纯 Array API 现写一个

#### 4.3.1 概念说明

并非每个 `scipy.special` 函数都被各后端原生实现。例如 PyTorch 没有 `betainc`,JAX 的 `betainc` 行为又可能与 SciPy 不一致。这时有两条路:**(a)** 退到最终兜底(转 NumPy 算完再转回去),慢且不可 JIT/GPU;**(b)** 用后端都遵循的 Array API 标准原语(`where`、`log`、`+`、`*` 等)**手写一个跨后端的实现**。

`generic_impl` 就是 (b)。它是一个工厂函数,签名形如 `generic_impl(xp, spsx) -> callable | None`:接收后端命名空间 `xp` 与对应的 scipy 命名空间 `spx`,返回一个用 `xp` 原语写成的函数;若返回 `None`,分发器会继续往下走(进入 marray/dask/兜底)。

这条机制的意义在于:**把「数学定义清楚、且能用基本算子表达」的函数,从「依赖特定后端是否实现」中解放出来**。只要后端支持标准 Array API,就能得到一个行为一致、可 JIT、可并行的实现。

#### 4.3.2 核心流程

`generic_impl` 在 `_wrapper_for` 里的位置是第三级(原生之后、兜底之前):

```text
_wrapper_for(xp):
  1. is_numpy → self.func
  2. _get_native_func → 后端原生函数(若有)
  3. self.generic_impl(xp, namespace) → 返回 f?
        是 → 用 f                  ← 本模块聚焦
        否(返回 None)→ 继续
  4. marray / dask / 最终兜底
```

一个 `generic_impl` 工厂的内部通常长这样:

```text
def _some_func(xp, spsx):
    # 先尝试借用后端原生里已有的「更基本」函数(可选)
    base = _get_native_func(xp, spsx, '<基础函数名>')
    if base is None:
        return None                # 借不到就放弃,交给后续兜底
    def __some_func(x, y, *, xp=xp):
        x, y = xp_promote(x, y, force_floating=True, xp=xp)
        # 用 xp.where / xp.log 等标准原语组装出目标函数
        return xp.where(x == 0., 0., x * xp.log(y))
    return __some_func
```

关键点:`generic_impl` 可以选择**返回 `None`**(表示「这个后端我也不实现」),从而把决策权交回给分发器走兜底。这让「能借原生就借、借不到再自写、自写也搞不定就兜底」形成一条优雅的退化链。

#### 4.3.3 源码精读

最简单的 `generic_impl` 例子是 `_xlogy`,它实现了 `xlogy(x, y) = x*log(y)`,并在 `x==0` 时返回 0(避免 `0 * log(0) = 0 * (-inf) = nan`):

```python
def _xlogy(xp, spsx):
    def __xlogy(x, y, *, xp=xp):
        x, y = xp_promote(x, y, force_floating=True, xp=xp)
        with np.errstate(divide='ignore', invalid='ignore'):
            temp = x * xp.log(y)
        return xp.where(x == 0., 0., temp)
    return __xlogy
```
> 见 [_support_alternative_backends.py:264-270](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L264-L270)。注意它只用 `xp_promote` + `xp.log` + `xp.where` 这几个跨后端原语,因此对任何支持 Array API 的后端都能工作。它对应 `_FuncInfo(_ufuncs.xlogy, 2, generic_impl=_xlogy)`,见 [_support_alternative_backends.py:882](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L882)。

更复杂、也更体现「退化链」的例子是 `_chdtr`。卡方分布 CDF `chdtr(v, x)` 在数学上等于 `gammainc(v/2, x/2)`。`_chdtr` 的策略是:**先看后端有没有原生 `gammainc`,有就基于它组合;没有就直接返回 `None`,让分发器退到 SciPy 兜底**(而不是错误地用 SciPy 的 `gammainc` 凑):

```python
def _chdtr(xp, spsx):
    # The difference between this and just using `gammainc`
    # defined by `get_array_special_func` is that if `gammainc`
    # isn't found, we don't want to use the SciPy version; we'll
    # return None here and use the SciPy version of `chdtr`.
    gammainc = _get_native_func(xp, spsx, 'gammainc')
    if gammainc is None:
        return None

    def __chdtr(v, x):
        res = gammainc(v / 2, x / 2)  # this is almost all we need
        # The rest can be removed when google/jax#20507 is resolved
        mask = (v == 0) & (x > 0)  # JAX returns NaN
        res = xp.where(mask, 1., res)
        mask = xp.isinf(v) & xp.isinf(x)  # JAX returns 1.0
        return xp.where(mask, xp.nan, res)
    return __chdtr
```
> 见 [_support_alternative_backends.py:274-290](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L274-L290)。这段同时展示了 `generic_impl` 的另一个价值:**在后端原生实现的已知缺陷处打补丁**(用 `xp.where` 修正 JAX 在 `v==0`、`v=inf` 边界的错误返回值),使各后端行为与 SciPy 对齐。

最「重量级」的例子是 `_stdtrit`:学生 t 分布的分位数函数(逆 CDF)。它**没有任何后端原生实现**,于是 `_stdtrit` 用 `scipy.optimize.elementwise` 的 `bracket_root`/`find_root` 在 `xp` 上做逐元素求根:

```python
def _stdtrit(xp, spsx):
    # Need either native stdtr or native betainc
    stdtr = _get_native_func(xp, spsx, 'stdtr') or _stdtr(xp, spsx)
    if stdtr is None:
        return None

    from scipy.optimize.elementwise import bracket_root, find_root

    def __stdtrit(df, p):
        def fun(t, df, p):  return stdtr(df, t) - p
        res_bracket = bracket_root(fun, xp.zeros_like(p), args=(df, p))
        res_root = find_root(fun, res_bracket.bracket, args=(df, p))
        return res_root.x

    return __stdtrit
```
> 见 [_support_alternative_backends.py:334-351](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L334-L351)。这展示了 `generic_impl` 的极致形态:用 SciPy 自家的高阶数值工具(逐元素求根)在任意 Array API 后端上**合成**一个原本不存在的特殊函数。

最后,把 `_FuncInfo` 与 `generic_impl` 的接线再确认一次:`_FuncInfo(_ufuncs.chdtr, 2, generic_impl=_chdtr)`、`_FuncInfo(_ufuncs.rel_entr, 2, generic_impl=_rel_entr)`、`_FuncInfo(_ufuncs.stdtr, 2, _needs_betainc, generic_impl=_stdtr, torch_native=False)` 等,见 [_support_alternative_backends.py:436](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L436)、[_support_alternative_backends.py:832](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L832)、[_support_alternative_backends.py:863-864](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L863-L864)。

#### 4.3.4 代码实践

**实践目标**:把 `_xlogy` 这个 `generic_impl` 当作「跨后端纯 Array API 实现」的范本来读懂,并用纯 NumPy 复现它,验证它解决了 `0 * log(0)` 的 NaN 问题。

**操作步骤**:

1. 阅读 [_support_alternative_backends.py:264-270](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L264-L270),用中文写出每一行的作用(提示:`xp_promote(..., force_floating=True)` 把输入统一提升为浮点;`np.errstate` 上下文只是压制 NumPy 的除零告警,对结果数值无影响;真正的语义在 `xp.where`)。
2. 运行下面这段「手写复现 + 行为验证」:

```python
import numpy as np
import scipy.special as sc

# 朴素实现(有 bug)
def naive_xlogy(x, y):
    return x * np.log(y)

# 仿 _xlogy 的修正实现
def fixed_xlogy(x, y):
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    with np.errstate(divide='ignore', invalid='ignore'):
        temp = x * np.log(y)
    return np.where(x == 0., 0., temp)

# 测试 x=0, y=0 这个坑:0*log(0) 朴素会得 nan,正确应是 0
print("朴素 xlogy(0, 0) =", naive_xlogy(0.0, 0.0))   # nan
print("修正 xlogy(0, 0) =", fixed_xlogy(0.0, 0.0))   # 0.0
print("SciPy xlogy(0, 0) =", sc.xlogy(0.0, 0.0))     # 0.0
```

3. 再读 [_support_alternative_backends.py:882](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L882),确认 `xlogy` 在 `_special_funcs` 里挂的就是 `_xlogy`,理解「当某后端没有原生 `xlogy` 时,会落到这段实现」。

**需要观察的现象**:

- 朴素实现在 `(x=0, y=0)` 返回 `nan`(`0 * (-inf) = nan`);修正实现与 SciPy 都返回 `0.0`。
- 这说明 `generic_impl` 不只是「换个后端算」,而是「用 Array API 原语**精确复刻 SciPy 的边界语义**」。

**预期结果**:你会理解 `generic_impl` 的本质——**用后端都认得的标准原语,把一个特殊函数的数学定义(含边界)重新表达一遍**,从而既跨后端又保持与 SciPy 一致。

> 待本地验证:NumPy 行为稳定,上述打印结果应可直接复现。

#### 4.3.5 小练习与答案

**练习 1**:`_chdtr` 里为什么要写成「先 `_get_native_func(xp, spsx, 'gammainc')`,取不到就 `return None`」,而不是直接调用 SciPy 的 `gammainc` 来组合?

> **答案**:为了**避免「跨后端混算」**。如果在 JAX 数组上调 SciPy 的 `gammainc`(NumPy 内核),会强制把数组拽回 NumPy、丢失 JAX 的 JIT/GPU 能力。`_chdtr` 的策略是:只有当**目标后端自己**有原生 `gammainc` 时,才用它在后端内组合出 `chdtr`;否则返回 `None`,把决策让回 `_wrapper_for` 走最终兜底(那才是「认命转 NumPy」的地方)。这样能把「留在原生后端」的机会最大化。

**练习 2**:`generic_impl` 工厂的返回值可以是 `None`,这一设计带来了什么好处?如果强制它必须返回一个函数会怎样?

> **答案**:返回 `None` 让 `generic_impl` 表达「我搞不定这个后端」。好处是形成了一条优雅的**退化链**:原生优先 → 自写实现(可选,按后端分别表态)→ marray/dask → NumPy 兜底。若强制它必须返回函数,作者就无法表达「这个后端上我不想自写、请走兜底」,只能要么硬写一个可能不正确的实现、要么抛异常,丧失了灵活的分档回退能力。`_chdtr`、`_betaincc`、`_stdtrit` 都依赖返回 `None` 这一逃生口。

---

## 5. 综合实践

把三个模块串起来,做一个「**追踪一次跨后端调用的完整旅程**」的小任务。

**任务背景**:用户在 `SCIPY_ARRAY_API=1` 环境下调用了 `scipy.special.xlogy(jax_array_a, jax_array_b)`。请你结合源码,画出从 Python 调用到最终数值计算的完整调用链,并标注每一步发生在哪个文件的哪段代码。

**操作步骤**:

1. **入口定位**:`xlogy` 这个名字在 `scipy.special` 命名空间里最终指向什么?追踪 [__init__.py:788-796](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/__init__.py#L788-L796),先 `from ._ufuncs import *` 带入裸 `_ufuncs.xlogy`,再 `from ._support_alternative_backends import *` 把它覆盖成 `_FuncInfo(...).wrapper`(因为 [_support_alternative_backends.py:926](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L926) 的 `globals().update`)。注意覆盖是「后导入覆盖先导入」,见 [__init__.py:794-796](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/__init__.py#L794-L796) 的注释。

2. **wrapper 决策**:因为 `SCIPY_ARRAY_API` 开启,`wrapper` 走 [_support_alternative_backends.py:107-117](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L107-L117) 分支,返回的 `wrapped` 会先 `array_namespace(a, b)` 推断出 JAX 命名空间。

3. **分发解析**:`_wrapper_for(jax_xp)` 依次走 [_support_alternative_backends.py:128-228](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L128-L228):非 NumPy → 查 `jax.scipy.special.xlogy` 原生(可能没有)→ 命中 `generic_impl=_xlogy`([_support_alternative_backends.py:882](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L882) + [_support_alternative_backends.py:264-270](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L264-L270))。

4. **实际求值**:在 JAX 数组上执行 `xp.where(x == 0., 0., x * xp.log(y))`,全程留在 JAX 后端。

5. **画一张序列图**(文字版即可),形如:

```text
用户调用 special.xlogy(jax_a, jax_b)
   │  (名字已由 _support_alternative_backends 覆盖)
   ▼
_FuncInfo.wrapper  →  wrapped(*args)              [L107-117]
   │
   ▼
array_namespace(jax_a, jax_b)  →  jax.numpy        [_array_api_override.py:73]
   │
   ▼
_FuncInfo._wrapper_for(jax.numpy) [lru_cache]      [L128]
   │  ① 非 NumPy
   │  ② _get_native_func(... "xlogy") → None(假设无原生)
   │  ③ generic_impl = _xlogy
   ▼
_xlogy(jax.numpy, jax.scipy.special)               [L264]
   │
   ▼
__xlogy(a, b): xp.where(a==0, 0, a*xp.log(b))      [L268-269]
   │
   ▼
返回 JAX 数组
```

6. **验证**:在装了 JAX 的环境里 `SCIPY_ARRAY_API=1` 运行 `scipy.special.xlogy(jnp.array([0.0, 2.0]), jnp.array([0.0, 1.0]))`,确认返回的是 JAX 数组、且第一个元素为 `0.0`(边界正确)。

**预期结果**:你能用一条完整调用链把本讲的三个模块(元数据、`array_namespace` 分发、`generic_impl` 回退)串起来,清楚地解释「为什么 `special.xlogy` 能透明地吃 JAX 数组」。

> 待本地验证:JAX 分支需 JAX 环境;若无 JAX,可改为追踪 `special.xlogy` 在纯 NumPy 下的「关闭开关」旅程(应直接命中 `is_numpy` 直通,返回裸 `_ufuncs.xlogy`)。

## 6. 本讲小结

- `_support_alternative_backends.py` 用一个数据类 `_FuncInfo` 把每个函数在多后端下的全部差异(参数数、是否 ufunc、整数/正值约束、后端别名、回退实现等)抽成**结构化元数据**,使分发逻辑只需写一份。详见 [_support_alternative_backends.py:25-97](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L25-L97)。
- 全局开关 `SCIPY_ARRAY_API`(读自同名环境变量)默认关闭:此时 `wrapper` 直接返回**裸 ufunc**,唯一副作用是借 `xp_capabilities` **原地**改写文档串、登记能力表(`assert cap_func is func` 是这一契约的保证)。详见 [_support_alternative_backends.py:99-126](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L99-L126)。
- 开启时,`wrapper` 返回一个**分发包装**:每次调用先 `array_namespace(*args)` 推断后端,再 `_wrapper_for(xp)`(带 `lru_cache`)按「NumPy 直通 → 后端原生 → `generic_impl` → marray → dask → NumPy/SciPy 兜底」六级解析。详见 [_support_alternative_backends.py:128-228](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L128-L228)。
- `generic_impl` 是「用纯 Array API 原语现写实现」的工厂,可返回 `None` 表达「此路不通」,从而构成优雅的退化链;`_xlogy`、`_chdtr`、`_stdtrit` 分别展示了「简单算子组合」「借原生+打补丁」「逐元素求根合成」三种形态。详见 4.3。
- 命名空间覆盖发生在模块加载时:[_support_alternative_backends.py:926](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L926) 的 `globals().update`,再由 [__init__.py:796](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/__init__.py#L796) 的 `from ._support_alternative_backends import *`(排在 `from ._ufuncs import *` 之后)把包装函数带入 `scipy.special`,覆盖原始 ufunc。
- 全套兜底(marray 按掩码传播、dask `map_blocks`)依赖「函数是逐元素」这一前提,这正是 `_FuncInfo.func` 字段上方那句 **"IT MUST BE ELEMENTWISE"** 的含义,也是 `logsumexp` 等非逐元素函数不在此清单、而在 `_logsumexp.py` 独立实现的原因。

## 7. 下一步学习建议

- **下一讲 u10-l2**:`@xp_capabilities` 装饰器如何用一份能力元数据同时驱动「文档串里的后端支持矩阵」与「测试中的 SKIP/XFAIL 标记」,实现文档、测试、运行时分发三位一体。本讲已多次提到 `xp_capabilities`(尤其是 `wrapper` 里那段 `assert cap_func is func`),下一讲拆开它内部。
- **延伸阅读源码**:
  - `_FuncInfo` 的测试字段(`int_only`/`positive_only`/`python_int_only`/`scalar_or_0d_only` 等)如何被消费:读 `tests/test_support_alternative_backends.py`,尤其是 `_skip_or_tweak_alternative_backends`([tests/test_support_alternative_backends.py:26-75](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/tests/test_support_alternative_backends.py#L26-L75))。
  - 另一套独立的多后端实现:`_logsumexp.py` 里的 `logsumexp`/`softmax`/`log_softmax`(u4-l3 已讲),对比它与本讲「元数据驱动 + 自动分发」的异同——前者是手写 Array API,后者是框架化分发。
  - 公共工具层:`scipy/_lib/_array_api.py` 的 `scipy_namespace_for`、`xp_promote`、`is_marray`,理解后端命名空间的映射规则。
- **动手方向**:尝试给 `_special_funcs` 里某个目前没有 `generic_impl` 的函数(如 `entr`)写一个跨后端的 `generic_impl` 雏形,体会「用 `xp` 原语精确复刻 SciPy 边界语义」的工程要点(注意:这只是练习,不要提交到仓库源码)。
