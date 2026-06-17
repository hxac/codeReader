# 性能测试与基准

在前面的学习中，我们了解了 Megakernels 的虚拟机架构和指令执行机制。在实际开发中，我们需要对这些系统进行性能测试和基准对比，以确保优化是有效的，不同实现之间是等价的，以及调度策略是合理的。本讲义将介绍 Megakernels 中的性能测试框架和差异测试工具。

## 最小模块 1：性能基准测试

### 1.1 概念说明

性能基准测试（Benchmarking）是测量和比较系统性能的关键方法。在 Megakernels 项目中，我们需要测量：

- **吞吐量（Throughput）**：系统每秒能处理的 token 数量
- **延迟（Latency）**：单个请求的响应时间
- **不同实现的对比**：Python 虚拟机 vs Megakernels 实现

基准测试的目的是建立一个性能基线，帮助我们：
1. 验证优化是否有效
2. 发现性能回归
3. 比较不同实现方案的优劣

### 1.2 伪代码或流程

```python
def benchmark_throughput(config):
    # 启动服务器（如果需要）
    server = launch_server_if_needed(config)
    
    # 预热阶段（排除编译和初始化开销）
    for i in range(config.num_warmup):
        run_single_request(config)
    
    # 正式测试阶段
    times = []
    for i in range(config.num_iters):
        start = current_time()
        result = run_single_request(config)
        end = current_time()
        times.append(end - start)
    
    # 计算统计指标
    mean_time = mean(times)
    stdev_time = stdev(times)
    
    # 计算吞吐量
    tokens_per_request = config.batch_size * config.output_len
    throughput = tokens_per_request / mean_time
    
    return throughput, mean_time, stdev_time
```

### 1.3 原理分析

#### 基线测试方法

Megakernels 使用**基线减法**来计算纯生成时间：

1. **Baseline 测试**：运行只生成 1 个 token 的请求
   - 测量时间 \( T_{baseline} \)，包含了所有固定开销（网络、调度、模型加载等）

2. **Full 测试**：运行生成 \( N \) 个 token 的请求
   - 测量时间 \( T_{full} \)

3. **纯生成时间**：
   \[
   T_{generation} = T_{full} - T_{baseline}
   \]

4. **吞吐量计算**：
   \[
   \text{Throughput} = \frac{\text{batch\_size} \times (N - 1)}{T_{generation}} \quad \text{tokens/s}
   \]

#### 统计显著性

使用多次迭代（`num_iters`）并计算均值和标准差，确保结果的可靠性：
- **均值**：代表平均性能
- **标准差**：反映性能的波动程度

### 1.4 代码实践

Megakernels 的基准测试实现在 `megakernels/scripts/bench_engines.py` 中：

**核心配置类**：
[ScriptConfig](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/bench_engines.py#L14-L40) 定义了所有测试参数

**基准测试函数**：
```python
def go(config: ScriptConfig, client: OpenAI, n_in: int, n_out: int, batch_size: int):
    times = []
    
    # 预热 + 正式测试
    for i in tqdm(range(config.num_warmup + config.num_iters)):
        start = time.time()
        resp = client.completions.create(
            model=config.model,
            prompt=[0] * n_in,          # 输入 token 数
            max_tokens=n_out,            # 输出 token 数
            temperature=config.temperature,
            n=batch_size,                 # 批大小
            extra_body={"ignore_eos": True},
        )
        end = time.time()
        
        # 跳过预热阶段的数据
        if i >= config.num_warmup:
            times.append(end - start)
    
    return mean(times), stdev(times)
```

这段代码[在 124-143 行](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/bench_engines.py#L124-L143)，实现了单次基准测试的核心逻辑。

**主测试流程**：
```python
def main(config: ScriptConfig):
    # 启动服务器（如果配置了 launch）
    with launch_server(config):
        client = OpenAI(
            api_key="fake-key",
            base_url=f"http://0.0.0.0:{config.port}/v1",
        )
        
        # 基线测试：只生成 1 个 token
        baseline_mean, baseline_stdev = go(
            config, client,
            n_in=config.prompt_len,
            n_out=1,                    # 基线只生成 1 个 token
            batch_size=config.batch_size,
        )
        
        # 完整测试：生成所有 token
        run_mean, run_stdev = go(
            config, client,
            n_in=config.prompt_len,
            n_out=config.output_len,    # 生成完整输出
            batch_size=config.batch_size,
        )
        
        # 计算吞吐量
        diff = run_mean - baseline_mean
        tokens = config.batch_size * (config.output_len - 1)
        tps = tokens / diff
        print(f"Throughput: {tps} tokens/s")
```

这段代码[在 146-178 行](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/bench_engines.py#L146-L178)，展示了基线减法计算吞吐量的完整流程。

### 1.5 练习题

1. **理解基线减法**：为什么要先测试 `n_out=1` 的基线，而不是直接测量生成时间？

2. **批处理影响**：如果 `batch_size=4`，`output_len=128`，理论上最多能提升多少倍吞吐量？实际中为什么达不到这个理论值？

3. **统计显著性**：如果 `num_iters=10`，标准差很大，说明什么？应该如何改进测试？

4. **预热的作用**：如果去掉预热阶段（`num_warmup=0`），测试结果会偏高还是偏低？为什么？

### 1.6 答案

1. **基线减法的目的**：因为推理时间包含固定开销（网络传输、模型加载、调度等）和可变开销（token 生成）。基线测试测量的是固定开销，减去后才能得到纯生成时间，避免固定开销干扰吞吐量计算。

2. **批处理的理论提升**：理论上最大提升 4 倍。但实际中达不到的原因包括：
   - 内存带宽限制
   - SM 负载不均衡
   - 内存访问模式不是完全并行的
   - 核间通信开销

3. **标准差大的含义**：说明性能不稳定，可能原因：
   - 系统后台任务干扰
   - GPU 温度节流
   - 内存碎片化
   - 改进方法：增加 `num_iters`、在专用测试机上运行、监控 GPU 状态

4. **预热的影响**：去掉预热后结果会偏低。因为第一次运行包含：
   - JIT 编译开销
   - 缓存冷启动
   - 内存分配初始化
   预热阶段让系统进入稳定状态，后续测试才代表真实性能。

---

## 最小模块 2：差异测试

### 2.1 概念说明

差异测试（Differential Testing）是一种通过比较多个实现的输出来发现错误的方法。在 Megakernels 项目中，我们有两个关键实现：

1. **PyVM（Python 虚拟机）**：参考实现，用 Python 解释每条指令
2. **MK（Megakernels）**：优化实现，在 GPU 上批量执行指令

差异测试的目的：
- **正确性验证**：确保优化实现与参考实现结果一致
- **调试辅助**：通过比较中间状态定位错误
- **回归检测**：代码变更后快速发现引入的错误

### 2.2 伪代码或流程

```python
def differential_test(config):
    # 加载模型
    model = load_model(config.model)
    
    # 构建调度
    schedule = build_schedule(model, config.layer_limit)
    instructions = schedule.get_linear_instructions()
    
    # 创建两个解释器
    pyvm_interpreter = PyVM_Interpreter()
    mk_interpreter = MK_Interpreter()
    
    # 初始化全局状态（确保两个解释器从相同状态开始）
    pyvm_globals = init_globals(model)
    mk_globals = clone_globals(pyvm_globals)
    
    # 分配指令到 SM
    assigned_queues = assign_to_sms(config.sched, instructions, sm_count)
    
    # 用 PyVM 执行
    pyvm_interpreter.interpret(pyvm_globals, instructions)
    
    # 用 MK 执行
    mk_interpreter.interpret(mk_globals, assigned_queues)
    
    # 比较结果
    diff_results = compare_globals(pyvm_globals, mk_globals)
    
    return diff_results
```

### 2.3 原理分析

#### 测试流程

差异测试的核心是**控制变量法**：

1. **相同的初始状态**：两个解释器从完全相同的全局状态开始
   - 相同的模型权重
   - 相同的输入数据
   - 相同的 KV 缓存（需要克隆，避免共享）

2. **相同的指令序列**：两个解释器执行相同的指令，只是执行方式不同
   - PyVM：逐条顺序执行
   - MK：批量并行执行

3. **比较最终状态**：检查所有全局状态是否一致
   - `hidden_states`：隐藏状态
   - `k_cache`、`v_cache`：KV 缓存
   - 其他张量状态

#### 错误定位

如果发现差异，可以：
- 设置 `stop_after_op` 参数，在特定操作后停止
- 比较中间状态，定位首次出现差异的位置
- 使用断点（`bp=True`）进入调试模式

### 2.4 代码实践

差异测试实现在 `megakernels/scripts/diff_test.py` 中：

**配置类**：
[ScriptConfig](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/diff_test.py#L22-L69) 支持丰富的测试选项

**关键初始化**：
```python
# 初始化全局状态
gpy = spy.globs  # PyVM 全局状态
gmk = smk.globs  # MK 全局状态

# 设置位置索引
seq_len = config.prompt_len + config.ntok
pos_id = seq_len - 1
gpy.pos_id = pos_id
gmk.pos_id = pos_id

# 初始化 hidden_states（两个解释器相同）
normal_(gpy.hidden_states)
gmk.hidden_states.copy_(gpy.hidden_states)  # 复制确保完全相同

# 初始化 KV 缓存（重要：必须克隆！）
normal_(gpy.k_cache[:, :, :seq_len])
normal_(gpy.v_cache[:, :, :seq_len])
normal_(gpy.k_cache[:, :, seq_len:], std=100)
normal_(gpy.v_cache[:, :, seq_len:], std=100)

# 克隆 KV 缓存，避免两个解释器共享同一缓存
smk.globs.k_cache = spy.globs.k_cache.clone()
smk.globs.v_cache = spy.globs.v_cache.clone()
```

这段代码[在 97-118 行](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/diff_test.py#L97-L118)，展示了如何确保两个解释器从相同状态开始。

**执行和比较**：
```python
# 执行 PyVM
print("interpreting with pyvm...")
start = time.time()
pyvm_interpreter.interpret(gpy, instructions)
torch.cuda.synchronize()
end = time.time()
print(f"pyvm time: {end - start}")

# 执行 MK
print("interpreting with mk...")
start = time.time()
mk_interpreter.interpret(gmk)
torch.cuda.synchronize()
end = time.time()
print(f"mk time: {end - start}")

# 比较结果
print("done! diffing tensors:")
gpy.diff(gmk)  # 这个方法会比较所有张量并报告差异
```

这段代码[在 209-226 行](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/diff_test.py#L209-L226)，展示了执行和比较的流程。

### 2.5 练习题

1. **克隆的重要性**：为什么要用 `.clone()` 复制 KV 缓存，而不是直接赋值？如果不克隆会发生什么？

2. **同步的作用**：为什么在 `pyvm_interpreter.interpret()` 后要调用 `torch.cuda.synchronize()`？

3. **性能对比**：如果 MK 比 PyVM 慢，可能是什么原因？如何进一步调试？

4. **部分执行**：如何使用 `stop_after_op` 参数来定位首次出现差异的指令？

### 2.6 答案

1. **克隆的重要性**：因为 PyTorch 张量默认是引用语义。直接赋值会让两个解释器共享同一个 KV 缓存张量，当一个解释器更新缓存时，另一个也会看到变化，无法区分是执行逻辑错误还是共享状态干扰。克隆创建独立的副本，确保两个解释器的状态完全独立。

2. **同步的作用**：因为 GPU 执行是异步的。`interpret()` 调用会把操作提交到 GPU 队列，但 CPU 会继续执行。如果不同步，时间测量会不准确（只记录了提交时间，不是实际执行时间），而且可能在一个操作还没完成时就读取其结果。

3. **MK 比 PyVM 慢的原因**：
   - SM 负载不均衡，有些 SM 空闲等待
   - 指令调度策略不当，导致依赖等待
   - 内存访问模式不好，缓存未命中
   - 调试方法：查看 `gmk.timings` 分析每个 SM 的利用率，尝试不同的 `sched` 参数

4. **使用 stop_after_op**：
   ```python
   # 第一次测试：在前半段查找
   config.stop_after_op = "middle_operation"
   # 如果无差异，说明错误在后半段
   
   # 第二次测试：在后半段查找
   config.start_after_op = "middle_operation"
   # 逐步缩小范围，直到定位到具体指令
   ```

---

## 最小模块 3：模式对比

### 3.1 概念说明

Megakernels 支持两种主要的执行模式：

1. **Latency 模式**：优化单请求延迟
   - 适用于交互式应用（聊天机器人、实时翻译）
   - 目标：最小化单个请求的响应时间
   - 策略：减少批处理、优化内存访问模式

2. **Throughput 模式**：优化批量处理吞吐量
   - 适用于离线处理（批量文本生成、批处理任务）
   - 目标：最大化单位时间处理的 token 数
   - 策略：大 batch size、数据并行、计算重叠

模式对比测试的目的是：
- 在不同场景下选择合适的模式
- 理解模式 trade-off（延迟 vs 吞吐量）
- 验证模式切换的正确性

### 3.2 伪代码或流程

```python
def compare_modes(config):
    results = {}
    
    for mode in ["latency", "throughput"]:
        # 构建该模式的调度器
        builder = make_schedule_builder(mode)
        schedule = builder.build(model, layer_limit)
        
        # 创建该模式的解释器
        mk_interpreter = make_mk_interpreter(mode, mk_dir)
        pyvm_interpreter = make_pyvm_interpreter(mode)
        
        # 运行测试
        pyvm_time = run_pyvm_test(pyvm_interpreter, schedule)
        mk_time = run_mk_test(mk_interpreter, schedule)
        
        # 计算加速比
        speedup = pyvm_time / mk_time
        
        results[mode] = {
            "pyvm_time": pyvm_time,
            "mk_time": mk_time,
            "speedup": speedup,
        }
    
    return results
```

### 3.3 原理分析

#### 模式差异

**Latency 模式的特点**：
- 单层调度（或少量层）
- 小 batch size（通常 1）
- 指令按层顺序执行
- 内存访问模式优化为连续访问

**Throughput 模式的特点**：
- 多层调度
- 大 batch size（可达 1024）
- 指令跨层重排，最大化并行
- 使用 wave scheduling 或其他高级调度策略

#### 性能权衡

对于单个请求：
\[
\text{Latency}_{\text{throughput}} > \text{Latency}_{\text{latency}}
\]

对于批量请求：
\[
\text{Throughput}_{\text{throughput}} \gg \text{Throughput}_{\text{latency}}
\`

### 3.4 代码实践

在 `diff_test.py` 中，模式通过配置选择：

**模式切换**：
```python
class ScriptConfig(pydra.Config):
    setting: str = "latency"  # 或 "throughput"
    
    def th(self, bs=1024, sl=128):
        """切换到吞吐量模式"""
        self.setting = "throughput"
        self.batch_size = bs
        self.max_len_override = sl
```

这段代码[在 42-62 行](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/diff_test.py#L42-L62)，展示了模式配置。

**构建模式特定的组件**：
```python
# 根据模式创建调度器
builder = make_schedule_builder(config.setting)
schedule = builder.build(model, layer_limit, stop_after_op)

# 根据模式创建解释器
mk_interpreter = make_mk_interpreter(config.setting, mk_dir)
pyvm_interpreter = make_pyvm_interpreter(config.setting)
```

这段代码[在 85-87 行](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/diff_test.py#L85-L87)，展示了模式特定组件的创建。

**工厂函数实现**（在 `dispatch.py` 中）：
```python
BUILDER_MAP = {
    "latency": LatencyScheduleBuilder,
    "throughput": ThroughputScheduleBuilder,
}

MK_INTERPRETER_MAP = {
    "latency": LatencyMK_Interpreter,
    "throughput": ThroughputMK_Interpreter,
}

def make_schedule_builder(mode: str) -> ScheduleBuilder:
    return BUILDER_MAP[mode]()
```

这段代码[在 17-34 行](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/dispatch.py#L17-L34)，展示了工厂模式的实现。

### 3.5 练习题

1. **模式选择**：对于一个实时聊天应用（batch_size=1），应该选择哪种模式？为什么？

2. **批量处理**：对于批量文本摘要任务（batch_size=64），应该选择哪种模式？预期加速比是多少？

3. **混合场景**：如果一个系统既要处理实时请求（batch_size=1），又要处理批量任务（batch_size=64），应该如何架构？

4. **性能建模**：假设 Throughput 模式在 batch_size=64 时的吞吐量是 10000 tokens/s，Latency 模式在 batch_size=1 时的延迟是 10ms。估算 Throughput 模式在 batch_size=1 时的延迟。

### 3.6 答案

1. **实时聊天应用**：应该选择 Latency 模式。因为：
   - Batch size = 1，无法利用 Throughput 模式的批量优势
   - Latency 模式针对单请求优化了内存访问和调度
   - Throughput 模式的复杂调度在小 batch 时反而增加开销

2. **批量摘要任务**：应该选择 Throughput 模式。预期加速比取决于：
   - GPU 利用率：Latency 模式可能只有 10-20% SM 利用率
   - Throughput 模式可以达到 70-90% 利用率
   - 实际加速比通常在 3-5x，取决于模型大小和批次大小

3. **混合场景架构**：
   - 部署两个独立的模型实例
   - 实时请求路由到 Latency 模式实例
   - 批量任务路由到 Throughput 模式实例
   - 或者：动态选择模式，根据请求队列长度和 batch size 自动切换

4. **性能估算**：
   - Throughput: 10000 tokens/s @ batch_size=64
   - 每个 batch 的处理时间：64 tokens / 10000 tokens/s = 6.4ms
   - 假设固定开销为 2ms，则 batch_size=1 时：
     - 总时间 = 固定开销 + 可变时间 ≈ 2ms + (6.4ms - 2ms) / 64 ≈ 2.07ms
   - 但实际 Throughput 模式在 batch_size=1 时会更慢（10-20ms），因为调度开销

---

## 最小模块 4：调度策略对比

### 4.1 概念说明

调度策略决定了如何将指令分配给 GPU 的 Streaming Multiprocessor（SM）。Megakernels 支持多种调度策略：

1. **Round-Robin（rr）**：轮流分配
2. **Zig-Zag（zz）**：来回分配
3. **Wave（wave）**：按指令波形分配
4. **DAG（dag）**：基于依赖图的智能调度
5. **Pool（pool）**：按内存/计算池分配

调度策略对比的目的是：
- 找到最优的负载均衡策略
- 理解不同策略的适用场景
- 验证调度的正确性

### 4.2 伪代码或流程

```python
def compare_scheduling_strategies(config):
    strategies = ["rr", "zz", "wave", "dag", "pool"]
    results = {}
    
    for strategy in strategies:
        # 分配指令到 SM
        assigned_queues = assign_to_sms(
            mode=strategy,
            schedule=schedule,
        )
        
        # 分析负载均衡
        queue_lengths = [len(q) for q in assigned_queues]
        load_imbalance = max(queue_lengths) - min(queue_lengths)
        
        # 分析计算成本
        costs = [sum(ins.cost(globs) for ins in queue) 
                 for queue in assigned_queues]
        cost_imbalance = max(costs) - min(costs)
        
        # 运行性能测试
        time = run_test(assigned_queues)
        
        results[strategy] = {
            "load_imbalance": load_imbalance,
            "cost_imbalance": cost_imbalance,
            "time": time,
        }
    
    return results
```

### 4.3 原理分析

#### 策略详解

**Round-Robin（rr）**：
```python
for i, instruction in enumerate(instructions):
    sm_queues[i % sm_count].append(instruction)
```
- 简单、可预测
- 但不考虑指令成本差异

**Zig-Zag（zz）**：
```python
for i, instruction in enumerate(instructions):
    base_id = i % (sm_count * 2)
    if base_id < sm_count:
        sm_queues[base_id].append(instruction)
    else:
        sm_queues[sm_count - 1 - (base_id - sm_count)].append(instruction)
```
- 试图改善内存访问局部性
- 适用于某些特定访问模式

**Wave（wave）**：
```python
waves = collect_into_waves(instructions)  # 按操作码分组
for wave in waves:
    sorted_by_cost = sorted(wave, key=cost, reverse=True)
    # 贪心分配到最空闲的 SM
    for ins in sorted_by_cost:
        sm_idx = select_sm_with_min_cost()
        sm_queues[sm_idx].append(ins)
```
- 考虑指令成本，贪心负载均衡
- 适用于成本差异大的场景

**DAG（dag）**：
```python
# 基于依赖关系的调度
for node in ready_nodes:
    sm_idx = select_sm_with_min_cost()
    assign(node, sm_idx)
    # 更新依赖关系，释放新的就绪节点
```
- 考虑指令依赖关系
- 理论上最优，但计算开销大

#### 性能指标

**负载均衡度**：
\[
\text{Imbalance} = \frac{\max(\text{costs}) - \min(\text{costs})}{\text{mean}(\text{costs})}
\]

理想情况下，Imbalance 应该接近 0。

### 4.4 代码实践

在 `diff_test.py` 中，调度策略通过 `sched` 参数选择：

**策略分配**：
```python
assigned_to_sms = assign_to_sms(
    mode=config.sched,  # "rr", "zz", "wave", "dag", "pool"
    instructions=instructions,
    sm_count=spy.globs.sm_count(),
)
```

这段代码[在 147-151 行](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/diff_test.py#L147-L151)，展示了策略选择。

**负载分析**：
```python
# 分析队列长度
queue_lengths = [len(q) for q in assigned_to_sms]
print(f"sm queue lengths: min={min(queue_lengths)}, "
      f"max={max(queue_lengths)}, "
      f"mean={sum(queue_lengths) / len(queue_lengths)}")

# 分析计算成本
if not config.skip_cost:
    cost_per_sm = []
    for sm_queue in assigned_to_sms:
        cost = 0
        for instruction in sm_queue:
            cost += instruction.cost(gpy)
        cost_per_sm.append(cost)
    
    cost_tensor = torch.tensor(cost_per_sm)
    relative_cost_tensor = cost_tensor / cost_tensor.max()
    
    print(f"cost per sm: min={relative_cost_tensor.min():.2f}, "
          f"mean={relative_cost_tensor.mean():.2f}")
```

这段代码[在 165-186 行](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/diff_test.py#L165-L186)，展示了负载分析。

**策略实现**（在 `scheduler.py` 中）：

**Round-Robin**：
[round_robin_assign_to_sms](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L154-L161) 实现

**Wave Scheduling**：
[wave_assign_to_sms](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L194-L217) 实现

**DAG Scheduling**：
[assign_dag_to_sms](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L94-L151) 实现

### 4.5 练习题

1. **策略选择**：如果指令成本差异很大（有些指令成本 1000，有些成本 10），应该选择哪种策略？

2. **依赖关系**：如果指令间有复杂的依赖关系（DAG），应该选择哪种策略？为什么？

3. **负载不均衡**：如果 Round-Robin 的 `max(queue_lengths)` 是 100，`min` 是 20，说明什么？如何改进？

4. **成本分析**：Wave 调度为什么先按操作码分组（`collect_into_waves`），然后按成本排序？

### 4.6 答案

1. **指令成本差异大**：应该选择 Wave 或 DAG 策略。
   - Wave：贪心策略，每次分配最昂贵的指令到最空闲的 SM
   - DAG：考虑依赖关系，更精确但开销更大
   - Round-Robin 会导致负载严重不均衡

2. **复杂依赖关系**：必须选择 DAG 策略。
   - 其他策略不考虑依赖，可能导致违反依赖关系
   - DAG 策略基于拓扑排序，确保依赖满足后才执行
   - 虽然调度开销大，但正确性更重要

3. **负载不均衡分析**：
   - Imbalance = (100 - 20) / mean ≈ 80 / 60 ≈ 133%
   - 说明有些 SM 忙碌，有些空闲，浪费计算资源
   - 改进方法：使用 Wave 或 DAG 策略，考虑指令成本

4. **Wave 分组的原因**：
   - 按操作码分组：相同操作的指令可以融合或优化执行
   - 按成本排序：贪心地先处理大指令，减少碎片化
   - 这样既保证了执行效率（融合），又保证了负载均衡（贪心）

---

## 总结

本讲义介绍了 Megakernels 中的性能测试和基准对比框架：

1. **性能基准测试**：通过基线减法测量吞吐量，排除固定开销干扰
2. **差异测试**：通过比较 PyVM 和 MK 实现验证正确性
3. **模式对比**：在不同场景下选择 Latency 或 Throughput 模式
4. **调度策略对比**：分析和选择最优的指令分配策略

这些测试工具构成了 Megakernels 开发的基础设施，帮助我们：
- 验证优化的有效性
- 确保实现的正确性
- 理解性能瓶颈
- 做出架构决策

在实际开发中，建议：
1. 先运行差异测试确保正确性
2. 再运行基准测试测量性能
3. 对比不同模式和策略找到最优配置
4. 建立性能基线，防止回归

通过系统的测试和分析，我们才能构建出既正确又高性能的 Megakernels 系统。
