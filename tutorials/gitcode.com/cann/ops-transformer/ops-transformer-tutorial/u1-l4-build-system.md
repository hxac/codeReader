# 构建体系入门：build.sh 与 CMake

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `build.sh` 的参数解析流程：从命令行选项到 `cmake` 参数的完整转换链路。
2. 区分 `--ophost`、`--opapi`、`--opgraph`、`--onnxplugin`、`--opkernel`、`--pkg` 等构建目标各自产出什么。
3. 理解 `CMakeLists.txt` 中 `ASCEND_COMPUTE_UNIT`、`ASCEND_OP_NAME`、`ASCEND_MODULE_NAME` 三个缓存变量如何驱动「按 SoC、按算子、按模块」的最小化编译。
4. 独立完成一次最小算子编译（以 `add_example` 为例），并能从编译日志中读出关键 cmake 参数。

本讲承接 u1-l2 建立的「五层算子目录范式」和 u1-l3 建立的「编译态/运行态环境」认知：那两讲回答了「编什么、在哪编」，本讲回答「怎么编」。

## 2. 前置知识

- **交叉编译的心智模型**：算子库的产物分两大类——Host 侧动态库（在 CPU 上运行，负责算子原型注册、shape 推导、tiling 计算）和 Device 侧二进制 kernel（在 NPU 上运行）。所以构建系统要同时驱动两套工具链：普通 g++/clang 编 host 代码，毕昇（bisheng）编译器编 device 侧 Ascend C 代码。
- **CMake 缓存变量（cache variable）**：`cmake -DASCEND_OP_NAME=add_example ..` 这样的命令会把值写进 `build/CMakeCache.txt`，CMakeLists 里用 `set(... CACHE STRING ...)` 声明并读取。`build.sh` 本质上就是一个「把好记的命令行选项翻译成一组 `-D` 参数」的包装器。
- **SoC 与 arch 目录**：不同代际昇腾芯片（如 ascend910b、ascend950）的 kernel 源码放在不同的 `archXX` 目录下隔离。构建时必须先告诉 CMake 目标芯片型号，它才知道去收集哪套 arch 目录。
- **snake_case 算子名**：`--ops=` 参数使用算子的目录名（小写下划线），例如 `flash_attention_score`，而不是接口名 `aclnnFlashAttentionScore`。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [build.sh](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh) | 构建入口脚本：参数解析、合法性校验、cmake 参数组装、模式分发 |
| [CMakeLists.txt](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CMakeLists.txt) | 顶层 CMake 配置：缓存变量声明、SoC→arch 映射、模块与算子目录收集 |
| [docs/zh/install/build.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/build.md) | 官方参数说明文档，与 `--help` 输出互为参照 |
| [docs/zh/install/compile.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/compile.md) | 官方源码构建文档：三种包形态、联网/离线编译、产物位置与安装方式 |
| [scripts/util/soc_validator.sh](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/scripts/util/soc_validator.sh) | SoC 名称校验共享库，被 build.sh source 进来作为唯一支持列表 |

## 4. 核心概念与源码讲解

### 4.1 build.sh 构建入口：参数体系与合法性校验

#### 4.1.1 概念说明

`build.sh` 是整个仓库唯一面向用户的构建入口。它存在的意义是：把「编译哪类目标（host 库/接口库/图库/kernel/安装包）+ 编哪些算子 + 面向哪块芯片 + 用什么编译选项」这四个维度的需求，翻译成一条 `cmake .. -Dxxx=yyy ...` 命令，再调用 `cmake --build` 执行。理解了这条翻译链路，你就不用死记几十个参数——只需要记住四个维度。

#### 4.1.2 核心流程

`build.sh` 从命令行到 cmake 的翻译链路分四步：

```text
1. check_option_validity "$@"      # 白名单校验：不认识的 --选项 直接报错退出
2. while [[ $# -gt 0 ]]; do case   # 逐个解析选项，写入 shell 变量（如 OP_HOST=TRUE）
3. assemble_cmake_args             # 把 shell 变量拼接进 CUSTOM_OPTION（一组 -D 参数）
4. main                            # 按模式标志位分发到 build_lib / build_package / UT 等分支
```

关键标志位与构建目标的对应关系：

| 命令行选项 | 置位的变量 | 最终产物 |
|---|---|---|
| `--ophost` | `ENABLE_CREATE_LIB=TRUE`，`BUILD_LIBS+=ophost_transformer` | `libophost_transformer.so`（host 侧算子信息库） |
| `--opapi` | 同上，目标 `opapi_transformer` | `libopapi_transformer.so`（aclnn 接口层） |
| `--opgraph` | 同上，目标 `opgraph_transformer` | `libopgraph_transformer.so`（图模式层） |
| `--onnxplugin` | 同上，目标 `oponnx_plugin_transformer` | ONNX 插件库 |
| `--opkernel` | `ENABLE_OPKERNEL=TRUE` | device 侧二进制 kernel（需配 `--soc`） |
| `--pkg` | `ENABLE_BUILD_PKG=TRUE` | `build_out/` 下的 `.run` 安装包 |
| `--ops=` / `--module=` / `--soc=` | `ascend_op_name` / `ascend_module_name` / `ascend_compute_unit` | 裁剪范围，分别对应「按算子/按模块/按芯片」 |

#### 4.1.3 源码精读

**（1）选项白名单。** build.sh 维护了一张所有合法长选项的列表，任何不在表里的 `--xxx` 会被直接拒绝，避免拼写错误被静默忽略：

[build.sh:1234-1249](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1234-L1249) —— 定义 `SUPPORTED_LONG_OPTS` 数组，其中带 `=` 后缀的（如 `ops=`、`soc=`）表示必须带值的选项。

[build.sh:1251-1280](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1251-L1280) —— `check_option_validity` 函数：遍历每个以 `-` 开头的参数，剥出选项名后在白名单中匹配，匹配不到就打印 `[ERROR] Invalid option` 并退出。

**（2）目标类选项的解析。** 四个「编库」选项和 kernel 选项的写法高度一致，都是「往 `BUILD_LIBS` 数组追加目标名 + 置位模式标志」：

[build.sh:1863-1891](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1863-L1891) —— `--opgraph`/`--onnxplugin`/`--opapi`/`--ophost` 各自追加对应库名并置 `ENABLE_CREATE_LIB=TRUE`；`--opkernel` 则置 `ENABLE_OPKERNEL=TRUE`（走另一条分发分支，因为 kernel 编译需要逐 SoC 循环）。

**（3）裁剪类选项的解析。** `--ops=` 与 `--module=`、`--soc=`：

[build.sh:1639-1649](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1639-L1649) —— `--ops=*` 把值存入 `ascend_op_name` 并置 `ENABLE_BUILT_CUSTOM=TRUE`（自定义算子包模式）；`--module=*` 存入 `ascend_module_name`。

[build.sh:1664-1667](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1664-L1667) —— `--soc=*` 调用 `process_soc_input` 处理（内部会做校验和归一化）。

**（4）SoC 唯一数据源。** 支持的芯片列表不是散落在 if-else 里的，而是集中在脚本顶部并 source 了共享校验库：

[build.sh:53-53](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L53-L53) —— `SUPPORT_COMPUTE_UNIT_SHORT` 数组列出全部支持的 SoC：`ascend910b`（A2）、`ascend910_93`（A3）、`ascend950`（A5）等。

[build.sh:396-402](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L396-L402) —— source `scripts/util/soc_validator.sh`，把 `SUPPORT_COMPUTE_UNIT_SHORT` 复用为校验用的 `SUPPORTED_SOC_LIST`。

[build.sh:1612-1616](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1612-L1616) —— `--list_soc` 选项直接打印这份列表后退出，是查询支持芯片的最快方式。

**（5）互斥参数校验。** [build.sh:1282-1313](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1282-L1313) 的 `check_param` 检查组合约束，例如 `--mssanitizer` 不能与 `--oom`/`--dump_cce` 同用、`--kernel_template_input` 必须搭配单个 `--ops`。

**（6）翻译成 cmake 参数。** [build.sh:1315-1373](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1315-L1373) 的 `assemble_cmake_args` 是「shell 变量 → `-D` 参数」的核心函数，值得逐行读。两个细节：

- `--soc` 的值会先经 `validate_soc_list` 逐项校验、再转小写，最后拼成 `-DASCEND_COMPUTE_UNIT=...`（[build.sh:1328-1341](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1328-L1341)）；
- `--ops` 有隐式依赖补全逻辑：指定 `moe_distribute_dispatch_v2` 会自动追加 `v3`，指定 `distribute_barrier` 会自动追加 `distribute_barrier_extend`（[build.sh:1343-1357](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1343-L1357)）——这是因为部分算子之间存在编译期依赖。

官方文档 [docs/zh/install/build.md:36-78](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/build.md#L36-L78) 用表格总结了全部参数，其中明确标注了互斥关系（如 `--ops` 不可与 `--ophost`、`--opapi`、`--opgraph` 同时使用；`--pkg` 不可与 `-u` 同时使用）。注意：文档说的「不可与」是使用约束，脚本对部分组合并不主动拦截，依赖使用者遵守。

#### 4.1.4 代码实践

1. **实践目标**：熟悉构建入口的两个「零副作用」命令，并验证白名单校验机制。
2. **操作步骤**：
   ```bash
   cd <仓库根目录>
   bash build.sh --help          # 查看全部参数分组说明
   bash build.sh --list_soc      # 查看支持的 SoC 列表
   bash build.sh --ophostt       # 故意拼错一个选项
   ```
3. **需要观察的现象**：前两条命令分别打印帮助文本和 `Supported SoC list: ...`；第三条应打印 `[ERROR] Invalid option: ophostt` 并以非零码退出。
4. **预期结果**：确认 `--list_soc` 输出与 `build.sh:53` 的 `SUPPORT_COMPUTE_UNIT_SHORT` 一致；确认拼错选项不会被静默接受。
5. 本组命令不触碰编译器，可在任意有 bash 的环境执行；`--list_soc` 依赖 `scripts/util/soc_validator.sh` 存在。

#### 4.1.5 小练习与答案

**练习 1**：如果想只编译 `opapi` 层的动态库，应该用什么命令？它会置位哪些变量？
答案：`bash build.sh --opapi`。它会向 `BUILD_LIBS` 追加 `opapi_transformer`、置 `ENABLE_CREATE_LIB=TRUE` 和 `OP_API=TRUE`，最终在 main 分发中进入 `build_lib` 分支。

**练习 2**：为什么 `--ops=abs` 这种拼写错误（正确算子名不存在）不会被 `check_option_validity` 拦住？
答案：白名单只校验「选项名」（`ops=` 合法），不校验「选项值」。算子名是否有效要等到 cmake 阶段收集算子目录时才会暴露。

**练习 3**：`--soc=ASCEND910B`（大写）会发生什么？
答案：`assemble_cmake_args` 中会先调用 `validate_soc_list` 校验，随后用 bash 的 `${var,,}` 归一化为小写再传给 `-DASCEND_COMPUTE_UNIT`（见 build.sh:1328-1341），因此大写输入可以正确归一；但完全不在支持列表里的值会报 `[ERROR] The input soc ... is not supported` 并退出。

### 4.2 build.sh 构建入口：模式分发与产物落盘

#### 4.2.1 概念说明

解析完参数后，脚本进入 `main` 函数的分发逻辑。它像一台多路选择器：根据前面置位的标志位，决定走「编库」「编 UT」「编 kernel」「打自定义包」「打整包」中的哪条路径，以及产物落到哪个目录。理解分发顺序很重要——多个模式标志同时存在时，是 `if/elif` 的先后顺序决定了谁生效。

#### 4.2.2 核心流程

`main` 中的分发优先级（在前者先命中，见 build.sh:2290-2374）：

```text
ENABLE_TEST == TRUE        →  UT 模式（-u / --ophost_test 等）           → build_ut
ENABLE_CREATE_LIB == TRUE  →  编库模式（--ophost/--opapi/--opgraph/--onnxplugin）→ build_lib
ENABLE_STATIC == TRUE      →  静态库模式（--static）                      → 逐 SoC build_static_lib
ENABLE_OPKERNEL == TRUE    →  kernel 模式（--opkernel）                   → build_kernel
ENABLE_BUILT_CUSTOM == TRUE→  自定义包模式（--ops / --vendor_name）       → build_package
ENABLE_BUILD_PKG == TRUE   →  整包模式（--pkg）                           → 逐 SoC 打包 + torch_extension whl
```

产物目录约定（变量定义见 build.sh:19-22）：

| 目录 | 内容 |
|---|---|
| `build/` | cmake 构建树（中间产物、CMakeCache.txt） |
| `output/` | 编库等场景的输出目录 |
| `build_out/` | `.run`/`.deb`/`.rpm` 安装包（官方文档 compile.md:74 明确说明） |

#### 4.2.3 源码精读

**（1）三个产物目录的定义。** [build.sh:19-22](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L19-L22)：`BUILD_DIR`、`OUTPUT_DIR`、`BUILD_OUT_DIR` 分别指向 `build/`、`output/`、`build_out/`。注意 [build.sh:432-445](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L432-L445) 的 `clean` 函数默认每次构建前会删除 `build/` 和 `output/`（`--incremental` 可跳过，见 [build.sh:2070-2075](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L2070-L2075)）。

**（2）编库模式的实现。** [build.sh:857-876](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L857-L876)：`build_lib` 先 `clean`，然后 `cd build && cmake .. ${CUSTOM_OPTION} -DENABLE_BUILT_IN=ON`，再对 `BUILD_LIBS` 中的每个目标执行 `cmake --build . --target ${lib}`。这就是 `--ophost` 的最终归宿：cmake target 名叫 `ophost_transformer`。

**（3）main 分发逻辑。** [build.sh:2290-2374](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L2290-L2374)：完整的多路分支。几个值得注意的点：

- `--opkernel` 分支（2317-2320 行）会先 `set_compute_unit_option` 注入 `-DASCEND_COMPUTE_UNIT`，再 `cmake_config -DENABLE_HOST_TILING=ON` 后 `build_kernel`；
- `--ops` 触发的自定义包分支（2325-2338 行）会追加 `-DENABLE_BUILT_IN=OFF -DENABLE_OPS_HOST=ON -DENABLE_OPS_KERNEL=ON/OFF`；
- `--pkg` 整包分支（2339-2350 行）对 `ASCEND_SOC_UNITS` 中的每块芯片循环调用 `build_pkg_for_single_soc`，并先构建 `torch_extension` 的 whl 包。

**（4）cmake_config 封装。** [build.sh:470-475](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L470-L475)：所有配置阶段最终都落到 `cmake .. ${CUSTOM_OPTION} ${extra_option}`，并打印 `log "Info: cmake config ..."` —— 这行日志就是你观察关键 cmake 参数的窗口。

**（5）环境检查。** [build.sh:416-430](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L416-L430)：`set_env` source CANN 包的 `setenv.bash` 并检查 `bisheng` 编译器是否可用——这就是 u1-l3 强调「先 source set_env.sh 再编译」的代码依据：`ASCEND_HOME_PATH` 未配置时 `set_env` 会失败退出。

**（6）官方产物说明。** [docs/zh/install/compile.md:68-74](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/docs/zh/install/compile.md#L68-L74)：自定义算子包编译成功的标志是打印 `Self-extractable archive "cann-ops-transformer-${vendor_name}_linux-${arch}.run" successfully created.`，run 包位于项目根目录 `build_out` 下。

#### 4.2.4 代码实践

1. **实践目标**：完成一次最小宿主库编译，观察产物与关键 cmake 参数（本讲的主实践）。
2. **操作步骤**：
   ```bash
   # 前置：已按 u1-l3 安装 CANN 包并 source set_env.sh
   cd <仓库根目录>
   bash build.sh --ophost --ops=add_example 2>&1 | tee /tmp/ophost_build.log
   ```
   编译结束后检查目录：
   ```bash
   ls -la build/ output/ 2>/dev/null
   find build output -name "*ophost*" 2>/dev/null
   grep -n "cmake config" /tmp/ophost_build.log        # 关键 cmake 参数
   grep -n "ASCEND_OP_NAME\|ENABLE_BUILT_IN" /tmp/ophost_build.log | head
   ```
3. **需要观察的现象**：日志开头出现 `Info: cmake config -DBUILD_OPEN_PROJECT=ON -DASCEND_OP_NAME=add_example ...` 一行；构建树出现在 `build/`；`libophost_transformer.so` 的具体落盘位置（`build/` 还是 `output/`）请以实际输出为准记录。
4. **预期结果**：编译成功结束打印 `Build libs ophost_transformer success`；日志中能找到 `-DASCEND_OP_NAME=add_example`，证明 `--ops` 裁剪确实传递到了 cmake 层。若环境缺 CANN 包，会在 `set_env` 处报 `bisheng compilation tool not found` 退出。
5. 本实践需要编译态环境（CANN toolkit 包，无需 NPU 卡）。产物 `.so` 的确切路径与是否受 `--ops` 组合影响，**待本地验证**——官方文档建议的常规用法是单独 `bash build.sh --ophost`（全量）或 `bash build.sh --pkg --soc=ascend910b --ops=add_example`（打包含裁剪），可对比三种命令的日志差异。

#### 4.2.5 小练习与答案

**练习 1**：同时传 `--ophost --opkernel`，哪个会生效？
答案：`--ophost`。它置位 `ENABLE_CREATE_LIB=TRUE`，在 main 的 `if/elif` 链中排在 `ENABLE_OPKERNEL` 分支之前（build.sh:2299 先于 2317），因此 `--opkernel` 被短路。要编 kernel 应单独使用 `--opkernel --soc=...`。

**练习 2**：为什么第二次编译常常想加 `--incremental`？
答案：不带它时 `clean` 函数会先 `rm -rf build/ output/`（build.sh:2070-2075 的分支逻辑），全量重建很慢；`--incremental` 改走 `ensure_build_dirs`，仅 `mkdir -p`，保留 cmake 缓存实现增量编译。

**练习 3**：`build_out/` 下的 `.run` 包和 `build/` 下的 `.so` 有什么本质区别？
答案：`.so` 是单个构建目标的中间/交付库；`.run` 是 makeself 自解压安装包，内含 host 库、kernel 二进制、算子信息等完整目录结构，通过 `./xxx.run` 安装到 `${ASCEND_HOME_PATH}/opp/vendors` 挂载生效（见 compile.md:76-88）。

### 4.3 CMake 配置体系：缓存变量与模块裁剪

#### 4.3.1 概念说明

`build.sh` 传进来的 `-D` 参数，接收方是顶层 `CMakeLists.txt`。它用三个核心缓存变量回答三个裁剪问题：

| 变量 | 默认值 | 回答的问题 |
|---|---|---|
| `ASCEND_COMPUTE_UNIT` | `ascend910b` | 面向哪块芯片编 kernel |
| `ASCEND_OP_NAME` | `ALL` | 只编哪些算子 |
| `ASCEND_MODULE_NAME` | `ALL` | 只编哪些算子域模块 |

「ALL/留空 = 全量」是这套裁剪机制的基本约定，靠 `should_add_module` 这类小函数统一判断。

#### 4.3.2 核心流程

```text
CMakeLists.txt 执行顺序（与构建相关的主干）：
1. 声明 option / 缓存变量        ← 接收 build.sh 的 -D 参数
2. 定位 CANN 包路径              ← ASCEND_HOME_PATH 环境变量优先
3. SoC → arch 目录映射循环       ← 把 ascend910b 翻译成 arch22 等
4. add_subdirectory(common)      ← 公共库总是参与
5. should_add_module 逐模块判断  ← 按 --module 裁剪 mc2/attention/moe/...
6. op_add_subdirectory 收集算子  ← 按 --ops 裁剪单个算子目录
7. 依赖算子补编                  ← op_add_depend_directory 把被依赖算子也加进来
```

#### 4.3.3 源码精读

**（1）三个裁剪变量的声明。** [CMakeLists.txt:51-57](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CMakeLists.txt#L51-L57)：`ASCEND_COMPUTE_UNIT`、`ASCEND_OP_NAME`、`ASCEND_MODULE_NAME` 都是 `CACHE STRING`，同时声明了全量列表 `ASCEND_ALL_COMPUTE_UNIT` 和 `ASCEND_ALL_MODULE_NAME`（注意模块全量列表比 build.sh 帮助文本多一个 `mamba`，帮助文本以 `--help` 输出为准核对）。

**（2）SoC 与 arch 目录的对应表。** [CMakeLists.txt:59-61](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CMakeLists.txt#L59-L61)：两个平行列表 `SOC_VERSION_LIST` 与 `ARCH_DIRECTORY_LIST` 按下标对应，注释直接给出了映射关系：

| SoC | arch 目录 | 产品线 |
|---|---|---|
| ascend310p | arch20 | — |
| ascend910b | arch22 | Atlas A2 |
| ascend910_93 | arch22 | Atlas A3 |
| ascend950 | arch35 | Ascend 950（A5） |
| mc62 | arch38 | — |
| kirinx90 / kirin9030 | arch22 | — |

**（3）模块裁剪函数。** [CMakeLists.txt:87-98](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CMakeLists.txt#L87-L98)：`should_add_module` 把 `ASCEND_MODULE_NAME` 按逗号拆成列表，模块名在列表中返回 TRUE；变量为空或为 `all` 时返回 TRUE（全量）。这是「不传 = 全编」语义的实现点。

**（4）模块目录的接入。** [CMakeLists.txt:328-339](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CMakeLists.txt#L328-L339) 与 [CMakeLists.txt:363-388](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CMakeLists.txt#L363-L388)：先无条件 `add_subdirectory(common)`，再对 mc2、posembedding、moe、ffn、attention、mhc、mamba 逐个调 `should_add_module` 决定是否 `add_subdirectory`。moe/ffn 分支里还能看到把 `moe_init_routing_v2` 等复用实现算子手工追加进 `OP_LIST` 的细节（对应 u1-l2 提过的「moe_token_permute 复用 moe_init_routing_v2 实现」）。

**（5）算子目录收集与依赖补编。** [CMakeLists.txt:351-361](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CMakeLists.txt#L351-L361)：`op_add_subdirectory`（定义在 cmake/func.cmake）产出 `OP_LIST`/`OP_DIR_LIST`，随后逐个目录判断——存在 `op_host` 子目录就只加 `op_host`，否则整个目录加入。这是五层范式在构建层的投影：**host 侧与 kernel 侧是分开接入的**。[CMakeLists.txt:394-406](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CMakeLists.txt#L394-L406)：`op_add_depend_directory` 再把 `--ops` 指定算子依赖的其他算子目录补进来，保证 `--ops=A` 时 A 依赖的 B 也能编到。

**（6）CANN 包定位。** [CMakeLists.txt:71-81](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CMakeLists.txt#L71-L81)：优先级为 `CUSTOM_ASCEND_CANN_PACKAGE_PATH` > 环境变量 `ASCEND_HOME_PATH` > `ASCEND_OPP_PATH` 推导 > `/usr/local/Ascend/latest` 兜底，并通过 `message(STATUS ...)` 打印实际使用的路径。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：用「目标列表对比法」亲眼验证 `--module` 裁剪是否生效。
2. **操作步骤**（无 NPU 也可做前两步，需要 cmake 配置阶段成功即可）：
   ```bash
   bash build.sh --ophost 2>&1 | tee /tmp/full.log
   cmake --build build --target help 2>/dev/null | wc -l    # 记录全量目标数
   bash build.sh --ophost --module=moe 2>&1 | tee /tmp/moe.log
   cmake --build build --target help 2>/dev/null | wc -l    # 对比裁剪后目标数
   grep -c "add_subdirectory" /tmp/full.log /tmp/moe.log     # 或对比日志
   ```
3. **需要观察的现象**：`--module=moe` 那次配置，日志中不应出现 attention/ffn 等模块目录的处理；build 树里的目标数量应明显少于全量。
4. **预期结果**：证明 `should_add_module` 只放行了 moe（以及 build.sh:1359-1369 自动补进的 gmm）。另外注意 build.sh 会为 mc2 自动补 gmm、为不含 attention 的组合关闭 tiling sink（`-DENABLE_TILING_SINK=OFF`），日志中可见。
5. 若配置阶段因缺少 CANN 包失败，可退化为纯阅读：对照 [CMakeLists.txt:363-388](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CMakeLists.txt#L363-L388) 逐行写出「传 `--module=ffn` 时哪些 add_subdirectory 会被执行」，**结果待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：不传任何 `--module`/`--ops` 时，会编译多少内容？
答案：全量。`ASCEND_MODULE_NAME` 默认 `ALL`、`ASCEND_OP_NAME` 默认 `ALL`，`should_add_module` 对 `all/ALL/空` 一律返回 TRUE，所有模块与算子目录都会被接入。

**练习 2**：`--soc=ascend950` 时，flash_attention_score 的哪套 kernel 源码会参与编译？
答案：arch35 目录。查表（CMakeLists.txt:59-61）ascend950 → arch35，且该算子的 `op_kernel/` 下确实同时存在 `arch22` 与 `arch35` 两个子目录，构建系统按 SoC 选择收集哪一套。

**练习 3**：为什么 `add_subdirectory(common)` 不走 `should_add_module` 判断？
答案：common 是全部算子域共享的公共库（错误码、shape 工具、通信 fallback 等），任何模块编译都依赖它，因此无条件接入（CMakeLists.txt:328）。

### 4.4 CMake 配置体系：SoC→arch 映射与不支持的芯片

#### 4.4.1 概念说明

多 SoC 支持是本仓库的一大工程特征。`ASCEND_COMPUTE_UNIT` 可能有三种结局：命中 `SOC_VERSION_LIST` 得到 arch 目录、不在列表中触发「空包」逻辑、或者干脆被 build.sh 前置校验拦下。理解这条路径，后面阅读 FA 算子的 arch22/arch35 双实现（u4）时就不会迷惑。

#### 4.4.2 核心流程

```text
--soc=xxx
  └─ build.sh: validate_soc_list          # 第一道关：不在支持列表 → 直接报错退出
      └─ -DASCEND_COMPUTE_UNIT=xxx
          └─ CMakeLists: foreach SOC in ASCEND_COMPUTE_UNIT:
               list(FIND SOC_VERSION_LIST SOC)
               ├─ 找到   → 取同下标的 ARCH_DIRECTORY_LIST 值，追加进 ARCH_DIRECTORY
               └─ 找不到 → 打印 "unsupported chip type"
                            └─ 开源构建(BUILD_OPEN_PROJECT)时 → cpack_empty_package() 打空包并 return
```

#### 4.4.3 源码精读

**（1）映射循环。** [CMakeLists.txt:106-119](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CMakeLists.txt#L106-L119)：对 `ASCEND_COMPUTE_UNIT`（支持分号分隔多值）逐个查 `SOC_VERSION_LIST` 下标，再从 `ARCH_DIRECTORY_LIST` 同下标取 arch 名，累积进 `ARCH_DIRECTORY`。注意 `ASCEND_COMPUTE_UNIT` 变量在此被逐项消费，而 `ARCH_DIRECTORY` 缓存变量（CMakeLists.txt:54）才是后续 kernel 收集真正使用的 arch 集合。

**（2）空包兜底。** [CMakeLists.txt:112-117](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CMakeLists.txt#L112-L117)：未命中且非 RTY kernel、开源构建场景下，include `cmake/build_empty_package.cmake` 并 `cpack_empty_package()` 后 `return()`——保证 CI 对不支持芯片也能产出结构合法的空包而不是直接失败。build.sh:52-53 的注释解释了这一设计：不在 `SOC_VERSION_LIST` 里的 soc 会走空包。

**（3）build.sh 侧的校验与归一化。** [build.sh:1328-1341](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L1328-L1341)（前文已引）与 [build.sh:2034-2041](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/build.sh#L2034-L2041) 的 `set_compute_unit_option`：kernel/打包路径上还会再校验一次 SoC 并强制注入 `-DASCEND_COMPUTE_UNIT`。两道关卡都以 `scripts/util/soc_validator.sh` 为单一数据源。

**（4）kernel 安装时的 arch 目录处理。** [CMakeLists.txt:650-663](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/CMakeLists.txt#L650-L663)：打包阶段对每个算子目录 `install(DIRECTORY ${op_dir}/arch22 ...)`、`arch35`、`arch38` 逐一尝试（带 `OPTIONAL`），把存在的那套 arch 源码装进包内 `ascendc/<op_name>/` 下，供运行态在线编译（jit）使用。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：亲手核实 SoC→arch 映射在真实算子目录中的落点。
2. **操作步骤**：
   ```bash
   bash build.sh --list_soc
   ls attention/flash_attention_score/op_kernel/          # 应看到 arch22、arch35
   grep -n "arch" CMakeLists.txt | head                   # 找到映射表与 install 逻辑
   ```
   然后做一次「纸上编译」：分别写出 `--soc=ascend910b`、`--soc=ascend910_93`、`--soc=ascend950` 时 flash_attention_score 各自会收集哪套 arch 目录（答案：arch22 / arch22 / arch35，前两者同属 arch22 代际）。
3. **需要观察的现象**：`op_kernel` 下同时存在两套 arch 目录；`CMakeLists.txt:59` 的注释与两个列表的下标一一对应。
4. **预期结果**：能独立解释「为什么 A2 和 A3 可以共用 arch22，而 A5 必须单独一套 arch35」——不同代际硬件的指令/内存层级差异大到需要独立实现。
5. `ls` 与 `grep` 无环境要求；如需真实对比可执行 `bash build.sh --opkernel --soc=ascend910b --ops=flash_attention_score` 与 `--soc=ascend950` 各一次（需编译态环境，**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1**：`--soc=ascend610lite` 会发生什么？
答案：它在 build.sh:53 的 `SUPPORT_COMPUTE_UNIT_SHORT` 列表里，能通过脚本校验；但不在 CMakeLists 的 `SOC_VERSION_LIST`（CMakeLists.txt:60）中，进入 cmake 后打印 `unsupported chip type` 并走空包路径。

**练习 2**：为什么映射表用两个平行列表而不是 CMake map？
答案：保持 cmake 3.18 兼容且写法极简，`list(FIND)` 取下标 + `list(GET)` 同下标取值即可完成映射（CMakeLists.txt:106-119）；代价是两表必须严格等长对齐，注释（CMakeLists.txt:59）就是用来人工核对这种对齐关系的。

**练习 3**：`--soc=ascend910b,ascend950`（一次指定两块芯片）允许吗？
答案：允许传给脚本（`validate_soc_list` 支持逗号分隔多项），`-DASCEND_COMPUTE_UNIT` 会被逐项映射出 `arch22;arch35` 两个 arch；但官方文档 build.md:45 建议「每次编译只支持 1 个 NPU 型号」，打包路径（`--pkg`）由 `ASCEND_SOC_UNITS` 循环逐 SoC 出包，实践中通常一次一块。

## 5. 综合实践

**任务：给 build.sh 做一次「构建画像」。** 在编译态环境（u1-l3 部署的 CANN toolkit 包，无需 NPU 卡）完成以下闭环，产出一份个人笔记：

1. `bash build.sh --help > help.txt`、`bash build.sh --list_soc > soc.txt`，记录参数全集和支持芯片。
2. 执行 `bash build.sh --ophost --ops=add_example 2>&1 | tee build1.log`，从日志摘出 `cmake config` 行，列出全部 `-D` 参数并逐个注明来源（哪个命令行选项或哪个默认值产生）。
3. 编译结束后 `find build output build_out -maxdepth 3 -newer help.txt -type f 2>/dev/null | head -30`，记录产物清单与 `.so` 的确切位置。
4. 再执行 `bash build.sh --pkg --soc=<你的soc> --ops=add_example 2>&1 | tee build2.log`（时间允许时），对比 `build_out/` 下 `.run` 包与上一步 `.so` 的差异。
5. 最后 `bash build.sh --make_clean` 清理，验证 `build/`、`output/` 被清空。
6. 笔记末尾回答：如果把 `--ops=add_example` 换成 `--module=attention`，第 2 步日志里哪些 `-D` 参数会变？（提示：`-DASCEND_OP_NAME` 换成 `-DASCEND_MODULE_NAME`，且 tiling sink 开关行为不同，见 build.sh:1354-1369。）

无 NPU 环境时，第 4 步可省略；若第 2 步因依赖下载失败，可改用 `--cann_3rd_lib_path` 离线依赖方案（compile.md:173-236）或仅完成第 1、6 步。

## 6. 本讲小结

- `build.sh` 是「选项翻译器」：白名单校验 → case 解析 → `assemble_cmake_args` 拼 `-D` 参数 → main 按标志位分发，四个维度是「目标类型 / 算子 / 模块 / SoC」。
- 构建目标分三类：编库（`--ophost/--opapi/--opgraph/--onnxplugin` → `build_lib`）、编 kernel（`--opkernel` → 逐 SoC `build_kernel`）、打包（`--pkg`/`--ops` → `build_out/` 下的 `.run`/rpm/deb 包）。
- CMake 侧由 `ASCEND_COMPUTE_UNIT`、`ASCEND_OP_NAME`、`ASCEND_MODULE_NAME` 三个缓存变量驱动裁剪；`should_add_module` 实现「不传 = 全量」语义；依赖算子由 `op_add_depend_directory` 自动补编。
- SoC 与 arch 目录按下标平行列表映射（ascend910b→arch22、ascend950→arch35 等），不支持的芯片在 cmake 层走空包兜底，在脚本层由 `soc_validator.sh` 前置拦截。
- 产物目录约定：`build/` 构建树、`output/` 编库输出、`build_out/` 安装包；默认每次全量 clean，`--incremental` 保留缓存。
- 每次配置的 `Info: cmake config ...` 日志行是观察实际生效参数的最佳入口。

## 7. 下一步学习建议

下一讲（u2-l1「add_example 算子目录全景」）将带着本讲的构建认知，进入 `examples/add_example` 内部，逐个文件看「被谁编译、被谁调用」。建议提前：

1. 通读 [examples/add_example/README.md](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/README.md) 和 [examples/add_example/CMakeLists.txt](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/examples/add_example/CMakeLists.txt)，看单个算子的 CMake 是如何接入本讲顶层 CMakeLists 的 `add_subdirectory` 链的。
2. 浏览 [cmake/func.cmake](https://github.com/gitcode.com/cann/ops-transformer/blob/b2adacfe384b910c6965ca19236e42152674a87c/cmake/func.cmake) 中 `op_add_subdirectory` 的实现，理解算子目录是如何被自动发现的（这也是「文件名必须含 `_tiling` 才被识别」等约定的出处）。
