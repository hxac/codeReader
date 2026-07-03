# dendrogram 可视化与配色

## 1. 本讲目标

`scipy.cluster.hierarchy.dendrogram` 是把一棵层次聚类树（linkage matrix `Z`）画成「系统树图」的入口函数。学完本讲，你应该能够：

1. 看懂 `dendrogram` 返回字典 `R` 里的 `icoord` / `dcoord` / `ivl` / `color_list` / `leaves_color_list` 这些绘图数据结构各自代表什么。
2. 理解 `color_threshold` 是如何在「阈值线」之下自动给每个子树（即每个 flat 簇）涂上不同颜色，并掌握 `0.7 * max(Z[:,2])` 这个 MATLAB 风格的默认阈值。
3. 会用 `set_link_color_palette` 自定义调色板、用 `above_threshold_color` 改阈值之上链接的颜色。
4. 看清 `truncate_mode='lastp'` / `'level'` 两种截断模式如何把大树「折叠」成小树以便阅读。
5. 跟踪 `_dendrogram_calculate_info` 的递归过程，理解一个 U 形链接的四条线段坐标是如何算出来的。

## 2. 前置知识

本讲承接 [u6-l1](u6-l1-clusternode-and-tree.md)，默认你已经掌握：

- **linkage matrix `Z`**：形状为 (n−1)×4，四列依次是「被合并的两簇编号、合并距离、新簇成员数」；簇编号遵循 n+i 约定，原始观测占 0…n−1，第 i 步合并出的新簇编号为 n+i，根节点固定为 2n−2（见 [u3-l1](u3-l1-linkage-matrix.md)）。
- **cophenetic 距离**：两个原始观测在树上首次并入同一簇时的合并高度，即它们 LCA 节点的 `Z[:,2]`（见 [u5-l4](u5-l4-cophenet.md)）。
- **前序遍历 / 递归**：从根出发、先左后右地访问节点的过程（见 [u6-l1](u6-l1-clusternode-and-tree.md) 的 `leaves_list`）。

补充几个本讲会用到的术语：

- **U 形链接（U-link）**：dendrogram 里每一个非叶子节点都画成一个倒 U（orientation='top' 时）：两条竖腿 + 一条横杠。横杠的高度 = 该节点合并距离 `Z[i,2]`；两条竖腿的长度 = 两个孩子到该节点的距离。这正是 cophenetic 距离的可视化。
- **调色板（palette）**：一组 matplotlib 颜色字符串（如 `'C1'`、`'m'`、`'#bcbddc'`），低于 `color_threshold` 的子树按出现顺序循环取色。
- **matplotlib collection**：把多条同色线段打包成一个图元对象，这样图例只会为「每种颜色」生成一个条目，而不是每条线段一个。

## 3. 本讲源码地图

本讲涉及的代码全部集中在一个文件里：

| 文件 | 作用 |
|------|------|
| [hierarchy/_hierarchy_impl.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py) | `dendrogram` 公开入口、`_dendrogram_calculate_info` 递归算坐标、`_plot_dendrogram` 渲染、`set_link_color_palette` 调色板、`_get_leaves_color_list` 叶子配色，全部在此 |

值得注意：与 `linkage`/`fcluster` 不同，`dendrogram` 及其辅助函数**不依赖 Cython 后端**，全部是纯 Python 实现——因为绘图需要的是灵活的递归与字符串拼接，而非数值热点循环。

---

## 4. 核心概念与源码讲解

### 4.1 dendrogram 公开入口：校验、默认配色阈值与返回字典

#### 4.1.1 概念说明

`dendrogram(Z, ...)` 是面向用户的「前门」。它本身不做递归绘图，而是承担三类职责：

1. **输入校验**：检查 `Z` 是不是合法 linkage matrix、`orientation` / `truncate_mode` 取值是否合法、`labels` 长度是否与 `Z` 一致。
2. **设置默认值**：把 `color_threshold=None` 翻译成 `0.7 * max(Z[:,2])`，把 `truncate_mode` 别名（`'mtica'`→`'level'`）归一化。
3. **拼装返回字典 `R`**：准备空列表 `icoord_list` / `dcoord_list` / `color_list` / `ivl` / `lvs`，调用 `_dendrogram_calculate_info` 填充它们，再（可选地）调用 `_plot_dendrogram` 真正画图。

#### 4.1.2 核心流程

```
dendrogram(Z, p, truncate_mode, color_threshold, ...)
  ├─ 校验 Z / orientation / labels / truncate_mode
  ├─ 归一化 truncate_mode（'mtica'→'level'，p 上限为 n）
  ├─ 若 color_threshold 为 None 或 'default'：color_threshold = 0.7 * max(Z[:,2])
  ├─ 构造空字典 R = {icoord, dcoord, ivl, leaves, color_list}
  ├─ _dendrogram_calculate_info(i=2n-2, ...)   # 从根开始递归，填充上述列表
  ├─ if not no_plot: _plot_dendrogram(...)     # 真正画图（需 matplotlib）
  └─ R['leaves_color_list'] = _get_leaves_color_list(R)
     return R
```

注意递归从根节点 `i = 2*n - 2` 开始（[L3370](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3370)），独立变量初值 `iv = 0.0`（叶子从 x=5 开始放，每个叶子占 10 单位宽）。

#### 4.1.3 源码精读

公开入口签名与装饰器：[hierarchy/_hierarchy_impl.py:L3024-L3031](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3024-L3031) 声明 `dendrogram(Z, p=30, truncate_mode=None, color_threshold=None, ...)`，并用 `@xp_capabilities(cpu_only=True, ...)` 标注。参数 `p=30` 是 `truncate_mode` 的配套参数（截断时显示多少）。

关键参数校验与归一化：[hierarchy/_hierarchy_impl.py:L3322-L3336](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3322-L3336) 校验 `truncate_mode` 只能取 `('lastp','mtica','level','none',None)`，把 `'mtica'` 别名重写为 `'level'`，并在 `lastp` 模式下把 `p` 上限钳制为 `n`（`p > n or p == 0` 时 `p = n`）。

默认配色阈值：[hierarchy/_hierarchy_impl.py:L3350-L3352](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3350-L3352)

```python
if color_threshold is None or (isinstance(color_threshold, str) and
                               color_threshold == 'default'):
    color_threshold = xp.max(Z[:, 2]) * 0.7
```

这就是 MATLAB 风格的 `0.7 * max` 规则：在最高合并高度的 70% 处画一条隐形横线，线下的子树自动获得不同颜色。

返回字典的构造与填充：[hierarchy/_hierarchy_impl.py:L3354-L3383](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3354-L3383) 先建空字典 `R`，再把这些可变列表传给 `_dendrogram_calculate_info`，递归函数直接往里 append（典型的「用可变容器做累加器」模式，避免递归拼大列表的性能开销）。

可选绘图与叶子配色：[hierarchy/_hierarchy_impl.py:L3385-L3397](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3385-L3397)。当 `no_plot=True` 时跳过 `_plot_dendrogram`——这样在无 matplotlib 的环境（如纯计算 / 文档生成）里也能拿到坐标数据。最后用 `_get_leaves_color_list(R)` 反推出每个叶子的颜色并塞进 `R`。

#### 4.1.4 代码实践

**实践目标**：在不画图的前提下，观察 `dendrogram` 返回字典的结构。

**操作步骤**（在能 `import scipy` 的环境里）：

```python
import numpy as np
from scipy.cluster.hierarchy import linkage, dendrogram

# 6 个观测的压缩距离矩阵（来自 scipy 自带测试数据 ytdist）
ytdist = np.array([662., 877., 255., 412., 996., 295.,
                   468., 268., 400., 754., 564., 138.,
                   219., 869., 669.])
Z = linkage(ytdist, 'single')

R = dendrogram(Z, no_plot=True)   # no_plot=True：只算坐标，不画图
print(R.keys())
print('ivl  =', R['ivl'])          # 叶子标签，从左到右
print('leaves =', R['leaves'])     # 叶子对应的簇编号
print('color_list =', R['color_list'])
print('icoord[0] =', R['icoord'][0])   # 第一条 U 形链接的 4 个 x 坐标
print('dcoord[0] =', R['dcoord'][0])   # 第一条 U 形链接的 4 个 y 坐标
```

**预期结果**：`ivl = ['2', '5', '1', '0', '3', '4']`，`leaves = [2, 5, 1, 0, 3, 4]`，`color_list` 中只有第一条链接是 `'C1'`（阈值之下的彩色子树），其余是 `'C0'`（阈值之上的默认色）。`icoord[0] = [5.0, 5.0, 15.0, 15.0]`、`dcoord[0] = [0.0, 138.0, 138.0, 0.0]`——这正是官方测试 [test_dendrogram_plot](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/tests/test_hierarchy.py#L980) 断言的固定值。

> 待本地验证：若你的 scipy 版本或默认随机设置不同，`color_list` 的具体颜色字符串可能因默认 `color_threshold` 而略有差异，但 `icoord` / `dcoord` 这组固定数据应一致。

#### 4.1.5 小练习与答案

**练习 1**：把上面的例子改为 `dendrogram(Z, no_plot=True, color_threshold=0)`，`color_list` 会变成什么？为什么？

**参考答案**：全部变成 `'C0'`（`above_threshold_color` 默认值）。因为源码里 `if h >= color_threshold or color_threshold <= 0:`（[L3659](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3659)），当 `color_threshold <= 0` 时，所有链接都被判为「阈值之上」，统一上 `C0`。

**练习 2**：为什么 `dendrogram` 把 `icoord_list` 等作为可变参数传给递归函数，而不是靠返回值拼装？

**参考答案**：递归深度可达 n−1 层，若每层都返回一个大列表再拼接，会产生大量中间列表与 O(n²) 的拷贝。直接往共享的可变列表 append，是 O(1) 累加、O(n) 总开销，且天然保持「孩子在前、父节点在后」的后序追加顺序。

---

### 4.2 _dendrogram_calculate_info：递归计算 U 形坐标

#### 4.2.1 概念说明

`_dendrogram_calculate_info` 是 dendrogram 的「计算心脏」。给定一个节点 `i` 和「画它的子树时，最左侧应从 x=`iv` 开始」这个约束，它完成：

- 确定该节点两个孩子 `ua`（左）、`ub`（右）的绘制顺序（由 `count_sort` / `distance_sort` 控制）。
- 递归处理左、右子树，得到它们各自的中点横坐标 `uiva` / `uivb`、占用宽度 `uwa` / `uwb`、子树高度 `uah` / `ubh`。
- 决定本节点 U 形链接的颜色（见 4.3）。
- 往 `icoord_list` / `dcoord_list` / `color_list` 各 append 一条记录。
- 返回四元组 `(中点横坐标, 总宽度, 本节点高度, 子树最大距离)`，供父节点继续拼接。

#### 4.2.2 核心流程

每条 U 形链接由 4 个点组成（orientation='top' 时，横坐标为 i、纵坐标为 d）：

```
点1 (uiva, uah) ──竖腿── 点2 (uiva, h) ──横杠── 点3 (uivb, h) ──竖腿── 点4 (uivb, ubh)
```

其中 `h = Z[i-n, 2]` 是本节点的合并高度，`uah` / `ubh` 是两个子树的「顶端高度」（叶子为 0）。于是：

```python
icoord_list.append([uiva, uiva, uivb, uivb])   # 4 个 x
dcoord_list.append([uah,  h,    h,    ubh])     # 4 个 y
```

这 4 个点连起来正好是一个倒 U。横杠在高度 `h`，两条竖腿分别从左右子树顶端升到 `h`。叶子节点的 `uah`/`ubh` 为 0，所以最底层链接的两条腿直接落到地面。

#### 4.2.3 源码精读

函数签名与返回契约：[hierarchy/_hierarchy_impl.py:L3477-L3490](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3477-L3490)。注意它有大量默认参数（`i=-1, iv=0.0, n=0, ...`），但公开入口调用时全部显式传入。

确定左右孩子：[hierarchy/_hierarchy_impl.py:L3574-L3589](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3574-L3589) 用 `_int_floor` 把 `Z[i-n, 0]` / `Z[i-n, 1]` 转成整数簇编号 `aa` / `ab`，并取出它们的成员数 `na`/`nb` 与直接孩子距离 `da`/`db`（孩子若是原始观测，则 `na=1, da=0`）。

排序分发（决定谁画左谁画右）：[hierarchy/_hierarchy_impl.py:L3591-L3633](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3591-L3633)。`count_sort='ascending'/True` 让成员少的子树画在左边；`distance_sort='ascending'/True` 让孩子距离小的画在左边；都不开则保持 `ua=aa, ub=ab` 的原始顺序。注意 `count_sort` 与 `distance_sort` 不能同时为真（文档已声明）。

追加 U 形坐标（核心四行）：[hierarchy/_hierarchy_impl.py:L3691-L3704](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3691-L3704)

```python
max_dist = max(uamd, ubmd, h)
icoord_list.append([uiva, uiva, uivb, uivb])
dcoord_list.append([uah, h, h, ubh])
...
return (((uiva + uivb) / 2), uwa + uwb, h, max_dist)
```

返回的「中点横坐标 `(uiva+uivb)/2`」会被父节点用作它那条 U 形腿的横坐标；`uwa + uwb` 是本子树总宽度，父节点据此安排兄弟子树的起始 `iv`（右子树的 `iv = iv + uwa`，见 [L3680](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3680)）。

#### 4.2.4 代码实践

**实践目标**：手工追踪一个 U 形链接的 4 个坐标，验证它与源码公式一致。

**操作步骤**：接 4.1.4 的 `Z`。第一条 append 的链接对应根节点（最后合并，距离 295）。但 `color_list` / `icoord` 是后序追加，所以 `icoord[-1]` 才是根。

```python
# 根节点：i = 2n-2 = 10, h = Z[10-6, 2] = Z[4,2] = 295
print('根链接 icoord =', R['icoord'][-1])
print('根链接 dcoord =', R['dcoord'][-1])
print('根的高度 h   =', Z[-1, 2])          # 295.0
print('左子树顶端 uah =', R['dcoord'][-1][0])  # 应 = 138（簇6子树最高合并）
print('右子树顶端 ubh =', R['dcoord'][-1][3])  # 应 = 268（簇9子树最高合并）
```

**预期结果**：`icoord[-1] = [10.0, 10.0, 33.75, 33.75]`，`dcoord[-1] = [138.0, 295.0, 295.0, 268.0]`。可对照 [测试断言](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/tests/test_hierarchy.py#L988-L993)。横杠在 y=295（根的合并高度），左腿从 138 升到 295，右腿从 268 升到 295——两条腿不等长，正反映了两个子树内部的最大合并高度不同。

> 待本地验证：上述坐标为固定测试数据，应稳定复现。

#### 4.2.5 小练习与答案

**练习**：`_dendrogram_calculate_info` 为什么在递归左子树**之后**、递归右子树**之前**才决定本节点颜色（[L3658-L3672](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3658-L3672) 夹在两次递归之间）？

**参考答案**：因为颜色计数器 `current_color` 是跨整棵树共享的、按「访问顺序」推进的。配色要求「阈值之下的每个连通子树」拿到一种新颜色，而「离开一个下方子树、进入下一个」的时机恰好发生在「刚处理完左子树、正要处理当前节点」这一刻。把着色夹在两次递归之间，能让 `current_color[0] + 1` 在「上一棵子树在阈值下、本节点又回到阈值上」时精确触发，保证每段阈值下子树颜色不重不漏。

---

### 4.3 color_threshold 自动配色与调色板

#### 4.3.1 概念说明

「配色」是 dendrogram 最实用的功能：在 `color_threshold` 高度处想象一条横线，**完全落在线下方**的子树（即一个 flat 簇）被涂成同一种颜色，不同子树颜色不同；横跨或位于线上的链接统一涂 `above_threshold_color`（默认 `'C0'`，matplotlib 第一色）。这其实和 [u5-l1](u5-l1-fcluster-criteria.md) 的 `fcluster(criterion='distance', t=color_threshold)` 切出的簇是一回事——颜色相同的叶子即同属一簇。

#### 4.3.2 核心流程

着色逻辑由两个「单元素列表充当可变状态」驱动（[L3346-L3347](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3346-L3347)）：

- `current_color = [0]`：当前阈值下方子树该用的调色板下标。
- `currently_below_threshold = [False]`：上一次处理的链接是否在阈值之下。

每个节点：

```
h = Z[i-n, 2]                         # 本节点合并高度
if h >= color_threshold or color_threshold <= 0:
    c = above_threshold_color         # 阈值之上 → 默认色
    if currently_below_threshold[0]:  # 若刚从阈值下方「冒上来」
        current_color[0] = (current_color[0] + 1) % len(_link_line_colors)  # 换下一种颜色
    currently_below_threshold[0] = False
else:
    currently_below_threshold[0] = True
    c = _link_line_colors[current_color[0]]   # 阈值之下 → 取当前色
```

用单元素列表 `[0]` 而非整数，是为了在嵌套递归里通过闭包共享同一个可变状态（Python 整数不可变，传 `int` 进递归不会回传）。

调色板默认值：[hierarchy/_hierarchy_impl.py:L2948-L2950](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2948-L2950)

```python
# C0 is used for above threshold color
_link_line_colors_default = ('C1', 'C2', 'C3', 'C4', 'C5', 'C6', 'C7', 'C8', 'C9')
_link_line_colors = list(_link_line_colors_default)
```

注意 `C0` 被刻意排除在调色板之外——它专留给 `above_threshold_color`，避免「阈值下」与「阈值上」撞色。

#### 4.3.3 源码精读

着色判断与计数器推进：[hierarchy/_hierarchy_impl.py:L3658-L3672](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3658-L3672)（代码见 4.3.2）。取模运算 `% len(_link_line_colors)` 保证颜色循环不越界——若子树数超过调色板长度，颜色会重复使用。

自定义颜色优先：[hierarchy/_hierarchy_impl.py:L3695-L3702](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3695-L3702)。若传了 `link_color_func`，则**完全绕开**自动配色，由用户函数按节点 id 决定颜色（并校验返回值必须是字符串）。

`set_link_color_palette` 修改全局调色板：[hierarchy/_hierarchy_impl.py:L2954-L3021](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2954-L3021)

```python
def set_link_color_palette(palette):
    if palette is None:
        palette = _link_line_colors_default      # None → 重置默认
    elif not isinstance(palette, list | tuple):
        raise TypeError("palette must be a list or tuple")
    ...
    global _link_line_colors
    _link_line_colors = palette                  # 改的是模块级全局变量
```

**关键特性（也是坑）**：这是**全局可变状态**——改一次，之后所有 `dendrogram` 调用都受影响。docstring 明确警告「using this function in a multi-threaded fashion may result in dendrogram producing plots with unexpected colors」（[L2983-L2984](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2983-L2984)）。它**只影响阈值之下的链接**（阈值之上始终是 `above_threshold_color`）。相对地，`link_color_func` 是「每次调用、非全局」的，更灵活，官方推荐优先用它。

#### 4.3.4 代码实践

**实践目标**：用 `set_link_color_palette` 换一套自定义颜色，观察 `color_list` 的变化，并验证「阈值之上始终是 `above_threshold_color`」。

**操作步骤**：

```python
import numpy as np
from scipy.cluster.hierarchy import linkage, dendrogram, set_link_color_palette

ytdist = np.array([662., 877., 255., 412., 996., 295., 468., 268.,
                   400., 754., 564., 138., 219., 869., 669.])
Z = linkage(ytdist, 'single')

# 默认调色板
print('默认:', dendrogram(Z, no_plot=True)['color_list'])

# 自定义调色板 + 自定义阈值之上颜色 + 较低阈值
set_link_color_palette(['c', 'm', 'y', 'k'])
R = dendrogram(Z, no_plot=True, above_threshold_color='b', color_threshold=250)
print('自定义:', R['color_list'])     # 期望 ['c', 'm', 'b', 'b', 'b']

# 用完务必重置，避免污染后续调用
set_link_color_palette(None)
```

**预期结果**：自定义那次得到 `['c', 'm', 'b', 'b', 'b']`——前两条链接在阈值 250 之下，分别取 `'c'`、`'m'`；后三条横跨/高于阈值，统一为 `'b'`。这与官方 docstring 示例（[L3000-L3003](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3000-L3003)）及 [test_dendrogram_colors](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/tests/test_hierarchy.py#L1085-L1100) 完全一致。

> 待本地验证：若多线程或 Notebook 中其他单元格改过全局调色板，结果可能受干扰——这正是「用完 `set_link_color_palette(None)` 重置」的原因。

#### 4.3.5 小练习与答案

**练习 1**：为什么调色板默认从 `C1` 开始而不是 `C0`？

**参考答案**：`C0` 被 `above_threshold_color` 占用（[L2948](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2948)）。若阈值之下也用 `C0`，就无法在图上区分「阈值之上的链接」与「阈值之下的第一个子树」。

**练习 2**：若阈值之下有 12 个子树、而调色板只有 9 种颜色，会发生什么？

**参考答案**：`current_color[0] = (current_color[0] + 1) % len(_link_line_colors)`（[L3663](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3663)）会取模回绕，颜色会重复。这意味着靠颜色区分簇不再是单射——这时应改用 `link_color_func` 或调大调色板。

---

### 4.4 truncate_mode：截断显示大树

#### 4.4.1 概念说明

当观测数 n 很大（比如几百几千），完整 dendrogram 的叶子密到无法阅读。`truncate_mode` 把「树底部的细枝末节」折叠成几个代表节点，只展示顶部的粗结构。两种模式：

- `'lastp'`：结果图**最多保留 p 个叶子**。把「较早合并、较深的」非单点簇整体压扁成 1 个叶子（标注它包含多少原始观测，如 `(42)`）。
- `'level'`：从根往下**最多显示 p 层**（每层 = 距最终合并的一次合并），更深的非单点簇压成叶子。

两种模式都只折叠「非单点簇」（原始观测单点永远是叶子），且压扁后的叶子可用 `show_contracted=True` 在其竖腿上画十字标记（contraction marks）示意「这里其实还有内部结构」。

#### 4.4.2 核心流程

截断判断发生在递归进入节点 `i` 的最开头（在展开孩子之前）：

```
# lastp 模式：簇编号 i 在 [n, 2n-p) 区间内的非单点簇 → 折叠成叶子
if truncate_mode == 'lastp' and (2*n - p > i >= n):
    记录为「含多个观测的叶子」，返回 (iv+5, 10, 0, d)

# level 模式：非根(i>n) 且 层数 level > p → 折叠成叶子
elif truncate_mode == 'level' and (i > n and level > p):
    同上
```

折叠成的叶子由 `_append_nonsingleton_leaf_node` 处理：标签默认是 `(成员数)`（`show_leaf_counts=True` 时），或空串。

#### 4.4.3 源码精读

`lastp` 折叠：[hierarchy/_hierarchy_impl.py:L3533-L3548](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3533-L3548)。条件 `2*n - p > i >= n` 表示：簇编号 i 是非单点（i≥n），但又「不够靠后」（i < 2n−p，即不在最后约 p 次合并里），于是压成叶子。`p` 在入口已被钳制为 `≤ n`（[L3326-L3328](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3326-L3328)），所以 `p=n` 等于不截断。

`level` 折叠：[hierarchy/_hierarchy_impl.py:L3549-L3561](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3549-L3561)。`level` 从根的 0 开始，每深入一层 `level + 1`（[L3654](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3654)、[L3687](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3687)）；`i > n and level > p` 时折叠。注意入口处 `if p <= 0: p = np.inf`（[L3334-L3336](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3334-L3336)），即 `'level'` 配 `p<=0` 等于不截断。`'mtica'` 是 `'level'` 的历史别名。

折叠叶子的标签生成：[hierarchy/_hierarchy_impl.py:L3443-L3458](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3443-L3458)

```python
if show_leaf_counts:
    ivl.append("(" + str(np.asarray(Z[i - n, 3], dtype=np.int64)) + ")")
else:
    ivl.append("")
```

`(成员数)` 的写法由此而来——`Z[i-n, 3]` 正是该簇的原始观测数（见 [u3-l1](u3-l1-linkage-matrix.md) 的第四列）。

#### 4.4.4 代码实践

**实践目标**：用 `truncate_mode='lastp', p=2` 把 6 叶树折叠，验证结果只剩 1 条链接、2 个叶子。

**操作步骤**：

```python
import numpy as np
from scipy.cluster.hierarchy import linkage, dendrogram

ytdist = np.array([662., 877., 255., 412., 996., 295., 468., 268.,
                   400., 754., 564., 138., 219., 869., 669.])
Z = linkage(ytdist, 'single')

R = dendrogram(Z, p=2, truncate_mode='lastp', no_plot=True, show_contracted=True)
print('icoord =', R['icoord'])          # 只剩 1 条
print('dcoord =', R['dcoord'])          # [[0.0, 295.0, 295.0, 0.0]]
print('ivl    =', R['ivl'])             # ['(2)', '(4)']：两个折叠叶子
print('leaves =', R['leaves'])          # [6, 9]：被折叠的簇编号
```

**预期结果**：`icoord = [[5.0, 5.0, 15.0, 15.0]]`、`dcoord = [[0.0, 295.0, 295.0, 0.0]]`、`ivl = ['(2)', '(4)']`、`leaves = [6, 9]`。即原本 6 个原始观测被压成 2 个叶子：一个含 2 个观测、一个含 4 个观测，由根（距离 295）合并。这正是 [test_dendrogram_truncate_mode](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/tests/test_hierarchy.py#L1050-L1062) 的断言。

> 待本地验证：该数据为固定测试数据，结果稳定。

#### 4.4.5 小练习与答案

**练习**：`truncate_mode='lastp', p=2` 时，`leaves` 返回 `[6, 9]` 而非原始观测号。`6` 和 `9` 是什么？

**参考答案**：它们是被折叠的**非单点簇编号**（遵循 n+i 约定，n=6，所以 6 和 9 是合并产生的簇）。簇 6 含 2 个原始观测（标签 `(2)`），簇 9 含 4 个（标签 `(4)`）。这告诉我们：截断后的 `leaves` 元素可能 ≥ n，需要用「`< n` 才是原始观测」这一规则来判断（与 [u6-l1](u6-l1-clusternode-and-tree.md) 的 `leaves_list` 一致）。

---

### 4.5 _plot_dendrogram 渲染与 _get_leaves_color_list 叶子配色

#### 4.5.1 概念说明

前面四个模块算出了 `icoord` / `dcoord` / `color_list` 这些纯数据。真正「画到 matplotlib 坐标轴」由 `_plot_dendrogram` 完成；而「每个叶子该上什么颜色」由 `_get_leaves_color_list` 从已算好的链接坐标反推。两者都不改算法结果，只做呈现。

#### 4.5.2 核心流程

`_plot_dendrogram` 的关键技巧是「**按颜色分组打包成 LineCollection**」：先把所有同色的线段收集到一个列表，为每种颜色建一个 `LineCollection`，再依次 `add_collection`。这样图例只为「每种颜色」生成一个条目，而非「每条线段」一个。

`_get_leaves_color_list` 的技巧是「**用坐标特征识别叶子**」：叶子恒落在地面（`yi == 0.0`）且其横坐标是奇数倍 5（`5, 15, 25, ...`，即 `xi % 5 == 0 and xi % 2 == 1`）。扫描每条 U 形链接的 4 个点，命中叶子特征就把该链接的颜色赋给对应叶子。

#### 4.5.3 源码精读

按颜色分组的渲染：[hierarchy/_hierarchy_impl.py:L2908-L2930](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2908-L2930)

```python
colors_used = _remove_dups(color_list)          # 去重且保序
color_to_lines = {}
for color in colors_used:
    color_to_lines[color] = []
for (xline, yline, color) in zip(xlines, ylines, color_list):
    color_to_lines[color].append(list(zip(xline, yline)))
...
for color in colors_used:
    if color != above_threshold_color:
        ax.add_collection(colors_to_collections[color])   # 阈值之下先画
if above_threshold_color in colors_to_collections:
    ax.add_collection(colors_to_collections[above_threshold_color])  # 阈值之上最后画
```

注意 `above_threshold_color` 的 collection 被**最后**添加——matplotlib 后画的盖在前面，这样阈值之上的灰色链接不会遮住下方彩色子树。

叶子位置与刻度：[hierarchy/_hierarchy_impl.py:L2843](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2843) `iv_ticks = np.arange(5, len(ivl)*10+5, 10)`，即叶子横坐标固定为 5, 15, 25, …，每个叶子占 10 单位宽。`orientation` 决定 x/y 是否对调（'left'/'right' 时把 `dcoord` 当 x、`icoord` 当 y）。

叶子配色反推：[hierarchy/_hierarchy_impl.py:L3400-L3414](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L3400-L3414)

```python
def _get_leaves_color_list(R):
    leaves_color_list = [None] * len(R['leaves'])
    for link_x, link_y, link_color in zip(R['icoord'], R['dcoord'], R['color_list']):
        for (xi, yi) in zip(link_x, link_y):
            if yi == 0.0 and (xi % 5 == 0 and xi % 2 == 1):
                leaf_index = (int(xi) - 5) // 10
                leaves_color_list[leaf_index] = link_color
    return leaves_color_list
```

注释点明：`xi` 是 5, 15, 25, …（`xi % 5 == 0` 且为奇数 `xi % 2 == 1`），叶子下标 `(xi - 5) // 10` 还原为 0, 1, 2, …。每个叶子的颜色 = 它所属那条 U 形链接（即它所在子树的着色）。

#### 4.5.4 代码实践

**实践目标**：验证 `_get_leaves_color_list` 推出的叶子颜色与 `color_list` 的对应关系。

**操作步骤**：

```python
import numpy as np
from scipy.cluster.hierarchy import linkage, dendrogram

ytdist = np.array([662., 877., 255., 412., 996., 295., 468., 268.,
                   400., 754., 564., 138., 219., 869., 669.])
Z = linkage(ytdist, 'single')
R = dendrogram(Z, no_plot=True)

print('leaves_color_list =', R['leaves_color_list'])
# 手动复核：第 0 号叶子横坐标 5 → 它落在哪条链接？该链接颜色是？
for idx, (xs, ys, col) in enumerate(zip(R['icoord'], R['dcoord'], R['color_list'])):
    for x, y in zip(xs, ys):
        if y == 0.0 and x % 5 == 0 and x % 2 == 1:
            print(f'叶子 (x={x}) 来自链接 {idx}，颜色 {col}')
```

**预期结果**：`leaves_color_list = ['C1', 'C1', 'C0', 'C0', 'C0', 'C0']`（前两个叶子属阈值之下的彩色子树，后四个属阈值之上的默认色子树）。手动复核循环会打印出同样的对应关系——这正是 [test_dendrogram_plot](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/tests/test_hierarchy.py#L996) 断言的值。

> 待本地验证：固定测试数据，结果应稳定。

#### 4.5.5 小练习与答案

**练习**：`_get_leaves_color_list` 用 `xi % 5 == 0 and xi % 2 == 1` 识别叶子。为什么不能用 `xi % 10 == 5` 这种更直观的写法？

**参考答案**：两者其实等价（5, 15, 25, … 既满足 `% 5 == 0` 又满足奇数），源码用两个条件是为了与注释里「divisible by 5 and odd」的文字描述一一对应、便于读者理解判定逻辑。功能上 `xi % 10 == 5`（且 `xi` 是整数）同样可行；不过源码还隐含要求 `xi` 为整数（`xi % 2`），故写成两段条件更安全、更自解释。

---

## 5. 综合实践

把本讲的颜色、截断、坐标三个主题串起来：对一个稍大的数据集跑 `ward` 聚类，分别用「完整 + 自动配色」与「截断 + 自定义调色板」两种方式出图，并用返回字典解释每个 U 形的高度。

**实践目标**：综合运用 `color_threshold`、`truncate_mode='lastp'`、`set_link_color_palette` 与 `dcoord` 数据。

**操作步骤**：

```python
import numpy as np
import matplotlib.pyplot as plt
from scipy.cluster.hierarchy import linkage, dendrogram, set_link_color_palette
from scipy.spatial.distance import pdist

# 50 个点，3 个明显的团
rng = np.random.default_rng(42)
X = np.vstack([rng.normal(0, 1, (17, 2)),
               rng.normal(8, 1, (17, 2)),
               rng.normal(4, 1, (16, 2))])

Z = linkage(X, method='ward')

# (a) 完整树 + 自动配色（默认 color_threshold = 0.7*max(Z[:,2])）
fig, axes = plt.subplots(1, 2, figsize=(14, 5))
R_full = dendrogram(Z, ax=axes[0], no_plot=False)
axes[0].set_title('full + default color_threshold')

# 用 dcoord 解释最高那条 U 形（根链接）的高度
root_dcoord = R_full['dcoord'][-1]
print('根链接 dcoord =', root_dcoord, '→ 横杠高度 =', root_dcoord[1],
      '（应 = max(Z[:,2])）')

# (b) 截断到 p=10 + 自定义调色板
set_link_color_palette(['#e41a1c', '#377eb8', '#4daf4a', '#984ea3', '#ff7f00'])
R_trunc = dendrogram(Z, p=10, truncate_mode='lastp', ax=axes[1],
                     show_contracted=True, show_leaf_counts=True)
axes[1].set_title('truncate_mode=lastp, p=10 + custom palette')
set_link_color_palette(None)   # 务必重置

print('截断后叶子数 =', len(R_trunc['leaves']), '(应 ≤ 10)')
print('截断后 ivl   =', R_trunc['ivl'])           # 含 (n) 形式的折叠叶子
print('截断后 color_list =', R_trunc['color_list'])
print('截断后叶子配色 =', R_trunc['leaves_color_list'])

# 用 dcoords 解释每个 U 形链接的高度
for i, dc in enumerate(R_trunc['dcoord']):
    print(f'  链接 {i}: 高度={dc[1]:.3f}, 左腿起点={dc[0]:.3f}, 右腿起点={dc[3]:.3f}')

plt.tight_layout()
plt.show()
```

**需要观察的现象**：

1. 左图（完整）在 `0.7 * max(Z[:,2])` 处自动把 3 个团涂成 3 种不同颜色，根链接（最高那条 U）的横杠高度等于 `max(Z[:,2])`。
2. 右图（截断）叶子数 ≤ 10，`ivl` 里出现 `(n)` 形式的折叠叶子（如 `(7)`、`(12)`），`show_contracted=True` 时这些叶子的竖腿上有小十字标记。
3. 右图因使用了自定义调色板，阈值之下的链接颜色变为红/蓝/绿等，而非默认的 `C1`/`C2`。
4. 遍历 `R_trunc['dcoord']` 时，每条记录的 `dc[1] == dc[2]`（横杠两端等高），这正是 U 形横杠的特征。

**预期结果**：根链接高度 = `max(Z[:,2])`；截断后叶子数恰为 10（或更少）；折叠叶子的 `(n)` 中 n 是该子树的原始观测数。若 3 个团分得开，完整图的 `leaves_color_list` 应基本呈现 3 段同色。

> 待本地验证：由于使用了固定 `seed=42`，聚类结构稳定；但具体颜色字符串取决于调色板与默认阈值，请以本地实际输出为准。

---

## 6. 本讲小结

- `dendrogram` 是纯 Python 入口：校验输入 → 设默认 `color_threshold = 0.7*max(Z[:,2])` → 调 `_dendrogram_calculate_info` 递归算坐标 → 可选调 `_plot_dendrogram` 画图，返回字典 `R` 含 `icoord`/`dcoord`/`ivl`/`leaves`/`color_list`/`leaves_color_list`。
- 每条 U 形链接由 4 个点组成：`icoord=[uiva,uiva,uivb,uivb]`、`dcoord=[uah,h,h,ubh]`，横杠高度 `h=Z[i,2]`，两条竖腿长度反映两子树内部最大合并高度之差。
- `color_threshold` 在阈值线下方给每个子树（flat 簇）自动涂不同色，靠 `current_color`/`currently_below_threshold` 两个单元素列表在递归闭包里共享可变状态；`C0` 专留给 `above_threshold_color`。
- `set_link_color_palette` 改的是**模块级全局变量**，只影响阈值之下链接，用完须 `set_link_color_palette(None)` 重置；非全局的 `link_color_func` 更灵活、更安全。
- `truncate_mode='lastp'`（最多 p 个叶子）/`'level'`（最多 p 层）把大树折叠，被折叠的非单点簇显示为 `(成员数)` 叶子，`show_contracted=True` 可画出折叠处的十字标记。
- `_plot_dendrogram` 按颜色把线段打包成 `LineCollection`（阈值之上最后画以免遮挡），`_get_leaves_color_list` 用「`yi==0` 且 `xi` 为奇数倍 5」反推每个叶子颜色。

## 7. 下一步学习建议

- **接线 `fcluster`**：本讲的 `color_threshold` 与 [u5-l1](u5-l1-fcluster-criteria.md) 的 `fcluster(criterion='distance', t=...)` 是同一件事的两面——同色的叶子即同簇。建议把 dendrogram 的 `color_list` 与 `fcluster` 的标签做一次交叉验证。
- **最优叶序**：dendrogram 的叶子顺序由 `Z` 的结构决定，可用 [u6-l3](u6-l3-optimal-leaf-ordering.md) 的 `optimal_leaf_ordering` 在不改变聚类结构的前提下重排叶子，使相邻叶子更接近、树图更易读。
- **继续阅读源码**：若想深入「树的表示」，可回到 [u6-l1](u6-l1-clusternode-and-tree.md) 对照 `ClusterNode`/`to_tree`；若关心 dendrogram 之外的可视化工程，可阅读 `_plot_dendrogram` 对四种 `orientation` 的 x/y 对调逻辑（[L2844-L2906](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2844-L2906)）。
