# assume 重写 RewriteAssumeWithCudaTile

## 1. 本讲目标

本讲聚焦 TileIR 后端编译流水线（`make_tileir`）中的一道预处理 pass——`rewrite-assume-with-cuda-tile`。学完后你应当能够：

- 说清 `llvm.intr.assume` 这种「断言型」操作在 IR 里的典型长相，以及 Triton 是怎样产生它的。
- 读懂 `RewriteAssumeWithCudaTile.cpp` 里「六步匹配」的识别逻辑，知道它把 `remsi(x, C) == 0` 这个整除模式识别出来。
- 解释为何匹配后要生成 `cuda_tile.assume div_by<C>`，以及为何无匹配时要直接把 `assume` 删掉。
- 能对照测试用例，手写一段「前 / 后」IR，并理解这道 pass 在整条转换链里的位置与存在意义。

本讲是 [u3-l1（转换 Pass 的 C++ 插件入口与骨架）](u3-l1-pass-plugin-skeleton.md) 的直接延续：u3-l1 讲了骨架与 `Passes.td`，本讲则深入其中**一道具体 pass 的实现**。

## 2. 前置知识

在进入源码前，先建立几个概念直觉。

- **`assume` 是「提示」，不是「计算」。** `llvm.intr.assume %cond : i1` 是 LLVM 的不可失败断言：它告诉编译器「`%cond` 在运行时一定为真」。它本身不产生任何机器指令，只是把一个已知事实喂给编译器，供后续优化使用（例如知道一个指针按 16 字节对齐，就可以用更宽的访存指令）。`assume` 的语义是「丢弃安全」——如果编译器忽略它，程序结果不变，只是少优化一些。

- **整除性（divisibility）与对齐如何编码成 `assume`。** 「`x` 能被 `C` 整除」这件事，等价于：

  \[ x \bmod C = 0 \]

  在 IR 里它通常被写成一组标量算子：先取余 `arith.remsi %x, %C`，再比较 `arith.cmpi eq, %rem, %c0`，得到一个 `i1` 条件，最后喂给 `llvm.intr.assume`。本讲要讲的就是「识别这串 idiom 并改写成更原生的形式」。Triton 里 `tl.assume(cond)` 这个 API 就会生成 `assume` 操作（见 [semantic.py:1811-1812](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/language/semantic.py#L1811-L1812) 调用 `builder.create_assume`）。

- **`unrealized_conversion_cast` 是「临时跳板」。** 在 MLIR 方言转换里，当源类型（如 `i32`、`tt.ptr<i32>`）和目标类型（如 `cuda_tile.tile<i32>`）还没建立正式 lowering 时，用一个 `builtin.unrealized_conversion_cast` 临时桥接两边，后续由 `reconcile-unrealized-casts` 等收尾。本讲里你会大量看到它。

- **贪心模式重写（greedy pattern rewrite）。** 一道 pass 可以注册若干 `OpRewritePattern`，由 `applyPatternsGreedily` 在整个模块上反复尝试匹配并改写，直到不动点。本讲的 pass 就只注册了一个针对 `LLVM::AssumeOp` 的 pattern。

- **支配关系（dominance）。** 「操作 A 支配操作 B」意味着从入口到 B 的每条路径都必经过 A。本讲在替换一个值的用途时，会用到支配关系来保证「只有发生在 `assume` 之后的用法，才能用到被断言后的新值」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `third_party/tileir/lib/Transform/RewriteAssumeWithCudaTile.cpp` | 本讲主角：整除/对齐模式的识别与改写逻辑、pattern 与 pass 类 |
| `third_party/tileir/include/Transform/Passes.td` | 用 TableGen 定义这道 pass 的命令行名、依赖方言、文字说明（含前/后 IR 示例） |
| `third_party/tileir/triton_tileir.cc` | 通过 pybind 把 pass 暴露为 Python 侧的 `tileir.passes.add_assume_to_tileir` |
| `third_party/tileir/backend/compiler.py` | `make_tileir` 里调用该 pass，决定它在转换链中的位置 |
| `third_party/tileir/test/FileCheck/op-rewrite-assume.mlir` | 该 pass 的 lit/FileCheck 单元测试，三个典型用例 |

## 4. 核心概念与源码讲解

### 4.1 assume 模式识别

#### 4.1.1 概念说明

`cuda_tile` 方言有自己的 `assume` 操作，但它接收的不是「一个 i1 条件」，而是结构化的提示——例如「某个值能被 `C` 整除」（`div_by<C>`）。而上游 Triton 产出的 TTIR 里，断言以「裸的 `llvm.intr.assume %cond`」形式存在，`%cond` 往往是一串算子表达式。

所以这道 pass 的第一项工作就是**逆向解析**：从 `%cond` 往回追，看它是不是「取余等于零」这个整除 idiom。如果是，就能提炼出一个除数 `C`，从而构造出结构化的 `cuda_tile.assume div_by<C>`。

源码文件顶部注释给出了要识别的两种模式（[RewriteAssumeWithCudaTile.cpp:30-51](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/RewriteAssumeWithCudaTile.cpp#L30-L51)）：

- **整数情形**：`%a : i32` 经过 `remsi %a, C`、`cmpi eq, _, 0` 后喂给 `assume`。
- **指针情形**：先 `tt.ptr_to_int %ptr : !tt.ptr<T> -> i64`，再 `remsi %pi, C`、`cmpi eq, _, 0`、`assume`。指针场景表达的是「这个指针对齐到 `C` 字节」。

#### 4.1.2 核心流程

识别逻辑全部集中在自由函数 `RewriteArithAssumeImpl`（[RewriteAssumeWithCudaTile.cpp:52-174](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/RewriteAssumeWithCudaTile.cpp#L52-L174)）。它走的是一条「从 `assume` 倒推」的六步链，任何一步不满足就 `return failure()`（放弃匹配）：

```text
llvm.intr.assume %cond
        │  Step1: %cond 的定义算子必须是 arith.cmpi，且谓词必须是 eq
        ▼
   arith.cmpi eq, %rem, %zero
        │  Step2: 取左操作数 %rem、右操作数 %zero
        │  Step3: %zero 必须是常量 0（IntegerAttr.isZero()）
        ▼
   %rem = arith.remsi %x, %divisor
        │  Step4: %rem 的定义算子必须是 arith.remsi
        │  Step5: 取左操作数 %x、右操作数 %divisor
        │  Step6: %divisor 必须是整型常量 → 读出除数 C
        ▼
   对 %x 分两种情形：tt.ptr_to_int 的结果 → 指针分支；否则整数分支
```

关键点：除数 `C` 来自 `arith.remsi` 的**右操作数**（必须是常量），而**被断言的值** `%x` 是左操作数（可能是一个标量整数，也可能来自 `tt.ptr_to_int`）。

#### 4.1.3 源码精读

六步匹配的代码：

- Step 1：确认条件来自 `cmpi eq`——[RewriteAssumeWithCudaTile.cpp:57-60](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/RewriteAssumeWithCudaTile.cpp#L57-L60)。谓词不是 `eq` 直接放弃（比如 `ne` 表示「不能整除」，语义完全不同）。
- Step 2–3：取左右操作数，并要求右操作数是常量 0——[RewriteAssumeWithCudaTile.cpp:62-74](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/RewriteAssumeWithCudaTile.cpp#L62-L74)。这里用 `IntegerAttr.getValue().isZero()` 判零。
- Step 4–6：左操作数必须来自 `arith.remsi`，且 remsi 的右操作数是整型常量——[RewriteAssumeWithCudaTile.cpp:76-94](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/RewriteAssumeWithCudaTile.cpp#L76-L94)。除数用 `getSExtValue()` 符号扩展成 `int64_t`。

```cpp
auto remOp = remResult.getDefiningOp<arith::RemSIOp>();
if (!remOp)
  return failure();
Value intOrPtrToInt = remOp.getLhs();
Value divisorConstant = remOp.getRhs();
// ... 要求 divisorConstant 是整型常量 ...
int64_t divisor = divisorAttr.getValue().getSExtValue();
```

之后用 `intOrPtrToInt.getDefiningOp<triton::PtrToIntOp>()` 判定走哪条分支——[RewriteAssumeWithCudaTile.cpp:102](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/RewriteAssumeWithCudaTile.cpp#L102)：能 `dyn_cast` 成 `PtrToIntOp` 就是指针情形，否则进入整数情形。

还有一个**附带的小动作**：在第 96–98 行，若 `%x` 有定义算子，就给它打上 `tt.divisibility = C` 属性：

```cpp
auto definingOp = intOrPtrToInt.getDefiningOp();
if (definingOp)
  definingOp->setAttr("tt.divisibility", divisorAttr);
```

这等于把「这个值能被 `C` 整除」的信息回写为 Triton 的 divisibility 提示属性，方便下游转换。

#### 4.1.4 代码实践

**实践目标**：用眼睛「跑」一遍六步匹配，理解它在什么 IR 上成功、什么 IR 上失败。

**操作步骤**：

1. 打开测试文件 [op-rewrite-assume.mlir:7-17](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-rewrite-assume.mlir#L7-L17) 的 `kernel_assume` 用例，找到输入 IR：

   ```mlir
   %c32_i32 = arith.constant 32 : i32
   %c0_i32  = arith.constant 0 : i32
   %0 = arith.remsi %arg0, %c32_i32 : i32
   %1 = arith.cmpi eq, %0, %c0_i32 : i32
   llvm.intr.assume %1 : i1
   ```

2. 对照六步链，在纸上标注：`%cond = %1` → `cmpi eq`（Step1 ✓）→ 右操作数 `%c0_i32 = 0`（Step3 ✓）→ `%rem = %0` 来自 `remsi`（Step4 ✓）→ 除数 `%c32_i32 = 32`（Step6 ✓）→ `%x = %arg0` 是普通整数 → 走**整数分支**。

**需要观察的现象**：除数 `C = 32`，被断言的值是 `%arg0`（类型 `i32`）。

**预期结果**：匹配成功，`C = 32`，进入 4.2 的整数分支生成 `cuda_tile.assume div_by<32>`。

> 是否真正运行该 pass 见第 5 节综合实践；若仅做阅读型实践，到这里即可。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `arith.cmpi eq` 改成 `arith.cmpi ne`（不等于零），这道 pass 还会匹配吗？为什么？

**答案**：不会。`cmpi ne` 表示「余数不为零」，也就是「`x` **不能**被 `C` 整除」，这与整除提示语义相反。Step 1 在 [RewriteAssumeWithCudaTile.cpp:59](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/RewriteAssumeWithCudaTile.cpp#L59) 明确要求谓词必须是 `eq`，故返回 `failure()`。

**练习 2**：如果 `remsi` 的右操作数不是一个常量（而是一个变量），会发生什么？

**答案**：Step 6 失败——[RewriteAssumeWithCudaTile.cpp:86-88](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/RewriteAssumeWithCudaTile.cpp#L86-L88) 要求除数必须是 `arith.constant`，否则无法读出一个具体的 `C` 来构造 `div_by<C>`，于是放弃匹配。

---

### 4.2 cuda_tile.assume 生成

#### 4.2.1 概念说明

一旦识别出整除 idiom，pass 要把「裸的 `llvm.intr.assume`」换成 `cuda_tile` 方言原生的 `assume` 操作。`cuda_tile.assume` 长这样：

```mlir
%r = cuda_tile.assume div_by<32>, %v : tile<i32>
```

它带一个 **`div_by<C>` 属性**（`cuda_tile::DivByAttr`），断言操作数 `%v` 能被 `C` 整除；操作数类型必须是 `cuda_tile` 的 tile 类型（标量情形是 `tile<i32>`，指针情形是 `tile<ptr<i32>>`）。

由于被断言的值原本是 Triton 侧的类型（`i32`、`tt.ptr<i32>`），而 `cuda_tile.assume` 要 tile 类型，所以必须用 `unrealized_conversion_cast` 临时桥接。匹配后的 IR 形如（整数情形，来自源码注释）：

```mlir
%tile_a  = builtin.unrealized_conversion_cast %a : i32 -> tile<i32>
%assume_a = assume div_by<8 : i64>, %tile_a : tile<i32>
%new_a   = builtin.unrealized_conversion_cast %assume_a : tile<i32> -> i32
```

#### 4.2.2 核心流程

改写分整数与指针两个对称的分支，套路一致：

1. **构造 `div_by` 属性**：`cuda_tile::DivByAttr::get(ctx, divisor, std::nullopt, std::nullopt)`。这里后两个参数（`every`、`along`，用于张量级「每 N 个元素/沿某轴」的整除提示）传 `nullopt`，因为本 pass 只处理标量/指针。
2. **源类型 → tile 类型**：插一个 `unrealized_conversion_cast`。
3. **生成 `cuda_tile::AssumeOp`**：把 cast 结果与 `div_by` 属性组装起来。
4. **tile 类型 → 源类型**：再插一个 `unrealized_conversion_cast`，把结果转回原类型，供 Triton 侧的后续用法继续使用。
5. **受限替换**：只把「被 assume 支配的那些用法」替换成新值，保证时序正确。

#### 4.2.3 源码精读

**指针分支**（Case 2，[RewriteAssumeWithCudaTile.cpp:102-137](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/RewriteAssumeWithCudaTile.cpp#L102-L137)）：先从 `tt.ptr_to_int` 取回原始 `tt.ptr`，依据其 pointee 类型构造 `tile<ptr<pointee>>`，再组装 `div_by` + `AssumeOp`，最后转回 `tt.ptr`：

```cpp
auto divByAttr = cuda_tile::DivByAttr::get(rewriter.getContext(), divisor,
                                           std::nullopt, std::nullopt);
auto ttptr2cudaPtrOp = UnrealizedConversionCastOp::create(
    rewriter, loc, cudaTilePtrType, ttPtr);
auto cudaTilePtr = ttptr2cudaPtrOp.getResult(0);
auto assumeCudaTileOp =
    cuda_tile::AssumeOp::create(rewriter, loc, cudaTilePtr, divByAttr);
Value newTtPtr =
    UnrealizedConversionCastOp::create(rewriter, loc, ttPtr.getType(),
                                       assumeCudaTileOp.getResult()).getResult(0);
```

**整数分支**（Case 1，[RewriteAssumeWithCudaTile.cpp:138-173](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/RewriteAssumeWithCudaTile.cpp#L138-L173)）：完全对称，只是类型从 `tile<ptr<T>>` 换成 `tile<IntegerType>`。

**支配受限替换**是两段共有的关键技巧，以指针分支为例（[RewriteAssumeWithCudaTile.cpp:128-136](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/RewriteAssumeWithCudaTile.cpp#L128-L136)）：

```cpp
DominanceInfo domInfo(assumeOp);
ttPtr.replaceUsesWithIf(newTtPtr, [&](OpOperand &operand) {
  Operation *user = operand.getOwner();
  if (user == ttptr2cudaPtrOp.getOperation())   // 跳过刚插入的 cast 自身
    return false;
  if (domInfo.dominates(assumeOp, user))         // 只替换 assume 之后的用法
    return true;
  return false;
});
```

为什么需要支配检查？因为 `cuda_tile.assume` 是新建在 `assume` 原位置之后才生效的，**只有发生在 `assume` 之后的用法**才能安全地「享用」这条断言；早于 `assume` 的用法若被替换成带断言的新值，就会出现「在断言生效前就使用断言结果」的时序倒挂。同时还要排除掉第一个 cast 自己对原值的引用，否则会形成自环。

#### 4.2.4 代码实践

**实践目标**：把指针分支的输入 IR 改写成预期输出 IR。

**操作步骤**：

1. 打开 [op-rewrite-assume.mlir:25-36](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-rewrite-assume.mlir#L25-L36)（`kernel_assume_2`），输入是：

   ```mlir
   %0 = tt.ptr_to_int %arg0 : !tt.ptr<i32> -> i64
   %c32_i64 = arith.constant 32 : i64
   %c0_i64  = arith.constant 0 : i64
   %1 = arith.remsi %0, %c32_i64 : i64
   %2 = arith.cmpi eq, %1, %c0_i64 : i64
   llvm.intr.assume %2 : i1
   ```

2. 按指针分支套路，手工写出改写结果，应当与文件顶部的 `CHECK` 行一致（[op-rewrite-assume.mlir:22-24](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-rewrite-assume.mlir#L22-L24)）。

**需要观察的现象**：注意除数是 `32`（来自 remsi 右操作数），而参数声明里的 `tt.divisibility = 16` 与之不同——pass 用 remsi 里真实的除数覆盖了它。

**预期结果**（即 `CHECK` 行）：

```mlir
tt.ptr_to_int %arg0 {tt.divisibility = 32 : i64} : !tt.ptr<i32> -> i64
%cast = builtin.unrealized_conversion_cast %arg0 : !tt.ptr<i32> to !cuda_tile.tile<ptr<i32>>
cuda_tile.assume div_by<32>, %cast : tile<ptr<i32>>
```

#### 4.2.5 小练习与答案

**练习 1**：为什么改写时要在 `assume` 前后各插一个 `unrealized_conversion_cast`，而不是直接把原值传给 `cuda_tile.assume`？

**答案**：因为类型不匹配。`cuda_tile.assume` 的操作数必须是 `cuda_tile` 的 tile 类型（如 `tile<i32>` / `tile<ptr<i32>>`），而被断言的值是 Triton/标量类型（`i32` / `tt.ptr<i32>`）。前一个 cast 把源类型「升」成 tile 类型喂给 `assume`，后一个 cast 再把 `assume` 结果「降」回源类型，保证后续 Triton 侧用法仍能正确接续。

**练习 2**：`DivByAttr::get` 的后两个参数为什么传 `std::nullopt`？

**答案**：`DivByAttr` 还支持 `every`/`along` 两个可选字段，用于描述「张量每 N 个元素成一组」「沿某个轴整除」这类**张量级**整除提示（在主转换 pass `TritonToTileIRPass.cpp` 里才会用到）。本 pass 处理的是标量整数和裸指针，不涉及张量轴，所以这两个字段留空，只表达最朴素的「整个值能被 `C` 整除」。

---

### 4.3 无匹配清理

#### 4.3.1 概念说明

并非每个 `llvm.intr.assume` 都是整除 idiom——它可能断言任意条件。这道 pass 的设计是「**能识别就转成 `cuda_tile.assume`，识别不了就删掉**」，而**不是**报错。

这个取舍很关键：`assume` 本质是优化提示，丢弃它只会「少优化一点」，绝不会改变程序结果。另一方面，下游的 `convert-triton-to-cuda-tile` 主转换是一道 **full conversion**，它的 `CudaTileConversionTarget` 把 `triton/scf/cf/gpu/ub`（以及随之的 LLVM 内置操作）判为**非法**——如果让 `llvm.intr.assume` 漏到那里，转换会因为「残留非法算子」而失败。所以本道 pass 必须保证：**转换链开始前，IR 里不再有任何 `llvm.intr.assume`**。要么翻译、要么删除，二者必居其一。

`Passes.td` 的描述里把这个行为写得很清楚（[Passes.td:38-40](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/include/Transform/Passes.td#L38-L40)）：

> If there are no patterns matched, the llvm.intr.assume will be removed without any new op.

#### 4.3.2 核心流程

实现上只有一个 pattern `CudaTileTensorAssumePattern`，它包住 `RewriteArithAssumeImpl`，并在**两条分支都擦除原 `assume`**：

```text
matchAndRewrite(assumeOp):
  if RewriteArithAssumeImpl(assumeOp) 成功:
      已经插入 cuda_tile.assume + 两个 cast
      eraseOp(assumeOp)        ← 原始 assume 已被新结构取代
      return success()
  else:
      eraseOp(assumeOp)        ← 无匹配，直接删除
      return failure()
```

注意：无论匹配成功与否，`llvm.intr.assume` 这个操作本身都会被 `eraseOp` 删除——区别只在于「删之前有没有插入 `cuda_tile.assume`」。

#### 4.3.3 源码精读

pattern 的 `matchAndRewrite`（[RewriteAssumeWithCudaTile.cpp:181-190](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/RewriteAssumeWithCudaTile.cpp#L181-L190)）：

```cpp
LogicalResult matchAndRewrite(LLVM::AssumeOp assumeOp,
                              PatternRewriter &rewriter) const override {
  if (succeeded(RewriteArithAssumeImpl(assumeOp, rewriter))) {
    rewriter.eraseOp(assumeOp);
    return success();
  }
  rewriter.eraseOp(assumeOp);   // 无匹配：删除原 op，不插入任何新 op
  return failure();
}
```

pass 类本身很薄（[RewriteAssumeWithCudaTile.cpp:194-210](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/lib/Transform/RewriteAssumeWithCudaTile.cpp#L194-L210)）：拿到 `ModuleOp`，注册这一个 pattern，然后用 `applyPatternsGreedily` 跑到不动点；失败才 `signalPassFailure()`：

```cpp
void runOnOperation() override {
  RewritePatternSet patterns(context);
  patterns.add<CudaTileTensorAssumePattern>(context);
  if (failed(applyPatternsGreedily(module, std::move(patterns))))
    signalPassFailure();
}
```

这道 pass **没有选项**（对比 `AutoGenMemoryToken` 有 `autogen-alias-memtoken`），`Passes.td` 里也没有 `let options`——它是一个零配置的、确定性的预处理。

> 实现注意：无匹配分支在 `eraseOp` 之后仍 `return failure()`。MLIR 的贪心重写器在 pattern 返回 `failure()` 时通常会回滚本次改动，因此「无匹配时该 op 是否真正从最终 IR 中消失」在严格意义上取决于具体 MLIR 版本的行为；但 pass 的设计意图与 `Passes.td` 的描述一致——无匹配的 `assume` 应被移除。无论运行期细节如何，核心设计是清晰的：**不让裸 `llvm.intr.assume` 进入下游转换**。（待本地验证）

#### 4.3.4 代码实践

**实践目标**：理解 pass 在转换链中的位置，以及它为何必须排在主转换之前。

**操作步骤**：

1. 打开 `make_tileir` 的挂载顺序（[compiler.py:296-320](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L296-L320)），注意三行的先后：

   ```python
   tileir.passes.add_assume_to_tileir(pm)          # 第 304 行：本讲 pass
   tileir.passes.add_triton_to_cudatile(pm, ...)   # 第 305 行：主转换（full conversion）
   tileir.passes.add_auto_gen_memtoken(pm, ...)    # 第 315 行：memtoken
   ```

2. 解释：为何 `add_assume_to_tileir` 必须在 `add_triton_to_cudatile` 之前？

**需要观察的现象**：`assume` 改写位于主转换**之前**，紧随 `lift_cf`（控制流结构化）之后。

**预期结果**：因为主转换把 `llvm`/`triton` 等方言判为非法且没有 `llvm.intr.assume` 的 lowering 模式；若不先在本 pass 里处理掉，残留的 `assume` 会让 full conversion 在 `only_contain_legal_dialects` 校验（[compiler.py:321-324](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L321-L324)）时抛 `RuntimeError`。本 pass 把可识别的 assume 翻译成合法的 `cuda_tile.assume`、不可识别的删除，为主转换扫清障碍。

#### 4.3.5 小练习与答案

**练习 1**：假设把 `add_assume_to_tileir` 这一行从 `make_tileir` 中删掉，对一个内核里含有 `llvm.intr.assume` 的程序，会发生什么？

**答案**：`llvm.intr.assume` 会原样进入 `convert-triton-to-cuda-tile` 主转换。由于该转换是 full conversion，且 `CudaTileConversionTarget` 不认 `llvm.intr.assume`（无对应 lowering），转换会留下非法算子；随后 `only_contain_legal_dialects` 返回 `False`，`make_tileir` 抛出 `"Triton ttir to tileir ir failed..."` 的 `RuntimeError`。所以本 pass 是把这类失败**前移并消解**的必要预处理。

**练习 2**：为什么「删掉无匹配的 assume」是安全的，而不会改变程序结果？

**答案**：`assume` 只是编译期的优化提示，不产生运行时指令，丢弃它只是放弃一个优化机会。程序的可观测行为（计算结果）完全不依赖某个 `assume` 是否存在——这正是它「能识别就翻译、识别不了就删除」这种宽松策略得以成立的前提。

## 5. 综合实践

**任务**：自己动手跑通这道 pass，并验证「前 / 后」IR。

**步骤**：

1. **构建工具**（依据 `AGENTS.md` 的方法，在 build 目录中）：构建出 `triton-cuda-tile-opt` 这个独立工具，它通过 [RegisterTritonCudaTileDialects.h:35](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/tools/triton-cuda-tile-opt/RegisterTritonCudaTileDialects.h#L35) 注册了 `rewrite-assume-with-cuda-tile`。

2. **跑测试**：在 build 目录执行（命令需依据你本地的 build 目录调整，待本地验证）：

   ```bash
   <build>/third_party/tileir/tools/triton-cuda-tile-opt/triton-cuda-tile-opt \
     third_party/tileir/test/FileCheck/op-rewrite-assume.mlir \
     -split-input-file \
     --pass-pipeline="builtin.module(rewrite-assume-with-cuda-tile)"
   ```

3. **对照输出**：把实际输出与文件顶部的 `CHECK` 行（[op-rewrite-assume.mlir:3-6](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-rewrite-assume.mlir#L3-L6)）逐条比对，确认：
   - 出现了 `builtin.unrealized_conversion_cast ... : i32 to !cuda_tile.tile<i32>`；
   - 紧接着是 `cuda_tile.assume div_by<32>, ... : tile<i32>`；
   - 再一个 cast 把结果转回 `i32`，并带上了 `{tt.divisibility = 32 : i32}` 属性。

4. **写一份自己的「前 / 后」IR**（即本讲规格要求的实践任务）：参考 `Passes.td` 给出的整数示例，构造一个除数为 `8` 的整除断言，写出它的 `llvm.intr.assume` 前置形态，再写出经过本 pass 后的 `cuda_tile.assume` 形态。

   **前置（示例输入）**：

   ```mlir
   // x : i32，断言 x % 8 == 0
   %c8_i32 = arith.constant 8 : i32
   %c0_i32 = arith.constant 0 : i32
   %rem = arith.remsi %x, %c8_i32 : i32
   %eq  = arith.cmpi eq, %rem, %c0_i32 : i32
   llvm.intr.assume %eq : i1
   ```

   **后置（预期输出，示例）**：

   ```mlir
   %1 = builtin.unrealized_conversion_cast %x : i32 to !cuda_tile.tile<i32>
   %2 = cuda_tile.assume div_by<8>, %1 : tile<i32>
   builtin.unrealized_conversion_cast %2 : !cuda_tile.tile<i32> to i32 {tt.divisibility = 8 : i32}
   ```

5. **观察要点**：除数 `8` 从 `remsi` 的右操作数进入 `div_by<8>` 属性；输出值的 cast 上被打上 `tt.divisibility = 8`；原始 `llvm.intr.assume` 已不在输出中。

> 若本地未配置好构建环境，无法运行 `triton-cuda-tile-opt`，则上述第 1–3 步属于「待本地验证」，第 4 步的「前 / 后 IR 推导」可作为纯阅读型实践独立完成。

## 6. 本讲小结

- `rewrite-assume-with-cuda-tile` 是 `make_tileir` 里、位于主转换 `convert-triton-to-cuda-tile` **之前**的一道零配置预处理。
- 它用 `RewriteArithAssumeImpl` 走「六步倒推」，识别 `arith.cmpi eq, arith.remsi(x, C), 0 → llvm.intr.assume` 这个整除 idiom，区分整数与指针（`tt.ptr_to_int`）两种情形。
- 匹配成功时，用 `unrealized_conversion_cast` 桥接类型，生成 `cuda_tile.assume div_by<C>`，并用 `DominanceInfo` 保证只在 `assume` 之后替换用法。
- 无匹配时按设计删除原 `assume`、不生成新 op——因为 `assume` 是优化提示，丢弃安全；而让它漏到 full conversion 会因「残留非法算子」直接报错。
- 这道 pass 的存在意义是**把对下游非法的 `llvm.intr.assume` 提前消化掉**，为主转换扫清障碍。
- 它在 pybind 侧名为 `add_assume_to_tileir`（[triton_tileir.cc:93-95](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/triton_tileir.cc#L93-L95)），命令行名为 `rewrite-assume-with-cuda-tile`，无选项。

## 7. 下一步学习建议

- 接下来建议学习 [u3-l6（无序内存模型与 AutoGenMemoryToken）](u3-l6-memory-token.md)：它紧接本讲之后在 `make_tileir` 中运行（[compiler.py:315](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/compiler.py#L315)），同样是「在转换后修整 cuda_tile IR」的 pass，但关心的是访存顺序而非断言。
- 若想看 `div_by` 属性更丰富的用法（`every`/`along` 字段、张量级整除提示），可阅读主转换 pass 中 `Assumption` 与 `DivByAttr::get` 的使用处（`third_party/tileir/lib/TritonToTileIR/TritonToTileIRPass.cpp` 约 2487 行附近）。
- 想系统了解 `triton-cuda-tile-opt` 与 lit/FileCheck 测试如何运行，可进入 [u4-l1（opt 工具与 lit 测试）](u4-l1-opt-tool-and-lit-tests.md)。
