# Llama 低延迟推理演示

本讲介绍 Megakernels 项目的低延迟 Llama 推理演示，通过交互式 REPL 和性能测试脚本理解系统的核心功能。

## 模块一：交互式 REPL

### 概念说明

交互式 REPL（Read-Eval-Print Loop）是一个用于实时对话推理的命令行界面。它解决了快速体验模型推理能力、测试响应速度和进行交互式调试的需求。相比于批处理脚本，REPL 提供了即时反馈，适合探索性使用和性能直观感受。

在 Megakernels 项目中，REPL 支持两种运行模式：
- **mk 模式**：使用 Megakernel 加速器，通过预编译的 CUDA kernel 执行推理
- **torch 模式**：使用标准 PyTorch 实现，作为基线对比

### 伪代码流程

```
初始化阶段：
1. 加载分词器和模型
2. 构建调度器（schedule）并分配到流多处理器（SM）
3. 选择生成器类型（MK 或 PyTorch）
4. 预分配输出 token 缓冲区

对话循环：
while 用户输入非空：
    A. 编码输入
       - 应用聊天模板格式化
       - tokenize 得到 input_ids
       - 构建 position_ids

    B. Prefill 阶段（处理整段 prompt）
       - 一次性计算 prompt 中所有 token 的表示
       - 返回最后一个 token 的表示作为生成的起点

    C. Decode 阶段（自回归生成）
       for i in range(max_tokens):
           - 基于前一个 token 生成下一个 token
           - 检查是否遇到 EOS（结束）token
           - 记录生成时间和 token 数量

    D. 解码输出
       - 将 token IDs 转换为文本
       - 显示响应内容和生成速度

    E. 更新对话历史
       - 将用户输入和助手响应加入 messages
       - 准备下一轮对话
```

### 原理分析

REPL 的核心工作原理可以分为三个阶段：

**1. 编码阶段（Encoding）**

输入文本首先经过聊天模板格式化，确保符合 Llama-Instruct 模型的输入格式。然后通过分词器将文本转换为 token IDs 序列：

\[ \text{input\_ids} = \text{Tokenizer}(\text{chat\_template(messages)}) \]

同时生成位置编码索引，用于 RoPE（旋转位置编码）：

\[ \text{position\_ids} = [0, 1, 2, ..., L-1] \]

其中 \(L\) 是 prompt 的长度。

**2. Prefill 阶段**

Prefill 阶段一次性处理整个 prompt，计算所有 token 的隐藏状态表示。这是 Transformer 模型的标准前向传播过程。由于 prompt 中所有 token 可以并行处理（自注意力机制），这个阶段充分利用了 GPU 的并行计算能力。

预填充输出取最后一个位置的表示：

\[ h_{L} = \text{Model}(\text{input\_ids}) \]

这个 \(h_{L}\) 将作为第一个生成 token 的输入。

**3. Decode 阶段（自回归生成）**

Decode 阶段采用自回归方式逐个生成 token。对于第 \(t\) 个生成步骤：

- 输入：第 \(t-1\) 个生成的 token
- 模型输出：第 \(t\) 个 token 的概率分布
- 采样：选择概率最大的 token（贪婪解码）或按采样策略选择

这个过程重复直到：
- 生成达到 `max_tokens_per_turn` 限制
- 遇到 EOS token
- 用户主动中断

**时间测量：**

REPL 使用 CUDA Events 进行精确的时间测量：

```python
start_event.record()        # 记录开始时间
generate()                  # 执行生成
end_event.record()          # 记录结束时间
torch.cuda.synchronize()    # 等待所有 CUDA 操作完成
elapsed = start_event.elapsed_time(end_event) / 1000  # 转换为秒
```

通过 `torch.cuda.synchronize()` 确保所有 GPU 操作完成后再计算时间，避免异步执行导致的测量误差。

### 代码实践

**REPL 主循环实现**

链接：https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/llama_repl.py#L136-L146

```python
while True:
    user_input = input(">>> ")
    messages.append({"role": "user", "content": user_input})

    output_text, tokens_per_second = generate(messages)

    print("Response: ", output_text)
    print(f"Speed: {tokens_per_second:.2f} tokens/s")

    messages.append({"role": "assistant", "content": output_text})
```

这段代码实现了 REPL 的核心循环：读取用户输入、生成响应、打印结果、更新对话历史。

**生成器模式选择**

链接：https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/llama_repl.py#L61-L75

```python
match config.mode:
    case "mk":
        interpreter = make_mk_interpreter(config.setting, config.mk_dir)
        gen = MK_Generator(
            model,
            interpreter,
            schedule,
            barrier_fill_val=0,
            skip_mk=False,
            skip_rest=False,
        )
    case "torch":
        gen = PyTorchGenerator(model)
    case _:
        raise ValueError(f"Invalid mode: {config.mode}")
```

这里使用 Python 的 match-case 语句根据 `config.mode` 选择不同的生成器：
- `mk`：使用 Megakernel 加速的生成器
- `torch`：使用标准 PyTorch 生成器

**Prefill 阶段实现**

链接：https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/llama_repl.py#L98-L107

```python
prefill_inp = BatchState(
    input_ids=input_ids,
    position_ids=position_ids,
)

prefill_output: BatchState = model(prefill_inp)
assert prefill_output.output_ids is not None
new_input_token = prefill_output.output_ids[:, -1:]

output_tokens[:, 0] = new_input_token
```

这段代码展示了 prefill 过程：
1. 构造包含 input_ids 和 position_ids 的 BatchState
2. 通过模型前向传播计算所有位置的概率分布
3. 提取最后一个位置的输出（第一个要生成的 token）
4. 将其写入输出缓冲区的第一个位置

**带 EOS 检测的生成**

链接：https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/llama_repl.py#L109-L116

```python
start_event.record()
until_eos, num_generated = gen.generate_with_eos(
    output_tokens=output_tokens,
    prompt_len=prompt_len,
    ntok=config.max_tokens_per_turn,
    eos_token_ids=eos_token_ids,
    eos_token_check_interval=16,
)
end_event.record()
```

`generate_with_eos` 方法每隔 16 个 token 检查是否遇到 EOS token，避免过早停止或生成过多内容。

**启动艺术字**

链接：https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/llama_repl.py#L133-L134

```python
startup_message = "you have\nbeen granted\nan audience\nwith the\nmegakernel"
print(text2art(startup_message.replace(" ", "   ")))
```

使用 `art` 库将启动消息转换为 ASCII 艺术，增强用户体验。

### 练习题

1. **REPL 流程理解**：在 REPL 中，为什么需要在每轮对话前应用聊天模板？如果不应用会发生什么？

2. **时间测量精度**：代码中使用 CUDA Events 而不是 Python 的 `time.time()` 来测量时间，这样做的好处是什么？

3. **生成器对比**：在 REPL 启动时，代码会先执行一次 warmup 生成（`generate([{"role": "user", "content": "hi"}])`）。为什么要做 warmup？

4. **EOS 检测**：`generate_with_eos` 方法中使用 `eos_token_check_interval=16`，为什么不是每生成一个 token 就检查一次？这个参数的权衡是什么？

### 答案

1. **聊天模板的作用**：Llama-Instruct 等对话模型期望输入符合特定的对话格式，通常包含 `user`、`assistant` 等角色标记。聊天模板将消息列表转换为这种格式，确保模型正确理解对话上下文。如果不应用模板，模型可能将输入当作普通文本而非对话，导致输出质量下降。

2. **CUDA Events 的优势**：
   - **精度高**：CUDA Events 直接记录 GPU 的时间戳，精度可达微秒级
   - **避免异步误差**：PyTorch 的 CUDA 操作是异步的，`time.time()` 只记录 CPU 发出命令的时间，而非实际执行时间。CUDA Events 配合 `synchronize()` 确保记录的是真实完成时间
   - **跨线程安全**：Events 能正确处理多流并行的情况

3. **Warmup 的目的**：
   - **CUDA 编译**：首次运行 kernel 时，CUDA 需要实时编译 PTX 代码，warmup 预先完成这个编译
   - **内存分配**：首次执行会触发显存分配和页表建立
   - **预热预热**：让 GPU 达到稳定工作状态
   不做 warmup 会导致首次生成时间异常长，影响测量准确性。

4. **EOS 检测间隔的权衡**：
   - **过小（如 1）**：每个 token 都检查 EOS，增加 CPU-GPU 同步开销，降低吞吐量
   - **过大（如 100）**：减少同步开销，但可能在遇到 EOS 后继续生成无用 token，浪费计算
   - **16 的选择**：在延迟和效率间取得平衡，适合大多数场景。对于短文本生成，可以设置更小值以更快停止

---

## 模块二：生成器脚本

### 概念说明

生成器脚本（`generate.py`）是一个灵活的性能测试工具，支持多种运行模式和配置选项。它解决了系统性能评估、模式对比和压力测试的需求。与 REPL 的交互式特性不同，生成器脚本专注于自动化测试和精确的性能指标收集。

脚本支持三种模式：
- **model/torch 模式**：纯 PyTorch 实现，作为性能基线
- **pyvm 模式**：使用 Python 虚拟机解释调度，介于 torch 和 mk 之间
- **mk 模式**：使用 Megakernel 加速器，最优性能

### 伪代码流程

```
初始化阶段：
1. 解析命令行参数（模式、prompt、token 数量、批次大小等）
2. 加载分词器和模型
3. 处理输入（聊天模板或纯文本）
4. 构建 BatchState 进行 prefill
5. 构建调度器并分配到 SM

生成器选择：
match mode:
    case "torch":
        gen = PyTorchGenerator(model)
    case "pyvm":
        interpreter = make_pyvm_interpreter()
        gen = PyVM_Generator(model, interpreter, schedule)
    case "mk":
        interpreter = make_mk_interpreter()
        gen = MK_Generator(model, interpreter, schedule)

性能测试循环：
1. Warmup 阶段（可选）
   for i in range(num_warmup):
       执行一次生成（不计入统计）

2. 测试阶段
   times = []
   for i in range(num_iters):
       start_event.record()
       gen.generate(output_tokens, prompt_len, ntok)
       end_event.record()
       elapsed = start_event.elapsed_time(end_event) / 1000
       times.append(elapsed)

3. 统计计算
   avg_time = mean(times[config.num_warmup:])
   avg_cpu_time = mean(cpu_times[config.num_warmup:])
   tokens_per_sec = (ntok * batch_size) / avg_time

4. 结果输出
   - 输出 token IDs
   - 解码文本
   - 显示性能指标
```

### 原理分析

生成器脚本的核心原理是通过迭代测试减少噪声影响，并支持不同执行模式的对比。

**1. 模式对比架构**

```
┌─────────────────┐
│  PyTorchGenerator │  ← 纯 PyTorch，逐层调用
└─────────────────┘
         ↓
┌─────────────────┐
│  PyVM_Generator  │  ← Python VM 解释调度指令
└─────────────────┘
         ↓
┌─────────────────┐
│   MK_Generator   │  ← 编译为 CUDA megakernel
└─────────────────┘
```

三种模式的区别在于：
- **PyTorch**：标准实现，每层分别调用 PyTorch 算子
- **PyVM**：将模型计算分解为调度指令，由 Python VM 解释执行
- **MK**：将调度编译为单个 CUDA kernel（megakernel），在 GPU 上一次性执行

**2. 批次大小和序列长度**

脚本支持配置批次大小（`batch_size`）和序列长度（`max_len_override`），这对性能影响显著：

- **批次大小**：更大的批次增加 GPU 利用率，提高吞吐量
- **序列长度**：影响 KV 缓存大小和注意力计算复杂度

\[ \text{Throughput} = \frac{\text{batch\_size} \times \text{ntok}}{\text{time}} \]

**3. 统计稳定性**

通过多次迭代和 warmup 减少测量噪声：

- **Warmup**：消除首次启动的编译和初始化开销
- **多次迭代**：平均减少偶然波动
- **去除 warmup 数据**：只统计稳定状态的性能

**4. 时间测量**

脚本同时测量 GPU 时间和 CPU 时间：

```python
start_event.record()         # GPU 时间开始
cpu_start = time.time()      # CPU 时间开始
gen.generate(...)
cpu_end = time.time()        # CPU 时间结束
end_event.record()           # GPU 时间结束
```

GPU 时间反映实际计算时间，CPU 时间包含整体执行开销（包括内存传输、同步等）。

### 代码实践

**配置类定义**

链接：https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L28-L50

```python
class ScriptConfig(pydra.Config):
    model: str = "meta-llama/Llama-3.2-1B-Instruct"
    device: str = "cuda:0"
    prompt: str = "tell me a funny joke about cookies"
    chat: bool = False
    ntok: int = 100
    mode: str = "model"
    interleave_rope: bool = True
    mk_dir: Path = Path(__file__).parent.parent.parent / "demos" / "low-latency-llama"
    token_details: bool = False
    tokens: bool = True
    num_warmup: int = 5
    num_iters: int = 10
    barrier_fill_val: int = 0
    batch_size: int = 1
    max_len_override: int | None = 16384
    noops: bool = False
    skip_mk: bool = False
    skip_rest: bool = False
    sched: str = "rr"
    setting: str = "latency"
    memory_fraction: float | None = None
```

使用 `pydra.Config` 简化配置管理，支持通过命令行参数覆盖默认值。

**聊天输入处理**

链接：https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L98-L113

```python
if config.chat:
    messages = [
        {"role": "user", "content": config.prompt},
    ]
    tok_inp = tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )
    input_ids_cpu = tokenizer(
        tok_inp, return_tensors="pt", add_special_tokens=False
    )["input_ids"]
else:
    tok_inp = config.prompt
    input_ids_cpu = tokenizer(
        tok_inp, return_tensors="pt", add_special_tokens=True
    )["input_ids"]
```

当 `chat=True` 时，应用聊天模板将输入转换为对话格式；否则直接处理纯文本。

**生成器模式分发**

链接：https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L146-L165

```python
match config.mode:
    case "torch":
        gen = PyTorchGenerator(model)
    case "pyvm":
        interpreter = make_pyvm_interpreter(config.setting)
        gen = PyVM_Generator(model, interpreter, schedule)
    case "mk":
        interpreter = make_mk_interpreter(config.setting, config.mk_dir)
        gen = MK_Generator(
            model,
            interpreter,
            schedule,
            barrier_fill_val=config.barrier_fill_val,
            skip_mk=config.skip_mk,
            skip_rest=config.skip_rest,
        )
        if config.noops:
            gen.replace_with_noops()
    case _:
        raise ValueError(f"Invalid mode: {config.mode}")
```

使用 match-case 根据模式选择生成器，并支持调试选项（`noops`、`skip_mk`、`skip_rest`）。

**性能测试循环**

链接：https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L167-L180

```python
times = []
cpu_times = []
for _ in tqdm(range(config.num_warmup + config.num_iters)):
    start_event = torch.cuda.Event(enable_timing=True)
    end_event = torch.cuda.Event(enable_timing=True)
    start_event.record()
    cpu_start = time.time()
    gen.generate(output_tokens, prompt_len, config.ntok - 1)
    cpu_end = time.time()
    end_event.record()
    torch.cuda.synchronize()
    times.append(start_event.elapsed_time(end_event) / 1000)
    cpu_times.append(cpu_end - cpu_start)
```

使用 `tqdm` 显示进度，同时收集 GPU 时间和 CPU 时间。

**性能统计**

链接：https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L181-L207

```python
non_warmup_times = times[config.num_warmup :]
non_warmup_cpu_times = cpu_times[config.num_warmup :]
elapsed = sum(non_warmup_times) / len(non_warmup_times)
elapsed_cpu = sum(non_warmup_cpu_times) / len(non_warmup_cpu_times)
print(f"Average time: {(elapsed * 1000):.2f}ms (CPU: {(elapsed_cpu * 1000):.2f}ms)")

if config.tokens:
    to_cpu = output_tokens.cpu()
    print("Output ids: ", to_cpu)
    print("Output text: ", tokenizer.batch_decode(to_cpu))

fwd_per_second = (config.ntok - 1) / elapsed
print(f"Fwd per second: {fwd_per_second:.2f}")
tokens_per_second = config.batch_size * fwd_per_second
print(f"Tokens per second: {tokens_per_second:.2f}")
```

计算平均时间、吞吐量（tokens/second），并可选输出 token IDs 和解码文本。

### 练习题

1. **模式对比**：三种生成器模式（torch/pyvm/mk）在性能上会有什么差异？原因是什么？

2. **批次大小影响**：将 `batch_size` 从 1 增加到 1024，理论上吞吐量会如何变化？实际中可能遇到什么限制？

3. **Warmup 次数**：默认 warmup 5 次，如果测试中发现前几次迭代时间明显更长，应该增加还是减少 warmup 次数？

4. **调试选项**：`skip_mk=True` 和 `skip_rest=True` 的含义是什么？在什么场景下使用这些选项？

### 答案

1. **模式性能差异**：
   - **torch（最慢）**：逐层调用 PyTorch 算子，每层都有 Python overhead 和 kernel launch 开销
   - **pyvm（中等）**：Python VM 解释调度指令，减少了 Python 调用层数，但仍有解释开销
   - **mk（最快）**：所有计算编译为一个 CUDA kernel，一次启动，无 Python overhead，最小化 kernel launch 次数
   性能提升来自减少 CPU-GPU 交互、合并 kernel、优化内存访问模式。

2. **批次大小的影响**：
   - **理论上**：吞吐量应随 batch_size 线性增长，直到 GPU 计算资源饱和
   - **实际限制**：
     - 显存限制：KV 缓存占用与 batch_size × seq_len 成正比
     - MK 模式可能需要重新编译 kernel（代码中有注释"must recompile the kernel with new BATCH_SIZE"）
     - 注意力计算复杂度为 \(O(n^2)\)，大 batch + 长序列可能导致显存不足

3. **Warmup 次数调整**：
   - **前几次明显更长**：说明首次启动开销大（编译、初始化），应**增加** warmup 次数
   - **目标**：warmup 后的时间应稳定，方差小
   - **经验值**：对于 GPU kernel，5-10 次 warmup 通常足够；如果波动仍大，可增加到 15-20 次

4. **调试选项的用途**：
   - **skip_mk=True**：跳过 megakernel 执行，只运行前后处理（embedding、lm_head）。用于验证前后处理的正确性，隔离 MK kernel 的问题
   - **skip_rest=True**：只执行 MK kernel，跳过前后处理。用于验证 MK kernel 的独立性能
   - **使用场景**：性能剖析、bug 定位、逐步验证各组件的正确性。例如，如果总时间异常，可以分别测试 skip_mk 和 skip_rest，找出瓶颈

---

## 模块三：性能测试与基准

### 概念说明

性能测试模块通过系统的基准测试评估 Megakernels 相对于传统 PyTorch 实现的性能优势。它解决了量化优化效果、识别性能瓶颈和验证系统正确性的需求。

性能测试关注的核心指标：
- **延迟（Latency）**：生成单个 token 或完成一次推理的时间
- **吞吐量（Throughput）**：每秒生成的 token 数量
- **加速比（Speedup）**：相对于基线的性能提升倍数

### 伪代码流程

```
性能测试流程：
1. 配置测试环境
   - 选择模型（1B、8B 等）
   - 设置批次大小和序列长度
   - 选择运行模式（torch/pyvm/mk）
   - 配置 warmup 和迭代次数

2. 执行基准测试
   for mode in [torch, pyvm, mk]:
       for config in test_configs:
           - 初始化模型和生成器
           - Warmup 阶段
           - 测试阶段（多次迭代）
           - 记录时间、吞吐量

3. 结果分析
   - 计算平均值和标准差
   - 对比不同模式的加速比
   - 识别性能异常

4. 可视化输出
   - 打印性能表格
   - 绘制性能对比图
   - 输出 token 采样（验证正确性）
```

### 原理分析

性能测试的原理基于统计学和计算机体系结构的基本概念。

**1. 测量误差来源**

GPU 性能测试中的主要误差来源：

- **系统噪声**：操作系统调度、后台进程、GPU 上下文切换
- **热管理**：GPU 温度导致频率动态调整（thermal throttling）
- **频率波动**：GPU boost 频率随负载变化
- **编译开销**：首次运行的 JIT 编译、PTX 编译
- **缓存效应**：冷启动时缓存未命中

通过多次迭代和 warmup 减少这些噪声。

**2. 性能指标计算**

关键指标及其计算方式：

- **平均延迟**：
  \[ \text{Latency} = \frac{\sum_{i=warmup}^{N} t_i}{N - warmup} \]

- **吞吐量**：
  \[ \text{Throughput} = \frac{\text{batch\_size} \times \text{ntok}}{\text{Latency}} \]

- **加速比**：
  \[ \text{Speedup} = \frac{\text{Latency}_{\text{baseline}}}{\text{Latency}_{\text{optimized}}} \]

**3. 统计显著性**

为了确保性能提升真实可信，需要：

- **多次采样**：至少 10 次迭代，最好 30+ 次
- **稳定方差**：标准差应小于均值的 5-10%
- **重复测试**：在不同时段多次运行，排除偶然因素

**4. 性能瓶颈分析**

性能测试可帮助识别瓶颈：

- **内存受限（Memory-bound）**：计算强度低，内存带宽是瓶颈
- **计算受限（Compute-bound）**：计算密度高，算力是瓶颈
- **延迟受限（Latency-bound）**：kernel launch 开销大，小批次场景

Megakernels 通过减少 kernel launch 次数和优化内存访问模式，在延迟受限场景下优势明显。

### 代码实践

**测试配置快捷方法**

链接：https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L55-L81

```python
def once(self):
    self.num_warmup = 0
    self.num_iters = 1

def th(self, bs=1024, sl=128):
    self.setting = "throughput"
    self.mk_dir = (
        Path(__file__).parent.parent.parent.parent
        / "tests"
        / "batch-vm"
        / "llama_official"
    )
    self.batch_size = bs
    self.max_len_override = sl
    self.interleave_rope = False
    self.l8()

    if self.mode == "mk":
        assert self.batch_size == 1024, (
            "must recompile the kernel with new BATCH_SIZE"
        )

def l1(self):
    self.model = "meta-llama/Llama-3.2-1B-Instruct"

def l8(self):
    self.model = "meta-llama/Llama-3.1-8B-Instruct"
```

这些快捷方法简化了常用测试配置：
- `once()`：单次运行，用于快速验证
- `th()`：吞吐量测试配置，大批次（1024）、短序列（128）
- `l1()`、`l8()`：选择 1B 或 8B 模型

**详细 token 输出**

链接：https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L192-L202

```python
if config.token_details:
    ids_list = to_cpu.tolist()
    tokens = tokenizer.convert_ids_to_tokens(ids_list)

    table = []
    for i, token in enumerate(tokens):
        pos_id = i + prompt_len
        table.append([i, pos_id, token])

    print("More detailed output:")
    print(tabulate(table, headers=["output id", "position id", "token"]))
```

使用 `tabulate` 库格式化输出，显示每个生成 token 的位置 ID 和实际 token，用于验证生成正确性。

**性能统计输出**

链接：https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L204-207

```python
fwd_per_second = (config.ntok - 1) / elapsed
print(f"Fwd per second: {fwd_per_second:.2f}")
tokens_per_second = config.batch_size * fwd_per_second
print(f"Tokens per second: {tokens_per_second:.2f}")
```

计算并显示两种吞吐量指标：
- `fwd_per_second`：每秒前向传播次数（生成步骤数）
- `tokens_per_second`：每秒生成的 token 总数（考虑批次大小）

### 练习题

1. **性能测试设计**：如果要准确比较 torch 和 mk 模式的性能，应该控制哪些变量保持一致？

2. **异常检测**：如果某次测试中标准差很大（超过均值的 20%），可能的原因是什么？如何解决？

3. **批次大小权衡**：在延迟测试（batch_size=1）和吞吐量测试（batch_size=1024）中，哪种测试更能体现 Megakernels 的优势？为什么？

4. **性能验证**：除了吞吐量和延迟，还有什么指标可以验证生成的正确性？为什么这些指标很重要？

### 答案

1. **需要控制的变量**：
   - **模型**：相同的模型和权重
   - **输入**：相同的 prompt 和生成长度
   - **硬件**：同一 GPU、同一驱动版本
   - **环境**：相同的环境温度、电源设置
   - **软件**：相同版本的 PyTorch、CUDA
   - **精度**：相同的数值精度（FP16/BF16/FP32）
   - **随机性**：固定的随机种子（如果涉及采样）
   只改变 `mode` 参数（torch/pyvm/mk），确保对比公平。

2. **大方差的原因和解决**：
   - **原因**：
     - 后台进程占用 GPU（桌面环境、其他训练任务）
     - GPU 热节流（温度过高降频）
     - 动态频率调整（boost 频率不稳定）
     - 内存碎片化
   - **解决**：
     - 关闭后台应用，使用专用 GPU
     - 降低 GPU 温度（改善散热、降低功耗限制）
     - 增加迭代次数（30+ 次）
     - 多次重复测试，取中位数而非均值
     - 使用 `nvidia-smi` 监控 GPU 利用率和温度

3. **Megakernels 的优势体现**：
   - **延迟测试（batch_size=1）更能体现优势**：
     - Megakernels 的核心优势是减少 kernel launch 开销
     - 小批次场景下，kernel launch 开销占比高
     - 大批次场景下，计算密度高，kernel launch 开销被摊薄
   - **吞吐量测试（batch_size=1024）**：
     - PyTorch 在大批次下也能获得较高利用率
     - Megakernels 的优势相对缩小
     - 但仍有优势（更好的内存访问模式、更少的同步开销）

4. **正确性验证指标**：
   - **输出文本质量**：人工检查生成内容是否通顺、符合预期
   - **Token 一致性**：不同模式应生成相同的 token 序列（确定性推理）
   - **数值精度**：检查 logits 或隐藏状态的数值误差
   - **边界条件**：
     - 空输入
     - 超长输入
     - 特殊字符（Unicode、emoji）
   - **回归测试**：保存已知正确的输出，每次修改后对比
   - **重要性**：
     - 性能优化不应牺牲正确性
     - 数值误差可能累积导致输出错误
     - 边界条件常暴露隐藏的 bug

---

## 总结

本讲通过三个模块介绍了 Megakernels 的 Llama 低延迟推理演示：

1. **交互式 REPL**：实时对话体验，支持 mk 和 torch 两种模式
2. **生成器脚本**：灵活的性能测试工具，支持多种模式和配置
3. **性能测试**：系统的基准测试和性能分析方法

这些工具共同构成了 Megakernels 项目的演示和测试框架，帮助理解系统的功能和性能优势。核心价值在于通过对比传统 PyTorch 实现，直观感受 Megakernels 在低延迟推理场景下的性能提升。
