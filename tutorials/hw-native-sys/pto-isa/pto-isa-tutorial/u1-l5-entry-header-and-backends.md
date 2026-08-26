# 统一入口与多后端架构：pto-inst.hpp 剖析

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐行读懂 [include/pto/pto-inst.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/pto-inst.hpp#L11-L33) 这份只有 33 行的「总装配头文件」，说清它每一行 `#include` 在什么条件下生效。
2. 解释 `__CPU_SIM`、`__CCE_AICORE__`、`__COSTMODEL` 三个编译期宏如何把同一份内核代码分别装配到 CPU 模拟器、NPU 真机、CostModel 三种后端，并说出隐藏在幕后起作用的第四个宏 `__NPU_ARCH__`——它经 `arch_macro.hpp` 翻译成 `PTO_NPU_ARCH_A2A3/A5/A6/KIRIN9030` 等内部宏后，决定 NPU 分支落到哪一代实现目录（本版本恢复了 A6 指令头的接入）。
3. 画出（并亲手用 `g++ -E` 验证）不同宏组合下 `pto-inst.hpp` 实际展开的头文件依赖图。
4. 精读 [include/pto/common/cpu_stub.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/cpu_stub.hpp#L36-L49)，理解 CPU 模拟器如何用「空宏 + 空函数 + malloc 版 aclrt」三类替身把昇腾专有语法翻译成本机 C++。
5. 理解「公共接口声明在 `common/`、具体实现按后端分布在 `cpu/`、`npu/a2a3/`、`npu/a5/`」的分层动机，并能沿 `TADD → TADD_IMPL` 这条链找到同一指令的三份实现。

本讲是单元一的收官之作：u1-l4 里你已经「用」过这套机制（同一份 tadd 内核在本机直接跑），本讲带你打开引擎盖，看清它是怎么发生的。

## 2. 前置知识

本讲不需要新的硬件知识，但需要你接受几个「编译视角」的概念：

- **条件编译**：C++ 预处理器根据宏是否被定义，决定哪些行参与编译。`#if defined(A)` / `#elif` / `#else` / `#endif` 是纯文本层面的开关——被关闭的分支甚至不会被语法检查。PTO 的多后端选择完全发生在这一层。
- **header-only 模板库**：PTO-ISA 没有 `.so`、`.a` 可以链接，所有指令实现都以模板/内联函数的形式写在头文件里，在**编译上层内核时**才展开。这意味着「选择哪个后端」不是运行时决策，而是编译期决策（见 [docs/PTO-ISA-Header-and-Library-Description.md:29](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/PTO-ISA-Header-and-Library-Description.md#L29) 中「header-only 纯头文件模板库」的说明）。
- **三种运行模式回顾**（u1-l2 已讲）：CPU 模拟器（本机跑，免昇腾环境）、NPU 真机/sim（需 CANN 与 BiSheng/ccec 编译器）、CostModel（无硬件估算性能）。本讲要回答的问题是：**同一份 `.cpp`，编译器凭什么知道该变成哪一种？**
- **地址空间修饰符**：昇腾 CCE 编译器有一批关键字，如 `__gm__`（全局内存）、`__ubuf__`（Unified Buffer）、`__ca__`/`__cb__`/`__cc__`（矩阵单元的 L0 缓冲）、`__aicore__`、`__tf__` 等。它们在 NPU 编译器里是真实关键字；在普通 g++/clang 里**根本不存在**。这就是 CPU 路径需要「替身」的根源。
- **`__NPU_ARCH__`**：SoC 代际编号宏，由 CCE 编译器在设备编译时内置定义（如 2201 代表 A2/A3 代、3101/3510 代表 A5 代）。它决定 NPU 内部再细分到 `npu/a2a3/` 还是 `npu/a5/` 目录。
- **预处理命令 `g++ -E`**：只跑预处理器（展开 `#include` 与宏），不做编译。因此即使头文件里用了 g++ 不认识的关键字，`-E` 依然能成功输出——这是本讲观察 include 依赖图的工具。

一个直觉比喻：`pto-inst.hpp` 像一家餐厅的「总菜单」，顾客（上层内核）只需要说「来一份 PTO」；后厨（编译宏）决定今天用哪套灶台——CPU 灶、NPU 灶还是 CostModel 灶。菜谱（指令 API 的签名）是同一份，做菜的手法（实现）各不相同。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `include/pto/pto-inst.hpp` | 统一入口头：按宏装配公共基础层、stub 层与指令层（本讲主角，仅 33 行） |
| `include/pto/README.md` | `include/pto/` 的布局说明，指明推荐入口与目录职责 |
| `include/pto/common/type.hpp` | 数据类型与公共修饰宏（`AICORE`、`PTO_INST`、断言宏），无条件被首先包含 |
| `include/pto/common/kernel_meta.hpp` | 内核元信息（混合核任务配比等），与后端无关 |
| `include/pto/common/memory.hpp` | `MemoryQualifier`：把 TileType 映射到 `__ubuf__`/`__cbuf__` 等地址空间修饰符 |
| `include/pto/common/cpu_stub.hpp` | CPU 替身层：空宏、空事件、malloc 版 aclrt、模拟多核启动器 |
| `include/pto/common/arch_macro.hpp` | 把 `__NPU_ARCH__` 翻译成 `PTO_NPU_ARCH_A2A3/A5/...` 内部宏 |
| `include/pto/common/arch_capability.hpp` | 按架构给出能力开关（支持哪些 dtype、通信、量化等）`CurrArch` |
| `include/pto/common/pto_tile.hpp` | `Shape`/`Stride`/`GlobalTensor`/`Tile` 核心类型系统 |
| `include/pto/common/pto_instr.hpp` | 指令**公共 API 声明**层（`TADD`、`TASSIGN`…）与 `MAP_INSTR_IMPL` 宏 |
| `include/pto/common/pto_instr_impl.hpp` | 实现装配枢纽：按架构宏批量 `#include` 对应后端的 `*_IMPL` 头（本版本恢复了 A6 块并修复了 `__DAV_VEC__` 保护的闭合） |
| `include/pto/npu/a6/header.hpp` | A6 代后端的「汇总头」：专用 TLoad/TExtract/TMatmul 等实现 + 直接复用 A5 的 TAssign/TAdd/TStore |
| `include/pto/cpu/TAdd.hpp`、`include/pto/npu/a2a3/TAdd.hpp`、`include/pto/npu/a5/TAdd.hpp` | `TADD_IMPL` 的三份并行实现（CPU / A2A3 / A5） |
| `include/pto/costmodel/runtime_stub.hpp` | CostModel 后端的 stub 入口（内含 aclrt 假实现与架构选择） |
| `include/pto/costmodel/pto_instr.hpp` | CostModel 版指令声明层：复用 common 的 include guard，作为「替身」顶替 common/pto_instr.hpp |
| `tests/cpu/st/CMakeLists.txt` | CPU 模拟器 ST 构建的宏定义处（`-D__CPU_SIM`） |
| `tests/costmodel/st/CMakeLists.txt` | CostModel ST 构建的宏定义处（`-D__COSTMODEL -D__NPU_ARCH__=2201`） |
| `tests/cpu/st/testcase/tadd/tadd_kernel.cpp` | 只包含 `pto-inst.hpp` 就写完一个内核的实例证据 |
| `docs/coding/cpu_sim.md` | 官方对 `__CPU_SIM` 与 cpu_stub 角色的说明 |

## 4. 核心概念与源码讲解

### 4.1 统一入口 pto-inst.hpp：33 行的「装配车间」

#### 4.1.1 概念说明

PTO 有 149 条指令、上百个实现头文件，但官方要求上层代码**只包含一个头**。[include/pto/README.md:10-14](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/README.md#L10-L14) 写明 `include/pto/pto-inst.hpp` 是「Unified entry header (recommended for upper-layer code)」，并在 CPU 仿真场景下由它负责拉入 `cpu_stub.hpp`。更权威的表述在 [docs/PTO-ISA-Header-and-Library-Description.md:29](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/PTO-ISA-Header-and-Library-Description.md#L29)：它是「PTO 指令集架构的唯一对外统一入口头文件……依据编译期宏自动选择实现后端」。

你已经见过实际用法——u1-l4 精读的 tadd 内核，开头只有两行 include：

- [tests/cpu/st/testcase/tadd/tadd_kernel.cpp:11-12](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L11-L12)：包含 `pto/pto-inst.hpp` 与 `pto/common/constants.hpp`，随后的 `AICORE`、`__gm__`、`TASSIGN`、`TADD` 全部由这一个入口（在 CPU 宏下）自动供给。

统一入口解决三个问题：

1. **使用方零心智负担**：不必知道 `TAdd.hpp` 在 `cpu/` 还是 `npu/a2a3/`。
2. **后端切换零代码改动**：同一份内核源文件，换一组编译宏即换后端。
3. **包含顺序正确性由入口负责**：stub 必须先于使用昇腾修饰符的头被包含（见 4.3.1），这类顺序约束集中在一处维护。

#### 4.1.2 核心流程

`pto-inst.hpp` 的装配逻辑可以概括为三段式：

```text
第一段（无条件公共基础）
    type.hpp（类型与修饰宏） + kernel_meta.hpp（内核元信息）
        │
第二段（stub 选择：为「昇腾语法」准备替身或真身）
    __CPU_SIM    ──► common/cpu_stub.hpp        （空宏替身）
    __COSTMODEL  ──► costmodel/runtime_stub.hpp （CostModel 替身，内部也复用 cpu_stub）
    两者皆无(NPU) ──► 什么都不包含（修饰符由 CCE 编译器内置提供）
        │
    memory.hpp（使用 __ubuf__ 等修饰符，必须在 stub 之后！）
        │
第三段（指令层：三宏任一存在才装配）
    __CPU_SIM / __CCE_AICORE__ / __COSTMODEL 任一定义
        ├─► arch_macro.hpp（__NPU_ARCH__ → 内部架构宏）
        ├─► arch_capability.hpp（CurrArch 能力开关）
        ├─► pto_tile.hpp（Tile 类型系统）
        └─► __COSTMODEL ? costmodel/pto_instr.hpp : common/pto_instr.hpp
             （指令 API 声明 + 拉起全部 *_IMPL 实现）
```

注意两个精妙的条件细节：

- 第三段的外层条件是「三宏**任一**定义」。如果三个宏都没定义（例如某些纯 host 侧代码只想要类型定义），只有第一、二段生效，**指令 API 完全不可用**——这是入口刻意表达的语义：「不选后端就没有指令」。
- CostModel 单独走 `costmodel/pto_instr.hpp`，而不是 common 版。为什么能「顶替」？见 4.4.3 的 include guard 复用技巧。

#### 4.1.3 源码精读

入口全文如下（去掉版权头后即为全部内容）：

```cpp
#ifndef PTO_INST_HPP
#define PTO_INST_HPP

#include <pto/common/type.hpp>
#include <pto/common/kernel_meta.hpp>
#if defined(__CPU_SIM)
#include "pto/common/cpu_stub.hpp"
#elif defined(__COSTMODEL)
#include "pto/costmodel/runtime_stub.hpp"
#endif
#include <pto/common/memory.hpp>

#if defined(__CPU_SIM) || defined(__CCE_AICORE__) || defined(__COSTMODEL)
#include <pto/common/arch_macro.hpp>
#include <pto/common/arch_capability.hpp>
#include <pto/common/pto_tile.hpp>
#if defined(__COSTMODEL)
#include "pto/costmodel/pto_instr.hpp"
#else
#include "pto/common/pto_instr.hpp"
#endif
#endif
#endif
```

逐段对应（真实行号）：

- [include/pto/pto-inst.hpp:14-15](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/pto-inst.hpp#L14-L15)：无条件包含 `type.hpp` 与 `kernel_meta.hpp`。`type.hpp` 里定义了 `PTO_INST`/`PTO_INTERNAL` 修饰宏（详见 4.4.3），是后面一切声明的地基；`kernel_meta.hpp` 提供 SYNCALL 混合核的元信息结构（[include/pto/common/kernel_meta.hpp:24-44](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/kernel_meta.hpp#L24-L44)），与后端无关。
- [include/pto/pto-inst.hpp:16-20](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/pto-inst.hpp#L16-L20)：stub 选择。`__CPU_SIM` 优先级最高（`#if`/`#elif` 互斥）；NPU 路径**没有任何 include**——`__gm__`、`__aicore__` 等在 CCE 编译器里是内置关键字，无需文件提供。
- [include/pto/pto-inst.hpp:21](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/pto-inst.hpp#L21)：`memory.hpp` 被**刻意放在 stub 之后**。原因看一眼它的内容就懂：[include/pto/common/memory.hpp:26-33](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/memory.hpp#L26-L33) 中 `MemoryQualifier` 直接使用 `__ubuf__ DType*` 这样的写法——CPU/CostModel 路径若不先由 stub 把 `__ubuf__` 定义成空宏，这里就无法通过编译。**「stub 先行」是写在新入口里的顺序契约。**
- [include/pto/pto-inst.hpp:23-26](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/pto-inst.hpp#L23-L26)：三宏任一存在，才包含架构层与 Tile 类型系统。
- [include/pto/pto-inst.hpp:27-31](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/pto-inst.hpp#L27-L31)：指令声明层的二选一——CostModel 用自己的版本，其余（CPU/NPU）共用 common 版本。

#### 4.1.4 代码实践

**实践目标**：不运行任何程序，仅凭上面 33 行源码，手工推导三种宏组合下会被包含的 PTO 头文件集合。

**操作步骤**：

1. 打开 [include/pto/pto-inst.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/pto-inst.hpp#L11-L33)，准备三张草稿纸，分别标注「无宏」「`__CPU_SIM`」「`__COSTMODEL`」。
2. 对每张纸，沿每个 `#include` 递归展开**一层**（即打开被包含文件，看它顶部又包含了什么），把发现的新头文件记入集合。
3. 用下面的命令做静态核对（不需要编译，`grep` 即可）：

```bash
# 列出入口直接包含的头
grep -n '#include' include/pto/pto-inst.hpp
# 看 stub 头自己又拉入了什么
grep -n '#include' include/pto/common/cpu_stub.hpp | head
# 看 common 指令声明层拉入了什么
grep -n '#include' include/pto/common/pto_instr.hpp | head -20
```

**需要观察的现象**：

- 「无宏」组合的集合最小：只有 `type.hpp`、`kernel_meta.hpp`、`memory.hpp` 及其传递依赖（`arch_macro.hpp` 等），**不含任何指令 API**。
- 「`__CPU_SIM`」组合会经 `common/pto_instr.hpp → pto_instr_impl.hpp` 一路滚雪球到整个 `pto/cpu/` 家族。
- 「`__COSTMODEL`」组合则进入 `costmodel/` 目录自己的替换链。

**预期结果**：三张手绘图就是第 5 节综合实践的「参考答案」，届时用 `g++ -E` 机器验证。本步骤的 grep 输出可以直接核对每个箭头。

**待本地验证**：递归展开后的完整文件清单（数量级见 5. 综合实践）。

#### 4.1.5 小练习与答案

**练习 1**：如果用户代码 `#include <pto/pto-inst.hpp>` 时三个宏都没定义，会发生什么？这条路径有什么用？

**答案**：只获得类型与元信息层（`type.hpp`/`kernel_meta.hpp`/`memory.hpp`），没有任何指令 API、没有 Tile 类型系统（`pto_tile.hpp` 在第三段条件内）。用途是让 host 侧工具代码能引用 PTO 的基础类型而不引入设备侧代码。注意由于 `memory.hpp` 使用 `__ubuf__` 等修饰符，这条路径面向的是能提供这些修饰符的编译环境（NPU 编译器），在普通 g++ 下做完整编译会遇到名字未定义问题（但 `-E` 预处理不受影响，见综合实践）。

**练习 2**：为什么 `memory.hpp` 的 include 写在 stub 的 `#if/#elif/#endif` 之后、而不是和 `type.hpp` 放在一起？

**答案**：`memory.hpp` 的 `MemoryQualifier` 直接使用 `__ubuf__`、`__cbuf__`、`__ca__` 等修饰符（[include/pto/common/memory.hpp:26-33](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/memory.hpp#L26-L33)）。CPU/CostModel 路径必须先由 `cpu_stub.hpp` 把这些记号定义为空宏，NPU 路径则依赖编译器内置关键字先行存在。顺序颠倒了，CPU 仿真编译就会失败。

**练习 3**：`#if defined(__CPU_SIM)` 与 `#elif defined(__COSTMODEL)` 的先后顺序说明什么？如果有人同时定义了两个宏会怎样？

**答案**：说明 CPU 仿真优先级更高，二者互斥；同时定义时 `__CPU_SIM` 获胜（`#elif` 分支不会执行），CostModel 的 stub 不会被包含。实际构建系统里二者从不同时出现（见 4.2.3 的两处 `add_definitions`）。

### 4.2 宏条件编译：三个后端宏与幕后的 `__NPU_ARCH__`

#### 4.2.1 概念说明

三个「后端选择宏」各对应一条构建链路，但只有 `__CPU_SIM` 和 `__COSTMODEL` 是**由本仓库的构建脚本显式定义**的；`__CCE_AICORE__` 是 CCE 设备编译器（ccec，BiSheng 编译器家族）在编译设备代码时的**内置宏**，本仓库只需「察觉」它。此外还有一批起辅助作用的宏：

| 宏 | 谁定义它 | 作用 |
| --- | --- | --- |
| `__CPU_SIM` | `tests/cpu/st/CMakeLists.txt:32`、各 demo/kernel 的 CMake 选项 | 选择 CPU 模拟器后端 |
| `__CCE_AICORE__` | CCE 设备编译器内置（NPU 构建时自动存在） | 选择 NPU 真机后端 |
| `__COSTMODEL` | `tests/costmodel/st/CMakeLists.txt:21` 等 | 选择 CostModel 后端 |
| `__NPU_ARCH__` | NPU 路径由 CCE 编译器内置；CostModel 路径由 CMake 显式给出 | SoC 代际编号：2201→A2/A3，3101/3510→A5，9201→A6 等 |
| `PTO_COMM_NOT_SUPPORTED` | CostModel 构建等场景显式定义 | 关闭通信指令集装配 |
| `__PTO_AUTO__` | Auto Mode 构建选项（如 [demos/cpu/mla_attention_demo/CMakeLists.txt:26](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/demos/cpu/mla_attention_demo/CMakeLists.txt#L26)） | 启用 tile 自动内存分配（u7-l1 会展开） |

`__NPU_ARCH__` 是「第四个主角」：三个后端宏回答「用哪条链路」，`__NPU_ARCH__` 回答「这条链路里用哪一代芯片的实现」。

#### 4.2.2 核心流程

把宏翻译成后端的完整决策流程（与源码一一对应）：

```text
编译开始
 ├── 定义了 __CPU_SIM？
 │     └─ 是 ─► CPU 模拟器后端（实现来自 include/pto/cpu/*）
 │              且 arch_macro 会自动补定义 __DAV_CUBE__/__DAV_VEC__
 ├── 定义了 __COSTMODEL？
 │     └─ 是 ─► CostModel 后端（实现来自 include/pto/costmodel/*）
 │              必须同时给出 __NPU_ARCH__（2201 或 3101/3510），否则报错
 ├── 由 CCE 设备编译器编译（自动有 __CCE_AICORE__）？
 │     └─ 是 ─► NPU 真机后端，__NPU_ARCH__ 同样由编译器内置
 └── 三者皆无 ─► 只有类型层，无指令

无论哪条链路（NPU/CostModel），接下来：
__NPU_ARCH__ == 2201          ──► PTO_NPU_ARCH_A2A3   ──► npu/a2a3/ 目录
__NPU_ARCH__ == 3101 或 3510  ──► PTO_NPU_ARCH_A5     ──► npu/a5/ 目录
                                  （3510 额外定义 PTO_URMA_SUPPORTED）
__NPU_ARCH__ == 3113/3003/5101──► Kirin 系列（并定义 PTO_COMM_NOT_SUPPORTED）
__NPU_ARCH__ == 9201          ──► PTO_NPU_ARCH_A6（同样定义 PTO_COMM_NOT_SUPPORTED；
                                  经 a6/header.hpp 汇总头接入，详见 4.4.3⑥）
CPU 模拟器路径不定义任何 PTO_NPU_ARCH_* ──► ArchTraits 走 UNKNOWN 全能兜底
```

#### 4.2.3 源码精读

**宏的「产地」——三处构建脚本：**

- [tests/cpu/st/CMakeLists.txt:32](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/cpu/st/CMakeLists.txt#L32)：`add_definitions(-D__CPU_SIM)`——你在 u1-l2 跑的所有 CPU ST 测试，后端开关就是这一行。
- [tests/costmodel/st/CMakeLists.txt:21](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/costmodel/st/CMakeLists.txt#L21)：`add_definitions(-D__COSTMODEL -D__NPU_ARCH__=2201 -DPTO_COMM_NOT_SUPPORTED)`——CostModel 链路一次定义三个宏。
- [kernels/manual/a2a3/gemm_performance/CMakeLists.txt:15-28](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/manual/a2a3/gemm_performance/CMakeLists.txt#L15-L28)：同一份 GEMM 内核提供 `PTO_CPU_SIM_STANDALONE` 选项，打开后 `add_compile_definitions(__CPU_SIM)`——「一份内核源码，本地也能跑」的直接证据。
- NPU 侧不定义 `__CCE_AICORE__`，而是切换到 CCE 编译模式：[tests/npu/a2a3/src/st/CMakeLists.txt:76-84](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/npu/a2a3/src/st/CMakeLists.txt#L76-L84) 设置 `-xcce` 等编译选项，宏由该编译器内置。

**宏的「消费地」——arch_macro.hpp 的翻译表：**

- [include/pto/common/arch_macro.hpp:14-17](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/arch_macro.hpp#L14-L17)：CPU 模拟器下自动补定义 `__DAV_CUBE__`、`__DAV_VEC__`（模拟「Cube+Vector 齐全」的完整核形态，供后续代码分支使用）。
- [include/pto/common/arch_macro.hpp:19-38](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/arch_macro.hpp#L19-L38)：`__NPU_ARCH__` 数值到 `PTO_NPU_ARCH_*` 宏的完整映射（2201→A2A3；3101/3510→A5 且 3510 加 `PTO_URMA_SUPPORTED`；3113→KIRIN9030；3003→KIRINX90；5101→KIRINDEV0000；9201→A6）。其中 A6 分支见 [include/pto/common/arch_macro.hpp:35-37](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/arch_macro.hpp#L35-L37)：`9201` 同时定义 `PTO_COMM_NOT_SUPPORTED` 与 `PTO_NPU_ARCH_A6`——和 Kirin 系列一样，A6 当前不接入通信指令族。

**宏的「能力面」——arch_capability.hpp：**

- [include/pto/common/arch_capability.hpp:20-27](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/arch_capability.hpp#L20-L27)：`ChipArch` 枚举列出全部支持的代际。
- [include/pto/common/arch_capability.hpp:81-98](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/arch_capability.hpp#L81-L98)：为 A2A3、A5 分别特化 `ArchTraits` 并起别名 `CurrArch`——注意 A5 特化继承自 `ArchTraitsFp4Capable`（支持 FP4/FP8/MX 布局），A2A3 只有 BF16 等，能力差异在类型系统里显式表达。
- [include/pto/common/arch_capability.hpp:124-141](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/arch_capability.hpp#L124-L141)：当没有任何 `PTO_NPU_ARCH_*` 时（正是 CPU 模拟器的情形），走 `ChipArch::UNKNOWN` 特化——所有能力开关全开。**这就是 CPU 模拟器能编译全部指令家族（包括 A5 专属的 MX 指令）的原因**：它在类型层面自认「全能」，真正的行为差异由 `pto/cpu/` 的实现兜底。

**CostModel 的架构硬约束：**

- [include/pto/costmodel/common/arch_select.hpp:13-21](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/costmodel/common/arch_select.hpp#L13-L21)：`__NPU_ARCH__` 未定义直接 `#error`，且只接受 2201（A2/A3）与 3101/3510（A5）两档——CostModel 是「带架构参数的模拟」，必须明确模拟哪一代。

#### 4.2.4 代码实践

**实践目标**：亲手找到仓库中定义后端宏的所有「产地」，把 4.2.1 的表格补全成有文件行号证据的速查表。

**操作步骤**：

```bash
# 1. 找 CPU 宏的所有定义点
grep -rn "D__CPU_SIM" tests/ kernels/ demos/ build.sh
# 2. 找 CostModel 宏的定义点
grep -rn "D__COSTMODEL" tests/
# 3. 找 NPU 构建切换到 CCE 编译器的位置（它不定义 __CCE_AICORE__，而是换编译模式）
grep -rn "xcce" tests/npu/ | head
# 4. 看 __NPU_ARCH__ 在仓库内被消费的位置
grep -rn "__NPU_ARCH__" include/pto/common/arch_macro.hpp include/pto/costmodel/common/arch_select.hpp
```

**需要观察的现象**：每条 grep 命中行的文件与行号；注意 `D__CPU_SIM` 在 kernels/ 与 demos/ 下的命中是「可选独立构建」形态（`option(...STANDALONE...)` + `add_compile_definitions(__CPU_SIM)`）。

**预期结果**：你会得到与 4.2.3 所列完全一致的证据链——CPU 宏产地最多（测试、demo、内核三处），CostModel 宏永远与 `__NPU_ARCH__` 成对出现，NPU 路径完全没有 `-D__CCE_AICORE__`。

**待本地验证**：grep 的完整命中清单（与仓库当前状态相关）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `tests/costmodel/st/CMakeLists.txt` 要同时定义 `__COSTMODEL` 和 `__NPU_ARCH__=2201`，而 CPU 测试只定义 `__CPU_SIM` 一个宏？

**答案**：CostModel 要模拟具体一代芯片的流水线时序，`arch_select.hpp` 明确要求 `__NPU_ARCH__`，否则 `#error`（[include/pto/costmodel/common/arch_select.hpp:13-14](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/costmodel/common/arch_select.hpp#L13-L14)）。CPU 模拟器不模拟特定代际时序，无任何 `PTO_NPU_ARCH_*` 时 `ArchTraits` 走 UNKNOWN 全能兜底（[include/pto/common/arch_capability.hpp:124-141](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/arch_capability.hpp#L124-L141)），因此不需要该宏。

**练习 2**：`PTO_COMM_NOT_SUPPORTED` 出现在 CostModel 构建里，它最终影响了哪条 include 链？

**答案**：它会让 [include/pto/common/pto_instr.hpp:78-80](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp#L78-L80) 跳过 `pto/comm/pto_comm_inst.hpp`（通信指令库）的包含——即整个通信指令族在该构建里不存在。

**练习 3**：CPU 模拟器编译的代码里出现 `PTO_NPU_ARCH_A5` 吗？

**答案**：默认不会。该宏由 `arch_macro.hpp` 仅在 `__NPU_ARCH__==3101/3510` 时定义，而 CPU 构建不设置 `__NPU_ARCH__`。所以 CPU 模拟器下 `#if defined(PTO_NPU_ARCH_A5) || defined(__CPU_SIM)` 这类条件（`pto_instr.hpp` 中大量出现）总是靠 `__CPU_SIM` 那一项为真——指令声明层用「架构或模拟器」的并集来决定某指令是否可用。

### 4.3 cpu_stub：把「昇腾世界」翻译成本机 C++

#### 4.3.1 概念说明

stub（替身/桩）是指：为一个真实环境里的符号，在另一个环境里提供一个「长得一样但行为简化」的替代实现。CPU 模拟器要让**为 NPU 写的代码**原封不动地在普通 g++ 下编译运行，需要三类替身（全部集中在 [include/pto/common/cpu_stub.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/cpu_stub.hpp#L11-L12)）：

1. **语法替身（空宏）**：把 `__gm__`、`__ubuf__`、`__aicore__` 等编译器关键字定义成空——让代码「能编译」。
2. **同步替身（空函数）**：把 `set_flag`/`wait_flag`/`pipe_barrier` 等硬件同步原语变成什么都不做的 inline 函数——让代码「能跑」。u1-l4 的结论在此落到了实锤：CPU 模拟器不检查事件链错误。
3. **运行时替身（host 模拟）**：把 `aclrtMalloc`/`aclrtMemcpy` 等 CANN 运行时接口映射到 `calloc`/`memcpy`，再补一套 `pto::cpu_sim` 命名空间的多线程启动器——把「设备」这件事本身模拟出来。

官方文档 [docs/coding/cpu_sim.md:8-11](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/coding/cpu_sim.md#L8-L11) 明确了它的定位：定义 `__CPU_SIM` 即可启用 CPU 后端，且「为兼容 NPU 程序，部分昇腾专有函数在 cpu_stub.hpp 中提供了 CPU 实现；把它包含进已有的 NPU 程序，只需极小改动即可在 CPU 上编译」。

值得一提的是：**CostModel 后端也复用了这套替身**——[include/pto/costmodel/common/qualifiers.hpp:13](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/costmodel/common/qualifiers.hpp#L13) 第一行就是 `#include <pto/common/cpu_stub.hpp>`，随后再叠加 CostModel 自己的 aclrt 假实现（cpu_stub 内部用 `#if !defined(__COSTMODEL)` 留出了避让，如 [include/pto/common/cpu_stub.hpp:66](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/cpu_stub.hpp#L66)）。

#### 4.3.2 核心流程

一份 NPU 风格的内核在 CPU 上「被翻译」的全过程：

```text
源码里的写法                 cpu_stub 提供的替身                实际效果
─────────────────────────────────────────────────────────────────────
AICORE void runTAdd(...)   #define AICORE（空，type.hpp:16      普通函数
                            在 __CPU_SIM 下展开为空）
__gm__ T __out__* out      #define __gm__ / __out__（空）        普通指针 T* out
__ubuf__ DType*            #define __ubuf__（空）                普通指针
set_flag(PIPE_MTE2,...)    inline 空函数 + PIPE_* 常量           什么也不做
wait_flag(PIPE_V,...)      inline 空函数                         什么也不做
EVENT_ID0                  #define EVENT_ID0 0                  整数 0
aclrtMalloc(&p,sz,…)       calloc                               进程堆内存
aclrtMemcpy(d,s,src,…)     std::memcpy                          本机内存拷贝
block_idx 相关语义          thread_local ExecutionContext        每线程一个核编号
多核 launch                 LaunchKernelMultiCore → std::thread  真实多线程扇出
```

于是「TASSIGN→TLOAD→TADD→TSTORE」这条链在 CPU 上退化为「往堆上的模拟 UB/L1/L0 数组里做普通的读、算、写」——数值语义保真，时序语义（流水线并行、事件握手）不保真。这正是 u1-l4 提醒过的：事件链错误必须上 NPU/sim 才会暴露，[docs/coding/Event.md:7](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/docs/coding/Event.md#L7) 也写明 CPU 模拟器把事件同步视为 no-op。

#### 4.3.3 源码精读

**① 语法替身：一批空宏**

- [include/pto/common/cpu_stub.hpp:36-49](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/cpu_stub.hpp#L36-L49)：`__global__`、`AICORE`、`__aicore__`、`__gm__`、`__out__`、`__in__`、`__ubuf__`、`__cbuf__`、`__ca__`、`__cb__`、`__cc__`、`__fbuf__`、`__biasbuf__`、`__tf__` 全部定义为空。tadd 内核里的 `__gm__ T __out__*` 在 CPU 上就是 `T*`。
- [include/pto/common/cpu_stub.hpp:184-191](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/cpu_stub.hpp#L184-L191)：`EVENT_ID0`~`EVENT_ID7` 就是整数 0~7。

**② 同步替身：空函数与流水线常量**

- [include/pto/common/cpu_stub.hpp:51-62](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/cpu_stub.hpp#L51-L62)：`pipe_t`/`event_t` 类型和 `PIPE_S/PIPE_V/PIPE_MTE1/PIPE_MTE2/PIPE_MTE3/...` 常量照常提供，`pipe_barrier` 为空——类型系统在，行为不在。
- [include/pto/common/cpu_stub.hpp:123-124](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/cpu_stub.hpp#L123-L124)：`set_flag`/`wait_flag` 是**连参数都不接收的空 inline 函数**（`(pipe_t, pipe_t, int)` 形参无名）。u1-l4 里 tadd 内核第 36-40 行的那两组事件握手，在 CPU 上执行到这里直接返回。
- [include/pto/common/cpu_stub.hpp:138-140](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/cpu_stub.hpp#L138-L140)：cache 维护指令 `dcci`/`dsb` 同样为空。

**③ 运行时替身：malloc 版 aclrt 与模拟多核**

- [include/pto/common/cpu_stub.hpp:74-110](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/cpu_stub.hpp#L74-L110)：`aclrtMallocHost` 就是 `calloc`，`aclrtMemcpy` 就是 `std::memcpy`（取 `min(szDst, szSrc)`），`aclrtMemset` 是带参数校验的 `std::fill_n`。
- [include/pto/common/cpu_stub.hpp:196-205](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/cpu_stub.hpp#L196-L205)：`RuntimeConfig` 保存模拟器全局状态（核数、trace 开关、launch 计数器、互斥锁）。
- [include/pto/common/cpu_stub.hpp:261-278](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/cpu_stub.hpp#L261-L278)：`InitializeRuntime` 读三个环境变量——`PTO_CPU_SIM_NUM_CORES`（默认 4）、`PTO_CPU_SIM_TRACE_ENABLE`、`PTO_CPU_SIM_TRACE_DIR`（默认 `cpu_sim_traces`）。这是「用环境变量调模拟器」的官方入口。
- [include/pto/common/cpu_stub.hpp:361-421](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/cpu_stub.hpp#L361-L421)：`LaunchKernelMultiCore`——CPU 模拟器里最「像硬件」的一段：按 `ResolveActiveCoreCount` 算出活跃核数，为每个核起一个 `std::thread`，在每个线程里 `set_execution_context(block_idx, ...)` 后调用用户内核 lambda，最后 join、聚合异常、可选地把每个核的指令 trace 写成 `trace.jsonl`。真实调用者示例见 [kernels/custom/fused_add_relu_mul/fused_add_relu_mul_kernel.cpp:253-262](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/kernels/custom/fused_add_relu_mul/fused_add_relu_mul_kernel.cpp#L253-L262)。
- [include/pto/common/cpu_stub.hpp:516-553](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/cpu_stub.hpp#L516-L553)：`aclInit` 退化为 `InitializeRuntime()`，`get_block_idx()` 读取每线程的 `ExecutionContext`——多核切分语义（u3-l3 的主角）在 CPU 上靠线程局部变量扮演。
- 通信桩：[include/pto/common/cpu_stub.hpp:160-182](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/cpu_stub.hpp#L160-L182) 提供 `HcclRootInfo`/`CommDeviceContext` 等骨架，供通信指令头文件编译通过。

#### 4.3.4 代码实践

**实践目标**：用「删掉同步、结果不变」的实验，亲眼确认 cpu_stub 的同步替身是空函数，并理解其边界。

**操作步骤**：

1. 重新运行 tadd 的一个用例（u1-l4 已做过，此处关注机制而非结果）：

```bash
python3 tests/run_cpu.py -t tadd
```

2. 用 grep 确认你运行的内核里事件调用在 CPU 上的归宿：

```bash
grep -n "set_flag\|wait_flag" tests/cpu/st/testcase/tadd/tadd_kernel.cpp
grep -n "inline void set_flag\|inline void wait_flag" include/pto/common/cpu_stub.hpp
```

3. 思考实验（不必真改）：若把 `tadd_kernel.cpp` 第 36-37 行的 `set_flag`/`wait_flag` 整行删除再跑 CPU 测试，结果会怎样？在 NPU 上呢？
4. 进阶观察模拟器运行时开关（可选）：在任何使用 `LaunchKernelMultiCore` 的程序（如 fused_add_relu_mul 的 CPU 独立构建）前后设置环境变量对比：

```bash
PTO_CPU_SIM_NUM_CORES=1 <可执行文件>
PTO_CPU_SIM_NUM_CORES=8 <可执行文件>
ls cpu_sim_traces/   # 观察轨迹目录是否生成、launch 编号如何递增
```

**需要观察的现象**：步骤 1 测试通过；步骤 2 中 cpu_stub 里的两个函数体是 `{}`；步骤 4（若执行）不同核数下运行时间/trace 内容的差异。

**预期结果**：步骤 3 的答案——CPU 上删掉事件**结果不变且测试照样通过**（同步是空函数、单线程程序序天然有序）；NPU 上这是未定义行为，三条流水线并行执行时 TADD 可能读到未搬完的数据。**「CPU 模拟器只能验证数值语义」这句话从此有了源码级证据。**

**待本地验证**：步骤 4 的 trace 目录内容与耗时变化（依赖具体 demo 的构建）。

#### 4.3.5 小练习与答案

**练习 1**：`PIPE_MTE2`、`PIPE_V` 这些常量在 CPU 模拟器里明明「没用」（事件都是空的），为什么还要定义？

**答案**：为了让**同一份内核源码**无需 `#ifdef` 就能编译——内核作者写 `set_flag(PIPE_MTE2, PIPE_V, EVENT_ID0)`，这行代码在 NPU 与 CPU 上必须都能通过编译。stub 的职责是「类型与语法齐全、行为简化」，而不是删除概念。

**练习 2**：`LaunchKernelMultiCore` 里为什么要 `set_execution_context(block_idx, ...)` 再调用内核？

**答案**：内核里通过 `get_block_idx()` 获取当前核编号来做多核切分（[include/pto/common/cpu_stub.hpp:543-553](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/cpu_stub.hpp#L543-L553)）。CPU 上每个 `std::thread` 对应一个模拟核，`thread_local` 的 `ExecutionContext` 让每个线程读到自己的核号——用线程机制扮演硬件的多核上下文。

**练习 3**：CostModel 后端为什么也要包含 cpu_stub.hpp（经 qualifiers.hpp），而不是自己重写一份空宏？

**答案**：复用避免两处维护同一批「昇腾记号→空宏」的映射；同时 cpu_stub 用 `#if !defined(__COSTMODEL)` 把 aclrt 等会与 CostModel 自己的桩冲突的部分让出（[include/pto/common/cpu_stub.hpp:66](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/cpu_stub.hpp#L66)、[include/pto/common/cpu_stub.hpp:125](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/cpu_stub.hpp#L125)），实现「共享语法替身、各留运行时替身」。

### 4.4 common 公共层：一份声明，多套实现

#### 4.4.1 概念说明

「公共接口与后端实现分离」是 PTO-ISA 最重要的架构决定。它的含义是：

- **声明只有一份**，在 `include/pto/common/pto_instr.hpp`：`TADD`、`TLOAD` 等公共 API 的签名（模板参数、`WaitEvents` 变参、返回 `RecordEvent`）对所有后端完全一致——这就是「一份 Tile 抽象」。
- **实现按后端各归其位**：CPU 实现在 `include/pto/cpu/TAdd.hpp`，A2/A3 在 `include/pto/npu/a2a3/TAdd.hpp`，A5 在 `include/pto/npu/a5/TAdd.hpp`——这就是「多套实现」。
- **装配枢纽**是 `include/pto/common/pto_instr_impl.hpp`：它按架构宏批量 `#include` 对应目录的实现头，从而让「声明里的每个 `XXX_IMPL` 调用」都能解析到唯一一份实现。

设计动机有三：① 上层算子代码跨代际复用（A2/A3→A5 迁移不用改内核）；② 指令的约束检查可以集中在公共层（编译期 `static_assert`），三个后端共享；③ 新增一条指令时，各后端可以异步补齐（这正是 include/README.md 状态表里 Yes/TODO/No 并存的原因）。

#### 4.4.2 核心流程

以 TADD 为例，从用户调用到后端实现的完整链路（CPU 宏下）：

```text
用户代码: TADD(dstTile, src0Tile, src1Tile)
    │
    ▼ common/pto_instr.hpp:174-180  公共 API（唯一签名）
    template <...typename... WaitEvents...>
    PTO_INST RecordEvent TADD(dst, src0, src1, events...) {
        detail::PtoWaitEvents(events...);      // 先等待入参事件
        MAP_INSTR_IMPL(TADD, dst, src0, src1); // ← 展开点
        return {};
    }
    │
    ▼ MAP_INSTR_IMPL 宏（common/pto_instr.hpp:36-40，CPU 宏下）
    do {
        PtoInstrTraceScope _pto_instr_trace_scope("TADD", 1, dst, src0, src1); // CPU 专属：指令轨迹
        TADD_IMPL(dst, src0, src1);                                            // ← 真正的实现调用
    } while (0)
    │
    ▼ TADD_IMPL 由谁提供？→ common/pto_instr_impl.hpp 按宏装配：
    __CPU_SIM                                → include "pto/cpu/TAdd.hpp"       (cpu/TAdd.hpp:64)
    PTO_NPU_ARCH_A2A3 且非 __COSTMODEL      → include "pto/npu/a2a3/TAdd.hpp"  (a2a3/TAdd.hpp:81)
    PTO_NPU_ARCH_A5   且非 __COSTMODEL      → include "pto/npu/a5/TAdd.hpp"    (a5/TAdd.hpp:82)
    __COSTMODEL（含 __NPU_ARCH__=2201）     → 仍包含 npu/a2a3/TAdd.hpp 等实现头
                                              （由 costmodel 桩改写底层行为，见 4.4.3 末段）
```

同一个宏在 NPU 构建下展开更简单——`MAP_INSTR_IMPL(TADD, ...)` 直接变成 `TADD_IMPL(...)`（无 trace 桩），见 [include/pto/common/pto_instr.hpp:66-76](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp#L66-L76) 的 `#else` 分支。**公共 API 层一个字都不用改，后端差异全部被宏吸收。**

#### 4.4.3 源码精读

**① 声明层：common/pto_instr.hpp**

- [include/pto/common/pto_instr.hpp:14-25](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp#L14-L25)：声明层自己的条件包含——`pto/npu/a2a3/grid_intrinsic.hpp` 只在**非 CPU** 构建下拉入（`#ifndef __CPU_SIM`），而 `pto/cpu/trace.hpp` 只在 CPU 构建下拉入。声明层从第一行起就是「双面」的。
- [include/pto/common/pto_instr.hpp:29-76](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp#L29-L76)：`MAP_INSTR_IMPL` 宏族的两套定义。CPU 版多包一层 `PtoInstrTraceScope`（这就是 CPU 模拟器能产出指令轨迹的原因）；NPU 版直接转发。
- [include/pto/common/pto_instr.hpp:101-118](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp#L101-L118)：`TASSIGN` 的两个重载——运行时地址版与编译期地址版（后者触发 `tassign_static_check` 的静态边界/对齐检查）。「公共层集中放约束」的典型样例。
- [include/pto/common/pto_instr.hpp:174-180](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr.hpp#L174-L180)：`TADD` 公共签名，变参 `WaitEvents...` + 返回 `RecordEvent`（u3-l1 事件模型的主角）。

**② 装配枢纽：common/pto_instr_impl.hpp**

- [include/pto/common/pto_instr_impl.hpp:18-22](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr_impl.hpp#L18-L22)：`PTO_NPU_ARCH_A2A3` 且 `__COSTMODEL` 同时存在 → 包含**精简子集**的 a2a3 实现头（CostModel 只对已建模指令拉实现；该分支上方的注释还提到了供校准的占位系数）。
- [include/pto/common/pto_instr_impl.hpp:82-86](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr_impl.hpp#L82-L86)：`#else` 分支——真机 NPU 构建（A2/A3 代）包含**全量** a2a3 实现头（从 `TAssign.hpp`、`SyncAll.hpp`、`TAdd.hpp` 开始一路到一百多个）。
- [include/pto/common/pto_instr_impl.hpp:191-201](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr_impl.hpp#L191-L201)：A5 块同样的「CostModel 精简 / 真机全量」二分（本版本只调整了块内几个 include 的字母序，无行为变化）。
- [include/pto/common/pto_instr_impl.hpp:226-228](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr_impl.hpp#L226-L228)：真机分支里的 `__DAV_VEC__` 子条件——现在**只包住 `TCvt.hpp` 一个头**，`#endif` 紧随其后立即闭合（TCvt 是向量核形态专属的类型转换指令）。这个「立即闭合」正是本版本提交 `e773179d`（fix: Restore public header and CPU ST compilation）修复的关键，值得展开讲，见下方专栏。
- [include/pto/common/pto_instr_impl.hpp:317-319](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr_impl.hpp#L317-L319)：**本版本恢复的 A6 接入点**——`#ifdef PTO_NPU_ARCH_A6` → `pto/npu/a6/header.hpp`。A6（`__NPU_ARCH__==9201`）在这个装配枢纽里从此有了自己的入口，其内部结构见 4.4.3⑥。
- [include/pto/common/pto_instr_impl.hpp:321-329](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr_impl.hpp#L321-L329)：Kirin 三代各一个汇总头——与 A6 相同的「汇总头」接入模式。
- [include/pto/common/pto_instr_impl.hpp:331-343](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr_impl.hpp#L331-L343)：`TPrefetchAsync`（L2 异步预取）**只在真机构建**（`__CCE_AICORE__` 且非两个模拟宏）拉入——模拟后端连 API 都不提供的例子。注释（本版本重写过）说明两个架构包装头共享同一个架构无关的 SDMA 实现，SQE 字段差异在 SDMA helper 内部用 `#ifdef PTO_NPU_ARCH_A5` 处理。
- [include/pto/common/pto_instr_impl.hpp:345-435](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr_impl.hpp#L345-L435)：`__CPU_SIM` 块——第 346 行起连续包含约 80 个 `pto/cpu/*.hpp`（含 `pto/cpu/comm/` 下的通信实现），末尾 [include/pto/common/pto_instr_impl.hpp:432-433](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr_impl.hpp#L432-L433) 注明 `TPrefetchAsync` 在 CPU 上是「保 API 面」的 no-op。

**专栏：一个 `#endif` 如何砍掉整个后端（本版本修复的真实事故）**

把上一个版本（`0dbecbe`）的这份文件与当前版本对比，diff 只有一行核心差异：`TCvt.hpp` 之后补上了 `#endif`。但缺失它时发生了什么？我们可以用「条件配对」规则手工推演旧文件的嵌套（这是阅读大型条件编译头文件的基本功）：

```text
旧版本（0dbecbe，节选，行号为旧文件行号）
191  #ifdef PTO_NPU_ARCH_A5        ← 开
192  #ifdef __COSTMODEL / 197 #else ← 开
226    #ifdef __DAV_VEC__          ← 开，且后面没有自己的 #endif
         ...TCvt/TStore/...一路到块尾...
313    #endif // __COSTMODEL       ← 按配对规则，实际关闭的是 226 的 __DAV_VEC__！
314    #endif                      ← 关闭的是 192 的 __COSTMODEL/#else
316/319/322 Kirin 三块             ← 现在嵌在 PTO_NPU_ARCH_A5 内部
328    #if defined(__CCE_AICORE__) ← TPrefetchAsync 块也嵌在 A5 内部
337    #ifdef __CPU_SIM            ← CPU 块同样嵌在 A5 内部！
429    #endif                      ← 关闭的是 191 的 PTO_NPU_ARCH_A5
（最外层 10 行的 #ifndef PTO_INSTR_IMPL_HPP 直到文件结束都没有闭合）
```

后果有两层：其一，Kirin 块、TPrefetchAsync 块、`__CPU_SIM` 块全部被卷进 `#ifdef PTO_NPU_ARCH_A5` 的作用域——CPU 模拟构建不定义 `PTO_NPU_ARCH_A5`，于是**一个 `pto/cpu/` 实现头都拉不到**，`TADD_IMPL` 等符号全部缺失，CPU ST 编译失败（这正是修复提交标题里 "CPU ST compilation" 的出处）；其二，文件最外层的 include guard 无法闭合，多数预处理器会在此直接报错（"public header" 被破坏的另一层含义）。**在「条件编译即架构」的仓库里，一个 `#endif` 的错位等价于砍掉整个后端**——这是阅读装配头时必须保持的敏感度。

**③ 一份声明的三份实现（TADD 为证）**

- CPU：[include/pto/cpu/TAdd.hpp:64](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/cpu/TAdd.hpp#L64) 定义 `TADD_IMPL`（u4-l2 将精读其并行 for 循环模拟）。
- A2/A3：[include/pto/npu/a2a3/TAdd.hpp:81](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a2a3/TAdd.hpp#L81) 定义同名 `TADD_IMPL`（内部封装 CCE 的 `vadd` 内置指令，u4-l3 精读）。
- A5：[include/pto/npu/a5/TAdd.hpp:82](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a5/TAdd.hpp#L82) 又一份同名实现。
- 三者签名一致、命名一致、目录不同——「一份 Tile 抽象、多套实现」最直观的物证。

**④ 让同一份声明「双面兼容」的公共修饰宏（type.hpp）**

- [include/pto/common/type.hpp:13-23](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/type.hpp#L13-L23)：`AICORE` 只在真机构建展开为 CCE 属性 `[aicore]`，CPU/CostModel 下为空；`PTO_INST`/`PTO_INTERNAL` 由它拼装。于是同一个函数声明在 NPU 上带设备属性、在 CPU 上是普通函数。
- [include/pto/common/type.hpp:66-96](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/type.hpp#L66-L96)：`PTO_CPU_ASSERT`（运行期断言）只在 CPU/CostModel 生效，真机构建退化为 `((void)0)`——**模拟后端承担更多运行期检查、真机构建零开销**的分工。

**⑤ CostModel 的「顶替术」**

- [include/pto/costmodel/pto_instr.hpp:10-15](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/costmodel/pto_instr.hpp#L10-L15)：注释写明「Intentionally reuse the common PTO include guard so this header can act as a drop-in replacement」——它复用了 `common/pto_instr.hpp` 的 include guard `PTO_INSTR_HPP`。因此当 `pto-inst.hpp` 在 CostModel 宏下先包含它时，即使后续有代码再包含 common 版，也会被同一 guard 挡掉。**用 include guard 实现「无侵入替换」**是本仓库一个漂亮的技巧。
- [include/pto/costmodel/pto_instr.hpp:34-37](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/costmodel/pto_instr.hpp#L34-L37)：把 `TSTORE_FP_IMPL` 等别名到 `TSTORE_IMPL`——CostModel 不区分 Fixpipe 专用路径的又一处「声明层微调」。
- [include/pto/costmodel/runtime_stub.hpp:13-16](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/costmodel/runtime_stub.hpp#L13-L16)：CostModel stub 的四个组成（qualifiers/aclrt_stub/runtime_util/arch_select），是 4.1 中第二段 `#elif` 分支拉入的全部内容。

**⑥ A6 的「汇总头 + 跨代复用」接入方式（本版本恢复）**

A2A3 与 A5 的指令头由装配枢纽逐条罗列（见上面两个大块），而 A6 走了另一条路：枢纽里只有一行 `#include "pto/npu/a6/header.hpp"`（[include/pto/common/pto_instr_impl.hpp:317-319](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr_impl.hpp#L317-L319)），具体包含哪些指令由这个「汇总头」自己决定：

- [include/pto/npu/a6/header.hpp:20-21](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a6/header.hpp#L20-L21)：注释直接写明分工——「A6 uses dedicated TLoad/TExtract/TMatmul implementations, while some other instructions still reuse A5」（A6 为 TLoad/TExtract/TMatmul 提供专用实现，其余指令仍复用 A5）。
- 专用实现一支：[include/pto/npu/a6/header.hpp:17-18](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a6/header.hpp#L17-L18) 的 `a6/datatype.hpp`、`a6/TSync.hpp`，加上 [include/pto/npu/a6/header.hpp:23-32](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a6/header.hpp#L23-L32) 中的 `a6/SyncAll.hpp`、`a6/TLoad.hpp`、`a6/TExtract.hpp`、`a6/TMatmul.hpp`、`a6/TReshape.hpp`、`a6/TQuant.hpp` 及 `a6/common.hpp`/`a6/utils.hpp`——目录下共 11 个文件。
- 跨代复用一支：[include/pto/npu/a6/header.hpp:22-26](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a6/header.hpp#L22-L26) 里夹着的 `pto/npu/a5/TAssign.hpp`、`pto/npu/a5/TAdd.hpp`、`pto/npu/a5/TStore.hpp`——**A6 直接包含 A5 的实现头**（A6 的专用头内部也会拉 a5 的 `common.hpp`/`utils.hpp` 基础设施，如 [include/pto/npu/a6/TMatmul.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a6/TMatmul.hpp) 的 include 列表）。

这展示了「一份 Tile 抽象、多套实现」的第三种形态：**新代际不必维护全套实现，只重写有差异的指令，其余按头文件级复用上一代**。A6 目录当前只有少量指令头，是一个正在「逐步长出实现」的后端；同时注意 A6 与 Kirin 系列一样定义了 `PTO_COMM_NOT_SUPPORTED`（[include/pto/common/arch_macro.hpp:35-37](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/arch_macro.hpp#L35-L37)），即不接入通信指令族。

#### 4.4.4 代码实践

**实践目标**：沿「声明 → 装配 → 实现」把 TADD 的三份实现亲手找出来，验证公共 API 的唯一性。

**操作步骤**：

```bash
# 1. 看公共声明（唯一签名）
grep -n "RecordEvent TADD" include/pto/common/pto_instr.hpp
# 2. 看 MAP_INSTR_IMPL 如何把 TADD 连到 TADD_IMPL
grep -n "MAP_INSTR_IMPL(TADD" include/pto/common/pto_instr.hpp
grep -n "define MAP_INSTR_IMPL(" include/pto/common/pto_instr.hpp
# 3. 找出 TADD_IMPL 的全部定义点（应为 3 个后端各一份）
grep -rn "void TADD_IMPL" include/pto/
# 4. 验证装配条件：在 pto_instr_impl.hpp 中找到三份实现各自被 include 的行
grep -n "TAdd.hpp" include/pto/common/pto_instr_impl.hpp
# 5. A6 接入验证（本版本恢复的入口）：在 NPU 分支下追加架构宏，预处理装配枢纽本身
g++ -E -x c++ -Iinclude -D__CCE_AICORE__ -DPTO_NPU_ARCH_A6 -std=c++20 \
  include/pto/common/pto_instr_impl.hpp | grep -oE '"[^"]*pto/npu/a6/[^"]*"' | sort -u
```

**需要观察的现象**：步骤 3 命中恰好三行（`cpu/TAdd.hpp`、`npu/a2a3/TAdd.hpp`、`npu/a5/TAdd.hpp`）；步骤 4 显示 `pto/cpu/TAdd.hpp` 出现在 `#ifdef __CPU_SIM` 块（第 345 行起）、两份 NPU 版各自出现在 `PTO_NPU_ARCH_A2A3`/`PTO_NPU_ARCH_A5` 块；步骤 5 输出 `pto/npu/a6/` 目录的全部 11 个头文件（header/datatype/TSync/SyncAll/common/utils/TLoad/TExtract/TMatmul/TReshape/TQuant）。

**预期结果**：你完成了一次零运行成本的「调用链跟踪」——这正是阅读大型 header-only 库的基本功：**找声明、找宏展开、找装配条件、找实现**。u4 全单元的指令精读都会重复这条路径。步骤 5 的两点说明：① A6 块（装配枢纽 L317-319）只以 `PTO_NPU_ARCH_A6` 为条件，因此直接 `-DPTO_NPU_ARCH_A6` 就能命中，加 `-D__CCE_AICORE__` 是为了贴近真实 NPU 分支的宏环境；② 也可以走「正规」路线——从统一入口进入并用 `-D__NPU_ARCH__=9201` 让 `arch_macro.hpp` 自动推导出 A6 宏（见 5.2 组合 E）。A6 的专用头与它复用的 a5 头的全部依赖都在仓库内部或标准库（`cstdint` 等），纯预处理不需要 CANN 环境。

**待本地验证**：步骤 5 的 g++ 命令输出（本讲义撰写环境的沙箱不可执行 g++；预期清单已由源码静态核对）。

#### 4.4.5 小练习与答案

**练习 1**：`costmodel/pto_instr.hpp` 复用 common 版的 include guard（都叫 `PTO_INSTR_HPP`），这解决了什么问题？如果两者 guard 名不同会怎样？

**答案**：解决「替换而非叠加」的问题：`pto-inst.hpp` 在 CostModel 宏下先包含 costmodel 版，guard 已占用；若此后任何头再试图包含 common 版（传递依赖中完全可能），会因 guard 已定义而整段跳过，保证声明层唯一。若 guard 不同，两个声明层都会被包含，同一批指令 API 将被重复定义，直接编译错误。

**练习 2**：为什么 `TPrefetchAsync` 只在真机构建中拉入（[include/pto/common/pto_instr_impl.hpp:331-343](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr_impl.hpp#L331-L343)），而 CPU 块末尾却又包含了一个 `pto/cpu/TPrefetchAsync.hpp`（[include/pto/common/pto_instr_impl.hpp:432-433](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr_impl.hpp#L432-L433)）？

**答案**：真机的异步 L2 预取走 SDMA CMO 专有路径，模拟后端没有对应硬件语义，故用条件编译把真机实现严格限定在 `__CCE_AICORE__` 且非模拟宏的构建里；同时为了让内核源码在 CPU 上也能编译（API 面一致），CPU 块提供了一个 no-op 版本——注释明确写着「no-op on CPU sim - kept for API surface compatibility」。**实现可以有后端差异，但 API 面尽量对齐**。

**练习 3**：请用一句话向同事解释「为什么 PTO 要把指令声明放在 common、实现分散到 cpu/npu 各目录，而不是每个后端各写一套从头到尾的 API？」

**答案**：因为上层内核只应依赖「稳定的指令签名」，把签名收敛到 common 一份，后端只填充 `*_IMPL`，才能做到换芯片代际（A2/A3→A5）或换运行方式（真机→CPU 仿真→CostModel 估算）时上层算子代码零改动，同时让编译期约束检查（static_assert）三个后端共享一份。

## 5. 综合实践

**任务：画出并机器验证 `pto-inst.hpp` 的宏组合依赖图。**

这是本讲的压轴实践，把 4.1 的手工推导交给预处理器裁决。

### 5.1 实践目标

1. 用 `g++ -E`（只预处理、不编译）获取三种宏组合下实际展开的 PTO 头文件全集。
2. 与你 4.1.4 的手绘图对比，修正理解。
3. 体会「NPU 路径无法在本机验证」的原因与替代手段。

### 5.2 操作步骤

在仓库根目录执行（命令中的 `grep`/`sort`/`wc` 用于从 `# 行号 "文件"` 形式的行标记中提取去重后的 PTO 头文件清单）：

```bash
# 组合 A：无任何宏（只应得到类型基础层）
echo '#include <pto/pto-inst.hpp>' | g++ -E -std=c++20 -Iinclude -x c++ - \
  | grep '^# ' | grep -oE '"[^"]*/pto/[^"]*"' | sort -u

# 组合 B：CPU 模拟器
echo '#include <pto/pto-inst.hpp>' | g++ -E -D__CPU_SIM -std=c++20 -Iinclude -x c++ - \
  | grep '^# ' | grep -oE '"[^"]*/pto/[^"]*"' | sort -u > /tmp/cpu_sim_headers.txt
wc -l /tmp/cpu_sim_headers.txt          # 预期：上百个（pto/cpu 全家族）
grep -c 'pto/cpu/' /tmp/cpu_sim_headers.txt   # 其中 pto/cpu/ 实现头的数量

# 组合 C：CostModel（模拟 A2/A3 代）
echo '#include <pto/pto-inst.hpp>' | g++ -E -D__COSTMODEL -D__NPU_ARCH__=2201 -std=c++20 -Iinclude -x c++ - \
  | grep '^# ' | grep -oE '"[^"]*/pto/[^"]*"' | sort -u > /tmp/costmodel_headers.txt
wc -l /tmp/costmodel_headers.txt
grep 'costmodel' /tmp/costmodel_headers.txt | head

# 组合 D（延伸）：用 g++ 模拟 NPU 宏组合，观察「只给 __CCE_AICORE__ 不给 __NPU_ARCH__」的差别
echo '#include <pto/pto-inst.hpp>' | g++ -E -D__CCE_AICORE__ -std=c++20 -Iinclude -x c++ - \
  | grep '^# ' | grep -cE '"/?pto/(npu|cpu)/'      # 预期：几乎没有后端实现头
echo '#include <pto/pto-inst.hpp>' | g++ -E -D__CCE_AICORE__ -D__NPU_ARCH__=2201 -std=c++20 -Iinclude -x c++ - \
  | grep '^# ' | grep -cE 'pto/npu/a2a3/'          # 预期：拉入整个 npu/a2a3 家族

# 组合 E（延伸）：NPU 分支 + A6 架构宏——本版本恢复的接入点（汇总头 + 跨代复用）
echo '#include <pto/pto-inst.hpp>' | g++ -E -D__CCE_AICORE__ -D__NPU_ARCH__=9201 -std=c++20 -Iinclude -x c++ - \
  | grep '^# ' | grep -oE '"[^"]*/pto/npu/(a6|a5)/[^"]*"' | sort -u
```

说明：`-E` 只做预处理，不会因为 `__ubuf__` 这类 g++ 不认识的记号报错（它们要到编译阶段才会被检查），因此以上命令在只有 g++ 的机器上也能跑通；`-Iinclude` 对应真实构建里指向 `include/` 的包含路径（对照 [tests/cpu/st/testcase/CMakeLists.txt:18-23](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/tests/cpu/st/testcase/CMakeLists.txt#L18-L23) 的 `target_include_directories`）。

### 5.3 需要观察的现象与预期结果

基于对源码的静态分析（本讲义撰写环境未能执行 g++，以下结论均由 4.1-4.4 的条件编译逻辑推出，**数量请以本地运行为准——待本地验证**）：

- **组合 A**：仅约 4-5 个 PTO 头——`pto-inst.hpp`、`common/type.hpp`、`common/kernel_meta.hpp`、`common/memory.hpp`、`common/arch_macro.hpp`（后两者是 `memory.hpp` 的传递依赖），无任何指令与 Tile 类型。
- **组合 B**：额外出现 `common/cpu_stub.hpp` → `cpu/MXTypes.hpp`、`cpu/Hifloat8.hpp`、`cpu/trace.hpp`；再经 `arch_capability.hpp`、`pto_tile.hpp`（其 CPU 专属包含 `cpu/atomic.hpp`，见 [include/pto/common/pto_tile.hpp:18-24](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_tile.hpp#L18-L24)）、`common/pto_instr.hpp` → `pto_instr_impl.hpp` → **约 80+ 个 `pto/cpu/*.hpp`**（对应 [include/pto/common/pto_instr_impl.hpp:345-435](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/common/pto_instr_impl.hpp#L345-L435) 的连续 include），最后还有 `comm/pto_comm_inst.hpp` 一支。
- **组合 C**：出现 `costmodel/runtime_stub.hpp` → `costmodel/common/*` → `cpu_stub.hpp`（复用！）、`costmodel/a2a3/cce_costmodel.hpp`（由 arch_select 选择）、`costmodel/pto_instr.hpp`、`costmodel/perf_sim/*`；注意其中 **也会包含 `pto/npu/a2a3/` 的部分实现头**（CostModel 精简子集，见 4.4.3②）。
- **组合 D 第一步**：只有 `__CCE_AICORE__` 而无 `__NPU_ARCH__` 时，`pto_instr_impl.hpp` 中所有架构块都不命中——**一个后端实现头都不会拉入**。这证明 NPU 构建对 ccec 内置 `__NPU_ARCH__` 的硬依赖；补上 `-D__NPU_ARCH__=2201` 后 `pto/npu/a2a3/` 全家族出现。同时提醒：g++ 只能「预处理」这条路径，真正编译 A2/A3 实现需要 CCE 编译器（实现里用了 `vadd` 等内置指令）。
- **组合 E**：`arch_macro.hpp` 把 9201 翻译成 `PTO_NPU_ARCH_A6`（并附赠 `PTO_COMM_NOT_SUPPORTED`，通信指令族被跳过），装配枢纽经 [include/pto/npu/a6/header.hpp](https://github.com/hw-native-sys/pto-isa/blob/be5ccb765a4ce5d14ca5da8b0e2f182d7f003369/include/pto/npu/a6/header.hpp) 汇总头拉入 a6 目录全部 11 个文件，**同时**出现被复用的 a5 头（`TAssign.hpp`/`TAdd.hpp`/`TStore.hpp` 及其传递依赖 `a5/common.hpp`/`a5/utils.hpp`）——「跨代复用」在头文件级直接可见。

### 5.4 把结果沉淀下来

把四组输出整理成一张「宏组合 × 头文件集合」对照表贴进你的学习笔记，并标注三个关键连接点：stub 的位置（在 `memory.hpp` 之前）、声明层的选择（common vs costmodel）、装配枢纽的条件块（`pto_instr_impl.hpp` 的五个 `#ifdef` 区段）。这张表将是你日后阅读任何一条 PTO 指令实现时的「定位地图」。

## 6. 本讲小结

- `pto-inst.hpp` 是 PTO 唯一推荐的统一入口，仅 33 行，用三段式结构完成「公共基础 → stub 选择 → 指令层装配」；上层内核只需 `#include <pto/pto-inst.hpp>`。
- 后端由编译期宏决定：`__CPU_SIM`（CPU 模拟器，构建脚本显式定义）、`__CCE_AICORE__`（NPU 真机，CCE 编译器内置）、`__COSTMODEL`（性能模型，与 `__NPU_ARCH__` 成对定义）；`__NPU_ARCH__` 再把 NPU/CostModel 细分到 A2/A3、A5、Kirin、A6 各代实现目录——其中 A6（9201，本版本恢复接入）走「汇总头」模式，并像 Kirin 一样不接入通信指令族。
- `cpu_stub.hpp` 用三类替身（空宏、空同步函数、malloc 版 aclrt + 多线程启动器）让 NPU 风格代码在本机编译运行；`set_flag`/`wait_flag` 在 CPU 上是空函数——这是「CPU 模拟器只验证数值语义、不验证时序」的源码级根源；CostModel 的 stub 也复用了它。
- 公共层与实现层严格分离：指令签名只存在于 `common/pto_instr.hpp`，`MAP_INSTR_IMPL` 宏把它接到 `*_IMPL`；`pto_instr_impl.hpp` 按架构宏批量装配 `cpu/`、`npu/a2a3/`、`npu/a5/` 的实现，`TADD_IMPL` 三份实现并存是「一份 Tile 抽象、多套实现」的直接物证；A6 则展示第三种形态——只重写有差异的指令（TLoad/TExtract/TMatmul 等），其余在头文件级复用 A5。
- 本版本修复的 `__DAV_VEC__` 保护事故是最佳反面教材：装配枢纽里一个 `#endif` 的缺失会让后续所有条件块整体错位一层，CPU 模拟构建因此拉不到任何 `pto/cpu/` 实现头——在「条件编译即架构」的仓库里，改装配头必须逐个核对 `#if/#endif` 配对。
- CostModel 版指令头通过**复用 include guard** 实现 drop-in 替换，是条件编译架构下「无侵入替换声明层」的代表性技巧。
- `g++ -E` 是观察这类 header-only 库装配关系的利器：只预处理、不编译，因此能在无昇腾环境的机器上验证 include 依赖图；但 NPU 路径的真正编译仍依赖 CCE 编译器。

## 7. 下一步学习建议

本讲完成了单元一（走进 PTO-ISA）的全部内容。从下一讲起进入单元二「核心数据抽象」，建议按以下顺序继续：

1. **u2-l1 类型系统与公共常量**：本讲多次路过 `type.hpp`（`PTO_INST`、`PTO_CPU_ASSERT`、`AICORE` 宏），下一讲正面精读它定义的 dtype 体系（half/bfloat16/int4b_t 等）与 `constants.hpp`。
2. **u2-l2 GlobalTensor 与 u2-l3 Tile 深度剖析**：`pto_tile.hpp` 在本讲只作为「第三段装配的成员」出现，后续两讲将进入其内部的 `Shape`/`Stride`/`GlobalTensor`/`Tile` 模板参数体系。
3. **u2-l4 TASSIGN 与片上内存规划**：把本讲 4.3 的「模拟内存」与真实的 UB/L0 地址规划连接起来。
4. 若你对本讲的宏装配机制意犹未尽，可以先做两个延伸阅读：`include/pto/costmodel/lightweight_costmodel.hpp`（CostModel 后端的完整装配面）与 `docs/coding/cpu_sim.md`（CPU 内存模型的官方描述），为 u7-l4 的 CostModel 讲义预热。
5. 想观察「一个新代际后端如何逐步长出实现」，可以持续跟踪 `include/pto/npu/a6/` 目录——它当前只有 11 个文件，是「汇总头 + 跨代复用」的活样本；等后续版本为它补充更多指令头时，你可以用本讲 5.2 组合 E 的命令重新画一次依赖图。

带着本讲的「宏组合 × 头文件集合」地图去读后续讲义，任何一条指令你都能立刻定位它的声明、检查与各后端实现。
