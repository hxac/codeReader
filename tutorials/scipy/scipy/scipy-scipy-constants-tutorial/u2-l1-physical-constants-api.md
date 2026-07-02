# physical_constants 数据库与查找 API

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `physical_constants` 字典中每个条目的三元组结构 `(value, unit, uncertainty)`，并理解第三个元素是**不确定度（uncertainty）**而不是精度。
- 用 `value()` / `unit()` / `precision()` / `find()` 四个函数查询任意物理常数。
- 解释 `precision(key)` 为什么等于 `uncertainty / value`，以及精确常数为何精度为 `0.0`。
- 理解 `_check_obsolete` 如何对「不属于当前 CODATA 数据集」的常量发出 `ConstantWarning`。

本讲是进阶层的第一篇。在 u1-l4 里我们已经见过 `_cd(...)`（即 `_codata.value`）被用来派生 `atm`、`psi`、`light_year` 等换算因子。本讲我们就走进 `_codata.py`，看清这个「物理常数数据库」到底是什么、又该怎么查。

## 2. 前置知识

- **CODATA**：国际科学技术数据委员会（Committee on Data for Science and Technology）每隔几年发布一组「基本物理常数推荐值」，是世界范围内公认的权威数据。`scipy.constants` 当前使用的是 **CODATA 2022** 推荐值（u1-l1 已提到参考文献 `[CODATA2022]`）。
- **不确定度（uncertainty）**：测量值不是「绝对精确」的，总带有一个误差范围。CODATA 给每个常量同时列出「推荐值」和「不确定度」。例如某常量写成 `1.380 649 e-23 ± 0.000 000 079 e-23`，前者是值，后者是不确定度。
- **精确常数（exact）**：自 2019 年 SI 单位制重新定义后，光速 `c`、普朗克常数 `h`、基本电荷 `e`、玻尔兹曼常数 `k`、阿伏伽德罗常数 \(N_A\) 等「定义常数」是**精确定义**的，不确定度记为 `0`。
- **相对精度（relative precision）**：用「不确定度 ÷ 值」衡量一个常量「准不准」。值越小说明测得越精确。这就是 `precision()` 的定义。

> 一个容易踩坑的点：`_codata.py` 顶部模块文档把字典值写成 `(value, units, precision)`（见下方源码），但**第三个元素实际存的是不确定度**，真正的「精度」要由 `precision()` 函数做一次除法得到。这个文档措辞会在 4.1 节专门澄清。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件：

| 文件 | 作用 |
| --- | --- |
| `scipy/constants/_codata.py` | CODATA 物理常数数据库的全部实现：原始定宽文本、解析函数、`physical_constants` 字典、四个查找函数、`ConstantWarning` |

测试文件作为「行为契约」会引用到：

| 文件 | 作用 |
| --- | --- |
| `scipy/constants/tests/test_codata.py` | 用断言锁定 `find` / `value` / `precision` / 精确值 / 别名兼容等行为，是理解正确行为的最好参照 |

永久链接的 base 是：
`https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/`

---

## 4. 核心概念与源码讲解

### 4.1 physical_constants 数据库：三元组结构与组装

#### 4.1.1 概念说明

`physical_constants` 是一个普通的 Python 字典，但它扮演着「物理常数数据库」的角色：

- **键**：常量的英文全名，例如 `'Boltzmann constant'`、`'speed of light in vacuum'`。
- **值**：一个三元组 `(value, unit, uncertainty)`。

也就是说，访问一个常量会一次性拿到「数值、单位、不确定度」三件套。模块文档把这三件套描述如下（注意它把第三项写成 "precision"，我们稍后会看到这是不精确的措辞）：

[_codata.py:10-12 — physical_constants 的模块级说明](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L10-L12)　这段文档告诉读者：字典的键是常量名，值是 `(value, units, precision)` 三元组。

> ⚠️ 澄清：这里的 "precision" 是文档的**口语化说法**，第三个元素在源码里实际是**不确定度（uncertainty）**。下文 4.2 节会看到，真正的「相对精度」由 `precision()` 函数用 `uncertainty / value` 算出来。

#### 4.1.2 核心流程

字典不是手写的，而是把六个历史版本（2002 / 2006 / 2010 / 2014 / 2018 / 2022）解析结果**逐个 `update` 合并**出来的：

```text
physical_constants = {}
for 版本 in [2002, 2006, 2010, 2014, 2018, 2022]:
    physical_constants.update(该版本解析出的字典)
```

因为 `dict.update()` 对同名键会用新值覆盖旧值，所以**越晚的版本优先级越高**，最终保留的是 CODATA 2022 的值；但那些只在旧版本里出现过、2022 里已被改名或删除的常量，仍然以「旧名字」留在字典里——这正是后面「废弃常量」的来源。

合并完成后，再用 2022 的解析结果作为「当前数据集」基准：

```text
_current_constants  = _physical_constants_2022   # 当前基准
_obsolete_constants = {在合并字典里、但不在 2022 里的键}  # 废弃名单
```

精确常数的 `uncertainty` 为什么是 `0.0`？因为解析定宽文本时，遇到原始数据里的 `(exact)` 标记，解析函数会把它替换成 `'0'` 再转 float：

[_codata.py:2013-2015 — 解析时把 (exact) 当作不确定度 0](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2013-L2015)　这一行从文本里切出不确定度列，把 `(exact)` 替换成 `0`，所以精确常数第三元是 `0.0`；下一行把三元组写进字典。

#### 4.1.3 源码精读

字典的组装过程在文件末尾：

[_codata.py:2063-2071 — 合并六版本并设定当前基准](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2063-L2071)　先建空字典，依次 `update` 六个版本（后覆盖前），再把 2022 设为 `_current_constants`、当前数据集名设为 `"CODATA 2022"`。

接着用「合并字典 ∖ 当前字典」算出废弃名单：

[_codata.py:2073-2077 — 计算废弃常量名单](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2073-L2077)　凡是在合并字典里、却不在 2022 当前数据集里的键，都标记为 obsolete。这个名单就是 4.3 节 `ConstantWarning` 的判据。

别名（alias）会被插回字典，让旧名字继续可用：

[_codata.py:2110-2115 — 把别名插回字典](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2110-L2115)　若别名指向的常量在当前数据集中，就把别名键也加进 `physical_constants`，指向同一个三元组；否则丢弃这个别名。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到三元组结构，并验证精确常数的第三元是 `0.0`。
2. **操作步骤**：运行下面的 Python 代码。

   ```python
   from scipy.constants import physical_constants

   # 玻尔兹曼常数（2019 SI 重定义后是精确常数）
   k = physical_constants['Boltzmann constant']
   print(k)            # 预期: (1.380649e-23, 'J K^-1', 0.0)

   # 一个非精确常数：经典电子半径
   re = physical_constants['classical electron radius']
   print(re)           # 预期: (2.8179403262e-15, 'm', 1.3e-24)
   ```
3. **需要观察的现象**：`k` 的第三个元素是 `0.0`（精确），而 `re` 的第三个元素是一个非零小数（有不确定度）。
4. **预期结果**：`Boltzmann constant` 的三元组第三元为 `0.0`；`classical electron radius` 的第三元为 `1.3e-24`（与 `find` 的文档示例一致，待本地验证精确数值）。

#### 4.1.5 小练习与答案

**练习 1**：`physical_constants['speed of light in vacuum']` 的三元组第三个元素是多少？为什么？

> **答案**：`0.0`。因为光速 `c` 是 SI 2019 重定义中的**定义常数**，精确定义为 `299792458 m/s`，不确定度为 0，解析时 `(exact)` 被替换成 `0`。

**练习 2**：如果 2002 和 2022 都有同名常量 `'X'`，合并后 `physical_constants['X']` 取哪个版本？

> **答案**：取 **2022** 版本。因为 `update` 顺序是 2002→2022，后执行的 `update` 覆盖前面的同名键。

---

### 4.2 四个查找函数：value / unit / precision / find

#### 4.2.1 概念说明

虽然可以直接用 `physical_constants[name]` 拿到整个三元组，但 `_codata.py` 还提供了四个更顺手的函数，让调用者不必去记「下标 0 是值、1 是单位、2 是不确定度」：

| 函数 | 返回 | 等价表达式 |
| --- | --- | --- |
| `value(key)` | 数值 | `physical_constants[key][0]` |
| `unit(key)` | 单位字符串 | `physical_constants[key][1]` |
| `precision(key)` | 相对精度 | `physical_constants[key][2] / physical_constants[key][0]` |
| `find(sub=None, disp=False)` | 匹配的键列表 | 在当前数据集中按子串（大小写不敏感）过滤 |

其中最值得记住的数学关系是 `precision`：

\[
  \text{precision}(k) = \frac{\text{uncertainty}(k)}{\text{value}(k)} = \frac{\text{physical\_constants}[k][2]}{\text{physical\_constants}[k][0]}
\]

这是一个**无量纲**的相对量，直观含义是「这个值的不确定度占它本身的几分之几」。对精确常数，分子为 `0`，故 `precision` 也是 `0`。

#### 4.2.2 核心流程

四个函数的分工可以这样记：

```text
value(key)     →  取下标 [0]
unit(key)      →  取下标 [1]
precision(key) →  取 [2] / [0]      ← 唯一需要做除法的
find(sub)      →  在 _current_constants（2022）里按子串过滤，排序后返回
```

两个关键设计：

1. **`value` / `unit` / `precision` 在取值前都会先调用 `_check_obsolete(key)`**，于是访问废弃常量会先弹警告再返回旧值（见 4.3 节）。
2. **`find` 只在「当前数据集」`_current_constants` 里搜**，而**不是**在整个 `physical_constants` 里搜。这意味着已被废弃的旧名字**不会**出现在 `find` 的结果里——废弃常量虽然还能通过 `physical_constants[name]` 取到，但 `find` 不会再帮你找到它们。匹配是大小写不敏感的（`sub.lower() in key.lower()`）。

`find` 的第二个参数 `disp`：默认 `False` 返回列表；设为 `True` 则把结果逐行**打印**到屏幕并返回 `None`。

#### 4.2.3 源码精读

三个取值函数都极短，且模式一致——先查废弃，再取下标：

[_codata.py:2129-2152 — value 取数值](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2129-L2152)　`_check_obsolete(key)` 之后返回 `physical_constants[key][0]`。函数上的 `@xp_capabilities(out_of_scope=True)` 表示它**不参与** Array API（返回纯 float），这部分会在 u3-l2 详讲。

[_codata.py:2155-2178 — unit 取单位字符串](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2155-L2178)　同样的守卫，返回下标 `[1]`。

[_codata.py:2181-2204 — precision = uncertainty / value](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2181-L2204)　核心就是 `return physical_constants[key][2] / physical_constants[key][0]`——不确定度除以值。对精确常数返回 `0.0`。

`find` 的实现重点是「搜索范围」和「大小写不敏感」：

[_codata.py:2256-2268 — find 的过滤与输出逻辑](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2256-L2268)　`sub is None` 时返回当前数据集全部键；否则用 `sub.lower() in key.lower()` 过滤；结果排序；`disp` 为真则打印并隐式返回 `None`。注意它遍历的是 `_current_constants`，不是 `physical_constants`。

#### 4.2.4 代码实践

1. **实践目标**：用 `find('Boltzmann')` 列出全部相关键，再分别取 `'Boltzmann constant'` 的值、单位、相对精度，并**手算**验证 `precision` 的公式。
2. **操作步骤**：

   ```python
   import warnings
   from scipy.constants import find, value, unit, precision, physical_constants

   # 1) 列出所有含 'Boltzmann' 的键（注意大小写不敏感）
   keys = find('Boltzmann')
   print(keys)
   # 预期包含: 'Boltzmann constant', 'Boltzmann constant in Hz/K',
   #           'Boltzmann constant in eV/K', 'Stefan-Boltzmann constant' 等

   # 2) 取 'Boltzmann constant' 的值、单位、相对精度
   name = 'Boltzmann constant'
   print('value     =', value(name))   # 预期 1.380649e-23
   print('unit      =', unit(name))    # 预期 'J K^-1'
   print('precision =', precision(name))  # 预期 0.0（精确常数）

   # 3) 手算 precision = uncertainty / value
   v, u, uncert = physical_constants[name]
   hand = uncert / v
   print('hand calc =', hand)          # 预期 0.0，应与 precision(name) 完全相等

   # 4) 换一个非精确常数再验一次
   name2 = 'classical electron radius'
   print('precision =', precision(name2))
   v2, u2, uncert2 = physical_constants[name2]
   print('hand calc =', uncert2 / v2)  # 应与上一行相等，且非零
   ```
3. **需要观察的现象**：
   - `find('Boltzmann')` 的结果里**没有** `'Boltzmann constant in inverse meters per kelvin'`（带 s 的旧拼写），因为它已是废弃常量、不在当前数据集。
   - `'Boltzmann constant'` 的 `precision` 与手算结果都是 `0.0`。
   - `'classical electron radius'` 的 `precision` 与 `uncertainty/value` 手算结果相等且非零。
4. **预期结果**：`precision(name)` 恒等于 `physical_constants[name][2] / physical_constants[name][0]`；精确常数的 `precision` 为 `0.0`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `find('Boltzmann')` 能匹配到 `'Stefan-Boltzmann constant'`，尽管首字母大小写不同？

> **答案**：因为 `find` 用 `sub.lower() in key.lower()` 做匹配，把键和子串都转成小写再比较，所以是大小写不敏感的。

**练习 2**：`find(disp=True)` 和 `find(disp=False)` 的返回值有什么区别？

> **答案**：`disp=False`（默认）返回键的**列表**；`disp=True` 把键逐行**打印**到屏幕，并且函数**返回 `None`**。所以若想拿到列表去后续处理，不要开 `disp`。

---

### 4.3 ConstantWarning 与 _check_obsolete：废弃常量检测

#### 4.3.1 概念说明

CODATA 每个新版本都会改名或删掉一些常量。SciPy 的策略是**不删旧名**，保证老代码不会立刻崩，但要在你访问时**提醒一句**：这个常量已经不在当前推荐值里了。这个提醒就是 `ConstantWarning`。

- `ConstantWarning` 是 `DeprecationWarning` 的子类，语义上是「这个常量名已被废弃」。
- 触发条件由 `_check_obsolete(key)` 判断：**当 `key` 在废弃名单 `_obsolete_constants` 里、并且不是某个别名时**，才发警告。
- 别名（alias）是「合法的旧名」，例如 CODATA 2018/2022 把 `mag. constant` 改名为 `vacuum mag. permeability`，但通过别名机制，`mag. constant` 仍指向同一个三元组且**不**触发警告。

#### 4.3.2 核心流程

```text
访问 value/unit/precision(key)
        │
        ▼
_check_obsolete(key):
    if (key 在 _obsolete_constants) and (key 不在 _aliases):
        warnings.warn(..., ConstantWarning, stacklevel=3)
        │
        ▼
照常返回旧值（不会抛异常）
```

要点：

1. 警告**不中断**执行——旧值照常返回，只是多一条警告。
2. `stacklevel=3` 让警告指向「调用 `value(...)` 的那一行用户代码」，而不是 scipy 内部，方便定位。
3. `_obsolete_constants` 在 4.1 节已经由「合并字典 ∖ 当前字典」算好；`_aliases` 在 4.1 节末尾生成。
4. **别名豁免**：`key not in _aliases` 这一项保证合法旧名（别名）不会误报。

#### 4.3.3 源码精读

[_codata.py:2118-2120 — ConstantWarning 类定义](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2118-L2120)　它只是继承 `DeprecationWarning` 的空类，docstring 说明「访问已不在当前 CODATA 数据集中的常量」时使用。

[_codata.py:2123-2126 — _check_obsolete 判据](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2123-L2126)　核心条件 `key in _obsolete_constants and key not in _aliases`；满足时发出 `ConstantWarning`，提示该常量不在当前 `CODATA 2022` 数据集。

一个真实的废弃例子：旧拼写 `'Boltzmann constant in inverse meters per kelvin'`（带 s）存在于 2002–2014 的数据里，2018 起被改名为 `'Boltzmann constant in inverse meter per kelvin'`（无 s）。因此旧拼写留在合并字典中、却不在 2022 当前数据集里，也没有别名，访问它会触发 `ConstantWarning`。

#### 4.3.4 代码实践

1. **实践目标**：亲手触发一次 `ConstantWarning`，并验证合法别名不会触发。
2. **操作步骤**：

   ```python
   import warnings
   from scipy.constants import value, physical_constants
   import scipy.constants._codata as _cd

   # (a) 触发废弃警告：访问 2018 起被改名的旧拼写
   obsolete_name = 'Boltzmann constant in inverse meters per kelvin'
   print('是否在废弃名单:', obsolete_name in _cd._obsolete_constants)  # 预期 True

   with warnings.catch_warnings(record=True) as w:
       warnings.simplefilter('always')
       val = value(obsolete_name)         # 仍会返回旧值
       print('返回值:', val)
       print('捕获到警告:', any(issubclass(x.category, _cd.ConstantWarning) for x in w))

   # (b) 合法别名不触发：'electric constant' 是 'vacuum electric permittivity' 的别名
   alias_name = 'electric constant'
   print('是否在别名表:', alias_name in _cd._aliases)  # 预期 True
   with warnings.catch_warnings(record=True) as w:
       warnings.simplefilter('always')
       value(alias_name)
       print('别名触发的 ConstantWarning 数:', sum(1 for x in w if issubclass(x.category, _cd.ConstantWarning)))  # 预期 0
   ```
3. **需要观察的现象**：第 (a) 步能捕获到 `ConstantWarning` 且函数仍返回了一个数值；第 (b) 步别名 `electric constant` 不触发任何 `ConstantWarning`。
4. **预期结果**：废弃名触发警告、合法别名不触发；二者都能正常返回数值。具体返回值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：如果直接用 `physical_constants['Boltzmann constant in inverse meters per kelvin'][0]`（绕过 `value()`）去取值，还会触发 `ConstantWarning` 吗？为什么？

> **答案**：**不会**。因为 `_check_obsolete` 只在 `value()` / `unit()` / `precision()` 这三个函数里被调用，直接索引字典不会经过它。这也说明：想得到「废弃提醒」就必须走这三个函数，而不是直接翻字典。

**练习 2**：为什么 `'mag. constant'` 访问时不会报警，而 `'Boltzmann constant in inverse meters per kelvin'` 会？

> **答案**：前者被收录在 `_aliases` 中（指向 `'vacuum mag. permeability'`），`_check_obsolete` 里的 `key not in _aliases` 条件使其豁免；后者没有对应的别名，所以满足废弃条件并报警。

---

## 5. 综合实践

把本讲的知识串起来，写一个小脚本：**盘点 CODATA 2022 里所有「精确常数」并统计它们的相对精度**。

要求：

1. 用 `find()`（不带参数）拿到当前数据集全部键的列表。
2. 遍历每个键，用 `physical_constants[key]` 取三元组，把 `uncertainty == 0.0`（即 `[2] == 0.0`）的挑出来。
3. 对这些精确常数调用 `precision(key)`，验证结果都是 `0.0`。
4. 统计：精确常数有多少个？非精确的有多少个？精确常数占总数的比例是多少？

参考框架（请自行补全统计部分）：

```python
from scipy.constants import find, precision, physical_constants

keys = find()                       # 当前数据集全部键
exact_keys = [k for k in keys if physical_constants[k][2] == 0.0]

# TODO: 打印精确常数的个数、非精确常数的个数、精确占比
# TODO: 抽查 3 个精确常数，断言 precision(k) == 0.0
```

思考题（结合源码回答）：

- 为什么这里用 `find()` 拿键，而不是 `physical_constants.keys()`？（提示：回忆 4.2 节「find 只搜当前数据集」。）
- 如果改用 `physical_constants.keys()`，名单里会多出哪一类键？访问它们时会发生什么？

> 参考答案：用 `find()` 能保证只统计「当前 CODATA 2022 仍在使用的常量」；若改用 `physical_constants.keys()`，会多出那些已废弃的旧名字，且通过 `value()` 访问它们会触发 `ConstantWarning`。

---

## 6. 本讲小结

- `physical_constants` 是一个字典，值是三元组 `(value, unit, uncertainty)`；模块文档里的 "precision" 是口语化说法，第三个元素实际是**不确定度**。
- 字典由六个 CODATA 版本（2002→2022）依次 `update` 合并而成，**后覆盖前**，最终以 CODATA 2022 为准。
- `value()` / `unit()` / `precision()` 分别取 `[0]` / `[1]` / `[2] ÷ [0]`，三者都先调用 `_check_obsolete` 再返回。
- `precision(key) = uncertainty / value`，精确常数（`(exact)`）不确定度为 `0`，故精度为 `0.0`。
- `find()` 只在**当前数据集** `_current_constants` 里做大小写不敏感的子串匹配，废弃常量不会出现在结果中。
- 访问废弃常量会触发 `ConstantWarning`（`DeprecationWarning` 子类），但**不中断**返回旧值；合法别名因 `_aliases` 豁免而不报警。

## 7. 下一步学习建议

本讲只讲了「数据库长什么样、怎么查」，但还没回答两个更深层的问题：

1. **原始数据是怎么变成字典的？** 那些 `txt2002 … txt2022` 的定宽文本，到底是怎么被切成 `(value, unit, uncertainty)` 的？列宽为什么在 2018 版本之后从 55 变成 60？—— 下一讲 **u2-l2「CODATA 文本数据的固定列宽解析」** 会拆解 `parse_constants_2002to2014` 与 `parse_constants_2018toXXXX` 的列切片逻辑。
2. **精确常数的值是怎么「算」出来的？** 本讲看到精确常数不确定度为 0，但有些精确值在文本里被截断成 `...`，需要用 SI 定义常数重新计算后回填——这部分在 **u2-l3「精确值与派生常数的计算」** 中讲解 `exact2018` / `replace_exact`。

建议阅读顺序：u2-l2 → u2-l3 → u2-l4（多版本合并、别名与废弃的完整机制），本讲已经为它们铺垫了「字典结构」和「查找 API」这两块基础。
