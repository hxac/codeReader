# 构建系统：build.sh 与 CMake 工程组织

## 1. 本讲目标

学完本讲，你应该能够：

- 知道用一条 `bash build.sh ...` 命令就能把 GE 源码编译成可安装的软件包。
- 看懂 `build.sh` 的常用参数（`--ge_compiler` / `--ge_executor` / `--dflow`、`--build-type`、`--asan`、`--cov`、`-j<N>` 等）分别控制什么。
- 理解 `build.sh` 只是一个「外壳」，真正组织编译的是顶层 `CMakeLists.txt`，并能说清它如何把三大子包（ge-compiler / ge-executor / dflow-executor）拆开编译。
- 区分 Release / Debug、AddressSanitizer（ASAN）、覆盖率（GCOV）等构建开关的用途与产物差异。
- 能独立运行 `bash build.sh --help`，并尝试只构建某一个组件，观察产物目录。

## 2. 前置知识

在进入源码前，先用大白话建立几个概念。

**什么是「构建」？**
源码是一堆 `.cc` / `.h` / `.py` 文本，设备只认识机器码。把源码翻译成可执行/可加载产物（库文件 `.so`、可执行文件、安装包）的过程就叫构建（build）。GE 用 **CMake** 这套跨平台构建工具来描述「要编译什么、怎么编译」，再用一个 **`build.sh`** 脚本把 CMake 的繁琐调用封装成一条命令。

**`build.sh` 与 `CMakeLists.txt` 的分工**
打个比方：`CMakeLists.txt` 是「菜谱」，写清楚每个模块要放哪些材料（源文件、依赖）、按什么火候（编译选项）做；`build.sh` 是「厨师」，负责按你点的菜（`--ge_compiler` 等）、把菜谱交给 CMake 这口锅去炒，最后装盘（打包成 `.run` 安装包）。

**为什么 GE 要拆成多个「子包」？**
GE 本身体量很大，但不是所有人都要全部功能。在线推理场景主要用执行器，离线编译场景主要用编译器，只有用到 DataFlow 异步流水时才需要 dflow。GE 因此把产物拆成三个可独立安装的子包，让你「按需编译」，缩短编译时间。

**几个必须先认识的环境概念**
- **CANN Toolkit**：GE 不是孤立编译的，它依赖一套已安装好的 CANN 开发套件（提供 securec、runtime、metadef 等基础库）。构建前必须 `source .../set_env.sh`，让 `ASCEND_HOME_PATH` 环境变量指向 CANN 安装路径。
- **Host / Device**：Host 指你编译所在的 x86/aarch64 服务器；Device 指昇腾芯片。本讲的构建主要发生在 Host 上。
- **`.run` 包**：CANN 生态特有的一种自解压安装脚本（本质是 shell 脚本 + 数据），用 `./xxx.run --full` 即可安装。

> 本讲承接 [u1-l2 源码目录结构与模块划分](u1-l2-directory-structure.md)：你已经知道仓库有 `compiler`、`runtime`、`parser`、`graph_metadef`、`dflow` 等顶层目录。本讲要回答的是——这些目录里的源码，是怎么被组织起来编译成三个子包的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [build.sh](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/build.sh) | 构建入口脚本：解析命令行参数、设置环境、为每个子包调用一次 `cmake` + `make` + `cpack`。 |
| [CMakeLists.txt](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/CMakeLists.txt) | 顶层 CMake 工程文件：定义三个合法子包、按子包聚合依赖目标（target）、配置编译选项与安装/打包规则。 |
| [docs/zh/build.md](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/docs/zh/build.md) | 官方构建说明：环境要求、依赖清单、编译命令、UT/ST 与覆盖率用法、安装卸载。 |
| [cmake/build_type.cmake](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/cmake/build_type.cmake) | 根据 `CMAKE_BUILD_TYPE` 设置通用编译选项（Release/Debug/GCOV/DT 等）。 |
| [version.cmake](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/version.cmake) | 声明各子包的版本号与构建/运行依赖（runtime、metadef、hcomm 等）。 |
| [tests/run_test.sh](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/tests/run_test.sh) | UT/ST 测试的编译执行入口（与覆盖率、ASAN 的另一套用法相关）。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1 build.sh 用法** —— 入口脚本如何解析参数并驱动编译。
2. **4.2 CMake 顶层组织** —— `CMakeLists.txt` 如何描述三个子包。
3. **4.3 构建选项与产物** —— Debug/ASAN/覆盖率开关，以及最终的 `.run` 产物。

### 4.1 build.sh 用法

#### 4.1.1 概念说明

`build.sh` 是站在仓库根目录的「一键编译」入口。它对外暴露一组人类友好的参数（如 `--ge_compiler`），对内把这些参数翻译成 CMake 变量（如 `CANN_PACKAGES=ge-compiler`），再调用 `cmake` / `make` / `cpack` 完成实际编译和打包。

它的设计目标有三点：

- **封装复杂性**：CMake 命令有十几个 `-D` 变量，普通使用者记不住；`build.sh` 只让你关心几个高层选项。
- **支持组件化**：可以只编译某个子包，节省时间。
- **保证环境正确**：在编译前强制检查 CANN 环境（`ASCEND_HOME_PATH`）、Python 路径等，避免「编到一半才报错」。

#### 4.1.2 核心流程

`build.sh` 的执行主线可以概括为：

```text
main()
  └─ checkopts "$@"            # 1. 解析命令行参数
        ├─ getopt 拆分参数
        ├─ 把 --ge_compiler 等映射到 BUILD_COMPONENT
        ├─ 若未指定任何组件 → 默认编译全部三个
        └─ 校验 ASCEND_HOME_PATH / python3 路径
  └─ build_pkg                 # 2. 逐个组件编译
        └─ 对 BUILD_COMPONENT 中每个 component：
             ├─ cmake ... -D CANN_PACKAGES=<component>  <BASEPATH>   # 配置
             ├─ make <component> -j<N>                                # 编译
             └─ cpack                                                 # 打包
  └─ copy_pkg                  # 3. 把 .run 包移动到 build_out/
```

要点：

- `BUILD_COMPONENT` 是一个用分号分隔的字符串，例如 `ge-compiler;ge-executor`。
- 每个组件在 `build/build/<component>/` 下独立 `cmake` 配置、独立 `make`，互不干扰。
- 线程数 `-j<N>` 默认取 `/proc/cpuinfo` 的 CPU 核数（`THREAD_NUM=$(grep -c ^processor /proc/cpuinfo)`），也可以用 `-j8` 显式指定。

#### 4.1.3 源码精读

**(1) 参数清单：`--help` 里能看到什么**

`build.sh` 的 `usage()` 函数列出了所有支持参数，这是你最先该看的「说明书」：

[build.sh:31-67](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/build.sh#L31-L67) —— 打印所有可用参数的 `usage()` 函数。

其中与组件化直接相关的是这三行，决定了你「要编译哪个子包」：

[build.sh:45-47](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/build.sh#L45-L47) —— `--ge_compiler`、`--ge_executor`、`--dflow` 三个组件开关，分别编译 ge-compiler、ge-executor、dflow-executor 子包。

其余常用参数：

- `--build-type=<Release|Debug>`：编译类型，默认 Release（见 [build.sh:52-53](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/build.sh#L52-L53)）。
- `--asan` / `--cov`：开启地址消毒器 / 覆盖率（见 [build.sh:50-51](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/build.sh#L50-L51)）。
- `--output_path=<PATH>`：产物输出目录，默认 `./output`（见 [build.sh:54-55](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/build.sh#L54-L55)）。

**(2) 参数解析：getopt 与组件拼接**

`checkopts()` 用 `getopt` 把长参数拆开。注意它先定义了三个子包的「正式名字」（带连字符，与 CMake 里一致）：

[build.sh:170-174](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/build.sh#L170-L174) —— 定义 `BUILD_COMPONENT_COMPILER=ge-compiler` 等常量与 `BUILD_OUT_PATH`。

`getopt` 的声明很长，但你能从中看出它支持哪些长选项：

[build.sh:177-180](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/build.sh#L177-L180) —— `getopt` 解析参数，长选项列表里包含 `ge_compiler,ge_executor,dflow,asan,tsan,cov,...`。

当用户写了 `--ge_compiler`，脚本会把它追加进 `BUILD_COMPONENT` 字符串：

[build.sh:198-214](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/build.sh#L198-L214) —— 三个 `case` 分支分别处理 `--ge_compiler` / `--ge_executor` / `--dflow`，通过 `append_build_component` 把组件名拼到分号分隔的列表里。

关键的一行「默认全编」逻辑：如果三个组件一个都没指定，就默认三个全编译。这是为什么 `bash build.sh`（不带任何参数）会编出所有子包的原因：

[build.sh:302-308](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/build.sh#L302-L308) —— 若未指定任何组件，则同时打开三个开关，`BUILD_COMPONENT` 设为全部三个组件。

> 注意源码里有一句重要注释：**dflow-executor 子包依赖 ge-executor 包，所以不能同时编译**。这是组件间的真实依赖关系，留作小练习。

**(3) 环境校验**

编译前，脚本强制要求 CANN 环境已 `source`，否则直接退出：

[build.sh:295-300](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/build.sh#L295-L300) —— 检查 `ASCEND_HOME_PATH` 环境变量，缺失则报错退出。

**(4) 真正的编译调用**

`build_single_pkg()` 是每个子包的实际编译函数，它对每个组件执行 `cmake` → `make` → `cpack` 三步：

[build.sh:435-473](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/build.sh#L435-L473) —— `build_single_pkg`：创建构建目录、调用 cmake 配置、make 编译、cpack 打包。

其中 `cmake` 那行命令把所有 shell 变量传给 CMake（节选关键字段）：

[build.sh:441-465](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/build.sh#L441-L465) —— cmake 配置命令，把 `CMAKE_BUILD_TYPE`、`ENABLE_ASAN`、`ENABLE_GCOV`、`CANN_PACKAGES`、`ASCEND_INSTALL_PATH` 等以 `-D` 形式传入。

注意 `-D CANN_PACKAGES=${component}` 这一项——它正是「只编译当前子包」的关键，CMake 顶层会根据它决定构建哪些目标（详见 4.2）。随后：

[build.sh:467-468](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/build.sh#L467-L468) —— `make <component>` 编译该组件的全部目标，`cpack` 把产物打包成 `.run`。

#### 4.1.4 代码实践

**实践目标**：不实际触发完整编译，仅通过阅读 `--help` 与源码，建立「参数 → CMake 变量 → 子包」的映射直觉。

**操作步骤**：

1. 进入仓库根目录，运行：
   ```bash
   bash build.sh --help
   ```
2. 对照 [build.sh:31-67](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/build.sh#L31-L67) 的 `usage()`，在输出里找到 `--ge_compiler`、`--ge_executor`、`--dflow`、`--build-type`、`--asan`、`--cov` 这几个选项。
3. 打开 [build.sh:441-465](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/build.sh#L441-L465)，找出每个高层参数对应传给 CMake 的 `-D` 变量，填一张表：

   | 高层参数 | 传入 CMake 的变量 |
   | --- | --- |
   | `--build-type=Debug` | `-D CMAKE_BUILD_TYPE=Debug` |
   | `--asan` | `-D ENABLE_ASAN=on` |
   | `--cov` | `-D ENABLE_GCOV=on` |
   | `--ge_compiler` | `-D CANN_PACKAGES=ge-compiler` |

**需要观察的现象**：`--help` 的输出与源码 `usage()` 完全一致；高层选项总是能在 `build_single_pkg` 的 `cmake` 命令里找到一个对应的 `-D`。

**预期结果**：你会清晰地看到 `build.sh` 的「翻译」作用——它只是把人类友好的选项翻译成 CMake 变量。

> 说明：本实践只读不写，不会真正编译，对环境无要求。如果机器没有 `ASCEND_HOME_PATH`，直接 `bash build.sh --help` 仍可成功（`-h` 在环境校验之前就 `exit 0` 了，见 [build.sh:186-189](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/build.sh#L186-L189)）。实际执行完整编译需要先按 [docs/zh/build.md](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/docs/zh/build.md) 安装 CANN 并 `source set_env.sh`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `bash build.sh`（不带任何组件参数）会编译出所有三个子包？请指出对应的源码位置。

**参考答案**：因为 `checkopts` 末尾有一段「默认全编」逻辑：当三个组件开关都没被置为 `on` 时，会把三者全部加入 `BUILD_COMPONENT`，见 [build.sh:302-308](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/build.sh#L302-L308)。

**练习 2**：源码注释说「dflow-executor 子包依赖 ge-executor 包，所以不能同时编译」。请找到这条注释，并思考：如果你想用 dflow，正确的编译顺序应该是什么？

**参考答案**：注释在 [build.sh:302](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/build.sh#L302)（函数 `checkopts` 内的注释行）。正确顺序是先 `bash build.sh --ge_executor` 编出并安装 ge-executor，再 `bash build.sh --dflow` 编译 dflow-executor，让 dflow 能链接到已安装的 ge-executor 产物。

---

### 4.2 CMake 顶层组织

#### 4.2.1 概念说明

`build.sh` 解决「怎么调用」，`CMakeLists.txt` 解决「编译什么」。GE 的顶层 `CMakeLists.txt` 主要做四件事：

1. **声明合法的子包集合**，并决定本次构建要编译哪些。
2. **用 `add_subdirectory` 把各模块纳入编译**（`compiler`、`runtime`、`parser`、`graph_metadef` 等）。
3. **为每个子包定义一个同名「聚合目标」**（custom target），把它依赖的所有库挂上去。
4. **配置安装（install）与打包（cpack）规则**，生成最终的 `.run` 包。

理解它的关键是「**目标（target）聚合**」思路：GE 没有把所有源文件堆在一个 target 里，而是先在各自子目录编译出很多小库（如 `graph`、`ge_compiler`、`parser_common`），再由顶层的 `ge-compiler` / `ge-executor` / `dflow-executor` 三个聚合目标把它们按子包归属串起来。

#### 4.2.2 核心流程

```text
顶层 CMakeLists.txt
  ├─ project() / set_common_params()              # 初始化、设编译选项
  ├─ SUPPORTED_COMPONENTS = "ge-compiler;ge-executor;dflow-executor"
  ├─ 根据 CANN_PACKAGES 计算 BUILD_COMPONENT       # 本次编译哪些子包
  ├─ add_subdirectory(base/api/runtime/compiler/parser/dflow/...)
  │      ↑ 每个子目录各自定义库 target
  └─ 若 BUILD_PKG_COMPONENT 为真（组件化模式）：
       ├─ add_custom_target(ge-compiler)   + add_dependencies(...)
       ├─ add_custom_target(ge-executor)   + add_dependencies(...)
       └─ add_custom_target(dflow-executor)+ add_dependencies(...)
       然后 include(cmake/package.cmake)   # 打包规则
```

CMake 里有两个变量容易混淆，先讲清：

| 变量 | 含义 |
| --- | --- |
| `CANN_PACKAGES` | `build.sh` 传入的「用户想编哪个子包」，例如 `ge-compiler`。 |
| `BUILD_COMPONENT` | CMake 解析后得到的「本次实际要编译的子包列表」。 |

#### 4.2.3 源码精读

**(1) 合法子包与选择逻辑**

顶层一开始就声明了三个合法子包的名字，并据此计算 `BUILD_COMPONENT`：

[CMakeLists.txt:31-48](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/CMakeLists.txt#L31-L48) —— 定义 `SUPPORTED_COMPONENTS`，并按 `CANN_PACKAGES`（来自 `build.sh` 的 `-D CANN_PACKAGES=...`）筛选出本次要编译的 `BUILD_COMPONENT`，据此决定是否进入「组件化打包模式」（`BUILD_PKG_COMPONENT`）。

这段逻辑解释了 4.1 里 `-D CANN_PACKAGES=ge-compiler` 的去向：CMake 在这里读它，从而知道本次只构建 ge-compiler。

**(2) 把各模块纳入编译**

无论编哪个子包，所有基础模块的子目录都会被 `add_subdirectory` 纳入（它们都是潜在的依赖提供方）：

[CMakeLists.txt:250-255](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/CMakeLists.txt#L250-L255) —— 依次 `add_subdirectory(base / api / runtime / compiler / parser / dflow)`，把六大模块的 CMake 子工程挂到顶层。

这与 [u1-l2](u1-l2-directory-structure.md) 讲的目录职责一一对应：`base` 基础组件、`api` 对外接口、`runtime` 执行器、`compiler` 编译器、`parser` 解析器、`dflow` 异步流水。

**(3) 三个聚合目标：谁属于哪个子包**

组件化模式下，顶层为每个子包建一个 `add_custom_target`，再用 `add_dependencies` 把它「该包含的库」全部挂上。这就是「按子包打包」的真正出处。

**ge-compiler**（编译器子包）：包含解析器、图优化、算子编译、引擎、ATC 工具等——凡是「编译期能力」都在这里：

[CMakeLists.txt:528-543](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/CMakeLists.txt#L528-L543) —— `ge-compiler` 聚合目标及其依赖：`fmk_parser`、`fmk_onnx_parser`、`ge_compiler`、`aicore_utils`、`fusion_pass`、`atc_atc.bin`、`ge_python` 等编译侧组件。

> 可以看到 ATC 工具（`atc_atc.bin`）、解析器（`fmk_onnx_parser`）、GE-Python（`ge_python`）都被归进了 ge-compiler 子包——因为这些都是「把模型编译成 OM」需要的能力，对应 [u1-l1](u1-l1-project-overview.md) 讲的离线编译链路。

**ge-executor**（执行器子包）：包含 v1/v2 执行器、ACL 模型接口、Lowering、运行时等——凡是「加载并执行 OM」需要的能力：

[CMakeLists.txt:545-551](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/CMakeLists.txt#L545-L551) —— `ge-executor` 聚合目标：`davinci_executor`、`hybrid_executor`、`ge_runner`、`om2_executor`、`lowering`、`acl_mdl` 等执行侧组件。

**dflow-executor**（DataFlow 子包）：包含 deployer、npu/host 执行器、flow_func 等 DataFlow 运行时组件：

[CMakeLists.txt:571-574](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/CMakeLists.txt#L571-L574) —— `dflow-executor` 聚合目标：`deployer_daemon`、`npu_executor_main`、`host_cpu_executor_main`、`flow_func` 等。

**(4) 版本与依赖声明**

每个子包的版本号、构建依赖、运行依赖在 `version.cmake` 中声明，`cpack` 打包时会用到：

[version.cmake](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/version.cmake) —— 声明 `ge-executor` / `ge-compiler` 的版本（如 `9.2.0`）及其对 runtime、metadef、hcomm 等的构建/运行依赖。

#### 4.2.4 代码实践

**实践目标**：弄清「某个库/工具到底属于哪个子包」，建立从源码模块到产物的映射。

**操作步骤**：

1. 打开 [CMakeLists.txt:528-574](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/CMakeLists.txt#L528-L574)。
2. 分别在 `ge-compiler`、`ge-executor`、`dflow-executor` 三个 `add_custom_target` 的 `add_dependencies` 列表里，找出以下组件各属于哪个子包：
   - `atc_atc.bin`（ATC 离线编译工具）
   - `fmk_onnx_parser`（ONNX 解析器）
   - `davinci_executor`（v1 执行器核心）
   - `acl_mdl`（ACL 模型接口）
   - `npu_executor_main`（DataFlow 的 NPU 执行器）

**需要观察的现象**：你会发现编译器/解析器/ATC 类组件都在 `ge-compiler`，执行器/ACL 类都在 `ge-executor`，DataFlow 类在 `dflow-executor`——这与「编译」和「执行」的职责划分完全吻合。

**预期结果**：能填出下表（参考答案见 4.2.5）。

> 本实践为纯源码阅读，不需要编译环境。

#### 4.2.5 小练习与答案

**练习 1**：把 4.2.4 里的五个组件归类到对应子包。

**参考答案**：

| 组件 | 所属子包 | 依据 |
| --- | --- | --- |
| `atc_atc.bin` | ge-compiler | [CMakeLists.txt:539](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/CMakeLists.txt#L539) |
| `fmk_onnx_parser` | ge-compiler | [CMakeLists.txt:530](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/CMakeLists.txt#L530) |
| `davinci_executor` | ge-executor | [CMakeLists.txt:546](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/CMakeLists.txt#L546) |
| `acl_mdl` | ge-executor | [CMakeLists.txt:549](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/CMakeLists.txt#L549) |
| `npu_executor_main` | dflow-executor | [CMakeLists.txt:572](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/CMakeLists.txt#L572) |

**练习 2**：为什么不直接用一个大 target 编译所有东西，而要拆成「子目录小库 + 顶层聚合目标」两层？

**参考答案**：拆分有两个好处——一是**编译复用**：很多基础库（如 `graph`、`ge_common`）被多个子包共享，先编成独立库可避免重复编译；二是**按需打包**：通过聚合目标可以精确控制「哪些库进 ge-compiler、哪些进 ge-executor」，从而支持只安装用户需要的那一个子包，减小部署体积。

---

### 4.3 构建选项与产物

#### 4.3.1 概念说明

同一份源码，可以用不同的「构建配置」编出行为不同的产物。GE 支持几类重要开关：

| 开关 | 作用 | 典型场景 |
| --- | --- | --- |
| `--build-type=Release/Debug` | 控制优化级别（`-O`）与调试符号（`-g`） | Release 用于正式发布，Debug 用于调试 |
| `--asan` | 开启 AddressSanitizer，运行期检测内存越界/泄漏 | 排查内存问题；UT 默认在 x86 上开启 |
| `--cov` | 开启 GCOV 代码覆盖率统计 | 度量测试覆盖率，指导补测试 |
| `--tsan` | 开启 ThreadSanitizer，检测数据竞争 | 排查多线程并发问题 |

最终产物是 `build_out/` 下的 `.run` 安装包，命名形如 `cann-<component>_<version>_<arch>.run`。

#### 4.3.2 核心流程

构建选项的传递链：

```text
命令行 --asan        →  build.sh: ENABLE_ASAN="on"
                      →  cmake -D ENABLE_ASAN=on
                      →  CMake 顶层读取，传给各子目录
                      →  编译时加 sanitizer 选项 / 链接 asan 库
```

构建类型的处理在 `cmake/build_type.cmake` 里：它根据 `CMAKE_BUILD_TYPE` 匹配 `GCOV` / `DT` 等关键字，决定通用编译选项集合。

产物的产生链：

```text
make <component>  →  生成 .so / 可执行文件（在 build/<component>/）
cpack             →  按 cmake/package.cmake 规则打包成 .run
copy_pkg          →  把 .run 移到 build_out/cann-<component>_<version>_<arch>.run
```

#### 4.3.3 源码精读

**(1) 构建类型如何影响编译选项**

`build.sh` 把 `--build-type` 透传为 `CMAKE_BUILD_TYPE`（见 [build.sh:235-239](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/build.sh#L235-L239)）。CMake 顶层再用 `build_type.cmake` 翻译成具体的 `-O` / `-g` 选项：

[cmake/build_type.cmake:11-33](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/cmake/build_type.cmake#L11-L33) —— 按 `CMAKE_BUILD_TYPE` 分支：`GCOV` 覆盖率模式清空优化选项；`DT` 测试模式用 `-O0 -g`；其余（Release 等）用 `${OPTIMIZE_OPTION} -fvisibility=hidden`。

注意覆盖率与测试模式会**主动关掉优化**（`-O0`），因为开了优化后代码行与源码行对应不上，覆盖率统计会失真。

**(2) ASAN 选项的传递**

`--asan` 在 `build.sh` 里设 `ENABLE_ASAN="on"`（见 [build.sh:215-218](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/build.sh#L215-L218)），再经 cmake 命令传为 `-D ENABLE_ASAN=on`（见 [build.sh:441-445](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/build.sh#L441-L445)，其中 `ENABLE_ASAN`/`ENABLE_TSAN`/`ENABLE_GCOV` 三项并列传入）。`ENABLE_ASAN` 随后在各模块 CMake 里被用来加 sanitizer 相关编译/链接选项，例如：

[graph_metadef/graph/CMakeLists.txt:153](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/graph_metadef/graph/CMakeLists.txt#L153) —— 当 `ENABLE_ASAN` 为真时，额外加 `-Werror=maybe-uninitialized`（ASAN 下未初始化使用更容易被放大为错误）。

> 说明：实际的 `-fsanitize=address` 链接选项由 CANN Toolkit 提供的 `init_cann_project()` / `set_common_params()`（外部 cmake 基础设施）统一注入，本仓库只负责把 `ENABLE_ASAN` 这个开关传下去。这一点对理解「为什么 grep 不到 `-fsanitize=address`」很重要。

**(3) 产物的命名与位置**

官方文档明确给出了产物路径与命名规则：

[docs/zh/build.md:106-114](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/docs/zh/build.md#L106-L114) —— 一键编译命令 `bash build.sh --<pkg_type>`，`<pkg_type>` 取值 `ge_compiler` / `ge_executor` / `dflow`，不设则全编；成功后在 `build_out/` 生成 `cann-<component>_<version>_<arch>.run`。

注意 `<component>` 用的是带连字符的正式名（`ge-compiler`），而命令行 `--<pkg_type>` 用的是下划线/短名（`ge_compiler` / `dflow`），二者在 `build.sh` 内部做了映射。

**(4) 依赖与环境前置**

`build.md` 还规定了编译前的依赖清单与检查手段：

[docs/zh/build.md:48-64](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/docs/zh/build.md#L48-L64) —— 编译依赖：GCC ≥ 7.3.x、Python3 ≥ 3.9.x、CMake ≥ 3.16.0（建议 3.20.0）、bash ≥ 5.1.16，以及 ccache/asan/lcov 等工具。

[docs/zh/build.md:82-87](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/docs/zh/build.md#L82-L87) —— 安装依赖后用 `bash scripts/check_env.sh` 检查环境，输出 `[PASS]/[WARNING]/[ERROR]` 三级结果。

#### 4.3.4 代码实践

**实践目标**：体验「只构建 ge_compiler 组件」并理解产物（本实践需要完整 CANN 环境；若无环境，请按「待本地验证」理解）。

**操作步骤**：

1. 按文档配置环境：
   ```bash
   source /usr/local/Ascend/cann/set_env.sh      # 让 ASCEND_HOME_PATH 就绪
   bash scripts/check_env.sh                      # 检查依赖
   ```
2. 只构建 ge-compiler 子包：
   ```bash
   bash build.sh --ge_compiler -j8
   ```
3. 编译完成后，查看产物目录：
   ```bash
   ls -l build_out/
   ```

**需要观察的现象**：`build_out/` 下应出现一个 `cann-ge-compiler_<version>_<arch>.run` 文件；因为只编了 ge-compiler，所以**不会**出现 `cann-ge-executor_*.run` 或 `cann-dflow-executor_*.run`。

**预期结果**：产物列表大致为（具体版本号/架构依环境而定）：

```text
build_out/
└── cann-ge-compiler_9.2.0_x86_64.run   # 示例文件名
```

> **待本地验证**：以上 `.run` 文件名与是否同时出现 `output/` 中的中间产物，取决于实际 CANN 版本与编译环境。若你只想阅读验证，可对照 [build.sh:393-409](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/build.sh#L393-L409) 的 `copy_pkg()` 理解命名逻辑：它在 Ubuntu 上会把 `.run` 重命名为 `cann-<component>-<version>-<ubuntu-ver>.x86_64.run`。

#### 4.3.5 小练习与答案

**练习 1**：为什么开启覆盖率（`--cov`）后要关掉编译优化？

**参考答案**：因为开了优化（如 `-O2`）后，编译器会内联、重排、删除代码，导致「机器码行」与「源码行」不再一一对应，gcov 统计到的覆盖率会失真（某些源码行看似没被执行）。所以 `build_type.cmake` 在 GCOV 模式下清空了优化选项，见 [cmake/build_type.cmake:11-15](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/cmake/build_type.cmake#L11-L15)。

**练习 2**：你执行 `bash build.sh --ge_compiler` 后，`build_out/` 里却没有 ge-executor 的包，这是 bug 吗？

**参考答案**：不是。`--ge_compiler` 只把 `ge-compiler` 加入 `BUILD_COMPONENT`，因此只编译并打包这一个子包。要得到全部三个包，应不带组件参数运行 `bash build.sh`（默认全编），或分别执行各组件命令。

## 5. 综合实践

把三个模块串起来，完成一个「从需求到命令」的小任务。

**场景**：你的同事想知道，如何只编译出「离线模型编译工具 ATC」所需的最小子包，并在编译时顺便做一次带 ASAN 的 Debug 构建，以便排查 ATC 的内存问题。

**任务**：

1. **判断用哪个子包**：根据 [u1-l1](u1-l1-project-overview.md) 讲的「ATC 是离线编译入口」和 [CMakeLists.txt:539](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/CMakeLists.txt#L539)（`atc_atc.bin` 在 `ge-compiler` 聚合目标里），确定 ATC 属于 ge-compiler 子包。
2. **写出构建命令**：组合 `build.sh` 的参数，满足「只编 ge-compiler + Debug + ASAN」。
3. **预测产物**：说出产物文件名与所在目录。
4. **画出数据流**：用文字或伪代码画出 `命令行参数 → build.sh 变量 → CMake -D 变量 → 编译选项 → 产物` 这条链路。

**参考答案**：

1. ATC 属于 **ge-compiler** 子包。
2. 命令：
   ```bash
   bash build.sh --ge_compiler --build-type=Debug --asan -j8
   ```
3. 产物：`build_out/cann-ge-compiler_<version>_<arch>.run`（Debug+ASAN 不改变文件名，只改变库内部的编译选项）。
4. 数据流：
   ```text
   --ge_compiler         → BUILD_COMPONENT=ge-compiler → cmake -D CANN_PACKAGES=ge-compiler
   --build-type=Debug    → CMAKE_BUILD_TYPE=Debug      → cmake -D CMAKE_BUILD_TYPE=Debug
                                                       → build_type.cmake 选 -O0/-g 等选项
   --asan                → ENABLE_ASAN=on              → cmake -D ENABLE_ASAN=on
                                                       → 各模块加 sanitizer 选项
   最终 → make ge-compiler → cpack → build_out/cann-ge-compiler_*.run
   ```

## 6. 本讲小结

- `build.sh` 是构建入口，用 `getopt` 解析 `--ge_compiler` / `--ge_executor` / `--dflow` / `--build-type` / `--asan` / `--cov` 等参数，再为每个子包调用一次 `cmake` + `make` + `cpack`。
- 「只编译某个子包」的关键是 `build.sh` 把 `CANN_PACKAGES=<component>` 传给 CMake；不指定任何组件时默认全编三个子包。
- 顶层 `CMakeLists.txt` 用 `add_subdirectory` 纳入 base/api/runtime/compiler/parser/dflow 六大模块，再用 `ge-compiler` / `ge-executor` / `dflow-executor` 三个聚合目标把库按子包归属串起来。
- 子包的职责划分与编译/执行链路对应：ATC、解析器、GE-Python 在 ge-compiler；执行器、ACL 接口在 ge-executor；DataFlow 运行时在 dflow-executor。
- 构建选项经 `build.sh` → `-D` → CMake 逐层传递：`--build-type` 经 `build_type.cmake` 转成优化/调试选项，`--cov` 会关掉优化以保覆盖率准确，`--asan` 由 CANN cmake 基础设施注入 sanitizer。
- 最终产物是 `build_out/cann-<component>_<version>_<arch>.run` 安装包，可用 `./xxx.run --full` 安装、替换已装 CANN 中的 GE 组件。

## 7. 下一步学习建议

本讲解决的是「怎么把源码编出来」。接下来建议：

- **进入数据结构基石**：阅读 [u2-l1 AscendIR 四层对象模型](u2-l2-ascendir-object-model.md)，了解编译产物 OM 背后的图数据结构 `ComputeGraph` / `Node` / `OpDesc` / `Tensor`。
- **看编译链路如何被触发**：阅读 [u3-l3 ATC 离线编译工具链](u3-l3-atc-toolchain.md)，理解本讲编出的 `atc_atc.bin` 是如何把一个模型文件编译成 OM 的。
- **动手跑测试**：在搭好环境后，参考 [docs/zh/build.md:120-186](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/docs/zh/build.md#L120-L186) 用 `bash tests/run_test.sh --ut=<target>` 体验 UT 编译运行（这也是 ASAN/覆盖率的另一套入口），为后续 [u9-l5 测试体系](u9-l5-testing-and-contribution.md) 打基础。
