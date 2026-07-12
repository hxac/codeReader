# 模型评测

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `swift eval` 这条命令在源码层是如何「跑起来」的：从 CLI 路由到 `SwiftEval` 管道，再到 EvalScope 后端执行。
- 区分 ms-swift 里三套容易混淆的「评测」概念：独立评测管道（`swift eval`）、训练中评测钩子（`--eval_use_evalscope`）、训练验证指标（`swift/metrics/`）。
- 掌握 `EvalArguments` 的关键字段（`eval_backend`、`eval_dataset`、`eval_limit`、`eval_url`、`infer_backend` 等）及其对评测流程的影响。
- 理解 `infer_backend`（transformers/vllm/sglang/lmdeploy）为何会显著影响评测速度——评测本质是「先部署、再批量推理」。
- 自己动手用 Native 与 OpenCompass 两种后端跑通一次评测，并读懂评测报告。

## 2. 前置知识

在进入源码前，先用大白话建立三点直觉。

**第一，什么是「评测」。** 训练只告诉你 loss 在降，却不直接告诉你模型到底「懂没懂」。评测（evaluation）就是拿一套标准题库（如 MMLU、GSM8K、ARC）去考模型，把模型输出和标准答案比对，得出一个客观分数（准确率、ROUGE/BLEU 等）。这是判断微调是否真有成效的尺子。

**第二，ms-swift 自己不写题库，也不算分。** 它把这套活外包给了魔搭社区的另一个框架 **EvalScope**。ms-swift 在评测这件事上的角色是「胶水」：把训练/导出好的模型包装成一个 OpenAI 兼容的 HTTP 服务，再把 EvalScope 接到这个服务上去发题、收答案、算分。所以你会看到 `eval.py` 里大量 `from evalscope...` 的导入。

**第三，评测 ≈ 部署 + 批量推理。** 这是本讲最关键的一句。`swift eval` 内部会先用 `run_deploy` 拉起一个临时的 `swift deploy` 服务（就是上一单元 u8-l2 讲的那个），然后让 EvalScope 当客户端去打这个服务。这意味着**你在评测时选的 `--infer_backend`（vllm/sglang/lmdeploy/transformers）直接决定评测快慢**——vllm 的连续批处理（continuous batching）能让 GSM8K 这类需要长生成的题快上好几倍。这也解释了为什么 `EvalArguments` 要继承自 `DeployArguments`：评测参数 = 部署参数 + 一层评测壳。

承接前置讲义：u6-l1/u6-l2 已建立推理引擎抽象与多后端切换；u8-l2 已建立 `swift deploy` 的服务化机制。本讲正是把「推理引擎」与「服务部署」拼装成「评测」这一新场景。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `swift/pipelines/eval/eval.py` | 评测主管道 `SwiftEval` 与入口 `eval_main`，编排「部署→出题→收报告」全流程 |
| `swift/arguments/eval_args.py` | 评测参数 `EvalArguments`，继承 `DeployArguments`，含 `eval_backend`/`eval_dataset`/`eval_url` 等 |
| `swift/metrics/__init__.py` 及 `mapping.py` | 训练验证指标注册表 `eval_metrics_map`（acc/nlg/...），**注意：它不属于 `swift eval` 管道** |
| `swift/pipelines/infer/deploy.py` | `run_deploy` 上下文管理器：临时拉起部署服务并返回 base_url |
| `swift/pipelines/eval/utils.py` | `EvalModel`：训练中评测用的进程内 ModelAPI 适配器（不走 HTTP） |
| `swift/trainers/mixin.py` | 训练中评测钩子 `_evalscope_eval` 与 `create_loss_and_eval_metric` |
| `swift/cli/eval.py` / `swift/cli/main.py` | CLI 入口与路由表 `ROUTE_MAPPING` |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 eval_main 管道**（评测怎么跑起来）、**4.2 EvalScope 后端集成**（三套后端如何接入）、**4.3 评测指标与引擎**（指标从哪来、引擎如何影响速度）。

---

### 4.1 eval_main 管道

#### 4.1.1 概念说明

`swift eval` 命令最终落到一个叫 `SwiftEval` 的管道类。它的设计哲学和 u5-l4 讲过的 `SwiftSft` 完全一致——继承统一的 `SwiftPipeline` 抽象基类，把「解析参数、设种子、计时」的骨架交给基类 `main()`，自己只实现业务 `run()`。这是 ms-swift 全项目统一的「模板方法模式」。

`SwiftEval` 的特殊之处在于：它**不直接持有模型做推理**，而是借助部署服务间接推理。所以 `run()` 的核心是「准备一个可访问的服务地址（base_url）→ 让 EvalScope 去打这个地址 → 收报告」。

#### 4.1.2 核心流程

`swift eval ...` 的端到端执行流程可以画成下面这样：

```
swift eval (shell)
   │
   ▼  setup.py console_scripts → swift.cli.main:cli_main
cli_main 查 ROUTE_MAPPING['eval'] = 'swift.cli.eval'
   │  subprocess 重启 swift/cli/eval.py（eval 不走 torchrun）
   ▼
eval_main()  ──► SwiftEval(args).main()
                        │
                        ▼  SwiftPipeline.main()（基类骨架）
                     run()                        ← SwiftEval 实现
                        │
        ┌───────────────┴────────────────┐
        ▼                                ▼
 args.eval_url 已设?                  未设
   用外部 URL                    run_deploy(args) 拉临时服务
   (nullcontext)                 (spawn 子进程, 轮询就绪, 返回 base_url)
        │                                │
        └───────────────┬────────────────┘
                        ▼
          get_task_cfg(eval_dataset, eval_backend, base_url)
                   │ 按 eval_backend 三选一构造 TaskConfig
                   ▼
          get_task_result(task_cfg)
                   │ run_task(task_cfg)  ← EvalScope 真正出题/打分
                   │ Summarizer.get_report_from_cfg  ← 汇总报告
                   ▼
          写入 result_jsonl，返回 eval_report
```

两个关键判断点：

1. **要不要自己部署？** 由 `eval_url` 决定。设了就用你给的 URL（评测一个已在跑的外部服务）；没设就由 ms-swift 临时拉起一个 `swift deploy`。
2. **用哪个后端出题？** 由 `eval_backend` 决定（Native / OpenCompass / VLMEvalKit），三者构造 `TaskConfig` 的方式不同。

#### 4.1.3 源码精读

先看入口与类声明。CLI 路由表把 `eval` 映射到脚本：

[swift/cli/main.py:L14-L27](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/main.py#L14-L27) —— `ROUTE_MAPPING` 里 `'eval': 'swift.cli.eval'` 这一行决定了 `swift eval` 走哪个脚本（机制见 u1-l4）。

[swift/cli/eval.py:L1-L5](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/eval.py#L1-L5) —— 脚本本身极薄，只是调用 `eval_main()`。注意 `eval` **不在 torchrun 命令清单里**，所以即便你设了 `NPROC_PER_NODE`，eval 也只跑单进程（多卡并行由内部部署服务的 `infer_backend` 自己处理）。

[swift/pipelines/eval/eval.py:L18-L20](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/eval/eval.py#L18-L20) —— `SwiftEval` 声明 `args_class = EvalArguments`，继承 `SwiftPipeline`。

接着是全篇心脏 `run()`：

[swift/pipelines/eval/eval.py:L22-L45](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/eval/eval.py#L22-L45) —— 逐段读：

- **第 25 行**是「要不要自部署」的分叉点：`nullcontext() if args.eval_url else run_deploy(args, return_url=True)`。设了 `eval_url` 就用空上下文（什么都不做），否则用 `run_deploy` 拉临时服务。`with ... as base_url` 把两种情况统一成「拿到一个 base_url」。
- **第 29-31 行**：用 base_url 构造 `TaskConfig`、跑任务、把结果塞进 `eval_report[eval_backend]`。
- **第 33-44 行**：把时间、模型、adapter、结果路径等元信息补进报告；若设了 `result_jsonl` 就追加写盘。

注意第 25 行这个 `with` 语句的精妙：`run_deploy` 是个上下文管理器，进入时拉服务、退出时 `process.terminate()` 杀掉它（见 4.1 末尾）。所以评测一结束，临时部署服务就自动消失，不会占着显存。

[swift/pipelines/eval/eval.py:L47-L65](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/eval/eval.py#L47-L65) —— `get_task_result` 调 EvalScope 的 `run_task` 真正出题打分，再用 `Summarizer.get_report_from_cfg` 取报告。值得细看的是它按后端**分别解析**报告结构：OpenCompass 的报告是「dataset→{metric→score}」的扁平表，要靠 `model_suffix` 列定位本模型分数；VLMEvalKit 的 key 形如 `xxx_dataset_metric`，要 `rsplit('_', 2)` 拆开；Native 直接用原始 reports。这是因为三个后端的报告 schema 各不相同，ms-swift 在此做了一层归一化。

最后是入口函数：

[swift/pipelines/eval/eval.py:L159-L160](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/eval/eval.py#L159-L160) —— `eval_main` 一行实例化 + `main()`，与 `sft_main`/`deploy_main` 完全同构。

再看「自部署」这条支线的 `run_deploy`：

[swift/pipelines/infer/deploy.py:L268-L289](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/deploy.py#L268-L289) —— `run_deploy` 是个 `@contextmanager`：用 `multiprocessing.get_context('spawn')` 起一个**独立子进程**跑 `_deploy_main`（为何用 spawn？因为评测主进程已经持有 GPU/CUDA 上下文，fork 会出问题），然后 `while not is_accessible(port)` 轮询直到服务 `/v1/models` 可达，`yield` 出 base_url，`finally` 里 `terminate()`。这正好印证了「评测 = 部署 + 打分」。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「评测会临时拉起一个部署服务」这件事，而不用真跑完整评测。

**操作步骤**：

1. 准备一个本地小模型路径（或用 `Qwen/Qwen2.5-0.5B-Instruct`）。
2. 在一个终端先手动起一个常驻服务（模拟外部服务）：
   ```bash
   CUDA_VISIBLE_DEVICES=0 swift deploy \
       --model Qwen/Qwen2.5-0.5B-Instruct \
       --infer_backend vllm \
       --port 8000
   ```
3. 在另一个终端，用 `--eval_url` 让评测**复用**这个已存在的服务（这样 `run_deploy` 分支被跳过）：
   ```bash
   swift eval \
       --model Qwen/Qwen2.5-0.5B-Instruct \
       --eval_backend OpenCompass \
       --eval_url http://127.0.0.1:8000/v1 \
       --eval_limit 20 \
       --eval_dataset ARC_c
   ```

**需要观察的现象**：

- 步骤 3 的日志里**不会**出现 `The deployment process has been terminated.`（因为没走 `run_deploy`）。
- 对比：去掉 `--eval_url` 再跑一次，日志末尾会出现 `The deployment process has been terminated.`，且评测期间 `nvidia-smi` 会看到一个临时的部署进程，评测一结束就消失。

**预期结果**：带 `--eval_url` 时评测直接打你起的服务；不带时 ms-swift 自己拉一个临时服务、跑完即杀。两者指标应基本一致（受采样波动影响）。

> 说明：上述命令依赖 `pip install 'ms-swift[eval]'`（含 EvalScope 与可选 OpenCompass 数据），若本地未装请在「待本地验证」前提下观察日志行为即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `eval` 不在 `use_torchrun()` 的多进程命令清单里，却仍能利用多卡加速评测？

**参考答案**：因为评测的多卡并行不在 shell 层（不用 torchrun 启动多个评测进程），而在内部部署服务层——`SwiftDeploy` 会按 `--infer_backend vllm` 等用 vLLM 的 tensor/data parallel 把模型铺到多卡上。评测主进程只有一个，它通过 HTTP 调用这个多卡部署服务。

**练习 2**：如果同时设了 `--eval_url` 和 `--infer_backend vllm`，`infer_backend` 还会生效吗？

**参考答案**：不会影响打分用的服务（那是你外部已经起好的），但 `infer_backend` 仍是 `EvalArguments` 的合法字段（继承自 `DeployArguments`），只是在不走 `run_deploy` 时被忽略。

---

### 4.2 EvalScope 后端集成

#### 4.2.1 概念说明

ms-swift 通过 `eval_backend` 把评测委托给 EvalScope 的三套「出题器」：

| 后端 | 适用场景 | 是否支持结果可视化 | 端点要求 |
|---|---|---|---|
| `Native`（默认） | 纯文本、含自定义 MCQ/QA | 支持 | `/v1` 通用（completions/chat 均可） |
| `OpenCompass` | 纯文本、题库更全（如 ARC_c/cmb/mbpp） | 不支持 | **必须** `/chat/completions` |
| `VLMEvalKit` | **多模态**（图像/视频评测，如 MMBench/RealWorldQA） | 不支持 | **必须** `/chat/completions` |

三者的差异在源码里体现为三个不同的 `get_xxx_task_cfg` 方法，它们各自构造 EvalScope 的 `TaskConfig`。一个反复出现的细节：OpenCompass 与 VLMEvalKit 都把 url 强行补上 `/chat/completions`，因为它们只会调 chat 接口。

#### 4.2.2 核心流程

`get_task_cfg` 是后端分派的入口，逻辑极其简洁：

```
get_task_cfg(dataset, eval_backend, url):
    assert eval_backend in {NATIVE, OPEN_COMPASS, VLM_EVAL_KIT}
    if OPEN_COMPASS:  → get_opencompass_task_cfg  (url 补 /chat/completions)
    elif VLM_EVAL_KIT: → get_vlmeval_task_cfg      (url 补 /chat/completions)
    else:             → get_native_task_cfg         (直接用 /v1)
```

三个方法都返回 EvalScope 的 `TaskConfig`，但填法不同：

- **Native**：`eval_type=EvalType.SERVICE`，直接把 `api_url=url` 当作一个「远端服务型」模型来评测；额外参数走 `extra_eval_args`。
- **OpenCompass**：把模型塞进 `eval_config['models']` 列表，用 `openai_api_base` 指向服务，`is_chat=args.use_chat_template` 决定走 chat 还是 completion。
- **VLMEvalKit**：把模型描述成 `CustomAPIModel`，多模态专用。

#### 4.2.3 源码精读

[swift/pipelines/eval/eval.py:L67-L89](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/eval/eval.py#L67-L89) —— `get_task_cfg` 的 `assert` 把后端限定为三种；OpenCompass 分支里还有一段 `local_dataset` 逻辑：当评测 CMB 这类需要本地数据集的题时，会从 ModelScope 下载 `OpenCompassData-complete` 压缩包并在当前目录建一个 `data` 软链（若 `data` 已存在会报错，避免误覆盖）。

[swift/pipelines/eval/eval.py:L91-L105](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/eval/eval.py#L91-L105) —— `get_native_task_cfg`：关键字段 `model=args.model_suffix`（模型名，用于报告里定位本模型那一列）、`eval_type=EvalType.SERVICE`（声明这是「打服务」型评测）、`api_url=url`、`limit=args.eval_limit`（采样条数）、`eval_batch_size=args.eval_num_proc`（并发客户端数）、`generation_config=args.eval_generation_config`（采样超参）。`**args.extra_eval_args` 把 Native 专属的额外参数透传给 EvalScope。

[swift/pipelines/eval/eval.py:L107-L131](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/eval/eval.py#L107-L131) —— `get_opencompass_task_cfg`：注意第 109 行 `url = f"{url.rstrip('/')}/chat/completions"`，强制 chat 端点；模型以 dict 列表形式给出，`is_chat=args.use_chat_template`。

[swift/pipelines/eval/eval.py:L133-L156](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/eval/eval.py#L133-L156) —— `get_vlmeval_task_cfg`：同样补 `/chat/completions`；模型类型用 `CustomAPIModel`，`**args.eval_generation_config` 直接展开进模型描述。

再看参数侧如何校验数据集合法性：

[swift/arguments/eval_args.py:L99-L115](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/eval_args.py#L99-L115) —— `_init_eval_dataset` 在 `__post_init__` 里被调用：它通过 `list_eval_dataset(eval_backend)` 拿到该后端**支持的全部数据集名**，把你传的 `eval_dataset` 逐个对照（大小写不敏感），不合法就直接 `raise ValueError` 并打印该后端支持列表。这就是为什么写错数据集名时你能拿到一份「可选清单」。

[swift/arguments/eval_args.py:L80-L97](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/eval_args.py#L80-L97) —— `list_eval_dataset`：数据集清单**不是 ms-swift 硬编码的**，而是现查 EvalScope 的三个注册表——Native 查 `BENCHMARK_REGISTRY`、OpenCompass 查 `OpenCompassBackendManager.list_datasets()`、VLMEvalKit 查 `VLMEvalKitBackendManager.list_supported_datasets()`。VLMEvalKit 因依赖 cv2 可能 import 失败，故用 try/except 包裹，仅当你确实选了 VLMEvalKit 才报错。这又一次印证「ms-swift 不维护题库，题库在 EvalScope」。

#### 4.2.4 代码实践

**实践目标**：搞清三套后端支持的数据集差异，并为同一模型分别用 Native 与 OpenCompass 跑同一个题（如 ARC_c）。

**操作步骤**：

1. 先用 Python 交互式查看两套后端各自支持哪些数据集：
   ```python
   from swift.arguments import EvalArguments
   print(EvalArguments.list_eval_dataset('Native'))
   print(EvalArguments.list_eval_dataset('OpenCompass'))
   ```
2. Native 跑 ARC（注意 Native 默认数据集名通常小写，如 `arc`）：
   ```bash
   CUDA_VISIBLE_DEVICES=0 swift eval \
       --model Qwen/Qwen2.5-0.5B-Instruct \
       --eval_backend Native \
       --infer_backend vllm \
       --eval_limit 50 \
       --eval_dataset arc
   ```
3. OpenCompass 跑 ARC_c：
   ```bash
   CUDA_VISIBLE_DEVICES=0 swift eval \
       --model Qwen/Qwen2.5-0.5B-Instruct \
       --eval_backend OpenCompass \
       --infer_backend vllm \
       --eval_limit 50 \
       --eval_dataset ARC_c
   ```

**需要观察的现象**：

- 步骤 1 中两份清单不同：`ARC_c` 在 OpenCompass 列表里，Native 里通常是 `arc`。
- 步骤 2 日志里 Native 的 `api_url` 形如 `http://127.0.0.1:<port>/v1`；步骤 3 OpenCompass 的 url 被补成 `.../v1/chat/completions`。
- 两份报告结构不同：Native 是 EvalScope 原生表格；OpenCompass 经 `get_task_result` 归一化成 `{dataset: {metric: score}}`。

**预期结果**：两次评测都产出准确率分数；OpenCompass 报告在 `eval_result.jsonl` 里以 `{eval_backend: {dataset: {metric: score}}}` 形态保存。

> 若本地未装 `[eval]` 与对应后端数据，以上为「待本地验证」。最小可行验证：仅跑步骤 1 的 Python 查询，确认数据集清单能正常打印。

#### 4.2.5 小练习与答案

**练习 1**：为什么 OpenCompass 和 VLMEvalKit 必须用 `/chat/completions` 端点，而 Native 不强制？

**参考答案**：OpenCompass/VLMEvalKit 这两个后端的客户端实现只走 chat 接口（它们需要把题目组装成对话格式，多模态尤其依赖 chat message 结构）；Native 后端是 EvalScope 自带的，能同时对接 completions 与 chat，故直接用 `/v1` 即可。

**练习 2**：`eval_limit=50` 意味着什么？它和训练里的「采样 500 条」是同一个概念吗？

**参考答案**：`eval_limit` 是从**每个**评测数据集里最多取 N 条样本来评（用于快速验证），与 u4-l1 讲过的数据集 `#N` 采样语法是同一种「按条数抽样」的思想，只是作用域不同——这里作用于评测集而非训练集。`None` 表示用全集。

---

### 4.3 评测指标与引擎

#### 4.3.1 概念说明

这一模块要澄清一个**极易踩坑**的点：`swift eval` 管道用的指标**全部来自 EvalScope**，与 `swift/metrics/` 目录里的 `eval_metrics_map` **没有关系**。`swift/metrics/` 是给训练器在验证步用的（`--eval_metric acc/nlg`），算的是 token 级准确率或 ROUGE/BLEU。两套指标服务两个不同场景：

| 维度 | `swift eval` 管道 | 训练验证（`swift/metrics/`） |
|---|---|---|
| 触发方式 | `swift eval` 独立命令 | 训练时 `--eval_metric` / `--eval_use_evalscope` |
| 指标来源 | EvalScope 内置（按数据集定） | ms-swift 自实现（acc/nlg/infonce/...） |
| 推理方式 | 部署服务批量生成 | 训练器前向 + argmax |
| 典型用途 | 跑标准 benchmark | 训练中监控 |

**引擎如何影响评测速度**：因为评测是「部署 + 批量推理」，`--infer_backend` 选择 vllm/sglang/lmdeploy 这类带连续批处理的引擎，会比 transformers 快很多——尤其对 GSM8K、MBPP 这类需要长生成的题。同时 `--eval_num_proc`（并发客户端数，默认 16）控制同时打多少请求，配合引擎的吞吐能力决定评测墙钟时间。

#### 4.3.2 核心流程

评测分数的产出路径（以 Native 为例）：

```
EvalScope 取数据集 → 按 eval_batch_size 并发向 base_url 发请求
   → 部署服务(infer_backend=vllm) 批量生成
   → EvalScope 用数据集自带的 metric_fn 打分
   → Summarizer 汇总成报告 → SwiftEval.get_task_result 归一化
```

训练验证指标则走完全不同的路径：

```
Trainer.evaluate() → model.forward 得 logits
   → preprocess_logits_for_metrics (argmax)
   → compute_metrics (eval_metrics_map[args.eval_metric])
```

#### 4.3.3 源码精读

先确认 `swift/metrics/` 的真实归属：

[swift/metrics/mapping.py:L10-L18](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/metrics/mapping.py#L10-L18) —— `eval_metrics_map` 收录 `acc/nlg/infonce/paired/reranker`，注释明确写「The metric here will only be called during validation」——即**只在训练验证时被调用**，不在 `swift eval` 管道里。

[swift/trainers/mixin.py:L1046-L1054](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/mixin.py#L1046-L1054) —— `create_loss_and_eval_metric` 证实了这点：`eval_metric = eval_metrics_map[args.eval_metric](args, self)` 把指标挂到 HF Trainer 的 `compute_metrics`/`preprocess_logits_for_metrics` 上。这是训练器验证步的指标来源。

典型的 acc 指标实现：

[swift/metrics/acc.py:L10-L41](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/metrics/acc.py#L10-L41) —— `compute_acc` 把 logits 的 argmax 与 labels 比，支持 `token`/`seq` 两种策略，处理 padding_free（用 `cu_seqlens` 切分）。注意它吃的是 `EvalPrediction`（logits/labels 张量），与 `swift eval`（吃生成文本）完全两码事。

现在回到评测管道的指标侧——它们由 EvalScope 决定，ms-swift 只负责把模型服务化好。`InferStats` 是部署服务侧的吞吐统计（不是评测分数）：

[swift/metrics/utils.py:L46-L70](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/metrics/utils.py#L46-L70) —— `InferStats` 累计 prompt/completion token 数与运行时间，算出 `samples/s`、`tokens/s`。它在 u8-l2 的 `SwiftDeploy` 里被用于周期性打印服务吞吐，评测时能间接反映引擎快慢。

引擎选择对评测的影响最终落在部署服务的构造上。`EvalArguments` 继承链 `EvalArguments → DeployArguments → InferArguments` 让 `--infer_backend` 直接生效：

[swift/arguments/eval_args.py:L14-L15](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/eval_args.py#L14-L15) —— `class EvalArguments(DeployArguments)`，这就是为什么评测命令能直接用 `--infer_backend vllm`——它本质是在配置那个临时部署服务。

[swift/arguments/eval_args.py:L49-L63](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/eval_args.py#L49-L63) —— `EvalArguments` 自身新增字段：`eval_dataset`/`eval_limit`/`eval_dataset_args`/`eval_generation_config`/`eval_output_dir`/`eval_backend`/`local_dataset`/`temperature`/`verbose`/`eval_num_proc`/`extra_eval_args`/`eval_url`。其中 `eval_num_proc=16`（并发客户端）与 `eval_backend` 是影响评测速度的两个直接旋钮。

[swift/arguments/eval_args.py:L125-L130](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/eval_args.py#L125-L130) —— `_init_torch_dtype`：当设了 `eval_url`（评测外部服务）时，本地**不加载模型权重**，只用 `get_matched_model_meta` 拿元信息（因为推理在外部服务里完成）。这是「评测外部服务」省显存的关键。

**补充：训练中评测钩子。** 还有一种评测入口——训练过程中用 EvalScope 评当前 checkpoint。它在 `_evalscope_eval` 里实现，与 `swift eval` 管道共享 EvalScope 但**不走 HTTP**：

[swift/trainers/mixin.py:L1036-L1044](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/mixin.py#L1036-L1044) —— 当 `eval_use_evalscope=True` 且到了评测步，调用 `_evalscope_eval()`。

[swift/trainers/mixin.py:L1143-L1177](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/mixin.py#L1143-L1177) —— `_evalscope_eval`：把当前模型包进一个 `EvalModel`（注册名 `swift_custom`），`eval_type='swift_custom'`，**直接在进程内**用 ms-swift 的 `TransformersEngine` 做批量推理（不开 HTTP 服务），再把分数以 `test_xxx` 写进训练日志。它临时关掉 `template.packing` 与 `padding_free` 以保证生成正确。

[swift/pipelines/eval/utils.py:L54-L102](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/eval/utils.py#L54-L102) —— `EvalModel` 是 EvalScope 的 `ModelAPI` 子类（`@register_model_api('swift_custom')`），内部持有 `TransformersEngine`，把 EvalScope 的请求/配置格式（`convert_request`/`convert_config`）翻译成 ms-swift 的 `InferRequest`/`RequestConfig`，并用一个后台线程做动态组批（`_process_batches`）以提升吞吐。这是「进程内评测」与「HTTP 评测」的根本区别。

> 数学上，准确率类指标可写成：
>
> \[
> \mathrm{Acc}=\frac{1}{|D|}\sum_{(q,a)\in D}\mathbf{1}\!\left[\arg\max_y f_\theta(y\mid q)=a\right]
> \]
>
> 其中 \(D\) 是评测集，\(f_\theta\) 是被评测模型，\(\mathbf{1}[\cdot]\) 是指示函数。这适用于 Native 的 MCQ 类；而 ROUGE/BLEU（见 `compute_rouge_bleu`）则是生成文本与参考答案的 n-gram 重叠度，本质不同。

#### 4.3.4 代码实践

**实践目标**：量化对比 `infer_backend` 对评测速度的影响（这是本讲核心实践，也是规格里要求的任务）。

**操作步骤**：

1. 用 transformers 后端评 ARC_c（慢基线）：
   ```bash
   CUDA_VISIBLE_DEVICES=0 time swift eval \
       --model Qwen/Qwen2.5-0.5B-Instruct \
       --eval_backend OpenCompass \
       --infer_backend transformers \
       --eval_limit 100 \
       --eval_dataset ARC_c
   ```
   记录 `time` 的 real 时间与报告里的准确率。
2. 用 vllm 后端评同一份题（快对照）：
   ```bash
   CUDA_VISIBLE_DEVICES=0 time swift eval \
       --model Qwen/Qwen2.5-0.5B-Instruct \
       --eval_backend OpenCompass \
       --infer_backend vllm \
       --eval_limit 100 \
       --eval_dataset ARC_c
   ```
   同样记录 real 时间与准确率。
3. 翻看 `eval_output/opencompass/` 下的报告，以及 `result/Qwen2.5-0.5B-Instruct/eval_result.jsonl`。

**需要观察的现象**：

- 步骤 2 的墙钟时间应明显短于步骤 1（通常 vllm 比 transformers 快数倍，生成越长差距越大）。
- 两次准确率应**非常接近**（都是贪婪解码、同模型同题；微小差异来自 few-shot 拼接或 token 边界）。
- 评测期间 `nvidia-smi`：vllm 会预占显存做 KV cache；transformers 占用较低但逐条生成。

**预期结果**：得到一张「后端 → 耗时 → 准确率」对照表，验证「换引擎只改速度不改分数」这一结论。

> 若本地无 GPU 或未装 vllm，可退化为「源码阅读型实践」：阅读 [deploy.py 的 run_deploy](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/deploy.py#L268-L289) 与 [SwiftDeploy.get_infer_engine](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/deploy.py#L32-L44)，说明 `--infer_backend vllm` 是如何被评测管道透传到部署服务、再由 vLLM 引擎接管的。该结果「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`eval_metrics_map` 里的 `acc` 指标和 `swift eval --eval_dataset arc` 得到的准确率，是同一个数吗？

**参考答案**：不是。前者是训练验证步的 token 级/序列级准确率，吃 logits（见 `compute_acc`）；后者是 EvalScope 按 ARC 数据集规则（通常比对生成答案里的选项字母）算的题级准确率，吃生成文本。两者口径、输入、触发场景都不同。

**练习 2**：为什么 `--eval_num_proc` 调大不一定能让评测线性提速？

**参考答案**：因为评测吞吐受限于部署服务的推理吞吐。`eval_num_proc` 是客户端并发数，当并发请求超过引擎（尤其 transformers 后端）的处理能力时，请求只在服务端排队，墙钟时间不再下降；而 vllm 这类引擎带连续批处理，能更好吸收高并发，故 `eval_num_proc` 与 `infer_backend` 要搭配调优。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「评测外部已部署模型」的完整闭环：

1. **起服务（复用 u8-l2）**：用 vllm 起一个常驻 OpenAI 兼容服务，挂载一个 LoRA adapter：
   ```bash
   CUDA_VISIBLE_DEVICES=0 swift deploy \
       --model Qwen/Qwen2.5-0.5B-Instruct \
       --adapters ./output/lora/checkpoint-xxx \
       --infer_backend vllm --port 8000
   ```
2. **查可用数据集**：用 `EvalArguments.list_eval_dataset('OpenCompass')` 确认 `ARC_c` 与 `gsm8k` 都在列。
3. **评测外部服务**：开第二个终端，用 `--eval_url` 复用上面的服务，同时评两个数据集：
   ```bash
   swift eval \
       --model Qwen/Qwen2.5-0.5B-Instruct \
       --eval_backend OpenCompass \
       --eval_url http://127.0.0.1:8000/v1 \
       --eval_limit 100 \
       --eval_dataset ARC_c gsm8k \
       --eval_generation_config '{"max_tokens": 512, "temperature": 0}'
   ```
4. **解读报告**：打开 `result/<model>/eval_result.jsonl`，确认其结构为 `{OpenCompass: {ARC_c: {accuracy: ...}, gsm8k: {accuracy: ...}}, model: ..., eval_url: ...}`。

完成后再回答三个问题以自检：(a) 这次评测有没有走 `run_deploy`？为什么？(b) `--infer_backend` 在这次命令里生效了吗？(c) 报告里的指标是 ms-swift 算的还是 EvalScope 算的？

> 参考答案：(a) 没走，因为设了 `eval_url`；(b) 没生效——服务是你自己起的，评测命令里即便带 `--infer_backend` 也被忽略（推理已由外部服务负责）；(c) EvalScope 算的，ms-swift 只负责把模型服务化与归一化报告。

## 6. 本讲小结

- `swift eval` 是一条「模板方法」管道：`SwiftEval.run()` 编排「拿服务地址 → 构造 TaskConfig → run_task → 收报告」。
- 评测的本质是**部署 + 批量推理**：默认用 `run_deploy` 临时拉起一个 `swift deploy` 服务，跑完即杀；设 `--eval_url` 则复用外部服务。
- 三套后端 `eval_backend`（Native/OpenCompass/VLMEvalKit）分别服务纯文本可视化、纯文本全题库、多模态；后两者强制 `/chat/completions` 端点。
- 数据集合法性由 `_init_eval_dataset` 现查 EvalScope 注册表校验，题库不在 ms-swift 里。
- **关键澄清**：`swift/metrics/` 的 `eval_metrics_map`（acc/nlg/...）是训练验证指标，与 `swift eval` 管道无关；评测分数全部来自 EvalScope。
- `--infer_backend` 通过临时部署服务生效，vllm/sglang 等引擎能大幅缩短评测墙钟时间而不改变分数；另有训练中评测钩子 `--eval_use_evalscope` 走进程内 `EvalModel`，不开 HTTP。

## 7. 下一步学习建议

- **横向**：回到 u8-l2，对照 `SwiftDeploy` 与本讲的 `run_deploy`，理解「常驻部署」与「评测用临时部署」的差异；阅读 [SwiftDeploy.get_infer_engine](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/deploy.py#L32-L44) 看 vllm data-parallel 在部署层的校验。
- **纵向（扩展）**：u10-l2 会讲 `eval_metrics_map` 与 callbacks/optimizers 的扩展机制，届时可自定义一个训练验证指标；也可仿照 `EvalModel`（`register_model_api('swift_custom')`）写一个自定义 ModelAPI 接入 EvalScope。
- **深入 EvalScope**：本讲止于 ms-swift 侧的胶水；若需压测、自定义 MCQ/QA 评测数据集，请直接阅读 EvalScope 文档与 `docs/source_en/Instruction/Evaluation.md` 的「Custom Evaluation Datasets」一节。
