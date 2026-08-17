# Transforms Pass 全景：从 Passes.td 到 C++ 实现

## 1. 本讲目标

本讲是第 6 单元（Pass 优化与 Ascend C 代码生成）的开篇「地图讲」。学完后你应该能够：

1. 拿出一张完整的 Pass 全景表：pyasc 后端全部 16 个自定义 Pass，每个 Pass 属于 lowering / optimizing / postprocessing 哪个阶段、作用域是 ModuleOp 还是 FuncOp、实现文件在哪里、一句话作用是什么。
2. 说清一个 Pass 的「三件套」组织方式：在 `Passes.td` 里声明、在 `Passes.h` 里注册、在 `lib/Dialect/Asc/Transforms/` 下的 `.cpp` 里实现，并解释中间由 TableGen 生成的 `Passes.h.inc` 扮演的角色。
3. 对照 `compiler.py` 的三个调度函数，按顺序背出 Pass 流水线的执行顺序，以及 `insert_sync`、`matmul_cube_only`、`verify_sync`、`strip_loc` 四个开关分别控制哪些 Pass。
4. 掌握两种单独调试某个 Pass 的方法：用 `ascir-opt` 工具对一份 `.mlir` 只跑指定 Pass（需要构建 devtools），或退而求其次在 Python 侧用 `print_ir_before_all=True` 观察每个 Pass 前后的 IR。

## 2. 前置知识

在学习本讲前，请先回忆以下几个概念（前几讲已建立）：

- **Pass（通行变换）**：MLIR 中对整个 IR 做一次自顶向下遍历与改写的变换单元。第 3 单元（u3-l4）已见过：`Compiler.run` 把 codegen 生成的 IR 交给 `PassManager`，跑完一串 Pass 后才翻译成 Ascend C。
- **ASC-IR 与 ascendc 方言**：pyasc 用 MLIR 自定义方言逐条镜像 Ascend C API（u5-l1）。Pass 处理的就是这些 `ascendc.*` 操作。
- **TableGen 与 `.inc` 生成**：`.td` 文件是声明，构建时由 `ascir-tblgen` 展开成 `.inc` C++ 代码再被 include（u5-l4）。Pass 体系完全复用这套机制。
- **作用域（scope）与嵌套 Pass**：MLIR 的 Pass 分两类——作用于整个模块的 `ModuleOp` Pass，和作用于每个函数的 `func::FuncOp` Pass。后者注册时要通过 `addNestedPass<func::FuncOp>` 挂进 PassManager，PassManager 会对模块里每个函数各跑一次。
- **两阶段产物**：u3-l4 讲过 `codegen.mlir` 是 Pass 之前的 IR、`ascir.mlir` 是 Pass 之后的 IR。本讲要解释的正是这两份文件之间发生的一切。

一个容易混淆的命名约定：Python 侧的模块叫 `asc.ascendc`（如 `passes.ascendc.add_privatize_func`），而 IR 里方言前缀打印为 `ascendc.`、pybind 的 `create_asc_*` 用缩写 `asc`——u5-l1 的「四名合一」反查法在本讲同样适用。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/ascir/Dialect/Asc/Transforms/Passes.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td) | 16 个 Pass 的声明：注册名、作用域、构造函数、依赖方言 |
| [include/ascir/Dialect/Asc/Transforms/Passes.h](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.h) | 头文件：声明 `createXxxPass()` 工厂函数 + 两段 include 生成块 |
| [include/ascir/Dialect/Asc/Transforms/CMakeLists.txt](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/CMakeLists.txt) | 声明「td → Passes.h.inc」的 TableGen 生成规则 |
| [lib/Dialect/Asc/Transforms/](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms)（16 个 `.cpp`） | 每个 Pass 的 C++ 实现，文件名与 Pass 名对应 |
| [lib/Dialect/Asc/Transforms/Noop.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/Noop.cpp) | 「什么都不做」的 Pass，是理解三件套的最小样本 |
| [python/asc/runtime/compiler.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py) | Python 侧编译器驱动：三个调度函数决定 Pass 顺序 |
| [python/src/Passes.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/Passes.cpp) | pybind 桥接：把 Pass 工厂函数暴露为 `passes.common.*` / `passes.ascendc.*` |
| [include/ascir/Dialect/Utils/Registration.h](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Utils/Registration.h) | `ascir-opt` 等工具使用的统一注册入口 |
| [bin/ascir-opt.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/bin/ascir-opt.cpp) | 命令行 Pass 调试工具的 `main` |
| [test/Dialect/AscendC/Transforms/](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/privatize-func.mlir) | 每个 Pass 一个 `.mlir` lit 回归测试，也是学习 Pass 行为的最佳输入样例 |

## 4. 核心概念与源码讲解

### 4.1 Pass 清单：16 个 Pass 全景

#### 4.1.1 概念说明

pyasc 后端对 IR 的全部「深加工」都由自定义 Pass 完成。这些 Pass 要解决的问题大致分四类：

1. **把前端偷懒省掉的东西补回来**：前端（FunctionVisitor）允许用户惰性创建 LocalTensor、不写内存分配（u2-l2、u2-l6 的「惰性风格」），由 `MaterializeTensor`、`HoistUBAllocation` 等 Pass 在 IR 层补齐。
2. **重建同步**：框架模式下用户不写 `set_flag/wait_flag`，由 `EraseSync → HoistQueBind → InsertSync` 链按 API 依赖自动重建（u3-l4 提过的同步重建链）。
3. **为代码发射铺路**：`GenerateBoilerplate`、`LegalizeKernelArgs`、`DeclarePyStruct` 等把发射层需要的样板（include、kernel 入口、参数属性、结构体声明）以 IR 形式写好。
4. **向后端回传信息**：`DetectKernelType`、`DetectEnableDebug` 不改写逻辑，只在模块上打 `asc.compile_mix`、`asc.enable_debug` 两个单元属性，供 Python 侧读取——这就是 u3-l4 讲过的「后端写属性、前端读」的解耦通道。

#### 4.1.2 核心流程

下表是本讲的核心产出——Pass 全景表。「阶段」列按 `compiler.py` 的调度归属划分（详见 4.3）：

| # | Pass（命令行 flag） | 作用域 | 阶段 | 一句话作用 | 实现文件 |
| --- | --- | --- | --- | --- | --- |
| 1 | PrivatizeFunc（`-ascendc-privatize-func`） | ModuleOp | lowering | 把没有 `ascendc.global` 属性的函数改为 private（Kernel 公开、Device 子函数私有） | PrivatizeFunc.cpp |
| 2 | InputOutputTensor（`-ascendc-input-output-tensor`） | FuncOp | lowering | 为 `local_tensor_auto` 张量设置输入/输出角色 | InputOutputTensor.cpp |
| 3 | HoistUBAllocation（`-ascendc-hoist-ub-allocation`） | FuncOp | lowering | 把张量的 UB 分配提升到函数根部 | HoistUBAllocation.cpp |
| 4 | MaterializeTensor（`-ascendc-materialize-tensor`） | FuncOp | lowering | 为 `local_tensor_auto` 插入 `ascendc.tbuf`/`queue`/`alloca`，物化惰性张量 | MaterializeTensor.cpp |
| 5 | UnifyPipe（`-ascendc-unify-pipe`） | FuncOp | lowering（及 insert_sync 后再跑一次） | 统一 pipe 操作形态 | UnifyPipe.cpp |
| 6 | EraseSync（`-ascendc-erase-sync`） | FuncOp | optimizing（仅 `insert_sync=True`） | 删除核内手动同步操作 | EraseSync.cpp |
| 7 | HoistQueBind（`-ascendc-hoist-que-bind`） | FuncOp | optimizing（仅 `insert_sync=True`） | 提升 TQueBind/TQue/TBuf 初始化操作 | HoistQueBind.cpp |
| 8 | InsertSync（`-ascendc-insert-sync`） | FuncOp | optimizing（仅 `insert_sync=True`） | 按 API 依赖自动插入核内同步 | InsertSync.cpp |
| 9 | DeclarePyStruct（`-ascendc-declare-py-struct`） | ModuleOp | postprocessing | 插入 `emitasc.declare_py_struct`（Struct 参数的 C 结构体声明） | DeclarePyStructPass.cpp |
| 10 | GenerateBoilerplate（`-ascendc-generate-boilerplate`） | ModuleOp | postprocessing | 插入 `emitc.include` 与 extern 声明等样板代码 | GenerateBoilerplatePass.cpp |
| 11 | DefineCubeOnly（`-ascendc-define-cube-only`） | ModuleOp | postprocessing（仅 `matmul_cube_only=True`） | CUBE-ONLY 场景插入 `emitc.define` 宏定义 | DefineCubeOnlyPass.cpp |
| 12 | LegalizeKernelArgs（`-ascendc-legalize-kernel-args`） | ModuleOp | postprocessing | 挂 `emitasc.kernel_arg` 属性并插入参数操作，把 kernel 参数改写为 Ascend C 形参 | LegalizeKernelArgs.cpp |
| 13 | DetectKernelType（`-ascendc-detect-kernel-type`） | ModuleOp | postprocessing | 检测核是 vector-only 还是 mixed，打 `asc.compile_mix` 属性 | DetectKernelTypePass.cpp |
| 14 | DetectEnableDebug（`-ascendc-detect-enable-debug`） | ModuleOp | postprocessing | 检测是否用到 debug 工具（Printf/DumpTensor），打 `asc.enable_debug` 属性 | DetectEnableDebugPass.cpp |
| 15 | VerifySync（`-ascendc-verify-sync`） | FuncOp | postprocessing（仅 `verify_sync=True`） | 校验 TQue 同步正确性，不合法即报错 | VerifySync.cpp |
| 16 | Noop（`-ascendc-noop`） | FuncOp | 不被 compiler.py 调度 | 什么都不做，用作模板与测试 | Noop.cpp |

与之混排的还有 9 个 MLIR 上游「common」Pass（inliner、symbol-dce、canonicalizer、cse、reconcile-unrealized-casts、licm、sccp、strip-debug-info、print-ir），它们不属于 pyasc 自定义 Pass，本讲只在调度顺序中标注其位置。

#### 4.1.3 源码精读

Pass 的唯一权威清单就是 `Passes.td`。每条声明形如：

[include/ascir/Dialect/Asc/Transforms/Passes.td:L80-L83](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td#L80-L83) 声明了最简单的 Noop Pass：

```td
def Noop : Pass<"ascendc-noop", "func::FuncOp"> {
  let summary = "This pass does nothing";
  let constructor = "mlir::ascendc::createNoopPass()";
}
```

四个字段的含义：

- `Pass<"ascendc-noop", "func::FuncOp">`：第一个参数是**注册名**，同时也是命令行 flag（`ascir-opt -ascendc-noop`）；第二个参数是**作用域**——`func::FuncOp` 表示这是嵌套 Pass，PassManager 会对模块中每个函数各执行一次。
- `summary`：一句话摘要，会被生成为文档。
- `constructor`：C++ 工厂函数名，TableGen 生成的注册代码将调用它构造 Pass 实例。
- `dependentDialects`（Noop 没有，别的有）：声明该 Pass 需要预加载的方言，例如 MaterializeTensor 依赖 `arith` 与 `ascendc` 两个方言，见 [Passes.td:L65-L69](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td#L65-L69)。

作用域字段直接决定了 Python 绑定侧用哪个宏注册（见 4.2.3）：`ModuleOp` 的 Pass 用 `DEFINE_ADD_PASS`（直接 `addPass`），`func::FuncOp` 的用 `DEFINE_ADD_PASS_ON`（`addNestedPass<func::FuncOp>`）。

两个「Detect」类 Pass 的实现很短，值得各看一眼——它们示范了「打属性回传」这一模式：

- [lib/Dialect/Asc/Transforms/DetectKernelTypePass.cpp:L31-L39](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/DetectKernelTypePass.cpp#L31-L39)：`walk` 全模块找 `RegistMatmulObjOp`（Matmul 注册操作），只要找到一个，就在模块上打 `asc.compile_mix` 单元属性。这正是 u3-l4 讲过的 `kernel_type=None` 时推导 `MIX_AIC_1_2 / AIC_ONLY / AIV_ONLY` 的依据。
- [lib/Dialect/Asc/Transforms/DetectEnableDebugPass.cpp:L31-L43](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/DetectEnableDebugPass.cpp#L31-L43)：同理，找到 `PrintfOp` 或 `DumpTensorOp` 就打 `asc.enable_debug` 属性。

#### 4.1.4 代码实践

**实践目标**：验证「三方数量一致性」——`Passes.td` 的声明数、`Passes.h` 的 `createXxxPass()` 工厂数、`lib/Dialect/Asc/Transforms/` 的实现文件数应当完全相等。

**操作步骤**：

1. 在仓库根目录统计声明数：

   ```bash
   grep -c '^def .* : Pass<' include/ascir/Dialect/Asc/Transforms/Passes.td
   ```

2. 统计工厂数：

   ```bash
   grep -c 'create.*Pass();' include/ascir/Dialect/Asc/Transforms/Passes.h
   ```

3. 统计实现文件数：

   ```bash
   ls lib/Dialect/Asc/Transforms/*.cpp | wc -l
   ```

4. 再用一条命令把「Pass 注册名 → 工厂函数」的对应关系全部抽出来：

   ```bash
   grep -E 'def |constructor' include/ascir/Dialect/Asc/Transforms/Passes.td
   ```

**需要观察的现象**：三个命令都应输出 16；第 4 步输出 16 对「名字 + 工厂」映射，注意名字里的小写连字符风格（`ascendc-hoist-ub-allocation`）与工厂的驼峰风格（`createHoistUBAllocationPass`）之间的转换规律。

**预期结果**：16 / 16 / 16，且 `Passes.h` 中每个工厂函数都能在第 4 步的输出里找到出处。（本实践为纯只读统计，可离线完成；若你的统计与 16 不符，请以你仓库的 HEAD 为准——本讲基于 HEAD `739ef7e`。）

#### 4.1.5 小练习与答案

**练习 1**：`Pass<"ascendc-erase-sync", "func::FuncOp">` 与 `Pass<"ascendc-privatize-func", "ModuleOp">` 在 PassManager 里执行方式有何不同？

**答案**：前者是嵌套 Pass，PassManager 会遍历模块里每个 `func.func`，对每个函数各跑一次 `runOnOperation()`（操作对象是该函数）；后者是模块级 Pass，整个模块只跑一次，操作对象是 `ModuleOp`。这也解释了为什么删除函数可见性这类「跨函数信息」必须用 ModuleOp Pass，而张量物化这类「函数内部改写」用 FuncOp Pass 更自然。

**练习 2**：为什么 `DetectKernelTypePass` 需要在 Pass 流水线的**最后**跑，而不能放在最前面？

**答案**：它检测的是 IR 里有没有 `RegistMatmulObjOp`（Matmul 相关操作）。codegen 直接产出的 IR 中这些操作可能还混在子函数里、或形态未定型；放在 postprocessing 末尾意味着在所有结构改写（内联、物化、样板生成）完成之后再做检测，结论才可靠。此外它的输出 `asc.compile_mix` 是给 Python 侧推导 `kernel_type` 用的，而 `kernel_type` 又决定毕昇编译的目标架构，因此必须赶在翻译/编译之前完成。

**练习 3**：`Noop` Pass 在 `compiler.py` 中从未被调度，为什么还要保留？

**答案**：它是三件套的最小模板（实现体为空），新写一个 Pass 时可以照抄它的骨架；同时它被暴露为 `passes.ascendc.add_noop_pass`（见 4.2.3），可用于验证 Pass 管线本身的连通性，属于工程上的「空转测试件」。

### 4.2 Pass 三件套：声明（td）、注册（Passes.h）、实现（cpp）

#### 4.2.1 概念说明

「三件套」指一个 Pass 在代码库中的三个落点：

1. **声明**：`Passes.td` 里一条 `def`，给出注册名、作用域、工厂名、依赖方言。
2. **注册/接口**：`Passes.h` 头文件，手写 `createXxxPass()` 前置声明，再通过两段 include 块引入 TableGen 生成的 `Passes.h.inc`（声明侧）与注册函数（工具侧）。
3. **实现**：`lib/Dialect/Asc/Transforms/` 下同名 `.cpp`，用生成的 `XxxBase<Derived>` 基类派生出具体 Pass 并实现工厂函数。

中间的粘合剂是构建系统：`mlir_tablegen` 规则把 `.td` 展开成 `.inc`。这与 u5-l4 讲过的 Op 定义生成是同一套 TableGen 机制，只是换成了 `-gen-pass-decls` 后端。

#### 4.2.2 核心流程

一个 Pass 从声明到可被调用的完整链路：

```text
Passes.td (def Xxx : Pass<"ascendc-xxx", scope> { constructor = createXxxPass() })
   │  CMake: mlir_tablegen(Passes.h.inc -gen-pass-decls -name ascendc)
   ▼
Passes.h.inc（生成物：impl::XxxBase<Derived> 基类、GEN_PASS_DEF_XXX 宏、registerascendcPasses()）
   │  被 Passes.h 两处 include（GEN_PASS_DECL / GEN_PASS_REGISTRATION）
   ▼
Xxx.cpp（#define GEN_PASS_DEF_XXX + include → struct XxxPass : Base → 实现 createXxxPass()）
   │
   ├──► python/src/Passes.cpp：DEFINE_ADD_PASS / DEFINE_ADD_PASS_ON 暴露为 passes.ascendc.add_xxx
   │        └─► compiler.py 调度成流水线
   └──► bin/ascir-opt.cpp：ascir::registerPasses() → registerascendcPasses() 注册为命令行 flag
            └─► ascir-opt -ascendc-xxx 单独调试
```

#### 4.2.3 源码精读

**生成规则**：[include/ascir/Dialect/Asc/Transforms/CMakeLists.txt:L9-L13](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/CMakeLists.txt#L9-L13) 声明了 `.td` 到 `.inc` 的转换，`-name ascendc` 决定了注册函数叫 `registerascendcPasses`：

```cmake
set(LLVM_TARGET_DEFINITIONS Passes.td)
mlir_tablegen(Passes.h.inc -gen-pass-decls -name ascendc)
add_public_tablegen_target(MLIRAscPassIncGen)
```

而 [lib/Dialect/Asc/Transforms/CMakeLists.txt:L9-L39](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/CMakeLists.txt#L9-L39) 把 16 个 `.cpp` 编进 `MLIRAscTransforms` 库，并以 `DEPENDS MLIRAscPassIncGen` 保证 `.inc` 先生成。

**头文件的两段 include**：[Passes.h:L19-L20](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.h#L19-L20) 先 `#define GEN_PASS_DECL` 再 include，生成基类声明；[Passes.h:L22-L37](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.h#L22-L37) 是 16 个工厂函数的手写前置声明；[Passes.h:L41-L42](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.h#L41-L42) 再以 `GEN_PASS_REGISTRATION` include 一次，生成 `registerascendcPasses()`。

**实现侧三步**（以最短的 [Noop.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/Noop.cpp) 为例）：

1. [Noop.cpp:L17-L18](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/Noop.cpp#L17-L18)：`#define GEN_PASS_DEF_NOOP` 后 include `.inc`，本次展开的是该 Pass 的基类定义（`ascendc::impl::NoopBase<Derived>`，提供命令行选项解析等样板）。
2. [Noop.cpp:L26-L31](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/Noop.cpp#L26-L31)：派生并覆写 `runOnOperation()`——这是 Pass 的全部逻辑落点。

   ```cpp
   struct NoopPass : public ascendc::impl::NoopBase<NoopPass> {
       void runOnOperation() override
       {
           // This pass does nothing.
       }
   };
   ```

3. [Noop.cpp:L37](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/Noop.cpp#L37)：实现 `.td` 里承诺的工厂函数 `createNoopPass()`。

**两个消费入口**：

- **Python 侧**：[python/src/Passes.cpp:L27-L30](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/Passes.cpp#L27-L30) 定义两个宏，差别只在是否 `addNestedPass`；[Passes.cpp:L91-L111](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/Passes.cpp#L91-L111) 的 `defineAscendCPasses` 用它们把 16 个 Pass 逐个挂为 `passes.ascendc.add_xxx` 函数。对照可见：`add_privatize_func`、`add_detect_kernel_type` 等模块级 Pass 用 `DEFINE_ADD_PASS`，`add_noop_pass`、`add_erase_sync` 等函数级 Pass 用 `DEFINE_ADD_PASS_ON(func::FuncOp, ...)`——与 `.td` 中的作用域声明一一对应。上游通用 Pass 则在 [Passes.cpp:L77-L89](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/Passes.cpp#L77-L89) 挂为 `passes.common.add_xxx`。
- **工具侧**：[bin/ascir-opt.cpp:L18-L26](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/bin/ascir-opt.cpp#L18-L26) 的 `main` 注册全部方言与 Pass 后进入 `MlirOptMain`；其中 [Registration.h:L35-L39](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Utils/Registration.h#L35-L39) 的 `registerPasses()` 在上游 `registerAllPasses()` 之外追加了 `registerascendcPasses()`——后者正是 `Passes.h.inc` 生成物的另一半。

#### 4.2.4 代码实践

**实践目标**：单独运行一个 Pass（PrivatizeFunc），验证它「把无 `ascendc.global` 属性的 public 函数改成 private」的行为。

**操作步骤**：

1. 准备输入：直接使用仓库自带的回归测试 [test/Dialect/AscendC/Transforms/privatize-func.mlir](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/privatize-func.mlir)。该文件包含 6 个函数，覆盖 public/private 与有无 `ascendc.global` 属性的组合，并自带 FileCheck 断言。
2. 若已按 u1-l2 的方式用 `PYASC_SETUP_DEVTOOLS=1` 构建出 `ascir-opt`，执行：

   ```bash
   ascir-opt -ascendc-privatize-func test/Dialect/AscendC/Transforms/privatize-func.mlir
   ```

3. 手工比对输出与文件中 `// CHECK:` 行的期望（例如 [privatize-func.mlir:L16-L19](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/privatize-func.mlir#L16-L19) 期望 `public` 变 `private`，而 [L11-L14](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/privatize-func.mlir#L11-L14) 带 `ascendc.global` 的函数保持公开）。
4. 若没有构建 devtools：改为**源码阅读型实践**——通读 [PrivatizeFunc.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/PrivatizeFunc.cpp)，并对照 lit 测试的 6 个 CHECK 推断每个函数的输出形态；同时用 4.3.4 的 `print_ir_before_all` 方法在 Python 侧观察该 Pass 在真实编译中的效果。

**需要观察的现象**：只有「public 且无 `ascendc.global`」的函数可见性被改写为 private；带 `ascendc.global` 的函数（Kernel 函数，u4-l4 讲过它要被发射为 `extern "C" __global__` 公开符号）保持不变。

**预期结果**：输出与 lit 文件中全部 CHECK 断言一致。工具是否可用取决于是否构建 devtools，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果要新增一个名为 `FoldGelu` 的 Pass，需要改动哪几个文件？

**答案**：五处——(1) `Passes.td` 加 `def FoldGelu : Pass<"ascendc-fold-gelu", "func::FuncOp"> { let constructor = "mlir::ascendc::createFoldGeluPass()"; }`；(2) `Passes.h` 加 `std::unique_ptr<Pass> createFoldGeluPass();` 声明；(3) 新建 `lib/Dialect/Asc/Transforms/FoldGelu.cpp`，按 Noop.cpp 三步走（`GEN_PASS_DEF_FOLDGELU` + 派生类 + 工厂）；(4) 该目录 `CMakeLists.txt` 的源文件清单加入 `FoldGelu.cpp`；(5) 若要在 Python 流水线里用，还需在 `python/src/Passes.cpp` 的 `defineAscendCPasses` 中加一行宏注册，并在 `compiler.py` 的某个调度函数里调用。最后在 `test/Dialect/AscendC/Transforms/` 加一个 lit 测试。

**练习 2**：`GEN_PASS_DECL` 与 `GEN_PASS_REGISTRATION` 两次 include 同一个 `.inc`，为什么不会重定义冲突？

**答案**：生成的 `.inc` 内部用宏做了条件分段——先定义了 `GEN_PASS_DECL` 再 include 时只展开「声明段」（基类与工厂声明），先定义 `GEN_PASS_REGISTRATION` 再 include 时只展开「注册函数段」。两次 include 各取所需，这是 MLIR Pass 体系的标准手法，pyasc 只是对它加以利用。

**练习 3**：`passes.ascendc.add_erase_sync` 与 `passes.ascendc.add_privatize_func` 在 pybind 绑定上有何不同？为什么？

**答案**：前者经 `DEFINE_ADD_PASS_ON(func::FuncOp, ...)` 注册，内部调用 `pm.addNestedPass<func::FuncOp>(createEraseSyncPass())`；后者经 `DEFINE_ADD_PASS` 注册，调用 `pm.addPass(createPrivatizeFuncPass())`。原因就是 `.td` 中声明的作用域不同：EraseSync 作用于函数（`func::FuncOp`），PrivatizeFunc 作用于模块（`ModuleOp`）。

### 4.3 Pass 调度顺序：compiler.py 三阶段流水线

#### 4.3.1 概念说明

声明好的 Pass 只是零件，装配顺序由 Python 侧 `Compiler` 决定。u3-l4 已给出主流程骨架，本讲下钻到「每个位置放了哪个 Pass」。调度被拆成三个静态语义的阶段函数：

- **lowering（降级/补全）**：把前端产出的「欠定形」IR 补成规范形态——函数可见性、张量物化、内存分配提升、pipe 统一。
- **optimizing（优化）**：上游通用优化（LICM、SCCP、规范化），以及可选的同步重建链。
- **postprocessing（收尾）**：为发射 Ascend C 生成样板、合法化参数、打回传属性、按需校验。

注意「阶段」只是源码组织上的三个函数，PassManager 对全部 Pass 一视同仁地顺序执行；真正有条件分支的是四个编译选项。

#### 4.3.2 核心流程

把 [compiler.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py) 三个调度函数展开，完整流水线如下（【A】= ascendc 自定义，【C】= common 上游）：

```text
── lowering ──────────────────────────────────────────────
【A】privatize_func          函数可见性定形
【C】inliner                 子函数内联
【C】symbol_dce              死符号消除
【C】canonicalizer           规范化
【C】reconcile_unrealized_casts  类型转换清理
【A】input_output_tensor     标记输入输出张量
【A】hoist_ub_allocation     UB 分配提升
【A】materialize_tensor      惰性张量物化
【A】unify_pipe              pipe 统一
【C】canonicalizer           规范化
【C】cse                     公共子表达式消除
── optimizing ────────────────────────────────────────────
【C】licm                    循环不变量外提
【C】sccp                    稀疏条件常量传播
【C】canonicalizer           规范化
（若 insert_sync == True，追加同步重建链：）
【A】erase_sync              删除手动同步
【A】hoist_que_bind          提升队列绑定/初始化
【A】insert_sync             自动插入同步
【A】unify_pipe              再次 pipe 统一
【C】canonicalizer           规范化
── postprocessing ────────────────────────────────────────
【A】declare_py_struct       Struct 参数结构体声明
【A】generate_boilerplate    include 与样板代码
（若 matmul_cube_only == True：）
【A】define_cube_only        CUBE-ONLY 宏定义
【A】legalize_kernel_args    kernel 参数合法化
【A】detect_kernel_type      打 asc.compile_mix 属性
【A】detect_enable_debug     打 asc.enable_debug 属性
（若 verify_sync == True：）
【A】verify_sync             同步正确性校验
（若 strip_loc == True：）
【C】strip_debug_info        剥离调试定位
```

四个开关与被控 Pass 的对应关系（选项定义见 [compiler.py:L27-L41](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L27-L41) 的 `CompileOptions`）：

| 开关 | 控制的 Pass | 三态/两态 |
| --- | --- | --- |
| `insert_sync` | EraseSync、HoistQueBind、InsertSync、（第二次）UnifyPipe | 三态：`None` 时由 `mod.need_insert_sync()` 依据 IR 中 `LocalTensorAutoOp` 自动判定（u3-l4） |
| `matmul_cube_only` | DefineCubeOnly | 两态 bool |
| `verify_sync` | VerifySync | 两态 bool |
| `strip_loc` | strip_debug_info | 两态 bool |

#### 4.3.3 源码精读

**lowering 阶段**：[compiler.py:L119-L131](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L119-L131) 的 `_schedule_lowering` 是静态方法，逐行向 PassManager 添加 Pass：

```python
@staticmethod
def _schedule_lowering(pm: passes.PassManager) -> None:
    passes.ascendc.add_privatize_func(pm)
    passes.common.add_inliner(pm)
    ...
    passes.ascendc.add_unify_pipe(pm)
    passes.common.add_canonicalizer(pm)
    passes.common.add_cse(pm)
```

注意顺序设计的因果：先 `privatize_func` + `inliner` 把 Device 子函数内联进来（u4-l4 讲过 IR 层子函数与 Kernel 同模块共存，这里才真正合并），随后 `input_output_tensor`、`hoist_ub_allocation`、`materialize_tensor`、`unify_pipe` 依次补全张量与内存形态——这正是 u2-l6「惰性 LocalTensor 风格」能成立的底层支撑。

**optimizing 阶段**：[compiler.py:L133-L142](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L133-L142) 的 `_schedule_optimizing` 是实例方法（要读 `self.options`），先跑三个上游优化，再按 `insert_sync` 决定是否追加「擦除→提升→重插」三连：

```python
if self.options.insert_sync:
    passes.ascendc.add_erase_sync(pm)
    passes.ascendc.add_hoist_que_bind(pm)
    passes.ascendc.add_insert_sync(pm)
    passes.ascendc.add_unify_pipe(pm)
    passes.common.add_canonicalizer(pm)
```

**postprocessing 阶段**：[compiler.py:L219-L230](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L219-L230) 的 `_schedule_postprocessing` 收尾并挂上三个条件 Pass。三阶段的拼装点在 [compiler.py:L232-L235](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L232-L235) 的 `_schedule_passes`。

**run_passes 的前后处理**：[compiler.py:L175-L191](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L175-L191) 是调度与执行的粘合层，值得整段精读：

- L176 构造 `PassManager`，L177 打开 verifier（每个 Pass 后校验 IR 合法性）；
- L178-L179 `print_ir_before_all=True` 时调用 `enable_printing()`——该绑定在 [python/src/Passes.cpp:L62-L74](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/Passes.cpp#L62-L74)，效果是**每个 Pass 前后都把 IR 打到 stderr**；
- L180-L181 `insert_sync` 为 `None` 时用 `mod.need_insert_sync()` 自动判定；
- L182-L183 装填流水线并 `pm.run(mod)`；
- L184-L189 跑完后读 `asc.compile_mix` 属性推导 `kernel_type`（消费 DetectKernelTypePass 的产出）；
- L190-L191 读 `asc.enable_debug` 属性并结合 `ASCENDC_DUMP` 环境变量决定 `enable_debug`（消费 DetectEnableDebugPass 的产出）。

由此看清完整的闭环：**Pass 打属性 → run_passes 读取 → 决定后续毕昇编译行为**。这些 Pass 的执行时机夹在 [compiler.py:L162-L173](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L162-L173) `Compiler.run` 的两次 dump 之间——L164 先落盘 `codegen.mlir`（Pass 前），L165-L166 跑 Pass，L167 再落盘 `ascir.mlir`（Pass 后），L168 才翻译成 Ascend C。

#### 4.3.4 代码实践

**实践目标**：用 `print_ir_before_all` 观察真实编译中每个 Pass 前后的 IR，锁定 MaterializeTensor 与 GenerateBoilerplate 两个 Pass 的实际改写效果。

**操作步骤**：

1. 设置 dump 环境变量并运行框架风格示例（u2-l6 讲过 02 是惰性张量风格，Pass 效果最明显）：

   ```bash
   export PYASC_DUMP_PATH=/tmp/pyasc_dump
   cd examples/02_add_framework
   python3 add_framework.py -r Model
   ```

2. 打开 `/tmp/pyasc_dump/codegen.mlir` 与 `/tmp/pyasc_dump/ascir.mlir`，对照 4.3.2 的流水线逐段找差异。
3. 想看「每个 Pass 之后」的中间形态，给 jit 装饰器追加选项（CompileOptions 字段，u3-l1 讲过可从装饰器小括号进入）：

   ```python
   # 示例代码：在示例的核函数装饰器上追加 print_ir_before_all
   @asc.jit(print_ir_before_all=True)
   def add_kernel(...):
       ...
   ```

   运行时 stderr 会按流水线顺序打印每个 Pass 前后的完整 IR；用 `2> pass.log` 收集后，在 `pass.log` 中搜索 `IR Dump` 分隔块即可逐 Pass 对比。
4. （可选，若已构建 devtools）把 dump 出的 `codegen.mlir` 喂给工具单独复跑两个 Pass：

   ```bash
   ascir-opt -ascendc-materialize-tensor /tmp/pyasc_dump/codegen.mlir
   ascir-opt -ascendc-generate-boilerplate /tmp/pyasc_dump/codegen.mlir
   ```

**需要观察的现象**：

- `codegen.mlir` 中 LocalTensor 声明是「欠定形」的（没有对应的 `tbuf`/`queue`/`alloca` 分配）；`ascir.mlir` 中这些分配已就位——这是 MaterializeTensor 等 lowering Pass 的效果。
- `ascir.mlir` 顶部出现 `emitc.include` 等 IR 形式的样板——这是 GenerateBoilerplate 的效果（u6-l4 将展开）。
- `pass.log` 中每个 `IR Dump After` 块与 4.3.2 流水线的顺序一一对应。

**预期结果**：两份 mlir 的差异点能分别归因到具体 Pass；`pass.log` 的 Dump 块数量与流水线中 Pass 数量一致。示例运行依赖 CANN/Model 环境，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `privatize_func` 必须排在 `inliner` 之前？

**答案**：`privatize_func` 按「有无 `ascendc.global` 属性」定可见性：Kernel 函数保持公开供发射层生成 `extern "C" __global__` 符号，Device 子函数改为 private。若先内联再改可见性，被内联删掉的子函数符号不存在了，可见性标记无意义；先定形再内联，inliner 可以放心把 private 函数内联消解，符号 DCE（随后的 symbol_dce）也能清理干净。

**练习 2**：同一个 Pass `unify_pipe` 在流水线里出现了两次（lowering 末尾与 insert_sync 链末尾），这有问题吗？

**答案**：没有问题，Pass 是幂等设计思路的体现。第一次统一 pipe 形态服务于后续张量物化与优化；同步重建链（erase→hoist→insert）会插入新的同步与队列操作，可能再次产生不统一的 pipe 形态，故重跑一次收尾。这也是阅读调度代码时要养成的意识：同名函数出现两次不代表笔误，要结合前后文判断各自服务的目标。

**练习 3**：`verify_sync` 与 `strip_loc` 都在流水线最末尾，为什么顺序上 `verify_sync` 在前？

**答案**：`verify_sync` 的报错要携带源码定位才能指到用户 kernel 的具体行（u4-l5 讲过 CodegenError 的定位渲染机制），而 `strip_debug_info` 恰恰会剥离这些定位信息。先校验后剥离，保证校验失败时诊断信息完整；校验通过后再剥离定位，得到干净的发布产物。

## 5. 综合实践

**任务**：制作并验证一张「Pass 全景表」。

1. **制表**：以 4.1.2 的表格为模板，自己从 `Passes.td`、`python/src/Passes.cpp`、`compiler.py` 三个文件出发重新收集信息（不要照抄本讲），产出五列：Pass 名（含命令行 flag）、作用域、调度阶段、源码文件、一句话作用。要求每个单元格都能给出对应的永久链接。
2. **归类自查**：按 lowering / optimizing / postprocessing / 不调度 四类给 16 个 Pass 归类，标注哪些 Pass 受编译选项开关控制及开关名。
3. **验证两个 Pass**：
   - 路线 A（有 devtools）：任选两个 Pass（建议 `materialize-tensor` 与 `privatize-func`），从 `PYASC_DUMP_PATH` 导出的 `codegen.mlir` 出发，用 `ascir-opt -ascendc-xxx` 分别单独运行，把输出与全量流水线得到的 `ascir.mlir` 对比，确认「单跑该 Pass 的增量」与你表中「一句话作用」相符。
   - 路线 B（无 devtools）：用 `@asc.jit(print_ir_before_all=True)` 重跑 01_add 与 02_add_framework，在 stderr 日志中按 4.3.2 的流水线顺序找到目标 Pass 的前后 IR Dump 块，摘录前后差异各 5 行以内，注明归因。
4. **产出物**：一份 Markdown 表格 + 两段差异摘录（各带源码链接）。

预期：表格 16 行无遗漏；两条验证路线中至少完成一条；差异摘录能明确说出「哪个 Pass 把 IR 从什么形态改成了什么形态」。运行环境不可用时，路线 B 降级为源码阅读 + lit 测试断言推断，并注明「待本地验证」。

## 6. 本讲小结

- pyasc 后端共 16 个自定义 Pass，全部声明于 [Passes.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/Transforms/Passes.td)，按调度归属分 lowering（补全形态）、optimizing（优化与同步重建）、postprocessing（发射铺路与属性回传）三阶段，其中 Noop 不参与调度。
- 一个 Pass 的三件套：`.td` 声明（注册名、作用域、工厂、依赖方言）→ `Passes.h` + TableGen 生成的 `Passes.h.inc`（`GEN_PASS_DECL` 声明段、`GEN_PASS_DEF_XXX` 基类段、`GEN_PASS_REGISTRATION` 注册段）→ 同名 `.cpp` 实现（派生 `XxxBase` + 覆写 `runOnOperation` + 实现工厂函数）。
- 作用域决定挂载方式：`ModuleOp` Pass 用 `DEFINE_ADD_PASS`，`func::FuncOp` Pass 用 `DEFINE_ADD_PASS_ON`（`addNestedPass`），`.td` 与 pybind 侧严格对应。
- 调度顺序由 [compiler.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py) 的 `_schedule_lowering/_schedule_optimizing/_schedule_postprocessing` 三个函数装填；`insert_sync`、`matmul_cube_only`、`verify_sync`、`strip_loc` 四个开关分别控制同步重建链、DefineCubeOnly、VerifySync、strip_debug_info。
- `DetectKernelType` / `DetectEnableDebug` 在模块上打 `asc.compile_mix` / `asc.enable_debug` 属性，`run_passes` 读回以推导 `kernel_type` 与 `enable_debug`，构成后端向前端的回传通道。
- 调试单个 Pass 的两条路：`ascir-opt -ascendc-xxx`（需 `PYASC_SETUP_DEVTOOLS=1` 构建）或 `print_ir_before_all=True`（每个 Pass 前后向 stderr 打印 IR）。

## 7. 下一步学习建议

本讲只建立了 Pass 体系的「骨架与地图」，后续两讲将下钻到具体 Pass 的实现内部：

- **u6-l2 张量物化与 UB 内存分配**：精读 `MaterializeTensor.cpp`、`HoistUBAllocation.cpp`、`UnifyPipe.cpp`、`InputOutputTensor.cpp`，理解惰性张量如何被补成真实的 UB 分配——本讲 4.3.4 观察到的 codegen→ascir 差异将在那里得到逐行解释。
- **u6-l3 自动同步插入**：精读 `EraseSync.cpp`、`HoistQueBind.cpp`、`InsertSync.cpp`、`VerifySync.cpp`，理解同步重建链如何按 API 依赖分析工作。
- 在继续之前，建议先完成本讲综合实践的全景表——它将是你阅读 u6 后续讲义时随手可查的索引。
- 若想加深对 Pass 机制本身的理解，可对照阅读 [test/Dialect/AscendC/Transforms/](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/privatize-func.mlir) 目录下每个 Pass 的 lit 测试输入：它们是「最小可复现 IR 样例」的宝库。
