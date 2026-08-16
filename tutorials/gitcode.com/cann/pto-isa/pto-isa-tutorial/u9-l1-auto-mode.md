# Auto Mode 双模式工作流

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 Auto 模式与 Manual 模式的分工边界：同一套 PTO 指令 API，Auto 模式把「Tile 缓冲摆放（TASSIGN）」与「流水线同步（TSYNC/Event）」两件最容易出错的样板收归编译器。
2. 掌握 `__PTO_AUTO__` 宏在头文件层的真实实现方式：Tile 的数据成员从「裸指针」变成「带 `tile_size` 的向量类型」、TASSIGN/TSYNC/Event 在 Auto 分支下被编译成空操作。
3. 掌握 kernel 开发者在 Auto 模式下的三类规则（控制流、内存分配、通用规则），理解每条规则背后「编译器分析能力的边界」。
4. 读懂 `demos/auto_mode/baseline/add` 示例，能对照 Manual 版列出 Auto 模式替你省掉的三类样板代码，并在有/无 NPU 两条路径上各完成一次验证。

> 先承接上一讲的结论（u3-l2）：Manual 模式下「分配」就是 TASSIGN 手工摆放地址，排布不重叠、不越界是开发者的第一责任；本讲正面回答它的反面——Auto 模式下这些责任由谁承担、以什么机制承担、开发者又让渡了哪些自由。

## 2. 前置知识

- **Manual 模式三件套**（回顾 u1-l4、u2-l3、u3-l2）：
  - `TASSIGN`：把整型片上偏移绑给 Tile（手工摆放 UB/L1/L0 地址）；
  - `set_flag`/`wait_flag` 或 `Event`/`TSYNC`：跨流水线（MTE2/V/MTE3/M…）的生产-消费依赖表达；
  - ping-pong 双缓冲：双份 Tile + 0/1 翻转 + 按槽配对的事件，实现搬运与计算重叠。
- **编译期后端路由**（回顾 u2-l4）：`pto-inst.hpp` 按 `__CPU_SIM` / `__CCE_AICORE__` / `__COSTMODEL` 三宏选择实现头文件集合。本讲引入与之**正交**的第四个宏 `__PTO_AUTO__`：它不选后端，而是在同一后端内切换「Manual/Auto 两种编程模式」。
- **Tile 的五族属性**（回顾 u2-l2）：位置（TileType）、元素类型、容量形状、布局、有效区。Auto 模式改变的是「位置」这一属性的获得方式——从 TASSIGN 手工指定变为编译器自动分配。
- **活跃区间（live range）**：一个变量从「第一次被定值」到「最后一次被使用」之间的程序区间。编译器寄存器分配、内存复用都建立在这个概念上——Auto 模式的缓冲分配也一样。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [docs/auto_mode/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/auto_mode/README.md) | Auto 模式入口文档：三大收益、TMUL 的 Manual/Auto 对照示例 |
| [docs/auto_mode/Auto_Mode_Overview.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/auto_mode/Auto_Mode_Overview.md) | Auto 模式总览：抽象层级边界、三大特性、编译开关 |
| [docs/auto_mode/Kernel_Developer_Rules_And_Limitations.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/auto_mode/Kernel_Developer_Rules_And_Limitations.md) | kernel 开发者规则与限制（本讲 4.2 节的主料） |
| [docs/auto_mode/Examples.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/auto_mode/Examples.md) | TADD/TMATMUL 的 Auto vs Manual 并排代码 |
| [include/pto/common/memory.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/memory.hpp) | `MemoryQualifier`：Auto 模式下 Tile 数据成员从指针类型变为向量类型的关键开关 |
| [include/pto/common/pto_tile.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp) | Tile 类本体：`tile_size` 向量声明、构造函数 `__cce_tinit`、禁用拷贝赋值、CPU 仿真懒分配 |
| [include/pto/npu/a2a3/TAssign.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TAssign.hpp) | NPU 端 TASSIGN 实现：Auto 分支下 Tile 绑定变空操作 |
| [include/pto/common/event.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/event.hpp) | 事件模型：`PtoSetWaitFlag` 与 `EventBase` 的 Auto 空操作分支 |
| [include/pto/npu/a2a3/TSync.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TSync.hpp) | 单流水线屏障 TSYNC 的 Auto 空操作分支 |
| [include/pto/npu/a2a3/TSubView.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TSubView.hpp) | TSUBVIEW 在 Auto 模式下的语义变化（`__cce_alias` 别名提示） |
| [demos/auto_mode/baseline/add/csrc/kernel/add_custom.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/auto_mode/baseline/add/csrc/kernel/add_custom.cpp) | 本讲主角：Auto 模式 Add kernel |
| [demos/baseline/add/csrc/kernel/add_custom.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp) | 对照组：Manual 模式 Add kernel（u1-l4 已精读） |
| [demos/auto_mode/baseline/add/CMakeLists.txt](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/auto_mode/baseline/add/CMakeLists.txt) | Auto 模式编译开关 `--cce-pto-enable --cce-pto-auto-enable -O2` |
| [demos/auto_mode/baseline/add/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/auto_mode/baseline/add/README.md) | 示例文档：目录结构、构建与运行步骤、三条注意事项 |
| [tests/cpu/st/testcase/tflashattn/CMakeLists.txt](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tflashattn/CMakeLists.txt) | CPU 仿真下开启 Auto 模式的 ST 用例（`add_compile_definitions(__PTO_AUTO__)`） |
| [tests/cpu/st/testcase/tflashattn/tflashattn_kernel.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tflashattn/tflashattn_kernel.cpp) | 无 TASSIGN、无事件的完整算子（Flash Attention 单块版），CPU 可运行 |
| [demos/cpu/gemm_demo/CMakeLists.txt](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/gemm_demo/CMakeLists.txt) | CPU demo 以 `__CPU_SIM __PTO_AUTO__` 双宏编译的证据 |
| [kernels/automode/a2a3/gemm/multiBuffer.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/automode/a2a3/gemm/multiBuffer.hpp) | 规则 1.4 提到的「双缓冲专用抽象」雏形（`pto_auto::MultiStaged`） |

## 4. 核心概念与源码讲解

### 4.1 Auto 模式总览：把两件样板从开发者手里收走

#### 4.1.1 概念说明

**Auto 模式是一种编译模式，不是一套新 API。** 官方定义非常直接：

> Auto mode is a compilation mode for PTO. It does all the memory allocation for tiles and synchronization in the compiler. Programming in auto mode works just like in manual mode, except there is no need for `TASSIGN` and `TSYNC`/`Event` (in fact, these will do nothing in auto mode).

拆开看，Auto 模式的目标是**用更高一层的接口抽象换开发效率，同时保住接近专家手工调优的性能**。它接管的两件事，恰好是 Manual 模式下最容易写错的两件：

| Manual 模式下你写 | Auto 模式下谁做 | 出错时的 Manual 典型症状 |
|---|---|---|
| `TASSIGN(tile, addr)` + 手工规划互不重叠的 UB/L1/L0 地址 | 编译器按 Tile 活跃区间自动分配地址 | 地址互踩、数据静默损坏、越界 |
| `set_flag`/`wait_flag`（或 `Event`/`TSYNC`）的精确配对 | 编译器自动判定插入位置 | 漏等牌 → 读到未就绪数据；漏挂牌 → 死等 |

对应地，Auto 模式的三大能力（后两大是用户可见收益，第一大是内部基础设施）：

1. **Tile 活跃区间分析**（Automated Tile Live Range Analysis）：跟踪每个 Tile 从生到死的区间，是后续两个能力的公共分析基础；
2. **自动同步**（Automatic Synchronization）：编译器在底层自动决定同步插入位置，保证功能正确且有竞争力的性能；
3. **Tile 内存分配**（Tile Memory Allocation）：声明 `Tile` 变量即自动获得缓冲地址，不再需要 `TASSIGN` 补一句。

还有一个容易忽略的收益：**跨架构兼容**。Cube 与 Vector 的协同方式在昇腾各代际间存在差异（A5/A6 与 A2A3 的事件与存储组织并不完全一致），Auto 模式把这些差异吸收进编译器，单份源码可在不同代际上编译并保持性能。

**关键边界：编译器只工作在 Tile Function（TF）层以上。** PTO 指令的标准抽象层级从高到低是：

```
User API（kernel 开发者调用的公共接口，如 TADD/TLOAD）
   │
IMPL 层 API（XXX_IMPL 契约检查与转发）
   │
TF（Tile Function）层 API        ←── PTO 编译器的工作下界
   │
内部 CCE 实现 API（VF、SIMT function 等原生 intrinsic）
```

一旦进入 tile function 内部，就只剩裸指针和原生 CCE intrinsic——那是 CCE 编译器的领地，对 PTO 编译器是完全的黑盒。所以**本讲所有 Auto 特性只在 TF 层以上生效**；你自己在 tile function 里手写的搬运/同步不受 Auto 模式保护。

#### 4.1.2 核心流程

同一份 kernel 源码在两种模式下的编译路径：

```
                    ┌──────────────────────────────────────────┐
                    │  kernel 源码（GlobalTensor + Tile + 指令） │
                    └──────────────────┬───────────────────────┘
                                       │
                    编译开关二选一（Bisheng CCE 工具链）
                    │                                       │
        Manual：--cce-pto-enable              Auto：--cce-pto-enable
        （不定义 __PTO_AUTO__）                 --cce-pto-auto-enable
                                               （编译器定义 __PTO_AUTO__，且必须 -O2）
                    │                                       │
        TASSIGN 有效（手工摆地址）               TASSIGN(tile,·) 编译为空操作
        set_flag/wait_flag 有效                 事件指令编译为空操作
        Tile.data_ 是片上存储指针               Tile.data_ 是 tile_size 向量类型
                    │                                       │
                    └──────────────┬───────────────────────┘
                                   ▼
                     昇腾硬件（AIV/AIC 多流水线并行执行）
```

注意三点：

1. **`__PTO_AUTO__` 宏不在本仓库任何头文件中定义**（可用 `grep -rn "define __PTO_AUTO__" include/` 验证，结果为空）。NPU 路径上它由 Bisheng 编译器在收到 `--cce-pto-auto-enable` 时预定义——与 `__CCE_AICORE__` 由 CCE 编译器自动预定义是同一机制（回顾 u2-l4）；CPU 仿真路径上则由构建脚本手工注入（见 4.2.4 实践）。
2. **Auto 模式目前只支持 `-O2`**。示例的 CMake 与官方文档都显式要求。
3. **模式选择发生在编译期**，与后端选择（CPU/NPU/CostModel）正交组合，运行期零开销——这与 u2-l4 讲的后端路由纪律一脉相承。

缓冲自动分配的原理可以用寄存器分配类比：设 Tile \(t\) 的活跃区间为 \( [d_t, l_t] \)（\(d_t\) 为首次定值点，\(l_t\) 为最后使用点），两个 Tile 的区间不重叠即可复用同一块片上地址。于是 Auto 模式下的分配问题近似为图着色：

\[
\text{overlap}(t_i, t_j) \iff [d_i, l_i] \cap [d_j, l_j] \neq \varnothing,\qquad
\sum_{t \,\in\, \text{peak live set}} \text{bytes}(t) \le \text{Cap}(\text{片上存储})
\]

这也直接解释了 4.2 节规则 3.1：活跃区间不重叠的两个 Tile 可能被分配到**同一地址**，此时对输出 Tile 补一次多余的 `TLOAD`，两次 `TLOAD` 就会互相覆盖。

#### 4.1.3 源码精读

**(1) 官方定义与三大能力**

[docs/auto_mode/Auto_Mode_Overview.md:7-18](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/auto_mode/Auto_Mode_Overview.md#L7-L18)：这段给出 Auto 模式的一句话定义（编译器完成 tile 内存分配与同步；`TASSIGN`/`TSYNC`/`Event` 在 Auto 模式下是空操作），并列出两大抽象——流水线间自动同步指令插入、Tile 抽象的内存分配管理。

[docs/auto_mode/Auto_Mode_Overview.md:24-33](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/auto_mode/Auto_Mode_Overview.md#L24-L33)：这段用加粗强调抽象层级边界——PTO 编译器工作在 Tile 层，**TF 接口是 Tile 抽象的最后一层**，进入 tile function 内部后只剩裸指针与原生 intrinsic，编译器不会深入其中。

[docs/auto_mode/Auto_Mode_Overview.md:35-47](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/auto_mode/Auto_Mode_Overview.md#L35-L47)：三个特性小节——活跃区间分析是服务后续特性的核心组件；自动同步免除事件模型负担；Tile 内存分配让「实例化 Tile 变量」本身就完成缓冲获取。

**(2) 编译开关**

[docs/auto_mode/Auto_Mode_Overview.md:49-70](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/auto_mode/Auto_Mode_Overview.md#L49-L70)：启用 Auto 模式只需在 Bisheng CCE 工具链加 `--cce-pto-enable --cce-pto-auto-enable`；示例命令展示了用 `bisheng -c -x cce -O2 --cce-aicore-only --cce-aicore-arch=...` 把单个 kernel 源文件编译成目标文件。

**(3) 头文件层机制之一：Tile 从指针变向量类型**

这是理解「为什么 TASSIGN 必须变空操作」的钥匙：

[include/pto/common/memory.hpp:26-33](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/memory.hpp#L26-L33)：`MemoryQualifier` 为每种 TileType 给出数据成员类型。以 Vec（UB）为例：Manual 模式下是 `__ubuf__ DType*`（一个可被 TASSIGN 重定向的指针）；**Auto 模式下是 `__ubuf__ DType`（不带指针）**。Mat/Left/Right/Acc 等其余 TileType 同样成对特化（[memory.hpp:35-69](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/memory.hpp#L35-L69)）。指针消失了，自然无处可「assign」。

向量类型还带上了容量声明：

[include/pto/common/pto_tile.hpp:1539-1554](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1539-L1554)：NPU 真机路径上，Auto 模式的 `TileDType` 是 `MemoryQualifier<...>::type tile_size(Rows * Cols)`——即「类型 + 大小」的向量类型声明，分配交给编译器；CPU 仿真/CostModel 路径则仍是指针（走懒分配，见下）。

[include/pto/common/pto_tile.hpp:1500-1503](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1500-L1503)：Auto 模式下 Tile 的拷贝赋值与移动赋值被 `= delete`——Tile 变量不能被整体搬动，编译器才能对「每个 Tile 变量 ↔ 一块稳定分配」做可靠分析。这是 4.2 节规则 2.3「把 Tile 当 C++ 引用看待」在类型系统层面的落实。

[include/pto/common/pto_tile.hpp:1456-1465](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1456-L1465)：Tile 默认构造函数在 Auto（非 CPU 仿真）分支下用 `__cce_tinit` 哑初始化数据成员——注释说明否则该成员会保持未定义状态、在编译器 SROA 优化后变成 undef 值。带运行期掩码的构造函数同样处理（[pto_tile.hpp:1468-1498](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1468-L1498)）。

**(4) 头文件层机制之二：TASSIGN / 事件 / TSYNC 空操作化**

[include/pto/npu/a2a3/TAssign.hpp:17-26](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TAssign.hpp#L17-L26)：NPU 端 `TASSIGN_IMPL` 的 Tile 分支被 `#ifndef __PTO_AUTO__` 包住——**Auto 模式下直接 `return`，什么都不做**。这也解释了向量类型为何必要：Manual 分支的 `obj.assignData(reinterpret_cast<TileDType>(addr))` 在向量类型下根本无法编译，必须整支屏蔽。注意紧随其后的 [TAssign.hpp:27-34](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TAssign.hpp#L27-L34) GlobalTensor 分支**没有** Auto 守卫——Auto 模式下 GM 视图地址仍可（也需要）用 TASSIGN 或重新构造视图来维护，这与 u3-l2 的结论一致。

[include/pto/common/event.hpp:358-370](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/event.hpp#L358-L370)：`PtoSetWaitFlag` 整个函数体被 `#ifndef __PTO_AUTO__` 包住——Auto 模式下编译为空函数，与编译器的自动同步互不冲突。对象风格的 `EventBase` 同样处理：

[include/pto/common/event.hpp:404-422](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/event.hpp#L404-L422)：`Wait()`/`Init()`（`Record()` 转发到 `Init()`）在 Auto 分支下直接返回自身，`set_flag`/`wait_flag` 一概不发。

[include/pto/npu/a2a3/TSync.hpp:31-42](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TSync.hpp#L31-L42)：单流水线屏障 `TSYNC_IMPL` 在 Auto 分支下不产生 `pipe_barrier`；[TSync.hpp:80-120](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TSync.hpp#L80-L120) 中 `Event` 的 `InitImpl`/`WaitImpl` 也整体被 `#ifndef __PTO_AUTO__` 屏蔽。a5/a6/kirin 后端的 TSync.hpp 结构相同。

**(5) CPU 仿真下的 Auto 路径**

[include/pto/common/pto_tile.hpp:1556-1568](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1556-L1568)：在 `__CPU_SIM && __PTO_AUTO__`（或 CostModel）下，`data()` 做懒分配——首次访问时 `internalBuffer.resize(Rows * Cols)` 并把指针交给 `data_`；私有成员声明见 [pto_tile.hpp:1697-1702](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1697-L1702)。也就是说，**CPU 仿真用宿主 std::vector 模拟「编译器已分好缓冲」的效果**，让 Auto 风格 kernel 无 NPU 也能验证功能。

[tests/cpu/st/testcase/tflashattn/CMakeLists.txt:10-11](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tflashattn/CMakeLists.txt#L10-L11)：`pto_cpu_sim_st(tflashattn)` 注册 CPU ST 用例，随后 `add_compile_definitions(__PTO_AUTO__)` 手工打开 Auto 宏——这就是 CPU 仿真路径启用 Auto 模式的标准写法。CPU demo 同理：[demos/cpu/gemm_demo/CMakeLists.txt:26](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/gemm_demo/CMakeLists.txt#L26) 以 `__CPU_SIM __PTO_AUTO__` 双宏编译。

#### 4.1.4 代码实践

**实践一：用 grep 丈量 Auto 模式在头文件里的渗透面**

1. **实践目标**：直观感受「Auto 模式不是一套平行 API，而是在原实现外面包 `#ifndef __PTO_AUTO__`」这一实现策略。
2. **操作步骤**：

   ```bash
   # 统计 __PTO_AUTO__ 在各层的出现次数
   grep -rc "__PTO_AUTO__" include/pto/common/ | grep -v ":0"
   grep -rc "__PTO_AUTO__" include/pto/npu/a2a3/ | grep -v ":0"
   grep -rc "__PTO_AUTO__" include/pto/cpu/    | grep -v ":0"
   # 确认它从未在仓库内被 define
   grep -rn "define __PTO_AUTO__" include/ demos/ tests/ kernels/
   ```

3. **需要观察的现象**：common 层命中集中在 `pto_tile.hpp`、`memory.hpp`、`event.hpp`、`pto_instr.hpp`、`syncall_soft.hpp`；npu 各架构目录命中集中在 `TSync.hpp`、`TAssign.hpp`、`TSubView.hpp`、`TReshape.hpp` 等「地址与同步类」指令；cpu 层几乎为零（CPU 仿真靠 Tile 懒分配兜底，无需逐指令分叉）；define 搜索无结果。
4. **预期结果**：三类替换（向量类型、同步空操作、别名指令语义变化）分布与你刚读过的 4.1.3 一一对应。

**实践二：在 CPU 仿真上跑一个「零 TASSIGN、零事件」的完整算子**

1. **实践目标**：不依赖 NPU 硬件，验证 Auto 风格 kernel（无 TASSIGN/无事件）在 CPU 仿真下功能正确。
2. **操作步骤**：

   ```bash
   python3 tests/run_cpu.py -t tflashattn --verbose
   ```

3. **需要观察的现象**：脚本先跑 `gen_data.py` 生成 golden，再编译（CMake 带 `__PTO_AUTO__`）并运行 gtest，输出 PASSED。
4. **预期结果**：用例通过。其 kernel（见 [tests/cpu/st/testcase/tflashattn/tflashattn_kernel.cpp:87-111](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tflashattn/tflashattn_kernel.cpp#L87-L111)，注释明言「No direct Tile memory assignment is made (via TASSIGN)」）从 TLOAD 到 TSTORE 共十几条指令，全程没有一次 TASSIGN 和事件——这就是 Auto 风格的样子。若本机缺 C++20 编译器或 numpy，参考 u1-l3 的环境准备。**待本地验证**（本讲义未替你执行）。

#### 4.1.5 小练习与答案

**练习 1**：`__PTO_AUTO__` 与 `__CPU_SIM`/`__CCE_AICORE__` 这组后端宏是什么关系？能否组合出「CPU 仿真 + Auto 模式」？

**答案**：二者正交。后端宏决定「同一指令用哪套实现头」（cpu/ 还是 npu/<arch>/），`__PTO_AUTO__` 决定「同一实现内 Manual/Auto 两个分支选哪个」。可以组合：CPU 仿真路径由构建脚本手工注入 `__PTO_AUTO__`（tflashattn 用例、CPU demos 都是 `__CPU_SIM __PTO_AUTO__`），此时 Tile 走懒分配、指令行为与 NPU Auto 分支对齐；NPU 路径则由 Bisheng 编译器配合 `--cce-pto-auto-enable` 自动预定义。

**练习 2**：为什么 Auto 模式下 `MemoryQualifier` 必须把 Tile 数据成员从 `__ubuf__ DType*` 换成 `__ubuf__ DType`（配合 `tile_size`）？

**答案**：Manual 模式的分配模型是「Tile 持有一个可重定向的指针，TASSIGN 改写它」；Auto 模式的分配模型是「Tile 声明即分配，地址终身不变」。把成员声明为带 `tile_size(Rows*Cols)` 的向量类型后，缓冲的存在性和大小成为类型系统的一部分，交给编译器统一规划，既没有可供用户改写的指针（从根上杜绝手工摆地址），也让活跃区间分析有确定的分配单位。这也正是 TASSIGN 的 Tile 分支必须整体 `return` 的原因——`assignData` 接收指针参数，在向量类型下无法编译。

**练习 3**：官方文档强调 Auto 特性「只在 TF 层以上生效」，这对写自定义 tile function 的开发者意味着什么？

**答案**：tile function 内部是 PTO 编译器的黑盒——里面手写的指针运算、intrinsic、同步不会被自动分配/自动同步覆盖，也不会被自动同步保护。若在 tile function 里访问了自动分配的 Tile 存储，或在其内部依赖了未经同步的跨流水线数据，正确性责任仍在开发者自己。所以 Auto 模式的 kernel 应尽量把工作表达为 TF 层以上的 PTO 指令序列；确需新能力时，向 pto-isa 申请新增 PTO 指令（见规则 3.2）而不是下探到 intrinsic。

### 4.2 开发者规则与限制：编译器分析能力的边界

#### 4.2.1 概念说明

Auto 模式不是「随便写都能高性能」。编译器要同时保证**正确性**与**可并行性**：当控制流复杂到无法精确分析跨流水线并行与双缓冲时，它只能退而求其次，生成更保守的同步——功能不坏，性能变差。官方把 kernel 开发者的约束整理成一份规则文档，并明确违反的三类后果：

1. 编译失败（源码层面报错或编译器崩溃）；
2. 功能不正确（如精度问题）；
3. 性能差。

理解这份文档的正确姿势：**每条规则都对应编译器某项分析的前提条件**。规则不是行政限制，而是「你把代码写成这个形状，我的分析才能证明安全」。

#### 4.2.2 核心流程

规则全景（编号沿用官方文档）：

| 类别 | 编号 | 一句话规则 | 保护的分析 |
|------|------|-----------|-----------|
| 控制流 | 1.1 | 循环首/末迭代的守卫条件要能静态求值 | 首尾迭代剥离（peeling），大幅简化自动同步 |
| 控制流 | 1.2 | 不依赖内层归纳变量的 if 应提到内层循环外 | 循环不变式外提 |
| 控制流 | 1.3 | 守卫 PTO 指令的复杂逻辑表达式先求值成 bool 再用 | 条件表达式静态化 |
| 控制流 | 1.4 | 暂不要用 double/multi buffering | 复杂控制流下的自动同步（专用抽象在设计中） |
| 内存 | 2.1 | 表达「两 Tile 同基地址」用 `TRESHAPE`，不要用 TASSIGN | 别名分析 |
| 内存 | 2.2 | 表达「Tile B 是 Tile A 加偏移的子视图」用 `TSUBVIEW` | 别名分析 |
| 内存 | 2.3 | Tile 的地址运行期不可变（一次分配、终身不变） | 地址稳定性 |
| 内存 | 2.4 | 一个 Tile 不能先后作为多个 TRESHAPE/TSUBVIEW 的目的地 | 别名关系唯一性 |
| 通用 | 3.1 | 不要对纯输出 Tile 补冗余 `TLOAD` | 活跃区间不重叠 ⇒ 地址复用 ⇒ 相互覆盖 |
| 通用 | 3.2 | 不要直接调 CCE intrinsic / Tile 的 `.data()` | Auto 下 Tile 是向量类型；分析只认 PTO 指令 |
| 通用 | 3.3 | 同步优先用 `PtoSetWaitFlag`/`TSYNC` 而非裸 `set_flag`/`wait_flag` | 这两个接口自带 Auto 空操作守卫 |

最核心的心智模型（官方原话的意译）：**把 Tile 当成 C++ 引用——一旦声明，其内存地址已定且不可更改**。Manual 模式里「一个 Tile 变量、多块地址轮转复用」的惯用法（Add 示例的 ping-pong 正是如此）在 Auto 模式下不再合法。

#### 4.2.3 源码精读

**(1) 控制流规则（1.1～1.4）**

[Kernel_Developer_Rules_And_Limitations.md:10-12](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/auto_mode/Kernel_Developer_Rules_And_Limitations.md#L10-L12)：开宗明义——复杂控制流（尤其是循环内）让精确的跨流水线并行与双缓冲分析变难，编译器为守住正确性会生成更保守的同步，性能受损。

[Kernel_Developer_Rules_And_Limitations.md:14-28](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/auto_mode/Kernel_Developer_Rules_And_Limitations.md#L14-L28)：规则 1.1，首/末迭代守卫写成可静态求值的形式（如 `tile_id == 0`、`tile_id == total_tiles-1`），编译器即可自动剥离首尾迭代，自动同步随之大幅简化。这个手法与 u6-l2 讲过的「首尾补同步（InitSyncFlags/WaitSyncFlags）」遥相呼应：Manual 模式里你手工剥首尾补事件，Auto 模式里你把首尾写成可识别的形状、让编译器来剥。

[Kernel_Developer_Rules_And_Limitations.md:30-59](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/auto_mode/Kernel_Developer_Rules_And_Limitations.md#L30-L59)：规则 1.2，不依赖内层归纳变量的 if 留在内层是反例（每轮重复判定、阻碍外提），应改写为把 if 提到内层循环外的正例。

[Kernel_Developer_Rules_And_Limitations.md:62-88](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/auto_mode/Kernel_Developer_Rules_And_Limitations.md#L62-L88)：规则 1.3，守卫 PTO 指令的复杂逻辑表达式（示例里组合了 `GetValidRow()`/`GetKAligned()` 等运行期查询）先求值到一个 `bool cond` 再进 if——强烈推荐。

[Kernel_Developer_Rules_And_Limitations.md:90-94](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/auto_mode/Kernel_Developer_Rules_And_Limitations.md#L90-L94)：规则 1.4，**目前强烈建议不要使用 double/multi buffering**——一旦 kernel 变复杂，双缓冲总是伴随复杂控制流，给自动同步带来巨大挑战；编译器团队正在设计带约束的专用抽象来支持它。这个「专用抽象」的雏形已进仓库：

[kernels/automode/a2a3/gemm/multiBuffer.hpp:17-47](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/automode/a2a3/gemm/multiBuffer.hpp#L17-L47)：`pto_auto::MultiStaged` 用「阶段函数 + `#pragma pto v_loop_barrier`」描述多级流水，把双缓冲的控制流交给编译器可识别的结构化形式；`kernels/automode/` 下的 gemm/topk/flash_atten 均包含此头（如 [gemm_performance_kernel.cpp:13](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/automode/a2a3/gemm/gemm_performance_kernel.cpp#L13)）。

**(2) 内存分配规则（2.1～2.4）**

[Kernel_Developer_Rules_And_Limitations.md:98-112](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/auto_mode/Kernel_Developer_Rules_And_Limitations.md#L98-L112)：规则 2.1，表达「Tile B 与 Tile A 同基地址」要用 `TRESHAPE(tileB, tileA)`（Manual/Auto 通用），而不是给两者 TASSIGN 同一个地址——后者在 Auto 模式下非法（TASSIGN 已空操作，且两个自动分配的地址互不相干）。

[Kernel_Developer_Rules_And_Limitations.md:114-133](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/auto_mode/Kernel_Developer_Rules_And_Limitations.md#L114-L133)：规则 2.2，表达「Tile B 的地址 = Tile A 的地址 + 行列偏移」要用 `TSUBVIEW(tileB, tileA, rowOffset, colOffset)`，让编译器知道两 Tile 如何别名。

TSUBVIEW 在 Auto 分支下的实现值得看一眼：

[include/pto/npu/a2a3/TSubView.hpp:18-43](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/npu/a2a3/TSubView.hpp#L18-L43)：Manual 分支把「源地址 + 偏移」重新 TASSIGN 给目的地（真·地址改写）；Auto 分支则先做 TileType/布局/有效区的一致性检查，然后调用 `__cce_alias(dst.data(), src.data(), byteOffset)`——**把别名关系作为提示告知编译器**，由编译器在自动分配的基地址上叠加偏移。同一 API、两种语义，这是「Manual 指令 → Auto 提示」的典型样本。

[Kernel_Developer_Rules_And_Limitations.md:135-150](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/auto_mode/Kernel_Developer_Rules_And_Limitations.md#L135-L150)：规则 2.3，Auto 模式给每个 Tile 变量分配**常量地址**、一次分配终身不变；Manual 模式「循环里 `TASSIGN(tile, 0x100 * i)` 轮转复用」的动态技巧无法被分配器处理，须改写。文档给出的心智模型：**把 Tile 想成 C++ 引用**。注意区分：这条约束针对 Tile；GlobalTensor 的 GM 视图地址不受影响（4.1.3 已确认其 TASSIGN 分支无 Auto 守卫），推进视图仍可用 TASSIGN 或重建视图。

[Kernel_Developer_Rules_And_Limitations.md:152-174](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/auto_mode/Kernel_Developer_Rules_And_Limitations.md#L152-L174)：规则 2.4，TRESHAPE/TSUBVIEW 在 Auto 模式下从「执行点改地址」变为「整个作用域内绑定两 Tile 的别名关系」；因此**一个 Tile 不能先后作为多个 TRESHAPE/TSUBVIEW 的目的地**（否则行为未定义），并建议把这两条指令紧贴目的地 Tile 的声明书写。

**(3) 通用规则（3.1～3.3）**

[Kernel_Developer_Rules_And_Limitations.md:178-201](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/auto_mode/Kernel_Developer_Rules_And_Limitations.md#L178-L201)：规则 3.1，**不要对输出 Tile 调冗余 TLOAD**。示例中 `TLOAD(dstTile, dstGlobal)` 在 Manual 模式无害（两个地址手工分开摆），但在 Auto 模式下，`dstTile` 与 `srcTile` 活跃区间不重叠 ⇒ 编译器可能复用同一地址 ⇒ 两个 TLOAD 同时发生、互相覆盖。这正是 4.1.2 公式的直接推论。

[Kernel_Developer_Rules_And_Limitations.md:203-213](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/auto_mode/Kernel_Developer_Rules_And_Limitations.md#L203-L213)：规则 3.2，kernel 开发者只应调用 PTO 指令，不要直接调 CCE intrinsic 或 Tile 的 `.data()`。两个理由：其一，intrinsic 收裸指针，而 Auto 模式下 Tile 是向量类型、根本编不过（见 `memory.hpp`）；其二，自动分配与自动同步只建立在「对 PTO 指令的分析」之上，认不出别的写法。确无等价 PTO 指令时，正确出路是向 pto-isa 申请新增指令（u11-l1 将走完整流程）。

[Kernel_Developer_Rules_And_Limitations.md:215-219](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/auto_mode/Kernel_Developer_Rules_And_Limitations.md#L215-L219)：规则 3.3，同步优先用 `PtoSetWaitFlag` 或 `TSYNC` 而非裸 `set_flag`/`wait_flag`——前者内置 `__PTO_AUTO__` 守卫（4.1.3 已读源码），Auto 模式下自动变空操作；直接调裸原语则每次都要自己包 `#ifndef __PTO_AUTO__`，繁琐且易漏。

**(4) 文档给出的最小对照**

[docs/auto_mode/README.md:19-81](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/auto_mode/README.md#L19-L81)：TMUL 的 Manual 版（三句 TASSIGN + 四句 set/wait）与 Auto 版（只剩 TLOAD×2 → TMUL → TSTORE 四行）并排对照，是「双模式工作流」最浓缩的展示；同文件 [README.md:5-12](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/auto_mode/README.md#L5-L12) 重申三大收益与「Auto 目前只支持 -O2」。更复杂的 TMATMUL 对照见 [docs/auto_mode/Examples.md:79-138](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/auto_mode/Examples.md#L79-L138)（Auto 版把 TLOAD→TMOV→TMATMUL→TSTORE_FP 直排，无一句 TASSIGN/Event）。

#### 4.2.4 代码实践

**实践：把一条规则「违反而复」——在 CPU 仿真下观察规则 2.3 的边界**

1. **实践目标**：亲手验证「Auto 模式下 Tile 地址一次分配终身不变」在 CPU 仿真路径上的实现形态，并体会规则 2.3 为何存在。
2. **操作步骤**：
   1. 阅读 [tests/cpu/st/testcase/tflashattn/tflashattn_kernel.cpp:66-111](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tflashattn/tflashattn_kernel.cpp#L66-L111)：注意 15 个 Tile 全部只声明、不 TASSIGN，指令序列直排。
   2. 对照其 CMake 的 `add_compile_definitions(__PTO_AUTO__)`，确认编译期开关。
   3. 运行 `python3 tests/run_cpu.py -t tflashattn`，确认功能正确。
   4. （源码阅读步骤）思考变体：若在该 kernel 里给 `scores` Tile 写 `TASSIGN(scores, 0x100 * i)` 循环改地址，在 NPU Auto 编译下会发生什么？对照 4.1.3 的 TAssign.hpp 源码回答（提示：整句被编译为空操作，循环改地址无效；但在 CPU 仿真下 TASSIGN 仍会真实改写指针——两个后端行为不同，这正是「同步/分配正确性必须真机验证」的又一例证）。
3. **需要观察的现象**：gtest 通过；变体思考中能说出「NPU 下空操作、CPU 下仍生效」的双态行为。
4. **预期结果**：能区分「规则禁止的写法」在两类后端上的不同失效方式。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：规则 1.1 要求首/末迭代守卫「可静态求值」。请从 u6-l2 的「首尾补同步」出发，解释这条规则与 Manual 模式手法的关系。

**答案**：u6-l2 讲过，真机流水的首轮没有历史事件可等、末轮还有未排空的写回，Manual 模式需要在循环外手工预置 `set_flag`（InitSyncFlags）并在循环后补 `wait_flag`（WaitSyncFlags）。Auto 模式下这两件事由编译器做，前提是它能**认出**首迭代与末迭代——把守卫写成 `tile_id == 0`、`tile_id == total_tiles - 1` 这类可静态求值形式，编译器就能剥离（peel）首尾迭代并精确补同步；守卫若藏在运行期复杂表达式里，它只能保守同步，性能受损。

**练习 2**：以下 Manual 代码迁移到 Auto 模式，至少违反了哪几条规则？

```cpp
TileData tileA, tileB;
for (int i = 0; i < N; i++) {
    TASSIGN(tileA, 0x100 * i);            // (a)
    TASSIGN(tileB, 0x100 * i);            // (b)
    TLOAD(tileA, gA);
    foo(tileB, tileA);
    TSTORE(gB, tileB);
}
```

**答案**：(a) 违反规则 2.3——Tile 地址运行期不可变，循环改地址在 Auto 下是空操作；(b) 除 2.3 外还违反规则 2.1——表达「B 与 A 同基地址」应该用 `TRESHAPE(tileB, tileA)`，并且按规则 2.4 紧贴声明书写、只做一次。改写方向：`TRESHAPE(tileB, tileA);` 放在声明后、循环外，循环体内只保留指令序列。

**练习 3**：为什么「对纯输出 Tile 补一次 TLOAD」在 Manual 模式无害、在 Auto 模式可能引发数据竞争？

**答案**：Manual 模式下两个 Tile 的地址由开发者手工分开摆，谁也不碰谁。Auto 模式下编译器按活跃区间分配：输出 Tile 在 TLOAD(输入) 之后才首次定值、在 TSTORE 时最后使用，与输入 Tile 的区间不重叠，因此二者可能被复用到同一地址。此时对输出 Tile 的那次冗余 TLOAD 与输入 TLOAD 同时发生，互相覆盖。本质是「活跃区间不重叠 ⇒ 地址复用」这一优化的副作用——文档用「DON'T CALL REDUNDANT TLOAD!」加粗警告。

### 4.3 auto add 示例：同一算子的两份代码

#### 4.3.1 概念说明

`demos/auto_mode/baseline/add` 与 u1-l4 精读过的 `demos/baseline/add` 实现同一个算子：20 个 AIV 核按行切分做逐元素加法（half，总长 totalLength 动态传入）。两份 kernel 的 host 侧完全同构（schema 声明、PrivateUse1 派发注册、`EXEC_KERNEL_CMD` 启动、`blockDim = 20`），差异全部集中在 kernel 侧——这正好把「Auto 模式省掉了什么」隔离得干干净净。

一个值得注意的取舍：Auto 版**没有循环**。Manual 版每核用 `tileNum * BUFFER_NUM = 4` 轮迭代 + ping-pong 流水吃完全部数据；Auto 版每核一次 `TLOAD → TADD → TSTORE` 处理自己那一片（`bTileRows × bTileCols = 1 × 2048` 个元素）。这与示例 README 的注意事项直接对应——Auto 模式暂不建议使用 double/multi buffering，示例索性用单发直排来规避。

#### 4.3.2 核心流程

Auto 版 kernel 的执行结构：

```
kernel 入口 add_custom(x, y, z, totalLength)
        │
        ▼
runTAdd<half, 20, 2048>
        │  编译期：核间切分 bTileRows = 20/20 = 1, bTileCols = 2048/1 = 2048
        │  运行期：offset = block_idx * bTileRows * bTileCols   ← 多核身份只体现为指针平移
        ▼
构造 3 个 GlobalTensor 视图（x/y/z + offset）
        ▼
声明 3 个 Tile（声明即分配，无 TASSIGN）
        ▼
TLOAD(xTile, xGlobal) ──┐
TLOAD(yTile, yGlobal) ──┤  指令直排，无一句 set_flag/wait_flag；
TADD(zTile, xTile, yTile)│  同步由编译器在编译期自动插入
TSTORE(zGlobal, zTile) ─┘
```

与 Manual 版（u1-l4 的「循环 { 更新视图地址 → TLOAD → 事件同步 → 计算 → 事件同步 → TSTORE }」骨架）对照，Auto 版把「循环 + 事件」两个维度同时拿掉，只剩数据通路本身。

#### 4.3.3 源码精读

**(1) Auto 版 kernel 全貌**

[demos/auto_mode/baseline/add/csrc/kernel/add_custom.cpp:24-53](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/auto_mode/baseline/add/csrc/kernel/add_custom.cpp#L24-L53)：`runTAdd` 模板函数完整实现。前半段与 Manual 版几乎逐行相同——`set_mask_norm()`/`set_vector_mask(-1, -1)`、`static_assert` 校验核切分与 UB 容量、定义 5 维 Shape/Stride 与 `GlobalTensor` 视图类型、计算 `offset = block_idx * bTileRows * bTileCols` 平移 GM 指针（[add_custom.cpp:35-42](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/auto_mode/baseline/add/csrc/kernel/add_custom.cpp#L35-L42)）。

[demos/auto_mode/baseline/add/csrc/kernel/add_custom.cpp:44-52](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/auto_mode/baseline/add/csrc/kernel/add_custom.cpp#L44-L52)：全 kernel 的「心脏」只有 7 行有效代码——声明三个 `TileData`（Vec/half/1×2048/RowMajor，行列掩码 DYNAMIC），然后 `TLOAD` 两条、`TADD` 一条、`TSTORE` 一条。**没有 TASSIGN，没有事件，没有循环，没有 ping-pong 标志**。对比 Manual 版同职责代码的 50 余行（见下），这就是 Auto 模式的全部卖点。

**(2) Manual 版的三类样板（对照组）**

[demos/baseline/add/csrc/kernel/add_custom.cpp:18-30](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L18-L30)：**样板一（地址规划）**——`BUFFER_NUM = 2` 加上 `X_PING/X_PONG/Y_PING/Y_PONG/Z_PING/Z_PONG` 六个常量把 192KB UB 手工切成输入/输出、乒乓四象限，人肉保证不重叠不越界；随后 [add_custom.cpp:60-71](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L60-L71) 声明三个 Tile 数组并用六连 `TASSIGN` 绑上这些地址。

[demos/baseline/add/csrc/kernel/add_custom.cpp:79-82](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L79-L82)：**样板二（事件同步）**——循环前的四句 `set_flag` 预置首轮事件（InitSyncFlags 手法）；循环体内还有 8 处 `wait_flag`/`set_flag` 交替配对（[add_custom.cpp:90-107](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L90-L107)），循环后 [add_custom.cpp:110-113](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L110-L113) 再补四句 `wait_flag` 收尾排空（WaitSyncFlags 手法）。这一整套「预置—配对—排空」在 Auto 版里一句都没有。

[demos/baseline/add/csrc/kernel/add_custom.cpp:83-109](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L83-L109)：**样板三（乒乓循环编排）**——`loopCount = tileNum * BUFFER_NUM` 的主循环里，每轮先 TASSIGN 推进三个 GlobalTensor 视图地址，再按 `pingpong_flag` 选择缓冲槽，结尾 `pingpong_flag` 0/1 翻转。Auto 版没有循环，每核一次直排完成，也就没有任何轮转状态。

**(3) 编译开关与运行方式**

[demos/auto_mode/baseline/add/CMakeLists.txt:56-67](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/auto_mode/baseline/add/CMakeLists.txt#L56-L67)：`ascendc_library` 收录 kernel 源文件，`ascendc_compile_options` 传入 `--cce-pto-enable --cce-pto-auto-enable -O2`——注释明确「auto mode only works with -O2」。与 Manual 版唯一的构建差异就是这两个编译选项。

[demos/auto_mode/baseline/add/README.md:19-38](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/auto_mode/baseline/add/README.md#L19-L38)：文档强调「与 Manual 模式不同，你不需要手工调用 TASSIGN 与同步指令」，并给出三条 NOTE：加两个编译选项启用 Auto、必须 -O2、本示例未用双缓冲且强烈建议暂不使用 double/multi buffering。

[demos/auto_mode/baseline/add/README.md:93-139](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/auto_mode/baseline/add/README.md#L93-L139)：真机构建运行四步——设 `SOC_VERSION`、导出 `PTO_LIB_PATH` 后 `python3 setup.py bdist_wheel` 打 wheel、`pip install`、跑 `test/test.py`；一键脚本见 [run.sh:20-25](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/auto_mode/baseline/add/run.sh#L20-L25)。host 侧的算子注册（`TORCH_LIBRARY_FRAGMENT(npu, ...)` 声明 schema、`TORCH_LIBRARY_IMPL(npu, PrivateUse1, ...)` 注册实现、`EXEC_KERNEL_CMD` 启动）与 Manual 版完全一致（README 第 2 节，[README.md:44-91](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/auto_mode/baseline/add/README.md#L44-L91)）——**Auto/Manual 的差异被完全封装在 kernel 侧与编译选项里，对上层框架透明**。

#### 4.3.4 代码实践（本讲主实践）

**对比两份 kernel 源码 diff，列出 Auto 模式替你省掉的三类样板代码**

1. **实践目标**：亲手从真实源码中归纳「Auto 模式接管了什么」，而不是背结论。
2. **操作步骤**：

   ```bash
   # 1) 逐行 diff 两份 kernel（均约 128/65 行，适合整读）
   diff -u demos/baseline/add/csrc/kernel/add_custom.cpp \
            demos/auto_mode/baseline/add/csrc/kernel/add_custom.cpp

   # 2) 按三类样板在 Manual 版中定位并统计行数
   grep -n "TASSIGN\|set_flag\|wait_flag\|BUFFER_NUM\|pingpong_flag" \
        demos/baseline/add/csrc/kernel/add_custom.cpp

   # 3) 确认 Auto 版中这些关键词全部消失
   grep -n "TASSIGN\|set_flag\|wait_flag\|BUFFER_NUM\|pingpong_flag" \
        demos/auto_mode/baseline/add/csrc/kernel/add_custom.cpp   # 应无输出
   ```

3. **需要观察的现象**：diff 里 Manual 独有的块恰好聚成三簇——地址常量与 TASSIGN、事件 set/wait（含循环前预置与循环后收尾）、乒乓循环与槽位翻转；Auto 版保留的只有 GlobalTensor 视图、Tile 声明与四条数据通路指令。
4. **预期结果**：**三类样板清单**——
   - **样板一：片上地址规划与 TASSIGN 绑定**（6 个地址常量 + 6 句 TASSIGN）→ Auto：声明 Tile 即分配（4.1.3 的向量类型 + 编译器分配）；
   - **样板二：跨流水线事件同步**（4 句预置 + 8 处循环内配对 + 4 句收尾）→ Auto：编译器自动插同步；
   - **样板三：乒乓循环编排**（BUFFER_NUM=2、pingpong_flag 翻转、loopCount 主循环、循环内推进 GlobalTensor 视图）→ Auto：本示例以单发直排规避（对应规则 1.4「暂不用双缓冲」）。
5. **延伸（有 NPU 时）**：按 [demos/auto_mode/baseline/add/README.md:115-139](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/auto_mode/baseline/add/README.md#L115-L139) 打 wheel、安装并运行 `test/test.py`，确认 `torch.ops.npu.my_add` 输出与 `x + y` 一致。**待本地验证**（需要 CANN 环境、torch_npu 与昇腾硬件）。
6. **延伸（无 NPU 时的等价观察）**：CPU 仿真路径上完成「Auto 风格 kernel 可运行」的验证——`python3 tests/run_cpu.py -t tflashattn`（4.1.4 实践二），或 `python3 tests/run_cpu.py --demo gemm` 跑以 `__CPU_SIM __PTO_AUTO__` 编译的 gemm demo。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：Auto 版 kernel 里为什么还保留 `set_mask_norm()` 和 `set_vector_mask(-1, -1)`？它们不也是「底层细节」吗？

**答案**：二者不归 Auto 模式管。`set_mask_norm`/`set_vector_mask` 是 CCE 向量部件的掩码模式/掩码寄存器设置（决定后续向量指令按 norm 还是 count 模式解释掩码），属于底层硬件配置原语而非「Tile 缓冲分配」或「跨流水线同步」。Auto 模式接管的是后两者；Tile 掩码相关的模式设置仍是 kernel 的职责。（这也再次印证 4.1.1 的边界：分析只认 PTO 指令层面的分配与依赖。）

**练习 2**：Auto 版每核只处理 `1 × 2048` 个元素、一次直排；Manual 版每核 4 轮迭代 + 乒乓。这个差异是「Auto 模式做不了循环」吗？

**答案**：不是。Auto 模式完全支持循环（规则 1.1/1.2 就是在教你写「对编译器友好」的循环）。示例选择单发直排是因为规则 1.4：double/multi buffering 尚未完全支持，而「多轮搬运 + 计算重叠」的循环若要高性能必然引入乒乓缓冲与复杂控制流。示例以功能正确、结构最简为先。需要流水的场景可关注 `kernels/automode/` 下的 gemm/topk/flash_atten 与 `multiBuffer.hpp` 的专用抽象。

**练习 3**：如果把 Auto 版 kernel 里再加一句 `TASSIGN(xTile, 0x0)`，编译能过吗？行为会变吗？

**答案**：能编译（API 仍存在），NPU Auto 路径上这句被 `TASSIGN_IMPL` 的 `#ifndef __PTO_AUTO__` 分支屏蔽、什么也不做（4.1.3 源码）；但在 CPU 仿真（`__CPU_SIM`，含或不含 `__PTO_AUTO__`）路径上，CPU 版 `TASSIGN_IMPL` 没有这个守卫，会真实地把地址解析并绑给 Tile（覆盖懒分配的缓冲）。所以「同一句冗余 TASSIGN」在两个后端上行为不同——这也是本讲反复强调的结论：**Auto 模式的分配/同步语义以 NPU 编译为准，CPU 仿真只保证功能正确性验证的便利**。

## 5. 综合实践

**任务：把一个 CPU demo 改写成「纯 Auto 风格」，并用 `__PTO_AUTO__` 的双后端行为做一次验证。**

背景：`demos/cpu/gemm_demo` 本来就以 `__CPU_SIM __PTO_AUTO__` 双宏编译（[demos/cpu/gemm_demo/CMakeLists.txt:26](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/gemm_demo/CMakeLists.txt#L26)），但它的源码里仍保留着 5 句冗余 TASSIGN（[demos/cpu/gemm_demo/gemm_demo.cpp:103-107](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/gemm_demo/gemm_demo.cpp#L103-L107)）——这是一个理想的「半迁移」标本。

步骤（全部在你自己的工作副本上做，不要改动仓库源码）：

1. 把 `demos/cpu/gemm_demo/gemm_demo.cpp` 复制到临时目录，删除 L103-107 的五句 `TASSIGN`，其余不动。
2. 对照 4.1.3 阅读删除后的代码：Tile 声明（`TileMatA aMat;` 等，[gemm_demo.cpp:97-101](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/gemm_demo/gemm_demo.cpp#L97-L101)）之后直接就是 `TLOAD → TMOV → TMATMUL → TSTORE`——这正是 `tflashattn_kernel.cpp` 的 Auto 风格。
3. 以相同编译定义（`__CPU_SIM __PTO_AUTO__`，对照原 CMakeLists）在临时目录编译运行（或临时改一份自己的 CMakeLists），检查输出的 `max_abs_diff` 是否仍小于 `1e-3` 阈值。
4. 记录现象并回答：删除 TASSIGN 后数据从哪里来？（预期：`Tile::data()` 的懒分配路径——`internalBuffer.resize(Rows*Cols)` 后把宿主指针交给 `data_`，见 4.1.3 第 (5) 点。）
5. 反向验证双态行为：同一份代码去掉 `__PTO_AUTO__` 只留 `__CPU_SIM` 再编译一次，观察会发生什么（预期：CPU 版 TASSIGN 被删后 Tile 的 `data_` 不再被指向 NPUMemoryModel 分配的地址——若实现仍依赖该路径则行为异常，从而体会 Manual 模式下 TASSIGN 的必要性）。

预期结果：第 3 步通过（功能正确），第 4 步能说出懒分配机制，第 5 步能说出「同一份无 TASSIGN 代码在 Manual 语义下不再被自动兜底」。**待本地验证**（本讲义未替你执行；若第 3/5 步结果与预期不符，请以实际输出为准并回到 4.1.3 的源码重新推理）。

## 6. 本讲小结

- **Auto 模式是编译模式而非新 API**：同一套 PTO 指令，由 `--cce-pto-enable --cce-pto-auto-enable -O2`（NPU）或 `add_compile_definitions(__PTO_AUTO__)`（CPU 仿真）开启；`TASSIGN`（Tile 分支）与 `TSYNC`/`Event` 在 Auto 下编译为空操作。
- **头文件层的实现三板斧**：Tile 数据成员从 `__ubuf__ DType*` 变为 `tile_size(Rows*Cols)` 向量类型（`memory.hpp`/`pto_tile.hpp`）；同步类指令整体被 `#ifndef __PTO_AUTO__` 屏蔽（`event.hpp`/`TSync.hpp`）；TRESHAPE/TSUBVIEW 从「执行点改地址」变为「告知编译器别名关系」（`__cce_alias`）。
- **编译器只工作在 TF 层以上**，三大能力（活跃区间分析、自动同步、自动内存分配）都以 Tile 抽象为界；tile function 内部是黑盒。
- **开发者规则是分析的前提**：控制流要可静态分析（首尾守卫、循环不变量外提、复杂条件预求值、暂不用双缓冲）；内存上把 Tile 当 C++ 引用（地址终身不变、别名用 TRESHAPE/TSUBVIEW 表达且目的地唯一）；通用上不写冗余 TLOAD、不直调 intrinsic/`.data()`、同步优先 `PtoSetWaitFlag`/`TSYNC`。
- **add 双版本对照**：Auto 版以 7 行核心代码替代 Manual 版约 50 行样板，省掉的三类是——地址规划与 TASSIGN、事件 set/wait 编排、乒乓循环与槽位翻转；host 侧与构建体系对模式切换完全透明。
- **CPU 仿真可验功能、不可证分配**：`__CPU_SIM __PTO_AUTO__` 下 Tile 走懒分配兜底，且 CPU 版 TASSIGN 无 Auto 守卫——Auto 分配/同步语义以 NPU 编译为准（`tflashattn` 用例是 CPU 侧的 Auto 风格样板）。

## 7. 下一步学习建议

- **下一讲（u9-l2）**：自定义算子封装与框架集成——Auto 版 add 的 host 侧（`TORCH_LIBRARY` schema、PrivateUse1 派发、`EXEC_KERNEL_CMD`）将展开成 `kernels/custom/fused_add_relu_mul` 的完整工程，请带着「kernel 侧模式切换对 host 透明」的结论去读。
- **Auto 模式高性能样本**：浏览 `kernels/automode/a2a3/`（gemm/topk/flash_atten）与 `kernels/automode/a5/flash_atten`，观察它们如何在不写 TASSIGN/事件的前提下组织多级流水，并精读 `multiBuffer.hpp` 的 `MultiStaged`/`#pragma pto v_loop_barrier`——这是规则 1.4 所说「双缓冲专用抽象」的落地形态。
- **库开发者视角**：`docs/auto_mode/Library_Developer_Rules_And_Limitations.md` 讲新增 PTO 指令（库侧）在 Auto 模式下必须满足的约束，是 u11-l1「新增一条指令」的前置材料。
- **JIT 路线**：`demos/auto_mode/torch_jit/add/` 提供了不经 wheel 打包、运行时编译并启动 Auto kernel 的 Python 路线（`add_compile_and_run.py`），适合快速实验。
