# 内存规划三部曲：MemRef 的创建、别名、复用与地址分配

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 **MemRef** 这个「地契」抽象的三要素（base / byte_offset / size），以及它与片上内存空间（Vec/Mat/Left/Right/Acc…）的对应关系。
2. 讲清内存规划三部曲——**InitMemRef（第 31 个 Pass）→ MaterializeSemanticAliases（第 32 个）→ MemoryReuse（第 33 个）→ AllocateMemoryAddr（第 34 个）**——各自负责什么、为什么顺序不可调换。
3. 区分两类共享：**must-alias**（语义要求的别名，如循环累加器）与 **may-alias**（基于生命周期的机会式复用）。
4. 读懂 MemoryReuse 的装箱算法：生命周期区间如何计算、「touching 允许」的判定、first-fit-decreasing 装箱与四道 `can_share` 闸门。
5. **（本版本重点）** 理解循环携带（iter_arg/yield）写回拷贝的三个正确性机制：**排序**（谁的源和别人的目的重叠谁先跑）、**破环**（swap 用临时缓冲 spill）、**共享缓冲拒绝**（两个携带共用一块缓冲时报错而不是静默出错）。
6. 读懂内存规划后 IR 中 `tile.alloc` 与地址分配的形态，并能对比开/关内存复用时的片上占用差异。

## 2. 前置知识

### 2.1 片上内存空间（MemorySpace）

回顾 u2-l4：Tile 是片上数据块，它必须落在某个具体的片上缓冲里。PyPTO 用枚举
`MemorySpace` 描述这些物理缓冲，见
[include/pypto/ir/memory_space.h:L35-L45](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/memory_space.h#L35-L45)：

| 空间 | 含义 | 通俗叫法 |
| ---- | ---- | -------- |
| `DDR` | 片外全局内存 | GM（global memory） |
| `Vec` | 向量核统一缓冲 | UB |
| `Mat` | 矩阵/L1 缓冲 | L1 |
| `Left` / `Right` | 矩阵乘左右操作数缓冲 | L0A / L0B |
| `Acc` | 累加器缓冲 | L0C |
| `Bias` | 偏置缓冲 | L0 bias |
| `LeftScale` / `RightScale` | MX 块缩放缓冲（A5） | — |

关键事实：**每个空间是一块容量固定、相互独立的竞技场**。Vec 只有约 200KB、L0C 更小，
所以「同一空间里生命周期不重叠的 Tile 能不能挤一块缓冲」直接决定算子能否装进芯片。

### 2.2 MemRef：一块内存的「地契」

每个 Tile 类型的变量最终都要回答三个问题：我的内存在哪块分配里（`base_`）、从分配起点偏移多少字节（`byte_offset_`）、占用多大（`size_`）。这三个字段就是 `MemRef`，见
[include/pypto/ir/memref.h:L42-L46](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/memref.h#L42-L46)：

```cpp
class MemRef : public Var {
 public:
  VarPtr base_;          ///< Ptr variable from alloc — allocation identity token
  ExprPtr byte_offset_;  ///< Byte offset from base (0 for full alloc, view offset for views)
  uint64_t size_;        ///< Size in bytes of this MemRef
```

`base_` 是**分配身份**：两个 MemRef 的 `base_` 指针相同，就说明它们物理上是同一块分配（可能一个是根、一个是视图）。别名判断用 `SameAllocation`（比 base）与 `MayAlias`（比字节区间是否重叠）。

### 2.3 循环携带四元组

回顾 u4-l3 的 ForStmt：一个循环携带变量在 IR 里有四个角色——

```text
initValue（循环前的初值） → iter_arg（循环体内的名字）
        ↑                        │
        │                        │ 每轮结束 pl.yield_(...)
        └── return_var ←── yield value
```

InitMemRef 的约定是 **Group A（initValue + iter_arg）共享一个 MemRef，Group B（yield value + return_var）共享一个 MemRef**，A 与 B 可以不同——这个「缝隙」正是 MemoryReuse 后续要做写回修补的地方（见 [docs/en/dev/passes/31-init_memref.md:L177-L193](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/31-init_memref.md#L177-L193)）。

### 2.4 must-alias 与 may-alias

[docs/en/dev/passes/32-materialize_semantic_aliases.md:L9-L16](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/32-materialize_semantic_aliases.md#L9-L16) 把共享分成两类：

- **must-alias（语义必须）**：循环累加器的「下一个值」必须写回携带缓冲，否则循环不累加。这是正确性问题。
- **may-alias（机会式）**：两个独立缓冲生命周期不重叠，**可以**共享以省内存。这是优化。

这条分界线解释了为什么有两个独立的 Pass：机会式复用可以被跳过（换用其他内存规划器），语义别名永远不能省。

### 2.5 MemoryPlanner 三种模式

`PassContext` 里的 `memory_planner` 决定谁来做内存规划（见
[python/pypto/ir/pass_manager.py:L316-L340](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/pass_manager.py#L316-L340)）：

| 模式 | MemoryReuse | AllocateMemoryAddr | 说明 |
| ---- | ----------- | ------------------ | ---- |
| `PYPTO`（默认） | 运行 | 运行（顺序分配器） | 本讲主线 |
| `DSA_RP` | 跳过 | 运行（求解器） | 保留独立分配身份，把复用决策变成带容量约束的求解问题 |
| `PTOAS` | 跳过 | 跳过 | 交给外部 ptoas 汇编器规划内存 |

三种模式下 MaterializeSemanticAliases 都会运行——must-alias 不可省。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
| ---- | ---- | -------- |
| [src/ir/transforms/materialize_tensor_strides_pass.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/materialize_tensor_strides_pass.cpp) | 第 30 个 Pass：给 TensorView 补上打包规范步长 | 前置铺垫（一句话） |
| [src/ir/transforms/init_memref.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/init_memref.cpp) | 第 31 个 Pass：创建 MemRef、插入 tile.alloc | 模块 4.2 |
| [include/pypto/ir/transforms/utils/memref_utils.h](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/transforms/utils/memref_utils.h) | MemRef 公共工具：地址比较、alloc 语句构造、字节偏移计算 | 模块 4.1/4.2 |
| [src/ir/transforms/memory_reuse_pass.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/memory_reuse_pass.cpp) | 第 32、33 个 Pass 的实现都在这一个文件：MaterializeSemanticAliases + MemoryReuse | 模块 4.3/4.4/4.5 |
| [src/ir/transforms/allocate_memory_addr_pass.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/allocate_memory_addr_pass.cpp) | 第 34 个 Pass：分配具体地址 | 模块 4.6 |
| [tests/ut/ir/transforms/test_memory_reuse.py](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/transforms/test_memory_reuse.py) | 三 Pass 流水线的 before/after 单测（含移位寄存器回归测试） | 实践依据 |
| [docs/en/dev/passes/31~34-*.md](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/33-memory_reuse.md) | 四个 Pass 的官方文档 | 权威参考 |

流水线中的位置（[python/pypto/ir/pass_manager.py:L213-L226](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/pass_manager.py#L213-L226)）：

```python
passes.materialize_tensor_strides,   # 30: TensorView 步长规范化
passes.init_mem_ref,                 # 31: 创建 MemRef
passes.materialize_semantic_aliases, # 32: 语义必须别名
passes.memory_reuse,                 # 33: 生命周期复用
passes.allocate_memory_addr,         # 34: 地址分配
```

为什么顺序不可换：MemRef 都不存在时谈不上别名；语义别名先行把携带链熔到一个 base 上，
生命周期分析才能把「一组变量」当作一个区间；复用改变了缓冲归属后才有地址可分。

## 4. 核心概念与源码讲解

### 4.1 MemRef 与地址比较：按地址，不按分配

#### 4.1.1 概念说明

内存规划里最频繁的问题不是「这两个变量相等吗」，而是**「这两个 MemRef 指向同一块字节吗」**。
`MemRef::SameAllocation` 只比较 `base_` 指针，但作者可以用
`pl.MemRef(slots=N)` 声明一个多槽分配——两个槽共享 base 却占用不相交的字节区间。
只看 base 会把「槽 0 和槽 1」误判成同一块存储，从而漏掉一次必需的搬运。

#### 4.1.2 核心流程

`CompareBaseAddress` 给出三值答案：

```text
比较 base_ 指针
├─ 不同 → kDifferent（一定不是同一地址）
└─ 相同 → 比较 byte_offset_（用 AreExprsEqual，按结构比较表达式）
    ├─ 相等 → kSame（同一地址）
    ├─ 两个都是 ConstInt 且不等 → kDifferent（常量偏移，可证明不同）
    └─ 其余（符号偏移）→ kUnknown（无法证明，调用方必须保守处理）
```

注意它**刻意不比较 size**：一个带行填充的累加器可能以两种 valid_shape 看同一块缓冲，
要求 size 相等会逼出「缓冲拷回自己」这种（Acc 空间根本无法降级的）非法操作。

#### 4.1.3 源码精读

[include/pypto/ir/transforms/utils/memref_utils.h:L226-L261](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/transforms/utils/memref_utils.h#L226-L261)
定义了三值枚举 `AddressRelation` 与 `CompareBaseAddress`：

```cpp
inline AddressRelation CompareBaseAddress(const MemRefPtr& a, const MemRefPtr& b) {
  CHECK(a != nullptr && b != nullptr) << "MemRef must not be null";
  if (a->base_.get() != b->base_.get()) return AddressRelation::kDifferent;
  if (AreExprsEqual(a->byte_offset_, b->byte_offset_)) return AddressRelation::kSame;
  // AreExprsEqual folds ConstInt by value, so two constants that compare unequal
  // really are different addresses; anything else is symbolic and unprovable.
  if (As<ConstInt>(a->byte_offset_) && As<ConstInt>(b->byte_offset_)) return AddressRelation::kDifferent;
  return AddressRelation::kUnknown;
}
```

这段代码做的事：先比分配身份，再比字节偏移；两个常量偏移可证不同，符号偏移（如
`buf[i % 2]` 两次出现在不同位置）一律返回「不可判定」。调用方（如 4.5 的 YieldFixupMutator）
遇到 `kUnknown` 必须**报错**而不是猜——搬运和不搬运各有一半概率错。

#### 4.1.4 代码实践

1. **实践目标**：直观感受「同分配 ≠ 同地址」。
2. **操作步骤**：阅读
   [tests/ut/ir/transforms/test_memory_reuse.py:L113-L124](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/transforms/test_memory_reuse.py#L113-L124)
   的 Python 镜像实现 `_same_base_address`——它用 `unique_id` 比 base、`structural_equal` 比偏移，
   与 C++ 语义一一对应。
3. **需要观察的现象**：该函数的 docstring 为什么强调「size 刻意不比较」。
4. **预期结果**：能口头复述 `kSame / kDifferent / kUnknown` 各自的判定条件与 `kUnknown` 的处理原则。

#### 4.1.5 小练习与答案

**练习 1**：`pl.MemRef("buf", slots=2)` 声明的两个槽 MemRef，`base_`、`byte_offset_`、`size_` 各是什么关系？
**答案**：`base_` 相同（同一个分配身份）；`byte_offset_` 分别是 `0` 和 `slot_size`（常量下标被 InitMemRef 折叠成 ConstInt）；`size_` 都等于单个槽的大小（不是整个分配的两倍）——见 [src/ir/transforms/init_memref.cpp:L279-L310](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/init_memref.cpp#L279-L310) 中 `UserBoundMemRef` 的注释。

**练习 2**：为什么 `CompareBaseAddress` 遇到两个**符号**偏移要返回 `kUnknown` 而不是当作相同？
**答案**：两个不同的符号表达式可能算出同一个值也可能不同。当作相同会漏掉必要的搬运（数据留在旧槽）；当作不同会发出「缓冲拷到自己」的无效搬运（Acc 空间没有合法降级）。所以调用方必须停下来向作者报错。

### 4.2 InitMemRef：给每个 Tile 落一张地契（第 31 个 Pass）

#### 4.2.1 概念说明

InitMemRef 之前的 IR 里，Tile 类型只有形状/dtype/内存空间，**没有 MemRef**——代码生成无法回答「这块 Tile 的数据具体放哪」。InitMemRef 就是「发地契」的一步：为每个 Tile/Tensor 变量创建或继承一个 MemRef，并为所有非 DDR 的 MemRef 生成一条 `tile.alloc(space, -1, size)` 语句（地址 -1 表示尚未分配，等第 34 个 Pass 填真值）。

#### 4.2.2 核心流程

主入口 `TransformInitMemRef`（[src/ir/transforms/init_memref.cpp:L818-L905](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/init_memref.cpp#L818-L905)）：

```text
1. NormalizeStmtStructure：保证函数体是扁平 SeqStmts
2. DeclaredAllocCollector：收集作者声明的 pl.MemRef("name") 分配
   （大小 = 绑定到它的最大 Tile；空间必须一致；PTOAS 模式拒绝单槽声明）
3. InitMemRefMutator 遍历，为每个变量定 MemRef：
   ├─ 视图算子（slice/reshape/…）→ ShareMemRefFrom：继承输入的 base，累加字节偏移
   ├─ 复用输入算子（tile.store、matmul_acc、gemv_acc…）→ 继承指定输入的 MemRef
   ├─ 纯别名（a = b）→ 直接共享同一个 MemRef shared_ptr（保留 base 指针身份）
   ├─ IterArg → 继承 initValue 的 MemRef（Group A）
   └─ 其余 → CreateMemRef：新建 base Ptr（mem_vec_N 之类命名）+ size
4. ForStmt/IfStmt 的 return_var 补齐：patch 成与对应 yield 值共享 MemRef
5. 收集全部 MemRef，按 base_ 指针去重，逐个 CreateAllocStatement
6. InsertAllocsIntoBody：把 alloc 语句全部提升到函数体头部的 SeqStmts
```

其中「视图偏移」的数学很直白：对 `tile.slice(input, shape, offset)`，字节偏移为

\[ \text{byte\_offset} = \Big(\sum_i o_i \prod_{j>i} s_j\Big) \times \frac{\text{storage\_bits}}{8} \]

即按父形状算出的线性元素偏移乘以每个元素的存储字节数，实现在
[include/pypto/ir/transforms/utils/memref_utils.h:L408-L448](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/transforms/utils/memref_utils.h#L408-L448) 的 `ComputeSliceByteOffset`。

#### 4.2.3 源码精读

**① AssignStmt 的 MemRef 决策链**（[src/ir/transforms/init_memref.cpp:L582-L662](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/init_memref.cpp#L582-L662)）依次尝试：视图算子继承输入 → 注册表标记的「输出复用某输入」→ 纯别名 `a = b` → 默认新建。关键片段：

```cpp
// Handle ops whose output reuses a specific input arg's MemRef (registry-based)
auto reuse_arg_idx = GetOutputReusesInputArg(call->op_->name_);
if (reuse_arg_idx.has_value()) { ... ShareMemRefFrom(new_call->args_[*reuse_arg_idx], op, new_value); ... }
```

这段代码做了什么：查算子注册表的 `output_reuses_input_arg` 属性（u4-l6 讲过），
让 `tile.matmul_acc` 的输出落在累加器输入的缓冲上——这就是「原地累加」在 IR 层的起点。

**② 循环携带的 Group A/B 约定**（[src/ir/transforms/init_memref.cpp:L664-L744](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/init_memref.cpp#L664-L744)）：
`VisitStmt_(ForStmt)` 先处理 iter_args（继承 initValue），再递归循环体，最后
`PatchReturnVarsFromYield`（[src/ir/transforms/init_memref.cpp:L774-L797](https://github.com/hw-native-sys-pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/init_memref.cpp#L774-L797)）
把每个 return_var 的类型改成携带 yield 值的 MemRef——注释明确说：
「initValue/iter_arg/return_var 共享一个 MemRef 缓冲……yield 与缓冲的错位是下游 Pass 的事」。

**③ alloc 提升**（[src/ir/transforms/init_memref.cpp:L886-L904](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/init_memref.cpp#L886-L904)）：
按 `base_` 指针去重后逐条 `CreateAllocStatement`，再统一
`InsertAllocsIntoBody`。alloc 语句的构造在
[include/pypto/ir/transforms/utils/memref_utils.h:L311-L330](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/transforms/utils/memref_utils.h#L311-L330)——
`pinned` 时才追加 kwarg，普通编译器分配打印/比较保持原样；插入函数在
[include/pypto/ir/transforms/utils/memref_utils.h:L332-L353](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/transforms/utils/memref_utils.h#L332-L353)。

> **本版本变更（PR #2494）**：`InsertAllocsIntoBody` 从 init_memref.cpp 移到了
> memref_utils.h，与 `CreateAllocStatement` 比邻——因为 MemoryReuse 破环时的
> spill 分配也要「提升到函数体头」的同等待遇（见 4.5），两处共用一份实现。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到 InitMemRef 前后 IR 的差异。
2. **操作步骤**（示例代码，可在仓库外任意目录运行；环境要求同 u1-l2）：

```python
import pypto.language as pl
from pypto.pypto_core import passes, ir

@pl.program
class P:
    @pl.function
    def main(self, a: pl.Tensor[[64, 64], pl.FP32]) -> pl.Tensor[[64, 64], pl.FP32]:
        with pl.at(level=pl.Level.CORE_GROUP):
            t0: pl.Tile[[64, 64], pl.FP32, pl.MemorySpace.Vec] = pl.load(a, [0, 0], [64, 64])
            t1: pl.Tile[[64, 64], pl.FP32, pl.MemorySpace.Vec] = pl.add(t0, t0)
            return pl.store(t1, [0, 0], pl.Tensor[[64, 64], pl.FP32])

after = passes.init_mem_ref()(P.program)   # 单跑第 31 个 Pass
print(ir.python_print(after))
```

3. **需要观察的现象**：函数体头部出现 `mem_vec_N: pl.Ptr = pl.tile.alloc(pl.Mem.Vec, ...)`，
   每个 Tile 类型注解里多出 `pl.MemRef(mem_vec_N, -1, 16384)`。
4. **预期结果**：`addr=-1`（未分配）、DDR 参数（输入张量）**没有** tile.alloc、
   `tile.store` 的结果与输出参数共享 MemRef。待本地验证（打印细节以实际版本为准）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 DDR 的 MemRef 不生成 `tile.alloc`？
**答案**：DDR 是片外全局内存，由运行时管理地址（张量实参传入时已就位）；`tile.alloc` 只为片上空间（Vec/Mat/Left/Right/Acc/Bias）声明缓冲，见 `TransformInitMemRef` 第 3 步的收集逻辑（[src/ir/transforms/init_memref.cpp:L875-L898](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/init_memref.cpp#L875-L898)）。

**练习 2**：一个 Tile 到达 InitMemRef 时还没有 `memory_space_`，会发生什么？
**答案**：设备函数（InCore）内是内部错误（InferTileMemorySpace 声明覆盖所有设备函数）；设备函数之外是**用户错误**——报错提示「Tile 是片上硬件状态，请把它移进 InCore 函数」，不再默认 DDR（默认 DDR 会造出「全局内存里的 Tile」这种假放置），见 [src/ir/transforms/init_memref.cpp:L429-L449](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/init_memref.cpp#L429-L449)。

### 4.3 MaterializeSemanticAliases：语义必须的别名（第 32 个 Pass）

#### 4.3.1 概念说明

InitMemRef 之后，累加器链条还差一环：`iter_arg` 已经和 initValue 同缓冲，但
**yield 值的生产者**（如计算 `acc_next` 的 `tile.add`）还挂在自己的新缓冲上——
如果不去修补，「下一轮的累加」写进了另一块内存，循环就不累加了。这个 Pass
把「必须同缓冲」的关系补全，所以它**永远运行**，与内存规划器选择无关。

#### 4.3.2 核心流程

```text
对每个 ForStmt（自顶向下）：
  target = iter_arg 的规范 MemRef（即累加器缓冲）
  沿 yield 值的生产者链追踪（跟随 output-reuses-input 的原地算子与视图输入）
  收集所有应改写到 target 的变量 → RetypeApplier 原地重写类型
IfStmt 的返回值同理：两臂的 yield 都改写到 phi 缓冲
```

#### 4.3.3 源码精读

实现就是文件内的两个类：`TopDownRetargeter`（[src/ir/transforms/memory_reuse_pass.cpp:L289](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/memory_reuse_pass.cpp#L289) 起）
做分析，`RetypeApplier`（[src/ir/transforms/memory_reuse_pass.cpp:L844](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/memory_reuse_pass.cpp#L844) 起）做重写。
主入口 [src/ir/transforms/memory_reuse_pass.cpp:L3687-L3748](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/memory_reuse_pass.cpp#L3687-L3748)：

```cpp
FunctionPtr TransformMaterializeSemanticAliases(const FunctionPtr& func) {
  // Orchestration functions submit tasks and never hold TileType variables.
  if (func->func_type_ == FunctionType::Orchestration) return func;
  ...
  TopDownRetargeter retargeter;
  auto rewrites = retargeter.Compute(new_body);
  if (!rewrites.empty()) { RetypeApplier applier(std::move(rewrites)); new_body = applier.VisitStmt(new_body); }
```

这段代码做了什么：编排函数直接跳过；否则计算「谁必须改写到累加器缓冲」的重写集并应用。
**同一个函数的后半段是本讲的伏笔**：在 `DSA_RP` / `PTOAS` 模式下 MemoryReuse 整个被跳过，
所以这里立刻补跑 YieldFixup 等正确性归一化（[src/ir/transforms/memory_reuse_pass.cpp:L3721-L3741](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/memory_reuse_pass.cpp#L3721-L3741)）——
没有这一步，携带永远得不到更新，循环「静默变成 no-op」。

#### 4.3.4 代码实践

1. **实践目标**：确认 must-alias 在循环累加器上生效。
2. **操作步骤**：跑
   [tests/ut/ir/transforms/test_memory_reuse.py:L1900](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/transforms/test_memory_reuse.py#L1900)
   附近的 `TestYieldFixup::test_simple_loop_memrefs_unified`（命令：
   `python -m pytest tests/ut/ir/transforms/test_memory_reuse.py::TestYieldFixup -k simple_loop -v`，
   运行前按 u1-l2 完成构建）。
3. **需要观察的现象**：断言用 `ir.assert_structural_equal` 比较经过
   `_run_pipeline`（init_mem_ref → materialize_semantic_aliases → memory_reuse，
   [tests/ut/ir/transforms/test_memory_reuse.py:L31-L39](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/transforms/test_memory_reuse.py#L31-L39)）的
   Before/Expected 程序。
4. **预期结果**：测试通过；Expected 里生产者与 iter_arg 共用同一个 `mem_vec_N`。
   待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么这个 Pass 要从 MemoryReuse 里拆出来？
**答案**：DSA_RP / PTOAS 模式跳过机会式复用（各自用求解器/外部汇编器做复用），但语义别名是正确性要求不能跳。拆分后「跑 32 不跑 33」才是合法组合，见 [docs/en/dev/passes/32-materialize_semantic_aliases.md:L18-L29](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/32-materialize_semantic_aliases.md#L18-L29)。

**练习 2**：PTO codegen 如何从 IR 上看出「原地累加」？
**答案**：解析到**同一 MemRef 身份**（base+offset+size）的变量渲染成同一个 `tile_buf` 句柄，于是发射 `pto.tadd ins(%acc, %t) outs(%acc)` 而非写进新缓冲，见 [docs/en/dev/passes/32-materialize_semantic_aliases.md:L60-L69](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/32-materialize_semantic_aliases.md#L60-L69)。

### 4.4 MemoryReuse：生命周期装箱复用（第 33 个 Pass）

#### 4.4.1 概念说明

机会式复用的核心直觉：**生命周期不重叠的变量可以共用一块物理缓冲**。
MemRef 共享 = 多个变量类型的 `memref_` 指到同一个对象（或同一个 base），
片上峰值占用从「所有 Tile 之和」降到「同一时刻活跃 Tile 的装箱和」。

#### 4.4.2 核心流程

主入口 `TransformMemoryReuse`（[src/ir/transforms/memory_reuse_pass.cpp:L3750-L3880](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/memory_reuse_pass.cpp#L3750-L3880)），
步骤在注释里编号得很清楚：

```text
Step 1  AnalyzeAllocationLifetimes：全树遍历，算每个变量的 def 点/最后使用点
        （循环外定义、循环内使用 → 生命周期延长到循环尾；共享组按 base 合并成区间）
Step 2  AnalyzeAllocationConstraints：收集约束
        （910B load+tpop 危害输入、not_inplace_safe/forbid_output_alias 禁止别名表、
         作者声明分配的 pinned bases）
Step 3  IdentifyReuseOpportunities：first-fit-decreasing 装箱
        每个内存空间内：按 size 降序 → 逐个候选找第一个「全体成员都能与它共享」的缓冲
Step 3.5  AlignLoopCarriesToInit：把 iter_arg/return_var 重新对齐到（可能被复用改写的）initValue
Step 3.75 CoalesceAccumulatorIfPhis：合并手剥 split-K 产生的 if-phi 累加器
Step 3.9/4.5 NormalizeIdentityCopyBuffers（前后各一次，见 4.5）
Step 4  YieldFixupMutator：修补 yield/return_var 的缓冲错位（4.5 详述）
        ＋ 把破环 spill 的分配提升到函数体头
Step 5  RemoveUnusedAllocStatements：删除不再被引用的 tile.alloc
Step 6  StripPipelineMembership：剥掉已被消费的流水线阶段属性
```

生命周期判定有个精妙的「touching 允许」规则
（[src/ir/transforms/memory_reuse_pass.cpp:L1466-L1475](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/memory_reuse_pass.cpp#L1466-L1475)）：

\[ \text{overlap}(a, b) = \neg\,(a.\text{last\_use} \le b.\text{def} \;\vee\; b.\text{last\_use} \le a.\text{def}) \]

用 `<=` 的含义：前一个变量**最后使用**和后一个变量**定义**落在同一条语句上也允许共享，
因为一条语句内部「先读输入、后写输出」——生产者-消费者对（`b = f(a)`）天然可以原地。

#### 4.4.3 源码精读

**① 装箱与四道闸门**。装箱循环在
[src/ir/transforms/memory_reuse_pass.cpp:L2288-L2334](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/memory_reuse_pass.cpp#L2288-L2334)：
先按「size 降序、def 升序、下标」排序保证确定性，然后每个候选找第一个能装下的缓冲。
能否共享由 `can_share` 决定（[src/ir/transforms/memory_reuse_pass.cpp:L2259-L2269](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/memory_reuse_pass.cpp#L2259-L2269)）：

```cpp
auto can_share = [&](const LifetimeInterval& cand, const LifetimeInterval& member) {
  if (LifetimesOverlap(cand, member) && overlap_blocks_sharing(cand, member)) return false;
  if (hazard_blocks(cand, member) || hazard_blocks(member, cand)) return false;
  if (forbid_blocks(cand, member) || forbid_blocks(member, cand)) return false;
  if (pipeline_blocks(cand, member)) return false;  // symmetric — one call suffices
  return true;
};
```

这段代码做了什么：四道闸门依次是（1）生命周期重叠（phi 家族豁免）；（2）910B
load+tpop 危害；（3）算子语义禁止别名（`tile.recip`/`tile.move` 等读输入的同时写输出，
原地会损坏数据）；（4）软件流水阶段守卫（并发克隆不能合并，否则把流水串行化）。

**② 没有 shape/dtype 闸门**。注意 `can_share` 里**没有**形状/类型兼容性检查——
共享同一物理 MemRef 的 Tile 可以有不同 shape/dtype/TileView，因为 PTO codegen
给每个变量绑定自己的 `alloc_tile`（各自的静态形状/布局），正确性由上面精确的
禁止别名守卫保证，而不是粗粒度的「类型全等」。BF16 Tile 复用死掉的 FP32 Tile
缓冲是合法且有价值的（跨 dtype 复用）。

**③ alloc 清理**。[src/ir/transforms/memory_reuse_pass.cpp:L3542-L3570](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/memory_reuse_pass.cpp#L3542-L3570)：
共享落地后，收集仍被引用的 base 指针集合，把 `tile.alloc` 的 LHS 不在集合里的语句整条删掉。

**④ 作者声明分配（pinned）**。装箱前先把声明分配标记为 pinned
（[src/ir/transforms/memory_reuse_pass.cpp:L1986-L2004](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/memory_reuse_pass.cpp#L1986-L2004)）：
pinned 区间自己开槽（[L2306-L2311](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/memory_reuse_pass.cpp#L2306-L2311)），
别的候选也不许进来——作者命名一块分配就是要把两个缓冲的**独立性**留住
（合并会引入源代码没写的 WAR 依赖，让硬件串行化本可并行的拷贝）。

#### 4.4.4 代码实践

1. **实践目标**：亲眼看到「多个 Tile 指向同一个 mem_vec_N」的复用结果。
2. **操作步骤**（示例代码，接 4.2.4 的程序 `P`）：

```python
head = passes.materialize_semantic_aliases()(passes.init_mem_ref()(P.program))
after = passes.memory_reuse()(head)        # 只跑第 33 个 Pass
print(ir.python_print(after))
```

3. **需要观察的现象**：中间 Tile（如 `t1`、`t2`）的类型注解里出现**相同的**
   `mem_vec_N`；函数体头部的 `tile.alloc` 条数比 InitMemRef 之后少了。
4. **预期结果**：生命期串行错开的中间 Tile 收敛到一两个缓冲；`tile.store`
   的输出与输入参数仍各自独立（参数生命期贯穿全函数，无人能与之重叠）。
   更系统的开/关对比见第 5 节综合实践任务 A。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`b = tile.muls(a, 0.0)`（a 的最后使用就在这条语句）能原地复用 a 的缓冲吗？
**答案**：能。`a.last_use == b.def` 属于 touching，`LifetimesOverlap` 用 `<=` 判不相交；
这正是文档里的 Producer-Consumer Reuse 示例。

**练习 2**：把 `b = tile.muls(a, 0.0)` 换成 `b = tile.recip(a)` 还能原地吗？
**答案**：不能。`tile.recip` 声明 `not_inplace_safe`（高精度路径在写输出的同时还要读输入和临时草稿），
`forbid_blocks` 闸门会拒绝输出与任何输入共享物理缓冲。

**练习 3**：为什么装箱按 size 降序而不是程序定义顺序？
**答案**：first-fit-decreasing 先放大件，缓冲大小由首个（最大）成员定，后进的小成员零成本；
且「后定义的大块」也能收编「先定义的小块」。旧的定义顺序贪心有单向 size 闸门，
两个生命周期不相交但小块先定义的 Tile 永远合不到一起（见 [docs/en/dev/passes/33-memory_reuse.md:L61](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/33-memory_reuse.md#L61)）。

### 4.5 循环携带写回：排序、破环与拒绝（本版本重点更新）

> 对应提交 `4b01d98c`（PR #2494，issue #2481）。这是本讲义本次更新的核心增量。

#### 4.5.1 概念说明

`pl.yield_(a, b)` 的语义是**同时**重绑定所有携带变量——一轮结束时 lag2 拿到旧 lag1、
lag1 拿到新值，两句「同时成立」。但落地到 IR 只能是一串 `tile.move` 拷贝，
而拷贝是**串行**的。如果不加干预，一条拷贝可能读到另一条拷贝刚写掉的数据。

最经典的翻车形态是移位寄存器：

```python
for _i, (lag1, lag2) in pl.range(0, N, init_values=(v0, v1)):
    new_v = compute(...)
    r_lag1, r_lag2 = pl.yield_(new_v, lag1)   # lag2 = lag1; lag1 = new_v
```

若按 iter_arg 顺序先写 lag1 的缓冲，再执行的 lag2 写回读到的是**新** lag1——
循环从此携带 `lag2 == lag1`，数值静默错误。更糟的形态是 swap（`cur, prev = prev, cur`）：
每个携带的值住在对方的缓冲里，**任何顺序都不对**。最隐蔽的形态是两个携带从同一个
Tile 播种（`init_values=(seed, seed)`）——两个携带就是同一块缓冲，谁都保不住谁。

#### 4.5.2 核心流程

YieldFixupMutator 对每个 ForStmt 的处理（[src/ir/transforms/memory_reuse_pass.cpp:L2785-L2886](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/memory_reuse_pass.cpp#L2785-L2886)）：

```text
1. CollectCarryRanges：取每个携带（initValue）的 MemRef 字节区间
2. RejectOverlappingCarryBuffers：
   任意两个携带的字节区间重叠 → CHECK 报错（点名两个携带 + 修复建议）
3. 对每个 (yield 值 ≠ 携带缓冲) 的位置，造一条 tile.move 拷贝（CarryCopy）
   —— 比较用 CompareBaseAddress：kSame 跳过；kUnknown 报错；kDifferent 造拷贝
4. OrderCarryCopies：给拷贝定序
   ├─ 建冲突图：拷贝 A 的源区间 ∩ 拷贝 B 的目的区间 → 边 A→B（A 必须先跑）
   ├─ Kahn 拓扑排序输出顺序
   └─ 卡住（有环）→ Tarjan 找强连通分量 → 每个环挑一个受害者 spill 到临时缓冲
5. spill 语句（读旧缓冲→写临时）排在所有拷贝之前；
   spill 的 tile.alloc 通过 TakePendingAllocs 交给调用方提升到函数体头
6. PatchIterArgsAndReturnVars：把 iter_arg/return_var 的类型对齐到 initValue 缓冲
```

破环的正确性论证：spill 目的地是一块**全新分配**，没有别的拷贝会写它，
所以 spill 掉的拷贝的出边全部消失；受害者集合来自强连通分量（且分量内可能再分解），
保证剩余残图无环——不会把 scratch 花在「只是位于环下游」的无辜拷贝上。
复杂度是 \( O(k \log k + E) \)（k 为单个循环的携带数），
因为区间重叠查询走的是按起始地址排序的二分索引（`CarryRangeIndex`，
[src/ir/transforms/memory_reuse_pass.cpp:L3019-L3118](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/memory_reuse_pass.cpp#L3019-L3118)），
k 个不相交槽位是 k 次查找而不是 k² 次两两比较。

#### 4.5.3 源码精读

**① 拷贝定序（Kahn）**。[src/ir/transforms/memory_reuse_pass.cpp:L3186-L3255](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/memory_reuse_pass.cpp#L3186-L3255)：

```cpp
for (size_t i = 0; i < count; ++i) {
  carry_index.ForEachOverlapping((*copies)[i].src_memref, [&](size_t carry) {
    ...
    successors[i].push_back(hit->second);  // i reads what hit->second overwrites
    ++in_degree[hit->second];
  });
}
```

这段代码做了什么：为每条拷贝查「我的源区间压到了谁的携带区间」，被压的那个携带的拷贝
必须排在我后面（否则我读到的会是它写完的新值）。随后标准 Kahn 出序；
`ready` 空了还没排完 → 有环 → 进入破环。

**② 破环（Tarjan SCC + spill）**。
[src/ir/transforms/memory_reuse_pass.cpp:L3341-L3376](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/memory_reuse_pass.cpp#L3341-L3376)
的 `CycleVictims` 在残图上求强连通分量（迭代版 Tarjan 在
[L3263-L3322](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/memory_reuse_pass.cpp#L3263-L3322)，
避免深链爆栈），每个分量挑编号最小的成员当受害者——注释明确说
「最小反馈顶点集是 NP-hard，这里不保证最优」。spill 动作在
[src/ir/transforms/memory_reuse_pass.cpp:L3381-L3412](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/memory_reuse_pass.cpp#L3381-L3412)：

```cpp
const std::string base_name = "mem_" + space_str + "_carry_spill_" + std::to_string(spill_counter_++);
auto scratch_base = std::make_shared<Var>(base_name, GetPtrType(), copy->source->span_);
...
auto [spilled_var, spill_stmt] = CreateTileMove(copy->source, scratch_memref, src_memory);
spills->push_back(std::move(spill_stmt));
auto [moved_var, move_stmt] = CreateTileMove(spilled_var, copy->dst_memref, copy->dst_memory);
```

这段代码做了什么：造 `mem_vec_carry_spill_N` 新分配 → 先把源搬到 scratch →
原拷贝改为「从 scratch 搬到目的」。命名规则刻意避开 InitMemRef 的
`mem_<space>_<N>` 数字后缀，保证打印→再解析往返能重新绑定同一分配。

**③ 共享缓冲拒绝**。[src/ir/transforms/memory_reuse_pass.cpp:L3149-L3158](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/memory_reuse_pass.cpp#L3149-L3158)：

```cpp
CHECK_SPAN(false, for_stmt->span_)
    << "Loop-carried values '" << ... << "' and '" << ... 
    << "' share the same on-chip buffer, so one iteration cannot preserve both. Give each "
       "loop-carried tile its own initial value -- build them with separate ops instead of "
       "seeding both from the same tile.";
```

这段代码做了什么：两个携带的字节区间重叠时直接报 `pypto::ValueError`（CHECK = 用户错误），
在「携带还有名字」的地方报，而不是让下游退化的 `tile.move` 只报一个字节偏移。
注意判据是**字节区间**而非分配——同一多槽声明的两个不相交槽是合法的独立携带。

**④ 身份拷贝归一化的「前后夹逼」**。
[src/ir/transforms/memory_reuse_pass.cpp:L3836-L3865](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/memory_reuse_pass.cpp#L3836-L3865)
在 YieldFixup **前后各跑一次** `NormalizeIdentityCopyBuffersMutator`
（实现在 [L3600-L3666](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/memory_reuse_pass.cpp#L3600-L3666)）：

- **之前**跑：`lag2 = lag1` 这种纯改名（赋值右边是裸 Var，不是 Call）必须落在 lag1 的
  缓冲上，YieldFixup 才能**看见**冲突并排序——不归一化的话每个改名各占一块缓冲，
  冲突不可见，移位寄存器照样坍缩。
- **之后**跑：YieldFixup 自己的 IfStmt 修补会把 phi 的 return_var 指到规范分支缓冲，
  又会搁浅该 phi 下游的改名链——同样的病，另一头来。

mutator 幂等，找不到错配的那次运行是 no-op。

**⑤ Acc 空间的硬约束**。[src/ir/transforms/memory_reuse_pass.cpp:L3423-L3427](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/memory_reuse_pass.cpp#L3423-L3427)：
`CreateTileMove` 里 `INTERNAL_CHECK` 禁止 Acc→Acc 的 tile.move——910B 没有两个
L0C 地址之间的硬件搬运路径，残余的 Acc 错位必须由 Step 3.75 的 if-phi 合并提前解决，
解决不了就大声失败（内部错误），绝不生成 ptoas 会拒绝的 IR。

#### 4.5.4 代码实践

1. **实践目标**：用一个移位寄存器循环验证「写回拷贝被排序」。
2. **操作步骤**：

   - 运行三个回归测试（均在 PR #2494 中新增，且在旧版本 origin/main 上会失败）：

     ```bash
     source .claude/skills/testing/load-env.sh
     python -m pytest "tests/ut/ir/transforms/test_memory_reuse.py::TestYieldFixup::test_carry_writebacks_run_before_they_are_overwritten" \
        "tests/ut/ir/transforms/test_memory_reuse.py::TestYieldFixup::test_carry_writeback_cycle_is_broken_with_a_spill_buffer" \
        "tests/ut/ir/transforms/test_memory_reuse.py::TestYieldFixup::test_carries_sharing_one_buffer_are_rejected" -v
     ```

   - 精读第一个测试 [tests/ut/ir/transforms/test_memory_reuse.py:L2377-L2452](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/transforms/test_memory_reuse.py#L2377-L2452)：
     Before 里 `shifted = cur`（改名）与 `grown = add(cur, prev)`；Expected 里
     `shifted_mv`（写入 prev 的缓冲 mem_vec_3）排在 `grown_mv`（写入 cur 的缓冲 mem_vec_2）
     **之前**——与 iter_arg 顺序（cur 是第 0 个携带）相反。

3. **需要观察的现象**：
   - 测试 1 通过 = 拷贝按「先读后写」排序；
   - 测试 2 的 Expected 里出现 `mem_vec_carry_spill_0` 分配与三条 move（先 spill、再两条写回）；
   - 测试 3 抛出 `ValueError`，报错文案含 `share the same on-chip buffer`。
4. **预期结果**：三条全部通过（提交信息记录该测试文件 102 项全绿）。
   另可关注辅助断言 `_assert_carry_writebacks_do_not_clobber`
   （[tests/ut/ir/transforms/test_memory_reuse.py:L201-L241](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/transforms/test_memory_reuse.py#L201-L241)）：
   它直接检查不变量「没有任何写回拷贝读取已被更早写回覆盖的携带缓冲」。
   待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：`cur, prev = prev, cur`（swap）为什么没有任何合法顺序？
**答案**：cur 的新值（旧 prev）住在 prev 的缓冲里，prev 的新值（旧 cur）住在 cur 的缓冲里。
先写哪个，另一个的源就被毁掉——冲突图是一个二元环。解法是把一个成员先 spill 到
scratch，两个写回都安全。

**练习 2**：两个携带 `init_values=(seed, seed)` 为什么直接报错而不是也 spill？
**答案**：两个携带**是同一块缓冲**（同一字节区间），不是拷贝顺序问题：一轮里第一个携带的
任何写回都在覆盖第二个携带的存储。没有顺序、也没有 scratch 能救，属于作者层面的
建模错误，所以在还知道携带名字的地方用 CHECK 报给作者。

**练习 3**：为什么 OrderCarryCopies 用字节区间索引而不是两两比较携带 MemRef？
**答案**：两个目的：一是正确性粒度——同一多槽声明的不同槽共享 base 但字节区间不相交，
按分配比较会漏搬/误判；二是复杂度——区间按起始地址排序后重叠查询是二分加局部扫描，
k 个携带是 \( O(k \log k) \) 级而非 \( O(k^2) \)，符合项目对 Pass 的复杂度约束。

### 4.6 AllocateMemoryAddr：分发最终地址（第 34 个 Pass）

#### 4.6.1 概念说明

前三步只回答了「谁和谁共享一块分配」，最后一步回答「这块分配落在空间内的哪个地址」。
默认（PYPTO）策略是**确定性的顺序分配器**：每个内存空间一个从 0 起步的独立竞技场，
按名字排序逐个缓冲放置、32 字节对齐、最后做容量校验。

#### 4.6.2 核心流程

```text
1. 收集所有 MemRef（按空间分组）
2. policy.OrderMemRefs 排序（默认按 name_hint 升序，保证可复现）
3. 按 base_ 指针分组：同 base 的根 + 视图共用一个"槽"
   槽大小 = 组内最大成员（多槽声明取声明大小）
4. SpaceFootprint.OpenBuffer(size)：对齐 + 前进，返回该槽的起始地址
5. 每个成员地址 = 槽起始 + 自己的相对偏移（ConstInt 折叠；
   声明分配的运行期槽下标保留为表达式交给 codegen）
6. 容量校验：高水位 > Backend::GetMemSize(space) → CHECK 报错
```

#### 4.6.3 源码精读

核心函数 `AllocateMemoryAddresses`
（[src/ir/transforms/allocate_memory_addr_pass.cpp:L333-L496](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/allocate_memory_addr_pass.cpp#L333-L496)）：

```cpp
// 按空间分组 → 排序 → 按 base 分槽
SpaceFootprint footprint(space, policy, reserved_start);
for (const Var* base_key : base_order) {
  ...
  const uint64_t base_addr = footprint.OpenBuffer(buffer_size);
  for (const auto& old_memref : group) {
    ...
    member_addr_expr = std::make_shared<ConstInt>(static_cast<int64_t>(base_addr) + old_offset->value_, ...);
```

这段代码做了什么：视图槽共享——根 MemRef 落在 `base_addr`，`tile.slice` 视图落在
`base_addr + k*row_stride`（InitMemRef 算好的相对偏移在这里兑现）。注释点名了为什么
偏移要在这里折叠：`tile.reshape` of `tile.slice` 的链不再走 `pto.subview`，
它的 `pto.alloc_tile addr` 必须直接从 MemRef 偏移读（issue #1510）。

容量校验（[src/ir/transforms/allocate_memory_addr_pass.cpp:L454-L466](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/allocate_memory_addr_pass.cpp#L454-L466)）
就在这里做，因为只有这里的手印是**精确**的（连声明分配里没人绑定的空槽都算得到）。

分配策略抽成了 `MemoryAllocatorPolicy` 接口（`ShouldAllocate` / `AlignAddress` /
`OrderMemRefs`），后端可通过 `Backend::CreateMemoryAllocatorPolicy()` 覆写——
这是 u6-l3 BackendHandler「Pass 不写死后端分支」原则的又一实例。

#### 4.6.4 代码实践

1. **实践目标**：读懂地址分配后的 IR 形态。
2. **操作步骤**：把 4.2.4 的脚本再往后接两个 Pass：

```python
after = passes.allocate_memory_addr()(passes.memory_reuse()(after))
print(ir.python_print(after))
```

3. **需要观察的现象**：`pl.MemRef(mem_vec_2, 0, 16384)` 与
   `pl.MemRef(mem_vec_3, 16384, 16384)`——MemRef 的第二槽从 -1 变成了具体地址
   （0、16384…），`tile.alloc` 语句本身不变（它只是指针与尺寸的声明）。
4. **预期结果**：Vec 空间内地址从 0 开始、步进按尺寸 32 字节对齐；如果两个 Tile
   被 MemoryReuse 合并到同一 base，它们显示**相同**地址。待本地验证。

#### 4.6.5 小练习与答案

**练习 1**：为什么每个空间都从地址 0 开始，而不是全局连续编址？
**答案**：Vec/Mat/Left/Right/Acc 是物理上独立的缓冲，各有自己的地址总线与容量；
`Backend::GetMemSize(space)` 按空间查询上限，跨空间没有「连续」可言。

**练习 2**：地址分配后 `tile.alloc` 语句为什么还留在 IR 里？
**答案**：`tile.alloc` 声明的是**分配根**（指针 + 尺寸），地址写在 Tile/Tensor 类型的
MemRef 上；`MemRefUpdateMutator` 只替换类型中的 MemRef 引用与 `system.reserve_buffer`
的 base，alloc 声明原样保留给 codegen（见 [docs/en/dev/passes/34-allocate_memory_addr.md:L4-L17](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/34-allocate_memory_addr.md#L4-L17)）。

## 5. 综合实践

把两个任务串起来做一遍（环境搭建见 u1-l2，以下脚本为**示例代码**，建议放在仓库外的临时目录运行）。

### 任务 A：量化内存复用的收益

**步骤**：

1. 写一个「很多串行中间 Tile」的算子（生命期互相错开）：

```python
import pypto.language as pl
from pypto.pypto_core import passes, ir

@pl.program
class P:
    @pl.function
    def main(self, a: pl.Tensor[[64, 64], pl.FP32]) -> pl.Tensor[[64, 64], pl.FP32]:
        with pl.at(level=pl.Level.CORE_GROUP):
            t: pl.Tile[[64, 64], pl.FP32, pl.MemorySpace.Vec] = pl.load(a, [0, 0], [64, 64])
            for _i in pl.range(0, 3):
                t1: pl.Tile[[64, 64], pl.FP32, pl.MemorySpace.Vec] = pl.add(t, t)
                t2: pl.Tile[[64, 64], pl.FP32, pl.MemorySpace.Vec] = pl.mul(t1, t1)
                t = t2
            return pl.store(t, [0, 0], pl.Tensor[[64, 64], pl.FP32])
```

2. 手工组装两条流水线（镜像测试文件 `_run_pipeline` 的做法，
   [tests/ut/ir/transforms/test_memory_reuse.py:L31-L39](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/transforms/test_memory_reuse.py#L31-L39)）：

```python
head = passes.materialize_semantic_aliases()(passes.init_mem_ref()(P.program))
reused     = passes.allocate_memory_addr()(passes.memory_reuse()(head))
not_reused = passes.allocate_memory_addr()(head)
```

3. 用一个收集器（仿照测试里的 `_collect_allocated_tile_ranges`，
   [tests/ut/ir/transforms/test_memory_reuse.py:L42-L57](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/transforms/test_memory_reuse.py#L42-L57)）
   把每个 Tile 的 `(byte_offset, size)` 抓出来，按空间取
   \( \max(\text{offset} + \text{size}) \) 即高水位。

**观察与预期**：`reused` 的高水位明显小于 `not_reused`（理想情况下串行链收敛到两三个槽）；
从 `reused` 的打印里找出多个 Tile 指向同一个 `mem_vec_N`——那就是被复用的 MemRef；
`not_reused` 里 `tile.alloc` 条数多于 `reused`（Step 5 清理掉了不再使用的分配）。
**待本地验证**（具体数字取决于该版本 InferTileMemorySpace 的空间选择）。

### 任务 B：移位寄存器的三重验证

**步骤**：

1. 把 4.5.4 的三个测试跑通。
2. 自己改写 Before：把 `init_values=(head_0, tail_0)` 改成 `init_values=(seed, seed)`
   （两个初值来自同一个 `pl.load`），重新运行——应看到
   `ValueError: ... share the same on-chip buffer ...`。
3. 对照 Expected（[tests/ut/ir/transforms/test_memory_reuse.py:L2413-L2448](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/transforms/test_memory_reuse.py#L2413-L2448)）
   写一段笔记：`shifted_mv` 为什么必须排在 `grown_mv` 前面——用「拷贝 A 的源区间 ∩ 拷贝 B
   的目的区间 ⇒ A 先跑」的规则推导一遍。

**预期结果**：能独立推导出正确顺序，并说明若顺序反过来，第 2 轮起
`r_prev` 读到的就是本轮的 `grown` 而非上轮的 `cur`。

## 6. 本讲小结

- **MemRef 是内存规划的货币**：`base_`（分配身份）+ `byte_offset_` + `size_` 三要素；
  「同一分配」与「同一地址」是两个问题，`CompareBaseAddress` 的三值答案是后续一切决策的地基。
- **四部曲分工**：InitMemRef 发地契（视图/原地算子/别名继承、alloc 提升到函数体头）→
  MaterializeSemanticAliases 落实 must-alias（永远运行）→ MemoryReuse 做 may-alias
  装箱（FFD + 四道 `can_share` 闸门，无 shape/dtype 闸门）→ AllocateMemoryAddr
  分发地址（按空间独立竞技场 + 32 字节对齐 + 精确容量校验）。
- **生命周期判定允许 touching**：`last_use == def` 可共享，因为语句内先读后写；
  生产者-消费者对天然可原地。
- **（本版本更新）循环携带写回是「并行语义、串行落地」的冲突**：
  拷贝按「源区间压到谁的携带区间」建边做 Kahn 排序；环（swap）用 Tarjan SCC 挑受害者
  spill 到 `mem_*_carry_spill_N`；共享缓冲的携带（同 Tile 播种）直接 CHECK 拒绝。
  身份拷贝归一化在 YieldFixup 前后各跑一次才能让冲突「可见」。
- **内存规划器可插拔**：PYPTO（默认装箱）/ DSA_RP（求解器）/ PTOAS（外部汇编器）
  三种模式跳过不同 Pass，但语义别名与携带修补等正确性步骤始终保留。

## 7. 下一步学习建议

1. **下一讲 u5-l8（动手写一个新 Pass）**：本讲的 `TransformMemoryReuse` 步骤化注释
   （Step 1~6）就是最好的 Pass 编写范文——分析器（Visitor）与改写器（Mutator）分离、
   每步幂等、注释写「为什么」。
2. **u6-l1（PTO 代码生成）**：追踪本讲的产物如何变成 `.pto` 里的
   `pto.alloc_tile addr=...`——为什么「同一 MemRef 身份渲染成同一 tile_buf 句柄」
   是原地发射的前提。
3. **延伸阅读**：
   - [docs/en/dev/passes/33-memory_reuse.md](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/33-memory_reuse.md)
     的「Declared allocations」与「Ascend910B load + tpop_from_aic hazard」两节——
     本讲只点到，细节值得精读；
   - 软件流水阶段守卫涉及 u6-l5 的 `pl.pipeline`，学完那一讲再回头看
     `pipeline_blocks` 闸门会豁然开朗；
   - DSA_RP 求解器的完整模型见
     [docs/en/dev/passes/34-allocate_memory_addr.md](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/34-allocate_memory_addr.md) 的「DSA-RP policy」一节。
