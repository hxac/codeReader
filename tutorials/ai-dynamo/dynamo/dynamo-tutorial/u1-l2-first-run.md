# u1-l2 五分钟跑起来：frontend + worker + curl

## 1. 本讲目标

上一讲（u1-l1）我们建立了 Dynamo 的定位与三面架构的认知，但还没有真正跑过一个服务。本讲结束时，你应该能够：

1. 用容器或 `uv`（PyPI）两种方式之一安装 Dynamo，并在本地启动 `frontend` 进程与一个 `worker` 进程。
2. 用 `curl` 或 OpenAI SDK 向 frontend 发起一次 OpenAI 兼容的 `/v1/chat/completions` 请求，拿到流式响应。
3. 读懂并使用 `examples/backends/sample/launch/` 下的 `agg.sh`（聚合模式）与 `disagg.sh`（分离模式）两份启动脚本，说清它们各自拉起了哪些进程。
4. 解释 `--discovery-backend file` 模式下 frontend 是如何"找到"worker 的——也就是本地的服务发现机制。
5. 写一个测量 TTFT（Time To First Token）的小脚本，并结合源码解释观察到的现象。

本讲刻意使用**不需要 GPU 的 sample 后端**，让你在任何一台有 Python 环境的机器上都能跑通全链路。

## 2. 前置知识

- **frontend 与 worker**：在 Dynamo 的术语里，`frontend` 是对外暴露 OpenAI 兼容 HTTP API 的进程（`python3 -m dynamo.frontend`）；`worker` 是真正执行推理的后端进程（可以是 vLLM / SGLang / TensorRT-LLM，也可以是本讲用的假后端 sample）。
- **聚合（aggregated）与分离（disaggregated）**：聚合模式下一个 worker 同时做 prefill（处理输入）和 decode（逐个生成 token）；分离模式则拆成 prefill worker 和 decode worker 两个进程池。上一讲的 S1–S9 九步流程描述的就是分离模式的请求路径。
- **TTFT（Time To First Token）**：从发出请求到收到第一个生成 token 的时间。对交互式体验最关键，Dynamo 的 KV 感知路由宣称能显著改善它。
- **服务发现（service discovery）**：frontend 需要知道"现在有哪些 worker 活着、地址是什么"。Dynamo 支持四种发现后端：`kubernetes`、`etcd`、`file`、`mem`。本地开发用 `file` 就够了——不需要任何外部基础设施。
- **OpenAI 兼容 API**：Dynamo frontend 暴露的 HTTP 接口（`/v1/chat/completions`、`/v1/models` 等）与 OpenAI 的公开 API 形状一致，所以任何 OpenAI SDK / 现有客户端代码换个 `base_url` 就能用。
- **假后端（sample backend）**：`dynamo.common.backend.sample_main` 是一个纯 Python 的参考引擎，不做真实推理，只按配置的节奏"轮转"地吐 token ID。它让你在没有 GPU、没有模型权重的情况下验证整条请求链路。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/README.md) | 官方 Quick Start（Option A 容器 / Option B PyPI）与服务发现说明 |
| [examples/backends/sample/launch/agg.sh](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/backends/sample/launch/agg.sh) | 聚合模式启动脚本：frontend + 1 个 sample worker |
| [examples/backends/sample/launch/disagg.sh](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/backends/sample/launch/disagg.sh) | 分离模式启动脚本：frontend + 1 prefill + 1 decode |
| [examples/common/launch_utils.sh](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/common/launch_utils.sh) | 所有示例脚本共享的工具函数（banner、进程管理） |
| [components/src/dynamo/common/backend/sample_main.py](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/components/src/dynamo/common/backend/sample_main.py) | sample 后端入口：`run(SampleLLMEngine)` |
| [components/src/dynamo/common/backend/run.py](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/components/src/dynamo/common/backend/run.py) | 所有后端共用启动函数：解析参数 → 构造 Worker → 运行 |
| [components/src/dynamo/common/backend/sample_engine.py](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/components/src/dynamo/common/backend/sample_engine.py) | 参考引擎实现（token 生成、KV 事件合成） |
| [components/src/dynamo/frontend/frontend_args.py](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/components/src/dynamo/frontend/frontend_args.py) | frontend 的 CLI/环境变量定义（`--http-port`、`--discovery-backend`） |
| [lib/runtime/src/storage/kv.rs](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/storage/kv.rs) | KV 存储选择器：`file` 模式的根目录解析 |
| [lib/runtime/src/storage/kv/file.rs](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/storage/kv/file.rs) | 文件 KV 存储实现：TTL、keep-alive、目录监听 |

## 4. 核心概念与源码讲解

### 4.1 README Quickstart：三种安装方式与最小可运行组合

#### 4.1.1 概念说明

README 提供三条上手路径：

| 方式 | 适用场景 | 需要什么 |
|------|---------|---------|
| **Option A：容器** | 最快、环境最干净 | Docker + GPU（示例用 SGLang 后端跑真模型） |
| **Option B：PyPI 安装** | 本地开发、想用自己的后端 | `uv` + Python 环境（+ GPU，如果跑真后端） |
| **Option C：Kubernetes** | 生产多节点集群 | K8s 集群 + Dynamo Platform（本讲不展开，见 u10） |

三条路径最终都落到同一件事：**启动一个 frontend 进程 + 至少一个 worker 进程，然后向 frontend 的 HTTP 端口发 OpenAI 格式请求**。这就是 Dynamo 最小的可运行组合。

#### 4.1.2 核心流程

```text
安装（容器 / uv pip install）
        |
        v
[终端 1] python3 -m dynamo.frontend --http-port 8000 --discovery-backend file
        |  启动 HTTP 服务，监听 8000，监听服务发现目录
        v
[终端 2] python3 -m dynamo.<backend> --model-path <model> --discovery-backend file
        |  worker 启动，把 endpoint 元数据写入 KV 存储（file 模式 = 写文件）
        |  frontend 通过目录监听发现 worker，注册为可路由目标
        v
[终端 3] curl localhost:8000/v1/chat/completions -d '{...}'
        |  frontend 分词 -> 路由到 worker -> worker 流式返回 token
        v
     拿到 OpenAI 格式的 JSON 响应
```

#### 4.1.3 源码精读

README 的 Quick Start 一节给出的容器路径命令（Option A）：

[README.md:139-157](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/README.md#L139-L157) —— 这段是官方最小启动范式：拉起 `sglang-runtime` 容器后，在容器内先后启动 frontend 和 worker，再用 `curl` 发请求。注意两条启动命令都带了 `--discovery-backend file`。

其中关键的两行：

[README.md:146-147](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/README.md#L146-L147) —— frontend 指定 `--http-port 8000` 与 `--discovery-backend file`；worker（这里是 SGLang）同样指定 `--discovery-backend file`。**两边必须一致**，否则一个写进 etcd、一个读文件系统，永远发现不了对方。

PyPI 安装方式（Option B）：

[README.md:159-169](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/README.md#L159-L169) —— 用 `uv pip install --prerelease=allow "ai-dynamo[sglang]"`（或 `[vllm]`）安装带后端 extra 的 `ai-dynamo` 包，然后"按上面方式启动 frontend 和 worker"。

为什么本地开发不需要 etcd / NATS？README 的服务发现表格给出了答案：

[README.md:259-275](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/README.md#L259-L275) —— 表格明确写了 **Local Development 行：etcd ❌ 不需要、NATS ❌ 不需要**，只要传 `--discovery-backend file`；唯一附带条件是 vLLM 还需加 `--kv-events-config '{"enable_kv_cache_events": false}'`（sample 后端不需要）。若你确实要跑 etcd/NATS 模式（agg.sh/disagg.sh 的默认值），可以 `docker compose -f dev/docker-compose.yml up -d` 一键起这两个服务。

frontend 侧这两个参数的定义处：

[components/src/dynamo/frontend/frontend_args.py:266-273](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/components/src/dynamo/frontend/frontend_args.py#L266-L273) —— `--http-port` 的默认值是 `8000`，环境变量 `DYN_HTTP_PORT` 可覆盖（这正是 agg.sh 里 `HTTP_PORT="${DYN_HTTP_PORT:-8000}"` 的依据）。

[components/src/dynamo/frontend/frontend_args.py:496-507](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/components/src/dynamo/frontend/frontend_args.py#L496-L507) —— `--discovery-backend` 的默认值是 `etcd`，可选 `kubernetes / etcd / file / mem`，环境变量 `DYN_DISCOVERY_BACKEND` 可覆盖。帮助文本还点出了 file 模式的根目录：环境变量 `DYN_FILE_KV`，缺省 `$TMPDIR/dynamo_store_kv`。

解析出来的配置最终传给运行时：

[components/src/dynamo/frontend/main.py:394-399](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/components/src/dynamo/frontend/main.py#L394-L399) —— frontend 主流程把 `config.discovery_backend` 与 `config.request_plane` 传给 `DistributedRuntime` 构造函数，由 Rust 侧完成实际的服务发现与请求面搭建（细节在 u3 单元展开）。

#### 4.1.4 代码实践

**实践目标**：按 README Option B 走一遍安装，并确认 `ai-dynamo` 可用。

**操作步骤**：

1. 安装 uv 并创建虚拟环境：

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   uv venv .venv && source .venv/bin/activate
   ```

2. 安装 Dynamo（不装任何真实推理引擎 extra，本讲用 sample 后端就够）：

   ```bash
   uv pip install --prerelease=allow "ai-dynamo"
   ```

3. 验证两个模块入口都能被找到（只看帮助信息，不真正启动）：

   ```bash
   python3 -m dynamo.frontend --help
   python3 -m dynamo.common.backend.sample_main --help
   ```

**需要观察的现象**：两条 `--help` 都能打印出参数列表；frontend 的帮助里能看到 `--http-port`、`--discovery-backend`；sample 后端的帮助里能看到 `--model-name`、`--disaggregation-mode`、`--delay`。

**预期结果**：命令退出码为 0，说明安装完整。若你在源码仓库里工作，也可以按 u1-l4 的方式从源码构建后执行同样命令。

> 待本地验证：不同版本的 `ai-dynamo` 帮助文本可能略有差异。

#### 4.1.5 小练习与答案

**练习 1**：为什么 README 里 frontend 和 worker 都必须传 `--discovery-backend file`？只给一边传会怎样？

**答案**：发现后端决定了 endpoint 元数据"写到哪里、从哪里读"。两边都是 `file` 时，worker 把注册信息写进同一个本地目录，frontend 监听该目录。只给 worker 传 `file` 而让 frontend 用默认 `etcd` 时，frontend 会去连 etcd（默认 `localhost:2379`），读不到 worker 写在文件里的注册信息，请求要么报"无可用 worker"，要么一直等待。反之亦然。

**练习 2**：`--http-port` 不传时 frontend 监听哪个端口？如何不改命令行就换端口？

**答案**：默认 `8000`（frontend_args.py 中 `default=8000`）。设置环境变量 `DYN_HTTP_PORT` 即可覆盖，例如 `DYN_HTTP_PORT=9000 python3 -m dynamo.frontend`。

**练习 3**：本地开发需要先 `docker compose -f dev/docker-compose.yml up -d` 把 etcd、NATS 起起来吗？

**答案**：不需要。README 的服务发现表格明确本地开发两者都不是必需的，`--discovery-backend file` 即可。这个 compose 文件只在你想跑默认 etcd 模式（例如直接执行 agg.sh）时才需要。

### 4.2 agg.sh：聚合模式的最小拓扑与 sample 后端入口链

#### 4.2.1 概念说明

`agg.sh` 是仓库自带的"一键聚合模式"脚本：frontend + 一个 sample worker，两个进程，CPU 即可运行。它同时是理解 **sample 后端入口链**的最佳入口：`sample_main.py → run.py → SampleLLMEngine`，这条三跳的链路是所有 Python 后端共用的启动范式（vLLM/SGLang 的接入层只是引擎不同，见 u8）。

sample 引擎的行为要点：

- 按 `--max-tokens`（默认 16）吐 token，每个 token 间隔 `--delay`（默认 0.01 秒）。
- token ID 按 `(i + 1) % 32000` 轮转生成，**内容没有语义**——解码出来的文本是"乱码"，这是正常的，我们的目标是验证链路。
- 它还会合成 KV 事件与负载快照，供路由器消费（这点在综合实践中会用到）。

#### 4.2.2 核心流程

agg.sh 的执行流程：

```text
agg.sh
  |-- 解析参数（--model-name，其余透传给 sample_main）
  |-- print_launch_banner  打印模型名/端口/示例 curl
  |-- python3 -m dynamo.frontend &                 (后台进程 1)
  |-- python3 -m dynamo.common.backend.sample_main \
  |     --model-name $MODEL_NAME ... &             (后台进程 2)
  `-- wait_any_exit          任何一个进程退出就整体收尾
```

sample 后端的启动链：

```text
python3 -m dynamo.common.backend.sample_main
  -> main() 调 run(SampleLLMEngine)              [sample_main.py]
     -> SampleLLMEngine.from_args(argv)           解析 CLI，返回 (engine, worker_config)
     -> Worker(engine, worker_config).run()       把引擎挂到运行时上
        -> Rust 侧 Worker：注册 endpoint 到 KV 存储，开始对外服务
```

#### 4.2.3 源码精读

启动脚本主体：

[examples/backends/sample/launch/agg.sh:40-52](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/backends/sample/launch/agg.sh#L40-L52) —— 这段是 agg.sh 的全部核心：从 `DYN_HTTP_PORT`（缺省 8000）取端口打印 banner；随后**后台**启动 frontend，再**后台**启动 sample worker（`MODEL_NAME` 缺省 `sample-model`，可用环境变量 `MODEL_NAME` 或 `--model-name` 覆盖）；最后 `wait_any_exit` 等待任一进程退出。

注意一个细节：agg.sh **没有**给两个进程传 `--discovery-backend file`，因此它们用的是默认值 `etcd`。想零依赖跑它，要么先 `docker compose -f dev/docker-compose.yml up -d`，要么参考 4.1 的命令手工以 file 模式启动（下面实践给出现成命令）。

进程管理的实现：

[examples/common/launch_utils.sh:92-106](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/common/launch_utils.sh#L92-L106) —— `wait_any_exit` 用 `wait -n` 监视**所有**后台子进程，任何一个退出（frontend 崩了或 worker 崩了）立即返回并触发清理，比手动记录 PID 或前台阻塞更可靠。脚本顶部的注释详细解释了这个设计取舍。

[examples/common/launch_utils.sh:138-171](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/common/launch_utils.sh#L138-L171) —— `print_launch_banner` 负责你启动时看到的那块"横幅"：模型名、frontend 地址 `http://localhost:<port>`，以及一段可以直接复制的示例 curl 命令。

sample 后端入口的三跳：

[components/src/dynamo/common/backend/sample_main.py:10-19](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/components/src/dynamo/common/backend/sample_main.py#L10-L19) —— 入口只有一行实质代码 `run(SampleLLMEngine)`：引擎类与启动逻辑完全解耦。

[components/src/dynamo/common/backend/run.py:26-34](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/components/src/dynamo/common/backend/run.py#L26-L34) —— `_start` 调用 `engine_cls.from_args(argv)` 拿到 `(engine, worker_config)` 二元组，然后 `Worker(engine, worker_config).run()`。任何新后端只要实现 `from_args`，就能复用这整套启动管线。

[components/src/dynamo/common/backend/sample_engine.py:130-184](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/components/src/dynamo/common/backend/sample_engine.py#L130-L184) —— `SampleLLMEngine.from_args` 定义了 sample 后端的全部 CLI 参数：`--model-name`（缺省 `sample-model`）、`--namespace`（缺省 `dynamo`）、`--component`（缺省 `sample`）、`--endpoint`（缺省 `generate`）、`--max-tokens`、`--delay`、`--discovery-backend`（缺省 `etcd`）、`--disaggregation-mode` 等，最后组装成 `WorkerConfig` 返回。这些参数就是 agg.sh/disagg.sh 透传 `EXTRA_ARGS` 的接收端。

token 是怎么"生成"的：

[components/src/dynamo/common/backend/sample_engine.py:391-428](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/components/src/dynamo/common/backend/sample_engine.py#L391-L428) —— `_generate_tokens` 循环 `max_new` 次：每次 `await asyncio.sleep(self.delay)` 后吐出一个 `token_id = (i + 1) % 32000` 的 chunk；最后一个 chunk 附带 `finish_reason: "length"` 与 `completion_usage` 统计。**TTFT 的下界就是 `delay` 加链路开销**——这是综合实践里解释现象的钥匙。

[components/src/dynamo/common/backend/sample_engine.py:186-198](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/components/src/dynamo/common/backend/sample_engine.py#L186-L198) —— `start()` 返回 `EngineConfig`，向运行时声明模型元数据（上下文长度 2048、KV 块大小 16 等），worker 拿它去注册模型。frontend 需要为模型名加载**分词器**来把文本转成 token ID，因此实践中推荐把 `MODEL_NAME` 设为一个真实存在的 HuggingFace 模型（仓库测试 fixture 就用 `Qwen/Qwen3-0.6B`，见 [tests/utils/constants.py:13](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/tests/utils/constants.py#L13)）——只需下载几 MB 的分词器文件，无需权重、无需 GPU。

#### 4.2.4 代码实践

**实践目标**：零外部依赖（无 etcd、无 GPU）跑通 frontend + sample worker，并发出第一条请求。

**操作步骤**：

1. 启动 frontend（file 发现模式）：

   ```bash
   python3 -m dynamo.frontend --http-port 8000 --discovery-backend file
   ```

2. 另开一个终端，启动 sample worker（同样 file 模式，模型名用真实 HF 名以便加载分词器）：

   ```bash
   python3 -m dynamo.common.backend.sample_main \
     --model-name Qwen/Qwen3-0.6B \
     --discovery-backend file
   ```

3. 第三个终端，先确认 worker 已注册（frontend 暴露的 OpenAI 模型列表）：

   ```bash
   curl -s localhost:8000/v1/models | head
   ```

4. 发第一条 chat 请求：

   ```bash
   curl -s localhost:8000/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{
       "model": "Qwen/Qwen3-0.6B",
       "messages": [{"role": "user", "content": "Hello!"}],
       "max_tokens": 32
     }'
   ```

**需要观察的现象**：

- frontend 日志先于 worker 启动时不会报错，worker 起来后 frontend 日志出现发现/注册相关的行。
- `/v1/models` 返回的列表里出现 `Qwen/Qwen3-0.6B`。
- chat 响应是一个完整的 OpenAI 格式 JSON：`choices[0].message.content` 是一串**无语义的文本**（轮转 token ID 解码的结果），`usage` 里有 token 统计。

**预期结果**：请求返回 200，`finish_reason` 为 `"length"`（因为我们只给了 32 个 max_tokens）。响应内容看起来像乱码是预期行为——sample 引擎不做真实推理。

**等价的脚本方式**（需要 etcd，或先 `docker compose -f dev/docker-compose.yml up -d`）：

```bash
MODEL_NAME=Qwen/Qwen3-0.6B ./examples/backends/sample/launch/agg.sh
```

> 待本地验证：脚本方式依赖默认 etcd 发现后端；在没起 etcd 的机器上请用上面四步的手工命令。

#### 4.2.5 小练习与答案

**练习 1**：把 worker 的 `--delay` 调成 `0.2`，响应总时长大约变成多少？为什么？

**答案**：大约 `max_tokens × 0.2` 秒（32 × 0.2 = 6.4 秒）再加少量链路开销。因为 `_generate_tokens` 对每个 token 都 `await asyncio.sleep(self.delay)`，延迟线性叠加。

**练习 2**：为什么 sample 后端推荐配真实模型名（如 `Qwen/Qwen3-0.6B`）而不是脚本默认的 `sample-model`？

**答案**：frontend 要为模型加载分词器才能把请求文本切成 token ID、把 worker 吐回的 token ID 解码成文本。真实 HF 模型名可以让 frontend 自动取到分词器（体积很小、无需权重）；而 `sample-model` 不是一个可解析的模型标识。仓库自己的测试 fixture（`tests/frontend/conftest.py` 启动 sample worker 时）也是传 `QWEN = "Qwen/Qwen3-0.6B"`。

**练习 3**：agg.sh 里 `wait_any_exit` 相比"把 worker 放前台跑"有什么好处？

**答案**：前台模式下 frontend 崩溃时脚本会一直阻塞在前台 worker 上，发现不了；`wait_any_exit` 用 `wait -n` 同时监视所有后台子进程，任何一个退出（无论 frontend 还是 worker）都立即触发清理陷阱，把其余进程一并带走，Ctrl+C 的行为也因此可预期。

### 4.3 disagg.sh：分离模式的最小拓扑

#### 4.3.1 概念说明

`disagg.sh` 在 agg.sh 的基础上把 worker 拆成两个：一个 `--disaggregation-mode prefill`、一个 `--disaggregation-mode decode`。这对应上一讲的 P/D 分离架构：prefill worker 只处理输入并产出"交接载荷"（`disaggregated_params`），decode worker 从交接点继续逐 token 生成。sample 引擎用**合成的**交接载荷走完整个线格式（wire format），没有真实 KV 传输——所以它能在纯 CPU 上当分离链路的冒烟测试。

#### 4.3.2 核心流程

```text
disagg.sh
  |-- python3 -m dynamo.frontend &                                  (进程 1)
  |-- DYN_SYSTEM_PORT=8081 sample_main --component sample-prefill \
  |     --disaggregation-mode prefill &                             (进程 2)
  |-- DYN_SYSTEM_PORT=8082 sample_main --component sample-decode \
  |     --disaggregation-mode decode &                              (进程 3)
  `-- wait_any_exit

一条请求的路径（对应上一讲 S1–S9 的简化版）：
  client -> frontend -> PrefillRouter 选中 prefill worker
         -> prefill 产出 1 个 token + disaggregated_params（合成句柄）
         -> frontend 把请求连同 prefill 结果转给 decode worker
         -> decode 逐 token 生成 -> 流式返回客户端
```

#### 4.3.3 源码精读

脚本头部的拓扑说明值得先读：

[examples/backends/sample/launch/disagg.sh:5-13](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/backends/sample/launch/disagg.sh#L5-L13) —— 注释说明了意图：spawn frontend + 一个 prefill worker + 一个 decode worker；prefill worker 以 `WorkerType::Prefill` 注册（由 Rust 侧 Worker 根据 `WorkerConfig.disaggregation_mode` 决定），frontend 的 `PrefillRouter` 负责把 prefill 产出的合成 `disaggregated_params` 转发给 decode。**GPU 数量：0**——它是统一分离路径的 CI 冒烟测试。

三个进程的启动：

[examples/backends/sample/launch/disagg.sh:53-66](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/backends/sample/launch/disagg.sh#L53-L66) —— frontend 之后是 prefill worker：通过环境变量 `DYN_SYSTEM_PORT`（缺省 8081）给它一个独立的系统指标端口，`--component sample-prefill` 让它在服务发现里以独立组件名可见，`--disaggregation-mode prefill` 声明角色。

[examples/backends/sample/launch/disagg.sh:68-77](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/backends/sample/launch/disagg.sh#L68-L77) —— decode worker 同理（端口 8082、组件 `sample-decode`、模式 `decode`），最后 `wait_any_exit` 收尾。两个 `DYN_SYSTEM_PORT` 错开是为了并行 CI 运行不撞端口——注释里点明这是从 vLLM 的 disagg 脚本镜像来的约定。

引擎侧如何扮演两个角色：

[components/src/dynamo/common/backend/sample_engine.py:342-348](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/components/src/dynamo/common/backend/sample_engine.py#L342-L348) —— `generate()` 开头的角色分派：decode worker 会用 `require_prefill_result` 强制要求请求携带 prefill 的交接结果（否则说明 frontend 没有经过 prefill 路由，直接报错）；prefill worker 用 `enforce_prefill_max_tokens` 把输出截到一个 token。

[components/src/dynamo/common/backend/sample_engine.py:416-427](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/components/src/dynamo/common/backend/sample_engine.py#L416-L427) —— prefill 的最后一个 chunk 会盖上 `disaggregated_params: {"sample_handle": <随机hex>, "completed_tokens": [...]}`——这就是"合成的 KV 交接句柄"，真实后端在这里放的会是 NIXL 传输元数据（u7-l3 展开）。

#### 4.3.4 代码实践

**实践目标**：跑通最小分离拓扑，验证同一条请求确实经过 prefill → decode 两跳。

**操作步骤**：

1. （脚本默认 etcd 发现）先起基础设施，再跑脚本：

   ```bash
   docker compose -f dev/docker-compose.yml up -d
   MODEL_NAME=Qwen/Qwen3-0.6B ./examples/backends/sample/launch/disagg.sh
   ```

2. 等 banner 出现后，用与 4.2 相同的 curl 命令发请求（端口同为 8000）。

3. 观察两个 worker 的日志：注意请求先出现在 `sample-prefill` 的输出里，随后出现在 `sample-decode` 的输出里。

**需要观察的现象**：响应正常返回；日志中 prefill 组件与 decode 组件先后处理了同一条请求；kill 掉 prefill 进程（`Ctrl+C` 分开跑时）后新请求会失败，因为链路缺了一环。

**预期结果**：端到端响应与聚合模式几乎一样（内容同样无语义），但链路上多了一跳。整个脚本无需 GPU。

**待本地验证**：若无 Docker，可仿照 4.2.4 的手工方式，以 file 模式分别启动 frontend、`--disaggregation-mode prefill` worker、`--disaggregation-mode decode` worker 三个进程（三个终端，都加 `--discovery-backend file`，模型名一致）。

#### 4.3.5 小练习与答案

**练习 1**：disagg.sh 里为什么给两个 worker 分别设 `DYN_SYSTEM_PORT=8081/8082` 和不同的 `--component` 名？

**答案**：`DYN_SYSTEM_PORT` 是各 worker 自己的系统指标/健康端口，同机并行时必须错开避免冲突（注释说明这镜像自 vLLM 的 disagg 脚本）；不同的 `--component` 名让两个 worker 在服务发现中以独立组件出现，运维和日志里能分清角色。

**练习 2**：如果直接把请求发给 decode worker（绕过 prefill），会发生什么？

**答案**：`generate()` 里的 `require_prefill_result(request, ...)` 会发现请求缺少 `prefill_result` 而抛错——sample 引擎用这种方式"大声失败"，保证分离链路的完整性约束不被悄悄绕过。

**练习 3**：sample 后端的 `disaggregated_params` 和真实后端（如 vLLM + NIXL）的有什么差别？

**答案**：sample 放的是一个随机 `sample_handle` 字符串加 token 列表，纯粹为了走通线格式；真实后端放的是 KV 传输所需的元数据（传输句柄、地址等），decode 侧据此通过 NIXL 等传输层拉取 KV 数据（u7-l3 详述）。

### 4.4 file discovery：frontend 如何找到 worker

#### 4.4.1 概念说明

`--discovery-backend file` 是本地开发的钥匙：它把"服务注册表"从 etcd 换成了一个本地目录。worker 注册 = 写文件，worker 下线 = 文件过期删除，frontend 发现 = 监听目录变化。语义上完整模拟了 etcd 的 KV + lease 模型，但零依赖。

理解它需要三个角色：

1. **选择器（Selector）**：决定用哪种 KV 存储 backend，`file` 解析出根目录。
2. **FileStore**：文件 KV 实现，用 TTL + keep-alive 线程模拟租约存活。
3. **watch**：用操作系统的文件系统事件（inotify 类机制）把目录变化变成 `Put/Delete` 事件流喂给订阅方（frontend）。

#### 4.4.2 核心流程

```text
worker 启动
  |-- 解析 --discovery-backend file
  |      -> Selector::File($DYN_FILE_KV 或 $TMPDIR/dynamo_store_kv)
  |-- 把 endpoint 元数据（namespace/component/service/endpoint -> 地址等）
  |   以"路径即 key、内容即 value"的方式写入目录（原子写：先写 .tmp_ 再改名）
  |-- 后台 keep-alive 线程每 ~TTL/3（最少 1s）刷新自己文件的时间戳
  v
frontend 启动
  |-- 同样解析出该目录
  |-- watch(): 先建立目录监听，再读一次快照（已有文件全部作为 Put 事件重放）
  |-- 持续接收文件创建/删除事件 -> 维护"活着的 worker"视图
  v
worker 被杀 / 机器断电
  |-- keep-alive 停止 -> 文件超过 TTL（默认 10s）被 expiry 线程删除
  |-- frontend 收到 Delete 事件 -> 把该 worker 移出路由目标
```

存活语义可以概括为：文件的新鲜度就是 worker 的心跳。

\[ TTL_{有效} = 10\,\text{s（默认）}, \quad \text{刷新间隔} = \max(TTL/3,\ 1\,\text{s}) \]

#### 4.4.3 源码精读

选择器如何解析 `file`：

[lib/runtime/src/storage/kv.rs:131-170](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/storage/kv.rs#L131-L170) —— `Selector` 枚举有 `Etcd / File / Memory` 三个变体；`FromStr` 实现里，字符串 `"file"` 解析为 `Selector::File(root)`，根目录取环境变量 `DYN_FILE_KV`，否则落到 `$TMPDIR/dynamo_store_kv`（Linux 上通常就是 `/tmp/dynamo_store_kv`）。同文件也可以看到 `"etcd"` 与 `"mem"` 的分支。

发现后端的顶层分发：

[lib/runtime/src/distributed.rs:634-653](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/distributed.rs#L634-L653) —— `DiscoveryBackend` 只有 `Kubernetes` 与 `KvStore(Selector)` 两个变体——etcd/file/mem 都归入 KV 存储一类；`is_local()` 判定 file 与 memory 是"无需外部服务"的本地后端，这就是"本地开发不需要 etcd/NATS"在类型层面的表达。

[lib/runtime/src/distributed.rs:696-718](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/distributed.rs#L696-L718) —— `DistributedConfig::from_settings` 读环境变量 `DYN_DISCOVERY_BACKEND`（缺省 `"etcd"`）：`kubernetes` 走 K8s API 分支，其余值解析为 KV 选择器，未知值直接 panic 报错。frontend Python 侧解析出的 `--discovery-backend` 最终也是流入这条路径。

文件存储的"租约"实现：

[lib/runtime/src/storage/kv/file.rs:30-33](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/storage/kv/file.rs#L30-L33) —— 常量 `DEFAULT_TTL = 10s`、`MIN_KEEP_ALIVE = 1s`，以及临时文件前缀 `.tmp_`（监听器会忽略它，保证不会读到写了一半的文件）。

[lib/runtime/src/storage/kv/file.rs:43-64](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/storage/kv/file.rs#L43-L64) —— `FileStore` 持有根目录与活动目录表，构造时**专门起一个原生线程**跑 `expiry_thread`——注释解释了为什么不用 tokio 任务：高负载下异步运行时可能延迟心跳，用真线程保证 keep-alive 及时。

[lib/runtime/src/storage/kv/file.rs:71-92](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/storage/kv/file.rs#L71-L92) —— `expiry_thread` 的循环：按最短 TTL 的三分之一（不少于 1 秒）睡眠，醒来先给自己持有的文件续期（keep_alive），再删除过期的文件。**worker 进程死亡后无人续期，它的注册文件最多 TTL 秒后消失**——与 etcd lease 到期收回 key 的行为一致。

frontend 侧的订阅：

[lib/runtime/src/storage/kv/file.rs:508-540](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/storage/kv/file.rs#L508-L540) —— `watch()` 用 `notify` crate 的 `RecommendedWatcher`（Linux 上即 inotify）监听桶目录，**先建监听、再读快照**（注释说明竞态期间的变更会被 notify 缓冲，重放不会产生回退），把已有文件作为 `Put` 事件先发给订阅者，之后把每个文件系统事件翻译成 `Put/Delete`。frontend 拿到这个事件流就得到了实时的 worker 视图（注册表的完整层级语义在 u3-l2 展开）。

#### 4.4.4 代码实践

**实践目标**：亲眼看到 file 模式下 worker 注册产生的文件，并验证"杀掉 worker → 文件消失"的存活语义。

**操作步骤**：

1. 按 4.2.4 启动 frontend 与 sample worker（都带 `--discovery-backend file`）。

2. 查看注册目录：

   ```bash
   ls -R ${TMPDIR:-/tmp}/dynamo_store_kv
   ```

3. 观察文件的"心跳"——每隔约 3 秒重复执行，看时间戳变化：

   ```bash
   watch -n 1 "ls -l --time-style=full-iso ${TMPDIR:-/tmp}/dynamo_store_kv/dynamo 2>/dev/null || ls -lR ${TMPDIR:-/tmp}/dynamo_store_kv"
   ```

4. `Ctrl+C` 杀掉 **worker**（保留 frontend），继续观察目录。

**需要观察的现象**：

- 步骤 2 能看到以 namespace（`dynamo`）开头的层级路径，key 的路径结构对应 `namespace/component/service/endpoint`。
- 步骤 3 中文件修改时间大约每 3 秒刷新一次（TTL 10s 的三分之一，但不会快于 1s）。
- 步骤 4 杀掉 worker 后，它的注册文件在 **10 秒内**被删除；frontend 日志随后出现 worker 移除相关的记录，`/v1/models` 里该模型消失。

**预期结果**：文件的出现/续期/过期与 4.4.2 的流程图一致。若想换个目录做实验，可用 `DYN_FILE_KV=/tmp/my_kv` 同时传给 frontend 与 worker。

**待本地验证**：不同操作系统的 inotify 行为、以及 `ls` 输出的具体层级深度可能略有差异；若目录为空，优先检查两个进程的发现后端是否都是 `file`。

#### 4.4.5 小练习与答案

**练习 1**：file 模式靠什么机制保证"worker 挂了，frontend 能发现"？延迟上限是多少？

**答案**：worker 定期给自己的注册文件续期（keep-alive 线程，间隔约 TTL/3 且不少于 1 秒）；worker 死后无人续期，`expiry_thread` 在文件超过默认 10 秒 TTL 后删除它，frontend 通过目录监听收到删除事件。延迟上限约为一个 TTL（10 秒）。

**练习 2**：为什么 `watch()` 要"先建监听、再读快照"，而不是反过来？

**答案**：先读快照后建监听的话，两次操作之间发生的写入会落进缝隙——快照里没有、监听也看不到，订阅者永远丢失这个 key。先建监听再读快照，竞态期间的变更会被 notify 缓冲，即使快照之后重放一个旧值也不会回退状态（源码注释明确说明了这一点）。

**练习 3**：`mem` 后端和 `file` 后端有什么本质区别？各自适合什么场景？

**答案**：`mem` 是纯进程内存储，注册信息根本不落盘，**跨进程不可见**，适合单进程内的单元测试；`file` 把注册写到磁盘目录，多个进程通过同一目录交互，适合本机多进程开发调试。两者都被 `is_local()` 判定为本地后端（无需 etcd/NATS）。

## 5. 综合实践

**任务**：按 agg 拓扑启动 frontend + sample worker，写一个 Python 脚本连续发送 5 条**相同前缀**的请求并打印每条 TTFT，然后结合源码解释观察到的差异。

**步骤**：

1. 启动服务（零依赖方式，两个终端）：

   ```bash
   # 终端 1
   python3 -m dynamo.frontend --http-port 8000 --discovery-backend file
   # 终端 2
   python3 -m dynamo.common.backend.sample_main \
     --model-name Qwen/Qwen3-0.6B --discovery-backend file
   ```

   等待 `curl -s localhost:8000/v1/models` 能看到模型后再开始测量。

2. 保存以下脚本为 `ttft_probe.py`（示例代码）：

   ```python
   # 示例代码：测量 5 条相同前缀请求的 TTFT
   import time
   from openai import OpenAI

   client = OpenAI(base_url="http://localhost:8000/v1", api_key="dummy")

   PREFIX = "分布式推理系统需要把 prefill 与 decode 分离，并让路由器感知 KV 缓存。" * 6

   for i in range(5):
       t0 = time.perf_counter()
       stream = client.chat.completions.create(
           model="Qwen/Qwen3-0.6B",
           messages=[{"role": "user", "content": PREFIX + f"（第 {i} 次）"}],
           max_tokens=16,
           stream=True,
       )
       ttft = None
       for chunk in stream:
           delta = chunk.choices[0].delta if chunk.choices else None
           if delta is not None and (delta.content or delta.reasoning_content):
               ttft = time.perf_counter() - t0
               break  # 拿到第一个 token 即可；如需总时长则读完整个流
       print(f"请求 {i}: TTFT = {ttft if ttft else float('nan'):.4f}s")
   ```

3. 安装依赖并运行：

   ```bash
   uv pip install openai
   python3 ttft_probe.py
   ```

4. 记录 5 条 TTFT，然后做两个对照实验：
   - 把 worker 的 `--delay` 从默认 `0.01` 改成 `0.05` 重启，再跑一遍；
   - 把 `PREFIX` 的重复次数从 6 改成 24（前缀长 4 倍），再跑一遍。

**预期结果与源码解释**：

- **第 1 条 TTFT 明显高于后续 4 条**：首次请求要付出 HTTP 连接建立、frontend 首次加载分词器、请求面 TCP 连接建立、路由器首次打分等一次性成本；后续请求复用这些状态。
- **后续 TTFT ≈ `--delay` + 小常数**：sample 引擎每个 token 前都 `await asyncio.sleep(delay)`（sample_engine.py 的 `_generate_tokens`），第一个 token 之前恰好只隔一个 `delay`。
- **`--delay 0.05` 会让 TTFT 大致线性增加约 40ms**：证明延迟主要来自引擎的逐 token 节奏，而非网络。
- **相同前缀不会带来 KV 命中型 TTFT 下降**——这是本实践最重要的源码发现：读 [components/src/dynamo/common/backend/sample_engine.py:261-278](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/components/src/dynamo/common/backend/sample_engine.py#L261-L278)，块哈希来自 `_block_hash_counter = itertools.count(1)`（[components/src/dynamo/common/backend/sample_engine.py:127](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/components/src/dynamo/common/backend/sample_engine.py#L127)），**每条请求都生成全新哈希**，索引层永远不会有跨请求的前缀重合。想观察"KV 感知路由把 TTFT 降一半"这类真实效果，需要真实后端的真实块哈希（u6 展开）。
- 前缀变长只会让 TTFT 轻微上升（分词与请求体变大），不会出现真实系统里的"长前缀反而更快"现象。

**待本地验证**：绝对数值因机器而异；请把三个实验的输出贴成表格，作为后续单元对照的基线。

## 6. 本讲小结

- Dynamo 最小可运行组合就是两个进程：`python3 -m dynamo.frontend` + 一个 worker；客户端只见 OpenAI 兼容 HTTP API（`/v1/chat/completions`、`/v1/models`）。
- 本地开发**不需要** etcd/NATS：frontend 与 worker 双方传 `--discovery-backend file` 即可；只有走示例脚本默认值（etcd）时才需要 `dev/docker-compose.yml`。
- `agg.sh` = frontend + 1 个聚合 worker；`disagg.sh` = frontend + 1 prefill + 1 decode。sample 后端用合成 `disaggregated_params` 在纯 CPU 上走通分离线格式。
- sample 后端入口链 `sample_main → run(SampleLLMEngine) → from_args → Worker.run()` 是所有 Python 后端共用的启动范式；它的 token 是按 `(i+1) % 32000` 轮转的假数据，TTFT 下界由 `--delay` 决定。
- file 发现模式 = "注册写文件 + TTL/keep-alive 模拟租约 + inotify 监听目录"；根目录在 `$DYN_FILE_KV` 或 `$TMPDIR/dynamo_store_kv`，worker 死亡后注册最多 10 秒（默认 TTL）消失。
- sample 引擎每条请求生成全新的合成块哈希，因此**观察不到跨请求 KV 前缀命中**——这解释了为什么本讲的 TTFT 实验里"相同前缀"没有带来加速。

## 7. 下一步学习建议

- **下一讲（u1-l3）**：仓库三层结构与 Cargo workspace 总览——弄清本讲遇到的 `components/`（Python 包）与 `lib/`（Rust 核心）在仓库里的完整版图。
- **顺手阅读**：[components/src/dynamo/common/backend/README.md](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/components/src/dynamo/common/backend/README.md)——sample 后端所属模块的官方参考，特别是引擎生命周期与分离契约两节。
- **预告**：想知道 frontend 收到 HTTP 请求之后、到达 worker 之前发生了什么，看 u4-l2（HttpService）与 u5（Python 前端主流程）；想深挖服务注册的 `namespace/component/service/endpoint` 层级与 etcd 语义，看 u3-l2。
