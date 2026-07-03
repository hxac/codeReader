# 估值更新与误差估计 post_func_eval

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `derivative` 在**每一轮迭代中调用完 `f` 之后、做收敛判断之前**，到底用这些新函数值做了哪三件事：拼接历史函数值、加权算出新的导数估计 `df`、算出本轮的误差估计 `error`。
- 写出中心差分 `work_fc` / `fc` 与单侧差分 `work_fo` / `fo` 的拼接方式，解释为什么首轮可以直接用全部点、后续轮次要「丢掉最外侧 2 个点、补上最内侧 2 个新点」。
- 解释 `df = fc @ wc / work.h`（以及单侧的 `fo @ wo / work.h`）这一行如何把 [u2-l2](_derivative_weights) 算出的权重作用在函数值上，并理解左侧差分为何最后要整体乘 `-1`。
- 说清楚为什么 `error` 取的是「相邻两次 `df` 估计之差的绝对值」，以及它为什么是一个**偏保守**的误差上界。
- 能够用 `callback` 逐轮收集 `df` 与 `error`，亲眼看到误差随迭代下降、直到被浮点消去误差再次抬升的过程。

## 2. 前置知识

本讲紧接 [u2-l3 迭代求值点生成 pre_func_eval](_post-func-eval)，请先确认你已经理解：

- **`eim._loop` 的钩子顺序**：每轮迭代依次执行 `pre_func_eval → func → post_func_eval → check_termination`。`pre_func_eval` 决定「去哪里取值」，`func` 真正求值，而本讲的 `post_func_eval` 负责「拿到函数值后怎么用」。
- **嵌套 stencil（nested stencil）**：首轮新增 `order` 个点，后续每轮只补 2 个最内侧新点；上一轮绝大多数函数值被复用。本讲要回答的就是「这些复用的点如何与新增的点重新拼好」。
- **`work` 对象**：跨轮保留的状态容器，里面有上一轮的函数值 `work.fs`、上一轮的导数估计 `work.df`、上一轮误差 `work.error`、当前步长 `work.h`、方向掩码 `il/ic/ir/io` 等。
- **差分权重**（来自 [u2-l2](_derivative_weights)）：中心权重 `wc` 与右侧单侧权重 `wo` 都是长度为 `2n+1`（`n = order//2`）的一维数组，只依赖 `step_factor` 与 `order`，已被缓存。本讲只**消费**它们，不重新推导。

此外建议你回忆 [u1-l3](_result-object-and-status) 里讲过的 `status` 状态码：本讲算出的 `error` 与 `df` 会被下一讲 [u2-l5](_check-termination) 的 `check_termination` 用来判定收敛（`0`）、误差回升（`-1`）或非有限值（`-3`）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [scipy/differentiate/_differentiate.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py) | 本讲主角。`post_func_eval`（L495–L560）是全部内容；它读写的 `work` 对象在 L434–L441 初始化（其中 `df` 初值为 `NaN`，见 L405）；`work.h /= work.fac`（L551）把步长缩减留给下一轮；误差定义在 L560。中心权重里 `weights[n] = 0` 的强加（L694–L696）是理解 `fc` 拼接的一个关键。 |
| [scipy/_lib/_elementwise_iterative_method.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py) | `eim._loop` 在 L261 调用 `post_func_eval(x, f, work)`，传入本轮新生成的横坐标 `x` 与函数值 `f`。框架还在 L259 累加了 `nfev`。 |

---

## 4. 核心概念与源码讲解

### 4.1 post_func_eval 的职责与调用时机

#### 4.1.1 概念说明

在 [u2-l3](_post-func-eval) 里，`pre_func_eval` 生成了本轮的新求值横坐标 `x_eval`，框架随后一次性调用 `f(x_eval)` 拿回函数值数组 `f`。**但光有函数值还不能直接得到导数**——这些函数值里，一部分是本轮刚算的新点，一部分要和上一轮存下来的旧点重新对齐，才能套用 [u2-l2](_derivative_weights) 算好的差分权重。

`post_func_eval` 就是干这件事的钩子。它的全部职责可以归纳成三步（正好对应本讲的三个最小模块）：

1. **拼接函数值**：把「本轮新点」和「`work.fs` 里存的历史点」按权重的顺序重新排好，得到本轮中心用的 `fc`、单侧用的 `fo`；同时把本轮所有点写回 `work.fs`，供下一轮复用。
2. **加权求 `df`**：用 `fc @ wc / work.h`（中心）和 `fo @ wo / work.h`（单侧）得到本轮的导数估计，并对左侧元素翻转符号。
3. **估计 `error`**：把本轮 `df` 与上一轮 `df` 之差的绝对值作为误差，并把步长 `work.h` 缩减一级留给下一轮。

#### 4.1.2 核心流程

`post_func_eval(x, f, work)` 在每轮 `func` 之后被调用，流程是：

1. 读 `n = work.terms`（= `order//2`），并按 `work.nit` 判定首轮还是后续轮：`n_new = n`（首轮）或 `1`（后续）。注意这里的 `n_new` 是**中心差分单侧**新增点数，所以首轮中心新增 `2*n_new = order` 个点、后续新增 `2` 个点，与 [u2-l3](_post-func-eval) 的 `pre_func_eval` 完全对齐。
2. 拼中心的 `work_fc`（= 新左点 + 历史点 + 新右点），再从中切出本轮真正要用的 `fc`。
3. 拼单侧的 `work_fo`（= 历史点 + 新点），再切出 `fo`。
4. 把 `work_fc` / `work_fo` 写回一个**变宽**的 `work.fs`，让历史点越攒越多。
5. 取权重 `wc, wo`；存 `df_last = df`；算 `df`（中心、单侧分掩码写入，左侧乘 `-1`）。
6. `work.h /= work.fac` 缩减步长；`error_last = error`；`error = |df - df_last|`。

关键时机：`post_func_eval` 跑在 `check_termination` **之前**，所以它算出的 `df` / `error` 是本轮判收敛的输入；而步长除法 `work.h /= work.fac` 放在本函数**末尾**（L551），保证本轮用的 `work.h` 还是缩减前的值（下一轮 `pre_func_eval` 才看到缩小后的步长）。

#### 4.1.3 源码精读

函数签名与开头取状态：[_differentiate.py:495-521](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L495-L521)。框架调用它的位置：[_elementwise_iterative_method.py:261](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L261)。

```python
def post_func_eval(x, f, work):
    n = work.terms
    n_new = n if work.nit == 0 else 1     # 中心单侧新增点数
    il, ic, io = work.il, work.ic, work.io   # 左 / 中 / (左|右) 掩码
```

> 说明：`f` 是框架本轮调用 `func` 的返回值，形状为 `(活跃元素数, n_new_total)`，列对应 `pre_func_eval` 生成的那些新横坐标。`work.fs` 是**上一轮结束时**写回的全部历史函数值。

---

### 4.2 函数值拼接：work_fc 与 work_fo

#### 4.2.1 概念说明

差分权重 `wc` / `wo` 是**固定顺序**的一维数组（长度 `2n+1`），每一格对应模板上一个特定的相对位置（见 [u2-l2](_derivative_weights) 的 Vandermonde 推导）。所以做矩阵乘法 `fc @ wc` 之前，必须保证 `fc` 每一列的函数值**正好**对应 `wc` 同一列的位置。源码注释把这称为「the tricky part is getting the order to match that of the weights」。

`post_func_eval` 用一个两段式策略来对齐：

- 先拼一个 `work_fc` / `work_fo`，表示「**到目前为止**所有用过的函数值」，顺序宽松（新点夹在旧点两头）。
- 再从中**切片**出本轮真正喂给权重的 `fc` / `fo`，顺序严格匹配权重。

这样写是因为嵌套 stencil 每轮要「丢 2 个最外侧旧点、加 2 个最内侧新点」，直接维护一个严格有序的数组比较繁琐，先汇总再切片更不容易错。

#### 4.2.2 核心流程

**中心差分**（`ic` 元素，对称模板，共 `2n+1` 个点，最中间是 `f(x)`）：

1. 拼 `work_fc = [ 本轮新左 n_new 个 | work.fs 里的历史点 | 本轮新右 n_new 个 ]`。
   - 首轮 `n_new = n`，历史点只有 1 个 `f(x)`，于是 `work_fc` 恰好 `2n+1` 列，顺序天然就是权重顺序。
   - 后续轮 `n_new = 1`，历史点有 `2n+1` 个，于是 `work_fc` 有 `2n+3` 列。
2. 切 `fc`：
   - 首轮：`fc = work_fc`（直接全用）。
   - 后续轮：丢掉最外侧左右各 1 个旧点，取 `work_fc[:, :n]`、中间 1 列、`work_fc[:, -n:]` 拼成 `2n+1` 列。

**单侧差分**（`io = il | ir` 元素，模板全在 `x` 的一侧，共 `2n+1` 个点，最左是 `f(x)`）：

1. 拼 `work_fo = [ work.fs 里的历史点 | 本轮新点 ]`（单侧没有「左右夹」的概念，新点统一接在尾部）。
2. 切 `fo`：
   - 首轮：`fo = work_fo`。
   - 后续轮：保留最左的 `f(x)`（`work_fo[:, 0:1]`）+ 最右 `2n` 个点（`work_fo[:, -2n:]`），丢掉中间最外侧的 2 个旧点。

一个**关键细节**（中心差分后续轮）：被切出来的 `fc` 正中间那一列（权重 `wc[n]`）实际装的是一个「被丢掉的旧点」的函数值——但这没关系，因为 [u2-l2](_derivative_weights) 强制令中心权重 `wc[n] = 0`（即 `f(x)` 对中心差分一阶导贡献为 0），这一列是「don't-care」。这点会在 4.3.3 详细说明。

#### 4.2.3 源码精读

中心差分的拼接与切片：[_differentiate.py:523-532](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L523-L532)。

```python
# Central difference
work_fc = (f[ic][:, :n_new], work.fs[ic], f[ic][:, -n_new:])
work_fc = xp.concat(work_fc, axis=-1)
if work.nit == 0:
    fc = work_fc
else:
    fc = (work_fc[:, :n], work_fc[:, n:n+1], work_fc[:, -n:])
    fc = xp.concat(fc, axis=-1)
```

> 说明：`f[ic][:, :n_new]` 是本轮新点中最左的 `n_new` 个（中心模板左翼的新点），`f[ic][:, -n_new:]` 是最右的 `n_new` 个（右翼新点），`work.fs[ic]` 是上一轮存下的历史点。首轮三者拼起来就是 `2n+1` 列的完整模板。

单侧差分的拼接与切片：[_differentiate.py:534-539](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L534-L539)。

```python
# One-sided difference
work_fo = xp.concat((work.fs[io], f[io]), axis=-1)
if work.nit == 0:
    fo = work_fo
else:
    fo = xp.concat((work_fo[:, 0:1], work_fo[:, -2*n:]), axis=-1)
```

> 说明：单侧模板的最左端永远是 `f(x)`（偏移 0），它的权重 `wo[0]` **非零**，所以必须显式保留 `work_fo[:, 0:1]`；右侧只留最新的 `2n` 个点。

把本轮全部点写回一个变宽的 `work.fs`：[_differentiate.py:541-543](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L541-L543)。

```python
work.fs = xp.zeros((ic.shape[0], work.fs.shape[-1] + 2*n_new), dtype=work.dtype)
work.fs = xpx.at(work.fs)[ic].set(work_fc)
work.fs = xpx.at(work.fs)[io].set(work_fo)
```

> 说明：每轮把 `work.fs` 的列数扩 `2*n_new`（首轮扩 `order`、后续扩 2），再把中心的 `work_fc`、单侧的 `work_fo` 按各自掩码写回。注意 `ic.shape[0]` 在这里是布尔掩码的**长度**（= 活跃元素总数），不是 `True` 的个数，所以中心 / 单侧行数一致。`xpx.at` 让这套索引赋值能跨 NumPy/JAX/Torch 后端工作（详见 [u4-l4](_array-api-backends)）。

#### 4.2.4 代码实践

**目标**：亲眼确认「首轮 `work_fc` 直接就是完整模板、后续轮 `work_fc` 多出 2 列再被切掉」。

**步骤**：

1. 阅读上面的 L523–L543。
2. 在本地写一段 NumPy，**复刻**中心差分首轮与第 2 轮的列拼接，打印每一步的列数。
3. 对照 [u2-l2](_derivative_weights) 的权重长度 `2n+1`，确认切出来的 `fc` 列数恒为 `2n+1`。

```python
# 示例代码：复刻中心差分 work_fc / fc 的「形状」演变（不调用真实 derivative）
import numpy as np
n = 4                       # order = 8 => terms = 4
fac = 2.0

# 首轮：历史点只有 f(x)（1 列），新点左右各 n 个
fs_old = np.zeros((1, 1))           # 模拟 work.fs[ic]：只有 f(x)
f_new0 = np.zeros((1, 2*n))         # 模拟首轮 f[ic]：order 个新点
n_new = n
work_fc0 = np.concat((f_new0[:, :n_new], fs_old, f_new0[:, -n_new:]), axis=-1)
fc0 = work_fc0
print("iter0 work_fc cols:", work_fc0.shape[-1], " fc cols:", fc0.shape[-1])
# 预期：work_fc 9 列，fc 9 列（= 2n+1）

# 第 2 轮：历史点已是 2n+1 列，新点只有 2 个
fs_old = np.zeros((1, 2*n + 1))     # 模拟上一轮写回的 work.fs[ic]
f_new1 = np.zeros((1, 2))           # 模拟后续轮 f[ic]：2 个新点
n_new = 1
work_fc1 = np.concat((f_new1[:, :n_new], fs_old, f_new1[:, -n_new:]), axis=-1)
fc1 = np.concat((work_fc1[:, :n], work_fc1[:, n:n+1], work_fc1[:, -n:]), axis=-1)
print("iter1 work_fc cols:", work_fc1.shape[-1], " fc cols:", fc1.shape[-1])
# 预期：work_fc 11 列（= 2n+3），fc 9 列（= 2n+1，丢掉 2 个最外侧点）
```

**需要观察的现象**：首轮 `fc` 列数 = `2n+1 = 9`；第 2 轮 `work_fc` 列数 = `2n+3 = 11`，切完 `fc` 又回到 `9`。

**预期结果**：`iter0 ... fc cols: 9`、`iter1 ... fc cols: 9`。（具体数值待本地运行确认。）

#### 4.2.5 小练习与答案

**练习 1**：为什么单侧差分的 `fo` 要显式保留 `work_fo[:, 0:1]`（即 `f(x)`），而中心差分的 `fc` 没有专门保留 `f(x)`？

**答案**：单侧模板最左端就是 `f(x)`（偏移 0），它的权重 `wo[0]` 非零，`f(x)` 必须参与计算，所以必须保留。中心模板里 `f(x)` 的权重 `wc[n] = 0`，对一阶导没贡献，所以代码干脆没在后续轮的 `fc` 里专门保留它，那一格成了 don't-care（详见 4.3.3）。

**练习 2**：第 2 轮的 `work_fc` 有 `2n+3` 列，但 `fc` 只要 `2n+1` 列。被丢掉的是哪 2 列？为什么是它们？

**答案**：丢掉的是上一轮**最外侧**（离 `x` 最远）的左右各 1 个点。因为步长每轮除以 `fac` 后，旧的最外侧点已经落在新的、更窄的模板之外，对更高阶估计不再有用，反而会降低数值稳定性（参见 [u2-l2](_derivative_weights) 注释里关于「不复用 `x±h`」的说明）。

---

### 4.3 加权求 df：权重对齐与单侧符号

#### 4.3.1 概念说明

`fc` / `fo` 拼好以后，求导估计就是一行矩阵乘法。回顾 [u2-l2](_derivative_weights)：权重是从 Vandermonde 方程组解出来的，满足矩条件

\[
\sum_{i} w_i\, h_i^{k} = \delta_{k,1},
\]

其中 \(h_i\) 是模板各点的**无量纲**相对位置。若本轮实际步长为 \(H\)（即 `work.h`），则实际偏移是 \(H h_i\)，于是

\[
\sum_{i} w_i\, f(x + H h_i)
= \sum_{k} \frac{f^{(k)}(x)}{k!}\, H^{k} \underbrace{\sum_{i} w_i h_i^{k}}_{\delta_{k,1}}
= H\, f'(x).
\]

所以导数估计为

\[
f'(x) \;\approx\; \frac{1}{H}\sum_{i} w_i\, f(x + H h_i).
\]

这正是源码里的 `fc @ wc / work.h`：`fc` 第 `i` 列就是 \(f(x + H h_i)\)，`wc[i]` 就是 \(w_i\)，逐行点乘后再除以 \(H\)。单侧同理用 `fo @ wo / work.h`。

#### 4.3.2 核心流程

1. 取权重：`wc, wo = _derivative_weights(work, n, xp)`（命中缓存，几乎零开销）。
2. 存上一轮估计：`work.df_last = copy(work.df)`（用于算误差）。
3. 中心元素：`work.df[ic] = fc @ wc / work.h[ic]`。
4. 单侧元素：`work.df[io] = fo @ wo / work.h[io]`。
   - 注意：此时左侧 `il` 元素也被写入了 `fo @ wo`，但它用的是「右侧模板镜像点」上的函数值，符号反了。
5. 左侧纠偏：`work.df[il] *= -1`。

为什么左侧要乘 `-1`？因为左侧元素的求值点是 `x - H h_i`（[u2-l3](_post-func-eval) 里 `work.x[il] - hr[il]`），相当于把右侧模板关于 `x` 镜像。对一阶导这种「奇」量，镜像模板估出来的正好是 \(-f'(x)\)，所以乘 `-1` 纠回。这也呼应了 [u2-l2](_derivative_weights) 的结论：左权重 = 右权重的相反数，源码干脆复用同一套 `wo` 再翻符号。

#### 4.3.3 源码精读

加权与符号处理：[_differentiate.py:545-549](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L545-L549)。

```python
wc, wo = _derivative_weights(work, n, xp)
work.df_last = xp.asarray(work.df, copy=True)
work.df = xpx.at(work.df)[ic].set(fc @ wc / work.h[ic])
work.df = xpx.at(work.df)[io].set(fo @ wo / work.h[io])
work.df = xpx.at(work.df)[il].multiply(-1)
```

> 说明：`fc @ wc` 是 `(活跃数, 2n+1) @ (2n+1,) -> (活跃数,)` 的逐行点乘；除以 `work.h[ic]`（本轮步长，**尚未**被 L551 缩减）才得到导数量纲。三处 `xpx.at(...).set/multiply` 分别只改中心、单侧、左侧子集，互不干扰。

**关于「中心 don't-care 列」**：在 4.2.2 提到，后续轮 `fc` 正中间那一列装的是一个被丢掉的旧点。这之所以无害，是因为中心权重 `wc[n]` 被 [u2-l2](_derivative_weights) 强制清零：[_differentiate.py:694-696](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L694-L696)。

```python
# Enforce identities to improve accuracy
weights[n] = 0
for i in range(n):
    weights[-i-1] = -weights[i]
```

> 说明：中心模板关于 `x` 对称，估计的又是一阶导（关于偏移的奇函数），所以 `f(x)`（偏移 0）的系数在数学上就是 0；源码强制置 0 只是为了抹掉 Vandermonde 求解留下的浮点残差、并顺手施加反对称性 `wc[-i-1] = -wc[i]` 提精度。正因为 `wc[n] = 0`，`fc` 第 `n` 列装什么都不会影响结果——切片把一个「废值」塞进去也无妨。

步长缩减与误差存档：[_differentiate.py:551-552](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L551-L552)。

```python
work.h /= work.fac
work.error_last = work.error
```

> 说明：步长除法放在加权之后，保证本轮除的是「本轮用的步长」；下一轮 `pre_func_eval` 看到的 `work.h` 已经缩小 `fac` 倍。

#### 4.3.4 代码实践

**目标**：验证 `fc @ wc / H` 确实还原解析导数，并理解左侧乘 `-1` 的必要性。

**步骤**：

1. 用 NumPy 手搓一个 2 阶中心差分（`n=1`，`order=2`），手算权重，再与 `derivative(order=2)` 的结果对照。
2. 分别对 `np.exp` 在 `x=1` 用 `step_direction=-1/0/1` 调 `derivative`，确认三种方向得到几乎相同的 `df`。

```python
# 示例代码：手搓 2 阶中心权重，对照 derivative
import numpy as np
from scipy.differentiate import derivative

x, H, fac = 1.0, 0.25, 2.0
# 2 阶中心模板偏移（无量纲）：h = [-1, 0, 1]
# 矩条件 -> 权重 wc = [-1/(2H)... ] 这里直接用解析： (f(x+H)-f(x-H))/(2H)
f = np.exp
df_manual = (f(x+H) - f(x-H)) / (2*H)
print("manual 2nd-order central df:", df_manual, " true:", f(x))

for hdir in (-1, 0, 1):
    res = derivative(f, x, order=2, step_direction=hdir, maxiter=1)
    print(f"hdir={hdir:+d}  df={float(res.df):.12f}")
```

**需要观察的现象**：三种 `hdir` 的 `df` 都接近 `e ≈ 2.71828`；`hdir=-1`（纯左侧）若**忘记**乘 `-1** 会得到相反数，而源码纠正后它与右侧同号。

**预期结果**：三行 `df` 数值互相接近且都接近 `np.exp(1)`。（具体数值待本地运行确认。）

#### 4.3.5 小练习与答案

**练习 1**：如果把 L549 的 `work.df[il].multiply(-1)` 删掉，对纯左侧（`step_direction=-1`）的元素，`df` 会变成什么样？

**答案**：会变成正确值的相反数。因为左侧用的是镜像点 `x - H h_i`，算出来的是 \(-f'(x)\)，必须乘 `-1` 纠回。删掉后左侧结果符号全反。

**练习 2**：为什么 `work.df_last` 要用 `xp.asarray(work.df, copy=True)` 显式拷贝，而不是直接 `work.df_last = work.df`？

**答案**：因为紧接着 L547–L549 会用 `xpx.at(...).set(...)` **原地**修改 `work.df`。若不拷贝，`work.df_last` 会和 `work.df` 指向同一块内存，随后被改掉，导致 L560 算出的 `error = |df - df_last|` 恒为 0。显式拷贝是为了冻结「上一轮的快照」。

---

### 4.4 误差估计 error：相邻两轮之差

#### 4.4.1 概念说明

`derivative` 是自适应的：它不知道真导数是多少，所以没法直接算「真实误差」。它采用的是一个通用且稳健的代理量——**相邻两次估计之差的绝对值**：

\[
\text{error}^{(k)} = \bigl|\,f'^{(k)}(x) - f'^{(k-1)}(x)\,\bigr|.
\]

直观上：如果连续两轮的估计几乎不变，说明步长已经小到估计值「稳定」了，可以停。这个量不需要知道真值，因此适用于任何黑盒函数。

它通常是**偏保守的上界**：一旦收敛已经开始（误差主要受截断误差主导、按 \(1/\text{fac}^{\text{order}}\) 规律下降），真实误差更接近「本轮估计与**下一轮**估计之差」，而不是「本轮与上一轮之差」。源码注释（L553–L559）明确提到了这一点，并指出可以用 Richardson 外推得到高one阶的误差估计，但当前实现选择了最简单的版本。

#### 4.4.2 核心流程

1. 在加权之后，`work.df` 已是本轮新估计；`work.df_last` 是 4.3 里存好的上一轮快照。
2. `work.error_last = work.error`（把上一轮的误差也存档，供 `check_termination` 判断「误差是否回升」）。
3. `work.error = abs(work.df - work.df_last)`。

**首轮的特殊性**：`work.df` 初值是 `NaN`（[_differentiate.py:405](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L405)），所以首轮 `df_last = NaN`，首轮 `error = |df - NaN| = NaN`。这是**有意为之**的：首轮只有一个估计，谈不上「稳定」，`NaN` 会让 `check_termination` 的收敛判断（`error < atol + rtol*|df|`）自动为假，从而保证至少跑两轮才有资格判收敛。

#### 4.4.3 源码精读

误差定义与上下文：[_differentiate.py:552-560](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L552-L560)。

```python
work.h /= work.fac
work.error_last = work.error
# Simple error estimate - the difference in derivative estimates between
# this iteration and the last. This is typically conservative ...
work.error = xp.abs(work.df - work.df_last)
```

> 说明：`work.error_last` 与 `work.error` 一前一后，分别服务于 [u2-l5](_check-termination) 的「误差回升」判定和「收敛」判定——前者比较 `error > error_last*10`，后者比较 `error < atol + rtol*|df|`。

首轮 `df` 初值为 `NaN`：[_differentiate.py:405](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L405)。

```python
df = xp.full_like(f, xp.nan)
```

> 说明：`NaN` 在任何比较中都返回假，首轮因此不会误判收敛；同时它也标志着「这个元素还没产生过有效估计」（失败元素最终也常以 `NaN` 暴露，见 [u1-l3](_result-object-and-status)）。

#### 4.4.4 代码实践

**目标**：用 `callback` 逐轮收集 `df` 与 `error`，画出误差随迭代下降、随后被消去误差抬升的曲线。这正是本讲综合实践的前奏，也是文档示例（[_differentiate.py:262-291](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L262-L291)）和测试 `test_maxiter_callback`（[test_differentiate.py:260-301](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L260-L301)）的做法。

**步骤**：

1. 对 `f(x)=exp(x)` 在 `x=1` 调 `derivative`，设 `tolerances=dict(atol=0, rtol=0)` 强制跑满 `maxiter`。
2. 用 `callback(res)` 把每轮的 `res.df`、`res.error` 收集进列表。
3. 用 `matplotlib` 画 `error` 随迭代次数的半对数曲线。
4. 同时画出真实误差 `|res.df - exp(1)|` 作对照，观察「估计误差」与「真实误差」的差距。

```python
# 示例代码
import numpy as np
import matplotlib.pyplot as plt
from scipy.differentiate import derivative

f = np.exp
x = 1.0
ref = f(x)

dfs, errs = [], []
def cb(res):
    dfs.append(float(res.df))
    errs.append(float(res.error))

res = derivative(f, x, tolerances=dict(atol=0, rtol=0), maxiter=12, callback=cb)
iters = np.arange(1, len(errs) + 1)
true_err = np.abs(np.array(dfs) - ref)

plt.semilogy(iters, errs, 'o-', label='estimated error (|df - df_last|)')
plt.semilogy(iters, true_err, 's-', label='true error (|df - exp(1)|)')
plt.xlabel('iteration'); plt.ylabel('error'); plt.legend(); plt.show()
print("final status:", int(res.status), " df:", float(res.df))
```

**需要观察的现象**：

1. 前几轮 `error` 与 `true_err` 都近似按 \(1/\text{fac}^{\text{order}} = 1/2^8 \approx 0.0039\) 的比例下降（每轮约降 3 个数量级）。
2. 到某一步后 `error` 不再下降反而**回升**——这是浮点消去误差开始主导（步长太小，相近数相减丢失有效位）。
3. 最终 `res.status` 很可能是 `-1`（误差回升终止）或 `-2`（跑满 `maxiter`），而不是 `0`，因为我们把容差设成了 0。

**预期结果**：曲线先线性（在对数轴上）下降、触底后回升；`status` 非 0。（具体拐点位置与数值待本地运行确认。）

> 提示：`callback` 会在**首轮迭代之前**被调用一次（此时 `res.df` 是 `NaN`、`res.status` 是 `1` 进行中），收集时建议跳过首个 `NaN`，或用 `np.nan_to_num` 处理。

#### 4.4.5 小练习与答案

**练习 1**：为什么文档示例里 `(errors[1,1]/errors[0,1], 1/hfac**order)` 这一对值会很接近（约 `0.0625`）？

**答案**：因为步长每轮除以 `hfac`，`order` 阶公式的截断误差按 \(h^{\text{order}+1}\) 量级下降，相邻两轮误差比约为 \(1/\text{hfac}^{\text{order}}\)。`hfac=2, order=4` 时即 \(1/16 = 0.0625\)，与实测 `0.06215` 吻合（略有偏差来自高阶项与浮点误差）。

**练习 2**：如果把 `tolerances` 设成默认值（不强制 `atol=0, rtol=0`），上面的曲线还会出现「回升」吗？

**答案**：通常不会。默认容差下，`check_termination` 会在误差首次低于 `atol + rtol*|df|` 时就判收敛（`status=0`）并停止该元素，根本跑不到消去误差主导的那几轮。强制 `atol=0, rtol=0` 正是为了关掉这条提前退出通道、把下降—回升的完整曲线暴露出来。

---

## 5. 综合实践

把本讲三件事串起来：**用一个会「自我记录」的函数，结合 `callback`，一次性看清 `post_func_eval` 如何复用历史函数值、加权得到 `df`、并产生逐轮下降的 `error`。**

任务：对 `f(x) = exp(x)` 在 `x = 1` 调用 `derivative`，设 `order=4`、`maxiter=8`、`tolerances=dict(atol=0, rtol=0)`，并提供一个 `callback` 收集每轮的 `res.df` 与 `res.error`。然后：

1. **看复用**：用一个包装函数统计 `f` 总共被调用了多少次（`nfev`），验证首轮 `nfev` 增量是 `order+1 = 9`、之后每轮只增 `2`，对应「首轮拼满、后续丢 2 加 2」。
2. **看加权**：把每轮的 `res.df` 与解析值 `exp(1)` 对比，确认前几轮误差按 \(1/2^4 = 1/16\) 的比例下降。
3. **看误差**：在同一张半对数图上画「估计误差 `res.error`」与「真实误差」，观察估计误差先与真实误差同步下降、触底后回升，最终 `status` 为 `-1`（误差回升）。
4. **看符号**（选做）：把 `step_direction` 分别设成 `-1/0/1` 再各跑一次，确认三种方向最终 `df` 都收敛到同一个 `exp(1)`（左侧依赖 4.3 的乘 `-1** 纠偏）。

```python
# 示例代码（骨架）
import numpy as np
import matplotlib.pyplot as plt
from scipy.differentiate import derivative

calls = {'n': 0}
def f(x):
    calls['n'] += 1
    return np.exp(x)

ref = np.exp(1.0)
records = []
def cb(res):
    records.append((float(res.df), float(res.error), int(res.nfev)))

res = derivative(f, 1.0, order=4, maxiter=8,
                 tolerances=dict(atol=0, rtol=0), callback=cb)

for i, (df, err, nfev) in enumerate(records):
    print(f"iter{i+1}: df={df:.10f}  est_err={err:.3e}  nfev={nfev}  true_err={abs(df-ref):.3e}")
print("status:", int(res.status))

dfs = [r[0] for r in records]; est = [r[1] for r in records]
plt.semilogy(range(1, len(est)+1), est, 'o-', label='estimated error')
plt.semilogy(range(1, len(dfs)+1), [abs(d-ref) for d in dfs], 's-', label='true error')
plt.xlabel('iteration'); plt.legend(); plt.show()
```

> 提示：若你看到首轮 `nfev` 从 1 跳到 9、之后每轮 +2，且估计误差曲线先降后升、`status=-1`，就说明你对 `post_func_eval` 的「拼接复用 → 加权求 df → 相邻差为误差」三步理解正确。（具体数值待本地运行确认。）

---

## 6. 本讲小结

- `post_func_eval` 是 `func` 之后、`check_termination` 之前的钩子，干三件事：**拼接函数值**、**加权求 `df`**、**估计 `error`**，并在末尾把步长 `work.h` 缩减一级留给下一轮。
- **拼接**采用「先汇总再切片」：`work_fc = [新左 | 历史 | 新右]`、`work_fo = [历史 | 新]`；首轮直接全用，后续轮丢掉最外侧 2 个旧点、补上最内侧 2 个新点。`work.fs` 每轮变宽 `2*n_new` 列以攒下所有历史值。
- **加权**就是一行 `fc @ wc / work.h`（中心）和 `fo @ wo / work.h`（单侧）；左侧因为用镜像点，最后整体乘 `-1` 纠符号。
- 一个关键细节：中心权重 `wc[n]` 被 [u2-l2](_derivative_weights) 强制清零，所以 `fc` 正中间那一列是 don't-care——这正是源码切片敢把一个「废值」塞进去的原因。
- **误差**取相邻两轮 `df` 之差的绝对值 `|df - df_last|`，是偏保守的上界；首轮 `df` 初值 `NaN`，使首轮 `error` 为 `NaN`，从而保证至少跑两轮才有资格判收敛。
- 误差随迭代先按 \(1/\text{fac}^{\text{order}}\) 下降，直到浮点消去误差主导而回升——这一升一降正是 [u2-l5](_check-termination) 里「误差回升终止（`status=-1`）」启发式的物理来源。

## 7. 下一步学习建议

- 接下来读 [u2-l5 收敛判断与终止 check_termination](_check-termination)，看本讲算出的 `error` / `df` 如何被用来判定收敛（`0`）、非有限值（`-3`）和误差回升（`-1`），以及 `error > error_last*10` 这条启发式的具体阈值。
- 想理解 `work.fs` 为什么「越攒越多却不会爆」、以及已收敛元素的函数值如何被框架压缩掉，请看 [u2-l6 逐元素迭代框架 eim._loop](_elementwise-loop-framework)。
- 想从测试角度验证本讲的拼接 / 加权 / 误差行为，可以读 `test_differentiate.py` 的 `test_maxiter_callback`（[L260-L301](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L260-L301)），它用 `callback` + `StopIteration` 精确控制迭代轮数。
- 误差回升与浮点消去误差的工程化应对（大 `|x|` 调步长、零导数点设 `atol`）留到 [u4-l3 数值精度、消去误差与调参](_numerical-precision-tuning)。
