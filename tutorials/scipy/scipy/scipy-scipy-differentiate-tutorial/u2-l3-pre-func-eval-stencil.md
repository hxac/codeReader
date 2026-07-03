# 迭代求值点生成 pre_func_eval

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `derivative` 在**每一轮迭代之前**到底在哪里对 `f` 取值，以及为什么首轮要取 `order` 个新点、后续每轮只取 2 个新点。
- 写出中心差分步长 `hc` 与单侧差分步长 `hr` 在首轮和后续轮次的具体公式，并解释 `step_factor`（记作 `c`）与 `d = sqrt(c)` 的几何含义。
- 解释 `il / ic / ir` 三个布尔索引如何把不同 `step_direction` 的元素分别导向中心、右侧、左侧三类求值点，并理解 `xpx.at` 在这里的作用。
- 能够用一个「包装函数」实测 `f` 每次被调用时收到的横坐标，验证「嵌套 stencil」的复用关系。

## 2. 前置知识

本讲紧接 [u2-l2 有限差分权重](_derivative_weights)，请先确认你已理解：

- **嵌套 stencil（nested stencil）**：差分公式的求值点被刻意安排成「几何等比、逐层向内收缩」的格局，使得缩小步长后，上一轮绝大多数函数值都能被复用。
- **`work` 对象**：`derivative` 把所有需要跨轮保留的状态塞进一个 `_RichResult`（`x`、`h`、`fac`、`terms`、`hdir`、`il/ic/ir`、`fs` 等），`pre_func_eval` 只读不改它（步长缩减实际发生在 `post_func_eval` 里）。
- **`step_direction` 的符号语义**：`0` 表示中心差分，负数表示只取非正步（左侧），正数表示只取非负步（右侧）。本讲会把这层「语义」落实成具体的横坐标。
- **`eim._loop` 的钩子顺序**：每轮迭代依次调用 `pre_func_eval → func → post_func_eval → check_termination`，`pre_func_eval` 是「本轮要去哪里取值」的决策点。

如果你对 `eim._loop` 的整体框架还不熟悉，可以先把它当成「每轮调用一次 `pre_func_eval` 来问：这一轮还要在哪些点求值？」的黑盒，本讲末尾再回头对照 [u2-l6](_loop) 即可。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [scipy/differentiate/_differentiate.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py) | 本讲主角。`pre_func_eval`（L449–L493）生成求值点；`work` 对象（L434–L441）提供它读取的状态；`work.h /= fac`（L551）在 `post_func_eval` 末尾完成步长缩减；`il/ic/ir`（L422–L425）来自 `hdir` 的符号化。`_derivative_weights` 的注释（L632–L659）给出了 stencil 的几何描述。 |
| [scipy/_lib/_elementwise_iterative_method.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py) | `eim._loop`（L237–L263）是调用 `pre_func_eval` 的地方，决定了它在每轮中的时机、传入的是「压缩后仅含活跃元素」的 `work`。 |

---

## 4. 核心概念与源码讲解

### 4.1 pre_func_eval 的职责与调用时机

#### 4.1.1 概念说明

`derivative` 是一个**自适应迭代**算法：它从大步长起步估计导数，再逐轮缩小步长、重新估计，直到估计值稳定。每一轮要做的第一件事，就是决定**这一轮还需要在哪些新横坐标上调用 `f`**——这正是 `pre_func_eval` 的全部职责。

它只回答一个问题，并返回一个数组：

> 给定当前的步长 `work.h`、步长缩减因子 `work.fac`、模板阶数 `work.terms`、以及每个元素的方向 `work.hdir`，生成本轮的求值横坐标矩阵 `x_eval`。

`x_eval` 的形状是 `(活跃元素数, 本轮新增点数)`：每一行对应一个还没收敛的 `x`，每一列是一个新的求值点。框架拿到它后，**一次性**把整张矩阵喂给 `f`，从而完成向量化求值。

#### 4.1.2 核心流程

`pre_func_eval` 在每轮迭代的最开头被调用，流程是：

1. 从 `work` 读取：当前步长 `h`、因子 `c = fac`、`d = c**0.5`、`n = terms`（阶数的一半）、方向索引 `il/ic/ir`、当前轮次 `nit`。
2. 按方向分别构造「中心步长向量 `hc`」和「单侧步长向量 `hr`」。
3. 确定「本轮新增点数 `n_new`」：首轮为 `2*n`，后续轮为 `2`。
4. 分配全零的 `x_eval`，按 `ir/ic/il` 三类元素分别填入 `x + hr`、`x + hc`、`x - hr`。
5. 返回 `x_eval`，框架用它调用 `f`。

关键时机：`pre_func_eval` 跑在 `func` **之前**，且此时 `work.nit` 还是「上一轮结束时的值」，首轮正是用 `work.nit == 0` 来识别的。步长 `work.h` 的除法发生在它**之后**的 `post_func_eval` 里，所以首轮 `pre_func_eval` 看到的 `work.h` 就是原始的 `initial_step`。

#### 4.1.3 源码精读

先看 `pre_func_eval` 的签名与开头取值：函数定义在 [_differentiate.py:449-493](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L449-L493)。

```python
def pre_func_eval(work):
    n = work.terms          # 阶数的一半，例如 order=8 时 n=4
    h = work.h[:, xp.newaxis]   # 当前步长，加一个尾部轴便于广播
    c = work.fac            # step_factor，步长缩减因子
    d = c**0.5              # 单侧模板用的 sqrt(step_factor)
```

这几行把后续要用到的量都备好。注意 `h = work.h[:, xp.newaxis]`：`work.h` 是一维的（每个活跃元素一个步长），加一个新轴后形状变成 `(活跃数, 1)`，以便和「多个偏移量」相乘时广播。

再看它读取的 `work` 是怎么搭起来的（`il/ic/ir` 来自 `hdir` 的符号化）：

- 方向布尔索引在 [_differentiate.py:422-425](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L422-L425)：`il = hdir < 0`、`ic = hdir == 0`、`ir = hdir > 0`、`io = il | ir`。
- `work` 对象本身在 [_differentiate.py:434-441](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L434-L441) 创建，其中 `h=h0`（即 `initial_step`）、`terms=(order+1)//2`、`fac=fac`。
- 步长缩减发生在 `post_func_eval` 末尾 [_differentiate.py:551](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L551)：`work.h /= work.fac`。

调用时机则在 `eim._loop` 的主循环里 [_elementwise_iterative_method.py:237-263](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L237-L263)：

```python
while work.nit < maxiter and ...:
    x = pre_func_eval(work)        # L238 本轮求值点
    ...
    f = func(x, *work.args)        # L254 一次性向量化求值
    work.nfev += ...               # L259 累计函数调用次数
    post_func_eval(x, f, work)     # L261 估导数 + 步长缩减(work.h/=fac)
    work.nit += 1                  # L263 轮次 +1
```

可以看出 `pre_func_eval` 接到的是**已经被压缩过**的 `work`：框架在每轮结束时会剔除已收敛的元素（详见 [u2-l6](_loop)），所以 `work.x`、`work.h`、`work.hdir` 只包含尚未收敛的活跃元素。这正是 `work.hdir.shape[0]` 等于「活跃元素数」的原因。

#### 4.1.4 代码实践

**目标**：确认 `pre_func_eval` 在每轮开头运行、且首轮 `work.h` 等于 `initial_step`（未被提前缩减）。

**操作步骤**（源码阅读型实践）：

1. 打开 [_elementwise_iterative_method.py:237-263](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L237-L263)，按顺序标出 `pre_func_eval`、`func`、`post_func_eval`、`work.nit += 1` 四行的相对位置。
2. 再打开 [_differentiate.py:551](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L551)，确认 `work.h /= work.fac` 位于 `post_func_eval` 内部。
3. 据此推理：第一轮 `pre_func_eval` 运行时，`work.h /= fac` **尚未发生**。

**需要观察的现象**：`pre_func_eval` 严格先于 `func` 和 `post_func_eval`；首轮步长未被提前缩小。

**预期结果**：首轮 `pre_func_eval` 使用 `work.h = initial_step`，从第二轮起 `work.h` 才变成 `initial_step/step_factor`、`initial_step/step_factor**2`、……

> 说明：本沙箱环境无法运行 Python，以上结论由源码顺序直接推出（待本地运行确认）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `work.nit += 1`（[_differentiate.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L263) 中由 `eim._loop` 执行）移到 `pre_func_eval` 之前，会出什么问题？

**答案**：`pre_func_eval` 用 `work.nit == 0` 区分「首轮」与「后续轮」。若 `nit` 提前自增，则首轮会被误判为后续轮，于是首轮只生成 2 个新点（而不是 `2*n` 个），整套嵌套 stencil 与权重都会错位，导致导数估计完全错误。

**练习 2**：为什么 `pre_func_eval` 里要用 `h = work.h[:, xp.newaxis]` 而不是直接用 `work.h`？

**答案**：`work.h` 是一维（每元素一个步长），而每轮要在「多个偏移量」上同时取值。加一个尾部轴使其形状为 `(活跃数, 1)`，才能与形状 `(活跃数, n_new)` 的偏移向量按元素广播。

---

### 4.2 嵌套 stencil 与新求值点数（首轮 order 个，后续 2 个）

#### 4.2.1 概念说明

「嵌套」是这套算法节省函数调用次数的核心思想。直觉上：第 0 轮我们用一个大步长模板估了一次导数；第 1 轮把步长缩小一档再估。如果模板设计得好，**第 1 轮需要的求值点，绝大部分在第 0 轮就已经算过了**，只需要补两个最靠近 `x` 的新点即可。

`derivative` 的中心模板长这样（取自 [_differentiate.py:632-647](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L632-L647) 的注释，以 `c = step_factor` 为公比）：

\[
\text{第 0 轮（步长 }h\text{）:}\quad x,\ \ x\pm h,\ \ x\pm h/c,\ \ x\pm h/c^2,\ \ x\pm h/c^3
\]

缩小一档后（步长变为 `h/c`），新模板变成：

\[
\text{第 1 轮（步长 }h/c\text{）:}\quad x,\ \ x\pm h/c,\ \ x\pm h/c^2,\ \ x\pm h/c^3,\ \ x\pm h/c^4
\]

对比两行：`x ± h/c`、`x ± h/c^2`、`x ± h/c^3` 全部复用；唯一需要**新算**的是最内侧的 `x ± h/c^4`，而被丢弃的是最外侧的 `x ± h`。所以：

- **首轮**：`f(x)` 已在初始化时算过，需新增 `order` 个点（中心模板共 `2n + 1` 个点，减去已有的 `f(x)` 即 `2n = order` 个新点）。
- **后续每轮**：只需补 **2** 个最内侧的新点。

这正是 [derivative 文档](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L215-L220) 所说的「after `f` is evaluated at `order + 1` points in the first iteration, `f` is evaluated at only two new points in each subsequent iteration」。

#### 4.2.2 核心流程

`pre_func_eval` 用 `n_new` 表达「本轮新增点数」：

- 首轮（`work.nit == 0`）：`n_new = 2*n = order`。
- 后续轮：`n_new = 2`。

中心步长向量 `hc` 的构造因此分两个分支：

- 首轮：生成完整的 `[-h/c^{n-1}, …, -h, +h, …, +h/c^{n-1}]`，共 `2n` 个偏移。
- 后续轮：只生成最内侧的 `[-h/c^{n-1}, +h/c^{n-1}]`，共 2 个偏移（注意此时 `h` 已经是缩小后的当前步长）。

注意偏移的**排列顺序**是刻意设计的（从最外侧负、过零、到最外侧正），这一顺序必须与 `_derivative_weights` 产生的权重顺序对齐——具体对齐细节见 u2-l2 与 `post_func_eval`。

#### 4.2.3 源码精读

新增点数的判定与中心步长的两分支写在 [_differentiate.py:476-487](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L476-L487)：

```python
if work.nit == 0:
    hc = h / c**xp.arange(n, dtype=work.dtype)   # [h, h/c, h/c^2, ..., h/c^{n-1}]
    hc = xp.concat((-xp.flip(hc, axis=-1), hc), axis=-1)
else:
    hc = xp.concat((-h, h), axis=-1) / c**(n-1)  # [-h/c^{n-1}, +h/c^{n-1}]

...

n_new = 2*n if work.nit == 0 else 2              # 首轮 order 个，后续 2 个
```

首轮推导：`xp.arange(n)` 给出 `[0,1,…,n-1]`，所以 `h / c**arange(n) = [h/c^0, …, h/c^{n-1}]`；`flip` 反转后取负，再拼到原向量前面，得到对称的 `[-h/c^{n-1}, …, -h, +h, …, +h/c^{n-1}]`。

后续轮推导：`h` 已经是当前（缩小后的）步长，`xp.concat((-h, h))/c^{n-1}` 正好给出新模板里**唯一两个尚未算过**的内侧点。可自行验证：把上一轮（步长 `h*c`）的中心偏移集合与本轮（步长 `h`）的集合相减，差集恰好就是 `{±h/c^{n-1}}`。

单侧模板 `hr` 的「首轮 `2n` 个、后续 2 个」对应代码在 [_differentiate.py:482-485](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L482-L485)，原理完全一致，下一节细讲。

#### 4.2.4 代码实践

**目标**：实测「首轮新增 8 个点、后续每轮新增 2 个点」，并验证嵌套关系（后续轮的点落在首轮最内侧点的更内侧）。

**操作步骤**（这是本讲的主实践）：用一个包装函数记录 `f` 每次收到的横坐标，对 `np.exp` 在 `x=1` 调用 `derivative`，并用 `tolerances=dict(atol=0, rtol=0)` 关掉提前收敛，强制跑满 3 轮。

```python
# 示例代码
import numpy as np
from scipy.differentiate import derivative

calls = []
def f(x):
    x = np.asarray(x)
    calls.append(np.sort(np.unique(np.round(x, 10))))  # 记录本轮求值点（排序便于阅读）
    return np.exp(x)

res = derivative(f, 1.0, maxiter=3, tolerances=dict(atol=0, rtol=0))
for i, pts in enumerate(calls):
    print("call", i, "->", pts)
```

**需要观察的现象**：

- `call 0` 是初始化时的校验性求值，只有 `x` 本身。
- `call 1`（首轮）应出现 **8** 个新点（默认 `order=8`），它们关于 `x=1` 对称、按公比 `1/c` 几何分布。
- `call 2`、`call 3`（后续轮）各只有 **2** 个新点，且越来越靠近 `x=1`。

**预期结果**（由源码公式推导，待本地运行确认；默认 `initial_step=0.5`、`step_factor=2`、`order=8` 即 `n=4`）：

```
call 0 -> [1.0]
call 1 -> [0.5, 0.75, 0.875, 0.9375, 1.0625, 1.125, 1.25, 1.5]   # 8 个点
call 2 -> [0.96875, 1.03125]                                       # 2 个点
call 3 -> [0.984375, 1.015625]                                     # 2 个点
```

推导要点：首轮中心偏移为 `1 ± {0.5, 0.25, 0.125, 0.0625}`；第 1 轮步长缩为 `0.25`，内侧新点为 `1 ± 0.25/2^3 = 1 ± 0.03125`；第 2 轮步长 `0.125`，新点为 `1 ± 0.015625`。注意 `call 2` 的两点 `0.96875 / 1.03125` 确实落在首轮最内侧点 `0.9375 / 1.0625` 的**更内侧**——这就是嵌套复用的几何证据。

#### 4.2.5 小练习与答案

**练习 1**：把 `order` 改成 `4`（即 `n=2`），首轮和后续轮分别会出现几个新点？

**答案**：首轮 `n_new = 2*n = 4` 个新点；后续轮仍是 2 个。因为 `order` 越小，模板越窄，首轮需要铺设的点也越少，但「后续每轮 2 点」的嵌套节奏不变。

**练习 2**：首轮 `call 1` 的原始数组顺序（未排序）是什么？为什么不能随便打乱？

**答案**：原始顺序是 `1 + [-h/c^3, -h/c^2, -h/c, -h, +h, +h/c, +h/c^2, +h/c^3]`，即 `[0.9375, 0.875, 0.75, 0.5, 1.5, 1.25, 1.125, 1.0625]`。不能打乱，是因为 `_derivative_weights` 产生的权重 `wc` 与函数值 `fc` 是按这个顺序一一对应的，最终 `df = fc @ wc / h`，顺序错位会让权重作用到错误的点上。

---

### 4.3 步长缩减 hc / hr 的几何

#### 4.3.1 概念说明

`pre_func_eval` 一共要造两条「步长向量」：

- `hc`：中心差分的偏移量，关于 `x` 对称（正负成对）。
- `hr`：单侧差分的偏移量，全部同号（右侧为正、左侧为负）。

两者的关键参数是 `c = step_factor` 和 `d = c**0.5`。中心模板用 `c` 作公比；单侧模板用 `d = sqrt(c)` 作公比。**为什么单侧要用 `sqrt(c)`？** 因为单侧模板的点要「向一侧」排开，若也用 `c` 作公比，则缩小步长时新旧点的对齐节奏会和中心模板不一致；改用 `sqrt(c)` 后，单侧模板每轮同样「丢 2 点、加 2 点」，与中心模板保持同步。这一点 [_differentiate.py:649-659](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L649-L659) 的注释有明确说明。

#### 4.3.2 核心流程

设当前步长为 `h`（首轮即 `initial_step`，之后每轮 `h /= c`），`n = terms`。

**中心 `hc`**：

- 首轮：\(\mathrm{hc}=[-h/c^{n-1},\ldots,-h,\ +h,\ldots,+h/c^{n-1}]\)，共 \(2n\) 个。
- 后续轮：\(\mathrm{hc}=[-h/c^{n-1},\ +h/c^{n-1}]\)，共 2 个。

**单侧 `hr`**（以右侧为例，左侧仅取负）：

- 首轮：\(\mathrm{hr}=[h/d^{0}, h/d^{1}, \ldots, h/d^{2n-1}]\)，共 \(2n\) 个。
- 后续轮：\(\mathrm{hr}=[h/c^{n-1},\ h/(d\cdot c^{n-1})]\)（即 \([h/d^{2n-2}, h/d^{2n-1}]\)），共 2 个。

注意 `c = d^2`，所以 `c^{n-1} = d^{2n-2}`，后续轮的 `hr` 正是单侧模板里最靠近 `x` 的两个内侧点。

#### 4.3.3 源码精读

`c` 与 `d` 的定义在 [_differentiate.py:471-473](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L471-L473)：

```python
h = work.h[:, xp.newaxis]
c = work.fac                 # step_factor
d = c**0.5                   # 单侧模板的公比
```

中心 `hc` 的两分支在 [_differentiate.py:476-480](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L476-L480)（上一节已展开）。单侧 `hr` 的两分支在 [_differentiate.py:482-485](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L482-L485)：

```python
if work.nit == 0:
    hr = h / d**xp.arange(2*n, dtype=work.dtype)   # [h, h/d, h/d^2, ..., h/d^{2n-1}]
else:
    hr = xp.concat((h, h/d), axis=-1) / c**(n-1)   # [h/d^{2n-2}, h/d^{2n-1}]
```

dtype 的处理也值得一提：[_differentiate.py:474](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L474) 注释说「在分配 `x_eval` 之前不必关心 dtype」，因为 `hc`、`hr` 这些中间量会用 Python 浮点参与运算，直到分配 `x_eval` 时（下一节的 L488）才统一落回 `work.dtype`。

#### 4.3.4 代码实践

**目标**：观察 `step_factor` 如何同时控制「点间距的公比」和「每轮步长缩减」。

**操作步骤**：在 4.2.4 的脚本基础上，把 `step_factor` 改成 `4`（即 `c=4, d=2`），重新打印首轮 `call 1` 的点。

```python
# 示例代码
res = derivative(f, 1.0, maxiter=2, step_factor=4.0,
                 tolerances=dict(atol=0, rtol=0))
```

**需要观察的现象**：首轮 8 个点之间的间距公比从 `1/2` 变成 `1/4`（中心模板），点会更快地向 `x=1` 聚拢。

**预期结果**（由源码推导，待本地运行确认；`c=4`、`n=4`、`h=0.5`，首轮中心偏移为 `1 ± {0.5, 0.125, 0.03125, 0.0078125}`）：

```
call 1 -> [0.4921875, 0.5, 0.96875, 0.9921875, 1.0078125, 1.03125, 1.5, 1.5078125]
```

排序后可看到点呈 `1/4` 公比的几何聚集，对比 4.2.4 中 `c=2` 的均匀两倍间距，能直观体会 `step_factor` 的双重作用。

#### 4.3.5 小练习与答案

**练习 1**：若把 `step_factor` 设成小于 1 的值（例如 `0.5`），`hc` 会怎样变化？这有什么用？

**答案**：`c < 1` 时 `1/c > 1`，偏移序列会**逐点增大**而非缩小，相当于每轮使用**更大**的步长。`derivative` 文档指出这可用于「刻意避开过小步长」的场景（例如担心消去误差时）。但首轮公式不变，只是几何方向反转。

**练习 2**：为什么 `hr` 用 `d = sqrt(c)` 而不是直接用 `c`？

**答案**：为了让单侧模板的「每轮丢 2 点、加 2 点」节奏与中心模板完全同步。若单侧也用 `c`，则步长缩小一档时单侧点的对齐关系会与中心不同步，无法做到「统一每轮只补 2 个新点」。

---

### 4.4 x_eval 的方向分支：il / ic / ir

#### 4.4.1 概念说明

到目前为止，`hc` 和 `hr` 是「按规则生成的偏移量」，但每个 `x` 元素到底用哪一条，取决于它的 `step_direction`：

- `step_direction == 0`（`ic`，中心）：用 `hc`，向 `x` 的两侧同时取点。
- `step_direction > 0`（`ir`，右侧）：用 `hr`，全部取 `x` 右侧的点。
- `step_direction < 0`（`il`，左侧）：用 `-hr`，全部取 `x` 左侧的点。

这样三种方向的元素可以**在同一次函数调用里一起求值**：把它们的横坐标拼进同一个 `x_eval` 矩阵，不同行用不同方向的偏移即可。左侧复用右侧的 `hr`、仅整体取负，是因为左侧权重恰好是右侧权重的相反数（见 u2-l2）。

实现上用到了 `xpx.at(...)[mask].set(...)`：这是 `array_api_extra` 提供的「带掩码的索引赋值」，在 NumPy 上等价于 `x[mask] = ...`，但能兼容 JAX/Torch 等「数组不可原地修改」的后端（相关后端能力声明见 u4-l4）。

#### 4.4.2 核心流程

1. 用全零分配 `x_eval`，形状 `(活跃数, n_new)`。
2. 对右侧元素（`ir`）：`x_eval[ir] = x[ir] + hr[ir]`。
3. 对中心元素（`ic`）：`x_eval[ic] = x[ic] + hc[ic]`。
4. 对左侧元素（`il`）：`x_eval[il] = x[il] - hr[il]`。

由于每个元素恰属于 `il/ic/ir` 之一，`x_eval` 的每一行被恰好赋值一次（全零初值只是占位）。

#### 4.4.3 源码精读

方向分支写在 [_differentiate.py:488-492](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L488-L492)：

```python
x_eval = xp.zeros((work.hdir.shape[0], n_new), dtype=work.dtype)
il, ic, ir = work.il, work.ic, work.ir
x_eval = xpx.at(x_eval)[ir].set(work.x[ir][:, xp.newaxis] + hr[ir])
x_eval = xpx.at(x_eval)[ic].set(work.x[ic][:, xp.newaxis] + hc[ic])
x_eval = xpx.at(x_eval)[il].set(work.x[il][:, xp.newaxis] - hr[il])
return x_eval
```

几个要点：

- `work.x[ir][:, xp.newaxis]` 形状是 `(ir 的个数, 1)`，`hr[ir]` 形状是 `(ir 的个数, n_new)`，相加时按列广播，得到该方向所有元素、所有新增点的横坐标。
- `hc` 与 `hr` 虽然是「对所有活跃元素」计算的完整向量，但只有对应方向的子集（`hc[ic]`、`hr[ir]`、`hr[il]`）被真正使用；三类向量列数同为 `n_new`，因此形状一致、可分别填入。
- 注意 `x_eval` 的列数 `n_new` 对中心与单侧是同一个值（首轮 `2n`、后续 `2`），这是 4.3 里 `d = sqrt(c)` 带来的「同步」结果——否则中心列数与单侧列数会对不上。

返回值 `x_eval` 随后被 `eim._loop` 在 [_elementwise_iterative_method.py:254](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L254) 喂给 `func`，并在 L259 据其列数 `x.shape[-1]` 累加 `nfev`。

#### 4.4.4 代码实践

**目标**：对比「中心差分」与「单侧差分」在同一轮里生成的点，验证左侧是右侧的镜像。

**操作步骤**：对同一个 `x` 同时传入三种 `step_direction`（向量化），观察首轮 `f` 收到的点。

```python
# 示例代码
import numpy as np
from scipy.differentiate import derivative

seen = {}
def f(x):
    x = np.asarray(x)
    seen[f(x).size if False else x.size] = x   # 仅占位
    return np.exp(x)

# 更稳妥：分别调用，避免向量化时形状混淆
for hdir in (-1, 0, 1):
    calls = []
    def f_local(x, calls=calls):
        x = np.asarray(x)
        calls.append(np.sort(np.unique(np.round(x, 10))))
        return np.exp(x)
    derivative(f_local, 1.0, step_direction=hdir, maxiter=1,
               tolerances=dict(atol=0, rtol=0))
    print("hdir=", hdir, "首轮点:", calls[1] if len(calls) > 1 else calls)
```

> 提示：上面 `f(x).size if False else x.size` 一行是刻意写成的占位演示，实际请使用下方的 `f_local` 版本分别记录。

**需要观察的现象**：

- `hdir=0`（中心）：首轮点关于 `1` 对称。
- `hdir=1`（右侧）：首轮点全部 `≥ 1`。
- `hdir=-1`（左侧）：首轮点全部 `≤ 1`，且恰是右侧情形关于 `1` 的镜像。

**预期结果**（由源码推导，待本地运行确认；`order=8`、`c=2`、`d≈1.414`、`h=0.5`，首轮）：

- 中心：`1 ± {0.5, 0.25, 0.125, 0.0625}`，即 `[0.5, 0.75, 0.875, 0.9375, 1.0625, 1.125, 1.25, 1.5]`。
- 右侧：`1 + {0.5, 0.5/d, 0.5/d^2, ..., 0.5/d^7}`，8 个点全部 `> 1`。
- 左侧：`1 - {0.5, 0.5/d, ..., 0.5/d^7}`，8 个点全部 `< 1`，与右侧关于 `1` 镜像。

#### 4.4.5 小练习与答案

**练习 1**：为什么左侧用 `work.x[il] - hr[il]`，而右侧用 `work.x[ir] + hr[ir]`？能否对左侧也单独构造一套正偏移？

**答案**：左侧的差分点是 `x` 减去一系列正偏移，等价于把右侧模板镜像。源码复用同一个 `hr`、只对左侧取负，是为了避免重复计算（左侧权重也只是右侧权重的相反数，见 u2-l2）。完全可以单独构造，但会多做一次几何运算且无精度收益。

**练习 2**：`x_eval` 用 `xp.zeros` 初始化后，是否存在某行始终为 0、未被赋值？

**答案**：不会。因为每个元素恰属于 `il`、`ic`、`ir` 三者之一（且 `io = il | ir` 与 `ic` 互补），三处 `xpx.at(...).set(...)` 覆盖了全部行，全零只是分配占位。

---

## 5. 综合实践

把本讲的三条主线串起来：**用 `step_direction` 向量化 + 包装记录，一次性看清「首轮 `order` 个点 / 后续 2 个点」「中心对称 vs 单侧镜像」「步长逐轮减半」三件事。**

任务：对 `f(x) = exp(x)`，在 `x = [1, 1, 1]` 三点上分别指定 `step_direction = [-1, 0, 1]`，设 `maxiter=3`、`tolerances=dict(atol=0, rtol=0)`，用一个包装函数记录每次 `f` 收到的输入数组。然后：

1. 数一数每一轮 `f` 收到的列数（即 `n_new`），验证首轮为 `order=8`、后续为 2。
2. 把首轮的三行（左/中/右）分别打印，验证左行 `< 1`、中行关于 1 对称、右行 `> 1`。
3. 把第二轮的中心行点（约 `1 ± 0.03125`）与首轮中心行的最内侧点（`1 ± 0.0625`）比较，确认嵌套关系。
4. 用 `res.df` 与解析值 `exp(1)` 对比，确认尽管取点方式不同，三种方向最终都收敛到同一个导数。

```python
# 示例代码（骨架）
import numpy as np
from scipy.differentiate import derivative

calls = []
def f(x):
    x = np.asarray(x)
    calls.append(x.copy())
    return np.exp(x)

x = np.array([1.0, 1.0, 1.0])
hdir = np.array([-1, 0, 1])
res = derivative(f, x, step_direction=hdir, maxiter=3,
                 tolerances=dict(atol=0, rtol=0))

for i, arr in enumerate(calls):
    print("call", i, "shape", arr.shape)
    print(arr)

print("df =", res.df, " true =", np.exp(1.0))
```

> 提示：由于三个 `x` 的 `step_direction` 不同，首轮 `x_eval` 的三行会分别呈现左侧、中心、右侧三种取点模式；后续轮每行 2 个点。若你观察到首轮 `call 1` 形如 `(3, 8)`、后续 `(3, 2)`，即说明你对 `pre_func_eval` 的理解正确。（具体数值待本地运行确认。）

---

## 6. 本讲小结

- `pre_func_eval` 的唯一职责是：在每轮迭代开头，根据当前 `work.h`、`work.fac`、`work.terms`、`work.hdir` 生成本轮的新求值横坐标矩阵 `x_eval`，供框架一次性向量化调用 `f`。
- **嵌套 stencil** 让函数调用极其省：首轮新增 `order` 个点（中心模板共 `2n+1` 个点，扣除已有的 `f(x)`），后续每轮只补 **2** 个最内侧新点，其余复用上一轮。
- 中心步长 `hc` 用公比 `c = step_factor`；单侧步长 `hr` 用 `d = sqrt(c)`，二者因此能保持「每轮同步丢 2 点、加 2 点」。
- 步长 `work.h` 从 `initial_step` 起步，在每轮 `post_func_eval` 末尾（[_differentiate.py:551](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L551)）除以 `step_factor`，所以第 `k` 轮 `pre_func_eval` 看到的步长为 `initial_step/step_factor**k`。
- `il/ic/ir` 把元素分到左/中/右三类，分别填入 `x - hr`、`x + hc`、`x + hr`；左侧复用右侧的 `hr` 并整体取负。`xpx.at` 让这套掩码赋值能跨 NumPy/JAX/Torch 后端工作。
- `x_eval` 的列数 `n_new`（首轮 `2n`、后续 `2`）对中心与单侧一致，是 `d = sqrt(c)` 设计的直接结果，也是后续 `post_func_eval` 能用同一套权重做矩阵乘法的前提。

## 7. 下一步学习建议

- 接下来读 [u2-l4 估值更新与误差估计 post_func_eval](_post-func-eval)，看 `pre_func_eval` 生成的这些点上的函数值如何被拼接（`work_fc / work_fo`）、如何与 `_derivative_weights` 的权重相乘得到 `df`，以及为什么误差取相邻两轮估计之差。
- 想理解「步长何时停」可继续看 [u2-l5 收敛判断与终止 check_termination](_check-termination)。
- 想看清 `work` 为何会被「压缩」、`pre_func_eval` 收到的为何只是活跃元素，请看 [u2-l6 逐元素迭代框架 eim._loop](_elementwise-loop-framework)。
- 边界附近的单侧差分实战（`step_direction` 处理定义域边界），留到 [u4-l2 步长方向与边界处理](_step-direction-boundary)。
