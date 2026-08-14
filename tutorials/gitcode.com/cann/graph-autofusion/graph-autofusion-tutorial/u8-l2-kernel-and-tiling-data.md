# u8-l2 Kernel 代码生成、打印与 Tiling 数据

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `codegen_kernel.h` 中「用 C++ 类描述 C++ 代码」的抽象体系：`Code`/`Type`/`Variable`/`Axis`/`Tensor`/`TQue`/`TBuf`/`Tiler`/`TPipe`/`Kernel` 各自描述 kernel 源码的哪一部分。
2. 理解 `CodePrinter` 这类轻量打印器如何把抽象结构落成可编译的 C++ 源码文本。
3. 掌握 `TilingLib` 如何生成 tiling 数据（一堆「文件名 → 文件内容」的映射），以及这些原子头文件的拆分方式。
4. 掌握本次更新的重大改动：CV tiling wrapper 的**复用编译**（编译为可复用 shared object）与 **dtype 感知 CV 融合**（按 `curAivM/curAivN` 二维参数和 dtype 位宽对齐生成 Cast/DataCopy）。
5. 了解 `GenerateForInductor` 的多阶段 PGO 候选稳定化机制：去重、按 default 过滤、TopN 截断。

## 2. 前置知识

- **元编程式代码生成**：Autofuse 的 codegen 不是拼字符串的「大杂烩」，而是先用一组 C++ 类把要生成的 kernel 源码建模成对象（变量、类型、轴、队列），再统一打印成文本。可以类比编译器前端里的 AST——只不过这里的「AST」直接以 C++ 对象的形式存在于生成器进程中。
- **Tiling 与 kernel 的关系**（承接 u3-l2、u7 系列）：ATT 在编译期生成的是「运行期求解器代码」，真正给定 shape 后算出 tiling 参数的是 host 侧的 tiling 函数；kernel 侧代码则按 tiling 参数循环执行。本讲的 `TilingLib` 负责 tiling 函数侧，`Kernel` 负责设备 kernel 侧。
- **CV 融合（Cube-Vector Fusion）**（承接 u6/u7 中的 cube 相关概念）：matmul/conv2d 等 Cube 算子与相邻 Vector 算子融合成一个 kernel。Cube 部分的 tiling 复用 CANN 原生 matmul tiling 实现，通过一个「wrapper」桥接到 Autofuse 的 tiling 流程里。
- **Inductor 后端与 PGO**（承接 u3-l3）：`torch.compile` 的 Inductor 后端走 `GenerateForInductor` 分支；PGO（Profile-Guided Optimization）指先实测各 tiling 候选的性能，再据此挑选最优解的两阶段流程。
- **动态 shape 与符号表达式**（承接 u4/u7）：shape 在编译期未知时，尺寸用 `SizeExpr`/`Expression` 符号表达式表示，运行期才求值。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [autofuse/codegen/codegen_kernel.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_kernel.h) | kernel 源码的抽象体系：`Code`/`Type`/`Variable`/`Axis`/`Tensor`/`TQue`/`TBuf`/`Tiler`/`TPipe`/`Kernel`，是本讲 4.1 的主角 |
| [autofuse/codegen/codegen_kernel.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_kernel.cpp) | 上述抽象的实现，包括 `Kernel::ParseGraph`/`Kernel::Generate` 与 CV 融合的向量函数生成 |
| [autofuse/common/code_printer.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/common/code_printer.h) / [.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/common/code_printer.cpp) | 轻量代码打印器，把拼接好的片段组织成 C++ 源文件 |
| [autofuse/codegen/codegen_tiling.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling.h) | `TilingLib` 类声明：tiling 文件名常量、`MatMulCubeInfo`、tiling 生成入口（含本次新增的 CV/Inductor 辅助方法） |
| [autofuse/codegen/codegen_tiling.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling.cpp) | `TilingLib` 的公共实现：`Generate`/`GenerateForInductor`/`GenerateCVFusion` 与 CV tiling 缓存生成 |
| [autofuse/codegen/codegen_tiling_inductor_topn.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling_inductor_topn.cpp) | Inductor TopN 候选协议与选择器生成（含本次新增的 `FilterMeasuredCandidatesByDefault`） |
| [autofuse/codegen/codegen_tiling_cube_wrapper.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling_cube_wrapper.h) | CV tiling wrapper 的 .hpp/.cpp 源码模板（原始字符串字面量），本次被大幅精简重写 |
| [autofuse/compiler/python/ascendc_compile.py](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascendc_compile.py) | host/device 编译脚本，本次新增 CV wrapper shared object 的缓存复用逻辑 |
| [autofuse/v35/codegen/vec_func_call/vf_loop.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/vec_func_call/vf_loop.cpp) | v35 平台 CV 融合向量循环生成，dtype 感知 stride 的落点之一（辅助引用） |

## 4. 核心概念与源码讲解

### 4.1 kernel 抽象与打印

#### 4.1.1 概念说明

要在编译期生成一个 AscendC kernel 的 C++ 源码，最朴素的做法是拿 `stringstream` 从头拼到尾。但这种做法无法复用、无法组合，也容易拼出非法代码。Autofuse 的选择是：**先用一组 C++ 类把目标 kernel 的每个语法元素建模成对象，最后统一调用 `Str()` 打印成文本**。

这套抽象的根是 `Code`——「任何能打印成一段 C++ 代码的东西」。其余类都直接或间接继承它：

- `Type`/`Variable`：描述类型与变量，是所有「kernel 里有个名字的东西」的基座；
- `Axis`：描述一条循环轴（同时是 ascir 轴和代码变量，多重继承）；
- `Tensor`：描述一个张量在 kernel 里的形态（GM 的还是 UB 的、挂在哪个 queue/buffer 上）；
- `TQue`/`TBuf`：描述 AscendC 的队列/缓冲句柄；
- `Tiler`：描述 tiling 数据在 kernel 侧的视图（怎么从 `TilingData` 里读轴大小、算偏移）；
- `TPipe`：描述整个 kernel 的内存管道（所有 tensor/queue/buf 的容器）；
- `Kernel`：顶层的 kernel 对象，持有 `Tiler`、`TPipe` 和根循环 `Loop`。

#### 4.1.2 核心流程

```text
ascir::ImplGraph + FusedScheduledResult
        │  Kernel::ParseGraph（解析图，建立 Tiler/TPipe/Tensor）
        ▼
   Kernel 对象（抽象的 kernel 源码）
        │  Kernel::Generate（按序打印各部件）
        ▼
   std::string result（kernel 的 C++ 源码文本）
        │  拼进 CodegenResult 的 device 源文件
        ▼
   底层编译器（bisheng）编译为 .o
```

`Kernel::Generate` 内部的打印顺序是固定的：子图函数定义 → tiling key 函数头 → 轴尺寸定义 → Block 外轴定义 → 全局张量初始化 → 局部 tensor/queue/buf 分配 → 根循环体。这个顺序本身就是 kernel 源码的语法顺序。

#### 4.1.3 源码精读

**抽象根与类型系统**——`Code` 只有一个纯虚 `Str()`；`Type` 持有类型名；预定义了常用类型常量：

- [codegen_kernel.h:L23-L42](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_kernel.h#L23-L42)：`Code` 是抽象基类，`Type` 带 `name` 字段，`kVoidT`/`kHalfT`/`kGmAddrT` 等是全局类型常量，任何地方引用同一个类型都共享同一对象。

- [codegen_kernel.h:L44-L74](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_kernel.h#L44-L74)：`Variable` 组合 `Type` 与变量名，提供 `AsArg()`（作为函数参数）、`Define()`（带初始化的定义）、`DefineConst()`（`const` 定义）、`Assign()`（赋值语句）等「语句级」方法；`Int`/`Int64`/`Uint32`/`GM_ADDR` 是常用类型的语法糖。

- [codegen_kernel.h:L76-L95](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_kernel.h#L76-L95)：`Axis` 同时继承 `ascir::Axis`（调度语义）与 `Variable`（代码中的变量），并携带 `loop_size`/`elem_size`/`actual_size`/`tail_size` 等尺寸变量和对应的符号表达式 `SizeExpr`，`IsOuter()`/`IsInner()` 判断它是 Block 外轴还是内轴——这是「一份图数据、两种视图」在 codegen 侧的体现。

- [codegen_kernel.h:L97-L174](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_kernel.h#L97-L174)：`Tensor` 是信息量最大的抽象：既包含 ASCIR 侧的 `id`/`reuse_id`/`dtype`/`axis`/`axis_strides`，又包含 kernel 侧的 `que_id`/`buf_id`/`size`/`que_depth`；`SetGlobalBuffer()` 生成 GM 张量绑地址的语句，`GetTensorSize()` 用符号乘法算出元素总数。注意 `is_ub_scalar` 等标志位——标量走 UB 的特殊优化也建模在这里。

- [codegen_kernel.h:L192-L233](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_kernel.h#L192-L233)：`TQue`/`TBuf` 分别对应 AscendC 的 `TPipe` 队列与缓冲，`AllocBuf()`/`EnqueBuf()`/`DequeBuf()`/`FreeBuf()` 直接生成对应调用语句。注意 `TQue` 上的 `is_cv_ub_fusion` 与 `skip_init_for_simt_direct_gm` 两个标志——前者标记该队列参与 CV UB 融合（u7-l1 讲过它会触发 NDDMA legacy 模型回退），后者服务 IndirectLoad SIMT 直访 GM 场景。

- [codegen_kernel.h:L235-L359](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_kernel.h#L235-L359)：`Tiler` 负责「从 TilingData 里取值」——`Size()`/`Offset()`/`AxisSize()` 把符号表达式翻译成 `t.xxx` 形式的取值表达式；`TPipe` 是所有 `Tensor`/`TQue`/`TBuf` 的容器，其 `cv_fusion_type`（第 301 行）与 `is_inductor`（第 302 行）字段记录当前 kernel 是否处于 CV 融合 / Inductor 场景，会影响后续生成分支。

- [codegen_kernel.h:L384-L411](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_kernel.h#L384-L411)：`Kernel` 顶层类持有 `tiler`、`tpipe`、`root_loop` 三大件；`ParseGraph()` 从调度结果建立整套抽象，`Generate()` 落成文本。第 450-453 行的 `GenerateVecFuncOfCVFusion()`/`InitCVFusionAddr()` 是 CV 融合专属入口（见 4.3）。

**Kernel::Generate 的打印顺序**：

- [codegen_kernel.cpp:L2406-L2451](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_kernel.cpp#L2406-L2451)：先跳过纯 Cube 图（Cube kernel 走别的路径）；然后依次拼接子图函数定义、轴尺寸（`GenAxisSizeNew`，含尾块处理）、Block 外轴定义、`GlobalTensorInit`、`LocalTensorQueBufAlloc`、根循环体；最后在开启编译期 dump 时把 api 参数落盘（`DumpGraphApiParams`，这就是 u3-l3 提到的 DFX 产物之一）。

**CodePrinter 打印器**：

- [code_printer.h:L19-L52](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/common/code_printer.h#L19-L52)：`CodePrinter` 只有一个 `std::stringstream output_` 成员，提供 `AddInclude`/`AddNamespaceBegin`/`DefineClassBegin`/`DefineFuncBegin` 等结构性接口，最后 `GetOutputStr()` 取全文或 `SaveToFile()` 落盘。
- [code_printer.cpp:L15-L52](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/common/code_printer.cpp#L15-L52)：每个方法只是一两行流式输出，例如 `AddInclude` 输出 `#include "xxx"`。它不理解 C++ 语法，只负责「行级」排版。

需要特别指出：**kernel 生成的主路径并不用 `CodePrinter`，而是直接 `stringstream` + 各抽象类的 `Str()`**；`CodePrinter` 的主要用户在 ATT generator 侧（`tiling_code_gen_impl.cpp`、`tiling_cache_code_gen.cpp` 等，见 u7-l3）。两者是同一哲学（结构化片段 → 文本）的两种粒度实现。文件末尾的 `operator<<(std::ostream &, const Code &)`（[codegen_kernel.h:L574](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_kernel.h#L574)）让任何 `Code` 对象都能直接流式输出，是把「抽象 → 文本」的转换收敛到 `Str()` 一处的关键。

#### 4.1.4 代码实践

**实践目标**：亲手验证「抽象类 → 文本」的映射关系。

**操作步骤**（源码阅读型实践，无需 NPU 环境）：

1. 在 `autofuse/codegen/` 下执行 `grep -n "Variable::Variable\|Variable::Str\|Variable::Define" codegen_kernel.cpp`，定位 `Variable` 各方法的实现，阅读 `Define()` 是如何拼出 `type name = init;` 的。
2. 阅读上面的 [codegen_kernel.cpp:L2406-L2451](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_kernel.cpp#L2406-L2451)，把 `Kernel::Generate` 拼接的 6 个段落列成表格。
3. 对照一个真实产物：如果你本地跑过 u3-l3 的 `af_add_ge.py` 且开了 `TORCH_COMPILE_DEBUG`，打开 `torch_compile_debug/autofused_*/` 下的设备源码，找到「轴定义 → SetGlobalBuffer → LocalTensor … EnQue → for 循环」的段落边界，与步骤 2 的表格一一对应。没有本地环境则跳过本步。

**需要观察的现象**：生成的 kernel 源码的语句顺序与 `Kernel::Generate` 的调用顺序完全一致；变量声明总是 `int32_t xxx = t.xxx;` 这种「tiling 取值 + 局部变量」的形态。

**预期结果**：能指出 kernel 源码中任意一段由哪个抽象类的哪个方法生成（步骤 3 待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Axis` 要同时继承 `ascir::Axis` 和 `Variable`？

**答案**：因为一条轴既是调度语义对象（属于 ASCIR 图，携带轴类型、分裂关系，被 optimize/att 消费），又是 kernel 源码里的一个循环变量（需要类型和变量名参与代码生成）。多重继承让「同一份轴数据」在两个阶段以两个视角被访问，避免拷贝或转换——这与 u4-l2 讲的「全链路只有一份图数据」原则一致。

**练习 2**：`CodePrinter` 与 `Variable::Str()` 分别解决什么粒度的问题？

**答案**：`Variable::Str()` 等 `Code::Str()` 实现 解决「一个语法元素如何变成文本」的细粒度问题；`CodePrinter` 解决「一个源文件由哪些结构（include、namespace、函数）按什么顺序组成」的文件级问题。前者在 codegen 主路径使用，后者主要在 ATT tiling 代码生成中使用。

**练习 3**：`TPipe::cv_fusion_type` 与 `TQue::is_cv_ub_fusion` 有什么区别？

**答案**：`TPipe::cv_fusion_type` 是整个 kernel 级的 CV 融合模板类型（记录这是哪种 Cube-Vector 融合形态，决定生成哪些专属函数）；`TQue::is_cv_ub_fusion` 是单个队列级的标志（标记该队列的内存被 CV UB 融合复用，会影响 tiling 建模——u7-l1 讲过 kUBFuse 场景会回退 legacy NDDMA 模型）。

### 4.2 tiling 数据生成

#### 4.2.1 概念说明

u7-l3 讲了 ATT 侧如何生成 tiling 求解代码；本讲从 codegen 侧看同一产物的另一个视角：**`TilingLib` 把 ATT 生成的原子代码片段组装成一组「文件名 → 文件内容」的映射**（`std::map<std::string, std::string>`），交由下游 host 编译流程落盘并编译。产物不是一个大文件，而是一个入口翻译单元加若干原子头文件（State/Log/Pgo/Solver/Api 等），这样 main tiling 逻辑变化时只需重编入口，原子头可以按内容缓存。

`TilingLib` 还承担「平台特化 tiling 函数」的动态加载：构造函数 `dlopen` 一个外部 so，取出 `TilingLibCodegenFunc` 类型的生成函数——这为 v35 等平台用自己的 tiling 生成逻辑覆盖默认实现留了口子。

#### 4.2.2 核心流程

```text
FusedScheduledResult
   │
   ├─ Generate()            ──→ 普通（GE/TF）场景：TilingFuncDef → GetTilingHeaders → 组装入口翻译单元
   ├─ GenerateCVFusion()    ──→ Cube 融合场景：静态/动态分支 + 注入 cube wrapper 文件
   └─ GenerateForInductor() ──→ Inductor 场景：TilingFuncDefForInductor + TopN 源 + (PGO)
   │
   ▼
tiling_file_name_to_content: map<文件名, 文件内容>
```

三个入口共享 `GetTilingHeaders()` 产出的原子头文件集合；区别在入口翻译单元的拼装方式。

#### 4.2.3 源码精读

- [codegen_tiling.h:L19-L46](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling.h#L19-L46)：一组文件名常量定义了产物结构：`TilingHead`（公共头）、`TilingStateHeader`/`TilingLogHeader`/`TilingPgoHeader`/`TilingBaseHeader`/`TilingSolverHeader`/`TilingApiHeader`（六个原子头，与 u7-l3 讲的五头一 common 对应，本版本细化为更多原子件）、`TilingData`、以及 CV 场景的 `ACubeKernelTilingWrapperHpp`/`BCubeKernelTilingWrapperCpp`。

- [codegen_tiling.cpp:L444-L460](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling.cpp#L444-L460)：`TilingLib` 构造函数 `dlopen` 平台 so 并 `dlsym` 取 `codegen_func_`，失败则保持 `nullptr` 走默认生成逻辑——平台覆盖是「可选」而非「必须」。

- [codegen_tiling.cpp:L462-L465](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling.cpp#L462-L465)：`ShouldFallbackPgo()`——tiling key 数量超过 `kMaxPgoTilingKeyCount`（10000）时 PGO 搜索空间爆炸，回退非 PGO 生成。这是 PGO 路径的第一道护栏。

- [codegen_tiling.cpp:L574-L589](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling.cpp#L574-L589)：`Generate()` 主入口——先做 PGO 回退判断，再判断是否 Cube 融合（是则转 `GenerateCVFusion`），否则走普通 `GetTilingHeaders` + 入口组装。

- [codegen_tiling.cpp:L530-L572](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling.cpp#L530-L572)：`GenerateCVFusion()`——按静态/动态 shape 分叉到 `GenerateCVFusionStatic/Dynamic`；关键动作是第 568-570 行：把 wrapper 的 .hpp/.cpp 内容注入 `tiling_file_name_to_content`，即 **cube tiling wrapper 也是一份 codegen 产物文件**（这一点是理解 4.3 复用编译的前提）。静态分支不注入（静态 shape 时 wrapper 逻辑被内联进主 tiling）。

- [codegen_tiling.cpp:L467-L507](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling.cpp#L467-L507)：`GenerateForInductor()`——Inductor 专属入口。三个要点：① PGO 场景仅支持「静态 + 非 CV」kernel（`IsSupportedInductorPgoScene`，第 505-507 行）；② 非 Cube 场景追加 `GenInductorTopnSources`（TopN 候选协议源码，见 4.4）；③ 第 492-496 行同样注入 cube wrapper 文件（Inductor 的 CV 融合动态场景）。第 497-500 行把入口体交给 `RenderEntryTranslationUnit` 渲染成最终翻译单元。

#### 4.2.4 代码实践

**实践目标**：数清一次 tiling 生成到底产出哪些「虚拟文件」。

**操作步骤**（源码阅读型实践）：

1. 打开 [codegen_tiling.h:L19-L46](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling.h#L19-L46)，把所有 `kTiling*Identify`/`kCube*` 常量抄成一张表。
2. 在 `autofuse/tests/ut/codegen/test_codegen_tiling.cpp` 中搜索 `tiling_file_name_to_content`，阅读 UT 如何断言产物文件的存在性（例如 `GenerateForInductorCvFusionShouldEmitCvTilingAndCubeWrapper` 用例断言 CV 融合会产出 wrapper 文件）。
3. 回答：普通非 CV 的 Inductor 场景会比 CV 场景少哪些 key？（提示：对比 L487-L496 的两个 if 分支）

**需要观察的现象**：UT 中对 map key 的断言清单就是产物的权威清单。

**预期结果**：非 CV Inductor 场景没有 `ACubeKernelTilingWrapperHpp`/`BCubeKernelTilingWrapperCpp` 两个 key；`GenInductorTopnSources` 只在非 CV 分支调用。

#### 4.2.5 小练习与答案

**练习 1**：为什么 tiling 产物要拆成一个入口翻译单元 + 多个原子头，而不是单文件？

**答案**：入口翻译单元包含随图变化的 tiling 主逻辑，必须每次重编；原子头（State/Log/Api 等）内容相对稳定，拆开后可按内容缓存跳过重编。这正是 4.3 把 wrapper 进一步抽成独立 so 的思想延续——粒度越细，可缓存的比例越高。

**练习 2**：`ShouldFallbackPgo` 防的是什么风险？

**答案**：PGO 需要枚举 tiling key 组合并逐一实测，组合数随轴数和取值空间组合增长；超过 10000 个 key 时搜索代价不可接受，因此编译期先估算 key 数（`TryCalcTilingKeyCount`），超限即回退到非 PGO 的普通生成路径。

### 4.3 cv tiling wrapper 复用编译与 dtype 感知融合

#### 4.3.1 概念说明

本模块覆盖本次更新（commit `1116eaa4`）的两项重大改动。

**问题一：CV tiling wrapper 重复编译。** Inductor CV fusion 场景下，codegen 会产出主 tiling 源文件和 cube tiling wrapper 源文件两份产物。wrapper 的实现（`AutofuseDoCubeMatMulTiling` 等，本质是把 CANN 原生 matmul tiling 桥接进 Autofuse）相对稳定，不随图和 shape 变化；但原流程把它当普通 host 源文件，每次编译都重新参与 host 编译，白白增加编译耗时。**解法**：把 wrapper 单独编译为可复用的 shared object（`libautofuse_cv_tiling_wrapper_<hash>.so`），用内容相关的 cache key 缓存，相同输入只编一次。

**问题二：CV 融合的位宽/精度转换不 dtype 感知。** CV fusion 里 Cast、RoundToInt、TruncToInt、FloorToInt 等位宽转换算子要处理 fp16/bf16/fp32/int32 等多种精度组合。旧代码复用普通向量路径的一维 `actual_size` 和默认 stride，没有按 CV stage 的二维 `curAivM/curAivN` 组织参数，也没有按 dtype 位宽做块对齐——低位宽与高位宽数据在 DataCopy、RemovePad/GatherMask 上语义不一致，影响精度与访存正确性。**解法**：补齐 dtype 感知的 dims/stride 生成：统一按 `curAivM/curAivN` 二维生成 Cast 参数，按 tensor dtype 计算 block-aligned 的 N 方向 stride，并区分 UBFuse 与 fallback 两条路径处理 DataCopy。

**触发条件**（dtype 感知 CV 融合什么时候生效）：

1. 图被判定为 Cube 融合调度（`IsCubeFusedScheduled`，即 matmul/conv2d 与 Vector 算子融合）；
2. kernel 中存在位宽转换类算子（Cast/RoundToInt/TruncToInt/FloorToInt 等）；
3. Python 编译侧以 `is_cv_fusion_compile()` 判定——任一待编译源文件内容含 `CVAutofuseTilingData` 字符串即为 CV 编译，才启用 shared wrapper 与 `nnopbase` 链接库，非 CV 路径零额外开销。

#### 4.3.2 核心流程

```text
Python 编译侧（ascendc_compile.py）
   host_files ──→ is_cv_fusion_compile? ──否──→ 原流程（全量 host 编译）
        │是
        ├─ is_cv_wrapper_source 挑出 wrapper 源文件
        ├─ ensure_shared_cv_wrapper_so:
        │     cache key = sha256(wrapper源码 ‖ CANN路径 ‖ 架构 ‖ SoC ‖ 编译选项 ‖ stage)
        │     命中 → 直接复用 so；未命中 → 文件锁保护首次编译 → 原子替换
        └─ 链接时 append_shared_cv_wrapper_so 追加共享 so（仅 CV 编译）

C++ codegen 侧（codegen_tiling.cpp）
   GenTilingFuncForInductor
     ├─ GenInductorShapeDim      （动态 shape 变量 → 形参/实参/tiling 赋值三件套）
     ├─ GenCallCubeTilingForInductor （CV 场景：CallCubeTiling + 结果缓存）
     └─ GenPlainInductorTilingTail （非 CV 场景：普通 tiling 收尾 + PGO 分支）
```

#### 4.3.3 源码精读

**wrapper 源码模板的精简重写**：

- [codegen_tiling_cube_wrapper.h:L3-L113](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling_cube_wrapper.h#L3-L113)：`.hpp` 模板只剩纯接口：`TensorInfo`/`AttrInfo`/`CompileInfo`/`TilingResult` 四个 POD 结构和 `AutofuseDoCubeMatMulTiling` C 接口声明（第 82-87 行）。对比上一版本（约 800 行、内嵌完整 JSON 解析器与 acl 依赖），本次把实现细节全部移入 `.cpp` 模板并精简依赖——头文件变薄意味着以它为接口的编译单元更稳定，**这正是「wrapper 可以抽成共享 so」的前提**：接口不变，实现单独编译也不破坏 ABI。

- [codegen_tiling_cube_wrapper.h:L115-L140](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling_cube_wrapper.h#L115-L140)：`.cpp` 模板改为直接包含 CANN 的 op_tiling context builder、platform_info、op_impl_kernel_registry 等头（不再自带 JSON/ACL 桥接），把 matmul tiling 委托给 CANN 原生注册实现。

**C++ 侧的重构**（把 `GenTilingFuncForInductor` 里 160 行内联逻辑拆成命名函数）：

- [codegen_tiling.h:L201-L210](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling.h#L201-L210)：本次新增的四个方法声明——`GenInductorShapeDim`、`GenCallCubeTilingForInductor`、`GenCallCubeTilingCacheRead/Write`、`GenPlainInductorTilingTail`。同时 [codegen_tiling.h:L53-L54](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling.h#L53-L54) 给 `MatMulCubeInfo` 补了 `has_bias`/`has_offset_w` 字段，配套在 [codegen_tiling_cube.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling_cube.cpp) 中提取并作为 `autofuse_has_bias`/`autofuse_has_offset_w` 属性传给 CANN tiling——让 wrapper 侧能感知 bias/offset_w 分支。

- [codegen_tiling.cpp:L1130-L1142](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling.cpp#L1130-L1142)：`GenInductorShapeDim` 把每个非常量 `origin_var` 生成三份代码：形参定义（`uint32_t m, `）、实参使用（`m, `）、tiling 赋值（写入 `PgoShapeStringStream` 的三个流）。这是动态 shape 的标准「三件套」。

- [codegen_tiling.cpp:L1144-L1211](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling.cpp#L1144-L1211)：`GenCallCubeTilingForInductor` 生成 `CallCubeTiling` 函数，内部先 `GenCallCubeTilingCacheRead`（静态变量缓存上次 shape 与 tiling 结果，命中直接 memcpy 返回），未命中走 `ProcessCubeKernelTilingFromFusedResult`（真正调 wrapper），最后 `GenCallCubeTilingCacheWrite` 回填缓存。相比旧版「sizeof 固定长度拷贝」，新版记录 `copy_size` 精确拷贝 tiling bytes。**注意这是运行期 tiling 结果缓存（同 shape 免重算），与 Python 侧的编译期 so 缓存是两层不同的缓存。**

- [codegen_tiling.cpp:L1213-L1235](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling.cpp#L1213-L1235)：`GenPlainInductorTilingTail` 生成非 CV 场景的 tiling 收尾（设 block_dim/ub_size、调 `optiling::GetTiling`、算 workspace），并按 `enable_autofuse_pgo_` 分叉出 PGO 版或普通版 `AutofuseTilingWithConfig`。

**Python 侧的共享 so 复用**：

- [ascendc_compile.py:L499-L510](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascendc_compile.py#L499-L510)：`is_cv_fusion_compile()` 的判定方式非常直白——扫描所有待编译源文件，内容含 `CVAutofuseTilingData` 即为 CV 编译。

- [ascendc_compile.py:L207-L211](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascendc_compile.py#L207-L211)：`is_cv_wrapper_source()` 识别两类 wrapper 源文件：未拆分的 `cube_kernel_tiling_wrapper.cpp` 与拆分后的 `*_tiling_func_BCubeKernelTilingWrapperCpp.cpp`（即 4.2 讲的 map key 落成的文件名）。

- [ascendc_compile.py:L233-L242](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascendc_compile.py#L233-L242)：`get_shared_cv_wrapper_so_path()` 计算 cache key——对 wrapper 源码字节、CANN 路径、机器架构、SoC、编译选项、stage 逐项喂 sha256，取前 16 位十六进制作 so 文件名。**凡是可能影响 wrapper ABI 或编译结果的输入都进了 key**，保证复用不牺牲正确性。

- [ascendc_compile.py:L257-L289](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascendc_compile.py#L257-L289)：`build_shared_cv_wrapper_so()` 先编到临时文件（带 pid + 时间戳后缀）再 `os.replace` 原子替换；`ensure_shared_cv_wrapper_so()` 用 `fcntl.flock` 文件锁串行化并发首次编译——多个编译进程命中同一 key 时只有一个真正构建，其余等锁后直接复用，避免部分写入。

- [ascendc_compile.py:L292-L313](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascendc_compile.py#L292-L313)：`append_shared_cv_wrapper_so()` 与 `prepare_shared_cv_wrapper()` 都以 `is_cv_fusion_compile` 为门禁——非 CV 编译不拆 wrapper、不追加 so、不切换 `nnopbase` 链接库，普通路径零负担；同时 `shared_cv_wrapper_so` 状态只存在于 CV 编译的参数对象上，杜绝残留状态污染非 CV 链接。

**dtype 感知的落点（v35 侧）**：

- [vf_loop.cpp:L97-L112](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/vec_func_call/vf_loop.cpp#L97-L112)：CV 融合向量循环的尺寸参数统一为 `curAivM, curAivN, curAlignN` 三元组；当需要按 dtype 对齐时返回 `KernelUtils::BlkAlign<dtype_name>(curAivN)`——即 **N 方向 stride 不再默认，而是按当前张量 dtype 做 32 字节块对齐**。上位概念在 4.1 的 `Tensor::DtypeName`（把 `ge::DataType` 翻译成模板参数名）。

#### 4.3.4 代码实践

**实践目标**：对比 cv tiling wrapper 复用编译前后的差异，说清 dtype 感知融合的触发条件。

**操作步骤**：

1. 在仓库根目录执行：
   ```bash
   git show 1116eaa4 --stat -- autofuse/codegen/codegen_tiling_cube_wrapper.h
   git show 1116eaa4 -- autofuse/codegen/codegen_tiling_cube_wrapper.h | head -120
   ```
   观察 `.hpp` 模板从一个约 800 行、内嵌 JSON/ACL 的实现瘦身为约 100 行纯接口。
2. 执行 `git show 1116eaa4 -- autofuse/codegen/codegen_tiling.cpp | grep -c "^-"` 与 `grep -c "^+"`，确认这次是「拆函数 + 加缓存」的重构而非逻辑推翻。
3. 阅读上方引用的 [ascendc_compile.py:L233-L289](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascendc_compile.py#L233-L289)，列出 cache key 的全部输入项。
4. 查看 UT 佐证：`autofuse/tests/ut/python/test_ascendc_compile.py` 中本次新增了 `test_ensure_shared_cv_wrapper_so_reuses_existing_so`、`test_ensure_shared_cv_wrapper_so_serializes_concurrent_first_compile` 等用例（用 `grep -n "def test_.*cv_wrapper" autofuse/tests/ut/python/test_ascendc_compile.py` 定位），从断言反推行为。

**需要观察的现象**：wrapper so 只在首次编译某 cache key 时生成；同一缓存根目录下重复编译同一 CV 图，第二次不再出现 wrapper 的 host 编译日志。

**预期结果**：能口头回答「wrapper 复用的正确性由什么保证」（cache key 覆盖全部 ABI 相关输入 + 文件锁 + 原子替换）。步骤 4 的运行验证需要本地 Python UT 环境（`sh build.sh -m autofuse_framework -i py -u` 可跑 python UT），具体输出待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 wrapper 要编译成 shared object 而不是静态库或普通 object？

**答案**：普通 object 仍需参与每次 host 链接且无法跨编译任务复用；shared object 以文件为缓存单元，命中即免编译免链接，还能通过 `-Wl,-soname` 与 rpath 让主 tiling so 在运行期正确找到它。同时 CV 专属依赖（`nnopbase`）被封装进 wrapper so 的链接选项，不污染非 CV 路径。

**练习 2**：dtype 感知融合里 `KernelUtils::BlkAlign<dtype>(curAivN)` 解决什么问题？不用它会有什么后果？

**答案**：它把 CV stage N 方向的长度按当前 dtype 的 32 字节块对齐换算成实际可访存的 stride。不用它时，fp16 与 int32 共用同一个默认 stride，低位宽（1/2 字节）数据会按高位宽的步长搬数，导致 DataCopy 越界或读错元素，Cast/GatherMask 去 padding 的边界也随之处错——体现为精度错误或访存非法。

**练习 3**：本小节出现了「两层缓存」，分别缓存什么、在哪一层生效？

**答案**：编译期缓存（Python 侧 `cv_tiling_wrapper_cache` 目录下的共享 so）缓存的是 wrapper 的**编译产物**，省 host 编译时间；运行期缓存（`GenCallCubeTilingCacheRead/Write` 生成的静态变量）缓存的是 **tiling 计算结果**，同 shape 下省去重复调 CANN matmul tiling 的开销。二者一个在编译流水线里，一个在生成的 tiling 函数里。

### 4.4 PGO/Inductor 后端与多阶段候选稳定化

#### 4.4.1 概念说明

`GenerateForInductor` 与普通 `Generate` 的根本区别：Inductor（torch.compile）场景下，Autofuse 拿不到框架的 shape 先验，tiling 选择被设计为**两阶段**：

1. **modeled 阶段**：按 ATT 成本模型（u7-l1）给每个 tiling 候选打分，产出 TopN 候选集；
2. **measured 阶段（PGO）**：用生成的独立 runner 在真实 NPU 上逐一实测 TopN 候选，按实测性能重排，最终把最优 tiling 固化。

「多阶段」指多个调度 group 逐个搜索时，前一 group 已确定的候选（前缀）不能被后一 group 的搜索动摇。本次更新（commit `b0b60285`）修复两个稳定性问题：

- 多 group PGO 搜索当前 group 时不再重算之前 group 的 tiling，已定前缀保持稳定；
- measured 候选中按 `canonical_repr` 去重（重复者保留实测更优者），并**过滤掉性能不快于 default 的非 default 候选**——这些候选既不该进最终 TopN，也不该进 `search.txt` 导出。

#### 4.4.2 核心流程

```text
GenInductorTopnSources
   ├─ 生成 CandidateSolution 协议（tiling_data + modeled_perf + canonical_repr + is_default）
   ├─ modeled 路径: GenTopnSelectorHelpersForInductor
   │     排序键 = (is_default 优先, modeled_perf 升序, canonical_repr 字典序) → 去重 → 截断 TopN
   └─ measured 路径: GenMeasuredTopnSelectorHelpersForInductor
         去重(保留实测更优) → 按实测 perf 排序
       → FilterMeasuredCandidatesByDefault   ← 本次新增
         (删掉所有 !is_default 且 perf ≥ default_perf 的候选)
       → 导出 measured_candidates(search.txt 与 TopN 共享同一份)
       → default 兜底位旋到第 2 位 → 截断 TopN
```

`is_default` 候选是「不调 solver、直接用默认 tiling」的保底解：若所有实测候选都不比它快，最终就退回默认，保证 PGO 永远不会劣于基线。

#### 4.4.3 源码精读

- [codegen_tiling_inductor_topn.cpp:L31-L56](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling_inductor_topn.cpp#L31-L56)：`GenInductorTopnSources` 入口——按 `enable_autofuse_pgo_` 决定生成 measured 版还是 modeled 兜底版（`GenModeledFallbackTopnForInductor`）的 `GetTopNSolutions` 函数源码。

- [codegen_tiling_inductor_topn.cpp:L793-L830](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling_inductor_topn.cpp#L793-L830)：modeled 选择器——`CompareCandidateSolution` 是三级排序键（default 优先 → 建模性能升序 → canonical 表示字典序），`SelectTopnCandidateSolutions` 做「去重 → 排序 → 日志 → 截断」。注意排序比较器以 `canonical_repr` 收尾：符号表达式相等的两个候选字典序一致，保证**生成结果跨次编译确定**（这是项目「图改写确定性」红线在 codegen 侧的延续）。

- [codegen_tiling_inductor_topn.cpp:L832-L846](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling_inductor_topn.cpp#L832-L846)：**本次新增**的 `GenMeasuredCandidateDefaultFilter`——生成 `FilterMeasuredCandidatesByDefault` 函数：先找 `is_default` 的解取其 `modeled_perf` 作门槛，再用 `std::remove_if` 删掉所有「非 default 且实测性能不优于门槛」的候选。没有 default 解时原样返回。

- [codegen_tiling_inductor_topn.cpp:L848-L887](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling_inductor_topn.cpp#L848-L887)：measured 选择器全貌——顺序为去重（L859）→ 注册 default 过滤器（L860）→ 选择器内「去重 → 排序 → 过滤」（L863-L865）→ 把过滤后的集合同时写入 `measured_candidates` 导出与 TopN 返回（L866-L873，二者共享同一份去重过滤后的候选，`search.txt` 与最终结果不再互相矛盾）→ default 旋到位置 1 作兜底（L874-L880）→ 截断（L881-L883）。

- 与普通生成的对照（[codegen_tiling.cpp:L467-L507](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling.cpp#L467-L507)）：普通 `Generate` 产出的是「编译期就定好求解逻辑」的 tiling 函数；`GenerateForInductor` 额外产出 TopN 候选协议与（开启 PGO 时）独立 runner/proxy 的整套源码，把「选哪个 tiling」从编译期推迟到首轮运行期实测。

#### 4.4.4 代码实践

**实践目标**：读懂 measured TopN 选择器的生成逻辑，并验证 UT 覆盖。

**操作步骤**：

1. 精读 [codegen_tiling_inductor_topn.cpp:L832-L887](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling_inductor_topn.cpp#L832-L887)，手工模拟一个输入集：`{A(default, perf=10), B(perf=8), C(perf=12), D(perf=8)}`，写出过滤 + 旋转 + 截断（topn=3）后的序列。
2. 查看 `autofuse/tests/ut/codegen/test_codegen_tiling.cpp` 中本次新增的用例（`git show b0b60285 -- autofuse/tests/ut/codegen/test_codegen_tiling.cpp`），对照你的手算结果与 UT 断言。
3. 若想本地复跑：`sh build.sh -m autofuse_framework -i cpp -u`（需要本地构建环境，待本地验证）。

**需要观察的现象**：步骤 1 手算结果应为 `{B, A, ...}`——C（比 default 慢）被过滤，A 保留并旋转到第 2 位兜底；D 与 B 的 `canonical_repr` 若相同则在去重阶段只剩实测更优者。

**预期结果**：能解释「为什么 measured 候选必须不快于 default 就删」：default 是无 PGO 时的行为基线，留下更慢的候选只会污染 TopN 与 search.txt，还可能被选中导致 PGO 劣化。

#### 4.4.5 小练习与答案

**练习 1**：modeled 与 measured 两条路径的排序键有何不同？为什么？

**答案**：modeled 按「default 优先 → 建模性能 → canonical_repr」，因为此阶段尚未实测，default 作为最可信的兜底排最前；measured 按「实测性能 → canonical_repr」，default 不再特殊排序，而是在过滤后单独旋转到位置 1 作兜底——实测数据已可比，default 只承担保底职责。

**练习 2**：`canonical_repr` 在整套机制里扮演什么角色？

**答案**：它是候选 tiling 的规范化字符串表示，身兼三职：去重键（表示相同即同一解）、排序末级 tiebreak（保证确定性）、日志/导出内容（search.txt 里人类可读）。u7-l3 讲过 ATT 侧生成 repr 的代码（`GenReprSingleGroup/MultiGroup`），本讲的选择器是它的消费端。

**练习 3**：为什么 `measured_candidates` 导出要和返回 TopN 共享同一份过滤后的集合？

**答案**：若两边各过滤各的，`search.txt` 里可能保留已被淘汰的候选，而返回集合没有——后续按 search.txt 复盘或离线分析时会得出与线上不同的候选集，排查 PGO 劣化问题时对不上账。共享一份是让「导出的账本」与「实际用的结果」一致。

## 5. 综合实践

**任务：给一次 CV 融合编译画出完整产物与缓存清单。**

假设一个 Inductor CV fusion 场景（matmul + cast + elementwise，动态 shape，未开 PGO）：

1. **codegen 产物清单**：从 [codegen_tiling.cpp:L467-L503](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling.cpp#L467-L503) 出发，列出 `tiling_file_name_to_content` 中会出现哪些 key（提示：入口 + 原子头 + 两个 wrapper 文件；CV 场景没有 TopN 源）。
2. **运行期缓存**：说明 [codegen_tiling.cpp:L1144-L1211](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/codegen/codegen_tiling.cpp#L1144-L1211) 生成的 `CallCubeTiling` 在两次相同 shape 调用之间的行为差异。
3. **编译期缓存**：说明 [ascendc_compile.py:L299-L313](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/compiler/python/ascendc_compile.py#L299-L313) 会把哪个产物文件从 host 编译列表里剥离、编成什么、放在哪个目录。
4. **dtype 感知**：指出该场景中 cast 算子的 N 方向 stride 由哪段代码决定（[vf_loop.cpp:L97-L112](https://github.com/gitcode.com/cann/graph-autofusion/blob/2b9c5c2a85a249e6407b8d1b845aebe7befd937c/autofuse/v35/codegen/vec_func_call/vf_loop.cpp#L97-L112)），以及为什么 fp16 与 int32 不能共用。
5. 把以上四点整理成一页「CV 编译产物与缓存」笔记，标注每条结论的源码永久链接。

预期产出：一张包含「产物文件 → 生成者 → 缓存层（无/编译期/运行期）→ 消费者」四列的表格。全部步骤为源码阅读型，无需 NPU 环境。

## 6. 本讲小结

- codegen 用「C++ 类描述 C++ 代码」的抽象体系生成 kernel：`Code` 是可打印根，`Variable/Axis/Tensor/TQue/TBuf` 建模语法元素，`Tiler/TPipe/Kernel` 组装成完整 kernel；`Kernel::Generate` 的固定打印顺序就是 kernel 源码的语法顺序。
- `CodePrinter` 是文件级排版器（主要服务 ATT 侧），与元素级 `Str()` 互补；`operator<<` 把「抽象→文本」收敛到一处。
- `TilingLib` 的三个入口（`Generate`/`GenerateCVFusion`/`GenerateForInductor`）都产出「文件名→内容」映射；产物拆成入口翻译单元 + 原子头，为按内容缓存创造条件。
- 本次更新把 CV tiling wrapper 精简为纯接口 + 独立实现，Python 侧将其编译为按内容 cache key 缓存的共享 so（文件锁 + 原子替换保证并发正确），非 CV 路径零额外开销；同时 C++ 侧为 `CallCubeTiling` 增加运行期 tiling 结果缓存。
- dtype 感知 CV 融合：Cast/取整类算子统一按 `curAivM/curAivN` 二维组织参数，N 方向 stride 按 dtype 做 `BlkAlign` 对齐，UBFuse 与 fallback 路径分别处理 DataCopy 与去 padding。
- Inductor 多阶段 PGO 候选稳定化：measured 候选按 `canonical_repr` 去重、按 default 性能过滤，导出集与 TopN 共享同一份结果，default 始终保有兜底位。

## 7. 下一步学习建议

- 下一讲 u8-l3（api_call 算子调用生成）深入 `codegen/api_call/`：本讲 `Kernel::Generate` 打印的循环体里，每个算子的具体调用语句正是由 api_call 层生成的，其中 where/compare 等 api_call 与 api_call_utils 在本次更新中同样有演进。
- 想追 CV 融合全貌，结合 u11-l2（cube 算子与 cv 融合）阅读 `autofuse/v35/codegen/vec_func_call/` 与 `reg_api_call/`，本讲的 wrapper 复用是其中的编译期环节。
- 想追 PGO 全链路，阅读 `codegen_tiling_inductor_pgo_runner.cpp` / `codegen_tiling_inductor_pgo_proxy.cpp`（独立 runner 与父进程代理），并对照 `autofuse/tests/st/python/test_inductor_pgo_compile_flow.py`。
- 建议同时翻阅 `autofuse/tests/ut/codegen/test_codegen_tiling.cpp` 与 `autofuse/tests/ut/python/test_ascendc_compile.py`——本次两大改动的行为契约几乎全部写在新增 UT 断言里。
