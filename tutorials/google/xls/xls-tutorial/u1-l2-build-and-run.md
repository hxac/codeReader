# 从源码构建与运行 XLS

## 1. 本讲目标

读完上一讲，你已经知道 XLS 是一条「DSLX → IR → 优化 → 调度 → Verilog」的高层综合工具链。但要把这条链真正跑起来，第一步是**把 XLS 自己编译出来**。本讲带你完成这一步，学完后你应当能够：

1. 说清 XLS 为什么选择 Bazel 作为构建系统，以及它的依赖是怎么管理的。
2. 在 Ubuntu 上安装好构建 XLS 所需的系统依赖。
3. 用一条 `bazel build` 命令把某个工具（如 `interpreter_main`）从源码编译出来，并理解「DSLX-only 构建」与「含 C++ 前端的完整构建」的差异。
4. 了解 Docker 构建与 GitHub Release 二进制两种「免配置」获取方式。
5. 用 disk cache / 远程缓存加速重复构建。
6. 跑通 `interpreter_main --version`，确认产物可用，并理解 `--version` 背后的实现机制。

## 2. 前置知识

在动手前，先建立两个直觉。

**什么是构建系统？** 当一个项目只有一两个源文件时，敲一句 `g++ main.cc` 就能编译。但 XLS 有成千上万个源文件、依赖几十个第三方库（Abseil、protobuf、LLVM、Z3……），还涉及 C++、Python、Verilog 多种语言。构建系统（build system）就是替你管理「先编哪个、依赖谁、产物在哪、哪些需要重编」的工具。XLS 用的是 Google 开源的 **Bazel**。如果你用过 Make/CMake，可以把 Bazel 理解为「能保证可复现、能精确声明依赖、能跨语言」的加强版 Make。

**什么是 Bazelisk？** XLS 对 Bazel 的版本有严格要求（项目根目录的 `.bazelversion` 文件里写死了版本号）。手动安装对应版本容易出错，所以社区常用 **Bazelisk**——它是一个 Bazel 的「版本管理器」，读到 `.bazelversion` 后会自动下载并调用正确版本的 Bazel。你可以把 Bazelisk 当成 `bazel` 命令本身来用。

**关于 Bzlmod（MODULE.bazel）：** 现代 Bazel 用一个叫 `MODULE.bazel` 的文件，以「模块」为单位声明第三方依赖（类似 Rust 的 Cargo.toml）。历史上 Bazel 用的是 `WORKSPACE` 文件，但 XLS 已经迁移到 Bzlmod，`WORKSPACE` 仅作为旧工具识别工作区根目录的标记被保留。这点本讲会在源码精读里展开。

## 3. 本讲源码地图

本讲主要阅读仓库根目录的构建与说明文件，它们决定了「怎么把 XLS 编出来」：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目主文档，包含「Building From Source」「Docker Build」「Install Latest Release」三段，是构建指令的权威来源。 |
| `MODULE.bazel` | Bzlmod 依赖声明文件，列出 XLS 用到的全部第三方模块（Abseil、protobuf、LLVM 工具链等）。 |
| `WORKSPACE` | 旧式工作区文件，现已弃用，仅用于工作区根目录识别。 |
| `.bazelversion` | 锁定本仓库要求的 Bazel 版本。 |
| `.bazelrc` | Bazel 的全局配置：C++ 标准、编译选项、CI 配置、缓存导入等。 |
| `Dockerfile-ubuntu-22.04` | 提供一个「开箱即用」的 Ubuntu 22.04 构建环境镜像。 |
| `xls/dslx/BUILD` | 声明 `interpreter_main` 等 DSLX 工具二进制目标的构建规则。 |
| `xls/common/init_xls.cc` | 所有 XLS 命令行工具共用的初始化逻辑，本讲用来解释 `--version` 的来历。 |

## 4. 核心概念与源码讲解

### 4.1 Bazel 构建系统：目标、依赖与版本锁定

#### 4.1.1 概念说明

Bazel 的世界里有三件事物最基本：

- **目标（target）**：一个可被构建的产物，用标签表示，形如 `//xls/dslx:interpreter_main`。其中 `//xls/dslx` 是包（package，即含 `BUILD` 文件的目录），`interpreter_main` 是该包内的目标名。
- **构建规则（rule）**：定义「这个目标怎么造出来」，比如 `cc_binary`（C++ 可执行文件）、`cc_library`（C++ 库）、`cc_test`（测试）。规则写在每个包的 `BUILD` 文件里。
- **依赖（deps）**：规则之间的引用关系，Bazel 据此构建一张依赖图，决定编译顺序与是否需要重编。

XLS 把这一切串起来靠两个文件：`.bazelversion` 锁版本，`MODULE.bazel`（Bzlmod）管第三方依赖。理解这两点，你就能解释「为什么在一个干净环境里第一次构建要下载一堆东西」。

#### 4.1.2 核心流程

一次 `bazel build //xls/dslx:interpreter_main` 大致经历：

1. Bazel（或 Bazelisk）读 `.bazelversion`，确保用的是正确版本。
2. 解析 `MODULE.bazel`，按 Bzlmod 拉取并解析第三方依赖图，下载到本地缓存。
3. 从目标标签出发，沿 `deps` 边把整张依赖图展开。
4. 按拓扑顺序编译每个 `cc_library`（受 `.bazelrc` 里的 C++ 标准、警告开关控制），链接成最终的 `cc_binary`。
5. 产物落到 `./bazel-bin/` 下，例如 `./bazel-bin/xls/dslx/interpreter_main`。

#### 4.1.3 源码精读

先看版本锁定。`.bazelversion` 只有一行，但它是「可复现构建」的第一道闸门：

[.bazelversion:1-1](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/.bazelversion#L1-L1) —— 锁定 Bazel 版本为 `8.7.0`，Bazelisk 会据此自动下载该版本。

> 小提示：README 文字里建议「install bazel 7」，但仓库实际锁定的已是 `8.7.0`。遇到分歧时，以 `.bazelversion` 为准；用 Bazelisk 可以彻底避免这种手动版本不一致。

再看 Bzlmod 的依赖声明。`MODULE.bazel` 顶部声明了本模块自身，紧接着是一长串第三方依赖：

[MODULE.bazel:1-13](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/MODULE.bazel#L1-L13) —— 声明模块名为 `xls`（仓库名 `com_google_xls`），并通过 `bazel_dep` 引入 Abseil、protobuf、LLVM 工具链、rules_python 等。`register_toolchains("@llvm//toolchain:all")` 这一句尤为关键：它让 XLS 使用自带的 LLVM/Clang 工具链来编译 C++，从而摆脱对系统编译器版本的依赖——这也是「为什么第一次构建会顺带构建一个 LLVM」的原因。

`MODULE.bazel` 中段还用 `use_extension` / `use_repo` 引入了一些不在 BCR（Bazel Central Registry）里的依赖（如 Z3、perfetto、OpenROAD），它们通过 `dependency_support/` 下的模块扩展拉取：

[MODULE.bazel:122-127](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/MODULE.bazel#L122-L127) —— 通过模块扩展拉取 Z3 与 Bitwuzla 两个 SMT 求解器（形式化验证要用到）。

而旧的 `WORKSPACE` 文件顶部明确标注了它已被弃用：

[WORKSPACE:15-21](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/WORKSPACE#L15-L21) —— 声明 `workspace(name = "com_google_xls")`，并注释说明「此文件已弃用，请用 MODULE.bazel；保留它仅是为了旧版 Bazel 工具（主要是 Bazelisk）识别工作区边界」。

最后看 `.bazelrc`，它决定了每一次构建都自动生效的编译选项。这里能看到 XLS 的 C++ 标准是 C++20：

[.bazelrc:38-39](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/.bazelrc#L38-L39) —— 全局强制 `-std=c++20`，且对 host 与 target 都生效。

[.bazelrc:20-22](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/.bazelrc#L20-L22) —— 定义 `--config=ci` 配置：要求锁文件（lockfile）必须最新、注入 `XLS_IS_CI_RUN` 宏。CI 与 Docker 构建会用到它。

#### 4.1.4 代码实践

**实践目标**：用源码确认一个 Bazel 目标的存在与构成，建立「标签 → BUILD 规则 → 源文件」的对应关系。

**操作步骤**：

1. 打开 [xls/dslx/BUILD:617-619](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/BUILD#L617-L619)，确认目标 `interpreter_main` 是一条 `cc_binary` 规则，`srcs = ["interpreter_main.cc"]`。
2. 在同一文件里向上滚动，观察它的 `deps` 列出了哪些 `:xxx` 库（如 `:parse_and_typecheck`、`:import_data`）。
3. 同理定位另外三个核心工具的标签：
   - IR 转换器：[xls/dslx/ir_convert/BUILD:705-706](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/dslx/ir_convert/BUILD#L705-L706)（`ir_converter_main`）
   - 优化器：[xls/tools/BUILD:437-438](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/BUILD#L437-L438)（`opt_main`）
   - 代码生成器：[xls/tools/BUILD:833-834](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/tools/BUILD#L833-L834)（`codegen_main`）

**预期现象**：你会看到这四个工具的标签分别是 `//xls/dslx:interpreter_main`、`//xls/dslx/ir_convert:ir_converter_main`、`//xls/tools:opt_main`、`//xls/tools:codegen_main`，正好对应上一讲提到的工具链四个环节。

#### 4.1.5 小练习与答案

**练习 1**：为什么 XLS 同时保留 `WORKSPACE` 和 `MODULE.bazel` 两个文件？
**答案**：项目已迁移到 Bzlmod（`MODULE.bazel`）声明依赖；`WORKSPACE` 仅保留是因为旧版工具（主要是 Bazelisk 早期版本）需要靠它来识别「工作区根目录」。实际依赖解析以 `MODULE.bazel` 为准。

**练习 2**：`.bazelrc` 里的 `build:ci --copt="-DXLS_IS_CI_RUN=1"` 起什么作用？它是默认生效的吗？
**答案**：它定义一个名为 `ci` 的配置，在该配置下给编译器注入宏 `XLS_IS_CI_RUN=1`，源码可据此在 CI 环境里改变行为。它不是默认生效的——只有在命令行加 `--config=ci` 时才会启用。

---

### 4.2 依赖安装说明：系统层面的准备

#### 4.2.1 概念说明

虽然 XLS 用 Bzlmod 管理**第三方库**的依赖（这些会被自动下载编译），但构建前仍需在系统里装好少数**系统级依赖**：Python 开发头文件、`libtinfo6`、以及一个关键的小工具 `python-is-python3`。少了这些，构建会在中途报出一些「神秘错误」。

#### 4.2.2 核心流程

Ubuntu 上的依赖安装就一行命令，它解决两类问题：

1. 提供 C++ 编译链之外的运行时库（`libtinfo6` 等）。
2. 修复 Ubuntu 上 `python` 命令默认缺失的问题——很多 Bazel 规则会调用 `python`，而新版 Ubuntu 默认只有 `python3`，`python-is-python3` 这个包把 `/usr/bin/python` 指向 `python3`。

#### 4.2.3 源码精读

README 的「Building From Source」段落给出了权威依赖清单：

[README.md:126-128](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L126-L128) —— 注释特别提醒「让 Ubuntu 知道 `/usr/bin/env python` 实际是 python3……这一步很重要，否则你会遇到神秘错误」，随后给出 `sudo apt install python3-dev libtinfo6 python-is-python3`。

如果你用 Docker，`Dockerfile-ubuntu-22.04` 里装了更完整的一套系统依赖，可以当作「参考依赖清单」：

[Dockerfile-ubuntu-22.04:28-28](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/Dockerfile-ubuntu-22.04#L28-L28) —— 一次性安装 `python3-dev python-is-python3 libtinfo6 build-essential libxml2-dev liblapack-dev libblas-dev gfortran zip default-jdk` 等构建所需系统包。其中 `build-essential` 提供 gcc/g++/make，`gfortran`/`liblapack`/`libblas` 主要服务于线性求解依赖（如 or-tools），`default-jdk` 服务于 Java 相关规则（protobuf/closure）。

#### 4.2.4 代码实践

**实践目标**：确认本机是否已具备构建 XLS 的最小系统依赖。

**操作步骤**：

1. 运行 `python3 --version`，确认 Python 3 已安装。
2. 运行 `which python || echo "python 命令缺失"`。
3. 若上一步提示缺失，按 README 安装：`sudo apt install python3-dev libtinfo6 python-is-python3`。
4. 安装后再次 `which python`，确认 `/usr/bin/python` 已存在。

**预期结果**：`python` 命令可用，指向 python3。

#### 4.2.5 小练习与答案

**练习**：为什么 README 要专门强调安装 `python-is-python3`？如果不装会发生什么？
**答案**：因为新版 Ubuntu 默认不提供 `/usr/bin/python`，而项目里部分 Bazel 规则调用的是 `python` 而非 `python3`。不装会导致这些规则在执行时找不到解释器，报出与 Python 无关的、令人困惑的报错。

---

### 4.3 从源码构建 XLS：DSLX-only 与全量构建

#### 4.3.1 概念说明

XLS 有两条前端：主推的 **DSLX** 与实验性的 **C++/xlscc**。`xlscc` 依赖 Clang，构建它要额外编译一大块工具链，耗时会显著增加。因此官方提供两种构建范围：

- **DSLX-only**：排除 `xls/contrib/xlscc/...`，构建更快（8 核 VM 约 2 小时）。
- **全量**：包含 `xlscc`（可能长达 6 小时）。

绝大多数学习者只需要 DSLX 前端，本讲（以及本手册前几个单元）都按 DSLX-only 路线来。

#### 4.3.2 核心流程

构建一个具体工具（而非全部）的命令是：

```bash
bazel build -c opt //xls/dslx:interpreter_main
```

要点拆解：

- `-c opt`：使用优化（optimized）编译模式，产物运行更快（XLS 工具是计算密集型，推荐始终用 `opt`）。
- `//xls/dslx:interpreter_main`：要构建的目标标签。
- 产物路径：`./bazel-bin/xls/dslx/interpreter_main`。

如果要构建并**测试**整个项目（DSLX-only），则用 `bazel test` 配合排除模式：

```bash
bazel test -c opt -- //xls/... -//xls/contrib/xlscc/...
```

这里的 `//xls/...` 是「`xls/` 下所有目标」的通配，`-//xls/contrib/xlscc/...` 前面的减号表示「排除」。`--` 用来分隔 Bazel 选项与目标模式。

#### 4.3.3 源码精读

README 给出了两种构建范围的命令与预期耗时：

[README.md:109-137](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L109-L137) —— 说明在 8 核 VM 上，不含 C++ 前端的「DSLX only」构建约 2 小时，含 C++ 前端可达 6 小时；并给出两条 `bazel test -c opt` 命令分别对应两种范围。

其中 DSLX-only 与全量两条命令的差别就在那个排除子模式：

[README.md:133-136](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L133-L136) —— DSLX-only 用 `-- //xls/... -//xls/contrib/xlscc/...`，全量用 `-- //xls/...`。

官方 Quick Start 进一步示范了「先 `bazel build`，再直接用 `./bazel-bin/` 下产物」的工作流，这能避免每次都走 `bazel run` 的开销：

[docs_src/tools_quick_start.md:39-47](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/docs_src/tools_quick_start.md#L39-L47) —— 假设已完成 `bazel build -c opt //xls/...`，之后可直接调用 `./bazel-bin/xls/dslx/interpreter_main /tmp/simple_add.x`。

#### 4.3.4 代码实践

**实践目标**：亲手构建出 `interpreter_main`，记录耗时，并确认产物可用。这是本讲的主实践。

**操作步骤**：

1. 克隆仓库并进入目录（若尚未做）：`git clone https://github.com/google/xls.git && cd xls`。
2. 安装 Bazel（推荐用 Bazelisk，它会按 `.bazelversion` 自动取 8.7.0）。
3. 安装系统依赖：`sudo apt install python3-dev libtinfo6 python-is-python3`。
4. 用 `time` 计时执行构建：
   ```bash
   time bazel build -c opt //xls/dslx:interpreter_main
   ```
5. 确认产物可用：
   ```bash
   ./bazel-bin/xls/dslx/interpreter_main --version
   ```

**需要观察的现象**：

- 首次构建会先下载并构建 LLVM 工具链及大量第三方库，磁盘与网络活动明显，耗时长（远超第二次增量构建）。
- 构建成功后，`./bazel-bin/xls/dslx/interpreter_main` 是一个可执行文件。

**预期结果**：

- `time` 打印的 `real` 时间即本次构建耗时，记录下来。
- `interpreter_main --version` 会打印一段版本信息并正常退出。

**关于 `--version` 的输出**：该标志并非 `interpreter_main` 自己定义，而是 Abseil flags 内置的；XLS 在通用初始化里给它注入了构建标签：

[xls/common/init_xls.cc:34-41](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/common/init_xls.cc#L34-L41) —— `InitXls` 在构建标签（embed label）非空时，把 `absl::FlagsUsageConfig.version_string` 设成该标签，于是 `--version` 会打印它。

这个标签来自一个 Bazel `linkstamp` 文件：

[xls/common/build_embed.cc:21-23](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/common/build_embed.cc#L21-L23) —— `GetBuildEmbedLabel()` 返回宏 `BUILD_EMBED_LABEL` 的值，该宏仅在构建时传入 `--embed_label` 才非空。

[xls/common/BUILD:198-206](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/xls/common/BUILD#L198-L206) —— `init_xls` 库用 `linkstamp = "build_embed.cc"`，使该文件在每次构建信息变化时被重新编译，从而能拿到 `BUILD_EMBED_LABEL` 等构建期宏。

> **待本地验证**：本地 `bazel build` 默认不传 `--embed_label`，因此 `--version` 可能打印的是 Abseil 默认版本串（而非形如 `v0.0.0-xxxx` 的标签）。GitHub Release 的二进制是带标签构建的，所以输出更「正式」。两种情况下命令都能成功退出，可用于确认产物可用。

#### 4.3.5 小练习与答案

**练习 1**：为什么官方在大多数示例里都加 `-c opt`？不加会怎样？
**答案**：`-c opt` 启用优化编译，运行性能更好；XLS 工具是计算密集的，慢速产物会明显拖慢迭代。不加时默认是 `-c fastbuild`（带调试符号、无优化），适合快速本地调试但运行更慢。

**练习 2**：`-- //xls/... -//xls/contrib/xlscc/...` 中的两个 `...` 和一个减号分别是什么含义？
**答案**：`//xls/...` 表示 `xls/` 目录下所有包的全部目标（递归通配）；`-//xls/contrib/xlscc/...` 前的减号表示从目标集合中排除 `xlscc` 子树，从而得到「不含 C++ 前端」的 DSLX-only 构建范围。

---

### 4.4 加速构建：disk cache 与远程缓存

#### 4.4.1 概念说明

第一次全量构建 XLS 很慢（动辄数小时），但 Bazel 本身的增量缓存非常可靠——只要输入不变，就不会重编。问题在于：`bazel clean` 之后、或换了一台机器、或切换分支后，本地缓存会失效，又得从头来。为此 Bazel 提供**共享缓存**：把中间产物存到磁盘上的某个目录或远程服务，多机、多次复用。

#### 4.4.2 核心流程

启用 disk cache 的思路是：建一个持久目录，再把它写进 `.bazelrc`：

1. 创建目录，如 `~/.bazel_disk_cache/`。
2. 往 `~/.bazelrc` 追加两行，让 `build` 与 `test` 都用这个目录做磁盘缓存。
3. 之后所有构建（含 `bazel clean` 之后）都会优先命中该缓存。

注意：Bazel **不会自动清理**这个目录，它会无限增长，需要你定期手动清理。

#### 4.4.3 源码精读

README 的「Adding Additional Build Caching」段落给出了完整做法：

[README.md:161-177](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L161-L177) —— 用 `echo "build --disk_cache=$(realpath ~/.bazel_disk_cache)" >> ~/.bazelrc`（test 同理）启用磁盘缓存；并警告该目录无自动 GC、会无限增长。

[README.md:179-187](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L179-L187) —— 介绍了能自动做垃圾回收的**远程缓存**（remote cache）替代方案，并推荐本地运行 `bazel-remote`。

`.bazelrc` 末尾还预留了对用户自定义配置的导入，你可以把缓存等个人设置放进 `user.bazelrc` 而不污染主配置：

[.bazelrc:111-112](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/.bazelrc#L111-L112) —— `try-import %workspace%/user.bazelrc`：若存在则导入用户私有配置（`try-import` 表示文件不存在也不报错）。

#### 4.4.4 代码实践

**实践目标**：启用 disk cache，验证它在「清理后重建」时能加速。

**操作步骤**：

1. 创建缓存目录：`mkdir -p ~/.bazel_disk_cache`。
2. 追加配置：
   ```bash
   echo "build --disk_cache=$(realpath ~/.bazel_disk_cache)" >> ~/.bazelrc
   echo "test --disk_cache=$(realpath ~/.bazel_disk_cache)" >> ~/.bazelrc
   ```
3. 先做一次基线构建：`time bazel build -c opt //xls/dslx:interpreter_main`（首次，慢）。
4. 清理后立即重建：`bazel clean && time bazel build -c opt //xls/dslx:interpreter_main`。

**需要观察的现象**：第 4 步「清理后重建」的耗时应该明显小于第 3 步的首次构建，因为大量中间产物命中了 disk cache。

**预期结果**：第二次（清理后）构建显著更快。

> **待本地验证**：加速幅度取决于机器与缓存命中情况，具体数字需你本机实测。

#### 4.4.5 小练习与答案

**练习**：既然 disk cache 能加速，为什么 README 还要提醒「它会无限增长」并建议定期清理？
**答案**：Bazel 不会对 disk cache 做垃圾回收，所有写入的中间产物都会保留；随着分支切换、版本演进，目录会持续膨胀直至占满磁盘，因此需要人工或脚本定期清理。

---

### 4.5 替代获取方式：Docker 与二进制 Release

#### 4.5.1 概念说明

并非每个人都想从零搭构建环境。XLS 提供两条「省心」路径：

- **Docker 构建**：仓库自带 `Dockerfile-ubuntu-22.04`，把依赖、Bazelisk、源码全部封装进镜像，适合环境配置困难或想快速得到一个干净构建环境的人。
- **二进制 Release**：官方在 GitHub Releases 提供 x64 Linux 的预编译二进制，下载解压即可用，完全无需构建。

#### 4.5.2 核心流程

**Docker** 两种用法：

1. 直接 `docker build` 让镜像内自动跑测试（构建即验证）。
2. 用 `SKIP_TESTS=1` 构建一个「只搭好环境、不跑测试」的镜像，再 `docker run -it` 进去手动操作。

**Release 二进制**：用 GitHub API 找到最新 release 的 tarball URL，下载、解压，直接调用里面的 `interpreter_main`、`opt_main` 等。

#### 4.5.3 源码精读

Dockerfile 的关键步骤——基于 Ubuntu 22.04、装 Bazelisk、装依赖、按需跑测试：

[Dockerfile-ubuntu-22.04:5-7](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/Dockerfile-ubuntu-22.04#L5-L7) —— 基础镜像 `ubuntu:22.04`，并声明构建参数 `ARG SKIP_TESTS=0`。

[Dockerfile-ubuntu-22.04:18-24](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/Dockerfile-ubuntu-22.04#L18-L24) —— 从 GitHub 下载 `bazelisk-linux-amd64` 并装为 `/usr/bin/bazel`，于是镜像内 `bazel` 命令会按 `.bazelversion` 自动取正确版本。

[Dockerfile-ubuntu-22.04:42-42](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/Dockerfile-ubuntu-22.04#L42-L42) —— 当 `SKIP_TESTS != 1` 时执行 `bazel test --config=ci -c opt -- //xls/... -//xls/contrib/... -//xls/dev_tools/...`。注意这里用的是 `--config=ci`，且排除了 `contrib`（含 xlscc）与 `dev_tools` 以缩短构建时间。

README 的「Docker Build」段落给出对应命令：

[README.md:144-159](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L144-L159) —— 演示两种用法：`docker build . -f Dockerfile-ubuntu-22.04`（自动跑测试）与带 `--build-arg SKIP_TESTS=1` 的「仅搭环境」镜像，再用 `docker run -it --rm xls-build-docker /bin/bash` 进入手动构建。

README 的「Install Latest Release」段落给出二进制获取脚本：

[README.md:81-98](https://github.com/google/xls/blob/e796ea8aeea3875362c7dbeb11a850f3854a9116/README.md#L81-L98) —— 用 GitHub API 取最新 release 的 tarball URL，下载解压后直接 `./interpreter_main --version` 等逐个验证。这些预编译二进制省去了全部构建时间。

#### 4.5.4 代码实践

**实践目标**：用 Release 二进制在「零构建」前提下快速获得可用的 XLS 工具。

**操作步骤**（在一台 x64 Linux 上）：

1. 按 README 脚本取最新 release 的 tarball URL。
2. `curl -O -L <url>` 下载，`tar -xzvvf xls-*.tar.gz` 解压，`cd xls-*/`。
3. 运行 `./interpreter_main --version` 与 `./opt_main --version`。

**预期结果**：每条 `--version` 都打印带版本标签的字符串并正常退出，说明二进制可用。

> **待本地验证**：若你的机器不是 x64 Linux（如 ARM Mac），Release 二进制不可直接使用，需改走源码或 Docker 路线。

#### 4.5.5 小练习与答案

**练习**：Dockerfile 里构建命令带了 `--config=ci`，而 README 给的手动命令没有。这个差异说明了什么？
**答案**：`--config=ci` 启用了 `.bazelrc` 里 `ci` 段定义的配置（强制 lockfile 最新、注入 `XLS_IS_CI_RUN` 宏）。Docker 镜像用于可复现的参考构建/CI，故启用更严格的 ci 配置；本地手动构建时按需选择即可。这也提醒我们：同一仓库在不同场景会用不同的配置组合。

---

## 5. 综合实践

把本讲的几条线索串起来：在 Ubuntu 上从零获得一个可用的 `interpreter_main`，并用它跑通一个最小的 DSLX 测试。

1. **环境**：确认 `python` 命令可用（必要时装 `python-is-python3`）。
2. **缓存**：先配置 disk cache（4.4 节），为后续单元的反复构建省时间。
3. **构建**：`time bazel build -c opt //xls/dslx:interpreter_main`，记录耗时。
4. **验证产物**：`./bazel-bin/xls/dslx/interpreter_main --version`。
5. **跑一个真实输入**：新建 `/tmp/simple_add.x`（内容参考 `docs_src/tools_quick_start.md`），执行：
   ```bash
   ./bazel-bin/xls/dslx/interpreter_main /tmp/simple_add.x
   ```
6. **对比**：若你也下载了 Release 二进制（4.5 节），用它跑同一个 `.x` 文件，确认两者行为一致——这正是 XLS「同一设计既能本地运行、又能分发」理念的缩影。

完成后，你应该拥有一台「能随时编译并运行 XLS 工具」的机器，为后续单元逐个走通 IR 转换、优化、代码生成打下基础。

## 6. 本讲小结

- XLS 用 **Bazel** 构建，依赖由 **Bzlmod（`MODULE.bazel`）** 管理，版本由 **`.bazelversion`** 锁定（当前 `8.7.0`），推荐用 **Bazelisk** 自动取版本。
- 构建 XLS 前要装少数系统依赖，其中 **`python-is-python3`** 尤为关键，能避免一堆神秘报错。
- 构建分两种范围：**DSLX-only**（排除 `xlscc`，约 2 小时）与**全量**（含 `xlscc`，可达 6 小时）；学习者通常用前者。
- 一个工具的构建命令形如 `bazel build -c opt //xls/dslx:interpreter_main`，产物在 `./bazel-bin/` 下。
- 可用 **disk cache / 远程缓存** 大幅缩短重复构建时间，但 disk cache 需自行清理。
- 除源码构建外，还有 **Docker 镜像** 与 **GitHub Release 二进制** 两条省心路径；`--version` 由 Abseil 内置标志 + `InitXls` 注入的构建标签实现。

## 7. 下一步学习建议

你已经能把 XLS 编出来并运行 `interpreter_main`。下一步建议：

- 进入 **u1-l3（目录结构与项目布局）**：对照 Stack Diagram 认识 `xls/` 下各子目录的职责，搞清你刚构建出的 `dslx/`、`tools/`、`ir/` 分别属于流水线的哪一段。
- 随后进入 **u1-l4（用 DSLX 写第一个硬件函数）**：开始动手写 DSLX，并用本讲构建出的 `interpreter_main` 跑测试。
- 若想深入构建本身，可继续阅读 `dependency_support/` 下的各模块扩展，理解 LLVM、Z3、OpenROAD 等是如何被接入 Bzlmod 的。
