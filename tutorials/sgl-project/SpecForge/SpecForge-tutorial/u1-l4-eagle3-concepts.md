# EAGLE3 特征式草稿原理

## 1. 本讲目标

[上一讲 u1-l3](./u1-l3-speculative-decoding.md) 解决了「投机解码为什么能加速、为什么还不改变输出」这个一般性问题。但它留下了一个关键追问没回答：**那个负责「提前猜 token」的草稿模型，到底长什么样、吃什么输入、吐什么输出？**

SpecForge 当前支持 6 种草稿方法（见 [u1-l1](./u1-l1-project-overview.md)），其中 **EAGLE3 是主力方法**，也是本手册后续大量源码（特征捕获、训练策略、损失算子）所围绕的中心。所以这一讲专门把 EAGLE3 的三条核心原理讲透。

学完本讲，你应该能够：

- 说清楚**特征式草拟（feature-based drafting）**和「直接把 token 喂给小模型」的区别，以及 EAGLE3 为什么非要走「特征空间」。
- 复述 EAGLE3 从目标模型**抽取 3 层隐藏状态并拼接**的机制，并能用永久链接指出这 3 层是怎么选出来的。
- 解释**训练时测试（Training-time Test, TTT）**如何降低误差累积、提升接受率，并区分**动态草稿树**这一推理期机制与 SpecForge 训练侧职责的边界。
- 看懂官方概念文档 `docs/concepts/EAGLE3.md` 的每一段，并能在真实源码里定位对应的实现。

本讲仍以「概念 + 源码精读」为主，不需要你真正跑训练。它直接承接 [u1-l3](./u1-l3-speculative-decoding.md) 的「目标模型 / 草稿模型 / 接受率」三个术语——本讲会反复用到「接受率」来解释 EAGLE3 每个设计选择的动机。

## 2. 前置知识

进入源码前，先用最通俗的方式补两个概念。

**（1）隐藏状态（hidden state）。**

Transformer 模型内部，每一层都会把输入变换成一个向量序列，这个中间向量就叫「隐藏状态」。你可以把它理解为模型在某一层对这段文本的「内部理解」——它比最终输出的 token 概率要丰富得多：token 概率是词表上的一个分布（几万个数里挑一个），而隐藏状态是几千维的连续向量，携带了大量还没被压缩成「选哪个词」的语义信息。

关键直觉是：**越靠近输出的层，隐藏状态越接近「最终预测」；越靠前的层，越保留原始、底层的特征。** EAGLE3 的一个核心发现就建立在这条直觉上——只看最后一层会丢掉很多有用信息。

**（2）teacher forcing 与误差累积（error accumulation）。**

训练一个「一次预测一个 token」的模型时，最简单的做法是每一步都把**真实的前一个 token**喂给它当输入，这叫 teacher forcing（教师强制）。问题在于：推理时根本没有「真实的前一个 token」，草稿模型只能吃**自己上一步的预测**。如果训练时总吃「正确答案」、推理时却要吃「自己的（可能错的）答案」，两者分布不一致，错误就会越滚越大——这就是「误差累积」。

TTT（训练时测试）正是为了解决这件事，本讲第 4.3 节会展开。

承接 [u1-l1](./u1-l1-project-overview.md) 与 [u1-l3](./u1-l3-speculative-decoding.md) 的术语：

| 术语 | 本讲如何用到 |
|---|---|
| 目标模型（Target Model） | 充当「老师」，EAGLE3 抽取它的隐藏状态当训练信号 |
| 草稿模型（Draft Model） | EAGLE3 训练的那个小模型，通常只有 **1 层** decoder |
| 接受率（acceptance rate） | EAGLE3 几乎所有设计的终极优化目标 |

> 提示：EAGLE3 的草稿模型往往只有一层 dense decoder layer，体积极小。这么小的模型能猜准，靠的不是「模型大」，而是「输入信息足」——这正是特征式草拟要解决的问题。

## 3. 本讲源码地图

本讲以一份概念文档为主，并补充 4 份真实实现源码来「落地」每个概念：

| 文件 | 作用 |
|---|---|
| `docs/concepts/EAGLE3.md` | 官方概念文档，讲清 EAGLE3 三大特性：特征式草拟、训练时测试、动态草稿树 |
| `specforge/algorithms/eagle3/model.py` | EAGLE3 训练模型 `OnlineEagle3Model`，其 docstring 用 5 步讲清了完整数据流，`forward` 里实现了 TTT 循环 |
| `specforge/algorithms/model_providers.py` | `resolve_eagle_capture_layers` 决定从目标模型抽哪 3 层 |
| `specforge/modeling/draft/llama3_eagle.py` | 草稿模型本体的 `project_hidden_states`，完成「3 层拼接 → 投影」 |
| `specforge/algorithms/eagle3/providers.py` | `algorithm_spec` 用 `FeatureContract` 声明 EAGLE3 需要哪些 tensor（含 `hidden_state`/`target`） |

> 这些源码只是用来「印证概念」。它们的工程细节（契约系统、训练策略装配）分别在 [u4 算法注册与契约](./u4-l1-algorithm-contracts.md)、[u6 训练主链路](./u6-l1-training-assembly.md) 中详讲，本讲只取与「原理」直接相关的片段。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，正好对应官方文档的三段叙事：

1. **特征式草拟**——EAGLE3 在「特征空间」而非「token 空间」工作。
2. **多层隐藏状态拼接**——为什么是 3 层、哪 3 层、怎么拼。
3. **训练时测试与动态草稿树**——TTT 如何提升接受率，以及草稿树这条推理期机制与训练侧的边界。

### 4.1 特征式草拟

#### 4.1.1 概念说明

最朴素的投机解码做法，是直接拿目标模型「同家族的小模型」当草稿——例如文档里举的例子：用 `Llama-3.1-8B-Instruct` 给 `Llama-3.1-70B-Instruct` 当草稿。这种「token 式草拟」把草稿模型当成一个独立的小语言模型：**输入是 token，输出也是 token**。

但这个做法有个现实困难：同家族的小模型不一定存在，即使存在，它和目标模型也是「两个各自训练的模型」，草稿并不知道目标模型「心里在想什么」，只能靠自身能力硬猜。

EAGLE3 走了完全不同的路——**特征式草拟（feature-based drafting）**：

> 与其它直接把 token 喂给草稿模型的方法不同，EAGLE3 在**特征空间**工作：它从目标模型抽取隐藏状态，把隐藏状态喂给草稿模型来生成预测。

这样做的本质是：**让草稿模型「站在目标模型的肩膀上」**。草稿模型本身可以只有 1 层、极小，但它吃的不是贫乏的 token，而是目标模型内部富含信息的隐藏状态向量。于是小模型 + 富输入 = 高接受率。官方文档把 EAGLE3 称为这类方法中「state-of-the-art（当前最优）」。

#### 4.1.2 核心流程

特征式草拟把一次「草拟」拆成这样的数据流：

```text
prompt
  │
  ▼
目标模型（教师）──── 抽取 ────► 隐藏状态（富含语义的连续向量）
                                  │
                                  ▼
                            草稿模型（1 层，极小）
                                  │
                                  ▼
                            预测接下来 N 个候选 token
                                  │
                                  ▼
                       交给目标模型验证（见 u1-l3 的 verification 阶段）
```

注意三件事：

- 草稿模型的**输入不是 token，而是向量（隐藏状态）**。
- 草稿模型的**输出仍是 token 概率**（最终要被目标模型验证的候选 token）。
- 目标模型同时扮演两个角色：验证阶段的主宰者（见 u1-l3），以及训练阶段的「特征提供者」。

#### 4.1.3 源码精读

官方概念文档明确点出 EAGLE3 的第一个特性就是特征式草拟，并说明了「3 层隐藏状态拼接」——后者是下一小节的主题，这里先看「特征空间」这条总纲：

[docs/concepts/EAGLE3.md:16-18](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/concepts/EAGLE3.md#L16-L18) —— 官方文档把「特征式草拟」列为 EAGLE3 与其它方法的第一个差异：从目标模型抽 3 层隐藏状态拼成单一特征向量，再喂给草稿模型。

而文档开头也交代了「为什么要训练一个独立小模型」的背景——同家族小模型不一定总有：

[docs/concepts/EAGLE3.md:5-7](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/concepts/EAGLE3.md#L5-L7) —— 说明「同家族小模型当草稿」并不总是可行，于是研究者转而训练一个用目标模型隐藏状态当输入的独立小模型，EAGLE3 是其中 SOTA 且已集成进 SGLang 的代表。

落到代码：EAGLE3 训练模型 `OnlineEagle3Model` 的 docstring 用 5 步讲清了整条「特征式」数据流，其中第 1 步就是「抽取隐藏状态」、第 4 步明确「把投影后的隐藏状态和 embedding 拼一起当草稿输入」：

[specforge/algorithms/eagle3/model.py:100-110](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/model.py#L100-L110) —— docstring 列出 EAGLE3 在线训练的 5 步：抽取隐藏状态 → 拼接 3 层 → 投影到目标隐维度 → 与 embedding 拼接 → 跑 TTT，最终输入维度是 `hidden_size * 2`。

「输入是隐藏状态、输出是 logits（token 概率）」这件事，也写死在 EAGLE3 的算法契约里。`algorithm_spec` 声明 EAGLE3 需要的 tensor 时，`hidden_state`（特征输入）和 `target`（监督信号，即目标模型给出的 token 分布）都在 `required_tensors` 中：

[specforge/algorithms/eagle3/providers.py:117-166](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/providers.py#L117-L166) —— EAGLE3 的 `AlgorithmSpec`：`required_tensors` 含 `input_ids / attention_mask / loss_mask / hidden_state / target`，并声明 `allowed_target_representations={"hidden_state"}`，即目标模型的「表现形式」就是隐藏状态。

> 这里只需建立「输入是特征、输出是 logits」的直觉。`FeatureContract` / `AlgorithmSpec` 这套契约系统的语法在第 [u4-l1](./u4-l1-algorithm-contracts.md) 详讲。

#### 4.1.4 代码实践

**实践目标：** 在源码中亲眼确认「草稿模型吃的是隐藏状态向量、而不是 token」。

**操作步骤：**

1. 打开 [specforge/algorithms/eagle3/model.py:244-264](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/model.py#L244-L264)，看 `OnlineEagle3Model.forward` 的形参表。
2. 找到形参 `hidden_states: torch.Tensor`，注意它的形状注释是 `(batch, seq_len, ...)`，且第 310 行用 `batch_size, seq_length, _ = hidden_states.shape` 取三元组——说明它是一个**向量序列**，不是 token id 序列。
3. 对比第 382 行 `inputs_embeds = self.draft_model.embed_input_ids(state.input_ids)`：token id（`input_ids`）需要先过 `embed_input_ids` 转成 embedding 才能用，而 `hidden_states` 是直接拿来用的。

**需要观察的现象：**

- `hidden_states` 形参直接参与计算，没有任何「先查 embedding 表」的步骤——因为它本身已经是向量。
- `input_ids` 则必须经过 `embed_input_ids` 才能 embedding 化。

**预期结果：** 你能用自己的话指出——在 EAGLE3 里，目标模型的隐藏状态是「现成的向量」，而 token id 只是「需要被查表转换的索引」，两者进入草稿模型的方式不同。这正是「特征式」的落点。

#### 4.1.5 小练习与答案

**练习 1：** 既然隐藏状态比 token 概率信息更丰富，为什么草稿模型的输出还要变回 token 概率（logits），而不是直接输出隐藏状态？

**参考答案：** 因为投机解码的验证阶段（见 [u1-l3](./u1-l3-speculative-decoding.md)）需要拿「候选 token」去和目标模型比对、做拒绝采样。草稿模型最终必须给出「我猜下一个 token 是哪个」的离散候选，所以不管中间走什么空间，输出端都要回到 token 概率上。特征式草拟改进的是**输入端**，输出端语义不变。

**练习 2：** 官方文档说「同家族小模型当草稿」不一定可行。请举一个让它「不可行」的典型场景。

**参考答案：** 目标模型本身已经是该家族里最小的那个（没有更小的同架构兄弟模型可当草稿），或者目标模型是团队自研的、根本没有公开发布的同家族小模型。这时只能像 EAGLE3 这样「现训练一个独立小模型」。

### 4.2 多层隐藏状态拼接

#### 4.2.1 概念说明

知道「吃隐藏状态」之后，下一个问题是：**从目标模型的哪一层抽？**

EAGLE（v1）的做法是只取**最后一层**的隐藏状态。EAGLE3 的关键改进是发现：只看最后一层信息是冗余的——因为最后一层隐藏状态和「最终输出 logits」高度相关，等于把目标模型已经压缩过的信息再喂一遍。

于是 EAGLE3 改成**从 3 个不同深度的层各抽一份，拼起来**：

- 一层**浅层**（靠输入侧，保留底层特征）；
- 一层**中层**（中间深度）；
- 一层**深层**（靠输出侧，接近预测语义）。

这样三份隐藏状态带来「多分辨率」的信息：既有底层原始特征，又有高层语义，比单看最后一层丰富得多。这是 EAGLE3 相对前代提升接受率的核心来源之一。

#### 4.2.2 核心流程

设目标模型的隐藏维度为 \(d\)（`hidden_size`），记从 3 个层抽出的隐藏状态为 \(\mathbf{h}_{l_1}, \mathbf{h}_{l_2}, \mathbf{h}_{l_3}\)，每份维度都是 \(d\)。拼接与投影过程：

\[
\mathbf{h}_{cat} = \mathrm{concat}(\mathbf{h}_{l_1}, \mathbf{h}_{l_2}, \mathbf{h}_{l_3}) \in \mathbb{R}^{3d}
\]

\[
\mathbf{h}_{proj} = W_{proj}\,\mathbf{h}_{cat} \in \mathbb{R}^{d}
\]

即先把 3 份拼成 \(3d\) 维，再用一个线性层 \(W_{proj}\) 压回 \(d\) 维。随后（见 docstring 第 4 步）把这个 \(\mathbf{h}_{proj}\) 与 token 的 embedding \(\mathbf{e}\)（也是 \(d\) 维）再拼一次，得到草稿模型真正的输入：

\[
\mathbf{x} = \mathrm{concat}(\mathbf{h}_{proj}, \mathbf{e}) \in \mathbb{R}^{2d}
\]

这正是 docstring 里写的「最终输入维度是 `hidden_size * 2`」的来历。

```text
层1(浅) ─┐
层N//2-1 ─┼─ concat ─► [3d] ── project(W_proj) ─► [d] ─┐
层N-4(深)─┘                                            ├─ concat ─► [2d] ─► 草稿模型
                                          token embed [d]─┘
```

#### 4.2.3 源码精读

**这 3 层具体是哪几层？** 由 `resolve_eagle_capture_layers` 决定，优先级是「run 覆盖 → draft config → 目标模型层数推导」。当没有任何显式配置时，用目标模型屽数 \(L\) 推出默认三层：

[specforge/algorithms/model_providers.py:188-211](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/model_providers.py#L188-L211) —— 解析 EAGLE 捕获层的函数，关键是第 204 行的默认值。

[specforge/algorithms/model_providers.py:201-204](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/model_providers.py#L201-L204) —— 默认捕获 `[1, num_layers // 2 - 1, num_layers - 4]` 三层，且第 206-210 行强制校验「必须恰好 3 个非负整数」，否则直接抛错（fail-fast）。

对照这三层：

| 层索引（默认） | 深度 | 角色 |
|---|---|---|
| `1` | 浅层 | 底层特征 |
| `num_layers // 2 - 1` | 中层 | 中间表示 |
| `num_layers - 4` | 深层 | 接近输出的语义（注意不是最后一层，留出几层余量） |

> 小提示：`OnlineEagle3Model` 的 docstring（model.py:106）把中层写成 `num_layers // 2`，而真正执行的代码（model_providers.py:204）是 `num_layers // 2 - 1`。两者差 1，以**可执行代码**为准；docstring 是近似表述。

**拼接与投影在哪里实现？** 草稿模型本体的 `project_hidden_states` 方法负责。以 Llama 架构的 EAGLE3 草稿为例：

[specforge/modeling/draft/llama3_eagle.py:1737-1745](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/llama3_eagle.py#L1737-L1745) —— `project_hidden_states`：先断言输入最后一维是 `target_hidden_size * 3`，按 `chunk(3, dim=-1)` 切成 3 份、各自归一化后再 `torch.cat` 拼回，最后过 `self.fc` 线性层压回 `hidden_size`。这正是上面公式里 \(W_{proj}\) 的落点。

而调用它的地方，就在 `OnlineEagle3Model.forward` 的「Step 2」：

[specforge/algorithms/eagle3/model.py:314-315](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/model.py#L314-L315) —— 第 314 行注释「Step 2: project the concatenated hidden states to the target hidden size」，第 315 行调用 `self.draft_model.project_hidden_states(hidden_states)`。

#### 4.2.4 代码实践

**实践目标：** 亲手算一遍「3 层拼接 → 投影」的维度变化，确认它和源码一致。

**操作步骤：**

1. 假设目标模型是 Qwen3-8B，查它的 `num_hidden_layers` 与 `hidden_size`（以模型 `config.json` 为准；如不确定，记 `hidden_size=d`、`num_hidden_layers=L` 即可）。
2. 用上面给的默认公式算出 3 个捕获层：\([1,\ L//2-1,\ L-4]\)。
3. 按 4.2.2 的公式，标出向量维度变化：`3d → project → d → concat(embed) → 2d`。
4. 打开 [llama3_eagle.py:1738](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/llama3_eagle.py#L1738) 的断言 `hidden_states.size(-1) == self.target_hidden_size * 3`，核对你算的 `3d` 与断言一致。

**需要观察的现象：** 投影前是 `3 × hidden_size`，投影后回到 `hidden_size`，再拼 embedding 变 `2 × hidden_size`。

**预期结果：** 维度链与 docstring 第 109 行「input size is `(batch, seq_len, hidden_size * 2)`」吻合。

> 说明：本讲只关注「抽哪 3 层、怎么拼」。这 3 层隐藏状态在**离线特征文件**里以 `hidden_state`（末层）+ `aux_hidden_state`（其余层打包）的形式落盘，字段细节留到 [u5-l3 离线特征生成](./u5-l3-offline-feature-capture.md) 详讲。

#### 4.2.5 小练习与答案

**练习 1：** 为什么深层用的是 `num_layers - 4`，而不是 `num_layers - 1`（最后一层）？

**参考答案：** EAGLE3 的核心发现是「最后一层与最终 logits 信息冗余」。用 `num_layers - 4` 而非最后一层，是为了避开那段与输出高度冗余的表示、同时仍处在「深层语义」区。这也解释了为什么要把浅层、中层一起拼进来——用多分辨率信息补偿单层的不足。

**练习 2：** `project_hidden_states` 里 `chunk(3, dim=-1)` 之前，为什么能断言最后一维恰好是 `3d`？

**参考答案：** 因为上游已经把 3 个层的隐藏状态在特征维度上拼接过了（拼接是 `3d`，每份 `d`），所以进入投影函数时最后一维必然是 `3 × hidden_size`。断言是一道防御：一旦上游拼接出错（比如层数不对），这里会立即 fail-fast，而不是让错误的维度悄悄传到后面。

### 4.3 训练时测试与动态草稿树

#### 4.3.1 概念说明

这一节讲 EAGLE3 的后两个特性。它们看似无关，其实都指向同一个目标：**提升接受率**。

**（A）训练时测试（Training-time Test, TTT）。**

回顾第 2 节讲的「误差累积」：如果训练时用 teacher forcing（每步都喂真实输入），推理时草稿模型却要吃自己的预测，两者分布不一致，错误就会逐步放大、拉低接受率。

TTT 的做法是：**训练时就模拟推理时的自回归过程**。也就是说，在训练循环里，草稿模型每走一步，下一步的输入就用**它自己上一步算出的隐藏状态**（而不是真实的隐藏状态），连走 `length` 步（默认 7），再对这一整段 rollout 计算损失。这样训练目标和推理行为对齐，显著减少误差累积，从而提升接受率。

> 术语澄清：官方文档把它叫 **Training-time Test**（训练阶段做测试式 rollout）；`OnlineEagle3Model` 的代码注释（model.py:104）把它简称为 **test time training (TTT)**。两个名字指的是同一件事，本讲统一用「训练时测试 / TTT」。

**（B）动态草稿树（Dynamic Draft Tree）。**

这是 EAGLE2 提出并在 EAGLE3 沿用的**推理期**机制：草稿模型一次会猜出多个候选 token，但不是全部交给目标模型验证，而是组织成一棵「树」，**只保留最可能被接受的若干分支**再去验证，从而在固定的验证预算下最大化接受数。

这里有一个**重要的职责边界**：动态草稿树的构造、剪枝、调度主要发生在 **SGLang 推理端**，不在 SpecForge 训练侧。SpecForge 的任务是**训练出一个能产生高质量 logits 的草稿模型**——只有 logits 足够准，「最可能被接受」的分支才真的准，树机制才有用武之地。所以本节讲 TTT 训练机制时有源码可看，讲草稿树时只做概念说明并诚实标注它的归属。

#### 4.3.2 核心流程

**TTT 的训练循环**（默认 `length=7` 步）可以用下面伪代码描述（对应 `OnlineEagle3Model.forward` 的 Step 5）：

```text
投影后的隐藏状态 h ← project_hidden_states(目标 3 层拼接)
for idx in range(length):              # 默认 length=7
    embed ← embed_input_ids(input_ids)            # token → embedding
    h_out ← draft.backbone(embed, h, ...)         # 草稿骨干，吃「上一步的 h」
    h ← h_out                                     # 关键：下一步吃自己的输出
    logits ← draft.compute_logits(h)              # 算 token 概率
    loss, acc ← 计算损失与接受率(logits, target)    # 与目标分布比对
    if not 最后一步:
        input_ids, loss_mask, position_ids ← 各自左移一位   # 为下一预测位置对齐
累加 length 步的 loss 作为本步训练损失
```

最关键的一行是 `h ← h_out`：草稿模型下一步吃的隐藏状态，是**它自己上一步算出来的**，而不是目标模型给的真实隐藏状态。这就是「训练时模拟推理」的本质。

**动态草稿树（推理期）**的流程则可以这样示意：

```text
草稿模型一次性猜出多条候选路径
        │
        ▼
按「被目标模型接受的概率」排序，只保留 top 若干分支 → 构成一棵候选树
        │
        ▼
目标模型对这棵树做一次并行验证（见 u1-l3 verification 阶段）
        │
        ▼
保留被接受的分支对应的 token，从首个被拒处重新开始
```

#### 4.3.3 源码精读

**TTT 的循环**完整写在 `OnlineEagle3Model.forward` 的 Step 5，循环 `self.length` 次：

[specforge/algorithms/eagle3/model.py:344-432](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/model.py#L344-L432) —— EAGLE3 的 TTT 主循环：注释「Step 5: run TTT」，`for idx in range(self.length)` 逐拍展开。

其中每个循环体内部的 5 个子步骤清晰可见：

[specforge/algorithms/eagle3/model.py:381-400](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/model.py#L381-L400) —— 一拍的细节：5.1 embed 输入 ids（382）、5.2 跑草稿骨干（386-394）、`hidden_states = hidden_states_out` 把自己的输出传给下一步（397）、5.4 算 logits（400）。

`length` 这个 TTT 长度是个可配置参数，构造函数里默认 `length=7`：

[specforge/algorithms/eagle3/model.py:112-128](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/model.py#L112-L128) —— `OnlineEagle3Model.__init__`，`length` 注释为「TTT length, how many turns to unroll during TTT」，默认 7。

而「非最后一步就左移」对齐下一位置的逻辑，正是误差累积得以被训练的机制：

[specforge/algorithms/eagle3/model.py:428-432](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/model.py#L428-L432) —— `if not is_last:` 时把 `global_input_ids / position_mask / loss_mask` 分别左移（`padding(..., left=False)`），为预测下一个位置对齐。

值得特别留意的是：训练循环里同时算了 `acceptance_rate`（接受率）作为指标——这与 [u1-l3](./u1-l3-speculative-decoding.md) 引入的「接受率」概念直接对接，说明 EAGLE3 的训练目标本就围绕接受率展开（甚至有专门的 LK 损失去直接优化它，这部分留到 [u6-l5](./u6-l5-loss-kernels.md)）：

[specforge/algorithms/eagle3/model.py:175-185](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/model.py#L175-L185) —— `_compute_loss_and_acceptance_rate` 同时返回接受率与损失，体现「训练即优化接受率」。

**动态草稿树**没有训练侧的实现（它是推理期机制），官方文档这样描述它：

[docs/concepts/EAGLE3.md:19](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/concepts/EAGLE3.md#L19) —— 动态草稿树源自 EAGLE2，只存储最可能被目标模型接受的候选 token，以提升接受率。

> 诚实声明：动态草稿树的构造与验证调度主要落在 SGLang 推理端；SpecForge 仓库内没有它的训练侧实现。这里只引官方文档说明概念，不臆造仓库里不存在的代码。

#### 4.3.4 代码实践

**实践目标：** 通过跟踪 TTT 循环，理解「训练时模拟推理」如何落实为 `h ← h_out` 这一赋值。

**操作步骤：**

1. 打开 [specforge/algorithms/eagle3/model.py:364-398](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/model.py#L364-L398)。
2. 在第 386-394 行的 `backbone(...)` 调用里，找到它的 `hidden_states=state.hidden_states` 实参——这是「本拍吃到的隐藏状态」。
3. 紧接着第 397 行 `hidden_states = hidden_states_out`，把骨干的输出赋回 `hidden_states`，下一拍 `state = adapter.step_view(..., hidden_states=hidden_states, ...)` 就会吃到这个新值。
4. 配合第 428-432 行的左移，确认「位置也跟着往前走一格」。

**需要观察的现象：** 从第 2 拍起，喂给骨干的 `hidden_states` 不再是目标模型给的真实隐藏状态，而是上一拍草稿模型自己的输出。

**预期结果：** 你能指出「TTT = 训练时让草稿吃自己的输出连走 7 步」，并解释这为什么能让训练分布贴近推理分布、从而降低误差累积。

> 说明：本实践是「源码阅读型」，不需要真正运行训练。若你想验证 `length` 的影响，可在配置里把它改成别的值（具体配置项在第 [u2-l2](./u2-l2-config-sections.md) 讲）后用 `--plan` 预览，观察 plan 是否变化——但接受率的实际数值需要真训练才能看到，属「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1：** 如果把 TTT 的 `length` 设成 1，训练退化成什么样？为什么这样会损害接受率？

**参考答案：** `length=1` 时循环只跑一拍，草稿模型只预测一步、不存在「吃自己输出」的多步 rollout，等价于单步的 teacher forcing。这样训练目标和「推理时要连猜多步」的真实行为不一致，误差累积的问题没有被训练显式缓解，推理时多步草拟的接受率通常会更低。

**练习 2：** 动态草稿树主要在 SGLang 推理端实现。那么 SpecForge 训练侧对「草稿树有用」贡献了什么？

**参考答案：** 草稿树靠「挑最可能被接受的分支」获益，而「是否可能被接受」取决于草稿模型给出的 logits 是否准确。SpecForge 通过特征式草拟 + 多层隐藏状态 + TTT，训练出一个 logits 质量高的草稿模型，让「挑 top 分支」这件事真的挑得准。换句话说，SpecForge 负责「logits 准」，SGLang 负责「用树把准的 logits 用好」。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个写作型小任务（对应本讲规格里的实践要求）：

**任务：** 用一段文字（150 字左右）说明 EAGLE3 草稿模型的**输入来自哪里、输出是什么**，并对比它与「用同家族小模型当草稿」的做法差异。

**要求覆盖的要点（写之前先回到源码核对）：**

1. **输入来源：** 输入是**目标模型在 3 个不同深度层（默认 `[1, L//2-1, L-4]`）抽出的隐藏状态**，经拼接（`3d`）→ 投影（压回 `d`）→ 再拼 token embedding（`2d`）后，喂给只有 1 层的草稿骨干。依据：[model_providers.py:204](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/model_providers.py#L204) 与 [llama3_eagle.py:1737-1745](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/llama3_eagle.py#L1737-L1745)。
2. **输出是什么：** 输出是**接下来若干 token 的概率分布（logits）**，最终交给目标模型验证。依据：[model.py:400](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/algorithms/eagle3/model.py#L400) 的 `compute_logits`。
3. **对比差异：** 「同家族小模型」是 token 进、token 出的独立小语言模型，与目标模型无信息共享；EAGLE3 则站在目标模型肩膀上（吃它的隐藏状态），模型虽小却信息足、接受率更高。依据：[EAGLE3.md:5-7](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/docs/concepts/EAGLE3.md#L5-L7)。
4. **加分项：** 顺带提一句 TTT（训练时让草稿吃自己的输出连走 `length=7` 步）如何进一步压低误差累积、提升接受率。

**预期结果：** 一段逻辑自洽的文字，能让人读完就明白「EAGLE3 = 特征式输入（多层隐藏状态）+ 小草稿 + TTT 训练」，并能区分它和「同家族小模型」路线的本质差别。

## 6. 本讲小结

- **特征式草拟**：EAGLE3 不像「同家族小模型」那样 token 进 token 出，而是吃**目标模型的隐藏状态**当输入；草稿模型可以只有 1 层，靠「富输入」而非「大模型」获得高接受率。
- **多层隐藏状态拼接**：从目标模型 3 个不同深度的层（默认 `[1, L//2-1, L-4]`）各抽一份，拼成 `3d` → 投影回 `d` → 再拼 embedding 得 `2d` 输入；多分辨率信息是 EAGLE3 相对前代的关键增益。
- **训练时测试（TTT）**：训练循环里草稿模型连走 `length=7` 步、下一步吃自己上一步的输出，让训练分布贴近推理分布，减少误差累积。
- **动态草稿树**：源自 EAGLE2 的**推理期**机制，只保留最可能被接受的候选分支去验证；它的构造在 SGLang 端，SpecForge 的职责是训练出 logits 足够准的草稿模型。
- **统一目标**：EAGLE3 的所有设计最终都指向**接受率**——训练循环里直接把接受率当指标算（甚至有专门损失去优化它）。
- **落点**：EAGLE3 是 SpecForge 的主力方法，本讲建立的「特征 / 隐藏状态 / TTT / 接受率」词汇，是后续 u4（算法契约）、u5（特征捕获）、u6（训练策略与损失）反复要用到的。

## 7. 下一步学习建议

- 想看「EAGLE3 需要的这些 tensor 是怎么用契约正式声明的」→ 下一单元 [u4-l1 算法契约 contracts](./u4-l1-algorithm-contracts.md)，本讲引用的 `FeatureContract` 会在那里逐字段拆解。
- 想看「目标模型的 3 层隐藏状态是怎么被实际抽出来、存成离线特征文件的」→ [u5-l3 离线特征生成 prepare_hidden_states](./u5-l3-offline-feature-capture.md)。
- 想看「TTT 循环里的损失到底怎么算、为什么能直接优化接受率」→ [u6-l5 损失与核心算子](./u6-l5-loss-kernels.md)，那里讲 `LogSoftmaxLoss` 与 LK 损失。
- 若你想先把项目跑起来再回头看这些原理，可以先跳到 [u2-l1 五分钟跑通一次训练](./u2-l1-first-run.md)。
