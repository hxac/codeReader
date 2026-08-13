# 仓库目录结构与组件关系

## 1. 本讲目标

上一讲（u1-l1）我们已经知道 graph-autofusion 是一个**解耦式**组件集合，含 SuperKernel 和 Autofuse 两个可独立选用的组件。本讲不重复这些定位结论，而是带你「把仓库翻一遍」，学完后你应当能够：

1. 说出顶层每个目录（`autofuse/`、`super_kernel/`、`cmake/`、`docs/`、`scripts/`）的职责，做到「想找某类代码，能直接定位到目录」。
2. 看懂顶层 `CMakeLists.txt` 是如何用 `add_subdirectory` 把两个组件串成一个工程的，并能指出控制 Autofuse 是否编译的那个**开关**。
3. 建立一张属于自己的「目录导航地图」，为后续逐模块精读源码做好准备。

## 2. 前置知识

在开始之前，请确认你理解以下几个概念（上一讲已建立，这里只做一句话回顾）：

- **组件（component）**：一个能独立完成某类加速任务、可单独编译选用的代码集合。本仓库当前有 SuperKernel 与 Autofuse 两个。
- **CMake**：一个跨平台的构建系统生成器。它不直接编译代码，而是根据 `CMakeLists.txt` 文件生成 Makefile 或 ninja 文件，再交给编译器。`add_subdirectory(目录)` 的含义是「把这个子目录也纳入构建」。
- **解耦（decoupling）**：两个组件互不强依赖，可以只编译、只使用其中一个。这一点在源码层面由顶层 `CMakeLists.txt` 的一个开关保证，本讲会精读它。
- **目录树（directory tree）**：用缩进表示的文件夹层级关系，本讲会大量使用它来展示结构。

如果你对 CMake 完全陌生，只需记住一个心智模型：**`CMakeLists.txt` 就是这个工程的「总装配图」，`add_subdirectory` 就是「把这块零件也装上去」**。

## 3. 本讲源码地图

本讲聚焦「导航」，只涉及仓库最顶层的几个文件与目录：

| 文件 / 目录 | 作用 |
|-------------|------|
| `README.md` | 项目整体说明，含一段官方目录结构说明，是我们对照的「标准答案」。 |
| `AGENTS.md` | 仓库工作指南，其中有一张「关键目录」速查表。 |
| `CMakeLists.txt` | 顶层工程总装配图，决定哪些子目录被编译。 |
| `build.sh` | 一键式编译脚本，提供 `--no-autofuse` 等开关，是 CMake 之上的一层封装。 |
| `cmake/` | 被顶层 CMakeLists 反复 `include` 的公共脚本（依赖、打包、函数库）。 |
| `docs/`、`scripts/` | 文档与各类辅助脚本，不参与核心编译链路，但对开发与交付必不可少。 |

> 本讲的所有永久链接基于当前 HEAD：`00627d97bf898d8331ec5189f93a7621294f9121`。

## 4. 核心概念与源码讲解

### 4.1 顶层目录划分：两大组件 + 辅助目录

#### 4.1.1 概念说明

一个规模合理的开源项目，顶层目录通常遵循一个朴素原则：**「谁的功能放谁的目录，公共能力单独成目录」**。graph-autofusion 正是这个结构：

- **两个功能组件**各自独占一个顶层目录：`autofuse/`（自动融合编译器）和 `super_kernel/`（超核融合）。组件自己的源码、测试、文档、示例都**收敛在自己的目录内部**，互不越界。
- **公共支撑目录**服务全局：`cmake/`（构建脚本）、`docs/`（文档）、`scripts/`（辅助脚本）。

这种划分让「解耦」不只是口号，而是落实在目录物理边界上的——你如果想把某个组件搬走或单独编译，它的所有依赖都在它自己的目录树里。

#### 4.1.2 核心流程

仓库根目录的实际布局可以概括为下面这棵树（只展示顶层与关键子目录，`...` 表示省略）：

```text
graph-autofusion/
├── autofuse/          # 组件一：Autofuse 自动融合（源码/测试/文档/示例均在内部）
├── super_kernel/      # 组件二：SuperKernel 超核融合（源码/测试/文档/示例均在内部）
├── cmake/             # 公共 CMake 脚本：依赖、打包、函数库
├── docs/              # 项目文档（中/英/规范/环境安装）
├── scripts/           # 构建、测试、OAT 检查、打包辅助脚本
├── CMakeLists.txt     # 顶层工程总装配图
├── build.sh           # 一键式编译脚本（CMake 之上的封装）
├── version.cmake      # 版本信息
├── README.md          # 项目整体说明（含官方目录结构）
├── AGENTS.md          # 仓库工作指南（含关键目录速查表）
├── CONTRIBUTING.md    # 贡献指南
├── LICENSE / SECURITY.md / OAT.xml ...   # 许可证、安全、开源审查配置
```

两个组件目录内部又各自有清晰的子结构（这些子目录的职责会在后续单元逐个精读，本讲只做命名速览，帮你建立索引）：

- `autofuse/` 内部：`graph_metadef/`（图元数据 IR）、`ascir/`（算子注册）、`optimize/`（优化调度）、`att/`（自动 tiling）、`codegen/`（内核代码生成）、`compiler/`（对外接口）、`ascendc/`（AscendC API 头）、`inc/`（对 GE 的 C++ 接口）、`v35/`（昇腾 950 平台扩展）、`tests/`、`examples/`、`common/`、`tools/` 等。
- `super_kernel/` 内部：`src/jit/`（Python 代码生成）、`src/aot/`（C++ 运行时）、`include/`（公共 C 接口头）、`kernel/`、`examples/`、`tests/`、`docs/` 等。

可以看到，**两个组件都自带 `tests/`、`examples/`、`docs/`**，这正是「组件自包含」的体现。

#### 4.1.3 源码精读

官方在 `README.md` 中给出的目录结构说明，是对照我们上面这棵树最权威的依据：

[README.md:27-45](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/README.md#L27-L45) —— 用一段树状注释说明 `autofuse/`、`super_kernel/`、`cmake/`、`docs/`、`scripts/` 各自的归属，并明确标注 `... # 未来规划的组件`，说明顶层是「按组件扩展」的设计。

`AGENTS.md` 里还有一张更紧凑的「关键目录」速查表，适合日常查找时直接用：

[AGENTS.md:9-18](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/AGENTS.md#L9-L18) —— 用一张表把 `super_kernel/`、`autofuse/`、`cmake/`、`scripts/`、`docs/` 的用途一句话讲清，是本讲目录划分结论的「官方背书」。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是亲手把目录地图建起来。

1. **实践目标**：在不看本讲答案的前提下，独立梳理出仓库目录结构，并区分「组件目录」与「公共目录」。
2. **操作步骤**：
   - 在仓库根目录执行 `ls -1F`（列出顶层条目，`/` 后缀表示目录）。
   - 对 `autofuse/` 与 `super_kernel/` 分别执行 `ls -1F autofuse/` 和 `ls -1F super_kernel/`，查看各自子模块。
   - 用树状缩进把结果画成一张图。
3. **需要观察的现象**：两个组件目录内部都各自出现了 `tests/`、`examples/`、`docs/`（或类似）子目录，而顶层 `cmake/`、`scripts/`、`docs/` 则是全局共享的。
4. **预期结果**：你得到一棵与本讲 4.1.2 节一致的目录树，并发现「组件自包含」这一规律。
5. 待本地验证（若你本地 `ls` 输出与本讲略有出入，以本地实际为准）。

#### 4.1.5 小练习与答案

**练习 1**：如果要给 graph-autofusion 新增第三个融合组件（比如叫 `FastReduce`），按现有目录约定，它的源码、测试、文档应该放在哪里？

> **参考答案**：应该在仓库根目录新建一个 `fast_reduce/`（或类似）顶层目录，把源码、`tests/`、`examples/`、`docs/` 全部收敛在该目录内部，仿照 `autofuse/` 与 `super_kernel/` 的自包含结构。这与 `README.md` 中 `... # 未来规划的组件` 的设计意图一致。

**练习 2**：`docs/` 和 `scripts/` 是为某个具体组件服务的，还是全局共享的？依据是什么？

> **参考答案**：全局共享。依据有两点：一是它们位于顶层、与两个组件目录平级；二是两个组件各自又带有自己的 `docs/`（如 `super_kernel/docs/`）与 `scripts/`，说明顶层 `docs/`、`scripts/` 是跨组件的公共资源。

---

### 4.2 CMake 子目录组织：顶层如何串起两个组件

#### 4.2.1 概念说明

有了目录划分，还要有一张「总装配图」告诉构建系统：**哪些目录要编译、按什么顺序编译**。这张图就是顶层 `CMakeLists.txt`。理解它的关键只有两条规则：

1. `add_subdirectory(目录)` —— 把该子目录纳入构建，相当于「装上这块零件」。
2. `option(名字 "说明" 默认值)` + `if(条件)` —— 定义一个可在命令行覆盖的开关，用来决定某段构建逻辑是否执行。

本节要回答的核心问题是：**SuperKernel 和 Autofuse 在顶层 CMake 里的地位一样吗？** 答案是「不一样」，而这个差异正是「解耦」在构建层面的落点。

#### 4.2.2 核心流程

顶层 `CMakeLists.txt` 的装配流程可以概括为：

```text
1. cmake_minimum_required / project          # 声明最低版本与工程名
2. include(cmake/fetch_cann_cmake.cmake)     # 拉取 CANN 相关 CMake 能力
3. include(cmake/dependencies.cmake)         # 处理依赖
4. set(AUTOFUSE_DIR .../autofuse)            # 记录 autofuse 目录路径
5. include(cmake/function.cmake)             # 引入公共函数库
6. add_subdirectory(super_kernel)            # 【无条件】装上 SuperKernel
7. if autofuse/CMakeLists.txt 存在:
       option(BUILD_AUTOFUSE ... ON)         # 定义开关，默认开
       if(BUILD_AUTOFUSE)
           add_subdirectory(autofuse)        # 【有条件】装上 Autofuse
8. include(cmake/package.cmake)              # 打包逻辑
```

注意第 6 步与第 7 步的对比：

- `super_kernel` 是**无条件**纳入构建的（直接 `add_subdirectory(super_kernel)`）。
- `autofuse` 是**有条件**纳入构建的，受 `BUILD_AUTOFUSE` 开关控制，默认 `ON`，但可以被关闭。

这个不对称恰好印证了上一讲的结论：**关闭 Autofuse 不影响 SuperKernel**——因为前者是后者的一个独立、可插拔的分支。

#### 4.2.3 源码精读

[CMakeLists.txt:11-22](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/CMakeLists.txt#L11-L22) —— 顶部连续 `include` 了 `cmake/` 下的 `fetch_cann_cmake.cmake` 与 `dependencies.cmake`，说明 `cmake/` 目录里存放的是被顶层反复引用的公共脚本（依赖拉取、工具函数、打包），这与 4.1 节的目录划分结论对得上。

[CMakeLists.txt:27-27](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/CMakeLists.txt#L27-L27) —— `set(AUTOFUSE_DIR ${CMAKE_CURRENT_SOURCE_DIR}/autofuse)` 把 autofuse 目录路径记为一个变量，方便后续引用。

[CMakeLists.txt:54-54](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/CMakeLists.txt#L54-L54) —— `add_subdirectory(super_kernel)`，**没有任何条件包裹**，SuperKernel 因此始终参与编译。

[CMakeLists.txt:56-61](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/CMakeLists.txt#L56-L61) —— 这就是本讲最关键的一段：

```cmake
if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/autofuse/CMakeLists.txt")
    option(BUILD_AUTOFUSE "Build autofuse backend modules" ON)
    if(BUILD_AUTOFUSE)
        add_subdirectory(autofuse)
    endif()
endif()
```

它做了三件事：① 检查 `autofuse/CMakeLists.txt` 是否存在（不存在就完全跳过）；② 定义开关 `BUILD_AUTOFUSE`，默认 `ON`；③ 仅当开关为真时才 `add_subdirectory(autofuse)`。把这个开关关掉（设为 `OFF`），Autofuse 就不会编译，而 `super_kernel` 的装配在第 54 行早已独立完成、不受影响。

命令行层面，`build.sh` 暴露了 `--no-autofuse` 来把该开关关掉：

[build.sh:64-64](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L64-L64) —— 帮助文本里写明 `--no-autofuse  Skip autofuse backend build/package artifacts`。

[build.sh:474-477](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L474-L477) —— 在脚本内部，它通过 `extra_option` 追加 `-DBUILD_AUTOFUSE=ON` 或 `-DBUILD_AUTOFUSE=OFF`，最终传递给 CMake，与顶层 `CMakeLists.txt` 的 `option(BUILD_AUTOFUSE ...)` 完成对接。这就是「`--no-autofuse` ⇄ `BUILD_AUTOFUSE=OFF` ⇄ 跳过 `add_subdirectory(autofuse)`」的完整链路。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证「关闭 Autofuse 后 SuperKernel 仍会被装配」。
2. **操作步骤**：
   - 阅读 [CMakeLists.txt:54-61](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/CMakeLists.txt#L54-L61)，确认 `super_kernel` 的 `add_subdirectory` 在 `BUILD_AUTOFUSE` 的 `if` 块**之外**。
   - 在 [build.sh:474-477](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L474-L477) 中找到 `--no-autofuse` 是如何被翻译成 `-DBUILD_AUTOFUSE=OFF` 的。
   - （可选，待本地验证）执行 `sh build.sh --pkg --no-autofuse -j 8`，观察构建产物里是否还出现 autofuse 相关目标。
3. **需要观察的现象**：即使带上了 `--no-autofuse`，`super_kernel` 对应的目标仍会被构建。
4. **预期结果**：你能在脑中画出 `--no-autofuse → BUILD_AUTOFUSE=OFF → 跳过 autofuse，保留 super_kernel` 的因果链。
5. 若无法实际运行构建，明确标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `add_subdirectory(super_kernel)` 没有放在任何 `if` 里，而 `add_subdirectory(autofuse)` 却包了两层条件？

> **参考答案**：因为 SuperKernel 是仓库最早开源、默认必装的基础组件，所以无条件装配；而 Autofuse 是后开源、可插拔的组件，需要先用 `EXISTS` 防止「目录/构建文件缺失」导致构建失败，再用 `BUILD_AUTOFUSE` 让用户能选择是否启用。两层条件分别防御「文件不存在」和「用户主动关闭」两种情况。

**练习 2**：如果不使用 `build.sh`，而是直接调用 `cmake`，想关闭 Autofuse 该怎么传参？

> **参考答案**：在 cmake 配置阶段传 `-DBUILD_AUTOFUSE=OFF`，例如 `cmake -B build -DBUILD_AUTOFUSE=OFF ...`。这正好对应 `build.sh` 内部追加的那个变量。

---

### 4.3 docs / scripts / cmake 辅助目录

#### 4.3.1 概念说明

除了两个组件，仓库还有三个「辅助目录」服务于整个工程的生命周期：构建（`cmake/`）、文档（`docs/`）、脚本（`scripts/`）。它们本身不实现融合加速逻辑，但没有它们，工程就无法编译、无法交付、无法被他人理解和使用。把它们单独成目录、与组件解耦，是大型工程的基本素养。

#### 4.3.2 核心流程

三个目录的内部组织各有侧重：

```text
cmake/                 # 构建期被顶层 CMakeLists 引用的公共脚本
├── fetch_cann_cmake.cmake   # 拉取 CANN 的 CMake 能力
├── dependencies.cmake       # 依赖声明与处理
├── function.cmake           # 公共 CMake 函数库
└── package.cmake            # 打包（run/rpm/deb）逻辑

docs/                  # 全局文档（中英双语 + 规范 + 环境安装）
├── zh/                # 中文文档：build.md / quick_install.md / autofuse/ / super_kernel/ ...
├── en/                # 英文文档
├── guidelines/        # 规范：cross_feature_check.md / design_document_template.md / 编码红线.md
├── env_install/       # 环境安装相关
└── figures/           # 文档配图

scripts/               # 辅助脚本（构建/测试/检查/打包）
├── check_env.sh / init_env.sh   # 环境检查与初始化
├── oat_check.sh                  # OAT 开源审查
├── env_install/                  # pytorch / tensorflow 环境安装脚本
├── package/                      # 打包脚本（graph_autofusion.xml、rpm/deb 定制脚本）
└── test/                         # 测试辅助
```

它们的共同点是：**被顶层或 `build.sh` 调用，而不直接包含算子/融合的实现代码**。例如 `cmake/*.cmake` 是被顶层 `CMakeLists.txt` 用 `include(...)` 引入的；`scripts/package/` 则服务于 `build.sh --pkg` 的打包产物。

#### 4.3.3 源码精读

`cmake/` 目录如何被顶层引用，已经在 4.2.3 节看到：顶层 `CMakeLists.txt` 用 `include(cmake/...)` 把这些公共脚本拼装进来。

[CMakeLists.txt:38-39](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/CMakeLists.txt#L38-L39) —— 引入 `cmake/function.cmake`（公共函数库）与 `version.cmake`（版本信息），随后调用 `check_cann_pkg_build_deps`、`add_cann_version_info_targets` 等来自这些脚本的能力。

[CMakeLists.txt:63-63](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/CMakeLists.txt#L63-L63) —— `include(cmake/package.cmake)`，在最末尾引入打包逻辑，对应 `build.sh --pkg` 产出的 run/rpm/deb 包，背后正是 `scripts/package/` 里的配置与定制脚本。

至于 `docs/`，它的价值体现在「找文档」时：中文文档入口在 `docs/zh/`，其中 `docs/zh/build.md` 是构建说明、`docs/zh/quick_install.md` 是快速安装、`docs/guidelines/` 下则有编码红线与设计文档模板（这些会在后续 u1-l3、u12 等讲义用到）。`AGENTS.md` 也明确把 `docs/` 归为「构建、设计、规范和组件说明文档」：

[AGENTS.md:9-18](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/AGENTS.md#L9-L18) —— 速查表里 `cmake/`、`scripts/`、`docs/` 三行分别被概括为「CMake 公共脚本、依赖和打包逻辑」「构建、测试、OAT 检查等脚本」「构建、设计、规范和组件说明文档」，与本节梳理完全吻合。

#### 4.3.4 代码实践

1. **实践目标**：验证「`cmake/` 是构建期公共脚本，`scripts/` 是运行/打包辅助脚本」这一分工。
2. **操作步骤**：
   - 在仓库根目录执行 `ls -1F cmake/` 与 `ls -1F scripts/`。
   - 用 `grep` 在顶层 `CMakeLists.txt` 中搜索 `include(cmake/`，确认 `cmake/` 下每个 `.cmake` 文件都被引用了哪些（例如 `dependencies.cmake`、`function.cmake`、`package.cmake`）。
   - 查看 `scripts/package/` 下是否存在 `graph_autofusion.xml` 这类打包描述文件。
3. **需要观察的现象**：`cmake/` 里的文件都以 `include` 形式出现在 `CMakeLists.txt` 中；`scripts/` 里的内容则更偏「可执行脚本 + 配置文件」，且与打包（package）强相关。
4. **预期结果**：你能用一句话区分 `cmake/` 与 `scripts/` —— 前者是「构建系统内部用的公共脚本」，后者是「围绕构建/测试/打包/检查的运维型脚本」。
5. 待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：如果你要新增一条「打包成 deb 包」的定制安装逻辑，应该改 `cmake/` 还是 `scripts/`？为什么？

> **参考答案**：主要改 `scripts/`。因为 `scripts/package/graph_autofusion/rpm_deb/` 下已经存放了 `custom_postinst.sh`、`custom_prerm.sh` 这类针对 rpm/deb 的定制脚本，这正是放置安装期定制逻辑的地方；`cmake/package.cmake` 则负责在构建期把这些脚本编排进打包流程。两者配合，但「定制逻辑本体」在 `scripts/`。

**练习 2**：中文用户想找「构建说明」和「编码红线」，分别去哪个目录？

> **参考答案**：构建说明去 `docs/zh/build.md`；编码红线去 `docs/guidelines/编码红线.md`（`guidelines/` 是跨语言的规范目录，未按 `zh/en` 分）。

---

## 5. 综合实践

把本讲三个模块串起来，完成一张「仓库导航地图」：

1. **画目录树**：在仓库根目录用 `ls -1F` 及对各子目录的列举，手画一棵两层目录树，标注：
   - 哪些是**组件目录**（`autofuse/`、`super_kernel/`），并各列 2 个有代表性的子模块（例如 Autofuse 的 `codegen/`、`optimize/`；SuperKernel 的 `src/jit/`、`src/aot/`）。
   - 哪些是**公共/辅助目录**（`cmake/`、`docs/`、`scripts/`）。
2. **标装配关系**：在树旁边补一段文字，说明顶层 `CMakeLists.txt` 中：
   - `add_subdirectory(super_kernel)` 是无条件的（引用 [CMakeLists.txt:54](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/CMakeLists.txt#L54-L54)）；
   - `add_subdirectory(autofuse)` 受 `BUILD_AUTOFUSE` 控制（引用 [CMakeLists.txt:56-61](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/CMakeLists.txt#L56-L61)）。
3. **写一条命令**：给出一条「只构建 SuperKernel、跳过 Autofuse」的 `build.sh` 命令，并解释它如何通过 `-DBUILD_AUTOFUSE=OFF` 影响顶层 CMake。

完成这张地图后，你就具备了「按目录找代码、按 CMake 理解装配关系」的能力，这正是后续逐模块精读源码的前置导航技能。

## 6. 本讲小结

- 仓库顶层遵循「**功能组件独占目录、公共能力单独成目录**」的原则：`autofuse/` 与 `super_kernel/` 是两个自包含组件（各自带 `tests/`、`examples/`、`docs/`），`cmake/`、`docs/`、`scripts/` 是全局辅助目录。
- 顶层 `CMakeLists.txt` 是工程「总装配图」：它用 `add_subdirectory` 装配子目录，并用 `include(cmake/*.cmake)` 引入公共构建脚本。
- 两个组件在构建中**地位不对称**：`super_kernel` 无条件装配；`autofuse` 受 `BUILD_AUTOFUSE` 开关（默认 `ON`）控制，这正是「解耦」在构建层面的落点。
- `build.sh --no-autofuse` 通过向 CMake 传 `-DBUILD_AUTOFUSE=OFF` 关闭 Autofuse，且不影响 SuperKernel。
- `cmake/` 服务于构建系统内部，`scripts/` 服务于构建/测试/打包/检查的运维流程，`docs/`（含 `zh/`、`en/`、`guidelines/`）承载全部文档与规范。

## 7. 下一步学习建议

本讲建立的是「静态地图」。接下来建议：

- **横向（构建实操）**：进入 **u1-l3「一键构建系统 build.sh 与 CMake 工程」**，把本讲的目录与开关真正跑起来，理解 `build.sh` 的 `--pkg`、`-u/-s`、`--module`、`-j` 等选项如何映射到这些目录。
- **纵向（组件入口）**：如果你想先看某个组件，可以直接进入 **u2（SuperKernel 组件入门）** 或 **u3（Autofuse 入门与端到端体验）**，它们会从各自的 `README.md` 与示例入手。
- **延伸阅读**：在动手编译前，可先浏览 [docs/zh/build.md](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/docs/zh/build.md)，它与本讲的目录结构相互印证。
