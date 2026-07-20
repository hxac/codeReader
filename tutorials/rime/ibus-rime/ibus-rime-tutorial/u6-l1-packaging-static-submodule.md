# 打包、静态构建与子模块

## 1. 本讲目标

本讲是「工程化与架构」单元的第一篇，聚焦 ibus-rime 在「能编译」之上的另一层问题：**如何把它连同它的全部依赖，打包成一个可分发、可移植的二进制包**。

学完本讲，你应当能够：

1. 说清 `librime` 与 `plum` 两个 git 子模块各自的角色，以及它们何时被「当作系统包」、何时被「拉到本地从源码构建」。
2. 理解 `cmake/FindRimeData.cmake` 如何在编译期探测 `rime-data` 目录，以及如何用 `-DRIME_DATA_DIR` 覆盖它。
3. 掌握 `BUILD_STATIC` 选项的含义、`RIME_DEPS` 静态库清单的组成，以及为什么静态链接要把 boost/leveldb/opencc 等显式列出。
4. 读懂 `package/` 下三个脚本——`make-package`、`make-binpkg-static`、`binpkg-install`——如何协作，把三个项目（librime、plum/brise、ibus-rime）编排进一个暂存目录再安装到系统。
5. 把 `binpkg-install` 里的每一条 `install` 命令，对回到 `CMakeLists.txt` 的 `install()` 规则，理解「打包脚本」与「CMake 安装规则」是同一套路径的两种表达。

---

## 2. 前置知识

本讲默认你已经读过：

- **u1-l2 依赖、构建与运行**：知道 ibus-rime 用 CMake 构建，根目录 `Makefile` 是 `cmake` 的薄封装，四组依赖（IBus、libnotify、librime、rime-data）用三种不同机制发现，最终产物是 `ibus-engine-rime`。
- **u5-l1 ibus_rime.yaml 与运行时配置加载**：知道 `IBUS_RIME_SHARED_DATA_DIR` 宏来自编译期的 `RIME_DATA_DIR`，`ibus_rime.yaml` 被安装到该目录。

此外，需要几个本讲会用到的工程概念：

- **动态链接 vs 静态链接**：动态链接（`.so`）在运行时由动态加载器找共享库，二进制小但依赖目标机器装好了对应 `.so`；静态链接（`.a`）把库代码直接抄进可执行文件，体积大但拷到哪都能跑。静态链接常用于「分发一个尽量自包含的二进制」。
- **传递依赖（transitive dependencies）**：librime 自身依赖 boost、leveldb、opencc、marisa、yaml-cpp、glog、ICU 等。动态链接 librime 时，这些传递依赖由 librime 的 `.so` 自己解决；静态链接 librime 时，这些依赖「浮出水面」，必须由最终可执行文件显式列出。
- **git 子模块（submodule）**：在一个 git 仓库里嵌入另一个仓库，只记录一个 commit 指针，不复制全部历史。适合「我的项目需要引用另一个独立维护的项目」。
- **暂存目录 / `DESTDIR`**：`make DESTDIR=/tmp/pkg install` 把本该装到 `/usr/...` 的文件改而装到 `/tmp/pkg/usr/...`，从而把一套安装结果「暂存」成一个可打包的目录树，而不污染构建机。

---

## 3. 本讲源码地图

本讲涉及的文件全部在仓库根目录，且绝大多数是构建/打包脚本而非 C 业务代码：

| 文件 | 作用 | 本讲用来讲 |
| --- | --- | --- |
| `.gitmodules` | 声明两个 git 子模块（librime、plum）的 path 与 url | 4.1 子模块 |
| `cmake/FindRimeData.cmake` | 自定义 CMake find 模块，按候选目录清单探测 `rime-data` | 4.2 数据目录定位 |
| `CMakeLists.txt` | 主构建脚本，含 `BUILD_STATIC`、`RIME_DEPS`、四条 `install()` | 4.2 / 4.3 / 4.4 |
| `Makefile` | `cmake` 薄封装，提供 `ibus-engine-rime` 与 `ibus-engine-rime-static` 两个目标 | 4.3 静态构建入口 |
| `package/make-package` | 用 `git archive` 生成源码 tarball | 4.4 源码包 |
| `package/make-binpkg-static` | 编排「librime → brise → ibus-rime」全静态构建，输出到暂存目录 `pkg/` | 4.4 二进制包编排 |
| `package/binpkg-install` | 把暂存目录里的文件安装到真实系统 | 4.4 安装落地 |

记忆线索：**子模块管「源码从哪来」，FindRimeData 管「数据目录在哪」，BUILD_STATIC 管「怎么链」，package 脚本管「怎么打包发出去」**。

---

## 4. 核心概念与源码讲解

### 4.1 librime/plum 子模块：源码与数据的来源

#### 4.1.1 概念说明

ibus-rime 是「薄前端」（见 u1-l1），它本身不含输入法算法，也不含输入方案与词库。算法在 **librime** 里，方案与词库由 **plum** 产出（产物即 `rime-data`）。这两个项目由 Rime 社区独立维护、各自发版，ibus-rime 通过 git 子模块把它们「挂」进自己的工作树，方便：

1. **普通构建**：直接用发行版已经打包好的 `librime` / `rime-data`，子模块留空也无所谓（见 u1-l2 的依赖发现）。
2. **完整/静态构建**：把子模块拉到本地，从源码构建 librime 与数据，得到一个完全自包含、版本锁定的产物。

也就是说，子模块是一种「**可选的、版本锁定的源码供应方式**」——平时可以不拉，需要做完整打包时才拉。

#### 4.1.2 核心流程

子模块的声明与使用流程：

1. `.gitmodules` 记录每个子模块的 `path`（挂载点）与 `url`（上游仓库）。
2. `git submodule update --init` 把对应 commit 的源码检出到 `path`。
3. 普通构建：CMake 用 `find_package(Rime)` 找系统已装的 librime，子模块是否在场不影响。
4. 静态/打包构建：脚本 `cd` 进子模块目录，从源码编译。

> **命名小历史**：当前子模块叫 `plum`，但打包脚本 `make-binpkg-static` 里仍用旧名 `brise`。plum 是 brise 的继任者（方案与词库集合从 brise 改名而来），脚本保留了 `brise` 这个目录名并带兼容软链（见 4.4.3）。阅读源码时把 `plum` ≈ `brise` 对应起来即可。

#### 4.1.3 源码精读

子模块声明在 [.gitmodules:1-6](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/.gitmodules#L1-L6)，声明了 `librime`（核心引擎）与 `plum`（方案/词库）两个挂载点及其 GitHub 上游。

README 也把这两个角色写得很清楚：构建依赖里有 [README.md:22](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/README.md#L22) 的 `plum (submodule)`，运行时依赖里有 [README.md:30](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/README.md#L30) 的 `rime-data (provided by plum)`——即 plum 的产物就是运行时要用的 rime-data。

#### 4.1.4 代码实践

**实践目标**：确认本仓库的两个子模块是否已检出，理解「子模块只是一个 commit 指针」。

**操作步骤**：

1. 在仓库根目录执行 `git submodule status`。
2. 观察输出：每行形如 `±<commit> librime` 或 `±<commit> plum`，前缀符号含义不同——空格表示已正常检出、`-` 表示未初始化、`+` 表示子模块 HEAD 与父仓库记录的 commit 不一致。

**需要观察的现象**：

- 若前缀是 `-`，说明子模块目录是空的，普通构建无所谓，但 `make-binpkg-static` 会失败。
- 记下 librime 与 plum 各自锁定的 commit 短哈希。

**预期结果**：两条记录，分别对应核心引擎与方案数据；若未初始化则前缀为 `-`。若你无权在当前环境执行该命令或子模块确未拉取，结果标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 librime 和 plum 用「子模块」而不是直接把它们的源码拷进 ibus-rime 仓库？

> **参考答案**：librime 与 plum 是独立发版的项目，被多个前端（ibus-rime、fcitx-rime、Squirrel 等）共用。子模块只存一个 commit 指针，既能锁定版本、又能各自独立演进，避免三方代码重复与同步负担。

**练习 2**：`rime-data` 是 librime 的「源码」还是「数据」？它由谁产出？

> **参考答案**：是数据（输入方案 `.yaml`、词库 `.dict.yaml`、编译后的二进制等）。它由 plum（旧名 brise）产出，运行时被 librime 读取；普通安装时由发行版的 `rime-data` 包提供。

---

### 4.2 FindRimeData：编译期数据目录定位

#### 4.2.1 概念说明

ibus-rime 在编译期就需要知道 `rime-data` 在哪，原因有二（见 u1-l2、u5-l1）：

1. 把目录路径烘焙进宏 `IBUS_RIME_SHARED_DATA_DIR`（经 `rime_config.h.in` → `rime_config.h`），运行时 librime 用它定位共享数据。
2. 把 `ibus_rime.yaml` 安装到该目录（`install(FILES ibus_rime.yaml DESTINATION ${RIME_DATA_DIR})`）。

但不同发行版把 rime-data 放在不同路径（有人放 `/usr/share/rime-data`，有人放 `/usr/share/rime/data`），而且用户也可能想自定义。于是项目写了一个**自定义 CMake find 模块** `cmake/FindRimeData.cmake`，按候选目录清单依次探测，命中第一个即采用；并允许用 `-DRIME_DATA_DIR=...` 直接覆盖、跳过探测。

#### 4.2.2 核心流程

探测与覆盖的优先级（高 → 低）：

1. **用户显式覆盖**：`cmake -DRIME_DATA_DIR=/my/path ..`。此时 `CMakeLists.txt` 里 `if(NOT DEFINED RIME_DATA_DIR)` 为假，**根本不调用** `find_package(RimeData)`，直接采用用户给的值。
2. **find 模块探测**：未覆盖时，`find_package(RimeData)` 执行 `FindRimeData.cmake`，遍历候选目录清单，取第一个真实存在的目录作为 `RIME_DATA_DIR`。
3. **都失败**：`REQUIRED` 会让 CMake 报错中止。

候选清单的遍历是「**顺序敏感**」的：列表里越靠前的目录优先级越高。本模块把 `${CMAKE_INSTALL_PREFIX}/share/...` 放在 `/usr/share/...` 之前，意味着「若我在自定义 prefix 下装了 rime-data，优先认它」。

#### 4.2.3 源码精读

主脚本里的触发点在 [CMakeLists.txt:30-34](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/CMakeLists.txt#L30-L34)：第 30-32 行只在 `RIME_DATA_DIR` 未被预定义时才 `find_package(RimeData REQUIRED)`；第 33 行打印最终值；第 34 行 `add_definitions(-DRIME_DATA_DIR="...")` 把它变成一个 C 预处理宏，供 `rime_config.h` 之外的地方也能用。

要让 `find_package(RimeData)` 能找到自定义模块，[CMakeLists.txt:10](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/CMakeLists.txt#L10) 先把项目自带的 `cmake/` 目录加进了 `CMAKE_MODULE_PATH`——否则 CMake 只会在自带的模块库里找，找不到 `FindRimeData.cmake`。

find 模块本身在 [cmake/FindRimeData.cmake:7-23](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/cmake/FindRimeData.cmake#L7-L23)：

- 第 7-10 行定义候选目录清单（两条 prefix 相对路径 + 两条绝对路径）。
- 第 14-19 行 `foreach` 顺序遍历，`IS_DIRECTORY` 为真就记下并 `set(RIME_DATA_FOUND True)`——注意它**不 break**，但因为后续命中会覆盖前面的值，实际效果是「最后一个命中的目录胜出」。不过由于正常环境通常只有一个目录存在，行为等价于「取命中的那一个」。
- 第 21-22 行用 CMake 标准的 `find_package_handle_standard_args` 统一处理「找到/没找到」的输出与 `REQUIRED` 报错。

#### 4.2.4 代码实践

**实践目标**：观察 `RIME_DATA_DIR` 的解析结果，并验证可用 `-D` 覆盖。

**操作步骤**：

1. 进入 build 目录配置（不传 `RIME_DATA_DIR`）：`cd build && cmake .. 2>&1 | grep RIME_DATA_DIR`，观察打印行。
2. 再试覆盖：`cmake -DRIME_DATA_DIR=/opt/rime-data .. 2>&1 | grep RIME_DATA_DIR`。

**需要观察的现象**：

- 第一次应输出探测到的真实目录（如 `/usr/share/rime-data` 或 `/usr/share/rime/data`，取决于环境）。
- 第二次应输出 `"/opt/rime-data"`，且不会再调用 find 模块。

**预期结果**：覆盖生效，证明 `-DRIME_DATA_DIR` 优先级高于 find 模块探测。若当前环境无 rime-data 目录且未覆盖，`REQUIRED` 会让 cmake 报 `RimeData not found` 而中止——这也属于预期行为。结果标注「待本地验证」若你无法运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么候选清单里 `${CMAKE_INSTALL_PREFIX}/share/...` 要排在 `/usr/share/...` 前面？

> **参考答案**：为了让「自定义安装前缀下的 rime-data」优先于「系统全局的 rime-data」。当你用 `cmake -DCMAKE_INSTALL_PREFIX=/opt/ibus ...` 安装到非默认位置时，若 `/opt/ibus/share/rime-data` 存在，应优先认它，避免误用系统的旧数据。

**练习 2**：如果探测失败又没传 `-D`，会发生什么？

> **参考答案**：`find_package(RimeData REQUIRED)` 因 `REQUIRED` 直接让 CMake 出错中止，构建不会继续——因为不知道数据目录就没法生成正确的 `rime_config.h`，也无法决定 `ibus_rime.yaml` 的安装目标。

---

### 4.3 BUILD_STATIC 与 RIME_DEPS：静态链接

#### 4.3.1 概念说明

默认构建（`make` / `make ibus-engine-rime`）是**动态链接**：`ibus-engine-rime` 在运行时去 `dlopen` `librime.so`、`libibus-1.0.so`、`libnotify.so`。这要求目标机器都装好了对应 `.so`，适合发行版打包。

但如果你想要一个「拷到哪都能跑」的二进制（典型场景：跨发行版分发的预编译包、隔离环境），就打开 [CMakeLists.txt:6](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/CMakeLists.txt#L6) 定义的 `option(BUILD_STATIC ...)`（默认 `OFF`）。它把 librime 改为静态链接 `librime.a`，而 librime 自己的传递依赖（boost、leveldb、opencc、marisa、yaml-cpp、glog、ICU…）也会一并「浮出水面」，必须由 `ibus-engine-rime` 显式链接。这份显式清单就是 `RIME_DEPS`。

#### 4.3.2 核心流程

静态链接的依赖解析，本质是在解一个「**符号闭包**」：

\[ \text{ibus-engine-rime} \supseteq \text{librime.a} \cup \text{deps}(\text{librime.a}) \]

动态链接时，\(\text{deps}(\text{librime})\) 由 `librime.so` 的 `NEEDED` 属性自动传递给加载器，应用层无感；静态链接时没有加载器介入，应用必须**手动列出整条依赖闭包**，否则链接器报「未定义符号」（undefined reference）。这就是为什么 `RIME_DEPS` 要把 boost、leveldb、opencc 等一个个写出来。

链接还有一个**顺序约束**：Unix 链接器（`ld`）默认从左到右单趟解析，提供符号的库应排在引用它的库之后。因此 `target_link_libraries` 里把 `${Rime_LIBRARIES}`（即 `librime.a`，消费方在前）放在 `RIME_DEPS`（被消费的底层库在后）之前，是符合单趟解析顺序的。

#### 4.3.3 源码精读

整个静态分支在 [CMakeLists.txt:44-54](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/CMakeLists.txt#L44-L54)：

- 第 44 行 `if(BUILD_STATIC)` 整段只在选项打开时生效。
- 第 45-48 行单独 `find_package(ICU ... uc)`，把 ICU 的库追加进 `RIME_DEPS`（ICU 是开放字符集/编码库，librime 用它做多语言支持，拆出来单独找是因为它的 find 调用形式特殊，要带组件名 `uc`）。
- 第 50 行 `link_directories(${PROJECT_SOURCE_DIR}/lib)` 告诉链接器去仓库根下的 `lib/` 找那些 `.a`——这个 `lib/` 平时不存在，由打包脚本软链到 `librime/thirdparty/lib`（见 4.4.3）。
- 第 51-53 行填充 `RIME_DEPS`：先放系统级基础库 `m stdc++ pthread`，再是 boost 全家桶（filesystem/locale/regex/signals/system/thread），最后是 `glog`（日志）、`leveldb`（KV 存储）、`marisa`（字典树）、`opencc`（简繁转换）、`yaml-cpp`（YAML 解析）。这正是 librime 的传递依赖闭包。

最终在 [CMakeLists.txt:58](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/CMakeLists.txt#L58) 的 `target_link_libraries` 里，`RIME_DEPS`（动态模式下为空、静态模式下为上述清单）被追加到 `${IBus_LIBRARIES} ${LIBNOTIFY_LIBRARIES} ${Rime_LIBRARIES}` 之后。注意它对两种模式都适用——动态模式下 `RIME_DEPS` 是空变量，追加无害；静态模式下它补齐闭包。这是一种「**条件性追加，统一出口**」的写法。

用户侧入口在 [Makefile:18-21](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/Makefile#L18-L21) 的 `ibus-engine-rime-static` 目标，它和动态目标 [Makefile:13-16](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/Makefile#L13-L16) 唯一的区别就是多传了一个 `-DBUILD_STATIC=ON`。

#### 4.3.4 代码实践

**实践目标**：对比两种构建目标，观察 `RIME_DEPS` 的作用。

**操作步骤**：

1. 读 [Makefile:13-21](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/Makefile#L13-L21)，对比 `ibus-engine-rime` 与 `ibus-engine-rime-static` 两个目标的 cmake 参数差异。
2. （可选，需本地具备 boost/leveldb 等静态库）依次执行 `make` 与 `make ibus-engine-rime-static`，对产物 `build/ibus-engine-rime` 分别运行 `ldd`：动态产物会列出 `librime.so` 等共享库依赖，静态产物的 `librime` 相关行应消失（符号已内联）。

**需要观察的现象**：

- 两个 make 目标的命令行差异只有 `-DBUILD_STATIC=ON`。
- `ldd` 输出：静态产物不再依赖 `librime.so`，但仍会依赖 glibc、libibus、libnotify 等没有静态化的库。

**预期结果**：静态产物体积明显更大，且不再列出 librime 及其第三方依赖的 `.so`。若本地无静态库环境，则 `make ibus-engine-rime-static` 会在链接期报找不到 `libboost_*.a` 等——这正说明 `RIME_DEPS` 清单是硬要求。结果标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么动态链接时不需要写 `RIME_DEPS`，静态链接时必须写？

> **参考答案**：动态链接由运行时加载器根据 `librime.so` 的 `NEEDED` 自动传递传递依赖，应用二进制无感；静态链接没有加载器，应用必须显式列出 librime 的全部传递依赖闭包，否则链接器找不到符号。

**练习 2**：`link_directories(${PROJECT_SOURCE_DIR}/lib)` 指向的 `lib/` 目录平时并不存在于仓库里，它是怎么出现的？

> **参考答案**：由打包脚本 `make-binpkg-static` 用 `ln -s librime/thirdparty/lib` 创建的软链（见 4.4.3）。librime 的 `make thirdparty` 会把 boost/leveldb 等编译成静态库放进 `librime/thirdparty/lib`，软链后仓库根的 `lib/` 就指向这些 `.a`。

---

### 4.4 打包脚本三件套：从源码到二进制包

#### 4.4.1 概念说明

`package/` 下有三个脚本，分工不同：

| 脚本 | 产物 | 用途 |
| --- | --- | --- |
| `make-package` | 源码 tarball（`.tar.gz`） | 给发行版打包者提供「干净的、带版本前缀的源码包」 |
| `make-binpkg-static` | 一个暂存目录 `pkg/`（含三个项目全部产物） | 编排 librime + brise + ibus-rime 的全静态构建，结果落到 `pkg/usr/...` |
| `binpkg-install` | 系统上的真实文件 | 把 `pkg/` 暂存树安装到 `/usr/...` 并重启 ibus |

核心思想是 **`DESTDIR` 暂存 + 二次安装**：`make-binpkg-static` 用 `DESTDIR=$PKGDIR` 把三个项目的安装结果都汇集到一个 `pkg/` 目录（不碰系统），再把 `binpkg-install` 拷进去当 `INSTALL` 脚本；最终在目标机器上跑 `binpkg-install`，把 `pkg/` 里的文件落到真实路径。

#### 4.4.2 核心流程

`make-binpkg-static` 的构建顺序是严格的依赖链：

```
1. 准备 thirdparty（lib/ 软链 + 编译 boost/leveldb/opencc…）
        │
        ▼
2. 构建 librime（静态） → make DESTDIR=$PKGDIR install
        │  产出：usr/lib/librime.a、usr/include/rime_api.h、rime.pc、cmake/rime/…
        ▼
3. 构建 brise（数据） → make DESTDIR=$PKGDIR install
        │  产出：usr/share/rime-data/*（方案与词库）
        │  注意：此时 PATH 前置 $PKGDIR/usr/bin，让 brise 能用到刚装的 librime 工具
        ▼
4. 构建 ibus-rime（静态） → make DESTDIR=$PKGDIR install
        │  产出：usr/lib/ibus-rime/ibus-engine-rime、rime.xml、icons、ibus_rime.yaml
        ▼
5. 拷 binpkg-install 作为 pkg/INSTALL
```

顺序的必然性：ibrise 依赖 librime 的工具（`rime_deployer` 等），ibus-rime 静态链接依赖 librime 的 `librime.a` 与 thirdparty 的 `.a`，所以必须 **librime → brise → ibus-rime**。

`binpkg-install` 则是「按真实路径逐条 `install`」，它的每一条命令对应 `pkg/usr/` 下的一个子树，其中**属于 ibus-rime 的那几条**，与 `CMakeLists.txt` 的四条 `install()` 规则一一对应（见综合实践）。

#### 4.4.3 源码精读

**`make-package`** 在 [package/make-package:1-26](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/package/make-package#L1-L26)：接受 `path version` 两个参数；第 15-19 行按项目名决定 tag 命名（含 `rime` 字样的用 `rime-${ver}`，否则用 `${pkg}-${ver}`）；第 22 行用 `git archive --prefix=$pkg/` 导出一个**干净的、不含 `.git` 与构建产物的源码 tarball**，前缀保证解压后是一个带项目名的顶层目录——这是发行版打包者期望的格式。

**`make-binpkg-static`** 在 [package/make-binpkg-static:1-29](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/package/make-binpkg-static#L1-L29)：

- 第 3-4 行设置 `BOOST_ROOT` 与 `LIBRARY_PATH`，指向本地一份 boost 1_55_0 的编译结果（脚本作者环境固化，体现了这是个「维护者用」脚本而非通用构建路径）。
- 第 6 行 `cd $(dirname $0)/..` 切回项目根。
- 第 8-9 行兼容老布局：若本地没有 `librime`/`brise` 目录但有同级的，就软链过来——这里的 `brise` 就是 4.1.1 提到的 plum 旧名。
- 第 11-12 行是 thirdparty 准备：软链 `lib → librime/thirdparty/lib`；若 `lib/libyaml-cpp.a` 不存在（说明 thirdparty 没编过），就 `cd librime; make thirdparty` 把全部第三方静态库编译出来。
- 第 14-15 行定义 `PKGDIR=$(pwd)/pkg` 作为暂存根。
- 第 17-18 行构建 librime 静态库并 `make -C build-static DESTDIR=$PKGDIR install` 落到暂存目录。
- 第 20-22 行构建 brise：第 21 行 `PATH=$PKGDIR/usr/bin:$PATH` 把刚装的 librime 工具前置，让 brise 的构建脚本能调用它们；再 `make DESTDIR=$PKGDIR install`。
- 第 24-25 行构建 ibus-rime：`make clean` 后 `make ibus-engine-rime-static`（即 4.3 的静态目标），再 `make DESTDIR=$PKGDIR install`。
- 第 27 行把 `binpkg-install` 拷成 `pkg/INSTALL`，让暂存目录自带安装说明。

**`binpkg-install`** 在 [package/binpkg-install:1-24](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/package/binpkg-install#L1-L24)：

- 第 3-4 行先移除发行版自带的旧包、补装运行时依赖 `opencc libnotify4`，避免新旧文件冲突。
- 第 6 行 `cd $(dirname $0)` 切到脚本所在目录——注意它被 `make-binpkg-static` 拷成了 `pkg/INSTALL`，所以此时 cwd 就是 `pkg/`，下面各命令里的 `usr/...` 相对路径就是暂存树。
- 第 8-20 行逐条把 `usr/` 下的文件按真实系统路径 `install` 出去。
- 第 23 行 `ibus-daemon -drxR` 重启 ibus 守护进程，让新引擎被加载。

#### 4.4.4 代码实践

**实践目标**：把 `binpkg-install` 中**属于 ibus-rime** 的安装命令，逐条对回到 `CMakeLists.txt` 的 `install()` 规则。

**操作步骤**：

1. 打开 [package/binpkg-install:8-20](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/package/binpkg-install#L8-L20) 与 [CMakeLists.txt:65-68](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/CMakeLists.txt#L65-L68)。
2. 区分三类来源：librime 的产物（`usr/bin/*`、`rime_api.h`、`librime.a`、`rime.pc`、`cmake/rime/`）、brise 的产物（`usr/share/rime-data/*` 的方案/词库）、ibus-rime 的产物。
3. 只把「ibus-rime 产物」那几条，与四条 CMake `install()` 规则一一连线。

**需要观察的现象**：

- `binpkg-install` 安装的远不止 ibus-rime 自己——它把三个项目的产物一次装全。
- ibus-rime 自己只占其中 4 条路径。

**预期结果**（对应关系见综合实践的表格）。这是「同一个安装意图，两种表达」：CMake 的 `install()` 是**声明式的、由 `make install` 在构建机执行**；`binpkg-install` 是**命令式的、在目标机把已暂存的文件拷过去**。两者目标路径必须一致。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `make-binpkg-static` 要按 librime → brise → ibus-rime 的顺序，能不能换？

> **参考答案**：不能。brise 的构建要用 librime 提供的工具（如 `rime_deployer` 编译方案），脚本甚至为此把 `$PKGDIR/usr/bin` 前置进 PATH；ibus-rime 静态链接要用 librime 的 `librime.a` 与 thirdparty 的 `.a`。顺序倒了，后面的步骤会因为缺工具或缺库而失败。

**练习 2**：`binpkg-install` 第 6 行 `cd $(dirname $0)` 为什么重要？

> **参考答案**：因为它被 `make-binpkg-static` 拷成了 `pkg/INSTALL`，脚本所在目录就是暂存根 `pkg/`。`cd` 到这里之后，下面 `usr/bin/*`、`usr/lib/...` 等相对路径才能正确指向暂存树里的文件；否则相对路径会相对于调用者的 cwd，装错文件。

---

## 5. 综合实践

**任务**：完成 `make-binpkg-static` 的构建顺序说明，并绘制 `binpkg-install` 安装路径 ↔ CMake `install()` 规则的对照表。

### 5.1 说明构建顺序

`make-binpkg-static`（[package/make-binpkg-static:17-25](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/package/make-binpkg-static#L17-L25)）依次构建三件事，全部 `DESTDIR=$PKGDIR` 落到暂存目录：

1. **librime**（`make librime-static` + install）：先有核心引擎的静态库 `librime.a` 与头文件、pkg-config、cmake 配置。
2. **brise / plum**（`make` + install，PATH 前置 `$PKGDIR/usr/bin`）：用 librime 的工具编译方案与词库，产出 `usr/share/rime-data/*`。
3. **ibus-rime**（`make ibus-engine-rime-static` + install）：静态链接前两步的产物，产出 `ibus-engine-rime`、`rime.xml`、icons、`ibus_rime.yaml`。

依赖方向是单向的 `librime → brise → ibus-rime`，因此顺序不可调换。

### 5.2 安装路径对照表

`binpkg-install`（[package/binpkg-install:8-20](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/package/binpkg-install#L8-L20)）里**属于 ibus-rime** 的命令，与 [CMakeLists.txt:65-68](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/CMakeLists.txt#L65-L68) 的四条 `install()` 一一对应：

| `binpkg-install` 命令 | 真实路径 | 对应 CMake `install()` 规则 |
| --- | --- | --- |
| 第 16 行 `.../usr/share/ibus/component/rime.xml → /usr/share/ibus/component/` | `/usr/share/ibus/component/rime.xml` | 第 65 行 `install(FILES .../rime.xml DESTINATION ${CMAKE_INSTALL_DATADIR}/ibus/component)` |
| 第 11-12 行 `.../usr/lib/ibus-rime/ibus-engine-rime → /usr/lib/ibus-rime/` | `/usr/lib/ibus-rime/ibus-engine-rime` | 第 66 行 `install(TARGETS ibus-engine-rime DESTINATION ${CMAKE_INSTALL_LIBEXECDIR}/ibus-rime)` |
| 第 17-18 行 `.../usr/share/ibus-rime/icons/* → /usr/share/ibus-rime/icons/` | `/usr/share/ibus-rime/icons/*.png` | 第 67 行 `install(DIRECTORY icons DESTINATION ${CMAKE_INSTALL_DATADIR}/ibus-rime FILES_MATCHING PATTERN "*.png")` |
| 第 19-20 行 `.../usr/share/rime-data/* → /usr/share/rime-data/`（其中含 `ibus_rime.yaml`） | `/usr/share/rime-data/ibus_rime.yaml` 等 | 第 68 行 `install(FILES ibus_rime.yaml DESTINATION ${RIME_DATA_DIR})` |

> 说明：第 19-20 行安装的是整个 `rime-data` 目录，里面**既有 brise 产出的方案/词库，也包含 ibus-rime 的 `ibus_rime.yaml`**（后者来自第 68 行规则，目标目录就是 `RIME_DATA_DIR`，本环境即 `/usr/share/rime-data`）。所以这一行同时承载了 brise 与 ibus-rime 两类产物。

**关键结论**：四条 CMake `install()` 规则决定了「`make install` 会把什么放到哪个相对路径」，而 `binpkg-install` 只是把这套相对路径（在 `DESTDIR=$PKGDIR` 下变成 `pkg/usr/...`）原样落到真实系统。两者的路径变量取值也一致：`CMAKE_INSTALL_DATADIR=share`、`CMAKE_INSTALL_LIBEXECDIR=lib`（由 [Makefile:4-5](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/Makefile#L4-L5) 的 `sharedir=$(PREFIX)/share`、`libexecdir=$(PREFIX)/lib` 经 cmake `-D` 传入），prefix 默认 `/usr`，故落到 `/usr/share/...` 与 `/usr/lib/...`，与 `binpkg-install` 完全吻合。

`binpkg-install` 里**不属于 ibus-rime** 的命令（第 8-9 行的 `usr/bin/*`、第 9 行 `rime_api.h`、第 10 行 `librime.a`、第 13 行 `rime.pc`、第 14-15 行 `cmake/rime/`）来自 librime 的 install，brise 的方案数据则混在第 19-20 行里——这正是「三合一打包」的体现。

---

## 6. 本讲小结

- **子模块是可选的版本锁定源码供应**：`librime`（引擎）与 `plum`/brise（方案数据）平时可用系统包，做完整打包时才拉到本地从源码构建。
- **FindRimeData 是顺序敏感的目录探测**：候选清单里 prefix 相对路径优先于 `/usr/share/...`，且可被 `-DRIME_DATA_DIR` 整体跳过覆盖。
- **BUILD_STATIC 把传递依赖闭包显式化**：`RIME_DEPS` 列出 boost/leveldb/opencc/marisa/yaml-cpp/glog/ICU 等，因为静态链接没有加载器代为传递依赖。
- **三脚本分工**：`make-package` 出源码 tarball、`make-binpkg-static` 编排三项目静态构建到 `pkg/` 暂存树、`binpkg-install` 把暂存树落到真实系统。
- **构建顺序固定为 librime → brise → ibus-rime**：因为后者的构建依赖前者的工具与静态库。
- **`binpkg-install` 与 CMake `install()` 是同一套路径的两种表达**：四条 ibus-rime 安装命令与 `CMakeLists.txt` 末尾四条规则逐条对应，路径变量取值一致。

---

## 7. 下一步学习建议

本讲把「打包与静态构建」讲完了，下一讲 **u6-l2 架构取舍与二次开发** 会从这些工程细节抽身，回到架构层面：

1. 总结 ibus-rime「薄前端」的设计取舍——为什么 RimeApi 是稳定边界、GObject 如何适配 IBus、配置如何驱动 UI。
2. 给出可操作的二次开发路径：新增一个 `style` 配置项，从 `ibus_rime.yaml` → `rime_settings.c` 解析 → `rime_engine.c` 使用，把本手册从 u5-l1 到 u4-3 的链路再串一次。
3. 建议顺带阅读 [rime_engine.c](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c) 的 `class_init` 虚函数表，理解 GObject + IBusEngineClass 的扩展点。

若你想更深入打包侧，可继续阅读 librime 上游的 `Makefile`（`librime-static`、`thirdparty` 目标）与 plum 的打包脚本，理解 4.4 编排链条中每一环在自己项目里是怎么被定义的。
