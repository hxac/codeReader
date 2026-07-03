# 测试体系与边界情况

## 1. 本讲目标

前几讲我们从「黑盒使用」一路拆到「白盒实现」，已经把 `derivative` / `jacobian` / `hessian` 的算法骨架（输入校验、差分权重、求值点生成、估值与误差、终止判断、eim 框架）逐一讲透。但**源码读得再熟，也不如一行断言说得清「这个函数到底承诺了什么」**——测试就是这份承诺的成文法。本讲我们把镜头对准 `scipy/differentiate/tests/test_differentiate.py`，从测试反推函数的边界行为。读完本讲你应该能够：

1. 理解 differentiate 的**测试组织结构**：一个 `@make_xp_test_case(derivative)` 装饰器如何让同一个测试自动在 NumPy / PyTorch / JAX / CuPy 等多个后端上各跑一遍，并据 `@xp_capabilities` 自动生成跳过/失败标记。
2. 掌握测试覆盖的**三类能力**：状态标志（`test_flags` 一次调用同时产生 4 种 `status`）、精度与收敛（`test_accuracy` / `test_convergence` / `test_step_parameters`）、向量化与 dtype。
3. 学会从**特殊边界用例反推函数行为**：整数不会被传入 `f`、标量 `args` 自动包成元组、非法步长返回 `status=-3`、以及真导数恰为零的鞍点（`test_saddle_gh18811`）为何必须显式设 `atol` 才收敛。

> 本讲承接 **u2-l5（check_termination）** 与 **u3-l2（hessian）**：u2-l5 解释了 5 个状态码各自的来源，本讲用 `test_flags` 把它们「同时复现」出来；u3-l2 解释了 hessian 的实现，本讲的 `TestHessian` 部分则会检验它的 `nfev` 记账与 `rtol` 钳位告警。建议先读完 u2 系列与 u3-l2 再进入本讲。

## 2. 前置知识

### 2.1 状态码速查（来自 u2-l5 / u1-l3）

回顾 `derivative` 返回对象里的 `status` 字段，共 6 种取值。它们的数值定义集中在 `scipy/_lib/_elementwise_iterative_method.py` 顶部：

| 常量 | 值 | 含义 | 由谁设置 |
|------|----|------|----------|
| `_ECONVERGED` | `0` | 收敛 | `check_termination` |
| `_EERRORINCREASE` | `-1` | 误差回升（步长太小触发消去误差） | `check_termination` |
| `_ECONVERR` | `-2` | 触达 `maxiter` 仍未收敛 | `eim._loop` 兜底 |
| `_EVALUEERR` | `-3` | 出现非有限值（`NaN`/`inf`） | `check_termination` |
| `_ECALLBACK` | `-4` | 被 `callback` 主动叫停 | `eim._loop` 兜底 |
| `_EINPROGRESS` | `1` | 进行中（迭代未结束时的临时值） | `eim._loop` 初始化 |

注意 `_EERRORINCREASE` 这个名字是 differentiate 自己起的别名：在通用框架里它叫 `_ESIGNERR`（值也是 `-1`），但「符号错误」这个名字对求导场景不直观，于是 [_differentiate.py:9](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L9) 重新定义了一个语义更贴切的别名供本子包使用。

### 2.2 「断言即契约」

一条 `assert` 或 `xp_assert_close` 不是「验证我想对了」，而是「**规定**函数必须如此」。例如 `assert res.nit == 2` 不是在描述「恰好跑了 2 轮」，而是在**钉死**：「对一个二次多项式，`derivative` 必须在第 2 轮就报告收敛」。以后改算法的人若让它变成 3 轮，这条测试就会红——这就是「测试反推行为」的意义：你不必读实现，读断言就知道承诺。

### 2.3 跨后端测试的 `xp` 夹具（fixture）

本测试文件里几乎每个测试方法都带一个 `xp` 参数（例如 `def test_flags(self, xp):`）。这个 `xp` 不是函数里定义的，而是由 `array_api_extra` 的 `lazy_xp_function` 注入的**测试夹具**：它在每次测试运行时被替换成某个后端的命名空间（`numpy`、`torch`、`jax.numpy`……），于是同一个测试函数体会被「参数化」成多次运行，每次 `xp` 是不同后端。这就是为什么测试里几乎不出现裸 `np.`，而全是 `xp.`。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`scipy/differentiate/tests/test_differentiate.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py) | 本讲主角。三个测试类 `TestDerivative` / `TestJacobian` / `TestHessian`，覆盖状态标志、精度、向量化、dtype、输入校验、边界用例、鞍点 |
| [`scipy/_lib/_array_api.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/../_lib/_array_api.py) | 定义 `make_xp_test_case`：读取函数上的 `@xp_capabilities` 装饰器，自动生成 `skip_xp_backends` / `xfail_xp_backends` 标记并注入 `xp` 夹具 |
| [`scipy/_lib/_elementwise_iterative_method.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/../_lib/_elementwise_iterative_method.py) | 状态码常量（`_ECONVERGED` 等）与 `_loop` 主循环的兜底终止逻辑（`-2` / `-4`） |
| [`scipy/differentiate/_differentiate.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py) | `check_termination`（设 `0` / `-1` / `-3`）、`@xp_capabilities` 装饰（声明跳过哪些后端） |

---

## 4. 核心概念与源码讲解

### 4.1 测试结构与跨后端机制

#### 4.1.1 概念说明

`test_differentiate.py` 的顶层结构非常简洁——三个测试类，每个类上方挂一个装饰器：

```python
@make_xp_test_case(derivative)
class TestDerivative:
    ...
```

这个 [`@make_xp_test_case(derivative)`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L17) 是整个跨后端测试体系的「总开关」。它的作用不是改写测试逻辑，而是给类里**每个测试方法**自动叠上两类东西：

1. **后端选择标记**：据 `derivative` 上 `@xp_capabilities` 声明的「支持/跳过哪些后端」，自动生成 `@pytest.mark.skip_xp_backends(...)` 与 `@pytest.mark.xfail_xp_backends(...)`。于是「该测哪些后端」这一信息**只在 `derivative` 定义处声明一次**，测试侧零重复。
2. **`xp` 夹具注入**：给每个测试方法打上 `lazy_xp_function` 标记，使方法签名里的 `xp` 参数在运行时被填充为当前后端的命名空间。

这正是 u4-l4 讲过的 `@xp_capabilities` 装饰器的**第二个用途**——除了给 docstring 注入支持矩阵表，它还驱动测试标记的自动生成。

#### 4.1.2 核心流程

`make_xp_test_case` 的执行流程可以画成：

```text
@make_xp_test_case(derivative)
        │
        ▼
读取 derivative.__dict__ 上的 xp_capabilities 元信息
（skip_backends=[('array_api_strict', ...), ('dask.array', ...)], jax_jit=False）
        │
        ▼
make_xp_pytest_marks(derivative)  →  生成一组 pytest.mark 对象
        │
        ▼
对被装饰的测试函数 / 测试类，reduce 叠加所有 mark
        │
        ▼
lazy_xp_function 包装：注册 xp 夹具
        │
        ▼
pytest 收集时：对每个 (后端 × 测试方法) 组合生成一个测试用例
（跳过 array_api_strict / dask.array，因为 derivative 不支持它们）
```

关键点是**声明与使用的解耦**：`derivative` 在 [_differentiate.py:65-66](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L65-L66) 声明自己跳过 `array_api_strict` 与 `dask.array`（根因是 `xpx.at` 依赖的花式/布尔索引赋值在这两个后端上缺失，详见 u4-l4），测试侧无需重复写「不要在 dask 上跑 derivative」——装饰器自动替你跳过。

#### 4.1.3 源码精读

先看测试文件的导入，它点出了本讲涉及的所有「积木」：

[test_differentiate.py:6-13](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L6-L13) 引入了三组关键工具——

- `eim`：状态码常量的来源（`_ECONVERGED` / `_ECONVERR` / `_EVALUEERR` / `_EINPROGRESS`）。
- `make_xp_test_case` / `is_numpy` / `is_torch`：跨后端测试装饰器与后端判定小工具。
- `_EERRORINCREASE`：从 `_differentiate` 直接导入的别名（值 `-1`），测试里用它构造期望的状态数组。

再看 `make_xp_test_case` 本体的核心两行：

[_array_api.py:1005-1006](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/../_lib/_array_api.py#L1005-L1006) 先调用 `make_xp_pytest_marks(*funcs)` 算出该叠哪些标记，再用 `functools.reduce` 把这些标记「层层包裹」到被装饰函数上。注意它返回的是一个 `lambda`——一个**装饰器工厂**，所以 `@make_xp_test_case(derivative)` 这种「带括号」的写法是对的：先调用 `make_xp_test_case(derivative)` 得到装饰器，再用这个装饰器去包 `TestDerivative`。

最后看状态码常量的「老家」：

[_elementwise_iterative_method.py:21-27](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/../_lib/_elementwise_iterative_method.py#L21-L27) 集中定义了所有状态码。负值表示各类失败/中止，`0` 表示收敛，`1` 表示进行中。测试里 `ref_flags` 数组正是用这些常量拼出来的，**用常量名而不是魔数 `-2`**，既防笔误又自文档化。

#### 4.1.4 代码实践

**实践目标**：亲眼看见「一个测试方法被复制成了多个后端的用例」。

**操作步骤**：

1. 进入 SciPy 源码根目录，确认已装好测试依赖（`pytest`、`array_api_strict`、`torch` 可选）。
2. 用 `-k test_basic` 只跑最简单的 `test_basic`，并加 `--co`（collect-only）只收集不运行：

```bash
cd /path/to/scipy
python -m pytest scipy/differentiate/tests/test_differentiate.py -k test_basic --co -q
```

**需要观察的现象**：收集结果里会出现多条形如 `test_basic[xp-numpy]`、`test_basic[xp-torch-cpu]`、`test_basic[xp-jax-cpu]` 的用例——同一个方法被参数化成了「每后端一条」。若你装了 `array_api_strict`，**不会**出现 `[xp-array_api_strict]`，因为它在 `derivative` 的 `skip_backends` 里。

**预期结果**：NumPy 后端必然出现；其它后端视你本地安装而定。这就是 `@make_xp_test_case` 的全部魔法——**一份测试代码，N 份后端用例**。

> 若本地未配置多后端环境，至少能看到 `[xp-numpy]` 一条，现象同样成立。

#### 4.1.5 小练习与答案

**练习 1**：`TestJacobian` 与 `TestHessian` 上方的装饰器分别是 `@make_xp_test_case(jacobian)` 和 `@make_xp_test_case(hessian)`。为什么不能三者共用一个 `@make_xp_test_case(derivative)`？

**参考答案**：因为「跳过哪些后端」是**每个函数各自声明**的。虽然 `jacobian`/`hessian` 内部复用 `derivative`，但它们各自有独立的 `@xp_capabilities` 装饰器（见 [_differentiate.py:721-722](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L721-L722) 与 [951-952](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L951-L952)），未来某天 hessian 可能支持一个 derivative 不支持的后端。装饰器必须传「被测函数本身」才能读到正确的元信息。

**练习 2**：测试类 `JacobianHessianTest`（[test_differentiate.py:456](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L456)）**没有**挂 `@make_xp_test_case`，但 `TestJacobian(JacobianHessianTest)` 和 `TestHessian(JacobianHessianTest)` 都挂了。这个基类为什么不需要装饰器？

**参考答案**：`JacobianHessianTest` 是一个**只被继承、不被 pytest 直接收集**的共享基类（名字不以 `Test` 开头的方法 pytest 不会当测试跑，且它存放的是 `test_iv` 这种 jacobian/hessian 共用的输入校验逻辑）。跨后端标记是给「真正会被运行的测试类」叠的，所以只放在两个具体子类上。基类若也挂装饰器，会被 pytest 当成独立测试类重复收集。

---

### 4.2 状态标志与精度测试

#### 4.2.1 概念说明

这一组测试回答两个核心问题：

1. **「一次调用能不能同时产生多种结局？」**——`test_flags` 的设计哲学。`derivative` 是逐元素的，不同元素可以独立收敛或失败。一个好测试应当**在一个数组里同时塞进四种命运**，验证它们各自拿到正确的 `status`，互不干扰。这是对 eim 框架「元素压缩」（u2-l6）的最强压力测试：若压缩逻辑有 bug，某个元素的 `status` 会错位。
2. **「精度承诺对得上参数吗？」**——`test_accuracy` / `test_convergence` / `test_step_parameters` 分别检验：跨几十种连续分布的导数精度、`atol`/`rtol` 放严后误差真的变小、`initial_step`/`step_factor` 对精度的方向性影响。

#### 4.2.2 核心流程

`test_flags` 构造一个「四元素四命运」的函数 `f`，关键在于它**按元素选择不同的子函数**：

```text
元素 j=0 → f(x)=x-2.5          （线性，立即收敛）           → 期望 status=0   (_ECONVERGED)
元素 j=1 → f(x)=exp(x)*随机数   （随机噪声 → 误差回升）       → 期望 status=-1  (_EERRORINCREASE)
元素 j=2 → f(x)=exp(x)          （order=2 太低 → 跑满 maxiter）→ 期望 status=-2  (_ECONVERR)
元素 j=3 → f(x)=NaN             （非有限值）                  → 期望 status=-3  (_EVALUEERR)
```

注意第 0 个元素「立即收敛」靠的是 `order=2` 的中心差分对**线性函数**精确成立（一阶导为常数，两轮内 `error` 即降到容差以下）；第 2 个元素「跑满 maxiter」靠的是 `order=2` 精度太低、`exp` 的曲率让误差降不到 `rtol=1e-14`。四种命运**恰好覆盖了 `derivative` 自己能设置的三种状态（0/-1/-3）加上框架兜底的一种（-2）**。

#### 4.2.3 源码精读

[test_differentiate.py:94-117](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L94-L117) 是 `test_flags` 全文。三个细节值得品味：

第一，[第 100-103 行](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L100-L103) 用一个列表把四种命运写成四个 lambda，再用 `js`（元素下标）去**按位置挑**：`funcs[int(j)](x)`。`f` 的签名是 `f(xs, js)`，`js` 通过 `args` 传进来，于是每个元素拿到自己的「剧本」。

第二，[第 108-111 行](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L108-L111) 把 `order=2` 与 `tolerances=dict(rtol=1e-14)` 设得「恰到好处」：`order=2` 让第 2 个元素无法快速收敛，极严的 `rtol` 让第 2 个元素确实撑到 `maxiter`。这两个参数不是随便选的，是**为了让四种命运同时成立**精心调出来的。

第三，[第 113-117 行](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L113-L117) 用常量名拼期望数组，最后 `xp_assert_equal(res.status, ref_flags)` 一次性断言四个元素的状态全部正确。**一条断言验四种结局**，这就是「逐元素独立性」的最强证据。

与 `test_flags` 形成对照的是 [test_flags_preserve_shape（第 119-137 行）](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L119-L137)：它把「按元素挑剧本」改写成「一次性返回 4 维向量」+ `preserve_shape=True`（u4-l1）。两版**期望完全相同的 `ref_flags`**，注释明说「Same test as above but using `preserve_shape` option to simplify」——这其实在告诉我们：`preserve_shape=True` 是处理向量值函数的**更简洁**写法，而 `args` 传下标是「绕过」向量值限制的老办法。

精度侧，[test_accuracy（第 35-42 行）](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L35-L42) 用 `stats._distr_params.distcont`（SciPy 所有连续分布的参数表）参数化，对每个分布把 `derivative(dist.cdf, x)` 与解析 `dist.pdf(x)` 对照，`atol=1e-10`。这是一张**覆盖几十种函数形态**的精度大网——`exp`、`sin` 之类太平凡，真实世界的 CDF 才有各种奇形怪状的曲率。

而 [test_convergence（第 151-174 行）](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L151-L174) 直接钉死「容差收紧 → 误差变小」的**单调性契约**：[第 165 行](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L165) `assert abs(res2.df - ref) < abs(res1.df - ref)` 断言 `atol=1e-6` 的结果比 `atol=1e-3` 更准。它不验证「误差小于多少」，而验证「**调参方向对**」——这是更稳健的契约形式。

#### 4.2.4 代码实践

**实践目标**：亲手复现 `test_flags` 的「四命运同时出现」，并验证其中**任意一个**的因果关系。

**操作步骤**：

1. 写一个小脚本（**示例代码**，非项目原有文件）：

```python
# demo_flags.py —— 示例代码
import numpy as np
from scipy.differentiate import derivative
import scipy._lib._elementwise_iterative_method as eim
from scipy.differentiate._differentiate import _EERRORINCREASE

rng = np.random.default_rng(5651219684984213)

def f(xs, js):
    funcs = [lambda x: x - 2.5,              # 收敛
             lambda x: np.exp(x)*rng.random(),  # 误差回升
             lambda x: np.exp(x),             # 跑满 maxiter
             lambda x: np.full_like(x, np.nan)]  # NaN
    res = [funcs[int(j)](x) for x, j in zip(xs, js)]
    return np.stack(res)

args = (np.arange(4, dtype=np.int64),)
res = derivative(f, np.ones(4), tolerances=dict(rtol=1e-14),
                 order=2, args=args)
print("status:", res.status)
print("期望:  ", np.array([0, -1, -2, -3]))
print("success:", res.success)
```

2. 运行 `python demo_flags.py`。

**需要观察的现象**：打印的 `status` 应为 `[0, -1, -2, -3]`，`success` 为 `[True, False, False, False]`。

**预期结果**：四个元素同一次调用、四种结局，与源码 `ref_flags` 完全一致。把 `order=2` 改成 `order=8` 再跑——第 2 个元素（`exp`）很可能变成 `0`（收敛），因为它精度足够了。这反向印证了「第 2 个元素的 `-2` 是被 `order=2` 的低精度逼出来的」。

#### 4.2.5 小练习与答案

**练习 1**：`test_flags` 里第 3 个元素返回 `NaN`，期望 `status=-3`。但 u2-l5 讲过，非有限值检查带 `nit > 0` 守卫，**首轮不会触发**。那么这个元素最早在第几轮被判 `-3`？

**参考答案**：最早第 2 轮（`nit=1` 时进入 `if work.nit > 0` 分支）。首轮 `nit=0`，守卫挡住检查（否则首轮所有元素的初始 `df=NaN` 会全员误判 `-3`，见 [_differentiate.py:570](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L570)）；从第 2 轮起 `nit>0`，该元素每次求值都是 `NaN`，`isfinite(df)` 为假，于是判 `-3` 并停。所以「NaN 元素最快也要两轮才能停」。

**练习 2**：`test_convergence` 用 `order=4` 而不是默认 `order=8`，为什么这个选择让测试更稳健？

**参考答案**：测试关心的是「容差收紧→误差变小」的**单调性**，而非绝对精度。`order=4` 仍能在 `maxiter` 内把误差压到 `atol=1e-6` 以下，又不至于像 `order=8` 那样「一上来就太准」——后者会让 `atol=1e-3` 与 `atol=1e-6` 两次结果几乎一样（都早已收敛到机器精度），单调性断言变得没有区分度、甚至因浮点抖动而脆弱。选 `order=4` 是为了让两次调用落在「仍在主动收敛」的区间，单调性才显眼。

---

### 4.3 特殊边界与鞍点用例

#### 4.3.1 概念说明

`test_special_cases` 是整个测试文件里**信息密度最高**的一段——它把好几个「不起眼但容易踩坑」的边界行为塞进一个测试。而 `test_saddle_gh18811` 则用一个 `@pytest.mark.xfail` 标记了一个**至今未完美解决**的已知缺陷。这两个测试最能体现「从测试反推边界行为」的读法。

要理解的几条边界承诺：

1. **整数不会传给 `f`**：`derivative` 内部（eim 的 `_initialize`）会把整数输入提升为浮点，保证 `f` 永远收到浮点数组。否则 `x ** 99` 在整数上会溢出。
2. **标量 `args` 自动包成元组**：传 `args=3` 而非 `args=(3,)` 也能工作（u2-l1 的柔性处理）。
3. **非法步长 → `status=-3`**：`step_direction=NaN` 或 `initial_step=0` 会让求值点退化，函数值变 `NaN`，于是 `-3`。
4. **多项式的精确收敛轮数**：对 `n` 次多项式，`order≥n` 时理论上 1 轮即可精确，但实测 `nit==2`——多出的 1 轮是「为了用 error 估计确认收敛」。
5. **鞍点（真导数为 0）默认不收敛**：`rtol·|df|` 在 `df→0` 时趋于 0，判据过严，必须显式给 `atol`。

#### 4.3.2 核心流程

`test_saddle_gh18811` 的核心矛盾可以写成一条数学不等式。收敛判据是（u2-l5）：

\[
\text{error} < \text{atol} + \text{rtol}\cdot|df|
\]

当真导数恰为 0（如 \(f(x)=(x-1)^3\) 在 \(x=1\) 处，\(f'(1)=0\)），数值估计 \(df\) 在迭代中趋于 0 但永远含噪声，于是右端 \(\text{rtol}\cdot|df|\to 0\)。此时除非 \(\text{error}\) 也精确到 0（浮点下不可能），否则判据**永不满足**，元素一路跑到 `maxiter` 拿到 `status=-2`。解法是显式设一个非零的 \(\text{atol}\)（如 `1e-16`），给右端一个「地板」，让趋于 0 的 `error` 能踩到它。

但 `test_saddle_gh18811` 标了 `@pytest.mark.xfail`：即便给了 `atol=1e-16`，**在某些场景下仍不稳定**（`1e-16` 太接近机器精度，浮点抖动可能让 `error` 始终略高于地板）。所以它是一个「期望失败」的测试——记录「我们知道这儿还有问题」，而非「我们解决了问题」。

#### 4.3.3 源码精读

先看 [test_special_cases:373-424](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L373-L424) 的三段边界。

**整数不传入 `f`**——[第 376-385 行](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L376-L385)：`f` 内部第一句就是 `assert xp.isdtype(x.dtype, 'real floating')`，传入整数 `7` 却能通过这个断言，证明 eim 在调用 `f` 前已把整数提升为浮点。注释「otherwise this would overflow」点明动机：`x ** 99` 在 `int64` 下对 `x=7` 是天文数字溢出，提升为浮点才安全。`res.df` 与解析值 `99*7.**98` 对照（注意 `7.` 是浮点，绕过整数溢出）。

**非法步长 → `-3`**——[第 387-394 行](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L387-L394)：两种非法输入——`step_direction=NaN`（方向不确定，求值点位置无效）与 `initial_step=0`（步长为零，求值点全塌缩到 `x` 本身）。两者都让差分模板退化、函数值无法构成有效差分，于是 `df=NaN`、`status=-3`。注意它**不抛异常**而是返回带 `-3` 的结果——这与「非法输入报 `ValueError`」（如 `maxiter=0`）是两类不同处理：语义上「能算但无意义」走 `-3`，「参数本身非法」走异常。

**标量 `args`**——[第 419-424 行](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L419-L424)：`args=xp.asarray(3)` 是个裸数组而非元组，`f(x, c)` 仍能正确解包，`res.df` 恰为 `c=3`（因为 `f=c*x-1` 的导数就是 `c`）。这验证了 u2-l1 讲的「不可迭代的 `args` 被包成元组」的柔性处理。

**多项式的收敛轮数**——[第 403-417 行](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L403-L417)：对 `n∈[0,5]` 次、`order=max(1,n)` 的多项式，断言 `res.nit == 2`。注释（[第 396-401 行](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L396-L401)）说透了原因：理想情况下 `order` 阶差分对 `n≤order` 次多项式 1 轮即精确，但「`derivative` 需要多一轮来基于 error 估计检测收敛」——因为首轮 `error` 是 `NaN`（u2-l4），必须跑到第 2 轮才有真实 `error` 可与容差比。这是「误差估计机制」对「最少迭代数」的硬性下探。

再看 [test_saddle_gh18811:426-440](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L426-L440) 的三处要害：

第一，[第 427-428 行](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L427-L428) 叠了 `@pytest.mark.skip_xp_backends(np_only=True)` 与 `@pytest.mark.xfail` 两层标记。`np_only=True` 表示「只在 NumPy 后端跑」（跨后端不必复现这个已知缺陷）；`xfail` 表示「预期它会失败」——若哪天它意外通过了（`XPASS`），pytest 会按配置提醒，说明缺陷被无意修好了。

第二，[第 429-432 行](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L429-L432) 两个用例：`(x-1)**3` 在 `x=1`（三阶鞍点，导数恰 0）和分段函数 `np.where(x>1, (x-1)**5, (x-1)**3)` 在 `x=1`（导数也恰 0，且左右高阶行为不同）。

第三，[第 437-440 行](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L437-L440) 用 `step_direction=[-1,0,1]` 同时在三个方向上求导、`atol=1e-16` 给地板、断言 `res.success` 全真且 `df` 接近 0。注释（[第 434-436 行](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L434-L436)）点题：「默认设置下 `derivative` 在真导数为 0 处不一定收敛，显式 `atol` 能缓解」——「缓解」而非「根治」，这正是 `xfail` 的由来。

最后提一句 `TestHessian` 里那条 [test_small_rtol_warning（第 698-703 行）](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L698-L703)：它用 `pytest.warns(RuntimeWarning)` 断言 `hessian(sin, [1.], rtol=1e-15)` **会发警告**——这正是 u3-l2 讲的「内层 rtol 钳位到 `100·eps`」机制的可观测证据：用户给的 `rtol` 太小，hessian 主动钳位并警告。

#### 4.3.4 代码实践

**实践目标**：把 `test_special_cases` 里的两条断言改写成**新的函数场景**，验证你理解了它们背后的承诺；并解释 `test_saddle_gh18811` 为何需要显式 `atol`。

**操作步骤**：

1. 在 `scipy/differentiate/tests/` 下新建一个临时测试文件 `test_my_edge.py`（**示例代码**，验证完可删）：

```python
# 示例代码
import numpy as np
import pytest
from scipy.differentiate import derivative

# 改写自 test_special_cases 的「标量 args」断言：
# 换成 f(x)=a*sin(x)+b，验证标量 args=(a,b 形式) 仍能解包
def test_scalar_args_rewritten():
    def f(x, a, b):
        return a * np.sin(x) + b
    # 故意不把 args 包成标准元组形式：传两个标量的元组
    res = derivative(f, np.asarray(1.0), args=(2.0, 5.0))
    # f'(x) = a*cos(x) = 2*cos(1)
    np.testing.assert_allclose(res.df, 2.0 * np.cos(1.0))

# 改写自 test_special_cases 的「非法步长 → status=-3」断言：
# 换成负的 initial_step（步长应为非负），观察是否也得到 -3
def test_bad_initial_step_rewritten():
    res = derivative(np.exp, np.asarray(1.0), initial_step=-1.0)
    # 负步长使求值点退化，期望 status=-3
    assert int(res.status) == -3
```

2. 运行：

```bash
python -m pytest scipy/differentiate/tests/test_my_edge.py -v
```

**需要观察的现象**：两条测试是否通过。特别留意 `test_bad_initial_step_rewritten`——若 `initial_step=-1` 被 `_derivative_iv` 当成「非法参数」直接抛 `ValueError`（而非返回 `-3`），那说明负步长属于「参数非法」而非「步长退化」，与 `initial_step=0` 的处理不同，此时应把断言改成 `pytest.raises(ValueError)`。

**预期结果**：`test_scalar_args_rewritten` 应通过（`args=(2.0, 5.0)` 正常解包）。第二条的结果**待本地验证**——它正是这道练习要你厘清的边界：`initial_step=0` 走 `-3`（见源码第 392-394 行），而 `initial_step=-1` 是否同路，需要你跑了才知道，并据此修正断言形式。

3. **解释 `test_saddle_gh18811` 为何需要显式 `atol`**：在脚本里加上

```python
# 示例代码：观察鞍点默认不收敛
res_default = derivative(lambda x: (x-1)**3, 1.0)
res_atol    = derivative(lambda x: (x-1)**3, 1.0, atol=1e-12)
print("默认 status:", res_default.status, "df:", res_default.df)
print("带atol status:", res_atol.status, "df:", res_atol.df)
```

**需要观察的现象**：默认调用 `status` 多半是 `-2`（跑满 `maxiter` 未收敛），`df` 接近 0 但 `success=False`；加了 `atol=1e-12` 后 `status=0`、`success=True`。

**预期结果 / 解释**：真导数 \(f'(1)=0\)，收敛判据右端 \(\text{rtol}\cdot|df|\) 随 `df→0` 而趋于 0，默认 `atol` 又极小（`smallest_normal`），判据几乎要求 `error` 严格为 0，浮点下达不到，于是 `-2`。显式 `atol` 在右端铺一块「地板」，让趋于 0 的 `error` 踩上去即判收敛。`test_saddle_gh18811` 用极紧的 `atol=1e-16`（接近机器精度）仍不稳定，所以标 `xfail`——「地板太薄，浮点抖动会踩空」。

#### 4.3.5 小练习与答案

**练习 1**：`test_special_cases` 里「整数不传入 `f`」用 `x=7`、`f(x)=x**99-1`。如果 eim **没有**把整数提升为浮点，这条测试会在哪一步、以什么方式失败？

**参考答案**：会在 `f` 内部的 `assert xp.isdtype(x.dtype, 'real floating')` 处直接 `AssertionError`（`int64` 不是 real floating）。即便去掉这个断言，`x ** 99` 在 `int64` 下 `7**99` 远超 `int64` 上限会溢出成一个错误的定值，最终 `xp_assert_close(res.df, 99*7.**98)` 因数值不符而失败。两个征兆都指向「整数必须被提升」。

**练习 2**：`test_saddle_gh18811` 用 `step_direction=[-1, 0, 1]` 同时在三个方向求导。为什么鞍点测试要特意覆盖三个方向，而不是只用中心差分 `0`？

**参考答案**：因为 `(x-1)**3` 在 `x=1` 处的高阶行为左右不对称（三阶项符号单一，但误差估计在不同方向上路径不同），且第二个分段用例 `np.where(x>1, (x-1)**5, (x-1)**3)` 在 `x=1` 处**左右高阶导数不同**（左 3 次、右 5 次）。三个方向（向后 `-1`、中心 `0`、向前 `1`）分别触及不同的 stencil，能更全面地暴露「真导数为 0 时各方向是否都能收敛」的问题。这也呼应 u4-l2：边界/不对称点往往需要显式指定 `step_direction`。

**练习 3**：`test_small_rtol_warning` 用 `pytest.warns(RuntimeWarning)` 而不是 `pytest.raises`。这说明 hessian 对「过小 rtol」采取的是**纠错+告警**而非**报错**策略。结合 u3-l2，说清楚告警之后 rtol 实际被改成了什么值。

**参考答案**：hessian 为抑制嵌套差分的误差传播，要求内层 `rtol` 比外层紧 100 倍（`rtol/100`）；当用户给的 `rtol`（如 `1e-15`）已经小于 `100·eps`（`eps≈2.2e-16`，`100·eps≈2.2e-14`）时，`rtol/100` 会小到无意义，于是 hessian 把内层 rtol **钳位**到 `100·eps` 并发 `RuntimeWarning` 告知用户「你给的 rtol 太小，我已上调」。所以告警之后内层实际用的 rtol 是 `≈2.2e-14`，而非用户给的 `1e-15`。

---

## 5. 综合实践

把本讲三块内容串成一个「**给 differentiate 写一条新测试**」的小任务。

**任务**：仿照 `test_flags` 的「四命运同框」思路，写一个新测试 `test_three_backends_flags`，验证 `jacobian`（而非 `derivative`）也能在一次调用里对**不同输出分量**产生不同 `status`。

**建议步骤**：

1. 构造一个 \(f:\mathbb{R}^2\to\mathbb{R}^2\) 的函数，使它的两个输出分量分别「容易收敛」与「故意不收敛」。例如：
   - 分量 0：\(f_0(x)=x_0\)（线性，雅可比恒为 1，必收敛）。
   - 分量 1：\(f_1(x)=\text{exp}(x_1)\cdot\text{rng.random}()\)（带随机噪声，误差回升）。
2. 用 `preserve_shape` 风格（参考 `test_flags_preserve_shape`）把两个分量 stack 成返回值。
3. 调用 `jacobian(f, x)`，断言 `res.status` 形状为 `(2, 2)`，且其中包含 `0` 与 `-1` 两种值。
4. 用 `python -m pytest` 运行你的测试，确认通过。

**验收要点**：

- 你能说清楚 `res.status` 为什么是 `(2,2)`（u3-l1：雅可比每个元素 \(\partial f_i/\partial x_j\) 独立求导）。
- 你能指出哪两个元素对应「收敛」、哪两个对应「误差回升」，并与 `test_flags` 的元素级独立性对照。
- 你用到了本讲的「断言即契约」思维：测试不是在描述实现，而是在**规定** jacobian 必须支持逐元素独立状态。

> 这个任务没有标准答案文件，目的是让你把「测试结构（4.1）」「状态标志（4.2）」「jacobian 逐元素（u3-l1）」三者融会贯通。

## 6. 本讲小结

- **跨后端测试靠一个装饰器**：`@make_xp_test_case(derivative)` 读取函数上的 `@xp_capabilities`，自动生成 `skip_xp_backends`/`xfail_xp_backends` 标记并注入 `xp` 夹具，于是「支持哪些后端」只在一处声明，测试侧零重复。
- **`test_flags` 是逐元素独立性的最强证据**：一次调用、四个元素、四种命运（`0`/`-1`/`-2`/`-3`），恰好覆盖 `derivative` 自设的三种状态加框架兜底的一种；`preserve_shape` 版本说明它是处理向量值函数的更简洁写法。
- **精度测试钉的是「方向」而非「绝对值」**：`test_convergence` 断言「容差收紧→误差变小」的单调性，`test_accuracy` 用几十种连续分布织成精度大网，都比写死一个阈值更稳健。
- **边界承诺藏在 `test_special_cases` 里**：整数不传入 `f`（防溢出）、标量 `args` 自动包元组、非法步长走 `-3` 而非异常、`n` 次多项式 `order≥n` 时 `nit==2`（多出的一轮是为了用 error 估计确认收敛）。
- **鞍点是已知缺陷、用 `xfail` 诚实记录**：真导数为 0 时 `rtol·|df|→0` 使判据过严，显式 `atol` 给「地板」可缓解，但 `atol=1e-16` 太接近机器精度仍不稳定，故 `test_saddle_gh18811` 标 `xfail`。
- **「断言即契约」是读测试的正确姿势**：一条 `assert res.nit == 2` 规定了函数必须如此，而非描述它恰好如此；从断言反推边界行为，比读实现更快摸清函数承诺。

## 7. 下一步学习建议

本讲是 differentiate 子包学习路线的**收尾篇**。建议你接下来：

1. **横向对比同框架的其它子包**：`scipy.differentiate` 复用的 `eim._loop` 框架（u2-l6）也被 `scipy.optimize._chandrupatla`（求根/求最小值）、`scipy.integrate._tanhsinh`（积分）等使用。去读它们的 `test_*.py`，你会发现同样有「四命运 `test_flags`」「`make_xp_test_case` 跨后端」结构——掌握一个，就掌握了一类。建议从 `scipy/integrate/tests/test_tanhsinh.py` 开始。
2. **亲手扩展测试矩阵**：挑一个本讲没覆盖的组合（例如 `order=1` 单侧差分 + 大 `|x|` 调大 `initial_step`，呼应 u4-l3），仿照综合实践的写法补一条测试，提交到上游——这是把「读懂」变成「能贡献」的最短路径。
3. **回到 docstring 的支持矩阵**：u4-l4 讲过 `@xp_capabilities` 会给 docstring 注入后端支持表。现在你可以打开 `help(scipy.differentiate.derivative)`，对照本讲 4.1 的 `skip_backends`，亲眼看见那张表与测试跳过逻辑是**同源**的——这是「声明驱动一切」的最佳注脚。
