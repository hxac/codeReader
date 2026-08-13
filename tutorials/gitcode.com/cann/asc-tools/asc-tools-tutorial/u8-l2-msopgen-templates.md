# msopgen 与算子工程模板

## 1. 本讲目标

本讲承接 u8-l1（optype_collector 把算子"采集出来做体检"）。如果说 optype_collector 回答的是"已经装上去的算子有没有冲突"，那么本讲回答的是更上游的问题：**一个全新的自定义算子，它的工程骨架从哪里来？**

学完本讲，你应当能够：

- 说清 `msopgen` 工具的输入（算子原型 json）、输出（完整工程骨架）以及"生成 + 拷贝源码"的两步工作流。
- 画出 `utils/templates` 下两套模板树（`op_project_templates/ascendc/{customize,aclnn,common}` 与 `new_op_project_template/custom_op`）的目录结构与职责差异。
- 解释一个自定义算子工程为何要拆成 `op_host` / `op_kernel` / `framework` 三层，以及这三层各自编译出什么产物。
- 读懂模板里的 `build.sh` 与 `CMakeLists.txt`，知道关键开关（`vendor_name`、`ASCEND_COMPUTE_UNIT`、`ASCEND_PACK_SHARED_LIBRARY`）控制了什么。

---

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自 u1、u2、u8-l1）：

- **算子（Op）与核函数（Kernel）**：算子是逻辑概念（如 Add），核函数是跑在 NPU AI Core 上的实际计算单元（如 `add_custom`）。
- **Host 侧与 Kernel 侧**：一次算子调用分两段——Host 侧（CPU 上跑）负责准备数据切分策略（Tiling）、注册算子原型、提供 C 调用接口；Kernel 侧（NPU 上跑）负责真正的并行计算。
- **OPP 包**：算子的标准交付形态，按 `vendors/<vendor_name>/` 分目录存放（见 u8-l1）。
- **run 包**：自解压安装脚本（`.run`），见 u1-l4。本讲会看到自定义算子工程同样产出一个 `.run`。

本讲会引入几个新术语，后续会逐一解释：**msopgen**（外部代码生成器）、**算子原型定义文件**（描述算子输入输出的 json）、**Tiling**（把输入张量切成适合硬件的块）、**vendor_name**（算子厂商名，决定安装目录）、**npu_op_\* 宏**（CMake 高层封装宏）。

---

## 3. 本讲源码地图

本讲涉及的文件分三类：**样例**、**模板**、**模板内构建脚本**。

| 文件 | 作用 |
|------|------|
| [examples/03_msopgen/README.md](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/03_msopgen/README.md) | msopgen 使用样例的完整说明，给出"生成→拷贝→编译→安装→验证"全流程命令 |
| [examples/03_msopgen/op_dev/add_custom.json](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/03_msopgen/op_dev/add_custom.json) | 算子原型定义文件，msopgen 的唯一输入 |
| [examples/03_msopgen/op_dev/op_host/add_custom.cpp](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/03_msopgen/op_dev/op_host/add_custom.cpp) | 用户手写的 Host 侧实现（Tiling + 算子原型注册） |
| [examples/03_msopgen/op_dev/op_kernel/add_custom.cpp](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/03_msopgen/op_dev/op_kernel/add_custom.cpp) | 用户手写的 Kernel 侧实现（Ascend C 核函数） |
| [utils/templates/CMakeLists.txt](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/CMakeLists.txt) | 模板的总安装入口，把两套模板树装进 CANN 的 `tools/` |
| [utils/templates/new_op_project_template/custom_op/CMakeLists.txt](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/new_op_project_template/custom_op/CMakeLists.txt) | 新式工程骨架根 CMake（msopgen 实际脚手架） |
| [utils/templates/new_op_project_template/custom_op/build.sh](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/new_op_project_template/custom_op/build.sh) | 新式工程的一键构建脚本 |
| [utils/templates/op_project_templates/ascendc/customize/CMakeLists.txt](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/op_project_templates/ascendc/customize/CMakeLists.txt) | 经典 customize 工程根 CMake（含 CPack 打 run 包） |
| [utils/templates/op_project_templates/ascendc/customize/cmake/config.cmake](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/op_project_templates/ascendc/customize/cmake/config.cmake) | customize 工程的默认变量配置 |

> 重要前提：**`msopgen` 本身不在 asc-tools 仓库内**，它是一个独立的昇腾代码生成工具（仓库地址见样例 README 的链接 `gitcode.com/Ascend/msopgen`）。asc-tools 对"算子工程"这件事的贡献，是**提供工程模板**（`utils/templates`），供 msopgen 在生成时作为脚手架复制/填充。所以本讲的源码精读重点是"模板长什么样、构建脚本如何工作"，而不是 msopgen 的生成算法。

---

## 4. 核心概念与源码讲解

### 4.1 msopgen 生成流程

#### 4.1.1 概念说明

`msopgen` 是昇腾 AI 自定义算子开发工具，它的核心能力是：**给定一份算子原型定义文件（json），生成一个可编译的自定义算子工程框架**，包括 CMake 构建系统、Host 侧代码骨架、Kernel 侧代码骨架和框架适配层。

样例 README 对它的一句话定位：

> `msopgen`是昇腾AI自定义算子开发工具，可根据算子原型定义文件生成自定义算子工程框架，包括CMake构建系统、Host侧代码、Kernel侧代码和框架适配层。（[examples/03_msopgen/README.md:7](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/03_msopgen/README.md#L7)）

这里需要建立一个关键认知：**msopgen 生成的是"工程骨架"（build.sh、CMakeLists.txt、CMakePresets.json、framework 适配层），而不是算法本身**。算子的真正算法逻辑（Tiling 策略、核函数实现）由开发者手写在 `op_dev/` 下，生成之后再拷进骨架。这从样例目录结构就能看出来。

#### 4.1.2 核心流程

msopgen 样例的完整工作流是"先生成骨架，再拷贝源码，最后编译安装验证"五步：

```
┌─────────────────────────────────────────────────────────────────┐
│  op_dev/add_custom.json   ──(msopgen gen 输入)──►  生成骨架       │
│  op_dev/op_host/*.cpp     ──(手写算法，待拷贝)                     │
│  op_dev/op_kernel/*.cpp   ──(手写算法，待拷贝)                     │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼  msopgen gen -out ./custom_op
┌─────────────────────────────────────────────────────────────────┐
│  custom_op/   ← 生成的工程骨架（build.sh + CMakeLists + ...）     │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼  cp -rf op_dev/op_host op_dev/op_kernel → custom_op
┌─────────────────────────────────────────────────────────────────┐
│  cd custom_op && bash build.sh  →  custom_opp_<os>_<arch>.run    │
│  ./custom_opp_*.run             →  装入 vendors/<vendor_name>/   │
│  aclnn_invocation 验证          →  test pass                      │
└─────────────────────────────────────────────────────────────────┘
```

样例 README 把它拆成了明确的编号步骤，核心命令是：

```bash
msopgen gen -i ./op_dev/add_custom.json -f <framework> -c ai_core-<soc_version> -lan cpp -out ./custom_op
```

参数含义见 [examples/03_msopgen/README.md:78-84](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/03_msopgen/README.md#L78-L84)：

- `-i` ：算子原型定义文件（输入）。
- `-f` ：框架类型，默认 `tf`/`tensorflow`（决定 framework 适配层生成哪一种插件）。
- `-c` ：计算核心与芯片型号，如 `ai_core-Ascend910B3`（决定编译目标）。
- `-lan` ：生成语言，样例用 `cpp`。
- `-out` ：输出目录。

#### 4.1.3 源码精读：输入 json 的结构

msopgen 的唯一输入是算子原型定义文件。以 AddCustom 为例，整个文件是一个数组，里面只有一个算子描述：

```json
[
    {
        "op": "AddCustom",
        "language": "cpp",
        "input_desc": [ { "name": "x", ... "type": ["fp16"] }, { "name": "y", ... } ],
        "output_desc": [ { "name": "z", ... "type": ["fp16"] } ],
        "attr": []
    }
]
```

（完整内容见 [examples/03_msopgen/op_dev/add_custom.json:1-41](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/03_msopgen/op_dev/add_custom.json#L1-L41)）

各字段的含义：

| 字段 | 含义 | 样例取值 |
|------|------|----------|
| `op` | 算子类型名（全局唯一标识） | `AddCustom` |
| `language` | 生成语言 | `cpp` |
| `input_desc` | 输入张量描述列表 | `x`、`y` 两个 fp16/ND 输入 |
| `output_desc` | 输出张量描述列表 | `z` 一个 fp16/ND 输出 |
| `attr` | 算子属性（编译期常量） | 空（AddCustom 无属性） |

每个 desc 项里，`name` 是张量名、`param_type` 是 `required`/`optional`、`format` 是格式（`ND` 即任意格式）、`type` 是数据类型（`fp16`）。msopgen 正是依据这份描述，去骨架里填充算子名、输入输出个数、数据类型等编译期信息，并据此生成对应的 aclnn C 接口声明与 OpDef 注册骨架。

注意 json 里只描述了"算子有几个输入输出、什么类型"，**完全没有描述怎么算**。这印证了 4.1.1 的结论：算法实现是开发者手写的，json 只负责"接口形状"。

#### 4.1.4 代码实践：手动走一遍生成流程（源码阅读型 + 命令演练）

> **实践目标**：对照 README 把 msopgen 的生成流程在脑中走一遍，理解"骨架从哪来、源码从哪来"。

**操作步骤**：

1. 打开 [examples/03_msopgen/README.md](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/03_msopgen/README.md)，对比"生成前目录结构"（[L19-29](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/03_msopgen/README.md#L19-L29)）与"生成后目录结构"（[L31-51](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/03_msopgen/README.md#L31-L51)），圈出新生成的 `custom_op/` 里多了哪些文件。
2. 在本地（已装好 CANN 且具备 `msopgen` 命令的环境），进入 `examples/03_msopgen/` 执行：
   ```bash
   msopgen gen -i ./op_dev/add_custom.json -f tf -c ai_core-<你的soc_version> -lan cpp -out ./custom_op
   ```
   （`<你的soc_version>` 用 `npu-smi info` 查到的 Name 前加 `Ascend` 替换。）
3. 执行 README 第 2 步的拷贝：
   ```bash
   cp -rf ./op_dev/op_kernel custom_op
   cp -rf ./op_dev/op_host custom_op
   ```
   （见 [examples/03_msopgen/README.md:86-90](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/03_msopgen/README.md#L86-L90)）

**需要观察的现象**：

- 第 2 步执行前，`custom_op/op_host/` 与 `custom_op/op_kernel/` 里**只有 CMakeLists.txt，没有任何 `.cpp`**——这证明 msopgen 只生成构建骨架。
- 第 3 步执行后，`custom_op/op_host/add_custom.cpp` 与 `custom_op/op_kernel/add_custom.cpp` 才出现，它们来自 `op_dev/`。

**预期结果**：生成后的 `custom_op/` 目录树应与 README [L31-51](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/03_msopgen/README.md#L31-L51) 描述一致，包含 `build.sh`、`CMakeLists.txt`、`CMakePresets.json`、`framework/tf_plugin/`、`op_host/`、`op_kernel/`。

> 说明：实际执行 msopgen 需要独立的 msopgen 工具与 CANN 环境。若本地不具备，本实践退化为"源码阅读型"——直接对照 README 的两份目录树得出结论即可。运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `add_custom.json` 里 `x` 的 `type` 从 `fp16` 改成 `fp32`，msopgen 生成的骨架里哪一处会随之变化？

**参考答案**：算子原型注册（OpDef）里的 `DataType({ge::DT_FLOAT16})` 会变成 `DT_FLOAT`，以及生成的 aclnn 接口里输入指针的类型推导也会跟着变。注意：这种"接口形状"由 json 决定，但 `op_dev/op_host/add_custom.cpp` 是开发者手写的，改 json 后需要同步手改这份 `.cpp`（或重新让 msopgen 生成骨架再拷贝）。

**练习 2**：为什么样例要强调"若多次运行 msopgen，请先删除已生成的 custom_op 目录"（README [L84](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/03_msopgen/README.md#L84)）？

**参考答案**：msopgen 生成的是脚手架，重复生成不会自动清理旧产物，可能残留过期文件（如改了算子名后旧的 framework 插件文件），导致编译行为不确定。删目录是最稳妥的"全量重生"。

---

### 4.2 工程模板目录结构

#### 4.2.1 概念说明

msopgen 之所以能生成结构稳定的工程骨架，是因为它背后有一套**模板**。asc-tools 在 `utils/templates/` 下维护了这些模板，并在编译安装 asc-tools 时把它们一并装进 CANN 的 `tools/` 目录，供 msopgen 使用。

这里有一个初学者最容易混淆的点：`utils/templates` 下其实有**两套**模板树，分属两个"时代"：

1. **`op_project_templates/ascendc/`**：经典模板树，功能最全，内含三个子目录：
   - `customize/`：完整的自定义算子工程（含 op_host/op_kernel/framework + cmake 脚本 + install/upgrade 脚本），使用**底层 CMake 原语**（`opbuild`、`add_library`、CPack）。
   - `aclnn/`：仅 Host 侧的 ACLNN 接口层工程。
   - `common/util/`：两类工程共用的 Python 构建辅助脚本（如 `ascendc_compile_kernel.py`、`opdesc_parser.py`）。
2. **`new_op_project_template/custom_op/`**：新式简化骨架，使用**高层 `npu_op_*` 封装宏**，隐藏了大量底层细节。样例 README 描述的"生成后目录结构"正是这一套。

它们的安装都由同一个根 CMake 控制——[utils/templates/CMakeLists.txt](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/CMakeLists.txt)。

#### 4.2.2 核心流程：模板如何被安装到 CANN

模板的安装是条件触发的，靠 `BUILD_OPEN_PROJECT` 开关：

```cmake
if(BUILD_OPEN_PROJECT)
    set(MAKESELF_PATH .../customize/cmake/util/makeself)
    add_custom_target(ascendc_project_templates ALL
        COMMAND ... copy makeself 工具到 MAKESELF_PATH ...)
    install(DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/op_project_templates  DESTINATION tools/ ...)
    install(DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/new_op_project_template DESTINATION tools/ ...)
endif()
```

（见 [utils/templates/CMakeLists.txt:13-36](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/CMakeLists.txt#L13-L36)）

两件事值得注意：

- **makeself 的搬运**（[L16-24](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/CMakeLists.txt#L16-L24)）：`makeself` 是把目录打包成自解压 `.run` 脚本的开源工具，customize 工程打 run 包时要用到它。这里从 CANN 的第三方库路径（`CANN_3RD_LIB_PATH`）把它复制到模板里，让生成的工程自带打包能力。
- **两套模板并列安装**（[L25-36](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/CMakeLists.txt#L25-L36)）：`op_project_templates` 与 `new_op_project_template` 都装到 `tools/` 下，二者并存、互不替代。

#### 4.2.3 源码精读：两套模板的目录对比

**新式骨架 `new_op_project_template/custom_op/`** 的文件清单：

```
custom_op/
├── build.sh                 # 一键构建（见 4.2.4）
├── CMakeLists.txt           # 根 CMake，用 npu_op_package 声明打包
├── CMakePresets.json        # CMake 预设（soc、CANN 路径等）
├── framework/
│   ├── CMakeLists.txt
│   ├── tf_plugin/CMakeLists.txt
│   └── onnx_plugin/CMakeLists.txt
├── op_host/CMakeLists.txt
└── op_kernel/CMakeLists.txt
```

注意：每个子目录**只有 CMakeLists.txt，没有 `.cpp`**——再次印证"骨架不含算法"。它的根 CMake 非常简洁：

```cmake
project(opp)
find_package(ASC REQUIRED)                       # 定位 CANN 工具链（见 u1-l4）
set(package_name ${vendor_name})
npu_op_package(${package_name} TYPE RUN CONFIG INSTALL_PATH ${CMAKE_BINARY_DIR}/)
if(EXISTS ${CMAKE_CURRENT_SOURCE_DIR}/framework)  add_subdirectory(framework)  endif()
if(EXISTS ${CMAKE_CURRENT_SOURCE_DIR}/op_host)    add_subdirectory(op_host)    endif()
if(EXISTS ${CMAKE_CURRENT_SOURCE_DIR}/op_kernel)  add_subdirectory(op_kernel)  endif()
```

（见 [utils/templates/new_op_project_template/custom_op/CMakeLists.txt:12-31](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/new_op_project_template/custom_op/CMakeLists.txt#L12-L31)）

`npu_op_package` 是 CANN 提供的高层封装宏，它替你处理了"声明一个 RUN 类型算子包、安装到构建目录"的全部细节。三个 `add_subdirectory` 都用 `if(EXISTS ...)` 守卫——这意味着 framework 是可选的（不是所有算子都需要框架适配），而 op_host/op_kernel 是核心。

**经典 customize 工程** 的文件清单要庞大得多（见 4.2.1 列举），其根 CMake 直接操作底层原语，并自己组装 CPack 打包：

```cmake
include(cmake/config.cmake)          # 默认变量
include(cmake/func.cmake)
include(cmake/intf.cmake)
# ... 交叉编译处理 ...
if(EXISTS .../framework) add_subdirectory(framework) endif()
if(EXISTS .../op_host)   add_subdirectory(op_host)   endif()
if(EXISTS .../op_kernel) add_subdirectory(op_kernel) endif()
# 把 scripts/install.sh 里的 vendor_name=customize 替换为实际厂商名
add_custom_target(modify_vendor ALL DEPENDS ...)
# 用 CPack + makeself 打成 custom_opp_${SYSTEM_INFO}.run
set(CPACK_GENERATOR External)
set(CPACK_EXTERNAL_PACKAGE_SCRIPT ${CMAKE_SOURCE_DIR}/cmake/makeself.cmake)
```

（见 [utils/templates/op_project_templates/ascendc/customize/CMakeLists.txt:4-76](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/op_project_templates/ascendc/customize/CMakeLists.txt#L4-L76)）

两套模板的核心差异可归纳为下表：

| 维度 | 新式 `new_op_project_template` | 经典 `customize` |
|------|--------------------------------|------------------|
| 构建抽象层级 | 高（`npu_op_*` 宏） | 低（`opbuild`/`add_library`/CPack 原语） |
| 打 run 包 | 由 `npu_op_package` 封装 | 自写 CPack + makeself（[L56-76](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/op_project_templates/ascendc/customize/CMakeLists.txt#L56-L76)） |
| 内置脚本 | 仅 build.sh | build.sh + scripts/install.sh + scripts/upgrade.sh |
| 适用场景 | msopgen 新流程的标准骨架 | 需要细粒度控制打包/安装的老流程 |

#### 4.2.4 源码精读：build.sh 做了什么

新式骨架的 [build.sh](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/new_op_project_template/custom_op/build.sh) 是个薄封装，关键四步：

1. **定位 CANN 路径**（[L12-26](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/new_op_project_template/custom_op/build.sh#L12-L26)）：按 `BASE_LIBS_PATH` → `ASCEND_HOME_PATH` → `ASCEND_AICPU_PATH` 的优先级确定 `ASCEND_HOME_PATH`，找不到就报错退出。这与 u1-l3 讲过的"`source set_env.sh` 导出 ASCEND_HOME_PATH"是一致的依赖关系。
2. **解析预设**（[L32](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/new_op_project_template/custom_op/build.sh#L32)）：调用 CANN 自带的 `preset_parse.py` 读 `CMakePresets.json`，把预设转成命令行 `opts`（兼容老版本 cmake）。
3. **配置**（[L35-39](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/new_op_project_template/custom_op/build.sh#L35-L39)）：cmake ≥ 3.19 用 `--preset=default`，否则回退到传 `opts` 的老办法。
4. **构建并打包**（[L40](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/new_op_project_template/custom_op/build.sh#L40)）：`cmake --build build_out --target binary package`——同时构建二进制（`binary`）和打包（`package`）两个目标。

`CMakePresets.json` 里几个关键开关（见 [utils/templates/new_op_project_template/custom_op/CMakePresets.json:28-63](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/new_op_project_template/custom_op/CMakePresets.json#L28-L63)）：

- `ASCEND_COMPUTE_UNIT`：占位符 `__ASCEND_COMPUTE_UNIT__`，msopgen 会用 `-c ai_core-<soc>` 传入的真实型号替换它。
- `vendor_name`：默认 `customize`，决定算子安装到 `vendors/<vendor_name>/`。
- `ASCEND_CANN_PACKAGE_PATH`：CANN 安装路径。
- `ASCEND_PACK_SHARED_LIBRARY`：是否打包成单一共享库（`False` = 传统多库分离交付）。

经典 customize 工程的默认值在 [cmake/config.cmake](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/op_project_templates/ascendc/customize/cmake/config.cmake) 里，例如默认支持型号为 `ascend910b;ascend310p`（[L20-22](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/op_project_templates/ascendc/customize/cmake/config.cmake#L20-L22)）、自定代码生成路径 `ASCEND_AUTOGEN_PATH`（[L40](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/op_project_templates/ascendc/customize/cmake/config.cmake#L40)）。

#### 4.2.5 代码实践：比对两套模板的构建入口

> **实践目标**：通过对比根 CMake，直观感受"高层宏 vs 底层原语"的抽象差异。

**操作步骤**：

1. 打开 [utils/templates/new_op_project_template/custom_op/CMakeLists.txt](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/new_op_project_template/custom_op/CMakeLists.txt)，数一下它有多少行、用了几个 `npu_op_*` 宏。
2. 打开 [utils/templates/op_project_templates/ascendc/customize/CMakeLists.txt](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/op_project_templates/ascendc/customize/CMakeLists.txt)，找到它打 run 包的 CPack 段（[L56-76](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/op_project_templates/ascendc/customize/CMakeLists.txt#L56-L76)）。
3. 在仓库根执行 `find utils/templates -name CMakeLists.txt | sort`，统计两套模板各自的 CMakeLists 数量。

**需要观察的现象**：新式骨架根 CMake 仅约 20 行、无任何 CPack 代码；经典 customize 根 CMake 有 70+ 行，显式写了 CPack 与 makeself。

**预期结果**：新式骨架用 `npu_op_package` 一个宏就涵盖了"配置+打包"，经典工程需要手写 CPack 段。这就是抽象层级差异的直接体现。

#### 4.2.6 小练习与答案

**练习 1**：新式骨架的根 CMake 里，为什么 `framework` 的 `add_subdirectory` 要套 `if(EXISTS ...)`，而 `op_host`/`op_kernel` 也要套？

**参考答案**：因为这三层是**可选组合**。一个纯 Kernel 算子可能不需要 framework 适配层；某些只调测 kernel 的场景可能暂不含 op_host。`if(EXISTS ...)` 让同一套模板能适配不同形态的算子工程，缺失的目录自动跳过、不报错。

**练习 2**：`vendor_name` 这个变量在整条链路里出现了几次、分别控制什么？

**参考答案**：至少三处——(1) CMakePresets.json 里的默认值 `customize`；(2) 经典工程的 `modify_vendor` 目标用它 `sed` 替换 install.sh 里的 `vendor_name=customize`（[customize/CMakeLists.txt:42-47](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/op_project_templates/ascendc/customize/CMakeLists.txt#L42-L47)）；(3) install.sh 据此决定安装目录 `vendors/<vendor_name>/`（见 4.3.3）。它最终决定算子装到 OPP 包的哪个厂商子目录下，正是 u8-l1 里 optype_collector 扫描的 `vendors/` 来源。

---

### 4.3 host/kernel/framework 分工

#### 4.3.1 概念说明

一个自定义算子工程之所以要拆成 `op_host`、`op_kernel`、`framework` 三个目录，是因为一次算子调用发生在**两个不同的硬件域**、并可能被**多种训练框架**触发：

- **op_host（Host 侧）**：跑在 CPU 上。负责两件事——(a) **Tiling**：根据输入 shape 算出数据怎么切分给各核；(b) **算子原型注册**：把算子的输入输出类型、支持的芯片型号登记到运行时。它还要提供 aclnn 的 C 调用接口，让上层应用能调到这个算子。
- **op_kernel（Kernel 侧）**：跑在 NPU AI Core 上。是真正的并行计算代码，即 u2-l2 讲过的 Ascend C 核函数（CopyIn/Compute/CopyOut 三段式）。
- **framework（框架适配层）**：当算子要从 TensorFlow/ONNX/Caffe 等训练框架调用时，需要一个"翻译插件"，把框架的算子语义映射到自定义算子。不需要框架集成时这一层可缺省。

这种"Host 准备 + Kernel 计算 + Framework 翻译"的三段划分，是 CANN 算子工程的标准范式。

#### 4.3.2 核心流程：三层各自编译出什么

新式骨架里，三层各自编译的产物（通过 `npu_op_package_add` 汇总到一个算子包）：

```
op_host/  ──npu_op_code_gen + npu_op_library──►  cust_opapi   (aclnn C 接口库)
                                          ──►  cust_op_proto (算子原型库)
                                          ──►  cust_optiling (Tiling 库)
op_kernel/ ──npu_op_kernel_library──────────►  ascendc_kernels (NPU 核二进制)
framework/tf_plugin/ ──npu_op_library────────►  cust_tf_parsers (TF 算子解析插件)
```

经典 customize 工程产物同名但实现路径不同：op_host 用 `opbuild` 自动生成代码（产物 `cust_opsproto_rt2.0`、`cust_opmaster_rt2.0`、`cust_opapi`，见 [customize/op_host/CMakeLists.txt:10-65](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/op_project_templates/ascendc/customize/op_host/CMakeLists.txt#L10-L65)），op_kernel 用 `add_kernels_compile()` 编译核函数（[customize/op_kernel/CMakeLists.txt:6](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/op_project_templates/ascendc/customize/op_kernel/CMakeLists.txt#L6)）。

#### 4.3.3 源码精读：op_host 的 Tiling 与注册

op_host 的算法逻辑以 AddCustom 为例。它做两件事。

**第一件：Tiling 函数**——计算数据切分策略，把结果写进 TilingData：

```cpp
namespace optiling {
const uint32_t BLOCK_DIM = 8;
const uint32_t TILE_NUM = 8;
static ge::graphStatus TilingFunc(gert::TilingContext* context) {
    AddCustomTilingData* tiling = context->GetTilingData<AddCustomTilingData>();
    uint32_t totalLength = context->GetInputShape(0)->GetOriginShape().GetShapeSize();
    context->SetBlockDim(BLOCK_DIM);     // 用多少个核
    tiling->totalLength = totalLength;   // 总元素数
    tiling->tileNum = TILE_NUM;          // 每核再切几块
    return ge::GRAPH_SUCCESS;
}
}
```

（见 [examples/03_msopgen/op_dev/op_host/add_custom.cpp:15-27](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/03_msopgen/op_dev/op_host/add_custom.cpp#L15-L27)）

`AddCustomTilingData` 是一个普通结构体，定义在 kernel 侧头文件里（[add_custom_tiling.h:15-18](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/03_msopgen/op_dev/op_kernel/add_custom_tiling.h#L15-L18)），host 与 kernel 共享同一份结构布局——host 写入、kernel 读出，这正是 Tiling 沟通两域的桥梁。

**第二件：算子原型注册**——用 `OpDef` 链式 DSL 描述接口，并登记支持的芯片：

```cpp
namespace ops {
class AddCustom : public OpDef {
public:
    explicit AddCustom(const char* name) : OpDef(name) {
        this->Input("x").ParamType(REQUIRED).DataType({ge::DT_FLOAT16}).Format({ge::FORMAT_ND});
        this->Input("y").ParamType(REQUIRED).DataType({ge::DT_FLOAT16}).Format({ge::FORMAT_ND});
        this->Output("z").ParamType(REQUIRED).DataType({ge::DT_FLOAT16}).Format({ge::FORMAT_ND});
        this->AICore()
            .SetTiling(optiling::TilingFunc)
            .AddConfig("ascend910").AddConfig("ascend310p")
            .AddConfig("ascend310b").AddConfig("ascend910b").AddConfig("ascend950");
    }
};
OP_ADD(AddCustom);   // 全局注册
}
```

（见 [examples/03_msopgen/op_dev/op_host/add_custom.cpp:29-48](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/03_msopgen/op_dev/op_host/add_custom.cpp#L29-L48)）

注意这里的 `Input("x")...DataType({ge::DT_FLOAT16})` 与 4.1.3 里 json 的 `"type":["fp16"]` 是**同一信息的两种表达**——json 给 msopgen 看，OpDef 给运行时看。这也是为什么改了 json 要同步改这份 `.cpp`。

#### 4.3.4 源码精读：op_kernel 的核函数

op_kernel 的内容在 u2-l2 已经详细讲过（KernelAdd 类、三段式、TQue），这里只点出它与 op_host 的衔接点——核函数签名和 Tiling 读取：

```cpp
extern "C" __global__ __aicore__ void add_custom(
    GM_ADDR x, GM_ADDR y, GM_ADDR z, GM_ADDR workspace, GM_ADDR tiling) {
    REGISTER_TILING_DEFAULT(AddCustomTilingData);
    GET_TILING_DATA(tilingData, tiling);               // 读出 host 写入的 TilingData
    KernelAdd op;
    op.Init(x, y, z, tilingData.totalLength, tilingData.tileNum);  // 用 tiling 初始化
    op.Process();
}
```

（见 [examples/03_msopgen/op_dev/op_kernel/add_custom.cpp:80-87](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/03_msopgen/op_dev/op_kernel/add_custom.cpp#L80-L87)）

`GET_TILING_DATA` 把 `tiling` 指针（host 侧 `TilingFunc` 填充的那块内存）解析成 `tililingData`，于是 `totalLength`/`tileNum` 就在 kernel 里可用了。这就是 host 与 kernel 的数据通路：**TilingFunc 写 → tiling 内存 → GET_TILING_DATA 读**。

#### 4.3.5 源码精读：framework 适配层

framework 层在新式骨架里极薄。tf_plugin 的 CMakeLists 只做"收集本目录 `.cc`、编成 `cust_tf_parsers` 库、加入算子包"：

```cmake
aux_source_directory(${CMAKE_CURRENT_SOURCE_DIR} plugin_srcs)
if(NOT plugin_srcs)
    return()                              # 没有插件源码就跳过
endif()
npu_op_library(cust_tf_parsers TF_PLUGIN ${plugin_srcs})
npu_op_package_add(${package_name} LIBRARY cust_tf_parsers)
```

（见 [utils/templates/new_op_project_template/custom_op/framework/tf_plugin/CMakeLists.txt:12-24](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/new_op_project_template/custom_op/framework/tf_plugin/CMakeLists.txt#L12-L24)）

`if(NOT plugin_srcs) return()` 这行很关键：它让"没有框架插件"成为合法状态——如果 msopgen 没有为某个框架生成 `.cc`，该目录自动空过，不影响构建。经典工程的 framework 根 CMake 进一步用 `if(EXISTS ...)` 守卫 caffe/tf/onnx 三种插件（[customize/framework/CMakeLists.txt:1-11](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/op_project_templates/ascendc/customize/framework/CMakeLists.txt#L1-L11)）。

#### 4.3.6 源码精读：install.sh 如何把产物落到 OPP

打出来的 run 包执行时，靠 customize 工程 [scripts/install.sh](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/op_project_templates/ascendc/customize/scripts/install.sh) 把各层产物安装到 OPP 的 `vendors/<vendor_name>/` 下。它的安装目标目录与 4.3.2 的产物一一对应：

| install.sh 调用 | 安装的层 | 目标目录 |
|-----------------|----------|----------|
| `upgrade framework`（[L259](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/op_project_templates/ascendc/customize/scripts/install.sh#L259)） | framework 插件 | `vendors/<vendor>/framework/` |
| `upgrade op_proto`（[L265](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/op_project_templates/ascendc/customize/scripts/install.sh#L265)） | 算子原型库 | `vendors/<vendor>/op_proto/` |
| `upgrade op_impl`（[L271-272](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/op_project_templates/ascendc/customize/scripts/install.sh#L271-L272)） | Tiling 库 + 核二进制 | `vendors/<vendor>/op_impl/` |
| `upgrade op_api`（[L278](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/op_project_templates/ascendc/customize/scripts/install.sh#L278)） | aclnn 接口库 | `vendors/<vendor>/op_api/` |

安装完成后，它还会写入 `vendors/config.ini` 的 `load_priority=$vendor_name`（[L320](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/op_project_templates/ascendc/customize/scripts/install.sh#L320)），告诉运行时优先加载哪个厂商包——这正是 u8-l1 里 optype_collector 扫描 `vendors/` 时读到的那条配置。至此，从 msopgen 生成骨架到算子被运行时加载的全链路闭环。

#### 4.3.7 代码实践：跟踪 Tiling 的跨域数据流

> **实践目标**：用源码阅读的方式，跟踪一个值（`tileNum`）从 host 写入到 kernel 读出的完整路径。

**操作步骤**：

1. 打开 [op_host/add_custom.cpp](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/03_msopgen/op_dev/op_host/add_custom.cpp)，定位 [L24](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/03_msopgen/op_dev/op_host/add_custom.cpp#L24) `tiling->tileNum = TILE_NUM;`——这是 host 侧写入点。
2. 打开 [op_kernel/add_custom_tiling.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/03_msopgen/op_dev/op_kernel/add_custom_tiling.h)，看 [L16](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/03_msopgen/op_dev/op_kernel/add_custom_tiling.h#L16) 结构体字段 `uint32_t tileNum;`——这是双方共享的内存布局。
3. 打开 [op_kernel/add_custom.cpp](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/03_msopgen/op_dev/op_kernel/add_custom.cpp)，定位 [L83-85](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/03_msopgen/op_dev/op_kernel/add_custom.cpp#L83-L85) `GET_TILING_DATA(tilingData, tiling)` 与 `tililingData.tileNum`——这是 kernel 侧读出点。

**需要观察的现象**：同一个 `tileNum` 字段名在三处出现：host 写、结构体定义、kernel 读。它没有任何函数调用传递，而是通过一块共享内存（`tiling` 指针）。

**预期结果**：你能画出 `TilingFunc 写入 → AddCustomTilingData 结构体内存 → GET_TILING_DATA 读出` 这条无函数调用的数据通路。

#### 4.3.8 小练习与答案

**练习 1**：为什么 `AddCustomTilingData` 结构体要放在 `op_kernel/` 目录、却同时被 `op_host/add_custom.cpp` `#include`？

**参考答案**：因为它是 host 与 kernel 的"契约"。把定义放在 kernel 侧、host 侧 `#include "../op_kernel/add_custom_tiling.h"`（见 [op_host/add_custom.cpp:11](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/03_msopgen/op_dev/op_host/add_custom.cpp#L11)），保证两端看到的内存布局完全一致——否则 TilingData 读写会错位。这是 C 结构体跨编译单元共享的标准做法。

**练习 2**：如果某个算子只想被 ACLNN C 接口调用、不需要任何训练框架集成，framework 层该怎么处理？

**参考答案**：直接不要 framework 目录，或保留目录但里面不放任何 `.cc`。新式骨架的 `if(NOT plugin_srcs) return()`（[tf_plugin/CMakeLists.txt:13-15](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/new_op_project_template/custom_op/framework/tf_plugin/CMakeLists.txt#L13-L15)）会自动跳过；根 CMake 的 `if(EXISTS .../framework)` 也会在目录缺失时跳过 `add_subdirectory`。算子包照常构建，只是少了 `cust_tf_parsers` 产物。

---

## 5. 综合实践

**任务：在脑中（或本地）走通"json → 骨架 → 三层产物 → 安装目录"的完整映射，并画一张端到端数据流图。**

具体做法：

1. **输入侧**：写出 `add_custom.json` 里 `op`、`input_desc`、`output_desc` 三个字段分别"流向"了生成工程的哪些地方。（提示：`op` → OpDef 类名与 framework 插件文件名；`input_desc`/`output_desc` → OpDef 的 `Input()`/`Output()` 链与 aclnn 接口签名。）
2. **构建侧**：在新式骨架的三个 `CMakeLists.txt`（op_host / op_kernel / framework）旁标注它们各自调用的 `npu_op_*` 宏与产出库名。
3. **安装侧**：对照 install.sh 的四个 `upgrade` 调用，把每个库映射到 `vendors/<vendor>/` 下的目标子目录。
4. **运行侧**：标出 `tileNum` 从 `TilingFunc` 到 `GET_TILING_DATA` 的跨域路径。

最终交付一张图（手绘或文本均可），包含：算子原型 json → msopgen → custom_op 骨架 → 三层编译产物 → run 包安装 → OPP vendors 目录 → 被 aclnn 调用。

> 如果本地有 CANN 与 msopgen 环境，可额外执行 README 的第 1–6 步（[examples/03_msopgen/README.md:78-115](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/03_msopgen/README.md#L78-L115)）实跑一遍，用 `find custom_op -type f` 与 `tree build_out` 验证你的图。实跑结果待本地验证。

---

## 6. 本讲小结

- **msopgen 是外部工具**，asc-tools 只提供模板；msopgen 依据算子原型 json **生成工程骨架**（build.sh/CMakeLists/framework），算法源码由开发者手写后拷入。
- **`utils/templates` 下有两套模板**：新式 `new_op_project_template`（`npu_op_*` 高层宏，msopgen 标准骨架）与经典 `op_project_templates/ascendc/{customize,aclnn,common}`（底层 CMake 原语 + CPack/makeself 自打 run 包）。
- **根 CMake 的安装**由 `BUILD_OPEN_PROJECT` 开关触发，把两套模板树与 makeself 工具一并装进 CANN 的 `tools/`。
- **build.sh 是薄封装**：定位 CANN 路径 → 解析 CMakePresets → `cmake --preset` 配置 → `--target binary package` 构建+打包。
- **算子工程三层分工**：op_host（CPU 侧 Tiling + 原型注册 + aclnn 接口）、op_kernel（NPU 侧 Ascend C 核函数）、framework（训练框架适配插件，可选）。
- **Tiling 是 host/kernel 的桥梁**：`TilingFunc` 写入共享结构体 → `GET_TILING_DATA` 读出；install.sh 再把各层产物落到 `vendors/<vendor>/` 的四个子目录，完成与 u8-l1 optype_collector 的链路闭环。

---

## 7. 下一步学习建议

- **向"构建系统"深入**：本讲看到了 `npu_op_package`、`opbuild`、CPack 等 CANN 构建宏。第 9 单元（u9-l1 CMake 构建系统与多架构产物）会从 asc-tools 自身角度讲 CMake 多架构组织，可对照阅读，理解"工具自身的构建"与"工具生成的算子工程的构建"有何异同。
- **向"交付"延伸**：u9-l2（打包安装与 run 包生成）会详细讲 run 包的安装机制与软链，可与本讲 install.sh 的 `load_priority` 配置对照。
- **回到工具链全景**：学完本讲，你已经看完了 asc-tools 提供的"算子工程模板"这一隐性能力。建议回头重读 u1-l1 的工具链关系图，确认 msopgen 模板与 cpu debug / npu check / msobjdump / optype_collector 五大工具的位置关系。
- **源码延伸阅读**：若对生成流程感兴趣，可阅读 [utils/templates/op_project_templates/ascendc/common/util/opdesc_parser.py](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/utils/templates/op_project_templates/ascendc/common/util/opdesc_parser.py)，它是构建期解析算子描述的 Python 辅助脚本，与 json 原型定义直接相关。
