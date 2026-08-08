# Transformers 集成与权重加载

## 1. 本讲目标

前面几讲我们已经把 DFlash 草稿模型的**内部结构**拆得很细：它复用 Qwen3 组件、没有自己的 `embed_tokens` 和 `lm_head`（两个「借」）、用 `fc` 把 target 多层隐藏状态投影进 draft 空间。但我们一直回避了一个非常现实的问题：

> 这个「残缺」的模型，到底怎么从 Hugging Face Hub 上**加载下来**、变成一个能直接 `draft.spec_generate(...)` 调用的对象？

本讲就回答这个问题。学完后你应该能够：

1. 说清 `DFlashDraftModel` 继承 `Qwen3PreTrainedModel` 带来了什么——尤其是**免费获得 `from_pretrained` 权重加载能力**。
2. 解释类属性 `_no_split_modules` 的作用，以及它和 `device_map` 的关系。
3. 读懂 `_run_transformers` 里 target 与 draft 两条加载链路的差异。
4. 理解 attention 实现（`flash_attention_2` / `sdpa`）的选择与**回退逻辑**，以及为什么回退会拉低加速比。
5. 说清为什么 Transformers 后端**只支持 Qwen3 与 LLaMA-3.1-8B-Instruct**。

本讲的主题是「**加载即用的链路**」——把训练好的权重、Transformers 的加载基础设施、DFlash 的自定义注意力串成一条完整通路。

## 2. 前置知识

本讲假设你已经读过：

- **u1-l3**：`dflash/__init__.py` 的懒加载导出，知道 `DFlashDraftModel` 来自 `dflash.model`。
- **u1-l4**：`spec_generate` 的签名与「两个借」（draft 借 target 的 `embed_tokens` 和 `lm_head`）。
- **u2-l2**：`DFlashDraftModel` 的内部结构（layers / norm / rotary_emb / fc / hidden_norm）与 `build_target_layer_ids`。

下面几个 Transformers 概念会用通俗语言补齐，不默认你熟悉：

- **`from_pretrained`**：Hugging Face Transformers 几乎所有模型都有的类方法。给它一个模型名或本地路径，它会自动下载权重、读取 `config.json`、按配置实例化模型对象、把权重张量逐个对位加载进去，最后返回一个可以直接用的模型。本讲的关键就是：**DFlash 自己没写这套逻辑，而是「继承」来的**。
- **`PreTrainedModel`**：Transformers 里所有模型的公共基类，`from_pretrained`、`save_pretrained`、`post_init`（权重初始化收尾）等方法都在这里实现。
- **`device_map`**：`from_pretrained` 的一个参数，用来把一个大模型**切分到多张卡**上（模型并行）。切分时需要知道「哪些模块不能被拆开」，这正是 `_no_split_modules` 的用途。
- **attention 实现（attn_implementation）**：同一个注意力计算可以用不同后端执行，常见有 `flash_attention_2`（FlashAttention，快且省显存）、`sdpa`（PyTorch 内置的 scaled dot-product attention）、`eager`（朴素实现）。DFlash 草稿模型的注意力是自定义的，但执行后端仍在这套体系里选。

## 3. 本讲源码地图

本讲涉及两个文件，每个文件我们只聚焦「加载与集成」相关的部分：

| 文件 | 本讲关注的内容 |
|---|---|
| `dflash/model.py` | `DFlashDraftModel` 的类定义、`_no_split_modules`、`__init__` 末尾的 `post_init()`、以及注意力里 `_attn_implementation` 的派发 |
| `dflash/benchmark.py` | `_check_transformers_model`（模型白名单）、`_get_transformers_attn_impl`（attention 回退）、`_run_transformers` 里的两条 `from_pretrained` 调用 |

> 注意：`model.py` 里的 `forward`、`fc`/`hidden_norm`、`build_target_layer_ids` 等内部计算在 u2-l2 已讲透，本讲不再重复，只在需要时一笔带过。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1** `DFlashDraftModel` 类定义与 `_no_split_modules`——它凭什么能被 `from_pretrained` 加载。
2. **4.2** `_run_transformers` 中的 `from_pretrained` 调用——target 与 draft 两条加载链路。
3. **4.3** attention 实现选择、回退与模型白名单——`_get_transformers_attn_impl` 与 `_check_transformers_model`。

---

### 4.1 DFlashDraftModel 类定义与 _no_split_modules

#### 4.1.1 概念说明

u2-l2 我们说过 `DFlashDraftModel` 是一个「残缺」的小模型：有 layers、norm、rotary_emb、fc、hidden_norm，却没有 `embed_tokens` 和 `lm_head`。但它在代码层面还有一个重要身份——它是 `Qwen3PreTrainedModel` 的子类：

[dflash/model.py:302-304](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L302-L304) 声明 `DFlashDraftModel` 继承自 `Qwen3PreTrainedModel`，并指定 `config_class` 与 `_no_split_modules`：

```python
class DFlashDraftModel(Qwen3PreTrainedModel):
    config_class = Qwen3Config
    _no_split_modules = ["Qwen3DFlashDecoderLayer"]
```

这两个类属性看似不起眼，却是「加载即用」的关键：

- **`config_class = Qwen3Config`**：告诉 Transformers「加载这个模型时，`config.json` 要用 `Qwen3Config` 来解析」。DFlash 草稿模型的 `config.json` 复用了 Qwen3 的字段（`hidden_size`、`num_hidden_layers`、`num_attention_heads` 等），只是额外加了 `block_size`、`num_target_layers`、`dflash_config`。所以直接用 `Qwen3Config` 就能读完，无需自定义 config 类。

- **继承 `Qwen3PreTrainedModel`**：这是真正的「免费午餐」。`Qwen3PreTrainedModel`（往上追溯是 `PreTrainedModel`）已经实现了 `from_pretrained`、`save_pretrained`、`post_init`、`push_to_hub` 等一整套基础设施。DFlash **一行都没自己写**，就拿到了完整权重加载能力。

#### 4.1.2 核心流程：from_pretrained 大致做了什么

当你调用 `DFlashDraftModel.from_pretrained("z-lab/Qwen3-8B-DFlash-b16", ...)` 时，继承来的 `from_pretrained` 会大致走如下流程（伪代码，方便建立心智模型）：

```text
from_pretrained(draft_name, attn_implementation=..., dtype=...)
  ├─ 1. 读取 draft 仓库的 config.json → 用 config_class=Qwen3Config 解析
  │      （读到 num_hidden_layers / block_size / dflash_config 等）
  ├─ 2. 把 attn_implementation 写进 config._attn_implementation
  │      （例：把 "flash_attention_2" 或 "sdpa" 记在 config 上）
  ├─ 3. 用 config 实例化空模型：DFlashDraftModel(config)
  │      → 触发 __init__，建好 layers / fc / hidden_norm / rotary_emb ...
  │      → __init__ 末尾调用 post_init() 完成权重初始化收尾
  ├─ 4. 下载/读取权重文件（*.safetensors），逐张量对位 load_state_dict
  └─ 5. 返回权重就绪的模型对象
```

注意第 2 步：**`attn_implementation` 参数最终落在 `config._attn_implementation` 这个字段上**。这个字段会在 4.3 节的注意力派发里被读出来——这是「加载时选后端、运行时用后端」的串联点。

#### 4.1.3 源码精读

[dflash/model.py:306-321](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L306-L321) 是 `__init__`，构建草稿模型各组件并在末尾调用 `post_init()`：

```python
def __init__(self, config) -> None:
    super().__init__(config)          # PreTrainedModel 的初始化（注册 config 等）
    self.config = config
    self.layers = nn.ModuleList(
        [Qwen3DFlashDecoderLayer(config, layer_idx) for layer_idx in range(config.num_hidden_layers)]
    )
    self.target_layer_ids = self.config.dflash_config.get(
        "target_layer_ids", build_target_layer_ids(config.num_target_layers, config.num_hidden_layers)
    )
    self.norm = Qwen3RMSNorm(config.hidden_size, eps=config.rms_norm_eps)
    self.rotary_emb = Qwen3RotaryEmbedding(config)
    self.fc = nn.Linear(len(self.target_layer_ids) * config.hidden_size, config.hidden_size, bias=False)
    self.hidden_norm = Qwen3RMSNorm(config.hidden_size, eps=config.rms_norm_eps)
    self.block_size = config.block_size
    self.mask_token_id = self.config.dflash_config.get("mask_token_id", None)
    self.post_init()                  # ← 来自 PreTrainedModel：初始化权重、设置 gradient checkpointing
```

两个要点：

- `super().__init__(config)` 调用 `Qwen3PreTrainedModel → PreTrainedModel` 的初始化，把 `config` 注册为 `self.config`、准备好各种 Transformer 基础设施。
- 末尾的 `self.post_init()` 是 `PreTrainedModel` 提供的方法，负责「权重初始化收尾」和 gradient checkpointing 的挂载。**所有继承 `PreTrainedModel` 的模型都必须在 `__init__` 末尾调用它**，否则 Transformers 会在加载时报警告。DFlash 老老实实照做了。

关于 `_no_split_modules`，它在「加载时」的用途是支持 `device_map`：当 `from_pretrained` 收到一个 `device_map`（比如 README 里的 `device_map="cuda:0"`，或多卡时的 `"auto"`），Transformers 需要知道**哪些模块是「不可拆分的原子单元」**，才能决定如何把模型切分到不同设备。`_no_split_modules = ["Qwen3DFlashDecoderLayer"]` 的含义就是：**每个 `Qwen3DFlashDecoderLayer`（即每一层 decoder）必须完整地落在同一张卡上，不能把一层的算子拆到两张卡**。

README 的 Transformers 示例正是这么用的——给 draft 传了 `device_map="cuda:0"`：

```python
draft = AutoModel.from_pretrained("z-lab/Qwen3-8B-DFlash-b16",
        trust_remote_code=True, dtype="auto", device_map="cuda:0").eval()
```

如果没有定义 `_no_split_modules`，使用多设备 `device_map` 时 Transformers 会直接报错。DFlash 提前声明，保证了「加载即用」。

> 小结：`DFlashDraftModel` 之所以能用一行 `from_pretrained` 加载，**不是因为它自己实现了加载逻辑，而是因为它继承了 `Qwen3PreTrainedModel`**；`_no_split_modules` 则让它兼容 `device_map`，补全了「加载即用」的最后一块拼图。

#### 4.1.4 代码实践

**实践目标**：亲手确认 `DFlashDraftModel` 继承来的「加载身份」。

**操作步骤**（需要已按 u1-l2 安装 `.[transformers]`，并在有 GPU 的机器上）：

1. 在 Python 里执行：

   ```python
   from dflash.model import DFlashDraftModel
   # 仅看类属性，不必真正下载权重
   print("基类：", DFlashDraftModel.__mro__[1].__name__)
   print("config_class：", DFlashDraftModel.config_class.__name__)
   print("_no_split_modules：", DFlashDraftModel._no_split_modules)
   print("是否有 from_pretrained：", hasattr(DFlashDraftModel, "from_pretrained"))
   ```

2.（可选，需联网+GPU）真正加载一个小草稿模型并检查 `_attn_implementation`：

   ```python
   draft = DFlashDraftModel.from_pretrained(
       "z-lab/Qwen3-4B-DFlash-b16", dtype=torch.bfloat16
   )
   print("block_size：", draft.block_size)
   print("当前 attn 实现：", draft.config._attn_implementation)
   ```

**需要观察的现象**：
- 第 1 步应打印基类是 `Qwen3PreTrainedModel`，`config_class` 是 `Qwen3Config`，`_no_split_modules` 是 `["Qwen3DFlashDecoderLayer"]`，且 `from_pretrained` 存在（来自继承）。
- 第 2 步若不传 `attn_implementation`，`config._attn_implementation` 通常是 `"sdpa"`（Transformers 的默认值）。

**预期结果**：你看到的所有加载相关方法都是「白来的」——它们不在 `dflash/model.py` 里，而在父类里。这一步不需要 GPU 也能跑第 1 步；第 2 步的真实下载结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `_no_split_modules` 这一行删掉，模型还能用 `from_pretrained(..., device_map="cuda:0")` 加载吗？为什么？

> **答案**：单设备字符串如 `"cuda:0"` 通常仍可加载（整个模型放一张卡，无需切分）；但一旦用多设备 `device_map`（如 `"auto"`、`"balanced"`），Transformers 会因为不知道原子模块边界而报错。所以这行是为多卡/量化场景兜底，保证「加载即用」。

**练习 2**：`DFlashDraftModel` 自己实现了 `from_pretrained` 吗？在 `dflash/model.py` 里能找到它的定义吗？

> **答案**：找不到。`from_pretrained` 是从 `Qwen3PreTrainedModel → PreTrainedModel` 继承来的，DFlash 没有重写。这正是「继承换加载能力」的核心。

**练习 3**：为什么 `config_class` 用 `Qwen3Config` 而不是自定义一个 `DFlashConfig`？

> **答案**：因为 DFlash 草稿模型的 `config.json` 复用了 Qwen3 的全部标准字段，只是额外加了 `block_size`、`num_target_layers`、`dflash_config`。`Qwen3Config` 能原样读完整份配置，所以没必要再造一个 config 类——「不重复造轮子」。

---

### 4.2 _run_transformers 中的 from_pretrained 调用

#### 4.2.1 概念说明

4.1 讲的是「类层面怎么获得加载能力」，本讲看「**实际评测脚本里怎么调用**」。`benchmark.py` 的 `_run_transformers` 是 Transformers 后端的评测入口，它把 target 和 draft 两个模型加载、组装好，再喂给 `dflash_generate`。

这里有一个 u1-l4 提过但值得再次强调的点：**target 和 draft 是两种不同的「加载身份」**：

- **target（目标模型）** 是一个完整的大模型，有自己的 `embed_tokens` 和 `lm_head`，用 `AutoModelForCausalLM` 加载。
- **draft（草稿模型）** 是「残缺」的，**没有** `embed_tokens` 和 `lm_head`（生成时向 target 借），所以用 `AutoModel`（README 路径）或直接用 `DFlashDraftModel`（benchmark 路径）加载。

#### 4.2.2 核心流程：_run_transformers 的加载阶段

`_run_transformers` 的前半段是一条清晰的「加载流水线」（伪代码）：

```text
_run_transformers(args)
  ├─ 1. _check_transformers_model(args.model)     # 白名单校验（4.3 讲）
  ├─ 2. 设随机数种子（可复现）
  ├─ 3. _dist_init(torch_dist)                     # 若用 torchrun，初始化 NCCL 进程组
  ├─ 4. torch.cuda.set_device(local_rank)          # 选定本进程的 GPU
  ├─ 5. attn_impl = _get_transformers_attn_impl()  # 选 flash_attention_2 还是 sdpa（4.3 讲）
  ├─ 6. target = AutoModelForCausalLM.from_pretrained(args.model, attn_implementation=attn_impl, dtype=bf16)
  ├─ 7. draft  = DFlashDraftModel.from_pretrained(args.draft_model, attn_implementation=attn_impl, dtype=bf16)
  ├─ 8. tokenizer = AutoTokenizer.from_pretrained(args.model)
  └─ 9. dataset = load_and_process_dataset(args.dataset)   # 之后进入评测循环
```

两个 `from_pretrained` 串起了整条链路，并且**共用同一个 `attn_impl`**——这一点对 4.3 节理解「回退对加速比的影响」很关键。

#### 4.2.3 源码精读

[dflash/benchmark.py:198-228](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L198-L228) 是 `_run_transformers` 的开头，包含校验、设备选择和两条加载链路：

```python
def _run_transformers(args: argparse.Namespace) -> None:
    import torch
    from torch import distributed as torch_dist
    from transformers import AutoModelForCausalLM, AutoTokenizer

    from .model import DFlashDraftModel, dflash_generate

    _check_transformers_model(args.model)              # 白名单校验

    random.seed(0); np.random.seed(0); torch.manual_seed(0)   # 固定种子，保证可复现
    torch.cuda.manual_seed_all(0)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False

    _dist_init(torch_dist)                             # torchrun 多卡时初始化 NCCL
    torch.cuda.set_device(_dist_local_rank())
    device = torch.device(f"cuda:{_dist_local_rank()}")
    attn_impl = _get_transformers_attn_impl()          # 选 attention 后端
```

接着是两条 `from_pretrained` 调用，[dflash/benchmark.py:219-225](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L219-L225)：

```python
    target = AutoModelForCausalLM.from_pretrained(
        args.model, attn_implementation=attn_impl, dtype=torch.bfloat16,
    ).to(device).eval()

    draft_model = DFlashDraftModel.from_pretrained(
        args.draft_model, attn_implementation=attn_impl, dtype=torch.bfloat16,
    ).to(device).eval()
```

对比要点：

| 维度 | target（目标模型） | draft（草稿模型） |
|---|---|---|
| 加载类 | `AutoModelForCausalLM` | `DFlashDraftModel`（benchmark）/ `AutoModel`（README） |
| 有无 lm_head | 有，完整 CausalLM | 无，残缺（借 target 的） |
| `attn_implementation` | 同一个 `attn_impl` | 同一个 `attn_impl` |
| `dtype` | `torch.bfloat16` | `torch.bfloat16` |

两个细节值得留意：

1. **benchmark 用 `DFlashDraftModel.from_pretrained` 直接调用**（从 `from .model import DFlashDraftModel` 导入），路径明确，不依赖 `trust_remote_code`。而 README 的 Quick Start 用 `AutoModel.from_pretrained(..., trust_remote_code=True)`，靠模型仓库里的自定义代码最终实例化同一个 `DFlashDraftModel` 架构。两条路径殊途同归。
2. **`.to(device).eval()`**：加载后搬到本进程的 GPU 并切到推理模式（关闭 dropout 等）。注意 benchmark 用的是 `.to(device)` 而非 `device_map`，所以这里**没有**用到 4.1 讲的 `_no_split_modules`——它是给 README 风格的 `device_map` 路径准备的。

#### 4.2.4 代码实践

**实践目标**：把「加载流水线」和「评测入口」串起来看一遍。

**操作步骤**：

1. 阅读上面的伪代码与源码片段，在笔记里画出 `_run_transformers` 从校验到加载完成的 9 步流程图。
2. 对照 README 的评测命令（[README.md:183-186](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L183-L186)），标注每一步对应源码的哪一行：

   ```bash
   torchrun --nproc_per_node=8 -m dflash.benchmark --backend transformers \
       --model Qwen/Qwen3-8B --draft-model z-lab/Qwen3-8B-DFlash-b16 \
       --dataset gsm8k --max-samples 128
   ```

3. 思考：为什么 `--draft-model` 是 transformers 后端的必填项？（提示：看 `main` 里的校验 [dflash/benchmark.py:507-510](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L507-L510)）

**需要观察的现象**：你应能在源码里精确定位「校验 → 种子 → 分布式初始化 → 选 attn 后端 → 加载 target → 加载 draft → tokenizer → dataset」这条链。

**预期结果**：`--draft-model` 必填，因为 transformers 后端是**库型**实现，必须在本进程里显式加载草稿模型；而 vLLM/SGLang 是服务型，草稿模型在起服务时已通过 `--speculative-config` 传给服务端，评测端只需发 HTTP 请求（见 u1-l2）。

> 这一实践是「源码阅读型」，不依赖 GPU 即可完成。

#### 4.2.5 小练习与答案

**练习 1**：为什么 target 用 `AutoModelForCausalLM` 而 draft 用 `DFlashDraftModel`（或 `AutoModel`）？

> **答案**：target 是完整模型，需要 `lm_head` 来输出 logits（验证时算 token 概率），所以用 `*ForCausalLM`；draft 没有 `lm_head`（向 target 借），它不是一个 CausalLM，用普通 `Model` 加载即可。

**练习 2**：benchmark 里 `attn_impl` 这个变量被几个 `from_pretrained` 共用？这会带来什么后果？

> **答案**：被 target 和 draft 两个 `from_pretrained` 共用。后果是：一旦环境里没有 flash-attn，`attn_impl` 回退成 `"sdpa"`，**target 和 draft 都会用 sdpa**，两个模型的前向都变慢，加速比会被「双重」拉低（详见 4.3）。

**练习 3**：README 的 Transformers 示例给 draft 传了 `device_map="cuda:0"`，而 benchmark 没传 `device_map` 而是用 `.to(device)`。这两种做法的区别是什么？

> **答案**：`device_map` 是在 `from_pretrained` 内部按设备映射放置权重（会用到 `_no_split_modules`）；`.to(device)` 是加载到 CPU 后再整体搬到 GPU。benchmark 走后者，因此不触发 `_no_split_modules` 的切分逻辑。

---

### 4.3 attention 实现选择、回退与模型白名单

#### 4.3.1 概念说明

4.2 里那个 `attn_impl = _get_transformers_attn_impl()` 决定了整条链路用哪种注意力后端。DFlash 对此有**两个硬约束**，对应两个工具函数：

1. **attention 后端的选择与回退**：优先用 `flash_attention_2`（需要安装 `flash-attn`），装不上就回退到 `sdpa` 并打警告。
2. **模型白名单**：Transformers 后端**只支持 Qwen3 系列和 LLaMA-3.1-8B-Instruct**，其它模型直接报错，让你改用 vLLM/SGLang。

这两点合起来，回答了本讲的两个核心问题：**为什么没装 flash-attn 会拉低加速比**，以及**为什么 Transformers 后端支持的模型这么少**。

#### 4.3.2 核心流程

**attention 回退逻辑**（伪代码）：

```text
_get_transformers_attn_impl()
  ├─ try: import flash_attn
  │     成功 → return "flash_attention_2"
  └─ except ImportError:
        logger.warning("... Falling back to torch.sdpa. Speedup will be lower ...")
        return "sdpa"
```

**模型白名单逻辑**（伪代码）：

```text
_check_transformers_model(model_name)
  ├─ 用正则 _TRANSFORMERS_SUPPORTED_PATTERN 匹配 model_name
  ├─ 匹配成功（Qwen3 非 3.5 / LLaMA-3.1-8B-Instruct）→ 放行
  └─ 匹配失败 → raise ValueError，提示改用 --backend sglang 或 --backend vllm
```

**运行时如何用上选中的后端**：4.1 讲过，`from_pretrained(..., attn_implementation=attn_impl)` 会把后端名写进 `config._attn_implementation`。草稿模型的自定义注意力 `Qwen3DFlashAttention` 在 forward 时读这个字段来派发：

[dflash/model.py:239-241](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L239-L241) 根据加载时写入的 `_attn_implementation` 选择执行后端：

```python
attn_fn: Callable = eager_attention_forward
if self.config._attn_implementation != "eager":
    attn_fn = ALL_ATTENTION_FUNCTIONS[self.config._attn_implementation]
```

`ALL_ATTENTION_FUNCTIONS` 是 Transformers 的注意力函数注册表（`"flash_attention_2"` / `"sdpa"` 等名字 → 对应的 fused kernel），它和 `eager_attention_forward` 一起在文件开头从 `transformers.models.qwen3.modeling_qwen3` 导入（[dflash/model.py:14-18](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L14-L18)）。于是「加载时选后端 → config 记录 → 运行时派发」三步串成闭环。

#### 4.3.3 源码精读

[dflash/benchmark.py:173-195](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L173-L195) 是白名单正则、校验函数与 attention 回退函数：

```python
_TRANSFORMERS_SUPPORTED_PATTERN = re.compile(r"qwen3(?!\.5)[\w-]*|llama.*3\.1.*8b.*instruct", re.IGNORECASE)


def _check_transformers_model(model_name: str) -> None:
    if not _TRANSFORMERS_SUPPORTED_PATTERN.search(model_name):
        raise ValueError(
            f"Transformers backend does not support '{model_name}'. "
            f"Only Qwen3 series and LLaMA-3.1-8B-Instruct are supported. "
            f"Use --backend sglang or --backend vllm for other models."
        )


def _get_transformers_attn_impl() -> str:
    try:
        import flash_attn  # noqa: F401
        return "flash_attention_2"
    except ImportError:
        logger.warning(
            "flash_attn not installed. Falling back to torch.sdpa. Speedup will be lower. "
            "For optimal speedup in Transformers backend, please install: "
            "pip install flash-attn --no-build-isolation"
        )
        return "sdpa"
```

逐个拆解这条正则 `qwen3(?!\.5)[\w-]*|llama.*3\.1.*8b.*instruct`（`re.IGNORECASE`，大小写不敏感）：

- **左半支** `qwen3(?!\.5)[\w-]*`：匹配以 `qwen3` 开头、但**紧接着不能是 `.5`**的名字。`(?!\.5)` 是负向先行断言。
  - `Qwen3-8B` → `qwen3` 后是 `-8B`，通过 → ✅ 放行
  - `Qwen3-4B-DFlash-b16` → ✅ 放行（这是草稿，但校验的是 target 名）
  - `Qwen3.5-27B` → `qwen3` 后是 `.5`，断言失败 → ❌ 拒绝
- **右半支** `llama.*3\.1.*8b.*instruct`：匹配形如 LLaMA 3.1 8B Instruct 的名字 → ✅ 放行 `Llama-3.1-8B-Instruct`。

**为什么是这个白名单？** 因为 `dflash/model.py` 这套 Transformers 实现**整套复用了 Qwen3 的组件**（`Qwen3RMSNorm`、`Qwen3RotaryEmbedding`、`Qwen3MLP`、`Qwen3Config`、`Qwen3PreTrainedModel`），草稿架构就是围绕 Qwen3 的层结构设计的。LLaMA-3.1-8B-Instruct 与 Qwen3 结构高度相似（都是 RMSNorm + RoPE + 分组查询注意力），所以同一套草稿架构也能瞄准它。而更新的模型（Qwen3.5、Gemma-4 等）使用了**混合架构**（如 GatedDeltaNet / 类 Mamba 的状态层），这个简洁的 Transformers 参考实现处理不了——它们必须走 vLLM/SGLang 的专用 kernel（MLX 版另有专门的状态回滚机制，见 u3-l3）。换句话说：

> Transformers 后端是一个**参考/调试实现**，只覆盖它原生设计针对的两个模型家族；生产部署全部模型走 vLLM/SGLang。

注意正则故意用 `(?!\.5)` 排除 Qwen3.5，这与「Qwen3.5 是混合架构、需专用后端」的事实完全对应。

**为什么 sdpa 回退会拉低加速比？** 投机解码的收益来自「用闲置算力换更少的串行 target 前向」。草稿模型在 decode 循环的**每一轮**都要跑一次块起草前向（见 u2-l1），所以草稿前向必须尽可能快。DFlash 的注意力形状很特殊：query 长度只有 `block_size`（如 16，很短），key/value 是「上下文 + 块」拼接、且 `is_causal=False`（见 u2-l3）。FlashAttention 是一个**融合 kernel**，不实例化完整的注意力矩阵，对这种短 query、非因果、变长的形状既快又省显存；而 PyTorch 的 `sdpa` 主要面向标准因果自注意力优化，在这条自定义路径上往往更慢。更关键的是——4.2 强调过 `attn_impl` 被 target 和 draft **共用**：没装 flash-attn 时，**验证用的 target 前向和起草用的 draft 前向都变慢**，加速比被两头夹击。源码警告里那句 `Speedup will be lower` 说的正是这件事。

#### 4.3.4 代码实践

**实践目标**：亲眼看到 attention 回退逻辑与模型白名单的运行时行为。这一步**不需要 GPU**，纯 Python 即可。

**操作步骤**：

1. 在**没有安装 `flash-attn`** 的虚拟环境里，直接调用回退函数：

   ```python
   from dflash.benchmark import _get_transformers_attn_impl, _check_transformers_model
   print("attn_impl =", _get_transformers_attn_impl())   # 应返回 "sdpa" 并打印一条 loguru 警告
   ```

2. 用几个模型名测试白名单（**不要**真的去加载，只测正则）：

   ```python
   for name in ["Qwen/Qwen3-8B", "Qwen/Qwen3.5-27B", "meta-llama/Llama-3.1-8B-Instruct",
                "google/gemma-4-26B-A4B-it", "Qwen/Qwen3-Coder-30B-A3B"]:
       try:
           _check_transformers_model(name)
           print(f"{name:40s} -> 放行 ✅")
       except ValueError as e:
           print(f"{name:40s} -> 拒绝 ❌")
   ```

**需要观察的现象**：
- 第 1 步：控制台出现 `flash_attn not installed. Falling back to torch.sdpa. Speedup will be lower.` 警告，函数返回 `"sdpa"`。
- 第 2 步：`Qwen3-8B`、`Llama-3.1-8B-Instruct`、`Qwen3-Coder-30B-A3B` 放行；`Qwen3.5-27B`、`gemma-4-26B-A4B-it` 被拒绝。

**预期结果**：与上面分析一致。若你的环境恰好装了 `flash-attn`，第 1 步会返回 `"flash_attention_2"` 且无警告——这也算正确结果，只是观察不到回退，建议在干净环境里复现回退分支。本实践第 1、2 步在普通 CPU 机器上即可完成；若因依赖缺失无法 import，则**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_get_transformers_attn_impl` 用 `try: import flash_attn` 而不是读某个配置开关？

> **答案**：因为它要探测的是**运行环境里到底装没装 flash-attn**。flash-attn 编译困难、不一定可用，用 try/except 探测是最可靠的「能力检测」方式，比让用户手动配开关更省心。

**练习 2**：白名单正则里 `(?!\.5)` 这个负向断言去掉了哪一类模型？为什么必须去掉？

> **答案**：去掉了 Qwen3.5 系列（名字里 `qwen3` 紧跟 `.5`）。因为 Qwen3.5 是混合架构（含 GatedDeltaNet 等状态层），`dflash/model.py` 这套基于 Qwen3 组件的参考实现无法正确处理，必须走 vLLM/SGLang（或 MLX 的专用状态回滚路径），所以从 Transformers 白名单里排除。

**练习 3**：假设把 `block_size` 调得很大（远超 16），草稿前向在 sdpa 下的相对劣势会变大还是变小？结合 FlashAttention 的特性说明。

> **答案**：会相对变小。FlashAttention 的优势在「不实例化 N×N 注意力矩阵」，当序列（block）越长，朴素实现省下的显存与带宽越多；反之 block 越小，朴素 sdpa 的开销相对越接近，FlashAttention 的相对优势越不明显。但 DFlash 的 block 通常不大（如 16），且 target 验证也受影响，所以即便如此，没装 flash-attn 仍会明显拉低整体加速比。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「**加载即用**」端到端小任务。

**任务背景**：你要向同事解释「为什么在没有 flash-attn 的机器上，用 Transformers 后端跑 DFlash 会比预期慢，以及为什么不能用它跑 Qwen3.5」。

**操作步骤**：

1. **复现回退**（CPU 机器即可）：在一个未装 `flash-attn` 的环境里运行
   ```python
   from dflash.benchmark import _get_transformers_attn_impl, _check_transformers_model
   print(_get_transformers_attn_impl())
   ```
   记录控制台的 warning 原文。

2. **跑一次真实生成**（需 GPU + 已装 `.[transformers]`）：按 README 的 Transformers Quick Start（[README.md:130-141](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L130-L141)）加载 `Qwen3-8B` 和 `z-lab/Qwen3-8B-DFlash-b16`，跑一次 `draft.spec_generate(...)`。若你用 benchmark 命令（[README.md:183-186](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L183-L186)），会直接看到第 1 步那条 warning。

3. **对比加速比**（可选，较重）：分别在「装了 flash-attn」和「没装」两个环境跑同一条 benchmark 命令，比较 `Decoding speedup`（该指标由 `_print_decode_summary` 计算，详见 u3-l5）。预期没装时数字明显更小。

4. **解释两件事**（写成两三句话）：
   - sdpa 回退为何降低加速比？（提示：草稿每轮都前向 + target/draft 共用同一 attn_impl）
   - Transformers 后端为何只支持 Qwen3 与 LLaMA-3.1-8B-Instruct？（提示：`dflash/model.py` 复用 Qwen3 组件；更新模型是混合架构）

**预期产出**：一份包含 warning 截图/原文、加速比对比、两句解释的简短笔记。

**若无法运行**：第 2、3 步需 GPU 与模型下载，**待本地验证**；第 1、4 步在任意机器上即可完成，是本实践的最小可执行版本。

## 6. 本讲小结

- `DFlashDraftModel` 继承 `Qwen3PreTrainedModel`，**白嫖**了 `from_pretrained` / `save_pretrained` / `post_init` 等一整套加载基础设施；`__init__` 末尾必须调用 `post_init()`。
- `config_class = Qwen3Config` 让 DFlash 复用 Qwen3 的配置解析；`_no_split_modules = ["Qwen3DFlashDecoderLayer"]` 声明 `device_map` 切分时的原子单元，使 README 风格的 `device_map` 加载成为可能。
- `_run_transformers` 用 `AutoModelForCausalLM.from_pretrained` 加载完整 target，用 `DFlashDraftModel.from_pretrained` 加载残缺 draft，**两者共用同一个 `attn_impl`**。
- 加载时传入的 `attn_implementation` 写进 `config._attn_implementation`，运行时由 `Qwen3DFlashAttention` 通过 `ALL_ATTENTION_FUNCTIONS` 注册表派发——这是「加载选后端、运行用后端」的闭环。
- `_get_transformers_attn_impl` 优先 `flash_attention_2`，装不上回退 `sdpa` 并警告；因草稿每轮前向且 target/draft 共用后端，回退会**双重拉低加速比**。
- `_check_transformers_model` 用正则白名单只放行 Qwen3（非 3.5）与 LLaMA-3.1-8B-Instruct，因为这套实现基于 Qwen3 组件，无法处理 Qwen3.5/Gemma-4 等混合架构——后者走 vLLM/SGLang。

## 7. 下一步学习建议

到这里，**Transformers 参考实现**这条线（u2 全单元）已经讲完：从全局控制流（u2-l1）到草稿架构（u2-l2）、块扩散注意力（u2-l3）、验证接受循环（u2-l4），再到本讲的加载与集成。接下来建议：

- **进入 MLX 实现**：读 **u3-l1（MLX 草稿模型与配置）**，对照本讲的 Transformers 版，看 MLX 版如何用 `DFlashConfig` 数据类、`bind()` 复用 target 的 embed/lm_head、以及 `make_cache()` 按层类型分发 KVCache。MLX 版是**完全独立**的另一套实现，能加深你对「同一算法、两种工程表达」的理解。
- **看评测如何度量加速比**：本讲多次提到 `_print_decode_summary` 计算的 `Decoding speedup`，它的精确算法在 **u3-l5（多后端评测运行器与指标）**。
- **想理解混合模型为何被白名单排除**：直接跳 **u3-l3（GDN 状态捕获与回滚）**，看 MLX 版如何为 Qwen3.5 这类 GatedDeltaNet 模型专门做状态回滚——你会立刻明白 Transformers 参考实现为什么处理不了。
