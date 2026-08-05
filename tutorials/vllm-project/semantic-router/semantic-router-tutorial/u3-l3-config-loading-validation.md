# 配置加载与校验

## 1. 本讲目标

本讲深入 vLLM Semantic Router（以下简称 SR）Go 端的配置子系统，回答一个贯穿全局的问题：**一份写在磁盘上的 `config.yaml`，是如何变成路由器在内存里安全使用的 `RouterConfig` 的？**

学完本讲，你应该能够：

- 说出 `config.Parse` / `config.Load` 如何把 YAML 文件解析为 `RouterConfig`，以及它们之间的区别。
- 描述「规范化（normalization）」这一步做了哪些事：环境变量展开、canonical 形态识别、recipe 拆分、全局默认叠加。
- 理解「语义校验（validation）」为什么要拆成「全局校验器」和「路由画像校验器」两个家族，以及一个具体校验器如何阻止一类错误配置。
- 说出 `config.Replace` 与「配置变更订阅」如何实现运行时热替换（hot reload），以及它在启动、管理 API、K8s 更新三条路径上的入口。

本讲承接 u3-l1（`config.yaml` v0.3 的七大顶层结构）与 u3-l2（Recipe 与四文件契约），把视角从「配置长什么样」推进到「配置如何被加载、规范化、校验并热替换」。**本讲只讲加载与校验机制本身**；决策求值（u5/u6）、投影数学（u2-l3）等内部逻辑留待后续讲义。

---

## 2. 前置知识

阅读本讲前，建议你已经具备以下概念（来自前置讲义）：

- **`config.yaml` 的七大顶层段**：`version` / `listeners` / `providers` / `routing` / `entrypoints` / `recipes` / `global`（见 u3-l1）。
- **Recipe 与 default 配方**：顶层 `routing` 本身就是 default 配方；`recipes[]` 定义其他命名配方；配方里的信号/投影/决策名字**局部于配方**，不可跨配方引用（见 u3-l2）。
- **信号-投影-决策三层模型**（见 u2-l1）：路由流水线由信号抽取、投影协调、决策求值组成。

此外，本讲会用到几个工程基础概念，先解释清楚：

| 术语 | 含义 |
| --- | --- |
| **canonical（规范形态）** | SR 当前唯一支持的配置写法，即 v0.3 的 `version/listeners/providers/routing/global` 五段式。不符合 canonical 的旧写法会被直接拒绝。 |
| **规范化（normalization）** | 把用户写的 YAML 转换成路由器内部统一的、补齐了默认值的 `RouterConfig` 结构。 |
| **语义校验（validation）** | 在结构正确的前提下，进一步检查「配置合不合业务逻辑」，例如某条决策引用的信号是否真的声明过。 |
| **热替换（hot reload）** | 不重启进程的前提下，用一份新配置替换内存中的旧配置，并通知所有订阅者。 |
| **`ConfigMap` 挂载** | Kubernetes 把配置以文件形式挂载进 Pod，常表现为符号链接（symlink），加载时要解析真实路径。 |

本讲全部代码位于 `src/semantic-router/pkg/config/`，核心是三个文件。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| [src/semantic-router/pkg/config/config.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/config.go) | 定义 `RouterConfig` 及其全部子结构的类型，以及信号族常量。 | 「加载产物长什么样」的类型骨架。 |
| [src/semantic-router/pkg/config/loader.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/loader.go) | `Parse`/`Load`/`Replace`/`Get` 的入口，串联「读取→拒绝旧字段→展开环境变量→识别 canonical→规范化→校验」全流程，并管理全局缓存与变更订阅。 | 本讲的核心，解析与热替换都在这里。 |
| [src/semantic-router/pkg/config/validator.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/validator.go) | 语义校验入口 `validateConfigStructure` / `validateConfigContracts`，以及全局校验器与路由画像校验器两个家族的注册表。 | 「校验如何组织」的总入口。 |

辅助文件（本讲会少量引用）：

- [src/semantic-router/pkg/config/canonical_config.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/canonical_config.go)：`CanonicalConfig` 类型与 `normalizeCanonicalConfig` 规范化主流程。
- [src/semantic-router/pkg/config/validator_routing_profiles.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/validator_routing_profiles.go)：`visitRoutingProfileConfigs`，逐个 recipe 投射成隔离视图再校验。
- [src/semantic-router/pkg/config/validator_signal_references.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/validator_signal_references.go)：一个具体校验器示例——校验决策引用的信号是否声明过。

---

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：**配置解析 → 规范化 → 语义校验 → 全局热替换**。这四步在源码里是一条由 `Parse` 串联起来的流水线，热替换则由 `Replace` 在另一条路径上触发。

先给一张全局流程图，记住它再往下读：

```
config.yaml (磁盘)
    │  config.Parse(path)
    ▼
[1] 读取文件 / 解析符号链接
    │
[2] 拒绝弃用/移除字段 (reject*)
    │
[3] 展开环境变量 (${VAR})
    │
[4] 识别 canonical 形态 (isCanonicalConfig)
    │
[5] 规范化 normalizeCanonicalConfig
    │     └─ 全局默认叠加 + recipe 拆分
    ▼
[6] finalizeParsedConfig
    │     └─ 语义校验 validateConfigStructure
    ▼
RouterConfig (内存)
    │  config.Replace(cfg)
    ▼
[7] 全局缓存替换 + 通知订阅者
```

---

### 4.1 配置解析：从 YAML 文件到内存结构

#### 4.1.1 概念说明

「解析」这一步要回答两个问题：

1. **从哪里读？** 文件路径可能是个符号链接（K8s `ConfigMap` 挂载的典型情况），必须解析到真实文件。
2. **读出来的字节怎么变成 Go 结构？** YAML 要先变成 `map[string]interface{}` 这种「无类型」中间形态，做完一连串预处理后，再反序列化成强类型的 `RouterConfig`。

SR 故意分成两层：先处理 `map`（这一层能做「拒绝旧字段」「展开环境变量」等不依赖类型的检查），再处理强类型结构。这样做的好处是：**类型层面的错误（拼写错误、非法结构）能在最早的阶段就被拦下，并给出可操作的迁移提示**，而不是等到反序列化时报一堆晦涩的 YAML tag 错误。

#### 4.1.2 核心流程

`config.Parse` 是「无副作用」的解析入口：它只读文件、返回 `*RouterConfig`，**不碰全局缓存**。`config.Load` 则在 `Parse` 外面套了一层 `sync.Once`，保证全局只解析一次并缓存结果。

```go
// Parse: 解析但不写全局缓存
func Parse(configPath string) (*RouterConfig, error) {
    resolved, _ := filepath.EvalSymlinks(configPath)   // 解析符号链接
    if resolved == "" { resolved = configPath }
    data, err := os.ReadFile(resolved)                  // 读真实文件
    if err != nil { return nil, ... }
    return parseYAMLBytesWithBaseDir(data, filepath.Dir(resolved))
}
```

读到的字节交给 `parseYAMLBytesWithOptions`，这是真正的解析流水线，它按顺序完成「解析预处理 → 识别 canonical → 规范化 → 收尾校验」：

```go
func parseYAMLBytesWithOptions(data []byte, baseDir string, expandEnvironment bool) (*RouterConfig, error) {
    raw, _ := parseRawConfigMap(data)               // ① 无类型 map
    rejectDeprecatedUserConfigFields(raw)           // ② 拒绝弃用字段
    rejectRemovedStructureFields(raw)               //    拒绝移除字段（一组）
    // ...更多 rejectRemoved* 检查
    if expandEnvironment { expandEnvSubstitutionsInMap(raw) }  // ③ 展开 ${VAR}
    WarnUnknownFields(raw, reflect.TypeOf(CanonicalConfig{}))  // ④ 警告拼写错误
    cfg, err := parseRouterConfigPayload(expandedData, raw)    // ⑤ 识别 canonical + 规范化
    cfg.ConfigBaseDir = baseDir
    cfg.DocumentHash = hex.EncodeToString(sha256.Sum256(data)  // ⑥ 记录文档指纹
    cfg.SkipExternalAssetValidation = !expandEnvironment
    finalizeParsedConfig(cfg)                                  // ⑦ 收尾 + 语义校验
    return cfg, nil
}
```

#### 4.1.3 源码精读

**`Parse` 入口与符号链接解析**——`EvalSymlinks` 是为了正确处理 K8s `ConfigMap` 的挂载方式：

[src/semantic-router/pkg/config/loader.go:52-77](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/loader.go#L52-L77) —— 读文件前先用 `filepath.EvalSymlinks` 解析符号链接，拿不到真实路径时回退到原始路径；读完后把「所在目录」作为 `baseDir` 传下去，用于后续解析相对资产路径（如模型文件）。

**`Parse` vs `Load` 的区别**——`Load` 用 `sync.Once` 包住 `Parse`，把结果存进包级全局 `config` 变量：

[src/semantic-router/pkg/config/loader.go:32-49](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/loader.go#L32-L49) —— `configOnce.Do` 保证只解析一次；`configMu`（`sync.RWMutex`）保护全局缓存，读多写少。

**解析流水线主体**——这是本讲最重要的一段代码，它把「拒绝旧字段」「展开环境变量」「识别 canonical」「规范化」「收尾」串成一条线：

[src/semantic-router/pkg/config/loader.go:95-151](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/loader.go#L95-L151) —— 关键点：① 先转成无类型 `map`；② 一连串 `rejectRemoved*` 把旧版字段挡在门外；③ 仅在 `expandEnvironment=true` 时展开 `${VAR}`；④ `WarnUnknownFields` 对拼写错误只告警不报错；⑤ 末尾 `finalizeParsedConfig` 触发语义校验。

**`RouterConfig` 是加载的产物**——它是配置在内存中的统一形态。注意它大量使用 Go 的**匿名嵌入（anonymous embedding）**，把多个子配置「平铺」进来：

[src/semantic-router/pkg/config/config.go:60-104](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/config.go#L60-L104) —— 几个关键字段：`IntelligentRouting`（内联嵌入，镜像 default 配方的 signals/projections/decisions/strategy）、`Entrypoints` 与 `Recipes`（标记为 `yaml:"-"`，即**不直接来自 YAML**，而是规范化阶段从 `entrypoints`/`recipes` 段拼出来的多配方状态）、`DocumentHash`（标记为 `yaml:"-"`，运行期由 SHA-256 计算，管理 API 用它区分「已持久化的配置」与「刚热替换完的配置」）。

> 小提示：`yaml:"-"` 表示该字段不参与 YAML 反序列化，只在运行期由代码填充。`Entrypoints`/`Recipes`/`RoutingScope`/`DocumentHash` 都是这种「派生字段」，这也是为什么 YAML 顶层结构和 `RouterConfig` 字段不能一一对应——中间隔了一层规范化。

#### 4.1.4 代码实践

**实践目标**：跟踪一次 `config.Parse` 的执行，搞清「YAML 的哪个段最终落到 `RouterConfig` 的哪个字段」。

**操作步骤**：

1. 打开 [src/semantic-router/pkg/config/config.go:60-104](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/config.go#L60-L104)，把 `RouterConfig` 的字段抄成一张表。
2. 对照 u3-l1 讲过的七大顶层段（`version/listeners/providers/routing/entrypoints/recipes/global`），在表上标注：哪个顶层段对应哪个嵌入子结构。例如 `routing` → `IntelligentRouting`，`global` → 多个全局嵌入（`InlineModels`/`SemanticCache`/`Memory`/`VectorStore` 等）。
3. 特别注意三个标了 `yaml:"-"` 的派生字段：`Entrypoints`、`Recipes`、`DocumentHash`，说明它们为什么不能直接从 YAML 读出来。

**需要观察的现象**：你会发现 YAML 的 `routing` 段和 `RouterConfig.IntelligentRouting` 几乎是镜像，但 `entrypoints`/`recipes` 段在 `RouterConfig` 里没有同名字段——它们被规范化「消化」成了 `Entrypoints []EntrypointMapping` 和 `Recipes []RoutingRecipe`。

**预期结果**：得到一张「顶层段 → RouterConfig 字段」的映射表，并能解释派生字段的来源。

---

### 4.2 规范化：拒绝旧字段、展开环境变量、叠加默认值

#### 4.2.1 概念说明

「规范化」是解析流水线里最厚的一层，目标是把**千差万别的用户输入**收敛成**唯一的内部表示**。它做四件事：

1. **拒绝弃用/移除字段**：旧版本支持的写法在新版里已经不存在了，遇到就直接报错并给出迁移命令，避免「静默忽略导致行为不符预期」。
2. **展开环境变量**：YAML 里的 `${VAR}` 占位符会被进程环境变量替换，方便在不同环境（开发/生产）复用同一份配置，同时不把密钥写进文件。
3. **识别 canonical 形态**：当前唯一合法的写法是 v0.3 五段式，识别失败就拒绝。
4. **叠加全局默认 + 拆分 recipe**：以 `DefaultGlobalConfig()` 为底，把用户配置覆盖上去，并把 `entrypoints`/`recipes` 拆成多个隔离的配方视图。

为什么拒绝旧字段要用「报错」而不是「兼容」？因为 SR 的配置语义在不断演进（例如把 `routing.signals.category_kb` 迁移到 `global.model_catalog.kbs[]`），如果静默忽略旧字段，用户会以为配置生效了，实则路由行为已变。**显式报错 + 迁移提示**是把「静默错误」变成「确定性的启动失败」，这比线上出问题安全得多。

#### 4.2.2 核心流程

规范化主流程在 `normalizeCanonicalConfig`，它先做一次「canonical 契约校验」，再把全局段、路由段、recipe 段、provider 段依次应用到一份 `DefaultGlobalConfig()` 上：

```go
func normalizeCanonicalConfig(canonical *CanonicalConfig) (*RouterConfig, error) {
    validateCanonicalContract(canonical)          // canonical 结构契约
    global := resolveCanonicalGlobal(...)          // 解析全局段
    cfg := DefaultGlobalConfig()                   // 以默认配置为底
    applyCanonicalGlobal(&cfg, &global)            // 叠加全局
    applyCanonicalRoutingState(&cfg, canonical)    // 叠加路由（signals/projections/decisions）
    applyCanonicalRecipeState(&cfg, canonical)     // 拆分 entrypoints/recipes
    applyCanonicalProviderState(&cfg, ...)         // 叠加 providers
    return &cfg, nil
}
```

环境变量展开则更早，发生在 `map` 层面，递归遍历所有字符串值：

```go
func expandEnvSubstitutionsInMap(raw map[string]interface{}) {
    for key, value := range raw {
        raw[key] = expandEnvSubstitutionsInValue(value)  // 递归
    }
}
```

#### 4.2.3 源码精读

**拒绝旧字段**——一组 `rejectRemoved*` 函数，每个针对一类已废弃写法。以「分类法遗留字段」为例：

[src/semantic-router/pkg/config/loader.go:184-205](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/loader.go#L184-L205) —— 检测到 `routing.signals.category_kb`、`routing.signals.taxonomy` 或 `global.model_catalog.classifiers` 时，直接返回错误，并指明应迁移到 `global.model_catalog.kbs[]` 加 `routing.signals.kb[]`。注意它操作的是 `map[string]interface{}`，不依赖任何 Go 类型——这正是「先 map 后类型」分层的好处。

> 还有一类重要的拒绝：[src/semantic-router/pkg/config/loader.go:339-409](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/loader.go#L339-L409) 的 `rejectUnsupported*` 系列用「白名单」思路检查 `global.router.learning` 等新模块——只允许列出的字段存在，其余一律报错。这能防止用户在新模块里写错字段名而无人察觉。

**环境变量展开**——递归处理任意嵌套深度的 YAML：

[src/semantic-router/pkg/config/env_substitution.go:15-19](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/env_substitution.go#L15-L19) —— 对 map 的每个 value 递归调用 `expandEnvSubstitutionsInValue`，从而覆盖列表、嵌套 map 等所有结构。

值得注意：解析流水线还有一个「不展开环境变量」的入口 `ParseYAMLBytesWithoutEnvExpansion`（[loader.go:91-93](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/loader.go#L91-L93)）。它给只读校验 API 用——这类请求**不能**把进程里的密钥展开后返回给调用方，所以要保持 `${VAR}` 原样。同时它会设置 `SkipExternalAssetValidation=true`，阻止任何文件系统读取（见 4.3）。

**canonical 形态识别**——判断一份配置是否符合当前唯一支持的写法：

[src/semantic-router/pkg/config/canonical_config.go:76-80](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/canonical_config.go#L76-L80) —— 只要存在 `routing` 或 `global` 段就视为 canonical；否则进入 `canonicalConfigRequiredError`，提示用 `vllm-sr config migrate` 迁移。

**规范化主流程**——把默认配置与用户配置合并：

[src/semantic-router/pkg/config/canonical_config.go:82-110](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/canonical_config.go#L82-L110) —— 顺序很关键：先 `validateCanonicalContract`（结构契约），再 `resolveCanonicalGlobal`（解析全局），然后以 `DefaultGlobalConfig()` 为底依次叠加全局、路由、recipe、provider 四块状态。`applyCanonicalRoutingState` 里还会做 `normalizeSignals` / `normalizeProjections`，把 DSL 风格的声明整理成统一形态。

#### 4.2.4 代码实践

**实践目标**：亲手触发一次「拒绝旧字段」的错误，观察它的报错信息。

**操作步骤**：

1. 复制一份有效配置（例如 `config/recipes/balance/config.yaml`）到一个临时文件 `/tmp/bad.yaml`。
2. 在 `routing` 段下故意加一个已移除的字段，例如：

   ```yaml
   routing:
     signals:
       taxonomy:
         - name: stale_signal
   ```

3. 运行校验（任选其一）：

   ```bash
   vllm-sr validate --config /tmp/bad.yaml
   # 或
   go run ./src/semantic-router/cmd/dsl validate /tmp/bad.yaml
   ```

**需要观察的现象**：校验**直接失败**，错误信息应明确指出 `routing.signals.taxonomy is no longer supported; migrate to routing.signals.kb[]`，而不是把这条规则静默忽略。

**预期结果**：你看到一条带迁移指引的报错。这验证了「拒绝旧字段」发生在类型化解析之前，且信息可操作。如果当前环境无法运行，明确标注「待本地验证」，但可以引用 [loader.go:184-205](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/loader.go#L184-L205) 说明预期报错文本。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `rejectRemovedTaxonomyLegacyFields` 要操作 `map[string]interface{}` 而不是直接用 `*RouterConfig`？

> **答案**：因为这些字段在新版 `RouterConfig` 类型里已经**不存在了**，无法反序列化到强类型结构；只有在「无类型 map」阶段才能检测到它们的存在并报错。这也体现了「先 map 后类型」分层的价值。

**练习 2**：`ParseYAMLBytesWithoutEnvExpansion` 设置 `SkipExternalAssetValidation=true` 的目的是什么？

> **答案**：这个入口服务于只读校验 API，这类请求来自不可信调用方，绝不能触发文件系统读取（否则可能泄露服务器文件信息），也不应把进程环境变量（可能含密钥）展开进返回结果。

---

### 4.3 语义校验：家族化拆分的校验器

#### 4.3.1 概念说明

「语义校验」回答的是：**配置结构上正确，但业务逻辑上合不合理？** 例如：

- 一条决策的 WHEN 规则引用了 `keyword("faq")`，但这个 recipe 里根本没声明 `faq` 这个 keyword 信号——这是「跨配方误引用」，结构上合法，语义上错误。
- 某个全局子能力（如语义缓存）和某个路由画像（recipe）的配置互相矛盾。

如果不在加载时校验，这类错误会变成**请求时才暴露的静默不匹配**（路由永远命中不了，却没人知道为什么）。语义校验的职责就是把它们变成**确定性的启动错误**。

SR 把校验器拆成**两个家族**：

| 家族 | 注册表 | 作用域 | 典型校验 |
| --- | --- | --- | --- |
| **全局校验器** | `globalConfigContractValidators` | 整个 `RouterConfig`（跨配方共享的基础设施） | 语义缓存契约、记忆契约、嵌入模型契约、模型选择配置 |
| **路由画像校验器** | `routingProfileContractValidators` | **单个 recipe 的隔离视图** | 决策信号引用、语言契约、投影契约、域契约 |

为什么要分两个家族？因为**全局配置只该校验一次**，而**路由画像（signals/projections/decisions）必须每个 recipe 独立校验**——这正是 u3-l2 强调的「配方局部性」：recipe A 的信号不能被 recipe B 的决策引用。把路由校验器放在「逐 recipe 视图」上跑，跨配方引用自然就成了可见的错误。

#### 4.3.2 核心流程

校验入口 `validateConfigStructure` 先判断配置来源：K8s 模式下路由状态还没合并进来，跳过；否则调用 `validateConfigContracts`：

```go
func validateConfigContracts(cfg *RouterConfig) error {
    runConfigContractValidators(cfg, globalConfigContractValidators)              // ① 全局家族
    visitRoutingProfileConfigs(cfg, func(profile *RouterConfig) error {           // ② 逐 recipe
        return runConfigContractValidators(profile, routingProfileContractValidators)
    })
}
```

`visitRoutingProfileConfigs` 是「逐 recipe 隔离」的关键：它把每个 recipe 投射成一个**只含该配方路由状态的 `RouterConfig` 视图**（`ConfigForRecipe`），再把路由画像校验器跑在这个视图上。default 配方则用原始 `cfg` 当视图。

```
RouterConfig (含多个 Recipes)
   │
   ├─ 全局家族 (跑一次)
   │
   └─ 对每个 recipe:
        ConfigForRecipe(recipe) → 隔离视图
          └─ 路由画像家族 (跑在这个视图上)
```

每个校验器都是同一个签名 `func(*RouterConfig) error`（类型别名 `configContractValidator`），`runConfigContractValidators` 顺序执行，**任何一个返回错误就立即短路返回**。

#### 4.3.3 源码精读

**校验入口与短路**——`validateConfigStructure` 是收尾阶段 `finalizeParsedConfig` 调用的：

[src/semantic-router/pkg/config/validator.go:122-147](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/validator.go#L122-L147) —— 注意 K8s 模式（`ConfigSourceKubernetes`）会跳过初始校验，因为此时路由状态（decisions/model_config）还没从 CRD 合并进来；待 Operator 调和完成后，会显式调用 `ValidateKubernetesConfigContracts` 再补校验。`validateConfigContracts` 先跑全局家族，再逐 recipe 跑路由画像家族。

**两个家族的注册表**——看清校验器的组织方式：

[src/semantic-router/pkg/config/validator.go:21-56](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/validator.go#L21-L56) —— 全局家族 13 个校验器（语义缓存、记忆、嵌入模型、模态、模型选择、分类运行时、Router Learning、ReMoM、Fusion、Flow、工具过滤、Prompt 压缩、幻觉）；路由画像家族 17 个校验器（路由局部名、语言、策略、**决策信号引用**、域、结构、reask、投影、知识库、会话、决策、决策级语义缓存/记忆、嵌入信号、模态、复杂度、决策级 Router Learning）。家族化拆分让「加一个校验器」变成「往对应 slice 里加一个函数」。

**逐 recipe 投射视图**——保证配方局部性的机制：

[src/semantic-router/pkg/config/validator_routing_profiles.go:8-23](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/validator_routing_profiles.go#L8-L23) —— 没有 `Recipes` 时直接对 `cfg`（即 default 配方）跑校验；否则对每个 recipe 调用 `cfg.ConfigForRecipe(recipe)` 生成隔离视图再校验。错误会用 `wrapRoutingProfileValidationError` 包上 `routing recipe "<name>":` 前缀，告诉你错在哪个配方。

**`ConfigForRecipe` 如何制造隔离视图**——把指定配方的路由状态「盖」到一个 `RouterConfig` 副本上：

[src/semantic-router/pkg/config/recipes.go:201-222](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/recipes.go#L201-L222) —— 关键细节：拷贝一份 `RouterConfig`，设置 `RoutingScope = recipe.Name`，用该 recipe 的 `Signals/Projections/Decisions/Strategy` 覆盖内联的 `IntelligentRouting`，并**清空 `Recipes`**（防止辅助函数 `AllRoutingDecisions` 又逃逸到别的配方）。`RoutingScope` 非空就是「严格引用模式」的信号（见下面的校验器示例）。

**一个具体校验器：决策信号引用**——这是「把静默不匹配变成启动错误」的典型例子：

[src/semantic-router/pkg/config/validator_signal_references.go:12-25](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/validator_signal_references.go#L12-L25) —— `validateDecisionSignalReferences` 收集该配方里**投影层声明过的信号**（`projectionDeclaredSignals`），然后遍历每条决策的规则树，检查每个叶子节点引用的信号是否声明过。`strictReferences = cfg.RoutingScope != ""` 表示：只有在「单配方隔离视图」上才严格检查——这正是它必须跑在 `ConfigForRecipe` 视图上的原因。

[src/semantic-router/pkg/config/validator_signal_references.go:47-78](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/validator_signal_references.go#L47-L78) —— 叶子节点校验逻辑：projection 引用跳过（投影输出名由投影 DAG 校验负责）；不支持的信号类型直接报错；名字为空报错；**未在配方内声明的信号引用**报错 `signal X("Y") is not declared in this recipe`。注释点明了设计意图：*「在 ConfigForRecipe 上跑这个校验器，能把跨配方引用变成确定性的启动错误，而不是请求时的静默不匹配。」*

#### 4.3.4 代码实践

**实践目标**：选一个校验器，说清它阻止了哪类错误配置（本讲指定练习）。

**操作步骤**：

1. 打开 [validator.go:21-56](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/validator.go#L21-L56)，选 `validateDecisionSignalReferences`（决策信号引用）作为分析对象。
2. 阅读它的实现 [validator_signal_references.go:12-78](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/validator_signal_references.go#L12-L78)。
3. 用一句话描述它阻止的错误：**「某条决策的 WHEN 规则引用了一个在该 recipe 内从未声明的信号（或跨配方引用了别的 recipe 的信号）。」**
4. 构造一个会触发它的配置：在 `balance` recipe 的某条决策里写一条引用 `keyword("nonexistent")` 的规则，但不在 `signals.keywords` 里声明 `nonexistent`，然后运行 `vllm-sr validate --config <path>`。

**需要观察的现象**：校验失败，错误形如 `routing recipe "balance": routing.decisions["..."]: signal keyword("nonexistent") is not declared in this recipe`。

**预期结果**：你能复现这条错误，并解释如果没有这个校验器，这条决策在请求时会**永远命中不了**，却没有任何报错——这正是「静默不匹配」的危害。

> 如果无法本地运行，标注「待本地验证」，但仍可引用源码注释 [validator_signal_references.go:8-11](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/validator_signal_references.go#L8-L11) 说明设计意图。

#### 4.3.5 小练习与答案

**练习 1**：`validateDecisionSignalReferences` 为什么只在 `RoutingScope != ""`（即 `ConfigForRecipe` 视图）时做严格检查？

> **答案**：严格检查依赖「该配方声明了哪些信号」这一上下文，而全局 `RouterConfig` 里 `IntelligentRouting` 只镜像 default 配方、不含其他 recipe 的局部声明。只有在 `ConfigForRecipe` 投射出的单配方视图上，才能正确判断一个信号引用是否「本配方内声明过」，从而发现跨配方误引用。

**练习 2**：为什么全局校验器只跑一次，而路由画像校验器要逐 recipe 跑？

> **答案**：全局配置（语义缓存、记忆、嵌入模型等）是跨配方共享的基础设施，校验一次即可；而 signals/projections/decisions 是**配方局部**的，每个 recipe 有自己独立的一套，必须各自在隔离视图上校验，才能保证配方间的引用不串。

---

### 4.4 全局热替换：Replace 与配置变更订阅

#### 4.4.1 概念说明

到这里，一份配置已经成功解析、规范化、校验成 `RouterConfig`。但 SR 是一个长运行的进程，配置不可能只在启动时读一次——管理 API 改了配置、K8s 更新了 `ConfigMap`，都需要**不重启进程**地换上新配置。这就是「热替换」。

SR 的热替换由三件事支撑：

1. **全局缓存**：包级变量 `config *RouterConfig`，配合 `sync.RWMutex`，所有读路径通过 `config.Get()` 拿当前配置。
2. **`config.Replace(newCfg)`**：写路径，原子地换掉全局缓存。
3. **变更订阅**：关心配置变化的子系统（如分类服务、记忆服务）通过 `SubscribeConfigUpdates` 注册一个 channel，`Replace` 后会收到通知。

这套机制让「配置更新」从「重启进程」降级为「一次函数调用 + 一次 channel 通知」，是 SR 控制面（apiserver）能在线改配置的基础。

#### 4.4.2 核心流程

`Replace` 做两件事：换缓存、发通知。

```go
func Replace(newCfg *RouterConfig) {
    // ① 加写锁，换掉全局缓存
    configMu.Lock(); config = newCfg; configErr = nil; configMu.Unlock()

    // ② 复制订阅者快照（避免持锁发通知）
    subscribers := snapshot(configUpdateSubscribers)

    // ③ 向每个订阅者非阻塞发通知
    for id, ch := range subscribers {
        select { case ch <- newCfg: ...   // 发成功
                 default: warn(...) }     // channel 满了就丢弃并告警
    }
}
```

注意几个并发要点：

- 发通知前**先复制订阅者快照**，再释放 `configUpdateMu`，避免在持锁状态下做可能阻塞的 channel 发送。
- channel 发送是**非阻塞**的（`select` + `default`）：如果订阅者来不及消费（channel 满），通知被丢弃并记一条告警日志，**绝不阻塞 `Replace`**——因为 `Replace` 经常在请求处理的关键路径上，绝不能被慢消费者拖住。

#### 4.4.3 源码精读

**`Replace` 的实现**——换缓存 + 通知，并发安全：

[src/semantic-router/pkg/config/loader.go:682-724](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/loader.go#L682-L724) —— 先用 `configMu`（写锁）换掉全局 `config` 并清空 `configErr`；再在 `configUpdateMu` 下复制订阅者 map 的快照；最后逐个非阻塞发送。注意 `default` 分支：channel 满时记 `config_update_notification_skipped`（reason=`channel_full`）告警，不阻塞。

**订阅注册与取消**——`SubscribeConfigUpdates` 返回一个带 `Close` 的订阅句柄：

[src/semantic-router/pkg/config/loader.go:757-778](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/loader.go#L757-L778) —— 用 `atomic.AddUint64` 生成唯一订阅 ID，注册一个带缓冲 channel；`Close` 用 `sync.Once` 保证只执行一次，从订阅表删除并关闭 channel。新代码被鼓励用 `SubscribeConfigUpdates` 而非旧版 `WatchConfigUpdates`（后者无法显式释放订阅）。

**热替换的三条入口路径**——`Replace` 在源码里有三类调用方，对应三种配置更新场景：

1. **启动路径**：[src/semantic-router/cmd/main.go:25](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/cmd/main.go#L25) —— `config.Replace(cfg)` 在启动早期把解析好的配置发布为全局默认。配置本身由 `loadRuntimeConfigOrFatal`（[runtime_bootstrap.go:99-119](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/cmd/runtime_bootstrap.go#L99-L119)）通过 `config.Parse` 加载，加载失败直接 `fatal`。
2. **ExtProc router 构建路径**：[src/semantic-router/pkg/extproc/router_build.go:62](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/router_build.go#L62) 和 [:82](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/router_build.go#L82) —— `NewOpenAIRouter` / `newOpenAIRouterForServer` 构建好 router 后把配置发布到全局。注意 [:107-117](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/router_build.go#L107-L117) 的 `loadRouterConfig` 会先看全局缓存里是否已是 K8s 源配置，是就直接复用，否则才 `Parse` 文件——这是 K8s 模式下「路由状态后合并」的体现。
3. **管理 API 热替换路径**：[src/semantic-router/pkg/apiserver/runtime_config.go:42-89](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/apiserver/runtime_config.go#L42-L89) —— `liveRuntimeConfig.Update` 和 `publishConfigMutation` 是控制面在线改配置的入口：优先走「注册的 updater / runtimeRegistry.UpdateConfig」，兜底直接 `config.Replace`。这层封装让 apiserver 可以在「运行时 Registry 已就绪」时走更重的更新（同步分类服务 `RefreshRuntimeConfig`），否则退化为简单 `Replace`。

> 一句话总结三条路径：**启动时 `main.go` 发一次；router 构建时发一次；运行期由 apiserver 在配置变更时发。** 三者共用同一个全局缓存与订阅机制。

#### 4.4.4 代码实践

**实践目标**：跟踪一次「管理 API 改配置 → 全局缓存替换 → 订阅者收到通知」的调用链。

**操作步骤**：

1. 从 [runtime_config.go:68-89](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/apiserver/runtime_config.go#L68-L89) 的 `publishConfigMutation` 出发，画出调用链：`publishConfigMutation` → `runtimeConfig.Update` → `config.Replace` → 遍历订阅者发通知。
2. 用 Grep 在 `pkg/config/loader_subscription_test.go` 里找订阅相关的测试，阅读它如何注册订阅、触发 `Replace`、断言收到通知。
3. 思考：如果某个订阅者的 channel 缓冲太小、消费太慢，会发生什么？结合 [loader.go:709-723](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/loader.go#L709-L723) 的 `default` 分支回答。

**需要观察的现象**：调用链清晰展示「apiserver 改配置」如何最终走到 `config.Replace` 的非阻塞通知循环。

**预期结果**：你能解释「慢消费者不会拖垮热替换」这一设计——因为发送是 `select`+`default` 非阻塞的，channel 满就丢弃通知并告警。订阅者若丢失了通知，需自行通过 `config.Get()` 兜底读取最新配置。

**待本地验证**：如要实际观察告警日志 `config_update_notification_skipped`，需构造一个 buffer=0/1 的订阅者且故意不消费，再触发 `Replace`——能否在本地复现取决于运行环境。

#### 4.4.5 小练习与答案

**练习 1**：`Replace` 在发送通知前为什么要先复制订阅者 map 的快照、再释放 `configUpdateMu`？

> **答案**：为了不在持锁状态下做可能阻塞的 channel 发送。若持锁发送，一旦某个订阅者 channel 满（且采用阻塞发送），`Replace` 会被卡住，而 `Replace` 可能在请求处理关键路径上，会导致整个路由器阻塞。复制快照后释放锁，发送阶段就允许其他 goroutine 并发注册/取消订阅。

**练习 2**：为什么 channel 发送要用 `select` + `default`（非阻塞）而不是直接 `ch <- newCfg`？

> **答案**：直接发送在 channel 满时会阻塞，慢消费者会拖住 `Replace`。非阻塞发送保证 `Replace` 永不因订阅者而阻塞；代价是可能丢失通知（记一条告警），订阅者需自行用 `config.Get()` 兜底。这是「优先保住关键路径」的取舍。

---

## 5. 综合实践

**任务**：完整跟踪一次「配置从磁盘到内存再到热替换」的全过程，画出一张带文件名与行号的调用链图。

**步骤**：

1. **解析阶段**：从 [cmd/runtime_bootstrap.go:106](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/cmd/runtime_bootstrap.go#L106) 的 `config.Parse(configPath)` 开始，沿 `Parse` → `parseYAMLBytesWithBaseDir` → `parseYAMLBytesWithOptions`（[loader.go:95-151](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/loader.go#L95-L151)）走，标注每一站做了什么（读文件、拒绝旧字段、展开 `${VAR}`、识别 canonical）。

2. **规范化阶段**：进入 `parseRouterConfigPayload` → `parseCanonicalConfigPayload` → `normalizeCanonicalConfig`（[canonical_config.go:82-110](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/canonical_config.go#L82-L110)），列出它依次调用的四个 `applyCanonical*` 函数。

3. **校验阶段**：进入 `finalizeParsedConfig`（[loader.go:517-535](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/loader.go#L517-L535)）→ `validateConfigStructure` → `validateConfigContracts`（[validator.go:122-147](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/validator.go#L122-L147)），标注全局家族与逐 recipe 路由画像家族。

4. **热替换阶段**：从 [cmd/main.go:25](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/cmd/main.go#L25) 的 `config.Replace(cfg)` 到 [loader.go:682-724](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/loader.go#L682-L724)，画出「换缓存 → 快照订阅者 → 非阻塞通知」三步。

5. **整理产物**：把以上四阶段拼成一张纵向流程图，每一步标上 `文件:行号`。再用一句话回答本讲开篇的问题——*「一份磁盘上的 `config.yaml` 如何变成路由器内存里安全使用的 `RouterConfig`」*。

**验收标准**：图里应包含至少 8 个带行号的源码锚点，并覆盖全部四个最小模块（解析、规范化、校验、热替换）。

---

## 6. 本讲小结

- **配置解析分两层**：先把 YAML 转成无类型 `map` 做预处理（拒绝旧字段、展开 `${VAR}`），再反序列化成强类型 `RouterConfig`；`Parse` 无副作用，`Load` 在其外包一层 `sync.Once` 做全局缓存。
- **`RouterConfig` 有派生字段**：`Entrypoints`/`Recipes`/`RoutingScope`/`DocumentHash` 都标了 `yaml:"-"`，由规范化阶段从 `entrypoints`/`recipes` 段拼出，不直接来自 YAML。
- **规范化**（`normalizeCanonicalConfig`）以 `DefaultGlobalConfig()` 为底，依次叠加全局、路由、recipe、provider 四块状态，把用户输入收敛成唯一内部表示；拒绝旧字段用「报错 + 迁移提示」把静默错误变成确定性启动失败。
- **语义校验分两个家族**：全局校验器跑一次，路由画像校验器逐 recipe 在 `ConfigForRecipe` 隔离视图上跑，从而把「跨配方误引用」变成可见的启动错误（典型例子 `validateDecisionSignalReferences`）。
- **热替换**由全局缓存 + `config.Replace` + 订阅 channel 三件套支撑；`Replace` 发通知是非阻塞的（`select`+`default`），慢消费者不会拖住关键路径。
- **三条 `Replace` 入口**：启动（`main.go`）、router 构建（`router_build.go`）、管理 API 热替换（`apiserver/runtime_config.go`），分别对应「开机一次」「建 router 一次」「运行期在线改」。

---

## 7. 下一步学习建议

- **进入请求主链路**：配置加载是「静态准备」，下一站应进入 u4-l1（`main.go` 启动序列）与 u4-l3（ExtProc 服务），看启动序列如何把本讲加载的 `RouterConfig` 交给运行时 Registry 与请求处理器。
- **读决策引擎消费配置**：本讲的校验器只检查「引用是否声明」，真正的规则求值在 u5-l2（决策求值管线）与 u6-l1（决策引擎）。可对照阅读，看 `Decisions`/`RuleNode` 是如何被消费的。
- **深入 recipe 局部性**：若对配方隔离感兴趣，可读 `recipes.go` 的 `ConfigForRecipe` 与 `AllRoutingDecisions`，理解 default 配方与命名配方的视图差异。
- **YAML↔DSL 往返**：本讲只讲了 YAML 加载；u7（路由 DSL）会讲 DSL 如何编译成同一份 `RouterConfig`、又能反编译回 YAML，与本讲的规范化形成闭环。
