# u6-l4 收尾 Pass：样板生成、参数合法化与核类型检测

## 1. 本讲目标

上一讲我们读完了 optimizing 阶段的同步重建链。本讲进入 `postprocessing`（收尾）阶段的最后一批 Pass。学完本讲，你应该能够：

1. 回答这个问题：最终 dump 出的 `ascendc.cpp` 里，那些**用户从没写过**的代码——`#include`、`extern "C" __global__ __aicore__` 入口、`set_ffts_base_addr(...)`、`#pragma pack` 的 `struct` 定义——分别是谁生成的？答案是：**它们不是 Python 字符串拼接出来的，而是 Pass 在 IR 上“种”好，再由发射层照着 IR 打印出来的**。
2. 讲清 kernel 参数 ABI 的完整闭环：`LegalizeKernelArgs` 在 kernel 形参上打 `emitasc.kernel_arg` 属性 → 前端用 `ir.get_kernel_arg_attrs` 读出 → `Launcher` 按属性种类注入隐藏实参（如 `ffts_addr`）。
3. 理解模块属性（`asc.compile_mix`、`asc.enable_debug`）是**后端向前端回传信息**的通道：C++ Pass 写属性，Python 编译器读属性，再据此决定毕昇编译命令与运行时行为。

本讲会顺带澄清一个容易误解的点：`extern "C" __global__ __aicore__` 入口**不是** `GenerateBoilerplatePass` 生成的——它由发射层依据 `ascendc.global` 属性打印。看清“谁种属性、谁消费属性”正是本讲的核心训练。

## 2. 前置知识

- **postprocessing 阶段**：`compiler.py` 把 Pass 流水线分成 lowering / optimizing / postprocessing 三段（见 u3-l4、u6-l1）。本讲的五个 Pass 全部在 postprocessing 段，排在同步重建之后，职责是“为发射 Ascend C 铺路”。
- **`ascendc.global` 属性**：codegen 给 Kernel 函数（被 `kernel[核数, 流]` 启动的那个）的 `func.func` 打上的 UnitAttr，是区分 Kernel 与 Device 子函数的标志（见 u4-l4）。`PrivatizeFunc` 会把没有该属性的函数标记为 private。
- **emitc 方言**：MLIR 上游的“生成 C 代码”方言，`emitc.include` 直接对应一条 `#include` 语句；emitasc 方言是 pyasc 自研的“贴近 C 语法”的低层方言（见 u6-l6 预习，本讲只用到其中几个 Op）。
- **FFTS**：昇腾上 Cube 核与 Vector 核之间做核间同步（cross-core sync）的机制。MIX（混合）核既编 cube 目标又编 vec 目标，两族核要用 FFTS 旗标握手；`SetFFTSBaseAddr` / `ffts_cross_core_sync` 是对应的 Ascend C 接口。
- **Struct 参数**：Host 侧用户定义的 `asc.Struct` 子类，经 ctypes 打包成字节流、以指针形式传给 kernel（见 u3-l3）；设备侧用 `create_local()` 拷贝出本地副本再读写成员。
- **kernel ABI**：kernel 形参在 launch 时被拼成 8 字节对齐的连续字节流下发（见 u3-l6）。本讲要补的是：这份形参表里除了用户显式参数，还有一个 `ffts_addr` 隐藏参数。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `python/asc/runtime/compiler.py` | `_schedule_postprocessing` 装填本讲全部 Pass；`run_passes` 末尾读模块属性推导 `kernel_type` 与 `enable_debug` |
| `lib/Dialect/Asc/Transforms/GenerateBoilerplatePass.cpp` | 在模块头部插入 `emitc.include`（按需选择头文件） |
| `lib/Dialect/Asc/Transforms/DefineCubeOnlyPass.cpp` | `matmul_cube_only` 时插入 `#define ASCENDC_CUBE_ONLY` 并打模块属性（本讲顺带读） |
| `lib/Dialect/Asc/Transforms/LegalizeKernelArgs.cpp` | 给 kernel 形参打 `emitasc.kernel_arg` 属性，追加 `ffts_addr` 隐藏参数并插桩 |
| `lib/Dialect/Asc/Transforms/DeclarePyStructPass.cpp` | 收集 IR 中全部 `PyStructType`，在模块头插入结构体声明 Op |
| `lib/Dialect/Asc/Transforms/DetectKernelTypePass.cpp` | 检测 Matmul，打 `asc.compile_mix` 模块属性 |
| `lib/Dialect/Asc/Transforms/DetectEnableDebugPass.cpp` | 检测 printf/dump_tensor，打 `asc.enable_debug` 模块属性 |
| `include/ascir/Dialect/Asc/Transforms/Passes.td` | 上述 Pass 的声明（注册名、作用域、构造函数） |
| `include/ascir/Dialect/Asc/Utils/Attributes.h` | 属性名字符串常量的唯一定义处 |
| `lib/Target/AscendC/External/Func.cpp` | 发射层：按 `ascendc.global` 打印 `extern "C" __global__ __aicore__` 入口 |
| `lib/Target/AscendC/External/Emitc.cpp` | 发射层：把 `emitc.include` 打成 `#include` 语句 |
| `lib/Target/AscendC/EmitAsc.cpp` | 发射层：把 `emitasc.declare_py_struct` 打成 `#pragma pack` 结构体 |
| `lib/Target/AscendC/Basic/OtherOps.cpp` | 发射层：`set_ffts_base_addr` / `ffts_cross_core_sync` 的打印 |
| `python/src/IR.cpp` | `getKernelArgAttrs`：从 IR 读出参数 ABI 表交还 Python |
| `python/asc/runtime/launcher.py` | 按参数 ABI 表注入 `ffts_addr` 与 debug 缓冲实参 |
| `python/asc/language/core/struct.py` | Struct 前端：`_pack_ = 8` 的 ctypes 镜像与 `PyStructType` 构造 |

## 4. 核心概念与源码讲解

先看这批 Pass 在流水线里的位置与顺序。[python/asc/runtime/compiler.py:219-230](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L219-L230) 中的 `_schedule_postprocessing` 按固定顺序装填：

```
add_declare_py_struct        ← 结构体声明
add_generate_boilerplate     ← include 样板
(可选) add_define_cube_only  ← matmul_cube_only 时
add_legalize_kernel_args     ← 参数 ABI 合法化 + ffts 插桩
add_detect_kernel_type       ← 打 asc.compile_mix
add_detect_enable_debug      ← 打 asc.enable_debug
(可选) add_verify_sync / add_strip_debug_info
```

记住这个顺序有助于理解两个衔接点：`DeclarePyStruct` 必须在发射前把结构体声明种进模块；`DetectKernelType`/`DetectEnableDebug` 必须放在最后，因为它们检测的是**经过前面所有 Pass 改写之后**的最终 IR。

### 4.1 GenerateBoilerplate：样板代码生成

#### 4.1.1 概念说明

一份能通过毕昇编译的 Ascend C 源文件，开头必须有 `#include "kernel_operator.h"` 这样的头文件；用了 Matmul 或 ListTensor 还要额外include 对应接口头。用户在 Python kernel 里从没写过这些 include——`GenerateBoilerplatePass` 的职责就是把它们以 `emitc.include` Op 的形式插到 IR 模块头部，让发射层“照单打印”。

这里要澄清一个常见误解：**`extern "C" __global__ __aicore__` 的 kernel 入口不是这个 Pass 生成的**。入口行由发射层打印 `func::FuncOp` 时依据 `ascendc.global` 属性决定（4.1.3 第二段）。换句话说，最终样板的生成是“两条腿”：

1. Pass 在 IR 里**种内容**（include Op、属性、隐藏参数）；
2. 发射层**按属性打印**（`ascendc.global` → `extern "C" __global__`，`emitc.include` → `#include`）。

#### 4.1.2 核心流程

```text
遍历模块，探测四类特征 Op：
  有 ListTensorDescOp / ListTensorDescV2Op / TensorDescOp
      → 需要 "kernel_operator_list_tensor_intf.h"
  有 RegistMatmulObjOp
      → 需要 "lib/matmul_intf.h"
无条件：
  → 需要 "kernel_operator.h"
把所需 emitc.include 逐个插到模块块首
```

注意所有 include 都用 `atBlockBegin` 插入，因此**后创建的 include 在最终文件里反而靠前**——`kernel_operator.h` 最先创建，最终通常排在最后。C/C++ 里这些头文件互相自包含，顺序不影响正确性，但读 dump 文件时不要因为顺序和创建顺序相反而困惑。

#### 4.1.3 源码精读

Pass 主体：[lib/Dialect/Asc/Transforms/GenerateBoilerplatePass.cpp:31-53](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/GenerateBoilerplatePass.cpp#L31-L53)

- 第 36-37 行：`ImplicitLocOpBuilder::atBlockBegin` 在模块体块首建 `emitc::IncludeOp("kernel_operator.h")`，这是每个 kernel 必需的 Ascend C 主头文件。
- 第 38-44 行：`mod.walk(...)` 配合 `WalkResult::interrupt()` 做“存在性探测”——只要遇到一个 `ListTensorDescOp` 或 `ListTensorDescV2Op` 就中断遍历，说明 kernel 用了列表张量接口，需要额外 include `kernel_operator_list_tensor_intf.h`。
- 第 45-48 行：同理，存在 `RegistMatmulObjOp`（Matmul 对象注册，见 u7-l1）时 include `lib/matmul_intf.h`。
- 第 49-52 行：存在 `TensorDescOp` 时也 include `kernel_operator_list_tensor_intf.h`。

发射侧：`emitc.include` 如何变成 C 语句，见 [lib/Target/AscendC/External/Emitc.cpp:55-67](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Emitc.cpp#L55-L67)。该函数按 `isStandardInclude` 决定用尖括号还是引号；pyasc 插的三个头都用引号形态，所以最终文件里是 `#include "kernel_operator.h"`。

再看入口行的真正来源：[lib/Target/AscendC/External/Func.cpp:63-77](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Func.cpp#L63-L77)。第 63 行读 `ascendc::attr::global`（即 `ascendc.global`，定义在 [include/ascir/Dialect/Asc/Utils/Attributes.h:23](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Utils/Attributes.h#L23)）；第 66-67 行据此二选一：

- Kernel 函数（有该属性）：`extern "C"  __global__ __aicore__`
- Device 子函数：`__inline__ __attribute__((always_inline)) __aicore__`（对应 u4-l4 讲过的发射层内联前缀）

随后第 68-77 行打印返回类型、函数名与形参列表。所以准确的说法是：**“extern 声明与 `__aicore__` 入口”由发射层依据 codegen 时期就存在的 `ascendc.global` 属性打印；`GenerateBoilerplatePass` 负责的是 include 这部分样板**。

Pass 声明（TableGen 三件套之一）：[include/ascir/Dialect/Asc/Transforms/Passes.td:38-42](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td#L38-L42)，注册名 `ascendc-generate-boilerplate`，作用域 `ModuleOp`，依赖 emitc 方言——这就是它能在模块上建 `emitc.include` 的前提。

#### 4.1.4 代码实践

1. **实践目标**：亲眼确认 include 样板由 Pass 决定，并观察“特征 Op → 头文件”的映射。
2. **操作步骤**：
   - 在已安装 pyasc 的环境里执行：
     ```bash
     mkdir -p /tmp/pyasc-dump
     export PYASC_DUMP_PATH=/tmp/pyasc-dump
     python3 examples/01_add/add.py -r Model
     head -5 /tmp/pyasc-dump/ascendc.cpp
     ```
   - 再运行 Matmul 示例（需要 Model 模式支持）：
     ```bash
     python3 examples/03_matmul_mix/matmul_mix.py -r Model
     head -8 /tmp/pyasc-dump/ascendc.cpp
     ```
3. **需要观察的现象**：01_add 的文件头部只有 `#include "kernel_operator.h"`；03_matmul_mix 多出 `#include "lib/matmul_intf.h"`（因为 IR 里有 `RegistMatmulObjOp`），且 include 的排列顺序与 Pass 创建顺序相反。
4. **预期结果**：两个示例的 `ascendc.cpp` 第一行附近都能找到 kernel_operator.h；Matmul 版本额外包含 matmul_intf.h。同时在任一文件中定位 `extern "C"  __global__ __aicore__` 行，记住它来自 Func.cpp:66-67 而非本 Pass。具体输出文本**待本地验证**（取决于安装环境与平台参数）。

#### 4.1.5 小练习与答案

**练习 1**：如果用户 kernel 用了 `asc.data_copy` 的 ND↔NZ 形态（涉及 ListTensorDesc），最终 `ascendc.cpp` 会多出哪一行？由哪行代码决定？

答案：会多出 `#include "kernel_operator_list_tensor_intf.h"`，由 [GenerateBoilerplatePass.cpp:38-44](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/GenerateBoilerplatePass.cpp#L38-L44) 的 `hasListTensorDesc || hasListTensorDescV2` 分支决定（`TensorDescOp` 走第 49-52 行的等价分支）。

**练习 2**：为什么 `GenerateBoilerplatePass` 要声明 `dependentDialects = ["emitc::EmitCDialect"]`？

答案：Pass 要创建 `emitc::IncludeOp`，MLIR 要求 Pass 声明（并在运行前加载）它将构造 Op 的方言，否则上下文里没有注册该方言时会构造失败。声明位置在 [Passes.td:38-42](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td#L38-L42)。

**练习 3**：Device 子函数的入口前缀是什么？它和 u4-l4 讲的“内联”有什么关系？

答案：`__inline__ __attribute__((always_inline)) __aicore__`（[Func.cpp:66-67](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Func.cpp#L66-L67)）。这正是在 C 源码层面要求毕昇编译器把子函数强制内联进 kernel 的手段，与 u4-l4 讲的“IR 层同模块共存 + 发射层 always_inline 前缀”两层内联设计对应。

### 4.2 LegalizeKernelArgs：Kernel 参数合法化

#### 4.2.1 概念说明

u3-l6 讲过：launch 时所有 kernel 实参被拼成 8 字节对齐的字节流下发。但硬件要求的 kernel 形参表和用户在 Python 里写的形参表**并不相等**——运行时还需要一个 FFTS 控制区基址（`ffts_addr`）这类“目标平台专属参数”。`LegalizeKernelArgsPass` 做两件事：

1. 给 kernel 的**每个形参**打上 `emitasc.kernel_arg = explicit` 属性，声明“这是用户显式参数”；
2. 在形参表**末尾追加**一个 `ffts_addr` 隐藏参数，并在函数体开头插入 `set_ffts_base_addr(*ffts_addr)` 调用；MIX 场景还会追加一条“仅在 Cube 核上执行”的 FFTS 跨核同步。

这样，**IR 里的 kernel 签名就是唯一的 ABI 真相**：前端从它读出参数种类表，Launcher 照表注入实参，发射层照表打印 C 形参——三方共用一份描述，天然一致。

#### 4.2.2 核心流程

```text
walk 模块，找到带 ascendc.global 属性的 func.func（即 Kernel）
processKernel(kernel):
  1. 为已有全部形参打 kernel_arg = explicit
  2. 末尾追加形参：
       种类 FftsAddr，名字 ffts_addr（NameLoc），
       类型 memref<?xi64>（gm 地址空间）
  3. 函数体块首插入 set_ffts_base_addr(*ffts_addr)
  4. 若模块含 RegistMatmulObjOp 且非 matmul_cube_only：
       插入 AscendIsAIC 判断 + scf.if 内的
       ffts_cross_core_sync(PIPE_MTE3, 0xf21)
```

发射时的产物：

- 形参列表末尾多出一个名为 `ffts_addr` 的 GM 指针参数（名字来自 NameLoc，见下文）；
- 函数体第一句多出 `set_ffts_base_addr(*ffts_addr)`；
- MIX 时多出 `if (AscendIsAIC()) { ffts_cross_core_sync(PIPE_MTE3, ...); }` 形态的同步。

#### 4.2.3 源码精读

追加形参的辅助函数：[lib/Dialect/Asc/Transforms/LegalizeKernelArgs.cpp:35-43](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/LegalizeKernelArgs.cpp#L35-L43)。`appendKernelArgument` 用 `op.insertArgument` 在签名末尾插入参数，参数携带一个字典属性 `{emitasc.kernel_arg: <kind>}`，并把参数位置设为 `NameLoc("ffts_addr")`——这个 NameLoc 就是最终 C 形参可读名字的来源：发射层 [lib/Target/AscendC/CodeEmitter.cpp:238-247](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L238-L247) 的 `getOrCreateName` 发现值有 NameLoc 时会把名字拼进变量名（第 242-243 行）。

Pass 主体：[lib/Dialect/Asc/Transforms/LegalizeKernelArgs.cpp:45-69](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/LegalizeKernelArgs.cpp#L45-L69) 的 `processKernel`：

- 第 47-52 行：循环把每个已有形参的 `kernel_arg` 属性设为 `Explicit`。
- 第 53-58 行：构造 `AddressSpace::gm` 的 64 位动态 memref 类型，追加 `ffts_addr` 参数，然后创建 `SetFftsBaseAddrOp`——它被发射为 `set_ffts_base_addr(*ffts_addr)`，见 [lib/Target/AscendC/Basic/OtherOps.cpp:247-252](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Basic/OtherOps.cpp#L247-L252)。
- 第 59-68 行：MIX 分支。`RegistMatmulObjOp` 存在且模块没有 `asc.matmul_cube_only` 属性时，创建 `AscendIsAICOp`（运行时判断“当前是不是 Cube 核”，结果是 i1）包一个无 else 的 `scf.if`，内部用常量 `0xf21` 作旗标调 `FftsCrossCoreSyncOp(PIPE_MTE3, flag)`；发射格式见 [lib/Target/AscendC/Basic/OtherOps.cpp:168-174](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Basic/OtherOps.cpp#L168-L174)。同一份源码会同时编成 cube 与 vec 两个目标（u3-l5 的双目标路径），这个运行时判断保证跨核同步只在 Cube 核上执行。
- 第 71-81 行：Pass 入口 walk 模块，只处理带 `ascendc.global` 属性的函数——Device 子函数不加隐藏参数。

`KernelArgument` 枚举的定义：[include/ascir/Dialect/EmitAsc/IR/Attributes.td:25-41](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Attributes.td#L25-L41)，目前只有 `Explicit`(0) 与 `FftsAddr`(1) 两个值，注释写明它的用途是描述“显式参数与目标平台专属参数（workspace、ffts 地址等）”——预留了扩展位。

属性如何流回 Python（ABI 闭环第一段）：[python/src/IR.cpp:65-86](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/IR.cpp#L65-L86) 的 `getKernelArgAttrs` 找到 kernel 函数，逐参数读 `kernel_arg` 属性（缺省按 `Explicit` 处理），返回种类列表。它在 [python/asc/runtime/compiler.py:172](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L172) 被调用后装进 `CompiledKernel.kernel_args`（[compiler.py:78-84](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L78-L84)）。

ABI 闭环第二段（Launcher 消费）：[python/asc/runtime/launcher.py:133-142](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L133-L142)。`run` 用一个迭代器遍历 `kernel.kernel_args`：`Explicit` 就取下一个用户实参；`FftsAddr` 就注入 `np.array([rt.c2c_ctrl_addr()], dtype=np.uint64)`——运行时控制区地址以 8 字节 uint64 进入参数 blob，与 u3-l6 手算的布局完全吻合。这就是“IR 属性 → 前端行动”的全部闭环。

Pass 声明：[include/ascir/Dialect/Asc/Transforms/Passes.td:71-78](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td#L71-L78)，依赖 ascendc / emitasc / scf / arith 四个方言，对应它创建的四类 Op。

#### 4.2.4 代码实践

1. **实践目标**：在 IR 与 C 两个层面看到 `ffts_addr` 的全链路踪迹，并手算一次参数 blob。
2. **操作步骤**：
   - 带着 dump 跑 01_add（同 4.1.4 的环境变量）。
   - 打开 `ascir.mlir`，搜索 `ffts_addr`：找到 kernel 函数签名末尾多出的形参（形如 `memref<?xi64, 22>`，22 是 gm 地址空间的打印值）和函数体第一句 `ascendc.set_ffts_base_addr`。
   - 打开 `ascendc.cpp`，对照观察：kernel 形参列表末尾的 `ffts_addr` 参数、函数体第一句 `set_ffts_base_addr(*...)`。
   - 手算：01_add 的 `vadd_kernel(x, y, z, block_length)` 有 3 个指针 + 1 个 int32，加上隐藏的 `ffts_addr`（uint64），共 5 个参数。按 u3-l6 的 8 字节对齐规则画出 blob 布局。
3. **需要观察的现象**：codegen.mlir（Pass 前）里**没有** ffts_addr，ascir.mlir（Pass 后）里**有**——证明它是本 Pass 加的；`ascendc.cpp` 中参数名可读。
4. **预期结果**：blob 依次为 x/y/z 三个 8 字节指针、block_length 的 4 字节 int32（补齐到 8）、ffts_addr 的 8 字节 uint64；IR 与 C 两处的 ffts_addr 一一对应。IR 文本的具体拼写**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ffts_addr` 要追加在参数表**末尾**而不是开头？

答案：追加在末尾保证用户显式参数的序号不变——`getKernelArgAttrs` 与 Launcher 的 `next(explicit_arg)` 迭代器都依赖“按顺序消费显式参数”的约定（[launcher.py:133-137](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L133-L137)）。若插在开头，所有用户参数的位置都要平移，前端的参数打包逻辑也得跟着重排。

**练习 2**：`matmul_cube_only=True` 时（[DefineCubeOnlyPass.cpp:39](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/DefineCubeOnlyPass.cpp#L39) 会打 `asc.matmul_cube_only` 属性），MIX 分支的跨核同步会怎样？为什么合理？

答案：被跳过（[LegalizeKernelArgs.cpp:60-61](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/LegalizeKernelArgs.cpp#L60-L61) 的 `hasMatmul && !matmulCubeOnly` 判定）。纯 Cube 模式只编 cube 单目标，不存在 Cube 与 Vector 两族核的握手需求，跨核同步自然没有必要。

**练习 3**：如果不经过本 Pass 直接发射，会发生什么？

答案：kernel 签名里没有 `ffts_addr`，C 代码里也没有 `set_ffts_base_addr` 初始化；同时 `getKernelArgAttrs` 读到的参数表与 Launcher 打包的实参数量一致（都没有隐藏参数），看似自洽，但硬件要求的 FFTS 基址从未被设置，MIX 类 kernel 的核间同步会失效。这说明该 Pass 是“目标平台 ABI”的一部分，而非可选优化。

### 4.3 DeclarePyStruct：Struct 参数的 C 结构体声明

#### 4.3.1 概念说明

u3-l3 讲过 Struct 参数是“三面体”：Host 侧 ctypes 打包、IR 侧 `PyStructType`、设备侧本地副本。但 C 编译器要编译 `ascendc.cpp`，前提是文件里**声明过**这个结构体类型——用户在 Python 里定义的 `class KernelConfig(Struct)` 不会自动变成 C 的 `struct`。`DeclarePyStructPass` 负责补上这一环：扫描 IR 中出现的所有 `emitasc.py_struct` 类型，在模块头部插入对应的 `emitasc.declare_py_struct` Op，发射层再把它打印成带 `#pragma pack(push, 8)` 的 C 结构体定义。

#### 4.3.2 核心流程

```text
walk 模块中每个 Operation：
  遍历其每个 Region 的每个 Block 的块参数 → 收集类型
  遍历其每个结果值 → 收集类型
  对每个类型做 type.walk 递归下降：
    命中 emitasc::PyStructType → 记入列表（嵌套子结构体也会被收录）
按首次出现顺序去重
在模块块首逐个创建 emitasc.declare_py_struct
```

两个细节值得注意：

1. **递归收集**：`PyStructType` 的参数里可以嵌套别的 `PyStructType`（Struct 里套 StructField），`type.walk` 会下降到类型参数里，因此嵌套结构体各自都会被声明——lit 测试的 CHECK 序列可以证明（见 4.3.3）。
2. **去重保序**：`deduplicate` 用 `unordered_set` 去重但保持首次出现顺序，保证同一个结构体只声明一次。

#### 4.3.3 源码精读

Pass 主体：[lib/Dialect/Asc/Transforms/DeclarePyStructPass.cpp:65-91](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/DeclarePyStructPass.cpp#L65-L91)。

- 第 71-83 行：三层嵌套循环（Operation → Region → Block）收集块参数类型，第 80-83 行收集结果类型——之所以两边都查，是因为 Struct 既可能作为函数形参（块参数）出现，也可能作为某 Op 的结果（例如 `copy_struct` 的返回值）出现。
- 第 56-63 行：`collectPyStructTypes` 用 `arg.getType().walk(...)` 递归访问类型树，命中 `PyStructType` 就收集——嵌套结构体由此被覆盖。
- 第 42-54 行：`deduplicate` 的“先查 set、命中即取并擦除”写法，保证重复类型只保留第一次。
- 第 85-89 行：在模块块首逐个创建 `emitasc::DeclarePyStructOp`。

类型与 Op 的定义：`PyStructType` 携带三个参数——结构体名、成员类型数组、成员名数组，见 [include/ascir/Dialect/EmitAsc/IR/Types.td:22-25](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Types.td#L22-L25)；`DeclarePyStructOp` 只有一个 `TypeAttr` 参数，见 [include/ascir/Dialect/EmitAsc/IR/Ops.td:48-52](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td#L48-L52)。

发射侧：[lib/Target/AscendC/EmitAsc.cpp:95-110](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/EmitAsc.cpp#L95-L110) 把它打印为：

```c
#pragma pack(push, 8)
struct KernelConfig {
    int32_t block;
    int32_t tile;
};
#pragma pack(pop)
```

（成员的具体 C 类型拼写以发射层 `emitType` 为准，此处为示意。）

**为什么是 pack 8？** 前端 Struct 基类在 `__init_subclass__` 里动态生成 ctypes 类时写死了 `_pack_ = 8`，见 [python/asc/language/core/struct.py:165-172](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/struct.py#L165-L172)。Host 侧按 8 字节对齐打包字节流，设备侧 C 结构体也按 8 字节对齐解析——两端的 `#pragma pack(push, 8)` 与 `_pack_ = 8` 必须严格一致，否则同一个字节流会被解读成不同布局。这是“ABI 一致性”在 Struct 上的具体体现。

前端如何构造 `PyStructType`：[python/asc/language/core/struct.py:222-229](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/struct.py#L222-L229) 的 `get_ir_type` 把类名、各字段 IR 类型、各字段名交给 `get_emitasc_PyStructType`；设备侧本地副本 `create_local` 在 [struct.py:242-245](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/struct.py#L242-L245) 创建 `CopyStructOp`。

lit 测试佐证（嵌套与去重）：[test/Dialect/AscendC/Transforms/declare-py-struct.mlir:9-20](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/declare-py-struct.mlir#L9-L20)。输入是两个以 `py_struct` 类型 memref 为参数的函数，`RUN: ascir-opt -ascendc-declare-py-struct` 之后，CHECK 断言按“PyStruct1、PyStruct2、KernelConfig”的顺序声明三个结构体——KernelConfig 嵌套了前两者，`arg0` 的类型 walk 把它们都翻了出来；`arg1` 的 PyStruct2 虽然再次出现，但去重后不重复声明。

Pass 声明：[include/ascir/Dialect/Asc/Transforms/Passes.td:16-20](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td#L16-L20)。

#### 4.3.4 代码实践

1. **实践目标**：用一个带 Struct 参数的 kernel，验证 `ascendc.cpp` 里出现的 C 结构体声明与 Python 定义逐字段对应。
2. **操作步骤**：
   - 以 [python/test/generalization/basic/test_vadd_tiling.py:23-34](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/generalization/basic/test_vadd_tiling.py#L23-L34) 为模板，把 `BlockConfig`/`TileConfig`/`KernelConfig` 三个 Struct 类抄进一个新脚本，kernel 签名照抄第 38-39 行（含 `kernel_config: KernelConfig` 形参），函数体可以先简化为只读 `kernel_config.block.block_length`。
   - Host 侧按第 101-106 行的方式组装 `KernelConfig` 并启动。
   - 设置 `PYASC_DUMP_PATH` 运行，打开 `ascir.mlir` 找 `emitasc.declare_py_struct`，再打开 `ascendc.cpp` 找 `#pragma pack(push, 8)` 段。
3. **需要观察的现象**：`ascir.mlir` 中三个结构体各有一条声明且顺序为 BlockConfig（或首次出现顺序）；`ascendc.cpp` 中有三段 `#pragma pack` 结构体，成员名与 Python 字段名一致；嵌套的 `KernelConfig` 的成员类型是子结构体。
4. **预期结果**：C 结构体的字段名 `block_length` / `tile_length` / `tile_num` / `block` / `tile` 与 Python 类字段一一对应；两端都是 8 字节对齐。完整可运行性**待本地验证**（该参考测试默认只启用了 NPU 后端，本地无 NPU 时可把 `config.set_platform` 换成 Model 模式试跑）。

#### 4.3.5 小练习与答案

**练习 1**：为什么收集类型时“块参数”和“Op 结果”都要遍历？

答案：Struct 值在 IR 里有两种出现位置：作为函数形参（函数入口块参数）或作为某操作的结果值（如 `emitasc.copy_struct` 的结果、`ConstructOp` 构造出的值）。只查一边都会漏掉另一边的结构体类型，导致 C 文件里缺声明、编译失败。对应 [DeclarePyStructPass.cpp:71-83](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/DeclarePyStructPass.cpp#L71-L83)。

**练习 2**：把前端 `_pack_ = 8` 改成 4（假设允许），哪一层不会报错但结果会错？为什么？

答案：pyasc 自身各层都不会报错——IR 类型只记录字段名与类型，不记录对齐；发射层照样打印（只是 `#pragma pack` 仍写 8，与 ctypes 的 4 不一致）。错误发生在运行时：Host 侧按 4 字节对齐打包的字节流，设备侧按 8 字节对齐的 C 结构体解读，成员偏移错位、数值错乱。这解释了为什么 [struct.py:165-172](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/struct.py#L165-L172) 与 [EmitAsc.cpp:98](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/EmitAsc.cpp#L98) 这两处必须成对维护。

**练习 3**：同一个 kernel 里两个函数都用到了 `TileConfig`，C 文件里会有几份 `struct TileConfig`？

答案：一份。`deduplicate`（[DeclarePyStructPass.cpp:42-54](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/DeclarePyStructPass.cpp#L42-L54)）按首次出现顺序去重，重复类型只声明一次；否则 C 编译器会报重定义错误。

### 4.4 DetectKernelType / DetectEnableDebug：后端向前端回传信息

#### 4.4.1 概念说明

u3-l4 埋过一个伏笔：`kernel_type=None` 时，前端靠 IR 上的 `asc.compile_mix` 属性推导核类型。现在我们看到属性的**生产者**：两个 Detect Pass 在流水线末尾扫描最终 IR，把“这份 IR 用了 Matmul / 用了调试工具”的事实写成**模块属性**，Python 侧在 `pm.run` 之后读取。这条“C++ Pass 写、Python 读”的通道让前端无需重新实现任何 IR 分析——属性的种植与消费在时间上被 Pass 流水线天然隔开。

属性名字符串集中定义在 [include/ascir/Dialect/Asc/Utils/Attributes.h:18-26](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Utils/Attributes.h#L18-L26)：`asc.compile_mix`、`asc.enable_debug`、`asc.matmul_cube_only` 等，C++ 与 Python（[compiler.py:184-191](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L184-L191) 以字符串字面量读取）两侧共用同一拼写，这里是唯一权威定义处。

#### 4.4.2 核心流程

```text
DetectKernelType:
  模块中存在 RegistMatmulObjOp → 模块属性 asc.compile_mix

DetectEnableDebug:
  模块中存在 PrintfOp 或 DumpTensorOp → 模块属性 asc.enable_debug

Python 侧（run_passes 尾部，pm.run 之后）:
  kernel_type 为 None 时:
    有 asc.compile_mix → matmul_cube_only ? AIC_ONLY : MIX_AIC_1_2
    没有              → AIV_ONLY
  enable_debug = 有 asc.enable_debug 且环境变量 ASCENDC_DUMP 不为 "false"
```

#### 4.4.3 源码精读

DetectKernelType：[lib/Dialect/Asc/Transforms/DetectKernelTypePass.cpp:31-39](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/DetectKernelTypePass.cpp#L31-L39)。整个 Pass 只有一件事：walk 到 `RegistMatmulObjOp`（register_matmul 创建的 Matmul 对象注册 Op，见 u7-l1）就在模块上设 `asc.compile_mix` UnitAttr。**判据是“IR 里有没有 Matmul”，不是“用户传了什么选项”**——所以哪怕用户没指定 `kernel_type`，只要 kernel 真的用了矩阵乘，后端就能自己发现需要 MIX 双目标编译。

DetectEnableDebug：[lib/Dialect/Asc/Transforms/DetectEnableDebugPass.cpp:31-43](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/DetectEnableDebugPass.cpp#L31-L43)。两个独立的 walk：遇到 `PrintfOp` 或 `DumpTensorOp` 任一个，都设 `asc.enable_debug`。这两个 Op 来自前端的 `asc.printf` / `asc.dump_tensor`（定义于 [python/asc/language/basic/dump_tensor.py:20-46](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/dump_tensor.py#L20-L46)）——即用户在 kernel 里写了一句调试打印，整条 debug 链路就会被自动点亮。

Python 消费端：[python/asc/runtime/compiler.py:184-191](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L184-L191)。

- 第 184-189 行：`kernel_type` 仍为 None 时按 `asc.compile_mix` 有无推导——有则 `MIX_AIC_1_2`（或 `matmul_cube_only` 时的 `AIC_ONLY`），无则 `AIV_ONLY`。推导结果立刻决定 u3-l5 的编译目标：`CompilationTarget` 的 vec/cube 架构选择、`_gen_dst_kernel` 走“双目标编译+链接”还是“单目标自链接”、以及 `-cce-enable-mix`、`-D__MIX_CORE_MACRO__` 等命令行开关（[compiler.py:341-343](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L341-L343)）。
- 第 190-191 行：`enable_debug` 是“IR 有属性”与“环境变量 `ASCENDC_DUMP`（默认 True）”的与——即用户写了 printf 但 export 了 `ASCENDC_DUMP=false` 时依然关闭，留了一个全局开关。

`enable_debug=True` 的三处下游：

1. 编译命令加 `-DASCENDC_DUMP=1`（[compiler.py:331-334](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L331-L334)），让设备侧调试代码生效；
2. `ascendc.cpp` 生成后由 Python 注入 InitDump 代码段（[compiler.py:169-171](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L169-L171) 调 `_gen_init_dump_code`，实现在 [compiler.py:237-272](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L237-L272)）——注意这是全流水线里**少数由 Python 字符串拼接注入 ascendc.cpp 的代码段**，与“样板主要由 Pass+发射层生成”形成有意义的对照；
3. 装进 `CompiledKernel.enable_debug`，Launcher 据此在参数表尾追加 dump 缓冲实参（[launcher.py:143-144](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L143-L144)，即 u3-l6 讲的 75 MiB dump 缓冲）。

`kernel_type` 还决定 `CompiledKernel.core_type`（[compiler.py:201-208](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L201-L208)），进而决定注册二进制时用哪种 ELF 魔数（u3-l7）。

两个 Pass 的声明：[include/ascir/Dialect/Asc/Transforms/Passes.td:28-31](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td#L28-L31) 与 [Passes.td:100-103](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td#L100-L103)，都不需要依赖方言——它们只读 Op 与设属性。

最后补一句属性的双向流转：`asc.matmul_cube_only` 是**反方向**的例子——前端选项（[compiler.py:222-223](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L222-L223) 触发 `DefineCubeOnlyPass`）经 [DefineCubeOnlyPass.cpp:32-41](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/DefineCubeOnlyPass.cpp#L32-L41) 写成模块属性并插入 `#define ASCENDC_CUBE_ONLY`，再被 `LegalizeKernelArgs` 读走（4.2 的练习 2）。属性通道是双向的：既向后端传递用户意图，也向前端回传 IR 事实。

#### 4.4.4 代码实践

1. **实践目标**：观察模块属性如何随 kernel 内容出现/消失，并追踪它对编译命令的影响。
2. **操作步骤**：
   - 跑 01_add 并打开 `ascir.mlir` 的第一行（module 定义），确认没有 `asc.compile_mix`。
   - 跑 03_matmul_mix，确认 module 属性里出现 `asc.compile_mix`（形如 `module attributes {asc.compile_mix}`）。
   - 在 01_add 的 kernel 里加一句 `asc.printf("idx %d", asc.get_block_idx())`（接口见 [python/asc/language/basic/dump_tensor.py:46](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/dump_tensor.py#L46)），重新 dump，确认 `asc.enable_debug` 出现，且 `ascendc.cpp` 里被注入了 InitDump 代码段。
   - 再 `export ASCENDC_DUMP=false` 重跑一次，对比 InitDump 段是否消失。
3. **需要观察的现象**：属性只在“IR 确实包含对应 Op”时出现；`ASCENDC_DUMP=false` 能关掉 debug 链路但属性仍在。
4. **预期结果**：01_add → AIV_ONLY 单目标编译；03_matmul_mix → MIX_AIC_1_2 双目标编译（可从编译耗时与 dump 的 `binary.o` 生成路径侧面观察，见 u3-l5 实践）。printf 版本在默认环境变量下带 InitDump 段。具体文本**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 DetectKernelType 必须排在 `LegalizeKernelArgs` 之后、流水线的最后？排早了会出什么问题？

答案：它检测的是最终 IR 的内容。排在前面时，后续 Pass（如内联、规范化、同步重建）可能改变 IR 中 `RegistMatmulObjOp` 的存在形态或位置；而且 `kernel_type` 的推导发生在 `pm.run` 之后（[compiler.py:184](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L184)），排早了属性还可能被后续 Pass 的模块改写波及。放在最后保证“检测的就是发射的那份 IR”。

**练习 2**：用户在 kernel 里写了 `asc.printf`，但环境变量设了 `ASCENDC_DUMP=false`。此时 `asc.enable_debug` 属性还在吗？`ascendc.cpp` 里有 InitDump 段吗？编译命令里有 `-DASCENDC_DUMP=1` 吗？

答案：属性还在（Pass 只看 IR）；`self.enable_debug` 为 False（[compiler.py:190-191](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L190-L191) 的与运算），因此 InitDump 段不注入（169 行的判断不通过）、编译命令带的是 `-DASCENDC_DUMP=0`（[compiler.py:331-334](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L331-L334)）。

**练习 3**：`asc.compile_mix`、`asc.matmul_cube_only`、`emitasc.kernel_arg` 三个属性分别在哪一“层”起作用？

答案：`asc.compile_mix` 是模块属性，供 **Python 前端**读（推导 kernel_type）；`asc.matmul_cube_only` 是模块属性，由前端选项触发写入、供 **C++ Pass**（LegalizeKernelArgs）读；`emitasc.kernel_arg` 是参数属性，先由 C++ Pass 写、再经 `getKernelArgAttrs` 供 **Python Launcher** 读。三者展示了同一条属性通道服务于三个不同消费方。

## 5. 综合实践

**任务：给你的 `ascendc.cpp` 写一份“样板溯源报告”。**

1. 准备：安装好 pyasc 后，`export PYASC_DUMP_PATH=/tmp/pyasc-dump`，分别运行 `examples/01_add/add.py -r Model` 与 `examples/03_matmul_mix/matmul_mix.py -r Model`。
2. 打开两份 `ascendc.cpp`，逐段标注每一段“非用户代码”的来源，至少覆盖下表 8 行，并填上你观察到的实际行号：

   | ascendc.cpp 中的样板段 | 来源（Pass / 发射层 / Python） | 依据（源码位置） |
   | --- | --- | --- |
   | `#include "kernel_operator.h"` | GenerateBoilerplate 插 Op + Emitc.cpp 打印 | GenerateBoilerplatePass.cpp:36-37 |
   | `#include "lib/matmul_intf.h"`（仅 03） | GenerateBoilerplate 的 Matmul 分支 | GenerateBoilerplatePass.cpp:45-48 |
   | `extern "C" __global__ __aicore__ ...` 入口行 | 发射层按 `ascendc.global` 属性打印 | Func.cpp:63-67 |
   | 形参表末尾的 `ffts_addr` 参数 | LegalizeKernelArgs 追加（NameLoc 命名） | LegalizeKernelArgs.cpp:53-58 |
   | 函数体首句 `set_ffts_base_addr(*...)` | LegalizeKernelArgs 插桩 | OtherOps.cpp:247-252 |
   | `ffts_cross_core_sync(PIPE_MTE3, ...)`（仅 03） | LegalizeKernelArgs 的 MIX 分支 | LegalizeKernelArgs.cpp:59-68 |
   | `#pragma pack(push, 8) struct ...`（Struct 示例） | DeclarePyStruct 插 Op + EmitAsc.cpp 打印 | EmitAsc.cpp:95-110 |
   | `AscendC::InitDump(...)` 段（printf 版本） | Python 侧 `_gen_init_dump_code` 注入 | compiler.py:237-272 |

3. 做一个带 Struct 参数的最小 kernel（照 4.3.4 的步骤），把生成的 `struct` 定义与 Python 类并排贴出，验证字段名、嵌套关系与 8 字节 pack 三点一致。
4. 交叉验证 ABI：数一数 01_add 的 `ascendc.cpp` kernel 形参个数（应为 5：x/y/z/block_length/ffts_addr），与 [launcher.py:133-145](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L133-L145) 打包的实参数量对上。
5. 产出：一页 Markdown 报告，含上表、结构体对照图与 blob 布局手算。若本机无可用环境，可将“运行观察”替换为“对照 lit 测试与源码推演”（如 [test/Dialect/AscendC/Transforms/declare-py-struct.mlir](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/declare-py-struct.mlir) 的 CHECK 序列），并在报告开头注明“待本地验证”。

## 6. 本讲小结

- postprocessing 收尾链固定为 `DeclarePyStruct → GenerateBoilerplate →（DefineCubeOnly）→ LegalizeKernelArgs → DetectKernelType → DetectEnableDebug`，装填于 [compiler.py:219-230](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L219-L230)。
- `ascendc.cpp` 的样板主要**不是 Python 拼接**：include 由 `GenerateBoilerplatePass` 种成 `emitc.include`、`extern "C" __global__ __aicore__` 入口由发射层按 `ascendc.global` 属性打印；唯一例外的 InitDump 段恰好在 Python 侧（`_gen_init_dump_code`），可作对照记忆。
- kernel 参数 ABI 的唯一真相在 IR 签名上：`LegalizeKernelArgs` 给全部形参打 `explicit`、末尾追加 `ffts_addr` 隐藏参数并插 `set_ffts_base_addr`（MIX 再加 Cube 核专属的跨核同步）；前端经 `getKernelArgAttrs` 读表，Launcher 按表注入 `rt.c2c_ctrl_addr()`。
- Struct 参数的 C 声明由 `DeclarePyStructPass` 补齐：递归收集（含嵌套）+ 保序去重，发射成 `#pragma pack(push, 8)` 结构体，与前端 ctypes 的 `_pack_ = 8` 构成两端 ABI 约定。
- 模块属性是后端向前端的回传通道：`asc.compile_mix`（有 Matmul）决定 kernel_type 推导（MIX_AIC_1_2 / AIC_ONLY / AIV_ONLY），`asc.enable_debug`（有 printf/dump_tensor）联合 `ASCENDC_DUMP` 环境变量点亮 debug 编译与 dump 缓冲；`asc.matmul_cube_only` 则演示了前端→后端的反向流转。
- 至此第 6 单元的 Pass 全部讲完：lowering 补形态、optimizing 重建同步、postprocessing 铺发射——下一讲进入发射层本体。

## 7. 下一步学习建议

下一讲 **u6-l5《Ascend C 代码发射：CodeEmitter 与 Translation》** 将顺着本讲的铺垫进入 `lib/Target/AscendC`：`Translation.cpp` 的 printOperation 分发、`CodeEmitter` 的作用域与命名栈如何保证变量名唯一，以及本讲反复出现的“发射层打印”在代码上如何落地。建议先带着一个问题重读本讲的 4.1.3 与 4.2.3：`getOrCreateName` 遇到 NameLoc 会拼接名字（[CodeEmitter.cpp:238-247](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L238-L247)），这正是 `ffts_addr` 在 C 文件里可读的原因。若想先复习前端侧的对称知识，可回读 u3-l5（毕昇编译目标如何随 kernel_type 变化）与 u3-l6（参数 blob 的打包规则）。
