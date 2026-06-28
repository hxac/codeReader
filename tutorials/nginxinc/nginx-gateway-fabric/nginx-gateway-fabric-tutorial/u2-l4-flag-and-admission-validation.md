# 参数校验与准入式校验机制

## 1. 本讲目标

本讲聚焦 NGF 控制面在「真正开始干活之前」的一道安全闸：**参数校验**。读完本讲，你应当能够：

- 说清 NGF 把校验拆成「赋值即校验」和「运行期一致性校验」两层的原因与分工；
- 读懂 `validation.go` 里那一组校验函数（控制器名、资源名、端口、URL、endpoint 等）各自的规则；
- 理解 `validating_types.go` 里 `stringValidatingValue` / `intValidatingValue` 等类型如何通过实现 `pflag.Value` 接口把校验织进 flag 解析过程；
- 掌握 `ensureNoPortCollisions` 这类跨 flag 的一致性校验在 `RunE` 流水线里的位置；
- 能够照葫芦画瓢，为一个新字符串 flag 添加一条校验规则。

## 2. 前置知识

本讲承接 [u2-l2](u2-l2-controller-command-and-config.md)，那里已经讲清了 `controller` 命令的「三步声明模式」（flag 名常量、带校验器的值变量、`cmd.Flags().Var` 注册）以及 `RunE` 的四阶段流水线。如果你还不熟悉下面的概念，请先回看那一讲：

- **pflag**：cobra 使用的命令行参数解析库。每个 flag 背后都有一个实现了 `pflag.Value` 接口（`String()`、`Set(string) error`、`Type()`）的对象。
- **赋值即校验**：在 `Set()` 里就调用校验函数，非法值在被存进变量之前就被拒掉，进程直接报错退出。
- **DNS-1123 子域名 / Qualified Name**：Kubernetes 对资源名、命名空间名等的命名规范，分别由 `validation.IsDNS1123Subdomain` 和 `validation.IsQualifiedName` 实现，是 K8s 生态里最基础的命名约束。
- **特权端口**：TCP/UDP 端口号范围是 1–65535，其中 1–1023 是特权端口（Linux 下需要 root 或 `CAP_NET_BIND_SERVICE` 才能监听）。NGF 会根据使用场景决定是否允许特权端口。

一句话回顾：上一讲告诉你「flag 会被汇聚成 `config.Config` 再交给 `StartManager`」，本讲要回答的是——**这些 flag 的值，在被汇聚之前，是怎么被一道道拦下来、确认合法的？**

## 3. 本讲源码地图

本讲只涉及 `cmd/gateway` 下的三个文件：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `cmd/gateway/validation.go` | 一组**纯函数校验器**，输入字符串或整数，输出 `error`。 | 各类校验规则（控制器名、资源名、端口、URL 等）。 |
| `cmd/gateway/validating_types.go` | 三个**实现了 `pflag.Value` 接口的校验型类型**，把校验器挂到 flag 上。 | `stringValidatingValue` / `stringSliceValidatingValue` / `intValidatingValue`。 |
| `cmd/gateway/commands.go` | 把上面的校验器装配进具体 flag，并在 `PreRunE` / `RunE` 里做跨 flag 一致性校验。 | flag 值变量声明、`cmd.Flags().Var` 注册、`ensureNoPortCollisions` 调用点。 |

辅助参考（不展开，但实践会用到）：

- `cmd/gateway/validation_test.go`：用 gomega 写的表驱动测试，是理解「某个值到底合不合法」最快的入口。
- `cmd/gateway/commands_test.go`：端到端地测 flag 注册与默认值。

## 4. 核心概念与源码讲解

NGF 的参数校验可以分成三个最小模块：**校验规则集合**（「校验什么」）、**pflag Value 自定义类型**（「什么时候校验、怎么把校验器接到 flag 上」）、**端口冲突检测与跨 flag 一致性校验**（「多个 flag 之间的关系校验」）。下面逐一拆解。

### 4.1 校验规则集合

#### 4.1.1 概念说明

`validation.go` 里是一堆**无状态的纯函数**，签名形如 `func validateXxx(value T) error`。它们的职责只有一个：判断一个值是否合法，合法返回 `nil`，非法返回一条人类可读的错误。

这种设计的好处是：

- **可单测**：纯函数不依赖任何全局状态，写表驱动测试最省事（NGF 正是这么做的）。
- **可复用**：同一个校验器可以挂到多个 flag 上。比如 `validateResourceName` 被 `gatewayClassName`、`configName`、`serviceName`、`agentTLSSecretName` 等十几个 flag 共用。
- **关注点分离**：「规则是什么」和「规则在何时触发」被拆到两个文件，互不耦合。

按校验对象的不同，这组函数大致分四类：**命名类**、**网络地址类**、**端口类**、**命令参数类**。

#### 4.1.2 核心流程

一个典型的命名类校验流程（以资源名为例）：

1. 先判空（空字符串直接报 `must be set`）。
2. 调用 K8s 官方的命名校验（`validation.IsDNS1123Subdomain` 返回一组错误信息）。
3. 若有错误信息，用分号拼接后包成一条 `error` 返回；否则返回 `nil`。

网络地址类（如 endpoint `<host>:<port>`）会多一步「先拆分、再分别校验」；端口类则是直接做数值范围判断。整体都遵循「**尽早失败、错误信息可读**」的原则。

#### 4.1.3 源码精读

**（1）控制器名校验**——这是最严格的一条，因为它决定了 NGF 能否认领一个 GatewayClass。

[cmd/gateway/validation.go:15-18](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/validation.go#L15-L18) 定义了 Gateway API 官方的控制器名正则（注释标明来源是 gateway-api 的 `shared_types.go`），格式必须是 `DOMAIN/PATH`：

[cmd/gateway/validation.go:20-41](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/validation.go#L20-L41) 依次做三道检查：

- 第 21–23 行：判空。
- 第 25–33 行：按 `/` 切分，要求至少两段（`DOMAIN/PATH`），且域名段必须等于常量 `domain`（即 `gateway.nginx.org`，定义在 [cmd/gateway/commands.go:30](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L30)）。这把控制器身份焊死在了 NGF 自己的域名下。
- 第 35–38 行：用正则做最终格式校验。

> 提示：第 31 行的 `domain` 是包级常量，不是 flag。这是有意为之——域名是 NGF 的「身份证号」，不允许用户随便改。

**（2）资源名 / 带命名空间的资源名校验**——这是被复用最多的一组。

[cmd/gateway/validation.go:43-56](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/validation.go#L43-L56) 直接复用 K8s 的 `validation.IsDNS1123Subdomain`，这是 Kubernetes 校验资源名的同一套规则（小写字母、数字、`-`、`.`，不以 `-`/`.` 开头或结尾等）。

[cmd/gateway/validation.go:61-81](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/validation.go#L61-L81) 是它的增强版：允许 `namespace/name` 形式（用 `/` 切，最多两段），两段分别按 DNS-1123 子域名校验。PLM（Policy Lifecycle Manager）相关 flag 的 Secret 名就用了它，因为那些 Secret 可以跨命名空间。

**（3）Qualified Name 校验**：

[cmd/gateway/validation.go:83-95](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/validation.go#L83-L95) 用 `validation.IsQualifiedName`，比 DNS-1123 更宽松（允许大写、长度限制不同），用于 `--cluster-domain` 这类值（如 `cluster.local`）。

**（4）网络地址：endpoint 与 URL**。

[cmd/gateway/validation.go:109-135](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/validation.go#L109-L135) 的 `validateEndpoint` 校验 `<host>:<port>`：先用 `net.SplitHostPort` 拆分，再校验端口范围，最后 host 既可以是合法 IP 也可以是 DNS 子域名。注意第 124–130 行的「双判」——先试 IP、再试域名，两者都不满足才报一个通用错误（因为无法判断用户本意是 IP 还是域名）。

[cmd/gateway/validation.go:213-232](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/validation.go#L213-L232) 的 `validateURL` 用 `url.ParseRequestURI` 解析，并强制 scheme 必须是 `http`/`https`、host 不能为空。PLM 的 `--plm-storage-url` 就用它。

**（5）端口范围校验——注意两套范围的差异**：

[cmd/gateway/validation.go:235-240](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/validation.go#L235-L240) 的 `validatePort` 要求 `[1024 - 65535]`，**排除特权端口**，用于 metrics / health 监听端口（默认 9113 / 8081）。

[cmd/gateway/validation.go:244-249](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/validation.go#L244-L249) 的 `validateAnyPort` 要求 `[1 - 65535]`，**允许特权端口**，用于 NGINX One Console 的 telemetry 端口（默认 443）。

> 这是一个值得品味的设计：是否允许特权端口不是拍脑袋决定的，而是由「这个端口在容器里会不会真的去 bind」决定的。metrics/health 由控制面进程直接监听，通常不希望它去抢特权端口；而 telemetry 端口 443 是约定俗成的 HTTPS 端口，需要放行。

#### 4.1.4 代码实践

**实践目标**：通过阅读测试断言，反推校验规则的边界，而不是去背函数实现。

**操作步骤**：

1. 打开 [cmd/gateway/validation_test.go:9-67](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/validation_test.go#L9-L67)，阅读 `TestValidateGatewayControllerName` 的表驱动用例。
2. 打开 [cmd/gateway/validation_test.go:510-540](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/validation_test.go#L510-L540)，阅读 `validatePort` 的边界用例。

**需要观察的现象**：

- `gateway.nginx.org`（缺 path）、`gateway.nginx.org/`（只有斜杠）、`invalid-domain/my-gateway`（域名错）三种情况 `expErr` 都是 `true`。
- `port: 1023` 报错、`port: 65536` 报错、`port: 9113` 通过——这正好印证了 `validatePort` 的 `[1024, 65535]` 闭区间。

**预期结果**：你能用自己的话复述「一个合法控制器名的三道关卡」和「特权端口为何被 metrics/health 排斥」，且能与测试断言一一对应。

#### 4.1.5 小练习与答案

**练习 1**：`validateResourceName("My_Gateway")` 会通过吗？为什么？

> **答案**：不通过。`My_Gateway` 含大写字母和下划线，违反 DNS-1123 子域名规则（只允许小写字母、数字、`-`、`.`），`validation.IsDNS1123Subdomain` 会返回错误信息。

**练习 2**：为什么 `validateEndpoint` 在 host 既不是 IP、也不是合法域名时，只返回一条通用错误，而不是分别列出两个原因？

> **答案**：因为代码无法判断用户本意是想填 IP 还是域名（见 [validation.go:132-134](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/validation.go#L132-L134)）。分别报两个错反而会让用户困惑，统一提示「格式应为 `<host>:<port>`」更友好。

### 4.2 pflag Value 自定义类型

#### 4.2.1 概念说明

光有校验函数还不够。默认情况下，`pflag` 用自己的 `stringValue`、`intValue` 来存 flag 值，**解析时只做类型转换、不做业务校验**。如果用默认类型，非法值要等到 `RunE` 里被使用时才暴露，错误定位会很绕。

NGF 的做法是**自己实现 `pflag.Value` 接口**，在 `Set()` 方法里插入校验函数。这样 pflag 在解析命令行、调用 `Set("用户输入")` 的瞬间就会跑校验——非法值当场报错、当场退出。这正是一种「**准入式校验**」（admission-style）：把不合格的输入挡在「值真正被写入」这一道门外。

`validating_types.go` 提供了三个这样的类型，对应三种最常见的 flag 类型：

| 类型 | 对应 flag 类型 | 存储字段 | 适用场景 |
| --- | --- | --- | --- |
| `stringValidatingValue` | `string` | 单个 `value string` | 单个字符串值，如资源名、URL。 |
| `stringSliceValidatingValue` | `stringSlice` | `values []string` + `changed bool` | 逗号分隔、可重复出现的字符串列表，如 `--watch-namespaces`。 |
| `intValidatingValue` | `int` | 单个 `value int` | 整数值，如端口。 |

#### 4.2.2 核心流程

以 `stringValidatingValue` 为例，它的工作流是：

1. **注册时**：在 `commands.go` 里声明一个该类型的变量，把校验函数塞进 `validator` 字段（如 `validator: validateResourceName`），再通过 `cmd.Flags().Var(&该变量, 名字, 用法)` 注册。pflag 会把 flag 绑到这个对象上。
2. **解析时**：pflag 遇到命令行里的 `--xxx=yyy`，调用该对象的 `Set("yyy")`。
3. **`Set` 内部**：先调用 `v.validator(param)`，返回 error 就原样上抛（pflag 会打印并退出）；通过后才把值写进 `v.value`。
4. **使用时**：业务代码直接读 `变量.value` 拿到的是「已经过校验」的值。

`intValidatingValue` 多一步：`Set` 里先用 `strconv.ParseInt` 把字符串转成 int（位数 32），再调校验器。`stringSliceValidatingValue` 多两步：按逗号切分、逐个校验，并用 `changed` 标志区分「第一次设值（覆盖默认）」还是「重复设值（追加）」。

#### 4.2.3 源码精读

**（1）`stringValidatingValue`——最核心、最简洁的一个**。

[cmd/gateway/validating_types.go:11-32](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/validating_types.go#L11-L32) 是完整定义。结构体只有两个字段：`validator`（校验函数）和 `value`（实际值）：

```go
type stringValidatingValue struct {
    validator func(v string) error
    value     string
}
```

关键在 [validating_types.go:22-28](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/validating_types.go#L22-L28) 的 `Set`——**校验先于赋值**：

```go
func (v *stringValidatingValue) Set(param string) error {
    if err := v.validator(param); err != nil {
        return err        // 校验失败，立刻返回，不写 value
    }
    v.value = param       // 校验通过，才写入
    return nil
}
```

`Type()` 返回 `"string"`（[L30-32](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/validating_types.go#L30-L32)），仅供 pflag 在生成帮助文本时显示类型名。

**（2）`stringSliceValidatingValue`——支持列表 + 重复追加**。

[cmd/gateway/validating_types.go:55-73](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/validating_types.go#L55-L73) 的 `Set` 做了三件事：按逗号切分（L56）、逐个 `TrimSpace` 并校验（L57-63）、根据 `changed` 决定覆盖还是追加（L65-69）。

为什么要 `changed` 标志？因为 pflag 对 slice flag 的语义是：**第一次出现覆盖默认值，后续出现追加**。没有这个标志的话，第二次写 `--watch-namespaces=b` 会覆盖掉第一次的 `a`，而不是变成 `[a, b]`。

**（3）`intValidatingValue`——多一步类型转换**。

[cmd/gateway/validating_types.go:88-100](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/validating_types.go#L88-L100) 的 `Set` 先 `strconv.ParseInt(param, 10, 32)`（注意位数是 32），再调 `v.validator(int(intVal))`。注意第 89 行用 `int32` 解析但存进 `int` 字段——在主流平台上这是安全的，但意味着这个类型只接受 32 位范围的整数。

**（4）这些类型如何被装配进 flag**。

[cmd/gateway/commands.go:121-197](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L121-L197) 是 flag 值变量的集中声明区。注意三个细节：

- 每个 `stringValidatingValue` 都挂了一个 `validator`，把「规则」与「变量」绑定。
- 部分变量带 `value: 默认值`（如 [L157-160](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L157-L160) 的 `metricsListenPort` 默认 9113、validator 是 `validatePort`）。**默认值不会经过校验器**（因为它直接写进字段，没走 `Set`），所以默认值必须是开发期就保证合法的常量。
- `intValidatingValue` 同理，[L147-150](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L147-L150) 的 telemetry 端口默认 443、validator 是 `validateAnyPort`（允许特权端口）。

注册则集中在 [cmd/gateway/commands.go:359-364](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L359-L364) 这类 `cmd.Flags().Var(&变量, 名字, 用法)` 调用。以控制器名为例：

```go
cmd.Flags().Var(
    &gatewayCtlrName,
    gatewayCtlrNameFlag,
    fmt.Sprintf(gatewayCtlrNameUsageFmt, domain),
)
utilruntime.Must(cmd.MarkFlagRequired(gatewayCtlrNameFlag))
```

注意紧跟的 `MarkFlagRequired`——它让 pflag 在该 flag 缺失时报错。这是另一道「准入」：**必填检查**交给 pflag，**格式检查**交给 validating value，各司其职。

#### 4.2.4 代码实践

**实践目标**：验证「默认值不经过校验器」这一结论。

**操作步骤**：

1. 阅读 [cmd/gateway/commands.go:162-165](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L162-L165)：`healthListenPort` 默认 8081、validator 是 `validatePort`。
2. 在脑中模拟：如果有人把默认值改成 `80`（特权端口），构建出的二进制启动时会报错吗？

**需要观察的现象 / 预期结果**：

- 默认值 `8081` 落在 `[1024, 65535]` 内，合法。
- 但即便改成非法的 `80`，启动时也**不会**被 `validatePort` 拦下——因为默认值是直接写进 `value` 字段的，没经过 `Set`。只有当用户显式传 `--health-port=80` 时才会触发校验报错。
- 结论：**默认值的安全性由开发者自负**，这也是为什么代码里所有默认值都是经过人工确认的常量（9113、8081、443 等）。

> 待本地验证：如果你在一个临时分支把 `healthListenPort` 的 `value` 改成 `80` 并重新 `make build && ./build/out/gateway controller --gateway-ctlr-name=... --gateway-class-name=...`，预期它不会因端口校验而失败（除非别处另有检查）。这能直观佐证上述结论。

#### 4.2.5 小练习与答案

**练习 1**：为什么不直接用 `cmd.Flags().StringVar(...)` 然后在 `RunE` 里手动校验？

> **答案**：那样会让非法值「潜伏」到 `RunE` 才暴露，错误信息离用户输入更远、调试更绕；而且要在 `RunE` 里为每个 flag 重复写「先判空再校验」的样板代码。用 `pflag.Value` 把校验织进解析阶段，错误最早暴露、代码也最集中。

**练习 2**：`stringSliceValidatingValue` 的 `changed` 字段去掉会怎样？

> **答案**：每次 `Set` 都会走 `v.values = params`（覆盖）分支，导致用户写多次 `--watch-namespaces=a --watch-namespaces=b` 时只剩 `b`，丢失 `a`。`changed` 保证「首次覆盖默认、后续追加」的 pflag 切片语义。

### 4.3 端口冲突检测与跨 flag 一致性校验

#### 4.3.1 概念说明

前面两个模块解决的是「单个 flag 的值是否合法」。但有一类问题单看任何一个 flag 都发现不了：**多个 flag 之间的冲突或依赖**。典型例子：

- **端口冲突**：metrics 端口和 health 端口不能相同，否则两个 HTTP server 抢同一个端口。
- **条件必填**：开启 NGINX Plus（`--nginx-plus`）时，必须提供 `--usage-report-secret`。
- **跨命名空间一致性**：PLM 引用的 Secret 如果显式带了命名空间，该命名空间必须在被 watch 的范围内。

这类校验无法塞进单个 `pflag.Value`（因为它需要读多个 flag 的值），所以 NGF 把它们放在 `PreRunE` 和 `RunE` 里做——即「**运行期一致性校验**」。这是与「赋值即校验」互补的第二层防线。

#### 4.3.2 核心流程

NGF 的两层校验时序：

```text
用户执行 controller 命令
   │
   ├─ pflag 解析每个 flag ──► 调用各 validating value 的 Set()  【第一层：赋值即校验】
   │        非法值在此被拦下，进程退出
   │
   ├─ cobra 调用 PreRunE     ──► validatePLMSecretNamespacesWatched  【第二层：跨 flag 一致性】
   │
   └─ cobra 调用 RunE        ──► ensureNoPortCollisions / buildUsageReportConfig 等
            进一步一致性校验，然后才装配 config.Config 并 StartManager
```

第一层负责「值本身对不对」，第二层负责「值和值之间协不协调」。两层都通过，才会进入 [u2-l2](u2-l2-controller-command-and-config.md) 讲过的 `config.Config` 装配。

#### 4.3.3 源码精读

**（1）端口冲突检测——本模块的主角**。

[cmd/gateway/validation.go:252-263](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/validation.go#L252-L263) 的 `ensureNoPortCollisions` 用一个 `map[int]struct{}` 当集合，遍历所有传入端口，发现重复就报错：

```go
func ensureNoPortCollisions(ports ...int) error {
    seen := make(map[int]struct{})
    for _, port := range ports {
        if _, ok := seen[port]; ok {
            return fmt.Errorf("port %d has been defined multiple times", port)
        }
        seen[port] = struct{}{}
    }
    return nil
}
```

用变参 `ports ...int` 是为了让调用方把任意多个端口一次性喂进来，函数本身不关心「具体是哪几个 flag」。

**调用点**在 `RunE` 里：[cmd/gateway/commands.go:255-257](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L255-L257)，把 metrics 端口和 health 端口传进去：

```go
if err := ensureNoPortCollisions(metricsListenPort.value, healthListenPort.value); err != nil {
    return fmt.Errorf("error validating ports: %w", err)
}
```

注意它放在 `RunE` 最前面（紧跟日志初始化），说明端口冲突属于「启动前置条件」，必须最早确认。

**（2）条件必填——Plus 与 usage-report-secret**。

[cmd/gateway/commands.go:636-650](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L636-L650) 的 `buildUsageReportConfig` 在 `SecretName.value == ""` 时返回错误。而它只在 [commands.go:281-286](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L281-L286) `if plus { ... }` 分支里被调用——于是形成了「**只有开启 Plus 时才强制要求 usage-report-secret**」的条件必填语义。这是跨 flag 依赖的典型实现。

**（3）跨命名空间一致性——PreRunE**。

[cmd/gateway/commands.go:236-238](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L236-L238) 把 `PreRunE` 指向 `validatePLMSecretNamespacesWatched`。该函数（[commands.go:666-](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L666)）会检查：PLM 的 Secret 如果显式带了 `namespace/name`，那个 namespace 必须出现在 `--watch-namespaces` 列表或控制器自身命名空间里，否则控制器根本读不到该 Secret，必须在 `RunE` 之前就拦下。

> 为什么放 `PreRunE` 而不是 `RunE`？cobra 的执行顺序是 `PreRunE → RunE → PostRunE`。`PreRunE` 失败会直接中断、不进 `RunE`。把这种「读不到资源」的硬性前置错误放 `PreRunE`，可以让失败更早、日志更干净。

#### 4.3.4 代码实践

**实践目标**：用 `go test` 直接验证 `ensureNoPortCollisions` 的行为，并理解它的测试为何只测函数本身、不测调用点。

**操作步骤**：

1. 阅读 [cmd/gateway/validation_test.go:636-637](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/validation_test.go#L636-L637)，可见两条断言：不同端口通过、相同端口失败。
2. 在仓库根目录执行（**待本地验证**）：

   ```bash
   go test ./cmd/gateway/ -run TestEnsureNoPortCollisions -v
   ```

   如果该测试函数名与此不同，可改用 `go test ./cmd/gateway/ -run 'EnsureNoPortCollisions' -v` 或直接 `go test ./cmd/gateway/ -v -count=1` 查看全部 validation 测试。

**需要观察的现象**：测试通过；说明「端口相同时报错、不同时通过」的行为已被测试锁定。

**预期结果**：即便没有集群、没有 NGINX，你也能用一条 `go test` 命令确认这条一致性规则的正确性——这正是纯函数校验器的可测性红利。

> 待本地验证：具体的测试函数名以你本地 `validation_test.go` 中的实际命名为准；若 `-run` 匹配不到，去掉 `-run` 跑整个包即可。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ensureNoPortCollisions` 设计成变参 `ports ...int`，而不是固定两个参数？

> **答案**：变参让函数与「具体有几个端口 flag」解耦。将来如果 NGF 新增第三个监听端口（比如一个独立的管理端口），调用方只需多传一个参数，函数本身不用改，符合开闭原则。

**练习 2**：假设新增一个 `--profile-port`，希望它与 metrics/health 都不冲突。需要改哪几处？

> **答案**：(a) 在 `commands.go` 声明一个 `intValidatingValue{validator: validatePort}` 变量；(b) 用 `cmd.Flags().Var` 注册它；(c) 在 [commands.go:255](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L255) 的 `ensureNoPortCollisions(...)` 调用里多传一个 `profilePort.value`。第一层（赋值即校验）和第二层（冲突检测）都要照顾到。

## 5. 综合实践

把三个模块串起来，完成本讲规格里要求的核心任务：**为一个新的字符串 flag 添加一条校验规则，参考现有 validating value 实现**。

**场景设定**（示例场景，非项目原有需求）：假设要新增一个 `--server-tls-domain` 之外的新 flag `--cluster-uid-prefix`，要求它的值必须是非空的、只含小写字母和数字、长度不超过 32。

**步骤 1：在 `validation.go` 写校验函数**（示例代码）：

```go
// 示例代码：新增校验器，放在 validation.go 末尾
func validateClusterUIDPrefix(value string) error {
    if len(value) == 0 {
        return errors.New("must be set")
    }
    if len(value) > 32 {
        return fmt.Errorf("too long: max 32 chars, got %d", len(value))
    }
    re := regexp.MustCompile(`^[a-z0-9]+$`)
    if !re.MatchString(value) {
        return fmt.Errorf("invalid format: only lowercase letters and digits allowed")
    }
    return nil
}
```

**步骤 2：在 `commands.go` 声明 validating value 并注册**（示例代码）：

```go
// 1) flag 名常量（沿用三步声明模式）
const clusterUIDPrefixFlag = "cluster-uid-prefix"

// 2) 值变量，挂上校验器
var clusterUIDPrefix = stringValidatingValue{
    validator: validateClusterUIDPrefix,
}

// 3) 注册
cmd.Flags().Var(&clusterUIDPrefix, clusterUIDPrefixFlag, "The prefix of the cluster UID.")
```

**步骤 3：在 `validation_test.go` 加表驱动测试**（参照 [validation_test.go:9-67](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/validation_test.go#L9-L67) 的写法），覆盖：空串、超长、含非法字符、合法值四种情况。

**步骤 4：验证**（待本地验证）：

```bash
go test ./cmd/gateway/ -run ValidateClusterUIDPrefix -v
./build/out/gateway controller --cluster-uid-prefix=ABC   # 预期：校验失败、报 invalid format
./build/out/gateway controller --cluster-uid-prefix=abc123 # 预期：通过该 flag 的校验
```

**需要观察的现象**：

- 非法值在 pflag 解析阶段（`Set`）就被拦下，进程不会进入 `RunE`——这正是「赋值即校验」的效果。
- 合法值能被读进 `clusterUIDPrefix.value`，可供后续装配 `config.Config` 使用。

**预期结果**：你完整走通了「写校验器 → 挂 validating value → 注册 flag → 加测试 → 运行验证」的闭环，复用了 `stringValidatingValue` 这套基础设施，没有重复造轮子。

## 6. 本讲小结

- NGF 的参数校验分两层：**第一层「赋值即校验」**由 `pflag.Value` 自定义类型在 `Set()` 里完成，**第二层「运行期一致性校验」**由 `PreRunE`/`RunE` 里的跨 flag 函数完成。
- `validation.go` 是一组无状态纯函数校验器，按命名、网络地址、端口、命令参数分类，最大优势是可单测、可复用（如 `validateResourceName` 被十几个 flag 共用）。
- `validating_types.go` 的 `stringValidatingValue` / `stringSliceValidatingValue` / `intValidatingValue` 通过实现 `pflag.Value` 接口，把校验织进 flag 解析阶段，非法值最早暴露。
- 注意两套端口范围：`validatePort`（`[1024, 65535]`，排斥特权端口）用于 metrics/health；`validateAnyPort`（`[1, 65535]`，允许特权端口）用于 telemetry 的 443。
- `ensureNoPortCollisions` 用变参 + map 集合做端口去重，在 `RunE` 最前面调用；条件必填（Plus→usage-report-secret）和跨命名空间一致性（PLM Secret）分别在 `RunE` 与 `PreRunE` 里完成。
- 默认值不经过校验器（直接写字段），其合法性由开发者自负；必填检查则交给 pflag 的 `MarkFlagRequired`。

## 7. 下一步学习建议

本讲把「flag 如何被校验」讲到了底。到此，`controller` 命令从「解析 flag → 校验 flag → 装配 `config.Config`」的整条前置链路已经完整。接下来：

- 进入 [u3-l1（StartManager 全景）](u3-l1-manager-startup-overview.md)，看校验通过的 `config.Config` 是如何被 `StartManager` 消费、装配出整个控制面的——这是 u3 单元「Manager 组装与控制器注册」的入口。
- 若你对「校验」话题意犹未尽，可以对比阅读 NGF 的 **Webhook 准入校验**（如果当前版本引入了 admission webhook）与 K8s 原生的 `IsDNS1123Subdomain` 等校验工具集，理解「命令行准入」与「资源对象准入」的异同。
- 在动手扩展 NGF 时，回头参考本讲的「三步声明模式 + 表驱动测试」套路——这是给 NGF 新增任何 flag 时的标准做法。
