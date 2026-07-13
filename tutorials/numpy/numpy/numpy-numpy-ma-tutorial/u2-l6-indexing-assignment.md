# 索引、切片与赋值语义

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `MaskedArray.__getitem__` 在读取单个元素、切片、花式索引时，如何**同时**切出 `data` 与 `mask`，并解释它为什么要费力气判断「结果是不是标量」。
- 说清 `MaskedArray.__setitem__` 如何把一次 `a[i] = v` 拆成「写数据」和「写掩码」两步，以及在**软掩码**与**硬掩码**下行为有何不同。
- 掌握 `put` / `putmask` 这两个批量改写入口对 `mask` 的副作用，尤其是 `put` **可以解除屏蔽**、而 `putmask` 在硬掩码下**只能加屏蔽不能解屏蔽**。
- 会用 `take` 沿轴抽取元素，并理解「掩码的下标」会把对应输出位置也变成屏蔽。
- 会用 `where` / `choose` 根据条件构造新的掩码数组，并理解「掩码的条件」会强制让结果屏蔽。

本讲是 u2-l2（`MaskedArray` 子类化机制）的直接延续。u2-l2 告诉你 `__array_finalize__` 只是「默认猜测」、真正精确的 mask 传播由 `__getitem__` 和 `__array_wrap__` 负责；本讲就把 `__getitem__`（以及它的对偶 `__setitem__`）掰开揉碎讲清楚。

## 2. 前置知识

本讲默认你已经掌握以下概念（只做最简回顾）：

- **掩码数组三件套**：`data`（含坏值的原始数据）、`mask`（同形状布尔数组，`True` 表示屏蔽；无屏蔽时压缩为单例 `nomask`）、`fill_value`（屏蔽位对外填充值）。详见 u1-l4。
- **`nomask` 单例**：定义在 [core.py:L87-L88](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L87-L88) 为 `nomask = MaskType(0)`，即 `np.False_`。全库用 `is nomask` 做 O(1) 身份判断，从而避免为「无屏蔽」的数组分配全 `False` 的布尔数组。详见 u2-l1。
- **`getmask` / `getmaskarray`**：前者忠实返回内部 `_mask`（可能为 `nomask`），后者永远返回同形状的全布尔数组。详见 u2-l1。
- **`make_mask_none(shape, dtype)`**：按形状与（可能的）结构化 dtype 生成一个全 `False` 的掩码。详见 u2-l1。
- **`mask_or(m1, m2)`**：用 `logical_or` 合并两个掩码，含 `nomask` 短路。详见 u2-l1。
- **`_update_from(obj)`**：把模板对象的「簿记属性」（`_fill_value`/`_hardmask`/`_sharedmask`/`_baseclass` 等）搬到 `self`，不搬数据与 mask。详见 u2-l2。
- **软掩码 vs 硬掩码**：默认是软掩码（`_hardmask=False`），给被屏蔽位置赋值会**解除屏蔽**；硬掩码（`_hardmask=True`，由 `harden_mask()` 开启）下，被屏蔽位置**无法被赋值还原**。详见 u3-l1（本讲只用到结论）。
- **`masked` 单例**：表示「这一个值被屏蔽」的全局不可变对象，索引单个屏蔽元素时会被返回。详见 u3-l3。

如果你对 NumPy 普通 `ndarray` 的索引规则（基础索引、切片、花式索引、布尔索引）已经熟悉，本讲只是在其之上加一层「mask 必须同步」的逻辑。

## 3. 本讲源码地图

本讲全部内容集中在 `numpy/ma/core.py` 一个文件里：

| 位置 | 作用 |
| --- | --- |
| [core.py:L3277-L3400](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3277-L3400) | `MaskedArray.__getitem__`：索引/切片读取入口，显式切分 mask，并判断结果是否为标量。 |
| [core.py:L3402-L3473](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3402-L3473) | `MaskedArray.__setitem__`：赋值入口，按软/硬掩码分四条路径同步 data 与 mask。 |
| [core.py:L4837-L4921](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L4837-L4921) | `MaskedArray.put`：把若干扁平下标位置的值批量改写，并更新 mask（可解除屏蔽）。 |
| [core.py:L7552-L7605](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L7552-L7605) | 模块级 `putmask(a, mask, values)`：按布尔掩码批量改写。 |
| [core.py:L6180-L6269](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6180-L6269) | `MaskedArray.take`：沿指定轴抽取元素，data 与 mask 各 take 一次。 |
| [core.py:L7930-L8019](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L7930-L8019) | 模块级 `where(condition, x, y)`：根据条件从 x/y 取值构造新掩码数组。 |
| [core.py:L8022-L8095](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L8022-L8095) | 模块级 `choose(indices, choices)`：用下标数组从一组候选里挑值。 |

辅助函数（本讲会引用但不展开）：

- [core.py:L1597-L1604](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1597-L1604) `_shrink_mask`：全 `False` 的掩码压回 `nomask`。
- [core.py:L1698](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1698) `make_mask_none`：生成全 `False` 掩码。
- [core.py:L1759](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1759) `mask_or`：合并两个掩码。
- [core.py:L2660-L2766](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2660-L2766) `MaskedIterator`：`a.flat` 返回的扁平迭代器，内部也实现了 `__getitem__`/`__setitem__`，逻辑与主类同构。

## 4. 核心概念与源码讲解

本讲按五个最小模块展开：`__getitem__`（读）、`__setitem__`（写）、`put`/`putmask`（批量改写）、`take`（沿轴抽取）、`where`/`choose`（条件构造）。它们共同回答一个问题：**对一个带掩码的数组做索引、赋值、挑选时，data 和 mask 这两份「平行数据」如何保持同步？**

理解本讲有一条贯穿始终的主线：

> **掩码数组有两份需要同步的数据——`_data` 和 `_mask`。每一次索引或赋值，源码都在做「对 data 做一次普通 ndarray 操作，再对 mask 做一次对应操作」。难点不在数据，而在 mask 的各种边界情形：标量 vs 数组、软掩码 vs 硬掩码、下标本身被屏蔽、无屏蔽（nomask）时如何避免无谓分配。**

### 4.1 `__getitem__`：索引与切片读取

#### 4.1.1 概念说明

对普通 `ndarray`，`a[i]` 是一件简单的事：取数据即可。但对 `MaskedArray`，每一个元素都带着一个「是否屏蔽」的标记，所以 `a[i]` 必须**同时**取出数据和掩码。

这件事听起来直白，但有一个棘手的边界：**索引结果到底是「一个标量」还是「一个数组」？** 这两种情况的返回类型完全不同：

- 取出**单个屏蔽元素**时，应当返回全局单例 `masked`（而不是一个含一个元素的 `MaskedArray`），这样 `a[i] is np.ma.masked` 才成立，方便判断。
- 取出**单个未屏蔽元素**时，应当返回一个普通标量（如 `np.float64`），而不是包了一层的数组，以保持与 `ndarray` 一致的行为。
- 取出**多个元素**（切片、花式索引）时，应当返回一个新的 `MaskedArray`，并带上切分后的 mask。

这个「标量 vs 数组」的判断，就是 `__getitem__` 大部分代码在处理的事。

#### 4.1.2 核心流程

`__getitem__` 的执行可以分为三步：

1. **分别切出 data 和 mask**：`dout = self.data[indx]`、`mout = self._mask[indx]`（若 mask 存在）。
2. **判断结果是否为标量**（`scalar_expected`）：
   - 若 `_mask is not nomask`：直接看切出来的 `mout` 是不是 `ndarray`，因为 mask 的 dtype 不会骗人（不可能是 object，也不会被子类化）。
   - 若 `_mask is nomask`（无屏蔽）：不能切 mask（否则要凭空分配一个全 `False` 数组，违背 `nomask` 省内存的初衷），于是用 `_scalar_heuristic` 凭 data 的类型猜；猜不出就退回 `getmaskarray(self)[indx]`（这时才肯分配）。
3. **按标量/数组分别组装返回值**。

用伪代码表示：

```text
def __getitem__(self, indx):
    dout = self.data[indx]                 # 切数据
    if self._mask is not nomask:
        mout = self._mask[indx]            # 切掩码
        scalar_expected = not isinstance(mout, ndarray)
    else:
        mout = nomask                      # 不分配，保持省内存
        scalar_expected = _scalar_heuristic(self.data, dout)  # 猜
        if scalar_expected is None:        # 猜不出
            scalar_expected = not isinstance(getmaskarray(self)[indx], ndarray)

    if scalar_expected:
        if isinstance(dout, np.void):      return mvoid(dout, mask=mout, ...)
        elif (object dtype 且 dout 是数组): return MaskedArray(dout, mask=True) 或 dout
        elif mout:                          return masked       # 屏蔽标量
        else:                               return dout         # 普通标量
    else:
        dout = dout.view(type(self))       # 升级回子类
        dout._update_from(self)            # 继承簿记属性
        if 字段名索引:                      dout._fill_value = self._fill_value[indx]
        if mout is not nomask:             dout._mask = reshape(mout, dout.shape)
        return dout
```

#### 4.1.3 源码精读

先看入口与「分别切 data/mask」：

[core.py:L3288](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3288) 这一行 `dout = self.data[indx]` 是数据侧的切分——注意它用的是 `self.data`（即把自身按 baseclass 看的普通 ndarray 视图），所以这里不会再次触发 `MaskedArray.__getitem__` 造成递归。

接着是判断「标量 vs 数组」的两条路径：

[core.py:L3317-L3331](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3317-L3331) 这段是判断的核心。`if _mask is not nomask` 分支里，`mout = _mask[indx]` 后用 `_is_scalar(mout)`（即「不是 ndarray 就是标量」）来判定——注释解释了为什么 mask 可信：`_mask` 不能是子类、也不能是 object dtype，所以它返回 ndarray 还是标量，就如实反映了索引的维度。而 `else`（`nomask`）分支刻意不分配 mask，先用 `_scalar_heuristic` 猜，只有猜不出来（返回 `None`）时才「花代价」去 `getmaskarray(self)[indx]`。

再看标量结果的四种子情况：

[core.py:L3334-L3356](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3334-L3356) 这里依次处理：结构化记录（`np.void`）重包成 `mvoid`；object dtype 的特殊情形（gh-5962）；屏蔽标量返回 `masked`；普通标量直接返回 `dout`。注意 [core.py:L3353-L3354](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3353-L3354) `elif mout: return masked`——这正是「单个屏蔽元素 → 返回 `masked` 单例」的来源。

最后是数组结果的组装：

[core.py:L3358-L3400](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3358-L3400) `dout = dout.view(type(self))` 把切出来的普通 ndarray 视图「升级」回 `MaskedArray`（或其子类，用 `type(self)` 保留具体类型），再 `_update_from(self)` 继承 `_fill_value` 等簿记属性。如果是字段名索引（`is_string_or_list_of_strings(indx)`），还要把 `fill_value` 也按字段切一刀（[core.py:L3363-L3393](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3363-L3393)）。最后 [core.py:L3395-L3398](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3395-L3398) `dout._mask = reshape(mout, dout.shape)`——**把 mask 也切分并 reshape 到与数据一致的形状**，这就是切片时 mask 能正确传播的根本原因（也是 u2-l2 里说「`__getitem__` 显式切分 mask、比 `__array_finalize__` 的猜测更精确」的具体落点）。注意末尾注释 `# Note: Don't try to check for m.any(), that'll take too long`：作者刻意不检查「切出来的 mask 是不是全 False」，因为那会触发一次全量扫描，得不偿失。

#### 4.1.4 代码实践

**实践目标**：亲手验证「单个屏蔽元素返回 `masked` 单例」「切片返回带正确 mask 的 `MaskedArray`」「无屏蔽数组的切片不分配 mask」三件事。

**操作步骤**：

```python
import numpy as np
import numpy.ma as ma

# 1. 带屏蔽的数组
x = ma.array([10, 20, 30, 40], mask=[0, 1, 0, 1])
print("x[1] is ma.masked ?", x[1] is ma.masked)   # 单个屏蔽元素
print("x[0]              :", x[0], type(x[0]))    # 单个未屏蔽元素 -> 普通标量
print("x[1:]             :\n", x[1:])             # 切片 -> MaskedArray
print("x[1:].mask        :", x[1:].mask)          # mask 也被切分

# 2. 无屏蔽数组（nomask）：切片后仍是 nomask，不会分配全 False
y = ma.array([1, 2, 3, 4])
print("y.mask is ma.nomask ?", y.mask is ma.nomask)
print("y[1:3].mask is ma.nomask ?", y[1:3].mask is ma.nomask)
```

**需要观察的现象**：
- `x[1] is ma.masked` 应为 `True`；`x[0]` 是 `numpy.int64` 而非 `MaskedArray`。
- `x[1:]` 的 mask 是 `[True, False, True]`（原 mask `[F,T,F,T]` 切掉第 0 位）。
- `y[1:3].mask is ma.nomask` 应为 `True`——切片没有强制分配布尔数组。

**预期结果**：上述四个断言全部成立。若你给 `y` 做了 `y[1] = ma.masked` 之后再切片，才会出现真实布尔 mask。

> 说明：以上输出可由 `__getitem__` 的标量分支与 `reshape(mout, dout.shape)` 直接推出，无需本地运行即可预判；但建议本地跑一遍加深印象（待本地验证具体打印格式）。

#### 4.1.5 小练习与答案

**练习 1**：对一个二维掩码数组 `m = ma.array([[1,2],[3,4]], mask=[[0,1],[0,0]])`，`m[0]` 和 `m[0,1]` 分别返回什么类型？

**参考答案**：`m[0]` 取出第一行，是**数组结果**，返回一个 `MaskedArray`（mask 为 `[False, True]`）；`m[0,1]` 取出单个屏蔽标量，走标量分支，返回全局单例 `ma.masked`。

**练习 2**：为什么 `__getitem__` 在 `_mask is nomask` 时宁可写一段 `_scalar_heuristic` 启发式，也不直接 `getmaskarray(self)[indx]`？

**参考答案**：`getmaskarray` 会**分配一个同形状的全 `False` 布尔数组**，违背 `nomask`「省内存、O(1) 身份判断」的设计。只有当启发式确实无法判定（返回 `None`）时，才肯付出这次分配的代价。

---

### 4.2 `__setitem__`：赋值与掩码同步

#### 4.2.1 概念说明

`a[i] = v` 对普通数组只是「把数据写进去」。对掩码数组，它要同时回答两个问题：

1. **数据怎么写**：把 `v` 的数据部分写到 `_data[indx]`。
2. **掩码怎么写**：`v` 如果本身带屏蔽（比如 `v` 是另一个 `MaskedArray`，或 `v` 就是 `masked`），那么对应位置的 mask 要置 `True`；反过来，给一个**原本屏蔽**的位置赋一个**未屏蔽**的值，要不要解除屏蔽？——这取决于软掩码还是硬掩码。

这就引出 `__setitem__` 最关键的设计：**软掩码可还原、硬掩码不可还原**。

#### 4.2.2 核心流程

`__setitem__` 的分支结构如下：

```text
def __setitem__(self, indx, value):
    if self is masked:                 raise MaskError   # 不能改 masked 常量
    if indx 是字段名(字符串):            _data[indx]=value; _mask[indx]=getmask(value); return
    if value is masked:                # 只设屏蔽，不动数据
        _mask[indx] = True (或结构化的全 True); return
    dval = value._data (或 value 本身)
    mval = getmask(value)
    if _mask is nomask:                # 懒分配：只在 mval 非空时才建 mask
        _data[indx] = dval
        if mval is not nomask:         _mask = 全False; _mask[indx] = mval
    elif 软掩码 (not _hardmask):
        _data[indx] = dval
        _mask[indx] = mval             # 直接覆盖，可解除屏蔽
    elif indx 是布尔掩码 且 硬掩码:
        indx = indx & ~_mask           # 排除已屏蔽位置，不能还原
        _data[indx] = dval
    else (普通索引 且 硬掩码):
        mindx = mask_or(_mask[indx], mval)   # 只能加屏蔽不能解
        copyto(dindx, dval, where=~mindx)    # 已屏蔽处不写入
        _mask[indx] = mindx
```

关键不变量：

- **软掩码**下 `_mask[indx] = mval` 是直接覆盖，所以给屏蔽位赋普通值会**解除屏蔽**。
- **硬掩码**下用 `mask_or`（逻辑或）合并，已屏蔽的位置**永远保持屏蔽**，`copyto(..., where=~mindx)` 保证数据也不写进这些位置。

#### 4.2.3 源码精读

先看装饰器和入口：

[core.py:L3402-L3415](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3402-L3415) 这里的 `@np.errstate(over='ignore', invalid='ignore')` 装饰器很关键：被屏蔽位置在底层缓冲区里可能存着垃圾值（比如上一次运算残留的 `inf`/`nan`），赋值时把这些值转成整数可能溢出，从而触发 `RuntimeWarning`。装饰器把这些警告静音，因为它们发生在「反正要被屏蔽」的值上，不是真正的错误。[core.py:L3414-L3415](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3414-L3415) `if self is masked: raise MaskError(...)` 禁止对全局 `masked` 常量赋值——它是不可变的（详见 u3-l3）。

字段名索引与「只设屏蔽」的快路径：

[core.py:L3418-L3436](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3418-L3436) 字符串下标（字段名）走单独路径：写数据、再写 mask。`value is masked` 时只更新 mask、不动 data——语义是「把这些位置标为屏蔽」，至于 data 里留着什么不重要（反正会被屏蔽）。

取出 value 的 data/mask 两部分，然后按四种情况分流：

[core.py:L3444-L3472](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3444-L3472) 这是赋值的核心。`dval = getattr(value, '_data', value)` 用「鸭子类型」取出数据部分（若 value 不是掩码数组则取它本身）；`mval = getmask(value)` 取掩码部分。随后四条分支：

- [core.py:L3444-L3449](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3444-L3449) `nomask` 分支：先写数据；**只有当 value 带屏蔽时**才用 `make_mask_none` 懒分配一个 mask。这是「无屏蔽就保持无屏蔽」原则的体现。
- [core.py:L3450-L3457](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3450-L3457) 软掩码分支：`_data[indx] = dval` 和 `_mask[indx] = mval` **直接覆盖**，所以原来的屏蔽可以被一个未屏蔽值「擦掉」。
- [core.py:L3458-L3460](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3458-L3460) 布尔索引 + 硬掩码分支：`indx = indx * umath.logical_not(_mask)` 把「已经是 True 的屏蔽位」从待写位置里剔除，所以硬掩码下没法靠布尔赋值还原屏蔽位。
- [core.py:L3461-L3472](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3461-L3472) 通用索引 + 硬掩码分支：`mindx = mask_or(_mask[indx], mval, copy=True)`——**用逻辑或合并**，于是屏蔽只能变多不能变少；`np.copyto(dindx, dval, where=~mindx)` 保证屏蔽位置的数据不被覆盖。

#### 4.2.4 代码实践

**实践目标**：对比软掩码与硬掩码下，给屏蔽位赋普通值时的不同表现。

**操作步骤**：

```python
import numpy.ma as ma

# 软掩码（默认）
s = ma.array([1, 2, 3], mask=[0, 1, 0])
s[1] = 99                # 给屏蔽位赋未屏蔽值
print("soft s        :", s)          # 屏蔽被解除
print("soft s.mask   :", s.mask)     # [False, False, False]

# 硬掩码
h = ma.array([1, 2, 3], mask=[0, 1, 0], hard_mask=True)
h[1] = 99                # 尝试还原屏蔽位
print("hard h        :", h)          # 屏蔽位仍是 --
print("hard h._data  :", h._data)    # 数据也没被写入，还是 2
print("hard h.mask   :", h.mask)     # [False, True, False]

# 用 masked 赋值：只设屏蔽，不动数据
s[0] = ma.masked
print("after s[0]=masked:", s)       # s[0] 变成 --
```

**需要观察的现象**：软掩码下 `s[1]` 由 `--` 变成 `99`，mask 全 `False`；硬掩码下 `h[1]` 仍是 `--`，且 `h._data[1]` 仍是 `2`（赋值被 `copyto(..., where=~mindx)` 拦下）。

**预期结果**：与上述一致。这正对应源码的 `not self._hardmask`（直接覆盖）与 `mask_or(...)` + `copyto(where=~mindx)`（不可还原）两条分支。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `__setitem__` 要加 `@np.errstate(over='ignore', invalid='ignore')`？

**参考答案**：因为屏蔽位的底层 `_data` 可能存放着垃圾值（如 `nan`/`inf` 或上一次运算的残留），把它们写入新位置（尤其涉及整型转换）容易触发溢出或无效值的 `RuntimeWarning`。这些警告发生在「反正要被屏蔽」的值上，属于噪声，所以静音。

**练习 2**：在硬掩码数组上执行 `h[[0,1]] = [10, 20]`（其中 `h[1]` 原本被屏蔽），`h._data` 会变成什么？

**参考答案**：`h._data` 变成 `[10, 2, 3]`——第 0 位被写成 10，但第 1 位因为是硬屏蔽、`copyto` 的 `where=~mindx` 排除了它，所以仍保留原值 `2`，且 mask 仍为 `[False, True, False]`。

---

### 4.3 `put` / `putmask`：批量改写与掩码副作用

#### 4.3.1 概念说明

`__setitem__` 解决「按下标/条件改若干位置」，但有两个更专门的批量入口：

- **`MaskedArray.put(indices, values, mode)`**：等价于 `ndarray.put`，把 `values` 按**扁平下标** `indices` 放进数组（按 C 顺序展平后定位）。它的掩码语义特别值得记：**若 `values` 某位屏蔽，则对应位置被屏蔽；若 `values` 某位未屏蔽，则对应位置被解除屏蔽**。
- **模块级 `putmask(a, mask, values)`**：等价于 `np.putmask`，在 `mask` 为 True 的位置写入 `values`。它在硬掩码下**只能加屏蔽不能解屏蔽**。

两者都同时改 data 和 mask，但「能不能解除屏蔽」的规则不同。

#### 4.3.2 核心流程

`put` 的流程：

```text
def put(self, indices, values, mode='raise'):
    if 硬掩码 且 有 mask:                         # 硬掩码：丢弃落在屏蔽位的写入
        mask = self._mask[indices]
        indices, values = indices[~mask], values[~mask]
    self._data.put(indices, values, mode)         # 写数据（委托 ndarray.put）
    if self._mask is nomask 且 values 无屏蔽: return  # 短路
    m = getmaskarray(self)
    if values 无屏蔽:  m.put(indices, False)       # 解除这些位置的屏蔽
    else:              m.put(indices, values._mask) # 按 values 的 mask 设置
    self._mask = make_mask(m, shrink=True)         # 可能压回 nomask
```

`putmask` 的流程：

```text
def putmask(a, mask, values):
    若 a 不是 MaskedArray: a = a.view(MaskedArray)
    valdata, valmask = getdata(values), getmask(values)
    if a 无屏蔽:
        if valmask 有: 新建 mask; copyto(a._mask, valmask, where=mask)
    elif a 硬掩码:
        if valmask 有: m = a._mask.copy(); copyto(m, valmask, where=mask); a.mask |= m   # 只加不减
    else (软掩码):
        copyto(a._mask, valmask, where=mask)        # 直接覆盖
    copyto(a._data, valdata, where=mask)            # 最后统一写数据
```

注意两者对「解除屏蔽」的差异：

| 操作 | 软掩码 | 硬掩码 |
| --- | --- | --- |
| `put(indices, 未屏蔽值)` | **解除**这些位置屏蔽 | 落在屏蔽位的写入被丢弃，其余解除 |
| `putmask(a, mask, 未屏蔽值)` | 在 mask=True 处写数据，不改 mask | 在 mask=True 且未屏蔽处写数据，不改 mask |
| `putmask(a, mask, 屏蔽值)` | mask=True 处置屏蔽 | `a.mask \|= m`，只能加屏蔽 |

#### 4.3.3 源码精读

先看 `put`：

[core.py:L4898-L4921](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L4898-L4921) 是 `put` 的全部逻辑。[core.py:L4899-L4905](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L4899-L4905) 处理硬掩码：先取出 `indices` 落点的现有 mask，把「落在已屏蔽位置」的下标和对应 value 一起剔除——硬掩码下这些位置写不进去。[core.py:L4907](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L4907) 把数据写入委托给普通 `ndarray.put`。[core.py:L4909-L4911](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L4909-L4911) 短路：自己和 values 都没屏蔽时，mask 完全不用动。否则 [core.py:L4913-L4920](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L4913-L4920) 更新 mask——注意 [core.py:L4916](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L4916) `m.put(indices, False, mode=mode)`：**当 values 未屏蔽时，把这些位置写成 `False`，即解除屏蔽**。最后 `make_mask(m, shrink=True)` 把可能全 False 的 mask 压回 `nomask`。

再看 `putmask`：

[core.py:L7587-L7605](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L7587-L7605) 是 `putmask` 的实现。注释说「参数顺序与 frommethod 不同，所以不能用 `frommethod` 包装」。[core.py:L7588-L7589](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L7588-L7589) 先把普通 ndarray 视图转成 `MaskedArray`。随后三条分支对应「a 无屏蔽 / a 硬掩码 / a 软掩码」：硬掩码分支 [core.py:L7596-L7600](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L7596-L7600) 用 `a.mask |= m`，所以**只能加屏蔽不能减**。最后 [core.py:L7605](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L7605) `np.copyto(a._data, valdata, where=mask)` 把数据统一写入——注意这一步对**所有**分支都执行，是数据写入的唯一入口。

模块级 `put`（[core.py:L7511-L7549](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L7511-L7549)）只是 `a.put(indices, values, mode)` 的薄壳：遇到没有 `put` 方法的对象退回 `np.asarray(a).put(...)`。

#### 4.3.4 代码实践

**实践目标**：验证 `put` 用未屏蔽值能解除屏蔽、用屏蔽值能设置屏蔽，并复现 `test_put` 的断言。

**操作步骤**（改编自 [test_core.py:L3575-L3595](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_core.py#L3575-L3595)）：

```python
import numpy as np
import numpy.ma as ma

x = ma.array(np.arange(10), mask=[1, 0, 0, 0, 0] * 2)   # 第 0、5 位屏蔽
i = [0, 2, 4, 6]

# 用未屏蔽值 put：会解除第 0 位的屏蔽
x.put(i, [6, 4, 2, 0])
print("after put unmasked:", x)            # 第 0 位 = 6 且解除屏蔽
print("mask:", x.mask)                     # [F,F,F,F,F,T,F,F,F,F]  第 5 位仍屏蔽

# 用带屏蔽的值 put：会把对应位置设为屏蔽
x.put(i, ma.array([0, 2, 4, 6], mask=[1, 0, 1, 0]))
print("after put masked  :", x)
print("mask:", x.mask)                     # [T,F,F,F,T,T,F,F,F,F]

# putmask 演示
y = ma.array([1, 2, 3, 4, 5, 6], mask=[0, 0, 0, 1, 1, 1])
ma.putmask(y, [0, 0, 1, 0, 0, 1], 99)
print("after putmask     :", y._data, y.mask)   # data[2],[5]=99; mask[5] 由 1->0
```

**需要观察的现象**：
- 第一次 `put` 后，第 0 位从屏蔽变成 `6`（解除屏蔽），第 5 位（不在 `i` 中）保持屏蔽。
- 第二次 `put` 后，`i` 中对应 values 屏蔽的第 0、2 位（values mask `[1,0,1,0]`）变为屏蔽。
- `putmask` 中 `mask[5]`（原本屏蔽）在 `where=mask` 为 True 时被写入 99 且解除屏蔽（软掩码直接覆盖）。

**预期结果**：与 `test_put` / `test_putmask` 的断言一致。注意 `put` 改变 mask 的方向完全由「values 这一位是否屏蔽」决定，这正是源码 `m.put(indices, False)` vs `m.put(indices, values._mask)` 的二分。

#### 4.3.5 小练习与答案

**练习 1**：对一个硬掩码数组 `xh`（某些位已屏蔽），调用 `xh.put([4,2,0], [1,2,3])`，落在屏蔽位的写入会怎样？

**参考答案**：会被丢弃。源码 [core.py:L4899-L4905](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L4899-L4905) 先用 `indices[~mask]` 把落在屏蔽位的下标连同对应 value 一起剔除，再写数据，所以硬掩码位置的值不变、mask 也不变。

**练习 2**：`putmask` 在硬掩码下为什么用 `a.mask |= m` 而不是 `copyto(a._mask, valmask, where=mask)`？

**参考答案**：硬掩码的语义是「屏蔽只能加不能减」。若直接 `copyto`，一个 `valmask=False` 的值会把屏蔽位写成 `False`，违反硬掩码不可还原的约定；用 `|=`（逻辑或）则保证原有屏蔽位恒为 `True`。

---

### 4.4 `take`：沿轴抽取元素

#### 4.4.1 概念说明

`take` 是「花式索引的函数化版本」：给定一组下标 `indices`，沿某条轴把这些位置的元素抽出来组成新数组。它比 `a[indices]` 更明确（可以指定 `axis`、`mode`、`out`）。

对掩码数组，`take` 要多做两件事：

1. data 和 mask **分别** take 一次，保证抽取出的元素带上正确的屏蔽标记。
2. 处理「**下标本身被屏蔽**」的情况——如果 `indices` 是一个 `MaskedArray` 且某些下标被屏蔽，那么输出里对应位置也应被屏蔽（因为「我不知道要取哪个位置」自然导出「取不到有效值」）。

#### 4.4.2 核心流程

```text
def take(self, indices, axis=None, out=None, mode='raise'):
    maskindices = getmask(indices)
    if maskindices is not nomask:
        indices = indices.filled(0)        # 屏蔽的下标先填成 0（占位）
    if out is None:
        out = _data.take(indices, axis, mode)[...].view(cls)   # 数据 take 后升级子类
    else:
        np.take(_data, indices, axis, mode, out=out)
    if out 是 MaskedArray:
        if _mask is nomask:  outmask = maskindices
        else:                outmask = _mask.take(indices, axis, mode); outmask |= maskindices
        out.__setmask__(outmask)
    return out[()]                          # 0d 降回标量，与 ndarray.take 一致
```

关键点：

- `indices.filled(0)` 只是把屏蔽下标**占位**为 0，让 `ndarray.take` 不报错；真正的屏蔽效果来自后面 `outmask |= maskindices`——屏蔽的下标让输出位置也屏蔽。
- `out[()]` 把 0 维结果降回 Python/NumPy 标量，保持与 `ndarray.take` 一致（`take` 单个标量下标返回标量而非 0d 数组）。

#### 4.4.3 源码精读

[core.py:L6248-L6269](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6248-L6269) 是 `take` 的全部逻辑。

[core.py:L6251-L6253](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6251-L6253) 取出 `indices` 自身的 mask；若有屏蔽下标，用 `indices.filled(0)` 把它们填成 0——这一步只是为了「让 `ndarray.take` 不抛错」，屏蔽语义随后补上。

[core.py:L6256-L6259](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6256-L6259) 对数据做 take：无 `out` 时 `_data.take(...)[...].view(cls)`，其中 `[...]` 把标量提升成 0d 数组以便 `.view` 正确工作，最后 `view(cls)` 升级回 `MaskedArray`（或子类）。

[core.py:L6261-L6267](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6261-L6267) 组装输出 mask：若源数组无屏蔽（`_mask is nomask`），输出 mask 就等于「下标的 mask」；否则把源 mask 也 take 一次，再与「下标的 mask」做 `|=`——这正是「屏蔽下标 → 屏蔽输出」的来源。最后 `out.__setmask__(outmask)`。

[core.py:L6269](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6269) `return out[()]`：把 0 维数组降回标量。注释说是「for consistency with ndarray.take」。

模块级 `take`（[core.py:L7126-L7131](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L7126-L7131)）只是先把输入 `masked_array(a)` 包装再调 `a.take`，这样普通 ndarray 也能用 `ma.take`。

#### 4.4.4 代码实践

**实践目标**：验证 `take` 同时抽取 data 与 mask，并验证「屏蔽的下标」会让输出对应位置屏蔽。

**操作步骤**（改编自 [test_core.py:L3912-L3928](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_core.py#L3912-L3928) 与 `test_take_masked_indices`）：

```python
import numpy as np
import numpy.ma as ma

x = ma.masked_array([10, 20, 30, 40], [0, 1, 0, 1])
print(x.take([0, 0, 3]))              # [10, 10, --]，mask=[F,F,T]
print(x.take([[0, 1], [0, 1]]))       # 二维下标 -> 二维输出，mask 跟随

# 沿轴 take
m = ma.array([[10,20,30],[40,50,60]], mask=[[0,0,1],[1,0,0]])
print(m.take([0, 2], axis=1))         # 抽第 0、2 列

# 屏蔽的下标 -> 输出对应位置屏蔽
a = np.array((40, 18, 37, 9, 22))
idx = ma.array([0, 1, 2, 3, 4], mask=[0, 0, 0, 0, 1])   # 下标 4 被屏蔽
print(ma.take(a, idx))                # [40,18,37,9,--]，最后一个屏蔽
```

**需要观察的现象**：`x.take([0,0,3])` 的输出第三个元素是 `--`（因为原数组第 3 位屏蔽）；屏蔽下标例子里，输出最后一个元素是 `--`（因为下标 4 被屏蔽，`outmask |= maskindices` 生效）。

**预期结果**：与上述一致。第二条尤其重要——**输入数组 `a` 本身没有任何屏蔽**，但输出却出现了屏蔽位，这只能由「下标被屏蔽」解释，对应源码 `outmask = maskindices` 分支。

#### 4.4.5 小练习与答案

**练习 1**：`take` 为什么先 `indices.filled(0)` 再 take，而不是直接屏蔽下标？

**参考答案**：底层 `ndarray.take` 需要确定性的整数下标才能定位元素，无法理解「屏蔽」。所以先用 `filled(0)` 给屏蔽下标一个占位值（取第 0 个元素），让 take 顺利完成；随后用 `outmask |= maskindices` 把这些位置标为屏蔽，从而在语义上「抹掉」占位取到的值。

**练习 2**：`take` 末尾的 `return out[()]` 去掉会怎样？

**参考答案**：当 `indices` 是单个标量下标时，`_data.take(标量)` 返回 0d 数组；若不做 `[()]` 降维，`ma.take` 会返回 0d `MaskedArray` 而非标量，与 `ndarray.take`（返回标量）行为不一致。`[()]` 正是为了对齐这一行为。

---

### 4.5 `where` / `choose`：条件构造新数组

#### 4.5.1 概念说明

`where` 和 `choose` 都是「根据某种规则，从若干候选数组里挑值，拼成新数组」。它们的掩码语义有一个共同的关键设计：**条件/下标一旦被屏蔽，输出对应位置就强制屏蔽**，而不论候选值是什么。

- **`where(condition, x, y)`**：`condition` 为 True 取 `x`、为 False 取 `y`。三参数版本是本节重点；单参数版本退化为 `nonzero`。
- **`choose(indices, choices)`**：`indices[i]=k` 表示「在位置 i 取 `choices[k]` 的对应元素」，相当于多路 `where`。

#### 4.5.2 核心流程

`where(cond, x, y)`：

```text
cf = filled(condition, False)        # 屏蔽的条件当 False 处理(取 y)
xd, yd = getdata(x), getdata(y)
cm, xm, ym = getmaskarray(condition), getmaskarray(x), getmaskarray(y)
# masked 单例特判（避免被当作 float64）
data = np.where(cf, xd, yd)          # 数据：True 取 x，False 取 y
mask = np.where(cf, xm, ym)          # mask：同理
mask = np.where(cm, True, mask)      # 关键：条件屏蔽 -> 结果强制屏蔽
mask = _shrink_mask(mask)            # 全 False 压回 nomask
return masked_array(data, mask=mask)
```

`choose(indices, choices)`：

```text
c = filled(indices, 0)                       # 屏蔽下标填 0
masks = [每个 choice 的 mask]
data  = [每个 choice 的 filled]
outputmask = np.choose(c, masks)             # 按下标选 mask
outputmask = make_mask(mask_or(outputmask, getmask(indices)))  # 屏蔽下标 -> 屏蔽输出
d = np.choose(c, data).view(MaskedArray)
d.__setmask__(outputmask)
return d
```

两者的共同不变量：

> **条件/下标被屏蔽 ⇒ 输出该位置屏蔽。** `where` 体现在 `np.where(cm, True, mask)`，`choose` 体现在 `mask_or(outputmask, getmask(indices))`。

#### 4.5.3 源码精读

先看 `where`：

[core.py:L7986-L8019](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L7986-L8019)。[core.py:L7987-L7991](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L7987-L7991) 处理参数数量：恰好缺一个报错；两个都缺则退化为 `nonzero(condition)`。[core.py:L7994](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L7994) `cf = filled(condition, False)`——**屏蔽的条件当 `False` 处理**（即「不知道真假时取 y」）。[core.py:L8012-L8013](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L8012-L8013) data 与 mask 各做一次 `np.where(cf, ..., ...)`，分别从 x/y 挑数据和 mask。[core.py:L8014](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L8014) `mask = np.where(cm, np.ones(...), mask)`——**这一行是 `where` 掩码语义的灵魂**：只要条件被屏蔽（`cm=True`），无论 x/y 是什么，结果都被强制屏蔽。[core.py:L8017](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L8017) `_shrink_mask` 把全 False 压回 `nomask`。

[core.py:L8005-L8010](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L8005-L8010) 是 `masked` 单例的特判：因为 `masked` 的 dtype 是 `float64`，若直接参与 `np.where` 会把整个结果抬成 float，所以这里手动构造一个 dtype 匹配的全 0 数据 + 全 True 掩码来替代。

再看 `choose`：

[core.py:L8068-L8095](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L8068-L8095)。[core.py:L8068-L8078](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L8068-L8078) 定义两个内嵌辅助函数：`fmask` 返回填充后的数据（`masked` 当 `True`）、`nmask` 返回 mask（`masked` 当 `True`、`nomask` 当 `False`）。[core.py:L8080-L8083](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L8080-L8083) 把每个候选的 mask 与数据各列成一张表。[core.py:L8085-L8087](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L8085-L8087) 先用 `np.choose(c, masks)` 按下标选 mask，再 `mask_or(..., getmask(indices))`——**屏蔽的下标让输出屏蔽**（与 `take` 同理）。[core.py:L8089-L8095](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L8089-L8095) 用 `np.choose(c, data)` 选数据并 `view(MaskedArray)`，最后 `__setmask__`。

#### 4.5.4 代码实践

**实践目标**：验证 `where` 中「条件屏蔽 ⇒ 结果屏蔽」「条件 False ⇒ 取 y」两条规则，以及 `choose` 中「屏蔽下标 ⇒ 屏蔽输出」。

**操作步骤**（改编自 [test_core.py:L4711-L4831](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_core.py#L4711-L4831) 与 `test_choose`）：

```python
import numpy as np
import numpy.ma as ma

x = ma.array(np.arange(9.).reshape(3, 3),
             mask=[[0,1,0],[1,0,1],[0,1,0]])
# 条件 x>5：True 取 x，False 取 -3.1416
print(ma.where(x > 5, x, -3.1416))
# 注意 x 屏蔽的位置，结果依然屏蔽（因为 xm 被选中）

# 条件本身被屏蔽 -> 结果强制屏蔽
cond = ma.array([1, 0, 1], mask=[0, 1, 0])   # 第 1 位条件屏蔽
print(ma.where(cond, [10, 20, 30], [40, 50, 60]))   # [10, --, 30]

# choose：屏蔽下标 -> 屏蔽输出
choices = [[0,1,2,3],[10,11,12,13],[20,21,22,23],[30,31,32,33]]
idx = ma.array([2, 4, 1, 0], mask=[1, 0, 0, 1])     # 下标 0、3 屏蔽
print(ma.choose(idx, choices, mode='wrap'))          # [--, 1, 12, --]
```

**需要观察的现象**：
- `where(x>5, x, -3.1416)` 中，即便 `x` 某位屏蔽，结果该位也屏蔽（`xm` 被选中）。
- 第二个 `where` 输出第 1 位是 `--`——尽管 x=`[10,20,30]`、y=`[40,50,60]` 都没屏蔽，但因为**条件**第 1 位屏蔽，`np.where(cm, True, mask)` 把它强制屏蔽。
- `choose` 输出第 0、3 位是 `--`，因为下标被屏蔽。

**预期结果**：与上述一致。这三个现象分别对应源码的 `np.where(cf, xm, ym)`、`np.where(cm, True, mask)`、`mask_or(outputmask, getmask(indices))`。

#### 4.5.5 小练习与答案

**练习 1**：`where(cond, x, y)` 中，如果 `cond` 既不屏蔽、`x`/`y` 也都不屏蔽，但 `cond` 某位为 False，结果该位的 mask 是什么？

**参考答案**：`False`（未屏蔽）。因为 `mask = np.where(cf, xm, ym)`：该位 `cf=False`，取 `ym`，而 `y` 未屏蔽故 `ym=False`；`cm` 也为 False，所以 `np.where(cm, True, mask)` 不改变它。

**练习 2**：为什么 `where` 里要单独处理 `x is masked` / `y is masked`？

**参考答案**：因为 `masked` 单例的 dtype 是 `float64`，若直接放进 `np.where(cf, xd, yd)`，会把结果整体抬升为 float，丢失原本的 dtype 信息。源码 [core.py:L8005-L8010](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L8005-L8010) 用一个「dtype 匹配另一侧、全 0 数据 + 全 True mask」的占位数组替代它，既保住了 dtype 又表达了「该侧全部屏蔽」的语义。

---

## 5. 综合实践

把本讲的五个模块串成一条完整的「数据清洗」流水线。假设你有一组传感器读数，其中 `−999` 是缺失标记、还有些位置读数超出可信范围 `[0, 100]`，你要把它们屏蔽掉，再抽取有效区间，最后用合理值替换异常点。

```python
import numpy as np
import numpy.ma as ma

raw = np.array([10, -999, 55, 140, 23, -999, 88, 0, 170, 42])

# 步骤 1：用本讲之外的 masked_where 屏蔽缺失标记与越界值
a = ma.masked_where((raw == -999) | (raw < 0) | (raw > 100), raw)
print("a          :", a)

# 步骤 2：用 take 抽取若干下标，观察屏蔽如何跟随
print("a.take([0,1,2,8]):", a.take([0, 1, 2, 8]))   # 1、8 位屏蔽

# 步骤 3：用 where 把屏蔽位用「相邻有效值的均值」替换显示
filled_mean = a.filled(a.mean())   # mean 自动跳过屏蔽
print("where fill :", ma.where(a.mask, filled_mean, a))

# 步骤 4：用 put 解除某个屏蔽位（传感器复核后确认 140 是有效值）
a.put([3], [140])
print("after put  :", a)            # 第 3 位解除屏蔽

# 步骤 5：切换硬掩码后，再尝试解除屏蔽，观察写不进去
a[0] = 999                          # 软掩码下可以写
b = ma.array(a, hard_mask=True)
b.put([1], [50])                    # 硬掩码下第 1 位写不进去
print("hard b     :", b)
```

**任务要求**：
1. 先预测每一步的 `mask` 和 `data`，再运行核对。
2. 在步骤 4 和步骤 5 之间，分别用 `a.mask`、`a._data`、`b.mask`、`b._data` 打印，对照源码解释「`put` 能解除屏蔽、硬掩码下 `put` 写不进屏蔽位」。
3. 把 `where` 那一行改成 `ma.where(a.mask, ma.masked, a)`（即屏蔽位保持屏蔽、其余保留原值），预测输出并与原版对比，解释 `masked` 单例特判（[core.py:L8005-L8010](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L8005-L8010)）的作用。

> 提示：步骤 2 的屏蔽跟随来自 `take` 的 `_mask.take` + `outmask |= maskindices`；步骤 4 的解除屏蔽来自 `put` 的 `m.put(indices, False)`；步骤 5 的写不进来自 `put` 硬掩码分支的 `indices[~mask]` 剔除。三者都能在本讲源码精读里找到对应行。

## 6. 本讲小结

- `MaskedArray` 的所有索引/赋值操作都在做**同一件事的两个副本**：对 `_data` 做一次普通 ndarray 操作，再对 `_mask` 做一次对应操作，难点全在 mask 的边界情形。
- `__getitem__`（[core.py:L3277-L3400](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3277-L3400)）的 core 难题是判断「结果是否标量」：单个屏蔽元素返回 `masked` 单例、单个未屏蔽元素返回普通标量、多个元素返回带 `reshape` 过 mask 的 `MaskedArray`；`nomask` 时用启发式避免无谓分配。
- `__setitem__`（[core.py:L3402-L3473](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3402-L3473)）按 `nomask` / 软掩码 / 硬掩码分四条路径；软掩码直接覆盖（可解除屏蔽），硬掩码用 `mask_or` + `copyto(where=~mindx)`（不可还原），并全程用 `@np.errstate` 静音屏蔽位垃圾值引发的警告。
- `put`（[core.py:L4837-L4921](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L4837-L4921)）按扁平下标批量改写，**values 未屏蔽则解除对应位置屏蔽、values 屏蔽则设置屏蔽**；硬掩码下落在屏蔽位的写入被剔除。`putmask`（[core.py:L7552-L7605](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L7552-L7605)）按布尔掩码改写，硬掩码下用 `|=` 只加不减。
- `take`（[core.py:L6180-L6269](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6180-L6269)）沿轴抽取，data 与 mask 各 take 一次；**屏蔽的下标会让输出对应位置屏蔽**（`outmask |= maskindices`）。
- `where`（[core.py:L7930-L8019](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L7930-L8019)）与 `choose`（[core.py:L8022-L8095](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L8022-L8095)）的共同灵魂是：**条件/下标被屏蔽 ⇒ 输出强制屏蔽**（`where` 的 `np.where(cm, True, mask)`、`choose` 的 `mask_or(outputmask, getmask(indices))`）。

## 7. 下一步学习建议

- **硬/软/共享掩码的完整图景**：本讲多次用到 `_hardmask`，但只取了结论。要彻底理解 `harden_mask`/`soften_mask`/`shrink_mask`/`unshare_mask` 的机制，请学 u3-l1《硬掩码、软掩码与共享掩码》。
- **`mvoid` 与结构化标量**：本讲 `__getitem__` 里出现了 `mvoid`（结构化记录的屏蔽标量）。它是 u3-l2《子类化 MaskedArray 与 mvoid》的主题。
- **掩码感知的断言**：本讲综合实践里你手动打印 `mask`/`_data` 核对结果；更专业的做法是用 `testutils.assert_mask_equal` / `assert_equal`（它们能正确处理 `masked` 单例和 `nomask`）。详见 u3-l6《测试体系与 testutils 工具》。
- **继续阅读源码**：建议接着读 [core.py:L2660-L2766](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2660-L2766) 的 `MaskedIterator`——它的 `__getitem__`/`__setitem__` 与主类同构但更精简，适合作为「对照阅读」来巩固本讲的 mask 同步思路；再读 `compress` / `diagonal` 等其它索引类方法，体会「data 一份、mask 一份」这一通用模式的复用。
