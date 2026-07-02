# 多版本 CODATA 合并、别名与废弃常量

## 1. 本讲目标

本讲是进阶篇 CODATA 系列的收尾。前面 u2-l2 讲了「如何把一份 CODATA 文本解析成字典」，u2-l3 讲了「如何为精确常数补全全精度」。但 `scipy.constants` 实际上同时携带了 **六份** CODATA 数据（2002、2006、2010、2014、2018、2022），它们是如何变成读者看到的「一个」 `physical_constants` 字典的？为什么像 `'mag. constant'` 这种 2014 年的老名字今天还能用、却不会报警？而 `'magn. constant'` 这种 2002 年的名字又会触发 `ConstantWarning`？

学完本讲你应该能够：

- 说清六份 CODATA 版本是如何「后更新覆盖」地合并成单一 `physical_constants` 的；
- 理解 `_current_constants` / `_current_codata` 如何界定「当前推荐值」与「历史值」；
- 看懂 `_aliases` 的四种生成模式与 `_extra_alias_keys` 的「白名单救场」逻辑；
- 解释 `_obsolete_constants` 与 `ConstantWarning` 的触发条件，知道别名为何能豁免告警。

## 2. 前置知识

阅读本讲前，请确认你已掌握（对应 u2-l1 / u2-l2 / u2-l3）：

- `physical_constants` 是一个字典，值是三元组 `(value, unit, uncertainty)`；`value()` / `unit()` / `precision()` / `find()` 四个函数访问它。
- CODATA 是国际科技数据委员会发布的基础物理常数推荐值，每隔几年更新一次；`scipy.constants` 当前以 **CODATA 2022** 为准。
- 六份原始文本 `txt2002` … `txt2022` 被 `parse_constants_2002to2014` / `parse_constants_2018toXXXX` 解析；`replace_exact` 用 `exact_func` 算出的全精度值回填被截断的精确常数。
- 2019 年 SI 重新定义后，`c`、`h`、`e`、`k`、`N_A` 成为精确定义常数（见 [`_codata.py:1535-1541`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L1535-L1541)），而旧版里「精确」的 `mag. constant`（μ₀）、`electric constant`（ε₀）反而变成了**测量值**并被改名。

本讲要解决的工程问题是：**当常数既会「版本演进」又会「改名/缩写」时，如何既保持向后兼容，又及时提醒用户别再用过时数据。**

## 3. 本讲源码地图

本讲几乎全部围绕一个文件：

| 文件 | 作用 |
| --- | --- |
| `scipy/constants/_codata.py` | CODATA 物理常数数据库的全部实现：文本数据、解析器、精确值推导、版本合并、别名、废弃检测、查找 API |

本讲聚焦该文件 **末尾约 200 行的「装配区」**（L2056–L2126），它把前面解析出的六个版本字典「拼装」成最终对外暴露的 `physical_constants`，并建立别名与废弃标记。涉及的几个内部对象关系如下：

```
txt2002…txt2022
   │  parse_constants_*(..., exact_func)
   ▼
_physical_constants_2002 … _physical_constants_2022   （六个版本字典）
   │  physical_constants.update(...)  逐版本覆盖
   ▼
physical_constants                                      （对外单一字典）
   │  以 _physical_constants_2022 为「当前」
   ├──► _current_constants / _current_codata
   ├──► _obsolete_constants = 不在当前版本里的键
   └──► _aliases  ──►  把别名键也写入 physical_constants
```

## 4. 核心概念与源码讲解

### 4.1 多版本合并：从六份 CODATA 到一个 physical_constants

#### 4.1.1 概念说明

`scipy.constants` 不只携带最新推荐值，还保留了过去六份 CODATA 数据集。这样做有两个好处：一是让旧代码里写死的常量名仍能查到值；二是支持历史可复现。但对外只暴露**一个** `physical_constants` 字典，因此需要把六份字典合并。

合并策略非常朴素：**后更新的覆盖先更新的**。`dict.update()` 天然就是这个语义——遇到同名键，后者的值胜出。因为代码按时间顺序 2002→2006→2010→2014→2018→2022 依次 `update`，最终每个键都持有**它最后一次出现版本**的值。

#### 4.1.2 核心流程

1. 对每份 `txtYYYY` 文本，调用对应的解析器得到 `_physical_constants_YYYY` 字典（2002–2014 用旧列宽解析器，2018/2022 用新列宽解析器）。
2. 新建空字典 `physical_constants`。
3. 按年份从小到大依次 `physical_constants.update(_physical_constants_YYYY)`。
4. 同名键被逐年刷新，最终值为最新版本的取值；仅旧版本才有的键则被保留下来（这正是「废弃常量」的来源）。

> 注意：被合并的「值」是元组 `(value, unit, uncertainty)`。`update` 是**引用赋值**，同名键直接指向新版本的元组对象。

#### 4.1.3 源码精读

先看六份版本字典是如何生成的（解析器与 `exact_func` 的细节见 u2-l2 / u2-l3）：

[_codata.py:2056-2061](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2056-L2061) —— 把六份文本分别解析成六个私有字典 `_physical_constants_2002` … `_physical_constants_2022`。

再看合并本体，仅 7 行：

[_codata.py:2063-2069](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2063-L2069) —— 新建空字典后按年份顺序 `update`，后者覆盖前者，保证最终取最新推荐值。关键代码：

```python
physical_constants: dict[str, tuple[float, str, float]] = {}
physical_constants.update(_physical_constants_2002)
physical_constants.update(_physical_constants_2006)
physical_constants.update(_physical_constants_2010)
physical_constants.update(_physical_constants_2014)
physical_constants.update(_physical_constants_2018)
physical_constants.update(_physical_constants_2022)
```

举一个被覆盖的例子：`'speed of light in vacuum'` 每个版本都有，但值都是精确的 299792458，所以覆盖与否结果不变；而像 `'alpha particle mass'` 这样随测量精度提升而值微调的常数，最终持有 2022 版的值。反过来，`'magn. constant'`（μ₀，仅 2002 版用此名）在后续版本里改了名，不会再被覆盖，于是它的 2002 精确值（`4e-7*pi`，见 [exact2002: L144-148](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L144-L148)）被原样保留——这也埋下了「废弃常量」的种子。

#### 4.1.4 代码实践

**实践目标**：验证「后更新覆盖」语义。

**操作步骤**（源码阅读 + 思考）：

1. 在 [_codata.py:108](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L108) 看到 2002 文本里 `'magn. constant'` 的原始值为 `12.566 370 614...e-7`（带 `...` 截断），不确定度标 `(exact)`。
2. 在 [exact2002 (L144-148)](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L144-L148) 看到 `'magn. constant'` 被回填为 `4e-7 * math.pi`（即 μ₀ = 4π×10⁻⁷ N/A²，SI 旧定义）。
3. 因为 2018/2022 把这个常数改名为 `'vacuum mag. permeability'` 并改为测量值，`'magn. constant'` 这个键在合并后**不会被后续版本覆盖**，于是 `physical_constants['magn. constant']` 仍然保留 2002 的精确旧值。

**需要观察的现象**：`physical_constants['magn. constant']` 与 `physical_constants['vacuum mag. permeability']` 是两个**不同的键、不同的值**——前者是已被 SI 2019 废弃的「精确 μ₀」，后者是新的「测量 μ₀」。

**预期结果**：`'magn. constant'` 的元组第三项（不确定度）为 `0.0`（精确），而 `'vacuum mag. permeability'` 的第三项非零（测量值）。具体数值**待本地验证**（可执行 `python -c "import scipy.constants as sc; print(sc.physical_constants['magn. constant']); print(sc.physical_constants['vacuum mag. permeability'])"`，注意访问 `'magn. constant'` 会触发 `ConstantWarning`，见 4.4）。

#### 4.1.5 小练习与答案

**练习 1**：如果未来 SciPy 新增 CODATA 2026，需要在这段合并代码里做什么修改？
**答案**：新增 `_physical_constants_2026 = parse_constants_2018toXXXX(txt2026, exact2026)`，并在末尾追加一行 `physical_constants.update(_physical_constants_2026)`；同时把 4.2 节的 `_current_constants` 指向它。顺序必须最后，否则会被旧版本覆盖。

**练习 2**：为什么不直接用 `_physical_constants_2022` 作为对外字典，而要合并六份？
**答案**：为了让历史上存在、但最新版已删除或改名的常量名仍可查询，保持向后兼容；同时 `_current_constants`（见 4.2）单独指向 2022，用于 `find()` 和废弃检测，从而在「兼容旧名」与「推荐新值」之间取得平衡。

---

### 4.2 当前数据集：_current_constants 与 _current_codata

#### 4.2.1 概念说明

合并后，`physical_constants` 里既有最新值，也有历史值。为了区分「当前推荐」与「历史遗留」，代码用两个模块级变量锁定**当前数据集**：

- `_current_constants`：指向当前版本的字典（现在是 `_physical_constants_2022`）。
- `_current_codata`：一个人类可读字符串，标注当前数据集名称（现在是 `"CODATA 2022"`），用于告警文案。

这两个变量是后续判断「常量是否过时」「`find()` 该搜哪些键」的唯一基准。

#### 4.2.2 核心流程

- `_current_constants` 直接**引用** `_physical_constants_2022`（不是副本），二者指向同一个字典对象。
- `find(sub)` 只在 `_current_constants` 里做大小写不敏感的子串匹配——因此废弃/历史常量名**不会出现在 `find()` 结果里**（见 u2-l1）。
- `_obsolete_constants`（4.4 节）通过「键在 `physical_constants` 但不在 `_current_constants`」来判定废弃。
- `_current_codata` 仅用于拼装告警信息，告诉用户「这个常量不在当前 CODATA 2022 数据集里」。

#### 4.2.3 源码精读

[_codata.py:2070-2071](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2070-L2071) —— 定义当前数据集及其名称：

```python
_current_constants = _physical_constants_2022
_current_codata = "CODATA 2022"
```

`find()` 如何使用 `_current_constants`：

[_codata.py:2256-2260](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2256-L2260) —— 不传 `sub` 时返回当前数据集全部键；传了则做 `.lower()` 子串匹配，搜索范围始终是 `_current_constants` 而非整个 `physical_constants`。

`_current_codata` 出现在告警文案中：

[_codata.py:2125-2126](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2125-L2126) —— 告警字符串里嵌入 `{_current_codata}`，让用户清楚看到自己用的是哪一代数据集。

#### 4.2.4 代码实践

**实践目标**：体会 `find()` 只看「当前」数据集。

**操作步骤**：

1. 执行 `find('magn.')`（注意带点），观察返回结果。
2. 再执行 `list(physical_constants.keys())` 里手工搜索包含 `'magn.'` 的键。

**需要观察的现象**：`find('magn.')` 很可能返回空列表（因为 2022 当前数据集用的是 `'mag.'`/`'vacuum mag.'` 拼写，没有 `'magn.'`），但 `physical_constants` 字典里却**存在**以 `'magn.'` 开头的历史键（来自 2002 版）。

**预期结果**：`find()` 结果与 `physical_constants` 全量键不一致——这正是 `_current_constants` 过滤的效果。具体返回值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`_current_constants = _physical_constants_2022` 之后，如果有人修改了 `_physical_constants_2022` 里某个键的值，`_current_constants` 会跟着变吗？
**答案**：会。二者是同一个字典对象的两个名字（引用赋值，非拷贝），修改一处另一处可见。在本文件里它们都是模块内部、合并完成后不再修改的，所以实际不会出问题。

**练习 2**：为什么 `find()` 不直接搜 `physical_constants`？
**答案**：`physical_constants` 含历史/废弃/别名键，直接搜会把过时名字也推荐给用户；限定 `_current_constants` 能保证 `find()` 只返回当前推荐使用的常量名。

---

### 4.3 别名机制：_aliases 生成、显式重命名与 _extra_alias_keys

#### 4.3.1 概念说明

CODATA 在不同年份对同一个物理量用过**不同的名字或缩写**。例如：

- `magn.` 与 `mag.`：2002 版用 `magn.`（magnetic 的旧缩写），后续版本改用 `mag.`。
- `momentum` 与 `mom.um`：2006/2010 版 NIST 把 momentum 缩写成 `mom.um`，其它版本用全称 `momentum`（可在文本里直接验证，如 [_codata.py:712](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L712) 是 `natural unit of mom.um`，而 [_codata.py:1856](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L1856) 是 `natural unit of momentum`）。
- `mag. constant` → `vacuum mag. permeability`、`electric constant` → `vacuum electric permittivity`：2018 年 SI 重定义后，原本「精确」的 μ₀、ε₀ 变成测量值并被改名。

如果只做合并，用户用旧名 `value('mag. constant')` 会查不到（2022 里没有这个键）。`_aliases` 就是为了让**新旧两种写法都能查到同一个值**。

#### 4.3.2 核心流程

别名机制分三步：

1. **按规则批量生成**：遍历特定版本的键，对含 `magn.`/`momentum` 的键用字符串替换生成别名映射 `_aliases[旧名] = 新名`。
2. **显式补两条重命名**：手动写入 `mag. constant`/`electric constant` 到新名的映射。
3. **存活过滤 + 写入**：遍历 `_aliases`，只有当「目标名 `v` 确实是当前可查的键」（在 `_current_constants` 或 `_extra_alias_keys` 白名单里）时，才把 `physical_constants[旧名] = physical_constants[目标名]`；否则删除该别名。

这里有个微妙点：别名目标必须**真实存在于 `physical_constants`**，否则 `physical_constants[v]` 会 `KeyError`。`_extra_alias_keys` 的作用就是给那些「不在 2022 当前集、但其 `mom.um` 拼写作为真实键存在于旧版合并结果里」的目标发放通行证，让它们通过存活检查。

#### 4.3.3 源码精读

**第一步：按规则生成** ——

[_codata.py:2080-2092](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2080-L2092) —— 对 2002 的 `magn.` 键生成 `mag.` 别名；对 2006/2018/2022 中含 `momentum` 的键生成 `mom.um` 别名：

```python
_aliases = {}
for k in _physical_constants_2002:
    if 'magn.' in k:
        _aliases[k] = k.replace('magn.', 'mag.')
for k in _physical_constants_2006:
    if 'momentum' in k:
        _aliases[k] = k.replace('momentum', 'mom.um')
for k in _physical_constants_2018:
    if 'momentum' in k:
        _aliases[k] = k.replace('momentum', 'mom.um')
for k in _physical_constants_2022:
    if 'momentum' in k:
        _aliases[k] = k.replace('momentum', 'mom.um')
```

> 说明：2006 循环看似冗余（2006 键本身已是 `mom.um`，不含 `momentum`），它是防御性写法；真正生效的是 2018/2022 循环——当前数据集用全称 `momentum`，故为它们生成 `mom.um` 别名以兼容旧式拼写。

**第二步：显式补两条重命名** ——

[_codata.py:2094-2096](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2094-L2096) —— 把 2018 改名后的两个老键映射到新名：

```python
# CODATA 2018 and 2022: renamed and no longer exact; use as aliases
_aliases['mag. constant'] = 'vacuum mag. permeability'
_aliases['electric constant'] = 'vacuum electric permittivity'
```

这两条是本讲实践任务的核心：它们让 `value('mag. constant')` 与 `value('vacuum mag. permeability')` 返回完全相同的值（同一元组对象）。

**第三步：`_extra_alias_keys` 白名单** ——

[_codata.py:2099-2108](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2099-L2108) —— 列出十个 `natural unit of ...` 的 `mom.um` 拼写目标，作为存活检查的额外白名单。

**第四步：存活过滤并写入** ——

[_codata.py:2110-2115](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2110-L2115) —— 只有目标可达的别名才被写入 `physical_constants`，其余删除：

```python
for k, v in list(_aliases.items()):
    if v in _current_constants or v in _extra_alias_keys:
        physical_constants[k] = physical_constants[v]
    else:
        del _aliases[k]
```

注意这里的「双面性」：

- `'mag. constant'` → `'vacuum mag. permeability'`：目标在 `_current_constants`，存活，且 `'mag. constant'` 进了 `_aliases`，于是 4.4 节的 `_check_obsolete` 会**豁免**它，不报警。
- `'magn. constant'` → `'mag. constant'`：目标 `'mag. constant'` 既不在 `_current_constants`（2022 没有），也不在 `_extra_alias_keys`，于是这条别名被 `del` 删除。结果 `'magn. constant'` 不在 `_aliases` 里，访问它会触发废弃告警。

回归测试 [_codata.py 测试 test_gh11341 (tests/test_codata.py:62-68)](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/tests/test_codata.py#L62-L68) 锁定了「`epsilon_0` == `electric constant` == `vacuum electric permittivity`」三者相等，正是别名机制的体现。

#### 4.3.4 代码实践

**实践目标**：验证 `'mag. constant'` 经别名指向 `'vacuum mag. permeability'`，二者值相等且**不报警**。

**操作步骤**：

1. 执行以下示例代码（**示例代码**，非项目原有）：

   ```python
   import scipy.constants as sc
   a = sc.value('mag. constant')
   b = sc.value('vacuum mag. permeability')
   print(a, b, a == b)
   print(sc.physical_constants['mag. constant'] is sc.physical_constants['vacuum mag. permeability'])
   ```

**需要观察的现象**：

- `a == b` 为 `True`，因为别名把两个键指向同一个三元组。
- `is` 比较为 `True`，说明二者引用的是**同一个元组对象**（`update`/赋值是引用拷贝）。
- 全程**没有** `ConstantWarning`（因为 `'mag. constant'` 在 `_aliases` 中，被 `_check_obsolete` 豁免，见 4.4）。

**预期结果**：两个值都是 μ₀ 的 2022 测量推荐值（约 `1.25663706212e-6` N/A²，具体尾数**待本地验证**）。作为对照，`'magn. constant'` 会给出 2002 的旧精确值 `4e-7*pi ≈ 1.2566370614e-6` 并伴随告警。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `'magn. constant'` 这条别名会被删除，而 `'mag. constant'` 不会？
**答案**：存活条件是「目标名在 `_current_constants` 或 `_extra_alias_keys`」。`'mag. constant'` 的目标 `'vacuum mag. permeability'` 在当前集，存活；`'magn. constant'` 的目标 `'mag. constant'` 不在当前集、也不在白名单，故被删除。

**练习 2**：`_extra_alias_keys` 里为什么要列 `'natural unit of mom.um'` 这种怪拼写？
**答案**：因为 2018/2022 当前集用的是全称 `momentum`，由规则生成的别名目标变成了 `mom.um` 拼写，这些目标不在 `_current_constants`；但它们作为真实键存在于 2006/2010 合并进来的 `physical_constants` 里。白名单让这些目标通过存活检查，使 `momentum` 与 `mom.um` 两种写法都能查到值。

---

### 4.4 废弃常量检测：_obsolete_constants 与 ConstantWarning

#### 4.4.1 概念说明

合并保留了历史常量，但 SciPy 希望在用户访问「已不在当前 CODATA 推荐集」的常量时给出**温和提醒**——不是抛异常中断程序，而是发一个 `ConstantWarning`（`DeprecationWarning` 的子类），同时**照常返回旧值**。这样既不破坏旧代码，又能推动用户迁移到新名。

判定废弃的依据很简单：一个键存在于合并后的 `physical_constants`，却**不在**当前数据集 `_current_constants`（= 2022）里。但有一个重要豁免：**合法别名不计为废弃**（因为别名是「有意保留的兼容名」，不是过时数据）。

#### 4.4.2 核心流程

1. 遍历 `physical_constants` 的所有键，凡不在 `_current_constants` 者，标记进 `_obsolete_constants`（值为 `True` 的占位字典）。
2. 定义 `ConstantWarning(DeprecationWarning)`。
3. `_check_obsolete(key)`：若 `key` 在 `_obsolete_constants` **并且** `key` 不在 `_aliases`，则发 `ConstantWarning`；否则静默。
4. `value()` / `unit()` / `precision()` 在取值前各自调用 `_check_obsolete(key)`，告警后仍正常返回。

判断逻辑可用下表概括：

| 键的状态 | 在 `_obsolete_constants`? | 在 `_aliases`? | 访问 `value(key)` 的结果 |
| --- | --- | --- | --- |
| 当前推荐常量（如 `vacuum mag. permeability`） | 否 | — | 正常返回，无告警 |
| 合法别名（如 `mag. constant`） | 是 | 是 | 正常返回，**无告警**（被豁免） |
| 真正废弃常量（如 `magn. constant`） | 是 | 否 | 发 `ConstantWarning`，仍返回旧值 |

#### 4.4.3 源码精读

**废弃集合的构建**（注意它在别名写入 `physical_constants` **之前**计算，所以别名键不会污染废弃集）——

[_codata.py:2073-2077](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2073-L2077) —— 凡合并后存在、却不在 2022 当前集的键，都标记为废弃：

```python
# check obsolete values
_obsolete_constants = {}
for k in physical_constants:
    if k not in _current_constants:
        _obsolete_constants[k] = True
```

**告警类**——

[_codata.py:2118-2120](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2118-L2120) —— `ConstantWarning` 继承自 `DeprecationWarning`，docstring 点明语义「Accessing a constant no longer in current CODATA data set」。

**检测函数**——

[_codata.py:2123-2126](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2123-L2126) —— 双条件判定，别名可豁免：

```python
def _check_obsolete(key: str) -> None:
    if key in _obsolete_constants and key not in _aliases:
        warnings.warn(f"Constant '{key}' is not in current {_current_codata} data set",
                      ConstantWarning, stacklevel=3)
```

`stacklevel=3` 让告警指向**用户调用** `value()` 的那一行，而不是 `_codata.py` 内部，便于定位。

**取值函数的接入点**——

[_codata.py:2130-2152](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2130-L2152) —— `value()` 先 `_check_obsolete(key)` 再 `return physical_constants[key][0]`，告警不阻断返回。`unit()` ([L2156-2178](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2156-L2178)) 与 `precision()` ([L2182-2204](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2182-L2204)) 同理。

#### 4.4.4 代码实践

**实践目标**：找到一个废弃常量，捕获 `ConstantWarning`；并对照说明为何 `'mag. constant'` 不报警。

**操作步骤**（**示例代码**）：

```python
import warnings
import scipy.constants as sc
from scipy.constants import ConstantWarning

# 1) 访问真正废弃的 2002 名 'magn. constant'，应触发 ConstantWarning
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    v = sc.value('magn. constant')
    print("value =", v)
    print("warning raised?", any(issubclass(x.category, ConstantWarning) for x in w))

# 2) 对照：访问别名 'mag. constant'，不应触发 ConstantWarning
with warnings.catch_warnings(record=True) as w2:
    warnings.simplefilter("always")
    sc.value('mag. constant')
    print("alias warning raised?", any(issubclass(x.category, ConstantWarning) for x in w2))
```

**需要观察的现象**：

- 第 1 段：`value` 仍返回了一个数（2002 的精确 μ₀ ≈ `1.2566370614e-6`），并且 `warning raised?` 为 `True`，告警文案形如 `Constant 'magn. constant' is not in current CODATA 2022 data set`。
- 第 2 段：`alias warning raised?` 为 `False`。

**预期结果**：`'magn. constant'` 触发告警但返回值；`'mag. constant'`（别名指向 `'vacuum mag. permeability'`）静默返回。完整数值**待本地验证**。

**原理解释（承接实践任务第二问）**：`'mag. constant'` 之所以仍可用且不报警，是因为 [L2095](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2095) 把它登记为别名、目标 `'vacuum mag. permeability'` 又在当前集中，[L2111-2115](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2111-L2115) 让它通过存活检查并写入 `physical_constants`；于是 [L2124](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2124) 的 `key not in _aliases` 为假，告警被豁免。而 `'magn. constant'` 的别名已被删除（不在 `_aliases`），故告警触发。

#### 4.4.5 小练习与答案

**练习 1**：`value('magn. constant')` 触发告警后，是抛异常还是返回值？
**答案**：返回值。`_check_obsolete` 只调用 `warnings.warn`，不 `raise`；`value()` 在告警后照常执行 `return physical_constants[key][0]`。这是「温和弃用」设计——提醒但不破坏旧代码。

**练习 2**：`ConstantWarning` 继承自 `DeprecationWarning`，这意味着什么？
**答案**：默认情况下 Python 会把 `DeprecationWarning` 显示给开发者（且在测试里用 `pytest.filterwarnings` 等可统一处理）；用户可通过 `warnings.filterwarnings('error', category=ConstantWarning)` 把它升级为异常，强制迁移到当前推荐名。

**练习 3**：如何在不读源码的情况下，编程枚举出所有「真正废弃」的常量名？
**答案**：取 `set(physical_constants) - set(scipy.constants._codata._current_constants)` 得到候选废弃集，再排除 `scipy.constants._codata._aliases` 的键，剩下的就是会触发 `ConstantWarning` 的名字（注意这些是私有对象，仅用于学习/调试）。

## 5. 综合实践

把本讲四个模块串起来，完成一次「常量考古」小任务：

1. **合并观察**：阅读 [_codata.py:2063-2071](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2063-L2071)，画出「六版本 → `physical_constants` → `_current_constants`(2022)」的数据流图，并用一句话说明 `_current_constants` 为何只是 `_physical_constants_2022` 的别名而非副本。

2. **三类常量鉴别**：编写一段程序（**示例代码**），把以下三个键分别归类到「当前 / 别名 / 废弃」，并打印其值与是否告警：
   - `'vacuum mag. permeability'`
   - `'mag. constant'`
   - `'magn. constant'`

   提示：用 `warnings.catch_warnings(record=True)` 捕获告警，结合 `key in sc.physical_constants`、`key in sc._codata._aliases`、`key in sc._codata._obsolete_constants` 三个判断。

3. **改名链还原**：根据 [_codata.py:108](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L108)（`magn. constant`, 2002, exact）、[_codata.py:326](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L326)（`mag. constant`, 2006, exact）、[_codata.py:1981](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L1981)（`vacuum mag. permeability`, 2022, 测量值）三行原文，叙述 μ₀ 这个常数的「改名 + 去精确化」历史，并解释为什么 4.3 节要为它单独写一条显式别名。

**预期成果**：你能用自己的话讲清「合并、当前集、别名、废弃」四者的关系，并能预测任意一个常量名访问时是否会告警、返回哪个版本的值。运行结果中无法确定的数值标注「待本地验证」。

## 6. 本讲小结

- 六份 CODATA 版本通过 `physical_constants.update(...)` **按年份顺序合并，后者覆盖前者**，最终每个键持有最新版本的值，旧版独有的键被保留（废弃常量的来源）。
- `_current_constants = _physical_constants_2022` 与 `_current_codata = "CODATA 2022"` 锁定「当前推荐集」，`find()` 只搜它、废弃检测也以它为基准。
- `_aliases` 用四种方式生成别名（`magn.→mag.`、`momentum→mom.um`、`mag. constant→vacuum mag. permeability`、`electric constant→vacuum electric permittivity`），让新旧两种写法查到同一值；`_extra_alias_keys` 为 `mom.um` 目标发放存活白名单。
- 别名存活规则：目标名必须在当前集或白名单中，否则删除——这决定了 `'mag. constant'` 存活（不报警）而 `'magn. constant'` 被删（报警）。
- `_obsolete_constants` 标记「不在当前集」的键，`_check_obsolete` 对「废弃且非别名」的键发 `ConstantWarning`（`DeprecationWarning` 子类）但**照常返回旧值**，实现温和弃用。

## 7. 下一步学习建议

本讲完成了 CODATA 数据库的全部内部机制。接下来建议：

- **u3-l1（非线性温度转换 convert_temperature）**：离开 CODATA，回到 `_constants.py`，看 constants 中唯一一个非线性换算函数如何设计。
- **u3-l4（测试体系与回归保护）**：阅读 `tests/test_codata.py`，重点关注 `test_gh11341`（别名等价）与 `test_exact_values`（精确值回填），它们正是锁定本讲所讲行为的回归测试。
- 若想理解 `_sub_module_deprecation` 这类弃用机制的更通用形态，可跳读 u3-l3（弃用模块垫片）。
