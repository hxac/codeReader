# u6-l2 二次开发实践：适配新基准与工程加固

## 1. 本讲目标

这是本学习手册的最后一讲，也是唯一一讲「动手改代码」的讲义。前面十一讲我们一直在读代码，这一讲要把读出来的理解变成改造能力。学完本讲，你应该能够：

1. 修复发布代码的启动崩溃：为 `main.py` 补齐 `proof_gen_url`、`proof_rate_url`、`infer_script` 三个被使用却未注册的 argparse 参数。
2. 说清楚 API 配置的「唯一真实入口」在哪里（`generate.py` 的 `APIModel.__init__`），并把它改成环境变量驱动的安全写法。
3. 把一套自定义数据集（新基准、新竞赛、课堂习题）正确接入流水线：知道 `source_name` 如何注入、`problem_idx` 唯一性约束在哪里被断言、证明池目录如何随之布局。
4. 给流水线加上三道工程保险：`--dry_run` 干跑开关、`os.system` 返回值检查、`--max_requests` 请求上限，避免「全量跑飞」产生高额 API 账单。

本讲的所有改动都发生在仓库副本 `inference_mini/` 上，**不触碰原始源码**——这本身也是二次开发的正确姿势：先复制，再改造，原始目录永远是可对照的基准。

## 2. 前置知识

本讲默认你已完成 u1–u6 全部前序讲义，尤其是 u1-l3（参数总览）、u2-l1/u2-l2（generate.py）、u5-l1（证明池）与 u6-l1（算力核算）。这里集中回顾几个本讲反复用到的概念：

- **parse_known_args 的宽容解析**：`parser.parse_known_args()` 返回 `(已识别的 Namespace, 未识别的参数列表)`，遇到不认识的命令行参数不会报错，而是静默收进第二个返回值。它让「拼错参数名」「传了接收方没有的参数」都不报错——是本讲要修补的第一类毛刺的根源。
- **Namespace 属性访问**：argparse 把每个 `--flag` 存为 `args.flag` 属性；访问一个从未 `add_argument` 注册过的属性会抛 `AttributeError`。
- **os.system 与 subprocess**：`os.system(cmd)` 把字符串交给 shell 执行，返回值是平台的等待状态（POSIX 下 0 表示成功），但本仓库的三处调用都不检查返回值；`subprocess.run(cmd, shell=True)` 返回 `CompletedProcess`，带 `returncode` 字段，是更现代的替代。
- **环境变量**：进程级配置的惯用手段（如 `OPENAI_API_KEY`）。相比硬编码，环境变量不会随代码提交泄漏到 git 历史里。
- **.meta 断点档案与 at-least-once**：generate.py 用 `{output}.meta` 这个 pickle 文件记录 `n`、`batch_size` 与 `complete_batches` 集合，已完成批次在重启后跳过。批次是幂等的最小单位——这是本讲 `--max_requests` 闸门选址的依据（详见 u2-l2）。
- **请求量核算公式**（来自 u6-l1）：总请求数上界

  \[ N_{req} \le P \times R \times n_{parallel} \times (1 + n_{ver}) \]

  其中 \(P\) 为题数、\(R\) 为轮数、\(n_{parallel}\) 为每题并行采样数、\(n_{ver}\) 为每证明验证次数。本讲设计调试参数时会反复用到它。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `inference/main.py` | 轮次编排器，自身不发请求 | argparse 段（L19–L55）、未注册属性的使用点（L61–L62、L450）、R1 输入加载段（L402–L428）、三处 `os.system`（L462、L492、L523） |
| `inference/generate.py` | 唯一真正发 HTTP 请求的引擎 | `APIModel.__init__` 硬编码（L16–L21）、`reasoning_content` 依赖（L29）、批处理主循环（L139–L157）与 `.meta` 收尾（L165–L179） |
| `inference/run.sh` | 官方启动脚本 | `set -f`、竞赛级参数配置（L1–L20） |
| `inference/utils.py` | 解析工具 | `hash_problem_idx`（缺 `problem_idx` 时的主键兜底） |
| `inputs/*.json` | 数据格式参照 | 自建数据集要模仿的字段结构 |

建议把这三个 inference 文件在编辑器里并排打开，本讲会在这三个文件之间来回跳。

## 4. 核心概念与源码讲解

### 4.1 缺失的 argparse 参数：修复启动崩溃

#### 4.1.1 概念说明

argparse 的契约是「先注册，后使用」：只有 `parser.add_argument("--x")` 注册过的参数才会成为 `args.x` 属性。发布版 `main.py` 违反了这条契约——它**使用**了三个从未**注册**的属性。结果按官方 README 的指引直接运行，程序会在进入主循环之前就崩溃。

这是发布代码常见的「作者本机有、仓库里没有」问题：作者的本机版本可能注册过这些参数，开源时做了删减但漏掉了使用点。修复方式极其简单——补三行 `add_argument`——但搞清楚**为什么是这三个、各自去向何处**，才算真正理解了流水线的调用结构。

#### 4.1.2 核心流程

先把「注册」与「使用」两侧对照一遍：

```text
注册侧（main.py L20–L53，共 26 个参数）:
  input_paths / output_dirname / proof_pool_dirname
  batch_size
  proof_gen_*（num_processes / temp / max_len / template）
  proof_refine_template / n_best_proofs_to_sample / n_proofs_to_refine
  n_agg_trials / n_parallel_proof_gen
  proof_verification_*（num_processes / temp / max_len / template）
  n_verification_per_proof
  skip_meta_verification / meta_verification_* / n_meta_verification_per_rating
  start_round / max_rounds

使用侧（未注册的三个属性）:
  args.proof_gen_url   → L61 赋值给 proof_gen_url → L453 拼进生成命令的 --api_url
  args.proof_rate_url  → L62 赋值给 proof_rate_url → 此后再无任何引用（死变量）
  args.infer_script    → L450 拼出生成命令的解释器目标 "{infer_script}.py"
```

执行时的崩溃顺序：

1. `parse_known_args()` 解析命令行成功（未注册属性不在命令行上，不报错）。
2. 模块顶层执行到 L61 `proof_gen_url = args.proof_gen_url` → 抛 `AttributeError`。
3. L450 的 `args.infer_script` 排在其后，根本没有机会执行——所以**只修 L61/L62 不修 L450，崩溃点会后移而不是消失**。

另一个容易忽略的事实：`python main.py --help` 是能正常退出的。因为 argparse 处理 `-h/--help` 时直接打印帮助并抛 `SystemExit`，发生在返回 Namespace 之前，L61 根本不会执行。「--help 能跑」不等于「程序能跑」。

#### 4.1.3 源码精读

未注册属性的第一处使用点——模块顶层、主循环之前：

[inference/main.py:L55-L64](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L55-L64)

这六行做了四件事：`parse_known_args()` 宽容解析（L55）；取出三个目录参数（L57–L59）；读取 `args.proof_gen_url` 与 `args.proof_rate_url`（L61–L62）——这两个属性从未注册，`AttributeError` 在 L61 爆发；L64 根据 `proof_gen_template` 是否为 `proof_generation` 决定是否启用自评解析。

第三处使用点藏在生成命令的 f-string 里：

[inference/main.py:L448-L462](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L448-L462)

L448 计算本轮采样数（R1 用 `n_parallel_proof_gen`，之后各轮用它与 `n_agg_trials` 的整除商）；L450 用 `{args.infer_script}.py` 拼出要执行的脚本名——注意 `.py` 后缀是命令里现拼的，所以参数值应当传 `generate` 而不是 `generate.py`，否则会变成 `generate.py.py`；L453 把 `proof_gen_url` 填进 `--api_url`；L461–L462 打印并交给 shell 执行。

而 `proof_rate_url` 的「死变量」身份可以直接验证——整个 main.py 中它只出现在 L62，验证与元验证两段命令（L480–L490、L511–L521）既不含 `--api_url`，也不引用它。也就是说：**即便补注册了 `proof_rate_url`，验证阶段的流量也不会走它**（详见 4.2.1 的完整链路分析）。

对照官方启动脚本，确认它同样没有传这些参数：

[inference/run.sh:L9-L20](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh#L9-L20)

run.sh 只传了 11 个参数，没有任何 URL 或 infer_script——所以注册时给它们设置合理的默认值，就能让 run.sh 不做任何修改直接跑通解析阶段。

修复补丁（示例代码，加在 `inference/main.py` 的 L53 `--max_rounds` 之后）：

```python
parser.add_argument("--infer_script", type=str, default="generate",
                    help="generation engine script name WITHOUT .py suffix")
parser.add_argument("--proof_gen_url", type=str, default="",
                    help="API base URL for proof generation (currently ignored by generate.py)")
parser.add_argument("--proof_rate_url", type=str, default="",
                    help="reserved for verification API URL (dead code in v665c840)")
```

#### 4.1.4 代码实践

**实践目标**：亲眼确认崩溃存在，再用三行补丁消除它。

**操作步骤**：

1. 进入 `inference/` 目录，运行（**本讲所有命令均为待本地验证**，以下给出的是按代码逻辑推断的预期现象）：

   ```bash
   python main.py --input_paths ../inputs/IMO2025.json \
                  --output_dirname /tmp/dmv2_out \
                  --proof_pool_dirname /tmp/dmv2_pool
   ```

2. 复制副本准备改造：`cp -r inference inference_mini`（与原目录平级）。
3. 在 `inference_mini/main.py` 的 argparse 段末尾加上面三行补丁。
4. 再跑一次步骤 1 的命令（换成 `inference_mini` 路径），并在看到打印出的生成命令后立刻 `Ctrl-C`。

**需要观察的现象**：

- 步骤 1：程序在打印任何 `Proof Verification >>>` 之前就退出，回溯信息指向 L61，异常类型为 `AttributeError: 'Namespace' object has no attribute 'proof_gen_url'`；`/tmp/dmv2_out` 目录不会被创建（崩溃发生在 L398 主循环之前）。
- 步骤 4：解析通过，屏幕打印出完整的 `python generate.py --input_data_path ... --n 128` 多行命令——此时它尚未真正执行（打印在 `os.system` 之前），这正是安全中断的窗口。

**预期结果**：修复前必崩于 L61；修复后可走到命令打印。注意步骤 4 若不中断，`os.system` 会真的启动 generate.py 并用硬编码的 `base_url="yyy"` 发起注定失败的请求，所以务必在打印后中断，或直接使用 4.4 的 `--dry_run`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `python main.py --help` 不会触发 `AttributeError`？

**答案**：argparse 在解析 `-h/--help` 时打印帮助并抛出 `SystemExit`，程序在 `parse_known_args()` 返回之前就结束了，模块顶层 L61 的属性访问永远不会执行。帮助文本能正常显示，恰恰掩盖了参数缺失的问题。

**练习 2**：如果用户把 `--infer_script` 传成 `generate.py`，会发生什么？

**答案**：L450 的 f-string 会拼出 `python generate.py.py ...`，shell 找不到 `generate.py.py` 文件，`os.system` 返回非零。但由于返回值不被检查（见 4.4），main.py 会若无其事地继续走验证准备，然后在读取不存在的 `output.jsonl` 时以 `FileNotFoundError` 崩溃——错误现场距离真正的病因隔了一整个阶段。

**练习 3**：补注册 `--proof_rate_url` 之后，验证阶段的请求会走这个 URL 吗？

**答案**：不会。`proof_rate_url` 在 L62 赋值后于全文件再无引用；验证命令（L480–L490）根本没有 `--api_url` 这一项；而且即便加了，generate.py 的 argparse（L84–L92）也没有 `--api_url`，`parse_known_args` 会把它静默丢弃，`APIModel` 用的仍是硬编码配置。要真正分流验证流量，需要同时改三处：验证命令加 `--api_url`、generate.py 注册该参数并传入 `APIModel`、`APIModel.__init__` 接受它。

### 4.2 APIModel.\_\_init\_\_：唯一真实生效的 API 配置入口

#### 4.2.1 概念说明

先把整条配置链路画清楚，这是理解「改哪里才有用」的关键：

```text
run.sh ──(无 URL 参数)──▶ main.py
main.py L453: --api_url {proof_gen_url} ──▶ 拼进生成命令
generate.py L93: parse_known_args() ──▶ --api_url 未注册，静默丢弃
generate.py L16-L21: APIModel() ──▶ AsyncOpenAI(api_key="xxx", base_url="yyy")  ← 唯一生效点
```

结论很反直觉：**命令行上的 URL 参数从头到尾没有生效过**。无论你在 run.sh 里传什么 `--api_url`，真正决定请求发往哪里、用什么密钥的，只有 `APIModel.__init__` 里那两个硬编码字面量 `"xxx"` 与 `"yyy"`。所以「配置 API Key」的实际操作是改源码——官方 inference/README 也是这么说的（"You should first specify your api key in the `generate.py` file"）。

硬编码的问题有三层：密钥会随代码提交泄漏；切换服务商要改源码；生成与验证无法指向不同端点（比如生成用大模型、验证用小模型以省成本——论文里生成器与验证器本就是两个角色）。

#### 4.2.2 核心流程

把配置改造成环境变量驱动的方案：

```text
启动前:  export DMV2_API_KEY=sk-...   export DMV2_BASE_URL=https://.../v1
fork 时: 子进程继承父进程环境变量（multiprocessing 默认 fork 语义）
运行时:  mp_generate_loop → APIModel() → __init__ 读 os.environ
```

选址上有一个不能违反的约束：`APIModel` 的实例化发生在**每个 worker 进程内部**（u2-l2 讲过原因——`AsyncOpenAI` 背后的连接池持有进程内资源，不能跨进程共享一个实例）。所以配置必须在 `__init__` 内读取，或者作为 `Process(target=..., args=...)` 的参数传进子进程；在主进程改一个全局变量对子进程无效（fork 之后各写各的内存）。

#### 4.2.3 源码精读

唯一真实生效的配置点：

[inference/generate.py:L15-L21](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L15-L21)

`APIModel.__init__` 构造 `AsyncOpenAI` 客户端：`api_key="xxx"` 与 `base_url="yyy"` 是占位符，用户必须手动替换；`timeout=300000` 的单位是**秒**（约 83 小时），等同于禁用超时——对超长证明生成是有意为之，但也意味着故障请求会挂到天荒地老。

子进程内的实例化位置：

[inference/generate.py:L78-L81](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L78-L81)

`mp_generate_loop` 是每个 `Process` 的入口：先 `sleep(5)` 错峰启动，再各自 `APIModel()`——这就是为什么配置必须写进 `__init__` 能触及的地方。

两个与服务商兼容性相关的隐藏依赖：

[inference/generate.py:L23-L35](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L23-L35)

L29 直接访问 `message.reasoning_content`——这是 DeepSeek 风格部署的扩展字段，**官方 OpenAI API 的消息对象没有这个属性**，换服务商时会抛 `AttributeError`；L33 的拼接逻辑（content 非空时丢弃开头的 `<think>` 标签）在 u2-l1 已精读。另外 L116–L121 构造的 `sampling_params` 里有一个非标准键 `max_total_tokens`，能否被服务端接受因部署而异（待本地验证），不适配时需要删除。

环境变量化补丁（示例代码）：

```python
class APIModel:
    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=os.environ.get("DMV2_API_KEY", "xxx"),
            timeout=300000,
            base_url=os.environ.get("DMV2_BASE_URL", "yyy")
        )
```

`generate.py` 首行已 `import os`，无需新增导入。保留 `"xxx"`/`"yyy"` 作为默认值，可以让「未设置环境变量」的失败模式与原版一致，方便对照。

#### 4.2.4 代码实践

**实践目标**：把密钥与端点移出源码，并验证「生成/验证分流」需要动哪几处。

**操作步骤**：

1. 在 `inference_mini/generate.py` 中应用上面的环境变量补丁。
2. 写一个 `probe_env.py`（示例代码，放在 `inference_mini/` 内）验证环境变量能穿透到子进程：

   ```python
   import os
   from multiprocessing import Process

   def child():
       print("child sees:", os.environ.get("DMV2_BASE_URL"))

   if __name__ == "__main__":
       p = Process(target=child); p.start(); p.join()
   ```

3. （选做，需要真实 API）`export` 两个变量后用 4.4 的 dry_run 之外的微型配置跑 1 道题。

**需要观察的现象**：步骤 2 中子进程打印出你在 shell 里 export 的值，证明 fork 继承成立；不设置变量时打印 `None`（回落到默认占位符逻辑）。

**预期结果**：`inference_mini` 中不再出现任何真实密钥；`git diff inference/generate.py inference_mini/generate.py` 只显示 `__init__` 三行的变化。步骤 3 的真实请求行为待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么不能在主进程创建一个 `APIModel` 然后传给所有 worker 共享？

**答案**：`AsyncOpenAI` 底层的 HTTP 连接池持有套接字与事件循环句柄，这些都是进程私有的资源；`multiprocessing` fork 出的子进程各自复制一份内存，共享的连接句柄在两个进程里同时使用会导致连接状态错乱。源码的选择是每个 worker 在 `mp_generate_loop` 里各自实例化（L79）。

**练习 2**：`timeout=300000` 如果单位被误读成毫秒，实际效果差多少？

**答案**：SDK 的单位是秒，300000 秒 ≈ 83.3 小时，基本等于禁用超时；若误以为是毫秒（即 300 秒 = 5 分钟），会误判「已经有 5 分钟超时保护」。对最长 128K token 的证明生成任务，5 分钟超时可能切断本来能完成的请求，造成大量 `finish_reason=length` 假象。

**练习 3**：想让「生成走 A 端点、验证走 B 端点」，最少要改几处代码？

**答案**：四处。① `main.py` 验证命令（L480–L490）补 `--api_url {proof_rate_url}`；② `generate.py` argparse 注册 `--api_url`；③ `mp_generate_loop`/`Process(args=...)` 把它传进子进程；④ `APIModel.__init__` 增加参数并填入 `AsyncOpenAI`。顺带要把元验证命令（L511–L521）也纳入考虑——它同样硬编码走默认端点。

### 4.3 适配新数据源：source_name 注入与 problem_idx 唯一性

#### 4.3.1 概念说明

接入一套新基准（比如你自己整理的习题集）看起来只是「准备一个 json」，但 R1 输入加载段对数据有三条隐性契约，违反任何一条都会在**离数据加载很远的地方**炸出错误：

1. **每条记录必须有 `question` 字段**——L418 直接 `item['question'].strip()`，缺失即 `KeyError`。
2. **`source_name` 由文件路径推导，不由数据决定**——它决定证明池的一级子目录，同名会混池。
3. **同一 source 内 `problem_idx` 必须唯一**——缺失时用题面 SHA-256 兜底；显式提供但重复时，断言在 R2 的 `prepare_proof_refinement` 里才触发。

第三条尤其值得强调：R1 阶段对重复的 `problem_idx` 完全无感，会正常花费一整轮的生成与验证请求，直到第二轮做数据汇合时才 `AssertionError` 崩溃——**钱已经花了**。这是「先用 dry_run 与小配置验证数据合法性」的又一理由。

#### 4.3.2 核心流程

R1 输入加载的完整流程：

```text
input_paths 按逗号切分（意味着路径本身不能含逗号）
  └─ 对每个 input_path:
       source_name = 路径最后一段去掉第一个点之后的内容
       后缀 .json → json.load 整体读；否则 → 逐行 json.loads（jsonl 语义）
       给每条记录注入 item['source_name'] = source_name
  └─ 汇总 raw_data
  └─ 每条记录用 proof_generation 模板渲染 messages
  └─ 写入 {output_dirname}/proof_gen_R1/input.jsonl
```

`source_name` 与 `problem_idx` 共同决定证明池路径：

```text
{proof_pool_dirname}/{source_name}/{problem_idx}.jsonl
```

#### 4.3.3 源码精读

输入加载与注入逻辑：

[inference/main.py:L402-L415](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L402-L415)

L404 按逗号切分多个输入文件；L405 是 `source_name` 的推导式 `input_path.split("/")[-1].split(".")[0]`——取路径最后一段、再取第一个点之前的部分；L406–L412 按 `.json` 后缀分流（注意判断的是小写字面量，其余后缀一律按逐行 jsonl 处理）；L413–L414 把 `source_name` 写进每条记录，随数据流贯穿后续所有阶段。

`source_name` 的消费点在聚合任务里：

[inference/main.py:L170-L179](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L170-L179)

L170 用 `item.get('source_name', 'temp_source_name')` 兜底；L171–L174 取 `problem_idx`，缺失时调用 `hash_problem_idx`（题面去空白后的 SHA-256，见 [inference/utils.py:L5-L6](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/utils.py#L5-L6)）；L179 拼出证明池的一题一文件路径。

唯一性断言的真正位置——第二轮的数据汇合阶段：

[inference/main.py:L359-L364](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L359-L364)

L363 `assert problem_idx not in problem_idx_dedup` 是唯一的重复检查点，此时 R1 的生成与验证请求早已发完。还要注意一个更隐蔽的陷阱：`problem2proof2ratings` 等嵌套字典以**题面文本**为外键（L322），两道题面完全相同的题会在字典层面静默合并、连断言都不触发，但它们的证明会被算作同一题的候选——自建数据集应保证题面互不相同。

顺带解释 run.sh 的第一行：

[inference/run.sh:L1-L3](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh#L1-L3)

`set -f` 关闭 shell 的路径名通配扩展，因为 L10 的 `${input_path}` 不带引号展开，若路径含 `*`、`?` 会被 shell 意外展开成文件列表；逗号拼接的多路径写法也意味着**文件名里不能出现逗号**。

#### 4.3.4 代码实践

**实践目标**：构造一份两题的迷你数据集，并验证 `source_name` 推导式的边界行为。

**操作步骤**：

1. 在仓库根目录创建 `mytest.json`（示例代码）：

   ```json
   [
     {
       "problem_idx": "MINI-1",
       "question": "Prove that for any positive integer $n$, the sum $1+2+\\cdots+n$ equals $\\frac{n(n+1)}{2}$.",
       "answer": "null"
     },
     {
       "problem_idx": "MINI-2",
       "question": "Prove that there are infinitely many prime numbers.",
       "answer": "null"
     }
   ]
   ```

2. 写 `probe_source_name.py`（示例代码）复刻 L405 的推导式并测试边界：

   ```python
   for p in ["../inputs/IMO2025.json", "mytest.json",
             "data/my.test.2024.json", "a/IMO2025.json", "b/IMO2025.json"]:
       print(p, "->", p.split("/")[-1].split(".")[0])
   ```

3. 把 `mytest.json` 与 `inference_mini/` 里改好的 main.py 组合，用 4.4 的 dry_run 跑 R1，检查 `proof_gen_R1/input.jsonl` 的每行都带 `"source_name": "mytest"`。

**需要观察的现象**：步骤 2 中 `data/my.test.2024.json` 推导出 `my`（第一个点截断）；`a/IMO2025.json` 与 `b/IMO2025.json` 推导出相同的 `IMO2025`（跨目录撞名 → 共用同一个证明池子目录）；步骤 3 的 input.jsonl 共 2 行、每行 `messages[0].content` 已是渲染后的完整证明生成提示词。

**预期结果**：迷你数据集可走通 R1 准备；推导式边界行为与上述推断一致。若你的数据集文件名带多个点，应重命名（如 `my.test.2024.json` → `my_test_2024.json`），或修改 L405 用 `os.path.splitext` 取主干（更稳健的写法：`os.path.splitext(os.path.basename(input_path))[0]`）。

#### 4.3.5 小练习与答案

**练习 1**：两道题都没有 `problem_idx` 字段、题面一字不差，流程会怎样？两道题都有 `problem_idx` 但值相同呢？

**答案**：前者：题面相同 → SHA-256 相同 → 在嵌套字典层面被合并成一道题，静默通过，两题的证明混入同一候选池；后者：字典外键是题面文本，两道题分别建条目，然后在 L363 的 `problem_idx` 去重断言处崩溃——而且是在 R1 全部请求发完之后。

**练习 2**：换一个新的 `output_dirname` 但沿用同一个 `proof_pool_dirname` 重跑，是一次干净的全新实验吗？

**答案**：不是。R≥2 的 `prepare_proof_refinement` 以 `use_old_proofs_for_refinement=True` 调用（L438），旧证明池里的历史证明会被并入候选；若旧池中已有 `meanscore > 0.99999` 的证明，该题在 L222 的早停判断处直接跳过，连生成请求都不发。想要全新实验必须同时清空证明池目录。

**练习 3**：为什么说「`id` 字段可以偷懒不填，`problem_idx` 不行」？

**答案**：`id` 只在输入文件内部使用、流水线后续不再引用它（u1-l2 讲过它仅文件内唯一）；`problem_idx` 则是证明池文件名的一部分、跨轮谱系追踪 `dep_proof_ids` 的锚点、以及 L363 断言的去重键——缺失时退化为题面哈希尚可工作，重复则直接崩溃。

### 4.4 os.system 调用点加固：dry_run、返回值检查与 --max_requests 请求闸门

#### 4.4.1 概念说明

`main.py` 把三个阶段的执行整体外包给 shell，形成了三个同构的调用点。这个设计有三个已知脆弱性：

1. **返回值不检查**：`os.system` 失败（比如 4.1 练习 2 的 `generate.py.py`）后流水线照样推进，直到某个下游函数读不到文件才以 `FileNotFoundError` 崩溃，错误现场与病因相隔一个阶段。
2. **f-string 拼接不做 shell 转义**：命令由 L449–L460 的多行 f-string 拼出，路径含空格或特殊字符时会被 shell 拆碎（当前部署路径恰好都安全，属于「碰巧能用」）。
3. **没有干跑与限额**：任何一次真实运行都从第一批请求开始烧钱，u6-l1 核算过竞赛配置的上界约 240 万请求、验证占 98.5%——手滑一次的代价是真实的。

对应的三道保险：`--dry_run`（只打印不执行）、返回值检查（失败即停）、`--max_requests`（generate.py 在提交批次前主动止步，`.meta` 断点天然保留续跑能力）。

其中 dry_run 有一个**必须预先想清楚的语义陷阱**：流水线的阶段是靠中间文件串联的，R1 不真跑生成，`proof_gen_R1/output.jsonl` 就不存在，紧接着的 `prepare_proof_verification` 会在 L68 的 `read_data` 处 `FileNotFoundError`。所以 dry_run 不能只是「把 `os.system` 换成 print」，还必须用 `continue` 跳过本阶段后续，并在下一轮入口补一个「上游输出缺失即 break」的守卫——否则干跑自己会在第二轮崩溃。

#### 4.4.2 核心流程

加固后的单轮逻辑（伪代码）：

```text
for R in range(start_round, max_rounds + 2):
    if proof_gen_R{R}/input.jsonl 不存在:
        if R == 1: 加载原始输入、注入 source_name、渲染模板、写 input.jsonl
        else:
            if dry_run 或上游 proof_verification_R{R-1}/output.jsonl 缺失: break   # 新增守卫
            prepare_proof_refinement(...)   # 刷新证明池
            if R == max_rounds + 1: break    # 收尾轮
    print(proof_gen_cmd)
    if dry_run: continue                     # 新增：跳过本轮后续阶段
    if run_cmd(proof_gen_cmd) != 0: 报错并退出   # 新增：返回值检查
    ... 验证、元验证两个阶段同理用 run_cmd 包裹 ...
```

`--max_requests` 闸门在 generate.py 侧，选址在「投递批次之前」：

```text
读入一行 → 复制 n 份 → 攒满一个 batch
  → batch_idx 已在 complete_batches?  是 → 跳过（原有断点续跑逻辑）
  → 新增: num_input + 本批大小 > max_requests?  是 → 置 stop_submit 并停止投递
  → 否则 input_queue.put(batch)
停止投递后照常: 发哨兵 → 收割输出 → 更新 .meta → 正常退出
```

为什么检查点放在投递前而不是收割循环里？因为 `.meta` 的幂等单位是**批次**：一个批次要么完整落盘并记入 `complete_batches`，要么根本没提交。在批次边界止步，已在途的批次仍会被收割循环正常记录，断点完好；下次提高限额重启，已完成批次自动跳过。这正是 u2-l2 分析过的 at-least-once 协议的直接应用。

#### 4.4.3 源码精读

三个同构的外包调用点——证明生成（用可配置的 `infer_script` 与 `--api_url`）：

[inference/main.py:L448-L462](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L448-L462)

L449–L460 的多行 f-string 拼命令、`.strip()` 去掉首尾换行，L461 打印，L462 裸调 `os.system`——返回值被丢弃。

证明验证（硬编码 `generate.py`、不传 URL）：

[inference/main.py:L480-L492](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L480-L492)

结构同上，`--n {n_verification_per_proof}` 控制每证明验证次数（u6-l1 的算力大户，竞赛配置为 64）。

元验证（受 `--skip_meta_verification` 开关保护，官方配置默认跳过）：

[inference/main.py:L504-L523](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L504-L523)

L504 的开关是现成的成本控制手段：关掉元验证可以省掉「低分评价数 × n_meta_verification_per_rating」那一整块请求。

generate.py 侧的投递循环——`--max_requests` 的插入位置：

[inference/generate.py:L139-L157](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L139-L157)

L141–L142 把每行复制 `n` 份（数据层多次采样）；L144 检查 `complete_batches` 实现断点跳过；L146 是投递动作——闸门就加在 L144 与 L146 之间；L151–L156 处理不足一批的尾部，同样要受闸门约束。

收割循环（不能动的部分）：

[inference/generate.py:L160-L179](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L160-L179)

L160–L161 发哨兵让 worker 退出；L167–L174 收结果并逐条落盘；L175–L177 每收完一批就更新 `.meta`。停止投递后这段原样运行，即可保证在途批次被完整记账。

两份补丁（示例代码）。main.py 侧——统一出口加 dry_run：

```python
def run_cmd(cmd):
    if args.dry_run:
        print(f"[dry-run] skip: {cmd}", flush=True)
        return 0
    return os.system(cmd)
```

三处 `os.system(...)` 改为 `run_cmd(...)`，并在生成命令之后加 `if args.dry_run: continue`；同时在 R≥2 分支入口加守卫：

```python
previous_proof_verification_output_path = f"{output_dirname}/proof_verification_R{R - 1}/output.jsonl"
if args.dry_run or not os.path.exists(previous_proof_verification_output_path):
    print(f"[dry-run] upstream output missing, stop at round {R}", flush=True)
    break
```

argparse 增加 `parser.add_argument("--dry_run", action="store_true")`。

generate.py 侧——请求闸门：

```python
parser.add_argument("--max_requests", default=None, type=int)   # 加入 argparse

stop_submit = False
for line in tqdm(fr, desc="Waiting Input"):
    if stop_submit:
        break
    item = json.loads(line)
    for i in range(n):
        submit_batch.append(item)
        if len(submit_batch) >= batch_size:
            if batch_idx not in meta_data["complete_batches"]:
                if max_requests is not None and num_input + len(submit_batch) > max_requests:
                    stop_submit = True
                    break
                num_input += batch_size
                input_queue.put((batch_idx, submit_batch))
            else:
                num_skip += batch_size
            batch_idx += 1
            submit_batch = []
```

尾部批次（对应 L151–L156）同样加 `max_requests` 判断；其余代码不动。到达限额时程序打印提示后走正常的哨兵—收割—记账流程退出（如需让调用方感知截断，可在结束时 `sys.exit(2)`，main.py 侧的返回值检查会将其拦下——两道保险在此闭环）。

#### 4.4.4 代码实践

**实践目标**：为 `inference_mini` 装上 dry_run 与返回值检查，先于任何真实请求看到完整命令序列。

**操作步骤**：

1. 按上节补丁修改 `inference_mini/main.py`（`--dry_run` 参数、`run_cmd`、`continue`、R≥2 守卫）。
2. 运行干跑（待本地验证）：

   ```bash
   cd inference_mini
   python main.py --input_paths ../mytest.json \
                  --output_dirname ../mini_out \
                  --proof_pool_dirname ../mini_out/proof_pool \
                  --n_parallel_proof_gen 8 --n_agg_trials 4 \
                  --n_verification_per_proof 2 --max_rounds 1 --dry_run
   ```

3. 对照打印出的命令，逐项检查 `--input_data_path`、`--n 8`（R1 采样数等于 `n_parallel_proof_gen`）、`--num_processes`、`--max_tokens` 是否符合预期。

**需要观察的现象**：

- 屏幕先出现 R1 的生成命令（含 `[dry-run] skip:` 前缀），随后跳过验证与元验证；
- `mini_out/proof_gen_R1/input.jsonl` 被真实写出（2 行、带 `source_name` 与渲染后的 messages）——dry_run 只跳过「执行」，不跳过「本地数据准备」，这恰好让你能检查输入构造是否正确；
- R2 进入收尾轮入口时命中守卫，打印 `upstream output missing` 后干净退出，退出码 0；
- 全程无任何 `output.jsonl` 产生、证明池目录为空、零 API 请求。

**预期结果**：命令序列与 [inference/main.py:L449-L460](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L449-L460) 的模板一致。若你先做「只替换 os.system 不加 continue」的 naive 版本，会在紧随其后的验证准备处看到 `FileNotFoundError`——建议故意试一次，感受阶段间文件依赖的严格性。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `--max_requests` 的检查放在 `input_queue.put` 之前，而不是收割循环里到达限额就 `break`？

**答案**：`.meta` 的记账单位是批次，收割循环每收完一批才更新 `complete_batches`。若在收割中途强行退出，已投递但在途的批次没有记账，重启后会整批重发（幂等尚可保）；但更糟的是落盘到一半的输出行会因 `"a+"` 追加模式产生重复（u2-l2 指出的半批窗口）。在投递前止步则批次边界清晰：在途批次照常收割记账，断点完好，续跑精确衔接。

**练习 2**：`os.system` 与 `subprocess.run(cmd, shell=True)` 在本场景下的关键差异是什么？

**答案**：功能上都把字符串交给 shell，但 `subprocess.run` 返回 `CompletedProcess`（含 `returncode`，还可设 `check=True` 直接抛异常、捕获 stdout/stderr），而裸 `os.system` 的返回值容易被忽略——本仓库三处调用正是这么丢掉的。渐进式加固可以只包一层 `run_cmd` 而不换 API；彻底重构则换 `subprocess.run` 并加 `check=True`。

**练习 3**：dry_run 模式下最多能「预演」到流水线的哪一步？为什么注定无法干跑完整 16 轮？

**答案**：最多到 R1 的输入准备 + 三个阶段的命令拼装检查（且只有生成命令能在 R1 被打印，验证/元验证命令依赖生成输出存在才有输入可准备）。无法继续的原因是数据依赖：每个阶段的 `prepare_*` 都以上一阶段的 `output.jsonl` 为输入，而这些文件只有真实请求才会产生。想进一步预演，可以构造「假输出」文件（手工伪造含 `output`/`finish_reason` 字段的 jsonl）喂给下游 prepare 函数——这正好是 u4-l2/u4-l3 实践里做过的事。

## 5. 综合实践：inference_mini 三项改造一次性完成

把本讲四个模块的成果拼成一条完整的加固流水线。总任务：**在 `inference_mini/` 副本上完成三项改造，并用 2 题数据集走通 dry_run 全流程，最后给出一次预算受控的真实微跑方案**。

### 5.1 准备

```bash
cp -r inference inference_mini     # math_templates.py / utils.py 一并复制，main.py 按模块名导入它们
```

按 4.3.4 创建两题的 `mytest.json`（放在仓库根目录，与 `inference_mini/` 平级）。

### 5.2 三项改造

**改造一（4.1）**：在 `inference_mini/main.py` 的 argparse 段补注册 `--infer_script`（默认 `generate`，不带 `.py`）、`--proof_gen_url`、`--proof_rate_url`。验收：`python main.py --input_paths ... --output_dirname ... --proof_pool_dirname ...` 不再抛 `AttributeError`。

**改造二（4.4）**：增加 `--dry_run` 与 `run_cmd`，三处 `os.system` 全部替换；生成命令后加 `if args.dry_run: continue`；R≥2 分支加上游缺失守卫；`run_cmd` 返回非零时打印错误并 `sys.exit(1)`。验收：干跑全程退出码 0，零请求。

**改造三（4.2 + 4.4）**：`inference_mini/generate.py` 的 `APIModel.__init__` 改为读 `DMV2_API_KEY` / `DMV2_BASE_URL` 环境变量；argparse 增加 `--max_requests`，投递循环加批次边界闸门。验收：人为设 `--max_requests 3`（小于一批）跑一次干跑之外的微型任务时（需真实 API，待本地验证），程序在投递第一批之前止步、`.meta` 正常落盘、再次以更大限额重跑能衔接。

### 5.3 走通 dry_run 全流程

执行 4.4.4 步骤 2 的命令，检查清单：

| 检查项 | 预期 |
| --- | --- |
| 退出码 | 0 |
| `mini_out/proof_gen_R1/input.jsonl` | 存在，2 行，每行含 `source_name: "mytest"`、`problem_idx`、渲染后的 `messages` |
| 打印的生成命令 | `--n 8`（R1 采样数）、`--input_data_path .../proof_gen_R1/input.jsonl` |
| `mini_out/proof_gen_R1/output.jsonl` | 不存在（零请求的直接证据） |
| 证明池 `mini_out/proof_pool/` | 不存在或为空 |
| R2 行为 | 命中守卫打印 `upstream output missing` 后退出 |

### 5.4 预算受控的真实微跑方案（待本地验证）

填好环境变量后，用同一套小参数真实执行（去掉 `--dry_run`）：

```bash
export DMV2_API_KEY=... DMV2_BASE_URL=...
python main.py --input_paths ../mytest.json \
               --output_dirname ../mini_out --proof_pool_dirname ../mini_out/proof_pool \
               --n_parallel_proof_gen 8 --n_agg_trials 4 \
               --n_verification_per_proof 2 --max_rounds 1 --skip_meta_verification
```

用 u6-l1 公式核算：\(N_{req} \le P \times R \times n_{parallel} \times (1 + n_{ver}) = 2 \times 1 \times 8 \times 3 = 48\)，其中生成 16、验证至多 32、元验证因 `--skip_meta_verification` 为 0、R2 是收尾轮只刷新证明池不发请求——总量小于 100，符合调试预算。观察 `mini_out/proof_pool/mytest/MINI-1.jsonl` 是否在 R2 收尾轮被写入（`round_idx: 1`、递增的 `proof_id`、均值分 `meanscore`），即可确认 u5 单元的证明池机制在真实链路上运转。跑完后 `rm -rf mini_out` 即可完全清理（证明池与输出同在 `mini_out` 下，一次删净）。

## 6. 本讲小结

- `main.py` 使用了三个未注册的 argparse 属性：`proof_gen_url`（L61 崩溃点）、`proof_rate_url`（L62 赋值后全文件不再引用的死变量）、`infer_script`（L450 命令拼装）——补三行 `add_argument` 即可修复，且 `--help` 正常不代表程序能跑。
- API 配置的唯一真实生效点是 `generate.py` 的 `APIModel.__init__`（L16–L21）硬编码；命令行 `--api_url` 被 generate.py 的 `parse_known_args` 静默丢弃，安全做法是改为环境变量，且必须在子进程内的 `__init__` 读取（连接池不可跨进程共享）。
- 接入新数据集的三条契约：必须有 `question`；`source_name` 由「路径末段第一个点之前」推导，多点文件名与跨目录同名会混证明池；`problem_idx` 同源重复的断言在 R2 才触发，R1 的请求已经花掉。
- 三处 `os.system` 均不检查返回值、f-string 拼接不做 shell 转义，失败会传导到下一阶段才以 `FileNotFoundError` 暴露。
- dry_run 不能只替换 `os.system`：阶段间以中间文件为数据依赖，必须配合 `continue` 与「上游输出缺失即 break」守卫，否则干跑自身在第二轮崩溃。
- `--max_requests` 闸门应放在 `input_queue.put` 之前的批次边界上，与 `.meta` 的 at-least-once 协议对齐，在途批次照常记账、断点精确续跑。

## 7. 下一步学习建议

本讲是学习手册的最后一讲，手册部分到此完整覆盖了 `inference/` 全部四个 Python 文件。后续的深入方向：

1. **回到论文**：带着代码读 `DeepSeekMath_V2.pdf`，重点对照「生成-验证-元验证-精炼」闭环的训练侧描述与本仓库的推理侧实现，体会「验证器即奖励模型」在测试时的镜像结构。
2. **复核官方输出**：用 u1-l2 的数据技能重新审视 `outputs/*.jsonl`，尝试用本讲的聚合知识解释 `average_automatic_rating` 与 `human_rating` 的分歧记录（如 CMO2024-3）。
3. **对照工业级框架**：把本仓库约 700 行的手写编排与 verl、OpenRLHF、SLM-Lab 等框架的推理/评估模块对比，理解「发布级示例代码」与「生产级框架」在容错、重试、限流、可观测性上的差距——你在本讲手写的 dry_run、限额、返回值检查，正是那些框架里 `Ray`、`tenacity`、`structlog` 承担的工作。
4. **继续一个小项目**：给 `inference_mini` 加上「假输出驱动的全流程干跑」（练习 4.4-3 的延伸），把 u4-l2/u4-l3 手工构造的伪造 jsonl 接成自动化 fixture，你就得到了一套不花一分钱的流水线回归测试。
