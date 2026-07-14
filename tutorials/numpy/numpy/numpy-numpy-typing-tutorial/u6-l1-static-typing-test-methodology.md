# 静态类型测试方法论：mypy reveal/pass/fail

## 1. 本讲目标

本讲是单元 6「测试体系与综合实战」的第一篇，把视角从「读类型别名」切换到「验证类型别名是否正确」。

读完本讲，你应当能够：

1. 说清 NumPy 是如何把**静态类型检查器 mypy 当成一个库来调用**的，以及为什么这套测试默认是关闭的。
2. 区分 `data/` 下的四类夹具目录 `pass` / `fail` / `reveal` / `misc`，并说清每一类编码的是「哪一种期望」。
3. 读懂 `test_pass` / `test_reveal` / `test_code_runs` 三个断言函数，理解它们各自从 mypy 输出里提取什么、又在什么条件下让测试失败。
4. 区分 `reveal_type`（人看的 note）与 `assert_type`（机器校验的 error），理解为什么目录叫 `reveal` 而现代夹具却几乎全用 `assert_type`。
5. 仿照 `reveal/` 风格，亲手写一个 `.pyi` 夹具，并用 `assert_type` 固化某个表达式的期望返回类型。

## 2. 前置知识

本讲默认你已经学完 u2-l1（ArrayLike）与 u2-l3（NDArray 与形状/元素类型泛型）。这里只补三个本讲特有的基础概念，全部用通俗语言：

- **静态类型检查 vs 运行时测试**。普通 pytest 测试是「运行代码，看它抛不抛异常、返回值对不对」——这只能覆盖**运行时行为**。而类型注解写在签名上，运行时往往被忽略（甚至被 `# type: ignore` 屏蔽），**注解写错了，运行时不会报错**。所以类型子系统需要一套独立的「静态测试」：让类型检查器去推演类型，再把推演结果与期望比对。NumPy 的这套测试就架在 mypy 上。

- **把 mypy 当库调用（mypy api）**。我们平时是在命令行敲 `mypy foo.py`；但 mypy 还提供一个 Python 模块 `mypy.api`，其 `api.run(argv)` 接收一个参数列表，返回三元组 `(stdout, stderr, exit_code)`。这样测试代码就能在进程内直接拿到 mypy 的文本输出，再用字符串处理把它解析成「每个文件、每条错误」。

- **夹具（fixture）文件**。这里说的「夹具」是指 `data/` 目录下那几百个 `.py` / `.pyi` 小文件——它们不是被 import 的产品代码，而是「喂给 mypy 检查的样本」。每个文件就是一条测试用例，文件名就是用例名。`pytest` 用参数化（`parametrize`）把这些文件逐个喂给断言函数。

> 名词提醒：本讲反复出现「reveal」「assert」「pass」「fail」，它们既是**目录名**、又是**mypy/typing 的函数名**、又是**测试函数名**。三者经常不是一一对应——这是本讲最容易绊倒人的地方，4.3、4.5 两节会专门讲清映射关系。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [numpy/typing/tests/test_typing.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_typing.py) | 测试主体。定义「跑一次 mypy、解析输出、对四类夹具分别断言」的全部逻辑。 |
| [numpy/typing/tests/data/mypy.ini](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/mypy.ini) | 专供这套测试用的 mypy 配置：开 `strict`，并启用 `deprecated`、`ignore-without-code`、`truthy-bool` 三个错误码。 |
| [numpy/typing/tests/data/pass/array_like.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/pass/array_like.py) | `pass/` 样本：真实 `.py`，示范「合法且类型正确」的 ArrayLike 用法，同时会被运行时执行。 |
| [numpy/typing/tests/data/fail/array_like.pyi](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/fail/array_like.pyi) | `fail/` 样本：`.pyi`，每条「应当报错」的行都用 `# type: ignore[错误码]` 钉死期望错误。 |
| [numpy/typing/tests/data/reveal/arithmetic.pyi](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/arithmetic.pyi) | `reveal/` 样本：`.pyi`，用 `assert_type(表达式, 期望类型)` 固化运算返回类型。 |
| [numpy/typing/tests/data/reveal/nbit_base_example.pyi](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/nbit_base_example.pyi) | `reveal/` 的小型范例：示范 `assert_type` 配合精度并集的用法。 |
| [numpy/typing/tests/data/fail/nested_sequence.pyi](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/fail/nested_sequence.pyi) | 全仓库唯一使用 `reveal_type` 的夹具，用于对照 `reveal_type` 与 `assert_type` 的差异。 |

> 约定：本讲所有永久链接均指向当前 HEAD `9559a6b1ac93610711d8f1243f8c949fca4420bb`。

---

## 4. 核心概念与源码讲解

### 4.1 把 mypy 当库调用：`run_mypy` 夹具与门控

#### 4.1.1 概念说明

普通项目验证类型，靠的是开发者本地或 CI 里手动跑一次 `mypy`。但 NumPy 的类型测试更进一步：它**在 pytest 进程内、通过 `mypy.api.run(...)` 直接调用 mypy**，把 mypy 的标准输出抓回来，再用字符串处理切分成「按文件归类的错误清单」。

这样做有两个好处：

1. **整批跑、整批校验**：一次 `api.run` 处理一整个目录（比如 `reveal/` 下几十个文件），mypy 内部还能复用缓存，比逐文件启动进程快得多。
2. **可断言**：拿到结构化输出后，就能写成 `pytest.fail("reveal mismatch: ...")` 这样的断言，让类型回归进入 pytest 报告。

但 mypy 很慢——源码注释里直言「这些测试在 macOS M1 上都要跑一分钟以上」。所以整套测试**默认关闭**，要靠环境变量显式打开。

#### 4.1.2 核心流程

整个「跑 mypy」的流程集中在模块级 fixture `run_mypy` 里，伪代码如下：

```
1. 读门控开关 RUN_MYPY（由环境变量 NPY_RUN_MYPY_IN_TESTSUITE 决定）
   └─ 没开  → pytestmark 整体跳过本文件所有测试
2. run_mypy fixture（模块级、autouse、只跑一次）：
   a. 清掉旧的 .mypy_cache（除非 NUMPY_TYPING_TEST_CLEAR_CACHE=0）
   b. 对 pass / reveal / fail / misc 四个目录各跑一次 api.run([...])
   c. 检查：有 stderr → 失败；exit_code 不是 0/1 → 失败（0=无错，1=有错，2=mypy 崩溃）
   d. 逐行解析 stdout：丢弃所有 note: 行，按 caret 行（^^^~~~）切块，
      归入全局字典 OUTPUT_MYPY[文件名]
3. 各 test_* 函数从 OUTPUT_MYPY 取自己关心的目录做断言
```

mypy 的退出码语义是关键：

| exit_code | 含义 | 测试对待 |
|-----------|------|----------|
| 0 | 干净，无任何错误 | 正常 |
| 1 | 发现了错误（这是**预期的**，fail/reveal 目录本来就该有错误） | 正常 |
| 2 | mypy 自身崩溃 / 致命错误 | `pytest.fail` |

注意第 2 行：**「目录里有错误」不是失败，「mypy 崩溃」才是失败**。错误本身的存在性、错误码、错误位置，是后续 `test_pass` / `test_reveal` 去逐文件校验的，而不是在 fixture 里一刀切。

#### 4.1.3 源码精读

**门控开关**——位于文件顶部，决定整套测试是否运行：

[numpy/typing/tests/test_typing.py:14-22](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_typing.py#L14-L22) —— `RUN_MYPY` 读环境变量 `NPY_RUN_MYPY_IN_TESTSUITE`；`pytestmark` 是模块级标记，`skipif(not RUN_MYPY, ...)` 会**跳过本文件里所有测试函数**，这是「默认关闭」的总闸。

[numpy/typing/tests/test_typing.py:25-30](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_typing.py#L25-L30) —— 第二道闸：尝试 `from mypy import api`，失败则 `NO_MYPY = True`。每个 `test_*` 还各自带 `@pytest.mark.skipif(NO_MYPY, reason="Mypy is not installed")`，所以「没装 mypy」也只会跳过、不会报错。

**目录与缓存常量**——

[numpy/typing/tests/test_typing.py:37-47](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_typing.py#L37-L47) —— 把四个夹具目录、mypy 配置文件、缓存目录都解析成绝对路径；`OUTPUT_MYPY` 是一个 `defaultdict[str, list[str]]`，键是文件名、值是该文件所有错误块的列表，由 `run_mypy` 填充。

**`run_mypy` fixture 本体**——这是本模块的引擎：

[numpy/typing/tests/test_typing.py:71-114](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_typing.py#L71-L114) —— `@pytest.fixture(scope="module", autouse=True)` 两个修饰词很关键：`scope="module"` 表示**整个测试文件只跑一次**（而不是每个测试函数跑一次），`autouse=True` 表示**无需在测试里显式声明参数就会自动执行**。它做了三件事：

- L81-85：清缓存（可用 `NUMPY_TYPING_TEST_CLEAR_CACHE=0` 跳过，便于本地反复跑时复用缓存）。
- L88-100：对四个目录各 `api.run(["--config-file", MYPY_INI, "--cache-dir", CACHE_DIR, directory])`；stderr 非空或 exit_code 不在 `{0, 1}` 则 `pytest.fail`。
- L102-114：逐行解析 stdout——**凡是含 `"note:"` 的行一律 `continue` 跳过**（这是 `reveal_type` 输出被丢弃的根本原因，详见 4.3）；遇到匹配 `split_pattern`（caret 行 `^^^~~~`）就把累积的文本作为一个错误块塞进 `OUTPUT_MYPY[filename]`。

caret 切块正则与两个解析辅助函数：

[numpy/typing/tests/test_typing.py:87](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_typing.py#L87) —— `split_pattern = re.compile(r"(\s+)?\^(\~+)?")` 匹配 mypy `pretty=True` 模式下、错误下方那一行用 `^` 与 `~` 画出的「定位条」。一条多行错误以 caret 行收尾，解析器据此把错误切块。

[numpy/typing/tests/test_typing.py:50-63](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_typing.py#L50-L63) —— `_key_func` 从一行 mypy 输出里抠出文件名（按第一个 `:` 切分，并用 `os.path.splitdrive` 避开 Windows 盘符 `C:`）；`_strip_filename` 则抠出行号和消息文本，供 `test_pass`/`test_reveal` 拼接失败信息。

#### 4.1.4 代码实践

**实践目标**：亲手验证「默认关闭」与「mypy api 可被当库调用」这两件事。

**操作步骤**：

1. 在 NumPy 仓库根目录，不设置任何环境变量，直接跑：
   ```
   python -m pytest numpy/typing/tests/test_typing.py -v
   ```
2. 观察输出：所有用例应当是 `SKIPPED`，原因是 `NPY_RUN_MYPY_IN_TESTSUITE not set`。
3. 再写一个**示例脚本**（非项目原有代码，仅为理解 api）：
   ```python
   # 示例代码：演示 mypy.api.run 的返回值结构
   import os
   os.environ.setdefault("MYPYPATH", "")
   from mypy import api
   stdout, stderr, exit_code = api.run([
       "--config-file", "numpy/typing/tests/data/mypy.ini",
       "numpy/typing/tests/data/reveal/nbit_base_example.pyi",
   ])
   print("exit_code:", exit_code)
   print("stderr:", stderr)
   print("stdout:", stdout)
   ```

**需要观察的现象**：第 1 步全是 SKIPPED；第 3 步的 `exit_code` 应为 `0`（因为该 reveal 夹具的 `assert_type` 全部成立），`stdout` 接近空，`stderr` 为空。

**预期结果**：你确认了「不设环境变量整套测试被跳过」与「`api.run` 返回三元组、退出码 0 表示该文件无类型问题」。

> 待本地验证：第 3 步的精确 stdout 内容依赖你机器上已安装的 numpy 与 mypy 版本，建议实际跑一次记录。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `run_mypy` 用 `scope="module"` 而不是默认的 `scope="function"`？

> **参考答案**：因为 mypy 很慢（注释说一分钟起步），而它对四个目录的检查结果对所有 `test_*` 函数都通用。`scope="module"` 让它在整个测试文件内只执行一次，结果存进模块级 `OUTPUT_MYPY` 字典复用；若用 `scope="function"`，每个测试函数都会重跑一遍 mypy，耗时翻几十倍。

**练习 2**：mypy 退出码分别是 0、1、2 时，`run_mypy` 各会怎样？

> **参考答案**：0（无错）与 1（有错）都被视为「mypy 正常工作」，fixture 不失败，把错误解析进 `OUTPUT_MYPY` 留给后续断言；2（mypy 崩溃）会触发 `pytest.fail(f"Unexpected mypy exit code: {exit_code}")`。

---

### 4.2 四类夹具目录：四种「期望」

#### 4.2.1 概念说明

`data/` 下有四个子目录：`pass`、`fail`、`reveal`、`misc`。它们都是「喂给 mypy 的样本」，但**编码的期望完全不同**。理解本节的关键，是抓住一句话：**同一个「mypy 输出」在不同目录里意味着不同的成败**。

| 目录 | 文件类型 | 期望（mypy 应当怎样对待它） | 谁来断言 |
|------|----------|------------------------------|----------|
| `pass/` | `.py`（真代码） | **零错误**，且运行时也能跑 | `test_pass`（零错误）+ `test_code_runs`（运行时） |
| `fail/` | `.pyi`（桩） | **必须有指定错误**，且错误码被 `# type: ignore[code]` 钉死 | `test_pass`（钉死的错误码若失配则失败） |
| `reveal/` | `.pyi`（桩） | **运算返回类型必须等于期望**，用 `assert_type` 断言 | `test_reveal`（任何 assert-type 错误即失败） |
| `misc/` | `.pyi`（桩） | **mypy 不崩溃即可**（平台相关，不强求逐条） | 仅 `run_mypy` 校验 exit_code ∈ {0,1} 且 stderr 为空 |

#### 4.2.2 核心流程

四类目录的「成败判定」可以这样对比（设某文件经过 mypy 后产出的错误集合为 \(E\)）：

- `pass/`：期望 \(E = \varnothing\)。只要 \(E\) 非空 → 失败。
- `fail/`：每条「期望报错」的行都挂了 `# type: ignore[code]`。mypy 的行为是——**如果该行真有且仅有 `code` 这个错误，则该错误被忽略、该行不再进入 \(E\)**；反之：
  - 该行根本没错误 → mypy 报 `unused 'type: ignore' comment`（因为 `strict` 启用了 `warn-unused-ignores`）；
  - 该行错误码对不上 → 真错误未被忽略，仍进入 \(E\)。
  
  于是「\(E\) 恰好为空」当且仅当「每条 `# type: ignore[code]` 都精确命中」。复用 `test_pass` 同一套「\(E\) 非空则失败」的逻辑，就把 fail 目录变成了**精确的错误预言机**。
- `reveal/`：每行 `assert_type(x, T)` 在类型不符时报 `error: ...assert-type...`。只要 \(E\) 非空 → 失败。
- `misc/`：不关心 \(E\) 的内容，只关心 mypy 别崩溃。

\(E\) 这个集合的「空与非空」由 `OUTPUT_MYPY[文件名]` 是否存在来体现——没有任何错误的文件**根本不会**出现在 `OUTPUT_MYPY` 里（因为没有错误行被解析进去）。

#### 4.2.3 源码精读

**mypy 配置**——四类夹具之所以能如此精确，配置文件功不可没：

[numpy/typing/tests/data/mypy.ini:1-8](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/mypy.ini#L1-L8) —— `strict = True`（启用 `warn-unused-ignores` 等，这是 fail 夹具能钉死错误码的前提）；`enable_error_code = deprecated, ignore-without-code, truthy-bool`：

- `ignore-without-code`：**禁止裸的 `# type: ignore`**，必须写 `# type: ignore[具体码]`，否则自身报错——这逼着 fail 夹具把错误码写全。
- `deprecated`：开启 PEP 702 `@deprecated` 装饰器检查（与 NBitBase 弃用、u5-l4 呼应），所以夹具里常出现 `# type: ignore[deprecated]`。
- `pretty = True` + `show_absolute_path = True`：让错误带 caret 定位条、路径为绝对路径，供 `split_pattern` 切块与 `os.path.relpath` 还原相对路径。

**`pass/` 样本**——真实可运行的合法代码：

[numpy/typing/tests/data/pass/array_like.py:4-15](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/pass/array_like.py#L4-L15) —— 一连串 `xN: ArrayLike = ...` 赋值，覆盖 bool / int / float / complex / 标量 / 数组 / list / tuple / str / memoryview。在严格 mypy 下这些**都必须零错误**，任何一条失败（比如类型系统不接受的输入）都会让 `test_pass` 报错。

[numpy/typing/tests/data/pass/array_like.py:18-23](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/pass/array_like.py#L18-L23) —— 自定义类 `A` 实现 `__array__`，于是它的实例可作为 `ArrayLike`（这正是 u3-l1 的 `_SupportsArray` 协议在运行时的体现）。

[numpy/typing/tests/data/pass/array_like.py:34-37](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/pass/array_like.py#L34-L37) —— 「逃生舱」：生成器本会被类型系统拒绝（见 u2-l1），这里先用 `object` 标注再传给 `np.array`，绕过 ArrayLike 的严格检查来制造 object 数组。

**`fail/` 样本**——把期望错误用 `# type: ignore[code]` 钉死：

[numpy/typing/tests/data/fail/array_like.pyi:6-8](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/fail/array_like.pyi#L6-L8) —— 三行各故意把「不该作为 ArrayLike」的对象赋给 ArrayLike：生成器、无 `__array__` 的裸类、dict。每行挂 `# type: ignore[assignment]`，声明「这里**必然**报 assignment 错」。若将来 NumPy 的 ArrayLike 放宽到接受其中某一种，这条 ignore 会变成「unused」→ `test_pass` 失败，**测试随即捕获到行为漂移**。

[numpy/typing/tests/data/fail/array_like.pyi:10-15](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/fail/array_like.pyi#L10-L15) —— 同理钉死 `call-overload` 与 `arg-type` 错误码。

**`reveal/` 样本**——用 `assert_type` 固化运算结果类型：

[numpy/typing/tests/data/reveal/arithmetic.pyi:67-76](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/arithmetic.pyi#L67-L76) —— `assert_type(AR_number - AR_number, npt.NDArray[np.number])` 断言「两个 `NDArray[np.number]` 相减仍是 `NDArray[np.number]`」；接下来一整片 `assert_type` 把「布尔数组减某类型 list → 得到何种 NDArray」的类型提升规则全部固化（如 `AR_b - AR_LIKE_f` 结果是 `NDArray[np.floating]`）。这些正是 u2-l3 讲的 NDArray 元素类型在运算中如何被推断。

[numpy/typing/tests/data/reveal/nbit_base_example.pyi:7](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/nbit_base_example.pyi#L7) 与 [numpy/typing/tests/data/reveal/nbit_base_example.pyi:14-17](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/nbit_base_example.pyi#L14-L17) —— 一个极简范例：函数 `add` 用精度 TypeVar `T1`/`T2` 声明返回 `np.floating[T1 | T2]`，下面四行 `assert_type` 把「`add(f8,i8)`→`floating[_64Bit]`」「`add(f4,i8)`→`floating[_32Bit | _64Bit]`」钉死。这是 u4-l1/u4-l3 精度并集在测试层的落点。

**`misc/` 样本**——平台相关、不强求逐条：

[numpy/typing/tests/data/misc/extended_precision.pyi:6-9](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/misc/extended_precision.pyi#L6-L9) —— 用 `assert_type` 校验 `np.float96`/`np.float128` 等扩展精度标量。这些类型**并非所有平台都存在**，所以放进 `misc/`：在支持的平台 `assert_type` 成立、在不支持的平台可能报错，但因为 `misc/` 没有专门的逐文件断言函数（见 4.4），只要 mypy 不崩溃即可。

#### 4.2.4 代码实践

**实践目标**：直观感受「同一类错误在 pass 与 fail 目录里命运相反」。

**操作步骤**：

1. 打开 `data/pass/array_like.py` 第 4 行 `x1: ArrayLike = True`，想象把它改成 `x1: ArrayLike = {1: "foo"}`（dict，类型系统拒绝）。
2. 在命令行直接对该 pass 文件跑一次 mypy（**示例命令**，仅用于理解，未在本讲执行）：
   ```
   mypy --config-file numpy/typing/tests/data/mypy.ini numpy/typing/tests/data/pass/array_like.py
   ```
3. 对比打开 `data/fail/array_like.pyi` 第 8 行 `x3: ArrayLike = {1: "foo", 2: "bar"}  # type: ignore[assignment]`，对 fail 文件跑同样命令。

**需要观察的现象**：对 pass 文件，mypy 会报一条 `error: ... assignment`；对 fail 文件，因为挂了精确匹配的 `# type: ignore[assignment]`，mypy 静默通过（exit 0、无错误）。

**预期结果**：你看到「dict 赋给 ArrayLike」这一行为在 pass 目录里是失败信号、在 fail 目录里因被精确 ignore 而归于平静——这正是 4.2.2 里 \(E\) 是否为空的对比。

> 待本地验证：若想看 fail 夹具「失配即失败」的效果，可把第 8 行的码改成错的（如 `# type: ignore[misc]`），重跑 mypy，应看到「unused ignore」或真实 `assignment` 错误重新冒出。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `pass/` 目录里全是 `.py`，而 `fail/`、`reveal/`、`misc/` 里全是 `.pyi`？

> **参考答案**：`pass/` 的文件还要被 `test_code_runs` 在**运行时真正执行**一遍（见 4.4），所以必须是可运行的 `.py`；而 `fail/`/`reveal/`/`misc/` 只关心**类型层面**的检查结果，写成桩文件 `.pyi` 更轻、且能写「只声明类型不实现」的样本（如 `i8: np.int64` 这种纯类型标注的「变量」），便于集中表达类型期望。

**练习 2**：如果有人手滑把一个裸的 `# type: ignore`（不带错误码）写进 `fail/` 文件，会怎样？

> **参考答案**：因为 `mypy.ini` 启用了 `ignore-without-code`，mypy 会把这条裸 ignore 本身当作错误报出（`error: ... "type: ignore[...]" syntax` 类），于是该文件出现未预期的错误 → `test_pass` 失败。这等价于「禁止开发者偷懒不写错误码」。

---

### 4.3 `assert_type` vs `reveal_type`：机器校验与人看

#### 4.3.1 概念说明

这是本讲最反直觉、也最重要的一组对比。先把两个函数分清楚：

- **`reveal_type(x)`**——mypy 内建工具。mypy 遇到它会输出一行 `note: Revealed type is "..."`。**它只为「人类肉眼阅读」服务**，让你知道某个表达式被推断成了什么类型。它本身不构成断言。
- **`assert_type(x, T)`**——来自标准库 `typing`。它是一条**真正的断言**：mypy 把 `x` 的推断类型与 `T` 比对，**不一致就报 `error: ...assert-type...`**，一致则静默。这才是「可机器校验」的写法。

而 `run_mypy` fixture 里有这么一句：

```python
if "note:" in i:
    continue
```

**任何含 `note:` 的行都被丢弃**。`reveal_type` 的输出正是 `note:` 行，所以在本测试框架里 `reveal_type` 的揭示**永远不会被断言、永远不进 `OUTPUT_MYPY`**。

于是出现一个「名实不符」的现象：目录叫 `reveal/`（历史遗留，源于早期用 `reveal_type` 配合人工核对），但现代的 reveal 夹具**几乎全部改用 `assert_type`**。全仓库唯一还在用 `reveal_type` 的夹具是 `fail/nested_sequence.pyi`，而且它靠 `# type: ignore[...]` 来保证「即使有 note 之外的错误也被精确捕获」。

#### 4.3.2 核心流程

两类写法在测试框架里的命运对比：

```
reveal_type(x)  → mypy 产出  note: Revealed type is "T"
                 → 被 run_mypy 的 "note:" 过滤丢弃
                 → 不进 OUTPUT_MYPY → 永不失败（仅供人看）

assert_type(x, T) → 类型匹配 → 无输出 → 文件不在 OUTPUT_MYPY → test_reveal 通过
                   → 类型失配 → 产出 error: ...assert-type...
                              → 进 OUTPUT_MYPY → test_reveal 失败
```

简言之：**`reveal_type` 是调试探针，`assert_type` 是测试断言**。本框架选择「丢弃探针、保留断言」，所以你会在 reveal 目录看到铺天盖地的 `assert_type`、几乎看不到 `reveal_type`。

#### 4.3.3 源码精读

**note 过滤逻辑**：

[numpy/typing/tests/test_typing.py:104-114](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_typing.py#L104-L114) —— L105-106 的 `if "note:" in i: continue` 就是「丢弃所有 note 行」的源头。`reveal_type` 的揭示行因此被滤掉；只有 `error:` 行（含 `assert_type` 失配、未忽略的真实错误等）才会被累积、切块、归入 `OUTPUT_MYPY`。

**唯一用 `reveal_type` 的夹具**：

[numpy/typing/tests/data/fail/nested_sequence.pyi:13-17](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/fail/nested_sequence.pyi#L13-L17) —— 五行 `reveal_type(func(...))`，每行挂 `# type: ignore[arg-type, misc]`。注意：这里的**断言来源不是 reveal 的 note**（note 会被丢弃），而是 `func(a)` 在类型上不匹配 `_NestedSequence[int]` 参数所触发的 `arg-type` / `misc` 错误——靠 `# type: ignore` 钉死。`reveal_type` 在这里更像「给人类标注『这里在揭示什么』」的注释。

**`assert_type` 的典型用法（reveal 目录的主流写法）**：

[numpy/typing/tests/data/reveal/arithmetic.pyi:355-368](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/arithmetic.pyi#L355-L368) —— 一组一元运算的断言：`assert_type(-f16, np.floating[_128Bit])`、`assert_type(-c8, np.complex64)`、`assert_type(-AR_f, npt.NDArray[np.float64])` 等。每一行都是一条可被 mypy 自动校验的「返回类型契约」——若哪天 `-c8` 被推断成别的类型，这条 `assert_type` 立即失配，`test_reveal` 失败。

#### 4.3.4 代码实践

**实践目标**：用一条命令同时看到 `reveal_type`（note）与 `assert_type`（error）两种输出，理解谁会被框架丢弃。

**操作步骤**：

1. 写一个**示例桩文件**（非项目原有代码）`/tmp/probe.pyi`：
   ```python
   # 示例代码：对照 reveal_type 与 assert_type
   from typing import assert_type, reveal_type
   import numpy as np
   import numpy.typing as npt

   a: npt.NDArray[np.float64] = np.array([1.0, 2.0])
   reveal_type(a)                        # 人看：note 行
   assert_type(a, npt.NDArray[np.float64])   # 机器校验：成立 → 无输出
   assert_type(a, npt.NDArray[np.int64])     # 机器校验：失配 → error 行
   ```
2. 对它跑 mypy（**示例命令**）：
   ```
   mypy --config-file numpy/typing/tests/data/mypy.ini /tmp/probe.pyi
   ```

**需要观察的现象**：你会看到一条 `note: Revealed type is "ndarray[...float64]"`（来自 `reveal_type`），以及一条 `error: ...assert-type...`（来自第二条 `assert_type` 的失配）。

**预期结果**：这恰好解释了为什么 `run_mypy` 要过滤 `note:`——note 数量随 `reveal_type` 调用线性增长且无法构成断言，保留它只会污染 `OUTPUT_MYPY`；而 `error:` 行才是真正的失败信号。

> 待本地验证：不同 mypy 版本对 `reveal_type` 输出的类型字符串措辞略有差异，但「note vs error」的分类不变。

#### 4.3.5 小练习与答案

**练习 1**：既然 `reveal_type` 的输出被框架丢弃，为什么 `reveal/` 目录还保留这个名字？

> **参考答案**：历史原因。早期这套测试确实大量用 `reveal_type` 配合人工核对推断结果，目录因此得名。后来为可机器校验迁移到 `assert_type`，但目录名已固化、外部文档与讨论都引用它，遂保留。这是一个「命名滞后于实现」的典型案例。

**练习 2**：假设你把一条 `assert_type(x, T)` 写进 `pass/` 目录的某个 `.py` 文件，会发生什么？

> **参考答案**：`pass/` 由 `test_pass` 校验，它要求 \(E=\varnothing\)。若 `assert_type` 成立，无 error，通过；若失配，产生 `error: ...assert-type...`，`test_pass` 收集到该错误并 `pytest.fail`。也就是说 `assert_type` 在 pass 目录里同样生效，只是 pass 目录习惯不写断言、只写「应当合法」的代码。

---

### 4.4 三个断言函数与参数化：期望如何被编码

#### 4.4.1 概念说明

有了「四类夹具」与「`OUTPUT_MYPY` 缓存」，最后一步是用 pytest 把「每个文件」变成「一条用例」，并对每类目录套用不同的断言逻辑。这就是 `test_pass` / `test_reveal` / `test_code_runs` 三兄弟的职责，而把文件列表喂给它们的，是 pytest 的 `parametrize`。

关键术语 **pytest parametrize**：`@pytest.mark.parametrize("path", 用例列表)` 会为列表里每一项生成一条独立的测试用例，参数 `path` 就是该项的值。本框架用 `get_test_cases(目录...)` 遍历目录、把每个 `.py`/`.pyi` 包成 `pytest.param(fullpath, id=文件名)`，于是测试报告里每条用例都显示成可读的文件名（如 `test_reveal[arithmetic]`）。

#### 4.4.2 核心流程

三个函数的分工与判定逻辑：

```
get_test_cases(PASS, FAIL)  ─┐
                             ├─→ test_pass(path)
                             │     从 OUTPUT_MYPY 取该 path 的错误块
                             │     if path 不在 OUTPUT_MYPY: 直接 return（通过）
                             │     else: 收集所有错误 → pytest.fail
                             │     ※ pass 期望零错误；fail 期望「ignore 后零错误」
                             │
get_test_cases(REVEAL)     ──┤
                             ├─→ test_reveal(path)
                             │     同样取 OUTPUT_MYPY[path]
                             │     if 不在: return（通过，assert_type 全成立）
                             │     else: 格式化成 "reveal mismatch" → pytest.fail
                             │
get_test_cases(PASS)       ──┤
                             └─→ test_code_runs(path)
                                   用 importlib 把这个 .py 当模块执行一遍
                                   验证「类型合法的代码运行时也合法」
                                   ※ 不读 OUTPUT_MYPY，是纯运行时回测
```

注意三个细节：

1. **`if path not in output_mypy: return`** 是所有静态断言的「通过」捷径——没有错误可解析的文件，根本不会作为键出现在 `OUTPUT_MYPY` 里。
2. **`test_code_runs` 只覆盖 `PASS_DIR`**，因为只有 `pass/` 是可执行的 `.py`。
3. **`misc/` 没有专属断言函数**：它在 `run_mypy` 里被 mypy 处理过（保证不崩溃），但没有任何 `test_*` 消费它的 `OUTPUT_MYPY` 条目。

#### 4.4.3 源码精读

**参数化用例生成**：

[numpy/typing/tests/test_typing.py:117-128](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_typing.py#L117-L128) —— `get_test_cases` 用 `os.walk` 遍历目录，过滤出 `.py`/`.pyi`，把每个文件的完整路径包成 `pytest.param(fullpath, id=short_fname)`。`id=short_fname`（文件去扩展名）决定 pytest 报告里的用例名，例如 `test_pass[array_like]`。

**`test_pass`——pass 与 fail 共用的「零错误」校验**：

[numpy/typing/tests/test_typing.py:139-159](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_typing.py#L139-L159) —— `@pytest.mark.parametrize("path", get_test_cases(PASS_DIR, FAIL_DIR))` 把 pass 与 fail 的文件都喂进来；L146-147 的 `if path not in output_mypy: return` 是「无错误即通过」的捷径；L152-159 收集该文件所有错误块、用 `_strip_filename` 抠出「相对路径:行号 - 内容」并 `pytest.fail`。对 pass 文件，任何错误都意味失败；对 fail 文件，只有当 `# type: ignore[code]` 没精确命中（产生 unused-ignore 或真实错误）时才会留下错误、进而失败。

**`test_reveal`——assert_type 失配即失败**：

[numpy/typing/tests/test_typing.py:162-187](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_typing.py#L162-L187) —— 只对 `REVEAL_DIR` 参数化。docstring 直言其职责是「验证 mypy 正确推断返回类型」。同样用 `if path not in output_mypy: return` 作为通过捷径；若有错误块，则用 `_FAIL_MSG_REVEAL` 模板格式化成「reveal mismatch」信息（带文件名、行号、缩进的错误内容），`_FAIL_SEP`（79 个下划线）在多条失败间分隔，最后 `pytest.fail(reasons, pytrace=False)`——`pytrace=False` 表示只显示自定义消息、不打印 Python 堆栈，让失败报告聚焦在类型问题上。

**`test_code_runs`——运行时回测**：

[numpy/typing/tests/test_typing.py:190-207](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_typing.py#L190-L207) —— `@pytest.mark.parametrize("path", get_test_cases(PASS_DIR))` 只覆盖 pass 目录。L197-207 用 `importlib.util.spec_from_file_location` + `module_from_spec` + `exec_module` 把每个 `.py` 文件**当模块执行一遍**。两个 `@pytest.mark.filterwarnings("ignore::DeprecationWarning")`（L190、L193）说明 pass 样本里会触发一些弃用警告（如 `numpy.fix`），运行时回测有意忽略它们，只关心「能不能跑起来」。它完全不碰 `OUTPUT_MYPY`，是一道独立的运行时安全网。

#### 4.4.4 代码实践

**实践目标**：复刻 `test_reveal` 的核心判定，亲手验证一个 reveal 夹具能否通过。

**操作步骤**：

1. 写一个**示例 reveal 夹具** `/tmp/my_reveal.pyi`（仿 `reveal/` 风格）：
   ```python
   # 示例代码：一个最小的 reveal 风格夹具
   from typing import assert_type
   import numpy as np
   import numpy.typing as npt

   ar: npt.NDArray[np.int64]
   assert_type(ar + 1, npt.NDArray[np.int64])     # 应当成立
   assert_type(ar + 1.0, npt.NDArray[np.float64]) # 应当成立：int64 + float → float64
   ```
2. 用 mypy 检查它（**示例命令**）：
   ```
   mypy --config-file numpy/typing/tests/data/mypy.ini /tmp/my_reveal.pyi
   ```
3. 对照 `test_reveal` 的逻辑：mypy exit 0、stdout 无 `error:` → 等价于「`/tmp/my_reveal.pyi` 不在 `OUTPUT_MYPY`」 → `test_reveal` 会 `return`（通过）。
4. 故意把第二条改成 `assert_type(ar + 1.0, npt.NDArray[np.int64])`（错的），重跑，观察出现的 `error: ...assert-type...`，并想象它如何被 `_strip_filename` + `_FAIL_MSG_REVEAL` 格式化成失败信息。

**需要观察的现象**：第 2 步应无 error（通过）；第 4 步应出现一条 assert-type 错误，标出失配行号。

**预期结果**：你完整走通了一遍「写 reveal 夹具 → mypy 校验 → 映射到 `test_reveal` 的通过/失败判定」的链路，这正是本讲综合实践（第 5 节）的雏形。

> 待本地验证：`ar + 1.0` 的确切返回类型依赖 numpy/mypy 版本，请以本地实际 `assert_type` 结果为准；若推断为 `NDArray[np.floating]` 而非 `NDArray[np.float64]`，说明需把期望类型放宽——这恰好是 reveal 夹具「捕获推断变化」的价值。

#### 4.4.5 小练习与答案

**练习 1**：`test_code_runs` 为什么只覆盖 `PASS_DIR` 而不覆盖 `REVEAL_DIR`？

> **参考答案**：`reveal/` 全是 `.pyi` 桩文件，桩文件只有类型标注、没有可执行逻辑（很多「变量」只是 `i8: np.int64` 这种声明，根本无右值），用 `exec_module` 执行它们没有意义甚至会失败；而 `pass/` 是真实的 `.py`，运行时回测才成立。`test_code_runs` 的目的是「验证类型合法的代码运行时也合法」，这只有对 `.py` 才说得通。

**练习 2**：`test_pass` 既覆盖 pass 又覆盖 fail，它如何用同一套逻辑区分两种「期望」？

> **参考答案**：它不区分。两种期望在 `test_pass` 眼里统一成「该文件经过 mypy 后 `OUTPUT_MYPY[path]` 应为空」：pass 文件本就无错误；fail 文件因每条问题都挂了精确匹配的 `# type: ignore[code]` 而把错误全部抵消，结果同样为空。区分「期望零错误」与「期望被精确忽略的错误」的是**夹具文件的写法**，而非 `test_pass` 的代码。

**练习 3**：`misc/` 目录的文件如果包含一条 `assert_type` 失配，会让哪个测试失败？

> **参考答案**：**没有测试会因此失败**。`misc/` 既不在 `test_pass`/`test_reveal` 的参数化范围里，也没有专属断言函数；它的错误进了 `OUTPUT_MYPY` 却无人消费。只有当 misc 文件让 mypy 崩溃（exit 2）或产生 stderr 时，`run_mypy` fixture 才会失败。所以 `misc/` 是「冒烟测试」性质的目录。

---

## 5. 综合实践

把本讲的四块知识串起来，完成下面这个贯穿任务：**为 NumPy 的一个真实行为，亲手写一条可被 `test_reveal` 校验的类型回归用例**。

**任务背景**：NumPy 的算术运算有一套「类型提升」规则，例如「整数数组 `+` 浮点标量」会得到浮点数组。这种规则一旦在版本升级中发生变化，用户代码的类型推断就会静默出错。你要用本讲学到的 reveal 夹具机制，把它固化成一条回归测试。

**操作步骤**：

1. **选一个表达式**。参考 `reveal/arithmetic.pyi` 的风格，挑一个你认为「返回类型有保障、值得固化」的运算，例如 `int64 数组 // float32 数组` 或 `timedelta64 数组 / 标量`。
2. **写夹具**。仿照 [reveal/nbit_base_example.pyi](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/nbit_base_example.pyi) 的结构，先声明变量类型，再为该运算写一行 `assert_type(表达式, 期望类型)`。先用 `mypy --config-file numpy/typing/tests/data/mypy.ini 你的文件` 单独校验，**反复修正期望类型直到无 error**（这正是「发现真实推断类型」的过程）。
3. **解释校验链路**。用一段话说明：你这行 `assert_type` 在 `run_mypy` 里如何被解析、为什么 `note:` 过滤不影响它、它在 `test_reveal` 里如何映射到「通过」或「reveal mismatch 失败」。
4. **（进阶）制造一次失败**。故意把期望类型写错，跑一次 mypy，把 `error:` 输出按 `_strip_filename` 的规则人工拆成「行号 + 内容」，再套进 `_FAIL_MSG_REVEAL` 模板，预演 `test_reveal` 会给出的失败信息。

**预期结果**：你产出一个可放入 `reveal/` 目录的合规夹具，并完整复述了它「从一行 `assert_type` 到 pytest 报告里的用例」的全部旅程。若把它真的放进 `numpy/typing/tests/data/reveal/` 并设 `NPY_RUN_MYPY_IN_TESTSUITE=1` 跑 `test_reveal`，应能看到它以 `[你的文件名]` 形式出现并通过。

> 待本地验证：把自制夹具放进仓库并运行完整套件属于改动测试目录的操作，请在本地分支验证；本讲不假定你已执行。

## 6. 本讲小结

- NumPy 用 `mypy.api.run(...)` **把 mypy 当库调用**，在 pytest 进程内对 `pass/fail/reveal/misc` 四个目录批量检查；整套测试默认关闭，由 `NPY_RUN_MYPY_IN_TESTSUITE` 与「是否安装 mypy」两道闸门控制。
- `run_mypy` 是模块级、`autouse`、只跑一次的 fixture；它要求 mypy「不崩溃」（stderr 为空、exit ∈ {0,1}），把 stdout 里的 `error:` 行按 caret 切块归入 `OUTPUT_MYPY`，并**丢弃所有 `note:` 行**。
- 四类夹具编码四种期望：`pass` 期望零错误（且运行时可执行）、`fail` 用 `# type: ignore[code]` 把「必有此错误」钉死、`reveal` 用 `assert_type` 固化返回类型、`misc` 仅作冒烟（不逐条断言）。
- `mypy.ini` 的 `strict` + `ignore-without-code` 是 fail 夹具能精确预言错误码的前提；`deprecated` 错误码与 NBitBase 弃用呼应。
- `reveal_type` 产出 `note:`（仅供人看，被框架丢弃），`assert_type` 产出 `error:`（可机器校验）；目录虽叫 `reveal`，现代夹具几乎全用 `assert_type`。
- `get_test_cases` + `pytest.param(id=文件名)` 把每个夹具变成可读用例；`test_pass`/`test_reveal` 以「`path` 不在 `OUTPUT_MYPY` 即通过」为捷径，`test_code_runs` 用 `importlib` 对 `pass/` 做纯运行时回测。

## 7. 下一步学习建议

- **继续本单元**：下一讲 u6-l2「运行时类型测试与打包完整性测试」会转向另一类验证——用 `get_args`/`get_origin`/`get_type_hints` 在运行时内省 PEP 695 类型别名，以及 `test_isfile.py` 如何保证 `.pyi` 桩文件随包安装（承接 u1-l3 的 PEP 561）。
- **综合实战**：u6-l3 会让你综合 ArrayLike/DTypeLike/NDArray/`@overload` 为一个真实函数写完整类型注解，并补一个 reveal 夹具——本讲的 `assert_type` 校验链路是它的直接前置。
- **回看精度体系**：若你对 `reveal/arithmetic.pyi` 与 `reveal/nbit_base_example.pyi` 里的 `_64Bit`、`np.floating[_128Bit]` 等精度表达仍有疑问，建议复习 u4-l1（NBitBase 层次）与 u4-l3（现代 TypeVar/`@overload`），再回来重读这两个夹具，会有更深的体会。
- **动手阅读**：通读 [test_typing.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_typing.py) 全文（仅约 200 行），并随机挑一个 `reveal/` 夹具，尝试不跑 mypy、纯靠阅读预测每条 `assert_type` 的推断结果，再用 mypy 验证——这是熟悉 NumPy 类型提升规则最快的途径。
