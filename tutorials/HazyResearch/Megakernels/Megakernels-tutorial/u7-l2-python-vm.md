# Python 虚拟机模式

本文将深入理解 Megakernels 中的 Python 虚拟机模式，分析其解释器设计、指令求解器映射机制，以及在调试和验证中的重要作用。

## 背景与动机

在 Megakernels 系统中，有三种执行模式：PyTorch 原生模式、PyVM（Python 虚拟机）模式和 MK（Megakernel）模式。PyVM 模式处于中间位置——它比 PyTorch 更接近目标架构，但比 MK 模式更易于调试和理解。

PyVM 的核心价值在于：

1. **正确性验证**：在调试 CUDA megakernel 之前，先用 PyVM 验证指令序列和调度逻辑
2. **快速迭代**：纯 Python 实现使得修改和测试指令变得容易
3. **数值对比**：可以直接与 MK 模式的输出进行逐元素对比，发现数值错误
4. **教学工具**：帮助理解指令系统的执行流程，无需深入 CUDA 代码

## PyVM 解释器设计

### 核心架构

PyVM 解释器的设计非常简洁，核心只有几十行代码。让我们先看整体架构：

```python
class PyVM_Interpreter:
    def __init__(self, instruction_to_solver: dict):
        self.instruction_to_solver = instruction_to_solver

    def interpret(self, globs: BaseGlobals, instructions: list[Instruction]):
        interpret_with_pyvm(globs, instructions, self.instruction_to_solver)
```

这个设计遵循了**解释器模式**：

- **指令集**：由 `Instruction` 的子类定义，如 `O_ProjResidual`、`PartialAttention` 等
- **求解器映射**：`instruction_to_solver` 字典将每种指令类型映射到对应的求解函数
- **执行循环**：遍历指令序列，查找对应的求解器并执行

### 解释执行流程

解释执行的核心函数是 `interpret_with_pyvm`：

```python
def interpret_with_pyvm(
    globals: BaseGlobals, instructions: list[Instruction], instruction_to_solver: dict
):
    for instruction in instructions:
        instruction_to_solver[type(instruction)](globals, instruction)
```

这是一个经典的**字节码解释器**模式：

1. 取指令：从指令序列中取出下一条指令
2. 译码：通过 `type(instruction)` 获取指令类型
3. 执行：查找对应的求解函数并调用，传入全局状态和指令参数

这种设计的优势在于：

- **类型安全**：利用 Python 的类型系统确保指令和求解器的正确匹配
- **易于扩展**：添加新指令只需在映射表中添加条目
- **调试友好**：每个求解函数都是独立的 Python 函数，易于单步调试

## 指令求解器映射

### 映射表结构

指令到求解器的映射是 PyVM 的核心组件。以延迟优化场景为例：

```python
INSTRUCTION_TO_SOLVER = {
    O_ProjResidual: o_proj_residual,
    DownProjResidual: down_proj_residual,
    LayerNormDoubleMatVecSiLU: layer_norm_double_matvec_silu,
    LayerNorm_QKV_MatVecRopeAppend: layer_norm_matvec_rope_append,
    RMS_LM_Head: rms_lm_head,
    PartialAttention: partial_attention,
    AttentionReduction: attention_reduction,
}
```

这个映射表将每种指令类型连接到其对应的求解函数。每个求解函数都遵循统一的接口：

```python
def solver_function(globals: Globals, instruction: Instruction) -> None:
    # 实现具体的计算逻辑
    pass
```

### 求解函数实现细节

让我们分析几个典型的求解函数来理解 PyVM 的执行模式。

#### 1. 矩阵向量乘法

这是最基础的操作，几乎所有指令都依赖它：

```python
def matvec(
    mat: Tensor,
    vec: Tensor,
    block_size: int,
    block_idx: int,
    reduce: bool = False,
    reduction_size: int = 0,
    reduction_idx: int = 0,
):
    start, end = get_start_end(block_size, block_idx)
    if reduce:
        red_start, red_end = get_start_end(reduction_size, reduction_idx)
        mat = mat[start:end, red_start:red_end]
        vec = vec[red_start:red_end]
    else:
        mat = mat[start:end]

    out = einsum(mat, vec, "o i, i -> o")
    return out, start, end
```

这个函数实现了**分块矩阵向量乘法**：

- 计算当前块的起止索引：`start = block_size * block_idx`
- 如果是归约模式，对矩阵和向量都进行切片
- 使用 `einsum` 执行矩阵乘法：`"o i, i -> o"` 表示输出维度

#### 2. O 投影残差连接

这是一个典型的 MLP 输出投影操作：

```python
def o_proj_residual(globals: Globals, instruction: O_ProjResidual):
    # Barrier 检查
    op_barriers = globals.barriers[instruction.layer_idx, instruction.prev_opcode() - 1]
    assert op_barriers[0] == globals.num_attention_heads

    assert instruction.start_block_idx == instruction.end_block_idx - 1
    assert instruction.reduction_block_idx == 0

    matvec_with_residual(
        mat=globals.o_proj_weights[instruction.layer_idx],
        vec=globals.attn_out,
        residual=globals.hidden_states,
        block_size=globals.o_proj_block_size,
        start_block_idx=instruction.start_block_idx,
        end_block_idx=instruction.end_block_idx,
        reduction_size=globals.matvec_reduction_size,
        reduction_block_idx=instruction.reduction_block_idx,
    )

    # Barrier 更新
    next_op_barriers = globals.barriers[instruction.layer_idx, instruction.opcode() - 1]
    next_op_barriers[0] += instruction.end_block_idx - instruction.start_block_idx
```

这个函数展示了 PyVM 的几个关键模式：

1. **Barrier 同步检查**：确保前置操作已完成
2. **断言验证**：验证指令参数的合法性
3. **分块计算**：将大矩阵运算分解为小块
4. **残差连接**：直接在 `hidden_states` 上累加结果
5. **Barrier 更新**：为后续操作提供同步信息

#### 3. LayerNorm 双矩阵向量乘法 + SiLU

这是一个融合操作，展示了 PyVM 如何处理复杂的计算流程：

```python
def layer_norm_double_matvec_silu(
    globals: Globals, instruction: LayerNormDoubleMatVecSiLU
):
    # Barrier 检查
    op_barriers = globals.barriers[instruction.layer_idx, instruction.prev_opcode() - 1]
    assert op_barriers[0] == 128

    # 1. RMS Norm
    post_ln = rms_norm(
        inp=globals.hidden_states,
        weight=globals.mlp_ln_weights[instruction.layer_idx],
        eps=globals.rms_norm_eps,
    )

    block_size = globals.up_gate_proj_block_size
    barriers = globals.barriers[instruction.layer_idx, instruction.opcode() - 1]

    # 2. 双矩阵向量乘法 + SiLU 融合
    for block_idx in instruction.block_idxs:
        start, end = get_start_end(block_size, block_idx)

        up_matvec, start, end = matvec(
            mat=globals.up_proj_weights[instruction.layer_idx],
            vec=post_ln,
            block_size=block_size,
            block_idx=block_idx,
        )

        gate_matvec, _, _ = matvec(
            mat=globals.gate_proj_weights[instruction.layer_idx],
            vec=post_ln,
            block_size=block_size,
            block_idx=block_idx,
        )

        # 3. SiLU 激活函数
        post_silu = F.silu(gate_matvec) * up_matvec

        globals.silu_out[start:end] = post_silu

        barriers[0] += 1
```

这个函数实现了 MLP 层的核心计算：

1. **Layer Normalization**：RMSNorm 归一化
2. **并行投影**：同时计算 up 和 gate 投影
3. **激活函数**：SiLU（Swish）激活：`SiLU(x) = x · sigmoid(x)`
4. **逐元素相乘**：gate 输出与 up 输出相乘

#### 4. 部分注意力计算

这是最复杂的操作之一，展示了 PyVM 如何处理注意力机制：

```python
def partial_attention(globals: Globals, instruction: PartialAttention):
    gqa_ratio = globals.num_attention_heads // globals.num_kv_heads

    # Barrier 检查
    op_barriers = globals.barriers[instruction.layer_idx, instruction.prev_opcode() - 1]
    for i in range(gqa_ratio):
        assert op_barriers[instruction.kv_head_idx * gqa_ratio + i] == 4
    assert op_barriers[globals.num_attention_heads + instruction.kv_head_idx] == 4
    assert (
        op_barriers[
            globals.num_attention_heads + globals.num_kv_heads + instruction.kv_head_idx
        ]
        == 4
    )

    kv_block_size = globals.attn_kv_block_size
    seq_len = globals.pos_id + 1
    layer_idx = instruction.layer_idx
    kv_head_idx = instruction.kv_head_idx

    total_blocks = math.ceil(seq_len / kv_block_size)
    blocks_per_partial = math.ceil(total_blocks / instruction.num_partials)

    start_block = instruction.partial_idx * blocks_per_partial
    end_block = min(start_block + blocks_per_partial, total_blocks)

    start_token = start_block * kv_block_size
    end_token = min(end_block * kv_block_size, seq_len)

    # 从 KV cache 中加载
    k = globals.k_cache[layer_idx, 0, start_token:end_token, kv_head_idx]
    v = globals.v_cache[layer_idx, 0, start_token:end_token, kv_head_idx]

    head_start = kv_head_idx * gqa_ratio
    head_end = head_start + gqa_ratio

    q = globals.post_ln_rope_q.view(globals.num_attention_heads, -1)[
        head_start:head_end
    ]

    # QK 矩阵乘法
    qk = einsum(q.float(), k.float(), "h i, k i -> h k")
    scaled_qk = qk * globals.attn_scale

    # Softmax 和 LogSumExp
    softmax = torch.softmax(scaled_qk, dim=-1)
    lse = torch.log2(torch.sum(torch.exp(scaled_qk), dim=-1))

    # 加权求和
    out = einsum(softmax.float(), v.float(), "h k, k o -> h o")

    if globals.skip_attn_reduction:
        globals.attn_out.view(globals.num_attention_heads, -1)[
            head_start:head_end, :
        ] = out
        barriers = globals.barriers[
            instruction.layer_idx, AttentionReduction.opcode() - 1
        ]
        barriers[0] += head_end - head_start
    else:
        # 存储中间结果用于后续归约
        globals.attn_lse_intermediates[head_start:head_end, instruction.partial_idx] = (
            lse
        )
        globals.attn_out_intermediates[head_start:head_end, instruction.partial_idx] = (
            out
        )

        # Barrier 更新
        barriers = globals.barriers[instruction.layer_idx, instruction.opcode() - 1]
        barriers[head_start:head_end] += 1
```

这个函数实现了注意力的核心计算流程，展示了几个重要概念：

1. **分组查询注意力（GQA）**：多个 query 头共享同一个 key-value 头
2. **分区计算**：将长序列分成多个部分，分别计算
3. **数值稳定性**：使用 LogSumExp 而非直接 softmax，提高数值精度
4. **中间结果存储**：为多阶段归约做准备

### RMSNorm 实现

RMSNorm 是 Transformer 模型中的核心归一化操作：

```python
def rms_norm(inp: Tensor, weight: Tensor, eps: float):
    input_dtype = inp.dtype
    inp = inp.to(torch.float32)
    variance = inp.pow(2).mean(-1, keepdim=True)
    inp = inp * torch.rsqrt(variance + eps)

    return weight * inp.to(input_dtype)
```

这个实现对应数学公式：

\[
\text{RMSNorm}(x) = \frac{x}{\sqrt{\text{mean}(x^2) + \epsilon}} \cdot \gamma
\]

其中：
- \(x\) 是输入向量
- \(\gamma\) 是可学习的权重参数
- \(\epsilon\) 是防止除零的小常数

## 调试与验证机制

### 差异测试框架

PyVM 的主要用途是验证 MK 模式的正确性。`scripts/diff_test.py` 提供了完整的测试框架：

```python
# 初始化两个解释器
pyvm_interpreter = make_pyvm_interpreter(config.setting)
mk_interpreter = make_mk_interpreter(config.setting, config.mk_dir)

# 构建调度
spy = builder.build(model=model, layer_limit=config.layer_limit)
smk = builder.with_new_globals(spy, model)

# 初始化两个全局状态（克隆以确保独立）
gpy = spy.globs
gmk = smk.globs

gmk.hidden_states.copy_(gpy.hidden_states)
smk.globs.k_cache = spy.globs.k_cache.clone()
smk.globs.v_cache = spy.globs.v_cache.clone()
```

关键点：

1. **独立状态**：PyVM 和 MK 各自维护独立的全局状态
2. **相同输入**：确保两个解释器从相同的输入开始
3. **克隆 KV Cache**：KV cache 需要深度克隆，避免一个解释器的修改影响另一个

### 数值对比机制

全局状态提供了 `diff` 方法进行逐张量对比：

```python
def diff(self, other: "BaseGlobals", skip_kv_cache: bool = False):
    for field in fields(self):
        name = field.name
        attr = getattr(self, name)
        other_attr = getattr(other, name)
        if (
            not isinstance(attr, Tensor)
            or "weights" in name
            or name in ["rope_cos", "rope_sin"]
            or (skip_kv_cache and "cache" in name)
        ):
            continue
        diff_tensors(attr, other_attr, name)
```

具体的张量对比函数：

```python
def diff_tensors(a: Tensor, b: Tensor, name: str):
    a = a.float()
    b = b.float()

    diff = a - b
    adiff = diff.abs()
    rdiff = 2 * adiff / (a.abs() + b.abs() + 1e-6)
    print(f"{name}: max adiff: {adiff.max()}, mean rdiff: {rdiff.mean()}")
    return diff, adiff, rdiff
```

这个对比计算了两个指标：

1. **绝对差异（Absolute Difference）**：
   \[
   \text{adiff} = |a - b|
   \]
   
2. **相对差异（Relative Difference）**：
   \[
   \text{rdiff} = \frac{2|a - b|}{|a| + |b| + \epsilon}
   \]

相对差异对大数值更宽容，对小数值更严格，更适合浮点数对比。

### 调试流程

典型的调试流程如下：

```python
# 1. 用 PyVM 执行
pyvm_interpreter.interpret(gpy, instructions)

# 2. 用 MK 执行
mk_interpreter.interpret(gmk)

# 3. 对比结果
gpy.diff(gmk)
```

如果发现差异，可以：

1. **打印中间状态**：使用 `PrintState` 指令打印关键张量的值
2. **单步调试**：在求解函数中设置断点
3. **隔离指令**：只执行有问题的指令子集
4. **简化模型**：使用更小的模型或单层测试

### 状态打印支持

PyVM 提供了状态打印指令用于调试：

```python
def print_state(globals: BaseGlobals, instruction: PrintState):
    print_info = instruction.print_info
    if (
        print_info.layer_filter is None
        or instruction.layer_idx in print_info.layer_filter
    ) and (
        print_info.name_filter is None or instruction.name in print_info.name_filter
    ):
        print(f"State at layer={instruction.layer_idx}, op={instruction.name}")
        for state in print_info.state_filter:
            attr = getattr(globals, state)
            print(f"{state}: {trepr(attr) if isinstance(attr, Tensor) else attr}")
```

这允许在指令序列中插入"检查点"，监控中间状态。

## 性能对比与特性

### 三种模式对比

Megakernels 支持三种执行模式，各有优缺点：

| 特性 | PyTorch 原生 | PyVM 模式 | MK 模式 |
|------|-------------|----------|---------|
| **实现语言** | Python + CUDA C++ | 纯 Python | CUDA C++ |
| **执行速度** | 中等 | 慢 | 最快 |
| **开发难度** | 低 | 低 | 高 |
| **调试难度** | 中等 | 最简单 | 最复杂 |
| **实现保真度** | 低（不遵循目标架构） | 高（完全模拟指令） | 最高（真实部署） |

PyVM 的独特价值在于：

1. **完全遵循指令抽象**：与 MK 模式使用相同的指令序列
2. **易于验证**：纯 Python 代码，可以单步调试和打印
3. **快速原型**：修改和测试新指令非常容易

### 性能测试示例

从 `diff_test.py` 中可以看到典型的性能输出：

```
interpreting with pyvm...
pyvm time: 0.123s

interpreting with mk...
mk time: 0.012s

done! diffing tensors:
hidden_states: max adiff: 1.2e-6, mean rdiff: 3.4e-8
logits: max adiff: 5.6e-7, mean rdiff: 2.1e-9
```

这里展示：

1. **速度差异**：MK 模式通常比 PyVM 快 10-100 倍
2. **数值精度**：差异通常在 \(10^{-6}\) 到 \(10^{-8}\) 级别，可接受范围
3. **测试覆盖**：所有关键张量都进行了对比

### Barrier 同步机制

PyVM 实现了与 MK 模式相同的 barrier 同步机制：

```python
# Barrier 检查
op_barriers = globals.barriers[instruction.layer_idx, instruction.prev_opcode() - 1]
assert op_barriers[0] == expected_value

# Barrier 更新
next_op_barriers = globals.barriers[instruction.layer_idx, instruction.opcode() - 1]
next_op_barriers[0] += increment
```

barrier 是一个计数器数组，用于确保：

1. **依赖关系**：前置操作完成指定次数后才能执行当前操作
2. **并行控制**：控制 SM 之间的并行执行顺序
3. **正确性验证**：通过断言确保指令序列的正确性

在 PyVM 中，barrier 的检查使用 `assert`，在调试时可以立即发现问题。在 MK 模式中，barrier 通过 CUDA 同步机制实现。

## 实现细节与最佳实践

### 分块计算策略

PyVM 严格遵循分块计算模式，这是与 MK 模式保持一致的关键：

```python
def get_start_end(block_size: int, block_idx: int):
    start = block_size * block_idx
    end = start + block_size
    return start, end
```

这个简单的函数是整个分块计算的基础。每个计算操作都：

1. **计算起止索引**：`start = block_size * block_idx`
2. **切片张量**：只在 `[start:end]` 范围内操作
3. **写回结果**：将结果写回对应位置

### 数值精度处理

PyVM 在数值精度上与 MK 保持一致：

```python
# 注意力计算中使用 float32
qk = einsum(q.float(), k.float(), "h i, k i -> h k")

# softmax 结果转为 float32 进行最终计算
out = einsum(softmax.float(), v.float(), "h k, k o -> h o")
```

这种做法确保：

1. **累积精度**：中间计算使用 float32 避免溢出
2. **存储精度**：最终结果可以转换回 float16 或 bfloat16
3. **与 CUDA 一致**：模仿 CUDA kernel 的精度处理

### 循环展开优化

PyVM 遵循 MK 模式的循环展开策略：

```python
for block_idx in range(start_block_idx, end_block_idx):
    matvec_out, start, end = matvec(...)
    residual[start:end] += matvec_out.to(residual.dtype)
```

而不是一次性计算：

```python
# 这是错误的方式，不符合 MK 模式
# out = full_matvec(mat, vec)
```

这种设计确保 PyVM 的执行流程与 MK kernel 的块级并行一致。

## 练习题

### 基础题

1. **指令映射理解**：
   假设我们要添加一个新指令 `LayerNormOnly`，它只执行 RMSNorm 操作。请写出：
   - 指令类定义
   - 对应的求解函数
   - 如何将它添加到 `INSTRUCTION_TO_SOLVER` 映射表

2. **Barrier 机制分析**：
   在 `layer_norm_double_matvec_silu` 函数中，为什么要检查 `op_barriers[0] == 128`？这个 128 代表什么含义？如果断言失败说明什么问题？

3. **分块计算验证**：
   给定 `block_size=16`，`block_idx=3`，计算 `start` 和 `end` 的值。如果总共有 100 个元素，`block_idx=6` 时的 `start` 和 `end` 是多少？

### 进阶题

4. **数值稳定性分析**：
   在 `partial_attention` 函数中，为什么要计算 `lse`（LogSumExp）而不是直接使用 softmax？分析在极端输入情况下的数值稳定性差异。

5. **GQA 机制理解**：
   分析 `partial_attention` 函数中 `gqa_ratio` 的作用。如果 `num_attention_heads=32`，`num_kv_heads=8`，那么 `gqa_ratio` 是多少？这如何影响内存访问模式？

6. **调试策略设计**：
   假设 `diff_test.py` 显示 `hidden_states` 的相对差异为 \(1.5 \times 10^{-4}\)，超过了阈值。设计一个系统的调试策略来定位问题源头。

### 挑战题

7. **新指令实现**：
   实现一个 `GroupedMatVec` 指令，它同时计算多个矩阵向量乘法但共享输入向量。考虑：
   - 指令参数设计
   - 求解函数实现
   - Barrier 同步策略
   - 性能优化考虑

8. **性能优化分析**：
   PyVM 比 MK 慢的主要原因有哪些？如果要对 PyVM 进行性能优化（保持功能不变），你会优先优化哪些部分？分析每种优化可能带来的性能提升。

9. **错误定位算法**：
   设计一个自动二分搜索算法，当 `diff_test.py` 发现数值差异时，自动定位到导致差异的具体指令。考虑：
   - 如何分割指令序列
   - 如何处理依赖关系
   - 如何最小化测试次数

## 答案

### 基础题答案

**1. 指令映射理解**

```python
from dataclasses import dataclass
from megakernels.instructions import Instruction

@dataclass
class LayerNormOnly(Instruction):
    layer_idx: int
    
    @classmethod
    def opcode(cls) -> int:
        return 8  # 选择一个未使用的操作码
    
    @classmethod
    def prev_opcode(cls) -> int:
        return DownProjResidual.opcode()

def layer_norm_only(globals: Globals, instruction: LayerNormOnly):
    post_ln = rms_norm(
        inp=globals.hidden_states,
        weight=globals.mlp_ln_weights[instruction.layer_idx],
        eps=globals.rms_norm_eps,
    )
    globals.hidden_states[:] = post_ln

# 添加到映射表
INSTRUCTION_TO_SOLVER[LayerNormOnly] = layer_norm_only
```

**2. Barrier 机制分析**

`128` 是 `hidden_size / block_size` 的典型值（假设 hidden_size=4096，block_size=32，则 4096/32=128）。这表示：

- 前置操作（如 DownProjResidual）应该已经完成了 128 个块的计算
- 每个块对应一个输出位置，因此 128 表示整个 hidden_states 维度
- 如果断言失败，说明前置操作未完成，可能存在：
  - 指令序列错误
  - Barrier 更新错误
  - 并行执行顺序问题

**3. 分块计算验证**

- `block_idx=3`：`start = 16 * 3 = 48`，`end = 48 + 16 = 64`
- `block_idx=6`：`start = 16 * 6 = 96`，`end = 96 + 16 = 112`

### 进阶题答案

**4. 数值稳定性分析**

LogSumExp 提供更好的数值稳定性：

对于普通 softmax：
\[
\text{softmax}(x)_i = \frac{e^{x_i}}{\sum_j e^{x_j}}
\]

当 \(x_i\) 很大时，\(e^{x_i}\) 可能溢出。

使用 LogSumExp：
\[
\text{LSE}(x) = \log(\sum_j e^{x_j}) = \max_j(x_j) + \log(\sum_j e^{x_j - \max_j(x_j)})
\]

减去最大值确保指数不会太大，避免溢出。这在注意力计算中特别重要，因为 QK 值可能很大。

**5. GQA 机制理解**

`gqa_ratio = 32 / 8 = 4`，意味着每 4 个 query 头共享 1 个 key-value 头。

内存访问优势：
- KV cache 只需存储 1/4 的 key-value 向量
- 减少内存带宽需求
- 特别适合长序列推理

代码中体现：
```python
head_start = kv_head_idx * gqa_ratio  # 例如 0, 4, 8, ...
head_end = head_start + gqa_ratio      # 4, 8, 12, ...
```

**6. 调试策略设计**

系统调试策略：

1. **隔离层**：设置 `layer_limit=1`，只测试第一层
2. **隔离操作**：使用 `stop_after_op` 参数停在特定操作
3. **二分搜索**：
   - 将指令序列分成两半
   - 分别测试前半和后半
   - 递归定位到问题指令
4. **张量级对比**：在怀疑的指令前后添加 `PrintState`
5. **参数简化**：使用更小的模型（如 1B 而非 8B）和更短的序列

### 挑战题答案

**7. 新指令实现**

```python
@dataclass
class GroupedMatVec(Instruction):
    layer_idx: int
    block_idxs: list[int]
    group_size: int  # 每组多少个矩阵
    
    @classmethod
    def opcode(cls) -> int:
        return 9
    
    @classmethod
    def prev_opcode(cls) -> int:
        return LayerNormOnly.opcode()

def grouped_matvec(globals: Globals, instruction: GroupedMatVec):
    post_ln = rms_norm(
        inp=globals.hidden_states,
        weight=globals.mlp_ln_weights[instruction.layer_idx],
        eps=globals.rms_norm_eps,
    )
    
    block_size = globals.up_gate_proj_block_size
    
    for group_start in range(0, len(instruction.block_idxs), instruction.group_size):
        group_indices = instruction.block_idxs[group_start:group_start + instruction.group_size]
        
        # 预计算共享的输入
        shared_input = post_ln.clone()
        
        for idx in group_indices:
            start, end = get_start_end(block_size, idx)
            
            # 使用预计算的输入
            out = einsum(
                globals.up_proj_weights[instruction.layer_idx][start:end],
                shared_input,
                "o i, i -> o"
            )
            
            globals.silu_out[start:end] = out
```

Barrier 同步策略：
- 每组完成后更新一次 barrier，而非每个块
- 减少同步开销

**8. 性能优化分析**

PyVM 主要性能瓶颈：

1. **Python 解释器开销**：每次函数调用都有开销
   - 优化：使用 NumPy 向量化操作
   
2. **张量切片**：每次 `matvec` 都创建新张量
   - 优化：预分配缓冲区，使用 in-place 操作
   
3. **循环开销**：Python for 循环比 CUDA 慢得多
   - 优化：使用 `torch.vmap` 或 JIT 编译
   
4. **内存分配**：频繁创建中间张量
   - 优化：对象池模式，重用缓冲区

预期性能提升：
- 向量化：2-5x
- 内存复用：1.5-2x
- JIT 编译：5-10x

但 PyVM 永远无法达到 MK 的性能，因为：
- 缺乏大规模并行
- Python GIL 限制
- 无专用硬件加速

**9. 错误定位算法**

```python
def locate_divergent_instruction(
    pyvm_interpreter, mk_interpreter, 
    gpy, gmk, instructions, 
    tolerance=1e-6
):
    """
    自动定位导致数值差异的指令
    
    返回：(divergent_idx, problematic_tensor_name)
    """
    
    def check_divergence(start, end):
        """检查指令片段是否有差异"""
        # 克隆状态
        gpy_test = deepcopy_globals(gpy)
        gmk_test = deepcopy_globals(gmk)
        
        # 执行片段
        pyvm_interpreter.interpret(gpy_test, instructions[start:end])
        mk_interpreter.interpret(gmk_test, instructions[start:end])
        
        # 检查差异
        return gpy_test.diff(gmk_test, verbose=False)
    
    # 初始检查
    if not check_divergence(0, len(instructions)):
        return None  # 无差异
    
    # 二分搜索
    left, right = 0, len(instructions)
    
    while right - left > 1:
        mid = (left + right) // 2
        
        # 检查前半部分
        has_divergence = check_divergence(left, mid)
        
        if has_divergence:
            right = mid
        else:
            # 前半部分正确，检查后半部分
            gpy_partial = deepcopy_globals(gpy)
            gmk_partial = deepcopy_globals(gmk)
            
            # 先执行正确的部分
            pyvm_interpreter.interpret(gpy_partial, instructions[left:mid])
            mk_interpreter.interpret(gmk_partial, instructions[left:mid])
            
            # 再检查后半部分
            has_divergence_post = check_divergence(mid, right)
            
            if has_divergence_post:
                left = mid
            else:
                # 问题可能在两部分交互处
                return mid - 1
    
    return left  # 返回问题指令的索引
```

考虑依赖关系的版本：

```python
def locate_with_dependencies(
    pyvm_interpreter, mk_interpreter,
    gpy, gmk, instructions
):
    """
    考虑指令依赖关系的定位算法
    """
    
    # 构建依赖图
    dependencies = build_dependency_graph(instructions)
    
    # 拓扑排序层
    layers = topological_layers(dependencies)
    
    for layer in layers:
        # 测试该层
        gpy_test = deepcopy_globals(gpy)
        gmk_test = deepcopy_globals(gmk)
        
        # 执行到该层结束
        for instr in instructions:
            if instr.layer_idx < layer:
                pyvm_interpreter.interpret(gpy_test, [instr])
                mk_interpreter.interpret(gmk_test, [instr])
        
        # 检查该层
        has_divergence = check_layer_divergence(gpy_test, gmk_test, layer)
        
        if has_divergence:
            # 在该层内进行二分搜索
            return locate_in_layer(pyvm_interpreter, mk_interpreter,
                                   gpy, gmk, instructions, layer)
    
    return None
```

最小化测试次数策略：

1. **自顶向下**：先测试完整序列，确定有问题
2. **二分分割**：每次排除一半指令
3. **边界优化**：考虑依赖关系，避免不必要的分割
4. **提前终止**：一旦定位到最小问题单元就停止

测试复杂度：\(O(\log n)\) 次完整测试，对于 \(n=1000\) 条指令，约需 10 次测试。
