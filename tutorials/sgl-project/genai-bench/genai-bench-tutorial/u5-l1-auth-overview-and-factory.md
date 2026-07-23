# 认证体系总览与 UnifiedAuthFactory

## 1. 本讲目标

本讲是 U5「多云认证与存储」单元的第一篇，从零建立对 genai-bench 认证体系的整体认知。学完本讲，你应当能够：

- 区分 **模型认证**（ModelAuthProvider）与 **存储认证**（StorageAuthProvider）这两套 provider 接口的职责与抽象方法。
- 看懂统一工厂 `UnifiedAuthFactory` 的两个静态方法 `create_model_auth` / `create_storage_auth`，以及它们如何按 provider 字符串分发到具体实现类。
- 识别项目中的**适配器模式（adapter pattern）**：为什么 `OpenAI` / `Together` / `OCI` 要多包一层 adapter，而 `aws-bedrock` / `azure-openai` / `gcp-vertex` 却直接实现接口。

> 本讲只讲「认证对象的构造入口与抽象分层」，具体每个云厂商的凭据细节（OCI session、Azure AD、GCP credentials 等）留给 u5-l2、u5-l4，存储对象如何被 `StorageFactory` 真正用于上传留给 u5-l3。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

### 2.1 为什么需要两套认证

genai-bench 是一个基准测试工具，一次实验里它要做**两类完全不同的远程调用**：

1. **调模型服务**：向 LLM 推理后端（OpenAI、OCI Generative AI、AWS Bedrock、Azure OpenAI、GCP Vertex、Together、vLLM、SGLang……）发推理请求。这类调用关注的是「带什么 HTTP 头」。
2. **读写对象存储**：把实验结果（JSON、Excel、PNG）上传到对象存储桶（OCI Object Storage、AWS S3、Azure Blob、GCP Cloud Storage、甚至 GitHub 仓库）。这类调用关注的是「给存储客户端什么配置/凭据」。

这两类调用**认证方式完全不同**：模型服务大多用 `Authorization: Bearer <key>` 这种 HTTP 头，而对象存储用的是 SDK 客户端的签名器（signer）、连接字符串、账号密钥等。所以 genai-bench 把它们抽象成两套接口，互不污染。

### 2.2 什么是 provider（提供者）与 factory（工厂）

- **provider（提供者）**：一个「能提供认证能力」的对象。每种云厂商对应一个 provider 类，比如 `OpenAIModelAuthAdapter`、`AWSBedrockAuth`。
- **factory（工厂）**：一个「根据输入字符串造出对应 provider」的中转站。你只要告诉它 `"openai"` 或 `"aws-bedrock"`，它就返回正确的对象，**调用方不需要知道具体类名**。

这样设计的好处是：业务代码（CLI）只跟工厂打交道，新增一个云厂商时，业务代码几乎不用改，只要在工厂里加一个 `elif` 分支。

### 2.3 什么是适配器模式（adapter pattern）

项目中**有些认证类是早就写好的**（早于统一接口存在），它们继承的是一个简陋的旧基类 `AuthProvider`。后来项目想统一接口，但又不忍心把这些老类推倒重写，于是写了「适配器」：**适配器实现新接口，但内部把活儿委托给老对象**。

打个比方：老类是英标插头，新接口是国标插座，适配器就是一个转接头——它本身是国标形状（满足新接口），但内部把电流（调用）转给英标插头（老对象）。

承接 u1-l4：你已经知道 `cli` group 下的 `benchmark` 命令是主流程入口。本讲就来看 benchmark 在发请求前、上传结果前，是如何通过 `UnifiedAuthFactory` 拿到认证对象的。承接 u3-l3：那里反复提到的 `auth_provider`（被注入到各 User 子类作为类属性）正是本讲工厂的产物。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到哪部分 |
| --- | --- | --- |
| [genai_bench/auth/model_auth_provider.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/model_auth_provider.py) | **模型认证**抽象基类 `ModelAuthProvider` | 全文，三个抽象方法 + 一个默认实现 |
| [genai_bench/auth/storage_auth_provider.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/storage_auth_provider.py) | **存储认证**抽象基类 `StorageAuthProvider` | 全文，三个抽象方法 + 一个默认实现 |
| [genai_bench/auth/unified_factory.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/unified_factory.py) | **统一工厂** `UnifiedAuthFactory`，两个 create 方法 | 全文 |
| [genai_bench/auth/auth_provider.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/auth_provider.py) | **旧基类** `AuthProvider`，被适配的老类继承它 | 全文，理解为何要 adapter |
| [genai_bench/auth/openai/model_auth_adapter.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/openai/model_auth_adapter.py) | OpenAI 适配器（典型 adapter 样例） | 全文 |
| [genai_bench/auth/oci/model_auth_adapter.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/model_auth_adapter.py) | OCI 模型适配器（最复杂的 adapter） | 全文 |
| [genai_bench/auth/oci/storage_auth_adapter.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/storage_auth_adapter.py) | OCI 存储适配器（与上一个共用底层 auth） | 全文 |
| [genai_bench/cli/cli.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py) | 工厂的两个真实调用点 + 后端别名映射 | L251-L264、L638-L650 |

---

## 4. 核心概念与源码讲解

### 4.1 两类 provider 接口

#### 4.1.1 概念说明

genai-bench 用 Python 的 `abc.ABC` 定义了两个抽象基类，分别描述「模型认证对象该长什么样」和「存储认证对象该长什么样」。抽象基类的核心价值是**契约**：任何子类都必须实现标了 `@abstractmethod` 的方法，否则实例化时报错。这保证了无论你接的是哪家云，工厂返回的对象都「一定能调用这几个方法」。

两套接口的方法设计反映了两类调用的关注点差异：

- **模型认证**关心「发请求时带什么 HTTP 头」，所以有 `get_headers()`。
- **存储认证**关心「怎么初始化一个存储客户端」，所以有 `get_client_config()`。

#### 4.1.2 核心流程

两个接口的方法对照如下：

| 接口 | 抽象方法（必须实现） | 带默认实现的方法 |
| --- | --- | --- |
| `ModelAuthProvider` | `get_headers() -> Dict[str,str]`<br>`get_config() -> Dict[str,Any]`<br>`get_auth_type() -> str` | `get_credentials() -> Optional[Any]`（默认返回 `None`） |
| `StorageAuthProvider` | `get_client_config() -> Dict[str,Any]`<br>`get_credentials() -> Any`<br>`get_storage_type() -> str` | `get_region() -> Optional[str]`（默认返回 `None`） |

注意一个**容易忽略的不对称**：`ModelAuthProvider.get_credentials` 是**有默认实现**的（返回 `None`），子类可以不重写；而 `StorageAuthProvider.get_credentials` 是**纯抽象**的，子类必须实现。原因是模型认证里凭据往往就藏在 headers 里（Bearer token），不一定要单独返回；而存储客户端几乎总是需要一个具体凭据对象（签名器、密钥等），所以强制实现。

#### 4.1.3 源码精读

**模型认证基类** —— 三个抽象方法加一个默认实现：

[genai_bench/auth/model_auth_provider.py:L7-L43](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/model_auth_provider.py#L7-L43) 定义 `ModelAuthProvider(ABC)`：`get_headers`（L10-L17，返回认证头）、`get_config`（L19-L26，返回服务端配置字典）、`get_auth_type`（L28-L35，返回认证类型标识，如 `'api_key'`/`'iam'`/`'oauth'`）都是 `@abstractmethod`；而 `get_credentials`（L37-L43）**没有** `@abstractmethod` 装饰，默认 `return None`。

**存储认证基类** —— 结构对称但 `get_credentials` 是抽象的：

[genai_bench/auth/storage_auth_provider.py:L7-L43](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/storage_auth_provider.py#L7-L43) 定义 `StorageAuthProvider(ABC)`：`get_client_config`（L10-L17，返回初始化存储客户端所需的配置）、`get_credentials`（L19-L26）、`get_storage_type`（L28-L35，返回 `'oci'`/`'aws'`/`'azure'` 等）都是抽象方法；只有 `get_region`（L37-L43）带默认实现返回 `None`。

**旧基类** —— 理解它才能理解为什么需要 adapter：

[genai_bench/auth/auth_provider.py:L5-L24](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/auth_provider.py#L5-L24) 定义 `AuthProvider(ABC)`，只有两个抽象方法 `get_config` 和 `get_credentials`。这是项目早期的统一认证基类，`OpenAIAuth`、`TogetherAuth`、各 OCI auth 类都继承它。它**没有** `get_headers` / `get_auth_type` / `get_storage_type`，所以无法直接满足后来的两个新接口——这正是 adapter 要解决的问题。

#### 4.1.4 代码实践

**实践目标**：亲手验证「抽象方法未实现时无法实例化」这一契约机制，并体会两个接口的对称与不对称。

**操作步骤**（这是「源码阅读型实践」，无需真实云凭据）：

1. 打开 `genai_bench/auth/model_auth_provider.py`，确认 `get_credentials`（L37）**没有** `@abstractmethod`。
2. 打开 `genai_bench/auth/storage_auth_provider.py`，确认 `get_credentials`（L19）**有** `@abstractmethod`。
3. 在项目根目录运行下面这段示例代码（标注为「示例代码」，非项目原有文件）：

```python
# 示例代码：验证抽象基类的契约
from genai_bench.auth.model_auth_provider import ModelAuthProvider
from genai_bench.auth.storage_auth_provider import StorageAuthProvider

# 1) 只实现部分抽象方法，尝试实例化
class Incomplete(StorageAuthProvider):
    def get_client_config(self):
        return {}

try:
    Incomplete()
except TypeError as e:
    print("StorageAuthProvider 强制实现全部抽象方法：", e)
```

**需要观察的现象**：

- `Incomplete()` 抛出 `TypeError`，提示还有 `get_credentials`、`get_storage_type` 未实现。
- 若把基类换成 `ModelAuthProvider` 并只实现 `get_headers`，同样会因缺 `get_config`/`get_auth_type` 报错；但**不实现** `get_credentials` 不会报错（因为它有默认实现）。

**预期结果**：你应能在终端看到 `TypeError: Can't instantiate abstract class ... with abstract method ...`，从而确认两套接口各自「强制实现」的方法集合。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ModelAuthProvider.get_credentials` 给了默认实现，而 `StorageAuthProvider.get_credentials` 却是纯抽象？

**参考答案**：模型认证场景下，凭据通常已经体现在 `get_headers()` 返回的 `Authorization` 头里（如 Bearer token），不一定需要单独的凭据对象，故默认 `None` 即可；存储认证场景下，几乎所有云存储 SDK 都需要一个具体的凭据/签名器对象来初始化客户端，因此强制子类提供，避免「忘实现」导致运行时才暴露 `None` 错误。

**练习 2**：假设要新增一个只支持模型认证、不支持存储的厂商，你需要实现哪几个抽象方法？

**参考答案**：只需实现 `ModelAuthProvider` 的 `get_headers`、`get_config`、`get_auth_type` 三个抽象方法；`get_credentials` 可选（默认返回 `None`，除非你的凭据需要单独暴露）。无需实现任何 `StorageAuthProvider` 方法。

---

### 4.2 统一工厂 UnifiedAuthFactory

#### 4.2.1 概念说明

`UnifiedAuthFactory` 是认证体系的**唯一对外入口**。它是一个只有两个静态方法的类，把「provider 字符串」翻译成「具体认证对象」。业务代码（CLI、测试）只认这个工厂，不需要 import 任何具体厂商类。

「Unified（统一）」二字体现在两点：

1. **一个工厂管两件事**：模型认证和存储认证都在同一个类里，只是分两个方法（`create_model_auth` / `create_storage_auth`）。
2. **抹平构造差异**：有的 provider 直接 `new` 一个对象，有的要先调老工厂再造对象再包 adapter——这些差异被工厂内部吸收，外部看到的都是统一的「给字符串、拿接口对象」。

#### 4.2.2 核心流程

两个方法的分发流程（伪代码）：

```text
create_model_auth(provider, **kwargs) -> ModelAuthProvider
  if   provider == "openai"       -> OpenAIAuth       → OpenAIModelAuthAdapter(...)
  elif provider == "oci"          -> AuthFactory.create_oci_auth(...) → OCIModelAuthAdapter(...)
  elif provider == "aws-bedrock"  -> AWSBedrockAuth(...)
  elif provider == "azure-openai" -> AzureOpenAIAuth(...)
  elif provider == "gcp-vertex"   -> GCPVertexAuth(...)
  elif provider == "together"     -> TogetherAuth     → TogetherModelAuthAdapter(...)
  else                            -> raise ValueError

create_storage_auth(provider, **kwargs) -> StorageAuthProvider
  if   provider == "oci"    -> AuthFactory.create_oci_auth(...) → OCIStorageAuthAdapter(...)
  elif provider == "aws"    -> AWSS3Auth(...)
  elif provider == "azure"  -> AzureBlobAuth(...)
  elif provider == "gcp"    -> GCPStorageAuth(...)
  elif provider == "github" -> GitHubAuth(...)
  else                      -> raise ValueError
```

可以观察到三个规律：

- **返回类型恒定**：`create_model_auth` 永远返回 `ModelAuthProvider`，`create_storage_auth` 永远返回 `StorageAuthProvider`，无论 provider 是谁。
- **两类构造策略并存**：左半边（openai/oci/together）是「老对象 + adapter」，右半边（aws-bedrock/azure-openai/gcp-vertex 等）是「直接 new」。
- **OCI 是唯一横跨两边的 provider**：它既出现在模型工厂又出现在存储工厂，且都用 adapter（详情见 4.3）。
- **错误兜底**：未知 provider 抛 `ValueError`，且错误信息里**列出全部合法取值**，便于排错。

#### 4.2.3 源码精读

**模型认证工厂** —— 整个分发链：

[genai_bench/auth/unified_factory.py:L31-L99](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/unified_factory.py#L31-L99) 是 `create_model_auth`。其中：

- `provider == "openai"` 分支（[L46-L49](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/unified_factory.py#L46-L49)）：先 `OpenAIAuth(api_key=...)` 造老对象，再 `OpenAIModelAuthAdapter(openai_auth)` 包一层返回。
- `provider == "aws-bedrock"` 分支（[L62-L69](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/unified_factory.py#L62-L69)）：直接 `return AWSBedrockAuth(...)`，**没有 adapter**——因为 `AWSBedrockAuth` 自己就是 `ModelAuthProvider` 子类。
- 未知 provider 分支（[L94-L99](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/unified_factory.py#L94-L99)）：`raise ValueError(...)`，信息里列出 `openai, oci, aws-bedrock, azure-openai, gcp-vertex, together`。

**存储认证工厂** —— 结构与模型工厂同构：

[genai_bench/auth/unified_factory.py:L101-L165](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/unified_factory.py#L101-L165) 是 `create_storage_auth`。其中 OCI 分支（[L115-L124](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/unified_factory.py#L115-L124)）复用 `AuthFactory.create_oci_auth(...)` 后包 `OCIStorageAuthAdapter`；其余（aws/azure/gcp/github）直接 new；未知 provider（[L161-L165](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/unified_factory.py#L161-L165)）抛错并列出合法值。

**真实调用点之一（benchmark 主流程）**：

[genai_bench/cli/cli.py:L262-L264](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L262-L264) 调用 `UnifiedAuthFactory.create_model_auth(auth_backend, **auth_kwargs)`。注意 `auth_backend` 不是原始的 `--api-backend` 字符串，而是经过一张**别名映射表**归一化后的值：

[genai_bench/cli/cli.py:L250-L260](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L250-L260) 定义 `auth_backend_map`，把 `oci-cohere`/`oci-genai`/`oci-openai`/`cohere` 都映射成 `"oci"`，把 `vllm`/`sglang` 映射成 `"openai"`（因为它们走 OpenAI 兼容协议，这与 u3-l3 讲的「vllm/sglang 是 OpenAIUser 的别名」完全呼应），其余原样透传。

**真实调用点之二（上传结果）**：

[genai_bench/cli/cli.py:L638-L650](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L638-L650) 调用 `create_storage_auth` 拿到 `storage_auth_provider`，紧接着把它喂给 `StorageFactory.create_storage(...)`（L648）造出真正的存储客户端。这里能清楚看到职责分层：**认证工厂只造认证对象，存储工厂拿认证对象造存储客户端**——认证与存储两套子系统的衔接点就在此。

#### 4.2.4 代码实践

**实践目标**：通读 `unified_factory.py`，列出 `create_model_auth` 支持的全部 provider 及其各自构造的类，画出 provider→（adapter）→实现类 的关系图。

**操作步骤**：

1. 打开 [unified_factory.py:L46-L99](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/unified_factory.py#L46-L99)，逐个 `elif` 抄下 provider 字符串与其 return 表达式。
2. 区分两类：return 里**出现 Adapter 的**记为「适配器路线」，**直接 return 具体类的**记为「直接实现路线」。
3. 用测试文件交叉验证你的结论：[tests/auth/test_unified_factory.py:L57-L97](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/auth/test_unified_factory.py#L57-L97) 用 `assert isinstance(auth, AWSBedrockAuth / AzureOpenAIAuth / GCPVertexAuth)` 验证了直接实现路线的返回类型。
4. 画出关系图（参考下表）。

**预期结果（provider → 构造路线对照表）**：

| provider | 内部构造 | 返回类型 | 路线 |
| --- | --- | --- | --- |
| `openai` | `OpenAIAuth` → `OpenAIModelAuthAdapter` | `ModelAuthProvider` | 适配器 |
| `oci` | `AuthFactory.create_oci_auth` → `OCIModelAuthAdapter` | `ModelAuthProvider` | 适配器 |
| `together` | `TogetherAuth` → `TogetherModelAuthAdapter` | `ModelAuthProvider` | 适配器 |
| `aws-bedrock` | `AWSBedrockAuth(...)` | `AWSBedrockAuth` | 直接实现 |
| `azure-openai` | `AzureOpenAIAuth(...)` | `AzureOpenAIAuth` | 直接实现 |
| `gcp-vertex` | `GCPVertexAuth(...)` | `GCPVertexAuth` | 直接实现 |

关系图（文字版）：

```text
                            UnifiedAuthFactory.create_model_auth
                                          │
        ┌─────────────────── 适配器路线 ───────────────────┐    ┌── 直接实现路线 ──┐
        │                                                  │    │                  │
 "openai"→ OpenAIAuth ──┐                                  │  "aws-bedrock" → AWSBedrockAuth(ModelAuthProvider)
 "together"→ TogetherAuth ─┤ 包装进 *ModelAuthAdapter       │  "azure-openai"→ AzureOpenAIAuth(ModelAuthProvider)
 "oci"→ AuthFactory.create_oci_auth ──┘ (实现 ModelAuthProvider)│  "gcp-vertex" → GCPVertexAuth(ModelAuthProvider)
```

**需要观察的现象**：三条适配器路线的 adapter 类名都以 `ModelAuthAdapter` 结尾、且都继承 `ModelAuthProvider`；三条直接实现路线的具体类**直接继承** `ModelAuthProvider`（见 4.3.3 的 grep 结论）。

#### 4.2.5 小练习与答案

**练习 1**：为什么工厂方法都设计成 `@staticmethod` 而不是普通方法或类方法？

**参考答案**：工厂不需要访问实例状态（`self`）也不需要访问类状态（`cls`），它只是「输入字符串 + kwargs → 输出对象」的纯函数。用 `@staticmethod` 使意图明确，调用时也无需先实例化工厂（直接 `UnifiedAuthFactory.create_model_auth(...)`）。

**练习 2**：如果用户传了 `provider="anthropic"`（当前不支持），会发生什么？错误信息会帮他什么忙？

**参考答案**：会走到 `else` 分支抛 `ValueError("Unsupported model provider: anthropic. Supported: openai, oci, aws-bedrock, azure-openai, gcp-vertex, together")`。错误信息直接列出全部合法取值，省去用户翻文档的功夫——这是友好错误信息的常见写法。

---

### 4.3 适配器模式（OCI / OpenAI / Together）

#### 4.3.1 概念说明

4.2 已经看到「适配器路线」和「直接实现路线」并存。本节回答：**为什么会有适配器路线？**

答案在 2.3 已经埋下：`OpenAIAuth`、`TogetherAuth`、各 OCI auth 类都继承自**旧基类 `AuthProvider`**（只有 `get_config`/`get_credentials`），它们**早于** `ModelAuthProvider`/`StorageAuthProvider` 存在，并且已经在别处被使用。项目不想改动这些稳定的老类（怕引入回归），于是用 adapter 模式「让老对象适配新接口」。

适配器的本质：**它自己实现新接口（满足类型契约），但所有方法体都把调用转发给内部持有的老对象**（组合优于继承）。

#### 4.3.2 核心流程

适配器的工作机制：

```text
调用方 ──> adapter.get_headers()         # adapter 实现 ModelAuthProvider
              │
              └─> self.<老对象>.api_key   # 转发：从老对象取数据，组装成新接口要求的样子
                      │
                      └─> return {"Authorization": "Bearer ..."}
```

三个适配器的共性：

- **构造时注入老对象**：`__init__(self, <老对象>)`，把它存为属性。
- **实现全部抽象方法**：`get_headers`/`get_config`/`get_auth_type`/`get_credentials`。
- **方法体是「翻译」**：把老对象的数据翻译成新接口要的格式（比如把 `api_key` 拼成 `Authorization` 头）。

OCI 的特殊性：同一个底层 OCI auth 对象，既能用于模型认证（包进 `OCIModelAuthAdapter`），也能用于存储认证（包进 `OCIStorageAuthAdapter`）。即「一份 OCI 凭据，两种视图」。这是因为 OCI 的模型服务和对象存储**共用同一套身份与访问管理（IAM）凭据**，所以工厂里 OCI 分支都先调 `AuthFactory.create_oci_auth(...)` 拿到同一个底层对象，再按用途选不同 adapter。

#### 4.3.3 源码精读

**先确认「谁直接实现、谁是 adapter」**——用 grep 看类继承（结论性事实）：

- 直接继承新接口：`AWSBedrockAuth(ModelAuthProvider)`、`AzureOpenAIAuth(ModelAuthProvider)`、`GCPVertexAuth(ModelAuthProvider)`、`AWSS3Auth(StorageAuthProvider)`、`AzureBlobAuth(StorageAuthProvider)`、`GCPStorageAuth(StorageAuthProvider)`、`GitHubAuth(StorageAuthProvider)`。
- 继承旧接口（需 adapter）：`OpenAIAuth(AuthProvider)`、`TogetherAuth(AuthProvider)`、各 OCI auth 类。

**OpenAI 适配器（最典型）**：

[genai_bench/auth/openai/model_auth_adapter.py:L9-L58](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/openai/model_auth_adapter.py#L9-L58) 定义 `OpenAIModelAuthAdapter(ModelAuthProvider)`：

- 构造（[L12-L18](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/openai/model_auth_adapter.py#L12-L18)）：接收 `OpenAIAuth` 老对象，存为 `self.openai_auth`。
- `get_headers`（[L20-L29](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/openai/model_auth_adapter.py#L20-L29)）：从老对象取 `api_key`，拼成 `{"Authorization": "Bearer <key>"}`——这是「翻译」的典型：老对象只有裸 key，adapter 把它变成 HTTP 头。无 key 时返回 `{}`。
- `get_auth_type`（[L42-L48](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/openai/model_auth_adapter.py#L42-L48)）：返回固定字符串 `"api_key"`。
- `get_credentials`（[L50-L58](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/openai/model_auth_adapter.py#L50-L58)）：覆写默认实现，返回 `{"api_key": ...}`（测试 [test_unified_factory.py:L30-L31](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/auth/test_unified_factory.py#L30-L31) 正是断言这个字典）。

> 对照被包装的老对象 [genai_bench/auth/openai/auth.py:L7-L44](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/openai/auth.py#L7-L44)：`OpenAIAuth(AuthProvider)` 只实现 `get_config`（返回 `{}`）和 `get_credentials`（返回裸 key 字符串）。它**没有** `get_headers`、**没有** `get_auth_type`，所以无法直接当 `ModelAuthProvider` 用——这正是 adapter 存在的理由。

**Together 适配器**：[genai_bench/auth/together/model_auth_adapter.py:L9-L58](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/together/model_auth_adapter.py#L9-L58) 与 OpenAI 版**几乎逐字相同**（因为 Together 也是 Bearer token 认证），只是属性名换成 `together_auth`。这种高度重复也提示：未来可抽公共基类，但当前为保持简单而容忍重复。

**OCI 模型适配器（最复杂）**：

[genai_bench/auth/oci/model_auth_adapter.py:L9-L72](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/model_auth_adapter.py#L9-L72) 定义 `OCIModelAuthAdapter(ModelAuthProvider)`，构造时接收任意 `AuthProvider`（即任意 OCI auth 子类）。它的「翻译」更精巧：

- `get_headers`（[L20-L26](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/model_auth_adapter.py#L20-L26)）：返回 `{}`——因为 OCI 用签名器（signer）而非 HTTP 头，注释里写得很清楚「Empty dict as OCI uses signers, not headers」。
- `get_auth_type`（[L45-L62](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/model_auth_adapter.py#L45-L62)）：**根据老对象的类名动态推断**认证类型——`InstancePrincipal` → `"oci_instance_principal"`、`UserPrincipal` → `"oci_user_principal"`、`OBOToken` → `"oci_obo_token"`、`Session` → `"oci_security_token"`。这是「一份凭据多种形态」的体现。
- `get_credentials`（[L64-L72](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/model_auth_adapter.py#L64-L72)）：直接 `return self.oci_auth.get_credentials()`——把老对象的签名器原样透传，模型客户端拿这个签名器去签请求。

**OCI 存储适配器（共用同一底层 auth 的另一半视图）**：

[genai_bench/auth/oci/storage_auth_adapter.py:L9-L55](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/storage_auth_adapter.py#L9-L55) 定义 `OCIStorageAuthAdapter(StorageAuthProvider)`，**同样**接收一个 `AuthProvider`。它的 `get_credentials`（[L30-L36](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/storage_auth_adapter.py#L30-L36)）返回 `self.oci_auth`（整个老对象），`get_client_config`（[L20-L28](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/storage_auth_adapter.py#L20-L28)）返回 `{"auth_provider": self.oci_auth}`。对比模型适配器返回的是 `oci_auth.get_credentials()`（签名器），存储适配器返回的是整个 `oci_auth` 对象——因为存储客户端需要的是完整的认证 provider，而非单个签名器。**同一个底层 OCI auth 对象，被两个 adapter 暴露出不同的视图**，这是本讲最值得回味的设计。

**老工厂 AuthFactory.create_oci_auth** —— adapter 包装前的「造老对象」环节：

[genai_bench/auth/factory.py:L41-L100](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/factory.py#L41-L100) 根据 `auth_type`（`user_principal`/`instance_principal`/`security_token`/`instance_obo_user`）造出对应的 OCI auth 子类（[L71-L81](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/factory.py#L71-L81) 先校验 `auth_type` 合法性）。注意统一工厂的 OCI 分支（unified_factory.py L53-L59、L117-L123）正是把 kwargs 透传给它——**老工厂没有被废弃，而是被新工厂当作内部零件复用**。

#### 4.3.4 代码实践

**实践目标**：动手验证 adapter 的「翻译」行为，并直观对比「直接实现」与「适配器」两条路线返回对象的真实差异。

**操作步骤**：

1. 运行下面这段示例代码（标注为「示例代码」，无真实凭据即可跑，因为 OpenAI 路线只需一个任意字符串作 key）：

```python
# 示例代码：对比直接实现 vs 适配器路线
from genai_bench.auth.unified_factory import UnifiedAuthFactory
from genai_bench.auth.openai.model_auth_adapter import OpenAIModelAuthAdapter
from genai_bench.auth.aws.bedrock_auth import AWSBedrockAuth

# 适配器路线：openai
m1 = UnifiedAuthFactory.create_model_auth("openai", api_key="sk-test")
print("openai 返回类型:", type(m1).__name__)            # OpenAIModelAuthAdapter
print("openai headers:", m1.get_headers())              # {'Authorization': 'Bearer sk-test'}
print("openai auth_type:", m1.get_auth_type())          # api_key
print("openai 是否 adapter:", isinstance(m1, OpenAIModelAuthAdapter))

# 直接实现路线：aws-bedrock（不需要真实凭据也能构造对象）
m2 = UnifiedAuthFactory.create_model_auth(
    "aws-bedrock", access_key_id="AK", secret_access_key="SK", region="us-east-1"
)
print("aws-bedrock 返回类型:", type(m2).__name__)       # AWSBedrockAuth
print("aws-bedrock 是否直接继承 ModelAuthProvider:",
      isinstance(m2, AWSBedrockAuth) and not hasattr(m2, "openai_auth"))
```

2. 在 `OCIModelAuthAdapter.get_auth_type`（[L45-L62](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/model_auth_adapter.py#L45-L62)）处，对照 `factory.py` 返回的四个 OCI auth 类名（`OCIUserPrincipalAuth`、`OCIInstancePrincipalAuth`、`OCIOBOTokenAuth`、`OCISessionAuth`），手动推演每个类名会被映射成哪个 auth_type 字符串。

**需要观察的现象**：

- `openai` 路线返回 `OpenAIModelAuthAdapter`，其 `get_headers()` 把裸 key 翻译成了 HTTP 头——这正是 adapter「翻译」职责的体现。
- `aws-bedrock` 路线返回 `AWSBedrockAuth` 本身，对象上**没有** `openai_auth`/`oci_auth` 这类「被包装老对象」的属性，证明它是直接实现而非适配器。
- 类名含 `UserPrincipal` 的会被 OCI 适配器映射为 `"oci_user_principal"`。

**预期结果**：终端打印 `openai headers: {'Authorization': 'Bearer sk-test'}` 与 `aws-bedrock 是否直接继承 ...: True`。

> 注：`OCIModelAuthAdapter` 的运行验证需要构造一个 OCI auth 老对象（依赖 `oci` 库与配置文件），较为繁琐。本实践采用「源码阅读 + 手动推演」方式理解其类名→auth_type 映射；若要真实运行，可参考 [tests/auth/test_unified_factory.py:L33-L55](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/auth/test_unified_factory.py#L33-L55) 用 `unittest.mock.patch` 把 `AuthFactory.create_oci_auth` 替换成 `MagicMock()` 来绕过真实凭据。这部分行为涉及 OCI 内部细节，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：假设项目要新增一个厂商 `FooLLM`，它和 OpenAI 一样用 Bearer token、且已经存在一个继承 `AuthProvider` 的老类 `FooAuth`。你会在工厂里怎么接？需要新写 adapter 吗？

**参考答案**：推荐复用 adapter 思路——新增 `FooModelAuthAdapter(ModelAuthProvider)`，构造时接收 `FooAuth`，`get_headers` 拼成 `{"Authorization": "Bearer <foo_auth.api_key>"}`；然后在 `create_model_auth` 加 `elif provider == "foo": return FooModelAuthAdapter(FooAuth(api_key=kwargs.get("api_key")))`。若懒得新写 adapter，也可让 `FooAuth` 直接改继承 `ModelAuthProvider` 并补三个抽象方法（即走「直接实现」路线），但这会改动已有稳定的老类，风险更高。

**练习 2**：为什么 OCI 需要 `OCIModelAuthAdapter` 和 `OCIStorageAuthAdapter` **两个** adapter，而 OpenAI 只需要 `OpenAIModelAuthAdapter` 一个？

**参考答案**：OpenAI 只做模型认证（没有「OpenAI 对象存储」），所以只需一个模型 adapter。OCI 同时提供模型服务（Generative AI）和对象存储（Object Storage），且两者**共用同一套 IAM 凭据**，因此同一份底层 OCI auth 对象要被分别适配成 `ModelAuthProvider`（暴露签名器给模型客户端）和 `StorageAuthProvider`（暴露完整 auth provider 给存储客户端）两种视图——这就是「一份凭据，两种视图」。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个贯穿任务。

**任务**：画一张完整的「认证体系鸟瞰图」，并写出一份「provider 接入清单」。

**步骤**：

1. **画接口层**：在最顶部画出 `ModelAuthProvider` 和 `StorageAuthProvider` 两个 ABC，标注各自的抽象方法。
2. **画工厂层**：在中间画出 `UnifiedAuthFactory`，画两条箭头分别指向两个 ABC（`create_model_auth`/`create_storage_auth`），并标注它的两个真实调用点 `cli.py:263` 与 `cli.py:639`。
3. **画实现层**：在底部把所有 provider 分两列排列：
   - 左列（模型）：`openai`/`oci`/`together`（adapter 路线，画一个「老对象→adapter」的组合框）、`aws-bedrock`/`azure-openai`/`gcp-vertex`（直接实现，单框）。
   - 右列（存储）：`oci`（adapter）、`aws`/`azure`/`gcp`/`github`（直接实现）。
4. **写接入清单**：假设现在要新增存储后端 `aliyun-oss`，按本讲学到的模式，列出你需要的改动：
   - 新建 `AliyunOSSAuth(StorageAuthProvider)`，实现 `get_client_config`/`get_credentials`/`get_storage_type`（返回 `"aliyun"`）。
   - 在 `create_storage_auth` 加 `elif provider == "aliyun": return AliyunOSSAuth(...)`。
   - 在未知 provider 的错误信息里补上 `aliyun`。
   - （可选）若已有老类，则改用 adapter 路线，避免改动老类。
5. **交叉验证**：打开 [tests/auth/test_unified_factory.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/auth/test_unified_factory.py) 与 [tests/integration/test_cross_cloud.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/integration/test_cross_cloud.py)，确认你的图上每个 provider→类 的箭头都有对应的测试用例在守护（例如 `test_create_aws_bedrock_model_auth` 守护 `aws-bedrock→AWSBedrockAuth`）。

**预期产出**：一张可读的关系图 + 一份 3~5 行的「新 provider 接入步骤」。这张图会让你在阅读后续 u5-l2（具体厂商凭据实现）、u5-l3（存储工厂）时，始终清楚每个类在整体中的位置。

## 6. 本讲小结

- genai-bench 把认证拆成**两套互不污染的接口**：`ModelAuthProvider`（管「带什么 HTTP 头」调模型）与 `StorageAuthProvider`（管「给存储客户端什么配置/凭据」），两者都基于 `abc.ABC`，用抽象方法强制子类实现核心能力。
- `UnifiedAuthFactory` 是认证体系的**唯一对外入口**，用两个 `@staticmethod`（`create_model_auth` / `create_storage_auth`）按 provider 字符串分发，未知 provider 抛 `ValueError` 并列出全部合法取值；它抹平了「直接 new」与「老对象+adapter」的构造差异，外部永远拿到统一接口类型。
- 工厂里并存**两种构造策略**：`aws-bedrock`/`azure-openai`/`gcp-vertex`（及存储侧的 aws/azure/gcp/github）**直接实现**新接口；`openai`/`together`/`oci` **走适配器路线**，原因是它们背后是继承旧基类 `AuthProvider` 的稳定老类。
- **适配器模式**的精髓是「实现新接口、内部转发给老对象」，本质是组合优于继承；`OpenAIModelAuthAdapter` 把裸 `api_key` 翻译成 `Authorization` 头就是典型「翻译」。
- **OCI 是最特殊的 provider**：同一份底层 IAM 凭据，被 `OCIModelAuthAdapter`（暴露签名器）和 `OCIStorageAuthAdapter`（暴露完整 auth provider）分别适配成模型视图与存储视图，体现「一份凭据，两种视图」。
- 工厂的两个真实调用点在 [cli.py:L263](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L263)（benchmark 主流程）与 [cli.py:L639](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L639)（上传结果），且模型侧经过 `auth_backend_map` 别名归一化（`vllm`/`sglang`→`openai`，`oci-*`→`oci`），与 u3-l3 的后端别名机制呼应。

## 7. 下一步学习建议

本讲只讲了认证对象的「构造入口与抽象分层」，还没有进入任何一家云的真实凭据细节。建议按以下顺序继续：

1. **u5-l2 模型认证 Provider 实现**：逐一深入 `AWSBedrockAuth`、`AzureOpenAIAuth`（含 Azure AD）、`GCPVertexAuth`（含 GCP credentials）等**直接实现路线**的内部，看它们各自的 `get_headers`/`get_auth_type` 返回什么、凭据从哪来。
2. **u5-l3 存储认证与多云存储**：看 `create_storage_auth` 产出的 `StorageAuthProvider` 如何被 `StorageFactory.create_storage` 消费、最终落到 `BaseStorage.upload_folder` 完成结果上传（承接本讲 L648 的衔接点）。
3. **u5-l4 OCI 认证与对象存储深入**：若你对 OCI 的「一份凭据两种视图」感兴趣，这里会展开 `OCISession`、user/instance principal、obo token 等多种认证方式，以及对象存储 datastore 细节。
4. 若想从「调用方」反向复习，可回到 u3-l3，体会 `auth_provider` 作为类属性被注入各 User 子类后，`get_headers()` 在真实发请求时如何被使用。
