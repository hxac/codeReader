# E2E 测试框架

## 1. 本讲目标

本讲带你看懂 Semantic Router 仓库里 `e2e/` 目录下的端到端（End-to-End）测试框架。读完本讲，你应该能够：

- 说清楚 `cluster / stacks / fixtures / testcases / framework` 这套分层各自负责什么，以及一个请求「从 kind 集群外部打到路由器」的完整测试链路。
- 理解 **profile（环境）** 与 **testcase（用例）** 的分离设计，以及 `testmatrix` 如何用「共享基线 + 独有契约」避免每个重环境都重跑全量用例。
- 理解 **profile 能力声明（`ProfileCapabilities`）**，尤其是 **`LocalImages`** 如何让 E2E 用「当前 checkout 的代码」构建镜像，而不是去拉一个随时可能被覆盖的远程标签。
- 理解 **契约断言（acceptance contract）**：用例跑完后，框架如何用「地板值」对结构化指标做最低通过率校验，从而把 E2E 变成「能抓严重回归、又不至于退化成模型基准门槛」的护栏。

本讲是「测试、E2E 与仿真」单元的首篇，承接 u5-l3（响应体处理与插件回调）之后的请求链路验证视角：E2E 验证的就是那条真实链路在被部署到 Kubernetes 后是否仍然正确。

## 2. 前置知识

- **kind（Kubernetes IN Docker）**：用一个容器模拟一个 Kubernetes 集群，E2E 用它在本机或 CI 里临时拉起一个真集群，跑完就删。
- **端口转发（port-forward）**：把集群内某个 Service 的端口，通过 `kubectl port-forward` 映射到本机一个本地端口，测试进程就能用 `http://localhost:<port>` 访问集群内的服务。本讲的 `fixtures` 就是这层能力的封装。
- **Docker 镜像与 `imagePullPolicy`**：`IfNotPresent` 表示「本地有就不拉」，`Never` 表示「绝不拉远程、只用本地已有的同名镜像」。后者是「本地镜像流」的关键开关——它强制集群用我们刚构建好、`kind load` 进去的镜像，而不会偷偷去公网拉一个标签同名但内容不同的镜像。
- **Helm chart**：Kubernetes 的「包管理器」，一组模板 + 取值文件，渲染成集群里的 Deployment/Service 等资源。E2E 里每个 profile 大多通过 Helm 部署 Semantic Router。
- **profile / testcase 分离**：profile 负责「把环境部署起来」（装哪些 Helm chart、哪些清单），testcase 负责「对部署好的环境发请求并断言」。同一份 testcase 逻辑可以被多个 profile 复用。
- **自注册（self-registration）**：Go 里常见的模式——每个包在 `init()` 函数里把自己「登记」到一个全局 registry，主程序无需硬编码「有哪些实现」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [e2e/cmd/e2e/main.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/cmd/e2e/main.go) | E2E 二进制入口：解析 `-profile` 等命令行参数，构造 profile 与 Runner 并运行。 |
| [e2e/pkg/framework/types.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/framework/types.go) | 框架核心类型：`Profile` 接口、`TestOptions`、`TestResult`、`SetupOptions`。 |
| [e2e/pkg/framework/profile_registry.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/framework/profile_registry.go) | profile 自注册表与 **能力声明**（`ProfileCapabilities`、`LocalImageBuild`）。 |
| [e2e/pkg/framework/runner.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/framework/runner.go) | `Runner`：建集群、构建并加载镜像、跑用例、出报告。 |
| [e2e/pkg/framework/runner_lifecycle.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/framework/runner_lifecycle.go) | `Runner.Run` 的顶层生命周期与清理栈。 |
| [e2e/profiles/all/imports.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/profiles/all/imports.go) | 把全部 profile 自注册进 registry，并声明各自的能力（GPU / LocalImages）。 |
| [e2e/profiles/all/imports_test.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/profiles/all/imports_test.go) | 单测：断言 dashboard profile 的 LocalImages 配置正确，防回归。 |
| [e2e/pkg/testmatrix/testcases.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/testmatrix/testcases.go) | 测试矩阵：共享契约分组（`RouterSmoke`、`BaselineRouterContract`、`DashboardContract`）。 |
| [e2e/pkg/testcases/registry.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/testcases/registry.go) | testcase 注册表；注册时自动包一层 acceptance contract。 |
| [e2e/pkg/testcases/acceptance_contracts.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/testcases/acceptance_contracts.go) | 契约断言：基线地板常量与 `wrapWithAcceptanceContract` 包装器。 |
| [e2e/testcases/anthropic_messages_request.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/testcases/anthropic_messages_request.go) | 一个具体用例：发 POST `/v1/messages` 并断言路由确实发生。 |
| [e2e/pkg/fixtures/session.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/fixtures/session.go) | 端口转发会话：把集群内 Service 暴露成本地端口。 |
| [tools/make/e2e.mk](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/tools/make/e2e.mk) | `make e2e-test` 等 Make 目标的真正定义。 |
| [e2e/profiles/dashboard/dashboard-deployment.yaml](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/profiles/dashboard/dashboard-deployment.yaml) | dashboard 的 E2E 专用 Deployment 清单（`:e2e-test` + `Never`）。 |

## 4. 核心概念与源码讲解

### 4.1 框架组织：cluster / stacks / fixtures / testcases 的分层

#### 4.1.1 概念说明

E2E 框架要回答一个问题：**如何用最小的样板代码，把「部署一个真实的 Semantic Router → 对它发请求 → 断言行为正确」自动化，而且能在多种部署形态（裸 Kubernetes、Istio 网格、KServe、带 GPU 的 Dynamo……）下复用？**

Semantic Router 的答案是 **关注点分离（separation of concerns）**，把职责切成几层：

- **Profile（环境）**：负责「把一套部署环境搭起来」——装哪个 Helm chart、apply 哪些清单、起哪些后端。不同部署形态各有一个 profile。
- **TestCase（用例）**：只负责「对已经部署好的环境发请求并验证」，不关心环境是怎么搭的。因此同一个用例可以被多个 profile 复用。
- **Framework（Runner）**：把上面两者串起来的「指挥棒」——建 kind 集群、构建镜像、驱动 profile.Setup、跑用例、收报告、清理。
- **Stacks / Fixtures**：可复用的部署模块（如 gateway 栈）与「类型化的服务会话」（端口转发封装），让 profile 和 testcase 都不用重复造轮子。

关键直觉：**profile 决定「测什么环境」，testcase 决定「测什么行为」，二者通过 `ServiceConfig`（一条「怎么连到服务」的契约）解耦。**

#### 4.1.2 核心流程

一次 `make e2e-test` 的端到端执行顺序（见 `Runner.Run` → `prepareRuntime` → `runTests` → `finishRun`）：

```
1. 解析参数（-profile 等）→ NewProfileByName 从 registry 取出 profile 工厂并实例化
2. prepareRuntime:
   a. prepareCluster：创建（或复用）kind 集群；RequiresGPU 则开启 GPU 支持
   b. buildAndLoadImages：构建 extproc 镜像 + profile 声明的 LocalImages，全部 kind load 进集群
   c. prepareKubeClient：拿到 kubeconfig、建 Kubernetes clientset
   d. configureHFTokenSecret：若有 HF_TOKEN 就建 Secret 供下载门控模型
   e. setupProfile：调用 profile.Setup（Helm + apply 清单）
3. runTests：按 profile.GetTestCases() 取用例（或 -tests 指定），顺序/并行执行
4. 每个 testcase：通过 ServiceConfig 建端口转发 → 发 HTTP 请求 → 断言 → 关闭转发
5. finishRun：汇总结果、打印、失败时 dump 全部 pod 状态、写 JSON/MD 报告
6. 清理：按 LIFO 栈逆序执行（teardown profile → 删集群，除非 --keep-cluster）
```

清理是关键设计点：`runState` 维护一个 `cleanup` 函数栈，`addCleanup` 入栈、`runCleanup` 逆序出栈，保证「先建的后删」，即便中途失败也能尽量回收资源。

#### 4.1.3 源码精读

**`Profile` 接口**——所有部署形态都要实现的 5 个方法（环境的最小契约）：

[文件路径:e2e/pkg/framework/types.go:L12-L27](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/framework/types.go#L12-L27) 定义了 `Name / Setup / Teardown / GetTestCases / GetServiceConfig`。注意 `Setup` 拿到的是 `*SetupOptions`（含 KubeClient、ImageTag、ValuesFiles），`GetServiceConfig` 返回的就是那条连接契约。

**`Runner.Run` 的顶层编排**——生命周期与清理栈：

[文件路径:e2e/pkg/framework/runner_lifecycle.go:L41-L75](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/framework/runner_lifecycle.go#L41-L75) 说明：`Run` 先 `prepareRuntime`，若 `--setup-only` 则只搭环境不跑用例（方便调试），否则 `runTests`，再 `finishRun`；`defer state.runCleanup()` 保证无论成功失败都清理。

**`runTests` 的用例选取与执行**——profile 与 testcase 的交汇点：

[文件路径:e2e/pkg/framework/runner.go:L106-L166](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/framework/runner.go#L106-L166)。若 `-tests` 非空就只跑指定用例，否则跑 `profile.GetTestCases()` 返回的全部；`Parallel` 决定顺序还是并发，并发用 `sync.WaitGroup` + 互斥锁收集结果。

**用例的注册契约**——`init()` 自注册，注册即自动包契约：

[文件路径:e2e/pkg/testcases/registry.go:L84-L96](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/testcases/registry.go#L84-L96)。`Register` 在写表前会把用例函数 `wrapWithAcceptanceContract` 包一层（第 94 行），重复注册直接 panic（注册期就把名字冲突暴露成启动失败）。

**用例如何连到服务**——`ServiceConfig` 经 fixtures 做端口转发，与部署细节彻底解耦：

[文件路径:e2e/pkg/fixtures/session.go:L25-L55](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/fixtures/session.go#L25-L55)。`OpenServiceSession` 用 `LabelSelector` 或服务名解析出目标，按 `ServiceConfig` 选端口，调 `helpers.StartPortForward` 建转发，返回持有 `localPort` 与 `stop()` 的 `ServiceSession`。用例侧的薄封装见 [e2e/testcases/common.go:L18-L24](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/testcases/common.go#L18-L24) 的 `setupServiceConnection`。

#### 4.1.4 代码实践

**实践目标**：理解一次 E2E 运行的执行顺序，并亲手触发「只搭环境不跑用例」的模式观察框架提示。

**操作步骤**：

1. 阅读 [e2e/pkg/framework/runner_lifecycle.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/framework/runner_lifecycle.go) 的 `Run` 与 `prepareRuntime`，把 4.1.2 的流程图与代码逐一对应。
2. （需本机有 Docker + kind 时）运行 `make e2e-setup`（等价 `./bin/e2e -profile=kubernetes -setup-only`，会自动 keep 集群）。
3. 观察终端输出的 `logSetupOnlyHints` 提示（见 [runner_lifecycle.go:L265-L271](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/framework/runner_lifecycle.go#L265-L271)），它会告诉你如何「跳过 setup 直接重跑用例」和「如何清理」。

**需要观察的现象**：`-setup-only` 模式下框架停在「Profile setup complete」，不进入 `runTests`；提示里给出 `./bin/e2e -profile kubernetes -skip-setup -use-existing-cluster` 这条「复用环境」命令。

**预期结果**：你能在不重搭集群的前提下反复用 `make e2e-test-only` 重跑用例，这正是开发调试时最常用的工作流。若本机不具备运行条件，明确记为「待本地验证」，但流程图与代码对应关系可纯靠阅读完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `runState.runCleanup` 要逆序（LIFO）执行清理函数？

**参考答案**：资源之间存在创建依赖——先建集群才能建 namespace，先建 namespace 才能 apply 清单。逆序清理保证「被依赖者最后删」，避免删 namespace 时还有依赖它的资源没回收，与 u12-l3 里 Operator 的 Owner Reference / Finalizer 是同一思想。

**练习 2**：如果两个用例不小心注册了同一个名字，会发生什么？在哪一行暴露？

**参考答案**：在 [registry.go:L90](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/testcases/registry.go#L90) 直接 `panic("test case %q already registered")`。因为注册发生在 `init()` 阶段，所以是「二进制启动即崩溃」，而非运行时才发现，把名字冲突前移成编译/启动期错误。

---

### 4.2 测试矩阵与 profile：覆盖策略

#### 4.2.1 概念说明

仓库里有二十多个 profile，如果每个 profile 都把全部用例跑一遍，既慢又重复——`kubernetes` 已经验证了「通用路由器契约」，Istio profile 真正该验证的是「网格特有行为（sidecar、mTLS、tracing）」，没必要再跑一遍域分类、语义缓存。

于是引入 **测试矩阵（testmatrix）**：把用例按「语义归属」打包成命名分组，让每个 profile 只声明「我拥有哪几组」。核心约定是 **覆盖所有权（coverage ownership）**：

- `kubernetes` 拥有完整的 **基线路由器契约**（`BaselineRouterContract`）——路由、安全、缓存、决策的所有通用行为。
- 重环境（Istio、production-stack、aibrix……）只保留 **自己独有的契约** + 一个 **共享冒烟用例**（`RouterSmoke`，即 `chat-completions-request`），用来证明「流量在这么重的环境里仍然能走通」。
- dashboard、response-api 这类完全不同面（API 面、Responses API）的 profile 各自拥有独立契约，不共享基线。

这样既保证覆盖、又把 CI 成本压在合理范围。

#### 4.2.2 核心流程

- profile 通过 `GetTestCases()` 返回要跑的用例名列表，通常直接 `return testmatrix.Combine(testmatrix.XxxContract)`。
- `Combine` 在拼接多组时 **保序去重**，避免同一个用例名出现两次（重复跑既慢又可能因端口冲突互相干扰）。
- 所有 profile 在 [imports.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/profiles/all/imports.go) 的 `init()` 里自注册，主程序通过 `RegisteredProfileNames()` 列出全部可用 profile，`-profile` 默认 `kubernetes`。

#### 4.2.3 源码精读

**三个共享契约分组**：

[文件路径:e2e/pkg/testmatrix/testcases.go:L4-L6](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/testmatrix/testcases.go#L4-L6) 定义 `RouterSmoke`——最小共享冒烟，只含 `chat-completions-request`。

[文件路径:e2e/pkg/testmatrix/testcases.go:L9-L40](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/testmatrix/testcases.go#L9-L40) 定义 `BaselineRouterContract`——kubernetes 基线拥有的全量契约，包含 `anthropic-messages-request`、`domain-classify`、`semantic-cache`、`pii-detection`、`jailbreak-detection`、各类 signal-decision 用例、压测、entrypoint-recipe 路由、session 可观测性等。每条用例后往往带 issue 编号注释说明来由。

[文件路径:e2e/pkg/testmatrix/testcases.go:L43-L58](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/testmatrix/testcases.go#L43-L58) 定义 `DashboardContract`——dashboard API 面独有的契约（health、status、config 读写、deploy preview、restart recovery、security policy）。

**`Combine` 的保序去重**：

[文件路径:e2e/pkg/testmatrix/testcases.go:L71-L90](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/testmatrix/testcases.go#L71-L90)。用一个 `seen` map 跳过重复名字，同时保持首次出现的顺序——顺序对可读的测试报告很重要。

**profile 自注册的汇总点**：

[文件路径:e2e/profiles/all/imports.go:L47-L104](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/profiles/all/imports.go#L47-L104)。这里导入全部 profile 包，并在 `init()` 里逐个 `register(name, factory, capabilities)`。注意 `kubernetes` 这个对外名字映射的是 `aigateway.NewProfile()`（历史命名）。命令行入口在 [e2e/cmd/e2e/main.go:L18-L38](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/cmd/e2e/main.go#L18-L38)，`-profile` 的 help 文本直接来自 `RegisteredProfileNames()`。

**dashboard profile 如何用矩阵**：

[文件路径:e2e/profiles/dashboard/profile.go:L117-L118](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/profiles/dashboard/profile.go#L117-L118) 直接 `return testmatrix.Combine(testmatrix.DashboardContract)`——它只跑自己的 API 契约，不碰基线路由契约。

#### 4.2.4 代码实践

**实践目标**：体会「覆盖所有权」如何把不同 profile 的用例集合区分开。

**操作步骤**：

1. 在 [imports.go:L47-L104](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/profiles/all/imports.go#L47-L104) 数出当前注册的 profile 总数。
2. 对比两个 profile 的 `GetTestCases()`：`kubernetes`（基线全量，见 `BaselineRouterContract`）与 `istio`（据 README，只跑 4 个 Istio 专属 + 1 个共享 `chat-completions-request`）。
3. 思考：为什么 `istio` 不重跑 `domain-classify`？

**需要观察的现象 / 预期结果**：基线 profile 跑 20+ 条用例覆盖通用路由；Istio profile 只跑 5 条，把宝贵的 CI 时间花在「网格是否真的注入了 sidecar、mTLS 是否真的生效」这类只有它能验证的事情上。这正是覆盖所有权的价值。本实践为源码阅读型，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：如果想让某个重 profile 在共享冒烟之外，额外多跑一个基线用例（比如 `semantic-cache`），应该改哪里？

**参考答案**：在该 profile 的 `GetTestCases()` 里用 `testmatrix.Combine(testmatrix.RouterSmoke, []string{"semantic-cache"})`。`Combine` 会去重保序，不会与已有名字冲突。不建议直接把它塞回 `BaselineRouterContract`，因为那会让所有引用基线的 profile 都跟着跑，违背覆盖所有权。

**练习 2**：`Combine` 为什么要保序？不保序会怎样？

**参考答案**：测试报告和可读性依赖稳定的用例顺序；若每次运行顺序随机，排查「第几条挂了」会很难。去重则是为了避免同一用例跑两次（端口转发、时间成本、潜在端口冲突）。

---

### 4.3 profile 能力声明与本地镜像（LocalImages）

#### 4.3.1 概念说明

不同 profile 对「运行环境」的要求差别很大：`dynamo` 需要 GPU；`dashboard` 需要一个 dashboard 后端镜像；`hallucination` / `response-api` / `router-replay` 需要一个 mock-vllm 后端镜像。这些「profile 级、但 runner 级」的需求，需要一个统一的声明机制——这就是 **`ProfileCapabilities`（能力声明）**。

它有两个字段：

- `RequiresGPU bool`：runner 据此给 kind 集群开 GPU 支持。
- `LocalImages []LocalImageBuild`：runner 在 `buildAndLoadImages` 阶段，除了无条件构建 extproc 主镜像外，会 **额外为 profile 构建并 `kind load` 这些镜像**。

**为什么需要 LocalImages？** 这是本讲的重点，也是仓库最新一次提交（HEAD `e7cbab82`，PR #2790）的核心改动。在它之前，dashboard profile 引用的镜像是 `ghcr.io/vllm-project/semantic-router/dashboard:latest` 且 `imagePullPolicy: IfNotPresent`——这是一个 **可变的远程标签**：`latest` 会被不断覆盖，`IfNotPresent` 又可能让集群命中本机一个陈旧的 `latest`。结果 CI 测的不一定是「当前 checkout 的代码」，而是「某次偶然构建出来的镜像」。修复办法是用 LocalImages：

1. 让 runner 从 **当前 checkout**（`dashboard/backend/Dockerfile`，build context 为仓库根 `.`）构建出一个 **E2E 专用标签** `ghcr.io/.../dashboard:e2e-test`，并 `kind load` 进集群。
2. 部署清单里把镜像改成 `:e2e-test`、`imagePullPolicy: Never`——强制只用本地刚加载的那个，绝不碰公网。
3. 用一个单测锁住「dashboard profile 的 LocalImages 配置」，防止有人误改回可变标签。

这与 u1-l3 讲过的「本地镜像流（local image flow，一律本地构建、`--image-pull-policy never`）」是同一理念在 E2E 层的落地。

#### 4.3.2 核心流程

能力声明是 **三段式** 串联起来的：

```
profile 自注册时带上 capabilities
        │
        ▼
NewRunner 通过 LookupProfileRegistration 取出 capabilities
        │
        ▼
buildAndLoadImages: 构建 extproc 镜像 → 遍历 LocalImages 逐个 BuildAndLoad
```

关键点：能力是 **声明在注册时、消费在 runner 里** 的，profile 自身的 `Setup()` 完全不感知镜像构建——它只管 apply 清单，清单里写「我会用到 `:e2e-test` 这个本地镜像」即可，因为 runner 已经保证它被加载进集群了。

#### 4.3.3 源码精读

**能力声明的数据结构**：

[文件路径:e2e/pkg/framework/profile_registry.go:L9-L20](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/framework/profile_registry.go#L9-L20) 定义 `LocalImageBuild{Dockerfile, Tag, BuildContext}` 与 `ProfileCapabilities{RequiresGPU, LocalImages}`。

**注册契约（带能力的自注册单元）**：

[文件路径:e2e/pkg/framework/profile_registry.go:L22-L27](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/framework/profile_registry.go#L22-L27) 定义 `ProfileRegistration{Name, Factory, Capabilities}`——一个 profile 的全部登记信息。`NewProfileByName`（[L70-L77](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/framework/profile_registry.go#L70-L77)）按名字取出注册项并调 `Factory()` 实例化。

**runner 消费 LocalImages**：

[文件路径:e2e/pkg/framework/runner.go:L78-L104](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/framework/runner.go#L78-L104) 是 `buildAndLoadImages`。前半段无条件构建 extproc 主镜像（`tools/docker/Dockerfile.extproc`，标签 `extproc:<ImageTag>`，build context `.`，并带本机架构 build-args），后半段 [L92-L101](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/framework/runner.go#L92-L101) 遍历 `profileCapabilities.LocalImages` 逐个构建加载。注意主镜像用的是「仓库内统一 Dockerfile」，而 LocalImages 用的是「profile 自己声明的 Dockerfile」——后者是本次改动的关键。

**dashboard 声明的 LocalImages**：

[文件路径:e2e/profiles/all/imports.go:L39-L45](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/profiles/all/imports.go#L39-L45) 定义 `dashboardLocalImages`：Dockerfile 为 `dashboard/backend/Dockerfile`、Tag 为 `ghcr.io/vllm-project/semantic-router/dashboard:e2e-test`、BuildContext 为 `.`（仓库根，因为该 Dockerfile 的 COPY 指令相对仓库根）。然后 [L53-L57](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/profiles/all/imports.go#L53-L57) 在注册 dashboard 时把它挂到 `ProfileCapabilities{LocalImages: dashboardLocalImages}`。

与之对照，[L31-L37](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/profiles/all/imports.go#L31-L37) 的 `mockVLLMLocalImages`（`tools/mock-vllm/Dockerfile`）被 `hallucination`、`response-api`、`response-api-redis(-cluster)`、`router-replay`、`ml-model-selection`、`vectorstore-registry` 等多个 profile 复用——一份声明，多处共享。

**单测锁住配置（防回归）**：

[文件路径:e2e/profiles/all/imports_test.go:L10-L24](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/profiles/all/imports_test.go#L10-L24) 的 `TestDashboardProfileBuildsLocalImage` 用 `reflect.DeepEqual` 断言 dashboard 注册项的 `Capabilities.LocalImages` 恰好等于期望的三元组。这样如果有人误改回 `:latest` 或漏掉 build context，单测直接挂掉。

**部署清单侧的配合改动**（PR #2790 的另一半）：

[文件路径:e2e/profiles/dashboard/dashboard-deployment.yaml:L19-L20](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/profiles/dashboard/dashboard-deployment.yaml#L19-L20) 把 image 从 `dashboard:latest` 改为 `dashboard:e2e-test`、`imagePullPolicy` 从 `IfNotPresent` 改为 `Never`。这一改与 imports.go 的 LocalImages 声明互为表里：runner 负责把 `:e2e-test` 构建加载进集群，清单负责「只用这个本地镜像、绝不远程拉」。这个清单常量在 profile 里声明于 [e2e/profiles/dashboard/profile.go:L29-L33](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/profiles/dashboard/profile.go#L29-L33)。

#### 4.3.4 代码实践

**实践目标**：把「能力声明 → runner 消费 → 清单引用」三段连起来看懂，并用 git 验证本次改动。

**操作步骤**：

1. 跟读 `dashboardLocalImages`（[imports.go:L39-L45](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/profiles/all/imports.go#L39-L45)）→ dashboard 注册（[L53-L57](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/profiles/all/imports.go#L53-L57)）→ runner 消费（[runner.go:L92-L101](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/framework/runner.go#L92-L101)）→ 清单引用（[dashboard-deployment.yaml:L19-L20](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/profiles/dashboard/dashboard-deployment.yaml#L19-L20)）这条链路。
2. 运行 `git show e7cbab82 -- e2e/profiles/dashboard/dashboard-deployment.yaml e2e/profiles/all/imports.go`，观察 PR #2790 的完整 diff。
3. 回答：dashboard profile 的 `Setup()` 方法里有任何「构建 dashboard 镜像」的代码吗？

**需要观察的现象**：`git show` 会显示三处改动——imports.go 新增 `dashboardLocalImages` 与 dashboard 注册时的 `LocalImages`、imports_test.go 新增单测、dashboard-deployment.yaml 的 image 标签与拉取策略。

**预期结果**：dashboard profile 的 `Setup()`（[profile.go](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/profiles/dashboard/profile.go)）里 **没有** 任何构建镜像的代码——它只 `kubectl apply` 清单。构建发生在 runner 的 `buildAndLoadImages`，由能力声明驱动。这就是「声明在 profile、执行在 runner」的解耦。

#### 4.3.5 小练习与答案

**练习 1**：如果某个新 profile 需要一个全新的本地镜像，应该改哪几个地方？

**参考答案**：(a) 在 `imports.go` 仿照 `dashboardLocalImages` 定义一个 `[]framework.LocalImageBuild`；(b) 在 `init()` 的 `register(...)` 调用里把它放进 `ProfileCapabilities{LocalImages: ...}`；(c) 在该 profile 的部署清单里把 image 写成对应的 `Tag` 并设 `imagePullPolicy: Never`；(d)（推荐）在 `imports_test.go` 加一个 `reflect.DeepEqual` 单测锁住配置。

**练习 2**：为什么 dashboard 用 `BuildContext: "."`（仓库根），而 mock-vllm 用 `"tools/mock-vllm"`？

**参考答案**：取决于对应 Dockerfile 里 `COPY` 指令的基准路径。`dashboard/backend/Dockerfile` 在 COPY 源码时是相对仓库根的（例如 `COPY dashboard/backend/ ./`），所以 build context 必须是 `.`；`tools/mock-vllm/Dockerfile` 自包含，context 用它自己的目录即可。build context 决定了能 COPY 哪些文件进镜像。

---

### 4.4 契约断言：acceptance contract

#### 4.4.1 概念说明

很多 E2E 用例的「正确」不是简单的「返回 200」，而是「准确率达到某个比例」。比如域分类在 65 个样例上要分类对一定比例、语义缓存要有一定命中率、越狱检测要拦住足够多的攻击。但 LLM 类行为的准确率天然有波动，把门槛设成「100% 才算过」会让 E2E 频繁误报。

Semantic Router 的解法是 **契约断言（acceptance contract）**：为关键用例定义 **地板值（floor）**——一个「低于它就是严重回归」的最低通过率。注释说得很直白：这些地板值「catch severe behavioral regressions without turning E2E into a model-benchmark gate」——抓严重退化，但别把 E2E 变成模型基准门槛。

机制上，它用 **装饰器模式**：用例注册时自动被 `wrapWithAcceptanceContract` 包一层；包过的用例会拦截用例写入的「结构化指标（details）」，用例跑完后用 contract 函数对 details 做校验，不达标就把 contract 错误叠加进测试错误。

#### 4.4.2 核心流程

```
Register(name, tc)
   └─ tc.Fn = wrapWithAcceptanceContract(name, tc.Fn)   // 注册即包装
            │
运行时：包装后的 Fn
   ├─ 把 opts.SetDetails 替换成「截获 details」的版本
   ├─ 调用原 Fn（用例照常跑、照常写 details）
   └─ 用例返回后：acceptanceContractForProfile(profile, name) 取该 profile 下该用例的契约
        ├─ 找不到契约 → 不校验，原样返回（非基线 profile 跑同名用例不触发门槛）
        └─ 找到契约 → contract(details)：算百分比，对比 minimum，不达标返回错误
```

两个关键设计：

- **按作用域（profile）绑定**：契约表 `acceptanceContractsByProfile` 目前只对 `kubernetes`（基线）profile 生效。同样跑 `domain-classify`，在 `kubernetes` 下要过 60% 地板，在其他 profile 下不触发（因为没有该 profile 的契约条目，`wrapWithAcceptanceContract` 直接返回原函数）。
- **地板而非基准**：常量值（60%、40%、70%、95%……）刻意宽松，只防严重退化。

#### 4.4.3 源码精读

**注册时自动包装**：

[文件路径:e2e/pkg/testcases/registry.go:L84-L96](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/testcases/registry.go#L84-L96)，第 94 行 `tc.Fn = wrapWithAcceptanceContract(name, tc.Fn)`——每个用例无差别地被包一层，是否真校验由运行时的 profile 作用域决定。

**包装器的拦截逻辑**：

[文件路径:e2e/pkg/testcases/acceptance_contracts.go:L142-L174](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/testcases/acceptance_contracts.go#L142-L174)。它先用 `hasScopedAcceptanceContract` 判断该用例在任何 profile 下有没有契约，没有就直接返回原函数（零成本短路）；有则在运行时把 `SetDetails` 替换成截获版本，跑完用例后用 `evaluateAcceptanceContract` 校验，**仅当用例本身没出错时** 才叠加 contract 错误（`if err == nil && contractErr != nil`）——避免错误叠加淹没真正的失败原因。最后把截获的 details 回灌给原始 `SetDetails`，保证报告里仍能看到指标。

**地板常量**：

[文件路径:e2e/pkg/testcases/acceptance_contracts.go:L17-L28](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/testcases/acceptance_contracts.go#L17-L28) 定义 `minBaselineDomainClassificationAccuracy = 60.0`、`minBaselineSemanticCacheHitRate = 40.0`、`minBaselineJailbreakDetectionRate = 70.0`、`minBaselineSequentialStressSuccessRate = 95.0` 等地板值。

**契约表（按 profile 作用域）**：

[文件路径:e2e/pkg/testcases/acceptance_contracts.go:L54-L136](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/testcases/acceptance_contracts.go#L54-L136)。`baselineAcceptanceContracts` 用 `applyFlatRateContract` 为每条用例声明「分子/分母键 + 地板值」，并经由 [L138-L140](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/testcases/acceptance_contracts.go#L138-L140) 的 `acceptanceContractsByProfile` 只挂在 `baselineRouterContractProfile = "kubernetes"` 下。压测用例 `chat-completions-progressive-stress` 用专门的 `applyProgressiveStressContract`，按 10/20/50 QPS 分阶段各有地板（[L30-L34](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/testcases/acceptance_contracts.go#L30-L34)），再算总体。

**对照：一个不走 acceptance contract 的用例如何自行断言**：

并非所有用例都用地板契约。[e2e/testcases/anthropic_messages_request.go:L68-L90](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/testcases/anthropic_messages_request.go#L68-L90) 的 `anthropic-messages-request` 用例走的是 **函数内直接断言**：① 状态码必须 200；② body 非空且是合法 JSON；③ `x-vsr-selected-decision` 或 `x-vsr-selected-model` 响应头 **至少一个非空**——这一条是最关键的「契约」：它证明请求 **确实经过了路由管线**，而不是绕过路由直接打到后端。注释明确说只断言「路由器能控制的属性」，不断言 mock 后端产生的自由文本。

#### 4.4.4 代码实践

**实践目标**：理解「地板契约」与「函数内断言」两种验证风格的区别与各自适用场景。

**操作步骤**：

1. 阅读 [acceptance_contracts.go:L142-L174](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/testcases/acceptance_contracts.go#L142-L174) 的包装器，回答：如果用例本身返回了错误，contract 还会执行吗？
2. 阅读 [anthropic_messages_request.go:L31-L93](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/testcases/anthropic_messages_request.go#L31-L93)，列出它做了哪几条断言。
3. 思考：为什么 `anthropic-messages-request` 不适合用「地板契约」，而 `domain-classify` 适合？

**需要观察的现象 / 预期结果**：
- 用例本身出错时 contract 不执行（`if err == nil && contractErr != nil`），先暴露用例自身错误。
- `anthropic-messages-request` 三条断言：status==200、body 合法 JSON、路由头非空——都是 **确定性** 断言（要么发生要么没发生），适合函数内写死。
- `domain-classify` 是 **统计性** 断言（65 个样例对多少个），天然有波动，适合地板契约。

本实践为源码阅读型，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：如果 `domain-classify` 的准确率从 90% 掉到 55%，E2E 会怎样？掉到 65% 呢？

**参考答案**：55% 低于地板 60%，contract 返回 `"... below minimum 60.00%"` 错误，用例判失败，CI 红。65% 在地板之上（≥60%），用例通过——这正是「地板而非基准」的用意：只抓严重退化，允许正常波动。

**练习 2**：为什么 contract 要按 profile 作用域绑定，而不是全局生效？

**参考答案**：不同 profile 的环境差异很大（比如某个 profile 用更弱的模型、或后端是 mock）。把地板只绑在 `kubernetes` 基线上，既保证基线行为有护栏，又避免在其他 profile 上因环境差异误触地板而频繁误报。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个贯穿任务（对应本讲规格里的 practice_task）。

### 任务：追踪一个用例从「kind 集群部署」到「断言路由正确」的完整链路，并解释 dashboard 的本地镜像构建

**背景**：选择用例 `anthropic-messages-request`（它在 `BaselineRouterContract` 里，归 `kubernetes` 基线 profile 拥有）。

**第一步：入口与 profile 解析**
- `make e2e-test` 的入口在 [tools/make/e2e.mk:L21-L42](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/tools/make/e2e.mk#L21-L42)：先 `build-e2e`（`cd e2e && go build -o ../bin/e2e ./cmd/e2e`），再用一长串 `-profile=$(E2E_PROFILE) ...` 调起二进制，默认 `E2E_PROFILE=kubernetes`。
- 二进制入口 [e2e/cmd/e2e/main.go:L83-L90](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/cmd/e2e/main.go#L83-L90)：`NewProfileByName(*profile)` 从 registry 取出 `kubernetes`（实际是 `aigateway.NewProfile()`），再 `NewRunner` + `runner.Run`。

**第二步：部署栈**
- `prepareRuntime` 建 kind 集群、构建加载镜像、建 kube client、`profile.Setup`（Helm 装 Semantic Router + apply Envoy/Gateway 清单）。`kubernetes` profile 不需要 GPU、也不需要 LocalImages（它的能力声明是空的 `ProfileCapabilities{}`，见 [imports.go:L49](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/profiles/all/imports.go#L49)），所以只构建标准的 extproc 镜像。

**第三步：发起断言**
- `runTests` 取 `BaselineRouterContract` 列表，其中含 `anthropic-messages-request`。
- 该用例（[anthropic_messages_request.go:L31-L93](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/testcases/anthropic_messages_request.go#L31-L93)）先用 `setupServiceConnection` 建端口转发，再向 `http://localhost:<port>/v1/messages` POST 一个 Anthropic Messages 体（`model: "MoM"` 触发决策引擎），最后断言 status 200、body 是合法 JSON、路由头非空。
- 因为它不在基线 `baselineAcceptanceContracts` 表里（该表只有 domain/cache/pii/压力等统计型用例），所以不会被地板契约二次校验——它靠函数内三条确定性断言把关。

**第四步（关键）：解释 dashboard profile 如何通过 LocalImages 用 checkout 构建后端镜像**
- dashboard profile 注册时带 `ProfileCapabilities{LocalImages: dashboardLocalImages}`（[imports.go:L53-L57](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/profiles/all/imports.go#L53-L57)），其中 `dashboardLocalImages` 指向 `dashboard/backend/Dockerfile`、标签 `ghcr.io/.../dashboard:e2e-test`、build context `.`。
- runner 的 `buildAndLoadImages` 在构建完 extproc 后，遍历 `LocalImages`，从 **当前 checkout** 构建出 `:e2e-test` 并 `kind load` 进集群（[runner.go:L92-L101](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/framework/runner.go#L92-L101)）。
- dashboard 的部署清单（[dashboard-deployment.yaml:L19-L20](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/profiles/dashboard/dashboard-deployment.yaml#L19-L20)）写死用 `:e2e-test` 且 `imagePullPolicy: Never`，所以集群用的是「刚从 checkout 构建出来的那个本地镜像」，而不会去拉一个可变的远程 `:latest`。
- 一个单测（[imports_test.go:L10-L24](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/profiles/all/imports_test.go#L10-L24)）锁住这套配置，防止回归到可变标签。

**产出要求**：画一张时序图，横轴是时间，把「make e2e-test → build-e2e → runner.Run → 建 kind → buildAndLoadImages(extproc + dashboard:e2e-test) → profile.Setup → runTests → 端口转发 → POST /v1/messages → 三条断言 → 报告 → 清理」串起来；在图上特别标注「dashboard 镜像是从 checkout 构建的本地镜像」这一支。

**预期结果**：你能向别人讲清楚两件事——(1) 一个用例在 kind 集群里从部署到断言的完整路径；(2) LocalImages 如何让 E2E 测的是「当前代码」而非「某个可变标签」。若本机无 Docker/kind，时序图与讲解仍可纯靠源码阅读完成（构建与运行步骤记为「待本地验证」）。

## 6. 本讲小结

- E2E 框架用 **关注点分离** 切层：`Profile` 管部署、`TestCase` 管验证、`Runner` 管编排，三者经 `ServiceConfig`（端口转发契约）解耦，使同一用例可跨 profile 复用。
- **测试矩阵（testmatrix）** 用命名契约分组 + `Combine` 保序去重实现 **覆盖所有权**：`kubernetes` 拥有全量基线契约，重环境只保留独有契约 + 一个共享冒烟。
- **能力声明 `ProfileCapabilities`** 把 profile 级需求（GPU、LocalImages）声明在注册时、消费在 runner；`LocalImages` 让 E2E 从 **当前 checkout** 构建镜像（如 dashboard 的 `:e2e-test`），配合清单的 `imagePullPolicy: Never`，杜绝「测到可变远程标签」的风险（HEAD `e7cbab82` / PR #2790）。
- **契约断言（acceptance contract）** 用装饰器在注册时自动包装用例，按 profile 作用域对统计型指标做 **地板值** 校验（如域分类 60%、越狱 70%、压测 95%），抓严重回归而不退化成基准门槛。
- 用例有两种验证风格：统计型走地板契约（`domain-classify` 等），确定性型走函数内断言（`anthropic-messages-request` 断言 status/JSON/路由头）。
- 清理用 LIFO 栈逆序回收（`runState.runCleanup`），失败也能尽量复原；`--setup-only` / `--skip-setup` 支撑「搭一次环境、反复跑用例」的调试工作流。

## 7. 下一步学习建议

- **u14-l2（Fleet 仿真器）**：从「验证路由正确性」转向「规划 GPU 舰队与评估路由策略」，理解 `fleet-sim` 如何用离散事件仿真评估不同路由策略的吞吐与成本。
- **u14-l3（训练与评估）**：E2E 的地板值（如域分类 60%）背后是分类器质量，本讲推荐接着看 `src/training` 的后训练与评估脚本，理解这些准确率数字是怎么被生产出来的。
- **延伸阅读**：若想深入部署侧，可结合 u12-l2（Helm/K8s 部署）与 u12-l3（Operator/CRD）看 E2E 各 profile 部署的「真实目标」到底是什么；也可阅读 [e2e/pkg/stacks/gateway](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/e2e/pkg/stacks/gateway) 下被多个 profile 复用的 gateway 栈实现，体会「可复用部署模块」的设计。
