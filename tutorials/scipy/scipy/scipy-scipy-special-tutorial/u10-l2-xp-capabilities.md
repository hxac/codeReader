# @xp_capabilities:能力标注与文档化后端支持矩阵

## 1. 本讲目标

本讲承接 u10-l1（`_FuncInfo` 与后端分发），回答一个看似简单却贯穿整个 Array API 支持体系的问题:**一个函数到底「支持哪些后端」这件事,在 SciPy 里是用什么机制表达、文档化、并落到测试上的?**

读完本讲,你应当能够:

- 说清 `@xp_capabilities` 装饰器同时做的「两件事」,以及它为什么必须**原地修改**被装饰对象、绝不套壳。
- 看懂装饰器的全部参数(`cpu_only` / `np_only` / `exceptions` / `skip_backends` / `xfail_backends` / `warnings` 等)如何被翻译成一张「后端 × 设备」能力矩阵,并自动注入函数文档串。
- 理解同一份能力元数据如何被 `make_xp_pytest_param` / `make_xp_test_case` 二次消费,自动生成 pytest 的 `skip_xp_backends` / `xfail_xp_backends` 标记,实现「文档、测试、运行时分发」三位一体。
- 读懂 `tests/test_support_alternative_backends.py` 如何用参数化把上百个函数 × 多个后端展开成测试用例,并理解 `test_doc` 这条「文档串恰好被改写一次」的契约。

## 2. 前置知识

在进入源码前,先用通俗语言澄清几个本讲反复出现的术语。

- **后端(backend / namespace,记作 `xp`)**:实现 Python Array API 标准的数组库,例如 NumPy、CuPy、PyTorch(`torch`)、JAX(`jax.numpy`)、Dask(`dask.array`)。不同后端能在 GPU、多线程、惰性求值上各展所长,但 API 形态一致,因此 SciPy 才有可能用一套代码分发到它们上。
- **设备(device)**:数组实际存放与计算的物理位置,主要是 CPU 与 GPU。一个后端可能两种设备都支持(如 PyTorch、JAX),也可能只支持一种(如 NumPy 只 CPU、CuPy 只 GPU)。所以「能力」必须是「后端 × 设备」的二维组合,而不是单纯「后端」一维。
- **`SCIPY_ARRAY_API` 开关**:一个环境变量,默认关闭。关闭时 `scipy.special` 里几乎全是裸 NumPy ufunc;开启后,`_FuncInfo` 分发层(见 u10-l1)才会把调用路由到 PyTorch/JAX/CuPy/Dask 原生实现或回退实现。**注意:本讲的 `@xp_capabilities` 与这个开关基本无关——它无论开关与否都会改写文档串、登记能力表**,这是本讲一个关键认知。
- **能力(capability)**:某个函数在「某后端 + 某设备」上是否被支持、是否需要告警。本讲用三态加一态表达:全支持(✅)、不支持(⛔)、支持但有告警(⚠️)、不适用(n/a,如 NumPy 谈 GPU 无意义)。
- **装饰器工厂(decorator factory)**:`@xp_capabilities(...)` 中的括号意味着 `xp_capabilities` 本身是个**返回装饰器**的函数——先调用 `xp_capabilities(cpu_only=True)` 得到一个 `decorator`,再用 `decorator(f)` 装饰函数 `f`。这与无括号的 `@xp_capabilities`(本仓库不这么用)是两种不同写法。
- **pytest 标记(mark)**:pytest 的 `pytest.mark.xxx` 机制,可在收集阶段给测试打标签,常用 `skip`(直接跳过)与 `xfail`(已知失败、允许失败)。Array API 测试套件扩展出 `skip_xp_backends` / `xfail_xp_backends`,专门按后端名跳过/标记失败。

## 3. 本讲源码地图

本讲涉及三个源文件,职责各不相同:

| 文件 | 作用 |
| --- | --- |
| [_logsumexp.py](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_logsumexp.py) | 三个**纯 Python** 函数 `logsumexp`/`softmax`/`log_softmax` 的实现;它们各自挂 `@xp_capabilities()`,是「全后端支持」的样板案例,也是观察「文档串被自动追加矩阵」最直观的入口。 |
| [_support_alternative_backends.py](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py) | u10-l1 讲的 `_FuncInfo` 分发层。本讲只关注其中一处衔接:`_FuncInfo.wrapper` 如何把 `xp_capabilities` 装饰器作用到(可能是裸 ufunc 的)函数上,并用 `assert cap_func is func` 强制「原地修改」契约。 |
| [_array_api.py](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/../_lib/_array_api.py) | `@xp_capabilities` 装饰器**本身的定义**所在(位于 `scipy/_lib/`,不在 `special/` 下)。同时定义了渲染矩阵的 `_XPSphinxCapability` / `_make_sphinx_capabilities` / `_make_capabilities_note`,以及把能力转成测试标记的 `make_xp_pytest_marks` / `make_xp_pytest_param` / `make_xp_test_case`。本讲的「心脏」在这里。 |
| [tests/test_support_alternative_backends.py](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/tests/test_support_alternative_backends.py) | 多后端测试入口。用 `make_xp_pytest_param` 把 `_special_funcs` 展开成参数化测试,并用 `test_doc` 守护「文档串恰好被改写一次」。 |

> 提示:`@xp_capabilities` 不住在 `scipy/special/` 里,而在公共库 `scipy/_lib/_array_api.py`。这意味着它是**全 SciPy 共享**的机制——`scipy.stats`、`scipy.linalg` 等子模块都用同一套装饰器。本讲虽然以 `special` 的函数为样本,但讲透的机制可迁移到整个 SciPy。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开:**(4.1)** 装饰器本身——它做什么、为什么必须原地修改;**(4.2)** 能力矩阵文档化——参数如何变成表格并写进文档串;**(4.3)** 测试标记生成——同一份元数据如何驱动 `skip`/`xfail`。

### 4.1 @xp_capabilities 装饰器:定义与「两件事」

#### 4.1.1 概念说明

`scipy.special` 里绝大多数函数是 ufunc(见 u2-l1),还有像 `logsumexp` 这样的纯 Python 函数(见 u4-l3)。当 `SCIPY_ARRAY_API` 开启时,这些函数要能在 PyTorch/JAX/CuPy/Dask 数组上工作;但现实是,**不是每个函数在每个后端都能跑**——有的只在 CPU 上有实现(没 GPU 内核),有的在某些后端上根本没有原生对应物。

于是需要一个统一的「能力声明」机制,回答两个问题:

1. **给用户看**:「我用 PyTorch GPU 调 `gamma`,能行吗?」——需要一张支持矩阵写进文档。
2. **给测试用**:「这条测试在 CuPy 上跑会崩,得自动跳过。」——需要把声明转成 pytest 跳过标记。

`@xp_capabilities` 就是同时服务这两件事的**单一事实来源(single source of truth)**。它的设计哲学是:**写一遍能力声明,文档与测试都从同一份声明派生**,避免「文档说支持、测试却没覆盖」或反之的割裂。

#### 4.1.2 核心流程

装饰器执行时分三步,可用伪代码描述:

```
调用 xp_capabilities(cpu_only=True, exceptions=["cupy"], ...)
   │
   ├─ 1. 组装 capabilities 字典(把所有参数原样收起来)
   ├─ 2. 立刻计算 sphinx_capabilities(供文档表格用):_make_sphinx_capabilities(**capabilities)
   └─ 3. 返回 decorator(f):
            ├─ a. capabilities_table[f] = capabilities   # 登记,供测试标记读取
            ├─ b. 解析 f.__doc__ 为结构化 FunctionDoc
            ├─ c. 把能力表格作为一段 Notes 追加进文档(除非 np_only)
            ├─ d. 重新渲染并写回 f.__doc__(原地,try/except 兜底老 NumPy)
            └─ e. return f   # 关键:返回的就是原对象,不套壳
```

两个关键不变量:

- **不变量 A(原地修改)**:`decorator` 返回的 `f` 与传入的 `f` 是**同一个对象**。这是为了在被装饰对象是 ufunc 时,不破坏它的 ufunc 身份(套一层 Python 函数会丢掉 `.types`、`out=`、广播等所有 ufunc 能力)。
- **不变量 B(单次登记)**:每个函数只在 `capabilities_table` 里登记一次。文档串改写也只发生一次——这正是 `test_doc` 要守护的契约(见 4.3)。

#### 4.1.3 源码精读

装饰器签名与全部参数定义在此,注意它是个**带关键字参数**的工厂(`*` 之后全是仅关键字参数):

[scipy/_lib/_array_api.py:839-863](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/../_lib/_array_api.py#L839-L863) 定义 `xp_capabilities` 工厂:接受 `cpu_only`/`np_only`/`exceptions`/`skip_backends`/`xfail_backends`/`warnings`/`marray` 等,以及一个仅供自测的 `capabilities_table`(默认指向模块级那张大表)。这些参数没有立即生效,而是被打包存档。

最关键的「两件事」在装饰器内层 `decorator(f)` 里:

[scipy/_lib/_array_api.py:922-938](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/../_lib/_array_api.py#L922-L938) 内层装饰器:第一句 `capabilities_table[f] = capabilities` 把能力登记进全局表(测试标记稍后查这张表);随后用 `FunctionDoc(f)` 把 docstring 解析成 `Parameters`/`Returns`/`Notes` 等结构化段落,在 `doc['Notes']` 末尾追加能力说明,重新渲染后**写回 `f.__doc__`**;最后 `return f`——注意没有 `return wrapper`,这正是「不变量 A:原地修改」的落点。`try/except AttributeError` 是为了兜底「SciPy 编译时所链 NumPy < 2.2 不允许写 ufunc 的 `__doc__`」的旧环境。

模块级那张全局能力表在这里——它是个被「导入时改动一次」的普通字典,被装饰器和测试标记两侧共享:

[scipy/_lib/_array_api.py:1163-1164](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/../_lib/_array_api.py#L1163-L1164) 注释「在多处(导入时)被改一次的字典可以吗?」点明了它的角色:装饰器写入、`make_xp_pytest_marks` 读取,形成「声明—消费」的回路。

那么「原地修改」契约在 `special` 侧是怎么被强制执行的?看 `_FuncInfo.wrapper`:

[scipy/special/_support_alternative_backends.py:121-126](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L121-L126) `wrapper` 取出该函数的装饰器(没有就退化为空参 `xp_capabilities()`),作用到 `func` 上,然后 `assert cap_func is func`。这行断言是**契约的守卫**:它要求装饰器无论如何都必须返回原对象。因为当 `SCIPY_ARRAY_API` 关闭时 `func` 就是裸 ufunc,若装饰器套壳,这里会直接断言失败,从而在开发期就拦住「破坏 ufunc」的错误实现。

注意一个分支:`@xp_capabilities` 的「是否追加文档」受 `np_only` 控制:

[scipy/_lib/_array_api.py:927-929](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/../_lib/_array_api.py#L927-L929) 只有 `not np_only`(或 `out_of_scope`)时才追加能力说明。`np_only=True` 表示「这函数只支持 NumPy」,此时文档里只放一句「不在多后端支持范围内」(见 4.2 的 out_of_scope 分支),而不放完整表格。

#### 4.1.4 代码实践

**实践目标**:亲手验证「不变量 A:原地修改」与「登记进全局表」两件事。

**操作步骤**(示例代码,可直接运行):

```python
# 示例代码
from scipy._lib._array_api import xp_capabilities, xp_capabilities_table

@xp_capabilities(cpu_only=True, exceptions=["cupy"])
def my_demo(x):
    """A toy function.

    Parameters
    ----------
    x : array
        input

    Notes
    -----
    nothing here yet.
    """
    return x

# 1) 验证原地修改:装饰前后是同一个对象
assert my_demo.__name__ == "my_demo"
# 装饰器返回的就是原函数,没有套壳(否则下面会 KeyError)
assert xp_capabilities_table[my_demo]["cpu_only"] is True
assert xp_capabilities_table[my_demo]["exceptions"] == ("cupy",)

# 2) 验证文档串被改写:Notes 段被追加了能力说明
assert "Array API Standard Support" in my_demo.__doc__
print(my_demo.__doc__)
```

**需要观察的现象**:

- 第 1 组断言通过,说明装饰器确实「原地修改」并登记了能力。
- 打印出的 `__doc__` 里,原来的「Notes」段后**多出**了一段 `**Array API Standard Support**`,内含一张「Library / CPU / GPU」表格。

**预期结果**:因为 `cpu_only=True` 且 `exceptions=("cupy",)`,表格里 NumPy、CuPy、PyTorch、JAX、Dask 的 CPU 列基本都是 ✅,而 PyTorch / JAX 的 **GPU 列应是 ⛔**(被 cpu_only 关掉),CuPy 因属例外仍保留 ✅。这正是 4.2 要精读的渲染逻辑。

> 若你在「SciPy 编译时所链 NumPy < 2.2」的旧环境下对一个真 ufunc 做同样实验,`f.__doc__ = doc` 会落入 `except AttributeError` 分支,文档不会被改写——但登记仍会发生。`test_doc` 用 `skipif` 排除了这种旧环境。

#### 4.1.5 小练习与答案

**练习 1**:如果把 `@xp_capabilities` 改成「返回一个 wrapper 函数」的实现,`_FuncInfo.wrapper` 里那行 `assert cap_func is func` 会怎样?为什么这是不可接受的?

**参考答案**:断言会失败(`AssertionError`)。因为在 `SCIPY_ARRAY_API` 关闭时 `func` 是裸 ufunc,套壳会丢失 ufunc 的 `.types`、`out=`、`where=`、广播、`resolve_dtypes` 等一切能力(见 u2-l1),整个 special 模块的性能与契约都会崩。所以「原地修改」不是风格选择,而是 ufunc 兼容性的硬约束。

**练习 2**:`_FuncInfo` 里 `xp_capabilities` 字段为 `None` 时(见 [scipy/special/_support_alternative_backends.py:33-36](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L33-L36)),会怎样?查源码确认。

**参考答案**:`wrapper` 中 `capabilities = self.xp_capabilities or xp_capabilities()` 会退化为「无参」装饰器。无参意味着所有后端、所有设备全支持(矩阵全 ✅),也仍会登记并改写文档。该字段注释明确写道:「省略表示对所有后端完整支持」。

### 4.2 能力矩阵文档化:从参数到表格

#### 4.2.1 概念说明

装饰器收到的参数(`cpu_only`、`exceptions`、`skip_backends`...)是**面向开发者**的「规则」,而文档串里要呈现的是**面向用户**的「结果表格」。这两者之间需要一个渲染层:把「规则」摊开成「6 个后端 × 2 个设备」的具体支持情况。

SciPy 把「后端 × 设备」建模成一个数据类 `_XPSphinxCapability`,每个后端对应一个实例,带 `cpu`/`gpu` 两个布尔(或 `None` 表示「不适用」)和一个 `warnings` 列表。渲染时:

| 取值 | 渲染符号 | 含义 |
| --- | --- | --- |
| `None` | `n/a` | 不适用(如 NumPy 谈 GPU) |
| `False` | ⛔ | 不支持 |
| `True`(无告警) | ✅ | 完整支持 |
| `True`(有告警) | ⚠️ + 文字 | 支持但有注意事项(如 JAX「no JIT」、Dask「computes graph」) |

注意一个容易被忽视的点:`@xp_capabilities()` 无参装饰的 `logsumexp`/`softmax`/`log_softmax` 表示**全后端、全设备完整支持**,所以它们的文档表格应该是「满屏 ✅」(Dask 的 GPU 列因天然不适用仍是 n/a)。这是「全支持」的最干净样板。

#### 4.2.2 核心流程

渲染分两步:

```
xp_capabilities(...) 收到的规则
   │
   ├─ 第一步 _make_sphinx_capabilities(...)
   │     ├─ 先给 6 个后端各发一个「默认能力」:
   │     │     numpy/array_api_strict → cpu=True, gpu=None
   │     │     cupy                    → cpu=None, gpu=True
   │     │     torch                   → cpu=True, gpu=True
   │     │     jax.numpy               → cpu=True, gpu=True(若 jax_jit=False 加告警)
   │     │     dask.array              → cpu=True, gpu=None(若 allow_dask_compute 加告警)
   │     ├─ skip_backends / xfail_backends 命中的后端 → cpu/gpu 置 False
   │     ├─ np_only=True → 除 numpy 与 exceptions 外全置 False
   │     ├─ cpu_only=True → 非 exceptions 后端的 gpu 置 False
   │     └─ warnings 命中的后端 → 在其 warnings 列表追加文字
   │
   └─ 第二步 _make_capabilities_note(fun_name, capabilities)
         把上面 5 个后端(numpy/array_api_strict/cupy/torch/jax.numpy/dask.array)
         渲染成一张 rst 表格(故意不展示 array_api_strict),拼成一段 Notes
```

`out_of_scope=True` 是个特殊捷径:它直接令 `np_only=True`,文档里只放一句「不在多后端支持范围内」,**不放表格**。

#### 4.2.3 源码精读

先看「单格」的渲染器 `_XPSphinxCapability`,它决定了一个「后端 × 设备」格子最终长什么样:

[scipy/_lib/_array_api.py:706-726](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/../_lib/_array_api.py#L706-L726) `_XPSphinxCapability` 数据类:`_render` 把 `None`/`False`/`True+warnings`/`True` 分别映射成 `n/a`/⛔/⚠️/✅,且 `assert len(res) <= 20` 强制告警文字不超过 20 字符(为了表格列宽稳定);`__str__` 把 CPU 与 GPU 两格拼成定宽 20 字符的两列。

再看「规则 → 矩阵」的核心 `_make_sphinx_capabilities`:

[scipy/_lib/_array_api.py:729-760](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/../_lib/_array_api.py#L729-L760) 开头给 6 个后端发默认能力。注意 CuPy 默认 `cpu=None`(它本就是 GPU 库,谈 CPU 是 n/a),NumPy 默认 `gpu=None`(反之),只有 torch/jax 同时 `cpu=True, gpu=True`。`jax_jit=False` 会给 JAX 加「no JIT」告警,`allow_dask_compute=True` 会给 Dask 加「computes graph」告警——这两条是 `xpx.lazy_xp_function` 在测试侧也认得的语义(见 4.3)。

[scipy/_lib/_array_api.py:762-781](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/../_lib/_array_api.py#L762-L781) 三条规则的应用顺序:① `skip_backends`/`xfail_backends` 命中即把对应格置 `False`(⛔);② `np_only` 时除 `exceptions ∪ {numpy}` 外全置 `False`;③ `cpu_only` 时把非 `exceptions` 后端的 `gpu` 置 `False`;④ `warnings` 命中即追加告警文字。`exceptions` 的本质就是「白名单豁免」,让个别后端逃过 `cpu_only`/`np_only` 的连坐。

最后看「矩阵 → 文档段落」的拼接 `_make_capabilities_note`:

[scipy/_lib/_array_api.py:791-836](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/../_lib/_array_api.py#L791-L836) `out_of_scope` 分支只放一句简短说明;正常分支拼出一张 rst 表格,固定 5 行(NumPy/CuPy/PyTorch/JAX/Dask,**故意不展示 `array_api_strict`**——见第 811 行注释),并按需追加 MArray 说明与 `extra_note`。这段文字里那句 `has experimental support for Python Array API Standard compatible backends` 正是 `test_doc` 用来判定「文档是否被改写」的锚点字符串。

现在对照真实样板:`logsumexp`/`softmax`/`log_softmax` 三个函数都用**无参** `@xp_capabilities()`:

[scipy/special/_logsumexp.py:15-16](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_logsumexp.py#L15-L16) `logsumexp` 头上的 `@xp_capabilities()`。无参 = 全规则默认 False = 全后端全设备支持。这三个函数之所以能「全支持」,是因为它们的实现(见 u4-l3)完全用 Array API 标准原语写成,不依赖任何后端特有的特殊函数——这与那些需要「某后端原生实现或回退」的 ufunc 形成对照。

[scipy/special/_logsumexp.py:251-252](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_logsumexp.py#L251-L252) 与 [scipy/special/_logsumexp.py:350-351](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_logsumexp.py#L350-L351) `softmax`、`log_softmax` 同样无参装饰。值得强调:**这三个函数不在 `_special_funcs` 名单里**(它们是归约/跨元素函数,不是逐元素 ufunc,不能走 u10-l1 的 `_FuncInfo` 分发),所以它们是在 `_logsumexp.py` 里**直接**挂 `@xp_capabilities`,而不经 `_FuncInfo.wrapper`。这恰好说明 `@xp_capabilities` 与分发层是**解耦**的——任何函数,无论是否逐元素、是否 ufunc,都能用它来声明并文档化自己的后端支持。

再看一个「受限支持」的真实例子:

[scipy/special/_support_alternative_backends.py:359-359](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L359-L359) `_needs_betainc = xp_capabilities(cpu_only=True, exceptions=["jax.numpy", "cupy"])` 是个**复用**的装饰器实例,用在 `betainc`/`betaincc`/`stdtr` 等需要 `betainc` 内核的函数上。它的语义是「PyTorch 没实现 `betainc`,所以这些函数在非 CuPy/非 JAX 后端上只能 CPU 回退到 NumPy」——`cpu_only=True` 会把 PyTorch 的 GPU 列关成 ⛔,而 CuPy/JAX 因属例外仍保留 GPU ✅。

#### 4.2.4 代码实践

**实践目标**:对比「全支持」与「受限支持」两类函数的文档表格差异,直观感受参数如何驱动矩阵。

**操作步骤**(示例代码):

```python
# 示例代码
from scipy import special

def show_table(doc):
    # 截取文档里 "Array API Standard Support" 那一段
    i = doc.find("**Array API Standard Support**")
    j = doc.find("See :ref:`dev-arrayapi`", i)
    return doc[i:j] if i != -1 and j != -1 else "(未找到能力段落)"

print("=== logsumexp(全后端支持)===")
print(show_table(special.logsumexp.__doc__))

print("\n=== gamma(cpu_only + 例外)===")
print(show_table(special.gamma.__doc__))
```

**需要观察的现象**:

- `logsumexp` 的表格里,CPU/GPU 列几乎都是 ✅(Dask 的 GPU 因天然 n/a 显示 `n/a`)。
- `gamma` 的表格里,**PyTorch 的 GPU 列应是 ⛔**(受 `cpu_only=True` 影响),而 CuPy、JAX 因在 `exceptions` 里仍保留 ✅。

**预期结果**:与 [scipy/special/_support_alternative_backends.py:571-574](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L571-L574) 中 `gamma` 的声明 `xp_capabilities(cpu_only=True, exceptions=["cupy", "jax.numpy"])` 一一对应。

> ⚠️ 待本地验证:具体符号(✅/⛔/⚠️)在不同终端字体下可能显示不全或对齐略有差异;以源码 `_render` 的返回值为准。如果你看到 `gamma` 的文档里**没有**这段表格,多半是运行在「SciPy 编译时所链 NumPy < 2.2」的旧环境(`__doc__` 写回被 `AttributeError` 跳过)——这正是 `test_doc` 用 `skipif` 排除的情形。

#### 4.2.5 小练习与答案

**练习 1**:`@xp_capabilities(out_of_scope=True)` 与 `@xp_capabilities(np_only=True)` 的文档输出有何异同?

**参考答案**:从 [scipy/_lib/_array_api.py:884-885](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/../_lib/_array_api.py#L884-L885) 可知 `out_of_scope` 会令 `np_only=True`,所以两者**测试标记**相同(都只对 NumPy 跑)。但**文档**不同:`out_of_scope` 走 [scipy/_lib/_array_api.py:792-804](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/../_lib/_array_api.py#L792-L804),只放一句「不在多后端支持范围内」;而普通 `np_only` 仍会放完整表格(只是表里除 NumPy 外全是 ⛔)。

**练习 2**:为什么 `_make_sphinx_capabilities` 计算了 6 个后端,文档表格却只展示 5 个?

**参考答案**:见 [scipy/_lib/_array_api.py:811](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/../_lib/_array_api.py#L811-L811) 的注释「故意不文档化 array-api-strict」。`array_api_strict` 是个**合规性校验工具**(只用来在测试里逼出标准违例),不是面向用户的「生产级后端」,所以文档不展示它,但内部仍按它计算能力(供测试标记用)。

### 4.3 SKIP/XFAIL 测试标记:让同一份声明驱动测试

#### 4.3.1 概念说明

光把能力写进文档还不够——文档可能撒谎(开发者改了实现却忘了改声明)。SciPy 的做法是:**用同一份 `capabilities` 字典驱动测试**。具体地,装饰器登记时把原始参数存进 `capabilities_table[func]`,测试侧用 `make_xp_pytest_param` / `make_xp_test_case` 读这张表,自动生成对应的 `skip_xp_backends` / `xfail_xp_backends` pytest 标记。

这样声明与测试天然同步:

- 声明「PyTorch GPU 不支持」(cpu_only) → 测试在 PyTorch GPU 上自动 `skip`。
- 声明「CuPy 行为不同但不算错」(xfail_backends) → 测试在 CuPy 上自动 `xfail`。

此外,装饰器还顺带调用 `xpx.testing.lazy_xp_function(func)`,把 `allow_dask_compute` / `jax_jit` 这两个「惰性后端行为开关」传给 array-api-extra 的测试基础设施,让 Dask/JAX 在测试时按声明的方式被驱动(比如「允许 Dask 实际计算图」「JAX 关闭 JIT」)。

#### 4.3.2 核心流程

测试标记的生成由 `make_xp_pytest_marks` 完成,`make_xp_pytest_param` / `make_xp_test_case` 都是它的薄封装:

```
make_xp_pytest_param(func)
   └─ make_xp_pytest_marks(func)
        ├─ 查 capabilities_table[func] 拿到 capabilities 字典
        ├─ if cpu_only:  追加 skip_xp_backends(cpu_only=True, exceptions=..., reason=...)
        ├─ if np_only:   追加 skip_xp_backends(np_only=True, ...)
        ├─ for (mod, reason) in skip_backends:  追加 skip_xp_backends(mod, reason=reason)
        ├─ for (mod, reason) in xfail_backends: 追加 xfail_xp_backends(mod, reason=reason)
        ├─ lazy_xp_function(func, allow_dask_compute=..., jax_jit=...)
        └─ 追加 uses_xp_capabilities(True, funcs=[func])   # 用于「是否漏标」审计
   └─ 包装成 pytest.param(func, marks=marks, id=func.__name__)
```

`make_xp_test_case(*funcs)` 则把上面这串 marks 折叠成一个装饰器,套在测试函数上;`make_xp_pytest_param` 则返回一个带 marks 的 `pytest.param`,适合塞进 `@pytest.mark.parametrize`。

#### 4.3.3 源码精读

先看标记生成的「主泵」`make_xp_pytest_marks`:

[scipy/_lib/_array_api.py:1126-1154](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/../_lib/_array_api.py#L1126-L1154) 核心逻辑全在这里:`cpu_only` → 一条 `skip_xp_backends(cpu_only=True, exceptions=exceptions, reason=reason)`;`np_only` → 类似;`skip_backends` 与 `xfail_backends` 各自逐条展开;然后 `lazy_xp_function(func, allow_dask_compute=..., jax_jit=...)` 把惰性后端开关也注册进去。注意它支持 `(cls, method_name)` 元组形式,以便给类的方法生成标记(配合装饰器的 `method_capabilities` 参数)。

[scipy/_lib/_array_api.py:1156-1160](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/../_lib/_array_api.py#L1156-L1160) 末尾追加的 `uses_xp_capabilities(True, objs=objs)` 是个**审计标记**:它让 CI 能找出「用了 `xp` fixture 却没声明能力」的测试,防止有人写了多后端测试却绕过声明机制——这是「文档/测试/分发三位一体」的最后一道护栏。

再看两个封装:`make_xp_test_case` 把 marks 折叠成装饰器,`make_xp_pytest_param` 把 marks 包成 `pytest.param`:

[scipy/_lib/_array_api.py:942-1006](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/../_lib/_array_api.py#L942-L1006) `make_xp_test_case` 的文档串给了最直观的对照:手写一串 `@pytest.mark.skip_xp_backends(...)` / `@pytest.mark.xfail_xp_backends(...)` 等价于一句 `@make_xp_test_case(f)`——marks 的内容完全由 `f` 上的 `@xp_capabilities` 决定。

[scipy/_lib/_array_api.py:1009-1080](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/../_lib/_array_api.py#L1009-L1080) `make_xp_pytest_param` 是参数化版本:返回 `pytest.param(func, *args, marks=marks, id=func.__name__)`,可直接塞进 `@pytest.mark.parametrize` 的取值列表。

现在看测试侧怎么消费它们。整模块用一行 `pytestmark` 把所有测试纳入 Array API CI:

[scipy/special/tests/test_support_alternative_backends.py:21-21](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/tests/test_support_alternative_backends.py#L21-L21) `pytestmark = pytest.mark.array_api_backends` 让本模块所有测试(包括没用 `xp` fixture 的那些)都在 Array API CI 矩阵里跑。

主测试函数把「上百个函数」一次性参数化:

[scipy/special/tests/test_support_alternative_backends.py:105-107](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/tests/test_support_alternative_backends.py#L105-L107) `[make_xp_pytest_param(i.wrapper, i) for i in _special_funcs]` 把 `_special_funcs` 里每个 `_FuncInfo` 都包成一个带 marks 的 `pytest.param`,mark 内容由该函数的 `@xp_capabilities` 声明决定。`xp` fixture 由 array-api 测试插件提供,会依次取 NumPy/CuPy/PyTorch/JAX/Dask 等后端;在每个后端上,只有未被 `skip_xp_backends` 跳过的函数才会真正执行。

测试体里还有大量「运行时再决定 skip/xfail」的细粒度逻辑,集中在 `_skip_or_tweak_alternative_backends`:

[scipy/special/tests/test_support_alternative_backends.py:26-97](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/tests/test_support_alternative_backends.py#L26-L97) 这是在 `@xp_capabilities` 声明**之外**的、更细的「函数 × 后端 × dtype」级别调整(如 `betaincinv` 在 CuPy 上 `xfail`、`multigammaln` 直接 `skip`)。它说明:声明层管「后端 × 设备」粗粒度,而函数体内部还能针对具体边界情况补刀——两层互补。

最后看守护「文档恰好被改写一次」的 `test_doc`:

[scipy/special/tests/test_support_alternative_backends.py:336-345](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/tests/test_support_alternative_backends.py#L336-L345) 这条测试用 `skipif` 排除「NumPy < 2.2」旧环境,然后断言每个函数的 `__doc__` 里 `has experimental support for Python Array API` 恰好出现**一次**。它的意义是:既不能「没改写」(声明没生效),也不能「改写多次」(被装饰两次,矩阵重复)——这正是 4.1 的「不变量 B:单次登记」的可执行守卫。

#### 4.3.4 代码实践

**实践目标**:亲眼看一份 `@xp_capabilities` 声明如何被翻译成具体的 pytest 标记。

**操作步骤**(示例代码,纯本地,不依赖 GPU/后端实际安装):

```python
# 示例代码
from scipy._lib._array_api import (
    xp_capabilities, make_xp_pytest_marks,
)

@xp_capabilities(
    cpu_only=True,
    exceptions=["cupy"],
    skip_backends=[("dask.array", "needs eager eval")],
    xfail_backends=[("jax.numpy", "known numerical diff")],
)
def my_fun(x):
    """toy

    Notes
    -----
    base note.
    """
    return x

marks = make_xp_pytest_marks(my_fun)   # 读 capabilities_table[my_fun] 生成 marks
for m in marks:
    # 每个 mark 携带它的名字与参数,可读出「会跳过/标记失败哪些后端」
    print(m.name, m.args, m.kwargs)
```

**需要观察的现象**:

- 输出里应能看到 `skip_xp_backends`(由 `cpu_only=True` 与 `skip_backends` 各产生一条)、`xfail_xp_backends`(由 `xfail_backends` 产生)、以及末尾的 `uses_xp_capabilities` 审计标记。
- `cpu_only` 那条 mark 的 `kwargs` 里应带 `exceptions=("cupy",)` 与 `reason=None`,说明 CuPy 会豁免跳过。

**预期结果**:对照 [scipy/_lib/_array_api.py:1140-1150](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/../_lib/_array_api.py#L1140-L1150) 的源码,`cpu_only` 与 `np_only` 走带关键字参数的 `skip_xp_backends`,而 `skip_backends`/`xfail_backends` 走「位置传后端名 + reason 关键字」的形式,两者生成的 mark 形态不同,但都能被 array-api 测试插件识别。

**源码阅读型补充实践**:在 `tests/test_support_alternative_backends.py` 里数一下 `make_xp_pytest_param` / `make_xp_test_case` 各出现几次,分别用在什么场景(参数化 vs. 整测试套标记),并解释为什么 `test_chdtr_gh21311` 与 `test_mixed_arrays_and_python_scalars` 用 `@make_xp_test_case(special.chdtr)` / `@make_xp_test_case(special.fdtrc)` 而不是 `make_xp_pytest_param`(提示:它们测的是「单个函数的特定边界行为」,不是「一批函数的批量比对」,所以用整函数级标记更自然)。参见 [scipy/special/tests/test_support_alternative_backends.py:387-404](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/tests/test_support_alternative_backends.py#L387-L404)。

#### 4.3.5 小练习与答案

**练习 1**:`make_xp_pytest_marks` 末尾那条 `uses_xp_capabilities(True, objs=...)` 标记,删掉会怎样?

**参考答案**:测试仍能正常跑过,但 CI 会**漏掉审计**——它原本的作用是让 array-api 测试插件能找出「用了 `xp` fixture、却没走 `@xp_capabilities` 声明」的测试(见 [scipy/_lib/_array_api.py:1156-1158](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/../_lib/_array_api.py#L1156-L1158))。删掉后,有人新增一个多后端测试却忘了声明能力,不会被自动发现,声明与测试的同步就出现了缺口。

**练习 2**:`_FuncInfo` 里 `xp_capabilities` 字段为 `None` 的函数(如 `entr`/`erf`/`gammaln`,见 [scipy/special/_support_alternative_backends.py:488](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_support_alternative_backends.py#L488-L488) 一带),`make_xp_pytest_marks` 会生成什么标记?

**参考答案**:几乎不生成 skip/xfail 标记。因为 `wrapper` 把 `None` 退化为无参 `xp_capabilities()`,登记进表的 capabilities 里 `cpu_only`/`np_only`/`skip_backends`/`xfail_backends` 全为空/False,所以 `make_xp_pytest_marks` 的四个 `if`/`for` 全部跳过,只剩 `lazy_xp_function` 与 `uses_xp_capabilities` 两条。这正是「全后端支持 = 测试不跳过任何后端」的体现。

## 5. 综合实践

把三个模块串起来,做一个端到端的小任务:**亲手声明一个函数的能力,并同时验证「文档表格」与「测试标记」两边都正确派生。**

**任务背景**:假设你给 `special` 新增了一个纯 Python 函数 `my_op(x, y)`,它用 Array API 原语写成,但依赖某个 PyTorch 在 GPU 上还没实现的内核,因此「PyTorch GPU 不支持,其余全支持」。

**步骤**:

1. **写声明**:用 `@xp_capabilities(cpu_only=True, exceptions=["cupy", "jax.numpy", "dask.array"])` 装饰 `my_op`(语义:除 CuPy/JAX/Dask 例外外,其余后端——主要是 PyTorch——只能 CPU)。

2. **验证文档侧**(对应 4.2):

   ```python
   # 示例代码
   from scipy._lib._array_api import xp_capabilities
   @xp_capabilities(cpu_only=True, exceptions=["cupy", "jax.numpy", "dask.array"])
   def my_op(x, y):
       """toy op.

       Notes
       -----
       base.
       """
       return x + y
   assert "Array API Standard Support" in my_op.__doc__
   # 解析表格,确认 PyTorch 行的 GPU 列为 ⛔,其余后端 GPU 为 ✅ 或 n/a
   ```

   预期:文档表格里 **PyTorch 的 GPU 列 = ⛔**,CuPy/JAX 的 GPU 列 = ✅(在 exceptions 里,豁免 cpu_only),Dask 的 GPU 列 = n/a(天然不适用),NumPy 的 GPU 列 = n/a。

3. **验证测试侧**(对应 4.3):

   ```python
   # 示例代码
   from scipy._lib._array_api import make_xp_pytest_marks
   marks = make_xp_pytest_marks(my_op)
   names = [m.name for m in marks]
   assert "skip_xp_backends" in names      # 由 cpu_only 派生
   assert "uses_xp_capabilities" in names  # 审计标记
   # 找到那条 skip_xp_backends,确认它的 exceptions 含 cupy/jax.numpy/dask.array
   ```

   预期:`cpu_only` 派生出一条 `skip_xp_backends`,其 `exceptions` 含三个例外后端,意味着测试在「PyTorch GPU」上会被跳过,而在「CuPy GPU / JAX GPU」上照常跑。

4. **反思一致性**:把第 1 步的 `exceptions` 改成空,重跑第 2、3 步,观察文档表格里 CuPy/JAX 的 GPU 列是否也变成 ⛔、测试标记的 `exceptions` 是否也变空——体会「声明改一处,文档与测试同步变」的**单一事实来源**威力。

**验收标准**:文档表格与测试标记在「哪些后端 GPU 被关」上完全一致;若不一致,说明你手写声明时参数写错了——这正是这套机制的价值所在。

> 提示:这是一个「源码阅读 + 离线验证」型任务,不需要真的安装 PyTorch/CuPy/JAX。它验证的是「声明 → 渲染」与「声明 → 标记」两条纯 Python 路径,与 u10-l1 的运行时分发层无关。

## 6. 本讲小结

- `@xp_capabilities` 是 SciPy 全局共享的多后端能力声明机制,定义在 `scipy/_lib/_array_api.py`(不在 `special/` 下),**同时做两件事**:把能力登记进全局表 `xp_capabilities_table`,并把一张「后端 × 设备」矩阵作为 Notes 段落追加进函数文档串。
- 它**必须原地修改**被装饰对象(`return f`,不套壳),因为被装饰的常是 ufunc,套壳会毁掉 ufunc 身份;`_FuncInfo.wrapper` 用 `assert cap_func is func` 强制这条契约。
- 参数(`cpu_only`/`np_only`/`exceptions`/`skip_backends`/`xfail_backends`/`warnings`)经 `_make_sphinx_capabilities` 摊成 6 个后端的 `_XPSphinxCapability`(✅/⛔/⚠️/n/a),再经 `_make_capabilities_note` 拼成 rst 表格注入文档;无参 `@xp_capabilities()`(如 `logsumexp`/`softmax`/`log_softmax`)即「全后端全设备支持」。
- 同一份 `capabilities` 字典被 `make_xp_pytest_marks`/`make_xp_pytest_param`/`make_xp_test_case` 二次消费,自动生成 `skip_xp_backends`/`xfail_xp_backends` 标记,并附带 `lazy_xp_function`(管 Dask/JAX 行为)与 `uses_xp_capabilities`(审计「漏声明」)。
- 「文档、测试、运行时分发」共享同一份声明 = 单一事实来源:`test_doc` 用 `__doc__.count(...) == 1` 守护「文档恰好改写一次」,`uses_xp_capabilities` 守护「测试别绕过声明」,二者共同保证声明与实际不脱节。
- `@xp_capabilities` 与 `_FuncInfo` 分发层(u10-l1)**解耦**:逐元素 ufunc 经 `_FuncInfo.wrapper` 间接挂装饰器;`logsumexp` 等非逐元素函数在 `_logsumexp.py` 里直接挂——任何函数都能用它声明并文档化自己的后端支持。

## 7. 下一步学习建议

- **横向迁移**:本讲的机制是全 SciPy 共享的。建议去读 `scipy.stats` 等子模块里 `xp_capabilities` 的其它调用点,体会「不同子模块如何复用同一份能力声明管线」。
- **深入测试基础设施**:本讲只讲了 `make_xp_pytest_*` 这一层。建议接着读 array-api-extra 的 `lazy_xp_function` 与 `pytest-array-api` 插件(提供 `xp` fixture、`skip_xp_backends`/`xfail_xp_backends` 的实际实现),理解 `allow_dask_compute`/`jax_jit` 在测试时如何驱动惰性后端。
- **回看运行时分发**:把本讲与 u10-l1 对照重读——u10-l1 讲「调用时怎么分发到各后端」,本讲讲「分发能力如何声明、文档化、测试化」,两者拼起来才是 `scipy.special` 多后端支持的全貌。
- **动手扩展**:试着给一个目前 `xp_capabilities=None`(全支持)的函数(如 `entr`)在本地加上 `cpu_only=True` 与一个 `exceptions`,观察文档表格与测试标记如何联动变化,加深「单一事实来源」的直觉。
