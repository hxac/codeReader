# 服务启动入口与命令行参数

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `python -m lightllm.server.api_server` 这条启动命令背后到底执行了哪几步。
- 理解 `api_server.py`（入口）与 `api_cli.py`（参数解析）两份文件的分工，以及它们为什么这样拆分。
- 看懂 `run_mode`（`normal` / `prefill` / `decode` / `pd_master` / `config_server` / `visual_only`）的不同启动形态是如何被分发到不同启动函数的。
- 认识最常用的命令行参数：`--model_dir`、`--port`、`--tp`、`--dp`、`--max_total_token_num`、`--mem_fraction` 等，并知道它们的默认值与作用。
- 学会用 `--help` 自主探索 LightLLM 的全部参数。

本讲只覆盖两个最小模块：**入口文件** 与 **命令行参数解析**。这是承上（u1-l2 学会启动、u1-l3 建立代码地图）启下（u1-l5 多进程编排、u2 多进程架构）的关键一讲。

## 2. 前置知识

在进入源码前，先用通俗语言铺垫几个概念。

### 2.1 什么是「入口文件」

一个 Python 服务通常有一个「最先被运行」的脚本，称为**入口文件（entry point）**。你执行：

```bash
python -m lightllm.server.api_server --model_dir /path/to/model
```

时，`-m` 表示「把 `lightllm.server.api_server` 当作模块来运行」，于是 Python 会去执行 `lightllm/server/api_server.py` 这个文件。入口文件本身通常写得很薄——它只负责「读命令、派活」，真正干活的是后续模块。LightLLM 就是这种风格：`api_server.py` 只有十几行。

### 2.2 什么是「命令行参数」

你在命令行里用 `--model_dir`、`--port` 这种 `--` 开头的写法传给程序的值，就是**命令行参数**。Python 标准库 `argparse` 专门用来定义和解析这些参数。LightLLM 把「定义参数」这件事单独放在 `api_cli.py` 里的一个函数 `make_argument_parser()` 中，`api_server.py` 调用它来拿到解析器。

### 2.3 多进程与 spawn

LightLLM 是多进程架构（见 u1-l1）。Python 启动子进程有两种主流方式：

- `fork`：复制父进程内存，速度快，但和 CUDA（GPU 显存）混用容易出问题。
- `spawn`：重新启动一个干净的 Python 解释器，安全，是 GPU 程序的推荐做法。

所以入口文件里会显式设置 `spawn`。这点在 4.1 节会看到。

## 3. 本讲源码地图

本讲只涉及两个文件，它们都位于 `lightllm/server/` 下：

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `lightllm/server/api_server.py` | 约 18 行 | **入口文件**：设置多进程启动方式、解析参数、按 `run_mode` 分发到具体启动函数。 |
| `lightllm/server/api_cli.py` | 约 860 行 | **参数定义**：用 `argparse` 集中定义全部命令行参数（含默认值、取值范围、help 文本）。 |

此外，入口会把控制权交给 `lightllm/server/api_start.py` 里的启动函数（`normal_or_p_d_start` / `pd_master_start` / `visual_only_start` / `config_server_start`）。`api_start.py` 是 u1-l5 的主角，本讲只在「分发」这一步提到它。

## 4. 核心概念与源码讲解

### 4.1 入口文件 api_server.py

#### 4.1.1 概念说明

`api_server.py` 是整个服务的「门」。它做三件事：

1. 设置多进程启动方式为 `spawn`（GPU 程序的安全选择）。
2. 构造参数解析器，把用户在命令行敲的 `--xxx` 解析成一个 `args` 对象。
3. 根据 `args.run_mode` 把 `args` 交给对应的「启动函数」，由后者去拉起各个子进程。

它故意写得很短，因为「读命令、派活」之外的细节都不该出现在入口里——这样入口职责单一，后续模块（`api_start.py`）可以独立演化。

> 承接 u1-l3：入口文件刻意保持极简，只做设备/进程适配与分发，这与 LightLLM「分层清晰」的代码组织风格一致。

#### 4.1.2 核心流程

入口的执行流程可以用下面这段伪代码概括：

```
导入 torch（因为要用它的多进程模块）
↓
from .api_cli import make_argument_parser
↓
if __name__ == "__main__":
    设置多进程 start method = spawn
    parser = make_argument_parser()
    args   = parser.parse_args()           # 读取命令行 --xxx
    按需导入 4 个启动函数（延迟导入）
    根据 args.run_mode 选择一个启动函数(args)
```

分发规则（关键）：

| `run_mode` 取值 | 调用的启动函数 | 含义 |
| --- | --- | --- |
| `normal` / `prefill` / `decode` | `normal_or_p_d_start` | 单机完整服务，或 PD 分离中的 prefill/decode 角色 |
| `pd_master` | `pd_master_start` | PD 分离的调度主节点 |
| `config_server` | `config_server_start` | 用于大规模场景下注册/发现 pd_master 节点 |
| `visual_only` | `visual_only_start` | 仅启动视觉推理进程（多模态） |

注意：`normal`、`prefill`、`decode` 三者都走 `normal_or_p_d_start`，由该函数内部再根据 `run_mode` 区分行为。

#### 4.1.3 源码精读

整个入口文件只有十几行，逐段看：

[lightllm/server/api_server.py:1-2](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_server.py#L1-L2)：顶部导入 `torch` 和参数解析器的构造函数。这里只导入了「构造解析器」的函数，**尚未**导入任何启动逻辑。

[lightllm/server/api_server.py:4-7](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_server.py#L4-L7)：`__main__` 守卫。第 5 行显式把多进程启动方式设为 `spawn`，注释说明「fork 子进程的设定在这里不适用」——这正是 2.3 节提到的 GPU 场景安全选择。第 6–7 行构造解析器并解析命令行，得到 `args`。

[lightllm/server/api_server.py:8](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_server.py#L8)：**延迟导入**——4 个启动函数在这里才被 import。这是一种常见技巧：把较重的启动相关代码推迟到真正要启动时再加载，既加快了 `--help` 等不启动服务的场景，也避免了在「只是想解析参数」时触发不必要的模块依赖。

[lightllm/server/api_server.py:10-17](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_server.py#L10-L17)：`run_mode` 分发。读法是「先匹配特殊形态，其余都走默认」：

- `pd_master` → `pd_master_start`
- `config_server` → `config_server_start`
- `visual_only` → `visual_only_start`
- 其它（含 `normal`、`prefill`、`decode`）→ `normal_or_p_d_start`

这 4 个启动函数都定义在 `api_start.py` 中，已被源码确认存在（`normal_or_p_d_start` 在 [api_start.py:74](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L74)，`pd_master_start` 在 [api_start.py:528](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L528)，`visual_only_start` 在 [api_start.py:593](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L593)，`config_server_start` 在 [api_start.py:649](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L649)）。

#### 4.1.4 代码实践

**目标**：观察「延迟导入」与「run_mode 分发」的真实行为。

**操作步骤**：

1. 打开 `lightllm/server/api_server.py`，定位第 8 行的延迟导入。
2. 思考：为什么第 8 行不放在文件顶部？如果放顶部，`python -m lightllm.server.api_server --help` 会不会变慢、甚至因缺少 GPU 驱动而报错？
3. 在第 10–17 行的 `if/elif` 上方手动「模拟」一次分发：假设用户传了 `--run_mode decode`，沿着代码指出它会落入哪个分支（答案是最后的 `else` → `normal_or_p_d_start`）。

**需要观察的现象**：

- `--help` 能否在不拉起 GPU 进程的前提下打印出帮助？为什么？（提示：`parse_args()` 对 `--help` 会直接退出，根本到不了第 8 行的导入。）

**预期结果**：

- `--run_mode decode` 与 `--run_mode normal` 走的是**同一个**启动函数，区别由 `normal_or_p_d_start` 内部读取 `args.run_mode` 来处理（参见 [api_start.py:88-89](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L88-L89)，函数会过滤只接受 `normal/prefill/decode/visual_only`）。

#### 4.1.5 小练习与答案

**练习 1**：如果把第 5 行 `set_start_method("spawn")` 删掉，服务还能启动吗？会有什么隐患？

**参考答案**：通常仍能启动（Python 会使用平台默认方式，Linux 上默认是 `fork`）。但 `fork` 会复制父进程的 CUDA 上下文，多 GPU 子进程场景下极易出现 CUDA 错误或死锁。注释明确指出 fork 设定「不适用」，所以这是为了多进程 + GPU 的安全而设的。详见 [api_server.py:5](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_server.py#L5)。

**练习 2**：`api_server.py` 里既没有 `import argparse`，也没有 `argparse.ArgumentParser(...)`，那参数是从哪来的？

**参考答案**：入口把「构造解析器」这件事委托给了 `api_cli.make_argument_parser()`（[api_server.py:2](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_server.py#L2) 与 [api_server.py:6](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_server.py#L6)）。入口只负责「拿到解析器并 parse」，不关心参数细节。这种拆分让 `api_cli.py` 可被单独测试、复用。

**练习 3**：为什么 4 个启动函数要延迟到 `__main__` 内部才导入（第 8 行），而不是放文件顶部？

**参考答案**：这样 `--help`、单测等「只想解析参数、不想真启动」的路径就不会触发 `api_start` 及其下游（含大量 GPU/进程相关）的导入，既快又稳。这是「按需加载」的典型写法。

### 4.2 命令行参数解析 api_cli.py

#### 4.2.1 概念说明

`api_cli.py` 只导出一个函数 `make_argument_parser()`，它返回一个配好所有参数的 `argparse.ArgumentParser`。LightLLM 有上百个命令行参数，全部集中在这个函数里通过 `parser.add_argument(...)` 注册。每个注册包含四要素：

- 参数名（如 `--port`）
- 类型（`int` / `str` / `float` / `store_true` 开关）
- 默认值（`default=...`）
- 帮助文本（`help=...`）

把所有参数集中在一个函数里有三个好处：① 用户只需看一个文件就能掌握全部配置；② 任何启动形态（normal / pd / 单测）都复用同一份参数定义，保证一致；③ 配合 `--help` 形成自带文档。

> 承接 u1-l2：你在那一讲用到的 `--model_dir`，正是这里 [api_cli.py:110](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L110) 注册的参数，默认值为 `None`（即必填）。

#### 4.2.2 核心流程

`make_argument_parser()` 的执行就是「顺序注册一堆参数」。为了便于理解，可以把上百个参数按用途分成下面几类（这是本讲帮你建立的**心智地图**，不必背）：

| 类别 | 代表参数（含默认值） | 作用 |
| --- | --- | --- |
| 服务监听 | `--host 127.0.0.1`、`--port 8000`、`--httpserver_workers 1`、`--zmq_mode ipc:///tmp/` | HTTP 服务绑定的地址与端口，进程间 zmq 通信方式 |
| 启动形态 | `--run_mode normal` 及 PD 相关 `--pd_master_ip/port`、`--select_p_d_node_strategy` | 决定本进程扮演什么角色 |
| 模型加载 | `--model_dir None`、`--tokenizer_mode fast`、`--load_way HF`、`--data_type None` | 从哪加载权重、用什么精度 |
| 并行 | `--tp 1`、`--dp 1`、`--dp_balancer bs_balancer`、`--nnodes 1`、`--node_rank 0`、`--nccl_host/port` | 张量并行 / 数据并行 / 多机 |
| 容量与内存 | `--max_total_token_num None`、`--mem_fraction 0.8`、`--batch_max_tokens None`、`--max_req_total_len None` | 决定能同时缓存/处理多少 token |
| 调度 | `--schedule_time_interval 0.03`、`--router_token_ratio None`、`--router_max_wait_tokens 1`、`--running_max_req_size 256` | Router 调度循环的节奏与策略 |
| 性能 | `--disable_cudagraph`、`--enable_prefill_cudagraph`、`--enable_tpsp_mix_mode`、`--enable_prefill_microbatch_overlap`、`--enable_decode_microbatch_overlap` | CUDA Graph、TPSP、microbatch overlap 等加速 |
| 注意力后端 | `--llm_prefill_att_backend auto`、`--llm_decode_att_backend auto`、`--vit_att_backend auto` | 选择 fa3/flashinfer/triton 等 kernel |
| 量化 | `--quant_type none`、`--llm_kv_type None`、`--expert_dtype None`、`--kv_quant_calibration_config_path None` | 权重/专家/KV cache 量化 |
| 多模态 | `--disable_vision`、`--disable_audio`、`--visual_gpu_ids`、`--audio_gpu_ids`、`--embed_cache_storage_size 4` | 视觉/音频嵌入与缓存 |
| MTP 推测解码 | `--mtp_mode None`、`--mtp_draft_model_dir None`、`--mtp_step 0` | Multi-Token Prediction |
| 多级缓存 | `--enable_cpu_cache`、`--cpu_cache_storage_size 2`、`--enable_disk_cache`、`--disk_cache_storage_size 10` | KV cache 卸载到 CPU/磁盘 |
| 监控 | `--health_monitor`、`--metric_gateway None`、`--job_name lightllm`、`--detail_log` | 健康检查与指标上报 |

本讲重点是**最常用的几个**（下节逐个精读），其余在后续单元（u2 调度、u6 性能、u7 分布式与扩展）会陆续用到。

#### 4.2.3 源码精读

先看解析器的定义与第一个、也是最关键的参数 `--run_mode`：

[lightllm/server/api_cli.py:4-5](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L4-L5)：函数签名，返回 `argparse.ArgumentParser`。

[lightllm/server/api_cli.py:7-23](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L7-L23)：`--run_mode`，可选值 6 个，默认 `normal`。帮助文本说明了 `prefill/decode/pd_master` 用于 PD 分离，`config_server` 用于大规模高并发时注册并获取 pd_master 列表（缓解 pd_master 的 CPU 瓶颈）。

服务监听相关：

[lightllm/server/api_cli.py:34-42](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L34-L42)：`--host` 默认 `127.0.0.1`、`--port` 默认 `8000`、`--httpserver_workers` 默认 `1`、`--zmq_mode` 默认 `ipc:///tmp/`（说明只能在 `tcp://` 与 `ipc:///tmp/` 间选择）。这里就解释了 u1-l2 里「默认监听 127.0.0.1:8000」的来源。

模型加载相关：

[lightllm/server/api_cli.py:110-115](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L110-L115)：`--model_dir`，默认 `None`，是**唯一必填参数**——程序会从这里读 `config.json`、权重和 tokenizer。这正是 u1-l2 强调的「直接读 HuggingFace 格式」的入口。

并行相关：

[lightllm/server/api_cli.py:223](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L223)：`--tp` 默认 `1`，张量并行大小。

[lightllm/server/api_cli.py:224-231](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L224-L231)：`--dp` 默认 `1`。帮助文本很重要：它主要给 deepseekv2 用（让 `dp` 等于 `tp`），其它情况保持默认 `1` 即可。这提示了 `dp` 是一个「特定模型才需要动」的参数，初学者不必理会。

容量与内存相关（本讲实践任务的重点之一）：

[lightllm/server/api_cli.py:131-143](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L131-L143)：`--max_total_token_num`（默认 `None`）与 `--mem_fraction`（默认 `0.8`）。两者关系是：若不指定 `max_total_token_num`，则按 `mem_fraction` 自动算出。帮助文本给出经验公式：

\[
\text{max\_total\_token\_num} \;\approx\; \text{max\_batch} \times (\text{input\_len} + \text{output\_len})
\]

直观理解：GPU 能同时缓存的 token 总量 ≈ 并发请求数 × 单请求（输入+输出）长度。这个总量直接决定系统并发能力，是后续 u4（KV 内存管理）的核心配置。

调度节奏：

[lightllm/server/api_cli.py:727-732](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L727-L732)：`--schedule_time_interval` 默认 `0.03`（30ms）。这正是 u2-l5 将讲的「Router 以固定时间间隔驱动的事件循环」的节拍来源。

最后看一个对初学者很有教育意义的「废弃参数」：

[lightllm/server/api_cli.py:315-318](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L315-L318)：`--use_dynamic_prompt_cache` 被标注为 `deprecated and no longer in use`（已废弃、不再生效），而它的「反义开关」`--disable_dynamic_prompt_cache` 才是当前生效的。保留废弃参数是为了向后兼容旧脚本——这在大型项目里很常见，也是你读 `--help` 时需要留意的细节。

#### 4.2.4 代码实践

**目标**：用 `--help` 自主探索全部参数，并把实践任务要求的 5 个参数说清楚。

**操作步骤**：

1. 在已按 u1-l2 安装好 LightLLM（含 torch）的环境中执行：

   ```bash
   python -m lightllm.server.api_server --help
   ```

   > 该命令只到 `parse_args()` 就会因 `--help` 退出，不会真正加载模型或申请 GPU 显存，因此即便没有 GPU 也能打印。是否在当前机器成功运行：待本地验证。

2. 在输出中找到 `--run_mode`、`--tp`、`--dp`、`--max_total_token_num`、`--port` 这 5 个，记录其默认值与 `help`。

3. 对这 5 个参数写一句中文说明（参考下表）：

   | 参数 | 默认值 | 一句话作用 |
   | --- | --- | --- |
   | `--run_mode` | `normal` | 决定本进程扮演的角色（单机 / PD 分离的 prefill 或 decode / pd_master / config_server / visual_only） |
   | `--tp` | `1` | 张量并行切分数，把模型权重按列切到多张 GPU |
   | `--dp` | `1` | 数据并行副本数（主要 deepseekv2 用，一般保持 1） |
   | `--max_total_token_num` | `None` | GPU 能同时缓存的 token 总量；不指定则按 `--mem_fraction` 自动算 |
   | `--port` | `8000` | HTTP 服务监听端口 |

**需要观察的现象**：

- `--help` 输出非常长（上百个参数），它们按 `api_cli.py` 中 `add_argument` 的注册顺序排列。
- 每个参数都标注了类型、默认值与帮助文本，相当于一份自带文档。

**预期结果**：

- 你能仅凭 `--help`，不查其它资料，说出任意一个参数的默认值和大致用途。
- 尝试用不同 `run_mode` 启动（需要真实模型与 GPU，**待本地验证**），例如：

  ```bash
  # 单机正常模式（最常用）
  python -m lightllm.server.api_server --model_dir /path/to/llama --tp 1

  # PD 分离：先起一个 pd_master，再分别起 prefill 与 decode 角色
  python -m lightllm.server.api_server --run_mode pd_master --pd_master_port 1212
  python -m lightllm.server.api_server --run_mode prefill --model_dir /path/to/model --pd_master_ip <master_ip>
  python -m lightllm.server.api_server --run_mode decode  --model_dir /path/to/model --pd_master_ip <master_ip>
  ```

  根据本节学到的分发规则，前两条 `pd_master` 会进入 `pd_master_start`；后两条 `prefill`/`decode` 会进入最后的 `else` → `normal_or_p_d_start`。这一对照能否在你的环境中复现，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`--model_dir` 的默认值是 `None`，意味着什么？如果启动时不传它会怎样？

**参考答案**：`None` 表示「没有默认值」，相当于**必填**。不传的话，程序能解析参数（`args.model_dir is None`），但进入后续加载流程时会因为找不到模型目录而失败。所以实践中 `--model_dir` 几乎总是要显式给出。见 [api_cli.py:110-115](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L110-L115)。

**练习 2**：`--mem_fraction` 和 `--max_total_token_num` 是什么关系？同时指定会怎样？

**参考答案**：两者都用来约束「能缓存多少 KV」。`--mem_fraction 0.8`（默认）表示允许占用 80% 显存做 KV cache；`--max_total_token_num` 直接给出 token 总量。按帮助文本，**若不指定 `max_total_token_num`，则按 `mem_fraction` 自动估算**；若显式指定了 `max_total_token_num`，则以指定值为准（`mem_fraction` 在容量推算上被覆盖）。发生 OOM 时可调小 `--mem_fraction`。见 [api_cli.py:131-143](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L131-L143)。

**练习 3**：为什么 `--use_dynamic_prompt_cache` 还留在代码里却「不再生效」？这反映了大型项目的什么实践？

**参考答案**：为了**向后兼容**——避免升级后旧的启动脚本/文档因参数不存在而报错。常见做法是保留参数注册但让其实际无效，并用 `help` 文本标注 `deprecated`，引导用户改用新参数（这里是 `--disable_dynamic_prompt_cache`）。读 `--help` 时看到 `deprecated` 就要警觉，避免误用。见 [api_cli.py:315-318](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L315-L318)。

## 5. 综合实践

把本讲两个最小模块串起来，完成下面这个「启动命令逆向追踪」任务：

**任务**：假设有人给你这样一条启动命令（伪命令，模型路径请替换为本地真实路径）：

```bash
python -m lightllm.server.api_server \
    --model_dir /path/to/llama \
    --tp 2 \
    --port 9000 \
    --max_total_token_num 16384 \
    --run_mode normal
```

请你完成：

1. **画出从敲下回车到进入「启动函数」的完整调用链**，标注每一步发生在哪个文件、哪一行。提示链路应为：`python -m` 执行 `api_server.py` → [api_server.py:5](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_server.py#L5) 设 spawn → [api_server.py:6-7](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_server.py#L6-L7) 用 `api_cli.make_argument_parser()` 解析参数 → [api_server.py:8](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_server.py#L8) 延迟导入 → [api_server.py:16-17](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_server.py#L16-L17) 的 `else` 分支调用 `normal_or_p_d_start(args)`。

2. **写一份「参数 → 默认值 → 本命令取值 → 含义」对照表**，至少覆盖命令里出现的 5 个参数。

3. **预测**：把 `--run_mode normal` 改成 `--run_mode decode` 后，调用链里哪一行/分支会变化？哪一行不变？（答案：进入的启动函数不变——仍是最后的 `else` → `normal_or_p_d_start`；但 `args.run_mode` 的值变了，会被 `normal_or_p_d_start` 内部用 `run_mode` 字段区分行为，见 [api_start.py:88-89](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L88-L89)。）

4. **进阶**：在不实际启动服务的前提下（无 GPU 也能做），运行 `python -m lightllm.server.api_server --help`，找出任意 3 个本讲没讲过的参数，用一句话写出它们的作用，并判断它属于 4.2.2 节心智地图的哪一类。这一步能验证你是否真正掌握了「用 `--help` 自主探索」的能力。

> 说明：步骤 1–3 是纯源码阅读型实践，无需 GPU；步骤 3 的运行结果与步骤 4 的 `--help` 行为，若需确认请「待本地验证」。

## 6. 本讲小结

- `api_server.py` 是一个极薄的入口：设 `spawn` → 解析参数 → 按 `run_mode` 分发，共约十几行。
- `run_mode` 的 6 个取值中，`normal`/`prefill`/`decode` 都走 `normal_or_p_d_start`，其余三种各走一个专门的启动函数。
- 启动函数被**延迟导入**到 `__main__` 内部，保证 `--help` 等非启动路径又快又稳。
- 全部命令行参数集中在 `api_cli.make_argument_parser()` 中，每个都带类型/默认值/help，`--help` 即自带文档。
- 最常用参数：`--model_dir`（必填）、`--port`(8000)、`--tp`(1)、`--dp`(1)、`--max_total_token_num`(None) 与 `--mem_fraction`(0.8)、`--schedule_time_interval`(0.03)。
- 大型项目会保留 `deprecated` 参数以兼容旧脚本，读 `--help` 时需留意标注。

## 7. 下一步学习建议

入口把控制权交给了 `api_start.py` 里的启动函数。下一讲 **u1-l5 多进程编排启动流程** 将深入 `normal_or_p_d_start`，看它如何按顺序拉起 router、detokenization、metric 等子进程，以及端口分配与信号处理。在那之后，**u2 多进程架构与请求链路** 会把 `--port`/`--zmq_mode`/`--schedule_time_interval` 这些参数与真实运行行为一一对应起来。

建议继续阅读的源码：

- `lightllm/server/api_start.py`（`normal_or_p_d_start` 等编排函数，u1-l5 主角）。
- `lightllm/server/api_cli.py` 中你尚未读过的参数段落——对照 4.2.2 的分类，提前建立印象，等 u2/u6/u7 用到时就不会陌生。
