# Bazel 构建系统与快速上手

## 1. 本讲目标

本讲是「走进 CoralNPU」单元的第三讲。在前两讲里，你已经认识了 CoralNPU 是「三核一体的 ML 加速器」，也拿到了仓库的代码地图。本讲要回答一个最实际的问题：**这么多代码（约 1000 个文件、Chisel + SystemVerilog + C/C++ + Python），到底用什么工具把它们组织起来、又怎么构建出可运行的产物？**

读完本讲，你应当能够：

1. 说出 Bazel 在 CoralNPU 仓库里扮演的角色，以及 `WORKSPACE`、`.bazelrc`、`BUILD.bazel` 三个文件各自管什么。
2. 看懂 README 给出的 Quick Start 三步命令，并理解每一步背后被 `.bazelrc` 触发了哪些配置。
3. 知道 `rules/` 目录下存在 CoralNPU 自定义的 Bazel 规则（以 `coralnpu_v2.bzl` 为代表），并能解释它如何把一段 C++ 编译成 CoralNPU 上能跑的 `.elf/.bin/.vmem`。
4. 自己动手跑通（或至少源码级读懂）Quick Start 的三条命令。

> 本讲刻意把硬件细节挡在门外，只讲「构建系统怎么运转」。具体的工具链、链接脚本、仿真器内部会在后续讲义（u2-l1、u2-l3）展开。

## 2. 前置知识

- **Bazel 是什么**：Google 开源的构建工具。它的核心思想是「把一次构建看作一张依赖图」，每个产物（target）声明自己的依赖，Bazel 负责增量、并行、可复现地把图算出来。对于 CoralNPU 这种「一个仓库里同时有 Scala/Verilog/C++/Python」的超大 monorepo，统一的构建系统几乎是必须的。
- **三个关键文件**：
  - `WORKSPACE`（或 `MODULE.bazel`）：声明「这是哪个工作区」、从哪里拉取外部依赖。
  - `BUILD` / `BUILD.bazel`：声明「这个目录（package）里有哪些可构建的目标」，是 Bazel 的主体。
  - `.bazelrc`：构建工具的「参数预设」，把一长串命令行开关固化下来。
- **目标标签（label）**：Bazel 用 `@repo//path:target` 的形式唯一定位一个目标。本仓库里你看到的最典型的写法是 `//examples:coralnpu_v2_hello_world_add_floats`，意思是「本工作区的 `examples/` 包里，名字叫 `coralnpu_v2_hello_world_add_floats` 的目标」。
- **一个真实的小坑（沿用 u1-l2 的发现）**：README 的 System Requirements 写着 **Bazel 7.4.1**，但仓库根的 [.bazelversion](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/.bazelversion) 把版本钉死在 **8.6.0**。两者有漂移，实际请以 `.bazelversion` 为准（用 `bazelisk` 时它会被自动尊重）。本讲讲解 Quick Start 时不会受这个版本号影响。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲怎么用 |
| --- | --- | --- |
| [README.md](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/README.md) | 项目入口文档，给出 Quick Start | Quick Start 三步命令的唯一权威来源 |
| [WORKSPACE](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/WORKSPACE) | 声明工作区名、拉取外部依赖、注册工具链 | 看懂「外部世界从哪来」 |
| [.bazelrc](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/.bazelrc) | 全局构建开关与 config 预设 | 解释 Quick Start 命令背后的隐含配置 |
| [BUILD.bazel](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/BUILD.bazel) | 工作区根的包（刻意留空） | 理解「根包」与目标标签的关系 |
| [rules/coralnpu_v2.bzl](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/rules/coralnpu_v2.bzl) | CoralNPU 自定义规则：编译出 `.elf/.bin/.vmem` | 理解「为什么编译器会切到 RISC-V」 |
| [platforms/BUILD.bazel](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/platforms/BUILD.bazel) | 定义 `coralnpu_v2` 目标平台 | 解释自定义规则的「平台切换」 |
| [examples/BUILD.bazel](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/examples/BUILD.bazel) | 声明示例二进制 | Quick Start 第二步的目标 |

> 补充：`rules/` 目录下还有 `verilog.bzl`、`chisel.bzl`、`coco_tb.bzl`、`vcs.bzl` 等大量自定义规则，分别管 Verilog/Chisel 仿真、cocotb 测试、VCS 综合流程。本讲只精读与「编译一个 CoralNPU 程序」最相关的 `coralnpu_v2.bzl`，其余在 u11 验证流程单元再讲。

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：先看「工作区从哪来」（WORKSPACE），再看「全局开关」（.bazelrc），再看「目标怎么声明」（BUILD.bazel），最后看「CoralNPU 专属编译规则」（coralnpu_v2.bzl）。

### 4.1 Bazel 工作区：WORKSPACE 与外部依赖

#### 4.1.1 概念说明

一个 Bazel 项目首先得告诉工具「我是一块工作区」。这块工作区有一个名字、一份根目录，并且通常会**从网络拉取大量第三方依赖**（工具链、规则库、源码包）。

CoralNPU 仓库体量很大，它几乎不把第三方代码塞进自己的 git 仓库，而是用 `WORKSPACE` 在构建时把它们拉下来。这些依赖包括：

- `rules_cc`、`rules_python`、`rules_java`、`io_bazel_rules_scala`：Bazel 官方/社区的「语言规则」，让 Bazel 懂得怎么编译 C++/Python/Java/Scala。
- `toolchain_coralnpu_v2`：CoralNPU 专用的 RISC-V 工具链（GCC 交叉编译器 tar 包）。
- `tflite_micro`、`com_google_mpact-riscv`、`lowrisc_opentitan_gh`：TensorFlow Lite Micro、ISA 参考模型、OpenTitan 工具。

> 小提示：`WORKSPACE` 这种「传统」写法正在被 Bazel 官方推向新的 `MODULE.bzl`（bzlmod）。CoralNPU 目前明确**还在用 WORKSPACE 模式**，这一点在 `.bazelrc` 第一行就能看到（4.2 节）。

#### 4.1.2 核心流程

WORKSPACE 的执行流程很线性，但有一个关键特点：**加载与 `load()` 是顺序敏感的**。

```text
workspace(name = ...)        # 1. 给工作区起名
http_archive(...)            # 2. 下载某个外部仓库
load("@xxx//:yyy.bzl", ...)  # 3. 这个外部仓库"下载完之后"，才能从它里面 load 符号
xxx_repos() / register_toolchains(...)  # 4. 用这些符号注册依赖/工具链
... 重复 2-4 ...
```

也就是说，你必须先 `http_archive` 把一个仓库拉下来，才能在后面的 `load()` 里引用它里面的 `.bzl` 文件。这就是为什么 WORKSPACE 里会出现「下载一段、load 一段、再下载一段」的交错结构——这是写出来的，不是乱序。

#### 4.1.3 源码精读

**工作区命名**。第 15 行把整个工作区命名为 `coralnpu_hw`：

[WORKSPACE:15-15](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/WORKSPACE#L15) —— `workspace(name = "coralnpu_hw")`，这个名字决定了仓库内部用 `@coralnpu_hw//...` 引用自己（在很多 `.bzl` 里能看到）。

**典型外部依赖：rules_cc**。第 33–38 行用 `http_archive` 下载 Bazel 官方的 C++ 规则，并校验了 sha256：

[WORKSPACE:33-38](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/WORKSPACE#L33-L38) —— 拉取 `rules_cc-0.2.9`。注意紧随其后的 `load("@rules_cc//cc:repositories.bzl", ...)` 必须出现在这段下载之后，否则符号不存在。

**CoralNPU 专用工具链**。第 162–178 行下载了真正关键的 RISC-V 工具链 tar 包：

[WORKSPACE:162-178](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/WORKSPACE#L162-L178) —— `name = "toolchain_coralnpu_v2"`，从一个 Google Storage 地址拉取 `toolchain_kelvin_v2-2025-09-11.tar.gz`（名字里带 `kelvin`，是历史沿袭）。下载完后，第 180–183 行把它注册成可用的工具链：

[WORKSPACE:180-183](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/WORKSPACE#L180-L183) —— 注册了 `cc_coralnpu_v2_toolchain`（正常运行）和 `cc_coralnpu_v2_semihosting_toolchain`（半托管调试）两条工具链。Quick Start 第二步编译出的 RISC-V ELF，就是靠这套工具链完成的。

**最后一道防护**。第 227–237 行有一个有趣的 `check_folder`：

[WORKSPACE:227-237](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/WORKSPACE#L227-L237) —— 它检查是否存在 `internal/` 目录（用于内部综合网表），不存在则跳过。这就是为什么普通用户 clone 仓库后，构建仍能正常进行——内部的、受许可限制的部分是「可选挂载」的。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：确认你机器上 Bazel 到底拉了哪些外部仓库。
2. **操作步骤**：
   - 在仓库根执行 `bazel info workspace`，确认工作区根目录被正确识别。
   - 执行 `bazel sync`（首次会比较慢，会真正下载 WORKSPACE 里声明的依赖），完成后观察 `$(bazel info output_base)/external/` 目录。
   - 执行 `bazel query 'attr("name", "toolchain_coralnpu_v2", @//...)'` 或 `ls` 上面那个 `external` 目录，找到 `toolchain_coralnpu_v2`。
3. **需要观察的现象**：`external/` 下应能看到 `rules_cc`、`toolchain_coralnpu_v2`、`tflite_micro`、`io_bazel_rules_scala` 等名字，与 WORKSPACE 里 `http_archive` 的 `name` 一一对应。
4. **预期结果**：外部依赖名 ↔ WORKSPACE 里 `http_archive` 的 `name` 完全对得上。
5. **若无法确定运行结果**：网络受限环境下 `bazel sync` 可能失败，此时可改为「源码阅读型」——把 WORKSPACE 里所有 `name = "xxx"` 收集起来，对照 `external/` 目录即可。**待本地验证**。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 WORKSPACE 里 `load("@rules_cc//cc:repositories.bzl", ...)` 必须出现在 `http_archive(name="rules_cc", ...)` 之后？
  - **答案**：`load()` 是在「仓库已经存在」的前提下从里面取符号。下载在前、加载在后，顺序写反了 Bazel 会报「找不到外部仓库 rules_cc」。
- **练习 2**：CoralNPU 的 RISC-V 工具链是 commit 进仓库的，还是构建时下载的？
  - **答案**：构建时下载。第 162–178 行的 `http_archive` 从 Google Storage 拉取 tar 包，sha256 校验后注册为工具链。

---

### 4.2 全局构建配置：.bazelrc

#### 4.2.1 概念说明

`.bazelrc` 是 Bazel 的「默认参数表」。每当你敲 `bazel build ...`，Bazel 都会先把 `.bazelrc` 里和 `build` 相关的行拼到你的命令前面。它有两个核心机制：

1. **命令分组**：`common`（所有命令）、`build`、`test`、`run` 分别对应对应子命令生效。
2. **命名 config**：写成 `build:名字 --flag` 的行，只有你显式加 `--config=名字` 时才启用。

CoralNPU 的 `.bazelrc` 用这套机制做了三件大事：**关掉 bzlmod**、**默认跳过需要商业 EDA 许可的目标**、**定义切到 CoralNPU 平台的 config**。

#### 4.2.2 核心流程

```text
用户敲: bazel build //examples:coralnpu_v2_hello_world_add_floats
        ↓
Bazel 读取 .bazelrc:
  - 先应用所有 common 行
  - 再应用所有 build 行（含 --copt, --action_env 等）
  - 若用户带了 --config=xxx，再叠加 build:xxx 行
        ↓
把合并后的完整命令行交给构建
```

#### 4.2.3 源码精读

**显式使用 WORKSPACE 模式**。第 1–2 行关掉了新默认的 bzlmod：

[.bazelrc:1-2](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/.bazelrc#L1-L2) —— `common --noenable_bzlmod --enable_workspace`。这就是为什么本讲 4.1 节说「还在用 WORKSPACE 模式」——它不是默认的，而是被显式选中的。

**统一的 C/C++ 标准与警告**。第 10–21 行固定了语言标准和告警抑制：

[.bazelrc:10-21](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/.bazelrc#L10-L21) —— C++17、C11、关掉一批噪声告警、host 工具 `-O3`。这些对任何 `build` 都生效。

**默认跳过 VCS/综合/功耗目标**。这是新手最该知道的一条。第 42–43 行：

[.bazelrc:42-43](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/.bazelrc#L42-L43) —— `build --build_tag_filters="-vcs,-synthesis,-power"`。开头的负号表示「排除」。也就是说，默认情况下所有打了 `vcs`/`synthesis`/`power` tag 的目标都不会被构建——因为它们需要 Synopsys VCS、综合工具、功耗分析工具的商业许可，普通工作站没有。

而当你**确实**有许可、想跑 VCS 流程时，第 85–89 行提供了专门的 config 来反转这个过滤：

[.bazelrc:85-89](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/.bazelrc#L85-L89) —— `build:vcs --build_tag_filters="vcs"`（注意这里是**正号/只保留** `vcs`）。于是 `bazel build --config=vcs //xxx` 就会专门挑出 VCS 目标。类似的还有 `build:synthesis`、`build:power`（第 91–103 行）。

**CoralNPU 平台 config**。第 113–114 行：

[.bazelrc:113-114](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/.bazelrc#L113-L114) —— `build:coralnpu_v2 --platforms=//platforms:coralnpu_v2`。这个 config 把构建目标平台切到 RISC-V 的 `coralnpu_v2`。**注意**：Quick Start 的三条命令里你**看不到** `--config=coralnpu_v2`，因为它是由 4.4 节的自定义规则通过「平台 transition」自动触发的（见 4.4.3）。

**用户私有配置**。第 132 行：

[.bazelrc:132-132](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/.bazelrc#L132) —— `try-import %workspace%/.bazelrc.user`。你可以建一个被 git 忽略的 `.bazelrc.user` 放自己的本地配置（比如本地工具路径），不会影响他人。

#### 4.2.4 代码实践

1. **实践目标**：把 Quick Start 三条命令与 `.bazelrc` 里的配置对应起来。
2. **操作步骤**：对照 [README.md:31-45](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/README.md#L31-L45) 的三条命令，逐条标注它隐式吃到了哪些 `.bazelrc` 行：
   - `bazel run //tests/cocotb:core_mini_axi_sim_cocotb` → 吃到所有 `run:` 行 + `common` 行。
   - `bazel build //examples:coralnpu_v2_hello_world_add_floats` → 吃到所有 `build` 行（含 tag 过滤、copt）；目标平台由规则自动 transition。
   - `bazel build //tests/verilator_sim:core_mini_axi_sim` → 同上，且因为不带 `vcs` tag，不会被第 42 行的过滤排除。
3. **需要观察的现象**：这三条命令都不需要你手动加 `--config=xxx` 也能跑，说明默认配置已经覆盖了普通用户的场景。
4. **预期结果**：你能用自己的话说出「为什么默认 `bazel build` 不会去构建 VCS 目标」。
5. **若无法确定运行结果**：`bazel build` 仍需完整工具链，若无网络/许可可只做源码对照。**待本地验证**。

#### 4.2.5 小练习与答案

- **练习 1**：默认 `bazel build //...` 会不会构建打了 `vcs` tag 的目标？为什么？想构建又该怎么做？
  - **答案**：不会。`.bazelrc:42` 的 `--build_tag_filters="-vcs,-synthesis,-power"` 默认排除它们，因为这些需要商业 EDA 许可。要构建就加 `--config=vcs`（`.bazelrc:85-89`）。
- **练习 2**：README Quick Start 里没有任何 `--platforms=//platforms:coralnpu_v2`，为什么编译示例时还是会用 RISC-V 工具链？
  - **答案**：示例目标用的是自定义规则 `coralnpu_v2_binary`，它内部用 transition 强制把平台切到 `//platforms:coralnpu_v2`（见 4.4.3），所以无需用户手动指定。

---

### 4.3 包与目标：BUILD.bazel 与标签语法

#### 4.3.1 概念说明

Bazel 把仓库切成一个个**包（package）**：每个含有 `BUILD`/`BUILD.bazel` 的目录是一个包。包里用各种规则（`cc_binary`、`filegroup`、自定义规则…）声明**目标（target）**。目标用标签定位：

- `//examples:coralnpu_v2_hello_world_add_floats`：本仓库 `examples/` 包里的某目标。
- `//tests/verilator_sim:core_mini_axi_sim`：`tests/verilator_sim/` 包里的某目标。

理解标签的关键是「**包 = 目录**，**目标 = 冒号后的名字**」。

#### 4.3.2 根包的「空 BUILD」是干什么的？

仓库根的 [BUILD.bazel](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/BUILD.bazel) 只有一行注释：

[BUILD.bazel:1-1](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/BUILD.bazel#L1) —— `# Empty BUILD file for the workspace root.`。

它看起来「什么都没做」，但其实很重要：它**让仓库根成为一个合法的 Bazel 包**。这样根目录下的文件（比如 `README.md`）可以被 `//:README.md` 这样的标签引用，子目录也能用相对标签向上引用根包里的东西（WORKSPACE 第 232 行的 `root_file = "//:BUILD.bazel"` 就依赖这一点）。简而言之：**空 BUILD 不是疏忽，是「注册根包」的约定**。

#### 4.3.3 Quick Start 的目标从哪来

Quick Start 第二步编译的目标，声明在 `examples/BUILD.bazel` 里：

[examples/BUILD.bazel:19-22](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/examples/BUILD.bazel#L19-L22) —— 这就是一个 `coralnpu_v2_binary` 自定义规则的调用，源码是 `hello_world_add_floats.cc`。它**不是** Bazel 自带的 `cc_binary`，而是 CoralNPU 自己写的（4.4 节）。

而它依赖的 `coralnpu_v2_binary` 符号，来自同文件第 17 行的 load：

[examples/BUILD.bazel:17-18](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/examples/BUILD.bazel#L17-L18) —— `load("/rules:coralnpu_v2.bzl", "coralnpu_v2_binary")`，把自定义规则加载进来。

#### 4.3.4 代码实践

1. **实践目标**：用 Bazel 的查询能力，不实际编译也能「画出」目标依赖。
2. **操作步骤**：
   - `bazel query '//examples:all'` 列出 `examples` 包下所有目标。
   - `bazel query 'deps(//examples:coralnpu_v2_hello_world_add_floats)' --output=label` 打印该目标的完整依赖闭包。
3. **需要观察的现象**：依赖图里应能看到 `//toolchain/crt`（C 运行时）、`@toolchain_coralnpu_v2`（RISC-V 工具链）、生成链接脚本的 `generate_linker_script` 等。
4. **预期结果**：你能在依赖图里找到「源码 → CRT → 工具链」这条链，为 u2-l1（工具链与链接脚本）埋下伏笔。
5. **若无法确定运行结果**：`bazel query` 是只读的、相对轻量，通常不需要许可即可运行；但仍可能触发部分加载。**待本地验证**。

#### 4.3.5 小练习与答案

- **练习 1**：标签 `//tests/verilator_sim:core_mini_axi_sim` 里的「包」和「目标」分别是哪部分？
  - **答案**：包是 `tests/verilator_sim/`（含 BUILD 文件的目录），目标是 `core_mini_axi_sim`（冒号后的名字）。
- **练习 2**：根目录的 `BUILD.bazel` 是空的，为什么还要存在？
  - **答案**：让仓库根成为合法 Bazel 包，使 `//:xxx` 形式的标签可用，并满足 `check_folder` 等 `root_file` 引用（WORKSPACE:232）。

---

### 4.4 自定义规则 rules/coralnpu_v2.bzl

这是本讲最有「CoralNPU 味道」的一节。前面的 `.bazelrc`/`WORKSPACE`/`BUILD.bazel` 都是 Bazel 通用知识；本节解释 CoralNPU 为什么需要自己写一条规则。

#### 4.4.1 概念说明

Bazel 内置的 `cc_binary` 默认用「宿主机工具链」编译（比如你 PC 上的 x86 gcc）。但 CoralNPU 程序要跑在 **RISC-V** 核上，必须：

1. **切换到 RISC-V 工具链**（4.1 里下载的 `toolchain_coralnpu_v2`）。
2. 用一份**为 ITCM/DTCM 内存布局定制的链接脚本**（u2-l1 会详讲）。
3. 除了 ELF，还要产出**裸机镜像** `.bin` 和仿真用的 `.vmem`。

这三件事都不是 `cc_binary` 默认能做的，所以 CoralNPU 写了 `coralnpu_v2.bzl`。

Bazel 规则用 **Starlark** 语言（Python 的一个受限子集）编写。「切换工具链」在 Bazel 里是通过 **platform transition**（平台转移）实现的：当一个目标被依赖时，强行把构建配置切到指定平台。

#### 4.4.2 核心流程

一条 `coralnpu_v2_binary` 目标从源码到产物，经过这些阶段：

```text
cc 源码
  │  ① transition: 把平台切到 //platforms:coralnpu_v2
  ▼
cc_common.compile   (用 RISC-V gcc 编译 .o)
  │  ② 用生成的 coralnpu 链接脚本
  ▼
cc_common.link      →  name.elf
  │  ③ objcopy
  ▼
name.bin            (裸机二进制)
  │  ④ srec_cat (SRecord 工具)
  ▼
name.vmem           (Verilog $readmemh 可加载的 hex 文件)
```

`.vmem` 是仿真器加载程序时常用的格式（字节序、按字填充），所以 Quick Start 跑仿真前会用到它。

#### 4.4.3 源码精读

**平台常量**。第 21–22 行定义了两个目标平台：

[rules/coralnpu_v2.bzl:21-22](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/rules/coralnpu_v2.bzl#L21-L22) —— `CORALNPU_V2_PLATFORM = "//platforms:coralnpu_v2"` 等。这两个标签对应 [platforms/BUILD.bazel:17-23](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/platforms/BUILD.bazel#L17-L23) 里定义的 `platform`，约束值是「`cpu: coralnpu_v2`、`os: none`」——一个裸机 RISC-V 目标。

**transition 实现**。第 24–34 行是平台切换的核心：

[rules/coralnpu_v2.bzl:24-34](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/rules/coralnpu_v2.bzl#L24-L34) —— `_coralnpu_v2_transition_impl` 根据属性 `semihosting`，把 `//command_line_option:platforms` 改写成 `coralnpu_v2` 或 `coralnpu_v2_semihosting`。这正是 4.2 节说的「Quick Start 不用手动 `--config=coralnpu_v2`」的原因——只要目标用了这条规则，平台就被自动切走。

**三件套产物的生成**。第 99–195 行的实现函数把上面流程图落地。关键三段：

- 编译 + 链接产出 ELF：[rules/coralnpu_v2.bzl:99-122](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/rules/coralnpu_v2.bzl#L99-L122)，注意第 117–119 行把链接脚本通过 `-Wl,-T,...` 喂给链接器。
- `objcopy` 产出 `.bin`：[rules/coralnpu_v2.bzl:124-138](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/rules/coralnpu_v2.bzl#L124-L138)。
- `srec_cat`（SRecord）产出 `.vmem`：[rules/coralnpu_v2.bzl:141-177](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/rules/coralnpu_v2.bzl#L141-L177)（README 的 System Requirements 里要求安装 SRecord，正是为了这一步）。

最终把这些产物整理成 `OutputGroupInfo`：[rules/coralnpu_v2.bzl:179-195](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/rules/coralnpu_v2.bzl#L179-L195)，分别叫 `elf_file` / `bin_file` / `vmem_file`。

**对外暴露的「宏（macro）」**。普通用户写的 `coralnpu_v2_binary(...)` 其实是一个**宏**，不是底层规则本身。第 218–330 行的 `coralnpu_v2_binary` 宏负责「填默认值」：

[rules/coralnpu_v2.bzl:218-256](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/rules/coralnpu_v2.bzl#L218-L256) —— 它把 `itcm_size_kbytes = 8`、`dtcm_size_kbytes = 32`、`stack_size_bytes = 128` 等默认值填好。这就和 u1-l1 讲过的「ITCM 8KB / DTCM 32KB」对上了——这两个数字不是偶然，它们就是默认链接脚本的参数。

当用户没自定义 `linker_script` 时，宏会**按内存大小生成**一份链接脚本：

[rules/coralnpu_v2.bzl:268-297](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/rules/coralnpu_v2.bzl#L268-L297) —— 调用 `generate_linker_script`，模板是 `@coralnpu_hw//toolchain:coralnpu_tcm.ld.tpl`（这份模板在 u2-l1 会精读）。如果内存大小是默认值，脚本名不带后缀；否则带上 `_ITCM%dKB_DTCM%dKB_...` 后缀，便于区分。

宏最后还顺手声明了三个 `filegroup`，让外部可以单独引用 elf/bin/vmem：

[rules/coralnpu_v2.bzl:311-330](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/rules/coralnpu_v2.bzl#L311-L330) —— 所以 `//examples:coralnpu_v2_hello_world_add_floats.elf`、`.bin`、`.vmem` 都是合法标签。

#### 4.4.4 代码实践

1. **实践目标**：亲眼确认 `coralnpu_v2_binary` 产出的三件套。
2. **操作步骤**：
   - 执行 `bazel build //examples:coralnpu_v2_hello_world_add_floats`。
   - 在 `bazel-bin/examples/` 下查找 `coralnpu_v2_hello_world_add_floats.elf`、`.bin`、`.vmem`，以及生成的 `coralnpu_v2_hello_world_add_floats.ld`。
   - 用 `file $(bazel-bin/examples/coralnpu_v2_hello_world_add_floats.elf)` 确认它是 RISC-V ELF。
3. **需要观察的现象**：四个产物都在，`.elf` 应被识别为 `ELF 32-bit LSB executable, UCB RISC-V`。
4. **预期结果**：和 4.4.2 流程图一一对应：源码 → elf → bin → vmem，外加一份生成的 `.ld`。
5. **若无法确定运行结果**：构建依赖完整工具链下载，首次较慢。**待本地验证**。

#### 4.4.5 小练习与答案

- **练习 1**：为什么 `coralnpu_v2_binary` 不直接用内置的 `cc_binary`？
  - **答案**：需要三件内置规则做不到的事——(a) 通过 transition 切到 RISC-V 平台、(b) 注入 ITCM/DTCM 链接脚本、(c) 额外产出 `.bin`/`.vmem` 镜像。
- **练习 2**：把 `coralnpu_v2_binary(itcm_size_kbytes = 16)` 改成 16KB 后，生成的链接脚本文件名会有什么变化？
  - **答案**：因为不再是默认的 8KB，脚本名会带上后缀，形如 `<name>_ITCM16KB_DTCM32KB_STACK128_HEAP_HEAPDTCM.ld`（见 `.bzl:281-282` 的格式化逻辑）。

---

## 5. 综合实践

把本讲四个模块串起来，完成 README 的 Quick Start 三步，并**对照 `.bazelrc` 解释每一步**。这三步的权威出处是 [README.md:31-45](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/README.md#L31-L45)。

```bash
# 第 1 步：跑 cocotb 测试套件（bazel run = build + 立即执行）
bazel run //tests/cocotb:core_mini_axi_sim_cocotb

# 第 2 步：编译一个 CoralNPU C++ 程序
bazel build //examples:coralnpu_v2_hello_world_add_floats

# 第 3 步：构建 Verilator 仿真器（非 RVV 版，编译更快）
bazel build //tests/verilator_sim:core_mini_axi_sim

# 第 4 步（README 附带）：在仿真器上加载并运行刚才编译出的程序
bazel-bin/tests/verilator_sim/core_mini_axi_sim \
  --binary bazel-out/k8-fastbuild-ST-dd8dc713f32d/bin/examples/coralnpu_v2_hello_world_add_floats.elf
```

**你的任务（不只是跑，还要理解）**：

1. **每步对应哪个模块？**
   - 第 1 步 → 用到 4.2 的 `run:` 配置；目标是 cocotb 测试（u2-l4 会详讲）。
   - 第 2 步 → 命中 4.4 的 `coralnpu_v2_binary`，自动 transition 到 RISC-V 平台，产出 elf/bin/vmem。
   - 第 3 步 → 用 4.2 的 `build` 配置；产物是个**宿主机可执行文件**（Verilator 编译出的 C++ 仿真器），所以它**不**走 CoralNPU 平台 transition，而是跑在你 PC 上。
   - 第 4 步 → 把第 2 步的 elf 喂给第 3 步的仿真器。

2. **产物路径里那个奇怪的 `k8-fastbuild-ST-dd8dc713f32d` 是什么？**
   - `k8`：Bazel 对「CPU + OS」的简写（这里指 Linux x86-64）。
   - `fastbuild`：编译模式（另有 `opt`、`dbg`）。
   - `ST-dd8dc713f32d`：一段**配置哈希**。`ST-` 前缀表示这是一段「稳定」配置（与 transition、平台切换、copt 有关）。**注意**：这个哈希是 README 写作时的一次快照，**你本地的哈希很可能不同**，所以第 4 步更稳妥的写法是用 `$(bazel cquery ...)` 或直接 `bazel-bin/examples/` 下的符号链接来定位 elf，而不是照抄 README 的哈希路径。

3. **记录表**（请填）：

| 步骤 | 命令 | 产物路径（本地实际值） | 大致耗时 | 吃到的关键 .bazelrc 行 |
| --- | --- | --- | --- | --- |
| 1 | `bazel run //tests/cocotb:core_mini_axi_sim_cocotb` | （填） | （填） | `common` + `run:*` |
| 2 | `bazel build //examples:coralnpu_v2_hello_world_add_floats` | （填 .elf/.bin/.vmem 路径） | （填） | `build` 行 + 规则自动 transition |
| 3 | `bazel build //tests/verilator_sim:core_mini_axi_sim` | （填可执行文件路径） | （填） | `build` 行 |

4. **若无法运行**：本综合实践需要完整工具链下载、较大磁盘与较长首次编译时间，在受限环境下很可能跑不通。此时请降级为「**源码阅读型综合实践**」：在不实际执行的前提下，对照本讲四节内容，画出「敲下 `bazel build //examples:coralnpu_v2_hello_world_add_floats` 之后，Bazel 读取 `.bazelrc` → 解析 `examples/BUILD.bazel` → 命中 `coralnpu_v2_binary` 宏 → transition 到 `//platforms:coralnpu_v2` → 用 `toolchain_coralnpu_v2` 编译 → 链接生成 `.elf` → objcopy 生成 `.bin` → srec_cat 生成 `.vmem`」的完整时序图。**待本地验证实际运行结果**。

> 第 4 步（在仿真器上运行程序）的具体机制——仿真器如何加载 elf、如何观测内核 halt——是 u2-l3 的主题；这里你只要把「编译产物喂给仿真器」这条链跑通即可。

## 6. 本讲小结

- Bazel 是 CoralNPU 这种「Scala + Verilog + C++ + Python」巨型 monorepo 的统一构建入口；`WORKSPACE` 管外部依赖与工具链、`.bazelrc` 管全局开关、`BUILD.bazel` 管每个包的目标。
- CoralNPU **显式**关闭 bzlmod、坚持 WORKSPACE 模式（`.bazelrc:1-2`），并通过 `http_archive` 在构建时下载 RISC-V 工具链（`WORKSPACE:162-178`）。
- 默认 `.bazelrc` 会**排除** `vcs`/`synthesis`/`power` 目标（它们需要商业 EDA 许可），想跑就用 `--config=vcs` 等（`.bazelrc:42,85-89`）。
- 仓库根的空 `BUILD.bazel` 不是疏忽，而是「注册根包」的约定，使 `//:xxx` 标签可用。
- Quick Start 三条命令背后真正「CoralNPU 专属」的部分，是 `rules/coralnpu_v2.bzl` 里的 `coralnpu_v2_binary` 规则：它通过 **platform transition** 自动切到 RISC-V，注入 ITCM/DTCM 链接脚本，并产出 `.elf/.bin/.vmem` 三件套。
- README 的 Bazel 版本要求（7.4.1）与 `.bazelversion`（8.6.0）存在漂移，实际以 `.bazelversion` 为准。

## 7. 下一步学习建议

本讲把「构建系统骨架」搭好了，你已经能让 Bazel 吐出一个 CoralNPU 程序。但要真正读懂那个 `.elf` 是怎么映射到 ITCM/DTCM 的、以及它复位后第一条指令从哪开始，你需要进入工具链细节：

- **下一讲 u2-l2（编写并编译一个 CoralNPU C++ 程序）**：直接上手写一个最小程序，理解输入/输出缓冲区与 `__attribute__((section(".data")))`。
- **更底层 u2-l1（RISC-V 工具链、CRT 与 TCM 链接脚本）**：精读本讲只是「点到」的 `coralnpu_tcm.ld.tpl`（`coralnpu_v2.bzl:289` 引用的模板）与 CRT 启动代码，把「内存布局」这件事彻底讲透。
- **运行侧 u2-l3（在 Verilator 仿真器上运行程序）**：解释本讲综合实践第 3、4 步用到的 `core_mini_axi_sim` 仿真器内部如何加载并执行 elf。

> 顺着这条线，你就能从「会构建」走到「会运行、会调试」，正式进入 CoralNPU 的软件实践循环。
