# 模型认证 Provider 实现

## 1. 本讲目标

本讲承接 [u5-l1](u5-l1-auth-overview-and-factory.md)。上一讲我们建立了认证体系的整体骨架：两套抽象接口（`ModelAuthProvider` / `StorageAuthProvider`）、一个统一工厂 `UnifiedAuthFactory`，以及「直接实现新接口」与「老对象 + 适配器」两种构造策略。但当时我们只看了工厂的「分发逻辑」，没有进入任何 provider 的方法体。

本讲就钻进方法体里，回答三个具体问题：

1. **api_key 类认证**：最简单的「一个静态密钥」是怎么实现的？密钥从哪来、如何校验、如何变成 HTTP 头？
2. **云厂商认证**：AWS Bedrock、Azure OpenAI、GCP Vertex 三家云厂商的认证模型差异极大——有的根本不用 HTTP 头，有的支持两套凭据来源，它们各自如何落地 `get_headers` / `get_config` / `get_credentials` / `get_auth_type`？
3. **适配器封装**：为什么 OpenAI / Together / OCI 要套一层 adapter？adapter 到底「补」了什么、又「转发」了什么？

学完后，你应该能：

- 说出 `get_headers` / `get_config` / `get_auth_type` / `get_credentials` 四个方法在不同 provider 下的具体返回值差异；
- 解释为什么 AWS Bedrock 的 `get_headers` 永远返回空字典；
- 理解 adapter「实现新接口、内部转发老对象」的组合模式，并能动手补全一个最小 provider。

## 2. 前置知识

- **HTTP 认证的两种载体**：一是把凭据塞进请求头（如 `Authorization: Bearer <token>`、`api-key: <key>`、`x-goog-api-key: <key>`）；二是不碰请求头，由 SDK 在内部对每个请求做签名（如 AWS 的 SigV4、OCI 的请求签名）。genai-bench 的 `get_headers()` 只负责第一种，第二种交给各家 SDK。
- **凭据的多种来源**：显式参数、环境变量、本机配置文件（如 AWS 的 `~/.aws/credentials`、OCI 的 `~/.oci/config`、GCP 的 service account JSON）。provider 的构造函数通常写成 `参数 or os.getenv(...)`，让「显式参数优先于环境变量」。
- **`ModelAuthProvider` 接口回顾**（[model_auth_provider.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/model_auth_provider.py)）：三个抽象方法 `get_headers` / `get_config` / `get_auth_type` 必须实现，外加一个带默认实现的 `get_credentials`（默认返回 `None`）。
- **老接口 `AuthProvider`**（下一节会看到）：只有 `get_config` 和 `get_credentials` 两个抽象方法，**没有** `get_headers`、**没有** `get_auth_type`。这正是需要 adapter 的根本原因。

## 3. 本讲源码地图

本讲聚焦六个文件，分三组：

| 文件 | 角色 | 关键内容 |
| --- | --- | --- |
| [genai_bench/auth/model_auth_provider.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/model_auth_provider.py) | 新接口 ABC | 四个方法的契约 |
| [genai_bench/auth/auth_provider.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/auth_provider.py) | 老接口 ABC | 只有 `get_config` / `get_credentials` |
| [genai_bench/auth/openai/auth.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/openai/auth.py) | 老实现 | `OpenAIAuth`：api_key + 校验 |
| [genai_bench/auth/openai/model_auth_adapter.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/openai/model_auth_adapter.py) | 适配器 | `OpenAIModelAuthAdapter`：补 headers / auth_type |
| [genai_bench/auth/aws/bedrock_auth.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/aws/bedrock_auth.py) | 云厂商 | `AWSBedrockAuth`：SigV4，headers 恒空 |
| [genai_bench/auth/azure/openai_auth.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/azure/openai_auth.py) | 云厂商 | `AzureOpenAIAuth`：api_key 与 Azure AD 双模式 |
| [genai_bench/auth/gcp/vertex_auth.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/gcp/vertex_auth.py) | 云厂商 | `GCPVertexAuth`：API key 与 service account 双模式 |

此外还会引用两个消费侧文件，说明这些方法的产出物被谁用、怎么用：[genai_bench/cli/cli.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py)、[genai_bench/user/aws_bedrock_user.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/aws_bedrock_user.py)、[genai_bench/user/azure_openai_user.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/azure_openai_user.py)。

## 4. 核心概念与源码讲解

### 4.1 api_key 类认证：最简单的静态密钥

#### 4.1.1 概念说明

api_key 类认证是最朴素的形式：**服务方发给你一个长字符串，你每次请求都把它带在请求头里**。OpenAI、Together.ai 都用这套，头部字段是标准的 `Authorization: Bearer <key>`。

它的特点是「无状态、无签名」——密钥本身既是身份也是凭证，谁拿到谁能用，所以构造时必须严格校验「密钥不能为空」。

在 genai-bench 里，OpenAI 和 Together 的 api_key 实现走的是「老类 + 适配器」路线：老类 `OpenAIAuth` / `TogetherAuth` 只管「持有并校验密钥」，至于「把密钥变成 HTTP 头」「报告 auth 类型」这些事，由 adapter 补上。

#### 4.1.2 核心流程

一个 api_key provider 从构造到产出头部的流程：

```text
显式 api_key 参数 ─┐
                  ├─► api_key or os.getenv(XXX_API_KEY) ─► 非空校验 ─► 持有 self.api_key
环境变量 XXX_API_KEY ─┘                                          （缺失则 raise ValueError）

[使用时] adapter.get_headers() ─► {"Authorization": f"Bearer {self.api_key}"}
```

两个关键设计：

- **参数优先于环境变量**：`api_key or os.getenv(...)`，`or` 短路保证显式传值优先。
- **构造期即校验**：密钥缺失在 `__init__` 里直接抛错，而不是等到发请求时才失败——fail fast。

#### 4.1.3 源码精读

先看老类 `OpenAIAuth`（[genai_bench/auth/openai/auth.py:L10-L25](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/openai/auth.py#L10-L25)），它继承的是**老接口** `AuthProvider`：

```python
class OpenAIAuth(AuthProvider):
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key or not self.api_key.strip():
            raise ValueError(
                "OpenAI API key must be provided or set in "
                "OPENAI_API_KEY environment variable"
            )
```

注意两点：第一，校验用 `not self.api_key or not self.api_key.strip()`，既挡 `None`/空串，也挡纯空白串；第二，这个类**没有** `get_headers` 方法——老接口 `AuthProvider`（[genai_bench/auth/auth_provider.py:L8-L24](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/auth_provider.py#L8-L24)）只声明了 `get_config` 和 `get_credentials`。老接口的 `get_credentials` 返回的是**裸字符串**（[openai/auth.py:L36-L44](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/openai/auth.py#L36-L44)），不是字典。

把密钥变成 HTTP 头的工作在 adapter 里。`OpenAIModelAuthAdapter`（[genai_bench/auth/openai/model_auth_adapter.py:L20-L48](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/openai/model_auth_adapter.py#L20-L48)）实现了新接口 `ModelAuthProvider`：

```python
def get_headers(self) -> Dict[str, str]:
    if self.openai_auth.api_key:
        return {"Authorization": f"Bearer {self.openai_auth.api_key}"}
    return {}

def get_auth_type(self) -> str:
    return "api_key"
```

这就是 adapter 的核心价值——**老类没有的概念（HTTP 头、auth 类型），由 adapter 现场合成**。`TogetherModelAuthAdapter`（[together/model_auth_adapter.py:L20-L29](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/together/model_auth_adapter.py#L20-L29)）的结构与 OpenAI 完全一致，只是包的是 `TogetherAuth`、读的环境变量是 `TOGETHER_API_KEY`——两段代码几乎是复制粘贴，说明「Bearer 头」是 api_key 类的通用模式。

#### 4.1.4 代码实践

> 示例代码（非项目原有，用于观察行为）

```python
import os
# 清掉可能存在的真实环境变量，确保用显式 key
os.environ.pop("OPENAI_API_KEY", None)

from genai_bench.auth.openai.auth import OpenAIAuth
from genai_bench.auth.openai.model_auth_adapter import OpenAIModelAuthAdapter

auth = OpenAIModelAuthAdapter(OpenAIAuth(api_key="sk-test"))
print("headers   =", auth.get_headers())
print("auth_type =", auth.get_auth_type())
print("config    =", auth.get_config())
print("creds     =", auth.get_credentials())
```

操作步骤：

1. 把上面的片段存成脚本运行（不需要联网，也不需要真实 key）。
2. 再用 `TogetherAuth` + `TogetherModelAuthAdapter` 跑一遍对比。

需要观察的现象与预期结果：

- `headers` 应为 `{'Authorization': 'Bearer sk-test'}`；
- `auth_type` 应为 `'api_key'`；
- `creds`（adapter 版）应为 `{'api_key': 'sk-test'}`——注意它是**字典**，而老类 `OpenAIAuth.get_credentials()` 返回的是**裸字符串** `'sk-test'`，这正是 adapter 改变了输出形状的活样本；
- 单独 `OpenAIAuth(api_key=None)`（且环境变量也无）应直接 `raise ValueError`。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `OpenAIAuth` 的校验改成「不校验、允许空 key」，下游会有什么后果？

**参考答案**：构造不会报错，但 `adapter.get_headers()` 在 key 为空时返回 `{}`，于是发出去的请求**不带任何认证头**，服务端返回 401。错误被推迟到「真正发请求」时才暴露，且报错信息不直观——这正是 fail fast（构造期校验）的价值。

**练习 2**：`OpenAIModelAuthAdapter.get_credentials()` 和 `OpenAIAuth.get_credentials()` 返回类型分别是什么？为什么不同？

**参考答案**：前者返回 `Dict[str, str]`（`{"api_key": ...}`），后者返回 `str`。老接口 `AuthProvider.get_credentials` 的语义是「裸凭据」（注释里举例就是 API key 字符串）；新接口 `ModelAuthProvider` 面向多种凭据形态（AWS 是多字段字典、Azure 是 token 字典），统一成字典更通用，所以 adapter 顺手把字符串包成了字典。

---

### 4.2 云厂商认证：AWS Bedrock / Azure OpenAI / GCP Vertex

#### 4.2.1 概念说明

云厂商的认证远比 api_key 复杂，三家各有各的「脾气」：

- **AWS Bedrock**：用 SigV4 签名。签名由 `boto3` / `botocore` SDK 在内部对每个请求计算，**genai-bench 完全不需要在请求头里放任何东西**。所以 `get_headers()` 恒为空。genai-bench 要做的只是把 AWS 凭据「喂」给 SDK 去建一个 `boto3.Session`。
- **Azure OpenAI**：支持两套凭据——`api-key` 头（静态密钥）或 Azure AD（OAuth Bearer token）。两套互斥，由 `use_azure_ad` 开关切换。
- **GCP Vertex**：同样两套——API key（`x-goog-api-key` 头）或 service account（凭据 JSON 文件，由 Google 客户端库读取，headers 为空）。

这三家都「直接实现」新接口 `ModelAuthProvider`，不走 adapter。

#### 4.2.2 核心流程

**AWS Bedrock** 的流程（凭据驱动型）：

```text
access_key_id / secret / session_token / region / profile
        │  (全部支持 参数 or 环境变量；region 默认 us-east-1)
        ▼
get_credentials() ─► {
    "aws_access_key_id": ..., "aws_secret_access_key": ...,
    "aws_session_token": ..., "region_name": ..., "profile_name": ...,
}   ──形状正好是 boto3.Session() 的关键字参数──► AWSBedrockUser.on_start 建 session
```

**Azure OpenAI** 的流程（头部驱动 + 配置驱动）：

```text
use_azure_ad ?
  ├─ True 且有 azure_ad_token ─► headers["Authorization"] = "Bearer <token>"
  └─ 否则，有 api_key         ─► headers["api-key"] = "<key>"
                                 （两者都没有 ─► headers = {}）

get_config() ─► {api_version, auth_type, [azure_endpoint], [azure_deployment], [use_azure_ad]}
                   └─► AzureOpenAIUser.on_start 据此设 api_version / deployment / host
```

> Azure 的 `get_auth_type()` 是**运行时动态**的：`use_azure_ad` 为真返回 `"azure_ad"`，否则返回 `"api_key"`——同一个类的 auth 类型会随构造参数变。

**GCP Vertex** 的流程：与 Azure 同构，只是字段名不同（`x-goog-api-key`、`service_account`）。

#### 4.2.3 源码精读

**(1) AWS Bedrock——headers 恒空，凭据直喂 SDK**

`get_headers` 直接返回空字典（[genai_bench/auth/aws/bedrock_auth.py:L35-L44](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/aws/bedrock_auth.py#L35-L44)），注释点明原因：SigV4 由 SDK 处理。真正的产出是 `get_credentials`（[bedrock_auth.py:L74-L86](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/aws/bedrock_auth.py#L74-L86)）：

```python
def get_credentials(self) -> Dict[str, Optional[str]]:
    return {
        "aws_access_key_id": self.access_key_id,
        "aws_secret_access_key": self.secret_access_key,
        "aws_session_token": self.session_token,
        "region_name": self.region,
        "profile_name": self.profile,
    }
```

这个字典的键名（`aws_access_key_id`、`region_name`、`profile_name`…）**故意对齐 `boto3.Session()` 的参数名**，所以消费侧 [genai_bench/user/aws_bedrock_user.py:L52-L66](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/aws_bedrock_user.py#L52-L66) 能直接拆包建会话：先取 `creds = self.auth_provider.get_credentials()`，再按 `profile_name` 或显式密钥组装 `session_kwargs`，最后 `boto3.Session(**session_kwargs)`。

一个值得注意的安全设计：`get_config`（[bedrock_auth.py:L46-L64](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/aws/bedrock_auth.py#L46-L64)）里**只放 region / auth_type / profile / access_key_id / session_token，绝不放 `secret_access_key`**。原因是 `get_config()` 的产物会被写进实验元数据（见 4.2 节末尾的「消费侧」），密钥不能落盘。另外 `profile` 优先：设了 profile 就只暴露 profile，不再暴露显式密钥。

**(2) Azure OpenAI——双模式头部**

`get_headers`（[genai_bench/auth/azure/openai_auth.py:L38-L51](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/azure/openai_auth.py#L38-L51)）用 `if/elif` 实现优先级：

```python
if self.use_azure_ad and self.azure_ad_token:
    headers["Authorization"] = f"Bearer {self.azure_ad_token}"
elif self.api_key:
    headers["api-key"] = self.api_key
```

注意头部字段名不同：AD 走标准 `Authorization`，api_key 走 Azure 专有的 `api-key`。`get_auth_type`（[openai_auth.py:L75-L81](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/azure/openai_auth.py#L75-L81)）随之动态返回 `"azure_ad"` 或 `"api_key"`。消费侧 [azure_openai_user.py:L50-L63](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/user/azure_openai_user.py#L50-L63) 同时用了 `get_headers()`（设请求头）和 `get_config()`（取 `api_version` / `azure_deployment` / `azure_endpoint`）。

**(3) GCP Vertex——service account 时 headers 也为空**

`get_headers`（[genai_bench/auth/gcp/vertex_auth.py:L34-L45](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/gcp/vertex_auth.py#L34-L45)）只在有 `api_key` 时填 `x-goog-api-key`，service account 模式返回空——认证交给 Google 客户端库读 `GOOGLE_APPLICATION_CREDENTIALS`。`get_auth_type`（[vertex_auth.py:L64-L70](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/gcp/vertex_auth.py#L64-L70)）返回 `"api_key"` 或 `"service_account"`。一个细节：`get_credentials`（[vertex_auth.py:L72-L95](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/gcp/vertex_auth.py#L72-L95)）在「啥都没设」时返回 `None`——它是少数会返回 `None` 的 provider（基类默认就是 `None`）。

**消费侧：为什么 `get_config` 必须可序列化**

工厂创建出的 provider 被注入 User 类（[cli.py:L282](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L282) `user_class.auth_provider = auth_provider`），同时它的 `get_config()` 被写进实验元数据（[cli.py:L354](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L354) `auth_config=auth_provider.get_config()`）。所以每个 provider 的 `get_config` 都只返回**纯 dict / 基本类型**，且自觉排除敏感字段（AWS 的 secret、各家的裸 token 基本不进 config）——这是「可复现」与「不泄密」的平衡。

#### 4.2.4 代码实践

> 示例代码（非项目原有，用于对照测试断言）

```python
import os
# 清环境变量，避免本地配置干扰观察
for k in ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT",
          "AZURE_OPENAI_DEPLOYMENT", "AZURE_AD_TOKEN",
          "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE"):
    os.environ.pop(k, None)

from genai_bench.auth.azure.openai_auth import AzureOpenAIAuth
from genai_bench.auth.aws.bedrock_auth import AWSBedrockAuth

# Azure api_key 模式
az = AzureOpenAIAuth(api_key="key", api_version="2023-12-01",
                     azure_endpoint="https://t.openai.azure.com",
                     azure_deployment="gpt-4")
print("azure headers   =", az.get_headers())   # {'api-key': 'key'}
print("azure auth_type =", az.get_auth_type()) # 'api_key'
print("azure config    =", az.get_config())

# Azure AD 模式
az_ad = AzureOpenAIAuth(use_azure_ad=True, azure_ad_token="bearer")
print("azure_ad headers =", az_ad.get_headers())   # {'Authorization': 'Bearer bearer'}
print("azure_ad type    =", az_ad.get_auth_type()) # 'azure_ad'

# AWS：headers 恒空，credentials 喂 boto3
aws = AWSBedrockAuth(access_key_id="ak", secret_access_key="sk",
                     region="us-east-1")
print("aws headers =", aws.get_headers())          # {}
print("aws creds   =", aws.get_credentials())      # boto3 风格 dict
print("aws config  =", aws.get_config())           # 注意没有 secret_access_key
```

操作步骤：

1. 运行脚本，对照 `tests/auth/azure/test_openai_auth.py` 与 `tests/auth/aws/test_bedrock_auth.py` 里的断言核对每个值。
2. 把 `AWSBedrockAuth` 换成带 `profile="test_profile"` 的构造，观察 `get_config()` 输出如何从「暴露 access_key_id」切换成「只暴露 profile」。

预期结果（来自项目测试断言）：

- Azure api_key 模式：`headers == {"api-key": "key"}`，`config["auth_type"] == "api_key"`，且 `"use_azure_ad" not in config`（False 不写入）。
- Azure AD 模式：`headers == {"Authorization": "Bearer bearer"}`，`config["auth_type"] == "azure_ad"`。
- AWS：`headers == {}`；`config` 含 `region` / `auth_type` / `access_key_id` / `session_token`（若有），**不含** `secret_access_key`，也**不含** `profile`（除非用 profile 构造）。

> 说明：以上断言均来自仓库 `tests/auth/` 下的真实测试，可直接 `pytest tests/auth/azure/test_openai_auth.py tests/auth/aws/test_bedrock_auth.py` 验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 AWS Bedrock 的 `get_headers()` 返回空字典，而 Azure / GCP 在某些模式下也返回空？

**参考答案**：AWS 全程靠 SigV4 由 SDK 签名，请求头里不需要任何预置凭据；Azure 的 AD 模式和 GCP 的 service account 模式同理——认证由各自 SDK 在内部完成（Azure AD token 由 SDK 刷新，GCP 由客户端库读凭据文件）。只有「静态密钥」类凭据（Azure api-key、GCP API key）才需要 genai-bench 显式塞进请求头。

**练习 2**：Azure 的 `get_auth_type()` 为什么要写成 `return "azure_ad" if self.use_azure_ad else "api_key"`，而不是返回固定字符串？

**参考答案**：因为同一个 `AzureOpenAIAuth` 类支持两种认证方式，具体走哪种由构造参数 `use_azure_ad` 决定。auth_type 会被写进实验元数据 `auth_config`，供事后复盘「这次实验到底用的是哪种认证」，所以必须反映真实运行时选择，而不是类名。

**练习 3**：AWS 的 `get_config()` 在使用 profile 时为什么不暴露 `access_key_id`？

**参考答案**：profile 模式下，凭据由 `~/.aws/credentials` 文件提供，运行时根本不需要 `access_key_id`；同时 `get_config()` 产物会落盘进实验元数据，少暴露一个字段就少一份泄露面。代码用 `if self.profile: ... else: ...` 的分支保证了「profile 与显式密钥互斥暴露」。

---

### 4.3 适配器封装：让老类说新接口

#### 4.3.1 概念说明

适配器（adapter）模式解决一个具体矛盾：项目里已经有一批稳定的「老认证类」（`OpenAIAuth` / `TogetherAuth` / 一众 OCI 认证类），它们继承老接口 `AuthProvider`，只实现了 `get_config` / `get_credentials`；而新代码（工厂、User 类、实验元数据）期望的是新接口 `ModelAuthProvider`，要求 `get_headers` / `get_auth_type`。

有两种解法：改老类（风险大、牵连广）或**套一层 adapter**（不动老类，新写一个小类实现新接口、内部持有老对象并转发）。genai-bench 选了后者——这就是 u5-l1 说的「组合优于继承」。

#### 4.3.2 核心流程

adapter 的工作分两类：

```text
老类已有的能力 ──(转发)──► get_config / get_credentials
老类缺失的能力 ──(补造)──► get_headers / get_auth_type
```

三个 adapter 的「补造」策略各有不同：

| adapter | `get_headers` | `get_auth_type` | 是否转发老 `get_config` |
| --- | --- | --- | --- |
| `OpenAIModelAuthAdapter` | 合成 `Authorization: Bearer` | 固定 `"api_key"` | 否（自己造 dict） |
| `TogetherModelAuthAdapter` | 同上 | 固定 `"api_key"` | 否 |
| `OCIModelAuthAdapter` | 恒空 `{}` | **按类名推断** `"oci_*"` | 是（`hasattr` 后 merge） |

#### 4.3.3 源码精读

最朴素的 `OpenAIModelAuthAdapter` 我们在 4.1 已看过：它**不转发**老类的 `get_config`，而是自己造一个 `{"auth_type": ..., "has_api_key": bool}`（[openai/model_auth_adapter.py:L31-L40](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/openai/model_auth_adapter.py#L31-L40)）。

最有意思的是 `OCIModelAuthAdapter`。OCI 不用请求头（用 signer 签名），所以 `get_headers` 恒空（[oci/model_auth_adapter.py:L20-L26](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/model_auth_adapter.py#L20-L26)）。难点在于：OCI 有多种认证方式（user principal / instance principal / OBO token / security token），但老类没有一个统一的「auth 类型」字段。adapter 用**类名嗅探**解决（[oci/model_auth_adapter.py:L45-L62](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/model_auth_adapter.py#L45-L62)）：

```python
class_name = self.oci_auth.__class__.__name__
if "InstancePrincipal" in class_name:
    return "oci_instance_principal"
elif "UserPrincipal" in class_name:
    return "oci_user_principal"
elif "OBOToken" in class_name:
    return "oci_obo_token"
elif "Session" in class_name:
    return "oci_security_token"
else:
    return "oci_unknown"
```

这是一种「没有元数据就靠反射」的实用主义写法——通过老对象的类名反推它属于哪种 OCI 认证。`get_config` 则**转发并合并**老类的输出（[oci/model_auth_adapter.py:L28-L43](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/model_auth_adapter.py#L28-L43)）：先放 `auth_type`，再用 `hasattr(self.oci_auth, "get_config")` 守卫后 `config.update(self.oci_auth.get_config())`；`get_credentials` 更直接，原样转发老类结果（[oci/model_auth_adapter.py:L64-L72](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/model_auth_adapter.py#L64-L72)）。

> 回到 u5-l1 的结论：OCI 最特殊——同一份 IAM 凭据，模型侧 adapter 给出「签名器视图」（headers 空、auth_type 标识类型），存储侧 adapter 给出「完整 provider 视图」。两个 adapter 共享同一个底层老对象，是「一份凭据，两种视图」。

#### 4.3.4 代码实践

> 示例代码（非项目原有）

实现一个最小的 `ModelAuthProvider` 子类——即「补全一个返回 headers 的 mock」，并验证它的 `get_config()` 形状符合新接口契约：

```python
from typing import Any, Dict
from genai_bench.auth.model_auth_provider import ModelAuthProvider

class MockVendorAuth(ModelAuthProvider):
    """示例代码：模拟一个仅支持 api_key 的假云厂商。"""
    def __init__(self, api_key: str):
        self.api_key = api_key

    def get_headers(self) -> Dict[str, str]:
        return {"x-mock-key": self.api_key}          # 补：合成自定义头

    def get_config(self) -> Dict[str, Any]:
        return {"auth_type": self.get_auth_type(), "has_api_key": bool(self.api_key)}

    def get_credentials(self) -> Dict[str, str]:
        return {"api_key": self.api_key}

    def get_auth_type(self) -> str:
        return "mock_api_key"

m = MockVendorAuth(api_key="mock-secret")
assert m.get_headers() == {"x-mock-key": "mock-secret"}
assert m.get_config()["auth_type"] == "mock_api_key"
assert isinstance(m.get_config(), dict)              # 必须可序列化，才能进实验元数据
print("mock provider OK:", m.get_config())
```

操作步骤：

1. 把片段存成脚本运行。
2. 故意注释掉 `get_auth_type` 方法，重新运行，观察会发生什么。

预期结果：

- 完整实现时脚本正常打印 config。
- 注释掉 `get_auth_type` 后，`MockVendorAuth(api_key=...)` 会抛 `TypeError: Can't instantiate abstract class ... with abstract method get_auth_type`——因为 `ModelAuthProvider` 是 `ABC`，三个抽象方法缺一不可。这验证了「接口契约靠 ABC 强制」。

> 说明：上面的 `MockVendorAuth` 是为讲解而写的示例代码，项目里并不存在。真实新增后端的流程见 u8-l3 扩展指南。

#### 4.3.5 小练习与答案

**练习 1**：为什么 adapter 选择「持有老对象引用」而不是「继承老类」？

**参考答案**：继承会同时继承老类的构造逻辑（如 `OpenAIAuth.__init__` 里的密钥校验），并且 Python 不支持同时继承两个都带抽象方法的 ABC 还能干净地选择性实现；更重要的是，组合（持有引用）让 adapter 与老类**解耦**——老类可以独立演化，adapter 只依赖它实际用到的方法。这正是「组合优于继承」。

**练习 2**：`OCIModelAuthAdapter.get_auth_type()` 为什么用类名嗅探而不是给老类加个属性？

**参考答案**：因为底层 OCI 认证类（`OCIUserPrincipalAuth` / `OCIInstancePrincipalAuth` 等）是已稳定的代码，加属性要改多个老类；而 adapter 作为「外挂」，用 `self.oci_auth.__class__.__name__` 反射既不动老类、又能区分类型。代价是耦合了类名（重命名老类会破坏映射），这是该实用主义写法的已知折中。

---

## 5. 综合实践

把本讲三块内容串起来，完成 spec 要求的实践：**选择一个 provider（如 `AzureOpenAIAuth`），补全一个返回 headers 的 mock，并验证 `get_config` 输出符合预期**。

### 实践目标

- 用真实 provider（`AzureOpenAIAuth`）观察 `get_headers` / `get_config` 在两种模式下的差异；
- 自己动手补全一个 `ModelAuthProvider` mock，理解接口契约；
- 把两者的 `get_config()` 拿来对比，体会「为什么 config 必须是可序列化的纯 dict」。

### 操作步骤

> 示例代码（非项目原有，整合本讲内容）

```python
import os, json
for k in ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT",
          "AZURE_OPENAI_DEPLOYMENT", "AZURE_AD_TOKEN"):
    os.environ.pop(k, None)

from typing import Any, Dict
from genai_bench.auth.model_auth_provider import ModelAuthProvider
from genai_bench.auth.azure.openai_auth import AzureOpenAIAuth

# ---- 1) 用真实 provider 观察两种模式 ----
az_key = AzureOpenAIAuth(api_key="K", azure_endpoint="https://t.openai.azure.com",
                         azure_deployment="d", api_version="2024-02-01")
az_ad  = AzureOpenAIAuth(use_azure_ad=True, azure_ad_token="T")

# ---- 2) 补全一个返回 headers 的 mock ----
class MyMockAuth(ModelAuthProvider):
    def __init__(self, token: str):
        self.token = token
    def get_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}
    def get_config(self) -> Dict[str, Any]:
        return {"auth_type": self.get_auth_type(), "has_token": bool(self.token)}
    def get_auth_type(self) -> str:
        return "mock_bearer"

mock = MyMockAuth(token="mock-tok")

# ---- 3) 统一对比 get_config，并验证可序列化 ----
for name, p in [("azure(api_key)", az_key), ("azure(ad)", az_ad), ("mock", mock)]:
    cfg = p.get_config()
    json.dumps(cfg)                       # 能 json.dumps ⇒ 可写进实验元数据 auth_config
    print(f"{name:16s} headers={p.get_headers()}")
    print(f"{name:16s} config ={cfg}")
```

### 需要观察的现象

- `azure(api_key)` 的 headers 是 `{'api-key': 'K'}`，`config` 的 `auth_type` 是 `'api_key'`；
- `azure(ad)` 的 headers 切换成 `{'Authorization': 'Bearer T'}`，`config` 的 `auth_type` 切换成 `'azure_ad'`；
- 三者的 `config` 都能通过 `json.dumps`（这是 [cli.py:L354](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L354) 把它写进 `ExperimentMetadata.auth_config` 的前提）。

### 预期结果 / 验证

对照 [tests/auth/azure/test_openai_auth.py:L94-L117](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/auth/azure/test_openai_auth.py#L94-L117) 的断言：api_key 模式下 `config` 含 `api_version` / `auth_type=="api_key"` / `azure_endpoint` / `azure_deployment`，且不含 `use_azure_ad`；AD 模式下 `config["auth_type"]=="azure_ad"` 且 `config["use_azure_ad"] is True`。你的观察应与之一致。若运行结果与断言不符，请「待本地验证」后回头核对 genai-bench 版本。

## 6. 本讲小结

- **api_key 类认证**：最简单的静态密钥，老类 `OpenAIAuth` / `TogetherAuth` 只管「持有 + 构造期校验」，密钥缺失即 `raise ValueError`；把密钥变成 `Authorization: Bearer` 头是 adapter 的活。
- **云厂商认证差异极大**：AWS Bedrock 的 `get_headers` 恒空（SigV4 由 SDK 签名），靠 `get_credentials` 喂 `boto3.Session`；Azure 与 GCP 都支持「静态密钥头」和「SDK 接管认证」双模式，且 `get_auth_type` 是运行时动态值。
- **`get_config` 必须可序列化且脱敏**：因为它会被写进 `ExperimentMetadata.auth_config`（[cli.py:L354](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L354)）；AWS 据此排除 `secret_access_key`、用 profile 时隐藏显式密钥。
- **接口契约靠 ABC 强制**：`ModelAuthProvider` 三个抽象方法（`get_headers` / `get_config` / `get_auth_type`）缺一不可，否则实例化即 `TypeError`。
- **适配器 = 组合优于继承**：adapter 持有老对象引用，「转发」老类已有的 `get_config` / `get_credentials`，「补造」老类缺失的 `get_headers` / `get_auth_type`；`OCIModelAuthAdapter` 更用类名嗅探推断 OCI 认证子类型。
- **消费侧两条路径**：头部驱动型（Azure/GCP/OpenAI）用 `get_headers`；凭据驱动型（AWS）用 `get_credentials`；`get_config` 则统一用于实验元数据记录。

## 7. 下一步学习建议

- **存储认证**：本讲只讲了「模型认证」provider。下一讲 [u5-l3 存储认证与多云存储](u5-l3-storage-auth-and-providers.md) 讲对称的「存储认证」`StorageAuthProvider` 及 `StorageFactory`，结构类似但面向对象存储。
- **OCI 深入**：若对 `OCIModelAuthAdapter` 的类名嗅探感兴趣，可先读 [u5-l4 OCI 认证与对象存储深入](u5-l4-oci-auth-and-object-storage.md)，那里讲清 OCI 四种认证方式与 session 机制。
- **扩展实践**：想真正新增一个后端 provider，跳到 [u8-l3 扩展指南](u8-l3-extension-guide.md)，它会把「实现 provider + 注册到工厂 + 接入 User 类」串成完整步骤。
- **回到主流程**：理解了 provider 之后，可以重读 [u5-l1](u5-l1-auth-overview-and-factory.md) 的工厂调用点（[cli.py:L263](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L263)），把「别名归一化 → 工厂分发 → 注入 User 类 → 写入实验元数据」这条链路彻底打通。
