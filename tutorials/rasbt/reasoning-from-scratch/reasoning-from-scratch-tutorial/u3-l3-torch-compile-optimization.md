# u3-l3　torch.compile 加速与推理对比

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `torch.compile` 加速推理的原理：从 eager 逐算子执行到「捕获计算图 → 生成融合内核」，以及为什么**首次调用要付出编译开销**。
2. 使用 [reasoning_from_scratch/ch03.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py) 中 `load_model_and_tokenizer(use_compile=True)` 这条一键「加载 + 编译」路径，并解释它为何要在编译前设置 `torch._dynamo.config.allow_unspec_int_on_nn_module = True`。
3. 读懂并复现 [ch02/03_optimized-LLM/compare_inference.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/03_optimized-LLM/compare_inference.py) 的基准对比方法：同一模型在「原始 vs 编译」两种模式下生成 2048 个 token、连跑三轮计时。
4. 知道 Windows 上使用 `torch.compile` 需要额外配置 C/C++ 工具链，以及「编译失败可以放心跳过」这一官方兜底建议。

本讲是单元三的收官，也是单元十「性能优化专题」（SDPA、批量推理）的第一块垫脚石。

## 2. 前置知识

### 2.1 从上一讲继承的认知

上一讲（u3-l2）我们吃透了四代生成函数：`basic` → `basic_cache` → `stream` → `stream_cache`，知道了：

- **KV 缓存**把自回归生成拆成 prefill（整段预填 prompt）+ decode（每步只前向 1 个新 token）两阶段，前向计算量从平方级降为线性；
- `generate_stats` 端到端报告 tokens/sec，GPU 上必须先 `synchronize` 再读峰值显存；
- 生成函数族共享同一套循环骨架，模型是它们的公共参数。

本讲回答一个新问题：**在不改动模型代码的前提下，还能从哪里再「白捡」一份速度？** 答案是 `torch.compile`。

### 2.2 什么是 eager 模式，什么是 JIT 编译

- **eager 模式（急切执行）**：PyTorch 的默认工作方式。你写的每一行张量运算都被立即执行一次：Python 解释器调度一个算子，框架把它发给 CPU/GPU，算完再回来调度下一个。优点是直观、易调试；缺点是每个算子都有固定的**调度开销**（Python 层 + 框架层 + GPU 上的 kernel launch 开销）。
- **JIT 编译（Just-In-Time，即时编译）**：程序运行过程中，把一段代码当场翻译成更底层的优化代码，之后再执行翻译结果。`torch.compile` 就是 PyTorch 2.0 引入的 JIT 编译入口，它内部有两个关键角色：
  - **TorchDynamo**：拦截 Python 字节码，把模型的 `forward` 追踪成一张**计算图**（记录「哪些张量经过了哪些运算」，而不是逐个执行）；遇到无法翻译的代码会产生 **graph break**（图被打断、退回逐段执行）。
  - **TorchInductor**：拿到计算图后，把它 lowering 成优化过的底层内核——GPU 上生成 Triton kernel，CPU 上生成 C++ 代码，并且做**内核融合**（kernel fusion）：把多个小算子合并成一个内核，省掉中间结果的反复读写和多次启动内核的开销。

用一句话概括直觉：

> eager 模式像是「每切一刀都下楼买一次菜」；`torch.compile` 是「先花时间把整桌菜的菜谱整理好（编译），之后每顿饭都按优化过的流程一次买齐（融合内核执行）」。

### 2.3 为什么 0.6B 的小模型特别受益

Qwen3 0.6B 有 28 层 TransformerBlock，每层包含 RMSNorm、SwiGLU 三个线性层、GQA 里的投影/缩放/掩码/softmax 等几十个小算子。模型越小，单个算子的计算量越小，**调度与启动开销占总耗时的比例就越高**——尤其在 GPU 上，kernel launch 开销可能比 kernel 本身的计算时间还长。Inductor 的融合内核恰好压缩的正是这部分开销，所以对本项目这种「层数多、算子碎、单算子小」的模型收益明显（后文 README 基准表里 H100 上约 3 倍加速）。

### 2.4 两个必须预先知道的行为特征

1. **首次编译开销**：第一次调用被编译的模型时才会触发编译，耗时从几秒到几分钟不等。编译产物有进程内缓存和磁盘缓存（Linux 上默认在 `/tmp/torchinductor_<用户名>/`），**同一进程内同形状的后续调用直接复用**，不再重复编译。
2. **对输入形状敏感**：计算图是按形状特化再泛化的。首次按静态形状编译，形状变化会触发再编译（默认策略下第二次重编译后会把变化的维度泛化成动态维度）。这与生成任务天然相关：
   - **无 KV cache** 的循环里序列每步 +1，形状反复变化；
   - **带 KV cache** 的 decode 阶段每步固定输入 1 个 token，形状稳定，是「编译友好」的路径。

用公式表达加速比与首次耗时：

\[ S = \frac{t_{\text{eager}}}{t_{\text{compiled}}} \]

\[ T_{\text{首次}} = T_{\text{compile}} + T_{\text{exec}} \qquad T_{\text{后续}} = T_{\text{exec}} \]

只有当生成 token 数足够多、\( T_{\text{compile}} \) 被摊薄后，加速比 \( S \) 才有意义——这正是所有基准测试都要「预热 + 多轮计时」的原因。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [reasoning_from_scratch/ch02.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py) | 第 2 章生成函数与设备/统计工具 | `get_device` 的 TF32 与 compile 的版本兼容注释；`generate_stats` 计时口径 |
| [ch02/03_optimized-LLM/compare_inference.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/03_optimized-LLM/compare_inference.py) | 独立基准脚本 | `--compile` 开关、版本守护 + dynamo 配置、三轮计时循环 |
| [ch02/03_optimized-LLM/README.md](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/03_optimized-LLM/README.md) | 优化版使用说明 | 两张基准数据表与结论建议 |
| [ch02/04_torch-compile-windows/README.md](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/04_torch-compile-windows/README.md) | Windows 编译指南 | VS2022 工具链、triton 安装、「可跳过」建议 |
| [reasoning_from_scratch/ch03.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py) | 第 3 章评测模块 | `load_model_and_tokenizer` 的 `use_compile` 分支 |
| [reasoning_from_scratch/qwen3.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py) | 模型本体 | `self.current_pos` 的整数属性突变——重编译问题的根源 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：① `torch.compile` 的接入方式与原理落地；② `compare_inference.py` 的基准方法；③ `ch03.py` 的 `use_compile` 分支与重编译抑制；④ 基准数据解读与 Windows 特殊处理。

### 4.1 模块一：torch.compile 的接入方式

#### 4.1.1 概念说明

`torch.compile(model)` 的使用面做到了极简：传入一个 `nn.Module`，返回一个**编译包装后的模型**。包装对象（`OptimizedModule`）会把属性访问代理回原始模型（保存在 `_orig_mod` 里），所以：

- `model.cfg["n_layers"]` 这类属性访问依然可用（生成函数内部正是这么取层数的）；
- `model.eval()`、`model.reset_kv_cache()` 也能穿透包装正常工作；
- **编译后的模型是原始模型的 drop-in replacement（直接替换品）**——上一讲的 `generate_text_basic_stream_cache` 无需任何修改即可接受编译后的模型。

两个工程要点：

1. **先加载权重、再编译**。`torch.compile` 包装后返回的是新对象，标准做法是在 `load_state_dict` 和 `.to(device)` 之后再 `model = torch.compile(model)`（本项目的两个接入点都是这个顺序）。
2. **编译发生在首次前向时**。`torch.compile(model)` 这一行本身很快，真正耗时的图捕获与内核生成推迟到第一次调用 `model(x)` 时才发生。

#### 4.1.2 核心流程

```text
model = torch.compile(model)      # 仅创建包装，几乎不耗时
        │
首次 model(x) ──► Dynamo 捕获 forward ──► 计算图 ──► Inductor 编译内核
        │                                              （耗时秒级~分钟级）
        └──────────────────► 执行编译产物，返回结果
后续 model(x) ──► 命中进程内/磁盘缓存，直接执行（形状相同则零重编译）
形状变化   ──► 触发守护（guard）检查失败 ──► 再编译（默认策略随后泛化动态维度）
```

结合生成任务的两种路径：

- **带 KV cache**：prefill 一次（形状 = prompt 长度）、之后每步固定形状 \( (b, 1) \)——形状集合极小，编译开销只付 1~2 次；
- **无 KV cache**：第 \( n \) 步形状是 \( (b, L_0+n) \)，每步都在变——依赖动态形状泛化来避免反复重编译。

#### 4.1.3 源码精读

接入点一：基准脚本里的编译块。

[ch02/03_optimized-LLM/compare_inference.py:L106-L113](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/03_optimized-LLM/compare_inference.py#L106-L113)

```python
if args.compile:
    major, minor = map(int, torch.__version__.split(".")[:2])
    if (major, minor) >= (2, 8):
        # This avoids retriggering model recompilations
        # in PyTorch 2.8 and newer
        # if the model contains code like self.pos = self.pos + 1
        torch._dynamo.config.allow_unspec_int_on_nn_module = True
    model = torch.compile(model)
```

这段代码做了两件事：先按 PyTorch 主次版本号（如 2.10 → `(2, 10)`）决定是否打开一个 dynamo 开关（模块三详解），再执行 `model = torch.compile(model)` 把模型替换为编译版。注意赋值语句——`torch.compile` **不原地修改**模型，必须接住返回值。

顺序佐证：权重加载与设备搬运在编译之前完成。

[ch02/03_optimized-LLM/compare_inference.py:L101-L104](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/03_optimized-LLM/compare_inference.py#L101-L104)

```python
model = Qwen3Model(QWEN_CONFIG_06_B)
model.load_state_dict(torch.load(model_path, map_location=device))
model.to(device)
```

先 `load_state_dict`、再 `.to(device)`，然后才是上文的 `torch.compile`——「装弹 → 搬运 → 包装」的固定次序。

接入点零（隐藏关联）：`get_device` 里的 TF32 设置同样考虑了 compile。

[reasoning_from_scratch/ch02.py:L15-L25](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L15-L25)

```python
if enable_tensor_cores:
    major, minor = map(int, torch.__version__.split(".")[:2])
    # PyTorch 2.9 and 2.10 still read the legacy TF32 setting in torch.compile.
    # See https://github.com/pytorch/pytorch/issues/166387
    # and https://github.com/rasbt/reasoning-from-scratch/issues/256
    if (major, minor) >= (2, 11):
        torch.backends.cuda.matmul.fp32_precision = "tf32"
        torch.backends.cudnn.conv.fp32_precision = "tf32"
    else:
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
```

这是 u3-l1 讲过的 TF32 开关，注释里特别说明：**PyTorch 2.9/2.10 的 `torch.compile` 读取的仍是旧版 TF32 设置**，所以代码按版本选用了新旧两套 API。这说明 compile 与全局数值后端设置存在交互——工程上真实存在的兼容性细节，作者用版本分支兜住了。

#### 4.1.4 代码实践

**实践：用玩具配置实测「首次编译开销 vs 后续复用」**

1. 实践目标：亲眼看到「第一次前向很慢、后续明显变快」的编译缓存行为，理解为什么基准测试必须预热。
2. 操作步骤：以下为示例代码（非项目原有文件），保存为 `compile_overhead_demo.py`，在已安装本包的环境中运行：

```python
# 示例代码：compile_overhead_demo.py
import time
import torch
from reasoning_from_scratch.qwen3 import Qwen3Model

# 缩小版配置：只为测编译开销，不追求真实模型行为
cfg = {
    "vocab_size": 32000, "context_length": 128, "emb_dim": 256,
    "n_heads": 4, "n_layers": 2, "hidden_dim": 768, "n_kv_groups": 2,
    "head_dim": 64, "qk_norm": True, "rope_base": 1_000_000.0,
    "dtype": torch.float32,
}
x = torch.randint(0, 32000, (1, 16))  # 固定形状输入

model = Qwen3Model(cfg).eval()

with torch.inference_mode():
    for i in range(3):
        t0 = time.perf_counter()
        model(x)
        print(f"eager  第{i+1}次: {time.perf_counter() - t0:.3f}s")

cmodel = torch.compile(model)
with torch.inference_mode():
    for i in range(3):
        t0 = time.perf_counter()
        cmodel(x)
        print(f"compile第{i+1}次: {time.perf_counter() - t0:.3f}s")
```

3. 需要观察的现象：eager 三次耗时接近；compile 第 1 次显著慢于 eager（编译开销），第 2、3 次明显快于 eager 与第 1 次。
4. 预期结果：CPU 上即可观察该规律；具体秒数随机器差异大，**待本地验证**。
5. 进阶一步：把第二次实验的输入换成长度 32（`torch.randint(0, 32000, (1, 32))`，需更新脚本），再按固定 16 与交替 16/32 各跑几轮，观察形状变化触发的额外再编译耗时。

#### 4.1.5 小练习与答案

**练习 1**：为什么说 `torch.compile` 对「带 KV cache 的生成循环」比对「无 cache 的生成循环」更友好？

**答案**：带 cache 的 decode 阶段每步输入形状固定为 \( (b, 1) \)，计算图只需为 prefill 和 decode 各编译一次即可稳定复用；无 cache 的循环中序列长度每步 +1，输入形状持续变化，会不断触发守护检查失败与再编译（虽然 dynamo 默认会在重编译后泛化动态维度，但过程更脆弱、更依赖版本行为）。

**练习 2**：如果执行了 `torch.compile(model)` 但没有接收返回值（写成裸调用 `torch.compile(model)` 一行），会发生什么？

**答案**：什么也不会加速。`torch.compile` 返回一个新的包装模型而不修改原对象，原变量 `model` 仍是 eager 版本，后续调用照旧逐算子执行。compare_inference.py 中是 `model = torch.compile(model)` 的赋值写法。

**练习 3**：一台机器上首次运行编译脚本用了 90 秒，第二次运行同一脚本（新进程）只用了 30 秒。为什么新进程内还会变快？

**答案**：Inductor 的编译产物除了进程内缓存，还有磁盘缓存（Linux 默认在 `/tmp/torchinductor_<用户名>/`）。新进程虽然不会命中内存缓存，但可以从磁盘缓存复用已生成的内核代码，跳过大部分编译工作。

### 4.2 模块二：compare_inference.py 的基准对比方法

#### 4.2.1 概念说明

「优化了多少」必须用可复现的测量回答。[compare_inference.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/03_optimized-LLM/compare_inference.py) 是作者提供的基准脚本，用**命令行开关**把五个影响速度的变量正交出来：

| 开关 | 控制变量 | 取值效果 |
| --- | --- | --- |
| `--device` | 硬件 | `cpu` / `cuda` / `mps`，缺省时用 `get_device()` 自动探测 |
| `--cache` | KV 缓存 | 开：prefill+decode 两阶段；关：整段重算 |
| `--compile` | torch.compile | 开：Inductor 编译；关：eager |
| `--optimize` | 模型实现 | 开：`qwen3_optimized`（附录 C / 单元十）；关：`qwen3` |
| `--reasoning` | 权重变体 | 开：reasoning 权重（约生成 2000 token）；关：base 权重（约 50 token） |

这种「每个维度一个开关」的设计让一张命令行就能唯一确定一种配置，例如 `--device cuda --cache --compile` 就是 README 表中「KV cache compiled」一行。

#### 4.2.2 核心流程

```text
解析参数
  ├─ 选模型实现（qwen3.py 或 qwen3_optimized.py 的 Qwen3Model）
  ├─ 选生成函数（stream_cache / stream / 优化版 cache）
  └─ 选设备
下载权重 + 构建分词器（base 或 reasoning 分支）
实例化模型 → 加载 state_dict → .to(device) → （可选）compile
固定 prompt → max_new_tokens=2048
for iteration in 1..3:
    计时生成（消费流式生成器收集 token id）
    打印本轮配置与输出长度
    generate_stats 打印总耗时 / tokens/sec / 峰值显存
```

注意一个细节：脚本**没有单独的预热轮**——三轮计时的**第 1 轮天然包含编译开销**，所以第 1 轮 tokens/sec 会明显低于第 2、3 轮。这是阅读其输出时必须知道的口径；本讲综合实践会让你在自己的脚本里补上显式预热，得到更干净的数据。

#### 4.2.3 源码精读

按执行顺序读四个片段。

① 开关定义与按开关选实现。

[ch02/03_optimized-LLM/compare_inference.py:L39-L43](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/03_optimized-LLM/compare_inference.py#L39-L43)

```python
parser.add_argument(
    "--compile",
    action="store_true",
    help="Compile PyTorch model."
)
```

`--compile` 是 `store_true` 型布尔开关；同类还有 `--cache`、`--optimized`、`--reasoning`（L34-L55）。

[ch02/03_optimized-LLM/compare_inference.py:L60-L76](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/03_optimized-LLM/compare_inference.py#L60-L76)

```python
if args.optimized:
    from reasoning_from_scratch.qwen3_optimized import Qwen3Model
else:
    from reasoning_from_scratch.qwen3 import Qwen3Model

if args.cache:
    if args.optimized:
        from reasoning_from_scratch.qwen3_optimized import generate_text_basic_cache as generate_text
        is_streaming_generate = False
    else:
        from reasoning_from_scratch.ch02 import generate_text_basic_stream_cache as generate_text
        is_streaming_generate = True
else:
    from reasoning_from_scratch.ch02 import generate_text_basic_stream as generate_text
    is_streaming_generate = True
```

这段用**延迟导入 + 起统一别名**的手法：按开关决定从哪个模块导入哪个 `Qwen3Model` / 生成函数，再绑定到 `generate_text` 这一个名字上——后续代码完全不感知差异。`is_streaming_generate` 标志则告诉计时循环该用「逐 token 消费生成器」还是「一次性拿张量」两种消费方式。

② 计时循环：三轮生成 + 流式消费。

[ch02/03_optimized-LLM/compare_inference.py:L132-L164](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/03_optimized-LLM/compare_inference.py#L132-L164)

```python
for iteration in range(1, 4):
    # ……打印本轮的 optimized/torch/device/cache/compile/reasoning 配置……
    start_time = time.time()
    if is_streaming_generate:
        generated_ids = []
        for token in generate_text(
            model=model,
            token_ids=input_token_ids_tensor,
            max_new_tokens=max_new_tokens,
            eos_token_id=tokenizer.eos_token_id,
        ):
            generated_ids.append(token.squeeze(0).item())
        output_token_ids_tensor = torch.tensor(generated_ids, device=device)
    else:
        output_token_ids_tensor = generate_text(...)
    end_time = time.time()

    print(f"Output length: {output_token_ids_tensor.numel()}")
    generate_stats(output_token_ids_tensor, tokenizer, start_time, end_time)
```

关键点：

- `time.time()` 只包住生成过程本身，权重下载、模型构建、编译都不计入（编译实际上被算进第 1 轮的生成耗时里——因为编译发生在第 1 轮首次前向时）；
- 流式分支逐 token `.item()` 收集 id——把生成器「拉干」，与 u3-l2 讲过的惰性求值衔接；
- 每轮末尾调用 `generate_stats` 打印 tokens/sec 与峰值显存。

③ 统计口径：`generate_stats`。

[reasoning_from_scratch/ch02.py:L173-L199](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L173-L199)

```python
def generate_stats(output_token_ids, tokenizer, start_time, end_time):
    total_time = end_time - start_time
    print(f"\n\nTime: {total_time:.2f} sec")
    print(f"{int(output_token_ids.numel() / total_time)} tokens/sec")
    # ……CUDA/XPU 后端：synchronize 后读取并复位 max_memory_allocated……
```

吞吐口径是 \( \text{tokens/sec} = \frac{\text{生成 token 数}}{\text{端到端耗时}} \)。另外记住 u3-l2 的结论：GPU 是异步执行的，所以读峰值显存前必须 `synchronize`（L193-L194），读完还要 `reset_peak_memory_stats`（L199），否则下一轮会继承上一轮的峰值。

④ 输出长度受 EOS 影响。脚本里 `max_new_tokens = 2048`（L129），但 base 模型对固定 prompt（"Explain large language models in a single sentence."，L122）大约只生成 50 个 token 就触发 EOS 提前停止——README 因此提醒 `--reasoning` 变体「约生成 2000 token」，两者计时长度不同，对比时要注意口径一致。

#### 4.2.4 代码实践

**实践：跑一轮作者基准，观察「第 1 轮慢、后两轮快」**

1. 实践目标：用真实 0.6B 模型验证编译缓存的摊薄效应，并熟悉基准脚本的输出格式。
2. 操作步骤：

```bash
cd ch02/03_optimized-LLM
# 第一次运行会下载 base 权重与分词器（约 1.2 GB）
uv run compare_inference.py --device cpu --cache
# 然后打开编译，再跑一次
uv run compare_inference.py --device cpu --cache --compile
```

3. 需要观察的现象：`--compile` 运行的 Iteration 1 明显慢于 Iteration 2/3（编译开销计入第 1 轮）；`--cache --compile` 的 Iteration 2/3 相比不带 `--compile` 的各轮有提速（README 数据为 CPU 28 → 68 tokens/sec 量级）。
4. 预期结果：趋势如上；具体数值随机器不同，**待本地验证**。有 GPU 的读者可把 `--device cpu` 换成 `--device cuda`，加速比通常更大（README 的 H100 数据为 48 → 141 tokens/sec）。
5. 若下载失败，按 u1-l3 讲过的排查原则处理：浏览器核对 URL、检查网络代理。

#### 4.2.5 小练习与答案

**练习 1**：compare_inference.py 的三轮计时里，哪一轮包含编译开销？为什么脚本不加单独的预热轮也不算「错误」？

**答案**：第 1 轮包含编译开销（编译发生在该轮首次前向时）。不加预热轮也是一种信息：三轮数字能同时呈现「冷启动成本」和「稳态吞吐」，作者在 README 中记录的就是这种口径；只是做严格对比时应看第 2、3 轮，或在自家脚本里显式预热（见综合实践）。

**练习 2**：为什么脚本要在计时前打印 `torch`、`device`、`cache`、`compile` 等配置行？

**答案**：基准结果只有在配置完全确定时才可比较与复现。打印 torch 版本尤其重要——compile 的行为（TF32 API、dynamo 开关）随 PyTorch 版本变化，README 表格也是按硬件 + 配置逐行给出的。

**练习 3**：`--reasoning` 变体生成约 2000 token，base 变体约 50 token。直接比较两者报告的 tokens/sec 公平吗？

**答案**：不完全公平。编译开销 \( T_{\text{compile}} \) 被摊到 50 token 上远比摊到 2000 token 上重；且 prefill/decode 占比、EOS 停止位置都不同。对比应在相同的权重变体、相同的生成长度口径下进行。

### 4.3 模块三：ch03.py 的 use_compile 分支与重编译抑制

#### 4.3.1 概念说明

第 3 章起，模型加载被收拢为一个函数：`load_model_and_tokenizer(which_model, device, use_compile)`。它把「下载权重 → 建分词器 → 建模型 → 加载权重 → 搬设备 →（可选）编译」整条流水线封装成一次调用，`use_compile` 是布尔开关。评测循环（MATH-500 上千次生成）对吞吐敏感，这条路径让「编译加速」成为一行参数就能打开的选项。

本模块还有个更深的知识点：**为什么编译本项目模型前要设置 `torch._dynamo.config.allow_unspec_int_on_nn_module = True`**。根源在 qwen3.py 的位置游标。

Dynamo 追踪模型时会把 Python 属性当作图输入并设置**守护（guard）**：属性值变了，守护失败，触发再编译。qwen3.py 用普通 int 属性 `self.current_pos` 追踪 KV 缓存位置，每次前向都执行 `self.current_pos = pos_end`——数值每个 decode 步都在涨（0、1、2、3……）。在 PyTorch 2.8 及更新版本更严格的守护策略下，这种「nn.Module 上的整数属性被突变」的模式会导致**每个新位置值都触发一次再编译**，编译开销反复发生、甚至撞上重编译次数上限（默认 `torch._dynamo.config.cache_size_limit = 8`）后性能雪崩。`allow_unspec_int_on_nn_module = True` 告诉 dynamo：把模块上的 int 属性当作「未特化的动态值」处理，而不是逐值特化守护，从而避免反复重编译。

compare_inference.py 的注释用 `self.pos = self.pos + 1` 泛指这一模式；本项目对应的具体代码是 `self.current_pos = pos_end`。

#### 4.3.2 核心流程

```text
load_model_and_tokenizer(which_model, device, use_compile)
  ├─ which_model == "base"      → 下载 base 权重 + 裸文本分词器
  ├─ which_model == "reasoning" → 下载 reasoning 权重 + 聊天模板分词器
  ├─ 其他                        → ValueError
  ├─ Qwen3Model(QWEN_CONFIG_06_B) → load_state_dict → .to(device)
  └─ if use_compile:
        ├─ torch._dynamo.config.allow_unspec_int_on_nn_module = True
        └─ model = torch.compile(model)
```

配合生成函数的标准三件套（每次调用新建 `KVCache` + `reset_kv_cache()` 归零游标，见 u3-l2），`current_pos` 的生命周期是 `0 → pos_end → … → reset 为 0`，编译版模型在多轮生成间复用同一份编译产物。

#### 4.3.3 源码精读

① `load_model_and_tokenizer` 全貌与 `use_compile` 分支。

[reasoning_from_scratch/ch03.py:L52-L90](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L52-L90)

```python
def load_model_and_tokenizer(which_model, device, use_compile, local_dir="qwen3"):
    # ……base / reasoning 两个分支：下载权重、构建对应配置的分词器……
    model = Qwen3Model(QWEN_CONFIG_06_B)
    model.load_state_dict(torch.load(model_path))

    model.to(device)

    if use_compile:
        torch._dynamo.config.allow_unspec_int_on_nn_module = True
        model = torch.compile(model)

    return model, tokenizer
```

要点：

- 编译是**最后一步**，严格排在 `load_state_dict` 与 `.to(device)` 之后；
- 与 compare_inference.py 不同，这里设置 dynamo 开关**没有版本守护**（无条件执行），而 compare_inference.py 用 `>= (2, 8)` 守护——同一开关、两种保守程度，属于两个文件的历史演进差异，读码时值得留意；
- 返回 `(model, tokenizer)` 二元组，编译与否对调用方完全透明。

② 重编译的根源：`current_pos` 的整数属性突变。

[reasoning_from_scratch/qwen3.py:L56-L73](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L56-L73)

```python
self.current_pos = 0  # Track current position in KV cache
...
def forward(self, in_idx, cache=None):
    ...
    pos_start = self.current_pos if cache is not None else 0
    pos_end = pos_start + num_tokens
    ...
    if cache is not None:
        self.current_pos = pos_end
```

`self.current_pos` 是 u2-l5 讲过的模型级位置游标：decode 每步 `pos_end = pos_start + 1`，属性值 0→1→2→… 持续变化。对 eager 执行毫无影响；对 Dynamo 追踪则是「模块 int 属性逐值变化」，正是 4.3.1 分析的重编译模式。归零入口在 [reasoning_from_scratch/qwen3.py:L104-L105](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L104-L105) 的 `reset_kv_cache`。

#### 4.3.4 代码实践

**实践：走一遍 ch03 的一键加载路径，确认包装模型是 drop-in replacement**

1. 实践目标：验证 `use_compile=True` 返回的模型能被上一讲的流式缓存生成函数直接消费，并体会首次/后续生成的耗时差。
2. 操作步骤：以下为示例代码（非项目原有文件）：

```python
# 示例代码：try_ch03_compile.py
import time, torch
from reasoning_from_scratch.ch03 import load_model_and_tokenizer
from reasoning_from_scratch.ch02 import generate_text_basic_stream_cache

device = torch.device("cpu")  # 有 GPU 可改为 "cuda"
model, tokenizer = load_model_and_tokenizer("base", device, use_compile=True)
print(type(model))  # 预期是 torch._dynamo.eval_frame.OptimizedModule 包装

prompt = torch.tensor(tokenizer.encode("Explain gravity briefly."), device=device).unsqueeze(0)

for round_no in range(1, 3):
    t0 = time.perf_counter()
    n = 0
    for token in generate_text_basic_stream_cache(
        model, prompt, max_new_tokens=50, eos_token_id=tokenizer.eos_token_id
    ):
        n += 1
    print(f"第{round_no}轮: {n} tokens, {time.perf_counter()-t0:.2f}s")
```

3. 需要观察的现象：`type(model)` 打印出 `OptimizedModule`（证明拿到的是编译包装而非原始模型）；第 1 轮耗时明显高于第 2 轮（编译摊销）；第 2 轮生成文本与不编译时内容一致（贪心解码确定性）。
4. 预期结果：如上；生成内容可与 u3-l2 的同 prompt 结果对照，**待本地验证**。
5. 排错提示：Windows 上首次调用若报 `cl not found` / `triton not found`，转 4.4 模块的排查清单；或把 `use_compile` 改回 `False` 直接绕过。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `self.current_pos` 从普通 Python int 改成 `torch.register_buffer` 注册的张量，还需要 `allow_unspec_int_on_nn_module` 吗？

**答案**：大概率不需要。buffer 是张量，Dynamo 把它当作图输入张量追踪，其数值变化不会触发「属性守护失败」（守护针对的是 Python 值/形状，而非张量内容）。这正是许多生产实现用张量游标的原因之一。但改写要连带处理 state_dict 污染（需 `persistent=False`）与 `reset_kv_cache` 逻辑，属于行为等价重构，需谨慎测试。

**练习 2**：`load_model_and_tokenizer` 里编译发生在 `model.to(device)` 之后。如果顺序反过来（先 compile 再 to(device)），可能有什么问题？

**答案**：编译包装后的模型在 `.to(device)` 上通常仍能代理到 `_orig_mod` 执行搬运，但编译产物是针对特定设备生成的；先在 CPU 上触发首次前向编译、再把参数搬到 GPU，会导致设备守护失效、触发针对 GPU 的再编译（甚至报错），白白多付一次编译成本。标准次序「加载 → 搬运 → 编译」保证一次编译即命中目标设备。

**练习 3**：compare_inference.py 用 `(major, minor) >= (2, 8)` 守护 dynamo 开关，ch03.py 无守护直接设置。从「两个文件的用途差异」角度说说哪种更合理。

**答案**：compare_inference.py 是跨版本基准脚本，需要在旧版 PyTorch（该配置项可能不存在或行为不同）上也能运行，版本守护更稳妥；ch03.py 面向本书锁定的较新 torch（≥2.10，见 u1-l2），无守护更简洁。两者都正确，取舍视角是「兼容面 vs 简洁性」。

### 4.4 模块四：基准数据解读与 Windows 特殊处理

#### 4.4.1 概念说明

**基准解读**：README 用两张表给出「标准模型 / 优化模型 × 普通或编译 × 有无缓存 × CPU/MPS/CUDA」的吞吐与显存矩阵。核心结论：

- GPU（H100）上编译收益最大：标准模型普通模式 51 → 164 tokens/sec（约 3.2 倍）；KV cache + 编译 48 → 141 tokens/sec；
- CPU（M4）上 KV cache 是第一增益（6 → 28），再叠加编译到 68；
- **MPS 上标准模型编译直接失败**（表中记为 InductorError）——编译后端对平台的支持度参差，这是「编译是可选优化而非依赖」的直接证据；
- 优化版（`qwen3_optimized`，SDPA + 预分配 KV cache）多数场景更快，但 CPU 编译 + 缓存组合反而是未优化版更快（68 vs 51），且优化版因预分配 KV 张量占用更多内存（H100 上 5.85 GB vs 1.52 GB）。作者建议：**CPU 用标准版 + `--cache --compile`；GPU 用优化版 + `--cache --compile`**。优化版细节留到单元十（u10-l1）精读。

**Windows 特殊处理**：`torch.compile` 依赖 TorchInductor，它要 JIT 编译内核，因此**需要可用的 C/C++ 编译器工具链**。Linux/macOS 一般开箱即用；Windows 需要手动准备 Visual Studio 环境，且 CUDA 场景还要单独装 triton。

#### 4.4.2 核心流程（Windows 编译就绪检查清单）

```text
安装 Visual Studio 2022（勾选 "Desktop development with C++" + 英语语言包）
  → 从 "x64 Native Tools Command Prompt for VS 2022" 启动终端（或先运行 vcvars64.bat）
  → cl.exe 能打印版本信息（编译器就绪）
  → （仅 CUDA）pip install "triton-windows<3.4"
  → 运行带 --compile 的脚本
失败兜底：跳过编译，本书全部代码在 eager 模式下均可运行
```

#### 4.4.3 源码精读

① 基准表与结论建议。

[ch02/03_optimized-LLM/README.md:L81-L84](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/03_optimized-LLM/README.md#L81-L84)

```text
| qwen3.py | Regular           | --device cuda                   | NVIDIA H100 GPU | 51            | 1.55 GB           |
| qwen3.py | Regular compiled  | --device cuda --compile         | NVIDIA H100 GPU | 164           | 1.81 GB           |
| qwen3.py | KV cache          | --device cuda --cache           | NVIDIA H100 GPU | 48            | 1.52 GB           |
| qwen3.py | KV cache compiled | --device cuda --compile --cache | NVIDIA H100 GPU | 141           | 1.81 GB           |
```

H100 上编译带来约 3 倍吞吐（51→164、48→141），代价是约 0.3 GB 的额外显存（编译产物与中间缓冲）。注意 MPS 行（L77、L79）为 InductorError——平台兼容性风险写在了明面上。

[ch02/03_optimized-LLM/README.md:L111-L117](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/03_optimized-LLM/README.md#L111-L117)

> ……the optimized variant is clearly faster …… However, note that the unoptimized version is faster (68 tok/sec) than the optimized version (51 tok/sec) when using the compiled version with KV cache. …… **Perhaps the best recommendation is to use the unoptimized version (with `--cache` and `--compile`) when using a CPU. When using a GPU, use the optimized version (with `--cache` and `--compile`).**

「更快」不是单调关系：优化版预分配 KV cache 用内存换时间（5.85 GB vs 1.5 GB），在 CPU 上这笔交换可能亏本。性能工程没有银弹，只有测量。

② Windows 的先决条件与兜底。

[ch02/04_torch-compile-windows/README.md:L3-L7](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/04_torch-compile-windows/README.md#L3-L7)

> `torch.compile()` relies on *TorchInductor*, which JIT-compiles kernels and requires a working C/C++ compiler toolchain. …… If you are a Windows user and using `torch.compile` sounds too tricky or complicated, don't worry, all code examples in this repository will work fine without compilation.

这三句是本模块的纲领：编译依赖工具链 + 全书代码不依赖编译。

[ch02/04_torch-compile-windows/README.md:L15-L18](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/04_torch-compile-windows/README.md#L15-L18)

> - Select the **"Desktop development with C++"** workload.
> - Make sure to include the **English language pack** (without it, you may run into UTF-8 encoding errors.)

安装 VS2022 的两个易错点：选对工作负载、带上英语语言包（否则可能撞 UTF-8 编码错误）。

[ch02/04_torch-compile-windows/README.md:L24-L35](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/04_torch-compile-windows/README.md#L24-L35)

> Launch Python from the **"x64 Native Tools Command Prompt for VS 2022"** …… Alternatively, you can initialize the environment manually by running: `vcvars64.bat`

`cl.exe` 只在开发者环境里可见——必须从正确的命令提示符启动 Python，或手动执行 `vcvars64.bat` 初始化。

[ch02/04_torch-compile-windows/README.md:L59-L70](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/04_torch-compile-windows/README.md#L59-L70)

```text
### 2.2 Error: `triton not found` (when using CUDA)
Install the Windows build of Triton manually:
pip install "triton-windows<3.4"
```

CUDA 场景的特有依赖：Inductor 生成 GPU 内核靠 triton，Windows 需手动装社区构建 `triton-windows<3.4`。

[ch02/04_torch-compile-windows/README.md:L79-L83](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/04_torch-compile-windows/README.md#L79-L83)

> On Windows, the `cl.exe` compiler is only accessible from within the Visual Studio Developer environment. This means that using `torch.compile()` in notebooks such as Jupyter may not work unless the notebook was launched from a Developer Command Prompt. …… **If compilation continues to cause issues, please feel free to skip it.**

两条收尾注意：Jupyter 需从开发者命令提示符启动才可能编译成功；持续失败就跳过——编译是锦上添花。

#### 4.4.4 代码实践

**实践 A（Windows 用户）：把编译环境跑通或明确放弃**

1. 实践目标：在 Windows 上得到「编译可用」或「确认跳过」的明确结论。
2. 操作步骤：按 4.4.2 清单逐项执行——装 VS2022（C++ 工作负载 + 英语包）→ 从 x64 Native Tools 启动 → 验证 `cl.exe` →（CUDA 用户）`pip install "triton-windows<3.4"` → 运行 4.2.4 实践中的 `--compile` 命令。
3. 需要观察的现象：`cl.exe` 打印版本信息；`--compile` 脚本第 1 轮耗时长、后两轮恢复。
4. 预期结果：任一步失败且短期无法解决时，记录错误关键词（如 `cl not found`、`triton not found`、UTF-8 报错）并对照 README 2.1/2.2 小节处置；最终结论允许是「跳过编译」，这符合作者建议。**待本地验证**。

**实践 B（非 Windows 用户）：从基准表读出三个结论**

1. 实践目标：训练「从测量矩阵提取工程结论」的能力。
2. 操作步骤：只读 [ch02/03_optimized-LLM/README.md:L69-L117](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/03_optimized-LLM/README.md#L69-L117) 的两张表，不看结论段，自己先回答：哪个硬件上编译收益最大？MPS 出现了什么？CPU 上编译 + 缓存该选哪个模型版本？
3. 需要观察的现象：你的三个回答与 README L111-L117 的结论段是否一致。
4. 预期结果：CUDA 收益最大（约 3 倍）；MPS 上标准模型编译报 InductorError；CPU 推荐标准版 + 缓存 + 编译。

#### 4.4.5 小练习与答案

**练习 1**：README 中 MPS 上编译失败记为 InductorError，但 CPU 与 CUDA 都成功。这说明 `torch.compile` 的什么特性？

**答案**：Inductor 后端按平台生成不同代码（GPU 用 Triton、CPU 用 C++），各平台支持成熟度不同。`torch.compile` 不是「一处编译处处运行」的通用加速，而是依赖平台后端能力的优化——基准矩阵里必须逐平台测量。

**练习 2**：为什么优化版模型在 H100 上 KV cache 模式占用 5.85 GB，而标准版只有 1.52 GB？这笔内存换来了什么？

**答案**：优化版的 KVCache 为最大上下文长度（40960）**预分配**了全部 K/V 张量，换来执行期不再反复分配存储（减少分配开销与内存碎片）；标准版缓存随用随长所以省内存。空间换时间的典型取舍，单元十 u10-l1 会精读其实现。

**练习 3**：Jupyter 里运行 `torch.compile` 报「找不到编译器」，而同一环境命令行下正常。为什么？

**答案**：Windows 上 `cl.exe` 只在 Visual Studio 开发者环境中可见。命令行大概率是从开发者提示符启动的（继承了环境变量），而 Jupyter 是从普通环境启动的，没有拿到编译器路径。解法是从 Developer Command Prompt 启动 Jupyter。

## 5. 综合实践

**任务：写一个「预热 + 三轮计时」的 baseline vs compile 对比脚本**

这是本讲规格指定的实践。compare_inference.py 的三轮计时把编译开销计入了第 1 轮；你的脚本要更干净：**显式预热一次后再计时三次取平均**，最终打印加速比。

1. 实践目标：综合运用本讲全部模块——`torch.compile` 接入、版本守护与 dynamo 开关、预热与计时口径、加速比计算。

2. 操作步骤：以下为示例代码（非项目原有文件），保存为 `my_compile_benchmark.py` 于仓库任意可运行处（如 `ch02/03_optimized-LLM/` 目录旁）：

```python
# 示例代码：my_compile_benchmark.py
import time
import torch

from reasoning_from_scratch.ch02 import (
    get_device, generate_text_basic_stream_cache
)
from reasoning_from_scratch.qwen3 import (
    download_qwen3_small, Qwen3Tokenizer, Qwen3Model, QWEN_CONFIG_06_B
)
from pathlib import Path

device = get_device()                       # u3-l1：自动探测设备（CUDA 会开 TF32）
download_qwen3_small(kind="base", tokenizer_only=False, out_dir="qwen3")

tokenizer = Qwen3Tokenizer(
    tokenizer_file_path=Path("qwen3") / "tokenizer-base.json"
)
prompt_ids = torch.tensor(
    tokenizer.encode("Explain large language models in a single sentence."),
    device=device,
).unsqueeze(0)

def timed_generate(model, max_new_tokens=50):
    """一次完整生成，返回耗时秒数与生成 token 数"""
    start = time.perf_counter()
    n = 0
    for _ in generate_text_basic_stream_cache(
        model, prompt_ids, max_new_tokens=max_new_tokens,
        eos_token_id=tokenizer.eos_token_id,
    ):
        n += 1
    return time.perf_counter() - start, n

def build_model():
    m = Qwen3Model(QWEN_CONFIG_06_B)
    m.load_state_dict(torch.load(
        Path("qwen3") / "qwen3-0.6B-base.pth", map_location=device))
    return m.to(device)

def benchmark(model, label):
    elapsed, n = timed_generate(model)          # 预热：触发编译/缓存，不计入统计
    print(f"[{label}] 预热 {elapsed:.2f}s ({n} tokens)")
    times = [timed_generate(model)[0] for _ in range(3)]
    avg = sum(times) / len(times)
    print(f"[{label}] 3 次计时: "
          + ", ".join(f"{t:.2f}s" for t in times)
          + f" | 平均 {avg:.2f}s")
    return avg

# ① baseline：eager 模式
eager_model = build_model()
t_eager = benchmark(eager_model, "eager ")

# ② compile：与 compare_inference.py / ch03.py 相同的接入次序
compiled_model = build_model()                  # 重新构建，避免缓存状态串扰
major, minor = map(int, torch.__version__.split(".")[:2])
if (major, minor) >= (2, 8):
    # 抑制 self.current_pos 逐值突变引起的反复重编译（PyTorch >= 2.8）
    torch._dynamo.config.allow_unspec_int_on_nn_module = True
compiled_model = torch.compile(compiled_model)
t_compiled = benchmark(compiled_model, "compile")

print(f"\n加速比: {t_eager / t_compiled:.2f}x")
```

3. 需要观察的现象：
   - `compile` 一栏的预热耗显著长于其后续三次计时（编译被显式隔离在预热里）；
   - `eager` 一栏预热与后续计时接近；
   - 两种模式生成的文本内容一致（同为贪心解码、同 prompt、同权重）；
   - 加速比为大于 1 的数值。

4. 预期结果：CPU 上期待温和提升（README 同类配置 28 → 68 tokens/sec 量级），GPU 上提升更明显（48 → 141 量级）；MPS 上可能遇到 InductorError，此时按 4.4 的兜底建议跳过编译并记录。具体数值**待本地验证**。

5. Windows 附加项：按 [ch02/04_torch-compile-windows/README.md](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/04_torch-compile-windows/README.md) 的清单逐条记录：是否安装 VS2022 C++ 工作负载、是否从 x64 Native Tools 启动、`cl.exe` 是否可用、（CUDA）`triton-windows` 是否安装；任何一步失败即记录错误关键词并对照该 README 2.1/2.2 处置。

6. 扩展（可选）：把 `generate_text_basic_stream_cache` 换成无缓存的 `generate_text_basic_stream` 再跑一轮，观察「无 cache + 形状每步变化」时编译收益如何变化；或加上 `--optimized` 思路，换 `qwen3_optimized.Qwen3Model` 对比（为 u10-l1 预热）。

## 6. 本讲小结

- `torch.compile` 用 TorchDynamo 捕获计算图、TorchInductor 生成融合内核（GPU 上是 Triton、CPU 上是 C++），把 eager 模式的逐算子调度开销压缩掉；对小而碎的 0.6B 模型收益显著（H100 上约 3 倍 tokens/sec）。
- 编译开销只在首次前向支付，产物有进程内与磁盘两级缓存；基准测试必须「预热 + 多轮计时」，compare_inference.py 的三轮计时的第 1 轮天然包含编译成本。
- 本项目有两个编译接入点：基准脚本 compare_inference.py 的 `--compile` 开关（带 `>= (2, 8)` 版本守护），与 ch03.py `load_model_and_tokenizer` 的 `use_compile` 分支（无守护）；共同点是都设置 `torch._dynamo.config.allow_unspec_int_on_nn_module = True`，以抑制 `self.current_pos = pos_end` 这种「模块 int 属性逐值突变」引发的反复重编译。
- 编译包装（OptimizedModule）代理属性访问，编译后的模型是生成函数的 drop-in replacement；标准次序是「load_state_dict → to(device) → compile」。
- 性能没有银弹：README 基准显示编译在 MPS 可能直接 InductorError、优化版在 CPU 编译 + 缓存组合反而更慢且更吃内存——结论必须来自测量，最终建议是 CPU 用标准版 + `--cache --compile`、GPU 用优化版 + `--cache --compile`。
- Windows 需要 VS2022 C++ 工具链、开发者命令提示符与（CUDA 场景）`triton-windows`；编译是可选优化，全书代码在 eager 模式下均可运行。

## 7. 下一步学习建议

单元三到此完成：你已经掌握设备探测、贪心解码、流式生成、KV 缓存与编译加速的完整推理工具链。接下来的两条路：

1. **主线（推荐）**：进入单元四第 3 章——[reasoning_from_scratch/ch03.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py) 的 MATH-500 评测与数学答案验证器。你会看到本讲的 `load_model_and_tokenizer(use_compile=True)` 被评测循环大量调用，编译带来的吞吐提升直接缩短整轮评测时间。
2. **性能支线**：若对「还能更快」感兴趣，可提前浏览 [reasoning_from_scratch/qwen3_optimized.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3_optimized.py)（SDPA 注意力 + 预分配 KVCache，对应附录 C 与单元十 u10-l1），把本讲 README 表里优化版与标准版的差异当成预习提纲。

建议动手：先把综合实践的基准脚本跑出你机器上的第一份加速比，之后每学一个新优化（缓存、编译、批处理、SDPA）都能用它量化「这次优化值多少」。
