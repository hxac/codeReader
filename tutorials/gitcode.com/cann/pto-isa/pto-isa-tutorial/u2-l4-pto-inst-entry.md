# 统一入口 pto-inst.hpp 与多后端架构切换

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `__CPU_SIM`、`__CCE_AICORE__`、`__COSTMODEL` 三个编译宏分别对应哪条运行路径，以及它们由谁定义。
2. 跟踪一次「包含 `pto/pto-inst.hpp`」之后发生的完整包含链，理解 common / cpu / npu 三层头文件的引用关系。
3. 理解 `arch_macro.hpp` 如何把 `__NPU_ARCH__` 数字翻译成 `PTO_NPU_ARCH_*` 架构宏，`arch_capability.hpp` 又如何在此基础上给出每代芯片的能力开关。
4. 理解指令接口层（`pto_instr.hpp`）与实现层（`pto_instr_impl.hpp` → `include/pto/cpu` 或 `include/pto/npu/<arch>`）之间通过 `XXX → XXX_IMPL` 宏转发粘合的机制。

本讲是单元二「编程模型核心」的收尾讲：前几讲讲了 GlobalTensor、Tile、事件三个数据/同步抽象，本讲回答「这些抽象和 90+ 条指令，是如何在同一份 kernel 源码下编译到 CPU 仿真、NPU 真机或 CostModel 三种后端的」。

## 2. 前置知识

- **编译宏即开关**：C/C++ 预处理器可以在编译前根据宏定义裁剪代码。PTO 是 header-only 库，同一份 kernel 源文件不改一行，只换编译宏，就能落到不同后端——这是「虚拟 ISA」落到工程上的核心手段。
- **三个后端宏**：
  - `__CPU_SIM`：CPU 仿真后端，用 gcc/clang 在普通电脑上编译运行，[docs/coding/cpu_sim.md](docs/coding/cpu_sim.md) 明确说明「设置 `__CPU_SIM` 编译定义即可启用，可用标准 CPU 编译器构建」。
  - `__CCE_AICORE__`：NPU 真机后端。它不是项目 CMake 手工定义的（在仓库所有 CMakeLists 中搜索不到），而是由昇腾 CCE 编译器在编译设备侧（kernel）代码时自动注入的预定义宏——仓库文档也按这个前提使用它，例如 [docs/coding/tutorial.md:147](docs/coding/tutorial.md) 中用 `#ifdef __CCE_AICORE__` 区分设备侧与仿真侧写法。
  - `__COSTMODEL`：性能模拟后端，不产出数值结果，只对指令序列做代价估算。
- **架构号 `__NPU_ARCH__`**：一个由构建系统传入的数字（如 2201、3101），标识目标芯片代际。CPU 仿真与 CostModel 也会显式指定一个架构号来模拟「某代芯片的行为」。
- **`XXX → XXX_IMPL` 转发**：上一讲见过的指令 API（如 `TADD`）定义在 common 层，真正干活的 `TADD_IMPL` 由后端头文件提供，common 层用宏 `MAP_INSTR_IMPL` 把前者拼接到后者。本讲会拆开这个粘合点。

如果你对「预处理宏裁剪代码」不熟悉，只需记住一句话：**PTO 的多后端支持 = 一棵以 `pto-inst.hpp` 为根、以宏为分支条件的包含树**。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/pto/pto-inst.hpp](include/pto/pto-inst.hpp) | 统一入口。用户 kernel 只需 `#include <pto/pto-inst.hpp>`，它按宏路由到各层头文件 |
| [include/pto/common/cpu_stub.hpp](include/pto/common/cpu_stub.hpp) | CPU 仿真环境的「地基」：把昇腾设备侧关键字/ACL 运行时函数替换成普通 C++ 等价物 |
| [include/pto/common/arch_macro.hpp](include/pto/common/arch_macro.hpp) | 把 `__NPU_ARCH__` 数字翻译为 `PTO_NPU_ARCH_A2A3/A5/A6/...` 架构宏，并做 CPU 仿真下的兼容补齐 |
| [include/pto/common/arch_capability.hpp](include/pto/common/arch_capability.hpp) | 每代芯片的能力特征（是否支持 Bf16/Fp8/Fp4/通信等）与类型别名，供接口层做编译期检查 |
| [include/pto/common/pto_instr.hpp](include/pto/common/pto_instr.hpp) | 指令接口层：90+ 条指令的公开 API 模板（`TADD`、`TLOAD`、`TMATMUL`……），统一做事件等待再转发 `_IMPL` |
| [include/pto/common/pto_instr_impl.hpp](include/pto/common/pto_instr_impl.hpp) | 实现路由层：按「架构宏 × 后端宏」批量 include 对应实现头文件 |
| tests/cpu/st/CMakeLists.txt、tests/costmodel/st/CMakeLists.txt | 真实证据：构建系统在哪里定义这些宏 |

## 4. 核心概念与源码讲解

### 4.1 编译期后端选择

#### 4.1.1 概念说明

「后端」在 PTO 里指这份 kernel 代码最终编译成什么：

- **CPU 仿真后端**（`__CPU_SIM`）：编译成跑在你电脑上的普通可执行文件。内存是宿主机内存模拟的，多核是单线程顺序模拟的，事件同步是空操作。用来验证算法逻辑。
- **NPU 真机后端**（`__CCE_AICORE__`）：由 CCE 编译器编译成昇腾设备上的机器码。真实的片上存储、多流水线、事件同步。
- **CostModel 后端**（`__COSTMODEL`）：编译成一个「记录指令、估算代价」的桩程序，不做真实数值计算。

关键设计：**选择发生在编译期，不是运行期**。没有虚函数、没有动态分发，`#ifdef` 直接把不相关后端的代码从编译产物里整段裁掉。代价为零，但一份二进制只属于一个后端。

#### 4.1.2 核心流程

一个 kernel 从 `#include <pto/pto-inst.hpp>` 开始的展开过程：

```text
#include <pto/pto-inst.hpp>
│
├─ 无条件引入公共类型：type.hpp / kernel_meta.hpp / memory.hpp
│
├─ 后端地基（二选一）：
│   ├─ defined(__CPU_SIM)    → common/cpu_stub.hpp     （设备关键字与 ACL 的桩替换）
│   └─ defined(__COSTMODEL)  → costmodel/runtime_stub.hpp
│      （__CCE_AICORE__ 真机构建不需要桩，CCE 工具链自带这些符号）
│
└─ 若定义了 __CPU_SIM / __CCE_AICORE__ / __COSTMODEL 之一：
    ├─ arch_macro.hpp      （__NPU_ARCH__ → PTO_NPU_ARCH_*）
    ├─ arch_capability.hpp （芯片能力特征）
    ├─ pto_tile.hpp        （Tile / GlobalTensor 编程模型，前三讲的主角）
    └─ pto_instr.hpp       （指令接口层）
        ├─ __COSTMODEL → costmodel/pto_instr.hpp
        └─ 否则        → common/pto_instr.hpp
              └─ pto_instr_impl.hpp → 按「架构 × 后端」批量 include
                    ├─ NPU 真机: npu/a2a3/*.hpp 或 npu/a5/*.hpp 或 npu/a6/header.hpp ...
                    ├─ CostModel: 同 npu/<arch> 头文件（插桩版）
                    └─ __CPU_SIM: cpu/*.hpp（纯 C++ 仿真实现）
```

注意顶层守卫的巧妙之处：如果三个宏一个都没定义（例如纯 host 侧代码只想要类型定义），`pto-inst.hpp` 依然可用，只是不引入指令层。

#### 4.1.3 源码精读

先看入口本体的宏路由，全文只有 30 余行，却是整个仓库的「总开关面板」：

[include/pto/pto-inst.hpp:16-33](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/pto-inst.hpp#L16-L33) —— 先按 `__CPU_SIM` / `__COSTMODEL` 引入后端地基桩（真机构建走 `__CCE_AICORE__` 时两个分支都不命中），再在「三者任一已定义」的守卫下引入架构宏、能力特征、Tile 编程模型与指令层；CostModel 用自己的指令头，其余用 common 版。

再看 CPU 仿真地基 `cpu_stub.hpp` 做了哪三类替换：

[include/pto/common/cpu_stub.hpp:28-52](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp#L28-L52) —— 第一类：把昇腾设备侧关键字全部定义为空宏。`__gm__`、`AICORE`、`__ubuf__` 这些在 CCE 编译器里有特殊含义的标注，在 gcc/clang 下被「抹掉」，源码就能原样通过普通编译器。同时用 `int` 和常量模拟出 `PIPE_S/PIPE_V/PIPE_MTE2/...` 流水线编号，让上一讲的 `pipe_barrier(pipe_t)` 可以有签名。

[include/pto/common/cpu_stub.hpp:118-119](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp#L118-L119) —— 第二类：事件原语的空桩。`set_flag` / `wait_flag` 被定义为什么都不做的 inline 函数，这正是 u2-l3 讲过的「CPU 仿真下单线程按序执行，同步是 no-op」的源头。

[include/pto/common/cpu_stub.hpp:69-116](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp#L69-L116) —— 第三类：ACL 运行时函数（`aclrtMalloc` / `aclrtMemcpy` / `aclrtMemset` 等）替换为 `calloc` / `memcpy` / `fill_n` 的宿主机实现，让 host 侧调用代码不改一行就能在 CPU 上链接通过。

除了「替换」，`cpu_stub.hpp` 还提供了仿真专属能力——执行上下文钩子：

[include/pto/common/cpu_stub.hpp:186-254](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp#L186-L254) —— `pto::cpu_sim` 命名空间用 `dlsym` 在运行期向宿主进程查询 `pto_cpu_sim_set_execution_context` 等钩子函数，配合 `thread_local` 的 `ExecutionContext`（block_idx / subblock_id / task_cookie），让测试框架可以在同一线程上模拟「当前我是几号核」。这就是 kernel 里 `get_block_idx()`（[include/pto/common/cpu_stub.hpp:256-266](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp#L256-L266)）在 CPU 仿真下也能返回核号的机制。

最后确认这些宏的「定义现场」在构建脚本里：

- [tests/cpu/st/CMakeLists.txt:31](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/CMakeLists.txt#L31)：`add_definitions(-D__CPU_SIM)` —— CPU ST 用例只定义 `__CPU_SIM`，不指定架构号。
- [tests/costmodel/st/CMakeLists.txt:21](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/costmodel/st/CMakeLists.txt#L21)：`add_definitions(-D__COSTMODEL -D__NPU_ARCH__=2201 -DPTO_COMM_NOT_SUPPORTED)` —— CostModel 用例要同时声明后端宏、架构号与「不支持通信」。
- [tests/costmodel/st_a5/CMakeLists.txt:52](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/costmodel/st_a5/CMakeLists.txt#L52)：`-D__COSTMODEL -D__NPU_ARCH__=3101` —— 同一后端换架构号，即模拟 A5。
- [demos/cpu/gemm_demo/CMakeLists.txt:26](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/gemm_demo/CMakeLists.txt#L26)：`__CPU_SIM __PTO_AUTO__` —— demo 还叠加了 Auto 模式宏（u9-l1 会展开）。

对比可见：真机路径的 CMake 中搜索不到 `__CCE_AICORE__` 的定义——它由 CCE 编译器对设备侧编译单元自动预定义（待本地用 NPU 工具链确认具体注入点）。

#### 4.1.4 代码实践

**实践目标**：用 grep 量化「两套实现的隔离方式」——验证 common 层是「既伺候 CPU 又伺候 NPU」的交汇层，而 cpu 层是纯仿真实现、不感知真机宏。

**操作步骤**：

```bash
cd <仓库根目录>
echo "--- common 层 __CPU_SIM 出现次数:"
grep -rn "__CPU_SIM" include/pto/common | wc -l
echo "--- common 层 __CCE_AICORE__ 出现次数:"
grep -rn "__CCE_AICORE__" include/pto/common | wc -l
echo "--- cpu 层 __CPU_SIM 出现次数:"
grep -rn "__CPU_SIM" include/pto/cpu | wc -l
echo "--- cpu 层 __CCE_AICORE__ 出现次数:"
grep -rn "__CCE_AICORE__" include/pto/cpu | wc -l
# 顺带看公共层里谁同时提到两个宏：
grep -rln "__CPU_SIM" include/pto/common | xargs grep -ln "__CCE_AICORE__" 2>/dev/null
```

**需要观察的现象**：common 层两个宏都大量出现且高度共现；cpu 层 `__CCE_AICORE__` 计数为 0；最后一行命令会列出少数同时含两个宏的 common 头文件（如 `arch_macro.hpp`、`pto_instr_impl.hpp`、`event.hpp` 相关文件）。

**预期结果**：在当前 HEAD（8aacb8e0）下，我实测得到：common 层 `__CPU_SIM` 44 次、`__CCE_AICORE__` 6 次；cpu 层 `__CPU_SIM` 5 次、`__CCE_AICORE__` 0 次。这正是隔离方式的量化表达：

1. `include/pto/cpu` 里的实现只在被 `pto_instr_impl.hpp` 的 `#ifdef __CPU_SIM` 分支 include 时才参与编译，自身几乎不需要再提任何后端宏——它们就是纯粹的 C++ 实现。
2. `include/pto/common` 是「后端无关的接口 + 后端相关的条件编译」混合层，所有 `#if defined(__CPU_SIM) ... #elif defined(__CCE_AICORE__)` 的分叉都集中在这里，kernel 源码因此完全看不到分叉。

若你本地数字与上述不同，多半是 HEAD 前进了，重跑 `git log --oneline -3` 核对版本即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `pto-inst.hpp` 第 16-20 行的桩引入只判断 `__CPU_SIM` 和 `__COSTMODEL`，不判断 `__CCE_AICORE__`？

**答案**：因为 `__CCE_AICORE__` 构建由 CCE 编译器编译设备侧代码，`__gm__`、`AICORE`、`set_flag`、ACL 运行时等符号本来就有真实定义，不需要桩替换；只有「借用普通编译器模拟设备环境」的 CPU 仿真与 CostModel 才需要 `cpu_stub.hpp` / `runtime_stub.hpp` 先铺一层假地基。

**练习 2**：如果不定义任何宏就包含 `pto-inst.hpp`，会发生什么？这种构建有什么用？

**答案**：只引入 `type.hpp`、`kernel_meta.hpp`、`memory.hpp` 等公共类型，第 23 行的大守卫不成立，指令层与架构层全部不引入。这种「无后端」包含适合 host 侧代码：只想用 GlobalTensor 的 shape/stride 类型描述数据布局、不想（也不能）链接任何设备实现。

**练习 3**：CPU 仿真下 `set_flag`/`wait_flag` 是空函数，那上一讲的事件正确性在 CPU 上岂不是完全没验证？

**答案**：正确。CPU 仿真只验证「指令序列的功能语义」（单线程按序执行天然满足一切依赖），不验证「跨流水线依赖是否被正确表达」。事件配对、编号轮转这类同步正确性必须在真机（或带检查的 CostModel）上验证——这也是 u1-l3 强调的「CPU 验逻辑、真机验同步与性能」工作流的根本原因。

### 4.2 架构宏：arch_macro 与 arch_capability

#### 4.2.1 概念说明

后端宏回答「在哪种机器形态上跑」（CPU 仿真 / 真机 / 性能模型），架构宏回答「模拟/目标是哪一代芯片」。两层正交：CostModel 可以配 A2A3（`__NPU_ARCH__=2201`）也可以配 A5（`3101`）；CPU 仿真不指定架构号时走「能力全开」的兜底分支。

`arch_macro.hpp` 做翻译：`__NPU_ARCH__` 数字 → `PTO_NPU_ARCH_*` 语义宏。后续所有代码（包括你自己写 kernel）只认语义宏，不认裸数字——数字换代号时上层不用改。

`arch_capability.hpp` 在语义宏之上再封装一层**能力特征表**：每代芯片支持哪些数据类型、哪些指令族，以 `constexpr bool` 和类型别名的形式暴露，供接口层在编译期做 `static_assert` 检查（例如对不支持的架构调用 MX 指令直接编译失败，而不是真机上跑出错）。

#### 4.2.2 核心流程

```text
__NPU_ARCH__（构建系统传入的数字）
   │  arch_macro.hpp 翻译
   ├─ 2201            → PTO_NPU_ARCH_A2A3
   ├─ 3101 / 3510     → PTO_NPU_ARCH_A5（3510 额外定义 PTO_URMA_SUPPORTED）
   ├─ 3113/3003/5101  → PTO_NPU_ARCH_KIRIN*（同时 PTO_COMM_NOT_SUPPORTED）
   ├─ 9201            → PTO_NPU_ARCH_A6
   │  arch_capability.hpp 查表
   └─ PTO_NPU_ARCH_* → ArchTraits<ChipArch::...> 特化 → CurrArch
                          （SupportsBf16 / SupportsFp8 / SupportsComm / ...）
```

架构号 ↔ 架构名的完整对应表也写在 [include/README.md](include/README.md) 的逐指令支持状态表中，遇到陌生代号可去查。

#### 4.2.3 源码精读

[include/pto/common/arch_macro.hpp:14-17](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/arch_macro.hpp#L14-L17) —— CPU 仿真的兼容补齐：若没有从 CCE 工具链继承 `__DAV_CUBE__`/`__DAV_VEC__`（Davinci Cube/Vector 单元的标记宏），就都补上。这样下游头文件可以放心地用这两个宏判断「Cube/Vector 能力存在」，不必区分是仿真还是真机。

[include/pto/common/arch_macro.hpp:19-38](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/arch_macro.hpp#L19-L38) —— 核心翻译表：`2201→A2A3`、`3101/3510→A5`（3510 加 `PTO_URMA_SUPPORTED`，即 u7 将讲到的 urma 通信引擎）、`3113/3003/5101→Kirin 系列`、`9201→A6`。注意 Kirin 系列同时定义 `PTO_COMM_NOT_SUPPORTED`——这正是 `pto_instr.hpp:19-21` 据此跳过通信指令头的依据。

[include/pto/common/arch_macro.hpp:40-45](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/arch_macro.hpp#L40-L45) —— 另一类兼容补齐：为 Kirin9030/X90 定义 `__tf__`、`__in__`、`__out__` 等编译器标注宏，使同一份 kernel 源码在「标注宏由 CCE 提供」与「标注宏需自行补齐」两种环境下都能编译。

接着看能力特征表的两层结构：

[include/pto/common/arch_capability.hpp:20-50](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/arch_capability.hpp#L20-L50) —— `ChipArch` 枚举列出全部已知架构；`ArchTraitsBase` 是「默认全关」的基类模板：所有能力位（`SupportsBf16`、`SupportsFp8`、`SupportsComm`、`SupportsMxLayout`、累加器支持的类型……）默认 `false`，类型别名默认 `void`。默认关、按架构显式开——不支持的组合会在编译期露馅。

[include/pto/common/arch_capability.hpp:81-98](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/arch_capability.hpp#L81-L98) —— 两个架构特化示例：A2A3 在基类之上开 `SupportsBf16 / SupportsSyncAll / SupportsComm`；A5 继承 `ArchTraitsFp4Capable`（[include/pto/common/arch_capability.hpp:59-79](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/arch_capability.hpp#L59-L79)，A5/A6 共享的「FP4+FP8+TQuant+MX 布局」能力块，注释明确说明该块用 `#if defined(PTO_NPU_ARCH_A5) || defined(PTO_NPU_ARCH_A6)` 守卫，是为了避免在 CPU 仿真/A2A3/Kirin 路径上引用不存在的 fp8/fp4 内建类型）并追加 `SupportsComm`。

[include/pto/common/arch_capability.hpp:124-141](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/arch_capability.hpp#L124-L141) —— 兜底分支：一个架构宏都没匹配时，`ArchTraits<ChipArch::UNKNOWN>` 把全部能力打开。这解释了 CPU 仿真（不传 `__NPU_ARCH__`）为何能编译所有指令——仿真目标是「功能全集」，与 u1-l1 讲的「新特性先落 CPU 仿真」相呼应。

[include/pto/common/arch_capability.hpp:143](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/arch_capability.hpp#L143) —— `GetCurrentArch()` 返回 `CurrArch::Id`，把「当前架构」收敛为一个运行期可查询的枚举值；同一文件 `caps::` 命名空间（[include/pto/common/arch_capability.hpp:145-365](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/arch_capability.hpp#L145-L365)）还提供 `IsFP8<T>()`、`IsBF16<T>()` 等 `constexpr` 谓词——注意它们都带 `Arch = CurrArch` 默认模板参数，即类型判定本身就依赖架构能力表。

#### 4.2.4 代码实践

**实践目标**：亲手验证「同一后端、不同架构号」产生的编译差异。

**操作步骤**：

1. 阅读 [tests/costmodel/st/CMakeLists.txt:21](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/costmodel/st/CMakeLists.txt#L21) 与 [tests/costmodel/st_a5/CMakeLists.txt:52](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/costmodel/st_a5/CMakeLists.txt#L52)，确认两者后端宏同为 `__COSTMODEL`，仅架构号不同（2201 vs 3101）。
2. 写一个 5 行的探测头（示例代码，非仓库原有文件）：

```cpp
// arch_probe.cpp（示例代码）
#include <pto/pto-inst.hpp>
#include <cstdio>
int main()
{
    printf("arch=%d bf16=%d fp8=%d comm=%d\n",
        (int)pto::GetCurrentArch(),
        (int)pto::CurrArch::SupportsBf16,
        (int)pto::CurrArch::SupportsFp8,
        (int)pto::CurrArch::SupportsComm);
}
```

3. 分别用 `g++ -std=c++20 -D__CPU_SIM -I include arch_probe.cpp` 和 `g++ -std=c++20 -D__CPU_SIM -D__NPU_ARCH__=2201 -I include arch_probe.cpp`、再换 `-D__NPU_ARCH__=3101` 编译运行三次。

**需要观察的现象**：无架构号时打印 UNKNOWN（枚举值 255）且全部能力为 1；`2201` 时 arch=0、fp8=0、comm=1；`3101` 时 arch=1、fp8=1、comm=1。

**预期结果**：与 arch_capability.hpp 的特化表逐项吻合。本实践为示例代码，具体输出待本地验证（g++ 需 ≥ 13 以满足 C++20 要求，见 u1-l3）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ArchTraitsFp4Capable` 要用 `#if defined(PTO_NPU_ARCH_A5) || defined(PTO_NPU_ARCH_A6)` 包起来，而不是无条件定义？

**答案**：它引用了 `float8_e4m3_t`、`float4_e2m1x2_t` 等 fp8/fp4 内建类型别名。这些类型只存在于 A5/A6（及 CPU 仿真的 UNKNOWN 兜底路径）——在 A2A3/Kirin 路径上这些类型不存在，无条件定义会在那些编译单元里直接报「类型未声明」。源码注释（[arch_capability.hpp:55-59](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/arch_capability.hpp#L55-L59)）明确记录了这一动机。

**练习 2**：3510 与 3101 都映射到 `PTO_NPU_ARCH_A5`，为什么不直接分成两个 `PTO_NPU_ARCH_*`？

**答案**：两者是同一代架构的不同型号，指令集与能力表完全一致，唯一差异是 3510 额外支持 urma 通信引擎——所以共用 A5 语义宏，再用 `PTO_URMA_SUPPORTED` 这类细粒度特性宏区分。这体现了「架构宏管代际、特性宏管代内差异」的分层原则。

**练习 3**：`PTO_COMM_NOT_SUPPORTED` 是谁消费的？

**答案**：`pto_instr.hpp` 第 19-21 行：`#if !defined(__COSTMODEL) && !defined(PTO_COMM_NOT_SUPPORTED)` 才 include 通信指令头 [include/pto/common/pto_instr.hpp:19-21](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L19-L21)。Kirin 系列与 CostModel 构建因此完全不引入通信指令，误用会在链接/编译期暴露而非运行期。

### 4.3 指令接口层：pto_instr.hpp 与 _IMPL 转发

#### 4.3.1 概念说明

接口层是「kernel 作者看见的 PTO」：`TADD(dst, src0, src1, events...)` 这样的函数模板。它不包含任何计算逻辑，只做三件事：

1. **统一事件协议**：先 `TSYNC(events...)` 折叠等待所有前置事件（u2-l3 讲过的机制）；
2. **转发实现**：经 `MAP_INSTR_IMPL` 宏调用 `XXX_IMPL`——实现由路由层按「架构 × 后端」选定的头文件提供；
3. **返回记录事件**：`return {}` 构造 `RecordEvent`，供下一条指令作参数（生产者挂牌）。

这个「薄壳」带来两个收益：kernel 源码与后端彻底解耦（写一次到处编译）；指令的事件语义集中在一处保证（不可能某后端忘记做等待）。

#### 4.3.2 核心流程

以 `TADD` 为例的完整调用链：

```text
kernel 代码:  TADD(tout, t0, t1, e)          ← 接口层 pto_instr.hpp
                 │  1. TSYNC(e)              ← 折叠等待前置事件
                 │  2. MAP_INSTR_IMPL(TADD, dst, s0, s1)
                 │        │ 宏展开为 TADD_IMPL(dst, s0, s1)
                 ▼
路由层:       pto_instr_impl.hpp 已按宏 include 过其中之一：
                 ├─ __CPU_SIM            → include/pto/cpu/TAdd.hpp      的 TADD_IMPL（纯 C++ 循环）
                 ├─ __COSTMODEL + 2201   → include/pto/npu/a2a3/TAdd.hpp 的 TADD_IMPL（插桩版）
                 └─ __CCE_AICORE__ + 2201→ include/pto/npu/a2a3/TAdd.hpp 的 TADD_IMPL（intrinsic 版）
```

同一个 `XXX_IMPL` 符号名、三种互斥的实现来源——这就是 4.1 练习里「cpu 层零后端宏」能成立的原因：同名符号由路由层保证全局唯一。

#### 4.3.3 源码精读

[include/pto/common/pto_instr.hpp:23](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L23) —— 粘合宏本体：`#define MAP_INSTR_IMPL(API, ...) API##_IMPL(__VA_ARGS__)`，用 token 拼接把 `TADD` 拼成 `TADD_IMPL`。整个接口层 2500 余行里成百上千次 `MAP_INSTR_IMPL(...)` 都靠它一行完成转发。

[include/pto/common/pto_instr.hpp:112-118](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L112-L118) —— 教科书式的指令壳：`TADD` 等待事件 → 转发 → 返回 `RecordEvent{}`。对比 [include/pto/common/pto_instr.hpp:217-223](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L217-L223) 的 `TLOAD`，结构完全一致——90+ 条指令都是这个三段式骨架的变体，差异只在参数与 `_IMPL` 名。

[include/pto/common/pto_instr.hpp:98-110](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L98-L110) —— 后端差异化指令的守卫示例一：`TPRINT` 只在 `_DEBUG` 或 `__CPU_SIM` 下提供——真机发布构建里调试打印指令压根不存在。

[include/pto/common/pto_instr.hpp:500-653](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L500-L653) —— 守卫示例二：整族 MX 混合精度指令（`TGEMV_MX`/`TMATMUL_MX`）用 `#if defined(PTO_NPU_ARCH_A5) || defined(PTO_NPU_ARCH_A6) || defined(__CPU_SIM)` 圈定可用范围——A2A3 真机上调用会直接编译失败，与能力表 `SupportsMxLayout` 的口径一致。类似地，`SET_IMG2COL_RPT/PADDING` 在 A2A3 与 A5/Kirin9030/CPU_SIM 上各有一段条件编译（[include/pto/common/pto_instr.hpp:942-975](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L942-L975)）。

路由层则是一张巨大的 include 矩阵：

[include/pto/common/pto_instr_impl.hpp:18-21](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr_impl.hpp#L18-L21) —— A2A3 分支再分叉：`__COSTMODEL` 时 include 的也是 `pto/npu/a2a3/*.hpp`，但列表短得多（注释说明只为让 mock 桩的系数被真实执行到）；`#else`（真机）才是全量指令头。

[include/pto/common/pto_instr_impl.hpp:331-343](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr_impl.hpp#L331-L343) —— A6 与 Kirin 系列没有逐指令头文件列表，而是每架构一个聚合头（`pto/npu/a6/header.hpp` 等），体现了不同代际目录组织方式的差异（u11-l2 会展开）。

[include/pto/common/pto_instr_impl.hpp:350-357](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr_impl.hpp#L350-L357) —— 精细路由示例：`TPREFETCH_ASYNC` 用 `#if defined(__CCE_AICORE__) && !(defined(__CPU_SIM) || defined(__COSTMODEL))` 限定只在真机引入，注释说明 CPU 仿真与 CostModel 各自在下方分支取自己的变体——后端宏与架构宏在这里联合出手。

[include/pto/common/pto_instr_impl.hpp:359-395](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr_impl.hpp#L359-L395) —— CPU 仿真分支：`#ifdef __CPU_SIM` 下成批 include `pto/cpu/*.hpp`（TAdd、TMul、TMatmul、ElementTileOp 骨架……），这正是 u3-l4 将逐个精读的仿真实现集合。注意此分支**不看架构宏**——CPU 仿真是「架构无关的功能全集」。

#### 4.3.4 代码实践

**实践目标**：以 `TADD` 为线索，从 kernel 调用点出发手工走通「接口 → 宏转发 → 路由 → 实现」四跳，画出调用关系。

**操作步骤**：

1. 在仓库根目录执行：

```bash
# 第一跳：接口壳
grep -n "RecordEvent TADD(" include/pto/common/pto_instr.hpp | head -3
# 第二跳：转发宏
grep -n "MAP_INSTR_IMPL(TADD" include/pto/common/pto_instr.hpp
# 第三跳：路由（CPU 分支与 A2A3 分支各引入了哪个 TAdd.hpp）
grep -n "TAdd.hpp" include/pto/common/pto_instr_impl.hpp
# 第四跳：实现符号
grep -rn "void TADD_IMPL" include/pto/cpu include/pto/npu | head -5
```

2. 打开 grep 命中的 `include/pto/cpu/TAdd.hpp`，找到 `TADD_IMPL` 的定义体；再打开 `include/pto/npu/a2a3/TAdd.hpp` 做同样的事。
3. 用纸或 Mermaid 把四跳画成一张图，在两个实现节点上分别标注「纯 C++ 元素循环」与「昇腾 vector intrinsic」。

**需要观察的现象**：`TADD_IMPL` 在 cpu 与 npu 目录下各有一份定义，但任何单一编译单元只会包含其中一份（由路由层的互斥 `#ifdef` 保证）；两份实现的函数签名一致（参数类型来自 common 层的 Tile/GlobalTensor）。

**预期结果**：得到一张「一个调用、两条实现支路」的分叉图。CPU 支路的 `TADD_IMPL` 是对 tile 有效区做的逐元素 `+`（u3-l4 精读）；NPU 支路则映射到 CCE 的向量加 intrinsic。签名一致是关键——它保证了接口层那一份壳可以同时伺候两边。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `MAP_INSTR_IMPL` 宏删掉，直接在接口层写 `TADD_IMPL(dst, src0, src1)`，行为有区别吗？为什么还要用宏？

**答案**：单看 `TADD` 没有区别，宏只是 token 拼接。但接口层还有大量「API 名与 IMPL 名不同」或「带模板参数」的转发，例如 `TSYNC` 转发 `TSYNC_IMPL<OpCode>()`、`TFILLPAD` 按模式转发到 `TFILLPAD_IMPL / TFILLPAD_INPLACE / TFILLPAD_EXPAND`（[include/pto/common/pto_instr.hpp:1070-1088](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L1070-L1088)）。统一宏让「API 名 → 默认 IMPL 名」这条最常见规则零样板，特殊规则再手写。

**练习 2**：接口层为什么统一 `return {}` 返回 `RecordEvent`，而不是返回 `void` 让用户自己 set_flag？

**答案**：这是 u2-l3 讲过的对象风格同步：指令返回的 `RecordEvent` 作为下一条指令的可选实参即完成「等待」，赋值给 `Event<Src,Dst>` 即完成「记录」。返回值把「这条指令何时完成」编码进类型系统，kernel 里就不必再手写裸 set_flag/wait_flag，配对错误从运行期问题降级为编译期/风格问题。

**练习 3**：`TSORT32` 的注释（[include/pto/common/pto_instr.hpp:1102](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L1102)）说它「不自动实现 wait，需手动 TSYNC」。对照标准三段式，这说明什么？

**答案**：说明「接口层统一先 TSYNC」是惯例而非语言强制，个别指令可以豁免。TSORT32 豁免的原因是该指令的源/目的 tile 在排序期间的特殊占用方式使得通用的「先等前置、后记录」协议不适用，需要使用者显式编排。读源码时看到无 `TSYNC(events...)` 前缀的指令壳就要警觉：它的依赖要自己管。

## 5. 综合实践

**任务：给「后端路由」画一张可核验的全景图，并用一次编译失败验证能力表。**

1. **画图**：从 [include/pto/pto-inst.hpp:16-33](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/pto-inst.hpp#L16-L33) 出发，画出一棵以它为根的包含树，节点标注「引入条件（宏）→ 文件」。至少覆盖：cpu_stub.hpp、arch_macro.hpp、arch_capability.hpp、pto_instr.hpp、pto_instr_impl.hpp 的五个分支（CPU / CostModel×A2A3 / CostModel×A5 / 真机×A2A3 / 真机×A6）。
2. **标注定义现场**：在图上用便签标出每个宏在哪里被定义——`__CPU_SIM`（tests/cpu/st/CMakeLists.txt:31）、`__COSTMODEL`+`__NPU_ARCH__`（tests/costmodel/st/CMakeLists.txt:21）、`__CCE_AICORE__`（CCE 编译器自动预定义）。
3. **验证能力表**：用 4.2.4 的探测程序，尝试在 `-D__CPU_SIM -D__NPU_ARCH__=2201` 下调用一条 MX 指令（如 `TMATMUL_MX`）——按 [include/pto/common/pto_instr.hpp:500](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_instr.hpp#L500) 的守卫，A2A3 下该指令不存在，应当得到编译错误；去掉架构号（走 UNKNOWN 兜底）后同一代码应能编译。把两次结果贴在图旁。
4. **结论**：用三句话总结「换一个后端/架构，到底是换了什么」——预期答案：换了桩层（或不需要桩）、换了 `PTO_NPU_ARCH_*` 语义宏、换了 `pto_instr_impl.hpp` 实际 include 的那批 `_IMPL` 头文件；kernel 源码一行不动。

步骤 3 的具体报错文本随编译器版本不同，待本地验证。

## 6. 本讲小结

- `pto-inst.hpp` 是全仓库唯一入口：按 `__CPU_SIM` / `__COSTMODEL` 先铺后端地基桩，再在「三宏任一」守卫下引入架构宏、能力表、Tile 模型与指令层；后端选择全部发生在编译期，零运行期开销。
- `cpu_stub.hpp` 用三类替换（关键字空宏、事件/同步空桩、ACL 函数的宿主机实现）让设备风格源码通过普通编译器，并用 `dlsym` 钩子模拟多核执行上下文——这就是 CPU 仿真「单线程按序、同步 no-op」的根源。
- `arch_macro.hpp` 把 `__NPU_ARCH__` 数字翻译为 `PTO_NPU_ARCH_*` 语义宏（A2A3/A5/A6/Kirin），细粒度差异用 `PTO_URMA_SUPPORTED`、`PTO_COMM_NOT_SUPPORTED` 等特性宏表达；无架构号时走 UNKNOWN 兜底（能力全开），对应 CPU 仿真的「功能全集」定位。
- `arch_capability.hpp` 以 `ArchTraits` 特化给出每代芯片的能力位与类型别名，接口层据此做编译期检查——用错架构的指令/类型在编译期就失败。
- 指令接口层是三段式薄壳：`TSYNC(events...)` → `MAP_INSTR_IMPL`（token 拼接转发 `XXX_IMPL`）→ `return {}`；`pto_instr_impl.hpp` 按「架构 × 后端」互斥地 include cpu 或 npu/<arch> 的实现头，保证同一编译单元内 `_IMPL` 符号唯一。
- common 层是所有后端分叉的汇聚地（实测 `__CPU_SIM` 44 处、`__CCE_AICORE__` 6 处），而 `include/pto/cpu` 内 `__CCE_AICORE__` 出现 0 次——接口在 common 分叉、实现按目录隔离，是 PTO 多后端的基本纪律。

## 7. 下一步学习建议

本讲补完了编程模型的最后一块基础设施。单元三将沿本讲建立的「接口 → 实现」通道进入第一条真实数据链路：

- **u3-l1（TLOAD/TSTORE）**：用本讲 4.3 的四跳法精读 `include/pto/cpu/TLoad.hpp` 与 `include/pto/npu/a2a3/TLoad.hpp`，看数据搬运指令在两种后端下的真实实现差异。
- **u3-l4（CPU 仿真实现剖析）**：深入 `include/pto/cpu/ElementTileOp.h`，理解 4.1 中那批被 `#ifdef __CPU_SIM` 引入的仿真头文件如何用一套骨架复刻全部逐元素指令。
- 若你更关心「能力表如何约束写 kernel」，可提前浏览 [include/README.md](include/README.md) 的逐指令支持矩阵，把「指令 × 后端 × 架构」三维坐标在心里建立起来（u1-l2 已建立，本讲后应有实感）。
