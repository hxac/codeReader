# Block 层：单核 Tile 切分与流水

## 1. 本讲目标

上一讲（u2-l8）我们打开了 Kernel 层：Host 算好切分方案，每个核用 `GetBlockIdx()` 经 `CalCurCoreEleCnt`/`CalGMOffset` 领到自己的工作量。本讲沿着 `blockOp.Run(configBlock, convertArgs)` 再往下走一层，进入 **Block 层**——一个核内部如何把自己的那段任务，再切成一个个 **Tile（瓦片）** 块，循环搬运、计算、写回。

学完本讲你应该能够：

1. 说清 `DefaultBlockPolicy`/`DefaultBlockConfig` 的职责，理解 `wholeLoop`、`tileCnt`、`BASIC_BLOCK`、`totalElemCnt` 这四个量的含义与推导。
2. 描述 `DefaultBlockSchedule` 的 `Run → Process` 主循环如何把单核任务切成「整 Tile + 尾 Tile」，并逐个调用 `Tile::Evaluate`。
3. 画出 UB（Unified Buffer，统一缓冲）按 **IN / OUT / CALC 三段** 划分的内存布局，并理解 `UB_TILE_SIZE` 与 ping-pong 双缓冲的关系。
4. 解释 `BlockTensor::CopyIn/CopyOut` 如何完成 GM↔UB 的数据搬运，以及它和 Tile 层求值器的配合。

---

## 2. 前置知识

- **UB（Unified Buffer）**：AI Core 内部紧邻 Vector 计算单元的高速片上内存，容量很小（DAV_3510 上是 240 KB）。数据必须先从 GM 搬到 UB 才能参与 Vector 计算。
- **GM（Global Memory）**：Device 显存（HBM），容量大、带宽相对低，所有核共享。数据流是 GM → UB → 计算 → UB → GM。
- **Tile（瓦片）**：把一大块数据切成固定大小的小块，每次处理一块。切块是为了让每块都能放进 UB，也为了配合流水线。
- **BASIC_BLOCK**：ATVOSS 里「一个完整 Tile 处理的元素数」，由用户在 `Config` 里指定的 `TileShape` 编译期累乘得到。
- **ping-pong（乒乓）双缓冲**：准备两套缓冲区（ping / pong），让「第 i 次的搬运」和「第 i-1 次的计算」重叠执行，从而隐藏 GM 搬运延迟。
- **PIPE（流水线）**：Ascend C 的硬件流水线，本讲会碰到三类：`MTE2`（GM→UB 搬运）、`V`（Vector 计算）、`MTE3`（UB→GM 搬运）。

> 承接 u2-l8：Kernel 层把 `configBlock.totalElemCnt = CalCurCoreEleCnt(...)`（当前核要处理的总元素数）填好后，调用 `blockOp.Run(configBlock, convertArgs)` 进入本讲的 Block 层。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `include/elewise/block/builder.h` | Block 层入口：`DefaultBlockConfig`（切分配置）、`DefaultBlockPolicy`（含 TileShape 与 MemMngPolicy）、`BlockBuilder`（对外 `Run`）。 |
| `include/elewise/block/schedule.h` | 本讲主角：`BaseBlockSchedule`/`DefaultBlockSchedule`，定义编译期 UB 内存划分、`MakeScheduleConfig`、以及 `Run → Process` 主循环。 |
| `include/elewise/block/block_info_tile.h` | `BlockTensor`：把「一个 GM 指针 + 一个 UB LocalTensor」绑在一起，提供 `CopyIn`/`CopyOut`。 |
| `include/common/type_def.h` | `ContextData`：单核单 Tile 的上下文包裹，把 gmOffset/elementNum/pingPong 下传给求值器。 |
| `include/common/arch.h` | `DAV_3510` 硬件常量：`CORE_NUM = 56`、`UB_SIZE = 240KB`。 |
| `include/utils/buf_pool/block_buf_pool.h` | `BlockBufferEx`：把整块 UB 切成 MAX_BUFFER_COUNT 个等大槽位的缓冲池。 |
| `include/elewise/tile/tensor_evaluator.h` | Tile 层的 `CopyIn`/`CopyOut` 求值器，真正调用 `DataCopyPad` 并做 PIPE 同步。 |
| `include/elewise/kernel/schedule.h` | 上游：Kernel 层如何算出 `totalElemCnt` 并下传给 Block 层。 |

---

## 4. 核心概念与源码讲解

### 4.1 BlockBuilder 与 DefaultBlockPolicy/Config

#### 4.1.1 概念说明

Block 层对外的门面是 `BlockBuilder`，但用户在 `Config` 里几乎不和它直接打交道——用户写的是 `Compute`，框架把 `BlockBuilder<Compute>` 嵌进 `KernelBuilder`，再嵌进 `DeviceAdapter`（见 u1-l4）。`BlockBuilder` 的真正职责是**携带两样东西**：

1. **策略（Policy）**：`DefaultBlockPolicy<TileShape>`，告诉 Block 层「Tile 切多大」「内存管理用 AUTO 还是 MANUAL」。
2. **配置（Config）**：`DefaultBlockConfig`，一个**运行期**结构体，存放切分结果（几个整 Tile、尾 Tile 多大等）。Config 是 Device 侧每核实例化时会读写的；Policy 是编译期常量。

#### 4.1.2 核心流程

```
用户 Config 里:
  using TileShape = Atvoss::Shape<...>;                         // 编译期形状
  static constexpr DefaultBlockPolicy<TileShape> blockPolicy{TileShape{}};
  using BlockOp = BlockBuilder<AbsCompute, ArchTag, blockPolicy, DefaultBlockConfig>;

Kernel 层调用:
  blockOp.Run(configBlock, convertArgs)
        │
        └── BlockBuilder::Run 只是转发 → schedule.Run(cfg, argTuple)
```

`DefaultBlockConfig` 的四个字段就是后续切分和循环的全部「状态」：

| 字段 | 含义 |
|------|------|
| `wholeLoop` | 完整 Tile 的循环次数（不含尾 Tile） |
| `tileCnt` | 尾 Tile 要处理的元素数；当前 Tile 是完整块时为 0 |
| `basicNum` | 单个完整 Tile 处理的元素数（即 BASIC_BLOCK） |
| `totalElemCnt` | 当前核要处理的总元素数（由 Kernel 层填入） |

#### 4.1.3 源码精读

`DefaultBlockConfig` 字段定义与英文注释（wholeLoop/tileCnt/basicNum/totalElemCnt）见 [include/elewise/block/builder.h:19-25](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/builder.h#L19-L25) —— 这段定义了切分结果的所有「运行期状态」。

`DefaultBlockPolicy<Shape>` 用一个模板形参接收 `TileShape`，并携带 `memPolicy`（默认 AUTO）：

```cpp
template <typename Shape>
struct DefaultBlockPolicy {
    using TileShape = Shape;
    Shape tileShape{};
    Atvoss::MemMngPolicy memPolicy = Atvoss::MemMngPolicy::AUTO;
};
```

对应 [include/elewise/block/builder.h:27-32](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/builder.h#L27-L32)。`memPolicy` 决定 DAG 走 `FullAutoDag`（AUTO，框架全自动插 Alloc/Free）还是 `ManualDag`（MANUAL），细节在 u3-l3/u3-l8。

库还提供了一个默认 TileShape——一行 4096 元素的二维形状，供不想手写形状的场景兜底（[include/elewise/block/builder.h:34-36](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/builder.h#L34-L36)）。

`BlockBuilder` 本身极其薄：它把模板参数（`Compute`、`ArchTag`、`Policy`、`ScheduleCfg`、`Schedule`）打包，对外只暴露 `Run`，而 `Run` 仅仅是 `new` 一个 `ScheduleClz` 再转发（[include/elewise/block/builder.h:42-58](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/builder.h#L42-L58)）。注意 `Schedule` 是一个**模板模板参数**——这是 ATVOSS 留给用户的扩展点：你可以把 `DefaultBlockSchedule` 换成自定义调度器而不动 `BlockBuilder` 的其余部分。

> 关键结论：`BlockBuilder` 是「策略 + 配置」的载体，真正的切分与循环逻辑全在 `DefaultBlockSchedule`（下一节）。

#### 4.1.4 代码实践

**实践目标**：建立「Policy（编译期） vs Config（运行期）」的直觉。

**操作步骤**：

1. 打开 `examples/abs/abs.cpp`，定位 `AbsConfig`。
2. 找到 `using TileShape = Atvoss::Shape<TILE_SIZE>;` 与 `blockPolicy{TileShape{}}`（[examples/abs/abs.cpp:16](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L16)、[examples/abs/abs.cpp:34](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L34)）。
3. 注意 `BlockOp` 第 4 个模板实参 `DefaultBlockConfig` 没有被显式带进 `KernelOp`/`DeviceOp`，但它在编译期被内嵌进了调度类。

**需要观察的现象**：`TILE_SIZE = 32`，所以 abs 的 TileShape 是一维 `Shape<32>`，含义是「每次 Tile 处理 32 个元素」。

**预期结果**：你能指出 abs 样例里**没有**出现 `wholeLoop`、`tileCnt` 这些字样——因为它们是运行期值，藏在 `DefaultBlockConfig` 对象里，由 `DefaultBlockSchedule::Run` 在 Device 侧现场计算，Host 代码看不见。

> 待本地验证：若把 `TILE_SIZE` 改成 `4096`，重新编译，观察构建是否仍通过（这是后续综合实践要分析的 UB 占用问题）。

#### 4.1.5 小练习与答案

**练习 1**：`DefaultBlockPolicy` 里的 `memPolicy` 默认值是什么？它影响什么？

**参考答案**：默认是 `Atvoss::MemMngPolicy::AUTO`。它决定 Block 层在编译期预处理表达式时选用 `FullAutoDag`（框架自动插入 Alloc/Free 与 CopyIn/CopyOut）还是 `ManualDag`（用户自行管理）。

**练习 2**：为什么 `BlockBuilder` 的 `Schedule` 形参要设计成「模板模板参数」（`template <...> class Schedule`）而不是普通类型参数？

**参考答案**：因为 `Schedule` 需要接收 `Compute`、`Policy`、`ScheduleCfg`、`ArchTag` 这一组固定的模板实参后才能被实例化（见 `using ScheduleClz = Schedule<Compute, Policy, ScheduleCfg, ArchTagcfg>;`）。用模板模板参数，框架才能在 `BlockBuilder` 内部把这组实参「喂」给用户自定义的 Schedule 类，从而允许用户替换整个调度策略。

---

### 4.2 Tile 切分：wholeLoop、tileCnt 与 BASIC_BLOCK

#### 4.2.1 概念说明

一个核拿到 `totalElemCnt`（比如 10000 个元素）后，不可能一次性塞进 UB（240KB / 4 字节 ≈ 6 万个 float，看似够，但还要给输出、临时变量、ping-pong 各留份），所以要切成多个 Tile。切法是经典的「整除取整 + 取余」：

- **BASIC_BLOCK**：一个完整 Tile 的元素数，由 `TileShape` 编译期累乘决定（abs 的 `Shape<32>` → BASIC_BLOCK = 32）。
- **wholeLoop**：能切出多少个「完整 Tile」。
- **tileCnt**：切完整 Tile 后剩下的「尾数」，即最后一个不完整 Tile 的元素数；能整除时为 0。

#### 4.2.2 核心流程

切分公式（整数除法与取余）：

\[
\text{wholeLoop} = \left\lfloor \frac{\text{totalElemCnt}}{\text{BASIC\_BLOCK}} \right\rfloor
\]

\[
\text{tileCnt} = \text{totalElemCnt} \bmod \text{BASIC\_BLOCK}
\]

举例：`totalElemCnt = 10000`，`BASIC_BLOCK = 4096`：

\[
\text{wholeLoop} = \lfloor 10000 / 4096 \rfloor = 2,\quad \text{tileCnt} = 10000 - 2 \times 4096 = 1808
\]

即 2 个完整 Tile（各 4096）+ 1 个尾 Tile（1808），合计 \(2 \times 4096 + 1808 = 10000\)。

BASIC_BLOCK 本身是怎么来的？由 `TileShape` 在编译期累乘——`GetTotalElement<N>` 从第 N 维开始，把每一维的常量乘起来（[include/operators/tile_shape.h:72-102](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/tile_shape.h#L72-L102)）：

- `Shape<32>` → 32
- `Shape<1, 4096>` → \(1 \times 4096 = 4096\)

#### 4.2.3 源码精读

`BaseBlockSchedule` 把 BASIC_BLOCK 定为 `TileShape` 的元素总数（[include/elewise/block/schedule.h:88](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L88)）：

```cpp
static constexpr uint32_t BASIC_BLOCK = Atvoss::Tile::GetTotalElement<0, BlockPolicy>(1, 1);
```

真正执行切分的是 `Run`——它在进入 `Process` 前现场算出 wholeLoop/tileCnt（[include/elewise/block/schedule.h:221-227](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L221-L227)）：

```cpp
__aicore__ inline void Run(ScheduleCfg& cfg, ArgTup& argTuple)
{
    cfg.wholeLoop = cfg.totalElemCnt / BASIC_BLOCK;
    cfg.tileCnt   = cfg.totalElemCnt % BASIC_BLOCK;
    Process(cfg, argTuple);
}
```

> 注意：`totalElemCnt` 不是 Block 层自己算的，而是 Kernel 层 `Run` 里通过 `configBlock.totalElemCnt = CalCurCoreEleCnt(cfg.kernelParam);` 填好的（[include/elewise/kernel/schedule.h:121-122](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/schedule.h#L121-L122)，`CalCurCoreEleCnt` 见 [include/elewise/kernel/schedule.h:140-151](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/schedule.h#L140-L151)）。所以「单核总元素数」是 u2-l8 的核间切分结果，Block 层只是继续在核内切 Tile。

#### 4.2.4 代码实践

**实践目标**：手工模拟一次切分，验证 wholeLoop/tileCnt 的推导。

**操作步骤**（源码阅读型，可在纸上完成）：

1. 给定单核 `totalElemCnt = 10000`，`TileShape = Atvoss::Shape<1, 4096>`。
2. 先求 BASIC_BLOCK：`Shape<1, 4096>` 累乘 → 4096。
3. 套公式：`wholeLoop = 10000 / 4096 = 2`，`tileCnt = 10000 % 4096 = 1808`。

**需要观察的现象**：总元素被分解成 3 个 Tile：第 0、1 个是完整块（各 4096），第 2 个是尾块（1808）。

**预期结果**：核内将循环 3 次（wholeLoop=2 次 + 1 次尾 Tile），与 4.3 节 `Process` 的循环结构一一对应。

> 待本地验证：若把 totalElemCnt 改成 8192（恰好整除），tileCnt 应为 0，此时 `Process` 里尾 Tile 分支不会进入。

#### 4.2.5 小练习与答案

**练习 1**：`TileShape = Shape<1, 4096>`、`totalElemCnt = 8192`，wholeLoop 和 tileCnt 各是多少？循环几次？

**参考答案**：BASIC_BLOCK = 4096；wholeLoop = 8192 / 4096 = 2；tileCnt = 0。循环 2 次（全是完整 Tile，不进入尾 Tile 分支）。

**练习 2**：如果用户的 `TileShape` 比 UB 能容纳的还大，会发生什么？

**参考答案**：编译期 `TileCheckAssert` 会触发 `static_assert(USER_TILE_SIZE <= UB_TILE_SIZE)`，编译失败（见 4.4 节）。ATVOSS 用编译期断言把「TileShape 过大」拦截在编译阶段，而不是运行时崩溃。

---

### 4.3 Process 循环与 Tile::Evaluate

#### 4.3.1 概念说明

切好 Tile 之后，`Process` 负责把每个 Tile 依次送进求值器执行。每个 Tile 的执行内容都是**同一份表达式**（线性化后的 `ExprTile`），差别只在于三件「位置相关」的事：

- 从 GM 的哪个偏移搬数据进来？
- 这一 Tile 处理多少个元素？
- 这一 Tile 用 ping 缓冲还是 pong 缓冲？

这三件事被打包进 `ContextData`，下传给 `Tile::Evaluate`。

#### 4.3.2 核心流程

```
Process(cfg, argTuple):
  1. PrepareParams<LocalVars>      // 构造临时变量（LocalVar）的 BlockTensor
  2. PrepareBlockParams<Params>    // 把 GM 指针包成 BlockTensor（含 in/out）
  3. for i in [0, wholeLoop):      // 完整 Tile
        ContextData{ ..., gmOffset=i*BASIC_BLOCK, elementNum=BASIC_BLOCK, pingPong=i&1 }
        Tile::Evaluate<ExprTile>(context)
  4. if tileCnt > 0:               // 尾 Tile
        ContextData{ ..., gmOffset=wholeLoop*BASIC_BLOCK, elementNum=tileCnt, pingPong=wholeLoop&1 }
        Tile::Evaluate<ExprTile>(context)
```

> 注意第 4 步的循环变量 `i` 复用了上面 for 循环结束时的值（恰好等于 wholeLoop），所以尾 Tile 的 gmOffset 是 `wholeLoop * BASIC_BLOCK`，正好接在最后一个完整 Tile 之后，无重叠无遗漏。

`Tile::Evaluate` 本身只有一行——把表达式交给 `Evaluator`（[include/elewise/tile/tile_evaluate.h:33-37](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tile_evaluate.h#L33-L37)）。求值器如何把表达式翻译成 Ascend C 指令，是 u3-l1/u3-l2 的主题，本讲把它当黑盒：给它 context，它就会按表达式依次做 CopyIn → 计算 → CopyOut。

#### 4.3.3 源码精读

`Process` 的完整实现（[include/elewise/block/schedule.h:230-248](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L230-L248)）：

```cpp
template <typename ArgTup>
__aicore__ inline void Process(ScheduleCfg& cfg, ArgTup& argTuple)
{
    auto blockLocalVars   = PrepareParams<LocalVars>();
    auto blockTensorsTile = PrepareBlockParams<Params>(argTuple);
    using BufferMaps = typename EleWiseDag::BufMap;
    using ContextDataT =
        ContextData<decltype(blockTensorsTile), decltype(blockLocalVars), decltype(bufPools_), BufferMaps>;

    uint32_t i = 0;
    for (; i < cfg.wholeLoop; i++) {
        ContextDataT context{blockTensorsTile, blockLocalVars, bufPools_,
                             i * BASIC_BLOCK, BASIC_BLOCK, i & 1};
        Atvoss::Ele::Tile::Evaluate<ExprTile>(context);
    }
    if (cfg.tileCnt > 0) {
        ContextDataT context{blockTensorsTile, blockLocalVars, bufPools_,
                             i * BASIC_BLOCK, cfg.tileCnt, i & 1};
        Atvoss::Ele::Tile::Evaluate<ExprTile>(context);
    }
}
```

`ContextData` 的结构（[include/common/type_def.h:15-25](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/common/type_def.h#L15-L25)）：构造函数后六个字段对应 `Process` 里那六个实参——前三个是「数据与缓冲」（张量、临时变量、缓冲池引用），后三个是「位置信息」：

| 实参（Process 传入） | ContextData 字段 | 作用 |
|---|---|---|
| `blockTensorsTile` | `argsTensors` | 所有 in/out 的 BlockTensor 元组（含 GM 指针 + UB 槽） |
| `blockLocalVars` | `tmpTensors` | 所有 LocalVar 的 BlockTensor 元组 |
| `bufPools_` | `bufPools` | UB 缓冲池引用，AllocTensor 时用 |
| `i * BASIC_BLOCK` | `gmOffset` | 当前 Tile 在 GM 中的起始偏移（元素数） |
| `BASIC_BLOCK` / `cfg.tileCnt` | `elementNum` | 当前 Tile 处理的元素数 |
| `i & 1` | `pingPong` | 双缓冲交替标志（0 或 1） |

`PrepareBlockParams` 把 GM 指针包成 BlockTensor 的关键细节在 `ConstructBlockParam`：它用 `ParamType::inplaceNumber - 1` 作为下标去 kernel 参数元组里取 GM 指针（[include/elewise/block/schedule.h:275-287](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L275-L287)）。这里的 `inplaceNumber` 就是 u2-l2 讲过的 Param 序号，被复用来对齐运行时入参顺序——IN_OUT 参数会复用同一块 GM（详见注释「adapter IN_OUT params optimization in AUTO Dag」）。

#### 4.3.4 代码实践

**实践目标**：逐 Tile 推导 `ContextData` 三个位置字段的取值。

**操作步骤**（承接 4.2 的参数：totalElemCnt=10000、TileShape=Shape<1,4096>，故 wholeLoop=2、tileCnt=1808）：

填表：

| Tile (i) | 类型 | gmOffset = i×4096 | elementNum | pingPong = i&1 |
|---|---|---|---|---|
| 0 | 完整 | 0 | 4096 | 0 |
| 1 | 完整 | 4096 | 4096 | 1 |
| 2 | 尾 | 8192 | 1808 | 0 |

**需要观察的现象**：

- **gmOffset**：随 Tile 线性递增，告诉 `CopyIn/CopyOut` 从 GM 的哪个元素开始搬。注意它是在「当前核的 GM 段内」的偏移——Kernel 层 `CalGMOffset` 已经把每个核的起点加进了 GM 指针（u2-l8），所以 Block 层的 gmOffset 是段内相对偏移。
- **elementNum**：完整 Tile 是 BASIC_BLOCK，尾 Tile 是 tileCnt；它就是 `DataCopyPad` 的搬运长度。
- **pingPong**：按 `i&1` 在 0/1 间交替，让相邻 Tile 轮流使用两套缓冲，从而把「第 i 次搬运」与「第 i-1 次计算」重叠（见 4.4、u3-l5）。

**预期结果**：三次 `Evaluate` 调用，每次处理的 GM 区间依次是 [0,4096)、[4096,8192)、[8192,10000)，正好覆盖 10000 个元素，无重叠无遗漏；缓冲在 ping(0)→pong(1)→ping(0) 间交替。

> 待本地验证：可在求值器的 `CopyIn` 处临时加一条 `AscendC::printf` 打印 `context.gmOffset / context.elementNum / context.pingPong`（参考 tensor_evaluator.h 里被注释掉的 printf 行），仿真时观察这三值的演变。

#### 4.3.5 小练习与答案

**练习 1**：尾 Tile 的循环变量 `i` 为何等于 wholeLoop？如果删掉 `if (cfg.tileCnt > 0)` 里的尾 Tile 分支，会漏掉哪些元素？

**参考答案**：因为 for 循环结束时 `i` 自增到 wholeLoop，第 4 步复用了这个值。删掉尾 Tile 分支会漏掉 `tileCnt` 个尾部元素——例如 totalElemCnt=10000、BASIC_BLOCK=4096 时，会漏掉最后的 1808 个。

**练习 2**：`PrepareBlockParams` 用 `inplaceNumber - 1` 作下标取 GM 指针。为什么是减 1？

**参考答案**：`inplaceNumber` 是 1-based 的 Param 序号（见 u2-l2），而 C++ tuple 的 `std::get` 是 0-based，所以减 1 转成下标。

---

### 4.4 UB 内存三段划分与 UB_TILE_SIZE

#### 4.4.1 概念说明

UB 是稀缺资源（240KB），一个核内的所有 in/out/临时变量都要挤在里面，还要预留 ping-pong 双缓冲。ATVOSS 在编译期就把 UB 划分成**三个概念区段**：

- **IN 区**：存放输入参数的 UB 缓冲，每个输入参数占 **2 个槽**（ping + pong）。
- **OUT 区**：存放输出参数的 UB 缓冲，每个输出参数占 **2 个槽**（ping + pong）。
- **CALC 区**：存放临时变量（LocalVar）的缓冲，每个 LocalVar 占 **1 个槽**（无双缓冲）。

每个槽的大小叫 `UB_TILE_SIZE`，由「UB 总量 ÷ 总槽数」得到，并按 1KB 向下取整。

#### 4.4.2 核心流程

总槽数：

\[
\text{MAX\_BUFFER\_COUNT} = \underbrace{2 \cdot |\text{InParams}|}_{\text{IN 区}} + \underbrace{2 \cdot |\text{OutParams}|}_{\text{OUT 区}} + \underbrace{|\text{LocalVars}|}_{\text{CALC 区}}
\]

每个槽的大小（先除以 1024 再乘回 1024，等价于按 1KB 向下取整）：

\[
\text{UB\_TILE\_SIZE} = \left\lfloor \frac{\text{UB\_SIZE}}{\text{MAX\_BUFFER\_COUNT} \times 1024} \right\rfloor \times 1024
\]

三个区段的起始地址：

\[
\text{UB\_ADDR\_IN} = 0,\quad
\text{UB\_ADDR\_OUT} = \text{UB\_TILE\_SIZE} \cdot |\text{IN\_PARAMS\_COUNT}|,\quad
\text{UB\_ADDR\_CALC} = \text{UB\_ADDR\_OUT} + \text{UB\_TILE\_SIZE} \cdot |\text{OUT\_PARAMS\_COUNT}|
\]

以 abs 为例（1 个输入、1 个输出、0 个 LocalVar）：IN_PARAMS_COUNT=2、OUT_PARAMS_COUNT=2、LOCAL_VAR_COUNT=0、MAX_BUFFER_COUNT=4。UB_TILE_SIZE = ⌊240KB / 4 / 1KB⌋ × 1KB = 60KB。

为什么 IN/OUT 要双缓冲（×2）而 LocalVar 不用？因为 in/out 的搬运（MTE2/MTE3）耗时长，需要用 ping-pong 隐藏延迟；临时变量是纯 UB→UB 的中间结果，生命周期短，单缓冲即可（缓冲复用的细节见 u3-l5）。

#### 4.4.3 源码精读

缓冲计数与三段地址的定义（[include/elewise/block/schedule.h:134-142](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L134-L142)）：

```cpp
static constexpr uint32_t IN_PARAMS_COUNT   = Size_v<InParams> * 2;   // ping + pong
static constexpr uint32_t OUT_PARAMS_COUNT = Size_v<OutParams> * 2;   // ping + pong
static constexpr uint32_t LOCAL_VAR_COUNT  = Size_v<LocalVars>;

static constexpr uint32_t MAX_BUFFER_COUNT = IN_PARAMS_COUNT + OUT_PARAMS_COUNT + LOCAL_VAR_COUNT;
static constexpr uint32_t UB_TILE_SIZE = ArchTag::UB_SIZE / MAX_BUFFER_COUNT / 1024 * 1024;
static constexpr uint64_t UB_ADDR_IN   = 0;
static constexpr uint64_t UB_ADDR_OUT  = UB_TILE_SIZE * IN_PARAMS_COUNT;
static constexpr uint64_t UB_ADDR_CALC = UB_ADDR_OUT + UB_TILE_SIZE * OUT_PARAMS_COUNT;
```

硬件常量来自 [include/common/arch.h:22-25](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/common/arch.h#L22-L25)：`CORE_NUM = 56`、`UB_SIZE = 240 * 1024`。

UB 的实际分配由 `BlockBufferEx` 这个缓冲池完成（[include/utils/buf_pool/block_buf_pool.h:15-43](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/buf_pool/block_buf_pool.h#L15-L43)）。它在 `Init` 里一次性向 `TPipe` 申请 `UB_TILE_SIZE * MAX_BUFFER_COUNT` 的一整块 TBuf，`AllocTensor(LocalTensor&, bufferId)` 再按 `tensorPool_[bufferId * BLOCK_LEN]` 把第 bufferId 个槽切给某个张量：

```cpp
__aicore__ inline void Init() {
    GetTPipePtr()->InitBuffer(tbuf_, TILE_SIZE * TILE_NUM);
    tensorPool_ = tbuf_.Get<uint8_t>();
}
template <typename T>
__aicore__ inline void AllocTensor(AscendC::LocalTensor<T>& inTensor, uint32_t bufferId) {
    inTensor = tensorPool_[bufferId * BLOCK_LEN].template ReinterpretCast<T>();
}
```

也就是说，`UB_ADDR_IN/OUT/CALC` 描述的是**概念上的三段布局**，而 `BlockBufferEx` 用连续的 bufferId（0, 1, 2, …）实现了同样的物理划分——bufferId 的顺序与 IN→OUT→CALC 一致。`BlockBuilder` 构造函数里会做一道安全检查：用户 TileShape 折算的 UB 占用不得超过 UB_TILE_SIZE（[include/elewise/block/schedule.h:74-77](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L74-L77)、[include/elewise/block/schedule.h:207-214](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L207-L214)）。

#### 4.4.4 代码实践

**实践目标**：手算 abs 的 UB 划分，理解 TileShape 对 UB 占用的回压。

**操作步骤**：

1. 对 abs（1 in / 1 out / 0 localVar），算 MAX_BUFFER_COUNT = 2 + 2 + 0 = 4。
2. 算 UB_TILE_SIZE = 240KB / 4 = 60KB。
3. 对比 BASIC_BLOCK=32（float，即 32×4 = 128 字节），远小于 60KB，所以 TileCheckAssert 通过。

**需要观察的现象**：UB_TILE_SIZE 反过来约束了「单个 Tile 最多能放多少元素」。即便用户把 TileShape 设得很大，只要 `(BASIC_BLOCK × MaxSize)` 向上对齐到 32 字节后超过 UB_TILE_SIZE，编译期就会 `static_assert` 失败。

**预期结果**：abs 当前配置下 UB 占用极低；若把 TileShape 改到接近 60KB（如 `Shape<1, 14000>` 量级），应触发编译期断言失败。

> 待本地验证：把 abs 的 `TILE_SIZE` 逐步调大，记录哪个值开始编译失败，验证 ≈ UB_TILE_SIZE / sizeof(float) 的上界。

#### 4.4.5 小练习与答案

**练习 1**：一个算子有 2 个输入、1 个输出、1 个 LocalVar，MAX_BUFFER_COUNT 是多少？UB_TILE_SIZE（240KB）是多少？

**参考答案**：IN_PARAMS_COUNT=4、OUT_PARAMS_COUNT=2、LOCAL_VAR_COUNT=1，MAX_BUFFER_COUNT=7。UB_TILE_SIZE = ⌊240 / 7⌋ KB = 34KB（⌊245760/7/1024⌋×1024 = 34×1024 = 34816 字节）。

**练习 2**：为什么 LocalVar 不做 ping-pong 双缓冲，而 in/out 要做？

**参考答案**：in/out 涉及 GM↔UB 的长延迟搬运（MTE2/MTE3），需要双缓冲把搬运与计算重叠以隐藏延迟；LocalVar 是 UB 内部的中间结果，不跨 GM，生命周期短且可被复用（见 u3-l5 的缓冲复用策略），单缓冲即可，避免浪费稀缺的 UB。

---

### 4.5 BlockTensor：CopyIn/CopyOut 实现 GM↔UB 搬运

#### 4.5.1 概念说明

`BlockTensor` 是 Block 层对「一个张量」的封装：左手抓 **GM 指针**（数据来源/去向），右手抓 **UB LocalTensor**（计算用的工作区）。它只暴露两个动作：

- **CopyIn(gmOffset, len)**：从 GM 的 `gmOffset` 处搬 `len` 个元素到 UB。
- **CopyOut(gmOffset, len)**：把 UB 里的 `len` 个结果搬回 GM 的 `gmOffset` 处。

这两个动作的真正实现（`DataCopyPad`）和 PIPE 同步在 Tile 层求值器里。

#### 4.5.2 核心流程

```
BlockTensor 内部:
  ubTensor_  : AscendC::LocalTensor<T>   // UB 工作区
  gmAddr_    : __gm__ uint8_t*           // GM 指针

CopyIn(gmOffset, len):
  GlobalTensor.SetGlobalBuffer(gmAddr_)
  Tile::CopyIn(ubTensor_, gmTensor[gmOffset], len)   // → DataCopyPad

CopyOut(gmOffset, len):
  GlobalTensor.SetGlobalBuffer(gmAddr_)
  Tile::CopyOut(gmTensor[gmOffset], ubTensor_, len)  // → DataCopyPad
```

求值器侧（tensor_evaluator.h）在调用 `CopyIn/CopyOut` 前后插入 **PIPE 同步**与 **Mutex**，保证双缓冲下不会读写冲突：

```
CopyIn 求值:  Lock<MTE2>(bufferId) → obj.CopyIn(gmOffset, elementNum) → Unlock<MTE2> → Lock<V>
CopyOut 求值: Unlock<V> → Lock<MTE3> → obj.CopyOut(gmOffset, elementNum) → Unlock<MTE3>
```

`bufferId` 由 `pingPong` 决定（ping 用 pingBufId，pong 用 pongBufId），这正是双缓冲不冲突的关键。

#### 4.5.3 源码精读

`BlockTensor` 的定义（[include/elewise/block/block_info_tile.h:18-55](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/block_info_tile.h#L18-L55)）。`CopyIn` 把 GM 指针包成 `GlobalTensor`，用 `gmTensor[curGmOffset]` 取偏移后调用 `Atvoss::Tile::CopyIn`（[include/elewise/block/block_info_tile.h:37-42](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/block_info_tile.h#L37-L42)）：

```cpp
__aicore__ inline void CopyIn(uint64_t curGmOffset, uint64_t copyLen)
{
    AscendC::GlobalTensor<T> gmTensor;
    gmTensor.SetGlobalBuffer(reinterpret_cast<__gm__ T*>(gmAddr_));
    Atvoss::Tile::CopyIn(ubTensor_, gmTensor[curGmOffset], copyLen);
}
```

`CopyOut` 对称（[include/elewise/block/block_info_tile.h:44-49](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/block_info_tile.h#L44-L49)）。

底层 `Atvoss::Tile::CopyIn/CopyOut` 封装了 `DataCopyPad`（[include/elewise/tile/tensor_evaluator.h:38-57](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L38-L57)）——`copyCnt * sizeof(T)` 把元素数换算成字节数，`DataCopyPad` 还会自动补齐（pad）到硬件对齐宽度，这正是「尾 Tile 元素数不是 32 倍数也能正确搬运」的原因。

求值器侧的同步逻辑：`Evaluator<OpCopyIn<T>>` 在调用 `obj.CopyIn(context.gmOffset, context.elementNum)` 之前 `Lock<PIPE_MTE2>`，之后 `Unlock<PIPE_MTE2>` 再 `Lock<PIPE_V>`（[include/elewise/tile/tensor_evaluator.h:60-85](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L60-L85)）；`Evaluator<OpCopyOut<T>>` 则 `Unlock<PIPE_V>` → `Lock<PIPE_MTE3>` → `CopyOut` → `Unlock<PIPE_MTE3>`（[include/elewise/tile/tensor_evaluator.h:88-113](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/tile/tensor_evaluator.h#L88-L113)）。这条 MTE2 → V → MTE3 的锁链，既保证数据依赖，又配合 ping-pong 让相邻 Tile 的搬运与计算重叠。

#### 4.5.4 代码实践

**实践目标**：理解「BlockTensor 只持有指针、不持有内存」的轻量设计，以及它如何被求值器驱动。

**操作步骤**：

1. 阅读 `BlockTensor` 的私有成员（[include/elewise/block/block_info_tile.h:52-53](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/block_info_tile.h#L52-L53)）：只有 `ubTensor_` 和 `gmAddr_` 两个成员。UB 内存由 `bufPools_.AllocTensor` 在 `OpAlloc` 求值时才真正绑定（见 tensor_evaluator.h 的 `OpAlloc` 分支），`BlockTensor` 自身不申请内存。
2. 跟踪一次 abs 的 Tile 执行：求值器先遇 `OpAlloc<in>`（绑 UB 槽）→ `OpCopyIn<in>`（GM→UB，`elementNum` 个元素）→ `OpAbs` 计算 → `OpCopyOut<out>`（UB→GM）→ `OpFree`（解锁）。所有这些节点共享同一个 `context.gmOffset / elementNum / pingPong`。

**需要观察的现象**：`CopyIn` 与 `CopyOut` 用的 `context.gmOffset` 和 `context.elementNum` 是同一个值，所以一 Tile 的输入区与输出区在 GM 上是对齐的（对逐元素算子而言）。

**预期结果**：能说清「数据流：GM →(MTE2)→ UB →(V)→ UB →(MTE3)→ GM」，以及 ping-pong 如何让第 i Tile 的 MTE2 与第 i-1 Tile 的 V/MTE3 重叠。

> 待本地验证：在 `Evaluator<OpCopyIn>` 与 `Evaluator<OpCopyOut>` 里临时解开被注释的 `AscendC::printf`，仿真运行 abs，对比相邻 Tile 的 bufferId 如何在 ping/pong 间切换。

#### 4.5.5 小练习与答案

**练习 1**：`BlockTensor` 没有任何 `aclrtMalloc`/`InitBuffer`，那它的 UB 内存从哪来？

**参考答案**：UB 内存由 `BlockBufferEx`（bufPools_）统一向 `TPipe` 申请（`Init` 里的 `InitBuffer`）。求值器在处理 `OpAlloc` 节点时调用 `context.bufPools.AllocTensor(obj.GetUbTensor(), bufferId)`，把某个 UB 槽绑定进 `BlockTensor::ubTensor_`。`BlockTensor` 只是「指针/引用」的容器。

**练习 2**：尾 Tile 的 elementNum（如 1808）不是 32 的整数倍，`DataCopyPad` 为何不会出错？

**参考答案**：`Atvoss::Tile::CopyIn/CopyOut` 用 `DataCopyPadExtParams` 告诉硬件按实际字节数搬运并自动补齐（pad）尾部，因此非对齐长度也能正确处理——这是 ATVOSS 用 `DataCopyPad`（而非要求严格对齐的 `DataCopy`）做 GM↔UB 搬运的原因。

---

## 5. 综合实践

**任务**：把本讲的知识串起来，做一次「单核 Tile 切分」的完整纸面推演与一处源码观察。

**背景**：假设有一个逐元素算子 `out = Sqrt(in)`（单输入、单输出、无临时变量），跑在单核上，该核分到 `totalElemCnt = 10000` 个元素，`TileShape = Atvoss::Shape<1, 4096>`，数据类型 float，目标硬件 DAV_3510（UB=240KB）。

**步骤**：

1. **切分推演**：
   - 求 BASIC_BLOCK = ?
   - 求 wholeLoop、tileCnt。
   - 列出每个 Tile 的 `(gmOffset, elementNum, pingPong)` 三元组。
2. **UB 划分推演**：
   - 该算子的 InParams=1、OutParams=1、LocalVars=0，求 MAX_BUFFER_COUNT 与 UB_TILE_SIZE。
   - 画出 UB 三段（IN/OUT/CALC）的地址范围，标注每段有几个槽、是否 ping-pong。
3. **执行流串联**：
   - 写出第 0 个 Tile 的求值顺序（OpAlloc → OpCopyIn → OpSqrt → OpCopyOut → OpFree），并标注每步用到的 `context` 字段与 PIPE。
4. **源码观察**（可选，待本地验证）：
   - 参考 `tests/st/test_block_cast1.cpp` 的 `CastConfig`（[tests/st/test_block_cast1.cpp:42](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/test_block_cast1.cpp#L42)），注意它把 `MemMngPolicy` 显式设成 `MANUAL`；对比 abs 默认的 `AUTO`，思考二者在 Block 层 DAG 选择上的差异（对应 schedule.h 的 `DagSelector`）。
   - 若有仿真环境，在 `tensor_evaluator.h` 的 `OpCopyIn` 求值器里解开 printf，运行后核对打印的 `gmOffset/elementNum/pingPong` 是否与第 1 步一致。

**参考答案要点**：

- BASIC_BLOCK = 4096；wholeLoop = 2；tileCnt = 1808。
- 三 Tile：(0, 4096, 0)、(4096, 4096, 1)、(8192, 1808, 0)。
- MAX_BUFFER_COUNT = 4（IN 2 + OUT 2 + CALC 0）；UB_TILE_SIZE = 60KB；IN 区 [0,120KB)、OUT 区 [120KB,240KB)、CALC 区为空。
- 第 0 Tile：OpAlloc<in>（AllocTensor 槽 0/ping）→ OpCopyIn（MTE2，搬 GM[0..4096) → UB）→ OpSqrt（V）→ OpCopyOut（MTE3，UB → GM[0..4096)）→ OpFree（解锁）。

---

## 6. 本讲小结

- Block 层的入口是 `BlockBuilder`，但它只是「Policy（编译期 TileShape + MemMngPolicy）+ Config（运行期切分状态）」的载体，真正干活的是 `DefaultBlockSchedule`。
- 单核任务被切成 `wholeLoop` 个完整 Tile + 至多 1 个尾 Tile：`wholeLoop = totalElemCnt / BASIC_BLOCK`，`tileCnt = totalElemCnt % BASIC_BLOCK`；`totalElemCnt` 由上层 Kernel 的 `CalCurCoreEleCnt` 填入。
- `Process` 主循环对每个 Tile 构造一个 `ContextData`，把 `gmOffset = i*BASIC_BLOCK`、`elementNum = BASIC_BLOCK 或 tileCnt`、`pingPong = i&1` 三件位置信息下传给 `Tile::Evaluate`，由求值器执行 CopyIn → 计算 → CopyOut。
- UB 在编译期被划成 IN（每入参 ping+pong 2 槽）、OUT（每出参 ping+pong 2 槽）、CALC（每 LocalVar 1 槽）三段；每槽大小 `UB_TILE_SIZE = UB_SIZE / MAX_BUFFER_COUNT`，物理分配由 `BlockBufferEx` 完成。
- `BlockTensor` 是「GM 指针 + UB LocalTensor」的轻量封装，`CopyIn/CopyOut` 落到 `DataCopyPad`；求值器用 MTE2→V→MTE3 的 Mutex 锁链保证依赖并配合 ping-pong 双缓冲隐藏搬运延迟。
- 安全网是编译期 `TileCheckAssert`：用户 TileShape 折算的 UB 占用不得超过 UB_TILE_SIZE，否则编译失败。

---

## 7. 下一步学习建议

- **向下一层（Tile 层内部）**：本讲把 `Tile::Evaluate` 当黑盒。u3-l1（求值器系统）和 u3-l2（Tile 层 Assign 到 Ascend C API）会拆开它，讲 `Evaluator<Expr>` 如何递归把表达式翻译成 `DataCopyPad`/`Abs`/`Sqrt` 等指令。
- **缓冲与双缓冲细节**：本讲只讲了「IN/OUT 双缓冲、LocalVar 单缓冲」的宏观划分。u3-l5 会讲 `buffer.h` 里的 `ParamBufIdMap`、`GenerateBufferIdOrder` 如何为每个节点按 MemLevel 分配 ping/pong bufferId，以及缓冲复用策略。
- **DAG 与图优化**：`Process` 里的 `ExprTile` 是表达式经过「线性化 + Alloc/Free 插入」后的产物。u3-l3（DAG 构建）和 u3-l4（线性化与图优化 Pass）会讲 `PreProcessComputeExpr` 如何把用户的 `Compute()` 变成这条线性操作序列。
- **建议阅读的源码**：重读 `include/elewise/block/schedule.h` 的 `BaseBlockSchedule` 全文，对照本讲的「概念」逐行消化；再跳读 `include/elewise/tile/tensor_evaluator.h`，建立「Block 循环 → Tile 求值 → Ascend C 指令」的完整链路直觉。
