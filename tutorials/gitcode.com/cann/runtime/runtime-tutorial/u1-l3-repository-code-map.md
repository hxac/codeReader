# u1-l3 仓库目录结构与代码地图

## 1. 本讲目标

前两讲我们已经知道了 CANN Runtime「是什么」（u1-l1 的分层架构）以及「怎么编出来」（u1-l2 的 build.sh 编译链路）。本讲解决第三个问题：**「代码都在哪」**。

学完本讲，你应该能够：

1. 说出仓库根目录下每个目录/关键文件的职责，拿到一个任务就知道该去哪个目录找代码。
2. 画出 `src/` 内部「ACL 对外 API 层 → Runtime 核心层 → 维测组件 → 支撑模块」四大板块的位置关系。
3. 掌握一条通用「定位路线」：给出任意一个 `aclrtXxx` 接口名，能沿着代码地图在 5 分钟内找到它从声明、导出、实现到下沉驱动的全部源码位置。
4. 知道样例（example）、单测（tests）、文档（docs）三类辅助资源的组织方式，后续遇到问题知道去哪里运行、验证、查资料。

## 2. 前置知识

本讲是「看地图」，不需要新的硬件或编程知识，但默认你已从前两讲了解：

- **分层架构（u1-l1）**：应用调用 `aclrtXxx` 接口 → ACL 对外 API 层 → Runtime 核心层 → 驱动适配层，最终经 `/dev/davinci*` 设备文件进入内核态驱动。本讲会把这张逻辑图落成**具体目录和文件**。
- **编译链路（u1-l2）**：`build.sh` 驱动 CMake 完成编译，产物是 `build_out` 下的 run 包。本讲会解释 CMake 为什么按某个顺序编译各模块——那个顺序本身就是一张依赖地图。

两个阅读源码的小常识，本讲会反复用到：

- **永久链接**：每段关键代码都给出形如 `[路径:L行号](仓库地址#L行号)` 的链接，点击可直接跳到线上对应行。
- **CMake 的 `add_subdirectory`**：CMake 构建系统中，每出现一次 `add_subdirectory(某目录)`，就代表「进入该目录继续处理它的 CMakeLists.txt」。顶层脚本里这些语句的先后顺序，大致反映了模块间的依赖与构建先后关系。

## 3. 本讲源码地图

本讲涉及的关键文件（只读，不改动）：

| 文件/目录 | 作用 |
|---|---|
| [README.md](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/README.md) | 官方目录结构说明，本讲 4.1 的对照基准 |
| [src/CMakeLists.txt](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/src/CMakeLists.txt) | src 内各模块的构建顺序，本讲 4.2 的核心证据 |
| [include/external/acl/acl_rt.h](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/include/external/acl/acl_rt.h) | 对外发布的 aclrt 接口声明头文件 |
| [src/acl/aclrt/acl_rt.cpp](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/src/acl/aclrt/acl_rt.cpp) | ACL 动态库的符号导出入口（宏展开） |
| [src/acl/aclrt_impl/acl_rt_wrapper.h](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/src/acl/aclrt_impl/acl_rt_wrapper.h) | aclrt 接口清单宏，串起「导出」与「实现」 |
| [src/acl/aclrt_impl/device.cpp](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/src/acl/aclrt_impl/device.cpp) | aclrt 设备类接口的 Impl 实现 |
| [src/runtime/api/api_c_device.cc](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/src/runtime/api/api_c_device.cc) | Runtime 层 `rtXxx` 设备类 C 接口 |
| [src/runtime/api/api.hpp](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/src/runtime/api/api.hpp) | `Api` 抽象类，runtime 内部接口契约 |
| [src/runtime/core/src/api_impl/api_impl.cc](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/src/runtime/core/src/api_impl/api_impl.cc) | `ApiImpl` 真正干活的核心实现 |
| [src/runtime/driver/driver.cc](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/src/runtime/driver/driver.cc) | 驱动适配层工厂 |
| [tests/build_ut.sh](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/tests/build_ut.sh) | UT 编译入口，含模块名到用例路径的映射表 |
| [docs/zh/README.md](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/docs/zh/README.md) | 中文文档导航首页 |
| [example/0_quickstart/0_hello_cann/main.cpp](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/example/0_quickstart/0_hello_cann/main.cpp) | 第一个可运行样例，实践任务的主角 |

## 4. 核心概念与源码讲解

### 4.1 仓库顶层布局：先认路标

#### 4.1.1 概念说明

一个几百万行的仓库，最怕的不是代码难，而是「不知道该打开哪个目录」。华为在 README 中给出了一份官方目录结构说明，我们以它为底图，补齐官方省略号里省掉的目录，形成一张**完整顶层地图**。

理解原则只有一条：**顶层目录按「角色」划分**——写的（src）、给的（include，对外头文件）、教的（example、docs）、测的（tests）、建的（build.sh、CMakeLists.txt、cmake）。

#### 4.1.2 核心流程

拿到仓库后建议按下面顺序建立空间感：

1. 先看 `README.md` 的目录结构段，建立骨架认知。
2. `ls` 根目录，把 README 里省略的目录（`pkg_inc`、`scripts`、`stub` 等）补全。
3. 分四类归位：源码类、接口发布类、学习验证类、构建合规类。
4. 之后任何问题先判断属于哪一类，再到对应目录定位。

#### 4.1.3 源码精读

官方目录结构说明位于 README 的「目录结构」章节：

- [README.md:L22-L53](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/README.md#L22-L53)：README 用一棵树说明了 `cmake`（工程编译目录）、`docs`（文档）、`example`（基于 acl 接口开发的样例）、`include`（对外发布头文件）、`pkg_inc`（仓间管控头文件）、`src`（各模块源代码）、`stub`（打桩）、`tests`（UT 用例）等目录的用途，以及 `CMakeLists.txt`、`build.sh` 两个构建文件。

结合实际 `ls` 根目录的结果，补全后的完整地图如下（✦ 为 README 树中未展开的条目）：

| 目录/文件 | 角色 | 说明 |
|---|---|---|
| `src/` | 源码 | 全部模块源代码，4.2 节展开 |
| `include/` | 接口发布 | 对外头文件；其中 `include/external/acl/` 下是 `acl.h`、`acl_rt.h` 等应用直接 include 的头文件，`dfx/`、`driver/` 等按来源再分层 |
| `pkg_inc/` | 接口发布 | 仓间管控头文件，按 `runtime`、`driver`、`platform`、`dump` 等模块分目录 |
| `example/` | 学习验证 | 7 大类样例（见 4.4） |
| `tests/` | 学习验证 | UT 用例 + `build_ut.sh` 编译入口 |
| `docs/` | 学习验证 | 中文文档 `docs/zh/`（见 4.4） |
| `cmake/`、`CMakeLists.txt`、`version.cmake` | 构建 | 构建配置骨架与版本定义（u1-l2 已讲） |
| `build.sh`、`install_deps.sh`、`download_3rd_party.py` | 构建 | 编译入口三件套（u1-l2 已讲） |
| `scripts/` ✦ | 构建 | 辅助脚本：`oat_check.sh`（开源合规检查）、`package`（打包）、`pre-smoking.sh`（冒烟） |
| `stub/` | 构建 | 打桩目录，含 `gen_stubapi.py`，用于生成符号检查用的桩 |
| `.devcontainer/` ✦ | 构建 | 容器化开发环境，README「Docker 源码构建」章节引用它 |
| `AGENTS.md`、`CONTRIBUTING.md`、`SECURITY.md`、`LICENSE` | 合规/协作 | 仓库指引（AGENTS.md 中同样有一份简明目录表） |
| `OAT.xml`、`Third_Party_Open_Source_Software_List.yaml`、`classify_rule.yaml` ✦ | 合规 | 开源审查、第三方软件清单、仓间分类规则 |

其中 AGENTS.md 的目录表可作为速查卡片：

- [AGENTS.md:L36-L51](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/AGENTS.md#L36-L51)：用一张表格概括 `include/`、`src/`（含 `src/acl`、`src/dfx/adump`、`src/dfx/msprof`、`src/dfx/log`、`src/runtime`）、`tests/`、`example/`、`cmake/` 的用途，是全仓最短的一张目录地图。

#### 4.1.4 代码实践

**实践：对照官方地图找路标（源码阅读型）**

1. **实践目标**：验证 README 的目录树与磁盘实际内容一致，并找出 README 未提到的 3 个顶层目录。
2. **操作步骤**：
   ```bash
   cd <仓库根目录>
   ls -d */          # 只列目录
   grep -n "目录结构" README.md   # 定位官方说明的行号
   ls include/external/acl/      # 看看应用侧真正 include 的头文件长什么样
   ```
3. **需要观察的现象**：`include/external/acl/` 下应能看到 `acl.h`、`acl_rt.h`、`acl_base.h`、`acl_tdt.h` 等头文件——它们就是你写应用时 `#include "acl/acl.h"` 实际引用的文件。
4. **预期结果**：README 目录树中的每个目录都能在磁盘上找到；同时能列出 `scripts/`、`pkg_inc/`、`.devcontainer/` 等 README 未在树中完整展开的目录。若在本地执行，以上命令均可直接复现。

#### 4.1.5 小练习与答案

**练习 1**：应用代码里 `#include "acl/acl.h"`，这个 `acl.h` 在仓库的哪个物理路径下？

答案：`include/external/acl/acl.h`。`include/` 是「3.1包整体对外发布的头文件」目录，安装 CANN 包后会被拷贝到安装路径的 include 目录，编译时通过 `-I` 搜索到。

**练习 2**：想了解仓库接受外部贡献的流程，应该看根目录哪个文件？

答案：`CONTRIBUTING.md`（贡献指南），README「相关信息」章节直接链接了它。

**练习 3**：`pkg_inc/` 和 `include/` 都是头文件目录，区别是什么？

答案：`include/` 面向**外部应用**（对外发布，如 `external/acl`）；`pkg_inc/` 是**仓间管控**头文件（供 CANN 内部多个仓之间共享，按 `runtime`、`driver`、`platform` 等模块组织），普通应用开发一般不需要关心。

### 4.2 src/ 源码主目录：四大板块与构建顺序

#### 4.2.1 概念说明

`src/` 是仓库的心脏，下辖十几个子目录。直接背列表容易忘，建议按「四大板块」理解：

1. **ACL 对外 API 层（`src/acl/`）**：实现 `aclrtXxx`/`aclXxx` 接口，是应用能直接调到的最上层。
2. **Runtime 核心层（`src/runtime/`）**：实现 `rtXxx` 接口与设备、流、内存等对象模型，是真正干活的地方。
3. **维测组件（`src/dfx/`）**：log（日志）、msprof（性能）、adump（精度 Dump）、error_manager（错误码）、trace（跟踪），横向服务于前两层。
4. **支撑模块**：`platform`（芯片平台信息）、`mmpa`（跨平台内存/进程抽象）、`tsd`、`aicpu_sched`（AICPU 任务调度）、`queue_schedule`（队列调度）、`cmodel_driver`（无卡仿真驱动）、`tprt`、`runtime_compact` 等，为上面三层提供公共能力。

#### 4.2.2 核心流程

CMake 顶层脚本 `src/CMakeLists.txt` 按依赖顺序逐个 `add_subdirectory` 进入各模块。把这个顺序读出来，就得到一张「自底向上」的构建地图：

```text
基础支撑：aicpu_sched → queue_schedule → mmpa → (cmake/stub)
维测底座：dfx/log/liblog → dfx/trace → tsd → dfx/error_manager → dfx/msprof → platform → dfx/adump
核心主体：runtime                    ← 先编 runtime
ACL 出口：acl/aclrt_impl → acl/aclrt → acl/acl_tdt_queue → acl/acl_tdt_channel   ← 后编 acl
```

规律非常清晰：**被依赖者先编，ACL 出口层最后编**——因为 ACL 层要调用 runtime 层与各维测组件。

#### 4.2.3 源码精读

- [src/CMakeLists.txt:L16-L33](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/src/CMakeLists.txt#L16-L33)：`add_subdirectory` 序列完整列出各模块构建顺序——第 20~26 行先后进入 `dfx/log/liblog`、`dfx/trace`、`tsd`、`dfx/error_manager`、`dfx/msprof`、`platform`、`dfx/adump`，第 27 行 `add_subdirectory(runtime)` 编译 runtime 核心，第 28~31 行才轮到 `acl/aclrt_impl`、`acl/aclrt`、`acl/acl_tdt_queue`、`acl/acl_tdt_channel`。这个文件就是 src 板块依赖关系的权威证据。

各板块内部再往下拆一层：

| 板块 | 子目录 | 职责 |
|---|---|---|
| `src/acl/` | `aclrt` | 动态库符号导出入口（`acl_rt.cpp` 用宏批量生成） |
| | `aclrt_impl` | aclrt 接口的实现体（`device.cpp`、`stream.cpp`、`memory.cpp`、`event.cpp`、`kernel.cpp` 等**按资源类型分文件**） |
| | `acl_tdt_queue` / `acl_tdt_channel` | 数据传输队列 / 通道（TDT） |
| `src/runtime/` | `api` | `rtXxx` C 接口（`api_c_device.cc`、`api_c_stream.cc`、`api_c_memory.cc` 等按资源分文件）+ `Api` 抽象类（`api.hpp`） |
| | `core` | 核心对象实现：`core/src/` 下有 `context`、`device`、`stream`、`event`、`memory`、`launch`、`task`、`api_impl` 等子目录 |
| | `driver` | 驱动适配：`driver.cc`（工厂）、`npu_driver.cc` 及 `v100`/`v200`/`v201` 平台适配 |
| | `feature` | 特性模块：`aclgraph`、`model`、`fusion`、`snapshot` 等 |
| | `config` | 芯片平台配置目录：`350`、`910_B_93`、`950`、`cloud`、`tiny` 等，**目录名即芯片/产品代号** |
| | `inc` | runtime 内部头文件 |
| `src/dfx/` | `log` / `msprof` / `adump` / `error_manager` / `trace` | 日志 / 性能采集 / 精度 Dump / 错误码管理 / 跟踪看门狗 |

一个容易混淆的点：`src/runtime/config` 下的 `350`、`950` 等目录不是版本号，而是**芯片平台代号**（如 950 对应 Ascend 950PR/950DT），runtime 据此为不同芯片加载不同配置。

#### 4.2.4 代码实践

**实践：读出构建顺序地图（源码阅读型）**

1. **实践目标**：亲眼从 CMake 脚本中提取模块构建顺序，验证 4.2.2 的地图。
2. **操作步骤**：
   ```bash
   grep -n "add_subdirectory" src/CMakeLists.txt
   ls src/runtime/config/     # 看芯片平台配置目录的命名
   ls src/runtime/feature/    # 看 runtime 支持哪些特性模块
   ```
3. **需要观察的现象**：`add_subdirectory` 输出中 `runtime`（第 27 行）排在 `acl/aclrt_impl`（第 28 行）之前；`config/` 下是一串芯片代号目录。
4. **预期结果**：与 4.2.2 的地图一致。若本地有编译环境，还可执行 `bash build.sh -v` 观察实际编译先后顺序是否吻合（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：新增了一个 aclrt 接口的实现代码，应该放在 `src/acl/aclrt` 还是 `src/acl/aclrt_impl`？

答案：`src/acl/aclrt` 只负责符号导出（宏展开生成薄壳函数），真正的实现逻辑放在 `src/acl/aclrt_impl`（按资源类型选择 `device.cpp`、`memory.cpp` 等文件）。

**练习 2**：为什么 `dfx/log` 要在 `runtime` 之前构建？

答案：runtime 核心代码大量使用日志宏打点（如 `RT_LOG`），依赖 log 模块提供的日志设施；CMake 中被依赖的模块必须先构建。这也解释了 `src/CMakeLists.txt` 中维测底座排在核心主体之前。

**练习 3**：`src/runtime/api` 和 `src/runtime/core` 为什么拆开？

答案：`api` 层定义稳定的 C 接口（`rtXxx`）与抽象类 `Api`（接口契约），`core` 层提供 `ApiImpl` 等具体实现。接口与实现分离，使得不同芯片平台（v100/v200/v201、tiny 等）可以替换实现而不动接口层。

### 4.3 一条调用链串起代码地图：跟着 aclrtSetDevice 走一遍

#### 4.3.1 概念说明

u1-l1 给出了全仓通用阅读范式：`aclrtXxx → rtXxx → Api::Xxx → 具体对象`。本节把这条范式**落到文件坐标上**——这就是本讲最重要的「代码地图使用方法」：以后找任何接口，照这条路线走即可。

我们继续用第一讲用过的例子 `aclrtSetDevice`（设置当前进程使用的设备）。

#### 4.3.2 核心流程

从应用代码到驱动的六站路：

```text
① 对外声明     include/external/acl/acl_rt.h        应用能看到的函数原型
② 接口清单     src/acl/aclrt_impl/acl_rt_wrapper.h   宏清单里登记 aclrtSetDevice
③ 符号导出     src/acl/aclrt/acl_rt.cpp              宏展开生成薄壳，转发到 Impl
④ ACL 实现     src/acl/aclrt_impl/device.cpp          aclrtSetDeviceImpl：日志/统计/校验，调 rtSetDevice
⑤ Runtime 接口 src/runtime/api/api_c_device.cc        rtSetDevice：取 Api 单例，调 Api::SetDevice
⑥ 核心实现     src/runtime/core/src/api_impl/...      ApiImpl::SetDevice：真正建 Context、绑定线程
   （再往下）   src/runtime/driver/driver.cc           DriverFactory 按芯片取驱动适配对象
```

记忆口诀：**「声明在 include，登记在 wrapper，导出在 aclrt，实现在 impl，转译在 api，干活在 core」**。

#### 4.3.3 源码精读

- ① [include/external/acl/acl_rt.h:L1546](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/include/external/acl/acl_rt.h#L1546)：`aclrtSetDevice(int32_t deviceId)` 的对外声明，带 `ACL_FUNC_VISIBILITY` 可见性标记——这是应用侧唯一需要知道的一行。

- ② [src/acl/aclrt_impl/acl_rt_wrapper.h:L22-L53](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/src/acl/aclrt_impl/acl_rt_wrapper.h#L22-L53)：`ACL_RT_FUNC_MAP` 宏把全部 aclrt 接口写成一张清单（每行一个 `_(返回类型, 函数名, (形参), (实参))` 条目），第 53 行即 `aclrtSetDevice` 的登记项。**想知道某个 aclrt 接口是否存在，先在这张清单里搜**。

- ③ [src/acl/aclrt/acl_rt.cpp:L38-L46](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/src/acl/aclrt/acl_rt.cpp#L38-L46)：第 40 行 `ACL_RT_FUNC_MAP(ACL_RT_CPP)` 把清单展开成一批导出函数，每个函数体只是把参数原样转发给对应的 `...Impl` 版本——所以这个文件只有几十行，却生成了上百个符号。

- ④ [src/acl/aclrt_impl/device.cpp:L55-L69](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/src/acl/aclrt_impl/device.cpp#L55-L69)：`aclrtSetDeviceImpl` 先注册 profiling 打点与调用统计，打日志，第 60 行 `ACL_REQUIRES_RTS_OK(rtSetDevice(deviceId))` 完成 ACL→runtime 的交接，随后更新平台信息。

- ⑤ [src/runtime/api/api_c_device.cc:L76-L85](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/src/runtime/api/api_c_device.cc#L76-L85)：`rtSetDevice` 取 `Api::Instance()` 单例，第 81 行调用虚函数 `apiInstance->SetDevice(devId)`。接口契约定义在同目录 [src/runtime/api/api.hpp:L116](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/src/runtime/api/api.hpp#L116) 起的 `class Api`（纯虚接口集合）。

- ⑥ [src/runtime/core/src/api_impl/api_impl.cc:L3440-L3465](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/src/runtime/core/src/api_impl/api_impl.cc#L3440-L3465)：`ApiImpl::SetDevice` 是真正干活的地方——通过 `Runtime::Instance()` 拿到运行时单例，`PrimaryContextRetain` 为主设备保留/创建 Context，`InnerThreadLocalContainer::SetCurRef` 把 Context 绑定到**当前线程**（这解释了 u1-l1 讲的「多数 API 靠线程绑定的 Context 隐式确定设备」）。类继承关系见 [src/runtime/core/src/api_impl/api_impl.hpp:L32](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/src/runtime/core/src/api_impl/api_impl.hpp#L32)（`class ApiImpl : public Api`）。

- 再往下 [src/runtime/driver/driver.cc:L32-L42](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/src/runtime/driver/driver.cc#L32-L42)：`DriverFactory::GetDriver` 按驱动类型取得已注册的驱动实例——core 层最终通过这里的能力触达真实硬件，本讲只需知道「门在这里」。

#### 4.3.4 代码实践

**实践：自己用 grep 走一遍调用链（源码阅读型，本讲核心实践）**

1. **实践目标**：不借助本讲义，用搜索工具独立复现 4.3.2 的六站定位过程。
2. **操作步骤**：
   ```bash
   # ② 清单登记
   grep -n "aclrtSetDevice," src/acl/aclrt_impl/acl_rt_wrapper.h
   # ④ ACL 实现（找转发到 rtXxx 的那一行）
   grep -n "rtSetDevice" src/acl/aclrt_impl/device.cpp | head -3
   # ⑤ runtime C 接口
   grep -n "rtError_t rtSetDevice(" src/runtime/api/api_c_device.cc
   # ⑥ 核心实现
   grep -n "rtError_t ApiImpl::SetDevice" src/runtime/core/src/api_impl/api_impl.cc
   ```
   然后换一个接口重走一遍，例如 `aclrtCreateStream`（提示：⑤ 在 `src/runtime/api/api_c_stream.cc`，⑥ 搜 `CreateStream`）。
3. **需要观察的现象**：四条 grep 都能命中且行号与本讲引用一致；换 `aclrtCreateStream` 后同样能在 `api_c_stream.cc` 找到对应的 `rtCreateStream`。
4. **预期结果**：确认「include → wrapper → aclrt → impl → api → core」的路线对设备、流、内存等各类接口普遍成立。本地执行可直接复现。

#### 4.3.5 小练习与答案

**练习 1**：`acl_rt.cpp` 只有几十行，为什么能导出上百个 C 符号？

答案：它通过 `ACL_RT_FUNC_MAP(ACL_RT_CPP)` 等宏（[src/acl/aclrt/acl_rt.cpp:L38-L46](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/src/acl/aclrt/acl_rt.cpp#L38-L46)）把 `acl_rt_wrapper.h` 中的接口清单批量展开成函数定义，实现「清单即代码」。

**练习 2**：`rtSetDevice` 里 `apiInstance->SetDevice(devId)` 是虚函数调用，这样设计的好处是什么？

答案：`Api` 是接口契约（[src/runtime/api/api.hpp:L116](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/src/runtime/api/api.hpp#L116)），不同平台/形态（standard SoC、tiny、cmodel 仿真等）可提供不同 `ApiImpl` 子类，在运行期由创建器装配，api 层代码完全不用改。

**练习 3**：如果要在 `aclrtSetDevice` 流程中加一条诊断日志，加在哪一层最合适？

答案：一般加在 ④ `src/acl/aclrt_impl/device.cpp` 的 `aclrtSetDeviceImpl` 或 ⑥ `api_impl.cc` 的 `ApiImpl::SetDevice`——这两层分别掌握「接口级上下文（deviceId、错误码转换）」和「内部上下文（Context 指针、平台状态）」。注意本讲只做阅读建议，**不要**在练习环境中真的改动源码。

### 4.4 example / tests / docs：可运行、可验证、可查阅的三个入口

#### 4.4.1 概念说明

地图的最后一块是「配套设施」：改完代码去哪验证行为（example）、怎么确认没改坏（tests）、概念不懂查哪里（docs）。这三者与 `src/` 一一呼应：example 按特性分类组织，与 runtime 的资源模型对应；tests 按模块组织，与 src 的板块划分对应；docs 按读者层次组织，与本手册的进阶路线对应。

#### 4.4.2 核心流程

- **example**：`example/` 下按 `0_quickstart → 1_basic_features → 2_advanced_features → 3_memory_advanced → 4_reliability → 5_performance → 6_scenarios` 七级递进；每级内再按资源/主题分目录（如 `1_basic_features/` 下有 `device`、`memory`、`stream`、`event`、`context`）。每个样例目录自带 `README.md`、`CMakeLists.txt`、源码和 `run.sh`。
- **tests**：`tests/ut/` 下按模块分目录（`acl`、`runtime`、`runtime_c`、`platform`、`queue_schedule`、`aicpu_sched`、`slog`、`atrace`、`msprof`、`adump`、`tsd`、`error_manager`、`mmpa`），入口脚本 `tests/build_ut.sh` 用 `--ut=模块名 --target=目标名` 选择要跑的范围。
- **docs**：`docs/zh/` 分 `quick_start`（快速入门）、`dev_guide`（编程指南）、`api_ref`（API 参考）、`design`（架构指南）、`guidelines`（研发规范）、`FAQ`、`env_vars`、`error_code_ref`、`log_ref`。

#### 4.4.3 源码精读

- [example/README.md:L8-L14](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/example/README.md#L8-L14)：样例总览，说明七个分类各自的内容（quickstart 以 `aclnnAdd` 向量加法为入口演示完整闭环；basic_features 覆盖设备、内存、Stream；advanced_features 覆盖 Kernel 加载、ACL Graph 等）。

- [example/0_quickstart/0_hello_cann/main.cpp:L87-L97](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/example/0_quickstart/0_hello_cann/main.cpp#L87-L97) 与 [L220-L224](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/example/0_quickstart/0_hello_cann/main.cpp#L220-L224)：Hello CANN 的骨架——`aclInit` → `aclrtSetDevice` → `aclrtCreateStream` →（中间执行 aclnnAdd 并同步）→ `aclrtResetDeviceForce` → `aclFinalize`。这五行主线正是 4.3 调用链的「用户视角」，u1 系列后续实践都会回到它。

- [tests/build_ut.sh:L20-L35](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/tests/build_ut.sh#L20-L35)：`ut_path_map` 把模块名映射到用例路径——`acl` 对应 `tests/ut/acl`，`runtime` 对应 `tests/ut/runtime/runtime`，共 13 个模块。想知道「某模块的 UT 在哪」，查这张表即可。

- [docs/zh/README.md:L15-L22](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/docs/zh/README.md#L15-L22)：文档导航表，按「快速入门 / 编程指南 / API 参考 / 架构指南 / 研发规范」五类给出入口，其中 API 参考（`api_ref/`）按 01~21 编号覆盖设备、Context、Stream、Event、内存、执行控制等主题，与 `src/` 的资源分文件方式高度一致。

#### 4.4.4 代码实践

**实践：从样例到单测跑通一次验证闭环（可运行型，需本地环境）**

1. **实践目标**：体验「读样例 → 查 UT 映射 → 编译单测」的完整验证路径。
2. **操作步骤**：
   ```bash
   # ① 阅读 quickstart 样例主线
   grep -n "aclInit\|aclrtSetDevice\|aclrtCreateStream\|aclFinalize" \
       example/0_quickstart/0_hello_cann/main.cpp
   # ② 查 UT 模块映射（本讲已给出，可自行核对）
   grep -n "ut_path_map" tests/build_ut.sh | head -3
   # ③ 编译并运行 acl 模块 UT（需要已装 CANN 与第三方库，离线环境先 python3 download_3rd_party.py）
   bash tests/build_ut.sh --ut=acl --target=ascendcl_utest --cann_3rd_lib_path=${PWD}/third_party
   ```
3. **需要观察的现象**：① 输出与 4.4.3 描述的主线一致；③ 会拉起 googletest 并逐条打印 `PASS/FAIL`。
4. **预期结果**：UT 全部 PASS。步骤③依赖本地编译环境与昇腾相关配套，具体结果**待本地验证**；若仅想阅读，可打开 `tests/ut/acl/` 下任一用例文件，对照断言理解接口行为。

#### 4.4.5 小练习与答案

**练习 1**：想找「进程间共享内存」的样例，应该去 example 哪个分类找？

答案：`1_basic_features/memory/`（如 `11_ipc_memory_withoutpid`，docs/zh/README.md 进阶表中也直接链接了它）；README 总览说明 basic_features 包含进程间内存共享等样例。

**练习 2**：`--ut=runtime` 时脚本实际编译哪个目录的用例？目标名是什么？

答案：查 [tests/build_ut.sh:L22-L38](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/tests/build_ut.sh#L22-L38)：路径是 `tests/ut/runtime/runtime`，目标名是 `runtime_utest`。

**练习 3**：查 `aclrtMemcpy` 的参数含义，应优先看 docs 下哪类文档？

答案：`docs/zh/api_ref/`（API 参考）中内存管理相关篇章（如 `11-03_memory_copy_and_set.md`），那里有函数签名、参数说明与返回值。

## 5. 综合实践

**任务：为 `aclrtMemcpy` 制作一张「我的代码地图」卡片。**

要求模仿 4.3 对 `aclrtSetDevice` 的做法，独立完成：

1. **定位五站**：用 grep 分别找到 `aclrtMemcpy` 的 ① 对外声明（提示：`include/external/acl/`）、② wrapper 清单登记、③ ACL 实现（提示：`src/acl/aclrt_impl/memory.cpp`，实现函数名形如 `aclrtMemcpyImpl`）、④ runtime 层 `rtMemcpy`（提示：`src/runtime/api/api_c_memory.cc`）、⑤ 核心实现（提示：在 `src/runtime/core/src/` 中搜 `Memcpy`）。记录每一步的**文件路径与行号**。
2. **补一张测试与样例索引**：在 `tests/ut/acl/` 中 grep 一个调用 `aclrtMemcpy` 的用例文件；在 `example/1_basic_features/memory/` 中找到一个内存拷贝样例目录。
3. **画图**：把以上位置画成一张类似 4.3.2 的流程图，并在每个节点标注文件路径:行号。
4. **自检**：对照 `docs/zh/api_ref/` 中 memcpy 的文档，确认你找到的函数签名与文档一致。

预期产出：一张可保存的 markdown 卡片。以后接手任何 `aclrtXxx` 接口，套用这张卡片模板 5 分钟即可完成定位。（本任务为纯阅读型，无需编译环境；所有 grep 命令在仓库根目录执行即可。）

## 6. 本讲小结

- 仓库顶层按角色分四类：源码（`src/`）、接口发布（`include/`、`pkg_inc/`）、学习验证（`example/`、`tests/`、`docs/`）、构建合规（`build.sh`、`CMakeLists.txt`、`cmake/`、`scripts/`、`stub/` 等）。
- `src/` 分四大板块：ACL 对外 API 层（`src/acl/`）、Runtime 核心层（`src/runtime/`：api/core/driver/feature/config/inc）、维测组件（`src/dfx/`）、支撑模块（`platform`、`mmpa`、`tsd`、`aicpu_sched` 等）；`src/CMakeLists.txt` 的 `add_subdirectory` 顺序就是一张权威的依赖地图——先维测底座与支撑、再 runtime、最后 ACL 出口。
- 全仓通用定位口诀：**声明在 include，登记在 wrapper，导出在 aclrt，实现在 impl，转译在 api，干活在 core**；`aclrtSetDevice → rtSetDevice → Api::SetDevice → ApiImpl::SetDevice → DriverFactory` 是标准示范路线。
- example 按学习难度七级分类，tests 按 13 个模块组织（`tests/build_ut.sh` 的 `ut_path_map` 是模块名→用例路径的查询表），docs 按读者层次五类组织——三者都与 src 的板块划分相互呼应。
- `src/runtime/config` 下的数字目录（350、910_B_93、950 等）是芯片平台代号，不是软件版本号。

## 7. 下一步学习建议

本讲之后，u1 单元收官：你已经建立了「定位任何代码」的空间能力。接下来进入 u2（ACL 层深度剖析），建议：

1. 精读 `src/acl/aclrt_impl/acl.cpp` 中的 `aclInit/aclFinalize` 实现，理解 ACL 层初始化都做了哪些准备（配置加载、回调管理、与 adump 的 shim 注册——本讲 4.3 引用过的 [src/acl/aclrt/acl_rt.cpp:L27-L35](https://github.com/gitcode.com/cann/runtime/blob/44a689408e8c06e4740320b265678f8d9c3c093d/src/acl/aclrt/acl_rt.cpp#L27-L35) 中的 `InitializeAscendDump` 就是伏笔）。
2. 通读 `src/acl/aclrt_impl/acl_rt_wrapper.h` 的完整宏清单，感受 ACL 对外接口的全貌，并思考宏分层（`ACL_FUNC_MAP`/`ACL_RT_FUNC_MAP`/`ACL_MDL_FUNC_MAP` 等）对应哪些头文件族。
3. 有环境的话跑通 `example/0_quickstart/0_hello_cann`（`bash run.sh`），把本讲的静态地图变成动态体验；随后对照 `tests/ut/acl/` 的初始化用例看断言如何覆盖 `aclInit` 的失败分支。
