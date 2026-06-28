# 测试体系：单元、Fake 与集成测试

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 NGF 的三层测试体系（单元测试 / conformance 一致性测试 / system 功能与 NFR 测试）各跑在哪、验证什么。
- 理解 NGF 的单元测试**完全靠 fake 注入**，而不启动真实 Kubernetes API server——并知道为什么规划文档里常说的「envtest 集成测试」在本仓库里其实并不存在（这是一个需要纠正的常见误解）。
- 掌握 counterfeiter 生成 fake 的完整链路：从 `//counterfeiter:generate` 指令到 `make generate`，再到 `xxx_fakes` 包里的产物。
- 读懂一个真实的 Ginkgo + fake 单元测试（以 `handler_test.go` 为范本），并能照着它的模式给新代码补一个单元测试。
- 了解 conformance 测试如何挂载上游 Gateway API 官方测试套件，以及 `make run-conformance-tests` 的入口。

本讲依赖 u1-l3（构建与运行），因为你需要先知道 `make` 这个统一开发入口。

## 2. 前置知识

在进入源码前，先用通俗语言澄清几个术语——它们是本讲的基础。

- **单元测试（unit test）**：针对一个函数或一个小模块，验证它在给定输入下产出正确输出。NGF 的单元测试要求快、可并行、不依赖外部环境（不连真实集群）。
- **BDD（Behavior-Driven Development）**：一种用「描述行为」来组织测试的风格。NGF 用 [Ginkgo](https://onsi.github.io/ginkgo/)（提供 `Describe`/`Context`/`It` 等容器）和 [Gomega](https://onsi.github.io/gomega/)（提供 `Expect(...).To(Equal(...))` 等断言）。
- **fake / mock**：被测代码通常依赖一些「协作者」（接口）。测试时不接真实协作者，而是塞进一个「假实现」，让你能**控制它的返回值、记录它被调用了几次、用什么参数**。这样被测逻辑就被隔离出来单独验证。
- **counterfeiter**：一个自动生成 fake 的工具。你只要在接口上方写一行指令，它就能把整个接口的 fake 代码生成出来。
- **table-driven test（表驱动测试）**：把「多组输入与期望」列成一张表，循环跑同一个断言，避免为每个用例复制粘贴代码。
- **envtest**：controller-runtime 提供的一种「在测试里拉起一个真实（但临时的）etcd + kube-apiserver」的能力。**本讲的一个重点是：NGF 并没有用它**——这条认知会在 4.3 节专门澄清。
- **conformance（一致性）测试**：Gateway API 官方维护的一组标准化测试，用来验证某个实现（如 NGF）是否正确实现了 Gateway API 规范。NGF 把这套官方测试当作「集成测试」的主干。
- **build tag（构建标签）**：Go 文件顶部的 `//go:build xxx` 注释，决定该文件只在指定条件下才参与编译。NGF 用它把 conformance 测试与普通单元测试隔开。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| `docs/developer/testing.md` | 测试方法的「官方说明书」，讲清单元测试规范、覆盖率、conformance 入口 |
| `internal/controller/controller_suite_test.go` | `internal/controller` 包的 Ginkgo 引导文件（很多人误以为它是 envtest 入口，其实不是） |
| `internal/controller/state/state_suite_test.go` | 另一个 Ginkgo 引导文件，用来对照说明「每个包都有这样一个 suite 文件」 |
| `internal/controller/state/change_processor.go` | 含 `//go:generate` 与 `//counterfeiter:generate` 指令，是 fake 生成的源头 |
| `internal/controller/state/statefakes/fake_change_processor.go` | counterfeiter **自动生成**的 fake，演示 fake 的结构 |
| `internal/controller/handler_test.go` | 一个完整的「真实单元测试」范本，演示 fake 注入与断言 |
| `Makefile` | `generate`（驱动 counterfeiter）与 `unit-test`（跑单元测试）目标 |
| `tests/README.md` | conformance / system 测试的运行手册 |
| `tests/conformance/conformance_test.go` | conformance 测试入口，挂载上游 Gateway API 官方套件 |
| `tests/go.mod` | 证明 `tests/` 是一个**独立 Go 模块**，与控制面主模块分离 |

## 4. 核心概念与源码讲解

### 4.1 测试体系总览：三层测试与一个重要澄清

#### 4.1.1 概念说明

NGF 的测试分成三层，它们验证的东西不同、运行环境也不同：

1. **单元测试**：验证控制面内部每一个函数/模块的正确性。纯 Go，**不连集群**，靠 fake 隔离依赖。由 `make unit-test` 驱动。
2. **conformance（一致性）测试**：验证 NGF 作为一个整体是否正确实现了 Gateway API 规范。在一个**真实的 kind 集群**里跑上游官方测试套件。由 `tests/` 模块里的 `make run-conformance-tests` 驱动。
3. **system（功能 / NFR）测试**：验证 NGF 在真实系统里的端到端行为，包括功能用例（functional，跑在 kind）和性能/长稳等非功能需求（NFR/longevity，跑在 GKE）。

#### 4.1.2 核心流程

三层测试的关系可以这样理解：

```text
速度最快、最细粒度          环境最真实、最接近生产
   单元测试  ──────────►  conformance 测试  ──────────►  system 测试
 (fake，无集群)        (真实 kind 集群)            (kind / GKE，端到端)
 make unit-test        make run-conformance-tests     make test / NFR
```

- 单元测试覆盖**代码正确性**，跑得最快，是日常开发的主力（u4~u8 各模块都靠它保证质量）。
- conformance 测试覆盖**规范符合度**，保证 NGF 没有把 Gateway API 实现错。
- system 测试覆盖**真实行为与性能**，包括 graceful recovery、升级、长稳、WAF 等场景。

#### 4.1.3 源码精读

官方说明书 `docs/developer/testing.md` 把单元测试规范讲得很清楚：

- 「所有导出接口都必须有 BDD 风格测试」，用 Ginkgo + Gomega：[docs/developer/testing.md:14-17](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/testing.md#L14-L17) ——说明 BDD 是主流风格。
- 「尽量在导出接口层测试」，让测试与内部实现解耦：[docs/developer/testing.md:19-22](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/testing.md#L19-L22)。
- 「用 counterfeiter 生成 mock」：[docs/developer/testing.md:34-37](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/testing.md#L34-L37)。
- 「单元测试要可并行」（标准 Go 测试加 `t.Parallel()`，Ginkgo 自动并行）：[docs/developer/testing.md:39](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/testing.md#L39)。
- 跑单元测试与看覆盖率：[docs/developer/testing.md:45-59](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/testing.md#L45-L59)。

`tests/README.md` 开篇就把测试分成 conformance 与 system 两类（注意这两类都在 `tests/` 模块里，**不在**主模块的单元测试里）：[tests/README.md:3-8](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/README.md#L3-L8)。

#### 4.1.4 代码实践

**实践目标**：建立三层测试的全局印象，并验证 conformance/system 测试确实在独立模块里。

**操作步骤**：

1. 在仓库根目录执行 `make unit-test`，观察它只编译并运行 `./cmd/... ./internal/...`（见 4.3.3 的 Makefile 引用），不会触碰 `tests/`。
2. 进入 `tests/` 目录，执行 `make help`，对比看到的 `run-conformance-tests`、`test` 等**集群级**目标。

**需要观察的现象**：

- `make unit-test` 不需要 kind 集群也能跑完（即使本机没有 Docker/kind）。
- `tests/` 下的目标都要求先有集群（README 的「Common steps」要先 `create-kind-cluster`）。

**预期结果**：你应当直观看到「单元测试零集群依赖、conformance/system 测试强集群依赖」的分界。待本地验证（取决于本机是否装了 kind/Docker）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 NGF 不把 conformance 测试也放进 `make unit-test`？

> **答案**：conformance 测试需要一个**真实运行 NGF 的集群**才能验证「客户端发请求 → NGINX 转发」的端到端行为，而 `make unit-test` 的设计目标是「快、可并行、无外部依赖」。两者环境要求根本不同，所以分属不同模块与不同 make 目标。

---

### 4.2 fake 生成：counterfeiter

#### 4.2.1 概念说明

被测代码（比如事件处理器 `eventHandlerImpl`）会依赖一堆协作者：变更处理器、配置生成器、NGINX 下发器、状态更新器……它们在 NGF 里都是**接口**。单元测试时，我们不想真的去构建图、真的生成 NGINX 配置、真的下发——那样就变成集成测试了。于是我们给每个接口造一个「假实现」（fake），由测试控制它返回什么、记录它被怎么调用。

counterfeiter 就是自动生成这些 fake 的工具。它的好处是：接口一变（加方法、改签名），重新生成即可，所有 fake 自动跟上，不会出现「手写 mock 忘了更新」的问题。

#### 4.2.2 核心流程

fake 的生成链路是：

```text
在接口源文件顶部写两行指令
   //go:generate go tool counterfeiter -generate
   //counterfeiter:generate . ChangeProcessor
              │
              ▼
   make generate   (=  go generate ./...)
              │
              ▼
   counterfeiter 扫描每个 //counterfeiter:generate 指令
              │
              ▼
   在同包的 <pkg>fakes/fake_<interface>.go 生成 fake
              │
              ▼
   测试 import 这个 fake，注入被测对象
```

生成的 fake 对每个接口方法都提供三类能力：

- `XxxReturns(...)`：设置「调用时返回什么」。
- `XxxCallCount()`：查询「被调用了几次」。
- `XxxArgsForCall(i)`：查询「第 i 次调用传入的参数」。

这三类能力正好覆盖了「控制行为 + 验证交互」两种测试需求。

#### 4.2.3 源码精读

以 `ChangeProcessor` 接口为例（这是 u4-l4 讲过的变更处理器）。它的源文件顶部有两行指令：

- `//go:generate go tool counterfeiter -generate` 告诉 `go generate` 在本包运行 counterfeiter：[internal/controller/state/change_processor.go:34](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor.go#L34)。
- `//counterfeiter:generate . ChangeProcessor` 是 counterfeiter 自己的指令，`.` 表示「当前包」，`ChangeProcessor` 是要造假的接口名：[internal/controller/state/change_processor.go:36](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor.go#L36)。

被造假的接口本身定义在：[internal/controller/state/change_processor.go:40-58](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor.go#L40-L58)（含 `CaptureUpsertChange`/`CaptureDeleteChange`/`Process`/`GetLatestGraph`/`ForceRebuild` 五个方法）。

生成的 fake 文件头部明确写着「机器生成、勿手改」：[internal/controller/state/statefakes/fake_change_processor.go:1-2](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/statefakes/fake_change_processor.go#L1-L2)。

fake 的结构体为**每个方法**都准备了一组字段——stub（要执行的函数）、互斥锁、调用参数记录、返回值设置：[internal/controller/state/statefakes/fake_change_processor.go:15-54](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/statefakes/fake_change_processor.go#L15-L54)。以 `CaptureDeleteChange` 方法为例，它先记录调用参数、再执行 stub（若设置了）：[internal/controller/state/statefakes/fake_change_processor.go:56-68](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/statefakes/fake_change_processor.go#L56-L68)；配套的查询方法 `CaptureDeleteChangeCallCount()`：[internal/controller/state/statefakes/fake_change_processor.go:70-74](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/statefakes/fake_change_processor.go#L70-L74) 与 `CaptureDeleteChangeArgsForCall(i)`：[internal/controller/state/statefakes/fake_change_processor.go:82-87](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/statefakes/fake_change_processor.go#L82-L87)。

> 注意：fake 内部用了 `sync.RWMutex` 保护每个方法——这正是 `make unit-test` 带 `-race` 标志能安全检测数据竞争的前提。

`make generate` 是驱动这一切的入口，它就是 `go generate ./...`：[Makefile:147-149](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/Makefile#L147-L149)。

全仓库有多少个这样的 fake？它们都落在 `<包>/<包>fakes/fake_<接口>.go` 命名规则下，例如 `configfakes.FakeGenerator`、`agentfakes.FakeNginxUpdater`、`provisionerfakes.FakeProvisioner`、`statusfakes.FakeGroupUpdater` 等——下一节的范本测试会用到它们。

#### 4.2.4 代码实践

**实践目标**：亲手走一遍「改指令 → 生成 fake → 看 diff」的过程，理解 fake 是产物而非手写。

**操作步骤**：

1. 在仓库根目录执行 `make generate`（即 `go generate ./...`）。
2. 执行 `git status`，确认 `internal/controller/state/statefakes/fake_change_processor.go` 等 fake 文件**没有出现在改动列表里**（因为它们已经是最新生成的）。
3. （可选）临时在 `ChangeProcessor` 接口里加一个空方法 `Dummy()`，再跑 `make generate`，观察 fake 里自动多出 `DummyStub`/`DummyCallCount()` 等代码；随后用 `git checkout` 还原源码。

**需要观察的现象**：counterfeiter 按接口方法一对一地生成「stub + 锁 + 调用记录 + 返回值」四件套。

**预期结果**：你会确信「fake 永远是接口的忠实镜像」，因此**永远不要手改 fake 文件**。待本地验证（需要本机有 Go 工具链）。

#### 4.2.5 小练习与答案

**练习 1**：如果接口新增了一个方法却忘了重新 `make generate`，单元测试会怎样？

> **答案**：fake 文件不会自动出现新方法，任何依赖新方法的测试将编译失败（因为 fake 不满足新接口）。这正是 counterfeiter 的安全网——它把「接口与 fake 不同步」从隐蔽的运行时错误，变成了显眼的编译错误。

**练习 2**：`XxxReturns` 与 `XxxCallCount` 分别服务于「控制行为」和「验证交互」中的哪一个？

> **答案**：`XxxReturns(...)` 服务于**控制行为**（让 fake 返回你想要的值，从而驱动被测代码走不同分支）；`XxxCallCount()` / `XxxArgsForCall(i)` 服务于**验证交互**（断言被测代码确实以预期参数调用了协作者）。

---

### 4.3 单元测试如何组织：Ginkgo 套件引导 + fake 注入（澄清 NGF 不用 envtest）

#### 4.3.1 概念说明

每个 Go 测试包要跑 Ginkgo，都需要一个**引导文件**（suite bootstrap）：它定义一个普通的 `TestXxx(t *testing.T)` 入口，在里面调用 `RegisterFailHandler(Fail)` 把 Ginkgo 的失败接上 `testing` 框架，再用 `RunSpecs(t, "Xxx Suite")` 启动 Ginkgo。**这个引导文件本身不包含任何测试逻辑**，真正测试写在同包的 `*_test.go` 里（用 `Describe`/`It` 等组织）。

> **重要澄清（纠正一处常见误解）**：规划文档与本系列其它讲义里多次提到「NGF 用 controller-runtime 的 **envtest** 做集成测试」。经过对全仓库的核实，**NGF 的 Go 代码里并没有使用 envtest**（`sigs.k8s.io/controller-runtime/pkg/envtest` 在 `internal/` 与 `cmd/` 下零引用）。`controller_suite_test.go` 等所谓的「suite test」文件**只是 Ginkgo 引导文件**，里面没有任何「拉起临时 apiserver」的逻辑。NGF 单元测试的隔离手段是**纯 fake 注入**：所有协作者接口用 counterfeiter fake 替换，连 Kubernetes client 都用 controller-runtime 自带的**内存版** `client/fake`（不连真实 apiserver）。真正的「集成测试」由 4.4 节的 conformance 测试和 `tests/` 模块的 functional 测试承担，它们跑在**真实 kind 集群**上。结论：**NGF 的单元测试层 = Ginkgo + counterfeiter fake + 内存 client/fake，不含 envtest**。

#### 4.3.2 核心流程

一个 NGF 单元测试的标准写法（以测 `eventHandlerImpl` 为例）：

```text
1. 在 suite 引导文件里：TestXxx → RegisterFailHandler(Fail) → RunSpecs
2. 在 *_test.go 里：
   a. 声明一组 fake 字段（*xxxFakes.FakeYyy）
   b. BeforeEach：new 出每个 fake，设置默认返回值，注入到被测对象
   c. It：触发被测方法
   d. Expect：用 CallCount/ArgsForCall 断言交互，或断言被测返回值
```

关键设计：被测对象的所有依赖都是接口字段，测试通过替换这些字段完成隔离——这就是「在导出接口层测试」原则的落地。

#### 4.3.3 源码精读

`controller_suite_test.go` 全文就是引导文件，没有任何 envtest 痕迹：[internal/controller/controller_suite_test.go:10-14](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/controller_suite_test.go#L10-L14) ——三行：开并行、注册失败处理器、运行 specs。对照另一个包的引导文件 `state_suite_test.go`，结构完全一样：[internal/controller/state/state_suite_test.go:10-14](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/state_suite_test.go#L10-L14)。结论：**每个测试包都有一个这样的「suite 文件」，它的唯一职责是启动 Ginkgo**。

`make unit-test` 目标定义如下，注意它带 `-race`（检测数据竞争）、`-shuffle=on`（随机化执行顺序，避免测试间隐性依赖），并产出覆盖率：[Makefile:239-242](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/Makefile#L239-L242)。

现在看一个**真实的单元测试范本** `handler_test.go`（它测的正是 u4-l3 讲的事件处理器）：

- 顶部 import 了一长串 fake 包（counterfeiter 产物）以及 controller-runtime 的内存 client：[internal/controller/handler_test.go:30-49](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler_test.go#L30-L49)。注意第 23 行 `sigs.k8s.io/controller-runtime/pkg/client/fake`——这是**内存版** client，**不是** envtest。
- 在 `Describe` 里声明 fake 字段：[internal/controller/handler_test.go:56-61](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler_test.go#L56-L61)（`fakeProcessor`、`fakeGenerator`、`fakeNginxUpdater`、`fakeProvisioner`、`fakeStatusUpdater`、`fakeK8sClient` 等）。
- 一个典型的辅助断言函数 `expectReconfig`，用 `XxxCallCount()` 与 `XxxArgsForCall(0)` 验证「配置被生成、被下发、状态被回写」这一整条交互链：[internal/controller/handler_test.go:71-97](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler_test.go#L71-L97)。
- `BeforeEach` 里 new 出每个 fake 并设置默认返回值（`ProcessReturns`、`GetLatestGraphReturns` 等），这就是「注入」动作：[internal/controller/handler_test.go:123-129](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler_test.go#L123-L129)。

这个范本完整展示了 NGF 单元测试的三大特征：**Ginkgo 组织、counterfeiter fake 控制行为、CallCount/ArgsForCall 验证交互，全程不碰真实集群**。

#### 4.3.4 代码实践

**实践目标**：照着 `handler_test.go` 的模式，亲手补一个用 fake 控制行为并验证交互的 Ginkgo spec。

**操作步骤**：

1. 打开 `internal/controller/handler_test.go`，在 `Describe("eventHandler", ...)` 块内新增一个 `It`。
2. 在该 `It` 里：
   - 用 `fakeGenerator.GenerateReturns(<期望的文件列表>)` 控制「配置生成」的返回；
   - 触发被测方法（参考同文件已有用例对 `eventHandlerImpl` 的调用方式）；
   - 用 `Expect(fakeNginxUpdater.UpdateConfigCallCount()).To(Equal(1))` 断言「下发确实发生了一次」，并用 `fakeNginxUpdater.UpdateConfigArgsForCall(0)` 取出传入的文件做进一步断言。
3. 在该文件所在目录执行 `go test -run TestController -race ./internal/controller/...`。

**需要观察的现象**：

- 因为 `fakeGenerator` 的返回由你完全控制，你可以让「生成」成功或失败，观察 `eventHandlerImpl` 是否相应地调用或跳过 `UpdateConfig`。
- 改动 `GenerateReturns` 的返回值后，断言里取到的文件也应随之变化。

**预期结果**：新增的 spec 能通过，证明你已经掌握了「fake 控制行为 + CallCount 验证交互」的模式。待本地验证（需要 Go 工具链；具体被测方法的调用签名请以同文件已有用例为准）。

> 说明：这是一个「在现有测试文件里加用例」的实践，目的是让你在真实代码上体验 NGF 的测试范式；不会改动任何产品源码。

#### 4.3.5 小练习与答案

**练习 1**：为什么 NGF 单元测试不需要（也不用）envtest？

> **答案**：因为被测对象（如 `eventHandlerImpl`）的所有依赖都被设计成接口，测试用 counterfeiter fake 全部替换；连 Kubernetes client 也用内存版 `client/fake`。既然没有任何代码需要真实 apiserver，就没必要用 envtest 去拉起一个。envtest 会显著拖慢测试、引入环境依赖，与 NGF「单元测试快、可并行、零集群依赖」的目标相悖。

**练习 2**：`make unit-test` 里的 `-shuffle=on` 有什么用？

> **答案**：它随机化测试执行顺序，用来暴露「测试之间存在隐性依赖」（比如 A 测试改了某全局状态、B 测试依赖它）这类 bug。如果一个测试只在固定顺序下通过，shuffle 后就会暴露问题。

---

### 4.4 conformance 测试：跑通 Gateway API 官方一致性套件

#### 4.4.1 概念说明

conformance 测试回答的问题是：「NGF 是否正确实现了 Gateway API 规范？」它由 Gateway API 项目官方维护一组标准化用例，任何实现（NGF、Envoy Gateway、Istio……）都可以挂载这套用例来证明自己的符合度。NGF 不重写这些用例，而是**直接调用上游套件**，把自己作为一个实现接入。

这部分代码在 `tests/` 目录——一个**独立 Go 模块**（有自己的 `go.mod`），与控制面主模块分开。这样 conformance/system 测试可以单独管理对上游 Gateway API 的依赖版本（甚至临时切到 `main` 分支测最新规范）。

#### 4.4.2 核心流程

conformance 测试的运行链路：

```text
准备好 kind 集群 + MetalLB（给 LoadBalancer 分配 IP）
        │
        ▼
部署 NGF（make install-ngf-local-build）
        │
        ▼
make run-conformance-tests
        │
        ▼
tests/conformance/conformance_test.go  (带 //go:build conformance 标签)
        │
        ▼
调用上游 suite.NewConformanceTestSuite → Setup → Run(tests.ConformanceTests)
        │
        ▼
上游用例通过 NGF 的 GatewayClass 创建资源、发请求、断言行为
        │
        ▼
生成一致性报告 YAML
```

#### 4.4.3 源码精读

conformance 测试入口文件顶部有构建标签，把它和普通单元测试隔开：[tests/conformance/conformance_test.go:1](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/conformance/conformance_test.go#L1)（`//go:build conformance`）。它 import 了上游 Gateway API 的 conformance 包：[tests/conformance/conformance_test.go:39-43](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/conformance/conformance_test.go#L39-L43)。

`TestConformance` 是主入口，它配置选项（超时、可用/不可用地址、实现信息）后，调用上游套件的三段式：[tests/conformance/conformance_test.go:59-95](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/conformance/conformance_test.go#L59-L95)。其中：

- 创建测试套件：[tests/conformance/conformance_test.go:90-91](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/conformance/conformance_test.go#L90-L91)（`suite.NewConformanceTestSuite(opts)`）。
- 安装上游基线资源：[tests/conformance/conformance_test.go:93](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/conformance/conformance_test.go#L93)（`testSuite.Setup(t, tests.ConformanceTests)`）。
- 跑全部官方用例：[tests/conformance/conformance_test.go:94-95](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/conformance/conformance_test.go#L94-L95)（`testSuite.Run(t, tests.ConformanceTests)`）。
- 产出报告：[tests/conformance/conformance_test.go:97-111](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/conformance/conformance_test.go#L97-L111)（`testSuite.Report()` 序列化为 YAML 写文件）。

同文件还有一个推理扩展（Inference）的 conformance 入口（呼应 u12-l1）：[tests/conformance/conformance_test.go:114-141](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/conformance/conformance_test.go#L114-L141)。

`tests/README.md` 给出完整的运行步骤，conformance 部分从安装 NGF 到跑测试：[tests/README.md:141-263](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/README.md#L141-L263)。其中跑 Gateway conformance 的命令是 `make run-conformance-tests`：[tests/README.md:229-233](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/README.md#L229-L233)。system（功能/NFR）测试是同一模块里的另一类：[tests/README.md:265-289](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/README.md#L265-L289)。

#### 4.4.4 代码实践

**实践目标**：跑通一次 conformance 测试，理解它如何用上游官方用例验证 NGF。

**操作步骤**（全部在 `tests/` 目录，参考 `tests/README.md`）：

1. `make create-kind-cluster` 创建本地 kind 集群。
2. `make build-images load-images TAG=$(whoami)` 构建并加载 NGF 镜像。
3. `make install-ngf-local-build` 把 NGF 装进集群。
4. `make deploy-metallb` 给 LoadBalancer 分配可用 IP。
5. `make build-test-runner-image` 构建 conformance 测试运行镜像。
6. `make run-conformance-tests` 跑 Gateway 一致性测试。
7. `make cleanup-conformance-tests` 与 `make uninstall-ngf` 清理。

**需要观察的现象**：

- 上游用例会通过 NGF 的 GatewayClass 创建 Gateway/HTTPRoute 等资源，发真实 HTTP 请求并断言响应。
- 测试结束后会生成一份 conformance 报告（YAML），列出 NGF 通过/支持的特性与 profile。

**预期结果**：NGF 能通过其声明的 conformance profile（如 Gateway 核心 profile）。待本地验证（需要本机有 kind、Docker 与足够资源；具体可用的 profile 以报告输出为准）。

> 说明：若只想本地快速验证部分用例，可用 `GINKGO_LABEL` / `GINKGO_FLAGS` 聚焦某个标签或用例（见 `tests/README.md` 的「Common test amendments」）。

#### 4.4.5 小练习与答案

**练习 1**：conformance 测试为什么要放在独立的 `tests/` Go 模块，而不是主模块？

> **答案**：因为 conformance 测试需要灵活切换对**上游 Gateway API** 的依赖版本（比如临时切到 `main` 测最新规范，或替换成自己的 fork），如果放进主模块会污染控制面的依赖。独立模块让这种「临时改上游依赖」的操作（`make update-go-modules`）只影响测试，不影响产品代码。

**练习 2**：`//go:build conformance` 这个构建标签起什么作用？

> **答案**：它让 `conformance_test.go` 只在显式带 `conformance` 标签时才编译。这样普通的 `go test ./...`（包括 `make unit-test`）不会去编译、更不会去跑这个需要真实集群的文件，从而把「无集群的单元测试」和「需集群的 conformance 测试」彻底隔离。

---

## 5. 综合实践

把本讲的三块知识串起来，完成一个**「从加一个函数到给它配齐测试」**的小任务：

1. **阅读规范**：先读 [docs/developer/testing.md](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/testing.md)，记住「导出接口要 BDD 测试 + 用 counterfeiter + 表驱动 + 可并行」这几条原则。
2. **选一个待测对象**：在 `internal/controller/state/conditions/conditions_test.go` 或 `internal/controller/ngfsort/sort_test.go` 里挑一个**纯函数**作为目标（这两个文件逻辑独立、无外部依赖，最适合练手）。
3. **写一个表驱动单元测试**：用 Go 原生 `subtests`（`t.Run` + `t.Parallel()`）+ Gomega 断言，覆盖正常、边界、错误三类输入。
4. **跑测试**：执行 `go test -race -shuffle=on ./internal/controller/state/conditions/...`（或你选的包），确认通过。
5. **看覆盖率**：执行 `make unit-test`，打开生成的 `cover.html`，找到你新测的函数，确认它被覆盖。
6. **（进阶）fake 注入练习**：参照 4.3.4，在 `handler_test.go` 里加一个用 `fakeGenerator.GenerateReturns(...)` 控制行为、用 `fakeNginxUpdater.UpdateConfigCallCount()` 验证交互的 spec。

> 这个任务让你同时体验「纯函数的表驱动测试」「覆盖率工具」「fake 注入的交互测试」三种 NGF 最常用的测试写法。注意：第 6 步会临时改动测试文件，请在本地分支进行，完成后还原，不要把练习改动提交。

## 6. 本讲小结

- NGF 测试分三层：**单元测试**（`make unit-test`，无集群、靠 fake）、**conformance 测试**（`tests/` 模块，真实 kind 集群，跑上游 Gateway API 官方套件）、**system 测试**（`tests/` 模块，functional 跑 kind、NFR/longevity 跑 GKE）。
- 单元测试用 **Ginkgo（BDD）+ Gomega** 组织，规范要求「导出接口层测试 + 表驱动 + 可并行」，由 `docs/developer/testing.md` 明确规定。
- **fake 由 counterfeiter 自动生成**：接口源文件顶部写 `//go:generate go tool counterfeiter -generate` 与 `//counterfeiter:generate . 接口名`，`make generate` 即产出 `<包>fakes/fake_<接口>.go`，提供 `XxxReturns`/`XxxCallCount`/`XxxArgsForCall` 三类能力。
- 每个测试包有一个 **Ginkgo 引导文件**（`*_suite_test.go`，如 `controller_suite_test.go`），它只做 `RegisterFailHandler(Fail)` + `RunSpecs`，不含任何测试逻辑。
- **重要澄清**：NGF **不使用 envtest**。单元测试靠 counterfeiter fake + 内存版 `client/fake` 隔离，所谓「集成测试」实为 `tests/` 模块里跑在真实集群上的 conformance / functional 测试。
- conformance 测试入口 `tests/conformance/conformance_test.go` 带 `//go:build conformance` 标签，直接调用上游 `suite.NewConformanceTestSuite` → `Setup` → `Run`，由 `make run-conformance-tests` 驱动。

## 7. 下一步学习建议

- **想看更多 fake 注入范本**：阅读 `internal/controller/nginx/agent/agent_test.go`、`internal/controller/provisioner/provisioner_test.go`，它们展示了更复杂的「多 fake 协作」测试。
- **想深入并发与可靠性**：进入 u13-l2，看 NGF 如何用「事件双缓冲 + leader 选举 + 冲突排序」保证可靠性——这些机制正是 `make unit-test` 带 `-race` 要守护的对象。
- **想动手扩展 NGF**：进入 u13-l3（二次开发），把「定义新 CRD → 图处理 → 配置生成 → 补单元测试」的完整改动链路走一遍，本讲的测试范式是其中最后一环。
- **想理解 conformance 背后的规范**：阅读上游 [Gateway API conformance 文档](https://gateway-api.sigs.k8s.io/concepts/conformance/)，对照 `tests/conformance/conformance_test.go` 看每个 profile 验证了什么。
