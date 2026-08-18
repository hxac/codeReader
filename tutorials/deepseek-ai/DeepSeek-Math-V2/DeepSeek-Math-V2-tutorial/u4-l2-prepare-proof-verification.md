# 证明验证准备：prepare_proof_verification 解析

## 1. 本讲目标

上一讲（u4-l1）我们看清了 `main.py` 轮次编排的全貌：每一轮产出 `proof_gen`、`proof_verification`、`meta_verification` 三类子目录。本讲钻进第一个真正的「数据加工车间」——`prepare_proof_verification` 函数。学完本讲你应该能够：

1. 解释**过滤闸门**：为什么一条生成输出必须同时满足 `finish_reason == 'stop'` 且含 `</think>` 才有资格进入验证，以及两种不满足情形的**截然不同**的结局（丢样本 vs. 崩溃）。
2. 复述 **self_eval_score 解析链**：从 `Self Evaluation` 小节切出文本 → 提取所有 `\boxed{...}` → 取**最后一个非空值** → `float()` 转分数，以及这条链上两层 `try/except` 的分工。
3. 说明**记录重装**：验证提示词如何用 `proof_verification` 模板渲染并**覆盖**原来的 `messages` 字段，生成阶段的 `finish_reason` 为什么要改名成 `proof_finish_reason`，哪些旧字段会被清理掉。

## 2. 前置知识

本讲站在三讲积累之上，先快速回顾三个关键认知：

**（1）generate.py 的输出契约（u2-l1）**。`generate.py` 的 `APIModel.generate` 对每条输入产出一条记录，形式为 `{**原字段, "output": 生成文本, "finish_reason": 小写原因}`（见 [inference/generate.py:L60-L66](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L60-L66)，这段代码把生成结果合并回原记录）。`output` 是把 `reasoning_content` 与 `content` 拼接后的单字符串，思维链与正式回答之间隔着 `</think>` 标记。`finish_reason` 取值 `stop`（正常结束）或 `length`（达到 `max_tokens` 被截断）等。

**（2）格式契约（u3-l1 与 u3-l2）**。`proof_generation` 模板要求模型输出 `## Solution` 与 `## Self Evaluation` 两个小节，并在 `Self Evaluation` 里用 `\boxed{}` 给出自评分；`utils.py` 的三个解析函数正是这份契约的兑现面。提示词与解析器是同一份契约的两面——本讲我们会看到 `main.py` 如何把它们串起来。

**（3）记录复用模式（u4-l1）**。整条流水线里，一条题目对应一个不断被就地修改（`update`/`pop`）的 dict。`messages` 字段决定「这条记录下一步要向模型请求什么」。`prepare_proof_verification` 做的事本质上是：**筛掉废品 → 从生成输出里切出干净的证明和自评 → 把 `messages` 从"请写证明"换成"请评证明"**。

另外提醒一个工程事实：`main.py` 在模块顶层就执行 `parse_known_args()` 且 `--input_paths` 为必填（[inference/main.py:L20](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L20)、[inference/main.py:L55](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L55)），所以**别的脚本无法 `import main`**——本讲的实践都要「仿照重写」而不是直接导入，这也是本讲实践任务采用这种方式的原因。

## 3. 本讲源码地图

| 文件 | 关键位置 | 作用 |
| --- | --- | --- |
| `inference/main.py` | L64 | `args.proof_gen_with_self_eval` 开关的定义 |
| `inference/main.py` | L66-L116 | `prepare_proof_verification` 全函数（本讲主角） |
| `inference/main.py` | L472-L492 | 主循环中的调用点与验证阶段生成命令 |
| `inference/utils.py` | L19-L34 | `extract_boxed_answers`：括号计数提取 `\boxed{}` |
| `inference/utils.py` | L36-L42 | `_normalize_prover_output`：星号标题归一化 |
| `inference/utils.py` | L44-L46 | `extract_solution`：切出 `## Solution` 小节 |
| `inference/utils.py` | L48-L50 | `extract_self_eval`：切出 `## Self Evaluation` 小节 |
| `inference/utils.py` | L8-L17 | `read_data`：按后缀统一读 json/jsonl |
| `inference/math_templates.py` | L2-L30 | `proof_verification` 验证提示词模板 |
| `inference/generate.py` | L60-L66 | 生成输出的字段合并方式（理解字段增删的参照） |

数据流向一句话：`proof_gen_R{R}/output.jsonl` →（本函数）→ `proof_verification_R{R}/input.jsonl`。

## 4. 核心概念与源码讲解

### 4.1 过滤闸门：什么样的证明才有资格被验证

#### 4.1.1 概念说明

证明生成阶段用较高温度采样出大量候选证明，其中难免混有「废品」：写到一半撞上 `max_tokens` 被截断的（`finish_reason == 'length'`）、或者输出格式异常的。验证（请另一个模型逐份评 0/0.5/1 分）是流水线里最烧钱的环节之一——每个证明默认要被独立验证 `n_verification_per_proof=4` 次。**在花钱之前先把废品扔掉**，就是这个模块存在的意义。

过滤条件有两个，且缺一不可：

- `finish_reason == 'stop'`：模型自己认为写完了，不是被截断；
- 输出中含有 `</think>`：思维链确实闭合了，后面跟着正式回答。

#### 4.1.2 核心流程

```text
对 proof_gen 输出的每条记录：
    1. 把 finish_reason 弹出并小写化，改名为 proof_finish_reason
    2. 取 statement = question，prover_output = output
    3. 若 proof_finish_reason != 'stop'：
           丢弃该样本（continue，静默跳过）
    4. 断言 '</think>' 在 prover_output 中
           —— 不满足则抛 AssertionError，整个准备阶段崩溃
    5. proof = prover_output 按 "</think>" 切分后的最后一段（去掉思维链）
```

注意第 3、4 步的不对称：截断样本被**静默丢弃**，而 `stop` 却没有 `</think>` 属于「违反输出契约的异常」，代码选择**当场爆炸**而不是继续。

#### 4.1.3 源码精读

函数开头与过滤段在 [inference/main.py:L66-L79](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L66-L79)，这段代码完成读取、改名、双重检查与思维链剥离：

```python
def prepare_proof_verification(path, tar_path):
    print(f"Proof Verification >>>\ninput path = {path}\noutput_path = {tar_path}", flush=True)
    items = read_data(path)
    data = []
    for item in items:
        item['proof_finish_reason'] = item.pop('finish_reason').lower()
        statement = item['question'].strip()
        prover_output = item['output'].strip()
        if item['proof_finish_reason'] == 'stop':
            assert '</think>' in prover_output
            proof = prover_output.split("</think>")[-1].strip()
        else:
            continue
        item['prover_output'] = prover_output
```

逐行拆解：

- **L71**：`item.pop('finish_reason').lower()` —— 弹出并小写化。`generate.py` 其实已经小写过（[inference/generate.py:L64](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L64)），这里是防御性的二次小写。**改名为 `proof_finish_reason` 是为下一段生成让路**：验证阶段的 `generate.py` 调用会写入一个新的 `finish_reason`（验证器自己的完成状态），若不改名就会被同名覆盖、生成侧信息丢失。下游 `prepare_meta_verification` 读的 `item['finish_reason']`（L124）正是验证器的新字段。
- **L74-L78**：闸门主体。`continue` 保证截断样本直接出局；`assert` 则在「声称正常结束却没有闭合思维链」时抛异常——注意它**没有**被 try 包住，一旦触发，整个 `prepare_proof_verification` 连同 `main.py` 进程一起终止。设计意图可理解为：截断是正常采样现象，静默丢弃即可；`stop` 而无 `</think>` 说明上游服务行为异常，宁可崩溃也不让脏数据流入验证。
- **L76**：`prover_output.split("</think>")[-1].strip()` —— 用**最后一个** `</think>` 切分取尾段，思维链被整段丢弃。若模型在正式回答里又写了 `</think>`（罕见），也只会取最后一段。
- **L79**：完整的原始输出（含思维链）以 `prover_output` 字段**保留**在记录里随行——后面你会发现正式送验证的只有切出来的 `proof`，但原文仍可追溯。

再看调用点 [inference/main.py:L472-L478](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L472-L478)，这段代码在主循环里以「目标文件是否存在」为条件调用本函数（存在即跳过，断点续跑的关键）：

```python
proof_verification_input_path = f"{output_dirname}/proof_verification_R{R}/input.jsonl"
...
if not os.path.exists(proof_verification_input_path):
    prepare_proof_verification(
        path=proof_gen_output_path,
        tar_path=proof_verification_input_path
    )
```

随后 [inference/main.py:L489](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L489) 的生成命令带上 `--n {args.n_verification_per_proof}`（默认 4，注册于 [inference/main.py:L41](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L41)）。结合 u2-l2 的计数公式：设 \( s \) 为通过闸门的证明数、\( n \) 为 `n_verification_per_proof`，则验证输出条数（即评分数）\( = s \times n \)。闸门每拦掉一条废品，就省下 \( n \) 次验证调用——这就是过滤的直接经济价值。

#### 4.1.4 代码实践

1. **实践目标**：亲手确认闸门对三种典型样本的处置差异。
2. **操作步骤**：在仓库外任一临时目录新建 `gate_demo.py`（不要放进 `inference/`，避免污染源码目录）：

   ```python
   # 示例代码：只模拟 main.py L71-L79 的闸门逻辑
   records = [
       {"name": "A-正常",    "finish_reason": "stop",   "output": "思考……</think>\n## Solution\n正文"},
       {"name": "B-截断",    "finish_reason": "length", "output": "思考……</think>\n## Solu"},   # 未写完
       {"name": "C-无标记",  "finish_reason": "stop",   "output": "我直接写答案"},               # 无 </think>
   ]
   for item in records:
       reason = item.pop("finish_reason").lower()
       out = item["output"].strip()
       if reason == "stop":
           assert "</think>" in out, item["name"]      # C 会在这里爆炸
           item["proof"] = out.split("</think>")[-1].strip()
       else:
           print(f"{item['name']} 被丢弃"); continue
       print(f"{item['name']} 通过, proof = {item['proof']!r}")
   ```

   运行 `python gate_demo.py`。
3. **需要观察的现象**：B 被打印「被丢弃」；A 正常通过且 `proof` 不再含「思考……」；进程在处理 C 时抛出 `AssertionError: C-无标记`。
4. **预期结果**：输出顺序为「A 通过 → B 被丢弃 → AssertionError」。把 C 的 `finish_reason` 改成 `length` 再跑，则不再崩溃，A、C 都走「被丢弃」分支——体会 `continue` 与 `assert` 两种处置的差别。（脚本极小，预期如上；如与你本地结果不符，请以本地为准并回读 L74-L78。）

#### 4.1.5 小练习与答案

**练习 1**：如果把 L75 的 `assert` 换成 `continue`，流水线会变得更健壮吗？有什么隐患？
**答案**：进程不会崩，但 `stop` 且无 `</think>` 的样本会被**静默**丢掉，上游 API 服务异常（正常结束却没输出思维链闭合标记）就被掩盖了；当前写法用崩溃「大声报错」，两种取舍各有道理，属于发布代码的保守选择。

**练习 2**：`proof` 变量为什么取 `split("</think>")[-1]` 而不是 `[1]`？
**答案**：`[-1]` 取最后一个分隔符之后的尾段。若思维链或正文中出现多个 `</think>`，`[1]` 会把后续内容错当思维链的一部分留在前面、取错段落；`[-1]` 恒定取「最后一次切换之后的正式回答」。

**练习 3**：某轮生成输出共 1280 条，其中 1190 条 `finish_reason == 'stop'` 且含 `</think>`，`n_verification_per_proof` 取 run.sh 的 64。验证阶段会发出多少次 API 请求？
**答案**：\( 1190 \times 64 = 76160 \) 次。闸门拦下的 90 条废品省了 \( 90 \times 64 = 5760 \) 次调用。

### 4.2 切分与打分：proof、self_eval 与 self_eval_score 的解析链

#### 4.2.1 概念说明

通过闸门后，`proof` 目前还是「正式回答全文」——按格式契约它应包含 `## Solution` 与 `## Self Evaluation` 两节。本模块做三件事：

1. **切出自评**：`extract_self_eval` 取 `## Self Evaluation` 之后的内容；
2. **切出纯解**：`extract_solution` 取两节之间的内容——注意完成后 `proof` 变量被覆盖为**只含解答**的文本，验证器看不到生成器的自我评价；
3. **解析自评分**：对自评文本跑 `extract_boxed_answers`，取最后一个非空结果转 `float`，得到 `self_eval_score`。这个分数在两讲之后（u5-l2）会与验证均分一起构成证明排序的双键。

这一切只在 `args.proof_gen_with_self_eval` 为真时执行。该开关在 [inference/main.py:L64](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L64) 定义——只有生成模板是 `proof_generation` 时才成立；换用自定义模板时整段跳过，`proof` 保持「去思维链后的全文」。

#### 4.2.2 核心流程

```text
（已通过闸门，proof = 去思维链后的全文）
若生成模板带自评（proof_gen_with_self_eval）：
    try:
        self_eval = extract_self_eval(proof)          # 缺 Self Evaluation 节 → IndexError
        proof     = extract_solution(proof)           # 缺 Solution 节       → IndexError
        try:
            self_eval_score = float(最后一个非空 boxed 值)   # 无 boxed / 非数字 → 0
        except:
            self_eval_score = 0                       # 样本保留，只是记 0 分
    except:
        continue                                      # 样本整条丢弃
```

**两层异常处理的分工是本模块的灵魂**：

- 缺小节（结构坏）→ 外层 `except` → `continue`，**样本丢弃**；
- 自评里缺 `\boxed{}` 或写成 `\boxed{1/2}` 这类 `float()` 不认识的串（内容坏但结构好）→ 内层 `except` → 记 0 分，**样本保留**。

#### 4.2.3 源码精读

主逻辑在 [inference/main.py:L81-L97](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L81-L97)，这段代码完成自评切分、纯解覆盖与两层异常兜底：

```python
self_eval = 'null'
self_eval_score = 0
if item['proof_finish_reason'] == 'stop' and args.proof_gen_with_self_eval:
    try:
        self_eval = extract_self_eval(proof).strip()
        proof = extract_solution(proof).strip()
        try:
            self_eval_score = float([s.strip() for s in extract_boxed_answers(self_eval) if s.strip()][-1])
        except:
            self_eval_score = 0
    except:
        continue

    item['self_eval'] = self_eval
    item['self_eval_score'] = self_eval_score

item['proof'] = proof
```

几个必讲的细节：

- **L83 的第一个条件是死代码**：能走到这里的一定是 `stop`（非 `stop` 在 L78 已 `continue`），真正起作用的是后半 `args.proof_gen_with_self_eval`。阅读时直接忽略前半即可。
- **L85-L86 的顺序不可颠倒**：`extract_self_eval` 与 `extract_solution` 都要吃**含两小节的全文**；先用全文切出自评，再覆盖 `proof` 为纯解。若先覆盖，自评就再也切不出来了。
- **L88 的取值规则**：列表推导过滤空串后取 `[-1]`——**最后一个** `\boxed{}` 的值才是最终自评分。这与模板「最终分数写在 `\boxed{}` 里」的约定一致；若自评里先出现过别的 boxed 值（如中间步骤分），只有最后一个作数。
- **L90 与 L92 两个裸 `except`** 分别落在内外两层，语义如上面的流程图；`float("1")` → `1.0`、`float("0.5")` → `0.5`，而 `float("1/2")`、`float("50%")` 抛 `ValueError` → 记 0 分。

支撑它的三个解析函数都在 `utils.py`（u3-l2 已精读过算法，这里只标定位置与在本链路中的角色）：

- [inference/utils.py:L48-L50](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/utils.py#L48-L50)：`extract_self_eval` 按 `\n## Self Evaluation\n` 切分取尾段，标题缺失时 `split` 结果只有一个元素，`[1]` 抛 `IndexError`——正是外层 `except` 捕获的对象。
- [inference/utils.py:L44-L46](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/utils.py#L44-L46)：`extract_solution` 先切掉 `Self Evaluation` 再取 `## Solution` 之后的内容，即「两节之间」的纯解。
- [inference/utils.py:L19-L34](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/utils.py#L19-L34)：`extract_boxed_answers` 用括号深度计数器提取任意嵌套的 `\boxed{...}` 内容，未闭合的静默丢弃——所以「自评只有半个 boxed」与「没有 boxed」同待遇：列表为空，`[-1]` 抛 `IndexError`，走内层 `except` 记 0 分。

两者之间还有 [inference/utils.py:L36-L42](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/utils.py#L36-L42) 的 `_normalize_prover_output` 兜底：模型把标题写成 `* Solution *`、`**Self Evaluation**` 等星号变体时，先被正则归一化成标准 `## Solution` / `## Self Evaluation` 再切分（宽容归一化 + 严格切分，u3-l2 的核心结论）。

#### 4.2.4 代码实践

1. **实践目标**：用真实解析函数走通「切分 → 提取 → 打分」全链，并亲测两类异常的不同结局。
2. **操作步骤**：先 `pip install regex`（`utils.py` 依赖第三方 `regex` 包，非标准库 `re`）。新建 `parse_demo.py`：

   ```python
   # 示例代码：驱动 utils.py 的三个解析函数
   import sys; sys.path.insert(0, "路径/到/DeepSeek-Math-V2/inference")
   from utils import extract_solution, extract_self_eval, extract_boxed_answers

   full = """* Solution *
   设 x=1 代入即得。
   * Self Evaluation *
   步骤完整，自评为 \\boxed{1}"""

   se = extract_self_eval(full).strip()
   sol = extract_solution(full).strip()
   score = float([s.strip() for s in extract_boxed_answers(se) if s.strip()][-1])
   print("self_eval =", repr(se)); print("solution =", repr(sol)); print("score =", score)

   # 异常路径一：缺 Self Evaluation 小节 → IndexError → 对应 main.py 的 continue
   try:
       extract_self_eval("## Solution\n只有解没有自评")
   except IndexError as e:
       print("缺节 → IndexError（main.py 丢弃样本）")

   # 异常路径二：自评无 boxed → [-1] 越界 → 对应记 0 分保留
   se2 = "我觉得写得不错但没有分数"
   try:
       float([s.strip() for s in extract_boxed_answers(se2) if s.strip()][-1])
   except IndexError:
       print("缺 boxed → IndexError（main.py 记 self_eval_score = 0，样本保留）")
   ```

3. **需要观察的现象**：星号标题被自动识别；`score` 打印 `1.0`；两个异常路径分别命中两层 `except`。
4. **预期结果**：`self_eval` 为 `"自评文本 + \\boxed{1}"`（不含 `* Solution *` 部分），`solution` 为 `"设 x=1 代入即得。"`，`score = 1.0`；随后打印两行异常路径说明。把 `\\boxed{1}` 换成 `\\boxed{1/2}` 再跑，第三行应变为触发内层异常（`float("1/2")` 抛 `ValueError`）——本讲按代码推演如上，具体打印格式待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`Self Evaluation` 小节缺失与小节里缺 `\boxed{}`，结局有何不同？为什么这样设计？
**答案**：前者抛 `IndexError` 被外层捕获 → 样本整条丢弃；后者（或 boxed 值不能转 float）被内层捕获 → 记 `self_eval_score = 0`、样本保留。结构损坏意味着无法可靠切出「解」本身，验证无从谈起；结构完好只是自评缺失，解仍然可验证，自评分只用于后续排序，缺省 0 即可。

**练习 2**：模型的自评是「整体不错，个别步骤存疑，倾向给 \boxed{0.5}，但重看后改为 \boxed{1}」，`self_eval_score` 是多少？
**答案**：`1.0`。`extract_boxed_answers` 会提取出 `["0.5", "1"]`，L88 取 `[-1]` 即最后一个非空值 `1`——「以最终结论为准」。

**练习 3**：为什么 L85-L86 必须先 `extract_self_eval` 后 `extract_solution`？
**答案**：两个函数都需要完整的两节文本作为输入；第二行执行后 `proof` 已被覆盖为纯解答文本，若顺序颠倒，`extract_self_eval` 拿到的输入里已没有 `Self Evaluation` 小节，必然抛 `IndexError`，所有样本都会被误杀。

### 4.3 记录重装：模板渲染与字段增删

#### 4.3.1 概念说明

到这里，一条通过考核的样本手里有：`question`（题面）、`proof`（纯解）、`self_eval`/`self_eval_score`（自评）、`prover_output`（生成原文）、`proof_finish_reason`（生成侧完成标记），以及从最初输入一路继承下来的 `problem_idx`、`source_name` 等字段。本模块完成最后一步「重装」：

1. 用 `proof_verification` 模板把 `statement`（题面）与 `proof`（纯解）渲染成验证提示词；
2. 用 `item.update({'messages': ...})` **覆盖**原 `messages`——记录从「请写证明」切换为「请评证明」；
3. 弹掉生成阶段的残留字段，让记录干干净净地进入下一次 `generate.py` 调用。

#### 4.3.2 核心流程

```text
question_text = proof_verification 模板.format(statement=题面, proof=纯解)
item['messages'] = [{'role': 'user', 'content': question_text}]   # 覆盖旧 prompt
for key in ['finished', 'finish_reason', 'input', 'output']:
    item.pop(key)                                                  # 清理生成残留
把 item 追加进 data
全部处理完后逐行写入 tar_path（proof_verification_R{R}/input.jsonl）
```

#### 4.3.3 源码精读

渲染与清理段在 [inference/main.py:L99-L111](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L99-L111)，这段代码把纯解填进验证模板并清理生成阶段字段：

```python
question = math_templates[args.proof_verification_template].format(
    statement=statement.strip(),
    proof=proof.strip()
)
item.update({
    'messages': [
        {'role': 'user', 'content': question},
    ]
})
for key in ['finished', 'finish_reason', 'input', 'output']:
    if key in item:
        item.pop(key)
data.append(item)
```

- **L99-L102**：模板默认是 `proof_verification`（[inference/main.py:L40](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L40)）。模板全文在 [inference/math_templates.py:L2-L30](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L2-L30)，其中两个占位符的位置：题面填入 `## Problem` 下的 `{statement}`（[inference/math_templates.py:L26](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L26)），纯解填入 `## Solution` 下的 `{proof}`（[inference/math_templates.py:L29](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L29)）。模板要求验证器以 `\\boxed{}` 收尾给出 0/0.5/1 分（[inference/math_templates.py:L19](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py#L19)，字面大括号双写以兼容 `str.format`）——这个约定正是下一讲元验证阶段用同一个 `extract_boxed_answers` 反向解析的依据。
- **注意 `proof` 只是纯解**：`self_eval` 小节不会出现在验证提示词里——验证器对生成器的自我评价一无所知，独立打分，避免被自评带偏。
- **L103-L107**：`update` 覆盖 `messages`。R1 时旧值是 `proof_generation` 提示词（[inference/main.py:L419-L423](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L419-L423)），R≥2 时是 `proof_refinement` 提示词；一律被替换。这就是学习目标里说的「messages 替换原始 input/output 字段」的前半句。
- **L108-L110**：清理四个键。`output` 是生成阶段的大文本字段（来自 [inference/generate.py:L63](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L63) 的合并），必须弹掉，否则验证输出记录会背着一份几万 token 的生成原文；`finish_reason` 其实早在 L71 已被 pop 改名，这里的 `if key in item` 对它是空操作，属于防御性写法；`finished` 与 `input` 在当前 `generate.py` 输出中并不出现，是兼容其它推理引擎（如 vLLM 风格输出）的兜底。要保留的生成痕迹都已换了名字存在：`proof_finish_reason`、`prover_output`、`proof`、`self_eval`、`self_eval_score`。

落盘与返回在 [inference/main.py:L112-L116](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L112-L116)，这段代码建目录、逐行写出验证输入并返回存活样本数：

```python
os.makedirs(os.path.dirname(tar_path), exist_ok=True)
with open(tar_path, "w") as file:
    for item in data:
        print(json.dumps(item), file=file, flush=True)
return len(data)
```

两个阅读注记：其一，项目通篇用 `print(json.dumps(...), file=file)` 写 JSONL（`"w"` 整文件重写，区别于 `generate.py` 断点续跑用的 `"a"` 追加）；其二，返回值 `len(data)` 在调用点（L475-L478）**并未被接收**，只是个方便手动调试的计数。R≥2 时记录里还会多出一个 `dep_proof_ids`（精炼来源证明编号，由 u5 的聚合逻辑写入），同样随本函数一路透传到验证与元验证输出。

#### 4.3.4 代码实践

1. **实践目标**：亲手渲染一份验证提示词，确认两个占位符的落位与字段清理的效果。
2. **操作步骤**：新建 `render_demo.py`：

   ```python
   # 示例代码：渲染 proof_verification 模板并演练字段清理
   import sys; sys.path.insert(0, "路径/到/DeepSeek-Math-V2/inference")
   from math_templates import math_templates

   prompt = math_templates["proof_verification"].format(
       statement="证明：任意偶数的平方是偶数。".strip(),
       proof="设偶数 n=2k，则 n²=4k²=2·(2k²) 为偶数。".strip()
   )
   print(prompt)

   item = {"question": "题面", "output": "旧生成文本", "finish_reason": "stop",
           "proof": "纯解", "messages": [{"role": "user", "content": "旧的生成提示"}]}
   item["messages"] = [{"role": "user", "content": prompt}]
   for key in ["finished", "finish_reason", "input", "output"]:
       if key in item:
           item.pop(key)
   print("剩余字段:", sorted(item.keys()))
   ```

3. **需要观察的现象**：打印的提示词里 `## Problem` 下是你的题面、`## Solution` 下是你的纯解；末尾剩余字段里不再有 `output`。
4. **预期结果**：提示词含 `## Problem`、`## Solution` 两节及 `\\boxed{}` 评分要求（`str.format` 不会碰双写的 `{{...}}`，输出中保留字面 `{...}`）；剩余字段为 `['messages', 'proof', 'question']`——`finish_reason` 因早已不在 `item` 中而无事发生，`finished`/`input` 本就不存在。格式细节待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `finish_reason` 要改名成 `proof_finish_reason`，而不是直接删掉？
**答案**：验证阶段的 `generate.py` 会给每条验证记录写入一个新的 `finish_reason`（验证器自己是否正常结束）。不改名就会被覆盖，生成侧「这条证明是否完整生成」的信息将丢失；改名后两个阶段的完成状态并存于同一条记录，下游可分别追溯。

**练习 2**：`messages` 被覆盖后，还能从这条验证输入记录里还原出生成阶段的提示词吗？
**答案**：不能直接还原（旧 `messages` 已被覆盖），但生成**结果**完整保留在 `prover_output`（含思维链的原文），且题面 `question` 仍在，生成提示词可由模板加 `question` 重新渲染出来——信息没有真丢。

**练习 3**：L108-L110 要弹掉的四个键中，哪个实际上早就被移除了？哪两个在当前 `generate.py` 输出中根本不存在？
**答案**：`finish_reason` 在 L71 已被 `pop` 改名，此处是空操作；`finished` 与 `input` 不在当前 `generate.py` 的输出字段中（它只写 `output` 与 `finish_reason`），属于对其它推理引擎输出风格的防御性兼容。

## 5. 综合实践

**任务：编写 `build_verification_input.py`，离线复刻 `prepare_proof_verification`。**
不调用任何 API，手工构造一份假想的 `proof_gen_R1/output.jsonl`，用 `utils.py` 的真实解析函数跑完整条链路，把对过滤与打分规则的理解「跑出来」。

**操作步骤**：

1. `pip install regex`，新建工作目录并创建模拟输入 `mock_proof_gen_R1_output.jsonl`（每行一条 JSON）：

   ```
   {"question": "证明 sqrt(2) 是无理数。", "problem_idx": "MOCK-1", "source_name": "mytest", "finish_reason": "stop", "output": "先假设它是有理数……</think>\n## Solution\n反设 sqrt(2)=p/q 且 gcd(p,q)=1，则 p^2=2q^2，故 p 为偶数，设 p=2r，得 q^2=2r^2，q 亦偶数，与 gcd=1 矛盾。\n## Self Evaluation\n反证法结构完整，无跳步。\\boxed{1}"}
   {"question": "求所有正整数 n 使得……", "problem_idx": "MOCK-2", "source_name": "mytest", "finish_reason": "length", "output": "我们考虑……</think>\n## Solution\n设 n 的质因数分解为"}
   {"question": "证明三角不等式。", "problem_idx": "MOCK-3", "source_name": "mytest", "finish_reason": "stop", "output": "从度量空间定义出发……</think>\n## Solution\n由 d(x,z)<=d(x,y)+d(y,z) 两边对 y 取下确界。\n## Self Evaluation\n思路正确但取下确界一步略快，未给满分。"}
   ```

   三条记录分别对应：正常样本、被截断样本、结构完好但自评缺 `\boxed{}` 的样本。

2. 编写 `build_verification_input.py`（示例代码，逐行仿照 main.py L66-L116）：

   ```python
   import json, sys
   sys.path.insert(0, "路径/到/DeepSeek-Math-V2/inference")
   from utils import read_data, extract_solution, extract_self_eval, extract_boxed_answers
   from math_templates import math_templates

   PROOF_GEN_WITH_SELF_EVAL = True   # 对应 args.proof_gen_template in ['proof_generation']

   def prepare_proof_verification(path, tar_path):
       items = read_data(path)
       data = []
       for item in items:
           item['proof_finish_reason'] = item.pop('finish_reason').lower()
           statement, prover_output = item['question'].strip(), item['output'].strip()
           if item['proof_finish_reason'] != 'stop':
               print(f"[丢弃] {item['problem_idx']}: finish_reason != stop"); continue
           assert '</think>' in prover_output, item['problem_idx']
           proof = prover_output.split("</think>")[-1].strip()
           item['prover_output'] = prover_output
           self_eval, self_eval_score = 'null', 0
           if PROOF_GEN_WITH_SELF_EVAL:
               try:
                   self_eval = extract_self_eval(proof).strip()
                   proof = extract_solution(proof).strip()
                   try:
                       self_eval_score = float(
                           [s.strip() for s in extract_boxed_answers(self_eval) if s.strip()][-1])
                   except Exception:
                       self_eval_score = 0
               except Exception:
                   print(f"[丢弃] {item['problem_idx']}: 小节缺失"); continue
               item['self_eval'], item['self_eval_score'] = self_eval, self_eval_score
           item['proof'] = proof
           item['messages'] = [{'role': 'user', 'content':
               math_templates['proof_verification'].format(
                   statement=statement.strip(), proof=proof.strip())}]
           for key in ['finished', 'finish_reason', 'input', 'output']:
               if key in item: item.pop(key)
           data.append(item)
       with open(tar_path, "w") as f:
           for item in data:
               print(json.dumps(item), file=f, flush=True)
       return len(data)

   if __name__ == '__main__':
       n = prepare_proof_verification("mock_proof_gen_R1_output.jsonl",
                                      "mock_proof_verification_R1_input.jsonl")
       print(f"存活 {n} 条")
   ```

   （与源码的差异仅是加了 `[丢弃]` 日志，便于观察闸门行为。）

3. 运行 `python build_verification_input.py`，然后检查产物：
   `wc -l mock_proof_verification_R1_input.jsonl`，并逐条查看字段。

**需要观察的现象与预期结果**（按源码推演，具体输出待本地验证）：

- **MOCK-2 是唯一被丢弃的记录**：`finish_reason == 'length'` 命中 L78 的 `continue`，打印 `[丢弃] MOCK-2`。
- **MOCK-1 与 MOCK-3 都存活**——注意这里会纠正一个直觉：直觉上「自评缺 `\boxed{}`」似乎也该被丢，实际它只触发**内层** `except`，记 `self_eval_score = 0` 后保留（4.2 节讲的两层异常分工）。
- MOCK-1 的 `self_eval_score == 1.0`（`Self Evaluation` 里 `\boxed{1}` 恰为其唯一 boxed 值）；MOCK-3 为 `0`。
- 两条存活记录的 `messages[0]['content']` 均以 `## Instruction` 开头，内含 `## Problem`（题面）与 `## Solution`（纯解）两节，且**不含** `Self Evaluation` 内容。
- 存活记录的字段集合里没有 `output`/`finish_reason`，但有 `proof_finish_reason == 'stop'`、`prover_output`（含思维链）、`proof`（纯解）、`self_eval`、`self_eval_score`、`problem_idx`、`source_name`。
- 若把 MOCK-1 的 `output` 中 `\n## Self Evaluation\n...` 整段删掉再跑，MOCK-1 将因 `IndexError` 走 `[丢弃]` 分支——对照验证两层 `except` 的边界。

**思考延伸**：把 `--n_verification_per_proof` 设为 4，这份 2 行的验证输入将产出 8 条评分（u2-l2 的计数公式）；下一讲的元验证正是从这 8 条评分里挑出低分评价送复核。

## 6. 本讲小结

- **过滤闸门的两重标准**：`finish_reason != 'stop'` → `continue` 静默丢样本；`stop` 却无 `</think>` → `assert` 当场崩溃。前者是正常采样现象，后者被视为上游违约。
- **两层异常的分工**：`Solution`/`Self Evaluation` 小节缺失（结构坏）→ 外层 `except` → 样本丢弃；自评缺 `\boxed{}` 或值不能转 `float`（内容坏）→ 内层 `except` → 记 `self_eval_score = 0`、样本保留。
- **自评分解析链**：`extract_self_eval` 切出自评 → `extract_boxed_answers` 深度计数提取全部 `\boxed{}` → 过滤空串取 `[-1]`（最后一个）→ `float()`。验证器只看 `extract_solution` 切出的纯解，看不到生成器的自评。
- **记录重装**：`proof_verification` 模板把题面与纯解渲染进新 `messages`，覆盖旧生成提示词；`finish_reason` 改名 `proof_finish_reason` 为验证器的新字段让位；`output` 等生成残留被弹出，`prover_output`/`proof`/`self_eval*` 换名随行。
- **经济账**：设通过闸门 \( s \) 条证明，验证调用数为 \( s \times n_{\text{verification\_per\_proof}} \)（run.sh 配置下 \( n = 64 \)），闸门与两层过滤直接决定验证算力的花费规模。

## 7. 下一步学习建议

下一讲 **u4-l3（元验证准备：prepare_meta_verification 与低分评价复核）**顺流而下：验证输出里每条评分同样是「`stop` 且含 `</think>`」才算数，同样用 `extract_boxed_answers` 取最后一个 boxed 值转分数，然后只把**评分不高于 0.75**（即 0 与 0.5 档）的评价连同证明、评价原文一起送元验证复核。你会发现 `prepare_meta_verification`（[inference/main.py:L118-L154](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L118-L154)）几乎是本讲函数的姊妹篇，带着 4.1 与 4.2 的结论去读会非常轻松。之后再进入 u5 单元的证明池与聚合——本讲产出的 `self_eval_score` 与验证评分将在那里汇合成证明排序的双键。
