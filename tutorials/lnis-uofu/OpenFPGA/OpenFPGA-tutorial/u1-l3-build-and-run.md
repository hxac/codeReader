# 从源码编译与运行环境搭建

## 1. 本讲目标

本讲是「动手」的第一课。读完上一讲你已经知道仓库长什么样、哪些目录参与编译，但还没有真正把 `openfpga` 这个可执行程序跑起来。本讲带你走完从「一个刚 clone 下来的空仓库」到「敲下 `openfpga --version` 看到版本号」的完整链路。

学完本讲你应当能够：

1. 说清楚为什么 clone 之后仓库里好几个目录是空的，以及如何用一条命令把它们填满（子模块 checkout）。
2. 看懂 OpenFPGA 的两层构建结构：外层 **Makefile** 是给人用的快捷封装，内层 **CMake** 才是真正的构建系统。
3. 掌握 `make checkout` / `make compile` / `make all` 三个核心目标，以及 `BUILD_TYPE`、`CMAKE_FLAGS`、`cmake_goals` 等可调参数。
4. 理解 `CMakeLists.txt` 里那些 `OPENFPGA_WITH_*` 选项（如 `OPENFPGA_WITH_INSTALLER`、`OPENFPGA_WITH_YOSYS`）控制了什么。
5. 学会用 `source openfpga.sh` 配置 `OPENFPGA_PATH` 等环境变量，为下一讲跑第一个设计流做准备。

## 2. 前置知识

在开始之前，先建立三个直觉。

**第一，OpenFPGA 不是「一个程序」，而是「一套工具」的集合。** 它的核心引擎自己用 C++ 写，但它依赖两个重量级第三方项目：负责逻辑综合的 **Yosys**，以及负责布局布线的 **VPR**（verilog-to-routing）。这两个项目以 **git 子模块（git submodule）** 的形式挂在本仓库里。git 子模块的特点是：`git clone` 主仓库时，子模块目录**默认是空的**——只记录了「指向哪个远程仓库的哪一次提交」，并没有把代码拉下来。所以编译之前必须先「填满」它们。

**第二，现代 C++ 项目的构建通常是两层的。** 最内层是 **CMake**：它读取 `CMakeLists.txt`，根据你的编译器、操作系统、选项，生成一堆真正的 Makefile（或 Ninja 等工程文件）。外层往往再包一层 **Makefile** 或脚本，把常用的 CMake 调用封装成 `make xxx` 这样的短命令。OpenFPGA 就是这种结构：`Makefile` → `CMake` → 真正的编译。你绝大多数时候只跟最外层 Makefile 打交道。

**第三，「构建类型」决定编译器优化程度。** `release`（默认）会开满优化、跑得快，适合日常使用；`debug` 不优化、带调试符号（`-O0 -g3`），适合用 gdb 排错。这是 CMake 的标准概念，OpenFPGA 只是把它暴露成了 Makefile 变量。

> 承接上一讲：你已经知道顶层 `CMakeLists.txt` 用三条 `add_subdirectory`（vtr → libs → openfpga）确定了编译顺序，版本号单一数据源是 `VERSION.md`（当前 `1.2.4307`）。本讲要回答的是：**怎么把这套东西真正编出来**。

## 3. 本讲源码地图

本讲涉及的关键文件都在仓库根目录，没有进入 `openfpga/src` 的业务逻辑：

| 文件 | 作用 | 本讲用它做什么 |
|------|------|----------------|
| `Makefile` | 外层构建封装，给用户提供 `checkout`/`compile`/`all` 等短命令 | 拆解每个 make 目标背后跑了什么 |
| `CMakeLists.txt` | 顶层 CMake 配置，定义项目、选项、编译顺序 | 理解构建选项与子项目组织 |
| `.gitmodules` | 声明三个 git 子模块（yosys、vtr、yosys-slang） | 解释为什么 clone 后目录是空的 |
| `openfpga.sh` | 运行环境脚本，定义 `OPENFPGA_PATH` 等变量与一堆 bash 快捷函数 | 配置运行环境、为跑流程做准备 |
| `Dockerfile` | 官方容器镜像定义，体现「推荐运行环境」 | 作为依赖与运行方式的参考 |
| `.github/workflows/build.yml` | CI 配置，是「官方是怎么编译的」最权威示范 | 对照真实构建命令验证我们的理解 |

## 4. 核心概念与源码讲解

本讲按真实操作顺序拆成四个最小模块：**先 checkout 子模块 → 再 make 编译（背后是 CMake）→ 最后 source 环境脚本**。

### 4.1 子模块 checkout：填满空仓库

#### 4.1.1 概念说明

git 子模块（submodule）让你在一个 git 仓库里嵌入另一个 git 仓库。主仓库只保存子模块的「远程地址 + 指定的 commit 号」，不保存子模块的文件内容。所以当你 `git clone` OpenFPGA 主仓库后，会看到 `yosys/`、`vtr-verilog-to-routing/`、`yosys-slang/` 这几个目录**存在但几乎为空**——它们需要单独「拉取」才能填满。

这一步在 OpenFPGA 文档和脚本里被称为 **checkout**。它是一次性的：只要子模块已经填满，后续重新编译不必再 checkout（除非你改了子模块指向的 commit）。

#### 4.1.2 核心流程

填满子模块的标准两步是：

```
git submodule init         # 把 .gitmodules 里的注册信息写入 .git/config
git submodule update --init --recursive   # 真正下载每个子模块到指定 commit；递归处理嵌套子模块
```

`--recursive` 很重要：VPR 自己也有子模块（比如 ABC），不加它会缺料。OpenFPGA 把这两条命令封装进了 Makefile 的 `checkout` 目标，所以你通常不用手敲，`make checkout` 一条命令即可。

#### 4.1.3 源码精读

先看三个子模块都挂在哪。[.gitmodules](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/.gitmodules) 声明了它们：

```ini
[submodule "yosys"]
    path = yosys
    url = https://github.com/YosysHQ/yosys
[submodule "vtr-verilog-to-routing"]
    path = vtr-verilog-to-routing
    url = https://github.com/verilog-to-routing/vtr-verilog-to-routing.git
[submodule "yosys-slang"]
    path = yosys-slang
    url = https://github.com/povik/yosys-slang.git
```

可以看到 `yosys`（综合器）、`vtr-verilog-to-routing`（布局布线 VPR）、`yosys-slang`（SystemVerilog 前端插件）都是外部仓库。

再看 Makefile 如何封装 checkout。[Makefile:71-74](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/Makefile#L71-L74) 就是上面说的两条 git 命令：

```makefile
checkout: 
# Update all the submodules
    git submodule init
    git submodule update --init --recursive
```

> 注意：CI 里也能看到同样的动作。[.github/workflows/build.yml:177](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/.github/workflows/build.yml#L177) 在编译前第一步就是 `make checkout`，说明这是构建的绝对前置条件。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 checkout 前后子模块目录从「空」变「满」。

**操作步骤**：

1. 在仓库根目录查看子模块目录的体积（checkout 前）：
   ```bash
   du -sh yosys vtr-verilog-to-routing yosys-slang
   ```
2. 执行 checkout：
   ```bash
   make checkout
   ```
3. 再次查看体积（checkout 后）。

**需要观察的现象**：第一步通常只有几十 KB（几乎是空目录），`make checkout` 会从网络下载三个子模块及其嵌套子模块（这一步耗时较长，取决于网络，可能十几分钟到更久），第三步 `vtr-verilog-to-routing` 应该有几百 MB，`yosys` 也有上百 MB。

**预期结果**：三个目录都被真实源码填满，例如 `vtr-verilog-to-routing/` 下能看到 `vpr/`、`abc/` 等。

> 如果无法确定本地网络能否拉取，明确写「待本地验证」。拉取失败最常见原因是网络无法访问 GitHub，可配置代理或使用镜像后重试。

#### 4.1.5 小练习与答案

**练习 1**：如果不加 `--recursive`，可能会出现什么问题？

**参考答案**：VPR 内部还嵌套了它自己的子模块（如 ABC 综合库）。不加 `--recursive`，这些嵌套子模块不会被拉取，后续编译 VPR 时会因找不到 ABC 源码而失败。

**练习 2**：为什么 `git clone` 主仓库时子模块目录默认是空的，而不是自动拉取？

**参考答案**：这是 git 子模块的设计取舍——自动拉取会显著拖慢 clone 速度，且子模块可能很大。git 选择只记录「地址 + commit」，把是否拉取的决定权交给用户。

---

### 4.2 Makefile 构建系统：面向用户的快捷封装

#### 4.2.1 概念说明

OpenFPGA 根目录的 `Makefile` **不是**真正的编译规则集合，而是一层薄薄的封装：它的每个目标（target）基本都是在「调用 cmake」或「调用 cmake 生成的 make」。这样设计的好处是用户不用记一长串 cmake 参数，只需 `make compile` 即可。

理解这层封装的关键变量有三个：

- `BUILD_TYPE`：构建类型，默认 `release`。
- `CMAKE_FLAGS`：传给 cmake 的额外参数，比如换编译器、开关某个特性。
- `CMAKE_GOALS`：要编译的 cmake 目标，默认 `all`（全部），可以缩到只编 `openfpga`。

#### 4.2.2 核心流程

完整的一次「配置 + 编译」分两步，被封装成两个 Makefile 目标：

```
make prebuild     # 在 build/ 目录里跑 cmake，生成真正的 Makefile（配置阶段）
make compile      # 进入 build/ 跑 make，真正编译（编译阶段）
```

而 `make all` 则是「先 checkout 子模块，再 compile」的串行快捷方式：

```
make all == make checkout  +  make compile
```

数据流可以这样理解（伪代码）：

```
make compile
  └─ 依赖 prebuild
        └─ mkdir -p build
        └─ cd build && cmake <CMAKE_FLAGS> <源码目录>     # 生成 build/Makefile
  └─ make -C build <CMAKE_GOALS>                          # 真正编译，产出二进制
```

#### 4.2.3 源码精读

**① 默认值与变量组装**。[Makefile:17](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/Makefile#L17) 设默认构建类型为 release：

```makefile
BUILD_TYPE ?= release
```

[Makefile:44-46](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/Makefile#L44-L46) 定义构建目录与默认目标：

```makefile
BUILD_DIR ?= build
CMAKE_GOALS = all
INSTALLER_TYPE=STGZ
```

最关键的一行是 [Makefile:36](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/Makefile#L36)，它把构建类型、内部标志、用户传入的 `CMAKE_FLAGS` 拼成最终传给 cmake 的参数（`override` 保证用户值不会覆盖这层组装逻辑，而是被追加在最后）：

```makefile
override CMAKE_FLAGS := -G 'Unix Makefiles' -DCMAKE_BUILD_TYPE=${BUILD_TYPE} \
                        ${INTERNAL_CMAKE_FLAGS} ${CMAKE_FLAGS}
```

其中 `INTERNAL_CMAKE_FLAGS` 由 [Makefile:23-32](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/Makefile#L23-L32) 拼出，包含两个开关：

```makefile
INTERNAL_CMAKE_FLAGS += -DOPENFPGA_WITH_INSTALLER=${BUILD_INSTALLER}
...
INTERNAL_CMAKE_FLAGS += -DOPENFPGA_INSTALL_DOC=${INSTALL_DOC}
```

这正是「Makefile 变量 → CMake 选项」的桥接点。

**② prebuild：配置阶段**。[Makefile:76-80](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/Makefile#L76-L80) 创建 build 目录并调用 cmake：

```makefile
prebuild:
    @mkdir -p ${BUILD_DIR} && \
    echo "cd ${BUILD_DIR} && ${CMAKE_COMMAND} ${CMAKE_FLAGS} ${SOURCE_DIR}" && \
    cd ${BUILD_DIR} && ${CMAKE_COMMAND} ${CMAKE_FLAGS} ${SOURCE_DIR}
```

注意 `cd ${BUILD_DIR} && cmake ... ${SOURCE_DIR}`——这是「带外构建（out-of-source build）」，编译产物全落在 `build/` 里，不污染源码树。

**③ compile：编译阶段**。[Makefile:82-89](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/Makefile#L82-L89)，注意它用 `| prebuild` 表示「先跑 prebuild」这种**顺序依赖**：

```makefile
compile: | prebuild
    echo "Building target(s): ${CMAKE_GOALS}"
    @+${MAKE} -C ${BUILD_DIR} ${CMAKE_GOALS}
```

`${MAKE} -C build` 就是进入 build 目录调用 cmake 生成的那个 Makefile。

**④ all：一键串行**。[Makefile:95-97](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/Makefile#L95-L97)：

```makefile
all: checkout
# A shortcut command to run checkout and compile in serial
    @+${MAKE} compile
```

所以 `make all` = checkout + compile，是「全新机器上最省事的一条命令」。CI 里也大量使用 `make all`（见 [.github/workflows/build.yml:302](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/.github/workflows/build.yml#L302)、[L644](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/.github/workflows/build.yml#L644) 等多处 `make all BUILD_TYPE=...`）。

**⑤ 其它实用目标**：[Makefile:91-93](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/Makefile#L91-L93) 的 `list_cmake_targets` 可列出所有可编译的 cmake 目标；[Makefile:132-134](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/Makefile#L132-L134) 的 `clean` 删除 `build/` 和 `yosys/install`；格式化目标 `format-cpp`/`format-xml`/`format-py`（[Makefile:108-129](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/Makefile#L108-L129)）会在后续讲义里用到。

#### 4.2.4 代码实践

**实践目标**：用最少的命令把 `openfpga` 二进制编出来，并理解 `cmake_goals` 如何缩小编译范围。

**操作步骤**：

1. （若上一节已 checkout 可跳过）`make checkout`
2. 只编译 `openfpga` 这一个目标，节省时间：
   ```bash
   make compile cmake_goals=openfpga
   ```
   > 全量 `make compile`（默认 `cmake_goals=all`）会连 VPR、Yosys 一起编，耗时极长。开发期只验证 `openfpga` 本体时，缩到 `openfpga` 一个目标最快。
3. 确认产物：
   ```bash
   ls -la build/openfpga/openfpga
   ```
   > 产物路径说明：顶层 CMake 把 `openfpga/` 作为子工程（`add_subdirectory(openfpga)`），其 `add_executable(openfpga ...)`（[openfpga/CMakeLists.txt:53](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/CMakeLists.txt#L53)）产出的二进制就落在 `build/openfpga/openfpga`。

**需要观察的现象**：第二步会先看到一段 `cd build && cmake ...` 的输出（配置阶段，因为 `compile` 依赖 `prebuild`），随后是真正的编译输出。

**预期结果**：`build/openfpga/openfpga` 这个可执行文件存在。

> 该实践依赖本机已装好编译器（GCC/Clang，支持 C++20）、cmake、Python3 等依赖；若缺少依赖会报错，明确写「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`make compile` 和 `make all` 的区别是什么？什么场景下用哪个？

**参考答案**：`make all` = `make checkout` + `make compile`，会先拉子模块再编译，适合全新环境首次构建；`make compile` 只编译不 checkout，适合子模块已就绪、只想改代码后增量编译的场景。

**练习 2**：为什么 `compile` 目标用 `| prebuild`（顺序依赖）而不是把 prebuild 的命令直接抄进 compile？

**参考答案**：用顺序依赖既保证「编译前必先配置」，又让两个步骤各自独立可调用——你可以单独 `make prebuild` 只做配置（比如想检查 cmake 选项是否正确），而不触发编译。

---

### 4.3 CMake 配置：构建系统的真正内核

#### 4.3.1 概念说明

`Makefile` 只是传话筒，真正决定「编什么、怎么编」的是顶层 [CMakeLists.txt](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/CMakeLists.txt)。它做四件事：

1. **定义工程**：`project("OpenFPGA-tool-suites" ...)`——这是承接上一讲提到的顶层工程名。
2. **设默认构建类型**：未指定时默认 `Release`。
3. **暴露一批 `OPENFPGA_WITH_*` 选项**：让用户开关 Yosys、SWIG、安装器、测试等特性。
4. **按顺序纳入子工程**：`add_subdirectory(vtr)` → `add_subdirectory(libs)` → `add_subdirectory(openfpga)`，外加一个特殊的 `yosys` 自定义目标。

#### 4.3.2 核心流程

CMake 配置阶段的关键决策流（伪代码）：

```
读 VERSION.md → 解析版本号
if 未指定 CMAKE_BUILD_TYPE: 默认 Release
检查 IPO（过程间优化）是否支持 → 决定是否开 LTO
设置 C++20 标准
add_subdirectory(vtr-verilog-to-routing)   # 先编 VPR（含 ABC）
add_subdirectory(libs)                     # 再编支撑库
add_subdirectory(openfpga)                 # 最后编核心引擎
if OPENFPGA_WITH_YOSYS: 构建 yosys 自定义目标
```

版本号解析值得一提：CMake 直接读 `VERSION.md` 文件内容并按 `.` 拆分，呼应上一讲「`VERSION.md` 是版本号单一数据源」。

#### 4.3.3 源码精读

**① 默认构建类型**。[CMakeLists.txt:20-25](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/CMakeLists.txt#L20-L25) 在用户没指定时强制设为 Release：

```cmake
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release CACHE STRING
        "Choose the type of build: None, Debug, Release, RelWithDebInfo, MinSizeRel"
        FORCE)
endif()
```

> Makefile 侧 `BUILD_TYPE=release` 会通过 `-DCMAKE_BUILD_TYPE` 传进来，所以两者是一致的。

**② 版本号来源**。[CMakeLists.txt:70-74](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/CMakeLists.txt#L70-L74) 从 `VERSION.md` 读取并拆成主/次/修订号：

```cmake
file (STRINGS "VERSION.md" VERSION_NUMBER)
string (REPLACE "." ";" VERSION_LIST ${VERSION_NUMBER})
list(GET VERSION_LIST 0 OPENFPGA_VERSION_MAJOR)
list(GET VERSION_LIST 1 OPENFPGA_VERSION_MINOR)
list(GET VERSION_LIST 2 OPENFPGA_VERSION_PATCH)
```

**③ 核心构建选项**。[CMakeLists.txt:86-96](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/CMakeLists.txt#L86-L96) 定义了一组 `option()`，每个都是布尔开关：

```cmake
option(OPENFPGA_WITH_YOSYS "Enable building Yosys" ON)
option(OPENFPGA_WITH_SLANG "Enable building Yosys Slang plugin" ON)
option(OPENFPGA_WITH_TEST "Enable testing build ..." ON)
option(OPENFPGA_WITH_SWIG "Enable SWIG interface ... Tcl/Python" ON)
...
option(OPENFPGA_WITH_INSTALLER "Enable installer to be built" ON)
option(OPENFPGA_INSTALL_DOC "Installer will include documentation" ON)
```

这些就是学习目标里说的「`OPENFPGA_WITH_INSTALLER` 等构建选项」。注意 Makefile 的 `BUILD_INSTALLER` 变量通过 `-DOPENFPGA_WITH_INSTALLER=...` 正好对应到这里。如果你想关掉 Yosys 编译以大幅缩短构建时间，可以 `make compile CMAKE_FLAGS="-DOPENFPGA_WITH_YOSYS=OFF"`。

**④ C++20 与优化**。[CMakeLists.txt:166-168](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/CMakeLists.txt#L166-L168) 要求 C++20：

```cmake
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
```

IPO（链接期优化/LTO）由 [CMakeLists.txt:175-193](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/CMakeLists.txt#L175-L193) 控制，默认 `auto`：release 时自动开、debug 时关，且在 MSYS2 下强制关以避开链接器冲突（[L43-46](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/CMakeLists.txt#L43-L46)）。

**⑤ 编译顺序**。[CMakeLists.txt:316](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/CMakeLists.txt#L316)、[L331-332](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/CMakeLists.txt#L331-L332) 是顺序的权威来源：

```cmake
add_subdirectory(vtr-verilog-to-routing)   # 先
...
add_subdirectory(libs)                      # 中
add_subdirectory(openfpga)                  # 后
```

Yosys 因为还没完全 CMake 化，用自定义目标单独构建，见 [CMakeLists.txt:404-416](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/CMakeLists.txt#L404-L416)（在 yosys 目录里调用它自己的 Makefile）。

**⑥ 安装目标**。编出来的二进制可通过 `install` 安装。[openfpga/CMakeLists.txt:80-83](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/CMakeLists.txt#L80-L83) 把 `openfpga` 安装到 `bin/`：

```cmake
install(TARGETS libopenfpga openfpga
        DESTINATION bin
        COMPONENT openfpga_package)
```

#### 4.3.4 代码实践

**实践目标**：通过开关一个 CMake 选项，直观感受它对构建范围的影响。

**操作步骤**：

1. 列出所有可编译目标，确认 `openfpga` 在其中：
   ```bash
   make list_cmake_targets
   ```
2. 先做一次干净配置并查看关键状态信息。删掉旧 build 后重新 prebuild，在输出里找 `CMAKE_BUILD_TYPE`、`Building with IPO`、`Readline feature mode` 等状态行：
   ```bash
   make clean
   make prebuild 2>&1 | grep -E "CMAKE_BUILD_TYPE|IPO|Readline"
   ```

**需要观察的现象**：第 2 步会打印类似 `CMAKE_BUILD_TYPE: Release`、`Building with IPO: on (auto)`、`Readline feature mode: libreadline` 的状态行——这些正是 [CMakeLists.txt:25](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/CMakeLists.txt#L25)、[L189](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/CMakeLists.txt#L189)、[L108](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/CMakeLists.txt#L108) 那几条 `message(STATUS ...)` 打印出来的。

**预期结果**：看到上述状态行，证明 CMake 配置阶段确实按 `CMakeLists.txt` 的逻辑执行了。

> 「待本地验证」——具体输出文本以本地 cmake 版本和编译器为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么构建顺序必须是 VPR → libs → openfpga，不能反过来？

**参考答案**：因为依赖方向是「openfpga 依赖 libs 和 VPR，libs 部分依赖 VPR」。CMake 的 `add_subdirectory` 顺序需保证被依赖者先配置好（target 先定义），否则后续 `target_link_libraries` 会找不到链接对象。

**练习 2**：想把编译时间砍到最短，只验证 `openfpga` 引擎本身的改动，应该怎么构建？

**参考答案**：关掉 Yosys 与安装器、只编 openfpga 目标：`make compile cmake_goals=openfpga CMAKE_FLAGS="-DOPENFPGA_WITH_YOSYS=OFF -DOPENFPGA_WITH_INSTALLER=OFF"`（前提是你不需要综合步骤，且 VPR 已编过）。

---

### 4.4 openfpga.sh 环境脚本：配置运行时环境

#### 4.4.1 概念说明

编出二进制只是第一步。OpenFPGA 的流程脚本（Python 写的 `run_fpga_task.py` 等）需要知道「OpenFPGA 装在哪、脚本在哪、任务在哪」，这些靠环境变量传递。[openfpga.sh](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga.sh) 就是一个**用 `source` 加载的 bash 脚本**：它不是可执行程序，而是定义了一堆环境变量和 bash 函数，加载后你就在当前 shell 里多了一组快捷命令（如 `run-task`、`goto-task`、`list-tasks`）。

> 为什么必须用 `source openfpga.sh` 而不是 `bash openfpga.sh`？因为 `source`（即 `.`）会在**当前 shell** 里执行脚本，`export` 的变量和定义的函数才会在执行后保留；用 `bash openfpga.sh` 会在子 shell 执行，脚本一退出变量和函数就没了。

#### 4.4.2 核心流程

加载后的环境配置与快捷命令（伪代码）：

```
source openfpga.sh
  ├─ 若 OPENFPGA_PATH 未设 → 设为当前目录 $(pwd)
  ├─ export OPENFPGA_SCRIPT_PATH = $OPENFPGA_PATH/openfpga_flow/scripts
  ├─ export OPENFPGA_TASK_PATH   = $OPENFPGA_PATH/openfpga_flow/tasks
  └─ 定义函数：run-task / run-flow / goto-task / list-tasks / ...
         ↓
run-task <任务名>   → 调用 $OPENFPGA_SCRIPT_PATH/run_fpga_task.py
goto-task <任务名>  → cd 到对应 run 结果目录
```

#### 4.4.3 源码精读

**① 设置核心路径变量**。[openfpga.sh:8-17](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga.sh#L8-L17)：

```bash
if [ -z $OPENFPGA_PATH ]; then
    echo "OPENFPGA_PATH variable not found"
    export OPENFPGA_PATH=$(pwd);
    echo "Setting OPENFPGA_PATH=${OPENFPGA_PATH}"
else
    echo "OPENFPGA_PATH=${OPENFPGA_PATH}"
fi
export OPENFPGA_SCRIPT_PATH="${OPENFPGA_PATH}/openfpga_flow/scripts"
export OPENFPGA_TASK_PATH="${OPENFPGA_PATH}/openfpga_flow/tasks"
if [ -z $PYTHON_EXEC ]; then export PYTHON_EXEC="python3"; fi
```

含义：如果你事先没设 `OPENFPGA_PATH`，它就把「执行 source 时所在的目录」当作 OpenFPGA 根目录。**所以一定要在仓库根目录下 source**，否则路径会指错。

**② 三组最常用的快捷函数**：

- `run-task`：跑一个批量任务。[openfpga.sh:66-68](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga.sh#L66-L68)：
  ```bash
  run-task () {
      $PYTHON_EXEC $OPENFPGA_SCRIPT_PATH/run_fpga_task.py "$@"
  }
  ```
- `run-flow`：跑一次单流程。[openfpga.sh:82-84](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga.sh#L82-L84) 调用的是 `run_fpga_flow.py`。
- `goto-task`：跳到任务的 run 结果目录。[openfpga.sh:106-133](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga.sh#L106-L133)。

**③ 其它辅助函数**：`list-tasks`（[L87-92](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga.sh#L87-L92)）列出所有可用任务；`goto-root`（[L95-97](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga.sh#L95-L97)）回到根目录；`run-regression-local`（[L100-103](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga.sh#L100-L103)）本地跑回归测试；`unset-openfpga`（[L136-139](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga.sh#L136-L139)）清理掉这些变量和函数。

**④ Dockerfile 的旁证**。官方镜像 [Dockerfile](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/Dockerfile) 基于预构建镜像 `ghcr.io/lnis-uofu/openfpga-master`（[L1](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/Dockerfile#L1)），工作目录是 `/opt/openfpga/`（[L59](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/Dockerfile#L59)）。如果你不想本地编译，用这个镜像是最快的上手方式——它已经把子模块、依赖、编译产物都准备好了。

#### 4.4.4 代码实践

**实践目标**：加载环境脚本，验证环境变量与快捷函数生效，并跑出 `openfpga` 的版本号。

**操作步骤**：

1. 在仓库根目录加载脚本：
   ```bash
   source openfpga.sh
   ```
2. 检查环境变量：
   ```bash
   echo $OPENFPGA_PATH
   echo $OPENFPGA_SCRIPT_PATH
   ```
3. 检查快捷函数是否已定义：
   ```bash
   type run-task
   type goto-task
   ```
4. 让系统能找到 openfpga 二进制（把它加进 PATH），再查版本：
   ```bash
   export PATH=$PWD/build/openfpga:$PATH
   openfpga --version
   ```

**需要观察的现象**：第 1 步会打印 `Setting OPENFPGA_PATH=...`；第 2 步两个变量都指向仓库内的真实路径；第 3 步 `type` 显示它们是 shell 函数；第 4 步打印版本号 `1.2.4307`（来自 `VERSION.md`）。

**预期结果**：`openfpga --version` 成功输出版本，证明编译产物可用、环境配置正确。

> 「待本地验证」——`openfpga --version` 的确切输出格式以实际二进制为准。

#### 4.4.5 小练习与答案

**练习 1**：为什么必须在仓库根目录执行 `source openfpga.sh`？

**参考答案**：因为脚本在 `OPENFPGA_PATH` 未设置时，用 `$(pwd)`（当前目录）作为根路径，并据此拼出 `scripts`、`tasks` 目录。若在别处 source，这些路径会指错，后续 `run-task` 会找不到 Python 脚本和任务。

**练习 2**：`source openfpga.sh` 和 `bash openfpga.sh` 有什么本质区别？

**参考答案**：`source` 在当前 shell 执行，`export` 的变量和 `function` 在脚本结束后依然存在于当前 shell；`bash` 在子 shell 执行，子 shell 一退出，所有变量和函数随之消失，等于白跑。

---

## 5. 综合实践

把本讲四个模块串起来，完成「从空仓库到看到版本号」的全流程，并用一条 CI 命令验证你的理解。

**任务**：

1. 模拟全新环境，先清理：`make clean`。
2. 用一条等价于 CI 的命令完成 checkout + 编译（设为 debug 构建以便日后调试）：
   ```bash
   make all BUILD_TYPE=debug cmake_goals=openfpga
   ```
   > 对照 [.github/workflows/build.yml:302](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/.github/workflows/build.yml#L302) 的 `make all BUILD_TYPE=DEBUG ...`，你做的工作和 CI 是一致的，只是缩小了 `cmake_goals`。
3. 配置运行环境并把二进制加入 PATH：
   ```bash
   source openfpga.sh
   export PATH=$PWD/build/openfpga:$PATH
   ```
4. 验证三件事，并用自己的话解释每一步背后发生了什么：
   - `openfpga --version` 输出版本号；
   - `echo $OPENFPGA_PATH` 指向仓库根目录；
   - `ls build/` 下能看到 `openfpga/` 子目录。

**验收标准**：能口头讲清「`make all` → checkout + cmake 配置 + make 编译 → `source` 设环境变量」这条链路上，每一步分别调用了本讲哪个文件、哪个目标/变量。

> 若本机依赖不全或网络受限无法完成完整编译，可改用官方 Docker 镜像 `ghcr.io/lnis-uofu/openfpga-master` 完成第 3、4 步的环境与版本验证，并标注「待本地验证」编译部分。

## 6. 本讲小结

- OpenFPGA 依赖三个 git 子模块（yosys、vtr、yosys-slang），clone 后目录为空，必须 `make checkout` 才能填满，这是编译的绝对前置条件。
- 构建是两层结构：外层 `Makefile` 是封装，内层 CMake 才是内核；`make compile` 背后是 `prebuild`（cmake 配置）+ `make -C build`（真正编译）。
- 三个核心目标：`make checkout`（拉子模块）、`make compile`（编译）、`make all`（前两者串行，全新环境首选）。
- 关键可调参数：`BUILD_TYPE`（release/debug）、`cmake_goals`（缩小编译范围，如只编 `openfpga`）、`CMAKE_FLAGS`（传 `-D` 开关给 CMake）。
- `CMakeLists.txt` 通过 `OPENFPGA_WITH_*` 选项（Yosys、SWIG、Installer 等）控制构建范围，版本号从 `VERSION.md` 读取，子工程按 vtr → libs → openfpga 顺序纳入。
- `source openfpga.sh` 在当前 shell 设置 `OPENFPGA_PATH` 等变量并定义 `run-task`/`goto-task` 等快捷函数，必须在仓库根目录执行。

## 7. 下一步学习建议

到这里你已经有了可运行的 `openfpga` 二进制和配置好的环境变量。下一讲 **u1-l4 运行第一个 FPGA 设计流** 将直接用 `source openfpga.sh` 后的 `run-task` 跑通一个最小示例（如 `and2`），让你第一次看到完整的「Verilog → 比特流」产出。

如果你想更深入理解本讲的构建细节，建议继续阅读：

- [Makefile](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/Makefile) 末尾的 `COMMENT_EXTRACT`（[L137-144](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/Makefile#L137-L144)）——它解释了为什么 `make help` 能自动列出所有目标说明。
- [.github/workflows/build.yml](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/.github/workflows/build.yml) 的各个 job——这是「官方在多种平台上到底怎么编译」的最权威参考，本讲的命令全部能在里面找到对应。
