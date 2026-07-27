# Expert 内存分配与 Cube/Vector Scope

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 Developer 模式与 Expert 模式在「内存分配」这一件事上的根本差别：一个把物理位置交给编译器推断，一个由你显式钉死。
- 熟练使用五个 Expert 内存分配原语 `T.alloc_ub` / `T.alloc_L1` / `T.alloc_L0A` / `T.alloc_L0B` / `T.alloc_L0C`，并知道它们各自绑定哪一块 Ascend 物理存储。
- 理解 `T.Scope("C")` / `T.Scope("V")` 如何在 TIR 层显式划分 Cube 与 Vector 两个执行域，以及它和 Developer 模式「自动 CV 分离」的对应关系。
- 能够把一个 Developer 写法的 GEMM 改写成「显式 `L1 → L0A/L0B → T.mma → L0C`」的 Expert 流水，并理解 Developer/Expert 混合编程的典型结构。

## 2. 前置知识

本讲建立在前面几讲之上，这里只做最简回顾，不重复展开：

- **Ascend 片上存储层级**（见 u1-l1、u3-l1）：GM（全局显存）→ L1（属 Cube 核）→ L0A/L0B/L0C（Cube 核内的寄存器级，分别存矩阵乘的 A、B 与累加结果 C）→ Unified Buffer / UB（属 Vector 核）。
- **两层前端抽象**（见 u3-l1）：tile-lang 把上面这些存储压缩为两层——`shared` 对应片上缓存 L1/UB，`fragment` 对应寄存器级 L0A/L0B/L0C。Developer 用 `T.alloc_shared` / `T.alloc_fragment`（scope 是 `dynamic`，由 `AscendInferBufferScope` pass 推断）；本讲的 Expert 用五个原语把 scope **直接钉死**。
- **Cube 与 Vector 的协作**（见 u1-l1）：A2/A3 上 Cube 和 Vector 是两个不同的核，二者通过 GM/L2 中转交换数据。
- **矩阵乘的两个入口**（见 u3-l3）：`T.gemm_v0`（Developer 块级，内部已包好 L1→L0A/L0B 搬运）与 `T.mma`（Expert 指令级，只发一条 `Mmad`，搬运要自己写）。

一个关键心智模型：**存储位置（scope）** 回答「数据放在哪块硬件」，**执行域（resource_scope）** 回答「这段代码在 Cube 核还是 Vector 核上跑」。本讲把这两件事都讲透。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilelang/language/allocate.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/allocate.py) | Developer 与 Expert 两套内存分配原语的实现，含 TIR scope 到 Ascend 存储的映射表。 |
| [tilelang/language/warpgroup.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/warpgroup.py) | `T.Scope` 的 Python 入口，构造一个 `ScopeFrame`。 |
| [src/ir.cc](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/ir.cc) | `Scope` 的 C++ 实现：把字符串 `"C"`/`"V"` 翻译成 `resource_scope` 属性 0/1。 |
| [tilelang/language/customize.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/customize.py) | `T.mma`（即 `npu_gemm`）的定义，Expert 模式的指令级矩阵乘。 |
| [examples/developer_mode/gemm_developer.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/gemm_developer.py) | 纯 Developer 写法的 GEMM，作为本讲实践的「改写起点」。 |
| [examples/developer_mode/matmul_add_developer.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/matmul_add_developer.py) | Developer 模式下 Cube（gemm）+ Vector（add）自动 CV 结合的范例。 |
| [testing/python/language/test_tilelang_ascend_language_l1_to_l0.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/testing/python/language/test_tilelang_ascend_language_l1_to_l0.py) | `explicit_l1_to_l0_gemm` 用例，给出 Expert「L1→L0A/L0B→mma→L0C」流水的标准写法，是本讲实践的参考答案。 |
| [README.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/README.md) | Quick Start GEMM 与高性能 GEMM 两段示例，分别演示 `T.Scope("C")` 的基本用法与手写 flag 流水。 |

---

## 4. 核心概念与源码讲解

### 4.1 两种抽象的取舍：Developer 与 Expert

#### 4.1.1 概念说明

TileLang 提供三套编程抽象（见 u1-l1）：Beginner（尚未支持）、Developer、Expert。本讲聚焦后两者在**内存分配**上的差别。

- **Developer 模式**：你只声明「语义层」需求——这是一块片上缓存（`alloc_shared`）还是一块寄存器级缓冲（`alloc_fragment`），但**不指定**它具体落在 L1、UB、L0A 还是 L0C。物理位置由 `AscendInferBufferScope` pass 根据它在算子里的使用上下文自动推断（见 u3-l1）。
- **Expert 模式**：你**显式指定**每一块缓冲的物理位置——`alloc_L1`、`alloc_ub`、`alloc_L0A`、`alloc_L0B`、`alloc_L0C` 一一对应五块物理存储，不再交给编译器推断。

为什么需要 Expert？因为极致性能往往要求你精确控制「数据走哪条搬运路径」「L0A/L0B 用多大」「哪段搬运和哪段计算重叠」。Developer 的 `T.gemm_v0` 把 `L1 → L0A/L0B` 的搬运藏在模板内部，对你透明；而 Expert 的 `T.mma` 只发一条 `Mmad` 指令，搬运和同步全靠你手写，换取最细的控制粒度。

两者不是二选一，而是**可以在同一个 kernel 内混用**：Programming Guide 明确指出「通常情况下，会是 Developer 和 Expert 两种方式结合的混合编程方式」。

#### 4.1.2 核心流程

两种模式的内存分配在编译链路里的位置：

```text
Developer: alloc_shared/alloc_fragment  (scope="shared"/"local.fragment", 标记为 dynamic)
                │
                ▼
        AscendInferBufferScope pass   ← 按使用上下文推断出 L1/UB/L0A/L0B/L0C
                │
                ▼
        后续搬运/计算 pass

Expert:   alloc_L1/alloc_ub/alloc_L0A/alloc_L0B/alloc_L0C  (scope 已钉死)
                │
                ▼
        直接按钉死的 scope 派发搬运指令 / 调用 mma
```

关键差别只有一个字：**dynamic vs 钉死**。Developer 的 scope 是 `dynamic`（一个占位符），Expert 的 scope 是写死的字符串。

#### 4.1.3 源码精读

五个 Expert 原语与两个 Developer 原语其实都是 `T.alloc_buffer` 的薄封装，差别只在传给 `scope=` 的字符串。看 [tilelang/language/allocate.py:128-157](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/allocate.py#L128-L157)，文件里直接附了一张 TIR scope 与 Ascend 物理存储的对照表：

```python
# allocate.py:128-157
"""
The following are memory scopes in Ascend.
- shared        -> dynamic (L1/UB, resolved by InferAllocScope)
- shared.l1     -> L1
- shared.ub     -> UB
- wmma.matrix_a -> L0A
- wmma.matrix_b -> L0B
- wmma.accumulator -> L0C
"""
def alloc_L1(shape, dtype):
    return T.alloc_buffer(shape, dtype, scope="shared.l1")
def alloc_L0A(shape, dtype):
    return T.alloc_buffer(shape, dtype, scope="wmma.matrix_a")
def alloc_L0B(shape, dtype):
    return T.alloc_buffer(shape, dtype, scope="wmma.matrix_b")
def alloc_L0C(shape, dtype):
    return T.alloc_buffer(shape, dtype, scope="wmma.accumulator")
def alloc_ub(shape, dtype):
    return T.alloc_buffer(shape, dtype, scope="shared.ub")
```

对照 Developer 的 [alloc_shared / alloc_fragment](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/allocate.py#L31-L60)：`alloc_shared` 默认 `scope="shared"`、`alloc_fragment` 默认 `scope="local.fragment"`——两者都不是某个具体物理存储，而是 `dynamic` 占位（`"shared"` 会被 `InferAllocScope` 进一步定为 `shared.l1` 或 `shared.ub`）。这就是「推断」与「钉死」在源码上的全部体现。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：用肉眼确认「五个 Expert 原语 = 五个写死的 scope 字符串」。
2. **操作步骤**：打开 `tilelang/language/allocate.py`，依次核对 `alloc_L1`/`alloc_L0A`/`alloc_L0B`/`alloc_L0C`/`alloc_ub` 的 `scope=` 实参。
3. **需要观察的现象**：它们的实现体都只有一行 `return T.alloc_buffer(...)`，没有任何推断逻辑。
4. **预期结果**：你会确认 Expert 原语纯粹是「换一个 scope 字符串」的便捷别名，所有推断工作都发生在 Developer 路径的 pass 里，而非这些函数里。

#### 4.1.5 小练习与答案

**练习 1**：如果我在 Expert 模式下用 `T.alloc_shared((128,128),"float16")` 分配一块缓冲，它的物理位置在编译期确定吗？

**答案**：不确定。`alloc_shared` 的 scope 是 `"shared"`（dynamic），仍会被 `AscendInferBufferScope` pass 推断。要钉死物理位置，必须用 Expert 原语（`alloc_L1` 或 `alloc_ub`）。这也正是「Expert 显式」的含义。

---

### 4.2 Expert 内存分配原语：五块物理存储各有一个入口

#### 4.2.1 概念说明

Expert 模式为 Ascend 的每一块片上存储都提供了一个一一对应的分配原语，让你把缓冲直接钉到目标存储：

| 原语 | TIR scope | 物理存储 | 所属核 | 典型用途 |
| --- | --- | --- | --- | --- |
| `T.alloc_L1(shape, dtype)` | `shared.l1` | L1 | Cube | 矩阵乘输入 A、B 的块缓存 |
| `T.alloc_L0A(shape, dtype)` | `wmma.matrix_a` | L0A | Cube | `mma` 的 A 操作数（寄存器级） |
| `T.alloc_L0B(shape, dtype)` | `wmma.matrix_b` | L0B | Cube | `mma` 的 B 操作数（寄存器级） |
| `T.alloc_L0C(shape, dtype)` | `wmma.accumulator` | L0C | Cube | `mma` 的累加结果 C（寄存器级） |
| `T.alloc_ub(shape, dtype)` | `shared.ub` | Unified Buffer | Vector | Vector 核上的逐元素/reduce 计算 |

一个直觉记忆法：**L 系列属于 Cube，UB 属于 Vector**。`L1` 是 Cube 的大缓存，`L0A/L0B/L0C` 是 Cube 内部紧挨着矩阵乘单元的三块小寄存器；`UB` 是 Vector 的主战场。这条线索直接决定了后面 `T.Scope("C")` / `T.Scope("V")` 该怎么用：操作 L1/L0A/L0B/L0C 的代码放进 `Scope("C")`，操作 UB 的代码放进 `Scope("V")`。

#### 4.2.2 核心流程

一个 Cube 矩阵乘的「数据旅程」与每段对应的 Expert 缓冲：

```text
GM ──copy──▶ L1 ──copy──▶ L0A / L0B ──mma──▶ L0C ──copy──▶ GM
            alloc_L1      alloc_L0A/L0B      alloc_L0C
            (MTE2 搬运)    (MTE1 搬运)         (fixpipe 搬出)
```

- 第一段 `GM → L1`：用 `alloc_L1` 接收，对应硬件的 MTE2（外部搬运到 L1）流水。
- 第二段 `L1 → L0A/L0B`：用 `alloc_L0A`/`alloc_L0B` 接收，对应 MTE1（L1 到 L0）流水。**这一段在 Developer 的 `T.gemm_v0` 里是藏在模板内的，Expert 模式下需要你用 `T.copy(A_L1, A_L0)` 显式写出来**。
- 第三段 `mma`：`alloc_L0A × alloc_L0B → alloc_L0C`，发一条 `Mmad` 指令。
- 第四段 `L0C → GM`：用 fixpipe 搬出。

Vector 侧则是 `GM → UB →（逐元素/reduce）→ GM`，缓冲用 `alloc_ub`。

#### 4.2.3 源码精读

README 的 Quick Start GEMM 直接展示了五块缓冲里三块的用法（L1、L0C），并用 `T.Scope("C")` 把整段计算包起来。见 [README.md:211-238](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/README.md#L211-L238)：

```python
# README.md:211-238（节选）
A_L1 = T.alloc_L1((block_M, K_L1), dtype)      # A 块钉到 L1
B_L1 = T.alloc_L1((K_L1, block_N), dtype)      # B 块钉到 L1
C_L0 = T.alloc_L0C((block_M, block_N), accum_dtype)  # 累加器钉到 L0C

with T.Scope("C"):                              # 显式 Cube 执行域
    loop_k = T.ceildiv(K, K_L1)
    for k in T.serial(loop_k):
        T.copy(A[bx * block_M, k * K_L1], A_L1)  # GM → L1
        T.copy(B[k * K_L1, by * block_N], B_L1)
        T.barrier_all()
        T.gemm_v0(A_L1, B_L1, C_L0, init=(k == 0))  # L1×L1 → L0C（内部 L1→L0）
        T.barrier_all()
    T.copy(C_L0, C[bx * block_M, by * block_N])  # L0C → GM
```

这里用的是 `T.gemm_v0`（Developer 块级接口），它内部已包好 `L1 → L0A/L0B`。如果想把这步也显式化，就要换成 `T.mma` 并自己写 `T.copy(A_L1, A_L0)`，这是 4.4 节和综合实践的内容。

`T.mma` 的真名是 `npu_gemm`，见 [tilelang/language/customize.py:115-228](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/customize.py#L115-L228)，并在 `__init__.py` 里被别名导出为 `mma`（[tilelang/language/__init__.py:75](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py#L75) `npu_gemm as mma`）。它发射 `tl.ascend_mma` intrinsic，模板签名为 `mma<dtypeA, dtypeC, M, N>`，只发一条 `Mmad`，搬运与同步留给用户。`init` 参数控制累加语义（首段清零）：

```python
# customize.py:222-228（节选）
mma_args = [f"mma<{_dtype(A)}, {_dtype(C)}, {M}, {N}>", Aptr, Bptr, Cptr, init, K_runtime]
...
return tir.call_intrin("handle", tir.op.Op.get("tl.ascend_mma"), *mma_args)
```

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：确认 `T.mma` 与 `T.gemm_v0` 在「谁负责 L1→L0 搬运」上的差别。
2. **操作步骤**：分别打开 [tilelang/language/customize.py:115](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/customize.py#L115)（`npu_gemm`/`mma`）和 [tilelang/language/ascend.py:343](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend.py#L343)（`gemm_v0`），对比模板字符串。
3. **需要观察的现象**：`mma` 的模板只有 `<dtype, dtype, M, N>`，不含 `kL0Size`；而 `gemm_v0` 的模板带 `kL0Size`（见 [ascend.py:442](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend.py#L442)）。
4. **预期结果**：`kL0Size` 是「L1→L0 的 K 轴切分粒度」，它的存在说明 `gemm_v0` 自己管这段搬运；`mma` 没有它，因为这段搬运交给你写了。这正是 Expert 比 Developer 多出来的那一层控制。

#### 4.2.5 小练习与答案

**练习 1**：把 README Quick Start 里的 `C_L0 = T.alloc_L0C((block_M, block_N), accum_dtype)` 改成 `T.alloc_fragment(...)`，会发生什么？

**答案**：`alloc_fragment` 的 scope 是 `local.fragment`（dynamic），`AscendInferBufferScope` 会根据它在 `gemm_v0` 中作为 C（累加器）的位置把它推断回 `wmma.accumulator`（即 L0C），所以多数情况下结果等价。但「钉死」的好处是**不依赖推断、行为可预测**，且能绕过推断 pass 的边界情况。Expert 写法偏好显式原语正是为此。

**练习 2**：为什么累加器 `C_L0` 的 dtype 通常用 `accum_dtype="float"`（fp32）而不是输入的 `float16`？

**答案**：K 维分块累加会反复相加，fp16 在大 K 时累加误差显著，故用更宽的 fp32 做累加器（L0C）保精度，最后搬出时再转回 fp16。这条规则在 u3-l1/u3-l3 已建立，Expert 模式同样适用。

---

### 4.3 T.Scope('C') / T.Scope('V')：显式划分 Cube/Vector 执行域

#### 4.3.1 概念说明

`T.Scope("C")` 和 `T.Scope("V")` 是 Expert 模式用来**显式声明执行域**的上下文管理器：

- `T.Scope("C")`：包起来的代码运行在 **Cube 核**上（操作 L1/L0A/L0B/L0C，发 `Mmad` 等）。
- `T.Scope("V")`：包起来的代码运行在 **Vector 核**上（操作 UB，发 `Add`/`Mul`/`Reduce` 等向量指令）。

它解决的问题是：当你在同一个 kernel 里既写 Cube 计算（矩阵乘）又写 Vector 计算（逐元素、softmax、reduce）时，编译器需要知道每段代码归谁执行。**Developer 模式下这件事是自动的**——`CombineCV` pass 配合 `AscendInferBufferScope` 会按缓冲 scope 自动把语句分到 Cube/Vector 两个执行域，并插入同步（见 u5-l1）。**Expert 模式下你用 `T.Scope` 自己划**，换取对「哪段归 Cube、哪段归 Vector、中间怎么同步」的完全掌控。

#### 4.3.2 核心流程

`T.Scope` 在 TIR 层的落地非常轻：它不创建新的循环或线程，只是给包起来的 block 打一个 `resource_scope` 属性：

```text
Python:  with T.Scope("C"): <语句>
   │  warpgroup.py: Scope("C") → _ffi_api.Scope("C")
   ▼
C++ ir.cc: Scope("C") → AttrFrame(resource_scope = 0)   # "V" → 1
   ▼
TIR:     attr [block] "resource_scope" = 0 { <语句> }
   ▼
后续 pass（CombineCV 等）按 resource_scope 把语句分到 Cube/Vector 域
```

属性值约定：`0 = Cube`，`1 = Vector`。这个映射关系定义在 C++ 端，下一节看源码。

> 注意：`T.Scope` 划的是**执行域**（这段代码在哪个核跑），与缓冲的**存储 scope**（数据放在哪块存储）是两个正交的概念。实践中二者高度相关——操作 L1/L0A/L0B/L0C 的语句天然属于 Cube 域，操作 UB 的语句天然属于 Vector 域。

#### 4.3.3 源码精读

Python 入口在 [tilelang/language/warpgroup.py:66-94](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/warpgroup.py#L66-L94)，`Scope(name)` 只是把名字透传给 C++ 的 `_ffi_api.Scope`：

```python
# warpgroup.py:66-94（节选）
@register_object("tl.ScopeFrame")
class ScopeFrame(TIRFrame):
    """... manages warp group indices and handles the entry and exit of the kernel launch scope."""

def Scope(name):
    return _ffi_api.Scope(name)
```

真正把 `"C"`/`"V"` 翻成数字的是 C++ 端，见 [src/ir.cc:495-506](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/ir.cc#L495-L506)：

```cpp
// ir.cc:495-506
ScopeFrame Scope(String scope_name) {
  ObjectPtr<ScopeFrameNode> n = make_object<ScopeFrameNode>();
  int scope_id = 0;
  if (scope_name == "V")
    scope_id = 1;
  AttrFrame attr_frame = Attr(Integer(0), "resource_scope", Integer(scope_id));
  n->frames.push_back(attr_frame);
  return ScopeFrame(n);
}
TVM_REGISTER_GLOBAL("tl.Scope").set_body_typed(Scope);
```

可以清楚看到：默认 `scope_id = 0`（Cube），只有当名字是 `"V"` 时才置 `1`（Vector），随后构造一个 `resource_scope` 属性帧。`ScopeFrame` 本身是一个容器帧（[ir.cc:462-487](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/ir.cc#L462-L487)），进入时调用内含 `AttrFrame` 的 `EnterWithScope`，退出时逆序 `ExitWithScope`，从而把 `with T.Scope(...)` 体里的语句都包进这个属性。

`CombineCV` pass 正是消费 `resource_scope`（注释明确写 `0: cube, 1: vec`）来区分 Cube/Vector 语句，见 [src/transform/ascend_combinecv.cc:44](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_combinecv.cc#L44)。在 Developer 模式下，pass 会自动给语句打上这个标记；在 Expert 模式下，标记由你的 `T.Scope` 提供。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：看清 `T.Scope("C")` 在最终 TIR 里就是一个属性，不是循环。
2. **操作步骤**：写一个最小的 `T.Scope("C")` GEMM（可直接用 README 的 Quick Start 段），编译后用 `func.get_kernel_source()` 之外的途径——观察 `lower` 的中间 IR。若不方便打印 IR，则阅读 [src/ir.cc:495](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/ir.cc#L495) 与 [ascend_combinecv.cc:44](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/transform/ascend_combinecv.cc#L44) 两处。
3. **需要观察的现象**：`Scope` 只产生 `resource_scope` 属性，没有任何 `threadIdx` 绑定或循环结构。
4. **预期结果**：理解 `T.Scope` 是「给 block 贴标签」，真正把 Cube/Vector 代码物理分离到两个核的工作由 `CombineCV` pass 完成（u5-l1 详述）。

#### 4.3.5 小练习与答案

**练习 1**：如果我把一段操作 UB 的逐元素加法（`T.tile.add`）错误地放进 `with T.Scope("C")`，会怎样？

**答案**：`T.Scope` 只是贴 `resource_scope` 属性，并不强制校验语句与缓冲的匹配。但 UB 缓冲的 scope 是 `shared.ub`（属于 Vector 存储），把它标成 Cube 域会导致后续 pass 在 Cube 核上引用一块 Vector 存储，要么报错、要么行为不符合预期。正确做法是按缓冲所属核选 Scope：操作 UB 用 `"V"`，操作 L1/L0A/L0B/L0C 用 `"C"`。

---

### 4.4 Developer/Expert 混合编程与 Expert GEMM 流水

#### 4.4.1 概念说明

真实的高性能算子往往是「Developer 主体 + Expert 微调」的混合体。三种典型结构：

1. **纯 Developer**：全部用 `alloc_shared`/`alloc_fragment` + `T.gemm_v0`/`T.Parallel`，CV 分离与同步全自动。如 [examples/developer_mode/gemm_developer.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/gemm_developer.py)。
2. **Developer 写法 + 显式 scope 原语**：缓冲用 Expert 原语钉死（`alloc_L1`/`alloc_L0C`），但计算仍用 `T.gemm_v0`，并用 `T.Scope("C")` 显式声明执行域。如 README Quick Start。
3. **完全 Expert**：缓冲全用 Expert 原语，`L1→L0A/L0B` 自己 `T.copy`，矩阵乘用 `T.mma`，同步自己写 `set_flag/wait_flag`。如 README 高性能 GEMM 和本节重点的测试用例。

本节聚焦第 3 种里最核心的那条流水：`L1 → L0A/L0B → mma → L0C`。

#### 4.4.2 核心流程

把 Developer 的 `T.gemm_v0(A_L1, B_L1, C_L0)` 展开成 Expert 三步：

```text
Developer（一步）:                   Expert（三步）:
T.gemm_v0(A_L1, B_L1, C_L0)    →    T.copy(A_L1, A_L0)   # L1 → L0A，显式
  （内部含 L1→L0A/L0B）                T.copy(B_L1, B_L0)   # L1 → L0B，显式
                                       T.mma(A_L0, B_L0, C_L0)  # 只发 Mmad
```

展开后你获得了两个新控制点：**L1→L0 的搬运时机与缓冲副本数**（可做乒乓/多缓冲）、以及 `mma` 的 `init`/`unit_flag` 流水参数。这是高性能 GEMM 双缓冲流水的基础（详见 u7-l2），本讲只验证正确性。

#### 4.4.3 源码精读

最干净的 Expert 流水范例是测试用例 `explicit_l1_to_l0_gemm`，见 [testing/python/language/test_tilelang_ascend_language_l1_to_l0.py:250-279](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/testing/python/language/test_tilelang_ascend_language_l1_to_l0.py#L250-L279)。它用五个 Expert 原语中的四个（L1、L0A、L0B、L0C），完整呈现 `GM→L1→L0A/L0B→mma→L0C→GM`：

```python
# test_tilelang_ascend_language_l1_to_l0.py:260-277（节选）
A_L1 = T.alloc_L1((block_M, block_K), dtype)
B_L1 = T.alloc_L1((block_K, block_N), dtype)
A_L0 = T.alloc_L0A((block_M, block_K), dtype)
B_L0 = T.alloc_L0B((block_K, block_N), dtype)
C_L0 = T.alloc_L0C((block_M, block_N), accum_dtype)

with T.Scope("C"):
    loop_k = T.ceildiv(K, block_K)
    for k in T.serial(loop_k):
        T.copy(A[bx * block_M, k * block_K], A_L1)   # GM → L1
        T.copy(B[k * block_K, by * block_N], B_L1)
        T.copy(A_L1, A_L0)                            # L1 → L0A（显式！）
        T.copy(B_L1, B_L0)                            # L1 → L0B（显式！）
        T.mma(A_L0, B_L0, C_L0, init=(k == 0))        # L0A×L0B → L0C
    T.copy(C_L0, C[bx * block_M, by * block_N])       # L0C → GM
```

对比纯 Developer 的 [examples/developer_mode/gemm_developer.py:41-54](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/gemm_developer.py#L41-L54)，后者用 `alloc_shared`/`alloc_fragment` + `T.gemm_v0`，没有 `alloc_L0A/L0B`、没有显式 `L1→L0` 搬运、也没有 `T.Scope`：

```python
# gemm_developer.py:41-54（节选）
A_L1 = T.alloc_shared((block_M, K_L1), dtype)      # dynamic scope
B_L1 = T.alloc_shared((K_L1, block_N), dtype)
C_L0 = T.alloc_fragment((block_M, block_N), accum_dtype)
for k in T.serial(loop_k):
    T.copy(A[bx * block_M, k * K_L1], A_L1)
    T.copy(B[k * K_L1, by * block_N], B_L1)
    T.gemm_v0(A_L1, B_L1, C_L0, init=(k == 0))      # 内部含 L1→L0
T.copy(C_L0, C[bx * block_M, by * block_N])
```

两段代码计算等价（都是 C=A×B 的分块累加），但 Expert 版把 `L1→L0` 这一步从模板里掏出来变成了两条可见的 `T.copy`，这就是「控制粒度更细」的具体含义。

混合编程（Developer + 自动 CV 结合）的范例见 [examples/developer_mode/matmul_add_developer.py:43-66](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/matmul_add_developer.py#L43-L66)：Cube 侧用 `alloc_shared` 做 `gemm_v0`，Vector 侧用 `alloc_shared`（推断为 UB）做 `T.tile.add`，开启 `TL_ASCEND_AUTO_CV_COMBINE` / `TL_ASCEND_AUTO_CV_SYNC` 后编译器自动把两段分到 Cube/Vector 并插同步——这正是 `T.Scope` 在 Developer 模式下被自动完成的事。

#### 4.4.4 代码实践（动手改写）

这是本讲的主实践任务，与讲义规格的 `practice_task` 对应。

1. **实践目标**：把 Developer 写法的 GEMM 改写成显式 `L1 → L0A/L0B → T.mma → L0C` 的 Expert 流水，并用 `T.Scope("C")` 包裹计算域。
2. **操作步骤**：
   - 复制 `examples/developer_mode/gemm_developer.py` 为 `my_expert_gemm.py`（放在你自己的工作目录，**不要改源码仓库里的文件**）。
   - 把 `A_L1 = T.alloc_shared(...)` 改成 `T.alloc_L1(...)`，`B_L1` 同理；把 `C_L0 = T.alloc_fragment(...)` 改成 `T.alloc_L0C(...)`。
   - 新增 `A_L0 = T.alloc_L0A((block_M, K_L1), dtype)` 和 `B_L0 = T.alloc_L0B((K_L1, block_N), dtype)`。
   - 把循环体里的 `T.gemm_v0(A_L1, B_L1, C_L0, init=(k == 0))` 替换为两条显式搬运 + 一条 `mma`：
     ```python
     T.copy(A_L1, A_L0)
     T.copy(B_L1, B_L0)
     T.mma(A_L0, B_L0, C_L0, init=(k == 0))
     ```
   - 用 `with T.Scope("C"):` 把 K 循环和最后的 `T.copy(C_L0, ...)` 包起来（参考 test 用例 [test_tilelang_ascend_language_l1_to_l0.py:266-277](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/testing/python/language/test_tilelang_ascend_language_l1_to_l0.py#L266-L277)）。
   - `pass_configs` 保持 `gemm_developer.py` 原有的 `TL_ASCEND_AUTO_SYNC` 等开启，让自动同步帮你兜底（Expert 不排斥自动同步）。
3. **需要观察的现象**：运行后应看到 `Kernel Output Match!`。
4. **预期结果**：在真实 NPU（含 CANN/bisheng）环境下可运行通过；若用 `func.get_kernel_source()` 查看生成代码，会看到 `copy_l1_to_l0a` / `copy_l1_to_l0b` 与 `mma` 模板被分别生成，而非 `gemm_v0` 一个大模板。
5. **若无 NPU 环境**：明确标注「待本地验证」。可退而求其次：对照 [test_tilelang_ascend_language_l1_to_l0.py:250-279](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/testing/python/language/test_tilelang_ascend_language_l1_to_l0.py#L250-L279) 逐行核对改写是否与参考答案一致——该测试在 CI 里以 `TARGETS = ["ascendc", "pto"]` 两条后端验证（[test_tilelang_ascend_language_l1_to_l0.py:125](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/testing/python/language/test_tilelang_ascend_language_l1_to_l0.py#L125)），是可信参考。

#### 4.4.5 小练习与答案

**练习 1**：把上面 Expert 版的 `T.mma(A_L0, B_L0, C_L0, init=(k == 0))` 里的 `init=(k == 0)` 去掉（恒为 `False`），结果会怎样？

**答案**：`mma` 的 `init=True` 会先把 L0C 清零再累加，`False` 则在旧值上累加。若每段都不 init，C_L0 从未清零，累加结果会叠加上一块脏数据，最终 `Kernel Output Match!` 失败。所以分块累加必须在首段 `init=True`（等价于 `k==0`），这与 u3-l3 的 `gemm_v0` init 语义一致。

**练习 2**：Expert 版相比 Developer 版多了 `A_L0`/`B_L0` 两块缓冲，它们的好处是什么？

**答案**：把 `L1→L0A/L0B` 暴露成独立语句后，你可以对 `A_L0`/`B_L0` 做多版本化（乒乓/多缓冲），让第 `k+1` 段的搬运与第 `k` 段的 `mma` 重叠——这正是软件流水（u3-l6）和双缓冲（u7-l2）的前提。Developer 的 `gemm_v0` 把这段藏在模板内，无法从外部做这种重叠调度。

---

## 5. 综合实践

把本讲的「显式 scope 原语 + `T.Scope`」与前面 u3 的「Cube + Vector 协作」结合起来，写一个 **Expert 风格的 matmul + add**：

- **Cube 段**（`with T.Scope("C")`）：用 `alloc_L1`/`alloc_L0A`/`alloc_L0B`/`alloc_L0C` 与显式 `L1→L0` 搬运 + `T.mma` 算出 `C_L0 = A×B`，搬到一个 `alloc_ub` 的 `c_ub`（触发 L0C→UB 的跨 CV 搬运，见 u3-l2/u5-l4）。
- **Vector 段**（`with T.Scope("V")`）：用 `alloc_ub` 读入 `D`，用 `T.tile.add(c_ub, c_ub, d_ub)` 做逐元素加，再搬回 GM。
- **同步**：先开启 `TL_ASCEND_AUTO_CV_SYNC` 让编译器自动插核间同步；跑通后，尝试参考 README 高性能 GEMM（[README.md:243-328](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/README.md#L243-L328)）把手写 `set_flag/wait_flag` 替换进去，对比自动同步与手写同步生成代码的差异。

参考起点：[examples/developer_mode/matmul_add_developer.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/matmul_add_developer.py)（Developer 版的 matmul+add）。验收标准：输出 `Kernel Output Match!`，且 `get_kernel_source()` 里能同时看到 Cube 的 `mma` 与 Vector 的 `Add` 两类指令。

## 6. 本讲小结

- Expert 与 Developer 在内存分配上的全部差别是：Developer 用 `alloc_shared`/`alloc_fragment`（scope 为 dynamic，由 `AscendInferBufferScope` 推断），Expert 用五个原语把 scope **直接钉死**到 `shared.l1`/`shared.ub`/`wmma.matrix_a`/`wmma.matrix_b`/`wmma.accumulator`。
- 五块物理存储各有入口：`alloc_L1`（L1）、`alloc_ub`（UB）、`alloc_L0A`/`alloc_L0B`/`alloc_L0C`（Cube 寄存器级）。记忆线索：**L 系列属 Cube，UB 属 Vector**。
- `T.Scope("C")`/`T.Scope("V")` 不是循环也不是线程绑定，只是给 block 贴一个 `resource_scope` 属性（C→0，V→1，见 `src/ir.cc`），后续 `CombineCV` pass 据此把语句分到 Cube/Vector 执行域。Developer 模式下这个标记由 pass 自动打。
- 把 Developer 的 `T.gemm_v0` 展开成 Expert 的 `T.copy(L1,L0A/L0B)` + `T.mma`，本质是把「L1→L0 搬运」从模板内部掏出来变成可见语句，从而获得更细的搬运/缓冲控制权。
- Developer 与 Expert 可以在同一 kernel 内混用；`pass_configs` 里的 `TL_ASCEND_AUTO_SYNC` 等自动机制与 Expert 显式控制并不冲突，常搭配使用。

## 7. 下一步学习建议

- **u4-l2 同步原语**：本讲的 Expert 流水依赖自动同步兜底，下一讲正式讲 `T.set_flag`/`T.wait_flag`/`T.barrier_all`/`T.set_cross_flag`/`T.wait_cross_flag`，让你能完全手写流水同步（对应 README 高性能 GEMM 的写法）。
- **u4-l3 自动同步插入**：深入 `AscendSyncInsert` pass，看清自动同步到底插了哪些 flag，理解本讲「兜底」背后的机制。
- **u4-l4 布局标注与 L2 Swizzle**：本讲的 `alloc_L1` 缓冲默认带 zN 布局，下一讲讲 `T.annotate_layout` 与 `T.use_swizzle`，把 Expert GEMM 的 L1 布局与 L2 局部性优化也补齐。
- **u5-l1 Cube/Vector 分离与 CombineCV**：本讲提到 `CombineCV` 消费 `resource_scope`，下一单元第一讲深入自动 CV 分离与 `auto_cv_combine`/`auto_cv_sync` 开关，补上 Developer 模式自动版的全貌。
- 推荐对照阅读 [docs/TileLang-Ascend Programming Guide.md 第 4.2 节](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/TileLang-Ascend%20Programming%20Guide.md)（Expert 模式）与 [README.md 高性能 GEMM 段](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/README.md#L243)。
