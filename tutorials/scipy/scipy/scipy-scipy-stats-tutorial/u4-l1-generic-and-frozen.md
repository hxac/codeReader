# rv_generic 与 frozen 分布机制

## 1. 本讲目标

本讲是「分布基础设施深入」的第一讲，承接 u3-l1/u3-l2/u3-l3 已经建立的「实例化模型 + 公共方法守门 + loc/scale 标准化」认知，往下钻一层，看这些公共能力到底挂在哪个基类上、`freeze()` 到底做了什么。

学完本讲，你应该能够：

- 说出 `rv_generic` 的角色，并列举它为 `rv_continuous` 与 `rv_discrete` 提供的公共能力（随机状态、形状参数解析、`freeze`/`__call__`、支撑域）。
- 解释「gen 单例实例」与「frozen 实例」的区别，理解为什么 `norm(loc=5)` 和 `norm.freeze(loc=5)` 等价、却又和 `norm` 本身不是同一个对象。
- 精确描述 `rv_frozen` 如何缓存 `args`/`kwds`，并在每一次方法调用（`pdf`/`cdf`/`ppf`/`rvs` 等）里把它们重新注入底层分布，从而实现「参数绑定」。
- 独立阅读 `_distn_infrastructure.py` 中 frozen 相关源码，并写出说明性注释。

## 2. 前置知识

本讲默认你已经掌握前置讲义的三个结论：

1. **实例化模型**（u3-l1）：`norm`、`gamma`、`binom` 这些名字，都是某个 `xxx_gen` 类在模块加载时造出的**单例实例**，而不是类本身。例如 `type(norm)` 是 `norm_gen`，而 `norm_gen` 才是类。
2. **公共/私有双层方法**（u3-l1）：公共方法 `pdf/cdf/ppf/rvs` 负责参数校验与 `loc/scale` 标准化，然后调用私有钩子 `_pdf/_cdf/_ppf`。
3. **loc/scale 平移缩放语义**（u3-l2、u3-l3）：连续分布满足 \(X = \text{loc} + \text{scale}\cdot Y\)，离散分布没有 `scale`，只有 \(X = \text{loc} + Y\)。

本讲会用到两个 Python 小知识，先在这里交代清楚：

- **「绑定方法」（bound method）**：在 Python 里，`obj.method` 会把 `obj` 自动绑定为方法的第一个参数 `self`。`rv_generic` 会用 `types.MethodType` 把动态生成的函数「绑」到实例上，变成实例方法。
- **「模板字符串 + `exec`」**：`_distn_infrastructure.py` 用一段字符串模板，在运行时拼出 `_parse_args` 等函数的源码，再用 `exec` 编译后挂到实例上。这是 scipy.stats 让「任意形状参数的分布」共享同一套参数解析代码的关键技巧。

## 3. 本讲源码地图

本讲几乎全部内容都集中在一个文件里：

| 文件 | 作用 | 本讲关注的行段 |
| --- | --- | --- |
| `_distn_infrastructure.py` | 分布基础设施，定义了 `rv_generic`、`rv_continuous`、`rv_discrete`、以及 frozen 家族 | `rv_generic`（L681 起）、`freeze`/`__call__`（L893–L915）、`rv_frozen`/`rv_continuous_frozen`/`rv_discrete_frozen`（L507–L610）、`rv_continuous.__init__` 与 `_updated_ctor_param`（L1877、L1959）、公共 `pdf`（L2054） |

记住一个总线索：**gen 类是「工厂 + 单例」，frozen 是「带参数的轻量包装」**。下面三个最小模块依次拆开这三层。

## 4. 核心概念与源码讲解

### 4.1 rv_generic：连续与离散分布的公共基类

#### 4.1.1 概念说明

`rv_continuous`（连续分布基类）和 `rv_discrete`（离散分布基类）共享大量逻辑：都要管理随机数状态、都要从 `_pdf`/`_pmf` 的签名里推断形状参数、都要提供 `freeze()`、都要计算支撑域。scipy 把这些「两类分布都需要的公共功能」抽到一个更上层的基类里——这就是 `rv_generic`。

类层次是这样的（u3-l1 已介绍 `rv_continuous`/`rv_discrete`）：

```text
rv_generic            ← 本讲的主角，公共能力都在这里
├── rv_continuous     ← 连续分布基类（norm/gamma 的父类逻辑）
└── rv_discrete       ← 离散分布基类（binom/poisson 的父类逻辑）
```

换句话说，`norm`（一个 `norm_gen` 实例）同时「是」一个 `rv_continuous`，也「是」一个 `rv_generic`。`rv_generic` 提供的关键公共能力有四块：

1. **随机状态管理**：`_random_state` 与 `random_state` 属性。
2. **形状参数解析器构造**：`_construct_argparser` + `_attach_argparser_methods`，动态生成 `_parse_args` 等方法。
3. **freeze 入口**：`freeze()` 与 `__call__()`，用来产出 frozen 对象。
4. **支撑域默认实现**：`_get_support`、`_support_mask`。

#### 4.1.2 核心流程

`rv_generic` 把「形状参数如何解析」这件最麻烦的事，统一成了一套模板机制，流程如下：

1. 子类（`rv_continuous`/`rv_discrete`）在自己的 `__init__` 里调用 `_construct_argparser`，传入要检查的钩子方法（连续是 `[_pdf, _cdf]`，离散是 `[_pmf, _cdf]`）和 loc/scale 形式。
2. `_construct_argparser` 检查这些钩子的函数签名，剥掉 `self`、`x`，剩下的就是形状参数（u3-l2 讲过的自动推断），再拼出一段模板字符串。
3. `_attach_argparser_methods` 用 `exec` 编译这段模板，把 `_parse_args`、`_parse_args_stats`、`_parse_args_rvs` 三个方法**挂到实例上**（注意是实例，不是类）。
4. 之后公共方法（如 `pdf`）只需调用 `self._parse_args(*args, **kwds)`，就能把用户传入的「形状参数 + loc + scale」整理成 `(shapes_tuple, loc, scale)` 三元组。

这段模板就是下面这个字符串，理解了它就理解了参数解析的全部秘密：

```python
def _parse_args(self, <shapes>, loc=0, scale=1):
    return (<shapes>), loc, scale
```

其中 `<shapes>` 在运行时被替换成具体分布的形状参数列表（如 gamma 的 `a`），离散分布则把 `scale=1` 写死。

#### 4.1.3 源码精读

先看 `rv_generic` 的定义与构造函数。注意 docstring 一句话点明了它的存在意义：「encapsulate common functionality between rv_discrete and rv_continuous」。

[_distn_infrastructure.py:L681-L695](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L681-L695) — `rv_generic` 类定义与 `__init__`：构造时探测 `_stats` 是否接受 `moments` 关键字，并初始化随机状态。

```python
class rv_generic:
    """Class which encapsulates common functionality between rv_discrete
    and rv_continuous."""
    def __init__(self, seed=None):
        super().__init__()
        sig = _getfullargspec(self._stats)
        self._stats_has_moments = (...)             # 探测 _stats 签名
        self._random_state = check_random_state(seed)  # 随机状态
```

接着看模板字符串本体，这是参数解析机制的「源代码的源代码」：

[_distn_infrastructure.py:L669-L678](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L669-L678) — `parse_arg_template` 模板：`%(shape_arg_str)s`、`%(locscale_in)s`、`%(locscale_out)s` 三个占位符在运行时被填充。

再看动态挂载方法的地方，注意那句注释「NB: attach to the instance, not class」——这是为了让每个分布实例拥有「自己的」`_parse_args`（因为形状参数因分布而异）：

[_distn_infrastructure.py:L739-L751](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L739-L751) — `_attach_argparser_methods`：`exec` 编译模板，用 `types.MethodType` 把生成的函数绑成实例方法。

而 `_construct_argparser` 负责「读签名 → 推形状 → 填模板」：

[_distn_infrastructure.py:L753-L832](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L753-L832) — `_construct_argparser`：若 `self.shapes` 非空则直接用，否则检查 `meths_to_inspect`（即 `_pdf`/`_cdf`）的签名自动推断形状参数，并校验签名一致性。

最后看两个被 frozen 机制依赖的支撑域方法：

[_distn_infrastructure.py:L1018-L1038](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L1018-L1038) — `_get_support` 默认实现：返回 `(self.a, self.b)`。注意 docstring 强调「Must be overridden by distributions which have support dependent upon shape parameters」——binom 这类上界随形状参数变化的分布会重写它（u3-l3 讲过）。

#### 4.1.4 代码实践

**实践目标**：亲眼确认 `norm` 同时是 `rv_generic` 的实例，并找到挂载在实例上的 `_parse_args` 方法。

**操作步骤**（请在本地 Python 里运行）：

```python
from scipy import stats
from scipy.stats._distn_infrastructure import rv_generic, rv_continuous

print(type(stats.norm).__name__)              # 预期: norm_gen
print(isinstance(stats.norm, rv_generic))     # 预期: True
print(isinstance(stats.norm, rv_continuous))  # 预期: True

# _parse_args 是挂载在【实例】上的方法
print('_parse_args' in stats.norm.__dict__)   # 预期: True
shapes, loc, scale = stats.norm._parse_args(loc=5, scale=2)
print(shapes, loc, scale)                     # 预期: () 5 2  (norm 无形状参数)

# gamma 有一个形状参数 a
g_shapes, g_loc, g_sc = stats.gamma._parse_args(2.0, loc=1, scale=3)
print(g_shapes, g_loc, g_sc)                  # 预期: (2.0,) 1 3
```

**需要观察的现象**：

- `norm` 没有形状参数，`_parse_args` 返回的 `shapes` 是空元组 `()`。
- `gamma` 的形状参数 `a=2.0` 被收进了 `shapes` 元组，`loc`/`scale` 各归其位。

**预期结果**：`norm._parse_args` 落在实例 `__dict__` 里（说明是动态挂载的），且能把混合的位置/关键字参数整理成三元组。本脚本未经本地实跑，输出值为依据源码推导，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_attach_argparser_methods` 要把方法挂到「实例」而不是「类」上？

> **参考答案**：因为不同分布的形状参数个数与名字不同（norm 是 0 个，gamma 是 1 个 `a`，beta 是 2 个），生成的 `_parse_args` 源码也因此不同。如果挂到类上，`rv_continuous` 的所有子类会共用同一个 `_parse_args`，互相覆盖；挂到实例上，每个分布单例都持有自己的版本。

**练习 2**：离散分布的 `locscale_out` 是什么？为什么和连续分布不同？

> **参考答案**：离散分布的 `locscale_out='loc, 1'`（见 4.3 节源码），把 `scale` 写死成 1。因为离散分布没有 `scale` 参数（u3-l3），标准化退化为 \(X=\text{loc}+Y\)。

### 4.2 freeze 与 __call__：从「单例实例」到「参数绑定对象」

#### 4.2.1 概念说明

回忆 u3-l2：`gamma(a, loc, scale)` 和 `gamma.freeze(...)` 是等价的，都返回一个「冻结了参数」的对象。但当时我们没追究这背后发生了什么。本节回答两个问题：

1. **`norm(loc=5)` 为什么能直接「调用」一个实例？**——因为 `rv_generic` 定义了 `__call__`。
2. **「冻结」到底冻结了什么？**——冻结的是 `loc`/`scale`/形状参数这些**调用参数**，不是把 pdf 预先算好缓存。

关键区分（本讲最重要的一个对比）：

| | gen 单例实例（如 `norm`） | frozen 实例（如 `norm(loc=5)`） |
| --- | --- | --- |
| 是什么 | 一个 `norm_gen`，全局唯一 | 一个 `rv_continuous_frozen`，每次 `freeze` 新建 |
| 持有参数吗 | 不持有，每次调用都要传 | 持有，缓存了 `args`/`kwds` |
| 类型 | `norm_gen`（`rv_continuous` 子类） | `rv_continuous_frozen` |
| 用途 | 「工厂 + 默认参数 loc=0, scale=1」 | 「带着固定参数反复用」 |

#### 4.2.2 核心流程

`freeze` 的分发逻辑非常简单：

1. 用户写 `norm(loc=5, scale=2)` 或 `norm.freeze(loc=5, scale=2)`。
2. `rv_generic.__call__` 被触发，它直接转调 `self.freeze(...)`。
3. `freeze` 用 `isinstance(self, rv_continuous)` 判断类型：连续分布返回 `rv_continuous_frozen`，否则返回 `rv_discrete_frozen`。
4. frozen 对象的 `__init__` 完成参数缓存与支撑域预计算（见 4.3 节）。

伪代码：

```text
norm(loc=5, scale=2)
  → rv_generic.__call__(norm, loc=5, scale=2)
  → rv_generic.freeze(norm, loc=5, scale=2)
  → isinstance(norm, rv_continuous)? 是
  → return rv_continuous_frozen(norm, loc=5, scale=2)
```

#### 4.2.3 源码精读

`freeze` 与 `__call__` 都在 `rv_generic` 里，紧挨着：

[_distn_infrastructure.py:L893-L915](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L893-L915) — `freeze` 按类型分发到 `rv_continuous_frozen`/`rv_discrete_frozen`；`__call__` 仅一行 `return self.freeze(*args, **kwds)`，并把 `freeze` 的 docstring 复用为 `__call__` 的 docstring。

```python
def freeze(self, *args, **kwds):
    if isinstance(self, rv_continuous):
        return rv_continuous_frozen(self, *args, **kwds)
    else:
        return rv_discrete_frozen(self, *args, **kwds)

def __call__(self, *args, **kwds):
    return self.freeze(*args, **kwds)
__call__.__doc__ = freeze.__doc__
```

这两行代码解释了 u3-l2 留下的一个伏笔：`gamma(a, loc, scale)` 和 `gamma.freeze(...)` 为什么完全等价——因为前者就是通过 `__call__` 走到后者。

> 小提醒：实践任务里提到的「`rv_frozen` 的 `__call__`」其实是个常见的口误。`__call__` 挂在 **gen 侧**（`rv_generic`），它让「调用一个分布实例」变得合法；而 frozen 侧（`rv_frozen`）本身**没有** `__call__`，它通过 `pdf`/`cdf` 等具名方法来用。下一节我们会看到 frozen 侧真正做的事。

#### 4.2.4 代码实践

**实践目标**：用 `is` 比较 `__call__` 与 `freeze` 两条路径产出的对象类型，确认二者等价。

**操作步骤**：

```python
from scipy import stats

a = stats.norm(loc=5, scale=2)          # 走 __call__
b = stats.norm.freeze(loc=5, scale=2)   # 走 freeze
print(type(a).__name__, type(b).__name__)   # 预期: rv_continuous_frozen x2
print(a.dist.__class__ is b.dist.__class__) # 预期: True（底层都是 norm_gen）
print(a is b)                               # 预期: False（两个独立 frozen 实例）
```

**需要观察的现象**：两条路径产生的 frozen 对象类型相同、底层分布类相同，但不是同一个对象（`is` 为 `False`）。

**预期结果**：`__call__` 与 `freeze` 行为一致，只是入口不同。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果某个第三方代码自定义了一个 `rv_continuous` 的子类分布 `mydist`，调用 `mydist(loc=1)` 会走哪条分支？

> **参考答案**：走 `rv_continuous_frozen` 分支。因为 `mydist` 的实例也是 `rv_continuous` 的实例，`isinstance(self, rv_continuous)` 为真。

**练习 2**：`__call__.__doc__ = freeze.__doc__` 这一行的作用是什么？删掉会怎样？

> **参考答案**：让 `help(norm.__call__)` 能显示和 `freeze` 一样的文档。删掉只是 `__call__` 失去 docstring，行为不受影响。

### 4.3 rv_frozen / rv_continuous_frozen / rv_discrete_frozen：参数缓存与转发

#### 4.3.1 概念说明

这一节是本讲的核心：frozen 对象到底缓存了什么、又是怎么在「多次方法调用间复用」参数的。

类层次如下，`rv_frozen` 是基类，连续/离散各自扩展：

```text
rv_frozen                       ← 通用 frozen：缓存参数 + 转发通用方法（cdf/ppf/rvs/mean...）
├── rv_discrete_frozen          ← 加 pmf/logpmf
└── rv_continuous_frozen        ← 加 pdf/logpdf
```

`rv_frozen.__init__` 做了三件事，是理解整个机制的钥匙：

1. **缓存调用参数**：把用户传进来的 `*args`（形状参数）和 `**kwds`（loc/scale 等）原样存到 `self.args`、`self.kwds`。
2. **新建一个底层分布实例**：用 `dist.__class__(**dist._updated_ctor_param())` 重新构造一个同类型的 `gen` 实例，而不是直接引用单例。
3. **预计算支撑域**：调用 `_parse_args` 拆出形状参数，再 `_get_support` 算出 `self.a`、`self.b` 缓存起来。

之后，frozen 的每一个方法（`pdf`/`cdf`/`ppf`/`rvs`/`mean`...）都是同一个套路：**把缓存的 `args`/`kwds` 重新展开，转发给底层分布的对应公共方法**。

为什么要「新建一个底层分布实例」而不是直接用 `norm` 这个单例？因为底层 `gen` 实例持有 `_random_state`（随机状态）。如果 frozen 直接共享单例，那么多个 frozen 对象（或 frozen 与单例本身）的随机数生成会互相干扰。给每个 frozen 配一个独立的底层实例，就隔离了各自的随机状态。

#### 4.3.2 核心流程

frozen 对象一次创建、多次复用的流程：

```text
创建阶段（仅一次）:
  rv_frozen.__init__(dist=norm, loc=5, scale=2)
    ├─ self.args = ()              # 形状参数（norm 无）
    ├─ self.kwds = {'loc':5,'scale':2}
    ├─ self.dist = norm.__class__(...)   # 新建一个独立的 norm_gen 实例
    └─ self.a, self.b = self.dist._get_support()  # 预计算支撑域

每次调用（如 frozen.pdf(x)):
  rv_continuous_frozen.pdf(x)
    → self.dist.pdf(x, *self.args, **self.kwds)
    → 走完整的公共 pdf（含 _parse_args、loc/scale 标准化、_pdf 钩子）
```

一个重要结论：**freeze 不是「预计算 pdf」式的缓存**。frozen 只缓存「输入参数」，每次调用仍然走完整的公共方法链路（参数解析、loc/scale 标准化、`_pdf` 钩子全都不省）。它的价值在于**方便与隔离随机状态**，而非加速单次求值。

#### 4.3.3 源码精读

先看 `rv_frozen.__init__`，这是本讲最关键的几行：

[_distn_infrastructure.py:L512-L520](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L512-L520) — `rv_frozen.__init__`：缓存 `args`/`kwds`，新建底层分布实例，预计算支撑域 `a`/`b`。

```python
def __init__(self, dist, *args, **kwds):
    self.args = args                                   # 缓存形状参数
    self.kwds = kwds                                   # 缓存 loc/scale 等
    # create a new instance
    self.dist = dist.__class__(**dist._updated_ctor_param())  # 新建底层实例
    shapes, _, _ = self.dist._parse_args(*args, **kwds)
    self.a, self.b = self.dist._get_support(*shapes)   # 预计算支撑域
```

其中 `_updated_ctor_param` 负责把构造分布所需的参数整理成字典，注释明确写着「Used by freezing. Keep this in sync with the signature of __init__.」：

[_distn_infrastructure.py:L1959-L1972](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L1959-L1972) — `rv_continuous._updated_ctor_param`：返回最新的构造参数（`a`/`b`/`xtol`/`name`/`shapes` 等），供 freeze 重建等价分布。

再看 frozen 方法的转发套路。连续分布多了 `pdf`/`logpdf`：

[_distn_infrastructure.py:L604-L610](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L604-L610) — `rv_continuous_frozen.pdf`/`logpdf`：把缓存的 `args`/`kwds` 展开，转发给底层分布的公共 `pdf`。

```python
class rv_continuous_frozen(rv_frozen):
    def pdf(self, x):
        return self.dist.pdf(x, *self.args, **self.kwds)
    def logpdf(self, x):
        return self.dist.logpdf(x, *self.args, **self.kwds)
```

离散分布对应的是 `pmf`/`logpmf`：

[_distn_infrastructure.py:L595-L601](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L595-L601) — `rv_discrete_frozen.pmf`/`logpmf`：同样的转发套路。

通用的 `cdf`/`ppf`/`rvs`/`mean` 等都写在基类 `rv_frozen` 里，模式完全一致。看几个代表：

[_distn_infrastructure.py:L530-L545](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L530-L545) — `rv_frozen.cdf` 与 `rv_frozen.rvs`：`cdf` 直接转发；`rvs` 额外把 `size`/`random_state` 合并进 `kwds` 再转发。

```python
def cdf(self, x):
    return self.dist.cdf(x, *self.args, **self.kwds)

def rvs(self, size=None, random_state=None):
    kwds = self.kwds.copy()
    kwds.update({'size': size, 'random_state': random_state})
    return self.dist.rvs(*self.args, **kwds)
```

注意 `rvs` 用 `self.kwds.copy()` 再 `update`，而不是直接改 `self.kwds`——这样每次调用的 `size`/`random_state` 不会污染缓存的参数，是「只读复用缓存」的正确写法。

为了让你看清「转发后走完整公共方法」这一点，回顾连续分布公共 `pdf` 的入口（u4-l2 会精读，这里只看它与 `_parse_args` 的衔接）：

[_distn_infrastructure.py:L2075-L2079](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L2075-L2079) — `rv_continuous.pdf` 开头：第一行就是 `self._parse_args(*args, **kwds)`，把 frozen 转发过来的参数再次拆成 `(args, loc, scale)`，随后做 `(x-loc)/scale` 标准化（即 u3-l1 讲过的 \(X=\text{loc}+\text{scale}\cdot Y\)）。

```python
args, loc, scale = self._parse_args(*args, **kwds)   # 拆参数
x, loc, scale = map(asarray, (x, loc, scale))
...
x = np.asarray((x - loc)/scale, dtype=dtyp)          # 标准化到标准型 Y
```

这就证实了 4.3.2 的结论：frozen 转发回来的参数，仍会完整经历 `_parse_args` 与 loc/scale 标准化。

最后，作为「形状参数自动推断」的对照，看 `rv_continuous.__init__` 如何调用 `_construct_argparser`（离散分布在 [_distn_infrastructure.py:L3357-L3361](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L3357-L3361) 以 `locscale_out='loc, 1'` 做同样的事）：

[_distn_infrastructure.py:L1905-L1908](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L1905-L1908) — `rv_continuous.__init__` 调用 `_construct_argparser(meths_to_inspect=[self._pdf, self._cdf], ...)`，并 `_attach_methods()` 挂载生成的方法。

#### 4.3.4 代码实践

**实践目标**（对应本讲的总实践任务）：阅读 `rv_frozen` 的参数缓存与转发逻辑，亲手验证 frozen 对象如何在多次方法调用间复用 shape/loc/scale，并写出说明性注释。

**操作步骤 1：观察缓存属性**。

```python
import numpy as np
from scipy import stats

rv = stats.gamma(2.0, loc=1, scale=3)   # 一个 frozen 对象
print('args :', rv.args)                 # 预期: (2.0,)  —— 形状参数 a
print('kwds :', rv.kwds)                 # 预期: {'loc': 1, 'scale': 3}
print('a, b :', rv.a, rv.b)              # 预期: 0.0 inf（标准型支撑域下界 0，见 u3-l2）
print('dist is singleton:', rv.dist is stats.gamma)  # 预期: False（独立底层实例）
```

**操作步骤 2：验证转发等价**。

```python
x = np.array([0.5, 1.0, 2.0])
print(np.allclose(rv.pdf(x),
                  stats.gamma.pdf(x, 2.0, loc=1, scale=3)))   # 预期: True
```

**操作步骤 3：给源码写注释**。打开 `_distn_infrastructure.py` 的 L512–L520（`rv_frozen.__init__`）和 L606–L607（`rv_continuous_frozen.pdf`），按下面的思路补注释（写在你的学习笔记里，不要改源码）：

- `self.args`/`self.kwds`：一次创建时缓存，之后所有方法只读复用。
- `self.dist = dist.__class__(...)`：新建独立底层实例，隔离随机状态，避免污染单例。
- `self.a, self.b = ... _get_support(...)`：预计算支撑域，供 frozen 的 `support()`/`interval()` 等快速返回。
- `pdf(self, x)` 的 `*self.args, **self.kwds`：每次调用都把缓存参数重新注入，等价于 `gamma.pdf(x, 2.0, loc=1, scale=3)`。

**需要观察的现象**：

- `rv.args`/`rv.kwds` 正好是你传入的形状参数与 loc/scale。
- `rv.dist` 不等于单例 `stats.gamma`（是新建的实例）。
- frozen 的 `pdf` 与显式传参的 `gamma.pdf` 数值完全一致。

**预期结果**：frozen 对象 = 「缓存的 `args`/`kwds` + 一个独立底层分布实例 + 预计算的支撑域」，每次方法调用都把缓存参数转发给底层公共方法。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果连续调用两次 `rv = stats.gamma(2.0, loc=1, scale=3)`，得到的两个 `rv` 是同一个对象吗？它们的 `.dist` 是同一个对象吗？

> **参考答案**：两次调用产生两个**不同的** frozen 对象（`is` 为 `False`），且各自的 `.dist` 也是两个**不同的**新建 `gamma_gen` 实例。因为每次 `freeze` 都会执行 `dist.__class__(...)` 新建底层实例。

**练习 2**：`rv_frozen.rvs` 为什么用 `self.kwds.copy()` 再 `update`，而不是 `self.kwds['size'] = size`？

> **参考答案**：后者会直接改写缓存的 `kwds`，导致上一次调用传入的 `size`/`random_state` 永久残留，污染后续调用。`.copy()` 保证缓存是只读的，每次调用都是独立的。

**练习 3**：frozen 对象的 `pdf` 调用，相比直接 `gamma.pdf(x, a, loc, scale)`，会更快吗？

> **参考答案**：不会明显更快。frozen 只省去了「用户重复敲参数」和「隔离随机状态」的麻烦，并没有省掉任何计算——参数仍会走完整的 `_parse_args` 与 loc/scale 标准化。它的价值是接口便利与状态隔离，不是性能。

## 5. 综合实践

把本讲三层知识串起来：**「单例 gen → freeze 分发 → frozen 缓存转发」**。

任务：用 `stats.norm` 完成下面的小流程，并在每一步标注它命中的源码位置。

1. 写 `n = stats.norm`，确认 `type(n)` 是 `norm_gen`，且 `isinstance(n, rv_generic)` 为真（命中 4.1）。
2. 分别用 `n(loc=5, scale=2)` 和 `n.freeze(loc=5, scale=2)` 创建两个 frozen 对象 `f1`、`f2`，确认它们类型相同、`is` 不同（命中 4.2，对应 `rv_generic.freeze`/`__call__`，L893–L915）。
3. 打印 `f1.args`、`f1.kwds`、`f1.a`、`f1.b`，并确认 `f1.dist is not n`（命中 4.3，对应 `rv_frozen.__init__`，L512–L520）。
4. 验证 `f1.pdf(0) == n.pdf(0, loc=5, scale=2)`，并解释为什么相等（对应 `rv_continuous_frozen.pdf` 转发，L606–L607，以及公共 `pdf` 的 `_parse_args`，L2075）。
5. 进阶：对 `stats.binom` 重复第 2、3 步，观察 `rv.args`（应是 `(n, p)`）和它的 frozen 类型（应是 `rv_discrete_frozen`），体会连续/离散两条分发分支（L908–L911）。

通过这个流程，你应该能在脑中画出一条完整链路：用户调用 → `__call__` → `freeze` 分发 → `rv_*_frozen.__init__` 缓存参数与新建底层实例 → 具名方法把缓存参数转发回底层公共方法。

## 6. 本讲小结

- `rv_generic` 是 `rv_continuous` 与 `rv_discrete` 的公共基类，集中提供随机状态管理、形状参数解析器构造（`_construct_argparser` + `_attach_argparser_methods`）、`freeze`/`__call__`、以及支撑域默认实现。
- 形状参数解析靠一段模板字符串（`parse_arg_template`）在运行时 `exec` 生成 `_parse_args` 等方法，并**挂到实例**而非类上，因为每个分布的形状参数不同。
- `rv_generic.__call__` 仅是 `self.freeze(...)` 的别名，所以 `norm(loc=5)` 与 `norm.freeze(loc=5)` 完全等价；`freeze` 按 `isinstance(self, rv_continuous)` 分发到 `rv_continuous_frozen` 或 `rv_discrete_frozen`。
- `rv_frozen.__init__` 做三件事：缓存 `args`/`kwds`、用 `_updated_ctor_param` **新建一个独立的底层分布实例**（隔离随机状态）、用 `_get_support` 预计算支撑域 `a`/`b`。
- frozen 的每个方法（`pdf`/`cdf`/`ppf`/`rvs`/...）都是同一个套路：把缓存的 `args`/`kwds` 展开转发给底层公共方法；freeze 缓存的是**参数**而非**计算结果**，因此不带来单次求值加速。

## 7. 下一步学习建议

下一讲 **u4-l2「分布方法的内部实现：pdf/cdf/ppf/rvs/fit」** 会继续在 `_distn_infrastructure.py` 里深入，承接本讲的两个点：

- 本讲看到公共 `pdf` 第一行调用了 `_parse_args` 并做 `(x-loc)/scale` 标准化，下一讲会完整追踪 `_pdf`/`_cdf` 钩子如何派生出 `logpdf`/`sf`/`ppf`，以及 `ppf` 的反演算法。
- 本讲提到 frozen 仍走完整公共方法，下一讲会讲清这条「完整公共方法」链路每一步在做什么。

建议阅读的源码：`rv_continuous.pdf`（L2054 起）、`rv_continuous.cdf`（L2135 起）、`rv_continuous._ppf_single`（L1977 起）。如果你对形状参数的元信息描述感兴趣，也可以提前翻看 u4-l3 会讲的 `_ShapeInfo`（L1627 起）。
