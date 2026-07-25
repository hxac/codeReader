# 版本信息与 C++ public 接口

## 1. 本讲目标

在前四讲里，我们已经知道了 TensorFlow 是什么、仓库怎么布局、Bazel 怎么构建、`import tensorflow as tf` 怎么把 Python 和 C++ 拼起来。这些大多发生在 **Python 侧**。本讲我们要第一次真正踏进 **C++ 核心**，进入 `tensorflow/core/public/` 这个目录。

读完本讲，你应当能够：

1. 说清楚 `core/public/` 为什么被称为「稳定的 C++ API 边界」，它和 `core/framework/`、`core/common_runtime/` 这些内部目录的分工差别。
2. 看懂 `Session` 与 `SessionOptions` 这两个核心抽象——它们是 C++ 侧驱动一张计算图执行的入口，也是后续 u3（计算图与执行模型）整章的起点。
3. 区分 TensorFlow 里「两套版本号」：`version.h` 里的 **GraphDef / Checkpoint 兼容版本** 和 `release_version.h` 里的 **语义版本号**（`TF_VERSION_STRING`），并能解释它们各自管什么。

本讲是「认知层」的最后一讲，它把读者的视线从 Python 表面拉到 C++ 内核的门口，为进入 u2（张量概念）和 u3（执行模型）打好地基。

## 2. 前置知识

在开始之前，请确认你已经具备下面这些概念（u1-l1 ~ u1-l4 已建立）：

- **稳定 API**：TensorFlow 官方只保证 **Python 与 C++** 两套接口向后兼容，其它语言绑定不保证（见 u1-l1、u1-l2）。
- **op / kernel**：计算图里的每个运算节点（op）最终由某个 C++ kernel 真正执行（u1-l1 引入，u4 会深入）。
- **pywrap 桥**：Python 通过 `pywrap_tensorflow` 加载承载 C++ 内核的 `.so`，从而调用底层能力（u1-l4）。
- **Graph 模式**：把计算先描述成一张图，再由运行时去执行它（与 Eager 立即执行相对，u3 会展开）。

本讲用到的几个 C++ 基础概念，这里先做通俗解释：

- **抽象类（abstract class / 接口）**：C++ 里用「纯虚函数」（`= 0`）定义一个只规定「要做什么」但不规定「怎么做」的类。子类必须实现这些函数。`Session` 就是这样一个抽象类。
- **工厂模式（factory）**：不直接 `new` 一个具体对象，而是调用一个「工厂」函数，由工厂根据配置决定创建哪一种具体实现。`NewSession()` 就是工厂入口。
- **协议缓冲（protobuf / `.pb.h`）**：Google 的二进制序列化格式。`GraphDef`、`ConfigProto` 都是用 protobuf 定义的结构化数据，序列化后可以存盘、跨进程传输。
- **语义化版本（semver）**：`主版本.次版本.修订号`（major.minor.patch）的版本号约定，参见 http://semver.org/ 。

## 3. 本讲源码地图

本讲涉及的关键文件都集中在 `tensorflow/core/public/`，外加两个用于补充说明的外围文件：

| 文件 | 作用 |
| --- | --- |
| `tensorflow/core/public/README.md` | 一句话说明 core 是什么，并给出 Python / C++ 两个最小调用示例。 |
| `tensorflow/core/public/version.h` | 定义 **GraphDef 与 Checkpoint 的兼容版本宏**（注意：不是软件发布版本号）。 |
| `tensorflow/core/public/release_version.h` | 定义 **语义发布版本号** `TF_VERSION_STRING`，真正的「TensorFlow 2.22.0」。 |
| `tensorflow/core/public/session_options.h` | 定义 `SessionOptions` 结构体，描述「在哪里、用什么配置」创建会话。 |
| `tensorflow/core/public/session.h` | 定义 `Session` 抽象类与工厂函数 `NewSession()`，是 C++ 侧驱动图计算的入口。 |
| `tensorflow/tf_version.bzl` | 语义版本号的真实来源（`TF_VERSION = "2.22.0"`），由 Bazel 注入 `release_version.h`。 |
| `tensorflow/core/common_runtime/session.cc` | `NewSession()` 的实现：通过工厂选择具体的 Session 实现。 |
| `tensorflow/core/common_runtime/session_factory.h` | 定义 `SessionFactory` 工厂接口与全局注册表。 |
| `tensorflow/core/common_runtime/direct_session.cc` | `DirectSession`（本地单进程会话）的实现与工厂注册，是默认的 Session 实现。 |

> 说明：后三个文件属于 `core/common_runtime/`（内部实现区，**不是**稳定 public API），本讲只在「NewSession 是怎么选到具体实现的」这一点上稍作引用，作为承上启下；它们的精读留到 u3-l2。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：API 边界 → 版本号 → 会话配置 → 会话接口与工厂。

### 4.1 core/public 目录：稳定的 C++ API 边界

#### 4.1.1 概念说明

一个大框架的源码通常会区分「**对外公开的稳定接口**」和「**内部实现细节**」。TensorFlow 在 C++ 层用目录命名来划这条线：

- `tensorflow/core/public/` —— 公开、稳定的 C++ 头文件。下游 C++ 程序、其它子项目（如 TFLite、TFRT）都应该只 `#include` 这里的头文件。
- `tensorflow/core/framework/`、`tensorflow/core/common_runtime/`、`tensorflow/core/graph/` 等 —— 内部实现。这里的头文件随时可能重构，外部不应直接依赖。

这条边界和 u1-l1 讲的「只有 Python 与 C++ 是稳定 API」一脉相承：Python 的稳定入口是 `tensorflow/__init__.py` 暴露的 `tf.*`，而 C++ 的稳定入口就是 `core/public/` 里的这几个头文件。

这个目录刻意保持「小而稳」。它只有 6 个文件：`BUILD`、`README.md`、`release_version.h`、`session.h`、`session_options.h`、`version.h`。文件少、改动慢，正是「稳定接口」该有的样子。

#### 4.1.2 核心流程

一个典型的 C++ 调用方使用 public 边界的方式：

```text
1. #include "tensorflow/core/public/session.h"        // 只依赖 public
   #include "tensorflow/core/public/session_options.h"
2. 用 SessionOptions 描述「目标 runtime（target）」和「配置（ConfigProto）」
3. 调用 NewSession(options) 拿到一个 Session* 指针
4. 用 Session::Create(graph) 把一张 GraphDef 注册进会话
5. 用 Session::Run(...) 执行图、取回结果
6. 用 Session::Close() 释放资源
```

注意第 1 步：调用方**只**包含 public 头文件，`GraphDef`、`Tensor` 这些类型由 public 头文件再间接 `#include` 进来（它们其实来自 `core/framework/`，但作为 public 头的「依赖」被合法地传递）。这就是 public 边界的作用——它是一道门，门里是海量内部代码，门外是稳定的几个入口。

#### 4.1.3 源码精读

README 对 core 的一句话定位，强调它本质是一个**计算数据流图库**：

[tensorflow/core/public/README.md:1-4](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/public/README.md#L1-L4) —— 把 TensorFlow core 定义为 "a computational dataflow graph library"，并给出 Python 与 C++ 两段最小示例。

其中 C++ 示例清晰地展示了「只用 public 头 + Session 三步走」的范式：

[tensorflow/core/public/README.md:47-64](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/public/README.md#L47-L64) —— `#include` 了 `session.h`，然后 `NewSession({})` 建会话、`session->Create(graph)` 装图、`session->Run(...)` 执行。

这就是 C++ 侧最稳的「骨架」，后续 u3-l2 讲 `DirectSession` 内部时，正是展开这骨架背后发生了什么。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：亲手确认 `core/public/` 确实是一个「小而稳」的目录。
2. **操作步骤**：
   - 在仓库根目录列出 `tensorflow/core/public/` 的全部文件。
   - 用 Grep 统计 `session.h` 顶部的 `#include` 行，看它都从哪些目录拉入了依赖。
3. **需要观察的现象**：
   - 该目录文件数量极少（约 6 个）。
   - `session.h` 的 include 里有 `core/framework/...` 的头文件（如 `graph.pb.h`、`tensor.h`）——说明 public 头会把必要的内部类型「有控制地」暴露出来。
4. **预期结果**：你会直观感受到 public 目录「文件少」的特点，并理解「稳定接口」不等于「不依赖内部」，而是「内部依赖是受控且向后兼容的」。
5. 运行环境的目录列举结果以你本地实际为准（本讲引用的文件清单见第 3 节）。

#### 4.1.5 小练习与答案

**练习 1**：如果一个外部 C++ 项目直接 `#include "tensorflow/core/common_runtime/direct_session.h"`，这违反了 public 边界的什么约定？

> **参考答案**：`common_runtime/` 属于内部实现区，其头文件不保证向后兼容，随时可能被重构或删除。正确做法是只包含 `core/public/session.h` 并通过 `NewSession()` 间接获得具体实现，从而把「用哪个 Session」的决策留给运行时，而不是编译期耦合到 `DirectSession`。

**练习 2**：为什么 `core/public/` 的文件数量被刻意控制得很小？

> **参考答案**：公开 API 的每一项都是长期承诺——一旦发布就难以删除或改名。文件少意味着承诺面小、维护成本低、稳定性高。内部的 `framework/`、`common_runtime/` 则可以自由演化。

---

### 4.2 version.h：GraphDef 与 Checkpoint 的兼容版本

#### 4.2.1 概念说明

很多人第一次打开 `version.h` 会困惑：「TensorFlow 的版本号 2.22.0 在哪里？」——**它不在 `version.h` 里**。这是一个极易踩的认知坑，本模块专门讲清楚。

`version.h` 里定义的，是**数据格式的兼容版本**，分两套：

- **GraphDef 兼容版本**：一张计算图（`GraphDef`）被序列化成 protobuf 后，记录了自己的「producer 版本」和「min_consumer 版本」。运行时（消费者）读这张图时，要判断「我能不能理解这份图」。
- **Checkpoint 兼容版本**：变量检查点（SavedSliceMeta）的格式版本，语义类似，但编号独立。

为什么需要这套机制？因为计算图可以被保存、传输、在**不同版本**的 TensorFlow 之间加载。比如用旧版 TF 存的图，新版 TF 要不要支持？某个有 bug 的中间版本要不要被「拉黑」？这套版本号就是用来做这种**向前/向后兼容判定**的。

而读者期待的「2.22.0」这种**软件发布版本号**，在另一个文件 `release_version.h` 里（见 4.3）。

#### 4.2.2 核心流程

`version.h` 顶部注释说明了 GraphDef 兼容性的判定规则：消费者会执行一张图，当且仅当三个条件同时成立。用逻辑式表达为：

\[
\text{execute} \iff (v_{\text{consumer}} \geq \text{graph.min\_consumer}) \;\land\; (\text{graph.producer} \geq v_{\text{consumer}}\text{'s min\_producer}) \;\land\; (v_{\text{consumer}} \notin \text{graph.bad\_consumers})
\]

其中 \(v_{\text{consumer}}\) 是消费者自身版本。三个宏分别对应：

- `TF_GRAPH_DEF_VERSION`：当前 TF 产生新图时写入的 **producer 版本**。
- `TF_GRAPH_DEF_VERSION_MIN_CONSUMER`：当前 TF 愿意消费的最小 consumer 版本。
- `TF_GRAPH_DEF_VERSION_MIN_PRODUCER`：当前 TF 愿意消费的最小 producer 版本。

这套版本号从 2019/05/09 起改为「按日期递增」，每天 +1。`version.h` 里那一长串历史注释（版本 0~30）记录了每一次破坏性格式变更的原因，是 TF 演化史的「活化石」。

#### 4.2.3 源码精读

三个核心宏的当前取值，注释里写明 `TF_GRAPH_DEF_VERSION` 更新到了 2474：

[tensorflow/core/public/version.h:94-96](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/public/version.h#L94-L96) —— 定义 `TF_GRAPH_DEF_VERSION_MIN_PRODUCER = 0`、`TF_GRAPH_DEF_VERSION_MIN_CONSUMER = 0`、`TF_GRAPH_DEF_VERSION = 2474`（Updated: 2026/1/16）。

Checkpoint 那套宏结构对称，但编号独立（长期停留在 1，因为 TF 不打算破坏 checkpoint 兼容性）：

[tensorflow/core/public/version.h:108-110](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/public/version.h#L108-L110) —— 定义 `TF_CHECKPOINT_VERSION_MIN_PRODUCER = 0`、`TF_CHECKPOINT_VERSION_MIN_CONSUMER = 0`、`TF_CHECKPOINT_VERSION = 1`。

文件顶部还有一句关于语义化版本的声明，但它只是「TF 遵循 semver」的说明，真正的 semver 数值并不在本文件：

[tensorflow/core/public/version.h:19](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/public/version.h#L19) —— 注释 "TensorFlow uses semantic versioning, see http://semver.org/."。这是初学者误以为「2.22.0 在这里」的根源。

辅助的字符串化宏（把数字宏变成字符串，供日志/接口拼接版本号用）：

[tensorflow/core/public/version.h:21-22](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/public/version.h#L21-L22) —— `TF_STR_HELPER` / `TF_STR`，是 C 宏「两层展开」的标准写法。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：从源码注释中读懂 GraphDef 版本号的演化逻辑。
2. **操作步骤**：
   - 打开 `tensorflow/core/public/version.h`。
   - 阅读从第 37 行开始的 "Version history" 注释块，找到「版本 30」对应的说明。
3. **需要观察的现象**：版本 30（2019/05/09）是一条分水岭——**从这天起 GraphDef 版本号每天 +1**。之前的版本号是按「破坏性变更事件」编号（0~30），之后改为按日期。
4. **预期结果**：你能解释为什么 `TF_GRAPH_DEF_VERSION` 高达 2474 而 `TF_CHECKPOINT_VERSION` 只有 1——前者每天递增、且不表示 API 破坏；后者只在真有破坏时才动。
5. 这些都是源码阅读型结论，无需运行；具体行号以你本地 HEAD 为准。

#### 4.2.5 小练习与答案

**练习 1**：`TF_GRAPH_DEF_VERSION = 2474` 是否意味着 TensorFlow 的 API 已经改了 2474 次？

> **参考答案**：不是。2474 是「按日期递增」累计的结果（自 2019/05/09 起每天 +1），它表示 GraphDef 序列化格式的 producer 版本，而不是 API 破坏次数。真正的语义发布版本是 `TF_VERSION_STRING`（见 4.3）。

**练习 2**：为什么 Checkpoint 版本长期停在 1？

> **参考答案**：`version.h` 注释明说「我们没有计划废弃 checkpoint 版本」。Checkpoint 格式刻意保持稳定，是为了让用户旧训练的权重始终能被新版加载；只有真发生不可逆破坏时才会动它，所以它几乎不变。

---

### 4.3 release_version.h 与 tf_version.bzl：语义版本号 TF_VERSION_STRING

#### 4.3.1 概念说明

「TensorFlow 2.22.0」这种**软件发布版本号**来自 `release_version.h`。它的巧妙之处在于：头文件本身**不写死**数字，而是声明「`TF_MAJOR_VERSION` / `TF_MINOR_VERSION` / `TF_PATCH_VERSION` / `TF_VERSION_SUFFIX` 这四个宏必须由外部定义」，再用宏拼接出 `TF_VERSION_STRING`。

数字的真正来源是 Bazel 构建文件 `tensorflow/tf_version.bzl`，其中 `TF_VERSION = "2.22.0"`。Bazel 在构建时把 `2`、`22`、`0` 注入为编译期宏。这样做的好处是：**版本号只有一个源头（tf_version.bzl），却能为 C++、Python wheel、文档等多处复用**，避免多处手写、各处不一致。

这个模块要建立的心智模型是：**版本号有两套，不要混淆。**

| 文件 | 是哪套版本 | 代表什么 |
| --- | --- | --- |
| `version.h` | GraphDef / Checkpoint 兼容版本 | 数据格式兼容性（2474 / 1） |
| `release_version.h` + `tf_version.bzl` | 语义发布版本 | 软件发行号（2.22.0） |

#### 4.3.2 核心流程

语义版本号从定义到可见的链路：

```text
tf_version.bzl:  TF_VERSION = "2.22.0"
        │  split(".") → MAJOR=2, MINOR=22, PATCH=0
        ▼
Bazel 构建把这三个数注入为编译宏 TF_MAJOR_VERSION / TF_MINOR_VERSION / TF_PATCH_VERSION
        ▼
release_version.h: 用 #error 断言「这四个宏必须被定义」，再拼成 TF_VERSION_STRING
        ▼
C++ 代码里可直接使用 TF_VERSION_STRING（如日志、版本接口）
```

`#error` 断言是一道保险：如果有人直接 `#include` 了 `release_version.h` 却没经过 Bazel 注入，编译就会立刻报「`TF_MAJOR_VERSION is not defined!`」，强制走正确的构建路径。

#### 4.3.3 源码精读

版本号的唯一真实来源在 `tf_version.bzl`：

[tensorflow/tf_version.bzl:26-27](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/tf_version.bzl#L26-L27) —— `TF_VERSION = "2.22.0"`，并 `split(".")` 得到主/次/修订号。注释说明这些常量被 C++ release_version、Python wheel、setup.py 三处复用。

`release_version.h` 用 `#error` 强制外部必须定义这四个宏：

[tensorflow/core/public/release_version.h:29-43](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/public/release_version.h#L29-L43) —— 对 `TF_MAJOR_VERSION`、`TF_MINOR_VERSION`、`TF_PATCH_VERSION`、`TF_VERSION_SUFFIX` 逐个做 `#ifndef ... #error` 断言。

最后拼接出可用的版本字符串：

[tensorflow/core/public/release_version.h:46-48](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/public/release_version.h#L46-L48) —— `TF_VERSION_STRING` 把三个数字和后缀拼成形如 `"0.5.0"` 或 `"0.6.0-alpha"` 的字符串（注释举的例）。它复用了 4.2 里见过的「两层字符串化」技巧。

#### 4.3.4 代码实践（源码阅读型 + 可选运行）

1. **实践目标**：验证「版本号只有一个源头」并理解构建期注入。
2. **操作步骤**：
   - 在 `tf_version.bzl` 中确认 `TF_VERSION` 的取值（本讲引用的 HEAD 下为 `"2.22.0"`）。
   - 阅读 `release_version.h`，设想若直接编译它会因缺宏而报 `#error`。
   - （可选运行）如果你已装好 TF 的 Python 包，执行 `python -c "import tensorflow as tf; print(tf.__version__)"`，观察输出。
3. **需要观察的现象**：`tf.__version__` 的字符串应与 `tf_version.bzl` 里的 `TF_VERSION` 一致（wheel 构建可能追加 `VERSION_SUFFIX` 后缀，如日期标记，见 release_version.h 第 22 行注释）。
4. **预期结果**：版本号在 `tf_version.bzl` 定义、经 Bazel 注入 C++、经 setup.py 写进 wheel，三处同源。
5. 若你无法本地运行 Python 包，则标注为「待本地验证」，仅完成源码阅读部分即可。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `release_version.h` 用 `#error` 而不是直接写 `#define TF_MAJOR_VERSION 2`？

> **参考答案**：为了让 `tf_version.bzl` 成为版本的唯一来源，由 Bazel 在构建时根据 `tf_version.bzl` 的值注入这些宏。如果头文件写死数字，C++、Python、wheel、文档就会各有一份版本号，难以同步。`#error` 则保证「不经过正确构建就拿不到版本号」，防止误用。

**练习 2**：`TF_VERSION_STRING` 和 `TF_GRAPH_DEF_VERSION` 谁更接近用户口中的「TF 版本」？

> **参考答案**：`TF_VERSION_STRING`（如 `"2.22.0"`）。`TF_GRAPH_DEF_VERSION`（2474）是内部数据格式版本，用户日常不会接触。

---

### 4.4 Session 与 SessionOptions：驱动图计算的入口

#### 4.4.1 概念说明

`Session` 是 TensorFlow C++ 侧最核心的执行入口。一句话概括：**一个 `Session` 实例让调用方驱动一张 TensorFlow 图的计算**。

它的生命周期是：创建（`NewSession`）→ 装图（`Create`）→ 执行（`Run`）→ 关闭（`Close`）。这与 README 里那段 C++ 示例完全对应。

而 `SessionOptions` 是「创建会话时的配置」，它只回答两个关键问题：

1. **target**：连到哪个 TensorFlow 运行时？空字符串 = 本地进程内运行时；填 `ip:port` 或 `host:port` = 远程运行时。这是单机与分布式的分水岭。
2. **config**：`ConfigProto`，一个 protobuf，里面塞满了诸如「是否开启 XLA、GPU 内存上限、设备可见性、线程池规模」等运行时开关。

`Session` 本身是**抽象类**：`session.h` 只声明 `Run`、`Create`、`Close`、`Extend`、`ListDevices` 等纯虚函数，不提供具体实现。具体实现由子类（本地用 `DirectSession`，远程用 gRPC 会话）给出。**调用方不直接 `new` 子类，而是通过 `NewSession()` 工厂获得指针**——这样上层代码就和「本地还是远程」解耦了。这正是本讲通往 u3（执行模型）的钥匙。

#### 4.4.2 核心流程

从配置到执行一次 `Run` 的完整链路：

```text
用户构造 SessionOptions（env / target / config）
        │
        ▼
调用 NewSession(options)            ← 工厂入口（session.h 声明，session.cc 实现）
        │
        ▼
SessionFactory::GetFactory(options) ← 根据 target 选工厂（本地 vs 远程）
        │
        ▼
factory->NewSession(options)        ← 工厂创建具体实现，如 DirectSession
        │
        ▼
返回 Session* 给调用方
        │
        ▼
session->Create(GraphDef)  → 把图注册进会话
session->Run(...)          → 调度执行 op，回收输出 Tensor
session->Close()           → 释放资源
```

注意工厂选择这一步：当 `target` 为空时，运行时通常选中 `DirectSession`（它以 `"DIRECT_SESSION"` 名字注册了自己）；当 `target` 指向远程时，则选中对应的远程会话工厂。具体的 `DirectSession::Run` 内部（放置、调度、执行、回收）会在 u3-l2 详细展开。

#### 4.4.3 源码精读

`Session` 抽象类的定义和文档注释，明确其职责：

[tensorflow/core/public/session.h:88-91](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/public/session.h#L88-L91) —— `class Session` 开头，文档说明 "A Session instance lets a caller drive a TensorFlow graph computation"，并强调 `Run` 可并发调用、但创建/扩展须单线程。

`Session` 的核心**纯虚方法**（子类必须实现），构成最小可用的会话契约：

- [tensorflow/core/public/session.h:98](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/public/session.h#L98) —— `Create(const GraphDef& graph) = 0`：把图注册进会话，重复 Create 会报错。
- [tensorflow/core/public/session.h:108](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/public/session.h#L108) —— `Extend(const GraphDef& graph) = 0`：在已注册图上追加新 op。
- [tensorflow/core/public/session.h:132-136](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/public/session.h#L132-L136) —— `Run(inputs, output_tensor_names, target_tensor_names, outputs) = 0`：执行图，喂入输入、取回指定输出、顺便触发但不取回若干目标节点。
- [tensorflow/core/public/session.h:218](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/public/session.h#L218) —— `ListDevices(...) = 0`：列出会话内可用设备。
- [tensorflow/core/public/session.h:225](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/public/session.h#L225) —— `Close() = 0`：关闭会话、释放资源。

此外还有一批**非纯虚、带默认 `UnimplementedError` 实现**的「实验性」方法（如带 `RunOptions` 的 `Run`、`PRunSetup`/`PRun` 部分执行、`MakeCallable`/`RunCallable` 子图调用、`Finalize`）。它们用默认实现的方式，让具体子类可以「按需选择实现」，而不必全部覆盖。`CallableHandle` 的定义见 [session.h:238](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/public/session.h#L238)。

工厂函数有两个重载，推荐用返回 `Status` 的那个：

[tensorflow/core/public/session.h:318](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/public/session.h#L318) —— `NewSession(const SessionOptions&, Session**)`：成功时把新 Session 写入 `*out_session` 并返回 `OK()`，是**推荐**用法（注释见下方 [session.h:355](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/public/session.h#L355) 的另一个重载，后者失败只返回 `nullptr`，错误信息更少）。

`SessionOptions` 结构体极简，只有三个字段：

[tensorflow/core/public/session_options.h:29-62](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/public/session_options.h#L29-L62) —— `struct SessionOptions`，包含 `tsl::Env* env`（环境）、`std::string target`（目标 runtime，空 = 本地）、`ConfigProto config`（配置）。`target` 的注释详细说明了 `local` / `ip:port` / `host:port` 等地址格式。

工厂实现——`NewSession` 通过工厂表选择具体实现（这是 public→internal 的衔接点）：

[tensorflow/core/common_runtime/session.cc:80-98](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/common_runtime/session.cc#L80-L98) —— `NewSession` 先 `SessionFactory::GetFactory(options, &factory)` 选工厂，再 `factory->NewSession(options, out_session)` 造实例；失败则置空并记日志。

工厂接口与全局注册表的定义：

[tensorflow/core/common_runtime/session_factory.h:31-73](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/common_runtime/session_factory.h#L31-L73) —— `SessionFactory` 抽象类声明 `NewSession` / `AcceptsOptions` / `Reset`，并提供静态 `Register`（注册）与 `GetFactory`（按 options 查询）。

默认实现 `DirectSession` 是如何在程序启动时把自己注册进工厂表的：

[tensorflow/core/common_runtime/direct_session.cc:296-302](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/common_runtime/direct_session.cc#L296-L302) —— 一个静态全局对象 `registrar`，其构造函数调用 `SessionFactory::Register("DIRECT_SESSION", new DirectSessionFactory())`，利用 C++ 静态初始化完成「自动注册」（和 u4 将要讲的 op 注册同构）。

#### 4.4.4 代码实践（源码阅读型）

本练习直接对应本讲的总实践任务。

1. **实践目标**：列出 `Session` 类暴露的核心公有方法，并理解工厂如何选出 `DirectSession`。
2. **操作步骤**：
   - 打开 `tensorflow/core/public/session.h`，定位 `class Session`（约 L88）。
   - 把其中带 `= 0` 的纯虚函数整理成一张表（Create / Extend / Run / ListDevices / Close）。
   - 再把带默认 `UnimplementedError` 的实验性方法另列一组。
   - 追踪 `NewSession()`（session.cc L80）→ `SessionFactory::GetFactory` → `factory->NewSession`，并到 direct_session.cc L296 看注册。
3. **需要观察的现象**：
   - `Session` 的公有方法分两类：必须实现的纯虚函数（稳定核心）和可选的实验性方法（带默认实现）。
   - `NewSession` 调用方代码里**没有出现** `DirectSession` 字样——具体实现是被「注册 + 查表」间接选中的。
4. **预期结果**：你能画出「SessionOptions → NewSession → SessionFactory → DirectSession」的对象创建链，并解释为何上层只依赖抽象 `Session` 而不依赖具体类。
5. 这是源码阅读型实践，无需编译运行；行号以你本地 HEAD 为准。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Session` 用纯虚函数（抽象类）而不是直接给一个 `Run` 的实现？

> **参考答案**：因为同一个 `Run` 接口背后，可能有不同的运行时实现——本地进程内的 `DirectSession`、跨进程的 gRPC 会话，甚至未来的 TFRT 会话。用抽象类 + 工厂，上层代码只依赖 `Session*` 接口，「本地还是远程」由 `target` 在运行时决定，实现了接口与实现的解耦。

**练习 2**：`SessionOptions::target` 为空字符串和为 `"grpc://host:port"` 时，分别会发生什么？

> **参考答案**：为空时 `SessionFactory::GetFactory` 通常选中本地工厂，创建出 `DirectSession`（进程内执行）；填远程地址时，选中能接受该 `target` 的远程会话工厂（如 gRPC 会话），后续 `Run` 通过网络把请求发到远端 runtime。这是「单机 vs 分布式」在 API 层的差异点，u6 的 distribute 会再展开。

**练习 3**：`DirectSessionRegistrar` 是怎么做到「不需要调用方手动注册」的？

> **参考答案**：它是一个文件作用域的静态全局对象。C++ 保证静态全局对象在 `main` 执行前完成构造，因此只要链接了 `direct_session.cc`，其构造函数里的 `SessionFactory::Register(...)` 就会自动执行，把 `DirectSession` 登记进工厂表。这种「静态初始化做自动注册」的范式在 TF 里很常见，op 的 `REGISTER_OP` 也是同类思想。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「**读源码画一张 C++ 入口认知图**」的任务：

1. **画版本号双轨图**：在一张纸上分两栏，左栏写 `version.h` 管的内容（GraphDef 兼容版本 `TF_GRAPH_DEF_VERSION=2474`、Checkpoint 兼容版本 `=1`，以及 `MIN_PRODUCER`/`MIN_CONSUMER`），右栏写 `release_version.h` + `tf_version.bzl` 管的内容（语义版本 `TF_VERSION_STRING="2.22.0"`，来源 `tf_version.bzl`）。并在两栏之间画一条醒目的「**不要混淆**」分隔线。
2. **画 Session 生命周期时序**：按 `SessionOptions → NewSession → SessionFactory::GetFactory → factory->NewSession → DirectSession → Create → Run → Close` 的顺序画出调用时序，标出每一步发生在哪个文件（public 还是 common_runtime）。
3. **判断边界**：对时序图里的每个文件，标注它属于「稳定 public API」还是「内部实现」。指出哪一步是「跨过边界」的关键一步（答案：`NewSession` 的实现位于 `common_runtime/session.cc`，调用它就是从 public 跨进 internal）。

> 这个任务不需要你编译或运行任何代码，重点是建立「public 边界 + 两套版本号 + 抽象 Session + 工厂」的整体心智模型。完成后，你就具备了进入 u2（张量与基本概念）和 u3（执行模型）的全部前置认知。

## 6. 本讲小结

- `tensorflow/core/public/` 是 TensorFlow C++ 侧**稳定的对外 API 边界**，文件少而稳；外部 C++ 程序应只依赖这里，内部 `common_runtime/` 等不保证兼容。
- **版本号有两套，不要混淆**：`version.h` 是 **GraphDef / Checkpoint 数据格式**的兼容版本（`TF_GRAPH_DEF_VERSION=2474`、`TF_CHECKPOINT_VERSION=1`）；`release_version.h` 配合 `tf_version.bzl` 才是**软件发布版本号**（`TF_VERSION_STRING="2.22.0"`）。
- `release_version.h` 用 `#error` 强制版本号由 Bazel 在构建期从 `tf_version.bzl` 注入，保证「版本号只有一个源头」。
- `Session` 是驱动图计算的**抽象入口**，核心契约是 `Create / Extend / Run / ListDevices / Close` 五个纯虚方法，外加一批带默认实现的实验性方法。
- `SessionOptions` 回答两个问题——连到哪（`target`）、怎么配（`ConfigProto`）；空 target = 本地运行时。
- `NewSession()` 是**工厂入口**：通过 `SessionFactory::GetFactory` 选工厂、再 `factory->NewSession` 造实例；默认的 `DirectSession` 借 C++ 静态全局对象在程序启动时自动注册为 `"DIRECT_SESSION"`。

## 7. 下一步学习建议

本讲把读者带到了 C++ 内核的门口。接下来：

1. **u2 张量与基本概念**：在进入 `DirectSession::Run` 的细节前，先掌握 `Tensor`、`dtype`、`TensorShape`、`Variable`、`Operation` 这些会在执行链路里反复出现的数据对象（主要看 `tensorflow/python/framework/`）。
2. **u3 计算图与执行模型**：本讲的 `Session` 是 u3 的主角。u3-l1 会讲 `Graph/Node/Edge` 与 `GraphDef` 的关系，u3-l2 会**展开 `DirectSession::Run` 的内部**（放置、调度、执行、回收），把本讲只点到为止的工厂与执行真正讲透。
3. **延伸阅读**：可先扫一眼 `tensorflow/core/common_runtime/direct_session.cc` 的 `DirectSessionFactory`（约 L205 起），提前感受一下「工厂 + 自动注册」在内部实现里的完整长相，为 u3 预热。
