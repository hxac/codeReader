# Pass 框架与 FuseFMA 融合

> 本讲进入「专家·优化器与变换 Pass」单元的第一篇。前面 u4-l3 已经建立了浮点算术与 FMA 的语义基础，本讲把视线从「操作语义」转到「编译期变换」——看 CUDA Tile 如何用一个 MLIR Pass 把分离的 `(a*b)+c` 重写成一条 `fma`，以及这套机制背后的注册框架。

## 1. 本讲目标

学完本讲，读者应该能够：

1. 说清 CUDA Tile 一个变换 Pass 是如何从 `Passes.td` 的声明，经 TableGen 生成胶水代码，到被 `registerCudaTilePasses` 注册、最终由 `cuda-tile-opt` 调起的完整链路。
2. 逐行讲清 `FuseFMA` 的两条重写规则 `MulAddPattern` 与 `MulSubPattern`：它们匹配什么、拒绝什么、如何保持舍入模式与 FTZ 一致、为何要求乘法「单一使用」。
3. 解释为什么 `FuseFMA` 被明确标注为「非数值保持（non-numeric-preserving）」，理解「双轮舍入 → 单轮舍入」对结果位级的影响。
4. 读懂 `fuse-fma.mlir` 中的正例与反例，并自己动手用 `cuda-tile-opt` 跑出融合前后的 IR。

## 2. 前置知识

在进入源码前，先用三段话补齐本讲需要的几个 MLIR 概念。

**MLIR Pass 是什么。** MLIR 把「对 IR 做一次遍历变换」抽象成 `Pass`。一个 Pass 有一个入口方法 `runOnOperation()`，框架会在合适的操作（Operation）上调用它。Pass 不直接改 IR，而是通过 `PatternRewriter` 提交修改，这样可以安全地做模式匹配与重写。

**OpRewritePattern 与贪心驱动器。** 「模式重写」是 MLIR 变换的主力写法：你继承 `OpRewritePattern<某Op>`，实现 `matchAndRewrite`，里面先判断当前操作是否满足条件（match），满足就用 rewriter 构造新操作替换旧的（rewrite）。一组 pattern 交给 `applyPatternsGreedily`（贪心模式重写驱动器）后，它会反复地在 IR 上应用这些 pattern，直到没有任何 pattern 还能匹配——这叫「不动点（fixpoint）」。`FuseFMA` 用的就是这套机制。

**Pass vs InterfacePass。** TableGen 里声明 Pass 时有两种基类：`Pass<"名字", "::某::OpType">` 把 Pass 绑定到**某个具体操作类型**；`InterfacePass<"名字", "某Interface">` 则把 Pass 绑定到**实现某个接口的所有操作**。后者更灵活——只要操作实现了 `FunctionOpInterface`，Pass 就能在它上面跑，不必管它是 `entry` 还是测试用的 `testing$func`。本讲的 `FuseFMA` 正是 `InterfacePass`。

> 承接 u4-l3：那里讲过 `fma`（融合乘加）只在扩展精度下舍入一次，精度高于分离的 `mulf+addf`。本讲就是「编译器自动把后者变成前者」的实现。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `include/cuda_tile/Dialect/CudaTile/Transforms/Passes.td` | 用 TableGen 声明所有变换 Pass（FuseFMA / LoopSplit / SynthesizeDebugInfoScopes），是 Pass 的「单一数据源」 |
| `include/cuda_tile/Dialect/CudaTile/Transforms/Passes.h` | 通过 `GEN_PASS_REGISTRATION` 宏触发生成 `registerCudaTilePasses` 等注册函数，供外部调起 |
| `lib/Dialect/CudaTile/Transforms/FuseFMA.cpp` | `FuseFMA` Pass 的全部实现：`MulAddPattern`、`MulSubPattern` 与 `runOnOperation` |
| `lib/Dialect/CudaTile/IR/CudaTile.cpp`（AddFOp 段） | `AddFOp` 的手写规范化：把乘法换到加法左操作数，为 FMA 融合铺路 |
| `tools/cuda-tile-opt/cuda-tile-opt.cpp` | 测试驱动工具，调用 `registerCudaTilePasses` 把 Pass 挂进 `mlir-opt` 主循环 |
| `test/Transforms/fuse-fma.mlir` | 覆盖融合正例与各类反例（舍入不一致、FTZ 不一致、多处使用等）的 lit/FileCheck 测试 |

## 4. 核心概念与源码讲解

### 4.1 Pass 注册框架：从 .td 到 registerCudaTilePasses

#### 4.1.1 概念说明

CUDA Tile 的每个变换 Pass 都遵循 MLIR 的标准套路：**声明在 `.td`，由 TableGen 生成 C++ 胶水，由一个 `registerXxxPasses` 函数统一注册到全局 PassRegistry，最后被某个工具（如 `cuda-tile-opt`）调起**。理解这条链路，是理解「新增一个 Pass 要动哪些地方」的前提，也是 u9 后续几讲（LoopSplit、cuda-tile-optimize）的共同骨架。

#### 4.1.2 核心流程

```
Passes.td 声明 FuseFMAPass
        │  TableGen (mlir-tblgen, 生成 Passes.h.inc)
        ▼
Passes.h 用 #define GEN_PASS_REGISTRATION + #include 触发
        │  生成 registerCudaTilePasses()
        ▼
cuda-tile-opt.cpp 调用 registerCudaTilePasses()
        │  注册到 MLIR 全局 PassRegistry
        ▼
命令行 --pass-pipeline='...(fuse-fma)' 或 -fuse-fma 调起
        │  框架在 FunctionOpInterface 操作上调用 runOnOperation()
        ▼
FuseFMA.cpp 的 runOnOperation 执行实际重写
```

#### 4.1.3 源码精读

**Pass 的声明：`InterfacePass` 与 `Pass` 的差别。** `FuseFMA` 用 `InterfacePass` 绑定到 `mlir::FunctionOpInterface`，而同文件里的 `SynthesizeDebugInfoScopesPass` 用 `Pass` 绑定到具体的 `ModuleOp`：

- [Passes.td:L19-L20](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/Transforms/Passes.td#L19-L20) —— `SynthesizeDebugInfoScopesPass` 用 `Pass<"...", "::mlir::cuda_tile::ModuleOp">`，只在模块上跑。
- [Passes.td:L39-L41](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/Transforms/Passes.td#L39-L41) —— `FuseFMAPass` 用 `InterfacePass<"fuse-fma", "mlir::FunctionOpInterface">`，在任何实现了 `FunctionOpInterface` 的操作（`entry`、`testing$func` 等）上都能跑。

**`FuseFMA` 的自我说明。** 它的 `summary` 直接点明这是「非数值保持」的：

- [Passes.td:L42-L58](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/Transforms/Passes.td#L42-L58) —— `description` 明确写出两条 pattern：`MulAddPattern: (a*b)+c → FMA(a,b,c)` 与 `MulSubPattern: (a*b)-c → FMA(a,b,-c)`，并声明「NON-NUMERIC-PRESERVING」「Preserves rounding modes/FTZ modifiers, requires single-use multiply」。这段文字既是文档，也是行为契约。

**注册函数的生成。** `Passes.h` 里两个宏加一行 include 就是全部：

- [Passes.h:L35-L37](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/Transforms/Passes.h#L35-L37) —— `#define GEN_PASS_REGISTRATION` 会让 TableGen 生成的 `Passes.h.inc` 展开出一个 `registerCudaTilePasses()` 函数，内部对每个 Pass 调 `registerPass([]{ return createXxxPass(); })`。`GEN_PASS_DECL` 则展开各 Pass 的基类声明（如 `impl::FuseFMAPassBase`）。

注意 [Passes.h:L22-L32](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/Transforms/Passes.h#L22-L32) 的 `TileIROptimizationsOpts` 是给 u9-l3 的 `cuda-tile-optimize` 整体管线用的（控制是否做 CSE、canonicalize 前后、loop_split 阈值），与本讲的独立 `fuse-fma` Pass 无直接关系，但同样落在 `Transforms` 头里，便于复用。

**工具侧的调起。** `cuda-tile-opt` 是 MLIR `mlir-opt` 的薄壳，注册完 Pass 后就交给 `MlirOptMain`：

- [cuda-tile-opt.cpp:L48-L51](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-opt/cuda-tile-opt.cpp#L48-L51) —— 依次注册 `canonicalize`/`CSE`/`inliner` 三个内建 Pass，再调 `registerCudaTilePasses()` 注册本方言的三个变换 Pass。这解释了为什么 `cuda-tile-opt` 既能跑 `--canonicalize`、`--cse`，也能跑 `--fuse-fma`。

#### 4.1.4 代码实践

1. **实践目标：** 确认 `fuse-fma` Pass 已被注册、可被命令行调起。
2. **操作步骤：** 在已构建（按 u1-l2 开启 `CUDA_TILE_ENABLE_TESTING=ON`、`CUDA_TILE_ENABLE_TOOLS=ON`）的环境里执行：
   ```bash
   build/bin/cuda-tile-opt --help | grep -i fuse-fma
   ```
3. **需要观察的现象：** 帮助输出里应出现 `--fuse-fma` 选项及其 summary。
4. **预期结果：** 能看到一行形如 `--fuse-fma` 的选项描述，对应 `Passes.td` 中的 summary。
5. **若未构建：** 待本地验证。也可只读源码：从 `Passes.td:L39` 的声明名 `fuse-fma` 追到注册宏，确认它必然生成 `-fuse-fma` 命令行选项。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `FuseFMA` 选 `InterfacePass` + `FunctionOpInterface`，而不是像 `SynthesizeDebugInfoScopesPass` 那样绑定到某个具体 Op？

**参考答案：** FMA 融合是对「函数体内逐条指令」做局部重写，凡是「有函数体、能装指令」的操作（`entry`、测试用的 `testing$func`）都该被处理。`InterfacePass` 让 Pass 作用于**所有实现 `FunctionOpInterface` 的操作**，不必为每一种函数式操作各写一个 Pass；而 `SynthesizeDebugInfoScopes` 是模块级的元信息合成，天然属于 `ModuleOp`，所以用具体 `Pass`。

**练习 2：** 如果要新增一个只对 `entry` 操作生效的 Pass，应该用哪种基类？两条路各有什么代价？

**参考答案：** 既可以用 `InterfacePass<..., "mlir::FunctionOpInterface">` 然后在 `runOnOperation` 里用 `isa<EntryOp>` 过滤（复用面广，但要手写类型判断），也可以用一个绑定到 `EntryOp` 的 `Pass`（更精确，但若将来 `testing$func` 也要支持就得再加声明）。CUDA Tile 的现有 Pass 多选前者以保持统一。

---

### 4.2 两条重写规则：MulAddPattern 与 MulSubPattern

#### 4.2.1 概念说明

`FuseFMA` 的全部「智能」集中在两条 `OpRewritePattern`：

- `MulAddPattern`：匹配 `addf`，当其一个操作数来自一条 `mulf` 且该 `mulf` 只被这一处使用时，把 `(a*b)+c` 重写为 `fma(a,b,c)`。
- `MulSubPattern`：匹配 `subf`，同样条件下把 `(a*b)-c` 重写为 `fma(a,b,-c)`——注意减法要先把 `c` 取负（`negf`）。

两条规则共享同一组「准入条件」：乘法必须单一使用、舍入模式与 FTZ 必须前后一致。理解这些条件，就理解了 Pass 的边界。

#### 4.2.2 核心流程

`MulAddPattern` 的判定与重写可以画成：

```
看到一条 addf op：
  ┌─ op.lhs 的定义操作是 mulf ab 吗？ 且 ab 结果只被这一处使用吗？
  │     否 → notifyMatchFailure，放弃
  │     是 → c = op.rhs；a,b = ab 的两个操作数
  │
  ├─ addf 的 rounding/ftz 与 ab 的 rounding/ftz 完全一致吗？
  │     否 → 放弃（不能改变舍入语义）
  │     是 → 继续
  │
  └─ 用 rewriter 创建 fma(a, b, c, rounding, ftz?)，替换掉 addf；
        再 eraseOp(ab) 删掉已成死代码的 mulf。
```

`MulSubPattern` 几乎一样，差别仅在 `c = negf(op.rhs)`，并且要求 `mulf` 在 `subf` 的**左**操作数上。

#### 4.2.3 源码精读

**`MulAddPattern`：匹配与单一使用检查。**

- [FuseFMA.cpp:L22-L35](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/FuseFMA.cpp#L22-L35) —— `ab = op.getLhs().getDefiningOp<MulFOp>()` 取左操作数的定义操作并断言它是 `MulFOp`；`ab.getResult().hasOneUse()` 要求这条乘法**只被当前 `addf` 引用**。若不满足，`notifyMatchFailure` 告知原因并返回，驱动器会跳过。

**为什么必须「单一使用」？** 因为重写会用 `fma` 替换 `addf`，并紧接着 `eraseOp(ab)` 删除 `mulf`。如果 `mulf` 的结果还被别处使用，删除它就会破坏其它计算——`hasOneUse()` 正是这道安全锁。

**舍入与 FTZ 一致性检查。**

- [FuseFMA.cpp:L40-L46](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/FuseFMA.cpp#L40-L46) —— 取 `addf` 的 `flushToZero` 与 `roundingMode`，与 `mulf` 的逐一比较；任一不符即放弃。这条约束保证融合**不擅自改变**操作的舍入模式或 FTZ 修饰——只把「两步、各带一种舍入」改成「一步、带同一种舍入」。注意它只比较「是否相同」，不限制取哪种值（只要前后一致即可）。

**构造 `fma` 并清理。**

- [FuseFMA.cpp:L48-L53](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/FuseFMA.cpp#L48-L53) —— `replaceOpWithNewOp<FmaOp>(op, a, b, c, RoundingModeAttr, ftz?UnitAttr:nullptr)` 创建 `fma`。这里 `fma` 的参数顺序是 `(lhs, rhs, acc, rounding_mode, flush_to_zero)`（与 [Ops.td:L2081-L2086](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2081-L2086) 的 `FmaOp` 定义一致）。`flush_to_zero` 是 `UnitAttr`（存在即开启），所以 `ftz ? rewriter.getUnitAttr() : nullptr` 直接把布尔转成「有/无属性」。最后 `eraseOp(ab)` 收掉死掉的乘法。

**`MulSubPattern`：减法的取负。**

- [FuseFMA.cpp:L57-L72](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/FuseFMA.cpp#L57-L72) —— 与 `MulAddPattern` 同构，关键差别在 [FuseFMA.cpp:L69](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/FuseFMA.cpp#L69)：`c = rewriter.createOrFold< NegFOp >(loc, op.getRhs())`，即 \((a*b)-c = a*b + (-c) = \text{fma}(a,b,-c)\)。`createOrFold` 会优先用 fold 规则简化（例如对常量直接算出相反数），否则才生成一条真正的 `negf`。

**Pass 入口：装配 pattern 并跑到不动点。**

- [FuseFMA.cpp:L104-L112](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/FuseFMA.cpp#L104-L112) —— `runOnOperation` 做三件事：① [L107](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/FuseFMA.cpp#L107) 把 `AddFOp` 的规范化 pattern 加进来（见 4.3）；② [L109](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/FuseFMA.cpp#L109) 加本 Pass 自己的两条 pattern；③ [L110-L111](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/FuseFMA.cpp#L110-L111) 用 `applyPatternsGreedily` 反复应用到不动点，失败则 `signalPassFailure`。

#### 4.2.4 代码实践

1. **实践目标：** 亲手把一段 `(a*b)+c` 跑成 `fma`，并构造一个「乘法多处使用」的反例确认它**不**被融合。
2. **操作步骤：** 新建 `my_fuse.mlir`，内容如下（模仿 [fuse-fma.mlir:L9-L20](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/fuse-fma.mlir#L9-L20) 的结构）：
   ```mlir
   cuda_tile.module @test {
     cuda_tile.testing$func @mul_add() -> !cuda_tile.tile<f32> {
       %0 = constant <f32: 2.0> : !cuda_tile.tile<f32>
       %1 = constant <f32: 3.0> : !cuda_tile.tile<f32>
       %2 = constant <f32: 4.0> : !cuda_tile.tile<f32>
       %3 = cuda_tile.mulf %0, %1 rounding<nearest_even> : !cuda_tile.tile<f32>
       %4 = cuda_tile.addf %3, %2 rounding<nearest_even> : !cuda_tile.tile<f32>
       return %4 : !cuda_tile.tile<f32>
     }
   }
   ```
   用测试同款的嵌套管线运行（因为 `fuse-fma` 是作用于 `FunctionOpInterface` 的 `InterfacePass`，需指明嵌套层级）：
   ```bash
   build/bin/cuda-tile-opt my_fuse.mlir \
     --pass-pipeline='builtin.module(cuda_tile.module(cuda_tile.testing$func(fuse-fma)))'
   ```
3. **需要观察的现象：** 输出里 `%3 = cuda_tile.mulf` 与 `%4 = cuda_tile.addf` 消失，变成一条 `fma %..., %..., %... : tile<f32>`。
4. **预期结果：** 与 `fuse-fma.mlir` 的 `CHECK: fma` / `CHECK-NOT: mulf` / `CHECK-NOT: addf` 断言一致。
5. **反例试验：** 把 `mulf` 的结果同时喂给 `addf` 和 `subf`（参照 [fuse-fma.mlir:L299-L311](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/fuse-fma.mlir#L299-L311) 的 `test_multiple_uses`），再跑同一管线，应观察到 `mulf/addf/subf` 全部保留、**没有** `fma`——因为 `hasOneUse()` 不成立。
6. **若无法运行：** 待本地验证；可改为「源码阅读型实践」——对照 `FuseFMA.cpp:L30-L34` 解释为何反例必然不被融合。

#### 4.2.5 小练习与答案

**练习 1：** `MulSubPattern` 里为什么用 `createOrFold<NegFOp>` 而不是 `create<NegFOp>`？

**参考答案：** `createOrFold` 会先尝试用操作的 fold 规则常量折叠；当 `c` 是常量（如 `constant <f32: 4.0>`）时，能直接得到 `-4.0` 的常量而不生成一条额外的 `negf` 指令，IR 更干净。只有 fold 失败（`c` 是非常量 SSA 值）时才会真正生成 `negf`。`fuse-fma.mlir` 的 `test_mul_sub_bcast_fusion` 里就显式 `CHECK: negf`，说明那里 fold 没能消掉。

**练习 2：** 若把 `MulAddPattern` 的 `hasOneUse()` 检查去掉，会发生什么？

**参考答案：** 当 `mulf` 结果被多处使用时，重写后 `eraseOp(ab)` 会删除一个仍被其它操作引用的 `mulf`，直接破坏 SSA 合法性（出现悬空引用），验证阶段会报错。`hasOneUse()` 是保证「删除安全」的前置条件。

---

### 4.3 「非数值保持」：双轮舍入到单轮舍入，以及规范化的配合

#### 4.3.1 概念说明

`FuseFMA` 最容易被忽视、却最重要的属性是：它**改变了数值结果**。`Passes.td` 的 summary 里专门写了 `(non-numeric-preserving)`。本模块讲清两件事——① 为什么 `(mulf+addf) → fma` 会改变位级结果；② `FuseFMA` 为什么还要顺便挂上 `AddFOp` 的规范化 pattern。

#### 4.3.2 核心流程

**双轮 vs 单轮舍入。** 设 `fl(x)` 表示把数学值 `x` 舍入到目标浮点精度：

- 分离形式：先算 `p = fl(a·b)`（第一次舍入），再算 `fl(p + c)`（第二次舍入），结果是 `fl(fl(a·b) + c)`——**两次舍入**。
- 融合形式：在扩展精度下算完 `a·b + c` 再舍入一次，结果是 `fl(a·b + c)`——**一次舍入**，且这是 `a·b+c` 的正确舍入（correctly rounded）值。

二者一般不等：

\[
\text{fl}\bigl(\text{fl}(a\cdot b) + c\bigr) \;\neq\; \text{fl}(a\cdot b + c) \quad \text{(in general)}
\]

经典的差异来自 `a·b` 本身不可精确表示时，第一次舍入丢掉的那部分信息在融合形式里被保留了下来，参与最后一次加法。因此对「位精确（bit-exact）」有严格要求的应用（如与某参考实现的逐位比对），不能随意开启 `FuseFMA`——这正是把它做成显式 Pass、而非默认必做规范化的原因。

**规范化的配合。** `AddFOp` 的规范化会把「乘法在右操作数」重排成「乘法在左操作数」，即 `addf(z, a*b) → addf(a*b, z)`，因为 `MulAddPattern` 只看左操作数。重排后 `MulAddPattern` 才能命中。两个 pattern 放进同一个 pattern set、交给同一个贪心驱动器，规范化和融合就能在同一轮里交替推进。

#### 4.3.3 源码精读

**「非数值保持」的契约。** 这一点写在 `Passes.td` 的 description 里，是 Pass 行为的一部分：

- [Passes.td:L42](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/Transforms/Passes.td#L42) —— summary 末尾的 `(non-numeric-preserving)`，以及 [Passes.td:L46-L47](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/Transforms/Passes.td#L46-L47) 的 `NON-NUMERIC-PRESERVING: Changes rounding behavior from double-round to single-round FMA, affecting exact bit patterns.`。这段文字同时是给前端的警告：开启即接受位级变化。

**为何要把 `AddFOp` 规范化挂进来。**

- [FuseFMA.cpp:L107](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/FuseFMA.cpp#L107) —— `AddFOp::getCanonicalizationPatterns(...)` 把 `addf` 的规范化模式并入当前 pattern set。它对应 [Ops.td:L185](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L185) 的 `hasCanonicalizeMethod = 1`，手写实现位于 CudaTile.cpp：

- [CudaTile.cpp:L1431-L1446](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1431-L1446) —— `canonicalizeAddOperands`：当右操作数是 `MulFOp` 而左操作数不是时，用 `replaceOpWithNewOp<AddFOp>(op, rhs, lhs, ...)` 交换两个操作数（同时原样搬运 `rounding` 与 `ftz`），把乘法搬到左边。

- [CudaTile.cpp:L1448-L1450](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1448-L1450) —— `AddFOp::canonicalize` 只是把调用转发给上面的 `canonicalizeAddOperands`。

这条规范化是 `fuse-fma.mlir` 里 `test_commutative_add_mul_rhs`（`addf(z, a*b)` 形态）能被融合的根本原因——没有它，`MulAddPattern` 看左操作数不是乘法就直接放弃了。注释 [CudaTile.cpp:L1428-L1429](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1428-L1429) 也写明：「put multiply operations on the LHS … enables FMA fusion」。

> 注意：`SubFOp` 没有挂这种「把乘法搬到左操作数」的规范化（`subf` 不可交换）。所以 `MulSubPattern` 在 [FuseFMA.cpp:L67](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/FuseFMA.cpp#L67) 直接要求 `mulf` 已经在左操作数上。

#### 4.3.4 代码实践

1. **实践目标：** 观察舍入不一致与 FTZ 不一致时融合被拒绝，以及交换律形态被规范化后融合。
2. **操作步骤：** 准备两段 IR。第一段「舍入不一致」对应 [fuse-fma.mlir:L193-L203](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/fuse-fma.mlir#L193-L203)（`mulf ... nearest_even` 配 `addf ... zero`），第二段「交换律」对应 [fuse-fma.mlir:L323-L334](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/fuse-fma.mlir#L323-L334)（`addf(z, a*b)` 形态）。各用 4.2.4 的嵌套管线跑一遍。
3. **需要观察的现象：** 舍入不一致那段保留 `mulf`+`addf`、无 `fma`；交换律那段最终出现 `fma` 且没有 `addf`/`mulf`。
4. **预期结果：** 分别匹配 `test_different_rounding`（`CHECK-NOT: fma`）与 `test_commutative_add_mul_rhs`（`CHECK: fma`、`CHECK-NOT: addf`）的断言。
5. **数值含义思考：** 把第一段的 `mulf` 也改成 `rounding<zero>`（与 `addf` 一致）再跑，应重新被融合——这印证「只看是否一致、不限取值」。
6. **若无法运行：** 待本地验证；可改为阅读 [FuseFMA.cpp:L44-L46](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/FuseFMA.cpp#L44-L46) 解释两种不一致分别由哪个比较拦截。

#### 4.3.5 小练习与答案

**练习 1：** 为什么 `FuseFMA` 把规范化 pattern 和融合 pattern 放进**同一个** `RewritePatternSet` 交给同一个贪心驱动器，而不是先单独跑 `--canonicalize` 再跑 `--fuse-fma`？

**参考答案：** 贪心驱动器会在同一轮里反复交替应用所有 pattern 直到不动点。把规范化与融合同置，可以让「重排操作数 → 触发融合 → 释放新的可重排机会」的链式反应在**一次**遍历里收敛到底（例如 `test_chained_commutative` 里两条 FMA 链）。若分两次跑，也能成功，但需要调用者记得按顺序串起来；同置则保证 `--fuse-fma` 单独使用就足够。

**练习 2：** `FuseFMA` 是「非数值保持」的。这意味着把它接入默认优化管线时，谁需要为此负责？

**参考答案：** 调用方（前端或 u9-l3 的 `cuda-tile-optimize`/`optimizeTileIR`）需要显式选择开启，并在文档/规范层面告知用户「结果位级可能变化」。这也是为什么 `Passes.td` 的 description 要用大写 `NON-NUMERIC-PRESERVING` 高亮——它是 Pass 的不可协商属性，不是实现细节。

---

### 4.4 fuse-fma.mlir 测试：用 FileCheck 钉死正反例

#### 4.4.1 概念说明

一个变换 Pass 没有测试就等于不存在。`fuse-fma.mlir` 用 LLVM 的 `lit` + `FileCheck` 体系，把 `FuseFMA` 的每一条匹配条件都钉成一个用例：正例断言「出现 `fma`、消失 `mulf/addf`」，反例断言「保留原操作、不出现 `fma`」。读这套测试，等于读一份 Pass 行为规格表。

#### 4.4.2 核心流程

```
RUN 行指定运行命令与 pipeline
   │
每个 // ----- 分隔一个独立用例
   │
用例里写输入 IR，CHECK/CHECK-NOT 行写期望
   │
lit 调起 cuda-tile-opt，FileCheck 按顺序比对输出
```

`CHECK` 要求输出中**存在**匹配行；`CHECK-NOT` 要求直到下一个 CHECK 之前**不存在**匹配行。`CHECK-LABEL` 用函数名作锚点，把输出切成与用例对应的段。

#### 4.4.3 源码精读

**RUN 行与嵌套 pipeline。**

- [fuse-fma.mlir:L1](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/fuse-fma.mlir#L1) —— `RUN: cuda-tile-opt %s --pass-pipeline='builtin.module(cuda_tile.module(cuda_tile.testing$func(fuse-fma)))' --split-input-file | FileCheck %s`。`--split-input-file` 让 `// -----` 把单个文件切成多个独立测试；嵌套 pipeline 指明 `fuse-fma` 跑在最内层的 `testing$func` 上（因为它是 `FunctionOpInterface` 上的 `InterfacePass`）。

**正例：基本融合。**

- [fuse-fma.mlir:L9-L20](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/fuse-fma.mlir#L9-L20) —— `test_mul_add_fusion`：`mulf` 与 `addf` 都带 `rounding<nearest_even>`，命中 `MulAddPattern`。[fuse-fma.mlir:L5-L7](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/fuse-fma.mlir#L5-L7) 的 `CHECK: fma` + `CHECK-NOT: mulf` + `CHECK-NOT: addf` 钉死结果。

紧随其后的 [fuse-fma.mlir:L30-L41](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/fuse-fma.mlir#L30-L41) 与 [fuse-fma.mlir:L51-L62](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/fuse-fma.mlir#L51-L62) 用同一函数名验证「省略舍入模式时也能融合」——因为默认舍入就是 `nearest_even`，两边一致即融合。

**反例：舍入不一致、FTZ 不一致、多处使用。**

- [fuse-fma.mlir:L193-L204](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/fuse-fma.mlir#L193-L204) —— `test_different_rounding`：`mulf nearest_even` 配 `addf zero`，[fuse-fma.mlir:L189-L191](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/fuse-fma.mlir#L189-L191) 用 `CHECK: mulf`/`CHECK: addf`/`CHECK-NOT: fma` 断言不融合。
- [fuse-fma.mlir:L235-L246](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/fuse-fma.mlir#L235-L246) —— `test_different_ftz`：一边 `flush_to_zero`、一边没有，FTZ 不一致，同样不融合。
- [fuse-fma.mlir:L299-L312](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/fuse-fma.mlir#L299-L312) —— `test_multiple_uses`：`%4 = mulf` 同时被 `addf` 和 `subf` 引用，违反 `hasOneUse()`，[fuse-fma.mlir:L294-L297](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/fuse-fma.mlir#L294-L297) 断言 `mulf/addf/subf` 全保留、无 `fma`。

**FTZ 正例。**

- [fuse-fma.mlir:L214-L225](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/fuse-fma.mlir#L214-L225) —— `test_ftz_enabled`：两边都带 `flush_to_zero`，融合后 `fma` 也应带 `flush_to_zero`，由 [fuse-fma.mlir:L210](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/fuse-fma.mlir#L210) 的 `CHECK: fma ... flush_to_zero` 验证——这正是 `FuseFMA.cpp:L51` 把 `ftz` 转成 `UnitAttr` 透传的体现。

**规范化 + 融合的复合用例。**

- [fuse-fma.mlir:L323-L335](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/fuse-fma.mlir#L323-L335) —— `test_commutative_add_mul_rhs`：输入是 `addf(z, a*b)`（乘法在右），期望仍被融合，证明 `AddFOp` 规范化先行生效。
- [fuse-fma.mlir:L412-L431](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/fuse-fma.mlir#L412-L431) —— `test_chained_commutative`：两条 `addf` 链最终变成 `fma → fma(用上一条 fma 做累加器)`，由 [fuse-fma.mlir:L407-L408](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/fuse-fma.mlir#L407-L408) 的两条 `CHECK: fma`（第二条引用 `%[[FMA1]]`）验证。

#### 4.4.4 代码实践

1. **实践目标：** 把整个 `fuse-fma.mlir` 跑一遍，统计通过用例数，并确认能复现某一反例。
2. **操作步骤：** 直接用 lit 跑该测试：
   ```bash
   cmake --build build --target check-cuda-tile
   # 或单独跑：
   build/bin/cuda-tile-opt test/Transforms/fuse-fma.mlir \
     --pass-pipeline='builtin.module(cuda_tile.module(cuda_tile.testing$func(fuse-fma)))' \
     --split-input-file
   ```
3. **需要观察的现象：** 单独跑时，每个 `// -----` 段会输出对应的融合后 IR；带 `FileCheck` 时全部通过。
4. **预期结果：** 所有段都符合各自 `CHECK`/`CHECK-NOT` 断言；`check-cuda-tile` 汇总里这部分为 PASS。
5. **若无法运行：** 待本地验证；可改为静态对照——挑 `test_mismatch_both`（[fuse-fma.mlir:L277-L288](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/fuse-fma.mlir#L277-L288)，舍入与 FTZ 同时不一致）逐条说明它命中了 `FuseFMA.cpp:L44` 的哪个比较而放弃。

#### 4.4.5 小练习与答案

**练习 1：** `CHECK-NOT: fma` 紧跟在 `CHECK: addf` 之后，它的作用范围是整个函数还是仅到下一个 `CHECK`/`CHECK-LABEL`？

**参考答案：** 仅到下一个 `CHECK`（或 `CHECK-LABEL`）之前那一段区间。FileCheck 的 `CHECK-NOT` 限定在「上一个匹配点」与「下一个匹配点」之间，因此用 `CHECK-LABEL` 按函数切分输出、再在每个函数段内用 `CHECK-NOT`，才能精确约束「这个函数里不出现 fma」。这也是测试里每个用例都先 `CHECK-LABEL: testing$func @名字` 的原因。

**练习 2：** 如果把 `FuseFMA.cpp:L107` 的 `AddFOp::getCanonicalizationPatterns` 注释掉，`fuse-fma.mlir` 里哪些用例会从「通过」变「失败」？

**参考答案：** 所有依赖「乘法在右操作数」先被重排再融合的用例会失败，典型是 `test_commutative_add_mul_rhs`（[fuse-fma.mlir:L316-L335](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/fuse-fma.mlir#L316-L335)）、`test_commutative_add_bcast_mul_rhs`、`test_chained_commutative`——它们期望 `CHECK: fma` 但实际会保留 `addf`。这反证了规范化与融合必须同置。

---

## 5. 综合实践

把本讲的三条主线——注册框架、重写规则、测试——串成一个完整小任务。

**任务：** 为 `FuseFMA` 补一个「负负得正」边界用例，并解释它命中哪条规则。

1. 在 `fuse-fma.mlir` 末尾仿照现有用例，新增一个 `// -----` 段，函数名 `@test_mul_sub_with_const`，IR 为：
   ```mlir
   cuda_tile.module @test {
     cuda_tile.testing$func @test_mul_sub_with_const() -> !cuda_tile.tile<f32> {
       %0 = constant <f32: 2.0> : !cuda_tile.tile<f32>
       %1 = constant <f32: 3.0> : !cuda_tile.tile<f32>
       %2 = constant <f32: 4.0> : !cuda_tile.tile<f32>
       %3 = cuda_tile.mulf %0, %1 rounding<nearest_even> : !cuda_tile.tile<f32>
       %4 = cuda_tile.subf %3, %2 rounding<nearest_even> : !cuda_tile.tile<f32>
       return %4 : !cuda_tile.tile<f32>
     }
   }
   ```
2. 写出你预期的 `CHECK`（提示：应命中 `MulSubPattern`，出现 `fma`，且因 `%2` 是常量、`createOrFold<NegFOp>` 会把它 fold 成 `-4.0`，可能**不**出现 `negf`——但若你不确定 fold 是否生效，可先只写 `CHECK: fma` + `CHECK-NOT: subf`，跑一次再决定是否加 `CHECK-NOT: negf`）。
3. 用 4.4.4 的命令单独跑这一段，比对预期。
4. **解释：** 在你的实验报告里写出——此用例命中 [FuseFMA.cpp:L57-L92](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/FuseFMA.cpp#L57-L92) 的 `MulSubPattern`，`c` 由 [FuseFMA.cpp:L69](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/FuseFMA.cpp#L69) 的 `createOrFold<NegFOp>` 产生；同时点名它满足三个准入条件：乘法在左、单一使用、舍入与 FTZ 一致。
5. **若无法运行：** 待本地验证。退化方案：只做「源码阅读型实践」，对照 `MulSubPattern` 与 [fuse-fma.mlir:L123-L134](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/fuse-fma.mlir#L123-L134) 的 `test_mul_sub_fusion`，预测输出。

## 6. 本讲小结

- CUDA Tile 的变换 Pass 走标准 MLIR 套路：`Passes.td` 声明 → TableGen 生成 `Passes.h.inc` → `GEN_PASS_REGISTRATION` 生成 `registerCudaTilePasses` → 工具（`cuda-tile-opt`）调起。
- `FuseFMA` 用 `InterfacePass<FunctionOpInterface>`，因而能作用于 `entry` 与 `testing$func` 等任何「函数式」操作；这与绑定具体 `ModuleOp` 的 `SynthesizeDebugInfoScopesPass` 形成对照。
- 两条规则 `MulAddPattern`/`MulSubPattern` 的准入条件统一为：乘法单一使用、舍入模式一致、FTZ 一致；`MulSub` 额外用 `createOrFold<NegFOp>` 把减数取负。
- `FuseFMA` 是**非数值保持**的：它把「双轮舍入（mulf+addf）」改成「单轮舍入（fma）」，结果位级会变，因此被做成需显式开启的 Pass，而非默认规范化。
- `AddFOp` 的规范化（把乘法搬到左操作数）与融合 pattern 放进同一 pattern set、同一贪心驱动器，使交换律形态 `addf(z, a*b)` 也能被融合，且链式融合在一轮内收敛。
- `fuse-fma.mlir` 用 `CHECK`/`CHECK-NOT`/`CHECK-LABEL` 钉死了每条准入条件的正反例，是 `FuseFMA` 最权威的行为规格表。

## 7. 下一步学习建议

- **u9-l2 LoopSplit：** 同样基于 `Passes.td` 注册，但变换对象是控制流（`for`/`if`），依赖 u5-l4 的循环与条件结构。学完后可对比「数据流 Pass（FuseFMA）」与「控制流 Pass（LoopSplit）」在模式写法上的差异。
- **u9-l3 cuda-tile-optimize：** 把 `FuseFMA` 等若干 Pass 组合成默认优化管线（`canonicalize`/`CSE`/`loop_split`），可回头印证本讲关于「非数值保持 Pass 如何被显式纳入管线」的讨论。
- **u9-l4 调试信息合成与规范化：** 进一步讲 `SynthesizeDebugInfoScopesPass` 与 `OpsCanonicalization.td` 驱动的逐操作规范化，补全对 `Transforms` 目录的全景认识。
- **延伸阅读：** 直接对照 MLIR 上游的 `mlir/Transforms/GreedyPatternRewriteDriver.h` 与 `mlir/Pass/Pass.h`，理解 `applyPatternsGreedily` 的不动点迭代与 `InterfacePass` 的调度细节。
