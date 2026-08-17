# 测试时算力扩展：参数如何控制验证规模

## 1. 本讲目标

学完本讲，你应该能够：

1. 推导 `n_parallel_proof_gen`、`n_agg_trials`、`n_verification_per_proof`、`max_rounds` 这四个超参数与整条流水线 API 请求总量之间的数量关系，能手算任意一轮的请求数。
2. 对比 `run.sh` 的竞赛配置与 `main.py` 的 argparse 默认值，说清楚「验证强度 ×16」最终落在总开销的哪个部分。
3. 解释 `_prepare_proof_agg_tasks` 中 `meanscore > 0.99999` 的早停条件为什么等价于「所有验证评分都是 1 分」，以及它与 README 中「扩展验证算力以维护生成-验证差距」这句话的对应关系。
4. 写出一个 `estimate_cost.py` 成本估算器，为本地小规模实验选出一套总请求量小于 100 的参数组合。

本讲是手册的倒数第二讲。前面五个单元已经把流水线的每个零件拆开讲过，这一讲换一个视角：**不再问「这段代码怎么工作」，而问「这套系统一共要花多少钱」**。

## 2. 前置知识

### 2.1 测试时算力扩展（test-time compute scaling）

「测试时算力扩展」指模型训练完成后，在推理（测试）阶段投入更多计算来换取更好的输出质量。最常见的形式是「多次采样 + 择优」：让模型对同一道题生成 N 个答案，再用某种机制挑出最好的一个。本项目的机制是**生成—验证—精炼**多轮闭环：采样多个证明 → 验证器给每个证明打分 → 低分证明进入下一轮精炼。投入的算力就消耗在这三个阶段的 API 调用上。

### 2.2 请求计数的基本事实（承接 u2-l2）

u2-l2 已经建立了计数公式：**generate.py 的输出条数＝请求数＝输入行数 × n**。原因是引擎在数据层把每行输入复制 `n` 份，而不是使用 API 原生的采样参数。本讲的全部推导都建立在这个公式上。

### 2.3 生成-验证差距（承接 u1-l1）

生成-验证差距（generation-verification gap）指验证能力领先于生成能力：模型能看出的错误比它能主动避免的错误多。这个差距是自验证可靠性的前提——一旦生成器追上验证器，「自我检查」就形同虚设。README 的核心主张之一是：随着生成器变强，必须同步扩展验证算力来维持这个差距。本讲会看到这句话在本仓库推理代码中的具体落点。

### 2.4 本讲记号

| 记号 | 含义 | 对应参数 |
|---|---|---|
| \( N \) | 题目总数 | 输入文件题数之和 |
| \( G \) | 每题并行生成采样数 | `--n_parallel_proof_gen` |
| \( T \) | 每题每轮精炼试验（组合）数 | `--n_agg_trials` |
| \( V \) | 每个证明的验证次数 | `--n_verification_per_proof` |
| \( R_{\max} \) | 生成轮数上限 | `--max_rounds` |
| \( U_R \) | 第 \( R \) 轮仍未「退役」（未早停）的题数 | 派生量 |
| \( P_R \) | 第 \( R \) 轮实际进入验证的证明条数 | 派生量 |

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
|---|---|
| [inference/main.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py) | argparse 参数默认值（L24-L53）；`__main__` 轮次循环中三处命令拼装（L448-L523）；`_prepare_proof_agg_tasks` 的早停判断（L219-L227） |
| [inference/run.sh](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh) | 竞赛配置的全部参数取值（L9-L20） |
| [inference/generate.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py) | 「每行复制 n 份」的计数机制（L139-L142），只引用这一小段 |
| [README.md](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/README.md) | 「扩展验证算力维护生成-验证差距」的原始表述与 IMO/Putnam 成绩（L40-L44） |

## 4. 核心概念与源码讲解

### 4.1 请求计数代数：一轮到底花多少次调用

#### 4.1.1 概念说明

`main.py` 自己不发任何 HTTP 请求，它只拼出三条 shell 命令交给 `generate.py` 执行（u1-l3、u4-l1）。因此「每轮花多少次 API 调用」完全由三条命令的 `--n` 参数和各自 `input.jsonl` 的行数决定。四个超参数各管一段：

- \( G \)（`n_parallel_proof_gen`）：第 1 轮每题采多少个证明，也间接决定后续每轮的总采样预算；
- \( T \)（`n_agg_trials`）：第 2 轮起每题构造多少个「精炼试验」（证明组合），u5-l2 已讲过它和采样数的守恒关系；
- \( V \)（`n_verification_per_proof`）：每个证明被验证多少次，`meanscore` 就是这 \( V \) 次评分的均值；
- \( R_{\max} \)（`max_rounds`）：整个闭环最多转多少轮。

#### 4.1.2 核心流程

一轮（第 \( R \) 轮）的请求数按三个阶段累加：

```text
生成请求   Gen_R  = U_R × s_R
             其中 s_1 = G                      （第 1 轮：每题 1 行输入 × n=G）
             s_R  = c_R × (G // T)   (R ≥ 2)   （每题 c_R 行输入 × n=G//T）
             c_R = min(T, 该题可用的证明组合数)

验证请求   Ver_R  = P_R × V
             P_R ≤ Gen_R（截断、格式违约的样本被丢弃，重复文本被去重）

元验证请求 Meta_R ≤ f_low × P_R × V × 1
             只统计评分 ≤ 0.75 的批语，f_low 为低分比例；
             run.sh 传了 --skip_meta_verification，此项为 0
```

两个关键观察：

**第一，算力守恒。** 当 \( G \) 能被 \( T \) 整除且 \( c_R = T \) 时，\( s_R = T \times (G/T) = G \)，即第 2 轮起每题每轮的生成请求数和第 1 轮完全一样。竞赛配置 \( 32 \times 4 = 128 \) 正是 u5-l2 推导过的守恒式。反例：若 \( G//T = 0 \)（如 \( G=4, T=32 \)），第 2 轮起生成阶段零请求，流水线空转到 `max_rounds`。

**第二，验证主导总开销。** 最坏情形下（无早停、全部样本存活、跳过元验证、\( G \bmod T = 0 \)）总量有一个极简的闭式：

\[
\mathrm{Total} = R_{\max} \cdot N \cdot G \cdot (1 + V)
\]

验证请求占比为 \( \frac{V}{1+V} \)。默认配置 \( V=4 \) 时占 80%；竞赛配置 \( V=64 \) 时占 **98.5%**。也就是说，这套流水线的钱几乎全部花在「反复检查证明」上，而不是「写证明」上——这正是标题里「扩展验证规模」的字面含义。

若考虑早停，设每轮题目存活率为 \( \rho \)（\( U_{R+1} \approx \rho \, U_R \)），生成请求总量变成几何级数：

\[
\sum_{R=1}^{R_{\max}} \mathrm{Gen}_R = N \cdot G \cdot \frac{1-\rho^{R_{\max}}}{1-\rho}
\]

例如 \( \rho = 0.7 \)、\( R_{\max}=16 \) 时级数为 \( (1-0.7^{16})/0.3 \approx 3.32 \)，总开销约为最坏值的五分之一。

#### 4.1.3 源码精读

**（1）采样数在第 1 轮和后续轮之间的切换** —— [inference/main.py:448](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L448)

```python
n_sample = args.n_parallel_proof_gen if R == 1 else args.n_parallel_proof_gen // args.n_agg_trials
```

这一行是 \( s_R \) 公式的直接对应物：第 1 轮每行输入复制 \( G \) 份；第 2 轮起每题的输入行数变成约 \( T \) 个组合，每行只复制 \( \lfloor G/T \rfloor \) 份，乘积守恒回 \( G \)。注意这里是整除，\( G < T \) 时结果为 0。

**（2）生成命令把 n_sample 填进 `--n`** —— [inference/main.py:449-L462](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L449-L462)

```python
proof_gen_cmd = f"""
python {args.infer_script}.py \
--input_data_path {proof_gen_input_path} \
...
--n {n_sample}
""".strip()
os.system(proof_gen_cmd)
```

拼好的命令交给 shell 执行，`--n` 就是上面的 \( s_R \)。`batch_size` 只影响分批方式（u2-l2：批次数 \( =\lceil nL/b \rceil \)），不影响请求总数。

**（3）验证命令的 `--n` 是 `n_verification_per_proof`** —— [inference/main.py:480-L492](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L480-L492)

```python
proof_verification_cmd = f"""
python generate.py \
--input_data_path {proof_verification_input_path} \
...
--n {args.n_verification_per_proof}
""".strip()
```

验证输入的每一行（一条通过筛选的证明）都被复制 \( V \) 份——同一个证明发给验证器 \( V \) 次，取均值算 `meanscore`。

**（4）元验证命令受开关保护** —— [inference/main.py:502-L523](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L502-L523)

`if not args.skip_meta_verification:` 包住了元验证的整段准备与执行；`--n` 取 `n_meta_verification_per_rating`（默认 1）。run.sh 开了跳过开关，所以竞赛跑法里这一段完全不产生请求（呼应 u4-l3、u5-l3 的「元验证休眠」结论）。

**（5）计数的物理基础：每行复制 n 份** —— [inference/generate.py:139-L142](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L139-L142)

```python
for line in tqdm(fr, desc="Waiting Input"):
    item = json.loads(line)
    for i in range(n):
        submit_batch.append(item)
```

这四行就是「请求数 = 行数 × n」的全部实现：多次采样靠数据层复制，而非 API 参数。

**（6）收尾轮不花钱** —— [inference/main.py:445-L446](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L445-L446)

循环范围是 `range(start_round, max_rounds + 2)`，多出来的那一轮只调用 `prepare_proof_refinement` 刷新证明池然后 `break`，不拼任何生成命令。所以实际产生请求的轮数就是 \( R_{\max} \)，闭式里的 \( R_{\max} \) 不用加一。

#### 4.1.4 代码实践：手算 run.sh 的第一轮

1. **实践目标**：不写代码，纯手推竞赛配置第 1 轮的两个请求数，验证自己真的掌握了计数公式。
2. **操作步骤**：
   - 从 [run.sh:3](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh#L3) 确认输入为 IMO2025 + CMO2024 + CMO2025 三份文件；用 `grep -c '"problem_idx"' inputs/IMO2025.json` 等命令数出每份题数（应为 6 + 6 + 6 = 18）。
   - 从 [run.sh:16-L17](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh#L16-L17) 读出 \( G=128 \)、\( V=64 \)。
   - 套公式：第 1 轮生成请求 \( = N \times G \)；验证请求上界 \( = N \times G \times V \)。
3. **需要观察的现象**：验证请求数是生成请求数的整整 64 倍。
4. **预期结果**（手工推算）：生成 \( 18 \times 128 = 2304 \) 次；验证上界 \( 2304 \times 64 = 147\,456 \) 次。仅第 1 轮就超过 14 万次验证请求。
5. 本实践为纯推算，无需运行流水线即可完成。

#### 4.1.5 小练习与答案

**练习 1**：若把 `--n_parallel_proof_gen` 设为 4、`--n_agg_trials` 设为 32（其余默认），第 2 轮会发生什么？

**答案**：`n_sample = 4 // 32 = 0`（[main.py:448](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L448)），第 2 轮起生成阶段每次复制 0 份、不发出任何请求，但轮次循环仍会继续准备输入、拼命令、跑验证（输入为空），流水线空转到 `max_rounds`。配置时必须保证 \( G \ge T \)（最好整除）。

**练习 2**：第 2 轮某题生成了 30 个组合（\( c_R = 30 < T = 32 \)），该题这一轮的生成请求数是多少（\( G=128 \)）？

**答案**：\( 30 \times \lfloor 128/32 \rfloor = 30 \times 4 = 120 \)。守恒式 \( T \times (G/T) = G \) 只在组合数凑满 \( T \) 时成立；候选证明太少时组合数不足，实际请求数按比例缩小。

**练习 3**：为什么说 `batch_size` 不影响本讲的任何数字？

**答案**：`batch_size` 只决定 generate.py 主进程把复制后的请求切成多大的批次投递给队列（u2-l2），请求总量在复制那一步（generate.py L141-142）就已经定死为「行数 × n」，与分批粒度无关。

### 4.2 竞赛配置 vs 默认配置：算力花在哪里

#### 4.2.1 概念说明

同一份 `main.py`，命令行参数不同，成本可以差出一个数量级以上。u1-l3 已经指出过「run.sh 的验证强度是默认值的 16 倍（64 对 4）」这个事实；本讲把它放进 4.1 的闭式里，看它对总账的放大效果，并补齐其余参数的差异。这也回答一个初学者常见疑问：**为什么 README 敢说这些成绩是 "with scaled test-time compute"——scale 到底 scale 在哪？** 答案：主要 scale 在 \( V \) 上。

#### 4.2.2 核心流程

逐项对照两套配置（默认值取自 argparse，竞赛值取自 run.sh）：

| 参数 | 默认值（main.py） | 竞赛值（run.sh） | 倍数 | 作用 |
|---|---|---|---|---|
| `n_parallel_proof_gen`（\( G \)） | 128 | 128 | ×1 | 每题每轮生成预算 |
| `n_agg_trials`（\( T \)） | 32 | 32 | ×1 | 每题精炼组合数 |
| `n_verification_per_proof`（\( V \)） | 4 | **64** | **×16** | 每证明验证次数 |
| `max_rounds`（\( R_{\max} \)） | 20 | 16 | ×0.8 | 轮数上限 |
| `skip_meta_verification` | 关（元验证开启） | **开** | — | 竞赛跑法跳过元验证 |
| `n_best_proofs_to_sample` | 32 | 32 | ×1 | 候选池截取数 |
| `n_proofs_to_refine` | 1 | 1 | ×1 | 每个组合内证明数 |

代入闭式 \( R_{\max} \cdot N \cdot G \cdot (1 + V) \)，取 \( N = 18 \)：

| 配置 | 生成请求 | 验证请求（上界） | 合计 |
|---|---|---|---|
| 竞赛配置（V=64, R=16） | \( 16 \times 2304 = 36\,864 \) | \( 36\,864 \times 64 = 2\,359\,296 \) | **≈ 239.6 万** |
| 默认配置（V=4, R=20，忽略元验证） | \( 20 \times 2304 = 46\,080 \) | \( 46\,080 \times 4 = 184\,320 \) | ≈ 23.0 万 |

两个结论：

1. \( V \) 从 4 提到 64，总请求量放大 \( \frac{65}{5} \times \frac{16}{20} = 10.4 \) 倍——**验证次数是整条流水线最陡的成本旋钮**。
2. 竞赛跑法的最坏情形约 240 万次请求；就算早停让每轮题目数按 \( \rho=0.7 \) 衰减，总量仍在几十万量级（几何级数因子约 3.32，约 50 万次）。这是 18 道题的成本。README 中 IMO 2025 金牌、Putnam 2024 的 118/120 正是花这个量级的验证算力换来的（[README.md:44](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/README.md#L44)）。

#### 4.2.3 源码精读

**（1）四个核心参数的默认值** —— [inference/main.py:31-L34](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L31-L34)

```python
parser.add_argument("--n_best_proofs_to_sample", type=int, default=32, ...)
parser.add_argument("--n_proofs_to_refine", type=int, default=1, ...)
parser.add_argument("--n_agg_trials", type=int, default=32, ...)
parser.add_argument("--n_parallel_proof_gen", type=int, default=128)
```

`n_best_proofs_to_sample` 和 `n_proofs_to_refine` 不直接出现在请求计数公式里，它们决定组合的「形状」（从多少候选里挑、每个组合装几条证明），再经由 \( c_R \le T \) 间接影响行数。

**（2）验证强度与元验证开关的默认值** —— [inference/main.py:41-L49](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L41-L49)

```python
parser.add_argument("--n_verification_per_proof", type=int, default=4)
parser.add_argument("--skip_meta_verification", action='store_true')
...
parser.add_argument("--n_meta_verification_per_rating", type=int, default=1)
```

注意 `action='store_true'`：默认关闭（即默认**会**跑元验证）；run.sh 显式传了 `--skip_meta_verification` 才跳过。轮数默认值见 [inference/main.py:52-L53](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L52-L53)（`start_round=1`、`max_rounds=20`）。

**（3）竞赛配置全文** —— [inference/run.sh:9-L20](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh#L9-L20)

```bash
python main.py \
    --input_paths ${input_path} \
    --output_dirname ${output_dirname} \
    --proof_pool_dirname ${proof_pool_dirname} \
    --n_best_proofs_to_sample 32 \
    --n_proofs_to_refine 1 \
    --n_agg_trials 32 \
    --n_parallel_proof_gen 128 \
    --n_verification_per_proof 64 \
    --skip_meta_verification \
    --start_round 1 \
    --max_rounds 16
```

与默认值真正不同的只有三处：\( V=64 \)、跳过元验证、\( R_{\max}=16 \)。生成侧（\( G \)、\( T \)）原封不动——**官方把 scaling 预算全部押在验证侧**，这是 README 第 43 行「scale verification compute」在配置层面的直接体现。

**（4）原始表述** —— [README.md:40-L44](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/README.md#L40-L44)

> To maintain the generation-verification gap as the generator becomes stronger, we propose to scale verification compute... achieving gold-level scores on IMO 2025 and CMO 2024 and a near-perfect 118/120 on Putnam 2024 with scaled test-time compute.

「训练时扩展验证算力造数据」与「测试时扩展验证算力做评估」是同一原则的两个应用面；本仓库能直接看到的是后者。

#### 4.2.4 代码实践：核对默认值

1. **实践目标**：不读源码、只用命令行确认 argparse 默认值，并验证 u1-l3 说过的「`--help` 能用」。
2. **操作步骤**：
   - `cd inference && python main.py --help`
   - 对照输出，抄下 `n_parallel_proof_gen`、`n_agg_trials`、`n_verification_per_proof`、`max_rounds`、`skip_meta_verification` 五项的默认值。
3. **需要观察的现象**：帮助信息正常打印、进程退出码为 0；输出中**没有** `proof_gen_url`、`proof_rate_url`、`infer_script` 三个参数（它们从未被注册）。
4. **预期结果**：五个默认值依次为 128、32、4、20、False（`store_true` 型缺省）。之所以不报 `AttributeError`，是因为 `--help` 在 [main.py:55](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L55) 的 `parse_known_args` 内就打印并退出，早于 [main.py:61](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L61) 对 `args.proof_gen_url` 的访问。若环境缺少 `numpy`/`orjson`/`tqdm` 等依赖，脚本在 import 阶段就会失败，此现象待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：保持其他参数不变，把 \( V \) 从 64 降回 4，总请求量变为原来的几分之几？

**答案**：单轮总量从 \( NG(1+64) = 65NG \) 变为 \( 5NG \)，即约 \( 5/65 \approx 7.7\% \)。\( V \) 同时出现在分子和占比公式里，是唯一能让总量变化超过一个数量级的单参数。

**练习 2**：默认配置会跑元验证而 run.sh 跳过。粗估默认配置下元验证的请求量级。

**答案**：元验证输入是评分 \( \le 0.75 \) 的批语，每条最多 1 次请求（\( n_{\text{meta}}=1 \)）。批语总数上界是 \( P_R \times V \)（每条验证输出一条批语），所以元验证请求 \( \le P_R \times V \)——量级与验证请求相同，最多再翻一倍。跳过它本身就是一项显著的成本决策。

**练习 3**：run.sh 把 `max_rounds` 从默认 20 降到 16，这是为了省钱吗？从早停的角度还能怎么解释？

**答案**：省钱是一部分（线性减少最坏总量），但更自然的解释是：\( V=64 \) 时早停门槛极严（见 4.3），绝大多数能在早期拿到全 1 分的题早已退役，剩下的是真正难的题，多转几轮边际收益很低；16 轮是「预算—收益」的经验折中。仅凭仓库内容无法确证作者动机，此解释待确认。

### 4.3 早停条件与生成-验证差距的维护

#### 4.3.1 概念说明

轮次循环不会真的把每题都跑满 \( R_{\max} \) 轮。`_prepare_proof_agg_tasks` 里有一个两行的早停判断：某题的证明池里只要出现过一条「近满分」证明（\( \mathrm{meanscore} > 0.99999 \)），该题就永远不再产生精炼请求——**退役**。这个判断是整条流水线唯一的「我们相信这个证明」决策点，而它完全委托给了验证器：验证器对所有 \( V \) 次评分一致给 1 分，系统才放行。理解这个门槛的数学结构，就理解了「扩展验证算力」如何转化为「更可信的停机」。

#### 4.3.2 核心流程

**第一步：0.99999 不是「平均很高」，而是「全票通过」。** 评分契约是 0/0.5/1 三档（u3-l1）。设某证明得到 \( k \) 个评分，其中至少一个低于 1，则均值上界为（其余全是 1、最低那个是 0.5）：

\[
\mathrm{meanscore} \le \frac{k-1+0.5}{k} = 1 - \frac{0.5}{k}
\]

- \( V = 4 \)：上界 \( 1 - 0.125 = 0.875 \)
- \( V = 64 \)：上界 \( 1 - 0.0078125 \approx 0.99219 \)

两者都远低于 0.99999。反过来，只要有一个非 1 评分就不可能过线，所以：

\[
\mathrm{meanscore} > 0.99999 \iff \text{所有（成功解析的）评分都等于 } 1
\]

（「成功解析」的限定来自 u4-l2/u5-l3：解析失败的验证输出被丢弃，不参与均值。）

**第二步：\( V \) 指数级收紧门槛。** 设验证器对某条**有缺陷**的证明单次误判为 1 分的概率为 \( p \)，则该证明被误退役的概率是：

\[
P(\text{误退役}) = p^{V}
\]

| 单次误判概率 \( p \) | \( V=4 \)：\( p^4 \) | \( V=64 \)：\( p^{64} \) |
|---|---|---|
| 0.90 | 65.6% | 0.118% |
| 0.95 | 81.5% | 3.76% |
| 0.99 | 96.1% | 52.6% |

\( V \) 从 4 提到 64，把「验证器看走眼」的容忍度压缩了几个数量级——这就是测试时的「维护生成-验证差距」：**生成器越强，越需要验证器的一致性背书才肯停机**。代价是真实好证明（\( p \approx 1 \) 表示验证器每次都认可）也要凑齐全票才停，\( p = 0.99 \) 时 64 票全对的概率只有 52.6%，于是系统继续烧轮次去精炼——严格的门槛与更多的轮次是一体两面。

**第三步：退役是永久的、跨轮累积的。** 早停检查发生在合并旧证明池**之后**，所以只要历史任何一轮出现过全票证明，该题从此每轮都直接跳过：

```text
for 每道未处理完的题:
    读旧证明池 → 合并本轮新证明
    if 池中任意证明 meanscore > 0.99999:
        continue                      # 该题退役：本轮不产生任何精炼输入行
    构造 T 个组合 → 写入下一轮 proof_gen input
```

于是 4.1 中的 \( U_R \) 单调不增，总请求量从「\( R_{\max} \) 倍」收敛到几何级数 \( \frac{1-\rho^{R_{\max}}}{1-\rho} \) 倍。

#### 4.3.3 源码精读

**（1）合并旧池，再查早停** —— [inference/main.py:219-L223](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L219-L223)

```python
if use_old_proofs_for_refinement:
    proof_meanscore_ratings_tuples += old_proof_pool

if any(record[1] > 0.99999 for record in proof_meanscore_ratings_tuples):
    continue
```

`record[1]` 是 `meanscore`。`use_old_proofs_for_refinement=True` 由主循环传入（[main.py:438](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L438)），因此检查范围是「本轮新证明 + 全部历史证明」，一旦过线即永久退役。注意 `continue` 发生在证明池追加写入（L211-L218）**之后**——退役题的当轮新证明仍然入账，只是不再派生新任务。

**（2）没退役的题才进入排序与组合枚举** —— [inference/main.py:225-L227](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L225-L227)

```python
for _ in range(10):
    np.random.shuffle(proof_meanscore_ratings_tuples)
proof_meanscore_ratings_tuples = sorted(proof_meanscore_ratings_tuples,
    key=lambda x: (x[1], x[3]['self_eval_score']), reverse=True)[:n_best_proofs_to_sample]
```

排序细节（洗牌打破并列、双键降序）属于 u5-l2 的内容；本讲只需要知道：早停 `continue` 之后的代码才决定 \( c_R \)（组合数），从而决定下一轮该题的生成请求数。

**（3）退役信息如何反馈回请求量** —— [inference/main.py:430-L444](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L430-L444)

第 \( R+1 \) 轮的 `proof_gen_R{R+1}/input.jsonl` 完全由 `prepare_proof_refinement` 产出；退役题不产生行，所以下一轮 `U` 自然缩小。`main.py` 没有任何显式的「题目状态表」——**早停状态就存在证明池文件里**，这也是断点续跑天然兼容早停的原因（u5-l1 的「一次成账」性质）。

#### 4.3.4 代码实践：算出门槛的数学

1. **实践目标**：用五行 Python 验证 4.3.2 的两张表，把「0.99999 = 全票」变成亲手算过的事实。
2. **操作步骤**：

   ```python
   # 示例代码：独立小脚本，与仓库无关
   for k in (4, 64):                     # 验证次数
       print(k, 1 - 0.5 / k)             # 有一个 0.5 分时的均值上界
   for p in (0.90, 0.95, 0.99):
       print(p, round(p ** 4, 4), round(p ** 64, 6))
   ```

3. **需要观察的现象**：均值上界都小于 0.99999；\( p^{64} \) 相对 \( p^4 \) 的坍缩速度。
4. **预期结果**（手工推算）：`4 0.875`、`64 0.9921875`；`0.9 0.6561 0.001180`、`0.95 0.8145 0.037553`、`0.99 0.9606 0.525638`。
5. 结果只依赖 Python 内建运算，任何环境均可复现；如在你的机器上数字不一致，请检查是否用了整除（`0.5 / k` 不能写成 `//`）。

#### 4.3.5 小练习与答案

**练习 1**：某证明 64 次验证中有 1 次给 0.5、63 次给 1，`meanscore` 是多少？会触发早停吗？

**答案**：\( (63 \times 1 + 0.5)/64 = 63.5/64 = 0.9921875 < 0.99999 \)，不触发。早停是「一票否决」而非「平均分高」。

**练习 2**：`> 0.99999` 里的 0.99999 为什么不直接写 `>= 1.0`？

**答案**：`meanscore` 是浮点均值，\( 63.5/64 \) 这类值与 1.0 之间没有整数分档，写 `>= 1.0` 在浮点语义下与 `> 0.99999` 等价（都要求全票），但作者用 0.99999 作缓冲，规避了「全票均值是否精确等于 1.0」的浮点表示问题——\( k \times 1.0 / k \) 在 IEEE 754 下应精确为 1.0，但用一个略小的阈值更稳健，属于防御式写法。

**练习 3**：早停检查为什么放在「合并旧池之后」而不是只看本轮新证明？换成只看本轮会有什么后果？

**答案**：若只看本轮，某题第 3 轮拿到全票证明、第 4 轮恰无新全票证明时，该题会被错误地重新拉回精炼，多花 \( G \times (1+V) \) 量级的请求。放在合并后，池文件本身就是跨轮的退役标记，检查一次、永久生效。

## 5. 综合实践：写一个 estimate_cost.py 成本估算器

这是本讲的收尾任务，把 4.1 的计数代数、4.2 的配置对比、4.3 的早停衰减全部装进一个脚本。

**任务**：实现 `estimate(n_problems, n_parallel_proof_gen, n_agg_trials, n_verification_per_proof, max_rounds, ...)`，输出三阶段请求量、总量、token 量与开销；分别计算 run.sh 竞赛配置与一组小型调试配置（8/4/4）并对比；最后给出一套总请求小于 100 的本地实验参数。

**参考实现（示例代码，仓库中不存在此文件）**：

```python
# estimate_cost.py —— 示例代码
import argparse

def estimate(n_problems, G, T, V, max_rounds, survival=1.0,
             skip_meta=True, avg_gen_tokens=16384, avg_ver_tokens=4096,
             price_per_ktoken=0.0):
    n_sample = G // T
    assert n_sample >= 1, "G // T == 0：第 2 轮起将不再生成任何样本"

    gen = ver = meta = 0
    U = n_problems                      # 未退役题数
    for R in range(1, max_rounds + 1):
        s = G if R == 1 else T * n_sample   # 竞争守恒：第 2 轮起每题仍约 G 次
        g = int(U * s)
        gen += g
        ver += g * V                    # 上界：假设全部样本通过筛选
        if not skip_meta:
            meta += g * V               # 上界：假设全部评分 ≤ 0.75
        U = int(U * survival)           # 早停：每轮存活率
    tokens = gen * avg_gen_tokens + (ver + meta) * avg_ver_tokens
    return {
        "gen_requests": gen, "ver_requests": ver, "meta_requests": meta,
        "total_requests": gen + ver + meta,
        "total_tokens": tokens,
        "cost": tokens / 1000 * price_per_ktoken,
    }

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--n_problems", type=int, required=True)
    p.add_argument("--G", type=int, default=128)
    p.add_argument("--T", type=int, default=32)
    p.add_argument("--V", type=int, default=64)
    p.add_argument("--max_rounds", type=int, default=16)
    p.add_argument("--survival", type=float, default=1.0)
    args = p.parse_args()
    print(estimate(args.n_problems, args.G, args.T, args.V, args.max_rounds, args.survival))
```

**操作步骤**：

1. 把上面的参考实现存为 `estimate_cost.py`（放在 `DeepSeek-Math-V2-tutorial/` 或任何不影响源码的目录）。
2. 计算竞赛配置：`python estimate_cost.py --n_problems 18 --G 128 --T 32 --V 64 --max_rounds 16`。
3. 计算调试配置（单份 6 题文件）：`python estimate_cost.py --n_problems 6 --G 8 --T 4 --V 4 --max_rounds 2`。
4. 给调试配置加上早停衰减：`--survival 0.5`，观察总量变化。
5. 反复调小参数，找到一套 `total_requests < 100` 的组合。

**预期结果（手工推算，待本地验证）**：

| 配置 | 生成 | 验证 | 合计请求 |
|---|---|---|---|
| 竞赛（18 题, 128/32/64, R=16, survival=1） | 36 864 | 2 359 296 | **2 396 160** |
| 调试（6 题, 8/4/4, R=2, survival=1） | 96 | 384 | **480** |
| 推荐（3 题, 4/2/2, R=2, survival=1） | 24 | 48 | **72** |

**需要观察的现象与结论**：

1. 即便是「小型」的 8/4/4 配置，6 题两轮也要 480 次请求——验证开销的放大效应（\( \times V \)）在小配置下同样成立，本地实验必须把 \( V \) 一并调小。
2. 推荐配置 4/2/2 满足约束：\( G//T = 2 \ge 1 \)（第 2 轮仍有采样）、总请求 72 < 100；第 1 轮每题 4 个证明虽少，但足以观察 `proof_pool`、`meanscore`、组合生成的完整链路。`n_best_proofs_to_sample` 保持默认 32 也没关系——代码里用 `min(..., len(...))` 截断（[main.py:227](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L227)）。
3. token 开销 = 生成请求 × 平均生成 token + 验证请求 × 平均验证 token。若按参考默认（生成 16K、验证 4K），竞赛配置约 \( 3.7 \times 10^8 + 9.7 \times 10^9 \approx 10^{10} \) token——百亿量级，验证侧占 94%。代入你所用服务的千 token 单价即可得到金额。

## 6. 本讲小结

- **请求计数闭式**：跳过元验证、无早停时，总请求 \( \approx R_{\max} \cdot N \cdot G \cdot (1+V) \)；三个 `--n` 参数分别来自 [main.py:448](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L448)（生成侧 \( G \) 或 \( \lfloor G/T \rfloor \)）与 [main.py:489](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L489)（验证侧 \( V \)）。
- **算力守恒**：第 2 轮起每题每轮生成请求 \( T \times \lfloor G/T \rfloor \approx G \)，前提是 \( G \ge T \)；组合数不足时按比例缩小。
- **竞赛配置的钱花在验证上**：run.sh 相对默认值只改了三处（\( V \): 4→64、跳过元验证、\( R_{\max} \): 20→16），总请求约 240 万（18 题、最坏情形），验证占 98.5%。
- **早停 = 全票通过**：`meanscore > 0.99999` 在 0/0.5/1 三档契约下等价于「所有验证评分都是 1」；\( V \) 越大，误退役概率 \( p^V \) 指数级下降。
- **退役是永久的**：早停检查发生在合并旧证明池之后（[main.py:219-L223](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L219-L223)），池文件本身就是跨轮的退役标记，也是断点续跑兼容早停的原因。
- **维护生成-验证差距的测试时落点**：README 的「scale verification compute」在本仓库体现为把 \( V \) 调大 16 倍、并让停机决策完全依赖验证器的一致性背书。

## 7. 下一步学习建议

本讲是 u6 单元的第一讲。下一讲 **u6-l2 二次开发实践：适配新基准与工程加固** 将把本讲的成本意识落到工程上：补齐 `proof_gen_url`、`proof_rate_url`、`infer_script` 三个缺失的 argparse 参数、新增 `--dry_run` 与请求上限保护，避免本讲算出的「百万级请求」在误配置下真的跑飞。

继续阅读源码的建议：

1. 拿一套小配置（如推荐的 4/2/2）完整跑两轮，对照本讲的计数表逐项核对 `output.jsonl` 的实际行数与估算的偏差来源（截断丢弃、文本去重、组合数不足）。
2. 重读 [main.py:222](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L222) 一行，思考若把阈值改成 `> 0.9`，早停行为与总成本会如何变化——这是理解「验证算力 ↔ 停机可信度」互换关系最直接的思想实验。
