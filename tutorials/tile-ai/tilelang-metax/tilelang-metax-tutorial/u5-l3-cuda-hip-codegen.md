# CUDA/HIP codegen 后端

## 1. 本讲目标

经过 u5-l1、u5-l2，我们已经知道 `src/` 是怎么组织的、`transform/` 的那些 pass 如何把高层 TileOp 一层层降级成「准机器码」的 TIR。但 TIR 仍然不是 GPU 能执行的东西——它只是一棵中间表示树。真正把它变成 NVIDIA/AMD GPU 能编译的 C/CUDA/HIP 源码文本的，是**代码生成器（codegen）**这一层。

本讲聚焦 `src/cuda` 与 `src/rocm` 两个 GPU 后端的 codegen，学完你应该能够：

1. 说清 `CodeGenTileLangCUDA` / `CodeGenTileLangHIP` 的继承结构，以及它们如何被绑定到 `cuda` / `hip` 这两个 target。
2. 理解 `intrin_rule` 是如何把与后端无关的 `tirx.*` 可移植算子，映射成 `rsqrtf`、`__shfl_sync` 这样的后端内置函数。
3. 读懂 `target_utils.cc` 里「按架构检测硬件能力」的函数（架构代际、warp_size、是否支持异步拷贝等），并知道这些检测结果在编译流水线里被谁消费。
4. 把 CUDA 与 HIP 两个后端并排放在一起对比，理解多后端架构的取舍。

本讲只讲 codegen 与 target_utils；张量核（MMA/WGMMA/MFMA）指令的具体发射在 u6 系列与本 fork 核心的 u7（MACA）系列展开。

## 2. 前置知识

- **codegen（代码生成）**：把 IR 翻译成某种具体语言源代码文本的程序。TileLang 的 GPU codegen 输出的是「带 `__shared__`、`__shfl_sync` 等扩展的 C 源码」，再交给 `nvcc`（CUDA）或 `hiprtc`/`clang`（HIP）编译成机器码。
- **`CodeGenC`**：TVM 自带的「生成 C 源码」的基类。它能把大部分 TIR（循环、加减、数组读写）翻译成普通 C，但遇到 GPU 专有的东西（共享内存作用域、warp shuffle、张量核）就无能为力——这正是 `CodeGenTileLangCUDA/HIP` 要 `override` 的地方。
- **`final` 继承**：C++ 里 `class X final : public CodeGenC` 表示 X 继承 CodeGenC，且 X 不允许再被继承。两个 codegen 类都是「叶子类」，只实例化、不派生。
- **target kind 与 arch/mcpu**：`target` 回答「编给谁」。CUDA target 用 `arch="sm_80"` 表示架构代（compute capability），HIP target 用 `mcpu="gfx90a"` 表示具体芯片。本讲的核心之一就是：**CUDA 用数字 arch 判代际，HIP 用 mcpu 字符串判代际**。
- **FLowerIntrinsic**：TVM 的「intrinsic lowering」机制。一个 TIR 算子（如 `tirx.rsqrt`）通过一个以 `<target>.FLowerIntrinsic` 为键的属性，挂上一条「如何把它降级」的规则。这条规则在 `LowerIntrin` pass 里被调用。
- **FFI（Foreign Function Interface）**：C++ 与 Python 互调的桥。本讲里大量 `tl.TargetIsCuda` 这样的「全局函数」就是经 FFI 暴露给 Python 的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/cuda/codegen/codegen_cuda.h` / `.cc` | `CodeGenTileLangCUDA` 类声明与实现：把 device TIR 印成 CUDA C 源码。 |
| `src/cuda/codegen/rt_mod_cuda.cc` | `BuildTileLangCUDA`：实例化 codegen、逐函数印码、跑 postproc/compile 回调，产出 `CUDAModule`。并把 `target.build.tilelang_cuda` 注册为全局函数。 |
| `src/cuda/codegen/intrin_rule_cuda.cc` | CUDA 的 intrinsic 降低规则（`cuda.FLowerIntrinsic`）。 |
| `src/cuda/target_utils.cc` / `.h` | CUDA 的按架构能力检测函数，并经 FFI 暴露为 `tl.TargetIs*` / `tl.TargetCuda*`。 |
| `src/rocm/codegen/codegen_hip.h` / `.cc` | `CodeGenTileLangHIP` 类声明与实现：把 device TIR 印成 HIP C 源码。 |
| `src/rocm/codegen/rt_mod_hip.cc` | `BuildTileLangHIP`：HIP 版的 build 入口，注册 `target.build.tilelang_hip`。 |
| `src/rocm/codegen/intrin_rule_hip.cc` | HIP 的 intrinsic 降低规则（`hip.FLowerIntrinsic`）。 |
| `src/rocm/target_utils.cc` / `.h` | ROCm 的按芯片能力检测函数。 |
| `src/backend/common/target_utils.cc` | 跨后端统一分发的能力检测（如 `TargetHasAsyncCopy`）。 |
| `tilelang/cuda/codegen.py` / `tilelang/rocm/codegen.py` | Python 侧：把 target kind 与 C++ build 函数对接起来。 |

一条贯穿全讲的链路是：

```
Python lower()  →  DeviceCodegen(cuda/hip)  →  全局函数 target.build.tilelang_cuda/hip
                                                         (rt_mod_*.cc 注册)
                                                      ↓
                                    CodeGenTileLangCUDA/HIP.AddFunction → .Finish()
                                                      ↓
                                         源码文本 → postproc 回调 → compile 回调(nvcc/hiprtc)
```

---

## 4. 核心概念与源码讲解

### 4.1 codegen_cuda / codegen_hip：从 IRModule 到设备源码

#### 4.1.1 概念说明

经过 `SplitHostDevice`（见 u5-l2）之后，编译器手里有一个只含「设备函数」的 `IRModule`。这些设备函数已经被打过 `kDeviceKernelLaunch` 标记。**codegen 的职责就是把这个 IRModule 翻译成一段 C 源码文本**，文本里每个函数都是 GPU kernel（CUDA 的 `__global__`，HIP 的 `__global__`）。

CUDA 和 HIP 的 codegen 长得非常像——因为 HIP 本身就是 AMD 仿照 CUDA 设计的编程模型（`__shared__`、`__syncthreads`、`__shfl_sync` 在两边几乎同名）。所以两个类都继承自 TVM 的 `CodeGenC`，只 override「CUDA/HIP 特有」的部分。

#### 4.1.2 核心流程

一个后端 codegen 的产出流程（以 CUDA 为例）：

1. **实例化** `CodeGenTileLangCUDA cg; cg.Init(false);`
2. **逐函数印码**：遍历 IRModule 的每个 PrimFunc，`cg.AddFunction(gvar, f)`——它会印出函数签名、函数体，期间触发各种 `VisitExpr_` / `VisitStmt_` 回调。印码过程中会顺手把一些 `need_*` 标志置位（如「用到了 mma.h」「用到了 warp shuffle」）。
3. **收尾** `cg.Finish()`——把第 2 步攒下的 `need_*` 标志翻译成源码顶部的 `#include`，最后返回完整源码字符串。
4. **后处理**：把源码交给 Python 注册的 postproc 回调做最后一道清洗，再交给 compile 回调（`nvcc` / `hiprtc`）编译成二进制（`cubin` / `hsaco`）。
5. **打包**：把二进制与函数签名表一起，包成 TVM 的 `CUDAModule` / `ROCmModule` 返回。

#### 4.1.3 源码精读

**继承结构。** 两个类都 `final : public CodeGenC`，且都把 `IsScopePartOfType()` 写成返回 `false`——意思是 `__shared__` 这种作用域要作为类型前的「前缀修饰」印出，而不是塞进类型名里。

[src/cuda/codegen/codegen_cuda.h:23-24](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/codegen/codegen_cuda.h#L23-L24) — `CodeGenTileLangCUDA final : public CodeGenC`，CUDA codegen 的根。

[src/rocm/codegen/codegen_hip.h:22-26](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/rocm/codegen/codegen_hip.h#L22-L26) — `CodeGenTileLangHIP final : public CodeGenC`，并多了一个 `SetTarget(Target)`，把 target 存进成员 `target_` 供印码时查询（CUDA 不存 target，直接调用 `target_utils` 的自由函数）。

**「按需 include」机制。** codegen 头文件里挂了一长串 `bool need_*_` 标志，表示「这次印码有没有用到某个特性对应的头文件」。例如 CUDA 侧：

[src/cuda/codegen/codegen_cuda.h:101-156](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/codegen/codegen_cuda.h#L101-L156) — 一堆 `need_*_` 布尔标志（fp16/bf16/fp8、warp shuffle、各种 mma 指令头、barrier、TMA copy 等）。印码时谁用到谁就把对应标志置位。

这些标志在 `Finish()` 里被翻译成 `#include`：

[src/cuda/codegen/codegen_cuda.cc:580-683](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/codegen/codegen_cuda.cc#L580-L683) — `Finish()`：例如 `need_mma_instruction_h_` 为真就 `#include <tl_templates/cuda/instruction/mma.h>`，`enable_fp8_` 为真就 `#include <tl_templates/cuda/cuda_fp8.h>`。最后无条件 include `reduce.h` / `scan.h` 等公共头。

**作用域印法。** 这是两个 codegen 最直观的差异之一：

[src/cuda/codegen/codegen_cuda.cc:1475-1486](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/codegen/codegen_cuda.cc#L1475-L1486) — CUDA 的 `PrintStorageScope`：`shared` → `__shared__ __align__(16) `；`shared.dyn` → `extern __shared__ __align__(1024) `（动态共享内存）。

[src/rocm/codegen/codegen_hip.cc:736-746](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/rocm/codegen/codegen_hip.cc#L736-L746) — HIP 的 `PrintStorageScope`：逻辑几乎一致，但 CUDA 多了对 `shared.barrier` / `shared.cluster_barrier` 的处理（Hopper 的 mbarrier 专用），HIP 没有这些。

**build 入口与注册。** 真正实例化 codegen 的地方是 `rt_mod_*.cc`：

[src/cuda/codegen/rt_mod_cuda.cc:97-138](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/codegen/rt_mod_cuda.cc#L97-L138) — `BuildTileLangCUDA`：新建 `CodeGenTileLangCUDA`、对每个函数校验 `kDeviceKernelLaunch`、`AddFunction`、`Finish`，然后调 postproc/compile 回调，最后 `CUDAModuleCreateWithFallback` 打包。注意 postproc 回调（L118-120）正是 u4-l1 提到的「Python 注册、C++ 按名调用」的拦截点。

[src/cuda/codegen/rt_mod_cuda.cc:175-181](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/codegen/rt_mod_cuda.cc#L175-L181) — 把 `BuildTileLangCUDA` 注册成全局函数 `target.build.tilelang_cuda`。HIP 对应 `target.build.tilelang_hip`（[src/rocm/codegen/rt_mod_hip.cc:128-134](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/rocm/codegen/rt_mod_hip.cc#L128-L134)）。

**Python 侧对接。** 这两个全局函数被 Python 的 `DeviceCodegen` 引用：

[tilelang/cuda/codegen.py:14-22](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/codegen.py#L14-L22) — 把 target kind `cuda` 的 build 指向 `target.build.tilelang_cuda`。

[tilelang/rocm/codegen.py:6-12](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/rocm/codegen.py#L6-L12) — 把 target kind `hip` 的 build 指向 `target.build.tilelang_hip`。

> 小提示：CUDA 这里其实注册了**两条** `cuda` codegen——普通 cuda 与 cutedsl（CuTe DSL 后端），用 `supports_target` 谓词区分（`cutedsl` 是否在 `target.keys` 里）。HIP 只有一条。

#### 4.1.4 代码实践

**实践目标**：从「函数签名」看清两个 codegen 类的继承与 override 全貌，建立结构对照表。

**操作步骤**：
1. 打开 `src/cuda/codegen/codegen_cuda.h` 与 `src/rocm/codegen/codegen_hip.h` 并排阅读。
2. 列出两个类各自 override 的方法名（`PrintStorageScope`、`VisitExpr_(CallNode*)`、`BindThreadIndex` 等）。
3. 标出「CUDA 有、HIP 没有」的方法（如 `HandleLateIntrinsicCall`、`PrintVecStore`、`GetVecLoad`、`PrintWmmaScope`）和「HIP 有、CUDA 没有」的字段（如 `target_`、`SetTarget`）。

**需要观察的现象**：CUDA 类明显更大（.cc 约 5900 行 vs HIP 约 2229 行），override 的 visitor 更多——因为 NVIDIA 跨越多代架构（Volta→Blackwell）需要处理的专用指令（WMMA、WGMMA、TCGEN05、TMA、TMEM）远多于 ROCm。

**预期结果**：你能画出一棵「`CodeGenC` ← `CodeGenTileLangCUDA` / `CodeGenTileLangHIP`」的继承树，并能解释为何 CUDA 子类更臃肿。

**待本地验证**：若你在仓库里执行 `wc -l src/cuda/codegen/codegen_cuda.cc src/rocm/codegen/codegen_hip.cc`，应得到约 5900 / 2229 的行数对比。

#### 4.1.5 小练习与答案

**练习 1**：为什么 HIP codegen 需要一个 `target_` 成员并实现 `SetTarget`，而 CUDA 不需要？

**参考答案**：HIP 印码时需要根据具体芯片（如是否 gfx950）做分支，把 target 留在对象里查询更直接；CUDA 的能力判断全部封装在 `src/cuda/target_utils.cc` 的自由函数里，谁需要谁调用，故 codegen 对象本身不必持有 target。

**练习 2**：`IsScopePartOfType() const final { return false; }` 这行对生成代码有什么影响？

**参考答案**：它告诉基类 `CodeGenC`：`__shared__` 这类作用域不要拼进类型名（如 `__shared__ float`），而是作为独立前缀印出，符合 CUDA/HIP 的语法。

---

### 4.2 intrin_rule：可移植算子如何映射到后端内置函数

#### 4.2.1 概念说明

DSL 里有一批「数学/位运算」算子，比如 `rsqrt`（平方根倒数）、`exp`、warp shuffle。这些算子在 TIR 层用统一的名字 `tirx.rsqrt`、`tirx.tvm_warp_shuffle` 表示，**与后端无关**。但 `nvcc` / `hiprtc` 只认各自的名字：CUDA 是 `rsqrtf`、`__shfl_sync`，HIP 是 `__rsqrtf`、`__shfl_sync`。

`intrin_rule` 就是这张「同名翻译表」。它通过给算子挂 `<target>.FLowerIntrinsic` 属性，注册一条「把这个 `tirx.*` 调用改写成后端 extern 调用」的规则。这些规则在 `LowerIntrin` pass（见 `tilelang/engine/lower.py`）里被统一应用。

#### 4.2.2 核心流程

以 `tirx.rsqrt`（CUDA 路径）为例：

1. TIR 里出现 `tirx.rsqrt(x)`，其中 `x` 是 `float32`。
2. `LowerIntrin` pass 查 `tirx.rsqrt` 的 `cuda.FLowerIntrinsic` 属性，拿到 `DispatchPureExtern<CUDAMath>`。
3. `CUDAMath(float32, "rsqrt")` 返回字符串 `"rsqrtf"`（见下）。
4. 规则把调用改写成 `call_pure_extern("rsqrtf", x)`。
5. codegen 的 `PrintCallExtern` 看到 `call_pure_extern`，直接印出 `rsqrtf(x)`。

关键：**真正的「字符串拼接」逻辑在这些 `CUDAMath` / `HIPMath` 结构体的 `operator()` 里**，它根据 dtype 的位宽决定加什么后缀。

#### 4.2.3 源码精读

**后缀拼接逻辑。** CUDA 与 HIP 的逻辑几乎逐字相同（这也是为什么本节代码两份很像）：

[src/cuda/codegen/intrin_rule_cuda.cc:19-59](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/codegen/intrin_rule_cuda.cc#L19-L59) — `CUDAMath::operator()`：`float64` 返回原名（如 `exp`）；`float32` 加 `'f'`（`expf`）；`float16`/`bfloat16` 加 `'h'` 前缀（`hexp`，`fabs` 特例为 `__habs`）；`int32` 加 `"__"` 前缀，`int64` 加 `"__ll"` 后缀。

[src/cuda/codegen/intrin_rule_cuda.cc:61-70](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/codegen/intrin_rule_cuda.cc#L61-L70) — `CUDAFastMath`：在 `CUDAMath` 基础上，对 `float32` 改用 fast-math 版（`__expf`，多两个下划线、更快但精度略低）。

**warp shuffle 的映射。** 这是「同名不同前缀」的典型：

[src/cuda/codegen/intrin_rule_cuda.cc:108-119](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/codegen/intrin_rule_cuda.cc#L108-L119) — `CUDAWarpIntrinsic`：把通用的 `tvm_warp_shuffle` 映射成 `tirx.cuda.__shfl_sync`。

[src/rocm/codegen/intrin_rule_hip.cc:136-147](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/rocm/codegen/intrin_rule_hip.cc#L136-L147) — `HIPWarpIntrinsic`：同样的通用 op，映射成 `tirx.hip.__shfl_sync`。最终 codegen 印出的都是 `__shfl_sync(...)`，但走的是各自的「底层 op」。

[src/rocm/codegen/intrin_rule_hip.cc:284-294](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/rocm/codegen/intrin_rule_hip.cc#L284-L294) — HIP 还显式注册了 `tirx.hip.__shfl_sync` 这个底层 op，并用 `TGlobalSymbol` 把它的全局符号定为 `"__shfl_sync"`，这样 codegen 就知道印出这个名字。

**CUDA 与 HIP 的注册数量差异。** 这是本节最值得注意的不对称：

[src/cuda/codegen/intrin_rule_cuda.cc:153-158](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/codegen/intrin_rule_cuda.cc#L153-L158) — CUDA 的 intrin_rule 只显式注册了 `tirx.rsqrt` 和 `tirx.isfinite` 两条规则。其余数学函数的规则复用了 TVM 内置的 cuda intrin rule（定义结构体 `CUDAMath` 等是给本文件内的两条规则与后续复用准备的）。

[src/rocm/codegen/intrin_rule_hip.cc:166-281](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/rocm/codegen/intrin_rule_hip.cc#L166-L281) — HIP 反过来，**逐个显式注册**了 `floor`/`ceil`/`exp`/`log`/`sin`/`cos`/`sqrt`/`pow`……一大批函数。原因见下一个精读点。

**HIP 的向量化难点。** 为什么 HIP 要费这么大劲逐个注册？关键在 `DispatchPureExternScalarized`：

[src/rocm/codegen/intrin_rule_hip.cc:27-47](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/rocm/codegen/intrin_rule_hip.cc#L27-L47) — HIP 没有向量化版本的 fp32 数学内置函数（没有 `exp2(float4)`，只有标量 `exp2f(float)`）。所以当 dtype 是向量时，这条规则改成「按元素类型算出名字，发射一个 `call_pure_extern`」，再由 HIP codegen 的 `PrintCallExtern` 展开成「每个 lane 一次标量调用」。文件顶部的注释（L18-26）把这个设计决策写得很清楚。

> 小结：CUDA 后端「省事」（复用 TVM 规则 + 仅补两条），HIP 后端「费事」（因向量数学函数缺失，需逐个注册并标量化）。这是多后端里典型的「看似对称、实则后端各有坑」。

#### 4.2.4 代码实践

**实践目标**：跟踪一个数学算子从 TIR 到生成源码的完整改写路径。

**操作步骤**：
1. 在 [src/rocm/codegen/intrin_rule_hip.cc:195-197](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/rocm/codegen/intrin_rule_hip.cc#L195-L197) 找到 `tirx.exp` 的注册，确认它用 `HIPFastMath`。
2. 顺着 `HIPFastMath`（L91-100）→ 对 `float32` 返回 `"__" + "exp" + "f"` = `"__expf"`。
3. 想象 `tirx.exp(float32x4 x)`：因 dtype 是向量，走 `DispatchPureExternScalarized`，先取 `element_of()` 得 `float32`，再算出 `__expf`，改写成 `call_pure_extern("__expf", x)`。
4. 确认 `LowerIntrin` 在 [tilelang/engine/lower.py:285](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/lower.py#L285) 与 [tilelang/engine/lower.py:294](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/lower.py#L294) 分别对 host_mod、device_mod 跑过——这就是上述规则被触发的时机。

**需要观察的现象**：同一份 kernel，CUDA 侧会生成 `expf(...)`（或 fast-math 的 `__expf`），HIP 侧会生成对每个 lane 的 `__expf(...)`。

**预期结果**：你能在脑中画出 `tirx.exp` →（FLowerIntrinsic 规则）→ `call_pure_extern` →（codegen `PrintCallExtern`）→ 源码文本 的三段式管道。

#### 4.2.5 小练习与答案

**练习 1**：`CUDAMath` 对 `float16` 的 `fabs` 为什么特判成 `__habs` 而不是 `hfabs`？

**参考答案**：CUDA/HIP 的 half 精度库中绝对值内置函数名是 `__habs`，并不存在 `hfabs`；这是后端实际 API 名字决定的特例。

**练习 2**：如果某个 `tirx.foo` 算子在 CUDA 上没有注册任何 `cuda.FLowerIntrinsic` 规则，会发生什么？

**参考答案**：`LowerIntrin` 找不到规则就保持原样，codegen 的 `VisitExpr_(CallNode*)` 又不认识 `tirx.foo`，最终会在生成源码时报错（无法印出该调用）。这正是「每接入一个新数学算子，常需要补 intrin_rule」的原因。

---

### 4.3 target_utils：按架构检测 GPU 特性

#### 4.3.1 概念说明

codegen 和很多 lowering pass 在生成指令前，都需要先问一句：「这块卡支持 X 吗？」——支持 Volta 的 WMMA？支持 Ampere 的 cp.async？warp_size 是 32 还是 64？这些「能力查询」全部集中在 `target_utils.cc`。

这些函数有三个特点：
1. **纯函数**：只读 target 属性，不改状态。
2. **按架构判代际**：CUDA 看 `arch="sm_XX"` 的数字，HIP 看 `mcpu="gfxXXX"` 的字符串前缀。
3. **经 FFI 暴露**：注册成 `tl.TargetIsCuda` / `tl.TargetIsHopper` 等全局函数，供 Python 侧（如 `tilelang/cuda/target.py`、`tilelang/rocm/target.py`）和 C++ 侧共同调用。

#### 4.3.2 核心流程

CUDA 判代际的统一入口是 `GetCudaArchInt`：把 `"sm_80"` 解析成整数 `80`。然后每个 `TargetIsXxx` 就是判断这个整数落在哪个区间。

HIP 判代际没有「整数」可解析，而是直接看 `mcpu` 字符串：以 `gfx9` 开头就是 CDNA（计算卡，有矩阵核），以 `gfx11`/`gfx12` 开头是 RDNA（消费卡），`gfx950` 是支持 FP4 的特例芯片。

warp_size（一个 warp 的线程数）也由这里回答：CUDA 恒为 32，HIP 在 CDNA 上是 64、RDNA 上是 32。（MACA 后端恒为 64，见 u7。）

#### 4.3.3 源码精读

**CUDA 架构解析。**

[src/cuda/target_utils.cc:19-27](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/target_utils.cc#L19-L27) — `GetCudaArchInt`：校验 `arch` 必须以 `sm_` 开头，取后缀转成整数。

[src/cuda/target_utils.cc:43-83](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/target_utils.cc#L43-L83) — 一族 `TargetIsVolta/Turing/Ampere/Hopper/Sm100/SM120`，各自判断 arch 整数落在哪个半开区间（如 Hopper = `[90,100)`，Sm100/Blackwell = `[100,110]`）。

[src/cuda/target_utils.cc:92-95](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/target_utils.cc#L92-L95) — `TargetCudaGetWarpSize`：忽略 target，恒返回 32。

**HIP 芯片识别。**

[src/rocm/target_utils.cc:22-31](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/rocm/target_utils.cc#L22-L31) — `TargetIsCDNA`：`mcpu` 以 `gfx9` 开头即为 CDNA。

[src/rocm/target_utils.cc:33-42](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/rocm/target_utils.cc#L33-L42) — `TargetIsRDNA`：`mcpu` 以 `gfx11` 或 `gfx12` 开头。

[src/rocm/target_utils.cc:44-52](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/rocm/target_utils.cc#L44-L52) — `TargetIsGfx950`：`mcpu` 含 `gfx950`（唯一支持 FP4 的 ROCm 芯片）。

[src/rocm/target_utils.cc:67-72](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/rocm/target_utils.cc#L67-L72) — `TargetRocmGetWarpSize`：CDNA 返回 64，否则 32。这是 HIP 与 CUDA 在 warp_size 上的本质差异。

**FFI 暴露。**

[src/cuda/target_utils.cc:258-288](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/target_utils.cc#L258-L288) — `TVM_FFI_STATIC_INIT_BLOCK`：把上述函数注册成 `tl.TargetIsCuda`、`tl.TargetIsHopper`、`tl.TargetCudaGetWarpSize`、`tl.TargetHasLdmatrix`、`tl.TargetHasBulkCopy` 等全局函数，供 Python 调用。

[src/rocm/target_utils.cc:87-102](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/rocm/target_utils.cc#L87-L102) — ROCm 版的 FFI 注册：`tl.TargetIsCDNA`、`tl.TargetIsRDNA`、`tl.TargetIsGfx950`、`tl.TargetRocmGetWarpSize` 等。

**两个「附加能力」函数（仅 CUDA）。** CUDA 的 target_utils 还回答了几个 ROCm 没有的细粒度问题：

[src/cuda/target_utils.cc:97-122](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/target_utils.cc#L97-L122) — `TargetHasLdmatrix`（arch≥75，Turing+）、`TargetHasStmatrix`（Hopper+ 或 Blackwell 的 m16n8 形态）、`TargetHasTmem`（仅 Blackwell SM100 的张量内存）、`TargetHasBulkCopy`（arch≥90，Hopper+ 的 TMA）。这些都是为张量核指令选择与 TMA 拷贝服务的。

#### 4.3.4 代码实践

**实践目标**：制作「CUDA vs HIP 能力检测对照表」，把本模块的知识沉淀下来。

**操作步骤**：
1. 通读 [src/cuda/target_utils.h:15-37](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/target_utils.h#L15-L37) 与 [src/rocm/target_utils.h:14-23](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/rocm/target_utils.h#L14-L23)，把两个头文件里声明的所有函数抄进一张表。
2. 对每个函数标注：判据来源（CUDA 的 `arch` 整数 / HIP 的 `mcpu` 字符串）、典型返回值。
3. 标出「只有 CUDA 有」「只有 HIP 有」「两边都有」三栏。

**需要观察的现象**：CUDA 侧函数更多、更细（因为架构代际多、专用指令多）；HIP 侧更少，但多出一个 `TargetGetRDNAGeneration`（RDNA 代际号）。

**预期结果**：一张类似下表的对照表（部分）：

| 能力 | CUDA 判据 | HIP 判据 |
| --- | --- | --- |
| 是否该后端 | `kDLCUDA` | `kDLROCM` |
| 代际划分 | arch ∈ sm_70/75/80/90/100/120 | mcpu 前缀 gfx9 / gfx11 / gfx12 / gfx950 |
| warp_size | 恒 32 | CDNA=64，RDNA=32 |
| 异步拷贝 | arch ≥ 80（Ampere+） | CDNA 且 gfx ≥ 94 |

#### 4.3.5 小练习与答案

**练习 1**：`TargetIsCDNA` 用字符串前缀 `gfx9` 判断，那 `gfx950` 会不会被误判成「不是 CDNA」？

**参考答案**：不会。`mcpu.find("gfx9") == 0` 只看开头四个字符，`gfx950` 同样以 `gfx9` 开头，所以既是 CDNA 也是 Gfx950（后者是前者的特例）。

**练习 2**：为什么 `TargetCudaGetWarpSize` 的参数 `target` 被标了 `(void)target;` 故意忽略？

**参考答案**：因为 CUDA 所有架构的 warp_size 都是 32，与具体卡无关；保留 target 参数是为了和 ROCm 版函数签名对称，便于上层统一调用。

---

### 4.4 异步拷贝检测：能力探测如何驱动指令选择

#### 4.4.1 概念说明

「异步拷贝（async copy）」指 GPU 直接从 global memory 把数据搬到 shared memory、不经过寄存器的硬件机制——CUDA 叫 `cp.async`（Ampere+）/ TMA（Hopper+），HIP/ROCm 叫 buffer load（CDNA gfx90a+）。它能和计算重叠，是软件流水线（u4-l4）能真正隐藏访存延迟的前提。

但**不是所有卡都支持**。所以编译器在决定「这次 `T.copy` 用普通循环搬还是用 cp.async 搬」之前，必须先问 target_utils：你支持异步拷贝吗？本模块就是讲这条「能力探测 → 指令选择」的链路。

#### 4.4.2 核心流程

1. 后端各自的 `TargetXxxHasAsyncCopy(target)` 给出 yes/no。
2. 一个**跨后端统一**的 `TargetHasAsyncCopy(target)`（在 `backend/common/target_utils.cc`）按 target 类型分发到上面那个函数——这样「与后端无关」的 pass 只需调这一个。
3. 高层 pass（如 `PipelinePlanning`、`InjectSoftwarePipeline`）用它决定「要不要规划异步生产者」；后端 op 层（如 `copy_analysis`、`copy.cc`）用它决定「这次 copy 降级成 cp.async 还是普通循环」。

#### 4.4.3 源码精读

**两边的判据。**

[src/cuda/target_utils.cc:85-90](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/target_utils.cc#L85-L90) — `TargetCudaHasAsyncCopy`：arch ≥ 80，即 Ampere 及以后才有 `cp.async`。

[src/rocm/target_utils.cc:54-65](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/rocm/target_utils.cc#L54-L65) — `TargetRocmHasAsyncCopy`：必须是 CDNA，且 `gfx` 版本号 ≥ 94（即 gfx94x / gfx950 等），取 `mcpu.substr(3,2)` 解析成整数判断。

**统一分发。** 这是 u5-l1 提到的「公共层 + 后端自有层」分层在能力检测上的体现：

[src/backend/common/target_utils.cc:15-26](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/backend/common/target_utils.cc#L15-L26) — `TargetHasAsyncCopy`：依次判断 CUDA / ROCm / MACA，分别转发到各自的 `TargetXxxHasAsyncCopy`。注意它已经把 MACA 也接进来了——这就是 metax 分支「把 maca 抬成与 cuda/hip 平级后端」的一个落点。

[src/backend/common/target_utils.cc:28-33](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/backend/common/target_utils.cc#L28-L33) — 把这个统一函数注册成 `tl.TargetHasAsyncCopy`，供 Python 侧（如 `tilelang/cuda/target.py` 的 `target_has_async_copy`）调用。

**消费方 1：高层 pipeline pass。** 与后端无关的软件流水线 pass 用统一函数：

[src/transform/pipeline_planning.cc:838](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/pipeline_planning.cc#L838) — `if (!TargetHasAsyncCopy(target_) || !use_async_copy_)`：不支持异步拷贝（或被手动关掉）就不把生产者标成异步。回看 u4-l4：metax 分支正是借这条路径在规划阶段对 MACA 关闭异步拷贝。

**消费方 2：后端 op 层的 copy 降级。** CUDA 与 HIP 各自的 copy 分析用它决定选不选 cp.async：

[src/cuda/op/copy_analysis.cc:357-368](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/op/copy_analysis.cc#L357-L368) — `CheckCPAsyncCopy`：先 `TargetCudaHasAsyncCopy(target)`，不支持直接返回 false；再校验「global→shared、dtype 一致」等前置条件。

[src/rocm/op/copy.cc:189-196](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/rocm/op/copy.cc#L189-L196) — HIP 版 `CheckCPAsyncCopy`：用 `TargetRocmHasAsyncCopy(target)` 做同样闸门。两边逻辑对称，只是判据函数不同。

> 直觉：能力检测是「开关」，copy 降级是「用开关选指令」。一个 `T.copy(A_shared, A_frag)` 到底变成 `cp.async` 还是普通 `for` 循环，完全由这个开关决定——这正是 u4 系列里「自动选搬运指令」的最终落点。

#### 4.4.4 代码实践

**实践目标**：用 `get_kernel_source` 直接观察「异步拷贝能力」对生成代码的影响。

**操作步骤**（需有 CUDA 环境，无环境则改为源码阅读型，见下）：
1. 写一个最简的 `T.copy(global_buf, shared_buf)` kernel，分别用 `target={"kind":"cuda","arch":"sm_80"}`（支持 cp.async）和 `target={"kind":"cuda","arch":"sm_75"}`（不支持）编译。
2. 用 `kernel.get_kernel_source()` 打印两份设备源码。
3. 对照 [src/cuda/op/copy_analysis.cc:357-368](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/op/copy_analysis.cc#L357-L368) 解释你看到的差异。

**需要观察的现象**：sm_80 的源码里应出现 `tl::cp_async_gs<...>(...)`（见 [src/cuda/codegen/codegen_cuda.cc:2370-2392](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/codegen/codegen_cuda.cc#L2370-L2392)）；sm_75 的源码里则是一个普通的向量化 `for` 拷贝循环。

**预期结果**：你亲眼看到「同一个 `T.copy`，因 arch 不同而印出完全不同的指令」——这就是 target_utils 驱动 codegen 的实证。

**待本地验证**：若无 GPU/编译环境，改为阅读 [src/cuda/codegen/codegen_cuda.cc:2370-2392](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/codegen/codegen_cuda.cc#L2370-L2392)（CUDA 把 `ptx_cp_async` 印成 `tl::cp_async_gs`）与 [src/rocm/codegen/codegen_hip.cc:1173-1192](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/rocm/codegen/codegen_hip.cc#L1173-L1192)（HIP 同样印成 `tl::cp_async_gs`），说明两后端在「支持异步拷贝时」复用了同一套 `tl::` 模板名。

#### 4.4.5 小练习与答案

**练习 1**：`TargetRocmHasAsyncCopy` 为什么要 `gfx_version >= 94` 而不是简单地「CDNA 即支持」？

**参考答案**：早期 CDNA（如 gfx906/gfx90a，对应 gfx90）虽然有矩阵核，但硬件异步 global→shared 拷贝（buffer load 用于此）是在 gfx94 之后才完善的；故用版本号 94 作为门槛，避免在不支持的卡上误发异步指令。

**练习 2**：`TargetHasAsyncCopy` 这个统一函数为什么不直接写在 cuda/rocm 各自的 target_utils 里，而要放到 `backend/common/`？

**参考答案**：因为它要被「与后端无关」的高层 pass（`pipeline_planning`、`inject_pipeline`）调用。如果放在某个后端目录里，高层 pass 就得反向依赖具体后端，破坏「公共层不依赖后端自有层」的分层（见 u5-l1）。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「双后端 codegen 逆向阅读」。

**任务**：选一个最简 GEMM（可复用 `examples/gemm/example_gemm.py`），分别用 `target={"kind":"cuda","arch":"sm_80"}` 和 `target={"kind":"hip","mcpu":"gfx90a"}` 编译（无设备时用 `*_without_compile` 路径，只取源码），各取一份设备源码，然后完成下面四件事：

1. **找继承落点**：在两份源码顶部找到 codegen `Finish()` 印出的 `#include` 列表。CUDA 的应包含 `tl_templates/cuda/...`（见 [src/cuda/codegen/codegen_cuda.cc:580-683](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/codegen/codegen_cuda.cc#L580-L683)），HIP 的应少很多——印证 4.1 里「CUDA codegen 更臃肿」。
2. **找 intrin 落点**：在源码里搜索 `__shfl_sync` 或某个数学函数（如 `expf`/`__expf`），回溯它来自 [src/cuda/codegen/intrin_rule_cuda.cc:108-119](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/codegen/intrin_rule_cuda.cc#L108-L119) 或 [src/rocm/codegen/intrin_rule_hip.cc:136-147](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/rocm/codegen/intrin_rule_hip.cc#L136-L147) 的那条规则。
3. **找能力检测落点**：把 CUDA 的 arch 改成 `sm_75` 重编，对比 GEMM 里 K 维搬运是否从 `cp.async` 退化为普通循环，用 [src/cuda/target_utils.cc:85-90](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/target_utils.cc#L85-L90) 解释原因。
4. **画一张链路图**：从 `@T.prim_func` 一直画到 `nvcc`/`hiprtc`，标出 codegen 类、`target.build.*` 全局函数、postproc 回调、target_utils 检测各自发生在哪一步。

**验收标准**：你能不查讲义，对着自己画的链路图，讲清「为什么同一份 TileLang kernel 在 sm_80 和 gfx90a 上生成的源码几乎同名（`__shared__`、`__shfl_sync`、`cp.async`）但来自两套独立的 codegen 与 intrin_rule」。

## 6. 本讲小结

- `CodeGenTileLangCUDA` / `CodeGenTileLangHIP` 都 `final : public CodeGenC`，override 一批 visitor 来处理 GPU 专有构造（共享内存作用域、warp shuffle、张量核），由 `rt_mod_*.cc` 的 `BuildTileLang*` 实例化，并注册为全局函数 `target.build.tilelang_cuda/hip` 供 Python `DeviceCodegen` 调用。
- codegen 用「按需 include」机制：印码时置位的 `need_*` 标志，在 `Finish()` 里翻译成源码顶部的 `#include`。
- `intrin_rule` 通过 `<target>.FLowerIntrinsic` 属性，把可移植的 `tirx.*` 算子改写成后端 extern 调用（`rsqrtf`、`__shfl_sync` 等），在 `LowerIntrin` pass 中触发；CUDA 复用 TVM 默认规则只补两条，HIP 因缺少向量数学内置函数需逐个注册并标量化。
- `target_utils.cc` 提供「按架构检测能力」的纯函数：CUDA 看 `arch` 整数判代际、HIP 看 `mcpu` 字符串前缀；warp_size 在 CUDA 恒 32、HIP CDNA 为 64；这些函数经 FFI 暴露为 `tl.TargetIs*`。
- 异步拷贝能力由 `TargetXxxHasAsyncCopy` 检测，再由公共层 `TargetHasAsyncCopy` 统一分发；它既驱动高层 pipeline pass 是否规划异步生产者，又驱动后端 copy op 选 cp.async 还是普通循环——是「自动选搬运指令」的最终落点。
- 多后端在表层高度对称（命名、作用域几乎一致），但细节各有坑：CUDA 跨代际多、codegen 更大；HIP 缺向量数学、warp_size 因架构而异。

## 7. 下一步学习建议

- 想看「张量核指令」在 codegen 里到底怎么印出来，进入 **u6-1（MMA intrinsics 总览）** 与 **u6-2（GEMM intrinsics 深入）**，它们会展开 `tl_templates/cuda/instruction/` 下 WGMMA/TCGEN05 的封装与本讲提到的 `need_*mma_instruction_h` 标志背后的细节。
- 想理解本 fork 的核心——MACA 后端如何照着 CUDA/HIP 的这套模式实现一遍，进入 **u7-1（MACA 后端架构总览）** 与 **u7-2（MACA codegen）**，可对照本讲验证「三后端结构对称」的结论。
- 想动手加一个新后端或新算子，进入 **u9-1（新增目标后端）** 与 **u9-2（新增 tile 算子）**，那里会把本讲的 `target.build.*` 注册、`DeviceCodegen` 注册、`target_utils` 分发串成一个完整的扩展清单。
