# 仓库三层结构与 Cargo workspace 总览

## 1. 本讲目标

学完本讲,你应该能够:

1. 不查资料地说出 `lib/`、`components/`、`deploy/` 三个顶层目录分别属于哪一层、用什么语言、产出什么构建产物。
2. 看懂根目录 `Cargo.toml` 的 `[workspace]` 成员列表,把 35 个 Rust crate 按「runtime / llm / 路由 / KVBM / 工具」分组,并说明为什么 `lib/bindings/python` 被排除在 workspace 之外。
3. 解释 `ai-dynamo` 这个 Python 包与 PyO3 扩展 `dynamo._core` 的关系:仓库里其实有**两个** Python wheel,它们分别由 maturin 和 hatchling 构建。
4. 拿到任何一个一级目录(如 `examples/`、`recipes/`、`tests/`),能立刻判断它在请求链路中的角色。

本讲是「地图课」:不深入任何模块内部,而是把整个仓库的地形刻进脑子,让后续每一讲都知道自己在哪一层打洞。

## 2. 前置知识

### 2.1 承接前两讲的认知

- **u1-l1** 告诉我们 Dynamo 是推理引擎之上的编排层,内部按「请求面 / 控制面 / 存储与事件面」组织,四大能力是 P/D 分离、KV 感知路由、KVBM 多层缓存、Planner 自动扩缩。本讲要做的,就是把这三面**落到具体的目录上**。
- **u1-l2** 我们用 `agg.sh` 跑通了 `frontend + worker` 的最小组合,知道 frontend 是 `python -m dynamo.frontend` 启动的、worker 是 sample 假后端。本讲结束后,你会知道这两条命令的代码分别住在仓库的哪个角落。

### 2.2 本讲需要的几个基础概念

| 术语 | 通俗解释 |
|------|----------|
| **Cargo workspace** | Rust 的「多 crate 单仓库」机制:多个子项目(crate)共享一份依赖版本表和一个锁文件,避免每个 crate 各自维护依赖、各自编译依赖的不同版本。 |
| **crate** | Rust 的最小编译单元,大致相当于 npm 的一个 package 或 Python 的一个发行包。 |
| **PyO3** | 一个让 Rust 代码可以被 Python 直接 `import` 的库。用 PyO3 写的 Rust crate 编译成 `.so` 共享库后,在 Python 眼里就是一个普通模块。 |
| **cdylib** | Rust 的一种构建产物类型:「C 兼容的动态库」,正是 Python 可加载的 `.so` 文件需要的形态。 |
| **maturin** | 专门用来构建「含 Rust 扩展的 Python wheel」的工具,底层调 cargo,产出 `.whl`。 |
| **wheel / extra** | wheel 是 Python 的二进制发行包格式;extra 是可选依赖组(如 `pip install ai-dynamo[vllm]`),Dynamo 用它来按推理引擎拆分依赖。 |
| **Operator / CRD** | Kubernetes 的扩展模式:自定义资源(CRD,如 DynamoGraphDeployment)+ 一个监听该资源并驱动集群向期望状态收敛的控制器(Operator,用 Go/Kubebuilder 编写)。 |
| **Helm chart** | Kubernetes 清单的参数化模板包,`helm install` 一次装一整套。 |

## 3. 本讲源码地图

本讲「精读」的不是业务代码,而是**仓库自身的构建与描述文件**——它们是整张地图的图例。

| 文件 | 作用 |
|------|------|
| [AGENTS.md](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/AGENTS.md) | 仓库官方导览:Overview 讲三层架构,Repository Map 讲目录分工,是本讲的「权威底稿」 |
| [Cargo.toml](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/Cargo.toml) | Rust workspace 根清单:35 个成员 crate、统一版本、依赖版本表 |
| [pyproject.toml](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/pyproject.toml) | `ai-dynamo` wheel 的清单:依赖 `ai-dynamo-runtime`、按引擎分 extra、hatch 打包配置、pytest 标记 |
| [lib/bindings/python/Cargo.toml](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/Cargo.toml) | PyO3 扩展 crate `dynamo-py3` 的清单:用空 `[workspace]` 把自己排除出顶层 workspace,产物名 `_core` |
| [lib/bindings/python/pyproject.toml](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/pyproject.toml) | maturin 配置:产出 `ai-dynamo-runtime` wheel,扩展模块名 `dynamo._core` |
| [lib/bindings/python/src/dynamo/runtime/\_\_init\_\_.py](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/src/dynamo/runtime/__init__.py) | Python 包装层:从 `dynamo._core` 再导出 `DistributedRuntime` 等类,是「Python→Rust」的接缝处 |
| [hatch_build.py](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/hatch_build.py) | `ai-dynamo` 的构建钩子:构建时向每个组件写入带 git 短 SHA 的 `_version.py` |

此外本讲会「路过」(只看目录、不精读)这些目录:`lib/`、`components/src/dynamo/`、`deploy/`、`examples/`、`recipes/`、`tests/`、`benchmarks/`、`container/`。

## 4. 核心概念与源码讲解

### 4.1 三层总览:一张地图读懂仓库

#### 4.1.1 概念说明

第一讲我们说 Dynamo 逻辑上分「请求面 / 控制面 / 存储与事件面」,那是**运行时视角**。仓库的物理结构则是**构建视角**的三层:

1. **Rust 核心层**(`lib/`,一个 Cargo workspace):装着运行时、LLM 引擎、KV 路由、KVBM 等性能敏感的发动机。HTTP 服务、路由打分、KV 块管理这些每秒百万次执行的热路径都在这层。
2. **Python 扩展层**(`components/src/dynamo/`,打成 `ai-dynamo` wheel):装着 frontend、各引擎后端(vllm/sglang/trtllm)、planner、mocker 等。这层存在的理由是**生态**:推理引擎和 AI 生态几乎都是 Python 的,把「接入各种引擎」的胶水放 Python 里最容易被用户改写。
3. **Kubernetes 部署层**(`deploy/`):Go 写的 Operator + Helm chart + inference-gateway,负责把上面两层变成集群里真正跑的 Pod。

为什么这么分?因为三层的**变更节奏**不同:Rust 核心追求稳定和高性能,Python 层需要频繁适配新引擎版本,部署层跟随 K8s 生态演进。物理隔离让它们可以独立测试、独立发版。理解了这一点,后面读任何代码都能先定位「我在哪层、这层的边界在哪」。

#### 4.1.2 核心流程

把仓库按层「压扁」成一张表(构建产物列回答「这层最终变成什么被用户安装」):

| 层 | 位置 | 语言 | 构建产物 | 一句话职责 |
|----|------|------|----------|-----------|
| Rust 核心 | `lib/` | Rust | 编译进 `_core` 扩展 / 独立二进制 | 运行时、HTTP 服务、路由、KVBM 的发动机 |
| Python 扩展 | `components/src/dynamo/` | Python | `ai-dynamo` wheel | frontend、引擎后端接入、planner 等用户可改写层 |
| K8s 部署 | `deploy/` | Go + YAML | Operator 镜像 + Helm chart | 把 DGD 描述变成集群里的真实进程 |
| (外围)示例 | `examples/` | Python/Shell | 不打包 | 可运行的教程式示例,含 sample/mocker 假后端 |
| (外围)配方 | `recipes/` | YAML | 不打包 | 按模型组织的一键部署单元(deploy.yaml 等) |
| (外围)测试 | `tests/` | Python | 不打包 | 顶层 pytest 套件,按域分子目录 |
| (外围)镜像 | `container/` | Dockerfile | 容器镜像 | runtime/dev 镜像的构建脚本 |

一个容易忽略的细节:**Rust 代码不全部住在 `lib/`**——`deploy/inference-gateway/ext-proc`(Envoy 扩展进程)也是 workspace 成员之一。反过来,**Python 代码也不全部住在 `components/`**——`lib/bindings/python/src/dynamo/` 下有一批 Python 包装模块,还有独立的 `lib/gpu_memory_service` Python 包。地图上有几块「飞地」,读代码时要留意。

#### 4.1.3 源码精读

仓库自己在 AGENTS.md 的 Overview 里给出了最权威的三层描述:

> [AGENTS.md:L16-L24](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/AGENTS.md#L16-L24) — 官方定位:Rust 核心(lib/ 的 Cargo workspace)承载 runtime、LLM、路由、KVBM 引擎;Python 扩展层(ai-dynamo wheel,经 PyO3/maturin 绑定到 Rust 核心)承载 frontend、后端、planner、profiler;K8s 层(deploy/)承载 operator、Helm chart 与网关集成。并明确警告:**任何跨层改动都是非平凡变更**。

紧接着的 Repository Map 是官方版目录表:

> [AGENTS.md:L157-L169](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/AGENTS.md#L157-L169) — 逐目录说明:`lib/` 是 Rust workspace(特别注明 `bindings/python` 因 PyO3 构建问题被排除在 workspace 外)、`components/src/dynamo/` 是 Python 包、`deploy/` 是 K8s 层、`container/` 是镜像、`docs/`+`fern/` 是文档、`examples/`+`recipes/` 是示例与配方、`benchmarks/`+`tests/` 是基准与测试。

注意其中一句话值得单独划线:`bindings/python — the PyO3 extension crate, built via maturin and deliberately excluded from the workspace`(经 maturin 构建、被有意排除出 workspace)。为什么「有意排除」?4.2 节揭晓。

#### 4.1.4 代码实践

**实践目标**:用只读命令验证三层结构真实存在,而不是文档的一面之词。

**操作步骤**(在仓库根目录执行):

```bash
# 1. 数一数每层的"体量":各一级目录下有多少个被 git 跟踪的文件
git ls-files | cut -d/ -f1 | sort | uniq -c | sort -rn

# 2. 验证 Rust 层:lib/ 下每个 crate 目录都有自己的 Cargo.toml
ls -d lib/*/Cargo.toml | head

# 3. 验证 Python 层:components/src/dynamo/ 下每个子目录都是一个 Python 包
ls components/src/dynamo/

# 4. 验证 K8s 层:deploy/ 下有 Go module 和 Helm chart
ls deploy/operator/go.mod deploy/helm/charts 2>/dev/null
```

**需要观察的现象**:步骤 1 的输出是一张「目录 → 文件数」降序表,`lib/` 与 `components/` 通常名列前茅;步骤 3 应列出 `frontend`、`vllm`、`sglang`、`trtllm`、`planner`、`router`、`mocker` 等约 17 个包。

**预期结果**:三条命令的输出与 4.1.2 的表格对得上,说明「三层」不是概念图,而是物理事实。(具体文件数量随版本浮动,以你本地输出为准。)

#### 4.1.5 小练习与答案

**练习 1**:`components/src/dynamo/` 下列出的包里,哪些对应 u1-l1 讲的「请求面」,哪些对应「控制面」?

**答案**:`frontend`、`router`、`vllm`/`sglang`/`trtllm`(以及测试用 `mocker`)在请求面上——它们处理或转发实际请求;`planner`、`global_planner`、`kv_state_agent` 在控制面上——它们决定扩缩与放置,不碰请求内容。

**练习 2**:既然 `deploy/` 是 K8s 层,为什么其中会出现一个 Rust crate(`deploy/inference-gateway/ext-proc`)?

**答案**:分层是按**部署角色**分的,不是严格按语言。ext-proc 是 Envoy 的扩展进程(GAIE 拓扑里网关侧的 KV 感知选点组件),它的运行位置在数据路径上,对性能敏感,所以用 Rust 写;但它属于网关部署单元,因此在 `deploy/` 下,并作为 workspace 成员参与统一构建。这正好印证了「Rust 不全在 lib/」的飞地现象。

### 4.2 Rust 核心层:Cargo workspace

#### 4.2.1 概念说明

`lib/` 下有二十多个目录,它们不是各自为政的独立项目,而是通过根 `Cargo.toml` 的 `[workspace]` 绑成一个整体。workspace 带来三个直接好处:

1. **依赖版本统一**:所有 crate 的 `tokio`、`serde` 等公共依赖只解析一次、编译一份,不会出现 A crate 用 tokio 1.50、B crate 用 tokio 1.40 的「依赖地狱」。
2. **一条命令构建全部**:`cargo build` 在根目录即可编译整个核心层;`cargo build -p dynamo-llm` 也能只编某一个。
3. **版本号统一 bump**:workspace 级 `version = "1.5.0"` 被所有成员继承,发版时只改一处。

#### 4.2.2 核心流程

workspace 成员按功能可以分成六组(记分组而不是背全名单):

```text
Cargo workspace(35 个成员)
├── 运行时基座
│   ├── lib/runtime      → dynamo-runtime(服务发现、传输、pipeline)
│   └── lib/tokens, lib/truthy, lib/memory, lib/data-gen, lib/kv-hashing
├── LLM 引擎层
│   ├── lib/llm          → dynamo-llm(HttpService、entrypoint、KV 路由宿主)
│   ├── lib/rl, lib/backend-common
├── 路由
│   ├── lib/kv-router    → dynamo-kv-router(radix 索引、策略框架)
│   ├── lib/router-plugins/catalog + examples/router/custom-policy-example/*(5 个)
├── KVBM 家族
│   └── lib/kvbm-{common,config,engine,kernels,consolidator,logical,physical}
├── 模拟与旁路
│   ├── lib/mocker(+ lib/mocker/servers/{vllm,sglang})
│   └── lib/sidecar/{common,vllm,sglang,trtllm}
└── 绑定与工具
    ├── lib/bindings/c, lib/bindings/python/codegen
    ├── lib/bench
    └── deploy/inference-gateway/ext-proc ← 唯一住在 lib/ 之外的成员
```

crate 之间的引用关系由 `[workspace.dependencies]` 统一登记:每个成员内部写 `dynamo-runtime.workspace = true` 即可拿到 `lib/runtime` 的路径依赖,版本与路径只在根清单维护一次。

#### 4.2.3 源码精读

成员清单——本仓库 Rust 层的「户口本」:

> [Cargo.toml:L4-L41](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/Cargo.toml#L4-L41) — `[workspace] members` 列出全部 35 个成员路径。注意清单里**没有** `lib/bindings/python`(它被有意排除,见下文),却有 `lib/bindings/python/codegen`(代码生成工具,是独立小 crate)和 `deploy/inference-gateway/ext-proc`。`resolver = "3"` 是配合 edition 2024 的新版依赖解析器。

统一版本与元数据:

> [Cargo.toml:L44-L52](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/Cargo.toml#L44-L52) — `workspace.package` 定义 `version = "1.5.0"`、`edition = "2024"` 等共享元数据,成员 crate 各自的 `Cargo.toml` 里写 `version.workspace = true` 继承,发版只改这一处。

本地 crate 的集中登记:

> [Cargo.toml:L58-L70](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/Cargo.toml#L58-L70) — `dynamo-runtime = { path = "lib/runtime", version = "1.5.0" }` 等:path + version 双写,既允许本地 path 互引,又保证发布到 crates.io 后能按版本解析。kvbm 家族同样集中登记在 [Cargo.toml:L83-L90](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/Cargo.toml#L83-L90)。

而那个「被排除的 crate」自己的清单长这样:

> [lib/bindings/python/Cargo.toml:L4-L22](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/Cargo.toml#L4-L22) — 顶部一个**空的 `[workspace]` 节**,注释写明用途:`empty workspace to exclude from top level workspace / excluded due to pyo3 extension module build issues`。空节会让 Cargo 认为「这里自成 workspace」,从而切断与根 workspace 的从属关系。包名 `dynamo-py3`,`[lib]` 段指定产物名为 `_core`,`crate-type = ["cdylib", "rlib"]`——cdylib 产出 Python 可 `import` 的 `.so`,rlib 支持文档测试。

为什么 PyO3 扩展必须排除?因为 PyO3 扩展要链接**特定版本**的 Python 解释器(由 maturin 注入的编译参数决定),与 workspace 其他 crate 的构建配置冲突;独立出去后由 maturin 单独驱动构建,互不干扰。这是 Rust+Python 混合仓库的一个经典布局手法。

#### 4.2.4 代码实践

**实践目标**:不靠死记,用工具随时列出 workspace 成员,并观察「排除」现象。

**操作步骤**:

```bash
# 1. 从根清单直接提取成员列表(纯文本处理,无需编译)
sed -n '/^\[workspace\]/,/resolver/p' Cargo.toml | grep '^\s*"lib\|^\s*"deploy\|^\s*"examples'

# 2. 让 cargo 自己报告 workspace 结构(首次运行需解析依赖,稍慢)
cargo metadata --no-deps --format-version 1 2>/dev/null | head -c 400

# 3. 验证 dynamo-py3 确实不在 workspace 里
cargo pkgid -p dynamo-py3 2>&1 | head -2
```

**需要观察的现象**:步骤 1 打印约 35 行路径;步骤 3 应报错,提示找不到 `dynamo-py3` 这个包——因为它被空 `[workspace]` 排除,根 workspace 对它「视而不见」。若想查询它,必须 `cd lib/bindings/python && cargo pkgid -p dynamo-py3`。

**预期结果**:你得到一份可复现的成员清单,并亲眼确认「排除」不是文档说法而是 Cargo 行为。(步骤 2/3 需要 Rust 工具链,待本地验证。)

#### 4.2.5 小练习与答案

**练习 1**:`cargo build -p dynamo-llm` 时,`lib/llm` 引用的 `dynamo-runtime` 从哪里来?

**答案**:从根 `Cargo.toml` 的 `[workspace.dependencies]`(L59)拿到 `path = "lib/runtime"` 的路径依赖,本地直接编译源码,不会去 crates.io 下载。同时它也带了 `version = "1.5.0"`,这是为将来单独发布准备的。

**练习 2**:如果不删任何代码,把 `lib/bindings/python` 加进 members 会怎样?

**答案**:构建会因 PyO3 扩展的链接配置与 workspace 统一构建冲突而出问题(该 crate 注释原话:pyo3 extension module build issues)。这正是它用空 `[workspace]` 自我排除的原因——排除是构建正确性的要求,不是风格偏好。

### 4.3 Python 扩展层:ai-dynamo wheel 与 PyO3 扩展

#### 4.3.1 概念说明

很多人以为 `pip install ai-dynamo` 装的就是「Dynamo 的 Python 部分」,但真相是:**仓库产出两个 Python wheel,而且是依赖关系**。

| | `ai-dynamo-runtime` | `ai-dynamo` |
|---|---|---|
| 源码位置 | `lib/bindings/python/`(Rust 的 `rust/` + Python 包装的 `src/dynamo/`) | `components/src/dynamo/` |
| 构建工具 | maturin(调 cargo 编 PyO3 扩展) | hatchling |
| 核心内容 | `dynamo._core`(`.so` 二进制)+ `dynamo.runtime` / `dynamo.llm` 等包装模块 | `dynamo.frontend` / `dynamo.vllm` / `dynamo.sglang` / `dynamo.trtllm` / `dynamo.planner` / … |
| 面向用户 | 一般不直接安装 | 用户安装的入口 |

这样拆的好处:核心绑定(`_core`)与上层组件可以**各自迭代发版**;且 `ai-dynamo` 是纯 Python wheel,装得快、不需要本地编译器——所有 Rust 重活都留在 `ai-dynamo-runtime` 的构建期。

此外 `ai-dynamo` 用 **extra** 拆分引擎依赖:`pip install ai-dynamo[vllm]` / `[sglang]` / `[trtllm]` / `[mocker]`。为什么不用一个大而全的依赖表?因为各引擎的依赖互斥(例如 NIXL 版本要求不同),一个环境装不下两家引擎——这就是为什么不同后端要不同的容器镜像。

#### 4.3.2 核心流程

以 u1-l2 的 `python -m dynamo.frontend` 为例,import 链自上而下穿透三层:

```text
components/src/dynamo/frontend/__main__.py     (ai-dynamo wheel,纯 Python)
        │  import dynamo.llm / dynamo.runtime
        ▼
lib/bindings/python/src/dynamo/llm, runtime    (ai-dynamo-runtime wheel 里的 Python 包装层)
        │  from dynamo._core import ...
        ▼
lib/bindings/python/rust/  编译出的 _core.so   (PyO3 扩展,真正干活的 Rust 核心)
        │  调用
        ▼
workspace 的 dynamo-llm / dynamo-runtime ...   (lib/ 下被静态链进来的 Rust crate)
```

关键认知:你在 Python 里 `import dynamo.runtime`,拿到的类(`DistributedRuntime` 等)**不是** Python 实现的,而是 Rust 结构体经 PyO3 暴露的影子;Python 文件只是给这些影子补上了文档、类型和便捷装饰器。

#### 4.3.3 源码精读

`ai-dynamo` 对 `ai-dynamo-runtime` 的依赖声明:

> [pyproject.toml:L4-L29](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/pyproject.toml#L4-L29) — `name = "ai-dynamo"`,依赖表第一项就是 `ai-dynamo-runtime==1.5.0`:两个 wheel 版本严格锁同版。这就是 monorepo 里「四处版本对齐」的一环(workspace 1.5.0 → dynamo-py3 1.5.0 → ai-dynamo-runtime 1.5.0 → ai-dynamo 1.5.0)。

按引擎拆分的 extras 与互斥关系:

> [pyproject.toml:L50-L91](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/pyproject.toml#L50-L91) — `trtllm` / `vllm` / `sglang` / `mocker` 四组可选依赖,各自钉住引擎版本(如 `vllm[flashinfer,runai,otel]==0.27.1`);mocker 组只有 `aiconfigurator-core`,这就是无 GPU 模拟的后端依赖。
>
> [pyproject.toml:L93-L122](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/pyproject.toml#L93-L122) — `[tool.uv] conflicts` 显式声明哪些 extra 两两不能共存(vllm↔sglang 的 NIXL 冲突、trtllm↔mocker 的 NumPy 冲突等),每条都注明冲突的依赖名。这张表就是「不同后端需要不同镜像」的机器可读证据。

两个 wheel 各自打包什么:

> [pyproject.toml:L167-L177](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/pyproject.toml#L167-L177) — `ai-dynamo` 用 hatchling 后端,`[tool.hatch.build.targets.wheel] packages = ["components/src/dynamo"]`:只打包 components 下的包。
>
> `ai-dynamo-runtime` 那边(即 `lib/bindings/python/pyproject.toml`)则声明 `module-name = "dynamo._core"`、`python-source = "src"`、`python-packages = ["dynamo"]`——maturin 把 Rust 编译产物和 `src/dynamo/` 下的 Python 包装一起塞进 wheel。

Python→Rust 的接缝,最直观的证据在这五行:

> [lib/bindings/python/src/dynamo/runtime/\_\_init\_\_.py:L14-L19](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/bindings/python/src/dynamo/runtime/__init__.py#L14-L19) — `from dynamo._core import Client / Context / DistributedRuntime / Endpoint / PyAsyncRequestStream`,注释写明「List all the classes in the _core module for re-export」。你后续在 u2 反复使用的 `DistributedRuntime`,源头就在这里跨过了语言边界。

最后看构建钩子如何给组件盖版本戳:

> [hatch_build.py:L34-L64](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/hatch_build.py#L34-L64) — `VersionWriterHook` 在 wheel 构建前执行:取项目版本,拼上 `git rev-parse --short HEAD` 的短 SHA,写入 `components/src/dynamo/<每个组件>/_version.py`。所以源码树里**找不到** `_version.py`——它是构建时生成的(mypy 配置里也专门为它开了忽略)。运行 `python -c "import dynamo.frontend; print(dynamo.frontend._version.__version__)"` 能看到形如 `1.5.0+2c4ab6c` 的版本串。

#### 4.3.4 代码实践

**实践目标**:在你 u1-l2 搭好的环境(或完成 u1-l4 源码构建后)里,亲手摸到「两个 wheel + 一个 .so」。

**操作步骤**:

```bash
# 1. 列出两个包的安装信息
pip show ai-dynamo ai-dynamo-runtime 2>/dev/null | grep -E "^(Name|Version|Location)"

# 2. 找到 Rust 扩展文件的真实路径
python3 -c "import dynamo._core; print(dynamo._core.__file__)"

# 3. 对比:Python 包装层来自哪个 wheel
python3 -c "import dynamo.runtime, inspect; print(inspect.getfile(dynamo.runtime))"

# 4. 查看构建期生成的版本戳
python3 -c "import dynamo.frontend._version as v; print(v.__version__)"
```

**需要观察的现象**:步骤 2 打印一个 `_core.cpython-3xx-xxx.so` 之类的二进制路径;步骤 3 打印的 `.py` 路径应位于 `ai-dynamo-runtime` 的安装目录(而非 `ai-dynamo`);步骤 4 输出版本 + git 短 SHA。

**预期结果**:三个路径分别落在「Rust 二进制 / runtime wheel 的 Python 包装 / frontend 组件」三处,与 4.3.2 的 import 链完全吻合。若尚未安装(`No module named dynamo`),请先回 u1-l2 用容器方式体验,或等 u1-l4 完成源码构建——本实践**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**:为什么 `dynamo.frontend._version` 在 GitHub 源码树里搜不到?

**答案**:它由 `hatch_build.py` 的 `VersionWriterHook` 在**构建 wheel 时**生成(写入每个组件目录的 `_version.py`),属于构建产物;源码树只有生成它的钩子。

**练习 2**:同事想在一个环境里同时装 `ai-dynamo[vllm]` 和 `ai-dynamo[sglang]` 做 A/B 对比,可行吗?

**答案**:不可行。`[tool.uv] conflicts` 明确标记这两个 extra 互斥,根因是两者对 NIXL 的版本要求不兼容。正确做法是两个容器/两个 venv 分开装——这也解释了仓库为什么要维护 per-backend 的容器镜像(`container/`)。

**练习 3**:`dynamo.llm`(Python 模块)里定义的类,和 `lib/llm`(Rust crate)里的代码是什么关系?

**答案**:`lib/llm` 的 Rust 代码被编译链接进 `_core` 扩展;`lib/bindings/python/src/dynamo/llm` 下的 Python 文件只是对这些 PyO3 导出再做一层薄包装(加文档、类型、易用 API)。真正的 HTTP 服务、引擎装配逻辑都在 Rust 侧执行。

### 4.4 部署层与外围目录:deploy/、examples/、recipes/、tests/

#### 4.4.1 概念说明

三层之外还有一圈「外围」目录,它们不参与 pip/cargo 产物,但决定了你如何**使用**和**验证** Dynamo:

- `deploy/`:K8s 层本体。`operator/`(Go/Kubebuilder, reconciler 把 DynamoGraphDeployment CRD 变成工作负载)、`helm/charts/`(platform umbrella chart)、`inference-gateway/`(GAIE 拓扑的 ext-proc 与 EPP)、`observability/`(Prometheus/Grafana/Loki 一键监控栈)。
- `examples/`:教程式可运行示例。你在 u1-l2 用的 `examples/backends/sample`、u2 要用的 `examples/custom_backend/hello_world` 都在这里;`examples/router/` 下还有 5 个 workspace 成员级的路由策略示例。
- `recipes/`:生产部署配方,按模型组织(`deepseek-r1/`、`glm-5.2/`、`gpt-oss-120b/`、`qwen3-32b-fp8/` …),每个配方内再按 `引擎/拓扑` 分目录(如 `qwen3-32b-fp8/trtllm/agg/`),核心是 `deploy.yaml`(内含 ConfigMap + DGD)。
- `tests/`:顶层 pytest 套件,按域分子目录(`basic/`、`runtime/`、`frontend/`、`router/`、`serve/`、`deploy/`、`fault_tolerance/`、`kvbm_integration/` …)。
- `benchmarks/`:基于 AIPerf 的压测负载定义;`container/`:Dockerfile 模板体系。

#### 4.4.2 核心流程

把 u1-l1 的「三面架构」与本讲目录对齐,一笔请求在仓库里的行走路线(K8s 生产模式):

```text
客户端
  → deploy/inference-gateway(GAIE 拓扑才有:网关侧选点)
  → components/src/dynamo/frontend(请求面入口,HTTP)
  → lib/bindings/python(_core 边界:Python→Rust)
  → lib/llm + lib/runtime + lib/kv-router(路由、传输、发现)
  → worker:components/src/dynamo/{vllm,sglang,trtllm}(或本地实验的 examples/backends/sample)
  旁路:lib/kvbm-* 管缓存;components/src/dynamo/planner + deploy/operator 管扩缩(控制面)
```

而**本地实验模式**(u1-l2 的 `agg.sh`)把首尾替换为:`examples/backends/sample` 的 worker + file 服务发现,deploy/ 整层缺席——这就是本地零依赖的原因。

#### 4.4.3 源码精读

`deploy/` 的一级内容(实际 `ls` 结果):

> `deploy/` 下可见 `operator/`、`helm/`、`inference-gateway/`、`observability/`、`checkpoint-placeholder/`、`power-agent/`、`pre-deployment/`、`utils/`。其中 `operator/` 是 Go module(含 `api/v1alpha1` 的 CRD 类型与 `internal/controller` 的 reconciler),`inference-gateway/ext-proc` 同时是 Cargo workspace 成员([Cargo.toml:L40](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/Cargo.toml#L40) 把它登记进 members)。

测试目录的分层与 GPU 门控:

> [pyproject.toml:L308-L323](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/pyproject.toml#L308-L323) — markers 里定义了 `gpu_0`(不需要 GPU)到 `gpu_8`(需要 8 卡)的硬件门控,以及 `unit`/`integration`/`fault_tolerance`/`kvbm` 等域标记;配合根目录 `conftest.py` 使用(`pytest -m unit tests/` 是仓库约定的单测入口)。注意 `--strict-markers` 开着:用了未登记的标记会直接报错。

构建与测试的官方速查(承接 u1-l2,预演 u1-l4):

> [AGENTS.md:L171-L199](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/AGENTS.md#L171-L199) — Python 侧五步构建(uv venv → 装 maturin → `cd lib/bindings/python && maturin develop --uv` → 装 gpu_memory_service → `uv pip install -e .`),Rust 侧 `cargo build` / `cargo build -p dynamo-llm`,测试 `cargo test` / `pytest -m unit tests/`。这段命令序列就是 4.3 节两个 wheel 的构建顺序:先 maturin 造 `ai-dynamo-runtime`(含 `_core`),再 editable 安装 `ai-dynamo`。

#### 4.4.4 代码实践

**实践目标**:为 `tests/` 建立索引感,以后能凭目录名直接猜出测试域。

**操作步骤**:

```bash
# 1. 列出测试域
ls tests/

# 2. 看看有没有说明文档
cat tests/README.md 2>/dev/null | head -40

# 3. 数一下各域的测试文件量,找出最厚的域
git ls-files 'tests/**/test_*.py' | cut -d/ -f2 | sort | uniq -c | sort -rn | head
```

**需要观察的现象**:步骤 1 输出 `basic`、`runtime`、`frontend`、`router`、`serve`、`deploy`、`fault_tolerance`、`kvbm_integration` 等子目录;步骤 3 给出各域测试文件计数。

**预期结果**:子目录名与本讲各层对得上(`runtime/` 对应 lib/runtime,`frontend/` 对应 components/frontend,`deploy/` 对应 deploy/ 层),验证「目录结构在测试树上同样成立」。

#### 4.4.5 小练习与答案

**练习 1**:`recipes/qwen3-32b-fp8/trtllm/agg/` 这个路径体现了哪两级组织维度?

**答案**:第一级按模型(`qwen3-32b-fp8`),第二级按「引擎/拓扑」(`trtllm` 引擎 + `agg` 聚合拓扑)。换成 `glm-5.2/vllm/disagg/` 就是另一个模型、另一引擎、分离拓扑。

**练习 2**:为什么 `tests/` 里有一个 `benchmarks/` 子目录,而仓库根目录又有独立的 `benchmarks/`?

**答案**:根目录 `benchmarks/` 是**负载生成与压测剖面**定义(基于 AIPerf,面向性能测量);`tests/benchmarks/` 是 pytest 管理的**测试用例**(面向正确性与回归)。两者名字相同但职责不同——这也是读大仓库时要小心的同名陷阱。

## 5. 综合实践

**任务**:为整个仓库制作一张「一级目录 → 层 / 语言 / 用途」速查表,并在表上标出**一笔 chat/completions 请求会经过的 5 个目录**。这张表将是你后续阅读所有讲义的随身地图。

### 步骤 1:用脚本采集原始数据

下面是一个示例脚本(「示例代码」,只读不写,可保存为任意文件运行或直接粘贴到终端):

```bash
#!/usr/bin/env bash
# repo-map.sh —— 生成 Dynamo 一级目录速查表的原始数据
for d in $(git ls-files | cut -d/ -f1 | sort -u); do
  [ -d "$d" ] || continue
  files=$(git ls-files "$d" | wc -l)
  lang="配置/文档"
  [ -f "$d/Cargo.toml" ] && lang="Rust 为主"
  [ -f "$d/go.mod" ]     && lang="Go 为主"
  [ -f "$d/pyproject.toml" ] && lang="Python 为主"
  ls "$d"/*.py >/dev/null 2>&1 && lang="Python/Shell 混合"
  printf "%-16s %8s 个文件   %s\n" "$d" "$files" "$lang"
done
```

运行 `bash repo-map.sh`,把输出粘进表格工具,再人工补上「层」和「用途」两列(语言探测是粗启发式,以你观察为准)。

### 步骤 2:对照参考答案

| 一级目录 | 层 | 主要语言 | 用途 | 请求经过? |
|----------|----|----------|------|-----------|
| `lib/` | Rust 核心 | Rust | workspace:runtime/llm/kv-router/kvbm-*/mocker/sidecar | ✅ 路由、传输、HTTP 服务(Rust 侧) |
| `components/` | Python 扩展 | Python | ai-dynamo wheel:frontend、各引擎后端、planner、mocker | ✅ frontend 入口与 worker 后端 |
| `deploy/` | K8s 部署 | Go + YAML | operator、helm、inference-gateway、observability | ✅(生产)网关与进程编排 |
| `lib/bindings/python/` | (核心内的飞地) | Rust + Python | dynamo-py3 crate → `_core` 扩展 + Python 包装层 | ✅ Python↔Rust 边界 |
| `examples/` | 外围 | Python/Shell | 可运行示例:sample/mocker 后端、hello_world、custom_encoder | ✅(本地实验)sample worker |
| `recipes/` | 外围 | YAML | 按模型组织的部署配方(deploy.yaml + DGD) | ⭕ 部署期,非运行期 |
| `tests/` | 外围 | Python | 顶层 pytest 套件,按域分层 | ⭕ 验证期 |
| `benchmarks/` | 外围 | Python/YAML | AIPerf 压测负载剖面 | ⭕ 测量期 |
| `container/` | 外围 | Dockerfile | runtime/dev 镜像构建 | ⭕ 打包期 |
| `docs/` + `fern/` | 外围 | MDX | 文档站源码与配置 | ⭕ |
| `scripts/`、`.ai/`、`agent-docs/` 等 | 外围 | 混合 | 工程化脚本与协作者指南 | ⭕ |

「请求经过的 5 个目录」参考答案(生产 K8s 模式):

1. `deploy/` —— inference-gateway(GAIE)或 operator 拉起的 frontend Pod 承接第一跳;
2. `components/` —— `dynamo.frontend` 处理 OpenAI 兼容请求;
3. `lib/bindings/python/` —— 调用穿过 `_core` 进入 Rust;
4. `lib/` —— lib/llm 路由 + lib/runtime 传输把请求送到目标 worker;
5. worker 侧回到 `components/`(vllm/sglang/trtllm 后端);本地实验模式下第 1 与第 5 分别换成「无」与 `examples/`(sample worker)。

### 步骤 3:自检

- 你能否不看你做的表,说出 `dynamo-kv-router` 与 `kvbm-logical` 分别住在哪、属于哪一组?
- 你能否解释为什么 `import dynamo.runtime` 不需要本地装 Rust,但**从源码改 Rust 行为**必须重新 maturin 构建?

如果两个问题都能秒答,本讲目标达成。

## 6. 本讲小结

- 仓库物理上是三层:**Rust 核心**(`lib/` 的 Cargo workspace,35 个成员 crate)、**Python 扩展**(`components/src/dynamo/`,打成 `ai-dynamo` wheel)、**K8s 部署**(`deploy/` 的 Operator/Helm/网关);跨层改动是非平凡变更。
- Rust 层按「runtime 基座 / llm 引擎 / 路由 / KVBM 家族 / mocker+sidecar / 绑定工具」六组记忆;`deploy/inference-gateway/ext-proc` 是唯一住在 `lib/` 外的成员。
- PyO3 扩展 crate `dynamo-py3`(产物名 `_core`)用**空 `[workspace]` 节**把自己排除出顶层 workspace,由 maturin 单独构建,以规避 PyO3 与统一构建的冲突。
- 仓库产出**两个** Python wheel:`ai-dynamo-runtime`(maturin,含 `.so` 与 `dynamo.runtime`/`dynamo.llm` 包装)与 `ai-dynamo`(hatchling,frontend/后端/planner),前者是后者的依赖,版本四处对齐(当前均为 1.5.0)。
- `hatch_build.py` 在构建期把「版本 + git 短 SHA」写进每个组件的 `_version.py`,所以源码树里搜不到这个文件。
- 引擎依赖按 extra(vllm/sglang/trtllm/mocker)拆分且互斥,这是「不同后端不同容器镜像」的根因;外围目录中 `examples/` 重学习、`recipes/` 重生产、`tests/` 与 `benchmarks/` 名字相近但职责不同。

## 7. 下一步学习建议

- **下一讲(u1-l4)「从源码构建与开发环境」**:本讲反复出现的 maturin、`_core`、hatch 构建链将在那一讲亲手跑通——构建两个 wheel、验证 `import dynamo.runtime`、并用 `cargo build -p dynamo-runtime` 体会「Rust 可单独编译」。
- 如果你更想先写代码:可以跳到 **u2-l1(Hello World worker)**,直接用 `dynamo.runtime` 的 API 写一个最小 worker,再回头补构建课。
- 建议现在花五分钟做一次「目录漫游」:`ls lib/kv-router/src`、`ls components/src/dynamo/frontend`、`ls deploy/operator/internal/controller`,只看文件名猜职责——u3 之后你会逐一验证这些猜测。
- 想读官方原始材料的话,`AGENTS.md` 的 Repository Map 与 Build 两节值得反复对照;`pyproject.toml` 的 `[tool.pytest.ini_options]` 则会在 u12-l2(测试体系)详细展开。
