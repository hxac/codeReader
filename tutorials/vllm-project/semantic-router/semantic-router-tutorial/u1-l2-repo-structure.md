# 仓库结构与目录组织

## 1. 本讲目标

上一讲（u1-l1）我们建立了 vLLM Semantic Router（下称 SR）的概念底座：它是一个位于客户端与模型后端之间的可编程路由层，按 `entrypoint → recipe（signals + projections + decisions）→ select backend(s) → apply plugins` 的流程运行，并以 Envoy ExtProc 控制面落地。

本讲我们要换一双「工程师的眼睛」来看这个项目：**当你把仓库 clone 下来后，这么大一个多语言 monorepo，东西放在哪里、为什么这样放？** 学完本讲，你应当能够：

- 看懂 SR 仓库的「资产边界」约定：哪个顶层目录归谁所有，能放什么、不能放什么。
- 在脑子里建立一张「目录 → 子系统职责」的映射表，知道 Go 路由器、Python CLI、推理绑定、配置、部署、面板、E2E、工具各自住在哪。
- 定位每个子系统的「入口目录」，知道下次想读某段逻辑时该从哪个门进去。
- 识别项目自己标注的「高变更风险区（High-Risk Areas）」与「热点文件（Hotspots）」，避免在不该下手的地方乱改。

> 本讲是「读地图」，不是「开车」。我们只认路，不深入任何一个子系统的内部实现——那是后续讲义（u4 起）的任务。

## 2. 前置知识

- **monorepo（单体仓库）**：把多个语言、多个子项目的代码放在同一个 Git 仓库里管理。SR 是典型的多语言 monorepo：Go、Python、Rust、TypeScript 都在里面。
- **资产边界（Asset Boundary）**：一种工程约定——每个目录只「拥有」一类资产，新增文件按归属和生命周期放置，而不是按文件后缀随便堆。
- **catch-all（兜底目录）**：很多项目会有一个根 `docs/` 或 `scripts/` 用来塞「不知道放哪儿」的文件。SR **故意没有**这种目录，这是它最重要的结构约定之一。
- **入口目录（Entry Point）**：一个子系统里最先被加载/执行的目录或文件，比如 Go 的 `cmd/`、Python CLI 的 `cli/main.py`。

如果你还没读上一讲，建议先读 u1-l1，因为本讲会把目录和上一讲建立的 `Signals→Projections→Decisions` 心智模型一一对应起来。

## 3. 本讲源码地图

本讲只读两份「地图型」源文件，它们本身就是项目对自身结构的权威描述：

| 文件 | 作用 |
| --- | --- |
| [tools/agent/docs/repo-map.md](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/agent/docs/repo-map.md) | 项目结构的最权威说明：资产边界、核心子系统、入口点、热点、高风险区。 |
| [AGENTS.md](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/AGENTS.md) | 仓库根的「编码智能体入口」，含简版仓库地图、支持环境与非协商规则。 |
| [tools/agent/structure-rules.yaml](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/agent/structure-rules.yaml) | 可执行的结构契约：根文件白名单、各语言 glob、依赖禁令。本讲用它佐证「根目录能放什么」。 |

> 提示：这三份文件不是普通文档，而是被 CI/工具机直接消费的「可执行契约」。改仓库结构时，必须同步更新它们。

## 4. 核心概念与源码讲解

本讲对应三个最小模块：**仓库资产边界**、**核心子系统映射**、**入口目录定位**。下面逐一拆解。

### 4.1 仓库资产边界

#### 4.1.1 概念说明

「资产边界」回答的问题是：**顶层每一个目录归谁所有、能放什么？** SR 用一组明确的归属规则代替了「随手放」的习惯。这样做的好处是：

- 每个目录有单一责任人（ownership），见 `OWNER` 文件。
- 新增文件按**归属和生命周期**放置，而不是按后缀堆砌。
- 不存在「什么都能往里扔」的 `docs/`、`scripts/` 兜底目录，避免结构腐烂。

#### 4.1.2 核心流程

一个新文件落到仓库时，按下面的判定顺序找家：

```text
新文件
  │
  ├─ 是否是仓库级契约/社区文件/安装器？ → 放仓库根（受白名单约束）
  │
  ├─ 是否是路由器运行配置/片段/recipe？ → config/
  │
  ├─ 是否是「创建或配置部署目标」的产物？ → deploy/
  │
  ├─ 是否是面向公众的文档？ → website/（唯一的文档树）
  │
  ├─ 是否是构建自动化/开发工具/测试辅助？ → tools/
  │
  └─ 都不是？ → 按语言和子系统进 src/ 或对应 *-binding/
```

注意一个反直觉点：**`website/` 才是唯一的公开文档树**，仓库根没有 `docs/`。

#### 4.1.3 源码精读

`repo-map.md` 开篇就给出了四大资产所有者：

[tools/agent/docs/repo-map.md:5-14](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/agent/docs/repo-map.md#L5-L14) — 定义了 `config/`、`deploy/`、`website/`、`tools/` 四个目录各自「拥有」什么，并在末尾明确「仓库故意没有根 `docs/` 或 `scripts/` 兜底目录，请按归属和生命周期放置新资产」。

根目录本身也不是随便能放文件的。`AGENTS.md` 的 Repository Map 末尾说明：

[AGENTS.md:38-40](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/AGENTS.md#L38-L40) — 「根目录只包含仓库级契约、社区元数据和工具要求的入口；可执行白名单在 `tools/agent/structure-rules.yaml`，不要新增根级兜底文件」。

这份「可执行白名单」确实存在，而且很短。根目录允许出现的文件是逐个列出的：

[tools/agent/structure-rules.yaml:14-32](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/agent/structure-rules.yaml#L14-L32) — `root_files.allowed` 列出了根目录允许的全部文件（如 `AGENTS.md`、`Makefile`、`install.sh`、`README.md` 等）。任何不在列表里的根文件都会被结构门禁拒绝。

对照实际仓库根，能看到的就是这些文件，没有 `docs/`、没有 `scripts/`：

```text
AGENTS.md  CODE_OF_CONDUCT.md  CONTRIBUTING.md  GOVERNANCE.md
LICENSE    Makefile            OWNER            README.md
SECURITY.md  install.sh
```

另外有一个容易踩的约定（称为「Root Contract」）：**工具自己的配置要跟着工具走，而不是堆到根目录。** 例如：

- GitHub 归属元数据放 `.github/CODEOWNERS`，不放根。
- CRD 文档生成配置放 `tools/crd/ref-docs.yaml`。
- 编辑器/助手文件（`.cursorrules`、`CLAUDE.md` 等）被忽略，`AGENTS.md` 是唯一的智能体入口。

[tools/agent/docs/repo-map.md:16-29](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/agent/docs/repo-map.md#L16-L29) — 说明了根契约以及「工具专属配置归工具」的原则，并指出 `tools/agent/structure-rules.yaml` 拥有可执行的根文件白名单。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证「根目录没有兜底目录」这一约定。
2. **操作步骤**：
   - 在仓库根执行 `ls -1p | grep -v /`，列出所有根级**文件**（不含目录）。
   - 再执行 `ls -1d */`，列出所有根级**目录**。
   - 打开 [tools/agent/structure-rules.yaml:14-32](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/agent/structure-rules.yaml#L14-L32)，把 `root_files.allowed` 列表和你的根文件清单逐项对照。
3. **需要观察的现象**：根目录文件清单与白名单高度一致；根目录**不会**出现 `docs/`、`scripts/`、`utils/` 这类兜底目录。
4. **预期结果**：根目录只包含仓库级契约（`LICENSE`、`GOVERNANCE.md` 等）、社区健康文件（`CODE_OF_CONDUCT.md`、`SECURITY.md`）、公开安装器（`install.sh`）和工具要求的入口（`Makefile`、`AGENTS.md`）。
5. 如无法运行命令，明确标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果你想新增一个面向用户的安装教程 Markdown，应该放在哪个目录？为什么不能放仓库根？

> **参考答案**：放 `website/`。因为 `website/` 是唯一的公开文档树，而仓库根只接受白名单内的契约/社区/安装器文件；新增根级 Markdown 会被结构门禁拒绝。

**练习 2**：`.cursorrules` 和 `CLAUDE.md` 这类助手配置文件被项目如何对待？

> **参考答案**：被忽略。`AGENTS.md` 是唯一的智能体入口；编辑器/助手文件不进入根白名单，项目明确不维护它们。

---

### 4.2 核心子系统映射

#### 4.2.1 概念说明

知道了「边界」之后，下一步是搞清楚每个目录里住着哪个子系统。SR 是个跨语言大仓，子系统很多。`repo-map.md` 的 Core Subsystems 段把它们归纳成了几条主线。我们把它们和上一讲建立的 `Signals→Projections→Decisions` 心智模型对应起来，方便记忆。

#### 4.2.2 核心流程

把顶层目录按「运行时角色」分成五组，更容易理解：

| 组别 | 目录 | 角色 | 主要语言 |
| --- | --- | --- | --- |
| 路由内核 | `src/semantic-router/` | Go 路由器、Envoy ExtProc 服务、配置加载、路由逻辑 | Go |
| 本地编排 | `src/vllm-sr/` | Python CLI、配置生成、Docker 编排、本地启动流程 | Python |
| 推理绑定 | `candle-binding/`、`ml-binding/`、`nlp-binding/`、`onnx-binding/`、`openvino-binding/` | Rust 支撑的嵌入/分类/ML/ONNX 推理能力 | Rust (+ CGO) |
| 配置 | `config/` | 规范路由配置、可复用片段、完整 recipe、后端运行示例 | YAML/DSL |
| 部署 | `deploy/` | 仅存放「创建或配置部署目标」的产物（Helm、K8s、KServe、OpenShift、operator、本地代理） | YAML/Go |
| 面板 | `dashboard/` | 前端（React）与管理后端（Go） | TypeScript/Go |
| 测试 | `e2e/` | kind/Kubernetes 端到端框架与 profile 矩阵 | Go |
| 仿真/训练 | `src/fleet-sim/`、`src/training/` | GPU 舰队仿真；分类器后训练与评估脚本 | Python |
| 工具 | `tools/` | 构建自动化、开发工具、冒烟测试、模型助手、安全扫描、agent harness | 混合 |
| 文档 | `website/` | 唯一公开文档树 | Markdown |

> 上一讲回顾：`entrypoint → recipe → select backend → apply plugins` 这条主链路，主要由 `src/semantic-router/`（执行决策）和 `config/`（声明 recipe）两块协同完成。本讲先记住「谁住哪儿」，具体调用链等 u5 再展开。

#### 4.2.3 源码精读

Core Subsystems 的权威定义在 `repo-map.md`：

[tools/agent/docs/repo-map.md:33-54](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/agent/docs/repo-map.md#L33-L54) — 列出全部核心子系统，包括 Go 路由器、共享 Milvus 拨号层、Python CLI、fleet 仿真器、Rust 推理绑定、dashboard、operator、training、e2e、`tools/make/`（规范自动化入口）和 `tools/agent/`（编码智能体清单与结构规则）。

`AGENTS.md` 给了一个更精简的「读者友好版」地图，适合作为速查表：

[AGENTS.md:28-36](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/AGENTS.md#L28-L36) — 用一行一句话概括了 `src/semantic-router/`、`src/vllm-sr/`、`config/`、各 `*-binding/`、`dashboard/`、`deploy/`、`e2e/`、`tools/`、`website/` 各自的职责。

这里有两个值得特别注意的细节：

1. **共享的 Milvus 层**：`src/semantic-router/pkg/milvus/` 被 memory、cache、vectorstore、replay、extproc memory wiring 共同复用（对应 issue #1601）。它说明 SR 在子系统之间会主动抽取共享层，而不是各自重复实现。
2. **推理绑定不止三个**：`repo-map.md` 的 Core Subsystems 段行文列了 `candle-binding/`、`ml-binding/`、`nlp-binding/`，但仓库里实际还有 `onnx-binding/` 和 `openvino-binding/`（`AGENTS.md` 的地图补上了 onnx）。实际目录如下：

```text
candle-binding/  ml-binding/  nlp-binding/  onnx-binding/  openvino-binding/
```

#### 4.2.4 代码实践

1. **实践目标**：把目录映射到上一讲的 `Signals→Projections→Decisions` 心智模型。
2. **操作步骤**：
   - 对照 [AGENTS.md:28-36](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/AGENTS.md#L28-L36) 的简版地图。
   - 思考：声明「信号/投影/决策」的 recipe 文件住哪个目录？执行这些决策的运行时住哪个目录？提供嵌入向量的推理后端住哪个目录？
3. **需要观察的现象**：你会发现「声明」与「执行」是分离的——配置在 `config/recipes/`，执行在 `src/semantic-router/`，推理能力在 `*-binding/`。
4. **预期结果**：能写出类似这样的三行映射：recipe 声明 → `config/`；决策求值 → `src/semantic-router/pkg/decision/`；嵌入推理 → `candle-binding/` 等。
5. 如某目录内部结构不确定，标注「待确认」并在后续讲义（u5、u8）核实。

#### 4.2.5 小练习与答案

**练习 1**：`deploy/` 目录「只拥有」哪一类资产？它和 `config/` 的边界在哪？

> **参考答案**：`deploy/` 只拥有「创建或配置部署目标」的产物（Helm chart、K8s 清单、KServe、OpenShift、operator、本地代理清单）。边界在于：`config/` 拥有路由器运行配置和 recipe，而 `deploy/` 拥有把这些东西部署到集群/本地的产物。

**练习 2**：为什么 memory、cache、vectorstore 都会复用 `src/semantic-router/pkg/milvus/`？

> **参考答案**：它们都需要连 Milvus 向量库。项目把「拨号、集合 ensure/load、重试」这些公共能力抽成一个共享层，避免每个子系统各写一份连接逻辑（issue #1601）。

---

### 4.3 入口目录定位

#### 4.3.1 概念说明

知道「谁住哪儿」之后，最后一个问题是：**想读某个子系统，从哪扇门进去？** 这就是「入口目录定位」。SR 在 `repo-map.md` 里专门列了 Main Entry Points，给每个常见任务指了起点。

#### 4.3.2 核心流程

按「我想做什么」找入口：

```text
我想……                           → 从这里开始
─────────────────────────────────────────────────────
构建本地镜像                       → tools/make/docker.mk（make vllm-sr-dev）
看根命令路由                       → Makefile
本地起整套服务（serve）            → src/vllm-sr/cli/main.py
看 serve 如何编排运行时命令         → src/vllm-sr/cli/commands/runtime.py
看本地服务的启动/状态管理           → src/vllm-sr/cli/core.py
看容器/镜像/服务适配（barrel）     → src/vllm-sr/cli/container_cli.py
跑端到端测试                       → tools/make/e2e.mk
读 Go 路由器主程序                 → src/semantic-router/cmd/main.go
```

#### 4.3.3 源码精读

Main Entry Points 的权威清单：

[tools/agent/docs/repo-map.md:93-101](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/agent/docs/repo-map.md#L93-L101) — 列出本地镜像构建、根命令路由、本地 serve、运行时命令编排、本地服务启动/状态、容器 barrel、E2E 驱动等全部入口，并附相对链接。

对应的「支持环境」入口命令在 `AGENTS.md`：

[AGENTS.md:42-47](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/AGENTS.md#L42-L47) — 定义了四种环境的启动命令：`cpu-local`、`amd-local`、`nvidia-local`、`ci-k8s`。例如 `cpu-local` 是 `make vllm-sr-dev` 然后 `vllm-sr serve --image-pull-policy never`。

把入口和实际目录对照，可以画出 Go 路由器的「门」：

```text
src/semantic-router/
├── cmd/            ← 命令入口（main.go 在这里）
│   ├── main.go
│   ├── runtime_bootstrap.go
│   ├── dsl/        ← DSL 命令行工具（validate/compile/...）
│   ├── fusioneval/
│   └── wasm/
├── pkg/            ← 路由器主要业务包（extproc/decision/...）
├── internal/
├── go.mod          ← Go 模块声明
└── hack/
```

> 这张表对应 [src/semantic-router/cmd/main.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/cmd/main.go)：Go 路由器的真正入口在 `cmd/main.go`，启动序列的逐段讲解留到 u4-l1。

最后要识别两类「危险区」，避免盲目改动：

[tools/agent/docs/repo-map.md:103-118](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/agent/docs/repo-map.md#L103-L118) — **High-Risk Areas**（高变更风险区）：`src/vllm-sr/**`、`src/fleet-sim/**`、`deploy/operator/**`、`src/semantic-router/**`、`tools/make/**`、`e2e/pkg/**`、`e2e/cmd/**`、`e2e/testcases/**`、`tools/agent/**` 与 `AGENTS.md`。这些区域的改动影响面大（本地开发体验、E2E 覆盖、CRD schema 等）。

与之相对的是 **Known Hotspots**（热点文件）：

[tools/agent/docs/repo-map.md:56-91](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/agent/docs/repo-map.md#L56-L91) — 这些是已知的「大文件/重编排骨」（如 `pkg/config/config.go`、`pkg/extproc/processor_req_body.go`）。项目的规则是：**热点是技术债，不是先例**；改动热点文件时，其职责不应继续膨胀，应优先「抽取优先（extraction-first）」。

> 高风险区 = 目录级（影响面广）；热点 = 文件级（容易膨胀）。两者是不同维度的警示。

#### 4.3.4 代码实践

1. **实践目标**：为每个常见任务定位正确的入口文件，并圈出高风险区。
2. **操作步骤**：
   - 打开 [tools/agent/docs/repo-map.md:93-101](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/agent/docs/repo-map.md#L93-L101)。
   - 用 `git ls-files src/semantic-router/cmd/` 查看 `cmd/` 下实际有哪些入口（应能看到 `main.go`、`dsl/`、`fusioneval/`、`wasm/`）。
   - 在仓库根执行 `make help 2>/dev/null | head -40`（或直接读 `Makefile`），看根 Makefile 暴露了哪些目标。
3. **需要观察的现象**：每个「我想做什么」都能在 Main Entry Points 里找到唯一起点；Go 路由器的入口集中在 `src/semantic-router/cmd/`。
4. **预期结果**：你能在不看笔记的情况下回答「想读 Go 路由器主程序从哪个文件开始」「想本地起服务用哪条命令」。
5. 如某条命令在本地无法运行（缺依赖），明确标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：「高变更风险区」和「热点文件」有什么区别？各举一例。

> **参考答案**：高风险区是**目录级**警示，指影响面广的目录，如 `src/semantic-router/**`（影响路由行为和广泛 E2E 覆盖）。热点是**文件级**警示，指容易膨胀的大文件，如 `pkg/config/config.go`（配置类型大表）。

**练习 2**：项目说改动热点文件时应遵循什么原则？

> **参考答案**：遵循「抽取优先（extraction-first）」——把窄职责抽到邻近文件，而不是继续往热点里加逻辑。`AGENTS.md` 明确「Legacy hotspots are debt, not precedent」（热点是债，不是先例）。

---

## 5. 综合实践

把本讲三个模块串起来，完成一张「仓库认知地图」：

1. **绘制目录 → 职责映射表**：对照 [tools/agent/docs/repo-map.md:33-54](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/agent/docs/repo-map.md#L33-L54)（Core Subsystems）和 [AGENTS.md:28-36](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/AGENTS.md#L28-L36)（简版地图），用 Markdown 表格列出仓库每一个顶层目录的一句话职责。至少应覆盖：`src/semantic-router`、`src/vllm-sr`、`src/fleet-sim`、`src/training`、各 `*-binding/`、`config`、`deploy`、`dashboard`、`e2e`、`tools`、`website`、`bench`、`perf`、`paper`。
2. **标注高风险区**：在你的映射表里，给属于 High-Risk Areas 的目录打上 ⚠️ 标记，参考 [tools/agent/docs/repo-map.md:103-118](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/agent/docs/repo-map.md#L103-L118)。
3. **标注热点文件**：在映射表的「路由内核」一行下，额外列出 3 个 Known Hotspots 文件（参考 [tools/agent/docs/repo-map.md:56-91](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/tools/agent/docs/repo-map.md#L56-L91)），并各写一句「它为什么会成为热点」。
4. **连线上一讲**：在你的表里，用箭头标出 `entrypoint → recipe → select backend → apply plugins` 这条主链路分别落在哪几个目录（提示：recipe 声明在 `config/`，链路执行在 `src/semantic-router/`）。

> 完成后，这张表就是你后续阅读源码时的「总览导航」。建议把它保存下来，每学完一篇讲义就回填更细的子目录。

## 6. 本讲小结

- SR 是多语言 monorepo，顶层目录按**资产边界**归属：`config/` 拥有配置与 recipe，`deploy/` 拥有部署产物，`website/` 是唯一公开文档树，`tools/` 拥有构建与开发工具。
- 仓库**故意没有**根 `docs/`、`scripts/` 兜底目录；根文件受 `tools/agent/structure-rules.yaml` 的可执行白名单约束。
- 核心子系统映射：路由内核 `src/semantic-router/`（Go）、本地编排 `src/vllm-sr/`（Python）、推理绑定 `*-binding/`（Rust）、面板 `dashboard/`、测试 `e2e/`、仿真/训练 `src/fleet-sim` 与 `src/training`。
- 入口目录定位：本地镜像 `tools/make/docker.mk`、本地服务 `src/vllm-sr/cli/main.py`、Go 路由器 `src/semantic-router/cmd/main.go`、E2E 驱动 `tools/make/e2e.mk`。
- **High-Risk Areas** 是目录级风险警示（如 `src/semantic-router/**`、`deploy/operator/**`），**Known Hotspots** 是文件级膨胀警示（如 `pkg/config/config.go`）；改动热点要遵循「抽取优先」。
- 共享层（如 `pkg/milvus/`）说明 SR 会主动抽取子系统公共能力，而不是让各方重复实现。

## 7. 下一步学习建议

本讲只认了路，还没真正「开车」。建议接下来：

- **想跑起来**：读 u1-l3（安装与本地运行），动手执行 `make vllm-sr-dev` 与 `vllm-sr serve`，把本讲的入口命令实际跑一遍。
- **想看配置长什么样**：直接打开 `config/config.yaml` 和 `config/recipes/balance/`，对照 u3-l1（config.yaml v0.3 结构）。
- **想深入路由内核**：等学到 u4-l1（main.go 启动序列）时，再回到本讲的「入口目录」表，从 `src/semantic-router/cmd/main.go` 这扇门正式进入 Go 路由器内部。
- **想持续用这张地图**：保留本讲综合实践产出的映射表，每学完一篇讲义就回填更细的子目录，把它养成你的私人「仓库导航」。
