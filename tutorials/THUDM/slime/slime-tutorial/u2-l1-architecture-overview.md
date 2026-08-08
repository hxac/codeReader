# 三模块架构总览

## 1. 本讲目标

本讲是 slime 源码阅读的「地图课」。读完本讲，你应当能够：

- 用一句话说清 slime 的 **training / rollout / data buffer** 三大模块各自负责什么；
- 画出三模块串联成的 **闭环数据流**：rollout 产数据 → data buffer 作桥梁 → training 消费 → 训练后把权重同步回 rollout；
- 解释 **权重同步为什么是 training → rollout 单向**，而不是反向；
- 理解 slime **只选 SGLang 一个 rollout 后端** 的设计权衡，并明白这为什么是后续所有源码阅读的大前提。

本讲不深入任何一个模块的内部实现（那是后续讲义的事），只建立「全局框架」。记住一句话：**slime 把整个 RL 训练组织成一个「采样→训练→同步」的循环，三大模块是这个循环里的三个工位。**

## 2. 前置知识

本讲假设你已经读过 **u1-l1（项目总览）**，知道 slime 是一个连接 Megatron 训练与 SGLang 推理的 LLM RL 后训练框架。在此基础上，我们先补两个最关键的概念。

### 2.1 什么是 RL 的「rollout（采样）」

强化学习（RL）训练的核心是「用当前策略去和环境交互、收集数据，再用这些数据更新策略」。在大模型 RL 里：

- **策略（policy）** 就是那个正在被训练的语言模型；
- **rollout（采样/采样回合）** 就是用当前模型去「生成回答」的过程——给定一批 prompt，让模型生成一段 response。

RL 训练里 rollout 会**反复发生**：每更新几步权重，就要重新采样新数据，因为这些数据必须来自「当前最新的模型」。这跟普通的监督学习「一次性准备好数据集」很不一样，**rollout 是贯穿训练始终的在线动作**。

### 2.2 什么是「on-policy（同策略）」

rollout 产出的数据必须由**当前正在训练的那一份权重**生成，否则数据就「过期」了，这叫 **off-policy（异策略）**问题。要让数据保持 on-policy，就必须在**每次训练更新权重之后、下一次采样之前**，把新权重同步给采样引擎。这正是 slime 里「权重同步」这一环存在的原因，也是本讲第 4.3 节要讲透的点。

> 如果你暂时不理解上面这些也没关系，本讲会用源码把它们具象化。你只需要记住：**模型要边训边采，采的数据必须来自最新权重，所以训练和采样之间需要一座桥。**

## 3. 本讲源码地图

本讲涉及的文件都围绕「三模块怎么连成环」，按层次从外到内排列：

| 文件 | 作用 | 本讲用它看什么 |
|------|------|----------------|
| `README.md` | 项目说明，含官方架构章节 | 三模块的官方定义与架构图 |
| `docs/en/blogs/introducing_slime.md` | 设计理念博客 | slime 为什么这样切模块、为什么选 SGLang |
| `imgs/arch.png` | 官方架构图 | 闭环的视觉总览 |
| `train.py` | 训练主循环入口（极薄） | 用 30 行看到「采样→训练→同步」的闭环 |
| `slime/rollout/data_source.py` | data buffer 的实现 | buffer 如何做「桥梁」 |
| `slime/ray/rollout.py` | rollout 模块的编排器 | rollout 如何产数据、转训练数据 |
| `slime/backends/megatron_utils/actor.py` | training 模块的训练工人 | update_weights 如何把权重推向 rollout |
| `slime/ray/placement_group.py` | 三模块的装配工厂 | 三大对象从哪里被创建出来 |

不用逐行读懂后几个文件——本讲只挑其中**体现三模块关系**的关键几行。

## 4. 核心概念与源码讲解

### 4.1 三模块的职责划分

#### 4.1.1 概念说明

slime 把整个 RL 训练系统切成三个职责清晰的模块。它们不是随便分的，而是对应 RL 训练里三种本质不同的工作：

1. **rollout（采样模块）**：用当前模型去「生成新数据」。在 slime 里它由 **SGLang + router** 实现——SGLang 负责高速推理生成，router 负责把请求分发到多个 SGLang 引擎。它产出的不只是文本，还包括**奖励（reward）/ 验证结果**。
2. **training（训练模块）**：用 rollout 产出的数据去**更新模型权重**。在 slime 里它由 **Megatron** 实现，吃进数据、算梯度、走优化器。
3. **data buffer（数据缓冲模块）**：一个**桥梁**。它管理 prompt 的初始化、自定义数据、以及 rollout 生成方法的对接，让 rollout 和 training 这两个节奏不同、关注点不同的模块不必直接耦合。

这三个名字在源码里反复出现，记住它们的英文：`rollout` / `training`(Megatron) / `data buffer`。

#### 4.1.2 核心流程

三模块的官方定义直接写在 README 的架构章节里：

> - **training (Megatron)**：负责主训练流程，从 Data Buffer 读取数据，训练完成后把参数**同步给 rollout 模块**。
> - **rollout (SGLang + router)**：**生成新数据**（含奖励/验证器输出）并存入 Data Buffer。
> - **data buffer**：一座**桥梁模块**，管理 prompt 初始化、自定义数据和 rollout 生成方法。

注意三个关键动词，它们定义了数据流向：

- rollout **「生成并存入」** → data buffer；
- training **「读取」** ← data buffer；
- training **「同步参数给」** → rollout。

把这三句话连起来，就是一个闭环。

#### 4.1.3 源码精读

官方架构章节和模块描述在这里（建议点开链接，对照架构图读一遍）：

[README.md:L84-L93](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/README.md#L84-L93) — README 的「Architecture Overview」章节，含架构图 `imgs/arch.png` 与三个模块的文字定义。

架构图本身（本讲封面图）位于：

[imgs/arch.png](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/imgs/arch.png) — 官方三模块架构图，展示了 training / data buffer / rollout(SGLang+router) 的闭环布局。

而设计理念博客解释了 slime 为什么选择「不把模块拆成互不相干的训练器 + 采样服务 + agent 框架」，而是让一切流经同一条 training / rollout / Data Buffer 路径：

[docs/en/blogs/introducing_slime.md:L36-L41](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/blogs/introducing_slime.md#L36-L41) — 博客里 slime 的核心主张：提供一个数据生成接口，**允许用户注入自定义逻辑、自由地与 SGLang 服务器交互**，而不是为每种任务 fork 一个框架。

这一段点明了 slime 的模块设计哲学：**复杂度被推到「用户自定义的数据生成逻辑」和「核心库（SGLang、Megatron）」里，框架本身只负责把它们串成闭环**。

#### 4.1.4 代码实践

**实践目标**：把三模块的职责从「文字描述」变成你自己的判断力。

**操作步骤**：

1. 打开上面给的两个永久链接，对照架构图，确认你能在图里分别指出 rollout、data buffer、training 三块。
2. 在笔记里画一张三列的简单表格，列标题是 `模块 / 用的引擎 / 输入什么 / 输出什么`，逐行填写：
   - rollout：引擎填 SGLang；输入填「prompt」；输出填「Sample（含 reward）」。
   - data buffer：引擎填「无（纯 Python）」；输入填「prompt + Sample」；输出填「给 training 用的训练数据」。
   - training：引擎填 Megatron；输入填「训练数据」；输出填「更新后的权重」。

**需要观察的现象**：你会发现自己填完后，三个模块的「输出」恰好能接上下一个模块的「输入」，自然形成一个环。

**预期结果**：得到一张说明三模块「输入/输出」首尾相接的小表。这就是闭环的雏形。

#### 4.1.5 小练习与答案

**练习 1**：如果只看三模块的「输出」，rollout 的输出会流向谁？training 的输出会流向谁？

**参考答案**：rollout 的输出（Sample）流入 data buffer；training 的输出（新权重）流向 rollout。注意 training 的输出**不**回灌给 data buffer——数据是单向消费品。

**练习 2**：data buffer 本身用 GPU 吗？为什么它适合做成「桥梁」？

**参考答案**：data buffer 基本不依赖 GPU（它是纯 Python 的数据结构，见第 4.2 节源码）。正因为它轻量、没有训练/推理那种重型状态，它才能安心地待在 rollout 和 training 之间做缓冲，不被任何一方的重型计算绑架。

---

### 4.2 闭环数据流：从 rollout 到 training

#### 4.2.1 概念说明

知道三模块是什么之后，下一个问题是：**它们在一轮训练里到底怎么交接数据？**

slime 的答案是：把每一轮 RL 训练组织成一个固定节奏的循环，循环里采样、训练、同步这三个动作**每轮必做**。这个循环就是整个框架的主干，入口文件 `train.py` 把它写得非常直白。

这里要先认识贯穿全框架的核心数据载体——**Sample**。一个 Sample 大致就是「一次采样的完整产物」：包含 prompt、模型生成的 tokens、loss_mask（标记哪些 token 要参与训练）、reward（奖励值）、rollout 时的 log_probs 等等。rollout 产出的是一批 Sample，training 消费的也是这批 Sample 转换后的张量。Sample 的详细字段会在 **u3-l1** 展开，本讲只需把它当成「闭环里流动的货物」。

#### 4.2.2 核心流程

`train.py` 里的主循环可以抽象成下面这段伪代码（去掉容错与周期动作后）：

```text
for rollout_id in range(num_rollout):           # 重复 num_rollout 轮
    1. rollout_data = rollout_manager.generate(rollout_id)   # rollout 产数据 → 转 train data
    2. actor_model.async_train(rollout_id, rollout_data)     # training 消费数据、更新权重
    3. actor_model.update_weights()                          # 把新权重同步回 rollout
```

三个步骤恰好对应三个模块：

- 步骤 1 = rollout + data buffer（产出并转换）；
- 步骤 2 = training（消费、训练）；
- 步骤 3 = training → rollout 的权重桥（下一节细讲）。

`generate` 内部其实做了两件事：先让 rollout 用 SGLang 生成 Sample 并算 reward，再把 Sample **转换成 training 能直接吃的张量字典**（tokens、loss_masks、rewards…）。这个「转换」就发生在 data buffer / rollout 模块里，是闭环的关键一环。

#### 4.2.3 源码精读

主循环入口 `train.py` 只有约 100 行，是理解闭环最好的入口：

[train.py:L49-L69](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L49-L69) — 主循环体。其中：

- 第 53 行 `rollout_data_ref = ray.get(rollout_manager.generate.remote(rollout_id))`：**rollout 产数据**（步骤 1）；
- 第 63–69 行 `actor_model.async_train(...)`：**training 消费数据并训练**（步骤 2）。这里 critic 和 actor 都可能训练，本讲先不区分。

注意第 53 行返回的 `rollout_data_ref` 立刻在第 63/69 行被传给 `async_train`——这正是「rollout 产 → training 消费」的交接点，数据载体就是它。

再看 `generate` 内部如何把 Sample 转成训练数据：

[slime/ray/rollout.py:L553-L567](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L553-L567) — `RolloutManager.generate` 方法。第 560 行拿到原始 `data, metrics`（一批 Sample），第 566 行 `data = self._convert_samples_to_train_data(data)` 把 Sample 转成训练张量字典，第 567 行按数据并行（DP）切分后返回。一句话：**rollout 模块同时负责「生成」和「转训练数据」两件事**。

转换函数 `_convert_samples_to_train_data` 产出的字典长这样（节选）：

[slime/ray/rollout.py:L734-L744](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L734-L744) — 训练数据字典的字段：`tokens`（token 序列）、`response_lengths`、`rewards`（奖励）、`loss_masks`（哪些 token 算 loss）、`rollout_ids`（属于哪一轮采样）等。这就是 training 实际吃进去的东西。

而 `RolloutManager` 这个类本身的定位，注释写得很清楚：

[slime/ray/rollout.py:L427-L429](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L427-L429) — 类文档字符串原话：**"The class to run rollout and convert rollout data to training data."**（运行 rollout 并把 rollout 数据转换成训练数据的类）。这正好印证了它在闭环里的双重职责。

最后看 data buffer 这座「桥」是怎么实现的。它的抽象基类定义了四个必备动作：

[slime/rollout/data_source.py:L17-L47](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/data_source.py#L17-L47) — `DataSource` 抽象基类，规定任何数据源都必须实现 `get_samples`（取一批 prompt）、`add_samples`（回灌样本）、`save`/`load`（持久化状态）、`__len__`。这就是「桥梁」对外暴露的统一接口。

带缓冲区的具体实现里，关键是它的 `buffer` 字段：

[slime/rollout/data_source.py:L168-L189](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/data_source.py#L168-L189) — `RolloutDataSourceWithBuffer`：第 171 行 `self.buffer = []`，`get_samples` 时**先从 buffer 取**（第 182 行），不够再向原始数据集要（第 188 行）。这个 buffer 就是「桥梁」能跨轮次续传的根本。

#### 4.2.4 代码实践

**实践目标**：亲手把 `train.py` 主循环「翻译」成阶段标签，建立「代码行 ↔ 闭环阶段」的直觉。

**操作步骤**：

1. 打开 [train.py:L49-L91](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L49-L91)。
2. 给循环体里的每一行打上五选一的标签：`采样 / 训练 / 保存 / 同步 / 评估`。例如：
   - 第 53 行 `rollout_manager.generate(...)` → **采样**；
   - 第 63–69 行 `async_train(...)` → **训练**；
   - 第 76–78 行 `save_model(...)` → **保存**；
   - 第 85 行 `actor_model.update_weights()` → **同步**；
   - 第 90–91 行 `rollout_manager.eval(...)` → **评估**。
3. 数一数：哪些标签每轮都出现？哪些只在特定条件下出现？

**需要观察的现象**：你会发现「采样 / 训练 / 同步」每轮固定出现，而「保存 / 评估」被一个 `should_run_periodic_action(...)` 条件包裹，只在周期边界才触发。

**预期结果**：得到一份带标签的 `train.py` 注释，明确区分「每轮必做」与「周期触发」。这恰好对应 u1-l6 讲过的循环结构，但本讲的关注点是**哪几行属于哪个模块**。

#### 4.2.5 小练习与答案

**练习 1**：rollout 产出的 `Sample`，和 training 实际吃进去的「训练数据字典」，是同一个东西吗？

**参考答案**：不是。`Sample` 是 rollout 的产物（面向采样/奖励语义），而 training 吃的是 `_convert_samples_to_train_data` 把 Sample 转换后的张量字典（`tokens`/`loss_masks`/`rewards`…，面向训练语义）。转换发生在 rollout 模块内部（[rollout.py:L712](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L712)）。

**练习 2**：data buffer 的 `get_samples` 先从哪取数据？这解决什么问题？

**参考答案**：先从 `self.buffer` 取（[data_source.py:L182](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/data_source.py#L182)），不够再向原始数据集要。这让「上一轮没采完/被过滤掉的半成品」能跨轮次续传，是 partial rollout 等高级数据流的基础。

---

### 4.3 权重同步：为什么是 training → rollout 单向

#### 4.3.1 概念说明

闭环里有一根特殊的箭头——**权重同步**。它的方向是固定的：**training → rollout，且单向**。training 训完一轮，把新权重推给 rollout 引擎，让它用新权重去采下一轮数据；但 rollout 永远不会反向把权重推给 training。

为什么是单向？因为：

- **唯一的「真理之源（source of truth）」在 training**。模型权重的更新只发生在 Megatron 的优化器里，rollout 引擎里的权重只是这份真理的一份**副本**。
- **rollout 的职责是「用最新副本去采样」**，它不产生新权重，自然没有东西可回推。
- 回到 2.2 节的 on-policy 要求：要让下一轮数据跟上最新策略，只能让 training → rollout 这条路通畅。反向通路既无必要也不存在。

可以用一个类比理解：training 是「中央厨房」不断更新菜谱，rollout 是「分店」照着最新菜谱做菜。分店不会反向修改中央菜谱。

#### 4.3.2 核心流程

权重同步发生在每轮训练**之后、下一轮采样之前**。在 `train.py` 里它就是一行：

```text
actor_model.update_weights()
```

这一行背后，training 工人（Megatron）会：

1. 向 rollout 管理器要「当前可更新的 rollout 引擎列表」；
2. 与这些引擎建立连接（必要时）；
3. 把自己最新的权重**推送**给它们（具体走 NCCL/磁盘/增量等通道，是 **u5 单元** 的主题）。

注意：权重同步对 rollout 是「被动接收」，rollout 引擎并不主动发起更新。

#### 4.3.3 源码精读

主循环里，每轮训练后立即同步权重：

[train.py:L85](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L85) — `actor_model.update_weights()`，循环里的权重同步点（训练→rollout）。另外训练开始前还有一次初始同步 [train.py:L27](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L27)，保证 rollout 第一次采样就用的是训练侧权重。

真正执行同步的是 Megatron 训练工人，注意它**从 rollout 管理器拿引擎、再向它们推权重**：

[slime/backends/megatron_utils/actor.py:L580-L616](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L580-L616) — `MegatronTrainRayActor.update_weights`。第 587 行 `ray.get(self.rollout_manager.get_updatable_engines_and_lock.remote())` 向 rollout 要「可更新的引擎」，第 615 行 `self.weight_updater.update_weights()` 把训练侧权重推送给它们。整段代码的**数据走向是 training → rollout**，没有任何反向写回 training 权重的逻辑——这就是「单向」的源码证据。

另外注意第 587 行只取 `update_weights=True` 的 server（见 rollout 模块里 `_get_updatable_server` 的过滤逻辑 [rollout.py:L518-L527](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L518-L527)）：**冻结的模型（reference / reward 模型）不会被同步**，只有「正在训练的那份策略」才会把权重推给 rollout。这进一步说明权重同步是为 on-policy 采样服务的、有明确方向的。

#### 4.3.4 代码实践

**实践目标**：从源码里亲自确认「权重同步单向、且只流向可更新引擎」。

**操作步骤**：

1. 打开 [actor.py:L571-L616](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L571-L616)。
2. 在这段 `update_weights` 里找：有没有任何一行代码是「把 rollout 引擎里的权重写回 Megatron 模型」的？（提示：找 `load` / `from rollout` 之类的反向赋值。）
3. 同时打开 [rollout.py:L518-L527](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L518-L527)，确认 `_get_updatable_server` 用 `srv.update_weights` 做了过滤。

**需要观察的现象**：你不会在 `update_weights` 里找到「rollout → training」的反向权重写入；同时会发现 reference/reward 这类冻结模型被排除在同步之外。

**预期结果**：能用自己的话给出两个结论——(1) 权重只从 training 流向 rollout；(2) 只有「可更新」的那份策略模型参与同步。这就是「单向」的代码级证明。

> 本实践为**源码阅读型实践**，不需要运行；如果你想在真实运行里验证，可参考 u1-l4 的启动脚本，开启 `--check-weight-update-equal`（见 [train.py:L29-L30](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L29-L30)），slime 会比对同步前后 training 与 rollout 的权重是否一致——这也是单向同步正确性的自检。具体运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：假如权重同步变成双向（rollout 也能改 training 权重），会出什么问题？

**参考答案**：rollout 引擎是推理用的副本，通常还做了低精度（如 fp8）或路由相关的处理，让它反向写回会污染 training 这份「真理之源」，破坏梯度与优化器状态的一致性，训练会失控。所以必须单向。

**练习 2**：为什么 reference（参考）模型和 reward（奖励）模型不需要每轮同步权重？

**参考答案**：它们是**冻结的**，不参与训练，权重从头到尾不变。`_get_updatable_server` 只挑 `update_weights=True` 的 server，冻结模型被自动排除（[rollout.py:L518-L527](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L518-L527)）。只有「正在被训练的策略」才需要把最新权重同步过去做 on-policy 采样。

---

### 4.4 为什么只选一个 rollout 后端（SGLang）

#### 4.4.1 概念说明

三模块里，training 用 Megatron、rollout 用 SGLang，看起来是很自然的选择。但 slime 做了一个**很有主见**的决定：**rollout 只支持 SGLang 一个推理后端**，而不是像某些框架那样同时兼容 vLLM、TensorRT-LLM 等多个引擎。

这个决定背后的权衡是：

- **多后端的代价**：要兼容多个推理引擎，就得抽象出它们的「公共子集」，结果是把每个引擎最强的特性都藏起来，变成「最低公约数」。
- **单后端的收益**：只优化 SGLang，就能**直接用**它的服务化、路由、缓存、分离部署、权重同步等全部能力，不必在框架里再包一层抽象。这就是 slime 自称的 **"SGLang-native"**。

把这个决定和三模块架构放在一起看，意义就清楚了：**rollout 这一头深度绑定 SGLang，是为了让闭环里「采样」这一工位跑得最快、最稳**——而采样正是 RL 训练里吞吐的命门（见 2.1 节：rollout 在训练全程反复发生）。

#### 4.4.2 核心流程

「SGLang-native」在工程上落地为三件事（来自设计博客）：

1. slime 以 **server-based 模式**在内部启动 SGLang 服务器；
2. slime 对所有 SGLang 参数做**无缝透传**（加 `--sglang-` 前缀即可用），保证所有优化项都能打开；
3. slime 提供 **SGLang-only 调试模式**（`--debug-rollout-only`），方便单独调推理性能。

这三点让「在 slime 里用 SGLang」几乎等同于「独立用 SGLang」，从而能复现 SGLang 的独立性能。

#### 4.4.3 源码精读

README 直接点明这个权衡：

[README.md:L50](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/README.md#L50) — 原话：「**Choosing SGLang as the single rollout backend is also intentional.**」多后端框架不得不抽象出多个引擎的公共子集，会掩盖每个后端最强的特性；slime 则深度优化 SGLang，让 RL 负载能直接使用 SGLang 特有的服务化、路由、缓存、分离与权重同步能力。

设计博客对「SGLang-native」的展开：

[docs/en/blogs/introducing_slime.md:L53-L61](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/blogs/introducing_slime.md#L53-L61) — 因为 RL 训练里有大量在线采样，**推理性能至关重要**，所以 slime 只集成 SGLang、刻意做成 SGLang-native；并以 server-based 模式、参数透传、调试模式三件事来保证「在 slime 里用 SGLang ≈ 独立用 SGLang」。

这个「单后端」决定也体现在源码组织上：rollout 模块几乎只认 SGLang。例如 `RolloutManager` 在初始化时直接启动 SGLang 引擎，并把 router 注入其中：

[slime/ray/rollout.py:L431-L463](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L431-L463) — `RolloutManager.__init__` 里，除非是 `debug_train_only`，否则第 442 行 `start_rollout_servers(args, pg)` 启动 SGLang 引擎群，并配 router。整段没有「选择哪个推理后端」的分支——后端就是 SGLang，这与博客宣称的「single rollout backend」一致。

> 拓展：slime 的生态里有一个叫 **vime** 的项目，它基于 slime 把 rollout 后端换成了 vLLM。这恰恰反过来说明：要换后端，相当于 fork 出一个独立系统（vime），而不是在 slime 内部多挂一个引擎。这印证了「单后端」是有意为之的架构选择。

#### 4.4.4 代码实践

**实践目标**：理解「参数透传」如何让 slime 与 SGLang 保持 native。

**操作步骤**：

1. 回忆 u1-l4：所有 SGLang 参数都加 `--sglang-` 前缀即可使用（如 `--mem-fraction-static` 写成 `--sglang-mem-fraction-static`）。
2. 打开 [slime/ray/rollout.py:L431-L463](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L431-L463)，观察 `RolloutManager.__init__` 里没有任何「适配多推理后端」的 if/else 分支——只有 SGLang 一条路。
3. 在笔记里写一句：**「正因为只有一个后端，slime 不需要抽象层，可以直接用 SGLang 的全部能力。」**

**需要观察的现象**：你会确认代码里不存在「选择推理引擎」的开关，这和 README/博客宣称的「single rollout backend」完全吻合。

**预期结果**：能用「单后端 → 免抽象层 → 直接用 SGLang 全部能力」这条因果链，向别人解释 slime 的 rollout 设计。具体多后端框架的对比运行**待本地验证**（可参考 vime 作为换后端的例子）。

#### 4.4.5 小练习与答案

**练习 1**：用一句话概括 slime 选择单 rollout 后端的核心理由。

**参考答案**：只优化 SGLang 一个后端，就不必抽象掉各引擎的差异，能直接、无损地使用 SGLang 全部的高性能特性，让 RL 训练里最吃吞吐的「采样」环节跑满。

**练习 2**：「参数透传（`--sglang-` 前缀）」和「单后端」这两个设计是怎么互相支撑的？

**参考答案**：正因为只绑定 SGLang 一个后端，slime 才敢把 SGLang 的全部参数直接透传给用户，而不用担心不同后端参数语义冲突；反过来，无缝透传又保证了 SGLang 一升级，slime 几乎零成本就能用上新优化，让「单后端」不意味着「功能受限」。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个**贯穿性小任务**：画一张 slime 的**闭环图**，并解释权重同步的单向性。

**任务要求**：

1. **画图**：在笔记里画出三个方框——`rollout (SGLang+router)`、`data buffer`、`training (Megatron)`，按闭环布局（顺时针或逆时针均可）。
2. **标注箭头与数据载体**：在每条箭头上写清楚流动的「货物」：
   - `rollout → data buffer`：标注 **Sample（含 reward）**；
   - `data buffer → training`：标注 **训练数据张量（tokens / loss_masks / rewards …）**；
   - `training → rollout`：标注 **更新后的权重**（这条是单向！）。
   - 可选：在 `rollout` 旁边再标一个回环箭头，注明「采样时还会产出**指标（metrics：response_len / 奖励分布 / 吞吐 …）**」，用于日志与监控（见 [rollout.py:L1294-L1309](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L1294-L1309)）。
3. **解释单向**：在图下写一段 100 字左右的说明，回答——**为什么权重同步是 training → rollout 单向？** 提示要点：真理之源在 training；rollout 权重只是副本；on-policy 要求新权重流向采样端；反向写回会污染优化器状态。
4. **对照源码自检**：在你的图上，为每条箭头标注一个**源码证据**（永久链接 + 行号），例如：
   - rollout→buffer 用 `generate`（[train.py:L53](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L53)）；
   - buffer→training 用 `async_train` 接 `rollout_data_ref`（[train.py:L63-L69](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L63-L69)）；
   - training→rollout 用 `update_weights`（[train.py:L85](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L85) / [actor.py:L580-L616](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L580-L616)）。

**预期结果**：一张带数据载体标注、带单向箭头、带源码证据的 slime 闭环图。这张图就是后续所有讲义的「总索引」——之后读任何模块，都先回到这张图定位它在闭环里的位置。

## 6. 本讲小结

- slime 把 RL 训练切成 **training(Megatron) / rollout(SGLang+router) / data buffer** 三个模块，对应「更新权重 / 生成数据 / 做桥梁」三种职责。
- 三模块串成一个**闭环**：rollout 产 Sample → data buffer 转训练数据 → training 消费训练 → 训练后同步权重回 rollout，每轮重复。
- 入口 `train.py` 极薄，主循环里 `generate`（采样）、`async_train`（训练）、`update_weights`（同步）三步每轮必做。
- **权重同步是 training → rollout 单向**：真理之源在 training，rollout 权重只是副本；on-policy 采样要求新权重流向 rollout，反向写回会污染优化器。
- slime **只选 SGLang 一个 rollout 后端**，是有意为之：避免多后端抽象成「最低公约数」，从而直接、无损地用上 SGLang 全部高性能特性。
- data buffer 是轻量纯 Python 模块（`DataSource` 抽象 + `buffer`），靠不持有重型 GPU 状态，安心待在 rollout 与 training 之间做缓冲。

## 7. 下一步学习建议

本讲建立了闭环的「全局地图」，接下来建议按这条线深入：

1. **下一讲 u2-l2（Ray 编排：placement group 与资源分配）**：三模块是怎么被 Ray 在 GPU 上「摆好位置」并创建出来的？去看 `create_placement_groups` / `create_training_models` / `create_rollout_manager`，理解 colocate 与资源分配。
2. **再下一讲 u2-l3（三大对象）**：看清 `actor_model` / `critic_model` / `rollout_manager` 的真身与对外接口，把本讲图里的方框对应到具体类。
3. **想先看数据细节**：可跳到 **u3-l1（Sample 数据结构）**，把本讲里「闭环流动的货物 Sample」彻底搞清楚。
4. **想先看同步细节**：权重同步的真正实现（NCCL / 磁盘 / 增量）在 **u5 单元**，建议在学完训练后端（u4）后再读。

阅读建议：每打开一个新模块源码，**先回到本讲画的闭环图**，问自己「这块代码在闭环的哪条箭头上？」——这是贯穿整本手册的读码习惯。
