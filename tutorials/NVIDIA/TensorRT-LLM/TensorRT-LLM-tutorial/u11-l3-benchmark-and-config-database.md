# 基准测试与配置数据库

## 1. 本讲目标

本讲解决两个在生产部署中最常被问到的问题：「**我这个模型在这块卡上到底能跑多快？**」和「**我该用哪套参数去部署？**」。

TensorRT-LLM 用两个互相配套的机制来回答它们：

- `trtllm-bench`：一个命令行基准工具，用来在真实硬件上测吞吐与延迟。
- `examples/configs/database/`：一份「配置数据库」，预先存好了多组模型 × GPU × 序列长度 × 并发度下的 Pareto 优化配置，可以直接喂给 `trtllm-serve --config` 当部署起点。

学完本讲你应该能够：

1. 说清 `trtllm-bench` 这个命令是怎么搭起来的（click 命令组 + 子命令），以及它的吞吐基准在内部经历了哪几步。
2. 读懂 `tensorrt_llm/bench/` 子包的目录划分，理解 `RuntimeConfig`、`GeneralExecSettings`、异步基准循环各自的角色。
3. 理解配置数据库里的 `Recipe` / `lookup.yaml` 是怎么组织的，Pareto「配方（recipe）」与「三档画像（latency / balanced / throughput）」的含义。
4. 把数据库里的一条配置，亲手改造成 `trtllm-serve --config` 的起点，并设计一次完整的吞吐基准命令行。

## 2. 前置知识

在进入正文前，先用大白话对齐几个概念。它们大多在前面讲义里出现过，这里只做最小回顾。

- **吞吐（throughput）与延迟（latency）的此消彼长**：在线推理里，并发度（同时处理的请求数）调得越高，单位时间产出的 token 越多（吞吐上升），但每个请求排队等待的时间也越长（延迟上升）。这是一对典型的权衡。
- **Pareto 前沿（Pareto frontier）**：如果配置 A 在「吞吐」和「延迟」两个指标上都不比配置 B 差、且至少一项更好，就称 A 支配 B。所有「不被任何配置支配」的配置构成 Pareto 前沿——它们是值得保留的候选，其余都可以丢掉。
- **`trtllm-serve` 与 `--config`**：在 [u1-l3](u1-l3-first-run-llm-api-and-serve.md) 我们见过，`trtllm-serve` 通过 `--config <yaml>` 读一份 YAML 配置来启动服务。这份 YAML 的字段就是 `TorchLlmArgs` 的字段（见 [u4-l1](u4-l1-llm-args-hierarchy.md)）。
- **CLI 与 YAML 谁说了算**：本讲会反复用到一条规则——用户在命令行**显式敲了**的参数，会覆盖 YAML 里同名字段；没敲的，才让 YAML 生效。这条「CLI 优先」规则在后面 4.2 节会落到源码。
- **`click`**：一个 Python 命令行框架，用 `@click.group`、`@click.command`、`@click.option` 装饰器把普通函数变成带帮助、带参数解析的 CLI。`trtllm-bench` 和 `trtllm-serve` 都用它。

> 名词提醒：本讲里「配置库（database）」「配方（recipe）」「画像（profile）」三个词会反复出现。**配置库**是整个目录；**配方**是库里的一条记录（描述「哪个模型 + 哪块卡 + 多少并发」对应哪个 YAML 文件）；**画像**是给配方贴的标签（latency / balanced / throughput）。

## 3. 本讲源码地图

本讲涉及的关键文件如下表。所有永久链接基于当前 HEAD `cf44a1ccee`。

| 文件 | 作用 |
|------|------|
| `tensorrt_llm/commands/bench.py` | `trtllm-bench` 命令的入口，定义 click 命令组与全局选项，注册四个子命令。 |
| `setup.py` | 把 `trtllm-bench` 注册为控制台脚本（console script）。 |
| `tensorrt_llm/bench/` | 基准工具的内部子包，下分 `benchmark/`、`dataclasses/`、`dataset/`、`tuning/`、`utils/`。 |
| `tensorrt_llm/bench/benchmark/throughput.py` | `throughput` 子命令的实现，吞吐基准的主流水线。 |
| `tensorrt_llm/bench/benchmark/low_latency.py` | `latency` 子命令的实现，低延迟基准。 |
| `tensorrt_llm/bench/benchmark/__init__.py` | 子包门面：`GeneralExecSettings`、`get_llm`、`collect_explicit_cli_keys` 等。 |
| `tensorrt_llm/bench/dataclasses/configuration.py` | `RuntimeConfig` 等运行时数据类，把基准参数翻译成 `LLM` 构造参数。 |
| `tensorrt_llm/bench/dataclasses/general.py` | `BenchmarkEnvironment`、`InferenceRequest`、`DatasetMetadata`。 |
| `tensorrt_llm/bench/benchmark/utils/general.py` | `get_settings`：用启发式算 `max_batch_size` / `max_num_tokens`；读 YAML 配置。 |
| `tensorrt_llm/llmapi/llm_args.py` | `update_llm_args_with_extra_options` / `..._extra_dict`：CLI 与 YAML 合并的优先级规则（与 `trtllm-serve` 共用）。 |
| `examples/configs/database/database.py` | 配置库的数据模型：`Recipe` / `RecipeList` / 画像分配逻辑。 |
| `examples/configs/database/lookup.yaml` | 配置库主索引（204 条配方）。 |
| `docs/source/commands/trtllm-bench.rst` | `trtllm-bench` 的官方文档页（sphinx-click 自动生成）。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**bench CLI**（入口）、**bench 子包**（内部组织与吞吐流水线）、**配置数据库**（Pareto 配方）。

### 4.1 trtllm-bench 命令行入口

#### 4.1.1 概念说明

`trtllm-bench` 是一个独立可执行命令，和 `trtllm-serve` 是「姊妹」关系：

- `trtllm-serve`：**在线服务**，起一个常驻 HTTP 服务接待请求（见 [u11-l1](u11-l1-trtllm-serve-openai-server.md)）。
- `trtllm-bench`：**离线测量**，跑一批固定请求，量出吞吐/延迟后退出。

两者背后构造的是**同一个 `LLM` 类**（[u1-l3](u1-l3-first-run-llm-api-and-serve.md) 已点明），只是 `trtllm-bench` 不套 HTTP 壳，而是直接驱动 `LLM.generate_async` 跑数据集并计时。这一点很关键：**你在 `trtllm-bench` 里测到的数字，就是同一份配置在 `trtllm-serve` 下大致能拿到的数字**。这就是「先 bench 测、再 serve 部署」这条工作流成立的基础。

`trtllm-bench` 采用 `click` 的「命令组 + 子命令」结构。全局选项（如 `--model`）放在命令组上，所有子命令共享；具体怎么跑由子命令决定。当前注册了四个子命令：

| 子命令 | 干什么 |
|--------|--------|
| `throughput` | 吞吐基准：以给定并发度灌一批请求，测总吞吐。 |
| `latency` | 延迟基准：以低并发/单请求逐条发，测每请求延迟。 |
| `prepare-dataset` | 生成/预处理基准数据集（真实数据或合成分布）。 |
| `visual-gen` | VisualGen（扩散模型图像/视频生成）的基准。 |

> 关于文档：`docs/source/commands/trtllm-bench.rst` 的导语说它提供「三个主子命令」、并在 sphinx-click 指令里列了 `throughput, latency, build`。但当前源码里**并没有 `build` 子命令**（旧版痕迹），实际注册的是上表四个。读文档时以源码为准。

#### 4.1.2 核心流程

`trtllm-bench` 的一次调用流程：

```text
$ trtllm-bench --model <M> [全局选项] <子命令> [子命令选项]
        │
        ▼
  click 解析全局选项 → 构造 BenchmarkEnvironment 存入 ctx.obj
        │
        ▼
  分发到子命令（throughput / latency / ...）
        │
        ▼
  子命令用 @click.pass_obj 拿到 BenchmarkEnvironment，开始干活
```

`ctx.obj` 是 click 的「上下文对象」机制：命令组在解析阶段往 `ctx.obj` 里塞一个对象，子命令通过 `@click.pass_obj` 装饰器就能直接收到它，从而共享全局信息（这里是「要测哪个模型、工作目录在哪」）。

#### 4.1.3 源码精读

**命令组定义**：`main` 是一个 `@click.group`，名字就叫 `trtllm-bench`，并打开 `show_default=True` 让帮助里显示每个选项的默认值。[bench.py:25-62](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/commands/bench.py#L25-L62) 定义了它和几个全局选项（`--model`/`-m`、`--model_path`、`--workspace`/`-w`、`--log_level`、`--revision`、`--telemetry/--no-telemetry`）。其中最值得注意的是 `--model` 被标成 `required=True`，但配合了一个自定义类 `NotRequiredForHelp`：

[bench.py:15-22](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/commands/bench.py#L15-L22) —— 这个类的作用是：**当用户敲 `--help` 时，临时把 `required` 关掉**。否则 click 会因为缺了必填的 `--model` 而拒绝打印帮助，用户体验很差。这是一个很实用的小技巧。

`main` 函数体本身只做三件事：设日志级别、构造 `BenchmarkEnvironment` 存进 `ctx.obj`、创建工作目录。[bench.py:63-87](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/commands/bench.py#L63-L87)。

`BenchmarkEnvironment` 是个极简的 Pydantic 模型，只承载全局信息：[general.py:13-18](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/bench/dataclasses/general.py#L13-L18)（字段：`model`、`checkpoint_path`、`workspace`、`revision`、`telemetry_config`）。它会被传给每个子命令。

**注册子命令**：在文件末尾用 `add_command` 把四个子命令挂上去。[bench.py:90-93](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/commands/bench.py#L90-L93)：

```python
main.add_command(throughput_command)
main.add_command(latency_command)
main.add_command(prepare_dataset)
main.add_command(visual_gen_command)
```

**控制台脚本注册**：`trtllm-bench` 这个命令名是怎么和 `main` 函数绑定的？答案在 `setup.py` 的 entry_points 里：[setup.py:469](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/setup.py#L469)（`'trtllm-bench=tensorrt_llm.commands.bench:main'`）。pip 安装时会据此生成一个可执行脚本，敲 `trtllm-bench` 等价于调用 `tensorrt_llm.commands.bench:main()`。和 `trtllm-serve` 是完全对称的注册方式（见 [u1-l3](u1-l3-first-run-llm-api-and-serve.md)）。

#### 4.1.4 代码实践

**实践目标**：不跑模型，只验证命令组的「全局选项 → 子命令」结构，并确认 `NotRequiredForHelp` 的效果。

**操作步骤**：

1. 在已安装 TensorRT-LLM 的环境里执行：
   ```bash
   trtllm-bench --help
   ```
2. 观察输出里是否列出了 `throughput`、`latency`、`prepare-dataset`、`visual-gen` 四个子命令，以及 `--model`、`--workspace` 等全局选项。
3. 再执行（故意**不**带 `--model`）：
   ```bash
   trtllm-bench throughput --help
   ```
4. 观察这一步是否仍然能打印帮助。如果 `--model` 没有 `NotRequiredForHelp`，click 会在解析阶段就报「Missing option '--model'」而拒绝打印 `throughput` 的帮助。

**需要观察的现象**：第 1、3 步都能正常打印帮助；尤其第 3 步即使没给 `--model` 也能看到 `throughput` 的全部选项（这正是 `NotRequiredForHelp` 的功劳）。

**预期结果**：帮助文本正常显示。**待本地验证**：不同版本 click 的输出排版可能略有差异。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `--model` 既要 `required=True`，又要套一个 `NotRequiredForHelp`？二者矛盾吗？

> **参考答案**：不矛盾。`required=True` 保证**真正执行子命令时**必须给模型（否则没法跑基准）；而 `NotRequiredForHelp` 只在命令行里出现 `--help` 时临时关掉必填检查，让用户**无论何时都能查阅帮助**。两者服务的场景不同。

**练习 2**：`trtllm-bench` 和 `trtllm-serve` 在「构造 LLM」这一步上是什么关系？

> **参考答案**：殊途同归。二者最终都构造同一个 `tensorrt_llm.llmapi.llm.LLM`（PyTorch 后端）或 `AutoDeployLLM` 对象，差别只在「外面套什么」：`serve` 套 HTTP 服务常驻，`bench` 套计时与数据集驱动跑完即退。因此同一份 YAML 配置在两者下行为一致。

---

### 4.2 bench 子包的内部组织与吞吐流水线

#### 4.2.1 概念说明

`trtllm-bench` 的入口很薄，真正的活儿都在 `tensorrt_llm/bench/` 子包里。这个子包按职责切成五块：

| 子目录 | 职责 |
|--------|------|
| `benchmark/` | 基准执行：`throughput.py`、`low_latency.py`、`visual_gen.py` 三个子命令，以及异步驱动、报告生成等公共逻辑。 |
| `dataclasses/` | 数据类：`RuntimeConfig`（运行时配置）、`BenchmarkEnvironment`、`DatasetMetadata`、统计与报告类型。 |
| `dataset/` | 数据集准备：`prepare-dataset` 及其 `real-dataset` / `token-norm-dist` / `token-unif-dist` 子命令。 |
| `tuning/` | 启发式调参：根据模型与硬件猜出合理的 `max_batch_size` / `max_num_tokens`。 |
| `utils/` | 杂项工具：数据集读写、tokenizer 初始化等。 |

> 小提醒：`tensorrt_llm/bench/__init__.py` 是**空文件**，它只是把这个目录标记成 Python 包，并不导出任何符号。子包门面实际上在 `benchmark/__init__.py`。

吞吐基准的核心是一条「**翻译链**」：把命令行参数一步步翻译成 `LLM` 构造参数，再交给一个异步循环去真正发请求、计时、出报告。理解这条链，就理解了 `throughput` 子命令。

#### 4.2.2 核心流程

`throughput` 子命令的主干（省略异常处理）：

```text
1. get_general_cli_options(params, bench_env)  →  GeneralExecSettings（通用执行设置）
2. initialize_tokenizer(...)                    →  分词器（用于解析数据集）
3. create_dataset_from_stream(...)              →  (metadata, requests) 数据集 + 统计
4. get_settings(params, metadata, ...)          →  启发式算 max_batch_size / max_num_tokens，
                                                   并读 --config/--extra_llm_api_options YAML
5. RuntimeConfig(**exec_settings)               →  打包成运行时配置对象
6. runtime_config.get_llm_args()                →  翻译成 LLM 关键字参数（含 YAML 合并）
7. get_llm(runtime_config, kwargs)              →  构造 LLM 实例（pytorch / _autodeploy）
8. asyncio.run(async_benchmark(...))            →  异步并发发请求、计时
9. ReportUtility(...).report_statistics()       →  汇总吞吐/延迟/分位数，写报告
```

其中「并发」由一个信号量（semaphore）控制：`--concurrency N` 表示同时最多 N 个请求在飞。这是测吞吐的关键旋钮——并发越高越能压满 GPU，但单请求延迟也越高。

吞吐与延迟的此消彼长，可以用一个极简的关系来理解（仅作直觉，非精确模型）：设单请求在无竞争下的服务时间为 \(\tau\)，并发度为 \(c\)，系统的「服务容量」为 \(C\)（单位时间内能处理的请求数上限）。当 \(c \cdot \tau\) 远小于容量时，每请求延迟近似 \(\tau\)，而聚合吞吐近似

\[
T(c) \approx \frac{c}{\tau} \quad (\text{未饱和区，吞吐随并发线性增长})
\]

一旦 \(c\) 大到把 GPU 压满（\(c \cdot \tau \geq 1\) 量级），吞吐触顶 \(T \to C\)，而每请求延迟则因排队开始上升：

\[
L(c) \uparrow ,\quad T(c) \to C \quad (\text{饱和区})
\]

`throughput` 子命令就是帮你找到这两个区间交界点的工具。

#### 4.2.3 源码精读

**通用执行设置 `GeneralExecSettings`**：它把子命令的一堆选项收拢成一个 Pydantic 对象，`extra="ignore"` 表示容错忽略未定义字段。注意它对一些字段做了别名，比如 `kv_cache_percent` 同时接受 `kv_cache_free_gpu_mem_fraction` 这个名字：[benchmark/__init__.py:41-100](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/bench/benchmark/__init__.py#L41-L100)。

**CLI → LlmArgs 字段名映射**：这是连接本讲与 [u4-l1](u4-l1-llm-args-hierarchy.md) 的关键。`trtllm-bench` 的 click 选项名（如 `--tp`）和 `TorchLlmArgs` 的字段名（如 `tensor_parallel_size`）并不总是一样，因此有一张翻译表：[benchmark/__init__.py:20-38](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/bench/benchmark/__init__.py#L20-L38)：

```python
_BENCH_CLICK_TO_LLM_ARG = {
    "tp": "tensor_parallel_size",
    "pp": "pipeline_parallel_size",
    "ep": "moe_expert_parallel_size",
    "cluster_size": "moe_cluster_parallel_size",
    "kv_cache_free_gpu_mem_fraction": "free_gpu_memory_fraction",
    "enable_chunked_context": "enable_chunked_prefill",
}
```

`collect_explicit_cli_keys()` 用 click 的 `ParameterSource.COMMANDLINE` 判断**哪些参数是用户在命令行上亲手敲的**（而不是用了默认值），再按上表翻译成 `TorchLlmArgs` 字段名。这张「用户显式敲了哪些键」的集合，后面会决定 CLI 能不能覆盖 YAML。

**`RuntimeConfig` 与 `get_llm_args`**：这是「翻译链」的中枢。`get_llm_args()` 把调度器配置、并行度、KV cache、解码配置等打包成一个字典，**最后一步**调用 `update_llm_args_with_extra_options` 把 YAML 配置合并进来：[configuration.py:41-110](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/bench/dataclasses/configuration.py#L41-L110)。注意第 90-93 行：

```python
updated_llm_args = update_llm_args_with_extra_options(
    llm_args,
    self.extra_llm_api_options,
    explicit_cli_keys=self.explicit_cli_keys)
```

`self.explicit_cli_keys` 就是上一步收集的「用户显式敲的键」。它会被传给合并函数来仲裁优先级。

**CLI vs YAML 优先级规则**：合并函数 `update_llm_args_with_extra_dict` 的 docstring 把规则说得很直白——「If `explicit_cli_keys` is provided, those CLI flag names override any conflicting YAML values. If `explicit_cli_keys` is None, YAML wins on conflicts.」：[llm_args.py:5872-5886](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L5872-L5886)。实现上，它先把 YAML 里被 CLI 显式认领的键**剔除**（并在值真的不同时打一条 warning 提醒用户），再做字典合并：[llm_args.py:5948-5963](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L5948-L5963)。

> 这条规则是**整个仓库统一**的——`trtllm-bench` 和 `trtllm-serve` 用的是同一个 `update_llm_args_with_extra_options`。这就是为什么「数据库里的一份 YAML，既能给 bench 测、又能给 serve 部署」：两边的配置语义和优先级完全一致。

**启发式算 `max_batch_size` / `max_num_tokens`**：如果用户没显式给这两个值，`get_settings` 会调用 `tuning/` 里的启发式，根据模型配置、数据集平均序列长度、KV cache 显存占比等猜出一组合理值：[utils/general.py:69-167](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/bench/benchmark/utils/general.py#L69-L167)。同一函数里还会读 YAML 里的 `kv_cache_config`、`enable_chunked_prefill` 等（第 87-103 行）。这也是为什么「`--config` 一份 YAML 能同时影响 KV cache dtype、chunked prefill 等众多开关」。

**异步基准循环**：真正发请求、计时的地方是 `async_benchmark`，它内部用 `LlmManager` 管理一个并发信号量来控制 `--concurrency`：[asynchronous.py:45-46](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/bench/benchmark/utils/asynchronous.py#L45-L46)（`asyncio.Semaphore(concurrency) if concurrency > 0 else None`）。`concurrency <= 0` 表示不限并发（把所有请求尽量同时发出去，用于压满吞吐）。

**throughput 主干**：把上面这些串起来，[throughput.py:316-548](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/bench/benchmark/throughput.py#L316-L548) 是完整的吞吐子命令函数体。它的几个关键节点：第 330 行 `get_general_cli_options` 收集设置；第 358-376 行读数据集；第 392 行 `get_settings` 跑启发式；第 444 行构造 `RuntimeConfig`；第 470 行 `get_llm` 造引擎；第 491-508 行先 warmup（预热，避免冷启动影响计时）；第 511-521 行正式 `asyncio.run(async_benchmark(...))`；第 528-539 行出报告。

> warmup 这一步很容易被新手忽略：第一批请求会触发 CUDA Graph 捕获、kernel 编译、KV cache 池预热等一次性开销。如果不先 warmup，这些开销会被算进吞吐里，数字会严重偏低（见 [u10-l4](u10-l4-cuda-graph-and-compile.md) 讲的 CUDA Graph 捕获成本）。

#### 4.2.4 代码实践

**实践目标**：理解吞吐基准里「并发度」对结果的影响，以及 warmup 的必要性。这是一个**源码阅读 + 思考型实践**，不要求你有 8 卡 B200。

**操作步骤**：

1. 打开 [throughput.py:316-548](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/bench/benchmark/throughput.py#L316-L548)，对照 4.2.2 的主干流程图，在源码里逐行标注「这是第几步」。
2. 找到 `--concurrency` 选项的定义（[throughput.py:246-252](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/bench/benchmark/throughput.py#L246-L252)），看清它的默认值与含义（`<=0` 表示不限并发）。
3. 追踪 `options.concurrency` 是怎么一路传到 `async_benchmark` 的（经第 512-521 行）。
4. 设计两次「 мысленно（心里推演）」的基准：
   - A：`--concurrency 1`（每次只发 1 个请求）。
   - B：`--concurrency 64`（同时 64 个请求在飞）。

**需要观察的现象**（据源码与异步循环推断）：

- A 的聚合吞吐低（GPU 大部分时间在等单个请求），但每请求延迟接近最优。
- B 的聚合吞吐高（请求被充分并行），但每请求延迟因排队上升。
- 两次都应先跑 `--warmup` 指定的请求数（默认 2），否则首批请求的 CUDA Graph 捕获开销会污染前几个采样。

**预期结果**：能用自己的话讲清「为什么并发度是吞吐基准的第一旋钮」「为什么 warmup 不可省」。**待本地验证**：真实吞吐/延迟数字依赖具体模型与硬件，本实践只要求理解趋势，不要求给出具体数值。

#### 4.2.5 小练习与答案

**练习 1**：`trtllm-bench` 的 `--tp` 和 `TorchLlmArgs.tensor_parallel_size` 是同一个东西吗？中间发生了什么？

> **参考答案**：是同一个语义，但名字不同。`_BENCH_CLICK_TO_LLM_ARG` 把 click 选项名 `tp` 翻译成 `TorchLlmArgs` 字段名 `tensor_parallel_size`；`collect_explicit_cli_keys` 据此把「用户敲了 `--tp`」翻译成「用户显式设置了 `tensor_parallel_size`」，从而在 CLI/YAML 合并时让 CLI 值覆盖 YAML。

**练习 2**：如果用户既在 YAML 里写了 `tensor_parallel_size: 8`，又在命令行敲了 `--tp 4`，最终生效的是几？会有提示吗？

> **参考答案**：生效的是 4（CLI 优先）。合并函数会发现 `tensor_parallel_size` 被 CLI 显式认领、且 YAML 值（8）与 CLI 值（4）不同，于是打一条 warning：`Explicit CLI flag(s) ['tensor_parallel_size'] override the value(s) set in the YAML config; CLI takes precedence.`（见 [llm_args.py:5956-5959](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L5956-L5959)）。

---

### 4.3 配置数据库与 Pareto 配方

#### 4.3.1 概念说明

`trtllm-bench` 帮你**测**出某套配置的性能；但「**该测哪些配置**」「**测完后该用哪套部署**」是另一回事。配置数据库就是来回答这个的。

它位于 `examples/configs/database/`，核心是一份索引文件 `lookup.yaml`（共 204 条记录）加上每个模型/GPU 目录下的大量 YAML 配置文件。每条记录叫做一个**配方（recipe）**，描述一个具体的「工作点」：

> 「模型 X + GPU Y + N 张卡 + 输入长度 ISL + 输出长度 OSL + 并发度 C」→ 用这份 YAML 配置。

这五个维度（模型、GPU、卡数、ISL/OSL、并发度）合起来叫**工作负载键（workload key）**。对同一个工作负载键，团队会在 bench 里扫一系列配置，保留 Pareto 前沿上的那几套，并用 `profile` 字段给它们贴标签：

| 画像 | 含义 | 经验并发区间 |
|------|------|------------|
| `latency` | 最小延迟档：低并发、追求单请求最快 | 并发较低 |
| `balanced` | 均衡档：兼顾吞吐与延迟 | 中等并发 |
| `throughput` | 最大吞吐档：高压并发、追求总产出 | 并发较高 |

> 别和 `examples/configs/curated/`（curated lookup.yaml，19 条）搞混：**curated** 是「人工精调、开箱即用」的少量推荐配方；**database** 是「扫参扫出来的 Pareto 全集」。两者数据模型不同（`CuratedRecipe` 字段更少）。本讲聚焦 database。

这个数据库的妙处在于：它存的 YAML **就是 `TorchLlmArgs` 字段**，因此可以直接当 `trtllm-serve --config` 的输入。换句话说，配置库 = 一堆「经过验证、按场景分好类的 `--config` 起点」。

#### 4.3.2 核心流程

从配置库里挑一条配方并部署的流程：

```text
1. 在 lookup.yaml 里按 (model, gpu, num_gpus, isl, osl, concurrency) 找配方
        │
        ▼
2. （可选）用 database.py 的 select_key_recipes 选出三档画像代表
        │
        ▼
3. Recipe.load_config() 读出 config_path 指向的 YAML 字典
        │
        ▼
4a. trtllm-serve --model <M> --config <那份 YAML>     （部署）
4b. trtllm-bench --model <M> throughput --extra_llm_api_options <那份 YAML>  （复测）
```

`database.py` 本身**没有 CLI**，它是一个纯库（library），被 `scripts/generate_config_table.py`（生成文档表格）和 `scripts/generate_config_database_tests.py`（生成测试）调用。它的价值是：用 Pydantic 给 `lookup.yaml` 做**强校验**，防止有人往索引里写错字段。

#### 4.3.3 源码精读

**`Recipe` 模型**：一条配方的字段。注意 `config_path` 是相对仓库根的路径，且校验器禁止绝对路径和 `..` 穿越（安全护栏）：[database.py:95-159](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/examples/configs/database/database.py#L95-L159)。其中 `load_config()`（第 149-159 行）负责把 `config_path` 解析成仓库根下的绝对路径并 `yaml.safe_load` 出来：

```python
def load_config(self) -> dict[str, Any]:
    config_relative_path = Path(self.config_path)
    if config_relative_path.is_absolute() or ".." in config_relative_path.parts:
        raise ValueError(f"Invalid config path: {self.config_path}")
    full_path = REPO_ROOT / self.config_path
    ...
```

`REPO_ROOT` 由 `database.py` 自身位置向上回溯四级得到（[database.py:35](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/examples/configs/database/database.py#L35)），保证无论从哪里调用都能定位到仓库根。

**画像与冲突校验**：`RecipeList._validate_conflict_profiles` 是配置库一致性的关键。它按工作负载键（model, gpu, num_gpus, isl, osl, concurrency）分组：如果一个键只对应 1 条配方，不允许带 `profile`；如果对应多条（即「冲突键」），**必须**恰好各有一条 `latency` 和 `throughput`，外加可选的 `balanced`：[database.py:163-195](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/examples/configs/database/database.py#L163-L195)。这条规则确保「同一工作负载键下，多份配置一定是按画像区分的」，不会出现两份配置争同一个键却无法区分的混乱。

**画像分配启发式**：`assign_profile` 根据一条配方「在按并发排序的列表里的位置」给它贴标签。对单条配方，用两个阈值判断：[database.py:211-229](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/examples/configs/database/database.py#L211-L229)：

```python
LOW_LATENCY_CONCURRENCY_THRESHOLD = 8
HIGH_THROUGHPUT_CONCURRENCY_THRESHOLD = 32
```

含义很直白：并发 ≤ 8 倾向「低延迟」，≥ 32 倾向「高吞吐」，中间是「均衡」。对多条配方，则按排序位置取首（Min Latency）、尾（Max Throughput）、中间（Balanced）。`select_key_recipes`（[database.py:232-252](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/examples/configs/database/database.py#L232-L252)）就是用这套规则从一堆配方里挑出三档代表，供生成文档表格用。

**溯源字段**：`Recipe` 还有两个可选字段 `validated_trtllm_commit`（40 位 Git SHA）和 `validated_trtllm_version`（版本号），记录「这条配方是在哪个 commit/版本上验证过的」。校验器要求**两者必须同时出现或同时缺省**：[database.py:141-147](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/examples/configs/database/database.py#L141-L147)。这是重要的可追溯性信息——配置库的数字绑定特定版本，升级版本后需要重测。

**一份真实配置长什么样**：以 DeepSeek-R1-0528 在 B200_NVL、8 卡、ISL=OSL=1024、并发 1 的配方为例，其 YAML 内容是：[1k1k_tp8_conc1.yaml](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/examples/configs/database/deepseek-ai/DeepSeek-R1-0528/B200/1k1k_tp8_conc1.yaml)：

```yaml
max_batch_size: 512
cuda_graph_config:
  enable_padding: true
  max_batch_size: 1
kv_cache_config:
  dtype: fp8
  free_gpu_memory_fraction: 0.8
moe_config:
  backend: TRTLLM
speculative_config:
  decoding_type: MTP
  max_draft_len: 3
tensor_parallel_size: 8
moe_expert_parallel_size: 8
max_num_tokens: 3136
max_seq_len: 2068
```

可以看到，它全是 `TorchLlmArgs` 的字段：`tensor_parallel_size`（[u9-l1](u9-l1-mapping-and-parallelism.md) 的 TP）、`kv_cache_config`（[u7-l1](u7-l1-paged-kv-cache-manager.md) 的 KV cache）、`moe_config.backend`（[u10-l1](u10-l1-moe-architecture-backends.md) 的 MoE 后端）、`speculative_config`（[u10-l3](u10-l3-speculative-decoding.md) 的投机解码 MTP）、`cuda_graph_config`（[u10-l4](u10-l4-cuda-graph-and-compile.md)）。**这份 YAML 既是 bench 的 `--extra_llm_api_options`，也是 serve 的 `--config`。** 这是本讲最核心的一句话。

**索引文件**：`lookup.yaml` 把上面这种 YAML 文件按配方登记，每条对应一个并发度，形成一条「并发扫描曲线」。[lookup.yaml:1-16](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/examples/configs/database/lookup.yaml#L1-L16) 是 DeepSeek-R1-0528 / B200_NVL / 1k1k 工作点下，并发从 1 扫到 512 的前几条；同目录后面还有并发 1024、2048 的配方，以及 1k8k、8k1k 等其它 ISL/OSL 组合。

#### 4.3.4 代码实践

**实践目标**：用 `lookup.yaml` 找出一个目标模型/GPU 的 Pareto 配方，把它改造成 `trtllm-serve --config` 起点，并设计一次吞吐基准命令行。这是本讲的主实践。

**操作步骤**：

1. **定位配方**：打开 [examples/configs/database/lookup.yaml](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/examples/configs/database/lookup.yaml)，按你的目标筛一条。例如目标 = DeepSeek-R1-0528 / B200_NVL / 8 卡 / ISL=1024 / OSL=1024 / 中等并发，可挑 `concurrency: 32` 那条，记下它的 `config_path`：
   ```
   examples/configs/database/deepseek-ai/DeepSeek-R1-0528/B200/1k1k_tp8_conc32.yaml
   ```
2. **读出配置**：用 `database.py` 的库接口把这条配方解析成 YAML 字典。示例代码（**示例代码**，可直接运行于仓库根）：
   ```python
   # 示例代码：从 lookup.yaml 取一条配方并打印其配置
   from pathlib import Path
   from examples.configs.database.database import RecipeList, DATABASE_LIST_PATH

   recipes = RecipeList.from_yaml(DATABASE_LIST_PATH)
   target = next(
       r for r in recipes
       if r.model == "deepseek-ai/DeepSeek-R1-0528"
       and r.gpu == "B200_NVL"
       and r.isl == 1024 and r.osl == 1024
       and r.concurrency == 32
   )
   print("config_path:", target.config_path)
   cfg = target.load_config()          # 读出 YAML 字典
   print("tensor_parallel_size:", cfg["tensor_parallel_size"])
   print("kv_cache dtype:", cfg["kv_cache_config"]["dtype"])
   ```
3. **当 serve 起点**：把这份 YAML 直接喂给 `trtllm-serve`（CLI 显式参数仍可覆盖其中任意字段）：
   ```bash
   trtllm-serve deepseek-ai/DeepSeek-R1-0528 \
     --config examples/configs/database/deepseek-ai/DeepSeek-R1-0528/B200/1k1k_tp8_conc32.yaml \
     --port 8000
   ```
   如果你只有更少的卡，可以在命令行**覆盖** TP（CLI 优先级高于 YAML）：
   ```bash
   trtllm-serve deepseek-ai/DeepSeek-R1-0528 \
     --config examples/configs/database/deepseek-ai/DeepSeek-R1-0528/B200/1k1k_tp8_conc32.yaml \
     --tp 2
   ```
4. **设计吞吐基准命令行**：用同一份 YAML 在 `trtllm-bench` 下复测（注意 bench 用 `--extra_llm_api_options` 或其别名 `--config`）：
   ```bash
   # 先准备一个数据集（合成正态分布，均值 ISL≈1024 / OSL≈1024）
   trtllm-bench --model deepseek-ai/DeepSeek-R1-0528 prepare-dataset token-norm-dist \
     --num-requests 1000 --input-mean 1024 --input-stdev 100 \
     --output-mean 1024 --output-stdev 100 \
     --output /tmp/ds_r1_1k1k.json

   # 再跑吞吐基准，并发 32，TP 由 YAML（=8）给定
   trtllm-bench --model deepseek-ai/DeepSeek-R1-0528 --workspace /tmp/bench \
     throughput \
       --backend pytorch \
       --extra_llm_api_options examples/configs/database/deepseek-ai/DeepSeek-R1-0528/B200/1k1k_tp8_conc32.yaml \
       --dataset /tmp/ds_r1_1k1k.json \
       --concurrency 32 \
       --warmup 20 \
       --report_json /tmp/bench_report.json
   ```

**需要观察的现象**：

- 第 2 步：脚本打印出 `config_path`、`tensor_parallel_size: 8`、`kv_cache dtype: fp8`，证明配方确实解析到了正确的 YAML。
- 第 3 步：`trtllm-serve` 正常起服务；若敲了 `--tp 2`，日志里应出现一条 warning 提示 `tensor_parallel_size` 被 CLI 覆盖（前提是 YAML 值 8 与 CLI 值 2 不同）。
- 第 4 步：基准跑完后，终端会打印一张含吞吐（tokens/s）、各分位延迟（p50/p90/p99）的统计表，并写出 `/tmp/bench_report.json`。

**预期结果**：能复现「测出的吞吐/延迟」与该配方所标注的工作点一致的数量级。**待本地验证**：具体数值依赖真实 8 卡 B200 环境与模型权重；若无此硬件，第 2、3、4 步的命令结构仍可学习，但数字无法给出。注意 DeepSeek-R1 是大模型，单卡装不下，步骤 3/4 仅在多卡大显存环境下可实际运行。

#### 4.3.5 小练习与答案

**练习 1**：`lookup.yaml` 里同一个工作负载键（model/gpu/num_gpus/isl/osl/concurrency 全相同）允许出现两条记录吗？

> **参考答案**：允许，但**必须**用 `profile` 字段区分，且这两条必须恰好一条是 `latency`、一条是 `throughput`（可再加一条 `balanced`）。这是 `_validate_conflict_profiles` 的硬性要求（[database.py:163-195](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/examples/configs/database/database.py#L163-L195)）。如果只有一条记录，则不允许带 `profile`。

**练习 2**：为什么 `Recipe` 要校验 `validated_trtllm_commit` 必须是 40 位 SHA、且与 `validated_trtllm_version` 成对出现？

> **参考答案**：配置库里的性能数字是**绑定到特定代码版本**的——换一个 commit，kernel、调度器、KV cache 行为都可能变，老数字就不再可信。40 位 SHA 精确定位 commit，版本号给人读。成对出现是为了避免「只填了 SHA 没填版本」这种半截溯源信息，保证每条配方的出处要么完整、要么留空。

**练习 3**：配置库里的一份 YAML，能同时用于 `trtllm-serve` 和 `trtllm-bench` 吗？为什么？

> **参考答案**：能。因为这份 YAML 的字段就是 `TorchLlmArgs`，而 serve 的 `--config` 和 bench 的 `--extra_llm_api_options`（别名也是 `--config`）走的是**同一个**合并函数 `update_llm_args_with_extra_options`，优先级规则也一致（CLI 显式键覆盖 YAML）。所以同一份 YAML 在两边语义完全相同。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个小任务：**为一个新的工作负载，走一遍「查配方 → 改配方 → 部署 → 复测」的完整闭环**。

**任务背景**：假设你要在 8 卡 B200 上部署 `deepseek-ai/DeepSeek-R1-0528`，但你的业务以**长输入、短输出**为主（ISL≈8192, OSL≈1024），且你更关心吞吐而非单请求延迟。

**要求**：

1. **查**：在 [lookup.yaml](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/examples/configs/database/lookup.yaml) 里找到 `isl: 8192, osl: 1024, gpu: B200_NVL, num_gpus: 8` 的工作点。观察它扫了哪些并发度（参考 [lookup.yaml:193-199](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/examples/configs/database/lookup.yaml#L193-L199) 起的段落）。挑一个**高并发**配方（对应 throughput 画像）。
2. **改**：用 4.3.4 第 2 步的示例脚本读出该配方的 YAML 字典；如果某个字段（如 `kv_cache_config.free_gpu_memory_fraction`）不适合你的环境，记下你想改的值。
3. **部署**：写出 `trtllm-serve --config <那份 YAML>` 的命令行；说明若你想临时改一个字段，应该改在命令行还是 YAML 里（提示：CLI 优先）。
4. **复测**：写出对应的 `trtllm-bench throughput` 命令行，注意带上 `--concurrency`（与配方标注一致）、`--warmup`、`--report_json`，并先用 `prepare-dataset token-norm-dist` 造一份 ISL≈8192/OSL≈1024 的合成数据集。
5. **判读**：据 4.2.2 的吞吐-延迟关系，预测：若把并发度从你选的高并发值降到很低（如 1），吞吐和延迟各自会怎么变？

**交付物**：一份简短笔记，包含——查到的 `config_path`、你想修改的字段、`trtllm-serve` 与 `trtllm-bench` 两条命令行、以及第 5 步的预测。

> **待本地验证**：本任务命令行的正确性可以离线推演；但真实吞吐/延迟数字需要 8 卡 B200 与模型权重才能得到。若无可用的硬件环境，重点完成「查、改、写命令行、预测」四步，数字部分留空。

## 6. 本讲小结

- `trtllm-bench` 是 click 命令组（`throughput` / `latency` / `prepare-dataset` / `visual-gen`），全局选项经 `BenchmarkEnvironment` 通过 `ctx.obj` 传给子命令；它和 `trtllm-serve` 构造的是同一个 `LLM`，因此 bench 测得的数字对 serve 有参考价值。
- bench 子包按 `benchmark` / `dataclasses` / `dataset` / `tuning` / `utils` 分工；吞吐基准是一条「翻译链」：CLI 选项 → `GeneralExecSettings` → 启发式 `get_settings` → `RuntimeConfig.get_llm_args` → `LLM` → 异步 `async_benchmark` → `ReportUtility`。
- CLI 与 YAML 的优先级是**全仓库统一**的：用户显式敲的键（经 `collect_explicit_cli_keys` + `_BENCH_CLICK_TO_LLM_ARG` 翻译）覆盖 YAML，由共享的 `update_llm_args_with_extra_options` 仲裁。这正是「一份 YAML 两边通用」的根本原因。
- 配置数据库 `examples/configs/database/` 用 `Recipe`/`RecipeList` 给 `lookup.yaml`（204 条）做强校验；每条配方 = 一个工作负载键 → 一份 `TorchLlmArgs` YAML；同一键下多份配置用 `profile`（latency/balanced/throughput）区分。
- 配方里的 YAML **就是** `trtllm-serve --config` 的起点，也是 `trtllm-bench --extra_llm_api_options` 的输入；`validated_trtllm_commit/version` 记录其性能数字绑定的代码版本。
- 并发度是吞吐基准的第一旋钮：低并发偏延迟、高并发偏吞吐，二者在饱和区此消彼长；warmup 不可省，否则 CUDA Graph 捕获等一次性开销会污染统计。

## 7. 下一步学习建议

- **想更懂「为什么这些 YAML 字段能这么配」**：回头看 [u4-l1 TorchLlmArgs 与配置层级](u4-l1-llm-args-hierarchy.md)、[u7-l1 分页 KV Cache](u7-l1-paged-kv-cache-manager.md)、[u10-l4 CUDA Graph 与 torch.compile](u10-l4-cuda-graph-and-compile.md)，理解 `kv_cache_config`、`cuda_graph_config`、`speculative_config` 背后的机制。
- **想测分离式服务**：本讲的 bench 只覆盖聚合（aggregate）部署；分离式（prefill/decode disaggregation）的性能测量见 [u11-l2 分离式服务](u11-l2-disaggregated-serving.md)，可结合其配置一起做基准。
- **想给配置库加一条配方**：阅读 `scripts/generate_config_table.py` 与 `scripts/generate_config_database_tests.py`，看 `RecipeList` 是如何被消费的；新增条目时记得遵守 `_validate_conflict_profiles` 的画像规则与 `validated_trtllm_commit` 成对约束。
- **想测自定义模型/算子**：[u12-l2 自定义算子与内核](u12-l2-custom-ops-and-kernels.md) 讲过的 custom_ops 会直接影响基准数字；可用 `trtllm-bench` 的 `--custom_module_dirs` 加载你的实现后复测对比。
