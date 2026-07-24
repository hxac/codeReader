# 存储认证与多云存储

## 1. 本讲目标

在上一讲（u5-l2）里，我们读懂了**模型认证**那一侧：怎么给"调 LLM 接口"这件事配好 HTTP 头与凭据。本讲转到对称的**存储侧**——压测跑完后，那一堆实验结果（JSON、Excel、PNG）该往哪里存、怎么存、用什么身份去存。

学完本讲，你应当能够：

- 说清 `BaseStorage` 这套抽象接口定义了哪几类操作，以及为什么所有云厂商都要实现它；
- 解释 `StorageFactory.create_storage` 做的两层校验（provider 与 auth 类型必须一致），以及它为什么对每个厂商 SDK 用**惰性导入（lazy import）**；
- 描绘一条完整的**结果上传链路**：从 `--upload-results` 这个 CLI 开关，经认证工厂、存储工厂，最终落到 `upload_folder` 把整个实验目录搬上云端；
- 区分 OCI 存储实现里独有的额外抽象层（`OSDataStore` / `ObjectURI`），理解它为什么比其他厂商多一层封装。

本讲承接 u5-l1（认证体系总览）建立的 `StorageAuthProvider` 接口认知，是认证—存储这条横切云能力的下半段。

## 2. 前置知识

在进入源码前，先用通俗语言铺垫三个概念。

**对象存储（Object Storage）。** 与"文件系统"里那种有目录层级的存储不同，对象存储把所有数据都看作扁平的"对象（object）"。每个对象有三个要素：**桶（bucket / container）**——相当于一个顶层容器；**键（key / object name）**——对象在桶里的名字，常含 `/` 但那只是普通字符，并不真的是文件夹；**内容**——二进制数据本身。AWS S3、Azure Blob、GCP Cloud Storage、OCI Object Storage 都是这个模型。本讲里你看到的 `bucket`、`remote_path`、`prefix`，都是这个模型的产物。

**横向关注点（cross-cutting concern）。** "存结果"这件事和"发请求压测"在业务上是两条正交的线：你可能用 OpenAI 压测，却把结果传到 AWS S3；也可能用 OCI GenAI 压测，结果存回 OCI Object Storage。项目因此把存储做成一个**可独立替换的子系统**，模型认证与存储认证互不绑定。文档里把这点总结为："benchmark models from one provider while storing results in another"（[multi-cloud-auth-storage.md:26-33](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/docs/user-guide/multi-cloud-auth-storage.md#L26-L33)）。

**抽象基类（ABC）与工厂（Factory）。** 这两个是面向对象里的经典套路。ABC 用 `@abstractmethod` 规定"任何存储实现都必须会这几招"，但不关心具体怎么做；工厂则负责根据一个字符串（如 `"aws"`）挑出正确的实现类并实例化。上一讲你已经见过 `UnifiedAuthFactory`，本讲的 `StorageFactory` 是同款思路。

## 3. 本讲源码地图

本讲涉及的文件按"由抽象到具体、由内部到调用方"排列：

| 文件 | 作用 |
|------|------|
| `genai_bench/storage/base.py` | `BaseStorage` 抽象基类，定义 upload/download/list/delete 等统一接口。 |
| `genai_bench/auth/storage_auth_provider.py` | `StorageAuthProvider` 抽象基类，定义存储侧认证契约（u5-l1 已介绍，本讲复用）。 |
| `genai_bench/storage/factory.py` | `StorageFactory`，按 provider 字符串分发并做 provider-auth 一致性校验。 |
| `genai_bench/storage/oci_storage.py` | `OCIObjectStorage`，OCI 实现，内部委托给更深一层的 `OSDataStore`。 |
| `genai_bench/storage/aws_storage.py` | `AWSS3Storage`，AWS S3 实现，用于横向对比各厂商实现风格。 |
| `genai_bench/storage/oci_object_storage/` | OCI 专属的更底层封装：`ObjectURI`（URI 数据类）、`OSDataStore`（真正调 OCI SDK）、`DataStore`（抽象）。 |
| `genai_bench/cli/option_groups.py` | `storage_auth_options` / `object_storage_options` 两个选项组，定义所有 `--storage-*` 与 `--upload-results` 参数。 |
| `genai_bench/cli/validation.py` | `validate_object_storage_options`，校验"要上传就必须给 bucket"。 |
| `genai_bench/cli/cli.py` | `benchmark` 函数末尾的上传段落，把上面所有零件串成一条链路。 |

## 4. 核心概念与源码讲解

本讲拆三个最小模块：**4.1** 讲统一接口 `BaseStorage`；**4.2** 讲分发与校验中枢 `StorageFactory`；**4.3** 讲从 CLI 到云端的完整上传链路，并把 OCI 实现作为深读样本。

### 4.1 BaseStorage 接口

#### 4.1.1 概念说明

多云存储最大的痛点是：每个云厂商的 SDK 都不一样——AWS 用 `boto3`，Azure 用 `azure-storage-blob`，GCP 用 `google-cloud-storage`，OCI 用 `oci`。如果上层代码（比如 CLI）直接调某一家 SDK，就会被彻底绑死在那家云上，换云等于重写。

`BaseStorage` 解决的就是这个问题。它声明一套**所有云都应满足的最小操作集**：上传单文件、上传整个文件夹、下载、列举、删除，外加一个"你是哪家云"的自报家门方法。上层只面向 `BaseStorage` 编程，至于底下是 S3 还是 Blob，由具体实现类决定。这就是"依赖抽象，不依赖具体"。

与它成对出现的是 `StorageAuthProvider`——**接口也分两套**：`BaseStorage` 管"怎么传数据"，`StorageAuthProvider` 管"用什么身份传"。一个存储实例在构造时必须吃进一个 auth provider，这把"操作能力"和"认证能力"也解耦了。

#### 4.1.2 核心流程

一个 `BaseStorage` 子类的生命周期可以概括为：

```text
构造阶段：  auth provider  ──►  __init__(auth, **kwargs)  ──►  持有 auth，按需建立云客户端
使用阶段：  上层调用 upload_folder / upload_file / download_file / list_objects / delete_object
自省：     get_storage_type() 返回 provider 字符串
```

六个抽象方法可分为三组：

| 分组 | 方法 | 用途 |
|------|------|------|
| 写 | `upload_file`、`upload_folder` | 把本地数据搬到云端 |
| 读 | `download_file`、`list_objects` | 把云端数据拉回本地或列举 |
| 删 / 自省 | `delete_object`、`get_storage_type` | 删除对象；报告 provider 类型 |

注意 `list_objects` 返回的是**生成器（Generator）**而非列表，这对桶里可能有海量对象的场景更省内存。

#### 4.1.3 源码精读

`BaseStorage` 用 `abc.ABC` 配合 `@abstractmethod` 强制契约，六个方法全抽象，基类自身不能实例化：

[base.py:8-9](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/base.py#L8-L9) 定义基类与文档字符串。

本讲的重头戏是 `upload_folder`——因为压测结果是"一整个目录"而非单个文件，CLI 最终调的就是它：

[base.py:25-37](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/base.py#L25-L37) 声明 `upload_folder` 抽象方法：吃本地目录、桶名、可选 `prefix`（上传后所有对象的公共前缀），以及厂商专属的 `**kwargs`。

`get_storage_type` 看似简单，却是下游工厂做一致性校验的关键依据：

[base.py:80-87](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/base.py#L80-L87) 返回 provider 类型字符串（如 `'aws'`、`'oci'`）。

与它对称的 auth 契约里，也有一个同名的 `get_storage_type`，外加一个带默认实现的 `get_region`：

[storage_auth_provider.py:28-43](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/auth/storage_auth_provider.py#L28-L43) `get_storage_type` 是抽象方法（必须实现），`get_region` 默认返回 `None`（OCI 等需要 region 的厂商才覆写）。

> 设计要点：**两侧同名方法**。存储实现类与认证 provider 都各自声明 `get_storage_type()`，各自返回自己那一侧的身份。4.2 里你会看到，工厂正是拿这两个返回值做比对，防止"用 AWS 的桶却传了 OCI 的认证"这类错配。

#### 4.1.4 代码实践

**实践目标：** 用一个最小的 mock 实现，亲手验证 ABC 的强制力，并体会"面向 BaseStorage 编程"。

**操作步骤：**

1. 新建一个临时脚本（不入库），写一个故意漏掉抽象方法的假存储类。
2. 尝试实例化它，观察报错。

```python
# 示例代码：仅用于演示 ABC 强制力，非项目原有代码
from genai_bench.storage.base import BaseStorage

class BrokenStorage(BaseStorage):
    """故意只实现一个方法，其余抽象方法缺失。"""
    def get_storage_type(self) -> str:
        return "broken"

try:
    BrokenStorage()
except TypeError as e:
    print("实例化被拦截：", e)
```

**需要观察的现象：** Python 会在 `BrokenStorage()` 处抛 `TypeError`，提示还有未实现的抽象方法（如 `upload_file`、`upload_folder` 等）。

**预期结果：** 这正说明 `BaseStorage` 是真抽象基类——只要少实现一个 `@abstractmethod`，类就无法实例化。补全全部六个方法后才能创建对象。

> 待本地验证：具体 `TypeError` 文案在不同 Python 小版本上略有差异，但"拦截实例化"的行为稳定。

#### 4.1.5 小练习与答案

**练习 1：** `list_objects` 为什么设计成生成器（`yield`）而不是返回一个 `list`？

**参考答案：** 桶里可能有成千上万甚至上百万个对象。若一次性返回列表，会把所有对象名都载入内存；用生成器则逐个产出、边迭代边消费，内存占用与对象总数无关。下游拿到生成器仍可用 `for name in storage.list_objects(...)` 遍历，使用方式不变。

**练习 2：** 为什么 `BaseStorage` 和 `StorageAuthProvider` 是两套独立接口，而不是合成一个"既管认证又管操作"的大类？

**参考答案：** 为了单一职责与可组合。认证（用什么身份）与操作（怎么传数据）是两个正交的维度：同一份 OCI 凭据，可以被适配成模型认证视图，也可以被适配成存储认证视图（u5-l1 的"一份凭据两种视图"）；而存储操作逻辑（分片上传、列举分页）只取决于目标云，与具体认证子类型（user_principal 还是 instance_principal）无关。拆开后，一个 `BaseStorage` 实例只吃一个 `StorageAuthProvider`，两者可独立替换、独立测试。

---

### 4.2 存储工厂与校验

#### 4.2.1 概念说明

有了抽象接口，上层还需要一个"门面"来屏蔽"该 new 哪个具体类"的细节。这个门面就是 `StorageFactory`。它对外只暴露一个静态方法 `create_storage(provider, auth, **kwargs)`：你给它一个 provider 字符串（`"oci"` / `"aws"` / `"azure"` / `"gcp"` / `"github"`）和一个 auth provider，它返回一个建好的 `BaseStorage` 实例。

`StorageFactory` 做两件关键的事：

1. **provider-auth 一致性校验**：它从 auth provider 取出 `get_storage_type()`，与传入的 `provider` 比对，不一致就立刻报错。这是一道防呆闸——避免配置上写"我要传到 AWS"却塞了一个 OCI 认证对象。
2. **惰性导入（lazy import）**：每家云的 SDK 只在那一家被实际选中时才 `import`。genai-bench 是一个基准测试小工具，不应为了"可能用到 OCI"就强求所有用户都装 `oci` SDK。惰性导入让依赖按需加载，呼应了 u1-l1 提到的 `[aws]/[azure]/[gcp]/[multi-cloud]` 可选依赖设计。

#### 4.2.2 核心流程

`create_storage` 的判定流程是一串 `if/elif`，结构很直白：

```text
输入 provider, auth, **kwargs
   │
   ├─ storage_type = auth.get_storage_type()
   ├─ if provider != storage_type:  raise ValueError   # 闸 1：身份一致性
   │
   ├─ provider == "oci"   ─► lazy import OCIObjectStorage  ─► return OCIObjectStorage(auth, **kwargs)
   ├─ provider == "aws"   ─► lazy import AWSS3Storage      ─► return AWSS3Storage(auth, **kwargs)
   ├─ provider == "azure" ─► lazy import AzureBlobStorage  ─► return AzureBlobStorage(auth, **kwargs)
   ├─ provider == "gcp"   ─► lazy import GCPCloudStorage   ─► return GCPCloudStorage(auth, **kwargs)
   ├─ provider == "github"─► lazy import GitHubStorage     ─► return GitHubStorage(auth, **kwargs)
   └─ else                ─► raise ValueError(列出合法取值)
```

注意校验有**两层**：工厂层做 `provider == storage_type`；而每个具体实现类的 `__init__` 还会再做一次（如 `if auth.get_storage_type() != "aws": raise ValueError`）。这是"防御性编程"——即便绕过工厂直接构造，也能被构造器拦住。两个 provider/auth 身份来源（参数 `provider` 与 `auth.get_storage_type()`）必须指向同一家云，这就是整个存储子系统的不变量（invariant）。

#### 4.2.3 源码精读

工厂先取身份、比对、再分发：

[factory.py:27-34](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/factory.py#L27-L34) 这是"闸 1"：拿 `auth.get_storage_type()` 与 `provider` 比对，不一致就抛 `ValueError`。中文说就是："你要的存储厂商和这个认证对象的类型对不上"。

随后是典型的惰性导入——`import` 语句写在 `if` 分支**内部**而非文件顶部：

[factory.py:36-40](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/factory.py#L36-L40) 只有当 `provider == "oci"` 真的发生时，才去 `from genai_bench.storage.oci_storage import OCIObjectStorage`。注释明说："Lazy import to avoid requiring OCI SDK if not used"。

落到不认识的 provider 时，给出带合法取值清单的报错：

[factory.py:61-65](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/factory.py#L61-L65) 列出 `oci, aws, azure, gcp, github`，方便用户自查拼错。

第二层校验在每个具体类里，以 OCI 为例：

[oci_storage.py:18-31](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_storage.py#L18-L31) `__init__` 一进来就再判一次 `auth.get_storage_type() != "oci"`，然后把 auth 里的底层凭据 `get_credentials()` 取出来，喂给更深一层的 `OSDataStore`。AWS 实现里也有同款守卫（`if auth.get_storage_type() != "aws"`，见 [aws_storage.py:23-24](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/aws_storage.py#L23-L24)）。

> 设计要点：**惰性导入有两处**。工厂里惰性导入的是"存储类本身"；而具体类（如 `AWSS3Storage`）的 `__init__` 里还会惰性导入"云 SDK"（`boto3`，见 [aws_storage.py:30-37](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/aws_storage.py#L30-L37)），且 import 失败时给出 `pip install boto3` 的友好提示。两层惰性共同保证了"不用哪家云就不需要装哪家的包"。

#### 4.2.4 代码实践

**实践目标：** 用一个假 auth 对象，亲手触发工厂的两道校验，看清它们各自报什么错。

**操作步骤：**

1. 写一个只实现 `StorageAuthProvider` 的假 provider，固定返回 `get_storage_type() == "oci"`。
2. 分别尝试两种错误调用：传错 `provider` 字符串、以及让 auth 身份与 provider 不一致。

```python
# 示例代码：用于观察工厂校验，非项目原有代码
from genai_bench.storage.factory import StorageFactory

class FakeAuth:
    """最小化的假 StorageAuthProvider，仅满足工厂读取需要。"""
    def get_storage_type(self):
        return "oci"
    def get_credentials(self):
        return object()
    def get_region(self):
        return None

# 情形 A：provider 与 auth 身份不一致
try:
    StorageFactory.create_storage("aws", FakeAuth())
except ValueError as e:
    print("情形 A（身份不一致）:", e)

# 情形 B：完全不认识的 provider
try:
    StorageFactory.create_storage("aliyun", FakeAuth())
except ValueError as e:
    print("情形 B（未知 provider）:", e)
```

**需要观察的现象：**

- 情形 A 应被"闸 1"拦下，提示 storage provider 与 auth type 不匹配；
- 情形 B 不会被闸 1 拦（因为 `FakeAuth` 自报 `"oci"`，但你传的是 `"aliyun"`，其实也会先被闸 1 拦）。要触发最后的"未知 provider"分支，需要让 `FakeAuth.get_storage_type()` 返回一个与 `provider` 相同但非法的值，例如两者都返回 `"aliyun"`。

**预期结果：** 修改 `FakeAuth.get_storage_type()` 与传入 `provider` 同为 `"aliyun"` 时，才会落到 [factory.py:61-65](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/factory.py#L61-L65) 的"未知 provider"分支。这能帮你区分两道校验各自负责的错误形态。

> 待本地验证：以上脚本依赖 `genai_bench` 已安装；运行前确认处于项目虚拟环境内。

#### 4.2.5 小练习与答案

**练习 1：** 如果把 `from genai_bench.storage.oci_storage import OCIObjectStorage` 从 `if` 分支里挪到 `factory.py` 文件顶部，会有什么副作用？

**参考答案：** 那么**任何**一次 `create_storage` 调用（哪怕只是想用 AWS）都会触发 `oci_storage` 模块的加载，进而可能触发 OCI SDK（`oci` 包）的加载。一旦用户没装 `oci`，即便他只想用 AWS，也会在导入阶段就 `ImportError`。惰性导入保证了"用到才加载"，把装 SDK 的义务推迟到真正选了那家云的时候。

**练习 2：** 为什么具体实现类的 `__init__` 里还要再判一次 `get_storage_type()`，工厂不是已经判过了吗？

**参考答案：** 工厂的校验只覆盖"经工厂构造"这条路径。但 `OCIObjectStorage(auth)` 也可以被直接 new 出来（比如在测试或二次开发里）。构造器里的二次校验是纵深防御，确保无论从哪条路径进来，"auth 身份必须与实现类匹配"这个不变量都不被破坏。

---

### 4.3 结果上传链路

#### 4.3.1 概念说明

前两个模块分别讲了"接口"和"工厂"，本模块把它们放进**真实的调用场景**：压测跑完，CLI 怎么把整个实验目录搬上云端。

这条链路的起点是 `benchmark` 函数末尾的一段代码（约 90 行）。它遵循一个清晰的三段式：**先认证、再建存储、最后传文件夹**。值得注意的是，模型认证用的是 `UnifiedAuthFactory.create_model_auth`，而存储认证用的是同一个工厂的 `create_storage_auth`——两者完全独立，所以才有"用 A 家模型、存到 B 家云"的跨云组合能力。

OCI 的存储实现比其他厂商多一层：`OCIObjectStorage` 并不直接调 OCI SDK，而是委托给一个叫 `OSDataStore` 的更底层对象（项目里把它描述为 "wrapping existing OSDataStore"）。这层封装带来了两个 OCI 专属能力：用 `ObjectURI` 这个 Pydantic 数据类统一描述"对象在哪"，以及针对大文件的**分片上传（multipart upload）**。其他厂商（如 AWS）则把这些逻辑直接写在存储类里。

#### 4.3.2 核心流程

一次"上传结果"的完整时序：

```text
CLI: --upload-results --storage-provider aws --storage-bucket my-bench ...
        │
        │  (validate_object_storage_options 先校验：要上传就必须给 bucket)
        ▼
benchmark 函数末尾：
   1. if not upload_results: return                 # 总开关
   2. storage_provider_final = storage_provider or "oci"   # 向后兼容兜底
   3. 按 provider 组装 storage_auth_kwargs            # 各家认证参数不同
   4. UnifiedAuthFactory.create_storage_auth(...)     # 得到 StorageAuthProvider
   5. （OCI 专属）把 namespace 塞进 storage_kwargs
   6. StorageFactory.create_storage(...)              # 得到 BaseStorage（含一致性校验）
   7. storage.upload_folder(experiment_folder, bucket, prefix=...)   # 真正上传
```

`upload_folder` 内部对所有厂商几乎是同一个套路：用 `rglob("*")` 递归遍历本地目录，对每个文件算出相对路径，再拼上 `prefix` 作为云端对象名，最后调用各自的 `upload_file`。差异只在于"单文件怎么传"——OCI 走 `OSDataStore.upload`（带 128MB 分片阈值），AWS 直接走 `boto3` 的 `upload_file`（100MB 阈值）。

#### 4.3.3 源码精读

先看 CLI 选项是怎么定义与初校验的。两个选项组分别管"存储认证参数"和"上传开关 + OCI namespace"：

[option_groups.py:688-696](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/option_groups.py#L688-L696) `--storage-provider` 用 `click.Choice` 限定五选一，且 `default="oci"`（向后兼容旧版 OCI-only CLI）。

[option_groups.py:842-856](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/option_groups.py#L842-L856) `object_storage_options` 定义 `--namespace`（OCI 专属）与 `--upload-results` 开关；后者挂了 `validate_object_storage_options` 回调。

该校验回调的逻辑很简短但很关键：

[validation.py:408-419](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L408-L419) 当 `--upload-results` 为真却没给 `--storage-bucket`，直接抛 `click.UsageError`——"要上传就得给桶"。

接下来是 `benchmark` 函数末尾的上传主体。总开关是第一道闸：

[cli.py:577-584](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L577-L584) `if not upload_results: return` 提前返回；否则用 `storage_provider or "oci"` 做向后兼容兜底（注释明示这是为了兼容旧 CLI）。

> 小提醒：因为 `--storage-provider` 的 CLI 默认值已经是 `"oci"`（见上面 option_groups），这里的 `or "oci"` 属于"双保险"——正常路径下 `storage_provider` 不会为 `None`，但留着兜底以防万一。

随后按 provider 分门别类组装**认证参数** `storage_auth_kwargs`，这里截取 OCI 与 AWS 两支：

[cli.py:589-609](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L589-L609) OCI 用 `auth_type / config_path / profile / token / region`；AWS 用 `access_key_id / secret_access_key / session_token / region / profile`。每家的 kwargs 形状不同，因为每家的认证 provider 构造参数不同。

接着用统一工厂造认证 provider，再用存储工厂造存储实例：

[cli.py:639-650](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L639-L650) 先 `UnifiedAuthFactory.create_storage_auth(...)` 拿到 `StorageAuthProvider`；OCI 时把 `namespace` 塞进 `storage_kwargs`；再 `StorageFactory.create_storage(...)` 拿到 `BaseStorage`。

最后一步就是真正的上传：

[cli.py:658-660](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L658-L660) 调 `storage.upload_folder(experiment_folder_abs_path, storage_bucket_final, prefix=storage_prefix_final)`。注意 `experiment_folder_abs_path` 是绝对路径——这正是 u1-l2 提到的、压测产出物所在的那个实验目录。

现在深入看 `upload_folder` 的实现。OCI 版用 `rglob` 递归遍历、对每个文件拼 `prefix/相对路径` 再调自身 `upload_file`：

[oci_storage.py:67-97](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_storage.py#L67-L97) 关键三步：`rglob("*")` 取所有文件、`relative_to(local_folder)` 算相对路径、`f"{prefix}/{relative_path}"`（有 prefix 时）拼云端对象名。AWS 版逻辑几乎逐字相同（见 [aws_storage.py:130-157](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/aws_storage.py#L130-L157)），差异仅在 `upload_file` 内部。

OCI 的 `upload_file` 把"对象在哪"建模成一个 `ObjectURI`，再交给 `OSDataStore`：

[oci_storage.py:54-64](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_storage.py#L54-L64) 用 `namespace / bucket_name / object_name / region` 构造 `ObjectURI`，然后 `self.datastore.upload(str(local_path), target_uri)`。`ObjectURI` 是个 Pydantic 模型，定义了 OCI 的 URI 格式 `oci://n/{namespace}/b/{bucket}/o/{object_path}`（[object_uri.py:9-19](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_object_storage/object_uri.py#L9-L19)）。

最底层的 `OSDataStore.upload` 才真正碰 OCI SDK，并按文件大小决定单次还是分片上传：

[os_datastore.py:149-161](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_object_storage/os_datastore.py#L149-L161) 超过 `128 * MB` 走 `multipart_upload`，否则单次 `put_object`。namespace 缺失时会先调 `get_namespace()` 向服务端查询并回填。

> 设计要点：**OCI 的"三层楼"**。最上层 `OCIObjectStorage`（实现 `BaseStorage`）→ 中层 `OSDataStore`（实现 `DataStore`，封装 OCI SDK 调用与分片逻辑）→ 最底层 `ObjectURI`（纯数据类，描述对象坐标）。这种分层让"统一接口"与"OCI 既有实现"各得其所：genai-bench 既能把 OCI 纳入统一的 `BaseStorage` 体系，又能复用项目里已有的成熟 `OSDataStore` 而不必重写。

#### 4.3.4 代码实践

**实践目标：** 通读 CLI 上传段落，把"参数 → 工厂 → 存储 → 上传"这条链路用人话说清楚，并对照源码标注每一步。

**操作步骤：**

1. 打开 [cli.py:576-665](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L576-L665)，逐段阅读。
2. 在本地建一个"假上传"实验：准备一个含若干小文件的临时目录，写脚本用 `UnifiedAuthFactory.create_storage_auth` + `StorageFactory.create_storage` 构造一个**本地文件系统版**的假存储（自实现 `BaseStorage`，把 `upload_file` 实现成"复制到另一个本地目录"），调用 `upload_folder` 观察对象名拼装。

```python
# 示例代码：用一个本地"影子存储"观察 upload_folder 的对象名拼装，非项目原有代码
import shutil
from pathlib import Path
from genai_bench.storage.base import BaseStorage

class LocalShadowStorage(BaseStorage):
    """把上传'伪装'成复制到本地另一个目录，便于观察对象名。"""
    def __init__(self, dest: Path):
        self.dest = dest
        self.dest.mkdir(parents=True, exist_ok=True)
    def upload_file(self, local_path, remote_path, bucket, **kwargs):
        target = self.dest / bucket / remote_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(local_path, target)
        print(f"  对象名 = {bucket}/{remote_path}")
    def upload_folder(self, local_folder, bucket, prefix="", **kwargs):
        local_folder = Path(local_folder)
        for fp in local_folder.rglob("*"):
            if fp.is_file():
                rel = fp.relative_to(local_folder)
                remote = f"{prefix}/{rel}" if prefix else str(rel)
                self.upload_file(fp, remote, bucket)
    # 其余抽象方法按需补全（list/download/delete/get_storage_type）

# 准备：tmp/exp/a.json、tmp/exp/sub/b.json
src = Path("/tmp/exp"); (src / "sub").mkdir(parents=True, exist_ok=True)
(src / "a.json").write_text("{}"); (src / "sub" / "b.json").write_text("{}")

shadow = LocalShadowStorage(Path("/tmp/shadow"))
shadow.upload_folder(src, bucket="my-bench", prefix="exp/2024")
```

**需要观察的现象：** 控制台应打印两条对象名，分别是 `my-bench/exp/2024/a.json` 与 `my-bench/exp/2024/sub/b.json`，体现了"prefix + 相对路径（含子目录）"的拼装规则，与 [oci_storage.py:85-97](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_storage.py#L85-L97) 的逻辑一致。

**预期结果：** `/tmp/shadow/my-bench/exp/2024/` 下会出现 `a.json` 和 `sub/b.json`，目录结构被忠实复刻。这验证了 `upload_folder` 是"把本地目录树原样搬到云端（带公共前缀）"。

> 待本地验证：示例代码未实现 `list_objects / download_file / delete_object / get_storage_type`，仅用于观察 `upload_folder`；若要作为完整 `BaseStorage` 实例需补全。

#### 4.3.5 小练习与答案

**练习 1：** 文档里有个"跨云"示例：用 OpenAI 压测、结果存 AWS S3。从源码看，为什么这种组合能成立？

**参考答案：** 因为模型认证走 `UnifiedAuthFactory.create_model_auth`，存储认证走 `create_storage_auth`，二者互不引用。`benchmark` 函数里 `--api-backend openai` 只影响模型认证与 User 类，而 `--storage-provider aws` 只影响存储认证与存储类。两条链路在代码上是分开的两个分支，组合任意。

**练习 2：** OCI 存储实现里，`namespace` 这个参数为什么需要单独传，而 AWS 就没有类似东西？

**参考答案：** OCI Object Storage 的对象寻址需要一个"namespace"（租户级命名空间）作为额外坐标——`ObjectURI` 的格式 `oci://n/{namespace}/b/{bucket}/o/{object}` 就体现了它。AWS S3 的对象坐标只需 `(bucket, key)` 两元，没有 namespace 概念。所以 CLI 里 `--namespace` 是 OCI 专属（见 [option_groups.py:844-848](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/option_groups.py#L844-L848)），且 [cli.py:645-646](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L645-L646) 只在 `provider == "oci"` 时把它塞进 `storage_kwargs`；OCI 实现还允许不传时向服务端查询（[oci_storage.py:34-37](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_storage.py#L34-L37)）。

**练习 3：** 如果一次压测产出了一个非常大的 PNG（比如 200MB），OCI 和 AWS 各会怎么传？

**参考答案：** OCI 在 `OSDataStore.upload` 里判定 `file_size > 128 * MB`，走 `multipart_upload`（分片、并发上传，见 [os_datastore.py:149-161](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/oci_object_storage/os_datastore.py#L149-L161)）；AWS 在 `upload_file` 里判定 `file_size > 100MB`，用 `TransferConfig` 触发分片（见 [aws_storage.py:87-101](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/aws_storage.py#L87-L101)）。两家阈值不同（128MB vs 100MB），但都把"大文件分片"作为内置策略。

## 5. 综合实践

**综合任务：绘制一张完整的"存储上传数据流图"，并用一个端到端 dry-run 验证关键不变量。**

1. **读图阶段**：以 [cli.py:576-665](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L576-L665) 为基准，画一张流程图，至少包含这些节点与箭头：
   - `--upload-results`（总开关）→ `validate_object_storage_options`（要求 bucket）；
   - `storage_provider or "oci"`（兜底）→ 组装 `storage_auth_kwargs`；
   - `UnifiedAuthFactory.create_storage_auth` → `StorageAuthProvider`；
   - （OCI）`namespace` 注入 → `StorageFactory.create_storage`（做 provider-auth 一致性校验）→ `BaseStorage`；
   - `BaseStorage.upload_folder` → 各厂商 `upload_file` → 云端对象。
   在每个节点旁注明它对应的源码行号区间。

2. **验证阶段**：写一个脚本，用一个"会自报身份为 azure 的假 auth"去调 `StorageFactory.create_storage("aws", fake_azure_auth)`，确认它被"闸 1"拦下；再把假 auth 改成自报 `aws` 但故意缺 `get_client_config`，确认它能通过工厂、却在 `AWSS3Storage.__init__` 里因为缺少 SDK 或方法而失败。用这两次实验说明"工厂校验"与"构造器校验"分别守住了哪道关。

3. **反思**：用一段话回答——为什么 genai-bench 要把存储做成这么一套"接口 + 工厂 + 惰性导入 + 双层校验"的结构，而不是简单地写一个 `upload_to_oci(...)` 函数了事？提示从"跨云、按需依赖、可测试、向后兼容"四个角度组织。

> 完成本任务后，你应能不看源码，向别人讲清"我加了 `--upload-results --storage-provider aws --storage-bucket xxx` 之后，代码内部依次发生了什么"。

## 6. 本讲小结

- `BaseStorage`（[base.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/base.py)）用六个抽象方法（upload_file/upload_folder/download_file/list_objects/delete_object/get_storage_type）定义了多云存储的统一契约，上层只面向它编程。
- `StorageFactory.create_storage`（[factory.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/factory.py)）是分发中枢：先做 `provider == auth.get_storage_type()` 的一致性校验，再按 provider 惰性导入对应实现类；不认识的 provider 报错并列出合法取值。
- 惰性导入有两层——工厂里懒加载"存储类"、具体类 `__init__` 里懒加载"云 SDK"，共同实现"不用哪家云就不装哪家包"。
- 校验也有两层——工厂层的 provider-auth 比对 + 每个具体类构造器里的 `get_storage_type()` 再判一次，构成纵深防御。
- 结果上传链路是清晰的三段式：`--upload-results` 开关 → `UnifiedAuthFactory.create_storage_auth` 造认证 → `StorageFactory.create_storage` 造存储 → `upload_folder` 传整个实验目录；模型认证与存储认证相互独立，因而支持跨云组合。
- OCI 实现比其他厂商多一层（`OCIObjectStorage → OSDataStore → ObjectURI`），借此复用既有成熟实现并支持 namespace 寻址与大文件分片上传（128MB 阈值）。

## 7. 下一步学习建议

本讲把"存储认证 + 多云存储 + 结果上传"这条链路讲完了，认证—存储横切能力到此收尾。建议接下来：

- **U5-L4（OCI 认证与对象存储深入）**：本讲里 OCI 多出来的那层 `OSDataStore` / `ObjectURI` 其实依赖 OCI 的认证体系（session、user/instance principal、obo token）。L4 会专门拆开 OCI 的多种认证方式，与你这里看到的 `get_credentials()` / `get_region()` 串起来。
- **U8-L1（benchmark 主流程编排 capstone）**：本讲只聚焦了 `benchmark` 函数**末尾**的上传段。L1 会把"认证 → 数据 → 采样 → 运行 → 报告 → 上传"整条主流程串成一张完整时序图，届时你会看清上传环节在整个实验生命周期里的位置。
- **自行扩展练习**：仿照 `AWSS3Storage`，尝试写一个指向**本地文件系统**的 `LocalFileSystemStorage`（实现全部六个抽象方法），并把它接到一个自定义 provider 字符串上——不过注意，要让 CLI 真的能选到它，还需要改 `StorageFactory` 与 `UnifiedAuthFactory` 的分发分支，这恰好是 U8-L3"扩展指南"要讲的注册点。
