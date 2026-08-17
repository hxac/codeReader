# u2-l3 用推理引擎重生成目标答案

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立启动一个 OpenAI 兼容的本地推理服务（示例为 SGLang），并用 `scripts/data/generate_train_data.py` 让目标模型重写数据集中的 assistant 回复。
2. 讲清楚脚本的三大工程机制：多服务器负载均衡 + 每服务器并发上限（背压）、错误样本单独落盘、`--resume` 断点续跑。
3. 从投机解码的原理出发，解释**为什么训练数据必须由目标模型自己重新生成**，而不能直接用原始语料里的答案。

本讲在数据流水线中的位置：上一讲（u2-l1）产出的 `train_datasets/perfectblend_train.jsonl` 是本讲的**输入**，本讲的**输出** `perfectblend_train_regen.jsonl` 将在 u2-l5 中被 `prepare_target_cache.py` 消费，生成训练用的目标缓存。

## 2. 前置知识

### 2.1 OpenAI 兼容接口是什么

OpenAI 的 Chat Completions API 已经成为推理服务的事实标准：任何服务只要暴露 `http://<host>:<port>/v1/chat/completions` 这个 HTTP 端点、接受 `{"model", "messages", "temperature", ...}` 形式的 JSON 请求，客户端就可以用官方 `openai` Python 包（改一下 `base_url`）直接访问。SGLang、vLLM、TGI 等推理引擎都实现了这套协议，所以 DeepSpec 的数据脚本**不绑定任何引擎**——你甚至可以用真正的 OpenAI API，只要传对 `--server-address`。

本讲会用到两个稍冷门的客户端特性：

- `extra_body`：OpenAI SDK 不认识的参数（如 SGLang 的 `top_k`、`min_p`、`chat_template_kwargs`）可以塞进这个字典，SDK 会原样并入请求体。
- `presence_penalty`：脚本把 `--repetition-penalty` 映射成这个标准参数（见 4.1.3），因为 repetition penalty 不是 OpenAI 协议的标准字段。

### 2.2 推理服务化：一份权重，多个 worker

`Qwen/Qwen3-4B` 这样的模型加载进显存后以 HTTP 服务形式常驻，才能被高并发请求复用。一台 8 卡机器上可以起 8 个独立的服务进程，每个绑定一张 GPU 和一个端口——这就是 `launch_sglang_server.sh` 做的事。

### 2.3 为什么必须重生成答案（本讲的核心动机）

承接 u1-l1 的结论：投机解码中草稿模型提议的 token 按接受概率

\[ \text{accept}(x) = \min\left(1,\ \frac{p_{\text{target}}(x)}{p_{\text{draft}}(x)}\right) \]

被目标模型逐个验证。期望接受长度（每轮平均提交的 token 数）决定了加速比，而它**只有在草稿分布逼近目标分布时才会高**。

问题在于：`mlabonne/open-perfectblend` 是从互联网上多个来源混合出的语料，里面的 assistant 回复出自各种不同的模型（甚至人类）。如果直接拿这些回复训练草稿模型，草稿学到的是「互联网平均文风」，而不是**你这一个目标模型**的条件分布——风格、用词、思考方式都对不上，验证时会被大量拒绝。

因此 DeepSpec 的做法是：只保留数据集中的 **user 提问**（ prompts），把所有 assistant 回复**丢弃后由目标模型现场重写**。README 明确说明了这一点：每个已发布 checkpoint 都是「用对应目标模型在非思考模式下生成的 open-perfectblend 数据」训练的（[README.md:55](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/README.md#L55)）。这样训练对 \((\text{目标隐状态},\ \text{目标的下一个 token})\) 才是自洽的：草稿学的正是目标自己的行为。

> 术语：**目标模型（target model）**是被加速的大模型；**草稿模型（draft model）**是为它训练的小模型。本讲中「重生成」的执行者永远是目标模型。

### 2.4 并发编程基础：ThreadPoolExecutor 与 Future

Python 的 `concurrent.futures.ThreadPoolExecutor` 把函数提交到线程池执行，`submit()` 立即返回一个 `Future` 对象；调用 `future.done()` 可查询是否完成，`future.result()` 可取回返回值。由于推理请求是网络 I/O，线程（而非进程）就足够并行。看懂 `future.done()` / `future.result()` 这两个调用，就能看懂本讲的调度循环。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| [scripts/data/generate_train_data.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L1-L367) | 367 | 本讲主角：读取输入 JSONL，逐条调用推理服务重写 assistant 回复，写出成功/错误两份 JSONL |
| [scripts/data/launch_sglang_server.sh](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/launch_sglang_server.sh#L1-L127) | 127 | 在本机每张可见 GPU 上各拉起一个 SGLang 服务进程，附心跳日志与退出清理 |
| [scripts/data/README.md](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/README.md#L1-L138) | 138 | 三步数据流水线的官方文档，含推荐采样参数与 38 TB 缓存警告 |
| [scripts/data/prepare_data.sh](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_data.sh#L1-L63) | 63 | 把三步串起来的包装脚本，记录了默认参数（并发 32、temperature 0.7 等） |
| [requirements.txt](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/requirements.txt#L16-L19) | — | `openai==2.6.1` 是本脚本唯一的网络依赖；SGLang 需另行安装 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **OpenAI 兼容 API 客户端**——单条样本如何被重写（`call_sglang` + `build_query_kwargs`）。
2. **并发与限速控制**——服务集群如何拉起、健康检查、轮转分发与背压（`launch_sglang_server.sh` + `validate_servers` + 主循环调度）。
3. **失败处理与 resume**——失败的样本去哪了、断点续跑如何算偏移（`error_sample` + `find_resume_offset` + 追加写模式）。

### 4.1 OpenAI 兼容 API 客户端：单条样本的重写过程

#### 4.1.1 概念说明

这个模块解决的问题是：给定一行训练数据 \(\{id, conversations\}\)（来自 u2-l1 的输出），把其中**所有 assistant 消息替换为目标模型生成的回复**，user 与 system 消息原样保留。

两个关键设计：

- **逐轮生成，而非一次生成**：多轮对话里第 2 个 user 提问的答案依赖前面的上下文。如果前面 assistant 回复已经被重写了，那么生成第 2 轮答案时必须以**重写后的历史**为条件，否则上下文不一致。所以脚本在遍历对话时边重写边拼接。
- **原始 assistant 回复被直接丢弃**：它们唯一的用处是占位说明「这里该有一条 assistant 消息」，内容本身不用（原因见 2.3）。

#### 4.1.2 核心流程

```text
call_sglang(sample):
    校验 conversations 非空、首条不是 assistant（否则标 error）
    为该 server 创建 OpenAI 客户端（base_url 指向该 server）
    regenerated = []
    逐条遍历消息:
        system   -> 原样保留
        assistant-> 跳过（丢弃原始答案）
        user     -> 追加，然后带着 regenerated 里已有的历史调一次
                    chat.completions.create，把返回内容包装成新的
                    assistant 消息追加
    sample["conversations"] = regenerated
    sample["status"] = "success"
```

注意：一个 \(n\) 轮 user 提问的样本会产生 \(n\) 次串行的 API 调用；样本之间才是并行的。

#### 4.1.3 源码精读

入口校验：数据必须非空、且以 user 开头，否则这条样本被标记为错误而不是让程序崩溃（这是数据脚本的通用风格：坏样本隔离，好样本继续跑）。

[scripts/data/generate_train_data.py:106-113](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L106-L113) 中 `call_sglang` 先做上述校验，然后为**当前分发到的这台服务器**创建客户端——注意客户端是按 server 创建的，这是 4.2 负载均衡的前提：

```python
conversations = sample.get("conversations")
if not conversations:
    return error_sample(sample, "Missing conversations")
if conversations[0].get("role") == "assistant":
    return error_sample(sample, "Data starts with an assistant message")

client = OpenAI(base_url=f"http://{server_address}/v1", api_key="None")
```

（`api_key="None"` 是占位字符串：本地服务不鉴权，但 SDK 要求非空。）

对话遍历与重写：[scripts/data/generate_train_data.py:116-144](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L116-L144)。`role == "assistant"` 的分支只有 `continue`——这就是「丢弃原始答案」的落点；每遇到一条 user 消息就以当前 `regenerated` 为历史发起一次补全，异常被捕获并转成错误样本：

```python
for message in conversations:
    role = message.get("role")
    if role == "system":
        regenerated.append(message)
        continue
    if role == "assistant":
        continue                      # 原始答案直接丢弃
    ...
    regenerated.append(message)
    try:
        response = client.chat.completions.create(
            **build_query_kwargs(args, regenerated, max_tokens=max_tokens)
        )
    except Exception as exc:
        return error_sample(sample, str(exc))
```

返回内容包装成新 assistant 消息；若声明了 `--is-reasoning-model`，还会把 SGLang 返回的 `reasoning_content` 存进 `thinking` 字段（[scripts/data/generate_train_data.py:134-140](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L134-L140)）。

采样参数的组装在 [scripts/data/generate_train_data.py:70-97](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L70-L97) 的 `build_query_kwargs`，有三处值得注意的映射规则：

```python
if args.repetition_penalty is not None:
    query_kwargs["presence_penalty"] = args.repetition_penalty   # 非标准参数 -> 标准参数

extra_body = {}
if args.top_k is not None:
    extra_body["top_k"] = args.top_k                             # 非标准参数 -> extra_body
if args.disable_thinking:
    extra_body.setdefault("chat_template_kwargs", {})["enable_thinking"] = False
```

1. `top_k` / `min_p` 不是 OpenAI 协议标准字段，走 `extra_body` 透传给 SGLang；
2. thinking 开关通过 `chat_template_kwargs.enable_thinking` 传给服务端的聊天模板（Qwen3 的模板支持该开关）——官方流水线用的是 `--disable-thinking`，与 README「非思考模式生成」的说法对应；
3. `--is-gpt-oss` 时每次请求随机注入 `reasoning_effort`，三档 low/medium/high 的权重是 4:4:2（[scripts/data/generate_train_data.py:53-54](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L53-L54)、[L95-L96](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L95-L96)）——这是为 GPT-OSS 系目标模型准备的数据增广。

命令行参数与取值校验见 [scripts/data/generate_train_data.py:12-50](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L12-L50)：`--enable-thinking` 与 `--disable-thinking` 互斥（`add_mutually_exclusive_group`），temperature 被限制在 \([0, 1]\)。

#### 4.1.4 代码实践

**实践目标**：不用任何 GPU，先在「协议层」验证你对客户端机制的理解——确认 `extra_body` 与丢弃-重写逻辑。

**操作步骤**（源码阅读 + 本地小实验，二选一或都做）：

1. 打开 [scripts/data/generate_train_data.py:106-144](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L106-L144)，在纸上模拟一个 `conversations = [user A, assistant X, user B]` 的样本走一遍 `call_sglang`：记录共发生几次 API 调用、每次 `messages` 里各有什么。
2. （示例代码，需要本地起任意 OpenAI 兼容服务后才可运行）写一个 15 行的小脚本验证 `extra_body`：

```python
# 示例代码：验证 extra_body 透传（非项目原有代码）
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:30000/v1", api_key="None")
resp = client.chat.completions.create(
    model="Qwen/Qwen3-4B",                     # 换成你实际服务的模型名
    messages=[{"role": "user", "content": "用一句话介绍你自己"}],
    max_tokens=32,
    temperature=0.7,
    extra_body={"top_k": 20, "chat_template_kwargs": {"enable_thinking": False}},
)
print(resp.choices[0].message.content)
```

**需要观察的现象**：步骤 1 中应为 **2 次** API 调用——第一次 `messages=[user A]`，第二次 `messages=[user A, assistant(重写), user B]`；原始的 `assistant X` 从未出现在任何请求里。

**预期结果**：纸上推演与源码一致；步骤 2 若服务已启动会打印一句话回答（本机无 GPU 时标注：**待本地验证**）。

#### 4.1.5 小练习与答案

**练习 1**：如果一个样本的对话是 `[assistant, user]`（assistant 在最前），`call_sglang` 会怎么处理？为什么要有这条防御？

**答案**：直接返回 `error_sample(sample, "Data starts with an assistant message")`，该样本被写入 error JSONL。因为重写流程以 user 消息为触发点，首条是 assistant 意味着第一条 user 之前还有一段需要条件化的 assistant 历史，而这段历史同样应该由目标模型生成，脚本不支持这种形态；u2-l1 的 `validate_conversations` 已保证正常数据不会出现这种情况，这里是双保险。

**练习 2**：`--repetition-penalty 1.1` 请求发出后，HTTP 报文里的字段名是什么？为什么这样映射？

**答案**：`presence_penalty: 1.1`。因为 OpenAI Chat Completions 协议没有 `repetition_penalty` 字段，直接放进请求体可能被严格校验的服务拒绝；映射到语义相近的标准字段可以保证对任何兼容实现都可用（见 [generate_train_data.py:80-81](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L80-L81)）。

**练习 3**：`--enable-thinking` 和 `--disable-thinking` 同时传会怎样？

**答案**：argparse 报错退出。两者在 [generate_train_data.py:32-34](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L32-L34) 的 mutually exclusive group 里，因为它们最终写同一个字段 `chat_template_kwargs.enable_thinking`，同时传语义矛盾。

### 4.2 并发与限速控制：服务集群、健康检查与背压调度

#### 4.2.1 概念说明

百万级样本 × 每样本多轮补全，意味着千万次量级的 API 调用，单服务器串行是不可接受的。这个模块解决三件事：

1. **拉起一组服务**：`launch_sglang_server.sh` 在本机每张 GPU 上起一个独立的 SGLang 进程，各自监听一个端口。
2. **健康检查**：开工前先用一条 `max_tokens=1` 的极小请求探测每个 server，坏的剔除，全坏则报错退出。
3. **客户端侧调度**：样本以轮转（round-robin）方式分给各个 server；每个 server 同时最多挂 `--concurrency` 个在途请求，超了就等——这就是**背压（backpressure）**，防止把某个服务打爆或把内存撑爆。

#### 4.2.2 核心流程

服务端（shell 脚本）：

```text
launch_sglang_server.sh:
    读取配置（模型路径、worker 数、起始端口、显存占比…）
    探测本机 IP
    for gpu_id in 0..num_workers-1:
        CUDA_VISIBLE_DEVICES=$gpu_id sglang serve --port $((start_port+gpu_id)) &
        记录 pid/port
    启动心跳循环（每 300s 打印各 worker 存活状态）
    trap INT/TERM/EXIT -> cleanup: 杀掉心跳与所有 worker
    wait 所有 worker
```

客户端（Python 主循环）的调度不变式：

\[ \text{在途请求总数} \le \text{server 数} \times \text{concurrency} \]

```text
for 每一行输入:
    server = valid_servers[next_server_index]        # 轮转选择
    next_server_index 前进并取模
    while queues[server] 已满 (>= concurrency):      # 背压
        找出一个已完成的 future -> 写盘 -> 移出队列
        若一个都没完成 -> sleep 0.05s 再看
    提交 call_sglang 到线程池，future 进 queues[server]
收尾: 依次收割所有队列中剩余 future
```

#### 4.2.3 源码精读

服务端脚本的关键是这一段——每张可见 GPU 一个进程、端口随 GPU 序号递增，日志按 `worker_<ip>_gpu_<id>_port_<port>.log` 落盘（[scripts/data/launch_sglang_server.sh:96-113](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/launch_sglang_server.sh#L96-L113)）：

```bash
for ((gpu_id = 0; gpu_id < num_workers; gpu_id++)); do
    port=$((start_port + gpu_id))
    ...
    CUDA_VISIBLE_DEVICES=${gpu_id} sglang serve \
        --model-path "${model_path}" \
        --host "${host}" \
        --port "${port}" \
        --nccl-port "${nccl_port}" \
        --dtype "${dtype}" \
        --mem-fraction-static "${mem_frac}" \
        "$@" > "${log_file}" 2>&1 &
    pids+=("$!")
done
```

三个工程细节：

- 脚本支持**透传额外参数**：`"$@"` 把命令行上传给 `launch_sglang_server.sh` 的参数原样转给 `sglang serve`，例如 `bash launch_sglang_server.sh --context-length 8192`。
- [launch_sglang_server.sh:82-94](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/launch_sglang_server.sh#L82-L94) 的 `cleanup` 通过 `trap … INT TERM EXIT` 注册：Ctrl-C 或退出时先杀心跳进程再逐个杀 worker，不留孤儿进程。
- [launch_sglang_server.sh:55-80](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/launch_sglang_server.sh#L55-L80) 的心跳循环每 300 秒用 `kill -0`（不发信号只探测存活）打印一次各 worker 状态——长时间数据生成任务里用来快速判断哪个 worker 挂了。默认配置（8 workers、`Qwen/Qwen3-4B`、`mem_frac=0.9`）见 [launch_sglang_server.sh:8-16](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/launch_sglang_server.sh#L8-L16)。

客户端的健康检查：[scripts/data/generate_train_data.py:161-230](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L161-L230)。探测请求是一条 `{"role": "user", "content": "Hello"}`、`max_tokens=1` 的对话（[L171-L174](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L171-L174)）——用最小代价区分「服务活着」和「服务没起好」，各 server 的探测也是并行的（`ThreadPoolExecutor(max_workers=server_count)`）。全部失败时直接 `RuntimeError("No available sglang server")`（[L228-L230](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L228-L230)），部分失败则打印可用/不可用两份清单后继续。

真正的调度核心在 `main` 的主循环（[scripts/data/generate_train_data.py:309-352](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L309-L352)）。线程池大小是**并发数 × server 数**（[L313](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L313)），与轮转 + 每服务器队列配合：

```python
ThreadPoolExecutor(max_workers=args.concurrency * len(valid_servers)) as executor,
...
    sample = json.loads(line)
    server_address = valid_servers[next_server_index]
    next_server_index = (next_server_index + 1) % len(valid_servers)

    while len(queues[server_address]) >= args.concurrency:   # 背压窗口
        wrote_result = False
        for future in list(queues[server_address]):
            if future.done():
                write_finished_result(future, output_handle, error_handle, stats)
                queues[server_address].remove(future)
                wrote_result = True
                break
        if not wrote_result:
            time.sleep(0.05)

    future = executor.submit(call_sglang, args, server_address, sample)
    queues[server_address].append(future)
```

读这段要抓三个不变式：

1. **轮转保证均匀**：第 \(i\) 个提交的样本去 `valid_servers[i mod S]`，\(S\) 为 server 数。
2. **`queues[server]` 是该 server 的在途请求集合**，长度达到 `concurrency` 就不再给它派新活，先收割已完成的（`write_finished_result` 把结果写盘并更新统计）。
3. **结果的写盘时机是「提交遇到背压」或「输入耗尽后的收尾」**，不是完成即写——这换来一个重要性质：**每个 server 队列内的结果按提交顺序写出**。

完成结果的统计与落盘在 [generate_train_data.py:233-254](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L233-L254)：error 样本进 error 文件，成功样本进输出文件，同时用 `compute_context_length`（[L57-L67](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L57-L67)，按空格切词近似词数，兼容字符串与多模态 list 两种 content 形态）累积 min/max/avg 上下文长度统计，跑完打印（[L355-L362](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L355-L362)）——这个统计可以帮你判断数据长度分布是否超出后续 `max_length` 截断阈值。

#### 4.2.4 代码实践

**实践目标**：搞清楚轮转 + 背压在不同参数下的行为，验证「每 server 在途上限」这条不变式。

**操作步骤**：

1. 纯推演（无需机器）：假设 `valid_servers = [s0, s1, s2]`、`concurrency = 2`。写出第 1~7 个样本各被分给哪个 server，并回答：第几个样本提交时第一次可能触发背压等待？
2. 阅读型验证：在 [generate_train_data.py:332-343](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L332-L343) 的 `while len(queues[server_address]) >= args.concurrency:` 一行旁加注释（脑内或本地副本），说明如果把这里的 `time.sleep(0.05)` 改成 `0`，CPU 占用会发生什么。
3. （可选，需本地有服务）用 `--concurrency 1 --num-samples 6` 跑一次，观察 tqdm 速度；再改 `--concurrency 8` 重跑同样 6 条，对比总耗时。

**需要观察的现象**：步骤 1 中分配序列应为 s0, s1, s2, s0, s1, s2, s0；当 s0 的队列里已有 2 个在途、第 7 个样本又轮到 s0 时触发背压。步骤 3 中并发 8 明显快于并发 1（若服务端成为瓶颈则加速小于 8 倍）。

**预期结果**：推演与源码一致；步骤 3 的具体加速比**待本地验证**（取决于机器）。

#### 4.2.5 小练习与答案

**练习 1**：8 台 server、`--concurrency 32`，线程池有多少线程？任意时刻在途请求最多多少个？

**答案**：线程 \(8 \times 32 = 256\) 个；在途请求同样最多 256 个——每台 server 的队列长度被钳制在 32。两个数字相等并非巧合：线程池容量与背压窗口联合保证了「每线程最多一个在途请求」。（见 [generate_train_data.py:313](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L313) 与 [L332](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L332)。）

**练习 2**：为什么探测 server 用 `max_tokens=1`，而不是干脆发一条正常长度的请求？

**答案**：探测只关心「服务是否可用」，`max_tokens=1` 让探测请求几乎不占生成时间与显存（[generate_train_data.py:164](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L164)）。另外它复用了 `call_sglang` 本身，顺带验证了「客户端到服务的整条链路（含 chat template 渲染）」都通。

**练习 3**：某台 server 中途宕机，这个脚本会发生什么？

**答案**：分到它的样本会在 `client.chat.completions.create` 处抛异常，被 `try/except` 捕获转成 error 样本写进 error JSONL；其余 server 照常工作，脚本不会整体失败。宕机的 server 不会被动态剔除（健康检查只在开工前做一次），所以恢复手段是：停掉脚本、重启 server、用 `--resume` 续跑。

### 4.3 失败处理与 resume：错误样本收集与断点续跑

#### 4.3.1 概念说明

先纠正一个容易望文生义的点：这个脚本**没有自动重试**。它的容错哲学是「快速失败、错误隔离、事后断点续跑」：

- 任何一条样本处理失败（数据非法、API 异常），立即写进 `<output>_error.jsonl`，带 `status` 与 `error` 字段说明原因，**不重试、不阻塞**其他样本。
- `--resume` 重新运行时，数一数输出文件 + 错误文件的**总行数**，就知道已经处理了多少条输入，直接跳过这些行从断点继续，输出以**追加**模式打开。

为什么够用：失败原因是客户端可见的（数据坏 / 服务挂），机械重试意义不大；而「成功 + 错误 = 已处理」的计数式断点实现极简单，不依赖任何额外的状态文件。

#### 4.3.2 核心流程

```text
main():
    error_path = output 路径把 .jsonl 换成 _error.jsonl
    若 --resume:
        skip = 行数(output) + 行数(error_path)
        若 skip >= 输入总行数: 打印"全部处理完"并退出
    file_mode = "a"（resume 且有进度）否则 "w"
    打开输入/输出/错误三个文件
    先从输入里跳过 skip 行
    ...正常调度循环...
```

正确性依赖一个不变式：**每个被处理过的输入样本，最终恰好向两个输出文件之一写入一行**。因此总行数就是已处理样本数，且与写出顺序无关。

#### 4.3.3 源码精读

错误样本的统一包装：[scripts/data/generate_train_data.py:100-103](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L100-L103)——只在原样本上追加 `status: "error"` 和 `error: <原因>` 两个字段，原始数据保留，方便事后排查或修复重跑：

```python
def error_sample(sample, message):
    sample["status"] = "error"
    sample["error"] = message
    return sample
```

断点偏移的计算：[scripts/data/generate_train_data.py:147-158](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L147-L158)。`count_lines` 逐行数文件（不用 `readlines`，内存友好），`find_resume_offset` 返回三元组 `(已处理数, 成功数, 错误数)`：

```python
def find_resume_offset(output_path, error_path):
    if not os.path.exists(output_path):
        return 0, 0, 0
    success_count = count_lines(output_path)
    error_count = count_lines(error_path) if os.path.exists(error_path) else 0
    return success_count + error_count, success_count, error_count
```

`main` 里的接线：[scripts/data/generate_train_data.py:277-297](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L277-L297)。注意错误文件路径的推导规则（把输出路径中的 `.jsonl` 后缀替换为 `_error.jsonl`，[L278](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L278)）以及「全部完成时安静退出」的分支：

```python
total_lines = count_lines(args.input_file_path)
error_path = args.output_file_path.replace(".jsonl", "_error.jsonl")
skip_lines, existing_success, existing_errors = (
    find_resume_offset(args.output_file_path, error_path)
    if args.resume
    else (0, 0, 0)
)
if skip_lines >= total_lines:
    print(f"All {total_lines} samples are already processed.")
    return
...
file_mode = "a" if args.resume and skip_lines > 0 else "w"
```

跳过已处理输入与收尾收割：[generate_train_data.py:315-316](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L315-L316) 用 `next(input_handle, None)` 消耗掉前 `skip_lines` 行；[L350-L352](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L350-L352) 在输入耗尽后把所有 server 队列里**仍在途**的 future 逐一 `result()` 写盘——注意这里没有超时保护，若某个请求卡死，收尾会一直等。

一个隐含约定值得指出：resume 的正确性只要求「行数 = 已处理数」，**不要求输出行序与输入行序一致**（如 4.2.3 所说，跨 server 的完成顺序会交错写出）。这是计数式断点相对顺序式断点更鲁棒的地方。

#### 4.3.4 代码实践

**实践目标**：在不碰 GPU 的前提下，完整验证 resume 的偏移计算与追加写逻辑。

**操作步骤**：

1. 手工构造输入文件 `in.jsonl`（示例代码，5 行即可，每行形如 `{"id": i, "conversations": [{"role": "user", "content": "hi"}]}`）。
2. 手工模拟一次中断：自己往 `out.jsonl` 写 2 行、往 `out_error.jsonl` 写 1 行（内容随意，形如成功/错误样本即可）。
3. 在 Python 里 `import` 该模块的三个函数（脚本是普通模块，可直接导入），调用 `find_resume_offset("out.jsonl", "out_error.jsonl")`，检查返回值。
4. 阅读式推演：带着 `skip_lines=3` 走一遍 [L315-L316](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L315-L316) 与 [L297](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L297)，确认第 4、5 行会被处理且输出是追加。

**需要观察的现象**：步骤 3 返回 `(3, 2, 1)`；步骤 4 推得 `file_mode == "a"`、输入从第 4 行开始消费。

**预期结果**：与上述一致。若你的构造导致返回值不同，先检查错误文件名是否严格是 `输出名去掉 .jsonl 后加 _error.jsonl`。真实端到端行为**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：输出文件 100 行、错误文件 5 行，`--resume` 重跑会跳过多少行输入？如果输入总共正好 105 行呢？

**答案**：跳过 \(100 + 5 = 105\) 行；此时 `skip_lines >= total_lines` 成立，脚本打印 `All 105 samples are already processed.` 后直接返回（[generate_train_data.py:284-286](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/generate_train_data.py#L284-L286)）——`--resume` 重跑已完成任务是幂等的。

**练习 2**：为什么不把失败样本自动重试 3 次，而是直接写 error 文件？什么场景下你会希望改造成自动重试？

**答案**：失败大多是非瞬态的（数据格式坏、server 配置错），机械重试浪费配额且拖慢整体；写 error 文件保留了完整现场（样本 + 原因），可以修复后单独重跑。若失败主要由瞬态网络抖动或服务端限流造成（error JSONL 里大量 timeout / 429 类错误），就值得在 `call_sglang` 的 `except` 分支加带退避的重试。

**练习 3**：假如脚本在收尾阶段（输入已读完、部分 future 还在途）被 Ctrl-C 杀掉，resume 语义会被破坏吗？

**答案**：会偏保守但不会错。被杀时在途样本的结果没写盘，下次 resume 的 `skip_lines` 只统计已写出的行，所以这些样本会被**重新处理一遍**——宁可重做也不丢数据。唯一被破坏的场景是「同一行被写了一半时被杀」（JSONL 行不完整导致行数统计仍正确、但该行内容损坏），概率极低；这也是为什么行数计数比「记录最后处理的样本 id」更简单且基本可靠。

## 5. 综合实践

把三个模块串成一次真实的小规模运行（对应讲义规格中的实践任务：对 20 条样本重生成答案）。

**目标**：起一个本地 OpenAI 兼容服务 → 用脚本重生成 20 条样本的 assistant 回复 → 检查输出与错误两份 JSONL → 再体验一次 resume。

**步骤**：

1. **准备输入**：从 u2-l1 的输出取前 20 行（没有的话现场手造 20 行 `{"id": i, "conversations": [{"role": "user", "content": "..."}]}`，其中故意混入 1 条首消息是 assistant 的脏数据，用于验证 error 路径）：

   ```bash
   head -n 20 train_datasets/perfectblend_train.jsonl > /tmp/tiny_train.jsonl
   ```

2. **起服务**（任选其一，GPU 紧张时把 `num_workers` 改小、换小模型如 `Qwen/Qwen3-0.6B`）：

   ```bash
   # 方式 A：本仓库脚本（默认 8 卡 Qwen3-4B，按需编辑脚本头部变量）
   bash scripts/data/launch_sglang_server.sh
   # 方式 B：vLLM 等价命令（单卡）
   # vllm serve Qwen/Qwen3-0.6B --port 30000
   ```

   等服务就绪后用 `curl http://127.0.0.1:30000/v1/models` 确认。

3. **重生成 20 条**：

   ```bash
   python scripts/data/generate_train_data.py \
       --model Qwen/Qwen3-0.6B \
       --server-address 127.0.0.1:30000 \
       --concurrency 4 \
       --temperature 0.7 --top-p 0.8 --top-k 20 --min-p 0 \
       --max-tokens 256 \
       --disable-thinking --resume \
       --input-file-path /tmp/tiny_train.jsonl \
       --output-file-path /tmp/tiny_regen.jsonl
   ```

4. **检查产物**：
   - `/tmp/tiny_regen.jsonl`：每行的 `conversations` 里 assistant 消息应全部来自目标模型；对照 `id` 抽 2 条与原始输入 diff，确认 user 消息原文未动、assistant 内容已换。
   - `/tmp/tiny_regen_error.jsonl`：若第 1 步混入了脏数据，应能看到那条样本带 `"status": "error"` 与原因字符串。
   - 终端最后打印的 `success / errors / context_min / context_max / context_avg` 统计。
5. **体验 resume**：不加参数直接重跑第 3 步命令，应看到 `All 20 samples are already processed.` 然后退出。再手工删掉输出文件的最后一行重跑，观察日志里 `Resume mode: N success, M errors, skip K` 的数字。

**预期结果**：输出 JSONL 行数 = 输入行数 − 错误行数；resume 幂等。具体生成内容与耗时**待本地验证**（取决于所选拿模型与机器）。

## 6. 本讲小结

- 训练数据必须由**目标模型自己**重写 assistant 回复：草稿模型要逼近的是目标的条件分布，混合语料的「平均文风」会拉低投机解码的接受率；重写时逐轮串行、以重写后的历史为上下文。
- 客户端是纯 OpenAI 兼容协议：`top_k`/`min_p`/thinking 开关走 `extra_body` 透传，`repetition_penalty` 映射为标准的 `presence_penalty`，因此 SGLang/vLLM/任何兼容服务都能直接替换。
- 并发模型 = 每 GPU 一个服务进程（shell 脚本拉起 + 心跳监控 + trap 清理）× 客户端轮转分发 × 每 server `--concurrency` 上限的背压窗口，线程池容量 = 并发 × server 数。
- 容错策略是「不重试、错误隔离、计数式断点」：失败样本带原因写入 `_error.jsonl`；`--resume` 以「成功行数 + 错误行数」为偏移跳过已处理输入，输出以追加模式打开，重跑幂等。
- 本讲输出 `perfectblend_train_regen.jsonl` 是下一阶段（target cache 生成）的直接输入；SGLang 用完记得停掉，再进入第 3 步。

## 7. 下一步学习建议

重生成后的对话数据接下来要被**离线前向**一遍目标模型、把中间层隐状态落盘。下一篇 u2-l4（`u2-l4-target-cache-format.md`）先讲清楚缓存 sidecar 的存储协议——`manifest.json`、`samples.idx` 定长索引与分片文件布局；随后 u2-l5 精读 `prepare_target_cache.py`，看它如何用 forward hook 抍取指定层输出。如果你对重生成数据如何变成 `input_ids/loss_mask` 感兴趣，可以回顾 u2-l2 的 parser；想了解这份数据最终在训练里怎么被消费，留到 u2-l6 的 CacheDataset。
