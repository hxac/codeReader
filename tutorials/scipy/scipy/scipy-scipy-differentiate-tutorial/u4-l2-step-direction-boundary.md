# 步长方向与边界处理 step_direction

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `derivative` 的 `step_direction` 参数三个方向的**差分含义**：`0` 中心差分、负数向后差分（步长非正）、正数向前差分（步长非负）。
- 解释源码如何把任意数值的 `step_direction` **符号化**为内部短名 `hdir`，并由此分支出左/中/右三套求值点生成逻辑。
- 理解单侧差分为何能在**受限定义域**上工作，并能针对定义域边界附近的点正确配置 `step_direction`（必要时配合 `initial_step`），让全程求值点都落在合法区域内。

本讲是专家层的第一类「工程实践」专题，前置知识是 u2-l5（`check_termination`）。我们会频繁回到 `_differentiate.py` 的 `derivative` 主流程与 `pre_func_eval` / `post_func_eval` 两个钩子。

## 2. 前置知识

在进入本讲前，请先回忆以下来自前序讲义的事实：

- **有限差分与 stencil（模板）**：求导是把函数在若干「求值点」上的函数值线性组合，组合系数叫权重。求值点的分布图案叫 stencil（u2-l2）。
- **步长**：求值点到中心点 `x` 的距离。`derivative` 从大步长 `initial_step` 起步，每轮按 `step_factor` 缩小（u1-l2、u2-l3）。
- **首轮 vs 后续轮**：首轮要新增 `order` 个求值点，后续每轮只补 2 个最内侧新点（u2-l3）。
- **中心差分**：求值点关于 `x` 对称分布（如 `x-h, x, x+h`），天然偶数阶、精度高；**单侧差分**：求值点全在 `x` 的一侧（如 `x, x+h, x+2h`），用于无法跨过 `x` 取另一侧的情形。
- **状态码 `-3`（非有限值）**：`check_termination` 一旦发现 `df` 或 `x` 出现 `NaN`/`inf`，就把该元素判为失败（u2-l5）。

本讲要解决的核心问题是：**当 `f` 只在某个区间内有定义（区间外返回 `NaN`）时，中心差分会把求值点甩到区间外而全军覆没，怎么办？** 答案就是用 `step_direction` 强制求值点只往区间内侧走。

## 3. 本讲源码地图

本讲几乎全部围绕同一个文件：

| 文件 | 作用 |
| --- | --- |
| `scipy/differentiate/_differentiate.py` | `derivative` 的全部实现：参数文档、输入校验 `_derivative_iv`、主流程对 `hdir` 的符号化与广播、`pre_func_eval` 的方向分支、`post_func_eval` 的符号纠正 |
| `scipy/differentiate/tests/test_differentiate.py` | `test_step_direction` 与 `test_step_direction_size`，是本讲代码实践的依据 |

辅助参考（前序讲义已精读，本讲只点对点回引）：`scipy/_lib/_elementwise_iterative_method.py` 的 `_loop` 负责调用各钩子；`scipy/_lib/_array_api.py` 提供 `xpx.at` 的跨后端索引赋值能力。

## 4. 核心概念与源码讲解

### 4.1 step_direction 语义与 hdir 符号化

#### 4.1.1 概念说明

`derivative` 默认用**中心差分**：在 `x` 左右两侧各取若干点。这要求 `f` 在 `x` 两侧都能求值。但很多真实函数定义域受限——比如 `f(x) = log(x)` 只在 `x>0` 有定义、一个分段函数在 `x<0` 或 `x>2` 返回 `NaN`。对这种函数，在边界附近做中心差分，第一轮的求值点 `x ± initial_step` 就可能跨出定义域，得到一堆 `NaN`，最终触发 `status=-3` 失败。

`step_direction` 就是用来告诉算法「求值点只能往哪边走」的开关。它的语义在 docstring 里写得非常精炼：

> Where 0 (default), central differences are used; where negative (e.g. -1), steps are non-positive; and where positive (e.g. 1), all steps are non-negative.

翻译成一句话：

| `step_direction` | 含义 | 求值点位置 | 通俗叫法 |
| --- | --- | --- | --- |
| `0` | 中心差分 | `x` 左右对称 | central |
| 负数（如 `-1`） | 步长非正 | 全在 `x` 左侧（`≤ x`） | 向后/左差分（backward/left） |
| 正数（如 `+1`） | 步长非负 | 全在 `x` 右侧（`≥ x`） | 向前/右差分（forward/right） |

注意三点：

1. **只看正负号，不看大小**。`step_direction=-1` 和 `step_direction=-100` 等价，源码会先取 `sign`。
2. **可以逐元素不同**。`step_direction` 是数组，能与 `x` 广播，于是同一个 `derivative` 调用里，左边界点用向前差分、右边界点用向后差分、中间点用中心差分——一次搞定。
3. **单侧差分仍然自适应**。它依然走「大步长起步、逐轮缩小、相邻两轮估计之差作误差」的同一套迭代，只是求值点的几何排布从对称变成单侧。

#### 4.1.2 核心流程

`step_direction` 从用户输入到生成求值点，经过四步：

```
用户传入 step_direction（标量或数组，可与 x 广播）
        │
        ▼  [_derivative_iv]  广播到与 x 同形，重命名为 hdir
   hdir（仍是原始数值，如 -1 / 0 / 1）
        │
        ▼  [derivative 主流程]  broadcast_to(shape) → reshape(-1) → sign()
   hdir ∈ {-1, 0, 1}（符号化，浮点 dtype）
        │
        ▼  派生三类布尔掩码
   il = hdir < 0   ic = hdir == 0   ir = hdir > 0   io = il | ir
        │
        ▼  [pre_func_eval]  按掩码把求值点写进 x_eval 的对应行
   左行：x - hr      中行：x + hc      右行：x + hr
```

关键在于第二步的**符号化**：无论用户写 `-1` 还是 `-7`，`sign()` 都把它压成 `-1`；正数压成 `+1`；`0` 保持 `0`。这样后续只需用三个布尔掩码 `il/ic/ir` 做分支，逻辑极其干净。

#### 4.1.3 源码精读

**第一步：docstring 语义定义。** 参数说明就在签名下方：

[scipy/differentiate/_differentiate.py:126-132](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L126-L132) —— 定义 `step_direction` 的三向语义，并强调它「用于 `x` 靠近函数定义域边界时」。

**第二步：输入校验层的广播与重命名。** 在 `_derivative_iv` 中，`step_direction` 被转成数组并与 `x`、`initial_step` 三者广播到同形，返回时被重命名为内部短名 `hdir`：

[scipy/differentiate/_differentiate.py:44-47](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L44-L47) —— `step_direction = xp.asarray(step_direction)`，再与 `x`、`initial_step` 一起 `broadcast_arrays`，确保三者形状一致。

注意：校验层**不**做 `sign`，只管形状；符号化留给主流程（这是 u2-l1 讲过的「校验层只管合法性与形状，语义处理交给主流程」原则）。

**第三步：主流程的符号化。** 解包出 `hdir` 后，主流程把它展平并取符号：

[scipy/differentiate/_differentiate.py:411-413](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L411-L413) —— `hdir = xp.broadcast_to(hdir, shape)` → `reshape((-1,))` → `astype(xp.sign(hdir), dtype)`。这三行把任意数值的 `step_direction` 压成 `{-1, 0, +1}` 并展平成一维，与 `x` 的展平维度对齐。

**第四步：派生三类布尔掩码。** 紧接着用符号化的 `hdir` 生成四个掩码，它们会贯穿后续所有钩子：

[scipy/differentiate/_differentiate.py:422-425](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L422-L425) —— `il = hdir < 0`（左）、`ic = hdir == 0`（中）、`ir = hdir > 0`（右）、`io = il | ir`（所有单侧）。这四个掩码随后被挂到 `work` 对象上（见 `work` 的构造，[L434-L441](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L434-L441)），供 `pre_func_eval` / `post_func_eval` 在每轮迭代中反复使用。

> 一个常被忽略的细节：`hdir` 被转成了**浮点 dtype**（`astype(..., dtype)`，`dtype` 是 `x` 的浮点类型），而不是整型。这是因为后续 `work.x[ir][:, xp.newaxis] + hr[ir]` 这类运算需要 `hdir` 参与的中间量与 `x` 同 dtype；真正的「分类」工作由布尔掩码承担，`hdir` 本身的数值在符号化后不再有歧义。

#### 4.1.4 代码实践

**实践目标**：直观验证「`step_direction` 只看正负号」，以及三类掩码如何切分元素。

**操作步骤**：

1. 阅读上面的四段源码，确认符号化发生在主流程 L413，而非校验层。
2. 写一小段脚本，分别用 `step_direction=-1`、`-5`、`0`、`3` 调用 `derivative`，比较结果是否一致。
3. 再用数组 `step_direction=[-1, 0, 1]` 一次性求三处导数，观察返回 `df` 的形状。

```python
# 示例代码：验证 step_direction 只看正负号
import numpy as np
from scipy.differentiate import derivative

f = np.exp
x = 1.0
for sd in [-1, -5, 0, 3]:
    res = derivative(f, x, step_direction=sd, maxiter=1, order=2)
    print(f"sd={sd:>2}  df={res.df:.6f}")

# 逐元素方向：一次调用同时算左/中/右三种差分
res = derivative(f, np.array([1.0, 1.0, 1.0]),
                 step_direction=np.array([-1, 0, 1]), maxiter=1, order=2)
print("逐元素 df =", res.df)   # 形状 (3,)，分别对应左/中/右
```

**需要观察的现象**：`sd=-1` 与 `sd=-5` 的 `df` 完全相同；`sd=0` 与 `sd=3` 各自不同（中心差分 vs 单侧差分在 `maxiter=1` 时精度不同）。逐元素调用返回长度为 3 的数组。

**预期结果**：三处 `df` 都近似 `e ≈ 2.71828`，但单侧差分（`sd=±1`，`order=2`、一轮）的误差比中心差分（`sd=0`）大。准确数值「待本地验证」，但其相对大小关系是确定的。

#### 4.1.5 小练习与答案

**练习 1**：如果用户传 `step_direction=0.0`（浮点零），会被归到哪一类？为什么不会被 `ic = hdir == 0` 漏掉？

**答案**：归到 `ic`（中心）。`sign(0.0) == 0.0`，而 `hdir == 0` 对 `0.0` 成立，故 `ic` 为真。符号化保证了「零就是中心」，无论它是整数 0 还是浮点 0.0。

**练习 2**：源码为何在 `_derivative_iv`（L44-L47）只做广播、不做 `sign`，而把 `sign` 推迟到主流程 L413？

**答案**：校验层的职责是「合法性与形状」（u2-l1 原则）。`sign` 是语义处理，依赖最终的展平 `shape`（由 `eim._initialize` 决定），而 `shape` 在校验层尚未确定；放到主流程、在 `_initialize` 之后做，才能正确地 `broadcast_to(shape)`。

---

### 4.2 中心 vs 单侧差分：求值点几何与符号纠正

#### 4.2.1 概念说明

`step_direction` 的三个方向，本质是三套**不同的 stencil 几何**。回顾 u2-l2、u2-l3：

- **中心 stencil**：关于 `x` 对称，点按公比 \(1/c\)（\(c\) = `step_factor`）几何排布，例如
  \[
  \ldots,\ x-h c,\ x-h,\ x,\ x+h,\ x+h c,\ \ldots
  \]
  权重具反对称性（`weights[-i-1] = -weights[i]`），中心点权重强制清零。
- **右单侧 stencil**：所有点在 `x` 右侧，按公比 \(1/d\) 排布（\(d=\sqrt{c}\)），例如
  \[
  x,\ x+h,\ x+h/d,\ x+h/d^{2},\ \ldots
  \]
  用 \(d=\sqrt{c}\) 是为了让单侧 stencil 每轮「丢 2 点、加 2 点」的节奏与中心 stencil 同步。
- **左单侧 stencil**：源码**不单独计算**左权重，而是复用右 stencil 的求值点偏移 `hr` 但整体取负（求值点变成 `x - hr`），最后对 `df` 乘 `-1` 纠正符号。这是本讲最巧妙的一处工程复用。

为什么要乘 `-1`？因为「左差分」等价于「对反射函数做右差分」。设 \(g(t)=f(2x-t)\)，则 \(g'(x)=-f'(x)\)。右差分公式作用在 \(g\) 上、用点 \(x, x+h, x+h/d,\ldots\) 得到的是 \(g'(x)\)；而这些点处的 \(g\) 值 \(g(x+h)=f(x-h)\)，恰好等于在 \(f\) 的点 \(x, x-h, x-h/d,\ldots\) 上求值。所以：**对 \(f\) 用左偏求值点 + 右权重，得到的是 \(-f'(x)\)，必须乘 \(-1\) 还原。**

#### 4.2.2 核心流程

每轮迭代，`pre_func_eval` 按 `il/ic/ir` 三个掩码，把对应行的求值点分别写入 `x_eval`：

```
对每一行（一个活跃元素 x_i）：
  if ir[i]（右）: x_eval[i] = x_i + hr      # 全部 ≥ x_i，向前
  if ic[i]（中）: x_eval[i] = x_i + hc      # 对称，含 ±hc
  if il[i]（左）: x_eval[i] = x_i - hr      # 全部 ≤ x_i，向后
```

其中 `hc` 是中心偏移序列（含正负，对称），`hr` 是单侧偏移序列（恒正）。注意左行复用的是 `hr`（右偏移），只是前面加了负号。

随后 `f(x_eval)` 一次性求出所有点的函数值。`post_func_eval` 用对应权重加权得到 `df`，并对 `il` 行乘 `-1`：

```
df[ic] = (fc @ wc) / h        # 中心：对称权重 wc
df[io] = (fo @ wo) / h        # 单侧：右权重 wo（左行暂得 -f'）
df[il] *= -1                  # 左行符号纠正 → 得到 +f'
```

#### 4.2.3 源码精读

**求值点生成的方向分支**，集中在 `pre_func_eval` 末尾几行：

[scipy/differentiate/_differentiate.py:487-493](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L487-L493) —— 先分配全零的 `x_eval`，再用 `xpx.at(...)[ir/ic/il].set(...)` 按掩码分别写入：右行 `x + hr`、中行 `x + hc`、左行 `x - hr`。`xpx.at` 是跨后端的「掩码索引赋值」接口（u4-l4 详讲），保证不可变后端（如 Torch/JAX）也能用。

> 注意三行 `set` 的顺序：`ir` → `ic` → `il`。由于三类掩码两两互斥（一个元素不可能同时 `<0` 和 `==0`），顺序不影响结果，写法只是按「右中左」自然排列。

**`hc` 与 `hr` 的构造**（同函数上半部分，[L476-L485](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L476-L485)）：首轮 `hc` 由 `h/c**arange(n)` 经翻转拼接成对称序列（含正负），`hr` 由 `h/d**arange(2n)` 给出（恒正）。后续轮两者都只补 2 个最内侧新点。`hc` 自带正负号（对称），`hr` 恒正——这正是「中心行直接 `x+hc`、单侧行需要决定加还是减」的根因。

**符号纠正**，在 `post_func_eval` 的加权之后：

[scipy/differentiate/_differentiate.py:547-549](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L547-L549) —— `df[ic] = fc @ wc / h`（中心）、`df[io] = fo @ wo / h`（单侧，含左右）、`df[il] *= -1`（左行符号纠正）。这一行 `multiply(-1)` 就是 4.2.1 推导的「乘 \(-1\) 还原」的落点。

**docstring 里的三向对比示例**，正是用 `hdir=[-1, 0, 1]` 一次性画左/中/右三条误差曲线：

[scipy/differentiate/_differentiate.py:268-291](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L268-L291) —— 对 `np.exp` 在 `x=1` 用 `step_direction=[-1,0,1]`、不同 `maxiter` 求导，画出三种差分的误差随迭代下降曲线。结论是：在函数性态良好的点上，三种方向最终都收敛到同一个真值，只是中心差分精度更高、收敛更快。

#### 4.2.4 代码实践

**实践目标**：亲眼看到「同一函数、同一 `x`、三个方向最终殊途同归」，并量化中心差分的精度优势。

**操作步骤**：复现 docstring 示例 L268-L291 的简化版。

```python
# 示例代码：左/中/右三向误差对比（改编自 docstring 示例）
import numpy as np
from scipy.differentiate import derivative

f = np.exp
x = 1.0
ref = f(x)                      # exp 的导数仍是 exp
hfac = 2                        # step_factor
order = 4

errors = []
for i in range(1, 8):
    res = derivative(f, x, maxiter=i, step_factor=hfac,
                     step_direction=[-1, 0, 1], order=order,
                     tolerances=dict(atol=0, rtol=0))   # 关掉早停，跑满
    errors.append(np.abs(res.df - ref))
errors = np.array(errors)       # 形状 (7, 3)：列 0=左, 1=中, 2=右

print("最后一轮三向误差:", errors[-1])
print("中心 vs 左 的误差比:", errors[-1, 1] / errors[-1, 0])
```

**需要观察的现象**：三列误差都随迭代单调下降；中心列（列 1）显著小于左右两列；左右两列数值接近。

**预期结果**：中心差分误差比单侧差分小若干个数量级（`order=4` 时，中心是 4 阶、单侧也是偶数阶但常数项不利）。具体比值「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么左单侧 stencil 不需要单独计算权重，复用右权重加一个 `*= -1` 就行？请用反射函数 \(g(t)=f(2x-t)\) 解释。

**答案**：见 4.2.1。右权重作用于反射函数 \(g\) 的右偏点等于作用于 \(f\) 的左偏点，得到 \(g'(x)=-f'(x)\)，故乘 \(-1\) 还原为 \(f'(x)\)。这避免了为左 stencil 再解一次 Vandermonde 方程组。

**练习 2**：`hc`（中心偏移）自带正负号，`hr`（单侧偏移）恒正。如果某行同时被 `ic` 和 `ir` 选中会怎样？

**答案**：不可能。`ic = (hdir==0)` 与 `ir = (hdir>0)` 互斥，`sign` 的输出三值互不相交，故每个元素恰好落入 `il/ic/ir` 之一，三类 `set` 不会冲突。

---

### 4.3 边界域求导实践

#### 4.3.1 概念说明

现在把前两个模块用到真正的痛点上：**定义域受限的函数**。典型场景：

- \(f(x)=\log(x)\)：定义域 \(x>0\)，在 \(x\) 很小处不能往左探。
- 分段函数：只在 \([0,2]\) 有效，区间外返回 `NaN`。
- 带平方根 \(f(x)=\sqrt{x}\)：定义域 \(x\ge 0\)。

在边界附近，中心差分会把求值点甩到区间外，函数返回 `NaN`，`check_termination` 判 `status=-3`（u2-l5）。解决方法：**让求值点只往区间内侧走**——左边界用 `step_direction=+1`（向前差分，点全在右侧），右边界用 `step_direction=-1`（向后差分，点全在左侧），中间安全区域继续用 `0`（中心差分，精度最高）。

但要小心一个**步长上限**约束：单侧差分第一轮的最远求值点距 `x` 为 `initial_step`（因为首轮最大偏移就是 \(h_0\)，见 4.3.2）。所以还必须保证 \(x \pm \text{initial\_step}\) 仍在定义域内。如果默认 `initial_step=0.5` 太大、会跨出边界，就要同时调小 `initial_step`。

#### 4.3.2 核心流程

边界域求导的配置三件套：

```
1. 判断每个 x 相对定义域 [lo, hi] 的位置：
     靠近 lo（左边界） → step_direction = +1   （向前，避开 lo）
     靠近 hi（右边界） → step_direction = -1   （向后，避开 hi）
     中间安全区        → step_direction =  0   （中心，精度最高）
2. 确保 initial_step 不跨出边界：
     向前差分要求  x + initial_step ≤ hi
     向后差分要求  x - initial_step ≥ lo
   必要时逐元素调小 initial_step。
3. 调用 derivative(f, x, step_direction=..., initial_step=...)
   检查 res.success 全为 True、res.df 有限且正确。
```

为什么首轮最大偏移恰是 `initial_step`？看 `pre_func_eval`：首轮 `hr = h / d**arange(2n)`，首元素 \(h/d^0 = h = h_0 =\) `initial_step` 是最大值；`hc` 同理首元素为 \(h_0\)。后续轮步长只减不增（每轮 `h /= fac`）。所以**全程最远的求值点出现在第一轮，距 `x` 恰为 `initial_step`**——只需校核这一轮即可。

> 还有一个保护：若用户传了非正的 `initial_step`，主流程会把它置成 `NaN`：
> [scipy/differentiate/_differentiate.py:417](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L417) —— `h0 = xpx.at(h0)[h0 <= 0].set(xp.nan)`。该 `NaN` 会顺着步长传进求值点，最终被 `check_termination` 的非有限值检查判为 `status=-3`。所以 `initial_step` 必须严格为正。

#### 4.3.3 源码精读

**docstring 明确点出 `step_direction` 的边界用途**：

[scipy/differentiate/_differentiate.py:126-132](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L126-L132) —— 「for use when `x` lies near to the boundary of the domain of the function」，并给出 `0/-1/+1` 三向语义。

**测试 `test_step_direction` 是最佳范例**：定义一个在 `x<0` 或 `x>2` 返回 `NaN` 的「截断指数」，用分段 `step_direction`（左段 `+1`、右段 `-1`、中段 `0`）一次调用求全程导数：

[scipy/differentiate/tests/test_differentiate.py:207-220](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L207-L220) —— `f` 用 `xpx.at(y)[(x<0)+(x>2)].set(nan)` 制造截断；`step_direction` 默认全 0，再把 `x<0.6` 段置 `+1`、`x>1.4` 段置 `-1`；断言 `res.df ≈ exp(x)` 且 `res.success` 全真。这正是「同一次调用、不同点用不同方向」的范本。

**测试 `test_step_direction_size` 进一步演示「方向 + 步长」双约束**，用于 `jacobian` 上保证扰动不跨出每个坐标的可用子域：

[scipy/differentiate/tests/test_differentiate.py:616-637](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L616-L637) —— 对三个坐标分别设 `dir=[1,-1,0]`、`h0=[0.25, 0.1, 0.5]`，使每个坐标的扰动都留在各自合法的窄窗口内。这说明 `step_direction` 与 `initial_step` 都可逐元素配置，组合起来能精确贴合任意定义域形状。

#### 4.3.4 代码实践

**实践目标**：复现 `test_step_direction`，亲手验证「分段 `step_direction` 能让截断函数全程求导成功」。

**操作步骤**：

1. 定义截断指数：在 \([0,2]\) 内等于 `exp(x)`，区间外返回 `NaN`。
2. 构造分段 `step_direction`：左段（`x<0.6`）取 `+1`、右段（`x>1.4`）取 `-1`、中段取 `0`。
3. 调用 `derivative`，断言 `df ≈ exp(x)` 且全部 `success`。
4. **对照实验**：把 `step_direction` 全置 `0`（强行中心差分），观察边界点变成 `NaN`、`success` 变 `False`。

```python
# 示例代码：复现 test_step_direction
import numpy as np
from scipy.differentiate import derivative

def f(x):
    y = np.exp(x)
    y = np.where((x < 0) | (x > 2), np.nan, y)   # 区间外截断为 NaN
    return y

x = np.linspace(0, 2, 10)

# 分段方向：左边界向前、右边界向后、中间中心
sd = np.zeros_like(x)
sd[x < 0.6] = 1      # 靠近左边界 0 → 向前，避开 x<0
sd[x > 1.4] = -1     # 靠近右边界 2 → 向后，避开 x>2

res = derivative(f, x, step_direction=sd)
print("df      =", res.df)
print("exp(x)  =", np.exp(x))
print("success =", res.success)
print("最大误差 =", np.max(np.abs(res.df - np.exp(x))))

# 对照：强行全部用中心差分 → 边界点应失败
res0 = derivative(f, x, step_direction=0)
print("中心差分 success =", res0.success)   # 预期首尾出现 False
```

**需要观察的现象**：分段方向的 `res.success` 全为 `True`，`df` 与 `exp(x)` 几乎重合；对照实验中 `res0.success` 在最左/最右几个点为 `False`，对应 `df` 为 `NaN`。

**预期结果**：分段版本最大误差在默认容差量级（约 `1e-12` 或更小，具体「待本地验证」）；中心差分对照版本在 `x=0` 和 `x=2` 附近出现 `NaN`。

**进阶操作**：把 `initial_step` 改大（如 `initial_step=1.0`）再跑分段版本，观察左边界点（`x=0`，向前差分需 `0+1.0=1.0≤2` 仍合法）是否仍成功、而若把截断区间缩窄到 `[0, 0.5]` 会怎样失败，从而体会「方向 + 步长」缺一不可。

#### 4.3.5 小练习与答案

**练习 1**：对 \(f(x)=\log(x)\) 在 `x=1e-3` 求导，应该用什么 `step_direction`？还需要调 `initial_step` 吗？

**答案**：用 `step_direction=+1`（向前差分），因为左侧 `x<0` 无定义。默认 `initial_step=0.5` 会让首个求值点 `1e-3 + 0.5` 仍在定义域内（`>0`），所以方向对了即可，无需调步长。但若 `x` 极小（如 `1e-12`）且关心相对精度，可适当减小 `initial_step` 以提高分辨率。

**练习 2**：`test_step_direction_size` 里为何三个坐标用了**不同**的 `initial_step`（`0.25, 0.1, 0.5`）？

**答案**：因为每个坐标的可用子域宽度不同（测试中用 `b[i]` 和 `b[i]±0.25/0.1` 限定），`initial_step` 必须小于「当前坐标到最近边界的有向距离」，否则第一轮最远求值点就跨出合法区。逐元素 `initial_step` 让每个坐标都能贴着各自的边界窗口安全求导。

**练习 3**：如果某个边界点既设了正确的 `step_direction=+1`，却仍返回 `status=-3`，最可能的原因是什么？

**答案**：`initial_step` 过大，导致首轮最远求值点 \(x+\text{initial\_step}\) 跨出右边界、函数返回 `NaN`。回顾 L417，非正步长也会被置 `NaN`；但此处方向正确却失败，主因是步长越界。解决办法是调小 `initial_step`。

## 5. 综合实践

把三个模块串起来，完成一个「定义域不规则函数的逐元素求导」任务。

**任务**：函数 \(f(x)=\sqrt{x}\,(x-2)^{2}\) 在生产环境中只能保证 \(x\in[0, 4]\) 内有效，区间外上游会返回 `NaN`。请在 `x = np.linspace(0, 4, 13)`（含两个端点）上一次性求出导数，要求全程成功收敛。

**参考步骤**：

1. 写出解析导数作为基准：\(f'(x)=\frac{(x-2)^{2}}{2\sqrt{x}} + 2\sqrt{x}(x-2)\)。
2. 定义截断版 `f`：区间外返回 `NaN`。
3. 设计分段 `step_direction`：靠近左边界 `0` 的点用 `+1`、靠近右边界 `4` 的点用 `-1`、中间用 `0`。注意 \(x=0\) 处 \(\sqrt{x}\) 导数本身发散，可单独剔除或改用更大 `atol`。
4. 必要时为端点配一个较小的 `initial_step`（例如端点处 `initial_step=0.2`，保证 \(0+0.2\) 与 \(4-0.2\) 都在域内）。
5. 调用 `derivative(f, x, step_direction=sd, initial_step=h0)`，打印 `res.df`、`res.success`、与解析导数的误差。

**自检问题**：

- 如果把所有点都设成 `step_direction=0`，哪些点会失败？为什么？
- 端点 `x=0` 处导数理论上是 \(+\infty\)，`derivative` 会给出什么 `status`？这和 u2-l5 讲的「非有限值终止」如何对应？

```python
# 示例代码：综合实践骨架（需读者补全方向与步长）
import numpy as np
from scipy.differentiate import derivative

def f(x):
    y = np.sqrt(x) * (x - 2) ** 2
    return np.where((x < 0) | (x > 4), np.nan, y)

def df_true(x):
    return (x - 2) ** 2 / (2 * np.sqrt(x)) + 2 * np.sqrt(x) * (x - 2)

x = np.linspace(0, 4, 13)

# TODO: 构造 sd（分段方向）与 h0（端点处较小步长）
# sd = ...
# h0 = ...

# res = derivative(f, x, step_direction=sd, initial_step=h0)
# print(res.success, res.df)
```

> 提示：`x=0` 处 \(f'(0)\) 发散，数值上会得到很大的有限值或触发误差回升（`status=-1`）/非有限值（`status=-3`），这是函数本身的奇点所致，而非 `step_direction` 配置错误。可从 `x[1:]` 开始评价收敛质量。

## 6. 本讲小结

- `step_direction` 是「求值点方向」开关：`0` 中心差分、负数向后（步长非正）、正数向前（步长非负），**只看正负号**，可逐元素配置。
- 源码把任意数值的 `step_direction` 经 `sign()` 符号化为 `hdir ∈ {-1,0,+1}`，再派生 `il/ic/ir/io` 四个布尔掩码驱动后续分支；符号化发生在主流程 L413，而非校验层。
- 三向对应三套 stencil 几何：中心对称（`hc` 含正负）、单侧恒正（`hr`）；左单侧**复用** `hr` 并对求值点取负（`x - hr`），最后对 `df` 乘 `-1` 纠正符号（反射函数 trick，L549）。
- 边界域求导的关键是「方向 + 步长」双约束：方向避开越界、`initial_step` 不大于到最近边界的有向距离（首轮最远点距 `x` 恰为 `initial_step`）；非正步长会被 L417 置 `NaN` 进而判 `-3`。
- `test_step_direction` 与 `test_step_direction_size` 是最佳范本：前者展示「同一次调用、不同点不同方向」，后者展示「方向 + 逐元素步长」精确贴合任意定义域。

## 7. 下一步学习建议

- **衔接 u4-l3（数值精度与调参）**：单侧差分的精度通常低于中心差分，在边界附近要特别留意截断误差与消去误差的权衡；本讲的 `initial_step` 选择正是调参的核心抓手。
- **衔接 u4-l1（向量化与 preserve_shape）**：`step_direction` 的逐元素广播与 `preserve_shape` 模式经常联合使用（例如向量值函数在受限定义域上求导），建议把两者放在一起做一次综合练习。
- **回到源码**：通读 `pre_func_eval`（[L449-L493](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L449-L493)）与 `post_func_eval`（[L495-L560](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L495-L560)），把 u2-l2（权重）、u2-l3（求值点）、本讲（方向）三张图叠成一张完整的「单次迭代」心智模型。
- **延伸阅读**：`jacobian` 与 `hessian` 也接受 `step_direction`（见 `jacobian` 签名 [L723-L724](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L723-L724)），可在多变量受限定义域上复用本讲的全部技巧。
