# 程序化 API 与 Generator 生命周期

## 1. 本讲目标

上一讲（u1-l4）我们追到了 CLI 入口 `tilert.generate` 的分发枢纽 `get_generator()`：它先 `load_backend(model_type)` 加载后端，再延迟 import 对应的 Generator 类并构造。但 CLI 只是最外层的壳，真正干活的是 Generator。本讲我们把镜头推近到 Generator 本身，学完后你将能够：

- 用**纯 Python API**（不依赖 CLI）完成一次端到端的文本生成。
- 说清楚 Generator 的完整生命周期：构造 → `init` → `from_pretrained` → `generate` → `cleanup`，以及每一步背后做了什么、为什么必须按这个顺序调用。
- 看懂 `generate` 的返回值结构 `(text, time_list, accepted_counts, prompt_len)`，并据此计算「单 token 平均延迟」。
- 理解 `enable_thinking` 参数如何映射到模型的 chat template（模板变量）。

本讲只关注**如何使用** Generator，不去拆它内部 `decode_layer` 的张量细节——那是后续进阶层（u2）的内容。

## 2. 前置知识

在开始前，请确认你已经理解下面几个概念（前几讲已建立）：

- **后端 `.so` 与单进程单后端约束**：TileRT 把真正的运行时大脑编译进 `libtilert_dsv32.so` / `libtilert_glm5.so`，`import tilert` 不会加载任何后端，必须显式 `load_backend(model_type)`；一个进程只能加载一个后端。
- **握手算子 `tilert_init_op`**：`.so` 被加载后，算子注册进 `torch.ops.tilert.*` 命名空间，但运行时还需要一次 `tilert_init()` 握手才算真正就绪。
- **权重目录**：TileRT 不能直接吃 HuggingFace 原始权重，必须先用 `weight_converter` 转成「每卡一份、键名带 `_dev_{0..7}`」的分片布局（见 u1-l6）。本讲里出现的 `model_weights_dir` 指的就是这个**转换后**的目录。
- **prefill / decode / TPOT**：生成分「预填充」和「逐 token 解码」两阶段；TPOT 指单个输出 token 的延迟，是 TileRT 的核心优化目标。
- **MTP（Multi-Token Prediction）**：一种「投机解码」机制，一次 forward 可能产出多个 token。本讲会用到一个事实：**是否启用 MTP 是在构造 Generator 时就决定的**，因为 MTP 模式需要额外加载 MTP 权重。

> 术语提示：本仓库的代码注释和变量名里频繁出现 **"show hands"**（亮牌）这个词——它是项目内部对「把张量交给 C++ 后端、由后端打出结果」这一动作的形象叫法。`dsa` 则是 DeepSeek Attention 的缩写。看到 `dsa_show_hands`、`ShowHandsDSALayer` 时，心里把它翻译成「驱动后端跑一步解码」即可。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [generator.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py) | DeepSeek-V3.2 的 `DSAv32Generator`，本讲主角。构造参数、生命周期方法、`generate` 主入口都在这里。 |
| [README.md](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md) | 官方编程示例（load_backend → 构造 → from_pretrained → generate）就在这里，是本讲代码实践的依据。 |
| [glm_5/generator.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/glm_5/generator.py) | GLM-5 的 `GLM5Generator`，与 `DSAv32Generator` 几乎镜像，用于对比。 |
| [tilert_init.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/tilert_init.py) | `tilert_init()` 包装的握手算子，对应生命周期的 `init` 步骤。 |
| [end2end.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py) | `ShowHandsDSALayer`，Generator 内部的 `decode_layer`。`from_pretrained/cleanup/reset_sequence` 等真正落在这里。本讲只引用它的对外方法签名，不深入内部。 |

---

## 4. 核心概念与源码讲解

### 4.1 Generator 构造参数

#### 4.1.1 概念说明

`DSAv32Generator` 是一个普通 Python 类（不继承 `nn.Module`）。它把「一次生成任务需要的全部配置」收集到一起：要生成多少 token、采样温度多少、用不用 MTP、权重在哪个目录、用不用 thinking 模式……这些都在**构造时**固定下来。

一个关键设计：Generator 把配置和「执行器」分开——构造函数只做两件事：

1. **记下配置**（temperature、top_p、with_mtp 等）。
2. **构造执行器** `self.decode_layer = ShowHandsDSALayer(...)`，并加载 tokenizer。

注意：**构造时不加载模型权重，也不初始化后端**。权重加载由后续的 `from_pretrained()` 负责，后端握手由 `init()` 负责。这是为了让「构造」是一个廉价、可重复的对象创建动作。

#### 4.1.2 核心流程

Generator 构造的伪代码：

```
DSAv32Generator.__init__(model_args, max_new_tokens, temperature,
                         model_weights_dir, with_mtp, use_topp, top_p,
                         top_k, sampling_seed, enable_thinking):
    1. torch.set_num_threads(64)            # CPU 线程数（权重加载是 CPU 密集）
    2. 记下全部采样/生成长度配置到 self.*
    3. self.tokenizer = AutoTokenizer.from_pretrained(model_weights_dir)
    4. self.eos_id = tokenizer.eos_token_id
    5. self.batch_size = 1                   # 当前固定单序列
    6. self.decode_layer = ShowHandsDSALayer(...)   # 构造执行器（此时还没权重）
    7. self.mtp_seq_len = 4 if with_mtp else 1
```

构造参数一览（按构造签名顺序）：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `model_args` | （必填） | 模型超参 `ModelArgs` 实例，决定层数、维度、序列长度上限等。 |
| `max_new_tokens` | `100` | 本次任务最多生成多少个新 token。 |
| `temperature` | `1.0` | 采样温度。 |
| `model_weights_dir` | `""` | **转换后**的 TileRT 权重目录路径。 |
| `with_mtp` | `False` | 是否加载 MTP（投机解码）权重。决定能否用 MTP 模式生成。 |
| `use_topp` | `False` | 是否用 top-p（核）采样；`False` 表示 top-1（argmax）贪心。 |
| `top_p` | `0.9` | top-p 阈值。 |
| `top_k` | `256` | top-p 采样的候选数上限。 |
| `sampling_seed` | `42` | 采样种子，**每个请求固定**（详见 u3-l4）。 |
| `enable_thinking` | `False` | 是否开启 chat template 的 thinking 模式（见 4.3）。 |

> 反直觉点 1：`with_mtp` 是「**加载** MTP 权重」的开关，不是「**本次**用 MTP 生成」的开关。如果构造时 `with_mtp=False`，那么之后调用 `generate(..., with_mtp=True)` 会被拒绝（因为没加载对应权重）。
>
> 反直觉点 2：`GLM5Generator` 的构造参数**几乎一致**，但内部把 tokenizer 换成了 GLM-5 专用的加载逻辑（含一段 `chat_template.jinja` 回退），并额外维护了一个 `stop_token_ids` 集合（GLM-5 用多个停止串而不只看 EOS）。

#### 4.1.3 源码精读

构造函数签名与默认值在 [generator.py:L32-L44](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L32-L44) ——这就是上面表格的来源。

构造体的关键几行：

- `torch.set_num_threads(64)`，见 [generator.py:L59](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L59) ——之所以把 CPU 线程开到 64，是因为权重加载阶段会在 CPU 侧用多线程并行搬运 8 张卡的分片（见 u2-l3）。
- tokenizer 与 eos：[generator.py:L72-L75](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L72-L75) ——`AutoTokenizer.from_pretrained` 直接从权重目录加载，`trust_remote_code=True` 是因为 DSv3.2/GLM-5 用了自定义 tokenizer。
- 构造执行器：[generator.py:L80-L87](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L80-L87) ——注意它把 `with_mtp / use_topp / top_p / top_k` 透传给 `ShowHandsDSALayer`，说明这些参数会影响**后端图的捕获方式**。
- `mtp_seq_len`：[generator.py:L89](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L89) ——MTP 模式一次喂 4 个 draft token，非 MTP 模式一次 1 个。

`enable_thinking` 的 docstring 明确说明了它的去向，见 [generator.py:L56-L58](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L56-L58)：「Maps to the DSv32 tokenizer's `thinking` Jinja variable」——也就是说它最终会成为 chat template 渲染时的一个变量。这一点我们在 4.3 详细展开。

对比 GLM-5 的构造，tokenizer 处理明显不同：它先尝试 `AutoTokenizer`，失败则回退到 `PreTrainedTokenizerFast`，并从权重目录读 `chat_template.jinja` 手动塞回模板，见 [glm_5/generator.py:L47-L58](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/glm_5/generator.py#L47-L58)。这是两个 Generator 最外在的差异。

#### 4.1.4 代码实践

**实践目标**：验证「构造是廉价的、且不触发后端加载」。

**操作步骤**（在已安装好 `tilert` 的容器内）：

1. 进入 Python，先 `import tilert` 但**不** `load_backend`。
2. 尝试 `from tilert.models.deepseek_v3_2.generator import DSAv32Generator`。
3. （可选）尝试构造一个 `DSAv32Generator`，`model_weights_dir` 指向一个真实权重目录。

**需要观察的现象**：你会发现单纯 `import` 和「能否构造」与是否调用 `load_backend` 无关——构造函数里没有任何调用后端 `.so` 的代码（它只动 tokenizer 和 `ShowHandsDSALayer` 对象本身）。后端真正被需要是在 `init()` / `from_pretrained()` 之后。

**预期结果**：

- `import` 成功，`tilert.__version__` 可读出（如 `0.1.5.post1`）。
- 构造对象成功，能打印出 `generator.max_new_tokens` 等属性。
- 但如果此时跳过 `load_backend` 直接调 `init()`，会在 `torch.ops.tilert.tilert_init_op()` 处报错（命名空间里没有该算子）——这正是「构造 ≠ 就绪」的体现。

> 说明：构造本身需要真实的 tokenizer 文件存在，若没有权重目录可用，可只做第 1、2 步，重点体会「import 与构造都不碰后端」这一结论。

#### 4.1.5 小练习与答案

**练习 1**：如果构造时 `with_mtp=False`，之后调用 `generate(prompt, with_mtp=True)` 会怎样？为什么？

**参考答案**：会抛 `ValueError("Cannot use MTP mode: MTP weights were not loaded")`。因为 `with_mtp` 是**加载 MTP 权重**的开关，构造时没加载，运行时就缺这部分权重，无法走 MTP 解码路径。对应代码见 [generator.py:L176-L178](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L176-L178)。

**练习 2**：`top_p=0.9` 但 `use_topp=False`，实际采样会走哪条路？

**参考答案**：走 top-1（argmax）贪心。`use_topp` 才是「是否启用核采样」的真正开关，`top_p` 只在 `use_topp=True` 时生效。这也是为什么 CLI 里 `use_topp` 是由 `top_p < 1.0` 隐式推导的（见 u1-l4）。

---

### 4.2 init / from_pretrained / generate / cleanup 生命周期

#### 4.2.1 概念说明

Generator 不是「构造完就能 `generate`」的。它有一段确定的生命周期，对应后端从「冷」到「热」再到「回收」的过程：

```
构造(廉价) → init(握手) → from_pretrained(加载权重) → generate(可多次) → cleanup(释放)
```

四个步骤各自负责一件不可替代的事：

- **`init()`**：调用握手算子 `tilert_init_op()`，让 C++ 后端进入就绪状态。
- **`from_pretrained()`**：把转换好的分片权重真正加载进 8 张卡的显存，并与后端的 `params/temp_vars/caches` 绑定（这一步在进阶层 u2-l3 详细拆解）。
- **`generate(prompt)`**：跑一次完整的生成；可以反复调用多次（每次内部会 `reset_sequence` 复位 KV 缓存）。
- **`cleanup()`**：调用后端的 `go_home` 释放 CUDA Graph、显存等资源。

> 注意：实际使用中，`load_backend` 由 `tilert.generate` CLI 的 `get_generator` 在构造前自动做了；但你写程序化 API 时，必须**自己**先 `tilert.load_backend(model_type)`，否则 `init()` 会失败。

#### 4.2.2 核心流程

完整生命周期的伪代码与状态变迁：

```
# 0) 先加载后端（每个进程一次）
tilert.load_backend("deepseek_v3_2")     # ctypes + torch.ops.load_library

# 1) 构造 Generator（廉价：只建对象）
g = DSAv32Generator(model_args=ModelArgs(), model_weights_dir=PATH, ...)

# 2) init：握手
g.init()                                  # -> torch.ops.tilert.tilert_init_op()

# 3) from_pretrained：加载分片权重到 8 卡显存 + 绑定后端
g.from_pretrained()                       # -> decode_layer._init_weights(PATH)

# 4) generate：可多次调用
text, times, accepted, plen = g.generate(prompt)
text, times, accepted, plen = g.generate(another_prompt)
#   每次内部: set_sampling_seed -> 解码循环 -> reset_sequence(复位KV)

# 5) cleanup：释放
g.cleanup()                               # -> dsa_show_hands_go_home(...)
```

几个要点：

- **`init` 和 `from_pretrained` 的顺序**：先握手、后加载权重。`init()` 让后端就绪，`from_pretrained()` 才能把权重交给一个就绪的后端。
- **`generate` 可复用**：每次 `generate` 结束都会调 `reset_sequence()` 把 KV 缓存复位，所以同一个 Generator 可以连续回答多个 prompt，不必重新加载权重。但**采样配置变了**会触发 CUDA Graph 重捕获（见 u3-l4），有额外开销。
- **`cleanup` 是幂等的收尾**：它调用后端的 `dsa_show_hands_go_home`，把捕获的图和显存还回去。即使你忘了调，`ShowHandsDSALayer` 的 `__del__` 也会兜底尝试一次。

#### 4.2.3 源码精读

**`init()`** 只有一行，但意义重大，见 [generator.py:L91-L93](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L91-L93)：它调用 `tilert_init()`。而 `tilert_init()` 本身是对握手算子的薄包装，见 [tilert_init.py:L11-L13](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/tilert_init.py#L11-L13)，即 `torch.ops.tilert.tilert_init_op()`。这正是 u1-l3 讲过的「load_library 注册算子之后，还需一次握手」的那次握手。

**`from_pretrained()`** 见 [generator.py:L103-L105](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L103-L105)：它把活儿交给 `self.decode_layer.from_pretrained(self.model_weights_dir)`。真正干活的 `ShowHandsDSALayer.from_pretrained` 会先检查目录存在性，再调内部 `_init_weights`，见 [end2end.py:L526-L530](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L526-L530)。（`_init_weights` 内部那套「8 卡多线程并行加载」是 u2-l3 的主题，本讲不展开。）

另外还有两个相关的「加载」方法：

- `init_random_weights()`：见 [generator.py:L99-L101](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L99-L101)，调 `_init_weights(None)`，用于不加载真实权重、只用随机权重把图跑通（调试/测试用）。
- `from_pretrained_with_cache(...)`：见 [generator.py:L128-L136](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L128-L136)，复用上一次提取出的 MoE/MLP 算子对象，加速**反复**加载权重（基准测试时很有用）。

**`generate()`** 是生命周期的主入口，见 [generator.py:L154-L185](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L154-L185)。它的关键逻辑：

1. 决定本次是否走 MTP：`active_mtp = with_mtp if with_mtp is not None else self.with_mtp`，并做「MTP 权重是否已加载」的校验。
2. `self.decode_layer.set_sampling_seed(self.sampling_seed, with_mtp=active_mtp)`：固定本请求的采样种子。
3. 分发到 `_generate_with_mtp` 或 `_generate_without_mtp`。

解码循环本身（以非 MTP 为例）在 [generator.py:L220-L245](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L220-L245)：每一步调 `self.decode_layer.forward(token, with_mtp=...)` 拿到多卡结果，从第 0 卡的 `intermediates[Idx.TOKEN_OUT]` 取出 next token，写入缓冲，直到命中 EOS。每步用 `time.time()` 计时塞进 `time_list`。

循环结束后会调 `self.decode_layer.reset_sequence()` 复位，见 [generator.py:L254](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L254)，对应的 `reset_sequence` 会调 `dsa_show_hands_reset(...)`，见 [end2end.py:L572-L577](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L572-L577)。

**`cleanup()`** 见 [generator.py:L95-L97](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L95-L97)，转发到 `ShowHandsDSALayer.cleanup`，后者调 `dsa_show_hands_go_home(...)` 释放资源，见 [end2end.py:L579-L584](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L579-L584)。`__del__` 里的兜底见 [end2end.py:L586-L590](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L586-L590)。

#### 4.2.4 代码实践

**实践目标**：完整跑一遍生命周期，确认 `generate` 可被多次复用。

**操作步骤**（依据 README 的编程示例，见 [README.md:L191-L218](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L191-L218)）：

```python
# 示例代码：基于 README 的最小复用脚本
import tilert
from tilert.models.deepseek_v3_2.generator import DSAv32Generator
from tilert.models.deepseek_v3_2.model_args import ModelArgs

WEIGHTS = "/path/to/DeepSeek-V3.2-TileRT"   # 改成你的转换后权重目录

tilert.load_backend("deepseek_v3_2")

g = DSAv32Generator(
    model_args=ModelArgs(),
    max_new_tokens=64,
    model_weights_dir=WEIGHTS,
    with_mtp=False,
)
g.init()
g.from_pretrained()

# 第一次生成
out1 = g.generate("用一句话解释什么是张量。", print_log=False)
print("【第一次】", out1[0][:40], "...")

# 复用同一个 generator 再来一次（无需重新加载权重）
out2 = g.generate("用一句话解释什么是 RoPE。", print_log=False)
print("【第二次】", out2[0][:40], "...")

g.cleanup()
```

**需要观察的现象**：

- 第一次 `generate` 之后，第二次 `generate` **不需要**再 `from_pretrained`——权重仍在显存里。
- 每次 `generate` 内部日志会出现 `==== Performance ====` 段（若 `print_log=True`），打印平均 token 延迟与标准差。

**预期结果**：两次都返回文本字符串，且第二次的首 token 不需要重新加载权重。

> 待本地验证：脚本需要真实 8×B200 环境与转换好的权重才能跑通；若无硬件，请把它当作「源码阅读型实践」——重点确认「`generate` 复用不重新加载权重」这一行为与源码一致。

#### 4.2.5 小练习与答案

**练习 1**：为什么推荐顺序是 `init()` → `from_pretrained()`，而不是反过来？

**参考答案**：`init()` 通过 `tilert_init_op()` 让后端进入就绪态；`from_pretrained()` 要把权重绑定进一个就绪的后端（其内部的 `prepare_money` 会把 `params/temp_vars/caches` 交给 C++）。若后端未握手就加载权重，绑定会失败或行为未定义。

**练习 2**：连续调用两次 `generate`，第二次为什么不用重新 `from_pretrained`？

**参考答案**：因为第一次 `generate` 结束时调用了 `reset_sequence()`，只复位了 KV 缓存（清空历史对话的注意力状态），并没有卸载模型权重。权重仍然在显存里，所以可以直接进行下一轮生成。

---

### 4.3 generate 返回值结构与 enable_thinking

#### 4.3.1 概念说明

`generate` 不是只返回一个字符串，它返回一个**四元组**，把「结果文本」和「性能度量」一起给你：

```python
text, time_list, accepted_counts, prompt_len = generator.generate(prompt)
```

| 返回项 | 类型 | 含义 |
| --- | --- | --- |
| `text` | `str` | 生成的文本（末尾带一个 `\n`）。 |
| `time_list` | `list[float]` | 每个 token（MTP 模式下是每次 decode forward）的耗时，单位秒。 |
| `accepted_counts` | `list[int]` | MTP 模式下每次 forward **接受**的 token 数；非 MTP 模式为空列表 `[]`。 |
| `prompt_len` | `int` | prompt 经过 chat template 渲染后的 token 数（含模板添加的特殊 token）。 |

有了 `time_list` 和「生成的 token 数」，就能算出 TileRT 最关心的指标——**单 token 平均延迟**和**有效 tokens/s**。

另一个影响生成行为的构造参数是 `enable_thinking`。它不是采样参数，而是控制 **chat template（对话模板）渲染**时是否插入「思考模式」相关的内容。所谓 chat template，就是把你给的 `prompt` 字符串包装成模型训练时见过的那种 `<|user|>...<|assistant|>` 对话格式的一段 Jinja 模板。`enable_thinking` 决定模板里的 `thinking`（DSv3.2）或 `enable_thinking`（GLM-5）变量取 True 还是 False。

#### 4.3.2 核心流程

**返回值的产生流程**（非 MTP）：

```
generate(prompt):
  prompt_tokens = tokenizer.apply_chat_template([{role:user, content:prompt}],
                                                thinking=enable_thinking)
  prompt_len = len(prompt_tokens)
  解码循环: 每步 forward -> 取 next_token -> 计时 append 到 time_list
  reset_sequence()
  截取 [prompt_len : prompt_len+max_new_tokens]，按 EOS 截断
  decode 回文本
  return (f"{text}\n", time_list, [], prompt_len)
```

**平均单 token 延迟**的计算：

设 `time_list = [t_1, t_2, ..., t_n]`，其中 \(n\) 为生成的 token 数（非 MTP 下 `n == len(time_list)`）。则

\[
\text{avg\_latency} = \frac{1}{n}\sum_{i=1}^{n} t_i
\]

\[
\text{effective\_tps} = \frac{n}{\sum_{i=1}^{n} t_i} = \frac{1}{\text{avg\_latency}}
\]

项目里已经提供了一个现成的统计函数 `stats_time`，它就是这么算的（外加一个标准差）。

**enable_thinking 的作用路径**：

```
enable_thinking(构造参数)
   └─> generate() 内调用 apply_chat_template(..., thinking=self.enable_thinking)
          └─> tokenizer 用 Jinja 模板渲染 prompt
                 └─> 渲染出的 token 序列不同（思考开关影响模板分支）
                        └─> prompt_len 不同 / 模型行为不同
```

#### 4.3.3 源码精读

**返回类型签名**：`generate` 的返回标注是 `tuple[str, list[float], list[int], int]`，见 [generator.py:L154-L161](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L154-L161)，docstring 也写明 `accepted_counts is empty for non-MTP mode`。

**非 MTP 分支的返回**：`return result, time_list, [], prompt_len`，见 [generator.py:L182-L185](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L182-L185)——注意第三个位置是字面量 `[]`。

**MTP 分支的返回**：返回 `decode_accepted_counts`（每次 forward 接受了几个 token），见 [generator.py:L412-L417](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L412-L417)。README 给的 `mean=2.77, min=1, max=4` 例子（见 [README.md:L272-L274](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L272-L274)）就是这个列表的统计。

**现成的统计函数 `stats_time`**：见 [generator.py:L21-L28](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L21-L28)。关键三行：

```python
avg_time = sum(time_list) / len(time_list)                       # 单 token 平均延迟
std_dev  = math.sqrt(sum((x-avg_time)**2 for x in time_list)/n)  # 抖动
logger.info(f"--Effective tokens per second: {1 / avg_time:.4f}") # 有效 TPS
```

这与上面的公式完全一致。`stats_time` 在非 MTP 生成结束时被调用，见 [generator.py:L251](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L251)。

**enable_thinking 怎样映射到 chat template**：非 MTP 分支里，渲染 prompt 的调用是

```python
prompt_tokens = self.tokenizer.apply_chat_template(
    [{"role": "user", "content": prompt}],
    add_generation_prompt=True,
    thinking=self.enable_thinking,      # <-- 这里
)
```

见 [generator.py:L196-L200](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L196-L200)。也就是说，`enable_thinking` 最终成为 `apply_chat_template` 的 `thinking=` 关键字参数，由 DSv3.2 tokenizer 的 Jinja 模板消费——构造函数 docstring 里说的「Maps to the DSv32 tokenizer's `thinking` Jinja variable」指的就是这条路径（docstring 见 [generator.py:L56-L58](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L56-L58)）。

> 对比 GLM-5：GLM-5 的模板变量名是 `enable_thinking`（不是 `thinking`），见 [glm_5/generator.py:L176-L182](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/glm_5/generator.py#L176-L182)（`enable_thinking=self.enable_thinking`）。变量名不同，但「构造参数 → chat template 变量」的设计是一致的。

**prompt_len 的来源**：`prompt_len = len(prompt_tokens)`，见 [generator.py:L203](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L203)。注意它是**渲染后**的长度，所以包含模板添加的 `<｜User｜>`、`<｜Assistant｜>` 等特殊 token，通常比你肉眼数的 prompt 字数要大一些。后续截取生成结果时用 `[prompt_len : prompt_len + max_new_tokens]`（见 [generator.py:L258](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L258)），正是为了只保留「新生成」的部分。

#### 4.3.4 代码实践

**实践目标**：从 `generate` 返回值计算平均单 token 延迟，并对比 `enable_thinking` 的效果。

**操作步骤**：

```python
# 示例代码：解析返回值 + 对比 enable_thinking
import tilert
from tilert.models.deepseek_v3_2.generator import DSAv32Generator, stats_time
from tilert.models.deepseek_v3_2.model_args import ModelArgs

tilert.load_backend("deepseek_v3_2")

def run(enable_thinking: bool):
    g = DSAv32Generator(
        model_args=ModelArgs(),
        max_new_tokens=128,
        model_weights_dir="/path/to/DeepSeek-V3.2-TileRT",
        with_mtp=False,
        enable_thinking=enable_thinking,
    )
    g.init()
    g.from_pretrained()

    text, time_list, accepted, prompt_len = g.generate(
        "证明：根号2是无理数。", print_log=False
    )

    n = len(time_list)
    avg_ms = (sum(time_list) / n) * 1000 if n else float("nan")
    print(f"thinking={enable_thinking}: prompt_len={prompt_len}, "
          f"generated={n} tokens, avg={avg_ms:.3f} ms/token, "
          f"tps={1/(sum(time_list)/n):.2f}" if n else "no tokens")
    # 也可以直接用项目自带的统计：
    stats_time(time_list, f"==== thinking={enable_thinking} ====")
    g.cleanup()

run(enable_thinking=False)
# 注意：两个 run() 各自起进程会更稳妥，因为同一个进程里 Generator 反复构造/清理
# 可能涉及 CUDA Graph 重捕获。最干净的做法是分别在两个进程里跑。
```

**需要观察的现象**：

- `enable_thinking=True` 时，`prompt_len` 通常会**变大**（模板插入了引导思考的特殊标记），且生成的文本里可能带有 `<think>...</think>` 之类的段落。
- `accepted` 列表在非 MTP 模式下始终是 `[]`。
- `avg_ms` 这个数字就是 TileRT 优化最在意的 TPOT（量级在毫秒）。

**预期结果**：

- `prompt_len(False)` < `prompt_len(True)`（思考模式模板更长）。
- 计算出的 `tps` 与 `stats_time` 打印的 `Effective tokens per second` 一致。

> 待本地验证：上述数值结论依赖真实模型与硬件；若无可运行环境，请把重点放在「`prompt_len` 是渲染后长度」「`accepted` 非 MTP 时为空」「`stats_time` 的公式」这三点源码事实上。

#### 4.3.5 小练习与答案

**练习 1**：非 MTP 模式下 `accepted_counts` 为什么是空列表？

**参考答案**：`accepted_counts` 描述的是「MTP 每次 forward 接受了几个 draft token」，这是投机解码才有的概念。非 MTP 模式每次 forward 只产 1 个 token、不存在「接受几个」的问题，所以 `generate` 在非 MTP 分支直接返回字面量 `[]`，见 [generator.py:L185](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/generator.py#L185)。

**练习 2**：`prompt_len` 为什么不等于 `len(prompt)`（prompt 字符串的字符数）？

**参考答案**：因为 `prompt_len` 是 `apply_chat_template` 渲染后的 **token 数**，既经过了「字符→token」的分词，又包含了模板添加的对话角色标记等特殊 token。字符数、token 数、渲染后 token 数是三个不同的量。

**练习 3**：如果把 `enable_thinking` 从 `False` 改成 `True`，`generate` 的哪个返回分量最可能变化？为什么？

**参考答案**：最直接变化的是 `prompt_len`（模板分支不同导致渲染序列变长），进而 `text` 内容也会变（模型可能输出思考过程）。`time_list` 的单步延迟基本不受影响（那是后端解码的固有开销），但因为生成长度可能变长，`time_list` 的**长度**会变。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个**端到端小程序化生成 + 性能分析**任务：

**任务**：写一个脚本，用程序化 API（不用 CLI）完成一次 DeepSeek-V3.2 生成，并把性能数据打印成一张小表。

要求：

1. 先 `tilert.load_backend("deepseek_v3_2")`（体会：这是程序化用法里你必须自己做的、CLI 会替你做的那一步）。
2. 构造 `DSAv32Generator`，依次调用 `init()` → `from_pretrained()`。
3. 调用 `generate(prompt)`，**完整解构**返回的四元组 `(text, time_list, accepted_counts, prompt_len)`。
4. 用 `time_list` 自行计算：
   - 生成 token 数 `n`；
   - 平均单 token 延迟（ms）；
   - 有效 tokens/s；
   - 延迟标准差（ms）。
   并与 `stats_time(time_list, ...)` 的输出对照，确认你的公式和项目一致。
5. 断言 `accepted_counts == []`（因为 `with_mtp=False`），验证你对返回结构的理解。
6. 最后**务必**调用 `cleanup()`。

**进阶追问**（源码阅读型，不要求运行）：

- 阅读本讲引用的 README 示例 [README.md:L191-L218](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L191-L218)，发现官方示例里**没有**显式写 `g.init()` 和 `g.cleanup()`。结合本讲源码，思考：这是否意味着这两步可以省略？在什么条件下它们会被隐式触发？（提示：`__del__` 兜底；以及某些集成路径里 `init` 被并入 `from_pretrained` 的上游调用。）把你的结论写下来。

> 说明：第 6 步的 `cleanup()` 是良好实践；即便忘记，`ShowHandsDSALayer.__del__`（[end2end.py:L586-L590](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/end2end.py#L586-L590)）也会尝试兜底，但依赖 GC 时机不可控，所以显式调用更稳妥。

---

## 6. 本讲小结

- Generator 是一个普通 Python 类，**构造是廉价的**：只记录配置、加载 tokenizer、构造 `decode_layer` 对象，**不**加载权重、**不**初始化后端。
- 完整生命周期是 `构造 → init → from_pretrained → generate(可多次) → cleanup`：`init` 握手后端、`from_pretrained` 加载 8 卡分片权重、`generate` 解码、`cleanup` 调 `go_home` 释放资源。
- `generate` 可复用：每次结束会 `reset_sequence()` 复位 KV 缓存，因此同一 Generator 能连续回答多个 prompt 而无需重新加载权重。
- `generate` 返回四元组 `(text, time_list, accepted_counts, prompt_len)`：非 MTP 时 `accepted_counts` 为 `[]`；`prompt_len` 是 chat template **渲染后**的 token 数。
- 平均单 token 延迟 = `sum(time_list)/len(time_list)`，有效 TPS = `1/平均延迟`，项目用 `stats_time` 函数统一计算。
- `enable_thinking` 不是采样参数，而是映射到 `apply_chat_template` 的模板变量（DSv3.2 叫 `thinking`，GLM-5 叫 `enable_thinking`），会改变渲染后的 `prompt_len` 与生成行为。

---

## 7. 下一步学习建议

到这里你已经会用程序化 API 跑通生成并读懂返回值。接下来建议：

- **横向**：阅读 [glm_5/generator.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/glm_5/generator.py)，对比它和 `DSAv32Generator` 在 tokenizer、停止串、`ar_steps`（非 MTP 也做多步投机）上的差异——你会更清楚「镜像对应」具体体现在哪。
- **纵向（权重）**：进入 u1-l6，看 `weight_converter` 如何把 HF 权重变成 `from_pretrained` 能吃的 `*_dev_{0..7}` 分片布局，补齐「权重从哪来」这一环。
- **纵向（内部）**：进入 u2-l3（`ShowHandsDSALayer`），看 `from_pretrained` 背后那套「8 卡多线程并行加载 + V2 P2P + prepare_money 绑定」是怎么把权重真正塞进后端的——本讲里被我们刻意「黑盒化」的 `decode_layer` 就在那里被打开。
- **采样与图**：进入 u3-l4，看 `update_sampling_config` 在采样参数变化时为何要 `go_home` 后重新 `prepare_money` 重捕获 CUDA Graph——这解释了为什么「反复换 top_p」会有额外开销。
