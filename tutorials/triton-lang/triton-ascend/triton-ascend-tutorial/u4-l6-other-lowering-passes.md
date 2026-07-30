# 其他 lowering pass：HIVM / HFusion / Annotation / LLVM

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `ttir_to_linalg` 流水线里 `triton-to-annotation`、`triton-to-hivm`、`triton-to-hfusion`、`triton-to-llvm` 这四个「杂项 lowering pass」各自负责什么；
- 理解用户层 API（`tl.compile_hint`、`tl.sync_block_*`、`tl.histogram`、`tl.inline_asm_elementwise` 等）是如何在 TTIR 里落成特定方言算子，再被这四个 pass 翻译成 Ascend 专用方言的；
- 能在 dump 出来的 IR 中认出 `annotation.mark`、`hivm.hir.sync_block_*`、`hfusion.*`、`llvm.inline_asm` 这些算子，并反推它们来自哪条 Python 语句；
- 掌握用 `triton-opt` 在命令行单独复现某个 pass、用 FileCheck 验证转换结果的方法。

这四个 pass 都不是「主力算子搬运工」（那是 u4-l5 的 `triton-to-linalg` 的活），而是处理一小批**昇腾特有、无法被通用 Linalg 表达**的语义：编译提示、跨核同步、直方图/取模/卷积、内联汇编。它们规模小但「四两拨千斤」，是把用户的高级意图交给 BiSheng 工具链的最后一组桥。

## 2. 前置知识

阅读本讲前，请确认你已掌握 u4-l1（`ttir_to_linalg` 的整体 pass 编排）与 u4-l5（`triton-to-linalg` 的 Dialect Conversion 框架）。本讲会反复用到两个 MLIR 概念：

- **Dialect Conversion（方言转换）**：一个 pass 声明「哪些方言合法（`ConversionTarget::addLegalDialect`）」，再注册一组 `matchAndRewrite` 模式，框架自动把不合法的旧算子重写成合法的新算子。本讲的四个 pass 全部基于这套机制。
- **方言（Dialect）**：一组算子的命名空间。本讲会涉及 BiSheng 提供的 `annotation`、`hivm`、`hfusion` 方言与 MLIR 自带的 `llvm`、`tensor`、`arith` 方言。它们的共同特点是：**越靠近这些方言，IR 越贴近昇腾硬件**。

另外需要知道三个昇腾硬件术语（u2-l2、u8-l1 有更详细介绍）：

- **Cube Core / Vector Core**：AI 核内的两类计算单元，前者专做矩阵乘，后者做向量运算。
- **PIPE（流水线）**：昇腾核内的数据通路，如 `PIPE_MTE2`（GM→片上）、`PIPE_MTE3`（片上→GM）、`PIPE_FIX`（L0C→GM）、`PIPE_V`（向量计算）等。跨核同步必须指明「在哪条 PIPE 上等/发」。
- **CCE intrinsic**：昇腾算子编程模型（CCE）的硬件内建函数，是 inline assembly 最终映射到的硬件指令。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [compiler.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py)（`ttir_to_linalg`） | 把四个 pass 按顺序登记进 pass manager |
| [TritonToAnnotation.cpp](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToAnnotation/TritonToAnnotation.cpp) | `ascend.annotation` → `annotation.mark` |
| [TritonToHIVM.cpp](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToHIVM/TritonToHIVM.cpp) | `ascend.custom "sync_block_*"` → `hivm.sync_block*` |
| [TritonToHFusion.cpp](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToHFusion/TritonToHFusion.cpp) | `tt.histogram` / `ascend.mod` / `tt.fp_to_fp` / `ascend.conv1d` → `hfusion.*` |
| [TritonToLLVM.cpp](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLLVM/TritonToLLVM.cpp) | `tt.ElementwiseInlineAsmOp` → `llvm.inline_asm` |
| [aux_ops.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/language/cann/extension/aux_ops.py) / [core.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/language/cann/extension/core.py) | `compile_hint` / `sync_block_*` 的 Python 入口 |
| [架构文档 §3.2.2.4](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/architecture_design_and_core_features.md) | 这四个 pass 的官方一句话说明 |

## 4. 核心概念与源码讲解

先看四个 pass 在 `ttir_to_linalg` 里的注册位置。它们被紧凑地连续登记，位于「结构化/离散掩码/标量化」之后、最终的 `triton-to-linalg` 之前：

[compiler.py:206-211](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L206-L211) 连续注册了这四个 pass（外加紧随其后的 `bubble-up-operation`）：

```python
ascend.passes.ttir.add_triton_to_annotation(pm)
ascend.passes.ttir.add_triton_to_unstructure(pm, compile_on_910_95, force_simt_template)
ascend.passes.ttir.add_triton_to_hivm(pm)
ascend.passes.ttir.add_triton_to_hfusion(pm, compile_on_910_95)
ascend.passes.ttir.add_triton_to_llvm(pm)
```

注意三点：① 登记序即执行序，它们都跑在同一个 `pm.run` 里；② 只有 `triton-to-hfusion` 接收 `compile_on_910_95` 参数（950 代会把直方图交给 `triton-to-linalg` 而非本 pass）；③ 这四个 pass 都不产出 Linalg IR，只做「把特定算子搬到对应专用方言」，真正生成 Linalg 的是下一行才登记的 `add_triton_to_linalg`。

下面逐个精读。

---

### 4.1 triton-to-annotation：编译提示进入 annotation 方言

#### 4.1.1 概念说明

`tl.compile_hint(ptr, hint_name, hint_val)` 是 Ascend 语言扩展提供的「给编译器塞纸条」接口。它不改变计算结果，只在某个张量上挂一个键值属性（例如 `hivm.multi_buffer = 2`），用来引导后端做缓冲、布局等优化决策。

这条「纸条」在 TTIR 里的载体是 `ascend.annotation` 算子（[TritonAscendOps.td:47-62](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/Dialect/TritonAscend/IR/TritonAscendOps.td#L47-L62)）。但 BiSheng 工具链不认识 `ascend` 方言，它只认识自家的 `annotation` 方言。于是 `triton-to-annotation` 这个 pass 就是来「翻译纸条格式」的：把 `ascend.annotation` 换成 `annotation.mark`，属性原样转发。

#### 4.1.2 核心流程

```text
tl.compile_hint(ptr, "hivm.multi_buffer", 2)
        │  (aux_ops.compile_hint_impl → builder.create_annotation_mark)
        ▼
TTIR:   %t = ascend.annotation %ptr { hivm.multi_buffer = 2 : i32 } : tensor<...>
        │  (triton-to-annotation: TritonAnnotationConversionPattern)
        ▼
IR:     %t = annotation.mark %ptr { hivm.multi_buffer = 2 : i32 }
        │  (后续交给 BiSheng 读取、生效)
```

转换规则极简：新建一个 `annotation.mark`，把旧 op 的源操作数 `src` 接上，再把旧 op 的**所有属性**整体搬到新 op 上，最后删除旧 op。

#### 4.1.3 源码精读

整个 pass 只有一个模式。看 [TritonToAnnotation.cpp:47-59](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToAnnotation/TritonToAnnotation.cpp#L47-L59)：

```cpp
struct TritonAnnotationConversionPattern
    : OpRewritePattern<mlir::triton::ascend::AnnotationOp> {
  LogicalResult matchAndRewrite(mlir::triton::ascend::AnnotationOp op,
                                PatternRewriter &rewriter) const final {
    auto markOp = rewriter.create<annotation::MarkOp>(op.getLoc(), op.getSrc());
    // Forward all annotations.
    markOp->setAttrs(op->getAttrs());
    rewriter.eraseOp(op);
    return success();
  }
};
```

- 第 53 行新建 `annotation::MarkOp`，操作数取自原 `ascend.annotation` 的 `src`；
- 第 55 行 `markOp->setAttrs(op->getAttrs())` 把全部提示属性（如 `hivm.multi_buffer`）原封不动搬过去——这就是「纸条内容不丢」的关键；
- 第 56 行删除旧算子。

[TritonToAnnotation.cpp:61-71](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToAnnotation/TritonToAnnotation.cpp#L61-L71) 的 `runOnOperation` 里，第 64 行把 `annotation::AnnotationDialect` 设为唯一合法方言，第 68 行 `applyPartialConversion` 触发上面那个模式，把所有 `ascend.annotation` 清扫干净。

Python 侧的源头在 [aux_ops.py:104-123](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/language/cann/extension/aux_ops.py#L104-L123)：`compile_hint_impl` 根据 `hint_val` 的类型（bool/int/string/list）选用对应的 MLIR 属性构造器，最后第 123 行 `builder.create_annotation_mark(ptr.handle, hint_name, hint_val)` 生成 `ascend.annotation`。值得一提的是内置的 `multibuffer`（[aux_ops.py:159-171](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/language/cann/extension/aux_ops.py#L159-L171)）就是对 `compile_hint_impl` 的薄封装，hint 名固定为 `"hivm.multi_buffer"`。

> Pass 注册名见 [TritonToAnnotation/Passes.td:11-15](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToAnnotation/Passes.td#L11-L15)：`Pass<"triton-to-annotation">`。

#### 4.1.4 代码实践

源码阅读型实践（无需硬件）：

1. **目标**：确认 `compile_hint` 的属性类型映射。
2. **步骤**：打开 [aux_ops.py:110-122](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/language/cann/extension/aux_ops.py#L110-L122)，列出 `hint_val` 为 `bool` / `int` / `constexpr` / `list` 时分别调用哪个 `builder.get_*_attr`。
3. **观察**：注意「bool 必须先判断」（第 110 行），因为 `False` 也是「假值」，否则会被第 112 行的 `not hint_val` 误当成无值属性。
4. **预期结果**：写出一张「Python 类型 → MLIR 属性」对照表。

#### 4.1.5 小练习与答案

**练习 1**：如果调用 `tl.compile_hint(ptr, "hivm.multi_buffer")` 不传第三个参数，最终 `annotation.mark` 上会挂什么属性？
**答案**：由 [aux_ops.py:112-113](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/language/cann/extension/aux_ops.py#L112-L113) 可知，`hint_val` 为假值时生成 `builder.get_unit_attr()`，即一个「只有键、无值」的 Unit 属性，表示「这个 hint 存在即可」。

**练习 2**：为什么 `triton-to-annotation` 要单独成 pass，而不是在 `triton-to-linalg` 里顺手处理？
**答案**：因为 `annotation.mark` 属于 BiSheng 的 `annotation` 方言，不属于 Linalg 体系；`triton-to-linalg` 的合法方言集里没有它。把「翻译纸条」隔离成独立 pass，职责清晰，也方便 BiSheng 单独识别这些标记。

---

### 4.2 triton-to-hivm：跨核同步进入 HIVM 方言

#### 4.2.1 概念说明

在 Cube-Vector 协同的 kernel 里（u8 单元），Cube 核算完矩阵、把结果写回共享内存后，必须通知 Vector 核「数据好了，可以读」。这种跨核握手就是 `tl.sync_block_set` / `tl.sync_block_wait`；如果要全局栅栏，则用 `tl.sync_block_all`。

它们在 TTIR 里以 `ascend.custom "sync_block_set/wait/all"` 的形式存在（[TritonAscendOps.td:452](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/Dialect/TritonAscend/IR/TritonAscendOps.td#L452) 定义的 `CustomOp`，字符串 `str_args` 里存着 sender 和 event_id）。`triton-to-hivm` 的工作是把这些通用「custom」算子翻译成 BiSheng HIVM 方言里语义明确的同步指令：`hivm.hir.sync_block_set` / `sync_block_wait` / `sync_block`，并依据 sender 是 cube 还是 vector，自动推导出正确的**核类型（TCoreType）**与**PIPE（流水线）**。

> 小贴士：现代 Python API（[core.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/language/cann/extension/core.py) 的 `sync_block_set/wait/all`）已改为在 builder 里**直接**生成 `hivm.*` 算子；本 pass 主要兜底处理仍以 `ascend.custom` 形式出现的同步（如旧版 API 或内部生成的 IR）。无论哪条路径，最终 IR 里看到的都是 `hivm.hir.sync_block*`。

#### 4.2.2 核心流程

同步语义用「信号量计数器」建模：每个 `event_id`（0~15）对应一个计数器，初值 0。`set` 让计数器 +1，`wait` 在计数器 >0 时 -1 并继续，否则阻塞。

```text
ascend.custom "sync_block_set"   {str_args=["cube", 2]}
        │  (GetCoreAndPipes: sender="cube" → 核=CUBE, prod=PIPE_FIX, cons=PIPE_MTE2)
        ▼
hivm.hir.sync_block_set[<CUBE>, <PIPE_FIX>, <PIPE_MTE2>] flag = 2
```

`sync_block_all` 则按 `mode`（`all_cube`/`all_vector`/`all`）选三种 `SyncBlockMode` 之一，生成一条全局 `hivm.hir.sync_block` 指令。

核类型与 PIPE 的推导规则（来自硬件分工）：

| 操作 | sender | 推导核类型 | producer pipe | consumer pipe |
| --- | --- | --- | --- | --- |
| `sync_block_set` | cube | CUBE | PIPE_FIX | PIPE_MTE2 |
| `sync_block_wait` | cube | VECTOR | PIPE_FIX | PIPE_MTE2 |
| `sync_block_set` | vector | VECTOR | PIPE_MTE3 | PIPE_MTE2 |
| `sync_block_wait` | vector | CUBE | PIPE_MTE3 | PIPE_MTE2 |

直觉：`set` 跑在「发送方」自己的核上，`wait` 跑在「接收方」的核上；cube 发数据走 `PIPE_FIX`（L0C→GM），vector 发数据走 `PIPE_MTE3`（UB→GM），接收方统一用 `PIPE_MTE2`（GM→片上）去消费。

#### 4.2.3 源码精读

核心推导在辅助函数 [TritonToHIVM.cpp:69-96](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToHIVM/TritonToHIVM.cpp#L69-L96)：

```cpp
static CoreAndPipes GetCoreAndPipes(MLIRContext *ctx, StringRef opName,
                                    StringRef sender) {
  PipeAttr consumer = PipeAttr::get(ctx, PIPE::PIPE_MTE2);
  if (sender == "cube")  producer = PipeAttr::get(ctx, PIPE::PIPE_FIX);
  else                   producer = PipeAttr::get(ctx, PIPE::PIPE_MTE3);

  if (sender == "cube")
    core = (opName == "sync_block_set") ? CUBE : VECTOR;
  else
    core = (opName == "sync_block_set") ? VECTOR : CUBE;
  return {core, producer, consumer};
}
```

主模式 [TritonToHIVM.cpp:106-157](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToHIVM/TritonToHIVM.cpp#L106-L157) 按 op 名分三支：

- 第 120-137 行处理 `sync_block_all`：读 `str_args[0]` 的字符串（`"all_cube"`/`"all_vector"`/`"all"`），调用 [CreateSyncBlock](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToHIVM/TritonToHIVM.cpp#L59-L67) 生成 `hivm::SyncBlockOp`，模式分别取 `ALL_CUBE`/`ALL_VECTOR`/`ALL`；
- 第 139-145 行处理 `sync_block_set`：用 `GetCoreAndPipes` 推导后生成 `hivm::SyncBlockSetOp`；
- 第 147-153 行处理 `sync_block_wait`：同样推导后生成 `hivm::SyncBlockWaitOp`。

[TritonToHIVM.cpp:159-169](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToHIVM/TritonToHIVM.cpp#L159-L169) 的 `runOnOperation` 第 162 行把 `hivm::HIVMDialect` 设为合法方言，`applyPartialConversion` 触发转换。

#### 4.2.4 代码实践

可复现的命令行实践（源码构建后，无需 NPU）。仓库已自带一个 FileCheck 测试，正好用来观察这个 pass：

1. **目标**：亲眼看到 `ascend.custom "sync_block_*"` 变成 `hivm.hir.sync_block*`。
2. **步骤**：阅读测试输入 [sync_block_op_conversion.mlir](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/unittest/Conversion/General/TritonToHIVM/sync_block_op_conversion.mlir)（第 5-11 行是输入）。若已构建项目，运行：

   ```bash
   triton-opt --triton-to-hivm \
     third_party/ascend/unittest/Conversion/General/TritonToHIVM/sync_block_op_conversion.mlir
   ```
3. **观察**：对照文件第 14-20 行的 `// CHECK` 注释，确认输出里出现 `hivm.hir.sync_block_set[<VECTOR>, <PIPE_MTE3>, <PIPE_MTE2>] flag = 1` 等行。
4. **预期结果**：`sender="vector"` 的 `set` 被推导成核类型 `VECTOR`、prod pipe `PIPE_MTE3`，与 4.2.2 的表格一致。
5. **无法构建时**：标注「待本地验证」，转而精读 `CHECK` 行也能得到同样的映射结论。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `consumer` pipe 在所有情况下都被硬编码成 `PIPE_MTE2`？
**答案**：因为接收方无论来自 cube（经 L0C→GM 的 `PIPE_FIX`）还是 vector（经 UB→GM 的 `PIPE_MTE3`），数据最终都落在全局内存 GM 上；接收方要从 GM 把数据搬进自己的片上存储，这条「GM→片上」通路就是 `PIPE_MTE2`，所以消费侧恒为 `PIPE_MTE2`。

**练习 2**：若 `str_args` 里的 `mode` 既不是 `all_cube`/`all_vector` 也不是 `all`，会发生什么？
**答案**：[TritonToHIVM.cpp:133-134](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToHIVM/TritonToHIVM.cpp#L133-L134) 走 `EmitUnknownOpError`，对该 op 报「Unknown custom operation」并返回 `failure()`，导致 `applyPartialConversion` 失败、pass 报错。（注意：`all_sub_vector` 只在现代 builder 路径支持，本 pass 的 `sync_block_all` 分支不处理它。）

---

### 4.3 triton-to-hfusion：直方图/取模/卷积进入 HFusion 方言

#### 4.3.1 概念说明

`hfusion`（Heterogeneous Fusion）是 BiSheng 里描述「可被向量化融合的高性能算子」的方言。有几类 Triton 算子，比起被 `triton-to-linalg` 拆成一串通用 Linalg 基元，直接对应到一个 `hfusion` 命名算子会更高效，于是交给本 pass 处理：

- `tt.histogram`（直方图）→ `hfusion.histogram`；
- `ascend.mod`（逐元素取模）→ `hfusion.elemwise_binary {fun = mod}`；
- `tt.fp_to_fp`（浮点精度转换，**非默认 RTNE 舍入**时）→ `hfusion.cast {mode = TRUNC}`；
- `ascend.conv1d`（一维卷积）→ `hfusion.conv1d`。

与 annotation/hivm 用 `applyPartialConversion` 不同，本 pass 用 **greedy pattern rewriter**（`applyPatternsGreedily`）：每个模式自己决定「要不要转换」（返回 `failure()` 即跳过，不会让 pass 失败），这让「条件性 lowering」（比如只处理 RTZ 舍入）写起来很自然。

#### 4.3.2 核心流程

```text
tt.histogram %in : tensor<16xi32> -> tensor<2xi32>
        │  (numBins = 结果张量的元素数 = 2)
        ▼
hfusion.histogram %in, 2 : (tensor<16xi32>) -> tensor<2xi32>
```

直方图的 bin 数来自**结果张量的元素个数**（不是输入），默认回退值 256。取模则建一个空的目的张量，按 `hfusion` 的二元算子模板生成 `{fun = mod}`。卷积会根据输入是否带 batch 维（3D vs 2D）算出输出长度并建空张量。

一个关键门控：950 代（`compile_on_910_95=true`）的直方图**不走本 pass**，而是交给 `triton-to-linalg` 里的 `HistogramConverter`，所以本 pass 只在非 950 上注册直方图模式。

#### 4.3.3 源码精读

直方图模式 [TritonToHFusion.cpp:56-79](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToHFusion/TritonToHFusion.cpp#L56-L79)：

```cpp
int64_t numBins = 256; // 256 is default fallback.
if (auto rankedTy = dyn_cast<RankedTensorType>(resultType))
  if (rankedTy.hasStaticShape() && rankedTy.getNumElements() > 0)
    numBins = rankedTy.getNumElements();
...
auto newOp = rewriter.create<hfusion::HistogramOp>(loc, resultType, input,
                                                   numBinsAttr, Value());
```

第 66-69 行确定 bin 数（取结果张量元素数），第 73 行生成 `hfusion::HistogramOp`。

取模模式 [TritonToHFusion.cpp:31-54](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToHFusion/TritonToHFusion.cpp#L31-L54) 用 `hfusion::createBinaryOp` 工厂生成 `{fun = mod}` 的二元算子。

舍入转换模式 [TritonToHFusion.cpp:81-134](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToHFusion/TritonToHFusion.cpp#L81-L134) 最值得读：第 100-106 行明确「RTNE（默认）或没指定舍入 → 返回 `failure()`，让 `triton-to-linalg` 用 `arith.truncf/extf` 处理」；只有 RTZ（向零取整）才会被映射成 `hfusion::CastOp {mode = TRUNC}`（第 110-116 行）。

一维卷积模式 [TritonToHFusion.cpp:136-206](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToHFusion/TritonToHFusion.cpp#L136-L206) 第 172-174 行按卷积公式算输出长度：

\[ L_{out} = \left\lfloor \frac{L_{in} + 2 \cdot \text{padding} - \text{dilation}\cdot(k-1) - 1}{\text{stride}} \right\rfloor + 1 \]

然后按是否带 batch 维建对应形状的空张量，生成 `hfusion::Conv1DOp`。

`runOnOperation` 见 [TritonToHFusion.cpp:217-239](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToHFusion/TritonToHFusion.cpp#L217-L239)，第 227-229 行的门控：

```cpp
if (!compileOn91095) {
  patterns.add<TritonHistogramToHFusionConversion>(patterns.getContext());
}
```

> Pass 注册名见 [TritonToHFusion/Passes.td:11-15](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToHFusion/Passes.td#L11-L15)：`Pass<"triton-to-hfusion">`。

#### 4.3.4 代码实践

可复现的命令行实践：

1. **目标**：观察 `tt.histogram` 与 `ascend.mod` 各自的 lowering 结果。
2. **步骤**：阅读测试 [histogram.mlir](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/unittest/Conversion/General/TritonToHFusion/histogram.mlir) 与 [mod.mlir](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/unittest/Conversion/General/TritonToHFusion/mod.mlir)。若已构建，运行：

   ```bash
   triton-opt --triton-to-hfusion \
     third_party/ascend/unittest/Conversion/General/TritonToHFusion/histogram.mlir
   triton-opt --triton-to-hfusion \
     third_party/ascend/unittest/Conversion/General/TritonToHFusion/mod.mlir
   ```
3. **观察**：直方图输入 `tensor<16xi32> -> tensor<2xi32>`，输出应是 `hfusion.histogram %arg0, 2`（bin 数 = 2）；取模输出应是 `hfusion.elemwise_binary {fun = #hfusion.binary_fn<mod>}`。
4. **预期结果**：与各文件顶部的 `// CHECK` 行一致。
5. **无法构建时**：标注「待本地验证」，但 `CHECK` 行已写明期望输出，可直接据其确认映射关系。

#### 4.3.5 小练习与答案

**练习 1**：把直方图结果张量改成 `tensor<256xi32>`，输出的 `hfusion.histogram` 第二个参数会变成多少？
**答案**：变成 256。由 [TritonToHFusion.cpp:67-69](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToHFusion/TritonToHFusion.cpp#L67-L69) 可知 bin 数取结果张量元素数；有趣的是这恰好等于默认回退值 256，所以看不出区别——这也是为什么测试故意用 `tensor<2xi32>` 来暴露该逻辑。

**练习 2**：`tt.fp_to_fp` 用默认 RTNE 舍入时，由谁 lowering？
**答案**：由 `triton-to-linalg` 用 `arith.truncf/extf` 处理。本 pass 的 `TritonFpToFpToHFusionConversion` 在第 102-105 行检测到 RTNE 即返回 `failure()` 主动放行。

---

### 4.4 triton-to-llvm：内联汇编映射到 LLVM（CCE intrinsic）

#### 4.4.1 概念说明

`tl.inline_asm_elementwise(...)` 让用户在 Triton kernel 里直接写一段硬件汇编（类似 GCC inline asm），用来调用 BiSheng/CCE 尚未 surfaced 的硬件能力。它在 TTIR 里是 `tt.ElementwiseInlineAsmOp`，携带汇编字符串、约束、是否纯函数、以及「打包元素数 `packedElement`」等信息。

`triton-to-llvm` 把它翻译成 MLIR LLVM 方言的 `llvm.inline_asm`。这一步之所以不平凡，是因为 Triton 的张量是「块级」的（一次处理整个 block），而 LLVM inline asm 是「寄存器级」的——必须把张量**拆成标量**喂给汇编，再把结果**拼回张量**；并且昇腾寄存器以 32 位为处理粒度，小于 32 位的元素要**打包**进 32 位寄存器。最终这段 `llvm.inline_asm` 会被 BiSheng 映射成 CCE intrinsic。

#### 4.4.2 核心流程

```text
tt.ElementwiseInlineAsmOp(asm, constraints, packed=N, ...)
        │  无操作数 → processScalarInlineAsm（标量汇编）
        │  有操作数 → processVectorInlineAsm：
        │       1) unpackElements: 把张量拆成标量列表
        │       2) packOperands:   小于32位的元素按 numElementPerReg 打包
        │       3) 每 N 个元素一组，建一个 LLVM::InlineAsmOp
        │       4) 把汇编返回值拆开、拼回张量
        ▼
llvm.inline asm ...  (最终经 BiSheng → CCE intrinsic)
```

`numElementPerReg = min(32 / bitWidth, packedElement)`：例如 16 位元素、`packed=8` 时，`32/16=2`，即 2 个 16 位元素打包成一个 32 位寄存器。

#### 4.4.3 源码精读

派发逻辑在 [TritonToLLVM.cpp:242-251](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLLVM/TritonToLLVM.cpp#L242-L251)：

```cpp
LogicalResult matchAndRewrite(triton::ElementwiseInlineAsmOp op,
                              PatternRewriter &rewriter) const final {
  return op.getOperands().empty() ? processScalarInlineAsm(op, rewriter)
                                  : processVectorInlineAsm(op, rewriter);
}
```

打包逻辑 [TritonToLLVM.cpp:51-77](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLLVM/TritonToLLVM.cpp#L51-L77) 的关键几行：

```cpp
unsigned numElementPerReg = std::max(32 / bitWidth, 1u);
numElementPerReg = std::min(numElementPerReg, numPackedElements);
// 用 LLVM::InsertElementOp 把多个元素塞进一个 vector
```

向量路径 [processVectorInlineAsm](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLLVM/TritonToLLVM.cpp#L195-L238) 用 `unpackElements`（[第 79-102 行](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLLVM/TritonToLLVM.cpp#L79-L102)，用 `tensor.extract` 逐元素拆张量）配合 `createDestOps`（[第 104-178 行](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLLVM/TritonToLLVM.cpp#L104-L178)，按 `packedElement` 分组、建 `LLVM::InlineAsmOp`、再 `ExtractValue/ExtractElement` 拆回），最后用 `tensor.from_elements` 拼回张量。

[TritonToLLVM.cpp:253-264](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLLVM/TritonToLLVM.cpp#L253-L264) 的 `runOnOperation` 把 `tensor`/`LLVM`/`arith` 三个方言设为合法，触发转换。

> Pass 注册名见 [TritonToLLVM/Passes.td:11-15](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/include/TritonToLLVM/Passes.td#L11-L15)：`Pass<"triton-to-llvm">`。

#### 4.4.4 代码实践

源码阅读型实践：

1. **目标**：理解「块级张量」如何被喂给「寄存器级」汇编。
2. **步骤**：跟随 [processVectorInlineAsm](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLLVM/TritonToLLVM.cpp#L195-L238) 的控制流，数一数一次 `inline_asm_elementwise` 调用会生成多少个 `LLVM::InlineAsmOp`。
3. **观察**：外层循环 `for (i = 0; i < resultLength; i += packedElement)`（第 212 行）每组 `packedElement` 个元素产生一次 `createDestOps`，即一次 `LLVM::InlineAsmOp`。
4. **预期结果**：对一个元素数 `E`、打包数 `P` 的张量，生成 \( E / P \) 条 `llvm.inline_asm`。
5. **结论**：这正是「向量化硬件单元」与「标量汇编」之间的桥梁，也解释了为什么 inline asm 过多会显著拉低性能。

#### 4.4.5 小练习与答案

**练习 1**：一个 16 位元素、共 64 个元素、`packed=8` 的 inline asm，会生成几条 `llvm.inline_asm`？每条操作数里有几个元素？
**答案**：`numElementPerReg = min(32/16, 8) = 2`，所以每条 `LLVM::InlineAsmOp` 内部把 2 个 16 位元素打包成一个 `<2 x f16>` 寄存器；总条数 = 64 / 8 = 8 条，每条处理 8 个元素（即 4 个 `<2 x f16>` 寄存器）。

**练习 2**：为什么 `op.getPure()` 决定了 `LLVM::InlineAsmOp` 的 `has_side_effects`？
**答案**：`pure=true` 表示该汇编无副作用、可被优化器移动或删除，对应 `has_side_effects=false`；反之带副作用的汇编（如读写硬件状态）必须标 `has_side_effects=true`，防止编译器乱序。见 [TritonToLLVM.cpp:144](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLLVM/TritonToLLVM.cpp#L144) 的 `/*has_side_effects=*/!op.getPure()`。

---

## 5. 综合实践

把本讲四个 pass 串起来，设计一个**含跨核同步 + 编译提示**的 kernel，dump 出 IR 观察这些 pass 产出的方言算子。这是本讲规格里指定的实践任务。

> 说明：本实践需要真实昇腾环境（950 代、含 Cube/Vector 核）才能真正跑通编译；若无硬件，请用 4.2.4 / 4.3.4 的 `triton-opt` 命令行实践作为等价替代，并标注「待本地验证」。

**实践目标**：在一个 kernel 里同时使用 `al.scope` + `al.sync_block_set/wait` 与 `al.compile_hint`（或 `al.multibuffer`），通过 IR dump 确认 `triton-to-annotation` 与 `triton-to-hivm` 生成了哪些算子。

**示例代码**（仅为观察 IR 用，非项目自带文件）：

```python
# 示例代码：观察 sync_block_* 与 compile_hint 的 lowering
import triton
import triton.language as tl
import triton.language.extra.cann.extension as al

@triton.jit
def kernel_with_sync_and_hint(in_ptr, out_ptr, N: tl.constexpr,
                              BLOCK: tl.constexpr):
    pid = tl.program_id(0)
    offs = pid * BLOCK + tl.arange(0, BLOCK)
    mask = offs < N
    # 1) 编译提示：给 load 进来的张量挂 multi_buffer
    x = tl.load(in_ptr + offs, mask=mask)
    al.compile_hint(x, "hivm.multi_buffer", 2)   # 等价于 al.multibuffer(x, 2)
    # 2) 跨核同步：cube 侧 set，vector 侧 wait（需在各自 scope 中）
    with al.scope(core_mode="cube"):
        al.sync_block_set("cube", "vector", 0)
    y = x + 1.0
    tl.store(out_ptr + offs, y, mask=mask)
    with al.scope(core_mode="vector"):
        al.sync_block_wait("cube", "vector", 0)
```

**操作步骤**：

1. 准备 950 代环境，设置 `export TRITON_DEBUG=1` 与 `export MLIR_ENABLE_DUMP=1`（调试环境变量详见 u10-l1、u10-l3）。
2. 用合适的 grid 启动该 kernel。
3. 在终端打印的 dump 目录里（`TRITON_DEBUG` 会打印 `Dumping intermediate results to ...` 路径），打开 `kernel.ttadapter.mlir`——这是 `ttir_to_linalg`（含本讲四个 pass）跑完后的 IR。

**需要观察的现象**：

- 搜索 `annotation.mark`：应能找到一处 `annotation.mark %x { hivm.multi_buffer = 2 : i32 }`，它来自 `compile_hint` 经 `triton-to-annotation` 的翻译；
- 搜索 `hivm.hir.sync_block`：应能看到 `sync_block_set` / `sync_block_wait` 形式的算子（若走了 `ascend.custom` 表示则由 `triton-to-hivm` 翻译；若现代 builder 已直接生成 `hivm.*`，则在 TTIR 阶段就已存在）。

**预期结果**：两个专用方言的算子都出现在 `ttadapter.mlir` 中，且原 `ascend.annotation` / `ascend.custom "sync_block_*"` 已被消除。

**待本地验证**：上述运行行为依赖真实 950 硬件与 CANN 工具链；dump 路径与确切算子文本以本地实际输出为准。无硬件时，4.2.4 的 `triton-opt --triton-to-hivm` 输出是最可靠的参照。

## 6. 本讲小结

- 这四个 pass 在 `ttir_to_linalg` 里位于 L206-L210，连续登记、顺序执行，专门处理 Linalg 表达不了的「昇腾特有语义」，本身不产出 Linalg IR。
- `triton-to-annotation` 把 `tl.compile_hint` 产生的 `ascend.annotation` 翻译成 BiSheng 的 `annotation.mark`，属性原样转发，是「编译纸条」的格式转换器。
- `triton-to-hivm` 把跨核同步 `ascend.custom "sync_block_set/wait/all"` 翻译成 `hivm.hir.sync_block*`，并依据 sender 自动推导核类型（CUBE/VECTOR）与 PIPE（FIX/MTE3/MTE2）。
- `triton-to-hfusion` 用 greedy rewriter 把 `histogram`/`mod`/（非 RTNE）`fp_to_fp`/`conv1d` 映射成 `hfusion.*` 命名算子；950 代的直方图走 `triton-to-linalg` 而非本 pass。
- `triton-to-llvm` 把 `tl.inline_asm_elementwise` 拆标量、按 32 位打包、翻译成 `llvm.inline_asm`，最终由 BiSheng 映射为 CCE intrinsic，是「块级张量」与「寄存器级汇编」的桥梁。
- 调试时，`triton-opt --triton-to-{hivm,hfusion}` 配合仓库自带的 FileCheck `.mlir` 测试，是无需硬件即可复现这些 pass 行为的最便捷方式。

## 7. 下一步学习建议

- **深入跨核同步的工程用法**：本讲只讲了「set/wait 翻成 hivm」，真正的 sender/receiver 协同要结合 u7-l4（同步原语）与 u8（Cube-Vector 融合）才能理解何时该插同步。
- **看懂 BiSheng 侧如何消费这些方言**：`annotation.mark`、`hivm.*`、`hfusion.*` 会在后续 `npubin` 阶段被 BiSheng 工具链处理，可结合 u3-l3 的产物与元数据机制对照阅读。
- **动手写一个 pass（u10-l5）**：这四个 pass 都是「单模式 + Dialect Conversion」的极简范本，是学习「如何新增一个 Ascend MLIR pass」的最佳模板；建议学完本讲后直接做 u10-l5 的二次开发实战。
- **调试与 dump（u10-l1）**：本讲综合实践用到的 `TRITON_DEBUG` / `MLIR_ENABLE_DUMP` 在 u10-l1 有系统讲解，想熟练 dump 各阶段 IR 可前往学习。
