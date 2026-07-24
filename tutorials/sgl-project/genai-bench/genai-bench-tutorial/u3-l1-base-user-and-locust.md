# User 基类与 Locust 集成

## 1. 本讲目标

在 [u2-l4](u2-l4-sampler-and-request-construction.md) 里，采样器（Sampler）把「场景」和「数据集」揉合成了一个 `UserRequest` 对象。但这些请求对象此刻还只是「躺在内存里的数据」，并没有真正发往被测的 LLM 服务。本讲要解决的问题是：**谁来发起请求、如何把请求与 Locust 的并发引擎绑定、又如何把单次请求的耗时数据汇总成可统计的指标。**

学完本讲你应当能够：

1. 说清 genai-bench 是如何把一个自定义的 `BaseUser` 嫁接到 Locust 的 `HttpUser` 之上，从而获得「虚拟用户」能力的。
2. 描述一次请求从 `sample()` 取数据、到 `send_request()` 发送、再到 `collect_metrics()` 上报指标的完整时序。
3. 解释 `collect_metrics` 内部「两路出口」——既调用 Locust 原生 `events.request.fire` 更新统计，又通过 `send_message("request_metrics", ...)` 把结构化指标送给 master runner 聚合。
4. 指出 `BaseUser` 留给子类的关键扩展点（`supported_tasks`、`sample()`、`@task` 方法、`collect_metrics`），为 [u3-l2](u3-l2-openai-user-response-parsing.md) 及多后端讲义做铺垫。

## 2. 前置知识

### 2.1 Locust 是什么

[Locust](https://docs.locust.io/) 是一个 Python 写的开源压测框架。它的核心思想是：**你用代码描述「一个虚拟用户」的行为，Locust 就帮你同时启动成百上千个这样的用户去压目标服务。** 每个「虚拟用户」在 Locust 里是一个轻量协程（greenlet），它会在一个循环里不断执行你定义的任务（task）。

要理解 genai-bench 的 User 体系，先记住 Locust 的四个概念：

| 概念 | 作用 |
| --- | --- |
| `HttpUser` | Locust 内置基类，给每个虚拟用户配一个 HTTP 客户端 `self.client`，并约定 `host`、`wait_time` 等属性。 |
| `@task` 装饰器 | 标记在「用户类」的方法上，Locust 会在运行时反复调用这些方法，模拟用户的一次次操作。 |
| `environment` | 一个进程内共享的 `Environment` 对象，持有 `runner`（调度器）、`events`（事件总线）、`stats`（统计表）等。所有虚拟用户共享同一个 environment。 |
| `events.request.fire(...)` | Locust 原生的「上报一次请求」事件，调用后会更新 `stats` 统计表、驱动 Web UI。 |

> 提示：还记得 [u1-l1](u1-l1-project-overview.md) 提到的关键运行前提吗——`__init__.py` 必须最先执行 `gevent.monkey.patch_all()`。这正是因为 Locust 的协程并发依赖 gevent，必须把标准库的阻塞 I/O 打补丁成协程友好的版本。genai-bench 用 Locust 做「并发引擎」，所以也继承了这一前提。

### 2.2 genai-bench 给 environment 额外挂的两个属性

原版 Locust 的 `environment` 并没有 `sampler` 和 `scenario` 这两个属性，它们是 genai-bench **额外挂上去**的：

- `environment.sampler`：一个 `Sampler` 实例（见 u2-l4），负责按场景生成 `UserRequest`。在 [genai_bench/cli/cli.py:383](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L383) 处由主流程注入：`environment.sampler = sampler`。
- `environment.scenario`：一个 `Scenario` 实例（见 [u2-l2](u2-l2-scenario-definition.md)），描述「这一次压测每个请求的输入/输出规模」。它由分布式 runner 在切换场景时下发，见 [genai_bench/distributed/runner.py:333](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L333)。

把这两个属性「挂」在 environment 上，而不是作为参数传来传去，是 genai-bench 的一个关键设计：**让每一个虚拟用户都能在运行时通过 `self.environment` 直接拿到「当前该发什么请求」的上下文。**

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [genai_bench/user/base_user.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/base_user.py) | 定义 `BaseUser(HttpUser)`，是所有后端 User 的共同基类，提供 `sample()` 与 `collect_metrics()` 两个核心方法。本讲的绝对主角。 |
| [genai_bench/user/openai_user.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py) | `OpenAIUser(BaseUser)`，最典型的子类。我们用它说明子类如何填 `supported_tasks`、写 `@task` 方法、并调用基类的 `collect_metrics`。 |
| [genai_bench/metrics/request_metrics_collector.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/request_metrics_collector.py) | `RequestMetricsCollector`，把 `UserResponse` 换算成 `RequestLevelMetrics`（TTFT/TPOT/吞吐等）。`collect_metrics` 内部就是调用它。指标公式细节留到 u4-l1，本讲只点出它在链路中的位置。 |
| [genai_bench/distributed/runner.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py) | `DistributedRunner`，注册 `request_metrics` 消息处理器，是 `collect_metrics` 发出消息的「接收方」。用来理解指标的最终去向。 |
| [genai_bench/protocol.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/protocol.py) | 数据契约：`UserRequest`、`UserResponse` 等（见 u1-l5）。本讲把它们当作「在链路里流动的对象」。 |
| [tests/user/test_base_user.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/user/test_base_user.py) | `BaseUser` 的单测，用真实 Locust `Environment` 验证 `sample()` 与 `collect_metrics()` 的行为，是本讲代码实践的依据。 |

## 4. 核心概念与源码讲解

### 4.1 BaseUser 与 Locust HttpUser

#### 4.1.1 概念说明

`BaseUser` 是 genai-bench 所有「后端用户」的共同祖先。它的第一行继承就把它和 Locust 绑在了一起：

```python
from locust import HttpUser
...
class BaseUser(HttpUser):
```

[genai_bench/user/base_user.py:1](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/base_user.py#L1) 引入 `HttpUser`，[genai_bench/user/base_user.py:12](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/base_user.py#L12) 声明 `BaseUser(HttpUser)`。这句话的含义是：**genai-bench 的虚拟用户「就是」Locust 的虚拟用户**，它继承了 Locust 的全部并发调度能力（被 runner 调度、共享 environment、按 `wait_time` 间隔循环执行 `@task` 方法），又在上面加了三件 genai-bench 特有的事：

1. 一个类级字典 `supported_tasks`，声明「这个后端支持哪些任务、每个任务对应哪个方法」。
2. 一个 `sample()` 方法，作为「取请求」的统一入口。
3. 一个 `collect_metrics()` 方法，作为「报指标」的统一出口。

需要强调：`BaseUser` 本身**不带任何 `@task` 方法**，它只定义骨架。真正能跑起来的用户是它的子类（如 `OpenAIUser`）。源码用 `__new__` 显式阻止了直接实例化 `BaseUser`。

#### 4.1.2 核心流程

一个后端 User 子类从「被 Locust 创建」到「开始干活」的过程：

```text
Locust runner 启动
   │
   ├─ 按配置的并发数，实例化 N 个 OpenAIUser（或其它子类）作为 greenlet
   │      ※ 实例化时 BaseUser.__new__ 校验：不能直接 new BaseUser
   │
   ├─ 对每个 user 调用 on_start()        ← 子类在这里初始化 auth headers
   │
   └─ 进入 task 循环：
         Locust 依据权重随机挑一个 @task 方法（如 chat）
            │
            ├─ 调 self.sample()           ← 取一个 UserRequest（见 4.2）
            ├─ 构造 payload，发 HTTP 请求  ← 子类自己实现
            ├─ 解析响应为 UserResponse     ← 子类自己实现
            └─ 调 self.collect_metrics()  ← 报指标（见 4.3）
         按 wait_time 间隔，重复以上循环
```

#### 4.1.3 源码精读

**类声明与 `supported_tasks` 字典。**

[genai_bench/user/base_user.py:12-L13](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/base_user.py#L12-L13)：`BaseUser` 继承 `HttpUser`，并声明一个空的类属性 `supported_tasks: Dict[str, str] = {}`。键是任务字符串（如 `"text-to-text"`），值是该任务对应的**方法名**（如 `"chat"`）。

```python
class BaseUser(HttpUser):
    supported_tasks: Dict[str, str] = {}
```

子类只要覆写这个字典就「注册」了自己能干的事。例如 `OpenAIUser` 在 [genai_bench/user/openai_user.py:34-L41](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py#L34-L41) 把六种任务映射到六个方法名：

```python
supported_tasks = {
    "text-to-text": "chat",
    "image-text-to-text": "chat",
    "text-to-embeddings": "embeddings",
    "text-to-rerank": "rerank",
    "text-to-image": "images_generations",
    "text-to-speech": "speech",
}
```

注意键用的是 u2-l1 讲过的「`<input>-to-<output>`」任务字符串；值是**方法名字符串**而非方法本身——这样校验层（`validation.py` 里的 `API_BACKEND_USER_MAP`）就能在不实例化的前提下，仅凭类查到「这个后端支持哪些任务」。

**`is_task_supported` 类方法。**

[genai_bench/user/base_user.py:23-L25](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/base_user.py#L23-L25)：判断某任务是否被支持，本质就是查 `supported_tasks` 字典的成员关系。

```python
@classmethod
def is_task_supported(cls, task: str) -> bool:
    return task in cls.supported_tasks
```

它是类方法（`@classmethod`），所以无需实例化即可调用，CLI 校验阶段会用到。

**`__new__` 守卫：禁止直接实例化基类。**

[genai_bench/user/base_user.py:15-L18](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/base_user.py#L15-L18) 用 `__new__`（而非 `__init__`）在对象真正创建之前就拦截：

```python
def __new__(cls, *args, **kwargs):
    if cls is BaseUser:
        raise TypeError("BaseUser is not meant to be instantiated directly.")
    return super().__new__(cls)
```

关键在 `cls is BaseUser`：它判断的是「正在被实例化的类是不是 BaseUser 本身」。子类（如 `OpenAIUser`）实例化时 `cls` 不是 `BaseUser`，会正常通过；只有 `BaseUser()` 这种写法会抛 `TypeError`。之所以用 `__new__` 而不是在 `__init__` 里检查，是因为 `__new__` 能在对象分配前就拒绝，更干净（连内存都不分配）。测试 [tests/user/test_base_user.py:44-L46](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/user/test_base_user.py#L44-L46) 正好验证了这一点。

**子类的 `on_start`：初始化认证头。**

Locust 约定：每个虚拟用户启动时会调用一次 `on_start()`。`OpenAIUser` 在 [genai_bench/user/openai_user.py:47-L56](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py#L47-L56) 用它把认证头准备好：

```python
def on_start(self):
    if not self.host or not self.auth_provider:
        raise ValueError("API key and base must be set for OpenAIUser.")
    auth_headers = self.auth_provider.get_headers()
    self.headers = {**auth_headers, "Content-Type": "application/json"}
    self.api_backend = getattr(self, "api_backend", self.BACKEND_NAME)
    super().on_start()
```

这里 `self.host`、`self.auth_provider` 都是 Locust 在实例化时通过 `environment.runner` 注入到实例上的类/实例属性（见 L43-L45 的类属性声明），`auth_provider` 来自 u5 的认证体系。注意结尾的 `super().on_start()`——显式回调基类，保证未来 `BaseUser` 若在 `on_start` 里加点公共逻辑也不会被子类漏掉。

#### 4.1.4 代码实践

**实践目标**：亲手验证「BaseUser 不能被直接实例化，但子类可以」，并理解 `supported_tasks` 的查表行为。

**操作步骤**：

1. 在仓库根目录进入 Python（需已按 [u1-l2](u1-l2-install-and-first-run.md) 安装好 genai-bench 及其依赖）：

```bash
python -c "from genai_bench.user.base_user import BaseUser; BaseUser()"
```

2. 再尝试实例化一个合法子类（用一个临时的子类，复刻测试里的做法）：

```python
# 示例代码（非项目原有代码）
from genai_bench.user.base_user import BaseUser

class ConcreteUser(BaseUser):
    host = "http://example.com"
    supported_tasks = {"text-to-text": "chat"}

print(ConcreteUser.is_task_supported("text-to-text"))   # True
print(ConcreteUser.is_task_supported("text-to-embeddings"))  # False
```

**需要观察的现象**：

- 第 1 步会抛出 `TypeError: BaseUser is not meant to be instantiated directly.`
- 第 2 步两个断言分别打印 `True` 和 `False`。

**预期结果**：与 [tests/user/test_base_user.py:44-L46](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/user/test_base_user.py#L44-L46) 中 `pytest.raises(TypeError)` 的行为一致；`is_task_supported` 纯粹是字典成员判断。

> 待本地验证：第 1 步命令行是否确为 `TypeError`，取决于你环境里 Locust 是否已随 `genai-bench` 安装到位。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `__new__` 里判断的是 `cls is BaseUser` 而不是 `isinstance(self, BaseUser)`？

**参考答案**：`__new__` 在对象创建**之前**执行，此时还没有 `self`；它接收的 `cls` 是「即将被实例化的类」。用 `cls is BaseUser` 能精确区分「直接造基类」与「造某个子类」——子类实例化时 `cls` 是子类本身，不相等，故放行。

**练习 2**：`supported_tasks` 的值为什么用方法名字符串（如 `"chat"`）而不是直接引用方法对象（如 `chat`）？

**参考答案**：因为校验阶段（CLI 启动时）需要在不实例化 User、甚至不触发 Locust 依赖完整初始化的前提下，仅凭「类」就能查到「支持哪些任务」。用字符串可避免在类体定义时就引用尚未定义的方法对象，也方便序列化与跨模块比对。

---

### 4.2 采样入口 sample()

#### 4.2.1 概念说明

虚拟用户被 Locust 调起来后，每个 `@task` 方法第一件事就是「拿一个请求」。`BaseUser.sample()` 就是这个统一的「取请求」入口。它的核心职责只有一句：**从 `environment.sampler` 里，按 `environment.scenario` 取一个 `UserRequest`。**

为什么把 sampler 和 scenario 放在 environment 上而不是参数传进来？因为 Locust 的 task 循环由框架调度，方法签名受限；而 environment 是所有虚拟用户共享、且 runner 会动态更新 `scenario`（切换场景时）的唯一可靠「黑板」。`sample()` 只是把这块黑板上的两个对象接在一起。

#### 4.2.2 核心流程

```text
某 @task 方法（如 chat）被 Locust 调用
   │
   └─ self.sample()
         │
         ├─ 1. 校验 environment.scenario 存在且非空
         │      否则 raise AttributeError
         ├─ 2. 校验 environment.sampler 存在且非空
         │      否则 raise AttributeError
         └─ 3. return environment.sampler.sample(environment.scenario)
                  └─ 返回一个 UserRequest 子类实例（如 UserChatRequest）
                      （采样器内部细节见 u2-l4）
```

这里的关键交接是第 3 步：`sampler.sample(scenario)`。在 u2-l4 里你已经见过，`TextSampler.sample()` 会依据 `output_modality` 分发到 chat/embeddings/rerank 等分支，最终返回一个 `UserChatRequest`、`UserEmbeddingRequest` 等协议模型（见 u1-l5）。也就是说，`sample()` 这个方法本身**不做采样逻辑**，它只是个「转发器」——真正的采样逻辑在 Sampler 子类里。

#### 4.2.3 源码精读

[genai_bench/user/base_user.py:27-L44](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/base_user.py#L27-L44)：

```python
def sample(self) -> UserRequest:
    if not (
        hasattr(self.environment, "scenario")
        and self.environment.scenario is not None
    ):
        raise AttributeError(
            f"Environment {self.environment} has no attribute "
            f"'scenario' or it is empty."
        )
    if not (
        hasattr(self.environment, "sampler")
        and self.environment.sampler is not None
    ):
        raise AttributeError(
            f"Environment {self.environment} has no attribute "
            f"'sampler' or it is empty."
        )
    return self.environment.sampler.sample(self.environment.scenario)
```

值得拆开看三处细节：

1. **双重校验**：用 `hasattr(...) and ... is not None` 同时防范「属性不存在」与「属性存在但为空」两种情况。这是防御性编程——在子类或测试中，environment 可能只 mock 了其中一项。测试 [tests/user/test_base_user.py:57-L63](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/user/test_base_user.py#L57-L63) 正是把 `environment.sampler = None` 来触发这个 `AttributeError` 的。
2. **返回类型注解 `-> UserRequest`**：明确告知调用方「我给你的是一个请求模型」。结合 u1-l5，`UserRequest` 是协议模型的基类，实际拿到的是 `UserChatRequest` 等子类。
3. **转发，不实现**：方法体最后一行直接 `return environment.sampler.sample(...)`，把「按场景采样」的复杂度完全委托给了 sampler。这就是基类「薄」的地方——它定义契约，不实现业务。

**调用方长什么样？** 以 `OpenAIUser.chat` 为例，[genai_bench/user/openai_user.py:58-L62](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py#L58-L62)：

```python
@task
def chat(self):
    endpoint = "/v1/chat/completions"
    user_request = self.sample()
    ...
```

被 `@task` 标记的方法会被 Locust 反复调用；它第一行就 `self.sample()` 拿到一个请求，然后用 `isinstance` 校验类型（确保 sampler 没返回错的任务类型），再据此构造 HTTP payload。这一步的类型校验非常关键——它把「采样器输出」与「后端期望」做了运行时绑定。

#### 4.2.4 代码实践

**实践目标**：用真实 Locust `Environment`（非纯 mock）验证 `sample()` 能从 environment 取到正确的 `UserChatRequest`。

**操作步骤**：直接复刻测试 [tests/user/test_base_user.py:28-L55](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/user/test_base_user.py#L28-L55) 的做法。

```python
# 示例代码（改编自 tests/user/test_base_user.py）
from locust.env import Environment
from unittest.mock import MagicMock
from genai_bench.protocol import UserChatRequest
from genai_bench.user.base_user import BaseUser

class ConcreteUser(BaseUser):
    host = "http://example.com"

# 1. 建一个真实的 Locust environment（带 local runner）
env = Environment(user_classes=[ConcreteUser])
env.create_local_runner()

# 2. 把 sampler / scenario 挂上去（mock 掉采样逻辑，只关心 sample() 的转发）
env.scenario = MagicMock()
env.sampler = MagicMock()
env.sampler.sample = lambda x: UserChatRequest(
    model="gpt-3", prompt="Hello",
    num_prefill_tokens=1, max_tokens=5,
    additional_request_params={"temperature": 0.7},
)

# 3. 实例化一个 user 并调用 sample()
user = ConcreteUser(environment=env)
req = user.sample()
print(req.model, req.max_tokens, req.prompt)
```

**需要观察的现象**：

- 打印出 `gpt-3 5 Hello`，说明 `sample()` 正确转发了 sampler 的返回值。
- 把 `env.sampler = None` 后再调用 `user.sample()`，会抛 `AttributeError`，对应源码第 36-L43 行的分支。

**预期结果**：与 [tests/user/test_base_user.py:48-L55](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/user/test_base_user.py#L48-L55) 中 `test_sample` 的断言一致。

> 待本地验证：若 Locust 未随 genai-bench 正确安装，`Environment` 导入会失败。

#### 4.2.5 小练习与答案

**练习 1**：如果某个 `@task` 方法忘记调用 `isinstance(user_request, UserChatRequest)` 校验，最坏会发生什么？

**参考答案**：采样器可能因配置错误返回了别的请求类型（如 `UserEmbeddingRequest`），task 方法却仍按 chat 的字段（如 `prompt`、`max_tokens`）去取值，要么 `AttributeError`，要么拼出一个语义错误的 payload 发给服务器，导致这一轮压测数据失真。所以类型校验是 task 方法的「自保」手段。

**练习 2**：`sample()` 为什么要同时检查 `hasattr` 和 `is not None` 两个条件？

**参考答案**：`hasattr` 只能判断「属性是否存在」，但 genai-bench 在某些路径下（例如初始化未完成、或测试中显式置空）可能出现「属性存在但值为 `None`」的情况。两者都判，才能给出更准确的报错并避免后续 `None.sample()` 的隐式 `AttributeError`。

---

### 4.3 指标上报链路 collect_metrics

#### 4.3.1 概念说明

请求发出去、响应解析完，下一步是把「这次请求花了多久、产生了几个 token」上报出去。`BaseUser.collect_metrics(user_response, endpoint)` 就是这个统一的「报指标」出口。它做了两件**互补**的事：

1. **喂给 Locust 原生统计**：调用 `environment.events.request.fire(...)`，让 Locust 自己的 `stats` 表能算出 RPS、失败率等。这一路是为了兼容 Locust 生态（Web UI、`stats.get(...)` 等）。
2. **喂给 genai-bench 自己的聚合器**：调用 `environment.runner.send_message("request_metrics", ...)`，把结构化的 `RequestLevelMetrics`（含 TTFT/TPOT 等业务指标）送给 master runner，最终由 `AggregatedMetricsCollector` 聚合（见 u4-l2）。

为什么需要「两路」？因为 Locust 原生统计只认 `response_time` / `response_length` / `exception` 这几个泛化字段，表达不了「首 token 延迟」「每 token 耗时」这种 LLM 特有指标。所以 genai-bench 一边把能塞进 Locust 框架的塞进去（`e2e_latency` 当 `response_time`，`num_output_tokens` 当 `response_length`），一边把完整的业务指标用自定义消息单独送一条。

#### 4.3.2 核心流程

```text
子类的 send_request() 拿到 metrics_response（UserResponse）
   │
   └─ self.collect_metrics(user_response, endpoint)
         │
         ├─ collector = RequestMetricsCollector()
         │
         ├─ if status_code == 200：           ← 成功路径
         │     ├─ collector.calculate_metrics(user_response)
         │     │     （把三元时间戳 + token 数 → TTFT/TPOT/吞吐，细节见 u4-l1）
         │     └─ events.request.fire(
         │            response_time = e2e_latency,
         │            response_length = num_output_tokens)
         │
         ├─ else：                            ← 失败路径
         │     ├─ 在 collector.metrics 上记 error_code / error_message
         │     └─ events.request.fire(exception=...)
         │
         └─ runner.send_message(
                "request_metrics",
                collector.metrics.model_dump_json())   ← 两路都走这一步
                 │
                 └─ master 端 handler：
                      RequestLevelMetrics.model_validate_json(msg.data)
                      → metrics_collector.add_single_request_metrics(metrics)
```

其中关键的几个指标公式（在 [genai_bench/metrics/request_metrics_collector.py:46-L56](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/request_metrics_collector.py#L46-L56) 计算）：

\[
\text{ttft} = \text{time\_at\_first\_token} - \text{start\_time}
\]

\[
\text{e2e\_latency} = \text{end\_time} - \text{start\_time}
\]

\[
\text{input\_throughput} = \frac{\text{num\_input\_tokens}}{\text{ttft}}
\]

对聊天响应，还有输出侧指标（见 [request_metrics_collector.py:75-L95](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/request_metrics_collector.py#L75-L95)）。其中 `output_latency` 是「首 token 之后」的耗时，TPOT 之所以分母是 \( \text{num\_output\_tokens} - 1 \)，是因为第一个 token 的时间已经被算进 TTFT 了：

\[
\text{output\_latency} = \text{e2e\_latency} - \text{ttft}
\]

\[
\text{tpot} = \frac{\text{output\_latency}}{\text{num\_output\_tokens} - 1}
\]

\[
\text{output\_inference\_speed} = \frac{1}{\text{tpot}}, \qquad
\text{output\_throughput} = \frac{\text{num\_output\_tokens} - 1}{\text{output\_latency}}
\]

> 这些公式的来龙去脉与「非聊天任务为何要重置输出指标」是 u4-l1 的主题，本讲只需理解它们在链路中的位置：`collect_metrics` 调用 `calculate_metrics` 算出这些数，再打包发出。

#### 4.3.3 源码精读

[genai_bench/user/base_user.py:46-L92](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/base_user.py#L46-L92) 是本讲最长、也最关键的方法。我们分段读。

**第一步：创建一个一次性的 collector。**

```python
request_metrics_collector = RequestMetricsCollector()
```

每次请求都新建一个 `RequestMetricsCollector`（它内部持有一个空的 `RequestLevelMetrics`，见 [request_metrics_collector.py:22-L23](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/request_metrics_collector.py#L22-L23)）。这是「每请求一个采集器」的设计，避免并发请求互相污染指标。

**第二步：成功路径（status_code == 200）。** [base_user.py:60-L67](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/base_user.py#L60-L67)：

```python
if user_response.status_code == 200:
    request_metrics_collector.calculate_metrics(user_response)
    self.environment.events.request.fire(
        request_type="POST",
        name=endpoint,
        response_time=request_metrics_collector.metrics.e2e_latency,
        response_length=request_metrics_collector.metrics.num_output_tokens,
    )
```

注意两个「翻译」：

- `response_time` ← `e2e_latency`：把端到端延迟塞进 Locust 认识的字段。
- `response_length` ← `num_output_tokens`：把「输出 token 数」当作「响应长度」。这样 Locust 的 stats 就能算平均延迟、延迟分布，也能在 UI 上显示。

`name=endpoint`（如 `/v1/chat/completions`）让 Locust 按端点分组统计。

**第三步：失败路径。** [base_user.py:68-L87](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/base_user.py#L68-L87)：

```python
else:
    request_metrics_collector.metrics.error_code = user_response.status_code
    request_metrics_collector.metrics.error_message = user_response.error_message
    self.environment.events.request.fire(
        request_type="POST", name=endpoint,
        response_time=0, response_length=0,
        exception=f"Request failed with status {user_response.status_code}, ...",
    )
    logger.warning(...)
```

失败时不调用 `calculate_metrics`（因为没有有效的时间戳/token 数可算），而是把 `error_code` / `error_message` 记到 metrics 上，并以 `exception=...` 的形式上报给 Locust——这一句决定了 Locust 会把这次请求计入 `num_failures`（见测试 [test_base_user.py:105-L117](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/user/test_base_user.py#L105-L117) 的断言 `num_failures == 1`）。

**第四步：无论如何都把结构化指标发出去。** [base_user.py:90-L92](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/base_user.py#L90-L92)：

```python
self.environment.runner.send_message(
    "request_metrics", request_metrics_collector.metrics.model_dump_json()
)
```

这是「第二路出口」。无论成功失败，都把 `RequestLevelMetrics` 序列化成 JSON 字符串（`model_dump_json()` 是 Pydantic v2 的方法，见 u1-l5），通过 Locust 的消息机制发给 runner。成功时带着 TTFT/TPOT 等业务指标；失败时带着 `error_code`/`error_message`。

**消息怎么被接收？** 在 master 端，[genai_bench/distributed/runner.py:280-L307](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L280-L307) 注册了 `request_metrics` 的处理器：

```python
metrics = RequestLevelMetrics.model_validate_json(msg.data)   # L283
...
self.metrics_collector.add_single_request_metrics(metrics)    # L293
```

即：把 JSON 反序列化回 `RequestLevelMetrics`（校验失败则丢弃，L284-L288），再交给 `AggregatedMetricsCollector` 累加。本地模式下，注册发生在 [runner.py:423-L425](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L423-L425)；分布式 master 模式在 [runner.py:407-L414](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/distributed/runner.py#L407-L414)。

**子类如何串进这条链？** 以 `OpenAIUser` 为例，[genai_bench/user/openai_user.py:311-L377](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py#L311-L377) 的 `send_request` 在 `finally` 之后调用：

```python
self.collect_metrics(metrics_response, endpoint)   # openai_user.py:376
return metrics_response
```

也就是说，子类只管「发请求 + 解析成 `UserResponse`」，剩下的指标上报全交给基类的 `collect_metrics`。这就是基类把公共逻辑下沉的好处——任何后端 User 都自动获得一致的指标上报行为。

#### 4.3.4 代码实践

**实践目标**：构造一个 `UserChatResponse`，调用 `collect_metrics`，验证它同时更新了 Locust 的 stats 表，并发出了 `request_metrics` 消息。

**操作步骤**：复刻测试 [tests/user/test_base_user.py:65-L84](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/user/test_base_user.py#L65-L84)。

```python
# 示例代码（改编自 tests/user/test_base_user.py）
from locust.env import Environment
from unittest.mock import MagicMock
from genai_bench.protocol import UserChatResponse, UserChatRequest
from genai_bench.user.base_user import BaseUser

class ConcreteUser(BaseUser):
    host = "http://example.com"

env = Environment(user_classes=[ConcreteUser])
env.create_local_runner()
env.scenario = MagicMock()
env.sampler = MagicMock()
env.sampler.sample = lambda x: UserChatRequest(
    model="gpt-3", prompt="Hi", num_prefill_tokens=1, max_tokens=5,
    additional_request_params={"temperature": 0.7},
)

# 构造一个成功响应：start=0, first_token=2, end=3, prefill=1, output=2
resp = UserChatResponse(
    status_code=200, generated_text="random", tokens_received=2,
    time_at_first_token=2, num_prefill_tokens=1, start_time=0, end_time=3,
)

user = ConcreteUser(environment=env)
user.collect_metrics(resp, "/v1/chat/completions")

# 观察 Locust stats 是否被更新
stats = env.runner.stats.get("/v1/chat/completions", "POST")
print("num_requests =", stats.num_requests)
print("total_response_time =", stats.total_response_time)   # 期望 = e2e_latency = 3
print("total_content_length =", stats.total_content_length)  # 期望 = num_output_tokens = 2
```

**需要观察的现象**：

- `num_requests == 1`，`total_response_time == 3`（= `end_time - start_time`），`total_content_length == 2`（= `tokens_received`）。这正好印证了「成功路径」把 `e2e_latency` 当 `response_time`、把 `num_output_tokens` 当 `response_length` 的翻译。
- 换成一个 `status_code=500` 的 `UserResponse` 再调用一次，会看到 `stats.num_failures` 增加。

**预期结果**：与 [test_base_user.py:80-L84](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/user/test_base_user.py#L80-L84)（chat 成功）和 [test_base_user.py:105-L117](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/user/test_base_user.py#L105-L117)（失败）的断言一致。

> 关于 `request_metrics` 消息的「接收端」：本地模式下，`create_local_runner()` 起的 runner 既是发送方又是接收方，但默认并未注册 `request_metrics` handler（那是 `DistributedRunner._register_message_handlers` 干的，见 runner.py:423-L425）。所以本实践主要观察 Locust stats 这一路；想观察消息那一路，可阅读 u7-l1 的 runner 测试 [tests/distributed/test_runner.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/distributed/test_runner.py)。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `collect_metrics` 在成功和失败两种情况下，最后都要执行 `send_message("request_metrics", ...)`？

**参考答案**：因为聚合器（`AggregatedMetricsCollector`）需要看到**全部**请求，包括失败请求，才能算出正确的总请求数、失败率，并对错误做归类统计。失败路径虽然没有调用 `calculate_metrics`，但仍把带 `error_code`/`error_message` 的 metrics 发出去，让 master 能完整还原这次失败请求。

**练习 2**：如果把 `response_length=request_metrics_collector.metrics.num_output_tokens` 改成传 `0`，会对 Locust stats 造成什么影响？

**参考答案**：Locust stats 的 `total_content_length` 会一直是 0，无法在 Web UI 或统计表里看到「平均每请求产出多少 token」。对纯失败统计（`num_failures`）无影响，但会丢失「响应规模」这一维度的信息——这正是测试 [test_base_user.py:86-L103](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/user/test_base_user.py#L86-L103) 对 embeddings 断言 `total_content_length == 0` 的原因（embeddings 不产出解码 token，本就该是 0）。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**贯穿性任务**：画出一次请求从 `sample()` 到 `send_message("request_metrics", ...)` 的完整时序，并指出关键扩展点。

**任务步骤**：

1. **阅读** [genai_bench/user/base_user.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/base_user.py) 全文，再跳到 [genai_bench/user/openai_user.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py) 的 `chat`（L58）与 `send_request`（L311）。

2. **画时序图**（文字版即可），至少覆盖以下参与者与消息方向：

   ```text
   Locust Runner ──(调度 @task chat)──> OpenAIUser
   OpenAIUser ──self.sample()──> environment.sampler.sample(scenario)
   sampler ──UserChatRequest──> OpenAIUser
   OpenAIUser ──requests.post──> LLM 服务
   LLM 服务 ──streaming chunks──> OpenAIUser.parse_chat_response
   parse_chat_response ──UserChatResponse──> send_request
   send_request ──self.collect_metrics(resp, endpoint)──> BaseUser.collect_metrics
   BaseUser ──events.request.fire──> Locust stats   （第一路）
   BaseUser ──send_message("request_metrics", json)──> Master runner  （第二路）
   Master runner ──add_single_request_metrics──> AggregatedMetricsCollector
   ```

3. **标注扩展点**。在你画的图上，至少标出下面这些「子类可以替换或必须实现」的地方：

   | 扩展点 | 位置 | 谁来填 |
   | --- | --- | --- |
   | `supported_tasks` 字典 | base_user.py:13 | 子类覆写，声明任务→方法名映射 |
   | `@task` 方法（如 `chat`） | openai_user.py:58 | 子类实现，负责取请求、构造 payload、发请求、解析响应 |
   | 响应解析策略 `parse_*_response` | openai_user.py:379 等 | 子类实现，把 HTTP 响应变成 `UserResponse` |
   | `on_start` 认证初始化 | openai_user.py:47 | 子类实现，准备 `self.headers` |
   | `collect_metrics` | base_user.py:46 | 基类已实现，子类通常不覆写（直接复用） |

4. **动手验证**：把 4.2.4 与 4.3.4 两个实践合并——先 `sample()` 拿到一个 `UserChatRequest`，再手工构造一个对应的 `UserChatResponse`，调用 `collect_metrics`，确认 Locust stats 同时出现「1 个请求」和「正确的 response_time/response_length」。

**预期产出**：一张清晰的文字时序图 + 一份扩展点清单 + 一次成功跑通「采样→上报」的最小验证。如果跑通了，你已经掌握了 genai-bench User 子系统的「脊柱」，下一讲的流式解析就是在 `parse_chat_response` 这个扩展点上往里填内容。

## 6. 本讲小结

- `BaseUser` 继承 Locust 的 `HttpUser`，让 genai-bench 的虚拟用户天然具备 Locust 的并发调度能力，并额外提供 `supported_tasks`、`sample()`、`collect_metrics()` 三件套。
- `__new__` 守卫禁止直接实例化 `BaseUser`，强制使用子类；`supported_tasks` 用「任务字符串→方法名」的字典声明能力，`is_task_supported` 仅做成员判断。
- `sample()` 是「取请求」入口，本质是把 `environment.sampler.sample(environment.scenario)` 的结果转发出来——真正的采样逻辑在 Sampler 子类（u2-l4）。
- `collect_metrics()` 有两路互补出口：成功时把 `e2e_latency`/`num_output_tokens` 翻译成 Locust 的 `response_time`/`response_length` 喂给原生 stats；无论成败都把结构化 `RequestLevelMetrics` 通过 `send_message("request_metrics", ...)` 送给 master runner 聚合。
- 子类只需实现「取请求→发请求→解析成 `UserResponse`」三步，最后调一次基类的 `collect_metrics` 即可，指标上报行为对所有后端一致。
- `environment` 上挂载的 `sampler` 与 `scenario` 是连接「Locust 引擎」与「genai-bench 业务上下文」的桥梁，分别由主流程（cli.py:383）和分布式 runner（runner.py:333）注入。

## 7. 下一步学习建议

- 下一讲 [u3-l2 OpenAIUser 流式与非流式响应解析](u3-l2-openai-user-response-parsing.md) 会深入本讲提到的 `parse_chat_response` 扩展点，重点讲 SSE 流式 chunk 如何被解析出 TTFT、`tokens_received`、`reasoning_tokens`，以及 usage 缺失时如何用 tokenizer 回退估算。本讲已经把「链路的骨架」立起来了，u3-l2 是往骨架里填「解析细节」。
- 如果想横向对比不同后端如何复用/覆写这套基类，可以先跳到 [u3-l3 多后端 User 体系](u3-l3-multi-backend-users.md)，看看 `OCI/AWS Bedrock/Azure/GCP` 等 User 是怎么挂在 `BaseUser` 之下的。
- 对「指标怎么算」感兴趣的话，[u4-l1 单请求指标计算](u4-l1-request-metrics-collector.md) 会把本讲里点到为止的 TTFT/TPOT/吞吐公式逐一拆解；对「指标怎么聚合」感兴趣则看 u4-l2，它正是 `send_message("request_metrics", ...)` 的接收端。
- 想了解 master/worker 如何分发 `scenario` 与汇总 `request_metrics`，可提前翻阅 [u7-l1 DistributedRunner 主从架构](u7-l1-distributed-runner.md)，本讲中 `environment.scenario` 的更新（runner.py:333）就来自那里。
