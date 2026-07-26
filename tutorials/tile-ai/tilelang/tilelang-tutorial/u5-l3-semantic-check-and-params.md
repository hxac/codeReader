# 语义检查与参数抽取

## 1. 本讲目标

本讲承接 u5-l1（eager builder 与 prim_func 转换）。当你写完一个 tilelang kernel、Python 函数已经被翻译成 TIR `PrimFunc` 之后，编译器在进入真正的 Pass 流水线**之前**，还会做两件容易被忽视、但对正确性至关重要的事：

1. **编译期语义检查**：`PreLowerSemanticCheck` 对 kernel 的循环结构做一次「与后端无关」的合法性校验，把结构上注定会出问题的 kernel 尽早拦下。
2. **参数抽取与产物封装**：`extrac_params` 把 `PrimFunc` 的参数描述成 `KernelParam` 列表，再把 lower 的全部产物打包成 `CompiledArtifact`，交给运行时 adapter 使用。

学完本讲，你应当能够：

- 说清 `PreLowerSemanticCheck` 到底检查什么、**不**检查什么，以及如何用 `PassConfigKey` 关闭它；
- 读懂 `KernelParam` 如何从 `PrimFunc` 的 `buffer_map` / `params` 抽取张量与标量参数；
- 解释 `CompiledArtifact` 承载了哪些产物，以及它如何流向 JIT 层与 adapter。

## 2. 前置知识

阅读本讲前，你应当已经了解（来自 u4-l1 / u5-l1）：

- **PrimFunc 与 IRModule**：tilelang 的 kernel 在 IR 层是一个 TVM TIR `PrimFunc`，多个函数装进 `IRModule`。
- **lower 主链路**：`tilelang.lower()` → `lower_to_host_device_ir()` 把 DSL 一步步变成可编译 IR，再生成设备源码。
- **Pass 与 PassContext**：Pass 是 `IR→IR` 的变换；`PassContext` 是 Pass 运行时的配置容器，里面的开关（`PassConfigKey`）控制各 Pass 的行为。
- **buffer_map**：`PrimFunc` 的张量参数是 `Var`，通过 `func.buffer_map` 映射到带 dtype/shape 的 `Buffer`；不在 `buffer_map` 里的 `Var` 是标量参数。

一个本讲要用到的小概念：**tile op**。在 IR 层，`T.copy`、`T.gemm`、`T.fill` 这类 DSL 原语一开始只生成一个 `tl.tileop.*` 的 intrinsic 占位调用（见 u3-1/u5-2），它带有 `TLOpBuilder` 属性。后续的 `lower_tile_op` Pass 才把它们展开成真正的底层指令。判断一个调用是不是 tile op，就看它有没有这个属性。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `tilelang/engine/semantic_check.py` | `PreLowerSemanticCheck` 入口：决定是否运行检查、运行哪些检查器 |
| `tilelang/analysis/nested_loop_checker.py` | 检查 tile 循环嵌套结构（Parallel/Pipelined/serial 的合法组合） |
| `tilelang/analysis/fragment_loop_checker.py` | 检查 fragment 缓冲能否被符号范围的 T.Parallel 索引 |
| `tilelang/analysis/ast_printer.py` | 可选：把 IR AST 打印成树，辅助调试 |
| `tilelang/engine/param.py` | `KernelParam`（参数描述）与 `CompiledArtifact`（编译产物）两个数据类 |
| `tilelang/engine/lower.py` | `extrac_params` 抽取参数；`lower()` 组装并返回 `CompiledArtifact` |
| `tilelang/transform/pass_config.py` | `PassConfigKey` 枚举，定义关闭语义检查的开关 |
| `docs/compiler_internals/tensor_checks.md` | host 侧 tensor 自动校验文档（与 pre-lower 检查是两套机制，本讲会专门区分） |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：编译期语义检查（`tilelang.engine.semantic_check`）、参数描述与抽取（`tilelang.engine.param` 的 `KernelParam`）、编译产物封装（`tilelang.engine.param` 的 `CompiledArtifact`）。

### 4.1 编译期语义检查：PreLowerSemanticCheck

#### 4.1.1 概念说明

`PreLowerSemanticCheck` 是 lower 流程里的一道「早期守门员」：在 `determine_target` 之后、Pass 流水线之前运行，对 kernel 做一次**与后端无关**的结构合法性校验。

为什么需要它？tilelang 的 tile 级循环原语有结构约束：

- `T.Parallel` 表达**线程级数据并行**（由 `LayoutInfer` 映射到 GPU 线程，见 u2-3）。
- `T.copy` / `T.gemm` 这类 **tile op** 本身会展开成「跨线程的集体操作」（搬运一整块数据、调用张量核）。

如果把一个 tile op 放进 `T.Parallel` 体内，就等于「在已经并行的循环里再要求集体协作」，语义冲突；同样，把 `T.Pipelined`（软件流水线，见 u3-3）嵌套进 `T.Parallel` 也会产生无法调度的结构。这类问题如果不在早期拦住，要么后续 Pass 崩溃，要么生成错误 kernel。**越早报错越好**，所以 tilelang 选择在 lower 一开始就检查。

> **关键澄清——它「不」检查什么。** 这是本讲最容易混淆的点，务必记住：
>
> - 它**不**检查数组越界（OOB）。越界访问由 `LegalizeSafeMemoryAccess` Pass 自动加运行时 guard 处理（见 u2-3）。
> - 它**不**检查 dtype / shape / strides / device 是否匹配调用方传入的张量。这些在 kernel **被调用时**由 host 侧自动插入的 tensor check 负责（详见 `docs/compiler_internals/tensor_checks.md`），属于**运行期、调用点**检查，不在编译期 pre-lower 阶段。
> - 它只检查**循环结构的合法性**与 **fragment 在符号范围 T.Parallel 中的索引合法性**。

也就是说，「语义检查」在这里是一个**范围有限、专门针对 tile 循环结构**的早期校验，不是万能合法性检查器。

#### 4.1.2 核心流程

`lower_to_host_device_ir` 的步骤（承接 u4-1）：

```text
PrimFunc
  → 封装进 IRModule
  → determine_target(target)            # 解析 target
  → PreLowerSemanticCheck(mod)          # ← 本模块焦点：Pass 流水线之前
  → resolve_pipeline(target).lower(mod) # 跑约 50 个 Pass
  → Filter(is_host_call / is_device_call)  # 拆 host / device
```

`PreLowerSemanticCheck` 内部三步，每一步都受 `PassConfigKey` 控制：

```text
若 TL_DISABLE_PRELOWER_SEMANTIC_CHECK == True → 直接返回，什么都不做
否则：
  1.（可选）若 TL_AST_PRINT_ENABLE == True → ASTPrinter()(mod)   # 打印 AST 树，调试用
  2. NestedLoopChecker()(mod)            # 检查循环嵌套规则
  3. FragmentLoopChecker()(mod)          # 检查 fragment 在符号并行循环中的索引
```

三个检查器都是 `prim_func_pass`（`opt_level=0`），它们**只读不改 IR**——发现违规就抛 `ValueError`，否则原样返回。

#### 4.1.3 源码精读

先看入口函数。`PreLowerSemanticCheck` 的全部逻辑非常短：

[tilelang/engine/semantic_check.py:21-31](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/semantic_check.py#L21-L31) — 检查入口：先看开关是否启用，启用才依次跑 AST 打印、嵌套循环检查、fragment 检查。

开关判定函数读的是当前 `PassContext` 的配置：

[tilelang/engine/semantic_check.py:9-12](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/semantic_check.py#L9-L12) — `should_enable_prelower_semantic_check`：当配置里 `TL_DISABLE_PRELOWER_SEMANTIC_CHECK` 为真时返回 `False`（即「不应启用」），否则返回 `True`。

对应开关在 `PassConfigKey` 枚举里的定义：

[tilelang/transform/pass_config.py:55-56](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/pass_config.py#L55-L56) — `TL_DISABLE_PRELOWER_SEMANTIC_CHECK = "tl.disable_prelower_semantic_check"`，默认 `False`。

> 顺带一提，AST 打印的开关是 `TL_AST_PRINT_ENABLE`（[tilelang/transform/pass_config.py:191](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/transform/pass_config.py#L191)）。打开它会在检查阶段把 IR 结构以树形打印到 stdout，是排查「我的 kernel 长出了什么 AST」的好工具。

调用点在 lower 主链路里，位置很关键——它在 `resolve_pipeline` **之前**：

[tilelang/engine/lower.py:285-289](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L285-L289) — 先跑 `PreLowerSemanticCheck(mod)`，再 `resolve_pipeline(target).lower(mod, target)` 跑真正的 Pass 流水线。

接下来看两个检查器各自检查什么。

**NestedLoopChecker** 的规则写在它的 docstring 里，核心是三条（[tilelang/analysis/nested_loop_checker.py:61-119](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/analysis/nested_loop_checker.py#L61-L119)）：

- **Rule 1**：`T.serial` 可以嵌套在任何循环里，无限制。
- **Rule 2**：`T.Parallel` 不能再嵌套「有并行行为的 tile op 或非连续的 Parallel」。唯一例外是**严格连续**的 Parallel 嵌 Parallel（可以被 fuse 成一个 Parallel）。
- **Rule 3**：`T.Pipelined` 不能出现在 `T.Parallel` 内部（反过来 Parallel 在 Pipelined 内是允许的）。

违规时抛出的错误（注意统一前缀 `[Tilelang Semantic Check]`，方便定位）：

[tilelang/analysis/nested_loop_checker.py:54-58](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/analysis/nested_loop_checker.py#L54-L58) — 在 `T.Parallel` 体内遇到 tile op（`is_tile_op` 判定 `TLOpBuilder` 属性）时报错：只允许 elementwise 操作。

[tilelang/analysis/nested_loop_checker.py:46-50](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/analysis/nested_loop_checker.py#L46-L50) — `T.Pipelined` 嵌套在 `T.Parallel` 内时报错。

实现上它是一个 `PyStmtExprVisitor`，用一个 `in_parallel_context` 布尔位标记「当前是否在 Parallel 体内」，遍历到嵌套 Parallel、tile op 调用、Pipelined 时据此判定（[tilelang/analysis/nested_loop_checker.py:24-58](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/analysis/nested_loop_checker.py#L24-L58)）。

**FragmentLoopChecker** 检查的是另一类问题：fragment（寄存器）缓冲的访问与 `T.Parallel` 的循环变量之间的关系。规则是：**当 `T.Parallel` 的范围是符号化的（min/extent 不是编译期整数）时，它的循环变量不能用来索引 fragment 缓冲**。

[tilelang/analysis/fragment_loop_checker.py:75-94](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/analysis/fragment_loop_checker.py#L75-L94) — 到达最内层循环时，收集 fragment 访问，若某个符号范围 Parallel 的循环变量出现在 fragment 索引里就抛错。

直觉解释：fragment 是线程私有寄存器，其布局由 `LayoutInfer` 推理并绑定到具体的线程坐标；如果用「编译期未知范围」的 Parallel 循环变量去索引 fragment，布局推理就无法确定元素归属，因此禁止。范围的「符号化」判定是 `not (isinstance(min, IntImm) and isinstance(extent, IntImm))`（[tilelang/analysis/fragment_loop_checker.py:79](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/analysis/fragment_loop_checker.py#L79)）。

#### 4.1.4 代码实践

**实践目标**：亲手触发一次 `PreLowerSemanticCheck` 报错，阅读它的错误信息；再用 `TL_DISABLE_PRELOWER_SEMANTIC_CHECK` 关掉它，观察编译期行为的变化。

**操作步骤**：

1. 写一个「把 tile op（`T.copy`）放进 `T.Parallel` 体内」的 kernel（这违反 Rule 2），用 `@tilelang.jit` 编译：

```python
# 示例代码：故意违反 Rule 2，用于触发 PreLowerSemanticCheck
import tilelang
import tilelang.language as T

@tilelang.jit
def bad_copy_in_parallel(M: int, N: int, block_M: int, block_N: int, dtype: str):
    @T.prim_func
    def main(
        A: T.Tensor((M, N), dtype),
        B: T.Tensor((M, N), dtype),
    ):
        with T.Kernel(T.ceildiv(M, block_M), T.ceildiv(N, block_N), threads=128) as (bx, by):
            A_shared = T.alloc_shared((block_M, block_N), dtype)
            # ↓ tile op (T.copy) 出现在 T.Parallel 体内 —— 违规
            for i in T.Parallel(block_M):
                T.copy(A[by * block_M, bx * block_N], A_shared)
    return main

# 触发编译期检查
fn = bad_copy_in_parallel(M=128, N=128, block_M=32, block_N=32, dtype="float16")
```

2. 阅读抛出的错误信息。**预期结果**（待本地验证确切措辞）：应出现形如
   `[Tilelang Semantic Check] Only elementwise operations are allowed inside a parallel loop. Got a tile-op "tl.tileop.copy".`
   的 `ValueError`，且在 Pass 流水线之前抛出。

3. 关掉检查后再编译，观察行为变化：

```python
# 示例代码：通过 pass_configs 关闭 pre-lower 语义检查
fn2 = bad_copy_in_parallel.compile(
    M=128, N=128, block_M=32, block_N=32, dtype="float16",
    pass_configs={
        tilelang.PassConfigKey.TL_DISABLE_PRELOWER_SEMANTIC_CHECK: True,
    },
)
```

4. **需要观察的现象**：第 3 步不再在第 2 步那一行抛出 `[Tilelang Semantic Check]` 错误——因为 `PreLowerSemanticCheck` 提前返回了。但这个 kernel 本身结构是非法的，所以它**很可能会在后续某个 Pass（如 layout inference / pipeline planning）里以另一种错误失败，或生成不正确的 kernel**。具体下游报什么，**待本地验证**。

> 备选触发方式：把 `T.Pipelined` 嵌进 `T.Parallel`（违反 Rule 3），预期错误为
> `[Tilelang Semantic Check] Pipelined loop cannot be nested inside a parallel loop.`
> （[tilelang/analysis/nested_loop_checker.py:48-50](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/analysis/nested_loop_checker.py#L48-L50)）。

> **再次提醒**：如果你想触发「dtype 不匹配」或「shape 不匹配」的错误，**不要**期望 `PreLowerSemanticCheck` 来报——它不检查这些。dtype/shape/strides/device 的校验发生在 kernel 被调用时，由 host 侧自动插入的检查负责，详见 `docs/compiler_internals/tensor_checks.md`（例如传错 dtype 会得到 `...dtype is expected to be float16, but got incompatible dtype`）。两套机制泾渭分明：一个是**编译期、结构**检查，一个是**运行期、张量字段**检查。

#### 4.1.5 小练习与答案

**练习 1**：下面哪种写法会被 `NestedLoopChecker` 接受？
(a) `for i in T.Parallel(M): for j in T.Parallel(N): B[i,j]=A[i,j]`
(b) `for i in T.Parallel(M): T.copy(A, B_shared)`
(c) `for i in T.Pipelined(K): for j in T.Parallel(N): ...`

> **答案**：(a) 和 (c) 接受，(b) 拒绝。(a) 是「严格连续的 Parallel 嵌 Parallel」例外，可被 fuse；(c) 是 Parallel 在 Pipelined **内部**，规则允许；(b) 是 Parallel 体内放 tile op，违反 Rule 2。

**练习 2**：为什么 `FragmentLoopChecker` 只对「符号范围」的 `T.Parallel` 报错，而对编译期常数范围的 `T.Parallel` 索引 fragment 不报错？

> **答案**：fragment 是线程私有寄存器，元素到线程/寄存器的映射由 `LayoutInfer` 在编译期推理。当 Parallel 的范围是编译期整数时，布局推理能确定每个元素归属哪个线程，索引合法；当范围是符号（运行期才知）时，布局推理无法定论，故禁止用其循环变量索引 fragment（见 [fragment_loop_checker.py:75-94](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/analysis/fragment_loop_checker.py#L75-L94)）。

**练习 3**：用一句话区分 `PreLowerSemanticCheck` 与 `docs/compiler_internals/tensor_checks.md` 描述的检查。

> **答案**：前者是编译期、对 tile 循环**结构**的合法性检查（在 lower 入口、Pass 之前）；后者是运行期、在 kernel 被调用时对传入张量的 **dtype/shape/strides/device** 等字段的校验（自动插入 host stub）。

### 4.2 参数描述与抽取：KernelParam 与 extrac_params

#### 4.2.1 概念说明

`KernelParam` 是「一个 kernel 参数的 Python 侧描述」，只有两个字段：

- `dtype: tvm.DataType`
- `shape: list[int | Var]`

为什么需要它？因为编译出的 kernel 最终要和 PyTorch（或任意 DLPack）tensor 互操作：运行时 adapter 需要**提前知道**每个参数的 dtype 与 shape，才能——

- 为输出张量分配正确 dtype/shape 的 `torch.Tensor`；
- 对输入做 dtype 校验与友好报错；
- 处理低精度类型（fp8/fp4）的存储形状换算。

而 TIR `PrimFunc` 的参数本身只是 `Var`：张量参数经 `func.buffer_map` 映射到带 dtype/shape 的 `Buffer`，标量参数则是裸 `Var`（只有 dtype、无 shape）。`KernelParam` 把这两种异构参数统一成同一种描述。

一个关键设计决定：`dtype` 用 `tvm.DataType` 而**不是** `torch.dtype`。原因写在源码注释里——TVM 的类型系统比 PyTorch 宽，能表达 `float8_e4m3`、`float4`、量化类型等；用 `torch.dtype` 会丢失这些信息。`KernelParam` 只在真正需要和 PyTorch 交互时，才用 `torch_dtype()` 把 TVM 类型转过去。

#### 4.2.2 核心流程

抽取参数的函数叫 `extrac_params`（注意：源码里就是这个拼写，少了一个 `t`），逻辑很直白：

```text
对 func.params 里的每一个 var：
  若 var 在 func.buffer_map 中 → KernelParam.from_buffer(buffer_map[var])   # 张量参数
  否则                          → KernelParam.from_var(var)                 # 标量参数
返回 list[KernelParam]，顺序与函数签名一致
```

两个工厂方法的区别：

- `from_buffer`：从 `Buffer` 取 `dtype`，遍历 `Buffer.shape`，把每个维度归一化——`IntImm` 取 `.value`（编译期常数），`Var`/`PrimExpr` 保留（符号维度）。
- `from_var`：标量参数，取 `var.dtype`，shape 为空列表 `[]`（`is_scalar()` 据此判定）。

#### 4.2.3 源码精读

抽取函数本体：

[tilelang/engine/lower.py:198-205](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L198-L205) — `extrac_params(func)`：按 `var ∈ buffer_map` 分流到 `from_buffer` 或 `from_var`。

它在 lower 主链路里的调用点（仅当 `runtime_only=False` 时才抽取）：

[tilelang/engine/lower.py:269-272](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L269-L272) — 入参是 `PrimFunc` 时，`params = extrac_params(func) if not runtime_only else None`，同时把函数封进 IRModule。

`KernelParam` 数据类定义与字段说明：

[tilelang/engine/param.py:12-25](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/param.py#L12-L25) — `KernelParam`：`dtype` 用 `tvm.DataType` 以保留全部 TVM 类型信息；`shape` 是维度列表，元素可为 `int` 或 `Var`。

两个工厂方法：

[tilelang/engine/param.py:26-51](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/param.py#L26-L51) — `from_buffer`：直接用 `buffer.dtype`，逐维归一化（`IntImm`→`int`，`Var`/`PrimExpr`→保留），遇到不支持的维度类型抛 `ValueError`。

[tilelang/engine/param.py:53-68](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/param.py#L53-L68) — `from_var`：标量参数，`shape=[]`。

一组谓词方法用于运行时分类（adapter 会用到）：

[tilelang/engine/param.py:70-125](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/param.py#L70-L125) — `is_scalar` / `is_unsigned` / `is_float8` / `is_float4` / `is_boolean`：基于 dtype 字符串前缀判定（会先剥掉可能的 `torch.` 前缀）。

以及和 PyTorch 桥接的转换方法：

[tilelang/engine/param.py:127-141](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/param.py#L127-L141) — `torch_dtype()`：经 `T.dtype(self.dtype).as_torch()` 把 TVM 类型转成 `torch.dtype`，供 adapter 创建张量用。

下游 adapter 真的会消费这些字段，例如 tvm_ffi adapter 在初始化时就把每个 `KernelParam` 转成 torch dtype 与原生 shape 列表：

[tilelang/jit/adapter/tvm_ffi.py:186-204](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/adapter/tvm_ffi.py#L186-L204) — `param_dtypes = [param.torch_dtype() for param in self.params]`，并对 fp4 等低比特类型做存储形状换算（最后一维乘以 bits/lanes）。

#### 4.2.4 代码实践

**实践目标**：编译一个 GEMM，观察它的 `params` 列表里每个 `KernelParam` 的 dtype 与 shape，验证 `extrac_params` 的分流逻辑。

**操作步骤**：

1. 编译一个熟悉的分块 GEMM（可复用 u1-4 / u3-1 的写法）：

```python
# 示例代码
import tilelang
import tilelang.language as T

@tilelang.jit
def matmul(M: int, N: int, K: int, dtype: str = "float16"):
    @T.prim_func
    def main(
        A: T.Tensor((M, K), dtype),
        B: T.Tensor((K, N), dtype),
        C: T.Tensor((M, N), dtype),
    ):
        with T.Kernel(T.ceildiv(M, 128), T.ceildiv(N, 128), threads=128) as (bx, by):
            A_shared = T.alloc_shared((128, 32), dtype)
            B_shared = T.alloc_shared((32, 128), dtype)
            C_local  = T.alloc_fragment((128, 128), dtype)
            T.clear(C_local)
            for k in T.Pipelined(T.ceildiv(K, 32), num_stages=3):
                T.copy(A[by*128, k*32], A_shared)
                T.copy(B[k*32, bx*128], B_shared)
                T.gemm(A_shared, B_shared, C_local)
            T.copy(C_local, C[by*128, bx*128])
    return main

kernel = matmul(M=512, N=512, K=512)   # 得到 JITKernel
```

2. 打印参数列表：

```python
# 示例代码
for i, p in enumerate(kernel.params):
    print(i, p.dtype, p.shape, "scalar?" , p.is_scalar())
```

3. **需要观察的现象**：应看到三个参数，dtype 都是 `float16`，shape 分别是 `[512, 512]`、`[512, 512]`、`[512, 512]`，`is_scalar()` 均为 `False`（因为 A/B/C 都在 `buffer_map` 里，走 `from_buffer`）。

4. **进阶**：把签名里加一个标量参数（例如 `alpha: T.float32`），重新编译，观察多出来的那个 `KernelParam` 的 `shape == []` 且 `is_scalar() == True`（走 `from_var`）。**预期结果**：标量参数 dtype 为 `float32`、shape 为空。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `KernelParam.dtype` 用 `tvm.DataType` 而不是 `torch.dtype`？

> **答案**：TVM 类型系统比 PyTorch 宽，能表达 `float8_e4m3`、`float4`、量化类型等 PyTorch 没有的类型；用 `torch.dtype` 会在 `from_buffer` 时丢失信息。只在需要与 PyTorch 交互时才用 `torch_dtype()` 转换（[param.py:19-23](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/param.py#L19-L23)）。

**练习 2**：`extrac_params` 如何区分一个参数是张量还是标量？

> **答案**：看该 `Var` 是否出现在 `func.buffer_map` 中：在则走 `from_buffer`（张量），不在则走 `from_var`（标量，shape 为空）（[lower.py:198-205](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L198-L205)）。

**练习 3**：`from_buffer` 遇到 `shape` 里的 `Var` 会怎么处理？

> **答案**：原样保留到 `shape` 列表里（`isinstance(s, (Var, PrimExpr))` 分支），表示这是一个符号维度（动态 shape）；只有 `IntImm` 才取 `.value` 变成 Python `int`（[param.py:44-50](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/param.py#L44-L50)）。

### 4.3 编译产物封装：CompiledArtifact

#### 4.3.1 概念说明

`lower()` 的返回值不是裸 IR，而是一个 `CompiledArtifact` 数据类——它把 lower 的**全部产物**打包在一起：

| 字段 | 含义 |
| --- | --- |
| `host_mod` | host 侧 IRModule（主机调度逻辑） |
| `device_mod` | device 侧 IRModule（设备 kernel 逻辑） |
| `params` | `list[KernelParam]`，kernel 的参数描述（来自 `extrac_params`） |
| `kernel_source` | 生成的设备源码字符串（CUDA/HIP/…） |
| `rt_mod` | 可选：runtime module，惰性初始化 |
| `target` | 归一化后的设备 target |
| `target_host` | 归一化后的 host target |

为什么要打包成一个对象？因为 `CompiledArtifact` 是**编译器（lower）与运行时之间**的契约。下游的 `JITKernel` 需要这些产物各司其职：用 `params` 构造 adapter（决定输出张量 dtype/shape、输入校验），用 `kernel_source` 调试与显示，用 `device_mod` 经 nvcc/hipcc 编译成二进制（见 u4-3 缓存、u7 adapter），用 `host_mod` 生成 host stub。把它们捆在一起，避免在 lower 与 JIT 之间传来传去一堆零散变量。

一个重要默认行为：`lower()` 默认 `enable_host_codegen=False` 且 `enable_device_compile=False`——也就是说，默认 `lower()` 只做到 **IR 层面的 host/device 拆分 + 设备源码生成**，**不**把源码编译成二进制、也**不**做完整 host codegen。二进制编译交由 JIT 层负责（这正好是 `CUDABinaryCache` 等缓存介入的地方，见 u4-3）。因此在默认路径下返回的 `CompiledArtifact` 里 `rt_mod=None`。

#### 4.3.2 核心流程

`lower()` 的组装流程：

```text
lower(func_or_mod, ...)
  → lower_to_host_device_ir(...)          # 返回 host_mod, device_mod, params, target, target_host
  → device_codegen_without_compile(...)   # 默认：只生成源码，不编译二进制
  → kernel_source = codegen_mod.inspect_source()
  → 组装 CompiledArtifact(host_mod, device_mod, params, kernel_source, target=..., target_host=...)
       （默认分支：rt_mod 不填）
```

若调用方显式 `enable_host_codegen=True`，则额外跑 `host_codegen(...)`、`host_mod.import_module(codegen_mod)`，并返回带 `rt_mod=host_mod` 的 `CompiledArtifact`。

下游消费：`JITKernel` 持有这个 artifact，在构造 adapter 时把 `artifact.params` 透传过去（见 [tilelang/jit/kernel.py:298](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L298) 等多处），adapter 再用这些 `KernelParam` 决定运行时行为。

#### 4.3.3 源码精读

`CompiledArtifact` 数据类定义：

[tilelang/engine/param.py:153-167](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/param.py#L153-L167) — `CompiledArtifact`：`host_mod`/`device_mod`/`params`/`kernel_source` 为必填，`rt_mod`/`target`/`target_host` 可选。

`lower()` 末尾的两个组装分支：

[tilelang/engine/lower.py:319-342](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L319-L342) — 默认走 `device_codegen_without_compile`，取 `inspect_source()` 得到 `kernel_source`；`enable_host_codegen=True` 时返回带 `rt_mod` 的版本，否则返回不带 `rt_mod` 的版本。

`device_codegen_without_compile` 与 `device_codegen` 的区别仅在一个 `compile_device` 布尔：

[tilelang/engine/lower.py:249-256](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L249-L256) — 两者都先 `_prepare_device_codegen_mod`（LowerIntrin + Simplify + HoistBroadcastValues），再交由 `resolve_device_codegen(target).lower(...)`，差别只在 `compile_device` 是否触发真正的二进制编译。

下游 JIT 层如何持有并透传 artifact 与 params：

[tilelang/jit/kernel.py:47-56](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L47-L56) — `JITKernel` 把 `CompiledArtifact` 作为字段持有。

[tilelang/jit/kernel.py:661](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L661) — `self.params` 取自 `self.artifact.params`（无 artifact 时回退到 adapter.params）。

#### 4.3.4 代码实践

**实践目标**：直接调用 `tilelang.lower()` 拿到一个 `CompiledArtifact`，检查它的字段，直观感受「lower 的产物是什么」。

**操作步骤**：

1. 用上一个模块的 GEMM `prim_func`，直接走 lower（绕过 jit 装饰器的封装）：

```python
# 示例代码
import tilelang

# prim_func 来自 4.2.4 的 main（lazy 风格下 jit 调用先返回 PrimFunc）
# 这里假设你已经拿到了 PrimFunc 对象 pf
artifact = tilelang.lower(pf, target="cuda")
```

2. 检查产物字段：

```python
# 示例代码
print(type(artifact).__name__)          # 期望 CompiledArtifact
print("params:", [(str(p.dtype), p.shape) for p in artifact.params])
print("kernel_source 前 20 行:")
print("\n".join(artifact.kernel_source.splitlines()[:20]))
print("rt_mod is None?", artifact.rt_mod is None)   # 默认应为 True
print("target:", artifact.target)
```

3. **需要观察的现象**：
   - `artifact` 是 `CompiledArtifact`；
   - `params` 与 4.2.4 看到的 `KernelParam` 列表一致；
   - `kernel_source` 是一段 CUDA C++ 源码（含 `__global__`、shared memory、CuTe/MMA 调用等）；
   - 默认路径下 `rt_mod is None`（因为 `lower` 默认不编译二进制）。

4. **预期结果**：上述四点全部成立。**待本地验证**（取决于本地是否有 CUDA target；无 GPU 时可用 `target="c"` 或 `target="llvm"` 观察同类结构，但 `kernel_source` 内容会不同）。

#### 4.3.5 小练习与答案

**练习 1**：默认 `tilelang.lower()` 返回的 `CompiledArtifact` 里 `rt_mod` 是什么？为什么？

> **答案**：默认是 `None`。因为 `lower()` 默认 `enable_host_codegen=False`，只做 IR 拆分与设备**源码**生成，不触发完整 host codegen，也不把源码编译成二进制——二进制编译由 JIT 层负责（[lower.py:319-342](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L319-L342)）。

**练习 2**：`CompiledArtifact` 为什么要把 `params`（`list[KernelParam]`）和 IR 模块放在一起打包？

> **答案**：因为它是 lower（编译器）与 JIT/adapter（运行时）之间的契约：adapter 需要 `params` 来创建输出张量、校验输入 dtype/shape，而这些信息不在 IR 模块里、是 `extrac_params` 单独抽取的；打包在一起避免 lower 与 JIT 之间传递零散变量（[param.py:153-167](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/param.py#L153-L167)）。

**练习 3**：`device_codegen` 与 `device_codegen_without_compile` 的差别是什么？

> **答案**：两者都先做 `LowerIntrin + Simplify + HoistBroadcastValues` 准备，再交给 `resolve_device_codegen(target).lower(...)`；唯一差别是传入的 `compile_device` 布尔——前者触发真正的二进制编译，后者只生成源码（[lower.py:249-256](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/engine/lower.py#L249-L256)）。

## 5. 综合实践

把本讲三个模块串起来，完成一个「**编译期检查 + 参数描述 + 产物检视**」的小任务：

1. **写一个结构非法的 kernel**：在 `T.Parallel` 体内放一个 `T.copy`（违反 `NestedLoopChecker` Rule 2），用 `@tilelang.jit` 编译，确认它抛出 `[Tilelang Semantic Check] ...` 错误，并记录确切错误信息。
2. **关掉检查复现「漏网」**：用 `pass_configs={tilelang.PassConfigKey.TL_DISABLE_PRELOWER_SEMANTIC_CHECK: True}` 再次编译同一 kernel，确认 pre-lower 错误不再出现，记录它在下游（哪个 Pass / codegen）以何种新错误失败（或是否生成了可疑的 kernel source）。这能让你直观体会「早期守门员」的价值。
3. **回到合法 kernel**：把 `T.copy` 移出 `T.Parallel`（放到 serial/Pipelined 主循环里，参考 4.2.4 的 GEMM 写法），正常编译。
4. **检视参数与产物**：打印 `kernel.params`（验证 `extrac_params` 的张量/标量分流），并打印 `kernel.get_kernel_source()` 的前若干行（验证 `CompiledArtifact.kernel_source` 是真实 CUDA 源码）。
5. **画一张数据流图**：把 `PrimFunc → extrac_params → KernelParam[] → CompiledArtifact → JITKernel → adapter` 这条链画出来，标注每一步发生在哪个文件、哪个函数。

完成这个任务后，你应该能向别人讲清楚：「tilelang 在 lower 入口做了哪些编译期结构检查、为什么这些检查可关闭、kernel 的参数信息是如何从 IR 抽取并随产物交给运行时的」。

## 6. 本讲小结

- `PreLowerSemanticCheck` 是 lower 入口、Pass 流水线**之前**的早期守门员，**与后端无关**，只检查 tile 循环结构（`NestedLoopChecker`）与 fragment 在符号范围 `T.Parallel` 中的索引（`FragmentLoopChecker`），可选地用 `ASTPrinter` 打印 AST。
- 它**不**检查越界（那是 `LegalizeSafeMemoryAccess` Pass）也**不**检查 dtype/shape/device（那是 host 侧 tensor check，见 `tensor_checks.md`）——两套机制泾渭分明。
- 用 `PassConfigKey.TL_DISABLE_PRELOWER_SEMANTIC_CHECK=True` 可整体关闭它（`should_enable_prelower_semantic_check` 读 `PassContext`）；JIT 层通过 `pass_configs` 注入。
- `KernelParam(dtype, shape)` 是 kernel 参数的 Python 侧统一描述，由 `extrac_params` 按 `var ∈ buffer_map` 分流到 `from_buffer`（张量）或 `from_var`（标量）；dtype 用 `tvm.DataType` 以保留 fp8/fp4 等宽类型。
- `CompiledArtifact` 把 `host_mod`/`device_mod`/`params`/`kernel_source`（及可选 `rt_mod`/`target`）打包，是 lower 与 JIT/adapter 之间的契约；默认 `lower()` 不编译二进制（`rt_mod=None`），二进制编译交由 JIT 层。
- 三个检查器与 `extrac_params` 都是 `prim_func_pass` / 纯 Python，位于 `tilelang/analysis` 与 `tilelang/engine`，是理解 tilelang「lower 入口在做什么」的关键拼图。

## 7. 下一步学习建议

- 想深入「Pass 流水线到底跑了哪些 Pass、`PassConfigKey` 还能控制什么」，进入 **u6-1（Pass 系统、PassContext 与 PassConfigKey）**。
- 想看 `CompiledArtifact` 的 `device_mod` 是如何变成 CUDA 源码的，进入 **u6-3（设备代码生成、模板与 tile op lowering）**。
- 想看 `CompiledArtifact` 下游如何被 adapter 包装成可调用对象、`params` 如何驱动 host stub 校验，进入 **u7-1（执行后端与 kernel adapter）**，并对照阅读 `docs/compiler_internals/tensor_checks.md` 的 host 侧校验细节。
- 想系统了解 host/device 拆分（本讲只触及 `Filter` 这一步），进入 **u7-2（host/device 拆分、库生成与编译回调）**。
