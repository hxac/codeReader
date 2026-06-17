# Megakernel 执行模式

本文讲解 Megakernels 的执行模式实现细节，包括 MK 解释器、PyBind11 集成、Barrier 同步机制和执行流程。

## 最小模块 1：MK 解释器架构

### 1. 概念说明

MK 解释器（MK_Interpreter）是 Megakernels 的核心执行组件，负责将 Python 层的指令和数据传递到底层 C++ megakernel。与传统 PyTorch 逐算子执行不同，MK 解释器通过**单次内核调用**完成整个 Transformer 层的前向传播，最大限度减少 CPU-GPU 交互和内核启动开销。

### 2. 伪代码或流程

```
MK 解释器执行流程：
1. 动态加载 mk_llama C++ 扩展（PyBind11 编译）
2. 预处理数据（KV cache 重排为 4D）
3. 组装参数列表（全局状态、权重、激活、标量）
4. 调用 mk_llama 函数（单次 CUDA kernel 启动）
5. GPU 内部并行执行所有指令（通过 barrier 同步）
```

### 3. 原理分析

MK 解释器的设计核心是**最小化主机-设备通信**：

传统 PyTorch 执行（逐算子）：
\[ \text{Latency} = \sum_{i=1}^{N} (\text{KernelLaunch}_i + \text{Execution}_i) \]

MK 执行（单次 megakernel）：
\[ \text{Latency} = \text{KernelLaunch} + \text{Execution}_{\text{megakernel}} \]

其中 \(N\) 是算子数量（对于 32 层 LLaMA，\(N > 500\)）。内核启动开销约 10-20μs，因此消除数百次启动可节省数毫秒延迟。

### 4. 代码实践

基础 MK 解释器定义（[`megakernels/mk.py:5-17`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/mk.py#L5-L17)）：

```python
def get_mk_func(mk_dir: Path):
    sys.path.append(str(mk_dir.expanduser().absolute()))
    from mk_llama import mk_llama  # type: ignore
    return mk_llama

class MK_Interpreter:
    def __init__(self, mk_dir: Path):
        self.mk_func = get_mk_func(mk_dir)

    def interpret(self, globs):
        raise NotImplementedError
```

`get_mk_func` 通过 `sys.path.append` 动态添加 mk_llama 所在目录，然后导入编译好的 C++ 扩展。`mk_llama` 是 PyBind11 暴露的 C++ 函数，接收所有模型参数并执行完整的推理计算。

延迟优化的具体实现（[`megakernels/demos/latency/mk.py:8-49`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py#L8-L49)）：

```python
def interpret_with_mk(globs: Globals, mk_func):
    fourD_k_cache = rearrange(globs.k_cache, "l b t h d -> (l b) t h d")
    fourD_v_cache = rearrange(globs.v_cache, "l b t h d -> (l b) t h d")

    mk_func(
        # vm stuff（虚拟机状态）
        globs.barriers,
        globs.instructions,
        globs.timings,
        # weights（模型权重，所有层堆叠）
        globs.qkv_proj_weights,
        globs.attn_ln_weights,
        globs.o_proj_weights,
        globs.mlp_ln_weights,
        globs.up_proj_weights,
        globs.gate_proj_weights,
        globs.down_proj_weights,
        globs.lm_head_norm_weights.data,
        globs.lm_head_weights.data,
        fourD_k_cache,
        fourD_v_cache,
        # rope（旋转位置编码）
        globs.rope_cos,
        globs.rope_sin,
        # activations（激活缓冲区）
        globs.hidden_states,
        globs.post_ln_rope_q,
        globs.attn_out,
        globs.attn_lse_intermediates,
        globs.attn_out_intermediates,
        globs.silu_out,
        globs.logits,
        # scalars（标量参数）
        globs.pos_id,
        globs.attn_scale,
        globs.rms_norm_eps,
        globs.skip_attn_reduction,
        stream=torch.cuda.current_stream(),
    )
```

关键步骤：
1. **KV cache 重排**：将 `[layers, batch, seq, heads, dim]` 转为 `[(layers × batch), seq, heads, dim]`，便于 C++ 端统一处理
2. **参数传递**：一次性传递所有权重、激活、配置参数，避免多次内核调用
3. **stream 传递**：确保与 PyTorch 操作的流同步

### 5. 练习题

1. MK 解释器为什么将所有层的权重堆叠传递，而不是逐层传递？这样做有什么性能优势？
2. `interpret` 方法为什么要定义为抽象方法，而不是在基类中实现？
3. KV cache 重排为什么使用 einops 的 `rearrange` 而不是 PyTorch 的 `view` 或 `reshape`？

### 6. 答案

1. **堆叠传递的优势**：
   - 消除层间内核启动：如果逐层传递，每层需要一次内核调用（32 层 = 32 次调用）
   - 允许跨层优化：C++ 端可以预取下一层权重到共享内存，减少全局内存访问
   - 简化调度：单个内核处理完整前向传播，调度器只需管理一次执行

2. **抽象方法的原因**：
   - 不同模式（latency/throughput）的参数列表可能不同（见 throughput/mk.py 多了 `batch_size`）
   - 允许子类自定义数据预处理（如 KV cache 重排方式可能不同）
   - 保持基类简洁，具体实现由 `LatencyMK_Interpreter` 和 `ThroughputMK_Interpreter` 提供

3. **使用 einops 的原因**：
   - **语义明确**：`rearrange` 清晰表达维度变换意图，比 `view`/`reshape` 更易读
   - **自动广播**：einops 自动处理高维展开，无需手动计算 stride
   - **类型安全**：einops 在运行时验证维度匹配，提前发现错误
   - PyTorch 的 `view` 要求张量在内存中连续，而 `rearrange` 会自动处理不连续情况

---

## 最小模块 2：PyBind11 集成机制

### 1. 概念说明

PyBind11 是一个轻量级的 C++/Python 绑定库，用于将 C++ 函数暴露为 Python 可调用对象。在 Megakernels 中，PyBind11 负责连接 Python 层的**解释器**与 C++ 层的**计算内核**，实现：
- 张量数据传递（零拷贝，通过 CUDA 指针）
- 标量参数传递（位置、缩放因子等）
- 流同步（确保与 PyTorch 操作的一致性）

### 2. 伪代码或流程

```
PyBind11 集成流程：
Python 端：
1. mk_llama = import_from("mk_llama.so")  # 加载 C++ 扩展
2. mk_llama(barriers, instructions, ..., stream)  # 调用

C++ 端（PyBind11 自动生成绑定代码）：
3. 将 Python 张量转换为 CUDA 指针（void*）
4. 将 Python 标量转换为 C++ 类型
5. 调用实际的 CUDA kernel
6. 自动处理引用计数和 GIL（全局解释器锁）
```

### 3. 原理分析

PyBind11 通过**类型映射**实现 Python 对象到 C++ 类型的转换：

| Python 类型 | C++ 类型 | CUDA 上下文 |
|---|---|---|
| `torch.Tensor` | `torch::Tensor`（ATen） | `data_ptr()` → `void*` |
| `int` | `int64_t` | 直接传递 |
| `float` | `double` | 直接传递 |
| `torch.cuda.Stream` | `cudaStream_t` | `cudaStream` 包装器 |

关键优化：**零拷贝传递**
- PyTorch 张量在 GPU 内存中，PyBind11 仅传递数据指针（8 字节），而非复制整个张量
- 对于 7B 模型（~26GB 权重），避免 26GB 的 CPU-GPU 数据传输

### 4. 代码实践

动态加载机制（[`megakernels/mk.py:5-9`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/mk.py#L5-L9)）：

```python
def get_mk_func(mk_dir: Path):
    sys.path.append(str(mk_dir.expanduser().absolute()))
    from mk_llama import mk_llama
    return mk_llama
```

`mk_dir` 指向编译产物的目录（如 `build/`），包含 `mk_llama.cpython-312-x86_64-linux-gnu.so`。这种设计允许：
- **多环境支持**：不同 GPU 架构编译不同版本（H100、A100 等）
- **动态切换**：无需重新安装 Python 包，只需更改 `mk_dir` 路径

PyBind11 参数传递示例（C++ 端伪代码，非项目源码）：

```cpp
// mk_llama.cu 中 PyBind11 绑定代码
PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {
    m.def("mk_llama", &mk_llama_wrapper,
          "Execute LLaMA megakernel",
          py::arg("barriers"),
          py::arg("instructions"),
          // ... 30+ 个参数
          py::arg("stream"));
}
```

每个 Python 参数通过 `py::arg()` 声明，PyBind11 自动生成类型检查和转换代码。

### 5. 练习题

1. 如果 PyBind11 传递张量时发生复制，会产生多大的性能损失？如何验证是否为零拷贝？
2. 为什么需要传递 `torch.cuda.current_stream()` 而不是默认流？
3. 如果 `mk_llama` 导入失败（如缺少 .so 文件），如何优雅降级到 Python VM 模式？

### 6. 答案

1. **拷贝开销计算**：
   - 7B 模型权重：~26GB（FP16）
   - PCIe 4.0 x16 带宽：32 GB/s
   - 复制时间：26 GB / 32 GB/s ≈ 0.81s
   - 验证方法：在 `mk_func` 调用前后插入 `torch.cuda.synchronize()` 并计时，如果时间 < 1ms 则为零拷贝
   - 也可用 `torch.cuda.memory_allocated()` 检查内存增长

2. **流传递的原因**：
   - PyTorch 默认使用**默认流**（stream 0），但 Megakernels 可能使用**自定义流**并行操作
   - 不传递流会导致：Megakernel 在流 A 运行，而 PyTorch 的 embedding/lm_head 在流 B，二者无法正确同步
   - `current_stream()` 确保所有操作在同一流中，避免数据竞争

3. **优雅降级方案**：
   ```python
   def get_mk_func(mk_dir: Path):
       try:
           sys.path.append(str(mk_dir.expanduser().absolute()))
           from mk_llama import mk_llama
           return mk_llama
       except (ImportError, AttributeError):
           logger.warning("mk_llama not found, falling back to PyVM")
           return None

   # 在 interpret 中检查
   if self.mk_func is None:
       return self.pyvm_interpret(globs)  # 使用 Python VM
   ```

---

## 最小模块 3：Barrier 同步机制

### 1. 概念说明

Barrier（屏障）是 GPU 并行计算中的**同步原语**，确保一组线程/块到达某个执行点后才继续执行。在 Megakernels 中，barrier 用于协调**多个流多处理器（SM）** 间的指令依赖，例如：
- Attention 计算必须等待 QKV MatVec 完成所有输出块
- MLP 的 Gate/Up 投影必须等待 Attention 输出

### 2. 伪代码或流程

```
Barrier 同步机制：
GPU 端（伪代码）：
for each instruction in instructions[sm_id]:
    # 1. 执行指令
    compute(instruction)

    # 2. 更新 barrier 计数
    atomicAdd(&barriers[layer][opcode][block_idx], 1)

    # 3. 等待依赖完成
    wait_until(barriers[layer][prev_opcode][dep_blocks] == expected_count)

    # 4. 重置 barrier（下次执行复用）
    if (is_last_sm_for_instruction):
        barriers[layer][opcode].zero_()
```

### 3. 原理分析

Barrier 张量的形状设计（[`megakernels/demos/latency/scheduler.py:53-61`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L53-L61)）：

```python
barriers = torch.zeros(
    [
        config.num_hidden_layers,              # 32（层数）
        10,                                     # 操作码数量（7 个实际使用）
        config.num_attention_heads + config.num_key_value_heads * 2,  # 头总数
    ],
    dtype=torch.int32,
    device=device,
)
```

三维含义：
1. **层维度**：每层独立的 barrier，避免跨层干扰
2. **操作维度**：每种操作码一个 barrier（如 opcode=1 为 QKV，opcode=2 为 Attention）
3. **块维度**：每个输出块一个计数器，用于细粒度依赖跟踪

同步算法（基于**原子操作 + 忙等待**）：
\[
\text{barrier}[l][o][b] = \sum_{i \in \text{SMs}} \mathbb{1}[\text{SM}_i \text{ 完成指令 } (l, o, b)]
\]

当 `barrier[l][o][b]` 等于依赖的 SM 数量时，后续指令可以开始执行。

### 4. 代码实践

Barrier 初始化（[`megakernels/demos/latency/scheduler.py:53-61`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L53-L61)）：

```python
barriers = torch.zeros(
    [
        config.num_hidden_layers,
        10,  # more than the number of opcodes we have
        config.num_attention_heads + config.num_key_value_heads * 2,
    ],
    dtype=torch.int32,
    device=device,
)
```

"more than the number of opcodes we have" 注释说明：预分配 10 个槽位，而实际只用 7 个（opcode 0-6），避免重新分配开销。

Barrier 重置（[`megakernels/generators.py:115-116`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L115-L116)）：

```python
def fill(self):
    self.schedule.globs.barriers.fill_(self.barrier_fill_val)
```

`barrier_fill_val` 默认为 0，每次生成新 token 前重置。如果设为非零值，可用于调试（模拟某些 SM 未完成的情况）。

依赖管理示例（[`megakernels/demos/latency/scheduler.py:330-336`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L330-L336)）：

```python
dep_set = {
    qkv_deps[(layer_idx, PartialAttention.prev_opcode(), block_idx)]
    for block_idx in block_indices
}
deps = list(dep_set)
partial_nodes.append(DAG_Node(ins, deps))
```

这里 PartialAttention（opcode=2）依赖多个 QKV 块（opcode=1），这些块在多个 SM 上并行执行。Barrier 确保所有块完成后才开始 Attention 计算。

### 5. 练习题

1. 为什么使用 `torch.int32` 而不是 `torch.int8` 存储 barrier？最小和最大可能值是多少？
2. 如果两个操作码（如 QKV 和 Attention）的 barrier 维度共享（即 10 改为 2），会产生什么问题？
3. 在什么情况下 barrier 的忙等待会导致 GPU 性能下降？如何优化？

### 6. 答案

1. **类型选择原因**：
   - **最小值**：0（初始状态）
   - **最大值**：SM 数量（如 H100 为 132），`int8`（最大 127）可能溢出
   - 原子操作要求：`atomicAdd` 对 `int32` 优化更好，`int8` 需要额外转换
   - 内存对齐：`int32` 4 字节对齐更高效，`int8` 可能导致非对齐访问

2. **共享维度的问题**：
   - **假唤醒**：如果 QKV 的 barrier 和 Attention 共享，QKV 完成时可能误触发后续的 Attention 指令
   - **清除冲突**：重置 QKV barrier 时可能影响 Attention 的计数
   - **调试困难**：无法独立验证每个操作的完成状态

3. **忙等待性能问题**：
   - **场景**：某个 SM 延迟完成（如内存访问慢），导致其他 SM 空转等待
   - **优化方法**：
     1. **优先级调度**：让关键路径上的指令先执行（DAG 调度中的 `calc_priority`）
     2. **工作窃取**：等待的 SM 可以执行其他独立指令（需要动态调度支持）
     3. **Fences 替代 Barrier**：对于点对点依赖，使用更轻量的 CUDA fences

---

## 最小模块 4：执行流程与调度

### 1. 概念说明

MK 执行流程从 Python 生成器开始，经过**指令序列化**、**SM 分配**、**内核启动**，最终在 GPU 上并行执行。核心挑战是在**满足依赖关系**的前提下，最大化 SM 利用率并最小化执行时间。

### 2. 伪代码或流程

```
完整执行流程（从生成 token 到 GPU 执行）：

Python 端（MK_Generator.run）：
1. 获取输入 token
2. 嵌入查找：hidden_states = embed(token)
3. 更新全局状态：globs.hidden_states = hidden_states
4. 更新位置 ID：globs.pos_id = pos_id
5. 重置 barrier：globs.barriers.fill_(0)
6. 调用解释器：interpreter.interpret(globs)

解释器端（interpret_with_mk）：
7. 重排 KV cache：fourD_k_cache = rearrange(k_cache, "l b t h d -> (l b) t h d")
8. 调用 mk_llama（PyBind11）

GPU 端（mk_llama 内核）：
9. 每个 SM 读取自己的指令队列：instructions[sm_id]
10. 循环执行指令，通过 barrier 同步
11. 写回最终 logits 到 globs.logits

Python 端（继续）：
12. 采样：output_ids = argmax(logits)
13. 返回生成的 token
```

### 3. 原理分析

**指令序列化**：将 Python 对象转换为 GPU 可读的格式（[`megakernels/scheduler.py:274-309`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L274-L309)）：

```python
def serialize_and_pad(instruction: Instruction):
    serialized = instruction.serialize()  # 转为 int 列表
    num_padding = INTS_PER_INSTRUCTION - len(serialized)  # 32
    return serialized + [0] * num_padding  # 填充到固定长度
```

每个指令序列化为 32 个 `int32`（128 字节），固定长度便于 GPU 端索引。填充零确保未使用的字段不影响执行。

**SM 分配策略**（[`megakernels/scheduler.py:94-151`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L94-L151)）：

- **DAG 调度**（`assign_dag_to_sms`）：基于依赖关系的优先级调度，关键路径优先
- **轮询分配**（`round_robin`）：简单负载均衡，但可能未充分利用并行性
- **波形分配**（`wave_assign`）：按操作码分组，同组指令并行执行

### 4. 代码实践

生成器主循环（[`megakernels/generators.py:145-163`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L145-L163)）：

```python
def generate(self, output_tokens: Tensor, prompt_len: int, ntok: int, ...):
    for i in range(ntok):
        input_token_pos = ntok_already_generated + i - 1
        output_token_pos = input_token_pos + 1

        input_ids = output_tokens[:, input_token_pos : input_token_pos + 1]
        pos_id = prompt_len + ntok_already_generated + i - 1

        output_ids = self.run(input_ids, pos_id=pos_id)
        output_tokens[:, output_token_pos] = output_ids.squeeze(-1)
```

每次迭代：
1. **读取上一个生成的 token**：`input_token_pos` 位置
2. **计算当前位置 ID**：基于 prompt 长度和已生成数量
3. **执行推理**：`run()` 调用解释器
4. **保存结果**：写入 `output_tokens`

单次推理执行（[`megakernels/generators.py:121-143`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L121-L143)）：

```python
def run(self, input_ids: Tensor, pos_id: int):
    if not self.skip_rest:
        batch_state = BatchState(input_ids=input_ids)
        post_embedding = self.model.model.embed_tokens(batch_state)
        hiddens = post_embedding.hidden_states
        self.schedule.globs.hidden_states[:] = hiddens.squeeze(1)

    self.fill()  # 重置 barrier
    self.schedule.globs.pos_id = pos_id

    if not self.skip_mk:
        self.interpreter.interpret(self.schedule.globs)  # 核心调用

    if self.skip_rest:
        return input_ids

    logits = self.schedule.globs.logits
    output_ids = torch.argmax(logits, dim=-1)
    return output_ids
```

关键点：
- **Embedding 复用**：使用 PyTorch 的 embedding 层（已在 GPU 上）
- **Barrier 重置**：每次生成前清零
- **Logits 采样**：简单的 argmax（未实现 beam search）

### 5. 练习题

1. 为什么不在 GPU 内核中直接执行 embedding 查找，而是先在 Python 端处理？
2. `INTS_PER_INSTRUCTION = 32` 的设计依据是什么？如何验证这个值是否足够？
3. 如果 `skip_mk=True` 但 `skip_rest=False`，会发生什么？这种模式有什么用途？

### 6. 答案

1. **Embedding 在 CPU 的原因**：
   - **计算简单**：查表操作（单次内存访问），无需复杂并行
   - **复用现有实现**：PyTorch 的 `Embedding` 层已高度优化（支持缓存、融合等）
   - **减少内核复杂度**：Megakernel 专注核心计算，简单操作交给 PyTorch
   - **灵活性**：可轻松切换不同的 embedding 技术（如 ALiBi、RoPE 的词嵌入部分）

2. **32 个 int 的依据**：
   - **统计最大指令大小**：查看所有 `Instruction.serialize()` 输出，最大为 29（如 `AttentionReduction` 带 `reduction_list`）
   - **内存对齐**：128 字节（32 × 4）是 GPU 共享内存的常见对齐单位
   - **验证方法**：
     ```python
     max_len = max(len(ins.serialize()) for ins in all_instructions)
     assert max_len <= INTS_PER_INSTRUCTION
     ```
   - 如果未来新增指令超过 32，需要增大此常量并重新编译

3. **混合模式用途**：
   - **调试验证**：`skip_mk=True` 跳过 Megakernel，但执行前后处理，可单独测试 embedding/lm_head
   - **差分测试**：与 PyTorch 原生实现对比，验证除核心计算外的逻辑正确性
   - **性能分析**：分解瓶颈，确定 Megakernel 的实际加速比
   - 如果同时为 `True`，则直接返回输入（用于单元测试的 mock 模式）

---

## 总结

本讲义覆盖了 Megakernels 执行模式的四个核心模块：

1. **MK 解释器架构**：单次内核调用消除数百次内核启动开销
2. **PyBind11 集成**：零拷贝传递 26GB 权重，避免 CPU-GPU 传输
3. **Barrier 同步**：原子操作 + 忙等待协调多 SM 依赖
4. **执行流程**：从 token 生成到 GPU 并行执行的完整管线

这些技术共同将 LLaMA 推理延迟从 PyTorch 的 ~100ms 降低到 <10ms（单 token），实现了**一个数量级的性能提升**。核心设计思想是通过**最大化 GPU 利用率**和**最小化主机-设备通信**来突破传统框架的性能瓶颈。
