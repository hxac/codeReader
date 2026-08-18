# 测试时算力扩展:参数如何控制验证规模

## 1. 本讲目标

学完本讲,你应该能够:

1. 推导 `n_parallel_proof_gen`、`n_agg_trials`、`n_verification_per_proof`、`max_rounds` 与整条流水线 API 请求总量之间的数量关系,并能对任意参数组合算出请求量上界。
2. 对比 `run.sh` 的竞赛配置与 `main.py` argparse 默认值在验证强度上的差异,理解「竞赛成绩是在大规模测试时算力下取得的」这句话在代码里的具体落点。
3. 解释 `_prepare_proof_agg_tasks` 中「出现满分证明即停止该题」的早停条件,以及它与「生成-验证差距」维护之间的张力:验证强度越高,早停越可靠。

本讲是高级单元的第一篇。前面五个单元已经把流水线的每个部件拆开讲过,本讲退后一步,把所有超参数放到「测试时算力扩展」(test-time compute scaling)这把尺子下统一度量:这套代码的每一个可调参数,本质上都在回答同一个问题——你愿意为一道题花多少次生成、多少次验证。

## 2. 前置知识

### 2.1 测试时算力扩展

训练时算力花在更新模型权重上;测试时算力花在推理阶段:让模型多采样几个答案、多检查几遍、把好答案挑出来。对数学推理而言,经典做法是「best-of-n」——采样 n 个解答再挑一个。本项目的闭环(生成 → 验证 → 元验证 → 精炼,循环 R 轮)是 best-of-n 的强化版:不仅采样多个证明,还用验证器逐个打分,把高分证明连同批语喂回给生成器精炼。

### 2.2 「请求」的精确定义

本讲所有计数中的「一次请求」指对 `client.chat.completions.create` 的一次调用,即 [inference/generate.py:L23-L28](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L23-L28) 里的一个异步任务。第 2 单元已建立关键事实:多次采样不是用 API 原生 `n` 参数,而是在数据层把每行输入复制 n 份,所以 **请求数 = 输出条数 = 输入行数 × n**。

### 2.3 生成-验证差距(回顾)

第 1 讲建立的术语:生成器越强,验证器越难发现它的错误,这个差距叫生成-验证差距。README 明确主张:为维持这个差距,要扩展**验证**算力去自动标注「难验证的证明」:

> To maintain the generation-verification gap as the generator becomes stronger, we propose to scale verification compute to automatically label new hard-to-verify proofs, creating training data to further improve the verifier.([README.md:L43-L43](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/README.md#L43-L43))

本讲会看到,这个主张在推理代码里同样成立:整套配置中真正「贵」的不是生成,而是验证。

### 2.4 早停

早停(early stopping)指某道题一旦满足特定条件,就不再为它投入后续轮次的算力。本流水线的条件是「证明池中出现均值分超过 0.99999 的证明」,细节见 4.3 节。

## 3. 本讲源码地图

| 文件 | 本讲关注的片段 | 作用 |
| --- | --- | --- |
| `inference/main.py` | argparse 参数段(L19-L55) | 全部超参数的默认值定义 |
| `inference/main.py` | `__main__` 轮次循环(L397-L523) | 参数如何流经生成/验证/元验证三阶段,决定每轮请求量 |
| `inference/main.py` | `_prepare_proof_agg_tasks` 早停判断(L222-L223) | 满分早停条件,控制算力何时停止投入 |
| `inference/run.sh` | 参数配置(L9-L20) | 官方竞赛配置,与默认值对照的基准 |
| `inference/generate.py` | 数据层 n 倍复制(L139-L150) | 「请求数 = 行数 × n」的实现依据 |

## 4. 核心概念与源码讲解

### 4.1 请求量核算:从四个参数到 API 调用总数

#### 4.1.1 概念说明

读者至此已经知道每个阶段做什么,但还没有把「参数 → 请求量」的账算清楚。这个账之所以重要,是因为:

- 这套流水线没有任何内置的费用上限,`os.system` 发出的命令一经启动就会按参数全量执行;
- 跑一次竞赛配置的请求量以百万计(见 4.1.2),参数随手一改就是十倍的开销差;
- 反过来,想在本机做小规模实验,也必须先会算这个账,才知道该把参数压到多小。

参与计数的参数有四个:`n_parallel_proof_gen`(每题每轮的生成采样预算,记 \( n_{\parallel} \))、`n_agg_trials`(每题每轮的精炼组合数上限,记 \( t_{\max} \))、`n_verification_per_proof`(每条证明的验证次数,记 \( n_{\mathrm{ver}} \))、`max_rounds`(轮数上限,记 \( R_{\max} \))。另有题目总数 \( P \)。

#### 4.1.2 核心流程

先看每轮每题的生成请求数 \( g_R \)。第 1 轮没有历史证明可组合,输入就是题目本身,采样预算全额下发;第 2 轮起输入变成「组合后的精炼请求」,每条请求复制取整后的份数:

\[ g_R = \begin{cases} n_{\parallel} & R = 1 \\ t_R \cdot \left\lfloor n_{\parallel} / t_{\max} \right\rfloor \le n_{\parallel} & R \ge 2 \end{cases} \]

其中 \( t_R = \min(t_{\max}, \binom{m}{k}) \) 是该题该轮实际生成的组合数(u5-l2 已推导),\( m \) 为可用证明数、\( k = n_{\text{proofs\_to\_refine}} \)。当整除且组合凑满时 \( g_R = n_{\parallel} \),这正是 u5-l2 提过的「算力守恒式」\( t_{\max} \times \lfloor n_{\parallel}/t_{\max} \rfloor \approx n_{\parallel} \)。

再看验证。验证阶段的输入是本轮生成输出中通过完整性闸门的记录,每条复制 \( n_{\mathrm{ver}} \) 份,所以每轮每题的验证请求数上界为:

\[ v = g_R \cdot n_{\mathrm{ver}} \]

忽略截断丢弃(闸门只会让实际值更小)。若元验证未跳过,还有一项低分评价的复核,最坏再叠加一个同阶量级(见 4.2.3)。

全程总请求量上界(不考虑早停,早停只会让实际值更小):

\[ C_{\text{total}} \le P \cdot R_{\max} \cdot n_{\parallel} \cdot (1 + n_{\mathrm{ver}}) \]

代入 run.sh 竞赛配置(\( P = 18 \)(IMO2025、CMO2024、CMO2025 各 6 题)、\( n_{\parallel} = 128 \)、\( n_{\mathrm{ver}} = 64 \)、\( R_{\max} = 16 \)):

- 每轮每题:生成 \( 128 \) + 验证 \( 128 \times 64 = 8192 \),合计 \( 8320 \);
- 全程上界:\( 18 \times 16 \times 8320 = 2{,}396{,}160 \) 次,约 **240 万次请求**;
- 其中验证占 \( 8192 / 8320 \approx 98.5\% \)。

这就是「扩展验证算力」的字面含义:在这套实现里,测试时算力几乎全部花在验证上,生成只占零头。

#### 4.1.3 源码精读

**参数定义。** 四个核心参数的默认值在 argparse 段一次看全:

- [inference/main.py:L31-L34](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L31-L34) 依次定义 `n_best_proofs_to_sample=32`、`n_proofs_to_refine=1`、`n_agg_trials=32`、`n_parallel_proof_gen=128`——后者就是每题每轮的生成采样预算 \( n_{\parallel} \)。
- [inference/main.py:L41-L41](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L41-L41) 定义 `n_verification_per_proof=4`,即每条证明被验证的次数 \( n_{\mathrm{ver}} \)。
- [inference/main.py:L52-L53](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L52-L53) 定义 `start_round=1`、`max_rounds=20`。

**采样预算的轮次切换。** 整个计数模型最关键的一行:

- [inference/main.py:L448-L448](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L448-L448) `n_sample = args.n_parallel_proof_gen if R == 1 else args.n_parallel_proof_gen // args.n_agg_trials`——R1 全额、R≥2 按 `n_agg_trials` 整除。注意整除用的是**参数** `n_agg_trials` 而非该题实际组合数 \( t_R \),所以「实际组合数不足」只会让请求更少,不会破坏上界。

**预算下发给引擎。** 生成命令把 `n_sample` 作为 `--n` 传给 generate.py:

- [inference/main.py:L449-L462](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L449-L462) 拼出 `python {args.infer_script}.py ... --n {n_sample}` 并 `os.system` 执行(发布代码中 `infer_script` 未注册的毛刺见 u1-l3,不赘述)。

**验证的乘法。** 验证命令把每条证明复制 \( n_{\mathrm{ver}} \) 份:

- [inference/main.py:L480-L492](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L480-L492) 生成验证命令,第 489 行 `--n {args.n_verification_per_proof}`——这是总账里那个 64 倍乘数的直接来源。

**数据层复制。** 「请求数 = 行数 × n」的实现:

- [inference/generate.py:L139-L150](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L139-L150) 读入每一行后 `for i in range(n): submit_batch.append(item)`,每行精确复制 n 份,一个批次凑满 `batch_size` 才入队。
- [inference/generate.py:L104-L114](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L104-L114) 断点档案 `.meta` 记录 `n` 与 `batch_size`,续跑时若改动会被 assert 拦截——注意改参数做实验前必须清掉旧的输出与 `.meta`。

**并发度不影响总账。** `--num_processes`(生成默认 40、验证默认 320)与 `--batch_size`(默认 160)只改变请求的并发调度与墙钟时间,**不改变请求总数**。另注意收尾轮:循环头部 `range(args.start_round, args.max_rounds + 2)` 多出的那一轮([inference/main.py:L398-L398](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L398-L398))只调用 `prepare_proof_refinement` 刷新证明池,随后在 [inference/main.py:L445-L446](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L445-L446) `break`,不发任何请求,故不计入 \( C_{\text{total}} \)。

#### 4.1.4 代码实践:手算两套配置的请求账

1. **实践目标**:不写代码,先用 4.1.2 的公式手算两套配置,建立数量级直觉,为综合实践的脚本打底。
2. **操作步骤**:
   - 对竞赛配置代入 \( P=18, n_{\parallel}=128, n_{\mathrm{ver}}=64, R_{\max}=16 \);
   - 对一组小调试配置代入 \( P=2, n_{\parallel}=8, n_{\mathrm{ver}}=2, R_{\max}=2 \);
   - 分别算出:每轮每题请求量、全程总请求上界、验证占比。
3. **需要观察的现象**:竞赛配置的总账是否落在百万量级;调试配置是否压进了三位数。
4. **预期结果**(手算):
   - 竞赛配置:每轮每题 \( 128 \times (1+64) = 8320 \);全程 \( 18 \times 16 \times 8320 = 2{,}396{,}160 \);验证占 98.5%。
   - 调试配置:每轮每题 \( 8 \times (1+2) = 24 \);全程 \( 2 \times 2 \times 24 = 96 \) 请求(R2 的组合上界 \( \min(8, m) \times (8 \div 8) \le 8 \),不破上界),满足「小于 100 次请求」。
5. 本实践为纸笔推导,数值已手算给出;综合实践的脚本应复现这些数字。

#### 4.1.5 小练习与答案

**练习 1**:取 `n_parallel_proof_gen=100`、`n_agg_trials=32`。R≥2 的 `n_sample` 是多少?每题每轮生成请求最多多少?整除损耗有多大?

答案:`100 // 32 = 3`;每题每轮生成请求 \( \le \min(32, \binom{m}{k}) \times 3 \le 96 \)。理想守恒值是 100,实际 96,损耗 4%——非整除配置会永久损失这部分采样预算,选参数时应让 \( n_{\parallel} \) 是 \( t_{\max} \) 的整数倍(run.sh 的 128 = 4 × 32 即如此)。

**练习 2**:为什么 R=1 不做整除切换,而 R≥2 要除?

答案:R1 的输入是题目本身,不存在组合,`n_parallel_proof_gen` 条采样就是后续组合的原料,必须全额下发;R≥2 的输入已经是「证明组合」,每条组合复制 \( \lfloor n_{\parallel}/t_{\max} \rfloor \) 份,组合数 × 每组合份数 ≈ 总预算,把同一笔预算按「组合广度 × 组合内深度」重新分配。

**练习 3**:收尾轮(R = `max_rounds + 1`)会发出多少次 API 请求?

答案:0 次。该轮只执行 `prepare_proof_refinement`(把最后一轮验证结果写入证明池)后立即 `break`([inference/main.py:L445-L446](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L445-L446)),不进入任何 `os.system` 生成调用。

### 4.2 run.sh 竞赛配置 vs argparse 默认值:验证强度的对撞

#### 4.2.1 概念说明

同一份 `main.py`,默认值与官方竞赛配置跑出的请求量差一个数量级。逐参数对照这份差异,能看出官方在「算力换质量」上的取舍:钱花在验证上,而不是生成上;省在轮数和元验证上,而不是验证次数上。

#### 4.2.2 核心流程

逐参数对照(argparse 默认值 → run.sh 竞赛配置):

| 参数 | 默认值 | run.sh | 对请求账的影响 |
| --- | --- | --- | --- |
| `n_parallel_proof_gen` | 128 | 128 | 不变,生成预算恒定 |
| `n_agg_trials` | 32 | 32 | 不变,R≥2 每组合 4 次采样 |
| `n_proofs_to_refine` | 4 | **1** | 精炼请求内含 1 份证明(而非 4 份),提示变短 |
| `n_best_proofs_to_sample` | 32 | 32 | 不变 |
| `n_verification_per_proof` | 4 | **64** | **验证强度 ×16,总账的主导项** |
| `skip_meta_verification` | 关 | **开** | 元验证请求归零 |
| `max_rounds` | 20 | **16** | 轮数 −20% |

对总账的影响(均取 \( P = 18 \) 题跑满、无早停的上界):

- 默认值:每轮每题 \( 128 \times (1+4) = 640 \),全程 \( 18 \times 20 \times 640 = 230{,}400 \) 请求;
- 竞赛配置:每轮每题 \( 128 \times (1+64) = 8320 \),全程 \( 18 \times 16 \times 8320 = 2{,}396{,}160 \) 请求;
- 比值:\( 8320/640 = 13 \) 倍于每轮每题;\( 2{,}396{,}160 / 230{,}400 \approx 10.4 \) 倍于全程(轮数缩短抵消了一部分)。

#### 4.2.3 源码精读

- [inference/run.sh:L9-L20](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh#L9-L20) 竞赛配置全貌:`--n_best_proofs_to_sample 32 --n_proofs_to_refine 1 --n_agg_trials 32 --n_parallel_proof_gen 128 --n_verification_per_proof 64 --skip_meta_verification --start_round 1 --max_rounds 16`。
- [inference/run.sh:L3-L3](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh#L3-L3) 输入为三份文件拼接:`../IMO2025.json,../CMO2024.json,../CMO2025.json`,每份 6 题,共 \( P = 18 \)。
- [inference/main.py:L41-L41](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L41-L41) 默认 `n_verification_per_proof=4` 对照 run.sh 的 64——这就是 u1-l3 指出的「验证强度 ×16」的原始出处。
- [inference/main.py:L44-L44](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L44-L44) `--skip_meta_verification` 为 `store_true` 开关;主循环中 [inference/main.py:L504-L509](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L504-L509) 仅在未跳过时准备元验证输入。若不跳过,元验证请求量最坏为 \( P \cdot R_{\max} \cdot n_{\parallel} \cdot n_{\mathrm{ver}} \cdot \rho \)(\( \rho \) 为低分评价占比,≤ 1),即再叠加一个与验证同阶的量——官方配置选择直接砍掉这一环。
- [inference/main.py:L389-L389](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L389-L389) `Avg trials per statement` 日志:跑完后可从这里读到每题实际组合数 \( t_R \) 的实测均值,用来校准 4.1.2 公式中「组合凑不满」造成的偏差。

**成绩与算力的绑定。** README 的表述是:模型「achieving gold-level scores on IMO 2025 and CMO 2024 and a near-perfect 118/120 on Putnam 2024 **with scaled test-time compute**」([README.md:L44-L44](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/README.md#L44-L44))。换言之,u1-l1 记录的那份成绩单对应的正是 run.sh 这套每轮每题 8320 次请求、全程百万量级的验证配置;把参数压到默认值的 1/13,成绩不保证可复现。

#### 4.2.4 代码实践:填一张属于你自己的对照表

1. **实践目标**:把「参数差异 → 请求账差异」的推理走一遍,产出一张可复算的表格。
2. **操作步骤**:
   - 打开 [inference/main.py:L24-L53](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L24-L53),把与请求量有关的全部默认参数抄下;
   - 打开 run.sh,逐项标注「相同 / 放大 / 缩小」;
   - 用 4.1.2 公式算出两套配置在 \( P = 18 \) 下的全程上界与验证占比;
   - 追加一行「本机调试配置」:自选四个参数,使 \( P = 2 \) 时全程 < 100 请求。
3. **需要观察的现象**:哪一个参数单独翻了总账?哪一个参数几乎不影响总账?
4. **预期结果**:`n_verification_per_proof` 4→64 单独造成 13 倍(每轮每题);`n_proofs_to_refine`、`n_best_proofs_to_sample` 对请求**条数**几乎无影响(只改变单条请求的 token 长度);调试配置一例:\( 8/8/2 \)、2 题 2 轮 → 96 请求。待本地用综合实践的脚本复核。
5. 本实践不改源码,仅阅读与计算。

#### 4.2.5 小练习与答案

**练习 1**:竞赛配置把 `n_proofs_to_refine` 从默认 4 降为 1,对请求条数和 token 成本分别有什么影响?

答案:请求条数几乎不变(组合数上限仍由 `n_agg_trials=32` 决定,且 \( \binom{32}{1} = 32 \) 恰好凑满);token 成本下降——每条精炼请求只内嵌 1 份证明及其批语摘要,而非 4 份,提示词长度大约降到原来的四分之一量级(分析推断,源码未明示动机)。

**练习 2**:`max_tokens` 如何进入成本估算?

答案:main.py 把 `proof_gen_max_len`(默认 128K)与 `proof_verification_max_len`(默认 64K)分别下发给两个阶段([inference/main.py:L449-L492](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L449-L492));generate.py 又把同一值同时作为 `max_tokens` 与 `max_total_tokens` 传入([inference/generate.py:L116-L121](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L116-L121))。它们不改变请求条数,只决定单条请求的 token 上限,是成本公式中的「单价 × 用量」里的用量系数。

**练习 3**:为什么不跳过元验证时,说元验证「最坏再叠加一个与验证同阶的量」?

答案:元验证输入来自验证输出中评分 ≤ 0.75 的低分批语(u4-l3 的漏斗),每条复制 `n_meta_verification_per_rating`(默认 1)份。低分占比 \( \rho \) 事先未知,最坏 \( \rho \to 1 \),此时元验证请求数趋近验证请求数本身,总账从 \( n_{\parallel}(1 + n_{\mathrm{ver}}) \) 变成 \( n_{\parallel}(1 + 2 n_{\mathrm{ver}}) \) 量级。

### 4.3 满分早停:0.99999 阈值与生成-验证差距的维护

#### 4.3.1 概念说明

如果每道题都跑满 \( R_{\max} \) 轮,大部分算力会浪费在已经被解决的题上。流水线的止损机制是:在为下一轮准备精炼输入时,若某题的候选证明中出现「均值分超过 0.99999」的证明,就不再为该题生成任何精炼请求——该题退出循环。

这个条件与生成-验证差距的关系是本讲最值得琢磨的一点:**早停完全信任验证器**。「一致满分」是验证器给出的信号,验证器越弱,这个信号越可能失真——一个会给错误证明打满分的验证器,会让生成器在错误证明上提前收工。而压制这种「假早停」的手段,恰恰是提高 `n_verification_per_proof`:验证强度越高,「碰巧全对」的概率被指数级压低。早停的可靠性与验证算力是同一个旋钮的两面。

#### 4.3.2 核心流程

早停判断发生在 `_prepare_proof_agg_tasks`(为 R+1 轮准备输入)内部,时序如下:

1. 读取证明池旧记录;本轮新证明入库(写池发生在早停判断**之前**,所以最后一轮的证明不丢);
2. `use_old_proofs_for_refinement=True` 时把旧池证明并入候选集;
3. 若候选集中**任一**证明的 meanscore > 0.99999,`continue` 跳过该题——不生成精炼请求;
4. 否则进入 u5-l2 讲过的排序、组合、采样流程。

阈值取 0.99999 而非 1.0 是浮点防御。由于评分只有 0/0.5/1 三档,meanscore 的次高可能值是 \( (n_{\mathrm{ver}} - 0.5) / n_{\mathrm{ver}} \)(仅一条 0.5 分、其余满分):

\[ \frac{n_{\mathrm{ver}} - 0.5}{n_{\mathrm{ver}}} = 1 - \frac{0.5}{n_{\mathrm{ver}}} \le 1 - \frac{0.5}{64} \approx 0.99219 < 0.99999 \]

所以 `> 0.99999` 严格等价于「\( n_{\mathrm{ver}} \) 次验证全部给 1 分」。

「假早停」的概率刻画:设单次验证给满分的事件独立、概率为 \( p \)(可理解为验证器对该证明的置信),则一致满分概率为:

\[ P(\text{early stop}) = p^{\, n_{\mathrm{ver}}} \]

代入几组数值感受验证强度的指数效应:

| \( p \) | \( n_{\mathrm{ver}} = 4 \) | \( n_{\mathrm{ver}} = 64 \) |
| --- | --- | --- |
| 0.90 | 0.656 | \( 1.2 \times 10^{-3} \) |
| 0.95 | 0.815 | 0.038 |
| 0.99 | 0.961 | 0.526 |

读法:若验证器对一个「其实有瑕疵」的证明每次有 10% 概率误打满分,`n_verification_per_proof=4` 时该错误证明有约 65% 的概率触发早停;升到 64 后降到约 0.1%。验证算力以线性代价换取假早停概率的指数下降——这就是「扩展验证算力以维持生成-验证差距」在推理阶段的具体形态。此外,meanscore 作为均值,其标准差随 \( 1/\sqrt{n_{\mathrm{ver}}} \) 收缩,排序选优(u5-l2 的双键排序)与早停判断同时受益。

#### 4.3.3 源码精读

- [inference/main.py:L222-L223](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L222-L223) 早停判断本体:`if any(record[1] > 0.99999 for record in proof_meanscore_ratings_tuples): continue`——`record[1]` 即 meanscore,`any` 意味着候选集中一条满分即止。
- [inference/main.py:L219-L220](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L219-L220) `use_old_proofs_for_refinement` 时 `proof_meanscore_ratings_tuples += old_proof_pool`——旧池证明参与早停判断,因此一旦某题历史任何一轮出现过满分证明,之后每一轮都会早停,该题永久退出。
- [inference/main.py:L211-L218](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L211-L218) 新证明写池在早停判断之前——早停的题也会把本轮证明完整入库,只是不再产生下游请求。
- [inference/main.py:L199-L199](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L199-L199) `meanscore = float(np.mean([rating['score'] for rating in ratings]))`——均值分子上的正是 \( n_{\mathrm{ver}} \) 次验证的得分(同一证明的多条验证记录在此聚合,u5-l1 已详述)。
- [inference/main.py:L437-L438](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L437-L438) 主循环调用处传 `use_old_proofs_for_refinement=True`——确认生产路径上旧池始终参与早停。

把三讲串起来:早停是「每轮每题 8320 请求」这笔账的**唯一系统性折扣**。实际总请求量会随轮次递减——被解决的题逐轮退出,`Avg trials per statement`([inference/main.py:L389-L389](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L389-L389))也随之走低。公式给的是天花板,早停决定你离天花板多远。

#### 4.3.4 代码实践:观察 \( p^{n} \) 的指数压制

1. **实践目标**:用十行脚本把 4.3.2 的表格扩展成曲线,直观建立「验证次数指数级压低假早停」的手感。
2. **操作步骤**:在教程目录外任意临时位置新建 `early_stop_curve.py`(示例代码,不改动仓库):

```python
# 示例代码:早停概率随验证次数的指数衰减
for p in (0.90, 0.95, 0.99):
    row = [f"p={p:.2f}"] + [f"{p**n:.2e}" for n in (1, 2, 4, 8, 16, 32, 64)]
    print("\t".join(row))
```

3. **需要观察的现象**:每行数值随 n 的衰减速度;`p=0.99` 在 n=64 处是否仍停留在 0.5 附近(强验证器难以被压制,这是符合预期的——它本来就「该」早停)。
4. **预期结果**:`p=0.90` 行从 0.90 一路降到 \( 1.2 \times 10^{-3} \)(n=64);`p=0.99` 行 n=64 时约 0.53。运行结果待本地验证。
5. 思考延伸:阈值 0.99999 固定不动,若把三档评分换成连续分数,次高可能值 \( 1 - 0.5/n \) 的论证需要如何修改?

#### 4.3.5 小练习与答案

**练习 1**:`n_verification_per_proof=6` 时,meanscore > 0.99999 要求几次 1 分、几次 0.5 分?

答案:6 次全 1 分。次高可能值 \( 5.5/6 \approx 0.9167 < 0.99999 \),中间不存在任何能达到阈值的得分组合。

**练习 2**:一道题在第 3 轮触发了早停,第 4 轮它的证明池还会更新吗?第 4 轮还会为它生成精炼请求吗?

答案:会更新,不会生成。早停判断(`continue`)发生在写池([inference/main.py:L211-L218](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L211-L218))之后,本轮新证明先入库再判断;`continue` 直接跳过组合生成,且由于旧池证明并入候选([inference/main.py:L219-L220](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L219-L220)),满分证明每轮都在候选集中,该题从第 4 轮起永久早停。

**练习 3**:为什么说「早停的可靠性与验证算力是同一个旋钮的两面」?

答案:早停条件是「\( n_{\mathrm{ver}} \) 次验证一致满分」,其假阳性概率为 \( p^{n_{\mathrm{ver}}} \)。调高 `n_verification_per_proof` 同时做了两件事:让总账里的验证请求线性变多(4.1.2 的乘子),让错误的早停概率指数变低(4.3.2 的公式)。反之,压低验证次数省下的钱,直接以「更容易在坏证明上提前收工」的形式还回去——这与 README 主张的「扩展验证算力以维持生成-验证差距」([README.md:L43-L43](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/README.md#L43-L43))是同一件事的成本面与收益面。

## 5. 综合实践:写一个 estimate_cost.py 估算器

**任务**:实现一个请求量与费用估算器,输入六个量——`n_parallel_proof_gen`、`n_agg_trials`、`n_verification_per_proof`、`n_problems`、`max_rounds`、平均 token 单价(元/百万 token)——输出整条流水线的 API 请求总量上界与大致开销;对比 run.sh 竞赛配置与小型调试配置(如 8/4/4),并据此推荐一套总请求 < 100 的本机实验参数。

**操作步骤**:

1. 在教程目录外新建 `estimate_cost.py`(示例代码,不改动仓库),核心如下:

```python
# 示例代码:DeepSeekMath-V2 推理流水线请求量/费用估算器(上界模型)
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--n_parallel_proof_gen", type=int, required=True)
parser.add_argument("--n_agg_trials", type=int, required=True)
parser.add_argument("--n_verification_per_proof", type=int, required=True)
parser.add_argument("--n_problems", type=int, required=True)
parser.add_argument("--max_rounds", type=int, required=True)
parser.add_argument("--gen_avg_tokens", type=int, default=32_000)   # 单次生成请求平均 token(输入+输出)
parser.add_argument("--ver_avg_tokens", type=int, default=16_000)   # 单次验证请求平均 token
parser.add_argument("--price_per_million", type=float, required=True)
args = parser.parse_args()

n_par, n_ver = args.n_parallel_proof_gen, args.n_verification_per_proof
n_sample = n_par // args.n_agg_trials          # R>=2 每组合采样数;R1 全额 n_par
print(f"R>=2 每组合采样数 = {n_sample}"
      f"{' (警告: 不整除, 每题每轮损耗 ' + str(n_par - n_sample * args.n_agg_trials) + ' 次采样)' if n_par % args.n_agg_trials else ''}")

# 上界模型:每轮每题生成 <= n_par;验证 <= n_par * n_ver;早停与丢弃只会更低
gen_req = args.n_problems * args.max_rounds * n_par
ver_req = args.n_problems * args.max_rounds * n_par * n_ver
tokens = gen_req * args.gen_avg_tokens + ver_req * args.ver_avg_tokens
cost = tokens / 1e6 * args.price_per_million
print(f"生成请求 <= {gen_req:,}; 验证请求 <= {ver_req:,}; 合计 <= {gen_req + ver_req:,}")
print(f"验证占比 <= {ver_req / (gen_req + ver_req):.1%}")
print(f"token <= {tokens:,}; 费用 <= {cost:,.2f}(单价 {args.price_per_million}/百万 token)")
```

2. 跑三组配置对比(费用单价自定):
   - 竞赛配置:`--n_parallel_proof_gen 128 --n_agg_trials 32 --n_verification_per_proof 64 --n_problems 18 --max_rounds 16`
   - 默认值:`--n_parallel_proof_gen 128 --n_agg_trials 32 --n_verification_per_proof 4 --n_problems 18 --max_rounds 20`
   - 调试配置:`--n_parallel_proof_gen 8 --n_agg_trials 8 --n_verification_per_proof 2 --n_problems 2 --max_rounds 2`
3. 检查调试配置的合计请求数是否 < 100,并记录三组的验证占比。

**需要观察的现象**:竞赛配置与默认配置的合计请求数比值;不整除配置的损耗警告是否触发;调试配置离 100 的余量。

**预期结果**(手算上界,脚本应复现):

| 配置 | 生成请求 | 验证请求 | 合计 | 验证占比 |
| --- | --- | --- | --- | --- |
| 竞赛(18 题/16 轮/128/64) | 36,864 | 2,359,296 | **2,396,160** | 98.5% |
| 默认(18 题/20 轮/128/4) | 46,080 | 184,320 | **230,400** | 80.0% |
| 调试(2 题/2 轮/8/2) | 32 | 64 | **96** | 66.7% |

**本机推荐参数**(总请求 96 < 100):`--n_problems 2 --max_rounds 2 --n_parallel_proof_gen 8 --n_agg_trials 8 --n_verification_per_proof 2 --n_proofs_to_refine 1`。要点:`n_parallel` 取 `n_agg_trials` 的整数倍(8/8=1,无损耗);`n_proofs_to_refine=1` 使 \( \binom{m}{1} = m \),只要 R1 产出 8 个不同证明即可凑满组合数,凑不满则请求更少、上界仍成立;验证次数压到 2 意味着早停条件实际几乎不可触发(两次全满分即可达标),调试时如需观察早停行为,可临时升到 4 并相应上调请求预算。若还需更省,可退到 4/4/2 × 2 题 2 轮 = 48 请求。

脚本运行输出待本地验证;以上数值为按 4.1.2 公式的手算结果。

## 6. 本讲小结

- 请求总账公式:\( C_{\text{total}} \le P \cdot R_{\max} \cdot n_{\parallel} \cdot (1 + n_{\mathrm{ver}}) \);R=1 每题生成 \( n_{\parallel} \) 次,R≥2 每题 \( \le t_R \cdot \lfloor n_{\parallel} / t_{\max} \rfloor \) 次,算力守恒的前提是整除。
- run.sh 竞赛配置(18 题、16 轮、128 采样、64 验证)全程上界约 240 万次请求,其中验证占 98.5%——「扩展测试时算力」在这套代码里几乎等于「扩展验证算力」;README 报告的 IMO/CMO/Putnam 成绩与这套高强度配置绑定。
- 竞赛配置相对默认值的实质差异是验证强度 ×16 与砍掉元验证;`n_proofs_to_refine` 4→1 影响的是 token 而非请求条数;进程数与批大小只改墙钟不改总账。
- 早停条件「候选证明中任一 meanscore > 0.99999」在三档评分下严格等价于「\( n_{\mathrm{ver}} \) 次验证全部满分」;写池先于判断,早停的题仍会入库最后一轮证明,且因旧池并入候选而永久退出。
- 假早停概率为 \( p^{n_{\mathrm{ver}}} \):验证算力以线性代价换取早停可靠性的指数提升——这是生成-验证差距在推理侧的具体维护机制。
- 断点续跑的 `.meta` 断言要求 `n` 与 `batch_size` 与上次一致,改参数做实验前必须清理旧输出与 `.meta` 文件。

## 7. 下一步学习建议

下一讲(u6-l2)把视角从「算参数」转向「改代码」:为 `main.py` 补齐未注册的 `proof_gen_url`、`proof_rate_url`、`infer_script` 三个参数,新增 `--dry_run` 与请求上限保护,把本讲的估算器变成流水线的硬约束。建议在进入下一讲前,先通读 [inference/main.py:L397-L523](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L397-L523) 的三处 `os.system` 调用点,思考每处命令的参数分别来自哪个 args 属性、哪些属性目前是悬空的;有余力的读者可以对照 `outputs/` 目录里官方发布的预测结果,结合 [outputs/README.md](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/outputs/README.md) 思考 `average_automatic_rating` 的分布与本讲的验证强度参数有何关联(待确认,发布物不含运行配置)。
