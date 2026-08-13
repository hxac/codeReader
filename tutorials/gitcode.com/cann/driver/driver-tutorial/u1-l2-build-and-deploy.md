# 环境准备、源码编译与部署安装

## 1. 本讲目标

本讲承接上一讲建立的三层架构认知（DCMI / HAL / SDK-driver），把视线从「读源码」转到「让源码跑起来」。读完本讲你应该能够：

- 说出 driver 源码编译所需的全部依赖软件，并能在 openEuler / Ubuntu 上一次性装齐。
- 看懂 `build.sh` 这个统一编译入口：它是如何用 `getopts` 解析 `--soc/--pkg/--ube` 等参数，又是如何通过 `--soc` 选定目标芯片（`ascend910b / ascend910_93 / ascend950`）并映射到内部 `PRODUCT` 变量的。
- 理解 `build.sh` 如何拼装 CMake 参数、调用 `cmake` + `make`，以及根 `CMakeLists.txt` 如何把 `src/` 子目录组织起来。
- 独立生成一个 driver run 包（`.run`），并完成部署安装、卸载，且能根据 `docs/zh/FAQ.md` 自行排查常见编译安装问题。

> 本讲只涉及「编译与部署」，不修改任何源码逻辑。

## 2. 前置知识

在动手编译前，先建立三个直觉。

**第一，driver 既是用户态库也是内核模块。** 上一讲我们说过，driver 包含三棵源码树：`ascend_hal`（用户态动态库 `libascend_hal.so`）、`sdk_driver`（内核态 `.ko` 模块）、`custom`（定制化特性）。编译时既要编译 C/C++ 用户态库，又要编译 Linux 内核模块，因此编译环境**必须同时具备 C/C++ 工具链与内核头文件**。

**第二，源码编译依赖第三方库，且会自动下载。** driver 编译依赖开源第三方库和 driver 开源二进制库（device 侧库），启动编译后由 CMake 自动拉取，因此编译机器需要联网。

**第三，driver 用 CMake 构建、用 `build.sh` 封装、最后用 makeself 打成自解压的 `.run` 包。** 这三者的关系是：`build.sh`（shell 胶水）→ 调用 `cmake` / `make`（真正的构建）→ 调用 `make package` → CPack + makeself（生成 `.run`）。理解这条链路，本讲的源码就都串起来了。

需要熟悉的几个名词：

- **run 包**：makeself 生成的自解压 shell 脚本（`.run` 文件），本身既是一个脚本又内嵌了被压缩的安装内容，执行它会自解压并调用内嵌的 `install.sh` 安装。
- **PRODUCT**：`build.sh` 内部的「产品」变量，取值 `ascend910B` 或 `ascend950`，CMake 用它来选择对应的驱动配置与特性配置。
- **--ube**：UB（灵衢超节点）引擎开关，仅 `ascend950` 在灵衢计算系统超节点 ARM 架构下编译时使用。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [build.sh](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/build.sh) | 项目统一编译入口：解析参数、选芯片、准备源码、调用 cmake/make、生成 run 包。 |
| [CMakeLists.txt](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/CMakeLists.txt) | CMake 顶层入口，工程名为 `npu_driver`，include 各 cmake 配置并 `add_subdirectory(src)`。 |
| [cmake/driver_config.cmake](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/cmake/driver_config.cmake) | 检测宿主机发行版、内核路径，并按 `PRODUCT` 加载对应驱动配置 `driver_config_${PRODUCT}.cmake`，定义 `driver` 构建目标。 |
| [cmake/feature_config.cmake](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/cmake/feature_config.cmake) | 按 `PRODUCT` 读取特性配置文件，生成 `feature.h/.mk/.cmake`，把特性宏注入到 C 与 Makefile 两个编译体系。 |
| [cmake/package.cmake](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/cmake/package.cmake) | `pack_built_in()`：根据构建组件（driver / driver_compat）选择打包配置。 |
| [cmake/config/package_config/package_driver.cmake](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/cmake/config/package_config/package_driver.cmake) | 设置 CPack 变量：架构、OS 版本、按芯片选择特性列表，最终交给 makeself 生成 run 包。 |
| [cmake/makeself_built_in.cmake](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/cmake/makeself_built_in.cmake) | 真正调用 `makeself.sh` + `package.py` 生成并移动 `.run` 包，决定包名前缀（910b / A3 / 950）。 |
| [docs/zh/QUICKSTART.md](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/docs/zh/QUICKSTART.md) | 官方端到端上手指南：环境准备、编译部署、开发指南。本讲的权威依据。 |
| [docs/zh/FAQ.md](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/docs/zh/FAQ.md) | 常见编译安装问题汇总，排错第一站。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：①编译依赖与系统环境；②`build.sh` 参数解析与芯片选择；③CMake 构建体系；④run 包生成与部署安装。

### 4.1 编译依赖与系统环境准备

#### 4.1.1 概念说明

driver 既要产出用户态 `.so`，又要产出内核态 `.ko`，所以它的依赖比普通 C++ 项目更「重」：除了 gcc/cmake/make 这类常规工具链，还必须有**与当前内核版本匹配的 kernel-headers**（内核模块编译会用到 `/lib/modules/$(uname -r)/build`），以及用于生成 run 包的 **makeself**。

#### 4.1.2 核心流程

环境准备的顺序是：

1. 确认编译机为 Linux，推荐内核版本为 v5.4 / v5.10 / v6.8。
2. 安装基础依赖：gcc、cmake、bash、kernel-headers、net-tools、openssl 开发库、pkg-config、patch。
3. （可选）安装 googletest release-1.11.0（仅跑 UT 时需要）。
4. （生成本地 run 包时）安装 makeself。
5. 确认机器可联网（CMake 会自动拉取第三方库与 device 二进制库）。
6. 若编译 `ascend950 --ube`，还需满足：openEuler 24.03 LTS SP4 + aarch64 + `umdk-urma-lib / umdk-urma-devel / libummu-devel`。

#### 4.1.3 源码精读

QUICKSTART 明确列出了依赖清单：

[docs/zh/QUICKSTART.md:40-49](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/docs/zh/QUICKSTART.md#L40-L49) —— 官方依赖软件清单（gcc / cmake / bash / kernel-headers / net-tools / openssl 开发库 / pkg-config / patch / googletest 可选 / makeself）。

并给出两种发行版的安装示例：

[docs/zh/QUICKSTART.md:56-65](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/docs/zh/QUICKSTART.md#L56-L65) —— openEuler 用 `yum`、Ubuntu 用 `apt` 的安装命令。

ascend950 的 ube 特殊路径：

[docs/zh/QUICKSTART.md:101-115](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/docs/zh/QUICKSTART.md#L101-L115) —— 950 在灵衢超节点 ARM 环境下需要 openEuler 24.03 LTS SP4、`yum install -y umdk-urma-lib umdk-urma-devel libummu-devel`，编译参数加 `--ube`。

注意：`build.sh` 在启用 ube 时还会做一道前置校验，强制要求 openEuler + aarch64 + 两个 urma 包都已安装，否则直接报错退出（详见 4.2.3）。

#### 4.1.4 代码实践

**实践目标**：在编译机上把基础依赖装齐，并自检内核头文件路径存在。

**操作步骤**：

1. 查询当前内核版本：`uname -r`。
2. 按你的发行版执行 QUICKSTART 中的安装命令（openEuler 用 `yum`，Ubuntu 用 `apt`）。
3. 自检内核构建目录是否存在：`ls /lib/modules/$(uname -r)/build`。

**需要观察的现象**：步骤 3 应能列出一个目录（通常是指向内核源码树的一个软链接）。

**预期结果**：目录存在即说明 kernel-headers 已正确安装；若提示 `No such file or directory`，正是 FAQ「问题三」描述的经典报错，解决见 4.4.5。

> 本地若无昇腾设备也可只做依赖自检；是否真正编译见 4.2.4 的综合实践。

#### 4.1.5 小练习与答案

- **练习 1**：为什么编译 driver 必须装 kernel-headers，而普通 C++ 项目不用？
  - **参考答案**：driver 的 `sdk_driver` 要编译成 Linux 内核模块 `.ko`，内核模块的构建依赖宿主机内核的构建树 `/lib/modules/$(uname -r)/build`；普通用户态 C++ 项目只链接 glibc，不需要内核头。
- **练习 2**：`googletest` 和 `makeself` 分别在什么场景下才需要？
  - **参考答案**：`googletest` 仅在执行 UT 单元测试时依赖；`makeself` 仅在本地编译并生成 `.run` 包（即带 `--pkg`）时依赖。

### 4.2 build.sh 编译脚本：参数解析与芯片选择

#### 4.2.1 概念说明

`build.sh` 是整个项目**唯一的编译入口**。它本质上是一段 shell「胶水」：把命令行参数解析成一组内部变量，准备好源码（把 `custom` 仓的产品定制代码拷进构建树），再拼装出一长串 CMake 定义（`-D...`），最后调用 `cmake` 与 `make`。理解 `build.sh`，就理解了「从一行命令到一次构建」的全部控制流。

#### 4.2.2 核心流程

`build.sh` 的执行顺序（顶层主线）：

```text
g++ -v                            # 打印编译器版本
prepare_src                       # 把 custom 产品源码合并进构建树
build_npu_driver                  # cmake .. + make driver + make install
  └─ 若 --pkg：generate_package   # make package → 生成 .run
clean_src                         # 还原被 prepare_src 改动的源码
```

关键路径变量在最开头定义：

[build.sh:14-17](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/build.sh#L14-L17) —— 定义 `BASE_PATH`、`BUILD_PATH=${BASE_PATH}/build`（cmake 构建目录）、`BUILD_OUT_PATH=${BASE_PATH}/build_out`（产物输出目录，run 包最终落在这里）。

#### 4.2.3 源码精读

**(a) 参数清单（usage）**

[build.sh:20-44](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/build.sh#L20-L44) —— 打印支持的参数。核心几个：

| 参数 | 含义 |
|------|------|
| `-j[n]` | 编译线程数（usage 说默认 8，见下方注意） |
| `-k` | 指定内核源码路径，默认 `/lib/modules/$(uname -r)/build` |
| `--soc=<版本>` | 目标芯片，取值 `ascend910b / ascend910_93 / ascend950` |
| `--pkg` | 编译并生成 run 包 |
| `--ube` | 启用 UB 引擎（仅 950） |

**(b) checkopts：用 getopts 解析参数**

[build.sh:94-173](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/build.sh#L94-L173) —— `checkopts` 是参数解析核心。它先用一组默认值初始化内部变量（注意第 98 行把 `THREAD_NUM` 默认设为 **32**，与 usage 注释里说的「默认 8」并不一致——这是源码中一处真实存在的文档/代码差异，读源码时以代码为准；并且第 309-312 行会在线程数超过 CPU 核数时自动下调到核数）。

`getopts 'uschj:k:v-:' 中的末尾 `-:` 让 getopts 能处理 `--soc=...` 这类长选项：每个 `--xxx` 会以 `-` 进入 case，再对 `$OPTARG` 二次分发。例如 `--pkg` 走到这里：

[build.sh:135-137](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/build.sh#L135-L137) —— `--pkg` 把 `ENABLE_PACKAGE` 置为 `TRUE`。

`--soc=` 是最关键的分支，它在解析后立即调用 `get_product` 把芯片字符串映射成 `PRODUCT`：

[build.sh:144-147](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/build.sh#L144-L147) —— `--soc=*)` 截取等号后的值赋给 `COMPUTE_UNIT`，并调用 `get_product`。

解析完所有参数后，`check_param` 强制要求必须传 `--soc`：

[build.sh:84-91](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/build.sh#L84-L91) —— 没有 `--soc` 就打印 usage 并退出。

**(c) get_product：芯片 → PRODUCT 映射（本讲最核心的查找表）**

[build.sh:46-66](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/build.sh#L46-L66) —— 把 `--soc` 的取值映射成内部 `PRODUCT`（注意第 48 行 `${COMPUTE_UNIT,,}` 是把输入转小写，所以用户写 `ascend910b` 或 `Ascend910B` 都能匹配）：

| `--soc` 输入 | `PRODUCT` | 附加变量 | 含义 |
|--------------|-----------|----------|------|
| `ascend910b` | `ascend910B` | — | 910B 系列 |
| `ascend910_93` | `ascend910B` | `ASCEND910_93_EX=TRUE` | 910_93（A3）形态，复用 910B 的 PRODUCT 但打额外标记 |
| `ascend950` | `ascend950` | — | 950 系列 |

> 关键点：`ascend910_93` 与 `ascend910b` 共享同一个 `PRODUCT=ascend910B`，差异只通过 `ASCEND910_93_EX` 这个布尔变量来表达——后续 CMake 打包时就是靠它区分出 `A3` 包名的（见 4.4.3）。

**(d) build_npu_driver：拼 CMAKE_ARGS 并调用 cmake/make**

[build.sh:302-380](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/build.sh#L302-L380) —— 这是最长的函数。先读取版本号（`PROJECT_VERSION`，来自 `sys_version.conf`，见 4.4.3），然后按一系列布尔变量把 `-D...` 追加到 `CMAKE_ARGS`：

[build.sh:315](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/build.sh#L315) —— 固定传入 `-DENABLE_OPEN_SRC=y -DPRODUCT_SIDE=host -DCMAKE_INSTALL_PREFIX=.`。

[build.sh:332-342](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/build.sh#L332-L342) —— 按需追加 `-DENABLE_PACKAGE=TRUE`、`-DASCEND910_93_EX=TRUE`、`-DENABLE_BUILD_PRODUCT=TRUE`。

[build.sh:344-350](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/build.sh#L344-L350) —— 仅当 `PRODUCT=ascend950` 且 `ENABLE_UBE=TRUE` 时追加 `-DENABLE_UBE=true`，并调用 `build_check_with_ube` 做前置校验。

[build.sh:273-299](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/build.sh#L273-L299) —— `build_check_with_ube`：要求发行版必须是 openEuler、架构必须是 aarch64、且已装 `umdk-urma-lib` 与 `umdk-urma-devel`，否则返回 1 终止编译。

最后真正发起构建：

[build.sh:355](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/build.sh#L355) —— `cmake ${CMAKE_ARGS} ..`（在 `build/` 目录内对上层源码树配置）。

[build.sh:362-377](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/build.sh#L362-L377) —— 根据 `BUILD_COMPONENT` 选定 make 目标（`driver` 或 `driver_compat`），执行 `make ${TARGET} -j${THREAD_NUM} && make install`。

**(e) generate_package：生成 run 包**

[build.sh:383-392](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/build.sh#L383-L392) —— `make package` 触发 CPack，产物落在 `BUILD_OUT_PATH`；随后用 `find ... ! -name "Ascend-hdk-*"` 删除该目录下非 run 包的杂物，只保留最终的 `.run`。

**(f) prepare_src / clean_src：源码的临时合并与还原**

[build.sh:183-236](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/build.sh#L183-L236) —— `prepare_src` 把 `src/custom/dev_prod` 下的产品定制代码（如 `dsmi_product_ext`、`dms/product`）拷贝/覆盖到 `ascend_hal`、`sdk_driver` 的对应位置，并按 `PRODUCT` 选择正确的 `driver.xml`、`sys_version.conf`、`specific_func.inc`。这是为什么 `custom` 仓叫「定制化特性源码库」——它的内容在编译期才被合并进主构建树。

[build.sh:238-263](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/build.sh#L238-L263) —— `clean_src` 把上述改动还原（把 `.org` 备份改回原名）。脚本还用 `trap cleanup INT` 保证 Ctrl+C 时也能还原：

[build.sh:265-271](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/build.sh#L265-L271) —— 注册 INT 信号处理，中断时调用 `clean_src`。

#### 4.2.4 代码实践

**实践目标**：亲手编译一次 910B 的 run 包，记录产物名称，并复述 `checkopts` → `get_product` 的解析链路。

**操作步骤**：

1. 进入仓库根目录，先清理上一次的编译缓存（首次编译可跳过）：
   ```bash
   bash build.sh --make_clean
   ```
2. 执行编译（生成 run 包）：
   ```bash
   bash build.sh --pkg --soc=ascend910b
   ```
3. 编译成功后查看产物：
   ```bash
   ls build_out/
   ```

**需要观察的现象**：终端会依次打印 `npu_driver build start`、cmake 配置日志、make 编译日志、`Generate package success.`，最后 `build_out/` 下出现一个 `Ascend-hdk-910b-driver-*.run` 文件。

**预期结果**：产物文件名形如 `Ascend-hdk-910b-driver-<version>_<os_version>-<arch>.run`，其中 `<version>` 来自 `scripts/package/driver/ascend910B/scripts/sys_version/sys_version.conf`（当前为 `8.5.T7.0.B053`），`<os_version>` 与 `<arch>` 取决于编译机（如 `ubuntu20.04-x86_64`）。**具体的 os/arch 段待本地验证**——以你机器上实际生成的文件名为准。

> 提示：`build.sh` 第 390 行会用 `find ... ! -name "Ascend-hdk-*"` 清理 `build_out`，且每次新编译会先清掉上一次产物（见 FAQ 问题十）。如果你想保留多份产物，编译完先备份再发起下一次编译。

#### 4.2.5 小练习与答案

- **练习 1**：用户执行 `bash build.sh --pkg --soc=ascend910_93`，请追踪 `PRODUCT` 和 `ASCEND910_93_EX` 的最终取值。
  - **参考答案**：`--soc=ascend910_93` 进入 `checkopts` 的 `soc=*` 分支，`COMPUTE_UNIT=ascend910_93`；调用 `get_product` 命中 `ascend910_93)` 分支，得到 `PRODUCT=ascend910B`、`ASCEND910_93_EX=TRUE`。两者随后以 `-DPRODUCT=ascend910B -DASCEND910_93_EX=TRUE` 传给 cmake。
- **练习 2**：如果忘了传 `--soc`，编译会发生什么？
  - **参考答案**：`checkopts` 解析完后调用 `check_param`，检测到 `COMPUTE_UNIT` 为空，打印 `Missing option: --soc`、打印 usage 并 `exit 1`，不会进入构建。

### 4.3 CMake 构建体系：CMakeLists.txt 与 cmake 配置

#### 4.3.1 概念说明

`build.sh` 只负责「指挥」，真正「搬砖」的是 CMake。根 `CMakeLists.txt` 定义了工程名 `npu_driver`、引入一组 `cmake/*.cmake` 配置模块，并通过 `add_subdirectory(src)` 把三棵源码树（`ascend_hal / sdk_driver / custom`）纳入构建。其中两个配置模块决定了「同一份源码如何适配不同芯片」：`driver_config.cmake` 负责**驱动配置**，`feature_config.cmake` 负责**特性宏**。

#### 4.3.2 核心流程

CMake 侧的控制流：

```text
CMakeLists.txt
 ├─ include cmake/driver_config.cmake   → 按 PRODUCT 加载 driver_config_${PRODUCT}.cmake，定义 driver 目标
 ├─ include cmake/feature_config.cmake  → 按 PRODUCT 读取 ${PRODUCT}.config，生成 feature.h/.mk/.cmake
 ├─ add_subdirectory(src)               → 进入三棵源码树各自的 CMakeLists
 └─ 若 ENABLE_PACKAGE：include cmake/package.cmake → pack_built_in() → makeself 打包
```

`PRODUCT` 是串联 shell 与 cmake 的关键变量：`build.sh` 用 `-DPRODUCT=...` 传进来，cmake 再用它去拼 `driver_config_${PRODUCT}.cmake` 和 `${PRODUCT}.config` 的路径。

#### 4.3.3 源码精读

**(a) 根 CMakeLists.txt**

[CMakeLists.txt:11-16](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/CMakeLists.txt#L11-L16) —— 声明工程 `npu_driver`（语言 C/CXX/ASM）；若定义了 `ENABLE_TEST` 则只进入 `test` 子目录并 `return()`（UT 单元测试的入口，见后续 UT 讲义）。

[CMakeLists.txt:19-24](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/CMakeLists.txt#L19-L24) —— include 五个 cmake 配置模块：`intf_pub_linux`、`function`、`driver_config`、`feature_config`、`create_ko_target`（用于构建 `.ko`）、`external_dependencies`（第三方依赖）。

[CMakeLists.txt:26-30](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/CMakeLists.txt#L26-L30) —— 编译 `DRIVER` 组件时，提前拉取 driver-device 二进制库供链接（这就是 QUICKSTART 说的「编译会自动下载 device 侧库」）。

[CMakeLists.txt:52-58](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/CMakeLists.txt#L52-L58) —— `add_subdirectory(src)` 进入源码树；若 `ENABLE_PACKAGE` 为真则 include `package.cmake` 调用 `pack_built_in()` 打包。

**(b) driver_config.cmake：按芯片加载驱动配置**

[cmake/driver_config.cmake:15-17](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/cmake/driver_config.cmake#L15-L17) —— 检测宿主机 Linux 发行版（`get_host_linux_distributor()`），打印一组诊断信息。

[cmake/driver_config.cmake:32-35](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/cmake/driver_config.cmake#L32-L35) —— 若未显式指定 `CUSTOM_KERNEL_PATH`，默认用 `/lib/modules/${CMAKE_HOST_SYSTEM_VERSION}/build`（即当前内核构建树）。这正是 FAQ「问题三」里 `/lib/modules/xxx/build` 报错的来源。

[cmake/driver_config.cmake:38](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/cmake/driver_config.cmake#L38) —— 关键行：`include(cmake/config/driver_config/driver_config_${PRODUCT}.cmake)`。`PRODUCT` 取 `ascend910B` 或 `ascend950`，于是不同芯片加载不同的驱动配置文件（里面定义各自要构建的 `.so`/`.ko` 目标列表 `${DRIVER_TARGETS}`）。

[cmake/driver_config.cmake:40-48](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/cmake/driver_config.cmake#L40-L48) —— 定义名为 `driver` 的顶层 `ALL` 目标，依赖 `${DRIVER_TARGETS}`（若开启 `ENABLE_BUILD_PRODUCT` 还依赖 `${DRIVER_CUSTOM_TARGETS}`）。`build.sh` 里 `make driver` 编译的就是这个聚合目标。

**(c) feature_config.cmake：特性宏的「一次配置，三处生效」**

[cmake/feature_config.cmake:11](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/cmake/feature_config.cmake#L11) —— 输入文件 `cmake/config/feature_config/${PRODUCT}.config`（每个芯片一份特性清单）。

[cmake/feature_config.cmake:14-23](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/cmake/feature_config.cmake#L14-L23) —— 用 `sed` 把同一份 `.config` 转成三种产物：
  - `feature.h`：C 源码用的 `#define CONFIG_xxx`（给用户态 `.so` 编译用）；
  - `feature.mk`：Makefile 用的 `CONFIG_DEFINES += -Dxxx`（给内核 `.ko` 的 Kbuild 式 Makefile 用）；
  - `feature.cmake`：CMake 的 `list(APPEND CONFIG_DEFINES xxx)`（给 cmake 侧用）。

这样一份配置同时驱动 C、Makefile、CMake 三个编译体系——这是 driver 多芯片适配的核心技巧。

#### 4.3.4 代码实践

**实践目标**：通过源码阅读，理解 `PRODUCT` 如何同时驱动 driver_config 与 feature_config 两套机制。

**操作步骤**：

1. 打开 `cmake/driver_config.cmake`，定位第 38 行，确认它用 `${PRODUCT}` 拼路径。
2. 打开 `cmake/feature_config.cmake`，定位第 11 行与第 14-23 行，理解同一份 `.config` 如何变成 `.h/.mk/.cmake`。
3. 列出特性配置输入目录，确认每个芯片各有一份配置：
   ```bash
   ls cmake/config/feature_config/
   ls cmake/config/driver_config/
   ```

**需要观察的现象**：步骤 3 应能看到按芯片（`ascend910B`、`ascend950` 等）命名的配置文件。

**预期结果**：能用自己的话说明——「`build.sh` 的 `--soc` 决定 `PRODUCT`，`PRODUCT` 又同时决定了加载哪份 driver_config 和哪份 feature_config，从而让同一份源码编译出适配不同芯片的驱动」。**目录下的确切文件名待本地确认。**

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `feature_config.cmake` 要生成 `.h`、`.mk`、`.cmake` 三种文件，而不是只生成一种？
  - **参考答案**：driver 既有用 CMake 编译的用户态 `.so`（用 `.h` 与 `.cmake`），又有用 Kbuild/Makefile 编译的内核 `.ko`（用 `.mk`）。三种产物让同一份特性配置同时服务于两套编译体系。
- **练习 2**：`CMakeLists.txt` 第 13-16 行的 `ENABLE_TEST` 分支起什么作用？
  - **参考答案**：当以 `-DENABLE_TEST` 配置时，工程只 `add_subdirectory(test)` 然后立即 `return()`，跳过整个驱动构建，只编译 UT——这是 UT 单元测试的独立编译入口。

### 4.4 run 包生成与部署安装

#### 4.4.1 概念说明

「run 包」是用 makeself 工具生成的自解压脚本：文件本身是一个 shell 脚本，尾部内嵌被压缩的安装内容。执行 `.run` 文件时，它会自解压到临时目录，然后调用内嵌的 `install.sh` 完成实际安装。driver 的 run 包名遵循固定格式 `Ascend-hdk-<chip_type>-driver-<version>_<os_version>-<arch>.run`，其中 `chip_type` 在包名里用的是 `910b / A3 / 950` 三种短名。

#### 4.4.2 核心流程

打包到部署的完整链路：

```text
build.sh --pkg
  └─ make package → CPack(External) → makeself_built_in.cmake
       ├─ package.py 生成 filelist.csv / scene.info
       ├─ 按 PRODUCT 选包名前缀（910b / A3 / 950）
       └─ makeself.sh 生成 .run，移动到 build_out/
部署：./Ascend-hdk-*.run --full          # 安装（需 root）
卸载：参考官方卸载指导                  # 见 QUICKSTART 第 5 节
```

#### 4.4.3 源码精读

**(a) package.cmake：选择打包配置**

[cmake/package.cmake:18-32](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/cmake/package.cmake#L18-L32) —— `pack_built_in()` 按 `BUILD_COMPONENT`（`DRIVER` 或 `DRIVER_COMPAT`）设置 `CPACK_PKG_NAME` 并 include 对应的 `package_driver.cmake`；`DRIVER` 分支还会确保 device 二进制库已被拉取。

**(b) package_driver.cmake：架构/OS/特性列表与 CPack 设置**

[cmake/config/package_config/package_driver.cmake:15-23](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/cmake/config/package_config/package_driver.cmake#L15-L23) —— 检测架构，`x86_64`→`x86_64`，`aarch64|arm64|arm`→`aarch64`。

[cmake/config/package_config/package_driver.cmake:37-56](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/cmake/config/package_config/package_driver.cmake#L37-L56) —— 设置 CPack 自定义变量：`CPACK_OS_VERSION = 发行版ID+版本`（如 `ubuntu20.04`），并按芯片选特性列表：

| 条件 | `CPACK_SOC_EX` | 特性列表 |
|------|----------------|----------|
| `ascend910B` 且 `ASCEND910_93_EX` | `ascend910_93` | `feature_910_93.list` |
| `ascend910B` | — | `feature_910b.list` |
| `ascend950` 且 `ENABLE_UBE` | — | `feature_ub.list` |
| `ascend950` | — | `feature_pcie.list` |

[cmake/config/package_config/package_driver.cmake:70-78](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/cmake/config/package_config/package_driver.cmake#L70-L78) —— 把 CPack 生成器设为 `External`，外部打包脚本指向 `makeself_built_in.cmake`，并 `include(CPack)`。

**(c) makeself_built_in.cmake：决定包名前缀并真正打包**

[cmake/makeself_built_in.cmake:26-33](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/cmake/makeself_built_in.cmake#L26-L33) —— 调用 `scripts/package/package.py` 生成 `filelist.csv`（决定包里装哪些文件）与 `scene.info`。

[cmake/makeself_built_in.cmake:55-67](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/cmake/makeself_built_in.cmake#L55-L67) —— **包名前缀查找表**（与 QUICKSTART 第 126 行的 `910b/A3/950` 完全对应）：

| `CPACK_SOC` | `CPACK_SOC_EX` | 包名前缀 |
|-------------|----------------|----------|
| `ascend910B` | — | `Ascend-hdk-910b-driver` |
| `ascend910B` | `ascend910_93` | `Ascend-hdk-A3-driver` |
| `ascend950` | — | `Ascend-hdk-950-driver` |

[cmake/makeself_built_in.cmake:78-96](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/cmake/makeself_built_in.cmake#L78-L96) —— 调用 `makeself.sh` 生成 `.run`，再用 `mv` 把它移动到 `CPACK_PACKAGE_DIRECTORY`（即 `build_out`）。

**(d) 版本号来源**

[scripts/package/driver/ascend910B/scripts/sys_version/sys_version.conf:1](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/scripts/package/driver/ascend910B/scripts/sys_version/sys_version.conf#L1) —— 文件内容为 `8.5.T7.0.B053`。`build.sh` 第 305 行 `PROJECT_VERSION=$(cat .../sys_version.conf)` 读取它，最终成为包名中的 `<version>` 段。

**(e) 部署安装与卸载（来自 QUICKSTART）**

[docs/zh/QUICKSTART.md:118-131](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/docs/zh/QUICKSTART.md#L118-L131) —— 安装命令 `./Ascend-hdk-<chip_type>-driver-<version>_<os_version>-<arch>.run --full`；安装后用户编译生成的 driver 包会替换已安装 CANN 开发套件中的 driver 相关软件；固件包需另行从昇腾官网获取并按配套版本安装。

[docs/zh/QUICKSTART.md:137-139](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/docs/zh/QUICKSTART.md#L137-L139) —— 卸载需参考配套版本的官方卸载指导。

#### 4.4.4 代码实践

**实践目标**：完成一次端到端的「编译 → 安装 → 卸载」闭环。

**操作步骤**：

1. 按 4.2.4 编译出 `build_out/Ascend-hdk-910b-driver-*.run`。
2. （在目标机器、root 权限下）安装：
   ```bash
   ./Ascend-hdk-910b-driver-<version>_<os_version>-<arch>.run --full
   ```
3. 查看安装帮助（不实际执行，仅了解可传参数）：
   ```bash
   ./Ascend-hdk-910b-driver-*.run --help
   ```
4. 卸载：参考 QUICKSTART 第 5 节的官方卸载指导执行。

**需要观察的现象**：步骤 2 自解压并调用 `install.sh`，安装到默认路径（如 `/usr/local/Ascend/driver`）；如环境缺少 `HwHiAiUser` 属组会报错（见 FAQ 问题四）。

**预期结果**：安装完成后，用户编译的 driver 软件会替换已安装 CANN 套件中的 driver 相关软件。**能否在你的环境真正安装成功待本地验证**（需要真实的昇腾硬件与配套固件）。

#### 4.4.5 小练习与答案

- **练习 1**：同样是 `ascend910B` 这个 `PRODUCT`，为什么 `ascend910b` 和 `ascend910_93` 编出来的包名不同（`910b` vs `A3`）？
  - **参考答案**：因为 `ascend910_93` 多设了 `ASCEND910_93_EX=TRUE`，经过 CMake 传到 `package_driver.cmake` 设 `CPACK_SOC_EX=ascend910_93`，再经 `makeself_built_in.cmake` 第 57-58 行把包名前缀从 `Ascend-hdk-910b-driver` 改写为 `Ascend-hdk-A3-driver`。
- **练习 2**：安装时报「do not have root permission」该如何处理？
  - **参考答案**：见 FAQ 问题二，原因是用了非 root 用户安装，需切换到 root 用户重新执行（FAQ 问题四还给出安装时指定 `--install-username/--install-usergroup` 的方法）。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个贯通任务。

**任务**：以 `ascend910b` 为目标，从「空环境」一路走到「生成 run 包」，并用一张表把整条链路上的关键变量传递关系填出来。

**步骤**：

1. **环境**：在 openEuler 或 Ubuntu 上装齐 QUICKSTART 第 1 节列出的依赖；执行 `uname -r` 与 `ls /lib/modules/$(uname -r)/build` 确认内核头就绪。
2. **编译**：`bash build.sh --make_clean && bash build.sh --pkg --soc=ascend910b`。
3. **记录产物**：把 `build_out/` 下实际生成的 `.run` 文件名完整抄下来，拆出其中的 `<version>`、`<os_version>`、`<arch>` 三段。
4. **画变量链**：填出下表（答案见本讲正文，建议先自己默写再核对）：

   | 阶段 | 变量 | 取值（ascend910b 场景） | 决定位置 |
   |------|------|------------------------|----------|
   | shell | `COMPUTE_UNIT` | `ascend910b` | build.sh `checkopts` |
   | shell | `PRODUCT` | `ascend910B` | build.sh `get_product` |
   | cmake | 加载的 driver_config | `driver_config_ascend910B.cmake` | driver_config.cmake:38 |
   | cmake | 加载的 feature_config | `ascend910B.config` | feature_config.cmake:11 |
   | 打包 | 包名前缀 | `Ascend-hdk-910b-driver` | makeself_built_in.cmake:55-67 |
   | 打包 | `<version>` 段 | `8.5.T7.0.B053` | sys_version.conf |

5. **排错演练**：故意只装依赖不装 `linux-headers-$(uname -r)`，重新编译，观察是否复现 FAQ「问题三」的 `/lib/modules/xxx/build: No such file or directory`，再用 `-k` 指定内核路径或补装头文件解决。

> 若本地无昇腾硬件，步骤 1-3 的编译部分仍可在普通 Linux 上完成（生成 run 包不需要硬件），只是步骤 4.4.4 的真正安装需要硬件与配套固件——这部分明确标注「待本地验证」。

## 6. 本讲小结

- driver 的编译入口是唯一的 `build.sh`，它用 `getopts` 解析 `--soc/--pkg/--ube/-j/-k` 等参数，并通过 `check_param` 强制要求 `--soc`。
- 芯片选择的核心是 `get_product`：`ascend910b→ascend910B`、`ascend910_93→ascend910B+ASCEND910_93_EX`、`ascend950→ascend950`；其中 910_93 复用 910B 的 PRODUCT 靠额外标记区分。
- `build.sh` 把内部变量拼成 `-D...` 传给 cmake，调用 `cmake .. + make driver + make install`，`--pkg` 时再 `make package` 生成 run 包。
- CMake 侧，`PRODUCT` 同时驱动 `driver_config_${PRODUCT}.cmake`（构建目标）与 `${PRODUCT}.config`（特性宏，一份配置生成 `.h/.mk/.cmake` 三种产物）。
- run 包由 CPack(External) + makeself 生成，包名格式 `Ascend-hdk-<910b|A3|950>-driver-<version>_<os>-<arch>.run`，落在 `build_out/`，每次新编译会清掉上一次产物。
- 部署用 `./xxx.run --full`（需 root，可能需配 `HwHiAiUser` 属组），常见问题见 `docs/zh/FAQ.md`。

## 7. 下一步学习建议

- 下一讲 **u1-l3 仓库目录结构与三大源码组织**：编译跑通后，建议顺势看清 `src/ascend_hal`、`src/sdk_driver`、`src/custom` 三棵源码树的内部划分，理解你刚编译出来的产物分别来自哪里。
- 若想理解多芯片特性开关的细节，可继续阅读 `cmake/config/feature_config/` 与 `cmake/config/driver_config/` 下的各芯片配置文件（本讲 4.3 已点出路径）。
- 若关注 UT 编译路径，可预习根 `CMakeLists.txt` 第 13-16 行的 `ENABLE_TEST` 分支与 `test/` 目录（对应后续 **u8-l2 UT 单元测试体系与覆盖率**）。
- 排错时把 `docs/zh/FAQ.md` 当作清单逐条对照，本讲已覆盖其中的内核头、root 权限、属组、os_version、编译缓存五个典型问题。
