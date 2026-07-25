# u1-l4 pip 包打包与 Python 入口 import tensorflow

## 1. 本讲目标

本讲要回答两个初学者最容易卡住的问题：

1. **TensorFlow 海量的 C++/Python 源码，到底是怎么变成一行 `pip install tensorflow` 就能装上的 wheel 包的？**
2. **当你在终端敲下 `import tensorflow as tf` 时，Python 究竟加载了哪些文件、按什么顺序加载，才让 `tf.constant`、`tf.keras` 这些符号「凭空」出现在命名空间里？**

学完本讲，你应当能够：

- 说出 TensorFlow 从「Bazel 构建产物」到「pip wheel」经历的几个关键阶段，以及 `setup.py.tpl`、`build_pip_package.py` 各自的职责。
- 解释源码树里的 `tensorflow/__init__.py` 只是一个**占位文件**，真正发布出去的 `__init__.py` 是构建时由 `api_template.__init__.py` **生成**出来的。
- 画出 `import tensorflow as tf` 的导入链，至少经过 `pywrap_tensorflow` 与 `tf2` 两个关键模块。
- 解释 `tensorflow/__init__.py` 末尾为什么出现 `del python` / `del core` 这种「自己删自己导入的符号」的写法。
- 理解 `pywrap_tensorflow` 这座桥如何把 Python 与 C++ 内核（`_pywrap_tensorflow_internal.so`）连接起来。

## 2. 前置知识

本讲假设你已经读过本手册的前三讲，并掌握以下概念：

- **TensorFlow 的定位与语言分层**（u1-l1）：仓库分 `core/`（C++ 核心）、`python/`（Python API）、`compiler/`（MLIR/XLA）、`lite/`（移动端）等语言层；公开符号靠 `@tf_export` 装饰器登记到一张全局「名字→符号」表。
- **顶层目录布局**（u1-l2）：能区分 `tensorflow/`、`third_party/`、`tools/` 三大区域，知道 `python→c→core` 的自底向上调用栈。
- **Bazel 构建系统**（u1-l3）：知道 `WORKSPACE`/`MODULE.bazel` 管依赖、`.bazelrc` 管配置档、BUILD 文件里用 `genrule`、`py_binary`、`copy_file` 等 rule 描述构建动作。

此外，需要补充几个本讲会用到的基础概念：

- **wheel**：Python 的二进制分发包格式（一个 `.whl` 文件本质是 zip）。`pip install xxx.whl` 就是把它解压到 `site-packages`。`setuptools` 的 `setup.py` + `bdist_wheel` 命令是生成 wheel 的传统方式。
- **占位符（placeholder）替换**：很多构建系统会先准备一个带「标记」的模板文件，构建时把标记替换成真实内容。本讲会看到两种：`setup.py.tpl`（带 `_VERSION = '0.0.0'` 等占位）和 `api_template.__init__.py`（带 `# API IMPORTS PLACEHOLDER` 等注释占位）。
- **导入副作用（import for side effects）**：Python 里 `import` 一个模块会**执行**该模块的全部顶层代码。TensorFlow 大量利用这一点——导入某个模块只是为了「顺带」把符号注册到全局表里，而不是真的要用该模块的返回值。
- **命名空间（namespace）**：一个 Python 模块就是一个「名字→对象」的字典。`import tensorflow as tf` 之后，`tf` 这个名字指向的就是顶层 `tensorflow` 模块的命名空间；`tf.xxx` 能不能用，取决于 `xxx` 有没有被放进这个字典。

## 3. 本讲源码地图

本讲涉及的关键文件如下表：

| 文件 | 作用 | 所属最小模块 |
| --- | --- | --- |
| `tensorflow/__init__.py` | 源码树里的顶层入口，**只是占位文件**，用于让测试在源码树下能跑 | `tensorflow/__init__.py` |
| `tensorflow/api_template.__init__.py` | 真正发布出去的 `__init__.py` 的**模板**，构建时被填入海量 API 导入 | `tensorflow/__init__.py` |
| `tensorflow/BUILD` | 用 `generate_apis`/`copy_file`/`genrule` 把模板变成最终的 `__init__.py` | `tensorflow/__init__.py` |
| `tensorflow/python/pywrap_tensorflow.py` | Python 侧的「桥」，负责加载 C++ 内核 `_pywrap_tensorflow_internal.so` | `tensorflow/__init__.py` |
| `tensorflow/python/__init__.py` | `tensorflow.python` 子包的入口，刻意保持极简以缩短导入时间 | `tensorflow/__init__.py` |
| `tensorflow/tools/pip_package/setup.py.tpl` | 发布 wheel 用的 `setup.py` 模板，含大量占位变量 | `tensorflow/tools/pip_package` |
| `tensorflow/tools/pip_package/build_pip_package.py` | 把 Bazel 产物重新整理成 wheel 目录结构的脚本 | `tensorflow/tools/pip_package` |
| `tensorflow/tools/pip_package/BUILD` | 定义 `tf_wheel` 目标与 `setup_py` genrule，驱动打包 | `tensorflow/tools/pip_package` |

> 两条主线：**打包线**（右半表，`tools/pip_package`）负责「源码→wheel」；**导入线**（左半表，`__init__.py`）负责「`pip install` 后→`import tensorflow`」。本讲按这两条线展开。

## 4. 核心概念与源码讲解

### 4.1 pip 包打包流水线：从 Bazel 构建产物到 wheel

#### 4.1.1 概念说明

你在 PyPI 上 `pip install tensorflow` 装下来的那个包，**不是**直接把仓库 `git clone` 下来就能得到的。TensorFlow 的发布物里同时包含：

- 大量 Python 源码（`.py`）；
- 一个用 C++ 编译出来的巨型共享库 `_pywrap_tensorflow_internal.so`（Windows 下是 `.pyd`），它承载了几乎所有真正的计算逻辑；
- 一批 C/C++ 头文件（给自定义 op 的开发者用）；
- 许可证、`MANIFEST.in` 等元数据。

这些产物由 **Bazel** 负责编译产出，而 Bazel 的输出目录结构与 pip 期望的目录结构并不一致。于是 TensorFlow 在 `tensorflow/tools/pip_package/` 下放了一套脚本，负责「把 Bazel 产物**搬运并重排**成 wheel 所需的目录，再调用 `setup.py bdist_wheel` 打成 `.whl`」。

这条流水线涉及三个角色：

- **`setup.py.tpl`**：`setup.py` 的模板，里面有 `_VERSION = '0.0.0'` 这样的占位字符串。
- **`setup_py` genrule**（在 BUILD 里）：构建时用 `modify_setup_py_binary` 把模板里的占位替换成真实版本号、CUDA 版本等，生成最终的 `setup.py`。
- **`build_pip_package.py`**：真正的「搬运工」，负责整理目录、调用 `setup.py bdist_wheel`。

#### 4.1.2 核心流程

`build_pip_package.py` 开头的模块文档字符串把整件事概括成了四步，这是理解整条流水线的总纲：

> 1) Takes lists of paths to .h/.py/.so/etc files.（拿到一批文件路径）
> 2) Creates a temporary directory.（建临时目录）
> 3) Copies files from #1 to #2 with some exceptions and corrections.（带修正地拷贝）
> 4) A wheel is created from the files in the temp directory.（在临时目录里打 wheel）

把它和 Bazel 视角合在一起，整条流水线如下：

```
Bazel 构建
  └─ //tensorflow/tools/pip_package:wheel   (tf_wheel 目标，收集所有产物)
        │  产物：一堆 .py / .so / .h / proto 文件路径列表
        ▼
build_pip_package.py
  ├─ prepare_wheel_srcs()
  │    ├─ prepare_headers()   : 整理头文件，做路径替换 (xla→compiler, tsl→tensorflow)
  │    ├─ prepare_srcs()      : 整理 .py 源文件
  │    ├─ create_init_files() : 给每个含 .py 的目录补一个空 __init__.py
  │    ├─ update_xla_tsl_imports() : 改写 import 路径以「假装」xla/tsl 还在仓库内
  │    └─ rename_libtensorflow() / patch_so() : 修正动态库名与 rpath
  └─ build_wheel()
       └─ subprocess 调用: setup.py bdist_wheel
              │  setup.py 由 setup.py.tpl 经 genrule 生成
              ▼
          tensorflow-<version>-<platform>.whl
```

其中最值得注意的一个「修正」是 **xla/tsl 的 vendoring 处理**：XLA 与 TSL（TF Shared Libraries）原本在仓库内，后来被拆到了外部仓库（`@xla`、`@tsl`）。为了**不破坏既有 TF 的 Python 导入路径**，打包脚本会把这些外部仓库的文件拷贝回 `tensorflow/compiler/xla`、`tensorflow/tsl`，并改写源码里的 `from tsl` / `from xla`，让它们「看起来」仍属于 TF 内部。这是 `build_pip_package.py` 文档里点名的「主要修正」。

#### 4.1.3 源码精读

**① `setup.py.tpl` 的占位变量**

模板顶部有一批「占位」变量，构建时由 genrule 填入真实值。比如版本号：

```python
_VERSION = '0.0.0'
```
详见 [tensorflow/tools/pip_package/setup.py.tpl:55-73](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/tools/pip_package/setup.py.tpl#L55-L73) ——这里 `_VERSION` 是占位串，`cuda_version`、`nvidia_cudnn_version` 等也全是占位，等构建时替换。

**② 模板如何变成 `setup.py`**

替换动作由 BUILD 里的 `setup_py` genrule 完成，它调用 `modify_setup_py_binary` 把版本号、CUDA 信息、NVIDIA 轮版本数据注入模板：

详见 [tensorflow/tools/pip_package/BUILD:303-321](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/tools/pip_package/BUILD#L303-L321) ——`cmd` 里用 `--tf_version`、`--cuda_version`、`--nvidia_wheel_versions_data` 把占位填掉，`outs = ["setup.py"]` 即最终用于打包的脚本。

**③ `setup.py.tpl` 末尾真正的 `setup()` 调用**

模板最底下是标准的 setuptools `setup()` 调用，声明了包名、依赖、入口命令脚本等。其中 `install_requires` 列出了 TF 运行时依赖的全部第三方 Python 包（`absl-py`、`numpy`、`protobuf` 等），这就是 `pip install tensorflow` 会顺带装上这些东西的根源：

详见 [tensorflow/tools/pip_package/setup.py.tpl:425-450](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/tools/pip_package/setup.py.tpl#L425-L450) ——`name`、`version`、`install_requires=REQUIRED_PACKAGES`、`extras_require=EXTRA_PACKAGES` 等关键字决定了 wheel 的元数据与依赖。

`REQUIRED_PACKAGES` 本身定义在前面，是一份带版本约束的依赖清单：

详见 [tensorflow/tools/pip_package/setup.py.tpl:99-133](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/tools/pip_package/setup.py.tpl#L99-L133) ——注意 `grpcio` 仅在小端机器上依赖，`h5py` 按 Python 小版本号取不同约束，体现了「跨平台 wheel」的细节。

**④ `BinaryDistribution`：声明这是一个带扩展模块的包**

```python
class BinaryDistribution(Distribution):
  def has_ext_modules(self):
    return True
```
详见 [tensorflow/tools/pip_package/setup.py.tpl:211-214](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/tools/pip_package/setup.py.tpl#L211-L214) ——覆盖 `has_ext_modules` 返回 `True`，告诉 setuptools「本包含 C 扩展」，从而让平台标记（plat-name）按平台而非纯 Python 处理。

**⑤ `build_pip_package.py` 的「搬运 + 重排」**

`prepare_headers` 里有两张表：`path_to_exclude`（要剔除的目录）和 `path_to_replace`（要做路径映射的目录）。后者把外部仓库路径映射回 TF 内部路径，正是「假装 xla/tsl 还在仓库内」的实现：

详见 [tensorflow/tools/pip_package/build_pip_package.py:154-161](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/tools/pip_package/build_pip_package.py#L154-L161) ——`get_repo_path("xla"): "tensorflow/compiler"`、`get_repo_path("tsl"): "tensorflow"`，即把 `@xla` 的产物摆到 `tensorflow/compiler` 下、`@tsl` 的产物摆到 `tensorflow` 下。

与之配套的 `update_xla_tsl_imports` 直接做文本替换，改写源码里的导入语句：

详见 [tensorflow/tools/pip_package/build_pip_package.py:345-349](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/tools/pip_package/build_pip_package.py#L345-L349) ——把 `from tsl` 改成 `from tensorflow.tsl`、`from xla` 改成 `from tensorflow.compiler.xla`，这样安装后的包里导入路径才与「拆分前」一致。

**⑥ 最终 `build_wheel` 调 `setup.py bdist_wheel`**

打包脚本在一个临时目录里整理好文件后，以子进程方式调用 `setup.py bdist_wheel` 生成真正的 `.whl`：

详见 [tensorflow/tools/pip_package/build_pip_package.py:472-483](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/tools/pip_package/build_pip_package.py#L472-L483) ——通过 `sys.executable`（当前 Python 解释器）执行 `setup.py bdist_wheel`，并指定 `--dist-dir`（输出目录）与 `--plat-name`（平台标签）。

#### 4.1.4 代码实践

> **实践类型：源码阅读型**（完整构建一个 wheel 需要全量 Bazel 编译，耗时极长且需 CUDA 环境，故本实践以阅读为主，真实构建标记为可选）。

1. **实践目标**：理解 `setup.py.tpl` → `setup.py` → `setup.py bdist_wheel` 这条链，以及打包脚本对 xla/tsl 的「假装还在仓库内」处理。
2. **操作步骤**：
   - 打开 `tensorflow/tools/pip_package/setup.py.tpl`，找到第 55 行 `_VERSION = '0.0.0'`，再到 `tensorflow/tools/pip_package/BUILD` 第 303-321 行的 `setup_py` genrule，确认 `_VERSION` 是被 `--tf_version "{wheel_version}{wheel_version_suffix}"` 替换掉的。
   - 在 `setup.py.tpl` 中找到 `CONSOLE_SCRIPTS`（约 191 行），说出 `pip install tensorflow` 后会在命令行注册哪几个命令（提示：`tflite_convert`、`saved_model_cli` 等）。
   - 打开 `build_pip_package.py`，依次阅读 `prepare_wheel_srcs`（296 行起）、`update_xla_tsl_imports`（345 行起）、`build_wheel`（439 行起），把它们的调用顺序填进 4.1.2 的流程图。
3. **需要观察的现象**：你会看到「源码里 `from xla` / `from tsl` 的导入」与「外部仓库 `@xla`/`@tsl`」之间存在错位，而打包脚本的文本替换正是为了弥合这个错位。
4. **预期结果**：能用自己的话说出「为什么 wheel 里要有 `tensorflow/compiler/xla` 和 `tensorflow/tsl` 目录」——因为安装后用户代码里的 `from tensorflow.compiler.xla import ...` 要能找到对应文件。
5. **真实构建（可选，待本地验证）**：在一台配好 CUDA 的机器上，按 README 跑 `./configure`，再执行类似 `bazelisk build //tensorflow/tools/pip_package:wheel` 的命令，最后用 `build_pip_package.py` 产出 `.whl` 并 `pip install`。这一步非常耗时，作为进阶练习即可。

#### 4.1.5 小练习与答案

**练习 1**：`setup.py.tpl` 里 `_VERSION = '0.0.0'`、`nvidia_cudnn_version = ''` 全是占位串。是谁、在构建的哪个阶段把它们替换成真实值的？

> **参考答案**：是 `tensorflow/tools/pip_package/BUILD` 里的 `setup_py` genrule（303-321 行）。它在 Bazel 的分析/执行阶段调用 `modify_setup_py_binary`，用 `--tf_version`、`--cuda_version`、`--nvidia_wheel_versions_data` 等参数把模板 `setup.py.tpl` 渲染成最终的 `setup.py`。

**练习 2**：为什么 `build_pip_package.py` 要把 `@xla`、`@tsl` 的产物搬进 wheel 的 `tensorflow/compiler`、`tensorflow` 目录，还要改写 `from xla`/`from tsl` 导入？

> **参考答案**：XLA 与 TSL 原本是 TF 仓库的一部分，后来被拆成独立的外部 Bazel 仓库。但 TF 对外暴露的 Python 导入路径（如 `from tensorflow.compiler.xla ...`）不能变，否则会破坏既有用户代码与内部相互引用。因此打包时「假装」它们还在仓库内：物理上把文件摆回原路径，逻辑上把导入语句改写回 `tensorflow.*` 前缀。

---

### 4.2 顶层 `tensorflow/__init__.py`：占位、生成与导入链

#### 4.2.1 概念说明

这一节是本讲的核心。很多人打开仓库直接读 `tensorflow/__init__.py`，发现它只有寥寥十几行，于是产生疑惑：**就这么几行，怎么 `import tensorflow as tf` 之后能冒出成千上万个 `tf.xxx` 符号？**

答案是：**源码树里这个 `tensorflow/__init__.py` 不是真正发布出去的那个。** 它只是一个「占位文件」，存在的唯一目的是让测试在源码树下、还没经过完整构建时也能 `import tensorflow`。真正随 wheel 发布的 `tensorflow/__init__.py`，是构建时根据 `tensorflow/api_template.__init__.py` **生成**出来的——构建系统会把仓库里所有用 `@tf_export` 登记过的符号，整理成海量的 `from tensorflow.python... import ...` 语句，填进模板的占位标记里。

这个事实在模板文件自己的文档字符串里就白纸黑字写明了：

```
Note that the file `__init__.py` in the TensorFlow source code tree is actually
only a placeholder to enable test cases to run. The TensorFlow build replaces
this file with a file generated from `api_template.__init__.py`
```
详见 [tensorflow/api_template.__init__.py:24-27](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/api_template.__init__.py#L24-L27)。

#### 4.2.2 核心流程

把「生成」与「导入」串起来，整体流程如下：

```
【构建期：生成 __init__.py】
  api_template.__init__.py        ← 模板，含 # API IMPORTS PLACEHOLDER 等占位
        │ generate_apis 宏 (tf_python_api_gen_v2)
        │  扫描全仓库 @tf_export 登记的符号，填入占位
        ▼
  _api/v2/v2.py                   ← 填好的完整入口
        │ root_init_gen (copy_file)
        ▼
  tensorflow/__init__.py          ← 覆盖占位文件，随 wheel 发布

【运行期：import tensorflow as tf】
  Python 执行 tensorflow/__init__.py（即生成后的 v2.py）
   1. from tensorflow.python import pywrap_tensorflow   ← 加载 C++ 内核（4.3 节）
   2. from tensorflow.python import tf2; tf2.enable()   ← 开启 TF2 行为
   3. （展开后的）API IMPORTS PLACEHOLDER                ← 海量 from ... import，组装 tf.*
   4. del python / del core / del compiler              ← 命名空间清理
```

注意第 ② 步——`root_init_gen` 只是一个 `copy_file`，把生成好的 `_api/v2/v2.py` 原样复制成 `__init__.py`，从而在构建产物里**盖掉**源码树那个占位文件。

#### 4.2.3 源码精读

**① 占位文件 `tensorflow/__init__.py` 的全部「干货」**

源码树里的占位 `__init__.py` 实际只做三件事：导入 `pywrap_tensorflow`（拉起 C++ 内核）、导入 `flags`/`app`（命令行标志支持）、然后 `del python`/`del core` 清理命名空间：

```python
from tensorflow.python import pywrap_tensorflow  # pylint: disable=unused-import

from tensorflow.python.platform import flags  # pylint: disable=g-import-not-at-top
from tensorflow.python.platform import app  # pylint: disable=g-import-not-at-top
app.flags = flags
...
del python
del core
```
详见 [tensorflow/__init__.py:20-32](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/__init__.py#L20-L32)。这个文件之所以能这么短，正是因为「真正干活」的导入都由构建时生成的版本补上。

**② 生成入口：`generate_apis` 宏**

在 `tensorflow/BUILD` 里，`tf_python_api_gen_v2` 调用 `generate_apis` 宏，以 `api_template.__init__.py` 为模板生成 v2 API 入口：

详见 [tensorflow/BUILD:1677-1706](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/BUILD#L1677-L1706) ——`root_init_template = "api_template.__init__.py"`、`root_file_name = "v2.py"`，即模板与输出文件名。`generate_apis` 来自 [tensorflow/python/tools/api/generator2:generate_api.bzl](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/BUILD#L75)（BUILD 第 75 行的 `load`）。

**③ 覆盖占位：`root_init_gen` 把 `v2.py` 拷成 `__init__.py`**

```
copy_file(
    name = "root_init_gen",
    src = select({
        "api_version_2": "_api/v2/v2.py",
        "//conditions:default": "_api/v1/v1.py",
    }),
    out = "__init__.py",
)
```
详见 [tensorflow/BUILD:1635-1642](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/BUILD#L1635-L1642) ——这一步在构建产物里用 `v2.py` 覆盖了源码树的占位 `__init__.py`，所以你 `pip install` 到的 `__init__.py` 是生成版，而不是上面那个占位版。

**④ 模板里的导入链骨架**

`api_template.__init__.py` 的前半段定义了 `import tensorflow as tf` 真正执行的导入顺序。最关键的两行是先加载 C++ 内核、再开启 TF2 行为：

```python
from tensorflow.python import pywrap_tensorflow as _pywrap_tensorflow  # pylint: disable=unused-import
...
from tensorflow.python import tf2 as _tf2
_tf2.enable()
```
详见 [tensorflow/api_template.__init__.py:40-47](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/api_template.__init__.py#L40-L47)。注意第 40 行的注释 `# pylint: disable=unused-import`——这个导入**确实没有被变量引用**，它的作用纯粹是「副作用」：触发 `pywrap_tensorflow` 把 C++ 内核加载进进程。这是典型的「import for side effects」模式。

紧接着的占位标记，就是构建时填入海量 API 导入的地方：

```python
# API IMPORTS PLACEHOLDER
# WRAPPER_PLACEHOLDER
```
详见 [tensorflow/api_template.__init__.py:49-51](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/api_template.__init__.py#L49-L51)。这两行注释在仓库里是「空」的，但生成后的 `__init__.py` 里，它们会被替换成成百上千行 `from tensorflow.python.ops.array_ops import ...` 之类的导入——这就是 `tf.constant`、`tf.add` 等符号的真正来源。

**⑤ 命名空间清理：`del python / del core / del compiler`**

模板末尾用 `try/except NameError` 删掉三个内部子包名：

```python
try:
  del python
except NameError:
  pass
try:
  del core
except NameError:
  pass
try:
  del compiler
except NameError:
  pass
```
详见 [tensorflow/api_template.__init__.py:167-183](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/api_template.__init__.py#L167-L183)。这一段的用意见 4.2.4 的实践解释——简而言之，是为了不让 `tf.python`、`tf.core`、`tf.compiler` 这些**内部**子包泄漏到公开命名空间。

**⑥ 子包入口刻意保持极简**

`tensorflow/python/__init__.py` 顶部有一段警告注释，强调不要往里加任何 import，否则会让 TensorFlow 的导入时间增加好几秒：

详见 [tensorflow/python/__init__.py:17-21](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/__init__.py#L17-L21)。它只用 `__all__` 过滤掉下划线开头的私有符号，本身几乎不执行任何工作——因为 `tensorflow.python` 会在每次 `import tensorflow` 时被导入，任何多余 import 都会拖慢启动。

#### 4.2.4 代码实践（本讲主实践）

> **实践类型：源码阅读型 + 可选运行验证**。对应讲义规格里的主实践任务。

1. **实践目标**：跟踪 `tensorflow/__init__.py`（生成版）的导入链，写出 `import tensorflow as tf` 至少经过的两个关键模块，并解释 `del python` / `del core` 出现的原因。
2. **操作步骤**：
   - 阅读占位文件 [tensorflow/__init__.py:20-32](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/__init__.py#L20-L32)，再阅读模板 [tensorflow/api_template.__init__.py:40-51](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/api_template.__init__.py#L40-L51)，理解「占位 vs 生成」的关系。
   - 列出导入链至少经过的两个关键模块：
     - 其一：`tensorflow.python.pywrap_tensorflow`（顺带触发 `tensorflow.python._pywrap_tensorflow_internal` 这个 C++ 扩展的加载）；
     - 其二：`tensorflow.python.tf2`（调用 `_tf2.enable()` 开启 TF2 行为）。
   - 解释 `del python` / `del core`：因为第 ① 步 `from tensorflow.python import pywrap_tensorflow` 会**导入 `tensorflow.python` 子包**，Python 会把子包名 `python` 作为属性绑定到父包 `tensorflow` 的命名空间里（`core`、`compiler` 同理，来自传递性导入）。这些是内部子包，不应作为 `tf.python` 暴露给用户，故在结尾 `del` 掉。占位文件里的注释也说明了这一点：这些符号是「因为 import 而出现的副作用」。
3. **需要观察的现象**：在模板里，`del` 被包在 `try/except NameError` 里；而占位文件里是直接 `del python`（并加了 `# pylint: disable=undefined-variable`）。
4. **预期结果**：能解释「为什么模板要用 `try/except NameError`」——因为生成后的入口文件在不同摆放位置下（如先放在 `tensorflow/_api/v2/` 再拷到 `tensorflow/`），`python`/`core` 这些名字不一定都存在，直接 `del` 可能抛 `NameError`，所以容错处理。
5. **运行验证（可选，待本地验证）**：如果你已 `pip install tensorflow`，可以这样做对比——
   ```python
   # 示例代码：对比「安装版」与「源码占位版」的差异
   import tensorflow as tf
   print(tf.__file__)              # 指向 site-packages 里的 __init__.py（生成版，体积很大）
   # 在该文件里搜索 "API IMPORTS"，你会看到占位标记已被海量 from ... import 取代
   # 再对比仓库里的 tensorflow/__init__.py（占位版，只有十几行）
   ```

#### 4.2.5 小练习与答案

**练习 1**：既然源码树的 `tensorflow/__init__.py` 是占位文件，那它为什么不能直接删掉？

> **参考答案**：因为开发者在源码树下跑测试、跑工具脚本时，并没有先做完整构建生成 `v2.py`。占位 `__init__.py` 提供了一个「最小可用」的入口，让 `import tensorflow` 在未构建状态下也能成功（至少能加载 `pywrap_tensorflow` 与 flags/app）。删掉它，源码树下的开发流程就会失败。

**练习 2**：`api_template.__init__.py` 里 `# API IMPORTS PLACEHOLDER` 在构建后被替换成什么？这些内容是从哪里来的？

> **参考答案**：被替换成成百上千行 `from tensorflow.python.<module> import <symbol>`。这些符号来自全仓库用 `@tf_export(...)` 装饰器登记的公开 API——`generate_apis` 宏在构建期扫描这些登记，按 API 版本（v1/v2）组织成导入语句，填进模板。这正是 u1-l1 讲过的「`tf.*` 符号由 `@tf_export` 注册到全局表再组装」的落地点。

**练习 3**：为什么 `tensorflow/python/__init__.py` 顶部要警告「不要往里加 import」？

> **参考答案**：因为 `tensorflow.python` 这个子包在**每次** `import tensorflow` 时都会被导入（顶层入口会 `from tensorflow.python import ...`）。任何多余的顶层 import 都会被乘以「每次启动一次」，显著拖慢 TensorFlow 的导入时间（注释说可能增加数秒）。所以该文件刻意保持极简。

---

### 4.3 pywrap_tensorflow：Python 与 C++ 内核的桥梁

#### 4.3.1 概念说明

通过 4.2 我们知道，`import tensorflow` 的第一件大事就是 `from tensorflow.python import pywrap_tensorflow`。这一节我们就钻进 `pywrap_tensorflow`，看它到底怎么把 Python 和 C++ 内核连起来。

TensorFlow 真正的计算（矩阵乘、卷积、自动微分调度……）几乎全在 C++ 里实现，编译成一个巨大的共享库。Python 只是「指挥官」：构造计算图、发起执行请求、接收结果。要让 Python 能调用 C++ 函数，需要一个**桥**。在 TensorFlow 里，这座桥就是：

- C++ 侧：`tensorflow/python/lib/core/` 下一批用 [pybind11](https://pybind11.readthedocs.io/)（早期是手写 CPython 扩展）封装的代码，编译成 `_pywrap_tensorflow_internal.so`（Windows 下 `.pyd`）。
- Python 侧：`tensorflow/python/pywrap_tensorflow.py`，负责把这个 `.so` 以 Python 扩展模块的形式加载进进程，并把它导出的函数「摊开」到当前命名空间。

`pywrap` 这个名字就是「Python wrapper（Python 包装）」的缩写。

#### 4.3.2 核心流程

`pywrap_tensorflow.py` 的执行流程（即「导入它时会发生什么」）如下：

```
执行 from tensorflow.python import pywrap_tensorflow
  1. import ctypes / os / sys / traceback        ← 准备动态加载工具
  2. from tensorflow.python.platform import self_check
  3. self_check.preload_check()                  ← 加载前的环境自检（给出可操作的报错）
  4. （尝试）设置 dlopen 标志：
       - 优先 pywrap_dlopen_global_flags.set_dlopen_flags()
       - 否则在非默认 RTLD_LOCAL 的平台（如 macOS）显式设 RTLD_LOCAL
  5. from tensorflow.python._pywrap_tensorflow_internal import *   ← 真正加载 C++ .so
  6. 还原 dlopen 标志
  7. 若第 5 步 ImportError：打印诊断信息（Windows 下额外跑 windows_lib_diagnostics）
```

第 ⑤ 步是命门：`_pywrap_tensorflow_internal` 才是那个承载 C++ 内核的扩展模块。`import *` 会把它通过 pybind11 暴露出来的全部函数摊到 `pywrap_tensorflow` 的命名空间，供上层（如 `tf_session_helper`、各种 op 包装）调用。

这里还要理解一个动态链接细节：TensorFlow 的 `.so` 内部还会再依赖别的 `.so`（比如 CUDA 库、`libtensorflow_framework`）。Linux 默认的符号可见性有时不够，所以脚本需要根据情况调整 `dlopen` 标志（`RTLD_GLOBAL` 或 `RTLD_LOCAL`），否则会出现「找不到符号」的加载错误。

#### 4.3.3 源码精读

**① 加载前自检**

```python
from tensorflow.python.platform import self_check
...
self_check.preload_check()
```
详见 [tensorflow/python/pywrap_tensorflow.py:33-38](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/pywrap_tensorflow.py#L33-L38)。`preload_check` 在真正 `dlopen` 之前先检查环境（如 CPU 指令集是否支持），目的是把「模糊的加载失败」提前转化成「可操作的错误提示」。

**② 调整 dlopen 标志**

```python
if _use_dlopen_global_flags:
  pywrap_dlopen_global_flags.set_dlopen_flags()
elif _can_set_rtld_local:
  sys.setdlopenflags(_default_dlopen_flags | ctypes.RTLD_LOCAL)
```
详见 [tensorflow/python/pywrap_tensorflow.py:57-64](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/pywrap_tensorflow.py#L57-L64)。注释解释：在 Linux 上 `RTLD_LOCAL` 是 0、本就是默认行为，所以这行是「无操作」；而在 macOS 等默认不是 `RTLD_LOCAL` 的平台上才真正生效。这体现了跨平台动态加载的微妙之处。

**③ 真正加载 C++ 内核**

```python
from tensorflow.python._pywrap_tensorflow_internal import *
```
详见 [tensorflow/python/pywrap_tensorflow.py:72-82](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/pywrap_tensorflow.py#L72-L82)。这一行触发 Python 加载 `_pywrap_tensorflow_internal.so`/`.pyd`——即承载 C++ 内核的扩展模块。外层的 `try/except ModuleNotFoundError: pass` 是为了兼容开源/内部不同的链接方式（有些构建把符号直接链进主库，不需要这个 `.so`）。

**④ 加载失败的诊断**

```python
except ImportError as exc:
  if os.name == 'nt':
    ...windows_lib_diagnostics.run_diagnosis()
  raise ImportError(
      f'{traceback.format_exc()}'
      f'\n\nFailed to load the native TensorFlow runtime.\n'
      ...) from exc
```
详见 [tensorflow/python/pywrap_tensorflow.py:88-104](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/pywrap_tensorflow.py#L88-L104)。当 `.so` 加载失败时，这里会给出指向官方「安装错误排查」页面的提示，并在 Windows 上额外运行库诊断，把「`ImportError: DLL load failed`」这种最让新手崩溃的报错变得可排查。

**⑤ 这个 `.so` 在 wheel 里的位置**

`setup.py.tpl` 用 `EXTENSION_NAME` 指明了这个 C++ 扩展在包内的相对路径，正是 `pywrap_tensorflow` 要加载的那个文件：

详见 [tensorflow/tools/pip_package/setup.py.tpl:333-336](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/tools/pip_package/setup.py.tpl#L333-L336) ——Windows 下是 `python/_pywrap_tensorflow_internal.pyd`，其他平台是 `python/_pywrap_tensorflow_internal.so`，通过 `package_data` 打进 wheel。

#### 4.3.4 代码实践

> **实践类型：源码阅读型 + 可选运行验证**。

1. **实践目标**：确认 `pywrap_tensorflow` 是 Python 与 C++ 之间的唯一桥梁，并能指出加载 `.so` 的那一行。
2. **操作步骤**：
   - 打开 [tensorflow/python/pywrap_tensorflow.py](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/pywrap_tensorflow.py)，定位第 74 行的 `from tensorflow.python._pywrap_tensorflow_internal import *`，在它上方画出 dlopen 标志的设置分支（57-64 行）。
   - 对照 4.2 节，确认顶层 `__init__.py`（生成版）第 40 行 `from tensorflow.python import pywrap_tensorflow` 会触发本文件的执行——这就是「import tensorflow → 加载 C++ 内核」的完整因果链。
   - 回答：为什么加载失败时要单独写一段诊断（88-104 行），而不是让 Python 直接抛原生 `ImportError`？
3. **需要观察的现象**：你会看到脚本对 `os.name == 'nt'`（Windows）做了特判；并对 `ModuleNotFoundError` 与 `ImportError` 做了不同处理。
4. **预期结果**：能说出「`_pywrap_tensorflow_internal` 是 C++ 编译产物，`pywrap_tensorflow.py` 是它的 Python 加载器，二者合起来构成 Python↔C++ 桥梁」。
5. **运行验证（可选，待本地验证）**：在已安装 TF 的环境里：
   ```python
   # 示例代码：观察 C++ 扩展来自哪里
   from tensorflow.python import pywrap_tensorflow
   import tensorflow.python._pywrap_tensorflow_internal as ext
   print(ext.__file__)   # 指向 site-packages/tensorflow/python/_pywrap_tensorflow_internal.so
   ```
   如果运行报错，对照 88-104 行的提示排查（常见原因：CUDA 版本不匹配、CPU 不支持所需指令集）。

#### 4.3.5 小练习与答案

**练习 1**：`pywrap_tensorflow.py` 里第 74 行 `from tensorflow.python._pywrap_tensorflow_internal import *` 中的 `_pywrap_tensorflow_internal` 到底是什么？

> **参考答案**：它是一个由 C++ 源码（位于 `tensorflow/python/lib/core/`，用 pybind11 封装）编译出来的 Python 扩展模块，在磁盘上是 `_pywrap_tensorflow_internal.so`（Linux/macOS）或 `.pyd`（Windows）。`import *` 把它导出的 C++ 函数摊到 `pywrap_tensorflow` 命名空间，供上层 Python 代码调用。

**练习 2**：为什么在 `import _pywrap_tensorflow_internal` 之前要先折腾 `dlopen` 标志（57-64 行）？

> **参考答案**：因为该扩展模块内部还会依赖其它共享库（如 CUDA、`libtensorflow_framework`）。不同平台的默认动态链接符号可见性不同，可能导致 `.so` 加载时「找不到符号」。脚本通过 `set_dlopen_flags` 或设置 `RTLD_LOCAL`/`RTLD_GLOBAL` 来保证跨平台都能正确解析符号，加载完成后再把标志还原，避免污染全局。

---

## 5. 综合实践

把本讲两条线（打包线、导入线）串起来，完成下面这个综合任务：

**任务：从 wheel 到 `tf.constant`，画出完整因果链。**

1. **打包侧**：假设你手上有一个 `tensorflow-2.x.x-cp310-cp310-manylinux_2_27_x86_64.whl`。
   - 解开它（`.whl` 本质是 zip，直接解压即可），找到 `tensorflow/__init__.py`，用编辑器打开，搜索 `API IMPORTS`。确认：它不再是占位注释，而是一长串 `from tensorflow.python... import ...`。（这验证了 4.2 的「生成」机制。）
   - 在解开的目录里找到 `tensorflow/python/_pywrap_tensorflow_internal.so`，确认这个 C++ 扩展被打了进去。（这验证了 4.3 的桥，以及 4.1 的 `package_data`。）
2. **导入侧**：在该环境里运行：
   ```python
   # 示例代码：验证导入链
   import tensorflow as tf
   # (a) 顶层入口文件来自 site-packages（生成版），而非源码占位版
   print(tf.__file__)
   # (b) python/core/compiler 已被清理，不应作为公开属性出现
   for name in ("python", "core", "compiler"):
       print(name, hasattr(tf, name))   # 预期多为 False（或仅作内部用）
   # (c) constant 来自 API IMPORTS 区的导入，最终落到 C++ 内核
   print(tf.constant([1, 2, 3]))
   ```
3. **解释**：结合本讲源码，用一段话讲清楚 `tf.constant([1,2,3])` 之所以能工作，背后依次依赖了：`setup.py.tpl`/`build_pip_package.py`（把 `.so` 和生成版 `__init__.py` 打进 wheel）→ `import tensorflow` 执行生成版 `__init__.py` → `pywrap_tensorflow` 加载 `_pywrap_tensorflow_internal.so` → `API IMPORTS` 把 `constant` 导入为 `tf.constant` → 调用最终落到 C++ 内核。
4. **若无法本地验证**（没有安装 TF 或无法解开 wheel）：改成纯源码阅读版——按本讲给出的永久链接，依次打开 `setup.py.tpl`、`build_pip_package.py`、`api_template.__init__.py`、`pywrap_tensorflow.py`、`tensorflow/BUILD`，把 4.1.2 与 4.2.2 两张流程图抄一遍并自己补注，作为交付物。

## 6. 本讲小结

- **打包线**：Bazel 产物经 `build_pip_package.py` 整理（含 xla/tsl 的 vendoring 改写）后，由 `setup.py.tpl` 渲染出的 `setup.py` 调 `bdist_wheel` 产出 wheel；`setup.py.tpl` 里的 `_VERSION`、CUDA 版本等都是被 `setup_py` genrule 替换的占位。
- **导入线核心事实**：源码树的 `tensorflow/__init__.py` 只是占位文件；真正发布的是构建时由 `api_template.__init__.py` 经 `generate_apis` 生成、再由 `root_init_gen` 拷贝覆盖的版本。
- **导入链**：`import tensorflow as tf` 先经 `pywrap_tensorflow` 加载 C++ 内核，再 `tf2.enable()` 开启 TF2 行为，然后执行（展开后的）`API IMPORTS` 把 `@tf_export` 登记的符号组装成 `tf.*`。
- **pywrap 桥**：`pywrap_tensorflow.py` 通过 `from tensorflow.python._pywrap_tensorflow_internal import *` 加载承载 C++ 内核的 `.so`，并在加载前后调整/还原 `dlopen` 标志以兼容跨平台符号解析。
- **命名空间清理**：`del python`/`del core`/`del compiler` 是为了删掉「因子包导入而作为副作用绑定进来的内部子包名」，避免它们以 `tf.python`/`tf.core` 的形式泄漏到公开 API。
- **性能意识**：`tensorflow/python/__init__.py` 刻意保持极简，因为 `tensorflow.python` 会在每次启动时被导入，任何多余 import 都会拖慢冷启动。

## 7. 下一步学习建议

本讲把「项目如何打包、如何被导入」这条**外层**链路讲透了。接下来应当向**内层**深入：

- **u2-l1（Tensor、dtype 与 TensorShape）**：从「能 import」走到「能用数据」，先掌握张量这一核心数据对象在 Python 层的定义。
- **u1-l5（版本信息与 C++ public 接口）**：本讲的下一站，进入 `core/public/`，认识 `Session`/`SessionOptions` 等 C++ 稳定 API，为 u3 的执行模型做铺垫。
- **延伸阅读**：若你对「桥」的实现感兴趣，可提前浏览 `tensorflow/python/lib/core/` 下用 pybind11 封装 C++ 函数的源码（如 `pybind11_lib.h`、`py_func_lib.cc`），那是 `_pywrap_tensorflow_internal.so` 的 C++ 侧源头，将在 u4（Op/Kernel 注册机制）与 u4-l4（C API 与 pywrap）中系统讲解。
