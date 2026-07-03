# ARFF 格式与属性类型（arff）

## 1. 本讲目标

学完本讲后，你应该能够：

- 读懂 ARFF（WEKA 数据集）文本文件的「三段式」结构：`@relation` / `@attribute` / `@data`，并说出每一段的作用，同时区分**缺失值**（`?`）与**稀疏数据**（`{...}`）这两种 ARFF 规范支持、但 scipy 读取器支持程度不同的写法。
- 理解 `scipy.io.arff._arffread` 里的 `Attribute` 类族——`NumericAttribute` / `NominalAttribute` / `StringAttribute` / `DateAttribute` / `RelationalAttribute`——是如何用「责任链」式的 `to_attribute` 分发来识别五种属性类型的，以及每种子类如何决定 numpy dtype。
- 跟踪 `tokenize_attribute`（把 `@attribute` 行切成「名字 + 类型串」）与 `split_data_line`（用 `csv.Sniffer` 把数据行切成字段）这两条「文本 → token」路径，并理解带引号 nominal 值为何要靠 csv 方言来处理。
- 掌握 `loadarff` 的完整主流程（`_loadarff` → `read_header` → 逐行 `generator` → 拼 record array）以及返回的 `MetaData` 容器（`names()` / `types()` / `__getitem__` / `__iter__`）的用法与局限。

## 2. 前置知识

本讲承接 [u1-l3](u1-l3-quick-start-examples.md) 已经建立的认知：scipy.io 是「多格式读写器集合」，子模块要经 `scipy.io.arff` 访问。在进入源码前，先建立几个关于 ARFF 本身的直觉。

### 什么是 ARFF / WEKA

ARFF（Attribute-Relation File Format）是机器学习工具 **WEKA** 的标准数据集格式，本质是一个**纯文本**文件，扩展名通常为 `.arff`。它的设计目标是「人能读、机器学习流水线好解析」，常见用途是把一张二维表（若干列「属性」+ 若干行「样本」）存成一个文件，供分类、聚类等算法直接读入。

### 三段式结构的直觉

一个 ARFF 文件从上到下分三段：

1. **`@relation`**：给这份数据集起一个名字（相当于「表名」）。
2. **`@attribute`**：声明每一列的名字和类型（相当于「表头 / schema」）。可以有任意多行，每行一个属性。
3. **`@data`**：真正的数据行（相当于「表体」），每行一条样本，字段顺序与 `@attribute` 声明顺序一致。

这个「schema 在前、数据在后」的结构，决定了 scipy 的解析器也分两步走：**先读头建 schema，再按 schema 解析每一行数据**。

### nominal vs numeric

属性类型里最基础的两类：

- **numeric**（数值型）：存浮点数（`numeric` / `real` / `int` 都归为这一类），对应 numpy 的 `float64`。
- **nominal**（标称型 / 枚举型）：取值只能在一个**固定集合**里，声明时用花括号列出，如 `{red, green, blue}`。它类似数据库的枚举列或 pandas 的 `category`。

### 缺失值与稀疏数据

ARFF 规范支持两种「数据不完整」的写法，要严格区分：

- **缺失值**：在数据里写一个 `?`，表示「这个格子没有值」。scipy 的读取器**支持**缺失值——数值列读成 `NaN`，日期列读成 `NaT`，标称列保留为 `?`。
- **稀疏数据**：在数据里写成 `{0 1.5, 3 2.0}`，表示「只有第 0、3 列有值，其余默认 0」。注意这里的 `{}` 出现在**数据段**，与 nominal 声明里的 `{red,green}` 毫无关系。scipy 的读取器**不支持**稀疏数据格式。

> 提醒：别把 nominal 声明里的 `{...}` 和稀疏数据里的 `{...}` 搞混——前者在 `@attribute` 行、是类型定义；后者在 `@data` 行、是数据写法。

### record array（结构化数组）

`loadarff` 返回的第一个值是 numpy 的 **record array**（结构化数组）：它像一个「每列各有名字和 dtype 的二维表」，既可以用 `data['color']` 取出某一列，也可以用 `data[0]` 取出某一行（一个元组）。这一点和普通 `ndarray` 不同，是理解返回结果的关键。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [scipy/io/arff/_arffread.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py) | ARFF 的全部实现。定义异常类、`Attribute` 类族、`tokenize_attribute` / `read_header` / `split_data_line` 等解析工具、`MetaData` 容器，以及公共函数 `loadarff` 与内部 `_loadarff`。本讲几乎全部内容都来自这个文件。 |
| [scipy/io/arff/__init__.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/__init__.py) | 子包入口。用 `from ._arffread import *` 把公共名字提升到 `scipy.io.arff` 命名空间；同时保留弃用包装 `arffread`（SciPy 2.0 移除）。 |
| [scipy/io/arff/tests/test_arffread.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/tests/test_arffread.py) | 测试套件。按「缺数据 / 无数据 / 头解析 / 日期 / 关系属性 / 带引号标称」分组，是验证各属性类型行为的最佳参照。 |
| [scipy/io/arff/tests/data/](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/tests/data) | 测试数据目录。含 `iris.arff`（4 numeric + 1 nominal，150 行）、`missing.arff`（含 `?`）、`quoted_nominal.arff`（带引号标称）、`test9.arff`（关系属性）等，本讲实践直接复用它们。 |

> 关于公共/私有模块的约定：真正干活的实现藏在带下划线的 `_arffread.py`，而 `arffread.py`（无下划线）只是 SciPy 2.0 即将移除的弃用壳。这与 [u1-l2](u1-l2-directory-and-build.md) 讲的全局约定一致——本讲只读 `_arffread.py`。

---

## 4. 核心概念与源码讲解

### 4.1 ARFF 三段式结构与 read_header 头解析

#### 4.1.1 概念说明

ARFF 文件是纯文本，行与行之间靠**关键字**分段。关键字大小写不敏感（`@RELATION` / `@relation` / `@Relation` 等价），且 `%` 开头是注释、空行被忽略。三段对应三个关键字：

| 关键字 | 作用 | 数量 |
|--------|------|------|
| `@relation <名字>` | 数据集名 | 0 或 1 个 |
| `@attribute <名字> <类型>` | 声明一列（名字 + 类型） | 任意多个 |
| `@data` | 数据段开始标志，之后每行一条样本 | 1 个 |

`@data` 是头与数据的**分水岭**：解析器一旦读到 `@data` 这一行，就认为 schema 已经建完，后续都是数据。因此「读头」的本质就是「从文件开头逐行读，直到撞见 `@data` 为止」。

真实例子（`iris.arff` 的头部，注释已省略）：

```
@RELATION iris

@ATTRIBUTE sepallength	REAL
@ATTRIBUTE sepalwidth 	REAL
@ATTRIBUTE petallength	REAL
@ATTRIBUTE petalwidth	REAL
@ATTRIBUTE class 	{Iris-setosa,Iris-versicolor,Iris-virginica}

@DATA
5.1,3.5,1.4,0.2,Iris-setosa
...
```

可以看到：4 个 `REAL`（数值）属性 + 1 个 nominal（花括号枚举）属性，`@DATA` 之后每行 5 个字段、逗号分隔。

#### 4.1.2 核心流程

`read_header` 用一组**正则**做行分类，循环推进直到命中 `@data`。流程如下：

```
read_header(ofile):
    i = 读第一行
    跳过所有 % 注释行
    relation = None; attributes = []
    while 不是 @data 行:
        if 像 @xxx 头行:
            if 是 @attribute 行:
                attr, i = tokenize_attribute(ofile, i)   # 切成 Attribute 对象
                attributes.append(attr)
            elif 是 @relation 行:
                relation = 取出的名字; i = 下一行
            else:
                报错
        else:
            i = 下一行            # 跳过空行等
    return relation, attributes
```

关键正则（大小写不敏感，靠 `[Rr][Ee]...` 这种逐字母写法实现）：

- `r_datameta`：匹配 `@DATA`，是头解析的终止条件。
- `r_relation`：匹配 `@relation` 并捕获其后的名字。
- `r_headerline`：匹配任何 `@关键字` 开头的行（用于区分「头行」与「无关行」）。
- `r_comment` / `r_empty`：注释行（`%`）与空行。

#### 4.1.3 源码精读

模块顶部的正则常量把上面的行分类规则代码化（[_arffread.py:L30-L47](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L30-L47)）：

```python
r_datameta = re.compile(r'^@[Dd][Aa][Tt][Aa]')
r_relation = re.compile(r'^@[Rr][Ee][Ll][Aa][Tt][Ii][Oo][Nn]\s*(\S*)')
r_attribute = re.compile(r'^\s*@[Aa][Tt][Tt][Rr][Ii][Bb][Uu][Tt][Ee]\s*(..*$)')
```

> 注意 `r_attribute` 捕获的是 `@attribute` 之后**整行的剩余内容**（`(..*$)`），交给后面的 `tokenize_attribute` 再切。这里只负责「认出这是属性行」。

`read_header` 主体（[_arffread.py:L636-L664](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L636-L664)）就是上面流程图的直译：

```python
def read_header(ofile):
    i = next(ofile)
    while r_comment.match(i):      # 跳过开头注释
        i = next(ofile)
    relation = None
    attributes = []
    while not r_datameta.match(i): # 直到 @data 为止
        m = r_headerline.match(i)
        if m:
            isattr = r_attribute.match(i)
            if isattr:
                attr, i = tokenize_attribute(ofile, i)
                attributes.append(attr)
            else:
                isrel = r_relation.match(i)
                if isrel:
                    relation = isrel.group(1)
                else:
                    raise ValueError(f"Error parsing line {i}")
                i = next(ofile)
        else:
            i = next(ofile)
    return relation, attributes
```

几个要点：

- **逐行推进的迭代器模型**：`ofile` 是一个**行迭代器**（文件对象或 `StringIO` 都行），`next(ofile)` 读下一行。`tokenize_attribute` 会在内部再 `next(iterable)` 推进，所以 `read_header` 把推进的责任「下放」给了 `tokenize_attribute`（关系属性甚至会连读多行，见 4.3）。
- **`@relation` 的名字来自捕获组**：`isrel.group(1)` 取正则第一个括号——也就是 `@relation` 后面的第一个非空白串。
- **非法头行直接抛 `ValueError`**：既不是 `@attribute` 也不是 `@relation` 的 `@xxx` 行会报错，这个 `ValueError` 最终在 `_loadarff` 里被包成 `ParseArffError`（见 4.4）。

#### 4.1.4 代码实践

**目标**：亲手跑一遍「读头」，看清 `relation` 和 `attributes` 长什么样。

1. 找到测试数据 `iris.arff` 的真实路径。
2. 用 `read_header` 直接读它，打印 `relation`、属性个数、每个属性的名字和类型。
3. 观察 nominal 属性的 `values` 字段。

```python
# 示例代码
import os
from scipy.io.arff._arffread import read_header

# 定位 scipy.io.arff 自带的测试数据（路径按你的环境调整）
import scipy.io.arff as arff
data_dir = os.path.join(os.path.dirname(arff.__file__), 'tests', 'data')
iris = os.path.join(data_dir, 'iris.arff')

with open(iris) as ofile:
    rel, attrs = read_header(ofile)

print("relation =", rel)
print("属性个数 =", len(attrs))
for a in attrs:
    print(f"  {a.name:14s}  type={a.type_name}  range={a.range}")
```

**需要观察的现象**：

- `relation` 应为 `'iris'`。
- 一共 5 个属性；前 4 个 `type_name='numeric'`、`range=None`；最后一个 `class` 的 `type_name='nominal'`、`range=('Iris-setosa','Iris-versicolor','Iris-virginica')`。
- nominal 的 `range` 正好是花括号里那三个枚举值组成的元组。

**预期结果**：与上面描述一致。如果路径找不到，可改用仓库源码目录 `scipy/io/arff/tests/data/iris.arff` 的绝对路径。

#### 4.1.5 小练习与答案

**练习 1**：如果一份 ARFF 文件**完全没有** `@relation` 行，`read_header` 会怎样？`relation` 会是什么？

> **参考答案**：不会报错。`relation` 初值为 `None`（[_arffread.py:L645](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L645)），循环里只有遇到 `@relation` 才赋值；没有这一行就一路保持 `None`，最终 `MetaData.name` 也是 `None`。`@relation` 是可选的。

**练习 2**：`@DATA` 写成 `@data` 或 `@Data` 能被识别吗？为什么？

> **参考答案**：能。`r_datameta` 用 `^@[Dd][Aa][Tt][Aa]` 逐字母大小写不敏感匹配（[_arffread.py:L37](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L37)），所以 `@DATA` / `@data` / `@Data` 都命中。ARFF 规范本身允许关键字大小写混用，源码忠实实现了这一点。

---

### 4.2 Attribute 类族：五种属性类型

#### 4.2.1 概念说明

ARFF 的 `@attribute` 类型串不止 numeric 和 nominal 两种，规范一共定义了五种主流类型，scipy 各用一个类来表示：

| 类型串 | 类 | 含义 | numpy dtype |
|--------|-----|------|-------------|
| `numeric` / `real` / `integer` | `NumericAttribute` | 浮点数 | `float64` |
| `{a, b, c}` | `NominalAttribute` | 枚举（取值固定集合） | `bytes_`（定长字节串） |
| `string` | `StringAttribute` | 任意字符串 | `object_`（基类默认） |
| `date "<格式>"` | `DateAttribute` | 日期时间 | `datetime64[unit]` |
| `relational` | `RelationalAttribute` | 嵌套子表（多值属性） | `object_`（每行一个子数组） |

每种类都做两件事：

1. **`parse_attribute(name, attr_string)`**（类方法）：看一眼类型串，能认出就返回一个自己类型的实例，认不出就返回 `None`。
2. **`parse_data(data_str)`**（实例方法）：把数据段里的一个字段字符串，转换成该类型对应的 Python/numpy 值。

这五种类都继承自抽象基类 `Attribute`，后者提供公共骨架（`name`、`range`、`dtype`、默认的 `parse_attribute`/`parse_data` 返回 `None`）。

#### 4.2.2 核心流程

类型识别用**责任链**模式：`to_attribute` 把类型串依次喂给五个类的 `parse_attribute`，第一个返回非 `None` 的胜出。

```
to_attribute(name, attr_string):
    for cls in (NominalAttribute, NumericAttribute, DateAttribute,
                StringAttribute, RelationalAttribute):
        attr = cls.parse_attribute(name, attr_string)
        if attr is not None:
            return attr
    raise ParseArffError("unknown attribute ...")
```

顺序很关键：

- **Nominal 在最前**：因为 nominal 的类型串以 `{` 开头，特征极强，必须先于其他判断。
- **Numeric 靠 `numeric`/`int`/`real` 前缀**：注意 `integer` 也归为 numeric（整数信息会丢失，统一变 `float64`，源码注释里有 TODO 说明）。
- **Date 靠 `date` 前缀**：之后再单独解析日期格式串。
- **String 靠 `string` 前缀**。
- **Relational 靠 `relational` 前缀**：识别后还要连读若干嵌套 `@attribute` 行（见 4.3）。

#### 4.2.3 源码精读

基类 `Attribute` 定义了骨架（[_arffread.py:L77-L104](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L77-L104)）：

```python
class Attribute:
    type_name: str | None = None
    def __init__(self, name):
        self.name = name
        self.range = None
        self.dtype = np.object_
    @classmethod
    def parse_attribute(cls, name, attr_string):
        return None           # 子类覆盖：认得出返回实例，认不出返回 None
    def parse_data(self, data_str):
        return None
```

`NumericAttribute` 是最简单的一种（[_arffread.py:L182-L206](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L182-L206)）：用前缀匹配识别，dtype 固定 `float64`：

```python
class NumericAttribute(Attribute):
    def __init__(self, name):
        super().__init__(name)
        self.type_name = 'numeric'
        self.dtype = np.float64
    @classmethod
    def parse_attribute(cls, name, attr_string):
        attr_string = attr_string.lower().strip()
        if (attr_string[:len('numeric')] == 'numeric' or
           attr_string[:len('int')] == 'int' or
           attr_string[:len('real')] == 'real'):
            return cls(name)
        return None
```

它的 `parse_data` 处理缺失值——遇到 `?` 返回 `np.nan`（[_arffread.py:L208-L236](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L208-L236)）：

```python
def parse_data(self, data_str):
    if '?' in data_str:
        return np.nan
    else:
        return float(data_str)
```

`NominalAttribute` 用花括号识别，并把枚举值存进 `range` 和 `values`，dtype 是「最长枚举值长度」的定长字节串（[_arffread.py:L107-L160](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L107-L160)）：

```python
class NominalAttribute(Attribute):
    type_name = 'nominal'
    def __init__(self, name, values):
        super().__init__(name)
        self.values = values
        self.range = values
        self.dtype = (np.bytes_, max(len(i) for i in values))
    @classmethod
    def parse_attribute(cls, name, attr_string):
        if attr_string[0] == '{':
            values = cls._get_nom_val(attr_string)   # 用 r_nominal 正则取出 { } 内的值
            return cls(name, values)
        return None
```

它的 `parse_data` 做集合校验：值必须在 `self.values` 里，否则报错；但 `?`（缺失）放行（[_arffread.py:L162-L171](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L162-L171)）：

```python
def parse_data(self, data_str):
    if data_str in self.values:
        return data_str
    elif data_str == '?':
        return data_str
    else:
        raise ValueError(f"{str(data_str)} value not in {str(self.values)}")
```

`DateAttribute` 稍复杂：它要把 ARFF 沿用的 **Java SimpleDateFormat** 记号（`yyyy`/`MM`/`dd`/`HH`/`mm`/`ss`）翻译成 Python `strptime` 的 C 风格记号（`%Y`/`%m`/`%d`/`%H`/`%M`/`%S`），同时推断一个 datetime 单位（Y/M/D/h/m/s）作为 dtype（[_arffread.py:L268-L333](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L268-L333)）。其 `parse_data` 用 `datetime.strptime` 解析后转成对应单位的 `datetime64`，缺失值 `?` 转成 `NaT`（[_arffread.py:L335-L345](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L335-L345)）。注意带时区（`z` 或 `Z`）会直接抛 `ValueError`，scipy 暂不支持。

`StringAttribute`（[_arffread.py:L244-L265](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L244-L265)）和 `RelationalAttribute`（[_arffread.py:L351-L396](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L351-L396)）的识别逻辑类似（靠前缀），但实现深浅不同——string 实际上**不能**进入最终数据加载（见 4.4），relational 则会把一行里用 `\n` 分隔的多条子记录重建成一个嵌套结构化数组。

责任链分发器 `to_attribute`（[_arffread.py:L402-L411](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L402-L411)）：

```python
def to_attribute(name, attr_string):
    attr_classes = (NominalAttribute, NumericAttribute, DateAttribute,
                    StringAttribute, RelationalAttribute)
    for cls in attr_classes:
        attr = cls.parse_attribute(name, attr_string)
        if attr is not None:
            return attr
    raise ParseArffError(f"unknown attribute {attr_string}")
```

#### 4.2.4 代码实践

**目标**：直接调用各类的 `parse_attribute` 与 `parse_data`，看清五种类型的识别与取值。

```python
# 示例代码
from scipy.io.arff._arffread import (to_attribute, NumericAttribute,
    NominalAttribute, DateAttribute)

# 识别
n = to_attribute('age', 'numeric')
m = to_attribute('color', '{red, green, blue}')
d = to_attribute('day', 'date "yyyy-MM-dd"')
print(type(n).__name__, type(m).__name__, type(d).__name__)   # Numeric/Nominal/Date
print("nominal dtype =", m.dtype, " values =", m.values)
print("date format   =", d.date_format, " unit =", d.datetime_unit)

# 取值（含缺失）
print(n.parse_data('42'))      # 42.0
print(n.parse_data('?'))       # nan
print(m.parse_data('red'))     # 'red'
print(m.parse_data('purple'))  # 抛 ValueError（不在枚举里）
```

**需要观察的现象**：

- `n.dtype` 是 `float64`；`m.dtype` 形如 `(numpy.bytes_, 5)`（5 = `'green'`/`' red'` 等的最大长度，注意 `_get_nom_val` 会按分隔符切分，空白处理依赖 `split_data_line`）。
- `d.date_format` 被翻译成了 `'%Y-%m-%d'`，`d.datetime_unit` 为 `'D'`。
- `m.parse_data('purple')` 抛 `ValueError: purple value not in ('red', 'green', 'blue')`。

**预期结果**：与上述一致。`to_attribute('x', 'integer')` 也会返回 `NumericAttribute`（整数被并入 numeric）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `to_attribute` 里 `NominalAttribute` 必须排在 `NumericAttribute` 前面？反过来排会出什么问题？

> **参考答案**：因为 nominal 的类型串 `{a,b,c}` 以 `{` 开头，而 numeric 靠 `numeric`/`int`/`real` 前缀识别。虽然两者识别条件不重叠（一个看首字符 `{`，一个看字符串前缀），顺序在这里不会造成误判；但 nominal 的特征「首字符即定」最强，放在最前是最稳的责任链顺序。真正会冲突的是：若有人误把枚举值命名为 `numeric`，只要首字符是 `{` 就会被 nominal 先截走——这正说明 nominal 优先是合理的。

**练习 2**：`integer` 类型的列，loadarff 读出来 dtype 是 `int64` 还是 `float64`？为什么？

> **参考答案**：是 `float64`。`NumericAttribute.parse_attribute` 对 `int` / `integer` 前缀也返回 `NumericAttribute`（[_arffread.py:L201-L203](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L201-L203)），其 dtype 固定 `float64`。源码注释（[_arffread.py:L22-L24](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L22-L24)）明确写了「integer 和 real 都按 numeric 处理，整数信息丢失」——这是一个已知的简化。

**练习 3**：`DateAttribute` 遇到带时区的格式串（如 `"yyyy-MM-dd HH:mm Z"`）会怎样？

> **参考答案**：抛 `ValueError("Date type attributes with time zone not supported, yet")`（[_arffread.py:L306-L308](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L306-L308)）。测试 `test8.arff` 与 `test_datetime_timezone` 专门覆盖这条路径，最终被包成 `ParseArffError`。

---

### 4.3 tokenize_attribute 与 split_data_line：token 化双路径

#### 4.3.1 概念说明

`read_header` 只负责「认出这是 `@attribute` 行」，真正把这一行切成「属性名 + 类型串」的是 `tokenize_attribute`。它要处理两类命名写法：

- **带引号的属性名**：`@attribute 'floupi 2' real`——名字里有空格，用单引号包住。对应正则 `r_comattrval`。
- **普通属性名**：`@attribute floupi real`——名字不含空格。对应正则 `r_wcomattrval`。

切出 `(name, type)` 后，`tokenize_attribute` 调 `to_attribute` 把类型串变成 `Attribute` 对象；如果类型是 `relational`，还要**继续连读**若干嵌套 `@attribute` 行，直到遇到 `@END <名字>`。

数据段那边则是另一条 token 化路径：`split_data_line` 负责把一行数据（如 `18, 'no'`）切成字段列表 `['18', "'no'"]`。它不是简单按逗号 split，而是借助 Python 标准库的 **`csv.Sniffer`** 自动探测分隔符（`,` 或 `\t`）和引号方言——这正是处理带引号 nominal 值（`'  yes'` 这种引号内带空格）的关键。

#### 4.3.2 核心流程

**头部分词**（`tokenize_attribute`）：

```
tokenize_attribute(iterable, attribute_line):
    去掉首尾空白，用 r_attribute 取出 @attribute 之后的内容 atrv
    if atrv 形如 '名字' 类型:       # 带引号
        name, type = tokenize_single_comma(atrv)
    elif atrv 形如 名字 类型:        # 普通无引号
        name, type = tokenize_single_wcomma(atrv)
    else:
        raise ValueError("multi line not supported yet")
    attribute = to_attribute(name, type)     # 类型识别
    if type 是 'relational':
        连读嵌套 @attribute 直到 @END name   # read_relational_attribute
    return attribute, next_item
```

**数据分词**（`split_data_line`）：

```
split_data_line(line, dialect=None):
    把 csv 字段上限调到最大（应对超长 relational 字段）
    去掉行尾换行与首尾空白
    if 该行没有 , 也没有 \t: 补一个 ',' 让 Sniffer 不报错
    if dialect is None:
        dialect = csv.Sniffer().sniff(line)    # 自动探测分隔符/引号
        workaround_csv_sniffer_bug_last_field(...)  # 修 Python csv 的已知 bug
    row = next(csv.reader([line], dialect))    # 按 dialect 切字段
    return row, dialect                        # dialect 复用，避免每行重探测
```

#### 4.3.3 源码精读

`tokenize_attribute` 用两个正则区分带引号与不带引号的命名（[_arffread.py:L515-L581](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L515-L581)）：

```python
def tokenize_attribute(iterable, attribute):
    sattr = attribute.strip()
    mattr = r_attribute.match(sattr)
    if mattr:
        atrv = mattr.group(1)                       # @attribute 之后的内容
        if r_comattrval.match(atrv):                # 'name with space' type
            name, type = tokenize_single_comma(atrv)
            next_item = next(iterable)
        elif r_wcomattrval.match(atrv):             # name type
            name, type = tokenize_single_wcomma(atrv)
            next_item = next(iterable)
        else:
            raise ValueError("multi line not supported yet")
    attribute = to_attribute(name, type)
    if type.lower() == 'relational':
        next_item = read_relational_attribute(iterable, attribute, next_item)
    return attribute, next_item
```

两个辅助函数 `tokenize_single_comma` / `tokenize_single_wcomma`（[_arffread.py:L584-L611](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L584-L611)）分别对应带引号正则 `r_comattrval`（`'(..+)'\s+(..+$)`）和无引号正则 `r_wcomattrval`（`(\S+)\s+(..+$)`），各自用捕获组取出 name 和 type。

关系属性的连读由 `read_relational_attribute` 完成（[_arffread.py:L614-L633](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L614-L633)）：它循环读行，把每个嵌套 `@attribute` 递归地 `tokenize_attribute` 后塞进 `relational_attribute.attributes` 列表，直到命中 `@END <关系属性名>`：

```python
def read_relational_attribute(ofile, relational_attribute, i):
    r_end_relational = re.compile(r'^@[Ee][Nn][Dd]\s*' +
                                  relational_attribute.name + r'\s*$')
    while not r_end_relational.match(i):
        m = r_headerline.match(i)
        if m and r_attribute.match(i):
            attr, i = tokenize_attribute(ofile, i)
            relational_attribute.attributes.append(attr)
        else:
            i = next(ofile)
    i = next(ofile)
    return i
```

数据分词 `split_data_line`（[_arffread.py:L480-L509](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L480-L509)）的核心是用 `csv.Sniffer`：

```python
def split_data_line(line, dialect=None):
    delimiters = ",\t"
    csv.field_size_limit(int(ctypes.c_ulong(-1).value // 2))  # 放宽字段上限
    if line[-1] == '\n':
        line = line[:-1]
    line = line.strip()
    sniff_line = line
    if not any(d in line for d in delimiters):
        sniff_line += ","                          # 单字段时补分隔符，避免 Sniffer 报错
    if dialect is None:
        dialect = csv.Sniffer().sniff(sniff_line, delimiters=delimiters)
        workaround_csv_sniffer_bug_last_field(sniff_line=sniff_line,
                                              dialect=dialect, delimiters=delimiters)
    row = next(csv.reader([line], dialect))
    return row, dialect
```

三个细节值得记住：

- **`dialect` 复用**：第一次探测后，`dialect` 被返回并传入下一次调用，避免每行都跑一遍 `Sniffer`（性能优化，见 4.4 generator）。
- **`csv.field_size_limit` 调到极大**：用 `ctypes.c_ulong(-1).value // 2` 算出平台最大无符号长整数的一半，目的是支持**超大 relational 字段**（注释里写明「relational fields can be HUGE」）。
- **`workaround_csv_sniffer_bug_last_field`**（[_arffread.py:L414-L477](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L414-L477)）：修补 Python csv 模块的一个已知 bug（[bpo-30157](https://bugs.python.org/issue30157)），确保**最后一个字段**带引号时方言探测正确。这正是带引号 nominal 能正确解析的底层原因。

#### 4.3.4 代码实践

**目标**：用 `StringIO` 喂一段带引号属性名 + 带引号 nominal 数据的 ARFF，验证两条分词路径。

```python
# 示例代码
from io import StringIO
from scipy.io.arff import loadarff

content = """
@relation demo
@attribute 'my attr' numeric
@attribute smoker {'yes', 'no'}
@data
18,'no'
24,'yes'
"""
data, meta = loadarff(StringIO(content))
print(meta.names())          # ['my attr', 'smoker']
print(data['smoker'])        # [b'no' b'yes']
```

再用 `test_arffread.py` 里的 `quoted_nominal_spaces.arff`（`{'  yes', 'no  '}`，引号内带空格）做对照：

```python
import os
from scipy.io.arff import loadarff
import scipy.io.arff as arff
f = os.path.join(os.path.dirname(arff.__file__), 'tests', 'data',
                 'quoted_nominal_spaces.arff')
data, meta = loadarff(f)
print(meta['smoker'])        # ('nominal', ['  yes', 'no  '])  —— 引号内空格被保留
print(data['smoker'])        # [b'no  ' b'  yes' b'no  ' ...]   —— 空格保留
```

**需要观察的现象**：

- 带引号的属性名 `'my attr'` 被正确切成名字 `my attr`（含空格），走的是 `tokenize_single_comma`。
- `quoted_nominal_spaces.arff` 的枚举值 `'  yes'` / `'no  '` 的**内部空格被完整保留**（dtype 是 `<S5`，长度按 `'  yes'`/`'no  '` 算），而引号**外**的空白被剥掉。这说明分隔与剥空白交给 csv 方言处理，引号内的内容原样保留。

**预期结果**：与上述一致。若把引号去掉、写成 `{  yes, no  }`，空白处理会不同——这正是 `csv.Sniffer` + 方言带来的行为。

#### 4.3.5 小练习与答案

**练习 1**：`tokenize_attribute` 在什么情况下抛 `"multi line not supported yet"`？

> **参考答案**：当 `@attribute` 之后的内容既不匹配带引号正则 `r_comattrval`（`'name' type`）、也不匹配无引号正则 `r_wcomattrval`（`name type`）时（[_arffread.py:L568-L571](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L568-L571)）。例如属性名跨多行、或类型串格式怪异时。注释也说「不确定 WEKA 是否支持」，故直接报错。

**练习 2**：为什么 `split_data_line` 要把 `csv.field_size_limit` 调到那么大？

> **参考答案**：因为 relational 属性的一「行」数据里，可能用 `\n` 分隔着成百上千条子记录，整条字符串可能非常长（注释明确写「relational fields can be HUGE」，见 [_arffread.py:L483-L485](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L483-L485)）。Python csv 默认字段上限只有约 131 KB，超大字段会被截断，所以要先放宽。测试 `test10.arff`（3 万条子记录）就是为覆盖这种场景。

---

### 4.4 loadarff 主流程与 MetaData 返回结构

#### 4.4.1 概念说明

`loadarff` 是子包对外的总入口，它把「读头 → 逐行解析 → 拼数组」串起来，返回一个二元组 `(data, meta)`：

- **`data`**：numpy **record array**（结构化数组）。每一列对应一个 `@attribute`，列名 = 属性名，列 dtype = 该属性类型的 dtype。既可 `data['color']` 取列，也可 `data[0]` 取行。
- **`meta`**：一个 `MetaData` 对象，保存数据集名和全部属性的「名字 → Attribute」映射，提供 `names()` / `types()` / `__getitem__` / `__iter__` 等查询接口。

两个重要局限要先讲清楚（容易踩坑）：

1. **string 属性不支持**：`_loadarff` 在读完头后会扫描所有属性，只要有一个 `StringAttribute`，就直接抛 `NotImplementedError("String attributes not supported yet, sorry")`。也就是说 string 能在头里被识别，但无法进入数据加载。
2. **稀疏数据不支持**：数据段里的 `{...}` 稀疏写法读不了（`loadarff` 的 docstring 明确声明）。

> 一个关于「文档 vs 实现」的提醒：`loadarff` 的 docstring（[_arffread.py:L779-L791](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L779-L791)）说「未实现：date 和 string 类型」。但 date 其实**已经实现**（`DateAttribute.parse_data` 存在，`test7.arff` 的日期用例全过）。这是 docstring 滞后于代码的典型例子——遇到不确定的行为，读测试比读 docstring 更可靠。

#### 4.4.2 核心流程

```
loadarff(f):
    if f 有 read 方法: ofile = f          # 文件对象 / StringIO 直接用
    else:           ofile = open(f)       # 字符串路径先打开
    try:
        return _loadarff(ofile)
    finally:
        只关闭「自己打开的」文件            # ofile is not f 才关

_loadarff(ofile):
    rel, attr = read_header(ofile)         # 1. 读头建 schema
    if 任一属性是 StringAttribute:
        raise NotImplementedError(...)    # string 不支持
    meta = MetaData(rel, attr)             # 2. 建 MetaData
    def generator(row_iter):
        for raw in ofile:                  # 3. 逐行
            跳过 % 注释和空行
            row, dialect = split_data_line(raw, dialect)   # 切字段
            yield tuple(attr[i].parse_data(row[i]) for i in range(ni))  # 按类型转值
    a = list(generator(ofile))             # 4. 物化成元组列表
    data = np.array(a, [(a.name, a.dtype) for a in attr])  # 5. 拼 record array
    return data, meta
```

第 4 步是性能热点（源码注释自评约占 80% 时间），所以 generator 内联了「跳过注释/空行」「复用 dialect」等优化，而不是抽出函数。

#### 4.4.3 源码精读

公共入口 `loadarff` 只做「打开/关闭文件」的资源管理（[_arffread.py:L819-L827](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L819-L827)）：

```python
def loadarff(f):
    if hasattr(f, 'read'):
        ofile = f
    else:
        ofile = open(f)
    try:
        return _loadarff(ofile)
    finally:
        if ofile is not f:   # 只关闭自己打开的，不关调用者传入的
            ofile.close()
```

> 这个 `try/finally` + `ofile is not f` 的写法，保证了「字符串路径会关、文件对象不关」，是处理「参数既可是路径也可是文件」的常见安全模式。

真正的逻辑在 `_loadarff`（[_arffread.py:L830-L892](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L830-L892)）。头解析的 `ValueError` 在这里被包成 `ParseArffError`，并先做 string 检查：

```python
def _loadarff(ofile):
    try:
        rel, attr = read_header(ofile)
    except ValueError as e:
        msg = "Error while parsing header, error was: " + str(e)
        raise ParseArffError(msg) from e
    hasstr = any(isinstance(a, StringAttribute) for a in attr)
    meta = MetaData(rel, attr)
    if hasstr:
        raise NotImplementedError("String attributes not supported yet, sorry")
    ...
```

逐行解析的 generator（[_arffread.py:L861-L887](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L861-L887)）与最终拼数组（[_arffread.py:L889-L892](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L889-L892)）：

```python
    def generator(row_iter, delim=','):
        elems = list(range(ni))
        dialect = None
        for raw in row_iter:
            if r_comment.match(raw) or r_empty.match(raw):
                continue                              # 跳过注释/空行
            row, dialect = split_data_line(raw, dialect)
            yield tuple([attr[i].parse_data(row[i]) for i in elems])
    a = list(generator(ofile))
    data = np.array(a, [(a.name, a.dtype) for a in attr])   # 结构化 dtype
    return data, meta
```

关键是最后一行：`[(a.name, a.dtype) for a in attr]` 把每个属性的「名字 + dtype」组成结构化 dtype 描述符，`np.array` 据此把元组列表装配成 record array。所以**列名和列类型完全由头里的 `@attribute` 决定**。

`MetaData` 容器（[_arffread.py:L667-L746](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L667-L746)）内部就是一个保持插入顺序的字典：

```python
class MetaData:
    def __init__(self, rel, attr):
        self.name = rel
        self._attributes = {a.name: a for a in attr}   # dict 保序
    def __iter__(self):
        return iter(self._attributes)                  # 迭代 = 按声明顺序遍历名字
    def __getitem__(self, key):
        attr = self._attributes[key]
        return (attr.type_name, attr.range)            # 取值返回 (类型, range)
    def names(self):
        return list(self._attributes)                  # 属性名列表
    def types(self):
        return [self._attributes[n].type_name for n in self._attributes]  # 类型名列表
```

所以用法是：`meta.name`（数据集名）、`meta.names()`（列名）、`meta.types()`（列类型）、`meta['color']`（返回 `('nominal', ('red',...))`）、`for name in meta`（按序遍历列名）。`__repr__`（[_arffread.py:L708-L716](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L708-L716)）会把数据集名和每个属性的类型/range 打印成多行文本，方便直接看。

#### 4.4.4 代码实践

**目标**：完成规格要求的核心实践——用真实 `.arff` 调 `loadarff`，打印 MetaData 的属性名与类型，统计 `@data` 行数；再手写一个含 nominal 与 missing 的小 ARFF 加载它。

```python
# 示例代码
import os
import numpy as np
from scipy.io.arff import loadarff
import scipy.io.arff as arff

# (a) 用自带的 iris.arff
data_dir = os.path.join(os.path.dirname(arff.__file__), 'tests', 'data')
iris = os.path.join(data_dir, 'iris.arff')
data, meta = loadarff(iris)

print("数据集名:", meta.name)
print("属性名  :", meta.names())
print("属性类型:", meta.types())
print("@data 行数 (len(data)):", len(data))      # 150
print("class 列的 (类型, range):", meta['class'])
print("sepallength 前 5 行:", data['sepallength'][:5])

# (b) 手写含 nominal + missing 的小 ARFF
from io import StringIO
small = """
@relation demo
@attribute age numeric
@attribute color {red,green,blue}
@data
18,red
?,green
30,?
"""
d2, m2 = loadarff(StringIO(small))
print("\n--- 手写 ARFF ---")
print("names:", m2.names(), " types:", m2.types())
print("age  :", d2['age'])      # [18. nan 30.]      <- ? 变 nan
print("color:", d2['color'])    # [b'red' b'green' b'?']  <- 标称缺失保留 '?'
print("dtype:", d2.dtype)       # [('age','<f8'), ('color','|S5')]
```

**需要观察的现象**：

- (a) iris 的 `meta.types()` 为 `['numeric','numeric','numeric','numeric','nominal']`；`len(data) == 150`（即 `@data` 段共 150 行有效样本，文件末尾的几个 `%` 注释行不计入）。
- (b) 数值列的缺失 `?` 变成 `nan`；标称列的缺失 `?` 保留为字节串 `b'?'`（因为 `NominalAttribute.parse_data` 对 `?` 原样返回）；`color` 的 dtype 长度按 `{'red','green','blue'}` 里最长的 `'green'`（5）决定，所以是 `|S5`。
- `data['age']` 是 `float64` 数组，`data['color']` 是定长字节串数组——列类型完全由 `@attribute` 决定。

**预期结果**：与上述一致。若 `len(data)` 不是 150，先确认读取的是完整的 `iris.arff`（它本身有 150 个数据行）。

#### 4.4.5 小练习与答案

**练习 1**：`loadarff` 的参数 `f` 既可以传字符串路径，也可以传文件对象 / `StringIO`。它是怎么区分的？传入文件对象时会被关闭吗？

> **参考答案**：用 `hasattr(f, 'read')` 区分（[_arffread.py:L819-L821](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L819-L821)）：有 `read` 方法就当文件对象直接用，否则当路径 `open(f)`。关闭策略在 `finally` 里用 `if ofile is not f` 守护（[_arffread.py:L826-L827](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L826-L827)）：只有「自己 open 的」才关，调用者传入的文件对象不会被 `loadarff` 关闭。测试 `test_filelike` 正是验证从 `StringIO` 和文件对象读到的结果一致。

**练习 2**：一个 `@data` 段为空（只有 `@data` 关键字、后面没有数据行）的 ARFF，`loadarff` 返回的 `data` 是什么？

> **参考答案**：是一个长度为 0 的 record array，但 **dtype 仍然完整**（列名和列类型照常由 `@attribute` 决定）。因为 `np.array([], [(name,dtype),...])` 会得到一个空数组、保留结构化 dtype。测试 `test_nodata`（`nodata.arff`）断言 `data.size == 0` 且 `data.dtype` 含全部 5 个字段。这很重要——schema 即使没数据也保留，便于后续往里填。

**练习 3**：`meta['color']` 返回什么？和 `meta.types()` 有什么区别？

> **参考答案**：`meta['color']` 返回该属性的类型与 range 组成的二元组 `(type_name, range)`（[_arffread.py:L721-L724](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L721-L724)），如 `('nominal', ('red','green','blue'))`，是「单属性详情」；`meta.types()` 返回**所有**属性的类型名列表（不含 range），是「全表概览」。numeric 属性的 range 是 `None`，所以 `meta['sepallength']` 是 `('numeric', None)`。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「真实文件解析 → MetaData 查询 → 手写多类型文件 → 行为验证」的完整闭环。

**任务**：写一段脚本，先用 iris 数据看清 schema 与数据形态，再手写一个**含 4 种类型**（numeric / nominal / date / relational 之外的简单组合：numeric + nominal + 含缺失）的小数据集，验证 dtype、缺失值与标称校验。

```python
# 示例代码
import os
import numpy as np
from io import StringIO
from scipy.io.arff import loadarff
from scipy.io.arff._arffread import read_header, to_attribute
import scipy.io.arff as arff

# === Part A：真实文件 iris.arff ===
data_dir = os.path.join(os.path.dirname(arff.__file__), 'tests', 'data')
data, meta = loadarff(os.path.join(data_dir, 'iris.arff'))
print("A. iris relation =", meta.name)
print("   属性 -> 类型:", list(zip(meta.names(), meta.types())))
print("   数据行数:", len(data))
# 取出 class 列的不同取值（标称列的枚举）
print("   class 枚举:", meta['class'][1])

# === Part B：手写多类型 + 缺失值 ===
small = """
@relation mydata
@attribute temp    numeric
@attribute weather {sunny,rainy,cloudy}
@data
36.5,sunny
?,rainy
-1.0,?
"""
d2, m2 = loadarff(StringIO(small))
print("\nB. 手写数据集 dtype:", d2.dtype)
print("   temp 列:", d2['temp'])        # [36.5 nan -1.]
print("   weather 列:", d2['weather'])  # [b'sunny' b'rainy' b'?']

# === Part C：直接验证类型识别与标称校验 ===
attr = to_attribute('weather', '{sunny,rainy,cloudy}')
print("\nC. 识别为:", type(attr).__name__, " dtype:", attr.dtype, " range:", attr.range)
try:
    attr.parse_data('snow')     # 不在枚举里
except ValueError as e:
    print("   标称校验报错:", e)
```

**需要观察与解释的要点**：

1. **Part A**：iris 的 5 个属性类型是 4 个 numeric + 1 个 nominal；`len(data)` 为 150；`meta['class'][1]` 给出三种类别（鸢尾花品种）的元组。
2. **Part B**：数值缺失 `?` → `nan`（含 `-1.0` 这种合法负数也被正确解析）；标称缺失 `?` → `b'?'`；`weather` 的 dtype 长度按 `'cloudy'`（6）决定。
3. **Part C**：`to_attribute` 把 `{sunny,rainy,cloudy}` 认成 `NominalAttribute`；`parse_data('snow')` 因不在枚举集合里抛 `ValueError`——这正是 nominal 列在数据加载时自动做取值校验的体现（4.2 讲过）。

这个任务一次性覆盖了：三段式结构与 `read_header`（4.1）、Attribute 类族与 `to_attribute` 分发（4.2）、数据行 `split_data_line` 分词（4.3）、`loadarff` 主流程与 `MetaData` 返回（4.4）。

## 6. 本讲小结

- ARFF 是 WEKA 的**纯文本**数据集格式，分三段：`@relation`（数据集名，可选）、`@attribute`（每列的名字+类型，可多个）、`@data`（数据行）。`%` 是注释，关键字大小写不敏感。
- `read_header` 用一组逐字母大小写不敏感的正则做行分类，从文件头逐行推进，直到撞见 `@data` 为止，产出 `(relation, [Attribute...])`。
- `Attribute` 类族用**责任链** `to_attribute` 识别五种类型：`NominalAttribute`（`{...}`，dtype 为定长 bytes）/ `NumericAttribute`（`numeric`/`real`/`int`，float64）/ `DateAttribute`（`date "fmt"`，datetime64）/ `StringAttribute` / `RelationalAttribute`；每种子类自己决定 dtype 和 `parse_data`，缺失值 `?` 在 numeric 里变 `nan`、date 里变 `NaT`、nominal 里保留 `?`。
- `tokenize_attribute` 区分带引号 / 无引号属性名两条分词路径，关系属性还会连读到 `@END`；`split_data_line` 借 `csv.Sniffer` 探测分隔符与引号方言（并修了 Python csv 的已知 bug），这是带引号 nominal 值能正确保留引号内空格的关键，且 dialect 跨行复用以提速。
- `loadarff` = 资源管理（路径/文件对象自适应，只关自己打开的）+ `_loadarff`（读头 → string 检查 → 逐行 generator 按属性类型转值 → 拼成 record array）；**string 属性和稀疏数据 `{}` 不支持**，但 date 实际已支持（docstring 滞后）。
- `MetaData` 是一个保序字典容器，提供 `name` / `names()` / `types()` / `__getitem__`（返回 `(type_name, range)`）/ `__iter__`（按声明顺序遍历列名），是连接 schema 与数据的查询接口。

## 7. 下一步学习建议

- 本讲的 ARFF 是**纯文本、schema 在前**的格式，与 [u2-l3](u2-l3-matrix-market-mmio.md) 的 Matrix Market 同属文本格式，可对比两者的「头解析」思路：ARFF 用逐行正则 + 责任链识别类型，Matrix Market 用首行四元组 + 常量校验。
- 想看另一种「二进制 + record」的格式，可继续读 [u2-l5](u2-l5-netcdf3-model.md) 的 NetCDF3——它也有「dimensions/variables/attributes」的 schema 模型，但布局是二进制头 + 记录区，与 ARFF 的纯文本形成对照。
- 想理解 scipy.io 整体的公共/私有模块约定（为什么实现藏在 `_arffread`、`arffread.py` 只是弃用壳），可回到 [u1-l2](u1-l2-directory-and-build.md)；弃用转发的完整机制在 [u4-l2](u4-l2-deprecated-namespaces.md)。
- 直接阅读源码时，建议按本讲顺序：先读正则常量（[_arffread.py:L30-L47](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L30-L47)）→ `read_header`（[_arffread.py:L636-L664](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L636-L664)）→ `Attribute` 类族与 `to_attribute`（[_arffread.py:L77-L411](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L77-L411)）→ `tokenize_attribute` / `split_data_line`（[_arffread.py:L480-L633](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L480-L633)）→ 最后 `loadarff` / `_loadarff` / `MetaData`（[_arffread.py:L667-L892](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/_arffread.py#L667-L892)），这条线最符合「先格式、再类型、再分词、最后主流程」的认知顺序。测试则按 [test_arffread.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/arff/tests/test_arffread.py) 的分组逐类验证。
