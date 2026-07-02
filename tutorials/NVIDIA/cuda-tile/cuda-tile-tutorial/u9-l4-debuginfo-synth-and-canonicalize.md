# 调试信息合成与规范化

## 1. 本讲目标

本讲是「优化器与变换 Pass」单元的第四篇，承接 [u6-l3 调试信息属性与位置](u6-l3-debug-info-attrs.md)（DI 模型与校验）与 [u9-l1 Pass 框架与 FuseFMA 融合](u9-l1-passes-and-fusefma.md)（Pass 注册与算术重写），把目光从「写一个变换 Pass」转向两条工程化副线：

1. **调试信息合成**：当上层前端（frontend）还来不及正确发射调试信息时，`SynthesizeDebugInfoScopesPass` 如何兜底合成一套 `DIScope`，让下游仍能产出行号表（line table）。
2. **规范化与公共变换的注册**：`cuda-tile-opt` 除了注册自家 Pass，还注册了 MLIR 内建的 `canonicalize`（规范化）、`cse`（公共子表达式消除）、`inline`（内联）三个全局 Pass；而规范化模式又由 `OpsCanonicalization.td` 声明式驱动。

学完后你应当能够：

- 说清 `SynthesizeDebugInfoScopesPass`「为谁、合成什么、依据什么」三件事，并理解它是「stop-gap（权宜之计）」而非前端 DI 的替代品。
- 画出 `cuda-tile-opt` 的 `main` 里「注册 Pass → 交还 `MlirOptMain`」的骨架，区分 `registerCudaTilePasses`（自家 Pass）与 `registerCanonicalizerPass/registerCSEPass/registerInlinerPass`（MLIR 公共 Pass）。
- 区分 MLIR 规范化的两套机制——`fold()`（折叠，不建新操作）与 `getCanonicalizationPatterns`/DRR（重写，可建新操作），并读懂 `OpsCanonicalization.td` 里 `SelectI1ToNot` 这条声明式规则如何被编译成 `.inc` 并挂到 `SelectOp` 上。
- 亲手跑通「合成 DI → 规范化 → CSE」的端到端流程并解释输出变化。

## 2. 前置知识

阅读本讲前，请确认你已理解以下概念（本讲不再重复展开，只做最小回顾）：

- **MLIR Pass 与 `MlirOptMain`**：MLIR 把变换封装为 Pass，`mlir-opt` 风格的工具用一个全局注册表（registry）收集所有 Pass，再由 `MlirOptMain` 解析命令行、按 `--pass-pipeline` 或裸 flag 构建管线并执行。详见 [u9-l1](u9-l1-passes-and-fusefma.md)。
- **`Pass` vs `InterfacePass`**：`Pass<"name", "OpType">` 绑定到具体操作类型（如 `cuda_tile::ModuleOp`）；`InterfacePass<"name", "Interface">` 绑定到接口（如 `FunctionOpInterface`）。绑定的操作类型决定了 Pass 在管线里必须挂在哪一层。
- **CUDA Tile 的调试信息（DI）层级**（详见 [u6-l3](u6-l3-debug-info-attrs.md)）：`di_file`/`di_compile_unit` 是顶层作用域，`di_subprogram`/`di_lexical_block` 是函数内局部作用域，`di_loc` 把「行号列号 + 局部作用域」打包成一个自定义 `Location`。`DebugInfoVerifier` 要求：函数若带 DI，则其体内操作的 DI 作用域最终都要归约到该函数的同一个 subprogram。
- **MLIR Location**：每个操作都有一个 `loc(...)`，常见形态有 `FileLineColLoc`（`loc("file.py":10:4)`）、`NameLoc`、`OpaqueLoc`、`FusedLoc`、`CallSiteLoc`、`UnknownLoc`。
- **DRR（Declarative Rewrite Rules，声明式重写规则）**：MLIR 用 TableGen 的 `Pat` 描述「源模式 → 目标模式 + 约束」，由 `mlir_tablegen(... -gen-rewriters)` 编译成 C++ 的 `OpRewritePattern`。

一个关键直觉：**规范化（canonicalize）是「把 IR 变成规范形态」的统一入口**，它本身不是某个具体优化，而是把每个操作自己声明的「我希望被这样简化」的规则集中跑一遍。理解了这一点，本讲三个最小模块就串起来了。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [lib/Dialect/CudaTile/Transforms/SynthesizeDebugInfoScopes.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/SynthesizeDebugInfoScopes.cpp) | 本讲主角 Pass 的全部实现：为无 DI 的函数合成 subprogram 与 di_loc |
| [include/cuda_tile/Dialect/CudaTile/Transforms/Passes.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/Transforms/Passes.td) | 用 TableGen 声明 `SynthesizeDebugInfoScopesPass`（含 summary/description/绑定操作类型） |
| [include/cuda_tile/Dialect/CudaTile/Transforms/Passes.h](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/Transforms/Passes.h) | 通过 `GEN_PASS_REGISTRATION` 宏生成 `registerCudaTilePasses()` |
| [tools/cuda-tile-opt/cuda-tile-opt.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-opt/cuda-tile-opt.cpp) | 测试驱动工具入口：注册方言、三个公共 Pass、自家 Pass，交还 `MlirOptMain` |
| [lib/Dialect/CudaTile/IR/OpsCanonicalization.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/OpsCanonicalization.td) | 用 DRR 声明的规范化重写规则（本讲的 `SelectI1ToNot`） |
| [lib/Dialect/CudaTile/IR/CudaTile.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp) | `#include` 生成的 `.inc`、各操作的 `fold()` 与 `getCanonicalizationPatterns` |
| [lib/Dialect/CudaTile/IR/CMakeLists.txt](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CMakeLists.txt) | 把 `OpsCanonicalization.td` 经 `mlir_tablegen(-gen-rewriters)` 编译成 `OpsCanonicalization.inc` |
| [lib/Dialect/CudaTile/Transforms/FuseFMA.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/FuseFMA.cpp) | 展示「规范化 + 融合」协作的经典案例（`AddFOp::getCanonicalizationPatterns`） |
| [test/Transforms/synthesize-debuginfo-scopes.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/synthesize-debuginfo-scopes.mlir) | 该 Pass 的 FileCheck 行为规格（合法/已存在 DI/带文件 loc/复合 loc 四类用例） |

## 4. 核心概念与源码讲解

### 4.1 调试信息合成：SynthesizeDebugInfoScopesPass

#### 4.1.1 概念说明

回想 [u6-l3](u6-l3-debug-info-attrs.md)：CUDA Tile 要产出任何形式的调试信息，光有「简单的文件位置 `loc("f.py":10:4)`」是不够的，必须把一整套 DI 元数据（`di_file` → `di_compile_unit` → `di_subprogram` → `di_lexical_block` → `di_loc`）挂到操作上，且要满足 `DebugInfoVerifier` 的一致性规则。

问题是：**并非所有上层前端都来得及正确发射这套 DI**。一个尚在开发中的前端可能只会给操作打上粗糙的 `loc(unknown)` 或裸的文件位置，就 lowering 出了 IR。如果直接送进后端，因为缺少 subprogram，行号表根本无从生成。

`SynthesizeDebugInfoScopesPass` 就是这个场景下的**兜底（stop-gap）**：它扫描模块里每一个函数式操作，若发现该函数还没有 DI 作用域，就**合成**一个 `di_subprogram`（必要时再合成配套的 `di_file`/`di_compile_unit`），并把函数体内每个操作的 `loc` 包装成指向这个 subprogram 的 `di_loc`，从而至少能产出一份行号表。

> ⚠️ 关键定位：该 Pass **不是**前端 DI 发射的替代品。`Passes.td` 的 description 写得很清楚——它只是「a convenient stop-gap（一个方便的权宜之计）」。前端一旦具备正确的 DI 发射能力，就不应再依赖它。

它的「幂等性」也由此而来：若函数已经有 `di_loc`，说明前端已经（或别的流程已经）给过 DI，本 Pass 会**原样跳过、绝不覆盖**。

#### 4.1.2 核心流程

整个 Pass 的执行可以用下面这段伪代码概括：

```
runOnOperation(module):
    cu = createCompileUnitForLoc(module.getLoc())   # 1 个编译单元 / 模块
    for func in module.getOps<FunctionOpInterface>():
        synthesizeScopeForFunction(func, cu)

synthesizeScopeForFunction(func, cu):
    if func.getLoc() 已经包含 DILocAttr:  return     # 幂等：不覆盖已有 DI
    fileAttr, line = 从 func.getLoc() 尽力抽取 FileLineColLoc
                   否则用 cu 的 fileAttr + line=1 兜底
    sp = DISubprogramAttr(fileAttr, line, name, linkageName=name, cu, scopeLine=line)
    func.walk(op):
        opLoc = 从 op.getLoc() 尽力抽取 FileLineColLoc，否则用上面的 line=1 兜底
        op.setLoc( DILocAttr(opLoc, sp) )            # 每条指令都挂上 di_loc
```

三个要点：

1. **一个模块一个编译单元**：`createCompileUnitForLoc` 以模块自身的 `loc` 为依据建一个 `di_compile_unit`，所有函数共用。
2. **尽力复用真实文件信息**：哪怕前端没给 DI，只要它在 `loc(...)` 里塞了真实的文件名/行号（哪怕包在 `NameLoc`/`FusedLoc`/`CallSiteLoc`/`OpaqueLoc` 里），Pass 都会递归剥出来用，而不是粗暴地一律写成 `<unknown>`。
3. **逐指令打标**：`funcOp->walk(...)` 遍历函数体内**每一个**操作，把它的 `loc` 替换成 `di_loc(原文件loc in subprogram)`。这是「产出行号表」的真正动作——行号表的本质就是「每条指令带一个指向 subprogram 的 di_loc」。

#### 4.1.3 源码精读

**Pass 声明（绑定到 `cuda_tile::ModuleOp`）** —— [Passes.td:L19-L33](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/Transforms/Passes.td#L19-L33)：注意它用的是 `Pass<"synthesize-debug-info-scopes", "::mlir::cuda_tile::ModuleOp">`，**绑定到 `cuda_tile.module`**。这与 `FuseFMA`（绑 `FunctionOpInterface`）、`LoopSplit`（同上）不同——本 Pass 要建「模块级」的编译单元，自然挂在模块上。这也意味着在 `--pass-pipeline` 里它必须嵌在 `cuda_tile.module(...)` 内层（见 4.2.4）。

**入口 `runOnOperation`** —— [SynthesizeDebugInfoScopes.cpp:L114-L123](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/SynthesizeDebugInfoScopes.cpp#L114-L123)：先建编译单元，再对模块内所有 `FunctionOpInterface` 操作（`entry`、`testing$func` 都算）逐一合成作用域。

**编译单元合成** —— [SynthesizeDebugInfoScopes.cpp:L56-L69](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/SynthesizeDebugInfoScopes.cpp#L56-L69)：`createCompileUnitForLoc` 先尝试从 loc 抽文件信息建 `DIFileAttr`，抽不到就用字符串 `<unknown>` 兜底，最后 `DICompileUnitAttr::get(ctx, fileAttr)`。这里用 `Builder` 的 `getType<DIFileAttr>(...)` 构造，因为 DI 属性在 MLIR 里以「类型/属性」形式存在。

**递归剥文件位置** —— [SynthesizeDebugInfoScopes.cpp:L27-L43](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/SynthesizeDebugInfoScopes.cpp#L27-L43)：`extractFileLoc` 用 `TypeSwitch` 递归处理各类 `Location`：`NameLoc` 取子 loc、`OpaqueLoc` 取 fallback loc、`FusedLoc` 取第一个子 `FileLineColLoc`、`CallSiteLoc` 取 caller。这就是「尽力复用真实文件信息」的实现。

**函数作用域合成（核心）** —— [SynthesizeDebugInfoScopes.cpp:L73-L106](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/SynthesizeDebugInfoScopes.cpp#L73-L106)：

- L79-L80 `if (loc->findInstanceOf<DILocAttr>()) return;` —— **幂等守卫**，已有 DI 直接返回。
- L97-L99 `DISubprogramAttr::get(ctx, fileAttr, line, funcName, funcName, compileUnitAttr, /*scopeLine=*/line)` —— 注意 `name` 与 `linkageName` 都填函数名（与 [u6-l3](u6-l3-debug-info-attrs.md) 里 Rule 2「函数名须等于子程序 linkageName」的校验一致），`scopeLine` 取函数所在行。
- L100-L105 `funcOp->walk([&](Operation *op){ ... op->setLoc(DILocAttr::get(...)); })` —— 给体内每条指令挂 `di_loc`，缺行号的用 `line=1` 兜底。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「无 DI 的函数」被合成出完整 DI 链，并理解 `--mlir-print-debuginfo` 开关的作用。

**操作步骤**：

1. 新建 `demo_synth.mlir`，内容如下（函数体内只有 `return`，模块与函数都标 `loc(unknown)`，模拟「前端没给任何 DI/文件信息」）：

   ```mlir
   cuda_tile.module @test {
     testing$func @func_no_debug() {
       return loc(unknown)
     } loc(unknown)
   } loc(unknown)
   ```

2. 用 `cuda-tile-opt` 跑该 Pass。因为 Pass 绑定到 `cuda_tile.module`，必须用 `--pass-pipeline` 嵌套形式，并加 `--mlir-print-debuginfo` 才能看到 DI 属性：

   ```bash
   cuda-tile-opt demo_synth.mlir \
     --pass-pipeline="builtin.module(cuda_tile.module(synthesize-debug-info-scopes))" \
     --mlir-print-debuginfo
   ```

**需要观察的现象**：

- 输出里出现三条新属性：`#cuda_tile.di_file<"<unknown>" in "">`、`#cuda_tile.di_compile_unit<file = ...>`、`#cuda_tile.di_subprogram<..., name = "func_no_debug", linkageName = "func_no_debug", ...>`。
- `return` 与函数本身的 `loc(...)` 变成 `loc(#cuda_tile.di_loc<... in #[[SUBPROGRAM]]>)`。

**预期结果**：与 [test/Transforms/synthesize-debuginfo-scopes.mlir:L3-L14](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/synthesize-debuginfo-scopes.mlir#L3-L14) 的 `@func_no_debug` 用例的 `CHECK` 行一致。若你的环境未编译 `cuda-tile-opt`，本步骤的精确输出**待本地验证**，但上述属性形态以测试文件为权威。

#### 4.1.5 小练习与答案

**练习 1**：如果把函数的 `loc` 改成 `loc("file.py":10:4)`，合成出的 `di_subprogram` 的 `line` 会是多少？`di_file` 的文件名取自哪里？

**答案**：`line = 10`，`di_file` 的 name 取 `llvm::sys::path::filename("file.py")` 即 `file.py`，directory 取其 `parent_path`（这里为空串）。依据 [SynthesizeDebugInfoScopes.cpp:L86-L93](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/SynthesizeDebugInfoScopes.cpp#L86-L93) 与 [test/Transforms/synthesize-debuginfo-scopes.mlir:L44-L51](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/synthesize-debuginfo-scopes.mlir#L44-L51)。

**练习 2**：若函数本身已经带了一个 `loc(#cuda_tile.di_loc<...>)`，Pass 会做什么？为什么？

**答案**：什么都不做，直接返回。依据 [SynthesizeDebugInfoScopes.cpp:L79-L80](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/SynthesizeDebugInfoScopes.cpp#L79-L80) 的 `findInstanceOf<DILocAttr>()` 守卫——保证幂等、不覆盖前端已有 DI。对应测试 [synthesize-debuginfo-scopes.mlir:L18-L35](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/synthesize-debuginfo-scopes.mlir#L18-L35)。

**练习 3**：为什么该 Pass 绑定到 `cuda_tile::ModuleOp` 而不是 `FunctionOpInterface`？

**答案**：因为它要先在模块级建一个共享的 `di_compile_unit`，再把该 CU 分发给模块内所有函数。若绑到函数级，就无处安放「每模块一个 CU」的语义。

---

### 4.2 cuda-tile-opt 的 Pass 注册与公共变换

#### 4.2.1 概念说明

`cuda-tile-opt` 是 CUDA Tile 的「IR 文本测试驱动」，本质就是 MLIR 的 `mlir-opt` 套了一层壳（对比：[u1-l3](u1-l3-toolchain-end-to-end.md) 里的 `cuda-tile-translate` 是 `mlir-translate` 的壳；[u9-l3](u9-l3-cuda-tile-optimize.md) 里的 `cuda-tile-optimize` 是字节码进字节码出的独立优化器，三者各司其职）。

`mlir-opt` 的工作模型是**全局注册表**：

- 启动时，各处调用 `registerXxxPass()` 把「Pass 工厂函数」塞进一个全局表；
- `MlirOptMain` 解析命令行，按 `--xxx` 或 `--pass-pipeline=...` 从表里取出工厂、构建管线、执行。

对 `cuda-tile-opt` 而言，要注册的 Pass 分两类：

| 类别 | 注册函数 | 来源 | 例子 |
| --- | --- | --- | --- |
| **MLIR 公共 Pass** | `mlir::registerCanonicalizerPass()` / `registerCSEPass()` / `registerInlinerPass()` | MLIR 上游 `mlir/Transforms/Passes.h` | `--canonicalize`、`--cse`、`--inline` |
| **CUDA Tile 自家 Pass** | `mlir::cuda_tile::registerCudaTilePasses()` | 由 `Passes.td` 经 TableGen 生成 | `--fuse-fma`、`--loop-split`、`--synthesize-debug-info-scopes` |

公共 Pass 之所以重要，是因为它们是「通用 IR 整理工具」：

- **`canonicalize`（规范化）**：把每个操作自己声明的规范化模式（`fold` + `getCanonicalizationPatterns`，见 4.3）集中跑一遍，化简常量、消除冗余、把操作数排成规范顺序。这是几乎所有优化管线的「前置/后置清理工」。
- **`cse`（Common Subexpression Elimination，公共子表达式消除）**：把两个完全相同、无副作用的操作合并成一个。
- **`inline`（内联）**：把函数调用展开到调用点。

> 这三个 Pass 都是 **op-agnostic（操作无关）** 的：它们不绑定具体操作类型，可以对任意 IR 生效，所以注册一次就能服务于整个 `cuda_tile` 方言。

#### 4.2.2 核心流程

`cuda-tile-opt` 的 `main` 是典型的「薄壳」结构：

```
main(argc, argv):
    1. 声明 cl::opt 命令行选项（-Wunsupported-hints / -Werr-hints）          # 见 u6-l1
    2. 建 DialectRegistry，insert CudaTileDialect
    3. 加一个 extension：方言被加载时，把上面两个 hint 开关注入方言     # 让 hint 行为与命令行一致
    4. registerCanonicalizerPass() / registerCSEPass() / registerInlinerPass()
    5. registerCudaTilePasses()                                          # 自家 Pass
    6. （测试构建）registerTransformsUtilsTestPasses()
    7. return MlirOptMain(argc, argv, "...", registry)                  # 交还 mlir-opt 主循环
```

关键设计：**`main` 几乎不做事，只负责「注册」**。真正的解析、调度、执行全交给 `MlirOptMain`。这与 [u1-l3](u1-l3-toolchain-end-to-end.md) 讲过的 `cuda-tile-translate`「注册必须先于命令行解析」是同一条铁律——所以所有 `registerXxx` 都排在 `MlirOptMain` 之前。

第 3 步的 **Dialect Extension** 是个值得注意的细节：hint 诊断开关（`-Wunsupported-hints`/`-Werr-hints`，详见 [u6-l1](u6-l1-attributes-and-opt-hints.md)）并不是 Pass 选项，而是方言级状态。这里用一个 extension 钩子，在 `CudaTileDialect` 被加载进 context 的那一刻，把命令行读到的布尔值写进方言对象，从而让后续所有验证都读到一致的配置。

#### 4.2.3 源码精读

**命令行选项与 hint 注入** —— [cuda-tile-opt.cpp:L25-L46](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-opt/cuda-tile-opt.cpp#L25-L46)：`cl::opt<bool>` 声明两个开关（默认 `false`），然后用 `registry.addExtension(+[](MLIRContext*, CudaTileDialect* dialect){ dialect->setWarnUnsupportedHints(...); dialect->setErrorOnHints(...); })` 注入。注意 lambda 前的 `+` 是为了让它退化成函数指针（`addExtension` 的签名要求）。

**Pass 注册** —— [cuda-tile-opt.cpp:L48-L51](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-opt/cuda-tile-opt.cpp#L48-L51)：四行注册——前三行是 MLIR 公共 Pass，第四行是 CUDA Tile 自家 Pass。注意 `mlir::registerCanonicalizerPass` 等来自上游头文件 `mlir/Transforms/Passes.h`（见 [cuda-tile-opt.cpp:L13](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-opt/cuda-tile-opt.cpp#L13) 的 `#include`）。

**自家 Pass 的注册入口** —— [Passes.h:L35-L37](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/Transforms/Passes.h#L35-L37)：`#define GEN_PASS_REGISTRATION` 后 `#include "...Passes.h.inc"` 会让 TableGen 生成的 `.inc` 展开出 `registerCudaTilePasses()`，内部对每个在 `Passes.td` 里声明的 Pass 调 `registerPass([]{ return createXxxPass(); })`。这一链路在 [u9-l1](u9-l1-passes-and-fusefma.md) 已详述，本讲只点出它在 `cuda-tile-opt` 里的落点。

**测试 Pass 的条件注册** —— [cuda-tile-opt.cpp:L53-L55](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-opt/cuda-tile-opt.cpp#L53-L55)：`#ifdef CUDA_TILE_ENABLE_TESTING` 包裹的 `registerTransformsUtilsTestPasses()`，只在做测试构建时挂上仅测试用的 Pass（呼应 [u1-l2](u1-l2-repo-and-build.md) 的 `TILE_IR_INCLUDE_TESTS` 宏）。

**交还主循环** —— [cuda-tile-opt.cpp:L57-L58](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-opt/cuda-tile-opt.cpp#L57-L58)：`mlir::asMainReturnCode(mlir::MlirOptMain(argc, argv, "CudaTile test driver\n", registry))`。

#### 4.2.4 代码实践

**实践目标**：用 `--help` 摸清 `cuda-tile-opt` 注册了哪些 Pass，并验证公共 Pass 与自家 Pass 都能调起。

**操作步骤**：

1. 列出全部已注册 Pass：

   ```bash
   cuda-tile-opt --help | grep -E "fuse-fma|loop-split|synthesize-debug-info|canonicalize|cse|inline"
   ```

2. 验证公共 Pass 可用——对 [test/Transforms/fuse-fma.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/fuse-fma.mlir) 只跑 `--canonicalize`（不跑 fuse-fma），观察 `addf` 操作数是否被规范化重排：

   ```bash
   cuda-tile-opt test/Transforms/fuse-fma.mlir --canonicalize --split-input-file
   ```

   重点看 `test_commutative_add_mul_rhs` 用例：`addf %2, %3`（乘积在右）应被规范化为 `addf %3, %2`（乘积在左）。依据见 4.3.3 的 `canonicalizeAddOperands`。

**需要观察的现象**：`--help` 输出里同时出现 MLIR 公共 Pass（`canonicalize`/`cse`/`inline`）与 CUDA Tile 自家 Pass（`fuse-fma`/`loop-split`/`synthesize-debug-info-scopes`）。

**预期结果**：两类 Pass 都在帮助列表中；单独的 `--canonicalize` 能把「乘积在右」的 `addf` 操作数对调。精确输出**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `mlir::registerCanonicalizerPass()` 这一行删掉重新编译，`cuda-tile-opt --canonicalize` 会发生什么？

**答案**：`--canonicalize` 选项会变成「未注册的 Pass」，`MlirOptMain` 解析时直接报错退出。因为命令行到 Pass 的映射就靠这张全局注册表，没注册就没这个选项。

**练习 2**：`cuda-tile-opt` 的 `main` 里，`registry.addExtension(...)` 为什么必须出现在 `MlirOptMain` 之前？为什么用 extension 而不是直接 `dialect->setWarnUnsupportedHints(...)`？

**答案**：必须在 `MlirOptMain` 之前，因为「注册先于解析」。而此时 `CudaTileDialect` 还没被实例化（它要等 IR 真正加载方言时才构造），无法直接调它的方法；extension 是一个「方言加载时再执行」的钩子，把配置延后到方言对象存在的时刻。依据 [cuda-tile-opt.cpp:L38-L46](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-opt/cuda-tile-opt.cpp#L38-L46)。

---

### 4.3 规范化模式：OpsCanonicalization.td 与 fold

#### 4.3.1 概念说明

「规范化（canonicalization）」在 MLIR 里有**两套并存的机制**，都由 `--canonicalize` 这个公共 Pass 统一驱动：

1. **`fold()`（折叠）**：每个操作可以实现 `OpFoldResult OpName::fold(adaptor)`。它的契约很严格——**只能返回一个已存在的 `Value` 或一个常量属性**，**不能创建新操作**（因为 `fold` 在不携带 `PatternRewriter` 的上下文里运行）。典型用途：常量折叠（`constant` 直接返回自己的值）、恒等消除（`select x, x => x`）。
2. **`getCanonicalizationPatterns`（重写模式）**：操作可以提供一个 `RewritePatternSet`，里面是若干 `OpRewritePattern`。这套**可以创建新操作**（因为携带 `PatternRewriter`）。典型用途：把 `select(c, false, true)` 重写成 `xor(c, true)`（建了一个 `xori`）。

`OpsCanonicalization.td` 属于第 2 套机制的**声明式写法**——用 DRR（`Pat`）把「源模式 → 目标模式 + 约束」写成 TableGen 记录，由构建期工具 `mlir_tablegen(-gen-rewriters)` 编译成 C++ 的 `OpRewritePattern`，再在某操作的 `getCanonicalizationPatterns` 里 `results.add<...>` 收纳。

> 这条链路是 [u2-l3](u2-l3-tablegen-codegen.md)「单一数据源」思想的又一体现：你只写一份 `.td`，构建系统同时产出 C++ 胶水与（经规范生成的）人类可读文档，运行时由 `--canonicalize` 调起。

#### 4.3.2 核心流程

DRR 从声明到生效的完整链路：

```
OpsCanonicalization.td                # 声明：Pat<(SelectOp $pred,$f,$t), (XOrIOp $pred,$t), [...]>
        │  set(LLVM_TARGET_DEFINITIONS OpsCanonicalization.td)
        ▼
mlir_tablegen(OpsCanonicalization.inc -gen-rewriters)    # 上游 mlir-tblgen 的 -gen-rewriters 后端
        │  产物：OpsCanonicalization.inc（含若干 struct : OpRewritePattern<...>）
        ▼
CudaTile.cpp:  #include "OpsCanonicalization.inc"        # 在匿名命名空间里纳入这些 struct
        │
        ▼
SelectOp::getCanonicalizationPatterns(results, ctx):     # 操作声明「我希望被这样规范化」
     results.add<SelectI1ToNot, SelectConsts, SelectToExtI>(ctx);
        │
        ▼
--canonicalize  →  applyPatternsGreedily 跑到不动点     # 贪心驱动器统一调度
```

本讲唯一的一条 DRR 是 `SelectI1ToNot`，它把：

```
select(cond, false, true)   // cond=1 返回 false、cond=0 返回 true ⟺ ¬cond
```

重写为：

```
xori(cond, true)            // 即 ¬cond
```

其语义正确性来自一个布尔恒等式：

\[
\text{select}(c, f, t) = (c \land f) \lor (\lnot c \land t)
\]

当 \(f = \text{false}\)、\(t = \text{true}\) 时：

\[
(c \land \text{false}) \lor (\lnot c \land \text{true}) = \lnot c = c \oplus \text{true}
\]

故可替换为 `xori(cond, true)`。

> 注意：DRR 里形参命名为 `$falseVal`/`$trueVal`，但它们是按 `SelectOp` 的**操作数顺序**（`cond, val_if_true, val_if_false`）绑定的。即 `$falseVal` 绑到第 2 个操作数（真正的 true 分支值），约束要求它是常量 `false`；`$trueVal` 绑到第 3 个操作数（真正的 false 分支值），约束要求它是常量 `true`。命名是「按用途」而非「按位置」，初读时容易绕。

**规范化与融合的协作（承接 [u9-l1](u9-l1-passes-and-fusefma.md)）**：规范化不只是省几条指令，它还能**为别的变换铺路**。经典例子在 `FuseFMA`：`MulAddPattern` 只识别 `(a*b)+c` 这种「乘在加号左」的形态；遇到 `z+(a*b)`（乘在右）就识别不了。`FuseFMA` 的 `runOnOperation` 因此先把 `AddFOp::getCanonicalizationPatterns`（内含 `canonicalizeAddOperands`，把乘法搬到左操作数）和融合模式塞进**同一个** `RewritePatternSet`，让贪心驱动器一轮内既重排又融合，跑到不动点。这就是 [fuse-fma.mlir:L316-L335](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/fuse-fma.mlir#L316-L335) `test_commutative_add_mul_rhs` 能被融合的根因。

#### 4.3.3 源码精读

**DRR 声明** —— [OpsCanonicalization.td:L35-L41](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/OpsCanonicalization.td#L35-L41)：`Pat<(CudaTile_SelectOp $pred, $falseVal, $trueVal), (CudaTile_XOrIOp $pred, $trueVal), [(IsConstFalseVal $falseVal), (IsConstTrueVal $trueVal)]>`。两个约束 `IsConstFalseVal`/`IsConstTrueVal` 定义于 [OpsCanonicalization.td:L21-L28](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/OpsCanonicalization.td#L21-L28)，本质是 `CPred<"isConstantFalseVal($0)">` 这样的原生 C++ 谓词。

**构建期编译** —— [lib/Dialect/CudaTile/IR/CMakeLists.txt:L13-L15](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CMakeLists.txt#L13-L15)：`set(LLVM_TARGET_DEFINITIONS OpsCanonicalization.td)` + `mlir_tablegen(OpsCanonicalization.inc -gen-rewriters)` + `add_public_tablegen_target(CudaTileOpsCanonicalizationIncGen)`。注意这里走的是**上游 `mlir_tablegen`**（`-gen-rewriters` 是 MLIR 自带后端），不是 [u2-l3](u2-l3-tablegen-codegen.md) 讲的本项目 `cuda-tile-tblgen`。

**纳入与挂载** —— [CudaTile.cpp:L1401](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1401) 在匿名命名空间里 `#include "OpsCanonicalization.inc"`；[CudaTile.cpp:L5185-L5188](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L5185-L5188) 的 `SelectOp::getCanonicalizationPatterns` 把 DRR 生成的 `SelectI1ToNot` 与两个手写模式 `SelectConsts`、`SelectToExtI` 一起 `results.add<...>`。这就是「声明式规则与手写规则混用」的典型。

**`fold` 的两个例子（对照第 1 套机制）**：

- [CudaTile.cpp:L1744](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1744)：`ConstantOp::fold` 直接 `return getValue();`——常量折叠成自身。
- [CudaTile.cpp:L1484-L1489](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1484-L1489)：`AssumeOp::fold` 在「连续两个 `assume` 谓词完全相同」时折叠掉冗余的那一个（承接 [u6-l2](u6-l2-assume-predicates.md)）。

**规范化为融合铺路** —— [FuseFMA.cpp:L104-L112](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/FuseFMA.cpp#L104-L112)：`runOnOperation` 里先 `AddFOp::getCanonicalizationPatterns(patterns, ...)` 再 `patterns.add<MulAddPattern, MulSubPattern>(...)`，共用一个 `RewritePatternSet` 与 `applyPatternsGreedily`。其调用的 `canonicalizeAddOperands` 定义于 [CudaTile.cpp:L1431-L1446](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1431-L1446)，逻辑即「右操作数是 `MulFOp` 而左不是时，交换两操作数」，由 [CudaTile.cpp:L1448-L1450](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1448-L1450) 的 `AddFOp::canonicalize` 转发。

#### 4.3.4 代码实践

**实践目标**：触发 `SelectI1ToNot` 这条 DRR，把 `select` 看成 `xori`。

**操作步骤**：

1. 新建 `demo_canon.mlir`（条件用函数参数，保证它不是常量，避免被 `select` 的常量条件折叠先吃掉）：

   ```mlir
   cuda_tile.module @test {
     testing$func @demo(%arg0 : !cuda_tile.tile<i1>) -> !cuda_tile.tile<i1> {
       %f = constant <i1: false> : !cuda_tile.tile<i1>
       %t = constant <i1: true>  : !cuda_tile.tile<i1>
       // select(cond, true分支=false, false分支=true)  ⟺ ¬cond
       %r = select %arg0, %f, %t : !cuda_tile.tile<i1>
       return %r : !cuda_tile.tile<i1>
     }
   }
   ```

   > 上面的 `select %arg0, %f, %t` 中，`%f`（false）位于 `val_if_true` 位、`%t`（true）位于 `val_if_false` 位，正好命中 `SelectI1ToNot`。

2. 跑规范化：

   ```bash
   cuda-tile-opt demo_canon.mlir --canonicalize --split-input-file
   ```

**需要观察的现象**：`select` 消失，取而代之的是一个 `xori %arg0, %true`（异或常量 true，即按位取反）。

**预期结果**：输出中 `select` 被替换为 `xori`。由于规范化还可能触发常量相关的其它化简，精确 IR**待本地验证**，但 `select → xori` 这一步由 `SelectI1ToNot` 保证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `SelectI1ToNot` 写成 DRR（`getCanonicalizationPatterns`），而 `ConstantOp::fold` 写成 `fold()`？两者能否互换？

**答案**：`SelectI1ToNot` 要**创建一个新操作 `xori`**，`fold` 契约禁止建新操作，所以只能走 `getCanonicalizationPatterns`。`ConstantOp::fold` 只是「返回自身已存在的值属性」，不建新操作，正适合 `fold`。两者不能互换——把建操作的逻辑塞进 `fold` 会违反契约。

**练习 2**：`canonicalizeAddOperands` 为什么不做「值是否相等」的判断就直接交换操作数？交换后语义改变吗？

**答案**：浮点加法 `addf` 满足交换律（在相同舍入模式下 `a+b == b+a`），所以只交换操作数不改变语义；它的目的是把 `MulFOp` 统一搬到左操作数位，为 `MulAddPattern` 铺路，而非「化简」。依据 [CudaTile.cpp:L1428-L1446](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1428-L1446) 的注释「put multiply operations on the LHS … enables FMA fusion」。

**练习 3**：如果把 `OpsCanonicalization.td` 里 `SelectI1ToNot` 整条删掉重编，`--canonicalize` 对 4.3.4 的输入还会有什么效果？

**答案**：`select(%arg0, false, true)` 不再被重写成 `xori`；其余 `select` 的 `fold`/手写模式（如 `tryFoldSelectSameOperands`、`tryFoldSelectBoolIdentity`）因条件不满足（两操作数不等、非 `select x,true,false` 形态）也不会动它，所以 `select` 会原样保留。

## 5. 综合实践

把本讲三个最小模块串成一条端到端流水线：**对同一个无 DI、含可规范化模式的模块，依次跑「合成 DI → 规范化 → CSE」，对比三个阶段的输出**。

**输入** `demo_all.mlir`（函数无 DI；含一条可被 `SelectI1ToNot` 规范化的 `select`；并故意重复一个常量以观察 CSE）：

```mlir
cuda_tile.module @test {
  testing$func @demo(%arg0 : !cuda_tile.tile<i1>) -> !cuda_tile.tile<i1> {
    %f  = constant <i1: false> : !cuda_tile.tile<i1>
    %t1 = constant <i1: true>  : !cuda_tile.tile<i1>
    %t2 = constant <i1: true>  : !cuda_tile.tile<i1>   // 与 %t1 完全相同，CSE 应去重
    %r1 = select %arg0, %f, %t1 : !cuda_tile.tile<i1>
    %r2 = select %arg0, %f, %t2 : !cuda_tile.tile<i1>   // 规范化后与 %r1 等价
    return %r1 : !cuda_tile.tile<i1>
  } loc(unknown)
} loc(unknown)
```

**分阶段运行**（公共 Pass 用裸 flag；合成 Pass 因绑定 `cuda_tile.module` 用 pass-pipeline）：

```bash
# 阶段 A：只合成 DI
cuda-tile-opt demo_all.mlir \
  --pass-pipeline="builtin.module(cuda_tile.module(synthesize-debug-info-scopes))" \
  --mlir-print-debuginfo

# 阶段 B：只规范化（裸 flag）
cuda-tile-opt demo_all.mlir --canonicalize --split-input-file

# 阶段 C：规范化 + CSE
cuda-tile-opt demo_all.mlir --canonicalize --cse --split-input-file
```

**需要观察并解释的现象**：

1. **阶段 A**：函数体里凭空多出 `di_file`/`di_compile_unit`/`di_subprogram` 三条属性，`return` 与 `select` 的 `loc` 都变成 `di_loc<... in subprogram>`——DI 被合成。但 `select`/`%t2` 等 IR 本身**没有变化**（合成 Pass 不改计算）。
2. **阶段 B**：两个 `select` 都变成 `xori %arg0, %true`；因 `%r1`/`%r2` 现在形式相同，规范化可能进一步处理。注意此时**没有 DI**（没跑合成）。
3. **阶段 C**：在 B 的基础上，重复的常量 `%t1`/`%t2`（以及规范化后等价的结果）被 CSE 合并，IR 更精简。

**预期结果**：三个阶段分别印证「DI 合成不改计算」「规范化改写操作」「CSE 去除冗余」三件事。若需把三件事一次跑完，可在一条 `--pass-pipeline` 里把三个 Pass 都嵌进 `cuda_tile.module(...)` 内（`canonicalize`/`cse` 是操作无关 Pass，可挂在任意层）。精确 IR 输出**待本地验证**，权威依据为 [test/Transforms/synthesize-debuginfo-scopes.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/synthesize-debuginfo-scopes.mlir) 与 [test/Transforms/fuse-fma.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/fuse-fma.mlir)。

> 进阶思考：为什么 `SynthesizeDebugInfoScopesPass` 不放进 `cuda-tile-optimize` 的默认 `-O3` 管线（见 [u9-l3](u9-l3-cuda-tile-optimize.md)）？因为它是「前端兜底」，属于 IR 生产侧的补救，而非优化侧的化简；字节码优化器假设前端已经把该做的 DI 做好了。

## 6. 本讲小结

- **`SynthesizeDebugInfoScopesPass` 是前端 DI 的兜底（stop-gap）**：绑定到 `cuda_tile::ModuleOp`，为无 DI 的函数合成 `di_file`/`di_compile_unit`/`di_subprogram`，并给体内每条指令挂 `di_loc`，从而至少能产出行号表；对已有 `di_loc` 的函数幂等跳过。
- **它尽力复用真实文件信息**：`extractFileLoc` 递归剥开 `NameLoc`/`OpaqueLoc`/`FusedLoc`/`CallSiteLoc`，抽到 `FileLineColLoc` 就用其文件名/行号，抽不到才用 `<unknown>` 与 `line=1` 兜底。
- **`cuda-tile-opt` 是 `mlir-opt` 的薄壳**：`main` 只负责注册——`registerCanonicalizerPass`/`registerCSEPass`/`registerInlinerPass`（MLIR 公共 Pass）+ `registerCudaTilePasses`（自家 Pass），再交还 `MlirOptMain`；hint 开关经 Dialect Extension 注入。
- **规范化有两套机制**：`fold()`（不建新操作，返回已有值/常量）与 `getCanonicalizationPatterns`/DRR（可建新操作）。两者都由 `--canonicalize` 统一驱动。
- **`OpsCanonicalization.td` 是声明式规范化的单一数据源**：`Pat` 经上游 `mlir_tablegen(-gen-rewriters)` 编译成 `OpsCanonicalization.inc`，在 `CudaTile.cpp` 里被 `#include` 并由 `getCanonicalizationPatterns` 收纳；本讲例子是 `SelectI1ToNot`（`select(c,false,true) → xori(c,true)`）。
- **规范化为融合铺路**：`FuseFMA` 把 `AddFOp::getCanonicalizationPatterns`（含 `canonicalizeAddOperands`，把乘法搬到左操作数）与融合模式塞进同一个 `RewritePatternSet`，一轮贪心跑到不动点，使 `z+(a*b)` 这类交换律形态也能被融合。

## 7. 下一步学习建议

- **进入第 10 单元（集成与测试）**：本讲已多次出现「`--pass-pipeline` 嵌套」「`testing$func` 测试操作」「FileCheck 行为规格」，下一讲 [u10-l1 C API 集成接口](u10-l1-capi-integration.md) 会讲如何把 `registerCudaTilePasses`、`registerCudaTileDialect` 通过 C API 暴露给第三方项目，是从「工具」走向「库」的关键一步。
- **补齐测试体系视角**：建议结合 [u10-l3 测试基础设施：lit 与 FileCheck](u10-l3-testing-infrastructure.md) 重读本讲的两个 `.mlir` 测试文件，理解 `RUN` 行里 `%s`/`%t`/`--split-input-file`/`CHECK-DAG` 的约定——你会发现本讲的「合法/已存在 DI/带文件 loc/复合 loc」四类用例正是 lit + FileCheck 的标准写法。
- **继续阅读源码**：若想看清「规范化全貌」，可逐操作浏览 [lib/Dialect/CudaTile/IR/CudaTile.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp) 里所有 `::fold` 与 `::getCanonicalizationPatterns` 的定义（如 `IfOp`、`SelectOp` 的多条模式），并在 `OpsCanonicalization.td` 里尝试为某操作新增一条 `Pat`，跟踪它从 `.td` 到 `.inc` 到被 `--canonicalize` 调起的完整旅程。
