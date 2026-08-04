# 仓库目录结构与多语言代码组织

## 1. 本讲目标

本讲承接上一讲《AIBrix 是什么：项目定位与整体架构》。上一讲我们认识了 AIBrix 的四大子系统（控制平面、网关、运行时、KV Cache），但还没有真正打开仓库。本讲要带读者「走进仓库」，学完后你应该能够：

- 说出仓库根目录下每个顶层目录（`cmd/`、`pkg/`、`api/`、`python/`、`config/`、`samples/` 等）各自承担什么职责。
- 看懂 Go 代码是怎么组织的：`cmd/` 放可执行入口、`pkg/` 放可复用库、`api/` 放自定义资源（CRD）类型。
- 看懂 Python 代码是怎么组织的：`python/aibrix`（运行时与前端）与 `python/aibrix_kvcache`（分布式 KV Cache，含 CUDA 内核）两个独立包。
- 在面对任意一个 AIBrix 源码文件时，能立刻判断它属于哪个子系统、用什么语言、由谁构建。

掌握目录布局是后续阅读所有讲义的前提——目录结构本身就是一张「系统地图」。

## 2. 前置知识

阅读本讲前，建议你已了解以下基础概念。不熟悉也不要紧，下面会用通俗语言快速过一遍。

- **Monorepo（单一仓库）**：把多种语言、多个组件的代码放在同一个 Git 仓库里管理。AIBrix 就是典型的 monorepo——Go 和 Python 代码共存。它的好处是各组件版本天然同步，坏处是目录会比较多，需要清晰的分层约定。
- **Go module**：Go 的依赖与包管理单元，由仓库根目录的 `go.mod` 文件声明。一个 `go.mod` 对应一个 import 路径前缀，AIBrix 的前缀是 `github.com/vllm-project/aibrix`。
- **Python package（包）**：Python 用 `pyproject.toml` 描述一个可安装的发行包。AIBrix 在 `python/` 下有两个这样的包。
- **CRD（CustomResourceDefinition）**：Kubernetes 的「自定义资源」。上一讲提到 AIBrix 用 CRD 描述模型、伸缩策略、拓扑等。这些资源的字段定义（用 Go struct 写成）就放在 `api/` 目录。
- **kustomize**：Kubernetes 官方的「配置拼装」工具，`config/` 目录就是一组 kustomize 的 overlay 分层。

一个直觉化的比喻：把 AIBrix 仓库想象成一栋办公楼。`cmd/` 是「前台」（每个子目录是一个独立的可运行程序入口），`pkg/` 是「各科室的公共资料室」（被前台反复引用的库代码），`api/` 是「公司档案柜的标准表格模板」（CRD 定义），`python/` 是「隔壁楼的两个研究所」（运行时与 KV Cache），`config/` 是「施工图纸」（部署清单），`samples/` 则是「样例填好的表格」。

## 3. 本讲源码地图

本讲会引用以下真实源码文件或目录，它们是理解仓库布局的「坐标点」：

| 路径 | 作用 |
| --- | --- |
| `go.mod` | Go 模块声明，定义整个 Go 代码树的 import 路径前缀与依赖。 |
| `cmd/controllers/main.go` | 控制平面（controller manager）的程序入口。 |
| `cmd/plugins/main.go` | Envoy ExtProc 网关插件的程序入口。 |
| `api/autoscaling/v1alpha1/`、`api/model/v1alpha1/`、`api/orchestration/v1alpha1/` | 三组 CRD 的 Go 类型定义。 |
| `pkg/controller/`、`pkg/plugins/gateway/`、`pkg/cache/` | 控制器、网关、中央缓存的实现库。 |
| `python/aibrix/aibrix/runtime/model_runtime.py` | Python 运行时边车的引擎生命周期管理。 |
| `python/aibrix/pyproject.toml` | `aibrix` 这个 Python 包的元数据。 |
| `python/aibrix_kvcache/` | 分布式 KV Cache 包，含 Python 与 CUDA 源码。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块拆分：先速查顶层目录，再分别细看 Go 与 Python 的组织方式。

### 4.1 顶层目录职责速查

#### 4.1.1 概念说明

AIBrix 是一个多语言、多组件的 monorepo。要让这么大的仓库不至于混乱，必须用一套稳定的「目录约定」：每个顶层目录只承担一类职责。只要记住这套约定，你就能在任何时候快速定位代码。

注意一个关键划分：AIBrix 同时包含 **控制平面/数据平面（Go）** 和 **运行时/KV Cache（Python）** 两套技术栈。它们在仓库里物理上是分开的——Go 代码集中在仓库根部的 `cmd/`、`pkg/`、`api/`，Python 代码全部集中在 `python/` 目录下。这一点和很多纯 Go 或纯 Python 项目不同，是理解 AIBrix 布局的第一把钥匙。

#### 4.1.2 核心流程

我们用一张表把顶层目录的职责一次性列清楚：

| 顶层目录 | 语言/类型 | 职责一句话说明 |
| --- | --- | --- |
| `cmd/` | Go | 各个可执行程序的入口，每个子目录是一个二进制（`controllers`、`plugins`、`console`、`kvcache-watcher`）。 |
| `pkg/` | Go | 可复用的库代码：控制器逻辑、网关逻辑、中央缓存、webhook、feature gate、指标等。 |
| `api/` | Go | CRD 的 Go 类型定义，分 `autoscaling/`、`model/`、`orchestration/` 三组。 |
| `python/` | Python | 运行时与 KV Cache 两个 Python 包（`aibrix`、`aibrix_kvcache`）。 |
| `config/` | YAML (kustomize) | Kubernetes 部署清单：CRD、控制器 manager、webhook、RBAC、网关等分层 overlay。 |
| `samples/` | YAML | 各类用法示例的自定义资源（CR），如 `quickstart/`、`autoscaling/`、`distributed/`。 |
| `deployment/` | 脚本/配置 | 部署方式集合：`standalone/`（docker-compose）、`local/`、`terraform/`。 |
| `build/` | Dockerfile | 容器镜像构建相关（`build/container/`）。 |
| `apps/` | 前端 | 应用前端：`chat/`、`console/`。 |
| `test/` | Go 测试 | 单元 / 集成 / e2e 测试（envtest、ginkgo）。 |
| `observability/` | 配置 | 监控相关：`grafana/` 仪表盘、`monitor/`。 |
| `brixbench/`、`benchmarks/` | 性能基准 | 性能基准测试框架与用例。 |
| `docs/` | 文档 | 项目文档源。 |
| `hack/`、`scripts/` | 脚本 | 开发辅助脚本。 |

> 记忆诀窍：**「入口看 `cmd/`、逻辑看 `pkg/`、类型看 `api/`、Python 看 `python/`、装集群看 `config/`、抄样例看 `samples/`」**。

#### 4.1.3 源码精读

仓库根的 `go.mod` 第一行就锁定了整个 Go 代码树的「身份证」——import 路径前缀。所有 Go 包都以这个前缀开头：

[go.mod:1-7](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/go.mod#L1-L7)

这一段说明模块名是 `github.com/vllm-project/aibrix`，Go 版本为 `1.22.5`，随后是依赖清单。它意味着：`cmd/controllers/main.go` 这个包的完整 import 路径就是 `github.com/vllm-project/aibrix/cmd/controllers`，而 `pkg/controller` 的路径是 `github.com/vllm-project/aibrix/pkg/controller`。这就是为什么后面所有的 import 语句都以 `github.com/vllm-project/aibrix/...` 开头——它们都是同一棵 Go 树的内部引用。

确认这一点很重要：**仓库里只有这一份 `go.mod`，Go 代码（无论控制平面还是网关）共用同一个 module**。这与 Python 那边「两个独立 pyproject」的组织方式形成对比（见 4.3）。

#### 4.1.4 代码实践

1. **实践目标**：建立顶层目录的肌肉记忆。
2. **操作步骤**：在仓库根目录执行 `ls -F`（或用文件浏览器），对照上面的职责表，给每个顶层目录在心里贴一个标签。
3. **需要观察的现象**：你会注意到 Go 相关目录（`cmd`、`pkg`、`api`）和 Python 目录（`python`）是并列的，且 Python 代码被「收拢」进了单一的 `python/` 目录。
4. **预期结果**：你能不查表说出 `samples/` 装的是示例 CR、`config/` 装的是部署清单、`pkg/` 装的是 Go 库。

#### 4.1.5 小练习与答案

**练习 1**：如果要找「PodAutoscaler 这个自定义资源有哪些字段」，应该去哪个目录？
> **答案**：去 `api/` 目录。具体是 `api/autoscaling/v1alpha1/podautoscaler_types.go`。CRD 的字段定义都集中在 `api/`，而不是 `pkg/`。

**练习 2**：`cmd/` 和 `pkg/` 的本质区别是什么？
> **答案**：`cmd/` 里的每个子目录编译出一个**可执行二进制**（`package main`），是程序入口；`pkg/` 里是被 `cmd/` 引用的**库代码**（非 main 包），自身不能单独运行，但可以被多处复用。

---

### 4.2 Go 代码组织（cmd / pkg / api）

#### 4.2.1 概念说明

AIBrix 的 Go 代码遵循 Kubernetes 生态里非常成熟的「kubebuilder 风格」三段式布局：

- **`api/`**：先定义「数据模型」（CRD 的 Go struct）。
- **`pkg/`**：再写「业务逻辑」（控制器、网关、缓存）。
- **`cmd/`**：最后写「程序入口」（组装 scheme、启动 manager）。

这是一种「数据→逻辑→入口」的依赖方向：`cmd/` 依赖 `pkg/`，`pkg/` 依赖 `api/`，反过来不成立。这样的单向依赖让代码易于测试和演进。

需要特别强调：控制平面（控制器）和网关虽然职责完全不同，但它们**都是 Go 写的、共用同一个 module**，只是入口不同——控制平面的入口是 `cmd/controllers/main.go`，网关的入口是 `cmd/plugins/main.go`。

#### 4.2.2 核心流程

Go 三段式的依赖与组装流程可以用下面这组关系描述：

```text
api/  (CRD 类型: PodAutoscaler / ModelAdapter / ModelClaim / StormService ...)
  ↑ 被 import
pkg/  (业务逻辑: pkg/controller/*  pkg/plugins/gateway/*  pkg/cache ...)
  ↑ 被 import
cmd/  (程序入口: cmd/controllers  cmd/plugins  cmd/console  cmd/kvcache-watcher)
  → go build → 二进制
```

`cmd/` 目录的四个子目录各自产出什么：

| `cmd/` 子目录 | 产出的二进制 | 对应子系统 |
| --- | --- | --- |
| `cmd/controllers/` | 控制平面 controller manager | 控制平面（控制器 + webhook） |
| `cmd/plugins/` | Envoy ExtProc 网关插件 | 数据平面（LLM 网关） |
| `cmd/console/` | 控制台后端 | 前端/管理 |
| `cmd/kvcache-watcher/` | KV Cache watcher | KV Cache 编排辅助 |

`pkg/` 里的关键库（按子系统归类）：

| `pkg/` 子目录 | 职责 |
| --- | --- |
| `pkg/controller/` | 所有控制器的 reconcile 逻辑，按域分子目录（`podautoscaler/`、`modeladapter/`、`modelclaim/`、`rayclusterfleet/`、`stormservice/` 等）。 |
| `pkg/controller/controller.go` | 借鉴 Kruise 的控制器**注册框架**（后续讲义 u2-l1 详讲）。 |
| `pkg/plugins/gateway/` | 网关实现：`gateway.go` 主流程、`algorithms/` 路由算法、`ratelimiter/` 限流、`queue/` 排队、`statesync/` 状态同步。 |
| `pkg/cache/` | 网关与伸缩器**共用**的中央内存缓存。 |
| `pkg/webhook/` | 准入 webhook（默认值、校验、边车注入）。 |
| `pkg/features/` | Feature Gate：用 `--controllers` 参数开关各控制器。 |

`api/` 的三组划分，恰好对应三类能力：

| `api/` 分组 | 包含的 CRD（举例） | 能力域 |
| --- | --- | --- |
| `api/autoscaling/v1alpha1/` | PodAutoscaler | 自动伸缩 |
| `api/model/v1alpha1/` | ModelAdapter、ModelClaim | 模型适配与激活 |
| `api/orchestration/v1alpha1/` | StormService、RoleSet、PodSet、RayClusterReplicaSet、RayClusterFleet、KVCache | 分布式推理与拓扑编排 |

#### 4.2.3 源码精读

先看控制平面入口 `cmd/controllers/main.go`。它的 import 部分把 `api/` 的三组类型、`pkg/` 的多个库都串了起来，正好印证上面的依赖方向：

[cmd/controllers/main.go:28-57](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/controllers/main.go#L28-L57)

这一段 import 做了什么：第 28 行注册 Kubernetes 内置 scheme；第 31–33 行分别 import 了 `api/autoscaling`、`api/model`、`api/orchestration` 三组 CRD 类型——这就是 `api/ → cmd/` 依赖的实证。第 34、51–54 行则 import 了 `pkg/features`、`pkg/cache`、`pkg/config`、`pkg/controller` 等 `pkg/` 库——这就是 `pkg/ → cmd/` 依赖的实证。注意末尾第 56 行 `//+kubebuilder:scaffold:imports` 是 kubebuilder 代码生成器留下的「锚点注释」，新增 API 时工具会自动往这里插入 import。

再看 `RegisterSchemas` 函数，它展示了 `api/` 三组类型是如何按 Feature Gate 选择性注册进 runtime scheme 的：

[cmd/controllers/main.go:81-111](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/controllers/main.go#L81-L111)

这一段做了什么：函数先查询每个控制器的开关状态（第 82–87 行），再按需把对应的 `api/` 类型组注册进 scheme（第 89–105 行）。例如启用了 PodAutoscaler 才注册 `autoscalingv1alpha1`（第 89–91 行），启用了分布式推理才注册 KubeRay 与 `orchestrationv1alpha1`（第 97–105 行）。这说明 `api/` 与 `pkg/controller/`、`pkg/features/` 是紧密配合的：类型定义、控制器逻辑、开关三者通过 Feature Gate 联动。

控制器的开关则来自 `--controllers` 命令行参数，在 `main()` 里被解析：

[cmd/controllers/main.go:152-153](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/controllers/main.go#L152-L153)

这一行声明了 `--controllers` 参数（默认 `*` 表示全部启用）。紧接着在 main 函数中：

[cmd/controllers/main.go:170-175](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/controllers/main.go#L170-L175)

这一段先校验、再调用 `features.InitControllers(controllers)` 初始化开关映射。Feature Gate 的细节是后续讲义 u2-l2 的主题，这里只需记住：**入口 `cmd/controllers/main.go` 是 `api/`（数据）、`pkg/controller/`（逻辑）、`pkg/features/`（开关）三者的组装点**。

最后看网关入口 `cmd/plugins/main.go` 的包声明，证明它是另一个独立的 `package main`：

[cmd/plugins/main.go:17-17](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/plugins/main.go#L17-L17)

第 17 行 `package main` 说明它和 `cmd/controllers` 一样是一个可执行入口，但组装的是网关（依赖 `pkg/plugins/gateway/`）而不是控制器。同一个 module、同一套布局，不同的入口二进制——这就是 AIBrix 控制平面与网关「同根不同枝」的组织方式。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证 Go 三段式的依赖方向。
2. **操作步骤**：
   1. 打开 `cmd/controllers/main.go`，找到所有以 `github.com/vllm-project/aibrix/api/...` 开头的 import 行，记录它们对应 `api/` 的哪一组。
   2. 再找到所有以 `github.com/vllm-project/aibrix/pkg/...` 开头的 import 行，记录它们对应 `pkg/` 的哪个子目录。
   3. 打开 `pkg/controller/podautoscaler/` 下任一文件，确认它会 import `api/autoscaling/v1alpha1`（验证 `pkg/ → api/` 依赖）。
3. **需要观察的现象**：你会看到依赖只朝一个方向流动（`cmd → pkg → api`），`api/` 目录里的文件几乎不 import `pkg/`。
4. **预期结果**：能画出 `cmd/controllers/main.go → pkg/controller → api/autoscaling` 这样一条依赖链，并解释为什么 `api/` 不应该反向依赖 `pkg/`（保持数据模型纯净、避免循环依赖）。

#### 4.2.5 小练习与答案

**练习 1**：为什么控制平面和网关不各建一个独立的 Go module，而要共用一个 `go.mod`？
> **答案**：因为它们共享大量公共代码（`pkg/cache`、`pkg/types`、`pkg/config` 等），共用一个 module 可以让这些内部依赖直接通过 import 路径引用，无需发布版本，也避免了循环依赖与版本漂移。

**练习 2**：`api/autoscaling/v1alpha1/` 目录里有个 `zz_generated.deepcopy.go` 文件，文件名前缀 `zz_generated_` 暗示了什么？
> **答案**：这是**代码生成**产物（由 controller-gen 工具根据 CRD 类型上的 `+kubebuilder` 标记自动生成），提供深拷贝方法。它不应该手动编辑——这正是为什么讲义 u1-l3 会专门讲 `make manifests` / `make generate`。

---

### 4.3 Python 代码组织（aibrix / aibrix_kvcache）

#### 4.3.1 概念说明

与 Go 那边「单 module、多入口」不同，AIBrix 的 Python 代码是**两个相互独立的发行包**，都位于 `python/` 目录下：

- `python/aibrix/`：发行包名 `aibrix`，包含运行时边车、OpenAI 兼容前端、模型下载器、指标采集、元数据服务等。它是被注入到每个推理 Pod 里的「助手」。
- `python/aibrix_kvcache/`：发行包名 `aibrix-kvcache`，专注分布式 KV Cache，里面既有 Python 代码，也有 C++/CUDA 源码（`csrc/`），通过 `setup.py` + CMake 编译出 PyTorch 算子扩展。

为什么分成两个包？因为它们的部署形态和依赖差异巨大：`aibrix` 是纯 Python、跑在每个推理 Pod 侧；`aibrix-kvcache` 需要 GPU 与 CUDA 工具链、做底层张量与跨节点传输。把二者拆成独立包，可以让用户按需安装，避免给轻量场景强加沉重的 CUDA 依赖。

#### 4.3.2 核心流程

两个 Python 包的内部结构对比：

```text
python/aibrix/                 # 包名: aibrix
├── pyproject.toml             # Poetry 元数据, name="aibrix"
└── aibrix/                    # 真正的 import 包 (import aibrix.xxx)
    ├── runtime/               # 运行时边车: 引擎生命周期、模型下载
    ├── openai_frontend/       # OpenAI 兼容的 HTTP 前端
    ├── downloader/            # 多后端模型下载器 (HF/S3/TOS)
    ├── metrics/               # 指标采集与标准化
    ├── metadata/              # 元数据服务
    ├── batch/  client/  common/  context/
    ├── gpu_optimizer/  openapi/  protos/  storage/
    └── app.py  config.py  envs.py  logger.py

python/aibrix_kvcache/         # 包名: aibrix-kvcache
├── pyproject.toml             # name="aibrix-kvcache"
├── setup.py  CMakeLists.txt   # 构建 CUDA 扩展
├── csrc/                      # C++/CUDA 内核 (会被编译成 PyTorch 算子)
│   ├── cache_kernels.cu  cache.h  torch_bindings.cpp
│   ├── attention/  core/  quantization/
└── aibrix_kvcache/            # import 包 (import aibrix_kvcache.xxx)
    ├── cache_manager.py  cache_handle.py  spec.py
    ├── l1/  l2/              # 两级缓存: 本地 L1 + 远程 L2
    ├── memory/  transport/   # 内存分配 + 跨引擎传输层
    ├── meta_service/  common/  integration/
    └── config.py  envs.py  metrics.py  profiling.py
```

> 一个易混点：`python/aibrix/` 是**项目目录**，里面还有一个同名的 `aibrix/` 才是 Python 的 **import 包**。也就是说，真正的源码在 `python/aibrix/aibrix/` 这一层的子目录里（如 `python/aibrix/aibrix/runtime/`）。`aibrix_kvcache` 同理。

#### 4.3.3 源码精读

先看 `aibrix` 包的元数据，确认它的发行包名与「源码包」映射：

[python/aibrix/pyproject.toml:27-30](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/python/aibrix/pyproject.toml#L27-L30)

这一段做了什么：声明发行包名是 `aibrix`，并把 `aibrix/` 目录作为打包内容（`packages = [{ include = "aibrix" }]`），同时排除 `tests/`。注意版本号 `version = "0.0.0"` 是占位符——下面的 `[tool.poetry-dynamic-versioning]` 配置说明真实版本会在构建时从 Git tag 动态生成。这也呼应了上一讲提到的「nightly 与 stable」两种发布方式。

再以运行时边车的核心文件 `model_runtime.py` 为例，看 Python 源码是如何组织的。它的模块文档串起了「运行时」这个子系统的定位：

[python/aibrix/aibrix/runtime/model_runtime.py:15-26](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/python/aibrix/aibrix/runtime/model_runtime.py#L15-L26)

这一段文档说明：`model_runtime.py` 是运行时边车里「引擎生命周期管理」的模块，它是「引擎 ↔ 控制平面协议」的引擎侧实现。控制平面的 ModelClaim 控制器通过 HTTP 调用它来 activate（激活）、deactivate（停用）、list（列出）模型（第 19–23 行）；并且引擎启动器是**可插拔**的，这样在没有 GPU 的环境下也能用 `MockEngineLauncher` 完整测试（第 24–26 行）。这段文字正好印证了上一讲所说的「运行时边车注入每个推理 Pod、向上暴露标准化接口供控制平面消费」。

注意第 43 行 `from aibrix.runtime.engine_registry import EngineRegistry`——这种 `from aibrix.xxx import` 的写法，正是因为 `python/aibrix/aibrix/` 才是真正的 import 包根。后续讲义（u9 系列）会深入这个 `runtime/` 目录。

最后确认第二个包的身份：`python/aibrix_kvcache/pyproject.toml` 里 `name = "aibrix-kvcache"`（注意是连字符的发行名），它带有 `csrc/`、`setup.py`、`CMakeLists.txt`，说明这是一个**需要编译原生扩展**的包。这部分是 u10 系列讲义的主题，本讲只需建立「Python 代码在 `python/` 下、分两个独立包、KV Cache 包带 CUDA 源码」的认知。

#### 4.3.4 代码实践

1. **实践目标**：亲手验证 Python「项目目录 ≠ import 包」的双层结构。
2. **操作步骤**：
   1. 进入 `python/aibrix/`，观察它下面有一个**同名的** `aibrix/` 子目录，真正的 import 包源码就在这里。
   2. 打开 `python/aibrix/aibrix/runtime/model_runtime.py`，定位第 43 行的 `from aibrix.runtime.engine_registry import EngineRegistry`，体会 import 路径 `aibrix.runtime.xxx` 与目录 `python/aibrix/aibrix/runtime/` 的对应关系。
   3. 对比 `python/aibrix_kvcache/`：同样有同名子目录 `aibrix_kvcache/`，并且额外有 `csrc/`（CUDA 源码）和 `setup.py`（编译脚本）。
3. **需要观察的现象**：两个包都遵循「外层项目目录 + 同名 import 包」的 Poetry 惯例；只有 `aibrix_kvcache` 多出了 C++/CUDA 构建产物。
4. **预期结果**：你能解释为什么 `import aibrix.runtime.model_runtime` 对应的文件路径是 `python/aibrix/aibrix/runtime/model_runtime.py`（中间多了一层同名 `aibrix/`）。

#### 4.3.5 小练习与答案

**练习 1**：如果要修改「OpenAI 兼容的 chat 接口」逻辑，应该改 `python/` 下的哪个目录？
> **答案**：`python/aibrix/aibrix/openai_frontend/`（它属于 `aibrix` 包，不属于 `aibrix-kvcache`）。

**练习 2**：`python/aibrix_kvcache/` 为什么需要 `setup.py` 和 `CMakeLists.txt`，而 `python/aibrix/` 不需要？
> **答案**：因为 `aibrix-kvcache` 包含 C++/CUDA 源码（`csrc/`），需要把 `.cu` 文件编译成 PyTorch 可调用的原生算子扩展；`setup.py` + `CMakeLists.txt` 就是这套编译流程的入口。`aibrix` 是纯 Python，无需编译。

**练习 3**：发行包名 `aibrix-kvcache` 用了连字符，但 import 时写 `import aibrix_kvcache` 用了下划线，为什么？
> **答案**：这是 Python 的通用约定——PyPI 发行包名允许连字符，但 Python 的 import 名不能含连字符，所以包目录与 import 名用下划线 `aibrix_kvcache`，二者自动对应。

## 5. 综合实践

把三个模块串起来，完成一次「仓库地图」实战。

**任务**：在仓库根目录用树形结构列出 `cmd/`、`pkg/`、`api/`、`python/` 四个目录的两级子目录，并为每个子目录写一句话用途说明。

**操作指引**：

1. 用以下命令获取两级子目录（命令本身只读，安全）：
   ```bash
   find cmd pkg api python -maxdepth 2 -type d | sort
   ```
2. 把输出整理成一棵树，对照本讲的职责表，给每个二级目录补一句说明。例如：
   - `cmd/controllers/` → 控制平面 controller manager 的入口。
   - `cmd/plugins/` → Envoy ExtProc 网关插件入口。
   - `pkg/controller/podautoscaler/` → PodAutoscaler 自动伸缩控制器逻辑。
   - `pkg/plugins/gateway/algorithms/` → 网关的多策略路由算法。
   - `api/orchestration/v1alpha1/` → 分布式推理与拓扑编排类 CRD 类型。
   - `python/aibrix/aibrix/runtime/` → 运行时边车的引擎生命周期与下载。
   - `python/aibrix_kvcache/csrc/` → KV Cache 的 CUDA 内核源码。

**预期成果**：你会得到一张属于自己的「AIBrix 仓库导航图」。当你以后想找某段功能时，先在这张图上定位它属于哪个子系统、哪个二级目录，再进去细看，效率会高得多。这张图也是后续每一篇讲义的索引基础——后续讲义都会落在这些目录里。

> 「待本地验证」提示：不同分支下 `pkg/controller/` 等目录的子项可能随功能演进而增减，请以你本地的 `find` 实际输出为准。

## 6. 本讲小结

- AIBrix 是 **monorepo**：Go 与 Python 共处一仓，靠目录约定分层。
- 顶层目录有固定职责：**入口看 `cmd/`、逻辑看 `pkg/`、类型看 `api/`、Python 看 `python/`、装集群看 `config/`、抄样例看 `samples/`**。
- Go 代码遵循 kubebuilder 三段式 **`api/ → pkg/ → cmd/`** 单向依赖，控制平面与网关共用一个 `go.mod`、只是入口不同（`cmd/controllers` vs `cmd/plugins`）。
- `api/` 分 `autoscaling`、`model`、`orchestration` 三组，正好对应自动伸缩、模型适配、分布式编排三大能力域。
- Python 代码是 `python/` 下两个**独立发行包**：`aibrix`（运行时/前端/下载/指标）与 `aibrix-kvcache`（带 CUDA 源码的分布式 KV Cache）。
- 注意 Python 的「外层项目目录 + 同名 import 包」双层结构（如 `python/aibrix/aibrix/runtime/`）。

## 7. 下一步学习建议

本讲只看了「目录长什么样」，还没有进入任何具体逻辑。建议按以下顺序继续：

1. **下一步讲义 u1-l3（构建系统、依赖与 Makefile 任务）**：理解 `Makefile` 如何把 `cmd/`、`api/`、`config/` 串起来——`make build` 编译入口、`make manifests` 生成 CRD、`make generate` 生成 `zz_generated_*.go`。
2. **讲义 u2-l1（控制器管理器入口与启动流程）**：深入 `cmd/controllers/main.go` 与 `pkg/controller/controller.go`，看 scheme 注册与控制器注册的具体实现。
3. **想直接看 Python 的读者**：可跳到 u9-l1（运行时边车与引擎生命周期），但建议先读完 u1 系列和 u2-l1，建立控制平面与运行时的协作认知。

> 延伸阅读建议：随手翻阅 `samples/quickstart/model.yaml`，对照 `api/model/v1alpha1/` 的类型定义，体会「示例 CR → CRD 类型」的对应关系，这是理解整个控制平面最快的方式。
