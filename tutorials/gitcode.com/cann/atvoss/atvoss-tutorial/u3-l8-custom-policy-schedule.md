# 策略与配置定制：自定义 Schedule 与 Policy

## 1. 本讲目标

前面 u2-l8、u2-l9 我们已经看清 ATVOSS 的两层默认调度：Kernel 层把总元素按 `UniformSegment` 均匀切到多核、Block 层把单核任务按 `TileShape` 切成 Tile 循环。但这些都是「开箱即用」的默认行为。本讲回答一个进阶问题：

> **当默认调度不满足需求时，ATVOSS 留了哪些可定制的旋钮？我该如何拧它们？**

学完本讲你应该能够：

1. 说清 `MemMngPolicy::AUTO` 与 `MemMngPolicy::MANUAL` 的本质区别——它们分别走 `FullAutoDag` 与 `ManualDag`，并对「冗余中间缓冲」采取完全不同的处理。
2. 理解 `BlockBuilder` / `KernelBuilder` 的 **Schedule 模板形参** 是替换调度策略的官方扩展点，并知道如何把一个自定义 Policy 真正「拧」进去（很多初学者会在这步踩坑）。
3. 通过自定义 `TileShape` 做性能调优：理解 TileShape 如何影响 UB 占用与 Tile 循环次数，以及 `TileCheckAssert` 在哪一步兜底。
4. 区分两套硬件信息来源——编译期常量 `arch.h::DAV_3510` 与运行期查询 `platform_info.h`——并理解它们各自适用的场景。

## 2. 前置知识

本讲默认你已掌握以下内容（来自 u2-l8、u2-l9，不再重复展开）：

- **五层架构**：Device > Kernel > Block > Tile > Basic（见 u1-l3）。
- **三级 Builder 嵌套**：`BlockBuilder<Compute>` → `KernelBuilder<BlockOp>` → `DeviceAdapter<KernelOp>`（见 u1-l4）。
- **Kernel 层调度量**：`MakeScheduleConfig` 算出 `blockNum / unitNumPerCore / moreUnitCoreNum / tailNum`，核数被 `CORE_NUM` 上限裁剪；每核用 `CalCurCoreEleCnt` 定量、`CalGMOffset` 定位。
- **Block 层切 Tile**：`wholeLoop = totalElemCnt / BASIC_BLOCK`、`tileCnt = totalElemCnt % BASIC_BLOCK`，UB 三段划分（IN/OUT 双缓冲、CALC 单缓冲），`pingPong = i & 1` 双缓冲。
- **DAG 与缓冲**（u3-l3、u3-l5）：表达式先线性化为 `OpAssign` 序列，再由 DAG 做依赖分析、插入 `OpAlloc/OpFree`，最后由求值器执行。

本讲要补的两个新概念：

- **MemMngPolicy（内存管理策略）**：一个枚举，决定 Block 层用哪种 DAG 构建器，从而决定「中间缓冲由谁分配、冗余的中间量是否会被消除」。它是连接「用户写的表达式」与「UB 缓冲分配」的关键开关。
- **Schedule 模板形参**：Builder 类的最后一个模板参数是一个「模板模板参数（template template parameter）」，默认指向 `DefaultBlockSchedule` / `DefaultKernelSchedule`，允许你替换成自己的调度实现。这是 ATVOSS「框架可扩展」理念的具体落点。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/utils/patterns.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/patterns.h) | 定义 `MemMngPolicy`、`MemLevel`、`Pattern`、`CastMode` 四个策略枚举。 |
| [include/common/arch.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/common/arch.h) | 编译期硬件常量 `DAV_3510`：`CORE_NUM=56`、`UB_SIZE=240KB`。 |
| [include/common/platform_info.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/common/platform_info.h) | 运行期硬件查询 `OpPlatformInfo`（vectorCoreNum/ubSize/cacheLineSize）。 |
| [include/elewise/block/builder.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/builder.h) | `BlockBuilder`，持有编译期 `DefaultBlockPolicy`（TileShape + MemMngPolicy），暴露 Schedule 模板形参。 |
| [include/elewise/block/schedule.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h) | `DagSelector`（按 MemMngPolicy 选 FullAutoDag/ManualDag）、`BaseBlockSchedule`、UB 缓冲计算、Tile 循环。 |
| [include/elewise/kernel/builder.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/builder.h) | `KernelBuilder`，持有 `DefaultKernelPolicy`（分段策略），暴露 Schedule 模板形参。 |
| [include/elewise/kernel/schedule.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/schedule.h) | `BaseKernelSchedule`：核数裁剪、每核工作量分配。 |
| [examples/abs/abs.cpp](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp) | 样例：演示如何把自定义 `blockPolicy` 正确拧入 `BlockBuilder`。 |
| tests/st/test_compute_*_with_manupolicy.cpp / *_with_autopolicy.cpp | 对比用例：同一表达式分别在 MANUAL/AUTO 下的行为。 |

---

## 4. 核心概念与源码讲解

### 4.1 MemMngPolicy：AUTO 与 MANUAL，以及它选哪条 DAG

#### 4.1.1 概念说明

`MemMngPolicy`（Memory Management Policy）回答的问题是：**用户在 `Compute()` 里写下的那些中间变量、中间表达式，到底由谁来分配 UB 缓冲、谁来决定它们的生命周期？**

ATVOSS 给出两种风格：

- **AUTO（默认）**：交给框架的 `FullAutoDag`。它会做完整的依赖分析：谁先用、谁最后用、谁根本没被用，然后据此自动插入 `OpAlloc/OpFree`，并**消除掉冗余/死掉的中间缓冲**。开发者只管写表达式，不用关心缓冲。
- **MANUAL**：交给 `ManualDag`。它更「老实」——基本按你写的顺序与变量分配缓冲，**不做激进的冗余消除**。适合你想对缓冲分配有更强掌控、或用于和 AUTO 做对照测试的场景。

一句话：AUTO 是「自动挡」，框架替你省缓冲；MANUAL 是「手动挡」，你写多少中间量它基本就分配多少。两者算出的**数值结果一致**，差别在 **UB 缓冲的数量与复用程度**。

#### 4.1.2 核心流程

`MemMngPolicy` 是 `DefaultBlockPolicy` 的一个字段，从 `Config` 一路传到 `BaseBlockSchedule`，在编译期由 `DagSelector` 翻译成具体的 DAG 类型：

```
DefaultBlockPolicy { TileShape, memPolicy }
        │  (作为 Policy 模板实参传入 BlockBuilder)
        ▼
BaseBlockSchedule 读取 Policy.memPolicy
        │
        ▼
PreProcessComputeExpr<memPolicy>(expr)
        │
        ▼
DagSelector ──memPolicy==AUTO──▶ FullAutoDag   (依赖分析 + 冗余消除)
            └──否则(MANUAL)────▶ ManualDag      (按声明分配)
```

关键结论：**MemMngPolicy 影响的是「缓冲怎么分配」，不是「算什么」。** 所以同一份表达式在 AUTO/MANUAL 下，最终输出的数值相同，但 UB 里实际占用的缓冲槽数可能不同。

#### 4.1.3 源码精读

枚举定义非常简洁，`MANUAL=0`、`AUTO=1`：

[include/utils/patterns.h:L33-L37](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/patterns.h#L33-L37) —— 定义 `MemMngPolicy` 两个取值，默认策略为 `AUTO`。

`DefaultBlockPolicy` 把 `TileShape` 和 `memPolicy` 打包成一个编译期策略对象，默认值就是 `AUTO`：

[include/elewise/block/builder.h:L27-L36](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/builder.h#L27-L36) —— `DefaultBlockPolicy` 持有 `tileShape` 与 `memPolicy`；全局 `defaultBlockPolicy` 默认 `MemMngPolicy::AUTO`、`TileShape=Shape<1,4096>`。

真正的「策略 → DAG 类型」分派发生在 `DagSelector`，用经典的「主模板 + `enable_if` 偏特化」实现编译期 `if`：

[include/elewise/block/schedule.h:L36-L44](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L36-L44) —— `DagSelector`：`AUTO` 走 `FullAutoDag`，其余（即 `MANUAL`）走 `ManualDag`。

`PreProcessComputeExpr` 把用户表达式拍平成 `OpAssign` 列表后，调用 `DagSelector` 选 DAG，再插入 `OpAlloc/OpFree`：

[include/elewise/block/schedule.h:L52-L72](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L52-L72) —— `PreProcessComputeExpr` 依 `memPolicy` 选 DAG，并据 DAG 算出的 Param/LocalVar 使用范围插入 `Alloc/Free`。

两个 DAG 类型的入口（细节在 u3-l3 已讲，这里只确认存在与归属）：

[include/elewise/graph/dag.h:L417-L417](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/dag.h#L417-L417) —— `ManualDag`，MANUAL 策略使用。

[include/elewise/graph/dag.h:L443-L446](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/dag.h#L443-L446) —— `FullAutoDag`，AUTO 策略使用，会处理 `IN_OUT` 原地参数并做依赖分析。

运行期还能在调试输出里直接看到当前生效的策略值：

[include/elewise/block/schedule.h:L193-L200](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L193-L200) —— `MakeScheduleConfig` 用 `printf` 打印当前 `Policy.memPolicy` 的整数值（AUTO=1，MANUAL=0），是验证策略是否生效的好钩子。

#### 4.1.4 代码实践

1. **目标**：确认 `MemMngPolicy` 确实走到了 `DagSelector`，并能从运行日志读到它的值。
2. **操作步骤**：
   - 打开 `examples/abs/abs.cpp`，确认它的 `blockPolicy` 用的是默认（即 AUTO）。
   - 用 `bash scripts/build.sh -DSOC=ascend950 abs` 编译并运行（真机或 cannsim 仿真均可）。
3. **需要观察的现象**：标准输出里应出现形如 `[DEBUG]: [Atvoss][BlockSchedule] MemPolicy is 1.` 的行。
4. **预期结果**：`1` 对应 `AUTO`。若你把策略改成 MANUAL（见 4.2.4）并正确拧入 Builder，这行会变成 `0`。
5. 若手头没有运行环境，**待本地验证**；但可静态确认：`printf` 的源码确实位于 `BaseBlockSchedule::MakeScheduleConfig`（schedule.h:198），逻辑成立。

#### 4.1.5 小练习与答案

**练习 1**：为什么说「AUTO 和 MANUAL 的数值结果一致，但缓冲分配不同」？请用「依赖分析」解释。

> 参考答案：DAG 选择只改变「缓冲何时 alloc/free、能否复用、冗余量是否被消除」，而每条 `OpAssign` 翻译成的 Ascend C 计算指令不变。所以算出来的值相同，区别仅在 UB 占用。

**练习 2**：如果把 `MemMngPolicy` 从枚举里删掉、直接写死 `FullAutoDag`，会失去什么能力？

> 参考答案：会失去「按声明顺序分配、保留所有中间缓冲」的对照能力，也无法构造 `tests/st/test_compute_*_with_manupolicy` 这类「故意保留冗余缓冲」的测试场景，从而无法回归验证 `FullAutoDag` 的冗余消除是否正确。

---

### 4.2 Builder 的 Schedule 模板形参：策略真正生效的入口

#### 4.2.1 概念说明

这是本讲**最容易踩坑、也最重要**的一节。很多读者以为「我在 Config 里声明了一个 `blockPolicy{MANUAL}`，策略就变成 MANUAL 了」——**不一定**。`MemMngPolicy` 只有作为 `Policy` 模板实参真正传进 `BlockBuilder` 时才生效。换句话说，Builder 的模板签名就是策略的「插口」，你必须把策略「插」到正确的插口上。

`BlockBuilder` 与 `KernelBuilder` 的最后一个模板参数是一个 **模板模板参数（template template parameter）** `Schedule`，默认分别指向 `DefaultBlockSchedule` / `DefaultKernelSchedule`。这给 ATVOSS 留了两个层面的扩展点：

- **Policy 层**（拧旋钮）：换 `TileShape`、换 `MemMngPolicy`、换 `DefaultSegmentPolicy`——这些都是数据，改 `Policy` 模板实参即可。
- **Schedule 层**（换实现）：替换整段调度算法——这是把 `Schedule` 模板模板参数换成你自己的类。

#### 4.2.2 核心流程

`BlockBuilder` 的模板形参从左到右依次是：

```
BlockBuilder<
    Compute,                // 用户计算表达式（必填）
    ArchTagcfg,             // 目标架构，默认 DAV_3510
    Policy,                 // ← blockPolicy 插这里！含 TileShape + MemMngPolicy
    ScheduleCfg,            // 运行期配置结构体，默认 DefaultBlockConfig
    Schedule                // ← 调度算法插这里！默认 DefaultBlockSchedule
>
```

`KernelBuilder` 类似，但**没有 ArchTag 参数**（架构从 BlockOp 透传），形参顺序是：

```
KernelBuilder<
    BlockOp,                // 上一层 Builder（必填）
    Policy,                 // ← kernelPolicy 插这里！含分段策略
    ScheduleCfg,            // 默认 DefaultKernelConfig
    Schedule                // ← 调度算法插这里！默认 DefaultKernelSchedule
>
```

> ⚠️ 两者模板参数个数不同（BlockBuilder 5 个、KernelBuilder 4 个），把 Policy 拧进去时位置不一样。这是初学者常错的地方。

#### 4.2.3 源码精读

`BlockBuilder` 签名——注意第 3 个参数 `const auto& Policy = defaultBlockPolicy`，第 5 个是模板模板参数 `Schedule`：

[include/elewise/block/builder.h:L42-L49](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/builder.h#L42-L49) —— `BlockBuilder` 模板形参：`Compute / ArchTagcfg / Policy / ScheduleCfg / Schedule`；`ScheduleClz` 把 Policy 与 Schedule 实例化成具体调度类。

`KernelBuilder` 签名——只有 4 个形参，且第 2 个就是 Policy：

[include/elewise/kernel/builder.h:L39-L49](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/builder.h#L39-L49) —— `KernelBuilder` 模板形参：`BlockOp / Policy / ScheduleCfg / Schedule`，`OpParam` 把 kernel 与 block 两层配置同框承载。

**正确的拧法（样例 abs）**：abs 把自定义 `blockPolicy` 作为**第 3 个**模板实参显式传入：

[examples/abs/abs.cpp:L34-L42](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L34-L42) —— abs 用 `BlockBuilder<AbsCompute, ArchTag, blockPolicy, DefaultBlockConfig>`，把 `blockPolicy`（含 TileShape、默认 AUTO）真正插到 Policy 插口上；`KernelBuilder<BlockOp, kernelPolicy>` 把分段策略插到第 2 个插口。

**反例（值得警醒的代码阅读发现）**：在 `tests/st/test_compute_*_with_manupolicy.cpp` 这几个文件里，`blockPolicy` 虽被声明为 MANUAL，但 `BlockOp` 别名**只写了两个模板实参**，没有把 `blockPolicy` 传进去：

[tests/st/test_compute_autobuffer_redundant_with_manupolicy.cpp:L38-L43](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/test_compute_autobuffer_redundant_with_manupolicy.cpp#L38-L43) —— `BlockOp = BlockBuilder<ComputeTestCompute, ArchTag>` 只有 2 个实参，第 3 个 Policy 形参取默认 `defaultBlockPolicy`（AUTO）；上面 L40 声明的 `blockPolicy{MANUAL}` 实际未被引用。

这是个真实的静态事实：`BlockBuilder<ComputeTestCompute, ArchTag>`（2 个实参）会回退到默认 `defaultBlockPolicy`，因此这几个 `_with_manupolicy` 测试**按当前写法实际跑的是 AUTO**。要让 MANUAL 真正生效，必须像 abs 那样把 `blockPolicy` 作为第 3 个模板实参传入（即 `BlockBuilder<ComputeTestCompute, ArchTag, blockPolicy>`）。这一对比恰好是本节核心教训：**策略不会因为你声明了它就生效，必须把它插到 Builder 的 Policy 形参上。**

> 说明：以上是对当前 HEAD 源码的静态阅读结论。运行期数值结果仍建议**待本地验证**——把第 3 个实参补上后重新编译，对比 `MemPolicy is 0/1` 的调试输出，即可确认。

#### 4.2.4 代码实践

1. **目标**：亲手把 abs 从 AUTO 切到 MANUAL 并验证生效。
2. **操作步骤**（只读分析，不修改源码仓库；可在本地副本上做）：
   - 复制 `examples/abs`，把 `AbsConfig` 里的 `blockPolicy` 改为 `Atvoss::Ele::DefaultBlockPolicy<TileShape>{TileShape{}, Atvoss::MemMngPolicy::MANUAL}`。
   - 把 `BlockOp` 别名补全为 `BlockBuilder<AbsCompute, ArchTag, blockPolicy, Atvoss::Ele::DefaultBlockConfig>`（参考 abs.cpp 原本就是这么写第 3 个实参的，只需确保 blockPolicy 现在带 MANUAL）。
   - 编译运行。
3. **需要观察的现象**：调试输出 `MemPolicy is X` 的值。
4. **预期结果**：改为 MANUAL 后应显示 `MemPolicy is 0.`；而原 abs（AUTO）显示 `1`。若数值仍为 1，说明 Policy 没拧进 Builder（回到本节反例的坑）。
5. 精度校验仍应 `passed`，因为数值结果与策略无关。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `BlockBuilder` 的 Policy 是第 3 个形参，而 `KernelBuilder` 的 Policy 是第 2 个？

> 参考答案：`BlockBuilder` 多了一个 `ArchTagcfg` 形参（架构在 Block 层就需要，用于读 `UB_SIZE` 等），Kernel 层的架构是从 `BlockOp` 透传的（见 kernel/schedule.h:42 `using ArchTag = typename BlockOp::ScheduleClz::ArchTag;`），所以 KernelBuilder 不需要单独的架构形参，Policy 顺位提前。

**练习 2**：若你想完全替换 Block 层的调度算法（不只是改 TileShape/MemMngPolicy），该动哪个模板形参？有什么约束？

> 参考答案：动第 5 个形参 `Schedule`（模板模板参数）。约束是你的自定义类必须满足 `DefaultBlockSchedule` 的「契约」：提供 `TileShape`、`BASIC_BLOCK`、`ScheduleCfgClz`、`ArchTag` 等内嵌类型与常量，以及 `Run(cfg, argTuple)` 成员，因为 `KernelBuilder` 会通过 `BlockOp::ScheduleClz::...` 反向访问这些成员（见 kernel/schedule.h:44、L121）。

---

### 4.3 自定义 TileShape 调优：UB 占用与 Tile 循环次数的权衡

#### 4.3.1 概念说明

`TileShape` 是 `DefaultBlockPolicy` 的另一半，决定**单核一次 Tile 处理多少数据**（即 `BASIC_BLOCK`）。它是性能调优最常拧的旋钮：

- **调大 TileShape** → 每次 Tile 处理更多元素 → Tile 循环次数（`wholeLoop`）更少 → 循环开销与同步开销下降，单 Tile 内的算术/访存比上升。
- **调小 TileShape** → 循环次数更多，但更省 UB，也利于尾部对齐。

但 TileShape 不能无限大——它受 UB 容量约束。ATVOSS 用一个编译期 `static_assert`（`TileCheckAssert`）兜底：用户 TileShape 对应的 `BASIC_BLOCK` 一旦超过单缓冲槽上限 `UB_TILE_SIZE`，直接编译失败。

#### 4.3.2 核心流程

两条公式刻画了 TileShape 的影响。设 `totalElemCnt` 为单核总元素数：

\[
\text{wholeLoop} = \left\lfloor \frac{\text{totalElemCnt}}{\text{BASIC\_BLOCK}} \right\rfloor,\quad
\text{tileCnt} = \text{totalElemCnt} \bmod \text{BASIC\_BLOCK}
\]

而 UB 每个缓冲槽的大小被等分：

\[
\text{UB\_TILE\_SIZE} = \frac{\text{UB\_SIZE}}{\text{MAX\_BUFFER\_COUNT}},\quad
\text{MAX\_BUFFER\_COUNT} = 2\cdot|\text{InParams}| + 2\cdot|\text{OutParams}| + |\text{LocalVars}|
\]

注意分母 `MAX_BUFFER_COUNT` 只取决于**入参/出参/临时变量的个数**（双缓冲所以乘 2），**与 TileShape 无关**。因此调大 TileShape 不会改变缓冲槽数，只会让每个槽要装更多元素；当 `BASIC_BLOCK * MaxSize > UB_TILE_SIZE` 时触发 `TileCheckAssert`。

#### 4.3.3 源码精读

abs 用一维 `Shape<TILE_SIZE>`（TILE_SIZE=32）作 TileShape：

[examples/abs/abs.cpp:L16-L23](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp#L16-L23) —— `TILE_SIZE=32`，`TileShape = Atvoss::Shape<32>`，是一维形状。

默认 TileShape 是二维 `Shape<1, 4096>`（`BASIC_BLOCK=4096`）：

[include/elewise/block/builder.h:L34-L36](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/builder.h#L34-L36) —— `DEFAULT_SHAPE=4096`，默认 `TileShape=Shape<1,4096>`、默认 AUTO。

`BASIC_BLOCK` 由 `GetTotalElement` 对 TileShape 编译期累乘得到（一维 Shape<32> → 32；二维 Shape<1,4096> → 4096）：

[include/elewise/block/schedule.h:L86-L88](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L86-L88) —— `BASIC_BLOCK` 在编译期由 Policy 的 TileShape 累乘得到。

UB 缓冲槽等分公式与三段地址（IN/OUT/CALC）：

[include/elewise/block/schedule.h:L134-L142](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L134-L142) —— `MAX_BUFFER_COUNT` 与 `UB_TILE_SIZE = UB_SIZE / MAX_BUFFER_COUNT`，并把 UB 划成 IN/OUT/CALC 三段。

Tile 循环拆分（整 Tile + 尾 Tile）：

[include/elewise/block/schedule.h:L222-L248](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L222-L248) —— `Run` 算出 `wholeLoop`/`tileCnt`，`Process` 循环 `wholeLoop` 次完整 Tile，再补一次尾 Tile，每次构造 `ContextData`（含 `pingPong=i&1`）下传 `Tile::Evaluate`。

编译期兜底断言：

[include/elewise/block/schedule.h:L74-L77](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L74-L77) —— `TileCheckAssert`：用户 TileSize 超过 UB 单槽容量时编译失败。

[include/elewise/block/schedule.h:L207-L214](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L207-L214) —— 构造函数里若 `UB_TILE_SIZE < BASIC_BLOCK*MaxSize`（对齐后）则实例化 `TileCheckAssert` 触发断言。

#### 4.3.4 代码实践

1. **目标**：定量分析把 abs 的 TileShape 从 32 调到 4096 对 UB 占用与 Tile 循环次数的影响。
2. **操作步骤**：
   - 用上面 4.3.2 的公式，假设单核 `totalElemCnt = 100000`、`MaxSize = 4`（float）、abs 有 1 入参 + 1 出参。
   - 算 `MAX_BUFFER_COUNT = 2*1 + 2*1 + 0 = 4`，`UB_TILE_SIZE = 240KiB / 4 = 60KiB = 61440 B`。
   - 算两种 TileShape 下的 `wholeLoop`/`tileCnt`。
3. **需要观察的现象**：循环次数的变化，以及是否触顶。
4. **预期结果**：
   - TileShape=32（`BASIC_BLOCK=32`）：`wholeLoop = 100000/32 = 3125`，`tileCnt=0`；每次 Tile 32 个 float=128B，远小于 60KiB 槽，UB 极度空闲、循环开销大。
   - TileShape=4096（`BASIC_BLOCK=4096`）：`wholeLoop = 100000/4096 = 24`，`tileCnt = 100000 - 24*4096 = 1696`；每次 Tile 4096 float=16KiB < 60KiB，**仍安全**，循环次数从 3125 降到 25，性能通常更优。
   - 若继续调大到 ~15360（`61440/4`）以上，`BASIC_BLOCK*MaxSize > UB_TILE_SIZE`，`TileCheckAssert` 编译失败。
5. 数值正确性与 TileShape 无关，精度仍应 `passed`；性能差异**待本地验证**（需在真机/仿真上测耗时）。

#### 4.3.5 小练习与答案

**练习 1**：为什么调大 TileShape 不会改变 `MAX_BUFFER_COUNT`？

> 参考答案：`MAX_BUFFER_COUNT = 2*|InParams| + 2*|OutParams| + |LocalVars|`，只取决于参数与临时变量的数量（双缓冲乘 2），与 TileShape 无关。调大 TileShape 只增加每个槽要装的数据量，不增加槽数。

**练习 2**：abs 当前 TileShape=32 时 UB 利用率很低，为什么框架仍允许这么小的值？

> 参考答案：`TileCheckAssert` 只设了「上界」（不能超 UB），没设下界。小 TileShape 虽浪费 UB、增加循环开销，但功能正确，且对尾部对齐友好、便于教学演示，故被允许。

---

### 4.4 platform_info 与 arch：两套硬件信息，各管一摊

#### 4.4.1 概念说明

ATVOSS 有**两套**硬件信息来源，初学者容易混淆：

- **`arch.h::DAV_3510`（编译期常量）**：`CORE_NUM=56`、`UB_SIZE=240KB`。这些是写死在类型里的 `static constexpr`，编译期即可用，是 ATVOSS 调度逻辑（核数裁剪、UB 等分）**真正依赖**的来源。
- **`platform_info.h::OpPlatformInfo`（运行期查询）**：通过 CANN 的 `platform_ascendc::PlatformAscendCManager` 在运行时查询 `vectorCoreNum / ubSize / cacheLineSize / ubBlockSize`，适合做更细粒度的硬件感知调优。

一句话区分：`arch.h` 是「编译期、写死、框架内部用」；`platform_info.h` 是「运行期、查询、留给高级调优用」。

#### 4.4.2 核心流程

- Kernel 层 `MakeScheduleConfig` 算核数时，用 `ArchTag::CORE_NUM`（编译期 56）做上限裁剪。
- Block 层算 `UB_TILE_SIZE` 时，用 `ArchTag::UB_SIZE`（编译期 240KB）做分母。
- `GetOpPlatformInfo()` 则在 Host 运行期，按 SoC 版本查表返回 `OpPlatformInfo`，供需要运行期硬件感知的场景使用。

#### 4.4.3 源码精读

编译期硬件常量（框架调度实际依赖的来源）：

[include/common/arch.h:L20-L27](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/common/arch.h#L20-L27) —— `DAV_3510`：`CORE_NUM=56`、`UB_SIZE=240*1024`，均为 `static constexpr`，编译期可用。

Kernel 层用 `CORE_NUM` 裁剪核数：

[include/elewise/kernel/schedule.h:L84-L87](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/schedule.h#L84-L87) —— `blockNum` 超过 `ArchTag::CORE_NUM` 时被裁剪到 56。

运行期硬件查询结构体与查表函数：

[include/common/platform_info.h:L16-L28](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/common/platform_info.h#L16-L28) —— `OpPlatformInfo` 含 `vectorCoreNum/ubSize/cacheLineSize/ubBlockSize` 四项。

[include/common/platform_info.h:L30-L59](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/common/platform_info.h#L30-L59) —— `GetOpPlatformInfo()` 按 SoC 版本查表；表内目前仅含 `ASCEND910B / ASCEND310B / ASCEND310P` 三项，未命中则返回 `{0,0,0,0}` 并打印错误。

> ⚠️ **代码阅读提示（待确认）**：`platform_info.h` 的查表里**没有** `ASCEND950` 这一项。而 ATVOSS 的目标芯片是 `ascend950`（对应 `DAV_3510`，见 u1-l2 的 SOC 映射）。这意味着在 950 上直接调 `GetOpPlatformInfo()` 当前会落入「未命中」分支返回全 0。因此**对 950 而言，`arch.h` 的编译期常量才是框架调度真正依赖的可靠来源**；`platform_info.h` 更像是为其它 SoC 预留的运行期查询通道。这一判断基于静态阅读，运行期表现**待本地验证**。

`ArchTag` 是如何在层间透传的（解释 KernelBuilder 为何没有架构形参）：

[include/elewise/kernel/schedule.h:L40-L44](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/schedule.h#L40-L44) —— `BaseKernelSchedule` 通过 `BlockOp::ScheduleClz::ArchTag` 拿到架构标签，故 KernelBuilder 不必单独传架构形参。

#### 4.4.4 代码实践

1. **目标**：在源码中定位两套硬件信息各自被消费的位置。
2. **操作步骤**：
   - 在 `include/elewise/` 下搜索 `ArchTag::CORE_NUM` 与 `ArchTag::UB_SIZE` 的使用点。
   - 在 `include/` 下搜索 `GetOpPlatformInfo` 的调用点。
3. **需要观察的现象**：前者应有多处（Kernel/Block 调度），后者调用点很少甚至没有（在当前 HEAD）。
4. **预期结果**：`CORE_NUM` 用于核数裁剪、`UB_SIZE` 用于 UB 等分；`GetOpPlatformInfo` 在本仓库的算子主链路中**未被调度核心消费**（与上一条「待确认」提示一致）。调用点的确切数量**待本地用 Grep 确认**。
5. 结论：日常调优应信任 `arch.h` 常量；若要做跨 SoC 的运行期适配，才需要扩展 `platform_info.h` 的查表。

#### 4.4.5 小练习与答案

**练习 1**：如果未来要支持一款新芯片（核数不同、UB 更大），最少要改哪里让 ATVOSS 调度正确？

> 参考答案：在 `arch.h` 增加一个新的架构结构体（如 `DAV_XXXX`，带新的 `CORE_NUM`/`UB_SIZE`），并在 Config 的 `ArchTag` 指向它。因为 Kernel/Block 调度都读 `ArchTag::CORE_NUM/UB_SIZE`，这样改即可让核数裁剪与 UB 等分适配新硬件。

**练习 2**：`platform_info.h` 里 `cacheLineSize`（256）和 `ubBlockSize`（32）对调优有什么潜在用途？

> 参考答案：`cacheLineSize` 影响访存对齐策略（按 cache line 对齐可提升 GM↔UB 带宽利用率），`ubBlockSize` 影响 UB 内 bank 冲突规避。它们是更细粒度的硬件感知参数，可用于优化 DataCopy 的对齐与分块，但当前主调度链路未消费它们（待确认）。

---

## 5. 综合实践

把本讲四个模块串成一个完整任务：**为一个含冗余中间量的算子，对比 AUTO/MANUAL 的缓冲差异，并做 TileShape 调优。**

### 任务背景

`tests/st/` 下有三组对照用例，每组共享同一份 `Compute` 表达式，只在策略命名上区分 AUTO/MANUAL：

- `test_compute_autobuffer_redundant_*`：表达式里写了 `_1 = in1 + in2` 但最终只用 `_2 = in1 * in2`，`_1` 是死掉的自动缓冲。
- `test_compute_tmp_redundant_*`：声明了 `tmp1`、`tmp2` 两个临时变量，但 `tmp1` 赋值后未被使用。
- `test_compute_expression_redundant_*`：`tmp` 被连续赋值两次（重复计算）。

### 步骤一：阅读与对比（源码阅读型）

1. 打开任一对，例如：
   - [tests/st/test_compute_autobuffer_redundant_with_manupolicy.cpp:L24-L43](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/test_compute_autobuffer_redundant_with_manupolicy.cpp#L24-L43)
   - [tests/st/test_compute_autobuffer_redundant_with_autopolicy.cpp:L23-L41](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/test_compute_autobuffer_redundant_with_autopolicy.cpp#L23-L41)
2. 确认两者 `Compute` 完全相同，golden 也相同（autobuffer 组 golden=6，即 `out=in1*in2=2*3`）。
3. **回答两个问题**：
   - **概念层面**：若 MANUAL 真正生效，它会为死掉的 `_1` 分配 UB 缓冲；而 AUTO 的 `FullAutoDag` 会通过依赖分析把 `_1` 消除（不分配缓冲、不生成计算）。两者数值相同，缓冲槽数不同。
   - **代码层面（关键发现）**：`_with_manupolicy` 文件里 `blockPolicy{MANUAL}` 被声明，但 `BlockOp = BlockBuilder<ComputeTestCompute, ArchTag>` 只给了 2 个模板实参，**Policy 形参取默认 `defaultBlockPolicy`（AUTO）**。所以按当前写法，MANUAL 并未真正生效。要让它生效，需把第 3 个模板实参补上：`BlockBuilder<ComputeTestCompute, ArchTag, blockPolicy>`。

### 步骤二：动手验证（本地副本，不修改仓库）

1. 复制 `test_compute_autobuffer_redundant_with_manupolicy.cpp`，把 `BlockOp` 改为 `BlockBuilder<ComputeTestCompute, ArchTag, blockPolicy>`，使 MANUAL 真正拧入。
2. 用 `bash scripts/build.sh -DSOC=ascend950 --st test_compute_autobuffer_redundant_with_manupolicy` 编译运行（构建/运行参数以本地 `scripts/build.sh` 实际支持为准，**待本地验证**）。
3. 观察调试输出 `MemPolicy is X`：补全前（默认）应为 `1`（AUTO），补全后应为 `0`（MANUAL）。
4. 精度都应 `passed`——这正说明 MemMngPolicy 只影响缓冲分配、不影响数值。

### 步骤三：TileShape 调优分析

1. 假设单核 `totalElemCnt = 100000`、float、1 入参 1 出参。
2. 按 4.3 推导：TileShape=32 → `wholeLoop=3125`；TileShape=4096 → `wholeLoop=24`、`tileCnt=1696`，且 4096×4B=16KiB < 60KiB 槽，安全。
3. 给出你的调优建议：在 UB 不超的前提下，TileShape 调大可显著降低循环开销；上限约为 `UB_TILE_SIZE/MaxSize ≈ 15360` 个元素。

### 预期成果

- 一段文字说明 MemMngPolicy 如何影响缓冲分配（AUTO 消除冗余、MANUAL 保留）。
- 一段文字指出 `_with_manupolicy` 测试当前 Policy 未拧入的事实及修正方法。
- 一张 TileShape vs `wholeLoop`/UB 占用 的小表格。

> 全程不修改仓库源码；所有运行结果在缺环境时标注**待本地验证**，不编造命令输出。

## 6. 本讲小结

- **MemMngPolicy** 是「内存管理策略」枚举：`AUTO`（默认）走 `FullAutoDag` 做依赖分析与冗余缓冲消除，`MANUAL` 走 `ManualDag` 基本按声明分配；两者数值结果一致，差别在 UB 缓冲数量。
- 分派由 `DagSelector` 在编译期完成（`schedule.h:36-44`），策略值还能在运行期 `MakeScheduleConfig` 的 `printf` 里读到。
- **Builder 的模板形参就是策略插口**：`BlockBuilder` 的 Policy 是第 3 个形参，`KernelBuilder` 的是第 2 个；策略必须作为模板实参显式传入才生效——`abs.cpp` 是正确范例，`tests/st/*_with_manupolicy` 当前未把 Policy 拧入（回退默认 AUTO）是反面教材。
- 最后一个形参是 **Schedule 模板模板参数**，允许替换整段调度算法（扩展点），但自定义类须满足 `DefaultBlockSchedule` 的类型/成员契约。
- **TileShape 调优**改变 `BASIC_BLOCK`，影响 `wholeLoop` 循环次数与单 Tile 数据量；`MAX_BUFFER_COUNT` 只与参数个数有关，`UB_TILE_SIZE = UB_SIZE / MAX_BUFFER_COUNT`，`TileCheckAssert` 在编译期兜底防越界。
- **两套硬件信息**：`arch.h::DAV_3510`（编译期 `CORE_NUM=56`、`UB_SIZE=240KB`）是调度真正依赖的来源；`platform_info.h`（运行期查询，当前查表未含 950）留给更细粒度的硬件感知调优。

## 7. 下一步学习建议

- 想深入「AUTO 到底如何消除冗余缓冲」：阅读 u3-l3（DAG 与 Bind）与 u3-l4（线性化与图优化 Pass），重点看 `FullAutoDag` 的依赖闭包与 `Simplify` 内联。
- 想理解双缓冲与缓冲池的物理实现：阅读 u3-l5（Buffer 管理与双缓冲），看 `BlockBufferEx` 如何把 `bufferId` 变成 UB 切片、`pingPong` 如何隐藏搬运延迟。
- 想看归约/广播算子如何改变 DAG 处理（从而影响缓冲策略）：阅读 u3-l6（Reduce 模块）与 u3-l7（rms_norm 级联样例）。
- 若计划做硬件感知的高级调优：先在本地用 Grep 摸清 `GetOpPlatformInfo` 的实际调用点，再决定是否扩展 `platform_info.h` 的 SoC 查表以覆盖 950。
