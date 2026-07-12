# 配置体系：Config 与 SamplingParams

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `Config` 数据类里每个字段的含义、默认值，以及它最终被哪个模块消费。
- 区分「引擎级参数」和「请求级参数」：前者由 `Config` 管，影响整条推理流水线；后者由 `SamplingParams` 管，影响单条请求的生成行为。
- 理解 `Config` 里两个看起来「没有值」的字段（`eos`、`num_kvcache_blocks`）为什么默认是 `-1`，以及它们在初始化过程中被谁「补写」上真实值。
- 掌握 `Config.__post_init__` 中的校验与裁剪逻辑，尤其是 `max_model_len` 如何被 HuggingFace 配置自动封顶。
- 看懂 `SamplingParams` 的三个旋钮（`temperature` / `max_tokens` / `ignore_eos`），并理解为什么 nano-vllm 明确禁止 greedy 采样。
- 通过修改参数、用 `bench.py` 对比，亲身感受配置对吞吐和显存的影响。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 引擎级 vs 请求级参数

一个 LLM 推理引擎有两类「旋钮」：

- **引擎级参数**：在引擎构造时就定下来、整个生命周期不变。比如「最多同时跑多少条序列」「显存用百分之多少」「要不要开 CUDA Graph」。这类参数在 nano-vllm 里就是 `Config`。它们决定了引擎的**容量上限**。
- **请求级参数**：每来一条请求可以单独指定。比如「这条请求温度设多少」「最多生成多少 token」。这类参数在 nano-vllm 里就是 `SamplingParams`，它会被装进 `Sequence` 里随请求流转。

用一个比喻：`Config` 是「这辆大巴有多少座位、油箱多大」，`SamplingParams` 是「每位乘客要去哪、坐几站」。

### 2.2 dataclass 与 `__post_init__`

Python 的 `@dataclass` 装饰器能自动生成 `__init__`，让你用 `Config(model, max_num_seqs=64)` 这样的关键字参数构造对象。被 `slots=True` 修饰后，对象用更紧凑的内存布局，且不能再动态添加未声明的属性（拼错字段名会直接报错，这对配置类是好事）。

`@dataclass` 还提供一个钩子 `__post_init__`：在自动生成的 `__init__` 跑完之后立刻调用，常用于「构造完做一次校验或补算」。nano-vllm 正是用它来做断言校验和 `max_model_len` 裁剪。

### 2.3 采样里的「温度」是什么

模型每一步会给词表里每个 token 打一个分数（logits）。采样就是把这些分数变成「下一个 token 是谁」的概率。温度 \(T\) 作用在 logits 上：

\[ p_i = \frac{\exp(z_i / T)}{\sum_j \exp(z_j / T)} \]

- \(T\) 越小，最大分数的 token 概率越接近 1，输出越「确定」（趋近 greedy）。
- \(T\) 越大，分布越平均，输出越「随机」。
- \(T = 0\) 时退化成纯贪心取 argmax，但 nano-vllm 的采样器走的是另一条数学路线，**不允许 \(T = 0\)**，本讲 4.2 节会展开。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [nanovllm/config.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/config.py) | 引擎级配置数据类 `Config` | 全部字段、默认值、`__post_init__` 校验与裁剪 |
| [nanovllm/sampling_params.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/sampling_params.py) | 请求级采样参数数据类 `SamplingParams` | 三字段、禁止 greedy 的断言 |
| [nanovllm/engine/llm_engine.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py) | 引擎主类 `LLMEngine` | `**kwargs` 如何被过滤成 `Config`、`eos` 如何被补写 |
| [nanovllm/engine/model_runner.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py) | 执行器 `ModelRunner` | `gpu_memory_utilization` 如何算出 `num_kvcache_blocks`、`enforce_eager` 何时生效 |
| [nanovllm/engine/scheduler.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py) | 调度器 `Scheduler` | `max_num_seqs` / `max_num_batched_tokens` 如何约束调度 |
| [nanovllm/engine/sequence.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py) | 请求对象 `Sequence` | `SamplingParams` 如何被拆解进 `Sequence` |
| [bench.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/bench.py) | 基准测试脚本 | 实践任务中用来对比配置效果的入口 |

---

## 4. 核心概念与源码讲解

### 4.1 Config：引擎级配置数据类

#### 4.1.1 概念说明

`Config` 是 nano-vllm 全局唯一的「引擎参数容器」。它是一个被 `@dataclass(slots=True)` 修饰的普通 Python 类，没有方法，只有字段和一个 `__post_init__` 钩子。它的存在回答了一个问题：**「这个引擎开多大、怎么跑」**。

理解 `Config` 的关键不是记住每个默认值，而是建立一张「字段 → 消费者」的映射表：每个字段最终会被引擎里的哪个模块读走。因为 nano-vllm 代码极简，几乎每个字段都只在一个地方被消费，追踪起来非常干净。

还有一个反直觉的设计值得注意：`Config` 里有两个字段（`eos`、`num_kvcache_blocks`）默认值是 `-1`，看起来「没意义」。它们是**占位符**——真实值要在引擎初始化的特定阶段才能确定，所以先填 `-1`，等条件具备时再由对应模块**就地改写** `Config` 实例。这是 nano-vllm 把 `Config` 当作「可变的、全引擎共享的状态对象」来用的体现。

#### 4.1.2 核心流程

`Config` 的生命周期分三步：

1. **构造与校验**：`LLMEngine.__init__` 用过滤后的关键字参数构造 `Config`，触发 `__post_init__` 做断言校验、加载 `hf_config`、裁剪 `max_model_len`。
2. **占位字段补写**：
   - `num_kvcache_blocks` 在 `ModelRunner.allocate_kv_cache` 里根据剩余显存算出来后写回 `Config`（因为算它需要先把模型权重加载到 GPU、跑一次 warmup，这步只能在 `ModelRunner.__init__` 里完成）。
   - `eos` 在 `AutoTokenizer` 加载完成后，由 `LLMEngine` 把 `tokenizer.eos_token_id` 写回 `Config`。
3. **下游消费**：`Scheduler` 在自己的 `__init__` 里一次性读走 `max_num_seqs`、`max_num_batched_tokens`、`eos`、`num_kvcache_blocks`、`kvcache_block_size`；`ModelRunner` 读走 `enforce_eager`、`tensor_parallel_size`、`gpu_memory_utilization` 等。

用伪代码描述补写时序（注意 `ModelRunner` 在 `Scheduler` **之前**构造）：

```
LLMEngine.__init__:
    config = Config(model, **过滤后的kwargs)   # num_kvcache_blocks=-1, eos=-1
    Sequence.block_size = config.kvcache_block_size
    model_runner = ModelRunner(config, ...)     # 内部 allocate_kv_cache 写回 num_kvcache_blocks
    tokenizer = AutoTokenizer.from_pretrained(...)
    config.eos = tokenizer.eos_token_id         # 写回 eos
    scheduler = Scheduler(config)               # 此时 config 两个字段已是真实值
```

#### 4.1.3 源码精读

先看 `Config` 的全部字段定义：

[ nanovllm/config.py:6-18](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/config.py#L6-L18) —— 用 `@dataclass(slots=True)` 声明引擎配置，9 个字段全部带默认值。下表逐一说明含义、默认值和消费者：

| 字段 | 默认值 | 含义 | 谁消费它 |
|---|---|---|---|
| `model` | （必填，无默认） | HuggingFace 模型的本地目录路径 | `AutoConfig` / `AutoTokenizer` / `load_model` |
| `max_num_batched_tokens` | `16384` | 单次 prefill 最多打包多少 token | `Scheduler`（约束 prefill 批量）、`ModelRunner.warmup_model` |
| `max_num_seqs` | `512` | 单步最多同时处理多少条序列 | `Scheduler`（prefill/decode 上限）、`ModelRunner.capture_cudagraph`（CUDA Graph 最大档位） |
| `max_model_len` | `4096` | 单条序列的最大长度上限 | 会在 `__post_init__` 被 HF 的 `max_position_embeddings` 取 `min` 裁剪；`ModelRunner` 用于 warmup 与 graph 档位估算 |
| `gpu_memory_utilization` | `0.9` | KV cache 显存预算占比 | `ModelRunner.allocate_kv_cache` |
| `tensor_parallel_size` | `1` | 张量并行数（GPU 数） | `LLMEngine`（拉起几个 worker 进程）、`ModelRunner`（NCCL world_size） |
| `enforce_eager` | `False` | 是否强制走 eager 模式、禁用 CUDA Graph | `ModelRunner`（是否 `capture_cudagraph`、`run_model` 走图还是 eager） |
| `hf_config` | `None` | HF 模型配置对象（构造后自动填充） | `__post_init__` 加载，`ModelRunner`/`models` 大量读取 |
| `eos` | `-1` | 结束符 token id（占位，后补写） | `LLMEngine` 写入；`Scheduler` 读取用于判停 |
| `kvcache_block_size` | `256` | KV cache 单块的 token 数（必须 256 的倍数） | `Scheduler` / `Sequence.block_size` / `ModelRunner` |
| `num_kvcache_blocks` | `-1` | KV cache 块数（占位，后补写） | `ModelRunner.allocate_kv_cache` 写入；`Scheduler` → `BlockManager` 读取 |

> 说明：表格里把 `model` 也算进来共 11 项，与源码 9 个带默认值的字段加 1 个必填 `model` 一致；`hf_config` / `eos` / `num_kvcache_blocks` 是「运行时被改写」的字段。

再看 `__post_init__` 的校验与裁剪：

[ nanovllm/config.py:20-25](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/config.py#L20-L25) 做了四件事：

1. **`assert os.path.isdir(self.model)`**：构造时就校验模型路径必须是已存在的目录，避免拖到后面才报错。
2. **`assert self.kvcache_block_size % 256 == 0`**：块大小必须是 256 的整数倍（与 Triton 内核的访存粒度、flash-attn 的对齐要求相关）。
3. **`assert 1 <= self.tensor_parallel_size <= 8`**：张量并行数被限制在 1~8。
4. **`self.hf_config = AutoConfig.from_pretrained(self.model)`** + **`self.max_model_len = min(...)`**：自动加载 HF 配置；并用模型的 `max_position_embeddings` 给用户传入的 `max_model_len` 封顶——即使你传 `max_model_len=8192`，模型只支持 4096，最终也是 4096。这是一种「用户值不能超过模型能力」的保护。

接着看 `LLMEngine` 如何把 `**kwargs` 过滤成合法的 `Config` 字段，这是上一讲提到的「拼错字段会被静默忽略」的根源：

[ nanovllm/engine/llm_engine.py:17-20](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L17-L20) —— 用 `dataclasses.fields(Config)` 拿到所有合法字段名集合，再只保留 `kwargs` 里属于该集合的键。**任何拼错的参数（比如 `max_num_seq` 少了个 s）会被静默丢弃**，不会报错也不会生效，这是 nano-vllm 一个容易踩的坑。

[ nanovllm/engine/llm_engine.py:33](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L33) —— tokenizer 加载完成后，把 `eos_token_id` 写回 `config.eos`，完成「占位字段」的第一次补写。

然后看 `num_kvcache_blocks` 如何被算出来并写回——这是「占位字段」的第二次补写，也是 `gpu_memory_utilization` 真正发挥作用的地方：

[ nanovllm/engine/model_runner.py:103-121](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L103-L121) `allocate_kv_cache` 的核心是两行：

```python
block_bytes = 2 * num_hidden_layers * block_size * num_kv_heads * head_dim * dtype.itemsize
config.num_kvcache_blocks = int(total * config.gpu_memory_utilization - used - peak + current) // block_bytes
```

- `block_bytes` 是**一个 KV 块占多少字节**：`2`（K 和 V 两份）× 层数 × 块大小（token 数）× KV 头数 × 每头维度 × 每元素字节数（如 bfloat16 是 2）。
- 第二行是**KV cache 的显存预算公式**：
  - `total * gpu_memory_utilization`：允许 PyTorch 最多占用的显存上限。
  - `used`：当前 GPU 已被占用的字节（含其它进程和当前 PyTorch 常驻分配）。
  - `peak`：warmup 期间峰值分配字节（模型权重 + 最大前向激活）。
  - `current`：当前常驻分配字节（warmup 后 `empty_cache` 过，基本是模型权重）。
  - 于是可用预算 = 目标上限 − 已用 − 需要为前向激活预留的余量 \( (\text{peak} - \text{current}) \)，即：

\[ \text{budget} = \text{total}\cdot\text{util} - \text{used} - (\text{peak} - \text{current}) \]

再整除单个块字节数得到块数。这里有个**隐含耦合**值得记住：warmup 用的是 `max_num_batched_tokens` 决定的最大前向规模（见下一处引用），所以 `max_num_batched_tokens` 越大 → warmup 激活峰值越高 → 留给 KV cache 的显存越少 → 块数越少。**调大批量会吃掉 KV cache 容量**，二者是权衡关系。

[ nanovllm/engine/model_runner.py:91-101](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L91-L101) `warmup_model` 用 `max_num_batched_tokens` 与 `max_model_len` 估算「最坏情况」下的前向规模并空跑一次，目的就是**撑出 `peak` 内存水位**，好让随后的 `allocate_kv_cache` 不会把激活显存也算进可分配预算、导致后续 OOM。

最后看几个字段被 `Scheduler` 消费的位置，体会「字段 → 消费者」的干净映射：

[ nanovllm/engine/scheduler.py:10-17](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L10-L17) —— `Scheduler.__init__` 把 `max_num_seqs`、`max_num_batched_tokens`、`eos`、`num_kvcache_blocks`、`kvcache_block_size` 一次性「抄」进自己的属性和 `BlockManager`。

这两个字段在调度主循环里真正起约束作用：

[ nanovllm/engine/scheduler.py:30](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L30) —— `max_num_seqs` 限制单步序列数。

[ nanovllm/engine/scheduler.py:32](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L32) —— `max_num_batched_tokens` 限制单步 prefill 总 token 数。

`enforce_eager` 和 `tensor_parallel_size` 被 `ModelRunner` 消费：

[ nanovllm/engine/model_runner.py:21-22](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L21-L22) —— 记录 `enforce_eager` 与 `world_size`（= `tensor_parallel_size`）。

[ nanovllm/engine/model_runner.py:197](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L197) —— `run_model` 里只要 `is_prefill`、或开了 `enforce_eager`、或批量超 512，就走 eager；否则走 CUDA Graph 回放（CUDA Graph 细节留待第 5 单元）。

#### 4.1.4 代码实践

> 这是一个**源码阅读 + 本地运行结合**的实践。运行部分需要 GPU 与 Qwen3-0.6B 权重，若本地无环境可只做阅读部分。

**实践目标**：亲眼看到 `Config` 的占位字段被补写、并感受 `gpu_memory_utilization` 对块数的影响。

**操作步骤**：

1. 阅读上面引用的 `llm_engine.py:17-35`，确认补写顺序：先 `ModelRunner`（写 `num_kvcache_blocks`），后 `config.eos`，最后 `Scheduler` 读两者。
2. 在 `nanovllm/engine/model_runner.py` 的 `allocate_kv_cache` 里，在 `config.num_kvcache_blocks = ...` 这一行**之后**临时加一行日志（这是你本地临时改动，验证完即可还原，不要提交）：

   ```python
   print(f"[debug] total={total} used={used} peak={peak} current={current} "
         f"block_bytes={block_bytes} num_kvcache_blocks={config.num_kvcache_blocks}")
   ```

3. 运行 `example.py`（参考第 1 讲的方式），观察日志输出。

**需要观察的现象**：

- `num_kvcache_blocks` 由 `-1` 变成了一个正整数（不再是默认占位值）。
- `block_bytes` 与你按公式手算的值一致。

**预期结果**：

- 对于 Qwen3-0.6B（bfloat16、28 层、KV 头数 4、head_dim 128、block_size 256），`block_bytes` 理论值为 \( 2 \times 28 \times 256 \times 4 \times 128 \times 2 = 29\_360\_128 \) 字节（约 28 MiB / 块）。你的手算结果应与此一致；`num_kvcache_blocks` 则取决于你 GPU 的剩余显存，**待本地验证**。

> 注意：上面 28 层/KV 头数等具体数值来自 Qwen3-0.6B 的 `hf_config`，若你用的是别的模型请以实际 `hf_config` 为准。

#### 4.1.5 小练习与答案

**练习 1**：用户写 `LLM(path, max_num_seq=64)`（少了一个 `s`），会发生什么？引擎的并发上限会变成 64 吗？

**参考答案**：不会。`max_num_seq` 不是合法的 `Config` 字段，会被 `llm_engine.py:19` 的过滤逻辑静默丢弃，`max_num_seqs` 保持默认值 `512`，且不会报错。这是 nano-vllm 的一个坑：配置拼错不会被发现。

**练习 2**：用户传 `max_model_len=8192`，但模型的 `max_position_embeddings=4096`，最终 `config.max_model_len` 是多少？为什么？

**参考答案**：是 `4096`。因为 `config.py:25` 用了 `min(self.max_model_len, self.hf_config.max_position_embeddings)`，用户值不能超过模型本身支持的最大上下文长度，这是一种保护性裁剪。

**练习 3**：为什么 `num_kvcache_blocks` 不能在 `Config.__post_init__` 里就算好，而要拖到 `ModelRunner.allocate_kv_cache`？

**参考答案**：因为它依赖「把模型权重加载到 GPU 后」的实时显存水位（`free`/`peak`/`current`）。在 `__post_init__` 阶段还没初始化 CUDA、没加载权重，无从得知剩余显存，所以只能先占位 `-1`，等 `ModelRunner` 跑完 warmup 再回填。

---

### 4.2 SamplingParams：请求级采样参数

#### 4.2.1 概念说明

`SamplingParams` 是 nano-vllm 对外导出的两个名字之一（另一个是 `LLM`）。它极其精简，只有三个字段：`temperature`、`max_tokens`、`ignore_eos`。它回答的问题是：**「这一条请求怎么采样、生成多长、何时停」**。

和 `Config` 的「引擎级、构造时定死」不同，`SamplingParams` 是「请求级」的——每次 `llm.generate(prompts, sampling_params)` 都可以传新的，甚至可以传一个列表，让每条 prompt 用不同的采样参数。它最终会被 `Sequence` 拆解成三个普通属性，随请求一起进入调度和采样流程。

nano-vllm 的采样器有一个鲜明的取舍：**它走的是「指数分布」采样路线，而不是直接做 multinomial 采样，因此禁止 `temperature=0`（greedy）**。这是理解 `SamplingParams` 最重要的一点。

#### 4.2.2 核心流程

`SamplingParams` 的数据流很短：

1. 用户构造 `SamplingParams(temperature=0.6, max_tokens=256)`，触发 `__post_init__` 断言 `temperature > 1e-10`。
2. 传入 `llm.generate`，在 `add_request` 里连同 prompt 一起装进 `Sequence`。
3. `Sequence.__init__` 把三个字段拷贝为自己的属性 `self.temperature` / `self.max_tokens` / `self.ignore_eos`，之后 `SamplingParams` 对象本身就不再被引用了。
4. 调度/前向阶段，`ModelRunner.prepare_sample` 收集所有序列的 `temperature` 拼成张量；`Sampler` 用它对 logits 做缩放并采样。
5. `Scheduler.postprocess` 里用 `ignore_eos` 和 `max_tokens` 判定序列是否结束。

采样的数学原理：`Sampler` 对 logits 除以温度后做 softmax 得到概率 \(p_i\)，然后用一个技巧采样——为每个 token 独立采样一个 \(\text{Exp}(1)\) 噪声 \(e_i\)，取 \(\arg\max_i (p_i / e_i)\)。可以证明这等价于按 \(\{p_i\}\) 做类别采样（这是 Gumbel-max 类技巧的指数分布变体）。正因为分母是随机噪声、且要 `div_` 温度，温度为 0 会让除法非法、也让该技巧退化，所以代码直接用断言挡掉 greedy。

#### 4.2.3 源码精读

先看 `SamplingParams` 全貌：

[ nanovllm/sampling_params.py:4-11](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/sampling_params.py#L4-L11) —— 同样是 `@dataclass(slots=True)`，三个字段：

| 字段 | 默认值 | 含义 |
|---|---|---|
| `temperature` | `1.0` | 采样温度，必须严格大于 0 |
| `max_tokens` | `64` | 这条请求最多生成多少个 token |
| `ignore_eos` | `False` | 是否忽略结束符（用于压测：强制生成到 `max_tokens`） |

[ nanovllm/sampling_params.py:11](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/sampling_params.py#L11) —— 这一行断言是关键：`assert self.temperature > 1e-10, "greedy sampling is not permitted"`。**nano-vllm 明确不支持 greedy 采样**，温度必须非零。原因要结合采样器实现看（见下）。

接着看 `SamplingParams` 如何被 `Sequence` 拆解：

[ nanovllm/engine/sequence.py:18-31](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py#L18-L31) —— `Sequence.__init__` 接收一个 `sampling_params`（默认是一个新建的 `SamplingParams()`），把它的三个字段拷贝为 `self.temperature` / `self.max_tokens` / `self.ignore_eos`。注意 `Sequence` 还有一个有意思的细节：构造时除了 token，唯一带的「外部状态」就是这个采样参数。

然后看 `temperature` 如何被收集并喂给采样器：

[ nanovllm/engine/model_runner.py:190-193](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L190-L193) `prepare_sample` 把本步所有序列的 `temperature` 拼成一个一维张量。这就是为什么不同请求可以用不同温度——它们被原样保留、逐序列作用。

[ nanovllm/layers/sampler.py:7-12](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/sampler.py#L7-L12) —— 采样器本体（带 `@torch.compile` 融合）：

```python
logits = logits.float().div_(temperatures.unsqueeze(dim=1))      # logits / T
probs = torch.softmax(logits, dim=-1)                            # softmax 得到 p_i
sample_tokens = probs.div_(torch.empty_like(probs).exponential_(1).clamp_min_(1e-10)).argmax(dim=-1)
```

- 第一行：logits 除以温度。这里 `div_` 是原地除法，温度越小、logits 被放大越多、分布越尖锐。
- 第二行：softmax 得到概率 \(p_i\)。
- 第三行：生成与 probs 同形状的 \(\text{Exp}(1)\) 噪声，`clamp_min_` 防止除零，然后 `probs / noise` 取 argmax。如 4.2.2 所述，这等价于按 \(\{p_i\}\) 类别采样。

现在能回答「为什么禁止 greedy」：如果 `temperature=0`，第一行 `logits / 0` 会得到 inf/nan，整个采样崩溃；即便规避除零，该指数分布技巧在 \(T \to 0\) 时也失去意义。所以 nano-vllm 干脆在 `SamplingParams` 构造时就用断言挡住，要求用户必须给一个正温度。**若你想要「接近确定性」的输出，请用一个很小的正温度（例如 `1e-5`）而不是 0。**

最后看 `max_tokens` 与 `ignore_eos` 如何决定序列终止：

[ nanovllm/engine/scheduler.py:89](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L89) —— 判停条件：`(not ignore_eos and token == eos) or num_completion_tokens == max_tokens`。即「没设 ignore_eos 且采到结束符」或「生成数达到 `max_tokens`」就置为 `FINISHED`。`ignore_eos=True` 时只能靠 `max_tokens` 停下，这正是 `bench.py` 压测时的用法。

#### 4.2.4 代码实践

**实践目标**：直观感受 `temperature` 对生成随机性的影响，并验证 greedy 采样确实被禁。

**操作步骤**：

1. 准备 `example.py` 的一个副本（例如临时改 `example.py`，验证后还原）。
2. 用同一个 prompt、固定随机性无关的设定，分别用三组采样参数各跑一次（`max_tokens` 设大一点便于观察，如 64）：

   ```python
   SamplingParams(temperature=1e-5, max_tokens=64)   # 近乎确定性
   SamplingParams(temperature=0.6,  max_tokens=64)   # 常规
   SamplingParams(temperature=1.5,  max_tokens=64)   # 更随机
   ```

3. 再尝试构造 `SamplingParams(temperature=0)`，观察是否如预期报错。

**需要观察的现象**：

- `temperature=1e-5` 时多次运行输出高度一致（近乎贪心）。
- `temperature` 越大，输出越发散、越不可预期。
- `temperature=0` 直接抛出 `AssertionError: greedy sampling is not permitted`。

**预期结果**：上述现象成立。**待本地验证**（nano-vllm 未固定采样随机种子，`temperature=1e-5` 下由于指数噪声仍可能有极小概率差异，但总体应高度稳定）。

> 注意：因为采样器引入了指数分布噪声，即使温度极小也不保证 100% 逐 token 确定；这是 nano-vllm 的设计取舍，与「严格 greedy」不同。

#### 4.2.5 小练习与答案

**练习 1**：`bench.py` 里为什么给每条请求都设 `ignore_eos=True`？

**参考答案**：压测要的是「每条请求都生成满 `max_tokens` 个 token」，这样才能用 `sum(sp.max_tokens)` 精确计算总产出 token 数、得出可信的吞吐。如果让 eos 提前终止，不同请求产出长度不一，吞吐就无法稳定比较。见 [bench.py:18](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/bench.py#L18) 与 [bench.py:26](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/bench.py#L26)。

**练习 2**：如果你想要「每次运行结果都一样」的确定性输出，nano-vllm 能做到吗？

**参考答案**：不能严格做到。即便把 `temperature` 设成极小正值（如 `1e-5`）趋近贪心，采样器 [sampler.py:11](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/sampler.py#L11) 仍会引入 \(\text{Exp}(1)\) 随机噪声，且代码没有暴露固定随机种子的接口。需要确定性输出的场景不适合 nano-vllm 当前实现。

**练习 3**：`max_tokens` 限制的是「prompt + 生成」的总长，还是仅「生成」部分？

**参考答案**：仅「生成」部分。`Sequence` 用 `num_completion_tokens`（= `num_tokens - num_prompt_tokens`）与 `max_tokens` 比较，见 [sequence.py:43-45](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py#L43-L45) 与 [scheduler.py:89](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L89)。prompt 长度由另一套机制（`max_model_len`）约束。

---

## 5. 综合实践：用 bench.py 对比不同配置的吞吐与显存

本任务把 `Config` 与 `SamplingParams` 串起来，量化感受配置旋钮的影响。**需要 GPU 环境与 Qwen3-0.6B 权重；若本地无环境，可只完成阅读与预测部分，并标注「待本地验证」。**

**背景**：[bench.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/bench.py) 跑 256 条随机 token 序列（输入 100~1024、输出 100~1024），用 `ignore_eos=True` 保证每条都生成满，最后打印总 token 数、耗时、吞吐。注意它的默认配置：

[ bench.py:15](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/bench.py#L15) —— `LLM(path, enforce_eager=False, max_model_len=4096)`，即默认**开启** CUDA Graph。

**操作步骤**：

1. **基线**：原样跑 `python bench.py`，记录 `Throughput`。同时用 `nvidia-smi` 在运行期间观察峰值显存。
2. **对比 enforce_eager**：把第 15 行改成 `enforce_eager=True` 再跑。结合 [model_runner.py:197](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L197) 预测：decode 阶段不再走 CUDA Graph 回放，单步 kernel launch 开销增大，吞吐应**下降**。
3. **对比 max_num_batched_tokens**：把第 15 行改成 `LLM(path, enforce_eager=False, max_model_len=4096, max_num_batched_tokens=2048)`（默认是 16384）再跑。结合本讲 4.1.3 的耦合分析预测：每步 prefill 打包更少 token → prefill 步数变多；但同时 warmup 激活峰值变小 → 留给 KV cache 的显存变多。综合吞吐可能**略降或持平**，显存占用可能**下降**。
4. **（可选）对比 tensor_parallel_size**：若有 2 张 GPU，改成 `tensor_parallel_size=2` 跑一次，观察吞吐与单卡显存的变化。注意 [llm_engine.py:25-31](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L25-L31) 会 spawn 额外 worker 进程。
5. 把每组结果填入下表（示例为待填）：

| 配置 | Throughput (tok/s) | 峰值显存 (GB) | 现象与解释 |
|---|---|---|---|
| 基线 `enforce_eager=False` | 待本地验证 | 待本地验证 | — |
| `enforce_eager=True` | 待本地验证 | 待本地验证 | decode 不走 Graph，吞吐下降 |
| `max_num_batched_tokens=2048` | 待本地验证 | 待本地验证 | prefill 步数增多，但 KV cache 余量增大 |

**需要观察的现象**：`enforce_eager` 与 `max_num_batched_tokens` 对吞吐的相反方向影响，以及配置对显存的挤压。

**预期结果**：开 CUDA Graph 比关快；`max_num_batched_tokens` 存在「吞吐 vs 显存」的权衡。具体数值**待本地验证**。

> 重要：第 2、3、4 步会修改 `bench.py`，请仅在本地临时改动并验证后还原，**不要提交这些改动**，也不要修改 `nanovllm/` 源码。

---

## 6. 本讲小结

- nano-vllm 的配置分两层：**引擎级** `Config`（构造时定死，影响整条流水线）与**请求级** `SamplingParams`（每条请求可不同，影响单条生成）。
- `Config` 是 `@dataclass(slots=True)`，`__post_init__` 做三件事：校验模型路径与 `kvcache_block_size`/`tensor_parallel_size`、自动加载 `hf_config`、用 `max_position_embeddings` 给 `max_model_len` 封顶。
- `eos` 与 `num_kvcache_blocks` 默认 `-1` 是**占位符**：前者在 tokenizer 加载后由 `LLMEngine` 写入，后者在 `ModelRunner.allocate_kv_cache` 里按显存预算公式算出后写回——这依赖 `ModelRunner` 先于 `Scheduler` 构造的顺序。
- KV cache 块数预算公式为 \(\text{budget} = \text{total}\cdot\text{util} - \text{used} - (\text{peak}-\text{current})\)，再整除单块字节数；其中 `peak` 来自 warmup，故 `max_num_batched_tokens` 越大、KV cache 余量越小，二者是权衡。
- `LLMEngine.__init__` 用 `dataclasses.fields` 过滤 `**kwargs`，**拼错的配置项会被静默忽略**，这是常见坑。
- `SamplingParams` 只有 `temperature`/`max_tokens`/`ignore_eos` 三项；采样器走指数分布路线，**禁止 `temperature=0`**，想要近确定性输出需用极小正温度。

## 7. 下一步学习建议

本讲建立了「配置如何流入引擎」的认知。配置里反复出现的 `max_num_seqs`、`max_num_batched_tokens`、`num_kvcache_blocks` 在下一讲将真正发挥作用——建议接着进入**第 2 单元「调度与请求管理」**：

- 先读 `u2-l1 Sequence：请求状态与生命周期`，看 `SamplingParams` 被拆解进 `Sequence` 后如何随 token 计数变化。
- 再读 `u2-l2 Scheduler：prefill 与 decode 调度`，看 `max_num_seqs` 与 `max_num_batched_tokens` 如何在 `schedule` 主循环里约束每一步的批量。

如果你更关心显存侧，也可以直接跳到第 3 单元 `u3-l3 KV Cache 显存预算与分配`，那里会更深入地展开本讲 4.1.3 提到的 `allocate_kv_cache` 公式与 `BlockManager` 的协作。配置字段是理解调度的钥匙，建议两条线并行推进。
