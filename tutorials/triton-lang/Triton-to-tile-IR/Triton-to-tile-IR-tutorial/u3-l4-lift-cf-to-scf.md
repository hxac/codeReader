# 控制流结构化 LiftTTCFToSCF

## 1. 本讲目标

本讲是第三单元（MLIR 转换 Pass 体系）的第四篇，承接 [u3-l1](u3-l1-pass-plugin-skeleton.md)（转换 Pass 的 C++ 插件入口与骨架）。学完后你应当能够：

- 说清楚「为什么要把 `cf` 控制流结构化成 `scf`」，以及 `cuda_tile` 后端为什么只接受结构化控制流。
- 读懂 `lift-tt-cf-to-scf` 这个 pass 的实现：它复用了 MLIR 的 `transformCFGToSCF` 算法，并针对 `tt.func` 做了哪些适配。
- 准确指出该 pass 依赖哪些方言、对哪些控制流算子生效。
- 解释它为什么必须排在 `convert-triton-to-cuda-tile`（主转换 pass）**之前**。

## 2. 前置知识

在进入源码之前，先建立两个关键直觉。

### 2.1 非结构化控制流（CFG / cf）vs 结构化控制流（scf）

MLIR 里有两套表达「分支与跳转」的方言：

- **`cf`（ControlFlow）方言**：表达的是**非结构化控制流**。它把一个函数体切成若干「基本块（basic block）」，块与块之间用跳转连接，形成一个**控制流图（Control Flow Graph, CFG）**。典型算子：
  - `cf.br`：无条件跳转到某个块。
  - `cf.cond_br`：按条件二选一跳转。
  - `cf.switch`：多路跳转。
  这套表示能力很强（理论上能表达任意 `goto`），但对编译器后端不友好——硬件很难直接执行「任意跳转」。

- **`scf`（Structured Control Flow）方言**：表达的是**结构化控制流**。它不允许任意跳转，只允许嵌套良好的结构，典型算子：
  - `scf.if`：if-then-else。
  - `scf.for`：带步长的计数循环。
  - `scf.while` / `scf.condition`：通用 while 循环。
  - `scf.execute_region`：把一段 CFG 包成一个可被结构化的区域。

> **一句话直觉**：`cf` 是「想跳哪跳哪的 `goto`」，`scf` 是「规规矩矩的 if/for/while」。GPU 与 `cuda_tile` IR 只认后者。

### 2.2 为什么要「结构化（structurize）」

GPU 的执行模型是大量线程并发跑同一段程序。要让分支可被硬件高效执行（例如 warp 内的线程要尽量走同一条路径），编译器需要**结构化**的控制流。因此 `cuda_tile` 后端只接受 `scf`，不接受 `cf`。

但 Triton 的前端在生成 IR 时，某些场景会产生 `cf`（例如 `map_elementwise` 内含条件分支、或内核里有显式分支逻辑）。于是需要一个 **「结构化 pass」**，把任意 CFG 转换成 `scf`，这就是本讲的 `lift-tt-cf-to-scf`。

### 2.3 tt.func vs func.func

MLIR 自带的 `ControlFlowToSCF` 转换只认标准库的 `func.func`，而 Triton 用的是自己的 `tt.func`（TritonDialect 里的函数）。这是本 pass 存在的直接原因——它把 MLIR 那套算法「搬」到 `tt.func` 上来用。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `third_party/tileir/lib/Transform/LiftTTCFToSCF.cpp` | pass 的全部实现：遍历 `tt.func`，调用 MLIR 的 `transformCFGToSCF` 做结构化。 |
| `third_party/tileir/include/Transform/Passes.td` | 用 TableGen 声明 `lift-tt-cf-to-scf` 的名称、说明、构造函数与**依赖方言**。 |
| `third_party/tileir/backend/compiler.py` | `make_tileir` 把这个 pass 挂进编译管线，并决定它的**位置**。 |
| `third_party/tileir/triton_tileir.cc` | pybind 薄壳 `add_lift_tt_cf_to_scf`，把 C++ pass 暴露给 Python。 |
| `third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp` | 主转换 pass 的转换目标，决定 `cf`/`scf` 是否合法、是否有 lowering 模式。 |
| `third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp` | `map_elementwise` 预处理，其 verifier 反向依赖本 pass 先跑。 |

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**cf→scf 结构化转换**、**依赖方言**、**转换链位置**。

### 4.1 cf→scf 结构化转换

#### 4.1.1 概念说明

「结构化（structurize）」要做的事是：给定一个由基本块和 `cf` 跳转组成的任意 CFG，识别出其中隐含的「条件分支 / 循环」结构，重新写成嵌套良好的 `scf.if` / `scf.execute_region` / `scf.index_switch`。

MLIR 已经提供了一个成熟的通用算法 `transformCFGToSCF`（声明在 `mlir/Transforms/CFGToSCF.h`）。它能把任意 CFG 转成 `scf`，但配套的官方 pass `ControlFlowToSCF` 只作用于 `func.func`。Triton 用的是 `tt.func`，所以本 pass 的工作是：**遍历所有 `tt.func`，对它们内部的每个 region 调一次 `transformCFGToSCF`**。

源码文件头部的注释把这件事说得很直白：

```cpp
// Mostly inherited from mlir/Conversion/ControlFlowToSCF/ControlFlowToSCF.cpp
// reason is cfToSCF only supports func.funcOp, we need to operate on tt.funcOp
```

——「大部分继承自上游，原因是上游的 cfToSCF 只支持 `func.func`，而我们需要作用于 `tt.func`」。

#### 4.1.2 核心流程

整个 pass 的执行流程可以概括为：

```
输入：一个 ModuleOp（含若干 tt.func）
  │
  ▼
walk 遍历所有 triton::FuncOp（即 tt.func）
  │   跳过空 body 的函数
  │
  ├── 为当前 tt.func 取得 DominanceInfo（支配关系，结构化算法必需）
  │
  ├── 以 PostOrder（后序）遍历函数内部的算子
  │     对每个算子的每个 Region：
  │       调用 transformCFGToSCF(region, transformation, domInfo)
  │         → cf.cond_br / cf.br / cf.switch 被改写为 scf 结构
  │       若失败 → 中断并 signalPassFailure()
  │       若有改动 → changed = true
  │
  ▼
若整趟没改动 → markAllAnalysesPreserved()（保留分析、不做无谓重算）
```

几个要点：

- **后序遍历（PostOrder）**：先处理最内层、再处理外层。这样内层 region 结构化后，外层看到的 CFG 更规整，结构化更稳定。
- **DominanceInfo（支配信息）**：结构化算法需要知道「某个块是否必然先于另一个块执行」（支配关系），才能识别出 `if` / 循环的边界。代码里通过 `getAnalysis<DominanceInfo>()` 或 `getChildAnalysis<DominanceInfo>(funcOp)` 取得。
- **`transformCFGToSCF` 的返回值是 `FailureOr<bool>`**：失败（`failed`）说明该 region 无法被结构化，此时立刻 `interrupt()` 中断整趟遍历。

#### 4.1.3 源码精读

pass 的主体 `runOnOperation()`：

```cpp
ModuleOp module = getOperation();
TTControlFlowToSCFTransformation transformation;
bool changed = false;

WalkResult walkRes = module.walk([&](triton::FuncOp funcOp) {
  if (funcOp.getBody().empty())
    return WalkResult::advance();

  auto &domInfo = funcOp != module
                      ? getChildAnalysis<DominanceInfo>(funcOp)
                      : getAnalysis<DominanceInfo>();

  auto visitor = [&](Operation *innerOp) -> WalkResult {
    for (Region &reg : innerOp->getRegions()) {
      FailureOr<bool> changedFunc =
          transformCFGToSCF(reg, transformation, domInfo);   // ← 结构化的核心调用
      if (failed(changedFunc))
        return WalkResult::interrupt();
      changed |= *changedFunc;
    }
    return WalkResult::advance();
  };

  if (funcOp->walk<WalkOrder::PostOrder>(visitor).wasInterrupted())
    return WalkResult::interrupt();
  return WalkResult::advance();
});
```

这段代码做了三件事：（1）对每个非空 `tt.func` 取支配信息；（2）用后序遍历把内部每个 `Region` 交给 MLIR 的 `transformCFGToSCF`；（3）失败则标记 pass 失败。详见 [LiftTTCFToSCF.cpp:52-78](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/LiftTTCFToSCF.cpp#L52-L78)。

那它对 `tt.func` 的「适配」体现在哪里？就在第二个参数 `transformation` 上。这是一个自定义的回调对象，用来告诉结构化算法「遇到特殊情况该怎么办」：

```cpp
struct TTControlFlowToSCFTransformation
    : public ControlFlowToSCFTransformation {
  FailureOr<Operation *> createUnreachableTerminator(Location loc,
                                                     OpBuilder &builder,
                                                     Region &region) override {
    Operation *parentOp = region.getParentOp();
    if (auto funcOp = dyn_cast<triton::FuncOp>(parentOp)) {
      SmallVector<Value> rets;
      for (Type ty : funcOp.getResultTypes())
        rets.push_back(getUndefValue(loc, builder, ty));
      return triton::ReturnOp::create(builder, loc, rets).getOperation();
    }
    return ControlFlowToSCFTransformation::createUnreachableTerminator(
        loc, builder, region);
  }
};
```

结构化过程中可能出现「不可达路径」，需要一个合法的终止符（terminator）占位。上游默认实现是为 `func.func` 生成 `func.return`，但 Triton 的函数体要求用 `tt.return`，且返回值类型必须匹配函数签名。于是这里：当父算子是 `tt.func` 时，按其 `getResultTypes()` 逐个用 `getUndefValue`（来自 `ub` 方言的 undef/poison）构造返回值，再 `create` 一个 `tt.return`；否则回落到上游基类的默认实现。详见 [LiftTTCFToSCF.cpp:32-47](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/LiftTTCFToSCF.cpp#L32-L47)。

> 这就是「继承上游算法 + 针对 tt.func 打补丁」的全部秘密：算法本身（`transformCFGToSCF`）是 MLIR 的，本 pass 只贡献了遍历 `tt.func` 的入口和「不可达终止符」这一个回调。

#### 4.1.4 代码实践

**实践目标**：通过阅读源码确认「结构化」到底改写了哪些算子、改写成什么。

**操作步骤**：

1. 打开 [LiftTTCFToSCF.cpp](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/LiftTTCFToSCF.cpp)，注意第 11 行 include 的 `mlir/Transforms/CFGToSCF.h` 与第 18 行 include 的 `mlir/Conversion/ControlFlowToSCF/ControlFlowToSCF.h`——结构化能力全部来自这两个 MLIR 头。
2. 在第 67 行 `transformCFGToSCF(reg, ...)` 处停下：它接收一个 `Region &`。`cf` 控制流就住在 region 的多个 block 里。
3. 对照 MLIR `transformCFGToSCF` 的语义：它会消费 `cf.cond_br`（条件分支）、`cf.br`（无条件跳转）、`cf.switch`（多路跳转），产出 `scf.if` / `scf.execute_region` / `scf.index_switch`。

**需要观察的现象**：本 pass 文件里**并没有**手写 `cf.cond_br → scf.if` 的模式匹配逻辑，结构化的所有细节都封装在 `transformCFGToSCF` 内部。

**预期结果**：本 pass = 「遍历 `tt.func` 的壳」+「`transformCFGToSCF` 的核」+「`tt.return` 不可达终止符回调」。它对 `cf.cond_br`、`cf.br`、`cf.switch` 生效，产物是 `scf` 系列。

> 注：若想实操运行 `-lift-tt-cf-to-scf`，需先用 CMake 构建 `triton-cuda-tile-opt` 工具（构建方法见 [u4-l1](u4-l1-opt-tool-and-lit-tests.md)），本讲为源码阅读型实践，不假设已运行命令。

#### 4.1.5 小练习与答案

**练习 1**：为什么本 pass 不直接手写「`cf.cond_br` → `scf.if`」的 `OpConversionPattern`，而是整体调用 `transformCFGToSCF`？

**参考答案**：因为任意 CFG 到结构化控制流是一个**全局**问题——一个 `cf.cond_br` 是否对应一个 `scf.if`、还是要配合 `scf.execute_region`、是否构成循环，取决于整个 CFG 的支配结构，单看一个算子无法决定。`transformCFGToSCF` 是 MLIR 提供的成熟全局算法，复用它远比手写逐算子模式稳健，也能覆盖 `cf.switch` 等复杂情况。

**练习 2**：`createUnreachableTerminator` 里为什么必须用 `getUndefValue` 逐个填返回值？

**参考答案**：`tt.return` 的操作数类型必须与 `tt.func` 的返回类型一一对应（来自 `funcOp.getResultTypes()`）。不可达路径并没有真实的返回值，所以用 undef/poison（`getUndefValue`）占位，纯粹是为了让 IR 类型合法、能通过 verifier。

---

### 4.2 依赖方言

#### 4.2.1 概念说明

每个 MLIR pass 在 `.td` 里会声明 `dependentDialects`（依赖方言）——意思是「这个 pass 运行前，必须保证这些方言已被加载进 context」。声明依赖有两个意义：

1. **正确性**：pass 读/写这些方言的算子，方言没加载会报错。
2. **可读性**：一眼看出这个 pass 涉及哪些方言，是「谁转谁」。

对于 `lift-tt-cf-to-scf`，依赖方言直接揭示了它的「源与目标」：源是 `cf`，目标是 `scf`，外加 Triton 函数与 ub 占位值。

#### 4.2.2 核心流程

`Passes.td` 中 `LiftTTCFToSCF` 的声明：

```
def LiftTTCFToSCF : Pass<"lift-tt-cf-to-scf", "mlir::ModuleOp"> {
  ...
  let dependentDialects = [
    "mlir::triton::TritonDialect",      // tt.func / tt.return
    "mlir::cf::ControlFlowDialect",     // 源：cf.cond_br / cf.br / cf.switch
    "mlir::scf::SCFDialect",            // 目标：scf.if / scf.execute_region / ...
    "mlir::ub::UBDialect"               // getUndefValue 产出的 undef/poison
  ];
}
```

四个依赖方言各自的角色：

| 方言 | 在本 pass 中的角色 |
| --- | --- |
| `TritonDialect` | pass 的遍历对象 `triton::FuncOp`（`tt.func`）、以及 `tt.return` 终止符。 |
| `ControlFlowDialect`（cf） | **源**：被消费的非结构化跳转 `cf.cond_br` / `cf.br` / `cf.switch`。 |
| `SCFDialect`（scf） | **目标**：结构化后产出的 `scf.if` 等结构。 |
| `UBDialect`（ub） | `createUnreachableTerminator` 用 `getUndefValue` 生成 undef/poison 值。 |

#### 4.2.3 源码精读

TableGen 声明见 [Passes.td:50-59](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/include/Transform/Passes.td#L50-L59)，其中第 58 行即 `dependentDialects`。

C++ 侧，源文件顶部 include 的方言头与依赖一致，并额外引入结构化算法所在的工具库：

```cpp
#include "mlir/Conversion/ControlFlowToSCF/ControlFlowToSCF.h"  // 回调接口基类
#include "mlir/Dialect/ControlFlow/IR/ControlFlowOps.h"          // cf 方言
#include "mlir/Dialect/SCF/IR/SCF.h"                              // scf 方言
#include "mlir/Dialect/UB/IR/UBOps.h"                             // ub（getUndefValue）
#include "mlir/IR/Dominance.h"                                    // DominanceInfo
#include "mlir/Transforms/CFGToSCF.h"                             // transformCFGToSCF 算法
#include "triton/Dialect/Triton/IR/Dialect.h"                     // TritonDialect
```

详见 [LiftTTCFToSCF.cpp:11-21](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/LiftTTCFToSCF.cpp#L11-L21)。

构建侧，`CMakeLists.txt` 把这些方言库与本 pass 链在一起：

```cmake
add_triton_library(TritonTileIRTransforms
  RewriteAssumeWithCudaTile.cpp
  LiftTTCFToSCF.cpp
  AutoGenMemoryToken.cpp
  ...
  LINK_LIBS PUBLIC
  MLIRControlFlowToSCF     # 结构化算法 + 回调接口
  MLIRControlFlowDialect   # cf
  MLIRSCFDialect           # scf
  MLIRFuncDialect          # 基类 createUnreachableTerminator 的回落分支用 func.return
  MLIRUBDialect            # ub
  TritonIR                 # TritonDialect
  ...
)
```

注意：链接清单里有 `MLIRFuncDialect`，但 `.td` 的 `dependentDialects` 并没有列 `func`。原因是——`func` 只在 `createUnreachableTerminator` 的**回落分支**里用到（父算子不是 `tt.func` 时调用基类，基类会生成 `func.return`），正常 Triton 场景不会走这条分支，所以没列进 `dependentDialects`，但链接时仍需它。详见 [CMakeLists.txt:3-22](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/CMakeLists.txt#L3-L22)。

#### 4.2.4 代码实践

**实践目标**：核对 `.td` 声明与 C++ 实现、CMake 链接三处的方言是否一致。

**操作步骤**：

1. 在 [Passes.td:50-59](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/include/Transform/Passes.td#L50-L59) 读出 `dependentDialects`，记下四个方言。
2. 在 [LiftTTCFToSCF.cpp:11-21](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/LiftTTCFToSCF.cpp#L11-L21) 逐一找到对应的 `#include` 头文件。
3. 在 [CMakeLists.txt](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/CMakeLists.txt#L3-L22) 找到对应的 `LINK_LIBS`。

**需要观察的现象**：`cf`（源）与 `scf`（目标）在三处都成对出现；`ub` 与 `triton` 也都在；`func` 只出现在 CMake 链接里。

**预期结果**：依赖方言 = `TritonDialect` + `cf` + `scf` + `ub`，与源码、链接清单吻合。

#### 4.2.5 小练习与答案

**练习 1**：`.td` 里没把 `func` 列入 `dependentDialects`，但 CMake 却链接了 `MLIRFuncDialect`，这是否矛盾？

**参考答案**：不矛盾。`dependentDialects` 表示「pass 运行时**通常**会触达、必须预先加载」的方言；`func` 只在非 `tt.func` 的回落分支里用到，正常路径不触发，故不列入。CMake 链接是**编译/链接期**需求——基类 `ControlFlowToSCFTransformation` 的符号（含 `func` 引用）必须能被链接器解析。二者关注点不同。

**练习 2**：如果 `ub` 方言没加载，本 pass 会在哪里出问题？

**参考答案**：在 `createUnreachableTerminator` 里调用 `getUndefValue(loc, builder, ty)` 时——`getUndefValue` 会创建 `ub` 方言的 undef/poison 值，方言未加载会导致 context 无法识别该类型/算子，结构化失败。

---

### 4.3 转换链位置

#### 4.3.1 概念说明

这个模块回答本讲最核心的问题：**`lift-tt-cf-to-scf` 为什么必须排在 `convert-triton-to-cuda-tile` 之前？**

答案的根据藏在主转换 pass 的「转换目标（ConversionTarget）」里。回忆 [u3-l1](u3-l1-pass-plugin-skeleton.md)：`convert-triton-to-cuda-tile` 用 `applyFullConversion` 强制要求转换后**不残留任何非法算子**。它的目标里，`cf` 和 `scf` **都是非法的**——但区别在于：

- `scf` 算子**有对应的 lowering 模式**（会被转成 `cuda_tile` 的等价结构）。
- `cf` 算子**没有任何 lowering 模式**，一旦残留就直接失败。

所以正确的处理顺序只能是：先用 `lift-tt-cf-to-scf` 把 `cf` 变成 `scf`，再让主转换 pass 把 `scf` 变成 `cuda_tile`。

#### 4.3.2 核心流程

`make_tileir` 里 pass 的挂载顺序（关键看谁在前）：

```
make_tileir(mod, metadata, opt, capability):
  1. add_lift_tt_cf_to_scf(pm)        # ← 本 pass，第一个
  2. add_assume_to_tileir(pm)
  3. add_triton_to_cudatile(pm, ...)  # ← 主转换：cf/scf → cuda_tile
  4. add_auto_gen_memtoken(pm, ...)
  5. add_inliner(pm)
  6. add_fma_fusion(pm)               # 可选
  7. add_strip_debuginfo(pm)
  pm.run(mod, "make_tileir")
  if not only_contain_legal_dialects(mod):
      raise RuntimeError(...)         # 残留非法 op → 编译失败
```

逻辑链条：

```
cf.cond_br/cf.br/cf.switch  （非法，无 lowering 模式）
        │  lift-tt-cf-to-scf（结构化）
        ▼
scf.if / scf.execute_region ...     （非法，但有 lowering 模式）
        │  convert-triton-to-cuda-tile
        ▼
cuda_tile.*                          （合法）
```

#### 4.3.3 源码精读

**位置一：`make_tileir` 把本 pass 挂在最前。** 注释直接点明意图：

```python
# Inherit LiftControlflowToSCF from upstream to adapt to `ControlFlow` within `triton.func`
tileir.passes.add_lift_tt_cf_to_scf(pm)
# The root IR for ttir is builtin moduleOp ...
tileir.passes.add_assume_to_tileir(pm)
tileir.passes.add_triton_to_cudatile(pm, ...)
```

`add_lift_tt_cf_to_scf` 出现在所有 pass 之前，紧跟其后才是主转换。详见 [compiler.py:299-305](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L299-L305)。pybind 薄壳见 [triton_tileir.cc:80-82](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc#L80-L82)。

**位置二：主转换的 ConversionTarget 同时判 `cf`、`scf` 非法。**

```cpp
addLegalDialect<cuda_tile::CudaTileDialect>();
addIllegalDialect<scf::SCFDialect, cf::ControlFlowDialect,
                  mlir::gpu::GPUDialect, triton::TritonDialect,
                  ub::UBDialect>();
```

注意 `cf::ControlFlowDialect` 与 `scf::SCFDialect` **都在非法列表**里。详见 [TritonToTileIRPass.cpp:52-55](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp#L52-L55)。

**位置三：`scf` 有 lowering 模式，`cf` 没有。** 在主转换文件里搜 `class Convert... : public OpConversionPattern`，能找到处理 `scf` 的一系列模式：

- `ConvertIfOp : public OpConversionPattern<scf::IfOp>`（[TritonToTileIRPass.cpp:1235](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp#L1235)）
- `ConvertWhileOp : public OpConversionPattern<scf::WhileOp>`（[:1290](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp#L1290)）
- `ConvertForOp : public OpConversionPattern<scf::ForOp>`（[:1364](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp#L1364)）
- `ConvertYieldOp : public OpConversionPattern<scf::YieldOp>`（[:1557](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp#L1557)）

而整个文件里**找不到**任何 `OpConversionPattern<cf::CondBranchOp>` 或 `cf::SwitchOp>` ——`cf` 没有 lowering 模式。这就是 `cf` 必须先被结构化掉的硬证据。

> **结论**：因为 `cf` 在主转换里「非法且无模式」，而 `scf`「非法但有模式」，所以 `lift-tt-cf-to-scf` 必须先跑，把 `cf` 变成 `scf`，主转换才能接得住。

**补充证据：下游 `map_elementwise` 预处理也依赖它。** `convert-triton-to-cuda-tile` 内部会先跑 `expandMapElementwiseOps`（见 [u3-l3](u3-l3-map-elementwise-expansion.md)），它的 verifier 要求 `map_elementwise` 的 region 只有一个 block：

```cpp
if (!region.hasOneBlock()) {
  op.emitError("map_elementwise region has multiple blocks; "
               "expected lift-tt-cf-to-scf to have run first");
  return failure();
}
```

如果 region 里有 `cf.cond_br` 造成多 block，必须先由 `lift-tt-cf-to-scf` 把它结构化成「单 block + `scf.if`」，才能通过这条校验。错误信息里甚至**点名**了 `lift-tt-cf-to-scf`。详见 [MapElementwiseExpansion.cpp:349-353](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp#L349-L353)。

#### 4.3.4 代码实践

**实践目标**：用三处源码证据，完整论证「`lift-tt-cf-to-scf` 必须在 `convert-triton-to-cuda-tile` 之前」。

**操作步骤**：

1. 打开 [compiler.py:296-320](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L296-L320)，确认 `add_lift_tt_cf_to_scf(pm)` 是 `make_tileir` 里第一个挂载的 pass（第 300 行），主转换 `add_triton_to_cudatile` 在其后（第 305 行）。
2. 打开 [TritonToTileIRPass.cpp:48-67](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp#L48-L67)，确认 `cf::ControlFlowDialect` 在非法列表中。
3. 在 [TritonToTileIRPass.cpp](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp#L1235) 搜索 `cf::` 的 `OpConversionPattern`，确认一个也没有；再确认 `scf::IfOp/WhileOp/ForOp/YieldOp` 都有对应模式。
4. 打开 [MapElementwiseExpansion.cpp:349-353](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp#L349-L353)，读错误信息里对 `lift-tt-cf-to-scf` 的点名。

**需要观察的现象**：`cf` 在主转换里「非法 + 无模式」；`lift_cf` 是 `make_tileir` 第一个 pass；`map_elementwise` 的多 block 错误信息直接要求本 pass 先跑。

**预期结果**：三者形成闭环——`cf`（无模式）必须先→`scf`（有模式）→`cuda_tile`（合法），故 `lift-tt-cf-to-scf` 必须排在主转换之前。

#### 4.3.5 小练习与答案

**练习 1**：如果删除 `make_tileir` 里的 `add_lift_tt_cf_to_scf(pm)`，对一个含有 `cf.cond_br` 的内核会发生什么？

**参考答案**：主转换 `convert-triton-to-cuda-tile` 运行时，`cf.cond_br` 在 ConversionTarget 里非法，又没有 lowering 模式，`applyFullConversion` 会留下残留非法算子；随后 `make_tileir` 末尾的 `only_contain_legal_dialects` 检查不通过，抛出 `RuntimeError("Triton ttir to tileir ir failed. ...")`。若该 `cf.cond_br` 恰好位于某个 `map_elementwise` 的多 block region 内，则会更早地在 `expandMapElementwiseOps` 报「expected lift-tt-cf-to-scf to have run first」。

**练习 2**：`scf` 在主转换里也是非法的，为什么不需要先有个「`scf` 预处理 pass」把它清掉？

**参考答案**：因为「非法」不等于「不能转」。`applyFullConversion` 的规则是：非法算子只要有匹配的 `ConversionPattern` 能把它转成合法算子即可。`scf` 恰好有 `ConvertIfOp/ConvertWhileOp/ConvertForOp/ConvertYieldOp` 等模式，能就地 lowering 成 `cuda_tile`；而 `cf` 没有这类模式，才必须靠外部的 `lift-tt-cf-to-scf` 先转换。

---

## 5. 综合实践

**任务**：模拟一次「带 `cf` 控制流的内核」在 `make_tileir` 里的完整旅程，画出 IR 在各 pass 之间的形态变化，并标出每一步若失败会触发哪个错误。

**操作步骤**：

1. 假设前端生成了如下（简化）的 TTIR 片段，某 `tt.func` 内含一个条件分支：

   ```
   tt.func @kernel(%cond : i1) {
     cf.cond_br %cond, ^bb1, ^bb2
   ^bb1:
     ... (then 分支)
     cf.br ^bb3
   ^bb2:
     ... (else 分支)
     cf.br ^bb3
   ^bb3:
     tt.return
   }
   ```

2. 跟踪它在 `make_tileir` 各 pass 后的形态：
   - **`lift-tt-cf-to-scf` 后**：三个基本块 + 两个 `cf.br` + 一个 `cf.cond_br` 被结构化为单个 block 内的 `scf.if %cond { ... } else { ... }`。
   - **`convert-triton-to-cuda-tile` 后**：`scf.if` 经 `ConvertIfOp` 被转成 `cuda_tile` 的条件结构，整个函数被包进 `cuda_tile.module` / `cuda_tile.entry` 容器。

3. 对每一步标注失败点：
   - 若 `lift-tt-cf-to-scf` 结构化失败 → `signalPassFailure()`（pass 直接失败）。
   - 若本 pass 没跑、`cf.cond_br` 残留到主转换 → `only_contain_legal_dialects` 失败 → `RuntimeError`。
   - 若该 `cf.cond_br` 在 `map_elementwise` 多 block region 内且本 pass 没跑 → `expandMapElementwiseOps` 报「expected lift-tt-cf-to-scf to have run first」。

**预期产出**：一张「IR 形态流转图」+ 一张「失败点对照表」，清晰说明本 pass 在整条链路里「结构化前置」的不可替代性。

> 本实践为源码阅读与 IR 推演型任务，无需实际编译运行；如需用真实 IR 验证，可参照 [u4-l1](u4-l1-opt-tool-and-lit-tests.md) 构建 `triton-cuda-tile-opt` 后跑 `-lift-tt-cf-to-scf`。

## 6. 本讲小结

- `lift-tt-cf-to-scf` 的职责是把 `tt.func` 内的**非结构化** `cf` 控制流（`cf.cond_br` / `cf.br` / `cf.switch`）**结构化**为 `scf`（`scf.if` / `scf.execute_region` / `scf.index_switch`）。
- 它的实现是「薄壳 + 复用」：遍历 `tt.func`、对每个 region 调 MLIR 的 `transformCFGToSCF`，并为 `tt.func` 定制不可达终止符 `tt.return`（用 `ub` 的 undef 填返回值）。存在的原因是上游算法只认 `func.func`、不认 `tt.func`。
- 依赖方言有四个：`TritonDialect`（`tt.func`/`tt.return`）、`cf`（源）、`scf`（目标）、`ub`（undef 占位）。
- 它在 `make_tileir` 里**第一个**挂载，必须排在 `convert-triton-to-cuda-tile` 之前——因为主转换的 ConversionTarget 把 `cf` 判为非法却**没有** `cf` 的 lowering 模式，而 `scf` 有（`ConvertIfOp`/`ConvertWhileOp`/`ConvertForOp`/`ConvertYieldOp`）。
- 它还被下游 `map_elementwise` 预处理反向依赖：后者要求 region 单 block，多 block 的 `cf` 必须先被它结构化，错误信息里直接点名了本 pass。

## 7. 下一步学习建议

- 接着读 [u3-l5](u3-l5-rewrite-assume.md)（`rewrite-assume-with-cuda-tile`），它是 `make_tileir` 里紧跟在 `lift_cf` 之后的预处理 pass，同样排在主转换之前。
- 若想动手跑 pass，进入 [u4-l1](u4-l1-opt-tool-and-lit-tests.md)，学习构建 `triton-cuda-tile-opt`、用 lit/FileCheck 给转换 pass 写测试，把本讲的 IR 推演变成可复现的命令行实验。
- 回到 [u3-l2](u3-l2-core-conversion-pass.md)（核心转换）与 [u3-l6](u3-l6-memory-token.md)（memory token），把 `scf → cuda_tile` 的具体 lowering 模式与转换后的内存模型补全，形成对第三单元的完整认知。
