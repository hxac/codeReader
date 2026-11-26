# Beaker 集群执行：从一条命令到 N 个 GPU 副本

> **讲义编号**：u3-l4 ｜ **单元**：单元 3（工作队列与可扩展部署） ｜ **难度**：intermediate
> **前置讲义**：u3-l1（WorkQueue 抽象）、u3-l2（LocalBackend 与 S3Backend）、u3-l3（vLLM 服务器生命周期）、u2-l1（main() 主流程）、u1-l5（CLI 参数）

## 1. 本讲目标

前几讲里，无论 pipeline 还是 vLLM，都还跑在"一台机器"上。本讲解决的问题是：**当 PDF 有百万个、一台 GPU 机器根本跑不完时，olmOCR 如何把一条本地命令放大成集群里 N 个 GPU 副本的并行作业。**

学完本讲，你应该能够：

1. 描述 `--beaker` 提交时命令行参数如何被改写（哪些参数被剥离、为什么必须剥离）；
2. 理解 `submit_beaker_job` 中 Weka / S3 / GCS secrets 的自动探测、自动写入与缺失时的人机交互流程；
3. 逐字段解释 `BeakerExperimentSpec` 任务规格：`replicas`、`gpu_count`、`preemptible` 分别由什么控制；
4. 解释 worker 端 `BEAKER_JOB_NAME` 环境变量分支的凭证落盘逻辑，以及多 replica 错峰下载模型（stagger）的计算公式；
5. 把"本地填队 → 远程派工 → 集群消费"这条完整的分布式执行链画出来。

本讲所有内容集中在 [olmocr/pipeline.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py) 一个文件里——上一单元的"队列抽象"在本讲被真正用到了多机场景。

## 2. 前置知识

### 2.1 集群与 Beaker 是什么

**集群（cluster）** 就是很多台 GPU 机器由一个调度系统统一管理：你提交一个"作业（job）"，告诉调度器需要几张卡，调度器决定作业在哪台机器、哪个容器里跑。你不需要 SSH 到每台机器。

**Beaker** 是 Allen AI（AI2）内部的实验计算平台（olmOCR 是 AI2 的项目，README 里写明这一功能"面向 Ai2 用户"，见 [README.md:373-375](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L373-L375)）。理解本讲需要先熟悉它的几个名词：

| 术语 | 含义 | 在 olmOCR 中的对应物 |
|------|------|---------------------|
| **workspace** | Beaker 上组织实验和 secrets 的命名空间 | 默认 `ai2/olmocr`，可用 `--beaker_workspace` 改 |
| **experiment** | 一次提交的实验单位 | 由 `b.experiment.create()` 创建 |
| **task** | 实验里的任务描述（镜像、命令、资源） | 一个 experiment 里恰好一个 task |
| **replica** | 同一个 task 复制出的并行执行实例 | `--beaker_gpus 4` → 4 个 replica |
| **image** | 容器镜像 | `jakep/olmocr-inference-{VERSION}` |
| **secret** | 平台托管的敏感字符串，注入成容器内环境变量 | Weka/S3/GCS/HF 凭证 |
| **preemptible** | "可抢占"实例：便宜、优先级低，随时可能被更高优先级作业回收 | olmOCR 硬编码开启 |
| **budget** | 实验的计费账户 | 硬编码 `ai2/oe-base` |

### 2.2 为什么"多副本"在 olmOCR 里几乎是免费的

这是本讲最重要的直觉，承接 u3-l1/u3-l2 的结论：

- 所有 worker 消费的**不是任务分配器，而是一个共享的 WorkQueue**。队列的后端是 S3Backend（workspace 用 `s3://` 路径时），done flag 和 worker lock 都是 S3 上的对象（存在性即状态）。
- u3-l1 讲过：`get_work` 会对正在被别的 worker 处理的项跳过（worker lock），做完的项不再重复（done flag）。所以**在 1 台机器上开 4 个 worker 和在 4 台机器上各开 1 个 worker，对队列来说没有区别**。

因此 Beaker 模式的本质是：**每个 replica 就是一次普通的 `olmocr` 单机运行**，它们恰好通过 S3 workspace 看到同一个队列。pipeline.py 里没有任何"分布式调度"代码——分布式是 WorkQueue 抽象白送的。

### 2.3 需要的其它背景

- **S3 兼容协议**：Weka 是 AI2 的高性能文件系统，但走 S3 协议访问（自定义 endpoint），所以能复用 boto3（见 [olmocr/s3_utils.py:286-296](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/s3_utils.py#L286-L296)）。
- **凭证文件惯例**：boto3 默认读 `~/.aws/credentials`；Google 客户端库读环境变量 `GOOGLE_APPLICATION_CREDENTIALS` 指向的服务账号（SA）key 文件。worker 容器里没有这些文件，所以要在启动时现场生成——这就是"凭证落盘"。
- **环境变量两种注入方式**：`BeakerEnvVar(name="X", value="v")` 注入字面值；`BeakerEnvVar(name="X", secret="Y")` 注入 secret `Y` 的**内容**。这个区别贯穿本讲。

## 3. 本讲源码地图

| 文件 | 本讲涉及的部分 | 作用 |
|------|----------------|------|
| [olmocr/pipeline.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py) | `submit_beaker_job()`（L992-1102） | 提交端：剥参数、管 secrets、组装 spec、创建实验 |
| 同上 | Beaker 参数组 argparse（L1260-1270） | `--beaker` / `--beaker_workspace` / `--beaker_cluster` / `--beaker_gpus` / `--beaker_priority` 五个参数的定义 |
| 同上 | `main()` 中 `BEAKER_JOB_NAME` 分支（L1291-1310） | worker 端：凭证落盘 + 错峰 sleep |
| 同上 | `main()` 中 `--beaker` 短路（L1414-1420） | 提交与运行的分流点 |
| 同上 | `download_model()`（L956-980） | 错峰保护的对象：模型下载 |
| 同上 | 模块级全局（L73-75、L87、L1526-1532） | boto3 客户端、CPU 信号量、CLI 入口 |
| [olmocr/s3_utils.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/s3_utils.py) | Weka 凭证消费（L286-296） | worker 里 `WEKA_ACCESS_KEY_ID` 环境变量被谁用掉 |
| [pyproject.toml](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml) | beaker extra（L59-61）、脚本入口（L49-50） | `beaker-py` 是可选依赖；`olmocr` 命令注册 |
| [README.md](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md) | Beaker 用法（L232-237、L373-385） | 官方用法示例 |

## 4. 核心概念与源码讲解

先看全局时序图，本讲三个最小模块分别对应"提交端"、"任务规格"、"worker 端适配"：

```
你的笔记本电脑（提交机）                          Beaker 集群（N 个副本）
──────────────────────────────                  ──────────────────────────────
olmocr s3://ws --pdfs 's3://pdfs/*.pdf' \
       --beaker --beaker_gpus 4
        │
        ▼  main() 正常执行到短路点之前：
 ├─ WorkQueue(S3Backend(s3://ws))   ← u3-l1/u3-l2
 ├─ 收集 --pdfs → populate_queue    ← u2-l2（本地就把队填好）
 ├─ --stats? ──→ 打印返回           ← u1-l5 的短路开关
 ├─ --beaker? ──┐
               ▼
       submit_beaker_job(args)        ──创建──▶  experiment: 1 个 task × 4 个 replica
               │                                   每个 replica 容器内执行：
        return（本地进程结束）                    python -m olmocr.pipeline s3://ws
                                                   │
                                                   ▼  main() 再跑一遍，但这次：
                                            ├─ BEAKER_JOB_NAME 分支：
                                            │    凭证落盘 + 错峰 sleep  ◀── 模块 4.3
                                            ├─ initialize_queue（消费共享队列）
                                            ├─ download_model（错峰保护对象）
                                            ├─ 起 vLLM                 ← u3-l3
                                            └─ workers 消费队列        ← u2-l1
```

### 4.1 提交端：submit_beaker_job 的参数剥离与 secrets 管理

#### 4.1.1 概念说明

`--beaker` 是一个"元命令"：**它不是让你在本地跑 OCR，而是让你在本地准备队列，然后把剩下的工作打包扔到集群**。所以提交函数要解决两件事：

1. **改写命令行**：worker 集群里跑的命令必须和本地不同——某些参数带过去会出大问题（递归提交！），某些参数已经没必要带（队列填好了）。
2. **搬运凭证**：worker 容器里没有你笔记本上的 AWS key、Weka key。Beaker 提供 secret 机制：提交时把凭证存进 Beaker，运行时自动注入为容器环境变量。

一个容易忽略的细节：`beaker-py` 是**可选依赖**。看导入方式就知道——`from beaker import ...` 写在函数体内而不是文件顶部，见 [olmocr/pipeline.py:993-1006](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L993-L1006)。只有真正走到 `submit_beaker_job` 的用户才需要 `pip install olmocr[beaker]`（该 extra 只有 `beaker-py` 一个包，见 [pyproject.toml:59-61](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L59-L61)）。轻量安装（u1-l2）的用户完全不受影响。

#### 4.1.2 核心流程

`submit_beaker_job(args)` 的执行顺序：

1. **初始化客户端**：`Beaker.from_env()` 用你本地登录的 Beaker 凭证连接平台；`Beaker.TIMEOUT = 60` 把每次 API 请求超时设为 60 秒；`owner = b.user_name` 取用户名（后面给 secret 命名用）。
2. **确定镜像与任务名**：镜像 `jakep/olmocr-inference-{VERSION}` 与你安装的 olmocr 版本绑定；任务名从 workspace 路径末段生成。
3. **剥离命令行参数**：两条列表推导，分别删掉 `--beaker` 和 `--pdfs`。
4. **处理 secrets**：先探测，缺了就（视情况）交互式补写。
5. **组装任务规格并创建实验**（下一模块 4.2 详讲）。

#### 4.1.3 源码精读

**（a）连接与命名。**

[olmocr/pipeline.py:1008-1013](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1008-L1013)：创建 Beaker 客户端（默认 workspace 用 `--beaker_workspace`，默认 `ai2/olmocr`），取用户名，用 `olmocr.version.VERSION` 拼出与当前安装版本一致的镜像名，并从 workspace 路径推导任务名。

```python
Beaker.TIMEOUT = 60
b = Beaker.from_env(default_workspace=args.beaker_workspace)
owner = b.user_name
beaker_image = f"jakep/olmocr-inference-{VERSION}"
task_name = f"olmocr-{os.path.basename(args.workspace.rstrip('/'))}"
```

- 镜像名带版本号：你用 0.4.0 的 olmocr 提交，worker 就跑为 0.4.0 构建的镜像，**提交端代码与 worker 端镜像强制对齐**，避免"新旧代码混跑同一个队列"。
- `rstrip('/')` 处理 `s3://bucket/ws/` 这种尾斜杠：先去掉，`basename` 才能取到 `ws` 而不是空串。

**（b）参数剥离——本模块的核心。**

[olmocr/pipeline.py:1015-1019](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1015-L1019)：从 `sys.argv`（原始命令行，而非解析后的 args）出发做两次过滤：

```python
# Take out --beaker flag so the workers will just run things
args_list = [arg for arg in sys.argv[1:] if arg != "--beaker"]

# Take out the --pdfs [arg] or --pdfs=[arg], since the queue is populated locally
args_list = [arg for i, arg in enumerate(args_list) if not (arg.startswith("--pdfs") or (i > 0 and args_list[i - 1] == "--pdfs"))]
```

- **删 `--beaker`：防止递归提交。** worker 容器里执行的就是 `python -m olmocr.pipeline <同一条命令>`。如果命令里还带 `--beaker`，worker 一启动会走到同一个短路点（L1418），再次提交一个新实验——每个实验又派生 4 个 worker，每个 worker 再提交……指数扇出。所以这一删是**正确性问题**，不是锦上添花。
- **删 `--pdfs`：队列已经填好了。** 注意 main() 中短路点的位置（[olmocr/pipeline.py:1414-1420](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1414-L1420)）：`--pdfs` 的收集与 `populate_queue`（L1334-1412）发生在 `if args.beaker` **之前**。也就是说提交机已经把队列写进了共享的 S3 workspace（u3-l1 的 populate 是幂等的路径级合并）。worker 再带 `--pdfs` 会白白重复"下载 100 篇 PDF 采样估页数"（u2-l2 讲过的采样逻辑）这类重活，还要求 GPU 容器能访问原始 PDF 存储。删掉后 worker 直接进入消费模式。
- 过滤条件的设计：`arg.startswith("--pdfs")` 兼顾 `--pdfs=path` 与 `--pdfs` 两种写法；`args_list[i-1] == "--pdfs"` 删除紧跟其后的那个值。

**（c）一个值得注意的边界情况。** 第二条推导只检查"前一个元素"，而 `--pdfs` 的 argparse 定义是 `nargs="*"`（[olmocr/pipeline.py:1206-1210](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1206-L1210)），允许多值。手工推演一下 `olmocr ws --pdfs a.pdf b.pdf --beaker`：

- 删 `--beaker` 后：`["ws", "--pdfs", "a.pdf", "b.pdf"]`
- 逐位检查：`"ws"` 保留；`"--pdfs"` 命中 `startswith` 删除；`"a.pdf"` 前一位是 `--pdfs` 删除；`"b.pdf"` 前一位是 `a.pdf` ≠ `--pdfs`，**保留**。

也就是说**多值 `--pdfs` 时只有第一个值会被剥掉**，后面的值会泄漏进 worker 命令行，最终经 `parse_known_args` 落入 `unknown_args`，被转发给 `vllm serve`（转发机制见 [olmocr/pipeline.py:833-834](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L833-L834)，u3-l3 已讲）。README 的官方示例只传一个 glob 值（[README.md:384](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L384)），所以文档用法不会踩到。这正好说明"读源码时要在脑中逐元素跑一遍列表推导"的价值。（该端到端行为待本地验证。）

**（d）secrets 探测与写入。** 提交端管理的凭证分三组：

[olmocr/pipeline.py:1021-1039](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1021-L1039)：**Weka + S3 凭证**（第一组，必需）。先用 `b.secret.get()` 探测三个以用户名命名的 secret 是否存在；只要任一缺失（抛 `BeakerSecretNotFound`），就进入人机交互：

```python
except BeakerSecretNotFound:
    print(f"Expected beaker secrets ... Are you okay to write those to your beaker workspace {args.beaker_workspace}? [y/n]")
    if input().strip().lower() != "y":
        print("Exiting...")
        sys.exit(1)
    b.secret.write(f"{owner}-WEKA_ACCESS_KEY_ID", os.environ.get("WEKA_ACCESS_KEY_ID", ""))
    b.secret.write(f"{owner}-WEKA_SECRET_ACCESS_KEY", os.environ.get("WEKA_SECRET_ACCESS_KEY", ""))
    b.secret.write(f"{owner}-AWS_CREDENTIALS_FILE",
        open(os.path.join(os.path.expanduser("~"), ".aws", "credentials")).read())
```

交互设计有三个要点：

1. **先征得同意**：往共享 workspace 写凭证是敏感操作，非 `y` 直接退出，绝无默认放行。
2. **来源是本地**：Weka 的两个 key 从本地环境变量读；AWS 凭证直接读 `~/.aws/credentials` 文件全文（这也意味着提交机上必须存在这个文件，否则 `open()` 会抛异常）。
3. **secret 以 `{owner}-` 前缀命名**：每个用户管理自己的凭证，互不覆盖。

Weka key 的消费方是 [olmocr/s3_utils.py:286-296](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/s3_utils.py#L286-L296)：`download_dir_from_storage` 遇到 `weka://` 路径时，从环境变量读这两个 key，用 S3 协议指向 Weka 的专属 endpoint（`https://weka-aus.beaker.org:9000`）建 boto3 client。**为什么 worker 需要它们？** u3-l3 讲过模型路径可以是 `s3://`、`gs://`、`weka://`（`download_model` 的三分支，见 [olmocr/pipeline.py:956-966](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L956-L966)）——AI2 内部大量模型放在 Weka 上，worker 得能拉下来。

[olmocr/pipeline.py:1041-1051](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1041-L1051)：把三个 secret 登记成"注入式环境变量"（`secret=` 而非 `value=`，容器里 `AWS_CREDENTIALS_FILE` 等环境变量的**值**就是 secret 内容）；**HF token**（第二组，可选）：探测 `OLMOCR_PREVIEW_HF_TOKEN`，存在则注入为 `HF_TOKEN`，不存在就 `pass` 静默跳过——用于访问 HF 上的私有预览模型，没有也不影响公开模型。

[olmocr/pipeline.py:1053-1068](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1053-L1068)：**GCS 服务账号 key**（第三组，可选交互式）。探测 `OE_DATA_GCS_SA_KEY`，存在则注入为 `GOOGLE_APPLICATION_CREDENTIALS_FILE`；不存在则提示用户**现场粘贴** SA key（`iter(input, None)` 循环读到连续两个空行结束），非空才写入 secret 并注入，留空可跳过。

> 注意三种凭证的缺失策略各不相同：Weka/S3 组是"探测 → 交互确认 → 写入"（必需）；HF token 是"有则用、无则略"（静默）；GCS 是"没有就现场问你要"（可跳过）。对初学者的启示：**可选依赖的缺失处理应该按"业务上是否必需"分级，而不是一刀切。**

#### 4.1.4 代码实践

**实践目标**：不依赖 Beaker 账号，本地复现"参数剥离"这一段纯字符串逻辑，亲眼看到 worker 命令行长什么样。

**操作步骤**：在任意目录新建 `strip_args_practice.py`（以下为**示例代码**，不是项目原有文件）：

```python
# 示例代码：复现 submit_beaker_job 的两条列表推导（pipeline.py L1015-1019）
import sys

def strip_for_worker(raw_argv):
    args_list = [arg for arg in raw_argv[1:] if arg != "--beaker"]
    args_list = [arg for i, arg in enumerate(args_list)
                 if not (arg.startswith("--pdfs") or (i > 0 and args_list[i - 1] == "--pdfs"))]
    return ["python", "-m", "olmocr.pipeline"] + args_list

# 模拟 README.md:384 的官方用法（一个 glob 值）
sys.argv = ["olmocr", "s3://bucket/pdfworkspaces/examplews",
            "--pdfs", "s3://bucket/jakep/gnarly_pdfs/*.pdf",
            "--beaker", "--beaker_gpus", "4"]
print(strip_for_worker(sys.argv))

# 再试一个多值 --pdfs 的边界情况
sys.argv = ["olmocr", "s3://bucket/ws", "--pdfs", "a.pdf", "b.pdf", "--beaker"]
print(strip_for_worker(sys.argv))
```

**需要观察的现象**：第一组打印出的 worker 命令里不再有 `--beaker` 与 `--pdfs`，但 `--beaker_gpus 4` 还在。

**预期结果**（手工逐元素推演，待本地验证）：

```
['python', '-m', 'olmocr.pipeline', 's3://bucket/pdfworkspaces/examplews', '--beaker_gpus', '4']
['python', '-m', 'olmocr.pipeline', 's3://bucket/ws', 'b.pdf']   # b.pdf 泄漏了！
```

**思考**：`--beaker_gpus` 留在 worker 命令里为什么无害？（提示：它只被 `submit_beaker_job` 读取，而 worker 命令里已没有 `--beaker`，走不到那个函数。）

#### 4.1.5 小练习与答案

**练习 1**：为什么剥离 `--beaker` 是"必须"，而剥离 `--pdfs` 只是"更好"？

**参考答案**：不剥 `--beaker`，worker 启动后会在 L1418 再次命中短路、再次 `submit_beaker_job`，实验按副本数指数扇出（正确性故障）；不剥 `--pdfs`，由于 populate_queue 是幂等的路径级合并（u3-l1），队不会重复入库，worker 仍能正常干活，只是白白重复输入收集与采样估算，并要求 worker 能访问原始 PDF 存储（效率与耦合问题）。

**练习 2**：`--pdfs=foo.pdf`（等号写法）会被正确剥离吗？`--pdfs foo.pdf` 呢？

**参考答案**：都会。等号写法整体是一个 token，`arg.startswith("--pdfs")` 命中它自身；空格写法下标志被 `startswith` 命中、值被 `args_list[i-1] == "--pdfs"` 命中。

**练习 3**：`AWS_CREDENTIALS_FILE` 这个 secret 存的是什么？`BeakerEnvVar(name="AWS_CREDENTIALS_FILE", secret=...)` 与 `BeakerEnvVar(name="BEAKER_JOB_NAME", value=...)` 有什么区别？

**参考答案**：存的是提交机 `~/.aws/credentials` 文件的全文（INI 格式的 key/secret 对）。`secret=` 版本在容器启动时把 secret 的内容作为该环境变量的值注入；`value=` 版本注入的是字面字符串。worker 端代码（下一模块）就是从环境变量 `AWS_CREDENTIALS_FILE` 读回这份内容再写成文件。

### 4.2 Beaker 任务规格：BeakerExperimentSpec 逐字段解剖

#### 4.2.1 概念说明

`BeakerExperimentSpec` 是一份"作业说明书"：你告诉 Beaker——跑什么镜像、执行什么命令、复制几份、每份给多少资源、优先级如何、失败了怎么办。olmOCR 把所有集群决策浓缩成**一个 task spec**，多副本并行完全靠 `replicas` 字段实现，而不是提交 N 个实验。

规格设计里有两个贯穿始终的思想：

- **每个副本 = 一台单卡机器上的完整 olmocr**（自带 vLLM、自带 worker 协程组），副本之间唯一的耦合是共享 S3 队列；
- **集群的不可靠性交给平台 + 队列**：`preemptible=True` 接受被抢占，`retry` 允许平台整任务重试，而被杀掉的副本没做的项会留在队列里，被任何活着的副本捡走——u3-l1 的 best-effort 乐观并发取向在集群场景下才显出真正的价值。

#### 4.2.2 核心流程

spec 的组装是一条纯粹的"数据构造"路径（无 I/O），可按下表逐字段核对：

| spec 字段 | 取值 | 由什么控制 |
|-----------|------|-----------|
| `budget` | `"ai2/oe-base"` | 硬编码 |
| `description` | `task_name` | workspace 路径末段派生 |
| task `name` | `task_name` | 同上 |
| task `replicas` | `args.beaker_gpus` | **CLI `--beaker_gpus`（默认 1）** |
| task `propagate_failure` / `propagate_preemption` | `False` / `False` | 硬编码：单副本失败/被抢占不连坐 |
| context `priority` | `BeakerJobPriority[args.beaker_priority]` | **CLI `--beaker_priority`（默认 `"normal"`）** |
| context `preemptible` | `True` | **硬编码**：可抢占实例 |
| `image` | `jakep/olmocr-inference-{VERSION}` | 安装的 olmocr 版本 |
| `command` | `["python", "-m", "olmocr.pipeline"] + args_list` | 剥离后的原始命令行 |
| `env_vars` | `BEAKER_JOB_NAME` / `OWNER` / `HF_HUB_OFFLINE` + secrets | 混合：字面值 + secret 注入 |
| resources `gpu_count` | `1` | **硬编码：每副本恰好 1 张 GPU** |
| resources `memory` | `"125GB"` | 硬编码（防 vLLM 吃光内存） |
| `constraints` | cluster 列表 | **CLI `--beaker_cluster`（默认 4 个集群）** |
| `result` | `path="/noop-results"` | 硬编码占位 |
| `retry` | `allowed_task_retries=10` | 硬编码：平台级任务重试上限 |

#### 4.2.3 源码精读

**（a）实验规格主体。**

[olmocr/pipeline.py:1071-1098](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1071-L1098)：构造 `BeakerExperimentSpec`，预算账户 `ai2/oe-base`，唯一 task 的命令由剥离后的 `args_list` 拼成：

```python
experiment_spec = BeakerExperimentSpec(
    budget="ai2/oe-base",
    description=task_name,
    tasks=[
        BeakerTaskSpec(
            name=task_name,
            propagate_failure=False,
            propagate_preemption=False,
            replicas=args.beaker_gpus,
            context=BeakerTaskContext(priority=BeakerJobPriority[args.beaker_priority], preemptible=True),
            image=BeakerImageSource(beaker=beaker_image),
            command=["python", "-m", "olmocr.pipeline"] + args_list,
            ...
```

三个学习目标中的问题在这里有了直接答案：

- **`replicas` 由 `--beaker_gpus` 控制**（argparse 定义在 [olmocr/pipeline.py:1269](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1269)，`type=int, default=1`）。`--beaker_gpus 4` 不是"每台机器 4 张卡"，而是"4 个单卡副本"。
- **`preemptible=True` 是硬编码**——不可配置。可抢占实例更便宜、排队更快，代价是随时可能被回收；olmOCR 敢用，正是因为队列断点续跑把"作业被杀"降级成了"这一轮少干几个 work item"。
- **`priority` 用枚举字符串索引** `BeakerJobPriority[args.beaker_priority]`：`--beaker_priority high` → `BeakerJobPriority["high"]`。写成 `args.beaker_priority` 而非裸字符串，能让 argparse 侧的非法值在校验时尽早爆掉（传入不存在的优先级名会 `KeyError`）。

命令行还有个细节：worker 用 `python -m olmocr.pipeline` 而不是 `olmocr` 入口脚本。两者殊途同归——`olmocr` 命令由 [pyproject.toml:49-50](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L49-L50) 注册到 `olmocr.pipeline:cli_main`，而模块方式走的是文件尾部的 `if __name__ == "__main__": cli_main()`（[olmocr/pipeline.py:1531-1532](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1531-L1532)）。

**（b）环境变量清单：worker 端分支的伏笔。**

[olmocr/pipeline.py:1086-1091](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1086-L1091)：

```python
command=["python", "-m", "olmocr.pipeline"] + args_list,
env_vars=[
    BeakerEnvVar(name="BEAKER_JOB_NAME", value=task_name),
    BeakerEnvVar(name="OWNER", value=owner),
    BeakerEnvVar(name="HF_HUB_OFFLINE", value="1"),
] + env_var_secrets,
```

- `BEAKER_JOB_NAME`（字面值）是**下一模块 worker 分支的开关**：worker 的 main() 靠 `if "BEAKER_JOB_NAME" in os.environ` 判断"我在 Beaker 容器里"。设计巧妙之处在于：**同一个提交端既定义了这个环境变量，又在 worker 端探测它**——worker 不需要额外的"角色"参数来区分自己是提交机还是集群副本。
- `OWNER` 是用户名标注；全仓库检索可知 `olmocr` 包内没有其它代码读它（只有 [olmocr/pipeline.py:1088](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1088) 这一处赋值），用途是给 AI2 运维/监控标识作业归属。
- `HF_HUB_OFFLINE=1` 让 huggingface_hub 进入离线模式、只使用本地缓存不再联网。结合 `download_model` 的三分支（[olmocr/pipeline.py:956-973](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L956-L973)）：默认模型走 HF 时要求模型已在镜像缓存里（参照公开镜像 `Dockerfile.with-model` 的做法——构建期 `snapshot_download` 预置模型缓存，见 [Dockerfile.with-model:15-20](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/Dockerfile.with-model#L15-L20)）；AI2 内部也可能直接传 `--model weka://...` 走第一分支。Beaker 镜像 `jakep/olmocr-inference-*` 托管在 Beaker 平台、不在本仓库构建，其具体内容待确认——但从 `HF_HUB_OFFLINE=1` 的设置可以推断设计意图：**worker 不应在运行时从公网拉模型**。
- `env_var_secrets`（4.1 讲的 Weka/S3/GCS/HF 凭证）追加在同一清单里，运行时注入。

**（c）资源、集群与重试。**

[olmocr/pipeline.py:1092-1097](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1092-L1097)：

```python
resources=BeakerTaskResources(gpu_count=1, memory="125GB"),  # Have to set a memory limit, otherwise VLLM may use too much on its own
constraints=BeakerConstraints(cluster=args.beaker_cluster if isinstance(args.beaker_cluster, list) else [args.beaker_cluster]),
result=BeakerResultSpec(path="/noop-results"),
...
retry=BeakerRetrySpec(allowed_task_retries=10),
```

- **`gpu_count=1` 硬编码**——每个副本恰好一张 GPU。多卡训练式的张量并行（`--tensor-parallel-size`）留给手动场景，Beaker 模式选择"横向复制"而非"纵向加大"：单卡副本是弹性伸缩的最小颗粒，一张卡空闲就能塞进一个副本。
- `memory="125GB"` 的行内注释直白地给了理由：不设上限的话 vLLM 会按机器内存自行扩张，把内存吃光。
- 集群约束读 `--beaker_cluster`（argparse 定义见 [olmocr/pipeline.py:1264-1268](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1264-L1268)，默认 `["ai2/jupiter", "ai2/ceres", "ai2/neptune", "ai2/saturn"]` 四集群）；`isinstance` 检查做单值/列表的归一。
- `result=path="/noop-results"` 是**占位符**——olmOCR 的真正结果直接写进 workspace 的 `results/` 目录（u2-l8），完全绕过 Beaker 的 result 机制；这里的 noop 路径只是满足 spec 的必填要求。
- `retry=BeakerRetrySpec(allowed_task_retries=10)` 是平台级重试：整个 task 挂掉（例如镜像拉取失败）Beaker 最多重试 10 次。

**（d）创建实验并返回。**

[olmocr/pipeline.py:1100-1102](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1100-L1102)：调用 Beaker API 创建实验，打印实验监控 URL 后 `return`——配合 main() 里 `submit_beaker_job(args); return`（[olmocr/pipeline.py:1418-1420](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1418-L1420)），提交机进程就此结束，**本地不再有任何 OCR 计算**。

#### 4.2.4 代码实践

**实践目标**：亲手完成"spec 字段 ↔ 控制来源"的对照表（这也是本讲综合实践要求的笔记之一），并用一个无副作用的脚本把"将要提交的说明书"打印出来。

**操作步骤**：

1. 通读 [olmocr/pipeline.py:1071-1098](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1071-L1098)，对每个字段追问：这个值是硬编码、来自哪个 CLI 参数、还是来自某个环境变量/用户名？把结论填进 4.2.2 的表格里。
2. 可选进阶（需要 `pip install beaker-py`，不会产生任何远端副作用）：写一个**示例代码**脚本，只导入并构造 `BeakerTaskSpec(...)` 等对象（照抄源码字面值、`replicas=4`），然后打印对象本身——**不要调用 `b.experiment.create`**。

**需要观察的现象**：构造好的 spec 对象就是一个嵌套的数据结构（beaker-py 用 dataclass 表示），所有字段都能原样读回。

**预期结果**：你能在不联网、不登录 Beaker 的情况下"干跑"规格组装。若字段签名与你安装的 beaker-py 版本不符（该库接口可能变化），以你本地版本的 dataclass 定义为准（待本地验证）。

**重要提醒**：这个实践**只构造、不提交**。真正调用 `b.experiment.create` 会消耗集群预算，且需要 AI2 账号。

#### 4.2.5 小练习与答案

**练习 1**：`--beaker_gpus 4` 与本地手动跑 4 台机器有什么区别？与单机 4 卡 `--tensor-parallel-size 4` 有什么区别？

**参考答案**：与手动 4 台几乎无区别——Beaker 模式就是自动化版的"多机共消费一个 S3 队列"（done flag/worker lock 保证不重不漏）；区别只在凭证注入、镜像分发、探活与重试这些运维环节。与 TP=4 的区别是并行维度：`--beaker_gpus` 是**数据并行**（4 个独立 vLLM 实例各自完整持有一份模型、处理不同 work item），TP=4 是**模型并行**（一个模型切到 4 张卡上跑）。olmOCR 批处理吞吐优先，选前者。

**练习 2**：`result=BeakerResultSpec(path="/noop-results")` 为什么要设一个"无操作"路径？

**参考答案**：结果数据（Dolma JSONL、markdown）由 worker 直接写进共享 workspace 的 `results/` 与 `markdown/` 目录（u2-l8），不经过 Beaker 的 result 存储；但 spec 要求必填 result，于是给一个从不写入的占位路径。

**练习 3**：`propagate_failure=False` 和 `preemptible=True` 组合起来，在"某副本跑到一半被平台抢占"时会发生什么？

**参考答案**：被抢占的副本终止，但不向其它副本传播（propagate_preemption=False），实验整体继续；它未完成的 work item 的 worker lock 在 1800 秒后过期（u3-l2 的 stale lock 机制），由其它副本在 `get_work` 时抢占；若平台决定重试整个 task（allowed_task_retries=10），新副本也会从队列里自然续跑。**没有任何一处需要"恢复现场"的代码——队列状态本身就是现场。**

### 4.3 worker 端：BEAKER 环境变量适配

#### 4.3.1 概念说明

现在视角切到集群里的一个副本：容器启动 → 镜像的默认命令执行 `python -m olmocr.pipeline s3://ws ...` → 走进同一个 `main()`。但容器环境和你的笔记本有三点不同：

1. **没有本地凭证文件**：`~/.aws/credentials`、GCS 的 SA key 文件都不存在，但它们的内容被 Beaker 注入成了环境变量（`AWS_CREDENTIALS_FILE`、`GOOGLE_APPLICATION_CREDENTIALS_FILE`）；
2. **所有副本同时启动**：如果 16 个副本在同一毫秒一起去拉同一个几十 GB 的模型，Weka/存储端会被打爆，重复 IO 拖慢所有人；
3. **资源由平台分配**：CPU 数不再是"机器总数"，而是 Beaker 分配额。

worker 端适配代码就围绕这三点展开。先明确环境变量的**来源分两类**：

- 提交端写进 spec 的：`BEAKER_JOB_NAME`、`OWNER`、`HF_HUB_OFFLINE` + 一组凭证；
- Beaker 平台对多副本 task 自动注入的标准变量：`BEAKER_REPLICA_COUNT`（副本总数）与 `BEAKER_REPLICA_RANK`（我是第几个副本，0 起）。核对 spec 的 env_vars 清单（L1086-1091）可以确认这两个**不在**提交端设置——它们是平台注入的。代码里用 `os.environ.get(..., 默认值)` 读取，因此本地运行（无这些变量）时也能安全通过，这正是"容器专用逻辑写得像通用代码"的技巧。

#### 4.3.2 核心流程

worker 的 main() 在磁盘日志配置之后、队列创建之前进入适配分支（[olmocr/pipeline.py:1291-1310](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1291-L1310)）：

1. 探测 `BEAKER_JOB_NAME` 是否在环境里（在 = 我在 Beaker 容器里）；
2. **AWS 凭证落盘**：把环境变量 `AWS_CREDENTIALS_FILE` 的内容写到 `~/.aws/credentials`；
3. **GCS 凭证落盘**：把 `GOOGLE_APPLICATION_CREDENTIALS_FILE` 的内容写到 `~/.gcs/credentials`，并设置 `GOOGLE_APPLICATION_CREDENTIALS` 指向该文件；
4. **重建两个全局 boto3 客户端**（模块级那两个是在凭证文件存在之前创建的）；
5. **错峰 sleep**：按 `BEAKER_REPLICA_RANK` 计算等待秒数并 `await asyncio.sleep(...)`；
6. 之后才继续队列初始化、`download_model`（[olmocr/pipeline.py:1429-1431](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1429-L1431)）、起 vLLM。

```
容器启动
  │
  ▼
main() ── BEAKER_JOB_NAME 在环境里吗？
  │ 是
  ├─ 写 ~/.aws/credentials      ← 内容来自注入的环境变量
  ├─ 写 ~/.gcs/credentials
  ├─ 设 GOOGLE_APPLICATION_CREDENTIALS
  ├─ 重建 workspace_s3 / pdf_s3
  └─ sleep(rank × interval)     ← 错峰，避免齐刷刷下载模型
  │
  ▼
initialize_queue → download_model → vllm serve → workers
```

#### 4.3.3 源码精读

**（a）凭证落盘与客户端重建。**

[olmocr/pipeline.py:1291-1303](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1291-L1303)：

```python
if "BEAKER_JOB_NAME" in os.environ:
    cred_path = os.path.join(os.path.expanduser("~"), ".aws", "credentials")
    os.makedirs(os.path.dirname(cred_path), exist_ok=True)
    with open(cred_path, "w") as f:
        f.write(os.environ.get("AWS_CREDENTIALS_FILE"))
    cred_path = os.path.join(os.path.expanduser("~"), ".gcs", "credentials")
    os.makedirs(os.path.dirname(cred_path), exist_ok=True)
    with open(cred_path, "w") as f:
        f.write(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_FILE"))
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = cred_path
    workspace_s3 = boto3.client("s3")
    pdf_s3 = boto3.client("s3")
```

逐行读出四层设计：

- **"环境变量 → 文件"的方向反转**：提交端把文件内容变成 secret → secret 注入成环境变量；worker 端把环境变量内容写回文件。为什么绕一圈？因为 boto3 和 google-cloud 的 SDK 认的是**文件路径惯例**（`~/.aws/credentials`、`GOOGLE_APPLICATION_CREDENTIALS` 指向的文件），不支持"从环境变量直接读凭证文件全文"。环境变量只是运输通道，落盘才是让生态工具能用的最后一公里。
- **重建 boto3 客户端**：模块顶层的 `workspace_s3 = boto3.client("s3")` / `pdf_s3 = ...`（[olmocr/pipeline.py:73-75](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L73-L75)）在 import 时创建，那时容器里凭证文件还不存在；凭证落盘后新建两个客户端，保证后续所有 S3 请求都基于新状态。注意 `main()` 开头 `global workspace_s3, pdf_s3`（L1287）配合了这次重绑定。
- **一个隐含的健壮性假设**：`AWS_CREDENTIALS_FILE` 一定在环境里吗？是——提交端把三个 Weka/S3 secret **无条件**登记进 `env_var_secrets`（4.1 的 (d) 组），并且早前保证了 secret 必然存在（找到或已写入）。而 `GOOGLE_APPLICATION_CREDENTIALS_FILE` 是**条件**登记的（secret 缺失且用户未粘贴 key 时不登记）——此时这里 `os.environ.get(...)` 返回 `None`，`f.write(None)` 将抛 `TypeError` 使 worker 启动失败。从代码看，当前实现默认"AI2 的 workspace 里 GCS secret 总是就绪"（该 secret 是否确实配置于 `ai2/olmocr` workspace：待确认）。这处不对称是个很好的防御性编程反例，练习 2 会让你亲自推演。

**（b）错峰 sleep 的计算公式。**

[olmocr/pipeline.py:1305-1310](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1305-L1310)：

```python
# Wait a little bit so that not all beaker jobs in a task start at the same time and download the model at the same time
replica_count = int(os.environ.get("BEAKER_REPLICA_COUNT", "1"))
interval = 10 if (replica_count - 1) * 10 <= 30 else 30 / max(1, replica_count - 1)
sleep_time = int(os.environ.get("BEAKER_REPLICA_RANK", "0")) * interval
logger.info(f"Beaker job sleeping for {sleep_time} seconds to stagger model downloads")
await asyncio.sleep(sleep_time)
```

用数学语言写清楚（\(N\) 为副本总数，\(r\) 为本副本的 rank，\(r=0,1,\dots,N-1\)）：

\[
\text{interval} = \begin{cases} 10 & \text{若 } (N-1)\times 10 \le 30 \\ \dfrac{30}{\max(1,\; N-1)} & \text{否则} \end{cases}
\qquad
\text{sleep\_time} = \left\lfloor r \times \text{interval} \right\rfloor
\]

这是一条**总窗口封顶约 30 秒**的阶梯：

- \(N \le 4\) 时阶梯固定 10 秒：rank 0/1/2/3 分别睡 0/10/20/30 秒，最后一个副本也只比第一个晚 30 秒——既能错开共享存储的下载洪峰，又几乎不损失总吞吐；
- \(N > 4\) 时若仍按 10 秒阶梯，末尾副本要等 \((N-1)\times 10\) 秒（16 个副本 = 150 秒），等待成本开始超过错峰收益；于是改为按人头均分 30 秒窗口：interval \(= 30/(N-1)\)，末尾副本仍在约 30 秒处启动；
- `max(1, replica_count - 1)` 防御 \(N=1\) 的除零（虽然 \(N=1\) 时条件必然走 10 秒分支，这个卫兵是纯防御式的）；
- 默认值 `"1"`/`"0"` 保证**非 Beaker 环境**（本地跑、Docker 跑）不进任何 sleep——整段逻辑只在 `BEAKER_JOB_NAME` 存在时才执行。

为什么要错峰？注释写得很清楚：防止所有副本**同时**下载模型。它保护的正是紧接着的 `download_model`（[olmocr/pipeline.py:956-966](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L956-L966)）：模型路径若为 `weka://`/`gs://`/`s3://`，会走 `download_directory` 真实网络下载；N 个副本同时拉同一个几十 GB 的目录，Weka 那一个 endpoint（s3_utils.py:292）就是明晃晃的单点热点。`HF_HUB_OFFLINE=1` 则把默认 HF 路径排除在下载之外（用镜像内缓存）。

**（c）Beaker 还悄悄适配了一处更早的代码。**

[olmocr/pipeline.py:87](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L87)：

```python
pdf_render_max_workers_limit = asyncio.BoundedSemaphore(int(float(os.environ.get("BEAKER_ASSIGNED_CPU_COUNT", max(1, multiprocessing.cpu_count() - 2)))))
```

u2-l3/u2-l7 讲过这个渲染闸门：限制并发 pdftoppm 子进程数。注意它的默认值"本机 CPU 数 - 2"在容器里是**宿主机**的 CPU 数（容器看到的是平台分配额），Beaker 用 `BEAKER_ASSIGNED_CPU_COUNT` 环境变量告诉任务实际分到了几个 CPU——有它就用分配额，避免在 CPU 限额 8 核的容器里按 128 核机器开闸。这是"平台分配 ≠ 机器总量"这一容器常识的代码化。

#### 4.3.4 代码实践

**实践目标**：不进集群，用纯 Python 把错峰公式跑一遍，亲眼验证"总窗口 30 秒封顶"的设计。

**操作步骤**：新建 `stagger_practice.py`（**示例代码**）：

```python
# 示例代码：复现 pipeline.py L1306-1308 的错峰公式
def stagger(rank: int, replica_count: int) -> int:
    interval = 10 if (replica_count - 1) * 10 <= 30 else 30 / max(1, replica_count - 1)
    return int(rank * interval)

for n in (1, 2, 4, 5, 8, 16):
    sleeps = [stagger(r, n) for r in range(n)]
    print(f"N={n:2d}  各rank睡眠: {sleeps}  窗口={sleeps[-1]}s")
```

**需要观察的现象**：N 从 1 涨到 16，最大等待时间始终不超过 30 秒；N≤4 时相邻 rank 间隔 10 秒，之后间隔随 N 增大而变窄。

**预期结果**（手工推演，待本地验证）：

```
N= 1  各rank睡眠: [0]  窗口=0s
N= 2  各rank睡眠: [0, 10]  窗口=10s
N= 4  各rank睡眠: [0, 10, 20, 30]  窗口=30s
N= 5  各rank睡眠: [0, 7, 15, 22, 30]  窗口=30s
N= 8  各rank睡眠: [0, 4, 8, 12, 17, 21, 25, 30]  窗口=30s
N=16  各rank睡眠: [0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 25, 27, 30]  窗口=30s
```

**对照源码检查**：注意 `int()` 套在整个乘积上（`int(rank * interval)` 而非 `int(interval) * rank`），所以 N=5 时是 `int(1×7.5)=7`、`int(3×7.5)=22`；N=8 时 `int(4×30/7)=int(17.14)=17`。

#### 4.3.5 小练习与答案

**练习 1**：为什么凭证注入走"环境变量 → 写文件 → 重建客户端"三步，而不是让 boto3 直接读环境变量里的 key？

**参考答案**：提交端注入的是**整个 credentials 文件的内容**（可能包含多个 profile）， boto3 的默认凭据链认的是文件路径（`~/.aws/credentials`），不认"文件全文"环境变量；GCS 的 SDK 同样只认 `GOOGLE_APPLICATION_CREDENTIALS` 指向的文件。落盘是把凭证交给整个生态（boto3、google-cloud、以及任何遵循同一惯例的工具）的通用做法；重建客户端是因为模块级客户端创建于文件落盘之前。

**练习 2**：如果你在自己的 Beaker workspace 从未配置过 GCS secret、提交时也选择不粘贴 SA key，worker 端会发生什么？沿代码指出出错行。

**参考答案**：提交端不会把 `GOOGLE_APPLICATION_CREDENTIALS_FILE` 加入 spec 的 env_vars（L1053-1068 的条件分支）；worker 端 [olmocr/pipeline.py:1297-1300](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1297-L1300) 里 `os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_FILE")` 返回 `None`，`f.write(None)` 抛 `TypeError`，worker 在 main() 早期崩溃。对照 `AWS_CREDENTIALS_FILE`（提交端无条件登记，见 L1041-1045）可知这是两份凭证"必需 vs 可选"的语义差异没有被 worker 端同步实现。（该推演待本地验证；实际部署中 AI2 的 workspace 应已预置该 secret。）

**练习 3**：把 `--beaker_gpus` 从 4 改成 64，错峰保护还有效吗？区间和总窗口各是多少？

**参考答案**：有效。N=64 时 \((64-1)\times10=630>30\)，走均分分支：interval \(=30/63\approx0.476\) 秒，相邻副本错开不到半秒，末尾 rank 63 睡 `int(63×30/63)=30` 秒。总窗口恒 30 秒，只是副本太密时错峰变薄——这在"存储端能承受多大瞬时并发"与"末尾副本等多久"之间取的折中，代码把这条折中曲线写成了两行。

## 5. 综合实践

本讲的实践任务是**源码阅读型**的（无需 Beaker 账号、无需 GPU）：把 `submit_beaker_job` 读透，回答三个问题并整理成笔记。下面给出完整的任务指引与参考答案。

### 任务一：参数剥离笔记

重读 [olmocr/pipeline.py:1015-1019](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1015-L1019) 与短路点 [olmocr/pipeline.py:1414-1420](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1414-L1420)，完成 4.1.4 的剥离模拟脚本，然后写下：

**参考笔记**：

- 删除的参数有二：`--beaker`（所有出现都删）与 `--pdfs`（连同紧跟的一个值；`--pdfs=x` 等号写法整体删除）。
- 删 `--beaker` 的原因：worker 命令若带它，会在 L1418 再次短路 → 再次提交实验 → 递归扇出，是正确性故障。
- 删 `--pdfs` 的原因：main() 里 `--pdfs` 的收集与 `populate_queue`（L1334-1412）先于 `--beaker` 短路执行，队列已在提交机写入共享 S3 workspace；worker 只需 `initialize_queue` 消费。populate 幂等（u3-l1），所以删它是"省重复采样、解耦存储访问"的优化而非硬性要求。
- 未删但无害：`--beaker_workspace/--beaker_cluster/--beaker_gpus/--beaker_priority` 都只被 `submit_beaker_job` 使用，worker 命令里没有 `--beaker` 就永远走不到。
- 边界情况：`--pdfs a.pdf b.pdf` 多值时只有第一个值被剥离（列表推导只检查前一个元素），后续值经 unknown_args 转发给 `vllm serve`。

### 任务二：任务规格对照笔记

对照 [olmocr/pipeline.py:1071-1098](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1071-L1098) 与 argparse 组 [olmocr/pipeline.py:1260-1270](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1260-L1270) 填 4.2.2 的表格，重点回答：

**参考笔记**：

- `replicas` 由 CLI `--beaker_gpus` 控制（默认 1）——副本数即"GPU 数"，每副本单卡；
- `gpu_count` 硬编码为 `1`（另有 `memory="125GB"` 同样硬编码，注释说明是防 vLLM 无上限吃内存）；
- `preemptible` 硬编码为 `True`（不可配置）；`priority` 才是可调的（`--beaker_priority`，默认 normal，经 `BeakerJobPriority[...]` 枚举索引）。

### 任务三：错峰公式笔记

**参考笔记**：`sleep_time = int(rank × interval)`，其中 \( \text{interval} = 10 \)（若 \((N-1)\times10 \le 30\)），否则 \(30/\max(1, N-1)\)；\(N\)、\(r\) 来自平台注入的 `BEAKER_REPLICA_COUNT`/`BEAKER_REPLICA_RANK`。总错峰窗口封顶约 30 秒：副本少用固定 10 秒阶梯，副本多则按人头均分窗口。配套跑 4.3.4 的模拟脚本验证曲线。

### 挑战（可选）：画一张"一次 Beaker 提交的完整生命周期"图

从你敲下命令到结果落盘，把以下节点连成时序图并标注涉及的函数/机制：本地 `populate_queue`（提交机）→ `submit_beaker_job` → 平台调度 N 副本 → worker 凭证落盘 → 错峰 sleep → `initialize_queue` → `download_model` → `vllm_server_host`（u3-l3）→ worker 协程组消费（u2-l1）→ `mark_done` 与结果落盘（u2-l8）。若某个副本中途被抢占，在图上标出恢复路径（stale lock 过期 + 平台 retry）。这张图也是对单元 2、单元 3 所学的一次总复习。

## 6. 本讲小结

- **`--beaker` 模式 = "本地填队 + 远程派工"**：main() 先在提交机把队列 populate 进共享 S3 workspace，再到 [olmocr/pipeline.py:1418-1420](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1418-L1420) 短路进 `submit_beaker_job` 后返回；本地不做任何 OCR。
- **命令行被原地改写**：`--beaker` 必须剥离（防递归提交），`--pdfs` 应当剥离（队列已填好、populate 幂等）；多值 `--pdfs` 只剥第一个值是代码中真实存在的边界情况。
- **secrets 是"文件内容 → 环境变量 → 文件"的往返**：提交端把本地 `~/.aws/credentials` 与 Weka/GCS key 存为 Beaker secret；worker 端在 `BEAKER_JOB_NAME` 分支把注入的环境变量写回文件，让 boto3/google-cloud 这些认文件惯例的 SDK 直接可用；三组凭证的缺失策略（必需确认 / 静默跳过 / 交互补录）各不相同。
- **任务规格一句话**：`replicas ← --beaker_gpus`（数据并行的副本数）、`gpu_count ← 硬编码 1`、`preemptible ← 硬编码 True`；镜像版本与安装版本绑定；结果不进 Beaker result 机制，直接写 workspace。
- **错峰公式**：`sleep_time = int(rank × interval)`，interval 在"10 秒阶梯"与"30/(N-1) 均分"之间二选一，总窗口封顶约 30 秒，保护的是启动时对 Weka/GCS/S3 模型存储的下载洪峰；`HF_HUB_OFFLINE=1` 把默认 HF 路径改走镜像内缓存。
- **分布式是队列抽象白送的**：每个副本就是一次普通的单机 `olmocr` 运行，靠 S3Backend 的 done flag 与 worker lock（u3-l1/u3-l2）实现不重不漏；被抢占的副本无需"恢复现场"，因为队列状态就是现场。

## 7. 下一步学习建议

本讲补完了单元 3 的最后一块拼图（队列 → 双后端 → vLLM 看护 → Beaker 集群）。接下来：

1. **u3-l5（Docker 部署）**：同单元的部署形态收尾——`Dockerfile.with-model` 与 Beaker 镜像"预置模型缓存"的思路一脉相承，建议对照阅读 [Dockerfile.with-model](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/Dockerfile.with-model)。
2. **单元 4（数据处理与支撑工具）**：本讲提到的 `s3_utils`（Weka/S3/GCS 三协议下载、zstd CSV 索引）将在 u4-l3 完整精读；`metrics.py` 的 WorkerTracker（u4-l4）是 Beaker 上观察 N 个副本各自在干什么的主要工具。
3. **想动手的读者**：若没有 Beaker 账号，可以在两台机器上用同一个 S3 workspace 手动复现"多机共消费一个队列"的效果（这正是 u3-l2 的 S3Backend 存在的意义），把本讲的架构结论亲手验证一遍；再读 [olmocr/bench/README.md](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/bench/README.md) 为单元 5 的评测体系做铺垫。
