# 构建系统 Bazel 与 configure 配置

## 1. 本讲目标

上一讲（u1-l2）我们建立了仓库的「空间感」——知道了 `tensorflow/`、`third_party/`、`ci/` 这些目录各自干什么，也初步看到 `WORKSPACE` 与 `MODULE.bazel` 负责「点名」、`third_party/` 负责「出力」。但当时我们刻意回避了一个问题：

> **这些「点名」「出力」最终是谁在执行？我从源码到能 `import tensorflow`，中间到底发生了什么？**

本讲就来回答这个问题。TensorFlow 的体量（数百万行 C++/Python，跨 CPU/GPU/TPU/移动端）决定了它不可能用 `gcc main.c` 这种简单方式构建——它需要一个工业级的构建系统。TensorFlow 选择的是 **Bazel**。本讲聚焦构建的「入口与配置层」：Bazel 是什么、`.bazelrc` 如何配置、`./configure` 脚本如何探测你的环境、`WORKSPACE`/`MODULE.bazel` 如何组织依赖。

学完本讲，你应当能够：

1. 说出 Bazel 是什么、为什么 TensorFlow 必须用它，并看懂 `.bazelrc` 中各种配置行的含义。
2. 跟着 `configure.py` 的 `main()` 流程，说清它在配置 Python / CPU / CUDA 时分别探测了哪些信息、又把这些信息写到了哪里。
3. 区分 `WORKSPACE`（传统）与 `MODULE.bazel`（Bzlmod 新式）两套依赖组织方式的写法与分工。
4. 看懂一条「从 `./configure` → 生成 `.tf_configure.bazelrc` → 被 `.bazelrc` 加载 → 影响 `bazel build`」的完整链路。

> 说明：本讲只讲「构建的配置与依赖组织」，不涉及「如何真正编译出 pip 包」。打包脚本与 `import tensorflow` 的拼装是下一讲（u1-l4）的主题。

## 2. 前置知识

- **构建系统（build system）**：把「源码 + 依赖」转成「可执行产物」的工具。你可能听过 `make`、`cmake`、`npm`。Bazel 是 Google 开源的构建系统，核心特点是**可复现（reproducible）**、**增量（incremental）**、**密封（hermetic）**——意思是只要输入和配置相同，无论在哪台机器上构建结果都一致。
- **工作区（workspace）**：Bazel 的一个「项目根」，由根目录的 `WORKSPACE`（或 `MODULE.bazel`）标识。工作区里的代码用「目标（target）」组织，每个目录放一个 `BUILD`（或 `BUILD.bazel`）文件声明目标。
- **目标（target）与 `BUILD` 文件**：一个目标可以是一个 `cc_library`（C++ 库）、`py_library`（Python 库）、`cc_binary`（可执行文件）等。`bazel build //tensorflow:libtensorflow_framework.so` 里 `//tensorflow:libtensorflow_framework.so` 就是一个目标。
- **配置（config）与 `.bazelrc`**：Bazel 的命令行选项非常多。`.bazelrc` 是「默认选项」文件，把常用选项固化下来；`--config=<名字>` 可以选中一组预先定义好的选项。
- **环境变量注入**：Bazel 出于可复现性，默认会把宿主环境「隔离」掉。要让构建动作能看到某个环境变量（比如 `PYTHON_BIN_PATH`），需要显式用 `--action_env`（构建动作可见）或 `--repo_env`（拉取外部仓库的规则可见）声明。这一点是理解 `configure.py` 为什么要把探测结果「写进 bazelrc」的关键。
- **CUDA / cuDNN**：NVIDIA 的 GPU 计算库（CUDA）与深度学习加速库（cuDNN）。是否启用 GPU、用哪个版本、针对哪些显卡架构，都是 `./configure` 要问你的问题。

如果你对 Bazel 完全没接触过，不必焦虑——本讲只读「配置与依赖」层面的文件，不要求你亲手跑 `bazel build`。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 路径 | 作用 |
| --- | --- |
| `configure` | 一行 bash 包装脚本，负责定位 Python 并调用 `configure.py`。 |
| `configure.py` | **核心**：用交互式问答探测环境（Python/CPU/CUDA/编译器/Android…），把结果写成 `.tf_configure.bazelrc`。 |
| `.bazelrc` | Bazel 的总配置文件：定义所有 `--config=<名字>` 预设、平台/编译器选项，并 `try-import` 加载 `configure` 生成的文件。 |
| `WORKSPACE` | 传统 Bazel 工作区声明：定义仓库名 `org_tensorflow`，级联拉取外部依赖。 |
| `MODULE.bazel` | 新式 Bzlmod 依赖声明：用 `bazel_dep` / `use_extension` 集中管理模块（仍实验性）。 |
| `.bazelversion` | 指定本仓库要求的 Bazel 版本（`7.7.0`）。 |

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：先建立「Bazel + `.bazelrc`」的总图，再精读 `configure.py` 如何探测环境，最后分别看 `WORKSPACE` 与 `MODULE.bazel` 如何组织依赖。

### 4.1 Bazel 与 .bazelrc：构建的总配置

#### 4.1.1 概念说明

为什么 TensorFlow 必须用一个像 Bazel 这样的构建系统？因为它的代码同时包含：

- 海量 C++ 源文件（核心运行时、kernel）；
- 海量 Python 源文件（API 层）；
- 数十个第三方库（protobuf、abseil、grpc、eigen……）；
- 跨平台目标（Linux / macOS / Windows / Android / iOS / 嵌入式）；
- 跨硬件后端（CPU / CUDA GPU / ROCm GPU / TPU）。

这种规模下，`make` 这类工具无法胜任「可复现 + 增量 + 跨平台 + 跨语言」。Bazel 通过**显式声明依赖图**（`BUILD` 文件里写清楚谁依赖谁）实现精确的增量编译，通过**密封工具链**（hermetic toolchain，连编译器版本都由仓库指定、而非用宿主机的）实现可复现。

`.bazelrc` 则是 Bazel 在这个仓库里的「默认行为说明书」：它告诉 Bazel，这个项目默认开哪些优化、各平台怎么编译、有哪些命名的「配置档」可以一键切换（`--config=cuda`、`--config=mkl` 等）。

`.bazelversion` 文件很简单，只有一行，但它很关键——它规定了这个仓库期望的 Bazel 版本：

[.bazelversion:1](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/.bazelversion#L1) — 内容是 `7.7.0`。配合 `bazelisk`（一个 Bazel 版本管理器，会自动读取 `.bazelversion` 下载对应版本），可以保证所有人用同一个 Bazel 版本构建，避免「我的机器能编、你的不能」。

> 上一讲（u1-l2）提过 `retrieve_bazel_version` 会优先找 `bazel`，找不到再找 `bazelisk`——后者正是靠 `.bazelversion` 工作的。

#### 4.1.2 核心流程

一次 `bazel build` 读取配置的大致顺序：

```
1. bazel 启动，读取 .bazelversion 决定用哪个 Bazel（通常由 bazelisk 完成）
        │
2. 读取根目录 .bazelrc：
     - 顶部 common --...        ← 无条件、对所有命令生效的默认选项
     - common:NAME / build:NAME ← 命名的「配置档」，需用 --config=NAME 触发
        │
3. 执行到 .bazelrc 末尾的 try-import：
     - try-import .tf_configure.bazelrc  ← ./configure 生成的本机专属选项
     - try-import .bazelrc.user          ← 你自己的临时覆盖（可选）
        │
4. 解析 WORKSPACE / MODULE.bazel，拉取外部依赖
        │
5. 依据 BUILD 文件里的目标 + 上述配置，开始真正的编译/链接
```

注意第 3 步的「分层」：`.bazelrc` 是**仓库共享**的（提交进 git），而 `.tf_configure.bazelrc` 是**本机专属**的（由 `./configure` 生成、通常被 git 忽略）。这样既保证了团队配置一致，又允许每个人按自己机器的情况（是否装了 CUDA、Python 在哪）做个性化。`try-import` 的 `try-` 前缀表示「文件不存在也不报错」。

#### 4.1.3 源码精读

打开 `.bazelrc`，开头一大段注释是「目录」，列出了所有可用的 `--config=<名字>`：

[.bazelrc:1-67](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/.bazelrc#L1-L67) — 这段注释说明了有哪些配置档，例如 `cuda`（GPU 支持）、`rocm`（AMD GPU）、`monolithic`（单体静态构建）、`v2`（TF2 API）、`nogcp`（禁用 GCS）。**以后看到文档里写 `--config=cuda`，就来这里查它具体展开成什么。**

接下来是「无条件默认选项」区（注释里写着 `Default build options. These are applied first and unconditionally.`）：

[.bazelrc:81-92](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/.bazelrc#L81-L92) — 这几行 `common --...` 对所有 bazel 命令生效。其中：
- `common --define framework_shared_object=true`（第 86 行）开启「模块化 op 注册」，让 TF 把部分 kernel 编译成独立 `.so` 动态加载；
- `common -c opt`（第 92 行）默认以**优化模式**编译（而不是 debug）。

`.bazelrc` 里还有一个关键决定——默认**关闭** Bzlmod、启用传统 `WORKSPACE`：

[.bazelrc:163-165](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/.bazelrc#L163-L165) — `common --noenable_bzlmod` 和 `common --enable_workspace`。也就是说，尽管仓库里有一个 `MODULE.bazel`（实验性 Bzlmod 支持），**当前默认构建路径仍走 `WORKSPACE`**。如果你想试 Bzlmod，需要显式 `--config=bzlmod`：

[.bazelrc:182-183](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/.bazelrc#L182-L183) — `common:bzlmod --enable_bzlmod --noenable_workspace`，正好和默认反过来。

「配置档」的写法是 `common:名字`（或 `build:名字` / `test:名字`），用 `--config=名字` 触发。以 CUDA 为例：

[.bazelrc:299-304](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/.bazelrc#L299-L304) — `--config=cuda` 会展开成：设置 `TF_NEED_CUDA=1`、启用 `@local_config_cuda`、再 `--config=cuda_version`（决定具体 CUDA 版本）。注意这里用的是 `--repo_env TF_NEED_CUDA=1`——**`repo_env`** 表示这个变量在「拉取/配置外部仓库规则」阶段可见（`local_config_cuda` 这个仓库规则需要它来判断要不要配 CUDA）。

`cuda_version` 进而指向一个具体的版本档：

[.bazelrc:284-289](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/.bazelrc#L284-L289) — `--config=cuda12_version` 把 CUDA 锁定为 `12.5.1`、cuDNN `9.3.0`、NCCL `2.29.7`，并指定一组目标显卡架构 `sm_60,sm_70,sm_80,sm_89,compute_90`。这种「密封（hermetic）版本」意味着 Bazel 会自己去下载指定版本的 CUDA 组件，而不依赖你机器上装了什么——这正是「可复现构建」的体现。

最后，文件末尾两行把本机配置接进来：

[.bazelrc:744-747](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/.bazelrc#L744-L747) — `try-import %workspace%/.tf_configure.bazelrc` 加载 `./configure` 的产物；`try-import %workspace%/.bazelrc.user` 留给你放私人覆盖。`%workspace%` 是 Bazel 的占位符，指工作区根目录。

#### 4.1.4 代码实践

1. **实践目标**：学会「读 `.bazelrc` 解析一个 `--config`」，建立「配置档 = 一组选项的别名」的直觉。
2. **操作步骤**：
   - 打开 `.bazelrc`，找到 `common:monolithic` 这一段（约 267–273 行）。
   - 读它展开成了哪些 `--define ...`，对照本讲 4.1.3 中 `framework_shared_object` 的解释。
   - 再找到 `common:v2`（约 571–572 行），看它设了什么。
3. **需要观察的现象**：每个「配置档」本质就是「`--config=名字` 触发若干个 `--define/--copt/--action_env`」。
4. **预期结果**：你能用自己的话解释「`bazel build --config=monolithic //tensorflow:...` 比不带这个 config 多做了什么」。（提示：它把 `framework_shared_object` 设回 `false`，退化为单体静态构建，Python 加载时需要 `RTLD_GLOBAL`。）
5. 待本地验证：如果你装了 bazelisk，可以运行 `bazelisk --version` 确认它按 `.bazelversion` 选了 `7.7.0`。

#### 4.1.5 小练习与答案

**练习 1**：`.bazelrc` 里 `--action_env` 和 `--repo_env` 有什么区别？

> **答案**：`--action_env` 让环境变量在**构建/测试动作**（编译、链接、跑测试）中可见；`--repo_env` 让环境变量在**仓库规则**（拉取/配置外部依赖的 `.bzl` 规则，如 `cuda_configure`）中可见。`configure.py` 会根据变量用途选择写哪一种。

**练习 2**：为什么 `.tf_configure.bazelrc` 用 `try-import` 而不是 `import`？

> **答案**：因为还没运行过 `./configure` 时这个文件不存在。`try-import` 在文件缺失时静默跳过，不报错；普通 `import` 会导致 bazel 启动失败。

### 4.2 configure 脚本与 configure.py：环境探测

#### 4.2.1 概念说明

`.bazelrc` 解决了「团队共享的默认配置」，但每个开发者的机器不一样：Python 装在哪、要不要 GPU、CUDA 是哪个版本、显卡是什么架构……这些是**本机专属**的，不能写进 git。`./configure` 就是用来采集这些信息、生成 `.tf_configure.bazelrc` 的脚本。

它分成两层：

- `configure`：一个极简的 bash 脚本，只负责「找到 Python 解释器，然后用它去跑 `configure.py`」。
- `configure.py`：真正的逻辑。它用一系列**交互式问答**（也支持用环境变量预填答案，方便 CI 自动化）探测环境，然后把结果一行行写进 `.tf_configure.bazelrc`。

为什么要用 Python 写探测逻辑？因为探测需要跨平台（Linux/macOS/Windows）、要解析版本号、要校验路径是否存在——这些用 bash 写会非常痛苦，Python 更合适。

#### 4.2.2 核心流程

`./configure` 的端到端流程：

```
用户在仓库根目录执行 ./configure
        │
        ▼
bash 脚本 configure：定位 python3 → 执行 configure.py
        │
        ▼
configure.py main()：
  (1) retrieve_bazel_version()   检查 bazel/bazelisk 是否存在、版本
  (2) reset_tf_configure_bazelrc() 清空旧的 .tf_configure.bazelrc
  (3) setup_python()             探测 Python 路径与库路径
  (4) TF_NEED_ROCM / TF_NEED_CUDA 询问是否启用 GPU（二者互斥）
        └─ 若 CUDA：问 CUDA/cuDNN 版本、compute capabilities、编译器
  (5) set_cc_opt_flags()         确定 CPU 优化编译选项
  (6) set_system_libs_flag()     是否用系统库替代部分第三方库
  (7) system_specific_test_config()  测试过滤标签
  (8) configure_ios()            （仅 macOS）iOS 构建
  (9) 打印所有可用的 --config 预设（mkl/monolithic/numa…）
        │
        ▼
每一步都通过 write_to_bazelrc() 把结论追加到 .tf_configure.bazelrc
        │
        ▼
.bazelrc 末尾的 try-import 在下次 bazel build 时加载它
```

关键设计：`configure.py` 不是「直接设置环境变量就完事」，而是把结论**落盘**到 `.tf_configure.bazelrc`。这样后续任何一次 `bazel build` 都能复现同样的配置，而不必每次都重新探测——这正是「密封/可复现」精神的体现。

#### 4.2.3 源码精读

先看最外层的 bash 包装。它短到可以整段读：

[configure:6-12](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/configure#L6-L12) — 如果没设 `PYTHON_BIN_PATH`，就用 `which python3`（退而求其次 `python`）找到一个 Python，然后 `"${CONFIGURE_DIR}/configure.py" "$@"`。注意 `"$@"` 把命令行参数透传给 `configure.py`。所以本讲实践任务里的 `./configure --help`，本质显示的是 `configure.py` 里 argparse 的帮助。

`configure.py` 一开篇就声明了它的产物文件名：

[configure.py:15](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/configure.py#L15) — docstring 写明 `configure script to get build parameters from user`。

[configure.py:34](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/configure.py#L34) — `_TF_BAZELRC_FILENAME = '.tf_configure.bazelrc'`。这就是 `./configure` 最终生成的文件名，和 `.bazelrc` 末尾 `try-import` 的目标完全对上。

写入 `.tf_configure.bazelrc` 靠三个小工具函数，它们是理解整篇文章如何「落盘」的钥匙：

[configure.py:113-119](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/configure.py#L113-L119) — `write_to_bazelrc(line)` 原样追加一行；`write_action_env_to_bazelrc(var, val)` 写成 `build --action_env VAR="VAL"`（注意前缀是 `build`，所以只对 `bazel build` 生效，对应 4.1 讲的 `--action_env`）。

[configure.py:122-125](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/configure.py#L122-L125) — `write_repo_env_to_bazelrc(config_name, var, val)` 写成 `build:CONFIG --repo_env VAR="VAL"`，即绑定到某个命名配置档下的 `--repo_env`（CUDA 相关变量都走这个，挂在 `cuda` 配置档下）。

再看 `main()` 如何串起整个流程：

[configure.py:1189-1203](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/configure.py#L1189-L1203) — `main()` 先解析唯一的命令行参数 `--workspace`（默认就是脚本所在目录），据此定位 `.tf_configure.bazelrc` 的绝对路径，然后把 `os.environ` 拷一份作为 `environ_cp`（后续所有探测都读写这份副本）。`--workspace` 就是 `./configure --help` 会显示的那一项。

[configure.py:1210-1220](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/configure.py#L1210-L1220) — 先 `retrieve_bazel_version()` 确认 bazel 装了，再 `reset_tf_configure_bazelrc()` 清空旧配置文件，最后 `setup_python(environ_cp)` 开始第一项实质探测——Python。

`retrieve_bazel_version` 的查找顺序值得记一下：

[configure.py:423-428](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/configure.py#L423-L428) — 先 `shutil.which('bazel')`，找不到再找 `bazelisk`，两个都没有就直接 `sys.exit(1)` 报错退出。这印证了 4.1.1 里「bazelisk 靠 `.bazelversion` 工作」的说法。

#### 4.2.4 代码实践

1. **实践目标**：把 `./configure --help` 跑通（或读源码替代），列出它探测的环境信息，并对照源码确认每项被写进了 bazelrc 的哪一行。
2. **操作步骤**：
   - 在仓库根目录运行 `./configure --help`（它会调用 `configure.py --help`）。
   - 你应该只看到一个 `--workspace` 选项——这说明 `./configure` 的真正「配置项」不是靠命令行参数，而是靠**交互问答或环境变量**。
   - 改为读 `configure.py` 的 `main()`（1189 行起），按顺序列出它调用的探测函数，每个函数对应一项被探测的环境信息。
3. **需要观察的现象**：你能至少列出 3 项被探测的信息，例如：① Python 解释器路径与库路径（`setup_python`）；② 是否启用 CUDA / ROCm（`TF_NEED_CUDA` / `TF_NEED_ROCM`）；③ CPU 编译优化选项（`set_cc_opt_flags`）；④ CUDA compute capabilities（显卡架构）；⑤ clang/gcc 编译器路径。
4. **预期结果**：写一张「探测函数 → 探测对象 → 写入 bazelrc 的形式」的小表。
5. 待本地验证：在只读环境无法真正交互式跑 `./configure` 时，上述结论全部可由阅读 `configure.py` 得到，无需实际执行。

#### 4.2.5 小练习与答案

**练习 1**：`./configure` 怎么做到既能交互问答、又能被 CI 自动化？

> **答案**：`get_from_env_or_user_or_default`（configure.py:515-539）的逻辑是「先看环境变量有没有预设，有就直接用，没有才去问用户」。所以 CI 只要把答案写进环境变量（如 `TF_NEED_CUDA=1`），脚本就不会卡在问答上。

**练习 2**：为什么 `configure.py` 要把结果写进文件，而不是只设环境变量？

> **答案**：因为后续的 `bazel build` 是**另起一个进程**，且 Bazel 默认会隔离宿主环境。只有把结论落盘到 `.tf_configure.bazelrc`、再由 `.bazelrc` 的 `try-import` 加载，才能保证每次构建都用同一套配置，实现可复现。

### 4.3 configure.py 如何生成构建选项：Python / CPU / CUDA

#### 4.3.1 概念说明

上一模块讲了 `configure.py` 的骨架，本模块深入它最关键的三个探测领域：**Python**、**CPU 优化**、**CUDA**。这是本讲学习目标里「知道 configure.py 在配置 CUDA/CPU 时的职责」的核心。

- **Python 探测**：TF 的 Python API 要链接到具体的 Python 解释器和它的库路径（`PYTHON_BIN_PATH` / `PYTHON_LIB_PATH`），这样编译出来的 C++ 扩展才能被正确的 Python 导入。
- **CPU 优化**：不同 CPU 支持不同指令集（AVX 等）。`--config=opt` 会带上一组 `-copt` 编译选项，`configure.py` 负责确定这组选项的默认值。
- **CUDA 探测**：这是最复杂的一块。要决定用哪个 CUDA/cuDNN 版本、为哪些显卡架构（compute capability）生成代码、用 clang 还是 nvcc 当 CUDA 编译器、host 编译器（gcc/clang）在哪。现代 TF 用「密封 CUDA」——版本和组件都由 Bazel 下载，`configure.py` 主要记录用户选择，再以 `--repo_env` 形式交给 `local_config_cuda` 仓库规则。

#### 4.3.2 核心流程

以 CUDA 为例（最复杂路径），`configure.py` 的处理逻辑：

```
main() 中：
  get_var(TF_NEED_CUDA, default=False)      问：要不要 CUDA？
        │ 否 → 走 CPU 路径：choose_compiler() 选 clang/gcc
        │ 是
        ▼
  set_hermetic_cuda_version()               问：用哪个 CUDA 版本？（密封）
  set_hermetic_cudnn_version()              问：用哪个 cuDNN 版本？
  set_hermetic_cuda_compute_capabilities()  问：为哪些显卡架构生成代码？
  set_cuda_local_path(...)                  （可选）本地 CUDA/cuDNN/NCCL 路径
  set_tf_cuda_clang()                       问：CUDA 用 clang 还是 nvcc 编译？
        │
        ▼
  set_other_cuda_vars() → write_to_bazelrc('build --config=cuda' 或 'cuda_clang')
```

每个 `set_hermetic_*` 都通过 `write_repo_env_to_bazelrc('cuda', VAR, VAL)` 把选择写进 `build:cuda --repo_env ...`，最终和 `.bazelrc` 里 `--config=cuda → --config=cuda_version` 链条对接，确定整套密封 CUDA 版本。

#### 4.3.3 源码精读

**Python 探测**：`setup_python` 是第一个被调用的实质探测函数。它先确认 Python 可执行文件存在且可执行，再推导出 Python 库路径，最后把两个路径写进 bazelrc 并生成一个 shell 脚本：

[configure.py:234-236](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/configure.py#L234-L236) — 把 `PYTHON_BIN_PATH` 和 `PYTHON_LIB_PATH` 以 `--action_env` 形式写进 bazelrc，外加一行 `build --python_path=...`。这样 Bazel 在构建 Python 相关目标时就知道用哪个 Python。

[configure.py:247-250](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/configure.py#L247-L250) — 额外写一个 `tools/python_bin_path.sh` 文件，内容是 `export PYTHON_BIN_PATH="..."`。注意：**这个文件在你 clone 仓库后并不存在**（你可以 `ls tools/python_bin_path.sh` 验证），它是 `./configure` 运行时才生成的——后续打包脚本（u1-l4 会讲）会 source 它来获知 Python 路径。

**CPU 优化**：`set_cc_opt_flags` 决定 `--config=opt` 时附加哪些编译选项，且因平台而异：

[configure.py:448-475](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/configure.py#L448-L475) — 在 ppc64le 上用 `-mcpu=native`，Windows 上用 `/arch:AVX`，其它平台（含普通 x86 Linux）默认只给 `-Wno-sign-compare`（**故意不再用 `-march=native`**，注释里解释了原因：`-march=native` 会生成过于现代的指令，导致在别的机器上跑不起来；追求性能的用户可自行覆盖）。最终每个选项写成 `build:opt --copt=...` 和 `--host_copt=...`。

**CUDA 探测**：先在 `main()` 里决定要不要 CUDA：

[configure.py:1281-1282](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/configure.py#L1281-L1282) — 非 Windows 时，`get_var(environ_cp, 'TF_NEED_CUDA', 'CUDA', False)` 问「是否启用 CUDA」，默认 `False`（注意默认是「否」，所以官方 CPU 包不需要你手动选 N）。

若启用，连续调用一组密封版本设置函数：

[configure.py:1283-1289](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/configure.py#L1283-L1289) — 依次设置密封 CUDA 版本、cuDNN 版本、compute capabilities、以及可选的本地 CUDA/cuDNN/NCCL 路径。以 `set_hermetic_cuda_version` 为例：

[configure.py:944-957](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/configure.py#L944-L957) — 询问「想用哪个密封 CUDA 版本，留空则用默认」，若用户指定了，就用 `write_repo_env_to_bazelrc('cuda', 'HERMETIC_CUDA_VERSION', ...)` 写成 `build:cuda --repo_env HERMETIC_CUDA_VERSION="..."`。这正是和 `.bazelrc` 里 `cuda_version`/`cuda12_version` 那套密封版本对接的入口。

最后，`set_other_cuda_vars` 把「用哪个 CUDA 配置档」的决定也写下来：

[configure.py:1065-1071](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/configure.py#L1065-L1071) — 若选了 `TF_CUDA_CLANG` 则写 `build --config=cuda_clang`，否则写 `build --config=cuda`。这行会无条件进入 `.tf_configure.bazelrc`，于是之后每次 `bazel build` 都自动带上 `--config=cuda`。

**配置档清单**：`main()` 末尾会打印所有可用的预设配置档，方便用户知道还能加哪些 `--config`：

[configure.py:1350-1366](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/configure.py#L1350-L1366) — 调用 `config_info_line` 逐条打印：`--config=mkl`（MKL 支持）、`--config=mkl_aarch64`（ARM 上的 oneDNN+ACL）、`--config=monolithic`（单体构建）、`--config=numa`、`--config=dynamic_kernels`（动态加载 kernel，实验性）、`--config=v1`（用 TF1 API）。这些名字和 `.bazelrc` 里的 `common:NAME` 一一对应——`configure.py` 负责告诉你「有哪些开关」，`.bazelrc` 负责「每个开关具体干什么」。

#### 4.3.4 代码实践

1. **实践目标**：用「源码阅读」方式追踪一次「用户选了 CUDA」后，`configure.py` 往 bazelrc 里写了哪些行。
2. **操作步骤**：
   - 假设用户在「是否启用 CUDA」回答了 `y`，且对版本问题都回车（用默认）。
   - 在 `configure.py` 里定位 1283–1307 行这一段，逐个函数看它写的 `--repo_env` / `--config`。
   - 把结果整理成一张表：`函数名 → 写入的内容 → 写入形式（action_env / repo_env / config）`。
3. **需要观察的现象**：CUDA 相关变量几乎都走 `--repo_env` 且挂在 `cuda` 配置档下；只有最终的 `--config=cuda` 是无条件 `build` 行。
4. **预期结果**：你能解释「为什么 CUDA 版本要用 `repo_env` 而不是 `action_env`」——因为版本信息要在配置 `local_config_cuda` 外部仓库规则时被读到，属于「仓库规则阶段」的需求。
5. 待本地验证：若你有 GPU 机器，可真实跑一次 `./configure` 观察它打印的问题与最终生成的 `.tf_configure.bazelrc` 内容是否吻合。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `set_cc_opt_flags` 在普通 x86 Linux 上默认**不**用 `-march=native`？

> **答案**：因为 `-march=native` 会让编译器针对当前 CPU 生成可能很新的指令，导致编出来的二进制在别的（较旧的）CPU 上无法运行，破坏可移植性。注释里特意指向了 issue #45744。追求性能的用户可以自己加 `-march=native`。

**练习 2**：`set_hermetic_cuda_version` 把选择写成 `build:cuda --repo_env HERMETIC_CUDA_VERSION=...`，这里的 `cuda` 是什么？

> **答案**：它是一个**配置档名字**（对应 `.bazelrc` 里的 `common:cuda`）。意思是「这个 `--repo_env` 只在用户带了 `--config=cuda` 时才生效」，把 CUDA 版本变量精确地限定在 CUDA 构建场景里，不影响纯 CPU 构建。

### 4.4 WORKSPACE 与 MODULE.bazel：外部依赖组织

#### 4.4.1 概念说明

u1-l2 已经初步介绍过「`WORKSPACE`/`MODULE.bazel` 点名、`third_party/` 出力」的分工。本模块精读这两个文件的具体写法。

TensorFlow 当前**默认走 `WORKSPACE`**（见 4.1.3 的 `--noenable_bzlmod`），但仓库里同时维护着一份实验性的 `MODULE.bazel`（Bzlmod 方式）。两者的核心区别：

| 维度 | `WORKSPACE`（传统） | `MODULE.bazel`（Bzlmod） |
| --- | --- | --- |
| 声明单元 | 一个个 `http_archive` / 自定义宏拉取的「外部仓库」 | 一个个 `bazel_dep` 声明的「模块」 |
| 传递依赖 | 手动：A 依赖 B，你得自己把 B 也写进来 | 自动：模块自己声明依赖，Bazel 解析传递闭包 |
| 版本来源 | URL + sha256（或 `third_party/*.bzl` 里的字典） | BCR（Bazel Central Registry）版本号 |
| 当前状态 | **默认启用** | 实验性，需 `--config=bzlmod` |

无论哪种方式，最终都会产出一批「外部仓库」（如 `@com_google_protobuf`、`@local_config_cuda`），在 `BUILD` 文件里以 `@<名字>` 引用。

#### 4.4.2 核心流程

`WORKSPACE` 的组织是「分层级联」：

```
WORKSPACE
  ├─ workspace(name="org_tensorflow")          定名字
  ├─ load third_party:repo.bzl 的 tf_http_archive 等宏
  ├─ tf_workspace3()  ─┐
  ├─ tf_workspace2()  ─┤  把庞大的依赖列表拆到
  ├─ tf_workspace1()  ─┤  tensorflow/workspace0~3.bzl
  ├─ tf_workspace0()  ─┘
  ├─ 密封 Python 初始化（python_init_repositories + requirements_lock）
  ├─ 密封 C++ 工具链（cc_toolchain_deps + register_toolchains）
  └─ cuda_configure / nccl_configure → 生成 @local_config_cuda 等
```

之所以拆成 `workspace0~3.bzl` 四个文件，是因为 `load()` 语句必须位于 `.bzl`/`WORKSPACE` 顶部，而每拉一个新仓库后又可能需要 `load` 它里面的宏——Bazel 限制同文件里 `load` 必须在前，于是 TF 用「级联调用」绕过这个限制（`WORKSPACE` 顶部注释里有解释）。

`MODULE.bazel` 则扁平得多：顶部一长串 `bazel_dep(...)` 直接点名，复杂的第三方仓库用「模块扩展（module extension）」`use_extension`/`use_repo` 延迟拉取。

#### 4.4.3 源码精读

**WORKSPACE** 开头声明工作区名（和 u1-l2 看到的一致）：

[WORKSPACE:18](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/WORKSPACE#L18) — `workspace(name = "org_tensorflow")`。这个名字是整个工作区的全局标识。

接着演示了「自定义宏拉取外部仓库」的标准写法：

[WORKSPACE:22-31](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/WORKSPACE#L22-L31) — `load("//third_party:repo.bzl", "tf_http_archive", "tf_mirror_urls")` 引入 TF 自己的宏，然后用 `tf_http_archive(name="rules_shell", sha256=..., urls=tf_mirror_urls(...))` 拉取 `rules_shell`。`tf_mirror_urls` 会生成一组镜像 URL（保证某个源挂了还能下载），`sha256` 校验完整性——这两者共同保证「密封下载」。

级联调用把庞大的依赖列表分流：

[WORKSPACE:39-41](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/WORKSPACE#L39-L41) — `load("@//tensorflow:workspace3.bzl", "tf_workspace3")` 然后 `tf_workspace3()`。类似地后面还有 `tf_workspace2/1/0`（106–116 行）。你可以在 `tensorflow/` 目录下找到对应的 `workspace0.bzl`~`workspace3.bzl`，真正的依赖清单在那里。

密封 Python 的初始化展示了「按 Python 版本锁定依赖」的做法：

[WORKSPACE:76-91](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/WORKSPACE#L76-L91) — `python_init_repositories` 接收一个 `requirements` 字典，为每个 Python 版本（3.10~3.14）指定一个锁文件（如 `requirements_lock_3_11.txt`）。这就是仓库根目录那些 `requirements_lock_*.txt` 文件的用途——每个 Python 版本一套确定的 pip 依赖。

CUDA 配置最终落地为一个外部仓库 `@local_config_cuda`：

[WORKSPACE:164-168](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/WORKSPACE#L164-L168) — `load(...cuda_configure.bzl, "cuda_configure")` 然后 `cuda_configure(name = "local_config_cuda")`。这个仓库规则会读取 `configure.py` 写下的 `HERMETIC_CUDA_VERSION` 等 `repo_env`，据此下载/配置 CUDA 组件，产出的 `@local_config_cuda` 就是 `.bazelrc` 里 `--@local_config_cuda//:enable_cuda` 引用的那个仓库。**这一行把 4.2/4.3 讲的 `configure.py` 探测结果与构建真正接上了。**

**MODULE.bazel** 开篇就标明自己实验性质：

[MODULE.bazel:1-6](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/MODULE.bazel#L1-L6) — 注释 `Experimental Bzlmod support for TensorFlow`，`module(name = "tensorflow", repo_name = "org_tensorflow")`。注意 `repo_name="org_tensorflow"` 与 `WORKSPACE` 保持一致，所以无论走哪条路，下游看到的仓库名都相同。

`bazel_dep` + `single_version_override` 是 Bzlmod 打补丁的标准模式：

[MODULE.bazel:8-17](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/MODULE.bazel#L8-L17) — 先 `bazel_dep(name="abseil-cpp", version="20260526.0", repo_name="com_google_absl")` 声明依赖，再用 `single_version_override(... patches=["//third_party/absl:build_dll.patch", ...])` 给它打补丁——补丁内容仍在 `third_party/` 里，印证 u1-l2 的「点名 + 出力」。

对于 TF 自己魔改较深、不便走 BCR 的依赖（如 `rules_ml_toolchain`、`xla`），用 `archive_override` / `local_path_override` 直接指源：

[MODULE.bazel:94-98](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/MODULE.bazel#L94-L98) — `bazel_dep(name="xla")` 后用 `local_path_override(module_name="xla", path="third_party/xla")`，把 XLA 指向仓库内 `third_party/xla` 子目录（XLA 已拆成独立仓库，TF 以子模块方式引入）。

大量「按需拉取」的第三方仓库通过「模块扩展」完成：

[MODULE.bazel:100-101](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/MODULE.bazel#L100-L101) — `use_extension("@xla//third_party/extensions:tsl.bzl", "tsl_extension")` 定义一个扩展，`use_repo(tsl_extension, tsl="tsl")` 把它产出的仓库 `tsl` 引入当前模块作用域。后面 106–145 行用同样的模式一口气引入了 `eigen_archive`、`XNNPACK`、`onednn`、`stablehlo` 等几十个仓库。

Python 多版本工具链在 Bzlmod 下用扩展声明：

[MODULE.bazel:211-220](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/MODULE.bazel#L211-L220) — `python.toolchain(python_version="3.10"..."3.13")` 为每个版本注册工具链，和 `WORKSPACE` 里 `requirements_lock` 的思路对应。

#### 4.4.4 代码实践

1. **实践目标**：在 `WORKSPACE` 与 `MODULE.bazel` 中找到「同一个依赖的两种写法」，体会新旧方式的差异。
2. **操作步骤**：
   - 在 `MODULE.bazel` 里搜 `abseil-cpp`（第 9 行 + 10–17 行 override），看它的版本号和补丁。
   - 在 `tensorflow/workspace0.bzl`~`workspace3.bzl` 里搜 `absl`/`abseil`，找到 `WORKSPACE` 路径下对同一个库的声明（通常是一个 `tf_http_archive` 或 `third_party/absl/BUILD` 模板）。
   - 对比：Bzlmod 用「版本号 + BCR」；WORKSPACE 用「URL + sha256 + third_party 补丁」。
3. **需要观察的现象**：同一个库在两套体系里「身份标识」不同——Bzlmod 是 `name+version`，WORKSPACE 是 `urls+sha256`。
4. **预期结果**：你能解释为什么 TF 还没完全切到 Bzlmod——大量深度魔改的依赖（CUDA 工具链、XLA）在 BCR 里没有现成条目，迁移成本高，所以 `MODULE.bazel` 仍标 `Experimental`。
5. 待本地验证：若你想试 Bzlmod，可运行 `bazelisk build --config=bzlmod //tensorflow/core:libtensorflow_framework.so`（首次会下载较多内容），但本讲不要求实际执行。

#### 4.4.5 小练习与答案

**练习 1**：`WORKSPACE` 里为什么要把依赖拆到 `tf_workspace0~3.bzl` 四个文件？

> **答案**：因为 Bazel 要求 `load()` 语句必须在文件顶部，而「拉取一个外部仓库后 `load` 它里面的宏」这种需求无法在同一文件里顺序完成。TF 用多个 `.bzl` 文件级联（`workspace3` → `workspace2` → … → `workspace0`），每个文件顶部 `load` 它需要的东西，从而绕开这个限制（`WORKSPACE` 第 33–38 行注释有说明）。

**练习 2**：`cuda_configure(name="local_config_cuda")` 和 `configure.py` 有什么关系？

> **答案**：`cuda_configure` 是一个仓库规则，它运行时会读取 `configure.py` 通过 `--repo_env` 写下的 `HERMETIC_CUDA_VERSION`、`HERMETIC_CUDA_COMPUTE_CAPABILITIES` 等变量，据此下载并配置 CUDA 组件，产出 `@local_config_cuda` 仓库。换句话说，`configure.py` 负责「采集用户意图」，`cuda_configure` 负责「按意图把 CUDA 弄到手」。

## 5. 综合实践

把本讲四个模块串起来，完成一次「配置链路全景追踪」：

1. **实践目标**：画出从「用户执行 `./configure`」到「`bazel build` 用上 CUDA」的完整数据流，并用源码行号佐证每一步。
2. **操作步骤**：
   - **第一步（入口）**：指出 `configure` 脚本如何转交给 `configure.py`（引用 `configure:6-12`）。
   - **第二步（探测）**：在 `configure.py` 的 `main()` 里标记出「询问 CUDA」（1281–1282 行）与「记录密封版本」（944–957 行）两步，说明它们调用 `write_repo_env_to_bazelrc` 把结果写进 `.tf_configure.bazelrc`（122–125 行）。
   - **第三步（落地）**：指出 `set_other_cuda_vars` 写下 `build --config=cuda`（1065–1071 行）。
   - **第四步（加载）**：在 `.bazelrc` 里找到 `try-import .tf_configure.bazelrc`（744 行），以及 `--config=cuda` 展开成什么（299–304 行 → cuda_version → cuda12_version，284–289 行）。
   - **第五步（消费）**：在 `WORKSPACE` 里找到 `cuda_configure(name="local_config_cuda")`（164–168 行），说明它读取上一步的 `repo_env` 完成密封 CUDA 下载。
3. **需要观察的现象**：一条「用户意图」如何穿越 bash → Python → bazelrc 文件 → bazel 配置档 → 仓库规则，最终变成构建行为。
4. **预期结果**：你能对着这张链路图，向别人解释「为什么改了显卡架构后必须重新跑 `./configure`」——因为旧的 `.tf_configure.bazelrc` 里记的还是旧 compute capabilities。
5. 把这张图存档；它也是后续 u1-l4（打包）理解「打包前要先 configure」的基础。

## 6. 本讲小结

- TensorFlow 用 **Bazel** 构建，根目录 `.bazelversion` 指定版本（`7.7.0`，常配合 `bazelisk` 使用），保证可复现、密封、增量。
- **`.bazelrc`** 是构建总配置：顶部 `common --...` 是无条件默认；`common:NAME`/`build:NAME` 是命名「配置档」，用 `--config=NAME` 触发；末尾 `try-import .tf_configure.bazelrc` 和 `.bazelrc.user` 分别加载本机配置与个人覆盖。当前默认 `--noenable_bzlmod`（走 WORKSPACE）。
- **`./configure`** = bash 包装（`configure`）+ 探测逻辑（`configure.py`）。它用「环境变量优先、否则交互问答」的方式探测 Python/CPU/CUDA/编译器/Android 等，把结论通过 `write_to_bazelrc`/`write_action_env_to_bazelrc`/`write_repo_env_to_bazelrc` **落盘**到 `.tf_configure.bazelrc`。
- **`configure.py` 在 CUDA/CPU 上的职责**：CPU 用 `set_cc_opt_flags` 定 `--config=opt` 的 `-copt`（默认不碰 `-march=native`）；CUDA 用一组 `set_hermetic_*` 把版本/显卡架构写成 `build:cuda --repo_env ...`，最后写一行 `build --config=cuda`。
- **`WORKSPACE`**（默认）通过 `tf_http_archive`+`tf_mirror_urls`（URL+sha256，密封下载）和 `tf_workspace0~3.bzl` 级联管理依赖，`cuda_configure`/`python_init_repositories` 把探测结果消费成 `@local_config_cuda` 等外部仓库。
- **`MODULE.bazel`**（实验性 Bzlmod）用 `bazel_dep`+版本号+BCR、`use_extension`/`use_repo` 管理依赖，补丁仍放 `third_party/`；因深度魔改依赖多，尚未默认启用。

## 7. 下一步学习建议

- **u1-l4 pip 包打包与 Python 入口 `import tensorflow`**：精读 `tensorflow/tools/pip_package/`（`build_pip_package.py`、`setup.py.tpl`）与 `tensorflow/__init__.py`，看看 configure 之后，源码如何被打包成 pip 包、以及 `import tensorflow as tf` 如何把 Python 与 C++ 拼起来（本讲提到的 `tools/python_bin_path.sh` 会在那里被用到）。
- **官方「从源码构建」文档**：README 里指向 `https://www.tensorflow.org/install/source`，可作为本讲的实际操作手册对照阅读。
- 进入单元 2（张量与基本概念）前，建议先完成本讲「综合实践」的链路图——它帮你把「构建配置层」彻底理清，之后阅读源码时遇到 `--config=...` 或 `@local_config_...` 都不会再迷惑。
