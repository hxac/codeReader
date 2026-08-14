# 仓库目录结构与三大源码组织

## 1. 本讲目标

本讲承接「driver 项目定位与三层架构总览」建立的 DCMI / HAL / SDK-driver 三层心智模型，把视角从「概念」落到「磁盘上的目录」。读完本讲，你应该能够：

- 说清楚仓库根目录下每一个顶层文件和目录（`build.sh`、`cmake`、`src`、`pkg_inc`、`examples` 等）各负责什么。
- 区分 `src/` 下三棵源码树——`ascend_hal`、`sdk_driver`、`custom`——分别承载哪一层、编译成什么产物（用户态 `.so` 还是内核态 `.ko`）。
- 理解一个关键设计规律：**HAL 层和 SDK 层的目录往往是「镜像」的**，同一个逻辑模块在用户态和内核态各有一份，通过 HDC / ioctl 跨态通信。
- 拿到一个功能需求时，能快速判断「该去哪棵源码树里找代码」。

本讲只讲「目录怎么组织、代码放在哪」，不讲具体接口实现——那是后续讲义的主题。

## 2. 前置知识

阅读本讲前，请先确认你理解以下概念（都在 [u1-l1](u1-l1-project-overview-and-architecture.md) 中建立过）：

- **三层架构**：driver 仓对外分 DCMI 层、HAL 层、SDK-driver 层。DCMI 面向管理工具做校验和适配；HAL 编译成用户态动态库，面向计算运行时；SDK-driver 编译成内核模块（`.ko`），运行在内核态操作硬件。
- **Host / Device**：Host 指主机（CPU 侧），Device 指昇腾 NPU 芯片。
- **用户态 / 内核态**：用户态代码跑在普通进程里（如 `libascend_hal.so`），内核态代码跑在内核里（如 `.ko` 驱动），二者通过 `ioctl` 等系统调用跨越边界。
- **`.so` 与 `.ko`**：`.so` 是 Linux 动态链接库（用户态），`.ko` 是 Linux 内核可加载模块（内核态）。

一个帮助记忆的直觉：**「同名目录出现在两棵树里」往往意味着一个在用户态、一个在内核态，中间靠通信协议连起来。**本讲会反复用到这个直觉。

## 3. 本讲源码地图

本讲涉及的关键文件与目录如下：

| 路径 | 作用 |
| --- | --- |
| `README.md` | 项目说明，其中有一整段权威的「目录结构」说明，是本讲的主要依据。 |
| `CMakeLists.txt` | 仓库顶层 CMake 入口，定义了三棵源码树的路径变量，并 `add_subdirectory(src)`。 |
| `src/CMakeLists.txt` | 把三棵源码树 `add_subdirectory` 串起来的「装配文件」，决定了哪些树参与编译。 |
| `src/ascend_hal/CMakeLists.txt` | HAL 层编译入口，顶部写着 `# build libascend_hal.so`，逐个加入子模块。 |
| `src/ascend_hal/` | HAL 层源码树（用户态，编译成 `.so`），含 17 个子目录。 |
| `src/sdk_driver/` | SDK 层源码树（内核态，编译成 `.ko`），含 22 个子目录。 |
| `src/custom/` | 定制化特性源码树，承载 DCMI 实现、网络、灵渠、NDA/NDR 等扩展特性。 |
| `pkg_inc/` | 仓库对外提供的公共头文件目录，是 `.so` 对外的「API 门面」。 |

## 4. 核心概念与源码讲解

### 4.1 仓库根目录总览与三大源码树的装配

#### 4.1.1 概念说明

打开仓库根目录，你会看到一堆顶层文件和目录。不要被数量吓到——它们可以分成三类：

1. **编译与打包**：`build.sh`（唯一编译入口）、`CMakeLists.txt`（CMake 入口）、`cmake/`（编译配置脚本）、`scripts/`（打包与覆盖率脚本）。
2. **对外交付物**：`pkg_inc/`（对外头文件）、`examples/`（接口样例）。
3. **源码与文档**：`src/`（全部源码）、`docs/`（说明文档）、`test/`（UT 用例）。

其中 `src/` 是真正的代码主体，它内部又分成三棵树：`ascend_hal`、`sdk_driver`、`custom`，正好对应三层架构里的三层。

> 为什么要把目录结构和「装配」放在一起讲？因为「三棵树各自独立、但又通过编译脚本按需组合」是理解这个仓库的关键。同一份 `src/`，在不同的编译模式下，参与编译的树是不同的。

#### 4.1.2 核心流程

仓库根目录的关键条目（按 `README.md` 中的目录结构说明）：

```text
├── build.sh          # 编译脚本
├── cmake             # 编译配置目录
├── CMakeLists.txt    # CMake 入口
├── docs              # 说明文档
├── examples          # 接口使用样例
├── pkg_inc           # 对外头文件
├── scripts           # 脚本目录（打包/UT 覆盖率）
├── src               # 全部源码（ascend_hal / sdk_driver / custom）
└── test              # UT 用例
```

根 `CMakeLists.txt` 先定义三棵树的路径变量，再把控制权交给 `src/CMakeLists.txt`，由后者决定哪棵树参与编译。装配规则是：

```text
编译模式 BUILD_COMPONENT=DRIVER  → ascend_hal + sdk_driver (+ custom)
编译模式 BUILD_COMPONENT=DRIVER_COMPAT → 只编译 ascend_hal（兼容包）
```

#### 4.1.3 源码精读

根 `CMakeLists.txt` 里有两段关键注释，点明了 `.so` 与 `.ko` 的分工：用户态树和内核态树是分开的路径变量。

[README.md:44](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/README.md#L44)：`pkg_inc` 是「本仓对外提供的头文件」，这是后面要讲的外部 API 门面。

[CMakeLists.txt:33-38](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/CMakeLists.txt#L33-L38)：定义了三棵树的路径变量——`DRIVER_USER_DIR` 指向 `src/ascend_hal`（用户态），`DRIVER_KERNEL_DIR` 指向 `src/sdk_driver`（内核态），`DRIVER_CUST_DIR` 指向 `src/custom`（定制化），`DRIVER_HAL_INC_DIR` 指向 `pkg_inc`。

[CMakeLists.txt:52](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/CMakeLists.txt#L52)：`add_subdirectory(src)`，把编译控制权交给 `src/` 目录。

真正决定「哪棵树参与编译」的是 `src/CMakeLists.txt`：

[src/CMakeLists.txt:10-25](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/CMakeLists.txt#L10-L25)：可以看到 `DRIVER` 模式下会 `add_subdirectory(ascend_hal)`、`add_subdirectory(sdk_driver)`，并且仅当 `ENABLE_BUILD_PRODUCT` 为真时才 `add_subdirectory(custom)`；而 `DRIVER_COMPAT` 模式只编 `ascend_hal`。这说明 **custom 是「按产品形态可选」的定制层**，而 ascend_hal / sdk_driver 是必备的主体。

#### 4.1.4 代码实践

1. **实践目标**：理解三棵源码树是如何被「装配」进一次编译的。
2. **操作步骤**：打开 `src/CMakeLists.txt`，定位到三个 `add_subdirectory(...)` 调用，记录它们各自的生效条件。
3. **需要观察的现象**：`custom` 这棵树的 `add_subdirectory` 被包在一个 `if(ENABLE_BUILD_PRODUCT)` 里，而另外两棵树没有这个保护。
4. **预期结果**：你能用自己的话说出「为什么不开启产品构建时 custom 不参与编译」——因为 custom 承载的是与具体产品形态强绑定的定制特性。
5. 待本地验证：若你有编译环境，可用 `bash build.sh --pkg --soc=ascend910b` 编译后对比 `build_out` 产物，观察是否包含 custom 相关产物。

#### 4.1.5 小练习与答案

**练习 1**：根目录里同时存在 `CMakeLists.txt` 和 `build.sh`，二者是什么关系？
**答案**：`build.sh` 是面向开发者的命令行编译入口（解析 `--soc/--pkg` 等参数），它内部调用 CMake；`CMakeLists.txt` 是 CMake 工程描述文件，被 `build.sh` 调用的 cmake 命令读取。一个是「门面脚本」，一个是「工程定义」。

**练习 2**：`src/CMakeLists.txt` 里 `ascend_hal` 出现了两次（一次在 `DRIVER_COMPAT`、一次在 `DRIVER`），为什么不是三次连写？
**答案**：因为不同 `BUILD_COMPONENT` 需要的产物不同。兼容包（DRIVER_COMPAT）只需要用户态 HAL 库，而完整包（DRIVER）还需要内核态 sdk_driver 和定制层 custom。用条件分支控制，是为了让一份 `src/` 能产出多种形态的包。

---

### 4.2 pkg_inc：对外公共头文件目录

#### 4.2.1 概念说明

`pkg_inc` 是仓库对外提供的**公共头文件目录**。它不包含实现代码，只有 `.h` 声明。你可以把它理解成 `libascend_hal.so` 这个动态库的「API 门面」——外部模块（如 Runtime、管理工具）要调用 driver，就 `#include` 这里的头文件。

为什么要单独拎出一个目录放头文件？因为对外接口必须**稳定、聚合、易发现**。把零散在各子模块里的头文件汇拢到 `pkg_inc`，外部使用者只需要关注这一个目录。

#### 4.2.2 核心流程

`pkg_inc` 里的头文件可以归成几组：

| 组别 | 代表文件 | 面向谁 |
| --- | --- | --- |
| HAL 主聚合头 | `ascend_hal.h` | 汇总 `base/external/dc` 等子头，给计算运行时用 |
| HAL 基础/扩展/DC | `ascend_hal_base.h`、`ascend_hal_external.h`、`ascend_hal_dc.h` | HAL 各类接口细分 |
| HAL 定义与类型 | `ascend_hal_define.h`、`ascend_hal_type.h` | 宏定义、类型定义 |
| HAL 错误码 | `ascend_hal_error.h`、`hal_error_code/` | 错误码声明 |
| DSMI 接口 | `dsmi_common_interface.h`、`dsmi_common_interface_base.h` | 设备系统管理接口 |
| 其他 | `ascend_inpackage_hal.h`、`dms_device_node_type.h`、`hal_pkg/` | 包内接口、节点类型 |

外部调用方一般只需要记住两套接口名前缀：`hal_*`（HAL 层）和 `dsmi_*`（设备系统管理）。

#### 4.2.3 源码精读

[README.md:44](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/README.md#L44)：注释明确写 `pkg_inc` 是「本仓对外提供的头文件」。

目录下实际存在的头文件（本机实际 `ls` 结果）包括：

```text
ascend_hal.h  ascend_hal_base.h  ascend_hal_dc.h  ascend_hal_define.h
ascend_hal_error.h  ascend_hal_external.h  ascend_hal_pkg.h  ascend_hal_type.h
ascend_inpackage_hal.h  dms_device_node_type.h
dsmi_common_interface.h  dsmi_common_interface_base.h
hal_error_code/   hal_pkg/
```

其中 `ascend_hal.h` 是**聚合头**，外部只需 `#include "ascend_hal.h"` 就能拿到一整套 HAL 接口；`dsmi_common_interface.h` 则是 DSMI（设备系统管理）这套接口的入口。这些头文件的**实现**分布在 `src/ascend_hal` 各子模块里——头文件在 `pkg_inc`，实现在 `src/ascend_hal`，这是「声明与实现分离」的典型组织。

#### 4.2.4 代码实践

1. **实践目标**：建立「头文件在 pkg_inc、实现在 ascend_hal」的对应关系。
2. **操作步骤**：在 `pkg_inc/dsmi_common_interface.h` 中任选一个接口声明（例如以 `dsmi_get_` 开头的查询接口），记下它的名字；再到 `src/ascend_hal/dmc/dsmi/` 目录下搜索同名函数定义。
3. **需要观察的现象**：声明出现在 `pkg_inc`，函数体出现在 `src/ascend_hal/dmc/dsmi/...`。
4. **预期结果**：你会直观看到「对外门面在 pkg_inc，实现在 ascend_hal/dmc」这条链路。
5. 待本地验证：具体函数定义文件需在本地用 `grep` 确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `pkg_inc` 里只有 `.h` 没有 `.c`？
**答案**：因为 `pkg_inc` 是对外公开的接口门面，只负责声明；真正的实现属于内部代码，放在 `src/ascend_hal` 里编译进 `libascend_hal.so`。对外只暴露声明，有助于保持接口稳定、隐藏实现细节。

**练习 2**：外部程序调用 HAL 接口时，应该 `#include` 哪个头文件最省事？
**答案**：`#include "ascend_hal.h"`。它是聚合头，已经把 base / external / dc 等子头包含进来了，一行即可拿到完整的 HAL 接口集合。

---

### 4.3 src/ascend_hal：用户态 HAL 层源码树

#### 4.3.1 概念说明

`src/ascend_hal` 是 HAL 层的源码树，也是整个仓库**体量最大**的一棵。它编译成用户态动态库 `libascend_hal.so`，面向计算运行时（Runtime/acl），向上暴露统一的 `hal_*` 接口。除了 `hal_*` 接口，它还内含大量基础设施：DSMI 设备管理、HDC 主机-设备通信、SVM 内存管理、PBL 公共库、TRS 任务调度、日志、性能采集、黑匣子等。

#### 4.3.2 核心流程

`ascend_hal` 下共有 17 个子目录。按职能可以这样归类：

| 类别 | 子目录 | 一句话用途 |
| --- | --- | --- |
| 通信地基 | `hdc` | 主机-设备通信（Host-Device Communication） |
| 通信地基 | `comm` | 通信层（含 urma 适配） |
| 公共库 | `pbl` | 基础公共库（UDA/URD/commlib/queryfeature） |
| 设备维护 | `dmc` | 设备维护组件（device_monitor/dsmi/logdrv/prof） |
| 设备管理 | `dms` | 设备管理系统 |
| 内存 | `svm` | 共享虚拟内存管理 |
| 任务 | `trs` | 任务资源调度 |
| 任务 | `esched` | 事件调度 |
| 网络 | `roce` | RoCE（RDMA over Converged Ethernet） |
| 维测 | `msnpureport` | 设备侧维测信息导出工具 |
| 维测 | `bbox` | 黑匣子（系统临终遗言） |
| 基础设施 | `mmpa` | 跨平台系统接口库 |
| 基础设施 | `buff` | 进程间共享内存管理 |
| 其他 | `queue` / `dpa` / `inc` / `build` | 消息队列 / 设备公共适配 / 内部头 / 编译脚本 |

本讲重点认识其中 **5 个核心子模块**：`dmc`、`hdc`、`pbl`、`svm`、`trs`（它们也是综合实践要映射的对象）。

#### 4.3.3 源码精读

[ascend_hal/CMakeLists.txt:11](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/CMakeLists.txt#L11)：文件顶部注释 `# build libascend_hal.so`，明确这棵树的产物就是用户态动态库；紧接着是一串 `add_subdirectory(...)` 把每个子模块逐个纳入编译。

[README.md:54-83](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/README.md#L54-L83)：`README.md` 对 `ascend_hal` 子树的逐项注释，给出了每个子目录的中文用途说明。

本机实际 `ls src/ascend_hal/` 得到的 17 个子目录与 README 描述一致。其中 5 个核心子模块的内部结构（本机实际 `ls` 结果）：

- `dmc/`：`device_monitor`（DSMI 消息通路）、`dsmi`（设备系统管理接口）、`logdrv`（日志）、`prof`（性能采集）、`prof_sample`（Host 侧采集注册）、`verify_tool`（镜像校验）。
- `hdc/`：`common`、`dc`、`inc`、`pcie`、`sock`、`ub`——按传输通道组织的主机-设备通信代码。
- `pbl/`：`uda`（统一设备接入）、`urd`（用户请求转发）、`commlib`（公共函数库）、`queryfeature`（软件特性查询）、`ubmm`（UB 内存适配）。
- `svm/`：`v2`、`v3`（两个版本目录）。
- `trs/`：`core`、`dc`、`inc`、`remote`、`shr_id`。

[svm/README.md:1-5](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/ascend_hal/svm/README.md#L1-L5)：SVM 自带的 README 把它的职责说得很清楚——「设备侧内存管理模块，向上层模块（如 Runtime）提供 HAL 接口」，这正是 `svm` 子目录在整棵 HAL 树中的定位。

#### 4.3.4 代码实践

1. **实践目标**：为 `ascend_hal` 下的 `dmc`、`hdc`、`pbl`、`svm`、`trs` 五个子模块各写一句话用途说明。
2. **操作步骤**：打开 `README.md` 的目录结构段，对照上面列出的内部子目录结构，用一句中文概括每个模块「解决什么问题」。
3. **需要观察的现象**：`pbl` 和 `hdc` 是「地基型」模块（被其他模块依赖），`svm`/`trs`/`dmc` 是「功能型」模块（直接提供对外能力）。
4. **预期结果**：参考答案见「综合实践」。例如 `hdc` 一句话：提供主机与设备之间的消息通信通道，是 HAL 各模块共用的通信底座。
5. 待本地验证：可进一步 `ls src/ascend_hal/<子模块>/` 确认内部目录与你写的一句话是否吻合。

#### 4.3.5 小练习与答案

**练习 1**：`ascend_hal` 里既有 `hal_*` 接口，又有 `dsmi_*` 接口，为什么放在同一棵树？
**答案**：因为二者都属于「用户态 HAL 库」的范畴，最终都编译进 `libascend_hal.so`。`hal_*` 面向计算运行时（内存/流/任务），`dsmi_*` 面向设备管理工具，但它们共享同一套用户态基础设施（HDC、PBL），所以同属一棵源码树。

**练习 2**：`roce` 子目录在 `ascend_hal/CMakeLists.txt` 里的加入是否有条件？
**答案**：有。`roce` 只在 `PRODUCT` 等于 `ascend910B` 时才 `add_subdirectory`，说明 RoCE 是与特定芯片形态绑定的特性，不是所有芯片都启用。

---

### 4.4 src/sdk_driver：内核态 SDK-driver 源码树

#### 4.4.1 概念说明

`src/sdk_driver` 是 SDK-driver 层的源码树，编译成 Linux 内核模块（`.ko`），运行在**内核态**。它负责真正与硬件打交道：中断处理、预留内存管理、任务调度、故障管理、算力切分等。上层的 HAL 用户态库通过 `ioctl` 陷入内核，把请求交给 sdk_driver 执行。

理解这棵树的**最重要规律**是：它的很多子目录与 `ascend_hal` **同名或对应**——同一个逻辑模块，用户态有一份、内核态有一份，中间靠 HDC/ioctl 连起来。这就是本讲反复强调的「镜像」直觉。

#### 4.4.2 核心流程

`sdk_driver` 下共有 22 个子目录。可以分成两组：

**与 ascend_hal「镜像」的模块**（用户态/内核态成对出现）：

| ascend_hal 子目录 | sdk_driver 对应子目录 | 协作关系 |
| --- | --- | --- |
| `hdc` | `hdc` | 通信通道：用户态 client ↔ 内核态 server |
| `pbl` | `pbl` | 公共库：用户态 helper ↔ 内核态 helper |
| `dmc` | `dmc` | 设备维护：用户态通路 ↔ 内核态（如 prof） |
| `svm` | `svm` | 内存管理：用户态 mmap ↔ 内核态建页表/分配物理页 |
| `trs` | `trsdrv`（注意命名不同！） | 任务调度：用户态提交 ↔ 内核态 sqcq 通信 |
| `buff` | `buff` | 进程间共享内存 |
| `comm` | `comm` | 通信层 |
| `dms` | `dms` | 设备管理系统 |

**仅内核态独有的模块**（用户态没有对应物，因为只能内核里做）：

| 子目录 | 用途 |
| --- | --- |
| `kernel_adapt` | 内核源码适配层，屏蔽不同内核版本差异 |
| `platform` | 芯片资源（中断、预留内存等）存储库 |
| `fms` | 故障管理系统 |
| `ts_agent` | 任务调度代理驱动 |
| `vascend` | 昇腾算力切分特性 |
| `vmng` / `vnic` / `vpc` | 设备虚拟化管理 / 虚拟网卡 / 虚物通信 |
| `dvpp` | 数字视觉预处理 |
| `seclib` | 公共安全函数库 |

> 小贴士：注意 `trs` ↔ `trsdrv` 这一对**命名不一致**。用户态叫 `trs`，内核态叫 `trsdrv`（内部还分 `trs` 和 `trsbase`），这是综合实践里容易踩坑的地方。

#### 4.4.3 源码精读

[README.md:91-115](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/README.md#L91-L115)：`README.md` 对 `sdk_driver` 子树的逐项注释，说明每个目录的用途。

本机实际 `ls src/sdk_driver/` 得到 22 个子目录，与 README 描述一致。镜像模块的内核侧内部结构（本机实际 `ls` 结果）：

- `hdc/`：`command`、`common`、`inc`、`pcie`、`ub`——内核侧的 HDC 通信实现，与用户态 `hdc/` 的 `common/pcie/ub` 形成对应。
- `pbl/`：`uda`、`dev_urd`、`mem_ops`、`msg_chan`、`soc_resmng`、`chip_config` 等——内核侧公共库，比用户态 `pbl` 更贴近硬件资源管理。
- `svm/`：`v2`、`v3`——与用户态 `svm` 完全镜像的版本结构。
- `trsdrv/`：`trs`、`trsbase`——对应用户态 `trs` 的内核侧 sqcq 通信与 mailbox 实现。
- `dmc/`：内核侧维护组件（含 `prof`）。

这些镜像关系印证了「同名/同义目录 = 一用户态一内核态」的判断法则。

#### 4.4.4 代码实践

1. **实践目标**：在 sdk_driver 中找到与 ascend_hal 五大子模块对应的内核侧目录。
2. **操作步骤**：对 `dmc / hdc / pbl / svm / trs` 五个名字，分别在 `src/sdk_driver/` 下找同目录或近义目录，记录对应关系。
3. **需要观察的现象**：前四个能找到同名目录，`trs` 对应的却是 `trsdrv`。
4. **预期结果**：建立一张「用户态 → 内核态」对应表（完整答案见综合实践）。
5. 待本地验证：可用 `ls src/sdk_driver/ | grep -E 'trs|svm|pbl|hdc|dmc'` 快速核对。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `kernel_adapt`、`platform`、`fms` 只出现在 sdk_driver，而 ascend_hal 里没有？
**答案**：因为它们是只能在内核态完成的工作。`kernel_adapt` 封装内核 API（不同内核版本差异必须在内核里处理）；`platform` 管理中断、预留内存等硬件资源；`fms` 处理硬件故障——这些都不可能放到用户态进程里做，所以没有用户态镜像。

**练习 2**：用户态 `trs` 和内核态 `trsdrv` 命名不一致，会造成什么影响？
**答案**：阅读源码时需要记住这个「别名」映射，否则按同名去 sdk_driver 里找 `trs` 会找不到。但内核侧 `trsdrv/` 内部又有一个 `trs` 子目录，所以 `src/sdk_driver/trsdrv/trs/` 才是真正对应 `src/ascend_hal/trs/` 的内核实现。

---

### 4.5 src/custom：定制化特性源码库

#### 4.5.1 概念说明

`src/custom` 是**定制化特性源码库**。它承载的是与具体产品形态、客户需求强绑定的扩展特性，因此它在 `src/CMakeLists.txt` 里被 `if(ENABLE_BUILD_PRODUCT)` 保护——只有开启产品构建时才参与编译。

`custom` 与 DCMI 层关系密切：DCMI 接口的**实现**大量位于 `custom` 树，而 DCMI 的**声明**头文件（如 `dcmi_interface_api.h`）放在 `src/custom/include/`。

#### 4.5.2 核心流程

`custom` 下的子目录（本机实际 `ls` 结果，含 README 未列出的 `nda`）：

| 子目录 | 用途 |
| --- | --- |
| `dev_prod` | 设备定制管理（含 `kernel` 与 `user` 两部分，user 下承载 DCMI 实现、npucli 命令行等） |
| `include` | 公共头文件导出目录（`dcmi_interface_api.h` 等就在这里） |
| `network` | DCMI 网络接口实现（HCCN 通信） |
| `lqdrv` | 灵渠 PCIe 故障检测 |
| `nda` | 高阶 RoCE 协商（ibv_extend V3 接口），README 目录树未列出 |
| `ndr` | NPU RDMA 直通特性 |
| `ops_debug` | 算子诊断目录 |
| `cmake` | CMake 编译配置目录 |

注意一个细节：**DCMI 接口头文件 `dcmi_interface_api.h` 不在 `pkg_inc`，而在 `src/custom/include/`**。这是因为 DCMI 属于定制层接口，而非 HAL 通用接口，所以放在 custom 树里。

#### 4.5.3 源码精读

[README.md:84-90](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/README.md#L84-L90)：`README.md` 对 `custom` 子树的注释，列出 `cmake/dev_prod/include/lqdrv/ndr/network/ops_debug`。

[src/CMakeLists.txt:18-20](https://github.com/gitcode.com/cann/driver/blob/e29d066fd6ee84cae705e2000a0387d721d3aaa0/src/CMakeLists.txt#L18-L20)：`custom` 的 `add_subdirectory` 被包在 `if(ENABLE_BUILD_PRODUCT)` 内，证明它是可选的定制层。

本机 `ls src/custom/` 实际包含 `nda` 目录（内部有 `ibv_extend`），这是 README 目录树中尚未列出的新增定制特性——说明**README 的目录树会滞后于实际代码**，定位代码时应以实际 `ls` 为准，README 作为用途说明的参考。

#### 4.5.4 代码实践

1. **实践目标**：定位 DCMI 接口头文件所在的源码树。
2. **操作步骤**：在仓库中搜索 `dcmi_interface_api.h`，确认它位于 `src/custom/include/`，而不是 `pkg_inc/`。
3. **需要观察的现象**：这个对外 DCMI 接口头文件归在 custom 树的 `include` 下，与 HAL 的 `pkg_inc` 分开存放。
4. **预期结果**：理解「HAL 通用头文件 → pkg_inc；DCMI 定制头文件 → custom/include」这条分工。
5. 待本地验证：可用 `find src -name dcmi_interface_api.h` 确认路径。

#### 4.5.5 小练习与答案

**练习 1**：`custom` 为什么是「可选」的，而 ascend_hal / sdk_driver 是必备的？
**答案**：ascend_hal 和 sdk_driver 构成了驱动的通用主体（用户态库 + 内核模块），任何形态的 driver 包都需要它们；custom 承载的是与具体产品/客户绑定的定制特性，不同产品形态裁剪不同，所以用 `ENABLE_BUILD_PRODUCT` 按需开启。

**练习 2**：README 目录树里没有 `nda`，但代码里有，这说明什么？
**答案**：说明 README 的目录结构说明会滞后于实际代码演进。定位代码时应以实际目录为准，README 主要用作「每个目录大概干什么」的参考。

---

## 5. 综合实践

本讲的综合实践就是把 4.3、4.4 串起来，完成 spec 要求的映射任务。

**任务**：依据 README 目录结构说明，为 `ascend_hal` 下的 `dmc`、`hdc`、`pbl`、`svm`、`trs` 五个子模块各写一句话用途说明，并指出 `sdk_driver` 中与之对应的内核侧模块。

**参考答案**：

| ascend_hal 子模块 | 一句话用途 | sdk_driver 对应内核侧模块 |
| --- | --- | --- |
| `dmc` | 设备维护组件，提供 DSMI 消息通路、日志、性能采集、镜像校验等维护能力 | `dmc`（内核侧含 `prof`） |
| `hdc` | 主机-设备通信通道，是 HAL 各模块共用的通信底座 | `hdc`（`command/common/pcie/ub`） |
| `pbl` | 基础公共库，提供 UDA 设备接入、URD 请求转发、commlib 公共函数、特性查询 | `pbl`（`uda/dev_urd/mem_ops/soc_resmng` 等） |
| `svm` | 设备侧共享虚拟内存管理，向上层 Runtime 提供 HAL 内存接口 | `svm`（`v2/v3`，与用户态版本结构镜像） |
| `trs` | 任务资源调度，提交与回收计算任务 | `trsdrv`（`trs/trsbase`，注意命名不同） |

**进阶**：在上面这张表的基础上，再画一张「调用穿越三层」的草图，标注一条请求如何从 `custom`（DCMI 实现）→ `ascend_hal`（HAL/DSMI + HDC）→ `sdk_driver`（内核态执行）→ NPU。这正是 u1-l1 讲过的跨层路径在本讲的目录层面的落点。

## 6. 本讲小结

- 仓库根目录分三类：编译打包（`build.sh`/`cmake`/`CMakeLists.txt`/`scripts`）、对外交付（`pkg_inc`/`examples`）、源码文档（`src`/`docs`/`test`）。
- `src/CMakeLists.txt` 是「装配文件」，按 `BUILD_COMPONENT` 决定哪几棵源码树参与编译；`custom` 还额外受 `ENABLE_BUILD_PRODUCT` 控制。
- `pkg_inc` 是对外头文件门面（`ascend_hal.h`、`dsmi_common_interface.h`），实现在 `ascend_hal` 各子模块。
- `ascend_hal`（用户态 `.so`）与 `sdk_driver`（内核态 `.ko`）的目录大量「镜像」：同名或同义目录 = 一个用户态、一个内核态，靠 HDC/ioctl 连接；注意 `trs` ↔ `trsdrv` 命名不一致。
- `custom` 是可选的定制层，承载 DCMI 实现、网络、灵渠、NDA/NDR 等；DCMI 头文件 `dcmi_interface_api.h` 在 `custom/include` 而非 `pkg_inc`。
- 定位代码时以实际 `ls` 为准，README 目录树可能滞后（如未列出的 `nda`）。

## 7. 下一步学习建议

掌握了目录结构后，建议按以下顺序继续：

1. 先读 **u1-l5（对外公共头文件与 API 总览）**，把 `pkg_inc` 和 `custom/include` 里的接口签名看明白，建立「接口名 → 用途」的索引。
2. 再读 **u2-l1（DCMI 接口总览与初始化流程）**，进入 `custom` 树看 DCMI 接口的具体实现，验证本讲建立的「DCMI 实现在 custom」的认知。
3. 想深入「镜像」协作机制，可直接跳到 **u3-l2（HDC 通信模型）**，看用户态 `ascend_hal/hdc` 与内核态 `sdk_driver/hdc` 如何通过 client/server/core 模型跨态通信。

继续阅读这些源码会加深理解：`src/ascend_hal/CMakeLists.txt`（看 HAL 子模块全貌）、`src/sdk_driver/CMakeLists.txt`（看内核子模块全貌）、`src/ascend_hal/svm/README.md`（一个完整的模块自述示例）。
