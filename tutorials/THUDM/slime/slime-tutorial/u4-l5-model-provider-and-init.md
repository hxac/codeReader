# 模型构建、并行初始化与参数冻结

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 slime 是如何「凭命令行参数搭出一个按并行策略分好片的 Megatron 模型」的，即 `get_model_provider_func` 做了什么。
- 区分三种「定制模型结构」的入口：`--spec`（换层规格）、`--custom-model-provider-path`（整体替换模型工厂）、`--custom-megatron-init-path`（替换初始化逻辑）。
- 掌握 `--only-train-params-name-list`（白名单）与 `--freeze-params-name-list`（黑名单）两套互斥的正则冻结机制，并知道它们在哪一步生效。
- 理解 `init()` 如何建立 TP/PP/CP/EP/DP 通信组、设置随机种子、构建 tokenizer，以及 critic 输出头为什么是一个 `hidden_size → 1` 的线性层。

本讲承接 [u4-l1 MegatronTrainRayActor 训练工人生命周期](u4-l1-megatron-actor-lifecycle.md)：u4-l1 讲了工人 `init → train_actor → update_weights` 的生命周期骨架，其中 `init()` 内部调用了本讲要讲的 `init(args)` 与 `initialize_model_and_optimizer`（内部又调用 `get_model_provider_func`）。本讲就是拆开这两个调用，看「搭模型」和「初始化分布式」的细节。

## 2. 前置知识

在进入源码前，先用三段话补齐基础概念。

**Megatron 的模型并行五件套。** Megatron-LM 把一个 Transformer 切成多个维度并行：

- **TP（Tensor Parallel，张量并行）**：把每一层的权重矩阵按列或行切到多张卡上，同一次矩阵乘法由多卡合作完成，通信频繁、适合同机高带宽。
- **PP（Pipeline Parallel，流水线并行）**：把不同的 Transformer 层放到不同卡上，前向时 micro-batch 像流水线一样流过各层。
- **CP（Context Parallel，上下文并行）**：把一条长序列切成多段分到多卡，各自处理一段，靠 zigzag（交错）切分与注意力通信还原全局结果。
- **EP（Expert Parallel，专家并行）**：MoE 模型里把不同的 expert（专家）放到不同卡上。
- **DP（Data Parallel，数据并行）**：把不同的 micro-batch 分给多组卡，各自前向后用 AllReduce 聚合梯度。

slime 本身不重写并行引擎，它只是把这些尺寸从命令行参数读进来，交给 Megatron 去建通信组。

**`requires_grad` 与参数冻结。** PyTorch 中每个参数张量有一个布尔属性 `requires_grad`。设为 `False` 后，反向传播不会为它计算梯度，优化器也不会更新它。本讲的「参数冻结」就是批量地把一批参数的 `requires_grad` 设为 `False`。

**Megatron 的 spec（层规格）。** Megatron 用「spec 对象」描述一层 Transformer 由哪些子模块组成（self-attention、MLP、layernorm 的排列与类型）。换一个 spec 就能改变层结构（例如开启 `multi_latent_attention`、换不同的 attention 实现），而不必改模型代码。slime 通过 `--spec` 让用户指定一个返回 spec 的函数。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `slime/backends/megatron_utils/model_provider.py` | 模型工厂：把命令行参数翻译成 Megatron `GPTModel`，并负责 critic 输出头替换与参数冻结 |
| `slime/backends/megatron_utils/initialize.py` | 分布式初始化：建 TP/PP/CP/EP/DP 通信组、设随机种子、建 tokenizer |
| `slime/backends/megatron_utils/actor.py` | 工人生命周期入口，依次调用 `init(args)` 与 `initialize_model_and_optimizer` |
| `slime/backends/megatron_utils/model.py` | `initialize_model_and_optimizer` 调用 `get_model_provider_func` 搭模型 |
| `slime/utils/arguments.py` | 定义 `--only-train-params-name-list` / `--freeze-params-name-list` / `--custom-model-provider-path` 等参数与互斥校验 |
| `slime_plugins/models/glm4.py` | 一个真实的「自定义层规格」示例，演示 `--spec` 的用法 |

---

## 4. 核心概念与源码讲解

### 4.1 模型构建：`get_model_provider_func`

#### 4.1.1 概念说明

Megatron 训练不能像 HuggingFace 那样「`from_pretrained` 读个 config.json 就搭出模型」——它要求你提供一个**模型工厂函数（model provider）**，签名形如 `model_provider(pre_process, post_process, vp_stage) -> GPTModel`，由它根据命令行参数搭出按并行策略分好片的模型。这个工厂的职责是：

1. 决定每一层用什么 spec（普通稠密层 / MoE / 自定义层）。
2. 用 Megatron 的 `GPTModel` 把 `config + spec + 并行参数` 组装成一个真实模型。
3. 如果是 critic 角色，把最后的「词表输出层」换成一个「标量打分层」。

slime 把这一切封装进 `get_model_provider_func(args, role)`：它读 `args`、记住 `role`（`"actor"` 或 `"critic"`），返回一个符合 Megatron 工厂签名的函数。

为什么需要 `pre_process` / `post_process` 两个布尔参数？这是流水线并行的产物：在 PP 场景，一个模型被拆到多张卡上，每张卡只持有模型的一部分。`pre_process=True` 表示这张卡负责输入 embedding，`post_process=True` 表示这张卡负责输出 logits。工厂函数据此决定要不要建 embedding 层和输出层。

#### 4.1.2 核心流程

`get_model_provider_func` 内部的决策流程（伪代码）：

```
get_model_provider_func(args, role):
    返回一个三层嵌套包装后的工厂函数：
    外层 wrap_model_provider_with_freeze
        → 搭好模型后调用 freeze_model_params(model, args)

    内层 _get_model_provider_func 选择「用哪条路搭模型」：
        if args.custom_model_provider_path:        # 路线 A：整体替换
            调用用户提供的 custom_model_provider 搭模型
        else:                                       # 路线 B：默认搭法
            1. 选 spec：
                 - if args.spec:  从用户函数拿 spec（或拿一个完整工厂）
                 - elif MoE:      get_gpt_decoder_block_spec
                 - else:          TE spec 或 local spec
            2. 用 GPTModel(config, spec, 并行参数...) 搭模型
        if post_process and role == "critic":
            把 output_layer 换成 LinearForLastLayer(hidden_size → 1)
```

三条「定制模型结构」的路线要分清：

- **`--spec`（换层规格）**：最常用。给出一个返回 spec 的函数，slime 仍用默认 `GPTModel` 组装，只是层结构变了。
- **`--custom-model-provider-path`（整体替换工厂）**：完全跳过 slime 的默认搭法，用户自己返回一个 `GPTModel`。适合 VL（视觉语言）等结构差异巨大的模型。
- **`--custom-megatron-init-path`（替换初始化逻辑）**：不动模型结构，只额外跑一段用户自定义的初始化代码（在 `init()` 末尾）。

#### 4.1.3 源码精读

先看对外入口，它把「真正搭模型」和「冻结参数」用一层包装缝在一起：

[model_provider.py:229-230](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model_provider.py#L229-L230) —— `get_model_provider_func` 把 `_get_model_provider_func` 的产物包上冻结逻辑后返回。

[model_provider.py:206-226](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model_provider.py#L206-L226) —— 包装函数：先调原始工厂搭出 `model`，紧接着调 `freeze_model_params(model, args)`。注意它用 `inspect.signature` 探测原始工厂接受哪些参数（`vp_stage` / `config` / `pg_collection`），按需透传，兼容不同 Megatron 版本的工厂签名。

接下来看核心的「默认搭法」。先看 spec 的三条分支：

[model_provider.py:104-145](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model_provider.py#L104-L145) —— spec 选择。`args.spec` 不为空时走自定义路线；否则 MoE 模型用 `get_gpt_decoder_block_spec`（block 级 spec，因为 MoE 的专家按 block 组织），稠密模型按是否用 TransformerEngine（`use_te`）选 `get_gpt_layer_with_transformer_engine_spec` 或 `get_gpt_layer_local_spec`。

`--spec` 这条自定义路线值得细看。脚本里它的写法是两个字符串：

```bash
# scripts/models/glm4-9B.sh
--spec "slime_plugins.models.glm4" "get_glm_spec"
```

[model_provider.py:104-118](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model_provider.py#L104-L118) —— `import_module(args.spec)` 把这个列表解析成最终属性（这里是 `get_glm_spec` 函数对象）。若它是可调用的，slime 调 `transformer_layer_spec(args, config, vp_stage)` 拿到结果；如果结果本身又是一个带 `pre_process` 参数的工厂（注释里以 glm-omni VL 模型为例），就**直接委托**给这个工厂搭模型——这是 `--spec` 能同时承担「换层规格」和「换整个工厂」两种角色的关键。否则把结果当作真正的 spec 使用。

来看真实的 `get_glm_spec`：

[slime_plugins/models/glm4.py:4-14](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime_plugins/models/glm4.py#L4-L14) —— 它只是调 Megatron 的 TE spec 工厂，多透传了 `post_self_attn_layernorm` / `post_mlp_layernorm` 两个 GLM4 特有开关。这就是「自定义层规格」最常见、最轻量的形态。

接着看 GPTModel 的组装：

[model_provider.py:164-196](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model_provider.py#L164-L196) —— 把命令行参数（`padded_vocab_size`、`max_position_embeddings`、`rotary_base`、`rotary_percent`、rope scaling、是否共享 embedding 权重等）组装成 `GPTModel` 的构造参数，并设置 `parallel_output=True`（让 logits 按 TP 切分输出，交由后续的 TP 感知 loss 处理，避免昂贵的全 gather）。若开了 `--fp8-param-gather`，模型在 `fp8_model_init` 上下文里构建（见第 149-158 行的 try/except）。若配置了 MTP（multi-token prediction）层数，还会额外建一个 `mtp_block_spec`。

最后是 critic 输出头——这是 actor 与 critic 在模型结构上的唯一差别：

[model_provider.py:198-199](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model_provider.py#L198-L199) —— 当 `role == "critic"` 且当前卡负责输出（`post_process`）时，把模型默认的 `output_layer`（一个 `hidden_size → vocab_size` 的词表投影）替换成 `LinearForLastLayer(hidden_size → 1)`。critic 要的不是词表概率，而是一个标量价值估计 \(V(s)\)，所以输出维度是 1。

来看这个自定义输出层的实现：

[model_provider.py:24-57](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model_provider.py#L24-L57) —— `LinearForLastLayer` 继承 `torch.nn.Linear`。两个关键点：(1) 它感知 `sequence_parallel`——若开启 SP，权重打上 `sequence_parallel` 标记，前向末尾用 `gather_from_sequence_parallel_region` 收集 logits（这正是它名为 *ForLastLayer* 的原因：最后一层需要把 SP 切开的序列维 gather 回来才能算标量）；(2) `forward` 把输出 `.float()` 后返回 `(logits, None)`，这个 `None` 占位是为了和 Megatron 输出层「返回 (logits, loss)」的约定对齐。

> 这段代码头部注释标注「Adapt from verl」，说明 critic 输出头的设计借鉴了 verl 的 parallel_linear。

这条「搭模型」调用链最终由 `initialize_model_and_optimizer` 触发：

[model.py:294](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L294) —— `get_model(get_model_provider_func(args, role), ModelType.encoder_or_decoder)`。`get_model` 是 Megatron 的工具，它会按 PP/VPP 的切分对工厂函数传不同的 `pre_process`/`post_process`/`vp_stage`，收集各卡持有的模型片段。

#### 4.1.4 代码实践

**实践目标：** 区分 `--spec` 与 `--custom-model-provider-path` 两条路线，理解 slime 如何把命令行参数变成一个分片模型。

**操作步骤：**

1. 打开 [scripts/models/glm4-9B.sh](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/scripts/models/glm4-9B.sh)，找到 `--spec "slime_plugins.models.glm4" "get_glm_spec"` 这一行。
2. 对照本讲 4.1.3 的源码，在脑海里走一遍：`args.spec = ["slime_plugins.models.glm4", "get_glm_spec"]` → `import_module` 解析出 `get_glm_spec` 函数 → 调 `get_glm_spec(args, config, vp_stage)` → 得到 TE spec → 喂给 `GPTModel`。
3. 现在假设你要训一个 critic。在不改任何源码的前提下，回答：critic 模型与 actor 模型在结构上有什么区别？这个区别是在哪一行代码引入的？

**需要观察的现象 / 预期结果：**

- critic 把输出层从 `hidden_size → vocab_size` 换成 `hidden_size → 1`。
- 这个替换发生在 [model_provider.py:198-199](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model_provider.py#L198-L199)，由 `role == "critic"` 触发；`role` 在工人 `init` 时由 `actor.py` 传入（见 u4-l1）。

> 说明：本实践为「源码阅读型」，不依赖 GPU。若要在本地真正搭出模型，需要安装 Megatron-LM 与 TransformerEngine，并准备一组模型结构参数（`num-layers`、`hidden-size` 等），属于「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1：** 如果一个模型既不是 MoE，又没有指定 `--spec`，slime 会用哪个 spec？依据是什么？

**答案：** 依据 [model_provider.py:128-145](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model_provider.py#L128-L145)：若 `args.transformer_impl == "transformer_engine"`（即 `use_te=True`）则用 `get_gpt_layer_with_transformer_engine_spec`，否则用 `get_gpt_layer_local_spec`。

**练习 2：** `parallel_output=True`（第 172 行）为什么不设成 `False`？

**答案：** 设为 `True` 让 `GPTModel` 输出按 TP 切分的 logits 而非全 gather 的完整 logits。后续 loss 计算是 **TP 感知**的（在切分维度上直接算，再跨 TP 通信），避免了在词表维度（通常很大）上做一次昂贵的 AllGather，是 Megatron 的标准省通信做法。

---

### 4.2 参数冻结：`freeze_model_params`

#### 4.2.1 概念说明

许多 RL 后训练场景不需要训全部参数。例如：

- **LoRA 式轻量微调**：只训某些投影层。
- **固定 embedding**：冻结词嵌入与输出层，只训中间层。
- **只训 MoE 专家**：dense 部分冻结，只让专家参数动。
- **Megatron 服务端**：slime 反向复用 Megatron 做 logprob 预填充时（见 u5-l4），模型只做前向、**不许训任何参数**。

slime 提供两套互斥的正则机制来满足这些需求：

- **白名单 `--only-train-params-name-list`**：先冻结全部，再解冻匹配的参数。语义是「**只训**这些」。
- **黑名单 `--freeze-params-name-list`**：冻结匹配的参数。语义是「**别训**这些」。

二者在参数校验阶段被强制互斥（见 4.2.3）。

匹配规则用的是 Python 的 `re.search`（子串匹配，不是全匹配），所以 `self_attention` 会匹配到所有名字里含 `self_attention` 的参数。

#### 4.2.2 核心流程

`freeze_model_params` 的执行逻辑（伪代码）：

```
freeze_model_params(model, args):
    # 白名单：只训匹配项
    if args.only_train_params_name_list:
        for (name, param) in model.named_parameters():
            param.requires_grad = False                  # 先全部冻结
            for pattern in args.only_train_params_name_list:
                if re.search(pattern, name):
                    param.requires_grad = True            # 命中则解冻
                    break

    # 黑名单：冻结匹配项
    if args.freeze_params_name_list:
        for (name, param) in model.named_parameters():
            for pattern in args.freeze_params_name_list:
                if re.search(pattern, name):
                    param.requires_grad = False           # 命中则冻结
                    break
```

关键性质：

1. 白名单是「**先全冻结，再逐个解冻**」，所以没被任何模式命中的参数一律 `requires_grad=False`。
2. 黑名单只动命中的参数，其余保持模型默认（一般是 `True`）。
3. 两个分支没有 `else`，理论上都能跑；但因为参数校验保证了二者不能同时设置（4.2.3），实际只会执行一个。
4. 冻结发生在**模型刚搭好、优化器还没建**的时刻（见 4.1.3 的 `wrap_model_provider_with_freeze`），所以优化器只会收到 `requires_grad=True` 的参数。

#### 4.2.3 源码精读

[model_provider.py:233-247](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model_provider.py#L233-L247) —— `freeze_model_params` 全文。注意白名单分支（234-240）与黑名单分支（242-247）各自独立遍历 `named_parameters()`，用 `re.search` 做 `pattern` 子串匹配，命中即 `break`。

两套机制的互斥校验在参数中枢：

[arguments.py:1973-1974](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1973-L1974) —— `slime_validate_args` 里显式 `raise ValueError`，禁止同时指定白名单和黑名单。

参数定义与示例直接写在 help 文本里，是理解命名约定的最好材料：

[arguments.py:246-264](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L246-L264) —— 白名单 `--only-train-params-name-list`（`nargs="*"`，即接收多个正则）。help 给了三个典型例子：只训 MoE 专家（`experts`）、只训 Indexer 参数、只训第 20–23 层（`layers\.2[0-3]\.`）。

[arguments.py:266-284](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L266-L284) —— 黑名单 `--freeze-params-name-list`。典型例子：冻结 embedding 与输出层（`embedding output_layer`）、冻结 `linear_fc1`（gate/up 投影）。

一个把「白名单冻结」用到极致的真实案例是 Megatron 服务端：

[server/arguments.py:100](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/arguments.py#L100) —— 服务端把 `only_train_params_name_list` 设成 `["nothing_to_train"]`。因为没有参数名能匹配字符串 `nothing_to_train`，所以白名单分支会**把全部参数冻结**，实现「服务端只做前向、绝不训练」。

[server/arguments.py:124-125](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/server/arguments.py#L124-L125) —— 校验函数 `validate_megatron_server_args` 还断言这个值确实等于 `["nothing_to_train"]`，防止误配。注释（第 99 行）特意提醒：必须保持为 list 而非 str，否则冻结逻辑会逐字符遍历字符串——这是 `freeze_model_params` 对 list 做 `for pattern in ...` 的直接后果。

#### 4.2.4 代码实践

**实践目标：** 用 `--only-train-params-name-list` 写一段正则，只训练模型的 attention 投影层，冻结其余所有参数，并说明如何验证冻结生效。

**操作步骤：**

1. **先确认参数名。** 不同 spec 下 attention 投影的参数名不同。对 TransformerEngine 后端，QKV 合并在 `self_attention.linear_qsv`，输出投影在 `self_attention.linear_proj`；对 local spec，则可能是 `self_attention.query` / `self_attention.key` / `self_attention.value` / `self_attention.dense`。命名形如 `decoder.layers.0.self_attention.linear_qsv.weight`。
2. **写白名单正则。** 用 TE 后端为例，命令行加：
   ```bash
   --only-train-params-name-list self_attention.linear_qsv self_attention.linear_proj
   ```
   `re.search` 是子串匹配，这两个 pattern 会命中每一层的 QKV 投影与输出投影，而 `embedding`、`mlp.linear_fc1`、`mlp.linear_fc2`、各 layernorm 因不命中而被冻结。
3. **如果你只想更宽泛地训「整个 self_attention 子模块」（含其 layernorm），** 可以简化为：
   ```bash
   --only-train-params-name-list self_attention
   ```

**如何验证冻结生效（待本地验证）：** 在搭好模型后、建优化器前，遍历参数统计：

```python
# 示例代码（非项目原有）：在 get_model 返回后插入
trainable = [n for n, p in model.named_parameters() if p.requires_grad]
frozen   = [n for n, p in model.named_parameters() if not p.requires_grad]
print(f"trainable={len(trainable)} frozen={len(frozen)}")
# 期望：只有名字含 self_attention.linear_qsv / self_attention.linear_proj 的参数可训
```

更贴近生产的验证方式：训练启动后查看 wandb / 日志里优化器的可训参数数量（ trainable params ）与梯度范数——冻结的参数不应出现在优化器参数组里，也不会贡献梯度。

> 说明：本实践为「参数阅读 + 配置型」。要真正跑通需要完整 Megatron 环境，故步骤断言标注为待本地验证。

#### 4.2.5 小练习与答案

**练习 1：** `--only-train-params-name-list experts` 会训哪些参数？为什么不需要写完整路径？

**答案：** 因为匹配用 `re.search`（子串匹配），`experts` 会命中所有名字里含 `experts` 的参数（如 MoE 的 `decoder.layers.N.mlp.experts.M...`），从而只训专家参数、冻结 dense 部分。这正是 help 文本里的第一个例子。

**练习 2：** 为什么 Megatron 服务端用白名单 `["nothing_to_train"]` 而不是直接 `freeze_params_name_list=[".*"]`？

**答案：** 两个原因。(1) 白名单「先全冻结再解冻」的语义天然适合「全部冻结」——没有任何参数能命中 `nothing_to_train`，于是全部 `requires_grad=False`，逻辑简洁且不依赖正则。(2) 服务端 `validate_megatron_server_args` 用 `== ["nothing_to_train"]` 做精确断言，白名单这种「占位字符串」更便于校验，确保服务端绝不被改成会训练的配置。

---

### 4.3 分布式初始化：`init`

#### 4.3.1 概念说明

在搭模型之前，必须先**建立分布式环境**：把一组进程组织成 TP/PP/CP/EP/DP 通信组，确定每个进程在每种并行维度上的 rank，并设置好随机种子和 tokenizer。这正是 `initialize.py` 的 `init(args)` 做的事。

这一步完全是 Megatron 的标准初始化，slime 几乎原样复用，只在结尾留了一个 `--custom-megatron-init-path` 钩子，允许用户追加自定义初始化。理解这一步的关键是搞清楚「通信组的 order」——即各并行维度的交织顺序，它决定了哪些卡会被分到同一组。

#### 4.3.2 核心流程

`init(args)` 的执行顺序（伪代码）：

```
init(args):
    set_args(args)                              # 把 args 放进 Megatron 全局变量
    if enable_experimental: set_experimental_flag(True)
    _initialize_distributed(args)               # 建 TP/PP/CP/EP/DP 通信组（核心）
    assert numpy 版本是 1.x                     # Megatron 不支持 numpy 2.x
    _set_random_seed(args.seed, ...)            # 设 Python/numpy/torch/CUDA 随机种子
    _build_tokenizer(args)                      # 建 tokenizer
    init_num_microbatches_calculator(...)       # 初始化微批数计算器（仅过 Megatron 校验）
    if deterministic_mode: 打开 cudnn / 算法确定性
    if tp_comm_overlap: _initialize_tp_communicators()
    if custom_megatron_init_path: 调用户的 custom_init(args)   # 自定义钩子
```

随机种子的「并行感知」设计很巧妙：

\[ \text{seed} = \text{seed}_0 + 100 \cdot \text{pp\_rank} \quad (+\ 10 \cdot \text{dp\_rank}\ \text{若开启}\ \text{data\_parallel\_random\_init}) \]

- **PP 维度必加偏移**：不同流水线阶段（持有不同层）用不同种子，避免初始化出的权重出现跨阶段的对称/重复模式。
- **DP 维度可选偏移**：默认 **不开** `data_parallel_random_init`，即所有 DP rank 用相同种子——这样同一份 micro-batch 在各 DP 复制上初始化一致，DDP AllReduce 时梯度能正确对齐。若开了，则各 DP rank 用不同种子（用于某些需要多样性的场景）。

#### 4.3.3 源码精读

先看入口函数：

[initialize.py:56-104](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/initialize.py#L56-L104) —— `init` 全文。按顺序：`set_args` → 可选 experimental → `_initialize_distributed` → numpy 版本断言 → `_set_random_seed` → `_build_tokenizer` → `init_num_microbatches_calculator` → 可选 deterministic_mode / tp_comm_overlap → 可选自定义钩子。

通信组的建立是核心：

[initialize.py:33-53](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/initialize.py#L33-L53) —— `_initialize_distributed` 调 Megatron 的 `mpu.initialize_model_parallel`，把命令行上的 `tensor_model_parallel_size`、`pipeline_model_parallel_size`、`context_parallel_size`、`expert_model_parallel_size`、`expert_tensor_parallel_size`、`virtual_pipeline_model_parallel_size`（VPP）等尺寸全传进去。注意第 49 行的 `order` 参数：

- 默认 `"tp-cp-ep-dp-pp"`：先 TP，再 CP、EP、DP，最后 PP。
- 若 `use_tp_pp_dp_mapping` 则换成 `"tp-cp-ep-pp-dp"`。

这个 order 决定了 rank 编号的交织方式，进而决定哪些物理 GPU 落在同一通信组——它直接影响 `is_megatron_main_rank`（见下）和日志/指标聚合的归属。

随机种子函数：

[initialize.py:14-30](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/initialize.py#L14-L30) —— `_set_random_seed`。第 23 行给 PP rank 加 `100 * pp_rank` 偏移；第 25-26 行在 `data_parallel_random_init` 时再给 DP rank（不含 CP 维度）加 `10 * dp_rank` 偏移。然后统一设 `random` / `numpy` / `torch.manual_seed` / `tensor_parallel.model_parallel_cuda_manual_seed`（后者还会为 TE 的 RNG tracker、cudagraphable RNG 等做特殊处理）。

自定义初始化钩子：

[initialize.py:100-104](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/initialize.py#L100-L104) —— 若设了 `--custom-megatron-init-path`，用 `load_function` 解析成函数对象后调 `custom_init(args)`。这是「不动模型结构、只追加初始化」的官方扩展点。参数定义见 [arguments.py:1445-1449](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1445-L1449)（注意它没有 help 文本，是个隐藏高级开关）。

最后一个工具函数决定了「谁负责写日志 / 记指标」：

[initialize.py:108-113](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/initialize.py#L108-L113) —— `is_megatron_main_rank` 返回 `True` 当且仅当：DP rank（含 CP）== 0 **且** TP rank == 0 **且** PP rank == 最后一个阶段。为什么是「最后一个 PP 阶段」？因为 loss / logprob 的最终归约发生在流水线的最后一 stage，那里才有完整的输出结果，适合做指标汇总与 wandb 上报。`actor.py` 的 `init` 正是用它判断是否 `init_tracking`（见 u4-l1 的 actor.init，第 75-76 行）。

#### 4.3.4 代码实践

**实践目标：** 理解 `init` 各步的执行顺序与「PP rank 影响种子」的副作用，能用一张时序图描述初始化。

**操作步骤：**

1. 对照 [initialize.py:56-104](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/initialize.py#L56-L104)，把 `init` 的每一步按顺序列成时序图（建通信组 → 设种子 → 建 tokenizer → 初始化微批计算器 → 可选确定性/TP通信/自定义钩子）。
2. 假设 `pipeline_model_parallel_size = 4`，`seed = 1234`，`data_parallel_random_init = False`，手算 4 个 PP 阶段的实际 torch 种子。

**预期结果：**

- 第 0 stage：\(1234 + 100 \times 0 = 1234\)
- 第 1 stage：\(1234 + 100 \times 1 = 1334\)
- 第 2 stage：\(1234 + 100 \times 2 = 1434\)
- 第 3 stage：\(1234 + 100 \times 3 = 1534\)

3. 思考：若误把 `data_parallel_random_init` 打开，DDP AllReduce 会出什么问题？

**答案（待本地验证）：** 默认关闭时，各 DP rank 用相同种子、对应位置参数初始化相同，前向输出一致，DDP 梯度 AllReduce 数学上正确。若开启，各 DP rank 参数初始化不同，虽然 DDP 仍能聚合梯度，但破坏了「DP 复制本应同构」的假设，可能让某些依赖复制一致性的逻辑（如部分指标统计、bucket 对齐）产生非预期行为，故默认关闭。

> 说明：本实践为「源码阅读 + 手算型」，不依赖 GPU 运行。

#### 4.3.5 小练习与答案

**练习 1：** `init` 里的 `assert np.__version__.startswith("1.")`（第 66 行）解决什么问题？

**答案：** Megatron-LM 与 numpy 2.x 不兼容（参见注释里的 Megatron issue #1563）。这个断言在初始化早期就 fail-fast，避免后续出现难以定位的数值/接口错误。

**练习 2：** `init_num_microbatches_calculator`（第 79 行）的注释说「We won't use this」，那为什么还要调？

**答案：** slime 自己用 `build_dp_schedule`（见 u4-l3）计算微批调度，不用 Megatron 的 `num_microbatches`。但 Megatron 内部有些校验逻辑会读取这个全局变量，若不初始化会在后续 `forward_backward` 时报错。所以这里调用只是为了**通过 Megatron 的内部校验**，并非真的使用其结果。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个「定制并验证一个只训 attention 的模型」的端到端阅读任务。

**任务：** 假设你要用 slime + TransformerEngine 后端训练一个 GLM4 模型，但显存有限，只想训练 self-attention 的投影层（QKV 与输出投影），冻结 embedding、MLP、所有 layernorm 与输出层。请完成：

1. **选 spec。** 参考 [scripts/models/glm4-9B.sh](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/scripts/models/glm4-9B.sh)，写出 `--spec` 参数。
   - 参考答案：`--spec "slime_plugins.models.glm4" "get_glm_spec"`
2. **写冻结参数。** 用本讲的白名单机制写出命令行参数，并解释为什么用白名单而不是黑名单。
   - 参考答案：`--only-train-params-name-list self_attention.linear_qsv self_attention.linear_proj`。用白名单是因为「只训少数几类参数、冻结其余绝大多数」时，白名单「先全冻结再解冻」更简洁、不易遗漏；黑名单要列出 embedding、两个 MLP 投影、所有 layernorm、output_layer 等一大堆，容易写漏。
3. **画出从命令行到模型搭好的调用链。** 至少应包含：`MegatronTrainRayActor.init` → `init(args)`（建通信组）→ `initialize_model_and_optimizer` → `get_model_provider_func` → `wrap_model_provider_with_freeze` → `_get_model_provider_func`（选 spec / 搭 GPTModel）→ `freeze_model_params`。
4. **指出 `init(args)` 在这条链里的位置，并解释顺序为什么不能反过来**（即为什么必须先建通信组再搭模型）。
   - 参考答案：`init(args)` 在搭模型之前。因为 `GPTModel` 的权重是按 TP/PP/CP/EP **分片**创建的，分片方式取决于通信组的划分；不先建好通信组，模型就不知道「我这张卡该持有哪些分片」，TP 感知的初始化（如 `model_parallel_cuda_manual_seed`、`column_parallel_linear` 的切分）也无从谈起。
5. **验证设计。** 写出在搭好模型后验证「只有 attention 投影可训」的方法（参考 4.2.4）。

> 全部步骤均不依赖 GPU 运行，重点训练「读源码 + 画调用链 + 配参数」的能力。第 3、4 步的调用链是本讲最值得固化的产出，建议整理进个人笔记。

## 6. 本讲小结

- **模型工厂 `get_model_provider_func`** 把命令行参数翻译成 Megatron `GPTModel`：默认走「选 spec（`--spec` / MoE block spec / TE 或 local spec）→ 组装 `GPTModel` → critic 替换输出头」三步。
- **三种定制入口要分清**：`--spec` 换层规格（最常用，如 `slime_plugins.models.glm4`）、`--custom-model-provider-path` 整体替换工厂（适合 VL 等大改）、`--custom-megatron-init-path` 只追加初始化逻辑。
- **critic 与 actor 的结构差别**仅在输出层：critic 把 `hidden_size → vocab_size` 换成 `LinearForLastLayer(hidden_size → 1)`，由 `role == "critic"` 触发。
- **参数冻结**有白名单（`--only-train-params-name-list`，先全冻再解冻）与黑名单（`--freeze-params-name-list`，只冻命中项）两套**互斥**机制，匹配用 `re.search` 子串匹配；冻结在模型搭好后、优化器建之前生效。
- **`init(args)`** 建立 TP/PP/CP/EP/DP 通信组（order 默认 `tp-cp-ep-dp-pp`）、设并行感知随机种子（PP 必偏移、DP 可选偏移）、建 tokenizer，并在结尾提供自定义初始化钩子。
- **`is_megatron_main_rank`** 用 `(DP==0 且 TP==0 且 PP==最后阶段)` 判定主 rank，决定谁负责指标上报与日志。

## 7. 下一步学习建议

- 本讲只讲了「搭模型」与「初始化」，**模型怎么跑起来**请继续读 [u4-l2 train_one_step 与 pipeline 前后向](u4-l2-train-one-step.md)，看 `forward_only` / `train` 如何复用 Megatron 流水线引擎。
- **数据如何喂进这个分片模型**见 [u4-l3 数据打包、微批调度与 loss mask](u4-l3-data-packing-microbatch.md)，那里会用到本讲 `init` 建立的 DP 通信组。
- **TP 感知 loss 的细节**（呼应本讲 `parallel_output=True`）见 [u4-l4 RL 损失与优势估计](u4-l4-rl-loss-and-advantage.md)。
- 想了解**模型插件与低精度**（`--custom-model-provider-path` 的进阶用法、fp8 权重转换）见 u8-l5。
- 建议同步阅读 Megatron-LM 源码中的 `mpu.initialize_model_parallel` 与 `get_model`，对照本讲的 slime 封装，体会「slime 尽量无损复用 Megatron、只在外层做薄封装」的设计哲学。
