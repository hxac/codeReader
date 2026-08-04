# Feature Gates 控制器开关机制

## 1. 本讲目标

本讲把上一讲（u2-l1）只用过接口、没展开内部的「Feature Gate」彻底讲透。读完本讲你应该能够：

- 说清 `--controllers` 这个命令行参数的语法：`*` 通配、裸名字、`-` 前缀分别表示什么。
- 解释 `pkg/features/features.go` 里的 `EnabledControllers` 映射是怎么从字符串一步步填出来的。
- 预测一段给定的 `--controllers` 字符串最终启用 / 禁用了哪些控制器，并知道**顺序敏感**带来的陷阱。
- 追踪 `IsControllerEnabled` 是如何在 `controller.Initialize` 中决定哪些控制器的 `Add` 函数被收集的。

## 2. 前置知识

在动手之前，先建立两个直觉。

**直觉一：开关是“白名单 + 黑名单”二选一的模式选择。**
AIBrix 的控制器管理器是一个进程里塞了七类控制器（自动伸缩、模型适配、模型路由、模型激活、分布式推理、KV Cache、StormService）。不同安装场景需要的子集不同。`--controllers` 就是一个总开关，让你用一行字符串表达「我要哪些、不要哪些」。

它有两种用法：

| 模式 | 写法 | 含义 |
| --- | --- | --- |
| 白名单（allowlist） | `pod-autoscaler-controller,model-claim-controller` | **只启用**列出的这几个，其余全部默认禁用 |
| 黑名单（blacklist） | `*,-kv-cache-controller` | 先启用全部，再排除指定的几个 |

**直觉二：开关的载体是一张 `map[string]bool`。**
这是理解后面一切行为的关键。Go 的 map 对同一个 key 的多次赋值会**覆盖**（最后一次写生效）。这决定了 `*` 和 `-foo` 出现的先后顺序会影响最终结果——我们会在 4.3 节专门讲这个坑。

> 名词速查：
> - **Feature Gate**：源自 Kubernetes 社区（component-base）的叫法，指用一组开关控制特性是否启用的机制。AIBrix 借用了这个名字，但实现很轻量，就是下面要讲的 map。
> - **map（映射）**：Go 语言的哈希表，`map[string]bool` 表示「字符串键 → 布尔值」。同一个键后写覆盖先写。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pkg/features/features.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/features/features.go) | Feature Gate 的全部实现：控制器名字常量、`EnabledControllers` 映射、解析（`InitControllers`）、校验（`ValidateControllers`）、查询（`IsControllerEnabled`）、全开（`EnableAllControllers`）。 |
| [pkg/controller/controller.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/controller.go) | Feature Gate 的**消费方**。`Initialize` 函数对每个能力域调用 `IsControllerEnabled`，决定是否把对应控制器的 `Add` 函数收集进 `controllerAddFuncs`。 |
| [cmd/controllers/main.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/controllers/main.go) | 启动入口。定义 `--controllers` flag（默认 `*`），先 `ValidateControllers` 校验、再 `InitControllers` 填充映射。 |
| [test/integration/controller/suit_test.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/test/integration/controller/suit_test.go) | 集成测试里直接调用 `features.InitControllers("*")`，是理解「全开」语义的最短示例。 |

## 4. 核心概念与源码讲解

### 4.1 控制器开关的数据结构：常量与 `EnabledControllers` 映射

#### 4.1.1 概念说明

开关机制要存在，首先得有一个「能被打开或关闭的东西的集合」。AIBrix 把每类控制器定义成一个**字符串常量**（比如 `"pod-autoscaler-controller"`），再用一张 `map[string]bool` 记录「这个名字 → 开/关」。这样：

- 命令行里写的是字符串，和常量一一对应，便于人读。
- 程序里查询时用常量名，避免拼写错误。
- 新增控制器只需加一个常量，再把它登记进两处清单（见 4.1.3）。

#### 4.1.2 核心流程

```
┌─────────────────────────────────────────────┐
│  控制器名字常量（7 个字符串）                 │
│  PodAutoscalerController = "pod-autoscaler-…" │
│  KVCacheController      = "kv-cache-…"        │
│  ...                                          │
└──────────────────┬──────────────────────────┘
                   │ 登记进
                   ▼
   ┌────────────────────────────────────┐
   │ ValidControllers []string           │ ← 校验白名单（4.2）
   │ EnableAllControllers()              │ ← `*` 触发（4.3）
   └────────────────────────────────────┘
                   │
                   ▼
   ┌────────────────────────────────────┐
   │ EnabledControllers map[string]bool  │ ← 真正的开关状态
   └────────────────────────────────────┘
```

#### 4.1.3 源码精读

七个控制器常量集中声明在文件顶部，名字即字符串值：

[pkg/features/features.go:24-33](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/features/features.go#L24-L33) 定义了全部控制器名字常量。StormService 那行还有一条注释，说明像 roleset 这类**内部**控制器不需要单独登记，复用顶层名字即可：

```go
const (
    PodAutoscalerController        = "pod-autoscaler-controller"
    DistributedInferenceController = "distributed-inference-controller"
    ModelAdapterController         = "model-adapter-controller"
    ModelRouteController           = "model-route-controller"
    KVCacheController              = "kv-cache-controller"
    ModelClaimController           = "model-claim-controller"
    // there's no need to register internal controllers like roleset,
    // just use top-level controller name
    StormServiceController = "stormservice-controller"
)
```

紧接着是核心数据结构。注意 `EnabledControllers` 上方那段注释，它**声明了设计意图**（`*`、`foo`、`-foo` 的含义，以及「同名第一项生效」），我们在 4.3 会拿它和实际行为对照：

[pkg/features/features.go:35-47](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/features/features.go#L35-L47) 定义开关映射 `EnabledControllers` 和合法名字清单 `ValidControllers`：

```go
var (
    // EnabledControllers is the map of controllers to enable or disable
    // '*' means "all enabled by default controllers"
    // 'foo' means "enable 'foo'"
    // '-foo' means "disable 'foo'"
    // first item for a particular name wins
    EnabledControllers = make(map[string]bool)

    ValidControllers = []string{
        PodAutoscalerController, DistributedInferenceController,
        ModelAdapterController, ModelRouteController, KVCacheController,
        ModelClaimController, StormServiceController,
    }
)
```

要点：

- `EnabledControllers` 是包级变量，初始为空 map。**空 map 意味着启动后必须先调用 `InitControllers` 才有数据**，否则 `IsControllerEnabled` 对任何名字都返回 `false`。
- `ValidControllers` 是一张「合法名单」，`ValidateControllers` 只放行出现在这里的名字，拦截拼写错误（见 4.2）。

#### 4.1.4 代码实践

**目标**：确认常量、`ValidControllers`、`EnableAllControllers` 三处是「新增控制器必须同步修改」的三件套。

**操作步骤**：

1. 打开 [pkg/features/features.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/features/features.go)。
2. 数一数常量（24–33 行）、`ValidControllers`（43–46 行）、`EnableAllControllers`（112–121 行）里各出现了哪些控制器。
3. 想象要新增一个 `FooController`：列出你必须在**这三处**分别加什么。

**需要观察的现象**：三处列表的成员应当完全一致（都是那 7 个）。如果漏掉某一处，会分别出现：漏常量 → 代码里没法引用；漏 `ValidControllers` → `--controllers` 里写它会被判非法；漏 `EnableAllControllers` → `*` 通配时它不会被打开。

**预期结果**：三处均为 7 个控制器，一一对应。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `EnabledControllers` 必须是 `map` 而不是 `[]string`？
**答案**：因为需要按名字**快速查询**某控制器是否启用，还要能表达「开 / 关」两种状态。map 的键查询是 O(1)，且 `bool` 值天然表达开关；切片查询是 O(n) 且不易表达「显式关闭」。

**练习 2**：`ModelRouteController` 对应的字符串值是什么？
**答案**：`"model-route-controller"`。

---

### 4.2 解析与校验：`InitControllers` 与 `ValidateControllers`

#### 4.2.1 概念说明

命令行拿到的是**一整个字符串**（比如 `*,-kv-cache-controller`），需要两步：

1. **校验（`ValidateControllers`）**：把字符串按逗号拆开，逐项检查每个名字是否合法。发现非法名字就返回错误，**让进程直接退出**——宁可启动失败，也不要带着拼写错误悄悄跑起来。
2. **解析（`InitControllers`）**：校验通过后，再走一遍，把每一项翻译成对 `EnabledControllers` map 的写入。

两步都从同一个字符串出发，`ValidateControllers` 先行，`InitControllers` 后行。

#### 4.2.2 核心流程

```
--controllers="*,-kv-cache-controller,foo-bad"
        │
        ▼
strings.Split(",") → ["*", "-kv-cache-controller", "foo-bad"]
        │
        ├─ ValidateControllers ─→ isValidController("foo-bad")?
        │                         false → return error → os.Exit(1)   ★ 启动失败
        │
        └─（校验通过才会走到）InitControllers
              for 每个项:
                  "*"  → EnableAllControllers()        # 全部置 true
                  "-X" → map[X] = false                # 关闭 X
                  "X"  → map[X] = true                 # 开启 X
```

把 map 的覆盖语义写成公式，对于按键 `k`、按字符串中出现的顺序 `t_1, t_2, …, t_n`：

\[
\text{EnabledControllers}[k] \;=\; \text{由最后一个「影响到 } k\text{ 的」 } t_i \text{ 决定}
\]

也就是说，**同一个名字被多次指定时，最后一次写生效**（last-write-wins）。这是 4.3 节陷阱的根源。

#### 4.2.3 源码精读

[pkg/features/features.go:50-69](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/features/features.go#L50-L69) 是校验函数。注意它对 `*` 直接放行（`continue`），对带 `-` 前缀的项先剥掉前缀再查合法性：

```go
func ValidateControllers(controllerList string) error {
    controllers := strings.Split(controllerList, ",")
    for _, controller := range controllers {
        trimmed := strings.TrimSpace(controller)
        if trimmed == "*" {
            continue   // 通配符恒合法
        }
        controllerName := trimmed
        if strings.HasPrefix(trimmed, "-") {
            controllerName = trimmed[1:] // 去掉 '-' 再校验
        }
        if !isValidController(controllerName) {
            return fmt.Errorf("invalid controller specified: %s", controllerName)
        }
    }
    return nil
}
```

[pkg/features/features.go:72-79](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/features/features.go#L72-L79) 是合法名单的线性查找（控制器数量很少，O(n) 完全够用）：

```go
func isValidController(name string) bool {
    for _, valid := range ValidControllers {
        if name == valid {
            return true
        }
    }
    return false
}
```

[pkg/features/features.go:82-100](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/features/features.go#L82-L100) 是解析函数，三条分支对应三种语法：

```go
func InitControllers(controllerList string) {
    controllers := strings.Split(controllerList, ",")
    for _, controller := range controllers {
        trimmed := strings.TrimSpace(controller)
        if trimmed == "*" {
            EnableAllControllers()       // 全开
            continue
        }
        if strings.HasPrefix(trimmed, "-") {
            EnabledControllers[trimmed[1:]] = false  // 关闭
        } else {
            EnabledControllers[trimmed] = true       // 开启
        }
    }
}
```

最后是调用方。启动入口在 `flag.Parse()` 之后、构建 Manager 之前，先校验后初始化：

[cmd/controllers/main.go:170-175](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/controllers/main.go#L170-L175) 串起校验与初始化；校验失败直接 `os.Exit(1)`：

```go
if err := features.ValidateControllers(controllers); err != nil {
    setupLog.Error(err, "unable to validate the controllers, ...")
    os.Exit(1)
}
features.InitControllers(controllers)
```

#### 4.2.4 代码实践

**目标**：学会预判哪些 `--controllers` 字符串会被 `ValidateControllers` 拒绝。

**操作步骤**：对下面每个字符串，按 4.2.3 的源码逐项走一遍 `ValidateControllers`，判断「能通过」还是「返回 error」。

1. `*`
2. `pod-autoscaler-controller`
3. `pod-autoscaler,model-claim-controller`（注意第一个少写 `-controller`）
4. `*,-kv-cache-controller`
5. `*, -kv-cache-controller`（带空格）

**需要观察的现象**：函数里有 `strings.TrimSpace`，所以空格被容忍；但名字必须**完全等于** `ValidControllers` 中的某个值，否则报错。

**预期结果**：① 通过；② 通过；③ **失败**（`pod-autoscaler` 不在名单，正确名是 `pod-autoscaler-controller`）；④ 通过；⑤ 通过（`TrimSpace` 去掉了空格，再剥 `-` 得 `kv-cache-controller`，合法）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ValidateControllers` 对 `*` 要 `continue` 而不是去查 `isValidController("*")`？
**答案**：`*` 是通配符不是控制器名，不在 `ValidControllers` 里。若不特殊放行，会被误判为非法。

**练习 2**：`-foo` 这种带前缀的项，校验时查的是 `foo` 还是 `-foo`？
**答案**：查 `foo`。代码用 `trimmed[1:]` 先剥掉 `-`，再交给 `isValidController`。

---

### 4.3 启用全部与排除语法：`*` 通配与 `-` 前缀的优先级陷阱

#### 4.3.1 概念说明

这是本讲最实用、也最容易踩坑的部分。三种语法：

| 写法 | 在 `InitControllers` 中的动作 |
| --- | --- |
| `*` | 调 `EnableAllControllers()`，把全部 7 个控制器一次性置 `true` |
| `foo` | `EnabledControllers["foo"] = true` |
| `-foo` | `EnabledControllers["foo"] = false` |

关键在于：这些动作是**按字符串里从左到右的顺序依次执行**的，而每次执行都是对 map 的写入（覆盖）。所以「谁后写谁说了算」。

#### 4.3.2 核心流程

来看 `EnableAllControllers` 本身——它就是挨个把 7 个键写 `true`：

[pkg/features/features.go:112-121](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/features/features.go#L112-L121) 全开函数，对每个已知控制器写 `true`：

```go
func EnableAllControllers() {
    EnabledControllers[PodAutoscalerController] = true
    EnabledControllers[ModelAdapterController] = true
    EnabledControllers[DistributedInferenceController] = true
    EnabledControllers[ModelRouteController] = true
    EnabledControllers[KVCacheController] = true
    EnabledControllers[ModelClaimController] = true
    EnabledControllers[StormServiceController] = true
}
```

于是产生两个必须记住的**顺序陷阱**。

**陷阱 A：排除项必须写在 `*` 之后。**

| `--controllers` | 执行序列 | kv-cache 最终状态 |
| --- | --- | --- |
| `*,-kv-cache-controller` | 全开 → 关闭 kv-cache | **false（已禁用）** ✅ 这是你想要的 |
| `-kv-cache-controller,*` | 关闭 kv-cache → 全开（又把 kv-cache 写回 true） | **true（又被启用了）** ❌ 排除失效 |

**陷阱 B：注释与实现的细微出入。**

`EnabledControllers` 的注释（[第 40 行](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/features/features.go#L40)）写着 `// first item for a particular name wins`（同名第一项生效）。但上面的 map 实现是「最后一次写生效」。所以对于**同一个名字出现两次且极性相反**的输入，例如 `pod-autoscaler-controller,-pod-autoscaler-controller`：

- 注释声明的语义：第一项（`true`）生效 → 启用；
- map 实际行为：第二项（`false`）覆盖 → **禁用**。

对于不冲突的常规输入（每个名字最多出现一次），两者没有区别；这个出入只在「同名重复指定」的边界情况下才显现。这是一个很好的源码阅读练习点：**注释描述意图，代码描述事实，二者偶尔会不一致，以代码为准**。

> 待本地验证：如果你想确认陷阱 B，可以写一个 5 行的 Go 测试调用 `InitControllers("pod-autoscaler-controller,-pod-autoscaler-controller")` 后打印 `IsControllerEnabled(PodAutoscalerController)`，预期看到 `false`（与注释的 first-wins 相反）。这是示例代码，未在本讲义中实际运行。

#### 4.3.3 源码精读

`InitControllers` 的循环已在 4.2.3 引用，这里把三条分支的等价伪代码再列清楚：

```
对 trimmed 中每一项（按出现顺序）:
    若是 "*"      → EnableAllControllers()          # 批量写 true
    否则若以 "-"  → EnabledControllers[剥前缀] = false  # 单点写 false
    否则           → EnabledControllers[原名]   = true   # 单点写 true
```

由于 `EnableAllControllers` 和单点写都是对同一个 map 的赋值，**后执行的覆盖先执行的**，这就是 4.3.2 两个陷阱的根因。

#### 4.3.4 代码实践

**目标**：亲手推算两种「排除」写法的最终 map 状态，体会顺序敏感性。

**操作步骤**：

1. 取输入 A = `*,-kv-cache-controller`，按顺序写下每一步对 `EnabledControllers` 的写入，最后统计哪些控制器为 `true`、哪个为 `false`。
2. 取输入 B = `-kv-cache-controller,*`，重复上面的推算。
3. 对比两者 `kv-cache-controller` 键的最终值。

**需要观察的现象**：输入 A 能成功排除 KV Cache；输入 B 因为 `*` 在后，把刚才的 `false` 又覆盖成 `true`，排除静默失效。

**预期结果**：
- A：除 `kv-cache-controller = false` 外，其余 6 个均为 `true`。
- B：全部 7 个均为 `true`（排除失效）。
- 待本地验证：可用下方示例代码确认。

```go
// 示例代码（仅供参考，未实际运行）
features.InitControllers("-kv-cache-controller,*")
fmt.Println(features.IsControllerEnabled(features.KVCacheController)) // 预期 true（排除失效）
features.InitControllers("*,-kv-cache-controller")
fmt.Println(features.IsControllerEnabled(features.KVCacheController)) // 预期 false（排除成功）
```

#### 4.3.5 小练习与答案

**练习 1**：想「启用全部、但关掉 KV Cache 和 StormService」，正确的 `--controllers` 怎么写？
**答案**：`*,-kv-cache-controller,-stormservice-controller`。排除项必须全部放在 `*` 之后。

**练习 2**：`--controllers=pod-autoscaler-controller`（只有一项、不带 `*`）时，`model-claim-controller` 是否启用？
**答案**：不启用。这是白名单模式，map 里只有 `pod-autoscaler-controller=true`；`model-claim-controller` 键不存在，`IsControllerEnabled` 对不存在的键返回 `false`。

---

### 4.4 查询与消费：`IsControllerEnabled` 如何驱动 `Initialize`

#### 4.4.1 概念说明

开关填好之后，得有人去「读」它。查询函数是 `IsControllerEnabled`，它的规则非常保守：**map 里没有这个键，就当作「关闭」**。这是「安全优先」的设计——避免某控制器在没被显式启用时意外跑起来。

真正的消费发生在 [pkg/controller/controller.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/controller.go) 的 `Initialize` 函数：它对每个能力域问一句「你开了吗」，开了就把对应控制器的 `Add`（注册函数）追加到 `controllerAddFuncs` 切片，留给第二阶段 `SetupWithManager` 装配（见上一讲 u2-l1）。

#### 4.4.2 核心流程

```
IsControllerEnabled(name)
   │
   ├─ map 里有 name？ ── 否 ──→ return false   ★ 默认禁用
   │
   └─ 是 ──→ return map[name]
                  │
                  ▼
        Initialize 里逐个判断：
        if IsControllerEnabled(PodAutoscalerController) {
            controllerAddFuncs = append(..., podautoscaler.Add)
        }
        ... 对每个能力域重复 ...
```

#### 4.4.3 源码精读

[pkg/features/features.go:103-109](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/features/features.go#L103-L109) 查询函数，注意「不存在即禁用」的注释：

```go
func IsControllerEnabled(name string) bool {
    enabled, exists := EnabledControllers[name]
    if !exists {
        return false // If not specified, consider it disabled to be safe.
    }
    return enabled
}
```

[pkg/controller/controller.go:51-100](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/controller.go#L51-L100) 是 `Initialize` 主体，对每个能力域用 `IsControllerEnabled` 守卫，决定是否收集 `Add` 函数。节选前后两段：

```go
func Initialize(mgr manager.Manager) error {
    if features.IsControllerEnabled(features.PodAutoscalerController) {
        controllerAddFuncs = append(controllerAddFuncs, podautoscaler.Add)
    }
    if features.IsControllerEnabled(features.ModelAdapterController) {
        controllerAddFuncs = append(controllerAddFuncs, modeladapter.Add)
    }
    // ... ModelRoute / ModelClaim 同理 ...
    if features.IsControllerEnabled(features.KVCacheController) {
        controllerAddFuncs = append(controllerAddFuncs, kvcache.Add)
    }
    if features.IsControllerEnabled(features.StormServiceController) {
        controllerAddFuncs = append(controllerAddFuncs, roleset.Add)
        controllerAddFuncs = append(controllerAddFuncs, stormservice.Add)
        controllerAddFuncs = append(controllerAddFuncs, podset.Add)
    }
    return nil
}
```

这里有两个值得注意的细节：

1. **一个开关可挂多个控制器。** StormService 开关一旦启用，会同时收集 `roleset.Add`、`stormservice.Add`、`podset.Add` 三个注册函数（这正是 4.1 里「内部控制器复用顶层名字」的体现）。
2. **Feature Gate 之上还有运行时降级。** 分布式推理开关即使被启用，`Initialize` 还会再去查 KubeRay 的 CRD 是否存在，缺失则优雅跳过。见 [pkg/controller/controller.go:68-87](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/controller.go#L68-L87)：先 `IsControllerEnabled(DistributedInferenceController)`，再 `checkCRDExists(..., "rayclusters.ray.io")`，CRD 不在就 `klog.InfoS` 记一条日志、不追加 `Add` 函数。也就是说「Feature Gate 说开」是必要条件，但 CRD 缺失时会被运行时拦下——**开关只表达意图，不保证一定装载**。

另外，`IsControllerEnabled` 的查询不止用于 `Initialize`。启动早期 [cmd/controllers/main.go:82-87](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/controllers/main.go#L82-L87) 还用它决定是否向 `runtime.Scheme` 注册对应的 CRD 类型（未注册的类型无法被 controller-runtime 序列化）。所以同一个开关，在「注册 Scheme」和「收集 Add 函数」两处都被读，二者必须一致。

#### 4.4.4 代码实践

**目标**：端到端追踪一次——从 `--controllers` 字符串到 `Initialize` 收集了哪些 `Add`。

**操作步骤**：

1. 假设启动参数为 `--controllers=*,-kv-cache-controller`（也是本讲综合实践的输入）。
2. 走 `InitControllers`：写出 `EnabledControllers` 的最终状态（哪些 `true`、哪个 `false`）。
3. 打开 [pkg/controller/controller.go:51-100](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/controller.go#L51-L100)，对每个 `if IsControllerEnabled(...)` 判断会不会进入、进入后 `append` 了哪些 `Add`。
4. 对照 [test/integration/controller/suit_test.go:158-159](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/test/integration/controller/suit_test.go#L158-L159)，看集成测试里用 `InitControllers("*")` 后调用 `Initialize` 与 `SetupWithManager` 的完整三连，作为「全开」场景的参照。

**需要观察的现象**：`kv-cache-controller=false` 时，`Initialize` 里 `IsControllerEnabled(KVCacheController)` 返回 `false`，`kvcache.Add` 不会被追加；其余六个开关为 `true`，对应 `Add` 全部被收集。

**预期结果**：`controllerAddFuncs` 最终包含 `podautoscaler.Add`、`modeladapter.Add`、`modelrouter.Add`、`modelclaim.Add`、（若 KubeRay CRD 存在）`rayclusterreplicaset.Add` + `rayclusterfleet.Add`、以及 StormService 三件套（`roleset/stormservice/podset.Add`）；**唯独不含 `kvcache.Add`**。待本地验证：可在一个临时测试里调用 `InitControllers("*,-kv-cache-controller")` 后断言 `IsControllerEnabled(KVCacheController) == false`。

#### 4.4.5 小练习与答案

**练习 1**：`IsControllerEnabled` 对一个从未在 `--controllers` 里出现的控制器返回什么？为什么这样设计？
**答案**：返回 `false`。注释写明「to be safe」——未显式启用即视为关闭，防止控制器在用户不知情时意外运行。

**练习 2**：分布式推理开关已启用、但集群里没装 KubeRay，会发生什么？
**答案**：`Initialize` 里 `checkCRDExists("rayclusters.ray.io")` 返回不存在，打印一条 `klog.InfoS` 日志说明跳过，且不追加 `rayclusterreplicaset.Add` / `rayclusterfleet.Add`。进程继续正常启动，只是这两个控制器不工作。这是「Feature Gate 意图 + 运行时降级」双层守卫。

**练习 3**：为什么 `cmd/controllers/main.go` 在注册 Scheme 和 `Initialize` 两处都要读 `IsControllerEnabled`？
**答案**：注册 Scheme 决定「这个 CRD 类型能不能被序列化 / watch」，`Initialize` 决定「对应控制器装不装载」。两处必须一致：若注册了 Scheme 却不装控制器，watch 会空转；若装了控制器却没注册 Scheme，controller-runtime 会因找不到类型而报错。

---

## 5. 综合实践

把本讲内容串起来，完成规格里要求的核心任务：**写出一个 `--controllers` 参数示例，并追踪它如何一路作用到 `Initialize`。**

**任务**：设计一个参数，达到「启用 PodAutoscaler 与 ModelClaim、禁用 KVCache」的效果，并写出完整的推算。

**第 1 步——构造参数。** 有两种写法，请说明哪种更符合需求、为什么：

- 写法一（黑名单）：`*,-kv-cache-controller` —— 但这会**同时**启用 PodAutoscaler 和 ModelClaim 之外的所有其它控制器（ModelAdapter、ModelRoute、DistributedInference、StormService），并非「只」启用 PodAutoscaler 与 ModelClaim。
- 写法二（白名单）：`pod-autoscaler-controller,model-claim-controller` —— 精确地**只**启用这两个，KVCache 因键不存在而默认禁用，正好满足需求。

结论：要「只」启用这两个，应选**写法二**；若意图是「全开但去掉 KVCache」，才用写法一。需求文字「启用 PodAutoscaler 与 ModelClaim、禁用 KVCache」更接近白名单语义，故推荐 `--controllers=pod-autoscaler-controller,model-claim-controller`。

**第 2 步——推算 `InitControllers` 后的 map。**

| 控制器 | 键是否存在 | 值 |
| --- | --- | --- |
| `pod-autoscaler-controller` | 是 | `true` |
| `model-claim-controller` | 是 | `true` |
| `kv-cache-controller` | **否** | （`IsControllerEnabled` 返回 `false`） |
| 其余四个 | 否 | （返回 `false`） |

**第 3 步——追踪 `Initialize`。** 对照 [pkg/controller/controller.go:51-100](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/controller.go#L51-L100)：

- `IsControllerEnabled(PodAutoscalerController)` → `true` → 追加 `podautoscaler.Add`。
- `IsControllerEnabled(ModelAdapterController)` → `false` → 跳过。
- `IsControllerEnabled(ModelRouteController)` → `false` → 跳过。
- `IsControllerEnabled(ModelClaimController)` → `true` → 追加 `modelclaim.Add`。
- `IsControllerEnabled(DistributedInferenceController)` → `false` → 跳过（也不会去查 KubeRay CRD）。
- `IsControllerEnabled(KVCacheController)` → `false` → 跳过。
- `IsControllerEnabled(StormServiceController)` → `false` → 跳过。

最终 `controllerAddFuncs = [podautoscaler.Add, modelclaim.Add]`，第二阶段 `SetupWithManager` 只会装配这两个控制器。同时 [cmd/controllers/main.go:82-87](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/controllers/main.go#L82-L87) 也只注册这两个开关对应的 Scheme。

**第 4 步（可选，待本地验证）**：写一个最小的 Go 测试复现上述断言（示例代码，未运行）：

```go
// 示例代码
features.InitControllers("pod-autoscaler-controller,model-claim-controller")
Expect(features.IsControllerEnabled(features.PodAutoscalerController)).To(BeTrue())
Expect(features.IsControllerEnabled(features.ModelClaimController)).To(BeTrue())
Expect(features.IsControllerEnabled(features.KVCacheController)).To(BeFalse())
```

## 6. 本讲小结

- `--controllers` 是一个逗号分隔的字符串，默认值 `*`（全开），在 `main.go` 中先经 `ValidateControllers` 校验、再经 `InitControllers` 填充 `EnabledControllers map[string]bool`。
- 三种语法：`*` 触发 `EnableAllControllers()`（全置 `true`）；裸名字置 `true`；`-名字` 置 `false`。
- 白名单模式（只列名字）与黑名单模式（`*,-排除`）语义不同：前者其余默认禁用，后者其余默认启用。
- **顺序敏感**：因为 map 写入是覆盖，排除项必须放在 `*` 之后；否则会被随后的 `*` 重新打开，排除静默失效。
- 注释声明的「first item wins」与 map 实际的「last-write-wins」在「同名重复且极性相反」时不一致，**以代码为准**。
- `IsControllerEnabled` 对不存在的键返回 `false`（安全优先）；它既驱动 `Initialize` 收集 `Add` 函数，也驱动 `main.go` 条件注册 Scheme。
- Feature Gate 只表达意图：分布式推理即使开关打开，`Initialize` 还会运行时检查 KubeRay CRD，缺失则优雅跳过。

## 7. 下一步学习建议

本讲讲清了「开关怎么存、怎么解析、怎么被读」。接下来建议：

1. **往下追消费侧**：进入 [u2-l3 自定义资源 (CRD) 数据模型设计](u2-l3-crd-data-models.md)，看这些被开关控制的控制器各自管理什么样的 CRD（PodAutoscaler、ModelAdapter、ModelClaim 的 Spec/Status）。
2. **横向对照**：阅读 [test/integration/controller/suit_test.go:158-161](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/test/integration/controller/suit_test.go#L158-L161)，体会测试里 `InitControllers("*")` → `Initialize` → `SetupWithManager` 的标准三连调用顺序。
3. **动手扩展（读源码即可）**：如果想新增一个控制器，复习上一讲 u2-l1 末尾给出的「三处同步」清单（常量 / `ValidControllers` / `EnableAllControllers` + Scheme 注册 + `Initialize` 分支），把本讲的开关机制和新控制器的接入完整串起来。
