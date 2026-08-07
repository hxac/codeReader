# 信号-投影-决策路由心智模型

## 1. 本讲目标

本讲是「核心概念模型」单元的第一讲，目标是帮你在脑子里建立一张**端到端的数据流地图**，让后续所有讲义都能挂在这张地图上。

学完后你应该能够：

- 用一句话说清楚 vLLM Semantic Router（以下简称 SR）把一次请求切成哪几个阶段。
- 理解 **Signals（信号）→ Projections（投影）→ Decisions（决策）→ Models + Plugins（模型与插件）** 这条主干数据流，以及每一层的职责边界。
- 理解投影层为什么存在：它负责把「相互竞争的原始信号」协调成「少数几个命名路由带」，供决策层消费。
- 拿到一份真实的 `recipe.dsl`，能从 SIGNAL 一路追到 ROUTE，再说清楚每个 ROUTE 用到了哪几类信号与投影。

本讲**只建立概念模型**，不深入 Go 源码实现。具体的信号求值、决策引擎、选择算法分别在 u8、u6 讲义展开。

## 2. 前置知识

在进入本讲前，你需要具备 u1-l1 建立的两个认知（这里只做一句话复习，不展开）：

1. **SR 是什么**：一个夹在客户端与模型后端之间的可编程路由层，以 Envoy External Processor（`ext_proc`）控制面的形态落地，目标是同时改善质量、成本、延迟、隐私、安全五个维度。
2. **Mixture-of-Models（MoM，混合模型）理念**：「最好的模型是一个组合」，因此需要一种机制把每条请求送到最合适的模型上——这套机制就是本讲要讲的信号-投影-决策。

此外，建议你事先了解三个通俗概念：

- **信号（Signal）**：从请求里抽取出来的、可用于判断的「事实碎片」，例如「这条请求是中文」「这条请求提到了法律」。
- **置信度（Confidence）**：一个 0~1 之间的数，表示某个信号有多强烈。例如 embedding 信号相似度 0.9 比 0.6 更强烈。
- **路由带（Routing Band）**：一个带名字的、互斥的分类结果，例如 `balance_simple`、`balance_complex`。决策层只认这些「带」，不直接认原始信号。

如果你能把这三个词和日常经验对应起来（信号=线索、置信度=线索有多硬、路由带=把线索归并后的结论），本讲会很顺。

## 3. 本讲源码地图

本讲只引用两个真实文件，它们一个是「概念说明书」，一个是「可运行实例」：

| 文件 | 作用 | 本讲怎么用 |
| --- | --- | --- |
| [website/docs/intro.md](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/website/docs/intro.md) | 项目对外介绍，用一张表把 Signals / Projections / Decisions 三层讲清楚 | 作为概念权威来源，建立心智模型 |
| [config/recipes/balance/recipe.dsl](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl) | 推荐的默认通用配方，用 DSL 文本写出了完整的信号-投影-决策 | 作为贯穿全讲的真实素材，逐块拆解 |

补充阅读（实践环节会用到）：

- [config/recipes/balance/README.md](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/README.md)：balance 配方的设计目标与路由顺序表，是理解「为什么这样配」的最佳注解。

> 名词提示：DSL 指 Domain-Specific Language（领域专用语言）。`recipe.dsl` 是 SR 专门为写路由策略设计的一种文本格式，后续 u7 会专门讲它的语法与编译。本讲你只需要把它当成「一份可读的路由策略文档」即可。

## 4. 核心概念与源码讲解

整体数据流可以浓缩成一句话（来自 intro.md 的官方表述）：

> Signals are extracted from requests, projections coordinate matched evidence, decision rules evaluate the resulting facts, and the chosen route drives plugins plus model dispatch.
> （信号从请求中抽取，投影协调匹配到的证据，决策规则对结果事实求值，被选中的路由驱动插件与模型分发。）

把它画成流水线就是：

```
请求(request)
   │
   ▼
[1] Signals 信号层      ── 抽取 16 族原始信号（带置信度）
   │
   ▼
[2] Projections 投影层   ── 把竞争的信号协调成少数命名路由带
   │
   ▼
[3] Decisions 决策层     ── 用 AND/OR/NOT 规则在「带 + 信号」上求值，选出唯一路由
   │
   ▼
[4] Models + Plugins     ── 选中模型候选 + 触发该路由启用的插件链
   │
   ▼
转发到后端模型
```

下面四个小节分别对应这四层。每层我们都先用直觉讲它解决什么问题，再用 `recipe.dsl` 的真实代码印证。

### 4.1 信号层（Signals）

#### 4.1.1 概念说明

信号层要回答的问题是：**「这条请求，有哪些可用于判断的事实？」**

设想你是个客服调度员，每来一条用户消息，你会下意识地抓几个线索：用户说的是什么语言？在聊什么领域（法律、医疗、写代码）？问题简短还是复杂？有没有提到「请给出处」「这答案错了」？这些「线索」就是信号。

SR 维护了 **16 个信号族**（intro.md 明确列出），每一族负责抽取一类事实：

| 信号族类别 | 代表信号 | 抽取角色 |
| --- | --- | --- |
| 内容类 | `keyword`、`domain`、`embedding`、`kb` | 这条请求「讲什么、像什么」 |
| 结构/复杂度类 | `structure`、`complexity`、`context` | 这条请求「多复杂、多长」 |
| 安全/合规类 | `pii`、`jailbreak`、`fact-check` | 这条请求「有没有风险」 |
| 反馈/跟进类 | `user-feedback`、`reask`、`preference` | 用户「对上一轮满不满意」 |
| 运行时类 | `authz`、`language`、`modality` | 「谁在问、用什么语言、什么模态」 |

每抽取到一个信号，都会附带一个**置信度**，表示这个信号有多强烈。这一点很关键：信号不是「有/无」的开关，而是「有多像」的连续值。

#### 4.1.2 核心流程

信号抽取的抽象流程：

1. 请求进入，取出文本内容（及上下文、用户身份等运行时信息）。
2. 对每一族信号，调用对应的分类器/匹配器去判定。
3. 每个被命中的信号产出一个 `(信号名, 置信度)` 对，未命中的不产出或产出 0。
4. 把所有产出汇总成一份「信号事实集合」，交给投影层。

不同信号族的判定方式不同，但产物结构一致：

```
对每个信号族 s:
    c = 判定器_s(请求)
    若 c > 0: 记录 (s, c)
汇总 => 信号事实集合
```

#### 4.1.3 源码精读

`recipe.dsl` 里 SIGNAL 块就是「信号声明」。我们看三类典型写法。

**keyword 信号**：用一组关键词做 OR 匹配。例如「简单请求」标记，命中任一关键词就视为有此信号：

[config/recipes/balance/recipe.dsl:86-89](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L86-L89) 声明了 `simple_request_markers`，它列出 "quick answer"、"tl;dr"、"简短回答" 等关键词，命中其一即触发。

**embedding 信号**：用一组候选句子做语义相似度匹配，超过阈值才触发，相似度作为置信度。例如「快速问答」：

[config/recipes/balance/recipe.dsl:141-145](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L141-L145) 声明了 `fast_qa_en`，`threshold: 0.72` 表示与候选句子的最大相似度需 ≥ 0.72 才算命中，`aggregation_method: "max"` 表示取所有候选里最高的相似度作为该信号的置信度。

**complexity 信号**：用「难例 vs 易例」两组候选句子做对比，判定请求难度。例如：

[config/recipes/balance/recipe.dsl:317-322](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L317-L322) 声明了 `general_reasoning`，`hard` 列「比较多种方案并论证取舍」类难句，`easy` 列「简单解释一下」类易句，`threshold: 0.14` 是难易分界。后续你会看到 complexity 信号还能产出 `:medium`、`:hard` 这样的分级结果。

> 小结：信号层只管「抽取事实」，不做任何路由决定。它产出的是一堆带名字、带置信度的证据。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**（不需要运行，目的是让你熟悉信号声明）。

1. **实践目标**：识别三类典型信号的声明结构。
2. **操作步骤**：
   - 打开 [config/recipes/balance/recipe.dsl](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl)。
   - 找到 `SIGNAL keyword simple_request_markers`、`SIGNAL embedding fast_qa_en`、`SIGNAL complexity general_reasoning` 三处声明。
   - 再找一个 `SIGNAL context short_context`（约第 263 行），看它如何用 `min_tokens`/`max_tokens` 判定上下文长度。
3. **需要观察的现象**：不同信号族用**完全不同的字段**来描述判定条件（keyword 用 `keywords`、embedding 用 `candidates`+`threshold`、complexity 用 `hard`/`easy`、context 用 token 区间），但都产出「一个带置信度的事实」。
4. **预期结果**：你能用自己的话说出这四种信号各自的「判定器」是什么。

#### 4.1.5 小练习与答案

**练习 1**：`embedding fast_qa_en` 的 `aggregation_method: "max"` 是什么意思？如果改成对每条候选都单独判定，会带来什么问题？

**参考答案**：`max` 表示「取所有候选句子里最大的那个相似度」作为该信号的置信度。如果对每条候选都单独产出信号，就会出现十几个含义重复的 `fast_qa` 信号，决策层很难写规则；聚合成一个「最大相似度」值，相当于把多个候选归约成一个事实。

**练习 2**：为什么信号要带置信度，而不是简单的「命中/未命中」开关？

**参考答案**：因为「像不像快速问答」是程度问题。0.90 的相似度比 0.73 更应该被当成快速问答。带置信度后，下游投影和决策可以做加权、做阈值分带，路由才能平滑而非抖动。

### 4.2 投影层（Projections）

#### 4.2.1 概念说明

投影层要回答的问题是：**「信号这么多，相互之间还会打架，怎么把它们归并成少数几个决策能用的结论？」**

举个会打架的例子：一条请求同时很像「快速问答」（embedding 命中）又很像「需要查证」（用户说了 "verify this"）。这两个信号都成立，但它们指向不同的处理力度。如果决策层直接面对几十个原始信号，规则会爆炸。

投影层的作用就是**协调（coordinate）**：把竞争的信号按某种数学方法合并，产出一组**命名路由带（named routing bands）**——例如 `balance_simple` / `balance_medium` / `balance_complex` / `balance_reasoning`。决策层只需要在「带」上写规则，规则数量大幅下降。

intro.md 用一句话点明了投影的职责：

[website/docs/intro.md:37-38](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/website/docs/intro.md#L37-L38)：Projections 的 Role 是 "Coordinate competing matches and emit named routing bands"（协调竞争匹配并产出命名路由带）。

SR 有三类投影，分别用三种数学方法：

| 投影类型 | 语义 | 数学方法 | 典型产物 |
| --- | --- | --- | --- |
| `partition` | 互斥分区：多个候选里选一个胜者 | softmax + 胜者通吃 | `domain` 落到 `law`、`math`… 之一 |
| `score` | 加权评分：把多个信号线性组合成一个分数 | 加权和 | `difficulty_score` 一个 0~1 的数 |
| `mapping` | 阈值分带：把分数切成几段命名带 | 阈值区间 + sigmoid 距离 | `difficulty_band` 落到 `balance_simple/medium/complex/reasoning` 之一 |

注意一条常见链路：`score` 先把多信号算成一个分数，`mapping` 再把这个分数切成命名带。两者经常成对出现。

#### 4.2.2 核心流程

**partition（softmax 互斥分区）**：成员之间相互竞争，置信度经 softmax 归一化后，取最大者为胜出带，其余被抑制。温度 \(T\) 控制竞争的尖锐程度：

\[ p_i = \frac{\exp(c_i / T)}{\sum_j \exp(c_j / T)} \]

\(T\) 越小，胜者越「通吃」；\(T\) 越大，分布越平。胜出的成员成为当前带，若所有成员都很弱，则回落到 `default`。

**score（加权和评分）**：把若干信号的置信度 \(v_i\) 按权重 \(w_i\) 线性组合：

\[ S = \sum_i w_i \cdot v_i \]

权重可正可负：正权重表示「这个信号拉高分数」，负权重表示「拉低分数」。

**mapping（阈值分带 + sigmoid 距离校准）**：把分数 \(S\) 按阈值切成若干区间，落入哪个区间就是哪个带。边界附近用 sigmoid 距离给置信度——离边界越远，越确定属于这个带；越靠近边界，越不确定：

\[ \text{conf} = \sigma(\text{slope} \cdot (S - \text{boundary})) \quad,\quad \sigma(x)=\frac{1}{1+e^{-x}} \]

三者的协作流程：

```
原始信号(多个, 带置信度)
   │
   ├─→ partition: 在互斥成员里 softmax 选胜者   => 一个命名带(如 domain=law)
   ├─→ score:     加权求和                      => 一个分数(如 difficulty_score=0.6)
   │                   │
   │                   └─→ mapping: 按阈值切段   => 一个命名带(如 balance_complex)
   ▼
供决策层消费的「命名带 + 分数」事实集合
```

#### 4.2.3 源码精读

**partition 实例**：`balance_domain_partition` 把 14 个 domain 信号协调成「到底属于哪个领域」。

[config/recipes/balance/recipe.dsl:363-368](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L363-L368)：`semantics: "softmax_exclusive"`、`temperature: 0.1`（很尖锐，几乎胜者通吃）、`members` 列出 14 个领域、`default: "other"`（全弱时回落到「其他」）。

**score 实例**：`difficulty_score` 是 balance 配方里最重要的评分投影，它把 keyword/embedding/context/structure/complexity 四五十个输入加权求和成一个难度分数。

[config/recipes/balance/recipe.dsl:377-380](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L377-L380)：`method: "weighted_sum"`，注意权重的正负——`simple_request_markers` 权重 `-0.26`（简单请求拉低难度分），`agentic_workflows` 权重 `+0.20`（智能体工作流拉高难度分），`math_task:hard` 权重 `+0.22`（数学难题拉高）。这一行就把「难度」这个抽象概念量化了。

**mapping 实例**：`difficulty_band` 把上面的难度分数切成四段命名带。

[config/recipes/balance/recipe.dsl:402-407](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L402-L407)：`source: "difficulty_score"`（吃上面 score 的输出）、`method: "threshold_bands"`、`calibration: sigmoid_distance, slope: 10`、`outputs` 把 0~1 切成四段：`<0.18 → balance_simple`、`0.18~0.48 → balance_medium`、`0.48~0.82 → balance_complex`、`≥0.82 → balance_reasoning`。决策层之后只需写 `projection("balance_complex")`，而不必关心 47 个原始输入。

#### 4.2.4 代码实践

1. **实践目标**：看懂「score + mapping」成对工作的链路。
2. **操作步骤**：
   - 在 `recipe.dsl` 中定位 `PROJECTION score difficulty_score`（第 377 行）与 `PROJECTION mapping difficulty_band`（第 402 行）。
   - 在 difficulty_score 的 `inputs` 里，找出**权重最大的三个正权重输入**和**权重最大的一个负权重输入**。
   - 对照 difficulty_band 的 `outputs`，说明这三个输入分别会把分数推向哪个带。
3. **需要观察的现象**：score 的输出是一个连续分数，mapping 把它离散化成带。决策层引用的是「带名」而非「分数」。
4. **预期结果**：你能解释「`agentic_request_markers` 命中会提高难度分，进而可能把请求从 `balance_simple` 推到 `balance_complex`」这条因果链。

#### 4.2.5 小练习与答案

**练习 1**：partition 的 `temperature: 0.1` 比较小，这意味着领域判定更「果断」还是更「犹豫」？

**参考答案**：更果断。温度越小，softmax 分布越尖锐，置信度最高的成员会被强烈放大、其余被强烈抑制，接近「胜者通吃」。如果温度很大，分布趋平，领域判定会变得模糊。

**练习 2**：为什么不直接让决策层读 `difficulty_score` 的数值，而要再过一层 mapping 切成 `balance_*` 带？

**参考答案**：为了让决策规则可读、可维护。直接在 0~1 的连续分数上写规则，意味着每个 ROUTE 的 WHEN 都要写一堆数值比较，调阈值时牵一发动全身。切成 4 个命名带后，ROUTE 只需写 `projection("balance_complex")`，阈值集中在一处（mapping 的 outputs）管理，调整成本和出错概率都低很多。

### 4.3 决策层（Decisions / Routes）

#### 4.3.1 概念说明

决策层要回答的问题是：**「有了信号和命名带，到底选哪条路由？」**

决策层的基本单元是 **ROUTE（路由）**。每条 ROUTE 长这样：

- 一个**名字**（如 `fast_qa`、`premium_legal`）。
- 一个 **WHEN 布尔规则**：用 `AND / OR / NOT` 把信号和投影组合成判断条件。
- 一个 **PRIORITY（优先级）**：数字越大越优先。
- 一个 **TIER（层级）**：用于分组与兜底语义。
- 一个 **MODEL**：命中后转发到哪个模型。
- 若干 **PLUGIN**：命中后启用哪些插件。

决策的执行语义是**优先级顺序匹配**：按 PRIORITY 从高到低逐条求值 WHEN，**第一条满足的 ROUTE 胜出**，其余不再求值。为了保证「永远有路可走」，最后通常会留一条**没有 WHEN 的兜底路由**。

intro.md 对决策层的定位：

[website/docs/intro.md:39](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/website/docs/intro.md#L39)：Decisions 是 "AND/OR policy rules over signals and projections"，Role 是 "Select the active route and model candidates"。

#### 4.3.2 核心流程

决策求值的抽象流程：

```
对每条 ROUTE，按 PRIORITY 从高到低:
    ok, conf = 求值(WHEN 规则树, 信号事实, 投影事实)
    若 ok:
        选中此 ROUTE → 取其 MODEL 与 PLUGIN → 结束
若全部未命中:
    选中兜底 ROUTE（无 WHEN 的那条）
```

WHEN 规则是一棵布尔表达式树，节点语义（这里只讲概念，求值细节在 u6-l1）：

- `signal("name")` / `projection("name")`：该信号/投影是否命中（存在性判断）。
- `A AND B`：两者都命中才为真；聚合置信度取**平均**（要求证据一致）。
- `A OR B`：任一命中即为真；聚合置信度取**最佳**（取最强证据）。
- `NOT A`：A 未命中才为真。

#### 4.3.3 源码精读

**典型带 WHEN 的路由**：`fast_qa` 把「短问题、低难度、无安全风险」的请求留在最便宜的通道。

[config/recipes/balance/recipe.dsl:650-662](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L650-L662)：`PRIORITY 184`、`TIER 12`，WHEN 里同时用到了 embedding（`fast_qa_en`/`fast_qa_zh`）、language（`en`/`zh`）、keyword（`simple_request_markers`）、structure（`low_question_density`）、context（`short_context`）、projection（`balance_simple`/`balance_medium`/`verification_required`/`urgency_elevated`）等多类信号与投影，并用一连串 `NOT (...)` 排除法律、健康、代码、紧急、反馈纠正等不该走快速通道的情况。注意它大量引用了 4.2 里讲过的命名带。

**兜底路由**：`casual_chat` 是「绝对兜底」，**没有 WHEN**。

[config/recipes/balance/recipe.dsl:678-689](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L678-L689)：`PRIORITY 10`（全表最低）、`TIER 14`，直接写 `MODEL "qwen/qwen3.5-rocm"`，没有任何 WHEN 条件。它的设计意图在 description 里写得很清楚——"Absolute final fallback that guarantees a routing decision when no earlier balance lane matches"（当更早的 lane 都不匹配时，保证一定有一个路由决策）。这就是「优先级顺序匹配 + 无条件兜底」的落地。

**最高优先级路由**：`premium_legal` 用 AND 组合「领域/关键词/嵌入命中」与「查证/复杂度」两组证据。

[config/recipes/balance/recipe.dsl:496-508](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L496-L508)：`PRIORITY 260`（全表最高）、`TIER 1`，WHEN 是 `(domain("law") OR keyword(...) OR embedding(...)) AND (embedding(...) OR projection("verification_required") OR complexity(...))`——外层 AND 要求「确实是法律」且「确实需要认真处理」同时成立，避免普通法律闲聊也升级到昂贵模型。

#### 4.3.4 代码实践

1. **实践目标**：验证「优先级顺序匹配 + 兜底」机制。
2. **操作步骤**：
   - 在 `recipe.dsl` 里把所有 ROUTE 的 `PRIORITY` 数字抄下来，按从大到小排序。
   - 确认 `casual_chat`（PRIORITY 10）是唯一**没有 WHEN**的路由。
   - 任选一条带 WHEN 的路由（如 `medium_creative`），把它的 WHEN 拆成「正向条件」和 `NOT` 排除条件两组。
3. **需要观察的现象**：PRIORITY 严格决定求值顺序；越靠前的路由条件越「窄而贵」（如法律、数学证明），越靠后的越「宽而便宜」（如快速问答、闲聊）。
4. **预期结果**：你能解释「为什么 PRIORITY 必须从高到低、且必须有一条无 WHEN 的兜底」——前者保证贵而窄的路由先抢，后者保证永不落空。

#### 4.3.5 小练习与答案

**练习 1**：`casual_chat` 没有 WHEN，会不会「抢走」所有请求，让前面的路由永远不执行？

**参考答案**：不会。决策是按 PRIORITY **从高到低顺序求值**，`casual_chat` 的 PRIORITY 是 10（最低），只有前面所有带 WHEN 的路由都不匹配时，才会落到它。顺序匹配保证了兜底路由只在「无人匹配」时生效。

**练习 2**：`premium_legal` 的 WHEN 外层为什么用 `AND` 而不是 `OR` 连接两组证据？

**参考答案**：用 AND 是为了同时要求「确实是法律话题」且「确实需要高规格处理」。如果用 OR，任何沾边法律的闲聊（比如随口问「NDA 是什么」）都会被升级到最贵的 claude-opus 模型，既浪费成本也违背「按需升级」的设计目标。AND 在这里起到「双重确认」的作用。

### 4.4 插件与模型分发（Models + Plugins）

#### 4.4.1 概念说明

决策层选中一条 ROUTE 后，要做两件事：

1. **模型分发**：把请求转发到该 ROUTE 声明的 MODEL。
2. **插件链**：执行该 ROUTE 启用的一组插件，对请求/响应做处理（缓存、改写、审计、安全检查等）。

**MODEL 声明**携带模型的元数据：能力（capabilities）、质量分（quality_score）、标签（tags，常用 `tier:`/`cost:` 标记档位与成本）、上下文窗口等。这些元数据既给人看，也给后续的「选择算法」用（u6 会讲如何在一个 ROUTE 对应多个候选模型时挑赢家）。ROUTE 里还可以给模型传运行参数，如 `reasoning = true, effort = "high"`，表示启用推理模式并调高推理力度。

**插件链**是 SR 的扩展点。intro.md 列出了一批插件类型：

[website/docs/intro.md:49-56](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/website/docs/intro.md#L49-L56)：semantic-cache（语义缓存）、jailbreak（越狱检测）、pii（隐私信息检测）、system_prompt（系统提示注入）、header_mutation（HTTP 头改写）、hallucination（幻觉检测）等。

关键设计：插件**按决策启停**（configurable enable/disable per decision），并形成**处理链**——每个插件都能检视/修改请求与响应。这意味着「走哪条路由」不仅决定用哪个模型，还决定走哪些治理逻辑。

#### 4.4.2 核心流程

```
决策选中 ROUTE R
   │
   ├─→ 取 R.MODEL + reasoning/effort 参数 ──→ 组装转发请求
   ├─→ 取 R.PLUGIN 列表(本路由启用的插件)
   │       │
   │       ├─ 请求阶段: 依次让插件检视/改写请求(如 semantic-cache 命中则直接返回)
   │       └─ 响应阶段: 依次让插件处理响应(如 router_replay 记录、hallucination 检测)
   │
   ▼
转发到后端模型(若缓存未命中) → 回传响应 → 响应阶段插件
```

#### 4.4.3 源码精读

**MODEL 声明**：每个 MODEL 是一段带元数据的别名。以最便宜的本地默认模型为例：

[config/recipes/balance/recipe.dsl:477-484](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L477-L484)：`qwen/qwen3.5-rocm`，`capabilities` 含 `fast_qa`/`general_chat`/`creative_drafting`，`tags` 标了 `tier:simple`、`cost:free`、`traffic:default`，`quality_score: 0.58`。对比最贵的 [anthropic/claude-opus-4.6（第 441-448 行）](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L441-L448)，它的 `quality_score: 0.94`、`tier:premium`、`cost:highest`——质量分和成本档位都明显更高。这些元数据是「成本感知路由」的基础。

**插件声明与按路由启用**：balance 配方只声明了一个插件 `router_replay`：

[config/recipes/balance/recipe.dsl:490](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L490)：`PLUGIN router_replay router_replay {}` 是全局声明。

然后在**每条 ROUTE 内部**单独启用它并配置参数，例如 `fast_qa` 内：

[config/recipes/balance/recipe.dsl:655-661](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L655-L661)：`enabled: true`、`max_records: 100000`、`capture_request_body: true`、`capture_response_body: true`、`max_body_bytes: 4096`。这就是「按决策启停 + 链式处理」的体现：同一种插件，可以在不同路由里启用、关掉或配不同参数。balance 的 README 也明确写了「每条维护路由都开启 router_replay 做审计」。

**模型运行参数**：ROUTE 可以给模型带运行参数。例如 `premium_legal`：

[config/recipes/balance/recipe.dsl:500](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/recipe.dsl#L500)：`MODEL "anthropic/claude-opus-4.6" (reasoning = true, effort = "high")`，表示这条高规格路由不仅用最贵的模型，还开启推理模式并拉满推理力度。而 `fast_qa` 则是 `(reasoning = false)`，省钱省时。

#### 4.4.4 代码实践

1. **实践目标**：理解「模型分发 + 插件按路由启用」是如何在 ROUTE 里打包在一起的。
2. **操作步骤**：
   - 在 `recipe.dsl` 中打开 `premium_legal`（第 496 行）和 `fast_qa`（第 650 行）两条路由。
   - 对比它们的 `MODEL` 行：模型别名、`reasoning`、`effort` 各是什么。
   - 对比它们的 `PLUGIN router_replay { ... }` 块：参数是否一致？
   - 再翻 [config/recipes/balance/README.md](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/README.md) 的「Cost Profile」表，看这两个模型的定价差多少。
3. **需要观察的现象**：贵的路由（premium_legal）用贵模型 + 高推理力度；便宜的路由（fast_qa）用免费模型 + 关闭推理。两者却都启用了同样的 `router_replay` 审计插件。
4. **预期结果**：你能说清楚「路由决定模型档位与推理力度，同时决定走哪些插件」——这就是 intro.md 说的 "the chosen route drives plugins plus model dispatch"。

#### 4.4.5 小练习与答案

**练习 1**：balance 配方里，`router_replay` 在 `premium_legal` 和 `fast_qa` 里的 `max_body_bytes` 都是 4096。如果你希望法律路由记录更完整的请求体（比如 8192），应该改哪里？

**参考答案**：改 `premium_legal` 路由内部的 `PLUGIN router_replay { max_body_bytes: 8192 }` 块。因为插件参数是「按路由」配置的，改一条路由的插件块不会影响别的路由。这正是「按决策启停/配置」设计带来的灵活性。

**练习 2**：`reasoning = true, effort = "high"` 和 `reasoning = false` 的区别会体现在哪里？

**参考答案**：体现在模型生成时的「思考力度」与对应的延迟/成本上。开启高推理力度通常意味着模型会做更多内部推理步骤，质量更高但更慢更贵；关闭则直接快速作答。balance 把贵而窄的路由（法律、数学证明）设为 `high`，把便宜的快速通道设为 `false`，正是「把预算花在刀刃上」的体现。

## 5. 综合实践

把四层串起来，完成下面这个贯穿全讲的任务（这是本讲的核心实践）。

**任务**：用 `config/recipes/balance/recipe.dsl` 为素材，画一张从 SIGNAL 到 ROUTE 再到 MODEL 的依赖关系图，标注每个 ROUTE 用到了哪几类信号与投影。

**操作步骤**：

1. **挑选路由**：从 `recipe.dsl` 的 ROUTES 里选 4 条有代表性的，建议选：
   - `premium_legal`（PRIORITY 260，最贵最窄）
   - `complex_specialist`（PRIORITY 242，复杂系统设计）
   - `fast_qa`（PRIORITY 184，便宜快速通道）
   - `casual_chat`（PRIORITY 10，无 WHEN 兜底）
2. **逆向依赖**：对每条选中的路由，读它的 WHEN，把引用到的**信号**和**投影**分别列出来，并按 4.1 的信号族类别给信号归类（keyword / embedding / domain / complexity / context / structure / language / feedback / fact_check / reask / projection）。
3. **画图**：用你顺手的工具（纸笔、Mermaid、excalidraw 都行）画一张三层图：
   - 左列：信号（按族着色）。
   - 中列：投影（partition / score / mapping）。
   - 右列：你选的 4 条 ROUTE，每条连到它用到的投影与信号。
   - 最右：每条 ROUTE 连到它的 MODEL。
4. **标注**：在每条 ROUTE 旁边标出它的 PRIORITY 与 TIER，并指出哪几条用了 `NOT` 排除条件、哪一条是兜底。

**参考起点（以 `premium_legal` 为例）**：

- 信号：`domain("law")`（domain 族）、`keyword("legal_risk_markers")`（keyword 族）、`embedding("premium_legal_analysis")`（embedding 族）、`complexity("legal_risk:medium/hard")`（complexity 族）。
- 投影：`projection("verification_required")`（来自 mapping `verification_band`，它又来自 score `verification_pressure`）。
- MODEL：`anthropic/claude-opus-4.6`，`reasoning=true, effort=high`。
- PRIORITY 260 / TIER 1，无 NOT 排除（它是最高优先级，靠 AND 双重确认而非排除来收窄）。

**需要观察的现象**：

- 几乎所有带 WHEN 的路由都同时引用了**多类信号**和**至少一个投影**——投影（尤其是 `balance_*` 与 `verification_required`）是路由之间共享的「公共词汇」。
- 越靠后的便宜路由，`NOT (...)` 排除条件越多，因为它们要把自己限制在「没有被前面贵路由抢走」的剩余流量里。
- `casual_chat` 是孤立的：既不引用信号也不引用投影，只连到 MODEL。

**预期结果**：你得到一张能一眼看出「信号如何被投影归并、投影如何被路由消费、路由如何落到模型」的依赖图。这张图就是本讲想让你建立的心智模型的可视化版本。如果你能用它向别人解释清楚「一条法律证明请求为什么会落到 claude-opus 而不是 qwen」，本讲就过关了。

> 提示：如果你想验证自己画得对不对，可以参考 [config/recipes/balance/README.md](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/recipes/balance/README.md) 的「Signal Strategy」一节，它用文字描述了每条主要路由的策略依据。

## 6. 本讲小结

- SR 的请求处理是一条四层流水线：**Signals → Projections → Decisions → Models + Plugins**，每一层职责清晰、上层只认下层的产物。
- **信号层**负责从请求中抽取 16 族事实，每条信号带 0~1 的置信度；它只管「抽取」，不做任何路由决定。
- **投影层**负责「协调竞争」：用 partition（softmax 互斥）、score（加权和）、mapping（阈值分带 + sigmoid 距离）三种数学方法，把几十个原始信号归并成少数几个命名路由带，让决策规则可读、可维护。
- **决策层**的基本单元是 ROUTE：用 AND/OR/NOT 在「信号 + 投影」上写 WHEN 规则，按 PRIORITY 从高到低顺序匹配，第一条命中者胜出；末尾保留一条无 WHEN 的兜底路由保证永不落空。
- **模型与插件**由选中的 ROUTE 驱动：ROUTE 决定用哪个 MODEL（及其 reasoning/effort 参数），同时决定启用哪些插件（如 `router_replay` 审计），插件可按路由独立启停与配置。
- balance 配方是这条心智模型的最佳实例：贵而窄的路由（法律、数学证明）优先级最高，便宜而宽的路由（快速问答、闲聊）优先级最低，靠投影与 NOT 排除条件实现「按需升级、按需省钱」。

## 7. 下一步学习建议

本讲建立的是**概念地图**，接下来的讲义会在这张地图上逐层展开细节：

- **u2-l2 Signals（16 个信号族）**：深入每个信号族的含义与写法，把本讲「信号层」展开。
- **u2-l3 Projections（分区/评分/映射）**：把本讲「投影层」的三种数学方法讲透，并引入投影追踪（projection trace）。
- **u2-l4 Decisions/Routes/Models**：把本讲「决策层」展开，详细讲 WHEN 规则树、PRIORITY/TIER 与 MODEL 元数据。
- 之后进入 **u3（配置体系）**，看 `recipe.dsl` 是如何与 `config.yaml` 对应、如何被加载校验的；**u5（请求处理主链路）**则进入 Go 源码，看这条四层流水线在代码里到底怎么跑。

建议在进入下一篇前，先把本讲的「综合实践」依赖图画出来——它会是你后续阅读所有源码的参照系。
