# DeepSpec 是什么：投机解码与草稿模型训练全栈

## 1. 本讲目标

读完本讲，你应该能够：

1. 用自己的话解释**投机解码（speculative decoding）**为什么能加速大模型推理：草稿模型「提议」、目标模型「验证」的两步协作。
2. 分清**草稿模型（draft model）**和**目标模型（target model）**各自的角色、大小悬殊和约束条件。
3. 说出 DeepSpec 的**三阶段工作流**（数据准备 → 训练 → 评估）中每个阶段的输入和输出。
4. 知道 README 里 12 个 Released Checkpoints 与仓库 `config/` 目录下 12 个配置文件的**一一对应关系**，并能亲手查表找到 `dspark_qwen3_4b_block7` 对应的配置文件。

本讲是整本手册的第一讲，不要求你写过训练代码，但要求你见过「训练一个模型大概需要哪些原料」。后面所有讲义都会反复用到本讲建立的两个词：**draft** 和 **target**。

## 2. 前置知识

- **自回归生成（autoregressive generation）**：语言模型生成文本时是一个 token 一个 token 地往外吐的——每生成一个 token，都要把它拼回输入里，再做一次完整的前向计算。生成长回答时，这个「逐个生成」的过程是推理最耗时的部分。
- **GPU 推理的瓶颈直觉**：解码阶段每次前向计算只处理 1 个新 token，计算量很小，大部分时间花在反复读取模型权重上（访存受限，memory-bandwidth bound）。也就是说：**一次前向「验证」一批 token 的代价，和一次前向「生成」一个 token 的代价差不多**。这是投机解码全部收益的来源。
- **概率分布与采样**：模型每一步输出的是词表上的概率分布 \( p(x) \)，从中采样得到下一个 token。本讲会用到「两个分布的比值」，不需要测度论，只要知道比值大的地方草稿「比目标更敢猜」即可。
- **checkpoint（检查点）**：训练过程中保存的模型权重快照，可以拿去推理或继续训练。

不熟悉以上概念也没关系，本讲用到的部分都会在正文里再解释一遍。

## 3. 本讲源码地图

本讲是总览讲，涉及的核心是两个文档型文件，外加三份「证据文件」用来交叉验证：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md) | 项目自述：定位、三阶段工作流、训练/评估命令、Released Checkpoints 表、支持的三种算法 |
| [NOTICE](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/NOTICE) | 第三方代码归属说明：哪些模块改编自 SpecForge、哪些设计来自 DFlash |
| [config/dspark/dspark_qwen3_4b.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py) | DSpark + Qwen3-4B 的训练配置：草稿模型超参、损失权重、训练日程（本讲的「证据」之一） |
| [config/dflash/dflash_qwen3_4b.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dflash/dflash_qwen3_4b.py) | DFlash 配置：与 DSpark 只差几个开关（「证据」之二） |
| [scripts/train/train.sh](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/train/train.sh) / [scripts/eval/eval.sh](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/eval/eval.sh) | 训练与评估的启动脚本，列出全部可用 config（「证据」之三） |

## 4. 核心概念与源码讲解

### 4.1 投机解码基本概念：propose 与 verify

#### 4.1.1 概念说明

大模型逐 token 生成很慢，但每次前向的算力其实没吃满。投机解码的思路是：

- 找一个**小而快**的草稿模型，先「自作主张」地连续猜出 \( k \) 个候选 token（这一步叫 **propose / 提议**），小模型的推理成本很低；
- 再让**大**的目标模型做**一次**前向计算，把这 \( k \) 个候选 token **一起**算出概率，逐个检查哪些猜对了（这一步叫 **verify / 验证**）；
- 被验证通过的 token 直接采纳，猜错的位置由目标模型亲自采样一个「兜底」token 修正。

由于验证 \( k \) 个 token 和生成 1 个 token 的耗时近似相同（都受访存支配），只要草稿模型猜得够准，一轮下来就能用「约 1 次大模型前向 + \( k \) 次小模型前向」的代价换到远多于 1 个的有效 token。

关键的一点：精心设计的验证规则（拒绝采样）能保证**最终输出序列的分布与目标模型单独生成时完全一致**——投机解码是一种「无损加速」，不是牺牲质量换速度。这个验证规则的数学细节我们留到单元 6（u6-l3）再深挖，本讲先记住结论。

#### 4.1.2 核心流程

一轮投机解码的伪代码：

```text
已生成前缀 x_1..x_t
循环：
    1. propose：草稿模型 q 基于前缀连续采样 k 个草稿 token：
       d_1, ..., d_k ~ q(· | 前缀, d_1..d_{i-1})
    2. verify：目标模型 p 对「前缀 + 全部草稿」做一次前向，
       得到每个位置上目标分布 p(x)
    3. 对 i = 1..k 逐个判定：
       以概率 min(1, p(d_i)/q(d_i)) 接受 d_i
       一旦某个 d_i 被拒绝，停在该位置
    4. commit：采纳被接受的前缀，并在第一个被拒位置
       （或第 k 个位置之后）由目标分布补采 1 个 token
    5. 更新前缀，回到第 1 步
```

两个可以推导出的性质：

- **每轮至少前进 1 个 token**：即使草稿全猜错，兜底采样也会贡献 1 个目标模型「亲笔」的 token，所以投机解码最差也只是略慢于普通解码。
- **接受长度决定加速比**：设第 \( i \) 个草稿 token 的接受概率为 \( a_i = \min(1, p(d_i)/q(d_i)) \)，则一轮中「前 \( i \) 个全部被接受」的概率是连乘 \( \prod_{j \le i} a_j \)，期望接受的草稿 token 数为

\[ \mathbb{E}[\alpha] = \sum_{i=1}^{k} \prod_{j=1}^{i} a_j \]

每轮提交的 token 数约为 \( \mathbb{E}[\alpha] + 1 \)（多出的 1 来自兜底 token）。草稿分布 \( q \) 越接近目标分布 \( p \)，\( a_j \) 越接近 1，加速越明显——**这正是「训练一个更好的草稿模型」这件事的全部意义，也是 DeepSpec 这个仓库存在的理由**。

#### 4.1.3 源码精读

README 开头一句话给 DeepSpec 定位，说明它就是围绕上面这套 propose/verify 流程建的训练+评估代码库：

> DeepSpec is a full-stack codebase for training and evaluating draft models for speculative decoding.（原文见 [README.md:1-3](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md#L1-L3)，这段把「draft models」「speculative decoding」「training and evaluating」三个关键词都点出来了。）

评估阶段对投机解码成效的度量方式写在 README 的 Evaluation 一节：

- [README.md:42-51](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md#L42-L51)：说明评估就是「measure speculative-decoding acceptance on benchmark tasks」——度量的是**接受情况**（acceptance），即在 gsm8k、math500 等基准上草稿提议的 token 被目标模型接受的比例与长度。这正对应上面公式里的 \( a_i \) 和 \( \mathbb{E}[\alpha] \)。

> 本讲只需建立「评估 = 测接受率/接受长度」的直觉；评估循环的逐行源码在 u6-l2 精读。

#### 4.1.4 代码实践

**实践目标**：不用任何 GPU，用一段纯 Python 模拟「propose → verify → commit」循环，亲眼看到「每轮至少前进 1 个 token」和「接受长度决定加速」。

**操作步骤**（示例代码，非项目源码）：

1. 新建一个临时脚本（放在仓库外的任意目录即可，不要写进仓库），内容如下：

```python
import random

VOCAB = ["A", "B", "C"]
# 目标分布 p 与草稿分布 q：两个「模型」在这里被抽象成三个数字
p = {"A": 0.6, "B": 0.3, "C": 0.1}
q = {"A": 0.5, "B": 0.4, "C": 0.1}   # 草稿还算接近目标

def sample(dist):
    r, acc = random.random(), 0.0
    for tok, prob in dist.items():
        acc += prob
        if r <= acc:
            return tok

rounds, total_tokens, total_accepts = 0, 0, 0
while total_tokens < 1000:
    drafts = [sample(q) for _ in range(7)]           # 1. propose：7 个草稿
    accepted = 0
    for i, d in enumerate(drafts):                   # 2. verify：逐个按 min(1, p/q) 判定
        a = min(1.0, p[d] / q[d])
        if random.random() < a:
            accepted += 1
        else:
            break                                    # 首个被拒即停
    total_accepts += accepted
    total_tokens += accepted + 1                     # 3. commit：接受前缀 + 1 个兜底 token
    rounds += 1

print(f"轮数={rounds}, 平均每轮提交={total_tokens/rounds:.2f}, 平均接受草稿={total_accepts/rounds:.2f}")
```

2. 运行 `python` 该脚本 3 次，记录平均每轮提交的 token 数。
3. 把 `q` 改成 `{"A": 0.1, "B": 0.2, "C": 0.7}`（一个和目标分布严重不符的「坏草稿」），再跑 3 次。

**需要观察的现象**：

- 好草稿下，平均每轮提交明显大于 1（约 2~3）；换成坏草稿后迅速逼近 1，但**永远不会低于 1**。
- 兜底机制保证了最坏情况也不倒退。

**预期结果**：加速感直接来自「每轮提交数」这个数字。该脚本是「待本地验证」的演示——具体数值取决于随机种子，但量级趋势是确定的。

#### 4.1.5 小练习与答案

**练习 1**：为什么「验证 k 个草稿 token」可以和「生成 1 个 token」放在同一次前向里做，从而几乎不额外花时间？

**答案**：Transformer 前向对一批连续 token 本来就是并行计算的；解码阶段的耗时主要花在读取权重（访存受限），而不是算 token 数。把 k 个待验证 token 拼成一次前向，权重只读一遍，因此验证 k 个 token 的耗时 ≈ 生成 1 个 token 的耗时。

**练习 2**：如果草稿模型的分布 \( q \) 在某个 token 上比目标 \( p \) 更「自信」（\( q(x) < p(x) \)），接受概率是多少？草稿「过度自信」（\( q(x) > p(x) \)）时又是多少？

**答案**：接受概率是 \( \min(1, p(x)/q(x)) \)。当 \( q(x) < p(x) \) 时比值为 1（必接受）；当 \( q(x) > p(x) \) 时接受概率小于 1，草稿在这个 token 上越自信、被拒得越狠。这样无论草稿好坏，最终输出分布仍等于目标分布。

**练习 3**：投机解码最坏情况下（草稿全错）会比普通解码慢多少？

**答案**：每轮仍然提交恰好 1 个兜底 token，额外代价只是 k 次小模型前向。由于草稿模型很小，这部分开销通常远小于一次目标模型前向，所以最坏情况大约等于普通解码速度，略慢一点点。

### 4.2 草稿模型与目标模型：DeepSpec 的三种草稿算法

#### 4.2.1 概念说明

- **目标模型（target model）**：你真正想加速的那个大模型，例如 `Qwen/Qwen3-4B` 或 `google/gemma-4-12B-it`。它只负责验证和兜底采样，**永远不需要重新训练**。
- **草稿模型（draft model）**：为某个目标模型量身定制的小模型。它必须「会说目标模型的话」——即它的分布 \( q \) 要尽量接近 \( p \)。DeepSpec 训练的就是它。

DeepSpec 收录了三种草稿算法（[README.md:67-69](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md#L67-L69) 给出了三个 arXiv 链接）：

| 算法 | 提议方式 | 本仓库中的实现方式 |
| --- | --- | --- |
| **DSpark** | 半自回归：一次提议一「块」token（block-wise），带置信度头，可按置信度提前截断提议 | `deepspec/modeling/dspark/`，主打算法（对应论文即 README 引用的 DSpark） |
| **DFlash** | 同为块式（block-wise）提议 | 复用 DSpark 的同一套模型与训练代码，只改配置关掉 markov 头、置信度头，退化为纯 CE 损失 |
| **Eagle3** | 链式逐 token 提议，拼接多层目标隐状态作输入，训练时做 train-time test | `deepspec/modeling/eagle3/`，改编自 SpecForge（见 NOTICE） |

三者的共同点：**草稿主干都非常小**，而且**都以目标模型的中间层隐藏状态为输入特征**——这正是 4.3 节数据流水线要花 38 TB 磁盘去预缓存的东西。

#### 4.2.2 核心流程

三种算法在仓库里的组织关系可以画成：

```text
                    DeepSpec 草稿算法族
                    │
        ┌───────────┼──────────────┐
   DSpark 代码     Eagle3 代码      （无独立 DFlash 代码）
   modeling/dspark  modeling/eagle3
        │                               │
        ├── dspark_*.py 配置             └── eagle3_*.py 配置
        └── dflash_*.py 配置
            （同一 trainer，关掉 markov/置信度/L1）
```

训练一个草稿模型时，「草稿有多小、吃目标哪几层隐状态、怎么算损失」全部由一份 `config/<算法>/<算法>_<目标>.py` 描述，流程是：

1. 读取配置，得到目标模型名与草稿超参；
2. 从目标模型 config 派生草稿 config（例如把隐藏层数压到 5 层）；
3. 用预缓存的目标隐状态作为输入特征训练草稿主干。

#### 4.2.3 源码精读

**DSpark 草稿有多小、吃什么** —— 看 DSpark + Qwen3-4B 的配置：

- [config/dspark/dspark_qwen3_4b.py:10-30](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L10-L30)：`model` 字典里写明了草稿模型的全部超参——`block_size=7`（一次提议 7 个 token 的块）、`num_draft_layers=5`（草稿主干只有 5 层，由 [deepspec/modeling/dspark/qwen3/config.py:40](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/config.py#L40) 直接设成草稿 config 的 `num_hidden_layers`）、`target_layer_ids=[1, 9, 17, 25, 33]`（训练时缓存并使用目标模型这 5 层的隐藏状态）、以及 markov 头/置信度头/损失权重等开关。
- `target_layer_ids` 并非摆设：缓存生成脚本按它抓取对应层的隐状态（见 [scripts/data/prepare_target_cache.py:123](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L123)，从捕获的隐状态中取出 `target_layer_ids` 指定的各层）。

**DFlash 是 DSpark 的配置化简化版** —— 对比两份配置的 `model` 字典：

- [config/dflash/dflash_qwen3_4b.py:18-27](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dflash/dflash_qwen3_4b.py#L18-L27)：注释写得很直白——`markov_rank=0`「Disable markov head」、`confidence_head_alpha=0.0`「Disable confidence head」、`ce_loss_alpha=1.0, l1_loss_alpha=0.0`「CE-only loss」。
- 而它 import 的 trainer 与 DSpark 完全相同：[config/dflash/dflash_qwen3_4b.py:3](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dflash/dflash_qwen3_4b.py#L3) 里 `from deepspec.trainer import Qwen3DSparkTrainer`，连 `train.trainer_cls` 都是同一个类（第 31 行）。也就是说：**DFlash 没有独立模型代码，是「关掉几个开关」的 DSpark**。

**代码血缘** —— NOTICE 记录了哪些模块改编自哪个上游项目：

- [NOTICE:10-26](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/NOTICE#L10-L26)：Eagle3 的建模/损失/优化器/评估等代码改编自 SpecForge（Apache-2.0），并列出了受影响的文件清单（如 `deepspec/modeling/eagle3/loss.py`、`deepspec/utils/optim.py`）。
- [NOTICE:32-36](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/NOTICE#L32-L36)：DFlash 的草稿模型设计与训练配方来自 z-lab/dflash（MIT）。
- README 的致谢一节与之呼应：[README.md:79-85](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md#L79-L85) 说明 SpecForge 贡献了整体训练框架与 Eagle3 实现，DFlash 贡献了设计配方，并欢迎新算法贡献。

#### 4.2.4 代码实践

**实践目标**：用 `diff` 亲眼确认「DFlash = DSpark 换配置」，并数出究竟差了几个字段。

**操作步骤**：

```bash
# 在仓库根目录执行
diff config/dspark/dspark_qwen3_4b.py config/dflash/dflash_qwen3_4b.py
```

**需要观察的现象**：diff 输出只集中在两处——`exp_name`（`dspark_block7_qwen3_4b` vs `dflash_block7_qwen3_4b`，第 7 行）和 `model` 字典里的 4 个超参（`markov_rank`、`confidence_head_alpha`、`ce_loss_alpha`、`l1_loss_alpha`，第 18~29 行）。`trainer_cls`、层数、`target_layer_ids`、训练日程全部一致。

**预期结果**：你会得出结论——算法差异在本仓库里被压缩成了**配置差异**，这正是第 5 单元（u5-l3 三算法对比）要展开的主题。本实践只读文件、不改文件，可放心执行。

#### 4.2.5 小练习与答案

**练习 1**：目标模型和草稿模型，哪个需要训练？为什么 DeepSpec 的仓库里没有 `Qwen/Qwen3-4B` 的权重文件？

**答案**：只训练草稿模型。目标模型只用来验证与兜底采样，直接从 Hugging Face 下载（配置里 `target_model_name_or_path=QWEN_3_4B` 指向 HF 仓库 ID），所以本仓库只存代码和配置，不存目标权重。

**练习 2**：DSpark 配置里 `num_draft_layers=5` 而 `target_layer_ids=[1, 9, 17, 25, 33]` 有 5 个层号，这两个「5」是同一回事吗？

**答案**：不是。`num_draft_layers=5` 指草稿主干自身有 5 层 transformer（被写成草稿 config 的 `num_hidden_layers`）；`target_layer_ids` 的 5 个层号指训练时要缓存/使用目标模型内部的第 1、9、17、25、33 层隐藏状态作为输入特征。一个是「自己多深」，一个是「看对方哪些层」。

**练习 3**：既然 Eagle3 代码改编自 SpecForge、DFlash 设计来自 z-lab/dflash，DSpark 又是本仓库主推的算法，那 `deepspec/utils/optim.py` 这种公共文件最可能源自哪里？

**答案**：根据 [NOTICE:10-26](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/NOTICE#L10-L26) 的文件清单，`deepspec/utils/optim.py` 属于从 SpecForge 改编的文件（优化器代码）。

### 4.3 DeepSpec 三阶段工作流与 checkpoint 对应关系

#### 4.3.1 概念说明

DeepSpec 把「训练一个草稿模型」组织成严格串联的三个阶段，**每一阶段的产出就是下一阶段的原料**（README 原话：Run the stages in order — each stage's output feeds the next）：

```text
阶段 1 数据准备                阶段 2 训练                  阶段 3 评估
──────────────                ──────────                  ──────────
下载 prompt 数据      ──►     读取 target cache    ──►    加载 draft checkpoint
用目标模型重写答案            训练草稿模型                   目标模型验证草稿提议
预计算 target cache          产出 step_* checkpoints      产出 accept 等指标
（隐状态缓存，可能非常大）
```

- **为什么需要「重写答案」**：训练数据里的 assistant 回复不是目标模型写的，分布对不上；必须让目标模型亲自把每条回复重新生成一遍，草稿学到的才是「这个目标模型的口吻」。
- **为什么需要「target cache」**：4.2 节说过，草稿以目标模型中间层隐状态为输入。训练时每个 step 都要用到这些隐状态，如果每次都现场跑一遍 4B/8B/14B 的目标模型，训练会被拖死。所以提前离线算好、存到磁盘（代价是惊人的磁盘占用）。
- **评估**回答的问题是：这个训练出来的草稿，在真实投机解码里平均每轮能被接受几个 token。

#### 4.3.2 核心流程

把三个阶段落到文件层面（均为 README 与脚本中明确记录的行为）：

```text
阶段 1  scripts/data/
        download_and_split.py   → train_datasets/perfectblend_train.jsonl（训练 prompt）
                                 eval_datasets/*.jsonl（留出评测集）
        generate_train_data.py  → perfectblend_train_regen.jsonl（目标模型重写的答案）
        prepare_target_cache.py → ~/.cache/deepspec/qwen3_4b_target_cache（隐状态缓存）

阶段 2  scripts/train/train.sh → python train.py --config config/dspark/dspark_qwen3_4b.py
                                 检查点写入 ~/checkpoints/<project_name>/<exp_name>/step_*

阶段 3  scripts/eval/eval.sh   → python eval.py --target_name_or_path ... --draft_name_or_path ...
                                 在 eval_datasets/ 各基准上输出接受率/接受长度
```

#### 4.3.3 源码精读

**三阶段总纲**：

- [README.md:15-21](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md#L15-L21)：Workflow 一节用三行列出 Data Preparation / Training / Evaluation 及各自一句话职责——这是整本手册的目录页。
- [README.md:23-29](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md#L23-L29)：数据准备的三个子步骤（下载切分、重写答案、建 target cache），并给出著名警告——默认 `Qwen/Qwen3-4B` 设置下缓存**约 38 TB**。这个数字直观体现了「多层隐状态 × 每个位置 × 每条样本」的体积，也是阶段 1 最重要的工程约束。

**阶段 2 与 3 的启动方式**：

- [README.md:31-39](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md#L31-L39)：训练只需 `bash scripts/train/train.sh`；通过把 `config_path` 指向 `config/` 下的不同文件来选择算法与目标模型；默认假设单机 8 卡。
- [scripts/train/train.sh:38-40](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/train/train.sh#L38-L40)：实际启动命令——`CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 python train.py --config config/dspark/dspark_qwen3_4b.py --opts "data.target_cache_path=..."`，用 `--opts` 把阶段 1 产出的缓存目录喂给训练。
- [scripts/eval/eval.sh:6-14](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/eval/eval.sh#L6-L14)：评估命令——`python eval.py --target_name_or_path Qwen/Qwen3-4B --draft_name_or_path ~/checkpoints/deepspec/dspark_block7_qwen3_4b/step_latest`。注意三个信息：目标必须与训练草稿时的目标**同一个**；draft 路径里的 `deepspec/dspark_block7_qwen3_4b` 正是训练配置里的 `project_name/exp_name`（[config/dspark/dspark_qwen3_4b.py:6-7](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L6-L7)）；`step_latest` 是指向最新检查点的链接。

**Released Checkpoints 与 config/ 的一一对应**：

- [README.md:53-62](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md#L53-L62)：表格是 3 种算法 × 4 个目标模型 = **12 个 checkpoint**，例如 `deepseek-ai/dspark_qwen3_4b_block7`。README 明确说每个 checkpoint「is the direct output of the corresponding training configuration under config/」。
- [scripts/train/train.sh:8-23](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/train/train.sh#L8-L23)：脚本头部注释列出了全部 12 个可用配置，恰好是 `config/{dflash,dspark,eagle3}/{算法}_{目标}.py` 的 3×4 矩阵——与 checkpoint 表格逐格对应。
- 命名对照规则：checkpoint 名 `dspark_qwen3_4b_block7` 中的 `block7` 来自配置里的 `block_size=7`（[config/dspark/dspark_qwen3_4b.py:12](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L12)），于是它对应的配置文件就是 **`config/dspark/dspark_qwen3_4b.py`**。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：回答两个问题——① 为什么训练草稿模型需要目标模型的中间层隐藏状态？② Hugging Face 上的 `dspark_qwen3_4b_block7` 对应本仓库哪个配置文件？

**操作步骤**：

1. 通读 [README.md](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md)，重点看 Workflow（L15-21）、Data Preparation（L23-29）、Released Checkpoints（L53-62）三节。
2. 打开 DSpark 论文摘要（README 引用链接 <https://arxiv.org/abs/2607.05147>，浏览器阅读摘要即可）。
3. 结合本讲 4.2.3 引用的两处源码证据思考问题①：草稿主干只有 5 层（`num_draft_layers=5`），输入特征却包括目标模型 5 个中间层的隐状态（`target_layer_ids`，由 [scripts/data/prepare_target_cache.py:123](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L123) 抓取），且缓存大到 38 TB。
4. 查 [README.md:58-62](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md#L58-L62) 的表格找到 `deepseek-ai/dspark_qwen3_4b_block7` 所在行列（DSpark 行 × Qwen3-4B 列），再对照 [scripts/train/train.sh:14-18](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/train/train.sh#L14-L18) 列出的 dspark 配置清单确定文件路径。
5. 写一段约 200 字的中文笔记回答问题①（写在自己的笔记本里即可）。

**需要观察的现象 / 预期结果**：

- 问题②的答案可以直接验证：**`config/dspark/dspark_qwen3_4b.py`**。验证依据有三条——表格说 checkpoint 是 config 的直接产物；train.sh 注释清单里 dspark × qwen3_4b 只有这一个文件；文件内 `exp_name = "dspark_block7_qwen3_4b"` 与 eval.sh 默认 draft 路径、配置内 `block_size=7` 互相咬合。
- 问题①的参考要点（可对照自己的笔记）：① 草稿主干极小（5 层），若只看 token id，信息量不足以逼近目标分布；② 目标模型中间层隐状态是「目标模型对前文的深层理解」，把它作为输入特征等于让草稿站在目标肩膀上，用极少的参数复用目标已完成的计算；③ 隐状态每个位置都要用、训练要反复扫多轮，所以必须离线预缓存（38 TB 的由来）——训练时读缓存而非现场跑目标模型。

**待本地验证**：论文摘要内容需你实际打开 arXiv 页面阅读，本讲不代读。

#### 4.3.5 小练习与答案

**练习 1**：如果我把 `eval.sh` 里的 `target_name_or_path` 从 `Qwen/Qwen3-4B` 换成 `Qwen/Qwen3-8B`，但 draft checkpoint 仍是针对 4B 训练的，会发生什么？

**答案**：结果不可信。eval.sh 第 6 行注释明确要求 target 必须匹配训练草稿时用的目标（"Match this to the target model used by the draft checkpoint"）。草稿是在 4B 的输出分布和隐状态上训练的，换目标后隐状态空间与分布都不对齐，接受率会显著劣化。README 的 IMPORTANT 提示（[README.md:64-65](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md#L64-L65)）也强调换场景应重新微调草稿。

**练习 2**：`deepseek-ai/eagle3_gemma4_12b_ttt7` 对应哪个配置文件？`ttt7` 大概指什么？

**答案**：`config/eagle3/eagle3_gemma4_12b.py`（对照 train.sh 注释清单 L19-23 中 eagle3 部分）。`ttt` 指 Eagle3 的 train-time test 训练技巧，`7` 与其展开长度相关（具体机制在 u5-l1 详解；此处能从命名定位到配置文件即可）。

**练习 3**：为什么训练检查点目录是 `~/checkpoints/deepspec/dspark_block7_qwen3_4b/` 这样两级结构？

**答案**：这两级正是训练配置里的 `project_name = "deepspec"` 和 `exp_name = "dspark_block7_qwen3_4b"`（[config/dspark/dspark_qwen3_4b.py:6-7](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L6-L7)），由配置的 `finalize_cfg` 钩子拼成 checkpoint 目录（同文件 L60-65）。同一 project 下跑多个实验互不干扰。

## 5. 综合实践

**任务：给「论文表格里的一格」做一次完整的档案卡。**

从 [README.md:58-62](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md#L58-L62) 的表格中任选一个 checkpoint（不要选本讲已示范的 `dspark_qwen3_4b_block7`），为它制作一张档案卡，须包含：

1. **身份**：HF repo ID、算法、目标模型。
2. **配置**：对应的 `config/` 文件路径；打开该文件，抄下 `target_model_name_or_path`、`block_size` 或 ttt 相关参数、`num_draft_layers`、`target_layer_ids`。
3. **血缘**：依据 [NOTICE](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/NOTICE) 判断它的算法实现改编自哪个上游项目。
4. **工作流**：写出训练它需要的三阶段命令各自的核心脚本/文件名（提示：数据阶段见 [scripts/data/README.md](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/README.md)，训练/评估见本讲 4.3.2）。
5. **一句话**：用本讲 4.1 的语言说明它在投机解码中扮演什么角色（提示：它是 propose 一方还是 verify 一方？）。

完成后自查：档案卡上的每个文件路径都应该真实存在于仓库中（可用 `ls` 验证）。

## 6. 本讲小结

- 投机解码 = 小草稿模型 **propose** 一串候选 token + 大目标模型一次前向 **verify**；验证采用拒绝采样，输出分布与目标模型单独生成完全一致，是「无损加速」。
- 每轮至少前进 1 个兜底 token；加速比由期望接受长度 \( \mathbb{E}[\alpha] = \sum_i \prod_{j \le i} \min(1, p/q) \) 决定，所以**把草稿训得像目标**是全部意义所在。
- DeepSpec 是「数据准备 → 训练 → 评估」三位一体的草稿模型全栈代码库；阶段间以文件交接：JSONL 数据 → target cache（默认设置约 38 TB）→ `step_*` checkpoints → accept 指标。
- 三种草稿算法中，DSpark 是主推（块式提议 + 置信度头），DFlash 是**同一套 DSpark 代码关掉 markov/置信度/L1 的配置化变体**，Eagle3 改编自 SpecForge（链式逐 token 提议）。
- 草稿主干极小（如 5 层）但以目标模型多个中间层的隐状态为输入特征，这是需要预计算巨大 target cache 的根本原因。
- README 的 12 个 Released Checkpoints 与 `config/` 下 12 个配置文件一一对应；`dspark_qwen3_4b_block7` 对应 `config/dspark/dspark_qwen3_4b.py`。

## 7. 下一步学习建议

下一讲（u1-l2「代码结构与三阶段工作流总览」）会打开 `deepspec/` 包，逐目录浏览 `data`、`modeling`、`trainer`、`eval`、`utils` 各自的子模块，并把 `scripts/` 下的脚本串成完整流程图。建议你先自己完成 `python -m pip install -r requirements.txt`，再带着本讲的两个问题去逛目录：

1. `deepspec/modeling/` 下有没有独立的 dflash 目录？（验证本讲 4.2 的结论）
2. `scripts/data/` 下的三个脚本，输出文件分别是什么？（验证本讲 4.3.2 的流程图）

之后按依赖顺序学习：u1-l3（入口文件 `train.py`/`eval.py`）→ u1-l4（配置系统），随后进入第 2 单元的数据流水线精读。
