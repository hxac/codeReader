# Tile 层：Assign 函数到 Ascend C API

## 1. 本讲目标

本讲打开 ATVOSS 五层架构中倒数第二层——**Tile 层**的黑盒，回答一个核心问题：

> 用户在 `Compute()` 里写下的声明式表达式（如 `out = Abs(in)`），经过 u3-l1 的表达式线性化、DAG 构建、Alloc/Free 插入之后，最终是怎样变成一条条真实的 **Ascend C 指令**的？

学完本讲你应当能够：

- 说清 `Tile::Evaluate` 这个执行入口如何用「求值器递归 + 模板特化」驱动整条线性化表达式。
- 区分四类节点的求值器：`OpCopyIn`/`OpCopyOut`（GM↔UB 搬运）、`OpCopy`（UB↔UB 搬运）、`OpAlloc`/`OpFree`（缓冲分配与释放）、数学算子（`OpAbs`/`OpAdd`…，已在本讲串联）。
- 理解 `DataCopyPad`、`bufPools.AllocTensor`、`Mutex::Lock/Unlock`、`PipeBarrier` 这些 Ascend C 原语分别出现在哪个求值器里。
- 说清一次 Tile 执行中 **MTE2 → V → MTE3** 的流水同步顺序，以及 `pingPong` 双缓冲如何隐藏搬运延迟。

本讲是 u3-l1（求值器系统）在「张量搬运类节点」上的落地，也是 u2-l9（Block 层 Tile 切分）里那句「求值器用 MTE2→V→MTE3 同步配合双缓冲隐藏搬运延迟」的兑现。

## 2. 前置知识

在进入源码前，先用通俗语言铺垫几个昇腾 AI Core 的硬件概念。本讲不要求你写过 Ascend C，但需要建立下面这张「流水线 + 内存」的直觉图。

### 2.1 三级流水线（Pipe）与同步

昇腾 AI Core 内部有多条**并行流水线**，各自负责不同硬件单元，相互独立、可并发执行。本讲涉及三条：

| 流水线 | 名称 | 职责 | 本讲对应操作 |
|--------|------|------|--------------|
| `PIPE_MTE2` | Memory Transfer Engine 2 | GM（全局显存）→ UB（统一缓冲）搬运 | `OpCopyIn` |
| `PIPE_V` | Vector | 向量计算 | `OpAbs`/`OpAdd` 等数学算子 |
| `PIPE_MTE3` | Memory Transfer Engine 3 | UB → GM 搬运 | `OpCopyOut` |

问题在于：相邻流水线之间存在**生产者-消费者依赖**。例如 `PIPE_V` 要对 `PIPE_MTE2` 刚搬进 UB 的数据做 `Abs`，必须等搬运完成，否则读到的是旧数据。ATVOSS 用两类同步原语表达这种依赖：

- **`PipeBarrier<PIPE>()`**：粗粒度屏障，强制某条流水线把**之前所有**指令排空。最粗暴的形式是 `PipeBarrier<PIPE_ALL>()`，等于把三条流水线全部同步，简单但牺牲并发。
- **`Mutex::Lock<PIPE>(id)` / `Unlock<PIPE>(id)`**：细粒度互斥锁，**以缓冲 `id` 为粒度**建立依赖。同一 `id` 上的 `Unlock<MTE2>` 与 `Lock<V>` 构成一次精确的「等 MTE2 写完，再让 V 读」握手，不影响其他缓冲。

> 这是本讲最关键的认知点：**同步的粗细决定了双缓冲能否生效**。`PipeBarrier` 会让两条流水线不得不整体停下来等；`Mutex` 只在同一个缓冲上握手，不同缓冲可以并行推进。

### 2.2 两级内存：GM 与 UB

- **GM（Global Memory）**：整块芯片共享的显存，容量大（GB 级）、带宽相对低。算子的输入输出最终放在这里。
- **UB（Unified Buffer）**：每个 AI Core 核**私有**的片上高速缓存，容量小（Ascend 950 为 240 KB）、带宽高。Vector 计算必须先要把数据搬进 UB 才能算。

所以一个 Tile 的执行永远是 **GM → UB（MTE2）→ 计算（V）→ UB → GM（MTE3）** 这条固定路线。这正是本讲要追踪的同步链。

### 2.3 一个关键提醒：`_ATVOSS_ARCH35_` 双分支

本讲源码里到处是 `#if _ATVOSS_ARCH35_ ... #else ... #endif`，它由硬件架构决定（[include/common/arch.h:14-18](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/common/arch.h#L14-L18)）：

- 当目标为 `__DAV_C310__` / `__DAV_310R6__` / `__NPU_ARCH__ == 5102` 时为 `1` → 走 **`Mutex` 细粒度锁**路径（双缓冲生效）。
- 否则为 `0` → 走 **`PipeBarrier<PIPE_ALL>()`** 粗粒度路径。

本仓库默认目标芯片是 **ascend950（对应 `Arch::DAV_3510`，见 [include/common/arch.h:22-25](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/common/arch.h#L22-L25)）**，因此默认编译走的是 `PipeBarrier` 分支。而本讲（以及实践任务）重点讲解的 `Mutex` + ping-pong 机制，对应的是 `#if _ATVOSS_ARCH35_` 为 `1` 的分支——它体现了 ATVOSS「细粒度流水同步」的设计意图。请务必带着「两条分支」的意识读下面的源码。

## 3. 本讲源码地图

本讲聚焦 `include/elewise/tile/` 目录下的两个文件，并向上、向下各牵出几个支撑文件：

| 文件 | 作用 | 本讲角色 |
|------|------|----------|
| `include/elewise/tile/tile_evaluate.h` | Tile 层执行入口 `Evaluate` | 求值器的总开关 |
| `include/elewise/tile/tensor_evaluator.h` | `OpCopyIn/OpCopyOut/OpCopy/OpAlloc/OpFree` 的求值器特化 | 本讲主角 |
| `include/evaluator/eval_base.h` | `Evaluator` 主模板、`OpAssign`/`OpAndThen`/`Param`/`LocalVar` 特化、`Assign` 函数 | u3-l1 已讲，本讲引用 |
| `include/operators/math_evaluator.h` | 数学算子求值器（`AbsAssign`/`AddAssign`…） | 「Assign 函数」的来源 |
| `include/elewise/block/block_info_tile.h` | `BlockTensor`：`GetUbTensor`/`CopyIn`/`CopyOut` | 搬运动作的真正实现 |
| `include/utils/buf_pool/block_buf_pool.h` | `BlockBufferEx`：`AllocTensor` | UB 缓冲的物理分配 |
| `include/elewise/block/schedule.h` | `Process` 循环构造 `ContextData` | 上下文与 `pingPong` 的来源 |
| `include/elewise/graph/buffer.h` | `GetBufferId`、`BufType`、ping/pong 映射 | 缓冲 ID 的生成 |
| `include/common/type_def.h` | `ContextData` 结构 | 单 Tile 上下文包裹 |

## 4. 核心概念与源码讲解

### 4.1 Tile::Evaluate 执行入口：求值器如何驱动线性化表达式

#### 4.1.1 概念说明

经过 u3-l4（表达式线性化与图优化 Pass）的处理，用户写的 `(out = Abs(in))` 这样一棵嵌套表达式树，已经被「拍平」成一串顺序执行的 `OpAssign` 节点，并由 DAG/AllocInserter/FreeInserter 插入好了搬运与缓冲管理节点。最终得到的是一个「线性化的表达式类型」`ExprTile`（在 [include/elewise/block/schedule.h:70-71](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L70-L71) 由 `BuildExpression<decltype(result4)>` 重建）。

Tile 层要做的事很纯粹：**对这串线性化表达式，按顺序逐个求值**。它不关心表达式长什么样——一切差异都被「求值器模板特化 + 递归」吸收了。这就是标题里「Assign 函数到 Ascend C API」的含义：每个 `OpAssign<dst, OpXxx>` 求值器特化，最终都会调用一个 `XxxAssign` 辅助函数（如 `AbsAssign`、`AddAssign`、`CastAssign`、`CopyIn`、`CopyOut`…），而 `XxxAssign` 内部就是一句 Ascend C 指令。

#### 4.1.2 核心流程

一次 `Evaluate<ExprTile>(context)` 的执行流程：

```text
Evaluate<Expr>(context)                       # tile_evaluate.h 入口
   │
   └─> 构造 Atvoss::Tile::Evaluator<Expr>{}
       并调用 operator()(Expr{}, context)
   │
   └─> Evaluator<Expr> 经由 Evaluator<Expression<T>> 继承
       匹配到对应特化：
         • OpAndThen<L,R>  → 先求 L，再求 R（逗号串联，整体顺序执行）
         • OpAssign<dst,OpXxx> → 调 XxxAssign(...) → Ascend C 指令
         • OpCopyIn/OpCopyOut/OpAlloc/OpFree → 搬运/缓冲特化
         • Param<N>/LocalVar<N> → 从 context 取出对应 LocalTensor（叶子）
```

关键点：`OpAndThen` 是「顺序胶水」。u2-l1 讲过，逗号表达式 `operator,` 触发 `OpAndThen`，把多句 `OpAssign` 串成一条链。求值时它先递归左子树、再递归右子树，从而保证 Alloc → CopyIn → 计算 → Free → CopyOut 的严格顺序。

#### 4.1.3 源码精读

Tile 层入口 `Evaluate` 极其精简，只有一行真正逻辑——构造求值器并调用（[include/elewise/tile/tile_evaluate.h:33-37](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tile_evaluate.h#L33-L37)）：

```cpp
template <typename Expr, typename Context>
__aicore__ inline void Evaluate(Context& context)
{
    Atvoss::Tile::Evaluator<Expr>{}(Expr{}, context);
}
```

它把所有重活交给 `Evaluator<Expr>`。而 `Evaluator` 的「分派骨架」来自 u3-l1 讲过的 eval_base.h，本讲只引用三处关键特化：

1. **`Evaluator<Expression<T>>` 直接继承 `Evaluator<T>`**（[include/evaluator/eval_base.h:35-36](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/evaluator/eval_base.h#L35-L36)）：剥掉 `Expression` 外壳，匹配内层类型。

2. **`OpAndThen` 特化保证顺序执行**（[include/evaluator/eval_base.h:85-96](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/evaluator/eval_base.h#L85-L96)）：先在两条子链之间插一句 `PipeBarrier<PIPE_V>()`，再递归左、右子树。这句屏障的作用是：在两条 `OpAssign` 语句交接处给 V 流水线一个汇合点，保证前一句的计算结果对后一句可见。

3. **`OpAssign<T,U>` 特化是「翻译」的统一入口**（[include/evaluator/eval_base.h:73-82](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/evaluator/eval_base.h#L73-L82)）：调用 `Assign(Evaluator<T>{}(lhs), Evaluator<U>{}(rhs))`。当 `U` 是 `OpAbs`、`OpAdd` 等时，有更精确的特化（如 `Evaluator<OpAssign<T, OpAbs<U>>>`）接管，转而调用 `AbsAssign`。当没有任何更精确特化时，落到这里的 `Assign(T&, const U&)` 即 `dst = src`。

#### 4.1.4 代码实践

**实践目标**：在不读 Tile 源码细节的前提下，仅凭本节的「求值器递归」模型，手工推导 `out = Abs(in)` 的求值分派路径。

**操作步骤**：

1. 打开 [examples/abs/abs.cpp:24-32](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L24-L32)，确认 `AbsCompute::Compute()` 返回的就是 `(out = Abs(in))`。
2. 回顾 u3-l1：`Evaluator<OpAssign<T, U>>` 会先看 `U`，对 `OpAbs` 优先匹配到 [include/operators/math_evaluator.h:502-515](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L502-L515) 的 `Evaluator<OpAssign<T, OpSqrt<U>>>`（`OpAbs` 同构，见 [include/operators/math_evaluator.h:480-493](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L480-L493) 的 `AbsAssign`）。
3. 画出分派链：`Evaluate<Expr>` → `Evaluator<OpAndThen<...>>`（逐句）→ `Evaluator<OpAssign<out, OpAbs<in>>>` → `AbsAssign(...)` → `AscendC::Abs(...)`。

**需要观察的现象 / 预期结果**：你能用「求值器主模板 + 特化 + 递归」这套机制，在纸上把任何一句 `OpAssign` 解释到一条 `AscendC::*` 指令，而无需运行任何代码。这正是 ATVOSS「零运行时开销」的体现——所有分派在编译期完成。

#### 4.1.5 小练习与答案

**练习 1**：`Evaluator<Expression<T>>` 为什么直接继承 `Evaluator<T>` 而不是自己实现 `operator()`？

**参考答案**：因为 `Expression<T>` 只是 u2-l1 讲过的「统一外壳」，真正的结构信息在内层 `T`。通过 `struct Evaluator<Expression<T>> : Evaluator<T> {}` 继承，可以在求值入口把外壳透明剥掉，让后续特化都直接针对内层 `T`（如 `OpAssign`、`Param`）来写，避免为每个 `Op` 重复写 `Expression<Op>` 形态。

**练习 2**：为什么 `Evaluator<OpAndThen<T,U>>` 的 `operator()` 里要有一句 `AscendC::PipeBarrier<PIPE_V>()`？

**参考答案**：`OpAndThen` 把多句 `OpAssign` 串联。相邻两句之间，前一句的 V 计算结果必须先落盘到 UB，后一句才能读到。`PipeBarrier<PIPE_V>()` 给 V 流水线插入一个汇合点，确保前一句的计算完成后，后一句才开始读取相关 UB 缓冲。

---

### 4.2 数据搬运求值器：OpCopyIn / OpCopyOut / OpCopy → DataCopyPad

#### 4.2.1 概念说明

GM 与 UB 之间不会自动交换数据，必须显式搬运。ATVOSS 用三个表达式节点描述搬运：

- **`OpCopyIn`**：把 GM 上的输入搬进 UB（对应硬件 `PIPE_MTE2`）。
- **`OpCopyOut`**：把 UB 上的输出搬回 GM（对应硬件 `PIPE_MTE3`）。
- **`OpCopy`**：UB↔UB 拷贝，用于「纯变量搬运」（u2-l1 提到 `OpAssign` 对纯变量插入 `OpCopy`），对应 `PIPE_V`。

这三个节点**由框架自动插入**（u3-l3 的 DAG 构建），用户一般不直接写。本节看它们的求值器如何落到 `DataCopyPad` / `DataCopy`。

#### 4.2.2 核心流程

一次 CopyIn/CopyOut 的搬运链：

```text
Evaluator<OpCopyIn<in>>(op, context)
   │  1. Evaluator<in>{}(op.GetData(), context)  → 拿到 BlockTensor 对象 obj
   │  2. bufferId = GetBufferId<PARAM>(context.pingPong)  → 选 ping/pong 缓冲
   │  3. 同步：Mutex::Lock<PIPE_MTE2>(bufferId)   （或 PipeBarrier<PIPE_ALL>）
   │  4. obj.CopyIn(gmOffset, elementNum)         → 内部调 DataCopyPad（GM→UB）
   │  5. 同步：Mutex::Unlock<PIPE_MTE2>(bufferId); Mutex::Lock<PIPE_V>(bufferId)
   └─
```

CopyOut 的链路对称：先 `Unlock<PIPE_V>` + `Lock<PIPE_MTE3>`，再 `CopyOut`，最后 `Unlock<PIPE_MTE3>`。注意步骤 5 的 `Lock<PIPE_V>`：它**不阻塞当前求值**，而是给后续 V 计算建立一个「等本缓冲 MTE2 写完」的依赖——这是 4.4 节双缓冲能生效的伏笔。

#### 4.2.3 源码精读

**三个底层搬运函数**（[include/elewise/tile/tensor_evaluator.h:25-57](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L25-L57)）是「Assign 函数」的搬运版，直接封装 Ascend C：

```cpp
// UB → UB，第 25-30 行
template <typename OperationShape, typename T>
__aicore__ inline void CopyAssign(LocalTensor<T>& dst, const LocalTensor<T>& src, OperationShape& s)
{ AscendC::DataCopy(dst, src, s.axis0); }

// GM → UB，第 38-44 行
template <typename T>
__aicore__ inline void CopyIn(LocalTensor<T> dst, GlobalTensor<T> src, uint64_t copyCnt) {
    AscendC::DataCopyExtParams copyParams{1, static_cast<uint32_t>(copyCnt * sizeof(T)), 0, 0, 0};
    AscendC::DataCopyPadExtParams<T> padParams{false, 0, 0, 0};
    AscendC::DataCopyPad(dst, src, copyParams, padParams);
}

// UB → GM，第 52-57 行
template <typename T>
__aicore__ inline void CopyOut(GlobalTensor<T> dst, LocalTensor<T> src, uint64_t copyCnt) {
    AscendC::DataCopyExtParams copyParams{1, static_cast<uint32_t>(copyCnt * sizeof(T)), 0, 0, 0};
    AscendC::DataCopyPad(dst, src, copyParams);
}
```

要点：`CopyIn`/`CopyOut` 用 `DataCopyExtParams{1, byteLen, 0, 0, 0}` 声明「1 个 block、长度为 `copyCnt*sizeof(T)` 字节」的搬运；CopyIn 还带 `DataCopyPadExtParams` 控制是否补齐（此处 `false` 不补齐）。`copyCnt` 来自 `context.elementNum`（即本 Tile 的实际元素数，尾部 Tile 会小于 TileShape）。

**`Evaluator<OpCopyIn<T>>`**（[include/elewise/tile/tensor_evaluator.h:60-85](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L60-L85)）把搬运与同步包在一起：

```cpp
auto& obj = Evaluator<T>{}(op.GetData(), context);   // 取 BlockTensor
uint32_t bufferId = GetBufferId<...,PARAM>(context.pingPong);
#if _ATVOSS_ARCH35_
    AscendC::Mutex::Lock<PIPE_MTE2>(bufferId);        // 等该缓冲前一次消费完
#else
    AscendC::PipeBarrier<PIPE_ALL>();
#endif
obj.CopyIn(context.gmOffset, context.elementNum);     // GM→UB
#if _ATVOSS_ARCH35_
    AscendC::Mutex::Unlock<PIPE_MTE2>(bufferId);      // MTE2 写完
    AscendC::Mutex::Lock<PIPE_V>(bufferId);           // 让后续 V 等本缓冲就绪
#else
    AscendC::PipeBarrier<PIPE_ALL>();
#endif
```

`Evaluator<OpCopyOut<T>>`（[include/elewise/tile/tensor_evaluator.h:88-113](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L88-L113)）对称：先 `Unlock<PIPE_V>(bufferId)` + `Lock<PIPE_MTE3>(bufferId)`，搬运后再 `Unlock<PIPE_MTE3>(bufferId)`。

**`Evaluator<OpAssign<T, OpCopy<U>>>`**（[include/elewise/tile/tensor_evaluator.h:116-131](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L116-L131)）处理 UB↔UB 拷贝，直接调上面的 `CopyAssign`，注意它**没有任何 Mutex**（同为 V 流水，无需跨流水同步）：

```cpp
return Atvoss::Tile::CopyAssign(
    Evaluator<T>{}(op.GetLhs(), context).GetUbTensor(),
    Evaluator<U>{}(op.GetRhs().GetData(), context).GetUbTensor(), operationShape);
```

最后，`obj.CopyIn(...)` 里 `obj` 是 `BlockTensor`，它的 `CopyIn`/`CopyOut` 实现在 [include/elewise/block/block_info_tile.h:37-49](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/block_info_tile.h#L37-L49)，内部就是 `SetGlobalBuffer` + `Atvoss::Tile::CopyIn/CopyOut`，把 GM 指针（`gmAddr_`）按 `curGmOffset` 偏移后传给上面的搬运函数。

#### 4.2.4 代码实践

**实践目标**：验证 `CopyIn` 的搬运长度计算与 Tile 切分一致。

**操作步骤**：

1. 读 [include/elewise/block/block_info_tile.h:37-42](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/block_info_tile.h#L37-L42)，确认 `BlockTensor::CopyIn(curGmOffset, copyLen)` 把 `copyLen` 透传给 `Atvoss::Tile::CopyIn`。
2. 读 [include/elewise/tile/tensor_evaluator.h:39-43](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L39-L43)，确认字节数为 `copyCnt * sizeof(T)`。
3. 回到 u2-l9：`context.elementNum` 在整 Tile 为 `BASIC_BLOCK`（TileShape 累乘），尾 Tile 为 `cfg.tileCnt`（余数）。

**需要观察的现象 / 预期结果**：当 `totalElemCnt` 不能被 `BASIC_BLOCK` 整除时，最后一个 Tile 的 `copyCnt` 是余数 `tileCnt`，于是 `DataCopyPad` 的 `byteLen` 也按比例缩短，**不会越界搬运**。这说明 Tile 层的搬运长度天然适配尾 Tile，无需特殊分支。

#### 4.2.5 小练习与答案

**练习 1**：`OpCopyIn` 求值器里 `obj.CopyIn(context.gmOffset, context.elementNum)` 的两个参数分别来自哪里？

**参考答案**：`gmOffset` 和 `elementNum` 都来自 Block 层构造的 `ContextData`（见 [include/elewise/block/schedule.h:241](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L241) 的 `i * BASIC_BLOCK` 与 `BASIC_BLOCK`），它们由 Kernel 层的 `CalGMOffset`/`CalCurCoreEleCnt` 一路下传，决定了本核本 Tile 从 GM 的哪个偏移搬多少元素。

**练习 2**：为什么 `Evaluator<OpAssign<T, OpCopy<U>>>`（UB↔UB）里没有任何 `Mutex` 或 `PipeBarrier`？

**参考答案**：`OpCopy` 落到 `AscendC::DataCopy`，与读它的数学算子同属 `PIPE_V`，不存在跨流水依赖，因此无需同步原语。跨流水依赖只发生在 MTE2→V 与 V→MTE3 的边界上，分别由 `OpCopyIn` 和 `OpCopyOut` 的 Mutex/PipeBarrier 负责。

---

### 4.3 缓冲管理求值器：OpAlloc / OpFree → bufPools.AllocTensor

#### 4.3.1 概念说明

UB 是稀缺资源，不能给每个变量都静态预留一大块。ATVOSS 用 `OpAlloc`/`OpFree` 这对节点做 UB 缓冲的「按需分配 / 用完释放」（由 u3-l4 的 `AllocInserter`/`FreeInserter` 在首用前/末用后插入）。求值器要解决的问题是：

- 同一个变量（Param 或 LocalVar）在 UB 里到底用哪一块缓冲？这由 `GetBufferId` 按 `pingPong` 算出。
- 分配的物理动作是什么？由 `BlockBufferEx::AllocTensor` 把一块 `TBuf` 切片 `ReinterpretCast` 成目标类型的 `LocalTensor`。

`OpAlloc` 还要按变量身份分支处理：LocalVar（临时变量）、IN 入参、OUT 出参走不同的锁策略；`OpFree` 则负责把持有的 V 锁释放掉，为下一轮 ping-pong 让出缓冲。

#### 4.3.2 核心流程

`Evaluator<OpAlloc<T>>` 的三分支（[include/elewise/tile/tensor_evaluator.h:134-177](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L134-L177)）：

```text
if 标量类型 → 直接返回（不占 UB）
else if LocalVar（!HasUsage）  → AllocTensor(tmpId); Lock<PIPE_V>(tmpId)   // 临时量，V 锁
else if IN / IN_OUT           → AllocTensor(inId)                           // 仅分配，不锁（CopyIn 来锁）
else if OUT                   → AllocTensor(outId); Lock<PIPE_V>(outId)     // 出参，先占 V 锁
```

为什么 IN 不锁、OUT 要锁？因为 IN 的「就绪」由后续 `OpCopyIn` 的 `Lock<PIPE_MTE2>` 负责（MTE2 是 IN 数据的写入方）；而 OUT 没有 CopyIn，它从一开始就要被 V 计算（如 `Abs`）写入，所以 `OpAlloc<out>` 提前 `Lock<PIPE_V>(outId)` 占位，等真正写它的计算到来。

#### 4.3.3 源码精读

`Evaluator<OpAlloc<T>>`（[include/elewise/tile/tensor_evaluator.h:134-177](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L134-L177)）核心片段：

```cpp
if constexpr (!HasUsage<T>{}) {                       // LocalVar 临时变量
    uint32_t tmpId = GetBufferId<...,LOCAL_VAR>(context.pingPong);
    context.bufPools.AllocTensor(obj.GetUbTensor(), tmpId);
#if _ATVOSS_ARCH35_
    AscendC::Mutex::Lock<PIPE_V>(tmpId);              // 临时量默认 V 锁
#endif
} else if constexpr (T::usage == IN || T::usage == IN_OUT) {
    uint32_t inId = GetBufferId<...,PARAM>(context.pingPong);
    context.bufPools.AllocTensor(obj.GetUbTensor(), inId);   // 仅分配
} else if constexpr (T::usage == OUT) {
    uint32_t outId = GetBufferId<...,PARAM>(context.pingPong);
    context.bufPools.AllocTensor(obj.GetUbTensor(), outId);
#if _ATVOSS_ARCH35_
    AscendC::Mutex::Lock<PIPE_V>(outId);              // 出参 V 锁
#endif
}
```

> 这里 `HasUsage<T>`（[include/expression/expr_template.h:295-299](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L295-L299)）是一个 SFINAE trait：`Param` 有 `usage` 成员故为 `true`，`LocalVar` 没有 `usage` 故为 `false`，用它来区分两类叶子节点。

`Evaluator<OpFree<T>>`（[include/elewise/tile/tensor_evaluator.h:180-210](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L180-L210)）则是 Alloc 的镜像，对 LocalVar 和 IN 都执行 `Mutex::Unlock<PIPE_V>(id)`，把 V 锁交还——这样下一轮 Tile（如果它复用同一个 bufferId）才不会永远等下去。

`bufPools.AllocTensor` 的物理实现极其精简（[include/utils/buf_pool/block_buf_pool.h:34-38](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/buf_pool/block_buf_pool.h#L34-L38)）：

```cpp
template <typename T>
__aicore__ inline void AllocTensor(LocalTensor<T>& inTensor, uint32_t bufferId)
{
    inTensor = tensorPool_[bufferId * BLOCK_LEN].template ReinterpretCast<T>();
}
```

也就是说，UB 在 `Init()` 时被一次性切成 `TILE_NUM * TILE_SIZE` 的平铺数组 `tensorPool_`（[include/utils/buf_pool/block_buf_pool.h:28-32](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/buf_pool/block_buf_pool.h#L28-L32)），`AllocTensor` 只是按 `bufferId * BLOCK_LEN` 取出第 `bufferId` 块、`ReinterpretCast` 成目标类型。所谓「分配」其实是「按 ID 取切片」，零成本且可复用——这正是 ping-pong 能反复占用同一物理块的基础。

#### 4.3.4 代码实践

**实践目标**：理解 `GetBufferId` 如何把 `(Param 序号, pingPong)` 映射成 UB 切片 ID。

**操作步骤**：

1. 读 [include/elewise/graph/buffer.h:509-523](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/buffer.h#L509-L523)：`GetBufferId<TL, N, bt>(isPing)` 在类型表 `TL`（即 `Context::BuffMaps`）里线性查找 `paramNum == N && bufType == bt` 的那一项，返回 `isPing ? pingBufId : pongBufId`。
2. 读 [include/elewise/graph/buffer.h:68-106](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/buffer.h#L68-L106)：`ParamBufIdMap` 把 Param N 映射到 `pingBufId`（= N-1）与 `pongBufId`（= pingBufId + pongOffset），其中 `pongOffset = paramCount + localVarCount`，保证 ping 区与 pong 区物理不重叠。

**需要观察的现象 / 预期结果**：对一个 1 入参算子（如 abs，`paramCount=2` 含 in/out，假设 `localVarCount=0`），`pongOffset=2`。于是 `in`(N=1) 的 ping=0、pong=2；`out`(N=2) 的 ping=1、pong=3。即奇偶 Tile 分别落在 UB 的 [0,1] 切片与 [2,3] 切片，**两块物理区域互不重叠**——这是双缓冲能并行的物理前提。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `OpAlloc` 对 IN 参数只做 `AllocTensor` 不加锁，而对 OUT 参数要加 `Lock<PIPE_V>`？

**参考答案**：IN 数据的「就绪」时刻是 MTE2（CopyIn）写完，由后续 `OpCopyIn` 的 `Lock<PIPE_MTE2>` 表达，所以 Alloc 阶段无需提前占锁。OUT 没有 CopyIn，它从分配起就等着被 V 计算（`Abs` 等）写入，因此 `OpAlloc<out>` 立即 `Lock<PIPE_V>(outId)`，让真正写它的计算挂在这个锁上排队。

**练习 2**：`bufPools.AllocTensor` 里没有任何「申请内存」的系统调用，为什么叫 Alloc？

**参考答案**：因为 UB 在 `BlockBufferEx::Init` 时已被 `TPipe::InitBuffer` 一次性切好（header-only 里看不到真正的硬件分配，它发生在 Ascend C 运行时）。`AllocTensor` 只是「按 `bufferId` 取一片已切好的切片并 `ReinterpretCast`」，是 O(1) 的指针运算，所以叫「分配」其实是「按 ID 取用」，可被 ping-pong 反复复用。

---

### 4.4 流水同步：PIPE_MTE2 → V → PIPE_MTE3 与 ping-pong 双缓冲

#### 4.4.1 概念说明

把 4.2、4.3 的锁拼接起来，就得到本讲最核心的图景：**每个变量缓冲上，都挂着一串 Mutex 握手，形成 MTE2→V→MTE3 的生产者-消费者链**。而 `pingPong`（= Tile 序号 `i & 1`）让相邻 Tile 落在 ping/pong 两块物理缓冲上，两块的 Mutex 独立，于是「Tile i 的 V 计算（pong）」与「Tile i+1 的 MTE2 搬运（ping）」可以**同时**推进，从而把 MTE2 的搬运延迟隐藏在 V 计算的时间里。这就是「双缓冲隐藏搬运延迟」。

> 再次提醒：这一机制对应 `_ATVOSS_ARCH35_ == 1` 的 Mutex 分支。默认 ascend950 走 `PipeBarrier<PIPE_ALL>()`，它是全流水线屏障，**不具备**这种细粒度重叠能力——每次屏障都把三条流水线整体同步一次。两种路径的代码差异请对照本节引用的源码阅读。

#### 4.4.2 核心流程

以 `out = Abs(in)` 在**单个 Tile**内、沿 `in` 与 `out` 两个缓冲各自展开的同步链（arch35 路径）：

```text
in 缓冲 (bufferId 由 pingPong 选定):
  OpCopyIn:   Lock<MTE2>(id) ──▶ DataCopyPad(GM→UB) ──▶ Unlock<MTE2>(id); Lock<V>(id)
                                 (MTE2 写)                (V 必须等 MTE2 写完)
  OpAbs:      AbsAssign ──▶ AscendC::Abs(读 in, 写 out)   (V 计算，挂在 in 的 V 锁上读)
  OpFree<in>: Unlock<V>(id)                                  (V 读完 in，释放 in 的 V 锁)

out 缓冲:
  OpAlloc<out>: AllocTensor; Lock<V>(id_out)               (出参先占 V 锁)
  OpAbs:        AscendC::Abs 写 out                         (V 计算，挂在 out 的 V 锁上写)
  OpCopyOut:    Unlock<V>(id_out); Lock<MTE3>(id_out)
              ──▶ DataCopyPad(UB→GM) ──▶ Unlock<MTE3>(id_out)
                 (MTE3 必须等 V 写完)        (MTE3 读出)
```

抽象成单缓冲上的握手序列就是固定的 **`Lock<MTE2>` → `Unlock<MTE2>` → `Lock<V>` → … → `Unlock<V>` → `Lock<MTE3>` → `Unlock<MTE3>`**，即 **MTE2 写 → V 读/写 → MTE3 读** 的三段式。

#### 4.4.3 源码精读

**pingPong 的来源**——Block 层 `Process` 循环里，第 5 个构造参数 `i & 1`（[include/elewise/block/schedule.h:239-247](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L239-L247)）：

```cpp
for (; i < cfg.wholeLoop; i++) {
    ContextDataT context{blockTensorsTile, blockLocalVars, bufPools_,
                         i * BASIC_BLOCK, BASIC_BLOCK, i & 1};   // ← pingPong = i & 1
    Atvoss::Ele::Tile::Evaluate<ExprTile>(context);
}
```

`ContextData` 把它存为 `uint32_t pingPong`（[include/common/type_def.h:16-25](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/common/type_def.h#L16-L25)），连同 `gmOffset`、`elementNum`、`bufPools`、`argsTensors`、`tmpTensors` 一并下传。

**pingPong 怎么影响 bufferId**——每个搬运/缓冲求值器都这样取 ID：

```cpp
uint32_t bufferId = GetBufferId<BuffMaps, T::number, BufType::PARAM>(context.pingPong);
//                                                                       ↑ 作 isPing: 0→pong, 1→ping
```

`GetBufferId` 的实现见 [include/elewise/graph/buffer.h:510-522](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/buffer.h#L510-L522)：`isPing ? pingBufId : pongBufId`。由于相邻 Tile 的 `i&1` 交替 0/1，同一变量在 Tile i 与 Tile i+1 上拿到的是**不同的 bufferId**（如 in 的 0 与 2），落进**不同的物理 UB 切片**。

**双缓冲为何不冲突**——`Mutex::Lock/Unlock<PIPE>(bufferId)` 的同步粒度是 `bufferId`，而非「整条流水线」。Tile i 用 pong（id=2）做 V 计算时，它持有的锁是 `<V>(2)`；而 Tile i+1 用 ping（id=0）做 MTE2 搬运时，它要的是 `<MTE2>(0)`。两个不同 `id` 的锁互不阻塞，于是 MTE2 搬运与 V 计算**真正并行**。等到 Tile i+1 要算 V 时，它锁 `<V>(0)`，而 pong(2) 的 V 早已做完（Tile i 已 `Unlock<V>(2)`），握手瞬间完成。这就是 ping-pong 隐藏延迟的本质：**靠不同物理缓冲 + 以 id 为粒度的锁，把相邻 Tile 的不同流水阶段解耦**。

> 对照 [include/elewise/block/schedule.h:138-142](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L138-L142)：`MAX_BUFFER_COUNT = IN_PARAMS_COUNT(=2×入参数) + OUT_PARAMS_COUNT(=2×出参数) + LOCAL_VAR_COUNT`，`UB_TILE_SIZE = Arch::UB_SIZE / MAX_BUFFER_COUNT`。这里的「×2」正是给 ping 与 pong 各预留一份，是双缓冲在容量预算上的体现。

#### 4.4.4 代码实践

**实践目标**：画出 `out = Abs(in)` 在 ascend950（`PipeBarrier` 路径）与 arch35（`Mutex` 路径）两种目标下的单 Tile 同步时序，并解释为何后者能重叠。

**操作步骤**：

1. 取 abs 样例 [examples/abs/abs.cpp:30](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L30) 的表达式 `(out = Abs(in))`，列出它在 Tile 内的求值顺序（经线性化 + Alloc/Free 插入后）：
   `OpAlloc<in>` → `OpCopyIn<in>` → `OpAlloc<out>` → `OpAssign<out, OpAbs<in>>` → `OpFree<in>` → `OpCopyOut<out>` → `OpFree<out>`。
   （具体顺序由 DAG/Pass 决定，本实践只要求你按「先备数据再算再搬出」的逻辑给出一个自洽排列即可。）
2. 对每一步，标注它在 ascend950 分支触发的 `PipeBarrier<PIPE_ALL>()` 位置。
3. 对每一步，标注它在 arch35 分支触发的 `Mutex::Lock/Unlock<PIPE>(id)` 位置，并标出 in/out 的 id。

**需要观察的现象 / 预期结果**：

- ascend950 路径：每跨一次流水边界都 `PipeBarrier<PIPE_ALL>`，三条流水线被反复整体同步，**没有重叠空间**。
- arch35 路径：in 缓冲的握手是 `Lock<MTE2>→Unlock<MTE2>→Lock<V>`…`Unlock<V>`，out 缓冲是 `Lock<V>`…`Unlock<V>→Lock<MTE3>→Unlock<MTE3>`；由于锁以 id 为粒度，Tile i+1 的 MTE2（ping, id=0）可与 Tile i 的 V（pong, id=2）并行。你能据此说明为何同一份源码在两种目标芯片上性能特征不同。

> 本实践为「源码阅读型实践」，不要求在真机运行；若想确认目标分支，可检查编译期宏：当目标 NPU 架构为 `__DAV_C310__`/`__DAV_310R6__`/`__NPU_ARCH__==5102` 时 `_ATVOSS_ARCH35_==1`（[include/common/arch.h:14-18](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/common/arch.h#L14-L18)），ascend950（dav-3510）则为 0。

#### 4.4.5 小练习与答案

**练习 1**：如果去掉双缓冲（即 ping/pong 共用同一个 bufferId），arch35 路径的 Mutex 还能隐藏 MTE2 延迟吗？

**参考答案**：不能。若共用一个 bufferId，则 Tile i+1 的 `Lock<MTE2>(id)` 会与 Tile i 尚未 `Unlock<V>(id)` 的 V 计算锁在**同一个 id** 上冲突，Tile i+1 必须等 Tile i 的 V 完全跑完才能开始搬运，MTE2 与 V 又退回串行。双缓冲的关键正是用不同物理缓冲 + 不同 id，让两套锁相互独立。

**练习 2**：`ContextData` 里的 `pingPong`、`gmOffset`、`elementNum` 三个字段，在 `OpCopyIn` 求值中分别用在哪个步骤？

**参考答案**：`pingPong` 用于 `GetBufferId(...)(context.pingPong)` 选 ping/pong 缓冲（同步锁的 id）；`gmOffset` 传给 `obj.CopyIn(context.gmOffset, ...)` 决定从 GM 的哪个偏移开始搬；`elementNum` 传给 `CopyIn` 决定搬多少元素（并换算成 `DataCopyPad` 的字节数）。三者共同把「本核本 Tile 的一段 GM 数据」精确映射到「一块 ping/pong UB 缓冲」。

## 5. 综合实践

把本讲四个模块串起来，完成一次完整的「表达式 → Ascend C 指令」追踪任务。

**任务**：以 muls 样例的 `MulsCompute`（`out = in * scalarValue`）为对象，画出它在单核单 Tile 内、从 `Evaluate` 到最终 Ascend C 指令的完整求值与同步时序。

**步骤**：

1. 打开 [examples/muls/muls.cpp](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp)（回顾 u2-l10），确认标量参与运算的表达式形态：`in` 是 Tensor 入参，`scalarValue` 是标量。
2. 经线性化/DAG/Alloc/Free 后，写出 Tile 内的求值节点序列（参照 abs 的模式，自行推导 muls 应有的 Alloc/CopyIn/计算/Free/CopyOut 顺序）。
3. 对每个节点，查表给出它匹配的求值器特化：
   - `OpAlloc`/`OpFree` → 本讲 4.3 的 [tensor_evaluator.h:134-210](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L134-L210)。
   - `OpCopyIn`/`OpCopyOut` → 本讲 4.2 的 [tensor_evaluator.h:60-113](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L60-L113)。
   - `OpAssign<out, OpMul<in, scalar>>` → [math_evaluator.h:354-383](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L354-L383)，注意 `if constexpr (std::is_scalar_v<...>)` 分派到 `MulsAssign` → `AscendC::Muls`（标量广播）。
4. 分别画出 ascend950（PipeBarrier）与 arch35（Mutex）两条路径下 `in` 缓冲与 `out` 缓冲的握手序列。
5. 回答：muls 比 abs 多了一个标量操作数，这对缓冲管理（`MAX_BUFFER_COUNT`、`UB_TILE_SIZE`）有没有影响？为什么？

**预期产出**：一张表，列出「求值节点 → 匹配特化 → 触发的 Ascend C 指令 → 触发的同步原语」四列；以及一段说明：标量不占 UB 缓冲（`if constexpr (!std::is_scalar_v<...>)` 在 [tensor_evaluator.h:142](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L142) 直接 return），因此 `MAX_BUFFER_COUNT` 不含标量，`UB_TILE_SIZE` 不变。

## 6. 本讲小结

- **Tile 层是「表达式 → Ascend C 指令」的最后一跳**：`Tile::Evaluate` 用求值器递归 + 模板特化，把线性化后的表达式逐节点翻译成 `DataCopyPad`、`Abs`、`Muls` 等指令，零运行时开销。
- **搬运靠三个底层函数**：`CopyIn`/`CopyOut`（`DataCopyPad`，GM↔UB）与 `CopyAssign`（`DataCopy`，UB↔UB），分别被 `OpCopyIn`/`OpCopyOut`/`OpCopy` 求值器特化调用。
- **缓冲管理是「按 ID 取切片」**：`OpAlloc`/`OpFree` 经 `bufPools.AllocTensor`（`ReinterpretCast` 切片）按需占用 UB，`GetBufferId` 按 `(Param 序号, pingPong)` 选出 ping 或 pong 物理块。
- **同步链是固定的 MTE2→V→MTE3**：每个缓冲上挂着一串 `Mutex::Lock/Unlock`（arch35 路径）或 `PipeBarrier<PIPE_ALL>`（ascend950 路径），把搬运与计算的跨流水依赖表达出来。
- **双缓冲靠「不同物理缓冲 + 以 id 为粒度的锁」生效**：`pingPong = i & 1` 让相邻 Tile 落在 ping/pong 两块缓冲，Mutex 以 bufferId 为粒度，使 Tile i 的 V 与 Tile i+1 的 MTE2 真正并行，从而隐藏搬运延迟。
- **务必区分两条编译分支**：`_ATVOSS_ARCH35_` 决定走 Mutex 还是 PipeBarrier；本仓库默认 ascend950 走 PipeBarrier，Mutex 双缓冲是 arch35 的优化路径。

## 7. 下一步学习建议

- **u3-l3（DAG 与 Bind）**：本讲的 `OpCopyIn`/`OpCopyOut`/`OpAlloc`/`OpFree` 是「谁」插进表达式里的？答案在 DAG 构建与 `AllocInserter`/`FreeInserter`，读完你会理解 Tile 层收到的表达式为何已经天然带有正确的搬运与缓冲节点。
- **u3-l4（表达式线性化与图优化 Pass）**：理解 `Simplify`（内联单用 LocalVar）、`expr_cast_eliminate`（冗余 Cast 消除）如何改变 Tile 层要执行的节点序列。
- **u3-l5（Buffer 管理与双缓冲）**：本讲的 ping/pong id 只讲了 Param 侧的简单情形，复杂多变量场景下缓冲如何按 `MemLevel`（MTE2/MTE3/Temp）分配与复用，要看 `GenerateBufferIdOrder` 的完整图优化逻辑。
- **u3-l6（Reduce 模块）**：含 `ReduceSum`/`Broadcast` 的表达式在 Tile 层会有额外的 `PIPE` 同步与缓冲拷贝，是本讲机制在「改变形状」算子上的延伸。
