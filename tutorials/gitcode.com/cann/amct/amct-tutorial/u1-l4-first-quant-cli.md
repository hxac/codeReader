# 一站式量化初体验：四条 CLI 命令

## 1. 本讲目标

本讲是「能跑起来」的第一课。学完之后，你应该能够：

- 说清楚 AMCT 把一次完整的大模型（LLM）量化拆成了**哪四个阶段**，以及它们**为什么必须按固定顺序串联**。
- 看懂仓库里 `examples/` 目录下那几个 `.sh` 脚本每一行在做什么。
- 理解 `python -m amct_pytorch.ptq` 这种调用方式背后，「顶层薄壳」与「真正干活的 CLI」是怎么分层的。
- 拿到一条真实的 `ptq` 命令，能逐个解释 `--model` / `--quant_target` / `--quant_dtype` / `--bit_config` / `--algos` 这些关键参数的含义。

本讲**不深入算法和量化模块的实现**（那是第 6、7 单元的事），只解决「命令长什么样、怎么串起来」这个工程入门问题。

## 2. 前置知识

在开始前，请确认你已经具备以下认知（它们来自前置讲义 u1-l1 ~ u1-l3）：

- **AMCT 是什么**：昇腾 NPU 原生的模型量化压缩工具，链路是「浮点模型 → AMCT 量化 → 昇腾 NPU 低比特推理」，AMCT 只负责中间的压缩环节。
- **PTQ（训练后量化）**：不需要重新训练整个模型，只用一小批校准数据就能把权重/激活从高比特压到低比特。
- **仓库的两条主线**：`amct_pytorch`（核心量化源码）里既有「LLM PTQ 主流程」，也有「Classic 经典图压缩」。本讲的四条命令都属于 **LLM PTQ 主流程**。
- **目录结构**：`examples/` 存放端到端调用样例脚本；`amct_pytorch/cli/llm/` 存放命令行的真正入口；`amct_pytorch/` 根目录下有几个「薄壳」模块文件。

还需要两个通俗概念：

| 术语 | 一句话解释 |
|------|-----------|
| **PPL（Perplexity，困惑度）** | 衡量语言模型「有多惊讶」的指标，越低越好。量化前后各测一次 PPL，就能知道精度掉了多少。 |
| **Block（块）** | Transformer 里一个完整的 decoder layer（含 self-attention + MLP）。AMCT 的 LLM 量化默认「一层一层」处理，这种粒度叫 `granularity=block`。 |
| **校准数据（calibration data）** | 一小批用来「教」量化器了解激活值分布的样本（AMCT 用 Pileval 数据集），不是用来训练模型的。 |

## 3. 本讲源码地图

本讲涉及的文件很少，分三类：

| 文件 | 作用 |
|------|------|
| `amct_pytorch/eval.py`、`amct_pytorch/extract_ptq_data.py`、`amct_pytorch/ptq.py`、`amct_pytorch/deploy.py` | 四个**顶层薄壳模块**，让用户能用 `python -m amct_pytorch.<cmd>` 启动对应阶段。 |
| `amct_pytorch/cli/llm/eval.py`、`.../ptq.py`、`.../extract_ptq_data.py`、`.../deploy.py` | 真正干活的 **CLI 入口**：解析参数 → 构造 Workflow → 执行。 |
| `amct_pytorch/cli/llm/args.py` | 四条命令**共用**的参数定义（`parser_gen`）。 |
| `examples/eval.sh`、`examples/extract_ptq_data.sh`、`examples/ptq_single_npu.sh`、`examples/deploy.sh`、`examples/ptq_multi_npu.sh` | 端到端样例脚本，演示四条命令的真实写法。 |
| `docs/zh/AMCT_Pytorch_LLM.md` | 官方对这四条命令的完整说明文档，是本讲最重要的参考资料。 |

记忆口诀：**「根目录薄壳 → cli/llm 真入口 → workflows 执行」** 三层，外加 `examples/` 给你抄作业。

## 4. 核心概念与源码讲解

### 4.1 四阶段流水线总览：一次完整的 LLM 量化

#### 4.1.1 概念说明

AMCT 把一次完整的 LLM 训练后量化，拆成了**四条独立的命令**。它们不是可有可无的选项，而是**一条必须按顺序走的流水线**：

```
① eval（评估）  →  ② extract_ptq_data（提取校准数据）  →  ③ ptq（量化优化）  →  ④ deploy（部署导出）
```

为什么要拆成四步，而不是一个命令一把梭？因为每一步的**计算代价**和**产物**完全不同：

- **eval**：只测量、不修改模型。用来在量化前后各跑一次，确认「精度掉了多少」。
- **extract_ptq_data**：用原始浮点模型跑一遍校准样本，把中间层的输入激活「录下来」。这一步耗时但不改权重。
- **ptq**：拿上一步录下来的数据，逐层优化量化参数（最吃算力和时间的一步）。
- **deploy**：把优化好的量化参数「烘培」进权重文件，产出可以直接部署的低比特模型。

拆分的好处是：**最贵的 ptq 可以单独重跑、可以多卡并行、可以断点续跑**，而不用每次都重做前面的数据准备。

#### 4.1.2 核心流程

四阶段的「谁产出什么、谁消费什么」数据流如下：

```
┌─────────┐   不产出文件，只在 logs/ 写 PPL
│  eval   │   （量化前后各跑一次，对比精度）
└─────────┘

┌─────────────────────┐   产出：block/unit 的输入、kwargs
│ extract_ptq_data    │   位置：--data_dir 指定的目录
└─────────────────────┘
          │  （data_dir 在下一步被读取）
          ▼
┌─────────────────────┐   消费：--data_dir 的校准数据
│        ptq          │   产出：layer_*.pt 量化参数
└─────────────────────┘   位置：{output_dir}/ptq_params/{model_name}/{quant_target}/
          │  （param_dir 在下一步被读取）
          ▼
┌─────────────────────┐   消费：--moe_mlp_param_dir 等 PTQ 参数目录
│       deploy        │   产出：layer_*.safetensors + rest_*.safetensors
└─────────────────────┘         + 更新 model.safetensors.index.json / config.json
```

这里有三个**强约束**（来自 [docs/zh/AMCT_Pytorch_LLM.md](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/AMCT_Pytorch_LLM.md) 第 4 章「常见注意事项」）：

1. `extract_ptq_data` 和 `ptq` 的 `--quant_target` **必须一致**，且每次只能指定**一个**目标（如只写 `mlp`，不能同时写 `mlp attn-linear`）。
2. 两者的 `--granularity` 也**必须一致**（当前都只能用 `block`）。
3. `deploy` 读取的参数目录，必须对应 `ptq` 已经训练完成的 `quant_target`。

> 提示：`eval` 是个「旁观者」，它不参与 data_dir → param_dir 的数据链，可以独立在任何时刻运行。

#### 4.1.3 源码精读

这套四阶段划分在官方文档 [docs/zh/AMCT_Pytorch_LLM.md:11-18](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/AMCT_Pytorch_LLM.md) 的「能力概览」表里有明确说明：CLI 入口 `amct_pytorch/cli/llm/` 提供 `eval`、`extract_ptq_data`、`ptq`、`deploy` 四类命令行入口。

文档第 47 行起的第 2 章「CLI入口及对应实例化操作」则逐一描述了每条命令的功能特性、输出和约束。这是阅读源码前最好的「地图」。

#### 4.1.4 代码实践

**实践目标**：建立四阶段数据流的直观印象。

**操作步骤**：

1. 打开 [docs/zh/AMCT_Pytorch_LLM.md](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/AMCT_Pytorch_LLM.md)，定位到第 2 章的四个小节（2.1 `eval`、2.2 `extract_ptq_data`、2.3 `ptq`、2.4 `deploy`）。
2. 在每小节的「**输出**」一行，记下它产出的东西是什么、放在哪个目录。

**需要观察的现象**：你会发现四条命令的输出目录含义各不相同——`eval` 写日志、`extract_ptq_data` 写 data_dir、`ptq` 写 param 目录、`deploy` 写 safetensors。

**预期结果**：你能用自己的话回答「为什么 `extract_ptq_data` 的 `--data_dir` 和 `ptq` 的 `--data_dir` 必须指向同一个目录」。答案：前者往里写校准数据，后者从里面读，串起来才是一条完整的链。

#### 4.1.5 小练习与答案

**练习 1**：如果我只想要一个部署用的量化模型，能不能跳过 `eval` 直接做 `extract_ptq_data → ptq → deploy`？

> **答案**：可以。`eval` 只负责测 PPL，不产出下游需要的文件，是可选的精度验证步骤。生产环境通常先跑 `eval` 拿到基线 PPL，量化后再跑一次 `eval` 看掉点幅度，但它在数据链上不是必需的。

**练习 2**：为什么 `ptq` 不能合并进 `extract_ptq_data`？

> **答案**：两者代价和目的不同。`extract` 是「用浮点模型录数据」，不改权重、可复用；`ptq` 是「用录到的数据做逐层优化训练」，最耗时且常需多卡并行（见 `examples/ptq_multi_npu.sh`）和断点续跑。拆开才能让贵的 `ptq` 单独重跑而不重做数据准备。

---

### 4.2 顶层 `-m` 模块入口：薄壳分发设计

#### 4.2.1 概念说明

当你看到 `python -m amct_pytorch.ptq` 这条命令时，`-m` 是 Python 的标准用法，意思是「把 `amct_pytorch/ptq.py` 当作脚本运行」。这个 `ptq.py` 就是一个**顶层薄壳模块**（thin wrapper）。

「薄壳」是指：这个文件**自己几乎不做事**，只负责把调用转发给真正干活的 `amct_pytorch/cli/llm/ptq.py`。AMCT 为四个阶段各做了一个这样的薄壳：`eval.py`、`extract_ptq_data.py`、`ptq.py`、`deploy.py`。

为什么要这样设计？因为：

- **对外提供稳定的命令名**：用户永远用 `python -m amct_pytorch.<cmd>`，不用关心内部实现搬到哪里。
- **延迟导入**：真正的实现（含 PyTorch、torch_npu 等重依赖）只在 `main()` 内部才 import，避免一执行 `-m` 就触发整包加载。
- **职责分层**：薄壳管「叫什么名字」，`cli/llm/` 管「怎么解析参数、跑哪个 Workflow」。

#### 4.2.2 核心流程

一次 `python -m amct_pytorch.ptq ...` 的调用链是：

```
python -m amct_pytorch.ptq          # 用户命令
   └─ amct_pytorch/ptq.py:main()    # 薄壳：把调用转发
        └─ amct_pytorch/cli/llm/ptq.py:main()   # 真入口
             ├─ parser_gen(command="ptq")        # 解析参数
             ├─ LlmPtqWorkflow(args)             # 构造工作流
             └─ workflow.run()                   # 执行
```

四个阶段的真入口都遵循同一个套路：**`parser_gen(command=...)` → 构造 Workflow → `run()`**。

#### 4.2.3 源码精读

先看薄壳 `amct_pytorch/ptq.py`，它短到可以全文贴出关键部分：

[amct_pytorch/ptq.py:18-28](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/ptq.py) —— 文档字符串写明它就是给 `python -m amct_pytorch.ptq` 用的；`main()` 内部 `from ... import ... cli_main` 再调用，是典型的延迟导入。

```python
"""Run LLM PTQ with ``python -m amct_pytorch.ptq``."""

def main():
    from amct_pytorch.cli.llm.ptq import main as cli_main
    return cli_main()

if __name__ == "__main__":
    main()
```

其余三个薄壳完全同构：[amct_pytorch/eval.py:21-24](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/eval.py)、[amct_pytorch/extract_ptq_data.py:21-24](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/extract_ptq_data.py)、[amct_pytorch/deploy.py:21-24](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/deploy.py)，都是把 `cli_main` 换成各自对应的 `cli/llm/<cmd>.main`。

再看真入口 [amct_pytorch/cli/llm/ptq.py:18-25](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/ptq.py) —— 这才是真正「解析参数 + 跑 Workflow」的地方：

```python
from amct_pytorch.cli.llm.args import parser_gen
from amct_pytorch.workflows.llm_ptq import LlmPtqWorkflow

def main():
    args = parser_gen(command="ptq")
    workflow = LlmPtqWorkflow(args)
    workflow.run()
```

其余三个真入口同构，只是换了 `command` 和 Workflow 类：

- [amct_pytorch/cli/llm/eval.py:22-25](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/eval.py) → `LlmEvalWorkflow`
- [amct_pytorch/cli/llm/extract_ptq_data.py:22-25](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/extract_ptq_data.py) → `LlmExtractPtqDataWorkflow`
- [amct_pytorch/cli/llm/deploy.py:22-25](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/deploy.py) → `LlmDeployWorkflow`

> 注意一个细节：`examples/deploy.sh` 里写的是 `python amct_pytorch/cli/llm/deploy.py ...`（直接跑真入口脚本），而其他三个 `.sh` 用的是规范的 `python -m amct_pytorch.<cmd>`（走薄壳）。两种写法都能跑通，但**推荐用 `-m` 形式**，它更稳定、不依赖你在仓库里的相对路径。

#### 4.2.4 代码实践

**实践目标**：亲手验证「薄壳 → 真入口」的转发关系。

**操作步骤**：

1. 打开四个薄壳文件（`amct_pytorch/{eval,extract_ptq_data,ptq,deploy}.py`）。
2. 对比它们的 `main()` 函数体，确认唯一的差别就是 `from amct_pytorch.cli.llm.<cmd> import main as cli_main` 这一行里的 `<cmd>` 不同。

**需要观察的现象**：四个薄壳的结构几乎一字不差，只是转发的目标模块名换了一下。

**预期结果**：你能总结出「AMCT 的四条 `-m` 命令共享同一个薄壳模板」，并理解这种设计让新增一条命令变得很容易——只要在 `cli/llm/` 加一个真入口，再在根目录放一个同构薄壳即可。

#### 4.2.5 小练习与答案

**练习 1**：薄壳里为什么把 `from amct_pytorch.cli.llm.ptq import main as cli_main` 写在 `main()` 函数体内部，而不是写在文件顶部？

> **答案**：为了延迟（懒）导入。写在顶部会让 `python -m amct_pytorch.ptq --help` 这种只看帮助的调用也触发整条依赖链（含 torch、torch_npu）的加载；写在函数体内，只有真正执行 `main()` 时才加载，加快启动、减少无关报错。

**练习 2**：如果我想新增第五条命令 `calibrate`，最少要改哪些地方？

> **答案**：① 在 `amct_pytorch/cli/llm/` 加一个 `calibrate.py` 真入口（`parser_gen(command="calibrate")` + 对应 Workflow + `run()`）；② 在 `amct_pytorch/` 根目录加一个同构薄壳 `calibrate.py` 转发调用。`args.py` 里若有 `calibrate` 专属参数，再在 `parser_gen` 里按 `command == "calibrate"` 分支补充。

---

### 4.3 四阶段 examples 脚本精读

#### 4.3.1 概念说明

`examples/` 目录下的 `.sh` 脚本是**可抄可改的起点模板**。它们把 `/path/to/model` 之类的路径留空或写成占位符，你只要换成自己的模型路径和数据目录就能跑。本模块逐个拆解这四条命令的真实写法。

#### 4.3.2 核心流程

四条命令的精简骨架（去掉版权头后）：

| 阶段 | 脚本 | 核心调用 |
|------|------|----------|
| ① eval | `examples/eval.sh` | `python -m amct_pytorch.eval --eval_mode bf16` 和 `--eval_mode quant` 两条 |
| ② extract | `examples/extract_ptq_data.sh` | `python -m amct_pytorch.extract_ptq_data --quant_target mlp --data_dir ... --output_dir ./outputs` |
| ③ ptq | `examples/ptq_single_npu.sh` | `python -m amct_pytorch.ptq --quant_target mlp --quant_dtype int --algos lwc lac ...` |
| ④ deploy | `examples/deploy.sh` | `python amct_pytorch/cli/llm/deploy.py --quant_target mlp attn-linear --quant_dtype int ...` |

注意 `eval.sh` 里**有两条命令**——它演示了 eval 的两种用法：先测 BF16 基线，再测量化后的 PPL。

#### 4.3.3 源码精读

**① eval.sh** —— 评估基线与量化精度。

[examples/eval.sh:19-29](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/examples/eval.sh) 先设了 `ASCEND_RT_VISIBLE_DEVICES=0`（指定用 0 号 NPU 卡），然后跑第一条 `--eval_mode bf16` 命令，用 `bf16.yaml`（全 16-bit，不量化）测原始精度：

```bash
python -m amct_pytorch.eval \
  --model /path/to/model --model_name qwen3_5 \
  --device npu:0 --granularity block \
  --eval_mode bf16 \
  --bit_config amct_pytorch/configs/bf16.yaml \
  --seq_len 4096
```

[examples/eval.sh:32-40](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/examples/eval.sh) 紧接着的第二条命令换成 `--eval_mode quant`，用 `w8a8.yaml`（全局 W8A8）测量化后的精度。两者对比就知道 W8A8 掉了多少 PPL。

**② extract_ptq_data.sh** —— 录制校准数据。

[examples/extract_ptq_data.sh:19-27](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/examples/extract_ptq_data.sh)：

```bash
python -m amct_pytorch.extract_ptq_data \
  --model /path/to/model --model_name qwen3_5 \
  --device npu:0 --granularity block \
  --quant_target mlp \
  --data_dir /path/to/data \   # 校准数据「写到这里」
  --output_dir ./outputs \
  --nsamples 128               # 取 128 条 Pileval 样本
```

注意：这里的 `--data_dir` 是**输出目录**，提取出的 block/unit 输入会落盘到这里。

**③ ptq_single_npu.sh** —— 单卡做 PTQ 优化。

[examples/ptq_single_npu.sh:19-29](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/examples/ptq_single_npu.sh)：

```bash
python -m amct_pytorch.ptq \
  --model /path/to/model --model_name qwen3_5 \
  --device npu:0 --granularity block \
  --quant_target mlp \
  --quant_dtype int \
  --data_dir ./data_dirs \     # 「读这里」——必须和 extract 的 --data_dir 一致
  --bit_config amct_pytorch/configs/w8a8.yaml \
  --algos lwc lac \            # 启用 LWC（权重截断）+ LAC（激活截断）两个算法
  --output_dir ./outputs
```

对比 ② 和 ③：`extract` 把数据写到 `--data_dir`，`ptq` 从 `--data_dir` 读——**两者必须指向同一个目录**，这是数据链能串起来的关键。

> 进阶提示：如果模型很大，单卡太慢，可以参考 [examples/ptq_multi_npu.sh:36-66](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/examples/ptq_multi_npu.sh)。它用 bash 把 `NUM_BLOCKS` 个 layer 切成 `NUM_TASKS` 份，每份用 `--start_block_idx` / `--end_block_idx` 指定区间，分发到多张 NPU 卡上并行跑，相邻任务之间还 `sleep $LAUNCH_INTERVAL` 秒避免冷启动挤兑。

**④ deploy.sh** —— 导出部署权重。

[examples/deploy.sh:19-27](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/examples/deploy.sh)：

```bash
python amct_pytorch/cli/llm/deploy.py \   # 注意：这里没用 -m 薄壳，直接跑真入口脚本
  --model /path/to/model --model_name qwen3_5 \
  --device npu:0 --granularity block \
  --quant_target mlp attn-linear \   # deploy 允许一次写多个 target
  --quant_dtype int \
  --bit_config amct_pytorch/configs/w8a8.yaml \
  --output_dir ./deploy_out
```

注意 `deploy.sh` 的几个特点：① 用 `python amct_pytorch/cli/llm/deploy.py` 而非 `python -m amct_pytorch.deploy`（见 4.2.3 的提醒）；② `--quant_target` 写了两个（`mlp attn-linear`），因为 deploy 只是「按已训练好的参数导出」，不像 extract/ptq 那样一次只处理一个目标。

#### 4.3.4 代码实践

**实践目标**：把四条命令的产出与消费关系串成一条线。

**操作步骤**：

1. 打开本讲列出的四个 `examples/*.sh` 脚本。
2. 用下表模板填空（**示例代码**，请自行补全）：

| 阶段 | 命令 | 产出什么 | 给下一阶段用什么 |
|------|------|----------|------------------|
| eval | `python -m amct_pytorch.eval` | 待填 | 不直接喂给下游 |
| extract | ? | 待填 | 待填 |
| ptq | ? | 待填 | 待填 |
| deploy | ? | 待填 | （终点） |

3. 重点标注：`extract` 的 `--data_dir` 和 `ptq` 的 `--data_dir` 是同一个目录吗？`ptq` 的参数目录怎么传给 `deploy`？

**需要观察的现象**：四条命令通过 `data_dir`（数据）和 `param_dir`（参数）两个目录「接力」。

**预期结果**：你产出一张完整的数据流卡片，能说清楚「这一步产出什么、下一步从哪里读」。完整参考答案见本讲 4.1.2 的流程图和官方文档 [docs/zh/AMCT_Pytorch_LLM.md:320-330](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/AMCT_Pytorch_LLM.md)（PTQ 参数目录命名规则 `layer_{layer_idx}_{unit_name}.pt`）。

#### 4.3.5 小练习与答案

**练习 1**：`examples/eval.sh` 里为什么连续跑了两次 `eval`？

> **答案**：第一次 `--eval_mode bf16` 测原始浮点模型的 PPL 作为**基线**；第二次 `--eval_mode quant` 用 `w8a8.yaml` 测**量化后**的 PPL。两次相减（相除）才能看出量化到底掉了多少精度。`eval` 本身不改模型，所以可以放心连跑。

**练习 2**：`examples/ptq_multi_npu.sh` 为什么要 `sleep $LAUNCH_INTERVAL`？

> **答案**：多卡并行时，每个 PTQ 进程启动都要加载模型权重、初始化 NPU，瞬间同时启动 8 个进程会造成显存/IO 尖峰（冷启动挤兑）。错开 `LAUNCH_INTERVAL` 秒启动，让先启动的进程进入稳态后再启动下一个，避免资源争抢导致 OOM 或卡顿。

---

### 4.4 读懂一条 ptq 命令：核心参数体系

#### 4.4.1 概念说明

四条命令的参数全部定义在**同一个函数** `parser_gen` 里（[amct_pytorch/cli/llm/args.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py)），通过 `command` 参数区分是哪条命令在调用。这意味着四个阶段**共享大部分参数**，只是各自关心其中一部分。

本模块带你读懂最具代表性的一条命令——`ptq`——里的关键参数。掌握了这些，其他三条命令的参数也能举一反三。

#### 4.4.2 核心流程

`parser_gen` 的工作分两步：

```
parser_gen(command="ptq")
  ├─ 1. 用 argparse 定义所有参数（--model / --quant_target / --algos / ...）
  ├─ 2. parser.parse_args() 解析命令行
  ├─ 3. 把 --bit_config 的 yaml 路径解析成 BitPolicy 对象（args.bit_policy）
  ├─ 4. 若 command=="eval"，调用 _validate_eval_mode 做校验
  └─ 5. 返回 args
```

#### 4.4.3 源码精读

逐个看 `ptq` 命令里最关键的几个参数：

**`--model` / `--model_name`**：[amct_pytorch/cli/llm/args.py:39-50](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py)。`--model` 是模型权重路径（本地目录），`--model_name` 是 **AMCT 内部适配器名**（如 `qwen3`、`qwen3_5`、`deepseek_v4`），两者**不一定相同**。`--model_name` 决定了走哪个模型适配器，是路由到具体模型实现的关键。

**`--quant_target`**：[amct_pytorch/cli/llm/args.py:61-67](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py)：

```python
parser.add_argument(
    '--quant_target', nargs="+", default=[],
    choices=["mlp", "moe", "attn-linear", "attn-cache"],
    help='Only support [mlp, moe, attn-linear, attn-cache]',
)
```

它指定**量化哪些模块**，四个合法取值：

| 取值 | 含义 |
|------|------|
| `mlp` | 多层感知机（FFN 的 dense 版本） |
| `moe` | 混合专家模型（MoE 版本的 FFN） |
| `attn-linear` | 注意力里的线性层（Q/K/V/O 投影） |
| `attn-cache` | 注意力的 KV cache |

> 强约束：`extract_ptq_data` 和 `ptq` 阶段，`--quant_target` **一次只能填一个**（虽然 `nargs="+"` 允许多个，但运行时会校验）。

**`--quant_dtype`**：[amct_pytorch/cli/llm/args.py:88-94](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py)，取值 `int` / `mxfp` / `hifp`，决定量化后的数据类型（对应第 7 单元讲的三类量化反量化实现）。

**`--bit_config` 与 `bit_policy`**：[amct_pytorch/cli/llm/args.py:135-153](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py)：

```python
parser.add_argument('--bit_config', type=str, default=None, help='Path to a yaml ...')
...
args = parser.parse_args()
if args.bit_config:
    args.bit_policy = BitPolicy.from_yaml(args.bit_config)   # yaml → BitPolicy 对象
else:
    args.bit_policy = BitPolicy()                            # 默认全 16-bit
```

这里把一个**文件路径**（yaml）转换成了内存里的 **`BitPolicy` 对象**，后续所有模块都从这个对象读位宽。yaml 长什么样？以 `w8a8.yaml` 为例（[amct_pytorch/configs/w8a8.yaml:19-20](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/configs/w8a8.yaml)）就是两行：`w_bits: 8` 和 `a_bits: 8`，表示全局权重 8-bit、激活 8-bit。

**`--algos`**：[amct_pytorch/cli/llm/args.py:116-121](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py)，一个列表，填要启用的量化算法名（如 `lwc lac`、`learnable_had`、`flatquant`）。框架会根据每个算法注册时声明的 `target`，自动把它路由到权重、激活或结构上去（路由机制在第 6 单元细讲）。

**`--eval_mode` 与校验**：[amct_pytorch/cli/llm/args.py:79-86](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py) 定义了 `bf16` / `quant` 两种模式；而 [amct_pytorch/cli/llm/args.py:25-33](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py) 的 `_validate_eval_mode` 做了一个反直觉的校验——`eval_mode=bf16` 时，bit_config 里**不允许出现任何低于 16-bit 的配置**，否则直接抛 `ValueError`：

```python
def _validate_eval_mode(args):
    if args.eval_mode != "bf16":
        return
    policy = args.bit_policy
    if policy.has_quant_linear() or policy.has_quant_cache():
        raise ValueError("eval_mode=bf16 requires a bit_config with no <16-bit entries.")
```

这个校验只在 `command == "eval"` 时触发（[第 155-156 行](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py)），逻辑很直白：你说要跑 BF16 基线，却又配了量化位宽，这是自相矛盾，提前报错好过跑出错误结果。

#### 4.4.4 代码实践

**实践目标**：从一条命令反推参数定义。

**操作步骤**：

1. 拿到 `examples/ptq_single_npu.sh` 里的这条命令：

```bash
python -m amct_pytorch.ptq \
  --model /path/to/model --model_name qwen3_5 \
  --granularity block --quant_target mlp \
  --quant_dtype int --bit_config amct_pytorch/configs/w8a8.yaml \
  --algos lwc lac --data_dir ./data_dirs --output_dir ./outputs
```

2. 在 [amct_pytorch/cli/llm/args.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py) 里逐个找出 `--model`、`--granularity`、`--quant_target`、`--quant_dtype`、`--bit_config`、`--algos`、`--data_dir`、`--output_dir` 的定义行，记下它们的**默认值**。
3. 思考：这条命令里哪些参数用了默认值（没显式写）？比如 `--device`、`--nsamples`、`--epochs`、`--start_block_idx`、`--end_block_idx`。

**需要观察的现象**：你会发现命令里没写的参数都有默认值兜底，例如 `--device` 默认 `npu:0`、`--epochs` 默认 `15`、`--end_block_idx` 默认 `61`。

**预期结果**：你能解释「为什么这条 ptq 命令没写 `--device` 也能跑」——因为默认值是 `npu:0`。同时理解 `--quant_target mlp` 限定了本次只量化 MLP，`--algos lwc lac` 启用了两个可学习截断算法。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `--model` 和 `--model_name` 是两个独立的参数？

> **答案**：`--model` 是**文件系统路径**（模型权重在哪），`--model_name` 是 **AMCT 适配器名**（用哪套模型结构代码去解析它）。同一种结构可能存放在不同路径，所以两者必须分开。例如模型放在 `/data/my_qwen3`，但 `--model_name` 仍要写 `qwen3_5` 才能匹配到正确的适配器代码。

**练习 2**：如果我把 `--eval_mode bf16` 和 `--bit_config w8a8.yaml` 同时传给 `eval` 命令，会发生什么？

> **答案**：会直接报错。`parser_gen(command="eval")` 会调用 `_validate_eval_mode`，检测到 `bit_policy` 里有低于 16-bit 的 linear 配置（W8A8），抛出 `ValueError: eval_mode=bf16 requires a bit_config with no <16-bit entries.`。想测 W8A8 必须用 `--eval_mode quant`。

**练习 3**：`--quant_target` 在 `extract_ptq_data`、`ptq`、`deploy` 三个阶段里，哪些能一次写多个？

> **答案**：只有 `deploy` 能一次写多个（如 `--quant_target mlp attn-linear`，因为它只是按已训练参数导出）。`extract_ptq_data` 和 `ptq` 每次只能指定**一个**目标——这是官方文档「常见注意事项」里的强约束，多目标量化需要分目标依次执行。

---

## 5. 综合实践

**任务**：把本讲学的「四阶段串联」和「参数体系」串起来，手工编排一次端到端量化（以 MLP、W8A8、int 为例），并为每条命令写一句话说明。

**操作步骤**：

1. 假设你的模型在 `/data/models/qwen3_5`，AMCT 适配器名是 `qwen3_5`，工作目录是 `./work`。
2. 按顺序写出四条命令（参考 `examples/*.sh`，但自己填路径），并满足这些约束：
   - `extract_ptq_data` 和 `ptq` 的 `--quant_target` 必须一致（都用 `mlp`）。
   - `extract_ptq_data` 的 `--data_dir` 和 `ptq` 的 `--data_dir` 指向同一个目录（如 `./work/calib_data`）。
   - `ptq` 用 `w8a8.yaml` + `int` + `lwc lac`。
   - `deploy` 的 `--moe_mlp_param_dir` 指向 `ptq` 产出的参数目录。
3. 为每条命令写一句话：**「它产出什么、给下一阶段用什么」**。

**预期输出**（**示例代码**，路径按你的环境调整）：

```bash
# ① 评估 BF16 基线（可选，产出 PPL 日志，不喂给下游）
python -m amct_pytorch.eval --model /data/models/qwen3_5 --model_name qwen3_5 \
  --device npu:0 --granularity block --eval_mode bf16 \
  --bit_config amct_pytorch/configs/bf16.yaml --seq_len 4096

# ② 提取校准数据（产出：./work/calib_data 下的 block/unit 输入；喂给 ③）
python -m amct_pytorch.extract_ptq_data --model /data/models/qwen3_5 --model_name qwen3_5 \
  --device npu:0 --granularity block --quant_target mlp \
  --data_dir ./work/calib_data --output_dir ./work --nsamples 128

# ③ PTQ 优化（消费：./work/calib_data；产出：./work 下的 layer_*.pt 参数；喂给 ④）
python -m amct_pytorch.ptq --model /data/models/qwen3_5 --model_name qwen3_5 \
  --device npu:0 --granularity block --quant_target mlp \
  --quant_dtype int --bit_config amct_pytorch/configs/w8a8.yaml \
  --algos lwc lac --data_dir ./work/calib_data --output_dir ./work

# ④ 部署导出（消费：./work/ptq_params/.../mlp；产出：./work/deploy_out 下的 safetensors）
python -m amct_pytorch.deploy --model /data/models/qwen3_5 --model_name qwen3_5 \
  --device npu:0 --granularity block --quant_target mlp \
  --quant_dtype int --bit_config amct_pytorch/configs/w8a8.yaml \
  --moe_mlp_param_dir ./work/ptq_params/qwen3_5/mlp --output_dir ./work/deploy_out
```

> **待本地验证**：上述命令的精确参数目录名（`ptq_params/qwen3_5/mlp`）遵循文档 [docs/zh/AMCT_Pytorch_LLM.md:320-324](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/docs/zh/AMCT_Pytorch_LLM.md) 给出的规则 `{output_dir}/ptq_params/{model_name}/{quant_target}/`，但实际目录是否完全如此，请在本地跑完 ③ 后用 `ls` 确认，再把路径填进 ④。

## 6. 本讲小结

- AMCT 的 LLM 量化是一条**四阶段流水线**：`eval`（评估）→ `extract_ptq_data`（提取校准数据）→ `ptq`（量化优化）→ `deploy`（部署导出），必须按此顺序串联。
- 四个阶段通过**两个目录接力**：`data_dir` 连接 extract↔ptq（数据），`*_param_dir` 连接 ptq↔deploy（参数）。
- 用户侧永远用 `python -m amct_pytorch.<cmd>`；它先命中**根目录薄壳**（延迟导入），再转发给 `cli/llm/<cmd>.py` 的真入口（`parser_gen` → Workflow → `run()`）。
- 四条命令的参数**共用同一个 `parser_gen`**，靠 `command` 参数区分；关键参数有 `--model/--model_name`、`--quant_target`、`--quant_dtype`、`--bit_config`、`--algos`。
- `extract_ptq_data` 与 `ptq` 的 `--quant_target` 必须一致且每次只填一个；`--granularity` 也必须一致（当前都用 `block`）。
- `eval_mode=bf16` 时 bit_config 不允许出现低于 16-bit 的配置，否则 `_validate_eval_mode` 直接报错。

## 7. 下一步学习建议

本讲只让你「看懂命令、会抄脚本」。接下来建议：

1. **想搞懂参数怎么被解析、命令怎么分发** → 进入第 3 单元 u3-l1《CLI 参数体系与命令分发》，那里会逐行精读 `args.py` 的 `parser_gen`。
2. **想搞懂每条命令内部到底跑了什么** → 进入 u3-l2《Workflow 编排骨架与运行模式》，看 `LlmPtqWorkflow` 等四个 Workflow 的 `setup/run` 结构。
3. **想理解 `--bit_config` 的 yaml 怎么解析成位宽策略** → 进入 u3-l4《BitPolicy 位宽配置与 yaml 模板》。
4. **想跑一个真实的端到端样例** → 参考 `examples/models/qwen3.6/Qwen3.6-Moe.md` 或 `examples/models/deepseekv4/DeepSeekV4-Flash-Walkthrough.md`（一站式平台样例）。
