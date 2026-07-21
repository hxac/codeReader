# 基准评测推理脚本（VerilogEval 与 RTLLM）

## 1. 本讲目标

学完本讲，读者应该能够：

- 说清 `benchmark_inference/` 目录的**职责边界**：它只负责「让模型把代码写出来」，**不负责打分**，评分交给外部基准仓库。
- 掌握 `test_on_verilog-eval.py` 与 `test_on_rtllm.py` 两个脚本的 `argparse` 参数，并把它们和 README 给出的运行命令一一对应起来。
- 理解 VerilogEval 脚本「描述文件 + 题目文件」按 `task_id` 对齐、`detail_description + prompt` 拼接的流程，以及它用「列表复制」产生 `n` 个候选的方式。
- 看懂 RTLLM 脚本的 `design_list` 关键字匹配如何把每段生成代码落盘成 `{keyword}.v`，并理解这种「子串匹配 + 命中即停」机制的脆弱点。
- 复用 u1-l5 已讲过的输出抽取逻辑，并发现 VerilogEval 脚本里「抽取后的代码 `s` 实际并未写盘」这一源码问题。

## 2. 前置知识

本讲是进阶层最后一篇，默认你已经读过：

- **u1-l3 快速上手推理**：`model.generate` 的采样参数（`temperature` / `top_p` / `max_length`）、`fp16` 半精度加载、`device_map` 选卡。本讲两个脚本就是在批量题目上反复调用这一套。
- **u1-l5 输出后处理与代码抽取**：从 `s_full` 里用 `rsplit('endmodule', 1)` 截断、再用 `rfind('tb_module')` / `find('testbench')` 剔除测试台的整套 Mistral 版抽取流程。两个基准脚本**原样复用**了这段逻辑，本讲只做回访，不再重新推导。

还需要一点领域常识：什么是**基准（benchmark）**。基准 = 一套标准题目 + 一套标准评分脚本，用来横向比较不同模型在同一个任务上的好坏。RTL-Coder 用了两个公开的 RTL 基准：

| 基准 | 来源 | 子集 / 规模 | 题目形式 |
|---|---|---|---|
| **VerilogEval** | NVIDIA NVlabs | `Machine`（自动生成题）与 `Human`（人工题） | 每题一个 `task_id`，配一段自然语言描述 + 一段模块签名提示 |
| **RTLLM** | 港科大自家（hkust-zhiyao/RTLLM） | 28 个真实设计（累加器、各类加法器、FIFO、状态机、交通灯等） | 每题一段完整的「专业设计师」指令 + 模块签名 |

> 关键认知：`benchmark_inference/` 只做**前半段**——读题、拼 prompt、调模型、抽取代码、落盘。**后半段**（编译、仿真、功能打分）由各自基准的官方仓库完成。本讲所有源码都在这「前半段」里。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用途 |
|---|---|---|
| `benchmark_inference/test_on_verilog-eval.py` | VerilogEval 基准推理脚本 | 精读：双文件对齐、候选复制、结果落盘 |
| `benchmark_inference/test_on_rtllm.py` | RTLLM 基准推理脚本 | 精读：关键字匹配、按候选目录落 `.v` |
| `benchmark_inference/rtllm-1.1.json` | RTLLM 题目集（29 行 JSONL） | 看 `Instruction` + `Input` 字段结构 |
| `README.md` | 项目说明 | 取两个脚本的官方运行命令 |

## 4. 核心概念与源码讲解

### 4.1 两个基准脚本的整体定位与公共骨架

#### 4.1.1 概念说明

`benchmark_inference/` 里有两个长得非常像的脚本，因为它们解决的是同一个问题：**把一套题目挨个喂给模型，把模型写出的 Verilog 收下来**。它们之间不是「谁替代谁」，而是分别对接两个**评分口径不同**的基准：

- `test_on_verilog-eval.py` → 输出交给 [NVlabs/verilog-eval](https://github.com/NVlabs/verilog-eval) 评分。
- `test_on_rtllm.py` → 输出交给 [hkust-zhiyao/RTLLM](https://github.com/hkust-zhiyao/RTLLM) 评分。

正因为「只生成、不评分」，两个脚本的公共骨架高度一致，可以抽象成同一条流水线：

```
解析命令行参数
    ↓
加载题目（两个脚本的题目来源不同）
    ↓
加载模型与 tokenizer（fp16 + device_map，左填充）
    ↓
for 每道题（× 每个候选）:
    拼 prompt → model.generate → 解码成 s_full
    ↓
    用 u1-l5 的 Mistral 版抽取把 s_full 切成干净 module
    ↓
    落盘（两个脚本的落盘方式不同）
```

#### 4.1.2 核心流程

下面这张表是本讲的「导航地图」，先建立整体差异，4.3 / 4.4 再逐个深入：

| 维度 | `test_on_verilog-eval.py` | `test_on_rtllm.py` |
|---|---|---|
| 题目来源 | 外部克隆的 verilog-eval 仓库（**两个文件**） | 仓库自带 `rtllm-1.1.json`（**一个文件**） |
| 字段拼接 | `detail_description + '\n' + prompt` | `Instruction + '\n' + Input` |
| 题目对齐 | 两个文件按 `task_id` 线性查找拼接 | 单文件已天然对齐 |
| 候选数 `n` | 把题目列表**整体复制 n 份**拍平成一个大列表 | **外层循环 n 次**，每次建一个 `test_i/` 目录 |
| 单题最长续写 | `max_length = prompt_len + 1024` | `max_length = prompt_len + 2048` |
| 落盘格式 | 一个 JSONL 文件（`'a'` 追加），每行 `{task_id, description, prompt}` | 每个候选一个目录，每个设计一个 `{keyword}.v` |
| 特有参数 | `--bench_type`（`Machine`/`Human`）、`--output_file` | 无 `bench_type`，无 `output_file` |

#### 4.1.3 源码精读

两个脚本的「加载模型」段几乎逐字相同——都是 u1-l3 讲过的那套 `fp16 + device_map`，只是多了 `padding_side="left"`：

[benchmark_inference/test_on_verilog-eval.py:51-53](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_verilog-eval.py#L51-L53) —— VerilogEval 版的模型加载，`padding_side="left"` 是为批量生成做的左填充，`device_map=args.gpu_name` 接收一个整数选卡。

[benchmark_inference/test_on_rtllm.py:48-52](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_rtllm.py#L48-L52) —— RTLLM 版的模型加载，多了一行 `AutoConfig.from_pretrained`（本脚本里其实未被使用，属于冗余导入残留）。

两个脚本都把 `gen_batch_size` 写死为 1：

[benchmark_inference/test_on_verilog-eval.py:57-61](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_verilog-eval.py#L57-L61) —— 虽然套了一层 `for ite in range(gen_batch_size)` 的批量骨架，但 `gen_batch_size = 1`，实际每次只处理 1 道题。RTLLM 脚本里 [test_on_rtllm.py:53](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_rtllm.py#L53) 同样是 `gen_batch_size = 1`。

> 为什么留这个批量骨架却只跑 1？因为批量生成需要左填充 + 等长切片，作者把脚手架先搭好，但默认走最稳妥的「单条」路径，避免不同题目互相影响生成长度。

#### 4.1.4 代码实践

**实践目标**：在不动模型的前提下，把两个脚本「输入数据 → 模型 → 输出文件」的数据流画清楚。

**操作步骤**：

1. 打开 `benchmark_inference/` 下两个脚本与 `rtllm-1.1.json`。
2. 对照 4.1.2 的差异表，分别标注：每个脚本的「题目从哪来」「prompt 怎么拼」「结果写到哪」。
3. 用箭头画出一条从「原始题目文件」到「最终评分输入」的链路，标注哪一步在 `benchmark_inference/` 内、哪一步交给外部基准仓库。

**需要观察的现象**：两个脚本的数据流形状相同（题目 → prompt → generate → 抽取 → 落盘），但「题目来源」和「落盘格式」两端完全不同。

**预期结果**：你会得到两张同构的流水线图，差异只在首尾两端——这正是后续 4.3、4.4 要分别精读的部分。

#### 4.1.5 小练习与答案

**练习 1**：为什么两个脚本都不在本地做编译/仿真打分？

**参考答案**：因为打分逻辑（用iverilog编译、跑testbench、统计通过率）由 verilog-eval 与 RTLLM 两个官方基准仓库维护。`benchmark_inference/` 的定位是「跨基准的统一生成层」，把生成与评分解耦，便于复现：只要本目录产出的文件符合基准仓库的输入格式，就能直接喂进去评分。

**练习 2**：两个脚本都设了 `gen_batch_size = 1`，却保留了批量骨架。这样做有什么好处和坏处？

**参考答案**：好处是脚手架已就绪，调大 `gen_batch_size` 即可批量加速（左填充 + `output[len(inputs[0]):]` 切片已支持等长对齐）；坏处是默认配置下「批量」名不副实，读代码的人容易被这层循环误导，以为真的在批量生成。

---

### 4.2 命令行参数接口（argparse）

#### 4.2.1 概念说明

两个脚本都用 Python 标准库 `argparse` 解析命令行。理解参数清单是「跑通基准」的第一步——README 给出的运行命令本质就是一串 `--参数 值`。两个脚本共享四个核心参数（`model` / `temperature` / `gpu_name` / `n`），各自又有一两个独有参数。本模块把它们一次列清。

#### 4.2.2 核心流程

两个脚本的公共参数语义：

| 参数 | 类型 | 含义 |
|---|---|---|
| `--model` | str | HF 模型卡名或本地路径，如 `ishorn5/RTLCoder-v1.1` |
| `--temperature` | float | 采样温度（u1-l3 讲过：越大越随机） |
| `--gpu_name` | int | 整数选卡，直接喂给 `device_map` |
| `--n` | int | 每道题生成几个候选（pass@k 的 k） |

独有参数：

- VerilogEval 多了 `--output_dir`、`--output_file`、`--bench_type`（`Machine` 或 `Human`）。
- RTLLM 只有 `--output_dir`（因为输出是按设计名拆成一堆 `.v`，没有单一「结果文件」概念）。

#### 4.2.3 源码精读

[benchmark_inference/test_on_verilog-eval.py:28-36](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_verilog-eval.py#L28-L36) —— VerilogEval 的 argparse。注意 `--bench_type` 的注释明确写了 `it can be Machine or Human`，而 `--n` 的注释解释了它代表「每条指令生成多少个代码候选」。

[benchmark_inference/test_on_rtllm.py:20-26](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_rtllm.py#L20-L26) —— RTLLM 的 argparse，比 VerilogEval 少了 `output_file` 与 `bench_type`，`--n` 注释写作 `candidate num to each of the instruction`。

这两个参数块随后直接驱动后续路径与目录的构造。VerilogEval 用 `bench_type` 拼出两条不同的题目路径：

[benchmark_inference/test_on_verilog-eval.py:39-40](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_verilog-eval.py#L39-L40) —— `descri_path` 与 `input_path` 都用字符串拼接 `args.bench_type`，所以 `--bench_type Machine` 会指向 `VerilogDescription_Machine.jsonl` 与 `VerilogEval_Machine.jsonl`。这就是 README 说「想测 EvalHuman 就把 `--bench_type` 从 Machine 改成 Human」的实现原因。

#### 4.2.4 代码实践

**实践目标**：不加载模型，仅用 `--help` 校验两个脚本的参数清单，并核对 README 命令。

**操作步骤**：

1. 在仓库根目录运行（不需要 GPU，`--help` 不会加载模型）：
   ```bash
   python benchmark_inference/test_on_rtllm.py --help
   python benchmark_inference/test_on_verilog-eval.py --help
   ```
   > 注意：`test_on_rtllm.py` 在 `args = parser.parse_args()` **之后**才 `import json / tqdm`，但 `--help` 会在解析阶段提前退出，通常不受影响；若你的环境对延迟导入敏感，可把这两行 import 提前观察差异（仅本地实验，勿提交）。
2. 把 `--help` 输出的参数列表与 4.2.2 的表格逐项对照。
3. 打开 README 第 215、229 行附近的两条官方命令，把每个 `--xxx` 映射到表格里的语义。

**需要观察的现象**：`--help` 打印的参数顺序、类型与表格一致；README 的命令里 `--n 20`（VerilogEval）/ `--n 5`（RTLLM）、`--temperature 0.2` / `0.5` 的取值都能在表格里找到解释。

**预期结果**：你能口头解释 README 任一条命令里每个参数的作用，并能说出「为什么 RTLLM 命令里没有 `--output_file`」——因为它的输出是目录里的一堆 `.v`。

> 本地若无该环境，可只做「对照 README 与源码」的静态阅读，结论一致。

#### 4.2.5 小练习与答案

**练习 1**：README 里 VerilogEval 用 `--n 20 --temperature 0.2`，RTLLM 用 `--n 5 --temperature 0.5`。为什么 RTLLM 的温度更高、候选数更少？

**参考答案**：候选数 `n` 决定 pass@k 的 k：VerilogEval 算 pass@20 需要 20 个候选；RTLLM 基准惯例测更少的候选。温度方面，0.2 偏贪心、结果稳定可复现，0.5 更多样、能在少量候选里覆盖更多解空间。两者都是论文复现经验值，不是脚本硬编码。

**练习 2**：如果我把 `--gpu_name` 传成字符串 `"cuda:1"` 会怎样？

**参考答案**：会出错。脚本把 `args.gpu_name` 直接传给 `device_map` 并用于 `.to(args.gpu_name)`，README 与参数类型（`type=int`）都约定传**整数**（如 `0`）。传字符串既不符合 `argparse` 声明的 `int` 类型（会被 `int()` 转换失败），也不符合 `device_map` 期望的整型设备号。

---

### 4.3 VerilogEval 脚本精读：双文件对齐与候选复制

#### 4.3.1 概念说明

VerilogEval 的题目被官方仓库拆成**两个文件**：一个放「自然语言详细描述」（`detail_description`），一个放「模块签名提示」（`prompt`），两者靠共同的 `task_id` 关联。RTL-Coder 的脚本要做三件事：

1. 把两个文件按 `task_id` 拼成一条完整 prompt（描述 + 签名）。
2. 用「把题目列表复制 `n` 份」的方式，把每道题变成 `n` 个候选任务。
3. 逐条生成、抽取、落盘到一个 JSONL 结果文件。

#### 4.3.2 核心流程

```
des_data  ← load_json(descri_path)     # 每条 {task_id, detail_description}
input_data ← load_json(input_path)     # 每条 {task_id, prompt}

# 产生 n 个候选：把题目列表整体复制 n 份
des_data = des_data 重复 n 次

while 还有题目:
    取一条 description
    在 input_data 里线性查找相同 task_id → 拿到 prompt
    prompt = detail_description + '\n' + prompt + '\n'
    generate → s_full → 抽取 s
    把 {task_id, description, prompt} 追加写进 output_file
```

值得注意的三点（后两条是本脚本的「坑」）：

- **`n` 候选用「列表复制」实现**：不是循环调用，而是把题目数组本身重复 `n` 份，再线性遍历这个变长数组。
- **结果文件用 `'a'` 追加打开**，且不在脚本开头清空——重复运行会累加。
- **抽取后的代码 `s` 实际并未落盘**（详见 4.3.3 的源码观察）。

#### 4.3.3 源码精读

先看双文件加载与候选复制：

[benchmark_inference/test_on_verilog-eval.py:42-48](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_verilog-eval.py#L42-L48) —— 先加载描述文件，再把 `tmp_list`（原始题目）重复 `args.n` 次追加到 `des_data`。于是 `des_data` 变成「原始题目 × n」的扁平列表，后续 `while` 循环遍历它即可一次性产出 `len × n` 条结果。进度条 total 也相应设成 `len(des_data) * args.n`（此处因 `des_data` 还没复制，等价于原始题数 × n）。

再看题目对齐与 prompt 拼接：

[benchmark_inference/test_on_verilog-eval.py:62-75](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_verilog-eval.py#L62-L75) —— 对每条 description，用 `for j in range(len(input_data))` **线性扫描** `input_data`，找到 `task_id` 相同的那条取出 `prompt`，再拼成 `dic['description'] + '\n' + dic['prompt'] + '\n'`。这是一个 O(题目数²) 的朴素对齐——题目不多时无所谓，但能看出脚本没在性能上做优化。

生成与解码：

[benchmark_inference/test_on_verilog-eval.py:81-86](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_verilog-eval.py#L81-L86) —— 注意 `max_length=len(inputs[0]) + 1024`（u1-l3 讲过 `max_length` 是 prompt + 续写的总长，所以这里给续写留了 1024 个 token）。解码时用 `output[len(inputs[0]):]` 切掉 prompt 段——**正因为前面用了左填充**，所有样本的 prompt 都被右对齐到 `len(inputs[0])`（批量内最长），所以从这一列往后切，就是每条各自的续写。

抽取段（u1-l5 的原样复用）：

[benchmark_inference/test_on_verilog-eval.py:101-112](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_verilog-eval.py#L101-L112) —— 启用的只有 Mistral 版两步：`s_full.rsplit('endmodule', 1)` 截断补尾，再 `rfind('tb_module')` / `find('testbench')` 剔测试台。Deepseek 版的 `endmodulemodule` / `top_module` 处理被整段注释保留（与 u1-l5 完全一致）。

> ⚠️ **源码观察（重要）**：请注意 [test_on_verilog-eval.py:85-117](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_verilog-eval.py#L85-L117) 这一段。抽取得到的代码 `s` 只在 `for res_i, output in enumerate(outputs):` 循环里被赋值，**没有任何一行把它写回 `dic` 或追加到某个列表**。紧接着的落盘循环写的是 `dic_list`，而 `dic_list` 里的 `dic` 只含 `task_id` / `description` / `prompt`（见 [L62-66](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_verilog-eval.py#L62-L66)）。也就是说，**当前 HEAD 的 Mistral 默认分支下，生成的 Verilog `s` 实际被丢弃了**，最终 JSONL 文件每行只是把输入回写。这很可能是一次历史改动里漏掉了一行类似 `dic['completion'] = s` 的赋值。对比 4.4 的 RTLLM 脚本（它确实把 `s` 拼进了 `result` 并写盘），更能确认这是 VerilogEval 脚本独有的遗漏。综合实践会请你把这行补上。

#### 4.3.4 代码实践

**实践目标**：动手修补 4.3.3 发现的「`s` 未落盘」问题，让 VerilogEval 脚本真正写出可被基准仓库消费的生成结果。

**操作步骤**：

1. 复制一份脚本到本地实验目录（**不要改原文件**），如 `benchmark_inference/my_test_on_verilog-eval.py`。
2. 在抽取 `s` 之后、写文件之前，把 `s` 挂到对应 `dic` 上。一种最小改法是利用 `res_i` 与 `dic_list` 的顺序一致：
   ```python
   # 示例代码（非项目原有代码）：补全生成结果的持久化
   for res_i, output in enumerate(outputs):
       s_full = tokenizer.decode(output[len(inputs[0]):].cpu().squeeze(), skip_special_tokens=True)
       s = s_full.rsplit('endmodule', 1)[0] + "\n" + "endmodule"
       index = s.rfind('tb_module')
       if index == -1:
           index = s.find('testbench')
       if index != -1:
           s_tmp = s[:index]
           s = s_tmp.rsplit("endmodule", 1)[0] + "\n" + "endmodule"
       dic_list[res_i]['completion'] = s   # ← 补这一行，把抽取结果存回去
   ```
3. 准备一份很小的假描述文件与假题目文件（各 2 条，`task_id` 对齐），用一个本地小模型或 mock 模型跑一次。

**需要观察的现象**：补丁前，输出 JSONL 每行只有 `task_id / description / prompt`；补丁后，每行多出一个 `completion` 字段，内容是截断补尾后的 Verilog。

**预期结果**：补丁后产出的 JSONL 可被 verilog-eval 仓库按 `task_id + completion` 的格式读取并评分（具体字段名以你所用 verilog-eval 版本的要求为准，可能需要把 `completion` 调整为该仓库期望的键名）。

> 如果本地没有可用 GPU/模型，可以把 `model.generate` 替换为一个返回固定文本的 mock 函数（参考 u2-l3 的 mock 思路），只验证「补丁后 completion 字段确实被写入」这一行为——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：脚本为什么用「把列表复制 n 份」而不是「外层循环 n 次」来实现候选？

**参考答案**：两种方式都能产出 `len × n` 条结果，区别在落盘形状。VerilogEval 想把所有候选写进**同一个 JSONL 文件**（便于基准仓库一次性读取），所以拍平成一个大列表、顺序遍历、统一追加即可。RTLLM 则正好相反——它要按候选分目录（见 4.4），所以用了外层循环。

**练习 2**：结果文件用 `'a'`（追加）模式打开，且脚本开头不删除旧文件。这会带来什么问题？

**参考答案**：重复运行同一命令会让旧结果和新结果叠在同一文件里，造成题目重复、基准评分失真。复现实验前应手动清空或删除旧的 `output_file`。相比之下，RTLLM 脚本每次建新 `test_i/` 目录，重复运行的污染面更小（但同样不会主动清理旧 `.v`）。

**练习 3**：`detail_description + '\n' + prompt` 这种「先描述后签名」的拼接顺序，与 u1-l3 讲的 prompt 范式一致吗？

**参考答案**：一致。u1-l3 总结的范式就是「自然语言描述（含端口与行为）+ 模块签名骨架」。这里 `detail_description` 承担自然语言描述，`prompt` 承担签名骨架，二者用换行拼起来正是同一套范式，只是题目数据来自基准仓库而非手写。

---

### 4.4 RTLLM 脚本精读：关键字匹配与 `.v` 落盘

#### 4.4.1 概念说明

RTLLM 基准的题目本就在仓库里（`rtllm-1.1.json`），每条天然带有 `Instruction`（完整「专业设计师」指令）和 `Input`（模块签名），不需要二次对齐。这个脚本的两个特别之处是：

1. **候选数 `n` 用「外层循环 + 每候选一个目录」实现**：第 `i` 个候选写进 `test_i/`，正好契合 RTLLM 基准对多候选的目录约定。
2. **落盘靠 `design_list` 关键字匹配**：脚本内置一张 28 个设计名的列表，对每段生成代码做「子串匹配，命中即停」，把代码写进 `{命中的关键字}.v`。

#### 4.4.2 核心流程

```
bench_data ← load_testjson('rtllm-1.1.json')   # 每条 {Instruction, Input}

for iter in 0..n-1:
    建 output_dir/test_{iter+1}/
    while 还有题目:
        prompt = Instruction + '\n' + Input + '\n'
        generate（max_length = prompt_len + 2048）→ s_full → 抽取 s
        result = Input + '\n' + s                 # 完整 Verilog 文件内容
        for keyword in design_list:               # 顺序扫描 28 个关键字
            if keyword in result:                 # 子串匹配
                把 result 写进 test_{iter+1}/{keyword}.v
                break                             # 命中即停
```

这里最值得品味的是 `design_list` 的「子串匹配 + 命中即停」——它既是功能实现，也埋着一个脆弱点（见 4.4.3 末尾）。

#### 4.4.3 源码精读

先看候选目录的建立方式：

[benchmark_inference/test_on_rtllm.py:57-61](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_rtllm.py#L57-L61) —— 外层 `for iter in range(args.n)`，每轮建一个 `test_{iter+1}` 目录（用 `os.path.exists` 守卫，避免重复运行时报 `FileExistsError`，但**不会清理**旧文件）。这与 VerilogEval 的「拍平复制」形成鲜明对比：RTLLM 把候选维度显式编码进目录结构。

prompt 拼接与生成：

[benchmark_inference/test_on_rtllm.py:67-77](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_rtllm.py#L67-L77) —— `prompt = dic['Instruction'] + '\n' + dic['Input'] + '\n'`，`max_length=len(inputs[0]) + 2048`（给续写留 2048，比 VerilogEval 的 1024 更宽裕，因为 RTLLM 设计更复杂）。同样用左填充 + `output[len(inputs[0]):]` 切 prompt。

抽取与结果拼装：

[benchmark_inference/test_on_rtllm.py:94-104](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_rtllm.py#L94-L104) —— 抽取段与 VerilogEval 完全相同（u1-l5 的 Mistral 版）。拼装时 `result_list.append(inp_list[res_i] + s)`：`inp_list[res_i]` 是 `Input + '\n'`（模块签名），`s` 是生成体，拼起来正好是一个完整 `.v` 文件。**注意它确实把 `s` 用上了**——这正是 4.3.3 说的「对比之下更能确认 VerilogEval 漏写」的依据。

最关键的落盘段：

[benchmark_inference/test_on_rtllm.py:107-113](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_rtllm.py#L107-L113) —— 对每段 `result`，**按 `design_list` 的顺序**逐个关键字做 `if keyword in result` 子串判断，命中就把 `result` 写成 `{keyword}.v` 并 `break`。

`design_list` 本身定义在脚本顶部：

[benchmark_inference/test_on_rtllm.py:30-35](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_rtllm.py#L30-L35) —— 28 个设计名（`accu`、`adder_8bit`、…、`width_8to16`）。

题目文件结构，看 `rtllm-1.1.json` 第一条：

[benchmark_inference/rtllm-1.1.json:1](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/rtllm-1.1.json#L1) —— 第一条是 `accu`（串行输入累加器）。`Instruction` 是完整的「Please act as a professional verilog designer. …」指令（含模块名、输入/输出端口、实现说明、`Give me the complete code.`），`Input` 是 `module accu( … );` 模块签名。正因为 `Input` 里已经写了 `module accu(`，所以 `keyword='accu'` 一定能在 `result` 里子串命中——这就是匹配能work的根本原因：**它实际是在匹配 `Input` 里的模块声明名**，而非真的在「理解」生成代码。

> ⚠️ **脆弱点（子串匹配的副作用）**：`if keyword in result` 是朴素的子串包含判断，且 `design_list` 的**顺序**决定优先级（`break` 在第一个命中处停止）。这带来两个隐患：
> 1. **短关键字易误伤**：`alu` 是 `value` / `module value_reg` 的子串（`v-a-l-u-e` 含 `alu`）；`accu` 是 `accumulation` / `accumulator` 的子串。若某设计的生成代码里出现了这些词，且对应短关键字在列表里**排在前面**，就会被错认成那个短关键字设计。
> 2. **顺序敏感**：`design_list` 里 `alu` 排在第 26 位左右，而 `div_16bit`、`traffic_light`、`width_8to16` 排在它之后。理论上，若 `width_8to16` 的生成代码里含 `value` 这类词，`alu` 会先命中，代码被错写成 `alu.v`，而 `width_8to16.v` 缺失。
>
> 实际是否触发取决于模型的生成内容，属于**运行时风险**而非必然 bug。稳健的改法是：直接用 `Input` 里解析出的模块名（`module (\w+)`）去匹配 `design_list`，而不是对整段 `result` 做子串包含。综合实践里会请你验证自己跑出来的 `.v` 是否都落在了正确的关键字上。

#### 4.4.4 代码实践（本讲指定实践）

**实践目标**：在 `rtllm-1.1.json` 上实跑 `test_on_rtllm.py`，设 `n=1`、`temperature=0.2`，验证输出目录 `test_1` 下是否按 `design_list` 关键字生成了对应 `.v` 文件。

**操作步骤**：

1. 确认依赖就绪（`torch` / `transformers`，参考 u1-l2 的 requirements 说明）。
2. 在仓库根目录运行（把 `<model>` 换成你本地可用的 RTLCoder 模型卡或路径）：
   ```bash
   python benchmark_inference/test_on_rtllm.py \
       --model ishorn5/RTLCoder-v1.1 \
       --n 1 \
       --temperature 0.2 \
       --gpu_name 0 \
       --output_dir ./rtllm_out
   ```
3. 运行结束后查看输出目录：
   ```bash
   ls rtllm_out/test_1/
   ```

**需要观察的现象**：

- `rtllm_out/test_1/` 下应出现一系列 `.v` 文件，文件名是 `design_list` 里的关键字（如 `accu.v`、`adder_8bit.v`、`fsm.v` 等）。
- 因为 `n=1`，只有 `test_1` 一个目录（不会有 `test_2`）。
- 用 `head` 看任一 `.v`，内容应是 `Input` 模块签名 + 模型生成的模块体，以 `endmodule` 结尾。

**预期结果**：

- 理想情况下，28 个设计各产出 1 个 `.v`，文件名与设计一一对应。
- **重点核查**（对应 4.4.3 的脆弱点）：统计 `test_1/` 下 `.v` 文件数量与名字。如果出现「某个短关键字（尤其 `alu`）的 `.v` 数量异常多」或「`width_8to16.v` / `div_16bit.v` 缺失」，就说明子串误伤在本次运行中触发了。把你观察到的文件名清单记下来，与 `design_list` 比对。

> 若本地无 GPU 或模型，可退化为「源码阅读型实践」：把 `model.generate` 替换为返回含 `value` 字样的 mock 输出，人为复现「`alu` 抢走 `width_8to16`」的场景，验证子串匹配的顺序敏感性——**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：RTLLM 脚本里 `result = Input + '\n' + s`，为什么不直接写 `s`？

**参考答案**：因为模型生成体 `s` 有时不会重复模块签名（它接在 `Input` 后面续写）。把 `Input`（含 `module xxx(...)` 声明）拼到最前面，才能保证 `.v` 文件是一个结构完整、可直接交给 iverilog 编译的 Verilog 文件。同时也正因 `Input` 里有模块名，`design_list` 的关键字匹配才有可靠的命中来源。

**练习 2**：若要把候选数从 `n=1` 改成 `n=5`，输出结构会怎样变化？这对应评估里的什么指标？

**参考答案**：会出现 `test_1/` ~ `test_5/` 五个目录，每个目录里是同一批设计的第 `i` 个候选。这对应 RTLLM 基准的 pass@k 评估——5 个候选可以算 pass@1、pass@5，衡量「在 k 次尝试内至少有一次通过」的概率。

**练习 3**：请提出一种比「对整段 result 做子串匹配」更稳健的落盘命名方法。

**参考答案**：用正则从 `Input`（或生成代码的第一个 `module` 声明）里解析出模块名，再用「模块名 == design_list 里的某个关键字」做**精确相等**匹配，而不是子串包含。这样 `value` 里的 `alu` 就不会再误命中 `alu`。代价是要处理大小写（`RAM_single` 是大写）和命名归一化，但能消除 4.4.3 提到的顺序敏感与短关键字误伤。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来，做一次「跨两个基准的最小端到端推理与自检」。

1. **准备阶段**（对应 4.1 / 4.2）：把 4.1.2 的对比表抄在一张纸上，标注每个脚本各自的输入文件、prompt 拼法、输出位置。口头复述 README 第 215、229 行两条命令里每个参数的含义。
2. **VerilogEval 侧**（对应 4.3）：按 4.3.4 给脚本打补丁，补上 `dic['completion'] = s`，用 2~3 条假题目 + mock 模型跑通，确认输出 JSONL 里确有 `completion` 字段。
3. **RTLLM 侧**（对应 4.4）：按 4.4.4 实跑 `--n 1 --temperature 0.2`，统计 `test_1/` 下的 `.v` 文件名清单，与 `design_list` 比对，记录是否有疑似子串误伤。
4. **横向对照**：写一段话回答——为什么两个脚本「生成 + 抽取」几乎完全一致，却在「候选编码方式」和「落盘格式」上分道扬镳？把这跟你画的数据流图对应起来。

**验收标准**：
- 能说清 VerilogEval「双文件 + task_id 对齐 + 拍平复制 + 单 JSONL」与 RTLLM「单文件 + 外层循环 + 多目录 + 关键字 .v」的实现差异。
- 能复现并解释 4.3.3 的「`s` 未落盘」现象，并给出可用的补丁。
- 能识别 4.4.3 的子串匹配风险，并对自己跑出的 `.v` 文件名做正确性核查。

## 6. 本讲小结

- `benchmark_inference/` **只生成代码、不评分**：VerilogEval 结果交 NVlabs 仓库，RTLLM 结果交 hkust-zhiyao/RTLLM 仓库，本目录是统一的「生成层」。
- 两脚本共享同一套骨架：`argparse → 加载模型(fp16/device_map/左填充) → 循环 → generate → u1-l5 的 Mistral 抽取 → 落盘`，`gen_batch_size` 都写死为 1。
- 参数上，`model/temperature/gpu_name/n` 四件套相同；VerilogEval 多 `--bench_type`（Machine/Human）与 `--output_file`，RTLLM 只有 `--output_dir`。
- VerilogEval 靠**两个文件按 `task_id` 线性对齐**拼 `detail_description + prompt`，用「列表复制 n 份」产生候选，结果追加进单个 JSONL。
- RTLLM 用自带的 `rtllm-1.1.json`（`Instruction + Input`），用「外层循环 n 次 + `test_i/` 目录」产生候选，靠 `design_list` 的**子串匹配 + 命中即停**把代码落成 `{keyword}.v`。
- 两处源码「坑」：VerilogEval 脚本里抽取后的 `s` **未被写盘**（生成的 Verilog 被丢弃）；RTLLM 的子串匹配对短关键字（如 `alu`/`accu`）和列表顺序敏感，存在误分类风险。

## 7. 下一步学习建议

- **向上一层（专家层 u3）**：本讲是进阶层（u2）的收尾。接下来进入 u3，重点回到 `train/` 目录——先读 u3-l1 的 `mle_scoring.py` 质量评分训练，理解「这些基准上表现好的模型是怎么训出来的」。
- **横向扩展**：如果你想扩评到新基准，本讲的两个脚本就是模板——仿照 `test_on_rtllm.py` 写一个 `test_on_<your_bench>.py`，关键是把「题目加载 + prompt 拼接 + 落盘命名」三段替换成新基准的约定（参考 u3-l5 对扩展实践的讨论）。
- **补漏建议**：若你要真正复现 VerilogEval 数字，务必先按 4.3.4 修补 `s` 落盘问题，并核对 verilog-eval 仓库对你输出字段名（如 `completion`）的具体要求，否则基准仓库会读到空答案。
