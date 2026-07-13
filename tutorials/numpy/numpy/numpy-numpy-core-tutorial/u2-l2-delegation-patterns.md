# 委派模式：sentinel 与 None 两种缺失属性处理

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `numpy.core` 各垫片（shim）在「判断属性是否真的缺失」时使用的两种写法。
- 解释为什么用 `None` 作为 `getattr` 的默认值在面对「值为 `None` 的真属性」时会误判。
- 解释为什么 `sentinel = object()` 这种哨兵写法对所有取值都安全。
- 根据「被转发模块里是否可能出现值为 `None` 的公开属性」选择合适的委派写法。

本讲只聚焦一个小问题：**当垫片的 `__getattr__` 拿到一个名字时，怎么判断这个名字是「废弃但仍然存在」还是「从来就不存在」？** 这是一个看似琐碎、实则容易埋雷的判断。

## 2. 前置知识

本讲建立在你已经学过 [u2-l1 模块级 `__getattr__`（PEP 562）与惰性转发](u2-l1-module-getattr.md) 的基础之上。如果你还不清楚「模块级 `__getattr__` 只在属性查找失败时被回调」，请先读那一讲。

这里再补三个 Python 基础概念：

1. **`getattr(obj, name, default)` 的三参数形式**。
   当 `obj` 上**存在**名为 `name` 的属性时，返回它的值（哪怕该值是 `None`、`0`、`False`）。
   当属性**不存在**时，返回 `default`，而**不抛异常**。
   也就是说，`getattr` 把「属性不存在」这件事翻译成了一个「可被你指定的返回值」。

2. **恒等判断 `is` 与相等判断 `==` 的区别**。
   `a is b` 为真，当且仅当 `a` 和 `b` 是同一个对象（同一块内存）。
   对于 `None`，Python 保证全局只有一个 `None`，所以 `x is None` 是判断「`x` 是不是那个唯一的 `None`」的标准写法。
   注意：`0 == False` 为真，但 `0 is None` 为假；`is` 不受「真值」影响。

3. **falsy（假值）与 `None` 不是一回事**。
   `0`、`False`、`""`、`[]`、`None` 在 `if` 里都判定为假，但它们彼此并不是同一个对象。
   所以「真值判断 `if not ret`」和「恒等判断 `if ret is None`」是两件不同的事——本讲的核心坑，就来自这两者的混淆。

> 一句话：本讲的两种写法，区别只在于「用什么作为 `getattr` 的 `default`、又用什么来判断『拿到的是 default 还是真值』」。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `numeric.py` | 纯转发垫片（哨兵写法） | 全目录**唯一**使用 `sentinel = object()` 的垫片 |
| `umath.py` | 纯转发垫片（None 写法） | 最干净的 None 写法范例 |
| `records.py` | 纯转发垫片（None 写法） | 与 `umath.py` 结构完全一致，可作对照 |
| `_utils.py` | 工具 | 提供 `_raise_warning`，两种写法都调用它 |

铺垫一句：在 [u1-l2 目录结构与文件分类](u1-l2-directory-map.md) 里我们已经确认，纯转发垫片共 14 个，其中 **13 个用 None 写法、仅 `numeric.py` 用 sentinel 写法**。本讲就把这两种写法并排读透。

## 4. 核心概念与源码讲解

### 4.1 共同骨架：getattr 的三参数与「缺失检测」

#### 4.1.1 概念说明

无论是 sentinel 还是 None，两种委派写法共享同一段骨架：

1. 在 `__getattr__` 函数体里惰性地 `import` 真正的实现模块（如 `numpy._core.umath`）。
2. 用 `getattr(真模块, attr_name, 默认值)` 取属性，并把「属性不存在」这件事映射成「拿到默认值」。
3. 判断拿到的到底是不是默认值：
   - 如果**是**默认值 → 属性真的不存在 → 抛 `AttributeError`。
   - 如果**不是**默认值 → 属性存在（只是被废弃了）→ 调 `_raise_warning` 报弃用警告，然后正常返回。

两种写法的**唯一区别**，就是第 2、3 步里的「默认值」选什么、第 3 步用什么方式判断。

#### 4.1.2 核心流程

用伪代码描述这段共同骨架：

```text
def __getattr__(attr_name):
    real = import 真模块
    default = <某种「绝不可能等于真值」的东西>   # None 或 sentinel
    ret = getattr(real, attr_name, default)
    if ret 就是 default:                          # 属性真的缺失
        raise AttributeError(...)
    _raise_warning(attr_name, 子模块名)            # 存在但废弃 → 报警
    return ret                                    # 同时正常返回
```

关键洞察：这套写法是否安全，**完全取决于「默认值」能不能与某个真实的属性值撞车**。

- 如果默认值是 `None`，而真模块里恰好有一个属性的值也是 `None`，那么「真值 `None`」和「默认值 `None`」无法区分 → 误判为缺失。
- 如果默认值是一个**全新创建、全程序唯一**的对象（sentinel），那么没有任何真实属性会等于它 → 永远不会误判。

#### 4.1.3 源码精读

两种写法的对照，先看骨架最干净的两个文件。

`umath.py` 全文（None 写法）：

[umath.py:1-11](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/umath.py#L1-L11) —— 整个垫片只有一个 `__getattr__`，用 `None` 作默认值。

`numeric.py` 全文（sentinel 写法）：

[numeric.py:1-13](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py#L1-L13) —— 同样只有一个 `__getattr__`，但用 `sentinel = object()` 作默认值。

两个文件长度几乎一样、结构几乎一样，差别只在第 5～8 行。下面两节分别精读。

#### 4.1.4 代码实践

打开一个 Python REPL（不需要 numpy），用最短的代码体会「三参数 `getattr` 把『缺失』翻译成『默认值』」：

```python
class M:
    ZERO = 0          # 真实存在，但是 falsy
    NONE = None       # 真实存在，且值就是 None
    REAL = 42

print(getattr(M, "ZERO", "缺省"))   # 0       —— 存在，返回真值
print(getattr(M, "NONE", "缺省"))   # None    —— 存在，返回真值（注意不是"缺省"）
print(getattr(M, "REAL", "缺省"))   # 42
print(getattr(M, "MISSING", "缺省"))  # 缺省  —— 不存在，返回默认值
```

**需要观察的现象**：`M.NONE` 存在时，`getattr` 返回的是真值 `None`，而不是字符串 `"缺省"`。这印证了「`getattr` 只在属性不存在时才返回 default」。

#### 4.1.5 小练习与答案

**练习 1**：`getattr(obj, "x", None)` 返回了 `None`。你能据此断定 `obj.x` 不存在吗？

> **答案**：不能。返回 `None` 有两种可能：①`obj.x` 不存在（于是返回了默认值 `None`）；②`obj.x` 存在，但它的值就是 `None`。这正是 None 写法的盲点。

**练习 2**：为什么 numpy 的 None 写法用的是 `if ret is None`，而不是 `if not ret`？

> **答案**：`is None` 是恒等判断，只有值真的是那个唯一的 `None` 才为真；而 `not ret` 会把 `0`、`False`、`""`、`[]` 等所有 falsy 值都判为「缺失」。numpy 用 `is None`，意味着它已经避开了「`0` 被误判」这一类坑，只剩「值为 `None` 的真属性」这一种盲点。详见 4.2。

---

### 4.2 None 默认值写法（umath.py、records.py）

#### 4.2.1 概念说明

None 写法直接拿 `None` 当 `getattr` 的默认值，再用 `if ret is None` 判断。它最大的优点是**简短**——不需要额外创建对象。numpy 的 13 个纯转发垫片都采用了它。

它的缺点也很明确：**无法区分「属性不存在」和「属性存在但值为 `None`」**。只要被转发的真模块里出现一个值为 `None` 的公开名字，这个写法就会把它当成「不存在」抛 `AttributeError`——一个本该能用的属性，被错误地拒之门外。

> 注意：本讲规格里提到「用值为 0 的属性来演示 None 写法失败」。但因为 numpy 实际用的是 `is None`（恒等判断），**值为 `0` 的属性其实能被正确转发**（`0 is None` 为假）。真正的盲点是「值为 `None` 的属性」。本节和综合实践都用 `None` 来演示真实陷阱，避免误导。

#### 4.2.2 核心流程

None 写法的判定逻辑：

```text
ret = getattr(real, name, None)
如果 ret is None:
    # 两种情况无法区分：
    #   (a) name 真的不存在  →  应该报错
    #   (b) name 存在但值是 None  →  本该返回 None，却也被报错了（BUG）
    raise AttributeError(...)
否则:
    _raise_warning(...)   # 存在（且非 None）→ 报废警告
    return ret
```

用一个最小真值表对照「属性是否存在 × 属性值」与 None 写法的判定结果：

| 真实情况 | `getattr` 返回 | `ret is None` | None 写法的判定 | 是否正确 |
| --- | --- | --- | --- | --- |
| 不存在 | `None`（默认值） | 真 | 抛 `AttributeError` | ✅ 正确 |
| 存在，值 `42` | `42` | 假 | 报警并返回 `42` | ✅ 正确 |
| 存在，值 `0` | `0` | 假 | 报警并返回 `0` | ✅ 正确 |
| 存在，值 `None` | `None` | 真 | 抛 `AttributeError` | ❌ 误判 |

最后一行就是 None 写法唯一的、也是致命的盲点。

#### 4.2.3 源码精读

[umath.py:5-8](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/umath.py#L5-L8) —— 这三行就是 None 写法的全部精髓：

```python
ret = getattr(umath, attr_name, None)
if ret is None:
    raise AttributeError(
        f"module 'numpy.core.umath' has no attribute {attr_name}")
```

`records.py` 用的是**完全相同**的结构，只是把模块名换成 `records`：

[records.py:5-8](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/records.py#L5-L8) —— 与 `umath.py` 逐行对应，可作对照阅读。

为什么 numpy 敢在这么多垫片里用 None 写法？因为这些被转发的模块（`umath`、`records`、`function_base` 等）导出的名字几乎都是函数、ufunc 或对象类型，**没有一个公开名字的值会是 `None`**。于是在实际使用中，None 写法「恰好」不会出错。这是一种**依赖外部不变量**的简化：只要真模块承诺「公开名字不会是 `None`」，None 写法就足够。

#### 4.2.4 代码实践

在 REPL 里复现 None 写法的盲点（不需要 numpy）：

```python
import types
real = types.SimpleNamespace(ZERO=0, NONE=None, REAL=42)

def none_getattr(name):
    ret = getattr(real, name, None)
    if ret is None:
        raise AttributeError(f"no attribute {name}")
    return ret

print(none_getattr("ZERO"))    # 0   —— falsy 但能正确返回
try:
    none_getattr("NONE")       # 真实存在！值就是 None
except AttributeError as e:
    print("误判:", e)           # 却被当成了「不存在」
```

**需要观察的现象**：`ZERO`（值为 `0`）能正常返回 `0`；而 `NONE`（值为 `None`，**真实存在**）却被抛了 `AttributeError`。

**预期结果**：输出 `0`，随后输出 `误判: no attribute NONE`。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `umath.py` 的 `if ret is None` 改成 `if not ret`，会引入哪一类新 bug？

> **答案**：会把所有 falsy 的真属性（`0`、`False`、`""`、`[]` 等）都误判为「不存在」。`is None` 只误判值为 `None` 的属性；`not ret` 的误判范围大得多。所以 numpy 选 `is None` 是更小心的写法。

**练习 2**：假设有一天 `numpy._core.umath` 里新增了一个公开常量 `DEFAULT_DTYPE = None`，None 写法会出现什么问题？

> **答案**：访问 `numpy.core.umath.DEFAULT_DTYPE` 会拿到 `None`，被 `if ret is None` 判定为「不存在」而抛 `AttributeError`——一个本应可用的属性被错误拒识。这就是 None 写法依赖「公开名字不为 `None`」这一外部不变量所付出的代价。

---

### 4.3 sentinel 哨兵写法（numeric.py）

#### 4.3.1 概念说明

sentinel（哨兵）写法的思路是：**临时创建一个全程序唯一的对象**，把它当作 `getattr` 的默认值。由于这个对象是刚刚 `object()` 出来的、没有任何别处引用它，所以**没有任何真实的属性值会等于它**。于是「拿到默认值」就唯一地等价于「属性不存在」，没有任何盲点。

`numeric.py` 是 `numpy/core` 目录里**唯一**采用这种写法的垫片。

#### 4.3.2 核心流程

sentinel 写法的判定逻辑：

```text
sentinel = object()                       # 每次调用都新建一个唯一对象
ret = getattr(real, name, sentinel)
如果 ret is sentinel:                      # 只有「真缺失」才会拿到 sentinel
    raise AttributeError(...)             # 真的不存在 → 报错
否则:
    _raise_warning(...)                   # 存在（任何取值，包括 None）→ 报警
    return ret
```

对照真值表：

| 真实情况 | `getattr` 返回 | `ret is sentinel` | sentinel 写法的判定 | 是否正确 |
| --- | --- | --- | --- | --- |
| 不存在 | `sentinel`（默认值） | 真 | 抛 `AttributeError` | ✅ 正确 |
| 存在，值 `42` | `42` | 假 | 报警并返回 `42` | ✅ 正确 |
| 存在，值 `0` | `0` | 假 | 报警并返回 `0` | ✅ 正确 |
| 存在，值 `None` | `None` | 假 | 报警并返回 `None` | ✅ 正确 |

最后一行正是 sentinel 相对 None 的关键优势：**值为 `None` 的真实属性也能被正确返回**。

#### 4.3.3 源码精读

[numeric.py:6-8](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py#L6-L8) —— sentinel 写法的三行：

```python
sentinel = object()
ret = getattr(numeric, attr_name, sentinel)
if ret is sentinel:
```

要点逐条解释：

- `sentinel = object()`：`object()` 每次调用都产生一个**全新的、唯一的**实例。`sentinel is object()` 永远为假（这是两个不同对象），所以它绝不会被任何真实属性「碰巧等于」。
- `getattr(numeric, attr_name, sentinel)`：把「缺失」映射成「拿到这个 sentinel」。
- `if ret is sentinel`：用恒等判断确认拿到的就是默认值本身。由于 sentinel 唯一，这个判断**没有盲点**。

代码里**没有注释说明**为什么 `numeric.py` 单独选用 sentinel、而其它垫片用 None。但从工程角度看：`numeric` 模块导出的名字种类更杂（标量、常量、类型对象等都有），未来出现一个值为 `None` 的名字的风险更高；sentinel 是一种「不依赖外部不变量」的稳健写法。多花一行代码，换来对任何取值都正确的保证。

#### 4.3.4 代码实践

在 REPL 里验证 sentinel 的「唯一性」与安全性：

```python
import types
real = types.SimpleNamespace(ZERO=0, NONE=None, REAL=42)

def sentinel_getattr(name):
    sentinel = object()
    ret = getattr(real, name, sentinel)
    if ret is sentinel:
        raise AttributeError(f"no attribute {name}")
    return ret

print(sentinel_getattr("ZERO"))    # 0
print(sentinel_getattr("NONE"))    # None —— 真实属性，正确返回（None 写法会在这里翻车）
print(sentinel_getattr("REAL"))    # 42
try:
    sentinel_getattr("MISSING")
except AttributeError as e:
    print("正确报错:", e)

# 体会 sentinel 的唯一性：两次 object() 得到的是不同对象
print(object() is object())        # False
```

**需要观察的现象**：`NONE` 这次能正确返回 `None`，不再被误判；只有 `MISSING`（真的不存在）才抛 `AttributeError`。最后的 `object() is object()` 为 `False`，说明每个 sentinel 都是独一无二的。

**预期结果**：依次输出 `0`、`None`、`42`、`正确报错: no attribute MISSING`、`False`。

#### 4.3.5 小练习与答案

**练习 1**：把 `sentinel = object()` 写在模块顶层（函数外面）共享一个全局 sentinel，和现在写在函数内部每次新建，哪个更好？为什么？

> **答案**：写在函数内部、每次新建略好。虽然两种写法在「正确性」上等价（全局 sentinel 同样唯一），但写在函数内部能让 sentinel 的作用域最小、生命周期最短，避免它意外被外部代码引用；同时也保证「默认值」与单次 `__getattr__` 调用严格绑定，语义最清晰。numpy 选的就是函数内每次新建。

**练习 2**：能不能用 `sentinel = []`（一个空列表）代替 `object()`？

> **答案**：能用，但不推荐。空列表同样是一个全新对象，作为默认值也唯一，逻辑上正确。但 `object()` 更轻、更明确地表达「我只是要一个占位符」的意图，且不会被误以为是某种数据结构。社区约定俗成的哨兵写法就是 `sentinel = object()`，可读性更好。

---

### 4.4 AttributeError 分支：真缺失如何收尾

#### 4.4.1 概念说明

两种写法在「属性真的不存在」时，都走同一条收尾路径：**抛 `AttributeError`，并且不报警**。

这一点很重要：弃用警告（`DeprecationWarning`）只针对「废弃但仍然能用」的名字；如果一个名字从来就不存在，那它和「废弃」无关，应当像普通 Python 属性查找失败一样抛 `AttributeError`。`__getattr__` 抛出 `AttributeError` 也正是 PEP 562 规定的「告诉解释器：这个属性我真没有」的标准方式。

#### 4.4.2 核心流程

```text
if 拿到的是默认值:          # 属性真的不存在
    raise AttributeError(    # 直接抛错，不调 _raise_warning
        f"module 'numpy.core.<子模块>' has no attribute {name}")
# 只有走到这里（属性存在）才报警
_raise_warning(name, 子模块名)
return ret
```

关键顺序：**先判存在，再报警**。这就保证了「真缺失 → 只抛 AttributeError、不打扰用户」和「废弃但存在 → 报警 + 正常返回」两条路径互不干扰。

#### 4.4.3 源码精读

两种写法的 `AttributeError` 分支几乎逐字相同，只是模块名不同。

`numeric.py` 的报错分支（紧跟 sentinel 判断之后）：

[numeric.py:8-10](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py#L8-L10) —— 抛出 `AttributeError`，消息里写明 `numpy.core.numeric`。

`umath.py` 的报错分支：

[umath.py:6-8](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/umath.py#L6-L8) —— 结构相同，消息里写明 `numpy.core.umath`。

而「报警」发生在判断**之后**，调用的是统一的 `_raise_warning`：

[_utils.py:10-21](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L10-L21) —— 注意 `stacklevel=3`（第 20 行）：它把警告的归因跳过两层 numpy 内部帧，指向真正访问 `numpy.core.*` 的用户代码。`stacklevel` 的细节会在 [u2-l3](u2-l3-raise-warning.md) 专门讲，这里只要记住「`AttributeError` 分支不走这里、不报警」即可。

#### 4.4.4 代码实践

用一个最小实验验证「真缺失只抛 AttributeError、不报警；废弃但存在才报警」。如果你本地装了 numpy（>=2.0），可以直接跑；否则标注为「待本地验证」。

```python
import warnings, numpy.core.numeric as numeric

# 场景一：访问一个废弃但仍然存在的名字 → 应当报警并返回对象
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    obj = numeric.asarray              # asarray 真实存在于 numpy._core.numeric
print("返回的对象:", obj)
print("捕获到警告数:", len(w), "| 类别:", w[0].category.__name__ if w else "无")

# 场景二：访问一个根本不存在的名字 → 应当抛 AttributeError，且不报警
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    try:
        numeric.definitely_not_here
    except AttributeError as e:
        print("抛了:", e)
print("此时捕获到警告数:", len(w), "（预期 0）")
```

**需要观察的现象**：场景一返回对象的同时产生 1 条 `DeprecationWarning`；场景二抛 `AttributeError`，且**警告数为 0**。

**预期结果**：场景一打印「捕获到警告数: 1 | 类别: DeprecationWarning」；场景二打印抛错信息与「此时捕获到警告数: 0」。若本地 numpy 版本行为不同，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `_raise_warning(attr_name, "numeric")` 这一行挪到「判断缺失之前」，会发生什么变化？

> **答案**：那么即使属性真的不存在，也会先打印一条 `DeprecationWarning`，紧接着才抛 `AttributeError`。这会在用户拼错名字时产生误导性的弃用提示（明明是写错了，却被提示「该名字已废弃」）。所以 numpy「先判存在、再报警」的顺序是有意为之的。

**练习 2**：`AttributeError` 的消息里写的是 `numpy.core.numeric`（旧路径）而不是 `numpy._core.numeric`（新路径），这是 bug 吗？

> **答案**：不是。这个消息描述的是「`numpy.core.numeric` 这个旧模块没有这个属性」，是面向用户访问旧路径时的准确陈述；而 `_raise_warning` 里的消息才会引导用户改用 `numpy._core.*`。两者职责不同，消息文本自然不同。

---

## 5. 综合实践

把本讲三种知识（None 写法、sentinel 写法、AttributeError 分支）串起来，亲手做一次对照实验。

**实践目标**：构造一个「真模块」，分别用 None 写法和 sentinel 写法做两个垫片，证明 None 写法在「值为 `None` 的真属性」上翻车，而 sentinel 写法不会；并解释原因。

**操作步骤**：

1. 在一个空目录里建一个最小包 `deleg_demo/`：

   ```
   deleg_demo/
     _real.py          # 真模块（仿 numpy._core.*）
     none_shim.py      # 仿 umath.py
     sentinel_shim.py  # 仿 numeric.py
     run.py            # 测试脚本
   ```

2. `_real.py`（关键：放一个值为 `None` 的真属性）：

   ```python
   # 这是一个「真模块」的替身
   ZERO = 0          # falsy 但有效
   NONE = None       # 值就是 None —— None 写法的盲点
   REAL = 42
   ```

3. `none_shim.py`（逐行仿照 [umath.py:1-11](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/umath.py#L1-L11)）：

   ```python
   def __getattr__(attr_name):
       from . import _real
       ret = getattr(_real, attr_name, None)
       if ret is None:
           raise AttributeError(
               f"module 'none_shim' has no attribute {attr_name}")
       return ret
   ```

4. `sentinel_shim.py`（逐行仿照 [numeric.py:1-13](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py#L1-L13)）：

   ```python
   def __getattr__(attr_name):
       from . import _real
       sentinel = object()
       ret = getattr(_real, attr_name, sentinel)
       if ret is sentinel:
           raise AttributeError(
               f"module 'sentinel_shim' has no attribute {attr_name}")
       return ret
   ```

5. `run.py`：

   ```python
   from . import none_shim, sentinel_shim

   def probe(shim, name):
       try:
           val = getattr(shim, name)
           print(f"{shim.__name__}.{name} -> 返回 {val!r}")
       except AttributeError as e:
           print(f"{shim.__name__}.{name} -> 抛 AttributeError: {e}")

   for name in ("ZERO", "NONE", "REAL", "MISSING"):
       probe(none_shim, name)
       probe(sentinel_shim, name)
       print("-" * 40)
   ```

6. 运行：`python -m deleg_demo.run`

**需要观察的现象**：
- `ZERO`：两个垫片都返回 `0`（说明 `is None` 不会误判 `0`，纠正「falsy 即误判」的误解）。
- `NONE`：**None 垫片抛 `AttributeError`**（盲点暴露）；**sentinel 垫片返回 `None`**（正确）。
- `REAL`：两个垫片都返回 `42`。
- `MISSING`：两个垫片都抛 `AttributeError`（真缺失，行为一致）。

**预期结果**：唯一不同的那一行是 `NONE`——None 垫片报错、sentinel 垫片返回 `None`。

**解释（写在你的实验记录里）**：None 写法的 `getattr(_real, name, None)` 在 `name` 缺失和 `name` 存在但值为 `None` 两种情况下都返回 `None`，`if ret is None` 无法区分二者，于是把真实属性 `NONE` 误判为缺失。sentinel 写法的默认值是一个全局唯一的 `object()`，任何真实属性值（包括 `None`）都不会等于它，所以只有真缺失才会触发 `AttributeError`。结论：**当被转发模块可能出现值为 `None` 的公开名字时，应当像 `numeric.py` 那样用 sentinel 写法**；当能保证「公开名字永不为 `None`」时，None 写法更简洁、numpy 也大量使用。

## 6. 本讲小结

- 两种委派写法共享同一骨架：`getattr(真模块, name, 默认值)` + 「拿到默认值就报 `AttributeError`、否则报警并返回」。
- **None 写法**（`umath.py`、`records.py` 等 13 个垫片）用 `None` 作默认值、`if ret is None` 判断；它**无法区分「属性缺失」与「属性存在但值为 `None`」**，这是它的唯一盲点。
- 由于 numpy 用的是 `is None` 而非 `if not ret`，值为 `0`、`False`、`""` 的真属性**不会被误判**——只有值为 `None` 的属性才会。
- **sentinel 写法**（仅 `numeric.py`）用 `sentinel = object()` 作默认值；因为 sentinel 全程序唯一，它对所有取值都安全，包括 `None`。
- 「真缺失」走 `AttributeError` 分支、**不报警**；「废弃但存在」才调 `_raise_warning` 报警并正常返回——「先判存在、再报警」的顺序不可颠倒。
- 选型原则：若被转发模块可能出现值为 `None` 的公开名字，选 sentinel；若能保证公开名字永不为 `None`，None 写法更简洁。

## 7. 下一步学习建议

- 下一讲 [u2-l3 `_raise_warning`：统一弃用信息的生成与 stacklevel](u2-l3-raise-warning.md) 会专门拆解本讲里反复出现的 `_raise_warning`，重点讲它如何拼装弃用消息、如何用 `stacklevel=3` 把警告指向用户代码。
- 想从「单个垫片」拉高到「整个包入口」，接着读 [u2-l4 包入口 `__init__.py`](u2-l4-package-init.md)，看包级 `__all__` 与 `__getattr__` 如何协作。
- 延伸阅读：可直接对照 `numpy/core` 下任意一个 None 写法垫片（如 `fromnumeric.py`、`shape_base.py`）与 `numeric.py`，确认「13 个 None + 1 个 sentinel」的分布，加深对本讲选型判断的理解。
