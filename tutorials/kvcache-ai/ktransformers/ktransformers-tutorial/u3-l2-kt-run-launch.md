# 用 kt run 启动推理服务

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `kt run <模型名>` 这一条命令，背后是如何从「一个名字」一路走到「一条 `python -m sglang.launch_server ...` 启动命令」并最终执行它的。
- 理解 `kt run` 的两条参数收集路径：交互式向导（interactive）与非交互式命令行参数（non-interactive），以及它们与配置文件默认值之间的优先级关系。
- 看懂 `_build_sglang_command` 是如何把 CPU-GPU 异构推理所需的 `--kt-*` 参数逐条拼装到最终命令里的。
- 学会用 `--dry-run` 只打印、不执行最终命令，用来检查自己的参数配置是否正确。

本讲承接上一讲 `u3-l1`（KT CLI 总览）。上一讲我们知道了 `kt run` 因为要透传未知参数而被单独拦截；本讲就钻进 `commands/run.py`，看它到底做了什么。

## 2. 前置知识

在开始之前，先用通俗的话解释几个关键概念：

- **SGLang**：一个高性能的大模型推理服务框架，负责把模型权重加载成 HTTP API。KTransformers（kt-kernel）并不自己写一个完整的推理服务，而是「寄生」在 SGLang 上——用 `python -m sglang.launch_server` 启动服务，再通过一组 `--kt-*` 参数把 KTransformers 的 CPU-GPU 异构算子接进去。这就是为什么 `kt run` 最终拼出来的是一条 sglang 命令。

- **CPU-GPU 异构推理**：MoE（混合专家）模型里，被频繁激活的「热专家」放 GPU（快但贵），「冷专家」放 CPU（慢但便宜）。`--kt-num-gpu-experts` 控制放多少专家到 GPU，`--kt-cpuinfer` 控制 CPU 用多少线程算剩下的专家。

- **kt-* 参数**：SGLang 官方版不认识这些参数。它们来自 `kvcache-ai` 维护的 SGLang fork（发布名 `sglang-kt`）。所以 `kt run` 在启动前必须先确认你装的是这个 fork，否则会直接报错。

- **交互式 vs 非交互式**：当你什么都不填直接 `kt run`，或者填了模型名但漏了关键参数时，`kt run` 会弹出一个分步向导逐步问你（前提是终端是 TTY）；当你在命令行一次性把关键参数都给齐时，它就走「非交互式」快速路径。

- **resolve（解析）**：`kt run` 内部一个小函数，按「命令行显式给的值 > 配置文件里的值 > 代码里的默认值」三级优先级，决定每个参数最终用什么值。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `kt-kernel/python/cli/commands/run.py` | `kt run` 的核心实现：参数定义、模型解析、参数收集、命令拼装、最终执行。本讲的主角。 |
| `kt-kernel/python/cli/utils/run_interactive.py` | 交互式向导的 8 个步骤实现（选模型、选方法、配 NUMA/CPU、配 GPU 专家、配 KV cache、选 GPU、配解析器、配端口）。 |
| `kt-kernel/python/cli/utils/sglang_checker.py` | 启动前的依赖检查：确认 SGLang 已安装、确认它是支持 kt-kernel 的 fork。 |
| `kt-kernel/python/cli/main.py` | CLI 总入口，其中有一段专门「拦截」`run` 子命令，让它能把未知参数透传给 sglang。 |
| `kt-kernel/python/cli/utils/environment.py` | 硬件探测（GPU、CPU 核数、NUMA 节点数、内存），用于计算智能默认值。 |
| `kt-kernel/python/cli/utils/port_checker.py` | 端口可用性检查，交互式选端口时用到。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，正好对应一条命令从输入到执行的三段旅程：

1. **模型解析**——把 `<模型名>` 变成一个磁盘上真实存在的路径。
2. **参数收集**——把「CPU 线程数、GPU 专家数、张量并行数」等一堆参数凑齐（交互式或非交互式）。
3. **命令拼装**——把所有参数拼成一条 `python -m sglang.launch_server ...` 并执行。

### 4.1 模型解析：从模型名到磁盘路径

#### 4.1.1 概念说明

你在终端敲 `kt run deepseek-v3` 时，`deepseek-v3` 只是一个字符串。要启动模型，KTransformers 必须知道这个模型在磁盘上的真实目录。模型解析就是完成「名字 → 路径」这一步。

这里有两种合法的「名字」：

- **直接给路径**：`kt run /mnt/data/models/Qwen3-30B-A3B`，只要这个路径在磁盘上存在，就直接用。
- **给注册名**：`kt run deepseek-v3`，此时去「用户模型注册表」（`user_models.yaml`）里按名字查，查到对应路径再用。

在解析之前，还有两道关卡：第一，`kt run` 这个子命令本身的入口是怎么被调用的；第二，启动前必须确认环境里装了正确的 SGLang。

#### 4.1.2 核心流程

模型解析的完整前置链路可以这样描述：

```text
用户敲: kt run <model> [选项...] [未知参数...]
   │
   ├─ main.py:main() 检测到 args[0]=="run"
   │     └─ 直接调用 run_module.run.main(args=run_args, standalone_mode=False)
   │        （绕过 typer，让 click 的 ignore_unknown_options 生效）
   │
   ├─ run.py:run()  —— @click.command 定义参数，转交给 _run_impl()
   │     ├─ 处理 --help（因为禁用了自带 help，要手动 echo）
   │     └─ 收集 ctx.args 里所有「未识别的选项」→ extra_cli_args
   │
   └─ run.py:_run_impl()
         ├─ ① 检查 SGLang 是否安装           (check_sglang_installation)
         ├─ ② 检查 SGLang 是否支持 kt-kernel  (check_sglang_kt_kernel_support)
         ├─ ③ 判断走交互式还是非交互式
         └─ ④ 非交互式分支里，做模型解析（本模块的重点）
```

注意：`run` 命令是用 `@click.command` 装饰的，而不是 typer。这是因为 typer 默认会拒绝未知选项，而 `kt run` 需要把用户随手敲的 sglang 参数（比如 `--fp8-gemm-backend triton`）原样透传下去。click 通过 `context_settings` 里的两个开关做到这点。

#### 4.1.3 源码精读

**第一处：main.py 拦截 run 子命令。** 因为 typer 不方便透传未知参数，`main()` 里专门判断第一个参数是不是 `run`，是就直接用 click 的方式调用，其余命令才交给 typer 的 `app()`：

[kt-kernel/python/cli/main.py:534-541](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/main.py#L534-L541) —— 这段就是「run 特殊处理」：取出 `run` 后面的全部参数，交给 `run_module.run.main()`。

**第二处：click 命令开启未知选项放行。** `@click.command` 用 `context_settings` 设了两个关键开关：

[kt-kernel/python/cli/commands/run.py:34-37](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/run.py#L34-L37) —— `ignore_unknown_options=True`（不认识的选项不报错）+ `allow_extra_args=True`（允许多余参数）。同时 `add_help_option=False`，因为 `--help` 要手动处理，避免和透传机制打架。

**第三处：启动前的两道 SGLang 依赖检查。** 在 `_run_impl` 一开头：

[kt-kernel/python/cli/commands/run.py:204-219](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/run.py#L204-L219) —— 先 `check_sglang_installation()` 确认 SGLang 装了，再 `check_sglang_kt_kernel_support()` 确认它认识 kt-kernel 参数；任一不满足都打印安装指引并退出。

这两个检查的实现很有意思。`check_sglang_kt_kernel_support` 的判定方式是「跑一次 `sglang.launch_server --help`，看输出里有没有 `--kt-gpu-prefill-token-threshold` 这个参数」——这个参数只有 fork 版才有：

[kt-kernel/python/cli/utils/sglang_checker.py:287-333](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/utils/sglang_checker.py#L287-L333) —— 这段说明检测逻辑：子进程跑 help、把 stdout+stderr 拼起来、用字符串包含判断 fork 支持。为避免每次都跑（这条 help 在慢机器上要几十秒），结果会缓存到 `~/.ktransformers/cache/sglang_kt_kernel_supported`（见同文件 241-284 行的缓存读写函数）。

**第四处：模型解析的核心——非交互式分支。** 在确认走非交互式路径后，解析逻辑分两种情况：

[kt-kernel/python/cli/commands/run.py:338-373](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/run.py#L338-L373) —— 若 `Path(model).exists()` 为真，说明用户给的是路径，直接用，并顺便在注册表里查一下是否已登记（便于显示名字）；否则按名字到 `user_registry.get_model(model)` 查注册表，查不到就报错并列出可用模型、提示用 `kt model add` 登记。

> 补充：交互式分支（见 4.2 节）里，模型选择是向导第一步，由 `run_interactive.py` 的 `select_model()` 完成，它会过滤出 safetensors 格式的 MoE 模型让你选（[run_interactive.py:60-159](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/utils/run_interactive.py#L60-L159)）。所以「模型解析」在两条路径里实现不同，但目的相同：拿到 `resolved_model_path`。

#### 4.1.4 代码实践

**实践目标**：亲手追踪一个模型名如何被解析成路径，不真正加载大模型。

**操作步骤**：

1. 先看看本地登记了哪些模型（如果还没有，先跑一次 `kt model scan` 或 `kt model add /路径`，这在 `u3-l3` 会详讲）：

   ```bash
   kt model list
   ```

2. 用 `--dry-run` 观察一个**已登记模型**会被解析成什么路径（`<your-model-name>` 换成上面列出的名字）：

   ```bash
   kt run <your-model-name> --gpu-experts 1 --tensor-parallel-size 1 --cpu-threads 8 --numa-nodes 1 --max-total-tokens 1024 --dry-run
   ```

3. 再试一个**直接给路径**的情况（随便指一个存在的目录）：

   ```bash
   kt run /tmp --gpu-experts 1 --tensor-parallel-size 1 --cpu-threads 8 --numa-nodes 1 --max-total-tokens 1024 --dry-run
   ```

4. 最后试一个**不存在的名字**，观察报错信息：

   ```bash
   kt run this-model-does-not-exist --dry-run
   ```

**需要观察的现象**：

- 第 2、3 步：`--dry-run` 会先打印 `Path: <解析后的绝对路径>`，再打印拼好的命令。注意第 3 步虽然 `/tmp` 不是真模型，但因为它「存在」，解析阶段不会报错（报错会发生在真正加载时）。
- 第 4 步：会打印 `Model 'this-model-does-not-exist' not found`，并列出已登记的模型。

**预期结果**：理解「名字 → 注册表查路径」和「路径直接用」两条分支的区别。若没有真实模型可测，**待本地验证**当前机器上 `kt model list` 的输出。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `kt run` 要在 `main.py` 里被单独拦截，而不是像 `kt version` 那样用 `app.command()` 注册？

**参考答案**：因为 `kt run` 需要把用户随手输入的、kt CLI 本身没有定义的 sglang 参数（如 `--fp8-gemm-backend triton`）原样透传给最终的 sglang 命令。typer 默认对未知选项报错，而 click 通过 `context_settings={"ignore_unknown_options": True, "allow_extra_args": True}` 可以放行。所以 `run` 用 `@click.command` 实现，并在 `main()` 里被单独拦截调用。

**练习 2**：`check_sglang_kt_kernel_support` 是靠什么特征判断「当前装的是 kvcache-ai 的 SGLang fork」？为什么这个判断需要缓存？

**参考答案**：靠「`sglang.launch_server --help` 的输出里是否包含 `--kt-gpu-prefill-token-threshold`」来判断——这个参数只在 fork 版里存在。需要缓存是因为跑一次 help 要拉起 sglang、初始化 CUDA，在慢机器上可能要几十秒，每次 `kt run` 都跑一遍体验很差；首次确认通过后结果写进 `~/.ktransformers/cache/sglang_kt_kernel_supported`，之后直接读缓存。

---

### 4.2 参数收集：交互式与非交互式两条路径

#### 4.2.1 概念说明

启动一个异构推理服务需要一大堆参数：CPU 线程数、GPU 专家数、张量并行（TP）大小、NUMA 节点数、最大 token 数、显存占用比例……逐个手敲很痛苦。`kt run` 提供了两种收集方式：

- **交互式向导**：你只敲 `kt run`，它弹出一个 8 步向导，每步给推荐默认值，你回车确认或改值即可。适合第一次用、不熟悉参数含义的场景。
- **非交互式**：你在命令行把参数一次性给齐，它直接用。适合写脚本、自动化部署。

两条路径最终都会产出一组「确定下来的参数」，交给 4.3 节的命令拼装。无论哪条路径，都遵循同一个三级优先级：**命令行显式给的值 > 配置文件（`~/.ktransformers/config.yaml`）里的值 > 代码里的智能默认值**。

智能默认值会调用硬件探测，比如 CPU 线程默认取「逻辑线程数 × 0.8」，TP 大小默认取 GPU 数量。

#### 4.2.2 核心流程

判断走哪条路径的逻辑很简洁：

```text
use_interactive = False
如果 model 是 None（没指定模型）           → use_interactive = True
否则如果 关键参数(gpu_experts/tp/cpu_threads/numa/max_total_tokens) 任一缺失 → use_interactive = True

如果 use_interactive 且 终端是 TTY：
    调 interactive_run_config() 跑 8 步向导
否则：
    走非交互式：探测硬件 → 用 resolve() 按三级优先级填值
```

交互式向导的 8 步大致是：

```text
Step 1 选模型        → select_model()
Step 2 选推理方法     → select_inference_method()  (saved / raw / amx / gguf)
Step 3 配 NUMA+CPU   → configure_numa_and_cpu()
Step 4 配 GPU 专家数  → configure_gpu_experts()
Step 5 配 KV cache   → configure_kv_cache()  (仅 raw 精度)
Step 6 选 GPU+TP+显存 → select_gpus_and_tp()
Step 7 配解析器(可选) → configure_parsers()
Step 8 配 host+port  → configure_host_and_port()
```

非交互式分支则没有这些步骤，而是直接用一个 `resolve()` 小函数把每个参数定下来。

#### 4.2.3 源码精读

**第一处：决定走哪条路径的判断。**

[kt-kernel/python/cli/commands/run.py:228-241](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/run.py#L228-L241) —— 注意 `not numa_nodes` 这个条件：`numa_nodes` 是 click 的 `multiple=True` 选项，没给时是空 tuple `()`，布尔值为假，于是触发交互式。这就是为什么「不把 5 个关键参数给齐」就会进向导。

**第二处：交互式分支调用向导并提取结果。**

[kt-kernel/python/cli/commands/run.py:242-299](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/run.py#L242-L299) —— 这里 `interactive_run_config()` 返回一个大字典 `config`，逐项取出模型路径、线程数、GPU 专家、TP、host/port 等；其中 `os.environ["CUDA_VISIBLE_DEVICES"] = ...` 一行很关键——向导里你选了哪几张卡，这里就只让这些卡对 sglang 可见。

向导的总调度函数：

[kt-kernel/python/cli/utils/run_interactive.py:886-992](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/utils/run_interactive.py#L886-L992) —— 这是 `interactive_run_config` 的前半段，依次调用 Step1~Step8 的函数，把结果合并进 `full_config`。其中 Step2 的「选推理方法」会决定后续 `kt_method`（如 `AMXINT4`、`FP8`、`LLAMAFILE`）。

推理方法的四种选择（raw/amx/gguf/saved）：

[kt-kernel/python/cli/utils/run_interactive.py:162-235](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/utils/run_interactive.py#L162-L235) —— 这一步决定了 CPU 侧用什么权重、什么精度后端。

**第三处：非交互式分支探测硬件、计算默认值。**

[kt-kernel/python/cli/commands/run.py:314-328](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/run.py#L314-L328) —— 调 `detect_gpus()` 和 `detect_cpu_info()`，打印一行 GPU/CPU 信息，作为后续默认值的依据。

硬件探测函数主要靠解析 `nvidia-smi` 和 `/proc/cpuinfo`、`/sys/devices/system/node`：

[kt-kernel/python/cli/utils/environment.py:222-262](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/utils/environment.py#L222-L262) —— `detect_gpus()` 跑 `nvidia-smi` 拿到卡数和显存，并尊重 `CUDA_VISIBLE_DEVICES` 做过滤与重编号。

[kt-kernel/python/cli/utils/environment.py:289-375](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/utils/environment.py#L289-L375) —— `detect_cpu_info()` 从 `/proc/cpuinfo` 拿 CPU 名字、核数、指令集，从 `/sys/devices/system/node` 数 NUMA 节点数。

**第四处：三级优先级的核心——`resolve()`。**

[kt-kernel/python/cli/commands/run.py:415-420](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/run.py#L415-L420) —— 这是优先级链的灵魂：`cli_val`（命令行给的）不为 None 就用它；否则读 `settings.get(config_key)`（配置文件）；都没有就用第三个参数 `default`。

紧接着一堆 `final_xxx = resolve(...)` 调用，把每个参数都套上这套优先级：

[kt-kernel/python/cli/commands/run.py:422-461](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/run.py#L422-L461) —— 几个值得注意的默认值：TP 默认 `len(gpus) if gpus else 1`（有几张卡就 TP 几）；CPU 线程默认 `int(total_threads * 0.8)`（留 20% 给系统）；`kt_method` 默认 `AMXINT4`；`mem_fraction_static` 默认 `0.98`（把显存吃满）。

**第五处：未知参数的透传。** 回到 `run()` 函数，所有 click 没识别的参数都被收进 `ctx.args`：

[kt-kernel/python/cli/commands/run.py:135-140](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/run.py#L135-L140) —— `extra_cli_args` 收集未知选项（并剔除 `--help`），最后会原样追加到 sglang 命令末尾。

#### 4.2.4 代码实践

**实践目标**：直观感受三级优先级——同样的关键参数，命令行给的值如何覆盖默认值。

**操作步骤**：

1. 用 `--dry-run` 跑非交互式，故意把关键参数都给齐，并指定 `--cpu-threads 16`：

   ```bash
   kt run <your-model-name> --gpu-experts 4 --tensor-parallel-size 1 \
     --cpu-threads 16 --numa-nodes 1 --max-total-tokens 2048 --dry-run
   ```

2. 在输出里找到 `Command:` 那一段，确认 `--kt-cpuinfer` 是不是 `16`（你给的值），而不是默认的「线程数×0.8」。

3. 再跑一次，故意把 `--cpu-threads` 去掉（其余关键参数仍给齐），看 `--kt-cpuinfer` 变成什么（应是默认值）：

   ```bash
   kt run <your-model-name> --gpu-experts 4 --tensor-parallel-size 1 \
     --numa-nodes 1 --max-total-tokens 2048 --dry-run
   ```

**需要观察的现象**：第 1 步 `--kt-cpuinfer 16`；第 2 步 `--kt-cpuinfer` 变成一个较大的默认值（取决于你的 CPU 逻辑线程数 × 0.8）。

**预期结果**：验证「命令行值 > 默认值」生效。`--dry-run` 让整个过程不真正启动模型，只看拼出来的命令。若无真实模型，第 3、4 步的具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `kt run` 默认 CPU 线程数取「逻辑线程数 × 0.8」而不是全部线程？

**参考答案**：留出约 20% 的线程给操作系统、sglang 的 GPU 调度、日志等后台任务，避免 CPU MoE 计算把机器卡死。源码里是 `resolve(cpu_threads, "inference.cpu_threads", int(total_threads * 0.8))`。

**练习 2**：如果你既没在命令行给 `--tensor-parallel-size`，也没在配置文件里设，机器上有 2 张 GPU，最终 TP 会是多少？依据是哪一行代码？

**参考答案**：TP = 2。依据 [run.py:427-429](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/run.py#L427-L429)，`resolve(tensor_parallel_size, "inference.tensor_parallel_size", len(gpus) if gpus else 1)`，命令行和配置都为空时取默认值 `len(gpus)=2`。

**练习 3**：交互式向导里「选 GPU」这一步要求选的卡数必须是 2 的幂（1/2/4/8…），这个校验在哪段代码里？

**参考答案**：在 `select_gpus_and_tp` 内部的 `validate_tp_requirements` 校验函数里，用位运算 `actual_count & (actual_count - 1) != 0` 判断是否为 2 的幂，见 [run_interactive.py:673-689](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/utils/run_interactive.py#L673-L689)。

---

### 4.3 命令拼装：构造 sglang 启动命令

#### 4.3.1 概念说明

参数都凑齐后，最后一步是拼出真正的启动命令。`kt run` 自己不会加载模型，它拼出来的是这样一条命令，然后交给 `subprocess.run` 执行：

```bash
python -m sglang.launch_server \
  --host 0.0.0.0 --port 30000 \
  --model /path/to/model \
  --kt-weight-path /path/to/weights \
  --kt-cpuinfer 64 \
  --kt-threadpool-count 2 \
  --kt-num-gpu-experts 32 \
  --kt-method AMXINT4 \
  ... (其它 sglang 参数)
```

`_build_sglang_command` 这个函数就是干这件事的。它按固定顺序往一个列表里 `append`/`extend` 字符串，最后返回这个列表。命令由四部分组成：

1. **基础命令**：解释器 + 模块 + host/port/model。
2. **kt-* 参数块**（条件性加入）：CPU-GPU 异构推理的核心开关。
3. **sglang 通用参数块**：attention 后端、显存比例、TP 等。
4. **追加参数**：模型默认值、`advanced.sglang_args`、用户透传的未知参数。

#### 4.3.2 核心流程

是否注入 kt-* 参数块，由一个判断决定：

```text
use_kt_kernel = False
如果 提供了 weights_path（量化权重）        → use_kt_kernel = True
否则 如果 cpu_threads > 0 或 gpu_experts > 1  → use_kt_kernel = True

如果 use_kt_kernel：
    追加 --kt-weight-path / --kt-cpuinfer / --kt-threadpool-count
           --kt-num-gpu-experts / --kt-method
           --kt-gpu-prefill-token-threshold / --kt-enable-dynamic-expert-update
    （如果指定了 --numa-nodes 多个值，追加 --kt-numa-nodes ...）
```

也就是说：只有当「要加载量化权重」或「启用了 CPU 卸载（CPU 算专家 / GPU 放多于 1 个专家）」时，才会启用 kt-kernel 异构路径；否则就是普通 sglang 推理。

`--kt-weight-path` 用哪个路径也有讲究：有量化权重就用权重路径，没有就用模型路径本身（对应「原生精度」后端，CPU 和 GPU 共享同一份权重）。

#### 4.3.3 源码精读

**第一处：基础命令。** 函数开头先放解释器和模块、地址、模型路径：

[kt-kernel/python/cli/commands/run.py:633-643](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/run.py#L633-L643) —— 注意用的是 `sys.executable`（当前 Python 解释器），保证用的是 kt 所在的同一个环境，而不是 PATH 里随便一个 `python`。

**第二处：是否启用 kt-kernel 的判断 + weight-path 选择。**

[kt-kernel/python/cli/commands/run.py:649-661](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/run.py#L649-L661) —— 这段是「异构推理开关」：`weights_path` 优先于 `model_path` 作为 `--kt-weight-path`。

**第三处：kt-* 参数块的注入。**

[kt-kernel/python/cli/commands/run.py:664-682](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/run.py#L664-L682) —— 这 8 个 `--kt-*` 参数就是 CPU-GPU 异构推理的全部核心开关：

| 参数 | 含义 |
| --- | --- |
| `--kt-weight-path` | CPU 侧权重目录（量化权重或原模型目录） |
| `--kt-cpuinfer` | CPU 推理线程数 |
| `--kt-threadpool-count` | 线程池数量（通常 = NUMA 节点数） |
| `--kt-num-gpu-experts` | 每层放多少专家到 GPU |
| `--kt-method` | CPU 后端方法（AMXINT4/AMXINT8/FP8/BF16/LLAMAFILE…） |
| `--kt-gpu-prefill-token-threshold` | 超过该 token 数时启用逐层 prefill 策略 |
| `--kt-enable-dynamic-expert-update` | 启用专家动态迁移 |
| `--kt-numa-nodes`（可选） | 显式指定每个线程池绑定到哪些 NUMA 节点 |

**第四处：sglang 通用参数块。**

[kt-kernel/python/cli/commands/run.py:685-705](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/run.py#L685-L705) —— 这些是 sglang 自己的参数：attention 后端、`--trust-remote-code`、显存静态占比、chunked prefill、最大并发请求、最大 token、看门狗超时、TP 大小等。其中 `--enable-mixed-chunk`、`--enable-p2p-check` 是固定开启的。

**第五处：条件性追加的标志位。**

[kt-kernel/python/cli/commands/run.py:707-723](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/run.py#L707-L723) —— 三个条件块：有 `served_model_name` 才加；`disable_shared_experts_fusion` 为真才加 `--disable-shared-experts-fusion`；`kt_method` 含 `FP8` 时自动加 `--fp8-gemm-backend triton`。

**第六处：未知参数透传到命令末尾。**

[kt-kernel/python/cli/commands/run.py:758-766](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/run.py#L758-L766) —— 配置里的 `advanced.sglang_args` 和命令行透传的 `extra_cli_args` 都追加到末尾。这就是为什么你能直接 `kt run m2 --fp8-gemm-backend triton` 把任意 sglang 参数传下去。

**第七处：执行前准备环境变量 + 冲突检查。**

[kt-kernel/python/cli/commands/run.py:500-512](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/run.py#L500-L512) —— 把 shell 环境变量和配置文件里的 `inference.env`/`advanced.env` 合并成最终环境；再调 `_check_conflicting_env_vars` 提前拦截会导致 sglang 崩溃的环境变量冲突（比如同时设了 MXFP4 专用变量却用了别的 method）。

**第八处：`--dry-run` 与真正执行的分叉。**

[kt-kernel/python/cli/commands/run.py:545-568](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/run.py#L545-L568) —— `dry_run` 为真时只打印命令、不执行（`return`）；否则用 `subprocess.run(cmd, env=env)` 真正启动，并把 sglang 的退出码透传出去（`sys.exit(process.returncode)`）。注意是「直接执行、不拦截输出」，这样 sglang 的日志原样进终端，`Ctrl+C` 也能自然生效。

> 小贴士：如果你只想看拼出来的命令长什么样，永远先加 `--dry-run`。这是最安全的「命令构造观察」手段。

#### 4.3.4 代码实践

**实践目标**：用 `--dry-run` 打印出一条完整的 sglang 启动命令，逐段核对它是怎么被拼出来的。

**操作步骤**：

1. 准备一个已登记的 safetensors MoE 模型（用 `kt model list` 找一个，记为 `<m>`）。然后跑：

   ```bash
   kt run <m> \
     --gpu-experts 8 --tensor-parallel-size 1 \
     --cpu-threads 64 --numa-nodes 2 \
     --max-total-tokens 4096 \
     --kt-method AMXINT8 \
     --dry-run
   ```

2. 在输出的 `Command:` 段落里，逐项核对下面这些片段是否存在、值是否正确：
   - `python -m sglang.launch_server`
   - `--model <模型路径>`
   - `--kt-weight-path`（因为没给 `--weights-path`，应等于 `--model` 的路径）
   - `--kt-cpuinfer 64`、`--kt-threadpool-count 2`、`--kt-num-gpu-experts 8`
   - `--kt-method AMXINT8`、`--kt-enable-dynamic-expert-update`
   - `--attention-backend flashinfer`、`--tensor-parallel-size 1`
   - 是否**没有** `--fp8-gemm-backend triton`（因为 method 是 AMXINT8，不是 FP8）

3. 再加一个透传参数试试，验证未知参数透传：

   ```bash
   kt run <m> --gpu-experts 8 --tensor-parallel-size 1 \
     --cpu-threads 64 --numa-nodes 2 --max-total-tokens 4096 \
     --kt-method AMXINT8 --dry-run --foo-bar 123
   ```

   观察 `--foo-bar 123` 是否原样出现在命令末尾。

**需要观察的现象**：第 2 步各片段一一对应；第 3 步 `--foo-bar 123` 出现在末尾。

**预期结果**：完整理解「基础命令 + kt-* 块 + sglang 块 + 透传参数」的四段式拼接。若机器上没有合适的 MoE 模型，可用一个存在的目录代替 `<m>` 只观察命令构造（解析阶段不会报错），但具体路径与参数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：什么情况下 `_build_sglang_command` 不会注入任何 `--kt-*` 参数？这种情况下 `kt run` 和直接用 sglang 有区别吗？

**参考答案**：当既没有提供 `weights_path`，又满足 `cpu_threads <= 0 且 gpu_experts <= 1` 时，`use_kt_kernel` 为假，不注入 kt-* 参数。此时 `kt run` 拼出的命令本质上就是一条普通 sglang 启动命令（仅多了些固定 sglang 参数和透传参数），和不带 kt 的 sglang 推理没有实质区别——也就是说没有启用 CPU-GPU 异构路径。

**练习 2**：为什么命令开头用 `sys.executable` 而不是写死 `"python"`？

**参考答案**：`sys.executable` 是「当前运行 kt 的那个 Python 解释器」的绝对路径。这样能保证 sglang 用的是和 kt-kernel 完全相同的 Python 环境（同一个虚拟环境 / conda 环境），避免 PATH 里的 `python` 指向另一个没装 sglang-kt 的环境导致启动失败。见 [run.py:634](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/run.py#L634)。

**练习 3**：`--kt-gpu-prefill-token-threshold` 这个参数，在交互式向导里只有选了「raw 精度」才会让你配；但在非交互式 `_build_sglang_command` 里它总是被加进去。这会冲突吗？

**参考答案**：不冲突。交互式向导里 `configure_kv_cache` 只对 raw 精度弹出该配置项（[run_interactive.py:597-624](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/utils/run_interactive.py#L597-L624)），非 raw 时取默认值；`_build_sglang_command` 无条件追加它只是把（可能是默认的）阈值传给 sglang，sglang 侧会根据 method 自行决定是否真正启用逐层 prefill。两者职责不同：向导决定「是否让用户调」，拼装函数决定「是否把值传下去」。

## 5. 综合实践

把三个模块串起来：模拟一次完整的「模型名 → 解析 → 收集参数 → 拼装命令」旅程，全程不真正启动大模型。

**任务**：写一张「`kt run` 命令构造追踪表」。

1. 选定一个已登记模型 `<m>`（用 `kt model list` 找）。
2. 设计一条非交互式命令，要求同时覆盖三类来源的参数：
   - 命令行显式给的：`--cpu-threads 32`、`--kt-method AMXINT8`
   - 依赖硬件默认的：不给 `--tensor-parallel-size`（让它默认 = GPU 数）
   - 透传给 sglang 的：`--served-model-name my-test`
3. 命令：

   ```bash
   kt run <m> \
     --gpu-experts 4 --numa-nodes 1 --max-total-tokens 2048 \
     --cpu-threads 32 --kt-method AMXINT8 \
     --served-model-name my-test \
     --dry-run
   ```

4. 跑完后，把输出里的 `Command:` 段落拆成下面这张表（示例填法，请用你机器的真实输出替换）：

   | 命令片段 | 来自哪个模块/来源 | 对应源码位置 |
   | --- | --- | --- |
   | `python -m sglang.launch_server` | 命令拼装-基础命令 | run.py:633-643 |
   | `--model <路径>` | 模型解析-注册表查到的路径 | run.py:373 |
   | `--kt-cpuinfer 32` | 参数收集-命令行显式值（经 resolve） | run.py:433、664-680 |
   | `--tensor-parallel-size <N>` | 参数收集-硬件默认（GPU 数） | run.py:427-429 |
   | `--served-model-name my-test` | 透传/条件追加 | run.py:707-709 |
   | `--enable-mixed-chunk` | 命令拼装-固定 sglang 标志 | run.py:685-705 |

5. 对照表格，向自己解释：为什么 `--kt-cpuinfer` 是 32（你给的），而 `--tensor-parallel-size` 不是你给的（你根本没给）？

**预期收获**：你能对着一条 `kt run` 命令，准确说出它的每个片段是「用户给的、配置文件给的、还是代码默认的」，并且知道去 `run.py` 的哪一段找依据。这标志着你已经真正读懂了 `kt run` 的三段式流程。

> 如果当前机器没有可用模型或 GPU，可以用任意存在的目录充当 `<m>` 只观察命令构造（模型解析对「存在的路径」不会报错）；具体的路径与数值需**待本地验证**。

## 6. 本讲小结

- `kt run` 在 `main.py` 里被单独拦截、用 click 实现，是为了能通过 `ignore_unknown_options` 把任意 sglang 参数透传下去——这是它与其它子命令最大的不同。
- 启动前有两道硬性检查：SGLang 已安装、且是支持 kt-kernel 的 kvcache-ai fork（靠 `--help` 输出是否含 `--kt-gpu-prefill-token-threshold` 判定，结果有缓存）。
- 模型解析有两种合法输入：直接给磁盘路径（存在即用），或给注册名（查 `user_models.yaml`）。
- 参数收集有两条路径：交互式 8 步向导（适合新手）、非交互式命令行（适合脚本）；两者都遵循「命令行 > 配置文件 > 智能默认」三级优先级，默认值依赖 `detect_gpus`/`detect_cpu_info` 的硬件探测。
- 命令拼装由 `_build_sglang_command` 完成，分四段：基础命令 + 条件性 kt-* 块（由「有量化权重或启用 CPU 卸载」触发）+ sglang 通用块 + 透传参数；最终用 `subprocess.run` 执行。
- `--dry-run` 是观察命令构造的「安全网」，永远可以先 `--dry-run` 再正式跑。

## 7. 下一步学习建议

- **下一步学 `u3-l3`（模型管理与配置）**：本讲多次提到「用户模型注册表」和「配置文件默认值」，下一讲会讲清 `kt model add/scan/list` 怎么维护注册表、`~/.ktransformers/config.yaml` 怎么改默认值，让你能彻底掌控 `resolve()` 的第二级优先级。
- **进阶到 `u6-l1`（SGLang 集成与 kt-* 参数）**：本讲只讲了 `--kt-*` 参数「怎么被拼进去」，它们各自的语义、调优原则（为什么 `--kt-cpuinfer` 取物理核、为什么 `--kt-threadpool-count` 取 NUMA 节点数）会在进阶层详讲。
- **想动手改命令拼装逻辑**：直接读 [run.py 的 `_build_sglang_command`](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/run.py#L606-L767)，配合 `--dry-run` 反复对照输出，是理解这部分最快的路径。
- **想理解异构推理本身**：等学到 `u4`（Python 推理 API）和 `u6`（SGLang 集成与专家调度），你会看到这些 `--kt-*` 参数在 sglang-kt 里是如何被消费、如何把专家在 CPU/GPU 之间调度的。
