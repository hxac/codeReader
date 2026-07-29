# map_elementwise 预处理

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `tt.map_elementwise` 这个 triton 方言操作的结构：它把一个「标量子区（scalar region）」映射到整张张量上。
- 解释为什么 `convert-triton-to-cuda-tile` 主转换 pass 在做方言转换**之前**，必须先把 `map_elementwise` 预处理（展开）掉。
- 描述 `expandMapElementwiseOps` 把标量算子「提升（lifting）」为张量算子的整体思路，包括常量 splat、标量广播、`scf.if → arith.select`、以及 `pack > 1` 的拆分与重组。
- 阅读真实源码 `MapElementwiseExpansion.cpp/.h` 并定位每个最小模块对应的实现。

本讲只读源码、不修改源码，承接 [u3-l1](u3-l1-pass-plugin-skeleton.md)（转换 pass 骨架与 `CudaTileConversionTarget` 的合法性划分）。核心结论先行：`map_elementwise` 是一个「容器型」triton 算子，它的 region 体里装的是**标量**算子；而下游的 `cuda_tile` 方言只认张量算子。所以本 pass 的任务就是「把标量算子集体提升成张量算子，再把容器本身擦除」。

## 2. 前置知识

### 2.1 标量 op 与张量 op 的区别

在 MLIR 里，一个算子（op）操作的是「值（Value）」。值可以有不同的类型：

- **标量类型**：如 `i32`、`f32`，表示一个单独的数。
- **张量类型**：如 `tensor<256xf32>`，表示 256 个 `f32` 组成的张量。

`arith.addf %a, %b : f32` 是标量加法，输入输出都是单个 `f32`。而 `tt` 方言里大量算子是张量级的，如把两个 `tensor<256xf32>` 逐元素相加。这两类算子在 IR 里**不能直接混用**——它们的操作数类型不一致。

### 2.2 region（区域）与块（block）

MLIR 的某些 op 内部可以嵌套一段 IR，称为 **region**。region 里至少有一个 **block（基本块）**，block 以一个 terminator op（终止符，如 `return`/`yield`）结尾。`tt.map_elementwise` 就带有一个 region，里面装的是「对单个元素要做的计算」。

### 2.3 if-conversion（条件转选择）

标量控制流 `scf.if %cond -> T { then } else { else }` 会「二选一」地执行其中一个分支。如果两个分支都没有副作用（pure），就可以把它改写成无分支的 `arith.select %cond, %thenVal, %elseVal`：两个分支都执行，再用条件挑出结果。这叫 **if-conversion**（if 转换），是把结构化控制流「拍平」成数据流的标准手段。

> 你在 [u3-l4](u3-l4-lift-cf-to-scf.md) 会学到 `lift-tt-cf-to-scf` 把 `cf` 控制流转成 `scf`；本讲处理的是 `scf.if` 出现在 `map_elementwise` region 内部时的进一步拍平。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp) | 本讲主角。实现标量 if-conversion、张量级提升、pack>1 拆分/重组，以及入口 `expandMapElementwiseOps`。 |
| [third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.h](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.h) | 对外声明两个预处理函数：`ifConvertMapElementwiseRegions` 与 `expandMapElementwiseOps`。 |
| [third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp) | 主转换 pass `convert-triton-to-cuda-tile` 的 `runOnOperation`，在方言转换前调用 `expandMapElementwiseOps`。 |
| [include/triton/Dialect/Triton/IR/TritonOps.td](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/include/triton/Dialect/Triton/IR/TritonOps.td) | `tt.map_elementwise` 与 `tt.map_elementwise.return` 的算子定义（TableGen）。 |
| [lib/Dialect/Triton/IR/Ops.cpp](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/lib/Dialect/Triton/IR/Ops.cpp) | `MapElementwiseOp` 的 `verify`/`verifyRegions`，规定了 pack、region 入参个数、禁止 store 等约束。 |

## 4. 核心概念与源码讲解

### 4.1 概念基础：map_elementwise 是什么、为什么要预处理

#### 4.1.1 概念说明

`tt.map_elementwise` 是一个「把标量函数逐元素映射到张量」的算子。它的核心思想是：**把一段只对单个元素（标量）生效的计算，自动广播到整张张量的每个元素上**。

这对应 Python 侧的 `tl.map_elementwise(scalar_fn, *args, pack=...)`。它的典型用途是允许对单个元素做控制流分支——比如一个 SELU 激活函数，正负分支代价不同，用 `if` 可以只算一侧，而 `tl.where` 则被迫两侧都算。源码 docstring 给了这个例子：

[python/triton/language/core.py:3184-3193](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/language/core.py#L3184-L3193) —— `selu_scalar` 用 `if x > 0` 分支，再由 `tl.map_elementwise(selu_scalar, x, alpha)` 映射到张量。

算子定义在 TableGen 里：

[include/triton/Dialect/Triton/IR/TritonOps.td:790-806](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/include/triton/Dialect/Triton/IR/TritonOps.td#L790-L806) —— 这段定义了 `map_elementwise`（带 `srcs` 张量、`pack` 属性、`scalarOp` region）与它的终止符 `map_elementwise.return`。

它打印成 IR 大致长这样（示例，结构来自真实测试 `test/Triton/invalid.mlir`）：

```mlir
// 示例代码：map_elementwise 的 IR 结构
"tt.map_elementwise" (%cst) <{pack = 1 : i32}> ({
^bb0(%arg0: i32):                       // 标量 block 参数，类型是张量的元素类型 i32
  tt.map_elementwise.return %arg0 : i32 // 标量返回
}) : (tensor<256xi32>) -> (tensor<256xi32>)
```

关键点：**region 内部的 `^bb0(%arg0: i32)` 是标量**，而外层算子的操作数 `%cst : tensor<256xi32>` 是张量。这两层「标量体内 / 张量体外」是本 pass 要解决的核心矛盾。

#### 4.1.2 核心流程

为什么要预处理？因为下游的 `cuda_tile` 方言**只接受张量算子**，而 `map_elementwise` 容器本身没有对应的 lowering 模式。流程是：

1. 主转换 pass `convert-triton-to-cuda-tile` 启动，先插入 `cuda_tile.module` 容器。
2. **预处理**：调用 `expandMapElementwiseOps`，把所有 `map_elementwise` 展开成普通的张量级 `arith`/`math`/`tt` 算子，并擦除容器。
3. **方言转换**：用 `applyFullConversion` + `CudaTileConversionTarget` 把所有 `triton` 方言算子 lowering 成 `cuda_tile`。此时已经没有 `map_elementwise`，展开后的张量算子被通用的 `ConvertGenericOp` 模式正常处理。

步骤 2 必须在步骤 3 之前，注释明确说明：

[third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp:2829-2833](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp#L2829-L2833) —— 注释写明：预处理必须先跑，展开出的 arith/math 算子才能被 `ConvertGenericOp` 模式拾取。

#### 4.1.3 源码精读

region 的合法性由 verifier 保证。`verifyRegions` 规定了 block 参数个数 = 操作数 × pack，并禁止 region 内出现 store：

[lib/Dialect/Triton/IR/Ops.cpp:706-736](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/lib/Dialect/Triton/IR/Ops.cpp#L706-L736) —— 检查 region 入参数与类型、禁止带写副作用的 op（注释说明：「因为我们把它当标量处理，无法正确处理冗余掩码的 store」）。

这里出现一个关键工具函数 `repeatInterleave`，它决定了 pack>1 时 block 参数的排列：

[lib/Dialect/Triton/IR/Ops.cpp:696-704](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/lib/Dialect/Triton/IR/Ops.cpp#L696-L704) —— `repeatInterleave([src0, src1], K)` 得到 `[src0_e0..src0_eK-1, src1_e0..src1_eK-1]`，即每个输入连续重复 K 次。这个排列在 4.4 节 pack>1 处理里会被原样复用。

#### 4.1.4 代码实践

**实践目标**：直观理解 `map_elementwise` 的「标量体内 / 张量体外」双层结构。

**操作步骤**：

1. 打开 [python/triton/language/core.py:3163-3237](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/language/core.py#L3163-L3237)，阅读 `map_elementwise` 的实现。
2. 关注它如何用 `block.add_argument_at` 给 region 添加**标量**参数（类型取自 `t.type.scalar`，即张量的元素类型），再调用 `scalar_fn` 得到标量结果。

**需要观察的现象**：

- block 参数类型是标量（如 `f32`），而返回给外层算子的结果类型是张量（如 `tensor<256xf32>`）。
- `scalar_fn` 完全不知道张量的存在，它只写「对一个数做什么」。

**预期结果**：你能用自己的话说出——`map_elementwise` 让用户写标量逻辑，而把「逐元素广播」这件事交给编译器。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `map_elementwise` 的 region 里禁止 `tt.store`？

> **答案**：见 [Ops.cpp:722-727](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/lib/Dialect/Triton/IR/Ops.cpp#L722-L727)。因为编译器把 region 体当标量逐元素处理，无法保证 store 的掩码（mask）正确性；禁止 store 保证 region 是纯计算、可安全拍平的。

**练习 2**：`pack` 属性在 verifier 里有什么约束？

> **答案**：必须是 2 的幂（[Ops.cpp:690-692](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/lib/Dialect/Triton/IR/Ops.cpp#L690-L692)）。这为 4.4 节的二进制拆分铺路。

---

### 4.2 最小模块一：if 转 select（scf.if → arith.select）

#### 4.2.1 概念说明

当 `scalar_fn` 里含有 `if`（在 IR 里是 `scf.if`），`map_elementwise` 的 region 体内就出现了控制流。提升为张量算子时，标量的 `scf.if` 不能直接搬到张量世界——因为张量是「批量」的，不存在「整张张量一起走 then 分支」的概念，而是「每个元素各自选 then 或 else」。

解决办法是 **if-conversion**：把 `scf.if` 转成 `arith.select`（逐元素选择）。条件 `%cond` 也被提升成张量，`arith.select %condTensor, %thenTensor, %elseTensor` 对每个元素独立挑选。这要求两个分支都是 pure（无副作用），而 verifier 已经禁止了 store，分支里基本只剩算术，满足前提。

#### 4.2.2 核心流程

源码里有两处实现 if-conversion，分工不同：

- **张量级（运行期实际路径）**：在 `liftBodyOp` 处理 `scf::IfOp` 时，递归提升 then/else 两个子 region 到张量，再为每个结果发一条 `arith.select`。这是 `expandMapElementwiseOps` 内联完成的。
- **标量级（独立工具）**：`scalarIfConvert`（被公共函数 `ifConvertMapElementwiseRegions` 调用）在标量层面把 then/else 的算子搬到 `scf.if` 之前，再用 `arith.select` 替换。**注意：当前管线只调用 `expandMapElementwiseOps`，并未调用 `ifConvertMapElementwiseRegions`**（见 4.2.3 的调用点确认），所以实际生效的是张量级路径。

张量级 if-conversion 的流程：

```
遇到 scf.if (标量条件 %cond, 标量 then/else):
  1. 把 %cond 提升为张量 condTensor（标量→tt.splat，见 4.3）
  2. 复制 mapping，递归 lift then region → thenVals（张量列表）
  3. 复制 mapping，递归 lift else region → elseVals（张量列表）
  4. 对每个结果 i，发一条 arith.select condTensor, thenVals[i], elseVals[i]
  5. 把原 scf.if 的结果映射到 select 结果
```

#### 4.2.3 源码精读

张量级 if-conversion 在 `liftBodyOp` 的第二个分支：

[third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp:107-135](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp#L107-L135) —— 处理 `scf::IfOp`：先 `liftToTensor` 提升条件，再各自复制一份 `IRMapping` 递归提升 then/else region（`liftRegionBody`），最后对每个结果 `llvm::enumerate` 发 `arith::SelectOp`。复制 mapping 是为了隔离两个分支的中间映射，避免互相污染。

注意第 117-118、125-126 行的失败检查：若某个分支提升后 `thenVals.empty()` 却又本应有结果，则 `failure()`——这是防御性编程。

标量级 `scalarIfConvert` 的实现（公共 API `ifConvertMapElementwiseRegions` 的底层）：

[third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp:171-215](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp#L171-L215) —— 它用 `walk` 收集所有 `scf.if`，再 `llvm::reverse` **从内层向外层**处理（注释 line 180 解释：walk 先访问外层，reverse 保证处理外层时内层已经拍平成纯算术）。把 then/else 的算子 `moveBefore(ifOp)`（因为 pure，可安全前移 speculate），再发 `arith.select` 并 `replaceAllUsesWith` + `erase` 掉 `scf.if`。

公共声明与调用现状：

[third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.h:20-27](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.h#L20-L27) —— 声明了 `ifConvertMapElementwiseRegions` 与 `expandMapElementwiseOps` 两个入口。但在 `TritonToTileIRPass.cpp` 的 `runOnOperation` 里只调用了 `expandMapElementwiseOps`（[TritonToTileIRPass.cpp:2832](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp#L2832)），没有调用 `ifConvertMapElementwiseRegions`。因此 `ifConvertMapElementwiseRegions`/`scalarIfConvert` 目前是「已实现但管线未启用」的标量级工具，实际 if-conversion 由 `liftBodyOp` 在张量级完成。

#### 4.2.4 代码实践

**实践目标**：手工模拟一段 `scf.if` 在 `map_elementwise` 内被拍平成 `arith.select` 的过程。

**操作步骤**：

1. 想象一段 pack=1 的 `map_elementwise`，region 内有（示例代码，基于 selu_scalar 语义构造）：

   ```mlir
   // 示例代码：转换前（标量级 scf.if 在 region 内）
   ^bb0(%x: f32, %alpha: f32):
     %cond = arith.cmpf ogt, %x, %c0 : f32
     %e = math.exp %x : f32
     %neg = arith.select %cond, %x, %alpha : f32   // 标量 select，仅示意分支值来源
     %res = scf.if %cond -> f32 {
       scf.yield %x : f32
     } else {
       %m = arith.subf %e, %c1 : f32
       %r = arith.mulf %alpha, %m : f32
       scf.yield %r : f32
     }
     tt.map_elementwise.return %res : f32
   ```

2. 跟踪 [MapElementwiseExpansion.cpp:107-135](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp#L107-L135)：条件 `%cond` 提升为张量；then 分支 yield `%x` 提升为张量；else 分支的 `subf`/`mulf` 被提升为张量算子，yield 值提升为张量；最后发 `arith.select`。

**需要观察的现象**：转换后 region 体里不再有 `scf.if`，只剩张量级的 `arith.cmpf`、`math.exp`、`arith.subf`、`arith.mulf` 和一条 `arith.select`。

**预期结果**（示例代码）：

```mlir
// 示例代码：转换后（全部张量化，scf.if 消失）
%xT   = <x 的张量>      // 来自 block arg 映射
%c0T  = arith.constant dense<0.0> : tensor<256xf32>
%condT = arith.cmpf ogt, %xT, %c0T : tensor<256xf32>
%eT   = math.exp %xT : tensor<256xf32>
%thenT = <x 的张量>
%c1T  = arith.constant dense<1.0> : tensor<256xf32>
%mT   = arith.subf %eT, %c1T : tensor<256xf32>
%rT   = arith.mulf %alphaT, %mT : tensor<256xf32>
%resT = arith.select %condT, %thenT, %rT : tensor<256xf32>
```

#### 4.2.5 小练习与答案

**练习 1**：`scalarIfConvert` 为什么要 `llvm::reverse(ifOps)`？

> **答案**：[MapElementwiseExpansion.cpp:180-181](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp#L180-L181) 注释：`walk` 先访问外层 `scf.if` 再访问内层；reverse 后先处理内层，保证处理外层时内层体已是纯算术、可安全 `moveBefore`。

**练习 2**：`liftBodyOp` 处理 `scf.if` 时，为什么要为 then/else 各复制一份 `IRMapping`？

> **答案**：[MapElementwiseExpansion.cpp:113-121](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp#L113-L121)。两个分支各自提升时会产生各自的中间张量映射，复制 mapping 使两分支互不干扰，最后只把 `scf.if` 结果（而非分支内部中间值）映射回外层。

---

### 4.3 最小模块二：张量级提升（标量算子 → 张量算子）

#### 4.3.1 概念说明

「提升（lifting / tensor lifting）」是本 pass 的核心动作：把 region 体内每一个标量算子，改写成操作**同形状张量**的对应算子。提升后，标量世界整段平移到张量世界，原 `map_elementwise` 容器就「空」了，可以擦除。

提升要处理三类东西：

1. **标量常量**：`arith.constant 1.0 : f32` → `arith.constant dense<1.0> : tensor<Nxf32>`（splat 密集属性）。
2. **来自外部的标量值**：region 内可能引用了 region 外 hoist 出来的标量（如 block 参数之外的东西），用 `tt.splat` 把它广播到目标张量形状。
3. **普通标量算子**：`arith.addf`（标量）→ `arith.addf`（张量）：操作数换张量、结果类型换张量，算子本身不变。

#### 4.3.2 核心流程

整个提升由一个 `IRMapping`（旧值→新值映射）驱动，递归遍历 region body：

```
liftRegionBody(region):
  for 每个非终止符 op in body:
    liftBodyOp(op)            # 把 op 改写/映射
  收集 terminator 的 yield/return 操作数，逐个 liftToTensor
  返回提升后的张量结果列表
```

`liftBodyOp` 分三类（[MapElementwiseExpansion.cpp:87-162](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp#L87-L162)）：

| 标量 op | 提升方式 |
| --- | --- |
| `arith::ConstantOp` | 用 `DenseElementsAttr::get(tensorType, scalarAttr)` 做张量 splat 常量 |
| `scf::IfOp` | 递归提升两分支 + `arith.select`（见 4.2） |
| 其它通用 op | 操作数逐个 `liftToTensor`，结果类型改张量，重建同名 op |

`liftToTensor` 是「标量→张量」的归一入口：若值已是张量则原样返回，否则插一条 `tt.splat` 广播。

#### 4.3.3 源码精读

`liftToTensor`：标量到张量的广播：

[third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp:49-60](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp#L49-L60) —— 先 `mapping.lookupOrDefault` 取已映射值；若类型已是 `RankedTensorType` 直接返回；否则构造目标张量类型并插 `triton::SplatOp`，写回 mapping。

`liftBodyOp` 的三类处理：

- **常量（case 1）**：[MapElementwiseExpansion.cpp:96-104](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp#L96-L104) —— 把标量属性包成 `DenseElementsAttr` splat，新建张量常量。
- **通用算子（case 3）**：[MapElementwiseExpansion.cpp:137-161](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp#L137-L161) —— 用 `OperationState` 拷贝原 op 的名字、属性，操作数全部 `liftToTensor`，结果类型全部改成 `RankedTensorType::get(shape, elemType, encoding)`，`builder.insert` 新 op，再建立旧结果→新结果的映射。

`liftRegionBody`：遍历 body 并收集终止符操作数：

[third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp:64-80](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp#L64-L80) —— 断言单 block，遍历 `without_terminator()` 逐个 lift，最后把 `scf::YieldOp` 的操作数 `liftToTensor` 作为返回值（用于 4.2 的 if 分支提升）。

#### 4.3.4 代码实践

**实践目标**：理解「通用算子」提升时类型如何变化。

**操作步骤**：

1. 读 [MapElementwiseExpansion.cpp:144-161](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp#L144-L161)，关注 `newResultTypes` 如何由 `RankedTensorType::get(shape, result.getType(), encoding)` 构造。
2. 设想一个标量 `arith.mulf %a, %b : f32` 出现在 shape=`[256]`、encoding=`E` 的 region 里。

**需要观察的现象**：新 op 的操作数都成了 `tensor<256xf32, E>`，结果也成了 `tensor<256xf32, E>`，但 op 的**名字和属性**（如 `arith.mulf`、fastmath 属性）原样保留。

**预期结果**：`arith.mulf %aT, %bT : tensor<256xf32>`，其中 `%aT/%bT` 是经 `liftToTensor` 提升后的张量。该结果随后会被主转换 pass 的通用 lowering 模式处理成 `cuda_tile` 算子。

#### 4.3.5 小练习与答案

**练习 1**：常量提升为什么用 `DenseElementsAttr` 而不是对每个元素发一条常量 op？

> **答案**：`DenseElementsAttr` 是 MLIR 里「整个张量的密集常量属性」，splat 形式只存一个标量值却表示整张同值张量，紧凑且能被后续 pass 识别折叠；逐元素发常量会产生海量 IR，既慢又破坏可读性。见 [MapElementwiseExpansion.cpp:100-101](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp#L100-L101)。

**练习 2**：`liftToTensor` 何时会真正插入 `tt.splat`？

> **答案**：当被提升的值**还不是张量**时（如 region 外 hoist 进来的标量、或尚未提升的 block 参数）。已是 `RankedTensorType` 则直接返回。见 [MapElementwiseExpansion.cpp:52-58](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp#L52-L58)。

---

### 4.4 最小模块三：擦除 map_elementwise（含 pack>1 拆分与重组）

#### 4.4.1 概念说明

提升完成后，`map_elementwise` 容器的 region 内容已被「平移」到容器之外的张量算子，容器本身不再做任何事，于是 `replaceAllUsesWith(results)` + `erase()` 把它擦除，IR 里只剩标准张量算子。

`pack > 1` 是更复杂的情况。`pack` 表示「一次函数调用处理 K 个元素」。此时 region 的 block 参数是 `repeatInterleave([src 类型], K)`——每个输入贡献 K 个标量参数（对应 K 个相邻元素）。提升策略是：把每张输入张量 `[.., N]` 拆成 K 个子张量 `[.., N/K]`，分别提升，再把 K 个结果子张量重组回 `[.., N]`。这要求末维 N 能被 K 整除，且 K 是 2 的幂（用二进制 split/join 实现）。

#### 4.4.2 核心流程

`expandMapElementwiseOpsImpl` 对每个 `map_elementwise` op 处理。先做两个前置校验：

- region 必须单 block（否则报错提示 `lift-tt-cf-to-scf` 应先跑）；
- `pack` 必须是 2 的幂。

**pack == 1（直接提升）**：

```
1. 建 IRMapping：block 参数 i ↔ 输入张量 srcs[i]
2. 遍历 body op 逐个 liftBodyOp（用原始张量 shape/encoding）
3. 收集 map_elementwise.return 的操作数，逐个 liftToTensor
4. op.replaceAllUsesWith(results); op.erase()
```

**pack > 1（拆分—提升—重组）**：

```
对每个输入 src ([.., N]):
  1. reshape 成 [.., N/K, K]
  2. recursiveSplit 成 K 个子张量 [.., N/K]
把 block 参数按 repeatInterleave 顺序映射到子张量
在子张量 shape [.., N/K] 上 liftBodyOp 提升所有算子
对每个输出：收集 K 个子结果 → recursiveJoin 成 [.., N/K, K] → reshape 回 [.., N]
op.replaceAllUsesWith(results); op.erase()
```

二进制 split 的深度为 \(\lceil \log_2 K \rceil\) 层，每层做一次 `tt.split`（一分为二）；join 是其逆过程，用 `tt.join`（合二为一）。

#### 4.4.3 源码精读

入口与校验：

[third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp:340-359](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp#L340-L359) —— `walk` 收集所有 `MapElementwiseOp`；对每个 op 检查单 block（否则 emitError 提示 `lift-tt-cf-to-scf` 先跑）与 pack 为 2 的幂。

pack == 1 的直接提升与擦除：

[third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp:370-390](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp#L370-L390) —— 把 block 参数映射到 `op.getSrcs()`，遍历 body 提升，收集 `MapElementwiseReturnOp` 操作数提升为结果，最后 `replaceAllUsesWith` + `erase`。这正是「擦除 map_elementwise」。

pack > 1 的拆分（recursiveSplit）：

[third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp:227-274](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp#L227-L274) —— K=1 时 reshape 丢掉末维；K=2 时一次 `tt.split` 得左右两半；K>2 时 reshape 末维成 `[K/2, 2]`，split 后递归，再 **interleave**（`[lhs0, rhs0, lhs1, rhs1, ...]`）保持元素顺序。

pack > 1 的重组（recursiveJoin）：

[third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp:280-328](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp#L280-L328) —— recursiveSplit 的逆：de-interleave 成奇偶两半，递归 join，再 reshape 折叠末两维。K=1 加回末维 1；K=2 一次 `tt.join`。

pack > 1 的主流程（reshape→split→映射→提升→join→reshape）：

[third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp:391-480](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp#L391-L480) —— 注释 line 393-397 明确写出 block 参数与返回值的 `repeatInterleave` 排列；末维被 `pack` 整除的校验在 line 400-403；映射按 `[src0_e0..eK-1, src1_e0..]` 顺序（line 438-443）；每个输出收集 K 个子结果 join 后 reshape 回原 shape（line 458-476），最后同样 `replaceAllUsesWith` + `erase`。

公共入口：

[third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp:501-503](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp#L501-L503) —— `expandMapElementwiseOps` 只是 `expandMapElementwiseOpsImpl` 的薄壳，供主转换 pass 调用。

#### 4.4.4 代码实践

**实践目标**：解释为何要在转换 pass 之前预处理 `map_elementwise`，并说明 `expandMapElementwiseOps` 的整体思路（对应本讲总实践任务）。

**操作步骤**：

1. 打开 [TritonToTileIRPass.cpp:2829-2849](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp#L2829-L2849)，对比「预处理（2832 行）」与「方言转换（2836-2848 行）」的先后与关系。
2. 回答两个问题（见「预期结果」），用源码行号佐证。

**需要观察的现象**：

- 若把 2832 行的预处理删掉，`applyFullConversion` 会在遇到 `map_elementwise` 时因「无合法 lowering 模式 + triton 方言非法」而失败（参考 [u3-l1](u3-l1-pass-plugin-skeleton.md) 的 `CudaTileConversionTarget` 合法性划分）。
- 预处理后 `map_elementwise` 消失，2836 行的 `CudaTileConversionTarget` 与 `populateTTirToCudaTileConversionPatternsAndLegality` 只需面对标准张量算子。

**预期结果**（用自己的话写出）：

- **为何预处理**：`cuda_tile` 方言只接受张量算子，`CudaTileConversionTarget` 把 `triton` 方言（含 `map_elementwise`）判为非法，且没有为 `map_elementwise` 写专门的 lowering 模式；必须在 full conversion 前把它展开成通用张量算子，否则转换失败。注释 [TritonToTileIRPass.cpp:2829-2831](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp#L2829-L2831) 正是此意。
- **整体思路**：`expandMapElementwiseOps` 遍历所有 `map_elementwise`；对每个 op，用 `IRMapping` 把标量 region 体「提升」为同形状张量算子（常量 splat、标量 `tt.splat` 广播、`scf.if` 转 `arith.select`、通用算子换张量类型）；`pack>1` 时先 `tt.split` 拆成 K 个子张量、在子张量上提升、再用 `tt.join` 重组；最后 `replaceAllUsesWith` + `erase` 擦除容器。

> 说明：本仓库的 `third_party/tileir/test/FileCheck/` 目录下**没有**专门针对 `map_elementwise` 展开的 lit 测试（可自行 `ls` 确认）。若想验证行为，可在本地构建 `triton-cuda-tile-opt`（见 [u4-l1](u4-l1-opt-tool-and-lit-tests.md)），构造一段含 `tt.map_elementwise` 的 `.mlir`，运行 `convert-triton-to-cuda-tile` 观察 `map_elementwise` 是否被展开——**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`expandMapElementwiseOpsImpl` 在 region 非单 block 时报什么错、为什么？

> **答案**：[MapElementwiseExpansion.cpp:349-353](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp#L349-L353) emitError「region has multiple blocks; expected lift-tt-cf-to-scf to have run first」。因为提升逻辑假设单 block 顺序遍历，多 block（一般来自 `cf` 控制流）需先由 `lift-tt-cf-to-scf` 结构化成 `scf`、再坍缩成单 block。

**练习 2**：`recursiveSplit` 在 K>2 时为什么递归后要做 interleave？

> **答案**：[MapElementwiseExpansion.cpp:267-273](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp#L267-L273)。二进制 split 把 `[K/2,2]` 拆成左右两半各 `K/2`，递归后两半内部已是正确顺序，但「左半全部 + 右半全部」会打乱元素交错关系，故按 `[lhs0,rhs0,lhs1,rhs1,...]` 交错排列，保持与原 K 个相邻元素的顺序一致，使后续 join 能正确还原。

## 5. 综合实践

把三个最小模块串起来，完成一次完整的「源码追踪 + 行为推演」：

**任务**：给定一个 pack=2、含 `scf.if` 的 `tt.map_elementwise`（输入 `tensor<8xf32>`），画出它经过 `expandMapElementwiseOps` 后的 IR 形态与数据流。

**步骤**：

1. 写出输入 IR（示例代码）：两个输入 `%a, %b : tensor<8xf32>`，`pack=2`，region 有 4 个标量参数 `(%a0,%a1,%b0,%b1)`（按 `repeatInterleave`），体内含一个 `scf.if` 比较 `%a0` 与 `%b0`。
2. 跟踪 [MapElementwiseExpansion.cpp:391-480](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp#L391-L480)：
   - 每个 `%a/%b` reshape 成 `tensor<4x2xf32>`，`recursiveSplit` 拆成两个 `tensor<4xf32>` 子张量。
   - block 参数映射：`%a0↔a_sub0, %a1↔a_sub1, %b0↔b_sub0, %b1↔b_sub1`。
   - 在 `tensor<4xf32>` 上提升体内算子；`scf.if` 经 [liftBodyOp](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp#L107-L135) 变成张量 `arith.select`。
   - 收集 K=2 个子结果，`recursiveJoin` 合成 `tensor<4x2xf32>`，reshape 回 `tensor<8xf32>`。
3. 核对最终 IR：**不应再有 `tt.map_elementwise` 与 `scf.if`**，只剩 `tt.reshape`/`tt.split`/`tt.join`/张量 `arith` 算子。

**验收标准**：

- 能指出三个最小模块分别在哪些行实现（if→select：107-135；张量提升：49-104、137-161；擦除与 pack 拆分：370-480）。
- 能解释「为何预处理」（见 4.4.4 预期结果）。
- 明确标注哪些 IR 是「示例代码」（本仓库无现成 lit 用例，结果待本地验证）。

## 6. 本讲小结

- `tt.map_elementwise` 是「标量子区映射到张量」的容器型 triton 算子，region 体操作**标量**，外层操作**张量**。
- 它必须在主转换 pass `convert-triton-to-cuda-tile` 做 `applyFullConversion` **之前**被展开，因为 `cuda_tile` 方言只认张量算子、且没有为它写 lowering 模式（[TritonToTileIRPass.cpp:2829-2833](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp#L2829-L2833)）。
- **if 转 select**：region 内 `scf.if` 被拍平为张量 `arith.select`（张量级路径在 `liftBodyOp`，标量级 `scalarIfConvert` 已实现但管线未启用）。
- **张量级提升**：标量常量→`DenseElementsAttr` splat，外部标量→`tt.splat` 广播，通用算子→换张量类型重建，由 `IRMapping` 驱动。
- **擦除 map_elementwise**：提升后 `replaceAllUsesWith` + `erase`；`pack>1` 时用 `tt.split` 拆 K 个子张量、提升、`tt.join` 重组（K 须为 2 的幂，末维须被 K 整除）。
- 预处理后 IR 只剩标准张量算子，平稳进入后续 `CudaTileConversionTarget` 的 full conversion。

## 7. 下一步学习建议

- 继续阅读 [u3-l4 lift-cf-to-scf](u3-l4-lift-cf-to-scf.md)：理解 `lift-tt-cf-to-scf` 如何把 `cf` 控制流结构化成 `scf`，这正是本讲「region 必须单 block」前置假设的来源（错误信息 [MapElementwiseExpansion.cpp:350-351](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/TritonToTileIR/MapElementwiseExpansion.cpp#L350-L351) 直接点名该 pass）。
- 阅读 [u3-l2 核心转换](u3-l2-core-conversion-pass.md)（讲义待补）：看展开后的张量算子如何被 `ConvertGenericOp` 等 lowering 模式转换成 `cuda_tile`。
- 想本地复现展开效果，参考 [u4-l1 triton-cuda-tile-opt 与 lit 测试](u4-l1-opt-tool-and-lit-tests.md)，自行构造 `map_elementwise` 用例验证。
