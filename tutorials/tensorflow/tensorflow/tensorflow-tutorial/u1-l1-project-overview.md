# TensorFlow 项目定位与生态全景

> 本讲是 TensorFlow 学习手册的第一讲。如果你此前从没读过 TensorFlow（以下简称 TF）的源码，也不必担心——本讲只做一件事：让你在打开这个巨大仓库时不再迷路，知道它是什么、解决什么问题，以及「敲下 `import tensorflow as tf`」时究竟发生了什么。

---

## 1. 本讲目标

读完本讲，你应当能够：

- 用一句话说清 **TensorFlow 是什么**、它的定位与生态范围。
- 区分 TF 提供的 **Python / C++ / 移动端（TFLite）** 等多语言接口的分工。
- 读懂仓库根目录的 `README.md`，并从中找到安装与运行方式。
- 看懂 `tensorflow/__init__.py` 这个 Python 入口文件，理解它为什么这么短，以及它如何连接 Python 与底层 C++。
- 掌握一套**「从源码仓库快速建立全局认知」**的通用方法。

---

## 2. 前置知识

本讲尽量从零开始，但如果你具备以下任一背景，会读得更顺：

- 会一点 **Python**：知道 `import`、`from ... import ...` 的含义，知道 `del` 是删除一个变量。
- 听说过**机器学习 / 深度学习**这个词即可，不需要懂任何模型细节。
- 用过命令行（敲 `pip install`、运行 `python`）。

几个先解释的术语：

| 术语 | 通俗解释 |
| --- | --- |
| 机器学习平台（ML platform） | 一套覆盖「造数据 → 训练模型 → 部署推理」全流程的工具集合，TF 就是这样一个平台。 |
| 张量（Tensor） | 多维数组，是 TF 里数据的统一表达方式（标量、向量、矩阵都是它的特例）。 |
| op / kernel | op 是「操作」的声明（比如加法），kernel 是这个操作在某种设备上的具体实现。本讲只需建立直觉，细节留到第 4 单元。 |
| C++ 核心 / Python API | TF 用 C++ 写高性能计算内核，再用 Python 包一层好用的接口。 |

---

## 3. 本讲源码地图

本讲只涉及两个最小模块对应的文件，外加几个「入口级」文件作为认知锚点：

| 文件 | 作用 | 本讲用它做什么 |
| --- | --- | --- |
| `README.md` | 项目的「门面」，说明 TF 是什么、怎么装、怎么跑第一个程序 | 理解项目定位与生态 |
| `tensorflow/__init__.py` | `import tensorflow` 时第一个被执行的 Python 文件 | 理解 Python 入口与 C++ 桥梁 |
| `tensorflow/python/__init__.py` | 真正被 `__init__.py` 触发的子包入口 | 解释「入口为何这么短」 |
| `tensorflow/python/util/tf_export.py` | 给符号打标签、组装公开 API 的机制 | 解释 `tf.add` 这类名字从哪来 |
| 根目录 `configure.py` / `WORKSPACE` / `MODULE.bazel` | 构建/配置入口 | 练习中「找三个关键入口文件」 |

> 说明：本讲聚焦「定位与认知」，不对 op、kernel、图执行等做深入，这些会分散到后续单元。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **README：TF 的定位与生态**
2. **`tensorflow/__init__.py`：Python 入口与 C++ 桥梁**
3. **从源码仓库快速建立全局认知的方法**

---

### 4.1 README：TensorFlow 的定位与生态

#### 4.1.1 概念说明

打开任何开源项目，第一份该读的文档就是 `README.md`。它通常回答三个问题：

- 这个项目**是什么**？
- 它**解决什么问题**？
- 我**怎么开始**（安装、第一个例子、文档在哪）？

对 TF 而言，README 用一句话给出了最权威的定位。

#### 4.1.2 核心流程

阅读 README 时，建议按这个顺序抓重点：

1. **定位句**（一句话定义）。
2. **来源与背景**（谁做的、为什么做）。
3. **接口分层**（提供了哪些语言的 API，哪些是稳定 API）。
4. **安装方式**（pip 包、CPU/GPU 版本）。
5. **第一个可运行例子**（最小验证程序）。
6. **生态资源**（教程、模型、社区链接）。

抓住这六点，就建立起了项目的「骨架认知」。

#### 4.1.3 源码精读

**① 项目定位句**——这是全仓库最该记住的一句话：

[README.md:19-25](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/README.md#L19-L25) 把 TF 定义为一个 **end-to-end open source platform for machine learning**（端到端的机器学习开源平台），并强调它有一整套 tools（工具）、libraries（库）、community（社区）资源。「端到端」是关键词：它不只是写模型，而是覆盖从研究到部署的完整链路。

**② 来源背景**：

[README.md:27-30](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/README.md#L27-L30) 说明 TF 最初由 Google Brain 团队为机器学习与神经网络研究而开发，但又足够通用，可用于其他领域。知道它的出身，有助于理解它为什么「既面向研究、又面向工程」的双面性。

**③ 接口分层（关键）**：

[README.md:32-35](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/README.md#L32-L35) 明确说：

- **Python 和 C++ 是「稳定 API」**（stable API）。
- 其它语言（如早期 Java/Go/Swift 绑定）是 **non-guaranteed backward compatible**（不保证向后兼容）的。

这句话直接对应了仓库的目录划分：`tensorflow/python/`（Python 接口）和 `tensorflow/core/`（C++ 核心）是两块最核心、最稳定的代码。后文 TFLite（`tensorflow/lite/`）则对应移动端部署。

**④ 安装方式**：

[README.md:50-56](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/README.md#L50-L56) 给出最常见的安装命令 `pip install tensorflow`（含 GPU 支持），[README.md:61-65](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/README.md#L61-L65) 给出更小的纯 CPU 版本 `pip install tensorflow-cpu`。

**⑤ 第一个可运行例子**：

[README.md:76-87](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/README.md#L76-L87) 是官方的「Hello, World」：

```python
>>> import tensorflow as tf
>>> tf.add(1, 2).numpy()
3
>>> hello = tf.constant('Hello, TensorFlow!')
>>> hello.numpy()
b'Hello, TensorFlow!'
```

注意两点直觉：`tf.add(1, 2)` 直接得到结果 `3`（立即执行，不是先建图），`.numpy()` 把张量转成 Python/NumPy 值。这背后是 TF 2.x 默认的 Eager 执行模式，第 3 单元会专门讲。

#### 4.1.4 代码实践

**实践目标**：亲手跑通 README 的「Hello, World」，确认本机环境可用。

**操作步骤**：

1. 在命令行执行 `pip install tensorflow`（或 `pip install tensorflow-cpu`）。
2. 进入 Python 交互环境：`python`。
3. 逐行粘贴 [README.md:80-87](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/README.md#L80-L87) 的代码。

**需要观察的现象**：

- `tf.add(1, 2).numpy()` 是否输出 `3`。
- `hello.numpy()` 是否输出字节串 `b'Hello, TensorFlow!'`。

**预期结果**：与 README 完全一致。

> 注意：**待本地验证**。本讲编写环境为纯源码仓库，未实际执行上述 pip 安装与运行，请以你本机真实输出为准。如果安装或运行失败，先记录报错信息——后续讲义会带你从源码理解这些行为，而不是依赖预装包。

#### 4.1.5 小练习与答案

**练习 1**：README 说哪些语言的 API 是「稳定（stable）」的？
**答案**：Python 和 C++。见 [README.md:32-35](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/README.md#L32-L35)。

**练习 2**：`pip install tensorflow` 和 `pip install tensorflow-cpu` 的主要区别是什么？
**答案**：前者包含 CUDA GPU 卡支持（Ubuntu/Windows），后者是更小的纯 CPU 版本。见 [README.md:50-65](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/README.md#L50-L65)。

**练习 3**：用一句话写下 TensorFlow 与 PyTorch、JAX 的差异定位（开放式，见「综合实践」）。
**答案（参考）**：TF 强调「端到端 + 全平台部署（含 TFLite 边缘端）」的工程化平台；PyTorch 更偏「动态图、研究友好、Pythonic」；JAX 更偏「函数式、可组合的数值计算与可微分并行」。三句话都成立即可。

---

### 4.2 `tensorflow/__init__.py`：Python 入口与 C++ 桥梁

#### 4.2.1 概念说明

当你写 `import tensorflow as tf` 时，Python 解释器会执行包目录下的 `__init__.py`。所以 `tensorflow/__init__.py` 就是 **整个 Python 世界的入口**。

令人意外的是：这个入口文件**非常短**（算上注释和空行也只有三十多行）。本模块要回答的核心问题是——

> 入口这么短，那 `tf.add`、`tf.constant`、`tf.Variable` 这些函数都是从哪儿冒出来的？

理解了这一点，你才算真正「读懂」了 TF 的入口。

#### 4.2.2 核心流程

`import tensorflow as tf` 背后的关键步骤可概括为：

1. 执行 `tensorflow/__init__.py`。
2. 该文件导入 `pywrap_tensorflow`——它是连接 **Python 与底层 C++ 核心**的桥梁（pywrap 通常基于 pybind11 之类的机制绑定）。
3. 导入 `pywrap_tensorflow` 会触发 `tensorflow.python` 子包被加载，从而把整个 Python API 拉起来。
4. 文件末尾用 `del python`、`del core` 把「子包名」从公开命名空间里删掉，保持 `tf` 命名空间干净。
5. 真正的 `tf.xxx` 名字，是各模块用 `tf_export('xxx')` 装饰器注册后，由包构建机制组装进 `tf` 命名空间的。

用伪代码表示就是：

```
import tensorflow as tf
   │
   ▼ 执行 tensorflow/__init__.py
导入 pywrap_tensorflow  ──► 触发 tensorflow.python 加载 ──► 拉起全部 Python API
   │
   ▼ 各模块的 @tf_export('add') 把符号注册到 API 表
tf.add / tf.constant / ...  在构建/导入时被组装进 tf 命名空间
   │
   ▼ del python / del core
清理掉不想暴露给用户的子包名
```

#### 4.2.3 源码精读

**① 桥梁导入 pywrap_tensorflow**：

[tensorflow/__init__.py:20](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/__init__.py#L20)

```python
from tensorflow.python import pywrap_tensorflow  # pylint: disable=unused-import
```

这一行是「入口之所以能干活」的关键：`pywrap_tensorflow` 是一个编译好的扩展模块，把 C++ 核心（`tensorflow/core/`）的能力暴露给 Python。它被标了 `unused-import`，因为本文件并不直接使用它——导入它的**副作用**（拉起整个 `tensorflow.python`）才是目的。

**② flags 与 app**：

[tensorflow/__init__.py:22-24](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/__init__.py#L22-L24)

```python
from tensorflow.python.platform import flags
from tensorflow.python.platform import app
app.flags = flags
```

TF 自带一套命令行参数（flags）体系，`app` 是程序入口辅助模块（类似 absl）。这里把 `flags` 挂到 `app` 上，是历史遗留的兼容写法。

**③ 为什么要 `del python` / `del core`**：

[tensorflow/__init__.py:26-32](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/__init__.py#L26-L32)

```python
# These symbols appear because we import the python package which
# in turn imports from tensorflow.core and tensorflow.python. They
# must come from this module. So python adds these symbols for the
# resolution to succeed.
del python
del core
```

注释解释得很清楚：由于 `tensorflow` 包内部会互相导入 `tensorflow.core` 和 `tensorflow.python`，Python 会把 `core`、`python` 这两个子包名也挂到顶层 `tensorflow` 模块对象上。为了不让用户看到 `tf.python`、`tf.core`（这些不是公开 API），入口文件主动把它们 `del` 掉。这是一个**「清理公开命名空间」**的细节，却很能体现 TF 对 API 边界的谨慎。

**④ `tf.add` 等名字从哪来——tf_export 机制**：

[tensorflow/python/util/tf_export.py:19-31](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/util/tf_export.py#L19-L31) 展示了「导出一个符号」的标准写法：

```python
@tf_export('foo', 'bar.foo')
def foo(...):
  ...
```

`tf_export('foo', 'bar.foo')` 的作用是给函数/类打标签，登记它「应该以 `foo` 和 `bar.foo` 这两个名字出现在公开 API 里」。这些登记信息被收集进一张全局表：

[tensorflow/python/util/tf_export.py:81-85](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/util/tf_export.py#L81-L85)

```python
_NAME_TO_SYMBOL_MAPPING: dict[str, Any] = dict()

def get_symbol_from_name(name: str) -> Optional[Any]:
  return _NAME_TO_SYMBOL_MAPPING.get(name)
```

也就是说，整个 `tf` 命名空间是**由一张「名字 → 符号」的映射表组装出来的**，而不是在 `__init__.py` 里手写几百行 `from ... import ...`。这正是入口文件能这么短的根本原因。

**⑤ 为什么子包入口也尽量保持精简**：

[tensorflow/python/__init__.py:17-20](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/__init__.py#L17-L20) 有一条重要警告：

> Do not add code to //third_party/tensorflow/python/__init__.py. This file is imported whenever TensorFlow is imported. Additional imports in this file could cause the internal import time of TensorFlow to increase by multiple seconds.

大意是：这个文件在每次 `import tensorflow` 时都会执行，多加一行导入就可能让导入时间增加好几秒。所以入口文件「刻意保持极简」是一个**有意的性能决策**，而非疏忽。

> 关于「`__version__` 等信息」：[tensorflow/python/__init__.py:24-34](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/__init__.py#L24-L34) 通过 `_exported_dunders` 白名单导出 `__version__`、`__git_version__` 等特殊属性，这些是在构建时注入的。

#### 4.2.4 代码实践

**实践目标**：用运行时观察，验证「`del python/core`」与「`tf_export` 组装命名空间」这两件事。

**操作步骤**：

1. 安装好 tensorflow 后，进入 `python`。
2. 执行下面这段**示例代码**（标注为示例，非仓库原有）：

```python
import tensorflow as tf

# 观察 1：tf 命名空间里有没有暴露 python / core 子包？
print('python' in dir(tf))   # 预期 False（因为入口里 del python）
print('core'   in dir(tf))   # 预期 False（因为入口里 del core）

# 观察 2：tf.add 这类公开符号是否存在？
print(hasattr(tf, 'add'))       # 预期 True
print(hasattr(tf, 'constant'))  # 预期 True

# 观察 3：版本信息（构建时注入的 dunder）
print(tf.__version__)
```

**需要观察的现象**：

- 前两个 `print` 是否都为 `False`（证明 `del` 生效）。
- `tf.add` / `tf.constant` 是否可访问（证明 API 被组装出来了）。
- `tf.__version__` 能否打印出一个版本号字符串。

**预期结果**：`False / False / True / True / 版本号字符串`。

> 注意：**待本地验证**。上述为示例代码，本讲未在当前环境实际运行，请以本机输出为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `tensorflow/__init__.py` 只有三十多行，却能提供成百上千个 `tf.*` 函数？
**答案**：因为公开符号不是在 `__init__.py` 里逐个 `import` 的，而是各模块用 `@tf_export(...)` 注册到一张全局映射表，再由包机制组装进 `tf` 命名空间。见 [tf_export.py:81-85](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/util/tf_export.py#L81-L85)。

**练习 2**：`del python` 和 `del core` 解决了什么问题？
**答案**：互导入会让 `python`、`core` 子包名泄漏到顶层 `tf` 模块对象上；`del` 把它们从公开命名空间清除，避免用户误用 `tf.python` 这类非公开 API。见 [tensorflow/__init__.py:26-32](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/__init__.py#L26-L32)。

**练习 3**：`pywrap_tensorflow` 在入口里被导入却不直接使用，为什么还要导入它？
**答案**：是为了利用它的**导入副作用**——导入它会触发 `tensorflow.python` 子包加载，从而把整个 Python API 拉起来。见 [tensorflow/__init__.py:20](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/__init__.py#L20)。

---

### 4.3 从源码仓库快速建立全局认知的方法

#### 4.3.1 概念说明

面对 TF 这种几千万行级别的仓库，「从哪儿开始读」本身就是一门技术。本模块不教具体代码，而是教你一套**通用的「仓库认知建立法」**，适用于任何大型开源项目：

1. **先读 README**——定位、接口分层、安装、第一个例子。
2. **看构建/配置入口**——用什么构建、依赖怎么管。
3. **找语言入口**——`import` 的第一个文件在哪。
4. **看目录分层**——哪些目录对应 Python、C++、移动端。
5. **抓一条最小调用链**——从一个能跑的例子，反推它经过了哪些文件。

前三步你在本讲已经做完，第四步是下一讲（u1-l2）的主题。本模块聚焦第 2、3 步：识别仓库根目录的关键入口文件。

#### 4.3.2 核心流程

「找入口文件」的判别流程：

```
仓库根目录
   │
   ├─ README.md           ──► 是什么、怎么装、怎么跑（定位）
   │
   ├─ 构建类入口
   │    ├─ configure / configure.py ──► 构建前的环境配置
   │    ├─ WORKSPACE / MODULE.bazel   ──► Bazel 外部依赖与模块声明
   │    └─ .bazelrc                    ──► Bazel 构建默认参数
   │
   └─ 语言入口
        └─ tensorflow/__init__.py      ──► import tensorflow 的起点
```

只要握住「定位文件 + 构建入口 + 语言入口」这三类文件，就能在任何时候快速回到主干。

#### 4.3.3 源码精读

下面三个是仓库根目录最关键的入口文件（已确认存在）：

- **`README.md`**——项目门面，本讲已精读。永久链接：[README.md](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/README.md)。
- **`configure.py`**（及配套的 `configure`、`configure.cmd`）——构建前用于探测与配置环境（如 CPU/GPU、编译选项）的脚本，运行 `./configure` 时被调用。永久链接：[configure.py](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/configure.py)（细节会在 u1-l3 详解）。
- **`tensorflow/__init__.py`**——`import tensorflow` 的起点，本模块 4.2 已精读。

此外还有两个 Bazel 相关入口值得知道：

- **`WORKSPACE`**——Bazel 工作区声明，用于管理外部依赖。永久链接：[WORKSPACE](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/WORKSPACE)。
- **`MODULE.bazel`**——Bazel 较新的模块化依赖声明（Bzlmod）。永久链接：[MODULE.bazel](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/MODULE.bazel)。

> 说明：TF 主构建工具是 **Bazel**（根目录有 `.bazelversion`、`.bazelrc`）。本讲只需知道「它用 Bazel 构建、configure.py 负责配置」即可，深入内容见 u1-l3。

#### 4.3.4 代码实践

**实践目标**：亲手在仓库根目录定位「三个最关键的入口文件」，并写下 TF 的差异定位。

**操作步骤**：

1. 进入仓库根目录，列出顶层文件，确认下列三者存在：
   - `README.md`
   - `configure.py`
   - `tensorflow/__init__.py`
2. 分别打开这三个文件，确认它们的「入口」身份：
   - `README.md`：能否找到定位句与第一个可运行例子？
   - `configure.py`：是否是配置/探测环境的脚本？（看到文件开头的 license 头与配置逻辑即可）
   - `tensorflow/__init__.py`：是否是极简入口、是否出现 `pywrap_tensorflow` 与 `del python / del core`？
3. 用**一句话**写下 TensorFlow 相对 PyTorch、JAX 的差异定位。

**需要观察的现象**：

- 三个文件都真实存在于根目录（或 `tensorflow/` 子目录）。
- 它们恰好对应「定位 / 构建 / 语言」三类入口。

**预期结果**：你能不看讲义，独立说出「读 TF 源码，先看 README 定位、configure.py 配置、`tensorflow/__init__.py` 是 Python 入口」，并给出一句差异定位。

> 注意：第 2 步对 `configure.py` 只要求确认身份，不要求读懂全部配置逻辑——那是 u1-l3 的任务。

#### 4.3.5 小练习与答案

**练习 1**：仓库根目录里，哪三个文件最适合作为「建立全局认知」的入口？
**答案**：`README.md`（定位与运行）、`configure.py`（构建配置）、`tensorflow/__init__.py`（Python 语言入口）。

**练习 2**：TF 的主构建工具是什么？根目录里哪两个文件暴露了这一点？
**答案**：Bazel；`WORKSPACE` 与 `MODULE.bazel`（外加 `.bazelrc`、`.bazelversion`）。

**练习 3**：如果你要向新同事介绍「打开 TF 仓库该先看什么」，你会给出怎样的三步路线？
**答案（参考）**：① 读 `README.md` 建立定位与运行认知；② 看 `configure.py` + `WORKSPACE`/`MODULE.bazel` 了解构建方式；③ 从 `tensorflow/__init__.py` 进入 Python 入口，顺着 `pywrap_tensorflow` 理解 Python↔C++ 边界。

---

## 5. 综合实践

**任务**：为 TensorFlow 制作一张「一页纸认知地图」。

要求你把本讲三个模块串起来，完成一份不超过一页的笔记，包含：

1. **定位句**：用自己的话写一句 TF 是什么（参考 [README.md:19-25](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/README.md#L19-L25)）。
2. **接口分层**：列出 Python / C++ / 移动端（TFLite）三类接口分别对应仓库里的哪个目录（提示：`tensorflow/python/`、`tensorflow/core/`、`tensorflow/lite/`）。
3. **入口三件套**：写出 `README.md`、`configure.py`、`tensorflow/__init__.py` 各自的一句话职责。
4. **一个反直觉点**：写明为什么 `tensorflow/__init__.py` 这么短，`tf.add` 却能用（关键词：`pywrap_tensorflow` + `tf_export` + `del python/core`）。
5. **差异定位**：一句话写 TensorFlow 与 PyTorch、JAX 的区别。

**自检标准**：如果合上讲义你还能独立写出以上五点，说明本讲的「全局认知」目标已达成。

> 注意：本任务无需运行任何命令，是纯「源码阅读 + 归纳」型实践，重点是把认知结构化。

---

## 6. 本讲小结

- TensorFlow 是一个**端到端的机器学习开源平台**，覆盖研究、训练、推理、部署全链路；出自 Google Brain 团队。
- README 明确：**Python 与 C++ 是稳定 API**，其它语言绑定不保证向后兼容——这直接对应仓库的目录分层。
- `tensorflow/__init__.py` 是 `import tensorflow` 的入口，但它**刻意保持极简**，靠导入 `pywrap_tensorflow` 触发整个 Python API 加载。
- 公开的 `tf.*` 符号是通过 **`@tf_export(...)` 装饰器**注册到一张全局映射表、再组装进命名空间的，所以入口无需手写海量 `import`。
- `del python` / `del core` 是为了清理命名空间，不让内部子包泄漏成公开 API。
- 面对大型仓库，记住「定位（README）+ 构建（configure.py / WORKSPACE）+ 语言入口（`__init__.py`）」三类文件，就能随时回到主干。

---

## 7. 下一步学习建议

本讲建立了「全局认知」，接下来建议：

- **u1-l2 顶层目录结构与仓库布局**：系统走一遍 `tensorflow/`、`third_party/`、`tools/`、`ci/` 等目录，弄清 `core / python / cc / lite / compiler` 如何对应不同语言层——这是「读源码不迷路」的地基。
- **u1-l3 构建系统 Bazel 与 configure 配置**：深入 `configure.py`、`WORKSPACE`、`MODULE.bazel`，理解 TF 怎么被构建出来。
- **u1-l4 pip 包打包与 Python 入口 import tensorflow**：更完整地追踪 `import tensorflow as tf` 的导入链与 `pywrap_tensorflow` 桥接，本讲埋下的「`tf.add` 从哪来」会在这里彻底讲透。

> 一个小建议：在进入 u1-l2 之前，先在仓库根目录随手浏览一下 `README.md` 与 `tensorflow/` 目录结构，带着「这些目录是干嘛的」的问题去读下一讲，效果会更好。
