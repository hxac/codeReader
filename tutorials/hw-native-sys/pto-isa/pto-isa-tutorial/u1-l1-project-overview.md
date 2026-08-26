# u1-l1 项目全景：PTO Tile Library 是什么

## 1. 本讲目标

本讲是整本学习手册的第一讲，不要求任何 Ascend 或 PTO 背景。学完后你应该能够：

1. 用一句话说清 PTO ISA 的定位——一个**跨 Ascend 代际的 Tile 级虚拟 ISA**，以及它为什么存在。
2. 列出 PTO 指令集的 11 个大类和每类的作用，并知道去哪个文件查权威清单。
3. 读懂 `include/README.md` 里的指令支持状态表，说出 CPU / Costmodel / A2 / A3 / A5 / Kirin 六个后端列的含义。
4. 分清 PyPTO、TileLang Ascend、PTOAS、pto-dsl 四个生态项目各自扮演的角色。

本讲不写任何内核代码，任务只有一个：**把项目的"地图"装进脑子里**。从下一讲（u1-l2）开始才动手跑 CPU 模拟器。

## 2. 前置知识

本讲只需要几个通用概念，用通俗语言解释如下：

- **ISA（Instruction Set Architecture，指令集架构）**：硬件向软件暴露的"操作菜单"。你写程序时调用的每一条指令，最终都要落到某套 ISA 上。例如 x86 CPU 有 x86 ISA，Ascend NPU 有自己的算子开发 ISA。
- **虚拟 ISA（virtual ISA）**：不直接对应某一块物理硬件，而是人为定义一层"中间指令集"。上层代码先写成虚拟 ISA，再由各后端把它落实到具体硬件。好处是换硬件时上层代码不用重写——这正是 PTO 解决问题的抓手。
- **Tile（块/瓦片）**：一小块二维数据（例如 64×64 的矩阵），是 PTO 的基本编程单位。与传统"逐元素"思维不同，PTO 的一条指令一次处理一整个 Tile。
- **Ascend 代际**：华为昇腾 NPU 的不同世代，本项目关心 A2（Ascend 910B）、A3（Ascend 910C）、A5（Ascend 950）。不同代际的底层指令并不完全相同，这是迁移成本的主要来源。
- **header-only 模板库**：整个库几乎只有 `.hpp` 头文件，没有 `.cpp`。你 `#include` 它之后，指令实现通过 C++ 模板在**编译期**展开到你的代码里。
- **后端（backend）**：同一份公共 API 背后的一套具体实现。PTO 有 CPU 模拟后端、NPU 后端、CostModel 后端等多套，后面 4.3 节会展开。

## 3. 本讲源码地图

本讲涉及的文件以文档为主，它们本身就是"权威源码"：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目门面：定位、特性、快速开始、平台支持、路线图、生态链接 |
| `docs/README.md` | 文档总入口：推荐阅读路径与文档分类导航 |
| `docs/PTOISA.md` | **自动生成的 ISA 总索引**：全部指令按类别排列的权威清单 |
| `include/README.md` | 公共头文件说明 + **逐指令后端支持状态表**（本讲最重要的表） |
| `include/pto/README.md` | `include/pto/` 内部模块布局（common / cpu / npu / comm） |
| `docs/isa/comm/README.md` | 通信扩展指令集的入口与分组 |

一个重要的阅读习惯从这里开始建立：**PTO 的"事实"分散在几个权威文件里**——项目定位看根 README，指令清单看 `docs/PTOISA.md`，某条指令在某个平台上能不能用看 `include/README.md` 的状态表。三份文件都要会查。

## 4. 核心概念与源码讲解

### 4.1 模块一：项目定位

#### 4.1.1 概念说明

PTO 全称 **Parallel Tile Operation（并行块操作）**，是 Ascend CANN 定义的面向 Tile 编程的虚拟 ISA。本仓库（PTO Tile Library）提供的是这套虚拟 ISA 的**指令实现、示例、测试与文档**。

它解决的核心问题是：**同一份算子代码如何平滑地跑在不同代的 Ascend 芯片上**。传统做法是针对每代芯片的底层指令各写一遍，迁移成本高；PTO 的做法是抬高抽象层次——让上层用统一的 Tile 指令编程，由各后端负责落实到具体硬件。

特别注意 README 里一句很关键的话：PTO 的目标**不是隐藏底层能力**，而是在抬高抽象的同时保留性能调优空间（tile 尺寸、tile 形状、指令排布顺序仍是程序员可调的）。这决定了 PTO 的定位介于"高层框架"和"汇编程语言"之间。

#### 4.1.2 核心流程

PTO 在整个技术栈中的位置可以画成：

```text
上层框架 / 算子 / 编译器前端
        │  (PyPTO / TileLang Ascend / PTOAS / pto-dsl ...)
        ▼
┌─────────────────────────────────────────────┐
│  PTO 虚拟 ISA（90+ 条标准 Tile 指令）          │   ← 本仓库定义的抽象层
│  公共 API：include/pto/common/               │
└─────────────────────────────────────────────┘
        │ 按编译宏分发到不同后端
        ├──────────────┬──────────────┬──────────────┐
        ▼              ▼              ▼              ▼
   CPU 模拟后端    NPU a2a3 后端   NPU a5 后端    CostModel 后端
  (__CPU_SIM)    (A2/A3 910B/C)  (A5 950)      (__COSTMODEL)
```

理解这条"一份抽象、多套实现"的主线，后面所有讲义都在讲它的某个侧面。

#### 4.1.3 源码精读

**① PTO 的定义句**。[README.md:7](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/README.md#L7) 用一句话给出定位：PTO 是 Ascend CANN 定义的面向 Tile 编程的虚拟 ISA，本仓库提供指令实现、示例、测试与文档，帮助开发者在不同 Ascend 代际之间更顺畅地迁移和优化算子。

**② 四条设计原则**。[README.md:21-28](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/README.md#L21-L28) 是"Project Positioning"小节，列出：

- 统一的跨代 Tile 抽象 → 降低代际迁移成本；
- 平衡可移植性与性能 → 固定 tile 形状下保证行为正确，同时保留 tile 尺寸/形状/指令顺序等调优维度；
- 面向框架、算子与工具链 → 作为上层各方的公共接口；
- 持续可扩展 → 目前定义 90+ 条标准操作，仍在继续实现与生态集成。

**③ 通信扩展也是一等公民**。[README.md:30-32](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/README.md#L30-L32) 说明 PTO 除了计算与搬运指令，还提供**通信扩展指令集**（NPU 间数据传输与同步），覆盖点对点、信号同步与集合通信，并与计算指令使用同一套 Tile 抽象——这为"计算通信深度融合"的算子（如 GEMM+AllReduce）打下基础。

**④ 它是一个头文件模板库**。[include/README.md:3](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/README.md#L3) 说明本目录是"主要以 header-only、模板方式提供的公共 C/C++ 头文件"，上层代码包含它们即可发出 PTO Tile 级操作。统一入口只需一行：

```cpp
#include <pto/pto-inst.hpp>
```

（见 [include/README.md:9-13](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/README.md#L9-L13)。这个头如何按宏选后端，是 u1-l5 的主题。）

**⑤ 官方推荐学习路径**。[README.md:95-100](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/README.md#L95-L100) 给出四步：从简单示例理解 Tile 级计算与搬运的组织方式 → 在 CPU 模拟器上验证功能与正确性 → 移植到 Ascend 硬件收集性能数据 → 识别瓶颈（CUBE Bound / MTE Bound / Vector Bound）并优化。整本手册的单元顺序（u1-u2 跑起来 → u3-u6 读实现 → u7-u8 性能与生态）就是照这条路径展开的。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：把"PTO 是什么"从一句口号变成你能复述的三句话。
2. **操作步骤**：
   - 通读 [README.md:21-47](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/README.md#L21-L47)（Project Positioning + Core Features 两节）。
   - 再读 [docs/README.md:16-25](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/README.md#L16-L25) 的推荐阅读顺序。
   - 用自己的话写下三句话：① PTO 给谁用；② 它解决什么问题；③ 它不打算做什么（提示：不打算隐藏底层调优能力）。
3. **需要观察的现象**：注意 Core Features 里 "Auto / Manual dual-mode workflow" 与 "CPU Simulator support" 两条——它们预告了手册后面的 Auto Mode（u7-l1）和 CPU 模拟器（u1-l2）。
4. **预期结果**：你能不看资料说出"PTO = 跨 Ascend 代的 Tile 级虚拟 ISA，本仓库是它的头文件实现 + 文档 + 测试"。
5. 本实践为纯阅读，无"待本地验证"项。

#### 4.1.5 小练习与答案

**练习 1**：PTO 说自己"不是要隐藏底层能力"，这句话和"虚拟 ISA 抬高抽象层次"矛盾吗？

**参考答案**：不矛盾。"抬高抽象"指上层代码用统一的 Tile 指令描述计算，不必针对每代芯片重写；"不隐藏底层能力"指 tile 尺寸、tile 形状、片上内存规划（TASSIGN）、事件同步与指令排布这些影响性能的自由度仍然交给程序员。前者解决可移植性，后者保留性能上限，两者是同一设计的两面。

**练习 2**：本仓库的物理形态是什么？`.cpp` 多还是 `.hpp` 多？

**参考答案**：主要以 header-only 模板头文件形态存在，`include/` 下几乎全是 `.hpp`。指令实现通过 C++ 模板在编译期展开进用户代码（见 `include/README.md` 开头说明）。

### 4.2 模块二：指令分类

#### 4.2.1 概念说明

PTO 定义了 90+ 条标准 Tile 指令（见 [README.md:23](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/README.md#L23)）。权威清单在 [docs/PTOISA.md](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/PTOISA.md)，它由 `docs/isa/manifest.yaml` 同步生成，按 11 个类别组织。

先解释几个类别名里的术语：

- **Elementwise（逐元素）**：对两个 Tile 的对应元素做运算，形状不变，如 `TADD`。
- **Tile-Scalar（块-标量）**：一个 Tile 与一个标量运算，指令名常以 `S` 结尾（如 `TADDS` = tile add scalar）。
- **Axis Reduce / Expand（轴向规约/扩展）**：沿行或列压缩（`TROWSUM` 每行求和）或反向广播（`TROWEXPAND` 把每行首元素铺满一行）。
- **Matrix Multiply（矩阵乘）**：走 Cube 单元的 `TMATMUL` 家族，与走 Vector 单元的 elementwise 相对。
- **Memory（GM ↔ Tile）**：全局内存与片上 Tile 之间的搬运，如 `TLOAD` / `TSTORE`。
- **MTE**：Memory Transfer Engine，搬运流水线；**Cube / Vector** 是两类计算单元——这三者在性能分析（CUBE/MTE/Vector Bound）中会反复出现。

#### 4.2.2 核心流程

指令类别的"职责分工"可以按数据流串起来：

```text
GM（全局内存）
 │  Memory 类: TLOAD / MGATHER        ← 搬入
 ▼
Tile（片上缓冲，UB/L1/L0）
 │  Data Movement / Layout 类: TMOV / TTRANS / TEXTRACT / TIMG2COL ...   ← 重排
 ▼
计算
 ├─ Elementwise / Tile-Scalar 类: TADD / TMUL / TADDS ...   ← Vector 单元
 ├─ Axis Reduce / Expand 类: TROWSUM / TROWEXPAND ...        ← 规约与广播
 └─ Matrix Multiply 类: TMATMUL / TMATMUL_ACC / TGEMV ...    ← Cube 单元
 ▼
 │  Memory 类: TSTORE / TSTORE_FP / MSCATTER   ← 搬出
 ▼
GM
   （旁路）Manual / Resource Binding: TASSIGN / SETFMATRIX ...   ← 资源绑定与配置
   （旁路）Synchronization: SYNCALL                              ← 跨核栅栏
   （旁路）Complex: TSORT32 / TQUANT / THISTOGRAM ...            ← 复杂/专用
   （旁路）Cross-core Communication: TALLOC / TPUSH / TPOP       ← Cube-Vector 核间队列
   （旁路）Communication: TPUT / TGET / TREDUCE / TNOTIFY ...    ← NPU 间通信
```

#### 4.2.3 源码精读

**① 总索引的结构**。[docs/PTOISA.md:17-27](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/PTOISA.md#L17-L27) 是"Instruction Index (All PTO Instructions)"的开头：三列表格（类别 / 指令 / 说明），前三行分别是 `SYNCALL`（跨核同步栅栏）和 `TASSIGN`、`SETFMATRIX` 等"Manual / Resource Binding"类指令——后者负责把 Tile 绑定到片上地址、配置 FMATRIX 等专用寄存器。

**② 逐元素类的规模**。[docs/PTOISA.md:28-56](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/PTOISA.md#L28-L56) 是 Elementwise (Tile-Tile) 类，共 29 条，从 `TADD`、`TSUB`、`TMUL` 这类算术，到 `TAND`/`TOR`/`TXOR` 位运算，再到 `TEXP`/`TLOG`/`TSQRT` 数学函数和 `TSEL`（按掩码逐元素选择）。

**③ 搬运与矩阵乘类**。[docs/PTOISA.md:104-110](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/PTOISA.md#L104-L110) 是 Memory (GM ↔ Tile) 类：`TLOAD` 从 GlobalTensor 装入 Tile，`TSTORE` 写回，`TSTORE_FP` 走带缩放参数的 Fixpipe 路径，`TPREFETCH`/`TPREFETCH_ASYNC` 是预取提示，`MGATHER`/`MSCATTER` 是按索引的收集/散布。[docs/PTOISA.md:111-119](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/PTOISA.md#L111-L119) 是 Matrix Multiply 类：`TMATMUL` 家族（含带累加输入的 `_ACC`、带偏置的 `_BIAS`、带缩放 tile 的 `_MX` 低精度变体）和 `TGEMV` 家族。

**④ 通信类的四个子方向**。[docs/isa/comm/README.md:21-36](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/isa/comm/README.md#L21-L36) 把通信指令分成三组：异步点对点（`TPUT_ASYNC` / `TPUT_ASYNC_NOTIFY` / `TGET_ASYNC`，走 DMA 引擎直达）、信号同步（`TNOTIFY` / `TWAIT` / `TTEST`）、集合通信（`TGATHER` / `TSCATTER` / `TREDUCE` / `TBROADCAST`）。加上同步点对点的 `TPUT` / `TGET`，共四个子方向。[docs/PTOISA.md:159-169](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/PTOISA.md#L159-L169) 的 Communication 类逐条给出了这 11 条指令的一句话语义，例如 `TGET` 是"读远端 NPU 数据到本地（GM → UB → GM）"，`TREDUCE` 是"收集所有 rank 的数据并逐元素规约到本地"。

**⑤ 各类指令数量（本讲已实际统计验证）**。对 [docs/PTOISA.md](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/PTOISA.md) 的指令索引逐行统计，当前 HEAD 共 **149 条**指令条目：

| 类别 | 数量 | 代表指令 |
| --- | ---: | --- |
| Elementwise (Tile-Tile) | 29 | `TADD` `TMUL` `TSEL` |
| Axis Reduce / Expand | 28 | `TROWSUM` `TCOLMAX` `TROWEXPAND` |
| Complex | 20 | `TSORT32` `TQUANT` `TGATHER` |
| Tile-Scalar / Tile-Immediate | 19 | `TADDS` `TMULS` `TEXPANDS` |
| Data Movement / Layout | 15 | `TMOV` `TTRANS` `TEXTRACT` |
| Communication | 11 | `TPUT` `TGET` `TREDUCE` |
| Matrix Multiply | 9 | `TMATMUL` `TMATMUL_ACC` `TGEMV` |
| Memory (GM ↔ Tile) | 7 | `TLOAD` `TSTORE` `MGATHER` |
| Manual / Resource Binding | 6 | `TASSIGN` `SETFMATRIX` |
| Cross-core Communication | 4 | `TALLOC` `TPUSH` `TPOP` |
| Synchronization | 1 | `SYNCALL` |
| **合计** | **149** | |

README 中"90+ 条"是定位性的下限表述；索引里包含配置类与通信类指令，因此总数更高。两个数字都对，口径不同。

#### 4.2.4 代码实践（命令验证型）

1. **实践目标**：亲手验证上表的数量，而不是背下来。
2. **操作步骤**：在仓库根目录执行：

   ```bash
   # 统计每个类别在 docs/PTOISA.md 指令索引中出现的行数
   grep -oP '^\| [A-Za-z/ -]+ \| \[' docs/PTOISA.md | sort | uniq -c | sort -rn

   # Elementwise 与 Memory 两个类别名里含括号，单独数
   grep -c '^| Elementwise' docs/PTOISA.md
   ```

3. **需要观察的现象**：输出中 `Axis Reduce / Expand` 28 条、`Complex` 20 条、`Tile-Scalar / Tile-Immediate` 19 条……与上表一致；另外会混入几行 `Overview`、`ISA reference` 等，那是文档开头的"Docs Contents"导航表，不是指令，应剔除。
4. **预期结果**：11 个类别合计 149 条（剔除导航行后）。若你数出的总数不是 149，说明可能把导航行也算进去了，或 HEAD 已更新——以后者为准，这正是"索引是生成物、会随版本增长"的体现。
5. 本实践只读文件、不构建，Linux/macOS 可直接运行；Windows 需在 Git Bash/WSL 下执行。

#### 4.2.5 小练习与答案

**练习 1**：`TADD` 和 `TADDS` 有什么区别？从命名规律出发再举一组同类例子。

**参考答案**：`TADD` 是 Tile 与 Tile 的逐元素加，`TADDS` 是 Tile 与标量（Scalar）加，后缀 `S` 表示标量操作数。同类例子：`TMUL`/`TMULS`、`TMAX`/`TMAXS`、`TSUB`/`TSUBS`、`TAND`/`TANDS`。（命名约定的系统讲解见 `docs/isa/conventions.md`，u4-l1 展开。）

**练习 2**：要把一个 Tile 从全局内存搬进来、转置、再搬出去，分别用哪类指令？

**参考答案**：搬入用 Memory 类的 `TLOAD`（GM → Tile），转置用 Data Movement / Layout 类的 `TTRANS`，搬出用 Memory 类的 `TSTORE`（Tile → GM）。三步之间还需要事件同步保证顺序（u3-l1 主题）。

**练习 3**：`TMATMUL` 与 `TMATMUL_MX` 的差别是什么？

**参考答案**：`TMATMUL` 是标准 GEMM（矩阵乘产生累加器/输出 Tile）；`TMATMUL_MX` 是带额外缩放 Tile 的混合精度/量化矩阵乘变体，主要服务于 A5 上的 MX 低精度数据格式（如 MXFP4/MXFP8），u5-l6 会专门讲。

### 4.3 模块三：平台支持矩阵

#### 4.3.1 概念说明

"一条指令"和"这条指令在某个后端可用"是两回事。PTO 的公共 API 在 `include/pto/common/` 声明，但**每条指令在每个后端各有独立的实现状态**。`include/README.md` 中的状态表就是逐指令 × 逐后端的可用性矩阵。

六个后端列的含义：

| 列 | 对应实现目录 / 宏 | 说明 |
| --- | --- | --- |
| CPU | `__CPU_SIM`，`include/pto/cpu/` | 跨平台 CPU 模拟后端，x86_64 / AArch64，用于功能验证 |
| Costmodel | `__COSTMODEL`，`include/pto/costmodel/` | A2/A3 性能模型后端（含 `stub` 与 `fit` 两条路径，任一支持即标支持） |
| A2 / A3 | `include/pto/npu/a2a3/` | Ascend 910B / 910C，**共用同一套实现**，两列状态恒相同 |
| A5 | `include/pto/npu/a5/` | Ascend 950，独立实现 |
| Kirin | `include/pto/npu/kirin9030/` | Kirin 9030 实现 |

状态取值有四种（见 [include/README.md:172-177](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/README.md#L172-L177)）：

- `Yes`：该后端已有可用实现；
- `TODO`：指令已进入公共 API 或文档面，但该后端实现尚未完成/尚未集成；
- `No`：明确不支持或暂无计划；
- 留空：状态尚未定稿或仍在评审。

#### 4.3.2 核心流程

查表的标准流程：

```text
我想用指令 X
   │
   ▼
打开 include/README.md 状态表，找到 X 那一行
   │
   ├─ 目标后端列 = Yes   → 可以直接用
   ├─ 目标后端列 = TODO  → 公共 API 存在但该后端没实现：
   │                       换等价指令组合，或参与贡献实现（u8-l2）
   ├─ 目标后端列 = No    → 该后端明确不支持（如 CPU 列的 TPARTMUL、TRANDOM）
   └─ 留空              → 状态未定稿，谨慎使用
   │
   ▼
若不确定语义 → 点该行链接跳转 docs/isa/<指令>.md 单页参考
```

注意 CPU 列与 NPU 列**不是子集关系**：多数指令 CPU=Yes 可先行验证，但存在 CPU=TODO 而 A5=Yes 的指令（如 `TGEMV`、`TIMG2COL`、`TQUANT`），也存在 CPU=Yes 而 A2/A3=No 的指令（如 `THISTOGRAM`，见 [include/README.md:85](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/README.md#L85)）。查表不能想当然。

#### 4.3.3 源码精读

**① 表头与后端说明**。[include/README.md:24-35](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/README.md#L24-L35) 先解释六列分别绑定哪个宏、哪个目录，并特别说明 A2/A3 共用 `include/pto/npu/a2a3/` 实现，所以两列状态永远一致；随后是表头 `| Instruction | CPU | Costmodel | A2 | A3 | A5 | Kirin |`。

**② 以 TADD 为例读一行**。[include/README.md:39](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/README.md#L39)：

```text
| TADD | Yes | Yes | Yes | Yes | Yes | Yes |
```

含义：`TADD` 在全部六个后端都有可用实现——CPU 模拟器可验证，Costmodel 可建模，A2/A3/A5/Kirin 都能真跑。这就是它被选为"第一个教学指令"（u1-l4）的原因。

**③ 反例：CPU 侧尚缺的指令**。[include/README.md:79](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/README.md#L79) 的 `TGEMV` 行是 `TODO | TODO | Yes | Yes | Yes | Yes`：NPU 各代都支持，但 CPU 模拟与 Costmodel 还没实现——想在 CPU 上先验证 GEMV 算子逻辑 presently 做不到，只能上真机或改用 `TMATMUL` 等价表达。[include/README.md:126](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/README.md#L126) 的 `TQUANT` 同理，且它是 A5 专属能力。

**④ 反例：NPU 侧不支持而 CPU 支持的指令**。[include/README.md:85](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/README.md#L85) 的 `THISTOGRAM` 行是 `Yes | TODO | No | No | Yes | Yes`：A2/A3 明确标 `No`，A5 与 Kirin 支持。这说明矩阵必须逐行查，不能按列推断。

**⑤ 后端目录的物理布局**。[include/pto/README.md:17-29](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/README.md#L17-L29) 描述了与状态表列一一对应的目录：`common/` 是平台无关的 Tile 类型系统与指令声明（`pto_tile.hpp`、`pto_instr.hpp`），`cpu/` 是 CPU 模拟，`npu/a2a3/` 与 `npu/a5/` 按代际拆分，`comm/` 是通信指令库（含平台分发层 `pto_comm_instr_impl.hpp`）。状态表中任意一个 `Yes`，背后都能在对应目录找到一个同名头文件。

#### 4.3.4 代码实践（查表型）

1. **实践目标**：熟练使用支持矩阵回答"指令 X 能不能在后端 Y 上用"。
2. **操作步骤**：
   - 打开 [include/README.md:34-170](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/README.md#L34-L170) 的状态表。
   - 查 `TADD` 行，记下 CPU 与 A5 两列的值。
   - 再各找一条满足下列条件的指令并记下行号：① CPU=`TODO` 且 A5=`Yes`（提示：`TGEMV`、`TIMG2COL`、`TQUANT`、`TCONCAT`、`TCOLARGMAX` 均满足）；② A2/A3=`No` 而 CPU=`Yes`（提示：`THISTOGRAM`）。
   - 对找到的每条指令，点行内链接读它的 `docs/isa/*.md` 单页，确认语义与你猜的一致。
3. **需要观察的现象**：A2 与 A3 两列在任何一行都完全相同（因为共用实现目录）；`Yes/TODO/No` 三种取值都真实存在；表是按指令名字典序排列的，可以用编辑器的查找功能快速定位。
4. **预期结果**：`TADD` 在 CPU 列与 A5 列均为 `Yes`；你至少找到两条 "CPU=TODO 且 A5=Yes" 的指令和一条 "A2/A3=No" 的指令。
5. 本实践纯查表，无"待本地验证"项。

#### 4.3.5 小练习与答案

**练习 1**：为什么 A2 和 A3 两列永远相同？

**参考答案**：因为 Ascend A2（910B）与 A3（910C）目前共用同一套实现目录 `include/pto/npu/a2a3/`，状态表按目录记录实现状态，两列自然一致（见 `include/README.md` 表前的说明）。

**练习 2**：`TODO` 和 `No` 的区别是什么？对你写代码分别意味着什么？

**参考答案**：`TODO` 表示指令已在公共 API/文档中定义，只是该后端的实现尚未完成或未集成——未来可能变 `Yes`，且其他后端可能已可用；`No` 表示明确不支持或暂无计划。写代码时遇到 `TODO`：换等价指令组合绕过，或等待/贡献实现（u8-l2 讲贡献链路）；遇到 `No`：不要指望该后端，必须改方案。

**练习 3**：你想在笔记本电脑上用 CPU 模拟器验证一个用到了 `TGEMV` 的算子，会发生什么？

**参考答案**：行不通。`TGEMV` 的 CPU 列是 `TODO`，CPU 模拟后端没有它的实现。可行的替代是用 CPU 已支持的 `TMATMUL`（CPU 列为 `Yes`）等价表达矩阵-向量乘，先验证算法逻辑，再在 NPU 上换回 `TGEMV`。

### 4.4 模块四：生态集成

#### 4.4.1 概念说明

PTO-ISA 仓库是整个 PTO 生态的"指令实现底座"，它的直接使用者不是终端业务开发者，而是上层框架与工具链。生态里的四个关键角色：

| 项目 | 角色 | 与本仓库的关系 |
| --- | --- | --- |
| **PyPTO** | PTO 生态的上层编程框架 | 已集成 PTO 指令，把 PTO 能力暴露给 Python 侧使用 |
| **TileLang Ascend** | Tile 语言前端的 Ascend 后端 | 已集成 PTO 指令，用 TileLang 写的调度可落到 PTO |
| **PTOAS** | PTO 汇编器与编译器后端 | 处理 PTO 工作流的汇编/编译，对应 Roadmap 中的 PTO-AS 字节码方向 |
| **pto-dsl** | Pythonic 前端与 JIT 工作流探索 | 探索用 Python 直接驱动 PTO 的开发方式 |

另外注意区分两个"通信"概念：`Cross-core Communication`（`TPUSH`/`TPOP` 等，**单 NPU 内 Cube 核与 Vector 核之间**通过 TPipe FIFO 传数据）与 `Communication`（`TPUT`/`TGET` 等，**NPU 与 NPU 之间**的跨设备传输）。它们都在生态叙事里常被混称，但在指令分类上是两类。

#### 4.4.2 核心流程

生态分层的调用关系：

```text
                 ┌───────────────┐  ┌───────────────┐
                 │    PyPTO      │  │ TileLang      │
                 │ (Python 框架) │  │ (调度语言前端) │
                 └───────┬───────┘  └───────┬───────┘
                         │    已集成 PTO 指令 │
                 ┌───────┴──────────────────┴───────┐
                 │   PTO-ISA（本仓库）               │
                 │   公共 API + 各后端实现            │
                 └───────┬──────────────────┬───────┘
                         │                  │
                 ┌───────┴───────┐  ┌───────┴───────┐
                 │  PTOAS        │  │  pto-dsl      │
                 │  汇编器/编译后端│  │ Pythonic 前端  │
                 └───────────────┘  └───────────────┘
```

Roadmap（[README.md:186-197](https://github.com/hw-native-sys-pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/README.md#L186-L197)）里还能看到生态的演进方向：PTO Auto Mode（BiSheng 编译器自动分配 tile 缓冲与插入同步）、PTO Tile Fusion（自动融合）、PTO-AS（字节码）、卷积/集合通信/系统调度/微指令等 ISA 扩展，以及 CostModel 与 CPU-SIM 的持续同步。

#### 4.4.3 源码精读

**① 已集成 PTO 的框架**。[README.md:34-38](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/README.md#L34-L38) 明确列出 PyPTO 与 TileLang Ascend 两个已集成项目，并注明"更多语言与前端支持持续完善中"。

**② 生态链接与一句话定位**。[README.md:231-233](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/README.md#L231-L233) 在"Related Information"里给出三个外部项目的定位原文：

- PyPTO — "an upper-layer programming framework in the PTO ecosystem"（PTO 生态的上层编程框架）；
- PTOAS — "PTO assembler and compiler backend for PTO workflows"（PTO 汇编器与编译器后端）；
- pto-dsl — "Pythonic frontend and JIT workflow exploration for PTO"（Pythonic 前端与 JIT 工作流探索）。

**③ 目标用户画像**。[README.md:49-56](https://github.com/hw-native-sys-pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/README.md#L49-L56) 把本仓库的受众写成三类：直接对接 Ascend 硬件的框架/编译器后端开发者、需要跨平台迁移复用算子实现的高性能算子开发者、需要精确控制 tile/缓冲/流水线的性能工程师。这也解释了为什么生态项目（而非业务代码）是本仓库的第一批用户。

**④ 语言边界的例外**。虽然本仓库主体是 C++ 头文件，但 `kernels/python/` 提供了"Python 侧配置 + C++ caller"的驱动工作流，`demos/torch_jit/` 演示了即时编译运行——本仓库并非完全排斥 Python 入口（u8-l3 专题）。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：建立"本仓库 → 生态项目"的关系表，能对任何新同事说清四者的分工。
2. **操作步骤**：
   - 读 [README.md:34-38](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/README.md#L34-L38) 与 [README.md:231-233](https://github.com/hw-native-sys-pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/README.md#L231-L233)。
   - 读 Roadmap 表 [README.md:186-197](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/README.md#L186-L197)，标出哪些条目属于"编译器/工具链"、哪些属于"ISA 扩展"。
   - 用 `ls kernels/python/` 与 `ls demos/` 确认仓库内的 Python 入口确实存在。
3. **需要观察的现象**：Roadmap 表的 Scope 列只有三种取值（Compiler / toolchain、ISA extension、以及工具链与算子开发的组合），可以据此快速判断每项工作的归属层级。
4. **预期结果**：产出一张四行的小表（项目 / 一句话定位 / 与本仓库关系），例如 PTOAS 一行写"汇编器与编译器后端，对应 Roadmap 的 PTO-AS 字节码方向"。
5. 本实践的 `ls` 命令只读目录，无"待本地验证"项。

#### 4.4.5 小练习与答案

**练习 1**：PyPTO 和 pto-dsl 都是 Python 相关项目，它们定位差别在哪？

**参考答案**：PyPTO 是"PTO 生态的上层编程框架"，已经是正式集成 PTO 指令的框架；pto-dsl 是"Pythonic 前端与 JIT 工作流探索"，定位是探索性的（探索用 Python 直接驱动 PTO 的开发与即时编译方式），成熟度和定位都不同于 PyPTO。

**练习 2**：`TPUSH`/`TPOP` 和 `TPUT`/`TGET` 都叫"通信"，区别是什么？

**参考答案**：`TPUSH`/`TPOP` 属于 Cross-core Communication 类，通过 TPipe FIFO 在**同一 NPU 内部的 Cube 核与 Vector 核之间**传数据（配套 `TALLOC`/`TFREE` 管理 FIFO 槽位）；`TPUT`/`TGET` 属于 Communication 类，在**不同 NPU 设备之间**做远端写/远端读。前者是片内核间流水，后者是跨设备互连。

**练习 3**：Roadmap 中 "PTO Auto Mode" 要解决什么问题？这与你将在 u7-l1 学到的内容有什么关系？

**参考答案**：Auto Mode 由 BiSheng 编译器支持，自动完成 tile 缓冲分配与同步插入，让开发者不必手写 `TASSIGN` 与显式事件（Manual 模式的工作量正在于此）。u7-l1 会对比 `kernels/automode` 与 `kernels/manual` 的同源算子，展示被省掉的代码。

## 5. 综合实践

把本讲四个模块串成一张"项目全景速查表"。这是本讲唯一必做产出，后续讲义会反复用到它。

**任务**：通读 [README.md](https://github.com/hw-native-sys-pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/README.md) 与 [docs/README.md](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/README.md)，然后制作两张表：

**表 A：指令类别 × 平台概览**。以 `docs/PTOISA.md` 的 11 个类别为行，包含四列：类别名、指令数量（用 4.2.4 的 grep 命令实测）、两个代表指令、该类别在你目标平台上的可用性抽查结果（每类挑 1 条代表指令，去 `include/README.md` 状态表查 CPU 与 A5 两列）。

参考格式（前两行已示例，请补全 11 行）：

| 类别 | 数量 | 代表指令 | 抽查指令 | CPU | A5 |
| --- | ---: | --- | --- | --- | --- |
| Elementwise (Tile-Tile) | 29 | TADD、TMUL | TADD | Yes | Yes |
| Matrix Multiply | 9 | TMATMUL、TGEMV | TGEMV | TODO | Yes |
| … | … | … | … | … | … |

**表 B：TADD 状态确认**。写下 `TADD` 在状态表中的完整一行（六个后端列），并回答：为什么教程第二讲敢直接从 TADD 的 CPU 模拟器用例开始？

**预期结果自查**：

- 表 A 每行数量之和 = 149（若 HEAD 更新导致不同，以你实测为准并注明）。
- 表 B 中 TADD 六列全为 `Yes`，因此 CPU 模拟器路径畅通，适合作为第一个教学指令。
- 全程只读文件与执行 `grep`/`ls`，不修改任何源码。

## 6. 本讲小结

- **PTO = Parallel Tile Operation**，是 Ascend CANN 定义的跨代际 Tile 级虚拟 ISA；本仓库是它的 header-only 模板实现 + 示例 + 测试 + 文档，目标是让算子在 A2/A3/A5 间平滑迁移，同时保留 tile 尺寸、内存规划、指令排布等调优自由度。
- 指令清单的权威来源是 `docs/PTOISA.md`，当前 HEAD 共 **149 条**指令、11 个大类：Elementwise 29、Axis Reduce/Expand 28、Complex 20、Tile-Scalar 19、Data Movement 15、Communication 11、Matrix Multiply 9、Memory 7、Resource Binding 6、Cross-core 4、Synchronization 1。
- 通信是两条线：`TPUSH/TPOP` 是 NPU **内部** Cube↔Vector 核间 FIFO；`TPUT/TGET/TREDUCE` 等是 NPU **之间**的点对点/信号/集合通信。
- 逐后端可用性看 `include/README.md` 状态表（`Yes`/`TODO`/`No`/留空）；A2 与 A3 共用 `npu/a2a3/` 实现故两列恒同；CPU 列与 NPU 列不是子集关系（`TGEMV` CPU=TODO 而 NPU 全 Yes；`THISTOGRAM` A2/A3=No 而 CPU=Yes）。
- 生态分工：PyPTO 是上层编程框架，TileLang Ascend 是调度语言前端，PTOAS 是汇编器/编译器后端，pto-dsl 是 Pythonic 前端探索；本仓库是它们共同的指令底座。
- 三个必须会查的位置：定位看根 `README.md`、指令清单看 `docs/PTOISA.md`、平台可用性看 `include/README.md`。

## 7. 下一步学习建议

下一讲 **u1-l2《环境搭建与 CPU 模拟器快速上手》**：配置 Python/CMake/C++20 环境，用 `python3 tests/run_cpu.py` 在 CPU 模拟器上跑通 GEMM 与 FlashAttention 两个 demo，理解 sim 模式与板端模式的区别——那将是你在本项目中第一次**运行**而非只阅读。

在进入下一讲前，建议先自己浏览一遍这两个文件，带着问题去：

- [docs/getting-started.md](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/getting-started.md)：环境要求与两条路径（CPU / NPU）的差异。
- [tests/README.md](https://github.com/hw-native-sys-pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/README.md)：测试入口与常用命令。

如果想提前建立对"Tile 到底长什么样"的直觉，可以翻阅 [docs/coding/Tile.md](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/coding/Tile.md)（tile 形状、掩码与数据组织）——那是 u2-l3 的主题，现在只需混个眼熟。
