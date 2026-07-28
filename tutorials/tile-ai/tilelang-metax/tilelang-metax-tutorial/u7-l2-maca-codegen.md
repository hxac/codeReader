# MACA codegen 实现

## 1. 本讲目标

本讲是 U7 Metax/MACA 后端系列的第二讲，承接 u7-l1（MACA 后端架构总览）。u7-l1 讲清楚了「MACA 后端由哪些零件拼起来」——target 注册、device API、module 加载；本讲则钻进其中最核心的一个零件：**把设备端 TIR 翻译成 MetaX GPU 能编译的 MACA C 源码的那个类 `CodeGenTileLangMACA`**。

读完本讲，你应当能够：

- 说清 `CodeGenTileLangMACA` 与父类 `CodeGenC` 的继承关系，以及它 override 了哪些关键 visitor 方法。
- 区分 MACA 上的两套 intrinsic 降低通道：`intrin_rule` + `LowerIntrin` pass（处理可移植的 `tirx.*` 算子）与 codegen 自带的 `VisitExpr_(CallNode)`（处理显式的 `tl.*` builtins）。
- 区分 **fastmath**（`__expf` 等快速数学）与 **warp shuffle**（`__shfl_sync` 等线程束洗牌），并说出它们各自映射到什么 MACA 内置函数。
- 读懂 MACA 张量核指令（`mxmaca::wmma::*`、`__builtin_mxc_mma_*`）在 codegen 中是如何发射出来的。

---

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 codegen 在整个编译链里的位置

回顾 u4-l1 / u5-l3 的编译流程：DSL → IR → lowering pass → **设备 codegen** → 设备源码文本 → 设备编译器（nvcc/hiprtc/**mxcc**）。本讲的 `CodeGenTileLangMACA` 就是 MACA 那一行「设备 codegen」。它的输入是已经 lower 过的设备 `PrimFunc`（一棵 TIR），输出是一段 `.cu` 风格的 MACA C 字符串，随后交给 MACA 编译器 `mxcc` 编成 `mcbin`。

> 一句话：codegen = 「TIR → 源码字符串」的 visitor。

### 2.2 CodeGenC 与 visitor 模式

TileLang 复用了 TVM 的 `CodeGenC`——一个通用的「把 TIR 印成 C 代码」的访问者（visitor）。它对每种 TIR 节点都有一个 `VisitExpr_` / `VisitStmt_` 方法（例如遇到 `ForNode` 印一个 `for` 循环，遇到 `AddNode` 印 `a + b`）。GPU 后端要做的，就是**继承 `CodeGenC`，override 那些与 GPU 相关、与具体厂商相关的节点处理**，其余通用逻辑直接复用父类。CUDA、HIP、MACA 三个 codegen 类都是这种「`final : public CodeGenC`」的结构（见 u5-l3）。

### 2.3 intrinsic 降低的两条通道

MACA 上存在两种「把高层算子翻译成 MACA 内置函数」的方式，本讲会反复对比，先记结论：

| 通道 | 触发对象 | 在哪一步执行 | 例子 |
|------|----------|--------------|------|
| **intrin_rule 通道** | 可移植的 `tirx.*` 算子（如 `tirx.exp`、`tirx.tvm_warp_shuffle`） | `LowerIntrin` pass（codegen **之前**）改写 IR | `tirx.exp(x)` → 外部调用 `expf(x)` |
| **codegen 通道** | 显式的 `tl.*` builtins（如 `T.__exp`、`T.shfl_sync`） | codegen 的 `VisitExpr_(CallNode)` **直接**印出 | `T.__exp(x)` → `__expf(x)` |

两条通道最终都落到 MACA 的 C 内置函数，但「谁负责改写」不同。区分这两条通道是理解本讲「fastmath vs warp shuffle」的钥匙。

---

## 3. 本讲源码地图

本讲涉及的关键文件（都在 `src/maca/codegen/` 下）：

| 文件 | 作用 |
|------|------|
| [src/maca/codegen/codegen_maca.h](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.h) | `CodeGenTileLangMACA` 类声明：列出它 override 的全部 visitor 方法、`need_*`/`enable_*` 标志位 |
| [src/maca/codegen/codegen_maca.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.cc) | 主体实现：类型印出、向量化、作用域、MMA 发射、数学/shuffle builtins、`AddFunction` |
| [src/maca/codegen/intrin_rule_maca.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/intrin_rule_maca.cc) | MACA intrinsic 规则：把 `tirx.*` 注册到 `maca.FLowerIntrinsic`，供 `LowerIntrin` pass 调用 |
| [src/maca/codegen/rt_mod_maca.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/rt_mod_maca.cc) | `BuildTileLangMACA`：实例化 codegen、逐函数 `AddFunction`、调 `mxcc` 编译，注册为 `target.build.tilelang_maca` |
| [src/transform/lower_intrin.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/lower_intrin.cc) | `LowerIntrin` pass 实现：按 `<target>.FLowerIntrinsic` 属性查表改写 IR（公共层，非 MACA 专属） |

---

## 4. 核心概念与源码讲解

### 4.1 CodeGenTileLangMACA：类结构与 CodeGenC 继承关系

#### 4.1.1 概念说明

`CodeGenTileLangMACA` 是 MACA 后端的设备 codegen。它的设计哲学和 CUDA/HIP codegen 完全对称（见 u5-l3）：**不重写通用逻辑，只 override 与 MetaX GPU / MACA 语言相关的部分**。因此它 `final : public CodeGenC`——继承 TVM 的通用 C codegen，把 GPU 专有的东西（存储作用域 `__shared__`、kernel 前缀 `__global__`、张量核、warp shuffle、向量化打包类型等）换成 MACA 版本。

#### 4.1.2 核心流程

codegen 的工作流由 `rt_mod_maca.cc::BuildTileLangMACA` 驱动，分四步：

1. 实例化 `CodeGenTileLangMACA cg; cg.Init(false);`
2. 校验每个设备 `PrimFunc` 都带 `kDeviceKernelLaunch` 调用约定，逐个 `cg.AddFunction(gvar, f)`。
3. `cg.Finish()` 收尾，根据沿途置位的 `need_*` 标志补上 `#include`，返回完整源码字符串。
4. 经 Python 回调 `tilelang_callback_maca_postproc` / `tilelang_callback_maca_compile` 调 `mxcc` 编成 `mcbin`。

#### 4.1.3 源码精读

类的继承关系与 override 列表（这是理解「它到底改了什么」的入口）：

[src/maca/codegen/codegen_maca.h:27-73](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.h#L27-L73) 声明 `CodeGenTileLangMACA final : public CodeGenC`，并 override 了一长串方法，可归为三类：
- **类型与向量化**：`PrintType`、`PrintVecBinaryOp`、`PrintVecElemLoad/Store`、`GetVecLoad`、`PrintVecStore`、`CastFromFrom`、`VisitExpr_(CastNode*)`。
- **存储与作用域**：`PrintStorageScope`、`PrintStorageSync`、`IsScopePartOfType`、`GetBufferRef`。
- **语句/表达式 visitor**：`VisitStmt_(ForNode*)`、`VisitExpr_(CallNode*)`、`VisitExpr_(MinNode*/MaxNode*)`、`VisitStmt_(AttrStmtNode*)` 等。

MACA 与 CUDA 一个关键差异落在作用域处理上。MACA 把存储作用域当作独立的前缀关键字，而不是类型的一部分：

[src/maca/codegen/codegen_maca.h:88](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.h#L88) `IsScopePartOfType() const final { return false; }` —— 告诉父类「`__shared__` 之类的修饰符不属于类型本身」，因此 codegen 会把它单独印在变量前面。配合 `PrintStorageScope`：

[src/maca/codegen/codegen_maca.cc:1139-1149](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.cc#L1139-L1149) 把 `shared` 印成 `__shared__`、`shared.dyn` 印成 `extern __shared__ __align__(1024)`（动态 shared memory，对齐 1024 字节），并禁止在 kernel 内分配 global。

> 注意 `shared.dyn` 用 1024 字节对齐——u7-l1 提到 MACA device API 强制 256 字节对齐，这里对 shared 动态分配提出了更高的对齐要求，是为了让异步批量拷贝（TMA/cp.async 类）的首地址满足对齐前提。

`AddFunction` 决定每个 kernel 函数「长什么样」：

[src/maca/codegen/codegen_maca.cc:3786-3834](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.cc#L3786-L3834) 先 `PrintFuncPrefix` 印出 `extern "C" __global__`（见 [codegen_maca.cc:233-235](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.cc#L233-L235)），再 `PrintExtraAttrs` 印 `__launch_bounds__`，然后逐个参数印类型与名字。其中 `grid_constant` 参数会加上 `__grid_constant__ const`（MACA 对应 CUDA 的 grid constant 机制）；当 kernel 用了 PDL（Programmatic Dependent Launch）同步时会抑制 `__restrict__`，因为 MXCC 在这种场景下对 `__restrict__` 有问题。

#### 4.1.4 代码实践

**实践目标**：确认 MACA codegen 复用了多少 `CodeGenC`，又有多少是自己 override 的。

**操作步骤**：
1. 打开 [codegen_maca.h:27-73](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.h#L27-L73)，数一下 `final` override 的方法数量。
2. 在 [codegen_maca.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.cc) 里搜索 `CodeGenC::`（显式调用父类实现的地方），例如 `PrintCallExtern` 末尾的 `CodeGenC::PrintCallExtern(...)`、`VisitExpr_(CallNode*)` 末尾的 `CodeGenC::VisitExpr_(op, os)`。

**需要观察的现象**：MACA codegen 在「不认识的 Call」时回落到父类（`CodeGenC::VisitExpr_(op, os)`），意味着所有通用算术（加减乘除、比较等）都不用自己写，只有 MACA 专有的 builtin 才需要特判。

**预期结果**：你会看到绝大多数「普通」节点都最终落到 `CodeGenC::`，证明「继承 + 选择性 override」的设计有效。

#### 4.1.5 小练习与答案

**练习 1**：`CodeGenTileLangMACA` 为什么要 `final`？如果去掉 `final` 会有什么影响？
**参考答案**：`final` 表示该类不能再被继承。MACA codegen 是叶子实现，没有子类化需求；加 `final` 还能让编译器对它的虚函数调用做去虚化（devirtualization），略微提升编译期性能，并防止下游意外派生出行为不一致的 codegen。

**练习 2**：`PrintFuncPrefix` 印出的 `extern "C" __global__` 各自起什么作用？
**参考答案**：`extern "C"` 关闭 C++ 的名字修饰（name mangling），保证链接符号名就是函数名本身——这对 runtime 按名字 `mcModuleLaunchKernel` 启动 kernel 是必须的；`__global__` 是 MACA/CUDA 的 kernel 函数限定符，表示该函数在 host 调用、在 device 执行。

---

### 4.2 intrin_rule：MACA intrinsic 规则体系

#### 4.2.1 概念说明

「intrinsic 规则」解决的问题是：TIR 里有一批**与厂商无关的可移植算子**（如 `tirx.exp`、`tirx.sqrt`、`tirx.tvm_warp_shuffle`），但每个 GPU 厂商的内置函数命名都不一样（NVIDIA 是 `__expf`/`__shfl_sync`，MetaX 是另一套名字）。TileLang 用一张「`<target>.FLowerIntrinsic` 属性表」来登记「在 XXX 后端，某个可移植算子应该改写成什么」，由一个公共 pass `LowerIntrin` 在 codegen 之前完成改写。MACA 的这张表就写在 `intrin_rule_maca.cc`。

#### 4.2.2 核心流程

`LowerIntrin` pass 的核心是 `IntrinInjecter`，它在构造时按优先级拼接若干属性表名：

[src/transform/lower_intrin.cc:52-69](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/lower_intrin.cc#L52-L69) 先查 `<target>.FLowerIntrinsic`（对 MACA 就是 `maca.FLowerIntrinsic`），再查 `<target>.FLegalize`，最后兜底 `default.FLowerIntrinsic` / `default.FLegalize`。遍历时第一个命中的规则胜出。

[src/transform/lower_intrin.cc:72-90](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/lower_intrin.cc#L72-L90) 每遇到一个 `Call` 节点，就按上述顺序查表，若规则函数把表达式改写了，就用改写后的结果替换并递归访问。

于是「注册规则」和「使用规则」完全解耦：`intrin_rule_maca.cc` 只管往 `maca.FLowerIntrinsic` 这张表里塞条目，`LowerIntrin` pass 负责查表执行。

#### 4.2.3 源码精读

注册一条规则的两步范式。以「指数函数」为例：

[src/maca/codegen/intrin_rule_maca.cc:213-215](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/intrin_rule_maca.cc#L213-L215) 给 `tirx.exp` 这个 Op 挂上 `maca.FLowerIntrinsic` 属性，值为 `DispatchPureExtern<MACAMath>`。

`MACAMath` 是个「名字生成器」仿函数，根据 dtype 决定后缀：

[src/maca/codegen/intrin_rule_maca.cc:36-73](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/intrin_rule_maca.cc#L36-L73) 对浮点数：fp64 返回原名（`exp`）、fp32 加 `f`（`expf`）、fp16 加 `h` 前缀（`hexp`，`fabs`/`round` 有特例）。`DispatchPureExtern` 是 TVM 提供的模板：它取出算子的基础名（`exp`），喂给 `MACAMath(dtype, "exp")` 得到目标函数名（如 `expf`），再把整个 Call 改写成一个「纯外部调用」`call_pure_extern("expf", args)`。

整张表用同一个 `MACAMath` 注册了一大批标准数学函数：

[src/maca/codegen/intrin_rule_maca.cc:189-303](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/intrin_rule_maca.cc#L189-L303) `floor`/`ceil`/`trunc`/`fabs`/`exp`/`exp2`/`exp10`/`erf`/`log`/`log2`/`log10`/`tan`/`cos`/`cosh`/`sin`/`sinh`/`atan`/`tanh`/`sqrt`/`pow`/`rsqrt`/`fmod` 等都走 `DispatchPureExtern<MACAMath>`，各自映射到带 `f`/`h` 后缀的 MACA 数学库函数。

> 关键区别（与 4.3 呼应）：这里注册的是**标准精度**数学（`expf`），不是快速数学（`__expf`）。快速数学走的是另一条通道，见 4.3。

#### 4.2.4 代码实践

**实践目标**：验证「注册」与「使用」解耦，并理解 `DispatchPureExtern` 的输入输出。

**操作步骤**：
1. 在 [intrin_rule_maca.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/intrin_rule_maca.cc) 里数出有多少个 `tirx.*` 算子被挂上了 `maca.FLowerIntrinsic`。
2. 对照 [lower_intrin.cc:52-69](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/lower_intrin.cc#L52-L69)，确认 `maca.FLowerIntrinsic` 是在 `target.kind.name == "maca"` 时被拼出来的 pattern。

**需要观察的现象**：`intrin_rule_maca.cc` 里没有任何「执行改写」的循环逻辑，只有一堆 `TVM_REGISTER_OP(...).set_attr<FLowerIntrinsic>(...)`；真正遍历 IR 查表的是 `lower_intrin.cc`。

**预期结果**：你会直观看到「数据（规则表）与逻辑（pass 遍历器）分离」的设计——这正是 TVM 的 Op 属性机制（见 u5-l1 的 `op/builtin.cc` 注册方式同源）。

#### 4.2.5 小练习与答案

**练习 1**：如果 MetaX 的 MACA 库里 `rsqrt` 的函数名变了（比如改成 `__maca_rsqrtf`），需要改 codegen 哪里？
**参考答案**：只需改 [intrin_rule_maca.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/intrin_rule_maca.cc) 里 `MACAMath` 仿函数对 `rsqrt` 的返回值，或单独给 `tirx.rsqrt` 挂一条自定义规则。不必动 `LowerIntrin` pass 或 codegen 主体——这正是规则表解耦的好处。

**练习 2**：为什么 `LowerIntrin` 要先查 `maca.FLowerIntrinsic`，再查 `default.FLowerIntrinsic`？
**参考答案**：先查后端专属规则，让 MACA 能覆盖默认行为；找不到再退回 `default.*`，保证未被特化的算子仍有合理 lowering。这是一种「特化优先、兜底保底」的优先级链。

---

### 4.3 fastmath：快速数学的降低规则

#### 4.3.1 概念说明

GPU kernel 里 `exp`、`log`、`sin` 这类超越函数很贵。厂商通常提供两套实现：**标准精度版**（如 `expf`，符合 IEEE）和**快速近似版**（如 `__expf`，牺牲一点精度换速度）。TileLang 让用户通过显式的 `T.__exp`、`T.__log`、`T.__sin` 等「双下划线」builtin 来主动要求快速版。

要点：**标准数学（`expf`）走 intrin_rule 通道（4.2），快速数学（`__expf`）走 codegen 通道**。两者命名生成器不同。

#### 4.3.2 核心流程

快速数学 lowering 全部发生在 codegen 的 `VisitExpr_(CallNode*)` 里——也就是说，`T.__exp` 这个 Call 节点**不会被 `LowerIntrin` 改写**，而是原样保留到 codegen，由 codegen 直接印成 `__expf(x)`：

```
T.__exp(x)   [tl.__exp 这个 Op 的 Call 节点]
   │
   │  codegen::VisitExpr_(CallNode*) 命中 op->op.same_as(tl::__exp())
   ▼
MACAFastMath(fp32, "exp")  →  "__" + "exp" + "f"  =  "__expf"
   │
   ▼
印出：__expf(x)
```

#### 4.3.3 源码精读

codegen 内的快速数学名字生成器 `MACAFastMath`：

[src/maca/codegen/codegen_maca.cc:61-70](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.cc#L61-L70) 对 fp32 在名字前加 `__`、后加 `f`（`exp` → `__expf`）；其它精度退回父类 `MACAMath`（fp64 → `exp`，fp16 → `hexp`）。注意它与 [intrin_rule_maca.cc:75-84](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/intrin_rule_maca.cc#L75-L84) 里同名但**未被注册使用**的 `MACAFastMath` 是两处独立定义——codegen 用的是自己文件内的这份。

实际发射 `T.__exp`：

[src/maca/codegen/codegen_maca.cc:2465-2468](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.cc#L2465-L2468) `tl::__exp` → `MACAFastMath(dtype, "exp")(arg)`，fp32 即印成 `__expf(x)`。

同一套机制覆盖一批快速数学 builtin：

[src/maca/codegen/codegen_maca.cc:2465-2496](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.cc#L2465-L2496) `T.__exp`/`T.__exp10`/`T.__log`/`T.__log2`/`T.__log10`/`T.__tan`/`T.__cos`/`T.__sin` 都用 `MACAFastMath` 生成 `__<name>f` 形式的 MACA 快速内置函数。

> 另有一条「IEEE 精确舍入」通道：`T.ieee_add`/`ieee_mul`/`ieee_fmaf`/`ieee_fsqrt` 等用 `MACAIEEEMath` 生成带舍入模式的 `__fadd_rn` / `__fmul_rn` / `__dadd_rn` 等（见 [codegen_maca.cc:90-100](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.cc#L90-L100) 与 [2497-2540](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.cc#L2497-L2540)）。于是 MACA 上同时存在「标准（`expf`）—快速（`__expf`）—IEEE 精确（`__fadd_rn`）」三档数学，分别由 intrin_rule、codegen-fastmath、codegen-ieee 三处负责。

#### 4.3.4 代码实践

**实践目标**：对比 `tirx.exp`（标准）与 `T.__exp`（快速）最终生成的 MACA 源码差异。

**操作步骤**：
1. 阅读 [intrin_rule_maca.cc:213-215](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/intrin_rule_maca.cc#L213-L215)（`tirx.exp` → `expf`）与 [codegen_maca.cc:2465-2468](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.cc#L2465-L2468)（`T.__exp` → `__expf`）。
2. 若本地有 MetaX 设备：写一个 elementwise kernel，分别用 `T.exp(x)`（标准）和 `T.__exp(x)`（快速），用 `get_kernel_source()` 打印生成代码并对比；用 `get_profiler().do_bench()` 对比两者延迟。
3. 若无设备：仅完成源码阅读，列出两者的 builtin 名字差异即可，标注「待本地验证」性能数字。

**需要观察的现象**：标准版生成 `expf(...)`，快速版生成 `__expf(...)`；快速版在数值精度上略低，但延迟通常更低。

**预期结果**：生成源码里能清晰看到 `expf` vs `__expf` 的区别；性能差异待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `T.__exp` 不像 `tirx.exp` 那样在 `intrin_rule_maca.cc` 里注册 lowering？
**参考答案**：因为快速数学是**用户显式选择**的语义（带 `__` 前缀的 builtin），它本身就是 MACA-specific 的，不需要「可移植算子→后端名」的间接层；codegen 直接印出最简单。而 `tirx.exp` 是可移植算子，必须通过 intrin_rule + `LowerIntrin` 让每个后端各自决定映射，才能保持前端与后端解耦。

**练习 2**：fp16 下 `T.__exp` 会生成什么？
**参考答案**：根据 [codegen_maca.cc:61-70](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.cc#L61-L70)，fp16 不满足「fp32」条件，退回 `MACAMath`，返回 `hexp`。所以 fp16 的 `T.__exp` 生成 `hexp(x)`（快速版与标准版在 fp16 上合并为同一个 half 数学函数）。

---

### 4.4 warp shuffle：线程束洗牌的降低规则

#### 4.4.1 概念说明

warp shuffle（线程束洗牌）让同一 warp 内的线程直接交换寄存器值，不必经过 shared memory，是做 warp 内归约（reduction）、scan 的高速通道。MACA（与 CUDA 一样）提供 `__shfl_sync`、`__shfl_down_sync`、`__shfl_up_sync`、`__shfl_xor_sync`、`__activemask` 等内置函数。

和 fastmath 类似，warp shuffle 也有两条通道，但它的「intrin_rule 通道」更复杂——因为可移植的 `tirx.tvm_warp_shuffle` 带 5 个参数（含 `warp_size`），而 MACA 的 `__shfl_sync` 只要 4 个参数，需要在 lowering 时**改写 Op 并丢掉一个参数**。

#### 4.4.2 核心流程

warp shuffle 的两条通道：

```
通道 A（可移植，经 intrin_rule + LowerIntrin）：
  tirx.tvm_warp_shuffle(mask, value, warp_id, width, warp_size)   [5 参数]
     │  maca.FLowerIntrinsic = DispatchMACAShuffle<MACAWarpIntrinsic>
     ▼  改写 Op：tirx.tvm_warp_shuffle → tirx.maca.__shfl_sync，丢弃第 5 个 warp_size
  tirx.maca.__shfl_sync(mask, value, warp_id, width)              [4 参数]
     │  该 Op 注册了 TGlobalSymbol="__shfl_sync"
     ▼  codegen 当作外部调用印出
  __shfl_sync(mask, value, warp_id, width)

通道 B（显式 TL builtin，经 codegen 直接印）：
  tl.shfl_sync(mask, value, src_lane, width)
     │  codegen::VisitExpr_(CallNode*) 命中
     ▼
  __shfl_sync(mask, value, src_lane, width)
```

两条通道最终都落到 MACA 的 `__shfl_sync`。

#### 4.4.3 源码精读

intrin_rule 通道：把可移植 warp shuffle 改写成 MACA 专属 Op。

[src/maca/codegen/intrin_rule_maca.cc:142-153](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/intrin_rule_maca.cc#L142-L153) `MACAWarpIntrinsic` 把三种可移植 warp shuffle Op 映射到三个 MACA 专属 Op：`tirx.tvm_warp_shuffle` → `tirx.maca.__shfl_sync`，`..._up` → `tirx.maca.__shfl_up_sync`，`..._down` → `tirx.maca.__shfl_down_sync`。

[src/maca/codegen/intrin_rule_maca.cc:175-182](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/intrin_rule_maca.cc#L175-L182) `DispatchMACAShuffle` 取原 Call 的前 4 个参数（`mask, value, warp_id, width`，**丢弃第 5 个 `warp_size`**），用 `MACAWarpIntrinsic` 选出目标 Op，构造新的 Call。注册见 [intrin_rule_maca.cc:281-291](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/intrin_rule_maca.cc#L281-L291)。

改写后的低层 Op `tirx.maca.__shfl_sync` 自己也是一个注册的 Op，带全局符号：

[src/maca/codegen/intrin_rule_maca.cc:311-321](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/intrin_rule_maca.cc#L311-L321) 注册 `tirx.maca.__shfl_sync`：4 个输入（mask/var/lane/width），`TGlobalSymbol` 设为 `__shfl_sync`，并打上 `maca.need_warp_shuffle=true` 属性。codegen 遇到带 `TGlobalSymbol` 的 Op 时，会按「外部调用」印成 `__shfl_sync(...)`。同文件还注册了 `__shfl_up_sync`、`__shfl_down_sync`、`__activemask`（[323-353](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/intrin_rule_maca.cc#L323-L353)）。

codegen 通道：显式 TL builtin 直接印成 MACA shuffle。

[src/maca/codegen/codegen_maca.cc:2389-2412](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.cc#L2389-L2412) `tl.shfl_sync` → `__shfl_sync(mask, value, src_lane, width)`、`tl.shfl_xor_sync` → `__shfl_xor_sync(...)`、`tl.shfl_down_sync` → `__shfl_down_sync(...)`、`tl.shfl_up_sync` → `__shfl_up_sync(...)`。这组是用户在 DSL 里直接写 `T.shfl_sync(...)` 时走的路径。

> warp 内归约的便利封装 `tl::warp_reduce_sum/max/min` 也在 codegen 直接印出（[codegen_maca.cc:2249-2258](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.cc#L2249-L2258)），它们内部最终也依赖 shuffle。

#### 4.4.4 代码实践（本讲主实践任务）

**实践目标**：在 `intrin_rule_maca.cc` 与 `codegen_maca.cc` 中分别找出 warp shuffle（`__shfl_sync` 等）与快速数学（`__expf` 等）的降低规则，说明它们各自映射到什么 MACA 内置函数，并区分两条通道。

**操作步骤**：
1. **warp shuffle（intrin_rule 通道）**：打开 [intrin_rule_maca.cc:142-153](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/intrin_rule_maca.cc#L142-L153) 与 [175-182](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/intrin_rule_maca.cc#L175-L182)，确认 `tirx.tvm_warp_shuffle` 经 `DispatchMACAShuffle<MACAWarpIntrinsic>` 改写为 `tirx.maca.__shfl_sync`（丢掉 `warp_size` 参数），再查 [311-321](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/intrin_rule_maca.cc#L311-L321) 确认该 Op 的 `TGlobalSymbol="__shfl_sync"`。结论：可移植 warp shuffle → MACA 内置函数 `__shfl_sync(mask, value, lane, width)`。
2. **warp shuffle（codegen 通道）**：打开 [codegen_maca.cc:2389-2394](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.cc#L2389-L2394)，确认 `tl.shfl_sync` 直接印成 `__shfl_sync(...)`。
3. **fastmath**：注意 `__expf` **不在** `intrin_rule_maca.cc` 的注册表里（那里注册的是标准 `expf`，见 [213-215](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/intrin_rule_maca.cc#L213-L215)）。快速版 `__expf` 在 [codegen_maca.cc:2465-2468](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.cc#L2465-L2468)，由 `MACAFastMath`（[61-70](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.cc#L61-L70)）把 `T.__exp` 印成 `__expf`。结论：`T.__exp`（fp32）→ MACA 快速数学内置函数 `__expf`。
4. 整理成一张对照表（见下方「预期结果」）。

**需要观察的现象**：
- warp shuffle 的 intrin_rule 规则**改写 Op 并减参数**（5→4），是「Op 级」改写；而 fastmath 的标准数学规则只是**换函数名**（`exp`→`expf`），是「extern 调用级」改写。
- `__expf` 这条快速规则**不在 intrin_rule_maca.cc**，而在 codegen——因为它绑定的是显式 builtin `T.__exp`，不经 `LowerIntrin`。

**预期结果**（对照表）：

| 高层入口 | 降低通道 | 中间结果 | 最终 MACA 内置函数 |
|----------|----------|----------|--------------------|
| `tirx.tvm_warp_shuffle`（可移植，5 参） | intrin_rule + `LowerIntrin` | 改写为 `tirx.maca.__shfl_sync`（4 参） | `__shfl_sync(mask, value, lane, width)` |
| `T.shfl_sync`（显式 builtin，4 参） | codegen 直接印 | — | `__shfl_sync(mask, value, src_lane, width)` |
| `tirx.exp`（可移植） | intrin_rule + `LowerIntrin` | extern 调用 `expf` | `expf(x)`（标准精度） |
| `T.__exp`（显式快速 builtin） | codegen 直接印 | — | `__expf(x)`（快速近似） |

**说明**：本实践为源码阅读型实践，无需 MetaX 设备即可完成；若要验证生成代码，可在有设备时用 `get_kernel_source()` 打印对照（性能数字待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `tirx.tvm_warp_shuffle` 有 5 个参数，而 MACA 的 `__shfl_sync` 只要 4 个？被丢掉的那个参数是什么？
**参考答案**：被丢掉的是第 5 个参数 `warp_size`。可移植算子需要一个 `warp_size` 参数来适配不同后端（CUDA warp_size=32、MACA=64，见 u7-l1），但 MACA 的 `__shfl_sync` 内置函数自身隐含了 warp 语义、不需要外部传入 warp_size，所以 `DispatchMACAShuffle` 在 [intrin_rule_maca.cc:179-181](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/intrin_rule_maca.cc#L179-L181) 只取前 4 个参数。

**练习 2**：`tirx.maca.__shfl_sync` 这个 Op 注册时为什么还要打 `maca.need_warp_shuffle=true` 属性？
**参考答案**：这是一个「能力标记」属性。codegen 在 [codegen_maca.h:144-145](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.h#L144-L145) 用 `OpAttrMap<bool>("maca.need_warp_shuffle")` 把它读进 `op_need_warp_shuffle_`，便于在更上层（如布局推断、归约策略）判断当前 kernel 是否用到了 warp shuffle，从而决定能否做 warp 级优化。

---

### 4.5 （附加）向量化、MMA 指令发射与按需 include

本节简要覆盖 spec 提到的「向量化」与「MMA 指令发射」，帮助你把 codegen 的全貌补齐。

#### 4.5.1 向量化与打包类型

MACA 没有像 LLVM 那样的通用向量类型，而是用厂商提供的打包结构（`half2`、`float4`、`uint2`、`maca_bfloat162` 等）。`PrintType` 负责把 TIR 的向量 dtype 映射到这些结构：

[src/maca/codegen/codegen_maca.cc:406-700](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.cc#L406-L700) 例如 fp16 的 `lanes<=8` 映射成 `uintN`（每两个 half 打包成一个 uint），fp32 的 `4<lanes<=8` 映射成 `ulonglongN`，并配 `PrintVecElemLoad/Store` 用 `((half2*)(&vec.field))->lane` 的方式按 lane 读写。

[src/maca/codegen/codegen_maca.cc:702-900](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.cc#L702-L900) `PrintVecBinaryOp` 有一条「打包 x2 算术」快路径 `CanEmitPackedX2MathMACA`：当 dtype 是 fp16/bf16/fp32 且 lanes 为偶数时，把向量运算拆成若干个 `tl::add2/sub2/mul2/fma2` 调用（每两个 lane 一组），并能自动把 `mul+add` 融合成 `fma2`，从而用上 MACA 的打包 SIMD 指令。

#### 4.5.2 MMA 指令发射

MACA 张量核有两套发射方式，都在 `VisitExpr_(CallNode*)`：

[src/maca/codegen/codegen_maca.cc:2034-2053](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.cc#L2034-L2053) legacy WMMA 接口：`tvm_mma_sync` → `mxmaca::wmma::mma_sync(...)`、`tvm_bmma_sync` → `mxmaca::wmma::bmma_sync(...)`，配 `tvm_fill_fragment`/`tvm_load_matrix_sync`/`tvm_store_matrix_sync`，并置 `need_mma_h_` 以便 `Finish` 补 `#include <mma.h>`。

[src/maca/codegen/codegen_maca.cc:2062-2138](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.cc#L2062-L2138) MFMA 接口（u7-l3 会深入）：`tvm_mfma` 取出 `prefix`（如 `16x16x16fp16`），拼成 `__builtin_mxc_mma_<prefix>`，用一个模板字符串加 `Replacer` 替换占位符（dtype、A/B/C 引用与偏移），印出一行 `*(((C*)c_ref)+c_bias) = __builtin_mxc_mma_...(...)`。这正是 u7-l3 讲的 mfma 指令在 codegen 的落点。

#### 4.5.3 按需 include

[src/maca/codegen/codegen_maca.cc:318-372](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.cc#L318-L372) `Finish()` 根据沿途置位的 `need_*` 标志（`need_mma_h_`、`need_copy_h_`、`need_cooperative_groups_`、`need_mcrand_kernel_h_`、`enable_fp8_`、`enable_sparse_gemm_` 等）按需补上对应的 `#include <tl_templates/maca/...>`，最后调用 `CodeGenC::Finish()`。这套「标志位 + 延迟 include」机制和 CUDA codegen 一致（见 u5-l3），目的是只在用到某类指令时才引入对应头文件，避免无谓依赖。

---

## 5. 综合实践：跟踪一条 GEMM 在 MACA codegen 的脚印

把本讲四个模块串起来，做一个端到端的源码追踪。

**任务**：假设有一份 GEMM kernel 走 MACA target 编译，请按 codegen 的执行顺序，把以下「脚印」一一对应到本讲讲过的代码点，并写出每一步在哪个文件、做了什么：

1. `BuildTileLangMACA` 实例化 codegen，对设备 `PrimFunc` 调 `AddFunction`。
2. `AddFunction` 印出 `extern "C" __global__` 前缀与 `__launch_bounds__`，处理 `__shared__` shared memory 分配。
3. 遇到 `T.copy` 产生的 `maca_memcpy_async` → 印成 `memcpy_async<bytes>(...)`（异步拷贝，对应 [codegen_maca.cc:1842-1857](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.cc#L1842-L1857)）。
4. 遇到 `T.gemm` 下译出的 `tvm_mfma` → 印成 `__builtin_mxc_mma_<prefix>(...)`（4.5.2）。
5. 遇到 warp 内归约用的 `tirx.tvm_warp_shuffle` → 经 `LowerIntrin`（在 codegen 前已改写）变成 `tirx.maca.__shfl_sync` → 印成 `__shfl_sync(...)`（4.4）。
6. 遇到激活函数 `T.__exp` → 印成 `__expf(...)`（4.3）。
7. `Finish()` 按标志位补 `#include <tl_templates/maca/gemm.h>`、`<mma.h>` 等（4.5.3）。
8. 源码字符串经 `tilelang_callback_maca_postproc` / `tilelang_callback_maca_compile` 交 `mxcc` 编成 `mcbin`（[rt_mod_maca.cc:121-139](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/rt_mod_maca.cc#L121-L139)）。

**产出**：一张「步骤 → 文件:行号 → 作用 → 最终印出的 MACA 构造」的表。完成后你会看到，`CodeGenTileLangMACA` 本质上就是一个「**遇到什么 TIR 节点，就印什么 MACA 代码**」的大 visitor，而 intrin_rule + `LowerIntrin` 是它在 codegen 之前的「预处理帮手」。

---

## 6. 本讲小结

- `CodeGenTileLangMACA final : public CodeGenC`：继承 TVM 通用 C codegen，只 override 与 MetaX/MACA 相关的 visitor（类型印出、存储作用域、向量化、MMA、shuffle、数学 builtin），其余复用父类。
- MACA 把存储作用域视为独立修饰符（`IsScopePartOfType()` 返回 `false`），`shared` 印 `__shared__`、`shared.dyn` 印 1024 字节对齐的 `extern __shared__`。
- MACA 有**两条 intrinsic 降低通道**：intrin_rule + `LowerIntrin` pass（处理可移植 `tirx.*`，在 codegen 前改写 IR）与 codegen 自带 `VisitExpr_(CallNode*)`（处理显式 `tl.*` builtin，直接印出）。
- **fastmath**：`tirx.exp`（标准）经 intrin_rule 印 `expf`；`T.__exp`（快速）经 codegen 印 `__expf`。另有 `T.ieee_*`（IEEE 精确舍入）印 `__fadd_rn` 等。三档数学各走各的通道。
- **warp shuffle**：`tirx.tvm_warp_shuffle`（5 参）经 intrin_rule 改写为 `tirx.maca.__shfl_sync`（4 参，丢 `warp_size`）再印 `__shfl_sync`；`T.shfl_sync` 经 codegen 直接印 `__shfl_sync`。
- MMA 指令通过 `tvm_mfma` → `__builtin_mxc_mma_<prefix>` 发射，`Finish()` 用「按需 include」机制根据 `need_*` 标志补头文件；整个 codegen 由 `BuildTileLangMACA` 驱动，产物交 `mxcc` 编译。

---

## 7. 下一步学习建议

- **u7-l3 MACA MMA intrinsics（mfma）**：本讲只讲了 codegen 如何把 `tvm_mfma` 印成 `__builtin_mxc_mma_<prefix>`，下一讲深入 Python 侧的 mfma 发射器（`mma_macro_generator.py`）与 `mma_layout` 布局变换，讲清 `prefix`（如 `16x16x16fp16`）是怎么决定的。
- **u7-l4 MACA 编译流水线与 transform**：本讲的 codegen 是流水线末端，下一讲往前看 MACA 专属的 `MACAPassPipelineBody` 与 `LowerMACAIntrin` pass，理解 IR 在到达 codegen 之前还经过了哪些 MACA 专属处理。
- **想加深对比**：回头重读 u5-l3（CUDA/HIP codegen）与 u5-l4（tl_templates），把 MACA codegen 与 CUDA/HIP codegen 的「三后端结构对称、细节各有坑」对照起来，会更理解本讲反复强调的两条通道设计。
