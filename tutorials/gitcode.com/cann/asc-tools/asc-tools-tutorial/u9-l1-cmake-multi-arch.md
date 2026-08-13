# CMake 构建系统与多架构产物

## 1. 本讲目标

本讲是「构建、打包与测试体系」单元的第一讲。读完本讲，你应当能够：

- 说清一条命令 `bash build.sh --pkg` 背后，CMake 是如何把 asc-tools 的源码树组织起来的；
- 解释 **「同一份开源源码，为每一种 NPU 架构各编出一个 `libcpudebug.so`」** 这一核心设计是怎么用 `PRODUCT_TYPE_LIST` + `foreach` 实现的；
- 读懂每种产品对应的 `__CCE_AICORE__` / `__DAV_` / `__NPU_ARCH__` 宏差异，并理解这些宏如何驱动源码里的条件编译；
- 说明开源的 `api_check` / `regfwk` 源码与闭源的 `libcpudebug_model.a` 是如何被合并进同一个 `.so` 的。

本讲是 u1-l2（目录结构）与 u1-l4（build.sh 编译闭环）的向下延伸：u1-l4 告诉你「怎么编」，本讲告诉你「CMake 究竟编了什么、为什么编出这么多份」。

## 2. 前置知识

本讲默认你已经具备以下基础（不会的术语下面会顺带解释）：

- **CMake 基础语法**：`add_library`（定义构建目标）、`target_compile_definitions`（给目标加宏）、`target_link_libraries`（链接依赖）、`add_subdirectory`（把子目录挂进构建树）、`install`（声明安装规则）。
- **静态库（`.a`）/ 共享库（`.so`）/ 目标文件（`.o`）的关系**：`.o` 是编译产物，多个 `.o` 打包成 `.a`（静态归档），`.so` 是运行期可加载的共享库。asc-tools 的关键技巧是「把闭源 `.a` 拆回 `.o`，再和开源 `.o` 重新链接成 `.so`」。
- **C 预处理器宏**：`#ifdef` / `#define`。本讲会看到大量 `__CCE_AICORE__=220` 这样的宏，它们在编译期决定源码的哪一段被编译进去。
- **CMake 生成器表达式**：形如 `$<STREQUAL:a,b>:X>` 的写法，意思是「当 a 等于 b 时，展开为 X」。
- **IMPORTED 目标与 OBJECT 库**：`IMPORTED` 表示这个目标不在本工程编译、而是「从外部引进来的现成产物」；`OBJECT` 库只产出 `.o` 文件、不直接产出 `.so`，常用来「打包一批对象文件给别的目标链接」。

如果你对 CMake 完全陌生，建议先花半小时读一遍 `add_library` / `target_*` / `foreach` 的官方教程，再回来读本讲。

## 3. 本讲源码地图

本讲涉及的文件都在「构建装配」这条线上：

| 文件 | 作用 |
| --- | --- |
| [build.sh](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh) | 唯一编译入口，封装「探测 CANN → cmake 配置 → 编译/打包」 |
| [CMakeLists.txt](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/CMakeLists.txt)（根） | 顶层装配：加载 cann-cmake 工具链、按开关挂入各子目录 |
| [cpudebug/CMakeLists.txt](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt) | **本讲主角**：`PRODUCT_TYPE_LIST` + `foreach` 的多架构构建逻辑全在这里 |
| [cpudebug/cmake/fun.cmake](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/cmake/fun.cmake) | `product_dir()` 等辅助函数：把产品名映射成安装目录名 |
| [cmake/third_party/cpudebug.cmake](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cmake/third_party/cpudebug.cmake) | 下载并解压闭源 cpudebug 依赖包（含 `libcpudebug_model.a`） |
| [cmake/dependencies.cmake](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cmake/dependencies.cmake) | 声明对 CANN 包内 `unified_dlog`/`securec`/`mmpa`/`pvmodel` 等的依赖 |
| [cpudebug/cmake/tikicpulib-config.cmake.in](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/cmake/tikicpulib-config.cmake.in) | 样例 `find_package(ASC)` 解析的配置模板，含 SoC→产品系列的归并映射 |
| [version.cmake](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/version.cmake) | 定义版本号 `9.1.0` 与上下游依赖约束 |

## 4. 核心概念与源码讲解

### 4.1 构建全景：从 build.sh 到模块装配

#### 4.1.1 概念说明

asc-tools 的构建存在 **两种模式**，理解这点是看懂根 `CMakeLists.txt` 的前提：

- **`BUILD_OPEN_PROJECT=ON`（开源独立构建）**：由 `build.sh` 触发，把 asc-tools 当成一个独立仓库编译，编译产物是可交付的 run 包。本讲主要分析这种模式。
- **`BUILD_OPEN_PROJECT=OFF`（CANN 源内构建）**：当 asc-tools 作为 CANN 大仓的一部分被编译时，依赖路径与生成逻辑不同（例如 `cpudebug/src/regfwk/CMakeLists.txt` 会用脚本现场生成 stub 表）。

根 `CMakeLists.txt` 里几乎所有条件分支都围绕 `BUILD_OPEN_PROJECT` 展开：它在 `ON` 时才拉取闭源依赖、才配置打包。

#### 4.1.2 核心流程

`build.sh` 的主线非常简洁，三步：

1. **`set_env`**：按优先级（`-p` 参数 > `ASCEND_HOME_PATH` > `ASCEND_OPP_PATH` > 默认安装目录）定位 CANN 包，并 `source setenv.bash`。
2. **`cmake_config`**：执行 `cmake ..`，传入 `-DBUILD_OPEN_PROJECT=ON -DASCEND_CANN_PACKAGE_PATH=... -DCANN_3RD_LIB_PATH=...` 等选项。
3. **`build package`**：执行 `cmake --build . --target package`，触发 CPack 打包。

随后控制权交给根 `CMakeLists.txt`：它先加载 cann-cmake 工具链（提供 `find_cann_package`、`npu_op_package` 等函数），再按 `BUILD_OPEN_PROJECT` 决定是否引入依赖与打包脚本，最后用一连串 `add_subdirectory` 把各模块挂进构建树。

#### 4.1.3 源码精读

`build.sh` 在 `CUSTOM_OPTION` 里写死了 `BUILD_OPEN_PROJECT=ON`，这是区分两种构建模式的总开关：

[build.sh:25](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L25) —— 固定携带 `BUILD_OPEN_PROJECT=ON`，以及安装前缀。

打包分支就是「配置 + 构建 package 目标」两句：

[build.sh:254-257](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L254-L257) —— `build_package` 调 `cmake_config` 再 `build package`。

根 `CMakeLists.txt` 开头先抓取 cann-cmake 工具链（一个独立仓库，提供所有 `*_cann_*` 辅助函数），再初始化工程：

[CMakeLists.txt:11-17](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/CMakeLists.txt#L11-L17) —— `include(cmake/fetch_cann_cmake.cmake)` 拉取 cann-cmake（tag `master-034`），随后 `init_cann_project` 注册工具链；`add_compile_options(-Werror)` 全局把警告当错误。

`fetch_cann_cmake.cmake` 的拉取策略是「本地优先、否则联网」：

[cmake/fetch_cann_cmake.cmake:17-31](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cmake/fetch_cann_cmake.cmake#L17-L31) —— 优先用本地 `cmake-master-034.tar.gz`，否则从 gitcode 拉 cann/cmake 仓库。

`BUILD_OPEN_PROJECT` 为真时才引入依赖与打包脚本（这是开源独立构建的「装配套件」）：

[CMakeLists.txt:39-49](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/CMakeLists.txt#L39-L49) —— 引入 `dependencies.cmake`（CANN 包内依赖）、`intf_pub_linux.cmake`（编译/链接基线选项）、`package.cmake`（CPack 打包）。

随后用一串 `add_subdirectory` 装配模块，注意 `third_party` 与 `tests` 是有条件挂入的：

[CMakeLists.txt:59-70](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/CMakeLists.txt#L59-L70) —— `cpudebug`、`npuchk`、四个 Python 工具目录依次挂入；`third_party`（msopgen 等）仅在开源构建且非测试时挂入；`tests` 仅在 `ENABLE_TEST` 时挂入。

#### 4.1.4 代码实践

**实践目标**：看清 `build.sh` 到根 `CMakeLists.txt` 的交接面。

**操作步骤**：

1. 打开 `build.sh`，定位 `CUSTOM_OPTION` 的拼装处（约第 876 行附近），确认它向 cmake 传递了哪些 `-D` 变量。
2. 打开根 `CMakeLists.txt`，对照第 39–70 行，回答：哪些子目录是无条件挂入的？哪些是有条件挂入的？条件分别是什么？

**需要观察的现象**：你会看到 `BUILD_OPEN_PROJECT` 与 `ENABLE_TEST` 两个开关如何「点亮/熄灭」不同的子目录。

**预期结果**：`cpudebug`、`npuchk`、`utils/msobjdump`、`utils/optype_collector`、`utils/templates`、`utils/show_kernel_debug_data` 始终挂入；`third_party` 需要 `BUILD_OPEN_PROJECT AND NOT ENABLE_TEST`；`tests` 需要 `ENABLE_TEST`。

#### 4.1.5 小练习与答案

**练习 1**：为什么根 `CMakeLists.txt` 第 64 行要求 `third_party` 在 `NOT ENABLE_TEST` 时才挂入？
**答案**：`third_party` 子目录负责拉取/构建 msopgen 等交付工具（见 `third_party/CMakeLists.txt`），属于「打包产物」而非「被测代码」。测试模式下只编译被测库与用例，拉这些交付件既慢又无意义，故用 `NOT ENABLE_TEST` 关掉。

**练习 2**：`add_compile_options(-Werror)`（根 CMakeLists 第 17 行）对整个工程意味着什么？
**答案**：它把所有编译警告升级为错误，任何一个警告都会让编译失败。这是 asc-tools 保证代码质量的硬约束，也意味着贡献代码时必须消除全部警告。

---

### 4.2 多架构 PRODUCT_TYPE_LIST：一源多库（最小模块 1）

#### 4.2.1 概念说明

这是本讲最核心的设计。NPU 有很多代际型号（Ascend 910、910B、310P、310B、950 等），每一代的指令集、存储层级、向量宽度都不同。asc-tools 的 `cpudebug` 要在 CPU 上「孪生仿真」这些 NPU，就必须为 **每一种架构各提供一份** 行为正确的仿真库。

但开源源码只有一份。于是 cpudebug 用了一个朴素而强大的办法：定义一个产品清单 `PRODUCT_TYPE_LIST`，用 `foreach` 遍历它，**每遍历一次就生成一个独立的 `.so` 目标**，通过给每个目标打不同的架构宏，让同一份源码「长出」不同形态的二进制。

设产品数为 \( P \)、共享源文件数为 \( S \)，则该设计要编译的对象文件数约为 \( O(P \times S) \)：同一批源码被编译 \( P \) 次。这是「一源多库」的必然代价，换来的是每个 `.so` 都与一种真实硬件严格对应、互不干扰。

#### 4.2.2 核心流程

`cpudebug/CMakeLists.txt` 的多架构构建可以抽象成下面的伪代码：

```
PRODUCT_TYPE_LIST = [ascend910, ascend310p, ascend910B1, ascend310B1, ascend950pr_9599]
若 x86_64: 追加 [kirinx90, kirin9030]

ASCENDC_CHECK_SRC = glob(src/api_check/*.cpp)      # 开源：API 校验器（约 30 个 .cpp）
ASCENDC_REGFWK_SRC = [stub_base, stub_reg, stub_backtrace, kernel_print_lock]  # 开源：注册框架

foreach product in PRODUCT_TYPE_LIST:
    1. 用 CMAKE_AR -x 解包 libraries/lib/<product>/libcpudebug_model.a  → 得到一批闭源 .o
    2. 把这些 .o 包成一个 IMPORTED OBJECT 目标 cpudebug_obj_<product>
    3. add_library(cpudebug_<product> SHARED,
           ASCENDC_CHECK_SRC,          # 开源
           ASCENDC_REGFWK_SRC,         # 开源
           闭源 .o 对象)               # 闭源
    4. 给 cpudebug_<product> 打上该产品的架构宏（__CCE_AICORE__ 等）
    5. 设置输出名为 cpudebug，输出目录分产品存放
    6. install 到 tools/cpudebug/lib64/<Product_cap>/，并建 libtikcpp_debug.so 软链
```

关键点：循环体里 `add_library` 的目标名带产品后缀（`cpudebug_ascend910B1`），但最终输出名统一是 `libcpudebug.so`（靠 `OUTPUT_NAME`），只是落在不同子目录，从而避免文件名冲突。

#### 4.2.3 源码精读

产品清单的定义，x86_64 下额外追加两款 kirinx90/kirin9030：

[cpudebug/CMakeLists.txt:33-36](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L33-L36) —— 基础 5 款产品；x86_64 平台再 `list(APPEND ...)` 两款。所以在 x86_64 上 `foreach` 会跑 7 次、产出 7 个 `.so`，aarch64 上是 5 个。

开源源码用 `file(GLOB ...)` 收集，注意 `api_check` 是整目录通配，`regfwk` 是显式列四个文件：

[cpudebug/CMakeLists.txt:44-52](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L44-L52) —— `ASCENDC_CHECK_SRC` 通配 `src/api_check/*.cpp`；`ASCENDC_REGFWK_SRC` 精确指定 4 个注册框架源文件。

整个多架构循环的骨架（解包闭源 `.a` → 拼 IMPORTED OBJECT → 合库）：

[cpudebug/CMakeLists.txt:54-72](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L54-L72) —— 每个产品：`execute_process(${CMAKE_AR} -x libcpudebug_model.a)` 把闭源静态库拆成 `.o`；`glob` 收集这些 `.o`；声明 `IMPORTED` 的 `OBJECT` 目标；最后 `add_library(... SHARED ...)` 把开源源码与闭源对象合并成一个共享库。

输出名统一、但输出目录按产品分开（这正是「同名不同库」的实现）：

[cpudebug/CMakeLists.txt:118-121](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L118-L121) —— `OUTPUT_NAME cpudebug` 让所有产品都叫 `libcpudebug.so`；`LIBRARY_OUTPUT_DIRECTORY` 带 `${product_type}` 区分，避免互相覆盖。

#### 4.2.4 代码实践

**实践目标**：亲手数出一次开源构建会产出多少个 `libcpudebug.so`。

**操作步骤**：

1. 在 `cpudebug/CMakeLists.txt` 第 33 行读出基础产品列表，数一下有几个。
2. 查看你当前机器架构：`uname -m`。
3. 判断：若你是 x86_64，`PRODUCT_TYPE_LIST` 最终有几个元素？aarch64 呢？

**需要观察的现象**：产品数 = `foreach` 循环次数 = 最终 `libcpudebug.so` 的份数。

**预期结果**：x86_64 为 7 份（5 基础 + kirinx90 + kirin9030），aarch64 为 5 份。每份都位于独立的 `tools/cpudebug/lib64/<Product_cap>/` 子目录下。

#### 4.2.5 小练习与答案

**练习 1**：为什么 kirinx90 / kirin9030 只在 x86_64 下加入清单（第 34–36 行）？
**答案**：这两款是面向特定（x86 平台相关的）型号的仿真库，其闭源 `libcpudebug_model.a` 只在 x86_64 的依赖包里提供。aarch64 依赖包不含它们，若加入清单会在解包 `.a` 时找不到文件而失败，故用架构判断门控。

**练习 2**：如果未来新增一款 NPU（比如 ascend910C），要让 cpudebug 支持它，至少要改 `cpudebug/CMakeLists.txt` 的哪几处？
**答案**：至少三处——①把新产品加入 `PRODUCT_TYPE_LIST`；②在 `target_compile_definitions` 的生成器表达式里补一行它的架构宏（`__CCE_AICORE__`/`__DAV_`/`__NPU_ARCH__`）；③在 `fun.cmake` 的 `product_dir()` 里补它的安装目录映射。当然，前提是闭源依赖包里已提供对应的 `libcpudebug_model.a`。

---

### 4.3 架构宏定义：让同一份源码长出不同形态（最小模块 2）

#### 4.3.1 概念说明

上一模块解决了「编出多份」，本模块解决「凭什么同一份源码能编出不同的行为」。答案就是 **架构宏**。

cpudebug 的源码里散布着大量 `#ifdef __DAV_C220__`、`#if __NPU_ARCH__ == 3510` 这样的条件编译。同一份 `kernel_base_check.cpp`，编给 ascend910 时 `__CCE_AICORE__=100` 生效、走 910 的校验分支；编给 ascend910B1 时 `__CCE_AICORE__=220` 生效、走 910B 的分支。CMake 的职责，就是在 `foreach` 的每一轮里，给当前产品注入正确的宏组合。

每个产品拿到 **三组一脉相承的宏**：

- `__CCE_AICORE__`：内核架构代际编号（100/200/220/300/310…），是源码里最常用的分支判据。
- `__DAV_<系列>__`：DAV（Davinci，达芬奇架构）系列标记，前缀字母区分核的定位：`C` = Cube（大核，训练为主）、`M` = Mini（小核，推理为主）、`L` = Lite（轻量，kirin 系列）。
- `__NPU_ARCH__`：一个数值化的架构 ID，供底层仿真（如 SIMT 路径，见 u3-l2）精确判别。

此外还有一个 `NO_COSIM` 宏，控制是否启用「协同仿真」（与 CANN 的 pvmodel 仿真器联动）。

#### 4.3.2 核心流程

架构宏的注入完全靠 CMake 的 **生成器表达式** `$<$<STREQUAL:${product_type},ascend910B1>:...>`，读法是：「当当前遍历到的产品等于 ascend910B1 时，展开为冒号后的内容」。它被放在 `target_compile_definitions(... PRIVATE ...)` 里，因此每个目标只拿到属于自己的那一行。

所有产品的宏映射汇总成下表（请对照源码记忆）：

| product_type | `__CCE_AICORE__` | `__DAV_` | `__NPU_ARCH__` | `NO_COSIM` |
| --- | --- | --- | --- | --- |
| ascend910 | 100 | `__DAV_C100__` | 1001 | 否（保留协同仿真） |
| ascend310p | 200 | `__DAV_M200__` | 2002 | 否 |
| ascend910B1 | 220 | `__DAV_C220__` | 2201 | 是 |
| ascend310B1 | 300 | `__DAV_M300__` | 3002 | 是 |
| ascend950pr_9599 | 310 | `__DAV_C310__` | 3510 | 是 |
| kirinx90 | 300 | `__DAV_L300__` | 3003 | 是 |
| kirin9030 | 311 | `__DAV_L311__` | 3113 | 是 |

规律：`__CCE_AICORE__` 数值随代际递增；`__DAV_` 前缀的 C/M/L 三类对应核的规模；只有最早两代（910、310P）保留协同仿真能力，新代际一律 `NO_COSIM`。

此外，所有产品都共享三个基础宏：`__CCE_KT_TEST__=1`、`ASCENDC_CPU_DEBUG=1`、`ASCENDC_DEBUG=1`——它们把整份代码切到「CPU 调试」模式（这正是 u2-l1 讲过的 `ASCENDC_CPU_DEBUG` 的来源）。

#### 4.3.3 源码精读

宏注入的核心代码——基础三宏在前，逐产品的架构宏用生成器表达式按需展开：

[cpudebug/CMakeLists.txt:73-84](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L73-L84) —— 第 74–76 行是所有产品共有的 `__CCE_KT_TEST__`/`ASCENDC_CPU_DEBUG`/`ASCENDC_DEBUG`；第 77–83 行每一行对应一个产品，只有当前 `${product_type}` 匹配时该行的宏才会被注入。这就是「一次 foreach、按产品激活不同宏」的实现。

`NO_COSIM` 的注入逻辑——除 ascend910 与 ascend310p 外，其余产品都加上 `NO_COSIM`：

[cpudebug/CMakeLists.txt:98-104](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L98-L104) —— 注意这里用的是「正向列举要加 NO_COSIM 的产品」，等价于「除 910/310P 外全部」。

`__NPU_ARCH__` 在仿真层的实际用处（承接 u3-l2）：SIMT 向量化仿真只在 `__NPU_ARCH__` 为 3510（950pr_9599）时启用。这正说明这些宏不是装饰，而是真正驱动执行路径的开关——可对照 `kernel_simt_cpu.h` 里的 `#if __NPU_ARCH__ ...` 判据印证。

之所以需要协同仿真（pvmodel），是因为根 `CMakeLists.txt` 会探测仿真器路径，并把它作为 pvmodel 依赖的来源：

[CMakeLists.txt:43-46](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/CMakeLists.txt#L43-L46) —— `PVMODEL_PATH` 指向 CANN 包里的 `simulator`，`dependencies.cmake` 据此 `find_cann_package(pvmodel ...)`；而打了 `NO_COSIM` 的产品不走这条协同仿真链路。

[cmake/dependencies.cmake:24-29](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cmake/dependencies.cmake#L24-L29) —— 声明对 `unified_dlog`（日志）、`securec`（安全字符串库 c_sec）、`mmpa`（跨平台内存/进程抽象）、`acl_rt`、`pvmodel` 的依赖，全部来自 CANN 包。

#### 4.3.4 代码实践

**实践目标**：验证「同一份源码、不同宏 → 不同的条件编译分支」。

**操作步骤**：

1. 在仓库里搜索架构宏在源码中的使用，例如搜索 `__DAV_C220__` 或 `__CCE_AICORE__`，观察哪些 `.cpp` / `.h` 用它做分支判据。
2. 对照上表，回答：编给 ascend910B1 时，命中 `__DAV_C220__` 的分支会被编译进去，而 `__DAV_C100__` 分支会被预处理器剔除——这对最终 `.so` 的体积与行为意味着什么？

**需要观察的现象**：你会看到 api_check 的部分校验器按架构分叉（不同架构支持的指令集不同，校验规则也不同）。

**预期结果**：确认架构宏确实驱动了源码内部的 `#ifdef` 分支，从而让 7 份 `libcpudebug.so` 各自有不同的有效代码段，而非完全相同的拷贝。

> 说明：本实践为源码阅读型，无需实际编译；若要运行验证，可在本地用 `gcc -D__CCE_AICORE__=220 -E` 对某个头文件做预处理，对比不同宏下展开的差异。

#### 4.3.5 小练习与答案

**练习 1**：`__DAV_C220__` 与 `__DAV_M300__` 的前缀 C/M 分别代表什么？据此判断 ascend910B1 与 ascend310B1 哪个偏训练、哪个偏推理。
**答案**：C = Cube 大核、面向训练；M = Mini 小核、面向推理。ascend910B1（`__DAV_C220__`）偏训练，ascend310B1（`__DAV_M300__`）偏推理。

**练习 2**：为什么 ascend910 与 ascend310p 不加 `NO_COSIM`，而新代际都加？
**答案**：ascend910/310p 是较早的型号，cpudebug 仍保留与 CANN pvmodel 仿真器协同工作的能力（不加 `NO_COSIM` 即启用协同仿真）；新代际改用纯 CPU 孪生仿真、不再依赖 pvmodel，故加 `NO_COSIM` 关闭协同链路，简化依赖、提升速度。

---

### 4.4 闭源 model 库与开源代码的合并（最小模块 3）

#### 4.4.1 概念说明

cpudebug 的代码分属两个世界：

- **开源部分**（本仓库可见）：`src/api_check/*.cpp`（API 校验器）、`src/regfwk/` 下的 `stub_base.cpp`/`stub_reg.cpp`/`stub_backtrace.cpp`/`kernel_print_lock.cpp`（stub 注册驱动）、`src/acl_stub/`（ACL 桩）。这些源码你能直接读、直接改。
- **闭源部分**（不在仓库里，以二进制下发）：每种产品的 `libcpudebug_model.a`——它包含 NPU 行为的「孪生模型」核心实现（真正的仿真引擎、SIMT 调度、内存模型等），以及预生成的 `libcpudebug_stubreg.so`/`libcpudebug_cceprint.so`/`libcpudebug_npuchk.so`。

闭源 `.a` 之所以以 **静态归档** 形式下发，是为了方便「拆解后重组」：CMake 用 `ar -x` 把它拆成一个个 `.o`，再和开源源码编译出的 `.o` 一起链接成最终的 `libcpudebug.so`。这样最终交付的是一个「开源 + 闭源混合」的共享库，使用方无需感知内部边界。

这套闭源依赖由 `cmake/third_party/cpudebug.cmake` 在配置期自动下载，下载源是华为 OBS 对象存储，包名带版本号与架构。

#### 4.4.2 核心流程

闭源依赖的获取与合并流程：

```
配置期（cmake/third_party/cpudebug.cmake）：
  1. 检测架构（x86 / aarch64）
  2. 若 libraries/lib/include/stub_fun.h 不存在：
       a. 拼 包名: cann-asc-tools-cpudebug-deps-lib_<buildtype>_9.1.0_linux-<arch>.tar.gz
       b. 优先用本地 CANN_3RD_LIB_PATH 下的包，否则从 OBS 下载
       c. tar -xf 解压到 libraries/ 下
  3. 把 targets-tikicpulib*.cmake 改名为 targets-cpudebug*.cmake（新名替换旧名）
  4. 声明 IMPORTED 目标 cpudebug_stubreg → libraries/lib/libcpudebug_stubreg.so

编译期（cpudebug/CMakeLists.txt 的 foreach）：
  对每个产品：
       ar -x libraries/lib/<product>/libcpudebug_model.a   # 拆闭源 .a → .o
       glob 这些 .o → IMPORTED OBJECT 目标
       add_library SHARED: 开源 api_check + 开源 regfwk + 闭源 .o  # 合库
```

关键洞察：**开源源码被编译 P 次（每产品一次），闭源 `.o` 则是预先编好的、按产品取用**。两者在链接期合流为 `libcpudebug.so`。

#### 4.4.3 源码精读

闭源依赖包的下载与解压——注意包名里嵌着版本 `ASC_TOOLS_VERSION` 与架构：

[cmake/third_party/cpudebug.cmake:31-61](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cmake/third_party/cpudebug.cmake#L31-L61) —— 用 `FetchContent` 声明下载，`DOWNLOAD_NO_EXTRACT TRUE` 只下载不自动解压，随后自己 `tar -xf ... --strip-components 1` 解到 `libraries/`。这保证了「配置期完成依赖准备，编译期直接用」。

> 说明：本仓库 `libraries/` 目录在 git 里只保留一个 `.gitkeep`（`git ls-files libraries/` 仅显示 `libraries/.gitkeep`），真实内容由上述脚本在配置期填充。所以你 clone 仓库后看不到 `libcpudebug_model.a`，必须先跑一次构建才会出现。

`libraries/lib/` 最终含三类闭源产物，被 `cpudebug/CMakeLists.txt` 末尾直接 install（无需重新编译）：

[cpudebug/CMakeLists.txt:245-252](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L245-L252) —— `libcpudebug_cceprint.so`、`libcpudebug_npuchk.so`、`libcpudebug_stubreg.so` 三个闭源 `.so` 原样安装。

闭源 `.a` 的拆解与 IMPORTED OBJECT 目标的建立：

[cpudebug/CMakeLists.txt:55-66](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L55-L66) —— `${CMAKE_AR} -x libcpudebug_model.a` 在 `libraries/lib/${product_type}` 工作目录下解包；`IMPORTED_OBJECTS` 把这些 `.o` 注册给 CMake，使其可被其它目标链接。

合库的那一行——开源与闭源在此汇合：

[cpudebug/CMakeLists.txt:68-72](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L68-L72) —— `add_library(cpudebug_${product_type} SHARED ${ASCENDC_CHECK_SRC} ${ASCENDC_REGFWK_SRC} $<TARGET_OBJECTS:cpudebug_obj_${product_type}>)`。开源源码列表 + 闭源对象，三者一起链接成 `libcpudebug.so`。

链接期依赖的外部库（来自 CANN 包与系统）：

[cpudebug/CMakeLists.txt:105-117](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L105-L117) —— 链接 `intf_pub`（编译基线）、`asc_tools_headers`、`mmpa_headers`、`c_sec`（安全字符串）、`dl`（dlsym 动态装载，承接 u3-l3 的 stub 注册）、`mmpa`、`unified_dlog`（日志）、`pthread`（多线程/fork，承接 u3-l1）。`-Wl,--no-as-needed ... -Wl,--as-needed` 强制中间这些库被真正链接而非丢弃。

> 边界提示：`cpudebug/src/regfwk/CMakeLists.txt` 里还有另一个 `add_library(cpudebug_stubreg SHARED ...)`，它会用 `gen_intris`/`gen_stubs` 等脚本 **现场生成** stub 表（`intri_fun.cc`/`intri_fmt.cc`）再编译。那条路径属于 `BUILD_OPEN_PROJECT=OFF` 的 CANN 源内构建，用于 **生产** 闭源 `libcpudebug_stubreg.so` 本身；开源独立构建（build.sh）不走向它，而是直接消费预编译好的闭源 `.so`。

#### 4.4.4 代码实践

**实践目标**：追踪 ascend910B1 目标，说清「开源 api_check 源码 + 闭源 model.a」是如何合并成 `libcpudebug.so` 的。

**操作步骤**：

1. 在 `cpudebug/CMakeLists.txt` 第 33 行确认 `ascend910B1` 在 `PRODUCT_TYPE_LIST` 内。
2. 跟着 `foreach` 循环体，对 `product_type=ascend910B1` 逐步回答：
   - 第 55–59 行：从哪个路径解包哪个 `.a` 文件？
   - 第 68–72 行：这个 `.so` 由哪三部分源/对象组成？
   - 第 79 行：它被注入了哪些架构宏？
3. 第 122 行调用 `product_dir`，查 `fun.cmake`：`ascend910B1` 映射到哪个安装目录名？

**需要观察的现象**：你会看到开源部分（`ASCENDC_CHECK_SRC` + `ASCENDC_REGFWK_SRC`）在每一轮都被重新编译，而闭源 `.o` 只是「拿来用」。

**预期结果**：ascend910B1 的 `libcpudebug.so` ＝ 约 30 个开源 api_check `.cpp` + 4 个开源 regfwk `.cpp`（均以 `__CCE_AICORE__=220`/`__DAV_C220__`/`__NPU_ARCH__=2201`/`NO_COSIM` 编译）＋ `libraries/lib/ascend910B1/libcpudebug_model.a` 拆出的闭源 `.o`，链接后输出到 `ascend910B1/` 子目录、最终安装到 `tools/cpudebug/lib64/Ascend910B1/`。

> 说明：`ascend910B1` 在 `product_dir` 里并不命中显式的 `ascend910b` 分支（[fun.cmake:47-48](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/cmake/fun.cmake#L47-L48) 比较的是不带 `1` 的全小写 `ascend910b`），而是落入末尾的 `else` 兜底分支——取首字母大写、其余原样拼接（[fun.cmake:57-62](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/cmake/fun.cmake#L57-L62)），结果恰为 `Ascend910B1`。这提醒我们：`product_dir` 的显式分支与其说是「白名单」，不如说大多数字符串都在走兜底规则。

#### 4.4.5 小练习与答案

**练习 1**：为什么闭源部分要以 `.a`（静态归档）下发，而不是直接给 `.so`？
**答案**：因为最终交付的 `libcpudebug.so` 需要 **开源与闭源代码混合链接在同一份 `.so` 里**。`.a` 可以被 `ar -x` 拆成 `.o` 再参与链接，从而和开源 `.o` 共生成一个 `.so`；若直接给闭源 `.so`，就只能整体链接、无法与开源对象合并成单一库，符号可见性与版本管理都会变复杂。

**练习 2**：clone 仓库后为什么看不到 `libraries/lib/libcpudebug_model.a`？它在什么时候出现？
**答案**：仓库里 `libraries/` 只提交了 `.gitkeep` 占位。真实的闭源依赖包由 `cmake/third_party/cpudebug.cmake` 在 **cmake 配置期** 从 OBS 下载并 `tar -xf` 解压进来。所以必须先跑一次 `cmake` 配置（如 `bash build.sh --pkg` 的第一步）后，`libraries/lib/` 才会被填充。

---

### 4.5 交付件装配：同名产物、分目录与软链兼容

#### 4.5.1 概念说明

多架构构建产出一堆「都叫 `libcpudebug.so`」的文件，它们如何不打架地安装到 CANN 目录？答案是 **按产品分子目录** 安装。同时，asc-tools 的库名经历过一次更名：旧名是 `tikcpp` / `tikicpulib`（tik + cpu lib），新名统一为 `cpudebug`。为了不破坏已存在的样例与文档（它们可能还在 `find_package` 旧名、链接旧 `.so` 名），安装阶段建立了一组 **符号链接（symlink）** 作为兼容层。

#### 4.5.2 核心流程

```
对每个产品：
  install cpudebug_<product> → tools/cpudebug/lib64/<Product_cap>/libcpudebug.so
  建软链: libtikcpp_debug.so → libcpudebug.so          （旧内核库名兼容）

公共部分（与产品无关）：
  install libcpudebug_cceprint.so / libcpudebug_npuchk.so / libcpudebug_stubreg.so
  建软链: libtikicpulib_cceprint.so → libcpudebug_cceprint.so
  建软链: libtikicpulib_npuchk.so  → libcpudebug_npuchk.so
  建软链: libtikicpulib_stubreg.so → libcpudebug_stubreg.so

cmake 配置层兼容：
  生成 cpudebug-config.cmake，并建软链 tikicpulib-config.cmake → cpudebug-config.cmake
  （样例里的 find_package(ASC) 经 tikicpulib-config.cmake 命中本工具）
```

#### 4.5.3 源码精读

按产品安装并建立第一个软链（`libtikcpp_debug.so` → `libcpudebug.so`）：

[cpudebug/CMakeLists.txt:123-137](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L123-L137) —— `install(TARGETS ... DESTINATION tools/cpudebug/lib64/${Product_cap})`；`install(CODE ... create_symlink libcpudebug.so libtikcpp_debug.so ...)`。`${Product_cap}` 由 `product_dir()` 得到。

`product_dir()` 函数把小写产品名翻译成带大小写的安装目录名（如 `ascend910` → `Ascend910A`、`ascend950pr_9599` → `Ascend950PR_9599`）：

[cpudebug/cmake/fun.cmake:38-63](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/cmake/fun.cmake#L38-L63) —— 维护一张产品→目录名的映射表；末尾 `else` 分支用「首字母大写」兜底未列出的产品。

三个公共闭源 `.so` 的安装与对应的 `tikicpulib_*` 旧名软链：

[cpudebug/CMakeLists.txt:245-282](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L245-L282) —— 逐个 `create_symlink libcpudebug_xxx.so libtikicpulib_xxx.so`。

cmake 配置文件的兼容软链（让旧名 `tikicpulib-config.cmake` 指向新名）：

[cpudebug/CMakeLists.txt:201-229](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L201-L229) —— 三条 `create_symlink`，覆盖 config 文件与 targets 文件（release 变体）。

最终，样例 `find_package(ASC REQUIRED)` 命中的就是这个配置文件，它内部把众多 SoC 别名归并到 7 个产品系列：

[cpudebug/cmake/tikicpulib-config.cmake.in:2-5](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/cmake/tikicpulib-config.cmake.in#L2-L5) —— 例如 `PRODUCT_TYPE_LIST_V220_` 把 `Ascend910B1/B2/B3/B4`、`Ascend910_9391/9381/...` 等十余个 SoC 别名全归并到 `ascend910B1` 这一个产品系列。

[cpudebug/cmake/tikicpulib-config.cmake.in:19-35](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/cmake/tikicpulib-config.cmake.in#L19-L35) —— `product_map()` 函数：任何一个 SoC 别名，先查表归并到 7 个产品之一，再链接到对应的 `tikicpulib_<产品系列>` 目标。

#### 4.5.4 代码实践

**实践目标**：理解为何要建立这么多软链，以及它们如何让旧代码继续工作。

**操作步骤**：

1. 在 `cpudebug/CMakeLists.txt` 中统计 `create_symlink` 出现的次数，把每一对「源名 → 目标名」列成表。
2. 回顾 u1-l4：样例用 `find_package(ASC REQUIRED)` 定位工具链、链接 `tikcpp_debug` 等旧名库。结合本讲的软链表，解释为什么样例代码无需改动就能用上新版 `libcpudebug.so`。

**需要观察的现象**：几乎所有「新名 → 旧名」的软链都成对存在（库本身 + cmake 配置）。

**预期结果**：旧名 `tikcpp_debug`/`tikicpulib_*` 全部以软链形式保留，指向新名 `cpudebug`/`cpudebug_*`；样例代码与旧文档因此无需改动即可兼容。

#### 4.5.5 小练习与答案

**练习 1**：所有产品的 `.so` 都叫 `libcpudebug.so`，安装时为什么不会互相覆盖？
**答案**：因为安装目标目录带了 `${Product_cap}`（如 `Ascend910B1`、`Ascend950PR_9599`），每个产品装进各自子目录，文件名虽同、路径不同，故不冲突。样例经 `tikicpulib-config.cmake` 的 `product_map` 选中正确子目录。

**练习 2**：假如某天彻底放弃旧名 `tikcpp`，可以删掉本节提到的哪些安装步骤？
**答案**：可以删除所有 `create_symlink libcpudebug*.so libtikicpulib*.so / libtikcpp_debug.so` 的 `install(CODE ...)` 段，以及 config 文件的 `tikicpulib-config.cmake`/`targets-tikicpulib*.cmake` 软链。但前提是确认所有下游样例、文档、配套仓都已改用新名 `cpudebug`，否则会破坏向后兼容。

---

## 5. 综合实践

**任务**：以 `ascend910B1` 为例，画出从「一条 build.sh 命令」到「`tools/cpudebug/lib64/Ascend910B1/libcpudebug.so`」的完整装配链路图，并在图上标出每一步对应的源码行号。

**建议步骤**：

1. 从 [build.sh:25](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/build.sh#L25) 出发，标出 `BUILD_OPEN_PROJECT=ON` 的传递。
2. 进入根 [CMakeLists.txt:54](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/CMakeLists.txt#L54)，标出 `cmake/third_party/cpudebug.cmake` 下载闭源包（[cpudebug.cmake:31-61](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cmake/third_party/cpudebug.cmake#L31-L61)）。
3. 进入 [cpudebug/CMakeLists.txt:54](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L54) 的 foreach，对 `ascend910B1` 标出：解包 `libcpudebug_model.a`（L55-59）→ 建 IMPORTED OBJECT（L63-66）→ 合库 `add_library`（L68-72）→ 注入 `__CCE_AICORE__=220` 等宏（L79）→ 注入 `NO_COSIM`（L99）→ 链接外部库（L105-117）。
4. 标出安装阶段：`product_dir` 把 `ascend910B1` 经 `else` 兜底分支映射到 `Ascend910B1`（[fun.cmake:57-62](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/cmake/fun.cmake#L57-L62)）→ install 到 `tools/cpudebug/lib64/Ascend910B1/`（L124-128）→ 建软链 `libtikcpp_debug.so`（L129-137）。

**交付物**：一张包含「源码行号 + 动作说明」的有向流程图（手绘或工具画均可）。完成后你应当能用一句话讲清：**开源 api_check/regfwk 源码与闭源 `libcpudebug_model.a` 解出的对象，在 `ascend910B1` 这轮 foreach 里被链接成一份带 `__DAV_C220__` 宏的 `libcpudebug.so`，安装到 `Ascend910B1/` 子目录并建立旧名软链。**

> 若本地具备 CANN 环境且时间允许，可执行 `bash build.sh --pkg` 后，在 `build_out/` 解包出的 run 包里找到 `tools/cpudebug/lib64/Ascend910B1/libcpudebug.so`，并用 `readelf -d` 观察它的链接依赖（应能看到 `libmmpa.so`、`libunified_dlog.so`、`libc_sec.so` 等）。若不具备环境，本任务以源码阅读与画图为准，标注「待本地验证」即可。

## 6. 本讲小结

- asc-tools 有两种构建模式，由 `BUILD_OPEN_PROJECT` 开关区分；`build.sh` 走的是 `ON`（开源独立构建），根 `CMakeLists.txt` 据此挂入依赖、打包与各子目录。
- cpudebug 用 `PRODUCT_TYPE_LIST` + `foreach` 实现 **一源多库**：同一份开源源码为每种 NPU 架构各编出一个 `libcpudebug.so`，x86_64 下共 7 份、aarch64 下 5 份。
- 每种产品在编译期被注入一组架构宏（`__CCE_AICORE__` / `__DAV_` / `__NPU_ARCH__` / `NO_COSIM`），驱动源码内部的条件编译，使同一份代码长出与硬件对应的不同形态。
- 开源的 `api_check` / `regfwk` 源码与闭源的 `libcpudebug_model.a`（`ar -x` 拆成 `.o`）在链接期合并为单一的 `libcpudebug.so`；闭源依赖由 `cmake/third_party/cpudebug.cmake` 在配置期从 OBS 下载。
- 所有产品的 `.so` 同名但分目录安装，并建立 `tikcpp_debug` / `tikicpulib_*` 一整套旧名软链与 config 软链，保证旧样例与文档无需改动即可兼容新名 `cpudebug`。

## 7. 下一步学习建议

- **u9-l2 打包安装与 run 包生成**：本讲到 `install()` 为止，下一讲接着讲这些 install 规则如何被 CPack 打成 `.run` 自解压包、如何安装进 CANN 目录，是本讲的直接下游。
- **u9-l3 单元测试体系**：想知道 `ENABLE_TEST` 开关点亮了什么、`TEST_MOD` 如何控制 C++/Python 测试分发，继续读 tests 目录的构建逻辑。
- **回看 u3-l1 / u3-l3**：本讲提到的 `dl`、`pthread` 链接、`stub_reg` 注册框架，正是多核 fork 执行模型与 stub 注册机制的承载者；带着本讲的「链接视角」重读 u3，会对 `libcpudebug.so` 内部的代码组织有更立体的认识。
- **延伸阅读**：若你想深入理解闭源 `libcpudebug_stubreg.so` 是如何被「生产」出来的，可对照阅读 `cpudebug/src/regfwk/CMakeLists.txt` 与 `cpudebug/cmake/fun.cmake` 里的 `gen_intris`/`gen_stubs`/`gen_cce_stub`/`gen_npuchk_stub` 系列函数——那是 `BUILD_OPEN_PROJECT=OFF` 下现场生成 stub 表的另一条构建路径。
