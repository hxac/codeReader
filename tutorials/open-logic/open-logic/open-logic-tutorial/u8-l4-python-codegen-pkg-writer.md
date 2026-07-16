# Python 代码生成：olo_fix_pkg_writer

## 1. 本讲目标

本讲讲解 Open Logic `fix` 区域里的一个**纯 Python 工具**：`olo_fix_pkg_writer`。它本身不是可综合的硬件，而是「把 Python 里算好的常量、定点格式、向量，自动写成一份 VHDL 包或 Verilog 头文件」的代码生成器。

学完本讲，你应当能够：

- 说出为什么需要用 Python 生成 HDL 常量包（单一真相源、跨语言一致性）。
- 会用 `olo_fix_pkg_writer` 的 `add_constant` / `add_vector` / `write_vhdl_pkg` / `write_verilog_header` 四个方法。
- 看懂 Jinja2 模板如何把 Python 数据渲染成 VHDL/Verilog，并能解释 `as_string` 的作用。
- 理解 `sim/codegen.py` 为什么必须在 VUnit 扫描文件**之前**运行。
- 为自己的设计维护一份「Python 定义 → HDL 包」的单一真相源。

## 2. 前置知识

本讲假设你已学过 u8-l1（定点原理与 en_cl_fix）和 u8-l2（olo_fix_pkg 与字符串泛型模式）。这里复用几个关键概念：

- **定点格式三元组 `(S, I, F)`**：S=符号位、I=整数位、F=小数位，位宽 `W = S + I + F`。Python 侧用 `FixFormat(S, I, F)` 表示，HDL 侧用 `FixFormat_t` 这个 record。
- **字符串泛型模式**：`olo_fix` 的实体对外用 `string`（如 `"(1,8,8)"`）而非自定义类型来传定点格式，根本动机是让 Verilog 也能实例化（Verilog 里没有 `FixFormat_t`）。
- **单一真相源（single source of truth）**：算法通常先在 Python（en_cl_fix/olo_fix）里搭位真模型，再把参数（格式、系数字长等）交给 HDL。如果参数在 Python 和 HDL 两处各写一份，就很容易不一致；用代码生成器把它们串起来，Python 那份就是唯一来源。
- **Jinja2**：一个 Python 模板引擎，模板里写 `{{ 变量 }}` 和 `{% for %}`，渲染时用真实数据替换，得到最终文本。本讲会用到它，但不需要你提前掌握。

补充几个通用术语：

| 术语 | 含义 |
|------|------|
| 代码生成（codegen） | 在编译/仿真之前，由脚本自动产出源代码文件 |
| VHDL 包（package） | 一组常量、类型、函数的集合，可被多个设计 `use` |
| Verilog 头文件（`.vh`/`.svh`） | Verilog/SystemVerilog 用 `localparam` 定义常量的头文件 |
| 模板（template） | 带占位符的文本骨架，渲染后变成真实代码 |

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [src/fix/python/olo_fix/olo_fix_pkg_writer.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/olo_fix/olo_fix_pkg_writer.py) | 核心类 `olo_fix_pkg_writer`，提供 `add_constant` / `add_vector` / `write_vhdl_pkg` / `write_verilog_header` |
| [src/fix/python/olo_fix/templates/olo_fix_pkg_writer_vhdl.template](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/olo_fix/templates/olo_fix_pkg_writer_vhdl.template) | VHDL 包的 Jinja2 模板 |
| [src/fix/python/olo_fix/templates/olo_fix_pkg_writer_verilog.template](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/olo_fix/templates/olo_fix_pkg_writer_verilog.template) | Verilog 头文件的 Jinja2 模板 |
| [sim/codegen.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/codegen.py) | 仿真前的代码生成脚本，实例化 writer 并产出测试用包 |
| [sim/run.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py) | VUnit 运行器，在文件扫描前调用 `codegen_generate()` |
| [test/fix/olo_fix_pkg_writer/olo_fix_pkg_writer_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_pkg_writer/olo_fix_pkg_writer_tb.vhd) | 验证生成包内容的测试台 |
| [src/fix/python/tests/test_olo_fix_pkg_writer.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/tests/test_olo_fix_pkg_writer.py) | writer 自身的 Python 单元测试 |
| [doc/fix/olo_fix_pkg_writer.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_pkg_writer.md) | 官方使用文档 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **4.1 pkg_writer API**：类与四个公开方法的全貌。
2. **4.2 常量与向量定义**：`add_constant` / `add_vector` 的类型映射与 `as_string`。
3. **4.3 VHDL/Verilog 模板**：Jinja2 模板如何渲染，以及私有声明生成函数。
4. **4.4 codegen 调用时机**：`sim/codegen.py` 与 VUnit 的先后顺序。

### 4.1 olo_fix_pkg_writer API

#### 4.1.1 概念说明

`olo_fix_pkg_writer` 是一个**有状态的生成器对象**：你先 `new` 一个 writer，往里 `add_constant` / `add_vector` 累加成员，最后调用一次 `write_vhdl_pkg` 或 `write_verilog_header` 把全部成员渲染成一个文件。它解决的问题是：算法在 Python 里有格式、系数、阈值等常量，手动抄到 HDL 既繁琐又易错，不如让脚本生成一份包。

类自身的文档字符串把它定位得很清楚：

```python
# This class is used to write VHDL packages resp. verilog header files
# containing information from python.
```

它有四个公开方法：

| 方法 | 作用 |
|------|------|
| `add_constant(name, type, value, as_string=False)` | 加一个标量常量 |
| `add_vector(name, type, value, as_string=False)` | 加一个一维数组（向量） |
| `write_vhdl_pkg(pkg_name, directory, olo_library="olo")` | 渲染成 `<pkg_name>.vhd` |
| `write_verilog_header(pkg_name, directory)` | 渲染成 `<pkg_name>.vh` |

#### 4.1.2 核心流程

整体调用流程是一条「累加 → 渲染」的流水线：

```
new olo_fix_pkg_writer()
        │
        ├── add_constant(...)   ┐
        ├── add_vector(...)     │  累加进内部 dict（去重、校验命名）
        ├── add_constant(...)   ┘
        │
        ▼
write_vhdl_pkg() / write_verilog_header()
        │
        │  为每个成员生成一行声明字符串
        │  填入 Jinja2 模板
        │  写到目标目录
        ▼
   <pkg_name>.vhd  /  <pkg_name>.vh
```

内部状态只有两个字典：常量字典 `_constants` 与向量字典 `_vectors`，两者都把名字映射到一个 `MemberData(type, value, as_string)` 三元组（见 [olo_fix_pkg_writer.py:20](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/olo_fix/olo_fix_pkg_writer.py#L20)）。`add_constant` / `add_vector` 负责往里塞，`write_*` 负责遍历它们并渲染。

#### 4.1.3 源码精读

构造函数只初始化两个空字典，不做别的（[olo_fix_pkg_writer.py:52-57](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/olo_fix/olo_fix_pkg_writer.py#L52-L57)）：

```python
def __init__(self):
    self._vectors = {}
    self._constants = {}
```

四个公开方法里有数据流动的是 `add_*`（写入字典）和 `write_*`（读字典并渲染）。以 `write_vhdl_pkg` 为例，它先把「包名、库名、所有常量声明、所有向量声明」打包成一个 `data` 字典，再交给 Jinja2 渲染（[olo_fix_pkg_writer.py:93-114](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/olo_fix/olo_fix_pkg_writer.py#L93-L114)）：

```python
data = {
    "pkg_name" : pkg_name,
    "olo_library" : olo_library,
    "constants" : [self._vhdl_const_declaration(name, value) for name, value in self._constants.items()],
    "vectors"   : [self._vhdl_vector_declaration(name, value) for name, value in self._vectors.items()]
}
env = Environment(loader=FileSystemLoader(self._TEMPLATE_DIR))
template = env.get_template("olo_fix_pkg_writer_vhdl.template")
rendered_template = template.render(data)
with open(join(directory, f"{pkg_name}.vhd"), "w+") as f:
    f.write(rendered_template)
```

两个关键点：

- `self._TEMPLATE_DIR` 指向本文件同目录下的 `templates/` 子目录（[olo_fix_pkg_writer.py:49](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/olo_fix/olo_fix_pkg_writer.py#L49)），所以模板和工具类是打包发布的。
- 文件名直接由 `pkg_name` 加后缀得到（`.vhd` 或 `.vh`），这也是文档强调「文件名就是 `<pkg_name>.vhd`」的原因。

#### 4.1.4 代码实践

**目标**：运行 writer 自带的 Python 单元测试，确认生成逻辑本身是可信的（不依赖任何仿真器）。

**步骤**：

1. 在仓库根目录执行：

   ```bash
   cd src/fix/python
   python -m pytest tests/test_olo_fix_pkg_writer.py -v
   # 或：python -m unittest tests.test_olo_fix_pkg_writer -v
   ```

2. 观察输出里 `test_write_vhdl_pkg`、`test_write_verilog_pkg`、`test_vhdl_const_declaration` 等用例全部 PASSED。

**需要观察的现象**：所有用例通过；其中 `test_write_vhdl_pkg`（[test_olo_fix_pkg_writer.py:151-185](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/tests/test_olo_fix_pkg_writer.py#L151-L185)）会把生成内容写进临时目录，再断言文件里出现诸如 `constant constInt : integer := 42;` 这样的字符串。

**预期结果**：全部 PASSED。若你的环境缺少 `en_cl_fix` 子模块（未 `--recursive` 克隆），导入 `from en_cl_fix_pkg import *` 会失败——这正说明本工具依赖 en_cl_fix（见 u8-l1）。

> 注意：`3rdParty/en_cl_fix` 是 git 子模块，若目录为空需先 `git submodule update --init`。此环境的子模块可能未检出，「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`write_vhdl_pkg` 与 `write_verilog_header` 都没有返回值（返回 `None`），那它的「输出」在哪？

**答案**：输出是写到磁盘的文件——`<directory>/<pkg_name>.vhd` 或 `.vh`。这是典型的「副作用型」API：调用者靠读文件来拿结果。

**练习 2**：为什么 `add_constant` 和 `add_vector` 要存在两个字典（`_constants` / `_vectors`），而不是合并成一个？

**答案**：因为模板里常量和向量分两段渲染（VHDL 模板有 `-- Constants` 和 `-- Vectors` 两块，见 4.3）。分开存放便于渲染时分别遍历、分别加注释分组。

---

### 4.2 常量与向量定义

#### 4.2.1 概念说明

`add_constant` 加标量，`add_vector` 加一维数组。两者都接受 Python 类型 `int / float / FixFormat / str`（向量不支持 `str`），并有一个关键开关 **`as_string`**：

- `as_string=False`（默认）：按**原生类型**生成。比如 `FixFormat` 在 VHDL 里生成 `FixFormat_t := (1, 8, 8);`。
- `as_string=True`：把值**转成字符串**生成，类型统一为 `string`。比如同一个 `FixFormat` 生成 `string := "(1, 8, 8)";`。

`as_string=True` 的意义正是 u8-l2 讲过的「字符串泛型模式」：`olo_fix` 实体接收格式时用的是 `string`，所以把格式以字符串形式写进包，HDL 那边就能直接 `generic map(Fmt_g => MyFmt_c)` 喂给实体，省去运行期再把 `FixFormat_t` 转 string 的麻烦。对 Verilog 更是必须——Verilog 根本没有 `FixFormat` 类型。

#### 4.2.2 核心流程

类型如何映射到 HDL，由两个查表常量决定：

```python
_VHDL_TYPES    = {int:"integer", float:"real", FixFormat:"FixFormat_t", str:"string"}
_VERILOG_TYPES = {int:"int",     float:"real", str:"string", FixFormat:"NOT-SUPPORTED"}
```

注意 Verilog 表里 `FixFormat` 是 `"NOT-SUPPORTED"`——这会在 `_verilog_const_declaration` 里被撞上并抛 `ValueError`（详见 4.3.3）。两条流水线如下：

```
add_constant/add_vector
   │  _check_name(name)   ← 校验合法标识符 + 查重
   │  类型白名单校验        ← 只允许 int/float/FixFormat(/str)
   ▼
存入 dict: name -> MemberData(type, value, as_string)
```

校验失败会立刻抛 `ValueError`，**不会**等到写文件时才报错——这是 fail-fast 设计。

#### 4.2.3 源码精读

`add_constant` 的实现很短：先 `_check_name`，再查类型白名单，最后存入字典（[olo_fix_pkg_writer.py:60-74](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/olo_fix/olo_fix_pkg_writer.py#L60-L74)）：

```python
def add_constant(self, name, type, value, as_string=False):
    self._check_name(name)
    if not type in [int, float, FixFormat, str]:
        raise ValueError(f"Type {type} is not supported. Only int, float, FixFormat and str are supported.")
    self._constants[name] = MemberData(type, value, as_string)
```

`add_vector` 几乎一样，只是类型白名单不含 `str`（向量不允许字符串元素，[olo_fix_pkg_writer.py:76-91](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/olo_fix/olo_fix_pkg_writer.py#L76-L91)）。

命名校验 `_check_name` 做两件事（[olo_fix_pkg_writer.py:137-147](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/olo_fix/olo_fix_pkg_writer.py#L137-L147)）：

1. **查重**：名字不能在 `_constants` 或 `_vectors` 里已存在。
2. **合法标识符**：必须是字母开头、由字母数字下划线组成（用 Python 的 `name.isidentifier()` 且 `name[0].isalpha()`）。注意这会拒绝 `_private` 这种以下划线开头的名字。

真实的生成示例可以直接看仓库自带的 `codegen.py`，它几乎把所有类型和 `as_string` 组合都演示了一遍（[codegen.py:21-37](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/codegen.py#L21-L37)）：

```python
pkg_writer.add_constant("ConstInt_c", int, 42)
pkg_writer.add_constant("ConstFloat_c", float, 3.14)
pkg_writer.add_constant("ConstFixFormat_c", FixFormat, FixFormat(1, 8, 8))
pkg_writer.add_constant("ConstString_c", str, "Hello")
pkg_writer.add_vector("VectorInt_c", int, [1, 2, 3])
...
pkg_writer.add_constant("ConstFixFormatAsString_c", FixFormat, FixFormat(1, 8, 8), as_string=True)
pkg_writer.add_vector("VectorFixFormatAsString_c", FixFormat, [FixFormat(1,8,8), FixFormat(1,16,16)], as_string=True)
```

这些常量随后被仓库的测试台逐一检查（见 4.4.3），所以它们就是「权威示例」。

#### 4.2.4 代码实践

**目标**：不写文件，直接观察 `add_constant` 不同组合会生成什么样的声明字符串。

**步骤**（在 `src/fix/python` 目录下）：

```python
from olo_fix import olo_fix_pkg_writer
from olo_fix_pkg_writer import MemberData   # 仅用于直接调私有声明函数演示
from en_cl_fix_pkg import *

w = olo_fix_pkg_writer()
# 原生 FixFormat
print(w._vhdl_const_declaration("AFmt_c", MemberData(FixFormat, FixFormat(1,8,8), False)))
# 字符串 FixFormat（喂给 olo_fix 实体的推荐写法）
print(w._vhdl_const_declaration("AFmtStr_c", MemberData(FixFormat, FixFormat(1,8,8), True)))
```

> 说明：这里为演示直接调用了私有的 `_vhdl_const_declaration`（[olo_fix_pkg_writer.py:149](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/olo_fix/olo_fix_pkg_writer.py#L149)）。正式使用时只应调公开方法 `add_constant` + `write_vhdl_pkg`。

**需要观察的现象**：第一行输出 `constant AFmt_c : FixFormat_t := (1, 8, 8);`，第二行输出 `constant AFmtStr_c : string := "(1, 8, 8)";`。

**预期结果**：与上述完全一致（与单元测试 [test_olo_fix_pkg_writer.py:56-67](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/tests/test_olo_fix_pkg_writer.py#L56-L67) 的断言相同）。若 `en_cl_fix` 未检出，导入会失败，则「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：同一个 `FixFormat(1,8,8)`，在 Verilog 里 `as_string=False` 会发生什么？

**答案**：抛 `ValueError: FixFormat type is not supported for Verilog constants`（[olo_fix_pkg_writer.py:184-187](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/olo_fix/olo_fix_pkg_writer.py#L184-L187)）。Verilog 必须用 `as_string=True`，把它写成 `localparam string AFmt_c = "(1, 8, 8)";`。

**练习 2**：`add_vector(..., type=str, ...)` 会被接受吗？

**答案**：不会。向量类型白名单只含 `int/float/FixFormat`（[olo_fix_pkg_writer.py:88-89](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/olo_fix/olo_fix_pkg_writer.py#L88-L89)），传 `str` 会抛 `ValueError`。

---

### 4.3 VHDL/Verilog 模板

#### 4.3.1 概念说明

writer 用 **Jinja2 模板**把数据渲染成代码，而不是在 Python 里手写字符串拼接。模板是「带占位符的骨架文件」：`{{ pkg_name }}` 是变量替换，`{% for c in constants %} ... {% endfor %}` 是循环。这样做的好处是：HDL 的整体结构（库声明、包头、包体）写在模板里，Python 只负责喂「每一行声明」。

VHDL 与 Verilog 各有一份模板，结构对仗：

- VHDL 模板生成 `package ... end package;`，包体为空（常量在包头的声明里直接赋值，无需包体）。
- Verilog 模板生成 `package ... endpackage`，外加 `` `ifndef / `define `` 头文件保护。

#### 4.3.2 核心流程

渲染流程是「声明生成 + 模板套用」两步：

```
对每个常量/向量 ──► _vhdl_const_declaration()  → "constant Name : <T> := <V>;"
                                       _verilog_const_declaration() → "localparam <T> Name = <V>;"

把所有声明行收集成 list ──► 填入模板的 constants / vectors 占位
                          ──► Jinja2 render ──► 写文件
```

类型→字符串的转换与 `as_string` 改写都发生在这些私有声明函数里（4.2 已看类型表，这里看拼接）。

#### 4.3.3 源码精读

**VHDL 模板**（[olo_fix_pkg_writer_vhdl.template:1-33](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/olo_fix/templates/olo_fix_pkg_writer_vhdl.template)）的骨架是：

```vhdl
library {{olo_library}};
    use {{olo_library}}.olo_base_pkg_array.all;
    use {{olo_library}}.en_cl_fix_pkg.all;

package {{pkg_name}} is
    -- Constants
{% for constant in constants %}
    {{constant}}
{% endfor %}
    -- Vectors
{% for vector in vectors %}
    {{vector}}
{% endfor %}
end package;

package body {{pkg_name}} is
end package body;
```

要点：

- 生成包会 `use <olo_library>.olo_base_pkg_array.all`——因为 `IntegerArray_t` / `RealArray_t` / `FixFormatArray_t` 这些数组类型定义在那里（见 u2-l1）。所以 `olo_library` 必须指向编译了 Open Logic 的库名（默认 `olo`）。
- 包体为空：所有常量都在包头声明处直接初始化（VHDL 允许 deferred 常量，但这里直接赋值更简单）。

**Verilog 模板**（[olo_fix_pkg_writer_verilog.template:1-20](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/olo_fix/templates/olo_fix_pkg_writer_verilog.template)）多了头文件保护：

```verilog
`ifndef {{pkg_name}}_SVH
`define {{pkg_name}}_SVH
package {{pkg_name}};
    // Constants
    {% for constant in constants %}{{constant}}{% endfor %}
    // Vectors
    {% for vector in vectors %}{{vector}}{% endfor %}
endpackage : {{pkg_name}}
`endif
```

声明行由私有函数生成。VHDL 常量声明 `_vhdl_const_declaration`（[olo_fix_pkg_writer.py:149-169](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/olo_fix/olo_fix_pkg_writer.py#L149-L169)）的核心是查类型表 + `as_string` 改写：

```python
type_str = self._VHDL_TYPES[member_data.type]
value_str = str(member_data.value)
...
if member_data.as_string:                       # as_string 把类型强制改成 string
    type_str = self._VHDL_TYPES[str]
    if member_data.type is not str:
        value_str = f'"{value_str}"'            # 并给值套上双引号
return f"constant {name} : {type_str} := {value_str};"
```

Verilog 常量声明 `_verilog_const_declaration`（[olo_fix_pkg_writer.py:171-199](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/olo_fix/olo_fix_pkg_writer.py#L171-L199)）多一道关卡：原生 `FixFormat` 直接抛错：

```python
elif member_data.type is FixFormat:
    if not member_data.as_string:
        raise ValueError("FixFormat type is not supported for Verilog constants")
```

向量声明 `_vhdl_vector_declaration`（[olo_fix_pkg_writer.py:201-228](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/olo_fix/olo_fix_pkg_writer.py#L201-L228)）按元素类型选 `IntegerArray_t / RealArray_t / FixFormatArray_t`，并带上范围 `(0 to N-1)`：

```python
range = f"(0 to {len(member_data.value)-1})"
return f"constant {name} : {type_str}{range} := ({value_str});"
```

浮点数格式化由 `_float_str`（[olo_fix_pkg_writer.py:259-265](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/olo_fix/olo_fix_pkg_writer.py#L259-L265)）统一处理：整数值的浮点（如 `3.0`）输出一位小数，否则保留 9 位有效数字：

```python
return f"{value:.1f}" if value == int(value) else f"{value:.9g}"
```

#### 4.3.4 代码实践

**目标**：亲手渲染一份 VHDL 包并查看结果，验证模板与声明函数协作正确。

**步骤**（在 `src/fix/python` 目录下，写到一个临时目录）：

```python
import tempfile, os
from olo_fix import olo_fix_pkg_writer
from en_cl_fix_pkg import *

w = olo_fix_pkg_writer()
w.add_constant("Gain_c", FixFormat, FixFormat(1, 8, 23))
w.add_constant("GainStr_c", FixFormat, FixFormat(1, 8, 23), as_string=True)
w.add_vector("Coefs_c", int, [1, 2, 3, 4])

d = tempfile.mkdtemp()
w.write_vhdl_pkg("my_gain_pkg", d, olo_library="olo")
print(open(os.path.join(d, "my_gain_pkg.vhd")).read())
```

**需要观察的现象**：输出的包里 `Gain_c` 是 `FixFormat_t := (1, 8, 23);`，`GainStr_c` 是 `string := "(1, 8, 23)";`，`Coefs_c` 是 `IntegerArray_t(0 to 3) := (1, 2, 3, 4);`，且顶部 `library olo; use olo.olo_base_pkg_array.all;`。

**预期结果**：与上述一致（对照官方文档示例 [olo_fix_pkg_writer.md:36-43](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/fix/olo_fix_pkg_writer.md#L36-L43)）。若 `en_cl_fix` 未检出，则「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：VHDL 模板里 `package body` 是空的，为什么生成包还能用？

**答案**：因为所有常量都在 `package ... is` 包头里**声明并直接赋值**（如 `constant X : integer := 42;`）。VHDL 允许在包头直接初始化常量，不需要在包体里再写。包体仅为语法完整性而保留。

**练习 2**：`_float_str(3.0)` 和 `_float_str(3.14)` 分别返回什么？

**答案**：`3.0` 返回 `"3.0"`（整数值浮点用 `:.1f`），`3.14` 返回 `"3.14"`（用 `:.9g`，9 位有效数字，去掉尾随零）。见 [olo_fix_pkg_writer.py:265](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/python/olo_fix/olo_fix_pkg_writer.py#L265)。

---

### 4.4 codegen 调用时机

#### 4.4.1 概念说明

writer 是个库，真正「在仿真流程里被调用」的是 [sim/codegen.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/codegen.py)。它实例化一个 writer、加好示例成员、调用 `write_vhdl_pkg` 把测试用包写到测试目录。`codegen.py` 既可以被 `sim/run.py` 自动调用（每次仿真前），也可以单独 `python codegen.py` 手动跑。

最关键的一条时序约束：**代码生成必须发生在 VUnit 扫描源文件之前**。因为 VUnit 在启动时会 `glob` 所有 `.vhd` 文件并建立编译图，而生成的包此刻还不存在——如果 VUnit 先扫描，testbench 里 `use olo_tb.pkg_writer_test_pkg.all;` 就会找不到包，编译失败。

#### 4.4.2 核心流程

仿真启动的时序如下：

```
sim/run.py 执行
   │
   ├─ codegen_generate()            ← 第 19 行，最先执行
   │      └─ codegen.py: generate()
   │           └─ writer.write_vhdl_pkg("pkg_writer_test_pkg", "../test/fix/olo_fix_pkg_writer")
   │                └─ 产出 pkg_writer_test_pkg.vhd   ← 文件现在存在了
   │
   ├─ vu.add_library('olo' / 'olo_tb')
   ├─ olo_tb.add_source_files(glob('../test/**/*.vhd'))   ← 此时才扫描，能扫到生成的包
   │
   └─ vu.main()  → 编译 → 仿真
```

#### 4.4.3 源码精读

`run.py` 在文件最顶部、任何 VUnit 调用之前，就执行了代码生成（[run.py:12-19](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L12-L19)）：

```python
from codegen import generate as codegen_generate
...
# Code-generator tests must generate code before VUnit detects files because the files must be present for VUnit
# to detect them.
codegen_generate()
```

注释把原因说得非常明白：生成的文件必须先存在，VUnit 才能检测到。随后才是建库与扫描（[run.py:113-127](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L113-L127)）：

```python
olo    = vu.add_library('olo')
olo_tb = vu.add_library('olo_tb')
...
files = glob('../test/**/*.vhd', recursive=True)
olo_tb.add_source_files(files)     # 生成的包在这里被扫进 olo_tb 库
```

`codegen.py` 的 `generate()` 本身很直白：实例化 writer、加成员、写文件（[codegen.py:16-39](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/codegen.py#L16-L39)）：

```python
def generate():
    os.chdir(os.path.dirname(os.path.realpath(__file__)))
    pkg_writer = olo_fix_pkg_writer()
    pkg_writer.add_constant("ConstInt_c", int, 42)
    ...
    pkg_writer.write_vhdl_pkg("pkg_writer_test_pkg", "../test/fix/olo_fix_pkg_writer", olo_library="olo")
```

注意它把包写到 `../test/fix/olo_fix_pkg_writer/`，那里正是对应 testbench 所在目录；该目录的 `.gitignore` 用 `*_pkg.vhd` 规则忽略生成文件（[test/fix/olo_fix_pkg_writer/.gitignore](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_pkg_writer/.gitignore)），所以生成物不入库——它是「构建产物」，每次由 codegen 重新生成。

消费端 testbench 把生成包 `use` 进来并逐项断言（[olo_fix_pkg_writer_tb.vhd:17-22](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_pkg_writer/olo_fix_pkg_writer_tb.vhd#L17-L22)）：

```vhdl
library olo_tb;
    use olo_tb.pkg_writer_test_pkg.all;
```

它在注释里再次强调了同一时序（[olo_fix_pkg_writer_tb.vhd:52](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_pkg_writer/olo_fix_pkg_writer_tb.vhd#L52)）：

```vhdl
-- Code is generated through <root>/sim/codegen.py, which is executed before VUnit detects files.
```

测试用例分四组：`nativeConstants`（原生标量）、`nativeVectors`（原生向量）、`stringConstants`（字符串标量）、`stringVectors`（字符串向量），逐个 `check_equal`（[olo_fix_pkg_writer_tb.vhd:55-103](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_pkg_writer/olo_fix_pkg_writer_tb.vhd#L55-L103)）。例如字符串向量的断言：

```vhdl
elsif run("stringVectors") then
    check_equal(VectorIntAsString_c, "1, 2, 3", "vectorIntAsString wrong");
    check_equal(VectorFixFormatAsString_c, "(1, 8, 8), (1, 16, 16)", "vectorFixFormatAsString wrong");
```

注意：这个 TB 在 [olo_fix.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_fix.py) 里**没有显式配置**，靠 VUnit 自动发现（`glob('../test/**/*.vhd')` 扫到 `*_tb.vhd` 即注册为测试台），用默认 `runner_cfg` generic 跑全部 `run(...)` 用例。

#### 4.4.4 代码实践

**目标**：手动触发一次代码生成，确认生成物出现在测试目录。

**步骤**：

1. 在 `sim/` 目录执行：

   ```bash
   cd sim
   python codegen.py
   ```

2. 检查生成文件：

   ```bash
   ls -l ../test/fix/olo_fix_pkg_writer/pkg_writer_test_pkg.vhd
   head -25 ../test/fix/olo_fix_pkg_writer/pkg_writer_test_pkg.vhd
   ```

3. 确认它**不会**出现在 `git status` 里（被 `.gitignore` 的 `*_pkg.vhd` 忽略）。

**需要观察的现象**：文件被（重新）生成；内容包含 `package pkg_writer_test_pkg is` 及 `constant ConstInt_c : integer := 42;` 等行；`git status` 干净（不显示该文件）。

**预期结果**：文件存在且内容正确；`git status` 不显示它。该 TB **未在** `olo_fix.py` 的 `add_configs` 里登记，因此 VUnit 自动发现它并运行全部 4 个用例。若 `en_cl_fix` 子模块未检出，codegen 因 `from en_cl_fix_pkg import *` 失败——则「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `run.py` 里的 `codegen_generate()` 删掉，直接 `python run.py`，会在哪一步报错？

**答案**：在 VUnit 扫描 + 编译阶段。TB 里 `use olo_tb.pkg_writer_test_pkg.all;` 找不到包（文件没被生成），编译失败。这正说明 codegen 必须先于文件扫描。

**练习 2**：为什么生成包被 `.gitignore` 忽略（`*_pkg.vhd`），而不是入库？

**答案**：因为它是**构建产物**，完全可由 `codegen.py` 从 Python 定义重建。入库会造成「同一份信息两处维护」（Python 定义 + 提交的 .vhd），违背单一真相源，还可能在定义改动后忘记重新生成、导致入库文件与 Python 不一致。

---

## 5. 综合实践

把四个模块串起来：**修改 Python 定义 → 重新生成 → 在仿真里引用生成常量并验证**。这正是「Python 单一真相源」的完整闭环。

**任务**：给 `sim/codegen.py` 增加一个 `FixFormat` 常量和一个整数向量，重新生成包，再在现有 TB 里加一个用例引用它们。

**步骤**：

1. **改 Python 定义**（仅作练习，不要提交）。在 [codegen.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/codegen.py) 的 `generate()` 里、`write_vhdl_pkg` 之前追加：

   ```python
   pkg_writer.add_constant("MyFmt_c", FixFormat, FixFormat(1, 5, 10))
   pkg_writer.add_vector("MyCoefs_c", int, [7, 8, 9])
   ```

2. **重新生成**：`cd sim && python codegen.py`，确认 `pkg_writer_test_pkg.vhd` 里出现 `constant MyFmt_c : FixFormat_t := (1, 5, 10);` 与 `constant MyCoefs_c : IntegerArray_t(0 to 2) := (7, 8, 9);`。

3. **在 TB 里引用并验证**：在 [olo_fix_pkg_writer_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_pkg_writer/olo_fix_pkg_writer_tb.vhd) 的 `while test_suite` 循环里仿照已有用例加一段（参照第 62-66 行对 `ConstFixFormat_c` 的写法，需用 `-- vsg_off/-- vsg_on` 包裹 record 赋值以过 lint）：

   ```vhdl
   elsif run("myConstants") then
       -- vsg_off
       Format_v := (1, 5, 10);
       -- vsg_on
       check_equal(MyFmt_c.I, Format_v.I, "MyFmt I wrong");
       check_equal(MyCoefs_c'length, 3, "MyCoefs length wrong");
       check_equal(MyCoefs_c(0), 7, "MyCoefs(0) wrong");
   ```

4. **运行仿真**（在 `sim/` 目录）：

   ```bash
   python run.py --ghdl '*olo_fix_pkg_writer*'
   ```

**需要观察的现象**：新用例 `myConstants` 被发现并 PASS；`MyFmt_c` 的 `I=5`、`MyCoefs_c` 长度为 3 且首元素为 7，全部与 Python 定义一致。

**预期结果**：仿真全部通过，证明「Python 里写的值」经代码生成后被 HDL 原样读回——单一真相源闭环成立。若 `en_cl_fix` 子模块或仿真器未就绪，步骤 1-2 的 Python 部分仍可独立验证（`python codegen.py`），仿真部分「待本地验证」。

> 进阶：参照官方教程 [OloFixTutorial.md:390-398](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/tutorials/OloFixTutorial.md#L390-L398)（VHDL）与 [:855-866](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/tutorials/OloFixTutorial.md#L855-L866)（Verilog），尝试同时生成 `.vhd` 与 `.vh`，体会 Verilog 必须 `as_string=True`、且需另存位宽常量（`cl_fix_width`）供信号声明使用的差异。

## 6. 本讲小结

- `olo_fix_pkg_writer` 是纯 Python 代码生成器，把 Python 里的常量/向量/定点格式渲染成 VHDL 包或 Verilog 头文件，解决「Python 算法模型 → HDL 参数」的一致性传递问题。
- 它是「累加 → 渲染」的有状态对象：`add_constant` / `add_vector` 累加成员到内部字典（含命名查重与类型白名单），`write_vhdl_pkg` / `write_verilog_header` 用 Jinja2 模板渲染成文件。
- `as_string` 开关决定按原生类型还是字符串生成；对喂给 `olo_fix` 实体的格式推荐 `as_string=True`，对 Verilog 则是**强制**（Verilog 无 `FixFormat` 类型，原生写法会抛错）。
- VHDL 模板会 `use <olo_library>.olo_base_pkg_array.all`（数组类型来源）与 `en_cl_fix_pkg.all`；浮点用 `_float_str` 统一为「整数小数 1 位 / 否则 9 位有效数字」。
- 时序红线：`sim/run.py` 必须在 VUnit 扫描源文件**之前**调用 `codegen_generate()`，否则生成的包还未存在、TB 编译失败；生成包被 `.gitignore` 的 `*_pkg.vhd` 忽略，是可重建的构建产物。
- 官方验证闭环：`codegen.py` 生成 `pkg_writer_test_pkg` → 自动发现的 `olo_fix_pkg_writer_tb` 用四组用例（native/string × constant/vector）逐项 `check_equal` 验证。

## 7. 下一步学习建议

- **下一讲 u8-l5（协同仿真）**：本讲的 writer 只生成「常量定义」，下一讲进入 `olo_fix_cosim` / `olo_fix_sim_stimuli` / `olo_fix_sim_checker`，讲解如何生成**数据级**的协仿真文件，用 Python 位真模型逐拍比对 HDL 输出。两者合起来构成完整的「Python 单一真相源 + 位真验证」体系。
- **延伸阅读**：通读 [doc/tutorials/OloFixTutorial.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/tutorials/OloFixTutorial.md)，它给出了一个端到端的「Python 建模 → writer 生成格式包 → HDL 实现」的真实工程样例（Step 6 与 Appendix C）。
- **回到第 9 单元**：第 9 单元的 FIR/CIC 等高级实体的系数与格式正适合用本讲工具从 Python 生成；学完 u8-l5 后再读 `olo_fix_coef_storage`（u9-l4）会看到系数下发的另一条路径。
