# 注意力后端：可插拔的 attention 实现

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚「注意力后端（attention backend）」这个抽象在 SGLang 里扮演的角色——它是一层可插拔的 attention 计算实现。
2. 看懂 `AttentionBackend` 基类定义的统一契约：`forward` / `forward_extend` / `forward_decode` 以及一套「前向元数据」初始化方法。
3. 理解 `attention_registry.py` 的注册表机制，以及 `build_attention_backends` 如何根据 `--attention-backend` 把字符串解析成一个真实的后端对象。
4. 区分主流后端（FlashInfer / Triton / HPC-Ops 等）的定位与适用场景。
5. 精读本批次（#30540）新增的 `hpc_ops` 后端的约束校验链路：注册级、构造级、层级三道关卡。

> 说明：本讲在 manifest 中标记为 `update`，因为本轮仓库新增了 `hpc_ops` 后端。但旧的讲义文件此前并未落盘，因此本讲按「从零生成」的方式编写，并完整覆盖 hpc_ops 这一新内容。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**第一，注意力（attention）是 Transformer 里最吃算力的算子。** 给定一批 query \(Q\) 和历史 key/value 缓存，注意力做的事情可以简化为：

\[
\text{Attention}(Q,K,V)=\text{softmax}\!\left(\frac{QK^{\top}}{\sqrt{d}}\right)V
\]

在推理场景下，\(K,V\) 不是每次重算，而是被存进 KV 缓存（见 u4-l2）。所以「注意力后端」要解决的核心问题是：**如何在分页（paged）的 KV 缓存上，高效地算这一步？**

**第二，没有一个 attention kernel 能在所有硬件/模型上都是最优的。** Hopper 卡上有专门的 TMA 指令，AMD ROCm 有自己的 aiter 库，NPU/CPU/XPU 又各不相同；MLA 模型（如 DeepSeek）和普通 MHA 模型对 kernel 的要求也完全不同。如果把这些差异硬编码进模型代码，模型层会变得无法维护。

**第三，SGLang 的解法是「策略模式 + 注册表」。** 模型层（比如 `LlamaDecoderLayer` 里的 `RadixAttention`）只依赖一个抽象基类 `AttentionBackend`，不知道具体用了哪个 kernel；具体实现（FlashInfer、Triton、HPC-Ops……）在启动时按硬件/模型/用户参数选好，注入进去。这样新增一个 kernel 库只需要「写一个子类 + 注册一个名字」，模型代码一行都不用改。

> 名词解释：
> - **后端（backend）**：一个具体的 attention kernel 实现，对应一个 Python 类。
> - **MLA / MHA**：Multi-Head Attention（普通多头）vs Multi-head Latent Attention（DeepSeek 的低秩注意力）。两者 KV 缓存结构不同，需要的 kernel 也不同。
> - **分页 KV（paged KV）**：把 KV 缓存按固定大小的 page（如 64 个 token 一页）切块管理，类似操作系统的分页内存。
> - **extend / decode**：extend 指 prefill 或继续生成时「处理一段新 token」，decode 指「每个请求只生成 1 个新 token」。两种模式对 kernel 的形状要求不同，所以基类把它们拆成两个方法。

## 3. 本讲源码地图

本讲涉及的源码文件如下，全部位于 `python/sglang/srt/layers/attention/` 及其调用方：

| 文件 | 作用 |
| --- | --- |
| [`base_attn_backend.py`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/attention/base_attn_backend.py) | 定义抽象基类 `AttentionBackend`，是所有后端必须实现的契约。 |
| [`attention_registry.py`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/attention/attention_registry.py) | 注册表。用 `@register_attention_backend("名字")` 把字符串映射到一个「工厂函数」，工厂函数负责真正实例化后端对象。 |
| [`flashinfer_backend.py`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/attention/flashinfer_backend.py) | FlashInfer 后端，NVIDIA GPU 上的默认主力后端。 |
| [`triton_backend.py`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/attention/triton_backend.py) | Triton 后端，纯 Python/Triton 实现，可移植性强、易调试。 |
| [`hpc_ops_backend.py`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/attention/hpc_ops_backend.py) | 本轮新增的 HPC-Ops 后端（腾讯 Hunyuan 团队），分页 MHA kernel，约束较多。 |
| [`model_runner_components/attention_backend_setup.py`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/model_executor/model_runner_components/attention_backend_setup.py) | 启动时把「字符串」解析成「后端对象」的装配代码。 |
| [`server_args.py`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/server_args.py) | 定义 `--attention-backend` 等命令行参数及其可选值 `ATTENTION_BACKEND_CHOICES`。 |
| [`radix_attention.py`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/radix_attention.py) | 模型层使用的 `RadixAttention`，它把真正的计算委托给当前后端。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：抽象接口、注册与选择机制、三个代表性后端（含 hpc_ops 的约束校验）。

### 4.1 `AttentionBackend` 抽象接口

#### 4.1.1 概念说明

`AttentionBackend` 是所有后端的抽象基类（ABC）。它的存在让「模型层」和「kernel 实现」彻底解耦：

- 模型层（如 `RadixAttention`）只调用 `backend.forward(q, k, v, layer, forward_batch)`；
- 后端子类负责把 `q/k/v` 和「这次前向的批次信息」翻译成具体 kernel（FlashInfer / Triton / HPC-Ops……）能接受的入参，再调用 kernel。

一个后端要做的事可以归纳成两类：

1. **计算**：给定 `q/k/v`，算出注意力输出。又分 `forward_extend`（处理一段 token）和 `forward_decode`（每请求 1 个 token）两种形状。
2. **准备元数据（metadata）**：每次前向之前，根据批次信息（哪些请求、各自的序列长度、KV 缓存的 page 表）预先算好 kernel 需要的辅助张量（如 cumulative seq lens、page table）。这部分开销大，且很多字段要喂给 CUDA Graph 捕获，所以基类把它拆成一套「out-of-graph / in-graph」的初始化契约。

#### 4.1.2 核心流程

后端在每次前向中的参与点（伪代码）：

```
# 调度器/Worker 决定要跑一个批后（见 u5-l1）
forward_batch = ForwardBatch.init_new(schedule_batch, ...)   # 打包本次前向数据

# 1) 后端准备元数据
attn_backend.init_forward_metadata(forward_batch)
#   默认实现 = init_forward_metadata_out_graph(fb)   # CPU/动态形状部分
#            + init_forward_metadata_in_graph(fb)     # 可被 CUDA Graph 录制的 GPU 部分

# 2) 模型前向，每遇到一个 RadixAttention 层：
for layer in model.layers:
    q, k, v = layer.qkv_proj(hidden)                  # 投影出 q/k/v
    out = attn_backend.forward(q, k, v, layer, forward_batch)
    #     └─ 按 forward_mode 分发：
    #        idle  -> 返回空
    #        decode-> forward_decode(...)
    #        其它  -> forward_extend(...)
```

关键点：`forward` 是一个**分发方法**，它根据 `forward_batch.forward_mode`（idle / decode / extend）决定调用 `forward_decode` 还是 `forward_extend`。子类真正实现的是后两者。

#### 4.1.3 源码精读

**分发逻辑在基类的 `forward` 里**：

[`base_attn_backend.py:L167-L210`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/attention/base_attn_backend.py#L167-L210) —— `forward` 根据 `forward_mode` 把调用路由到 `forward_decode` / `forward_extend`（NPU 的 mixed 模式另有分支）；idle 模式直接返回一个形状正确但内容为空的张量，避免空算。这是所有后端共享的入口，子类不会重写它，只重写 `forward_extend` / `forward_decode`。

**两个待实现的计算方法**（默认抛 `NotImplementedError`）：

[`base_attn_backend.py:L212-L236`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/attention/base_attn_backend.py#L212-L236) —— `forward_decode` 和 `forward_extend` 的签名。注意它们都接收同一组参数：`q, k, v, layer, forward_batch, save_kv_cache`。`save_kv_cache` 控制是否把本轮的新 K/V 写回 KV 池（某些路径如 cross-layer 共享或 FP8 融合 RoPE 已经自己写过了，会传 `False`）。

**元数据初始化契约**（这是较新、也是较容易困惑的部分）：

[`base_attn_backend.py:L47-L89`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/attention/base_attn_backend.py#L47-L89) —— 基类把元数据初始化拆成三档：
- `init_forward_metadata(fb)`：eager（非图）入口，默认实现是「out_graph + in_graph」两步之和；
- `init_forward_metadata_out_graph(fb, in_capture=False)`：跑在 CUDA Graph 捕获块**之外**，承载 CPU 端、动态形状、不能被图录制的逻辑；
- `init_forward_metadata_in_graph(fb)`：跑在 `with graph.capture():` **之内**，必须是静态形状、可被录制的 GPU op，回放时由 `graph.replay()` 自动重放。

这个三段式契约存在的根本原因是 **CUDA Graph**（见 u7-l1）：只有静态形状、不触发 D2H 同步的 op 才能被录制进图。后端必须把「能录制的」和「不能录制的」分开，否则 decode 阶段无法用图回放来降延迟。hpc_ops 后端就是严格按这套契约实现的（见 4.3）。

**一个值得记住的类属性**：

[`base_attn_backend.py:L98-L99`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/attention/base_attn_backend.py#L98-L99) —— `needs_cpu_seq_lens: bool = True`。大多数后端需要把序列长度同步到 CPU（`seq_lens_cpu`）来建 page 表；但像 triton、trtllm_mha、hpc_ops 这些「在 GPU 上从 seq_lens 直接建 page table」的后端会把它置为 `False`，省掉一次 D2H 同步。

#### 4.1.4 代码实践

**实践目标**：确认「基类的 `forward` 是统一分发入口，子类只实现 extend/decode」。

**操作步骤（源码阅读型）**：

1. 打开 `base_attn_backend.py`，定位 `forward` 方法（L167）。
2. 观察它如何用 `forward_batch.forward_mode.is_idle()` / `is_decode()` 做三路分发。
3. 再打开任意一个后端（如 `hpc_ops_backend.py`），用编辑器搜索 `def forward`，确认该后端只定义了 `forward_extend` 和 `forward_decode`，**没有**重写 `forward`。

**需要观察的现象**：后端类里搜不到独立的 `forward`，但运行时 `RadixAttention` 调到的 `forward` 仍然能正确走到 extend/decode。

**预期结果**：分发逻辑全在基类，子类只填两个「形状相关」的实现。这正是策略模式的典型形态。

#### 4.1.5 小练习与答案

**练习 1**：如果一个后端只实现了 `forward_extend` 而忘了 `forward_decode`，会在什么时候报错？
**答案**：在运行时第一次出现 decode 批时。因为基类的 `forward_decode` 默认 `raise NotImplementedError()`（L223），只有真正进入 decode 路径才会触发，启动阶段不会报错——这也是为什么后端的约束校验（见 4.3）会尽量前置到 `__init__`，做到「启动即失败」。

**练习 2**：为什么 `init_forward_metadata` 要拆成 `_out_graph` 和 `_in_graph` 两个方法？
**答案**：因为 CUDA Graph 只能录制静态形状、不触发 CPU 同步的 GPU op。把元数据准备拆成「图外（CPU/动态）」和「图内（可录制）」两段，让 decode 能整段录进图里回放，从而降低每步的 CPU 开销（零开销调度器的关键之一，见 u3-l4）。

---

### 4.2 注册表与后端选择机制

#### 4.2.1 概念说明

光有抽象基类还不够——模型层需要拿到一个**具体的**后端对象。SGLang 用一个全局字典 `ATTENTION_BACKENDS` 作为注册表，把「字符串名字」映射到一个「工厂函数」。启动时，根据用户传的 `--attention-backend`（或模型/硬件的默认值）查表，调用对应工厂函数，产出后端实例。

这样做的好处是**开放封闭**：新增一个后端，只需在它自己的文件里写一个被 `@register_attention_backend("xxx")` 装饰的工厂函数，注册表自动多一条记录，模型代码和选择逻辑都不用动。

#### 4.2.2 核心流程

从命令行字符串到后端对象的全链路：

```
用户: --attention-backend hpc_ops
   │
   ▼  argparse 校验该值 ∈ ATTENTION_BACKEND_CHOICES
ServerArgs.attention_backend = "hpc_ops"
   │
   ▼  ModelRunner 启动时
build_attention_backends(model_runner)         # attention_backend_setup.py
   │   解析 prefill/decode 两个字符串（可分别指定）
   ▼
_build_full_attention_backend_from_str(backend_str="hpc_ops")
   │   查注册表 ATTENTION_BACKENDS["hpc_ops"]
   ▼
create_hpc_ops_backend(runner)                 # 工厂函数，做约束校验
   │
   ▼
HPCOpsAttnBackend(runner)                      # 真正的后端对象
   │
   ▼  存入 model_runner.attn_backend
模型前向: RadixAttention -> get_attn_backend().forward(...)
```

有两个细节值得注意：

1. **prefill / decode 可以用不同后端**。`--prefill-attention-backend` 和 `--decode-attention-backend` 优先级高于 `--attention-backend`；若两者不同，会被包成一个 `HybridAttnBackend`。
2. **工厂函数是「懒加载」的**。`create_xxx_backend` 里 `import` 具体后端类的语句写在函数体内（而不是文件顶部），这样即便用户没装某个 kernel 库（比如没装 `hpc` 包），也不会因为 `import` 失败而启动崩掉——只有真选中它时才 import。

#### 4.2.3 源码精读

**注册表与装饰器**：

[`attention_registry.py:L30-L38`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/attention/attention_registry.py#L30-L38) —— `ATTENTION_BACKENDS = {}` 是全局注册表；`register_attention_backend(name)` 是一个装饰器工厂，它把被装饰的函数 `fn` 存进字典。注意存的是**函数本身**（工厂），不是实例。

**FlashInfer 的工厂（懒加载 + MLA 分流）**：

[`attention_registry.py:L41-L65`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/attention/attention_registry.py#L41-L65) —— `create_flashinfer_backend` 在函数体内 `import FlashInferAttnBackend`；并按 `runner.use_mla_backend` 分流到普通 MHA 后端或 MLA 专用后端。这是「一个名字、多种实现」的常见手法。

**Triton 的工厂（带前置断言）**：

[`attention_registry.py:L164-L172`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/attention/attention_registry.py#L164-L172) —— `create_triton_backend` 在 import 之前先 `assert not runner.model_config.is_encoder_decoder`，即 triton 不支持 encoder-decoder（交叉注意力），把不兼容的情况在工厂入口就挡掉。

**hpc_ops 的工厂（注册级约束校验）**：

[`attention_registry.py:L247-L261`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/attention/attention_registry.py#L247-L261) —— `create_hpc_ops_backend` 在 import 前做了三道**注册级**校验：

- 非 MLA 模型（`runner.use_mla_backend` 为真则报错）；
- 非 encoder-decoder（`is_encoder_decoder` 为真则报错，因为不支持交叉注意力）；
- 未开启投机解码（`speculative_algorithm is not None` 则报错）。

这三条与本讲的实践任务直接对应。注意它们和 Triton 的 `assert` 思路一致：**在构造对象之前就拒绝不合法组合**，给出明确错误信息。

**真正查表的地方**：

[`attention_backend_setup.py:L240-L246`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/model_runner/model_runner_components/attention_backend_setup.py#L240-L246) —— `_build_full_attention_backend_from_str` 是注册表的消费方：`ATTENTION_BACKENDS[backend_str](model_runner)`。前一行 `if backend_str not in ATTENTION_BACKENDS: raise ValueError(...)` 兜底非法名字。

**选择/装配的总入口**：

[`attention_backend_setup.py:L68-L136`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/model_executor/model_runner_components/attention_backend_setup.py#L68-L136) —— `build_attention_backends` 处理 pdmux（多路 decode 后端）、two-batch-overlap 等高级形态，普通情况下走 `_build_resolved_backend`，最终调用上面的查表逻辑，并把结果存进 `model_runner.attn_backend`。

**命令行可选值清单**：

[`server_args.py:L194-L222`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/server_args.py#L194-L222) —— `ATTENTION_BACKEND_CHOICES` 列出所有合法后端名。本轮新增的 `hpc_ops` 出现在第 214 行，注释标明「Hopper+, requires --page-size 64」。`argparse` 用这个列表做 `choices=` 校验，传错名字会在解析阶段就报错。

**prefill/decode 分别的解析**：

[`server_args.py:L7944-L7955`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/server_args.py#L7944-L7955) —— `get_attention_backends()` 体现「专用优先于通用」的回退：prefill/decode 字段没填就用 `attention_backend`。

**模型层如何拿到后端**：

[`radix_attention.py:L272-L280`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/radix_attention.py#L272-L280) —— `RadixAttention.forward` 的「非图编译分支」直接 `get_attn_backend().forward(...)`。`get_attn_backend()` 从本次前向的 context 里取出当前生效的后端对象（overlap 模式下不同 stream 可能用不同后端实例）。这一行就是「模型层 ↔ 后端」的解耦点：模型完全不知道具体后端是谁。

#### 4.2.4 代码实践

**实践目标**：把「字符串 → 工厂函数 → 后端对象」这条链路在源码里走一遍，并定位每个后端名字的注册位置。

**操作步骤（源码阅读型，无需 GPU）**：

1. 在 `attention_registry.py` 里全局搜索 `@register_attention_backend(`，记录每个名字对应的工厂函数行号。你会看到 `flashinfer`、`triton`、`fa3`、`trtllm_mla`、`hpc_ops` 等几十个。
2. 在 `server_args.py` 里找到 `ATTENTION_BACKEND_CHOICES`，对比两者：每个 `choices` 里的名字是否都能在注册表里找到工厂函数？（理论上应一一对应。）
3. 在 `attention_backend_setup.py` 里定位 `ATTENTION_BACKENDS[backend_str]`（L246），这是「名字 → 对象」的真正转换点。

**需要观察的现象**：注册表是「装饰器在模块导入时自动填充」的，没有任何一处集中写「名字↔类」的大表。

**预期结果**：你能用一句话说清「新增一个后端需要改哪两处」——(a) 写一个 `AttentionBackend` 子类；(b) 写一个被 `@register_attention_backend("名字")` 装饰的工厂函数；(c) 把名字加进 `ATTENTION_BACKEND_CHOICES`（否则 argparse 拒绝）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `create_flashinfer_backend` 里的 `from ... import FlashInferAttnBackend` 要写在函数体内，而不是文件顶部？
**答案**：为了懒加载和隔离导入失败。注册表模块会在启动时被整体 import，如果每个后端的依赖都写在顶部，那么没装 flashinfer（或 hpc、aiter 等）的用户一启动就会 import 失败。把 import 放进工厂函数，只有真正选中该后端时才会触发 import，互不影响。

**练习 2**：用户传 `--attention-backend foo`（一个不存在的名字），会在哪一层被挡住？
**答案**：通常在 argparse 解析阶段就被 `choices=ATTENTION_BACKEND_CHOICES` 挡住（返回 argparse 错误）。即便绕过 argparse，`_build_full_attention_backend_from_str` 里的 `if backend_str not in ATTENTION_BACKENDS` 也会兜底抛 `ValueError`。

---

### 4.3 三个代表性后端：FlashInfer / Triton / HPC-Ops（含约束校验）

#### 4.3.1 概念说明

这一节精读三个后端，它们代表三种典型取舍：

| 后端 | 定位 | 实现语言 | 典型场景 |
| --- | --- | --- | --- |
| **FlashInfer** (`flashinfer`) | NVIDIA GPU 上的高性能主力后端 | C++/CUDA 库 + Python 绑定 | 通用 LLM 服务，特性最全（MLA/滑动窗口/多种 KV dtype） |
| **Triton** (`triton`) | 纯 Python/Triton 写的参考实现 | Triton kernel | 调试、不支持 FlashInfer 的环境、可移植性优先 |
| **HPC-Ops** (`hpc_ops`) | 腾讯 Hunyuan 团队针对 H20 调优的分页 MHA | 外部 `hpc` 包 | HunYuan 等特定模型在 H20 上的极致延迟，约束很多 |

HPC-Ops 是本轮（PR #30540）新增的，它最能体现「后端约束校验」的设计：**一个后端能做什么、不能做什么，要在尽可能早的时刻、用清晰的错误信息告诉用户**。hpc_ops 的约束分布在三层，值得逐一精读。

#### 4.3.2 核心流程

hpc_ops 后端从「被选中」到「真正能跑」要过三道关：

```
第 1 关：注册级（create_hpc_ops_backend，工厂函数入口）
   非 MLA、非 encoder-decoder、未开投机解码   —— 不满足直接 ValueError

第 2 关：构造级（HPCOpsAttnBackend.__init__）
   装了 hpc 包?       —— 没装 ImportError
   page_size == 64?   —— 否则 ValueError
   KV dtype ∈ {bf16, fp8_e4m3}?  —— 否则 ValueError
   FP8 路径下 (num_q_heads, num_kv_heads) ∈ {(8,1),(64,8)}?
   head_dim == 128 且 GQA group size ∈ {4,8}?

第 3 关：层级（_check_layer_supported，每次 forward 调用）
   无滑动窗口、无 logit cap、softmax scale == head_dim**-0.5
```

这三层校验体现一个原则：**能前置的尽量前置**（启动即失败 > 运行时崩）。注册级和构造级在启动时就会触发，层级校验因为依赖每层的 `RadixAttention` 配置，只能在 forward 时做，但同样给出明确错误。

#### 4.3.3 源码精读

**模块顶部文档**——hpc_ops 的硬性要求一览：

[`hpc_ops_backend.py:L1-L28`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/attention/hpc_ops_backend.py#L1-L28) —— 注释里把约束讲得很清楚：Hopper+(sm90+)、`--page-size 64`、bf16 或 fp8_e4m3 KV cache、head_dim==128、GQA group size ∈ {4,8}、默认 softmax scale、无滑动窗口、无 logit cap、decoder-only。还特别说明：当前主要针对 **H20** 调优，在 H100/H200/B200 上提升可能有限。

**约束常量**——把「魔法数字」抽成有名常量：

[`hpc_ops_backend.py:L61-L70`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/attention/hpc_ops_backend.py#L61-L70) —— `_SUPPORTED_GQA_GROUP_SIZES = (4, 8)`、`_SUPPORTED_HEAD_DIM = 128`、`_REQUIRED_PAGE_SIZE = 64`、`FP8_ROPE_SUPPORTED_HEAD_CONFIGS = ((8, 1), (64, 8))`。把约束常量化，校验代码读起来才像自然语言。

**是否有 hpc 包**（懒探测）：

[`hpc_ops_backend.py:L73-L76`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/attention/hpc_ops_backend.py#L73-L76) —— `has_hpc_ops()` 用 `importlib.util.find_spec("hpc")` 探测是否安装了 `hpc` 包，用 `@functools.cache` 只探测一次。

**构造级校验**——本节最核心的源码：

[`hpc_ops_backend.py:L116-L174`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/attention/hpc_ops_backend.py#L116-L174) —— `HPCOpsAttnBackend.__init__` 顺序校验：
- L119-L123：没装 `hpc` 包则 `ImportError`，提示安装地址；
- L125-L129：**再次**校验投机解码（与注册级重复，因为 `__init__` 也可能被直接调用，属于防御性双保险）；
- L131-L136：`page_size != 64` 则报错；
- L137-L141：KV dtype 必须是 bf16 或 fp8_e4m3；
- L145-L159：FP8 路径额外要求 `(num_q_heads // tp, num_kv_heads)` 落在 `FP8_ROPE_SUPPORTED_HEAD_CONFIGS`，因为融合 RoPE+quant kernel 是按形状特化的；
- L166-L174：`head_dim == 128` 且 `gqa_group_size ∈ {4,8}`。

注意 L162-L165 计算 `num_q_heads` 和 `num_kv_heads` 时都**除以了 `model_runner.ps.tp_size`**——也就是说 GQA group size 的约束是按「每张卡（每个 TP rank）」算的，这是张量并行（见 u8-l1）下的正确口径。

**层级校验**——每次 forward 都查一遍：

[`hpc_ops_backend.py:L323-L337`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/attention/hpc_ops_backend.py#L323-L337) —— `_check_layer_supported` 校验单个 `RadixAttention` 层：无滑动窗口、`logit_cap <= 0`、`scaling ≈ head_dim**-0.5`。因为 hpc 的 kernel 把 softmax scale 硬编码成了 \(1/\sqrt{d}\)，模型若用了别的 scale 就会算错，必须拦下。它在 `forward_extend` 和 `forward_decode` 开头各调用一次（L534、L588）。

**forward_extend / forward_decode 的最终调用**：

[`hpc_ops_backend.py:L522-L574`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/attention/hpc_ops_backend.py#L522-L574) —— `forward_extend` 先 `_check_layer_supported`，再按需把 K/V 写回池（`set_kv_buffer`），最后从 `forward_metadata` 取出 page_table / cu_seqlens，调用 `hpc.attention_with_kvcache_prefill_bf16`（bf16）或 `..._fp8`（FP8）。`forward_decode`（[L576-L632](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/attention/hpc_ops_backend.py#L576-L632)）结构对称，调用 `hpc.attention_decode_bf16` / `hpc.attention_decode_fp8`。

**与基类契约的对齐**——CUDA Graph 友好：

[`hpc_ops_backend.py:L114`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/attention/hpc_ops_backend.py#L114) —— `needs_cpu_seq_lens: bool = False`，因为 hpc_ops 在 GPU 上从 `seq_lens` 直接建 page table（见 `init_forward_metadata_in_graph`，[L296-L311](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/attention/hpc_ops_backend.py#L296-L311) 调用 `update_trtllm_mha_graph_metadata`），不需要 D2H 同步。这正好呼应 4.1 讲的基类契约。

**对比：FlashInfer 后端的构造**（看它「不挑食」）：

[`flashinfer_backend.py:L300-L329`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/attention/flashinfer_backend.py#L300-L329) —— `FlashInferAttnBackend.__init__` 没有 hpc_ops 那一堆硬约束，而是处理滑动窗口 KV 池、量化访问方式等「通用能力适配」，体现它「特性最全的主力后端」定位。

**对比：Triton 后端的构造**（纯 Python kernel）：

[`triton_backend.py:L110-L139`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/attention/triton_backend.py#L110-L139) —— `TritonAttnBackend.__init__` 懒加载来自 `kernels.ops.attention` 的 `decode_attention_fwd` / `extend_attention_fwd` 等 Triton kernel，并用 `torch.compiler.disable` 包裹它们（避免被 torch.compile 追踪）。同样 `needs_cpu_seq_lens = False`。

#### 4.3.4 代码实践

**实践目标**：亲手切换后端启动服务，并在源码中定位「选择逻辑」与「hpc_ops 约束校验」的精确位置。这是本讲的主实践。

**操作步骤**：

1. **可运行部分（需要 NVIDIA GPU）**：用一个小模型分别以两个后端启动，对比能否成功：

   ```bash
   # FlashInfer（默认主力，最可能成功）
   python -m sglang.launch_server --model-path <小模型> --attention-backend flashinfer --port 30000

   # Triton（可移植，几乎任何 NVIDIA 卡都行）
   python -m sglang.launch_server --model-path <小模型> --attention-backend triton --port 30000
   ```

   启动日志里搜 `attention backend` 相关行，确认生效的后端；再发一个 `/v1/chat/completions` 请求验证可用。

2. **hpc_ops 部分（大概率本地无法运行，需 Hopper+ 且装了 `hpc` 包）**：

   ```bash
   python -m sglang.launch_server --model-path <HunYuan 或任意 MHA 模型> \
       --attention-backend hpc_ops --page-size 64 --port 30000
   ```

   若不满足约束，预期会看到**清晰的报错**。逐条对照源码验证报错来自哪一行：
   - 若 `hpc` 包未安装 → 来自 [hpc_ops_backend.py:L119-L123](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/attention/hpc_ops_backend.py#L119-L123)；
   - 若未加 `--page-size 64` → 来自 [L131-L136](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/attention/hpc_ops_backend.py#L131-L136)；
   - 若开了投机解码 → 来自注册级 [attention_registry.py:L255-L258](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/attention/attention_registry.py#L255-L258)。

3. **源码阅读部分（无需 GPU）**：完成本讲 4.2.4 的注册表清点，并确认 `ATTENTION_BACKEND_CHOICES`（[server_args.py:L214](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/server_args.py#L214)）里 `hpc_ops` 的注释「Hopper+, requires --page-size 64」与构造级校验一一对应。

**需要观察的现象**：
- flashinfer / triton 切换后服务正常，输出一致（数值上可能有极小浮点差异）。
- hpc_ops 在不满足约束时**启动即失败**，错误信息精确指出缺什么。

**预期结果**：
- 能用一句话说出「选择逻辑」位置：`build_attention_backends` → `_build_full_attention_backend_from_str` → `ATTENTION_BACKENDS[backend_str](runner)`。
- 能列出 hpc_ops 的三组约束（非 MLA/非 encoder-decoder/不支持投机解码）及其在校验代码中的行号。

> 待本地验证：hpc_ops 的实际加速效果依赖 H20 硬件与 `hpc` 包；若本地无此环境，第 2 步只验证「约束被正确拦截」即可，不要假装跑出了延迟数字。

#### 4.3.5 小练习与答案

**练习 1**：hpc_ops 的约束校验为什么要在三个地方（注册级 / 构造级 / 层级）分别做，而不是集中在一处？
**答案**：因为三处能拿到的信息不同、时机不同。注册级（工厂函数）在 import 前就能拿到 `runner.use_mla_backend` / `is_encoder_decoder` / `speculative_algorithm`，最早、最廉价，用于拦截「根本不兼容的组合」；构造级（`__init__`）能拿到 page_size、kv_cache_dtype、head 配置，用于「装好对象前」的硬件/参数校验；层级（`_check_layer_supported`）只能拿到每层的 sliding_window / scaling，且不同层可能不同，只能在 forward 时校验。分层校验让「能早失败的早失败」，避免跑了一半才崩。

**练习 2**：hpc_ops 在 `__init__` 里计算 GQA group size 时为什么要除以 `model_runner.ps.tp_size`？
**答案**：因为张量并行（TP）会把注意力头切分到多张卡上（见 u8-l1）。kernel 看到的是「每张卡上」的 q/kv 头数，约束 `(num_q_heads // num_kv_heads) ∈ {4,8}` 也是按单卡口径定义的。所以必须用 TP 切分后的头数来算 group size，而不是模型配置里的全局头数。

**练习 3**：如果你给 hpc_ops 传了一个带滑动窗口的模型，会在哪一行、什么时候报错？
**答案**：在 [`hpc_ops_backend.py:L324-L327`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/attention/hpc_ops_backend.py#L324-L327)（`_check_layer_supported`），在**第一次 forward**（prefill 或 decode 的开头）触发。因为它依赖具体层的 `sliding_window_size`，启动阶段并不知道，所以只能延后到 forward。

## 5. 综合实践

**任务：把「后端」这个抽象从头到尾串起来，模拟新增一个假后端 `my_attn`。**

> 这是一个**源码阅读 + 设计推演**型任务，不需要真的提交代码，目的是检验你是否掌握了「抽象接口 + 注册表 + 选择 + 校验」这一整套机制。

请按顺序完成：

1. **抽象层**：打开 [`base_attn_backend.py`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/attention/base_attn_backend.py)，列出 `my_attn` 后端最少要实现哪些方法才能跑起来（提示：`forward_extend`、`forward_decode` 是必须的；`init_forward_metadata*` 若你要支持 CUDA Graph 也必须实现）。

2. **注册层**：仿照 [`attention_registry.py:L247-L261`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/attention/attention_registry.py#L247-L261) 的 `create_hpc_ops_backend`，写出 `create_my_attn_backend(runner)` 工厂函数的骨架，并决定：你的后端有哪些「不兼容组合」要在工厂入口校验？（例如只支持 decode、不支持 MLA 等）

3. **选择层**：追踪 `--attention-backend my_attn` 这个字符串会经过 `ATTENTION_BACKEND_CHOICES`（[server_args.py:L194](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/server_args.py#L194)）→ `build_attention_backends` → `ATTENTION_BACKENDS["my_attn"](runner)` 的路径，标注每一步的文件和行号。

4. **校验设计**：参照 hpc_ops 的三层校验，为你的 `my_attn` 设计约束：哪些放注册级、哪些放构造级、哪些放层级，并说明理由（提示：原则是「信息可得的最早时机 + 启动即失败优先」）。

5. **委托点**：最后确认模型层完全不感知你的后端——它的入口永远是 [`radix_attention.py:L272-L280`](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/python/sglang/srt/layers/radix_attention.py#L272-L280) 的 `get_attn_backend().forward(...)`。

**交付物**：一张表，列出「新增 `my_attn` 后端需要改动的文件 / 函数 / 行为」；一段话，解释为什么模型层（`RadixAttention`）一行都不用改。

## 6. 本讲小结

- **注意力后端是可插拔的 attention 实现**：模型层 `RadixAttention` 只依赖抽象基类 `AttentionBackend`，具体 kernel（FlashInfer/Triton/HPC-Ops 等）在启动时被注入，新增 kernel 不改模型代码。
- **基类契约有两部分**：计算（`forward` 按 `forward_mode` 分发到 `forward_extend` / `forward_decode`）和元数据准备（`init_forward_metadata` 拆成 `_out_graph` / `_in_graph` 以兼容 CUDA Graph 录制）。
- **注册表是装饰器自动填充的**：`@register_attention_backend("名字")` 把工厂函数存进全局 `ATTENTION_BACKENDS`；工厂函数体内部懒加载具体类，隔离导入失败。
- **选择链路清晰**：`--attention-backend` 字符串 → `ATTENTION_BACKEND_CHOICES` 校验 → `build_attention_backends` → `ATTENTION_BACKENDS[str](runner)` → 后端对象，存入 `model_runner.attn_backend`。
- **prefill/decode 可分别指定后端**，不一致时包成 `HybridAttnBackend`；`get_attn_backend()` 在前向 context 里取出当前后端。
- **本轮新增的 hpc_ops 后端示范了「分层约束校验」**：注册级（非 MLA/非 encoder-decoder/不支持投机解码）、构造级（Hopper+ 的 hpc 包、page_size 64、bf16/fp8 KV、head_dim 128、GQA ∈ {4,8}）、层级（无滑动窗口/无 logit cap/默认 scale），原则是「能早失败的早失败」。

## 7. 下一步学习建议

- **CUDA Graph 如何消费这套元数据契约**：本讲多次提到 `init_forward_metadata_in_graph` 是为 CUDA Graph 录制服务的，建议接着读 u7-l1（CUDA Graph 捕获与回放），看 `CPUGraphRunner.capture` 如何在 `with graph.capture():` 里调用各后端的 in_graph 方法。
- **MLA 后端与普通 MHA 后端的分野**：本讲的 flashinfer 工厂里出现了 `use_mla_backend` 分流。如果你关心 DeepSeek 类模型，建议阅读 `flashinfer_mla_backend.py`、`trtllm_mla_backend.py` 等 MLA 专用后端，对比它们与 MHA 后端在 KV 缓存结构上的差异（联系 u4-l2 的 `MLATokenToKVPool`）。
- **算子层的统一命名空间**：triton / hpc_ops 后端实际调用的 kernel（如 `decode_attention_fwd`、`build_trtllm_mha_page_table`）已迁入 `python/sglang/kernels/ops/`，建议在读 u11-l2（统一算子体系）时回看本讲，理解「attention backend（调度适配层）」与「kernel（纯算子层）」的分层关系。
- **尝试新增一个后端**：完成本讲综合实践后，可以参考仓库里最简单的后端（如 `torch_native_backend.py`）作为模板，真正动手写一个最小后端并注册，这是检验理解的最佳方式。
