# 假想输出:planned_completion 与 logprobs 协议夹具

> 所属单元:u2 进阶 · 两条调度车道与确定性 worker
> 前置讲义:[u2-l1 legacy 定时车道](u2-l1-legacy-timing-lane.md)
> 讲义级别:intermediate

## 1. 本讲目标

上一讲(u2-l1)我们拆了 legacy 定时车道的「节拍」:TTFT 何时到、TPOT 怎么重排、`WAIT_SLICE` 为什么切成 1ms 睡。当时刻意留下了一块空白——`step_legacy` 里每个 token 的**内容**从哪来。本讲补上这块:

1. 能按 `planned_completion` 的分支表,推演出任意 `(scripted_completion, prompt, max_tokens)` 组合的输出 token 序列与 `FinishReason`。
2. 能解释 `fake_token_id` 为什么用「循环复用 prompt token」生成假输出,以及空 prompt 时 `fallback_token_id` 的必要性。
3. 能说出 logprobs 夹具的协议含义:prompt 回显的首位置为什么是 `None`、`rank = 3` 代表什么、`alternatives` 为什么故意不含被计分的 token。

一句话:上一讲解决「**什么时候**发 token」,本讲解决「**发什么** token、以及**带什么附件**(logprobs)」。

## 2. 前置知识

### 2.1 logprob、top-k 与 rank(对数概率三件套)

LLM 每生成一个 token,实际是输出整个词表上的概率分布。协议层通常上报三样东西:

- **logprob**:该 token 的对数概率。取值 \( \le 0 \),越接近 0 表示模型越确信(概率越接近 1)。
- **top-k 候选**:按分数排序的前 k 个候选 token 及其分数,即 `top_logprobs`。
- **rank**:被采样 token 在分布中的名次。PegaInfer 的契约采用 vLLM 约定:**词表中 logit 不小于该 token 的个数(含并列)**,所以 `rank = 1` 表示它是最优 token,`rank = 3` 表示有两个以上的 token 比它强。见 `TokenLogprob` 的字段注释([pegainfer-frontend/src/engine/event.rs:4-9](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/event.rs#L4-L9))。

### 2.2 prompt logprobs 与 completion logprobs

- **completion logprobs**:对**生成的** token 打分,OpenAI 兼容接口里的 `logprobs` 参数。
- **prompt logprobs**:对**输入 prompt** 的每个位置打分(vLLM 的 `prompt_logprobs`)。关键协议约定:**prompt 的第一个 token 之前没有任何上文,模型无法为它打分,所以首位置是 `None`**。契约类型的注释写明:「`None` entries where the model produced no logprob (the first position, or logprobs disabled)」,见 `PromptEcho`([pegainfer-frontend/src/engine/step.rs:171-176](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/step.rs#L171-L176))。

### 2.3 FinishReason 与「夹具」

`FinishReason` 只有三个值([pegainfer-frontend/src/engine/event.rs:12-16](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/event.rs#L12-L16)):

- `Stop`:自然停止(模型主动停,如生成出终止符)。
- `Length`:被 `max_tokens` 截断。
- `Error`:出错。

模拟引擎只产出前两种,且在**规划输出时一次性定死**,不是边发边判断——这是本讲反复出现的主题。

**夹具(fixture)**指为测试而合成的、形状与真货一致但数值是假的数据。pegainfer-sim 没有模型权重,它的 logprobs 全是常数合成的「协议夹具」:目的是让前端协议层(HTTP 序列化、SSE 分片、字段透传)可以在 CPU 上被测到,而这些分数**不是模型精度证据**。

### 2.4 与上一讲的衔接

回忆 u2-l1 的 `step_legacy` 三段式:准入循环 → 推进循环 → 停靠。准入循环里有一行:

```rust
let (pending, finish_reason) =
    planned_completion(&self.config, &request.prompt_tokens, request.max_tokens);
```

`planned_completion` 返回的 `pending` 被存进 `RunningRequest.pending`——上一讲只说了它是「反转存储、O(1) 弹出」,没说内容怎么来。本讲就从这行调用往下钻。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注的部位 |
|------|------|----------------|
| `src/lib.rs` | 引擎装配与两条调度车道 | `planned_completion` / `planned_completion_len` / `fake_token_id` 三个自由函数;两条车道对它们的调用点;`tests` 模块里的参考测试 |
| `src/logprobs.rs` | logprobs 协议夹具(私有模块) | `prompt` / `completion` / `alternatives` 三个函数与四个常数 |
| `pegainfer-frontend/src/engine/step.rs` | 前端契约(参考) | `PromptEcho` 结构定义 |
| `pegainfer-frontend/src/engine/event.rs` | 前端契约(参考) | `TokenLogprob`、`FinishReason` 结构定义 |

前两个文件在 pegainfer-sim 内,是本讲的精读对象;后两个只引用定义,帮助理解协议含义,不需要通读。

## 4. 核心概念与源码讲解

### 4.1 planned_completion:一次算清输出与终止原因

#### 4.1.1 概念说明

真实引擎的输出是前向计算逐 token 采样出来的;模拟引擎没有权重,输出必须**凭空构造**,但又必须满足两个硬约束:

1. **确定性**:同样的配置 + 请求,输出必须逐位一致,否则 e2e 断言无从谈起。
2. **可控性**:测试需要精确指定输出(比如 u4-l3 的工具调用测试要让 `<tool_call>` JSON 跨分片出现),所以输出要能被配置「脚本化」。

`planned_completion` 就是这个**单一定义点**:在请求被准入的那一刻,一次性算出「发哪些 token」和「以什么理由终止」。注意它的签名只吃 `(config, prompt_tokens, max_tokens)`——它**不看** `stop_policy`,`Stop` 不来自停止串匹配,而只来自脚本放完,这一点和真实引擎完全不同,读源码时不要脑补。

#### 4.1.2 核心流程

分支表(全文只有两个分支、三种结局):

| `scripted_completion` | `emit_count` | 输出内容 | `finish_reason` |
|---|---|---|---|
| 空(默认) | `max_tokens` | 循环复用 prompt token(见 4.2) | 恒为 `Length` |
| 非空,且 `max_tokens < len(script)` | `max_tokens` | `script` 的前缀 | `Length`(被截断) |
| 非空,且 `max_tokens >= len(script)` | `len(script)` | `script` 全文 | `Stop`(自然放完) |

统一成公式:

\[
\text{emit\_count} = \begin{cases} \text{max\_tokens} & \text{script 为空} \\ \min(\text{max\_tokens},\ |\text{script}|) & \text{script 非空} \end{cases}
\]

\[
\text{finish} = \begin{cases} \text{Stop} & \text{script 非空且 emit\_count} = |\text{script}| \\ \text{Length} & \text{其他一切情况} \end{cases}
\]

伪代码:

```
planned_completion(config, prompt_tokens, max_tokens):
    script = config.scripted_completion
    if script 为空:
        emit_count = max_tokens
        tokens[i] = fake_token_id(prompt_tokens, i, config.fallback_token_id)   # 循环复用
        finish = Length
    else:
        emit_count = min(max_tokens, len(script))
        tokens = script 的前 emit_count 个
        finish = (emit_count == len(script)) ? Stop : Length
    return (tokens 反转后存入 pending, finish)
```

三个值得咀嚼的细节:

- **没有脚本的引擎永远发不出 `Stop`**。循环 prompt token 没有「 EOS」的概念,输出只能被 `max_tokens` 挡住,所以恒为 `Length`。想要 `Stop`,必须给脚本——这让「终止原因」在测试里变成完全可编程的开关。
- **返回值是反转的**。`pending` 按「栈」方向存储(尾元素在 Vec 末尾),legacy 车道每步 `pop()` 一个即下一个该发的 token,免去 `remove(0)` 的 O(n) 搬移——这是 u2-l1 讲过的反转存储的来源。
- **两条车道消费方向相反**。legacy 车道直接把反转的 `pending` 存进 `RunningRequest` 按栈弹;profiled 车道(档案车道)却把它**再 reverse 回正序**,然后按 worker 报来的 1-based `token_index` 减一后做下标访问。同一份计划,两种吃法。

还有一个孪生函数 `planned_completion_len`:只返回长度、不物化 token。profiled 车道在做 worker preflight 时只需要「output 预算是多少」这个数(用来判超长),不需要 token 本身,于是省掉一次无谓的 Vec 分配。两者必须永远同步——这是它唯一的纪律。

#### 4.1.3 源码精读

**主函数**——[src/lib.rs:744-769](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L744-L769):先按脚本是否为空定 `emit_count` 与 `finish_reason`,再按分支填充 `pending`,最后 `pending.reverse()` 返回。文档注释(第 742-743 行)一句话点题:「Remaining tokens (reversed) plus the terminal reason. Empty pending means finish immediately after admit.」——`pending` 为空(即 `emit_count == 0`,如 `max_tokens = 0`)意味着准入后立刻终结。

**长度孪生**——[src/lib.rs:771-777](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L771-L777):`planned_completion_len` 与主函数的 `emit_count` 表达式逐字相同,只是不构造 Vec。

**旋钮入口**——[src/lib.rs:126-131](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L126-L131):`with_scripted_completion(ids)` 把脚本写进配置,消费型 builder,u1-l3 已讲过这个模式。

**legacy 车道调用点**——[src/lib.rs:681-698](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L681-L698):准入循环里求出 `(pending, finish_reason)`,`ledger.admit` 之后:若 `pending` 为空则**当场 `finish`**(第 688-691 行,不发任何 token);否则连同 `finish_reason`、`next_token_at = now + TTFT` 一起存入 `RunningRequest`(第 692-698 行)。注意 `finish_reason` 在准入时已经定死,后续推进循环只是照章执行([src/lib.rs:712-715](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L712-L715):`pending` 弹空即 `finish`)。

**profiled 车道调用点(正序还原)**——[src/lib.rs:384-400](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L384-L400):`drain_submissions` 先用 `planned_completion_len` 做 preflight 的 output 预算(第 386-389 行),准入后再调 `planned_completion` 拿到完整序列,并立刻 `completion_tokens.reverse()` 转回**正序**(第 396-398 行)。消费侧在 `apply_outcome`——[src/lib.rs:497-525](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L497-L525):worker 报来的 `token_index` 是 1-based,`checked_sub(1)` 换 0-based 后对正序 `completion_tokens` 做下标取值(第 509-517 行)。

#### 4.1.4 代码实践(本讲主实践)

1. **实践目标**:不看参考实现,亲手写出「脚本完整重放 → Stop」与「脚本被 max_tokens 截断 → Length」两个测试,验证分支表。

2. **操作步骤**:
   - 打开 `src/lib.rs` 的 `#[cfg(test)] mod tests`,先**不要**翻到第 920-952 行。
   - 复用文件里现成的两个辅助函数:`request(prompt_tokens, max_tokens, logprobs)`([src/lib.rs:820-835](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L820-L835))和 `collect_completion(config, req)`([src/lib.rs:837-865](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L837-L865)),新增两个测试(示例代码,名字可自取):

     ```rust
     // 示例代码:读者本地练习,验证后可保留或还原
     #[test]
     fn my_scripted_full_replay_stops() {
         let config = SimulatedEngineConfig::new(0.0, 100.0, 0.0, 0)
             .unwrap()
             .with_scripted_completion(vec![11, 22, 33]);
         let (tokens, _, terminal) = collect_completion(&config, request(vec![7, 9], 8, 0));
         assert_eq!(tokens, [11, 22, 33]);
         assert!(matches!(
             terminal,
             Terminal::Finished { reason: FinishReason::Stop, completion_tokens: 3, .. }
         ));
     }

     #[test]
     fn my_scripted_truncated_is_length() {
         let config = SimulatedEngineConfig::new(0.0, 100.0, 0.0, 0)
             .unwrap()
             .with_scripted_completion(vec![11, 22, 33]);
         let (tokens, _, terminal) = collect_completion(&config, request(vec![7], 2, 0));
         assert_eq!(tokens, [11, 22]);
         assert!(matches!(
             terminal,
             Terminal::Finished { reason: FinishReason::Length, completion_tokens: 2, .. }
         ));
     }
     ```
   - 运行(定时参数全 0,墙钟开销近似为零,聚焦输出语义):

     ```bash
     cargo test --release -p pegainfer-sim --lib scripted
     ```

3. **需要观察的现象**:过滤词 `scripted` 会同时命中你新增的两个测试与既有的 `scripted_completion_replays_ids_and_stops`、`scripted_completion_truncated_by_max_tokens_is_length`。

4. **预期结果**:4 个测试全部通过。然后对照 [src/lib.rs:920-952](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L920-L952) 的参考实现 diff 你的写法——若断言完全一致,说明分支表已被你内化。注意参考测试里第一个用例 `max_tokens = 8 > len(script) = 3` 走的是 `min` 收口为 3 → `Stop`;第二个 `max_tokens = 2 < 3` 截断 → `Length`。

5. 练习属于本地源码修改(往 tests 模块加代码),验证完可自行保留或还原;本讲义不改动任何源码。

#### 4.1.5 小练习与答案

**练习 1**:`with_scripted_completion(vec![11, 22, 33])` 且 `max_tokens = 3`(恰好等于脚本长度),终止原因是什么?

**答案**:`Stop`。`emit_count = min(3, 3) = 3 == len(script)`,满足「非空且放完」的 Stop 条件。边界「恰好相等」与「超过」同归 Stop——这也是 4.1.4 参考测试用 `max_tokens = 8` 的原因,8 和 3 在这个分支上等价。

**练习 2**:不配脚本、`max_tokens = 0`,请求会发生什么?

**答案**:`emit_count = 0`,`pending` 为空 → legacy 车道在准入后**立即 `finish`**,一个 token 都不发,`finish_reason = Length`(脚本为空分支恒为 Length)。对应 [src/lib.rs:688-691](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L688-L691) 的「empty pending 短路」。

**练习 3**:`planned_completion_len` 和 `planned_completion` 返回的 Vec 长度在什么配置下会不一致?

**答案**:永远不会。两者的 `emit_count` 表达式逐字相同;`planned_completion_len` 只是省掉了物化 token 的成本。它们之间的纪律是「必须同步修改」——给输出规则加新分支时,两个函数要一起动。

### 4.2 fake_token_id:循环复用 prompt token 与空 prompt 回退

#### 4.2.1 概念说明

脚本为空时,引擎仍要发 `max_tokens` 个**真实存在的 token id**。约束是:发出去的 id 必须能被 tokenizer 解码成文本——前端要把它渲染成 OpenAI 响应的 `text` 字段,一个词表外的 id 会让 detokenize 失败。最省事的保证:**直接用 prompt 里出现过的 token**。prompt 既然被送进了引擎,它的每个 id 天然在词表内。于是策略是按位置循环复用:

\[
\text{token}(i) = \text{prompt}[i \bmod |\text{prompt}|]
\]

副作用反而成了特性:输出看起来像输入的复读(prompt = "你好" 输出近似 "你好你好…"),在 e2e 日志里一眼可辨,不会和真模型输出混淆。

**空 prompt 的坑**:`[] \bmod 0` 是除零。此时没有可复用的 token,走 `fallback_token_id`——一个配置旋钮(`with_fallback_token_id`,默认 0),由调用方保证它是个合法 id。这就是「fake token 对空 prompt 走 fallback 的必要性」:**循环复用这条路径在数学上对空序列不成立**,必须有一条独立的出口。

#### 4.2.2 核心流程

```
fake_token_id(prompt_tokens, index, fallback_token_id):
    if prompt_tokens 为空: return fallback_token_id
    else:                   return prompt_tokens[index % len(prompt_tokens)]
```

以 prompt = `[7, 9]` 为例:

| index | 0 | 1 | 2 | 3 | 4 |
|---|---|---|---|---|---|
| 输出 | 7 | 9 | 7 | 9 | 7 |

`max_tokens = 3` 时输出 `[7, 9, 7]`;`max_tokens = 5` 时输出 `[7, 9, 7, 9, 7]`。

#### 4.2.3 源码精读

**函数本体**——[src/lib.rs:779-784](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L779-L784):三行逻辑,空 prompt 先返回 `fallback_token_id`,否则取模循环。

**在 planned_completion 中的使用**——[src/lib.rs:760-763](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L760-L763):`(0..emit_count).map(|index| fake_token_id(prompt_tokens, index, config.fallback_token_id))`,即上表的逐位置生成。

**fallback 旋钮**——[src/lib.rs:133-138](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L133-L138):`with_fallback_token_id`,文档注释明确「Set the token id used for empty prompts in **either** scheduler mode」——两条车道共用同一回退。

**直接单测**——[src/lib.rs:912-918](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L912-L918):`fake_token_id_cycles_prompt_tokens` 用 `[7, 9]` 验证循环、用 `[]` 验证回退到 42,是本模块最浓缩的规格说明。

**端到端佐证**——[src/lib.rs:1024-1040](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L1024-L1040):`simulated_request_emits_scheduled_tokens_and_finished` 断言 prompt `[7, 9]`、`max_tokens = 3` 输出恰为 `[7, 9, 7]` 且 `FinishReason::Length`。

#### 4.2.4 代码实践(源码阅读 + 预测验证型)

1. **实践目标**:先用手算填表,再用测试对答案,把取模循环变成肌肉记忆。

2. **操作步骤**:
   - 不看源码,先填下面表格的「你的预测」列(假设 `fallback_token_id = 42`):

     | prompt | max_tokens | 你的预测 | 参考答案 |
     |---|---|---|---|
     | `[7, 9]` | 3 | ______ | `[7, 9, 7]` |
     | `[7, 9]` | 5 | ______ | `[7, 9, 7, 9, 7]` |
     | `[]` | 2 | ______ | `[42, 42]` |
     | `[5]` | 4 | ______ | `[5, 5, 5, 5]` |
   - 第 1 行可直接用既有测试验证:`cargo test --release -p pegainfer-sim --lib simulated_request`
   - 第 2-4 行写一个 scratch 单测(示例代码),直接对纯函数逐位置断言,无需启动引擎:

     ```rust
     // 示例代码:prompt=[7,9] 的第 4 个位置回绕到 7;空 prompt 回退;单 token 循环
     #[test]
     fn my_fake_token_table() {
         assert_eq!(fake_token_id(&[7, 9], 4, 42), 7); // 4 % 2 == 0 → 回绕到首个
         assert_eq!(fake_token_id(&[], 0, 42), 42);    // 空 prompt → fallback
         assert_eq!(fake_token_id(&[5], 3, 42), 5);    // 单 token 恒为它自己
     }
     ```

     (`fake_token_id` 是 crate 根下的自由函数,tests 模块经 `use super::*` 已可直接调用。)

3. **需要观察的现象**:三行断言覆盖「循环越界回绕」「空 prompt 回退」「单 token 循环」三种形态。

4. **预期结果**:表格四行与参考答案一致,scratch 测试通过。第 2-4 行的引擎级行为(整条输出序列)可在本地用 `collect_completion` 补一个端到端断言——**待本地验证**(纯函数行为由源码可直接推出,端到端链路建议跑一次确认)。

#### 4.2.5 小练习与答案

**练习 1**:为什么不用「随机采样一个词表 id」作为无脚本输出?

**答案**:破坏确定性——同配置同请求的输出会漂移,e2e 断言(以及 u4-l3 那种要求字节级可控的协议测试)无从下手;而且随机 id 可能落在词表的任意角落,不方便人工检视。循环 prompt token 零成本地同时满足「在词表内」与「确定」。

**练习 2**:`fallback_token_id` 默认值是多少?从哪条路径能看到?

**答案**:`0`。构造路径有两条:`SimulatedEngineConfig::new(.., fallback_token_id)` 的第四个参数([src/lib.rs:69-98](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L69-L98)),以及 `Default` 实现里的 `fallback_token_id: 0`([src/lib.rs:157-170](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L157-L170));运行时用 `with_fallback_token_id` 覆盖。

### 4.3 logprobs::prompt 与 logprobs::completion:协议夹具合成

#### 4.3.1 概念说明

前端把 `logprobs` / `prompt_logprobs` 请求参数翻译成 `Request` 上的两个 `Option<usize>` 字段(`usize` 是 top-k 档位)。引擎若不管它们,前端协议层关于 logprobs 的序列化、透传、SSE 分片就永远测不到。模拟引擎的做法:在**发 token 的同一个位置**顺路合成夹具——

- 请求带 `prompt_logprobs` → 准入时(legacy 车道)一次性 `echo_prompt`,内容是 `logprobs::prompt(prompt_tokens, top_k)` 合成的 `PromptEcho`;
- 请求带 `logprobs` → 每发一个 token,配一个 `logprobs::completion(token, top_k)` 合成的 `TokenLogprob`,与 token 一起 `push_tokens`。

夹具数值全是常数(见 4.3.3),协议含义才是重点:**prompt 回显首位 `None`**(首 token 无上文,模型打不了分)、**prompt 的 rank = 3**(假装排在第三)、**completion 的 rank = 1**(假装采样 token 就是最优解)。这样前端两条路径(带 rank 的、不带 rank 的、首位 None 的)都能在 CPU 上走到。

还有一个空 prompt 的协议细节:profiled 车道里,若 `prompt_echo.ids` 为空(空 prompt + prompt_logprobs),会在准入当步立刻 `echo_prompt` 清掉这个待办([src/lib.rs:347-359](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L347-L359)),不留悬空状态。

#### 4.3.2 核心流程

```
logprobs::prompt(ids, top_k) -> PromptEcho:
    对 ids 逐个位置 i:
        i == 0  → None                      # 协议:首位置无上文,不打分
        i > 0   → TokenLogprob {
                    logprob:  -2.5,          # 常数
                    rank:     3,             # 假装第三名
                    top_logprobs: alternatives(ids[i], top_k)
                  }
    返回 PromptEcho { ids: ids 原样回显, logprobs: 上述向量 }

logprobs::completion(id, top_k) -> TokenLogprob:
    TokenLogprob { logprob: -0.25, rank: 1, top_logprobs: alternatives(id, top_k) }
```

legacy 车道的挂接时机(与 u2-l1 的三段式对齐):

- 准入循环:`if let Some(top_k) = request.prompt_logprobs { ledger.echo_prompt(id, logprobs::prompt(...)) }`——prompt 回显在准入时一次发清;
- 推进循环:每个到期 token 上 `running.logprobs.map(|top_k| logprobs::completion(token, top_k))`,有则 `vec![Some(lp)]`(与单 token 平行),无则空 `Vec`。

#### 4.3.3 源码精读

**四个常数**——[src/logprobs.rs:4-7](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/logprobs.rs#L4-L7):`PROMPT_LOGPROB = -2.5`、`PROMPT_RANK = 3`、`COMPLETION_LOGPROB = -0.25`、`FIRST_ALTERNATIVE_LOGPROB = -1.0`。注意 prompt 分数(-2.5)比 completion 分数(-0.25)低一个量级、rank 更差——夹具在数值上也做出了「prompt 复述的 token 排名靠后、生成 token 是首选」的假象,让两个路径的输出在日志里可区分。

**prompt 合成**——[src/logprobs.rs:9-25](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/logprobs.rs#L9-L25):`(index > 0).then(|| TokenLogprob { .. })` 一行实现「首位 `None`、其余位置有值」,`logprobs` 与 `ids` 严格平行(等长),这正是 `PromptEcho` 契约的要求([pegainfer-frontend/src/engine/step.rs:171-176](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/step.rs#L171-L176))。

**completion 合成**——[src/logprobs.rs:27-33](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/logprobs.rs#L27-L33):单个 `TokenLogprob`,`rank: 1`。

**legacy 车道挂接:prompt 回显**——[src/lib.rs:685-687](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L685-L687):准入时 `echo_prompt`。

**legacy 车道挂接:逐 token**——[src/lib.rs:716-729](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L716-L729):`logprob` 为 `Some` 时打包成 `vec![Some(lp)]`(与那一个 token 平行),为 `None` 时发空 `Vec`,然后 `ledger.push_tokens(running.id, &[token], &logprobs)`——**附件与 token 同帧发出**,不存在「先发 token 后补分数」的第二帧。

**profiled 车道挂接**——同一套夹具在档案车道复用:元数据构建时 `prompt_logprobs.map(|top_k| logprobs::prompt(...))` 存入 `ProfiledRequest.prompt_echo`([src/lib.rs:413-415](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L413-L415));prefill 收尾时 `echo_prompt`([src/lib.rs:493-495](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L493-L495));每个生成 token 上 `logprobs::completion(token, top_k)`([src/lib.rs:518-525](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L518-L525))。两条车道共用一个私有模块([src/lib.rs:38](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L38) 的 `mod logprobs;`),协议形状不会分叉。

#### 4.3.4 代码实践(直接断言夹具形状)

1. **实践目标**:不经引擎,直接对夹具函数断言,验证「首位 None、rank、平行长度」三个协议要点。

2. **操作步骤**:`mod logprobs` 是私有模块,但 `pub(super)` 对 crate 根可见,tests 模块(根的子模块)可以直接调用。在 `mod tests` 里加(示例代码):

   ```rust
   // 示例代码:直接检验协议夹具的形状
   #[test]
   fn my_prompt_echo_fixture_shape() {
       let echo = crate::logprobs::prompt(&[7, 9], 2);
       assert_eq!(echo.ids, [7, 9]);            // ids 原样回显
       assert_eq!(echo.logprobs.len(), 2);      // 与 ids 严格平行
       assert!(echo.logprobs[0].is_none());     // 协议:首位置无分
       let lp = echo.logprobs[1].as_ref().unwrap();
       assert_eq!(lp.rank, 3);
       assert_eq!(lp.logprob, -2.5);
       assert_eq!(lp.top_logprobs.len(), 2);

       let done = crate::logprobs::completion(9, 2);
       assert_eq!(done.rank, 1);
       assert_eq!(done.logprob, -0.25);
   }
   ```

   运行:`cargo test --release -p pegainfer-sim --lib my_prompt_echo_fixture_shape`

3. **需要观察的现象**:所有断言一次通过;特别注意 `logprobs.len() == ids.len()` 即使首位是 `None` 也成立——`None` 占位,不缩短向量。

4. **预期结果**:测试通过。数值(-2.5 / 3 / -0.25 / 1)逐一直接来自 [src/logprobs.rs:4-7](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/logprobs.rs#L4-L7) 的常数,可当场核对。

#### 4.3.5 小练习与答案

**练习 1**:`logprobs::prompt(&[7], 3)` 的 `logprobs` 字段长什么样?

**答案**:`[None]`——只有一个位置且是首位,唯一元素就是 `None`。向量长度仍与 `ids`(长度 1)平行。这对应 e2e 里「单 token prompt + prompt_logprobs」的边界用例(u4-l3 会再遇到)。

**练习 2**:请求没开 `logprobs` 时,`push_tokens` 的第三参数是什么?前端会因此收到什么?

**答案**:空 `Vec`([src/lib.rs:716-722](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L716-L722) 的 `None => Vec::new()`)。前端收到 token 而不带分数附件,响应里 logprobs 相关字段为缺省——「不开就不发」是开关语义,不是「发零分」。

**练习 3**:prompt 的 rank 是 3、completion 的 rank 是 1,这两个数在协议上各是什么意思?

**答案**:按契约注释([pegainfer-frontend/src/engine/event.rs:6-7](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/event.rs#L6-L7)),rank 是「词表中 logit 不小于该 token 的个数(含并列)」的 vLLM 约定,1-based。rank = 1 表示该 token 是分布的最优解(completion 夹具假装如此);rank = 3 表示至少还有两个 token 不弱于它(prompt 夹具假装复述 token 只排第三)。

### 4.4 alternatives:不含被计分 token 的候选表

#### 4.4.1 概念说明

`top_logprobs: Vec<(u32, f32)>` 是 top-k 候选列表。夹具的生成规则极简:从「被计分 token 的 id + 1」开始,连发 `top_k` 个 id,分数从 -1.0 起每个递减 1。两条硬规则藏在文档注释里:

1. **候选表故意不含被计分的 token 本身**;
2. **这些分数不是模型精度证据**。

为什么刻意排除?因为一旦候选表包含被计分 token,它就携带了一个隐含承诺——「表中该 token 的分数应与外层 `logprob` 字段一致」。模拟引擎根本没有分布,无法兑现这个承诺;排除它,夹具就只是「形状正确的占位数」,任何消费方都不会误读成真实的概率分布。配套的第二条规则是对测试作者的约束:断言只能针对**形状**(长度、平行性、可序列化),不能针对**数值含义**(比如「top1 分数一定最高所以生成 token 应为 top1」)。

#### 4.4.2 核心流程

```
alternatives(scored_id, top_k):
    for index in 0..top_k:
        id    = scored_id.wrapping_add(index + 1)     # 永不等于 scored_id
        score = -1.0 - index                          # 严格递减:-1.0, -2.0, -3.0, ...
```

例:`alternatives(9, 3)` = `[(10, -1.0), (11, -2.0), (12, -3.0)]`。

`wrapping_add` 的用意:即使 `scored_id` 逼近 `u32::MAX`,加法回绕后得到的仍是**彼此不同、且不等于 scored_id** 的小 id——唯一性不靠溢出检查,靠回绕语义天然保证(只要 `top_k < 2^32`)。

#### 4.4.3 源码精读

**alternatives 本体与两条注释**——[src/logprobs.rs:35-46](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/logprobs.rs#L35-L46):文档注释原文「Protocol fixtures deliberately keep the scored token out of the alternatives. These scores are not model accuracy evidence.」,函数体即上述两行算式。

**两处调用**——prompt 侧 [src/logprobs.rs:17](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/logprobs.rs#L17) 与 completion 侧 [src/logprobs.rs:31](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/logprobs.rs#L31),都以「被计分的那个 id」为种子,保证候选表与主分描述的是同一个分布位置。

#### 4.4.4 代码实践(手算 + 断言)

1. **实践目标**:验证候选表的生成算式与「不含被计分 token」约定。

2. **操作步骤**:先手算 `alternatives(9, 3)` 与 `alternatives(u32::MAX, 2)`,再在 tests 模块加断言(示例代码):

   ```rust
   // 示例代码:候选表算式与排除约定
   #[test]
   fn my_alternatives_exclude_scored_token() {
       let alts = crate::logprobs::alternatives(9, 3);
       assert_eq!(alts, vec![(10, -1.0), (11, -2.0), (12, -3.0)]);
       assert!(!alts.iter().any(|(id, _)| *id == 9));   // 被计分 token 不在表内

       let edge = crate::logprobs::alternatives(u32::MAX, 2);
       assert_eq!(edge, vec![(0, -1.0), (1, -2.0)]);    // 回绕后仍互异
       assert!(!edge.iter().any(|(id, _)| *id == u32::MAX));
   }
   ```

   运行:`cargo test --release -p pegainfer-sim --lib my_alternatives`

3. **需要观察的现象**:`u32::MAX` 情形没有 panic——`wrapping_add` 吸收了溢出;回绕出的 0、1 与 `u32::MAX` 互不相同。

4. **预期结果**:断言通过。`alternatives` 是 `pub(super) fn`,与 4.3.4 同理可从 tests 模块直达。

#### 4.4.5 小练习与答案

**练习 1**:如果某测试断言「completion 的 top_logprobs 中排名第一的 id 应等于被生成的 token id」,这个断言错在哪?

**答案**:违反夹具的两条明文规则。候选表被刻意排除被计分 token,且分数只是占位、不代表任何真实分布;这样的断言把夹具数值当成了模型精度证据,必然失败(表内 id 是 `token_id + 1` 起步)。正确的断言对象是形状:长度等于 top-k、条目可序列化、与主帧平行。

**练习 2**:`top_k = 0` 时 `alternatives` 返回什么?这合法吗?

**答案**:返回空 `Vec`(`0..0` 不产生元素)。在引擎侧这不会发生——`Request.logprobs` 的构造约定是 `(logprobs > 0).then_some(logprobs)`(见 [src/lib.rs:830](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L830)),0 档会折算成 `None`(即「不开」),根本走不到夹具。

## 5. 综合实践

**任务:写一个「输出推演器」,与引擎对拍。**

把本讲的知识压缩成一个纯函数,再用真实引擎验证它——这是「读源码建立起可执行模型」的终极检验。

1. **实践目标**:不调用 `planned_completion`,凭分支表手写 `predict`,与 `collect_completion` 的真实输出逐位对拍,覆盖全部分支与边界。

2. **操作步骤**:
   - 在 `mod tests` 里新增(示例代码):

     ```rust
     // 示例代码:手写推演器,与引擎对拍
     fn predict(script: &[u32], prompt: &[u32], max_tokens: usize, fallback: u32) -> (Vec<u32>, FinishReason) {
         if script.is_empty() {
             let tokens = (0..max_tokens)
                 .map(|i| if prompt.is_empty() { fallback } else { prompt[i % prompt.len()] })
                 .collect();
             (tokens, FinishReason::Length)
         } else {
             let emit = max_tokens.min(script.len());
             let (tokens, reason) = if emit == script.len() {
                 (script.to_vec(), FinishReason::Stop)
             } else {
                 (script[..emit].to_vec(), FinishReason::Length)
             };
             (tokens, reason)
         }
     }

     #[test]
     fn my_predictor_matches_engine() {
         let cases: Vec<(Vec<u32>, Vec<u32>, usize, u32)> = vec![
             (vec![], vec![7, 9], 3, 42),          // 无脚本:循环 prompt
             (vec![], vec![], 2, 42),               // 无脚本:空 prompt 回退
             (vec![11, 22, 33], vec![7, 9], 8, 42), // 脚本放完:Stop
             (vec![11, 22, 33], vec![7], 2, 42),    // 脚本截断:Length
             (vec![11, 22, 33], vec![7], 3, 42),    // 边界:恰好相等也是 Stop
         ];
         for (script, prompt, max_tokens, fallback) in cases {
             // new() 的第 4 个参数就是 fallback_token_id;
             // with_scripted_completion(vec![]) 与「不设脚本」行为等价(分支判断的是 is_empty)
             let config = SimulatedEngineConfig::new(0.0, 100.0, 0.0, fallback)
                 .unwrap()
                 .with_scripted_completion(script.clone());
             let (want_tokens, want_reason) = predict(&script, &prompt, max_tokens, fallback);
             let (got, _, terminal) =
                 collect_completion(&config, request(prompt.clone(), max_tokens, 0));
             assert_eq!(got, want_tokens, "case {script:?} + {prompt:?} x {max_tokens}");
             assert!(matches!(
                 terminal,
                 Terminal::Finished { reason, .. } if reason == want_reason
             ));
         }
     }
     ```
   - 运行:`cargo test --release -p pegainfer-sim --lib my_predictor -- --nocapture`

3. **需要观察的现象**:五个用例全绿;第 5 个用例(脚本长度 == max_tokens)落在 Stop 边界上,第 2 个用例走 fallback 路径。

4. **预期结果**:推演器与引擎在全部用例上逐位一致——分支表、取模循环、空 prompt 回退、Stop/Length 边界四件事同时被验证。若任何一行失败,回到 4.1.2 的分支表核对,失败本身就是学习信号。

## 6. 本讲小结

- `planned_completion` 是输出内容与终止原因的**单一定义点**:脚本为空 → 按位循环 prompt token、恒 `Length`;脚本非空 → 取 \(\min(\text{max\_tokens}, |\text{script}|)\) 个,放完全程才是 `Stop`,被截断是 `Length`;它不看 `stop_policy`,`Stop` 只来自脚本耗尽。
- 返回的 `pending` 是**反转存储**:legacy 车道按栈弹出;profiled 车道再 `reverse` 回正序、按 worker 的 1-based `token_index` 下标取值;`planned_completion_len` 是只算长度的孪生函数,供 profiled 车道 preflight 报 output 预算。
- `fake_token_id` 用 \( i \bmod |\text{prompt}| \) 循环复用 prompt token,保证 id 天然在词表内且确定;空 prompt 在数学上无解,必须走 `fallback_token_id` 旋钮(默认 0,两种车道共用)。
- `logprobs.rs` 用四个常数合成协议夹具:`PromptEcho` 首位 `None`(首 token 无上文)、与 `ids` 严格平行、prompt rank = 3;completion rank = 1(假装最优);夹具在准入时 `echo_prompt`、随每帧 token `push_tokens` 同发,两条车道共用同一私有模块。
- `alternatives` 故意**不含被计分 token**,id 从 `scored_id + 1` 起回绕递增、分数从 -1.0 起严格递减;文档注释明文:「这些分数不是模型精度证据」——断言只能针对形状,不能针对数值含义。

## 7. 下一步学习建议

本讲之后,「一条请求在 legacy 车道的完整生命周期」已无死角:准入 → 输出计划 → 节拍 → 终结。接下来按依赖链走:

1. **[u2-l3] WorkerState 状态机(上)**:进入 `src/worker.rs`,看请求在 Waiting/Prefill/Decode 三阶段间如何迁移、preflight 的两类拒绝(`ModelLengthExceeded` / `WholePrefillExceedsStepBudget`)如何用上本讲的 `planned_completion_len` 所报的 output 预算。
2. **顺带重读一个连接点**:profiled 车道把 `completion_tokens` 反转回正序后按 `token_index - 1` 取值([src/lib.rs:509-517](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L509-L517)),等学完 worker 的 `GeneratedToken` 语义再回看,会明白 1-based 下标是 worker 契约的一部分。
3. **远期预告(u4-l3)**:`with_scripted_completion` 的进阶用法——`tests/tool_call_roundtrip.rs` 用脚本 + 定制 tokenizer 精确控制解码字节流,让 `<tool_call>` JSON 跨 SSE 分片重组;`tests/frontend_e2e/logprobs.rs` 则把本讲的夹具一路断言到 HTTP 响应。到时候本讲的「首位 None」「不含被计分 token」会变成 HTTP 层的可见行为。
