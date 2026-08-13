# ops_customize 独立编译开发流程

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `ops_customize` 这个独立目录的存在价值——为什么开发自定义算子**不必重编整个 ATB**。
- 画出 `ops_customize` 的目录结构，指出一个自定义算子的「ATB 高层实现」「MKI 层 Kernel 实现」「规格配置」分别放在哪里。
- 读懂 `ops_customize/CMakeLists.txt` 如何用 `IS_STANDALONE_BUILD` 一个开关在「单独编译」与「随 ATB 一同编译」两条路径间切换。
- 读懂 `ops_customize/build.sh` 的执行流程，并复述在已安装 ATB 的环境下独立编译一个自定义算子的完整命令序列。
- 理解 `customize_ops_info.ini` 这份规格文件与主仓的 `atb_ops_info.ini` 是同一套契约。

本讲是「自定义算子开发」单元（u6）的收尾篇，默认你已经学过 **u6-l2（AscendC Kernel 与 Tiling）** 与 **u6-l3（Operation + Runner + 注册的框架集成）**。本讲不再讲怎么写算子本身，而是讲「算子写好之后，用什么工程手段把它编译出来」。

## 2. 前置知识

本讲用到以下概念（均来自前置讲义，这里只做一句话唤醒）：

- **Operation / Runner / Kernel 三层**：一个 ATB 算子横跨「高层 `Operation`（管形状推导与选 Runner）— `OpsRunner`（组 `KernelGraph`）— MKI 层 Kernel（跑在 AI Core 上）」三层，分别在不同目录（u6-l3）。
- **同名两层 Operation**：ATB 高层 `atb::Operation` 与 MKI 层 `AtbOps::Operation` 共用一个名字字符串（`opDesc`），这是「注册名一致」铁律的接线点（u6-l3）。
- **Kernel 四件套**：AscendC kernel 计算 + tiling 切分 + MKI 注册 + CMake 构建（u3-l4、u6-l2）。
- **CXX11 ABI**：`_GLIBCXX_USE_CXX11_ABI` 必须与 PyTorch / ATB 已装版本对齐，否则链接失败；切换 ABI 要清缓存（u1-l3）。
- **Param 的 `rsv` 预留字段**：全 0 是版本兼容闸门，工厂入口逐字节校验（u2-l3）。
- **ini 规格约束**：`atb_ops_info.ini` 用「逗号并列」声明算子合法的 dtype/format 组合，运行时由 `CheckIniMatch` 校验（u6-l4）。

如果你对上面任何一条陌生，建议先回看对应讲义再继续。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `ops_customize/` 目录下：

| 文件 | 作用 |
| --- | --- |
| `ops_customize/README.md` | 外部开发者的入口文档，给出两种编译方式的完整命令 |
| `ops_customize/CMakeLists.txt` | 双模构建入口，用 `IS_STANDALONE_BUILD` 区分单独 / 联合编译 |
| `ops_customize/build.sh` | 独立编译脚本，封装依赖拉取、ABI 探测、cmake 三步 |
| `ops_customize/customize_ops_configs/customize_ops_info.ini` | 自定义算子的输入输出规格约束（与主仓 `atb_ops_info.ini` 同构） |
| `ops_customize/ops/CMakeLists.txt` | 算子层 CMake，对接 MKI、生成 `op_list.yaml`、产出 `customize_ops` 库 |
| `ops_customize/include/customize_op_params.h` | 用户自定义 Param 定义（示例 `BlockCopyParam`） |
| `ops_customize/ops/customize_blockcopy/...` | 示例算子 `customize_blockcopy` 的完整实现（四件套齐全） |

其中前 4 个是本讲的精读重点，后 3 个用于理解目录里到底装了什么。

## 4. 核心概念与源码讲解

### 4.1 ops_customize 目录结构与算子组织

#### 4.1.1 概念说明

`ops_customize` 是 ATB 专门为「外部开发者」划出的开发目录。它的核心定位是 README 第一句话：

> 单独为外部开发者设置开发目录，外部开发者可以按照本目录下的 `customize_block_copy` Operation 实现自定义算子。本目录支持单独编译和测试，也支持与 ATB 加速库一同编译。

这里有三个关键词：

1. **按 `customize_block_copy` 实现**：目录里自带一个完整的示例算子（块拷贝算子），新算子照着它的目录结构复制一份即可，不用从零摸索。
2. **单独编译**：即使你手上的 ATB 是官方 `.run` 包安装好的（没有源码），也能只编译自己写的几个算子，产出 `libatb_customize.so`，运行时和已安装的 `libatb.so` 配合使用。
3. **一同编译**：如果你有 ATB 源码，也可以把 `ops_customize` 作为子目录加进主仓构建，产出带自定义算子的完整 ATB。

「单独编译」是本目录的最大价值——它让自定义算子的迭代周期从「重编整个 ATB（几十分钟）」缩短到「只编几个算子（几分钟）」。

#### 4.1.2 核心流程

一个自定义算子在 `ops_customize` 里的组织方式，是 u6-l3「同名两层 Operation」的落地：

```
ops_customize/
├── README.md                        # 入口文档
├── CMakeLists.txt                   # 双模构建入口（IS_STANDALONE_BUILD 切换）
├── build.sh                         # 独立编译脚本
├── include/
│   └── customize_op_params.h        # 用户 Param（atb::customize::BlockCopyParam，带 rsv）
├── customize_ops_configs/
│   └── customize_ops_info.ini       # 输入输出规格约束（与主仓 atb_ops_info.ini 同构）
└── ops/                             # 算子胶水层 + 各算子目录
    ├── CMakeLists.txt               # 对接 MKI、生成 op_list.yaml、产出 customize_ops 库
    ├── ops.cpp                      # AtbOps::Ops 单例（持有 OpSchedule 调度器）
    ├── param_to_json.cpp            # Param → JSON 序列化（REG_STRINGIFY）
    ├── sym_check.cpp                # 符号完整性自检程序
    └── customize_blockcopy/         # —— 一个示例算子 ——
        ├── kernel_implement/        # MKI 层（Kernel 面）
        │   ├── customize_blockcopy_operation.cpp   # REG_OPERATION 注册
        │   ├── customize_blockcopy_kernel.cpp      # REG_KERNEL_BASE 注册
        │   ├── op_kernel/             # AscendC Device kernel（910b / 310p 各一份）
        │   └── tiling/                # Host 侧 tiling
        ├── operation_implement/     # ATB 高层（用户面）
        │   ├── customize_block_copy_operation.{h,cpp}  # 继承 OperationBase
        │   └── customize_block_copy_ops_runner.cpp      # REG_RUNNER_TYPE / REG_OP_PARAM
        └── tests/                   # gtest 测试
```

把这张图和 u6-l3 对照看：**同一个算子被拆到两个 `implement` 子目录**——`kernel_implement` 对应 MKI 层（选 Kernel、Tiling），`operation_implement` 对应 ATB 高层（形状推导、组 KernelGraph、选 Runner）。两层的接线点是 `opDesc` 里的操作名字符串。

顶层 `ops/` 下的三个 `.cpp` 是所有自定义算子**共享**的胶水：

- `ops.cpp` 维护一个全局 `AtbOps::Ops` 单例，`REG_OPERATION` / `REG_KERNEL_BASE` 宏在程序启动时把算子注册进它的 `OpSchedule`。
- `param_to_json.cpp` 为每种 MKI 层 Param 注册 `ToJson` 函数，供日志/调试打印。
- `sym_check.cpp` 是一个「只调用一次 `Ops::Instance()`」的可执行程序，用来快速发现链接缺失的符号。

#### 4.1.3 源码精读

示例算子的用户 Param 定义在公开头文件里，结构极简，且带 `rsv`——这正是 u2-l3 讲的版本兼容闸门：

[ops_customize/include/customize_op_params.h:39-44](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/include/customize_op_params.h#L39-L44) 定义 `BlockCopyParam`，仅一个全 0 的 `rsv[16]` 预留字段，放 `atb::customize` 命名空间下。

注意命名空间差异：高层 Param 在 `atb::customize::BlockCopyParam`，而 MKI 层 Param 在另一个头文件里、放 `AtbOps::OpParam::CustomizeBlockCopy`（含真正的业务字段 `type`）：

[ops_customize/ops/customize_blockcopy/kernel_implement/include/customizeblockcopy.h:13-27](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/kernel_implement/include/customizeblockcopy.h#L13-L27) 定义 MKI 层 `OpParam::CustomizeBlockCopy`，带 `BLOCK_COPY_CACHE_ND/NZ` 枚举与 `operator==`。

两个 Param 各司其职：高层 `BlockCopyParam` 是对外暴露给用户的（公开头），MKI 层 `CustomizeBlockCopy` 是内部 Kernel 选型用的。这在 u6-l3「同名两层」基础上又印证了一次：**用户面 Param 与 Kernel 面 Param 是分开的两个结构**。

最后看一眼「注册名一致」铁律在本目录的落点——高层 Runner 把图节点 `opDesc` 写成字符串 `"CustomizeBlockCopyOperation"`：

[ops_customize/ops/customize_blockcopy/operation_implement/customize_block_copy_ops_runner.cpp:33-37](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/ops/customize_blockcopy/operation_implement/customize_block_copy_ops_runner.cpp#L33-L37) 设置图节点 `opDesc = {0, "CustomizeBlockCopyOperation", ...}`，这个字符串必须与 MKI 层 `REG_OPERATION` 的注册名、ini 段名一致。

#### 4.1.4 代码实践

**实践目标**：在仓库里把 `customize_blockcopy` 这个示例算子的「同名两层」对应关系走一遍。

**操作步骤**：

1. 打开 `ops_customize/ops/customize_blockcopy/operation_implement/customize_block_copy_operation.h`，确认它继承自 `OperationBase`，并实现 `GetInputNum`（固定 5）/ `GetOutputNum`（固定 0，in-place）/ `InferShapeImpl` / `CreateRunner` 四个钩子。
2. 在同目录的 `customize_block_copy_ops_runner.cpp` 里找到 `opDesc` 字符串。
3. 在 `ops_customize/ops/customize_blockcopy/kernel_implement/customize_blockcopy_operation.cpp` 里找到 `REG_OPERATION(...)` 宏，确认注册名与上一步字符串一致。
4. 在 `customize_ops_configs/customize_ops_info.ini` 里确认段名也是这个字符串。

**需要观察的现象**：三处（`opDesc` 字符串、`REG_OPERATION` 注册名、ini 段名）应当完全相同。若任何一处拼写不一致，运行时调度器 `GetOperationByName` 取不到算子，会报「未注册」类错误。

**预期结果**：三处字符串均为 `CustomizeBlockCopyOperation`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `BlockCopyParam` 放在 `ops_customize/include/`（公开头），而 `CustomizeBlockCopy` 放在算子的 `kernel_implement/include/`（内部头）？

> **参考答案**：`BlockCopyParam` 是用户构造算子时要传入的对外 Param，必须放在被安装的公开 include 目录；`CustomizeBlockCopy` 是 MKI 层 Kernel 选型用的内部结构，只在 `customize_ops` 库内部使用，不需要对外暴露，故放算子私有目录。

**练习 2**：`ops.cpp`、`param_to_json.cpp`、`sym_check.cpp` 这三个文件属于「某个具体算子」还是「所有自定义算子共享」？

> **参考答案**：共享。它们处理的是全局单例 `AtbOps::Ops`、Param 序列化注册与符号自检，与具体算子无关；新加算子只需在自己的目录里写实现并用宏注册，这三个文件一般不需要改（除非要为新 Param 加 `ToJson`）。

### 4.2 CMakeLists.txt：IS_STANDALONE_BUILD 双模切换

#### 4.2.1 概念说明

`ops_customize/CMakeLists.txt` 最核心的设计是用一个 CMake `option` 在两条编译路径间切换：

- **单独编译**（`IS_STANDALONE_BUILD=ON`）：自己当顶层工程（`project("atb_customize")`），从环境变量 `$ATB_HOME_PATH/lib` 链接**已安装的** `libatb.so`，把依赖（MKI、atb 源码头、json）clone 到本地 `3rdparty`。
- **随 ATB 一同编译**（默认 `OFF`，由主仓 `add_subdirectory(ops_customize)` 引入）：不建独立 project，依赖直接复用主仓的 `3rdparty/mki` 与 `src/` 源码，并多链接一个 `acl_op_compiler`（算子在线编译器）。

这个开关由两个不同的 `build.sh` 分别拨动：`ops_customize/build.sh` 总是传 `-DIS_STANDALONE_BUILD=ON`；主仓 `scripts/build.sh customizeops` 不传这个参数（保持默认 `OFF`）。

#### 4.2.2 核心流程

```text
读 ops_customize/CMakeLists.txt
   │
   ├─ option(IS_STANDALONE_BUILD ...)   ← 第 11 行声明，默认 OFF
   ├─ option(BUILD_CUSTOMIZE_OPS_TEST ...) ← 第 12 行，是否编测试
   │
   ├─ file(GLOB_RECURSE CUSTOMIZE_SRCS ops/*/operation_implement/*.cpp)
   │     ↑ 自动扫描所有算子的「ATB 高层」源文件，加 .cpp 即入编
   │
   ├─ if (IS_STANDALONE_BUILD)          ← 第 28 行：单独编译分支
   │     ├─ project / 设编译选项 / 处理 ABI
   │     ├─ include: ATB_SOURCE_DIR 取自本地 3rdparty（克隆的源码）
   │     ├─ link:   $ENV{ATB_HOME_PATH}/lib（已安装的 libatb.so）
   │     └─ DEPS:   atb mki customize_ops ascendcl profapi pthread
   │
   └─ else()                            ← 第 101 行：随 ATB 编译分支
        ├─ include: 主仓 src/ 源码目录、主仓 3rdparty/mki
        └─ DEPS:   mki customize_ops ascendcl profapi pthread acl_op_compiler
   │
   ├─ add_library(atb_customize SHARED ${CUSTOMIZE_SRCS})
   ├─ add_library(atb_customize_static STATIC ...)
   ├─ install(...) → output/ops_customize/cxx_abi_${cxx_abi}/
   └─ if (BUILD_CUSTOMIZE_OPS_TEST) add_subdirectory(tests)
```

两条分支的差异可以浓缩成一句：**单独编译依赖「已安装的 `atb` 库」，联合编译依赖「主仓的 `acl_op_compiler` 在线编译器」**——前者复用现成产物，后者需要主仓的编译基础设施。

#### 4.2.3 源码精读

先看两个 option 声明与自动扫描逻辑：

[ops_customize/CMakeLists.txt:11-12](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/CMakeLists.txt#L11-L12) 声明 `IS_STANDALONE_BUILD`（默认 OFF）与 `BUILD_CUSTOMIZE_OPS_TEST`（默认 OFF）两个开关。

[ops_customize/CMakeLists.txt:23-25](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/CMakeLists.txt#L23-L25) 用 `file(GLOB_RECURSE)` 自动收集所有 `ops/*/operation_implement/*.cpp`。这意味着**往任意算子目录里加 `.cpp` 就会被自动编进 `atb_customize`**，与主仓 u1-l2 讲的 GLOB_RECURSE 自动入编机制一致。

单独编译分支里，ABI 处理与 u1-l3 完全同构：

[ops_customize/CMakeLists.txt:57-63](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/CMakeLists.txt#L57-L63) 根据 `USE_CXX11_ABI` 设置 `_GLIBCXX_USE_CXX11_ABI=0/1` 并记下 `cxx_abi` 变量。

注意单独编译分支的头文件来源——它指向的是**本地 3rdparty 里克隆的源码**，而不是已安装的 ATB：

[ops_customize/CMakeLists.txt:65-68](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/CMakeLists.txt#L65-L68) 设 `OPS_THIRD_PARTY_DIR`、`MKI_SOURCE_DIR`、`ATB_SOURCE_DIR` 都指向 `3rdparty/` 下的克隆目录。

> 说明：这里给出的是逻辑对应位置，链接锚点指向本文件第 65–68 行。

而**链接**才指向已安装的库：

[ops_customize/CMakeLists.txt:86-88](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/CMakeLists.txt#L86-L88) link_directories 同时含 `$ASCEND_HOME_PATH/lib64` 与 `$ATB_HOME_PATH/lib`——后者就是已安装 ATB 的库目录。

单独编译的依赖列表：

[ops_customize/CMakeLists.txt:90-97](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/CMakeLists.txt#L90-L97) `CUSTOMIZE_DEPS` 含 `atb`（已安装库），无 `acl_op_compiler`。

安装目录按 ABI 物理隔离：

[ops_customize/CMakeLists.txt:99](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/CMakeLists.txt#L99) `CMAKE_INSTALL_PREFIX` 设为 `output/ops_customize/cxx_abi_${cxx_abi}`，与 u1-l3 讲的「两套 ABI 产物物理隔离」同构。

联合编译分支的依赖列表差异：

[ops_customize/CMakeLists.txt:127-134](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/CMakeLists.txt#L127-L134) 这里**没有 `atb`**（因为整个工程一起编，`atb` 是同一个 build 里的 target），但**多了 `acl_op_compiler`**（在线编译算子二进制）。

最后的产物与安装：

[ops_customize/CMakeLists.txt:140-149](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/CMakeLists.txt#L140-L149) 产出 `atb_customize`（动态）与 `atb_customize_static`（静态），并安装 `include/` 与 `customize_ops_configs/` 到交付目录。

#### 4.2.4 代码实践

**实践目标**：对比两条编译分支的依赖差异。

**操作步骤**：

1. 打开 `ops_customize/CMakeLists.txt`，定位第 90–97 行（单独编译 `CUSTOMIZE_DEPS`）与第 127–134 行（联合编译 `CUSTOMIZE_DEPS`）。
2. 用纸笔列出两份列表，逐项画圈标出差异。

**需要观察的现象**：单独编译依赖里有 `atb`、无 `acl_op_compiler`；联合编译恰好相反（无 `atb`、有 `acl_op_compiler`）。

**预期结果**：理解差异背后的原因——单独编译把已安装的 `libatb.so` 当外部库链接；联合编译里 `atb` 是同一构建树内的 target（故不在 DEPS 里显式列出），但需要 `acl_op_compiler` 来在线编译 Device kernel 二进制。

#### 4.2.5 小练习与答案

**练习 1**：`IS_STANDALONE_BUILD=ON` 时，`ATB_SOURCE_DIR` 指向的是「已安装 ATB 的头文件」还是「本地克隆的 ATB 源码」？

> **参考答案**：本地克隆的 ATB 源码。`ATB_SOURCE_DIR = 3rdparty/ascend-transformer-boost`，由 `build.sh` 在编译前 `git clone` 而来，仅取其 `include/`、`src/` 头文件参与编译；真正链接的 `libatb.so` 来自 `$ATB_HOME_PATH/lib`（已安装产物）。

**练习 2**：为什么联合编译分支不需要把 `atb` 列进 `CUSTOMIZE_DEPS`？

> **参考答案**：联合编译时 `ops_customize` 是主仓构建树的一个子目录（`add_subdirectory`），主仓的 `atb` 是同一 CMake 工程内的 target，依赖关系由 target 间引用自动建立，不必在 `CUSTOMIZE_DEPS` 列表里重复声明。

### 4.3 build.sh：独立编译脚本流程

#### 4.3.1 概念说明

`ops_customize/build.sh` 是「单独编译」方式的入口脚本。它把三件麻烦事封装成一条 `bash build.sh`：

1. **依赖拉取**：单独编译没有主仓的 `3rdparty`，需要自己 clone MKI（`ascend-boost-comm`）、ATB 源码、nlohmannJson，并给编译器建符号链接。
2. **ABI 自动对齐**：通过 `torch.compiled_with_cxx11_abi()` 探测，与 u1-l3 主仓 `build.sh` 的做法完全一致。
3. **cmake 三步**：configure → build → install。

它支持的参数 README 里写得很清楚：

```text
default | clean | unittest
--use_cxx11_abi=0 | --use_cxx11_abi=1 | --debug | --msdebug
```

其中 `default` 是不传参数时的缺省行为。

#### 4.3.2 核心流程

`fn_main` 是整个脚本的入口，处理流程如下：

```text
fn_main "$@"
  │
  ├─ 1. 解析位置参数（default|clean|unittest|clean 之一）与配置开关（--use_cxx11_abi / --debug / --msdebug）
  │
  ├─ 2. fn_init_env: 探测 ABI
  │     └─ torch.compiled_with_cxx11_abi() → USE_CXX11_ABI=ON/OFF
  │
  ├─ 3. 拼装 COMPILE_OPTIONS:
  │     -DCMAKE_BUILD_TYPE=... -DUSE_CXX11_ABI=... -DIS_STANDALONE_BUILD=ON
  │     （unittest 额外加 -DBUILD_CUSTOMIZE_OPS_TEST=ON）
  │
  └─ 4. case arg1:
        ├─ default  → fn_build
        ├─ clean    → 删 build/ output/ 3rdparty/
        └─ unittest → fn_build_googletest → fn_build → fn_run_unittest
```

其中 `fn_build` 内部会做前置校验，再拉依赖，再 cmake：

```text
fn_build:
  ├─ 校验 ASCEND_HOME_PATH 非空（CANN 已装）
  ├─ 校验 ATB_HOME_PATH  非空（NNAL/ATB 已装）
  ├─ 拒绝 8.2.RC1 版本
  ├─ fn_get_code_branch      # 读 version.info，决定克隆哪个分支
  ├─ fn_load_3rdparty_for_compile   # clone json / mki / atb / 建编译器软链
  ├─ cmake $CODE_ROOT $COMPILE_OPTIONS
  ├─ cmake --build . --parallel
  └─ cmake --install .
```

#### 4.3.3 源码精读

前置校验三连——必须先装好 CANN 与 NNAL/ATB：

[ops_customize/build.sh:37-48](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/build.sh#L37-L48) 检查 `ASCEND_HOME_PATH`、`ATB_HOME_PATH` 非空，并拒绝 `8.2.RC1` 版本（不支持单独编译）。

读 ATB 已装版本的分支，用于克隆同分支的依赖（保证 ABI/接口对齐）：

[ops_customize/build.sh:25-33](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/build.sh#L25-L33) `fn_get_code_branch` 从 `${ATB_HOME_PATH}/../../version.info` 解析 `branch:` 字段。

依赖拉取四件套：

[ops_customize/build.sh:123-130](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/build.sh#L123-L130) `fn_load_3rdparty_for_compile` 依次拉 nlohmannJson、MKI（`ascend-boost-comm`）、ATB（`ascend-transformer-boost`），并建编译器软链。

其中 MKI 与 ATB 都按上面读到的 `CODE_BRANCH` 克隆：

[ops_customize/build.sh:93-100](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/build.sh#L93-L100) `fn_load_mki` 从 `gitcode.com/cann/ascend-boost-comm.git` 克隆 `$CODE_BRANCH` 分支。

ABI 自动探测，逻辑与主仓 `build.sh` 一致：

[ops_customize/build.sh:142-157](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/build.sh#L142-L157) `fn_init_env` 用 `torch.compiled_with_cxx11_abi()` 自动判定 ABI，无 torch 时默认 ON。

最终的 cmake 选项与分支：

[ops_customize/build.sh:210-227](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/build.sh#L210-L227) 拼上 `-DIS_STANDALONE_BUILD=ON`，然后按 `default/unittest` 分派；`unittest` 额外加 `-DBUILD_CUSTOMIZE_OPS_TEST=ON` 并在最后跑 `customize_blockcopy_test`。

#### 4.3.4 代码实践

**实践目标**：用 `ops_customize/build.sh` 在已安装 ATB 的环境下独立编译示例算子。

**操作步骤**（来自 README，本实践为「源码阅读 + 命令复述」型，实际执行需要昇腾环境）：

1. 装好并 source CANN 环境：
   ```shell
   source ${HOME}/Ascend/ascend-toolkit/set_env.sh
   ```
2. 装好并 source NNAL/ATB 环境：
   ```shell
   source ${HOME}/Ascend/nnal/atb/set_env.sh
   ```
3. 进入目录编译：
   ```shell
   cd ascend-transformer-boost/ops_customize
   bash build.sh
   ```
4. （可选）构建并运行单元测试：
   ```shell
   bash build.sh unittest
   ```

**需要观察的现象**：

- 第 3 步会先打印 `USE_CXX11_ABI=ON/OFF`，再 clone 一批依赖到 `ops_customize/3rdparty/`，最后 cmake 编译安装。
- 产物落在 `ops_customize/output/ops_customize/cxx_abi_${abi}/lib/`，含 `libatb_customize.so`。

**预期结果**：编译成功，`output/ops_customize/cxx_abi_0|1/lib/libatb_customize.so` 存在。

> 说明：本实践需要真实的昇腾 NPU 环境与已安装的 CANN / NNAL。若无该环境，请标注「待本地验证」并改为阅读型实践：对照本节源码精读，复述 `fn_build` 从校验到 `cmake --install` 的 7 个步骤。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `fn_get_code_branch` 要读已安装 ATB 的 `version.info`？

> **参考答案**：单独编译克隆 MKI 与 ATB 源码时，必须选与本地已装 ATB **同分支**的版本，否则头文件接口、MKI 注册宏可能与已安装的 `libatb.so` 不匹配，导致编译或链接失败。`version.info` 里的 `branch:` 字段就是本地 ATB 的分支标识。

**练习 2**：`bash build.sh unittest` 相比 `bash build.sh` 多做了哪两件事？

> **参考答案**：① 在 cmake 选项里加 `-DBUILD_CUSTOMIZE_OPS_TEST=ON`（触发测试目标编译）；② 先 `fn_build_googletest` 编译 GoogleTest，编译完成后 `fn_run_unittest` 把 `output/bin/customize_blockcopy_test` 跑起来。

### 4.4 customize_ops_info.ini 规格与共享契约

#### 4.4.1 概念说明

`customize_ops_info.ini` 是 `ops_customize` 自己的算子规格约束文件，作用与主仓 `ops_configs/atb_ops_info.ini` 完全相同（见 u6-l4）：声明每个算子的输入输出张量名、合法的 dtype / format 组合。运行时框架的 `CheckIniMatch` 会按这份 ini 校验用户传入的张量描述，不匹配就返回 `ERROR_INVALID_TENSOR_INI_MATCH`。

把它单列出来，是想强调一个关键结论：**主仓与 `ops_customize` 共用同一套契约模板**——Param 的 `rsv`、ini 规格段名、注册名一致、Kernel 四件套，这四条规则在两个目录里一模一样地生效。这也是「独立编译」之所以能成立的基础：自定义算子遵循和内置算子相同的约定，自然能被同一套调度框架识别和执行。

#### 4.4.2 核心流程

ini 的语法很简单，每个算子一个段，段名就是 IR key（必须等于注册名）：

```ini
[CustomizeBlockCopyOperation]      ← 段名 = 注册名 = opDesc 字符串
input0.name = kcache
input0.dtype = float16,bf16,int8   ← 逗号并列 = 支持的组合
input0.format = nd,nd,nd
...
output0.name = kcacheOut
```

`dtype` 与 `format` 用逗号并列，**按位置一一对应**（第 1 个 dtype 配第 1 个 format），表示「这套组合里任取一组都合法」。`customize_blockcopy` 支持 float16 / bf16 / int8 三种 cache 类型，故三种各列一行。

#### 4.4.3 源码精读

[ops_customize/customize_ops_configs/customize_ops_info.ini:1-22](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/ops_customize/customize_ops_configs/customize_ops_info.ini#L1-L22) 整份文件只有一个段 `CustomizeBlockCopyOperation`，声明 5 个输入（kcache/vcache/srcIndices/dstIndices/cumSum）与 2 个输出（kcacheOut/vcacheOut）。

注意几个细节与 u4-5（KV Cache）讲的知识吻合：

- 5 输入正好对应 `customize_block_copy_operation.h` 里 `GetInputNum` 返回的固定 5（KCache、VCache、srcIdx、dstIdx、cumSum）。
- 2 输出在 ini 里出现，但 `GetOutputNum` 返回 0（in-place 算子）——ini 里的 `output0/output1` 描述的是「逻辑上改写后的 kcache/vcache」，与 in-place 语义不冲突，因为它们与输入同名同地址。
- `cumSum` 的 dtype 是 int32，对应 u4-8 讲过的「Gating 算子输出的 cumSum 即下游 expertCount」——这里它是块拷贝的累积计数输入。

这份 ini 会被安装到交付目录（见 4.2.3 的 `install(DIRECTORY .../customize_ops_configs ...)`），随 `libatb_customize.so` 一起交付。

#### 4.4.4 代码实践

**实践目标**：为示例算子核对 ini 与 Operation 的输入输出一致性。

**操作步骤**：

1. 打开 `customize_ops_info.ini`，数一下 `input*` 和 `output*` 各有几个。
2. 打开 `customize_block_copy_operation.h`，对比 `GetInputNum` / `GetOutputNum` 的注释。

**需要观察的现象**：ini 里 input0–input4 共 5 个，与 `GetInputNum` 注释「固定为 5」一致。

**预期结果**：ini 张量个数与 Operation 的 `GetInputNum`/`GetOutputNum` 自洽。

#### 4.4.5 小练习与答案

**练习 1**：如果新算子支持 fp32 与 fp16 两种输入，ini 里这一行该怎么写？

> **参考答案**：`input0.dtype = float32,float16`，对应的 `input0.format` 也写两份并列，如 `input0.format = nd,nd`。逗号分隔的项数在 dtype 与 format 之间必须一致。

**练习 2**：ini 段名写错了（比如拼成 `CustomizeBlockCopyOp`），会发生什么？

> **参考答案**：框架按 IR key 查 ini 找不到对应段，校验阶段会判为规格不匹配，返回 `ERROR_INVALID_TENSOR_INI_MATCH`；即使侥幸绕过 ini 校验，调度器 `GetOperationByName("CustomizeBlockCopyOperation")` 也取不到算子。段名必须与注册名、`opDesc` 字符串严格一致。

## 5. 综合实践

**任务**：假设你要在已安装 ATB 的机器上，新增并独立编译一个自定义算子 `customize_addcustom`。请写出完整的目录与命令规划（无需真实运行，按本讲结构作答）。

**要求覆盖以下要点**：

1. **目录骨架**：参照 `customize_blockcopy`，列出 `customize_addcustom` 应建立的子目录（`kernel_implement/`、`operation_implement/`、`tests/`）与各自应放什么文件。
2. **共享胶水**：说明哪些文件需要改（如 `ops/param_to_json.cpp` 要为新 Param 加 `ToJson`），哪些不用改（如 `ops/ops.cpp`）。
3. **配置交付件**：在 `customize_ops_info.ini` 里追加一段 `[CustomizeAddCustomOperation]`，声明 2 输入 1 输出与合法 dtype。
4. **编译命令**：写出 source 两个 `set_env.sh` 后，用 `bash build.sh` 独立编译、用 `bash build.sh unittest` 跑测试的完整命令序列。
5. **ABI 注意事项**：说明为何切换 `--use_cxx11_abi=0/1` 后建议先 `bash build.sh clean`。

**参考思路**（不唯一）：

- 目录照抄 `customize_blockcopy` 的两段式（`kernel_implement` 放 `REG_OPERATION`/`REG_KERNEL_BASE`/tiling/`op_kernel`，`operation_implement` 放继承 `OperationBase` 的 Operation 与 `REG_RUNNER_TYPE`/`REG_OP_PARAM` 的 OpsRunner）。
- `opDesc` 字符串、`REG_OPERATION` 名、ini 段名三者统一为 `CustomizeAddCustomOperation`。
- `build.sh` 自动 GLOB_RECURSE 收集新 `.cpp`，无需改构建脚本；只需新增 `param_to_json` 的 `REG_STRINGIFY`。
- 命令：`source $HOME/Ascend/ascend-toolkit/set_env.sh` → `source $HOME/Ascend/nnal/atb/set_env.sh` → `cd ops_customize && bash build.sh`。
- ABI 切换需清缓存的原因与 u1-l3 一致：CMake 缓存、克隆的依赖、编译产物都按 ABI 区分，残留会导致链接错配。

> 说明：本综合实践为「源码阅读 + 工程规划」型，实际执行需昇腾环境，可标注「待本地验证」。

## 6. 本讲小结

- `ops_customize` 是 ATB 为外部开发者划出的独立目录，最大价值是**不重编 ATB 即可开发编译自定义算子**，迭代周期从几十分钟降到几分钟。
- 一个自定义算子沿用「同名两层」组织：`operation_implement/`（ATB 高层，继承 `OperationBase`）与 `kernel_implement/`（MKI 层，`REG_OPERATION`/`REG_KERNEL_BASE` + AscendC 四件套），接线点是 `opDesc` 字符串。
- `ops_customize/CMakeLists.txt` 用 `IS_STANDALONE_BUILD` 一个开关在「单独编译」（链接已装 `libatb.so`）与「随 ATB 一同编译」（多链 `acl_op_compiler`）两条路径间切换，并按 `cxx_abi` 物理隔离产物。
- `ops_customize/build.sh` 封装三件事：clone 依赖（MKI/atb/json，按已装 ATB 的分支）、用 `torch.compiled_with_cxx11_abi()` 自动对齐 ABI、cmake configure/build/install；支持 `default|clean|unittest` 与 ABI/debug 开关。
- `customize_ops_info.ini` 与主仓 `atb_ops_info.ini` 同构，段名 = 注册名，用逗号并列声明合法 dtype/format 组合，运行时由 `CheckIniMatch` 校验。
- 主仓与 `ops_customize` 共用同一套契约（rsv、ini、注册名一致、四件套），这是「独立编译能被同一调度框架识别」的根本原因。

## 7. 下一步学习建议

本讲是自定义算子开发单元（u6）的收尾。接下来建议：

- **若想验证所学**：进入单元 7 的 **u7-l3（测试框架与算子测试）**，学习如何用 JSON 驱动的方式为自定义算子编写功能与精度测试，与本讲的 `customize_blockcopy_test` 互补。
- **若想深入工程化**：阅读 **u7-l4（编译选项、ABI 与 Sanitizers）**，把本讲的 ABI 切换、`--debug`/`--msdebug` 放到更系统的编译选项图谱里理解。
- **若想回顾全链路**：回看 **u6-l3（框架集成）** 与本讲对照，体会「算子怎么写」与「算子怎么编译交付」是同一件事的两个面。
- **建议继续阅读的源码**：`ops_customize/ops/CMakeLists.txt`（MKI 的 `op_list.yaml` 生成与 `add_operation`/`add_kernel` 机制）、主仓 `scripts/build.sh` 的 `customizeops` 分支（956–962 行），理解联合编译的完整入口。
