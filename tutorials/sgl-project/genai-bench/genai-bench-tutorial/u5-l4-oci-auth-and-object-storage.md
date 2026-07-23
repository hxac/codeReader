# OCI 认证与对象存储深入

## 1. 本讲目标

上一讲（u5-l3）我们把多云存储的全景走了一遍，并在小结里留了一个钩子：**OCI 的存储实现比其他厂商多一层封装（`OCIObjectStorage → OSDataStore → ObjectURI`）**。本讲就专门钻进 OCI 这条线，把「认证怎么来」和「对象怎么存」这两件事彻底讲透。

学完本讲，你应当能够：

- 说清 OCI 的四种认证方式（`user_principal` / `instance_principal` / `security_token` / `instance_obo_user`）各自的工作原理、凭据来源与适用场景。
- 理解为什么 OCI 这套认证要用「签名器（signer）」而不是「请求头里的 Bearer token」，以及这给适配器设计带来的不对称性。
- 复述 `OCIModelAuthAdapter` 与 `OCIStorageAuthAdapter` 如何把**同一份** IAM 凭据，分别适配成「给模型用的签名器视图」与「给存储用的完整 provider 视图」——即 u5-l1 提出的「一份凭据，两种视图」。
- 画出对象存储三层封装的数据流，理解 `namespace` 寻址、`oci://` URI 解析与 128MB 分片上传阈值。
- 读懂 `AuthFactory.create_oci_auth` 的 `auth_type` 路由，并能据此推断新加一种 OCI 认证需要改哪些地方。

## 2. 前置知识

本讲默认你已学完 u5-l1（认证体系总览）与 u5-l3（存储认证与多云存储）。在进入细节前，先用三段话补齐 OCI 特有的背景。

**OCI（Oracle Cloud Infrastructure）是什么。** OCI 是甲骨文的云平台。和多数云一样，它有「租户（tenancy）」「区间（compartment）」「区域（region）」这几层组织概念。本讲会反复碰到两个 OCI 独有的名词：

- **namespace（命名空间）**：OCI 对象存储（Object Storage）的全局寻址前缀，每个租户有一个，所有 bucket 都挂在某个 namespace 下。所以一个对象的「地址」是「namespace + bucket + object 名」，缺一不可，这与 AWS S3「bucket 直接是顶层」不同。
- **signer（签名器）**：OCI 的 API 鉴权不走「在 HTTP 头里塞一个静态 Bearer token」的路子，而是**对每个请求做签名**——用私钥或临时凭据，把请求的方法、路径、时间戳等算成一个签名放进 `Authorization` 头。这跟 AWS 的 SigV4 是一类思路。理解这一点非常关键：它解释了为什么 OCI 的 `get_credentials()` 返回的是一个「签名器对象」而不是一个字符串 token。

**几种典型认证身份。** 在 OCI 里，「你是谁」可以由不同来源决定：

- **User Principal（用户主体）**：一个真实的人类/服务账号，用本地配置文件里的 API Key（一对公私钥）证明身份。最常见的开发场景。
- **Instance Principal（实例主体）**：一段运行在 OCI 计算实例（VM）上的代码，不持有任何配置文件，而是靠「这台机器本身」在 OCI 元数据服务里取到的临时凭据证明身份。常用于部署在 OCI 上的服务。
- **Security Token / Session（安全令牌/会话）**：一份**临时**令牌（通常由某个令牌交换流程签发），配一把私钥一起使用，过期需要续期。常用于委托、短期授权。
- **OBO Token（On-Behalf-Of，代表用户）**：实例主体代表某个用户行事时持有的令牌，本质是「instance principal + 用户委托」的组合。

**承接 u5-l1/u5-l3 的两个结论。** u5-l1 指出 OCI 在认证体系里最特殊：**同一份 IAM 凭据会被模型认证与存储认证两个适配器分别加工**；u5-l3 指出 OCI 存储多一层封装以复用成熟实现并支持 namespace 寻址。本讲就是这两句话的「展开证明」。

## 3. 本讲源码地图

本讲涉及的源码集中在两个目录下，按「认证」与「存储」分两条线：

| 文件 | 角色 |
| --- | --- |
| [auth/oci/session.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/session.py) | `security_token` 认证：读配置文件里的私钥+安全令牌，构造 `SecurityTokenSigner` |
| [auth/oci/user_principal.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/user_principal.py) | `user_principal` 认证：读 OCI 配置文件 + API Key，构造签名器 |
| [auth/oci/instance_principal.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/instance_principal.py) | `instance_principal` 认证：无配置文件，从实例元数据取临时凭据 |
| [auth/oci/obo_token.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/obo_token.py) | `instance_obo_user` 认证：用 OBO token + region 构造签名器 |
| [auth/oci/model_auth_adapter.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/model_auth_adapter.py) | 把 OCI 认证适配成「模型认证」视图（返回签名器） |
| [auth/oci/storage_auth_adapter.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/storage_auth_adapter.py) | 把 OCI 认证适配成「存储认证」视图（返回完整 provider） |
| [auth/factory.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/factory.py) | `create_oci_auth(auth_type=...)`：按 `auth_type` 路由到上述四个类 |
| [storage/oci_storage.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_storage.py) | 第一层：`OCIObjectStorage`，实现 `BaseStorage` 契约 |
| [storage/oci_object_storage/os_datastore.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_object_storage/os_datastore.py) | 第二层：`OSDataStore`，真正封装 OCI SDK 的 `ObjectStorageClient` |
| [storage/oci_object_storage/object_uri.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_object_storage/object_uri.py) | 第三层：`ObjectURI`，Pydantic 模型，解析/序列化 `oci://` 地址 |
| [storage/oci_object_storage/datastore.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_object_storage/datastore.py) | `DataStore` 抽象基类，定义 upload/download 契约 |

> 一个容易被忽视的事实：`oci`（OCI 的 Python SDK）是 genai-bench 的**核心依赖**，写在 [pyproject.toml:33](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/pyproject.toml#L33) 的 `dependencies` 里，而 `boto3`/`azure-storage-blob`/`google-cloud-storage` 都是 `[aws]`/`[azure]`/`[gcp]` 可选依赖。也就是说，OCI 是这个项目的「一等公民」云——这也解释了为什么 OCI 相关代码可以稳稳地放在核心包里，且认证类能直接 `import oci`。

## 4. 核心概念与源码讲解

### 4.1 OCI session 与认证类型

#### 4.1.1 概念说明

OCI 的认证不像 OpenAI 那样「一个 API Key 字符串走天下」。它要回答的问题更复杂：你的身份来自哪里？是一台机器、一个配置文件、还是一份临时令牌？genai-bench 把这些差异收敛成四种 `auth_type`，每种对应 `genai_bench/auth/oci/` 下的一个类：

| `auth_type` | 类 | 凭据来源 | 是否需要本地配置文件 | 典型场景 |
| --- | --- | --- | --- | --- |
| `user_principal` | `OCIUserPrincipalAuth` | OCI 配置文件里的 API Key | 是（默认 `~/.oci/config`） | 开发者在笔记本上跑压测 |
| `instance_principal` | `OCIInstancePrincipalAuth` | 计算实例元数据服务 | 否 | 服务部署在 OCI VM 上 |
| `security_token` | `OCISessionAuth` | 配置文件里的私钥 + 安全令牌文件 | 是 | 委托令牌、短期授权 |
| `instance_obo_user` | `OCIOBOTokenAuth` | OBO token 字符串 + region | 否 | 实例代表某用户行事 |

这四个类有一个共同点：它们都继承自 [auth_provider.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/auth_provider.py) 里的 `AuthProvider`，只需实现两个抽象方法：

- `get_config() -> Dict`：返回构造 OCI 客户端要用的「配置」（至少含 `region`）。
- `get_credentials() -> Any`：返回一个 **OCI 签名器（signer）**，而不是 token 字符串。

记住这条契约，下面看代码会顺很多。

#### 4.1.2 核心流程

四种认证的构造流程可以用一张表对齐来看，它们的差异只在「`get_config` 怎么来」和「`get_credentials` 怎么造签名器」这两步：

```text
                    user_principal        instance_principal     security_token         instance_obo_user
─────────────────────────────────────────────────────────────────────────────────────────────────────────
配置来源            oci.config.from_file  （无）                  oci.config.from_file   （构造参数）
                    + validate_config                            + 手动校验 3 字段
get_config 返回     整份 config           {region, tenancy}（来自 signer）  整份 config         {region}
签名器构造          Signer.from_config    InstancePrincipals     SecurityTokenSigner    SecurityTokenSigner
                                          SecurityTokenSigner()  (token, private_key)   (token, region=)
```

四个类还共享同一个**惰性初始化 + 缓存**的小模式：

```text
首次调用 get_config() / get_credentials()
   → self._config / self._signer is None ?
        是：读文件 / 建签名器，结果存入 self._config / self._signer
        否：直接返回缓存
```

这意味着：配置文件最多读一次、签名器最多建一次。这在压测里很重要——成千上万个并发虚拟用户会反复取凭据，缓存避免了重复的文件 IO 和签名器构造。

谁负责挑选这四个类？是 [auth/factory.py:42-100](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/factory.py#L42-L100) 里的 `AuthFactory.create_oci_auth`。它先白名单校验 `auth_type`，再逐一分发：

```text
create_oci_auth(auth_type, ...)
  ├─ auth_type not in [user_principal, instance_principal,
  │                    security_token, instance_obo_user]  →  ValueError
  ├─ "instance_principal"  → OCIInstancePrincipalAuth(security_token=token, region=region)
  ├─ "instance_obo_user"   → 校验 token/region 非空 → OCIOBOTokenAuth(token, region)
  ├─ "user_principal"      → OCIUserPrincipalAuth(config_path, profile)
  └─ "security_token"      → OCISessionAuth(config_path, profile)
```

#### 4.1.3 源码精读

**① user_principal——最常规的一种。** 它读 OCI 配置文件、用官方校验、再从配置造签名器：

- [user_principal.py:29-45](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/user_principal.py#L29-L45) `get_config()`：调 `oci.config.from_file(self.config_path, self.profile)` 读配置，紧接着 `oci.config.validate_config(config)` 做完整校验，通过后才缓存。
- [user_principal.py:47-60](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/user_principal.py#L47-L60) `get_credentials()`：`oci.signer.Signer.from_config(config)` 直接从配置里的 API Key 字段造出签名器。这是「用一把长期私钥签名」的典型路径。

**② instance_principal——「这台机器就是我」。** 它完全不碰配置文件：

- [instance_principal.py:38-49](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/instance_principal.py#L38-L49) `get_credentials()`：调 `oci.auth.signers.InstancePrincipalsSecurityTokenSigner()`，这个签名器会去 OCI 计算实例的元数据服务拿**临时**凭据。所以代码必须在 OCI 的 VM 上跑，且该 VM 所在区间配了允许它调目标服务的 IAM 策略。
- [instance_principal.py:23-36](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/instance_principal.py#L23-L36) `get_config()`：因为不读文件，配置直接从签名器「反推」——`{"region": signer.region, "tenancy": signer.tenancy_id}`；若构造时传了 `region`，则用它覆盖。

> 一个细节：[instance_principal.py:11-22](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/instance_principal.py#L11-L22) 的 `__init__` 声明了 `security_token` 参数，但**函数体只存了 `region`，并未使用 `security_token`**；同时 [factory.py:84](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/factory.py#L84) 调用时也照传了 `security_token=token`。这是个历史遗留的「多余参数」——instance principal 的凭据本就由元数据服务决定，不需要外部 token。读源码时遇到这种「签名上有、逻辑上无」的参数，要能识别出来。

**③ security_token——「私钥 + 临时令牌文件」。** 它和 user_principal 一样读配置文件，但校验更「手工」：

- [session.py:27-51](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/session.py#L27-L51) `get_config()`：`oci.config.from_file(...)` 后，**手动**检查三个必填字段 `["key_file", "security_token_file", "region"]`，缺一个就抛 `oci.exceptions.InvalidConfig`。注意它没有像 user_principal 那样调官方的 `validate_config`——因为 session 认证用的字段集合与默认校验不同（需要 `security_token_file`）。
- [session.py:53-87](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/session.py#L53-L87) `get_credentials()`：先用 `oci.signer.load_private_key_from_file(key_file, None)` 加载私钥，再 `open(security_token_file).read()` 读令牌字符串，最后 `oci.auth.signers.SecurityTokenSigner(security_token, private_key)` 组合成签名器。这里的 `security_token_file` 是个**会过期**的文件，通常由某个续期进程持续刷新。

**④ instance_obo_user——「带 region 的纯 token」。** 最简洁的一种：

- [obo_token.py:11-20](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/obo_token.py#L11-L20) 构造只收 `token` 和 `region` 两个必填参数。
- [obo_token.py:32-45](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/obo_token.py#L32-L45) `get_credentials()`：`SecurityTokenSigner(self.token, region=self.region)`。注意它和 security_token 都用 `SecurityTokenSigner`，区别在于：security_token 额外提供私钥做请求签名，而 OBO 直接用 token + region（签名细节由 SDK 内部处理）。[factory.py:86-91](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/factory.py#L86-L91) 还会先校验 `token is None or region is None` 才构造，是参数前置守卫。

四种认证的「异同」可以用一句话收束：**它们都实现 `get_config` + `get_credentials`，差别只在「配置从哪来」与「签名器怎么造」**。

#### 4.1.4 代码实践

**实践目标：** 对照源码，列出 OCI 支持的全部认证方式，并讲清 instance principal 与 user principal 的适用场景（即本讲规格指定的实践任务）。

**操作步骤：**

1. 打开 [auth/factory.py:71-81](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/factory.py#L71-L81)，抄下 `valid_types` 白名单——这是「OCI 支持哪些认证方式」的**权威答案**，比文档更准。
2. 对每个 `auth_type`，记录它在 [factory.py:83-97](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/factory.py#L83-L97) 里被实例化时**传入了哪些参数**（这决定它需要哪些 CLI 输入）。
3. 给 instance principal 与 user principal 各写一段「适用场景」说明，要点参考下表。

**参考答案（适用场景对比）：**

| 维度 | `user_principal` | `instance_principal` |
| --- | --- | --- |
| 凭据形态 | 本地 `~/.oci/config` + 一对长期 API Key | 无本地文件，靠 VM 元数据服务发的临时凭据 |
| 凭据有效期 | 长（除非人为轮换） | 短（自动轮换） |
| 跑在哪 | 任意能访问 OCI 的机器（如开发笔记本） | 必须跑在 OCI 计算实例上 |
| 适合谁 | 人在跑压测、临时实验、CI 里挂配置 | 常驻服务、无人值守的生产压测 worker |
| 安全取舍 | 私钥落地，需妥善保管 | 无凭据落地，但需配 IAM 策略放行该实例 |

**预期现象：** 你应能用一句话回答「我该选哪个」——**人在交互式跑 → user_principal；代码常驻 OCI VM 跑 → instance_principal**。

> 待本地验证：若你手头有 OCI 环境，可分别用 `--auth user_principal --config-file ~/.oci/config` 与 `--auth instance_principal --region <你的区间>` 各跑一次（参考 [docs/user-guide/multi-cloud-auth-storage.md:83-112](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/docs/user-guide/multi-cloud-auth-storage.md#L83-L112)），对比两者对 CLI 参数的依赖差异。没有 OCI 环境时，本实践以源码阅读 + 推理为准。

#### 4.1.5 小练习与答案

**练习 1：** `security_token` 和 `instance_obo_user` 都用了 `SecurityTokenSigner`，它们的构造参数有何不同？为什么？

**参考答案：** `OCISessionAuth`（security_token）传 `(security_token, private_key)`——额外提供私钥用于对请求做签名（[session.py:84-86](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/session.py#L84-L86)）；`OCIOBOTokenAuth` 传 `(self.token, region=self.region)`——只给 token 和 region，签名细节交给 SDK（[obo_token.py:42-44](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/obo_token.py#L42-L44)）。差别源于凭据来源：前者是「人持有的私钥+短期令牌」，后者是「实例代表用户拿到的现成 token」。

**练习 2：** 为什么 `OCIUserPrincipalAuth.get_config` 要调 `oci.config.validate_config`，而 `OCISessionAuth.get_config` 改成手动检查三个字段？

**参考答案：** 两者的必填字段集合不同。user_principal 走标准的 API Key 流程，字段集合与官方 `validate_config` 完全吻合，直接复用即可；session 认证需要 `security_token_file` 这种官方默认校验里没有的字段，所以手动列出 `["key_file", "security_token_file", "region"]` 做针对性校验（[session.py:43-50](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/session.py#L43-L50)）。

**练习 3：** 如果要新增第五种 OCI 认证（比如「resource principal」），需要改 `auth/oci/` 下哪几处？

**参考答案：** ① 新建一个继承 `AuthProvider` 的类，实现 `get_config` 与 `get_credentials`；② 在 [factory.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/factory.py) 的 `valid_types` 白名单里加新 `auth_type`，并加一条 `if auth_type == "...":` 分发分支；③（可选）在 [model_auth_adapter.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/model_auth_adapter.py) 的 `get_auth_type` 类名嗅探里补一个 `elif`，否则它会被归到 `oci_unknown`。

---

### 4.2 认证适配器（一份凭据，两种视图）

#### 4.2.1 概念说明

上节的四个 `AuthProvider` 子类，本身并不直接满足 u5-l1 定义的「模型认证」或「存储认证」接口——它们实现的是更底层的 `get_config` / `get_credentials`。而 `UnifiedAuthFactory` 要求：模型侧返回 `ModelAuthProvider`，存储侧返回 `StorageAuthProvider`。

OCI 的特殊性在于：**同一个 `AuthProvider` 实例**（比如一个 `OCIUserPrincipalAuth`），既要被用来给模型端点签名，又要被用来给对象存储签名。怎么让一份凭据同时满足两套不同的接口契约？答案是两个适配器：

- `OCIModelAuthAdapter`：实现 `ModelAuthProvider`，把 OCI 凭据**适配成模型端点要的样子**——核心是暴露签名器。
- `OCIStorageAuthAdapter`：实现 `StorageAuthProvider`，把**同一份** OCI 凭据**适配成存储客户端要的样子**——核心是暴露完整 provider。

这就是 u5-l1 所说的「**一份凭据，两种视图**」。两个适配器都遵循 u5-l2 总结的「组合优于继承」：它们不继承 `AuthProvider`，而是**持有**一个 `AuthProvider` 引用（`self.oci_auth`），按需转发。

这里有一个**不对称**值得预先点破：

- 模型适配器的 `get_credentials()` 返回的是**签名器**（signer），因为模型端点只需要对 HTTP 请求签名，`get_headers()` 干脆返回空 `{}`。
- 存储适配器的 `get_credentials()` 返回的是**整个 provider**，因为 `OSDataStore` 既要 `get_config()` 又要 `get_credentials()`，少了哪个都构不出 `ObjectStorageClient`。

#### 4.2.2 核心流程

两个适配器的构造与使用流程：

```text
UnifiedAuthFactory.create_model_auth("oci", auth_type=...)
   └─ AuthFactory.create_oci_auth(auth_type=...)  → 一个 AuthProvider（如 OCIUserPrincipalAuth）
   └─ OCIModelAuthAdapter(oci_auth)               → ModelAuthProvider
        用法：get_credentials() 返回 signer（给模型客户端签名）；get_headers() 返回 {}

UnifiedAuthFactory.create_storage_auth("oci", auth_type=...)
   └─ AuthFactory.create_oci_auth(auth_type=...)  → 同一个 AuthProvider
   └─ OCIStorageAuthAdapter(oci_auth)             → StorageAuthProvider
        用法：get_credentials() 返回整个 oci_auth（给 OSDataStore 同时取 config+signer）
```

关键观察：**两个工厂方法调的是同一个 `create_oci_auth`，拿到同一种 `AuthProvider`，只是最后套的适配器不同**。这也是「一份凭据，两种视图」在代码层面的落点（见 [unified_factory.py:51-60](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/unified_factory.py#L51-L60) 与 [unified_factory.py:115-124](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/unified_factory.py#L115-L124)）。

#### 4.2.3 源码精读

**① OCIModelAuthAdapter——「签名器视图」。**

- [model_auth_adapter.py:12-18](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/model_auth_adapter.py#L12-L18) 构造时把 `AuthProvider` 存为 `self.oci_auth`。
- [model_auth_adapter.py:20-26](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/model_auth_adapter.py#L20-L26) `get_headers()` 返回 `{}`——这是 OCI 用签名器而非请求头鉴权的直接体现，与 OpenAI 那种 `Authorization: Bearer xxx` 形成鲜明对比。
- [model_auth_adapter.py:64-72](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/model_auth_adapter.py#L64-L72) `get_credentials()` 直接转发 `self.oci_auth.get_credentials()`——把**签名器**交给模型客户端。
- [model_auth_adapter.py:45-62](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/model_auth_adapter.py#L45-L62) `get_auth_type()` 用**类名嗅探**（class name sniffing）反推 auth 类型：检查 `self.oci_auth.__class__.__name__` 里是否含 `InstancePrincipal`/`UserPrincipal`/`OBOToken`/`Session`，分别映射成 `oci_instance_principal`/`oci_user_principal`/`oci_obo_token`/`oci_security_token`，否则 `oci_unknown`。这是 u5-l2 提到的「以类名嗅探推断 OCI 认证子类型」。
- [model_auth_adapter.py:28-43](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/model_auth_adapter.py#L28-L43) `get_config()` 拼一个可序列化的字典：先放 `{"auth_type": self.get_auth_type()}`，再 `update` 底层 provider 的 config（如果有 `get_config`）。注意它产出的内容会被写入 `ExperimentMetadata.auth_config`，所以必须可序列化。

> 为什么用「类名嗅探」这种略显 hacky 的写法？因为适配器构造时拿到的参数类型是 `AuthProvider`（基类），它**不知道**具体被传进来的是 `OCIUserPrincipalAuth` 还是 `OCIOBOTokenAuth`，又没有在构造时显式记录 `auth_type`，于是只能在运行时靠类名反推。这算一个可改进点——更稳的做法是在构造时把 `auth_type` 作为参数显式传进来。

**② OCIStorageAuthAdapter——「完整 provider 视图」。**

- [storage_auth_adapter.py:12-18](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/storage_auth_adapter.py#L12-L18) 同样持有 `self.oci_auth`。
- [storage_auth_adapter.py:20-28](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/storage_auth_adapter.py#L20-L28) `get_client_config()` 返回 `{"auth_provider": self.oci_auth}`——把整个 provider 塞进配置。
- [storage_auth_adapter.py:30-36](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/storage_auth_adapter.py#L30-L36) `get_credentials()` 返回 `self.oci_auth`（**整个 provider，不是签名器**）——这正是存储侧的关键差异。
- [storage_auth_adapter.py:38-44](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/storage_auth_adapter.py#L38-L44) `get_storage_type()` 恒返回 `"oci"`，供 `StorageFactory` 做 provider-auth 一致性校验（见 u5-l3）。
- [storage_auth_adapter.py:46-55](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/storage_auth_adapter.py#L46-L55) `get_region()` 尝试从底层 provider 的 `.region` 属性取 region；但注意，只有 `OCIInstancePrincipalAuth` 和 `OCIOBOTokenAuth` 在构造时存了 `region`，`OCIUserPrincipalAuth` / `OCISessionAuth` 没有这个属性——对它们 `hasattr` 为假，返回 `None`。这也是为什么 [oci_storage.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_storage.py) 里取 region 时到处有 `or None` 的兜底。

把两个适配器并排放，那张「不对称」表就清楚了：

| 方法 | `OCIModelAuthAdapter` | `OCIStorageAuthAdapter` |
| --- | --- | --- |
| 实现的接口 | `ModelAuthProvider` | `StorageAuthProvider` |
| `get_headers()` | `{}`（OCI 用签名器） | （无此方法） |
| `get_credentials()` 返回 | **signer**（签名器） | **整个 provider** |
| `get_auth_type()` | 类名嗅探 | （无，改为 `get_storage_type()` 返 `"oci"`） |
| `get_region()` | （无） | 从 `oci_auth.region` 取，可能为 `None` |

#### 4.2.4 代码实践

**实践目标：** 不依赖 OCI SDK、不联网，用「假 provider」直接观察两个适配器「一份凭据两种视图」的不对称输出。

> 说明：以下为**示例代码**，非项目原有代码。它之所以能脱离 OCI SDK 运行，是因为 `OCIModelAuthAdapter` 与 `OCIStorageAuthAdapter` 自身只 `import` 了 `AuthProvider` / `ModelAuthProvider` / `StorageAuthProvider` 这几个抽象基类，并没有 `import oci`（参见 [model_auth_adapter.py:1-6](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/model_auth_adapter.py#L1-L6)、[storage_auth_adapter.py:1-6](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/storage_auth_adapter.py#L1-L6)）。

**操作步骤：**

1. 把下面的脚本存为 `oci_adapter_demo.py`（任意目录）。
2. 在已 `pip install genai-bench` 的环境里运行 `python oci_adapter_demo.py`。

```python
# 示例代码：用假 provider 观察 OCI 两个适配器的「不对称」输出
from genai_bench.auth.auth_provider import AuthProvider
from genai_bench.auth.oci.model_auth_adapter import OCIModelAuthAdapter
from genai_bench.auth.oci.storage_auth_adapter import OCIStorageAuthAdapter


class FakeUserPrincipal(AuthProvider):
    """模拟 OCIUserPrincipalAuth：不依赖 oci SDK。"""

    def __init__(self):
        self.region = None  # 故意不设 region，观察 get_region() 兜底

    def get_config(self):
        return {"region": "us-ashburn-1", "tenancy": "ocid1.tenancy.fake"}

    def get_credentials(self):
        return "<<<SIGNER-STUB>>>"  # 真实场景这里是 oci 签名器对象


class FakeInstancePrincipal(AuthProvider):
    """模拟 OCIInstancePrincipalAuth：类名里含 'InstancePrincipal'。"""

    def get_config(self):
        return {"region": "us-chicago-1", "tenancy": "ocid1.tenancy.fake"}

    def get_credentials(self):
        return "<<<INSTANCE-SIGNER-STUB>>>"


base = FakeUserPrincipal()
model_view = OCIModelAuthAdapter(base)
storage_view = OCIStorageAuthAdapter(base)

print("=== 模型视图 OCIModelAuthAdapter ===")
print("get_headers   :", model_view.get_headers())          # 预期 {}
print("get_auth_type :", model_view.get_auth_type())        # 预期 oci_user_principal
print("get_credentials 返回的是 signer:",
      model_view.get_credentials() == base.get_credentials())

print("\n=== 存储视图 OCIStorageAuthAdapter ===")
print("get_storage_type :", storage_view.get_storage_type())  # 预期 oci
print("get_credentials 返回的是整个 provider:",
      storage_view.get_credentials() is base)                # 预期 True
print("get_region       :", storage_view.get_region())        # 预期 None（FakeUserPrincipal.region=None）

print("\n=== 类名嗅探对照 ===")
print("FakeUserPrincipal    ->", OCIModelAuthAdapter(FakeUserPrincipal()).get_auth_type())
print("FakeInstancePrincipal->", OCIModelAuthAdapter(FakeInstancePrincipal()).get_auth_type())
```

**需要观察的现象：**

- 模型视图的 `get_headers()` 是 `{}`、`get_credentials()` 返回签名器桩；
- 存储视图的 `get_credentials()` 返回的 `is base` 为 `True`（整个 provider），`get_region()` 为 `None`；
- 类名嗅探能把 `FakeUserPrincipal` 映射成 `oci_user_principal`、把 `FakeInstancePrincipal` 映射成 `oci_instance_principal`。

**预期结果：** 一次运行就直观看到「同一份 `base` 凭据，经两个适配器分别暴露出 signer 视图与 provider 视图」，以及类名嗅探的工作方式。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 `OCIModelAuthAdapter.get_credentials()` 返回签名器，而 `OCIStorageAuthAdapter.get_credentials()` 返回整个 provider？

**参考答案：** 模型端点（如 OCI GenAI/Cohere）只需要对每个 HTTP 请求签名，签名器就够了，所以模型适配器转发 `oci_auth.get_credentials()`（签名器）（[model_auth_adapter.py:64-72](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/model_auth_adapter.py#L64-L72)）。而 `OSDataStore` 构造 `ObjectStorageClient` 时需要 `config=` 与 `signer=` 两个入参（见 4.3 节），即 config 和签名器**都要**，所以存储适配器干脆把整个 provider 交出去（[storage_auth_adapter.py:30-36](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/storage_auth_adapter.py#L30-L36)），让下游自己各取所需。

**练习 2：** 如果把一个 `OCIOBOTokenAuth` 实例包进 `OCIModelAuthAdapter`，`get_auth_type()` 会返回什么？为什么这对写入 `ExperimentMetadata.auth_config` 有意义？

**参考答案：** 返回 `"oci_obo_token"`（[model_auth_adapter.py:57-58](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/model_auth_adapter.py#L57-L58)，类名含 `OBOToken`）。意义在于：实验元数据会记录「这次压测用了哪种 OCI 认证」，事后排查或复现实验时能知道当时用的是哪种凭据来源，而不必去翻底层 provider 对象。

---

### 4.3 对象存储 datastore（三层封装）

#### 4.3.1 概念说明

u5-l3 已经画过 `BaseStorage` 这个多云统一契约，并指出 OCI 的实现「多一层」。本节就把这「多出来的一层」彻底拆开。OCI 对象存储在 genai-bench 里被组织成**自上而下三层**：

| 层 | 类 | 职责 | 依赖 |
| --- | --- | --- | --- |
| 第一层（公共契约） | `OCIObjectStorage`（继承 `BaseStorage`） | 对外暴露 `upload_file` / `upload_folder` / `download_file` / `list_objects` / `delete_object`，把「bucket + 路径」翻译成 `ObjectURI`，再委派下层 | `OSDataStore`、`ObjectURI` |
| 第二层（SDK 封装） | `OSDataStore`（继承 `DataStore`） | 真正持有 OCI SDK 的 `ObjectStorageClient`，处理 namespace 查询、单/多分片上传、流式下载、列举、删除 | `oci` SDK |
| 第三层（地址模型） | `ObjectURI`（Pydantic `BaseModel`） | 表示并解析/序列化 `oci://n/{namespace}/b/{bucket}/o/{object}` 这个地址 | 仅 `pydantic` |

为什么要分三层？这是典型的「**关注点分离 + 复用成熟实现**」：

- 第一层让 OCI 融入 u5-l3 的多云抽象，上层（CLI、上传链路）只认 `BaseStorage`。
- 第二层把「和 OCI SDK 打交道」的所有脏活（namespace、分片、重试）隔离起来，`OSDataStore` 看起来像是一个独立的、可单独测试的数据存储客户端（docstring 里甚至还有历史遗留的 "Casper client" 字样，说明它复用自更早的成熟实现）。
- 第三层用一个纯数据模型统一「对象地址」的表达与解析，避免到处手拼字符串。

#### 4.3.2 核心流程

一次「上传整个实验目录」的调用，在三层之间的流转是：

```text
BaseStorage.upload_folder(local_folder, bucket, prefix)        # 多云统一入口（u5-l3）
   │  实现者：OCIObjectStorage.upload_folder
   ▼
对每个文件 file_path：
   1. 算出相对路径 relative_path，拼 remote_path = prefix/relative_path
   2. 调自己的 upload_file(file_path, remote_path, bucket)
        │
        ▼  OCIObjectStorage.upload_file
   3. 构造 ObjectURI(namespace, bucket, remote_path, region)
   4. self.datastore.upload(str(local_path), target_uri)
        │
        ▼  OSDataStore.upload
   5. 若 namespace 缺失 → get_namespace() 向服务查
   6. 看 file_size：> 128MB → multipart_upload；否则单次 put_object
        │
        ▼  ObjectStorageClient（oci SDK）
   7. 真正发起 HTTP 请求，由注入的 signer 签名
```

构造阶段同样关键——三层是怎么被串起来的：

```text
StorageFactory.create_storage("oci", auth=OCIStorageAuthAdapter, namespace=...)
   └─ OCIObjectStorage(auth, namespace=...)                       # 第一层
        ├─ oci_auth = auth.get_credentials()   # = 整个 AuthProvider（见 4.2）
        ├─ self.datastore = OSDataStore(oci_auth)                 # 第二层
        │     ├─ self.config  = oci_auth.get_config()
        │     └─ self.client  = ObjectStorageClient(config=..., signer=oci_auth.get_credentials())
        └─ self.namespace = kwargs["namespace"] 或 datastore.get_namespace()
```

这里能清楚地看到 4.2 节那个「存储适配器返回整个 provider」的原因：`OSDataStore.__init__` 要对它**同时**调 `get_config()` 与 `get_credentials()`（[os_datastore.py:21-31](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_object_storage/os_datastore.py#L21-L31)），少了任一都不行。

#### 4.3.3 源码精读

**① 第一层 OCIObjectStorage——契约翻译器。**

- [oci_storage.py:15-37](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_storage.py#L15-L37) 构造：先 `auth.get_storage_type() != "oci"` 把关（与 u5-l3 的双层校验呼应），再 `auth.get_credentials()` 取出整个 provider，`OSDataStore(oci_auth)` 造第二层；namespace 优先用 `kwargs` 传入的，取不到就调 `datastore.get_namespace()` 向服务查。**namespace 是 OCI 对象存储寻址的必需项**，所以这里必须有兜底。
- [oci_storage.py:39-65](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_storage.py#L39-L65) `upload_file`：校验本地文件存在 → 构造 `ObjectURI(namespace, bucket, remote_path, region=auth.get_region() or None)` → `datastore.upload(...)`。注意 region 来自 `auth.get_region()`，而 4.2 节指出它可能为 `None`，所以处处 `or None` 兜底。
- [oci_storage.py:67-97](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_storage.py#L67-L97) `upload_folder`：用 `local_folder.rglob("*")` 递归遍历，对每个文件算 `relative_path`，按是否传 `prefix` 决定 `remote_path` 的拼法，逐个调 `upload_file`。下载、列举、删除方法（[oci_storage.py:99-173](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_storage.py#L99-L173)）套路一致：都先构造 `ObjectURI`，再委派 `datastore` 的对应方法。

**② 第二层 OSDataStore——SDK 封装与分片逻辑。**

- [os_datastore.py:21-31](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_object_storage/os_datastore.py#L21-L31) 构造：`self.config = auth.get_config()`、`self.client = ObjectStorageClient(config=self.config, signer=auth.get_credentials())`——**config 与 signer 双入参**，这正是存储适配器必须暴露整个 provider 的根因。
- [os_datastore.py:123-130](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_object_storage/os_datastore.py#L123-L130) `get_namespace()`：`client.get_namespace()` 向服务查询当前租户的 namespace。这是「namespace 寻址」的服务端入口。
- [os_datastore.py:132-161](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_object_storage/os_datastore.py#L132-L161) `upload`：核心是 128MB 分水岭——`file_size > 128 * MB` 走 `multipart_upload`，否则 `open(source,"rb")` 后 `put_object`。`MB = 1024*1024` 定义在 [os_datastore.py:14](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_object_storage/os_datastore.py#L14)。
- [os_datastore.py:163-196](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_object_storage/os_datastore.py#L163-L196) `multipart_upload`：用 `UploadManager(self.client, allow_multipart_uploads=True)`，默认 `part_size=128*MB`、`parallel_process_count=3` 并发上传分片，最后检查 `response.status != 200` 抛错。分片数约为：

  \[ \text{part\_count} = \left\lceil \frac{\text{file\_size}}{\text{part\_size}} \right\rceil = \left\lceil \frac{\text{file\_size}}{128\,\text{MB}} \right\rceil \]

- [os_datastore.py:43-64](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_object_storage/os_datastore.py#L43-L64) `download`：namespace 缺失则补查，调 `get_object` 后用 `response.data.raw.stream(1024*1024, decode_content=False)` **按 1MB 分块流式写盘**，避免大对象撑爆内存。
- [datastore.py:9-44](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_object_storage/datastore.py#L9-L44) `DataStore` 抽象基类：只定义 `download(source, target, retries=3)` 与 `upload(source, target, retries=3)` 两个抽象方法，给 `OSDataStore` 提供契约。注意它定义的 `retries` 参数在当前 `OSDataStore` 实现里**并未真正使用**（方法签名带了这个参数但函数体没消费），属于「为未来留口子」。

**③ 第三层 ObjectURI——地址模型与解析。**

- [object_uri.py:9-20](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_object_storage/object_uri.py#L9-L20) 用 Pydantic 定义五个字段：`namespace`、`bucket_name`、`object_name`、`region`、`prefix`。对应的 URI 格式为 `oci://n/{namespace}/b/{bucket}/o/{object_path}`。
- [object_uri.py:21-73](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_object_storage/object_uri.py#L21-L73) `from_uri(cls, uri)` 类方法做**手写解析**：先要求 `oci://` 前缀，再依次剥 `n/`（namespace）、`b/`（bucket）、`o/`（object，可选）三段，任一段缺失都抛 `ValueError`；object 段还会顺便算出 `prefix`（`os.path.dirname` + 尾部 `/`）。注意 `region` 不在 URI 里（`region=None`），因为 region 是客户端/认证侧的属性，不属于对象地址。
- [object_uri.py:75-84](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_object_storage/object_uri.py#L75-L84) `__str__` 是 `from_uri` 的逆运算，把模型重新拼回字符串形式，便于日志输出。

> 小提示：`ObjectURI` 只依赖 `pydantic`，不依赖 `oci` SDK、不联网——这使它成为本讲最适合「真跑」的实践对象（见 4.3.4）。

#### 4.3.4 代码实践

**实践目标：** 用纯 Pydantic 的 `ObjectURI` 验证 `oci://` 地址的解析与序列化是互逆的，并对照 `OSDataStore.upload` 的 128MB 阈值算出一个大文件的分片数。

> 说明：以下为**示例代码**，非项目原有代码。`ObjectURI` 只需 `pydantic`，无需 OCI SDK、无需联网，可直接运行。

**操作步骤：**

1. 把下面的脚本存为 `oci_uri_demo.py`。
2. 在已 `pip install genai-bench` 的环境里运行 `python oci_uri_demo.py`。

```python
# 示例代码：验证 ObjectURI 解析/序列化互逆，并演示 128MB 分片阈值
import math
from genai_bench.storage.oci_object_storage.object_uri import ObjectURI

MB = 1024 * 1024
PART_SIZE = 128 * MB

uri_str = "oci://n/ax1234567b/b/my-bucket/o/exp/run1/summary.json"
obj = ObjectURI.from_uri(uri_str)

print("解析结果:")
print("  namespace  :", obj.namespace)   # ax1234567b
print("  bucket_name:", obj.bucket_name) # my-bucket
print("  object_name:", obj.object_name) # exp/run1/summary.json
print("  prefix     :", obj.prefix)      # exp/run1/
print("  region     :", obj.region)      # None（region 不在 URI 里）
print("序列化回字符串:", str(obj))        # 应与 uri_str 一致（无尾斜杠差异）

# 对照 OSDataStore.upload 的 128MB 阈值，估算分片数
for size_mb in (1, 128, 129, 500, 1024):
    size = size_mb * MB
    if size > 128 * MB:
        parts = math.ceil(size / PART_SIZE)
        mode = f"multipart ({parts} 片)"
    else:
        mode = "single-part"
    print(f"{size_mb:>5} MB -> {mode}")

# 故意构造一个非法 URI，观察解析报错
try:
    ObjectURI.from_uri("oci://my-bucket/summary.json")  # 缺 n/ b/ 段
except ValueError as e:
    print("\n非法 URI 报错:", e)
```

**需要观察的现象：**

- `from_uri` 正确拆出 namespace/bucket/object/prefix，且 `str(obj)` 与原 URI 一致；
- `region` 恒为 `None`（地址里不含 region）；
- 1MB 与 128MB 走单次上传，129MB 起走分片（129MB→2 片，500MB→4 片，1024MB→8 片）；
- 缺 `n/`、`b/` 段的 URI 会抛 `ValueError`。

**预期结果：** 你应能用一句话解释「为什么 129MB 就开始分片」——因为 [os_datastore.py:150](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_object_storage/os_datastore.py#L150) 的判断是严格 `> 128 * MB`，128MB 本身仍是单次上传。

> 待本地验证：`part_size` 与 `parallel_process_count` 的默认值见 [os_datastore.py:163-184](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_object_storage/os_datastore.py#L163-L184)；若想确认真实分片行为，需有 OCI 对象存储环境上传一个 >128MB 文件并观察日志（会打印 "Using multipart upload ..."）。

#### 4.3.5 小练习与答案

**练习 1：** `OCIObjectStorage.__init__` 里 `self.namespace` 的取值逻辑是什么？为什么需要 `get_namespace()` 兜底？

**参考答案：** 优先用 `kwargs.get("namespace")`（即 CLI/调用方显式传入），取不到才调 `self.datastore.get_namespace()` 向服务查询（[oci_storage.py:34-37](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_storage.py#L34-L37)）。需要兜底是因为 OCI 对象存储的每个对象地址都必须含 namespace（见 `ObjectURI`），而用户未必知道自己的 namespace 字符串，于是代码提供一个「问服务」的后备手段。

**练习 2：** 三层封装里，为什么把 `ObjectStorageClient` 放在 `OSDataStore` 而不是 `OCIObjectStorage`？

**参考答案：** 分层隔离关注点。`OCIObjectStorage` 的职责是「把多云契约（bucket + 路径）翻译成 OCI 的地址表达（`ObjectURI`）」，不该直接懂 SDK；`OSDataStore` 才是「懂 OCI SDK」的那一层，持有 `ObjectStorageClient` 并处理 namespace、分片、流式下载等 SDK 细节。这样 `OSDataStore` 可以被独立测试与复用（它继承自通用的 `DataStore`），而 `OCIObjectStorage` 只关心如何融入 `BaseStorage` 多云体系。

**练习 3：** `DataStore` 抽象基类定义了 `retries=3` 参数，但 `OSDataStore` 的方法体没用它。这是 bug 吗？

**参考答案：** 不是功能 bug（当前行为是「不重试，失败即抛」），更像是「为契约留口子」——抽象基类把 `retries` 写进签名，方便未来在 `OSDataStore` 里补上重试逻辑而不破坏调用方。读源码时应能区分「签名承诺的能力」与「当前是否真正实现」，避免误以为已经有重试。

## 5. 综合实践

**综合任务：把「认证 → 适配器 → 三层存储」串起来，画一张完整的 OCI 数据流图，并用一个脱离 OCI SDK 的脚本验证关键不变量。**

1. **画图阶段**：以本讲三节的流程为依据，画一张从 CLI 到 `ObjectStorageClient` 的完整时序图，至少包含这些节点与箭头，并标注每个节点对应的源码行号：
   - CLI 传入 `--auth <auth_type>` → `UnifiedAuthFactory.create_storage_auth("oci", ...)`（[unified_factory.py:115-124](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/unified_factory.py#L115-L124)）；
   - `AuthFactory.create_oci_auth(auth_type=...)` 路由到四个 `AuthProvider` 子类之一（[factory.py:83-97](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/factory.py#L83-L97)）；
   - `OCIStorageAuthAdapter(oci_auth)` 包成 `StorageAuthProvider`（[storage_auth_adapter.py:12-18](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/storage_auth_adapter.py#L12-L18)）；
   - `StorageFactory.create_storage("oci", auth, namespace=...)` → `OCIObjectStorage`（[oci_storage.py:15-37](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_storage.py#L15-L37)）；
   - `OSDataStore(oci_auth)` 用 `get_config()` + `get_credentials()` 造 `ObjectStorageClient`（[os_datastore.py:21-31](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_object_storage/os_datastore.py#L21-L31)）；
   - `upload_folder` → 逐文件构造 `ObjectURI` → `OSDataStore.upload` → 按 128MB 选单/多分片（[os_datastore.py:132-161](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_object_storage/os_datastore.py#L132-L161)）。
   在 `OCIStorageAuthAdapter.get_credentials()` 这一步旁批注「返回整个 provider，而非 signer」，并解释为什么。

2. **验证阶段**：合并 4.2.4 与 4.3.4 的两个示例脚本，证明以下不变量同时成立（全部无需 OCI SDK、无需联网）：
   - 用一个假 `AuthProvider` 同时构造 `OCIModelAuthAdapter` 与 `OCIStorageAuthAdapter`，断言它们 `get_credentials()` 一个返回 signer 桩、一个返回 provider 本身（`is base` 为真）；
   - 用 `ObjectURI.from_uri` 解析一个 `oci://` 地址，再 `str(...)` 回去，断言往返一致；
   - 给定 `auth_type` 列表 `["user_principal","instance_principal","security_token","instance_obo_user"]`，逐个喂给一个**你自己重写的**简化路由函数（模仿 [factory.py:71-97](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/factory.py#L71-L97) 的白名单 + 分发逻辑，但用假类替换真 OCI 类），断言四个都能被路由、一个非法值会被拒。

3. **反思**：用一段话回答——为什么 OCI 要在「多云统一契约」和「OCI SDK」之间多塞一层 `OSDataStore`？如果直接让 `OCIObjectStorage` 调 `ObjectStorageClient`，会牺牲什么？提示从「复用既有实现、namespace/分片复杂度、可测试性、与 `DataStore` 抽象的关系」四个角度组织。

> 完成本任务后，你应能不看源码，向别人讲清「我加了 `--storage-provider oci --storage-bucket xxx --auth instance_principal --region ...` 之后，代码内部从认证到把一个实验目录传上 OCI 对象存储，依次发生了什么」。

## 6. 本讲小结

- OCI 有四种认证方式，`auth_type` 白名单的权威来源是 [factory.py:71-81](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/factory.py#L71-L81)：`user_principal`（配置文件+API Key）、`instance_principal`（实例元数据）、`security_token`（私钥+令牌文件）、`instance_obo_user`（OBO token+region）；四者都实现 `AuthProvider` 的 `get_config`/`get_credentials`，且都惰性缓存签名器与配置。
- OCI 用**签名器（signer）**而非请求头 Bearer token 鉴权，所以 `get_credentials()` 返回的是签名器对象；这直接导致 `OCIModelAuthAdapter.get_headers()` 返回 `{}`（[model_auth_adapter.py:20-26](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/model_auth_adapter.py#L20-L26)）。
- 「一份凭据，两种视图」：同一个 `AuthProvider` 被 `OCIModelAuthAdapter` 与 `OCIStorageAuthAdapter` 分别包装；前者 `get_credentials()` 返回**签名器**（模型端点够用），后者返回**整个 provider**（因为 `OSDataStore` 构造客户端要 config + signer 两个入参）。
- `OCIModelAuthAdapter.get_auth_type()` 用**类名嗅探**反推 auth 类型（[model_auth_adapter.py:45-62](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/oci/model_auth_adapter.py#L45-L62)），其产物会进 `ExperimentMetadata.auth_config`；这是一种务实但可改进的写法。
- OCI 对象存储是**三层封装**：`OCIObjectStorage`（实现 `BaseStorage`，做地址翻译）→ `OSDataStore`（封装 `ObjectStorageClient`，处理 namespace/分片/流式下载）→ `ObjectURI`（Pydantic 地址模型，解析 `oci://n/.../b/.../o/...`）。
- 关键数值与细节：namespace 是 OCI 寻址必需项、缺失时 `get_namespace()` 向服务查询；上传 128MB 阈值（[os_datastore.py:150](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_object_storage/os_datastore.py#L150)）以上走 `UploadManager` 并发分片；`oci` SDK 是核心依赖而非可选依赖（[pyproject.toml:33](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/pyproject.toml#L33)）。

## 7. 下一步学习建议

本讲把 OCI 这条「认证 + 对象存储」专线讲透了，至此 U5（多云认证与存储）单元完整收尾。建议接下来：

- **横向对照其他云的认证实现**：回到 u5-l2（模型认证 Provider）与 u5-l3（存储认证），把 OCI 的「签名器 + 适配器」与 AWS Bedrock 的 SigV4（同样签名、`get_headers` 返空）、Azure/GCP 的「头 + SDK 双模式」做一张对比表，加深对「为什么 OCI 需要两个适配器而 OpenAI 只要一个」的理解。
- **顺数据流向下**：OCI 存储是结果上传链路的终点。可进入 U6（实验分析与报告），看上传之前的产物——`experiment_metadata.json` 与各 run JSON——是如何被 `experiment_loader` 读回、组织成 scenario×concurrency 结构并生成 Excel/绘图的。
- **若你想动手扩展**：以本讲练习 3 的思路，尝试新增一种 OCI 认证（如 resource principal），体会「新加 auth_type 要改白名单 + 分发分支 + 类名嗅探」三处注册点，为 U8 的扩展指南（u8-l3）做铺垫。
