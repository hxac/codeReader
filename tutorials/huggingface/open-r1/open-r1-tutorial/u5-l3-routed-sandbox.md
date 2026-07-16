# Router 路由沙箱与限流

## 1. 本讲目标

本讲是「代码奖励与沙箱执行」单元的第三篇，承接 [u5-l2 代码执行 Provider 抽象](u5-l2-code-providers.md)。

u5-l2 解决了「奖励函数与沙箱后端解耦」的问题：`code_reward` 通过 `get_provider(provider_type)` 拿到一个 `CodeExecutionProvider`，由它在沙箱里跑代码、回收奖励。但那只回答了「换哪个后端」，没有回答「**当几十个训练进程同时跑代码奖励、每秒砸出上百次沙箱创建请求时怎么办**」。

本讲就回答这个问题。读完本讲你应该能够：

- 说清楚「为什么直连沙箱会被限流（rate limit）」以及「Router 模式如何化解」。
- 读懂 `RoutedSandbox.run_code` / `RoutedMorphSandbox.run_code` 如何把一批脚本打包成**一次** HTTP 请求发给路由服务。
- 读懂 `E2BProvider` / `MorphProvider` 在 `*_router_url` 被设置时如何切换到 Router 分支。
- 读懂 `scripts/e2b_router.py`（及 `morph_router.py`）这个 FastAPI 服务：它用一个全局 `Semaphore(max_num_sandboxes)` 作为唯一的并发闸门，并理解「多训练任务共享同一个 Router IP」为什么能避免限流。

## 2. 前置知识

在进入源码前，先建立两个直觉。

### 2.1 限流从哪里来

GRPO 的代码奖励阶段，每个训练 step 都会做这样的事：

1. 对当前 batch 里的每个 prompt，模型采样出若干条候选代码答案（`num_generations` 份）。
2. 每条答案都要送进**沙箱**（E2B 或 MorphCloud）跑测试用例，得到一个 `success_rate` 作为奖励。

于是单步就可能产生「`batch_size` × `num_generations`」个待执行脚本。在多机多卡训练里，每个 GPU 进程都独立这么做。沙箱服务（尤其是 E2B 这类托管云沙箱）会对**单个账号 / 单个来源 IP / 单位时间内的并发沙箱数**设上限，超出就返回限流错误（类似 HTTP 429）或直接拒绝创建沙箱。

直连模式（u5-l2 讲的 `_run_async` + 本进程内 `Semaphore(num_parallel)`）的问题是：**每个训练进程各自维护一个本地信号量，进程之间没有全局协调**。N 个进程 × `num_parallel`，聚合成一团不受控的请求风暴。

### 2.2 Router 模式的核心三招

Router 模式用一个常驻的 HTTP 服务（「路由器」）做中间人，用三招化解限流：

| 招式 | 直连模式 | Router 模式 |
| --- | --- | --- |
| **批量** | 每条脚本各开一个沙箱、各发一次请求 | 一批脚本打包成**一次** `POST /execute_batch` |
| **单一出口** | N 个训练节点各自从自己的 IP 发请求 | 全部请求只从 Router 节点的**一个 IP** 发出 |
| **全局闸门** | 每进程一个本地信号量，无全局协调 | Router 用**一个** `Semaphore(max_num_sandboxes)` 统一限流 |

一句话：把「N 个进程各自直连云沙箱」收敛成「所有进程 → 一个 Router → 云沙箱」。云沙箱看到的，是一个来源稳定、并发被全局封顶的单一客户，而不是一群互相抢资源的训练进程。

> 本讲提到的沙箱路由器（`scripts/e2b_router.py`、`scripts/morph_router.py`）与仓库里的 `slurm/serve_router.slurm`（那是 sglang 的**推理**路由器）是完全不同的东西，不要混淆。本讲只讲代码沙箱路由。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [src/open_r1/utils/routed_sandbox.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/routed_sandbox.py) | `RoutedSandbox`：E2B 的**客户端**，把一批脚本打包成一次 HTTP 请求发给 Router |
| [src/open_r1/utils/routed_morph.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/routed_morph.py) | `RoutedMorphSandbox`：MorphCloud 的同款客户端，返回结构略不同 |
| [src/open_r1/utils/code_providers.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/code_providers.py) | `E2BProvider` / `MorphProvider`：根据 `*_router_url` 是否设置，在「直连」与「Router」两条分支间切换 |
| [scripts/e2b_router.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/scripts/e2b_router.py) | E2B 路由服务：FastAPI + 全局信号量 + `/execute_batch` 批量接口 |
| [scripts/morph_router.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/scripts/morph_router.py) | MorphCloud 路由服务：同构实现 |
| [slurm/e2b_router.slurm](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/e2b_router.slurm) / [slurm/morph_router.slurm](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/morph_router.slurm) | 在 CPU 节点上把路由服务跑成一个长驻 Slurm 作业 |
| [src/open_r1/configs.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py) | `e2b_router_url` / `morph_router_url` 两个配置字段，是开启 Router 模式的总开关 |

## 4. 核心概念与源码讲解

### 4.1 RoutedSandbox.run_code 的 HTTP 批量请求

#### 4.1.1 概念说明

`RoutedSandbox` 是一个**极薄的 HTTP 客户端**。它的设计目标是「**伪装成 E2B 的 `Sandbox`**」——同样提供一个 `run_code(...)` 方法、返回 `Execution` 对象——这样 Provider 侧的回收逻辑（`float(execution.text)`）可以和直连模式几乎一致。区别只有一个：它不去直连 E2B 云，而是把**一整批脚本**装进一个 JSON，POST 给本地的 Router 服务。

为什么强调「一批」？因为在 GRPO 一步里，Provider 手里本来就攥着一整批 `scripts`（见 u5-l1 的 `code_reward` 末尾 `execute_scripts(scripts, [...])`）。直连模式会逐个开沙箱；Router 模式则把这批一次性交给 Router，由 Router 内部并行。**一次 HTTP = 一次 step 的全部脚本**，连接数和鉴权握手次数骤降。

#### 4.1.2 核心流程

`run_code` 的执行过程可以用下面的伪代码概括：

```
run_code(scripts, languages, timeout, request_timeout):
    timeout        ??= 300        # 每条脚本的执行超时（秒）
    request_timeout ??= 30        # 透传给 Router（用作单沙箱的 request_timeout）
    languages      ??= ["python"] * len(scripts)

    payload = { scripts, languages, timeout, request_timeout }
    response = POST  http://{router_url}/execute_batch   json=payload
    # 注意：requests.post 这里没有传 timeout=，客户端不强制 HTTP 超时

    results = response.json()
    output = []
    for result in results:
        if result["execution"] is None:        # 超时/失败时路由器回传空 execution
            output.append(Execution())          # -> .text 取不到值 -> 奖励为 None
        else:
            output.append(Execution(results=[Result(**r) ...], logs=..., error=..., execution_count=...))
    return output
```

两个关键点先记住，后面精读会展开：

1. **`request_timeout` 不是 HTTP 超时**，它只是 payload 的一个字段，会被透传给 Router，在那里变成每个沙箱的 `request_timeout`。E2B 客户端本身并没有给 `requests.post` 设超时——它信任 Router 会在合理时间内返回。
2. **失败是「软」的**：当某条脚本超时或异常，Router 回传 `execution=None`，客户端把它构造成一个空的 `Execution()`；随后 Provider 取 `.text` 会抛异常、被捕获成奖励 `None`（即「跳过该样本」，沿用 u5-l1 的语义），而不是让整个 GRPO step 崩掉。

#### 4.1.3 源码精读

类定义与构造，只存一个 `router_url`：[src/open_r1/utils/routed_sandbox.py:22-39](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/routed_sandbox.py#L22-L39)。注意类注释里点明了它的定位——「mimics the usage of 'Sandbox' ... but adds support for batch processing」（模仿 `Sandbox`，但加了批处理）。

`run_code` 的核心是组装 payload 并发出**唯一一次** POST：[src/open_r1/utils/routed_sandbox.py:60-84](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/routed_sandbox.py#L60-L84)。下面是关键几行：

```python
payload = {
    "scripts": scripts,
    "languages": languages,
    "timeout": timeout,
    "request_timeout": request_timeout,
}
response = requests.post(f"http://{self.router_url}/execute_batch", json=payload)
if not response.ok:
    print(f"Request failed with status code: {response.status_code}")
```

注意 `requests.post(...)` **没有 `timeout=` 参数**，与 Morph 客户端不同（见 4.1 末尾对比）。`not response.ok` 时只 `print` 一条告警、**不抛异常**，紧接着仍去 `response.json()`——若错误页不是合法 JSON 才会在这里炸开。这是该客户端相对「乐观」的一面。

把路由器返回的 JSON 还原成 `Execution` 对象：[src/open_r1/utils/routed_sandbox.py:84-100](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/routed_sandbox.py#L84-L100)。`execution is None` 分支造空 `Execution()`，正是前述「软失败」的落地。

文件末尾还自带一段本地自测入口，方便你不用写脚本就能验证连通性：[src/open_r1/utils/routed_sandbox.py:103-109](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/routed_sandbox.py#L103-L109)。

Morph 版 `RoutedMorphSandbox` 同构，但有两处值得注意的差异：[src/open_r1/utils/routed_morph.py:48-120](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/routed_morph.py#L48-L120)。

- **更防御**：整个请求被包在 `try/except Exception` 里，失败时为每条脚本返回一个带 `text=None`、`exception_str=error` 的鸭子类型对象，绝不抛错。
- **设了 HTTP 超时**：`requests.post(endpoint, json=payload, timeout=actual_request_timeout)`，而 E2B 版没有。
- **返回结构更朴素**：不走 `Execution`，而是用 `type("obj", (object,), {"text": ..., "exception_str": ...})` 现造一个只有两个属性的对象，因为 Morph 的 `code_reward` 只需要 `float(result.text)`。

> 小结对比：两个客户端**协议一致**（都打 `/execute_batch`、payload 字段相同），但 E2B 版「乐观、无 HTTP 超时、返回 Execution」，Morph 版「防御、有 HTTP 超时、返回极简对象」。这种差异源于两个后端 SDK 本身的成熟度与返回类型不同。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `RoutedSandbox.run_code` 真的只发**一次** HTTP、却拿到多条结果。

**操作步骤**（需要 E2B 账号与本机起 Router，无法起则改为「源码阅读型」，见下）：

1. 准备 `.env` 写入 E2B API Key，并 `pip install e2b-code-interpreter fastapi uvicorn`。
2. 在一个终端启动 Router：`python scripts/e2b_router.py`（默认监听 `0.0.0.0:8000`）。
3. 在另一个终端，直接运行 `RoutedSandbox` 的自带自测入口：
   ```bash
   python src/open_r1/utils/routed_sandbox.py
   ```
   它等价于：
   ```python
   sbx = RoutedSandbox(router_url="0.0.0.0:8000")
   codes = ["print('hello world')", "print('hello world)"]  # 第二条故意语法错
   print(sbx.run_code(codes))
   ```

**需要观察的现象**：

- Router 终端应只看到**一次** `/execute_batch` 命中（可在 `execute_batch` 里临时加一行 `print(len(batch.scripts))` 验证批大小=2）。
- 返回长度为 2：第一条正常，`logs.stdout` 含 `hello world`；第二条因语法错误，`execution` 为空对象或 `error` 非空。

**预期结果**：与 [tests/slow/test_code_reward.py:87-103](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/slow/test_code_reward.py#L87-L103)（`test_e2b_router_run_code_success`）和 [tests/slow/test_code_reward.py:105-114](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/slow/test_code_reward.py#L105-L114)（`test_e2b_router_run_code_with_error`）的断言一致——两条脚本、一条成功一条失败。**待本地验证**（依赖真实 E2B 与网络）。

若本机无 E2B，改做**源码阅读型实践**：在 `run_code` 的 `response = requests.post(...)` 一行旁，按伪代码画出「输入 N 条 → 1 个 payload → 1 次 POST → N 个 Execution」的数据流，并回答：为什么 `request_timeout` 即便小于 `timeout`，也不会让客户端提前断开？（提示：它没被传给 `requests.post`。）

#### 4.1.5 小练习与答案

**练习 1**：如果 Router 返回的某条结果 `execution=None`，最终这条样本的奖励会是多少？为什么不会让训练崩溃？

> **答案**：客户端把它造成空 `Execution()`；Provider 取 `float(execution.text)` 时抛异常、被 `except` 捕获，奖励记为 `None`（Morph 版记 `0.0`）。`None` 沿用 u5-l1 的「跳过样本、不奖不罚」语义，故不会崩。

**练习 2**：`RoutedSandbox` 和 `RoutedMorphSandbox` 都打 `/execute_batch`、payload 字段相同，为什么前者返回 `Execution`、后者返回只有 `text/exception_str` 的临时对象？

> **答案**：因为两个后端 Provider 回收奖励的方式不同——E2B 侧用 E2B 官方的 `Execution`/`Result` 模型（`.text` 是其属性），Morph 侧只需要末行文本做 `float()`，故用最简单的鸭子类型即可，不必引入 E2B 的类型。

---

### 4.2 E2BProvider 在 router_url 模式下的分支

#### 4.2.1 概念说明

u5-l2 讲过：`get_provider(provider_type)` 是工厂，`E2BProvider` / `MorphProvider` 都实现 `execute_scripts(scripts, languages) -> List[float]`。本模块聚焦一个此前被略过的细节——**同一个 Provider 类内部，还有「直连」与「Router」两条子分支**，切换开关就是构造时传入的 `e2b_router_url` / `morph_router_url`。

这个设计的好处是：**奖励函数和 YAML 配置完全不需要知道后端怎么跑**。读者只要在 YAML 里多写一行 `e2b_router_url: 1.2.3.4:8000`，同一个 `code_reward` 就从「每进程直连云沙箱」无缝切到「走集中式 Router」，代码一个字不改。

#### 4.2.2 核心流程

配置的流转链路（从 YAML 到沙箱）：

```
recipes/.../config_*.yaml
   e2b_router_url: 1.2.3.4:8000
        │  (TrlParser 解析进 GRPOScriptArguments，见 u1-l4)
        ▼
grpo.py 的 reward_funcs —— 注册表把 "code" 映射到 code_reward（见 u3-l2）
        │
        ▼
code_reward(completions, **kwargs)        # kwargs 里带着 e2b_router_url
        │  rewards.py:586-590
        ▼
get_provider(provider_type="e2b", num_parallel=2, **kwargs)
        │  code_providers.py:339-366  把 e2b_router_url 从 kwargs 取出
        ▼
E2BProvider(num_parallel=2, e2b_router_url="1.2.3.4:8000")
        │
        ▼
execute_scripts(scripts, languages)
        │  e2b_router_url is not None  ──▶  Router 分支
        ▼
RoutedSandbox(...).run_code(scripts, languages, timeout=30, request_timeout=28)
```

关键判断就一句：`if self.e2b_router_url is not None:`。**是 `None` 就直连，非 `None` 就走 Router**。`num_parallel` 只在直连分支里作本地信号量上限，在 Router 分支里被忽略——因为真正的并发闸门已经搬到 Router 服务里去了（见 4.3）。

#### 4.2.3 源码精读

构造函数同时接收两套旋钮，`e2b_router_url` 默认 `None`（即默认直连）：[src/open_r1/utils/code_providers.py:66-80](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/code_providers.py#L66-L80)。

`execute_scripts` 的两条分支：[src/open_r1/utils/code_providers.py:82-113](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/code_providers.py#L82-L113)。Router 分支关键代码：

```python
if self.e2b_router_url is not None:
    routed_sandbox = RoutedSandbox(router_url=self.e2b_router_url)
    executions = routed_sandbox.run_code(
        scripts=scripts, languages=languages, timeout=30, request_timeout=28,
    )
    rewards = []
    for execution in executions:
        try:
            rewards.append(float(execution.text))
        except Exception:
            rewards.append(None)
    return rewards
```

三个要点：

1. 调用 `run_code` 时硬编码 `timeout=30, request_timeout=28`——这两个值与直连分支 `_run_script` 里的 `SANDBOX_TIMEOUT=30 / REQUEST_TIMEOUT=28` 完全一致（见 [src/open_r1/utils/code_providers.py:141-144](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/code_providers.py#L141-L144)），保证「换通道不换每条脚本的超时语义」。
2. 回收奖励同样用 `float(execution.text)`：沙箱以 Notebook 方式捕获 `evaluation_script_template` 末行 `evaluate_code(...)` 的返回值 `success_rate`（见 u5-l1），`execution.text` 就是它。
3. 失败记 `None`——与直连分支「失败记 `0.0`」不同。这是一个容易踩的差异：**Router 模式下解析失败是 `None`（跳过），直连模式下是 `0.0`（惩罚）**。

工厂 `get_provider` 负责把 `e2b_router_url` 从 `**kwargs` 里挑出来：[src/open_r1/utils/code_providers.py:339-366](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/code_providers.py#L339-L366)。它先 `kwargs.pop("num_parallel", 2)`，再按 `provider_type` 分别 `pop("e2b_router_url")` / `pop("morph_router_url")`，其余 kwargs 被「弹干净」，避免传给不认识它的构造函数。

Morph 侧的切换更彻底：构造时一旦 `morph_router_url is not None`，就**提前 `return`**，连 `MorphCloudClient` / API Key 都不初始化：[src/open_r1/utils/code_providers.py:195-197](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/code_providers.py#L195-L197)。`execute_scripts` 则用 `hasattr(self, "routed_sandbox")` 判断走哪条：[src/open_r1/utils/code_providers.py:222-241](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/code_providers.py#L222-L241)。这说明一个事实：**Router 模式下，训练节点根本不需要任何沙箱凭据**——凭据只存在于 Router 节点。这也是安全上的好处。

配置字段定义在 `GRPOScriptArguments` 里，两个字段、默认都是 `None`：[src/open_r1/configs.py:298-306](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L298-L306)。

#### 4.2.4 代码实践

**实践目标**：确认「在 YAML 加一行 `e2b_router_url`」就足以让 `code_reward` 切换执行通道，且不需要在本机配 E2B 凭据。

**操作步骤**（源码追踪型，不需要 GPU/E2B）：

1. 打开任一代码奖励配方，例如 `recipes/Qwen2.5-Coder-7B-Instruct/grpo/` 下的 `config_codeforces.yaml`（参见 u6-l2）。
2. 想象在文件里新增两行：
   ```yaml
   reward_funcs: ["code"]            # 或 ioi/cf 相关奖励
   e2b_router_url: 1.2.3.4:8000      # 这一行是总开关
   ```
3. 按 4.2.2 的链路，逐跳回答：
   - 这个 `e2b_router_url` 会被 `TrlParser` 路由到哪个 dataclass？（答：`GRPOScriptArguments`）
   - 它如何进入 `code_reward`？（答：作为 `reward_kwargs`/`**kwargs`）
   - 在 `get_provider` 里它被谁 `pop` 出来？（答：`provider_type == "e2b"` 分支）
   - 最终触发 `execute_scripts` 的哪一行分支？（答：`if self.e2b_router_url is not None:`）

**需要观察的现象 / 预期结果**：你能讲清楚「字段名 → dataclass → kwargs → 工厂 → 分支」的完整跳转，并指出此时训练节点**不需要** `E2B_API_KEY`（凭据只在 Router 节点的 `.env` 里）。这是纯阅读任务，**结论可在不运行任何命令的情况下得出**。

#### 4.2.5 小练习与答案

**练习 1**：同一个 `code_reward`，直连模式下脚本解析失败记 `0.0`，Router 模式下记 `None`。这两种结果对 GRPO 训练分别意味着什么？

> **答案**：`0.0` 会作为「错误答案」参与优势估计，给出负梯度；`None` 在 trl 的奖励处理里通常被视为「跳过该样本」，不贡献梯度。换言之，Router 模式更倾向「拿不准就不罚」，直连模式更倾向「失败即惩罚」。混用两种模式时要注意这个隐含差异。

**练习 2**：为什么 `MorphProvider` 在 Router 模式下连 `MORPH_API_KEY` 都不读？

> **答案**：因为真正调 MorphCloud 的是 Router 服务（`scripts/morph_router.py`），凭据只需在 Router 节点配置。训练节点只发 HTTP 给 Router，不直接接触 Morph SDK，故无需任何凭据——这也降低了在多台训练机上分发密钥的安全风险。

---

### 4.3 router 服务的部署与共享 IP

#### 4.3.1 概念说明

4.1 讲了客户端怎么发、4.2 讲了 Provider 怎么切换，本模块讲「**接收那一端**」——Router 服务本身，以及它为什么必须是一个**独立部署、被所有训练任务共享**的常驻进程。

Router 服务的本质，是把 u5-l2 里「每进程的本地信号量」**提升为全局唯一信号量**。它是一个用 FastAPI 写的 HTTP 服务，常驻在一台 CPU 节点上，持有沙箱凭据，内部用一个 `asyncio.Semaphore(max_num_sandboxes)` 作为**全局并发闸门**。所有训练任务的代码奖励请求都流向它，它再以受控的并发去开沙箱。

为什么是「共享一个 IP」而不是「每个训练任务各起一个 Router」？因为限流是**按来源**计的：多个 Router = 多个来源 = 又回到「多源并发」的老问题。只有当**所有训练任务指向同一个 Router URL** 时，云沙箱才看到单一的、并发被全局封顶的客户。README 说得很直白：「All training jobs can share the same router IP which will ensure parallel executions are properly managed.」

#### 4.3.2 核心流程

Router 服务（以 `e2b_router.py` 为例）启动后做三件事：

```
1. create_app(args):
     app.state.sandbox_semaphore = asyncio.Semaphore(args.max_num_sandboxes)  # 全局闸门，默认 20
     暴露 GET  /health        -> {"status": "ok"}
     暴露 POST /execute_batch -> execute_batch(batch)

2. execute_batch(batch):              # batch 是 BatchRequest(scripts, languages, timeout, request_timeout)
     semaphore = app.state.sandbox_semaphore
     asyncio_timeout = batch.timeout + 1

     async def run_script(script, language):
         async with semaphore:                           # 全局抢一个名额（最多 max_num_sandboxes 个并发）
             try:
                 sandbox = await AsyncSandbox.create(timeout=timeout, request_timeout=request_timeout)
                 execution = await asyncio.wait_for(
                     sandbox.run_code(script, language=language),
                     timeout=asyncio_timeout,            # 超时硬兜底
                 )
                 return ScriptResult(execution=execution, exception_str=None)
             except Exception as e:
                 return ScriptResult(execution=None, exception_str=str(e))   # 软失败
             finally:
                 await sandbox.kill()                    # 必清理

     tasks = [run_script(s, l) for s, l in zip(batch.scripts, batch.languages)]
     return await asyncio.gather(*tasks)                 # 整批并发，受 semaphore 封顶
```

部署侧，`slurm/e2b_router.slurm` 把它包成一个**长驻 CPU 作业**：`#SBATCH --partition=hopper-cpu`、`--cpus-per-task=16`、`--time=7-00:00:00`，正文就是一行 `srun python scripts/e2b_router.py`。Morph 版同理（`slurm/morph_router.slurm`，默认端口 8001）。

端到端的请求流转图（**这是本讲最该记的一张图**）：

```
   训练节点 (GPU, 可多台)                Router 节点 (CPU, 单 IP, 长驻)                E2B / Morph 云沙箱
   ─────────────────────                ───────────────────────────────                ───────────────────
   GRPO 一步
   │ code_reward(..., e2b_router_url=R)
   │ E2BProvider.execute_scripts (router 分支)
   │ RoutedSandbox.run_code
   │   ── 1 次 HTTP POST ─────────────▶│  POST /execute_batch
   │      json={scripts:[s0..sN],       │  Semaphore(max=20)  ◀── 全局唯一并发闸门
   │           languages, timeout,      │  asyncio.gather(run_script × N)
   │           request_timeout}         │      async with semaphore:
   │                                    │        AsyncSandbox.create()  ─────────────▶│  沙箱 #1
   │                                    │        AsyncSandbox.create()  ─────────────▶│  沙箱 #2
   │                                    │        ... (并发 ≤ 20)                      │   ...
   │                                    │        sandbox.kill()                       │  (统一来源、统一账号)
   │                                    │  return ScriptResult × N
   │   ◀── [Execution] × N ─────────────│
   │ float(exec.text) -> rewards        │
   ▼ 优势估计                           │  (所有训练任务都指向同一个 R)
```

图中三个关键收敛点：(1) 每步只发 **1 次** HTTP；(2) 并发由 Router 的**一个**信号量封顶；(3) 云沙箱只看到 Router 节点的**一个来源**。这三点合起来就是「避免限流」的全部原理。

#### 4.3.3 源码精读

数据模型 `BatchRequest` / `ScriptResult` 是客户端 payload 的镜像：[scripts/e2b_router.py:32-64](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/scripts/e2b_router.py#L32-L64)。注意 `ScriptResult` 用 `model_config = ConfigDict(arbitrary_types_allowed=True)` 才能容纳 E2B 的 `Execution`（非 Pydantic 原生类型）。

`create_app` 创建 app 并把信号量挂到 `app.state`（FastAPI 推荐的「应用级共享状态」写法）：[scripts/e2b_router.py:93-97](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/scripts/e2b_router.py#L93-L97)。

`execute_batch` 与内层 `run_script` 是 Router 的心脏——抢信号量、开沙箱、`wait_for` 硬超时、`finally` 必杀沙箱：[scripts/e2b_router.py:102-134](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/scripts/e2b_router.py#L102-L134)。三个细节：

- `asyncio_timeout = batch.timeout + 1`：比单沙箱超时多 1 秒，作兜底（u5-l2 提过「E2B 自带超时不可靠，靠 `asyncio.wait_for` 硬兜」的同一思路）。
- 异常一律转成 `ScriptResult(execution=None, exception_str=str(e))`——**软失败**，对应 4.1 客户端看到的 `execution is None`。
- `finally: await sandbox.kill()` 无论成败都清理，防沙箱泄漏（泄漏的沙箱会持续占用配额，间接触发限流）。

命令行参数与启动：默认 `--host 0.0.0.0 --port 8000 --max_num_sandboxes 20`，最后 `uvicorn.run`：[scripts/e2b_router.py:139-161](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/scripts/e2b_router.py#L139-L161)。

Morph 版 `morph_router.py` 同构，但因 Morph SDK 是**同步阻塞**的，全用 `asyncio.to_thread(...)` 把 `Sandbox.new` / `sandbox.run_code` / `sandbox.close` 扔进线程池，且 `run_code` 的 `timeout` 单位是毫秒（`timeout * 1000`）：[scripts/morph_router.py:96-136](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/scripts/morph_router.py#L96-L136)。默认端口 8001，且强制要求 `MORPH_API_KEY`：[scripts/morph_router.py:143-166](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/scripts/morph_router.py#L143-L166)。

部署与共享 IP 的官方说明在 README 的「Using Router Services」一节：[README.md:318-342](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/README.md#L318-L342)。要点：`sbatch slurm/e2b_router.slurm` 起服务 → 在训练 YAML 写 `e2b_router_url: <IP>:8000` → 「All training jobs can share the same router IP」。

Slurm 作业本体很薄，就是个常驻 CPU 进程：[slurm/e2b_router.slurm:1-17](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/e2b_router.slurm#L1-L17)。

#### 4.3.4 代码实践

**实践目标**（本讲指定的核心实践）：阅读 `scripts/e2b_router.py`，写一段说明——「当多个训练任务共享同一个 Router 时，`RoutedSandbox` 如何帮助避免 E2B 限流」，并画出请求流转图。

**操作步骤**：

1. 通读 [scripts/e2b_router.py:66-136](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/scripts/e2b_router.py#L66-L136)，定位三处：信号量在哪里创建、在哪里被 `async with` 获取、超时如何兜底。
2. 用你自己的话写一段 ≤150 字的说明，必须覆盖「批量 / 单一出口 / 全局闸门」三点中的至少两点。
3. 参照 4.3.2 的流转图，画一张属于你自己的版本，要求标注：训练节点数（假设 4 台）、单步 batch 脚本数（假设 64）、Router 的 `max_num_sandboxes`（假设 20），并在图上算出「4 台训练机同时各发 1 个 batch = 共 256 脚本」时，Router 如何把这 256 个脚本的并发压到 ≤20。

**需要观察的现象 / 预期结果**：

- 你的说明应能指出：直连模式下 4 台机器各自最多 `num_parallel` 并发、彼此无协调，聚合并发 = `4 × num_parallel` 且来自 4 个 IP；Router 模式下，无论多少台机器、多少脚本，同一时刻全局只有 ≤ `max_num_sandboxes` 个沙箱存活，且全部来自 Router 的单一 IP。
- 流转图应能体现「多个 `execute_batch` 请求在 Router 内部被信号量串行化（并发封顶）」这一关键收敛。

**待本地验证**：若你想实测，可在本机起 `python scripts/e2b_router.py --max_num_sandboxes 2`，再用两个终端同时跑 4.1.4 的自测脚本，观察 Router 日志里并发被压到 2。需要真实 E2B 账号。

#### 4.3.5 小练习与答案

**练习 1**：Router 已经有 `asyncio.wait_for(..., timeout=asyncio_timeout)` 兜底超时了，`finally: sandbox.kill()` 还有必要吗？

> **答案**：有必要。`wait_for` 超时只会取消 `await`，并不保证远端沙箱被销毁；如果不 `kill()`，超时/异常的沙箱会继续存活并占用 E2B 配额，堆积后反而更容易触发限流。`kill()` 是「防泄漏」，与「防超时」是两件事。

**练习 2**：如果把 `max_num_sandboxes` 设得非常大（比如 10000），Router 模式还能避免限流吗？

> **答案**：不能。`max_num_sandboxes` 就是全局并发上限，设太大等于放弃了「全局闸门」这一招，回到接近直连的高并发状态，仍会被云沙箱按账号/IP 限流。它的正确取值应略低于「云沙箱对该账号的并发容忍度」，是一个需要按账号配额调参的值（仓库默认 20）。

## 5. 综合实践

把本讲三个模块串起来：**用一段不依赖任何云沙箱账号的示例代码，复刻「客户端批量 POST → Router 信号量限流 → 回收奖励」的最小闭环**。

下面的示例代码用 FastAPI 复刻一个**假 Router**（沙箱用 `exec` 模拟，捕获末行 `print` 的数值当奖励），并用 `requests` 复刻一个**假客户端**。它跑得起来、能让你肉眼看到「一次 HTTP 携带 N 条脚本、并发被信号量封顶」。

> 安装：`pip install fastapi uvicorn requests`。示例代码，**非项目原有文件**。

```python
# 示例代码：mock_router.py —— 复刻 scripts/e2b_router.py 的核心结构
import asyncio
from fastapi import FastAPI, Request
from pydantic import BaseModel
import uvicorn

class BatchRequest(BaseModel):
    scripts: list[str]
    languages: list[str]
    timeout: int
    request_timeout: int

class ScriptResult(BaseModel):
    text: str | None
    exception_str: str | None

app = FastAPI()
MAX = 2  # 对应 --max_num_sandboxes，故意调小以便观察限流
app.state.sem = asyncio.Semaphore(MAX)

@app.post("/execute_batch")
async def execute_batch(batch: BatchRequest, request: Request):
    sem = request.app.state.sem
    in_flight = 0  # 统计本批当前在途脚本数（进/出平衡），便于观察并发被压到 MAX

    async def run_script(script: str) -> ScriptResult:
        nonlocal in_flight
        async with sem:                       # 全局闸门：同一时刻最多 MAX 个在跑
            in_flight += 1
            print(f"  [router] start (in-flight {in_flight})")
            try:
                await asyncio.sleep(0.2)      # 假装沙箱有延迟，好让并发上限可见
                ns = {}
                exec(compile(script, "<s>", "exec"), ns)   # 假沙箱：直接本地执行
                # 约定：脚本里给 __reward__ 赋一个数值当作奖励
                return ScriptResult(text=str(ns.get("__reward__", "0.0")), exception_str=None)
            except Exception as e:
                return ScriptResult(text=None, exception_str=str(e))
            finally:
                in_flight -= 1

    tasks = [run_script(s) for s in batch.scripts]
    return await asyncio.gather(*tasks)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
```

```python
# 示例代码：mock_client.py —— 复刻 RoutedSandbox + Provider 回收
import requests

def run_code(router_url, scripts):
    payload = {"scripts": scripts, "languages": ["python"]*len(scripts),
               "timeout": 5, "request_timeout": 5}
    resp = requests.post(f"http://{router_url}/execute_batch", json=payload)
    results = resp.json()
    rewards = []
    for r in results:
        try:
            rewards.append(float(r["text"]))     # 对应 float(execution.text)
        except Exception:
            rewards.append(None)                  # 对应 Router 模式的 None 语义
    return rewards

if __name__ == "__main__":
    scripts = [f"print({i})\n__reward__ = {i/4}" for i in range(6)]  # 6 条脚本
    print("rewards =", run_code("127.0.0.1:8000", scripts))
```

**操作步骤**：

1. 一个终端 `python mock_router.py`；另一个终端 `python mock_client.py`。
2. 把 `MAX` 从 2 改成 6，再跑一次，观察 Router 日志里并发变化。
3. 在 `mock_client.py` 里把某条脚本改成有语法错误，确认它的奖励变成 `None` 而不是让整批失败。

**需要观察的现象**：

- 客户端只发 **1 次** HTTP，却拿到 **6** 个奖励。
- `MAX=2` 时 Router 日志里 `in-flight` 峰值不超过 2（6 条脚本被分成若干轮，每轮 ≤2）；改成 `MAX=6` 后峰值能到 6。这正是「全局信号量把并发封顶」的可视化。
- 单条脚本失败 → 该条奖励 `None`，其余正常。

**预期结果**：`rewards = [0.0, 0.25, 0.5, 0.75, 1.0, 1.25]`（对应 `i/4`）。**待本地验证**。

完成本实践后，你应该能向别人解释清楚：**为什么把这段逻辑从训练进程里搬到一个独立常驻服务、再让所有训练进程共享它，就能避免限流**——这正是 `scripts/e2b_router.py` 存在的全部理由。

## 6. 本讲小结

- **限流根因**：GRPO 代码奖励阶段每步产生大量脚本，多机直连云沙箱 = 多源、无全局协调的高并发，触发账号/IP 级限流。
- **Router 三招**：批量（一次 `POST /execute_batch` 携带整批脚本）、单一出口（全部请求出自 Router 节点一个 IP）、全局闸门（一个 `Semaphore(max_num_sandboxes)` 统一限流）。
- **客户端**：`RoutedSandbox` / `RoutedMorphSandbox` 是薄 HTTP 客户端，伪装成各自后端的 `Sandbox.run_code`，把整批脚本打包成一次请求；E2B 版乐观无 HTTP 超时、返回 `Execution`，Morph 版防御性更强、带 HTTP 超时、返回极简对象。
- **Provider 分支**：`e2b_router_url` / `morph_router_url` 非 `None` 即走 Router 分支，`num_parallel` 在该分支被忽略；Morph 模式下连凭据都不在训练节点出现。
- **软失败语义**：Router 模式解析失败记 `None`（跳过样本），与直连模式的 `0.0`（惩罚）不同，混用要注意。
- **部署**：Router 是跑在 CPU 节点上的常驻 FastAPI 服务（`slurm/e2b_router.slurm` / `slurm/morph_router.slurm`），所有训练任务在 YAML 指向同一个 Router URL 即可共享。

## 7. 下一步学习建议

- **横向迁移**：本讲的「集中式服务 + 全局信号量」思路，与 u6-l3 的 `PistonClient`（多端点令牌桶负载均衡）是同一类问题的两种解法，建议对比阅读，理解「自建 Router 收敛请求」与「多端点分散负载」的取舍。
- **纵深 finishing**：代码奖励链路至此已完整——u5-l1 判分模板、u5-l2 Provider 抽象、本讲 Router 限流。下一单元 u6 将进入更上层的「竞赛编程评分」（IOI / Codeforces），那里的 `ioi_code_reward` / CF 评分最终也会落到本讲这套沙箱执行基础设施上，届时可回看本讲确认执行层如何被复用。
- **源码延伸阅读**：若想了解 Router 之外另一种「批处理 + 限流」实践，可直接读 [scripts/e2b_router.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/scripts/e2b_router.py) 与 [scripts/morph_router.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/scripts/morph_router.py) 的差异，体会「同一接口、不同 SDK 成熟度」带来的实现分歧。
