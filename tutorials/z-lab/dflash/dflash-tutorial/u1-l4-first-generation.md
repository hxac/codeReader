# 动手跑通第一次生成

## 1. 本讲目标

前面三讲我们分别认识了 DFlash 是什么（u1-l1）、怎么按后端安装隔离（u1-l2）、包是怎么导出 API 的（u1-l3）。但还差最关键的一步：**真正让 DFlash 跑起来，亲眼看到加速生成输出一行结果**。本讲就来补上这一步。

学完本讲，你应当能够：

- 把 README 里的 **Transformers 后端**示例在本地（或 GPU 机器上）跑通，并打印出第一段 DFlash 加速生成的文本。
- 在 **Apple 芯片**机器上用 MLX 后端跑通等价的流式生成示例。
- 看懂草稿模型对外的主入口 [`spec_generate`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L350-L357) 的五个参数：`target`、`input_ids`、`max_new_tokens`、`stop_token_ids`、`temperature`。
- 从直觉上理解 **target 与 draft 在代码层面如何配合**——尤其是「草稿模型没有自己的 embed 和 lm_head，而是复用 target 的」这一关键设计。
- 通过修改 `max_new_tokens` 与 `temperature`，观察生成结果与速度的变化。

> 本讲是「入门单元」的收尾：不再讲新机制，而是把已建立的概念落到一次可运行的实践中。算法细节（块扩散注意力、接受长度计算、KV cache 裁剪）留给第二单元，本讲只看「怎么调用、参数什么含义、跑起来什么样」。

## 2. 前置知识

继续之前，确认你理解下面几点（不熟的下面会再点一下）：

- **投机解码的角色分工**（回顾 u1-l1）：一个**快而小的草稿模型（draft）**负责一次「起草」一整块候选 token，一个**大而准的目标模型（target）**负责一次性并行「验证」这些候选，验证通过的 token 就被接受。平均每步能接受多少个 token（记作接受长度 \(a\)）决定了加速效果，单步产出 token 数 = 接受长度 \(a + 1\)。
- **四种后端的定位**（回顾 u1-l2）：vLLM / SGLang 是**服务型**（起 HTTP 服务，OpenAI 兼容接口）；**Transformers 与 MLX 是库型**（在 Python 里直接 `import` 调用）。本讲的两个示例都属于库型——Transformers 需要 GPU，MLX 需要 Apple 芯片。
- **顶层 API 怎么来**（回顾 u1-l3）：`import dflash` 是懒加载的，`dflash.DFlashDraftModel` 在首次访问时才从 `dflash.model` 取出。本讲的 Transformers 示例其实绕过了顶层门面，直接用 `transformers.AutoModel` 加载草稿模型；MLX 示例则显式 `from dflash.model_mlx import ...`。
- **几个 Transformers 基础术语**：`AutoModel.from_pretrained(...)` 按 HF 仓库里的 `config.json` 自动实例化模型并下载权重；`apply_chat_template` 把对话消息渲染成模型能吃的 token 序列；`tokenizer.decode(...)` 把 token id 还原回文本。

如果你机器上暂时没有 GPU 或 Apple 芯片，本讲仍提供了「源码阅读型实践」，让你不运行也能吃透调用链。

## 3. 本讲源码地图

本讲只涉及两个真实文件，且只取其中与「第一次生成」直接相关的片段：

| 文件 | 行数级别 | 本讲关注点 |
| --- | --- | --- |
| [README.md](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md) | 约 210 行 | Transformers Quick Start（L126-L142）与 MLX Quick Start（L144-L161）两段可运行示例 |
| [dflash/model.py](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py) | 约 367 行 | `DFlashDraftModel.spec_generate`（L349-L366）、它委托的 `dflash_generate`（L62-L169）、以及体现 target/draft 协作的几行关键代码 |

> 说明：`dflash_generate` 的内部算法（三阶段控制流、注意力、接受长度、KV cache 裁剪）是第二单元（u2）的主题，本讲只把它当作「`spec_generate` 背后真正干活的函数」来定位，不展开推导。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. Transformers Quick Start 示例逐行解读（库型的主入口）
2. `spec_generate` 方法签名：草稿模型对外的主入口
3. target 与 draft 在代码层面如何协作（直觉版）
4. MLX Quick Start 示例：流式生成入口

### 4.1 Transformers Quick Start 示例逐行解读

#### 4.1.1 概念说明

Transformers 后端是读源码、做调试最方便的库型后端，但 README 明确指出它**只支持 Qwen3 和 LLaMA-3.1 模型**（[README.md:128](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L128)）。原因是这个后端的实现 `dflash/model.py` 直接复用了 Transformers 里的 Qwen3 组件（回顾 u1-l3 提到的 `from transformers.models.qwen3.modeling_qwen3 import ...`），并额外适配了 LLaMA-3.1。

官方示例的目标是：**用最小的代码，加载一对「目标 + 草稿」模型，跑通一次块扩散投机解码生成。** 我们要逐行看懂它每一行在干什么、为什么这么写。

#### 4.1.2 核心流程

Transformers 示例的整体流程可以分成五步：

```text
1. 准备三个对象
   ├─ draft  = AutoModel.from_pretrained(草稿仓库)   # 注意是 AutoModel
   ├─ target = AutoModelForCausalLM.from_pretrained(目标仓库)
   └─ tokenizer
2. 把对话消息渲染成 input_ids（apply_chat_template）
3. 调 draft.spec_generate(input_ids, target=target, ...)  ← 草稿发起，内部驱动 target 验证
4. 拿到 output（一整段 token id，含 prompt）
5. tokenizer.decode(output[0]) → 打印「prompt + 生成内容」
```

最反直觉的一点是第 3 步：**生成是「草稿」发起的**（`draft.spec_generate(...)`），但调用时要把「目标」作为参数传进去（`target=target`）。这与「目标模型验证、草稿模型起草」的分工一致——草稿是这次投机解码的「指挥」，它在内部按需驱动 target 做验证。第 4.3 节会从源码证实这一点。

#### 4.1.3 源码精读

完整示例见 [README.md:130-142](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L130-L142)。逐行拆解：

**第 1 步：加载草稿模型**（[README.md:133](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L133)）

```python
draft = AutoModel.from_pretrained("z-lab/Qwen3-8B-DFlash-b16", trust_remote_code=True, dtype="auto", device_map="cuda:0").eval()
```

四个细节值得记住：

| 细节 | 含义 | 为什么 |
| --- | --- | --- |
| `AutoModel`（而非 `AutoModelForCausalLM`） | 只加载到 `DFlashDraftModel` 这一层 | 草稿模型**没有自己的 `lm_head`**，它要复用 target 的 lm_head 来出 logits（见 4.3），用 `AutoModelForCausalLM` 反而会期待一个不存在的 lm_head |
| `trust_remote_code=True` | 允许执行草稿仓库里的自定义建模代码 | `DFlashDraftModel` 是 dflash 自定义类，不在官方 Transformers 里，必须开远程代码信任 |
| `dtype="auto"`、`device_map="cuda:0"` | 自动选精度、放到 0 号 GPU | 草稿与 target **必须在同一张 GPU**，否则「复用 lm_head/embed」无从谈起 |
| `.eval()` | 切到推理模式 | 关闭 dropout 等；`spec_generate` 内部还会再 `self.eval()` 一次并包 `@torch.inference_mode()`（见 4.2） |

仓库名 `Qwen3-8B-DFlash-b16` 里的 **`b16`** 暗示了它的 `block_size = 16`（一次起草 16 个 token 的一块）。这个 `block_size` 不在 `spec_generate` 的参数里，而是写在模型的 `config.json` 中——4.2 节会回到这一点。

**第 2 步：加载目标模型与 tokenizer**（[README.md:134-L135](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L134-L135)）

```python
target = AutoModelForCausalLM.from_pretrained("Qwen/Qwen3-8B", dtype="auto", device_map="cuda:0").eval()
tokenizer = AutoTokenizer.from_pretrained("Qwen/Qwen3-8B")
```

- target 用的是标准的 `AutoModelForCausalLM`——它**有** `lm_head` 和 `model.embed_tokens`，正是这两样会被草稿「借」去用。
- tokenizer 从**目标模型**的仓库加载。草稿与目标共享同一套词表（它们处理的是同一种语言），所以一份 tokenizer 就够。

**第 3 步：渲染 input_ids**（[README.md:137-L138](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L137-L138)）

```python
messages = [{"role": "user", "content": "How many positive whole-number divisors does 196 have?"}]
input_ids = tokenizer.apply_chat_template(messages, return_tensors="pt", add_generation_prompt=True, enable_thinking=False).to(draft.device)
```

- `add_generation_prompt=True`：在末尾加上「助手回合」的开头标记，告诉模型「现在轮到你回答了」。
- `enable_thinking=False`：关掉 Qwen3 的思考模式，直接给最终答案（MLX 示例里恰好相反，用的是 `True`，见 4.4）。
- `return_tensors="pt"`：返回 PyTorch 张量；`.to(draft.device)` 确保它与模型在同一张 GPU。

**第 4 步：发起生成**（[README.md:140](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L140)）

```python
output = draft.spec_generate(input_ids=input_ids, max_new_tokens=2048, temperature=0.0, target=target, stop_token_ids=[tokenizer.eos_token_id])
```

这就是本讲的「主角调用」。五个参数的含义先记个大概，4.2 会逐一对照源码：`target`（目标模型）、`input_ids`（输入）、`max_new_tokens=2048`（最多新生成 2048 个 token）、`temperature=0.0`（贪心解码）、`stop_token_ids=[eos]`（遇到结束符就停）。注意：**这里没有传 `block_size`**——它由草稿模型的配置决定。

**第 5 步：解码打印**（[README.md:141](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L141)）

```python
print(tokenizer.decode(output[0], skip_special_tokens=False))
```

一个容易踩坑的点：`output` 里**包含原始 prompt**（`dflash_generate` 把输入 token 放在输出序列开头，见 [model.py:96](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L96)）。所以 `decode(output[0])` 打印的是「渲染后的 prompt + 模型回答」。如果你只想要回答，可以切片：`output[0, input_ids.shape[1]:]` 再 decode。

#### 4.1.4 代码实践

**运行型实践（需要 GPU）**：把 README 示例原样跑通。

1. 实践目标：在一台有 GPU 的机器上，第一次亲眼看到 DFlash 加速生成的输出。
2. 操作步骤：
   - 按 u1-l2 的隔离原则，建一个干净的虚拟环境并安装 Transformers 后端：
     ```bash
     uv pip install -e ".[transformers]"
     ```
   - 把 [README.md:130-142](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L130-L142) 的代码存成 `run_dflash.py` 并 `python run_dflash.py`。
3. 需要观察的现象：首次运行会从 HuggingFace 下载 `Qwen/Qwen3-8B`（约 16GB）与 `z-lab/Qwen3-8B-DFlash-b16`（体量小得多）；随后打印出 prompt 与对 `196 的因子个数` 的回答（正确答案是 9）。
4. 预期结果：终端输出包含「How many positive whole-number divisors does 196 have?」以及模型的解答。显存需能同时容纳 8B 的 target 与更小的 draft（典型 16GB+ 显存）。**待本地验证**——以你机器上的真实输出与显存为准。

> 没有 GPU？跳到本模块末尾的「源码阅读型实践」，或直接看 4.4 的 MLX 示例（若你有 Apple 芯片）。

#### 4.1.5 小练习与答案

**练习 1**：为什么加载草稿用 `AutoModel`，而加载 target 用 `AutoModelForCausalLM`？

> 参考答案：`AutoModelForCausalLM` 会实例化一个**带 `lm_head`** 的因果语言模型。但 `DFlashDraftModel` 故意**没有** `lm_head`（也没有 `embed_tokens`），它要复用 target 的这两层（见 4.3 的源码佐证）。若用 `AutoModelForCausalLM` 加载草稿，Transformers 会期待一个不存在的 lm_head 权重而出错。target 则是标准因果 LM，必须用 `AutoModelForCausalLM` 才能拿到 `lm_head`。

**练习 2**：把 `enable_thinking=False` 改成 `True`，生成的「形状」会发生什么变化？

> 参考答案：Qwen3 的思考模式会在给出最终答案前，先输出一段 `<think>...</think>` 的推理过程。改成 `True` 后，前若干百个 token 会是思考内容，最终答案出现在思考块之后；在 `max_new_tokens` 固定的情况下，留给「正式回答」的预算会变少。这也是为什么本练习里需要相对较大的 `max_new_tokens=2048`。

### 4.2 `spec_generate` 方法签名：草稿模型对外的主入口

#### 4.2.1 概念说明

上一节我们看到，Transformers 后端「发起一次生成」的全部入口就是这一行：

```python
draft.spec_generate(input_ids=..., max_new_tokens=..., temperature=..., target=..., stop_token_ids=...)
```

也就是说，**`DFlashDraftModel.spec_generate` 是草稿模型对外承诺的、最简洁的生成 API**。本模块的目标是把这个方法签名上的每一个参数对上源码，并弄清「哪些能调、哪些不能调」。

一个容易忽略的事实：`spec_generate` 只是**一层很薄的封装**。它真正的工作由同文件里的自由函数 `dflash_generate` 完成。`spec_generate` 故意只暴露 5 个最常用参数，把 `block_size`、`mask_token_id`、`return_stats` 等「进阶旋钮」藏起来了。这是「简单入口 + 完整内核」的常见分层。

#### 4.2.2 核心流程

`spec_generate` 做的事极少，可以画成两步：

```text
draft.spec_generate(target, input_ids, max_new_tokens, stop_token_ids, temperature)
        │
        ├─ 1) self.eval()                         # 确保推理模式
        └─ 2) return dflash_generate(self, ...)   # 把活儿全交给内核函数
                     │
                     └─ 真正的 prefill → 块起草 → 验证 → 接受 循环都在这里（u2 详解）
```

要点是：`spec_generate` 把**自己**（`self`，即草稿模型）作为第一个位置参数 `model` 传给 `dflash_generate`。也就是说，`dflash_generate(model, target, ...)` 同时拿到了「草稿」和「目标」两个模型——这与 4.1 里「草稿发起、内部驱动 target」的直觉完全对应。

#### 4.2.3 源码精读

完整方法见 [`model.py:349-366`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L349-L366)：

```python
@torch.inference_mode()
def spec_generate(
    self,
    target: nn.Module,
    input_ids: torch.LongTensor,
    max_new_tokens: int,
    stop_token_ids: list[int],
    temperature: float,
):
    self.eval()
    return dflash_generate(
        self,
        target=target,
        input_ids=input_ids,
        max_new_tokens=max_new_tokens,
        stop_token_ids=stop_token_ids,
        temperature=temperature,
    )
```

逐项对照 README 调用，把五个参数对号入座：

| 参数 | README 示例里的值 | 作用 |
| --- | --- | --- |
| `target` | `target`（Qwen3-8B 因果 LM） | 目标模型，负责验证；它的 `lm_head`/`embed_tokens` 会被草稿复用 |
| `input_ids` | `apply_chat_template` 的结果 | 输入 token 序列（含渲染后的 prompt） |
| `max_new_tokens` | `2048` | 最多新生成多少个 token（不含 prompt） |
| `stop_token_ids` | `[tokenizer.eos_token_id]` | 命中其中任意一个 id 就提前停止生成 |
| `temperature` | `0.0` | 采样温度；`< 1e-5` 走贪心 `argmax`，否则走温度采样（见 [`sample`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L48-L54)） |

两个常被忽略的点：

1. **签名里没有 `block_size` / `mask_token_id`**。它们在 `dflash_generate` 的签名里是 `Optional` 参数（[model.py:70-71](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L70-L71)），当传 `None` 时，函数内部回退到 `model.block_size` 和 `model.mask_token_id`（[model.py:76-L77](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L76-L77)）。而这两个属性来自草稿模型的 `config`：在 [`__init__`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L319-L320) 里 `self.block_size = config.block_size`、`self.mask_token_id = config.dflash_config.get("mask_token_id", None)`。**所以「一次起草多少个 token」由草稿模型的 config.json 决定，不在调用方手里**——`Qwen3-8B-DFlash-b16` 的 `b16` 就是这么来的。
2. **签名里也没有 `return_stats`**。`spec_generate` 调 `dflash_generate` 时没传它，因此恒为 `False`，只返回 `output_ids`（[model.py:157-L158](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L157-L158)）。也就是说，**通过 `spec_generate` 拿不到首 token 时间、每 token 时间、接受长度分布等统计**。想看这些指标，需要绕过 `spec_generate`，直接调 `dflash_generate(..., return_stats=True)`（见本模块实践的「进阶」项），或用第三单元的 benchmark 模块。

另外注意 `@torch.inference_mode()` 装饰器（[model.py:349](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L349)）：它让整个生成过程不记录梯度、不占 Autograd 显存，是推理时的标准做法；`dflash_generate` 自己也带了同样的装饰器（[model.py:62](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L62)），属于双重保险。

#### 4.2.4 代码实践

**源码阅读型实践**（无需 GPU）：核对「签名藏起的参数来自哪里」。

1. 实践目标：亲手验证 `block_size` 不在 `spec_generate` 手里，而是来自草稿模型的 config。
2. 操作步骤：在装好 Transformers 后端的环境里：
   ```python
   from transformers import AutoModel
   draft = AutoModel.from_pretrained("z-lab/Qwen3-8B-DFlash-b16", trust_remote_code=True)
   print("block_size =", draft.block_size)          # 期望 16（对应名字里的 b16）
   print("mask_token_id =", draft.mask_token_id)    # 来自 config.dflash_config
   print("target_layer_ids =", draft.target_layer_ids)
   print("spec_generate params =", list(draft.spec_generate.__code__.co_varnames[:draft.spec_generate.__code__.co_argcount]))
   ```
3. 需要观察的现象：`block_size` 打印为 `16`；最后一行打印出的形参里**没有** `block_size`、`mask_token_id`、`return_stats`。
4. 预期结果：坐实「这些旋钮藏在 config / 内核函数里，`spec_generate` 不暴露」。需要能联网下载草稿权重；**待本地验证**。

> 进阶（可选）：若想看加速统计，绕过 `spec_generate` 直接调内核——
> ```python
> from dflash.model import dflash_generate
> stats = dflash_generate(draft, target=target, input_ids=input_ids,
>                         max_new_tokens=256, stop_token_ids=[tokenizer.eos_token_id],
>                         temperature=0.0, return_stats=True)
> print("time_to_first_token =", stats.time_to_first_token)
> print("time_per_output_token =", stats.time_per_output_token)
> print("acceptance_lengths =", stats.acceptance_lengths)
> ```
> 此时返回的是 `SimpleNamespace`（见 [model.py:162-L169](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L162-L169)），含计时与每步接受长度。这正好为「修改 temperature 观察速度」提供了一个客观抓手。

#### 4.2.5 小练习与答案

**练习 1**：README 示例里没有出现 `block_size`，那一次起草多少个 token 是谁决定的？

> 参考答案：由草稿模型仓库 `z-lab/Qwen3-8B-DFlash-b16` 的 `config.json` 里的 `block_size` 字段决定（值为 16，对应名字里的 `b16`）。`dflash_generate` 在 `block_size=None` 时回退到 `model.block_size`（[model.py:76](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L76)），而 `spec_generate` 根本没把这个参数透传出去。如果想临时改起草规模，只能直接调 `dflash_generate(..., block_size=N)`。

**练习 2**：为什么通过 `spec_generate` 拿不到「每 token 生成时间」？

> 参考答案：因为 `spec_generate` 调用 `dflash_generate` 时没有传 `return_stats`，它默认为 `False`，函数走 [model.py:157-L158](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L157-L158) 的分支，只返回 `output_ids`。统计信息（首 token 时间、每 token 时间、接受长度列表）只在 `return_stats=True` 时打包成 `SimpleNamespace` 返回（[model.py:162-L169](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L162-L169)）。`spec_generate` 是「极简入口」，刻意不暴露这些。

### 4.3 target 与 draft 在代码层面如何协作（直觉版）

#### 4.3.1 概念说明

u1-l1 已经讲过投机解码的分工直觉（草稿起草、目标验证）。本模块的目标是从**源码**看这对分工是怎么落实的，重点是一个贯穿全篇的设计：

> **草稿模型是「残缺」的——它没有自己的 `embed_tokens` 和 `lm_head`，而是在生成时直接借用 target 的这两层。**

这解释了前两节的几个「为什么」：为什么草稿用 `AutoModel` 加载、为什么草稿和 target 必须在同一张 GPU、为什么 README 称它「lightweight（轻量）」。本模块只点到为止，把内部三阶段流程当作「黑盒地图」记住即可，真正的算法拆解放到 u2。

#### 4.3.2 核心流程

把 `dflash_generate` 的主循环抽象成三阶段（具体实现见 u2-l1/u2-l3/u2-l4）：

```text
① prefill（预填）
   target 一次性吃下 input_ids → 产出「第一个 token」+ 「多层隐藏状态」
   （隐藏状态会被抽出来当作草稿的「上下文/context」）

② 块起草（draft 一块）
   草稿把一整块「掩码/noise 位置」并行去噪 → 借 target.lm_head 出 logits → 采样得到一块候选 token

③ 验证 + 接受（target 验证）
   target 一次性并行验证这一块候选 → 用 cumprod 算出连续接受了多少个 → 写回输出、裁剪 KV cache
   回到 ②，直到达到 max_new_tokens 或命中 stop_token_ids
```

记住两个「借」的动作发生在第 ② 阶段：草稿**借用 `target.model.embed_tokens`** 把候选 token id 变成输入向量，再**借用 `target.lm_head`** 把自己的隐藏状态变成 logits。这两个「借」就是 target/draft 协作的代码落点。

#### 4.3.3 源码精读

**先看草稿模型「缺什么」**：[`DFlashDraftModel.__init__`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L306-L321) 里定义的成员只有 `layers`（解码层）、`norm`、`rotary_emb`、`fc`（投影）、`hidden_norm`，外加配置派生的 `target_layer_ids`/`block_size`/`mask_token_id`。**没有 `embed_tokens`，也没有 `lm_head`**。这就是它能被 `AutoModel` 加载、却不能被 `AutoModelForCausalLM` 加载的根本原因。

**再看草稿「借什么」**：在 `dflash_generate` 的块起草段（[model.py:110-L121](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L110-L121)）能看到两处直接调用 target 的子模块：

```python
noise_embedding = target.model.embed_tokens(block_output_ids)   # ← 借 target 的 embedding
draft_logits = target.lm_head(model(                            # ← 借 target 的 lm_head
    target_hidden=target_hidden,
    noise_embedding=noise_embedding,
    ...
)[:, 1 - block_size :, :])
```

- 第 1 行（[model.py:111](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L111)）：把这一块的候选 token id 通过 **target 的 embedding 层**变成输入向量 `noise_embedding`（「噪声/掩码」嵌入由此得名）。
- 第 2-6 行（[model.py:112-L119](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L112-L119)）：把 `noise_embedding` 与从 target 抽取的 `target_hidden`（上下文）一起喂给草稿 `model(...)`，得到草稿的隐藏状态；再用 **target 的 `lm_head`** 把它投影成 logits。

**最后看 target 自己怎么参与**：第 ① 阶段的 prefill（[model.py:87-L99](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L87-L99)）和第 ③ 阶段的验证（[model.py:126-L132](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L126-L132)）都是对 `target(...)` 的直接调用。target 既出第一个 token、出上下文隐藏状态，又出验证用的 logits。

把这三处连起来，你就看清了「target/draft 协作」的真实代码形态：**target 提供 embedding + lm_head + 验证能力，draft 提供块式去噪的解码层与投影**。这也是 DFlash 称得上「轻量草稿」的底气——它不需要自备两端的「头」。

> 注意：`block_output_ids` 一开始是用 `mask_token_id` 填充的（见 [model.py:79-L81](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L79-L81)），这正是「块扩散」名称的来源——草稿对一整块被掩码的位置并行去噪。具体注意力如何把 `target_hidden` 当作 context、把 `noise` 当作查询，是 u2-l3 的主题，本讲不展开。

#### 4.3.4 代码实践

**源码阅读型实践**：用 `grep` 思路定位「两个借」的调用点，建立心智锚。

1. 实践目标：不看本讲，自己从源码里找出「草稿借用 target 的 embed_tokens 和 lm_head」的两行。
2. 操作步骤：在仓库根目录用内容搜索工具查 `target.lm_head` 与 `target.model.embed_tokens`，定位到 `dflash/model.py` 的对应行；再打开 [`__init__`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L306-L321) 确认草稿自身确实没有这两层。
3. 需要观察的现象：两处调用都集中在 `dflash_generate` 的块起草段（约 L111、L112）；草稿的 `__init__` 里搜不到 `embed_tokens` 或 `lm_head` 的赋值。
4. 预期结果：佐证「草稿无头、借 target 的头」这一设计。命令本身只读源码，不依赖环境，可放心运行。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果把 target 放在 `cuda:0`、draft 放在 `cuda:1`，会发生什么？

> 参考答案：会出错（或极度低效）。因为草稿在块起草时要**直接调用** `target.model.embed_tokens(...)` 和 `target.lm_head(...)`（[model.py:111-L112](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L111-L112)）：输入张量在 draft 这边、层在 target 那边，跨设备调用会触发张量设备不匹配的错误。这就是 README 示例里两者都用 `device_map="cuda:0"`、且强调「同卡」的原因。

**练习 2**：为什么说草稿模型是「轻量」的？结合 `__init__` 说出一条具体理由。

> 参考答案：因为 `DFlashDraftModel.__init__`（[model.py:306-L321](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L306-L321)）里**没有** `embed_tokens`（词表 embedding，参数量正比于词表大小 × 隐藏维度，对 8B 级模型是上亿参数）和 `lm_head`（同理）。这两个「大头」都借 target 的，草稿只保留解码层、一个 `fc` 投影和归一化层，因此参数量与显存占用远小于一个完整的因果 LM。

### 4.4 MLX Quick Start 示例：流式生成入口

#### 4.4.1 概念说明

如果你在 Apple 芯片（M 系列）上，就用 MLX 后端。它和 Transformers 后端同样是「库型」（直接 `import` 调用），但 API 风格差别很大：

- Transformers 后端是**面向对象**的：加载得到 `draft` 对象，调用 `draft.spec_generate(...)`。
- MLX 后端是**函数式 + 流式**的：加载用 `load` / `load_draft`，生成用自由函数 `stream_generate`，且**逐块产出**文本。

README 特别说明这个 MLX 实现是「简单而高效」的一个，在 Apple M5 Pro 上用 Qwen3、Qwen3.5、Gemma-4 测试过（[README.md:146](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L146)）。本模块只教你怎么把它跑起来；MLX 实现内部（滑动窗口、钩子捕获 target 隐藏状态、缓存回滚、混合模型 GDN 状态）是第三单元（u3）的主题。

#### 4.4.2 核心流程

MLX 示例的流程与 Transformers 版对应，但形态不同：

```text
1. model, tokenizer = load(目标仓库)      # 函数式加载，返回模型与 tokenizer
2. draft = load_draft(草稿仓库)
3. prompt = tokenizer.apply_chat_template(..., tokenize=False)   # 注意：返回字符串
4. for r in stream_generate(model, draft, tokenizer, prompt,
                            block_size=16, max_tokens=2048, temperature=0.6):
        print(r.text, end="")             # 一边生成一边打印（流式）
        tps = r.generation_tps            # 实时吞吐
5. 打印最终 throughput
```

最大的差别在三点：(a) 用自由函数而非方法；(b) `tokenize=False` 直接拿字符串 prompt；(c) **`block_size`、`max_tokens`、`temperature` 作为 `stream_generate` 的参数显式传入**——不像 Transformers 那样把 `block_size` 藏进 config。

#### 4.4.3 源码精读

完整示例见 [README.md:148-161](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L148-L161)。逐段看：

**导入与加载**（[README.md:149-L152](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L149-L152)）

```python
from dflash.model_mlx import load, load_draft, stream_generate
model, tokenizer = load("Qwen/Qwen3.5-4B")
draft = load_draft("z-lab/Qwen3.5-4B-DFlash")
```

注意这里是 `from dflash.model_mlx import ...`——**显式导入子模块**，而不是走顶层 `dflash.xxx`。回顾 u1-l3：MLX 的符号**不在** `__all__` 里，顶层懒加载分发表也不认识它们，所以必须直接从 `dflash.model_mlx` 取。三个函数：`load`（加载 target + tokenizer）、`load_draft`（加载草稿）、`stream_generate`（流式生成）。

**渲染 prompt**（[README.md:154-L155](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L154-L155)）

```python
messages = [{"role": "user", "content": "How many positive whole-number divisors does 196 have?"}]
prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True, enable_thinking=True)
```

- `tokenize=False`：返回**字符串**而非 token id——MLX 版的 `stream_generate` 内部自己负责分词。
- `enable_thinking=True`：与 Transformers 示例的 `False` 形成对照；这里开启思考模式。

**流式生成**（[README.md:157-L160](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L157-L160)）

```python
tps = 0.0
for r in stream_generate(model, draft, tokenizer, prompt, block_size=16, max_tokens=2048, temperature=0.6):
    print(r.text, end="", flush=True)
    tps = r.generation_tps
print(f"\nThroughput: {tps:.2f} tok/s")
```

几个对照 Transformers 后端的要点：

| 维度 | Transformers（`spec_generate`） | MLX（`stream_generate`） |
| --- | --- | --- |
| 调用形态 | `draft.spec_generate(...)`（方法） | `stream_generate(model, draft, ...)`（自由函数） |
| 输出形态 | 一次性返回完整 `output_ids` | 逐块产出 `r`（流式），`r.text` 是增量文本 |
| `block_size` 在哪 | 藏在草稿 config | 显式作参数传入（这里是 `16`） |
| `temperature` 默认风格 | 示例用 `0.0`（贪心） | 示例用 `0.6`（温度采样，更有多样性） |
| 吞吐指标 | `spec_generate` 不返回（需绕到 `dflash_generate(..., return_stats=True)`） | 直接从 `r.generation_tps` 实时拿到 |

可以看到：**MLX 的 `stream_generate` 把 `block_size` 暴露成了调用方参数**，这正好补上了 4.2 里「Transformers 的 `spec_generate` 不能调 block_size」的遗憾。`r` 是每次迭代返回的响应对象，`r.text` 是当前块的文本、`r.generation_tps` 是实时吞吐（tok/s）；循环结束后最后一次的 `tps` 即整段吞吐。

#### 4.4.4 代码实践

**运行型实践（需要 Apple 芯片 Mac）**：跑通 MLX 流式示例。

1. 实践目标：在 Apple 芯片上第一次看到 DFlash **逐块流式**吐字。
2. 操作步骤：
   - 新建虚拟环境，按 u1-l2 安装 MLX 后端（MLX 分组精确锁版本）：
     ```bash
     pip install -e ".[mlx]"
     ```
   - 把 [README.md:148-161](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L148-L161) 存成 `run_dflash_mlx.py` 并运行。
3. 需要观察的现象：终端**逐字/逐块**打印出回答（因为 `print(r.text, end="", flush=True)`），最后单独打印一行 `Throughput: ... tok/s`。
4. 预期结果：能看到流畅的流式输出与一个正数的吞吐。`Qwen3.5-4B` 较小，适合在 16GB 统一内存的 Mac 上尝试。**待本地验证**——以你机器上的真实吞吐为准。

#### 4.4.5 小练习与答案

**练习 1**：MLX 示例为什么要写 `from dflash.model_mlx import ...`，而不是 `from dflash import stream_generate`？

> 参考答案：因为 MLX 的符号不在顶层公开 API 里。回顾 u1-l3：`__all__` 只有 `DFlashDraftModel`/`extract_context_feature`/`sample`/`load_and_process_dataset` 四个（且都来自 `dflash.model` 或 `dflash.benchmark`），`__getattr__` 分发表也只认这四个。`from dflash import stream_generate` 会落到兜底的 `raise AttributeError`。所以必须直接从子模块 `dflash.model_mlx` 导入。

**练习 2**：MLX 示例把 `block_size=16` 当参数传，而 Transformers 示例里完全不出现 `block_size`。这说明两个后端在哪一层抽象上有差异？

> 参考答案：说明两者对「起草规模」的归属不同。Transformers 版把它当作**模型固有属性**（写进草稿 config，`spec_generate` 不暴露）；MLX 版把它当作**调用方参数**（每次 `stream_generate` 现传）。这背后是两套实现的不同设计取向——MLX 版更「函数式、参数显式」，Transformers 版更「对象化、配置驱动」。两种都合理，只是抽象层级不同。

## 5. 综合实践

把本讲的「跑通 + 调参 + 看协作」串成一个贯通任务。

**任务**：用任一库型后端（有 GPU 选 Transformers，有 Apple 芯片选 MLX）跑通第一次 DFlash 生成，然后通过**修改 `max_new_tokens` 与 `temperature`** 观察输出与速度的变化，并用一句话把你观察到的现象与 4.3 的「target/draft 协作」联系起来。

**建议步骤（以 Transformers 为例；MLX 同理）**：

1. 按 4.1.4 跑通原版示例，确认能正常输出。
2. **调 `max_new_tokens`**：先设成 `64`，再设成 `1024`，对比生成长度与耗时。观察小 `max_new_tokens` 下「prompt 处理（prefill）耗时占比」是否更明显。
3. **调 `temperature`**：
   - `temperature=0.0`（贪心）：连续跑两次，输出应当**完全一致**。
   - `temperature=0.8`（采样）：连续跑两次，输出会**不同**；且因为采样引入了与 target 分布的偏差，草稿被接受的「平均接受长度」通常会**下降**，导致单位时间产出变慢。
4. （进阶，需 GPU）按 4.2.4 的「进阶」项，绕到 `dflash_generate(..., return_stats=True)`，打印 `acceptance_lengths`，**定量**对比 `temperature=0.0` 与 `temperature=0.8` 下的平均接受长度，验证第 3 步的直觉。
5. 回到 4.3：用一句话解释——「`temperature` 升高 → 草稿采样偏离 target → 验证时接受长度下降 → 加速变弱」，这正是 target/draft 协作中「验证门槛」的体现。

**需要观察的现象与预期结果**：

- 第 2 步：`max_new_tokens` 越大，总耗时越长，但「每 token 时间」基本稳定（decode 阶段每步成本相近）。
- 第 3 步：`temperature=0.0` 两次输出一致；`temperature=0.8` 两次输出不同且更慢。
- 第 4 步：`acceptance_lengths` 在高温下均值更低、方差更大。
- 以上都依赖真实硬件，**待本地验证**。若没有合适硬件，可把第 5 步作为纯理解性练习完成。

> 提醒：第 3 步里「采样会降低接受长度」是投机解码的一个普适规律，不是 DFlash 独有——只要草稿分布与 target 分布有偏差，验证就会拒得更多。这一点在 u2-l4《验证接受循环与采样》会从 `cumprod` 公式严格推导。

## 6. 本讲小结

- DFlash 在库型后端有两条等价的「第一次生成」路径：Transformers（`draft.spec_generate(...)`，需 GPU）与 MLX（`stream_generate(...)`，需 Apple 芯片）；前者一次性返回完整序列，后者流式逐块产出。
- Transformers 示例里，**草稿用 `AutoModel` 加载、target 用 `AutoModelForCausalLM` 加载**，两者必须同卡——因为草稿要复用 target 的 `embed_tokens` 与 `lm_head`。
- [`spec_generate`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L349-L366) 只暴露 5 个参数：`target`、`input_ids`、`max_new_tokens`、`stop_token_ids`、`temperature`；它是 [`dflash_generate`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L62-L169) 的一层薄封装。
- `block_size`、`mask_token_id`、`return_stats` **不在** `spec_generate` 签名里：前两个来自草稿模型的 `config`（`b16` 即 block_size=16），`return_stats` 只能通过直接调 `dflash_generate(..., return_stats=True)` 触发。
- target/draft 协作的代码落点是块起草段的「两个借」：[`target.model.embed_tokens(...)`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L111) 与 [`target.lm_head(...)`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L112)；草稿 `__init__` 里没有这两层，这正是它「轻量」的来源。
- `temperature=0.0` 走贪心 `argmax`（确定性），升高温度会降低草稿与 target 的吻合度、进而降低平均接受长度——这是本讲「调 temperature 观察速度」背后的原理。

## 7. 下一步学习建议

到这里，入门单元（u1）的「认识项目、安装隔离、包结构、跑通一次」四件事就齐了。接下来按你的兴趣选路：

- **想搞懂 Transformers 版的算法** → 进入 u2-l1《投机解码全局视图与生成控制流》，从 [`dflash_generate`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L62-L169) 的 prefill / 块起草 / 验证三阶段切入，建立完整推理主链路的心智模型；之后 u2-l2~u2-l5 逐层拆解草稿架构、块扩散注意力、验证接受循环（含 `cumprod` 接受长度公式）与权重加载。
- **想用 MLX 或做评测** → 第三单元 u3：u3-l1/u3-l2 深入 MLX 的 `stream_generate`、`_LayerHook` 钩子捕获 target 隐藏状态与缓存回滚；u3-l4/u3-l5 覆盖 benchmark 的数据集缓存、CLI 与多后端评测指标。
- **想立刻在生产里用** → 回到 u1-l2 的 vLLM/SGLang 服务型后端，用 `--speculative-config` 起一个服务，再用 u3-l5 的 `python -m dflash.benchmark --backend vllm ...` 量一把加速比。

一句话提醒：本讲把 `dflash_generate` 当作黑盒用了。它内部那套「块扩散并行起草 + cumprod 验证接受 + KV cache 裁剪」的精巧机制，正是第二单元要逐行打开的黑盒。
