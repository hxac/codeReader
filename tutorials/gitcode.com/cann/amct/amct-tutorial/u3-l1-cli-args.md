# CLI 参数体系与命令分发

## 1. 本讲目标

本讲是「LLM 量化工程主链路」单元的第一讲。在 [u1-l4](u1-l4-first-quant-cli.md) 里，你已经知道一次完整量化被拆成 `eval → extract_ptq_data → ptq → deploy` 四条命令，并且统一用 `python -m amct_pytorch.<cmd>` 调用。但当时我们没有回答两个关键问题：

1. 这些命令的**参数是从哪里定义的、各有什么默认值**？
2. `python -m amct_pytorch.eval` 这个字符串，**到底是怎么一步步走到真正干活的代码里的**？

学完本讲，你应当能够：

- 看懂 `amct_pytorch/cli/llm/args.py` 中 `parser_gen` 定义的全部命令行参数及其默认值；
- 说清 `quant_target` 的四个取值（`mlp`/`moe`/`attn-linear`/`attn-cache`）各自量化的是模型的哪一块；
- 解释 `args.bit_policy` 是如何由 `--bit_config` 指向的 yaml 文件初始化出来的；
- 讲清 `_validate_eval_mode` 为什么在 `eval_mode=bf16` 时禁止出现低于 16-bit 的配置条目。

本讲**只讲参数解析与命令分发**，不深入 Workflow 内部编排（那是 [u3-l2](u3-l2-workflow-skeleton.md) 的任务），也不讲算法实现。

## 2. 前置知识

本讲假设你已经掌握 [u1-l4](u1-l4-first-quant-cli.md) 建立的术语。这里做最简回顾，并补充两个本讲要用到的 Python 概念。

**回顾：四阶段命令与两条数据链。** 四条命令按固定顺序串联，靠两个目录接力：`data_dir` 在 `extract_ptq_data` 与 `ptq` 之间传递校准数据，`*_param_dir`（如 `--moe_mlp_param_dir`）在 `ptq` 与 `deploy` 之间传递量化参数。`eval` 是旁观者，不参与数据链，只产出 PPL（困惑度）。

**回顾：五个核心用户参数。** `--model`（模型路径）、`--quant_target`（量化哪一块）、`--quant_dtype`（量化的数据类型）、`--bit_config`（位宽 yaml）、`--algos`（量化算法）。

**补充：Python `argparse` 速记。** AMCT 用标准库 `argparse` 解析命令行。你需要知道三件事：

- `parser.add_argument('--foo', default=1)`：注册一个 `--foo` 参数，缺省值为 `1`，解析后挂在 `args.foo` 上。
- `choices=[...]`：限定该参数只能取列表里的值，传别的会直接报错。
- `nargs="+"` / `nargs="*"`：参数可接收多个值（`+` 至少一个，`*` 可以为零个），解析后 `args.xxx` 是一个列表。

**补充：`python -m 包.模块` 的含义。** `python -m amct_pytorch.eval` 表示「把 `amct_pytorch/eval.py` 当作脚本运行」，即执行该文件里 `if __name__ == "__main__":` 下的代码。这是 AMCT 用户侧统一的入口形式。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `amct_pytorch/cli/llm/args.py` | **本讲主角**。定义 `parser_gen(command=None)`，集中声明所有命令行参数，并完成 `bit_policy` 初始化与 `eval` 模式校验。 |
| `amct_pytorch/cli/llm/eval.py` | `eval` 子命令真入口：`parser_gen(command="eval")` → 构造 `LlmEvalWorkflow` → `run()`。 |
| `amct_pytorch/cli/llm/ptq.py` | `ptq` 子命令真入口，套路同上，构造 `LlmPtqWorkflow`。 |
| `amct_pytorch/cli/llm/deploy.py` | `deploy` 子命令真入口，构造 `LlmDeployWorkflow`。 |
| `amct_pytorch/cli/llm/extract_ptq_data.py` | `extract_ptq_data` 子命令真入口，构造 `LlmExtractPtqDataWorkflow`。 |
| `amct_pytorch/quantization/bit_policy.py` | `BitPolicy` 类。把 yaml 解析成内存中的位宽策略对象，并提供 `has_quant_linear()` / `has_quant_cache()` 等查询方法，供校验使用。 |
| `amct_pytorch/eval.py`（根目录薄壳） | 用户敲 `python -m amct_pytorch.eval` 命中的第一层模块，内部延迟导入并转发给 `cli/llm/eval.py`。 |

一句话理解分工：**用户敲的命令 → 根目录薄壳（加速启动）→ `cli/llm/<cmd>.py` 的 `main()` → `args.py` 解析参数 → 构造 Workflow → `run()`**。本讲聚焦前半段（命令分发 + 参数解析），后半段（Workflow）留给 [u3-l2](u3-l2-workflow-skeleton.md)。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **4.1 cli 子命令 main 入口** —— 命令分发的骨架。
2. **4.2 parser_gen 参数定义** —— 参数全表与分组。
3. **4.3 quant_target 四个取值** —— 量化目标到底指什么。
4. **4.4 BitPolicy 初始化与 _validate_eval_mode 校验** —— 位宽策略怎么来、eval 为什么校验它。

---

### 4.1 cli 子命令 main 入口（命令分发骨架）

#### 4.1.1 概念说明

AMCT 把「用户命令」和「真正干活的工作流」用一层极薄的「壳」隔开。这样做有两个好处：

- **启动加速**：根目录的 `amct_pytorch/eval.py` 等模块不立即 import 重依赖（PyTorch、torch_npu 等），而是把真正的 import 推迟到 `main()` 函数内部，避免 `python -m amct_pytorch.eval --help` 都要等很久。
- **统一套路**：四条命令的真入口 `cli/llm/<cmd>.py` 结构几乎一模一样，降低阅读成本。

#### 4.1.2 核心流程

以 `eval` 为例，一次命令的转发链路如下：

```text
用户: python -m amct_pytorch.eval --model ... --eval_mode bf16 ...
        │
        ▼
amct_pytorch/eval.py  的 main()          # 根目录薄壳
        │  延迟 import
        ▼
amct_pytorch/cli/llm/eval.py 的 main()    # 真入口
        │  args = parser_gen(command="eval")
        │  workflow = LlmEvalWorkflow(args)
        │  workflow.run()
        ▼
真正干活的 Workflow（u3-l2 详讲）
```

四条命令的 `main()` 唯一区别只有两处：传给 `parser_gen` 的 `command` 字符串、以及构造的 Workflow 类。

#### 4.1.3 源码精读

先看根目录薄壳 [amct_pytorch/eval.py:L18-L24](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/eval.py#L18-L24)。它只做一件事——把真正的 import 关进 `main()` 函数体里，再调用 `cli.llm.eval.main`：

```python
"""Run LLM eval with ``python -m amct_pytorch.eval``."""

def main():
    from amct_pytorch.cli.llm.eval import main as cli_main
    return cli_main()
```

> 这个 `from ... import ...` 写在函数内部（而不是文件顶部）就是「延迟导入」。它的价值在于：解释器执行到 `def main():` 这一行时并不会真正去 import，只有 `main()` 被调用时才触发，从而把重依赖的加载推迟到最后一刻。

再看真入口 [amct_pytorch/cli/llm/eval.py:L22-L25](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/eval.py#L22-L25)：

```python
def main():
    args = parser_gen(command="eval")
    workflow = LlmEvalWorkflow(args)
    workflow.run()
```

这就是 AMCT 所有子命令的统一三段式：**解析参数 → 构造 Workflow → 执行 run()**。`ptq.py`、`deploy.py`、`extract_ptq_data.py` 的 `main()` 与之完全同构，只是把 `"eval"` 换成各自的命令名、把 `LlmEvalWorkflow` 换成对应的 Workflow 类（见 [ptq.py:L22-L25](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/ptq.py#L22-L25)、[deploy.py:L22-L25](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/deploy.py#L22-L25)、[extract_ptq_data.py:L22-L25](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/extract_ptq_data.py#L22-L25)）。

> 注意 `command` 参数：`parser_gen(command="eval")` 把当前命令名传进去，目的是让参数解析器知道「我现在是哪条命令」，从而决定是否做命令专属的校验（详见 4.4）。这是同一份参数定义服务四条命令的关键钩子。

#### 4.1.4 代码实践

**实践目标**：亲手验证「根目录薄壳 → 真入口」的转发链确实存在，且四条命令的 `main()` 同构。

**操作步骤**：

1. 打开仓库根目录，用 `python -m` 的「只读」方式查看根薄壳模块（不需要 NPU、不需要模型，只是看 import 链）：

   ```bash
   python -c "import amct_pytorch.eval as m; print(m.main)"
   ```

2. 打开 `amct_pytorch/cli/llm/` 下的 `eval.py`、`ptq.py`、`deploy.py`、`extract_ptq_data.py` 四个文件。

**需要观察的现象**：
- 步骤 1 能打印出 `<function main at ...>`，说明 `amct_pytorch.eval` 确实暴露了一个 `main`，且它内部会去 `cli.llm.eval` 取真入口。
- 步骤 2 中四个文件的 `main()` 函数体结构完全一致，只有 `command="..."` 和 `Llm...Workflow(args)` 两处不同。

**预期结果**：你会确认 AMCT 的命令分发是「薄壳 + 统一三段式」结构。

> 如果当前环境没有安装完整依赖，步骤 1 的 import 可能因缺少 `torch` 等失败——这恰好反证了「延迟导入」的价值：失败发生在 `main()` 调用时，而不是模块加载时。**待本地验证**（视环境是否装好 torch 而定）。

#### 4.1.5 小练习与答案

**练习 1**：如果把根目录 `amct_pytorch/eval.py` 里的 `from amct_pytorch.cli.llm.eval import main as cli_main` 移到文件顶部（模块级），会有什么副作用？

> **答案**：一旦移动到顶部，`import amct_pytorch.eval` 就会立刻触发 `amct_pytorch.cli.llm.eval` 的加载，进而级联 import `LlmEvalWorkflow` 依赖的 PyTorch/torch_npu 等重包。结果是连 `python -m amct_pytorch.eval --help` 这种「只想看帮助」的操作也要等重依赖加载完成，启动变慢——延迟导入的加速效果就没了。

**练习 2**：四条命令的 `main()` 都调用 `parser_gen(command=...)`，为什么 `command` 不能省略？

> **答案**：`command` 是触发命令专属校验的开关。例如 `command == "eval"` 时才会执行 `_validate_eval_mode(args)`（见 4.4）。如果省略，`command` 默认为 `None`，校验就不会跑，用户可能在 `eval_mode=bf16` 下配了低比特 yaml 却得不到任何错误提示，等跑完才发现结果不对。

---

### 4.2 parser_gen 参数定义（参数全表与分组）

#### 4.2.1 概念说明

`parser_gen(command=None)` 是 AMCT LLM 量化的**唯一参数定义中心**。四条命令共用这一份定义——也就是说，`eval`、`extract_ptq_data`、`ptq`、`deploy` 能接受的参数是同一套。不同命令只是「实际用到哪些参数」不同，但参数空间是共享的。

这种设计的好处是：用户不需要为每条命令记一套不同的参数表，只需要理解一份。

#### 4.2.2 核心流程

`parser_gen` 的执行过程可以概括为三步：

1. 用 `argparse.ArgumentParser()` 新建解析器；
2. 逐个 `add_argument(...)` 注册所有参数（含类型、默认值、`choices` 限定）；
3. `parser.parse_args()` 解析命令行，得到 `args` 对象；
4. 后处理：根据 `--bit_config` 初始化 `args.bit_policy`；若 `command == "eval"` 则做 `_validate_eval_mode` 校验。

参数按用途可分成六组，便于记忆：

| 组别 | 参数 | 一句话用途 |
| --- | --- | --- |
| 模型与设备 | `--model` `--model_name` `--device` `--seed` | 要量化的模型路径、名字（路由用）、NPU 设备号、随机种子 |
| 量化目标与精度 | `--quant_target` `--quant_dtype` `--bit_config` `--algos` | 量化哪一块、用什么数据类型、位宽怎么配、上哪些算法 |
| 数据与评测 | `--data_dir` `--output_dir` `--nsamples` `--seq_len` `--eval_mode` `--wikitext_final_out` | 校准数据目录、输出目录、采样数、序列长度、评测模式、PPL 结果文件 |
| 训练超参（PTQ 优化用） | `--base_lr` `--optimizer` `--weight_decay` `--momentum` `--lr_scheduler` `--min_lr` `--lr_step_size` `--lr_gamma` `--epochs` `--cali_bsz` | BlockwiseSolver 逐块优化量化参数时的学习率/优化器/调度/轮数 |
| 块范围与粒度 | `--granularity` `--start_block_idx` `--end_block_idx` | 量化粒度（block/model/tensor）、起止 decoder 层 |
| 阶段间接力目录 | `--attn_linear_param_dir` `--attn_cache_param_dir` `--moe_mlp_param_dir` | ptq 产出、deploy 读取的量化参数目录 |
| 其它 | `--is_per_tensor` `--k_size` | 激活是否用 per-tensor 统计、可学习矩阵尺寸（FlatQuant 等） |

> 提示：上表中只有「量化目标与精度」「阶段间接力目录」两组是理解量化主链路必须掌握的；训练超参那组服务于 PTQ 优化（[u4-l3](u4-l3-blockwise-solver.md) 详讲），这里先知道用途即可。

#### 4.2.3 源码精读

函数签名与解析器创建见 [amct_pytorch/cli/llm/args.py:L36-L37](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py#L36-L37)：

```python
def parser_gen(command=None):
    parser = argparse.ArgumentParser()
```

五个核心用户参数的定义（这是本讲要求重点掌握的）：

- `--model`，默认 `'deepseek-ai/DeepSeek-V4-Flash'`，见 [args.py:L39-L44](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py#L39-L44)。模型路径。
- `--quant_target`，`nargs="+"`（至少一个值），`choices=["mlp", "moe", "attn-linear", "attn-cache"]`，默认空列表，见 [args.py:L61-L67](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py#L61-L67)。这是本讲 4.3 的主角。
- `--quant_dtype`，`choices=['int', 'mxfp', 'hifp']`，默认空串，见 [args.py:L88-L94](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py#L88-L94)。对应 [u2-l2](u2-l2-quant-dtypes-overview.md) 讲过的三大数据类型家族。
- `--bit_config`，无默认值（`None`），是一个 yaml 文件路径，见 [args.py:L135-L141](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py#L135-L141)。
- `--algos`，`nargs="*"`（可零个或多个），默认空列表，见 [args.py:L116-L121](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py#L116-L121)。算法名会被注册表按 target 路由（[u6-l2](u6-l2-algo-target-routing.md) 详讲）。

几个容易踩坑的默认值（均来自 [args.py:L51-L133](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py#L51-L133)）：

- `--device` 默认 `'npu:0'`（不是 `cuda:0`，提醒我们 AMCT 跑在昇腾 NPU 上）；
- `--granularity` 默认 `'model'`（注意不是 `block`，examples 里通常会显式传 `--granularity block`，见 [examples/eval.sh:L26](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py#L52-L57)）；
- `--eval_mode` 默认 `'bf16'`，`choices=['bf16', 'quant']`；
- `--seq_len` 默认 `4096`，`--nsamples` 默认 `128`（校准样本数）；
- `--end_block_idx` 默认 `61`（与默认模型 DeepSeek-V4-Flash 的层数相关，换模型时一般要改）。

最后，解析与后处理见 [args.py:L148-L160](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py#L148-L160)：

```python
args = parser.parse_args()

if args.bit_config:
    args.bit_policy = BitPolicy.from_yaml(args.bit_config)
else:
    args.bit_policy = BitPolicy()

if command == "eval":
    _validate_eval_mode(args)

os.environ["TOKENIZERS_PARALLELISM"] = "false"
return args
```

这段是本讲后半段（4.4）的核心：先解析，再初始化 `bit_policy`，再按命令做校验，最后顺手关掉 tokenizers 的并行告警，返回 `args`。

#### 4.2.4 代码实践

**实践目标**：用「只读」方式让 `parser_gen` 把帮助文本打印出来，从而获得一份**真实的、权威的参数清单**（而不是凭记忆）。

**操作步骤**：

1. 在仓库根目录执行（注意：`--help` 会触发 `SystemExit`，这是 argparse 的正常行为）：

   ```bash
   python -c "import sys; sys.argv=['x','--help']; from amct_pytorch.cli.llm.args import parser_gen; parser_gen()" 2>&1 | head -80
   ```

   若依赖未装齐导致 import 失败，则退化为「源码阅读型实践」：直接通读 [args.py:L39-L146](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py#L39-L146)，逐行抄录参数名与默认值。

2. 把你看到的每个参数及其默认值填进一张表（建议按 4.2.2 的六组分门别类）。

**需要观察的现象**：
- `--quant_target` 的帮助里赫然写着 `Only support [mlp, moe, attn-linear, attn-cache]`；
- `--quant_dtype`、`--eval_mode`、`--granularity` 都有明确的 `choices` 限定。

**预期结果**：你得到一张与 4.2.2 表格一致的、基于真实源码的参数清单。这份清单就是你日后写量化命令的「字典」。

**如果无法确定运行结果**：本实践能否跑通取决于环境是否已安装 `amct_pytorch` 及其依赖；若未安装，请采用源码阅读方式，**结果以源码为准**，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`--quant_target` 用的是 `nargs="+"`，而 `--algos` 用的是 `nargs="*"`。两者有什么区别？为什么 `--quant_target` 不能用 `*`？

> **答案**：`nargs="+"` 要求至少提供一个值，`nargs="*"` 允许零个值（得到空列表）。`--quant_target` 用 `+` 是合理的——`ptq`/`extract_ptq_data` 阶段必须明确告诉系统要量化哪一块，不能为空；而 `--algos` 用 `*` 是因为某些流程（如纯 `eval`）确实可以不指定任何算法。

**练习 2**：为什么不把训练超参（`--base_lr`、`--epochs` 等）单独做成 `ptq` 命令的专属参数？

> **答案**：因为 AMCT 四条命令共用一份 `parser_gen`，参数空间是统一的。虽然只有 `ptq` 真正会用 `--base_lr`/`--epochs` 去优化，但让 `eval`/`deploy` 也「认识」这些参数（即使忽略它们），可以避免用户在不同命令间切换时反复改命令行风格。代价是参数表偏长，所以本讲建议你按用途分组记忆。

---

### 4.3 quant_target 四个取值的含义

#### 4.3.1 概念说明

`--quant_target` 回答的是「这一轮要量化模型里的哪一类子模块」。它不是某一层、某个矩阵的名字，而是一**类**模块的统称。AMCT 把一个大模型里所有可量化的对象归并为四类：

| 取值 | 含义 | 典型被量化的权重 |
| --- | --- | --- |
| `mlp` | 稠密模型的 MLP/FFN 子模块（每个 decoder layer 里一个） | `gate_proj` / `up_proj` / `down_proj` |
| `moe` | MoE（混合专家）模型的专家 MLP，分 `routed`（路由专家）与 `shared`（共享专家） | 各专家的 `gate/up/down_proj` |
| `attn-linear` | 注意力里的线性投影 | `q_proj` / `k_proj` / `v_proj` / `o_proj`（或 MLA 的 `comp_wgate` 等） |
| `attn-cache` | 注意力的 KV cache（推理时缓存的 k/v 张量，不是权重） | cache 中的 `k` / `v`（甚至 `q`/`p`） |

> 这四类与 `bit_config` yaml 里的分组是对应的：`_LINEAR_GROUPS = ("attn-linear", "mlp", "moe")` 覆盖前三类（都是 Linear 权重），`_CACHE_GROUP = "attn-cache"` 单独覆盖 cache（见 [bit_policy.py:L24-L25](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/bit_policy.py#L24-L25)）。

#### 4.3.2 核心流程

`--quant_target` 的使用有一条强约束（来自 [u1-l4](u1-l4-first-quant-cli.md)）：

```text
extract_ptq_data 阶段: --quant_target mlp   → 录制 mlp 的校准数据
ptq 阶段:              --quant_target mlp   → 必须一致，否则数据对不上
```

**注意**：`args.py` 本身**不做跨命令的一致性校验**——每条命令各自独立调用 `parser_gen`，彼此看不到对方的参数。一致性是**使用约定**，靠用户保证；如果两阶段 `quant_target` 不一致，会在 `ptq` 阶段读校准数据时报错（数据形状/数量对不上）。

#### 4.3.3 源码精读

`--quant_target` 的定义见 [args.py:L61-L67](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py#L61-L67)：

```python
parser.add_argument(
    '--quant_target',
    nargs="+",
    default=[],
    choices=["mlp", "moe", "attn-linear", "attn-cache"],
    help='Only support [mlp, moe, attn-linear, attn-cache]',
)
```

`choices` 限定只允许这四个值，传 `--quant_target ffn` 之类会直接被 argparse 拒绝。`nargs="+"` 让你可以一次传多个（例如 eval 时同时看 `mlp attn-linear` 的精度，见 [examples/eval.sh:L38](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/examples/eval.sh#L38)）。

`quant_target` 与 `bit_config` 的对应关系体现在 `BitPolicy` 里：`_LINEAR_GROUPS` 和 `_CACHE_GROUP` 这两个常量把 yaml 的顶层分组名写死成了与 `quant_target` 一致的字符串（见 [bit_policy.py:L24-L25](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/bit_policy.py#L24-L25)）。这就保证了「你在命令行指定量化哪类模块」与「你在 yaml 里为哪类模块配位宽」用的是同一套词汇表。

一个真实例子来自 [examples/ptq_single_npu.sh:L19-L29](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/examples/ptq_single_npu.sh#L19-L29)：

```bash
python -m amct_pytorch.ptq \
  --model /path/to/model \
  --quant_target mlp \
  --quant_dtype int \
  --bit_config amct_pytorch/configs/w8a8.yaml \
  --algos lwc lac \
  ...
```

这里 `--quant_target mlp` 表示本轮只量化稠密 MLP，配 `w8a8.yaml`（全局 W8A8），用 `lwc lac` 两个算法。

#### 4.3.4 代码实践

**实践目标**：理解「`quant_target` 是分类名，不是层名」，并验证它与 yaml 分组的对应。

**操作步骤**：

1. 打开 [amct_pytorch/configs/example_w4a8.yaml](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/configs/example_w4a8.yaml)。
2. 找到 `attn-linear`、`mlp`、`moe`、`attn-cache` 四个顶层分组。
3. 对照下表，把每个分组里的叶子键（如 `o_proj`、`down_proj`）与 4.3.1 表格里的「典型被量化的权重」一一对应。

**需要观察的现象**：
- `moe` 分组下有 `routed` 和 `shared` 两个子块（对应路由专家与共享专家）；
- `attn-cache` 分组下是 `k: 8` / `v: 8` 这种单值（cache 只有「位宽」概念，不区分 w/a）；
- `attn-linear` / `mlp` 分组下的叶子键都是成对的 `{ w_bits: .., a_bits: .. }`（权重位宽 + 激活位宽）。

**预期结果**：你会确认 `--quant_target` 的四个取值与 `bit_config` yaml 的四个顶层分组是一一对应的词汇表。

**待本地验证**：如果你想真正加载 yaml 看解析结果，可在装好依赖后运行 `BitPolicy.from_yaml("amct_pytorch/configs/example_w4a8.yaml").summary()` 打印（详见综合实践）。

#### 4.3.5 小练习与答案

**练习 1**：`attn-cache` 量化的是权重还是激活？为什么它的 yaml 条目是单值（`k: 8`）而不是成对的 `{w_bits, a_bits}`？

> **答案**：`attn-cache` 量化的既不是静态权重也不是普通激活，而是推理时**动态生成并缓存下来的 KV 张量**（KV cache）。它只有一个「用多少 bit 存」的维度，没有「权重位宽 vs 激活位宽」之分，所以 yaml 里用单值。这也是 `BitPolicy` 把它单独归为 `_CACHE_GROUP`、并用 `has_quant_cache()` 单独判断的原因。

**练习 2**：用户在 `extract_ptq_data` 用了 `--quant_target mlp`，在 `ptq` 改用 `--quant_target moe`。`args.py` 会在解析阶段报错吗？

> **答案**：不会。`args.py` 只校验 `quant_target` 是否在 `choices` 列表里，`moe` 是合法值，所以解析通过。问题会在 `ptq` 运行时才暴露——因为它读到的校准数据是给 `mlp` 录的，与 `moe` 对不上。这说明跨命令一致性是使用约定，不是参数解析层的强约束。

---

### 4.4 BitPolicy 初始化与 _validate_eval_mode 校验

#### 4.4.1 概念说明

`--bit_config` 只是一个 yaml 文件路径，是「死」的字符串。真正在程序里被使用的，是被解析后的 `args.bit_policy`——一个 `BitPolicy` 对象。`parser_gen` 在 `parse_args()` 之后做的第一件后处理，就是把这个字符串变成对象。

之后，**仅当 `command == "eval"`** 时，会再做一道 `_validate_eval_mode(args)` 校验。这道校验的目的是：**防止用户在「纯基线评测」模式下，给出一个「要求量化」的位宽配置**，从而产生自相矛盾的命令。

#### 4.4.2 核心流程

两件事的触发时机：

```text
parser_gen(command):
    1. parser.parse_args()                      # 得到 args
    2. if args.bit_config:                      # 初始化 bit_policy
           args.bit_policy = BitPolicy.from_yaml(args.bit_config)
       else:
           args.bit_policy = BitPolicy()         # 空配置 -> 全部 16-bit
    3. if command == "eval":                    # 仅 eval 命令做校验
           _validate_eval_mode(args)
    4. return args
```

`_validate_eval_mode` 的判定逻辑（结合 [bit_policy.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/bit_policy.py) 的两个查询方法）可以写成：

\[
\text{报错} \iff (\text{eval\_mode} = \text{bf16}) \land \big(\text{has\_quant\_linear()} \lor \text{has\_quant\_cache()}\big)
\]

其中：

- `has_quant_linear()` 为真 ⇔ 顶层 `w_bits`/`a_bits` 至少有一个 <16，**或** `attn-linear`/`mlp`/`moe` 任一分组内存在 <16 的条目（见 [bit_policy.py:L83-L90](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/bit_policy.py#L83-L90)）。
- `has_quant_cache()` 为真 ⇔ `attn-cache` 分组里存在 <16 的整数值（见 [bit_policy.py:L79-L81](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/bit_policy.py#L79-L81)）。

#### 4.4.3 源码精读

**bit_policy 初始化**见 [args.py:L150-L153](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py#L150-L153)：

```python
if args.bit_config:
    args.bit_policy = BitPolicy.from_yaml(args.bit_config)
else:
    args.bit_policy = BitPolicy()
```

- 给了 `--bit_config`：用 `BitPolicy.from_yaml(path)` 读 yaml、校验、构造对象（见 [bit_policy.py:L64-L73](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/bit_policy.py#L64-L73)）。
- 没给：`BitPolicy()` 用空字典构造，`w_bits`/`a_bits` 都默认 16（见 [bit_policy.py:L49-L59](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/bit_policy.py#L49-L59)），即「什么都不量化」。这也正是 [bf16.yaml](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/configs/bf16.yaml) 的语义——它的注释写明「Empty config -> everything stays at 16 bit」。

**eval 校验**见 [args.py:L25-L33](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py#L25-L33)：

```python
def _validate_eval_mode(args):
    if args.eval_mode != "bf16":
        return
    policy = args.bit_policy
    if policy.has_quant_linear() or policy.has_quant_cache():
        raise ValueError(
            "eval_mode=bf16 requires a bit_config with no <16-bit entries.\n"
            f"{policy.summary()}"
        )
```

而它只在 `command == "eval"` 时被调用，见 [args.py:L155-L156](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py#L155-L156)：

```python
if command == "eval":
    _validate_eval_mode(args)
```

**为什么 `eval_mode=bf16` 时禁止 <16-bit 条目？** 这要结合 `--eval_mode` 的 help 文本来理解（见 [args.py:L79-L86](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py#L79-L86)）：

> `bf16 uses the original model path; quant rebuilds quant modules and toggles quantizers by bit-widths.`

含义是：

- `eval_mode=bf16`：评测的是**原始浮点模型**（bf16 精度），不重建量化模块、不应用任何低比特位宽。它用来测**基线 PPL**。
- `eval_mode=quant`：重建量化模块并按位宽打开量化器，用来测**量化后 PPL**。

于是矛盾就清楚了：如果你声明 `eval_mode=bf16`（我不想量化，只看基线），却又在 `bit_config` 里写了 `w_bits: 8`（我想量化到 8-bit），这两个意图是冲突的。在 `bf16` 模式下，那份低比特配置根本不会被应用，等于被静默忽略——用户很可能以为自己在测 8-bit 精度，实际测的还是 bf16。`_validate_eval_mode` 就是用一个**早期硬错误**把这个隐患挡在解析阶段，避免用户拿到误导性的 PPL 数字。

正确用法只有两种：

| 想测什么 | `--eval_mode` | `--bit_config` |
| --- | --- | --- |
| 纯基线（不量化） | `bf16` | `bf16.yaml`（空）或干脆不传 |
| 量化后精度 | `quant` | 你的低比特 yaml（如 `w8a8.yaml`） |

#### 4.4.4 代码实践

**实践目标**：亲手复现 `_validate_eval_mode` 的「报错 / 不报错」两种情况，从而彻底理解这条校验。

**操作步骤**（源码阅读 + 可选运行）：

1. **阅读** [bit_policy.py:L83-L90](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/bit_policy.py#L83-L90) 的 `has_quant_linear` 与 [L79-L81](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/bit_policy.py#L79-L81) 的 `has_quant_cache`，确认它们扫描的是哪些节点。
2. **（可选，需装好 PyYAML）** 写一段最小脚本验证两种情形：

   ```python
   # 示例代码（非项目原有代码）
   from amct_pytorch.quantization.bit_policy import BitPolicy

   # 情形 A：bf16.yaml 是空配置 -> has_quant_linear() 为 False
   p_bf16 = BitPolicy.from_yaml("amct_pytorch/configs/bf16.yaml")
   print("bf16.yaml:", p_bf16.has_quant_linear(), p_bf16.has_quant_cache())

   # 情形 B：w8a8.yaml 顶层 w_bits=8 -> has_quant_linear() 为 True
   p_w8a8 = BitPolicy.from_yaml("amct_pytorch/configs/w8a8.yaml")
   print("w8a8.yaml:", p_w8a8.has_quant_linear(), p_w8a8.has_quant_cache())
   ```

3. 推断：若把 `p_w8a8` 配上 `eval_mode="bf16"` 喂给 `_validate_eval_mode`，会发生什么？再用源码核对你的推断。

**需要观察的现象**：
- 情形 A：两个查询都返回 `False`（空配置，全部 16-bit）；
- 情形 B：`has_quant_linear()` 返回 `True`（顶层 `w_bits=8 < 16`），`has_quant_cache()` 返回 `False`（w8a8.yaml 没有 `attn-cache` 分组）。

**预期结果**：情形 B 若进入 `_validate_eval_mode`（且 `eval_mode=="bf16"`），会抛出 `ValueError`，错误信息里还附带 `policy.summary()` 打印的整份 yaml，方便用户定位是哪一行 <16。这正好解释了「为什么 eval_mode=bf16 时要求 bit_config 中没有低于 16-bit 的条目」——因为 bf16 模式不应用量化，低比特配置只会造成误导，所以解析阶段直接拒绝。

**待本地验证**：步骤 2 的运行结果取决于本地是否安装了 `amct_pytorch` 与 `PyYAML`；若未安装，请以源码阅读为准推断结果。

#### 4.4.5 小练习与答案

**练习 1**：`eval_mode=quant` 时，`bit_config` 里有 <16-bit 条目会被 `_validate_eval_mode` 拒绝吗？

> **答案**：不会。`_validate_eval_mode` 第一行就是 `if args.eval_mode != "bf16": return`，只有 `bf16` 才往下查。`quant` 模式的本意就是「按位宽打开量化器」，低比特条目是必需的，自然不拦截。

**练习 2**：一个 yaml 顶层是 `w_bits: 16 / a_bits: 16`，但在 `mlp` 分组下写了 `down_proj: { w_bits: 4, a_bits: 8 }`。`has_quant_linear()` 返回什么？配 `eval_mode=bf16` 会报错吗？

> **答案**：返回 `True`，会报错。因为 `has_quant_linear()` 不仅看顶层，还会递归扫描 `attn-linear`/`mlp`/`moe` 三个分组（借助 `_has_lt_16`，见 [bit_policy.py:L191-L198](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/quantization/bit_policy.py#L191-L198)），`mlp.down_proj` 里有 4 和 8，都 <16，所以判定为「存在量化条目」。这说明「只要 yaml 任何角落有低比特」都会触发 bf16 模式的报错，校验是彻底的。

**练习 3**：为什么 `_validate_eval_mode` 只在 `command == "eval"` 时调用，而 `ptq`/`deploy` 不调用？

> **答案**：`ptq` 和 `deploy` 的语义本身就要求低比特配置——`ptq` 是去求量化参数，`deploy` 是去导出量化权重，没有低比特条目反而没法干活。只有 `eval` 存在「bf16 基线 vs quant 量化」两种模式之分，才需要这道防自相矛盾的校验。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「参数审计」小任务。

**任务背景**：你拿到同事写的一条 eval 命令，需要审计它是否合法、各参数默认值是什么：

```bash
python -m amct_pytorch.eval \
  --model /data/qwen3 \
  --granularity block \
  --eval_mode bf16 \
  --bit_config amct_pytorch/configs/w8a8.yaml
```

**要求你完成**：

1. **列参数表**：通读 [args.py:L39-L146](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py#L39-L146)，把命令里**没有出现**的参数（如 `--device`、`--quant_target`、`--quant_dtype`、`--nsamples`、`--seq_len`、`--seed`、`--start_block_idx`、`--end_block_idx` 等）的**默认值**列成一张表。
2. **预测行为**：这条命令会成功运行吗？为什么？（提示：`eval_mode=bf16` + `w8a8.yaml`，结合 4.4 的判定公式。）
3. **给出修复**：若它不合法，给出两种不同的修复方案，并说明各自对应的评测意图（基线评测 / 量化评测）。
4. **补一个 `quant_target` 说明**：若把 `--eval_mode` 改成 `quant` 并加上 `--quant_target mlp attn-linear`，这两个 target 分别量化模型的哪部分？（结合 4.3 表格。）

**参考答案**：

1. 默认值表（摘自源码）：`--device`=`npu:0`、`--quant_target`=`[]`（空）、`--quant_dtype`=`""`（空）、`--nsamples`=`128`、`--seq_len`=`4096`、`--seed`=`0`、`--start_block_idx`=`0`、`--end_block_idx`=`61`、`--algos`=`[]`、`--output_dir`=`./outputs`。
2. **不会成功**。`eval_mode=bf16`，而 `w8a8.yaml` 顶层 `w_bits=8 / a_bits=8`，`has_quant_linear()` 为 `True`，触发 `_validate_eval_mode` 抛 `ValueError`。
3. 两种修复：
   - **想测基线**：把 `--bit_config` 换成 `amct_pytorch/configs/bf16.yaml`（或直接删掉 `--bit_config`，让 `BitPolicy()` 取空默认），`eval_mode` 保持 `bf16`。
   - **想测量化精度**：把 `--eval_mode` 改成 `quant`，并补上 `--quant_target`（如 `mlp attn-linear`）和 `--quant_dtype`（如 `int`），保留 `w8a8.yaml`。
4. `mlp` 量化稠密 MLP 的 `gate/up/down_proj`；`attn-linear` 量化注意力的 `q/k/v/o_proj`（或 MLA 的对应投影）。

## 6. 本讲小结

- AMCT 四条命令共用一份参数定义中心 `parser_gen(command)`，统一三段式：**解析参数 → 构造 Workflow → `run()`**；根目录的 `amct_pytorch/<cmd>.py` 是延迟导入的薄壳，只为加速启动。
- `command` 参数是「命令身份」钩子：只有 `command == "eval"` 才会触发 `_validate_eval_mode` 校验。
- `--quant_target` 的四个取值 `mlp`/`moe`/`attn-linear`/`attn-cache` 是**分类名**，与 `bit_config` yaml 的顶层分组一一对应；`extract_ptq_data` 与 `ptq` 两阶段的 `quant_target` 必须一致（使用约定，非解析期强约束）。
- `args.bit_policy` 由 `--bit_config` 经 `BitPolicy.from_yaml` 初始化；不传则为空策略（全 16-bit）。
- `_validate_eval_mode` 的本质是防自相矛盾：`eval_mode=bf16` 表示「评测原始浮点模型」，若此时 yaml 任何角落出现 <16-bit 条目（无论顶层还是 `mlp`/`moe`/`attn-linear`/`attn-cache` 分组内），都会在解析阶段被拒绝，避免用户拿到误导性的 PPL。

## 7. 下一步学习建议

本讲只讲到「参数解析完、构造好 Workflow、准备 `run()`」就停了。接下来应该：

- **[u3-l2 工作流编排骨架与运行模式](u3-l2-workflow-skeleton.md)**：进入 `LlmEvalWorkflow` / `LlmPtqWorkflow` 等类的 `setup()` / `run()`，看 `args` 是怎么被 Workflow 消费的，以及 `granularity`（block/model/tensor）如何决定走哪条路径。
- **[u3-l4 BitPolicy 位宽配置与 yaml 模板](u3-l4-bit-policy-config.md)**：如果想更深入理解 `BitPolicy.linear_bits` 的「从最具体叶子到最粗组逐级回退」解析规则，以及 `example_w4a8.yaml` 里 per-layer override 的写法。
- 暂时不想往下游走的话，可以回到 [u1-l4](u1-l4-first-quant-cli.md) 用本讲新学的参数知识，重新审视那四条 examples 脚本，你会发现每个参数现在都有据可查了。
