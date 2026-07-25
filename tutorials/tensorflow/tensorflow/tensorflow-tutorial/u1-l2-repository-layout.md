# 顶层目录结构与仓库布局

## 1. 本讲目标

上一讲（u1-l1）我们确立了 TensorFlow 的定位，并掌握了「定位—构建—语言入口」三类文件这套建立全局认知的方法。本讲要在这套方法上更进一步，解决一个非常实际的问题：

> **打开 tensorflow/tensorflow 这个庞大仓库时，几十个顶层目录、成千上万个文件，到底每个目录是干什么的？我想找的某段功能（比如一个 op 的实现、一个 Python API）应该去哪个目录里翻？**

学完本讲，你应当能够：

1. 说出仓库顶层三大区域（源码区、外部依赖区、工具与 CI 区）的划分逻辑。
2. 区分 `tensorflow/core`（C++ 核心）与 `tensorflow/python`（Python API）的边界，并看懂 `c`、`cc`、`compiler`、`lite` 等子目录各自对应的语言层。
3. 看懂 `WORKSPACE` 与 `MODULE.bazel` 如何组织外部依赖。
4. 在仓库中快速定位某一功能所属目录，而不是盲目搜索。

## 2. 前置知识

- **目录（directory）与构建目标（build target）**：TensorFlow 用 Bazel 构建，代码不仅按文件夹组织，还会在每个目录里放一个 `BUILD`（或 `BUILD.bazel`）文件，把目录里的源码声明为可构建的「目标」。目录划分既服务于「人读」，也服务于「机器构建」。
- **语言层（language layer）**：同一个机器学习框架，会同时提供 C++、Python、C 等多种语言的接口。这些接口并非各自独立实现，而是层层叠在一个 C++ 核心之上。我们把这些不同语言对应的代码集合称为不同的「语言层」。
- **依赖（dependency）**：TensorFlow 本身的代码在 `tensorflow/` 里，但它还依赖大量第三方库（如 protobuf、abseil、grpc）。这些外部库的获取与补丁管理放在 `third_party/`。
- **Bzlmod / MODULE.bazel**：Bazel 较新的依赖管理方式（基于模块）。TensorFlow 同时保留了传统的 `WORKSPACE` 文件和实验性的 `MODULE.bazel`。这一点在上一讲已提及，本讲我们会看具体内容。

如果你对 Bazel 还不熟悉，不用紧张——本讲只看「目录职责」，不涉及如何编译。具体构建流程是下一讲（u1-l3）的主题。

## 3. 本讲源码地图

本讲涉及的关键文件与目录如下：

| 路径 | 作用 |
| --- | --- |
| `README.md` | 项目说明，确立 TensorFlow 的定位与稳定 API 边界。 |
| `WORKSPACE` | 传统 Bazel 工作区声明，定义仓库名 `org_tensorflow` 并级联加载依赖。 |
| `MODULE.bazel` | 新式 Bzlmod 依赖声明，集中列出第三方模块版本与补丁。 |
| `tensorflow/` | **全部源码**所在目录，内部再按语言层（core/python/c/cc/lite/compiler 等）细分。 |
| `third_party/` | 第三方依赖的下载、补丁与 BUILD 模板。 |
| `tools/` | 仓库内的辅助脚本（如环境采集脚本）。 |
| `ci/` | 持续集成（CI）的配置与脚本。 |
| `ci/README.md` | 说明 `ci/` 目录的用途。 |

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：先看顶层三大区域，再看 `tensorflow/` 内部的语言层，再看依赖如何组织，最后给出一套「定位」方法论。

### 4.1 顶层目录的三大区域划分

#### 4.1.1 概念说明

站在仓库根目录看（用 `ls` 列一下），你会看到一大批文件和目录。乍看很乱，但其实可以归为三大区域：

1. **源码区**：`tensorflow/`。这是整个项目的「主角」，几乎所有 TF 自己的代码都在这里。
2. **外部依赖区**：`third_party/`。存放如何下载、打补丁、改写第三方库的配置。
3. **工具与基础设施区**：`tools/`、`ci/`、根目录的 `configure.py`、`WORKSPACE`、`MODULE.bazel` 等。它们不实现 ML 功能，而是支撑「构建、配置、测试、打包」。

之所以要这样划分，是因为 TensorFlow 是一个**跨语言、跨平台、依赖海量第三方库**的大型工程。把「自己的代码」和「别人的代码」、把「功能代码」和「工程基础设施」清晰隔开，才能让仓库可维护。

> 这套划分不是 TF 独有，而是大型 C++/Bazel 工程的通行做法：源码在一处、第三方依赖在 `third_party`、构建脚本散布在根目录与各子目录的 `BUILD` 文件中。

#### 4.1.2 核心流程

当你 clone 仓库并准备构建时，三大区域的协作流程大致是：

```
根目录 configure.py / .bazelrc   ←(1) 配置环境（CPU/GPU 等）
        │
        ▼
WORKSPACE / MODULE.bazel         ←(2) 声明并拉取 third_party/ 指定的外部依赖
        │
        ▼
tensorflow/ 各子目录的 BUILD     ←(3) 把源码区与依赖区的目标组装成可构建产物
        │
        ▼
ci/ 中的流水线配置               ←(4) 在 CI 上重复 (1)~(3) 并跑测试、打包
```

- 第 (1) 步是构建前的环境探测；
- 第 (2) 步让 Bazel 知道「需要哪些外部库、从哪里取」；
- 第 (3) 步是真正的编译组织；
- 第 (4) 步保证每次提交都能被自动化地构建与验证。

#### 4.1.3 源码精读

`README.md` 在开头就明确了 TensorFlow 的定位，这也是理解整个仓库「为什么这么大」的起点：

[README.md:19-25](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/README.md#L19-L25) — 这段说明 TensorFlow 是一个**端到端的机器学习开源平台**，拥有工具、库和社区生态。正因为「端到端」（从研究、训练到推理、部署都要覆盖），仓库里才会同时存在 Python、C++、移动端、编译器等多种代码。

紧接着它界定了「哪些是稳定 API」，这直接对应到不同语言层目录的优先级：

[README.md:32-35](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/README.md#L32-L35) — 明确 **Python 与 C++ 是稳定 API**，其他语言绑定**不保证向后兼容**。所以在仓库里你会看到 `tensorflow/python/` 和 `tensorflow/core/`（C++）最完善、最稳定，而 `go/`、`java/`、`js/` 等目录相对次要。

对于 `ci/` 目录，它自己有一份 README 说明职责：

[ci/README.md:13-17](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/ci/README.md#L13-L17) — 说明该目录存放用于**构建、测试、部署 TensorFlow 的配置文件与脚本**，并按支持级别和归属划分子目录（如 `ci/official/`、`ci/devinfra/`）。

#### 4.1.4 代码实践

这是一个「源码阅读 + 目录观察」型实践：

1. **实践目标**：亲手把顶层目录归入三大区域，建立空间感。
2. **操作步骤**：
   - 在仓库根目录执行 `ls -1`，列出所有顶层条目。
   - 对每一个条目，判断它属于「源码区 / 依赖区 / 工具与基础设施区」中的哪一类。
   - 对于拿不准的（比如 `models.BUILD`、`arm_compiler.BUILD`、`requirements_lock_3_1x.txt`），点开看一眼第一行注释再归类。
3. **需要观察的现象**：你会发现绝大多数「带功能含义」的东西都在 `tensorflow/` 里；而根目录散落的文件几乎都是构建/打包/锁文件（如 `.bazelrc`、`requirements_lock_*.txt`、`*.BUILD`）。
4. **预期结果**：根目录被清晰地分成三块——一个大源码目录、一个 `third_party/`、以及一堆工程基础设施文件。
5. 如果你无法运行命令（例如只读环境），可以直接对照本讲 4.1.1 的三大区域列表在脑中完成归类。

#### 4.1.5 小练习与答案

**练习 1**：根目录下的 `requirements_lock_3_11.txt` 属于哪一区域？它大概是什么？

> **答案**：属于「工具与基础设施区」。它是 Python 依赖的锁文件（pin 版本），用于复现 pip 依赖环境，与 ML 功能本身无关。

**练习 2**：为什么 `third_party/` 不直接并进 `tensorflow/` 里？

> **答案**：因为 `third_party/` 装的是**第三方代码**的获取与补丁配置，不属于 TensorFlow 自身源码；单独存放可以清楚区分「自有代码」与「外部依赖」，也方便升级和打补丁。

### 4.2 tensorflow/ 内部的语言层划分

#### 4.2.1 概念说明

进入 `tensorflow/` 目录后，你会看到一堆子目录，这是本讲最需要建立的心智模型。关键在于理解：**不同的子目录对应不同的「语言层」或「子系统」**。

下面这张表是本讲最重要的一张图，请记牢：

| 子目录 | 语言 / 角色 | 一句话职责 |
| --- | --- | --- |
| `core/` | **C++** | 框架的 C++ 核心：图、运行时、op/kernel 框架、设备、内存、分布式、profiler 等。是 Python API 的「地基」。 |
| `python/` | **Python** | Python API 实现：Tensor、Variable、Eager、tf.function、Keras、tf.data、自动微分等。 |
| `c/` | **C** | 语言无关的 C API 边界，供其他语言（Go、Java、JS 等）绑定调用。 |
| `cc/` | **C++** | 建立在 `core/` 之上的 C++ 客户端/高层封装（如 `cc/client`、`cc/saved_model`、`cc/training`）。 |
| `compiler/` | **C++ (编译器)** | MLIR / XLA / tf2xla / JIT 等编译流水线，把计算图编译为高效设备代码。 |
| `lite/` | **C++ (移动端)** | TensorFlow Lite，面向手机/嵌入式/边缘设备的轻量推理运行时。 |
| `java/`、`go/`、`js/` | 各语言 | 其他语言绑定（非稳定 API）。 |

还有几个重要目录不是语言层，而是「子系统/资料」：

- `examples/`：示例工程（如 `adding_an_op`、`label_image`、`android`）。
- `docs_src/`：文档源。
- `distribute/`、`dtensor/`、`security/`：特定子系统的代码或政策。
- `tools/`：与打包相关的辅助（如 `tensorflow/tools/pip_package`，pip 包打脚本就在这里，u1-l4 会用到）。

#### 4.2.2 核心流程

理解语言层的关键是「自底向上」的调用栈。虽然细节要到后续讲义才展开，但整体方向是：

```
tensorflow/python/  (用户写 tf.add 等 Python 调用)
        │  通过 pywrap / C API 桥接
        ▼
tensorflow/c/        (C API：语言无关边界)
        │
        ▼
tensorflow/core/     (C++ 核心：真正执行 op、调度设备、管理内存)
        │
        ▼  (可选，需要加速时)
tensorflow/compiler/ (MLIR/XLA 把子图编译成高效设备代码)
```

也就是说：你在 Python 里写的每一行代码，最终几乎都会穿过 `c`（或直接的 eager 桥）落到 `core` 的 C++ 实现上。而 `lite/` 则是另一条相对独立的「部署」支线——它把训练好的模型转换并跑在手机上。

> 一条经验法则：**「实现」去 `core/` 找，「Python 接口」去 `python/` 找，「其他语言入口」去 `c/`、`cc/` 或对应语言目录找，「编译加速」去 `compiler/` 找，「手机部署」去 `lite/` 找。**

#### 4.2.3 源码精读

我们用目录列表本身作为「源码」，验证上表的划分。先看 C++ 核心 `core/` 的内部：

`tensorflow/core/` 包含（节选）：`framework/`（op/kernel 框架）、`common_runtime/`（运行时、Session）、`graph/`（计算图数据结构）、`kernels/`（各种 op 的具体计算实现）、`ops/`（op 声明）、`grappler/`（图优化）、`distributed_runtime/`（分布式）、`public/`（稳定 C++ 公有头文件）等。可以看到 **`core/` 几乎是一个完整的 C++ 框架**。

再看 Python 层 `tensorflow/python/` 的内部（节选）：`framework/`（Tensor、ops、tensor_shape）、`ops/`（math_ops、array_ops 等包装）、`eager/`（Eager 执行、tf.function）、`keras/`（高层 API）、`data/`（tf.data）、`client/`（session、pywrap）、`autograph/`（Python 控制流转图）。这些正是日常 `tf.*` 用的东西。

C API 层入口文件直观可见：

[tensorflow/c/c_api.h](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/c/c_api.h) — C API 的主头文件，是「语言无关边界」，其他语言通过它调用 TF。同目录还有 `c_api.cc`、`eager/c_api.h` 等（u4-l4 会精读）。

C API 目录自带 README 说明其定位：

[tensorflow/c/README.md](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/c/README.md) — 标题即「TensorFlow C API」，并指向官方安装页 `install/lang_c`，证实 `c/` 就是 C 语言接口层。

而 `core/public/` 则是「稳定 C++ 接口」的存放处（上一讲 u1-l1 提到过稳定 API 概念，u1-l5 会精读）：

[tensorflow/core/public/session.h](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/public/session.h) — 稳定 C++ 入口 `Session` 所在。

#### 4.2.4 代码实践

1. **实践目标**：用 `ls` 亲自验证语言层划分，把抽象的表格对照到真实目录。
2. **操作步骤**：
   - 执行 `ls -1 tensorflow/`，确认 `core`、`python`、`c`、`cc`、`compiler`、`lite` 这些目录确实存在。
   - 执行 `ls -1 tensorflow/core/ | head -20`，观察 C++ 核心都包含哪些子模块。
   - 执行 `ls -1 tensorflow/python/ | head -20`，对比 Python 层的子模块。
3. **需要观察的现象**：`core/` 里满是 C++ 运行时与框架概念（kernels、ops、graph、runtime）；`python/` 里则是 Python 侧概念（eager、keras、data）。
4. **预期结果**：你能在脑中画一张「目录 → 语言层 → 职责」的对照表，和本讲 4.2.1 的表格一致。
5. 无法运行命令时，可直接参照 4.2.1 的表格与 4.2.3 的目录说明完成观察。

#### 4.2.5 小练习与答案

**练习 1**：我想看 `tf.add` 这个加法 op 的 **C++ 计算实现**（kernel），应该去哪个目录？

> **答案**：去 `tensorflow/core/kernels/`（op 的计算实现）和 `tensorflow/core/ops/`（op 声明）。`python/ops/math_ops.py` 只是 Python 包装。

**练习 2**：`tensorflow/cc/` 和 `tensorflow/core/` 都是 C++，区别在哪？

> **答案**：`core/` 是框架的 C++ **核心地基**（运行时、图、kernel）；`cc/` 是建立在 `core/` 之上的 C++ **客户端/高层封装**（如 `cc/client`、`cc/saved_model`、`cc/training`），面向用 C++ 直接使用 TF 的用户。

### 4.3 构建与依赖组织：WORKSPACE 与 MODULE.bazel

#### 4.3.1 概念说明

TensorFlow 体量极大，依赖众多（protobuf、abseil、grpc、eigen、cuda 工具链……）。这些外部依赖怎么管？仓库同时提供了两套机制：

- **`WORKSPACE`（传统方式）**：用 `workspace(name=...)` 声明工作区名，然后通过一系列 `tf_http_archive`、`tf_workspace` 宏拉取外部仓库。这是 Bazel 老式的依赖管理方式。
- **`MODULE.bazel`（Bzlmod 新方式）**：用 `module(name=...)` 声明自身模块，再用 `bazel_dep` 声明依赖、`use_extension` / `use_repo` 拉取大量第三方仓库。文件开头明确写着 `Experimental Bzlmod support`，说明它仍处于实验阶段。

两者目前并存。理解它们，主要是为了知道「这个仓库叫什么名字、它依赖哪些外部库、这些库的版本和补丁在哪」。

#### 4.3.2 核心流程

依赖被「组织」起来的过程可以概括为：

```
MODULE.bazel / WORKSPACE  ──(声明)──▶  模块名 + 一长串 bazel_dep / 外部仓库
        │
        │  其中很多仓库的真实「下载+打补丁」规则
        ▼
third_party/  里的 *.bzl / *.BUILD / patch 文件
        │
        ▼
Bazel 构建时把这些外部代码作为可引用的目标（如 @com_google_protobuf）
```

也就是说：`WORKSPACE`/`MODULE.bazel` 负责「点名」（要哪些依赖），而 `third_party/` 负责「具体怎么把每个依赖弄到手」（下载地址、sha256、补丁、BUILD 模板）。二者配合，Bazel 才能在构建时把外部代码当作正常目标使用。

#### 4.3.3 源码精读

先看 `WORKSPACE` 如何声明工作区名：

[WORKSPACE:18](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/WORKSPACE#L18) — `workspace(name = "org_tensorflow")`。这一行给了整个 Bazel 工作区一个全局名字 `org_tensorflow`，仓库内所有本地目标都以 `//...` 引用，而外部依赖以 `@<repo>` 引用。

再看它如何级联加载依赖（节选自文件前段）：`WORKSPACE` 通过 `load("//third_party:repo.bzl", ...)` 引入自定义宏 `tf_http_archive`、`tf_mirror_urls`，然后用它们逐个声明外部仓库；接着用 `load("@//tensorflow:workspace3.bzl", "tf_workspace3")` 把庞大的依赖列表拆分到 `tensorflow/workspace0.bzl`~`workspace3.bzl` 这几个文件里（你在 `tensorflow/` 目录列表里能看到这些 `workspace*.bzl`），避免 `WORKSPACE` 本身过长。

新式 `MODULE.bazel` 则更集中：

[MODULE.bazel:1-6](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/MODULE.bazel#L1-L6) — 首行注释 `Experimental Bzlmod support for TensorFlow`，随后 `module(name = "tensorflow", repo_name = "org_tensorflow")` 声明自身。注意 `repo_name = "org_tensorflow"` 与 `WORKSPACE` 里的 `workspace(name="org_tensorflow")` 保持一致，这样无论用哪种方式构建，外部看到的仓库名都相同。

紧接着的几十行 `bazel_dep(...)`（如 `abseil-cpp`、`protobuf`、`grpc`、`rules_cc`）就是「点名」——声明需要的第三方模块及版本；而 `single_version_override(...)` + `patches=[...]` 则把补丁指向 `third_party/` 下的具体文件（例如 `//third_party:grpc.patch`）。这正好体现了 4.3.2 所说的「MODULE.bazel 点名 + third_party/ 出力」的分工。

#### 4.3.4 代码实践

1. **实践目标**：在 `MODULE.bazel` / `WORKSPACE` 里追踪一个真实依赖，体会「点名 + 出力」的分工。
2. **操作步骤**：
   - 打开 `MODULE.bazel`，找到 `grpc` 相关的 `bazel_dep(...)` 与紧随其后的 `single_version_override(...)`。
   - 注意它的 `patches = ["//third_party:grpc.patch"]` 指向了 `third_party/` 下的文件。
   - 在仓库里确认 `third_party/grpc.patch` 这个文件确实存在。
3. **需要观察的现象**：依赖的「版本号、补丁位置」在 `MODULE.bazel` 里声明，而「补丁内容本身」躺在 `third_party/` 里。
4. **预期结果**：你能复述「`MODULE.bazel` 声明依赖 → `third_party/` 提供下载/补丁/BUILD 模板」的协作关系。
5. 不确定 `grpc.patch` 是否存在时，可在 `third_party/` 目录列表里直接核对（本讲已确认其存在）。

#### 4.3.5 小练习与答案

**练习 1**：`WORKSPACE` 和 `MODULE.bazel` 都把仓库对外名字设成了同一个值，是哪个？

> **答案**：`org_tensorflow`（`WORKSPACE` 用 `workspace(name=...)`，`MODULE.bazel` 用 `repo_name="org_tensorflow"`）。

**练习 2**：为什么要拆出 `tensorflow/workspace0.bzl`~`workspace3.bzl` 这些文件？

> **答案**：因为 `WORKSPACE` 传统方式下依赖列表极其庞大，全部堆在 `WORKSPACE` 里会难以维护；用 `.bzl` 宏文件拆分后，`WORKSPACE` 只需 `load` 并调用即可，结构更清晰。

### 4.4 在仓库中快速定位功能的方法论

#### 4.4.1 概念说明

知道目录职责后，还要会「找」。这一模块给出一套通用的定位方法论，让你在面对任意功能需求时不至于无从下手。核心是两步法：

1. **先判语言层**：这个功能属于 Python 接口、C++ 核心、C API，还是编译器/移动端？——决定你进 `python/`、`core/`、`c/`、`compiler/` 还是 `lite/`。
2. **再用工具检索**：用文件名 `Glob`（找文件）或内容 `Grep`（找符号/字符串）在候选目录里精确定位。

这套方法不依赖记忆，只依赖前面建立的「目录 → 语言层」对照表加上检索工具。

#### 4.4.2 核心流程

定位一个功能的通用流程：

```
1. 问自己：我要找的是「接口」还是「实现」？
        │
   接口？─→ python/ (Python) 或 core/public/、c/ (C++/C 稳定接口)
   实现？─→ core/kernels, core/ops, core/common_runtime ...
        │
2. 在候选目录里用 Glob 按文件名找：
     例如 Glob "**/math_ops.py"  →  找到 python/ops/math_ops.py
        │
3. 仍不确定时，用 Grep 按内容找：
     例如 Grep "REGISTER_OP(\"Add\")"  →  定位 C++ op 注册点
```

关键经验：

- **文件名即线索**：TensorFlow 的命名很规律，`math_ops`、`array_ops`、`constant_op`、`variables` 这类名字在 Python 层和 C++ 层往往对应存在（如 `python/ops/math_ops.py` 对应 `core/ops/math_ops.cc`）。
- **`REGISTER_OP` 是 op 的「出生证」**：C++ 侧每个 op 都用 `REGISTER_OP("OpName")` 注册，grep 它能瞬间定位某个 op 的声明。
- **`@tf_export` 是 Python API 的「出口标记」**：被这个装饰器标记的函数才会暴露为 `tf.*`。

#### 4.4.3 源码精读

由于本模块讲方法，这里不贴大段代码，而是给出几个「可直接复用的检索锚点」与它们对应的目录结论：

- 想找 `tf.constant` 的实现 → 文件名线索指向 `tensorflow/python/framework/constant_op.py`（u2-l2 会精读）。
- 想找某个 C++ op 的注册 → 在 `tensorflow/core/ops/` 下用 `REGISTER_OP("Add")` 之类检索（u4-l1 会精读 `core/framework/op.h`）。
- 想找 Session 执行主链路 → 跨语言层：Python 侧 `tensorflow/python/client/session.py`，C++ 侧 `tensorflow/core/common_runtime/direct_session.cc`（u3-l2 会精读）。

> 这些路径你现在不必记住，只要记住**「先判语言层、再按文件名/内容检索」**这套方法即可。本手册后续每一讲的开头「本讲源码地图」，其实就是在替你完成第一步的「判语言层」。

#### 4.4.4 代码实践

1. **实践目标**：用两步法亲手定位一个真实功能，把方法论跑通。
2. **操作步骤**：
   - 选一个目标，比如「`tf.Variable` 的 Python 实现」。
   - 第一步判语言层：这是 Python 接口 → 进 `tensorflow/python/`。
   - 第二步按文件名找：在 `tensorflow/python/ops/` 下找名字像「variable」的文件。
3. **需要观察的现象**：你应该能找到 `tensorflow/python/ops/variables.py`。
4. **预期结果**：打开该文件，确认它确实定义了 `Variable` 相关类（u2-l3 会精读）。
5. 如果在只读环境无法检索，可直接信任本讲给出的路径结论，并在后续讲义中验证。

#### 4.4.5 小练习与答案

**练习 1**：我想找一个名叫 `ZeroOut` 的自定义 op 示例（自定义 op 全流程），按两步法该去哪？

> **答案**：先判语言层——「示例」属于 examples 区 → 进 `tensorflow/examples/`；再按文件名找 → `tensorflow/examples/adding_an_op/` 下有 `zero_out_op_kernel_1.cc`、`zero_out_op_1.py` 等（u9-l1 会精读）。

**练习 2**：为什么说「文件名往往就是线索」？

> **答案**：因为 TensorFlow 的目录与文件命名高度规律，同一个概念在 Python 层和 C++ 层常用相同词根（如 `math_ops`、`array_ops`），看到功能名基本就能猜到对应文件名。

## 5. 综合实践

把本讲四个模块串起来，完成一张「TensorFlow 仓库布局思维导图」：

1. **实践目标**：产出一张你自己画的顶层目录思维导图，作为后续阅读的导航地图。
2. **操作步骤**：
   - 在仓库根目录执行 `ls -1`，再执行 `ls -1 tensorflow/`。
   - 画一棵树，根节点是「tensorflow/tensorflow 仓库」。
   - 第二层画三大区域：**源码区 `tensorflow/`**、**依赖区 `third_party/`**、**工具与 CI 区（`tools/`、`ci/`、`WORKSPACE`、`MODULE.bazel`、`configure.py`）**。
   - 在「源码区」下展开语言层子节点：`core`(C++ 核心)、`python`(Python API)、`c`(C API)、`cc`(C++ 客户端)、`compiler`(MLIR/XLA)、`lite`(移动端)。
   - 在每个节点旁标注「语言层」和「一句话职责」（可直接抄自本讲 4.2.1 表格）。
   - 最后用两个醒目标记标出：**Python API 位于 `tensorflow/python/`**，**C++ 核心位于 `tensorflow/core/`**。
3. **需要观察的现象**：画完后，你会直观看到「自底向上」的调用栈方向（python → c → core，以及 compiler/lite 两条支线）。
4. **预期结果**：拿到任意一个功能需求，你能顺着这棵树迅速判断它落在哪个目录。
5. 建议把这张图保存下来，后续每篇讲义的「本讲源码地图」都可以在这棵树上定位。

## 6. 本讲小结

- 仓库顶层可分为**三大区域**：源码区 `tensorflow/`、外部依赖区 `third_party/`、工具与基础设施区（`tools/`、`ci/`、根目录构建脚本）。
- `tensorflow/` 内部按**语言层**细分：`core`(C++ 核心)、`python`(Python API)、`c`(C API)、`cc`(C++ 客户端)、`compiler`(MLIR/XLA)、`lite`(移动端)，外加 `examples/`、`docs_src/` 等。
- **Python 与 C++ 是稳定 API**（README 明确），对应 `tensorflow/python/` 与 `tensorflow/core/`（尤其是 `core/public/`）；其他语言绑定不保证向后兼容。
- 依赖管理**双轨并存**：传统 `WORKSPACE`（`workspace(name="org_tensorflow")`）与新式 `MODULE.bazel`（`module(name="tensorflow", repo_name="org_tensorflow")`，仍实验性）；二者「点名」，`third_party/`「出力」（下载/补丁/BUILD 模板）。
- 定位功能的**两步法**：先判语言层选目录，再按文件名（Glob）或内容（Grep，如 `REGISTER_OP`、`@tf_export`）精确定位。

## 7. 下一步学习建议

本讲建立了「空间感」——知道了每个目录是什么。接下来建议：

- **u1-l3 构建系统 Bazel 与 configure 配置**：深入 `configure.py`、`.bazelrc`、`WORKSPACE`/`MODULE.bazel` 的构建细节，把本讲提到的「点名 + 出力」机制讲透。
- **u1-l4 pip 包打包与 Python 入口 `import tensorflow`**：精读 `tensorflow/__init__.py` 与 `tensorflow/tools/pip_package/`，理解 Python 与 C++ 如何拼装成 `import tensorflow as tf`。
- 在进入单元 2（张量与基本概念）之前，**强烈建议**先完成本讲「综合实践」的思维导图，它会成为整本手册的导航底图。
