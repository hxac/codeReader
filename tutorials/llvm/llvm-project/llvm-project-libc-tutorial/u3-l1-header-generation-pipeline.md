# 头文件生成管线：YAML 规范 + hdrgen + .h.def

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 LLVM-libc 的**公共头文件不是手写的，而是生成出来的**这件事，以及为什么要这么做。
- 读懂 `include/*.yaml` 这种以机器可读方式描述一个头文件（函数、宏、类型、枚举、对象）的「规范文件」。
- 描述 `utils/hdrgen` 这套 Python 工具链的工作流程：把 YAML **反序列化**成类对象，再把类对象**重新序列化**成 C 声明字符串。
- 看懂 `.h.def` 模板文件的作用：它承载手写内容，并通过 `%%public_api()` 占位符与生成内容拼装成最终头文件。
- 理解 CMake 是如何把 hdrgen 接进构建系统的（`add_gen_header` 规则调用 `main.py`）。

## 2. 前置知识

本讲承接 **u2-l1 入口点（entrypoint）机制**。那里我们建立了这样一个认知：LLVM-libc 把「实现」「构建」「平台取舍」三件事解耦。本讲聚焦其中一根隐藏的支柱——**公共头文件本身也是被生成出来的**。

如果你还没读过 u2-l1，至少先记住一个结论：每个公开函数都有一份「五件套」（yaml 规范、内部头、cpp 实现、CMake 注册、单元测试）。本讲专门拆解其中的**第一件——yaml 规范**，以及它如何变成用户 `#include` 的那个公共头文件。

需要用到的基础概念：

- **公共头文件（public header）**：用户代码 `#include <ctype.h>` 实际包含的那个文件，里面是 `int isalpha(int);` 这样的标准 C 声明。
- **YAML**：一种用缩进表示层级、对人类和程序都友好的数据格式。本讲你会看到大量 YAML。
- **反序列化 / 序列化**：把文本（YAML）读成程序里的对象叫反序列化；把对象重新写成文本（C 声明）叫序列化。hdrgen 的本质就是「YAML → 对象 → C」。
- **模板 + 占位符**：一个带「留空位置」的骨架文件，留空处用一个标记（本讲是 `%%public_api()`）占位，生成时把标记替换成实际内容。

一个关键的直觉：**为什么不用手写头文件？** 因为头文件里的函数签名，既是「用户该看到什么」的规范，又是「构建系统该生成哪些入口点」的依据，还是「文档」的来源。把同一份信息写三遍（手写头文件、写 entrypoints 配置、写文档）一定会三处不一致。LLVM-libc 的选择是：**让 YAML 成为唯一的「事实来源（single source of truth）」**，头文件、构建配置、文档都从它派生。这正是本讲要讲清楚的生成管线。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| `docs/dev/header_generation.md` | 官方说明文档，描述三大组件与使用方式 |
| `include/ctype.yaml` | 一个真实头文件（ctype.h）的 YAML 规范，函数族的范本 |
| `include/errno.yaml` + `include/errno.h.def` | 自定义模板的范例：YAML + 一个 `.h.def` 模板 |
| `utils/hdrgen/yaml_to_classes.py`（薄入口） | 命令行入口之一，实际转发到 `hdrgen` 包 |
| `utils/hdrgen/hdrgen/yaml_to_classes.py`（真模块） | **核心逻辑**：YAML → 类对象，含 `--add_function` 实现 |
| `utils/hdrgen/hdrgen/header.py` | `HeaderFile` 类：序列化成头文件字符串、模板与代理头 |
| `utils/hdrgen/hdrgen/function.py` | `Function` 类：把一个函数渲染成 C 声明 |
| `utils/hdrgen/hdrgen/main.py` | **真正的命令行入口**：编排「加载 YAML → 模板 → 写文件」 |
| `utils/hdrgen/tests/` | 集成测试：固定输入 YAML，比对生成的头文件 |
| `cmake/modules/LLVMLibCHeaderRules.cmake` | `add_gen_header` CMake 规则：调用 hdrgen |
| `include/CMakeLists.txt` | 用 `add_header_macro` 把每个头文件接到生成规则上 |

> 注意一个容易踩的坑：仓库里有两个 `yaml_to_classes.py` 和两个 `main.py`。
> - 仓库根层的 `utils/hdrgen/yaml_to_classes.py`、`utils/hdrgen/main.py` 都是**只有十几行的薄入口**，它们 `from hdrgen.main import main` 后直接调用。
> - 真正的逻辑在 **`utils/hdrgen/hdrgen/` 包**里。本讲引用「yaml_to_classes」或「main」的源码时，默认指包内那个真正的实现文件。

---

## 4. 核心概念与源码讲解

官方文档把头文件生成归纳为三大组件，本讲据此拆成四个最小模块，最后一个模块讲构建集成：

> 1. **YAML 文件**：按头文件和标准拆分，承载全部函数头信息。
> 2. **类对象**：为函数头的每个组成部分（宏、枚举、类型、函数、参数、对象）建一个类。
> 3. **Python 脚本**：用类对象把 YAML 反序列化、再重新序列化成函数头，并与 `.h.def` 模板、额外的宏/类型包含拼装。

参见 [docs/dev/header_generation.md:L5-L13](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/dev/header_generation.md#L5-L13)（中文：官方对三大组件的原文描述）。

---

### 4.1 YAML 规范：用机器可读的方式描述一个头文件

#### 4.1.1 概念说明

`include/` 目录下有 **55 个 `.yaml` 文件**，每个对应一个标准头文件（`ctype.yaml` ↔ `ctype.h`、`errno.yaml` ↔ `errno.h`，依此类推）。一个 YAML 文件就是「这个头文件里到底有哪些公开内容」的完整、机器可读描述。

它描述五类内容，恰好对应 C 头文件里的五类元素：

1. **functions**：函数。这是最主要的。每个函数有名字、返回类型、参数列表、所属标准、可选的 guard（条件编译宏）和 attributes。
2. **macros**：宏（如 `NULL`）。
3. **types**：类型（如 `size_t`、`errno_t`）。
4. **enums**：枚举常量。
5. **objects**：全局对象（如 `extern` 变量）。

为什么用 YAML 而不是直接写 C？因为 YAML 是**结构化数据**，程序可以可靠地读出「isalpha 接受一个 int、返回 int」，从而同时驱动「生成 `int isalpha(int);` 声明」「把它注册成入口点」「检查它属于哪个标准」三件事。手写 C 头文件则要靠解析 C 语法才能得到这些信息，既脆弱又重复。

#### 4.1.2 核心流程

一个 YAML 文件的骨架（以 `ctype.yaml` 为例）：

```text
header: ctype.h            # 生成哪个头文件
standards: [stdc]          # 整个头文件遵循的标准
enums: []                  # 枚举（ctype 没有）
objects: []                # 对象（ctype 没有）
functions:                 # 函数列表
  - name: isalpha
    standards: [stdc]
    return_type: int
    arguments:
      - type: int
  ... 更多函数 ...
```

一个函数条目的字段到 C 声明的映射关系（这是本讲最需要记住的一张表）：

| YAML 字段 | 含义 | 在 C 声明里的位置 |
|-----------|------|-------------------|
| `name` | 函数名 | 标识符：`isalpha` |
| `return_type` | 返回类型 | 最左侧：`int` |
| `arguments[].type` | 各参数类型 | 括号内逗号分隔：`(int)` |
| `standards` | 所属标准（stdc/posix/gnu…） | 不直接出现在声明里，用于分类注释与裁剪 |
| `guard` | 条件编译宏 | 包裹成 `#ifdef <guard> ... #endif` |
| `attributes` | 属性宏 | 声明最前面，如 `_Noreturn` |

于是 `isalpha` 的 YAML 条目就会渲染成 `int isalpha(int) __NOEXCEPT;`（`__NOEXCEPT` 由生成器统一追加，见 4.2）。

#### 4.1.3 源码精读

先看一个最简单的函数条目——`isalpha`：

[include/ctype.yaml:L13-L18](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/include/ctype.yaml#L13-L18)

```yaml
  - name: isalpha
    standards:
      - stdc
    return_type: int
    arguments:
      - type: int
```

中文：这是 `functions` 列表下的一个条目。`name`/`return_type`/`arguments` 三项直接决定了 C 声明 `int isalpha(int)`；`standards: [stdc]` 标明它属于「标准 C」。

再看一个**多参数**的条目——`isalnum_l`（带 locale 版本），它的 `arguments` 有两个元素：

[include/ctype.yaml:L103-L109](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/include/ctype.yaml#L103-L109)

```yaml
  - name: isalnum_l
    standards:
      - posix
    return_type: int
    arguments:
      - type: int
      - type: locale_t
```

中文：两个参数 `int` 和 `locale_t` 会渲染成 `int isalnum_l(int, locale_t)`。注意它属于 `posix` 而非 `stdc`——这正是 u2-l4 讲过的「Overlay 模式会把 locale 系函数挡在外面」的依据，因为 Overlay 模式通常只暴露 stdc 子集。

最后看 YAML 的顶层结构：

[include/ctype.yaml:L1-L5](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/include/ctype.yaml#L1-L5)

```yaml
header: ctype.h
standards:
  - stdc
enums: []
objects: []
```

中文：`header` 字段是必填项，决定生成的头文件名；`enums: []` 和 `objects: []` 用空列表显式声明「ctype 没有这两类内容」。

#### 4.1.4 代码实践

**实践目标**：动手把「YAML 字段」和「C 声明」对应起来，建立直觉。

**操作步骤**：

1. 打开 `include/ctype.yaml`，定位 `isalpha`、`isdigit`、`isalnum_l` 三个函数条目。
2. 对每个函数，在纸上写出你预期的 C 声明（先别看答案）。
3. 在 `include/ctype.yaml` 里数一数：哪些函数属于 `stdc`，哪些属于 `posix`，哪些属于 `gnu`。

**预期结果**（待本地核对）：

| 函数 | 你写出的 C 声明 | 标准 |
|------|-----------------|------|
| isalpha | `int isalpha(int)` | stdc |
| isdigit | `int isdigit(int)` | stdc |
| isalnum_l | `int isalnum_l(int, locale_t)` | posix |

如果你写对了这三行，说明你已经掌握了 YAML 规范的核心。注意 `__NOEXCEPT` 和末尾分号是生成器加的，**YAML 里并不写**。

#### 4.1.5 小练习与答案

**练习 1**：如果要在 `ctype.yaml` 里描述一个「接受 `void`、返回 `void`、名为 `foo`」的函数，`arguments` 该怎么写？

**答案**：`arguments` 写成空列表 `arguments: []`。生成器会把空参数列表渲染成 `(void)` 而不是 `()`（详见 4.2 的 `Function.__str__`），这是 C 中「无参数」的正确写法。

**练习 2**：`standards` 字段会出现在最终生成的 C 声明里吗？

**答案**：不会直接出现在单条声明里。它的作用是：①决定该函数在头文件顶部注释里被归到哪一类（Standard C / POSIX / GNU…）；②在构建配置层用于按标准裁剪（如 Overlay 模式只选 stdc）。

---

### 4.2 hdrgen Python 工具：从 YAML 到类对象再到字符串

#### 4.2.1 概念说明

光有 YAML 还不能变成头文件——需要一段程序读它、理解它、再吐出 C 代码。这段程序就是 `utils/hdrgen/` 下的 Python 工具链，社区里叫它 **hdrgen**（header generator）。

hdrgen 的设计是经典的「**中间表示（IR）**」思路：它不直接把 YAML 文本翻译成 C 文本，而是中间引入一组**类对象**作为中间表示。这样做有两个好处：

- **解耦**：「读 YAML」和「写 C」是两个独立步骤，可以分别测试和修改。
- **类型安全**：每个函数被建模成一个 `Function` 对象，每个头文件被建模成一个 `HeaderFile` 对象，程序操作的是有结构的对象而不是裸字符串，避免字符串拼接的错误。

为 C 头文件的每个组成部分都建了一个类，集中在 `utils/hdrgen/hdrgen/` 包里：

| 类（文件） | 模型化的事物 |
|------------|--------------|
| `HeaderFile`（header.py） | 整个头文件 |
| `Function`（function.py） | 一个函数（继承自 `Symbol`） |
| `Macro`（macro.py） | 一个宏 |
| `Type`（type.py） | 一个类型 |
| `Enumeration`（enumeration.py） | 一个枚举 |
| `Object`（object.py） | 一个全局对象 |

#### 4.2.2 核心流程

整条管线分四步，可以用下面这个伪流程描述：

```text
[YAML 文件]
   │  yaml.safe_load        # PyYAML 把文本解析成 Python dict/list
   ▼
[Python dict]
   │  yaml_to_classes()     # 把 dict 翻译成 HeaderFile + 一堆 Function/Type/...
   ▼
[HeaderFile 对象]
   │  str(header)           # 等价于调用 public_api()，把对象序列化成 C 声明字符串
   ▼
[生成的 C 片段字符串]
   │  fill_public_api()     # 把片段填进模板的 %%public_api() 占位符
   ▼
[最终的头文件]
```

其中最关键的两段逻辑：

1. **`yaml_to_classes()`**：遍历 YAML 里的 macros/types/enums/functions/objects，为每一项 new 出对应的类对象，`add_*` 进 `HeaderFile`。注意它会对函数按名字**排序**后再加，保证生成结果稳定（不依赖 YAML 里的书写顺序）。
2. **`Function.__str__()`**：决定一个函数对象如何变成 C 声明。这是「YAML 字段 → C 文本」映射的最终落点。

#### 4.2.3 源码精读

**（1）`yaml_to_classes` 的函数处理循环**

[utils/hdrgen/hdrgen/yaml_to_classes.py:L78-L100](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/utils/hdrgen/hdrgen/yaml_to_classes.py#L78-L100)

```python
    functions = yaml_data.get("functions", [])
    if entry_points:
        entry_points_set = set(entry_points)
        functions = [f for f in functions if f["name"] in entry_points_set]
    sorted_functions = sorted(functions, key=lambda x: x["name"])
    ...
    for function_data in sorted_functions:
        guard = function_data.get("guard", None)
        if guard is None:
            arguments = [arg["type"] for arg in function_data["arguments"]]
            ...
            header.add_function(
                Function(
                    function_data["return_type"],
                    function_data["name"],
                    arguments,
                    standards,
                    guard,
                    attributes,
                )
            )
```

中文：这段做了三件事——①如果传入了 `entry_points`（构建时只想要部分函数），就用名字做白名单过滤；②按 `name` 排序，保证生成稳定；③为每个函数构造一个 `Function` 对象并加入 `HeaderFile`。注意 `arguments` 是从 `arg["type"]` 提取出来的——YAML 里每个参数是 `{type: int}` 这样的字典，这里只取它的 `type` 值。

**（2）`Function.__str__` ——字段如何变成 C 声明**

[utils/hdrgen/hdrgen/function.py:L71-L82](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/utils/hdrgen/hdrgen/function.py#L71-L82)

```python
    def __str__(self):
        attrs_str = "".join(f"{attr} " for attr in self.attributes)
        arguments_str = ", ".join(self.arguments) if self.arguments else "void"
        type_str = str(self.return_type)
        if type_str[-1].isalnum() or type_str[-1] == "_":
            type_str += " "
        return attrs_str + type_str + self.name + "(" + arguments_str + ")"
```

中文：这是字段到 C 文本的核心映射。`attributes` 拼到最前面（每个属性后加空格）；`arguments` 用逗号连接，**空列表则渲染成 `void`**（呼应练习 1）；返回类型若以字母/下划线结尾就补一个空格（避免 `intisalpha`，但 `int *` 类型的 `*` 后不补空格以保持规范风格）。最终拼成 `attrs + 类型 + 名字 + (参数)`。

所以 `isalpha`（return_type=`int`, name=`isalpha`, arguments=`[int]`）渲染成 `int isalpha(int)`。

**（3）`public_api` ——把整个 HeaderFile 序列化成 C 片段**

[utils/hdrgen/hdrgen/header.py:L324-L347](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/utils/hdrgen/hdrgen/header.py#L324-L347)

```python
    def public_api(self):
        content = (
            self.include_lines(self.template_file is None)
            + self.macro_lines()
            + self.enum_lines()
        )
        content.append("")
        has_decls = self.functions or self.objects
        if has_decls:
            content.append("__BEGIN_C_DECLS")
            content.append("")
        ...
        for function in sorted(self.functions):
            ...
            content.append(str(function) + " __NOEXCEPT;")
            content.append("")
```

中文：`public_api` 把「include 行 + 宏 + 枚举 + 函数声明 + 对象声明」按顺序拼成一段 C 片段。函数声明来自 `str(function)`（即上面的 `__str__`），再统一追加 ` __NOEXCEPT;`。整段用 `__BEGIN_C_DECLS` / `__END_C_DECLS` 包起来，保证 C++ 也能包含。这段返回的字符串就是要填进模板的内容（见 4.3）。

**（4）`main` ——真正的命令行入口编排**

[utils/hdrgen/hdrgen/main.py:L126-L133](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/utils/hdrgen/hdrgen/main.py#L126-L133)

```python
        [yaml_file] = args.yaml_file
        header = load_header(yaml_file)
        if args.proxy:
            contents = header.proxy_contents()
        else:
            # The header_template path is relative to the containing YAML file.
            template = header.template(yaml_file.parent, files_read)
            contents = fill_public_api(header.public_api(), template)
```

中文：这是默认（非 JSON、非代理）分支的编排：加载 YAML 成 `header` 对象 → 取模板（自定义 `.h.def` 或默认模板）→ `fill_public_api` 把 `public_api()` 的结果填进模板。这就把 4.2 和 4.3 串起来了。

#### 4.2.4 代码实践

**实践目标**：亲手跑一遍「YAML → 对象 → 字符串」，验证字段映射。

**操作步骤**（这是最可靠的源码阅读型实践，不依赖完整构建）：

1. 在仓库根目录（`libc/` 的上层）执行：
   ```bash
   cd libc && python3 -c "
   import sys; sys.path.insert(0, 'utils/hdrgen')
   from pathlib import Path
   from hdrgen.yaml_to_classes import load_yaml_file
   from hdrgen.header import HeaderFile
   h = load_yaml_file(Path('include/ctype.yaml'), HeaderFile, ['isalpha','isdigit'])
   # 找到 isalpha 这个 Function 对象，看它如何渲染
   for f in h.functions:
       print(repr(str(f)))   # 应输出 'int isalpha(int)' 之类
   "
   ```
2. 观察打印结果。

**预期结果**（待本地验证）：你会看到类似 `'int isalpha(int)'` 和 `'int isdigit(int)'` 的输出。这正是 `Function.__str__` 的产物，也是最终头文件里那一行声明（去掉 `__NOEXCEPT;`）。

**如果运行失败**：可能是 Python 版本或 PyYAML 未安装（文档要求 Python 3.8、PyYAML 5.1）。这种情况下，退化为纯阅读实践：对照 4.2.3 的源码，人工推演 `isalnum_l`（参数 `[int, locale_t]`）经过 `__str__` 后应得到 `int isalnum_l(int, locale_t)`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `yaml_to_classes` 要对函数 `sorted(functions, key=lambda x: x["name"])`？

**答案**：为了保证生成的头文件内容**稳定且可重复**。如果不排序，生成结果会依赖 YAML 里的书写顺序，一旦有人重排 YAML 行就会产生无意义的大 diff，也会让集成测试（逐字节比对）频繁失败。排序让「YAML 书写顺序」与「生成结果」解耦。

**练习 2**：`Function.__str__` 里 `arguments_str = ... if self.arguments else "void"`，为什么空参数要写成 `void` 而不是空括号 `()`？

**答案**：在 C 语言里，`f()` 表示「参数未指定」（向后兼容旧 K&R 风格），而 `f(void)` 才明确表示「不接受任何参数」。标准库头文件必须用 `f(void)`，所以生成器特意把空参数列表渲染成 `void`。

---

### 4.3 .h.def 模板：手写内容与生成内容的拼装

#### 4.3.1 概念说明

到目前为止，生成器能产出函数声明、include 行、宏、枚举。但有些头文件的内容**没法用 YAML 表达**，必须手写。最典型的例子是 `errno.h`：

- 它需要按平台 `#include <linux/errno.h>` 或 `<sys/errno.h>`。
- 它要手写一个 `int *__llvm_libc_errno(void);` 声明和 `#define errno (*__llvm_libc_errno())` 这种「函数伪装成变量」的技巧。

这些都不适合塞进结构化的 YAML。于是 hdrgen 提供了**模板文件 `.h.def`**：一个**手写的骨架**，里面用一个占位符 `%%public_api()` 标记「生成内容请填到这里」。

关键认知：

- `.h.def` 是**可选的**。仓库里 55 个 yaml 中**只有 5 个**（assert、errno、math、stdbit、stdfix）配了 `.h.def`。其余 50 个头文件用 hdrgen 内置的**默认模板**。
- 当 YAML 里有 `header_template: foo.h.def` 这一行时，用这个自定义模板；否则用默认模板。
- 无论哪种模板，都通过同一个 `%%public_api()` 占位符与生成内容拼装。

#### 4.3.2 核心流程

拼装由一个函数完成，逻辑非常简单——**字符串替换**：

```text
模板内容（含 %%public_api()）
   │
   │  h_def_content.replace("%%public_api()", 生成的C片段, 1)
   ▼
最终头文件（手写骨架 + 生成内容）
```

两种模板的取舍：

| | 默认模板（无 .h.def） | 自定义 .h.def 模板 |
|---|---|---|
| 适用 | 能完全用 YAML 描述的头文件（ctype、string…） | 含手写内容的头文件（errno、math…） |
| 谁写 `#include "__llvm-libc-common.h"` | 生成器自动加 | 模板作者手写 |
| 占位符 | `%%public_api()` | `%%public_api()`（同样） |
| YAML 触发条件 | 无 `header_template` 字段 | 有 `header_template: xxx.h.def` |

#### 4.3.3 源码精读

**（1）默认模板长什么样**

[utils/hdrgen/hdrgen/header.py:L60-L73](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/utils/hdrgen/hdrgen/header.py#L60-L73)

```python
HEADER_TEMPLATE = """\
//===-- {library} header <{header}> --===//
...
#ifndef {guard}
#define {guard}

%%public_api()

#endif // {guard}
"""
```

中文：默认模板只有头注释、include 守卫（`#ifndef/#define`）和一个 `%%public_api()` 占位符。`{library}`/`{header}`/`{guard}` 是 Python `str.format` 的占位符，在 `template()` 方法里填入。这个模板被 50 个头文件复用。

**（2）自定义模板范例：errno.h.def**

先看 YAML 怎么声明要用自定义模板：

[include/errno.yaml:L1-L2](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/include/errno.yaml#L1-L2)

```yaml
header: errno.h
header_template: errno.h.def
```

中文：`header_template` 字段告诉 hdrgen「不要用默认模板，用同目录下的 `errno.h.def`」。

再看模板本身的手写内容：

[include/errno.h.def:L14-L34](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/include/errno.h.def#L14-L34)

```c
#ifdef __linux__

#include <linux/errno.h>
...
#include "llvm-libc-macros/linux/error-number-macros.h"

#elif defined(__APPLE__)

#include <sys/errno.h>

#else // __APPLE__

#include "llvm-libc-macros/generic-error-number-macros.h"

#endif

%%public_api()
```

中文：这些都是**手写的、与平台相关的**包含逻辑（Linux / Apple / 其它各走不同分支），YAML 表达不了。注意倒数第二行的 `%%public_api()`——生成器会把 `errno.yaml` 里描述的内容（这里主要是 `errno_t` 类型）填到这里。再往下（L36-L42）还有手写的 `__llvm_libc_errno` 声明和 `#define errno`，它们在占位符**之外**，原样保留。

**（3）拼装函数 `fill_public_api`**

[utils/hdrgen/hdrgen/yaml_to_classes.py:L151-L163](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/utils/hdrgen/hdrgen/yaml_to_classes.py#L151-L163)

```python
def fill_public_api(header_str, h_def_content):
    header_str = header_str.strip()
    return h_def_content.replace("%%public_api()", header_str, 1)
```

中文：整个拼装就这么简单——把模板里**第一个** `%%public_api()` 替换成生成的 C 片段（去掉首尾空白）。`replace(..., 1)` 的 `1` 表示只替换第一处，避免误伤。

#### 4.3.4 代码实践

**实践目标**：对比「默认模板」与「自定义 .h.def」两条路径，理解何时需要 `.h.def`。

**操作步骤**：

1. 打开 `include/ctype.yaml`，确认它**没有** `header_template` 字段 → ctype.h 走默认模板。
2. 打开 `include/errno.yaml`，确认它**有** `header_template: errno.h.def` → errno.h 走自定义模板。
3. 打开 `include/errno.h.def`，找出其中：①平台相关的手写包含块；②`%%public_api()` 占位符；③占位符之外的手写声明。

**预期结果**（待本地核对）：

| 头文件 | 用模板？ | 必须手写的原因 |
|--------|----------|----------------|
| ctype.h | 默认模板 | 函数声明全可由 YAML 描述，无需手写 |
| errno.h | errno.h.def | 需要按平台包含系统头文件 + 手写 `errno` 宏技巧 |

**需要观察的现象**：在 `errno.h.def` 里，`#include "__llvm-libc-common.h"`（L12）是**作者手写**的；而在默认模板路径下，这一行是**生成器自动**加的（见 `public_api` 里的 `include_lines(self.template_file is None)`，`with_common=True` 时自动补 common 头）。这就是上表「谁写 common 头」一列的来源。

#### 4.3.5 小练习与答案

**练习 1**：假如一个新头文件既需要平台相关的手写 `#include`，又有几个能写进 YAML 的函数，该怎么做？

**答案**：写一个 `xxx.h.def` 模板，把平台相关内容手写在 `%%public_api()` 之前/之后，函数写进 `xxx.yaml` 的 `functions` 里，并在 YAML 顶部加 `header_template: xxx.h.def`。生成时函数声明会被填进占位符，手写内容原样保留。

**练习 2**：`fill_public_api` 为什么用 `replace(..., 1)` 只替换第一处，而不是替换全部？

**答案**：占位符 `%%public_api()` 在一个模板里只应出现一次；用 `1` 是一种防御性写法——万一模板里意外出现了第二个同名标记，也不会被错误地替换成生成内容，保证只在一个预期位置注入。

---

### 4.4 生成产物与构建集成：CMake 如何调用 hdrgen

#### 4.4.1 概念说明

hdrgen 是一个 Python 脚本，但它不能自己跑起来——它必须被构建系统在正确的时机、用正确的参数调用，把生成的头文件放进构建目录。这一节讲 **CMake 如何把 hdrgen 接进构建**，也就是「生成产物」是怎么真正出现在 `build/.../include/` 里的。

这里有两个关键概念：

- **`add_gen_header` 规则**（在 `cmake/modules/LLVMLibCHeaderRules.cmake`）：定义「如何从一个 YAML 生成一个头文件」的 CMake 函数，内部用一个 `add_custom_command` 调用 `main.py`。
- **入口点过滤**：回顾 u2-l4，每个平台有一个 `entrypoints.txt` 决定支持哪些函数。生成头文件时也会用同一份名单——只生成「本平台真正支持的」那些函数声明。这通过 `--entry-point` 参数传给 hdrgen。

#### 4.4.2 核心流程

```text
include/CMakeLists.txt 里写：
  add_header_macro(ctype  ../libc/include/ctype.yaml  ctype.h  DEPENDS ...)
        │
        ▼ （宏展开成）
  add_gen_header(ctype  YAML_FILE ...  GEN_HDR ctype.h ...)
        │
        ▼ （CMake 规则内部）
  add_custom_command(
    COMMAND python3 .../utils/hdrgen/main.py
            --output <build>/include/ctype.h
            --depfile ...
            ctype.yaml
            "@<rsp 文件，内含 --entry-point=isalpha --entry-point=isdigit ..."
  )
        │
        ▼ （构建时执行）
  ninja libc   →   生成 build/.../include/ctype.h
```

注意 `add_gen_header` 有个 Overlay 守卫：**非 Full 模式且非 PROXY 时直接返回、不生成**。这呼应 u1-l4：Overlay 模式下公共头文件回退到系统头，不需要自己生成（代理头例外）。

#### 4.4.3 源码精读

**（1）`include/CMakeLists.txt` 的接线**

[include/CMakeLists.txt:L25-L38](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/include/CMakeLists.txt#L25-L38)

```cmake
macro(add_header_macro TARGET_NAME YAML_FILE GEN_HDR DEPENDS)
  add_gen_header(
    ${TARGET_NAME}
    YAML_FILE ${YAML_FILE}
    GEN_HDR ${GEN_HDR}
    ${DEPENDS}
    ${ARGN}
  )
endmacro()

add_header_macro(
  ctype
  ../libc/include/ctype.yaml
  ctype.h
  ...
```

中文：`add_header_macro` 只是个薄封装，把「目标名 / YAML 文件 / 生成的头文件名 / 依赖」转交给 `add_gen_header`。`ctype` 这一行就是在声明「用 `ctype.yaml` 生成 `ctype.h`」。`errno` 的接线在 [include/CMakeLists.txt:L386-L388](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/include/CMakeLists.txt#L386-L388) 同理。

**（2）`add_gen_header` 核心：调用 hdrgen**

[cmake/modules/LLVMLibCHeaderRules.cmake:L84-L87](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCHeaderRules.cmake#L84-L87)（Overlay 守卫）

```cmake
  if(NOT LLVM_LIBC_FULL_BUILD AND NOT ADD_GEN_HDR_PROXY)
    add_library(${fq_target_name} INTERFACE)
    return()
  endif()
```

中文：非 Full 模式且不是代理头，就直接 return 不生成——Overlay 模式用系统头。

[cmake/modules/LLVMLibCHeaderRules.cmake:L104-L128](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/cmake/modules/LLVMLibCHeaderRules.cmake#L104-L128)（构建入口点名单并调用 hdrgen）

```cmake
  if(LLVM_LIBC_ALL_HEADERS)
    set(entry_points "")
  else()
    set(entry_points "${TARGET_ENTRYPOINT_NAME_LIST}")
  endif()
  list(TRANSFORM entry_points PREPEND "--entry-point=")
  set(rsp_file "${CMAKE_CURRENT_BINARY_DIR}/${relative_path}.rsp")
  file(GENERATE OUTPUT ${rsp_file} CONTENT "$<JOIN:${entry_points},\n>")

  add_custom_command(
    OUTPUT ${out_file}
    COMMAND ${Python3_EXECUTABLE} "${LIBC_SOURCE_DIR}/utils/hdrgen/main.py"
            --output ${out_file}
            --depfile ${dep_file}
            --write-if-changed
            ${proxy_arg}
            ${yaml_file}
            "@${rsp_file}"
    DEPENDS ${yaml_file} ${rsp_file}
    DEPFILE ${dep_file}
  )
```

中文：这段是构建集成的精华。①`entry_points` 来自 `TARGET_ENTRYPOINT_NAME_LIST`——这正是 u2-l4 讲过的、由平台 `entrypoints.txt` 推导出来的「本平台支持的函数短名名单」。②把这些名字都加上 `--entry-point=` 前缀，写进一个 response 文件（`.rsp`），再用 `@${rsp_file}` 传给脚本（参数太多时用 rsp 文件是惯例）。③`add_custom_command` 在构建时调用 `main.py`，注意它调的是 **`utils/hdrgen/main.py`**（薄入口），并启用 `--write-if-changed`（内容没变就不重写，避免不必要的重新编译）。`DEPFILE` 让 CMake 知道 YAML/模板一改就要重新生成。

**（3）集成测试：固定输入比对输出**

hdrgen 自带一个集成测试 `ninja check-hdrgen`，做法是「喂一个固定的测试 YAML，逐字节比对生成的头文件」：

[utils/hdrgen/CMakeLists.txt:L7-L14](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/utils/hdrgen/CMakeLists.txt#L7-L14)

```cmake
  add_test(
    NAME hdrgen_integration_test
    COMMAND python3 ${HDRGEN_TESTS_DIR}/test_integration.py --output_dir ${TEST_OUTPUT_DIR}
  )
  add_custom_target(check-hdrgen
    COMMAND ${CMAKE_CTEST_COMMAND} -R hdrgen_integration_test --output-on-failure
  )
```

测试输入 `test_small.yaml` 里特意覆盖了多种情况（自定义模板、merge 文件、guard、宏、多类型参数），期望输出 `expected_output/test_header.h` 就是一份「教科书式的生成结果」，强烈推荐对照阅读：

- 输入：[utils/hdrgen/tests/input/test_small.yaml](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/utils/hdrgen/tests/input/test_small.yaml)
- 期望输出：[utils/hdrgen/tests/expected_output/test_header.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/utils/hdrgen/tests/expected_output/test_header.h)

#### 4.4.4 代码实践

**实践目标**：在已有构建里跑通 hdrgen 的测试与生成，亲眼看一个头文件被生成出来。

**操作步骤**（需要先完成 u1-l3 的 runtimes 构建，待本地验证）：

1. 进入你的 build 目录。
2. 运行 `ninja check-hdrgen`，确认集成测试通过。
3. 运行 `ninja libc`，触发头文件生成。
4. 查看 `build/projects/libc/include/ctype.h`（或 runtime 构建下的 `build/libc/include/ctype.h`）。

**需要观察的现象**：

- 打开生成的 `ctype.h`，找到 `int isalpha(int) __NOEXCEPT;` 这一行，回想它是从 `ctype.yaml` 的 `isalpha` 条目经 `Function.__str__` + `public_api` 来的。
- 对比 `ctype.yaml` 里属于 `posix` 的 `isalnum_l` 是否出现在生成结果里——取决于你构建时 `entrypoints.txt` 是否纳入了它。

**预期结果**（待本地验证）：生成的 `ctype.h` 顶部有 include 守卫 `_LLVM_LIBC_CTYPE_H`，内部是 `__BEGIN_C_DECLS ... __END_C_DECLS` 包裹的一组函数声明，结构与 `expected_output/test_header.h` 同构。如果 `check-hdrgen` 失败，通常是 PyYAML 版本或 Python 版本问题（文档要求 Python 3.8、PyYAML 5.1）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `add_custom_command` 用 `@${rsp_file}` 传入口点，而不是直接把所有 `--entry-point=` 写在 COMMAND 里？

**答案**：因为一个平台的入口点名单可能有几百个，直接展开成命令行会超出操作系统的命令行长度限制。response 文件（rsp）把参数写进一个文件，再用 `@file` 引用，绕开长度限制。`hdrgen/main.py` 的 `argparse` 用 `fromfile_prefix_chars="@"` 来支持读取这种文件。

**练习 2**：Overlay 模式下 `add_gen_header` 直接 `return()` 不生成头文件，那 Overlay 模式用户 `#include <ctype.h>` 时包含的是谁的头文件？

**答案**：是系统的 ctype.h。Overlay 模式不替换公共头，只覆盖少数函数符号（详见 u1-l4）。只有 PROXY（代理头）例外——代理头会在 Full/Overlay 之间切换内部类型来源，这在 u3-l2 会专门讲。

---

## 5. 综合实践

把本讲四个模块串起来，完成 spec 指定的核心任务：**用 hdrgen 给 `ctype.yaml` 假装添加一个示例函数，运行后检查生成的头文件片段，说明 YAML 各字段如何映射到最终 C 声明。**

### 任务

给 `ctype.yaml` 添加一个虚构函数 `iscool`：返回 `int`，接受一个 `int` 参数，属于 `stdc` 标准。

### 步骤

**第 1 步：理解字段映射（用 4.1、4.2 的知识）。**

按映射表，`iscool` 的 YAML 条目应是：

```yaml
  - name: iscool
    standards:
      - stdc
    return_type: int
    arguments:
      - type: int
```

它应渲染成 C 声明 `int iscool(int) __NOEXCEPT;`。

**第 2 步：尝试用文档记载的命令行添加（注意：当前 HEAD 下需要本地验证）。**

官方文档（[docs/dev/header_generation.md:L41-L52](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/docs/dev/header_generation.md#L41-L52)）给出的命令是：

```bash
python3 libc/utils/hdrgen/yaml_to_classes.py \
  libc/include/ctype.yaml \
  --add_function "int" iscool "int" stdc null null
```

> ⚠️ **诚实提示**：在当前 HEAD，仓库根层的 `utils/hdrgen/yaml_to_classes.py` 是一个薄入口，它 `from hdrgen.main import main` 后调用 `hdrgen.main.main`；而 `--add_function` 这个参数其实是在 **`hdrgen/yaml_to_classes.py` 自己的 `main()`**（[utils/hdrgen/hdrgen/yaml_to_classes.py:L245-L273](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/utils/hdrgen/hdrgen/yaml_to_classes.py#L245-L273)）里注册的。因此上面这条命令在当前版本**很可能**因 argparse 报「 unrecognized arguments」而失败——**待本地验证**。这是源码当前的一个不一致点：文档与入口脚本的对接受众有偏差。

**第 3 步（更可靠的替代方案）：直接调用 Python API 完成添加并查看生成片段。**

绕开命令行对接受众问题，直接用底层函数。在 `libc/` 目录下：

```bash
python3 -c "
import sys; sys.path.insert(0, 'utils/hdrgen')
from hdrgen.yaml_to_classes import add_function_to_yaml
# 这会真的修改 ctype.yaml（请确保你在 git 工作区，便于 git checkout 还原）
add_function_to_yaml('include/ctype.yaml', ['int','iscool','int','stdc','null','null'])
"
git diff include/ctype.yaml     # 看被插入的条目
```

`add_function_to_yaml` 的逻辑见 [utils/hdrgen/hdrgen/yaml_to_classes.py:L191-L227](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/libc/utils/hdrgen/hdrgen/yaml_to_classes.py#L191-L227)：它会把 `['int','iscool','int','stdc','null','null']` 经 `parse_function_details` 解析（`null` 被解释成「无 guard / 无 attributes」），构造出函数字典 `{name: iscool, standards: [stdc], return_type: int, arguments: [{type: int}]}`，按字母序插入 `functions` 列表，再写回 YAML。

**第 4 步：生成头文件片段，确认映射。**

```bash
python3 -c "
import sys; sys.path.insert(0, 'utils/hdrgen')
from pathlib import Path
from hdrgen.yaml_to_classes import load_yaml_file
from hdrgen.header import HeaderFile
h = load_yaml_file(Path('include/ctype.yaml'), HeaderFile, ['iscool'])
print(str(h.public_api()))
"
```

**第 5 步：还原。**

```bash
git checkout include/ctype.yaml   # 删除刚才插入的虚构函数，不要污染源码
```

### 需要观察的现象与预期结果（待本地验证）

- `git diff` 应显示一个新插入的 `iscool` 条目，结构与第 1 步手写的 YAML 一致（`null` 字段不会出现，因为 `add_function_to_yaml` 只在 guard/attributes 非空时才写入对应键）。
- `public_api()` 输出里应包含一行 `int iscool(int) __NOEXCEPT;`，夹在 `__BEGIN_C_DECLS` 与 `__END_C_DECLS` 之间。
- 字段映射的完整链条得到验证：`name→iscool`、`return_type→int`、`arguments[int]→(int)`、`standards→`（用于归类，不在声明内）、`__NOEXCEPT;` 由 `public_api` 统一追加。

> 这个综合实践同时调用了你四个模块的知识：YAML 规范（4.1）、hdrgen 对象模型与 `__str__`/`public_api`（4.2）、默认模板与 `%%public_api()` 拼装（4.3）、以及构建期入口点过滤的边界（4.4）。

---

## 6. 本讲小结

- LLVM-libc 的公共头文件**是生成出来的**，不是手写的；`include/*.yaml` 是描述头文件内容的**唯一事实来源**。
- 一个 YAML 描述五类内容（functions/macros/types/enums/objects），其中函数条目的 `name`/`return_type`/`arguments` 三字段直接决定 C 声明，`standards`/`guard`/`attributes` 用于分类与条件编译。
- hdrgen 用「**YAML → 类对象 → C 字符串**」的三段式：`yaml_to_classes` 反序列化，`Function.__str__` 把对象渲染成声明，`HeaderFile.public_api` 拼成完整片段；函数按名排序保证生成稳定。
- `.h.def` 是**可选的手写模板**，通过 `%%public_api()` 占位符与生成内容拼装；只有需要手写平台逻辑的头文件（如 errno）才用，其余用默认模板。仓库里 55 个 yaml 中仅 5 个配了 `.h.def`。
- `fill_public_api` 用一次 `replace("%%public_api()", 片段, 1)` 完成拼装。
- CMake 的 `add_gen_header` 规则在构建时调用 `main.py`，并通过 `--entry-point` 名单（来自平台 `entrypoints.txt`）实现「只生成本平台支持的函数声明」；Overlay 模式下默认不生成（回退系统头）。
- `ninja check-hdrgen` 用固定输入逐字节比对来守护生成器的正确性。

## 7. 下一步学习建议

- **紧接 u3-l2**：本讲生成的头文件里反复出现 `llvm-libc-types`、`llvm-libc-macros` 这些 include，以及「代理头（proxy header）」这个概念（4.4 练习 2 提到）。下一讲 **u3-l2 代理头文件、公共宏与类型** 会专门讲这些公共构件，以及它们在 Full / Overlay 两种模式间如何切换来源，建议直接续读。
- **回到函数族**：有了「头文件是 YAML 生成的」这一认知，再读 **u5-l1 ctype 函数族** 时，你能把「用户看到的 `int isalpha(int)`」与「`src/ctype/isalpha.cpp` 的实现」清楚对应起来——前者来自本讲的 YAML，后者来自 u2-l2 的实现规范。
- **想贡献新函数**：等读到 **u11-l3 贡献一个完整新函数** 时，本讲的 YAML 规范知识就是「六步流程」的第一步。
- **源码延伸阅读**：如果对生成器的边界情况感兴趣，可以读 `utils/hdrgen/hdrgen/header.py` 里的 `includes()`（自动推导一个头文件该包含哪些类型/宏头）和 `proxy_contents()`（代理头的生成），它们是本讲未展开的进阶部分。
