# 扩展：新增 tile 算子

## 1. 本讲目标

TileLang 的 DSL 之所以能用一行 `T.copy`、`T.gemm` 表达一块 tile 的搬运或矩阵乘，是因为这些「tile 算子」在编译期会被识别、解析成一个 C++ 对象，再由编译器统一降级（lower）成底层 TIR。本讲要回答的核心问题是：

> **如果你想给 TileLang 增加一个全新的 tile 算子（比如 `T.transpose_copy`），需要在 C++ 与 Python 两侧做哪些改动？**

学完本讲，你应当能够：

- 说清 `TileOperatorNode` 这个抽象基类定义了哪几个必须实现的接口，以及它们各自被哪个 pass 调用。
- 理解一条 `tl.tileop.xxx` 的 TIR 调用是如何经 `TLOpBuilder` 被还原成一个算子对象的。
- 区分两类 `Lower()` 实现风格：自包含的 SIMT 循环降级（copy/transpose）与外包给 Python 全局函数的降级（gemm）。
- 理解 `InferLayout()` 在什么情况下返回空、在什么情况下必须返回 `Fragment` 布局。
- 对照 copy / transpose 算子，列出新增一个 `transpose_copy` 算子的完整改动清单。

## 2. 前置知识

本讲是「专家层·扩展机制」的一篇，默认你已经读过：

- **u5-l1（C++ 编译器核心总览）**：知道 `TileOperatorNode` 是所有 tile 算子的基类，有两个核心方法 `Lower()` 与 `InferLayout()`，分别由 `LowerTileOp` 与 `LayoutInference` 两个 pass 驱动。
- **u4-l2（tile 算子与 T.gemm 的分派）**：知道 GEMM 算子如何做指令选择、`Lower`/`InferLayout` 如何外包给 Python。
- **u2-l4（内存层级与显存分配）**：知道 `T.copy` 在 global/shared/fragment 间搬运的本质。

补充几个本讲会反复用到的术语：

- **TIR Call / Op**：TVM 中间表示里的一次函数调用，`call->op` 是一个 `Op` 对象（可看作「算子名句柄」，如 `tl.tileop.copy`）。
- **SBlock / Evaluate**：tilelang 把每个 tile 算子调用包在一个 `Evaluate(Call(...))` 语句里，挂在 `SBlock`（tile 块）下。
- **作用域（scope）**：buffer 的存储层级，如 `global`、`shared.dyn`、`local.fragment`。`Lower()` 的产物需要把 fragment buffer 的逻辑下标正确映射到线程。
- **Fragment 布局**：fragment buffer 的「逻辑坐标 → (线程号, 寄存器槽)」映射，由 `LayoutInference` pass 推断（见 u4-l3）。

一句话回顾整条链路：用户写 `T.xxx(...)` → 前端生成 `tl.tileop.xxx(...)` 的 TIR Call → `LowerTileOp` pass 用 `ParseOperator` 把它还原成 `TileOperator` 对象 → 调用其 `Lower()` 得到底层 TIR；与此同时 `LayoutInference` pass 调用其 `InferLayout()` 推断 fragment 布局。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 作用 |
| --- | --- |
| [src/op/operator.h](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/operator.h) | 定义 `TileOperatorNode` 基类、`LowerArgs`/`LayoutInferArgs` 参数结构、`TIR_REGISTER_TL_TILE_OP` 注册宏 |
| [src/op/operator.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/operator.cc) | `ParseOperator`：从 TIR Call 查 `TLOpBuilder` 表构造算子 |
| [src/op/copy.h](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/copy.h) / [copy.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/copy.cc) | `CopyNode`：自包含 SIMT 降级的标杆范例，含目标分派注册表 |
| [src/op/transpose.h](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/transpose.h) / [transpose.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/transpose.cc) | `TransposeNode`：最小、最干净的 SIMT 算子范例（`InferLayout` 返回空） |
| [src/op/gemm.h](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/gemm.h) / [gemm.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/gemm.cc) | `GemmNode`：`Lower`/`InferLayout` 外包给 Python 全局函数的复杂范例 |
| [src/metal/op/transpose.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/metal/op/transpose.cc) | 单后端算子实现注册范例（`RegisterTransposeImpl`） |
| [tilelang/language/copy_op.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/copy_op.py) | Python 侧 DSL 入口：`copy`/`transpose` 等，产出 `tl.tileop.*` 的 TIR Call |
| [tilelang/language/\_\_init\_\_.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/__init__.py) | 把 DSL 函数挂到 `T` 命名空间 |
| [src/transform/lower_tile_op.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/lower_tile_op.cc) | `LowerTileOp` pass：调用 `ParseOperator` + `tile_op->Lower()` |
| [src/transform/layout_inference.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/layout_inference.cc) | `LayoutInference` pass：调用 `tile_op->InferLayout()` |

> 本讲以 **copy（自包含降级）** 与 **transpose（最小范例）** 为主线，**gemm（外包 Python）** 为对照。三者合起来覆盖了新增算子会遇到的所有典型情况。

## 4. 核心概念与源码讲解

### 4.1 TileOperatorNode 抽象

#### 4.1.1 概念说明

在 tilelang 里，`T.copy`、`T.gemm`、`T.reduce_*` 这些「tile 级」算子和普通 TIR 表达式（如 `+`、`*`）不同：它们操作的是一整块 tile（一个 buffer region），语义上代表一段**有待降级的计算规格**。编译器不会直接执行它们，而是先把它们识别成一个 C++ 对象——`TileOperator`——再在后续 pass 里把这个对象展开成真正的底层 TIR（循环、`cp.async`、`mma` 指令等）。

`TileOperatorNode` 就是所有这类算子的抽象基类。它只规定三件**每个算子必须会做的事**：

1. **`Lower()`**：把自己降级成底层 TIR 语句（`Stmt`）。由 `LowerTileOp` pass 调用。
2. **`InferLayout()`**：推断自己用到的 fragment buffer 的寄存器布局。由 `LayoutInference` pass 调用。
3. **`Clone()`**：深拷贝自己（pass 改写 IR 时需要复制算子节点）。

这三个方法是「纯虚」（`= 0`）的——基类不提供默认实现，新增算子必须自己实现。

#### 4.1.2 核心流程

一个 tile 算子在编译期经历的生命周期：

```text
DSL 调用 T.xxx(src, dst, ...)
        │  (前端 copy_op.py 等生成)
        ▼
TIR 语句: Evaluate(Call(op = "tl.tileop.xxx", args=[...], annotations={...}))
        │
        │  LowerTileOp pass 遇到这条 Evaluate
        ▼
ParseOperator(call)  ──查 TLOpBuilder 表──▶  XxxNode 对象 (继承 TileOperatorNode)
        │
        ├─ LayoutInference pass  ──调用──▶  op->InferLayout(...)  → LayoutMap
        │
        └─ LowerTileOp pass      ──调用──▶  op->Lower(...)        → Stmt (底层 TIR)
```

关键点：**算子对象是「无状态的计算规格」**——它在 `Lower` 与 `InferLayout` 之间可能被复制（`Clone`）、被多个 pass 各读一次，因此实现里不应依赖调用顺序产生的副作用（GEMM 用 `mutable completed_` 做了缓存去重，是特例）。

#### 4.1.3 源码精读

基类定义在 [src/op/operator.h:148-174](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/operator.h#L148-L174)：

```cpp
class TileOperatorNode : public ffi::Object {
public:
  virtual tirx::Stmt Lower(const LowerArgs &lower_args,
                           arith::Analyzer *analyzer) const = 0;

  virtual LayoutMap InferLayout(const LayoutInferArgs &layout_args,
                                InferLevel level) const = 0;

  virtual TileOperator Clone() const = 0;

  virtual AccessRegions GetAccessRegions() const { /* 默认实现 */ }
  void SetAccessRegions(std::vector<AccessRegion> access_regions) { ... }

  TVM_FFI_DECLARE_OBJECT_INFO("tl.TileOperator", TileOperatorNode, ffi::Object);
protected:
  std::vector<AccessRegion> access_regions_;
};
```

- `Lower()` 与 `InferLayout()` 是 `= 0` 纯虚函数——**新增算子必须实现这两个**。
- `Clone()` 同样纯虚——通常实现成「拷贝自己的 node」一行（见 4.2）。
- `GetAccessRegions()` 有默认实现，依据 `access_regions_`（在构造函数里用 `SetAccessRegions` 设置）把 buffer 的读/写区域分门别类，供后续 pass 做生存期分析。简单算子直接复用默认即可。
- `TVM_FFI_DECLARE_OBJECT_INFO` 把这个类型注册到 tvm-ffi 反射系统，使其能跨 Python/C++ 边界传递。

`Lower()` 与 `InferLayout()` 的入参是两个「参数包」结构体，定义在 [src/op/operator.h:95-144](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/operator.h#L95-L144)，里面装着 pass 下发的上下文：

- `LowerArgs`：`target`（编给谁）、`thread_bounds`/`thread_var`（线程范围与线程变量）、`layout_map`（已推断的 fragment 布局）、`buffer_remap`（buffer 重命名映射），以及一组回调（`add_workspace` 申请动态 shared、`alloc_mbarrier` 申请 mbarrier、`require_smem_alignment` 上报对齐需求）。
- `LayoutInferArgs`：`target`、`thread_bounds`、`layout_map`、`analyzer`、`in_pipeline`（是否在流水线循环内）等。

> 对新增算子而言，你**不需要关心这些参数怎么来**——pass 会负责填好。你只需要在实现里按需取用（比如 `Lower` 里读 `lower_args.target` 决定走哪条降级路径）。

#### 4.1.4 代码实践

**目标**：在源码里定位「三个必须实现的接口」，并确认它们被哪两个 pass 调用。

**步骤**：

1. 打开 [src/op/operator.h:148-174](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/operator.h#L148-L174)，找到 `Lower`、`InferLayout`、`Clone` 三个 `virtual ... = 0` 声明。
2. 在 [src/transform/lower_tile_op.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/lower_tile_op.cc) 搜索 `tile_op->Lower(`，确认它出现在 `VisitStmt_(const EvaluateNode *)` 里（约 [L1221](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/lower_tile_op.cc#L1221)）。
3. 在 [src/transform/layout_inference.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/layout_inference.cc) 搜索 `->InferLayout(`，确认它在收集布局阶段被调用（约 [L154](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/layout_inference.cc#L154)）。

**观察现象**：`Lower` 与 `InferLayout` 分属两个不同 pass，彼此不直接调用。这意味着你的算子实现里不能假设「`InferLayout` 一定先于 `Lower` 跑过」——事实上 `Lower` 里要用 fragment 布局时，是直接从 `lower_args.layout_map` 里读已经写好的布局（由 `LayoutInference` pass 提前注入），而不是临时推断。

**预期结果**：能在源码中画出「基类声明 → pass 调用点」的对应关系。无需运行命令。

#### 4.1.5 小练习与答案

**练习 1**：`TileOperatorNode` 的三个纯虚方法里，哪一个「可以」不返回任何有效结果？为什么？

> **答案**：`InferLayout()` 可以返回空的 `LayoutMap`（即「我对 fragment 布局没有要求」）。`Lower()` 必须返回有效的 `Stmt`（哪怕是一条空 `Evaluate`），否则算子没有产物；`Clone()` 必须返回有效副本。

**练习 2**：为什么 `Lower()` 和 `InferLayout()` 的参数要包成 `LowerArgs`/`LayoutInferArgs` 结构体，而不是直接列一长串参数？

> **答案**：因为这些上下文字段会随编译器演进不断扩充（例如后来加的 `mbar_phase_expr`、`require_smem_alignment` 回调）。用结构体增删字段不会破坏已有算子的函数签名，保证扩展时向后兼容。

---

### 4.2 算子注册：从 TIR Call 到 TileOperator

#### 4.2.1 概念说明

光定义一个 `XxxNode` 类还不够——编译器遇到 `tl.tileop.xxx(...)` 这条 TIR Call 时，得有人告诉它「这条 Call 对应的算子类是什么、怎么从 `args` 和 `annotations` 构造它」。这就是**算子注册**要解决的事。

tilelang 用两张表把这件事串起来：

- **`TLOpBuilder` 属性表**：以 `Op`（算子名句柄）为 key，存一个「构造函数」`OpBuilderFunc`，签名是 `(Array<PrimExpr> args, Map<String, ObjectRef> annotations) -> TileOperator`。
- **`ParseOperator`**：给一条 `Call`，去这张表里查构造函数并调用，得到 `TileOperator` 对象。

而往 `TLOpBuilder` 表里登记「构造函数」的最简方式，是用一个宏：`TIR_REGISTER_TL_TILE_OP`。

#### 4.2.2 核心流程

注册与解析的配合关系：

```text
              ┌─ TIR_REGISTER_TL_TILE_OP(Copy, copy) ──────────────┐
编译期注册：  │   1. 声明 Copy::Get() 返回 Op("tl.tileop.copy")      │
              │   2. TVM_REGISTER_OP("tl.tileop.copy")               │
              │      .set_attr<TLOpBuilder>([](args, ann){           │
              │           return Copy(args, ann);  ← 调用构造函数     │
              │      })                                              │
              └──────────────────────────────────────────────────────┘
                              │ 表里登记了 "tl.tileop.copy" → 构造函数
                              ▼
运行期解析： ParseOperator(call)
                op = call->op.as<Op>()            // "tl.tileop.copy"
                builder = TLOpBuilder[op]         // 取出 lambda
                return builder(call->args, call->annotations)  // → Copy 对象
```

注意：**一个算子名（`tl.tileop.xxx`）对应一个 node 类的构造函数**。但多个 DSL 入口可以复用同一个 node 类——例如 `async_copy`、`tma_copy`、`maca_async_copy` 都构造 `Copy`，只是往 `annotations` 里塞了不同的标记（`is_async_copy`/`is_tma_copy`），让 `Lower()` 走不同分支。

#### 4.2.3 源码精读

**注册宏** `TIR_REGISTER_TL_TILE_OP` 定义在 [src/op/operator.h:190-202](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/operator.h#L190-L202)：

```cpp
#define TIR_REGISTER_TL_TILE_OP(Entry, OpName)                                 \
  const Op &Entry::Get() {                                                     \
    static const Op &op = Op::Get("tl.tileop." #OpName);                       \
    return op;                                                                 \
  }                                                                            \
  TVM_REGISTER_OP("tl.tileop." #OpName)                                        \
      .set_attr<tirx::TScriptPrinterName>("TScriptPrinterName", #OpName)       \
      .set_attr<OpBuilderFunc>(                                                \
          "TLOpBuilder",                                                       \
          [](ffi::Array<PrimExpr> args,                                        \
             ffi::Map<ffi::String, ffi::ObjectRef> annotations) {              \
            return Entry(args, annotations);                                   \
          })
```

这个宏干两件事：（1）给算子类生成 `Get()`，返回 `Op("tl.tileop.<OpName>")` 句柄；（2）在 TVM 的 Op 注册表里登记 `TLOpBuilder`，它就是一个调用 `Entry(args, annotations)` 构造 node 的 lambda。

**copy 的注册**——最干净的例子，在 [src/op/copy.cc:575-578](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/copy.cc#L575-L578)：

```cpp
TIR_REGISTER_TL_TILE_OP(Copy, copy)
    .set_num_inputs(5)
    .set_attr<TCallEffectKind>("TCallEffectKind",
                               Integer(CallEffectKind::kOpaque));
```

**复用同一 node 的多入口**——`async_copy`/`tma_copy`/`maca_async_copy` 不用宏，而是直接 `TVM_REGISTER_OP` 写自定义 builder，在构造前给 `annotations` 打标记。见 [src/op/copy.cc:596-608](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/copy.cc#L596-L608)（tma_copy）：

```cpp
TVM_REGISTER_OP("tl.tileop.tma_copy")
    .set_attr<TScriptPrinterName>("TScriptPrinterName", "tma_copy")
    .set_attr<OpBuilderFunc>("TLOpBuilder",
                             [](Array<PrimExpr> args, Map<String, ObjectRef> annotations) {
                               Map<String, ObjectRef> ann = annotations;
                               ann.Set("is_tma_copy", IntImm(DataType::Int(32), 1));
                               return Copy(args, ann);
                             })
    ...
```

**构造函数**——以 copy 为例，它把 `args` 里的两个 buffer region 解析成 `src`/`dst`，见 [src/op/copy.cc:270-291](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/copy.cc#L270-L291)：

```cpp
Copy::Copy(Array<PrimExpr> args, Map<String, ObjectRef> annotations) {
  ObjectPtr<CopyNode> node = make_object<CopyNode>();
  auto src_access = NormalizeToAccessRegion(args[0], kAccessRead);
  auto dst_access = NormalizeToAccessRegion(args[1], kAccessWrite);
  node->src = src_access.region->buffer;
  node->dst = dst_access.region->buffer;
  node->src_range = src_access.region->region;
  node->dst_range = dst_access.region->region;
  node->SetAccessRegions({src_access, dst_access});   // 喂给 GetAccessRegions
  node->annotations = annotations;
  ...
  data_ = std::move(node);
}
```

**解析入口** `ParseOperator` 在 [src/op/operator.cc:34-43](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/operator.cc#L34-L43)：

```cpp
TileOperator ParseOperator(Call call) {
  auto op_map = Op::GetAttrMap<OpBuilderFunc>("TLOpBuilder");
  Op op = call->op.as<Op>().value();
  if (op_map.count(op)) {
    auto tile_op = op_map[op](call->args, call->annotations);
    ICHECK(tile_op.defined());
    return tile_op;
  }
  return TileOperator();   // 没注册过 → 返回空
}
```

而 `LowerTileOp` pass 正是在遇到 `Evaluate(Call(...))` 语句时调用它（[src/transform/lower_tile_op.cc:1143-1151](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/lower_tile_op.cc#L1143-L1151)）：

```cpp
Stmt VisitStmt_(const EvaluateNode *op) final {
  const CallNode *call = op->value.as<CallNode>();
  if (call && call->op.as<GlobalVarNode>())        // 全局函数调用不处理
    return Downcast<Evaluate>(IRMutatorWithAnalyzer::VisitStmt_(op));
  auto tile_op = ParseOperator(GetRef<Stmt>(op));
  if (!tile_op.defined())                          // 不是 tile 算子 → 交给基类
    return IRMutatorWithAnalyzer::VisitStmt_(op);
  ...
  auto lowered = tile_op->Lower(lower_args, analyzer_);   // 见 4.3
  ...
}
```

#### 4.2.4 代码实践

**目标**：弄清「一个算子名 → 一个构造函数」的对应，并理解多入口复用。

**步骤**：

1. 在 [src/op/copy.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/copy.cc) 搜索 `TVM_REGISTER_OP("tl.tileop.` 与 `TIR_REGISTER_TL_TILE_OP`，列出所有以 `tl.tileop.` 开头的算子名，以及它们各自构造出哪种 node。
2. 对比 `tl.tileop.copy`（用宏）与 `tl.tileop.tma_copy`（手写 builder）两种写法。
3. 检查 [CMakeLists.txt](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/CMakeLists.txt) 中 `src/op/*.cc` 这条 glob（[L374](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/CMakeLists.txt#L374)）。

**观察现象**：`copy`、`async_copy`、`tma_copy`、`maca_async_copy` 四个算子名都构造 `Copy` 对象，但各自往 `annotations` 注入不同的标记位。新加的 `.cc` 文件因为 glob 自动纳入编译，无需手改 CMake。

**预期结果**：得到一张「算子名 → node 类 → 标记位」对照表。

| 算子名 | node 类 | 区分标记 |
| --- | --- | --- |
| `tl.tileop.copy` | `CopyNode` | 无（普通同步拷贝） |
| `tl.tileop.async_copy` | `CopyNode` | `is_async_copy=1` |
| `tl.tileop.tma_copy` | `CopyNode` | `is_tma_copy=1` |
| `tl.tileop.maca_async_copy` | `CopyNode` | `is_async_copy=1` |

#### 4.2.5 小练习与答案

**练习 1**：如果新增算子时忘记调用 `TIR_REGISTER_TL_TILE_OP` 或 `TVM_REGISTER_OP`，会发生什么？

> **答案**：`ParseOperator` 在 `TLOpBuilder` 表里查不到该 `Op`，`op_map.count(op)` 为假，返回空的 `TileOperator()`。`LowerTileOp` 里 `tile_op.defined()` 为假，于是这条 `Evaluate` 被当作普通语句交给基类 visitor——结果通常是这条算子调用**原封不动地残留到最终 IR 里**，在 codegen 阶段报「未知算子」之类的错。

**练习 2**：为什么 `maca_async_copy` 复用 `Copy` node 而不是新建一个 `MacaAsyncCopyNode`？

> **答案**：它的语义仍是「把一块 tile 从 src 搬到 dst」，与 copy 同构，只是底层指令不同（走 MACA 的 `memcpy_async`）。复用 node 让 layout 推断、访问区域分析等公共逻辑只写一遍，差异通过 `annotations` 标记 + `Lower()` 内部分支处理。这是「同构算子复用、用注解区分变体」的常见手法。

---

### 4.3 Lower()：把算子降级成底层 TIR

#### 4.3.1 概念说明

`Lower()` 是算子最核心的方法：**把「我想做这块 tile 的搬运/计算」翻译成「一段由循环、load/store、可能还有张量核指令组成的底层 TIR」**。它的签名是：

```cpp
Stmt Lower(const LowerArgs &lower_args, arith::Analyzer *analyzer) const;
```

返回的 `Stmt` 会被 `LowerTileOp` pass 替换回原算子调用所在的位置。

tilelang 里有**两种典型降级风格**：

1. **自包含 SIMT 降级**（copy、transpose）：在 C++ 里直接生成一组「按线程并行的嵌套循环」，每个线程搬/算若干元素。逻辑清晰、与后端无关的部分写在公共层，后端差异通过「实现注册表」分派。
2. **外包给 Python**（gemm）：C++ 的 `Lower()` 极薄，只是调一个 Python 全局函数 `tl.gemm.lower`，让真正复杂的指令选择与 fragment 构造在 Python 侧完成（见 u4-l2）。

新增算子时，**优先选风格 1**——只有当降级逻辑极其复杂（如涉及张量核发射器、descriptor 构造）时才考虑风格 2。

#### 4.3.2 核心流程

以 transpose（最简 SIMT 算子）为例，自包含降级的标准三步：

```text
1. MakeSIMTLoop()   生成按维度的 Parallel 嵌套循环
                     每个 loop var 对应一个 extent>1 的维度
                     body = BufferStore(dst, load(src), indices)
2. ResolveXxxImpl(target)  按后端选「实现」
3. impl.lower(...)   把 SIMT 循环按 layout 向量化、加边界守卫
```

其中「按后端选实现」用的是一张注册表：每个后端把自己的 `lower` 函数指针登记进去，`ResolveXxxImpl` 按 `match_target` 挑出匹配的实现。这让**算子本体与后端实现解耦**——新增一个后端只需登记一条 `RegisterXxxImpl`，不必改算子本体。

#### 4.3.3 源码精读

**transpose 的 `Lower`**——只有一行，把活儿交给按 target 解析出的实现，见 [src/op/transpose.cc:207-211](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/transpose.cc#L207-L211)：

```cpp
Stmt TransposeNode::Lower(const LowerArgs &lower_args,
                          arith::Analyzer *analyzer) const {
  return ResolveTransposeImpl(lower_args.target)
      .lower(*this, lower_args, analyzer);
}
```

**SIMT 循环的构造**——`MakeSIMTLoop` 是自包含降级的灵魂，[src/op/transpose.cc:174-205](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/transpose.cc#L174-L205)。它为每个 `extent>1` 的维度建一个 `Var`，组装出 src/dst 的下标表达式，再套上 `For(..., ForKind::kParallel, body)`：

```cpp
For TransposeNode::MakeSIMTLoop(arith::Analyzer *analyzer) const {
  Array<IterVar> loop_vars = MakeIterVars();
  ...
  Array<PrimExpr> src_indices = MakeIndices(loop_vars, 0);
  Array<PrimExpr> dst_indices = MakeIndices(loop_vars, 1);  // 关键：dst 下标反转
  ...
  PrimExpr value = BufferLoad(src, src_indices);
  Stmt body = BufferStore(dst, value, dst_indices);
  ...
  for (int i = loop_vars.size() - 1; i >= 0; i--) {
    body = For(loop_vars[i]->var, 0, loop_vars[i]->dom->extent,
               ForKind::kParallel, body);
  }
  return Downcast<For>(body);
}
```

> transpose 与 copy 的唯一本质差异就在 `MakeIndices(..., 1)`：dst 侧把非平凡维度的循环变量**反向映射**（[src/op/transpose.cc:92-134](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/transpose.cc#L92-L134)），从而实现 `dst[j,i] = src[i,j]`。这是「同一套 SIMT 框架，只改下标映射」的最佳范例。

**实现注册表**——`ResolveTransposeImpl` 从静态 vector 里按 target 挑实现，[src/op/transpose.cc:32-49](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/transpose.cc#L32-L49)：

```cpp
const TransposeImpl &ResolveTransposeImpl(Target target) {
  const auto &registry = TransposeImplRegistry();
  const TransposeImpl *matched_impl = nullptr;
  for (const TransposeImpl &impl : registry) {
    if (impl.match_target(target)) { ... matched_impl = &impl; }
  }
  ICHECK(matched_impl != nullptr) << "... no transpose implementation ...";
  return *matched_impl;
}
```

**单后端登记**——以 Metal 为例，[src/metal/op/transpose.cc:62-71](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/metal/op/transpose.cc#L62-L71) 用一个静态 bool 触发登记：

```cpp
bool RegisterMetalTranspose() {
  RegisterTransposeImpl(TransposeImpl{
      "metal.Transpose",
      MatchMetalTransposeTarget,      // 谓词：TargetIsMetal(target)
      metal::Transpose::Lower,        // 函数指针
  });
  return true;
}
const bool metal_transpose_registered = RegisterMetalTranspose();
```

**copy 的对照**——copy 的 `Lower` 同样是一行分派（[src/op/copy.cc:532-535](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/copy.cc#L532-L535)），但它有更丰富的「能力」：copy 的实现注册表 `CopyImpl` 带 `priority` 字段（[src/op/copy.cc:92-107](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/copy.cc#L92-L107)），允许多个实现同时 match 同一 target 时取优先级最高者（比如「TMA 加速版」优先于「普通 SIMT 版」）。transpose 不需要这种能力，所以没有 priority。

**gemm 的对照（风格 2）**——C++ `Lower` 调 Python 全局函数，[src/op/gemm.cc:178-200](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/gemm.cc#L178-L200)：

```cpp
Stmt GemmNode::Lower(const LowerArgs &lower_args, arith::Analyzer *analyzer) const {
  if (const auto f = Function::GetGlobal("tl.gemm.lower")) {
    ...
    auto prim_func = Downcast<PrimFunc>(
        (*f)(GetRef<Gemm>(this), lower_args.layout_map, lower_args.target,
             lower_args.thread_bounds, lower_args.thread_var, mbar_phase));
    ...
  }
}
```

> 选择哪种风格的经验法则：**能用 SIMT 循环表达的算子（elementwise、copy、transpose、reduce）走风格 1**；**需要发射张量核指令 / 构造 descriptor 的算子（gemm、wgmma）走风格 2**，把复杂度留给 Python 侧的发射器（见 u6）。

#### 4.3.4 代码实践

**目标**：对比两种降级风格，理解 transpose 的 dst 下标反转。

**步骤**：

1. 打开 [src/op/transpose.cc:92-134](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/transpose.cc#L92-L134)，在 `MakeIndices` 的 `src_dst == 1` 分支里找到 `size_t rev = N - 1 - nt_idx;` 这行，理解它如何把第 k 个循环变量映射到 dst 的第 (N-1-k) 个非平凡维度。
2. 对比 copy 的 `MakeIndices`（[src/op/copy.cc:399-416](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/copy.cc#L399-L416)），确认 copy 的 src 与 dst 用的是**同一套**顺序映射（不反转）。
3. 打开 [src/op/gemm.cc:178-200](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/gemm.cc#L178-L200)，确认 gemm 的 `Lower` 没有 `MakeSIMTLoop`，而是调 Python。

**观察现象**：transpose 与 copy 的 `MakeSIMTLoop` 几乎一模一样，唯一区别在 dst 下标是否反转。这说明**「新增一个与 copy 同构的算子」成本极低**——复制 copy 的骨架，改一处下标映射即可。

**预期结果**：能口述「transpose = copy 的骨架 + dst 维度反转」。无需运行命令（属源码阅读型实践）。

#### 4.3.5 小练习与答案

**练习 1**：copy 的 `CopyImpl` 注册表有 `priority` 字段，而 transpose 的 `TransposeImpl` 没有。请设想一个需要 priority 的 transpose 场景。

> **答案**：假如某后端为 transpose 提供了「硬件转置指令」（如某些 GPU 的 `prmt` 或专用 lane permute），可登记一个高优先级的 `TransposeImpl`，让它在 match 同一 target 时胜过普通 SIMT 版。当前 transpose 没有这种硬件加速路径，所以不需要 priority。

**练习 2**：`Lower()` 是 `const` 方法，但它返回的 `Stmt` 里可能含需要申请的 shared workspace。这是怎么做到的？

> **答案**：workspace 申请通过 `LowerArgs` 里下发的**回调**完成（`add_workspace`、`alloc_mbarrier` 等）。`Lower()` 不直接持有可变状态，而是调用回调让外层 pass 代为分配并把结果回填——这样算子对象本身保持无状态，符合「可被多 pass 复制重读」的约束。

---

### 4.4 InferLayout()：为 fragment 推断寄存器布局

#### 4.4.1 概念说明

`InferLayout()` 回答的问题是：**这个算子用到的 fragment buffer，其逻辑坐标该如何分配给线程？** 它返回一个 `LayoutMap`（`Map<Buffer, Layout>`），告诉 `LayoutInference` pass「这个 buffer 该绑哪种 fragment 布局」。

回顾 u4-l3：fragment（`local.fragment`）的「逻辑下标」与「物理寄存器」不一一对应，需要 layout 把 tile 切分给各线程。只有**会消费或生产 fragment 的算子**才需要给 fragment 返回非空布局——典型是 `T.gemm`（它的累加器 C 是 fragment）。

而对于**只在 shared/global 之间搬运、或纯 elementwise 的算子**（copy、transpose），它们不直接定义 fragment 布局，`InferLayout()` 直接返回空 `{}`，把布局推断让给 `LayoutInference` pass 自己从生成的 SIMT 循环反推（通过 `InferSIMTLayout`）。

`InferLayout` 带一个 `InferLevel` 参数（`kFree`/`kCommon`/`kStrict`），表示本轮推断的「严格度」——pass 会按 strict→common→free 的优先级多次调用，让算子在不同严格度下逐步给出布局（详见 u4-l3 的优先级传播机制）。新增简单算子时，直接忽略 `level`、返回固定结果即可。

#### 4.4.2 核心流程

两类算子的 `InferLayout` 行为对比：

```text
【SIMT 算子（transpose/copy）】
InferLayout() → 返回空 {}
    （布局由 LayoutInference 从 SIMT 循环自动反推，算子不插手）

【张量核算子（gemm）】
InferLayout() → 调 Python tl.gemm.infer_layout
    → 返回 {C: Fragment(...), A: swizzle布局, B: swizzle布局}
    （算子主动声明 fragment 累加器的 lane 映射）
```

#### 4.4.3 源码精读

**transpose 的 `InferLayout`**——最简形式，直接返回空，[src/op/transpose.cc:213-217](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/transpose.cc#L213-L217)：

```cpp
LayoutMap TransposeNode::InferLayout(const LayoutInferArgs &layout_args,
                                     InferLevel level) const {
  // Transpose always uses SIMT loops; no special layout inference needed.
  return {};
}
```

**copy 的 `InferLayout`**——把活儿外包给按 target 解析的实现（与 `Lower` 对称），[src/op/copy.cc:517-520](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/copy.cc#L517-L520)：

```cpp
LayoutMap CopyNode::InferLayout(const LayoutInferArgs &layout_args,
                                InferLevel level) const {
  return InferCopyLayout(*this, layout_args, level);
}
```

`InferCopyLayout` 再调 `ResolveCopyImpl(...).infer_layout(...)`（[src/op/copy.cc:109-114](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/copy.cc#L109-L114)）。默认的 SIMT 实现走 `InferSIMTLayout`——它构造出 SIMT 循环后，让 `ParallelOp` 自己推断布局（[src/op/copy.cc:522-529](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/copy.cc#L522-L529)）：

```cpp
LayoutMap CopyNode::InferSIMTLayout(const LayoutInferArgs &layout_args,
                                    InferLevel level) const {
  if (!par_op_.defined()) {
    arith::Analyzer analyzer;
    par_op_ = ParallelOp(MakeSIMTLoop(&analyzer));
  }
  return par_op_->InferLayout(layout_args, level);
}
```

**gemm 的 `InferLayout`**——主动声明 fragment 布局并外包给 Python，[src/op/gemm.cc:223-260](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/gemm.cc#L223-L260)：

```cpp
LayoutMap GemmNode::InferLayout(const LayoutInferArgs &layout_args,
                                InferLevel level) const {
  if (completed_) return {};
  LayoutMap results;
  if (const auto f = Function::GetGlobal("tl.gemm.infer_layout")) {
    auto inferred_layouts = Downcast<LayoutMap>((*f)(
        GetRef<Gemm>(this), layout_args.target, layout_args.thread_bounds));
    ...
    for (auto kv : inferred_layouts) {
      if (auto frag = layout.as<Fragment>()) {
        results.Set(buf, frag.value()->BindThreadRange(layout_args.thread_bounds));
      } ...
    }
  }
  ...
}
```

注意 gemm 把 fragment 用 `BindThreadRange(thread_bounds)` 绑定到当前线程范围——这是 fragment 布局「落池」前的标准处理（与 u6-l1 讲的 `make_mma_load_layout` 产出的布局一致）。

**调用点**——`LayoutInference` pass 在收集阶段调用每个算子的 `InferLayout`，[src/transform/layout_inference.cc:154-162](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/layout_inference.cc#L154-L162)：

```cpp
auto updates = next->InferLayout(LayoutInferArgs{target_, thread_bounds, layout_map,
                                                 cur_analyzer, buffer_oob, {},
                                                 bind_var_to_expr_, false},
                                 level);
```

#### 4.4.4 代码实践

**目标**：判断一个新算子该返回空布局还是非空布局。

**步骤**：

1. 在 [src/op/transpose.cc:213-217](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/transpose.cc#L213-L217) 与 [src/op/gemm.cc:223-260](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/gemm.cc#L223-L260) 之间做对照。
2. 思考：一个「把 fragment 累加器写回 shared」的算子，`InferLayout` 该返回什么？
3. 思考：一个「在 shared 上做 elementwise ReLU」的算子，`InferLayout` 该返回什么？

**预期结果（推理）**：

- 「fragment → shared」的 store：若它**消费**一个已推断好布局的 fragment，则 `InferLayout` 可返回空（布局由上游 gemm 已定）；若它**自己定义** fragment 布局，则需返回该布局。
- 「shared 上 ReLU」：不涉及 fragment，返回空 `{}`，由 SIMT 循环自动反推。

> 待本地验证：上述推理可通过写一个最小 kernel、开启 layout 可视化（见 u9-l3）观察 fragment 布局来源来确认。

#### 4.4.5 小练习与答案

**练习 1**：`InferLayout` 的 `level` 参数在 transpose 里被忽略了。这样做安全吗？

> **答案**：对 transpose 安全。因为它返回空 `{}`，相当于「我对任何严格度都没有布局建议」，把决策权完全交给 `LayoutInference` pass 的 SIMT 反推。`level` 只对**主动声明布局**的算子（如 gemm）有意义——它们可能在不同严格度下给出不同布局（strict 给精确布局、free 给宽松候选）。

**练习 2**：为什么 copy 既要在 `Lower` 里调 `ResolveCopyImpl`，又要在 `InferLayout` 里调一次？

> **答案**：因为 `Lower` 与 `InferLayout` 是**两个独立 pass**，分别需要按 target 选实现——一个选「怎么降级成 Stmt」，一个选「怎么推断布局」。两者可能由不同后端实现提供（例如某后端有 TMA 加速的 `lower`，但布局推断仍用默认 SIMT 版），所以分别解析。

---

### 4.5 DSL 暴露：在 Python 侧提供 T.xxx 入口

#### 4.5.1 概念说明

到目前为止，我们都在 C++ 侧打转——但用户写的是 Python：`T.copy(A, B)`。最后一步，是提供一个 Python 函数，**把用户的语义化参数翻译成一条 `tl.tileop.xxx` 的 TIR Call**。这就是「DSL 暴露」。

这个 Python 函数通常做三件事：

1. **规整参数**：把多种入参形式（`Buffer` / `BufferRegion` / `BufferLoad`）统一成标准的 buffer region 与 extent。
2. **打包 annotations**：把关键字参数（如 `coalesced_width`、`prefer_instruction`）翻译成 `Map<String, ObjectRef>` 注解。
3. **生成 TIR Call**：用 `tirx.call_intrin(...)` 产出对 `tl.tileop.xxx` 的调用——注意这个 `xxx` 必须与 C++ 侧 `TIR_REGISTER_TL_TILE_OP` / `TVM_REGISTER_OP` 里登记的算子名**完全一致**。

最后把这个函数挂到 `tilelang.language` 命名空间，用户就能以 `T.xxx` 调用它。

#### 4.5.2 核心流程

```text
用户: T.transpose(src, dst)
        │
        ▼
tilelang/language/copy_op.py::transpose(src, dst)
   1. get_extent(src/dst) → 推断形状
   2. to_buffer_region(...) → 规整成 region
   3. buffer_region_to_tile_region(...) → 转成 tile region（带 access 类型 r/w）
   4. tirx.call_intrin("handle", Op.get("tl.tileop.transpose"), src, dst)
        │  产出的 TIR Call 落进 kernel body
        ▼
（后续由 4.2 的 ParseOperator + 4.3 的 Lower 接管）
```

#### 4.5.3 源码精读

**transpose 的 Python 入口**——极其简洁，[tilelang/language/copy_op.py:543-577](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/copy_op.py#L543-L577)：

```python
def transpose(src: BufferLikeType, dst: BufferLikeType) -> tirx.PrimExpr:
    """Transpose a 2D buffer in shared memory: dst[j, i] = src[i, j]."""
    src_extent = get_extent(src)
    dst_extent = get_extent(dst)
    ...
    src_region = to_buffer_region(src)
    dst_region = to_buffer_region(dst)
    src = buffer_region_to_tile_region(src_region, "r", list(src_extent))
    dst = buffer_region_to_tile_region(dst_region, "w", list(dst_extent))
    return tirx.call_intrin(
        "handle",
        tirx.op.Op.get("tl.tileop.transpose"),   # ← 必须与 C++ 注册名一致
        src,
        dst,
    )
```

**copy 的 Python 入口**——带 annotations 打包的完整范例，[tilelang/language/copy_op.py:53-133](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/copy_op.py#L53-L133)。关键字参数被翻译进 `ann` 字典，最后随 Call 一起传给 C++ 构造函数：

```python
def copy(src, dst, *, coalesced_width=None, disable_tma=False,
         eviction_policy=None, prefer_instruction=None,
         annotations=None, loop_layout=None):
    src, dst = _normalize_copy_regions(src, dst)
    ...
    ann = annotations.copy() if annotations else {}
    if "coalesced_width" not in ann and coalesced_width is not None:
        ann["coalesced_width"] = coalesced_width
    ...
    return tirx.call_intrin("handle", tirx.op.Op.get("tl.tileop.copy"),
                            src, dst, annotations=ann if ann else None)
```

**挂到 `T` 命名空间**——在 [tilelang/language/\_\_init\_\_.py:60-72](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/__init__.py#L60-L72) 把这些函数 import 进来（`T` 即 `tilelang.language` 模块）：

```python
from .copy_op import (  # noqa: F401
    copy,
    async_copy,
    maca_async_copy,
    tma_copy,
    ...
    transpose,
    im2col,
    ...
)
```

> 三处命名必须对齐：**Python `Op.get("tl.tileop.xxx")` ↔ C++ `TVM_REGISTER_OP("tl.tileop.xxx")` ↔ 宏 `TIR_REGISTER_TL_TILE_OP(Node, xxx)`**。三者不一致时，`ParseOperator` 查不到 builder，算子不会被降级。

#### 4.5.4 代码实践

**目标**：追踪一条 `T.transpose(...)` 从 Python 到 C++ 的完整调用链，确认三处命名对齐。

**步骤**：

1. 在 [tilelang/language/copy_op.py:572-577](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/copy_op.py#L572-L577) 找到 `Op.get("tl.tileop.transpose")`。
2. 在 [src/op/transpose.cc:219](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/transpose.cc#L219) 找到 `TIR_REGISTER_TL_TILE_OP(Transpose, transpose)`，展开宏后等价于 `TVM_REGISTER_OP("tl.tileop.transpose")`。
3. 在 [tilelang/language/\_\_init\_\_.py:68](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/__init__.py#L68) 确认 `transpose` 被 re-export。

**观察现象**：三处的字符串 `transpose` 完全一致。这就是「命名对齐」契约。

**预期结果**：画出 Python → TIR Call → ParseOperator → TransposeNode 的链路图。如本地已装好 tilelang，可写一个调用 `T.transpose` 的最小 kernel，用 `get_kernel_source()` 看降级后的循环（待本地验证）。

#### 4.5.5 小练习与答案

**练习 1**：如果 Python 侧写 `Op.get("tl.tileop.transpose2")`，而 C++ 侧注册的是 `tl.tileop.transpose`，会怎样？

> **答案**：生成的 TIR Call 的 `op` 是 `tl.tileop.transpose2`，`ParseOperator` 在 `TLOpBuilder` 表里查不到它，返回空 `TileOperator`。该调用残留到 codegen，最终报错。**三处命名必须严格对齐**。

**练习 2**：为什么 DSL 入口函数要做 `_normalize_copy_regions` / `to_buffer_region` 这么多规整，而不是直接把参数透传给 C++？

> **答案**：因为用户传入的 `src`/`dst` 可能是裸 `Buffer`、`BufferRegion`、`BufferLoad`、甚至带切片的多种形式，且两侧形状可能需要按尾部对齐。Python 侧做规整可以利用 Python 的灵活表达力（切片、默认值、断言），把干净的 `(region, extent)` 交给 C++ 构造函数，让 C++ 侧只关心一种规范形式。

---

## 5. 综合实践

**任务**：参照 copy / transpose 算子的实现，设计一个全新的 `T.transpose_copy` 算子——它在一次操作中**既做转置又做拷贝**（从 global 的 `A[i,j]` 拷到 shared 的 `B[j,i]`，等价于「转置搬运」），并列出需要在 C++ 与 Python 哪些地方改动。

> 说明：本仓库已有 `T.transpose`（shared→shared 转置）与 `T.copy`（任意 scope 搬运）。`transpose_copy` 是两者的合体：跨 scope + 维度反转。这是个**设计型实践**，重点是走通「新增算子」的全流程，而非追求性能。

### 设计目标与约束

- 算子名：`tl.tileop.transpose_copy`。
- 语义：`dst[j, i] = src[i, j]`，src 与 dst 可在不同 scope（如 global→shared）。
- 复用现有 SIMT 框架，尽量少写新代码。

### 需要改动的文件清单（参考答案）

**C++ 侧（3 个新文件 + 1 个可选后端文件）**：

1. **新建 `src/op/transpose_copy.h`**：仿照 [src/op/transpose.h](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/transpose.h)，定义 `TransposeCopyNode`（继承 `TileOperatorNode`，含 `src`/`dst`/`src_range`/`dst_range`）、`TransposeCopy` 引用类、`TransposeCopyImpl` 结构体。

2. **新建 `src/op/transpose_copy.cc`**：仿照 [src/op/transpose.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/transpose.cc)，实现：
   - 构造函数：用 `NormalizeToAccessRegion` 解析 `args[0]`/`args[1]`（仿 [transpose.cc:60-70](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/transpose.cc#L60-L70)）。
   - `MakeSIMTLoop`：**直接抄 transpose**，但需注意 src/dst 可在不同 scope——可参考 [copy.cc:452-515](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/copy.cc#L452-L515) 里 `MakeIterVars` 的「按 scope 层级选 base range」逻辑（global/shared/fragment 优先级），确保循环域来自较低层级的一方。
   - `Lower`：一行分派 `ResolveTransposeCopyImpl(target).lower(...)`。
   - `InferLayout`：返回空 `{}`（SIMT 算子）。
   - `Clone`：拷贝 node。
   - 注册：`TIR_REGISTER_TL_TILE_OP(TransposeCopy, transpose_copy).set_num_inputs(2)...`。
   - `TVM_FFI_STATIC_INIT_BLOCK { TransposeCopyNode::RegisterReflection(); }`。

3. **新建后端实现**：由于跨 scope（global→shared），最省事的做法是仿照 [src/metal/op/transpose.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/metal/op/transpose.cc) 在各后端目录（`src/cuda/op/`、`src/rocm/op/`、`src/maca/op/`、`src/metal/op/`、`src/cpu/op/`）各写一个 `transpose_copy.cc`，登记 `RegisterTransposeCopyImpl(...)`。或者更简单——在公共层 `src/backend/common/op/` 写一个共享实现，各后端的 `match_target` 谓词都指向它（参考 [src/backend/common/op/transpose.h](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/backend/common/op/transpose.h)）。
   - 实现里调 `op.MakeSIMTLoop()`，再用 `LowerParallelLoop(...)` 做线程划分与向量化（抄 [metal/op/transpose.cc:23-53](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/metal/op/transpose.cc#L23-L53)）。
   - 若 src 在 global、dst 在 shared，可考虑额外登记一个高 `priority` 的异步拷贝实现（仿 copy 的 `CopyImpl.priority`），但这属于优化，非必需。

4. **CMake 无需改动**：[CMakeLists.txt:374](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/CMakeLists.txt#L374) 用 `src/op/*.cc` 与各后端 glob 自动纳入新文件。

**Python 侧（1 处新增 + 1 处 re-export）**：

5. **在 [tilelang/language/copy_op.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/copy_op.py) 新增 `transpose_copy` 函数**：仿 [copy_op.py:543-577](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/copy_op.py#L543-L577) 的 `transpose`，但 `Op.get("tl.tileop.transpose_copy")`：

   ```python
   # 示例代码（非项目原有）
   def transpose_copy(src, dst):
       src_region = to_buffer_region(src)
       dst_region = to_buffer_region(dst)
       src = buffer_region_to_tile_region(src_region, "r", list(get_extent(src)))
       dst = buffer_region_to_tile_region(dst_region, "w", list(get_extent(dst)))
       return tirx.call_intrin(
           "handle", tirx.op.Op.get("tl.tileop.transpose_copy"), src, dst)
   ```

6. **在 [tilelang/language/\_\_init\_\_.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/__init__.py) 的 `from .copy_op import (...)` 块里加上 `transpose_copy`**（[L60-72](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/__init__.py#L60-L72)）。

### 验证方法

- **命名对齐自检**：grep `tl.tileop.transpose_copy`，确认 Python 入口、C++ `TIR_REGISTER_TL_TILE_OP`、宏展开的 `TVM_REGISTER_OP` 三处字符串一致。
- **降级自检**（待本地验证，需重新编译 `libtilelang.so`）：写一个最小 kernel 调用 `T.transpose_copy(A_global, B_shared)`，用 `JITKernel.get_kernel_source()` 查看生成的循环里 dst 下标是否相对 src 反转。
- **数值自检**（待本地验证）：对比 `T.transpose_copy` 与「先 `T.copy` 再 `T.transpose`」两份结果是否数值相等。

> 这个实践的关键不是性能，而是**走通「Python 入口 → TIR Call → TLOpBuilder 构造 → Lower/InferLayout → 后端实现注册」整条链**。一旦链路打通，你就掌握了 TileLang 的算子扩展机制。

## 6. 本讲小结

- **`TileOperatorNode` 是抽象基类**，三个纯虚方法 `Lower()`、`InferLayout()`、`Clone()` 是新增算子必须实现的接口；它们分别由 `LowerTileOp`、`LayoutInference` 两个 pass 调用，参数通过 `LowerArgs`/`LayoutInferArgs` 结构体下发（[operator.h:148-174](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/operator.h#L148-L174)）。
- **算子注册** 用宏 `TIR_REGISTER_TL_TILE_OP(Node, name)` 把 `tl.tileop.<name>` 与构造函数登记进 `TLOpBuilder` 表；`ParseOperator` 在 pass 里查表把 TIR Call 还原成算子对象。同构变体（如 `async_copy`/`tma_copy`）可复用同一 node，靠 `annotations` 标记区分。
- **`Lower()` 有两种风格**：自包含 SIMT 降级（copy/transpose，在 C++ 生成并行循环）与外包 Python（gemm，调 `tl.gemm.lower` 全局函数）。新增算子优先选前者，其骨架是 `MakeSIMTLoop` + 按 target 分派的实现注册表。
- **`InferLayout()` 的取舍**：不涉及 fragment 的 SIMT 算子直接返回空 `{}`（transpose）；消费/生产 fragment 的算子（gemm）需主动声明 `Fragment` 布局并 `BindThreadRange`。
- **DSL 暴露** 是最后一公里：Python 函数用 `tirx.call_intrin(..., Op.get("tl.tileop.xxx"), ...)` 产出 TIR Call，并在 `tilelang/language/__init__.py` 里 re-export 为 `T.xxx`。**Python 算子名、C++ 宏名、`TVM_REGISTER_OP` 三处字符串必须严格对齐**。
- **新增算子的改动清单**：C++ 侧加 `Node` 类（.h/.cc，含构造、Lower、InferLayout、Clone、注册）、按需在各后端目录登记实现、Python 侧加 DSL 入口并 re-export；CMake 因 glob 自动纳入，通常无需改动。

## 7. 下一步学习建议

- **u9-l1（新增目标后端）**：本讲只改「算子」，不改「后端」。若你想让新算子在某后端上用专用指令（如 MACA 的 `memcpy_async`），需要结合 u9-l1 学的后端注册机制，在 `src/<backend>/op/` 下登记带 `priority` 的专用实现——这正是 copy 在 MACA 上走 `maca_async_copy` 的做法。
- **u6 系列（张量核 intrinsics）**：如果你的新算子需要发射张量核指令（不止 SIMT 循环），参考 gemm 的风格 2，把 `Lower`/`InferLayout` 外包给 Python 发射器。u6-l1/u6-l2 详细讲了 `TensorCoreIntrinEmitter` 与 `make_mma_load_layout` 的写法。
- **u4-l3（Layout 推断）**：要写出能主动声明 fragment 布局的 `InferLayout`，需要先吃透 `Fragment` 的 `Forward`/`Inverse`/`forward_thread` 语义。建议结合 `examples/plot_layout` 可视化加深理解。
- **实践延伸**：尝试实现一个更简单的 `T.fill_diag`（沿对角线填充）算子——它只需改 copy 的 `MakeIndices` 让 dst 下标 `i==j`，是巩固本讲流程的最小练习。完成后可对照本仓库的 `src/op/fill.cc` 对应实现（Python 侧见 `tilelang/language/fill_op.py`）验证思路。
