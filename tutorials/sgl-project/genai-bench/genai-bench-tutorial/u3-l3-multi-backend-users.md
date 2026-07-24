# 多后端 User 体系

## 1. 本讲目标

学完本讲，你应当能够：

- 画出 genai-bench 的「后端 User 全景」：知道有哪些后端、各自继承自哪个基类、声明支持哪些任务。
- 解释 `API_BACKEND_USER_MAP` 这张注册表，并跟踪 `--api-backend` 如何一步步「选中」一个 `user_class` 并注入运行配置。
- 区分两种扩展策略：**继承复用**（如 `OCIOpenAIUser` 继承 `OpenAIUser`）与**从零实现**（如 `AWSBedrockUser` 直接继承 `BaseUser`）。
- 说清为什么 `vllm` / `sglang` 这两个后端会复用同一个 `OpenAIUser`，以及 `OpenAIUser` 内部如何用一个 `api_backend` 字段在三者间切换行为。

本讲是 u3-l1「User 基类与 Locust 集成」的延伸：u3-l1 讲清了**所有后端共有的三件套**（`supported_tasks` / `sample()` / `collect_metrics()`），本讲则聚焦于**各后端之间的差异与复用关系**。

## 2. 前置知识

阅读本讲前，请确认你已理解以下概念（均在 u3-l1、u3-l2 中建立）：

- **Locust 虚拟用户**：`BaseUser` 继承自 `locust.HttpUser`，Locust 会按并发档位实例化出 N 个用户对象来发请求。
- **三步契约**：每个后端 User 只需实现「`sample()` 取请求 → 发请求并解析为 `UserResponse` → `collect_metrics()` 上报指标」，其中 `sample()` 与 `collect_metrics()` 由 `BaseUser` 统一提供。
- **`ctx.obj` 数据口袋**：click 的回调式校验函数把「选中的 user 类」「选中的任务方法」存进 `ctx.obj`，供后续 benchmark 函数体取用（见 u1-l4）。
- **任务字符串 `<input>-to-<output>`**：如 `text-to-text`、`text-to-embeddings`，决定采样器与请求类型（见 u2-l1）。

此外补充一个本讲会用到的关键事实：**`user_class` 的 `host` / `auth_provider` / `api_backend` 是「类属性」**。benchmark 主流程在启动 Locust 之前，把它们直接挂在类对象上；Locust 随后实例化的每一个用户对象，都能通过 `self.host` 等读到这些值。这是各后端能共享同一套基类机制的前提。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `genai_bench/user/base_user.py` | 所有后端的共同基类 `BaseUser`，提供 `sample()` 与 `collect_metrics()` |
| `genai_bench/user/openai_user.py` | `OpenAIUser`，OpenAI 兼容协议的实现，也是 vllm/sglang 复用的同一个类 |
| `genai_bench/user/oci_openai_user.py` | `OCIOpenAIUser`，**继承复用** `OpenAIUser` 的典型案例 |
| `genai_bench/user/aws_bedrock_user.py` | `AWSBedrockUser`，**从零实现**的典型案例（boto3 + 多模型族适配） |
| `genai_bench/cli/validation.py` | `API_BACKEND_USER_MAP` 注册表与 `validate_api_backend` 选择链路 |
| `genai_bench/cli/cli.py` | benchmark 函数体，把 `auth_provider`/`host`/`api_backend` 注入 `user_class` |

其余后端（`AzureOpenAIUser`、`GCPVertexUser`、`TogetherUser`、`CohereUser`、`OCICohereUser`、`OCICohereV2User`、`OCIGenAIUser`）结构相似，本讲择要对比，不做逐行精读。

## 4. 核心概念与源码讲解

### 4.1 后端 User 全景

#### 4.1.1 概念说明

genai-bench 要对**不同厂商的 LLM 服务**做基准测试：OpenAI、AWS Bedrock、Azure OpenAI、GCP Vertex AI、Together、Cohere、OCI Generative AI……这些服务的认证方式、SDK、请求/响应格式各不相同。

项目用一个简单的面向对象设计来应对这种差异：**每个后端对应一个 `User` 子类**，子类负责「怎么发请求、怎么解析响应」，而所有子类共享 `BaseUser` 提供的「怎么取请求、怎么报指标」。

每个后端类只需声明两样东西：

- `BACKEND_NAME`：类级字符串常量，是这个后端的「身份证号」，也是 `--api-backend` 的合法取值。
- `supported_tasks`：一个字典，键是任务字符串（如 `text-to-text`），值是实现该任务的方法名（如 `"chat"`）。它同时承担两个职责——声明「这个后端能做哪些任务」，并作为「任务 → 方法」的路由表。

#### 4.1.2 核心流程

一个后端类从「被定义」到「被 Locust 实例化并发请求」，经历以下流程：

```
定义子类  ──►  声明 BACKEND_NAME + supported_tasks
                    │
                    ▼
注册表 API_BACKEND_USER_MAP = { BACKEND_NAME: 子类 }
                    │
        --api-backend aws-bedrock
                    │
                    ▼
validate_api_backend 查表 ──► ctx.obj["user_class"] = AWSBedrockUser
                    │
                    ▼
benchmark 函数体注入类属性 ──► user_class.host / auth_provider / api_backend
                    │
                    ▼
Locust 实例化 N 个 AWSBedrockUser 对象
                    │
                    ▼
每个对象 on_start() 读取类属性 ──► 构建后端专用 client / headers
                    │
                    ▼
Locust 调度 @task 方法 ──► sample() → 发请求/解析 → collect_metrics()
```

#### 4.1.3 源码精读

先看共同基类 `BaseUser` 的两个声明点：

[genai_bench/user/base_user.py:12-25](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/base_user.py#L12-L25) —— 这段定义了 `BaseUser` 继承 `HttpUser`，声明了空的 `supported_tasks` 字典，并用 `__new__` 禁止直接实例化基类。`is_task_supported` 只做一件事：判断任务是否在 `supported_tasks` 字典里。

注意 `supported_tasks` 是**类属性**，所以 `is_task_supported` 可以作为类方法调用（`validate_task` 里正是 `user_class.is_task_supported(task)` 这样用的）。

再看 `OpenAIUser` 的声明，它是后续两个复用模式的原型：

[genai_bench/user/openai_user.py:32-41](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py#L32-L41) —— `BACKEND_NAME = "openai"`，`supported_tasks` 把 6 个任务分别映射到方法名：`text-to-text → chat`、`text-to-embeddings → embeddings`、`text-to-rerank → rerank`、`text-to-image → images_generations`、`text-to-speech → speech`，而 `image-text-to-text` 也复用 `chat`（多模态在 `chat` 内部用 `isinstance` 分流）。

下面这张表是全部 11 个后端（含 vllm/sglang 两个别名）的全景：

| 后端类 | `BACKEND_NAME` | 直接基类 | `supported_tasks` 关键项 | 底层客户端 |
|---|---|---|---|---|
| `OpenAIUser` | `openai` | `BaseUser` | chat/embeddings/rerank/images/speech | `requests` |
| `AWSBedrockUser` | `aws-bedrock` | `BaseUser` | chat/embeddings（多模态复用 chat） | `boto3` |
| `AzureOpenAIUser` | `azure-openai` | `BaseUser` | chat/embeddings（多模态复用 chat） | `requests` |
| `GCPVertexUser` | `gcp-vertex` | `BaseUser` | chat/embeddings（多模态复用 chat） | `requests` |
| `TogetherUser` | `together` | `BaseUser` | chat/embeddings | `requests` |
| `CohereUser` | `cohere` | `BaseUser` | chat/embeddings | `requests` |
| `OCICohereUser` | `oci-cohere` | `BaseUser` | chat/rerank/embeddings | OCI SDK |
| `OCIGenAIUser` | `oci-genai` | `BaseUser` | chat | OCI SDK |
| `OCIOpenAIUser` | `oci-openai` | **`OpenAIUser`** | images/speech（chat 等继承自父类） | `openai` SDK + `httpx` |
| `OCICohereV2User` | `oci-cohere-v2` | **`OCICohereUser`** | chat（含多模态） | OCI SDK |
| ——（别名） | `vllm` / `sglang` | 映射到 `OpenAIUser` | 同 `OpenAIUser` | `requests` |

读这张表能得到三个结论：

1. **绝大多数后端直接继承 `BaseUser`**，从零写自己的「发请求 + 解析」逻辑。
2. **少数后端走「中间基类」复用**：`OCIOpenAIUser → OpenAIUser`、`OCICohereV2User → OCICohereUser`。它们只覆写与父类不同的部分。
3. **`vllm` 与 `sglang` 不是新类**，而是注册表里指向 `OpenAIUser` 的别名（详见 4.2）。

每个子类的 `BACKEND_NAME` 就是它的「身份证」，例如：

[genai_bench/user/aws_bedrock_user.py:22-32](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/aws_bedrock_user.py#L22-L32) —— `AWSBedrockUser` 直接继承 `BaseUser`，`BACKEND_NAME = "aws-bedrock"`，声明支持 `text-to-text`、`text-to-embeddings`、`image-text-to-text`（后两者中多模态复用 `chat` 方法）。

#### 4.1.4 代码实践

**实践目标**：亲手把「后端全景表」从源码里挖出来，验证它不是凭空写的。

**操作步骤**：

1. 打开 `genai_bench/user/` 目录，对每个文件定位 `class XxxUser(...)` 行与紧随其后的 `BACKEND_NAME =` 和 `supported_tasks =`。
2. 用一张纸（或文本）画出继承树：根是 `BaseUser`，第二层是直接继承它的类，第三层是继承中间类的类。
3. 核对：你应该得到「9 个直接继承 `BaseUser` + `OCIOpenAIUser` 继承 `OpenAIUser` + `OCICohereV2User` 继承 `OCICohereUser`」的结构。

**需要观察的现象**：`BACKEND_NAME` 与类名之间存在稳定的命名规律（小写、连字符），这正是 `--api-backend` 取值的来源。

**预期结果**：你画出的继承树与本讲 4.1.3 的全景表一致。

**待本地验证**：若仓库新增了后端（如 `XxxUser`），你的表需要同步更新——这正是该表「可维护性」的来源：它完全由源码驱动。

#### 4.1.5 小练习与答案

**练习 1**：`OCICohereV2User` 的注释说「v2 API is required for command-A vision or command-A reasoning」。结合它的继承关系，说说它为什么选择继承 `OCICohereUser` 而不是 `BaseUser`。

**参考答案**：v2 与 v1 共享同一套 OCI 认证与 `GenerativeAiInferenceClient` 客户端构建逻辑（`on_start`、`send_request`），只在「构造 chat 请求体」这一步不同（v2 用 `CohereChatRequestV2`，支持多模态与 thinking）。继承 `OCICohereUser` 能复用认证、客户端、发送与指标上报，只覆写请求体构造，避免重复代码。

**练习 2**：`supported_tasks` 是实例属性还是类属性？为什么这个设计很重要？

**参考答案**：它是类属性（定义在 `class` 体顶层）。正因为是类属性，`validate_task` 才能在**尚未实例化任何用户对象**时，用 `user_class.is_task_supported(task)` 做任务兼容性校验——这是「先校验、后实例化」的关键。

---

### 4.2 选择映射 API_BACKEND_USER_MAP

#### 4.2.1 概念说明

有了 11 个后端类，还需要一个「**入口 → 类**」的映射，让 `--api-backend openai` 这样的命令能找到 `OpenAIUser`。这个映射就是 `API_BACKEND_USER_MAP`——一张 `{后端名: User 类}` 的字典。

它配合三个校验回调，构成一条「选择链路」：

- `validate_api_backend`：查表选类，存入 `ctx.obj["user_class"]`。
- `validate_api_key`：按后端分类，决定是否需要 API key。
- `validate_task`：用选中的类校验任务是否被支持，并取出对应的任务方法。

理解这条链路，就理解了「一次 `genai-bench benchmark` 命令是如何确定要用哪个后端、哪个方法发请求的」。

#### 4.2.2 核心流程

```
--api-backend <X>
        │
        ▼
validate_api_backend(X)
   X.lower() ──► API_BACKEND_USER_MAP[X] ──► ctx.obj["user_class"]
        │  （查不到则 BadParameter）
        ▼
--api-key <K>
        │
        ▼
validate_api_key(K)
   按 api_backend 分三类：必填 / 不用 / Azure 特殊
        │
        ▼
--task <T>
        │
        ▼
validate_task(T)
   user_class.is_task_supported(T) ?
        │  否 ──► BadParameter（列出支持的任务）
        │  是
        ▼
   ctx.obj["user_task"] = getattr(user_class, supported_tasks[T])
        │
        ▼
benchmark 函数体
   user_class.auth_provider / host / api_backend = ...
   Locust 用 user_class 实例化虚拟用户
```

#### 4.2.3 源码精读

**注册表本体**——注意最后两行，vllm 与 sglang 是「别名」：

[genai_bench/cli/validation.py:25-38](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L25-L38) —— 前 10 项是「`BACKEND_NAME → 类`」的正常映射；`"vllm": OpenAIUser` 和 `"sglang": OpenAIUser` 则把两个推理引擎别名直接指向 `OpenAIUser`。**为什么能这么做**：vLLM 和 SGLang 都是 LLM 服务系统，它们对外暴露了与 OpenAI 完全兼容的 REST 接口（`/v1/chat/completions` 等），所以同一套「构造 payload + 解析 SSE」的代码可以直接复用。

**选中类**：

[genai_bench/cli/validation.py:257-270](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L257-L270) —— `validate_api_backend` 把输入小写化后查表，查不到就抛 `BadParameter`；查到就把类存进 `ctx.obj["user_class"]`。注意它返回的是小写后的 `api_backend` 字符串（供后续 `validate_api_key` / `validate_task` 使用），而「类」本身走 `ctx.obj` 传递。

**按后端分类的 API key 校验**——这张三分类表是理解「不同后端认证差异」的钥匙：

[genai_bench/cli/validation.py:273-310](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L273-L310) —— 后端被分成三组：

- `api_key_required`（L281）：`openai`、`vllm`、`sglang`——传统 API key 认证，必填。
- `no_api_key`（L284-292）：OCI / Cohere / Bedrock / GCP——用云厂商专属认证，传了 API key 反而会警告并忽略。
- `azure-openai`（L298）：介于两者之间，既可用 API key 也可用 Azure AD。

**用选中的类校验任务，并绑定方法**：

[genai_bench/cli/validation.py:313-352](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L313-L352) —— 这段是「任务路由」的核心：

- L320-325：要求 `--api-backend` 必须先于 `--task` 出现（因为要先有 `user_class` 才能校验任务）。这正是 u2-l1 提到的「`--api-backend` 先于 `--task`」约束的来源。
- L328-333：调用类方法 `user_class.is_task_supported(task)`，不支持就报错并列出该后端支持的全部任务。
- L338-347：`backend_task_restrictions` 处理「同一类后端内部的任务限制」——`text-to-rerank` 需要 `/rerank` 端点，只有 `vllm`/`sglang` 暴露了它，标准 OpenAI 没有。
- L350：`getattr(user_class, user_class.supported_tasks[task])` 把「任务字符串」翻译成「类上的方法对象」，存入 `ctx.obj["user_task"]`。

**注入类属性**——这是「选中类之后、实例化之前」的关键一步：

[genai_bench/cli/cli.py:278-284](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L278-L284) —— benchmark 函数体从 `ctx.obj` 取出选中的 `user_class`，然后把 `auth_provider`、`host`、`api_backend` 三个值**作为类属性**挂上去。因为 Locust 稍后会用同一个类对象实例化所有虚拟用户，这些类属性就自然地「广播」给了每个实例，各实例的 `on_start()` 再用它们构建后端专用客户端。

#### 4.2.4 代码实践

**实践目标**：跟踪一次「`--api-backend` 选中类」的完整调用，确认 `ctx.obj` 在三个回调间传递的时序。

**操作步骤**：

1. 在 `validate_api_backend` 的 `ctx.obj["user_class"] = user_class` 一行 mental-trace：此时 `ctx.obj` 里多了一个键。
2. 跳到 `validate_api_key`，注意它**没有**读 `ctx.obj["user_class"]`，而是读 `ctx.params.get("api_backend")`——即上一个回调的**返回值**。这说明 click 把回调返回值存入 `ctx.params`，把 `ctx.obj` 留作「跨回调自定义口袋」。
3. 再跳到 `validate_task` 的 L320：它读 `ctx.obj.get("user_class")`，确认类已经就位。

**需要观察的现象**：三个回调分别用 `ctx.params`（click 自动填充的参数值）与 `ctx.obj`（自定义口袋）两种渠道传递信息。

**预期结果**：你能复述「`api_backend` 字符串走 `ctx.params`，`user_class` 对象走 `ctx.obj`」这条分工。

#### 4.2.5 小练习与答案

**练习 1**：如果用户执行 `--task text-to-rerank --api-backend openai`（注意顺序写反了），会触发哪个错误？

**参考答案**：会先在 `validate_task` 的 L320-325 报「API backend is not set」（因为 `--task` 先出现时 `ctx.obj["user_class"]` 还没被设置）。click 按命令行顺序处理参数，所以即便后面有 `--api-backend`，到 `--task` 时它还没被处理。

**练习 2**：为什么 `vllm` 和 `sglang` 不需要单独写一个 `VllmUser` 类？

**参考答案**：因为它们的服务端实现了 OpenAI 兼容协议，请求构造与 SSE 响应解析与 `OpenAIUser` 完全相同，直接在注册表里把这两个名字映射到 `OpenAIUser` 即可，零额外代码。唯一的差异（`ignore_eos`）通过 `self.api_backend` 字段在 `OpenAIUser.chat` 内部区分（见 4.3）。

---

### 4.3 后端差异与复用

#### 4.3.1 概念说明

虽然所有后端都遵守「`sample()` → 发请求/解析 → `collect_metrics()`」三步契约，但它们在「怎么发、怎么解析」上差异巨大。项目里存在两种典型的扩展策略：

- **继承复用**：新后端大部分行为与某个已有后端一致，只覆写不同之处。代表是 `OCIOpenAIUser`（继承 `OpenAIUser`）。
- **从零实现**：新后端的协议与任何已有后端都不兼容，只能直接继承 `BaseUser`，自己写发送与解析。代表是 `AWSBedrockUser`。

无论哪种策略，**复用的下限都是 `BaseUser` 的 `sample()` 与 `collect_metrics()`**——这两个方法对所有后端一视同仁。

#### 4.3.2 核心流程

两种策略的复用边界对比：

```
继承复用（OCIOpenAIUser → OpenAIUser）
  复用：chat / embeddings / rerank / send_request / parse_chat_response ...
  覆写：on_start（构建 OCI 认证的 openai 客户端）
        images_generations、speech（OCI 用官方 SDK + CompartmentId）

从零实现（AWSBedrockUser → BaseUser）
  复用：sample()、collect_metrics()、is_task_supported()
  自写：on_start（boto3 客户端）、chat、embeddings
        _prepare_request_body（claude/titan/llama 多模型族适配）
        _extract_chunk_text / _extract_response_text（多模型族解析）
```

#### 4.3.3 源码精读

**案例一：继承复用——`OCIOpenAIUser`**

[genai_bench/user/oci_openai_user.py:35-42](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/oci_openai_user.py#L35-L42) —— 它继承 `OpenAIUser`，但 `supported_tasks` **只声明了** `text-to-image` 和 `text-to-speech`。这意味着对 `oci-openai` 后端，`is_task_supported` 只认这两个任务——即便父类 `OpenAIUser` 本身支持 chat 等，子类的 `supported_tasks` 把它**收窄**了。

它的 `on_start` 展示了「OCI 用专属认证包装 OpenAI 兼容客户端」的套路：

[genai_bench/user/oci_openai_user.py:44-71](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/oci_openai_user.py#L44-L71) —— 关键细节：

- L27-32 的 `OCI_AUTH_CLASS_MAP` 把 4 种 OCI 认证类型映射到 `oci_openai` 库的认证类。
- L65-69 用官方 `openai.OpenAI` 客户端 + `httpx.Client(auth=oci_auth)`，让标准 OpenAI SDK 带上 OCI 签名——这是「适配器」思路。
- L71 `super(OpenAIUser, self).on_start()` 是**跳过父类** `OpenAIUser.on_start`、直接调用 `BaseUser.on_start`。原因是 `OCIOpenAIUser` 不用 `self.headers`（它用 SDK 客户端），所以不能执行父类里「从 `auth_provider.get_headers()` 构建 headers」的逻辑。

它只覆写 `images_generations` 与 `speech`，因为这两类 OCI 接口需要走官方 SDK 并附带 `CompartmentId` 头；而 chat 等任务**完全继承** `OpenAIUser` 的实现：

[genai_bench/user/oci_openai_user.py:73-136](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/oci_openai_user.py#L73-L136) —— 注意它**没有**重新实现 `send_request`，而是直接 `self.openai_client.images.generate(...)` 后手工调用基类的 `self.collect_metrics(...)`（L135）。这正是「三步契约」的灵活之处：只要最后调用 `collect_metrics` 上报一个 `UserResponse`，中间用 SDK 还是 `requests` 都无所谓。

**案例二：从零实现——`AWSBedrockUser`**

[genai_bench/user/aws_bedrock_user.py:38-76](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/aws_bedrock_user.py#L38-L76) —— `on_start` 用 `auth_provider.get_credentials()` 取出 AWS 凭据，构建 `boto3.Session` 与 `bedrock-runtime` 客户端。注意 L43-49 的**惰性导入**：`import boto3` 写在方法内部，这样不安装 `boto3` 的用户也能用其他后端（boto3 属于 `[aws]` 可选依赖）。

它的 `chat` 完全自写，不复用 `OpenAIUser` 的任何解析代码：

[genai_bench/user/aws_bedrock_user.py:78-174](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/aws_bedrock_user.py#L78-L174) —— 它要同时处理流式（`invoke_model_with_response_stream`）与非流式（`invoke_model`），并在流式里手工记录 `first_token_time`（TTFT 来源），最后构造 `UserChatResponse` 并调 `self.collect_metrics(...)`（L166）。注意 L157 用 `len(response_text.split())` **近似** token 数——这是 Bedrock 不总是返回 usage 时的妥协（与 u3-l2 讲的 OpenAI「tokenizer 回退估算」是同类问题）。

更复杂的是 Bedrock 要按**模型族**准备不同的请求体：

[genai_bench/user/aws_bedrock_user.py:240-343](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/aws_bedrock_user.py#L240-L343) —— `_prepare_request_body` 根据 `model_id` 里是否含 `claude` / `titan` / `llama`，构造完全不同的 JSON 结构（Anthropic 版本号、`inputText`、`prompt` 等字段各异）。对应的 `_extract_chunk_text`（L357-379）与 `_extract_response_text`（L398-425）也按模型族从不同字段取文本。这种「一个后端内多模型族适配」的复杂度，正是它选择从零实现、而非复用 `OpenAIUser` 的原因。

**对比：`OpenAIUser` 如何用一个类服务三个后端**

最后回到一个微妙点——`OpenAIUser` 同时是 `openai`、`vllm`、`sglang` 三个后端的实现类，它靠注入的 `api_backend` 字段在三者间切换：

[genai_bench/user/openai_user.py:47-56](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py#L47-L56) —— `on_start` 用 `getattr(self, "api_backend", self.BACKEND_NAME)` 兜底：如果 cli.py 没注入 `api_backend`（比如直接用 `openai` 后端），就回落到类自己的 `BACKEND_NAME`。

[genai_bench/user/openai_user.py:110-115](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/openai_user.py#L110-L115) —— `chat` 里这一段是「同一类、不同后端行为」的分叉：vllm/sglang 会带上 `ignore_eos`（强制生成到 `max_tokens` 以测吞吐，见 u2-l4），而标准 OpenAI 不支持该参数则删掉。**这就是 `api_backend` 必须作为类属性注入的根本原因**——让一个类根据「我此刻代表哪个后端」做细微行为切换。

#### 4.3.4 代码实践

**实践目标**：对比两种策略的复用边界，量化「复用了多少、自写了多少」。

**操作步骤**：

1. 打开 `oci_openai_user.py`，数一数它定义了多少个方法（`on_start`、`images_generations`、`speech`），再对照 `openai_user.py` 里 `OpenAIUser` 的方法（`chat`、`embeddings`、`rerank`、`images_generations`、`speech`、`send_request`、`parse_*`）。
2. 回答：`OCIOpenAIUser` 调用 `text-to-image` 任务时，走的是自己的 `images_generations`；那如果（假设）允许它跑 `text-to-text`，会走哪个方法？
3. 打开 `aws_bedrock_user.py`，确认它**没有**从 `OpenAIUser` 继承任何发送/解析方法，唯一复用的是 `BaseUser` 的 `sample()` 与 `collect_metrics()`。

**需要观察的现象**：`OCIOpenAIUser` 的方法数远少于 `OpenAIUser`（因为大量复用），而 `AWSBedrockUser` 的方法数与 `OpenAIUser` 相当（因为从零写）。

**预期结果**：步骤 2 的答案是「走继承来的 `OpenAIUser.chat`」——这就是继承复用的威力；但注意 `supported_tasks` 没声明 `text-to-text`，所以 `validate_task` 会拦下它，实际跑不通（除非扩展 `supported_tasks`）。

#### 4.3.5 小练习与答案

**练习 1**：`OCIOpenAIUser` 的 `images_generations` 里最后调用了 `self.collect_metrics(...)`（L135），但它没有继承 `OpenAIUser.send_request`。这说明 `collect_metrics` 来自哪里？为什么能直接用？

**参考答案**：`collect_metrics` 来自 `BaseUser`（经 `OpenAIUser` 间接继承）。它是所有后端的统一「报指标出口」，与具体后端的发送方式无关，所以无论用 `requests`、SDK 还是 boto3，最后都能调用它把 `UserResponse` 翻译成指标。

**练习 2**：`AWSBedrockUser` 为什么把 `import boto3` 写在 `on_start` 内部，而不是文件顶部？

**参考答案**：为了支持「按需安装」。boto3 属于 `[aws]` 可选依赖，写在方法内（惰性导入）意味着不装 boto3 的用户依然能 import `aws_bedrock_user` 模块、用其他后端；只有真正选 `aws-bedrock` 时才要求 boto3 已安装，否则抛出带安装提示的 `ImportError`。

**练习 3**：若要新增一个「OpenAI 兼容但需要自定义鉴权头」的后端，你会选继承复用还是从零实现？为什么？

**参考答案**：选继承复用（继承 `OpenAIUser`）。因为请求/响应解析完全相同，只需覆写 `on_start` 构造自定义 headers，并在 `supported_tasks` 里声明要开放的任务即可，最大化复用父类的 `chat`/`send_request`/`parse_chat_response`。

---

## 5. 综合实践

**任务**：本讲的 spec 实践——阅读 `validation.py` 的 `API_BACKEND_USER_MAP`，选择一个非 OpenAI 后端（推荐 `aws-bedrock`），系统说明它**复用了哪些基类方法、自写/覆写了哪些**。

**操作步骤**：

1. **定位注册表**：读 [validation.py:25-38](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L25-L38)，确认 `aws-bedrock` 映射到 `AWSBedrockUser`。
2. **查继承链**：打开 [aws_bedrock_user.py:22](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/aws_bedrock_user.py#L22)，确认它直接继承 `BaseUser`（不是 `OpenAIUser`）。
3. **列复用项**：通读全文件，找出所有 `self.sample(...)` 与 `self.collect_metrics(...)` 调用点，它们就是复用自 `BaseUser` 的方法。
4. **列自写项**：列出 `AWSBedrockUser` 自己定义的方法——`on_start`、`chat`、`embeddings`、`_prepare_request_body`、`_supports_streaming`、`_extract_chunk_text`、`_extract_usage_reasoning`、`_extract_response_text`。
5. **写一份对照说明**：用表格写清「方法名 | 来源（BaseUser 复用 / 自写） | 作用」。

**预期结果**：你的对照表应显示——复用项只有 `sample()` 与 `collect_metrics()`（及类方法 `is_task_supported`），其余全部自写；这印证了 `AWSBedrockUser` 属于「从零实现」策略。把它与 `OCIOpenAIUser`（复用项远多）的同类表格并排，你就能直观看出两种策略的复用密度差异。

**待本地验证**：若你本地装了 boto3 与 AWS 凭据，可进一步用 `--api-backend aws-bedrock --task text-to-text` 跑一次最小基准，观察 `on_start` 日志里打印的 region 信息。

## 6. 本讲小结

- genai-bench 用「每个后端一个 `User` 子类」应对多厂商差异；每个子类只需声明 `BACKEND_NAME`（身份证）与 `supported_tasks`（任务→方法路由表）。
- `API_BACKEND_USER_MAP` 是 `{后端名: 类}` 注册表；`validate_api_backend` 查表把选中类存入 `ctx.obj`，`validate_task` 再用该类校验任务并绑定方法对象。
- benchmark 函数体把 `auth_provider`/`host`/`api_backend` 作为**类属性**注入 `user_class`，让 Locust 实例化的每个虚拟用户都能在 `on_start` 里构建后端专用客户端。
- 存在两种扩展策略：**继承复用**（`OCIOpenAIUser → OpenAIUser`，只覆写差异部分）与**从零实现**（`AWSBedrockUser → BaseUser`，自写全部发送/解析）。
- 所有后端的复用下限是 `BaseUser` 的 `sample()` 与 `collect_metrics()`——这是统一的三步契约。
- `vllm`/`sglang` 是注册表里指向 `OpenAIUser` 的别名；`OpenAIUser` 靠注入的 `api_backend` 字段在三个后端间做细微行为切换（如 `ignore_eos`）。

## 7. 下一步学习建议

- **向认证侧深入**：本讲反复出现的 `auth_provider` 来自 U5（多云认证与存储）。建议接着读 u5-l1「认证体系总览与 UnifiedAuthFactory」，看清 `auth_provider.get_headers()` / `get_config()` / `get_credentials()` 在不同后端里分别返回什么。
- **向指标侧深入**：本讲所有后端最后都调用 `collect_metrics`，它把 `UserResponse` 交给 `RequestMetricsCollector`。建议读 u4-l1「单请求指标计算」，理解 TTFT/TPOT/吞吐的公式如何从 `UserResponse` 的时间戳与 token 数算出。
- **尝试扩展**：如果你计划新增一个后端，可直接跳到 u8-l3「扩展指南：添加新后端 / 任务 / 场景」，那里给出「继承哪个基类 + 注册到 `API_BACKEND_USER_MAP`」的完整步骤。
