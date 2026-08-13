# 打包安装与 run 包生成

## 1. 本讲目标

本讲是「构建、打包与测试体系」单元的第二讲，紧接 [u9-l1（CMake 构建系统与多架构产物）](u9-l1-cmake-multi-arch.md)。u9-l1 回答的是「**编译期**：同一份源码怎么变成 7 份（x86）/5 份（aarch64）架构各异的 `libcpudebug.so`」；本讲回答的是「**打包安装期**：这些 `.so` 编出来之后，是怎么落盘到安装目录、怎么建立新旧库名软链、怎么被封进一个 `.run` 自解压包、又怎么被装进 CANN 路径的」。

读完本讲，你应当能够：

- 说清 `bash build.sh --pkg` 在「编译」之外还多做了什么：`--pkg` / `--pkg-type` / `--msot` 三个开关如何分别走向 CPack 打包、rpm/deb 生成、msot 子仓构建；
- 读懂 `cpudebug/CMakeLists.txt` 里 `install(TARGETS ...)` 与 `install(CODE ... create_symlink ...)` 的区别，解释为什么每装一个 `libcpudebug.so` 都要顺手建一个 `libtikcpp_debug.so` 软链；
- 理解 `cmake/package.cmake` 如何调用闭源的 cann-cmake 工具链（`npu_op_package` / `set_cann_cpack_config`）把整棵安装树封成 `cann-asc-tools_*.run`；
- 描述 `.run` 包安装时的完整链路：`install.sh` → `run_asc-tools_install.sh` → `install_common_parser.sh`，以及多版本目录 `cann/<版本>/` 与 `latest` 软链是如何形成的。

本讲全部围绕 **`BUILD_OPEN_PROJECT=ON`（开源独立构建）** 模式展开——只有这个模式才会真正打包出可交付的 run 包。

## 2. 前置知识

本讲默认你已具备以下基础（不熟悉的术语下面会顺带解释）：

- **u9-l1 的结论**：asc-tools 用 `PRODUCT_TYPE_LIST` + `foreach` 为每种 NPU 架构各编出一个 `libcpudebug.so`，闭源 `libcpudebug_model.a` 被拆成 `.o` 后与开源 `api_check`/`regfwk` 源码合并链接。本讲不再重复编译细节，直接从「`.so` 已经编好」讲起。
- **CMake 的 `install` 机制**：`install(TARGETS ... DESTINATION ...)` 声明「构建产物安装到哪」，`install(FILES ...)` 声明「哪些文件被拷进安装树」，`install(CODE "...")` 声明「安装时执行一段 CMake 脚本」。这三者是本讲的主角。
- **符号链接（symlink / 软链）**：`ln -s 目标 名字` 创建一个「指向另一个文件」的特殊文件，访问 `名字` 等同于访问 `目标`。CMake 里等价命令是 `cmake -E create_symlink <目标> <名字>`。本讲会看到大量软链，它们的作用是「用旧名字兼容新库」。
- **CPack**：CMake 自带的打包工具，`cmake --build . --target package` 会触发它，把 `install` 规则收集到的文件树打成 `.tar.gz` / `.rpm` / `.deb` / 自解压 `.run` 等交付件。
- **makeself**：一个把任意目录打包成「自解压 shell 脚本」的开源工具，产物就是 `.run` 文件——它本质是一个 shell 脚本头部 + 末尾附带的压缩数据，执行时先把自己解压到临时目录、再运行里面的 `install.sh`。CANN 几乎所有交付包都用这种形态。
- **CANN 安装路径约定**：root 用户默认装到 `/usr/local/Ascend`，普通用户装到 `~/Ascend`；多版本共存时真实文件在 `<根>/cann/<版本>/` 下，再由 `<根>/latest` 软链指向最新版本。

如果你对 CMake `install` 完全陌生，建议先花十分钟读懂 `install(TARGETS)` 与 `install(CODE)` 的官方文档再来读本讲。

## 3. 本讲源码地图

本讲涉及的文件都在「编译产物 → 交付件 → 装机」这条线上：

| 文件 | 作用 |
| --- | --- |
| [build.sh](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh) | 唯一编译入口；本讲聚焦它的 `--pkg` / `--pkg-type` / `--msot` 打包分支 |
| [cpudebug/CMakeLists.txt](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt) | **本讲主角之一**：`install(TARGETS)` 落盘规则 + 一连串 `install(CODE create_symlink)` 软链声明全在这里 |
| [cpudebug/cmake/fun.cmake](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/cmake/fun.cmake) | `product_dir()`：把产品名（如 `ascend910B1`）映射成安装目录名（如 `Ascend910B1`） |
| [cmake/package.cmake](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cmake/package.cmake) | **本讲主角之二**：CPack 打包配置，决定包名、架构、装哪些脚本、rpm/deb 的特殊处理 |
| [cmake/fetch_cann_cmake.cmake](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cmake/fetch_cann_cmake.cmake) | 拉取闭源的 cann-cmake 工具链（提供 `npu_op_package` 等打包宏与 makeself 封装） |
| [version.cmake](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/version.cmake) | 版本号 `9.1.0` 与上下游依赖约束，是包名与版本目录的来源 |
| [scripts/package/asc-tools/scripts/install.sh](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/package/asc-tools/scripts/install.sh) | `.run` 包自解压后执行的**入口脚本**：解析 `--full`/`--run`/`--pylocal` 等参数、定位安装路径、调用真正的安装子脚本 |
| [scripts/package/asc-tools/scripts/run_asc-tools_install.sh](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/package/asc-tools/scripts/run_asc-tools_install.sh) | 安装子脚本：调用 `install_common_parser.sh --copy_all` 把文件铺到安装目录 |
| [scripts/package/asc-tools/scripts/asc-tools_custom_create_softlink.sh](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/package/asc-tools/scripts/asc-tools_custom_create_softlink.sh) | 多版本场景下，为 `latest` 目录补建 Python 工具与 tools 目录的软链 |

> 说明：`install_common_parser.sh`、`common_func.inc` 等安装公共脚本并不在 asc-tools 仓库内，而是来自 CANN 包（`CANN_CMAKE_DIR/scripts/install/`），由 [cmake/package.cmake:144-157](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cmake/package.cmake#L144-L157) 在打包时拷进包里。本讲讲到这些脚本时会标注「来自 CANN」。

## 4. 核心概念与源码讲解

本讲按「**打包分支 → install 落盘 → 软链兼容 → 封装与装机**」四个最小模块组织，正好对应一个 `.so` 从编出来到被用户用上的全过程。

### 4.1 build.sh 打包分支：从 --pkg 到 cmake --target package

#### 4.1.1 概念说明

u9-l1 讲过 `build.sh` 的主线三步（`set_env` → `cmake_config` → `build`）。但那只回答了「怎么触发编译」。本模块要回答的是：**`--pkg` 这个开关到底改变了什么？**

关键区分有两个维度：

- **「编译」还是「打包」**：不带 `--pkg`、不带 `-t` 时，`build.sh` 默认走 `build_package()`，它的 target 是 `package`——也就是说，**asc-tools 的默认行为就是「编完即打包」**；而 `-t/--test` 走 target `all`，只编译测试不打包，两者互斥。
- **「打什么包」**：`--pkg-type` 决定交付件格式（`run`/`rpm`/`deb`），`--msot` 决定是否额外构建配套的 msot 子仓包。

理解这条分支的关键变量是 `PACKAGE_TYPE` 和 `PACKAGE_OPEN_PROJECT`，它们最终都会变成 CMake 的 `-D` 参数，传给 `cmake/package.cmake`。

#### 4.1.2 核心流程

`build.sh` 打包分支的流程可以这样概括：

```text
main()
  ├── set_options()          # 解析 --pkg / --pkg-type / --msot 等
  ├── 若 --msot 单独出现 → 报错（必须配 --pkg）
  ├── 若 --pkg --msot → build_msot() 后直接 exit（另走子仓构建线）
  ├── set_env()              # 定位 CANN 包（u9-l1 已讲）
  ├── copy_deps_file()       # 把 simulator / cpudebug-deps 的 tar.gz 拷到 third_party
  ├── clean()                # 清空 build/ 与 build_out/
  ├── 拼装 CUSTOM_OPTION:
  │     -DPACKAGE_OPEN_PROJECT=ON        # 仅当 --pkg
  │     -DPACKAGE_TYPE=${PACKAGE_TYPE}   # run/rpm/deb
  │     -DASCEND_CANN_PACKAGE_PATH=...
  │     -DCANN_3RD_LIB_PATH=...
  └── 分发:
        TEST     → build_test()    # target=all
        TEST_PART → build_test_part()
        else     → build_package() # target=package  ← 打包走这里
```

注意三个易错点：

1. `--pkg` 与 `-t/--test` **互斥**，`build.sh` 会在多处校验并直接退出；
2. `--msot` **必须**与 `--pkg` 同时出现，否则报错；
3. `--pkg --msot` 是一条**独立的早退分支**，执行完 `build_msot()` 就 `exit 0`，根本不会走到后面的 cpudebug 打包逻辑——msot 是另一个独立交付件。

#### 4.1.3 源码精读

**① 默认包类型与合法值校验**

包类型默认是 `run`，且只允许 `run`/`rpm`/`deb` 三种：[build.sh:28](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L28) 设默认值，[build.sh:304-311](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L304-L311) 做校验。

```bash
PACKAGE_TYPE="run"          # 默认 run 包
...
check_pkg_type() {
  local pkg_type="$1"
  if [[ "${pkg_type}" != "run" && "${pkg_type}" != "rpm" && "${pkg_type}" != "deb" ]]; then
    log "[ERROR] Invalid value ${pkg_type} for option --pkg-type"
    usage; exit 1
  fi
}
```

**② `--pkg` 注入打包开关**

当用户传了 `--pkg`，`PKG=true`，于是往 CMake 选项里追加 `-DPACKAGE_OPEN_PROJECT=ON`；而 `PACKAGE_TYPE` 始终会被传下去：[build.sh:872-876](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L872-L876)。

```bash
if [ "${PKG}" == "true" ];then
  CUSTOM_OPTION="${CUSTOM_OPTION} -DPACKAGE_OPEN_PROJECT=ON"
fi
CUSTOM_OPTION="${CUSTOM_OPTION} -DASCEND_CANN_PACKAGE_PATH=... -DCANN_3RD_LIB_PATH=... -DCMAKE_BUILD_TYPE=${BUILD_TYPE} -DPACKAGE_TYPE=${PACKAGE_TYPE}"
```

`PACKAGE_OPEN_PROJECT` 这个开关最终被 cann-cmake 工具链里的打包宏消费（开源仓库里看不到它的定义，它来自 [cmake/fetch_cann_cmake.cmake](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cmake/fetch_cann_cmake.cmake) 拉取的 `cann/cmake` 仓库），作用是告诉打包器「这次要真正产出交付包，而不是只编译」。

**③ 默认 target 是 `package`**

`build_package()` 先 `cmake_config` 再 `build package`，这里的 `package` 是 CPack 注册的顶层目标：[build.sh:254-257](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L254-L257)。最终的分发逻辑在 [build.sh:887-893](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L887-L893)——只有传了 `-t` 或 `--cpp_utest`/`--python_utest` 才会偏离打包主线。

**④ `--msot` 的早退分支**

msot（MindStudio Operator Tools）是配套的另一个工具集，它的构建完全独立：[build.sh:836-846](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L836-L846) 校验「必须配 `--pkg`」并早退到 `build_msot()`。

```bash
if [ "${MSOT}" == "true" ] && [ "${PKG}" != "true" ]; then
  log "[ERROR] --msot must be used with --pkg. Example: bash build.sh --pkg --msot"
  exit 1
fi
if [ "${PKG}" == "true" ] && [ "${MSOT}" == "true" ]; then
  build_msot
  exit 0
fi
```

`build_msot()`（[build.sh:771-830](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L771-L830)）会探测构建环境里是否已存在 `mindstudio` 源码区：有则用 `python3 build.py local` 就地构建、并为各子仓（[build.sh:472](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L472) 列出 `msopgen/msdebug/mskpp/mskl/msopprof/mssanitizer`）补建三方库软链；没有则把 msot 作为 git submodule 拉下来再 `python3 build.py`。产物是另一套 `*.run`，拷到 `mindstudio/msot/output/`。这条线与 cpudebug 打包互不干扰。

**⑤ 打包前的两个准备动作**

- `copy_deps_file()`（[build.sh:113-121](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L113-L121)）：如果 build 目录里有 `simulator*.tar.gz` 或 `cann-asc-tools-cpudebug-deps*.tar.gz`，就拷到 `${CANN_3RD_LIB_PATH}`（默认 `./third_party`），让后续 CMake 能在本地找到闭源依赖，而不必联网下载。
- `clean()`（[build.sh:123-134](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L123-L134)）：每次构建都清空 `build/` 与 `build_out/`，保证产物干净。

#### 4.1.4 代码实践

**实践目标**：在不真正联网下载闭源依赖的前提下，看清 `--pkg` 到底往 CMake 传了哪些参数。

**操作步骤**：

1. 在仓库根目录执行 `bash build.sh --help --pkg`，观察 package 分支的帮助输出（[build.sh:37-54](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L37-L54)），确认 `--pkg` / `--pkg-type` / `--msot` 三个开关的用法。
2. 故意输入非法包类型 `bash build.sh --pkg --pkg-type=zip`，观察 `check_pkg_type` 的报错与退出。
3. 若本地已配好 CANN 环境，执行 `bash build.sh --pkg -j 8`，重点观察日志里 `cmake config ...` 那一行打印出的 `CUSTOM_OPTION` 完整内容。

**需要观察的现象**：

- 步骤 2 应输出 `[ERROR] Invalid value zip for option --pkg-type` 并退出码非 0。
- 步骤 3 的 `Info: cmake config ...` 行里应能看到 `-DBUILD_OPEN_PROJECT=ON -DPACKAGE_OPEN_PROJECT=ON ... -DPACKAGE_TYPE=run`。

**预期结果**：你能在 cmake config 日志里同时看到 `BUILD_OPEN_PROJECT`（永远 ON）与 `PACKAGE_OPEN_PROJECT`（仅 `--pkg` 时出现）两个开关，以及 `PACKAGE_TYPE=run`。

**待本地验证**：若本机没有匹配的 CANN 包与闭源依赖，步骤 3 会在配置期或依赖下载阶段失败——这属于环境问题，不影响你从日志里确认参数传递。即使失败，`cmake config` 那行日志通常已经打出。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `bash build.sh --pkg -t` 会报错？报错由哪段代码触发？
**答案**：`--pkg` 与 `-t/--test` 互斥。[build.sh:182-185](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L182-L185) 的 `check_help_combinations` 与 [build.sh:321-324](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L321-L324) 的 `check_param_test_pkg` 都会拦截，因为打包走 target `package`、测试走 target `all`，两者指向不同的 CMake 目标，不能同时生效。

**练习 2**：`bash build.sh`（不带任何参数）会产出 `.run` 包吗？
**答案**：会。因为既没有 `TEST` 也没有 `TEST_PART`，[build.sh:887-893](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L887-L893) 的分发会落到 `else build_package`，target 是 `package`。但此时没有 `PACKAGE_OPEN_PROJECT=ON`——这是 `build.sh` 默认行为与显式 `--pkg` 的细微差别，显式 `--pkg` 才会注入打包开关。

---

### 4.2 CMake install 规则：产物如何落盘到 tools/cpudebug

#### 4.2.1 概念说明

CPack 打包的第一步，是先让 CMake 把「该装的文件」收集到一棵**安装树**（staging 目录）。这棵树长什么样，完全由各 `CMakeLists.txt` 里的 `install()` 规则决定。本模块讲清楚：**cpudebug 编出来的那些 `.so`、头文件、cmake 配置，分别被装到了安装树的哪个子目录。**

回顾 u9-l1：`foreach(product_type ...)` 循环为每种架构各建了一个 `cpudebug_${product_type}` 目标，`OUTPUT_NAME` 统一是 `cpudebug`（即产物文件名都是 `libcpudebug.so`），但 `LIBRARY_OUTPUT_DIRECTORY` 是各自独立的 `${product_type}` 子目录。install 阶段要做的，就是把这 5~7 份同名但内容不同的 `.so`，分别铺到安装树的不同架构子目录里。

这里有一个关键映射：产品小写名（如 `ascend910B1`）和安装目录名（如 `Ascend910B1`）并不一致，靠 `product_dir()` 函数转换。

#### 4.2.2 核心流程

cpudebug 的 install 规则分三大块，都在 `foreach` 循环内或循环之后：

```text
foreach(product_type in PRODUCT_TYPE_LIST)        # 每种架构一份
  ├── (编译出 libcpudebug.so，见 u9-l1)
  ├── product_dir(${product_type} → Product_cap)   # ascend910B1 → Ascend910B1
  └── if(BUILD_OPEN_PROJECT)
        install(TARGETS cpudebug_${product_type}
                → tools/cpudebug/lib64/${Product_cap}/libcpudebug.so)
        install(CODE create_symlink libcpudebug.so → libtikcpp_debug.so)   # 详见 4.3

# 循环之后（与架构无关的公共文件）
install(FILES <22 个头文件> → tools/cpudebug/include/)
install(FILES libcpudebug_cceprint/npuchk/stubreg.so → tools/cpudebug/lib64/)
install(CODE ×3: 三个 libtikicpulib_* 软链)         # 详见 4.3
install(FILES cpudebug-config.cmake → tools/cpudebug/cmake/)
install(CODE ×3: 三个 tikicpulib-config 软链)        # 详见 4.3
```

要点：架构相关产物走「每架构一目录」，架构无关产物（头文件、cceprint/npuchk/stubreg 三个辅助 `.so`、cmake 配置）走公共目录 `tools/cpudebug/lib64`、`tools/cpudebug/include`、`tools/cpudebug/cmake`。

#### 4.2.3 源码精读

**① `product_dir()`：产品名 → 安装目录名**

[cpudebug/cmake/fun.cmake:38-63](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/cmake/fun.cmake#L38-L63) 是一张硬编码映射表，把构建用的产品名翻译成安装目录的大写名：

```cmake
function(product_dir str newstr)
  if("x${str}" STREQUAL "xascend910")
    set(${newstr} "Ascend910A" PARENT_SCOPE)
  ...
  elseif("x${str}" STREQUAL "xascend910b")
    set(${newstr} "Ascend910B1" PARENT_SCOPE)
  elseif("x${str}" STREQUAL "xascend950pr_9599")
    set(${newstr} "Ascend950PR_9599" PARENT_SCOPE)
  ...
```

这张表是「构建侧产品名」与「交付/样例侧目录名」之间的契约——样例里的 `find_package(ASC)` 和 `-DASCEND_COMPUTE_UNIT=` 用的就是这些大写目录名。

**② 每架构 install `libcpudebug.so`**

[cpudebug/CMakeLists.txt:122-137](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L122-L137) 是本模块的核心——先 `product_dir` 拿到目录名，再把 `.so` 装进去：

```cmake
product_dir(${product_type} Product_cap)
if(BUILD_OPEN_PROJECT)
  install(TARGETS cpudebug_${product_type}
    LIBRARY DESTINATION tools/cpudebug/lib64/${Product_cap} ${INSTALL_OPTIONAL}
    PERMISSIONS OWNER_READ OWNER_EXECUTE GROUP_READ GROUP_EXECUTE WORLD_READ WORLD_EXECUTE
    COMPONENT asc-tools
  )
  install(CODE
    "execute_process(
        COMMAND ${CMAKE_COMMAND} -E create_symlink
        libcpudebug.so
        libtikcpp_debug.so
        WORKING_DIRECTORY \"\${CMAKE_INSTALL_PREFIX}/tools/cpudebug/lib64/${Product_cap}\"
    )"
    COMPONENT asc-tools
  )
```

读懂这段需要明确三个概念：

- **`DESTINATION tools/cpudebug/lib64/${Product_cap}`**：安装目标子目录。`${CMAKE_INSTALL_PREFIX}` 是安装根（`build.sh` 里设成 `build_out/`），所以最终落盘到 `<安装根>/tools/cpudebug/lib64/Ascend910B1/libcpudebug.so` 这样的路径。
- **`COMPONENT asc-tools`**：所有 install 规则都打上同一个组件标签 `asc-tools`。CPack 打 run 包时按组件组织，这个标签让 asc-tools 的文件与 CANN 其它包（runtime、toolkit）区分开。
- **`PERMISSIONS ... OWNER_EXECUTE`**：`.so` 被赋予可执行权限，因为动态加载器加载 `.so` 时要求它可执行。

`install(CODE create_symlink libcpudebug.so libtikcpp_debug.so ...)` 那段会在「**刚装好 `libcpudebug.so` 的同一个目录里**」建一个指向它的软链 `libtikcpp_debug.so`。其意义留到 4.3 详讲。

> **闭源/开源边界提示**：注意 [cpudebug/CMakeLists.txt:138-143](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L138-L143) 的 `else()` 分支——当 `BUILD_OPEN_PROJECT=OFF`（CANN 源内构建）时，install 目标换成 `${INSTALL_LIBRARY_DIR}/${product_type}` 且**不建软链**。也就是说软链兼容层是「开源独立交付」专属，CANN 内部构建不需要它。

**③ 公共头文件与辅助 `.so`**

循环之外，[cpudebug/CMakeLists.txt:152-178](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L152-L178) 把 `kernel_fp16.h`、`cpu_debug_launch.h`、`tikicpulib.h` 等 22 个头文件装到 `tools/cpudebug/include`——这正是样例 `#include "cpu_debug_launch.h"` 能在安装后被找到的原因。

[cpudebug/CMakeLists.txt:245-252](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L245-L252) 装另外三个**架构无关**的辅助库（这三个库的内容与具体 NPU 型号无关，所以只装一份到公共 `lib64`）：

```cmake
install(FILES
  ${CMAKE_SOURCE_DIR}/libraries/lib/libcpudebug_cceprint.so   # 打印跟踪 stub
  ${CMAKE_SOURCE_DIR}/libraries/lib/libcpudebug_npuchk.so      # npu check stub
  ${CMAKE_SOURCE_DIR}/libraries/lib/libcpudebug_stubreg.so     # stub 注册引擎
  DESTINATION ${_install_path} ${INSTALL_OPTIONAL}
  ...
```

这三个 `.so` 对应 [u3-l3（Stub 注册）](u3-l3-stub-registration.md) 讲过的三类 stub 实现：`cceprint`（打印）、`npuchk`（运行时校验）、`stubreg`（注册引擎本体）。它们来自闭源 `libraries/lib/`，直接以现成文件形式装进去，不再编译。

**④ cmake 配置文件**

为了让样例的 `find_package(ASC REQUIRED)` 能找到 cpudebug，[cpudebug/CMakeLists.txt:180-192](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L180-L192) 用 `configure_package_config_file` 从模板 `tikicpulib-config.cmake.in` 生成 `cpudebug-config.cmake`，装到 `tools/cpudebug/cmake/`。`find_package` 的查找机制就是去这个目录读配置文件、定位头文件与库。

#### 4.2.4 代码实践

**实践目标**：从源码推断出「装完后 `tools/cpudebug/` 目录长什么样」，并对照 install 规则验证。

**操作步骤**：

1. 读 [cpudebug/CMakeLists.txt:146-150](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L146-L150)，确认 `_install_path` 在 `BUILD_OPEN_PROJECT` 下等于 `tools/cpudebug/lib64`。
2. 列出所有 `DESTINATION`，按目录归类：哪些文件进 `lib64/${Product_cap}`、哪些进 `lib64`、哪些进 `include`、哪些进 `cmake`。
3. 如果你本地已 `bash build.sh --pkg` 成功，执行 `find build_out/ -path '*tools/cpudebug*' | sort` 查看实际落盘结构，与自己画的图对比。

**需要观察的现象**：

- `lib64/` 下应有一个公共层（`libcpudebug_cceprint.so` 等 3 个 + 一堆软链），以及按架构分的子目录（`Ascend910B1/`、`Ascend310P1/`、`Ascend950PR_9599/` 等），每个架构子目录里都有一个 `libcpudebug.so` 和一个指向它的 `libtikcpp_debug.so`。
- `include/` 下应有 `cpu_debug_launch.h`、`tikicpulib.h`、`kernel_fp16.h` 等。

**预期结果**：实际目录结构与你从 `install()` 规则推断出的完全一致。

**待本地验证**：步骤 3 依赖一次成功打包；若打包未完成，可只做步骤 1-2 的「源码阅读型实践」，画出目录树草图。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `libcpudebug.so` 要按架构分目录装，而 `libcpudebug_cceprint.so` 只装一份？
**答案**：`libcpudebug.so` 里链接了闭源 `libcpudebug_model.a`，而该模型库是**按架构不同**的（不同 NPU 的指令仿真不一样，由 `__CCE_AICORE__`/`__DAV_`/`__NPU_ARCH__` 宏区分，见 u9-l1），所以每架构一份。而 `cceprint`/`npuchk`/`stubreg` 三个辅助库是架构无关的通用 stub 实现，一份即可被所有架构共用。

**练习 2**：`COMPONENT asc-tools` 这个标签在打包时起什么作用？
**答案**：它把所有 asc-tools 的文件归到同一个 CPack 组件。CPack 可以按组件分别打包或安装，`package.cmake` 里 `set_cann_cpack_config(asc-tools ...)` 就是按这个组件名配置包元信息；rpm 打包时 `CPACK_RPM_asc-tools_USER_FILELIST` 也用它来识别本组件的文件清单。

---

### 4.3 软链兼容层：为什么要有 libtikcpp_debug.so 这些别名

#### 4.3.1 概念说明

如果你数一下 `cpudebug/CMakeLists.txt` 里的 `install(CODE ... create_symlink ...)`，会发现整整 **6 处**（每架构 1 处 + 公共 3 处 cmake 配置相关）。本模块专门讲清楚这些软链**为什么存在**——这是本讲最重要的设计决策，也是本讲的实践任务所在。

核心动机一句话：**asc-tools 的库名经历过改名（`tikcpp` / `tikicpulib` → `cpudebug`），但大量存量样例、文档、第三方工程还在用旧库名链接。为了「改了源码库名却不破坏旧用户」，安装时同步建立一组从旧名指向新名的软链作为兼容层。**

理解这点需要知道改名史：

| 旧名（tikcpp 时代） | 新名（cpudebug 时代） | 性质 |
| --- | --- | --- |
| `libtikcpp_debug.so` | `libcpudebug.so` | 主仿真库（每架构一份） |
| `libtikicpulib_cceprint.so` | `libcpudebug_cceprint.so` | 打印 stub |
| `libtikicpulib_npuchk.so` | `libcpudebug_npuchk.so` | npuchk stub |
| `libtikicpulib_stubreg.so` | `libcpudebug_stubreg.so` | stub 注册引擎 |
| `tikicpulib-config.cmake` | `cpudebug-config.cmake` | find_package 配置 |
| `targets-tikicpulib.cmake` | `targets-cpudebug.cmake` | find_package 目标 |

新代码、新样例一律用新名 `cpudebug`；旧代码无需改动，因为安装后旧名作为软链依然存在，链接器和 `find_package` 透明地解析到新库。

#### 4.3.2 核心流程

软链的建立时机和位置很讲究——**总是在「真实文件刚装好之后、同一个目录内」建**，这样软链的相对目标一定有效：

```text
每架构目录 tools/cpudebug/lib64/${Product_cap}/:
   libcpudebug.so            ← 真实文件（install TARGETS 装的）
   libtikcpp_debug.so        ← 软链 → libcpudebug.so

公共目录 tools/cpudebug/lib64/:
   libcpudebug_cceprint.so   ← 真实文件
   libtikicpulib_cceprint.so ← 软链 → libcpudebug_cceprint.so
   libcpudebug_npuchk.so     ← 真实文件
   libtikicpulib_npuchk.so   ← 软链 → libcpudebug_npuchk.so
   libcpudebug_stubreg.so    ← 真实文件
   libtikicpulib_stubreg.so  ← 软链 → libcpudebug_stubreg.so

cmake 配置目录 tools/cpudebug/cmake/:
   cpudebug-config.cmake            ← 真实文件
   tikicpulib-config.cmake          ← 软链 → cpudebug-config.cmake
   targets-cpudebug.cmake           ← 真实文件
   targets-tikicpulib.cmake         ← 软链 → targets-cpudebug.cmake
   targets-cpudebug-release.cmake   ← 真实文件
   targets-tikicpulib-release.cmake ← 软链 → targets-cpudebug-release.cmake
```

注意 `create_symlink` 的参数顺序：`create_symlink <目标> <新建的名字>`，即「从 新名字 指向 已存在的目标」。这些软链用的是**相对名**（如 `libcpudebug.so` 而非绝对路径），所以整个 `tools/cpudebug` 目录可以被整体搬到别的位置（比如装机时从 staging 拷到 `/usr/local/Ascend/...`），软链依然有效——这是它能作为交付兼容层的关键。

#### 4.3.3 源码精读

**① 每架构主库软链**

已在 4.2.3 ② 引用，此处聚焦其语义：[cpudebug/CMakeLists.txt:129-137](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L129-L137) 的 `WORKING_DIRECTORY` 显式设成 `${CMAKE_INSTALL_PREFIX}/tools/cpudebug/lib64/${Product_cap}`，确保软链建在 `.so` 旁边。`create_symlink libcpudebug.so libtikcpp_debug.so` 即「新建 `libtikcpp_debug.so`，指向同目录的 `libcpudebug.so`」。

**② 三个公共 stub 库软链**

[cpudebug/CMakeLists.txt:254-282](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L254-L282) 是连续三段几乎同构的 `install(CODE)`，分别给 `cceprint`/`npuchk`/`stubreg` 建旧名软链。以 cceprint 为例：

```cmake
install(CODE
  "execute_process(
      COMMAND ${CMAKE_COMMAND} -E create_symlink
      libcpudebug_cceprint.so
      libtikicpulib_cceprint.so
      WORKING_DIRECTORY \"\${CMAKE_INSTALL_PREFIX}/${_install_path}\"
  )"
  COMPONENT asc-tools
)
```

`_install_path` 在开源模式下是 `tools/cpudebug/lib64`（[cpudebug/CMakeLists.txt:146-150](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L146-L150)），所以这三个软链与三个真实 `.so` 同处 `lib64` 根目录。

**③ cmake 配置软链**

[cpudebug/CMakeLists.txt:201-229](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L201-L229) 给 `find_package` 的配置文件也建了三组软链。这点常被忽略但很关键：旧样例若写 `find_package(tikicpulib)`，CMake 会去 `tools/cpudebug/cmake/` 找 `tikicpulib-config.cmake`——靠的就是这组软链把它解析到新的 `cpudebug-config.cmake`。

**④ 反向印证：rpm 的排除清单**

`cmake/package.cmake` 里有一段 rpm 打包的「排除清单」，从反面印证了这些软链确实存在于交付树：[cmake/package.cmake:82-115](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cmake/package.cmake#L82-L115) 的 `set_rpm_dynamic_path_excludes()` 把所有 `libtikcpp_debug.so`（每架构一个）、`libtikicpulib_cceprint.so`、`libtikicpulib_npuchk.so`、`libtikicpulib_stubreg.so`、`libascendc_acl_stub.so` 列为 `%exclude`。

```cmake
set(ASC_TOOLS_DYNAMIC_PATHS
    ...
    "tools/cpudebug/lib64/Ascend910B1/libtikcpp_debug.so"
    ...
    "tools/cpudebug/lib64/libtikicpulib_cceprint.so"
    "tools/cpudebug/lib64/libtikicpulib_npuchk.so"
    "tools/cpudebug/lib64/libtikicpulib_stubreg.so"
)
foreach(DYNAMIC_PATH IN LISTS ASC_TOOLS_DYNAMIC_PATHS)
    list(APPEND ASC_TOOLS_RPM_USER_FILELIST "%exclude ${ASC_TOOLS_INSTALL_PREFIX}/${DYNAMIC_PATH}")
endforeach()
```

为什么要 `%exclude`？因为 rpm/deb 安装时，**文件清单里不能出现「安装期才动态生成的软链」**——这些软链是 `.run` 包装好后由安装脚本现场 `ln -s` 建的（见 4.4），打包阶段它们只是「待生成」，不能写死进 rpm 的 `%files`。这份排除清单正好是「软链兼容层」存在的一份硬证据。

#### 4.3.4 代码实践（本讲主实践任务）

**实践目标**：通读 `cpudebug/CMakeLists.txt` 的全部 `install(CODE create_symlink)`，整理出完整的「新名 → 旧名」软链关系表，并解释为什么要建这些别名。

**操作步骤**：

1. 在 [cpudebug/CMakeLists.txt](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt) 中搜索所有 `create_symlink`，记录每处的「目标（真实文件）」「新建名（软链）」「所在目录」。
2. 把它们填进下面这张表（这里给出框架，请你自己补全）：

   | 真实文件（新名） | 软链（旧名） | 所在目录 | 行号 |
   | --- | --- | --- | --- |
   | `libcpudebug.so` | `libtikcpp_debug.so` | `lib64/${Product_cap}` | 129-137 |
   | `libcpudebug_cceprint.so` | ? | ? | ? |
   | ? | `libtikicpulib_npuchk.so` | ? | ? |
   | ? | ? | `tools/cpudebug/cmake` | 201-229 |

3. 回答两个问题：
   - 如果删掉这些软链，哪些场景会坏？（提示：旧样例的 `target_link_libraries(... tikcpp_debug)`、旧工程的 `find_package(tikicpulib)`）
   - 为什么软链用相对名（`libcpudebug.so`）而不是绝对路径？

**需要观察的现象**：

- 你应能在 `cpudebug/CMakeLists.txt` 里找到 **3 类共 6+ 处** `create_symlink`（每架构主库 1 处 × N 架构、公共 stub 库 3 处、cmake 配置 3 处）。
- 所有软链的目标都是「同目录下已 install 的真实文件」。

**预期结果**：补全后的表应覆盖 `libtikcpp_debug.so`、`libtikicpulib_cceprint.so`、`libtikicpulib_npuchk.so`、`libtikicpulib_stubreg.so`、`tikicpulib-config.cmake`、`targets-tikicpulib.cmake`、`targets-tikicpulib-release.cmake` 七个旧名。解释要点：**库名从 `tikcpp`/`tikicpulib` 改为 `cpudebug`，软链是向后兼容层，让旧代码无须改动即可链接到新库；用相对名是为了让整个目录可整体搬迁、软链仍有效。**

**待本地验证**：若已成功安装 asc-tools，可执行 `ls -l <安装路径>/tools/cpudebug/lib64/` 与 `ls -l <安装路径>/tools/cpudebug/lib64/Ascend910B1/`，亲眼看到软链的 `->` 指向。

#### 4.3.5 小练习与答案

**练习 1**：某旧样例的 CMake 里写了 `find_package(tikicpulib REQUIRED)`，在只装了新名 asc-tools 的环境下为何仍能跑通？
**答案**：因为 [cpudebug/CMakeLists.txt:201-229](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L201-L229) 安装了 `tikicpulib-config.cmake → cpudebug-config.cmake` 等软链。CMake 的 `find_package(tikicpulib)` 会在 `tools/cpudebug/cmake/` 找到 `tikicpulib-config.cmake`，透明解析到新配置，进而加载 `targets-tikicpulib.cmake`（同样软链到 `targets-cpudebug.cmake`），最终拿到的 import 目标就是新的 cpudebug 库。

**练习 2**：如果把 `create_symlink libcpudebug.so libtikcpp_debug.so` 的两个参数写反了，会发生什么？
**答案**：参数写反会变成「新建 `libcpudebug.so` 指向 `libtikcpp_debug.so`」。由于 `libtikcpp_debug.so` 此时尚不存在（它正是要被创建的），软链会指向一个空目标（dangling symlink），且可能覆盖刚 install 的真实 `libcpudebug.so`（若 `create_symlink` 强制覆盖），导致库损坏。这就是为什么理解 `create_symlink <目标> <新名>` 的参数顺序很重要。

---

### 4.4 CPack 打包配置与 .run 安装机制

#### 4.4.1 概念说明

前两模块讲的是「文件怎么进安装树」「软链怎么建」。本模块讲最后两步：**安装树怎么被封成 `.run` 自解压包**，以及**用户执行 `.run` 后，包怎么把自己铺进 CANN 路径**。

这里必须先厘清一个边界：**真正把目录封成 makeself `.run` 的逻辑不在 asc-tools 开源仓库里**，而在闭源的 cann-cmake 工具链中。asc-tools 的 `cmake/package.cmake` 只是**调用** cann-cmake 提供的宏（`npu_op_package` / `set_cann_cpack_config`）来「描述要打什么」，具体怎么打由 cann-cmake 决定。cann-cmake 由 [cmake/fetch_cann_cmake.cmake:24-31](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cmake/fetch_cann_cmake.cmake#L24-L31) 从 `gitcode.com/cann/cmake.git`（tag `master-034`）拉取。

至于装机脚本（`install.sh` 等），它们**是 asc-tools 自己的源码**（在 `scripts/package/asc-tools/scripts/`），在打包时被 `package.cmake` 装进包里，运行 `.run` 时被执行。

#### 4.4.2 核心流程

整条「打包 → 装机」链路：

```text
【打包侧：开发者的机器】
cmake --build . --target package
  └── CPack 按 package.cmake 配置:
        ① pack_built_in():  把 install.sh / version.info / 公共脚本装进安装树
        ② cann-cmake 的 makeself 封装: 把整棵安装树封成 cann-asc-tools_<ver>_linux-<arch>.run
              （.run = shell 脚本头 + 末尾压缩数据）

【装机侧：用户的机器】
./cann-asc-tools_*.run --full             # 用户执行
  └── makeself 自解压到临时目录
        └── 执行 install.sh               # 入口脚本（本仓库源码）
              ├── 解析 --full/--run/--devel/--pylocal/--install-path 等参数
              ├── checkArchitecture()      # 校验 .run 的架构 == 本机 uname -m
              ├── getInstallPath()         # 算出 <根>/cann/<版本> 多版本目录
              ├── checkVersion()           # 版本兼容性预检（来自 CANN）
              └── installRun()
                    └── run_asc-tools_install.sh --install <dir> <mode> ...
                          ├── installTool(): install_common_parser.sh --copy_all
                          │     把文件按 filelist.csv 铺到 <根>/cann/<版本>/
                          ├── installProfiling()（若有）
                          └── installModule(): 读 shells.info，依次跑 [install] 段脚本
                                └── asc-tools_custom_create_softlink.sh
                                      为 latest 目录补建 Python 工具 / tools 软链
```

理解这条链的关键是区分「**单版本安装**」与「**多版本安装**」：CANN 支持 `cann/<版本>/` 多版本共存，再由 `latest` 软链指向新装版本。`--pylocal` 这个开关会进一步把 Python 工具装到用户家目录的 site-packages（而非系统级），适合无 root 权限的开发者。

#### 4.4.3 源码精读

**① `pack_custom()`：run 包的身份与架构**

[cmake/package.cmake:12-33](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cmake/package.cmake#L12-L33) 先探测架构（`x86_64`/`aarch64`），再拼出包的身份名 `cann-asc-tools-${VENDOR_NAME}_linux-${ARCH}`，然后调用 cann-cmake 的 `npu_op_package` 声明「这是个 RUN 类型包」：

```cmake
if (CMAKE_SYSTEM_PROCESSOR MATCHES "x86_64")
    set(ARCH x86_64)
elseif (CMAKE_SYSTEM_PROCESSOR MATCHES "aarch64|arm64|arm")
    set(ARCH aarch64)
endif ()
set(PACK_CUSTOM_NAME "cann-asc-tools-${VENDOR_NAME}_linux-${ARCH}")
npu_op_package(${PACK_CUSTOM_NAME}
  TYPE RUN
  CONFIG
    ENABLE_SOURCE_PACKAGE True
    ENABLE_BINARY_PACKAGE True
    INSTALL_PATH ${CMAKE_INSTALL_PREFIX}/
    VENDOR_NAME ${PATH_NAME}           # 形如 xxx_tools
    ENABLE_DEFAULT_PACKAGE_NAME_RULE False
)
```

`TYPE RUN` 就是告诉 cann-cmake「用 makeself 产出自解压 run 包」。最终的 `.run` 文件名由 cann-cmake 结合 [version.cmake:11](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/version.cmake#L11) 的版本号 `9.1.0` 与这里的架构拼成，形如 `cann-asc-tools_<版本>_linux-<arch>.run`，落在 `build_out/`（见 [u1-l4](u1-l4-build-and-first-sample.md) 的观察）。

**② `pack_built_in()`：把装机脚本塞进包**

[cmake/package.cmake:117-238](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cmake/package.cmake#L117-L238) 是「往安装树里塞装机脚本与元信息」的部分。几个关键 install：

- [cmake/package.cmake:131-142](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cmake/package.cmake#L131-L142)：把 `scripts/package/asc-tools/scripts/` 下的所有 `.sh`（即 `install.sh`、`run_asc-tools_install.sh`、`asc-tools_custom_create_softlink.sh` 等）装到安装树的 `share/info/asc-tools/script`，并赋予可执行权限。这就是 `.run` 解压后能找到 `install.sh` 的原因。
- [cmake/package.cmake:144-157](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cmake/package.cmake#L144-L157)：把来自 CANN 包（`CANN_CMAKE_DIR/scripts/install/`）的公共安装脚本（`common_func.inc`、`common_interface.sh`、`version_compatiable.inc` 等）也拷进来——这些是 `install.sh` 运行时 `source` 的依赖。
- [cmake/package.cmake:179-184](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cmake/package.cmake#L179-L184)：把构建期生成的 `version.asc-tools.info` 改名 `version.info` 装进包，它是装机时版本校验与多版本目录命名的依据。
- [cmake/package.cmake:191-206](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cmake/package.cmake#L191-L206)：装 `compile_options_config.json`，并在 `compiler/conf/` 下为它建一个软链——这是又一个 `install(CODE create_symlink)` 的例子，目的是给编译器提供一个固定路径的配置入口。

**③ rpm/deb 的特殊处理**

当 `--pkg-type=rpm` 或 `deb` 时，[cmake/package.cmake:53-80](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cmake/package.cmake#L53-L80) 的 `patch_rpm_deb_package_generator()` 会调用 Python 脚本 `patch_cann_cmake_packaging.py` 去补丁 cann-cmake 的打包生成器；[cmake/package.cmake:234-236](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cmake/package.cmake#L234-L236) 再调用 `set_cann_cpack_config` 把 `PACKAGE_TYPE` 透传给 cann-cmake。配合 4.3 讲的 `set_rpm_dynamic_path_excludes()`（[cmake/package.cmake:235](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cmake/package.cmake#L235)），把动态软链排除出 rpm 文件清单。

**④ `.run` 入口：install.sh**

用户执行 `.run` 后，makeself 解压并调用 `install.sh`。它的参数解析（节选）：[scripts/package/asc-tools/scripts/install.sh:918-971](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/package/asc-tools/scripts/install.sh#L918-L971)

```bash
--full)   install_mode=$INSTALL_TYPE_ALL; install=y; full=y; ... ;;   # 全量安装（默认推荐）
--run)    install_mode=$INSTALL_TYPE_RUN; install=y; run=y; ... ;;    # 仅装运行态
--devel)  install_mode=$INSTALL_TYPE_DEV;  install=y; devel=y; ... ;; # 仅装开发态
--pylocal) pylocal=y; ... ;;        # Python 工具装到用户 site-packages
--install-path=*) input_install_path=... ;;  # 自定义安装根
```

`checkArchitecture()`（[install.sh:262-267](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/package/asc-tools/scripts/install.sh#L262-L267)）会比对 `.run` 内记录的 `arch`（来自 `scene.info`）与 `uname -m`，不一致直接拒绝——这就是为什么 x86 打的包不能在 aarch64 上装。

**⑤ 多版本安装路径的计算**

`getInstallPath()`（[install.sh:445-463](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/package/asc-tools/scripts/install.sh#L445-L463)）把安装根拼接成 `<根>/cann`：

```bash
getInstallPath() {
  if [ ! $input_path_flag = y ]; then
    input_install_path=$(getDefaultInstallPath)   # root→/usr/local/Ascend，用户→~/Ascend
  fi
  input_install_path=$(getInstallRealPath ${input_install_path})
  pkg_version_dir="cann"
  ...
  install_dir="${docker_root_path}${input_install_path}"
  if [ ! -z "$pkg_version_dir" ]; then
    install_dir="${install_dir}/${pkg_version_dir}"   # 最终：<根>/cann
  fi
}
```

`getDefaultInstallPath()`（[install.sh:465-477](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/package/asc-tools/scripts/install.sh#L465-L477)）按当前用户区分：root 默认 `/usr/local/Ascend`，普通用户默认 `~/Ascend`。最终文件落在 `<根>/cann/<版本>/tools/cpudebug/...`，与 4.2 讲的 `tools/cpudebug/lib64/...` 完美对接。

**⑥ 真正的文件铺放：run_asc-tools_install.sh**

`installRun()`（[install.sh:571-620](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/package/asc-tools/scripts/install.sh#L571-L620)）调用 `run_asc-tools_install.sh`，后者在 `installTool()` 里调用来自 CANN 的 `install_common_parser.sh --copy_all`，按包内 `filelist.csv` 把文件逐个铺到安装目录：[run_asc-tools_install.sh:123-168](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/package/asc-tools/scripts/run_asc-tools_install.sh#L123-L168)。

`installModule()`（[run_asc-tools_install.sh:194-223](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/package/asc-tools/scripts/run_asc-tools_install.sh#L194-L223)）则读 `shells.info` 的 `[install]` 段，依次执行列出的脚本——其中就包含 `asc-tools_custom_create_softlink.sh`。

**⑦ latest 目录软链**

多版本场景下，新版本装到 `<根>/cann/<新版本>/`，但用户工程通常指向稳定的 `<根>/latest`。[asc-tools_custom_create_softlink.sh:88-139](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/package/asc-tools/scripts/asc-tools_custom_create_softlink.sh#L88-L139) 的 `createToolSoftLink()` 负责把 `cpudebug`、`msobjdump`、`optype_collector`、`show_kernel_debug_data`、`tikicpulib`、`simulator` 等目录从 `<版本>/tools/` 软链到 `latest/tools/`；[asc-tools_custom_create_softlink.sh:39-65](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/package/asc-tools/scripts/asc-tools_custom_create_softlink.sh#L39-L65) 的 `createPythonSoftLink()` 则把三个 Python 工具软链到 `latest/python/site-packages/`。整段逻辑受 `is_multi_version_pkg` 守卫（[asc-tools_custom_create_softlink.sh:185-190](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/package/asc-tools/scripts/asc-tools_custom_create_softlink.sh#L185-L190)），单版本包直接跳过。

装机成功的提示里会打印一行 `TOOLCHAIN_HOME set with <根>/cann/<版本>/share/info/asc-tools`（[install.sh:597-598](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/package/asc-tools/scripts/install.sh#L597-L598)），提示样例编译依赖这个环境变量。

#### 4.4.4 代码实践

**实践目标**：追踪一个 `.run` 包从被执行到文件落盘的完整调用链，理解 makeself 自解压与多版本目录的形成。

**操作步骤**：

1. 阅读 [install.sh:851](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/package/asc-tools/scripts/install.sh#L851) 与 [install.sh:869-881](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/package/asc-tools/scripts/install.sh#L869-L881)，理解 `$1`（makeself 传入的包名）与 `$2`（run 文件路径）如何被解析。
2. 顺着 `main` 流程读 [install.sh:1024-1043](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/package/asc-tools/scripts/install.sh#L1024-L1043)：`getUserInfo` → `initLog` → `checkArchitecture` → `getInstallPath` → `checkVersion` → `startOperation`。
3. 若本地有现成的 `.run` 包，先执行 `./cann-asc-tools_*.run --version` 查看版本（[install.sh:930-933](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/package/asc-tools/scripts/install.sh#L930-L933)），再执行 `./cann-asc-tools_*.run --full --quiet` 装机，最后 `find <安装根>/cann -maxdepth 3 -type l` 查看所有软链。

**需要观察的现象**：

- `--version` 直接读包内 `version.info` 打印版本号后退出，不真正安装。
- `--full` 安装后，`<安装根>/cann/<版本>/tools/cpudebug/lib64/Ascend910B1/` 下应同时存在 `libcpudebug.so`（真实文件）与 `libtikcpp_debug.so`（软链）。
- `latest` 目录（或 `<安装根>/asc-tools/latest`，取决于版本布局）下应有指向各工具目录的软链。

**预期结果**：你能画出「`.run` → makeself 解压 → install.sh → run_asc-tools_install.sh → install_common_parser.sh 铺文件 → custom_create_softlink 建 latest 软链」的完整时序图。

**待本地验证**：`.run` 的执行需要真实的 CANN 环境与匹配架构；若本机无 NPU/CANN，步骤 3 无法完成，可只做步骤 1-2 的源码阅读型追踪。注意 `.run` 安装是** outward-facing 且会写系统路径**的操作，建议先 `--version`/`--check` 或装到自定义 `--install-path` 的临时目录里观察，避免影响系统 CANN。

#### 4.4.5 小练习与答案

**练习 1**：`--full`、`--run`、`--devel` 三种安装模式有何区别？为什么不能同时指定？
**答案**：它们对应 `INSTALL_TYPE_ALL`/`RUN`/`DEV` 三种 `install_mode`，决定装哪些 feature（`filelist.csv` 里按 feature 分组）。[install.sh:270-276](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/package/asc-tools/scripts/install.sh#L270-L276) 的 `checkOperation()` 校验「不能同时指定多个 install mode」，因为一次安装只能有一个 install_mode。

**练习 2**：为什么 `cmake/package.cmake` 要把 `libtikcpp_debug.so` 等软链加入 rpm 的 `%exclude` 清单？
**答案**：见 4.3.3 ④——这些软链是装机脚本（`install_common_parser.sh` / `asc-tools_custom_create_softlink.sh`）在现场动态生成的，不在打包期的静态文件清单里。rpm 的 `%files` 段要求文件在打包时即确定，若把动态软链写进去，rpm 安装时会因找不到源文件而失败。`%exclude` 告诉 rpm「这些路径由 postinst 脚本负责，不要打包」。

---

## 5. 综合实践

设计一个贯穿本讲的任务：**画一张「从 `bash build.sh --pkg` 到样例能用上 cpudebug」的全链路图，并标注每一环对应的源码文件与行号。**

具体步骤：

1. **打包环**：从 [build.sh:872-893](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L872-L893) 出发，标注 `PACKAGE_OPEN_PROJECT=ON` 与 `PACKAGE_TYPE=run` 如何传到 CMake，再到 `cmake --build . --target package`。
2. **install 环**：在 [cpudebug/CMakeLists.txt:122-143](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L122-L143) 标注每架构 `libcpudebug.so` 的落盘与 `libtikcpp_debug.so` 软链；在 [cpudebug/CMakeLists.txt:245-282](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L245-L282) 标注三个 stub 库的落盘与旧名软链。
3. **封装环**：在 [cmake/package.cmake:117-240](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cmake/package.cmake#L117-L240) 标注装机脚本如何被打进包，并指出 makeself 封装发生在闭源 cann-cmake 中。
4. **装机环**：在 [install.sh](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/package/asc-tools/scripts/install.sh) 标注 `--full` → `getInstallPath`（多版本 `cann/` 目录）→ `installRun` → `run_asc-tools_install.sh` 的调用链。
5. **验证环**：装机后，写一个最小样例分别用新名 `target_link_libraries(... cpudebug)` 与旧名 `... tikcpp_debug` 链接，验证两者都能跑通——以此证明软链兼容层生效。

**验收标准**：你的图能让另一个没读过本讲的人，仅凭图上的文件路径与行号，自己定位到每一步的源码；步骤 5 的两种链接方式都能编译通过。

## 6. 本讲小结

- `bash build.sh --pkg` 的本质是往 CMake 注入 `-DPACKAGE_OPEN_PROJECT=ON -DPACKAGE_TYPE=run`，再走 target `package` 触发 CPack；`--pkg-type` 只允许 `run/rpm/deb`，`--msot` 必须配 `--pkg` 且走独立的子仓构建早退分支。
- `cpudebug/CMakeLists.txt` 的 install 规则把每架构 `libcpudebug.so` 装进 `tools/cpudebug/lib64/${Product_cap}`（`Product_cap` 由 `product_dir()` 把 `ascend910B1` 翻译成 `Ascend910B1`），架构无关的头文件与三个 stub 库装进公共 `include`/`lib64`。
- asc-tools 的库名从 `tikcpp`/`tikicpulib` 改为 `cpudebug`，安装时用 6+ 处 `install(CODE create_symlink)` 建立从旧名到新名的软链兼容层，使旧样例与旧工程无须改动即可链接到新库；软链用相对名，保证整个 `tools/cpudebug` 目录可整体搬迁。
- 真正的 makeself `.run` 封装逻辑在闭源 cann-cmake 工具链（由 `fetch_cann_cmake.cmake` 拉取的 `cann/cmake` 仓库），`cmake/package.cmake` 只负责描述「打什么、装哪些脚本」；rpm/deb 模式还会补丁打包生成器并把动态软链加入 `%exclude`。
- `.run` 安装链路是 `install.sh`（入口、解析 `--full/--run/--pylocal`、校验架构、算多版本路径）→ `run_asc-tools_install.sh`（调 `install_common_parser.sh --copy_all` 按 `filelist.csv` 铺文件）→ `asc-tools_custom_create_softlink.sh`（为 `latest` 目录补建工具与 Python 软链）；最终文件落在 `<安装根>/cann/<版本>/tools/cpudebug/...`。

## 7. 下一步学习建议

- 下一讲 [u9-l3（单元测试体系）](u9-l3-unit-testing.md) 会转向 `build.sh` 的另一条分支——`-t/--cpp_utest/--python_utest`，讲清 C++ UT 与 Python UT 的组织结构、`TEST_MOD` 分发与 `--cov`/`--asan` 选项。它与本讲的打包分支互斥，正好构成 `build.sh` 的「测试 vs 打包」两条主线。
- 若你想更深入装机脚本，建议直接通读 [scripts/package/asc-tools/scripts/](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/scripts/package/asc-tools/scripts/) 下的 `common.sh`、`uninstall.sh`、`cleanup.sh`，它们与 `install.sh` 共享同一套来自 CANN 的 `common_func.inc` 工具函数。
- 若你对 makeself 本身感兴趣，可以阅读 `utils/templates/op_project_templates/ascendc/customize/cmake/makeself.cmake`——自定义算子工程也用 makeself 自打 run 包，机制与本章一致，只是规模更小、更易看懂全貌。
- 想理解 `find_package(ASC)` 如何消费本讲安装的 `cpudebug-config.cmake`，可回顾 [u1-l4](u1-l4-build-and-first-sample.md) 的样例编译闭环，并阅读 `cpudebug/cmake/tikicpulib-config.cmake.in` 模板。
