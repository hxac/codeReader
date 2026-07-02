# 逐元素迭代框架 eim._loop 与 _initialize

## 1. 本讲目标

前几讲我们逐个剖析了 `derivative` 的「专用零件」：输入校验 `_derivative_iv`、权重计算 `_derivative_weights`、求值点生成 `pre_func_eval`、估值更新 `post_func_eval`、终止裁判 `check_termination`。但把这些零件串成一台会自动运转的机器的「主轴」，并不是 `derivative` 自己写的——它来自一个共享的通用框架 `scipy._lib._elementwise_iterative_method`（下文简称 **eim**）。

本讲要回答三个问题：

1. `derivative` 进入主循环前，`eim._initialize` 为它做了哪些「数据标准化」（广播、dtype、展平、首次试调用）？
2. `eim._loop` 这根主轴如何通过一组回调钩子（hook）把专用零件装配起来、并驱动每一轮迭代？
3. 当某些元素提前收敛后，框架对 `work` 数组做的「压缩」为什么不会弄丢这些已收敛元素的结果？

学完本讲，你将能读懂任何基于 eim 框架的 SciPy 函数（如 `scipy.optimize._chandrupatla`、`scipy.integrate.tanhsinh`），并能解释 `res_work_pairs` 这一「接线表」的作用。

## 2. 前置知识

本讲假设你已经掌握 u1-l1 ~ u2-l5，尤其是：

- **逐元素向量化**：`f` 接受数组、返回同形状数组，框架对每个元素独立迭代。
- **`_RichResult`**：eim 框架使用的「类字典 + 点号属性」对象，既是跨轮状态容器 `work`，也是最终返回结果 `res`。
- **状态码分工**：`0`（收敛）、`-1`（误差回升）、`-3`（非有限值）由 `derivative` 的 `check_termination` 设置；而 `-2`（触达 `maxiter`）和 `-4`（callback 叫停）正是由本讲的 `eim._loop` 在循环末尾兜底设置的——这是本讲要揭开的关键悬念。
- **`work` 是跨轮状态**：步长 `h`、历史函数值 `fs`、当前估计 `df` 等都挂在 `work` 上代代相传。

本讲会用到两个不熟悉的术语，先解释：

- **钩子（hook / 回调）**：框架在主循环的固定位置「留好接口」，由具体算法（如 `derivative`）填入函数。框架负责「何时调用」，算法负责「做什么」。
- **压缩（compress）**：把数组中已经不需要再计算的元素删掉，只保留仍在迭代的「活跃」元素，从而减少后续运算量。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `scipy/_lib/_elementwise_iterative_method.py` | 通用逐元素迭代框架，定义 `_initialize`、`_loop`、`_check_termination`、`_update_active`、`_prepare_result` |
| `scipy/differentiate/_differentiate.py` | `derivative` 的专用实现：调用 `_initialize`、构造 `work`、定义四个钩子、调用 `_loop` |

> 说明：`scipy/_lib/_elementwise_iterative_method.py` 不在 `differentiate` 目录下，故其永久链接使用 `scipy/_lib/` 前缀；`_differentiate.py` 使用本讲义规定的 `scipy/differentiate/` 前缀。

## 4. 核心概念与源码讲解

### 4.1 为什么要有一个共享框架

在进入源码前，先建立直觉。SciPy 里有一大批函数长得惊人地相似：

- 都接收一个「逐元素可调用」`f` 和横坐标 `x`；
- 都对 `x` 的**每个元素**独立地跑一个迭代算法；
- 都支持用户 `callback` 钩子、都返回带 `nit/nfev/status/success` 的富结果对象；
- 都需要在「某些元素已收敛、另一些还没」时做向量化优化。

不同的只是「这一轮要算什么」（求根？求极值？求导？求积分？）。于是 SciPy 把**所有相同的部分**抽进 eim 框架，把**不同的部分**留给具体算法用钩子填写。文件开头的注释列出了复用者：

[_elementwise_iterative_method.py:1-13](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L1-L13) —— 注释说明本框架服务于标量求根、标量极小化、数值微分、求根括号、极小化括号、数值积分等一整族函数。

这种「控制反转（Inversion of Control）」的设计：**框架掌握主循环，算法只填空**。

### 4.2 模块一：`_initialize` 的广播与 dtype

#### 4.2.1 概念说明

`_initialize` 是主循环前的「数据标准化车间」。`derivative` 的主流程在校验完输入（`_derivative_iv`）后、构造 `work` 前，第一件事就是调用它：

[_differentiate.py:396-398](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L396-L398) —— `derivative` 调用 `eim._initialize(func, (x,), args, kwargs=kwargs, preserve_shape=preserve_shape)`。

它要解决四个问题：

1. **统一 dtype**：`x` 可能是整数、`f` 可能返回另一种浮点，迭代中要混用它们，必须先求出公共结果 dtype。
2. **广播对齐**：`x`、`args`、（可能的）`kwargs` 形状各异，要广播到同一形状。
3. **首次试调用做校验**：用标准化后的 `x` 调用一次 `f`，确认输出形状合法、能跑通。
4. **全部展平成 1D**：框架用整数索引 `active` 管理元素，所以内部一律按展平后的 1D 数组运算，原始形状记下来留到最后还原。

#### 4.2.2 核心流程

`_initialize` 的执行步骤（伪代码）：

```
输入: func, xs(横坐标元组), args, kwargs, preserve_shape, xp
1. nx = len(xs)                       # 记住有几个横坐标数组
2. xp = array_namespace(*xs)          # 推断数组后端(numpy/torch/jax)
3. 若给了 kwargs: 把 func 包一层,把 kwargs 从 args 末尾拆出来
4. xat = result_type(*xs, force_floating=True)   # 横坐标dtype(整数升float)
5. xs,args = broadcast_arrays(*xs, *args)        # 一起广播
6. xs = [asarray(x, dtype=xat) ...]; fs = [func(x,*args) ...]   # 试调用
7. shape = xs[0].shape
8. 若 preserve_shape: 包一层 func, 并用 broadcast_shapes 定 shape
9. 校验 fs[i].shape == shape, 否则抛 ValueError
10. xfat = result_type(*[f.dtype]+[xat])          # 最终公共dtype
11. 校验 xfat 是实浮点(complex_ok=False 时)
12. 把 xs, fs, args 都 copy 并转成 xfat
13. 把 xs, fs, args 全部 reshape 成 1D
14. 返回 func, xs, fs, args, shape, xfat, xp
```

注意第 6 步：**框架会真正调用一次 `f`**。这正是 `derivative` 里 `nfev` 初值为 1 的原因（`nit, nfev = 0, 1`），这次「校验性求值」算了一次函数调用。

#### 4.2.3 源码精读

**dtype 推断与广播**（关键三行）：

[_elementwise_iterative_method.py:97-101](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L97-L101) —— `xat` 用 `force_floating=True` 把整数横坐标升级为浮点（避免整数运算溢出）；`broadcast_arrays` 把 `xs` 和 `args` 一起广播；随后用 `xat` 重塑 `xs` 并**试调用** `func` 得到 `fs`。注释说明了为何坚持先把参数升级为浮点。

**求最终公共 dtype 并校验实数性**：

[_elementwise_iterative_method.py:125-129](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L125-L129) —— `xfat` 由「所有 `f` 输出 dtype + 横坐标 dtype」共同决定；`derivative` 没传 `complex_ok`（默认 `False`），所以若 `f` 返回复数会在此抛 `ValueError("Abscissae and function output must be real numbers.")`。

**全部展平成 1D**：

[_elementwise_iterative_method.py:131-136](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L131-L136) —— 框架为了能对元素做整数索引（`active = arange(n_elements)`），把 `xs/fs/args` 一律 `reshape(-1)`；原始 `shape` 单独返回，留给 `_prepare_result` 最后还原。

> **回到 `derivative`**：调用后 `dtype`（即 `xfat`）被用来计算默认容差 `atol = finfo.smallest_normal`、`rtol = finfo.eps**0.5`（见 [_differentiate.py:400-402](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L400-L402)）。这也解释了 u2-l1 的结论：默认容差不在校验层给出，而延后到这里依赖 dtype 计算。

#### 4.2.4 代码实践

**实践目标**：亲手观察 `_initialize` 的「展平 + 试调用」行为，并验证默认容差随 dtype 变化。

**操作步骤**：

1. 编写脚本，直接调用框架内部函数（示例代码，仅供学习，不属于公开 API）：

```python
# 示例代码：仅用于理解框架行为，非公开 API
import numpy as np
import scipy._lib._elementwise_iterative_method as eim

f = np.exp
x = np.array([[1, 2], [3, 4]])        # 2D 横坐标
args = ()
func, xs, fs, args, shape, dtype, xp = eim._initialize(
    f, (x,), args, preserve_shape=False)
print("原始 shape =", shape)          # (2, 2)
print("xs[0].ndim   =", xs[0].ndim)   # 1  —— 已被展平
print("dtype        =", dtype)        # float64
print("fs[0]        =", fs[0])        # exp 在各点的值，1D
```

2. 再做一个 dtype 演示：

```python
# 示例代码：观察整数横坐标被升级、复数输出被拒绝
_, xs, _, _, _, dt, _ = eim._initialize(np.exp, (np.array([1,2,3]),), ())
print("整数横坐标 -> dtype =", dt)     # float64

try:
    eim._initialize(lambda x: x+0j, (np.array([1.0]),), ())
except ValueError as e:
    print("复数被拒:", e)
```

**需要观察的现象**：

- 即便 `x` 是 2D，返回的 `xs[0]` 是 1D（长度 4），`shape` 仍是 `(2,2)`。
- 整数 `x` 经过 `_initialize` 后 dtype 变成 `float64`。
- 返回复数的 `f` 触发 `ValueError`。

**预期结果**：上述三条均成立。如本地无法导入内部模块，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`_initialize` 为什么要对 `xs`/`fs`/`args` 都 `reshape(-1)`？
**答案**：框架用一维整数索引数组 `active` 来标记「仍在迭代的元素」，并对 `work` 数组做布尔切片压缩。这些操作都依赖一维布局，所以内部统一展平，原始多维形状记在 `shape` 里，由 `_prepare_result` 在最后还原。

**练习 2**：`derivative` 不允许复数输入，是 `_derivative_iv` 还是 `_initialize` 拦下的？
**答案**：是 `_initialize`。`_derivative_iv` 只做「合法性 + 形状」校验，不区分实复；`_initialize` 在求出 `xfat` 后用 `complex_ok=False`（`derivative` 未传该参数，取默认值）判断 `xfat` 是否为实浮点，复数在此被 `ValueError` 拦截。

---

### 4.3 模块二：`_loop` 主循环与钩子

#### 4.3.1 概念说明

`_loop` 是整台机器的「主轴」。它接收一个装满跨轮状态的 `work` 对象、一组**钩子函数**、以及一张**接线表** `res_work_pairs`，然后按固定节奏循环：

```
每一轮:  pre_func_eval  →  func  →  post_func_eval  →  check_termination
                                (其间更新 nit/nfev、触发 callback)
```

`derivative` 把 u2-l3/u2-l4/u2-l5 讲过的三个函数分别填进 `pre_func_eval`、`post_func_eval`、`check_termination` 这三个钩子位，主轴便自动运转。`_loop` 还额外提供 `post_termination_check`（终止后、本轮结束前的扫尾钩子）和 `customize_result`（结果对象的自定义改写钩子），`derivative` 这两个都填成「什么都不做」（`return` / 原样返回 `shape`）。

#### 4.3.2 核心流程

`_loop` 的完整控制流（伪代码）：

```
1. 建结果对象 res:
     n_elements = prod(shape)
     active = arange(n_elements)            # 全部元素一开始都活跃
     res = {每个 pair 的左项: zeros(n_elements), 
            success: zeros(bool), status: 全填 _EINPROGRESS(=1),
            nit: 0, nfev: 0}
     work.args = args
2. 进循环前先 check_termination 一次   (处理「第 0 轮」的预检)
3. 若有 callback: 先准备临时结果并调用一次
4. while work.nit < maxiter and 活跃元素数>0 and 未被callback叫停:
     a. x = pre_func_eval(work)            # 钩子①: 算本轮求值点
     b. 若 args 维度 < x 维度: 给 args 补尾部 1 维以广播
     c. 若 preserve_shape: 把 x reshape 成 (shape + (n,))
     d. f = func(x, *work.args)            # 真正求值
     e. work.nfev += 1 (若 x 是 1D) 或 x.shape[-1] (多维)
     f. post_func_eval(x, f, work)         # 钩子②: 估 df 与 error
     g. work.nit += 1
     h. check_termination(含压缩)          # 钩子③: 判停 + 写 res + 压缩 work
     i. 若有 callback: 准备临时结果并调用; 若叫停则 break
     j. 若无活跃元素: break
     k. post_termination_check(work)       # 钩子④: 扫尾
5. 循环结束后: 给仍活跃的元素兜底赋 status
     = _ECALLBACK(-4) 若是 callback 叫停
     = _ECONVERR(-2) 若是单纯触达 maxiter
6. return _prepare_result(...)             # 组装并还原形状
```

两个关键设计点：

- **结果对象 `res` 一开始就是满尺寸的**（长度 `n_elements`），它的角色是「最终结果的累加器」，而 `work` 才是被压缩的对象。这是模块三的核心。
- **`-2` 和 `-4` 在循环外赋值**：只要某元素从未在任何一轮的 `check_termination` 里被标停，它就一路活到循环结束，然后在第 5 步被统一打上 `-2`（或 `-4`）。这正是 u1-l3 中「`-2`/`-4` 由框架兜底」的实现来源。

#### 4.3.3 源码精读

**`_loop` 的签名与参数语义**：

[_elementwise_iterative_method.py:139-191](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L139-L191) —— 注意 `work` 的 docstring 明确要求它「必须含 `nit`、`nfev`、`success`」，并警告：**`work` 里的数组都会被压缩，不想被压缩的数据要嵌进另一个对象（如 `dict`/`_RichResult`）**。这正是 u2-l2 把权重缓存放进 `diff_state`（一个 `_RichResult`）的原因——框架遍历 `work.items()` 时只会对「直接的 ndarray 属性」压缩，嵌套对象安然无恙。

**初始化结果对象与首次终止检查**：

[_elementwise_iterative_method.py:217-229](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L217-L229) —— `res_dict` 为每个 `res_work_pairs` 项建一个满尺寸零数组；`status` 初值填 `_EINPROGRESS`（即 `1`，仅在 `callback` 里可见的「进行中」码）；进 `while` 之前先跑一次 `_check_termination`，让第 0 轮的预检（如非有限值）有机会在循环前就标记元素。

**主循环体（钩子依次执行）**：

[_elementwise_iterative_method.py:237-276](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L237-L276) —— 这是整个框架的心脏。逐行对应伪代码 4.a–4.k：`pre_func_eval` 出 `x`、按需给 `args` 补维、`func` 求值并累加 `nfev`、`post_func_eval` 更新 `df/error`、`nit += 1`、`_check_termination` 判停并压缩、callback、`post_termination_check`。其中 `work.nfev += 1 if x.ndim == 1 else x.shape[-1]` 这一行体现了嵌套 stencil 的省调用思想：多维求值（一次算 `n` 个新点）时 `nfev` 加的是点数而非 1。

**循环外的兜底状态赋值与返回**：

[_elementwise_iterative_method.py:278-280](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L278-L280) —— `work.status` 被整体设为 `_ECALLBACK`（`-4`）或 `_ECONVERR`（`-2`）。注意这里用的是 `xpx.at(work.status)[:].set(...)`：因为某些后端（如 torch）数组不可变，必须用 `array_api_extra` 的 `.at` 接口完成「全量索引赋值」。随后 `_prepare_result` 把 `work` 的活跃元素抄进 `res` 并还原形状。

#### 4.3.4 代码实践

**实践目标**：用 `callback` 把每一轮的内部状态「拍快照」，直观看到 `_loop` 的迭代节奏与钩子顺序。

**操作步骤**：

```python
import numpy as np
from scipy.differentiate import derivative

f = np.exp
x = np.array([1.0, 2.0, 3.0])
snaps = []
def cb(res):
    # callback 在每轮 post_func_eval + check_termination 之后被调用
    snaps.append((int(res.nit[0]), int(res.nfev[0]),
                  float(res.df[0]), float(res.error[0])))

res = derivative(f, x, callback=cb, tolerances=dict(atol=0, rtol=0),
                 maxiter=4)
for nit, nfev, df, err in snaps:
    print(f"nit={nit} nfev={nfev} df={df:.10f} error={err:.3e}")
print("最终 status =", res.status, " success =", res.success)
```

**需要观察的现象**：

- `nit` 每轮 +1，`nfev` 的增量在首轮较大（首轮新增 `order` 个点），之后每轮只 +2（嵌套 stencil 每轮只补 2 个新点）。
- `error` 随迭代先下降。
- 因为设了 `atol=rtol=0`，永远无法满足 `error < atol+rtol*|df|`，最终 `status` 全为 `-2`（触达 `maxiter`），`success` 全 `False`——这正是 `_loop` 第 5 步兜底赋的 `-2`。

**预期结果**：`status` 数组全为 `-2`，`success` 全为 `False`，`nit` 全为 4。具体 `df`/`error` 数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_loop` 在进入 `while` 之前要先调用一次 `_check_termination`？
**答案**：这是「第 0 轮预检」。某些终止条件（如输入本身就是非有限值）不必等到第一轮求值后才被发现；进循环前先判一次，能让这类元素立刻被标停并写入 `res`，避免无谓计算。对 `derivative` 而言，`check_termination` 里的非有限值分支带 `nit > 0` 守卫，所以这步预检主要服务于其它算法。

**练习 2**：`post_termination_check` 在 `derivative` 里是空的，那它存在的意义是什么？
**答案**：它是框架留给「终止判定之后、本轮结束之前」的扫尾钩子。不同算法可能需要在确认某些元素停机后更新全局量（例如重新组织下一步的搜索区间）。`derivative` 不需要，故填成 `return`；但同框架下的求根/极小化算法会用它做实质性工作。这正是「框架留接口、算法按需填」的体现。

---

### 4.4 模块三：work 压缩与结果组装

#### 4.4.1 概念说明

逐元素迭代的最大性能优势在于：**不同元素的收敛速度不同，先收敛的不必陪着后收敛的一起算**。eim 用「压缩」实现这一点——一旦某元素被判停，它就从 `work` 的所有数组里被「删掉」，后续轮次的向量运算规模越来越小。

但这带来一个直觉上的担忧：**元素被删了，它的结果会不会丢？** 答案是：不会。因为结果在压缩**之前**就已经被抄进了一个满尺寸的、从不压缩的累加器 `res`。本模块要讲清楚三件事：压缩在哪里发生（`_check_termination`）、抄录在哪里发生（`_update_active`）、最终还原在哪里发生（`_prepare_result`）。

#### 4.4.2 核心流程

三个内部函数的协作（伪代码）：

```
_check_termination(work, res, active, ...):           # 每轮调用
    stop = check_termination(work)                    # 钩子③: 哪些元素该停
    if any(stop):
        _update_active(work, res, active, stop, ...)  # 先把停机元素抄进 res
        proceed = ~stop
        active = active[proceed]                      # 活跃索引收缩
        if not preserve_shape:
            for key,val in work.items():              # 压缩 work 的每个数组
                if key in {'args','n'}: continue
                work[key] = val[proceed]
            work.args = [arg[proceed] for arg in work.args]
    return active

_update_active(work, res, res_work_pairs, active, mask):
    update_dict = {res属性: work属性  for (res属性, work属性) in res_work_pairs}
    update_dict['success'] = (work.status == 0)       # success 自动派生
    # 用 active(原始位置) 把 work 的值写进 res 的对应位置
    res[key][active[mask]] = val[mask]

_prepare_result(...):                                 # 循环结束后调用
    res = res.copy()
    _update_active(..., mask=None)                    # 把最后仍活跃的元素也抄进去
    shape = customize_result(res, shape)
    把 res 每个属性 reshape 回原始 shape
    res['_order_keys'] = ['success'] + [左项 for (左项,_) in pairs]
    return _RichResult(**res)
```

关键洞见：`res` 始终是长度 `n_elements` 的满数组，`active` 数组存的是**原始一维下标**。所以无论 `work` 被压缩到多短，`_update_active` 写 `res` 时用的都是原始下标 `active[...]`，结果各归各位，绝不丢失或错位。

#### 4.4.3 源码精读

**`_check_termination`：判停 → 抄录 → 压缩**：

[_elementwise_iterative_method.py:283-310](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L283-L310) —— 顺序至关重要：先 `_update_active`（把停机元素的 `df/error/status/...` 写进满尺寸 `res`），**再**压缩 `work`。压缩时跳过 `'args'` 和 `'n'` 两个键（注释提到 `continued_fraction` 会 hack `n`），并对每个数组用 `val[proceed]` 切片。注意第 307 行的守卫 `getattr(val, 'ndim', 0) > 0`：标量属性（如 `work.fac`）不参与压缩。

**`_update_active`：把 work 抄进 res（核心答疑点）**：

[_elementwise_iterative_method.py:313-338](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L313-L338) —— 第 318 行 `update_dict = {key1: work[key2] for key1, key2 in res_work_pairs}` 就是按接线表把 `work` 属性映射到 `res` 属性；第 319 行额外派生 `success = (work.status == 0)`。非 `preserve_shape` 分支（第 330-333 行）里，`active_mask = active[mask]` 取的是**原始下标**，于是 `res[key][active_mask] = val[mask]` 把压缩后的 `work` 值精准写回 `res` 的原始位置——这就是「压缩后仍不影响最终结果」的根本机制。

**`_prepare_result`：最后一次抄录 + 还原形状 + 排定打印顺序**：

[_elementwise_iterative_method.py:341-357](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L341-L357) —— 先 `res.copy()` 避免污染内部状态；用 `mask=None` 再调一次 `_update_active`，把循环结束时**仍活跃**（即被判 `-2`/`-4`）的元素也抄进 `res`；`customize_result` 让算法有机会改写结果（`derivative` 原样返回 `shape`）；最后把每个属性 `reshape` 回 `shape`，并用 `_order_keys` 决定 `_RichResult` 的打印顺序——这个顺序正是接线表 `res_work_pairs` 的左项顺序。

**`derivative` 的接线表**：

[_differentiate.py:446-447](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L446-L447) —— `res_work_pairs = [('status','status'), ('df','df'), ('error','error'), ('nit','nit'), ('nfev','nfev'), ('x','x')]`。也就是说 `res.status←work.status`、`res.df←work.df`、`res.error←work.error`、`res.nit←work.nit`、`res.nfev←work.nfev`、`res.x←work.x`；`success` 由框架自动派生（`status==0`）并自动排在最前。注释里「the mapping is trivial」指的是左右同名。这张表同时也决定了 `_RichResult` 打印时的属性排列（u1-l3 讲过的 `_order_keys`）。

> **与 u2-l2 的呼应**：`work.diff_state` 不在接线表里，所以它永远不会被抄进 `res`，也因为它嵌在 `_RichResult` 里而不会被压缩——双重保护了这块全局共享的权重缓存。

#### 4.4.4 代码实践（本讲指定实践任务）

**实践目标**：对照 `res_work_pairs` 弄清 `derivative` 的 work→res 映射，并通过阅读 `_update_active` 解释「压缩不丢结果」。分两步。

**第一步：映射对照**

阅读 [_differentiate.py:446-447](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L446-L447) 的 `res_work_pairs`，填出下表（答案已给出，请自行核对）：

| `res` 属性（结果） | `work` 属性（来源） | 说明 |
|---|---|---|
| `success` | （派生）`work.status == 0` | 框架自动加，不在表里 |
| `status` | `work.status` | 终止状态码 |
| `df` | `work.df` | 导数估计 |
| `error` | `work.error` | 误差估计 |
| `nit` | `work.nit` | 迭代轮数 |
| `nfev` | `work.nfev` | 函数求值次数 |
| `x` | `work.x` | 求导点（广播后） |

注意 `work` 里还有 `fs`、`h`、`fac`、`hdir`、`il/ic/ir/io`、`df_last`、`error_last`、`atol`、`rtol`、`dtype`、`terms`、`diff_state` 等——它们都是**内部状态**，不在接线表中，因此不会出现在最终 `res` 里。

**第二步：解释「压缩后为何不影响最终结果」**

阅读 [_elementwise_iterative_method.py:290-308](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L290-L308) 与 [_elementwise_iterative_method.py:330-333](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L330-L333)，用自己的话写一段解释。参考答案要点：

1. `res` 在 `_loop` 开头就被建成长度 `n_elements` 的**满数组**，且全程从不被压缩。
2. 每次 `_check_termination` 的顺序是「先抄录、后压缩」：先 `_update_active` 把停机元素的值按**原始下标** `active[mask]` 写进 `res`，然后才压缩 `work`。
3. `active` 数组保存的是元素在**原始一维布局**中的下标，因此无论 `work` 被压缩到多短，写回 `res` 时定位的都是原始位置，不会错位。
4. 循环结束时 `_prepare_result` 再用 `mask=None` 调一次 `_update_active`，把最后仍活跃的元素（`-2`/`-4`）也补齐，最后 `reshape` 回原始 `shape`。

**验证脚本**（用 `callback` 观察压缩带来的省调用效应）：

```python
import numpy as np
from scipy.differentiate import derivative

# 不同频率的正弦，收敛速度不同：高频的需要更多迭代
def f(x, c):
    return np.sin(c * x)

c = np.array([1.0, 5.0, 10.0, 20.0])
res = derivative(f, 0.0, args=(c,))
print("nfev =", res.nfev)   # 预期：低频的 nfev 小，高频的 nfev 大
print("success =", res.success)
```

**需要观察的现象**：`res.nfev` 各元素互不相同（如 `[11, 13, 15, 17]` 之类），证明低频元素先收敛、被压缩出 `work` 后不再参与后续求值，而高频元素继续迭代。这正是 `_differentiate.py` docstring 里 `res.nfev` 示例的来源（见 [_differentiate.py:336-350](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L336-L350)）。

**预期结果**：四个元素的 `nfev` 单调递增，`success` 全为 `True`。具体数值待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：假如把 `derivative` 的 `res_work_pairs` 里 `('df', 'df')` 改成 `('df', 'error')`，会发生什么？
**答案**：那么 `res.df` 将从 `work.error` 抄录，最终结果的 `df` 属性里装的其实是误差估计，而真正的导数估计 `work.df` 因不在接线表中不会出现在 `res` 里。这说明接线表是 work→res 的**唯一**数据通道，改它就改了结果语义；同时 `_order_keys` 也会随之改变 `df` 的打印位置。

**练习 2**：为什么 `work.args` 和 `work.n` 在压缩循环里被显式跳过？
**答案**：`args` 是单独用列表推导 `[arg[proceed] for arg in work.args]` 压缩的（因为它是数组列表而非单个数组），所以在通用循环里跳过；`n` 被跳过是因为同框架下的 `continued_fraction` 算法会把 `n` 当作非数组的逻辑计数器「hack」使用，直接切片会破坏其语义。这是框架为兼容多个算法留下的特例。

**练习 3**：`_prepare_result` 为什么要先 `res.copy()`？
**答案**：`_loop` 内部那个 `res` 是跨轮复用的累加器，每次 `callback` 和最终返回都要调用 `_prepare_result`。若不 copy，`customize_result` 与 `reshape` 会就地改写内部 `res`，污染后续迭代所依赖的状态。copy 保证了「对外暴露的快照」与「内部累加器」互不影响。

## 5. 综合实践

**任务：用回调绘制「活跃元素数随迭代下降」的曲线，把本讲三个模块串起来。**

要求：

1. 取一个收敛速度差异明显的向量化函数，例如：
   ```python
   def f(x, c):
       return np.sin(c * x)
   c = np.array([1., 5., 10., 20., 40., 80.])
   ```
2. 给 `derivative` 传 `callback`，在回调里**间接**感知「当前还有多少元素活跃」。由于 `callback` 收到的 `res` 是满尺寸的（已由 `_prepare_result` 还原），你可以用 `np.sum(res.status == 1)`（`_EINPROGRESS`）统计仍在进行的元素数。
3. 收集每轮的「活跃元素数」「总 nfev」，绘制活跃元素数随 `nit` 下降的曲线。
4. 在报告里结合本讲源码解释：为什么活跃元素数会单调下降？下降的那部分元素的结果去了哪里？（答：被 `_update_active` 抄进了满尺寸 `res`，随后从 `work` 压缩掉。）

**进阶**：把 `tolerances=dict(atol=0, rtol=0)` 加上，让所有元素都跑到 `maxiter`，观察此时活跃元素数是否始终等于元素总数（因为没人能提前收敛，压缩不发生）。这能反向验证「压缩只发生在有元素判停时」。

> 提示：若本地不便绘图，可只打印每轮的 `(nit, 活跃数, 总nfev)` 序列，同样能完成分析。

## 6. 本讲小结

- **eim 是控制反转的共享框架**：它掌握主循环、计数、callback、结果组装与逐元素压缩；具体算法（`derivative`）只通过钩子填入「这一轮算什么」。
- **`_initialize` 做四件事**：统一 dtype（整数升浮点）、广播 `xs`/`args`、首次试调用 `f` 做校验、全部展平成 1D（原始 `shape` 留待还原）。`derivative` 的默认 `atol`/`rtol` 就依赖它返回的 `dtype`。
- **`_loop` 的钩子顺序**：每轮 `pre_func_eval → func → post_func_eval → check_termination`，辅以 `post_termination_check` 与 `customize_result` 两个扫尾/改写钩子（`derivative` 均填空）。
- **`-2`/`-4` 由框架兜底**：从未被 `check_termination` 标停的元素一路活到循环结束，在循环外被 `_loop` 统一赋 `-2`（触达 maxiter）或 `-4`（callback 叫停）。
- **压缩不丢结果**：`res` 是从不压缩的满尺寸累加器，`active` 保存原始下标；`_check_termination` 的顺序是「先 `_update_active` 抄录、后压缩 `work`」，故停机元素在压缩前已各归各位地写入 `res`。
- **`res_work_pairs` 是唯一接线表**：它既定义 work→res 的属性映射（`success` 自动派生），又决定 `_RichResult` 的打印顺序；`derivative` 的映射左右同名且平凡。

## 7. 下一步学习建议

至此，`derivative` 从黑盒到白盒的全链路（校验 → 标准化 → 权重 → 求值点 → 估值 → 判停 → 框架驱动）已经补齐。建议接下来：

1. **u3-l1 / u3-l2（jacobian / hessian）**：看 `jacobian` 如何用 `wrapped` + `preserve_shape=True` 复用 `derivative`，以及 `hessian` 如何嵌套两层 `jacobian`。你会再次用到本讲的 `preserve_shape` 分支与 `res_work_pairs` 知识。
2. **u4-l1（向量化与 preserve_shape）**：深入 `preserve_shape` 两种模式下 `f` 的形状契约，本讲提到的「`x` 被 reshape 成 `(shape + (n,))`」「args 补维」会在那里完整展开。
3. **u4-l4（Array API 后端）**：本讲反复出现的 `xpx.at(...)`、`array_namespace`、`xp_size` 等抽象，在那里系统讲解跨 NumPy/Torch/JAX 后端的实现技巧。
4. **横向阅读**：打开 `scipy/integrate/_tanhsinh.py` 或 `scipy/optimize/_chandrupatla.py`，对照它们如何填写同一套 `_initialize`/`_loop` 钩子，体会「一个框架、多种算法」的复用价值。
