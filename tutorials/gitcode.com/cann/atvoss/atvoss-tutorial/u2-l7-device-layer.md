# Device 层：DeviceAdapter 与算子启动

## 1. 本讲目标

在 u1-l3 里我们第一次看到 `DeviceAdapter::Run` 走「PrepareParams → CalculateTiling → LaunchKernel」三步，但当时只把它当作 Host 侧的一个总框；在 u2-l6 里我们又把 `deviceOp.Run(arguments, stream)` 当作「消费 `ArgumentsBuilder` 产物」的黑盒。本讲把这个黑盒彻底打开，聚焦 **Device 层内部到底做了什么**。读完本讲，你应当能够：

1. 画出 `deviceOp.Run(arguments, stream)` 的完整调用链，列出 `Run` 内部依次调用的每一个函数，并区分「Host 侧执行」与「跨进 NPU 执行」的边界。
2. 解释 `CalculateTiling` 如何用一个 `OpParam` 结构体**同时**承载 Kernel（核间）与 Block（核内 Tile）两层的调度配置，并说明为什么 Block 这一步要吃 Kernel 的结果。
3. 说出 `DeviceTensor<T>` 与 `Atvoss::Tensor<T>` 的区别，以及 `TransformArgs` 如何在启动 kernel 前把张量还原成裸设备指针、把标量原样透传。
4. 解释 `KernelCustom<<<blockNum, nullptr, stream>>>(cfg, args)` 这条启动语句里每一个位置参数的含义，以及 tiling 结果是如何作为 kernel 参数被送进 NPU 的。

## 2. 前置知识

本讲是 Host 侧调度逻辑与「跨 Host/Device 边界」的结合。阅读前请确认你已理解（见前置讲义）：

- **五层架构与 `DeviceAdapter::Run` 三步总框**（u1-l3）：Device 层跑在 Host CPU，`KernelCustom<<<blockNum>>>` 是跨 Host/Device 边界的唯一跳板；`CalculateTiling` 在 Host 上一次性算出核间（Kernel）与核内（Block）两层切分。
- **`ArgumentsBuilder` 产物结构**（u2-l6）：`arguments` 是一个两层 `std::tuple`，第 0 位是 inputOutput 实参 tuple（顺序对应 `Compute()` 里 `PlaceHolder<N>` 的序号），第 1 位是 attr tuple。
- **`PlaceHolder<N>` 序号契约**（u2-l2、u2-l6）：序号 `N` 是 1-based 且从 1 连续，运行时用 `std::get<N-1>` 取参。

本讲会用到几个 C++17 工具（u2-l6 已解释过 `tuple`/`tuple_cat`/折叠表达式，这里补充两个）：

- **`std::apply(f, tuple)`**：把一个 tuple「拆开」当成参数包喂给可调用对象 `f`，等价于 `f(get<0>(tuple), get<1>(tuple), ...)`。
- **`if constexpr (cond)`**：编译期分支。`cond` 在编译期求值，只编译被选中的那一路——这是 `TransformArgs` 区分「标量 / 张量」的关键。

还会用到一条昇腾（Ascend C）语法：

- **`KernelCustom<<<grid, workspace, stream>>>(args...)`**：kernel 启动语法。`grid` 是启动的核数（即 `blockNum`），`workspace` 是动态 workspace 指针，`stream` 是 ACL 流，括号里是传给 kernel 的参数。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `include/elewise/device/device_adapter.h` | **本讲主角**。`DeviceAdapter<KernelOp>`、`Run`、`TransformArgs`、`LaunchKernelWithDataTuple`、`KernelCustom` 全在这里。 |
| `include/elewise/device/tiling.h` | **仅 30 行**。自由函数 `CalculateTiling`，串联 Kernel 与 Block 两层 `MakeScheduleConfig`。 |
| `include/elewise/device/device_tensor.h` | `DeviceTensor<T>`：Device 侧的极简指针包装。 |
| `include/elewise/kernel/builder.h` | `KernelBuilder::OpParam` 结构体（`kernelParam` + `blockParam`）与各层类型别名。 |
| `include/elewise/kernel/schedule.h` | Kernel 层 `MakeScheduleConfig`（核数切分）与 kernel 侧 `Run`（消费 tiling）。 |
| `include/elewise/block/schedule.h` | Block 层 `MakeScheduleConfig` 的签名（吃 kernel 结果）。 |
| `include/common/arch.h` | `DAV_3510` 硬件常量 `CORE_NUM=56`、`UB_SIZE`，核数上限即来自此处。 |
| `examples/muls/muls.cpp` | `deviceOp.Run(arguments, stream)` 的真实调用点。 |

## 4. 核心概念与源码讲解

### 4.1 DeviceAdapter::Run 主流程

#### 4.1.1 概念说明

`DeviceAdapter<KernelOp>` 是 Device 层的唯一类，也是整个五层架构里**跑在 Host CPU 上的最后一站**。它对用户只暴露一个方法 `Run(arguments, stream)`，内部完成三件事：

1. **重建表达式、准备参数**：用 `DeviceTensor` 作为张量模板参数，重新跑一遍用户的 `Compute()`，拿到编译期表达式与 `Params` 列表；再据 `Params` 把 `arguments` 第 0 位的实参逐个构造成「运行期参数对象」。
2. **算 tiling**：调用 `CalculateTiling`，在 Host 上一次性算出核间（Kernel）与核内（Block）两层的调度配置，填进一个 `OpParam` 结构体。
3. **启动 kernel**：把参数转成「标量 / 裸指针」二分形式，用 `<<<blockNum>>>` 跨边界启动 `KernelCustom`。

注意一个容易忽略的点：`DeviceAdapter` 并**不从 `arguments` 里读 `Params`**。`arguments` 只携带运行期数据（指针、标量、形状），不携带类型信息。`Params`（即「有哪些 `PlaceHolder`、各是什么类型/序号/usage」）是 `DeviceAdapter` 自己**重新执行 `Compute()`** 推导出来的——这正是「声明式表达式」的体现：同一份 `Compute()`，在不同层用不同的 `Tensor` 模板参数跑，就得到不同层的视图。

#### 4.1.2 核心流程

`Run` 的执行顺序（Host 侧，直到 `LaunchKernelWithDataTuple` 内部才跨进 NPU）：

```
deviceOp.Run(arguments, stream)
 │
 ├─ expr   = ToLinearizerExpr( ExprMaker{}.Compute<DeviceTensor>() )   // 重建表达式
 ├─ Params = Params_t<Expr>                                            // 从表达式取参数列表
 ├─ argTuple = std::get<0>(arguments)                                  // 取 inputOutput tuple（第 0 位）
 │
 ├─ ① params = PrepareParams<Params>(argTuple)          // 构造运行期参数（DeviceTensor / 标量）
 │
 ├─ ② OpParam opParam;
 │     CalculateTiling<KernelOp>(arguments, opParam)     // 填 kernelParam + blockParam
 │
 ├─ ③ convertArgs = ConvertArgs<Params>(params, argTuple) // 按 inputOutput 顺序对齐
 │     LaunchKernelWithDataTuple<KernelOp>(              // ★ 跨进 NPU
 │           opParam.kernelParam.blockNum, stream, opParam, convertArgs)
 │           └─ KernelCustom<<<blockNum, nullptr, stream>>>(opParam, 转换后参数)
 └─ return 0
```

代码注释里明确把这叫三步：`// 1. prepare Param`、`// 2. calc dynamic param （tiling / workspace）`、`// 3. kernel launch`。表达式重建与取 `argTuple` 是这三步之前的「前奏」。

#### 4.1.3 源码精读

先看 `DeviceAdapter` 的类骨架与几个关键类型别名，它展示了 Device 层如何「向内」导航到 Block 层：

[include/elewise/device/device_adapter.h:L76-L90](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/device_adapter.h#L76-L90) —— `DeviceAdapter<KernelOp>` 从 `KernelOp::ScheduleClz` 取出 `ExprMaker`（即用户的 `Compute` 类型）与 `BlockOp`（Block 层算子），并定义 `OpParam = KernelOp::ScheduleCfgClz`（即 `KernelBuilder::OpParam`）。这里把 `Tensor` 别名重定义为 `DeviceTensor`，所以 `Compute<Tensor>()` 实际跑的是 `Compute<DeviceTensor>`。

`Run` 的完整主体：

[include/elewise/device/device_adapter.h:L97-L124](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/device_adapter.h#L97-L124) —— `Run` 的三步主流程。第 100 行 `ExprMaker{}.Compute<Tensor>()` 重建表达式；第 104 行 `std::get<0>(arguments)` 取第 0 位 inputOutput tuple；第 106 行 `PrepareParams`；第 110 行 `CalculateTiling`；第 115、121 行 `ConvertArgs` 与 `LaunchKernelWithDataTuple`。

注意第 116–119 行的 `#if ATVOSS_DEBUG_MODE == 2`：在性能剖析模式下，同一个 kernel 会被连续启动 200 次以便计时，正常编译时只启动一次。这说明 `LaunchKernelWithDataTuple` 本身是幂等的纯启动调用。

`OpParam` 到底长什么样？它定义在 `KernelBuilder` 内部：

[include/elewise/kernel/builder.h:L44-L49](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/builder.h#L44-L49) —— `KernelBuilder::OpParam` 是一个仅含两个字段的结构体：`ScheduleCfg kernelParam`（核间切分）与 `BlockOp::ScheduleCfgClz blockParam`（核内 Tile 切分）。这正是「一个 `OpParam` 同时承载两层配置」的来源。`ScheduleCfgClz` 就是这个 `OpParam` 的别名（第 49 行），所以 `DeviceAdapter` 里的 `OpParam` 与它完全等同。

#### 4.1.4 代码实践

**实践目标**：追踪 muls 样例里 `deviceOp.Run(arguments, stream)` 的调用链，把 `Run` 内部依次调用的函数抄成一张清单。

**操作步骤**：

1. 打开 `examples/muls/muls.cpp` 第 182–190 行，找到 `deviceOp.Run(arguments, stream)` 这一行。

   [examples/muls/muls.cpp:L182-L190](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/muls/muls.cpp#L182-L190) —— muls 样例里按输入类型选择 `DeviceOp`（`float` 走 `DeviceOp`、`int32_t` 走 `DeviceOpPromtIn`）并调用 `Run`。

2. 对照 `Run` 主体（上面的 L97–L124），按下表填写「第几步 / 调用了什么 / 在哪一侧执行」：

| 步骤 | 调用的函数 | Host 还是 NPU |
| --- | --- | --- |
| 前奏 | `ExprMaker{}.Compute<DeviceTensor>()` + `ToLinearizerExpr` | Host（编译期为主） |
| 前奏 | `std::get<0>(arguments)` | Host |
| ① | `PrepareParams<Params>(argTuple)` | Host |
| ② | `CalculateTiling<KernelOp>(arguments, opParam)` | Host |
| ③ | `ConvertArgs<Params>(params, argTuple)` | Host |
| ③ | `LaunchKernelWithDataTuple<KernelOp>(...)` | Host 发起，内部跨进 NPU |

**需要观察的现象**：除 `LaunchKernelWithDataTuple` 内部的 `KernelCustom<<<...>>>` 之外，`Run` 的其余所有调用都还在 Host CPU 上；真正「过边界」只在 `<<<blockNum>>>` 这一行。

**预期结果**：你应当能复述出「Host 侧完成全部 tiling 与参数准备，再一次性把 `opParam` 与转换后的参数送进 NPU」这一关键结论——这也解释了为什么 u1-l3 说「Device 层是 Host 侧最后一站」。

> 待本地验证：若在仿真器（cannsim）上运行 muls，可在 `Run` 的第 106、110、115 行各加一行 `printf`（仅用于学习，验证后还原），观察三步的实际执行顺序与 `opParam.kernelParam.blockNum` 的值。

#### 4.1.5 小练习与答案

**练习 1**：`Run` 里第一步是 `PrepareParams`（准备参数），第二步才是 `CalculateTiling`（算 tiling）。为什么参数准备在前、tiling 在后？tiling 不是更「上游」吗？

**参考答案**：因为这两步的输入不同。`PrepareParams` 只依赖 `argTuple`（第 0 位实参），作用是把 `Atvoss::Tensor` 包成 `DeviceTensor`、把标量原样取出；它的产物 `params` 稍后在 `ConvertArgs` 里要用。`CalculateTiling` 依赖的是 `arguments` 里的**形状信息**（从第 0 位第 0 个张量的 `shape_vector()` 取），与 `params` 无关。所以两者本就独立，顺序由代码作者安排；此处先准备 `params`，是为了让它在跨越 `CalculateTiling` 这一步后仍可用于第 ③ 步的 `ConvertArgs`。

**练习 2**：`DeviceAdapter` 为什么要自己再跑一遍 `Compute<DeviceTensor>()`，而不是直接从 `arguments` 里读「有哪些参数」？

**参考答案**：`arguments` 是运行期数据容器（`tuple` 里只有指针和标量值），不携带「第几个是张量、第几个是标量、usage 是什么」这类类型信息。而 `Compute()` 是声明式表达式，用不同的 `Tensor` 模板参数跑就能得到对应层的类型化视图。Device 层用 `DeviceTensor` 跑一遍，便能在编译期拿到 `Params` 列表（序号、类型、usage），从而驱动后续 `PrepareParams`/`ConvertArgs` 的下标对齐。

### 4.2 CalculateTiling 串联 Kernel/Block 配置

#### 4.2.1 概念说明

`CalculateTiling` 是三步里最「有计算量」的一步。它的职责是：在 Host 上，根据**输入张量的总元素数**，一次性算出两层调度配置——

- **Kernel 层（`kernelParam`）**：把总任务切到多少个核（`blockNum`）、每个核平均处理多少个对齐单元（`unitNumPerCore`）、有多少个核需要多处理一个单元（`moreUnitCoreNum`）、最后一个核的尾数（`tailNum`）。
- **Block 层（`blockParam`）**：单核内部的 Tile 切分信息（`wholeLoop`、`tileCnt` 等）。

这两层**不是平级**的：Block 的 `MakeScheduleConfig` 在签名上**接收 Kernel 的结果**（`kernelParam`）作为入参，因为「单核要处理多少元素」必须等「核间怎么分」定下来之后才能算。这就是 u1-l3 里说的「Block 依赖 Kernel 的结果」的具体体现。

值得一提的是，`DeviceAdapter` 内部其实**还有一个成员函数 `CalcParam`**（见下），它的逻辑与自由函数 `CalculateTiling` 完全相同，但 `Run` 调用的是 tiling.h 里的自由函数版本，成员 `CalcParam` 在当前代码路径中未被调用（疑似历史遗留的等价副本）。

#### 4.2.2 核心流程

`CalculateTiling` 的串联过程（tiling.h）：

```
CalculateTiling<KernelOp>(arguments, opParam)
 │
 ├─ KernelOp::ScheduleClz::MakeScheduleConfig(arguments, opParam.kernelParam)     // 先算核间
 │      └─ 从 arguments[0][0].shape_vector() 取形状 → totalEleNum
 │         → 按 ACTUAL_N_ASSIGN 对齐，切出 blockNum / unitNumPerCore / ...
 │         → blockNum 被 ArchTag::CORE_NUM(=56) 上限裁剪
 │
 └─ BlockOp::ScheduleClz::MakeScheduleConfig(arguments, opParam.kernelParam,      // 再算核内
                                              opParam.blockParam)
        └─ 当前默认实现仅打印调试信息并返回 true（真正的 Tile 切分在 kernel 侧 Run 时算）
```

Kernel 层的核数切分用一组简单公式完成。设总元素数为 \(N\)、对齐单元大小为 \(A\)（即 `ACTUAL_N_ASSIGN`，一维 Tile 时为 32）、单核基本块元素数为 \(B\)（`BASIC_CORE_ELE_NUM`，已向上对齐到 \(A\)），则：

\[
\text{unitNum}=A,\quad
\text{basicCoreUnitNum}=\frac{B}{A},\quad
\text{totalUnitCnt}=\left\lfloor \frac{N}{A}\right\rfloor
\]

\[
\text{blockNum}=\min\!\left(\left\lceil \frac{\text{totalUnitCnt}}{\text{basicCoreUnitNum}}\right\rceil,\ \text{CORE\_NUM}\right)
\]

\[
\text{unitNumPerCore}=\left\lfloor \frac{\text{totalUnitCnt}}{\text{blockNum}}\right\rfloor,\quad
\text{moreUnitCoreNum}=\text{totalUnitCnt}\bmod \text{blockNum},\quad
\text{tailNum}=N\bmod A
\]

当 \(N \le B\) 时退化为单核：`blockNum=1`、`tailNum=N`。这套公式的含义是：先把总元素折成「对齐单元」数，再尽量均分给各核；除不尽时让前 `moreUnitCoreNum` 个核各多扛一个单元，末尾再补一个不满一个单元的 `tailNum`。

#### 4.2.3 源码精读

tiling.h 的全部核心——自由函数 `CalculateTiling`，只有十几行：

[include/elewise/device/tiling.h:L16-L30](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/tiling.h#L16-L30) —— 先调 Kernel 层 `MakeScheduleConfig` 填 `cfg.kernelParam`，再调 Block 层 `MakeScheduleConfig`（多传一个 `cfg.kernelParam`）填 `cfg.blockParam`。两层任一失败则打印错误并返回 `false`，`Run` 据此中止。

Kernel 层 `MakeScheduleConfig` 的真实计算（即上面公式的代码化）：

[include/elewise/kernel/schedule.h:L57-L93](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/schedule.h#L57-L93) —— 第 63 行 `std::get<0>(std::get<0>(arguments)).shape_vector()` 取第一个输入张量的形状并累乘得 `totalEleNum`；第 71–91 行按上面的公式算出 `blockNum`（第 85–87 行被 `ArchTag::CORE_NUM` 上限裁剪）、`unitNumPerCore`、`moreUnitCoreNum`、`tailNum`。

`CORE_NUM` 这个上限就来自 `arch.h`：

[include/common/arch.h:L22-L25](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/common/arch.h#L22-L25) —— `DAV_3510` 目标架构定义 `CORE_NUM = 56`、`UB_SIZE = 240*1024`。`blockNum` 不会超过 56。

Block 层 `MakeScheduleConfig` 的签名与当前实现——注意它比 Kernel 层多一个 `kernelConfig` 入参：

[include/elewise/block/schedule.h:L193-L200](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L193-L200) —— Block 层 `MakeScheduleConfig(arguments, kernelConfig, blockConfig)` 当前只打印两行调试信息并返回 `true`，并未真正写 `blockConfig`。真正的单核 Tile 切分（`wholeLoop`、`tileCnt`）是在 kernel 侧 `Run` 时根据每核元素数现算的（见 4.4.3）。

最后，对比 `DeviceAdapter` 内部那个未被 `Run` 调用的等价成员：

[include/elewise/device/device_adapter.h:L128-L140](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/device_adapter.h#L128-L140) —— 成员 `CalcParam` 与 tiling.h 的自由函数 `CalculateTiling` 逻辑完全一致（同样的两次 `MakeScheduleConfig`）。`Run` 第 110 行调用的是自由函数版本，此成员在当前路径下未被调用，疑似历史遗留副本。

#### 4.2.4 代码实践

**实践目标**：用一组具体数字手工跑一遍 Kernel 层 `MakeScheduleConfig`，验证核数切分公式。

**操作步骤**：

1. 取 muls 的 TileShape 为 `Shape<32>`（见 `examples/muls/muls.cpp` 第 26 行 `using TileShape = Atvoss::Shape<WIDTH>`，`WIDTH=32`），故 \(A = \text{ACTUAL\_N\_ASSIGN} = 32\)。
2. 假设用户用 `--shape=100000` 跑 muls，即总元素数 \(N = 100000\)。
3. 假设单核基本块 \(B = \text{BASIC\_CORE\_ELE\_NUM}\) 已知（其值由 `BASIC_BLOCK` 与 TileShape 决定，此处只需知道 \(B\) 已对齐到 32；具体推导留到 u2-l9）。为方便手算，设 \(B = 32 \times k\)，按公式：`totalUnitCnt = 100000 / 32 = 3125`，再按 `basicCoreUnitNum` 与 `blockNum` 的关系算出核数（最终被 56 上限裁剪）。
4. 写出 `unitNumPerCore`、`moreUnitCoreNum`、`tailNum` 三者的值，并指出哪几个核会多处理一个单元。

**需要观察的现象**：`blockNum` 不会超过 56；`tailNum` 一定小于 \(A=32\)（因为它是 `N % 32`）。

**预期结果**：你能复述「`moreUnitCoreNum` 个核各多扛一个对齐单元，最后一个核再额外扛 `tailNum` 个元素」这一分配方式。具体的 `BASIC_BLOCK` 取值对核数结果有影响，精确数值待结合 u2-l9 的 Block 层推导后本地验证。

> 待本地验证：可在 kernel `MakeScheduleConfig` 末尾加一行 `printf` 打印 `blockNum/unitNumPerCore/moreUnitCoreNum/tailNum`（仅用于学习，验证后还原），用 `--shape=100000` 实跑核对。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Block 层 `MakeScheduleConfig` 要比 Kernel 层多一个 `kernelConfig` 参数？

**参考答案**：因为核内 Tile 切分依赖「当前核要处理多少元素」，而这个数量是核间切分（Kernel 层）的结果。把 `kernelParam` 传进来，Block 层才能知道每个核分到了多少工作量，进而决定要切几个 Tile。这是「Block 依赖 Kernel」在函数签名上的直接体现。

**练习 2**：当总元素数 \(N\) 非常小（比如 16，小于 `BASIC_CORE_ELE_NUM`）时，`blockNum` 会是多少？这条捷径在哪段代码里？

**参考答案**：`blockNum = 1`（单核），`tailNum = N`，`unitNumPerCore = moreUnitCoreNum = 0`。捷径在 `schedule.h` 第 72–78 行的 `if (totalEleNum <= BASIC_CORE_ELE_NUM)` 分支：数据量不超过单核基本块时直接单核处理，不再均分。

### 4.3 DeviceTensor 指针封装

#### 4.3.1 概念说明

在 4.1 里我们提到，`PrepareParams` 会把 Host 侧的 `Atvoss::Tensor<T>` 包成 `DeviceTensor<T>`。这两个名字相近的类型职责差别很大：

- **`Atvoss::Tensor<T>`**（u2-l5、`utils/tensor.h`）：**Host 侧**的轻量包装，持有「设备指针 + 形状（最多 8 维）」。形状在这里是必须的，因为 tiling 需要从形状算出总元素数。
- **`DeviceTensor<T>`**（`device/device_tensor.h`）：**Device 侧**的极简包装，**只持有一个裸指针 `T* ptr_`**，不带形状。

为什么 Device 侧不需要形状？因为 tiling 已经在 Host 上算完了。等执行流跨进 NPU 时，每个核只需要知道「从哪个 GM 偏移开始、处理多少元素」，这些都被 tiling 结果（`kernelParam`/`blockParam`）和 kernel 侧现算的偏移覆盖了，张量本身只需提供「数据起点在哪里」。所以 `DeviceTensor` 把形状信息主动丢弃，只留指针。

`DeviceTensor` 还带一个空成员别名 `using IsTensor = void;`，这是个「类型标记」，配合 `IsTensor_v` 让 `TransformArgs` 能在编译期识别「这是一个张量、需要取指针」。

#### 4.3.2 核心流程

`Atvoss::Tensor<T>` 到 `DeviceTensor<T>` 的转换发生在 `DeviceAdapter::ConstructParam` 里，二者通过一个显式构造函数衔接：

```
Atvoss::Tensor<T>（Host，ptr + shape）
        │  DeviceTensor<T>(Atvoss::Tensor<T>& src)   // 显式构造：ptr_ = src.data()
        ▼
DeviceTensor<T>（Device，仅 ptr）
        │  TransformArgs: value.GetPtr()             // 启动 kernel 前还原成裸指针
        ▼
T*（裸设备指针，随 kernel 参数进入 NPU）
```

#### 4.3.3 源码精读

`DeviceTensor` 的全部定义，只有几十行：

[include/elewise/device/device_tensor.h:L19-L53](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/device_tensor.h#L19-L53) —— `DeviceTensor<T>` 只有一个 `T* ptr_` 成员；显式构造函数从 `Atvoss::Tensor<T>&` 取 `src.data()`（第 23–26 行）；`GetPtr()` 返回裸指针（第 45–48 行）；`IsTensor` 标记在第 49 行。

Host 侧 `ConstructParam` 如何用它：

[include/elewise/device/device_adapter.h:L182-L192](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/device_adapter.h#L182-L192) —— 对每个 `PlaceHolder`，用 `ParamType::number - 1` 作下标从 `argTuple` 取实参。张量参数（`ParamType::Type` 是 `DeviceTensor<float>` 这类，非标量）走 `else` 分支，构造 `DeviceTensor<T>(实参)`；标量参数直接拷值。这条 `number - 1` 下标正是 u2-l6 里「`PlaceHolder<N> ↔ inputOutput[N-1]`」契约的兑现点。

`Atvoss::Tensor` 端提供形状与 `data()`：

[include/utils/tensor.h:L53-L68](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/tensor.h#L53-L68) —— `data()` 返回设备指针（第 53–56 行），`shape_vector()` 返回形状的 `std::vector`（第 65–68 行）。前者喂给 `DeviceTensor` 构造，后者喂给 Kernel 层 tiling。

#### 4.3.4 代码实践

**实践目标**：在纸上跟踪 muls 的三个 `PlaceHolder` 各自被包成什么运行期参数对象。

**操作步骤**：

1. 看 muls 的 `MulsCompute`（`examples/muls/muls.cpp` 第 28–38 行）：`PlaceHolder<1, Tensor<TensorDtype>, IN>`（in，张量）、`PlaceHolder<2, ScalarDtype, IN>`（scalar，标量）、`PlaceHolder<3, Tensor<TensorDtype>, OUT>`（out，张量）。
2. 当 `TensorDtype=float`、`ScalarDtype=float` 时，写出 `PrepareParams` 产出的 `params` tuple 中三个元素的类型。
3. 说明 `in` 与 `out` 经过 `ConstructParam` 后变成了什么（提示：`DeviceTensor<float>`，内部 `ptr_` 分别指向 `deviceInput` 与 `deviceOutput`），而 `scalar` 变成了什么（提示：就是一个 `float` 值）。

**需要观察的现象**：张量参数的类型从 `Atvoss::Tensor<float>`「瘦身」成 `DeviceTensor<float>`（丢掉形状、只剩指针），标量参数类型不变。

**预期结果**：`params` 的类型近似 `std::tuple<DeviceTensor<float>, float, DeviceTensor<float>>`，三者的「来源下标」分别是 `argTuple` 的第 0、1、2 位（即 `number-1`）。

> 待本地验证：`params` 的精确类型可在 Host 单测环境里用 `static_assert(std::is_same_v<...>)` 打印验证（不依赖真机）。

#### 4.3.5 小练习与答案

**练习 1**：`DeviceTensor` 的析构函数会释放设备内存吗？为什么这样设计是安全的？

**参考答案**：不会。`DeviceTensor` 只持有裸指针，不拥有内存；真正的设备内存由 Host 侧的 ACL 资源管理（`aclrtMalloc` 申请、RAII 守卫 `aclrtFree` 释放，见 u1-l5）。`DeviceTensor` 只是一个「视图」，所以它不负责、也不应该释放内存，避免双重释放。

**练习 2**：如果 `Compute()` 里声明了一个标量 `PlaceHolder<2, float, IN>`，但运行时 `inputOutput` 的第 2 个实参传了一个 `Atvoss::Tensor<float>`，`ConstructParam` 会走哪个分支？

**参考答案**：会走第 186–188 行的 `if constexpr` 分支（条件：`ParamType::Type` 是标量 `float`，且实参类型是 `Tensor` 特化），构造一个 `DeviceTensor<float>`（把 `Tensor` 的指针取出来）。这是为「标量位却传了张量」这一特殊情形留的兼容路径；正常用法（标量位传标量）走 `else` 分支直接拷值。

### 4.4 Kernel 启动与 workspace

#### 4.4.1 概念说明

第 ③ 步 `LaunchKernelWithDataTuple` 是真正「跨进 NPU」的地方。它做两件事：

1. **参数二分转换**：`TransformArgs` 对 `convertArgs` 里每个元素做分派——标量原样透传，`DeviceTensor` 调 `GetPtr()` 还原成裸设备指针。这一步把「高层的张量对象」全部拍平成「标量或裸指针」，因为 kernel 参数只能接受这些基本类型。
2. **启动 kernel**：用 `KernelCustom<<<blockNum, workspace, stream>>>(cfg, args)` 启动。其中 `blockNum = opParam.kernelParam.blockNum`（核数，来自第 ② 步 tiling），`workspace = nullptr`，`stream` 是用户传入的 ACL 流。

关于 **workspace**：Ascend C 的 kernel 启动语法第二个位置是「动态 workspace 指针」，用于给 kernel 传递一块额外的 Device 内存。ATVOSS 当前传 `nullptr`，即**不使用动态 workspace**——所有 tiling 配置都被打包进 `OpParam cfg`，作为 kernel 的**第一个值参数**直接送进 NPU。所以「tiling 结果如何传到 kernel」的答案是：**作为 `OpParam` 类型的 kernel 参数，按值传递**。

跨进 NPU 后，`KernelCustom` 实例化一个 `KernelOp` 并调它的 `Run(cfg, args...)`。`KernelOp::Run`（即 `KernelBuilder::Run` → `DefaultKernelSchedule::Run`）会消费 `cfg.kernelParam`：先用 `CalCurCoreEleCnt` 算「当前核要处理多少元素」，再用 `CalGMOffset` 算「当前核从 GM 的哪个偏移开始读」，最后把这些下传给 Block 层。

#### 4.4.2 核心流程

从 `convertArgs` 到「每个核真正开始干活」的链路：

```
convertArgs（DeviceTensor / 标量 的 tuple）
  │
  ├─ LaunchKernelWithDataTuple
  │     ├─ std::apply(TransformArgs, convertArgs)   → transformedArgs（标量 / 裸指针 的 tuple）
  │     └─ KernelCustom<<<blockNum, nullptr, stream>>>(opParam, transformedArgs)   ★ 跨进 NPU
  │
  └─ [每个核上] KernelCustom
         ├─ KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_AIV_ONLY)   // 声明是 Vector 核任务
         └─ KernelWrapper → KernelOp op; op.Run(cfg, args...)
                └─ ScheduleClz::Run(cfg, args...)
                       ├─ CalCurCoreEleCnt(cfg.kernelParam)   // 当前核元素数
                       ├─ CalGMOffset(cfg.kernelParam)        // 当前核 GM 偏移
                       └─ BlockOp::Run(configBlock, convertArgs)   // 下沉到 Block 层
```

#### 4.4.3 源码精读

`TransformArgs`——消费端的二分分派（这也解释了 u2-l6 里入口 `static_assert` 为何只允许 Tensor 与标量）：

[include/elewise/device/device_adapter.h:L46-L57](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/device_adapter.h#L46-L57) —— `TransformArgs` 用 `if constexpr` 区分：标量 `std::forward` 原样透传；`Tensor`（含 `DeviceTensor`，靠 `IsTensor_v` 识别）调 `value.GetPtr()` 取裸指针。

`LaunchKernelWithDataTuple`——批量转换 + 启动：

[include/elewise/device/device_adapter.h:L59-L70](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/device_adapter.h#L59-L70) —— 第 63–67 行用 `std::apply` 把 `TransformArgs` 逐个作用到 tuple 每个元素上，得到全是指针/标量的 `transformedArgs`；第 69 行 `KernelCustom<<<blockNum, nullptr, stream>>>(cfg, transformedArgs)` 启动。注意第 69 行第二个位置是 `nullptr`（无 workspace），`blockNum` 取自 `opParam.kernelParam.blockNum`。

`KernelCustom` 与 `KernelWrapper`——跨边界入口：

[include/elewise/device/device_adapter.h:L29-L42](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/device_adapter.h#L29-L42) —— `KernelCustom` 是 `__global__ __aicore__` 的 kernel 入口，第 39 行 `KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_AIV_ONLY)` 声明这是 AIV（Vector）核任务；`KernelWrapper`（第 29–34 行）把 tuple 参数包展开成多个实参，实例化 `KernelOp` 并调 `op.Run(cfg, args...)`。

kernel 侧如何消费 tiling——`DefaultKernelSchedule::Run` 与两个偏移函数：

[include/elewise/kernel/schedule.h:L117-L130](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/schedule.h#L117-L130) —— kernel 侧 `Run` 从 `cfg.blockParam` 取出 Block 配置，用 `CalCurCoreEleCnt(cfg.kernelParam)` 算出当前核元素数并写入 `configBlock.totalElemCnt`（第 121–122 行），再下沉给 `BlockOp::Run`。

[include/elewise/kernel/schedule.h:L140-L151](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/schedule.h#L140-L151) —— `CalCurCoreEleCnt`：`unitNum * unitNumPerCore` 是每核基准量；当前核号 `< moreUnitCoreNum` 则多一个单元；最后一个核再加 `tailNum`。这正是 4.2 节切分公式在「每个核上」的反向兑现。

[include/elewise/kernel/schedule.h:L203-L210](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/schedule.h#L203-L210) —— `CalGMOffset`：根据当前核号、`moreUnitCoreNum`、`unitNumPerCore*unitNum` 算出本核在 GM 上的起始元素偏移，配合 `ConstructParam`（第 159–176 行）把每个张量参数的指针加上 `sizeof(DType)*offset`，得到本核真正要读写的 GM 地址。

把这条链串起来：**tiling 结果 `opParam` 作为 kernel 值参数进入 NPU → 每个核用 `GetBlockIdx()` 配合 `kernelParam` 算出自己的元素数与 GM 偏移 → 下沉给 Block 层切 Tile**。

#### 4.4.4 代码实践

**实践目标**：解释 tiling 结果（`kernelParam`/`blockParam`）是如何被传递到实际 Kernel 的，并回答「workspace 在哪里」。

**操作步骤**：

1. 在 `device_adapter.h` 第 69 行确认启动语句：`KernelCustom<<<blockNum, nullptr, stream>>>(cfg, transformedArgs)`。
2. 回答三个问题：
   - `cfg`（即 `opParam`）是通过什么方式进入 kernel 的？（答：作为 kernel 的第一个**值参数**，按值传递。）
   - workspace 指针是什么？（答：`nullptr`，ATVOSS 不使用动态 workspace。）
   - kernel 内部从哪里读 tiling？（答：从 `cfg.kernelParam` / `cfg.blockParam` 两个字段。）
3. 追踪 kernel 侧：`KernelCustom` → `KernelWrapper` → `KernelOp::Run` → `ScheduleClz::Run` → `CalCurCoreEleCnt`/`CalGMOffset` → `BlockOp::Run`，确认 tiling 信息一路下沉到 Block 层。

**需要观察的现象**：`blockNum`（启动核数）来自 `opParam.kernelParam.blockNum`，而这个字段正是第 ② 步 `CalculateTiling` 里 Kernel 层 `MakeScheduleConfig` 算出来的——闭环。

**预期结果**：你能说清「Host 算好的 tiling 被打包进 `OpParam`，随 kernel 启动按值进入 NPU，每个核再用 `GetBlockIdx()` 与 tiling 字段算出自己的工作量与偏移，最后下传给 Block 层」这一完整路径。

> 待本地验证：workspace 是否真的恒为 `nullptr`，可在 `LaunchKernelWithDataTuple` 第 69 行处加打印确认（仅用于学习，验证后还原）。

#### 4.4.5 小练习与答案

**练习 1**：`LaunchKernelWithDataTuple` 为什么要先用 `TransformArgs` 把 `DeviceTensor` 转成裸指针，而不是直接把 `DeviceTensor` 对象作为 kernel 参数传过去？

**参考答案**：kernel 跨边界启动时，参数需要是可平凡传递的基本类型（标量、指针）。`DeviceTensor` 是带成员函数的 C++ 对象，跨 Host/Device 边界传递对象语义不可靠；而裸指针是确定的基本类型。所以启动前先用 `GetPtr()` 把张量还原成指针、标量原样透传，保证 kernel 拿到的都是基本类型。

**练习 2**：`KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_AIV_ONLY)` 这一行说明这是一个什么样的 kernel？

**参考答案**：`AIV_ONLY` 表示这是一个**只跑在 Vector（AIV）核上**的任务，不涉及 Cube（AIC）核。这与 ATVOSS 的定位一致——它是 Vector 算子模板库，所有算子都跑在 Vector 计算单元上。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「端到端追踪」任务。

**任务**：以 muls 样例（`TensorDtype=float`）为对象，画出从 `deviceOp.Run(arguments, stream)` 到「某个核开始执行 Block 层 `Run`」的完整调用链，并标注每一步用到的 tiling 字段。

**操作步骤**：

1. **Host 侧准备**（对应 4.1）：`Run` 重建表达式得 `Params`，取 `argTuple = std::get<0>(arguments)`，`PrepareParams` 把 `in/scalar/out` 包成 `DeviceTensor<float>/float/DeviceTensor<float>`。
2. **Host 侧 tiling**（对应 4.2）：`CalculateTiling` 先调 Kernel 层 `MakeScheduleConfig` 填 `opParam.kernelParam`（设 `--shape=100000`，推得 `blockNum` 等字段），再调 Block 层 `MakeScheduleConfig`（当前仅打印）。
3. **Host 侧转换 + 启动**（对应 4.3、4.4）：`ConvertArgs` 对齐顺序 → `LaunchKernelWithDataTuple` 用 `TransformArgs` 把张量还原成裸指针 → `KernelCustom<<<blockNum, nullptr, stream>>>(opParam, 指针/标量)`。
4. **NPU 侧消费 tiling**（对应 4.4）：在某个核上，`KernelOp::Run` 用 `GetBlockIdx()` 与 `opParam.kernelParam` 经 `CalCurCoreEleCnt`/`CalGMOffset` 算出本核元素数与 GM 偏移，写入 `configBlock.totalElemCnt`，调 `BlockOp::Run(configBlock, convertArgs)`。
5. 在链路图上用高亮标出三处对 tiling 的使用：① 启动核数 `blockNum`；② 本核元素数 `CalCurCoreEleCnt`；③ 本核 GM 偏移 `CalGMOffset`。

**需要观察的现象 / 预期结果**：

- tiling 只在 Host 算一次，结果打包进 `OpParam`，按值随 kernel 进入 NPU；NPU 侧不再重新 tiling，只是按核号「领取」属于自己的那份。
- `blockParam` 在当前默认实现里没有被 `MakeScheduleConfig` 真正填充，单核的 `wholeLoop`/`tileCnt` 是 kernel 侧 `Run` 时根据 `totalElemCnt` 现算的（进入 Block 层，详见 u2-l9）。
- 全程 workspace 为 `nullptr`。

> 待本地验证：完整链路需真机或 cannsim 运行；若只验证 Host 侧（步骤 1–3），可借助 `--host_ut` 环境，在 `Run` 各步加打印观察字段值。

## 6. 本讲小结

- `DeviceAdapter::Run` 是 Device 层唯一入口，走「重建表达式 → ① PrepareParams → ② CalculateTiling → ③ ConvertArgs + LaunchKernelWithDataTuple」三步，前三步全在 Host，只有 `<<<blockNum>>>` 跨进 NPU。
- `DeviceAdapter` **自己重跑 `Compute<DeviceTensor>()`** 得到 `Params` 列表，不依赖 `arguments` 携带类型信息——同一份声明式表达式在不同层用不同 `Tensor` 模板参数得到不同视图。
- `CalculateTiling` 用一个 `OpParam{kernelParam, blockParam}` **同时承载两层配置**：先算 Kernel（核间，结果被 `CORE_NUM=56` 上限裁剪），再算 Block（核内，且**吃 Kernel 的结果**）。当前默认 Block 层 `MakeScheduleConfig` 仅占位，真正 Tile 切分在 kernel 侧现算。
- `OpParam` 即 `KernelBuilder` 内部的 `{ ScheduleCfg kernelParam; BlockOp::ScheduleCfgClz blockParam; }`，是「两层 tiling 同框」的数据基础。
- `DeviceTensor<T>` 是 Device 侧极简指针包装，从 `Atvoss::Tensor<T>` 显式构造（只取 `data()`、丢弃形状）；`IsTensor` 标记 + `GetPtr()` 配合 `TransformArgs` 做二分分派。
- kernel 启动为 `KernelCustom<<<blockNum, nullptr, stream>>>(opParam, 转换后参数)`：tiling 作为**值参数**进 NPU，workspace 为 `nullptr`，任务类型为 `AIV_ONLY`（Vector 核）；每个核用 `GetBlockIdx()` + `kernelParam` 经 `CalCurCoreEleCnt`/`CalGMOffset` 领取自己的工作量与 GM 偏移。

## 7. 下一步学习建议

本讲把 Device 层「从 `Run` 到启动 kernel」讲透了，并交代了 tiling 如何跨进 NPU。但 kernel 侧 `BlockOp::Run` 之后、单核内部如何切 Tile、如何驱动流水，还留在黑盒里。建议接下来：

- **u2-l8（Kernel 层）**：深入 `DefaultKernelSchedule` 的 `UniformSegment` 均匀分段、`MakeScheduleConfig` 的核数切分细节，以及 `CalCurCoreEleCnt`/`CalGMOffset` 的多核分配数学。
- **u2-l9（Block 层）**：看 `BlockOp::Run` 如何把单核任务切成 `wholeLoop` 个完整 Tile + 1 个尾 Tile，理解 `totalElemCnt` 到 `wholeLoop`/`tileCnt` 的转换与 UB 内存三段划分。
- **u3-l1（求值器系统）**：跨进 NPU 后，表达式如何被 `Evaluator<Expr>` 递归翻译成 Ascend C API——这是 Tile/Basic 两层真正的执行机制。
