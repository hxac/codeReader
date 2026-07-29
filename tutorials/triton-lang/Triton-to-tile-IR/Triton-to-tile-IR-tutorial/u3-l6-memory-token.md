# 无序内存模型与 AutoGenMemoryToken

## 1. 本讲目标

本讲聚焦 `make_tileir` 流水线里的一道「收尾型」预处理 pass——`auto-gen-memory-token`（对应 C++ 类 `AutoGenMemoryTokenPass`）。学完后你应该能够：

- 说清楚 CUDA Tile IR 的**无序内存模型（unordered memory model）**是什么，以及它和 PTX 后端默认内存行为的差别。
- 理解 **memory token** 这种「显式串行化凭证」的语义：它如何把两次访存用「生产者→消费者」的数据依赖串起来。
- 读懂 `AutoGenMemoryTokenPass` 的「两遍扫描 + 序列识别」算法：它如何用 SID 把访问同一块内存的访存归到同一条序列、如何过滤掉不需要排序的序列、以及如何把 token 跨 `if`/`for`/`loop` 控制流正确传播。
- 解释 `debug_barrier`（`gpu.barrier`）被该 pass 移除后，是如何用「单序列全串行化」来替代的。

本讲承接 u3-l2（主转换 `convert-triton-to-cuda-tile`），因为该 pass 操作的正是主转换产出的 `cuda_tile` 方言访存算子（`load_ptr_tko` / `store_ptr_tko` 等）。

## 2. 前置知识

在阅读本讲前，你需要先建立以下直觉（相关概念在前序讲义已铺垫）：

- **cuda_tile 方言的访存算子**：主转换把 `tt.load`/`tt.store`/`tt.atomic_*` lowering 成 cuda_tile 的 `LoadPtrTkoOp`/`StorePtrTkoOp`/`AtomicRMWTkoOp`/`AtomicCASTkoOp`（指针类）以及 `LoadViewTkoOp`/`StoreViewTkoOp`（视图类）。这些算子天然带一个可选的 **token 操作数**和一个 **token 结果**。
- **什么是内存模型（memory model）**：它规定「在没有显式同步时，硬件可以按什么顺序重排两次访存」。PTX/SASS 默认对同一地址的访存保持较强的顺序；而 CUDA Tile IR 选择了**无序模型**——全局访存默认**不保证顺序**，需要顺序时必须用 token 显式声明。
- **什么是 RAW 数据冒险（read-after-write hazard）**：先写后读同一地址，读必须拿到写的最新值。在无序模型里这种依赖不是自动成立的，必须显式串行化。
- **`debug_barrier` 在 TileIR 下的形态**：u2-l4 已讲过，TileIR 不跑 TTG lowering，所以 Triton 的 `tl.debug_barrier` 在前端被生成为 `gpu.barrier`（`mlir::gpu::BarrierOp`）作为桥接。本讲会看到它正是触发「全串行化」的开关之一。

一句话建立心智模型：**token 是一根「接力棒」**。一次访存接过上一根接力棒（输入 token），跑完自己的访存后再交出一根新接力棒（输出 token）；下一个访存必须等这根新接力棒才能开跑。这样就把两次访存锁成了先后顺序。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [third_party/tileir/lib/Transform/AutoGenMemoryToken.cpp](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/AutoGenMemoryToken.cpp) | pass 的全部实现：序列识别、过滤、token 生成与跨控制流传播。 |
| [third_party/tileir/include/Transform/Passes.td](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/include/Transform/Passes.td) | TableGen 定义，声明 pass 选项 `autogen-alias-memtoken`。 |
| [third_party/tileir/triton_tileir.cc](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc) | pybind 薄壳 `add_auto_gen_memtoken`，把 pass 挂到 Python PassManager。 |
| [third_party/tileir/backend/compiler.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py) | `make_tileir` 在主转换之后调用该 pass；`TileIROptions.enable_autogen_alias_mem_token` 控制开关。 |
| [third_party/tileir/backend/conf.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/conf.py) | 环境变量 `TILEIR_ENABLE_AUTOGEN_ALIAS_MEM_TOKEN` 的读取。 |
| [third_party/tileir/test/FileCheck/op-conversion-auto-memtoken.mlir](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-auto-memtoken.mlir) | lit 测试：别名访存（`if`/`for`/`while`/只读）场景的 token 生成。 |
| [third_party/tileir/test/FileCheck/op-conversion-barrier.mlir](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-barrier.mlir) | lit 测试：`debug_barrier` 场景的全串行化。 |
| [README.md](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md) | 交代无序内存模型与「保守追加 token」策略的项目背景。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**4.1 无序内存模型与 token 语义**、**4.2 token 串行化（别名访存的序列识别与串接）**、**4.3 barrier 处理（debug_barrier 的全局串行化）**。

### 4.1 无序内存模型与 memory token 语义

#### 4.1.1 概念说明

README 用一句话点明了硬件层的事实：

> CUDA Tile IR now supports only an unordered memory model, where global memory access operations are not ordered by default. If explicit memory access ordering is required, memory token semantics are available for users to control this behavior.
> —— [README.md:56](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L56)

翻译过来就是：**全局访存默认不保证先后顺序**。这对编译器是好事——硬件可以自由乱序、合并、流水线化访存以榨取性能；但对正确性是隐患——如果两次访存访问同一块内存（别名，aliasing），默认顺序不成立就可能算错。README 明确列了两类会算错的场景：

- 不同全局访存之间存在**内存别名**（memory aliasing）。
- 数据需要**跨 tile 块流动**（如 splitK/streamK 的跨块归约需要全局内存锁）。
> 见 [README.md:58-61](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L58-L61)。

CUDA Tile IR 给出的解法是 **memory token**：每个访存算子带一个可选的输入 token 和一个输出 token。把 A 的输出 token 接到 B 的输入 token，就声明了「B 必须在 A 之后」，从而在无序模型里人工重建出顺序。

**为什么不直接用强模型？** 因为强模型会拖累所有访存，包括那些本不需要排序的。token 是「按需付费」的——只有需要顺序的地方才串。而本讲的 pass 干的事，就是**自动**把需要顺序的地方识别出来并串上 token，让用户**不必改 kernel 脚本**。这正是 README 的计划项之一：

> Apply conservative rules to append memory tokens during Triton-to-CUDA Tile conversion, which avoids script changes but may introduce performance loss.
> —— [README.md:67](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L67)

#### 4.1.2 核心流程

token 的「接力棒」语义可以用一段最简伪代码描述。原始无 token 的 IR：

```mlir
%v1 = load_ptr_tko %ptr        : tile<ptr<i32>> -> tile<i32>, token   ; 无输入 token，乱序
%t2 = store_ptr_tko %ptr, %v1  : ...                                  ; 无输入 token，乱序
```

加上 token 后（取自 [Passes.td:69-78](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/include/Transform/Passes.td#L69-L78) 的官方示例）：

```mlir
%0    = make_token : token
%v1, %tk1 = load_ptr_tko weak %ptr token=%0   : ... -> tile<i32>, token
%tk2 = store_ptr_tko weak %ptr, %v1 token=%tk1 : ... -> token
```

依赖链是一条线性表：

\[
\text{make\_token} \;\xrightarrow{\;t_0\;}\; \text{load} \;\xrightarrow{\;t_1\;}\; \text{store} \;\xrightarrow{\;t_2\;}
\]

每个箭头表示「输入 token → 输出 token」的承接。这条链上任何两个访存都被强制排序，链外的访存仍可自由乱序。

#### 4.1.3 源码精读

pass 文件开头的注释把设计规则讲得很清楚，是理解整篇实现的「总纲」：

> [AutoGenMemoryToken.cpp:21-56](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/AutoGenMemoryToken.cpp#L21-L56) —— 列出三条规则：
> - 若 kernel 已含带输入 token 的访存（用户已手动加 token），则**保持原 token 流不变，本 pass 啥也不做**；
> - 若 kernel 含 `debug_barrier`，则为**所有**访存按串行方式加 token；
> - 若 kernel 含**访问同一数据的多组访存**，则为它们加 token 以维持访问顺序。

注释还点明了核心抽象——**序列（sequence）**：把需要维持顺序的访存组织成序列，每条序列配一个 SID，函数 `getMemOpSeqId` 把 op 映射到所属序列。两类访存用不同方式表示访问位置：

- **ptr 类**（`LoadPtrTkoOp`/`StorePtrTkoOp`/`AtomicRMWTkoOp`/`AtomicCASTkoOp`）：用**指针值**的哈希当 SID；
- **view 类**（`LoadViewTkoOp`/`StoreViewTkoOp`）：用**视图值 + 索引值**组合哈希当 SID。

判别哪些算子属于「访存」、哪些属于「写」，由两个工具函数给出：

> [AutoGenMemoryToken.cpp:160-169](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/AutoGenMemoryToken.cpp#L160-L169) —— `isMemOp` 枚举六种访存算子；`isWriteMemOp` 进一步挑出四种写算子（store/atomic_rmw/atomic_cas）。读算子（load）不算写。

为什么要在 SID 之外再区分「读/写」？因为后面会看到：**纯读序列不需要串行化**（读读乱序无害），只有含写的序列才需要。这个区分在 4.2 的过滤阶段用到。

#### 4.1.4 代码实践

**实践目标**：亲手感受「无序模型下别名访存会算错」，并理解 token 为何能修正。

**操作步骤（源码阅读型）**：

1. 打开 [op-conversion-auto-memtoken.mlir:213-221](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-auto-memtoken.mlir#L213-L221) 的 `test_auto_memtoken_read_only` 输入。它对 `%X` 连读两次、对 `%Y` 读一次再写一次。
2. 设想这是无序模型：两个 `%X` 的 load 互换顺序会不会改变结果？`%Y` 的 load 和 store 呢？

**需要观察的现象**：

- `%X` 的两次 load 都是**纯读**，乱序无害，预期不该被串。
- `%Y` 是「先读后写」同地址，存在 RAW 冒险，**必须**串起来。

**预期结果**（见 [op-conversion-auto-memtoken.mlir:223-228](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-auto-memtoken.mlir#L223-L228)）：`%X` 的两个 load 不带 `token=`（只有 `weak` 标记），而 `%Y` 的 load 带 `token=%token0`、store 带 `token=%token3`，形成 `%Y` 的 token 链。我们会在 4.2.4 详细复盘这条用例。

#### 4.1.5 小练习与答案

**练习 1**：为什么 CUDA Tile IR 选择无序内存模型，而不是默认对全局访存强排序？

> **答**：强排序会让硬件失去乱序/合并/流水线访存的自由，拖累所有 kernel 的性能。无序模型把性能最大化，只在确实需要顺序的地方用 token 显式补回依赖，是「按需付费」的折中。

**练习 2**：用一句话解释 token 的「接力棒」语义。

> **答**：访存 A 把自己的输出 token 接到访存 B 的输入 token 上，就声明了「B 必须在 A 之后完成」；token 流就是一条由这些依赖串成的链。

### 4.2 token 串行化：别名访存的序列识别与串接

#### 4.2.1 概念说明

「别名访存」指多次访存落在同一块内存上。本模块解决：**怎么自动找出这些访存、怎么决定哪些值得串、串上之后 token 又怎么跨控制流接起来**。这是 `enable_autogen_alias_mem_token` 选项打开时（默认开）的主干逻辑。

核心抽象是上一节提到的 **SID（序列号）**：访问同一数据的访存共享同一个 SID，构成一条「序列」。pass 只对「含写、且不止一个访存」的序列生成 token 链。

#### 4.2.2 核心流程

整个 pass 对每个 kernel（`cuda_tile::EntryOp`）跑**两遍扫描**，这是理解实现的骨架：

```
预处理遍 (collect)：逐个访存 → 算 SID → 计入 srcToMemSeqInfoMap[Sid]{memOpCounter, writeMemOpCounter}
                              → 若发现 op 已带 token，置 hasMemToken=true 并中断
检查阶段 (check)  ：过滤（忽略 单 op 序列 / 无写序列）；无 barrier 且关掉 alias 选项则跳过；
                              若 hasMemToken 则整体跳过（尊重用户手写 token）
变换遍 (transform)：为每条幸存序列在入口块开头造一个 make_token；
                              递归遍历，给每个访存「接上输入 token、产出输出 token」，
                              跨 if/for/loop 把 token 当成额外迭代变量传播
```

数学上，token 链是一条对序列内访存按**源码顺序**施加的全序。设一条序列有访存 \(o_1, o_2, \dots, o_n\)（按程序顺序），生成：

\[
t_0 = \text{make\_token},\quad
t_i = \text{out\_token}(o_i,\, t_{i-1}),\ i=1..n
\]

其中 \(\text{out\_token}(o_i, t_{i-1})\) 表示把 \(t_{i-1}\) 作为 \(o_i\) 的输入 token，取 \(o_i\) 的输出 token 作为 \(t_i\)。这条链保证 \(o_i \succ o_{i-1}\)（\(o_i\) 晚于 \(o_{i-1}\)）。

跨控制流的传播是难点，规则如下：

- **`if`**：then/else 两支各自独立串接，出口处两支的 token 合并成 `if` 的一个额外结果（两支都要 yield token）。后续访存消费这个「汇合后的 token」。
- **`for`**：token 变成循环的一个额外迭代参数（init 值 = 进入循环前的 token；continue 带循环体内的 token）。循环结果再产出 token 给后续。
- **`loop`（cuda_tile 的通用循环，含 break/continue）**：与 `for` 类似，但还要处理循环体内的 `if` 里夹着 `break`/`continue` 的情况，把这些终止算子也挂上 token。

#### 4.2.3 源码精读

**SID 计算** —— `getMemOpSeqId` 把每个访存映射到序列号：

> [AutoGenMemoryToken.cpp:178-218](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/AutoGenMemoryToken.cpp#L178-L218) —— 对六种访存分别取出代表「访问位置」的值：ptr 类取指针操作数（如 `loadOp.getSource()`、`storeOp.getDestination()`），view 类取视图加索引；同时若 op 已带 token，置 `hasMemToken=true`。最后用 `mlir::hash_value` + `llvm::hash_combine` 把这些值压成一个 `SeqId`。注意开头一句：若 `hasBarrierOp`，直接返回常量 `BARRIER_SEQ_ID`（=1），这就是 4.3 要讲的 barrier 全串行化入口。

**预处理遍** —— 在 `runOnOperation` 里收集信息：

> [AutoGenMemoryToken.cpp:544-568](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/AutoGenMemoryToken.cpp#L544-L568) —— 对 kernel 体做一次 walk：遇 `gpu::BarrierOp` 置 `hasBarrierOp=true` 并**立即删除该 op**（见下节）；遇访存则算 SID、累加 `memOpCounter`，写算子再累加 `writeMemOpCounter`；一旦发现 `hasMemToken`（用户已加 token），`WalkResult::interrupt()` 提前结束。

**检查阶段** —— 决定要不要改、改哪些序列：

> [AutoGenMemoryToken.cpp:600-606](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/AutoGenMemoryToken.cpp#L600-L606) —— 过滤规则一眼可见：
> ```cpp
> if (p.second.memOpCounter <= 1 || !p.second.writeMemOpCounter)
>     p.second.ignored = true;
> else
>     willModify = true;
> ```
> 即**序列只有 1 个访存，或完全没有写算子，就忽略**（前者无可排序对象，后者是纯读无冒险）。`willModify` 为假则整个 kernel 跳过，不动 IR。

**为每条幸存序列造初始 token**：

> [AutoGenMemoryToken.cpp:138-149](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/AutoGenMemoryToken.cpp#L138-L149) —— `getBlockInitTokens` 在入口块开头为每条未忽略的序列插入一个 `cuda_tile::MakeTokenOp`，返回 `SeqTokens`（SID → 初始 token 值的映射）。这个映射就是后面在块内传递的「当前接力棒集合」。

**给单个访存挂 token**：

> [AutoGenMemoryToken.cpp:233-254](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/AutoGenMemoryToken.cpp#L233-L254) —— `updateMemOpWithToken`：把输入 token 追加到 op 的操作数末尾，同步更新 `operandSegmentSizes` 属性（标记 token 段存在），并返回 op 的最后一个结果（即它的输出 token）。这就是「接力棒」交接的实现。

**逐块传播与控制流穿透**：

> [AutoGenMemoryToken.cpp:490-525](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/AutoGenMemoryToken.cpp#L490-L525) —— `addMemTokenForBlock` 用 `WalkOrder::PreOrder` 扫描块：遇 `IfOp`/`ForOp`/`LoopOp` 分别递归 `handleIfOpTokens`/`handleForOpTokens`/`handleLoopOpTokens` 并 `skip()` 其子树（避免重复访问）；遇普通访存则 `updateMemOpWithToken` 串接，并 `seq.memOpCounter--`。

以 `if` 为例看「两支合并」：

> [AutoGenMemoryToken.cpp:266-342](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/AutoGenMemoryToken.cpp#L266-L342) —— `handleIfOpTokens` 先分别处理 then/else 两支；再用 `tokens.aggregate(...)`（[AutoGenMemoryToken.cpp:84-106](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/AutoGenMemoryToken.cpp#L84-L106)）收集两支都更新过的 SID，保证两支 yield 的 token 顺序一致；然后给两支的 `YieldOp` 追加 token 操作数（`updateTermOpWithToken`），重建一个多一个 token 结果的 `IfOp`，把新 token 结果回写到 `tokens`。`for`（[344-400](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/AutoGenMemoryToken.cpp#L344-L400)）和 `loop`（[402-469](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/AutoGenMemoryToken.cpp#L402-L469)）思路相同，只是把 token 当成循环迭代变量，并在 `continue`/`break` 终止算子上也挂 token。

**选项与挂载**：

> [Passes.td:86-90](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/include/Transform/Passes.td#L86-L90) —— pass 选项 `autogen-alias-memtoken`（C++ 字段 `enable_autogen_alias_mem_token`），默认 `true`。关掉它后，pass 只对 barrier 场景生效，不再自动处理别名（见 [AutoGenMemoryToken.cpp:573-576](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/AutoGenMemoryToken.cpp#L573-L576)）。

> [compiler.py:315](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L315) —— `make_tileir` 在主转换 `add_triton_to_cudatile` 之后、inliner/fma 之前调用 `tileir.passes.add_auto_gen_memtoken(pm, opt.enable_autogen_alias_mem_token)`。开关来自 [TileIROptions](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L100-L102)（字段 `enable_autogen_alias_mem_token` 默认 `True`，注释明说是「tileir 内存模型的 workaround」）；对应环境变量读取在 [conf.py:17-19](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/conf.py#L17-L19)（`TILEIR_ENABLE_AUTOGEN_ALIAS_MEM_TOKEN`，默认 `"1"`）。pybind 薄壳见 [triton_tileir.cc:96-100](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc#L96-L100)。

#### 4.2.4 代码实践

**实践目标**：用 `test_auto_memtoken_read_only` 验证「纯读序列被忽略、含写序列被串」的过滤规则。

**操作步骤**：

1. 读输入 [op-conversion-auto-memtoken.mlir:213-221](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-auto-memtoken.mlir#L213-L221)：`%1=load %X`、`%2=load %X`、`%3=load %Y`、`store %Y,%3`。
2. 手算两条序列的计数：
   - SID(X)：2 个访存，写计数 0 → `writeMemOpCounter=0` → **忽略**。
   - SID(Y)：2 个访存（1 读 + 1 写），写计数 1 → **保留**。
3. 对照期望输出 [op-conversion-auto-memtoken.mlir:223-228](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-auto-memtoken.mlir#L223-L228)。

**需要观察的现象**：

- 只有一个 `make_token`（只给 Y 序列造）。
- `%X` 的两个 load 形如 `load_ptr_tko weak %X`，**没有 `token=`**（被忽略，故 `weak` 无序）。
- `%Y` 的 load 是 `load_ptr_tko weak %Y token=%token0`，store 是 `store_ptr_tko ... token=%token3`，形成 Y 的 token 链。

**预期结果**：与 CHECK 行一致——X 无 token，Y 形成链。这正好印证 [AutoGenMemoryToken.cpp:600-606](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/AutoGenMemoryToken.cpp#L600-L606) 的过滤规则。

#### 4.2.5 小练习与答案

**练习 1**：为什么纯读序列（多次读同一地址）不需要串 token？

> **答**：读读之间没有数据冒险——无论谁先谁后，读到的值都一样（假设期间没有写）。无序模型下乱序读不会改变结果，所以 pass 用 `!writeMemOpCounter` 判定后忽略它，避免无谓的串行化损失性能。

**练习 2**：`if` 两支里都改了 token，出口处怎么保证后续访存拿到「正确的」token？

> **答**：`handleIfOpTokens` 把 token 变成 `if` 的一个额外结果值，then/else 两支在各自的 `YieldOp` 里 yield 自己的 token；`if` 执行了哪一支，结果 token 就是哪一支的。后续访存消费这个 `if` 结果 token，等价于「无论走哪条路，都被正确串在该分支的访存之后」。

### 4.3 barrier 处理：debug_barrier 的全局串行化

#### 4.3.1 概念说明

`tl.debug_barrier` 是 Triton 用户用来插同步点的工具：它要求「barrier 之前的所有访存必须在 barrier 之后的所有访存之前完成」。在 PTX 后端它通常 lowering 成一条硬件屏障指令；但 TileIR 的无序模型里，正确的等价物是**一条贯穿全部访存的 token 链**——把所有访存（无论是否别名）串成一条全序，barrier 自然就被 token 化了。

这就是 pass 注释里的第二条规则：「若 kernel 含 `debug_barrier`，则为所有访存按串行方式加 token」。实现上的关键技巧是：**让所有访存共享同一个 SID（`BARRIER_SEQ_ID = 1`）**，于是它们全部归入同一条序列，按源码顺序首尾相接。

#### 4.3.2 核心流程

barrier 路径比 alias 路径更激进，流程如下：

```
预处理遍：遇 gpu.barrier → 置 hasBarrierOp=true，并立即 eraseOp（barrier 被删除）
检查阶段：因 hasBarrierOp=true，把 srcToMemSeqInfoMap 里所有序列的计数汇总，
         清空 map，重置为单一 {BARRIER_SEQ_ID: 总写数, 总访存数}
         （getMemOpSeqId 在 hasBarrierOp 时恒返回 1，故 SID 全相同）
变换遍 ：与 alias 路径完全相同的串接逻辑（因为只剩一条序列）
```

要点：

- **barrier 被物理删除**：输出 IR 里 `CHECK-NOT: gpu.barrier`。它的同步语义被 token 链取代。
- **全串行**：因为只有一条序列，所有访存排成一条长链。这就是 README 说的「conservative rules … may introduce performance loss」——barrier 模式比 alias 模式更保守，性能损失更大，但语义绝对安全。
- **与 alias 模式互斥优先**：barrier 是更强的约束，一旦命中就走单序列，不再按指针哈希分多条。

#### 4.3.3 源码精读

**barrier 的检测与删除**：

> [AutoGenMemoryToken.cpp:549-553](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/AutoGenMemoryToken.cpp#L549-L553) —— 预处理遍里：
> ```cpp
> if (isa<mlir::gpu::BarrierOp>(op)) {
>     currentBlockMemSeqs.hasBarrierOp = true;
>     rewriter.eraseOp(op);
>     return WalkResult::advance();
> }
> ```
> 注意它直接 `eraseOp`，所以输出里不再有 `gpu.barrier`。

**全访存合并成单序列**：

> [AutoGenMemoryToken.cpp:588-597](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/AutoGenMemoryToken.cpp#L588-L597) —— 若 `hasBarrierOp`，把各序列的 `writeMemOpCounter`/`memOpCounter` 求和，清空 map，只留一条 `BARRIER_SEQ_ID`（[AutoGenMemoryToken.cpp:65](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/AutoGenMemoryToken.cpp#L65) 定义为常量 1）。配合 `getMemOpSeqId` 开头 `if (hasBarrierOp) return BARRIER_SEQ_ID;`，所有访存 SID 相同，自然成一条链。

**用户手写 token 时的告警**：

> [AutoGenMemoryToken.cpp:578-585](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/AutoGenMemoryToken.cpp#L578-L585) —— 若 kernel 同时有用户手写 token 和 barrier，pass 会 `emitWarning`（「debug_barrier should not be added when memory tokens are added manually」）并整体跳过，尊重用户手写的 token 流。

#### 4.3.4 代码实践

**实践目标**：在 `test_barrier_add_kernel` 上确认 barrier 被删除、全部访存被串成单链。

**操作步骤**：

1. 读输入 [op-conversion-barrier.mlir:3-26](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-barrier.mlir#L3-L26)。注意 [第 20 行](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-barrier.mlir#L20) 的 `gpu.barrier`，它夹在「两个 load（算 `%9+%12`）」和「一个 store（写 `%15`）」之间。
2. 因为有 barrier，三个访存（2 load + 1 store）全部进 `BARRIER_SEQ_ID` 这一条序列。
3. 对照期望输出 [op-conversion-barrier.mlir:28-32](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-barrier.mlir#L28-L32)。

**需要观察的现象**：

- `CHECK-NOT: gpu.barrier` —— barrier 确实被删了。
- 两个 load 之间、load 与 store 之间都用 `token=` 首尾相接：
  - `%r1, %t1 = load_ptr_tko ...`
  - `%r2, %t2 = load_ptr_tko ... token=%t1`
  - `%t3 = store_ptr_tko ... token=%t2`

**预期结果**：三条访存形成单链 `%t1→%t2→%t3`，与 CHECK 行一致。注意这里**即使两个 load 读的是不同地址（`%8` 和 `%11`）也被串起来**——这正是 barrier 全串行化的保守特性，与 4.2 alias 模式只串同地址截然不同。详细的链路画法见第 5 节综合实践。

#### 4.3.5 小练习与答案

**练习 1**：barrier 模式为什么不按指针哈希分多条序列，而要合成一条？

> **答**：`debug_barrier` 的语义是「barrier 前的**所有**访存先于 barrier 后的**所有**访存」，与是否同地址无关。只有把全部访存并入同一条序列、按源码顺序串成全序，才能忠实表达这种跨地址的全局同步。按指针分多条无法表达「跨地址」的先后。

**练习 2**：barrier 模式相比 alias 模式，性能损失为什么更大？

> **答**：alias 模式只串「同地址且含写」的序列，不同地址的访存仍可乱序并行；barrier 模式把**所有**访存（含无关地址、含纯读）排成一条链，任何两次访存都不能乱序，并发度被压到最低。这是用性能换「绝对正确」的保守策略。

## 5. 综合实践

**任务**：阅读 [op-conversion-barrier.mlir](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-barrier.mlir) 的 `test_barrier_add_kernel`，说明 `gpu.barrier` 被移除后，token 如何把它前后的 load/store 串行化，并画出 token 依赖链。

**步骤**：

1. **定位 barrier 与访存**。输入（[第 3-26 行](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-barrier.mlir#L3-L26)）里关键三行：
   - `%9 = tt.load %8`（第 15 行，barrier 之前）
   - `%12 = tt.load %11`（第 18 行，barrier 之前）
   - `gpu.barrier`（第 20 行）
   - `tt.store %15, %13`（第 23 行，barrier 之后）

2. **跑一遍 pass 的判定**。预处理遍发现 `gpu.barrier` → `hasBarrierOp=true`，删除 barrier，三个访存（经主转换后是 `load_ptr_tko`×2 + `store_ptr_tko`×1）全部 SID=`BARRIER_SEQ_ID`；写计数 ≥1 且访存数 >1，保留这一条序列。

3. **画出 token 依赖链**。参照期望输出（[第 28-32 行](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-barrier.mlir#L28-L32)）：

   ```
   make_token ──t0──> load_ptr_tko(%8)  ──t1──> load_ptr_tko(%11) ──t2──> store_ptr_tko(%15) ──t3──>
                                            ↑ barrier 原本在此处 ↑
   ```

   - `%r1, %t1 = load_ptr_tko %8`：接 `t0`，产 `t1`。
   - `%r2, %t2 = load_ptr_tko %11 token=%t1`：接 `t1`，产 `t2`。
   - `%t3 = store_ptr_tko %15, %13 token=%t2`：接 `t2`，产 `t3`。

4. **解释语义**：原本 `gpu.barrier` 要求「两个 load 完成 → 才能开始 store」。删除 barrier 后，这条 token 链把三个访存锁成全序 `load8 ≻ load11 ≻ store15`，恰好等价于 barrier 的同步语义；且因为是无序模型下唯一的显式依赖，硬件只要遵守这一条链即可，链外（本例没有）仍可自由调度。

5. **进阶对比**：再看同文件的 `test_barrier_layer_norm_bwd`（[第 36-139 行](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-barrier.mlir#L36-L139)），它同时含 `while`/`if`/`atomic`。观察期望输出（[第 141-164 行](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-barrier.mlir#L141-L164)）中 token 是如何作为 `loop` 的 `iter_values`、`if` 的额外结果值传播的，验证 4.2.2 讲的控制流穿透规则。

> 说明：若你尚未按 u4-l1 的方法构建 `triton-cuda-tile-opt`，本实践为**源码阅读型**，不必实际运行；待本地构建好工具后，可用其 RUN 行（[第 1 行](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-barrier.mlir#L1)）复现验证，结果**待本地验证**。

## 6. 本讲小结

- CUDA Tile IR 采用**无序内存模型**：全局访存默认不保证顺序，需要顺序时用 **memory token** 显式声明「生产者→消费者」依赖，token 像接力棒一样在一次访存的输出与下一次访存的输入之间传递。
- `AutoGenMemoryTokenPass` 用「**两遍扫描**」自动补 token：预处理遍按 SID 收集访存序列，检查阶段过滤掉「单访存」或「纯读」序列，变换遍为幸存序列造 `make_token` 并逐个串接。
- **SID** 是序列号：ptr 类访存用指针值哈希，view 类用视图+索引哈希；同 SID 的访存视为访问同一数据。含写且多于一个访存的序列才会被串。
- token 要跨控制流正确传播：`if` 把 token 变成额外结果（两支 yield）、`for`/`loop` 把 token 变成循环迭代变量（`continue`/`break` 也挂 token）。
- **`debug_barrier`（`gpu.barrier`）触发全串行化**：pass 删除 barrier，让全部访存共享 `BARRIER_SEQ_ID`，按源码顺序排成单链，等价于 barrier 的全局同步语义——更保守、性能损失更大，但绝对安全。
- 若 kernel 已含用户手写 token，pass 整体跳过以尊重用户；若同时有 barrier 则发告警。开关 `enable_autogen_alias_mem_token` 默认开，关掉后只保留 barrier 路径。

## 7. 下一步学习建议

- **u3-l7（FMA 融合与 bytecode 收尾）**：本 pass 之后紧接 inliner / fuse-fma / strip-debuginfo，再到 `write_bytecode` 与 `only_contain_legal_dialects` 收尾。读完本讲可直接进入流水线最后一段，看 token 化后的 IR 如何被序列化交给 `tileiras`。
- **u4-l1（triton-cuda-tile-opt 与 lit/FileCheck 测试）**：学完本讲后，动手用 `triton-cuda-tile-opt` 跑 [op-conversion-barrier.mlir](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-barrier.mlir) 与 [op-conversion-auto-memtoken.mlir](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-auto-memtoken.mlir)，亲眼看到 token 链的生成。
- **u4-l3（fallback 容错）**：理解为何无序模型下「跨 tile 块流动（splitK/streamK）」会算错、以及运行期如何回退 PTX 后端，与本讲的「别名访存需 token」对照阅读，能更全面理解 TileIR 内存模型的边界。
- 若对 token 的「接力棒」语义仍想深入，建议在本地构建后用 `--debug` 跑本 pass（`DEBUG_TYPE = "add-memory-token"`，见 [AutoGenMemoryToken.cpp:19](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/AutoGenMemoryToken.cpp#L19) 与其中的 `LLVM_DEBUG` 打印），观察 SID 分配与序列过滤的真实过程。
