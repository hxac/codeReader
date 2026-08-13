# SuperKernel 原理：调度开销与超核融合

> 学习阶段：入门（beginner）
> 所属单元：u2 SuperKernel 组件入门
> 依赖讲义：u1-l1 graph-autofusion 项目整体概览与定位

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说清楚「调度开销」是什么、为什么在算子越拆越细时它会反过来成为性能瓶颈。
2. 用一句话讲明白 SuperKernel 的核心思想——把整网 N 个子算子在编译期重编译成「一个」超核，从而省下 N−1 次调度。
3. 复述 SuperKernel 在「先合并」之上叠加的深度优化：ICache 预加载、Early-Start、同步优化、子 Kernel 拆分，以及 Notify/Wait 事件，并能指出每项优化的收益来源。
4. 在真实源码中定位 SuperKernel 的 JIT（Python 代码生成）与 AOT（C++ 运行时）两层边界，知道二者在哪里对接。

本讲是 SuperKernel 的「为什么」讲，只讲原理与最小代码骨架，不讲融合决策与任务切分细节（那是 u10 的事）。

## 2. 前置知识

本讲承接 u1-l1 已经引入的术语：**融合**、**调度开销**、**超核**。如果你还不熟悉下面几个硬件概念，先看这里的通俗解释：

- **算子（Operator）/ 子算子（Sub-operator）**：神经网络里的一个计算单元，比如一个 Add、一个 MatMul。在 SuperKernel 语境下，我们把要被合并的每个原始算子叫「子算子」，合并后的整体叫「超核（SuperKernel）」。
- **Kernel launch（任务下发）**：主机（Host）把一段设备（Device/NPU）代码提交给硬件去执行，这一次「提交动作」叫一次 launch。每 launch 一次，主机和设备之间都要做一次任务调度、握手。
- **同步（Synchronization）**：保证「先算完 A 再算 B」的机制。最重的一种是「全核同步（SyncAll）」——要等所有核心都到达同步点才能继续。
- **ICache（指令缓存）**：硬件里专门缓存「指令」的高速缓存。算子代码要先被加载进 ICache 才能高速执行；没命中（ICache Miss）就要去慢速内存里取，代价很高。
- **多核并行**：昇腾芯片有大量计算核心（AIV Vector 核、AIC Cube 核），同一份代码会被多个核同时执行。

一句话直觉：**算子越小、越多，花在「下发和等待」上的时间就越显眼，甚至盖过真正计算的时间。** SuperKernel 就是为解决这个问题而生的。

## 3. 本讲源码地图

本讲只碰原理相关、最轻量的几个文件：

| 文件 | 角色 | 本讲怎么用 |
|------|------|-----------|
| `super_kernel/README.md` | 原理总述 | 四项深度优化的权威文字来源 |
| `docs/zh/super_kernel/developer_guide.md` | 开发者指南 | 帮你建立「测试如何验证产物」的直觉 |
| `super_kernel/include/super_kernel/super_kernel.h` | **公共 C 接口** | JIT 与 AOT 的对接点；优化选项枚举 |
| `super_kernel/src/jit/superkernel/super_kernel.py` | JIT 代码生成入口 | `compile()`、Early-Start/同步代码生成 |
| `super_kernel/src/jit/superkernel/super_kernel_constants.py` | JIT 常量枚举 | 各优化模式的枚举定义 |
| `super_kernel/src/aot/super_kernel.cpp` | AOT 运行时入口 | `aclskOptimize` 主流程 |

> 提示：SuperKernel 是「JIT（编译期，Python）生成代码 + AOT（运行期，C++）做图级优化」的双层结构。本讲只需建立一个大致地图，第 4 节会逐层展开。

## 4. 核心概念与源码讲解

### 4.1 调度开销问题

#### 4.1.1 概念说明

一个网络通常由 N 个算子串成一条链。传统执行方式是：主机依次把 N 个 kernel **下发**给设备执行，每两个相邻算子之间还要插入同步，保证顺序正确。

这里的「调度开销」不是计算本身，而是围绕计算之外的「摩擦成本」：

- 主机侧把任务排进队列、与设备握手的开销；
- 设备侧取指、取任务描述符的开销；
- 相邻算子之间为保证顺序而插入的同步等待。

#### 4.1.2 核心流程

设第 i 个算子的纯计算时间为 \(c_i\)，每次下发/同步的额外开销为 \(s\)。传统逐算子执行的总时间大约是：

\[
T_{\text{传统}} \approx \sum_{i=1}^{N} c_i \;+\; N \cdot s
\]

当算子被拆得很细（例如大量小算子），\(c_i\) 很小，而 \(N\cdot s\) 几乎不变甚至随 N 增大，于是**调度开销反而成为主要耗时**——这正是 SuperKernel 要打击的痛点，README 把它表述为「节省 N−1 次算子调度开销」。

用伪流程描述传统链路：

```
Host:  launch(op1) --sync-- launch(op2) --sync-- ... --sync-- launch(opN)
Device: 执行op1      等待      执行op2     等待             执行opN
        ↑ 每个箭头处都付出一次 s（下发 + 同步）
```

直觉结论：**算子越多、越小，越值得把它们合并；合并的本质不是省计算，而是省「下发与同步」。**

#### 4.1.3 源码精读

README 在原理开头就把调度开销定位为 SuperKernel 的核心动机：

- [super_kernel/README.md:L5-L5](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/README.md#L5-L5) — 给出核心思想：基于算子先验信息 + JIT 把整网重编译为单一算子，「显著降低算子调度开销」。
- [super_kernel/README.md:L12-L12](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/README.md#L12-L12) — 明确点出收益的精确量级：将多个子算子融合成一个 SuperKernel，「节省 N-1 次算子调度开销」。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：亲手验证「N−1」这个数字是怎么来的。
2. **操作步骤**：
   - 打开 [super_kernel/README.md:L12-L12](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/README.md#L12-L12)。
   - 假设有 3 个子算子 A→B→C。按本节公式写出传统执行需要几次 launch、几次相邻同步；再想象把它们合并成 1 个超核后需要几次 launch。
3. **需要观察的现象**：合并后 launch 次数从 3 降到 1，相邻同步从 2 次降到 0 次（同步被吸收进超核内部），正好省下 3−1=2 次。
4. **预期结果**：你能口头解释「N 个算子合并成 1 个，省 N−1 次调度」的来源。
5. 本实践为纯阅读推理，无需上板，**待本地验证**仅在你确实跑了示例时才需要。

#### 4.1.5 小练习与答案

- **练习 1**：如果 N 个算子之间不是全串联，而是可以两两并行，SuperKernel 还能省 N−1 次调度吗？
  - **答案**：调度次数的节省本质上来自「把多次 launch 合并成一次」，与算子是否并行无关；但并行的算子合并后内部不需要插入顺序同步，反而收益更大。本讲先聚焦串联场景。

---

### 4.2 超核融合思想

#### 4.2.1 概念说明

SuperKernel 的核心思想：**既然编译阶段就能拿到整网所有子算子的先验信息（算子类型、前后序依赖、Kernel 类型等），那就用 JIT 把这 N 个子算子「缝合成一个」新的 kernel 二进制，运行时只下发一次。**

但这里有一个关键权衡（README L12 也点明了）：为了保证子算子之间的执行顺序，合并后通常要在子算子之间插入同步，**而同步本身会削弱合并带来的收益**。所以 SuperKernel 的真正价值不只在于「合并」，更在于「合并之后，因为手里有全部先验信息，可以做更多深层优化」（见 4.3）。

#### 4.2.2 核心流程

SuperKernel 是双层结构，先把全流程画出来：

```
┌─────────────── 编译期（JIT，Python）───────────────┐
│  compile(kernel_infos)                              │
│    └─ 构造 SuperOperatorInfos（汇总所有子算子元数据）│
│    └─ gen_super_kernel_file(...)  生成超核 C++ 源码  │
│         （把子算子用 CALLR 串成一段，插入同步代码）  │
│    └─ compile_super_kernel(...)   编译成超核二进制   │
└──────────────────────┬──────────────────────────────┘
                       │ 产物：一个融合后的超核 .o
                       ▼
┌─────────────── 运行期（AOT，C++）──────────────────┐
│  aclskOptimize(model, options)                      │
│    └─ 解析选项 (SuperKernelOptionsManager)          │
│    └─ 构造运行时图 SuperKernelGraph                 │
│    └─ optimizer.Process(graph)  做图级融合/调度优化  │
│    └─ graph.Update()            把结果写回模型      │
└─────────────────────────────────────────────────────┘
```

两层在公共 C 头文件 `super_kernel.h` 处对接：JIT 生成的代码遵循该头约定的接口与选项，AOT 运行时再按这些选项决定开哪些深度优化。

#### 4.2.3 源码精读

JIT 侧入口 `compile()` 非常短，恰好就是上面流程的代码化：

- [super_kernel.py:L1044-L1075](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel.py#L1044-L1075) — `compile()` 入口。注意三步：先重置全局状态、构造 `SuperOperatorInfos`，再调用 `gen_super_kernel_file()` 生成超核源码，最后 `compile_super_kernel()` 编译。关键设计：若目标 `.o` 已存在则直接 `return`（**融合结果可复用**，避免重复编译）。

子算子元数据汇总在 `SuperOperatorInfos` 这个类里，它就是「先验信息」的载体：

- [super_kernel_op_infos.py:L88-L90](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel_op_infos.py#L88-L90) — `SuperOperatorInfos.__init__`，从 `kernel_infos` 里收集所有子算子的 bin/json 路径、Kernel 类型等。

AOT 侧入口 `aclskOptimize` 把运行期优化串起来：

- [super_kernel.cpp:L80-L187](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/aot/super_kernel.cpp#L80-L187) — `aclskOptimize`：解析选项 → `InitSKGraph` → `optimizer.Process(graph)` → `graph.Update()`。这一段就是运行时把图「融合并优化」的主链路。
- [super_kernel.cpp:L148-L152](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/aot/super_kernel.cpp#L148-L152) — 真正的融合发生在 `optimizer.Process(graph)`，失败则整体失败。

两层对接的公共契约在：

- [super_kernel.h:L261-L263](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/include/super_kernel/super_kernel.h#L261-L263) — 对外暴露的三个 C 接口 `aclskOptimize` / `aclskScopeBegin` / `aclskScopeEnd`，这是 GE/框架调用 SuperKernel 的统一入口。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：确认「JIT 生成源码」与「AOT 运行时优化」是两段独立代码，且通过 `super_kernel.h` 对接。
2. **操作步骤**：
   - 在 [super_kernel.py:L1044-L1075](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel.py#L1044-L1075) 找到 `compile()`，标记它调用 `gen_super_kernel_file` 的那一行（约 L1073）。
   - 在 [super_kernel.cpp:L80-L187](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/aot/super_kernel.cpp#L80-L187) 找到 `aclskOptimize`，标记它调用 `optimizer.Process` 的那一行（约 L149）。
   - 打开公共头 [super_kernel.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/include/super_kernel/super_kernel.h)，确认 `aclskOptimize` 的声明同时被 AOT 实现和外部调用方引用。
3. **需要观察的现象**：两段代码分属不同语言、不同目录，没有任何直接调用关系，只靠头文件里的函数签名约定对接。
4. **预期结果**：你能画出「JIT 产物 → 被框架加载 → 框架调用 aclskOptimize → AOT 图优化」这条链。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `compile()` 在发现 `.o` 已存在时直接 `return`？
  - **答案**：因为同一组子算子融合出的超核二进制是确定且可复用的，跳过重复编译能显著缩短整网首次之外的编译时间。
- **练习 2**：JIT 和 AOT 各自的「输入」是什么？
  - **答案**：JIT 的输入是 `kernel_infos`（子算子列表 + 选项）；AOT 的输入是 `aclmdlRI model`（运行时模型句柄）+ `aclskOptions`（优化选项）。

---

### 4.3 深度优化手段

#### 4.3.1 概念说明

把 N 个算子合并成 1 个，只是「省调度」的第一层收益。README 的关键论点是：**正因为编译期就能看到全部子算子的先验信息，SuperKernel 可以在「合并」之上再做四项更深的优化**，把被同步削弱掉的收益补回来甚至超越。这四项是：

1. **ICache 预加载（ICache Preload）**
2. **Early-Start（提前启动）**
3. **同步优化（细粒度同步范围）**
4. **子 Kernel 拆分（缓解多核对同一指令地址的争用）**

此外还支持基于内存语义的 **Notify/Wait 事件**，用于 Tiling 下沉与 Weight 预取等场景。

#### 4.3.2 核心流程

逐项说明每项优化「看到什么先验信息 → 做什么 → 收益来自哪里」：

| 优化 | 利用的先验信息 | 做法 | 收益来源 |
|------|--------------|------|---------|
| ICache Preload | 超核体积大、子算子执行顺序已知 | 在当前子算子执行前，预取**后续**子算子的代码段进 ICache | 减少 ICache Miss，避免运行中去慢速内存取指令 |
| Early-Start | 前序算子末尾多为 MTE 搬运指令、后续算子开头多为与输入无关的初始化标量指令，二者属不同计算单元 | 在前序搬运前插 Set、后续初始化后插 Wait | 让两类指令**并发执行**，缩短串行等待 |
| 同步优化 | 编译期已知每个子算子的 Kernel Type（AIV/AIC/Mix） | 只做必要范围的同步（连续 Vector 算子只做 Vector 核同步） | 缩小同步范围，降低同步等待 |
| 子 Kernel 拆分 | 多核会并发访问同一指令地址，在 L2 Cache 串行化 | 把子 Kernel 代码复制多份，按核 ID 映射到不同物理地址 | 缓解多核对同一地址的争用，恢复并行增益 |

用数学直觉看 Early-Start：若前序算子末尾搬运耗时 \(t_{\text{搬}}\)，后续算子开头初始化耗时 \(t_{\text{初}}\)，传统串行需 \(t_{\text{搬}}+t_{\text{初}}\)；Early-Start 让二者重叠，理论上可降到 \(\max(t_{\text{搬}}, t_{\text{初}})\)。

#### 4.3.3 源码精读

四项优化的权威文字描述在 README：

- [super_kernel/README.md:L14-L17](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/README.md#L14-L17) — **ICache Preload**：超核二进制大，加载时只预取入口指令会引发高 ICache Miss；机制是「在当前子算子开始执行前预加载其后续子算子代码段」。
- [super_kernel/README.md:L24-L26](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/README.md#L24-L26) — **Early-Start**：在前序搬运指令前插 Set、后续初始化指令后插 Wait，实现两类指令并发。
- [super_kernel/README.md:L29-L32](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/README.md#L29-L32) — **同步优化**：Mix 1:2 类型原本要等 Vector+Cube 全核同步；编译期已知 Kernel Type，可只做必要范围（如连续 Vector 算子只做 Vector 核同步）。
- [super_kernel/README.md:L34-L37](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/README.md#L34-L37) — **子 Kernel 拆分**：多核并发访问同一指令地址会在 L2 Cache 形成串行队列；把子 Kernel 复制多份、按核 ID 映射不同地址以缓解争用。
- [super_kernel/README.md:L42-L42](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/README.md#L42-L42) — **Notify/Wait 事件**：基于内存语义，适配 Tiling 下沉（依赖前序输出的 Tiling 计算下沉到 AICpu）与 Weight 预取（借 SDMA 提前把数据加载到 L2）。

这些优化在代码里都对应着「可开关的选项」。公共头里用枚举集中登记：

- [super_kernel.h:L41-L61](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/include/super_kernel/super_kernel.h#L41-L61) — `aclskOptionType` 枚举：`PRELOAD_CODE = 0`（ICache 预加载）、`SPLIT_MODE = 1`（子 Kernel 拆分）、`EARLY_START = 16`（Early-Start）等。看到这些枚举值，就知道每项优化在运行时是一个可独立开关的选项。
- [super_kernel.h:L70-L76](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/include/super_kernel/super_kernel.h#L70-L76) — `aclskPreloadOption`（`preloadMode`）与 `aclskSplitModeOption`（`splitCnt`）两个选项结构体，分别是 ICache 预加载和子 Kernel 拆分的参数。
- [super_kernel.h:L155-L161](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/include/super_kernel/super_kernel.h#L155-L161) — `aclskEarlyStartValue`（`ACLSK_EARLY_START_DISABLED/ENABLED`）与 `aclskEarlyStartOption`，Early-Start 的开关。

JIT 侧的常量枚举与选项一一对应，且更细：

- [super_kernel_constants.py:L33-L39](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel_constants.py#L33-L39) — `SuperKernelEarlyStartMode`：Early-Start 有 Disable / V1 / V2 / V2 禁用子核等多个档位，说明它是一个逐步演进的优化。
- [super_kernel_constants.py:L65-L71](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel_constants.py#L65-L71) — `SuperKernelPreLoadMode`：预加载有「逐步 / 整体 / 提前一步」等多种策略。
- [super_kernel_constants.py:L97-L111](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel_constants.py#L97-L111) — `SuperKernelKernelType`：精确区分 AIV_ONLY / AIC_ONLY / MIX_AIC_1_2 等，这正是「同步优化」赖以缩小同步范围的依据。

最有说服力的是 JIT 生成代码时，**确实根据先验信息为每对相邻子算子计算了 Early-Start 配置和定制同步**：

- [super_kernel.py:L57-L87](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel.py#L57-L87) — `gen_early_start_config`：根据**前序**与**当前**子算子的设备类型（AIC/AIV/MIX）拼出一个 4 位配置 `(prev<<2)|cur`。注意它读取的是子算子的 Kernel 类型——这就是「编译期先验信息被用来生成 Early-Start 代码」的活证据。
- [super_kernel.py:L136-L150](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel.py#L136-L150) — `get_sync_code_by_kernel_type`：按 Kernel 类型返回**不同的同步指令**（MIX 1:1/1:2 用 `SyncAll<false>`，纯 AIC 用 `SYNC_AIC_FLAG`，其余用 `SYNC_AIV_ONLY_ALL`）。这正是「同步优化」——不再一律全核同步，而是按类型定制同步范围。

子 Kernel 拆分在 JIT 里体现为「把符号重命名后复制多份二进制」：

- [super_kernel_op_infos.py:L35-L56](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel_op_infos.py#L35-L56) — `gen_symbol_rename_file`：为拆分副本生成重命名映射（如 `kernel_name → kernel_name_split1`），让不同副本成为不同符号，配合 [super_kernel_op_infos.py:L59-L78](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel_op_infos.py#L59-L78) 的 `split_dynamic_o_in_super_kernel`（`cp` 复制 + `llvm-objcopy --redefine-syms`）产出多份物理副本。

> 示例代码（说明用，非仓库原样）——`get_sync_code_by_kernel_type` 的本质就是一张「Kernel 类型 → 同步指令」分发表：
> ```python
> # 示例代码：伪代码化的分发逻辑
> if kernel_type in (MIX_AIC_1_1, MIX_AIC_1_2):  return "SyncAll<false>()"   # 范围更小的同步
> elif kernel_type in (AIC_ONLY, MIX_AIC_1_0):   return ffts/wait(SYNC_AIC_FLAG)
> else:                                          return ffts/wait(SYNC_AIV_ONLY_ALL)
> ```

#### 4.3.4 代码实践（本讲主实践，源码阅读型）

1. **实践目标**：阅读 README，列出至少三种 SuperKernel 应用的深度优化技术，并各用一句话说明收益来源。
2. **操作步骤**：
   - 打开 [super_kernel/README.md](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/README.md) 的原理小节（L14–L42）。
   - 对 ICache Preload、Early-Start、同步优化、子 Kernel 拆分、Notify/Wait 这五项中**任选三项**，在代码里找到它的开关枚举（提示：`super_kernel.h` 的 `aclskOptionType` 或 `super_kernel_constants.py` 的对应枚举）。
3. **需要观察的现象**：每一项原理文字都能在代码里找到一个对应的「选项/枚举/生成函数」，原理与实现是一一对应的。
4. **预期结果**：产出一张三行表格，形如：
   | 优化 | 收益来源（一句话） | 对应代码 |
   |------|------------------|---------|
   | Early-Start | 让前序搬运与后续初始化两类指令并发 | `gen_early_start_config` / `aclskOptionType::EARLY_START` |
   | 同步优化 | 按 Kernel 类型只做必要范围同步 | `get_sync_code_by_kernel_type` |
   | 子 Kernel 拆分 | 按核 ID 映射不同物理地址，缓解 L2 争用 | `gen_symbol_rename_file` / `SPLIT_MODE` |
5. 本实践为纯阅读，无需上板。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 Early-Start 只能让「部分指令」并发，而不是全部？
  - **答案**：只有当「前序算子末尾指令」与「后续算子开头指令」分属不同计算单元、且后者与输入数据无关时才能并发；真正依赖前序输出结果的计算部分仍然必须等待，所以只是部分重叠。
- **练习 2**：如果关闭子 Kernel 拆分（`splitCnt=1`），多核并行时会遇到什么问题？
  - **答案**：多个核会并发访问内存中同一指令地址，在共享 L2 Cache 上形成串行化访问队列，争用会削弱多核并行带来的增益。
- **练习 3**：同步优化依赖的最关键先验信息是什么？
  - **答案**：每个子算子的 Kernel Type（AIV / AIC / Mix 1:1 / Mix 1:2 等），它决定了该做多大范围的同步。

## 5. 综合实践

把本讲三个模块串起来，做一次「先验信息如何贯穿 SuperKernel」的小追踪。

**任务**：以一对相邻子算子 `pre_op → cur_op` 为例，回答三个问题，把「调度开销 → 超核融合 → 深度优化」连成一条线。

1. **开销侧（4.1）**：这对算子在传统模式下会产生几次相邻同步？合并进超核后呢？用 \(N-1\) 公式解释。
2. **融合侧（4.2）**：这对算子的元数据从哪里进入 JIT？追踪到 [super_kernel_op_infos.py:L88-L90](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel_op_infos.py#L88-L90) 的 `SuperOperatorInfos`，再追踪到 [super_kernel.py:L1044-L1075](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel.py#L1044-L1075) 的 `compile()`。
3. **优化侧（4.3）**：这对算子的 Kernel 类型会被 [super_kernel.py:L57-L87](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel.py#L57-L87) 的 `gen_early_start_config` 和 [super_kernel.py:L136-L150](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel.py#L136-L150) 的 `get_sync_code_by_kernel_type` 同时读取。说明这两个函数分别基于「类型」产出了什么代码。

**预期产物**：一段三段式的文字说明，能让人看出「正是因为编译期握有全部子算子的类型与顺序，SuperKernel 才能既合并又做深层优化」。

> 说明：本实践为源码阅读与推理，不依赖实际上板。若你后续在 u2-l3 跑通 `superkernel_scope` 示例，可以回头对照本次追踪。

## 6. 本讲小结

- **调度开销**是算子越小越多时越显眼的「摩擦成本」，SuperKernel 的第一性目标是省掉 N 个算子之间的 N−1 次下发与同步。
- **超核融合**用编译期先验信息 + JIT 把 N 个子算子重编译成 1 个超核二进制；但合并本身要插入同步，会削弱收益。
- **深度优化**才是 SuperKernel 的真正价值：ICache Preload、Early-Start、同步优化、子 Kernel 拆分（外加 Notify/Wait 事件），每一项都依赖「编译期已知全部子算子」这一前提。
- SuperKernel 是**双层结构**：JIT（Python，编译期生成代码）与 AOT（C++，运行时图优化），通过公共 C 头 `super_kernel.h` 对接。
- 原理与代码一一对应：每项优化都能在 `aclskOptionType` 枚举、`super_kernel_constants.py` 枚举、以及 `super_kernel.py` 的生成函数里找到落点。

## 7. 下一步学习建议

- **下一步讲义**：u2-l2《SuperKernel 目录结构与构建产物》——把本讲的「JIT/AOT 两层」落到具体目录与构建目标（`ascendsk` 共享库、`superkernel_whl`），并看清二者通过哪个公共头对接。
- **更远的**：u2-l3 会带你跑通 `superkernel_scope` 基础示例，亲手看到 `compile()` 被调用的位置；u10 则深入 AOT 运行时的 `SkOptimizer`/`SkTaskBuilder`，讲清融合决策与任务切分——那是本讲刻意没展开的实现细节。
- **建议阅读源码**：想加深对「同步优化」的体感，重点读 [super_kernel.py:L136-L150](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel.py#L136-L150) 的 `get_sync_code_by_kernel_type`；想理解 Notify/Wait 内存语义，读 [super_kernel.py:L90-L133](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/src/jit/superkernel/super_kernel.py#L90-L133) 的 `gen_notify_wait_func`。
