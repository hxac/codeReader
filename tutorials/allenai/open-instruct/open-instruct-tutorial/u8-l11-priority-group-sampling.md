# 历史优先级提示采样与 epoch 预算

## 1. 本讲目标

本讲是 MILES 单元的高级主题，聚焦 2026 年 11 月合入的「历史优先级提示采样」特性（PR #2251，即当前 HEAD 提交 `bae4209fd`）。学完本讲你应该能够：

1. 说清 `PriorityPromptProvider` 的遍历语义：每个 sampler epoch 内严格按组 1→2→3→4 依序耗尽，组与组之间没有采样权重，组内每行恰好出现一次。
2. 解释确定性的三层设计：seed 派生的组内洗牌、checkpoint 只存 `(identity, epoch_id, offset)` 的小状态、resume 时用身份哈希拒绝任何配置漂移。
3. 掌握组内混源加权（`prompt_priority_source_field` / `prompt_priority_source_weights`）：权重只影响抽取顺序与早期曝光，不改变「每行一次」的总量；源耗尽即跳过并对剩余权重重归一化。
4. 理解 `provider_source.py` 如何把可替换的提供者桥接进 MILES 既有的缓冲数据源，以及配置层（`config.py` constraints）在编译期拦截了哪些非法组合。
5. 走通 `training.num_epoch` 预算的完整链路：TOML → `miles.num_epoch` → 原生运行时在数据加载后换算出 `num_rollout` → `save_final_checkpoint` 在最后一个 rollout 落盘 → workflow 用 driver 返回值判定 run 完成。

## 2. 前置知识

本讲默认你已读过前三讲的对应内容（u8-l2 配置系统、u8-l3 工作流与执行、u8-l5 Rollout 提供者）。这里把会用到的概念用通俗语言再过一遍：

- **提示提供者（PromptProvider）**：RLVR 训练的每一步都要先「出题」——决定下一批给策略模型采样哪些 prompt。u8-l5 讲过，提供者是一个实现了 `next_samples` / `feedback` / `state_dict` / `load_state_dict` 并声明 `requires_feedback` 的对象，默认实现是全局洗牌的 `ShuffledPromptProvider`。
- **历史优先级组（historical reward priority）**：Allen AI 的 Tagliere v6 数据管线会离线分析每个 prompt 在某个参考模型上的历史奖励记录，给每行打上一个 1–4 的组标签（具体含义见 4.1.1 的表格）。它写进样本元数据，训练期只读不改。
- **sampler epoch**：把整个提示数据集从头到尾完整遍历一遍叫一个 sampler epoch。优先级提供者每个 epoch 都会重新从组 1 开始。
- **rollout 预算 vs epoch 预算**：MILES 的 run 时长以前只能用 `num_rollouts`（固定采集多少个 rollout，缺省 100）描述；本特性让结构化 run 也能用 `num_epoch`（「把数据过几遍」）描述，由运行时在知道数据集大小后自行换算。
- **零方差过滤（zero-variance filter）**：GRPO 训练时，如果一个 prompt 的所有响应奖励完全相同（组内方差为 0），这个组对策略梯度没有贡献，通常会被在线过滤掉。本讲最后会解释它与历史优先级为什么互不干扰。

一个直觉性的比喻：把数据集想象成四个书架（组 1–4），优先级提供者是个图书管理员，他的规矩是「先把第一个书架的书全部借出一遍，再碰第二个书架」；每个书架内部的书按一个由 seed 决定的固定顺序排列，每轮（epoch）重新洗一次牌，但「先书架 1 后书架 2」的规矩永不变。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `open_instruct/miles/rollout/priority_provider.py` | 本讲主角：`PriorityPromptProvider`，约 180 行，含组分类、确定性洗牌、混源加权合并、checkpoint 身份 |
| `open_instruct/miles/rollout/provider_source.py` | `ProviderRolloutDataSource`：把各种提供者（shuffle/mixture/priority/remind/replay）桥接进 MILES 的缓冲数据源 API |
| `open_instruct/miles/configuration/config.py` | `CoreConfig` 字段定义与 `RunConfig.validate()` 的 constraints（priority 组合约束、`num_epoch` 正整数、`save_final_checkpoint`） |
| `open_instruct/miles/configuration/run_spec.py` | 结构化 run 文件编译：`[training]` 节落到 `miles.*`、`num_epoch`/`num_rollout` 互斥、epoch 预算的保存默认值 |
| `open_instruct/miles/configuration/options.json` | MILES 运行时命令行旗标快照，`num_epoch` 在此注册为 `--num-epoch` |
| `open_instruct/miles/execution/driver.py` | `driver.train`：rollout 循环、checkpoint 触发（含 `save_final_checkpoint`）、返回值携带解析后的 `num_rollout` |
| `open_instruct/miles/execution/workflow.py` | `workflow.execute`：用 driver 结果判定 run 是 `complete` 还是 `stopped` |
| `open_instruct/test_miles_priority_provider.py` | 优先级提供者的单测（CPU 可跑，共 15+ 个用例） |
| `open_instruct/test_miles_epoch_budget.py` | epoch 预算编译行为的单测（CPU 可跑） |
| `tests/miles/test_prompt_provider.py` | 提供者与数据源集成的运行时测试（需钉住的 miles/sglang 运行时才会被收集） |
| `docs/miles/data-and-evaluation.md` | 官方文档「Historical priority groups」一节（L230 起），组定义、权重语义与预算说明 |

## 4. 核心概念与源码讲解

### 4.1 PriorityPromptProvider：组序遍历与确定性

#### 4.1.1 概念说明

`PriorityPromptProvider` 回答一个问题：**当数据集每行都带有一个「历史奖励优先级」标签时，短预算的训练应该先看哪些 prompt？**

它的设计动机（见文档 L232-237）：让一个短训练 run 先集中在「历史上有混合奖励」的 prompt 上——这些 prompt 对当前策略既不全对也不全错，最有学习价值；同时一份导出数据可以复用于不同时长的 run，因为优先级不强制单遍停止，预算仍由普通的 epoch/rollout 机制控制。

组标签的含义直接来自文档的表格（[docs/miles/data-and-evaluation.md:245-250](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/docs/miles/data-and-evaluation.md#L245-L250)）：

| 组 | 历史证据 |
| --- | --- |
| 1 | 至少有一个完整打分的响应组出现了不同奖励（「有戏」的题） |
| 2 | 未知：没有匹配历史，或没有完整打分的响应组 |
| 3 | 所有完整打分的组奖励全为 0，且至少一半被评分的响应撞到了 token 上限 |
| 4 | 其他常数奖励，包括没有频繁截断的全 0 组 |

三个关键语义（都在类 docstring 与文档中言明）：

1. **组序固定、组间无权重**：所有新抽取都来自当前活跃组，直到它被耗尽，然后才进入下一组；组没有采样概率。
2. **组内每行每 epoch 恰好一次**：不存在组内重复采样。
3. **历史标签静态不变**：`requires_feedback = False`，`feedback()` 是空操作——训练中的奖励不会回头修改组序。这使它可以和不需要反馈通路的同步/异步管线都兼容。

#### 4.1.2 核心流程

初始化时做四件事：校验参数 → 把每行按 `(组, 源)` 分桶 → 计算身份摘要 → 物化第 0 个 epoch 的完整顺序。之后每次 `next_samples(n)` 只是对一条预生成的索引数组做切片：

```text
构造:
  rows[g-1][source] = 该组该源的所有行号（按数据集原序）
  counts            = 各组行数
  identity          = {kind, field, source_field, source_weights, seed,
                       shuffle, counts, assignments_sha256}
  order(0)          = 组1顺序 ++ 组2顺序 ++ 组3顺序 ++ 组4顺序

next_samples(n):
  while 还有需求:
    若 offset 走到 order 末尾 → 生成 order(epoch+1)，epoch+=1，offset=0
    take = min(剩余需求, len(order)-offset)
    返回 order[offset : offset+take] 对应的样本，offset += take
```

写成数学形式，第 \( e \) 个 epoch 的遍历顺序是四段拼接：

\[ \mathrm{order}^{(e)} = S^{(e)}_1 \,\Vert\, S^{(e)}_2 \,\Vert\, S^{(e)}_3 \,\Vert\, S^{(e)}_4 \]

其中 \( S^{(e)}_g \) 是第 \( g \) 组行号的一个排列。当 `shuffle=true` 时该排列由独立的 seeded RNG 生成，epoch 0 直接用 `seed`，之后的 epoch 用派生种子：

\[ \text{rng}^{(e)}_g = \texttt{Random}\left(\texttt{"\{seed\}:epoch:\{e\}:priority:\{g\}"}\right) \]

（带混源加权时还会拼上源名，见 4.2。）因为顺序在每个 epoch 开始时**一次性物化**成数组，所以：

- 请求怎么切分（批大小、跨组边界、跨 epoch 边界）都不影响选到了哪些行——确定性对批大小与 resume 都是稳定的；
- checkpoint 只需要存 `(identity, epoch_id, offset)` 三个东西，resume 时用相同的种子重算出同一条 `order` 再对齐游标即可，状态体积极小（单测断言 `len(str(state)) < 1000`）。

#### 4.1.3 源码精读

类声明与「无反馈」声明：

[open_instruct/miles/rollout/priority_provider.py:24-30](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rollout/priority_provider.py#L24-L30)
docstring 明确「每个 epoch 内按 1、2、3、4 顺序访问各组；组内的 seeded 排列固定该 epoch 的遍历；没有任何奖励反馈或组概率参与选择」。`requires_feedback = False` 让外层管线不必为它接反馈通路。

构造函数的入参校验（fail-fast，全部在训练启动前抛 `ValueError`）：

[open_instruct/miles/rollout/priority_provider.py:32-54](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rollout/priority_provider.py#L32-L54)
逐条检查：`field` 必须是非空字符串；数据集必须非空；`source_field` 与 `source_weights` 必须成对出现；每个权重必须是有限正数且权重总和有限。任何一条不满足都在构造期失败，而不是跑到一半才出问题。

行分类与身份摘要：

[open_instruct/miles/rollout/priority_provider.py:55-72](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rollout/priority_provider.py#L55-L72)
遍历 `dataset.origin_samples`，读 `sample.metadata[field]` 作为组号——注意 L60 用 `type(group) is not int` 严格判定，`True`（bool 是 int 的子类）、`1.0`、`"1"`、缺失全部拒绝，且必须是 1..4。若启用混源加权，L62-66 还要求每行的源标签在权重表里有配置。L57-72 把每行的（组号、可选源名）逐条喂进 SHA-256 摘要，L68 的 `digest.update(bytes([group]))` 加一个组号字节，带源时再加长度前缀的源名字节——这保证了 digest 同时捕捉「组员归属」和「源归属」，且与行顺序相关。

identity 与日志：

[open_instruct/miles/rollout/priority_provider.py:79-103](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rollout/priority_provider.py#L79-L103)
`identity` 字典记录了影响遍历语义的全部自由度：字段名、源字段、源权重、seed、shuffle、各组计数和 `assignments_sha256`。`kind` 固定为 `"priority/v3"`——版本号内嵌在身份里，将来语义变化可以用它拒绝旧 checkpoint。L97-103 的日志把字段、各组计数、seed、shuffle 打出来，方便在 run 日志里核对实际生效的配置。

顺序生成（每个 epoch 重新洗牌）：

[open_instruct/miles/rollout/priority_provider.py:109-128](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rollout/priority_provider.py#L109-L128)
`_make_order` 里 L116 做 epoch 种子派生（epoch 0 用原 seed，之后拼 `":epoch:{e}"`）；L118-123 逐组处理：`shuffle=True` 时用「epoch 种子 + 组号（+ 源名）」实例化 `random.Random` 洗牌该组的行号数组；L124-127 无权重时直接把该组行号接在 `order` 后（源名为空的桶），有权重时调用 `_merge_sources`（见 4.2）。

切片式取数与 epoch 回绕：

[open_instruct/miles/rollout/priority_provider.py:148-161](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rollout/priority_provider.py#L148-L161)
`next_samples` 先校验 `n`（拒绝负数与非 int），然后循环：当前 `order` 用尽就生成下一个 epoch 的 order 并把游标归零；`take = min(n, 剩余)` 保证单次请求可以跨越组边界、甚至跨越 epoch 边界而不断裂。

反馈为空操作、状态极小：

[open_instruct/miles/rollout/priority_provider.py:163-167](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rollout/priority_provider.py#L163-L167)
`feedback` 的 docstring 一句话：「历史优先级在整个 run 中保持不变」。`state_dict` 只返回 `identity + epoch_id + offset`，不含 `order` 本身。

resume 的身份校验与顺序重建：

[open_instruct/miles/rollout/priority_provider.py:169-180](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rollout/priority_provider.py#L169-L180)
`load_state_dict` 逐一比对 identity 的每个键（L170-172），任何一个不匹配（换了字段、换了 seed、改了权重、`kind` 版本变了）都抛「configuration mismatch」；然后校验 `epoch_id` 与 `offset` 的类型和范围；最后 L179 用 checkpoint 的 epoch **重算**那条 order 再设置游标——因为顺序是纯种子函数，重算结果与中断前完全一致。

#### 4.1.4 代码实践

**实践目标**：在 CPU 上直接驱动 `PriorityPromptProvider`，亲眼验证「组序耗尽、跨请求不重复、epoch 回绕、resume 一致」四个行为。

**操作步骤**（示例代码，仿照单测的 `SimpleNamespace` 假数据集，不需要 GPU 与 miles 运行时）：

```python
# demo_priority.py（示例代码）
from types import SimpleNamespace
from open_instruct.miles.rollout.priority_provider import PriorityPromptProvider

FIELD = "reward_priority_olmo35_sft_hero"

class Dataset:
    def __init__(self, sizes=(11, 120, 7, 20)):  # 每组的行数
        self.origin_samples = [
            SimpleNamespace(prompt=f"{g}:{i}", metadata={FIELD: g})
            for g, size in enumerate(sizes, 1) for i in range(size)
        ]
    def __len__(self):
        return len(self.origin_samples)

p = PriorityPromptProvider(Dataset(), field=FIELD, seed=17, shuffle=True)

# 1) 用三个不整齐的请求跨边界取完全部 158 行
samples = p.next_samples(9) + p.next_samples(124) + p.next_samples(25)
groups = [s.metadata[FIELD] for s in samples]
print("组序列单调不减:", groups == sorted(groups))
print("组分布:", [(g, groups.count(g)) for g in (1, 2, 3, 4)])
print("无重复:", len({s.prompt for s in samples}) == 158)

# 2) 再取 1 个 → 进入 epoch 1，又从组 1 开始
nxt = p.next_samples(1)[0]
print("epoch:", p.epoch_id, "下一行组号:", nxt.metadata[FIELD])

# 3) resume：保存状态后新建 provider 恢复，继续取数应与连续运行一致
import copy
q = PriorityPromptProvider(Dataset(), field=FIELD, seed=17, shuffle=True)
q.next_samples(50)
state = copy.deepcopy(q.state_dict())
expected = [s.prompt for s in q.next_samples(100)]
r = PriorityPromptProvider(Dataset(), field=FIELD, seed=17, shuffle=True)
r.load_state_dict(state)
print("resume 一致:", [s.prompt for s in r.next_samples(100)] == expected)
print("状态大小:", len(str(state)))
```

运行：`uv run python demo_priority.py`（可在仓库根目录执行）。

**需要观察的现象**：组序列严格按 1×11、2×120、3×7、4×20 分段；158 行互不重复；下一次取数组号回到 1 且 `epoch_id` 变为 1；resume 后续序列与连续运行完全一致；状态字符串很短。

**预期结果**：四项打印均为 `True`，状态大小在几百字符量级。这对应单测 [test_miles_priority_provider.py:40-47](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_miles_priority_provider.py#L40-L47)（组序与跨批边界）与 [test_miles_priority_provider.py:50-61](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_miles_priority_provider.py#L50-L61)（resume 回滚）。也可以直接跑 `uv run pytest open_instruct/test_miles_priority_provider.py -q` 复核全部断言。若本机未装 uv 环境则「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `state_dict` 不保存 `order` 数组，resume 却能精确续传？
**答案**：`order` 是 `(seed, epoch, 字段, 组员归属)` 的纯函数（4.1.2 的种子派生公式），`load_state_dict` 在 [priority_provider.py:179](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rollout/priority_provider.py#L179) 用 checkpoint 的 epoch 重算即可复原。不存数组让 checkpoint 体积与数据集规模无关（单测断言 `len(str(state)) < 1000`），也天然防止「保存的顺序」与「重算的顺序」两份真相漂移。

**练习 2**：把某行的组号从 1 改成 2、同时把另一行从 2 改成 1（各组计数不变），resume 会发生什么？
**答案**：构造新 provider 后 `load_state_dict` 抛 `ValueError`，错误信息含 `assignments_sha256`。因为身份摘要按数据集行序逐行吸收（组号, 源名），成员归属变了摘要必然变（[priority_provider.py:57-72](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rollout/priority_provider.py#L57-L72)）。这正是单测 [test_miles_priority_provider.py:107-112](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_miles_priority_provider.py#L107-L112) 验证的行为（链接中段有误时请以文件 L107-L112 为准）。

**练习 3**：组 2 是「未知」，组 3/4 多半是「难到全 0」——为什么遍历顺序仍是 1→2→3→4 而不是给组加权抽样？
**答案**：这是设计取舍：优先级提供者的目标是「短预算下先花在最可能有学习信号的行上，同时保证任何预算下数据都能被完整、可复现地遍历」。固定组序让遍历可预测、checkpoint 可校验、与 epoch 预算可对齐；引入组间概率会让「哪些行被看到」依赖随机过程而非确定清单，破坏 resume 审计与本文的切片式实现。文档 [data-and-evaluation.md:252-253](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/docs/miles/data-and-evaluation.md#L252-L253) 明确写了 "Groups have no sampling weights"。

### 4.2 组内混源加权：_merge_sources

#### 4.2.1 概念说明

一个 Tagliere 导出工件里可能混着多个数据来源（用 Workbench 的规范源 ID `workbench.source.candidate_id` 标注，或 `source_name` 这类平铺字段）。混源加权让你在**活跃组内部**控制「下一次抽哪个源」的比例，例如让 a:b = 3:1。

必须准确理解权重语义（文档 L275-282 讲得最清楚）：

1. 权重只决定**抽取顺序与早期预算曝光**——一次完整遍历后每个源的所有行都会被取到，总量不变，「每行每 epoch 恰好一次」不被破坏；
2. 权重与该源**剩余行数无关**：即使 a 只剩 1 行而 b 还有 1000 行，a:b 的抽中比例仍是 3:1；
3. 某个源耗尽（或本来就不存在）就被跳过，剩余权重**重归一化**；整个组必须吃完才进下一组；
4. `data.shuffle = false` 时保留每个源内的原始行序，但 seed 仍固定源与源之间的抽取；
5. 数据里出现的每个源标签都必须在权重表里有正的有限权重，缺了直接启动失败；反过来，配置里多写的、数据中不存在的源标签无害。

#### 4.2.2 核心流程

实现的关键决策写在 `_merge_sources` 的注释里（L131-134）：**在生成 order 时一次性完成加权合并**，训练期的每次取数因此仍是纯切片，checkpoint 也只需要一个 offset。每个源保留自己的（seeded）排列，只有「某源耗尽」这一事件会改变活跃概率。

算法是一个带重归一化的加权不放回抽样：

```text
names = 按字典序排序的活跃源
cumulative = names 权重的前缀和
循环直到所有源耗尽:
    u = rng.random() * cumulative[-1]        # 均匀落在 [0, 总权重)
    name = names[bisect_right(cumulative, u)] # 权重比例选中一个源
    取该源排列中下一个行号追加到 order
    若该源用尽: 从 names 移除, 重算 cumulative  # 等价于对剩余权重重归一化
```

任意时刻，下一个样本来自源 \( s \) 的概率是：

\[ P(\text{next} = s) = \frac{w_s}{\sum_{s' \in A} w_{s'}}, \qquad A = \text{尚未耗尽的源集合} \]

所以 3:1 的权重在两源都活跃时给出 75% / 25% 的早期曝光；一旦 a 耗尽，\( A=\{b\} \)，b 的概率变为 1。

#### 4.2.3 源码精读

加权合并的完整实现：

[open_instruct/miles/rollout/priority_provider.py:130-146](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rollout/priority_provider.py#L130-L146)
L134 `names = sorted(members)` 先按字典序固定源顺序——这步是确定性的前提，否则 `cumulative` 的段序会依赖 dict 迭代序。L137 用 `itertools.accumulate` 建前缀和；L139 用 `bisect_right` 在「均匀随机数 × 总权重」上按权重段选源（`len(names) > 1` 的短路让最后一个源不必抽签）；L144-146 源耗尽时 `names.pop` 并重算 `cumulative`，正是上式重归一化的实现。

带源的洗牌种子：

[priority_provider.py:118-127](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rollout/priority_provider.py#L118-L127)
有权重时每个源独立洗牌，种子拼上源名（L121-122 `f"{seed}:priority:{group}:{source}"`）；源与源之间的抽取由 L127 传入的 `f"{seed}:priority-sources:{group}"` 固定。注意每源的 `indices` 数组在多次 epoch 间是原地洗牌的，但每个 epoch 的种子不同，排列仍然逐 epoch 变化。

语义验证最有代表性的两个单测：

- [test_miles_priority_provider.py:168-177](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_miles_priority_provider.py#L168-L177)：8000 行（4 组 × 每组 a/b 各 1000），前 400 个样本全部来自组 1，且 a 的占比在 \( 0.75 \pm 0.06 \) 内；取完全部后组序严格是 1×2000 + 2×2000 + 3×2000 + 4×2000，且 8000 行互不重复——「权重只影响顺序、不影响总量」的直接证据。
- [test_miles_priority_provider.py:180-190](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_miles_priority_provider.py#L180-L190)：源 a 在组 1 只有 1 行、权重却给到 1000000，且组 2 的 a 行被预先删光、权重表里还有一个数据中不存在的 `absent` 源——结果第 1 个样本是 a，第 2–11 个全是 b（a 耗尽后立刻重归一化），组 2 直接全部来自 b（a 在该组不存在被自然跳过），`absent` 无害。

嵌套元数据路径的解析：

[priority_provider.py:15-21](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rollout/priority_provider.py#L15-L21)
`_metadata_value` 按英文句点逐段下钻嵌套 dict，所以 `source_field` 可以写 `"workbench.source.candidate_id"` 这样的规范路径；中途遇到非 dict 即返回 `None`（随后被「无配置权重」校验拒绝）。单测 [test_miles_priority_provider.py:236-251](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_miles_priority_provider.py#L236-L251) 专门验证了 Workbench 规范路径在单工件内选源的行为。

#### 4.2.4 代码实践

**实践目标**：验证「权重 → 早期曝光比例」与「源耗尽 → 重归一化」。

**操作步骤**：直接运行相关单测并读断言：

```bash
uv run pytest open_instruct/test_miles_priority_provider.py \
  -k "source_weights or source_depletion or workbench" -v
```

然后修改 4.1.4 的示例脚本：给每行加上 `metadata={"source_name": "a"|"b", ...}`，构造 `PriorityPromptProvider(..., source_field="source_name", source_weights={"a": 3, "b": 1})`，取前 400 个统计 a 的占比。

**需要观察的现象**：`test_source_weights_apply_only_within_the_active_group_and_never_repeat` 通过；自己统计的占比落在 0.69–0.81 区间（样本量 400 时的随机波动范围）。

**预期结果**：与断言 `abs(count_a/400 - 0.75) < 0.06` 一致。若想看极端情形，把权重改成 `{"a": 1000000, "b": 1}`，前段几乎全是 a，直到 a 耗尽。「待本地验证」（具体数值依赖 seed，但统计性质稳定）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `names` 必须 `sorted`？
**答案**：`cumulative` 前缀和的段序必须与 `names` 一致，`bisect_right` 才能把均匀随机数映射到正确源。若依赖 dict 迭代序（Python 中与插入历史有关），不同进程/不同构建下的 `cumulative` 段序可能不同，同 seed 也会产出不同 order，破坏确定性 resume。

**练习 2**：某行的源标签是 `"c"`，但权重表只有 `{"a":1,"b":2}`，什么时候报错、报什么？
**答案**：在 provider 构造期（训练启动时）抛 `ValueError`：「metadata.{source_field}='c' has no configured source weight」（[priority_provider.py:63-66](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rollout/priority_provider.py#L63-L66)）。注意这不是 `validate` 子命令能查出的——validate 只查配置结构，行级标签要在数据真正被加载后才校验。

**练习 3**：权重写 `{"a": True, "b": 1}` 或 `{"a": 0, "b": 1}` 会怎样？
**答案**：两者都在构造期抛 `ValueError`（见 [priority_provider.py:44-52](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rollout/priority_provider.py#L44-L52)）：`True` 不是合法数值（bool 被显式排除），0 不是正数。配置层 `CoreConfig.__post_init__` 也有对应的 `exclusive_min` 校验（[config.py:161-164](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/configuration/config.py#L161-L164)），所以这两个错误其实在 run 文件编译期就会先被拦下。单测 [test_miles_priority_provider.py:206-209](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_miles_priority_provider.py#L206-L209) 覆盖了这组非法权重。

### 4.3 provider_source 接入与配置约束

#### 4.3.1 概念说明

`PriorityPromptProvider` 本身不与 MILES 运行时对话；把它接进系统的是 `ProviderRolloutDataSource`（u8-l5 讲过的桥接层）。分工是：

- **外层 wrapper**（`DashboardDrainingRolloutDataSource` 及其 delegate）：拥有锁、缓冲重试队列、pending-group 台账、checkpoint 的原子保存——这些机制对所有提供者一视同仁；
- **provider**：只回答「下一批新 prompt 是哪些」，不感知缓冲、不感知重试。

对 priority 有一处特殊接线：构造数据源时会**关闭加载器的全局洗牌**。因为优先级提供者自己负责组内洗牌，加载器再做一个全局排列不仅用不上，还白白多一次全量数据遍历（启动开销）。同时 `_sync_legacy_cursor` 会把 provider 的 `(epoch_id, sample_offset)` 同步到外层的兼容游标字段，供 MILES 的 run 会计使用。

配置约束方面，`RunConfig.validate()` 在编译期（`validate`/`plan` 子命令、训练启动前）拦截所有非法组合，做到 fail-fast。

#### 4.3.2 核心流程

provider 的选择分发逻辑（伪代码）：

```text
若 core.prompt_provider == "mixture" 或 "pools" → MixturePromptProvider
若 core.prompt_provider == "priority":
    关闭加载器全局洗牌 (loader_args.rollout_shuffle = False)
    PriorityPromptProvider(dataset,
        field=core.prompt_priority_field,
        source_field=core.prompt_priority_source_field,      # 可选
        source_weights=core.prompt_priority_source_weights,  # 可选，与上者成对
        seed=args.rollout_seed, shuffle=bool(args.rollout_shuffle))
若 == "remind"  → ReMindPromptProvider
否则            → ShuffledPromptProvider（默认）
```

`get_samples(n)` 的取数顺序：先消费缓冲区里的重试组，不够的部分才向 provider 要新 prompt；每个新 prompt 被复制 \( K \) 份（`n_samples_per_prompt`，GRPO 的组采样数）并分配 group/response ID。整个投递是 **all-or-nothing**：任何一步失败，缓冲区、计数器、provider 游标、已分配的 ID 全部回滚。

编译期的 priority 约束清单（错误信息均为 `constraints.error`，`Invalid configuration` 类）：

| 约束 | 错误信息（节选） |
| --- | --- |
| priority 三件套字段只许在 `prompt_provider=priority` 时出现 | `Priority settings require core.prompt_provider=priority` |
| `prompt_priority_field` 必填 | `Priority sampling requires core.prompt_priority_field` |
| `source_field` 与 `source_weights` 必须成对 | `Priority source weighting requires both ...` |
| 必须全局数据集模式 | `Priority sampling requires rollout_global_dataset=true` |
| 不能与冻结排除表或 prompt replay 组合 | `Priority sampling cannot yet be combined with exclusions or prompt replay` |
| `num_epoch`/`num_rollout` 若设置必须是正整数 | `miles.num_epoch must be a positive integer` |

#### 4.3.3 源码精读

桥接类的职责声明与 priority 分支：

[open_instruct/miles/rollout/provider_source.py:19-29](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rollout/provider_source.py#L19-L29)
类 docstring 讲清分工：外层 Open Instruct wrapper 拥有锁与带 pending 台账的原子 checkpoint，provider 接口本身不要求 epoch 或配额抽象。L29 把 `pools` 和 `priority` 一起特判（两者都需要改写加载参数）。

关闭冗余的全局洗牌：

[provider_source.py:30-41](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rollout/provider_source.py#L30-L41)
L36-37 的注释直说：「priority 拥有自己的组内洗牌；全局排列用不上，还得多花一次全量清单遍历的启动成本」。注意改的是 `loader_args`（传给父类加载器的副本），构造 provider 时用的仍是原始 `args` 的 `rollout_shuffle`（L53），所以用户在 `[data]` 里的 `shuffle` 选择对组内行为依然生效。

priority provider 的实际构造：

[provider_source.py:46-54](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rollout/provider_source.py#L46-L54)
把 `CoreConfig` 的三个 priority 字段与 `args.rollout_seed` / `args.rollout_shuffle` 传入构造函数。行级标签错误（越界、非整数、源缺权重）就在这一刻抛出——即 train 启动、数据加载完成之后。

all-or-nothing 的取数与回滚：

[provider_source.py:105-142](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rollout/provider_source.py#L105-L142)
L108-118 先快照缓冲与计数器、深拷贝 provider 状态；L119 请求新 prompt；L122-131 把每个 prompt 复制 \( K \) 份并递增 group/response 计数；L132-140 的 `except` 把缓冲、计数器、provider 状态一并恢复后重抛——一次投递要么完整生效、要么像没发生过，这保证了 checkpoint 里 pending 台账与游标永远一致。

checkpoint 恢复时 provider 状态是权威：

[provider_source.py:144-165](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rollout/provider_source.py#L144-L165)
`load` 从全局数据集状态文件里取 `prompt_provider` 键恢复 provider；若 checkpoint 是旧版 shuffle 游标（没有 provider 状态），只有 `ShuffledPromptProvider` 允许从顶层游标字段兼容恢复，自定义 provider（含 priority）会拒绝——防止把 shuffle 的游标错装进 priority 的语义里。

配置字段与校验：

- [config.py:117-121](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/configuration/config.py#L117-L121)：三个 priority 字段的定义，默认全 `None`；`prompt_provider` 默认 `"shuffle"`。
- [config.py:148-150](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/configuration/config.py#L148-L150)：`prompt_provider` 的合法取值扩为五选一（`shuffle/remind/mixture/pools/priority`）。
- [config.py:151-164](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/configuration/config.py#L151-L164)：字段级校验——字段名必须非空文本，权重表非空、每个权重为正、总和为正。

跨选项约束：

[config.py:520-536](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/configuration/config.py#L520-L536)
L520-525：不是 priority 却带了 priority 字段 → 报错（防止字段拼错静默失效）；L526-536：是 priority 则必填字段、成对约束、`rollout_global_dataset=true`、不与 `[selection]` 排除表或 replay 组合。文档 [data-and-evaluation.md:313-318](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/docs/miles/data-and-evaluation.md#L313-L318) 同时说明该提供者只配单一 `data.rl_manifest` 或 `data.prompt_data` 输入，不能与命名 mixture/pools 同用（结构化 `data.sources` 会自动选中 mixture，见 [run_spec.py:793-799](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/configuration/run_spec.py#L793-L799)）。

#### 4.3.4 代码实践

**实践目标**：用 `validate` 子命令亲眼看三类配置报错，并区分「编译期可查出」与「数据加载后才查出」的错误。

**操作步骤**：

1. 以 `configs/miles/examples/dev.toml` 为底稿复制一份 `priority.toml`，把 `[data]` 节的 `[[data.tasks]]` 换成单文件输入（priority 不与结构化 tasks/mixture 组合）：`prompt_data = "/path/to/prompts.jsonl"`、`reward_config = "/path/to/rewards.json"`（字段见 u8-l2；具体必填项以 validate 输出为准）。
2. 报错一（缺字段）：`[core]` 只写 `prompt_provider = "priority"`，运行 `uv run python -m open_instruct.miles validate priority.toml` → 预期 `Priority sampling requires core.prompt_priority_field`。
3. 报错二（字段泄漏）：把 provider 改回 `"shuffle"` 但保留 `prompt_priority_field` → 预期 `Priority settings require core.prompt_provider=priority`。
4. 报错三（source 不成对）：写 `prompt_priority_source_field` 但漏 `prompt_priority_source_weights` → 预期 `Priority source weighting requires both ...`。
5. 行级错误（validate 查不出）：`prompts.jsonl` 里混入一行 `"metadata": {"reward_priority_olmo35_sft_hero": 5}` 或 `"1"` 或缺失——用 4.1.4 的脚本直接构造 provider，观察 `metadata.{field} must be an integer in 1..4; got 5`。
6. 也可以不改文件，用覆盖参数快速实验：`uv run python -m open_instruct.miles validate priority.toml --set core.prompt_provider='"shuffle"'`（`--set` 的值是 TOML 字面量，字符串要带引号）。

**需要观察的现象**：步骤 2–4 在 validate 阶段（CPU、无 GPU 依赖）即失败并给出可读信息；步骤 5 的行级错误只在 provider 构造（train 启动、数据加载后）出现。

**预期结果**：报错文案与 4.3.2 表格一致。完整行为对应用户测试 [test_miles_priority_provider.py:125-145](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_miles_priority_provider.py#L125-L145)（配置编译与四类校验错误）与 [test_miles_priority_provider.py:212-233](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_miles_priority_provider.py#L212-L233)（加权配置的成对校验）。「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 priority 要在加载器层面关掉 `rollout_shuffle`，而 `ShuffledPromptProvider` 不用关？
**答案**：shuffle 提供者的遍历顺序就来自加载器的全局排列；priority 的顺序完全由自己按组生成，加载器若再做全局洗牌，那个排列永远用不上，只浪费一次全量遍历与内存拷贝。集成测试 [tests/miles/test_prompt_provider.py:344-347](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/tests/miles/test_prompt_provider.py#L344-L347) 甚至 monkeypatch 了 `Dataset.shuffle` 并断言它**不被调用**（一旦调用就 `AssertionError`）。

**练习 2**：`get_samples` 为什么要做 all-or-nothing 回滚？举例说明不回滚会坏什么。
**答案**：一次 `get_samples` 会推进三个独立状态：缓冲队列（重试组）、provider 游标（offset）、group/response ID 计数器。若取数中途失败（比如 buffer_filter 抛错）只回滚一部分，checkpoint 里记录的 pending 台账和游标就会与实际不符：恢复后要么丢 prompt（游标已前进但组没投递）、要么 ID 冲突（计数器已递增）。[provider_source.py:132-140](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rollout/provider_source.py#L132-L140) 的注释点明「delivery 是全有或全无，包括缓冲重试、provider 选择状态与响应组展开时分配的 ID」。

**练习 3**：priority + replay 的组合在哪一层被拒绝？为什么不在 provider_source 里拦？
**答案**：在 `RunConfig.validate()`（[config.py:535-536](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/configuration/config.py#L535-L536)）。provider_source 的 `requires_feedback` 检查（[provider_source.py:67-69](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rollout/provider_source.py#L67-L69)）只能拦「反馈型」提供者（如 remind），priority 恰好是 `requires_feedback=False`，从那条路径拦不住。分层原则是：能用纯配置判定的问题都在编译期拦截（fail-fast、validate 可查），代码层检查只兜运行时才能发现的问题（行级标签、数据状态）。

### 4.4 epoch 预算与最终存档

#### 4.4.1 概念说明

同一个提交（#2251）给结构化 run 补上了第二类训练预算：`[training] num_epoch = 2`，含义是「把准备好的数据完整过 2 遍」。它与 `num_rollouts` 的关系是：

1. **互斥**：两者同时设置会被拒绝。原因写在错误信息里——`Choose num_epoch or num_rollouts; MILES would otherwise ignore num_epoch`（num_epoch 是原生运行时的旗标，若同时给了 rollout 数，运行时会忽略 epoch，静默用错预算）。
2. **换算发生在数据加载之后**：open-instruct 的编译层在预约 GPU 时并不知道数据集大小，所以它只把 `num_epoch` 透传为 `--num-epoch`；钉住的 MILES 原生运行时加载完准备数据后才解析出（文档 L304-306）：

\[ \text{num\_rollout} = \text{num\_epoch} \times \left\lfloor \frac{\text{dataset\_size}}{\text{rollout\_batch\_size}} \right\rfloor \]

3. **不再补默认值**：设置了 `num_epoch` 就不会再给 `num_rollout` 填缺省 100；两者都不设时缺省 100 rollouts 的旧行为保留。
4. **最终存档默认开启**：epoch 预算下编译层无法预知换算结果，也就无法编造一个「每 N 个 rollout 存一次」的周期，于是改为默认 `core.save_final_checkpoint = true`——在最后一个 rollout 完成后保存；显式 `training.save_interval` 仍可启用周期 checkpoint。

一个容易忽略的细节：换算只是**训练预算的估计**。批大小取整、在线零方差过滤、异步预取都会让「实际遍历了几遍新鲜 prompt」与 num_epoch 不严格相等（文档 L307-309 明确提醒）。另外 driver 启动时若 `num_rollout` 尚未解析（epoch run），训练器与推理引擎**不能并行启动**——epoch 派生的调度需要先拿到数据集长度。

#### 4.4.2 核心流程

完整链路（每一步都标了源码位置）：

```text
[training] num_epoch = 2                     (run.toml)
  ↓ run_spec 编译：RUN_SECTIONS 循环兜底 put("miles.num_epoch")     run_spec.py:550-551
  ↓ 互斥检查：num_epoch 与 num_rollout 只能选一个                    run_spec.py:735-739
  ↓ 保存策略：无 num_rollout 可派生 save_interval
    → core.save_final_checkpoint 默认 true                           run_spec.py:780-787
  ↓ 正整数校验（config constraints）                                  config.py:635-637
  ↓ arguments() 编码为 --num-epoch（不出现 --num-rollout）            config.py:801-822, options.json:166
  ↓ 原生 MILES 运行时加载准备数据后解析 num_rollout（上式换算，发生在仓库外）
  ↓ driver.train：num_rollout 未解析 → 训练器/推理顺序启动            driver.py:98-101
  ↓ rollout 循环 range(start, num_rollout)                            driver.py:231
  ↓ 最后一个 rollout：save_final_checkpoint 触发 checkpoint           driver.py:301-317
  ↓ 返回值携带解析后的 num_rollout                                    driver.py:430-438
  ↓ workflow 用 result["num_rollout"] 判定 complete/stopped           workflow.py:413-424
```

`save_final_checkpoint` 的触发条件与既有机制是「或」的关系（[driver.py:301-306](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/driver.py#L301-L306)）：时间预算到点、sentinel 文件、**最后一个 rollout 且开关开启**、或周期 save_interval 到期，任一满足即保存。完成判定则比较「已完成 rollout 数」与 driver 返回的 `num_rollout`。

#### 4.4.3 源码精读

训练节键的兜底映射：

[open_instruct/miles/configuration/run_spec.py:546-551](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/configuration/run_spec.py#L546-L551)
`[training]` 节的键依次尝试 FIELD_MAP（如 `num_rollouts` → `miles.num_rollout`，见 [run_spec.py:51](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/configuration/run_spec.py#L51)）、CORE_FIELDS 等，都不匹配时兜底 `put(f"miles.{key}", ...)`——`num_epoch` 就是走这条兜底进入 miles 选项表的。

互斥与默认值：

[open_instruct/miles/configuration/run_spec.py:735-739](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/configuration/run_spec.py#L735-L739)
L735-737：两者同设抛 `InputError`（信息即上面那句「MILES would otherwise ignore num_epoch」）；L738-739 的 `else` 分支说明**只有未设 num_epoch 时**才补默认 `num_rollout = 100`。

epoch 预算的保存默认：

[open_instruct/miles/configuration/run_spec.py:780-789](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/configuration/run_spec.py#L780-L789)
`save_checkpoints` 默认开启。有 `num_rollout` 时 `save_interval` 默认等于它（run 结束时恰好存一次）；epoch 预算走 `else`（L784-787），注释写明「原生 MILES 在加载数据集后才解析 epoch 预算；保存最终更新，不在这里发明周期节奏」，即 `core.save_final_checkpoint = True`。L788-789：`save_checkpoints=false` 与显式 `save_interval` 冲突报错。`save_final_checkpoint` 字段本身默认 `False`（[config.py:73](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/configuration/config.py#L73)），并在布尔校验清单里（[config.py:226-244](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/configuration/config.py#L226-L244)，含 L231）。

正整数校验与旗标编码：

- [config.py:635-637](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/configuration/config.py#L635-L637)：`num_epoch` 与 `num_rollout`、批大小等一起做「正整数」检查——单测里 `0`、`-1`、`True`（bool 不是合法 int）都会被拒。
- [options.json:164-167](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/configuration/options.json#L164-L167)：`num_rollout` 与 `num_epoch` 在运行时旗标快照中注册；[options.py:69-78](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/configuration/options.py#L69-L78) 按约定把 dest 还原成 `--num-epoch` / `--num-rollout`。`arguments()`（[config.py:801-822](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/configuration/config.py#L801-L822)）据此产出 argv——epoch run 的 argv 里只有 `--num-epoch`。

epoch run 的启动顺序约束：

[open_instruct/miles/execution/driver.py:98-101](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/driver.py#L98-L101)
L98-99 注释：「colocate/offload 共享内存；epoch 派生的调度在训练器初始化前需要 rollout 数据集长度，保持顺序启动」。L100 的并行启动条件要求 `args.num_rollout is not None`——epoch run 在 serving 组件创建并返回 `rollouts_per_epoch` 之前，num_rollout 可能还是 `None`，因此退回「先 serving 后 trainer」的顺序路径。

循环边界与最终存档：

- [driver.py:231](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/driver.py#L231)：`for rollout_id in range(args.start_rollout_id, args.num_rollout)`——epoch 换算完成后它与固定 rollout 预算的循环没有任何区别，这就是「priority 排序跟随普通预算」的含义。
- [driver.py:301-317](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/driver.py#L301-L317)：保存触发的四个「或」条件；其中 L304 是本提交新增的 `save_final_checkpoint and rollout_id == args.num_rollout - 1`。L307-314 的保存动作照旧：数据源游标与 pending 台账在同一把锁下快照，模型 `save_model + finalize_checkpoint`。

完成判定改用 driver 返回值：

- [driver.py:430-438](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/driver.py#L430-L438)：返回字典新增 `num_rollout` 字段（值为解析后的 `args.num_rollout`）。
- [workflow.py:413-424](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/workflow.py#L413-L424)：完成条件为「已完成数 == `result.get("num_rollout", config.miles.get("num_rollout"))`」。对 epoch run，`config.miles` 里**没有** `num_rollout`，只有 driver 返回的解析值可用——这正是本提交修改这一行的原因（之前直接读 config 会让 epoch run 永远判为 `stopped`）。前两个分支（pools 耗尽、不可恢复的时间停止）也是合法完成，与 u8-l3 讲过的判据一致。

#### 4.4.4 代码实践

**实践目标**：在 CPU 上验证 epoch 预算的编译行为（互斥、默认值、最终存档、旗标编码）。

**操作步骤**：

1. 跑现成单测：`uv run pytest open_instruct/test_miles_epoch_budget.py -v`。
2. 读 [test_miles_epoch_budget.py:10-36](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_miles_epoch_budget.py#L10-L36)：L21-24 断言编译产物里 `miles["num_epoch"]==2` 且**没有** `num_rollout`、`arguments()` 含 `--num-epoch` 不含 `--num-rollout`、`core.save_final_checkpoint` 为真、没有 `save_interval`；L26-30 分别在 `[training].num_rollouts` 与 `[miles].num_rollout` 里塞入冲突值，断言两类写法都被拒；L31-32 断言 `save_checkpoints=false` 时 `save_final_checkpoint` 为假；L33-36 断言 `num_epoch` 取 `0`、`-1`、`True` 都抛错。
3. 自己动手：复制 `configs/miles/examples/dev.toml`，把 `[training]` 的 `num_rollouts = 4` 换成 `num_epoch = 2`，运行 `uv run python -m open_instruct.miles plan priority.toml | grep -A2 argv`（或直接看 JSON 里的 `argv` 与 `miles` 节），再改回同时保留两者，运行 validate 观察互斥报错。

**需要观察的现象**：plan 输出的 argv 出现 `--num-epoch`、没有 `--num-rollout`；`core` 节里 `save_final_checkpoint: true`；同时设置两者时 validate 打出 `Choose num_epoch or num_rollouts`。

**预期结果**：与单测断言一致。「待本地验证」（plan 对模型/数据输入还有其他校验，若报无关错误，按提示补齐路径即可）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 epoch run 的训练器不能与推理引擎并行启动？
**答案**：并行启动的前提是所有启动参数已知。epoch 预算的 `num_rollout` 要等数据集加载后由原生运行时解析，而训练器的初始化（如学习率调度、epoch 派生的周期动作）需要这个数与 `rollouts_per_epoch`（[driver.py:98-101](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/driver.py#L98-L101)）。固定 rollout 预算在 argv 里就有这个数，可以并行；epoch run 必须先起 serving、拿到数据规模，再起 trainer。

**练习 2**：workflow 怎么判定一个 epoch run「完成」了？为什么旧代码（只读 `config.miles["num_rollout"]`）会判错？
**答案**：driver 返回值带了解析后的 `num_rollout`（[driver.py:432](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/driver.py#L432)），workflow 优先取它（[workflow.py:419-422](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/execution/workflow.py#L419-L422)）。epoch run 的 `config.miles` 里没有 `num_rollout`（编译层不补默认值），旧代码取到 `None`，`completed + 1 == None` 永远为假，跑完也会被标成 `stopped`，误导 resume 与运维判断。

**练习 3**：`save_checkpoints = false` 且设了 `num_epoch` 的 run，最后一个 rollout 会存档吗？
**答案**：不会。`core.save_final_checkpoint` 只在 `save_checkpoints`（默认 true）时被 run_spec 设为 true；显式关掉保存后它保持默认 `False`（[run_spec.py:780-787](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/configuration/run_spec.py#L780-L787)，单测 [test_miles_epoch_budget.py:31-32](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/test_miles_epoch_budget.py#L31-L32) 验证）。这类 run 也没有可恢复的 checkpoint，配合 `max_run_seconds` 时编译层还会把时间停止标记为最终停止（`max_run_final`）。

## 5. 综合实践

把本讲三个模块串成一个完整任务：**从造数据到配置报错，再到行为验证**。

**任务**：为优先级采样构造一份最小可用数据并跑通验证闭环。

1. **造数据**（示例代码，CPU 可跑）：生成 40 行 `prompt_data.jsonl`，每行形如
   ```json
   {"input": "Solve: ...", "label": "...", "metadata": {"reward_priority_olmo35_sft_hero": <1..4>, "workbench": {"source": {"candidate_id": "C036"}}}}
   ```
   组分布建议 11/120/7/20 缩为 6/24/4/6；源标签在 `C036`/`C046` 间交替。用 4.1.4 的脚本（改 `source_field="workbench.source.candidate_id"`、`source_weights={"C036": 3, "C046": 1}`）驱动 provider，观察：组序 1→2→3→4、前 12 个里 C036 占比约 3/4、跨 epoch 后洗牌变化但集合不变。
2. **跑测试并记录**：`uv run pytest open_instruct/test_miles_priority_provider.py open_instruct/test_miles_epoch_budget.py -v`，把每个用例对应到本讲的一个断言（组序、resume、身份校验、权重、耗尽重归一化、epoch 编译）。有 MILES 运行时（miles+sglang 已安装，如 MILES 镜像内）时再跑 `uv run pytest tests/miles/test_prompt_provider.py -v -k priority`，记录两个集成断言：pending 组先于新抽取恢复（[test_prompt_provider.py:309-334](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/tests/miles/test_prompt_provider.py#L309-L334)）与 epoch 游标跨轮回绕（[test_prompt_provider.py:337-357](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/tests/miles/test_prompt_provider.py#L337-L357)）。本地无运行时时该文件被 [tests/miles/conftest.py:8-10](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/tests/miles/conftest.py#L8-L10) 从收集中排除，可用 `--require-miles-runtime` 让缺失显式报错。
3. **写 TOML + validate**：按 4.3.4 的步骤启用 `prompt_provider="priority"`、带 source 权重、`[training] num_epoch = 2`，观察三类报错（缺 `prompt_priority_field`；行级组标签越界/非整数——用步骤 1 的数据混入一行 `5` 或 `"1"`，这一类要在 provider 构造时才报；源标签缺权重）；修好后确认 plan 的 argv 含 `--num-epoch` 且 `save_final_checkpoint` 为真。
4. **解释一个设计问题**：对照 [data-and-evaluation.md:290-292](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/docs/miles/data-and-evaluation.md#L290-L292)，用自己的话写 3-5 句：为什么历史优先级组与在线零方差过滤互不影响？参考要点：优先级是**导出时的静态离线标注**，provider 只读；零方差过滤发生在**奖励计算与训练批次阶段**（另一层），既不回写组标签也不改变 provider 的顺序；两者的时间尺度也不同——过滤掉的行不会立刻被重新抽取，最早要到下一个 sampler epoch 才会再次出现。这正是「历史 priority 不改变 grades 或 online zero-variance filter」的实现基础：两者根本没有共享状态。

预期产物：一份记录了组序/权重比例/三类报错文案的笔记，以及一条能通过 validate 的 `priority.toml`。GPU 上的实际训练观察「待本地验证」。

## 6. 本讲小结

- `PriorityPromptProvider` 按「组 1→2→3→4 依序耗尽」遍历：组间无采样权重、组内每行每 epoch 恰好一次；`feedback` 是 no-op，历史标签在训练中不变。
- 确定性靠三层机制：每 epoch 派生的 seeded 组内洗牌、一次性物化的顺序数组（取数=切片，对批大小稳定）、只含 `(identity, epoch_id, offset)` 的极小 checkpoint；resume 用 `assignments_sha256` 身份哈希拒绝任何配置或组员归属漂移。
- 组内混源加权（`source_field`/`source_weights` 成对）由 `_merge_sources` 一次性预计算：权重只影响顺序与早期曝光（\( P(s)=w_s/\sum_{A}w \)），源耗尽即跳过并重归一化，总量仍是每行一次；数据中出现的每个源标签都必须有权重，配置中多余的源无害。
- `ProviderRolloutDataSource` 负责桥接：priority 下关闭加载器全局洗牌（避免冗余遍历）、取数 all-or-nothing 回滚、checkpoint 恢复以 provider 状态为权威；组合约束（必填字段、成对、`rollout_global_dataset`、不与 replay/排除表/mixture 组合）在 `RunConfig.validate()` 编译期拦截。
- `training.num_epoch` 与 `num_rollouts` 互斥；epoch 预算由原生运行时在数据加载后按 `num_epoch × (dataset_size // rollout_batch_size)` 换算成 `num_rollout`，run 不再补默认值；epoch run 默认 `save_final_checkpoint=true` 在最后一个 rollout 落盘，driver 返回解析后的 `num_rollout` 供 workflow 判定完成。

## 7. 下一步学习建议

- **回到奖励层对照**：读 u8-l6（MILES 奖励体系），特别看零方差过滤与 `filter_zero_std_groups` 的实现位置，验证本讲「选择层与训练层互不干扰」的结论。
- **运维视角**：带着本讲的 `save_final_checkpoint` 知识读 u8-l8（启动、提交与运维），理解 epoch 预算 run 在 Beaker 上被抢占后 `auto_resume` 如何结合「pending 台账 + provider 游标」恢复。
- **源码延伸**：通读 [open_instruct/miles/rollout/priority_provider.py](https://github.com/allenai/open-instruct/blob/bae4209fdc541914540670c200628f67a39371ea/open_instruct/miles/rollout/priority_provider.py)（仅 181 行）与两个测试文件；再对照 `git show bae4209fd` 看这个特性如何以 14 个文件、约 740 行新增落地（含 CHANGELOG 与 reference-help 的文档同步）。
- **动手题**：仿照 u8-l5 的实践，实现一个自定义 `PromptProvider`（例如按难度标签升序遍历），复用本讲的 identity/resume 模式，并补一个对应单测。
