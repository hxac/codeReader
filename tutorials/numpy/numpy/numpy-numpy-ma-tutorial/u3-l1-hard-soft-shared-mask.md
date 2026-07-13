# 硬掩码、软掩码与共享掩码

## 1. 本讲目标

本讲是 numpy.ma 专家层的第一讲，聚焦 `MaskedArray` 内部三个相互关联但常被混淆的「掩码状态开关」：

- **硬掩码（hardmask）**：一旦某位置被屏蔽，赋值还能不能把它「救回来」？
- **shrink_mask**：全 `False` 的掩码该不该继续占一整块布尔数组的内存？
- **共享掩码（sharedmask）**：切片得到的子数组，改它的掩码会不会反过来污染原数组？

学完后你应该能够：

1. 说清「软掩码」与「硬掩码」在赋值语义上的本质区别，并能用源码解释为何硬掩码不可还原。
2. 熟练使用 `harden_mask` / `soften_mask` / `shrink_mask` 三个方法，并知道它们各自只改一个布尔位或一个引用。
3. 理解 `_sharedmask` 标志与 `unshare_mask` 的关系，能预测「切片后改 mask」会不会回传到原数组。
4. 在阅读 `__new__` / `__array_finalize__` / `__getitem__` 时，一眼看出某次操作产生的数组是「共享 mask」还是「独占 mask」。

## 2. 前置知识

本讲建立在前几讲的概念之上，建议你先确认以下知识点：

- **三件套**：一个 `MaskedArray` 由 `_data`（全部原始值）、`_mask`（同形布尔，`True` 表屏蔽）、`_fill_value`（对外填充值）组成（见 u1-l4）。
- **`nomask` 单例**：代表「无屏蔽」的省内存标志，其实就是 `np.False_`，全库用 `is nomask` 做身份判断（见 u2-l1）。
- **ndarray 子类化钩子**：`__new__` 负责构造、`__array_finalize__` 负责默认传播、`__array_wrap__` 负责 ufunc 收尾（见 u2-l2）。
- **视图（view）**：`a[1:]` 这类基础索引返回的是指向同一块内存的视图，不是拷贝；改视图会改原数组。本讲的「共享掩码」正是这一性质在 `_mask` 上的体现。
- **`__setitem__` 双副本同步**：所有索引赋值都同时作用于 `_data` 与 `_mask`（见 u2-l6）。

> 通俗类比：把掩码想象成在数据上贴标签。
> - **软掩码**像贴的可擦便利贴——你重新填一个数，就把那一张揭掉。
> - **硬掩码**像盖了钢印——你填的数能写进底层 `_data`，但钢印（屏蔽标记）擦不掉。
> - **共享掩码**像两个人看同一张标签表——任何一方在上面涂改，另一方立刻看到。

## 3. 本讲源码地图

本讲全部源码集中在 **`numpy/ma/core.py`** 一个文件内，涉及的关键位置如下：

| 关注点 | 大致位置 | 作用 |
|---|---|---|
| 类默认 `_defaulthardmask` | `__new__` 之前的类属性 | 给出硬掩码默认值 `False` |
| `_shrink_mask` 函数 | 模块级工具函数 | 把全 `False` 掩码压成 `nomask` |
| `__new__` 中的 mask/hardmask 处理 | 构造主干 | 决定初始 `_mask`、`_sharedmask`、`_hardmask` |
| `_update_from` | 属性搬运工 | 在视图/切片时复制 `_hardmask`、`_sharedmask` 等簿记属性 |
| `__array_finalize__` | 子类化兜底钩子 | 用「基址是否相同」启发式决定 mask 共享或复制 |
| `__getitem__` | 读取/切片 | 切片时让结果与原数组共享同一 mask 视图 |
| `__setitem__` / `__setmask__` | 赋值主干 | 软/硬掩码的分叉点 |
| `harden_mask` / `soften_mask` / `shrink_mask` / `unshare_mask` | 四个实例方法 | 本讲的四大主角 |
| `hardmask` / `sharedmask` property | 只读属性 | 读取当前状态 |
| `put` 方法 | 批量改写 | 硬掩码下自动跳过被屏蔽下标 |

## 4. 核心概念与源码讲解

### 4.1 软掩码与硬掩码：hardmask 语义

#### 4.1.1 概念说明

默认情况下，`MaskedArray` 使用**软掩码（soft mask）**：当你给一个被屏蔽的位置赋一个确定值时，该位置的屏蔽会被**自动解除**。这符合直觉——你既然给了新值，就表示这个位置现在可信了。

但有些场景下，屏蔽代表的是「这里的数据无论如何都不能信任」（例如传感器永久故障、字段缺失不可恢复）。此时你希望赋值**不能**解除屏蔽，只能往屏蔽集合里**追加**，绝不能减少。这就是**硬掩码（hard mask）**。

一句话区分：

- **软掩码**：赋值既改 `_data`，也**覆盖** `_mask`（可解除屏蔽）。
- **硬掩码**：赋值只改 `_data` 中**未被屏蔽**的位置，`_mask` 只增不减（不可还原）。

#### 4.1.2 核心流程

赋值语义的分叉点在 `__setitem__`，它按当前 mask 状态分成四条路径：

```text
__setitem__(indx, value):
├─ _mask is nomask         → 无屏蔽：直接写 _data[indx]，按需新建 mask
├─ not self._hardmask      → 软掩码：_data[indx]=dval 且 _mask[indx]=mval（覆盖）
├─ indx 是布尔数组 + 硬掩码  → indx &= ~_mask，只在「 indx 为真 且 原未屏蔽」处写
└─ 其它（硬掩码 + 普通索引） → mindx = mask_or(_mask[indx], mval)
                             copyto(_data[indx], dval, where=~mindx)  # 只写未屏蔽位
                             _mask[indx] = mindx                       # mask 取或，只增不减
```

软硬之差的**全部秘密**就在最后两支：硬掩码用 `where=~mindx` 把被屏蔽位置挡在写入之外，并用 `mask_or`（逻辑或）合并掩码——原来为 `True` 的位永远还是 `True`。

对 `__setmask__`（直接设置整片掩码）也是同理：硬掩码走 `current_mask |= mask`（只增），软掩码走 `current_mask[...] = mask`（覆盖）。

#### 4.1.3 源码精读

先看状态读取入口 `hardmask` property，它的 docstring 用一个完整例子讲清了软硬之别：

[core.py:3656-3699](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3656-L3699) —— `hardmask` 只读属性。注意 docstring 中的演示：软掩码下 `m[8] = 42` 会把第 8 位的屏蔽解除；调用 `harden_mask` 后 `m[:] = 23` 写遍了未屏蔽位，但原来屏蔽的 6、7、9 三位依旧显示 `--`。

[core.py:3444-3472](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3444-L3472) —— `__setitem__` 的四条路径。重点看 3450 行的 `elif not self._hardmask`（软：直接覆盖 mask）与 3458 行起的硬掩码三分支；尤其 3465-3468 行：

```python
mindx = mask_or(_mask[indx], mval, copy=True)   # 旧屏蔽 | 新屏蔽，只增不减
dindx = self._data[indx]
if dindx.size > 1:
    np.copyto(dindx, dval, where=~mindx)         # 只在未屏蔽处写入新值
```

`where=~mindx` 是硬掩码「不可还原」的物理根源——被屏蔽位置的 `_data` 拿不到新值，`_mask` 又因为 `mask_or` 而保留 `True`。

[core.py:3531-3532](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3531-L3532) —— `__setmask__` 中硬掩码分支 `current_mask |= mask`（与软掩码的 `current_mask[...] = mask` 形成对照）。

[core.py:4899-4905](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L4899-L4905) —— `put` 方法在硬掩码下的处理：先把落在已屏蔽下标上的 `(indices, values)` 用 `indices[~mask]` 过滤掉，再写入。这就是为何硬掩码数组上 `put` 无法解除屏蔽。

#### 4.1.4 代码实践

**实践目标**：亲眼看到硬掩码下赋值无法解除屏蔽，而软掩码可以。

**操作步骤**（在 Python 解释器中）：

```python
import numpy as np

# 1. 构造一个软掩码数组（默认），屏蔽 >=5 的位置
m = np.ma.masked_array(np.arange(10), np.arange(10) > 5)
print("初始:", m)
# masked_array(data=[0, 1, 2, 3, 4, 5, --, --, --, --], ...)

# 2. 软掩码：给一个被屏蔽位置赋值，观察屏蔽是否解除
m[8] = 42
print("软掩码 m[8]=42 后:", m)
# 第 8 位恢复成 42，mask[8] 变 False

# 3. 切一片新的做硬掩码实验，避免互相干扰
h = np.ma.masked_array(np.arange(10), np.arange(10) > 5)
h.harden_mask()
print("hardmask?", h.hardmask)   # True

# 4. 硬掩码：尝试给被屏蔽位置赋值
h[8] = 42
print("硬掩码 h[8]=42 后:", h)
print("h._data[8] =", h._data[8], " h._mask[8] =", h._mask[8])
```

**需要观察的现象**：

- 软掩码那组：`m[8]` 显示为 `42`，`m._mask[8]` 为 `False`——屏蔽被解除。
- 硬掩码那组：`h[8]` 仍显示 `--`，但 `h._data[8]` 可能仍是原值（因为 `where=~mindx` 挡住了写入），`h._mask[8]` 仍为 `True`——屏蔽未被解除。

**预期结果**：硬掩码下 `_data[8]` 不变（仍为 8），`_mask[8]` 仍为 `True`；软掩码下 `_data[8]` 变为 42、`_mask[8]` 变为 `False`。这一对比正是「钢印擦不掉」的体现。

#### 4.1.5 小练习与答案

**练习 1**：硬掩码数组上执行 `h[:] = 23`（整体赋值）后，哪些位置的 `_data` 会变成 23？哪些不会？

> **答案**：只有原本**未屏蔽**的位置（`_mask` 为 `False`）的 `_data` 会变成 23；被屏蔽位置的 `_data` 因 `where=~mindx` 被挡住，保持原值，且 `_mask` 全部维持 `True`。

**练习 2**：为什么 `put` 方法在硬掩码下用 `indices[~mask]` 过滤，而不是像 `__setitem__` 那样用 `copyto(where=...)`？

> **答案**：`put` 是按「扁平下标列表」批量写入，过滤掉落在屏蔽位的下标后，剩余下标直接走普通 `ndarray.put`，实现更简洁；`__setitem__` 面对的是任意索引表达式，用 `copyto(where=...)` 更通用。两者效果一致：硬掩码下都不触碰被屏蔽位置。

---

### 4.2 harden_mask / soften_mask：翻转一个布尔位

#### 4.2.1 概念说明

`harden_mask` 和 `soften_mask` 看起来像「大动作」，但源码极其朴素——它们各只做一件事：把内部标志 `self._hardmask` 置为 `True` 或 `False`，然后 `return self`。返回 `self` 是为了支持**链式调用**，例如 `a.harden_mask().__iadd__(1)`。

关键认知：**它们不改 `_data`，也不改 `_mask`，只拨动一个布尔开关。**真正「生效」是在下一次 `__setitem__` / `__setmask__` / `put` 被调用时，由那些方法去读 `_hardmask` 决定走哪条分支。

#### 4.2.2 核心流程

```text
创建阶段:
  __new__(..., hard_mask=None)
    └─ hard_mask is None ? 继承 data._hardmask : 直接用 hard_mask
       → 写入 _data._hardmask

传播阶段（视图/切片/ufunc）:
  _update_from(obj) 把 obj._hardmask 复制给 self._hardmask

显式切换阶段:
  harden_mask()  → _hardmask = True ; return self
  soften_mask()  → _hardmask = False; return self
```

类属性 `_defaulthardmask = False` 给出了「不显式指定时的默认值」——即默认软掩码。

#### 4.2.3 源码精读

[core.py:2874](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2874) —— 类属性 `_defaulthardmask = False`，说明默认是软掩码。

[core.py:3018-3021](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3018-L3021) —— `__new__` 末尾对 `hard_mask` 参数的处理：未传则从 `data` 继承，传了就直接用。这就是 `np.ma.array(data, hard_mask=True)` 能一步得到硬掩码数组的入口。

[core.py:3620-3636](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3620-L3636) —— `harden_mask`，全部实质逻辑只有 `self._hardmask = True` 与 `return self`。

[core.py:3638-3654](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3638-L3654) —— `soften_mask`，对称地置 `False`。

[core.py:3040-3042](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3040-L3042) —— `_update_from` 把 `_hardmask`（连同 `_sharedmask`、`_fill_value` 等）从模板对象搬到新对象，保证切片/视图后的子数组继承硬掩码状态。

[core.py:7106](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L7106) 与 [core.py:7117](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L7117) —— 模块级 `ma.harden_mask` / `ma.soften_mask` 由 `_frommethod` 工厂生成，是对实例方法的函数式封装（`np.ma.harden_mask(a)` 等价于 `a.harden_mask()`）。

#### 4.2.4 代码实践

**实践目标**：验证 `harden_mask` 返回的是 `self`（可链式），且「切换」本身不触碰数据。

**操作步骤**：

```python
import numpy as np

a = np.ma.array([1, 2, 3], mask=[0, 1, 0])
before_data = a._data.copy()
before_mask = a._mask.copy()

ret = a.harden_mask()
print("返回的是 self 吗?", ret is a)        # True（链式可用）
print("hardmask 现在是?", a.hardmask)        # True

# 切换动作本身不应改动 _data / _mask
print("data 未变?", np.array_equal(a._data, before_data))   # True
print("mask 未变?", np.array_equal(a._mask, before_mask))   # True

# 用构造参数一步到位
b = np.ma.array([1, 2, 3], mask=[0, 1, 0], hard_mask=True)
print("构造即硬掩码?", b.hardmask)           # True
```

**需要观察的现象**：`harden_mask()` 返回 `self`；调用前后 `_data`、`_mask` 逐位不变；`hard_mask=True` 构造参数直接产出硬掩码数组。

**预期结果**：三处打印依次为 `True`、`True`、`True`、`True`、`True`。

#### 4.2.5 小练习与答案

**练习 1**：对一个数组先 `harden_mask()` 再 `soften_mask()`，随后给被屏蔽位赋值，会发生什么？

> **答案**：`soften_mask()` 把 `_hardmask` 拨回 `False`，此后的赋值走软掩码分支，可以解除屏蔽。状态由**最后一次切换**决定，与历史无关。

**练习 2**：为什么 `harden_mask` 要 `return self` 而不是 `return None`？

> **答案**：为了支持链式调用与函数式写法，例如 `np.ma.harden_mask(a).__str__()`。`_frommethod` 封装的模块级版本也依赖这个返回值把结果透传给调用者。

---

### 4.3 shrink_mask：把全 False 掩码压缩为 nomask

#### 4.3.1 概念说明

回顾 u2-l1：`nomask`（即 `np.False_`）是「无屏蔽」的省内存单例。一个 100 万元素的布尔 mask 要占 1MB；如果它全 `False`，用一个标量 `False` 就能表达同样的信息。

`shrink_mask` 就是主动检查「当前 mask 是否全 `False`」，若是，则把整个布尔数组**替换**为 `nomask` 单例，释放内存。这个动作在很多内部路径（如 `make_mask(..., shrink=True)`、归约后重算掩码）会自动发生，`shrink_mask` 方法让你能手动触发。

#### 4.3.2 核心流程

压缩判定由模块级函数 `_shrink_mask` 完成，逻辑极简：

```text
_shrink_mask(m):
  if m 没有命名字段  and  not m.any():   # 纯布尔且全 False
      return nomask
  else:
      return m                          # 否则原样返回
```

注意一个细节：`m.dtype.names is None` 这个前提意味着——**结构化（带字段）dtype 的掩码即使全 `False` 也不会被压缩**。因为对结构化数组调 `.any()` 的语义与纯布尔不同，库作者选择保守地不压缩。

#### 4.3.3 源码精读

[core.py:1597-1604](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1597-L1604) —— `_shrink_mask` 工具函数。`m.dtype.names is None` 限定纯布尔；`not m.any()` 判定全 `False`；二者皆满足才返回 `nomask`。

[core.py:3724-3755](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3724-L3755) —— 实例方法 `shrink_mask`，实质只有 `self._mask = _shrink_mask(self._mask); return self`。docstring 演示了 `mask=False`（即 `nomask`）取代二维全 `False` 数组的过程。

[core.py:7116](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L7116) —— 模块级 `ma.shrink_mask`，同样由 `_frommethod` 生成。

> 旁证：`_shrink_mask` 在构造路径（[core.py:1597](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1597) 起的 `make_mask`）和归约重算掩码处被反复调用，说明「能压就压」是全库一致的设计取向。

#### 4.3.4 代码实践

**实践目标**：观察 `shrink_mask` 前后 `_mask` 的类型与身份变化。

**操作步骤**：

```python
import numpy as np

# 1. 强制构造一个「全 False 但非 nomask」的掩码数组（用 shrink=False）
a = np.ma.array([1, 2, 3], mask=[0, 0, 0], hard_mask=False)  # 通常会被自动压缩
# 想拿到显式的全 False 布尔数组，借助 masked_where 的内部路径或直接：
a = np.ma.array([1, 2, 3])
a._mask = np.zeros(3, dtype=bool)   # 人为塞一个全 False 布尔数组
print("压缩前 a._mask 是:", repr(a._mask), " 是否 nomask?", a._mask is np.ma.nomask)

a.shrink_mask()
print("压缩后 a._mask 是:", repr(a._mask), " 是否 nomask?", a._mask is np.ma.nomask)
```

**需要观察的现象**：压缩前 `_mask` 是一个 `array([False, False, False])`，`is nomask` 为 `False`；压缩后 `_mask` 变成 `False`（即 `nomask`），`is nomask` 为 `True`。

**预期结果**：第一次 `is nomask` 为 `False`，第二次为 `True`。注意日常用 `np.ma.array([1,2,3])` 构造时，库已在 `__new__` 里（经 `shrink=True`）自动压缩过，所以通常拿不到「全 False 布尔数组」状态，本实践用 `a._mask = np.zeros(...)` 人为构造以观察 `shrink_mask` 的效果。**待本地验证**：不同 NumPy 版本对 `a._mask = ...` 直接赋值的接受度一致，但行为符合上述描述。

#### 4.3.5 小练习与答案

**练习 1**：一个结构化 dtype 的掩码数组，即使所有字段所有位置都未屏蔽，`shrink_mask()` 能把它压成 `nomask` 吗？为什么？

> **答案**：不能。`_shrink_mask` 要求 `m.dtype.names is None`，结构化掩码有字段名，不满足该前提，故直接返回原 `m`。这是有意为之的保守策略。

**练习 2**：`shrink_mask` 与 `make_mask(..., shrink=True)` 的关系是什么？

> **答案**：二者底层都调用 `_shrink_mask`。`shrink_mask` 是面向已存在数组的实例方法；`make_mask` 在构造掩码时按 `shrink` 参数决定是否压缩。它们共享同一段「能压就压」逻辑。

---

### 4.4 共享掩码：sharedmask / unshare_mask / _sharedmask

#### 4.4.1 概念说明

当你对一个 `MaskedArray` 做切片 `b = a[1:]` 时，`b._data` 是 `a._data` 的**视图**（同一块内存）。问题是：`b._mask` 呢？

答案是：默认情况下 `b._mask` 也是 `a._mask` 的视图——**两个数组共享同一个 mask 对象**。这意味着如果你修改 `b` 的掩码（例如给 `b` 的某个位置解除屏蔽），改动会**回传（back-propagate）**到 `a`。这有时是 desired（视图本就该同步），有时是 footgun（你只想改局部）。

为了让你能掌控这件事，库维护了一个布尔标志 `_sharedmask`：

- `_sharedmask = True`：我的 `_mask` 与某个上游数组共享同一内存，原地改 `_mask` 会影响对方。
- `_sharedmask = False`：我的 `_mask` 是独占的，怎么改都不影响别人。

`unshare_mask()` 就是「我想独占」的开关：若当前共享，它**拷贝**一份 mask 再把标志置 `False`；若已独占，则什么都不做。

#### 4.4.2 核心流程

`_sharedmask` 在多个生命周期点被设定，理解这些点就能预测任何数组的共享状态：

```text
__new__（构造）:
  copy=False 且 mask 来自 data        → _sharedmask = not copy = True   # 与源共享
  copy=True                           → _sharedmask = not copy = False  # 拷贝，独占
  mask 经 logical_or 合并              → _sharedmask = False             # 新数组，独占

__getitem__（切片 b = a[idx]）:
  b._mask = a._mask[idx]              # 基础索引返回视图！
  b._sharedmask = True                # 子切片默认与父共享

__array_finalize__（视图/astype 等兜底）:
  if 基址相同（同一块数据内存）:        → 取 _mask.view()（共享）
  else:                                → _mask.astype(...)（拷贝，独占）

__array_wrap__（ufunc 结果）:
  result._sharedmask = False           # ufunc 结果总是独占

显式解绑:
  unshare_mask(): if _sharedmask: _mask = _mask.copy(); _sharedmask = False
```

一句话：**切片产生共享，ufunc 与拷贝产生独占，`unshare_mask` 把共享强制变独占。**

#### 4.4.3 源码精读

[core.py:3701-3717](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3701-L3717) —— `unshare_mask`：仅当 `_sharedmask` 为真时拷贝 mask 并置 `False`，否则零成本返回。这是「按需拷贝（copy-on-write 的手动版）」思想。

[core.py:3719-3722](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3719-L3722) —— `sharedmask` 只读 property，返回 `_sharedmask`。

[core.py:2943](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2943) 与 [core.py:2991-3009](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2991-L3009) —— `__new__` 中 `_sharedmask = not copy` 的赋值点。`copy=False` 时为 `True`（与源共享），`copy=True` 时为 `False`；一旦 mask 经过 `logical_or` 合并（2996-3009 行），结果必然是新数组，故直接置 `False`。

[core.py:3395-3398](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3395-L3398) —— `__getitem__` 切片分支：`dout._mask = reshape(mout, dout.shape)`（`mout = _mask[indx]` 是视图），随后 `dout._sharedmask = True`。这是「切片共享掩码」的直接证据。

[core.py:3099-3121](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3099-L3121) —— `__array_finalize__` 的启发式：用 `obj.__array_interface__["data"][0] != self.__array_interface__["data"][0]`（数据基址是否不同）决定——基址相同（典型视图）则 `_mask.view()`（共享），基址不同（如 `astype`）则 `_mask.astype(...)`（拷贝）。源码注释坦承这是 guesswork and heuristics，并不 100% 可靠。

[core.py:3199](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3199) —— `__array_wrap__`（ufunc 收尾）把 `result._sharedmask = False`，保证 ufunc 结果的 mask 独占、可安全原地修改。

[core.py:3042](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3042) —— `_update_from` 在搬运簿记属性时把 `_sharedmask` 一并复制，这就是切片/视图能继承父数组共享状态的管道。

#### 4.4.4 代码实践

**实践目标**：复现「切片共享 → 改子数组 mask 回传到父数组」，再用 `unshare_mask` 切断回传。

**操作步骤**：

```python
import numpy as np

a = np.ma.masked_array([10, 20, 30, 40], mask=[0, 1, 0, 1])
print("原始 a:", a)

# 1. 切片得到 b，b 与 a 共享 mask
b = a[1:3]                      # 取 [20(--), 30]
print("b.sharedmask?", b.sharedmask)   # True

# 2. 在 b 上解除某位屏蔽（软掩码赋值），观察 a 是否被牵连
b[0] = 999                      # b 的第 0 位对应 a 的第 1 位
print("改 b 后的 a:", a)         # 关注 a[1] 是否跟着变了

# 3. 重新构造，先 unshare 再改，观察隔离效果
a2 = np.ma.masked_array([10, 20, 30, 40], mask=[0, 1, 0, 1])
c = a2[1:3]
c.unshare_mask()                # 先解绑：拷贝 mask，_sharedmask=False
print("c.sharedmask?", c.sharedmask)   # False
c[0] = 999
print("unshare 后改 c，a2:", a2)        # a2 应保持不变
```

**需要观察的现象**：

- 第 2 步：`b.sharedmask` 为 `True`；改 `b[0]` 后，`a[1]` 的掩码也被解除、数据显示出来——回传发生。
- 第 3 步：`c.sharedmask` 为 `False`；改 `c[0]` 后 `a2` 不受影响——隔离成功。

**预期结果**：共享组中 `a` 会随 `b` 改变（`a[1]` 由 `--` 变为可见的 `999`）；解绑组中 `a2` 保持原样。这一对比直接验证了 `_sharedmask` 的作用。**待本地验证**：回传是否触发取决于 NumPy 是否在 `__setitem__` 路径上对共享 mask 做了隐式 unshare；若你的版本上 `a[1]` 未变，说明该版本在该路径增加了自动 unshare，请结合你本地的 `__setitem__` 源码（3450-3457 行）核对。

#### 4.4.5 小练习与答案

**练习 1**：`b = a + 1`（ufunc 加法）得到的 `b`，其 `sharedmask` 是 `True` 还是 `False`？为什么？

> **答案**：`False`。加法走 `__array_wrap__`，它在 [core.py:3199](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3199) 显式置 `result._sharedmask = False`。ufunc 结果是新数组，mask 自然独占，安全可改。

**练习 2**：`unshare_mask()` 在 `_sharedmask` 已经是 `False` 时会有什么开销？

> **答案**：零拷贝开销。方法体先判断 `if self._sharedmask:`，已为 `False` 时直接 `return self`，不做任何拷贝。这是「按需付费」设计。

**练习 3**：`__array_finalize__` 用「数据基址是否相同」来决定 mask 共享还是拷贝，为什么说这只是启发式、不可靠？

> **答案**：基址相同通常意味着 `self` 是 `obj` 的简单视图（如 `obj[...]`），此时共享 mask 合理；但存在反例，例如 `self` 是 `obj` 的某一行或有奇特 strides，基址相同却并非整片对应。源码注释（[core.py:3092-3098](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3092-L3098)）明说这只是「not bad」的猜测，并非精确判定。

---

## 5. 综合实践

把本讲四个模块串成一条完整链路。**目标**：模拟「读取一批带永久故障传感器的数据」——故障位必须永久屏蔽（硬掩码），中途切片分析时不能污染原始数据（unshare），最后把全好的辅助数组压缩存储（shrink）。

```python
import numpy as np

# 原始读数：第 1、4 号传感器永久故障，用硬掩码锁定
raw = np.ma.array([12.0, 99.0, 15.0, 13.0, 99.0, 14.0],
                  mask=[0, 1, 0, 0, 1, 0],
                  hard_mask=True)          # ← 模块 4.2：构造即硬掩码
print("raw.hardmask?", raw.hardmask)       # True

# 误操作尝试：给故障位赋“好值”，应被拒绝（屏蔽不解除）
raw[1] = 7.0
print("硬掩码下 raw[1] 仍屏蔽?", raw._mask[1])   # True（模块 4.1）

# 取一段做独立分析：先 unshare，避免回传污染 raw
segment = raw[0:3]
print("切片 segment.sharedmask?", segment.sharedmask)  # True
segment.unshare_mask()                     # ← 模块 4.4：切断共享
print("unshare 后?", segment.sharedmask)   # False
# 现在 segment 上做软掩码修正不会影响 raw
segment.soften_mask()                      # ← 模块 4.2：切回软掩码
segment._data[0] = 12.5                    # 仅改数据演示
print("raw[0] 是否被牵连?", raw._data[0])   # 应仍为 12.0（已 unshare）

# 另有一份“全好”的辅助数组，压缩其掩码省内存
aux = np.ma.array([1.0, 2.0, 3.0])
aux._mask = np.zeros(3, dtype=bool)        # 人为造一个全 False 布尔 mask
print("压缩前 aux._mask 类型:", type(aux._mask).__name__)   # ndarray
aux.shrink_mask()                          # ← 模块 4.3：压成 nomask
print("压缩后 aux._mask is nomask?", aux._mask is np.ma.nomask)  # True
```

**完成标志**：你能口头解释每一行注释对应的源码位置（`__new__` 的 `hard_mask` 参数、`__setitem__` 的 `where=~mindx`、`unshare_mask` 的 `if self._sharedmask`、`_shrink_mask` 的 `not m.any()`），并且运行结果与注释中的预期一致。**待本地验证**：综合实践中涉及直接赋值 `segment._data` / `aux._mask`，仅为演示内部机制；生产代码应使用公开 API（如 `filled`、`mask` setter）。

## 6. 本讲小结

- **软 vs 硬**：软掩码下赋值覆盖 `_mask`、可解除屏蔽；硬掩码下赋值用 `copyto(where=~mindx)` 只写未屏蔽位，`_mask` 经 `mask_or` 只增不减，故不可还原。
- **harden/soften 极简**：二者只拨动 `_hardmask` 布尔位并 `return self`，不改数据；默认值由 `_defaulthardmask = False` 与 `__new__` 的 `hard_mask` 参数控制。
- **shrink 省内存**：`_shrink_mask` 把「纯布尔且全 `False`」的 mask 替换为 `nomask` 单例；结构化掩码不压缩。
- **共享是视图的副产物**：切片的 `_mask` 是父数组 mask 的视图，`_sharedmask=True`，改子掩码会回传；ufunc 结果与拷贝则 `_sharedmask=False`。
- **unshare 按需拷贝**：`unshare_mask()` 仅在共享时拷贝 mask 并置 `False`，是切断回传、获得独占 mask 的官方手段。
- **`__array_finalize__` 不可全信**：它用「数据基址是否相同」猜测 mask 共享与否，源码自承为 heuristics，精确控制仍需 `unshare_mask`。

## 7. 下一步学习建议

本讲弄清了 `_hardmask` 与 `_sharedmask` 两个内部标志，它们在后续讲义中会反复出现：

- **u3-l2 子类化 MaskedArray 与 mvoid**：子类化时若重写 `__array_finalize__`，必须正确传播 `_hardmask` / `_sharedmask`，否则切片后的子类实例会丢失硬掩码语义。
- **u3-l4 持久化：pickle、重建与深拷贝**：`__getstate__` / `__setstate__` 需要序列化 `_hardmask`，反序列化后 `_sharedmask` 的重置策略值得对照阅读。
- **u3-l5 mrecords 与字段级屏蔽**：`MaskedRecords` 在结构化 dtype 下如何继承硬掩码、`_fieldmask` 与整体 `recordmask` 的关系，是本讲 4.3 节「结构化掩码不 shrink」的延伸。

建议接下来精读 `_update_from`（[core.py:3025-3048](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3025-L3048)）与 `__array_finalize__`（[core.py:3050-3135](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3050-L3135)），把「簿记属性的搬运链」彻底打通，这是理解所有掩码状态如何在视图与运算中传播的总钥匙。
