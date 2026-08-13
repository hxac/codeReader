# 五层架构总览

## 1. 本讲目标

本讲是入门篇的「骨架课」。学完后你应该能够：

- 说清 ATVOSS 的五层架构 **Device → Kernel → Block → Tile → Basic** 各自负责什么。
- 打开 `include/atvoss.h`，把它的每一个 `#include` 对应到具体某一层。
- 用一句话回答：「为什么 ATVOSS 要分成这五层？」
- 描述一次 `deviceOp.Run(...)` 从 Host 一直走到 Ascend C 底层 API 的端到端调用方向。

本讲**不**深入任何一层的实现细节（那是进阶篇 U2、专家篇 U3 的任务），只建立一张可以贯穿后续所有讲义的「整体地图」。

## 2. 前置知识

阅读本讲前，请确认你已经掌握以下内容（来自 u1-l1、u1-l2）：

- **ATVOSS 是什么**：一套基于 Ascend C 的 Vector 算子模板库，用声明式表达式描述逐元素/融合计算，底层仍调用 Ascend C 基础 API。
- **几个硬件名词**：
  - **AI Core（昇腾算核）**：芯片上的计算单元，一块 Ascend 950 上有多个核（多核并行）。
  - **GM（Global Memory）**：芯片外的全局显存，容量大但慢，Host 数据先到这里。
  - **UB（Unified Buffer）**：核内的高速缓存，容量小（几百 KB）但快，计算在 UB 里进行。
  - **Vector 计算单元**：专门做逐元素向量运算的硬件。
- **Tiling（分块/切分）**：因为 UB 装不下整份数据，需要把大任务切成小块（Tile）分批搬进 UB 计算，这叫 Tiling。
- **目录与构建**：`include/` 是 header-only 框架本体，用户只需 `#include "atvoss.h"`，用 `scripts/build.sh` 编译（见 u1-l2）。

如果上面某些概念还模糊，建议先回看 u1-l1 的术语表。本讲会用到「核 / GM / UB / Tiling」这几个词，但只用到「知道它们是什么」的程度。

> 一个直观比喻：把算子执行想象成「一家工厂处理一批货」。
>
> - **Device 层**＝工厂前台：接单、核对清单、安排车间、最后把成品发走（Host 侧总入口）。
> - **Kernel 层**＝车间调度：把整批货拆给多个车间（多核）。
> - **Block 层**＝单条流水线：一个车间内把货分成一筐一筐（Tile）轮流处理。
> - **Tile 层**＝操作手册：规定「搬货、加工、出货」每一步用什么标准动作。
> - **Basic 层**＝机器按钮：手册里的每个动作，最终都是工人按下 Ascend C 这台机器的按钮。
>
> 开发者通常只写「操作手册」（计算表达式），前四层的调度 ATVOSS 自动帮你做。

## 3. 本讲源码地图

本讲涉及的关键文件很少，但它们是整张地图的核心：

| 文件 | 作用 | 本讲用它来 |
|------|------|-----------|
| [include/atvoss.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/atvoss.h) | 用户唯一需要 include 的主入口头文件 | 看「五层如何被一个文件串起来」 |
| [docs/summary.md](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/summary.md) | 官方项目概述，含架构表格 | 取「五层职责对照」的权威描述 |
| [include/elewise/device/device_adapter.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/device_adapter.h) | Device 层核心 `DeviceAdapter` | 看 Host 侧入口与三步执行 |
| [include/elewise/device/tiling.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/tiling.h) | `CalculateTiling` 函数 | 看 Device 如何串联 Kernel/Block 的切分 |
| [include/elewise/kernel/builder.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/builder.h) | Kernel 层 `KernelBuilder` | 看多核切分核心组件 |
| [include/elewise/block/builder.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/builder.h) | Block 层 `BlockBuilder` | 看单核 Tile 切分核心组件 |
| [include/elewise/tile/tile_evaluate.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tile_evaluate.h) | Tile 层 `Tile::Evaluate` 入口 | 看表达式如何被驱动执行 |
| [include/elewise/tile/tensor_evaluator.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h) | Tile 层求值器特化 | 看动作如何落到 Ascend C API |
| [include/common/arch.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/common/arch.h) | 硬件常量 `DAV_3510` | 看分层背后的硬件约束 |
| [examples/abs/abs.cpp](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp) | 最简样例 | 看「三级 Builder 嵌套」的真实写法 |

> 提示：Basic 层对应的是外部 SDK 头文件 `kernel_basic_intf.h`，它来自 CANN 安装目录（见 4.2 节），**不在本仓库内**，所以本讲的源码地图里没有它的本地路径——这一点本身就是理解 Basic 层的关键。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

- **4.1 分层架构的动机与整体地图**——为什么要分五层、调用方向是怎样的。
- **4.2 `atvoss.h`：一个头文件串联五层**——主入口头文件与各层的对应关系。
- **4.3 三级 Builder 嵌套与 Host 侧调用链**——Device/Kernel/Block 三层如何一层包一层、`Run()` 如何往下走。
- **4.4 Tile 与 Basic 层：从表达式到 Ascend C**——最底两层如何把抽象表达式翻译成硬件动作。

### 4.1 分层架构的动机与整体地图

#### 4.1.1 概念说明

如果直接用 Ascend C 写一个「多核、分块、带流水同步」的 Vector 算子，你需要同时操心四件事：

1. **多核切分**：这批数据怎么分给几十个核？
2. **Tiling 分块**：每个核的 UB 装不下，怎么切成小块循环？
3. **搬运与同步**：GM↔UB 的搬运和计算要重叠起来（流水线），搬运完要发同步信号，否则数据竞争。
4. **计算本身**：每个元素到底做什么运算（加、乘、开方……）。

这四件事纠缠在一起，正是「裸写 Ascend C」复杂度高的根源。ATVOSS 的核心思路是：**把 1、2、3 封装成分层框架，只把 4 留给开发者用表达式描述**。

于是有了自顶向下的五层划分，抽象程度从高到低递减：

| 层级 | 职责 | 运行位置 | 谁来写 |
|------|------|---------|--------|
| **Device 层** | Host 侧总入口：参数校验、ACL 资源、切分计算、Kernel 启动 | Host（CPU） | 框架（开发者只调用 `Run`） |
| **Kernel 层** | 多核任务分解，控制 Block 调度 | Device（每个核都执行同一份 Kernel） | 框架 |
| **Block 层** | 单核内切成多个 Tile，编排搬运/计算流水 | 单核内 | 框架 |
| **Tile 层** | 封装 Ascend C API，提供搬运/计算的高层动作 | 单核内 | 框架 |
| **Basic 层** | 直接使用 Ascend C 基础 API | 单核内 | Ascend C SDK |

> 注意「运行位置」这一列：**Device 层跑在 Host CPU 上，其余四层都跑在 NPU 的 AI Core 上**。这是五层里最重要的一条物理边界——后面你会看到 `DeviceAdapter` 用 `aclrtStream` 把任务「发射(launch)」到设备上。

#### 4.1.2 核心流程

五层之间的关系可以用「调用方向」和「抽象方向」两条线来记：

```
        调用方向（运行时，自顶向下）
        ┌──────────────────────────────────────────────┐
        │                                              ▼
   ┌─────────┐   ┌────────┐   ┌───────┐   ┌──────┐   ┌───────┐
   │ Device  │──▶│ Kernel │──▶│ Block │──▶│ Tile │──▶│ Basic │
   │ (Host)  │   │(多核)   │   │(单核)  │   │(动作) │   │(Asc C)│
   └─────────┘   └────────┘   └───────┘   └──────┘   └───────┘
   参数校验/启动   核间切分     Tile切分/流水  搬运/计算动作   硬件API
        │                                              │
        └──────────── 抽象方向（设计上，自底向上）──────────┘
                  Basic 提供能力，上层逐级封装
```

要点：

- **调用方向**：上层「拥有」下层，调用下层。Device 决定启动多少核（blockNum），Kernel 决定每个核做多少，Block 决定每个 Tile 做多少，Tile 把每个动作翻译成 Basic 的 API 调用。
- **抽象方向**：Basic 层（Ascend C）是最底层的能力来源，Tile 封装它，Block 编排它，Kernel 调度它，Device 统领它。开发者站在最顶端，只描述「要算什么」。

一次完整的算子执行，数据流大致是：

```
Host 内存 ──(aclMemcpy)──▶ GM ──(DataCopy, Kernel/Block层驱动)──▶ UB ──(Vector计算)──▶ UB(结果) ──▶ GM ──▶ Host
```

而「谁负责把数据从 GM 搬到 UB」「谁负责切分成 Tile」「谁负责多核均衡」，正是 Kernel/Block/Device 三层各司其职的地方。

#### 4.1.3 源码精读

官方概述里对五层的权威描述是一张表，建议你先读它建立印象：

> ATVOSS 采用层次化的架构设计，从高到低分为五层，每层职责清晰，抽象程度逐步递减……
> 这种分层架构使得开发者可以专注于计算逻辑描述，而无需关注底层的硬件细节和并行调度策略。

完整表格见 [docs/summary.md:20-32](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/summary.md#L20-L32)，这里把它翻译成中文要点对照（原文 [docs/summary.md:22-28](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/summary.md#L22-L28)）：

| 层级 | 官方职责定位 |
|------|------------|
| Device 层 | Host 侧调用总入口：参数校验、ACL 资源管理、Host↔Device 数据管理、切分计算、Workspace 管理、Kernel 调用 |
| Kernel 层 | Kernel 函数总入口：多核间任务分解，控制 Block 调度 |
| Block 层 | 单核任务分解：将任务分解到多个 Tile 块，控制数据搬运/计算流水编排 |
| Tile 层 | Ascend C 封装：封装基础 API，提供大 Tile 块的搬运、计算能力 |
| Basic 层 | 基础操作：使用 Ascend C 基础 API 完成数据搬运计算 |

每一层的展开说明在 [docs/summary.md:34-72](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/summary.md#L34-L72)，其中点名了三个「核心组件」：

- Kernel 层核心组件是 `KernelBuilder<BlockOp>`（[docs/summary.md:51](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/summary.md#L51)）。
- Block 层核心组件是 `BlockBuilder<Compute>`（[docs/summary.md:60](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/summary.md#L60)）。
- Tile 层核心组件是各种 `Assign` 函数（如 `AddAssign`、`SqrtAssign`）（[docs/summary.md:69](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/docs/summary.md#L69)）。

这三个名字会贯穿后续所有讲义，现在先记住「`KernelBuilder`、`BlockBuilder`、`Assign`」即可。

#### 4.1.4 代码实践

**实践目标**：不看答案，凭理解画出五层架构图。

**操作步骤**：

1. 关掉本讲义，拿出一张纸。
2. 画 5 个方框，从上到下依次写 Device / Kernel / Block / Tile / Basic。
3. 在每个方框右侧标注它「跑在哪」（Host 还是 NPU 核内）。
4. 用箭头标出「调用方向」。
5. 在最下方画一条数据流：Host 内存 → GM → UB → UB(结果) → GM → Host。

**需要观察的现象**：画完后，你应该能一眼看出「Device 是唯一跑在 Host 上的层」。

**预期结果**：你的图应与本讲 4.1.2 的两张图一致。如果某层标注不出「跑在哪」，回看 4.1.1 的表格。

#### 4.1.5 小练习与答案

**练习 1**：如果把「多核切分」这一职责从 ATVOSS 里抽掉，让开发者自己写，会带来什么麻烦？

> **参考答案**：开发者必须自己在算子里根据核 ID（`GetBlockIdx`）计算「我这块核处理 GM 的哪一段」，还要处理总元素数不能被核数整除的尾数情况。这正是 Kernel 层 `MakeScheduleConfig` 自动完成的事（详见 u2-l8）。抽掉它，每个算子都要重复写这段容易出错的切分逻辑。

**练习 2**：Device 层和 Kernel 层都「调度任务」，它们的调度对象有什么不同？

> **参考答案**：Device 层调度的是「整个算子任务在 Host 与 Device 之间的生命周期」（资源、搬运、启动多少核），它决定 `blockNum`（启动几个核）；Kernel 层调度的是「设备上多核之间」的工作量分配（每个核算多少、偏移在哪），它在设备内、核间切分。

---

### 4.2 `atvoss.h`：一个头文件串联五层

#### 4.2.1 概念说明

ATVOSS 是 **header-only（仅头文件）** 库：没有 `.cpp`、没有预编译库，用户只要 `#include "atvoss.h"` 就能用全部能力。这意味着 `atvoss.h` 就是「整个框架的目录首页」——它决定了五层的代码按什么顺序、以什么形态进入用户的编译单元。

打开 `atvoss.h` 你会发现它极短，几乎只有 5 个 `#include`。这 5 个 include **一一对应五层**（其中 Basic 层对应一个外部 SDK 头）。看懂这 5 行，就掌握了五层的「文件入口」。

#### 4.2.2 核心流程

`atvoss.h` 的组织逻辑是「自底向上 include」：先包含最底层的 Basic 能力，再逐层向上，最后到 Device：

```
kernel_basic_intf.h   ← Basic 层（Ascend C SDK，外部）
elewise/tile/*        ← Tile 层（依赖 Basic）
elewise/block/*       ← Block 层（依赖 Tile）
elewise/kernel/*      ← Kernel 层（依赖 Block）
elewise/device/*      ← Device 层（依赖 Kernel + acl）
```

由于 C++ 头文件按出现顺序处理，这种排列保证了「上层用到下层符号时，下层已经声明」。也正因如此，用户只要 include 一个 `atvoss.h`，整条依赖链就自动拉进来。

#### 4.2.3 源码精读

`atvoss.h` 的全部正文就是这几行（[include/atvoss.h:13-17](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/atvoss.h#L13-L17)）：

```cpp
#include "kernel_basic_intf.h"            // Basic 层：Ascend C 基础 API 声明
#include "elewise/tile/tile_evaluate.h"   // Tile 层：表达式执行入口
#include "elewise/block/builder.h"        // Block 层：BlockBuilder
#include "elewise/kernel/builder.h"       // Kernel 层：KernelBuilder
#include "elewise/device/device_adapter.h"// Device 层：DeviceAdapter
```

逐行对应：

| `atvoss.h` 的 include | 所属层 | 本地仓库内？ |
|----------------------|--------|------------|
| `kernel_basic_intf.h`（[L13](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/atvoss.h#L13)） | **Basic** | ❌ 外部，来自 CANN SDK |
| `elewise/tile/tile_evaluate.h`（[L14](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/atvoss.h#L14)） | **Tile** | ✅ |
| `elewise/block/builder.h`（[L15](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/atvoss.h#L15)） | **Block** | ✅ |
| `elewise/kernel/builder.h`（[L16](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/atvoss.h#L16)） | **Kernel** | ✅ |
| `elewise/device/device_adapter.h`（[L17](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/atvoss.h#L17)） | **Device** | ✅ |

**关于 Basic 层的外部头**：`kernel_basic_intf.h` 在本仓库里找不到——用 `find` 搜索整个仓库都没有。它来自 CANN 安装目录。看构建脚本就能确认 include 路径来源（[cmake/CMakeASCEND.cmake:52-60](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/cmake/CMakeASCEND.cmake#L52-L60)）：

```cmake
set(ASCEND_INCLUDE_DIRS
    ${ASCEND_DIR}/include
    ${ASCEND_DIR}/compiler/tikcpp/include
    ${ASCEND_DIR}/compiler/ascendc/include/basic_api/impl
    ${ASCEND_DIR}/compiler/ascendc/include/basic_api/interface
    ...
)
```

也就是说，Basic 层就是 `${ASCEND_DIR}` 下 Ascend C 的 `basic_api`——`DataCopyPad`、`Adds`、`Sqrt` 这些底层函数全由 SDK 提供。ATVOSS 在此之上做封装，**性能上限与原生 Ascend C 一致**，这正是 u1-l1 反复强调的「零运行时开销」的由来。

> 一个验证小技巧：本仓库里所有出现 `AscendC::DataCopyPad`、`AscendC::Adds` 的地方，调用的都是 Basic 层能力。在 4.4 节你会看到 Tile 层如何包装它们。

#### 4.2.4 代码实践

**实践目标**：亲手验证「5 个 include ↔ 5 层」的对应关系。

**操作步骤**：

1. `Read` 或用编辑器打开 `include/atvoss.h`。
2. 对 4 个本地 include（tile/block/kernel/device），分别打开确认文件存在，并记录它们的命名空间：
   - `elewise/tile/tile_evaluate.h` → 命名空间 `Atvoss::Ele::Tile`
   - `elewise/block/builder.h` → 命名空间 `Atvoss::Ele`
   - `elewise/kernel/builder.h` → 命名空间 `Atvoss::Ele`
   - `elewise/device/device_adapter.h` → 命名空间 `Atvoss`
3. 对 Basic 层的 `kernel_basic_intf.h`，执行 `find . -name kernel_basic_intf.h`，确认本仓库内确实没有，并记下它需要靠 `ASCEND_INCLUDE_DIRS` 提供。

**需要观察的现象**：第 3 步的 `find` 应当没有任何输出（找不到文件），这印证了「Basic 层不在仓库内」。

**预期结果**：你得到一张与 4.2.3 表格一致的「include → 层 → 命名空间」对照表。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `atvoss.h` 里 `device_adapter.h` 放在最后 include，而 `kernel_basic_intf.h` 放在最前？

> **参考答案**：因为依赖方向是「Device 依赖 Kernel，Kernel 依赖 Block，Block 依赖 Tile，Tile 依赖 Basic」。C++ 头文件按顺序处理，先 include 底层，能保证上层引用下层符号时它已被声明。反过来写会报「未定义类型」。

**练习 2**：如果用户的机器上没有安装 CANN（`ASCEND_HOME_PATH` 未设置），编译会在哪一层失败？

> **参考答案**：在 **Basic 层**失败。`#include "kernel_basic_intf.h"` 找不到文件（include 路径 `ASCEND_INCLUDE_DIRS` 不存在），预处理阶段就报错，根本到不了上层。这也说明 Basic 层是整个框架的硬性地基。

---

### 4.3 三级 Builder 嵌套与 Host 侧调用链

#### 4.3.1 概念说明

光知道「五层」还不够，关键要理解它们**怎么连起来**。ATVOSS 用一个很优雅的设计：**把下层当作上层的模板参数**，一层包一层，最终用一个最外层的类型代表「整个算子」。

具体来说，从 abs 样例能看到三级嵌套：

```
BlockOp  = BlockBuilder<Compute, ...>          // Block 层，带「计算表达式」
KernelOp = KernelBuilder<BlockOp, ...>         // Kernel 层，把 BlockOp 包进去
DeviceOp = DeviceAdapter<KernelOp>             // Device 层，把 KernelOp 包进去
```

注意：`BlockBuilder` 的第一个模板参数是用户写的 `Compute`（计算表达式），这是**开发者唯一真正填写的内容**；从 `KernelOp` 往上的组装是固定样板。而 `Tile 层` 和 `Basic 层` 没有出现在这个嵌套里——它们不是「Builder」，而是被 Block 层在执行时**驱动**的（4.4 节讲）。

> 「Builder 模式 + 模板嵌套」是 ATVOSS 的骨架。本讲只看「三层怎么包、Run 怎么往下走」，每层内部的 `Schedule`（调度策略）留到 u2-l7~u2-l9。

#### 4.3.2 核心流程

一次 `deviceOp.Run(arguments, stream)` 的完整自顶向下调用链：

```
[Host]  DeviceOp.Run(arguments, stream)                         ← Device 层
          │
          ├─ ToLinearizerExpr(ExprMaker{}.Compute<Tensor>())    把用户表达式编译成内部表示
          ├─ PrepareParams<Params>(argTuple)                    构造每个 Param 的设备张量
          ├─ CalculateTiling<KernelOp>(arguments, opParam)      计算切分（见下）
          │     ├─ KernelOp::ScheduleClz::MakeScheduleConfig()  → kernelParam（多核切分）
          │     └─ BlockOp::ScheduleClz::MakeScheduleConfig()   → blockParam（单核 Tile 切分）
          └─ LaunchKernelWithDataTuple<KernelOp>(blockNum,...)  把任务「发射」到设备
                │
                ▼  KernelCustom<<<blockNum, stream>>>(cfg, args)   ← __global__ Kernel 函数
[Device]      └─ KernelOp.Run(cfg, args...)                      ← Kernel 层（每个核都跑一份）
                  └─ KernelSchedule.Run(cfg, args...)
                       └─ BlockOp.Run(blockParam, args)          ← Block 层（单核内）
                            └─ BlockSchedule.Run → Process(Tile 循环)
                                 └─ Tile::Evaluate<Expr>(context) ← Tile 层（见 4.4）
```

两个关键观察：

1. **`CalculateTiling` 一口气算两层切分**：它在 Host 上同时算出 `kernelParam`（启动几个核、每核多少）和 `blockParam`（单核切几个 Tile、尾块多少），然后把两者塞进同一个 `opParam` 传给设备。
2. **跨过 Host/Device 边界只有一处**：`KernelCustom<<<blockNum, stream>>>` 是 `<<<...>>>` 三尖括号语法（昇腾版 kernel 启动），`blockNum` 个核各自从 `KernelOp.Run` 开始执行同一份代码——这是从 Host 进入 NPU 的唯一跳板。

#### 4.3.3 源码精读

先看 abs 样例里真实的三级嵌套写法（[examples/abs/abs.cpp:40-44](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L40-L44)）：

```cpp
using BlockOp  = Atvoss::Ele::BlockBuilder<AbsCompute, ArchTag, blockPolicy, Atvoss::Ele::DefaultBlockConfig>;
using KernelOp = Atvoss::Ele::KernelBuilder<BlockOp, kernelPolicy>;
using DeviceOp = Atvoss::DeviceAdapter<KernelOp>;
```

- `BlockOp`（[L40](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L40)）：把用户写的 `AbsCompute`（计算表达式）交给 Block 层。
- `KernelOp`（[L42](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L42)）：把 `BlockOp` 作为模板参数塞进 Kernel 层。
- `DeviceOp`（[L44](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L44)）：把 `KernelOp` 塞进 Device 层，得到最终类型。

接着看 Device 层的 `Run`，它就是 4.3.2 那条链的 Host 段（[include/elewise/device/device_adapter.h:97-124](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/device_adapter.h#L97-L124)）：

```cpp
template <typename Args>
int64_t Run(const Args& arguments, aclrtStream stream = nullptr)
{
    auto expr = ToLinearizerExpr(ExprMaker{}.template Compute<Tensor>());   // L100 编译表达式
    using Expr = typename decltype(expr)::Type;
    using Params = Atvoss::Params_t<Expr>;

    auto argTuple = std::get<0>(arguments);
    auto params = PrepareParams<Params>(argTuple);                          // L106 构造参数

    OpParam opParam;
    if (!CalculateTiling<KernelOp>(arguments, opParam)) { ... }             // L110 切分

    auto convertArgs = ConvertArgs<Params>(params, argTuple);
    LaunchKernelWithDataTuple<KernelOp>(opParam.kernelParam.blockNum, stream, opParam, convertArgs); // L121 启动
    return 0;
}
```

三步 `PrepareParams → CalculateTiling → LaunchKernelWithDataTuple` 一目了然。

`CalculateTiling` 不在 `DeviceAdapter` 内部，而是一个独立函数（[include/elewise/device/tiling.h:16-30](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/tiling.h#L16-L30)），它清楚地展示了「Device 串联 Kernel 与 Block 两层切分」：

```cpp
template <typename KernelOp, typename Args>
bool CalculateTiling(const Args& args, typename KernelOp::ScheduleCfgClz& cfg)
{
    using BlockOp = typename KernelOp::ScheduleClz::BlockTemplate;
    if (!KernelOp::ScheduleClz::MakeScheduleConfig(args, cfg.kernelParam)) { ... }   // L20 Kernel 切分
    if (!BlockOp::ScheduleClz::MakeScheduleConfig(args, cfg.kernelParam, cfg.blockParam)) { ... } // L25 Block 切分
    return true;
}
```

注意 L25：Block 的切分**接收 Kernel 的切分结果 `cfg.kernelParam` 作为输入**——因为「单核要切多少 Tile」必须先知道「这个核分到了多少元素」。这就是层级之间数据依赖的真实样子。

跨边界启动的部分（[include/elewise/device/device_adapter.h:59-70](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/device_adapter.h#L59-L70)）：

```cpp
template <class KernelOp, typename OpParam, typename ArgTup>
void LaunchKernelWithDataTuple(uint32_t blockNum, aclrtStream& stream, OpParam& cfg, const ArgTup& argTuple)
{
    ...
    KernelCustom<KernelOp, OpParam><<<blockNum, nullptr, stream>>>(cfg, transformedArgs);   // L69 三尖括号启动
}
```

而 `KernelCustom` 是带 `__global__ __aicore__` 标记的 Kernel 函数（[include/elewise/device/device_adapter.h:36-42](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/device_adapter.h#L36-L42)），它内部调用 `KernelOp.Run`——自此进入 Kernel 层。

到了 Kernel 层，`KernelBuilder::Run` 非常薄，只是把活儿交给 `ScheduleClz`（[include/elewise/kernel/builder.h:58-66](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/builder.h#L58-L66)）：

```cpp
template <typename OpParam, typename... Args>
__aicore__ inline void Run(OpParam& cfg, Args... args)
{
    ScheduleClz schedule;
    schedule.Run(cfg, args...);     // 交给 KernelSchedule，再下到 Block
}
```

类似地，Block 层的 `BlockBuilder::Run` 也是转交给自己的 `ScheduleClz`（[include/elewise/block/builder.h:52-58](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/builder.h#L52-L58)）。于是「Builder::Run → Schedule::Run」是每一层共有的 delegation 模式，记住它，看 u2 时会轻松很多。

#### 4.3.4 代码实践

**实践目标**：把 4.3.2 的调用链与真实代码逐行对上。

**操作步骤**：

1. 打开 `include/elewise/device/device_adapter.h`，定位 `Run`（L97）。
2. 在 `Run` 体内找到三个调用点，填下表：

   | 步骤 | 代码片段 | 所在行 |
   |------|---------|-------|
   | 编译表达式 | `ToLinearizerExpr(...)` | L100 |
   | 构造参数 | `PrepareParams<Params>(...)` | L106 |
   | 切分 | `CalculateTiling<KernelOp>(...)` | L110 |
   | 启动 | `LaunchKernelWithDataTuple<KernelOp>(...)` | L121 |

3. 打开 `include/elewise/device/tiling.h`，确认 `CalculateTiling` 里先调 Kernel 的 `MakeScheduleConfig`（L20）、再调 Block 的（L25）。
4. 打开 `include/elewise/kernel/builder.h` 的 `Run`（L58），确认它只是 `schedule.Run(cfg, args...)`。

**需要观察的现象**：每一层的 `Run` 都是「转交给 Schedule」，唯独 Device 层的 `Run` 多做了「编译表达式 + 切分 + 启动」三件 Host 专属的事。

**预期结果**：你能对着代码讲出「从 `deviceOp.Run` 到 `BlockOp.Run` 中间经过了哪些函数」，且每一步都指得出行号。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Block 的 `MakeScheduleConfig` 需要拿 Kernel 的 `kernelParam` 当输入，而 Kernel 的 `MakeScheduleConfig` 不需要 Block 的结果？

> **参考答案**：因为切分顺序是「先核间、后核内」。Kernel 层先算出每个核分到多少元素（`unitNumPerCore` 等），Block 层再根据「本核分到的量」决定切几个 Tile。依赖是单向的：Block 依赖 Kernel，反之不成立。这也呼应了 4.1 说的「上层决定下层的工作量」。

**练习 2**：三级嵌套里，**用户**实际贡献的是哪个模板参数？

> **参考答案**：只有 `BlockBuilder<AbsCompute, ...>` 里的 `AbsCompute`（计算表达式结构体）。其余参数（`ArchTag`、`Policy`、`Config`、`Schedule`）都有默认值或固定写法。换句话说，五层架构里真正「因算子而异」的只有用户表达式，其余都是可复用的框架代码。

---

### 4.4 Tile 与 Basic 层：从表达式到 Ascend C

#### 4.4.1 概念说明

4.3 讲到 Block 层会「循环驱动 Tile」。本节看最底两层如何把抽象表达式翻译成硬件动作。

- **Tile 层**：不构建数据，而是**解释执行**用户的计算表达式。Block 每处理一个 Tile 块，就把当前 Tile 的上下文（数据指针、元素数、缓冲 ID）交给 Tile 层的 `Evaluate`，由它把表达式里的每个操作（搬运、加、乘……）翻译成对应动作。
- **Basic 层**：Tile 层翻译出的每个动作，最终是一次 Ascend C API 调用（如 `DataCopyPad`、`Adds`）。这一层没有 ATVOSS 代码，是 SDK 直接提供的。

打个比方：表达式是一份菜谱（「把食材搬上台、切丝、下锅、装盘」），Tile 层是照着菜谱一步步做的厨师，Basic 层是厨师手里的刀和锅（Ascend C API）。

#### 4.4.2 核心流程

Tile 层的执行模型是「递归求值」。表达式在编译期被组织成一棵类型树（这棵树长什么样是 u2-l1、u3-l1 的内容），`Tile::Evaluate` 从树根开始，递归地对每个节点调用对应的求值器特化，直到叶子（输入/输出张量）：

```
Block 层进入一个 Tile 循环
   │  构造 ContextData（gmOffset / elementNum / pingPong 等）
   ▼
Tile::Evaluate<Expr>(context)
   │
   ▼
Evaluator<Expr>{}(Expr, context)        ← 对整棵表达式树递归
   │  遇到 OpCopyIn  → Evaluator<OpCopyIn>  → AscendC::DataCopyPad  (GM→UB)
   │  遇到 OpAdd 等   → Evaluator<OpAdd>     → AscendC::Adds / ...   (UB 内计算)
   │  遇到 OpCopyOut → Evaluator<OpCopyOut> → AscendC::DataCopyPad  (UB→GM)
   ▼
一个 Tile 块的搬运+计算+回搬完成
```

这里出现了本讲唯一需要记住的「执行机制」名词：**Evaluator 求值器特化**。每种操作（`OpCopyIn`/`OpAdd`/`OpCopyOut`…）都对应一个 `Evaluator<该操作>` 的特化版本，里面写的就是「这个操作要调哪个 Ascend C API」。整棵树靠递归把它们串起来。

#### 4.4.3 源码精读

Tile 层的执行入口极其简短（[include/elewise/tile/tile_evaluate.h:35-40](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tile_evaluate.h#L35-L40)）：

```cpp
template <typename Expr, typename Context>
__aicore__ inline void Evaluate(Context& context)
{
    Atvoss::Tile::Evaluator<Expr>{}(Expr{}, context);   // 用 Expr 的类型选求值器特化，递归求值
}
```

短短两行就是 Tile 层的「总调度」：它不关心表达式具体是什么，只把 `Expr` 这个**类型**交给 `Evaluator` 模板，由 C++ 的模板特化机制去匹配正确的执行代码。这就是「类型驱动执行」。

落到具体动作，看搬运类操作的求值器特化。`CopyIn`（GM→UB）和 `CopyOut`（UB→GM）的底层实现就是 Ascend C 的 `DataCopyPad`（[include/elewise/tile/tensor_evaluator.h:38-57](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L38-L57)）：

```cpp
template <typename T>
__aicore__ inline void CopyIn(AscendC::LocalTensor<T> dst, AscendC::GlobalTensor<T> src, uint64_t copyCnt)
{
    AscendC::DataCopyExtParams copyParams{1, static_cast<uint32_t>(copyCnt * sizeof(T)), 0, 0, 0};
    AscendC::DataCopyPadExtParams<T> padParams{false, 0, 0, 0};
    AscendC::DataCopyPad(dst, src, copyParams, padParams);          // L43 ← Basic 层 API
}

template <typename T>
__aicore__ inline void CopyOut(AscendC::GlobalTensor<T> dst, AscendC::LocalTensor<T> src, uint64_t copyCnt)
{
    AscendC::DataCopyExtParams copyParams{1, static_cast<uint32_t>(copyCnt * sizeof(T)), 0, 0, 0};
    AscendC::DataCopyPad(dst, src, copyParams);                     // L56 ← Basic 层 API
}
```

`AscendC::DataCopyPad` 前缀 `AscendC::` 正是 Basic 层（SDK）的命名空间。可以清楚看到：**Tile 层的搬运动作 = 对 Basic 层 API 的一层薄封装**。而 `Evaluator<OpCopyIn<T>>`（[tensor_evaluator.h:60-61](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L60-L61) 及之后）则会调用上面的 `CopyIn`，从而把「表达式里的搬运节点」连到「真正的硬件搬运」。

最后，所有这些动作都受硬件约束。`arch.h` 里写死了目标芯片的能力（[include/common/arch.h:28-31](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/common/arch.h#L28-L31)）：

```cpp
struct DAV_3510 {
    static constexpr uint32_t CORE_NUM = 56;        // 56 个 Vector 核
    static constexpr uint32_t UB_SIZE  = 240 * 1024;// 每核 UB 240 KB
};
```

这两行常量是分层的物理根因：`CORE_NUM=56` 决定了 Kernel 层 `blockNum` 的上限；`UB_SIZE=240KB` 决定了 Block 层一个 Tile 能切多大（切太大 UB 装不下）。**五层架构的全部调度，最终都是在这两个数字（以及类似硬件常量）的约束下做分配。**

> 顺带复习：`DAV_3510` 正是 u1-l2 里 SOC=`ascend950` 映射到的 `--npu-arch=dav-3510`。到这里，硬件型号、构建参数、架构常量三者就串起来了。

#### 4.4.4 代码实践

**实践目标**：在源码里亲眼看到「Tile 动作 → Basic API」的翻译。

**操作步骤**：

1. 打开 `include/elewise/tile/tile_evaluate.h`，确认 `Evaluate` 只有「调用 `Evaluator<Expr>`」这一步（L35-40）。
2. 打开 `include/elewise/tile/tensor_evaluator.h`，搜索 `AscendC::DataCopyPad`，确认 `CopyIn`（L43）和 `CopyOut`（L56）都落在它上面。
3. 打开 `include/common/arch.h`，记录 `CORE_NUM` 和 `UB_SIZE` 的值。
4. 用 `Grep` 在整个 `include/` 下搜索 `AscendC::`，观察哪些文件大量出现它——你会看到 Tile 层求值器（`tensor_evaluator.h`、`math_evaluator.h` 等）是 `AscendC::` 的「集散地」，这印证了「Tile 是 Basic 之上的封装层」。

**需要观察的现象**：第 4 步的搜索结果里，`AscendC::` 几乎只出现在 `elewise/tile/` 与 `operators/*_evaluator.h` 中，而 `device/`、`kernel/`、`block/` 的 `builder.h` 里几乎不直接调 `AscendC::`——因为它们负责调度，不直接做底层动作。

**预期结果**：你得到一个结论：「越往下层，`AscendC::` 出现越密集；最底层全是 `AscendC::`。」这正好反映了抽象层次的递减。

#### 4.4.5 小练习与答案

**练习 1**：`Tile::Evaluate` 的函数体里没有任何 `if/switch` 区分「这是加法还是开方」，它是怎么知道该调哪个 API 的？

> **参考答案**：靠 C++ 模板特化。`Evaluator<Expr>` 是主模板，针对每种具体操作（如 `OpAdd`、`OpSqrt`、`OpCopyIn`）都有独立的特化版本。编译器根据 `Expr` 的**类型**在编译期就选好了该调哪个特化，运行时不存在分支判断——这就是 u1-l1 说的「编译期构建、零运行时开销」的具体含义。

**练习 2**：如果某天芯片升级，UB 从 240KB 变成 512KB，五层里哪一层最先受影响？

> **参考答案**：**Block 层**最直接受影响，因为它依据 `UB_SIZE` 决定 Tile 能切多大（更大的 UB 允许更大的 TileShape，减少 Tile 循环次数）。Kernel 层间接受影响（单次处理量可能变）。Device 与 Tile 层基本不受影响。`UB_SIZE` 改一行常量，Block 的切分行为自动跟着变——这正是把硬件约束集中到 `arch.h` 的好处。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这张「贯通任务」：

**任务**：为 ATVOSS 的五层架构产出一份「一页纸档案」，要求包含三张互相印证的图/表。

1. **架构图**：画出 Device → Kernel → Block → Tile → Basic 的五层方框图，标注：
   - 每层跑在哪（Host / NPU 核内）；
   - 调用方向箭头；
   - 每层的核心组件名（`DeviceAdapter` / `KernelBuilder` / `BlockBuilder` / `Tile::Evaluate` / Ascend C API）。

2. **头文件对应表**：列出 `atvoss.h` 的 5 个 `#include`，对应到五层，并标注「本地仓库 / 外部 SDK」。要求逐行与 [include/atvoss.h:13-17](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/atvoss.h#L13-L17) 对齐。

3. **调用链时序**：写出 `deviceOp.Run(arguments, stream)` 从 Host 到 Ascend C 的关键步骤顺序，每一步注明对应的源码位置，至少覆盖：
   - 表达式编译（[device_adapter.h:100](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/device_adapter.h#L100)）
   - 两层切分（[tiling.h:20](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/tiling.h#L20) 与 [tiling.h:25](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/tiling.h#L25)）
   - 跨边界启动（[device_adapter.h:69](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/device_adapter.h#L69)）
   - 表达式执行（[tile_evaluate.h:35-40](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tile_evaluate.h#L35-L40)）
   - 落到 Basic API（[tensor_evaluator.h:43](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L43)）

**自检标准**：如果有人指着你的架构图问「Block 层为什么受 `UB_SIZE` 约束？」你能用 4.4.3 里 `arch.h` 的常量回答；问「Basic 层代码在哪？」你能回答「不在仓库，来自 CANN SDK 的 `kernel_basic_intf.h`」——那么本讲就过关了。

> 本实践是「源码阅读型实践」，不需要真机运行。如果你已在 u1-l2 配好环境，可额外用 `Grep` 工具验证「`AscendC::` 主要集中在 tile/operators 目录」这一结论。

## 6. 本讲小结

- ATVOSS 自顶向下分 **Device / Kernel / Block / Tile / Basic** 五层，抽象程度递减，目的是让开发者只描述计算逻辑、把多核切分/Tiling/流水同步交给框架。
- **Device 层跑在 Host**，其余四层跑在 NPU 的 AI Core 内；`KernelCustom<<<blockNum>>>` 是跨 Host/Device 边界的唯一跳板。
- `include/atvoss.h` 用 5 个 `#include` 一一对应五层；其中 Basic 层的 `kernel_basic_intf.h` 来自 **CANN SDK 外部头**，不在本仓库。
- 三级 Builder 嵌套 `BlockOp → KernelOp → DeviceOp`，用户真正填写的只有 `Compute`（计算表达式），其余是可复用框架。
- `DeviceAdapter::Run` 走 `PrepareParams → CalculateTiling → LaunchKernel` 三步；`CalculateTiling` 在 Host 上一次性算出 Kernel 与 Block 两层切分，且 Block 依赖 Kernel 的结果。
- 最底两层用「递归求值 + 模板特化」把表达式翻译成 Ascend C API（如 `DataCopyPad`），全部受 `arch.h` 里 `CORE_NUM=56`、`UB_SIZE=240KB` 这类硬件常量约束。

## 7. 下一步学习建议

本讲建立了五层的「骨架」，接下来建议：

- **紧接着读 u1-l4《从 abs 样例看用户编程模型》**：亲手写一个 `Compute` 表达式，把本讲看到的「三级 Builder 嵌套」实际敲一遍，建立「我写的东西落在 Block 层」的肌肉记忆。
- **再读 u1-l5《算子运行时执行流程：ACL 与 Device 调用》**：补全本讲故意略过的 Host 侧 ACL 样板（`aclInit`/`aclrtMalloc`/`Memcpy`），看清 `DeviceOp.Run` 外围是怎么「喂数据」的。
- 进阶篇（U2）会逐层下钻：u2-l7 讲 Device、u2-l8 讲 Kernel 多核、u2-l9 讲 Block Tile——到那时再回看本讲的调用链图，你会发现自己已能填出每一层 `Schedule::Run` 的内部细节。
- 专家篇（U3）讲 Tile 内部的求值器系统（u3-l1）、DAG 构建（u3-l3）、表达式线性化（u3-l4）——那是「表达式树如何被递归求值」的完整真相，正好接上本讲 4.4 埋下的伏笔。

> 建议把本讲的「五层架构图」保留下来，后续每一讲读到某一层时，都在图上高亮对应方框，逐步把抽象地图变成详细地图。
