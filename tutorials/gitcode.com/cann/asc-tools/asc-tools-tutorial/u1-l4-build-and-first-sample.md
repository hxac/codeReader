# 一键编译与运行第一个样例

## 1. 本讲目标

在前几讲里，你已经知道 asc-tools 是「一个 C++ 核心（cpudebug）+ 四个 Python 工具」，并且搭好了 CANN 编译/运行环境（前置讲义 u1-l3）。本讲的目标是亲手把这套工具**从源码变成可用的软件包，并用它跑通第一个算子样例**，完成「编译 → 安装 → 运行验证」的完整闭环。

学完后你应该能够：

1. 看懂 `build.sh` 这个一键编译脚本的常用参数，知道 `--pkg`、`--pkg-type`、`-p`、`-j` 分别做什么。
2. 说出 `bash build.sh --pkg` 之后的产物在哪里、如何安装它、安装时建立了哪些软链。
3. 在 `examples/02_cpudebug` 样例目录里，用 `cmake -DCMAKE_ASC_RUN_MODE=cpu` 编译并运行 `add` 样例，看懂它的成功输出。
4. 建立一条心智模型：**先编译安装 asc-tools（提供 CPU 域运行库）→ 再编译运行算子样例（依赖这些库）**，两步缺一不可。

## 2. 前置知识

本讲默认你已经掌握前置讲义 u1-l2（目录结构）和 u1-l3（环境搭建）的内容。这里再补几个本讲要用到的基础概念：

- **构建（build）**：把人类写的源码（C++/Python）翻译成机器能运行的二进制库或可执行文件的过程。asc-tools 用 **CMake** 来管理这个翻译过程，再由 `build.sh` 脚本去调用 CMake。
- **CMake**：一个跨平台的「构建系统生成器」。它本身不编译代码，而是根据 `CMakeLists.txt` 文件，生成真正的编译指令（如 Makefile）。所以你会经常看到 `cmake ..` 然后 `make` 的两步操作。
- **run 包**：CANN 体系里的一种自解压安装包，后缀是 `.run`。它本质上是一个 shell 脚本 + 打包好的文件，执行时会自解压并把内容安装到指定路径。asc-tools 编译后的产物就被打包成 `cann-asc-tools_*.run`。
- **软链（symbolic link / symlink）**：类似 Windows 的快捷方式，一个文件名指向另一个文件。本讲会看到安装阶段用软链给库文件起「别名」。
- **CPU 域运行（CMAKE_ASC_RUN_MODE=cpu）**：让 Ascend C 算子源码不经过真实 NPU，而是经过 GCC 编译后在 CPU 上跑起来（即「孪生调试」，详见后续 u2 单元）。本讲的 `add` 样例就是 CPU 域运行。
- **SOC / 架构（dav-2201、dav-3510）**：不同型号 NPU 对应不同的内部架构代号。CPU 仿真时也要指明一个目标架构，因为不同架构的内存、向量位宽约束不同。

> 一句话理解：本讲要做的事情 = 「把工具本身装好」+「用装好的工具跑一个示范算子」。前者是 `build.sh --pkg`，后者是 `cmake .. && make && ./add`。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [build.sh](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh) | 一键编译/打包/测试的入口脚本。本讲最核心的文件。 |
| [docs/00_quick_start.md](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/00_quick_start.md) | 官方快速入门文档，给出 `build.sh --pkg` 与安装 run 包的权威步骤。 |
| [version.cmake](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/version.cmake) | 定义 asc-tools 的版本号（`9.1.0`），决定 run 包文件名中的版本段。 |
| [cmake/package.cmake](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cmake/package.cmake) | 由根 CMakeLists 引入，负责用 CPack 把编译产物打包成 `.run`/`.rpm`/`.deb`。 |
| [CMakeLists.txt](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/CMakeLists.txt) | 根构建文件，引入 `package.cmake` 并按 `add_subdirectory` 串起各模块。 |
| [cpudebug/CMakeLists.txt](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt) | cpudebug 核心库的构建与安装规则，包含安装阶段的软链定义。 |
| [examples/02_cpudebug/README.md](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/README.md) | add 样例说明，给出样例编译运行命令与预期结果。 |
| [examples/02_cpudebug/CMakeLists.txt](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/CMakeLists.txt) | add 样例的构建文件，体现 `ASC` 语言的特殊处理。 |
| [examples/02_cpudebug/add.asc](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc) | add 样例源码：包含 Kernel 实现 + 主机侧 `main` 验证逻辑。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**build.sh 用法**、**run 包安装**、**样例编译运行**。三者是递进关系——先会用脚本编译，再会安装产物，最后用装好的库跑通样例。

### 4.1 build.sh 用法

#### 4.1.1 概念说明

`build.sh` 是 asc-tools 仓库根目录下的一个 bash 脚本，是整个项目**唯一的编译入口**。它把「找 CANN 包 → 配置 CMake → 编译 → 打包/测试」这一长串步骤封装成一条命令，屏蔽了底层 CMake 的复杂参数。

为什么需要它？因为 asc-tools 的构建依赖很多外部条件：必须先找到版本匹配的 CANN 安装路径、必须设置正确的第三方库路径、还要区分「只编译」「打 run 包」「跑单元测试」等不同目标。如果让用户直接写 `cmake` 命令，参数会非常长且容易出错。`build.sh` 把这些「环境探测 + 参数拼装」的脏活全包了。

#### 4.1.2 核心流程

`build.sh` 的执行主线在 `main()` 函数里，可以用下面的伪代码概括：

```text
main():
    1. 校验命令行参数合法性 (check_param_with_help)
    2. 解析参数并设置开关变量 (set_options)   # 如 PKG / TEST / ASAN / COV / THREAD_NUM
    3. 若 --pkg --msot：单独构建 msot 包后退出
    4. set_env()   # 按优先级链找到 CANN 安装路径 → ASCEND_CANN_PACKAGE_PATH
    5. copy_deps_file()  # 把已下载的闭源依赖包拷到三方库目录
    6. clean()     # 清空 build/ 和 build_out/
    7. 根据开关拼装 CUSTOM_OPTION：
         - TEST  → 加 -DENABLE_TEST=ON -DTEST_MOD=all，构建类型 Debug
         - PKG   → 加 -DPACKAGE_OPEN_PROJECT=ON
         - ASAN  → 加 -DENABLE_ASAN=true
         - COV   → 加 -DENABLE_GCOV=true
         - 末尾统一加 ASCEND_CANN_PACKAGE_PATH / CANN_3RD_LIB_PATH / BUILD_TYPE / PACKAGE_TYPE
    8. cd build/，根据目标调用：
         - TEST     → build_test()   (cmake + build target all)
         - TEST_PART→ build_test_part()
         - 否则     → build_package() (cmake + build target package)
```

关键在于第 8 步的**分派**：`build_package()` 走的是 CMake 的 `package` 目标（会触发 CPack 打包），而 `build_test()` 走的是 `all` 目标（只编译不打包）。所以 `--pkg` 和 `--test` 是互斥的。

#### 4.1.3 源码精读

**(1) 脚本开头：默认路径与开关**

[build.sh:L19-L28](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L19-L28) 定义了几个贯穿全脚本的变量，理解它们就掌握了产物去哪、依赖在哪：

- `BUILD_DIR=${CURRENT_DIR}/build` 是 CMake 的中间构建目录（编译过程中的临时文件）。
- `OUTPUT_DIR=${CURRENT_DIR}/build_out` 是最终 run 包的输出目录——编译成功后你要去这里找 `.run` 文件。
- `CANN_3RD_LIB_PATH=${CURRENT_DIR}/third_party` 是第三方/闭源依赖的默认存放目录。
- `BUILD_TYPE` 默认 `Release`，`PACKAGE_TYPE` 默认 `run`。

注意脚本第 12 行 `set -e` 与第 896 行 `set -o pipefail`：任何一条命令失败都会让脚本立刻退出，所以一旦中途报错，不会有「半成品产物」。

**(2) 支持的参数清单**

[build.sh:L14-L17](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L14-L17) 列出了所有合法选项：

- 短选项：`h`（帮助）、`j`（线程数）、`t`（测试）、`p`（CANN 路径）。
- 长选项里与本讲最相关的是 `pkg`（打 asc-tools 包）、`pkg-type`（包类型 run/rpm/deb）、`cann_path`、`cann_3rd_lib_path`、`build-type`、`msot`、`asan`、`cov`。

输入未定义的选项会被 [build.sh:L452-L456](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L452-L456) 拦截并报 `[ERROR] Undefined option`。

**(3) `--pkg` 与 `--pkg-type` 的解析**

[build.sh:L389-L393](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L389-L393) 处理 `--pkg`：把开关变量 `PKG` 置为 `true`，并立即调用 `check_param_test_pkg` 检查它没有和 `--test` 冲突。

[build.sh:L442-L451](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L442-L451) 处理 `--pkg-type`：把值赋给 `PACKAGE_TYPE`，再由 [build.sh:L304-L311](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L304-L311) 校验它只能是 `run`/`rpm`/`deb`，否则报错退出。这就是为什么 `bash build.sh --help --pkg` 会给出 `--pkg-type=<TYPE>` 的合法取值说明。

**(4) `main()` 里拼装参数与分派**

在 `main()` 中，[build.sh:L872-L874](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L872-L874) 说明：当 `--pkg` 打开时，会给 CMake 加一个 `-DPACKAGE_OPEN_PROJECT=ON`。这个宏在根 [CMakeLists.txt:L39-L49](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/CMakeLists.txt#L39-L49) 里是引入 `package.cmake`、启用打包相关逻辑的开关。

最后 [build.sh:L876](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L876) 把 `ASCEND_CANN_PACKAGE_PATH`、`CANN_3RD_LIB_PATH`、`BUILD_TYPE`、`PACKAGE_TYPE` 四个关键变量统一追加到 `CUSTOM_OPTION`，再由 [build.sh:L241-L257](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L241-L257) 的 `cmake_config()`/`build()`/`build_package()` 实际执行 `cmake ..` 与 `cmake --build . --target package`。

[build.sh:L887-L893](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L887-L893) 就是第 8 步的分派：有 `TEST` 跑 `build_test`，有 `TEST_PART` 跑 `build_test_part`，否则（包括 `--pkg`）跑 `build_package`。

#### 4.1.4 代码实践

**实践目标**：在不真正触发完整编译的前提下，熟悉 `build.sh` 的帮助系统和参数校验。

**操作步骤**：

1. 进入仓库根目录，执行帮助命令：
   ```bash
   bash build.sh --help          # 通用帮助
   bash build.sh --help --pkg    # 打包相关的帮助（更聚焦）
   bash build.sh -h --pkg        # 短选项 -h 等价于 --help
   ```
2. 故意输入非法包类型，观察校验：
   ```bash
   bash build.sh --pkg --pkg-type=apk   # apk 不在 run/rpm/deb 之内
   ```
3. 故意把互斥选项组合在一起，观察冲突检查：
   ```bash
   bash build.sh --pkg -t               # --pkg 与 --test 互斥
   ```

**需要观察的现象**：

- 第 1 步会看到 `Package Build Options` 段落，列出 `--pkg`、`--pkg-type`、`-p`、`-j`、`--asan`、`--cann_3rd_lib_path` 等说明，并给出 `Examples`。
- 第 2 步应输出 `[ERROR] Invalid value apk for option --pkg-type` 并退出。
- 第 3 步应输出 `[ERROR] --pkg cannot be used with test(-t, --test).` 并退出。

**预期结果**：帮助文本与 [build.sh:L37-L54](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L37-L54) 完全对应；非法/互斥输入都能被前置校验拦下。**实际输出待本地验证。**

#### 4.1.5 小练习与答案

**练习 1**：`bash build.sh --pkg -j 32 --asan` 这条命令的实际效果是什么？`-j 32` 超过本机 CPU 核数会怎样？

**参考答案**：它会在 `Release` 模式下、开启 ASAN（地址错误检测）、用 32 个线程编译 asc-tools 并打成 run 包。`-j 32` 若超过本机核数，[build.sh:L292-L302](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L292-L302) 的 `check_param_j` 会打印 `[WARNING]` 并把线程数下调到核数，不会报错。

**练习 2**：为什么 `--pkg` 和 `-t` 不能同时使用？从源码找出依据。

**参考答案**：因为打包走 CMake 的 `package` 目标，测试走 `all` 目标，两者构建逻辑不同。脚本在 [build.sh:L320-L333](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L320-L333) 的 `check_param_test_pkg` 与 [build.sh:L182-L190](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L182-L190) 的 `check_help_combinations` 两处都做了互斥拦截。

---

### 4.2 run 包安装

#### 4.2.1 概念说明

「run 包」是 CANN 体系标准的交付物形态。`bash build.sh --pkg` 跑完后，你拿到的不是一个散装的目录，而是一个自包含的 `cann-asc-tools_<版本>_linux-<架构>.run` 文件。这个文件本身是一个 shell 脚本（头部）+ 一段压缩数据，执行它会自解压、再调用内部的 `install.sh` 把内容铺到 CANN 的安装路径下。

为什么要把 asc-tools 单独打成 run 包再安装？因为 asc-tools 是 CANN 的**配套补充工具**，它安装时会「覆盖/增强」CANN 安装路径下的 Ascend C 相关内容（头文件、cpudebug 库、脚本等）。run 包机制让这种覆盖安装可追溯、可重放、可分发。

#### 4.2.2 核心流程

run 包从生成到安装的完整链路：

```text
build.sh --pkg
   │
   ├─ cmake --build . --target package
   │       └─ 触发 cmake/package.cmake 的 pack_built_in()
   │              ├─ 检测架构 (x86_64 / aarch64)
   │              ├─ install(...) 规则登记要打包的文件
   │              └─ set_cann_cpack_config(...) → CPack 生成 .run
   │
   ▼
build_out/cann-asc-tools_<版本>_linux-<架构>.run   （产物）
   │
   ▼ 用户执行
./cann-asc-tools_<版本>_linux-<架构>.run --full --pylocal
   │
   ├─ 自解压到临时目录
   ├─ 运行内置 install.sh，按 INSTALL_PATH 安装
   └─ 在 tools/cpudebug/lib64/ 下创建若干软链（别名）
```

这里有两个关键事实：run 包的**文件名**由版本号和架构拼成；安装时会在目标目录**建立软链**，让新旧库名都能被找到。

#### 4.2.3 源码精读

**(1) 版本号决定文件名中的版本段**

[version.cmake:L11-L12](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/version.cmake#L11-L12) 设定版本：

```cmake
set(ASC_TOOLS_VERSION "9.1.0")
set_cann_package(asc-tools VERSION ${ASC_TOOLS_VERSION})
```

所以 run 包文件名里的 `<cann_version>` 段对应 `9.1.0`（与配套的 CANN 主版本一致）。文档里写的 `cann-asc-tools_*<cann_version>*_linux-*<arch>*.run`（[docs/00_quick_start.md:L372](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/00_quick_start.md#L372)）展开后形如 `cann-asc-tools_9.1.0_linux-x86_64.run`。

**(2) 打包逻辑：检测架构 + 命名**

[cmake/package.cmake:L12-L33](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cmake/package.cmake#L12-L33) 的 `pack_custom()` 函数做了两件事：先用 `CMAKE_SYSTEM_PROCESSOR` 判断是 `x86_64` 还是 `aarch64`，再据此拼出包名 `cann-asc-tools-${VENDOR_NAME}_linux-${ARCH}`，最后调用 CANN 提供的 `npu_op_package(...)` 完成 RUN 类型打包配置。这正是文件名里 `<arch>` 段的来源。

**(3) 安装阶段的软链：给库起「别名」**

这是本模块最值得理解的一点。asc-tools 把历史上叫 `tikcpp`/`tikicpulib` 的库重命名成了 `cpudebug` 系列，但为了不破坏老样例和老工具对旧库名的引用，安装时会用软链保留旧名。看 [cpudebug/CMakeLists.txt:L129-L137](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L129-L137)：

```cmake
install(CODE
  "execute_process(
      COMMAND ${CMAKE_COMMAND} -E create_symlink
      libcpudebug.so
      libtikcpp_debug.so
      WORKING_DIRECTORY \"\${CMAKE_INSTALL_PREFIX}/tools/cpudebug/lib64/${Product_cap}\"
  )"
  ...
)
```

这段在安装目录的 `tools/cpudebug/lib64/<产品>/` 下创建 `libtikcpp_debug.so → libcpudebug.so` 的软链。同理，[cpudebug/CMakeLists.txt:L254-L261](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L254-L261) 等处还会建立 `libtikicpulib_cceprint.so → libcpudebug_cceprint.so`、`libtikicpulib_npuchk.so → libcpudebug_npuchk.so`、`libtikicpulib_stubreg.so → libcpudebug_stubreg.so` 等软链。

> 直觉理解：`cpudebug` 是「新大名」，`tikcpp_debug`/`tikicpulib_*` 是「旧字」。安装时挂上软链，相当于让旧字也能找到人，保证老代码不用改就能继续链接。

**(4) 文档给出的安装命令**

[docs/00_quick_start.md:L379-L390](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/00_quick_start.md#L379-L390) 给出权威安装步骤：

```bash
cd build_out
# 默认路径安装
./cann-asc-tools_<cann_version>_linux-<arch>.run --full --pylocal
# 指定路径安装
./cann-asc-tools_<cann_version>_linux-<arch>.run --full --pylocal --install-path=${install_path}
```

其中 `--full` 表示完整安装，`--pylocal` 表示把 Python 工具装到用户级目录。安装目标路径默认是 CANN 的装包路径（由环境变量 `ASCEND_HOME_PATH` 等决定），安装会覆盖原 CANN 包中的 Ascend C 内容。

#### 4.2.4 代码实践

**实践目标**：完成 run 包的生成与安装，并验证软链确实被建立。

**操作步骤**：

1. 在仓库根目录编译出 run 包（联网环境）：
   ```bash
   bash build.sh --pkg
   ```
   若离线，按文档 [docs/00_quick_start.md:L367-L370](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/00_quick_start.md#L367-L370) 改用：
   ```bash
   bash build.sh --pkg --cann_3rd_lib_path={your_3rd_party_path}
   ```
2. 编译完成后，到产物目录查看 run 包：
   ```bash
   ls -1 build_out/cann-asc-tools_*.run
   ```
3. 安装 run 包：
   ```bash
   cd build_out
   ./cann-asc-tools_9.1.0_linux-$(uname -m).run --full --pylocal
   ```
4. 验证软链是否建立（路径中的 `<Product_cap>` 替换为实际产品，如 `Ascend910B1`）：
   ```bash
   ls -l ${ASCEND_HOME_PATH}/../tools/cpudebug/lib64/Ascend910B1/ | grep tikcpp
   ls -l ${ASCEND_HOME_PATH}/../tools/cpudebug/lib64/ | grep tikicpulib
   ```

**需要观察的现象**：

- 第 2 步应列出一个（或多个，若多架构）`.run` 文件，文件名包含 `9.1.0` 和架构。
- 第 3 步安装过程会打印解压与安装日志，末尾提示安装成功。
- 第 4 步应看到 `libtikcpp_debug.so -> libcpudebug.so`、`libtikicpulib_cceprint.so -> libcpudebug_cceprint.so` 等软链条目（`ls -l` 输出里的 `->`）。

**预期结果**：run 包生成于 `build_out/`，安装后 CANN 路径下出现 `cpudebug` 库及其软链别名。**完整的编译/安装过程依赖真实 CANN 环境，待本地验证。**

#### 4.2.5 小练习与答案

**练习 1**：run 包文件名 `cann-asc-tools_9.1.0_linux-x86_64.run` 中的 `9.1.0` 和 `x86_64` 分别由哪个文件、哪段代码决定？

**参考答案**：`9.1.0` 来自 [version.cmake:L11](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/version.cmake#L11) 的 `ASC_TOOLS_VERSION`；`x86_64` 来自 [cmake/package.cmake:L14-L22](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cmake/package.cmake#L14-L22) 对 `CMAKE_SYSTEM_PROCESSOR` 的判断。

**练习 2**：如果不建立 `libtikcpp_debug.so → libcpudebug.so` 这个软链，可能会出现什么问题？

**参考答案**：那些仍按旧库 `tikcpp_debug` 名字去链接或 `dlopen` 的老样例、老工具，会在运行/编译时找不到符号或库文件（`cannot find -ltikcpp_debug`）。软链是新旧命名之间的兼容层。

**练习 3**：`--full` 和 `--pylocal` 是 `build.sh` 的参数吗？

**参考答案**：不是。它们是 **run 包自身**（自解压脚本）的安装参数，传给 run 包内部的 `install.sh`，而不是 `build.sh`。`build.sh` 只负责「生成」run 包，不负责「安装」它。

---

### 4.3 样例编译运行

#### 4.3.1 概念说明

装好 asc-tools 之后，它的价值要通过「跑一个算子」体现出来。仓库的 `examples/02_cpudebug` 就是最小可运行的样板：一个 Ascend C 的 Add 算子（`z = x + y`），用 CPU 域模式编译运行。

这个样例的特殊之处在于：它的源码文件后缀是 `.asc`（不是 `.cpp`），构建文件里启用了一种叫 `ASC` 的语言（[examples/02_cpudebug/CMakeLists.txt:L16](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/CMakeLists.txt#L16) 的 `LANGUAGES ASC CXX`）。这是因为 `.asc` 文件里混合了「核函数（Kernel，运行在 NPU/CPU 仿真上）」和「主机侧验证代码（`main`，运行在 CPU 上）」两部分，需要 ASC 工具链做专门的源码转译。

#### 4.3.2 核心流程

样例从源码到验证通过的流程：

```text
examples/02_cpudebug/
   ├─ add.asc                 # 源码：KernelAdd 类 + add_custom 核函数 + main 验证
   └─ CMakeLists.txt          # 用 ASC 语言把 add.asc 编成可执行文件 add
            │
            ▼  cmake -DCMAKE_ASC_RUN_MODE=cpu ..
       生成 build/Makefile
            │
            ▼  make -j
       生成可执行文件 ./add
            │
            ▼  ./add
       在 CPU 上仿真 8 个 block，计算 z=x+y，与 golden 比对
            │
            ▼
   打印 [Success] Case accuracy is verification passed.
```

`CMAKE_ASC_RUN_MODE=cpu` 是关键开关：它告诉 ASC 工具链「不要编译成真 NPU 二进制，而是编译成能在 CPU 上跑、且链接到 cpudebug 仿真库的程序」。这正是上一模块安装 `libcpudebug.so` 的用武之地——样例运行时依赖它。

#### 4.3.3 源码精读

**(1) 样例构建文件**

[examples/02_cpudebug/CMakeLists.txt:L14-L22](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/CMakeLists.txt#L14-L22) 非常短，但信息量很大：

```cmake
find_package(ASC REQUIRED)
project(cpudebug LANGUAGES ASC CXX)
add_executable(add ${CMAKE_CURRENT_SOURCE_DIR}/add.asc)
target_compile_options(add PRIVATE
    $<$<COMPILE_LANGUAGE:ASC>:--npu-arch=${CMAKE_ASC_ARCHITECTURES}>
)
```

- `find_package(ASC REQUIRED)`：找到 CANN 提供的 ASC 工具链包（由 `source set_env.sh` 后才能定位）。找不到会直接报错——这说明样例**强依赖已安装的 CANN/asc-tools**。
- `LANGUAGES ASC CXX`：声明这个工程同时使用 ASC（核函数转译）和 CXX（普通 C++）两种语言。
- `add_executable(add ... add.asc)`：把 `.asc` 直接作为可执行目标 `add` 的源文件。
- `target_compile_options`：仅对 ASC 语言追加 `--npu-arch=${CMAKE_ASC_ARCHITECTURES}`，即把目标 NPU 架构（如 `dav-2201`）透传给编译器。

**(2) 编译运行命令与选项**

[examples/02_cpudebug/README.md:L67-L78](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/README.md#L67-L78) 给出标准命令：

```bash
mkdir -p build && cd build;
cmake -DCMAKE_ASC_RUN_MODE=cpu -DCMAKE_ASC_ARCHITECTURES=dav-2201 ..;
make -j;
./add
```

选项含义见同处的编译选项表：`CMAKE_ASC_RUN_MODE=cpu` 表示 CPU 调试模式；`CMAKE_ASC_ARCHITECTURES` 默认 `dav-2201`（对应 Atlas A2/A3 系列），也可选 `dav-3510`（对应 Ascend 950PR/950DT）。

**(3) 样例源码结构**

`add.asc` 把「算子」和「测试程序」写在同一个文件里。[add.asc:L90-L95](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L90-L95) 是核函数入口：

```cpp
__global__ __vector__ void add_custom(GM_ADDR x, GM_ADDR y, GM_ADDR z)
{
    KernelAdd op;
    op.Init(x, y, z);
    op.Process();
}
```

`__global__ __vector__` 标记它是一个向量核函数，会在每个 block 上各执行一次。[add.asc:L123](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L123) 用 `<<<>>>` 语法启动 8 个 block：

```cpp
add_custom<<<numBlocks, nullptr, stream>>>(xDevice, yDevice, zDevice);
```

而 [add.asc:L166-L181](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L166-L181) 是普通的主机侧 `main`：构造 `x`、`y` 输入，调用 `kernel_add`，再用 `VerifyResult` 把算子输出和「黄金值（golden）」逐元素比对。注意 [add.asc:L20-L22](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L20-L22)：

```cpp
#ifdef ASCENDC_CPU_DEBUG
#include "cpu_debug_launch.h"
#endif
```

只有在 CPU 域（定义了 `ASCENDC_CPU_DEBUG`）才会引入 `cpu_debug_launch.h`，这个头文件把 `<<<>>>` 启动语法「翻译」成 CPU 上的仿真执行（详见后续 u2-l1）。这是同一份源码既能 CPU 调试、又能 NPU 运行的关键。

**(4) 预期输出**

[examples/02_cpudebug/README.md:L80-L86](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/README.md#L80-L86) 写明成功标志：

```bash
[Success] Case accuracy is verification passed.
```

这行字符串来自 [add.asc:L157](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L157) 的 `VerifyResult`——当输出与 golden 完全相等时打印。看到它就说明 CPU 域仿真的算子计算结果正确。

#### 4.3.4 代码实践

**实践目标**：在本地编译运行 add 样例，亲手看到 CPU 域仿真输出。

**前置条件**：已完成 4.2 的 run 包安装，并执行过 `source ${install_path}/cann/set_env.sh`（让 `find_package(ASC)` 能找到工具链）。

**操作步骤**：

1. 进入样例目录：
   ```bash
   cd examples/02_cpudebug
   ```
2. 配置并编译（默认 dav-2201 架构）：
   ```bash
   mkdir -p build && cd build
   cmake -DCMAKE_ASC_RUN_MODE=cpu -DCMAKE_ASC_ARCHITECTURES=dav-2201 ..
   make -j
   ```
3. 运行：
   ```bash
   ./add
   ```

**需要观察的现象**：

- `cmake` 阶段会输出找到 ASC 工具链、设置 `--npu-arch=dav-2201` 等日志。
- `make` 阶段会编译 `add.asc`，生成可执行文件 `./add`。
- 运行时会先打印 `Output:` 和 `Golden:` 两行（各最多 20 个浮点数），最后打印结论行。

**预期结果**：最后一行为 `[Success] Case accuracy is verification passed.`，进程返回码为 0。若看到 `[Failed] Case accuracy is verification failed!` 则说明仿真结果与 golden 不符（通常是环境/版本不匹配）。**实际运行依赖正确安装的 CANN + asc-tools，待本地验证。**

#### 4.3.5 小练习与答案

**练习 1**：把 `CMAKE_ASC_RUN_MODE` 从 `cpu` 改成别的值（或不设置）会发生什么？请结合 `add.asc` 的 `#ifdef ASCENDC_CPU_DEBUG` 推断。

**参考答案**：CPU 域模式下编译器会定义 `ASCENDC_CPU_DEBUG`，于是 `cpu_debug_launch.h` 被引入，`<<<>>>` 被翻译成 CPU 仿真。若不是 cpu 模式，该宏不定义，`<<<>>>` 会按真实 NPU 启动语义处理，生成的就不是能在本机 CPU 直接 `./add` 运行的程序了（需要 NPU 设备或仿真器）。

**练习 2**：`add.asc` 里的 `NUM_BLOCKS = 8` 决定了什么？样例输入 shape 是 `[8, 2048]`，二者有什么关系？

**参考答案**：`NUM_BLOCKS` 决定核函数启动的 block 数（[add.asc:L123](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L123) 的 `numBlocks`），每个 block 负责一段连续数据（`BLOCK_LENGTH = TOTAL_LENGTH / NUM_BLOCKS = 2048`）。shape `[8, 2048]` 共 16384 个元素，正好被 8 个 block 各分到 2048 个，二者是「数据总量 / block 数 = 每 block 工作量」的关系。

**练习 3**：为什么 `find_package(ASC REQUIRED)` 找不到时样例无法编译？这和本讲 4.2 模块有什么联系？

**参考答案**：ASC 工具链来自 CANN/asc-tools 的安装产物，必须先 `source set_env.sh` 让 CMake 能定位到它。这说明样例**依赖 asc-tools 已被正确编译安装**——也就是 4.2 的 run 包安装是 4.3 样例运行的前提，二者顺序不能颠倒。

---

## 5. 综合实践

把三个模块串起来，完成一次端到端的「编译 → 安装 → 跑样例」全流程。这也是本讲规格里指定的核心实践任务。

**任务**：在已配好 CANN 环境（`source set_env.sh` 生效）的机器上，完成下面三步并记录每一步的产物。

**步骤**：

1. **编译 run 包**（仓库根目录）：
   ```bash
   bash build.sh --pkg
   ```
   记录：`build_out/` 下生成的 `.run` 文件全名（应含 `9.1.0` 与架构）。

2. **安装 run 包**：
   ```bash
   cd build_out
   ./cann-asc-tools_<cann_version>_linux-<arch>.run --full --pylocal
   ```
   记录：安装目标路径，以及 `tools/cpudebug/lib64/` 下出现的软链（`libtikcpp_debug.so`、`libtikicpulib_*.so`）。

3. **编译运行 add 样例**：
   ```bash
   cd examples/02_cpudebug
   mkdir -p build && cd build
   cmake -DCMAKE_ASC_RUN_MODE=cpu -DCMAKE_ASC_ARCHITECTURES=dav-2201 ..
   make -j
   ./add
   ```
   记录：最后一行是否为 `[Success] Case accuracy is verification passed.`。

**验收标准**：

- 能解释为什么第 1 步必须用 `--pkg`（而不是直接 `cmake`）——因为要走 `package` 目标触发 CPack 打包。
- 能指出第 2 步建立的软链是为了兼容旧库名 `tikcpp`/`tikicpulib`。
- 能说出第 3 步里 `CMAKE_ASC_RUN_MODE=cpu` 让 `ASCENDC_CPU_DEBUG` 生效，从而引入 `cpu_debug_launch.h`，使 `<<<>>>` 变成 CPU 仿真启动。
- 若任何一步因环境缺失失败，能根据报错定位到本讲对应的源码位置（例如 `find_package(ASC REQUIRED)` 找不到 → 回到 4.2 检查安装；`--pkg-type` 报错 → 回到 4.1 的 `check_pkg_type`）。

> 提示：整个流程对 CANN 版本有强约束（前置 u1-l3 提到「asc-tools 不能独立升级」）。若编译报版本不匹配，先核对 CANN 包版本是否与 asc-tools master/tag 对应。

## 6. 本讲小结

- `build.sh` 是 asc-tools 唯一的编译入口，`--pkg` 走打包流程、`--test` 走测试流程，二者互斥；产物落在 `build_out/`，中间文件在 `build/`。
- run 包是 CANN 标准交付物，文件名由 [version.cmake](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/version.cmake#L11) 的版本号与 [package.cmake](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cmake/package.cmake#L14-L22) 检测的架构共同决定。
- 安装 run 包时，[cpudebug/CMakeLists.txt](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L129-L137) 会建立 `libtikcpp_debug.so → libcpudebug.so` 等软链，作为新旧库名的兼容层。
- add 样例用 `cmake -DCMAKE_ASC_RUN_MODE=cpu` 编译，`.asc` 源码经 ASC 工具链转译后链接到 cpudebug 仿真库，运行成功标志是 `[Success] Case accuracy is verification passed.`。
- 「装工具（run 包）」与「跑样例（add）」是依赖关系：样例的 `find_package(ASC REQUIRED)` 要求 asc-tools 已安装，所以两步顺序固定。
- 同一份 `add.asc` 既能 CPU 调试又能 NPU 运行，靠的是 `#ifdef ASCENDC_CPU_DEBUG` 条件引入 `cpu_debug_launch.h`——这是下一单元的切入点。

## 7. 下一步学习建议

本讲你跑通了第一个 CPU 域样例，但还停留在「照着命令敲」的层面。接下来建议进入 **u2 单元「CPU Debug 使用入门」**：

- **u2-l1（CPU Debug 工作原理与使用流程）**：深入 `cpu_debug_launch.h`，看懂 `<<<>>>` 启动语法到底是怎么被翻译成 CPU 仿真执行的，理解本讲反复提到的 `ASCENDC_CPU_DEBUG` 背后的机制。
- **u2-l2（Ascend C 算子源码与 .asc 核函数结构）**：精读 `add.asc`，系统理解 `KernelAdd` 类的三段式（CopyIn/Compute/CopyOut）、`TQue`/`LocalTensor`/`GlobalTensor` 等核心数据结构。
- **u2-l3（使用 GDB 调试 CPU 域算子）**：在本讲的 `./add` 基础上，用 `gdb --args ./add` 进入单步调试，理解「为什么 CPU Debug 要为每个核 fork 一个子进程」。

如果对构建系统本身更感兴趣，可以先跳到 **u9-l1（CMake 构建系统与多架构产物）** 和 **u9-l2（打包安装与 run 包生成）**，那里会展开本讲浅尝辄止的 `package.cmake`、`PRODUCT_TYPE_LIST`、软链矩阵等细节。
