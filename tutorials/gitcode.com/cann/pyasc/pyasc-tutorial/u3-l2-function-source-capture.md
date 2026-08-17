# u3-l2 函数对象与源码捕获：Function 基类与依赖分析

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `Function` 基类在**装饰时**（`@asc.jit` 执行的那一刻）就完成了源码抓取与 AST 捕获，而不是在每次调用时；
2. 掌握 `cache_key` 哈希到底覆盖了什么：**源码哈希 + 起始行号 + 全局 ConstExpr 依赖**；
3. 解释为什么修改 kernel 函数源码（或它调用的其它 `@asc.jit` 子函数的源码）会使缓存失效；
4. 识别一个真实存在的陷阱：**修改普通全局变量的值既不会重编译、也不会报错**，旧 kernel 会被直接复用；
5. 会用 `PYASC_CACHE_DIR` 和纯 Python 探针脚本亲自验证上述结论。

## 2. 前置知识

本讲只需要 Python 语言层面的知识，不需要 NPU。

- **装饰器的执行时机**：`@asc.jit` 不是注释，而是一次真实的函数调用。Python 解释器执行到 `def` 语句时，先创建函数对象，再立刻把它传给装饰器。所以「装饰时」就是「模块导入时」。上一讲（u3-l1）已经看到 `JITFunction.__init__` 在这一刻就做了选项守门检查；本讲会看到它的父类 `Function` 在同一刻抓走了源码。
- **`inspect` 模块**：`inspect.getsource(fn)` 返回函数的源码文本（含装饰器行）；`inspect.getsourcelines(fn)` 返回 `(行列表, 起始行号)`；`inspect.getclosurevars(fn)` 返回闭包引用。它们都依赖 `fn.__code__.co_filename` 指向的真实磁盘文件——这就是为什么 kernel 必须写在 `.py` 文件里，而不能在交互式 REPL 里定义。
- **`ast` 模块**：`ast.parse(text)` 把源码文本解析成语法树；`ast.NodeVisitor` 提供按节点类型分发 `visit_Xxx` 方法的遍历框架。本讲的两位主角 `Function` 与 `DependenciesFinder` 都建立在这两者之上。
- **Python 名字解析顺序**：函数内一个名字，先看局部作用域，再看闭包（nonlocals），再看模块全局（`__globals__`），最后看内置（builtins）。`DependenciesFinder` 的查找顺序与此一致。
- **哈希摘要**：`hashlib.sha256(x).hexdigest()` 把任意长度的字节串映射成 64 个十六进制字符。摘要对输入极其敏感——源码改一个字符，摘要就面目全非，这正是「缓存 key」想要的性质。

承接前面讲义的认知：u1-l5 已给出两级缓存（进程内字典 + 落盘文件）与缓存 key 的分工总览；u2-l1 讲过 `ConstExpr` 参数以 `repr` 进入缓存 key。本讲深入到 key 的**来源**——`Function.cache_key` 是怎么算出来的。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [python/asc/codegen/function.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py) | `Function` 基类：装饰时捕获源码/AST/位置，惰性计算 `cache_key` |
| [python/asc/codegen/dependencies_finder.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/dependencies_finder.py) | `DependenciesFinder`：遍历 AST 找出全局依赖，把源码与被调子函数的哈希揉进摘要 |
| [python/asc/runtime/jit.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py) | `cache_key` 的消费方：`JITFunction` 组合 `Function`，把 key 送进两级缓存 |
| [python/asc/runtime/cache.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/cache.py) | `PYASC_CACHE_DIR`、文件缓存 key 的拼装与原子落盘 |
| [python/asc/codegen/name_scope.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/name_scope.py) | 内置函数白名单，被 `DependenciesFinder` 复用 |
| [python/asc/language/core/constexpr.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/constexpr.py) | `ConstExpr` 包装类：让「全局常量的值」能进入缓存 key |

## 4. 核心概念与源码讲解

本讲的四个最小模块：**Function 基类**、**get_function_node/parse**、**cache_key**、**DependenciesFinder**。

### 4.1 Function 基类：装饰时就捕获一切

#### 4.1.1 概念说明

上一讲我们精读了 `JITFunction`，它是 pyasc 首个扩展点（codegen/compiler/launcher 三个可替换类属性）。本讲下钻一层：`JITFunction` 继承自 `Function`，后者回答一个更基础的问题——

> 「一个 Python 函数被 `@asc.jit` 修饰之后，编译器需要记住关于它的哪些事实？」

答案是三样东西：

1. **AST**（`node`）：后续 `FunctionVisitor` 遍历的就是它；
2. **源码行**（`src` / `raw_src` / `starting_line_number`）：用于报错定位、以及参与缓存哈希；
3. **身份信息**（`fn_name`、`location`）：`模块名.限定名` 与文件位置。

关键设计决策：**这些捕获全部发生在装饰时（即模块导入时），而不是每次调用时**。这样做有两个理由：

- **正确性**：JIT 编译针对的是「这份源码」，应当在源码可用的那一刻固化下来，之后无论谁怎么改运行时状态，编译输入都不受影响；
- **效率**：AST 解析只需要做一次，`kernel[8, stream](...)` 每次调用直接复用。

#### 4.1.2 核心流程

`Function.__init__` 在装饰那一刻执行的流水线：

```text
fn (普通 Python 函数)
 ├─ 1. 可调用性检查（不是 callable 直接 TypeError）
 ├─ 2. get_function_node(fn)      → 抓源码文本 → 剥掉装饰器 → ast.parse → 校验后取 FunctionDef
 ├─ 3. get_location(fn)           → (co_filename, co_firstlineno) → FunctionLocation
 ├─ 4. get_source_lines(fn)       → (源码行列表, 起始行号)
 ├─ 5. 拼出 src（按行 split 的列表）
 ├─ 6. get_full_name(fn)          → "模块.限定名"
 ├─ 7. 再次剥装饰器得到 src_without_decorator（哈希输入，见 4.3）
 └─ 8. 复用被包装函数的元信息（__doc__/__name__/__qualname__/__globals__/__module__）
```

#### 4.1.3 源码精读

类的字段声明与构造函数：[python/asc/codegen/function.py:L30-L58](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L30-L58)

```python
class Function(Generic[P, T]):
    fn: Callable[P, T]
    node: ast.FunctionDef
    location: FunctionLocation
    src: Optional[List[str]]

    def __init__(self, fn: Callable[P, T]):
        if not callable(fn):
            raise TypeError(f"{fn.__class__.__name__} instance is not callable")
        self.fn = fn
        self.node = self.get_function_node(fn)
        self.location = self.get_location(fn)
        self.raw_src, self.starting_line_number = self.get_source_lines(fn)
        ...
        self.hash = None
        self.used_global_vals: Dict[Tuple[str, int], Tuple[Any, Dict[str, Any]]] = {}
```

这段代码在构造函数里一次性完成第 2~8 步，两个「预埋」值得注意：`self.hash = None` 是缓存哈希的惰性标记（4.3 详述）；`used_global_vals` 初始为空字典，等待 `DependenciesFinder` 填充（4.4 详述）。

`JITFunction` 如何衔接：[python/asc/runtime/jit.py:L35-L36](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L35-L36) 中 `super().__init__(fn)` 第一句就触发上述捕获——所以「装饰时捕获」在继承链的最顶端就完成了。

元信息复用：[python/asc/codegen/function.py:L53-L58](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L53-L58)

```python
# reuse docs of wrapped function
self.__doc__ = fn.__doc__
self.__name__ = fn.__name__
self.__qualname__ = fn.__qualname__
self.__globals__ = fn.__globals__
self.__module__ = fn.__module__
```

`Function` 对外「伪装」成原函数：`help()` 能看到文档字符串，缓存目录里的 kernel 文件名用的是 `self.fn.__name__`（见 [python/asc/runtime/jit.py:L166](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L166)）。特别注意 `__globals__` 被保留下来——它是原函数的模块全局字典，后面 `DependenciesFinder` 与 `FunctionVisitor` 都要靠它解析全局名字。

直接调用的直通行为：[python/asc/codegen/function.py:L60-L61](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L60-L61)

```python
def __call__(self, *args: P.args, **kwargs: P.kwargs) -> T:
    return self.fn(*args, **kwargs)
```

不带中括号直接调用 `kernel(...)` 时，走的是这行——按普通 Python 语义执行原函数，完全不触发 JIT（与 u3-l1 的结论呼应：只有 `kernel[核数, 流](...)` 才会进入 `_run` 编译链路）。

边界情况：`get_source_lines` 在拿不到源码时（比如函数由 `exec` 动态生成）返回 `None`：[python/asc/codegen/function.py:L107-L113](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L107-L113)。由于返回值要解包成两个变量，`None` 会在装饰那一刻抛 `TypeError`——所以「kernel 必须写在 .py 文件里」不是习惯建议，而是硬约束。

#### 4.1.4 代码实践：亲眼看到「装饰时捕获」

1. **实践目标**：验证 `src`、`node`、`starting_line_number` 在装饰后不再随运行时状态变化。
2. **操作步骤**：写一个 `demo_capture.py`（需按 u1-l2 完成 pyasc 安装；本实践不 launch，无需 NPU）：

   ```python
   # 示例代码
   import asc

   G = 10

   @asc.jit
   def kernel(x):
       y = x + G
       return y

   # 装饰已完成，现在观察捕获结果
   print("fn_name:", kernel.fn_name)
   print("starting_line:", kernel.starting_line_number)
   print("src:", kernel.src[:2], "...")          # 装饰器行是否被剥掉？
   print("node 类型:", type(kernel.node).__name__)

   G = 20  # 运行时改全局，不影响已捕获的内容
   print("改 G 后 src 是否变化:", kernel.src is not None)
   ```

   运行：`python3 demo_capture.py`
3. **需要观察的现象**：`node 类型` 输出 `FunctionDef`；`src` 的第一行以 `def` 开生（装饰器行已被剥掉）；修改 `G` 后属性原样不动。
4. **预期结果**：所有捕获物在装饰后冻结——这为 4.3 的「同进程内源码不可能变化」结论提供直观证据。

#### 4.1.5 小练习与答案

**练习 1**：为什么 pyasc 选择在装饰时而不是首次调用时抓取 AST？
**答案**：装饰时源码一定完整可用；抓取一次后编译输入即固化，调用路径上零解析开销，且缓存 key 有稳定身份。若拖到首次调用，每次调用都要判断「源码是否变过」，反而引入不一致风险。

**练习 2**：`Function.__call__` 为什么直接调用 `self.fn` 而不是走编译链路？
**答案**：`Function` 是基类，只负责「函数对象 + 源码捕获」这一层通用能力；编译与启动策略由子类 `JITFunction` 通过 `__getitem__` 返回的 `_run` 承担。基类保留直通行为，使得不带中括号的调用仍是合法 Python（按原语义执行）。

### 4.2 get_function_node 与 parse：从源码文本到 FunctionDef

#### 4.2.1 概念说明

`inspect.getsource(fn)` 返回的文本**包含装饰器行**（例如 `@asc.jit`），而 `ast.parse` 之后我们只想要那个 `FunctionDef` 节点。这里有两个小而关键的工程问题：

1. **剥装饰器**：用正则 `^def\s+\w+\s*\(`（多行模式）在文本里定位 `def` 关键字，从那里截取——装饰器、注释统统丢掉；
2. **缩进归一**：嵌套定义的函数（比如写在类或外层函数里的 kernel）源码带有前导缩进，`textwrap.dedent` 先把它抹平，`ast.parse` 才能接受。

#### 4.2.2 核心流程

```text
inspect.getsource(fn)          # "@asc.jit\ndef kernel(x):\n    ..."
  → textwrap.dedent            # 去公共缩进
  → 正则截取（从 ^def 开始）    # 剥掉装饰器行
  → ast.parse                  # 得到 ast.Module
  → 校验：Module 且 body 恰有 1 个孩子且为 FunctionDef
  → 返回 node.body[0]          # ast.FunctionDef
```

`parse()` 方法则把最后两步重做一遍——注意它**不复用** `self.node`。

#### 4.2.3 源码精读

`get_function_node`：[python/asc/codegen/function.py:L89-L100](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L89-L100)

```python
@staticmethod
def get_function_node(fn: Callable) -> ast.FunctionDef:
    source = inspect.getsource(fn)
    source = textwrap.dedent("".join(source))
    source = source[re.search(r"^def\s+\w+\s*\(", source, re.MULTILINE).start():]
    node = ast.parse(source)
    if not isinstance(node, ast.Module) or len(node.body) != 1:
        raise RuntimeError("Unexpected function definition, must be ast.Module node with a single child")
    def_node = node.body[0]
    if not isinstance(def_node, ast.FunctionDef):
        raise TypeError(f"JIT compilation is applicable to functions only, got {def_node.__class__.__name__}")
    return def_node
```

这段代码做了「抓取 → 归一 → 剥装饰器 → 解析 → 双重校验」五件事。两道校验分别保证：解析结果必须是**单语句模块**（防止意外解析出多个定义），且唯一孩子必须是 `FunctionDef`（`async def` 是 `AsyncFunctionDef`，会被拒绝；lambda 拿不到 `def`，正则匹配直接失败）。

`get_location` 与 `get_full_name`：[python/asc/codegen/function.py:L102-L105](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L102-L105)、[python/asc/codegen/function.py:L115-L117](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L115-L117)

```python
@staticmethod
def get_location(fn: Callable) -> FunctionLocation:
    code = fn.__code__
    return FunctionLocation(code.co_filename, code.co_firstlineno)

@staticmethod
def get_full_name(fn: Callable) -> str:
    return f"{fn.__module__}.{fn.__qualname__}"
```

`FunctionLocation` 是个极简 dataclass（[python/asc/codegen/function.py:L24-L27](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L24-L27)）：文件名 + 行偏移。它随后被传给 `FunctionVisitor`（[python/asc/runtime/jit.py:L190](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L190)），所以代码生成报错能把位置指回你的 `.py` 文件。

`parse` 为什么重新解析：[python/asc/codegen/function.py:L134-L145](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L134-L145)

```python
# we do not parse `src` in the constructor because
# the user might want to monkey-patch self.src dynamically.
# Our unit tests do this, for example.
def parse(self):
    tree = ast.parse(self.src_without_decorator)
    ...
```

注释自己解释了动机：单测会动态替换 `self.src` 来测试不同源码，所以 `parse()` 每次基于 `src_without_decorator` 现解析。`cache_key`（4.3）也是调用 `self.parse()` 而非复用 `self.node`，二者保持同一入口。

#### 4.2.4 代码实践：看一眼捕获到的 AST

1. **实践目标**：确认装饰器被剥掉、`node` 就是 `FunctionDef`。
2. **操作步骤**：在 4.1.4 的脚本里追加：

   ```python
   # 示例代码
   import ast
   print(kernel.src_without_decorator)
   print(ast.dump(kernel.node, indent=2)[:400])
   ```

3. **需要观察的现象**：打印的源码首行是 `def kernel(x):`；AST dump 的根是 `FunctionDef`，`decorator_list` 为空。
4. **预期结果**：`FunctionVisitor` 后续遍历的输入就是这棵不含装饰器的树——这也解释了为什么编译选项只能来自装饰器小括号（`@asc.jit(...)`，由 `JITFunction` 处理），而不会出现在 AST 里。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `@asc.jit` 换成两层装饰器 `@foo` `@asc.jit`，`get_function_node` 抓到的文本从哪一行开始？
**答案**：仍从第一个 `^def` 行开始。正则只找 `def`，装饰器行无论几层全部被截掉。

**练习 2**：`parse()` 与 `get_function_node()` 有重复劳动（都做了一次 `ast.parse`），为什么保留两个入口？
**答案**：`get_function_node` 是装饰时的**一次性捕获**（构造 `self.node`）；`parse` 是**按需重解析**，允许 `self.src` 被单测动态替换。分离后测试可以不碰磁盘文件就测试不同源码的哈希与访问行为。

### 4.3 cache_key：源码哈希 + 起始行号 + ConstExpr 全局依赖

#### 4.3.1 概念说明

`cache_key` 是 `Function` 提供给缓存体系的「这份源码的身份指纹」。上一讲（u1-l5）我们看到两级缓存按 key 复用 kernel；本讲回答 key 从哪来。

设计上要回答的问题：**哪些变化意味着「必须重编译」？**

- 源码文本变了 → 生成的 IR 必然不同 → 必须重编；
- 函数在文件里的位置变了（哪怕内容没变，比如上方加了一行注释）→ 也算身份变化，一起编进 key；
- 依赖的全局**常量**（ConstExpr 包装）变了 → 常量会被固化进 IR → 必须重编；
- 依赖的其它 `@asc.jit` 子函数源码变了 → 内联后的 IR 变了 → 必须重编（由 4.4 的 `update_hash` 传递实现）。

而**普通全局变量的值不在其中**——这是本讲最重要的陷阱，4.3.3 与综合实践会专门验证。

#### 4.3.2 核心流程

`cache_key` 的计算公式（三次拼接后做一次 sha256）：

\[ \text{cache\_key} = \mathrm{sha256}\Big( H_{\text{src}} \,\Big\|\, \mathrm{str}(L) \,\Big\|\, \mathrm{str}\big(\text{CE}\big) \Big) \]

其中：

- \( H_{\text{src}} = \mathrm{sha256}(\text{src\_without\_decorator}).\text{hexdigest}() \)，由 `DependenciesFinder` 的 hasher 直接给出；
- \( L \) 是 `starting_line_number`（函数 `def` 所在行号）；
- \( \text{CE} = [(name, val) \mid (name, val) \in \text{used\_global\_vals},\ val \text{ 是 } ConstExpr] \)，注意**只有 ConstExpr 类型的全局值入选**。

它随后参与**文件缓存 key**（进程间复用）：

\[ K_{\text{file}} = \mathrm{sha256}\big(\text{pyasc\_key} \,\|\, \text{cache\_key} \,\|\, \text{cache\_factors}\big) \]

而**内存缓存 key** 只用 `cache_factors`：\( K_{\text{mem}} = \mathrm{sha256}(\text{cache\_factors}) \)。这个差别是自洽的：`cache_factors`（两类选项、ConstExpr 形参 repr、参数类型、函数名，见 [python/asc/runtime/jit.py:L137-L154](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L137-L154)）在进程内可能每次调用都不同；而源码哈希在同进程内**不可能变**（装饰时已捕获），放进内存 key 没有意义——它只在跨进程的文件层才需要。

计算本身的控制流：

```text
首次访问 cache_key?
 ├─ 是 → 先放占位哈希 "recursion:{fn_name}"（防自递归死循环）
 │       取闭包 nonlocals
 │       构造 DependenciesFinder(fn_name, __globals__, nonlocals, src)
 │       finder.visit(parse())            # 遍历 AST，收集依赖并混入子函数哈希
 │       hash = finder.ret + str(起始行号)  # 源码哈希 ∥ 行号
 │       hash += str(ConstExpr 全局依赖列表)
 │       hash = sha256(hash)
 │       回填 self.hash
 └─ 否 → 直接返回 self.hash
```

#### 4.3.3 源码精读

`cache_key` 完整实现：[python/asc/codegen/function.py:L63-L87](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L63-L87)

```python
@property
def cache_key(self):
    if self.hash is None:
        # Set a placeholder hash to break recursion in case the function
        # transitively calls itself. The full hash is set after.
        self.hash = f"recursion:{self.fn_name}"
        nonlocals = inspect.getclosurevars(self.fn).nonlocals
        from .dependencies_finder import DependenciesFinder
        dependencies_finder = DependenciesFinder(
            name_=self.fn_name, globals_=self.__globals__,
            nonlocals_=nonlocals, src_=self.src_without_decorator,
        )
        dependencies_finder.visit(self.parse())
        self.hash = dependencies_finder.ret + str(self.starting_line_number)
        self.used_global_vals = dict(sorted(dependencies_finder.used_global_vals.items()))

        self.hash += str([(name, val)
                          for (name, _), (val, _) in self.used_global_vals.items()
                          if isinstance(val, ConstExpr)])
        self.hash = hashlib.sha256(self.hash.encode("utf-8")).hexdigest()
    return self.hash
```

逐段说明：

- **占位哈希**（第 67-69 行）：如果 kernel A 内联调用了子函数 B，而 B 又（直接或间接）调用 A，计算 A 的 key 需要算 B 的 key、算 B 的又需要算 A 的——先写入占位符 `recursion:...` 并立刻赋给 `self.hash`，递归回来时 `self.hash is None` 不成立，直接返回占位符，环被打断。
- **惰性 + 只算一次**：整个计算包在 `if self.hash is None` 里，首次访问后永久缓存。
- **第 80 行**：`finder.ret` 就是 `sha256(src)` 的十六进制摘要（见 4.4），拼上起始行号。**行号参与 key** 意味着「在 kernel 上方加一行注释」也会改变文件缓存 key——即使函数内容一字未改。
- **第 83-85 行（本讲最关键的三行）**：从 `used_global_vals` 里**筛出值类型为 `ConstExpr` 的条目**拼进哈希。普通 int/float/str 全局变量被 `DependenciesFinder` 记录进 `used_global_vals`（deepcopy 保存），但它们的**值不进入任何缓存 key**。

`used_global_vals` 的启动时校验存在吗？`DependenciesFinder` 的类文档（[python/asc/codegen/dependencies_finder.py:L29-L33](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/dependencies_finder.py#L29-L33)）写着「launch 时我们会检查这些全局变量是否还是当初的值，不是则报错」。但用 `grep` 全仓检索 `used_global_vals`，只有 [function.py:L51](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L51)、[function.py:L81](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L81)、[function.py:L84](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L84) 与 `dependencies_finder.py` 自身引用它——**没有任何启动路径消费它做校验**。也就是说：文档描述的是设计意图，当前实现里修改普通全局变量既不重编译、也不报错，旧 kernel 被静默复用。

`cache_key` 的消费点：[python/asc/runtime/jit.py:L156-L168](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L156-L168)

```python
mem_cache_key = get_mem_cache_key(cache_factors)
kernel = self.kernel_cache.get(mem_cache_key, None)
...
file_cache_key = get_file_cache_key(self.cache_key, cache_factors)
file_cache_manager = get_cache_manager(file_cache_key)
kernel_file_name = self.fn.__name__ + ".o"
cached_kernel_file = file_cache_manager.get_file(kernel_file_name)
```

注意时序：**先查内存缓存（不含 `cache_key`），未命中才用 `self.cache_key` 组装文件 key**。两级 key 的拼装在 [python/asc/runtime/cache.py:L139-L150](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/cache.py#L139-L150)：文件 key 额外混入 `pyasc_key()`（前端全部源码 + `libpyasc.so` 的哈希，[python/asc/runtime/cache.py:L108-L136](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/cache.py#L108-L136)），因此升级 pyasc 版本也会整体失效旧缓存。

缓存失效矩阵（本讲核心结论，综合实践会逐行验证）：

| 变化 | 进入 cache_key | 进入 cache_factors | 是否重编译 |
|---|---|---|---|
| kernel 函数源码改动 | ✔（源码哈希） | ✘ | 是（跨进程生效） |
| 函数起始行号变化（上方加注释） | ✔（行号） | ✘ | 是 |
| 被调 `@asc.jit` 子函数源码变化 | ✔（update_hash 传递） | ✘ | 是 |
| 模块级 `ConstExpr` 全局值变化 | ✔（L83-85 拼接） | ✘ | 是 |
| **普通全局变量值变化（G=10→20）** | **✘** | **✘** | **否（陷阱！）** |
| 运行时参数值变化（tensor 内容等） | ✘ | ✘（只记类型） | 否 |
| 参数类型变化（int→float） | ✘ | ✔ | 是 |
| ConstExpr 形参值变化 | ✘ | ✔（repr） | 是 |
| codegen/compile 选项变化 | ✘ | ✔ | 是 |
| pyasc 自身源码/版本变化 | ✘（进 pyasc_key） | ✘ | 是（文件层） |
| `always_compile=True` | — | — | 总是（全绕过） |

#### 4.3.4 代码实践：纯 Python 探针，验证失效矩阵（无需 NPU）

1. **实践目标**：不启动任何 kernel，只用 `Function.cache_key` 验证「哪些变化改变 key、哪些不改变」。
2. **操作步骤**：写 `probe_key.py`：

   ```python
   # 示例代码
   import asc
   from asc.codegen.function import Function

   G = 10
   H = asc.ConstExpr(7)

   def kernel_a(x):
       y = x + G + 5
       return y

   def kernel_b(x):   # 与 kernel_a 内容相同
       y = x + G + 5
       return y

   def kernel_c(x):   # 与 kernel_a 不同内容
       y = x + G + 6
       return y

   # Function 基类可以直接构造（不触发编译链路）
   print("a vs b(同名不同位):", Function(kernel_a).cache_key == Function(kernel_b).cache_key)
   print("a vs c(内容不同):  ", Function(kernel_a).cache_key == Function(kernel_c).cache_key)
   ```

   然后做两组对照实验：
   - **实验一（普通全局）**：把 `G = 10` 改成 `G = 20`，重新运行，对比两次打印的 `Function(kernel_a).cache_key`；
   - **实验二（ConstExpr 全局）**：把 `H = asc.ConstExpr(7)` 改成 `H = asc.ConstExpr(8)`（可让 kernel 引用 `H`），同样对比。
3. **需要观察的现象**：`a vs b` 为 `False`（起始行号不同）；`a vs c` 为 `False`（源码哈希不同）；实验一中改 `G` 前后 key **完全相同**；实验二中改 `H` 前后 key **不同**。
4. **预期结果**：与 4.3.3 的失效矩阵一致。若与预期不符，回头核对 `function.py` L80 与 L83-85 的拼接逻辑找原因。

#### 4.3.5 小练习与答案

**练习 1**：为什么内存缓存 key 不包含源码哈希，文件缓存 key 却包含？
**答案**：源码在装饰时捕获、进程内不可变，内存层查它无意义；跨进程时源码可能已被编辑（或函数移了行），文件层必须靠源码哈希 + 行号区分新旧 kernel，否则会错误复用旧 `.o`。

**练习 2**：在 kernel 定义上方加一行注释后重启程序，会发生什么？这合理吗？
**答案**：`starting_line_number` 变化 → `cache_key` 变化 → 文件缓存未命中 → 重新编译。从「注释也改变文件内容」的角度算保守但正确的取舍；代价是无谓的重编，这也是它被单独编入 key 而非从源码哈希中剔除的原因——实现上最简单、最不易出错。

**练习 3**：`always_compile=True` 与修改源码触发的重编，走的代码路径有何不同？
**答案**：见 [python/asc/runtime/jit.py:L161](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L161) 与 [jit.py:L169](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L169)：`always_compile` 让两级缓存的「命中即返回」判断全部短路（`not compile_options.always_compile and ...` 为假），且命中也不写入缓存；改源码则是 key 天然不同、查不到旧条目，新结果照常写盘。

### 4.4 DependenciesFinder：全局依赖分析与传递哈希

#### 4.4.1 概念说明

`cache_key` 公式里的 \( H_{\text{src}} \) 与「ConstExpr 全局依赖清单」都来自 `DependenciesFinder`——一个继承 `ast.NodeVisitor` 的访问器。它回答两个问题：

1. **这份源码的摘要是什么**——构造时直接 `sha256(src)` 初始化 hasher，之后每次发现「被调用的 `@asc.jit` 子函数」就把对方的 `cache_key` 追加进 hasher（**传递依赖**：A 调 B，A 的指纹里含 B 的指纹）；
2. **这份源码引用了哪些全局变量**——记入 `used_global_vals`，供 `Function.cache_key` 筛选 ConstExpr 条目。

为什么需要传递依赖？回忆 u4 将要展开的事实（u1-l4 已提过）：Device 侧子函数会被**内联**进 kernel 的同一个 IR。所以子函数源码变了，kernel 生成的 Ascend C 必然变了——A 的缓存必须随 B 一起失效。

#### 4.4.2 核心流程

对每个名字节点的处理决策树（`record_reference`，[python/asc/codegen/dependencies_finder.py:L89-L117](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/dependencies_finder.py#L89-L117)）：

```text
遇到一个名字引用（Load 上下文）
 ├─ 值是 None 或模块          → 忽略（模块不是"数据依赖"）
 ├─ 值定义在 asc.language / typing → 忽略（框架自身符号）
 ├─ 值是 pyasc Function（@asc.jit 子函数） → update_hash：混入对方 cache_key（递归）
 ├─ 值是其它 callable（非 type、非 ConstExpr）→ RuntimeError("Unsupported function referenced")
 ├─ 正在访问参数默认值         → 忽略（Python 默认值在定义时已求值一次）
 └─ 其余（普通值）            → deepcopy 后记入 used_global_vals[(名字, id(全局字典))]
```

关键数据结构的形状（[python/asc/codegen/dependencies_finder.py:L52-L62](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/dependencies_finder.py#L52-L62) 的注释写得很清楚）：

\[ \text{used\_global\_vals}: (\text{var\_name},\ id(\_\_globals\_\_)) \mapsto (\text{deepcopy(value)},\ \_\_globals\_\_) \]

键里带 `id(__globals__)` 是因为：不同模块可以有同名全局变量，它们是不同的依赖。值里 deepcopy 一份快照，防止后续修改污染记录。

#### 4.4.3 源码精读

构造与摘要初始化：[python/asc/codegen/dependencies_finder.py:L35-L50](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/dependencies_finder.py#L35-L50)

```python
def __init__(self, name_, globals_, nonlocals_, src_) -> None:
    super().__init__()
    self.name = name_
    self.hasher = hashlib.sha256(src_.encode("utf-8"))
    self.globals = globals_
    self.nonlocals = nonlocals_
    self.supported_python_builtins = set(NameScope.builtins)
    self.supported_modules = {PYASC_MODULE, "typing"}
```

`ret` 属性就是 hasher 的当前摘要（[python/asc/codegen/dependencies_finder.py:L66-L68](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/dependencies_finder.py#L66-L68)）——`Function.cache_key` 第 80 行取的正是它。白名单来自 `NameScope.builtins`（12 个：`dict/float/int/isinstance/issubclass/len/list/range/repr/str/tuple/type`，见 [python/asc/codegen/name_scope.py:L13-L29](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/name_scope.py#L13-L29)），与 u4-l5 将讲的「内置函数白名单」同一份。

传递依赖的核心：[python/asc/codegen/dependencies_finder.py:L70-L87](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/dependencies_finder.py#L70-L87)

```python
def update_hash(self, func):
    if not isinstance(func, Function):
        raise TypeError("func is not Function")
    for k in self.used_global_vals.keys() & func.used_global_vals.keys():
        var_name, _ = k
        v1, _ = self.used_global_vals[k]
        v2, _ = func.used_global_vals[k]
        if v1 != v2:
            raise RuntimeError(f"Global variable {var_name} has value {v1} when compiling {self.name}, \
                but inner kernel {func.__name__} has conflicting value {v2} ...")
    self.used_global_vals.update(func.used_global_vals)
    func_key = func.cache_key
    func_key += str(getattr(func, "noinline", False))
    self.hasher.update(func_key.encode("utf-8"))
```

这段做了三件事：**冲突检查**（同一个全局变量在 kernel 与子函数首次编译时值不同 → 直接报错，这是全链路唯一一处对全局变量值的运行时校验，且只覆盖「同名冲突」这一种情形）；**合并依赖清单**；**把子函数的 `cache_key` 追加进 hasher**（`func.cache_key` 触发对方同样惰性地计算自己的指纹，递归由此展开；`noinline` 是可选属性，缺省按 `False` 参与拼接）。

名字解析与记录：[python/asc/codegen/dependencies_finder.py:L119-L141](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/dependencies_finder.py#L119-L141)

```python
def visit_Name(self, node):
    if isinstance(node.ctx, ast.Store):
        return node.id
    if node.id in self.local_names:
        return None                      # 局部名遮蔽全局名
    def name_lookup(name):
        val = self.globals.get(name, None)
        if val is not None:
            return val, self.globals
        val = self.nonlocals.get(name, None)
        ...
    val, var_dict = name_lookup(node.id)
    if node.id in self.supported_python_builtins:
        return val
    self.record_reference(val, var_dict, node.id)
```

查找顺序与 Python 一致：全局 → 闭包。`local_names` 由 `visit_FunctionDef`（形参，[L159-L162](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/dependencies_finder.py#L159-L162)）、`visit_Assign`/`visit_AnnAssign`（赋值目标，[L215-L232](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/dependencies_finder.py#L215-L232)）、`visit_For`（循环变量，[L234-L239](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/dependencies_finder.py#L234-L239)）共同维护——所以 kernel 内部 `G = x` 之后再引用 `G` 不会被误判为全局依赖。

属性访问的豁免：[python/asc/codegen/dependencies_finder.py:L148-L157](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/dependencies_finder.py#L148-L157) 中 `visit_Attribute` 逐层下钻到根名字，若根是受支持模块（如 `asc`、`typing`）则直接返回 `None`——所以 kernel 里满篇的 `asc.add(...)`、`asc.data_copy(...)` 不会把整个 `asc` 包记成依赖。

默认值的特殊语义：[python/asc/codegen/dependencies_finder.py:L164-L204](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/dependencies_finder.py#L164-L204) 的 `visit_arguments` 在访问默认值表达式时置起 `visiting_arg_default_value` 标志，`record_reference` 据此跳过记录（[L112-L113](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/dependencies_finder.py#L112-L113)）。理由写在注释里：Python 默认参数只在 `def` 时求值一次，之后全局怎么变都与它无关。

把依赖分析接到代码生成：`FunctionVisitor` 构造时把 `fn.__globals__` 与 ConstExpr 形参合并进 `NameScope`（[python/asc/codegen/function_visitor.py:L84](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L84)），`visit_Name` 解引用后经 `ConstExpr.unwrap` 取值（[function_visitor.py:L635-L639](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L635-L639)），再由 `materialize_ir_value` 落成 IR 常量（u2-l3 讲过）。**这就是「全局变量在编译期被固化」的机制**：`G = 10` 时编译的 kernel，10 已经写死在 IR/Ascend C 里——与 4.3 的陷阱互为因果。

#### 4.4.4 代码实践：规格主任务——G=10 改 G=20，缓存会失效吗？

1. **实践目标**：用真实 kernel 与 `PYASC_CACHE_DIR` 观察「普通全局变量改值」是否触发重编译，并用 `dependencies_finder` 的源码逻辑解释现象。
2. **操作步骤**：
   - 步骤一：准备 `global_dep.py`（可仿照 `examples/01_add/add.py` 的结构，Model 模式即可）：

     ```python
     # 示例代码
     import torch
     import asc

     G = 10   # 模块级普通全局变量

     @asc.jit
     def add_global(x, y, z, block_length: asc.ConstExpr[int]):
         offset = asc.get_block_idx() * block_length
         x.set_global_buffer(z + offset * 8, block_length)
         ...
         # 在计算中使用 G，例如 asc.add(y_local, x_local, G_value, ...)
         # 具体baseline请直接复制 examples/01_add/add.py 后改造

     asc.config.set_platform("Model", "Ascend910B1")   # 无 NPU 用仿真器
     # ... 分配 tensor、kernel[USE_CORE_NUM, stream](...) 启动、校验结果
     ```

   - 步骤二：设缓存目录并首次运行：

     ```bash
     export PYASC_DUMP_PATH=/tmp/dump
     export PYASC_CACHE_DIR=/tmp/pyasc-cache
     python3 global_dep.py          # 第 1 次（G=10）
     ls /tmp/pyasc-cache | wc -l    # 记录缓存目录条目数
     ```

   - 步骤三：**同进程内**改值验证：在脚本末尾追加 `G = 20` 后再启动一次 kernel，计时对比两次启动耗时；
   - 步骤四：**跨进程**改值验证：把源文件里的 `G = 10` 改成 `G = 20`，重新运行整个脚本，观察缓存目录条目数与耗时、并核对结果数值是否等于「加 20」的期望；
   - 步骤五：对照实验——把 `G` 改成 `G = asc.ConstExpr(10)` 再改成 `asc.ConstExpr(20)` 各跑一次（记得让 kernel 体内引用它），重复步骤四的观察。
3. **需要观察的现象**：
   - 步骤三：第二次启动几乎零编译耗时（命中内存缓存）；
   - 步骤四：缓存目录**没有新增条目**、没有重编译日志，且结果数值仍是按 `G=10` 算的（旧 `.o` 被复用，10 已固化在二进制里）；
   - 步骤五：`ConstExpr` 版本改值后出现**新的缓存目录条目**并发生重编译。
4. **预期结果**（依据 4.3.3/4.4.3 的源码分析）：
   - `G` 是普通 int：`record_reference` 把它 deepcopy 记进 `used_global_vals`，但 [function.py:L83-L85](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L83-L85) 的筛选条件 `isinstance(val, ConstExpr)` 把它排除在 `cache_key` 之外；`cache_factors` 也不含它——两级 key 均不变，不重编译；
   - `G` 是 `ConstExpr`：进入 `cache_key` → 值变 key 变 → 重编译；
   - 本实践涉及真机/仿真器运行，具体日志与计时数字**待本地验证**；纯逻辑部分已由 4.3.4 的无 NPU 探针覆盖。
5. **结论写法**：把观察到的三类情形（同进程改值 / 跨进程改普通值 / 跨进程改 ConstExpr 值）整理成一张「现象 → 源码依据（文件:行号）」对照表。

#### 4.4.5 小练习与答案

**练习 1**：kernel A 内联调用 kernel B（两者都是 `@asc.jit`），只改 B 的源码，A 的缓存会失效吗？写出哈希传递路径。
**答案**：会。A 计算 `cache_key` 时，`DependenciesFinder.visit_Name` 发现名字 B 的值是 `Function` 实例 → `record_reference` 分支三 → `update_hash(B)` → `self.hasher.update(B.cache_key)`。B 源码变 → B 的 `finder.ret` 变 → B 的 `cache_key` 变 → A 的 hasher 内容变 → A 的 `cache_key` 变 → A 的文件缓存 key 变。

**练习 2**：为什么 `used_global_vals` 的键是 `(名字, id(全局字典))` 而不只是名字？
**答案**：不同模块（或不同动态创建的命名空间）可以有同名全局变量；只按名字会错误合并。键里加入 `id(__globals__)` 后，`模块1.G` 与 `模块2.G` 是两条独立记录，互不干扰。

**练习 3**：kernel 里写了 `def kernel(x, n=SOME_GLOBAL)`（默认值引用全局），把 `SOME_GLOBAL` 从 5 改成 9 再跑，会重编译吗？
**答案**：不会。`visit_arguments` 访问默认值时置起 `visiting_arg_default_value`，`record_reference` 据此直接返回、不记录（Python 默认值本来也只在 `def` 时求值一次）。真正的风险反而是：默认值在装饰那一刻已固化为 5，改全局根本影响不到它——想让它可变，应改成显式的 `ConstExpr` 形参。

## 5. 综合实践

制作一份属于你自己的「pyasc 缓存失效矩阵报告」：

1. **任务**：编写一个探针脚本 `cache_matrix.py`，把 4.3.4 的纯 Python 探针与 4.4.4 的真实 kernel 实验合并，系统覆盖下表左列的 6 种变化，每种变化记录三个数据：`Function.cache_key` 是否变化、内存缓存是否命中（两次启动耗时对比）、`PYASC_CACHE_DIR` 是否新增目录：

   | 变化 | cache_key | 重编译 | 结果是否正确 |
   |---|---|---|---|
   | kernel 源码 +1 字符 | 待验证 | 待验证 | — |
   | kernel 上方加一行注释 | 待验证 | 待验证 | — |
   | 子函数源码变化 | 待验证 | 待验证 | — |
   | 普通全局 G 10→20（跨进程） | 待验证 | 待验证 | **重点关注** |
   | ConstExpr 全局 10→20 | 待验证 | 待验证 | — |
   | ConstExpr 形参 4→8 | 待验证 | 待验证 | — |

2. **要求**：
   - 每一行结论必须附源码依据（文件:行号），例如「普通全局不进 key」依据 [function.py:L83-L85](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L83-L85) 的 `isinstance(val, ConstExpr)` 筛选；
   - 对「普通全局 G 10→20」一行，额外记录结果数值，验证旧 kernel 复用导致的行为；
   - 无 NPU 环境时，`cache_key` 列可用纯 Python 探针完成，kernel 列标注「待本地验证」。
3. **产出**：一页 Markdown 报告 + 一条给你的团队的使用守则（提示：**凡是会被 kernel 引用的模块级常量，一律用 `asc.ConstExpr` 包装，或改为 ConstExpr 形参传入**）。

## 6. 本讲小结

- `Function` 基类在**装饰时**（模块导入时）一次性完成源码抓取、AST 捕获与位置记录，`JITFunction.__init__` 第一句 `super().__init__(fn)` 即触发；之后进程内源码视为不可变。
- `get_function_node` 用「dedent + 正则定位 `^def` + ast.parse + 双重校验」把含装饰器的源码文本变成干净的 `FunctionDef`；`parse()` 保留重解析入口以支持单测替换 `self.src`。
- `cache_key = sha256(源码哈希 ‖ 起始行号 ‖ ConstExpr 全局依赖清单)`，惰性计算、只算一次、用占位符打断递归；它只参与**文件缓存** key（与 `pyasc_key`、`cache_factors` 共同决定缓存目录），内存缓存 key 只用 `cache_factors`。
- `DependenciesFinder` 以访问器模式完成两件事：把被调 `@asc.jit` 子函数的 `cache_key` 递归混入自己的 hasher（传递失效）；把引用到的全局值 deepcopy 记入 `used_global_vals`（键含 `id(__globals__)`，默认值位置豁免，`asc.language`/`typing` 符号豁免）。
- **陷阱**：普通全局变量的值不进任何缓存 key，也没有启动时校验（全仓检索无消费方，docstring 描述的是未实现的设计意图）——改值后旧 kernel 被静默复用；安全做法是 `asc.ConstExpr` 包装或改为 ConstExpr 形参。
- 唯一的全局值运行时检查在 `update_hash` 的冲突检测里：kernel 与子函数对同名全局持有不同值时直接 `RuntimeError`。

## 7. 下一步学习建议

下一讲 **u3-l3 参数特化：运行时参数与 ConstExpr 的分流** 将补全缓存 key 的最后一块拼图 `cache_factors`：`Specialization` 如何给每个实参定型（`PlainArgType`/`PointerArgType`/`StructArgType`），以及为什么 `str`/`list`/`dict` 不能做运行时参数。

继续阅读源码的建议顺序：

1. [python/asc/codegen/specialization.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/specialization.py)——参数类型分类；
2. [python/asc/runtime/jit.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py) 的 `get_arg_type` 与 `split_args`（后者在 [function.py:L119-L132](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L119-L132)）；
3. 如果你想提前看 AST 如何变成 IR，可以预习 u4-l1 的主角 [python/asc/codegen/function_visitor.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py)——本讲已多次出现的 `visit_Name`/`NameScope` 正是它的地基。
