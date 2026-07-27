# 扩展 TileLang：新 op、新 pass 与新后端

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚「新增一个 tile op」需要改动 Python 前端、C++ op 层、Python tileop 层的哪些文件，以及它们用什么「注册点」串起来。
- 说清楚「新增一个 Pass」从 C++ 实现到被 `lower()` 真正执行的完整路径，并能把它插入到某个后端的 pipeline 正确位置。
- 说清楚「移植一个新后端」需要补齐的 5 类注册点（language / pipeline / tileop / codegen / execution_backend），并以最瘦的 CPU 后端为参照。
- 独立设计一个自定义 tile op 或自定义 Pass 的接入方案（到接口签名级别），写出新增/修改文件清单与注册点。

本讲是整个学习手册的收尾篇，不再讲「怎么用」，而是讲「怎么改」——把前面 u6（Pass 与 codegen）、u7（运行时与拆分）建立的链路反向用来做二次开发。

## 2. 前置知识

本讲默认你已经掌握以下概念（否则请先读对应讲义）：

- **tile op 的「占位 → 指令」模型**（u3-l1、u6-l2、u6-l3）：DSL 层的 `T.gemm` 只生成一个 `tl.tileop.gemm` 的 `call_intrin` 占位节点，真正的 MMA/WGMMA 指令在 `LowerTileOp` Pass 里展开。
- **Pass 是 IRModule→IRModule 的纯变换**（u6-l1）：算法在 C++（`src/transform`），Python 门面（`tilelang/transform`）经 `_ffi_api` 转发；Pass 序列写死在各后端 `pipeline.py`，由 `resolve_pipeline(target)` 按 `target.kind.name` 查表。
- **lower 的总流程**（u4-l1）：`tilelang.lower()` → `determine_target` → `PreLowerSemanticCheck` → `resolve_pipeline(target).lower(mod, target)` → 拆分 host/device。
- **后端 = language 方言 + pipeline + tileop 实现 + codegen + execution_backend**（u4-l4、u7-l1）：`tilelang.language` 默认即 CUDA 方言，其它后端用 `tilelang.<backend>.language` 显式引入并标记 `__tilelang_dialect__`。

三个反复出现的统一模式，先记住它们的名字，后面三节都是它们的具体应用：

1. **属性表（Op attribute map）**：用 `Op::GetAttrMap<T>("TLOpBuilder")` 把一个 TIR Op 关联到一个 C++ 构造函数，按 op 名查表分发。
2. **注册表 + 惰性加载 + 谓词匹配**：pipeline、device codegen、execution backend 都用「按 `target.kind.name` 注册、首次使用时 import、`supports_target` 谓词过滤」这一套。
3. **`TVM_FFI_STATIC_INIT_BLOCK` + `refl::GlobalDef().def(...)`**：C++ 把函数挂成全局 FFI 函数（如 `tl.transform.LetInline`），Python 用 `_ffi_api.LetInline()` 调用。

## 3. 本讲源码地图

本讲涉及的关键文件按「扩展方向」分组：

| 方向 | 文件 | 作用 |
| --- | --- | --- |
| 新增 tile op | [src/op/operator.h](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/operator.h) | `TileOperatorNode` 抽象（`Lower`/`InferLayout` 纯虚）、`TIR_REGISTER_TL_TILE_OP` 宏、`OpBuilderFunc` 类型 |
| 新增 tile op | [src/op/operator.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/operator.cc) | `ParseOperator`：在 `LowerTileOp` 里查 `TLOpBuilder` 属性表构造 op |
| 新增 tile op | [src/op/gemm.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc) | GEMM 的完整范例：构造、`Lower`/`InferLayout`、op 注册、两段式 `ResolveGemmImpl` |
| 新增 tile op | [tilelang/language/gemm_op.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/gemm_op.py) | DSL 入口 `T.gemm`：用 `tirx.call_intrin` 生成 `tl.tileop.gemm` 占位 |
| 新增 tile op | [tilelang/tileop/gemm/__init__.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tileop/gemm/__init__.py) | Python 侧 lowering：注册 `tl.gemm.lower` / `tl.gemm.infer_layout` 让 C++ 回调 |
| 新增 tile op | [tilelang/tileop/gemm/registry.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tileop/gemm/registry.py) | Python 注册表：`register_gemm_impl` / `resolve_gemm_impl` |
| 新增 Pass | [src/transform/frontend_legalize.cc](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/frontend_legalize.cc) | 最简 Pass 范例 `LetInline`：`CreatePrimFuncPass` + FFI 注册 |
| 新增 Pass | [tilelang/transform/simplify.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/simplify.py) | Python 门面：`_ffi_api.LetInline()` 薄包装 |
| 新增 Pass | [tilelang/backend/pass_pipeline/pipeline.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/pass_pipeline/pipeline.py) | `PassPipeline` / `register_pipeline` / `resolve_pipeline` |
| 新增 Pass | [tilelang/cuda/pipeline.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/pipeline.py) / [tilelang/cpu/pipeline.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cpu/pipeline.py) | Pass 序列写死处：在 body 里按顺序调用各 Pass |
| 移植后端 | [tilelang/cpu/language/__init__.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cpu/language/__init__.py) | 最瘦后端的 language 方言（仅复用 common + 标记 dialect） |
| 移植后端 | [tilelang/cpu/op/gemm/gemm_scalar.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cpu/op/gemm/gemm_scalar.py) | CPU 的 GEMM 实现（三重循环标量版） |
| 移植后端 | [tilelang/cpu/op/gemm/__init__.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cpu/op/gemm/__init__.py) | 用 `register_gemm_impl` 把标量版注册给 c/llvm |
| 移植后端 | [tilelang/cpu/codegen.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cpu/codegen.py) / [tilelang/cpu/execution_backend.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cpu/execution_backend.py) | 注册 device/host codegen 与 execution_backend |

> 说明：本讲引用的 `src/transform`、`src/op` 是 C++ 子系统，`tilelang.backend.pass_pipeline`、`tilelang.cpu.language` 是 Python 子系统——这正是 tilelang「前后端镜像」的体现（见 u1-l3）。

## 4. 核心概念与源码讲解

### 4.1 新增一个 tile op：从 `T.gemm` 看四层协作

#### 4.1.1 概念说明

一个 tile op（如 `T.gemm`、`T.copy`、`T.reduce_max`）的生命周期横跨四层，扩展时必须**四层都改到、且用同一个 op 名串起来**：

1. **Python language 层**：用户写的 DSL 入口 `T.xxx`，职责是把用户参数序列化成一个 `tl.tileop.xxx` 的 `call_intrin` 占位节点（**只留占位、不展开**）。
2. **C++ op 层**：注册 `tl.tileop.xxx` 这个 TIR Op，并绑定一个「构造函数」（`TLOpBuilder`），把序列化的参数反序列化成一个 `TileOperatorNode` 子类对象。
3. **Python tileop 层**：实现 op 真正的 `lower`（展开成底层 intrinsic）与 `infer_layout`（推导缓冲布局），通过全局 FFI 函数让 C++ 回调。
4. **（多后端时）注册表层**：如果该 op 在不同硬件上实现不同（如 GEMM），再加一层 `(inst_name, predicate)` 注册表按 target 分发。

关键点：C++ op 层只负责「把占位节点认出来并构造对象」，**不写指令**；真正的指令发射在 Python tileop 层（GEMM）或 C++ 函数指针表（Copy）。这是 tilelang「DSL 留占位、后按硬件展开」核心模型在扩展接口上的直接体现。

#### 4.1.2 核心流程

新增 tile op `T.myop` 的流程：

```text
用户写 T.myop(A, B, C)
   │  (Python language 层)
   ▼
tirx.call_intrin("handle", Op.get("tl.tileop.myop"), A, B, C, ...)   # 生成占位节点
   │  ……经过若干 Pass……
   ▼
LowerTileOp Pass 遇到 Evaluate(Call("tl.tileop.myop"))
   │  调用 ParseOperator(call)                                      # C++ op 层
   ▼
Op::GetAttrMap<OpBuilderFunc>("TLOpBuilder")[op]                    # 查属性表
   │  构造 MyOpNode(args, annotations)
   ▼
MyOpNode::Lower(lower_args)                                         # 决定指令
   │  通常回调 Python: Function::GetGlobal("tl.myop.lower")
   ▼
展开成底层 intrinsic（mma_sync / cp.async / 普通循环……）
```

其中第 3、4 步是 C++ 与 Python 的**跨语言回调**：C++ 的 `GemmNode::Lower` 调用 Python 注册的 `tl.gemm.lower`，由 Python 根据注册表挑实现类发射指令。

#### 4.1.3 源码精读

**(a) Python language 层：生成占位节点**

`T.gemm` 把所有参数序列化后，用 `tirx.call_intrin` 生成一个 `tl.tileop.gemm` 占位——注意它返回的是 `tirx.PrimExpr`，不是任何实际计算：

```python
# tilelang/language/gemm_op.py（节选）
return tirx.call_intrin(
    "handle",
    tirx.op.Op.get("tl.tileop.gemm"),   # ← op 名，四层靠它串联
    A_arg, B_arg, C_arg,
    transpose_A, transpose_B, M, N, K, policy, clear_accum,
    stride_a, stride_b, offset_a, offset_b, k_pack, wg_wait,
    mbar_arg, C_coords[0], C_coords[1],
    annotations=annotations,
)
```

见 [tilelang/language/gemm_op.py:L123-L146](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/gemm_op.py#L123-L146)，`gemm()` 入口在 [tilelang/language/gemm_op.py:L149-L198](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/gemm_op.py#L149-L198)。参数顺序在 C++ 构造函数里逐个按下标反序列化，**两端必须严格对齐**。

**(b) C++ op 层：抽象基类 + 注册宏**

每个 tile op 都是一个 `TileOperatorNode` 子类，必须实现两个纯虚钩子：

```cpp
// src/op/operator.h（节选）
class TileOperatorNode : public ffi::Object {
public:
  virtual tirx::Stmt Lower(const LowerArgs &lower_args,
                           arith::Analyzer *analyzer) const = 0;     // 展开成指令
  virtual LayoutMap InferLayout(const LayoutInferArgs &layout_args,
                                InferLevel level) const = 0;          // 推导布局
  virtual TileOperator Clone() const = 0;
  ...
};
```

见 [src/op/operator.h:L161-L187](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/operator.h#L161-L187)。`Lower` 与 `InferLayout` 这两个钩子贯穿 `LayoutInference` 与 `LowerTileOp` 两个 Pass（见 u6-l2、u6-l3）。

注册一个新 op 只需一个宏，它展开成三件事：定义 `Entry::Get()` 返回该 Op、`TVM_REGISTER_OP("tl.tileop.<name>")` 注册 Op、把构造函数绑到 `TLOpBuilder` 属性：

```cpp
// src/op/operator.h（宏定义节选）
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

见 [src/op/operator.h:L212-L224](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/operator.h#L212-L224)。`OpBuilderFunc` 的类型签名在 [src/op/operator.h:L204-L205](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/operator.h#L204-L205)，即「接收序列化参数数组 + 注解，返回一个 `TileOperator`」。

**(c) C++ 解析：查属性表构造 op**

`LowerTileOp` Pass 内部用 `ParseOperator` 把占位节点认出来——本质就是查 `TLOpBuilder` 属性表：

```cpp
// src/op/operator.cc（节选）
TileOperator ParseOperator(const Call &call, const BlockAnnotations &block_annotations) {
  auto op_map = Op::GetAttrMap<OpBuilderFunc>("TLOpBuilder");   // ← 属性表
  Op op = call->op.as<Op>().value();
  if (op_map.count(op)) {
    auto tile_op = op_map[op](call->args, call->annotations);  // 调用注册的构造函数
    ICHECK(tile_op.defined());
    ...
    return tile_op;
  }
  return TileOperator();   // 未注册 → 空 op
}
```

见 [src/op/operator.cc:L37-L53](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/operator.cc#L37-L53)。这就是「属性表」分发模式：注册宏往表里写，`ParseOperator` 从表里读。

**(d) C++ 构造函数：参数反序列化**

`Gemm` 构造函数把 language 层序列化的参数数组按下标一一还原成缓冲、形状、转置标志等。注意它还读 `annotations` 里的 `is_wgmma` / `is_tcgen05` 来区分同一 IR 节点的不同变体：

```cpp
// src/op/gemm.cc（节选）
Gemm::Gemm(Array<PrimExpr> args, Map<String, ObjectRef> annotations) {
  ...
  node->aRegion_ = NormalizeToAccessRegion(args[0], kAccessRead);
  node->bRegion_ = NormalizeToAccessRegion(args[1], kAccessRead);
  node->cRegion_ = NormalizeToAccessRegion(args[2], kAccessReadWrite);
  node->transA_ = args[3].as<Bool>().value();
  ...
  node->m_ = args[5].as<IntImm>().value()->value;
  if (auto val = annotations.Get("is_wgmma")) { ... node->isWgmma_ = ...; }
  ...
}
```

见 [src/op/gemm.cc:L81-L142](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc#L81-L142)。

注册 + FFI 导出在文件末尾：`TIR_REGISTER_TL_TILE_OP(Gemm, gemm)` 注册主 op（[src/op/gemm.cc:L261-L264](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc#L261-L264)），`wgmma_gemm`/`tcgen05_gemm` 用 `TVM_REGISTER_OP` 手写并注入 `is_wgmma`/`is_tcgen05` 注解复用同一个 `Gemm` 构造函数（[src/op/gemm.cc:L266-L292](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc#L266-L292)），最后用 `TVM_FFI_STATIC_INIT_BLOCK` 导出两个辅助 FFI 函数（[src/op/gemm.cc:L297-L312](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc#L297-L312)）。

**(e) Python tileop 层：让 C++ 回调的 lower 钩子**

C++ 的 `GemmNode::Lower` 并不发射指令，而是回调 Python 全局函数 `tl.gemm.lower`：

```cpp
// src/op/gemm.cc（节选）
Stmt GemmNode::Lower(const LowerArgs &lower_args, arith::Analyzer *analyzer) const {
  if (const auto f = Function::GetGlobal("tl.gemm.lower")) {      // ← 回调 Python
    ...
    auto prim_func = Downcast<PrimFunc>(
        (*f)(GetRef<Gemm>(this), lower_args.layout_map, lower_args.target,
             lower_args.thread_bounds, lower_args.thread_index, mbar_phase));
    ...
  }
}
```

见 [src/op/gemm.cc:L176-L219](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc#L176-L219)。`InferLayout` 同理回调 `tl.gemm.infer_layout`，见 [src/op/gemm.cc:L221-L259](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc#L221-L259)。

Python 侧用 `@tvm_ffi.register_global_func` 把这两个名字挂上去，转发给 `gemm.lower(...)`，后者再按注册表挑实现类：

```python
# tilelang/tileop/gemm/__init__.py（节选）
@tvm_ffi.register_global_func("tl.gemm.infer_layout")
def gemm_infer_layout(gemm, target, thread_bounds):
    thread_nums = thread_bounds.extent
    return gemm.infer_layout(target, thread_nums)

@tvm_ffi.register_global_func("tl.gemm.lower")
def gemm_lower(gemm, layout_map, target, thread_bounds, thread_index, mbar_phase_expr):
    return gemm.lower(layout_map, target, thread_bounds, thread_index, mbar_phase_expr)
```

见 [tilelang/tileop/gemm/__init__.py:L12-L29](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tileop/gemm/__init__.py#L12-L29)；`gemm.lower` 内部挑实现类见 [tilelang/tileop/gemm/__init__.py:L121-L125](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tileop/gemm/__init__.py#L121-L125)。

**(f) 多后端注册表：`(inst_name, predicate)` 分发**

GEMM 因硬件而异，所以多了一层注册表。Python 侧 `resolve_gemm_impl` 按 `(inst_name 匹配, predicate(target) 为真)` 挑唯一实现类：

```python
# tilelang/tileop/gemm/registry.py（节选）
def resolve_gemm_impl(gemm_inst: str, target: Target) -> type:
    matches = [e for e in _GEMM_IMPLS if e.inst_name == gemm_inst and e.predicate(target)]
    if not matches:
        raise ValueError(...)
    if len(matches) > 1:
        raise ValueError(...)   # 唯一性约束
    return matches[0].impl_class
```

见 [tilelang/tileop/gemm/registry.py:L38-L46](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tileop/gemm/registry.py#L38-L46)，`register_gemm_impl` 在 [tilelang/tileop/gemm/registry.py:L23-L35](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tileop/gemm/registry.py#L23-L35)。C++ 侧也有同构的 `ResolveGemmImpl` / `RegisterGemmImpl`（[src/op/gemm.cc:L30-L63](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc#L30-L63)）——这就是 u3-l1 讲过的「C++ 选指令键、Python 挑实现类」两段式分发。

> 小结：四层靠 `tl.tileop.gemm` 这个名字 + `tl.gemm.lower` / `tl.gemm.infer_layout` 这两个 FFI 名串起来。新增 op 时这四个名字要一一对应。

#### 4.1.4 代码实践

**实践目标**：用源码阅读的方式，把 `T.gemm` 的四层串联关系走一遍，确认每层的「注册点」。

**操作步骤**：

1. 在 [tilelang/language/gemm_op.py:L186-L188](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/language/gemm_op.py#L186-L188) 确认 `gemm()` 调用 `_gemm_impl("tl.tileop.gemm", ...)`。
2. 在 [src/op/gemm.cc:L261-L264](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc#L261-L264) 确认 `TIR_REGISTER_TL_TILE_OP(Gemm, gemm)` 注册了同名 op 并绑了 `TLOpBuilder`。
3. 在 [src/op/operator.cc:L39-L44](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/operator.cc#L39-L44) 确认 `ParseOperator` 查的就是 `TLOpBuilder` 这张表。
4. 在 [src/op/gemm.cc:L178-L187](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc#L178-L187) 确认 C++ `Lower` 回调 `tl.gemm.lower`；在 [tilelang/tileop/gemm/__init__.py:L18-L29](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tileop/gemm/__init__.py#L18-L29) 确认 Python 注册的正是这个名字。

**需要观察的现象**：四个位置的字符串/标识符完全一致（`tl.tileop.gemm`、`TLOpBuilder`、`tl.gemm.lower`）。

**预期结果**：画出一条从 `T.gemm` 到 `GemmScalar.lower`（或 CUDA 的 MMA 实现）的调用链，标注每层的注册点。

> 本实践为源码阅读型，不涉及运行；如需运行验证，可结合 4.3 节用 `target="c"` 编译一个 GEMM，观察是否走到 CPU 标量实现。

#### 4.1.5 小练习与答案

**练习 1**：如果要新增一个 `T.myop`，C++ 端必须实现 `TileOperatorNode` 的哪两个方法？分别在哪个 Pass 里被调用？

**参考答案**：`Lower`（在 `LowerTileOp` 里把占位展开成底层 intrinsic）和 `InferLayout`（在 `LayoutInference` 里推导 fragment/shared 布局）。

**练习 2**：为什么 `wgmma_gemm` 和 `tcgen05_gemm` 可以复用 `Gemm` 的构造函数？

**参考答案**：它们在 IR 层是同一个 `Gemm` 节点，只是注册时往 annotations 里注入 `is_wgmma` / `is_tcgen05` 标记（见 [src/op/gemm.cc:L266-L292](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/op/gemm.cc#L266-L292)），下游凭这个标记选择不同的 lowering 路径。

**练习 3**：`resolve_gemm_impl` 在匹配到多个实现类时会怎样？

**参考答案**：直接抛 `ValueError`——它要求 `(inst_name, predicate)` 唯一命中（见 [tilelang/tileop/gemm/registry.py:L43-L46](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/tileop/gemm/registry.py#L43-L46)），所以 predicate 必须写得足够严格，避免歧义。

---

### 4.2 新增一个 Pass：从 `LetInline` 看三步接入

#### 4.2.1 概念说明

Pass 是「IRModule→IRModule」的纯变换（u6-l1）。新增一个 Pass 只需要三步，外加可选的第四步（配置开关）：

1. **C++ 实现**：写一个 `StmtExprMutator`/`StmtVisitor` 改写 IR，用 `CreatePrimFuncPass` 包成一个 Pass。
2. **FFI 注册**：用 `TVM_FFI_STATIC_INIT_BLOCK` + `refl::GlobalDef().def("tl.transform.<Name>", <Name>)` 把它挂成全局函数。
3. **Python 门面**：在 `tilelang/transform/` 下加一个 `_ffi_api.<Name>()` 的薄包装并导出。
4. **接入 pipeline**：在某个后端的 `pipeline.py` body 里，在**正确的相对顺序**上调用它——这是最容易出错的一步。

注意：第 3 步只是「让 Python 能调用它」，**不会让它在编译时自动运行**；真正决定它何时运行的是第 4 步——把它插进 `pipeline.py` 的某个位置。

#### 4.2.2 核心流程

```text
C++: class MyPassMutator : public StmtExprMutator { ... VisitStmt_(...); }
     Pass MyPass() { return CreatePrimFuncPass(pass_func, 0, "tl.MyPass", {}); }
     TVM_FFI_STATIC_INIT_BLOCK { refl::GlobalDef().def("tl.transform.MyPass", MyPass); }
                                                        │
Python 门面:  def MyPass(): return _ffi_api.MyPass()    │  （tilelang/transform/xxx.py）
                                                        │
接入 pipeline: mod = tilelang.transform.MyPass()(mod)   ▼  （tilelang/<backend>/pipeline.py）
                                                        │
lower():  pipeline = resolve_pipeline(target)           │
          pipeline.lower(mod, target)  ─────────────────┘  按 target.kind.name 查到上面那个 body
```

Pass 在 pipeline 里的**相对顺序**至关重要：`LayoutInference` 必须在 `LowerTileOp` 之前（否则 tile op 还没布局就展开）、`InjectSoftwarePipeline` 必须在 `LayoutInference` 之前（见 u6-l2、u3-l3）。新 Pass 要明确它依赖哪些 Pass 的产物、又会被哪些 Pass 消费。

#### 4.2.3 源码精读

**(a) C++ 实现 + FFI 注册（最简范例）**

`LetInline` 是 tilelang 里最简单的真实 Pass，整个实现就是一个内联 `let` 绑定的 mutator，正好当作模板：

```cpp
// src/transform/frontend_legalize.cc（节选）
class LetInliner : public arith::IRMutatorWithAnalyzer {
  PrimExpr VisitExpr_(const VarNode *node) final {
    if (let_bindings_.count(node)) {
      return arith::IRMutatorWithAnalyzer::VisitExpr(let_bindings_[node]); // 用绑定值替换
    }
    ...
  }
  Stmt VisitStmt_(const BindNode *node) final {
    let_bindings_[node->var.get()] = node->value;   // 记录绑定
    return Evaluate(Integer(0));                     // 删掉 Bind 语句
  }
  std::unordered_map<const VarNode *, PrimExpr> let_bindings_;
};

Pass LetInline() {
  auto pass_func = [=](PrimFunc f, const IRModule &m, const PassContext &ctx) {
    return LetInliner::Substitute(std::move(f));     // ← 真正的变换逻辑
  };
  return CreatePrimFuncPass(pass_func, 0, "tl.LetInline", {});
}

TVM_FFI_STATIC_INIT_BLOCK() {
  namespace refl = tvm::ffi::reflection;
  refl::GlobalDef().def("tl.transform.LetInline", LetInline);   // ← FFI 注册点
}
```

见类定义 [src/transform/frontend_legalize.cc:L37-L81](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/frontend_legalize.cc#L37-L81)、Pass 构造与 FFI 注册 [src/transform/frontend_legalize.cc:L85-L95](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/frontend_legalize.cc#L85-L95)。

两个要点：
- `CreatePrimFuncPass(pass_func, opt_level, name, required)` 把「对每个 PrimFunc 做的变换」提升为「对整个 IRModule 做的 Pass」，`pass_func` 的签名固定为 `(PrimFunc, IRModule, PassContext) -> PrimFunc`。
- FFI 注册名 `tl.transform.LetInline` 与下面的 Python 名字一一对应。

**(b) Python 门面：薄包装**

Python 侧几乎是空壳，只做 `_ffi_api` 转发：

```python
# tilelang/transform/simplify.py（节选）
from . import _ffi_api

def LetInline():
    return _ffi_api.LetInline()   # ← 调用 C++ 注册的 tl.transform.LetInline
```

见 [tilelang/transform/simplify.py:L9-L17](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/simplify.py#L9-L17)，再由 [tilelang/transform/__init__.py:L5](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/__init__.py#L5) 导出为 `tilelang.transform.LetInline`。复杂一点、带参数的 Pass（如 `Simplify(simplify_arguments=False)`）只是把参数透传给 `_ffi_api`，见 [tilelang/transform/simplify.py:L20-L28](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/simplify.py#L20-L28)。

> 这就是 u6-l1 讲的「Pass 双面镜像」：算法在 C++，门面在 Python，靠 `_ffi_api` 桥接。

**(c) 接入 pipeline：注册表 + body**

Pass 注册表 `PassPipeline` 极其轻量——它只是「名字 + 一个 lower 函数」的容器：

```python
# tilelang/backend/pass_pipeline/pipeline.py（节选）
class PassPipeline:
    def __init__(self, name: str, lower: LowerFunc):
        self.name = name
        self._lower = lower
    def lower(self, mod, target):
        return self._lower(mod, target)

def register_pipeline(pipeline: PassPipeline) -> PassPipeline:
    _PIPELINES[pipeline.name] = pipeline    # ← 按 name 注册
    return pipeline

def resolve_pipeline(target: Target) -> PassPipeline:
    return get_pipeline(target.kind.name)   # ← 按 target.kind.name 查表
```

见 [tilelang/backend/pass_pipeline/pipeline.py:L11-L48](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/pass_pipeline/pipeline.py#L11-L48)。`PassPipeline` 只是薄包装，**真实的 Pass 顺序完全写死在各后端的 lower 函数 body 里**。

`lower()` 正是经 `resolve_pipeline(target)` 找到这条 body：

```python
# tilelang/engine/lower.py（节选）
pipeline = resolve_pipeline(target)
mod = pipeline.lower(mod, target)
```

见 [tilelang/engine/lower.py:L288-L289](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L288-L289)。

body 内部就是一行行 `mod = tilelang.transform.XxxPass()(mod)`。例如 CUDA pipeline 在 `LayoutInference` 之前先跑 `InjectSoftwarePipeline`，再 `LowerTileOp`：

```python
# tilelang/cuda/pipeline.py（节选）
mod = tilelang.transform.PipelinePlanning()(mod)
mod = tilelang.transform.InjectSoftwarePipeline()(mod)
mod = tilelang.transform.Simplify()(mod)
mod = tilelang.transform.LayoutInference()(mod)
LayoutVisual(mod)
mod = tilelang.transform.LowerTileOp()(mod)
```

见 [tilelang/cuda/pipeline.py:L106-L117](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/pipeline.py#L106-L117)；CUDA pipeline 的注册在 [tilelang/cuda/pipeline.py:L257-L259](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/pipeline.py#L257-L259)。`LetInline` 的接入点在 [tilelang/cuda/pipeline.py:L71-L73](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/pipeline.py#L71-L73)，受 `should_force_let_inline()` 开关保护。

**(d) （可选）配置开关**

如果一个 Pass 希望被用户开关控制（如 `LetInline` 的 `should_force_let_inline`），就接 `PassContext` + `PassConfigKey`，这部分见 u6-l1，此处不重复。简言之：用户 `pass_configs` 经 `normalize_pass_configs` 进入 `PassContext.config`，Pass 在 body 里用 `get_pass_context().config.get(...)` 读回。

#### 4.2.4 代码实践

**实践目标**：跟踪一个 Pass 从注册到被执行的全链路，并定位 `LetInline` 在 pipeline 里的精确位置。

**操作步骤**：

1. 在 [src/transform/frontend_legalize.cc:L92-L95](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/src/transform/frontend_legalize.cc#L92-L95) 找到 FFI 注册名 `tl.transform.LetInline`。
2. 在 [tilelang/transform/simplify.py:L9-L17](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/simplify.py#L9-L17) 确认 Python 门面转发到 `_ffi_api.LetInline`。
3. 在 [tilelang/cuda/pipeline.py:L71-L73](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cuda/pipeline.py#L71-L73) 与 [tilelang/cpu/pipeline.py:L21-L22](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cpu/pipeline.py#L21-L22) 对比 `LetInline` 在两个后端的接入点（都在 `MaterializeKernelLaunch` 之后）。
4. （可选）开启 `TL_ENABLE_DUMP_IR`（或 lower trace，见 u9-l1）编译一个 GEMM，在 dump 出的 IR 序列里找到 `LetInline` 前后的差异——`let`/`Bind` 节点被替换成内联值。

**需要观察的现象**：`LetInline` 之前 IR 里有 `Bind`/`Let` 节点，之后这些节点消失、变量被其绑定值替换。

**预期结果**：确认 Pass 的「注册名 → 门面 → pipeline 接入点」三者名字一致，并能解释为什么 `LetInline` 要放在 `MaterializeKernelLaunch` 之后、`Simplify` 之前。

> 第 4 步如无法在本地运行，标注「待本地验证」；前 3 步为纯源码阅读，必定可完成。

#### 4.2.5 小练习与答案

**练习 1**：如果你只完成了「C++ 实现 + FFI 注册 + Python 门面」，但忘了把它加进任何 `pipeline.py`，会发生什么？

**参考答案**：这个 Pass 永远不会被执行——`lower()` 只跑 `resolve_pipeline(target).lower(...)` 这一条 body（[tilelang/engine/lower.py:L288-L289](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L288-L289)），不在 body 里的 Pass 等于不存在。Python 能 `import` 到它、能手动调用它，但编译流程不会触发。

**练习 2**：为什么不能把 `LowerTileOp` 放在 `LayoutInference` 之前？

**参考答案**：`LowerTileOp` 展开 tile op 时需要读取 `LayoutInference` 推导出的 fragment 布局（Thread-Value 映射）来发射正确的 MMA/WGMMA 指令（见 u6-l2、u6-l3）。顺序反了就拿到空布局，发射不出正确指令。

**练习 3**：新增一个带运行时开关的 Pass，开关用什么机制传到 C++？

**参考答案**：用户在 `pass_configs` 里设置，经 `normalize_pass_configs` 进入 `PassContext.config`；pipeline body 里用 `get_pass_context().config.get("tl.xxx", default)` 读回并决定是否调用该 Pass（或在 C++ 内用 `PassContext::GetConfig("tl.xxx")` 读取），见 u6-l1。

---

### 4.3 移植一个新后端：CPU 后端的五类注册点

#### 4.3.1 概念说明

「后端」在 tilelang 里不是单一文件，而是**五类注册点的集合**。这五类都必须补齐，`target=<kind>` 才能端到端跑通：

| 注册点 | 文件 | 职责 |
| --- | --- | --- |
| ① language 方言 | `tilelang/<bk>/language/__init__.py` | 导入 `language.common`，设 `__tilelang_dialect__`，追加硬件扩展 |
| ② pipeline | `tilelang/<bk>/pipeline.py` | 实现 `PassPipeline` body 并 `register_pipeline` |
| ③ tileop 实现 | `tilelang/<bk>/op/` | 该后端的 op lowering，注册到 tileop 注册表 |
| ④ device/host codegen | `tilelang/<bk>/codegen.py` | `register_device_codegen` / `register_host_codegen` |
| ⑤ execution_backend | `tilelang/<bk>/execution_backend.py` | `register_execution_backend` 选 adapter |

外加一个**包入口** `tilelang/<bk>/__init__.py` 把上述子模块导入（否则注册代码不会执行）。

CPU 后端是所有后端里**最瘦**的——它没有 TMA/cluster/warpgroup，language 方言只复用 common；GEMM 退化为标量三重循环。所以它是移植新后端最好的参照模板：先按 CPU 的样子搭起五类注册点，再往里加硬件特性。

#### 4.3.2 核心流程

移植新后端 `myhw` 的流程：

```text
① language:   tilelang/myhw/language/__init__.py
                  from tilelang.language.common import *
                  __tilelang_dialect__ = "myhw"
                  (可选) 追加硬件扩展 intrinsics
② pipeline:   tilelang/myhw/pipeline.py
                  def MyHwPassPipelineBody(mod, target): ...   # 按 myhw 能力裁剪 Pass 序列
                  register_pipeline(PassPipeline("myhw", MyHwPassPipelineBody))
③ tileop:     tilelang/myhw/op/gemm/__init__.py
                  register_gemm_impl("myhw.xxx", INST, _match_myhw, MyHwGemm)  # 复用现有注册表
④ codegen:    tilelang/myhw/codegen.py
                  register_device_codegen("myhw", DeviceCodegen(...))
                  register_host_codegen("myhw", HostCodegen(...))
⑤ exec:       tilelang/myhw/execution_backend.py
                  register_execution_backend("myhw", ExecutionBackendSpec("tvm_ffi", ...))
入口:         tilelang/myhw/__init__.py  →  import codegen, op, pipeline, execution_backend
target 解析:   tilelang/myhw/target.py  →  注册探测器/归一化器（见 u4-l4）
```

注意 ② 与 ④ 都依赖 `target.kind.name == "myhw"`：pipeline 注册表、device codegen 注册表都按 kind 名查表。所以「后端名」必须贯穿五类注册点。

#### 4.3.3 源码精读

**(a) ① language 方言：最瘦的入口**

CPU 方言只做两件事——复用通用语言、标记方言名，**没有任何硬件扩展**：

```python
# tilelang/cpu/language/__init__.py（全文）
"""CPU language dialect: common TileLang plus CPU extensions."""
from tilelang.language.common import *  # noqa: F401,F403
from tilelang.language.common import __all__ as _COMMON_ALL

__tilelang_dialect__ = "cpu"
__all__ = tuple(_COMMON_ALL)
del _COMMON_ALL
```

见 [tilelang/cpu/language/__init__.py:L1-L11](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cpu/language/__init__.py#L1-L11)。对比之下，CUDA 方言在 common 之上叠加 cluster/intrinsics/pdl/random/warpgroup（见 u9-l3），是最胖的。`__tilelang_dialect__` 是门面模式的关键标识（见 u4-l4）。

**(b) ② pipeline：按能力裁剪 Pass 序列**

CPU pipeline 与 CUDA pipeline 结构同构，但**删掉了一切 GPU 专属 Pass**（无 `LowerThreadAllreduce`、无 `LowerHopperIntrin`、无 `ThreadSync("shared")`），且 `MaterializeKernelLaunch(lower_thread_binding=False)`——因为 CPU 没有线程维度：

```python
# tilelang/cpu/pipeline.py（节选）
def CPUPassPipelineBody(mod: IRModule, target: Target) -> IRModule:
    mod = tirx.transform.BindTarget(target)(mod)
    mod = tilelang.transform.MaterializeKernelLaunch(lower_thread_binding=False)(mod)
    ...
    mod = tilelang.transform.LayoutInference()(mod)
    LayoutVisual(mod)
    mod = tilelang.transform.LowerTileOp()(mod)
    ...
    # CPU 目前跳过 LowerThreadAllreduce，因为线程绑定被 lower 成串行循环
    mod = tilelang.transform.AnnotateDeviceRegions()(mod)
    mod = tilelang.transform.SplitHostDevice()(mod)
    ...
    mod = tilelang.transform.LowerDeviceKernelLaunch()(mod)
    return mod

for _kind in ("c", "llvm"):
    register_pipeline(PassPipeline(_kind, CPUPassPipelineBody))   # ← c 与 llvm 共用一条 body
```

见 [tilelang/cpu/pipeline.py:L16-L80](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cpu/pipeline.py#L16-L80)。注意末尾把同一条 body 同时注册给 `c` 和 `llvm` 两个 kind——这揭示一个要点：**pipeline 按 `target.kind.name` 注册，但多个 kind 可共享一条 body**。

**(c) ③ tileop 实现：标量 GEMM + 注册**

CPU 没有 tensor core，GEMM 退化为标量三重循环。它继承通用 `GemmBase`，`infer_layout` 返回空（CPU 无 fragment 布局），`lower` 返回一个用 `@T.prim_func` 写的纯计算 PrimFunc：

```python
# tilelang/cpu/op/gemm/gemm_scalar.py（节选）
GEMM_INST_SCALAR = "cpu.scalar"

class GemmScalar(GemmBase):
    """CPU scalar fallback: triple nested loop gemm."""
    def infer_layout(self, target, thread_nums):
        return {}

    def lower(self, layout_map, target, thread_bounds, thread_index, mbar_phase_expr=None):
        ...
        @T.prim_func
        def _gemm_scalar() -> None:
            if clear_accum:
                T.clear(C_buf)
            for i, j, k in T.grid(M, N, K):
                C_buf[c0 + i, c1 + j] += T.cast(
                    A_buf[...] * B_buf[...], accum_dtype)
        return _gemm_scalar
```

见 [tilelang/cpu/op/gemm/gemm_scalar.py:L10-L55](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cpu/op/gemm/gemm_scalar.py#L10-L55)。然后用 predicate `_match_scalar`（`target.kind.name in {"c", "llvm"}`）把它注册进**已有的** GEMM 注册表：

```python
# tilelang/cpu/op/gemm/__init__.py（全文）
from tilelang.tileop.gemm.registry import register_gemm_impl
from .gemm_scalar import GEMM_INST_SCALAR, GemmScalar

def _match_scalar(target) -> bool:
    return target.kind.name in {"c", "llvm"}

register_gemm_impl("cpu.scalar", GEMM_INST_SCALAR, _match_scalar, GemmScalar)
```

见 [tilelang/cpu/op/gemm/__init__.py:L7-L11](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cpu/op/gemm/__init__.py#L7-L11)。这是 4.1 节注册表的直接复用——新后端的 GEMM 不用动 C++，只需写一个 Python 子类并注册。

**(d) ④ codegen：复用 TVM 的 llvm/c 编译器**

CPU 没有自研 codegen，直接复用 TVM 的 `target.build.llvm` / `target.build.tilelang_c`：

```python
# tilelang/cpu/codegen.py（节选）
register_device_codegen(
    "c",
    DeviceCodegen("c", build_without_compile=global_func_device_codegen("target.build.tilelang_c")),
    override=True,
)
register_device_codegen(
    "llvm",
    DeviceCodegen("llvm", build=_build_llvm, build_without_compile=_build_llvm),
    override=True,
)
register_host_codegen("c", HostCodegen("c", build=_build_host_c), override=True)
register_host_codegen("llvm", HostCodegen("llvm", build=_build_host_llvm), override=True)
```

见 [tilelang/cpu/codegen.py:L12-L41](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cpu/codegen.py#L12-L41)。`global_func_device_codegen(name)` 返回一个调用 TVM 全局函数的闭包（[tilelang/backend/device_codegen.py:L18-L24](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/device_codegen.py#L18-L24)）。`DeviceCodegen` 经 `resolve_device_codegen(target)` 按 kind 查表（见 u4-l1、u7-l2）。

**(e) ⑤ execution_backend：选 adapter**

最后把 target kind 关联到一个 adapter（这里 c/llvm 都用 `tvm_ffi`，c 额外登记 `cython`）：

```python
# tilelang/cpu/execution_backend.py（节选）
register_execution_backend("c", ExecutionBackendSpec("cython"), override=True)
register_execution_backend(
    "c", ExecutionBackendSpec("tvm_ffi", enable_host_codegen=True, enable_device_compile=True), override=True)
register_execution_backend(
    "llvm", ExecutionBackendSpec("tvm_ffi", enable_host_codegen=True, enable_device_compile=True), override=True)
```

见 [tilelang/cpu/execution_backend.py:L6-L17](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cpu/execution_backend.py#L6-L17)。`ExecutionBackendSpec` 上的 `enable_host_codegen` / `enable_device_compile` 是给 `lower()` 的指令，决定产物 `rt_mod` 是否非空（见 u7-l1）。

**(f) 包入口：确保注册代码被执行**

五类注册点都是模块级副作用代码，必须被 import 才能生效。CPU 的包入口把它们全部导入：

```python
# tilelang/cpu/__init__.py（全文）
from . import codegen  # noqa: F401
from . import op  # noqa: F401
from . import pipeline  # noqa: F401
from . import execution_backend  # noqa: F401
```

见 [tilelang/cpu/__init__.py:L1-L4](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cpu/__init__.py#L1-L4)。漏掉任何一个，对应的注册表里就没有 `c`/`llvm` 条目，运行时会报 `No pipeline registered for backend 'llvm'` 之类的错。

> 移植要点回顾：五类注册点 + 包入口，全部用同一个 `target.kind.name` 串联；先照 CPU 抄出最瘦骨架，再逐项加硬件能力。

#### 4.3.4 代码实践

**实践目标**：用 CPU target 编译一个 GEMM，验证它走的是标量 GEMM 实现，并对照五类注册点。

**操作步骤**：

1. 写一个最小 GEMM kernel（参考 u1-l4 的 quickstart），编译时显式指定 `target="llvm"` 或 `target="c"`（无 GPU 机器也可）。
2. 调用 `kernel.get_kernel_source()` 查看生成的设备源码。
3. 在源码里搜索：是否能看到三重循环（对应 `GemmScalar.lower` 的 `T.grid(M, N, K)`），而**没有**任何 `mma`/`wgmma`/`cp.async` 指令。
4. 对照本节五个注册点文件，确认 `target="llvm"` 时：pipeline 命中 [tilelang/cpu/pipeline.py:L79-L80](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cpu/pipeline.py#L79-L80) 的 `llvm` 条目、GEMM 命中 [tilelang/cpu/op/gemm/__init__.py:L11](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cpu/op/gemm/__init__.py#L11) 的 `_match_scalar`、codegen 命中 [tilelang/cpu/codegen.py:L21-L29](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cpu/codegen.py#L21-L29) 的 `llvm` 条目。

**需要观察的现象**：生成的源码是纯 C/C++（llvm 时为 IR，`c` 时为 C 源码），含三重循环，无 GPU 指令。

**预期结果**：确认「换一个 target kind，五类注册表各自独立命中对应条目」，端到端跑通 CPU GEMM。

> 若本地无可用 CPU codegen（TVM 未编 LLVM），第 1-3 步标注「待本地验证」；第 4 步为纯源码阅读，必定可完成。

#### 4.3.5 小练习与答案

**练习 1**：为什么 CPU pipeline 把 `c` 和 `llvm` 两个 kind 注册到同一条 body？

**参考答案**：因为两者都是 CPU 后端，Pass 序列完全相同（都不需要 GPU 专属 Pass），区别只在最后的 codegen（[tilelang/cpu/codegen.py:L12-L41](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/cpu/codegen.py#L12-L41) 分别注册 `c` 和 `llvm`）。pipeline 关心的是 IR 变换，与最终发射 C 还是 LLVM 无关。

**练习 2**：CPU 的 `GemmScalar.infer_layout` 为什么返回空字典？

**参考答案**：CPU 没有寄存器文件/fragment 概念，也不需要把逻辑下标映射到「线程-寄存器」对（那是 CUDA 张量核 `Fragment` 的职责，见 u3-l4、u9-l2）。标量三重循环直接用逻辑下标访问，无需布局推理。

**练习 3**：移植一个新后端时，`tilelang/myhw/__init__.py` 漏 import `pipeline` 子模块会怎样？

**参考答案**：`register_pipeline(PassPipeline("myhw", ...))` 永远不会执行，`resolve_pipeline(target)` 在 [tilelang/backend/pass_pipeline/pipeline.py:L39-L43](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/backend/pass_pipeline/pipeline.py#L39-L43) 会抛 `No pipeline registered for backend 'myhw'`。

---

## 5. 综合实践

设计一个**自定义 tile op `T.scale_add(A, B, C, alpha)`**（计算 `C = A + alpha * B`）的完整接入方案，或设计一个**自定义 Pass**（二选一）。要求只到「接口签名 + 文件清单 + 注册点」级别，不要求实现全部细节。

### 方案 A：自定义 tile op `T.scale_add`

仿照 4.1 节 `T.gemm` 的四层结构，写出需要新增/修改的文件：

| 层 | 文件（新增/修改） | 内容与注册点 |
| --- | --- | --- |
| language | `tilelang/language/scale_add_op.py`（新增） | 定义 `scale_add(A,B,C,alpha)`，调用 `tirx.call_intrin("handle", Op.get("tl.tileop.scale_add"), A, B, C, alpha)`；并在 `tilelang/language/__init__.py` 导出为 `T.scale_add` |
| C++ op | `src/op/scale_add.h` / `src/op/scale_add.cc`（新增） | 定义 `ScaleAddNode : TileOperatorNode`，实现 `Lower`/`InferLayout`；用 `TIR_REGISTER_TL_TILE_OP(ScaleAdd, scale_add)` 注册 op 并绑 `TLOpBuilder`；构造函数按下标反序列化 `(A, B, C, alpha)` |
| C++ 构建接入 | `src/op/CMakeLists.txt` 或对应的 source 列表（修改） | 把 `scale_add.cc` 加入编译列表 |
| Python tileop | `tilelang/tileop/scale_add.py`（新增） | 用 `@tvm_ffi.register_global_func("tl.scale_add.lower")` 与 `"tl.scale_add.infer_layout"` 注册回调；C++ `Lower` 里 `Function::GetGlobal("tl.scale_add.lower")` |
| 测试 | `testing/python/test_scale_add.py`（新增） | 用 `@tilelang.jit` 写 kernel 调用 `T.scale_add`，对照 torch 参考实现 `assert_allclose` |

关键接口签名（伪代码）：

```cpp
// src/op/scale_add.h
class ScaleAddNode : public TileOperatorNode {
 public:
  tirx::Buffer a_, b_, c_;
  PrimExpr alpha_;
  Stmt Lower(const LowerArgs& args, arith::Analyzer* analyzer) const override;
  LayoutMap InferLayout(const LayoutInferArgs& args, InferLevel level) const override;
  TileOperator Clone() const override;
  TVM_FFI_DECLARE_OBJECT_INFO("tl.ScaleAdd", ScaleAddNode, ffi::Object);
};
```

```python
# tilelang/language/scale_add_op.py
def scale_add(A, B, C, alpha):
    return tirx.call_intrin("handle", tirx.op.Op.get("tl.tileop.scale_add"),
                            A, B, C, alpha)
```

**自检清单**（四个名字必须一致）：`tl.tileop.scale_add`（language 层 call_intrin + C++ 注册宏）、`TLOpBuilder`（属性表）、`tl.scale_add.lower` / `tl.scale_add.infer_layout`（C++ 回调名 ↔ Python 注册名）。

### 方案 B：自定义 Pass `CountTileOps`

统计每个 kernel 里各 tile op 出现的次数并打印（调试用）。仿照 4.2 节 `LetInline`：

| 步骤 | 文件（新增/修改） | 内容与注册点 |
| --- | --- | --- |
| C++ 实现 | `src/transform/count_tile_ops.cc`（新增） | 写 `StmtVisitor` 遍历 `tl.tileop.*` 调用并计数；`Pass CountTileOps()` 用 `CreatePrimFuncPass` 包装；`TVM_FFI_STATIC_INIT_BLOCK { refl::GlobalDef().def("tl.transform.CountTileOps", CountTileOps); }` |
| C++ 构建接入 | source 列表（修改） | 加入编译列表 |
| Python 门面 | `tilelang/transform/count_tile_ops.py`（新增） | `def CountTileOps(): return _ffi_api.CountTileOps()`；在 `tilelang/transform/__init__.py` 导出 |
| 接入 pipeline | `tilelang/cuda/pipeline.py`（修改） | 在 `LowerTileOp` **之后**插入 `mod = tilelang.transform.CountTileOps()(mod)`（因为它要数已展开/未展开的 tile op，必须在 LowerTileOp 附近） |

**关键决策**：为什么放在 `LowerTileOp` 之后？因为它要统计的是「展开前后」的 tile op 数量差异，放在 `LowerTileOp` 前后各跑一次能对比（如需对比，可插入两次）。

> 这两个方案都不要求在本讲内实现并运行，重点训练「文件清单 + 注册点 + 名字一致性」的工程判断。完成后可参考 u10-l1 的测试规范补一个 pytest。

## 6. 本讲小结

- **新增 tile op 需改四层**：Python language 层（`call_intrin` 留 `tl.tileop.<name>` 占位）→ C++ op 层（`TIR_REGISTER_TL_TILE_OP` 注册 op + 绑 `TLOpBuilder`、实现 `Lower`/`InferLayout`）→ Python tileop 层（`@tvm_ffi.register_global_func` 注册 `tl.<name>.lower`/`infer_layout` 让 C++ 回调）→（多后端时）`(inst_name, predicate)` 注册表分发。四层靠 op 名与 FFI 名一一串联。
- **新增 Pass 需三步**：C++ `CreatePrimFuncPass` 包装 + `refl::GlobalDef().def("tl.transform.<Name>", ...)` 注册 → Python `_ffi_api.<Name>()` 薄包装 → 在某个后端 `pipeline.py` body 里按正确相对顺序调用。**不接入 pipeline 的 Pass 永远不会执行。**
- **pipeline 注册表是薄壳**：`PassPipeline` 仅是「name + lower 函数」容器，真实 Pass 顺序写死在各后端 body 里，`resolve_pipeline(target)` 按 `target.kind.name` 查表；多个 kind 可共享一条 body（如 CPU 的 `c`/`llvm`）。
- **移植新后端需补五类注册点 + 包入口**：language 方言、pipeline、tileop 实现、device/host codegen、execution_backend，全部用同一个 `target.kind.name` 串联；CPU 后端是最瘦参照模板。
- **贯穿全文的三个统一模式**：属性表（`TLOpBuilder`）、注册表+惰性加载+谓词匹配（pipeline/codegen/execution_backend）、`TVM_FFI_STATIC_INIT_BLOCK` + `refl::GlobalDef`（C++→Python FFI 桥）。

## 7. 下一步学习建议

- **动手实现综合实践的方案 A 或 B**：从 CPU target 起步（无需 GPU），先把 `T.scale_add` 跑通标量版，再考虑加 CUDA 路径。这是检验是否真正理解本讲的最佳方式。
- **阅读一个真实的「最近新增 op」**：用 `git log --oneline src/op/` 找最近一次新增 tile op 的提交，对照本讲四层模型看作者改了哪些文件、注册点怎么写。
- **通读一个后端的全部注册点**：以 `tilelang/cpu/` 为入口，按本节五类注册点逐个读完，再对比 `tilelang/metal/` 或 `tilelang/webgpu/`（它们也是较瘦后端），体会「最瘦骨架 + 硬件扩展」的分层。
- **回顾 u6-l1 的 PassConfigKey 与 u6-l2 的 lowering Pass**：本讲只讲了「怎么把 Pass 接进去」，Pass 内部如何读写 `PassContext` 配置、如何与 `LayoutInference`/`LowerTileOp` 协作，需结合 u6-l1/u6-l2 才能写出一个正确且有用的 Pass。
- **结合 u10-l1 的测试与贡献流程**：任何扩展都应配套测试；按 pre-commit/CI 规范提交，参考 `testing/python/` 下现有 op 测试的「工厂 + run + assert_allclose」三段式范式。
