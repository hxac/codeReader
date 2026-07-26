# Ascend C / PTO 双 Codegen

## 1. 本讲目标

本讲紧接 [u6-l1 编译 Pass 全景与配置](u6-l1-pass-overview.md)，把镜头对准 Pass 流水线的最末端——`device_codegen`：经过两阶段 Pass 改写后「已经合法且优化好的 TIR」是如何被翻译成可在昇腾 NPU 上编译运行的 C++ 源码的。

学完本讲，你应当能够：

1. 说清楚 tile-lang 在昇腾上有 **ascendc**（Ascend C / Catlass）与 **pto**（PTO IR）两条 codegen 路线的定位与差异。
2. 掌握 `target.model` 是如何分发到两个不同的 TVM 注册函数 `target.build.tilelang_ascend` / `target.build.tilelang_ascend_pto`，再进入对应的 `CodeGen` 类。
3. 理解同一个 TIR intrinsic（如 `tl::ascend_add`）在两条路线里被翻译成风格完全不同的 C++：一边是 `AscendC::Add` 类方法，另一边是 `TADD` 这样的 PTO 指令宏。
4. 读懂 `AddFunction`（生成设备侧 kernel 函数）与 `PrintHostFunc`（生成 host 侧 `call` 启动器）这对孪生函数的作用。
5. 能用 `get_kernel_source()` 取出两份生成代码，并解释头文件、命名空间、函数前缀与 bisheng 编译选项上的区别。

---

## 2. 前置知识

本讲默认你已经读过 [u6-l1](u6-l1-pass-overview.md)，知道 `lower()` 先跑 `LowerAndLegalize`、再跑 `OptimizeForTarget`，最后调用 `device_codegen`。这里补充几个本讲要用的概念：

- **TIR intrinsic（内建）**：前端 `T.copy`、`T.gemm_v0`、`+`、`reduce_max` 等原语，经过前面的 Pass 后会变成 TIR 里的 `tir.Call` 节点，其 `op` 是一组预注册的 builtin（如 `tl::ascend_add`、`tl::ascend_gemm_v0`）。Codegen 的工作就是把这些 builtin 翻译成具体的 C++ 代码。
- **Codegen（代码生成）**：把 IRModule「打印」成某种源码字符串的过程。tile-lang 复用 TVM 的 `CodeGenC` 基类（生成 C 风格源码），再针对不同后端派生子类。
- **Ascend C**：华为官方为昇腾 NPU 提供的 C++ 算子编程接口，核心是 `AscendC::` 命名空间下的类方法（`Add`、`Mmad`、`DataCopy`、`SetFlag` …）。catlass 是 tile-lang 在其上的模板封装。
- **PTO IR**：一条更贴近硬件指令的中间表示路线，源码以 `pto::` 命名空间下的指令宏（`TADD`、`TMUL`、`mma` …）和 `pto-inst.hpp` 提供的 ISA 为基础，便于做 A5 仿真与指令级调试。
- **bisheng（毕昇编译器）**：CANN 提供的设备编译器，负责把 codegen 产出的 C++ 源码编成 `.so`。ascendc 路线用 `-xasc`，pto 路线用 `-xcce`（详见 [u6-l4 运行时加载与 Bisheng 设备编译](u6-l4-runtime-bisheng.md)）。
- **CSourceModuleCreate**：TVM runtime 的一种 `Module`，把一段 C/C++ 源码连同函数名列表包起来，交给后续的设备编译器处理。

> 一句话直觉：两条路线 **输入相同（同一份优化后的 TIR）、输出同形（都是可被 bisheng 编译的 C++ 源码）**，只是「翻译目标语言」不同——ascendc 译成 Ascend C 类 API，pto 译成 PTO 指令宏。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `tilelang/engine/lower.py` | `device_codegen()`：按 `target.model` 把 IRModule 分发到两个注册函数 |
| `src/target/rt_mod_ascend.cc` | 注册 `target.build.tilelang_ascend`，调用 `CodeGenTileLangAscend` 生成源码 |
| `src/target/rt_mod_ascend_pto.cc` | 注册 `target.build.tilelang_ascend_pto`，调用 `CodeGenTileLangAscendPto` |
| `src/target/codegen_ascend.h` / `.cc` | Ascend C 路线的 Codegen 类（Catlass / AscendC 风格） |
| `src/target/codegen_ascend_pto.h` / `.cc` | PTO 路线的 Codegen 类（PTO IR 风格） |
| `tilelang/jit/adapter/libgen.py` | bisheng 命令构造：`-xasc`（ascendc）与 `-xcce`（pto）两套编译选项 |
| `CMakeLists.txt` | `USE_ASCEND` 开关把上述 4 个 ascend 源文件编进 `libtilelang.so` |
| `examples/gemm/example_gemm_pto_developer.py` | pto 路线的 GEMM 示例（`target="pto"`） |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：① 双 Codegen 的定位与分发；② Codegen 类骨架与 `AddFunction` / `PrintHostFunc`；③ intrinsic → 指令的映射；④ 头文件、命名空间与 bisheng 编译差异。

### 4.1 双 Codegen 的定位与分发（target.build 注册）

#### 4.1.1 概念说明

tile-lang 的设备 codegen 不直接产出二进制，而是产出一 **份 C++ 源码字符串**，再交给 bisheng 编译。对昇腾而言，这「同一份 TIR」可以翻译成两种「方言」：

- **ascendc**（默认、稳妥主线）：翻译成 Ascend C 的 `AscendC::` 类 API，依赖 catlass 模板库。功能最全，是大多数算子的默认选择。
- **pto**（更新路线）：翻译成 PTO IR 的指令宏与 `pto::Tile*` 模板，依赖 `pto-isa` 子模块。它更贴近硬件指令，便于做 **A5 仿真**（见 [u7-l5 A5 仿真运行](u7-l5-camodel-sim.md)）与指令级调试（`TL_PTO_DEBUG`，见 [u7-l4 调试与性能分析](u7-l4-debug-profiling.md)）。

两条路线共享前端 DSL、共享全部 Pass（u6-l1），只是在最后一跳 `device_codegen` 分道扬镳。

#### 4.1.2 核心流程

分发链路如下（伪代码）：

```
JITKernel.compile()
   └─ tilelang.lower()
        └─ LowerAndLegalize()     # 两阶段 Pass（u6-l1）
        └─ OptimizeForTarget()
        └─ device_codegen(mod, target, platform)   # 本讲入口
              ├─ if target.model in {"ascendc","auto"}:
              │     target.build.tilelang_ascend(mod, target, platform)
              │        └─ CodeGenTileLangAscend  →  "Ascend C 源码"
              └─ elif target.model == "pto":
                    target.build.tilelang_ascend_pto(mod, target, platform)
                       └─ CodeGenTileLangAscendPto → "PTO IR 源码"
```

关键点：

1. 分发的唯一依据是 `target.model`；`"auto"` 会落到 ascendc。
2. 两个 `target.build.*` 都是 TVM 注册的全局函数（FFI），Python 侧用 `tvm._ffi.get_global_func` 取出调用。
3. 两条路线都返回 `CSourceModuleCreate(code, "c", function_names)`，即「源码 + 函数名列表」，形态一致。

#### 4.1.3 源码精读

分发逻辑在 [tilelang/engine/lower.py:L159-L170](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/lower.py#L159-L170)：`device_codegen` 先对 device_mod 做一次 `Simplify`，再按 `target.model` 取出对应全局函数执行。

[tilelang/engine/lower.py:L162-L168](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/lower.py#L162-L168) 是核心分支，中文说明：`ascendc` 与 `auto` 走 `target.build.tilelang_ascend`，`pto` 走 `target.build.tilelang_ascend_pto`，其它直接报错。

两个注册函数分别住在两个 `rt_mod_*.cc` 中，结构几乎一模一样。先看 ascendc：

[src/target/rt_mod_ascend.cc:L9-L32](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/rt_mod_ascend.cc#L9-L32)：`BuildTileLangAscend` 新建一个 `CodeGenTileLangAscend(platform)`，对 IRModule 里每个 `PrimFunc` 调 `AddFunction` 收集函数名，最后 `Finish()` 拿到完整源码，包成 `CSourceModuleCreate`。文件末尾 `TVM_REGISTER_GLOBAL("target.build.tilelang_ascend")` 把它注册成全局函数名，这就是 Python 侧 `get_global_func` 能取到的原因。

[src/target/rt_mod_ascend_pto.cc:L9-L32](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/rt_mod_ascend_pto.cc#L9-L32) 是 pto 的对照版：换成 `CodeGenTileLangAscendPto`，注册名变成 `target.build.tilelang_ascend_pto`。两个文件除类名与注册名外完全对称。

这 4 个文件（两个 codegen + 两个 rt_mod）只有开启 `USE_ASCEND` 时才会被编译进 `libtilelang.so`，见 [CMakeLists.txt:L130-L138](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/CMakeLists.txt#L130-L138)（中文说明：`if(USE_ASCEND)` 把 4 个 ascend 源文件 glob 后追加到 `TILE_LANG_SRCS`）。这正是 [u1-l2 环境准备与安装构建](u1-l2-install-and-build.md) 提到的「Ascend 专用代码由 `USE_ASCEND` 隔离」在 codegen 层的落点。

#### 4.1.4 代码实践

**实践目标**：确认两个 `target.build.*` 注册函数确实存在，并理解 `target.model` 的取值如何决定走哪条路线。

**操作步骤**：

1. 在仓库根目录打开 `tilelang/engine/lower.py`，定位 `device_codegen`。
2. 用只读检索确认两个注册名：在 `src/target/` 下搜索 `TVM_REGISTER_GLOBAL("target.build.tilelang_`，应能看到 ascendc 与 pto 两条。
3. 对照 [examples/gemm/example_gemm_pto_developer.py:L24](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_pto_developer.py#L24)，确认 pto 示例用 `target="pto"` 触发 pto 路线。

**需要观察的现象**：两个 `rt_mod_*.cc` 的函数体几乎逐行对称，只有类名与注册名不同。

**预期结果**：能够画出「`target.model` → 注册函数名 → Codegen 类」的三级映射表。

> 说明：实际执行编译需要已构建的 tilelang（`USE_ASCEND=ON`）与 CANN 环境，本实践以源码阅读为主，运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `target.model` 设成一个既不是 `ascendc`/`auto` 也不是 `pto` 的值（比如 `"cuda"`），`device_codegen` 会发生什么？

**参考答案**：在 [lower.py:L166-L168](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/engine/lower.py#L166-L168) 落入 `else` 分支，先 `print(target.kind.name)` 再 `raise ValueError`。

**练习 2**：为什么 `auto` 会被当作 `ascendc` 处理？

**参考答案**：分支条件写成 `target.model == "ascendc" or target.model == "auto"`，即 `auto` 显式并入 ascendc，作为未指定时的稳妥默认主线。

---

### 4.2 Codegen 类骨架与 AddFunction / PrintHostFunc

#### 4.2.1 概念说明

两个 Codegen 类都继承自 TVM 的 `CodeGenC`，核心机制是 **visitor（访问者）模式**：对 TIR 树的每种节点重载一个 `VisitExpr_` / `VisitStmt_`，把节点「打印」成 C++ 文本。本模块先看它们共有的「骨架」——一次 kernel 翻译要产出 **两个** C++ 函数：

- **设备函数** `<symbol>_kernel(...)`：真正跑在 AI Core 上的算子体，由 `AddFunction` 生成。
- **host 启动器** `extern "C" void call(...)`：跑在 CPU 侧，负责取 `aclrtStream`、算 tiling、再用 `<<<core, nullptr, stream>>>` 语法把设备函数 launch 出去，由 `PrintHostFunc` 生成。

这正对应 [u1-l5 JIT 即时编译与运行总流程](u1-l5-jit-and-pipeline.md) 提到的「codegen 同时生成设备函数与 host 启动器 `call`」。

#### 4.2.2 核心流程

`AddFunction` 的统一节奏（两条路线一致）：

```
AddFunction(gvar, f):
  1. DeclareFunction(gvar, f)          # 前向声明（已声明过则 no-op）
  2. InitFuncState(f)                  # 清空上一轮状态
  3. 读取 PrimFunc 属性：global_symbol / address_map /
     use_swizzle / buffer shapes / npu_cv_ratio 等
  4. PrintFuncPrefix + 打印函数签名（参数 + shape_vars + tiling + fftsAddr）
  5. PreFunctionBody(f)                # 函数体前的固定初始化（因路线而异）
  6. PrintStmt(f->body)                # 递归打印 kernel 主体（visitor 入口）
  7. PrintHostFunc(...)                # 追加 host 侧 call() 启动器
```

注意第 4 步的函数签名末尾都带一个 `uint64_t fftsAddr`（FFTS 快速通信地址），它由 host 侧 `rtGetC2cCtrlAddr` 取得并传入，是昇腾 kernel 启动的固定约定。

#### 4.2.3 源码精读

先看 ascendc 的构造与前缀：构造函数把 `restrict_keyword_` 设成 `"GM_ADDR"`（全局内存指针的限定符），`PrintFuncPrefix` 打印 `extern "C" __global__ __aicore__ `，见 [src/target/codegen_ascend.cc:L92-L99](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L92-L99)（中文说明：ascendc 的设备函数以 `__aicore__` 标注，表示跑在 AI Core 上）。

`AddFunction` 的主体在 [src/target/codegen_ascend.cc:L1184-L1293](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L1184-L1293)。关键片段：

- L1192-L1210：取出 `global_symbol`、`address_map`、`use_swizzle`、`tiling_map`、`var_sequence`、`buffer_shapes`、`npu_cv_ratio` 等 PrimFunc 属性。
- L1212-L1215：打印前缀与函数名 `<symbol>_kernel(`。
- L1264-L1288：在参数列表后追加 `int64_t` 形态的 shape 变量、tiling 变量，以及固定的 `uint64_t fftsAddr`。
- L1290-L1293：`PreFunctionBody(f)` → `BeginScope` → `PrintStmt(f->body)` → `EndScope`。

`PreFunctionBody` 是 ascendc 路线独有的「重头戏」：它在每个 kernel 体最前面打印一堆 Ascend C 的缓冲初始化代码。见 [src/target/codegen_ascend.cc:L974-L1040](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L974-L1040)，中文说明：

- L977-L981：按 `cv_ratio_` 选 `KERNEL_TYPE_MIX_AIC_1_1` 或 `_1_2`（即 1:1 / 1:2 的 Cube:Vector 配比，承接 [u5-l3 Vid 消除与自动 CV 配比](u5-l3-vid-reduction.md)）。
- L983：声明 `AscendC::TPipe pipe;`——Ascend C 管理片上缓冲的总入口。
- L988-L995：为每个全局参数声明 `AscendC::GlobalTensor<dtype>` 并 `SetGlobalBuffer` 绑定 GM 地址。
- L1000-L1035：按平台（A5 或 A2/A3）用不同尺寸常量 `InitBuffer` 出 `ascend_l0a / ascend_l0b / ascend_l1 / ascend_l0c / ascend_ub` 五块片上缓冲。这些名字正是后续 `GemmOpCodegen` 等会引用的缓冲句柄。

`PrintHostFunc` 在 [src/target/codegen_ascend.cc:L1131-L1182](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L1131-L1182)：它先生成一个 `<name>_tiling(...)` 辅助函数（把运行时 shape 换算成 tiling 参数），再打印 `extern "C" void call(..., aclrtStream stream)`，体内调 `rtGetC2cCtrlAddr(&fftsAddr, &fftsLen)` 取 FFTS 地址，最后用 `name<<<core, nullptr, stream>>>(args..., fftsAddr)` 把设备函数 launch 出去。

pto 路线的 `AddFunction` 结构相同但细节不同，见 [src/target/codegen_ascend_pto.cc:L3911-L3976](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend_pto.cc#L3911-L3976)。最显眼的差别在 L3932-L3937：pto 的 `PrintFuncPrefix` 是空函数（前缀留空），转而在这里直接打印 `extern "C" __global__ AICORE`（注意是 `AICORE` 而非 `__aicore__`）；并且会为每个全局张量记录 `global_tensor_template`（区分 static / dynamic shape），供后续 GM 拷贝模板使用。

pto 的 `PreFunctionBody` 要轻得多：见 [src/target/codegen_ascend_pto.cc:L3809-L3822](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend_pto.cc#L3809-L3822)，只做一次参数地址表 `copy_base_addr_map_` 的填充——因为 PTO 路线的缓冲初始化（`Tile*` 模板）是延迟、按需在 visitor 里生成的，而不是在函数开头一次性 `InitBuffer`。

pto 的 `PrintHostFunc` 在 [src/target/codegen_ascend_pto.cc:L3857-L3909](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend_pto.cc#L3857-L3909)：同样生成 `call(..., void *stream)`，体内取 `fftsAddr` 后 `name<<<core, nullptr, stream>>>(...)`。与 ascendc 的差别是它 **暂未实现 tiling 辅助函数**（注释里写 `reserved for future tiling support`），所以参数更简单。

#### 4.2.4 代码实践

**实践目标**：理解「一次 codegen 产出两个函数」这一结构。

**操作步骤**：阅读 [src/target/codegen_ascend.cc:L1184-L1293](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L1184-L1293)，找到：① 设备函数名是如何由 `global_symbol + "_kernel"` 拼出来的（L1214）；② `fftsAddr` 这个参数从哪里来、又被 `PrintHostFunc` 里哪一行传入设备函数。

**需要观察的现象**：设备函数签名里的 `fftsAddr` 与 host 函数里 `rtGetC2cCtrlAddr` 取到的 `fftsAddr` 是同一个值。

**预期结果**：能画出「host `call()` → `<<<>>>` launch → 设备 `_kernel()`」的参数传递图，标注 `fftsAddr` 与 `stream` 的流向。运行结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：ascendc 的 `PreFunctionBody` 在函数开头 `InitBuffer` 了五块缓冲，pto 却没有。pto 的缓冲（如 L0A/L0B）在哪里生成？

**参考答案**：pto 走「按需延迟生成」——在 visitor 访问到具体算子（如 `MmaCodegen`）时，通过 `ResolveCubeSliceName` + `CreateCubeVariable` 等辅助函数现场生成 `pto::TileMatL0A` 等模板变量，见 4.3 节。

**练习 2**：`call` 函数为什么必须是 `extern "C"`？

**参考答案**：它要被 Python 侧经 ctypes/cython 用符号名 `call` 直接 `dlsym` 加载并调用，C 链接保证名字不被 C++ name-mangling 破坏（承接 [u1-l5](u1-l5-jit-and-pipeline.md) 的 `lib.call` 调用）。

---

### 4.3 intrinsic → 指令映射（VisitExpr_ CallNode）

#### 4.3.1 概念说明

这是两条 codegen 路线 **最核心的差异**。经过前面的 Pass，kernel 体里的计算都变成了对 builtin intrinsic 的 `tir.Call`，例如：

- `tl::ascend_add`：元素级加法
- `tl::ascend_gemm_v0` / `tl::ascend_mma`：矩阵乘
- `tl::ascend_set_flag` / `tl::ascend_pipe_barrier`：同步
- `tl::ascend_reduce`：reduce

两个 Codegen 都在重载的 `VisitExpr_(const CallNode*, ...)` 里用一长串 `if/else if (op->op.same_as(...))` 把这些 intrinsic 一一拦截，再翻译成各自方言的 C++。**同一个 intrinsic，两条路线翻译出的字符串完全不同：**

| intrinsic | ascendc 翻译 | pto 翻译 |
| --- | --- | --- |
| `tl::ascend_add` | `AscendC::Add(...)` | `TADD(...)` |
| `tl::ascend_mul` | `AscendC::Mul(...)` | `TMUL(...)` |
| `tl::ascend_exp` | `AscendC::Exp(...)` | `TEXP(...)` |
| `tl::ascend_set_flag` | `AscendC::SetFlag(...)` | `set_flag(...)` |
| `tl::ascend_mma` | 调 `MmaCodegen` → catlass `mma` 模板 | 调 `MmaCodegen` → `pto::mma<TileMatL0A,TileMatL0B,TileAcc>` |

#### 4.3.2 核心流程

visitor 分发的统一形态：

```
VisitExpr_(CallNode op):
  if op.op == builtin::call_extern:        # 形如 "tl::ascend::copy_*" 的外部调用
       CopyCodegen / CallExternCodegen
  elif op.op == tl::ascend_add():  BinaryVecOpCodegen(op, "<方言算子名>")
  elif op.op == tl::ascend_gemm_v0(): GemmOpCodegen / GemmV0Codegen
  elif op.op == tl::ascend_mma():   MmaCodegen
  ... (数十条)
```

两条路线的分支顺序与覆盖范围大致对应，只是每个分支里传入的「方言算子名」不同。

#### 4.3.3 源码精读

ascendc 的总分发在 [src/target/codegen_ascend.cc:L503-L702](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L503-L702)。几个典型映射：

- L516-L517：`tl::ascend_add` → `BinaryVecOpCodegen(op, "AscendC::Add")`。
- L660-L661：`tl::ascend_gemm_v0` → `GemmOpCodegen(op)`。
- L688-L689：`tl::ascend_mma` → `MmaCodegen(op)`。
- L654-L655：`tl::ascend_set_flag` → `FlagOpCodegen(op, "AscendC::SetFlag")`。

其中 `GemmOpCodegen` 直接把 intrinsic 拼成 `tl::ascend::<模板名>(A[off], B[off], C[off], ascend_l0a, ascend_l0b, M, N)`——注意它引用了 4.2 节 `PreFunctionBody` 里 `InitBuffer` 出来的 `ascend_l0a / ascend_l0b` 句柄。见 [src/target/codegen_ascend.cc:L2485-L2506](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L2485-L2506)。

pto 的总分发在 [src/target/codegen_ascend_pto.cc:L837-L926](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend_pto.cc#L837-L926)。同样的 intrinsic，翻译目标换成 PTO 指令宏：

- L879-L880：`tl::ascend_add` → `BinaryVecOpCodegen(op, "TADD")`。
- L883-L884：`tl::ascend_mul` → `BinaryVecOpCodegen(op, "TMUL")`。
- L853-L854：`tl::ascend_exp` → `UnaryVecOpCodegen(op, "TEXP")`。
- L845-L846：`tl::ascend_gemm_v0` → `GemmV0Codegen(op)`。

pto 的 `MmaCodegen` 在 [src/target/codegen_ascend_pto.cc:L4126-L4150](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend_pto.cc#L4126-L4150)：它把 intrinsic 自带的模板串补上 K 维，再调 `ResolveCubeSliceName` 把 A/B/C 解析成 `pto::TileMatL0A / TileMatL0B / TileAcc` 这类 PTO 模板变量名，最终打印 `tl::ascend_pto::mma<...>(a, b, c, ...)`。与 ascendc 相比，它不依赖预先 `InitBuffer` 的固定句柄，而是按 slice 信息现场解析缓冲名——这就是 4.2.5 练习 1 的答案。

> 一个有用的对照视角：ascendc 是「**对象方法**」风格（`AscendC::Add(dst, src1, src2)`，先有 Tensor 对象再调方法）；pto 是「**指令宏**」风格（`TADD(dst, src1, src2)`，更像汇编助记符）。两者最终都由 bisheng 编成同一批硬件指令，语义等价。

#### 4.3.4 代码实践

**实践目标**：亲手追踪一个 intrinsic 在两条路线里的不同翻译。

**操作步骤**：

1. 在 `src/op/builtin.h`（或 `src/op/ascend.h`）里确认 `tl::ascend_add`、`tl::ascend_mma` 等 builtin 的定义存在（只读检索）。
2. 在 [codegen_ascend.cc:L503-L702](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L503-L702) 里找到 `tl::ascend_add` 的分支，记下它传入的 `"AscendC::Add"`。
3. 在 [codegen_ascend_pto.cc:L837-L926](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend_pto.cc#L837-L926) 里找到同一个 intrinsic，记下它传入的 `"TADD"`。

**需要观察的现象**：拦截的是 **同一个** `op->op.same_as(tl::ascend_add())`，但下游传给 `BinaryVecOpCodegen` 的算子名字符串不同。

**预期结果**：能填写本节开头的对照表，并解释「同 intrinsic、不同方言」的含义。运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么两条路线能共用同一套 TIR intrinsic，而不用为 pto 单独定义一套 `tl::ascend_pto_add`？

**参考答案**：intrinsic 是 **后端无关** 的 TIR 标记，定义在 `src/op/` 里；它只描述「语义是什么」，不规定「译成什么」。译成哪种方言是 codegen 的职责，由 `VisitExpr_` 分支决定。这样前面所有 Pass 只需写一遍（见 u6-l1 的「先让它对」），两条 codegen 路线共享同一份优化后 TIR。

**练习 2**：pto 的 `GemmV0Codegen` 为什么要从模板串里 parse 出 `M/N/K/transpose/kL0Size`（[codegen_ascend_pto.cc:L1818-L1835](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend_pto.cc#L1818-L1835)）？

**参考答案**：因为 PTO 的 L1→L0 分段（`kL0split`/`kL0Tail`）需要在 codegen 时显式展开成多次 L0 搬运 + mma，而 ascendc 路线把这些细节藏在 catlass 的 `gemm` 模板内部、对用户透明（承接 [u3-l3 矩阵计算](u3-l3-gemm-mma.md) 对 `gemm_v0` 与 `mma` 的区分）。

---

### 4.4 头文件、命名空间与 bisheng 编译差异

#### 4.4.1 概念说明

两条路线产出的 C++ 源码，在「**开头 include 什么、用哪个命名空间、用什么编译器选项**」上截然不同。这些差异由两个地方共同决定：

1. **Codegen 的 `Finish()`**：在源码最前面拼上 include 与 `using namespace`。
2. **`libgen.py` 的 bisheng 命令**：决定用 `-xasc` 还是 `-xcce`、链接哪些模板库头文件。

#### 4.4.2 核心流程

```
Finish()                      # codegen 收尾，拼 include + using
   └─ 返回完整 C++ 源码字符串
        └─ 写入临时 .cpp
              └─ libgen.py compile_lib()
                   └─ bisheng -xasc / -xcce ... → .so
```

#### 4.4.3 源码精读

ascendc 的 `Finish()` 在 [src/target/codegen_ascend.cc:L101-L114](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend.cc#L101-L114)，中文说明：在源码顶部 include `tl_templates/ascend/common.h`、`acl/acl.h`、`runtime/rt_ffts.h`，并 `using namespace Catlass;`——这就是为什么后面能直接写 `AscendC::Add`、`tl::ascend::gemm`（catlass 在 AscendC 之上的封装）。

pto 的 `Finish()` 在 [src/target/codegen_ascend_pto.cc:L470-L489](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/target/codegen_ascend_pto.cc#L470-L489)：A5 平台会先 `#define PTO_PLATFORM_A5`，再 include `tl_templates/pto/common.h` 与 `<pto/pto-inst.hpp>`（PTO ISA 头），若用到 `dump_tensor` 则额外 include `tl_templates/pto/printf.h`，最后 `using namespace pto;`。

两侧 bisheng 命令的对比在 `libgen.py`。ascendc 命令见 [tilelang/jit/adapter/libgen.py:L152-L183](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L152-L183)：用 `--npu-arch=dav-2201`、`-xasc`，并 `-I` 引入 `3rdparty/catlass/include` 与 `3rdparty/shmem/...`，定义 `-DBACKEND_HYBM`。pto 命令见 [tilelang/jit/adapter/libgen.py:L184-L228](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/adapter/libgen.py#L184-L228)：按平台选 `--cce-aicore-arch=dav-c310`（A5）或 `dav-c220`（A2/A3），用 `-xcce`，`-I` 引入 `3rdparty/pto-isa/include`，并定义 `REGISTER_BASE`（A5）或 `MEMORY_BASE`、`-DL2_CACHE_HINT`，还带一组 `-mllvm -cce-aicore-*` 的代码生成旋钮。

把两条路线的差异汇总成一张表：

| 维度 | ascendc（Ascend C / Catlass） | pto（PTO IR） |
| --- | --- | --- |
| 头文件 | `tl_templates/ascend/common.h` | `tl_templates/pto/common.h` + `<pto/pto-inst.hpp>` |
| 命名空间 | `Catlass::`（内含 `AscendC::`） | `pto::`（即 `tl::ascend_pto::`） |
| 设备函数前缀 | `extern "C" __global__ __aicore__` | `extern "C" __global__ AICORE` |
| 指令风格 | C++ 类方法 `AscendC::Add` / `Mmad` | 指令宏 `TADD` / `mma<TileMatL0A,...>` |
| 缓冲初始化 | 函数开头一次性 `TPipe::InitBuffer` | 访问算子时按需 `Tile*` 模板 |
| bisheng 语言 | `-xasc`，`--npu-arch=dav-2201` | `-xcce`，`--cce-aicore-arch=dav-c220/c310` |
| 第三方模板库 | `3rdparty/catlass`、`3rdparty/shmem` | `3rdparty/pto-isa` |
| 适用场景 | 默认主线，功能最全 | A5 仿真、指令级调试（`TL_PTO_DEBUG`） |

（模板库本身的细节留到 [u6-l3 tl_templates 模板库与第三方 ISA](u6-l3-templates.md) 展开。）

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：用同一个 GEMM `prim_func`，分别以 `target='ascendc'` 和 `target='pto'` 编译，取出两份生成的 C++ 源码，对比头文件、命名空间、函数前缀与算子指令名的差异。这是本讲规格里指定的实践任务。

**操作步骤**：

1. 准备一个最小 GEMM kernel（可基于 [examples/gemm/example_gemm.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm.py) 与 [examples/gemm/example_gemm_pto_developer.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_pto_developer.py)）。
2. ascendc 版：用默认 target（或显式 `target="ascendc"`）：

   ```python
   # 示例代码：仅供说明用法，非项目原有文件
   @tilelang.jit(out_idx=[-1], target="ascendc")
   def matmul_ascendc(M, N, K, block_M, block_N, K_L1, ...):
       ...
       return kernel  # 内含 @T.prim_func

   @tilelang.jit(out_idx=[-1], target="pto")
   def matmul_pto(M, N, K, block_M, block_N, K_L1, ...):
       ...  # 同样的 kernel 体

   a = torch.randn(M, K, dtype=torch.float16, device="npu")
   b = torch.randn(K, N, dtype=torch.float16, device="npu")
   f_asc = matmul_ascendc(M, N, K, 128, 128, 128)
   f_pto = matmul_pto(M, N, K, 128, 128, 128)
   _ = f_asc(a, b)   # 首次调用触发 JIT 编译
   _ = f_pto(a, b)

   src_a = f_asc.get_kernel_source()
   src_p = f_pto.get_kernel_source()
   ```

3. 把两份源码分别存盘，用 `diff` 或肉眼对比以下四处：
   - 文件开头的 `#include` 行；
   - `using namespace` 行；
   - 设备函数的声明前缀（`__aicore__` vs `AICORE`）；
   - kernel 体里的算子调用（`AscendC::Mul` / `tl::ascend::gemm` vs `TMUL` / `tl::ascend_pto::mma<...>`）。

**需要观察的现象**：

- ascendc 源码顶部应有 `#include "tl_templates/ascend/common.h"` 与 `using namespace Catlass;`；pto 源码顶部应有 `#include "tl_templates/pto/common.h"`、`#include <pto/pto-inst.hpp>` 与 `using namespace pto;`。
- ascendc 的矩阵乘会引用 `ascend_l0a / ascend_l0b`（`PreFunctionBody` 里 `InitBuffer` 出来的句柄）；pto 的矩阵乘会出现 `pto::TileMatL0A / TileMatL0B / TileAcc`。
- 同样的元素级乘法，一边是 `AscendC::Mul(...)`，另一边是 `TMUL(...)`。

**预期结果**：得到一张如 4.4.1 表格所示的对照，并能指认每处差异分别由哪个函数（`Finish()` / `PreFunctionBody()` / `VisitExpr_()` / `libgen.py`）决定。

> **待本地验证**：本实践需要 (1) 已用 `USE_ASCEND=ON` 构建的 tilelang；(2) CANN/毕昇环境与一块昇腾 NPU（或 A5 仿真环境，但仿真仅支持 pto，见 [u7-l5](u7-l5-camodel-sim.md)）。在没有硬件时，可退化为「源码阅读型实践」：直接对照 4.4.3 给出的 `Finish()` 与 `libgen.py` 片段，推断两份源码的开头差异，而不实际运行。

#### 4.4.5 小练习与答案

**练习 1**：如果只用 pto 路线生成源码、却用 ascendc 的 bisheng 选项（`-xasc`）去编译，会发生什么？

**参考答案**：pto 源码里 `using namespace pto;`、`pto::TileMatL0A`、`<pto/pto-inst.hpp>` 等符号在 `-xasc`（Ascend C 编译模式）下找不到定义，bisheng 会在编译期报一堆未定义符号/头文件缺失错误。语言选项（`-xasc`/`-xcce`）必须与 codegen 路线严格配对。

**练习 2**：`Finish()` 里 pto 对 A5 平台额外 `#define PTO_PLATFORM_A5`，这个宏最可能影响什么？

**参考答案**：它会让 `tl_templates/pto/common.h` / `pto-inst.hpp` 里的模板走 A5 专属的指令或寄存器布局分支（如 A5 的 flag 同步语义、`workspace_name` 是否为空，见 `PipeInfo::workspace_name` 的注释「empty for A5」），与 [u7-l5 A5 仿真](u7-l5-camodel-sim.md) 衔接。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「**全链路 codegen 对照**」：

1. **选一个算子**：以 GEMM 为例（也可选 elementwise add，更简单）。
2. **两路编译**：分别用 `target="ascendc"` 与 `target="pto"` 各编译一次（参考 4.4.4 的脚本骨架）。
3. **取源码**：对两个 kernel 调 `get_kernel_source()`，各存一份 `.cpp`。
4. **分层对照**，给每处差异标注「责任人」：
   - **头文件/命名空间层** → `Finish()`（4.4）；
   - **函数前缀/签名层** → `PrintFuncPrefix()` + `AddFunction()`（4.2）；
   - **缓冲初始化层** → `PreFunctionBody()`（4.2，ascendc 在此 `InitBuffer`，pto 在此只填地址表）；
   - **指令翻译层** → `VisitExpr_(CallNode)` 的分支（4.3，`AscendC::Add` vs `TADD`）；
   - **编译选项层** → `libgen.py` 的 `-xasc` / `-xcce`（4.4）。
5. **验证语义等价**：在真机或仿真上跑两份 kernel，确认输出一致（精度允许范围内）。

**预期产出**：一份对照报告，含两段源码截图/摘录与一张「差异 → 责任函数」映射表。这张表同时也是你后续阅读 [u6-l3 模板库](u6-l3-templates.md) 与 [u6-l4 运行时/bisheng](u6-l4-runtime-bisheng.md) 的索引。

> 综合 practice 的实际运行依赖硬件环境，无硬件时聚焦「源码对照」即可，运行结果待本地验证。

---

## 6. 本讲小结

- tile-lang 在昇腾上有 **ascendc**（Ascend C/Catlass，默认主线）与 **pto**（PTO IR，支持 A5 仿真与指令级调试）两条 codegen 路线，二者 **输入相同（同一份优化后 TIR）、输出同形（C++ 源码 + `.so`）**，只是翻译方言不同。
- 分发由 `device_codegen` 按 `target.model` 决定：`ascendc`/`auto` → `target.build.tilelang_ascend` → `CodeGenTileLangAscend`；`pto` → `target.build.tilelang_ascend_pto` → `CodeGenTileLangAscendPto`，两者都返回 `CSourceModuleCreate`。
- 两个 Codegen 类都继承 `CodeGenC`，用 visitor 打印；每次 kernel 翻译产出 **设备函数 `_kernel`**（`AddFunction`）与 **host 启动器 `call`**（`PrintHostFunc`）两个 C++ 函数，由 `<<<core, nullptr, stream>>>` 串起。
- 最核心的差异是 `VisitExpr_(CallNode)`：**同一个 TIR intrinsic** 在 ascendc 译成 `AscendC::Add`/`tl::ascend::gemm` 等「类方法」风格，在 pto 译成 `TADD`/`tl::ascend_pto::mma<TileMatL0A,...>` 等「指令宏」风格。
- 两份源码的开头（include、`using namespace`、函数前缀）由各自的 `Finish()` 决定；最终的 bisheng 编译选项（`-xasc` vs `-xcce`、链接 catlass/shmem 还是 pto-isa）由 `libgen.py` 决定，二者必须配对。
- `get_kernel_source()` 是观察整条 codegen 链路最直接的窗口。

---

## 7. 下一步学习建议

- **[u6-l3 tl_templates 模板库与第三方 ISA](u6-l3-templates.md)**：本讲反复提到的 `tl_templates/ascend/common.h`、`tl_templates/pto/common.h` 与 `catlass/pto-isa/shmem` 子模块到底封装了什么，下一讲逐个打开。
- **[u6-l4 运行时加载与 Bisheng 设备编译](u6-l4-runtime-bisheng.md)**：本讲止步于「产出 `.cpp`」，下一讲追完最后一公里——`libgen.py` 调 bisheng 编出 `.so`、ctypes/cython 加载、`call` 符号经 `aclrtStream` 启动。
- **想立刻看真实生成代码**：直接运行 [examples/gemm/example_gemm_pto_developer.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_pto_developer.py) 并在末尾 `print(func.get_kernel_source())`，对照本讲 4.3/4.4 的表格逐行验证。
- **想理解某条 intrinsic 从哪来**：回看 [u3-l5 Element-wise 与 T.Parallel](u3-l5-parallel.md)（`tl::ascend_*` 的前端来源）与 [u3-l3 矩阵计算](u3-l3-gemm-mma.md)（`tl::ascend_gemm_v0` / `mma` 的定义），把「前端原语 → Pass → intrinsic → codegen」整条链补全。
