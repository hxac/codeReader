# 编译体系：build.sh 与 CMake 工程走读

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `build.sh` 的整体流程：它拆成了哪些子脚本、`main()` 按什么顺序调度它们。
2. 掌握最常用的参数组合：`--pkg`、`--soc`、`--ops`、`-u`（UT 编译）、`-j`、`--vendor_name`，以及哪些参数互斥。
3. 理解 shell 参数是如何一步步变成 CMake 变量（如 `ASCEND_OP_NAME`、`ASCEND_COMPUTE_UNIT`）的。
4. 看懂根 `CMakeLists.txt` 与 `cmake/` 目录（`variables.cmake`、`opbuild.cmake`、`gen_ops_info.cmake` 等）如何分工：谁定义开关、谁定义路径、谁生成算子信息。
5. 能独立完成一次单算子编译（以 `add_example` 为例），并知道编译产物去哪了、怎么安装。

## 2. 前置知识

- **Shell 脚本**：`build.sh` 是一个 bash 脚本，会用 `source` 加载其他脚本、用 `getopts` 解析命令行参数。你只需要懂"函数调用 + if 判断"级别的 bash。
- **CMake**：C++ 项目常用的构建系统生成器。`CMakeLists.txt` 描述"编什么、怎么编"，CMake 据此生成 Makefile 再编译。`option(X "描述" ON/OFF)` 定义开关，`-DX=TRUE` 从命令行传值，`add_subdirectory(dir)` 把子目录加入编译。
- **SoC / 昇腾芯片型号**：`--soc` 指定目标芯片，例如 `ascend910b`（Atlas A2 系列）、`ascend910_93`（Atlas A3 系列）、`ascend950`。同一份源码要为不同芯片生成不同的算子二进制，所以每次编译只能指定一个型号。
- **run 包**：编译最终产物是一个自解压安装包（`.run` 文件），安装后挂载到 CANN 环境中，上层才能调用里面的算子。上一讲（u1-l2）已经建立"算子工程交付件"的概念，本讲讲的是"这些交付件如何被批量编译并打包"。

上一讲我们知道了仓库由几十个结构一致的单算子工程组成；本讲回答的问题是：**这么多工程，是如何被一套统一的编译体系管理起来的？**

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [build.sh](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/build.sh) | 编译总入口：加载子脚本、按序调度各构建阶段 |
| [scripts/build_conf.sh](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/scripts/build_conf.sh) | 全局配置：支持的 SoC 列表、build/build_out 路径、仓库名 |
| [scripts/build_options.sh](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/scripts/build_options.sh) | 参数解析、互斥校验、帮助信息（本讲重点之一） |
| [scripts/build_cmake.sh](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/scripts/build_cmake.sh) | 把 shell 变量装配成 CMake 参数并执行 `cmake` 命令 |
| [CMakeLists.txt](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/CMakeLists.txt) | CMake 主入口：定义全局开关、include cmake 模块、收集算子目录 |
| [cmake/variables.cmake](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/cmake/variables.cmake) | 全局变量：库名、安装路径、待编译算子集合、工具路径 |
| [cmake/opbuild.cmake](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/cmake/opbuild.cmake) | 调用 opbuild 工具，从 `*_def.cpp` 生成 aclnn 接口代码与算子信息 |
| [cmake/gen_ops_info.cmake](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/cmake/gen_ops_info.cmake) | 拷贝 kernel 源码、生成 `aic-xxx-ops-info.json`、触发二进制编译 |
| [cmake/func.cmake](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/cmake/func.cmake) | 公共函数库，含 `check_compiled_ops`（校验 --ops 算子名） |
| [docs/zh/install/compile.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/install/compile.md) | 官方源码构建文档（命令与安装步骤） |
| [docs/zh/install/build.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/install/build.md) | 官方 build.sh 参数说明表 |

## 4. 核心概念与源码讲解

### 4.1 build.sh 主流程：脚本分层与 main 调度

#### 4.1.1 概念说明

`build.sh` 是编译的唯一入口，但它本身不到 70 行——所有逻辑被拆分到 `scripts/` 目录下的 8 个子脚本中，每个脚本负责一个阶段。这种"薄入口 + 分层脚本"的写法让各阶段可以独立维护（例如 UT 构建逻辑变化只改 `build_ut.sh`）。

#### 4.1.2 核心流程

```text
bash build.sh --pkg --soc=ascend910b --ops=add_example -j16
   │
   ├─ source build_conf.sh / build_clean.sh / build_options.sh / build_cmake.sh
   │         build_lib.sh / build_ut.sh / build_example.sh / build_genop.sh
   │
   └─ main()
       ├─ checkopts            解析参数 + 互斥校验（→ 4.2）
       ├─ assemble_cmake_args  装配 CMAKE_ARGS（→ 4.3）
       ├─ clean_build_binary   清理旧产物
       ├─ cmake_init           执行 cmake 配置（→ 4.3）
       ├─ build_lib            编译 ophost/opapi/opgraph 等库（按需）
       ├─ build_binary         编译算子二进制（按需）
       ├─ build_static_lib     静态库（--static）
       ├─ build_package        打 run 包（--pkg）
       ├─ build_ut             单元测试（-u / *_test）
       ├─ build_example        编译并运行样例（--run_example）
       └─ gen_op / gen_aicpu_op 创建算子初始目录（--genop）
```

#### 4.1.3 源码精读

主入口先 source 全部子脚本，把它们的函数加载进当前 shell：

- [build.sh:L17-L24](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/build.sh#L17-L24) —— 加载 8 个子脚本：`build_conf`（全局配置）、`build_clean`（清理）、`build_options`（参数解析）、`build_cmake`（CMake 装配）、`build_lib`（库编译）、`build_ut`（UT）、`build_example`（样例）、`build_genop`（算子脚手架）。

`main()` 是一张"构建阶段清单"，每个阶段由一个布尔变量控制是否执行：

- [build.sh:L26-L60](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/build.sh#L26-L60) —— 依次执行：参数检查 → 装配 CMake 参数 → 清理 → `cmake_init`；随后按 `ENABLE_CREATE_LIB`、`ENABLE_BINARY/ENABLE_CUSTOM`、`ENABLE_STATIC`、`ENABLE_PACKAGE`、`ENABLE_TEST`、`ENABLE_RUN_EXAMPLE`、`ENABLE_GENOP` 等开关决定执行哪些构建函数。你传的每个命令行参数，最终就是在点亮这串开关中的某几个。

两个容易忽略的细节：

- [build.sh:L62-L64](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/build.sh#L62-L64) —— 不带任何参数直接运行 `bash build.sh` 会打印帮助并退出，这是初学者最安全的入门命令。
- [build.sh:L65-L66](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/build.sh#L65-L66) —— `main` 的全部输出通过 `while read` 逐行加上时间戳再打印，方便从日志判断每个阶段的耗时。

编译目录在 `build_conf.sh` 中定义：

- [scripts/build_conf.sh:L32-L34](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/scripts/build_conf.sh#L32-L34) —— `BUILD_PATH=仓库/build`（CMake 构建目录）、`BUILD_OUT_PATH=仓库/build_out`（最终 run 包输出目录）、`REPOSITORY_NAME=cv`（这就是库名里 `ophost_cv.so` 中 `cv` 的来源）。

#### 4.1.4 代码实践

1. **实践目标**：不看任何文档，仅凭 `--help` 了解 build.sh 的能力分层。
2. **操作步骤**：在仓库根目录执行 `bash build.sh --help`、`bash build.sh --pkg --help`、`bash build.sh -u --help`。
3. **需要观察的现象**：三种命令打印的帮助内容不同——第一种是总览，第二种是打包（package）场景的参数与示例，第三种是测试（test）场景的参数与示例。
4. **预期结果**：能找到每种场景下的"Examples"段落。`bash build.sh --pkg --soc=ascend910b --ops=grid_sample,crop_and_resize --build-type=Debug` 这条示例就来自 [scripts/build_options.sh:L51](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/scripts/build_options.sh#L51)。
5. 本节实践只读不写，无环境依赖，可直接在任意克隆仓库执行（待本地验证：帮助文本以你机器上的输出为准）。

#### 4.1.5 小练习与答案

**练习 1**：`build.sh` 自己只有几十行，参数解析逻辑在哪里？
答：在 [scripts/build_options.sh](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/scripts/build_options.sh) 的 `checkopts` 函数中，`build.sh` 通过 `source` 加载后调用。

**练习 2**：`main()` 里为什么 `build_binary` 的条件是 `(ENABLE_BINARY 或 ENABLE_CUSTOM) 且非 JIT`？
答：`--pkg`/`--static` 会打开 `ENABLE_BINARY`；`--ops=xx`/`--vendor_name`/`--experimental` 会打开 `ENABLE_CUSTOM`（自定义算子场景同样需要编二进制）；而 `--jit` 表示图运行态在线编译、不需要预编二进制，所以要排除。

### 4.2 参数解析与组合校验：build_options.sh

#### 4.2.1 概念说明

build.sh 支持三十多个参数，很多参数之间存在互斥关系（例如 `--pkg` 不能和 `-u` 同时用）。这些规则全部集中在 `build_options.sh` 中，分三步执行：先逐个合法性检查，再 `getopts` 循环解析赋值给 shell 变量，最后做组合校验并推导内部模式。

#### 4.2.2 核心流程

```text
checkopts "$@"
  ├─ 1) 预检：每个 - 开头参数必须是合法选项；--pkg-type 必须带合法值
  ├─ 2) --help 分场景打印帮助并退出
  ├─ 3) getopts 循环：
  │      --ops=xx,yy   → COMPILED_OPS="xx,yy"，ENABLE_CUSTOM=TRUE
  │      --soc=xxx     → COMPUTE_UNIT=xxx
  │      --pkg         → ENABLE_BINARY=TRUE, ENABLE_PACKAGE=TRUE
  │      -u            → ENABLE_TEST=TRUE
  │      --vendor_name → VENDOR_NAME=xxx，ENABLE_CUSTOM=TRUE
  │      ...（每个选项点亮若干开关）
  ├─ 4) check_param()       组合互斥校验，非法组合直接 exit 1
  ├─ 5) set_create_libs()   推导要编译哪些库（ophost/opapi/opgraph/插件）
  └─ 6) set_ut_mode()       推导 UT 目标（ophost/opapi/opkernel/...）
```

#### 4.2.3 源码精读

参数到 shell 变量的映射核心在 `checkopts` 的 getopts 循环：

- [scripts/build_options.sh:L785-L788](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/scripts/build_options.sh#L785-L788) —— `--ops=grid_sample,iou_v2` 被拆出值赋给 `COMPILED_OPS`（多个算子逗号分隔），同时置 `ENABLE_CUSTOM=TRUE`：指定算子子集就意味着走"自定义算子包"路线。
- [scripts/build_options.sh:L826-L830](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/scripts/build_options.sh#L826-L830) —— `--pkg` 同时点亮 `ENABLE_BINARY` 和 `ENABLE_PACKAGE` 两个开关；对照 4.1 的 `main()` 可见它们分别触发"编二进制"和"打 run 包"两个阶段。
- [scripts/build_options.sh:L795-L797](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/scripts/build_options.sh#L795-L797) —— `--soc=ascend910b` 只是把值存进 `COMPUTE_UNIT`，真正校验"是否为支持的芯片"发生在 4.3 节的 `assemble_cmake_args` 中。

互斥规则集中在 `check_param`：

- [scripts/build_options.sh:L399-L403](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/scripts/build_options.sh#L399-L403) —— `--ops` 不能与 `--ophost`/`--opapi`/`--opgraph` 同用（前者是"编算子包"，后者是"编单个库"，语义冲突），除非处于 UT 模式。
- [scripts/build_options.sh:L406-L425](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/scripts/build_options.sh#L406-L425) —— `--pkg` 不能与 UT 模式、`--ophost`/`--opapi`/`--opgraph`、`--genop` 同时使用。

UT 模式的推导在 `set_ut_mode`：

- [scripts/build_options.sh:L545-L590](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/scripts/build_options.sh#L545-L590) —— `-u` 默认全部 UT（`UT_TEST_ALL=TRUE`）；叠加 `--ophost`/`--opapi`/`--opkernel`/`--opkernel_aicpu` 则只跑对应侧的 UT，并把目标名（如 `cv_op_host_ut`）追加进 `UT_TARGETS`。这解释了官方文档 [docs/zh/install/compile.md:L243](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/install/compile.md#L243) 中 `bash build.sh -u --ophost --ops=grid_sample` 这类写法。

#### 4.2.4 代码实践

1. **实践目标**：验证互斥规则确实生效，加深对参数分组的理解。
2. **操作步骤**：依次执行两条命令：
   - `bash build.sh --pkg -u`（预期报错）
   - `bash build.sh --pkg --soc=ascend910b --ops=not_exist_op`（预期在 CMake 阶段报错，见 4.5）
3. **需要观察的现象**：第一条命令应立刻打印 `[ERROR] --pkg cannot be used with test(-u, --ophost_test, etc.)` 并退出；第二条命令会通过参数检查，在 CMake 配置阶段由 `check_compiled_ops` 报 `Specified ops not found...`（见 4.5.3）。
4. **预期结果**：能区分"shell 层立即拦截的错误"与"CMake 层校验的错误"发生在不同阶段。
5. 第一条命令无环境依赖可直接验证；第二条需要进入 CMake 阶段（依赖 CANN toolkit 环境），待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`bash build.sh --pkg --soc=ascend910b --ops=grid_sample` 中，三个参数分别点亮了什么？
答：`--pkg` → `ENABLE_BINARY=TRUE, ENABLE_PACKAGE=TRUE`；`--soc=ascend910b` → `COMPUTE_UNIT=ascend910b`；`--ops=grid_sample` → `COMPILED_OPS=grid_sample, ENABLE_CUSTOM=TRUE`。

**练习 2**：为什么 `--ops` 会隐式打开"自定义包"模式？
答：`--ops` 表示只编译仓库中部分算子，产物以挂载（vendors）方式作用于 CANN 包，这正是"自定义算子包"的定义（见 [docs/zh/install/compile.md:L25-L31](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/install/compile.md#L25-L31)）；而整包（ops-cv 包）编译全部算子，无需 `--ops`。

**练习 3**：`-u` 与 `--ophost_test` 是什么关系？
答：等价。`--ophost_test` 在解析时会把 `ENABLE_TEST` 置真并截掉 `_test` 后缀归一到 `ophost`（[scripts/build_options.sh:L873-L878](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/scripts/build_options.sh#L873-L878)），随后 `set_ut_mode` 只勾选 ophost 侧 UT 目标。

### 4.3 从 shell 到 CMake：参数装配与 cmake 初始化

#### 4.3.1 概念说明

真正的编译由 CMake 完成，`build.sh` 的职责是把用户友好的命令行参数翻译成一串 `-D` 开头的 CMake 变量。理解这条"翻译链"，你就能从任何一条 build.sh 命令反推出 CMake 看到了什么配置。

#### 4.3.2 核心流程

```text
assemble_cmake_args()
  逐个检查 shell 开关 → 拼接 CMAKE_ARGS 字符串
    --ops=a,b    →  逗号转分号  →  -DASCEND_OP_NAME=a;b
    --vendor_name →  -DVENDOR_NAME=xxx
    --soc=xxx    →  校验在支持列表内 → -DASCEND_COMPUTE_UNIT=xxx
    --pkg        →  -DENABLE_PACKAGE=TRUE -DPACKAGE_TYPE=run
    -u           →  -DENABLE_TEST=TRUE
    ...
cmake_init()
  mkdir build / build_out → 删除旧 CMakeCache.txt
  cd build && cmake ${CMAKE_ARGS} ..
```

#### 4.3.3 源码精读

- [scripts/build_cmake.sh:L14-L22](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/scripts/build_cmake.sh#L14-L22) —— `custom_cmake_args` 做了一个关键转换：`--ops=grid_sample,iou_v2` 中的英文逗号被替换成分号，变成 CMake 列表 `-DASCEND_OP_NAME=grid_sample;iou_v2`。CMake 中分号是列表分隔符，根 CMakeLists 的 `"add_example" IN_LIST ASCEND_OP_NAME` 判断就依赖这个格式。
- [scripts/build_cmake.sh:L71-L77](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/scripts/build_cmake.sh#L71-L77) —— `ENABLE_BINARY` 与 `ENABLE_CUSTOM` 传给 CMake；注意 `ENABLE_CUSTOM=TRUE` 时会强制附带 `-DENABLE_BINARY=TRUE`。
- [scripts/build_cmake.sh:L108-L124](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/scripts/build_cmake.sh#L108-L124) —— `--soc` 在这里做白名单校验：`normalize_compute_unit` 归一化后必须命中 `SUPPORT_COMPUTE_UNIT_SHORT` 列表（定义在 [scripts/build_conf.sh:L16](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/scripts/build_conf.sh#L16)，含 ascend910b、ascend910_93、ascend950 等），否则报 `soc only support : ...` 退出；合法则追加 `-DASCEND_COMPUTE_UNIT=xxx`。
- [scripts/build_cmake.sh:L133-L147](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/scripts/build_cmake.sh#L133-L147) —— `cmake_init` 在 `build/` 目录执行 `cmake ${CMAKE_ARGS} ..`，每次都会先删除 `CMakeCache.txt`，避免上一次不同参数的缓存污染本次配置。`build.sh` 第 29 行的 `echo "CMAKE_ARGS: ..."` 会把这串参数完整打印出来——这是排查"我传的参数到底生效没有"的第一入口。

#### 4.3.4 代码实践

1. **实践目标**：学会用 CMAKE_ARGS 日志核对参数翻译。
2. **操作步骤**：执行 `bash build.sh --pkg --soc=ascend910b --ops=grid_sample -j16`，观察脚本开头的 `CMAKE_ARGS: ...` 一行。
3. **需要观察的现象**：该行应包含 `-DASCEND_OP_NAME=grid_sample`、`-DASCEND_COMPUTE_UNIT=ascend910b`、`-DENABLE_PACKAGE=TRUE`、`-DENABLE_CUSTOM=TRUE` 等片段。
4. **预期结果**：能把命令行参数与 CMAKE_ARGS 中的 `-D` 项一一对应；再对照 [CMakeLists.txt:L40-L76](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/CMakeLists.txt#L40-L76) 的 `option` 声明确认变量名。
5. 依赖 CANN toolkit 编译环境，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么每次编译都要删除 `CMakeCache.txt`？
答：CMake 会缓存首次配置的变量值；由于本项目每次编译的 `ASCEND_OP_NAME`、`ASCEND_COMPUTE_UNIT` 等可能不同，残留缓存会导致"传了参数但没生效"，所以 [scripts/build_cmake.sh:L144](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/scripts/build_cmake.sh#L144) 强制删除。

**练习 2**：`--ops=a,b,c` 中的逗号为什么必须转成分号再传给 CMake？
答：CMake 的列表以分号分隔；根 CMakeLists 用 `IN_LIST` 判断算子是否在编译清单中（如 [CMakeLists.txt:L161](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/CMakeLists.txt#L161)），转分号后 CMake 才能把它识别为列表。

### 4.4 根 CMakeLists.txt：全局开关与源码目录组织

#### 4.4.1 概念说明

根 `CMakeLists.txt` 是 CMake 侧的"总调度"：定义所有 `option` 开关、include `cmake/` 下的功能模块、决定哪些目录参与编译。上一讲说过"根 CMakeLists 自动收集带 CMakeLists.txt 的子目录"——本节看它的具体实现。

#### 4.4.2 核心流程

```text
CMakeLists.txt
  ├─ include fetch_cann_cmake.cmake / init_cann_project()   接入 CANN 官方构建框架
  ├─ 定义 option 开关（对应 build.sh 传来的 -D 变量）
  ├─ include cmake/ 模块：opbase、dependencies、variables、opbuild、func、ut ...
  ├─ add_subdirectory(common)                                公共库
  ├─ ENABLE_EXPERIMENTAL ? add_subdirectory(experimental/*) : add_subdirectory(image / objdetect / framework)
  ├─ add_example ∈ ASCEND_OP_NAME ? add_subdirectory(examples)
  ├─ check_compiled_ops()                                    校验算子名合法性
  └─ BUILD_WITH_INSTALLED_DEPENDENCY_CANN_PKG ? gen_ops_info_and_python() + 打包
```

#### 4.4.3 源码精读

- [CMakeLists.txt:L40-L59](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/CMakeLists.txt#L40-L59) —— 一整排 `option` 开关：`ENABLE_TEST`（UT）、`ENABLE_BINARY`（二进制）、`ENABLE_CUSTOM`（自定义包）、`ENABLE_PACKAGE`（打包）、`ENABLE_EXPERIMENTAL`（贡献目录）等，全部默认 OFF，由 build.sh 传入的 `-D` 点亮。UT 侧还有 `OP_HOST_UT`/`OP_API_UT`/`OP_KERNEL_UT` 等细分开关。
- [CMakeLists.txt:L70-L71](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/CMakeLists.txt#L70-L71) —— `ASCEND_COMPUTE_UNIT` 默认 `ascend910b`；`ASCEND_ALL_COMPUTE_UNIT` 列出全部支持的芯片型号，后面 `gen_ops_info_and_python` 会为其中每个型号生成算子信息文件。
- [CMakeLists.txt:L111-L130](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/CMakeLists.txt#L111-L130) —— 依次 include `opbase.cmake`（须在 dependencies 前）、`dependencies.cmake`、`variables.cmake`、`opbuild.cmake`、`func.cmake`、`ut.cmake` 等——这就是 `cmake/` 目录各模块的挂载点。
- [CMakeLists.txt:L137-L150](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/CMakeLists.txt#L137-L150) —— 目录收集的核心：`add_subdirectory(common)` 无条件执行；`ENABLE_EXPERIMENTAL` 打开时编译 `experimental/image`、`experimental/objdetect`（贡献算子区），否则编译正式的 `image`、`objdetect` 和 `common/src/framework`。image/objdetect 内部再递归收集各算子目录（即上一讲的"目录存在即参与编译"）。
- [CMakeLists.txt:L161-L163](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/CMakeLists.txt#L161-L163) —— 一个很有意思的特判：只有当 `add_example` 或 `add_example_aicpu` 出现在 `--ops` 清单里时，`examples/` 目录才参与编译。这就是本讲实践中编译 `add_example` 的机制依据。
- [CMakeLists.txt:L167-L191](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/CMakeLists.txt#L167-L191) —— 收尾阶段：`gen_ops_info_and_python()` 生成算子信息（→ 4.5），`symbol.cmake` 生成符号表，`ENABLE_PACKAGE` 时由 `package.cmake` 决定走 `pack_custom()`（自定义包）还是 `pack_built_in()`（整包）。

#### 4.4.4 代码实践

1. **实践目标**：验证 `examples/` 目录是按需编译的。
2. **操作步骤**：
   1. 执行 `bash build.sh --pkg --soc=ascend910b --ops=grid_sample`，在 CMake 配置日志中搜索 `examples`。
   2. 再执行 `bash build.sh --pkg --soc=ascend910b --ops=add_example`，同样搜索。
3. **需要观察的现象**：第一次日志中不应出现 `examples` 子目录的配置信息；第二次应出现 `compile project with src` 后跟 examples 目录的处理。
4. **预期结果**：确认 [CMakeLists.txt:L161-L163](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/CMakeLists.txt#L161-L163) 的条件生效——示例算子目录平时不参与编译，只有显式 `--ops=add_example` 才进入构建。
5. 依赖编译环境，待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`option(ENABLE_PACKAGE ...)` 默认 OFF，那怎么打开？
答：build.sh 的 `--pkg` 在 `assemble_cmake_args` 中追加 `-DENABLE_PACKAGE=TRUE`（[scripts/build_cmake.sh:L78-L80](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/scripts/build_cmake.sh#L78-L80)），命令行 `-D` 会覆盖 option 默认值。

**练习 2**：`--experimental` 为什么会改变 `add_subdirectory` 的目标？
答：该参数点亮 `ENABLE_EXPERIMENTAL`（[CMakeLists.txt:L138-L150](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/CMakeLists.txt#L138-L150)），使编译目标从正式目录 `image`/`objdetect` 切换到 `experimental/image`/`experimental/objdetect`，即社区贡献的算子区。

### 4.5 cmake 目录：variables / opbuild / gen_ops_info 的分工

#### 4.5.1 概念说明

`cmake/` 目录是编译体系的"CMake 功能库"。本讲聚焦三个文件：

- `variables.cmake`：定义全局变量——库名、安装路径、**待编译算子集合**（`NEED_COMPILE_OPS`/`COMPILED_OPS`）、opbuild 工具路径。
- `opbuild.cmake`：调用 CANN 的 `op_build` 工具，从算子的 `*_def.cpp` 自动生成 aclnn 接口代码（`.cpp/.h`）与算子描述信息。
- `gen_ops_info.cmake`：拷贝 kernel 源码到构建区、为每个芯片型号生成 `aic-<soc>-ops-info.json`、触发二进制编译。

`func.cmake` 中的 `check_compiled_ops` 负责"算子名写错就报错"的兜底校验。

#### 4.5.2 核心流程

```text
variables.cmake  ──提供──▶  NEED_COMPILE_OPS / COMPILED_OPS / 各安装路径 / OP_BUILD_TOOL
                                │
gen_ops_info_and_python()  (gen_ops_info.cmake)
  ├─ gen_aclnn_with_opdef()   (opbuild.cmake)
  │     对 aclnn / aclnnInner / aclnnExc 三类 def 文件分别调 op_build 生成接口代码
  ├─ kernel_src_copy()        把各算子 op_kernel 源码拷到 build/tbe/ascendc
  ├─ 对 ASCEND_ALL_COMPUTE_UNIT 中每个芯片生成 ops-info.json + 合并 ini
  └─ 对 ASCEND_COMPUTE_UNIT（--soc 指定的型号）触发真正的二进制编译

check_compiled_ops()  (func.cmake)
  NEED_COMPILE_OPS(--ops 传入) 与 COMPILED_OPS(实际收集到的) 求差集，差集非空即 FATAL_ERROR
```

#### 4.5.3 源码精读

- [cmake/variables.cmake:L34-L40](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/cmake/variables.cmake#L34-L40) —— 定义编译范围的两个核心变量：`NEED_COMPILE_OPS` 来自 `--ops`（用户想要的），`COMPILED_OPS` 记录实际被各算子目录注册进来的算子（实际拥有的）。二者在配置结束时由 `check_compiled_ops` 对账。
- [cmake/variables.cmake:L57-L77](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/cmake/variables.cmake#L57-L77) —— 自定义包（`ENABLE_CUSTOM`）的安装路径以 `packages/vendors/<vendor>_cv/` 开头（op_api 头文件、库、算子信息、kernel 二进制各有子路径）——这就是"挂载式"自定义包在磁盘上的形态；对应 `--vendor_name` 默认值 `custom`，所以装完后在 `${ASCEND_HOME_PATH}/opp/vendors/custom_cv` 下能找到产物。
- [cmake/variables.cmake:L124-L126](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/cmake/variables.cmake#L124-L126) —— 定位 CANN 自带的编译工具：`OP_BUILD_TOOL=${ASCEND_DIR}/tools/opbuild/op_build`，即 opbuild.cmake 调用的外部命令。
- [cmake/variables.cmake:L149](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/cmake/variables.cmake#L149) —— `CMAKE_INSTALL_PREFIX` 固定为源码根的 `build_out`，这就是官方文档说"run 包存放于 build_out 目录"的实现依据。
- [cmake/opbuild.cmake:L42-L56](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/cmake/opbuild.cmake#L42-L56) —— `gen_opbuild_target` 的关键 `add_custom_command`：先编译出一个临时的 `gen_op_host_<prefix>.so`（由各算子的 `*_def.cpp` 链接而成），再以 `OPS_PROTO_SEPARATE=1 OPS_ACLNN_GEN=...` 等环境变量启动 `${OP_BUILD_TOOL}` 加载该 so，生成 aclnn 接口源码和头文件到 `build/autogen/` 下。**上一讲说 add_example 的 op_api 是"自动生成"的，生成机制就是这里。**
- [cmake/opbuild.cmake:L58-L75](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/cmake/opbuild.cmake#L58-L75) —— `gen_aclnn_classify` 把 def 文件按前缀分成三类：`aclnn`（对外接口，生成接口代码）、`aclnnInner`（内部接口，输出到 `inner/` 子目录）、`aclnnExc`（只导出头文件、不生成实现）。
- [cmake/gen_ops_info.cmake:L15-L41](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/cmake/gen_ops_info.cmake#L15-L41) —— `kernel_src_copy`：为每个已编译算子创建 `<op>_src_copy` 目标，把其 `op_kernel` 目录整体拷贝到 `build/tbe/ascendc/<op_name>/`（若算子没有 op_kernel 目录则跳过），供后续统一编译二进制。
- [cmake/gen_ops_info.cmake:L558-L589](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/cmake/gen_ops_info.cmake#L558-L589) —— `gen_ops_info_and_python` 主流程：先 `gen_aclnn_with_opdef` 生成 aclnn 代码，再拷贝 kernel 源码，然后**遍历 `ASCEND_ALL_COMPUTE_UNIT` 全部芯片型号**生成各自的 `aic-<soc>-ops-info.json` 并合并 ini 配置——算子信息是全型号生成的，真正的二进制编译只针对 `ASCEND_COMPUTE_UNIT`（`--soc`）指定的型号。
- [cmake/gen_ops_info.cmake:L541-L555](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/cmake/gen_ops_info.cmake#L541-L555) —— `check_op_supported`：用 grep 检查算子 `*_def.cpp` 里是否有 `.AddConfig("<compute_unit>"` 声明，判断该算子是否支持目标芯片。这就是"某算子还没适配你的芯片型号"时被跳过的判定点。
- [cmake/func.cmake:L746-L779](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/cmake/func.cmake#L746-L779) —— `check_compiled_ops`：若 `--ops` 里有算子名没出现在 `COMPILED_OPS`（即仓库里没这个目录/没被注册），直接 `FATAL_ERROR`，错误信息提示检查 `--ops` 参数。这就是 4.2.4 实践中第二条命令预期的报错来源。

#### 4.5.4 代码实践

1. **实践目标**：观察 autogen 目录，理解 aclnn 接口代码是"生成物"而非手写物。
2. **操作步骤**：在具备编译环境时执行 `bash build.sh --pkg --soc=ascend910b --ops=add_example -j8`；成功后进入 `build/autogen/` 目录查看生成的文件。
3. **需要观察的现象**：`build/autogen/` 下应出现 `aclnn_add_example.cpp` / `aclnn_add_example.h` 之类的生成文件；`build/tbe/ascendc/` 下应有 add_example 的 kernel 源码拷贝；`build_out/` 下出现 `cann-ops-cv-custom_linux_<arch>.run`。
4. **预期结果**：把三个产物路径与 4.5.3 中 `gen_aclnn_classify`、`kernel_src_copy`、`CMAKE_INSTALL_PREFIX` 三处源码一一对应，形成"源码机制 → 磁盘产物"的闭环。
5. 依赖 CANN toolkit 与联网下载第三方依赖（或离线 `--cann_3rd_lib_path`），待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：`NEED_COMPILE_OPS` 和 `COMPILED_OPS` 有什么区别？
答：前者是用户通过 `--ops` **想要**编译的算子（来自命令行），后者是 CMake 遍历各算子目录后**实际收集**到的算子。`check_compiled_ops` 求差集，防止拼错的算子名被静默忽略。

**练习 2**：为什么算子信息要为 `ASCEND_ALL_COMPUTE_UNIT` 全部型号生成，二进制却只编译 `--soc` 指定的型号？
答：算子信息 json/ini 描述的是"算子支持哪些芯片、输入输出约束"等元数据，体积小、与运行芯片无关，全量生成便于同一个包描述完整能力；kernel 二进制与芯片微架构强相关、编译耗时，按需只为目标型号编译（`ASCEND_COMPUTE_UNIT`），可大幅缩短编译时间。

**练习 3**：自定义算子包安装后，文件落在哪个目录下？
答：`${ASCEND_HOME_PATH}/opp/vendors/<vendor_name>_cv/`（默认 vendor 为 custom，即 `custom_cv`），路径定义见 [cmake/variables.cmake:L57-L77](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/cmake/variables.cmake#L57-L77)，安装说明见 [docs/zh/install/compile.md:L71-L87](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/install/compile.md#L71-L87)。

## 5. 综合实践

**任务：完成一次单算子编译 → 记录产物 → 多算子编译对比**（对应本讲规格中的实践任务）。

前置：按 u1-l1 的方式准备好 CANN toolkit 编译环境（source `set_env.sh`），仓库须在配套 tag 分支上。

1. **单算子编译（add_example）**：

   ```bash
   bash build.sh --pkg --soc=ascend910b --ops=add_example -j16
   ```

   编译成功的标志是日志末尾出现
   `Self-extractable archive "cann-ops-cv-custom_linux-<arch>.run" successfully created.`（见 [docs/zh/install/compile.md:L63-L69](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/zh/install/compile.md#L63-L69)）。

2. **记录三类产物路径**（对照 4.5 的机制说明）：
   - `build/autogen/` —— opbuild 工具生成的 aclnn 接口代码；
   - `build/tbe/` —— 拷贝过来的 kernel 源码与各型号算子信息；
   - `build_out/cann-ops-cv-custom_linux-<arch>.run` —— 最终 run 包。

3. **安装并验证**：

   ```bash
   ./build_out/cann-ops-cv-custom_linux-<arch>.run          # 默认装到 ${ASCEND_HOME_PATH}/opp/vendors/custom_cv
   # 或安装到用户目录并加载环境
   ./build_out/cann-ops-cv-custom_linux-<arch>.run --install-path=/absolute/path/to/opp
   source /absolute/path/to/opp/vendors/custom_cv/bin/set_env.bash
   ```

4. **多算子编译**：改用 `--ops` 一次编译多个算子：

   ```bash
   bash build.sh --pkg --soc=ascend910b --ops=add_example,grid_sample -j16
   ```

   对比两次编译的 `CMAKE_ARGS` 日志（`-DASCEND_OP_NAME=add_example` vs `add_example;grid_sample`），并确认第二次的 run 包中同时包含两个算子。

5. **记录与思考**：把命令、CMAKE_ARGS 关键片段、三个产物路径、安装路径整理成一页笔记；回答——如果把 `--soc` 换成 `ascend910_93` 重编，哪些产物会变化？（提示：kernel 二进制与 `tbe/op_info_cfg/ai_core/ascend910_93/` 下的信息文件。）

以上步骤依赖真实 CANN 编译环境，本讲义未实际执行，均属**待本地验证**内容。

## 6. 本讲小结

- `build.sh` 是薄入口：`main()` 按开关调度 8 个子脚本（conf/options/cmake/lib/ut/example/genop 等）完成"参数解析 → CMake 配置 → 编库 → 编二进制 → 打包/测试"。
- 参数解析集中在 `build_options.sh` 的 `checkopts`：`--ops` → `COMPILED_OPS`+`ENABLE_CUSTOM`，`--pkg` → `ENABLE_BINARY`+`ENABLE_PACKAGE`，`--soc` → `COMPUTE_UNIT`；大量互斥规则在 `check_param` 中前置拦截。
- `build_cmake.sh` 负责翻译：shell 变量装配成 `-DASCEND_OP_NAME=a;b`、`-DASCEND_COMPUTE_UNIT=xxx` 等 CMake 变量，`--soc` 在此处做白名单校验，每次配置前清掉 `CMakeCache.txt`。
- 根 `CMakeLists.txt` 用一排 `option` 接收开关，`ENABLE_EXPERIMENTAL` 决定编译正式目录还是贡献目录，`examples/` 仅在 `--ops` 包含 add_example 时参与编译。
- `cmake/variables.cmake` 定义编译范围（`NEED_COMPILE_OPS` vs `COMPILED_OPS`）与安装路径（自定义包 → `vendors/<vendor>_cv`，产物 → `build_out`）；`opbuild.cmake` 借助 CANN `op_build` 工具从 `*_def.cpp` 自动生成 aclnn 接口代码；`gen_ops_info.cmake` 拷贝 kernel 源码并为全部芯片生成算子信息、为目标芯片编译二进制。
- `check_compiled_ops`（func.cmake）保证 `--ops` 里拼错的算子名会直接报错而不是被静默忽略。

## 7. 下一步学习建议

下一讲（u1-l4「第一次运行算子：AddExample 全流程实操」）会把本讲的编译产物真正跑起来：安装 run 包后执行 `test_aclnn_add_example` 样例，打通"编译 → 安装 → 运行"闭环。建议预习时先浏览 [docs/QUICKSTART.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/docs/QUICKSTART.md) 和 [examples/add_example/README.md](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/examples/add_example/README.md)。想深入了解打包细节的读者，可以继续阅读 [cmake/package.cmake](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/cmake/package.cmake) 与 [cmake/symbol.cmake](https://github.com/gitcode.com/cann/ops-cv/blob/2bd9cb7c292a1b753781ba301fcde08656554b5f/cmake/symbol.cmake)。
