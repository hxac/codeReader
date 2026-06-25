# Ingress 注解（annotations）解析

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `parseAnnotations` 的整体执行流程，以及它在配置生成链路中的位置。
- 区分并复述四类「类型转换辅助函数」的用途与统一返回约定。
- 追踪一条注解（以 `nginx.org/proxy-connect-timeout` 为例）从字符串到 `ConfigParams` 字段、再到 NGINX 指令的全过程。
- 理解 mergeable ingress（master/minion）模式下注解的三道工序：过滤（master/minion）、继承（master→minion），以及它们为何必须按特定顺序执行。

本讲承接 [u4-l1 Configurator：配置生成的中枢](u4-l1-configurator-overview.md)——我们已经知道 Configurator 会把 Ingress 资源交给配置生成层生成 `.conf`。本讲聚焦「注解」这条特殊的配置入口：它是用户在不写 CRD、不改 ConfigMap 的情况下，对单个 Ingress 施加 NGINX 指令的主要手段。

## 2. 前置知识

### 2.1 什么是注解（annotation）

Kubernetes 里的注解是挂在资源 `metadata.annotations` 上的 `map[string]string`，键值都是字符串。它和标签（label）的区别在于：标签用于**选择和筛选**，注解用于**附带任意非结构化元数据**。NIC 复用了注解这一机制——约定一组以 `nginx.org/`、`nginx.com/` 为前缀的键，把它们**翻译成对应的 NGINX 配置指令**。

例如用户在 Ingress 上写：

```yaml
metadata:
  annotations:
    nginx.org/proxy-connect-timeout: "30s"
```

最终会变成 NGINX `location` 块里的一行：

```nginx
proxy_connect_timeout 30s;
```

### 2.2 注解为何「类型不安全」

注解值只能是字符串，但 NGINX 指令的取值有不同语义：布尔（on/off）、整数、时间（`30s`、`1m`）、大小（`8k`、`1m`）、负载均衡方法（`round_robin`、`least_conn`）等。所以解析时必须做**字符串 → 目标类型**的转换，并在转换失败时安全降级。这就是 `parsing_helpers.go` 存在的全部理由。

### 2.3 ConfigParams 是什么

回顾 u4-l1 与 u4-l2：`ConfigParams` 是「资源级」配置结构体，每个 Ingress 解析出一个，承载该 Ingress 用到的所有 NGINX 参数。它有「副本拷贝」语义——`parseAnnotations` 的输入 `baseCfgParams` 已经带好了默认值（来自 `NewDefaultConfigParams`），解析只是「在副本上按需覆盖」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [internal/configs/annotations.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/annotations.go) | 注解常量定义、`parseAnnotations` 主函数、rate-limit 子解析器、mergeable ingress 的过滤/继承函数 |
| [internal/configs/parsing_helpers.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/parsing_helpers.go) | 类型转换辅助函数（`GetMapKeyAs*`、`ParseTime`/`ParseSize`/`ParseLBMethod` 等） |
| [internal/configs/config_params.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/config_params.go) | `ConfigParams` 结构体定义与默认值 |
| [internal/configs/ingress.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go) | 调用 `parseAnnotations` 的入口，以及 mergeable ingress 三道工序的编排 |
| [internal/configs/version1/nginx.ingress.tmpl](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/version1/nginx.ingress.tmpl) | OSS 版 Ingress 模板，把 `ConfigParams` 渲染成 NGINX 指令 |
| [internal/configs/version1/nginx-plus.ingress.tmpl](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/version1/nginx-plus.ingress.tmpl) | Plus 版 Ingress 模板（与 OSS 对应） |

## 4. 核心概念与源码讲解

### 4.1 注解常量与解析总览

#### 4.1.1 概念说明

NIC 支持上百条注解。为避免在代码里到处写魔法字符串，所有常用注解键都被抽成 Go 常量，集中在 `annotations.go` 顶部。每个常量都附中文注释说明它指向什么。这样做有三点好处：

- 重命名安全：改一处常量定义即可。
- 防笔误：拼写错误会在编译期暴露。
- 文档化：常量旁的注释就是一份「注解清单」。

部分注解键没有抽成常量（直接用字符串字面量），是因为它们只在 `parseAnnotations` 内部出现一次。

#### 4.1.2 核心流程

`parseAnnotations` 是整个注解子系统的总入口，它遵循一个极其规整的模式：

1. **拷贝基线**：`cfgParams := *baseCfgParams`——把带默认值的基线结构体复制一份，后续只修改这份副本。
2. **逐键探测**：对每一条支持的注解，用 `if v, exists := annotations[key]; exists` 探测是否存在。
3. **解析校验**：存在则调用对应的 `ParseXxx` 或 `GetMapKeyAs*` 辅助函数，把字符串转成目标类型。
4. **失败降级**：解析失败只记日志（`nl.Errorf`/`nl.Error`），**不中断**、**不 panic**，字段保持默认值。
5. **写入字段**：成功则写入 `cfgParams` 对应字段。
6. **返回副本**：最后返回修改后的 `ConfigParams`。

注意第 4 步——这是「best-effort」语义：单个注解写错只影响它自己，不会让整个 Ingress 配置生成失败。这与 u4-l2 里 ConfigMap 解析的策略一致。

#### 4.1.3 源码精读

先看函数签名与基线拷贝：

```go
// nolint: gocyclo
func parseAnnotations(ingEx *IngressEx, baseCfgParams *ConfigParams, isPlus bool, hasAppProtect bool, hasAppProtectDos bool, enableInternalRoutes bool, enableDirectiveAutoadjust bool) ConfigParams {
    l := nl.LoggerFromContext(baseCfgParams.Context)
    cfgParams := *baseCfgParams   // 关键：拷贝基线，后续只改副本
    ...
}
```

这段代码位于 [internal/configs/annotations.go:185-187](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/annotations.go#L185-L187)，是理解整个函数的钥匙：因为 `*baseCfgParams` 是值拷贝，所以即使注解缺失，默认值（如 `ProxyConnectTimeout: "60s"`）也已经就位。

函数的最后一步把所有解析结果集中写回：

[internal/configs/annotations.go:617-621](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/annotations.go#L617-L621) 调用 `parseRateLimitAnnotations` 收集限流相关注解的错误，最后 `return cfgParams`。

注解常量的定义样例见 [internal/configs/annotations.go:14-102](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/annotations.go#L14-L102)，例如：

```go
// ProxySetHeadersAnnotation is the annotation where the proxy set headers are specified.
const ProxySetHeadersAnnotation = "nginx.org/proxy-set-headers"
// AddHeaderAnnotation is the annotation where add_header directives are specified.
const AddHeaderAnnotation = "nginx.org/add-header"
```

#### 4.1.4 代码实践

**实践目标**：确认 `parseAnnotations` 的「值拷贝基线」行为，理解默认值从何而来。

**操作步骤**：

1. 打开 [internal/configs/annotations.go:185](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/annotations.go#L185)，定位 `cfgParams := *baseCfgParams`。
2. 打开 [internal/configs/config_params.go:262](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/config_params.go#L262)，确认 `ProxyConnectTimeout: "60s"` 是默认值。
3. 阅读 `annotations_test.go` 中调用 `parseAnnotations` 的单元测试，例如 [internal/configs/annotations_test.go:247](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/annotations_test.go#L247)，观察它如何构造 `baseCfgParams` 并断言结果。

**需要观察的现象**：即便测试的 Ingress 不带任何注解，返回的 `cfgParams.ProxyConnectTimeout` 仍是 `"60s"`——这证明默认值来自基线，而非函数内部硬编码。

**预期结果**：「值拷贝基线」让 `parseAnnotations` 天然具备「缺省即默认」的能力，无需在每个分支里写 `else { 设默认值 }`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `parseAnnotations` 用 `cfgParams := *baseCfgParams`（值拷贝）而不是 `cfgParams := baseCfgParams`（指针拷贝）？

**答案**：指针拷贝会让多个调用共享同一个底层结构体，对副本字段的修改会污染 `baseCfgParams`，进而影响后续资源。值拷贝保证每个 Ingress 拿到独立的配置副本，互不干扰。

**练习 2**：如果用户写了 `nginx.org/proxy-connect-timeout: "abc"`（非法时间），`parseAnnotations` 会怎样？

**答案**：`ParseTime` 返回错误，`nl.Errorf` 记录日志，但**不写入** `cfgParams.ProxyConnectTimeout`，该字段保持基线默认值 `"60s"`，函数继续解析其余注解，最终正常返回。

---

### 4.2 类型转换辅助函数

#### 4.2.1 概念说明

因为注解值都是字符串，且 NGINX 指令取值类型多样，`parsing_helpers.go` 提供了两类辅助函数：

- **从 map 取值并转换**：`GetMapKeyAsBool`、`GetMapKeyAsInt`、`GetMapKeyAsInt64`、`GetMapKeyAsUint64`、`GetMapKeyAsStringSlice`——封装「探测键是否存在 + 类型转换」这一对操作。
- **纯字符串校验/归一化**：`ParseTime`、`ParseSize`、`ParseOffset`、`ParseLBMethod`、`ParseRequestRate`、`ParseBool`、`ParseInt` 等——只负责把一个字符串判定为合法的某种 NGINX 取值。

#### 4.2.2 核心流程：统一的三返回值约定

`GetMapKeyAs*` 系列遵循一个统一的返回约定：`(value, exists, error)`。

- `exists` 表示「键是否存在」，决定是否要应用这个值。
- `error` 表示「键存在但解析失败」。
- 这种三段式让调用方可以区分「用户没写」与「用户写错了」，从而决定是记日志还是静默。

例如布尔转换：

```go
func GetMapKeyAsBool(m map[string]string, key string, context apiObject) (bool, bool, error) {
    if str, exists := m[key]; exists {
        b, err := ParseBool(str)
        if err != nil {
            return false, exists, fmt.Errorf("... contains invalid bool: %w, ignoring", ...)
        }
        return b, exists, nil
    }
    return false, false, nil   // 键不存在：exists=false
}
```

注意错误信息里带上了 `context`（资源 Kind/Namespace/Name），方便用户定位是哪个 Ingress 的哪个注解写错了——这是生产代码里值得学习的小细节。

而 `GetMapKeyAsStringSlice` 是个例外，它只返回 `(slice, exists)`，因为切分字符串不会失败。

#### 4.2.3 源码精读

`GetMapKeyAsBool` 见 [internal/configs/parsing_helpers.go:22-33](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/parsing_helpers.go#L22-L33)。

`GetMapKeyAsInt` 见 [internal/configs/parsing_helpers.go:36-47](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/parsing_helpers.go#L36-L47)，同样三返回值。

`GetMapKeyAsStringSlice` 见 [internal/configs/parsing_helpers.go:82-88](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/parsing_helpers.go#L82-L88)，按传入的分隔符（如 `","` 或 `"\n"`）切分。

时间校验是最有代表性的一类，因为它要兼容 NGINX 的时间语法：

```go
var timeRegexp = regexp.MustCompile(
    `^(\d+y)??\s*(\d+M)??\s*(\d+w)??\s*(\d+d)??\s*(\d+h)??\s*(\d+m)??\s*(\d+s?)??\s*(\d+ms)??$`)

func ParseTime(s string) (string, error) {
    if s == "" || strings.TrimSpace(s) == "" || !timeRegexp.MatchString(s) {
        return "", errors.New("invalid time string")
    }
    units := timeRegexp.FindStringSubmatch(s)
    ...
    secs := units[7]
    if secs != "" && !strings.HasSuffix(secs, "s") {
        secs = secs + "s"   // 秒值缺省时自动补 "s"，如 "30" → "30s"
    }
    ...
    return fmt.Sprintf(...), nil
}
```

见 [internal/configs/parsing_helpers.go:207-227](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/parsing_helpers.go#L207-L227)。这个正则来自 [NGINX 时间语法文档](http://nginx.org/en/docs/syntax.html)，注意它用大写 `M` 表示月、小写 `m` 表示分，二者不能混淆。`ParseTime` 还做了一个**归一化**：当秒数缺省 `s` 后缀时自动补上。

`ParseSize` 见 [internal/configs/parsing_helpers.go:250-257](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/parsing_helpers.go#L250-L257)，校验形如 `8k`、`1m` 的大小串。

`ParseLBMethod` 见 [internal/configs/parsing_helpers.go:91-108](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/parsing_helpers.go#L91-L108)，用一张白名单 map 校验合法的负载均衡方法，`round_robin` 特殊处理为返回空串（因为它是 NGINX 默认值，无需写出指令）。

#### 4.2.4 代码实践

**实践目标**：理解 `ParseTime` 的正则与「秒数自动补 s」行为。

**操作步骤**：

1. 阅读正则 [internal/configs/parsing_helpers.go:207](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/parsing_helpers.go#L207)，数清楚它支持的时间单位：`y`(年)、`M`(月)、`w`(周)、`d`(日)、`h`(时)、`m`(分)、`s`(秒)、`ms`(毫秒)。
2. 在 [internal/configs/annotations_test.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/annotations_test.go) 中搜索 `proxy-connect-timeout`，找一个测试用例看它传入什么值、期望什么结果。
3.（可选）写一个本地小测试，给注解 `"nginx.org/proxy-connect-timeout": "30"`（无 `s` 后缀），断言返回的 `ProxyConnectTimeout == "30s"`。

**需要观察的现象**：`ParseTime("30")` 返回 `("30s", nil)`；`ParseTime("30s")` 返回 `("30s", nil)`；`ParseTime("abc")` 返回 `("", error)`。

**预期结果**：秒数自动补 `s`，非法输入报错且不写入字段。**待本地验证**：若环境允许 `go test ./internal/configs/ -run TestParse`，可实际跑出断言。

#### 4.2.5 小练习与答案

**练习 1**：`GetMapKeyAsInt` 的三个返回值分别表示什么？为什么需要 `exists` 而不能只用 `(int, error)`？

**答案**：分别是「解析后的值」「键是否存在」「解析错误」。若只用 `(int, error)`，当键不存在时会返回 `(0, nil)`，无法区分「用户没写」与「用户写了 0」。`exists` 让调用方能精确区分这两种情况。

**练习 2**：`ParseLBMethod("round_robin")` 为什么返回空串？

**答案**：`round_robin` 是 NGINX upstream 的默认负载均衡方法，无需在配置里显式写出指令，所以返回空串表示「不生成任何 `xxx;` 指令」，避免无意义的重复。

---

### 4.3 注解到指令的映射：以 proxy-connect-timeout 为例

#### 4.3.1 概念说明

注解与 NGINX 指令的对应关系并非自动推导，而是**手工编写**的：每个 `if ... exists` 分支对应一条「注解键 → ConfigParams 字段 → NGINX 指令」的链路。本模块用 `nginx.org/proxy-connect-timeout` 这条链路串起全貌，因为它典型且完整。

完整的「值变换」链路是：

\[ \text{注解字符串} \xrightarrow{\text{ParseTime}} \text{归一化字符串} \xrightarrow{\text{赋值}} \text{ConfigParams 字段} \xrightarrow{\text{拷贝}} \text{Location 字段} \xrightarrow{\text{模板渲染}} \text{proxy\_connect\_timeout 指令} \]

#### 4.3.2 核心流程

以 `nginx.org/proxy-connect-timeout: "30s"` 为例，完整链路共 6 跳：

1. **取值解析**：`annotations.go` 用 `if ... exists` 探测键，调用 `ParseTime` 归一化。
2. **写字段**：成功则 `cfgParams.ProxyConnectTimeout = parsedProxyConnectTimeout`。
3. **默认兜底**：若键不存在，字段保留基线默认 `"60s"`（来自 `NewDefaultConfigParams`）。
4. **下发到 location**：`ingress.go` 在为每条 path 创建 `location` 时，把 `cfg.ProxyConnectTimeout` 拷进 `location.ProxyConnectTimeout`。
5. **渲染**：模板把 `{{$location.ProxyConnectTimeout}}` 渲成指令值。
6. **写盘 + reload**：见 u4-l1，Configurator 执行三段式。

#### 4.3.3 源码精读

**第 1–2 跳：解析与写字段**，见 [internal/configs/annotations.go:277-283](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/annotations.go#L277-L283)：

```go
if proxyConnectTimeout, exists := ingEx.Ingress.Annotations["nginx.org/proxy-connect-timeout"]; exists {
    if parsedProxyConnectTimeout, err := ParseTime(proxyConnectTimeout); err != nil {
        nl.Errorf(l, "Ingress %s/%s: Invalid value nginx.org/proxy-connect-timeout: got %q: %v", ...)
    } else {
        cfgParams.ProxyConnectTimeout = parsedProxyConnectTimeout
    }
}
```

**字段定义**见 [internal/configs/config_params.go:81](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/config_params.go#L81)：`ProxyConnectTimeout string`。

**第 3 跳：默认值**见 [internal/configs/config_params.go:262](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/config_params.go#L262)（`NewDefaultConfigParams` 内）：`ProxyConnectTimeout: "60s"`。

**第 4 跳：下发到 location**见 [internal/configs/ingress.go:1015](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1015)（以及 L866、L895 的另两处 location 构造）：`ProxyConnectTimeout: cfg.ProxyConnectTimeout`。

**第 5 跳：渲染**见 OSS 模板 [internal/configs/version1/nginx.ingress.tmpl:368](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/version1/nginx.ingress.tmpl#L368)：

```go
proxy_connect_timeout {{$location.ProxyConnectTimeout}};
```

Plus 模板对应位置在 [internal/configs/version1/nginx-plus.ingress.tmpl:476](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/version1/nginx-plus.ingress.tmpl#L476)。注意 OSS 与 Plus 两套模板都要渲染同一个字段，这正是 CLAUDE.md 里「OSS 与 Plus 模板必须同步更新」这一不变量的由来。

**调用点**：`parseAnnotations` 由配置生成的两条路径调用——普通 Ingress 在 [internal/configs/ingress.go:249](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L249) 的 `generateNginxCfg` 里调用；Configurator 的入口在 [internal/configs/configurator.go:1451](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go#L1451)。后者把 `cnf.CfgParams`（已带默认值）作为基线传入。

#### 4.3.4 代码实践

**实践目标**：完整追踪 `nginx.org/proxy-connect-timeout` 从字符串到指令的全过程。

**操作步骤**：

1. 准备一份带注解的 Ingress（示例代码，非项目原有文件）：

   ```yaml
   apiVersion: networking.k8s.io/v1
   kind: Ingress
   metadata:
     name: cafe-ingress
     annotations:
       nginx.org/proxy-connect-timeout: "30s"
   spec:
     rules:
       - host: cafe.example.com
         http:
           paths:
             - path: /tea
               pathType: Prefix
               backend:
                 service: { name: tea-svc, port: { number: 80 } }
   ```

2. 在源码里按 4.3.3 的 6 个跳转点依次打开对应文件与行号。
3. 用 `make test` 跑 [internal/configs/annotations_test.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/annotations_test.go) 与 `version1/template_test.go`，确认 `ProxyConnectTimeout` 的快照用例存在（见 `template_test.go` 中多处 `ProxyConnectTimeout: "10s"` 断言）。

**需要观察的现象**：注解值 `"30s"` 经 `ParseTime` 后仍是 `"30s"`；若改成 `"30"` 则被归一化为 `"30s"`；最终 location 块里出现 `proxy_connect_timeout 30s;`。

**预期结果**：链路 6 跳全部可定位到具体源码行，OSS/Plus 两套模板都渲染该字段。

#### 4.3.5 小练习与答案

**练习 1**：为什么把字段从 `ConfigParams` 再拷贝一份到每个 `location`，而不是模板直接读 `ConfigParams`？

**答案**：NGINX 的 `proxy_connect_timeout` 是 `location` 级指令，模板按 `location` 迭代渲染。把值放到 `location` 结构体上，让模板在每个 location 内自包含地输出指令，逻辑更清晰；同时为 mergeable ingress 中「不同 location 继承/覆盖不同值」留出扩展空间。

**练习 2**：如果只改了 OSS 模板忘了改 Plus 模板，会发生什么？

**答案**：Plus 镜像渲染出的配置会缺失或沿用旧值，导致 OSS 与 Plus 行为不一致。这正是 CLAUDE.md 强调「两套模板必须同步」的原因。

---

### 4.4 mergeable ingress 的注解过滤与继承

#### 4.4.1 概念说明

**mergeable ingress** 是 NIC 的一种特殊 Ingress 组织方式：用一个带 `nginx.org/mergeable-ingress-type: master` 的 Ingress 定义**整台虚拟主机**（host、TLS、server 级参数），再用若干带 `nginx.org/mergeable-ingress-type: minion` 的 Ingress 各自定义**一条路径**（location）。这样不同团队可以各自维护自己的 minion，互不冲突地拼装出同一台虚拟主机。

问题随之而来：master 与 minion 都是 Ingress，都能写注解。但有些注解是 **server 级**的（如 `nginx.org/server-tokens`、`nginx.org/hsts`），有些是 **location 级**的（如 `nginx.org/rewrites`、`nginx.org/proxy-connect-timeout`）。如果把 server 级注解写在 minion 上（它不产生 server 块），或把 location 级注解写在 master 上（它的 location 是空的），就会产生无意义甚至错误的指令。

NIC 用三张表 + 三道工序解决这个问题：

| 表名 | 作用 |
| --- | --- |
| `masterDenylist` | master 不允许带的注解（location 级），写在 master 上会被剔除 |
| `minionDenylist` | minion 不允许带的注解（server 级），写在 minion 上会被剔除 |
| `minionInheritanceList` | 允许从 master 继承到 minion 的注解白名单 |

#### 4.4.2 核心流程

mergeable ingress 的注解处理由 `generateNginxCfgForMergeableIngresses` 编排，三道工序**有严格顺序**：

1. **深拷贝**：先把 master 与每个 minion 各 `DeepCopy()` 一份，避免修改缓存中的原始对象。
2. **过滤 master**：`filterMasterAnnotations` 从 master 副本删除 `masterDenylist` 里的键，记录被删清单用于日志告警。
3. **生成 master 配置**：用过滤后的 master 调 `generateNginxCfg`，得到 server 骨架。
4. **逐个处理 minion**，对每个 minion依次执行：
   - `mergeMasterAnnotationsIntoMinion`：把 master 的注解继承到 minion——**仅当 minion 自己没有该键，且该键在 `minionInheritanceList` 白名单中**。
   - `filterMinionAnnotations`：从 minion 副本删除 `minionDenylist` 里的键。
   - `generateNginxCfg`：生成 minion 的 location。

顺序很关键：**继承必须在 minion 过滤之前**。这样即便继承来的注解撞上 minionDenylist，也会被随后过滤掉，保证安全；而继承动作本身只动白名单里的注解，这些注解本来就不在 minionDenylist 里，所以不会误删。

继承的语义可以写成：

\[ \text{继承条件} = (\text{minion 未自带该键}) \land (\text{键} \in \text{minionInheritanceList}) \]

即「minion 优先，master 仅作兜底」。这让单个 minion 可以覆盖从 master 继承的值，又能在不写时拿到 master 的统一配置。

#### 4.4.3 源码精读

三张表的定义：

- master 黑名单见 [internal/configs/annotations.go:104-115](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/annotations.go#L104-L115)，剔除 `rewrites`、`ssl-services`、`grpc-services`、`websocket-services`、`sticky-cookie-services`、`health-checks*`、`use-cluster-ip` 等 location/service 级注解。
- minion 黑名单见 [internal/configs/annotations.go:117-139](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/annotations.go#L117-L139)，剔除 `server-tokens`、`hsts*`、`listen-ports*`、`server-snippets`、`ssl-ciphers`、`ssl-prefer-server-ciphers`、`app-root`、`ssl-redirect`、`redirect-to-https`、`http-redirect-code`、`appprotect*` 等 server 级注解。
- 继承白名单见 [internal/configs/annotations.go:141-168](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/annotations.go#L141-L168)，允许继承 `proxy-connect-timeout`、`proxy-read/send-timeout`、`client-max-body-size`、proxy buffer 系列、`lb-method`、`keepalive`、`max-fails`、`max-conns`、`fail-timeout`、全部 `limit-req-*` 等 location 级注解。

三个函数实现极简：

```go
func filterMasterAnnotations(annotations map[string]string) []string {
    var removedAnnotations []string
    for key := range annotations {
        if _, notAllowed := masterDenylist[key]; notAllowed {
            removedAnnotations = append(removedAnnotations, key)
            delete(annotations, key)
        }
    }
    return removedAnnotations
}
```

见 [internal/configs/annotations.go:781-792](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/annotations.go#L781-L792)（master 过滤）。minion 过滤结构完全相同，见 [internal/configs/annotations.go:794-805](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/annotations.go#L794-L805)。

继承函数是本模块最精妙的一段，见 [internal/configs/annotations.go:807-815](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/annotations.go#L807-L815)：

```go
func mergeMasterAnnotationsIntoMinion(minionAnnotations map[string]string, masterAnnotations map[string]string) {
    for key, val := range masterAnnotations {
        if _, exists := minionAnnotations[key]; !exists {       // minion 自带则不覆盖
            if _, allowed := minionInheritanceList[key]; allowed { // 仅白名单可继承
                minionAnnotations[key] = val
            }
        }
    }
}
```

三道工序的编排见 [internal/configs/ingress.go:1184-1253](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1184-L1253)。关键代码：

```go
// 深拷贝 master（见 L1186），随后过滤
removedAnnotations := filterMasterAnnotations(ncp.mergeableIngs.Master.Ingress.Annotations)   // L1188
...
for _, minion := range minions {
    originalMinion := minion.Ingress
    minion.Ingress = minion.Ingress.DeepCopy()                                                 // L1236 深拷贝 minion
    mergeMasterAnnotationsIntoMinion(minion.Ingress.Annotations, ncp.mergeableIngs.Master.Ingress.Annotations)  // L1242 继承
    removedAnnotations = filterMinionAnnotations(minion.Ingress.Annotations)                   // L1249 过滤
    ...
}
```

**为什么必须深拷贝**：`filter*` 和 `merge*` 都直接 `delete`/写入 annotations map。若不深拷贝，会修改 controller 缓存里的原始对象，导致下次 reconcile 看到的是被污染的注解。深拷贝把「生成期的临时修改」与「缓存里的真相」隔离开。

#### 4.4.4 代码实践

**实践目标**：验证「minion 自带优先 + 白名单继承」的继承规则。

**操作步骤**：

1. 阅读 [internal/configs/annotations_test.go:201-229](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/annotations_test.go#L201-L229) 的 `TestMergeMasterAnnotationsIntoMinion`：
   - master 带 `proxy-connect-timeout: "50s"`，minion 自带 `proxy-connect-timeout: "20s"`。
   - master 还带 `proxy-buffering`、`proxy-buffers`、`proxy-buffer-size`（白名单内，minion 未自带）。
   - master 还带 `hsts`、`hsts-max-age`、`JWTToken`、`add-header-inherit`（不在白名单内）。
2. 观察断言：合并后 minion 的 `proxy-connect-timeout` 仍是 `"20s"`（自带优先）；`proxy-buffering/buffers/buffer-size` 被继承；`hsts*`、`JWTToken`、`add-header-inherit` 未被继承（不在白名单）。
3. 运行测试：`make test`（或 `go test -tags=aws,helmunit ./internal/configs/ -run TestMergeMasterAnnotationsIntoMinion`）。

**需要观察的现象**：

| 注解 | master 值 | minion 原值 | 合并后 minion | 原因 |
| --- | --- | --- | --- | --- |
| `proxy-connect-timeout` | 50s | 20s | 20s | minion 自带优先 |
| `proxy-buffering` | True | （无） | True | 白名单继承 |
| `hsts` | True | （无） | （无） | 不在白名单 |
| `JWTToken` | ... | （无） | （无） | 不在白名单 |

**预期结果**：表格中的合并结果与测试断言完全一致。

#### 4.4.5 小练习与答案

**练习 1**：`mergeMasterAnnotationsIntoMinion` 为什么「minion 自带该键则跳过」？

**答案**：为了让单个 minion 能覆盖 master 的统一默认值。继承是「兜底」而非「强制」，优先尊重 minion 自己的设置。

**练习 2**：如果 `nginx.org/proxy-connect-timeout` 不在 `minionInheritanceList` 里，会有什么后果？

**答案**：master 上设置的 `proxy-connect-timeout` 不会下发给 minion，minion 只能用基线默认 `60s`。这意味着 master 设的全局超时对 minion 失效，行为违反直觉。所以维护 `minionInheritanceList` 时，凡是 location 级且希望「一处设置、全体生效」的注解都应纳入。

**练习 3**：为什么 `filterMinionAnnotations` 必须在 `mergeMasterAnnotationsIntoMinion` 之后执行？

**答案**：继承可能把 master 的注解写入 minion，若这些注解恰在 minionDenylist 里，必须由随后的过滤剔除，否则会把 server 级注解错误地带进 location。尽管当前白名单与黑名单不交集，但「先继承后过滤」的顺序在语义上保证了任何继承值都仍受 minion 过滤约束，是防御性写法。

## 5. 综合实践

把本讲的四条主线串起来，完成下面这个端到端阅读任务。

**任务**：假设有一个 mergeable ingress 部署——master Ingress 写了 `nginx.org/proxy-connect-timeout: "50s"` 和 `nginx.org/server-tokens: "False"`，某个 minion Ingress 写了 `nginx.org/proxy-connect-timeout: "20s"` 和 `nginx.org/rewrites`。

请按顺序回答：

1. `filterMasterAnnotations` 会从 master 剔除哪些注解？（参考 `masterDenylist`）
2. `nginx.org/server-tokens` 会出现在最终 server 块吗？以什么值？（它是 server 级注解，由 master 承载）
3. `mergeMasterAnnotationsIntoMinion` 执行后，minion 的 `proxy-connect-timeout` 是多少？`rewrites` 是否保留？
4. `filterMinionAnnotations` 会从 minion 剔除哪些注解？（参考 `minionDenylist`，注意 `rewrites` 不在其中）
5. 最终该 minion 生成的 location 里，`proxy_connect_timeout` 指令的值是多少？为什么不是 50s？

**参考思路**：

1. masterDenylist 里没有 `proxy-connect-timeout` 也没有 `server-tokens`，所以两者都保留在 master 上；`rewrites` 在黑名单里但它在 minion 上而非 master 上，与本步无关。
2. `server-tokens` 是 server 级、由 master 承载，会按 `False` 解析为 `off`，出现在 master 的 server 块。
3. minion 自带 `proxy-connect-timeout: "20s"`，故继承时跳过，保持 `20s`；`rewrites` 不在继承白名单，但它是 minion 自带的，不被继承逻辑删除，仍保留。
4. minionDenylist 不含 `rewrites`，故 `rewrites` 保留；不含 `proxy-connect-timeout`，也保留。
5. 该 minion 的 location 用自己解析出的 `20s`（minion 自带优先于 master 的 50s），渲染出 `proxy_connect_timeout 20s;`。这正是「minion 优先」规则的体现。

> 注：本实践为源码阅读型任务，无需真实集群即可完成；如要实跑，可参照 `examples/ingress-resources/mergeable-ingress` 下的示例 apply 到测试集群后查看生成的 NGINX 配置（**待本地验证**）。

## 6. 本讲小结

- `parseAnnotations` 是注解子系统的总入口，采用「值拷贝基线 + 逐键探测 + best-effort 降级」模式，单条注解出错只记日志不中断。
- `parsing_helpers.go` 提供两套工具：`GetMapKeyAs*` 封装「探测+转换」并用 `(value, exists, error)` 三返回值区分「没写」与「写错」；`ParseTime/Size/LBMethod` 等负责把字符串校验为合法的 NGINX 取值。
- 注解到指令是手工映射，`proxy-connect-timeout` 的链路是：注解字符串 → `ParseTime` 归一化 → `ConfigParams.ProxyConnectTimeout`（默认 `60s`）→ `location.ProxyConnectTimeout` → OSS/Plus 模板 `proxy_connect_timeout {{$location.ProxyConnectTimeout}};`。
- mergeable ingress 用 `masterDenylist` / `minionDenylist` / `minionInheritanceList` 三张表，配合「过滤 master → 继承到 minion → 过滤 minion」三道工序，保证 server 级与 location 级注解各归其位。
- 继承遵循「minion 自带优先 + 白名单限定」；过滤与继承都作用于 master/minion 的**深拷贝**，避免污染 controller 缓存。

## 7. 下一步学习建议

- 下一讲 [u4-l4 Ingress → 配置（version1）](u4-l4-ingress-config-generation.md) 将讲解 `ConfigParams` 如何与 path/upstream/TLS 组装成完整的 `IngressNginxConfig`，承接本讲解析出的注解字段。
- 若对 master/minion 的资源模型感兴趣，可先读 [docs/crd](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs) 下关于 mergeable ingress 的说明与 `examples/ingress-resources/mergeable-ingress/`。
- 想扩展新注解的读者，可先阅读 [internal/configs/annotations_test.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/annotations_test.go) 的 table-driven 用例，再参考 u8-l2「扩展实践：新增一个 Ingress 注解」。
