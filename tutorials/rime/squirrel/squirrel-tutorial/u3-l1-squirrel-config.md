# SquirrelConfig 类型化配置门面

## 1. 本讲目标

本讲是「配置与主题」单元的第一讲。读完本讲，你应当能够：

- 说清楚 SquirrelConfig 是什么、为什么需要它：它是 librime C 配置 API 之上的一层 Swift「门面（facade）」，把裸 C 函数包装成类型安全的 `getBool / getDouble / getString / getColor` 读取接口。
- 理解 **base config（基础配置）与 schema config（方案配置）的回退关系**：当某个方案没有定义某项配置时，如何自动回落到 `squirrel.yaml` 的全局默认值。
- 掌握 **类型化读取 + 缓存机制**：每个 `getXxx` 都遵循「先查缓存 → 再查 librime → 命中后写回缓存」的同构模板。
- 理解 **颜色字符串的解析规则**：Rime 使用 `0xAABBGGRR`（而非直觉上的 `0xAARRGGBB`）字节序，并支持 `displayP3` 与 `sRGB` 两种色空间。
- 理解 `getAppOptions` 如何读取 `app_options` 应用级选项，并把这些布尔开关喂给引擎。

本讲只深读一个文件：`sources/SquirrelConfig.swift`（148 行）。它是后续 SquirrelTheme（u3-l3）与候选面板（第四单元）读取所有外观参数的统一入口。

## 2. 前置知识

在进入源码前，先用三段话把背景铺平。

** librime 的配置是什么形态？**
librime 引擎用 YAML 文件承载配置。两套最常用的配置是：

- `squirrel.yaml`：Squirrel 前端自己的全局配置（配色、布局、字体、应用级选项）。对应 librime 的「具名配置（named config）」，用 `config_open("squirrel", ...)` 打开。
- 某个输入方案（schema，如 `luna_pinyin`）自带的配置。对应 librime 的「方案配置（schema config）」，用 `schema_open(schemaID, ...)` 打开。

这两类配置在磁盘上都是 YAML，但 librime 用不同的 C 函数打开它们。SquirrelConfig 把这两种打开方式统一成一个 Swift 类，并通过 `baseConfig` 字段实现「方案配置缺失时回退到全局配置」。

**什么是「门面（facade）」模式？**
librime 的 C API 是一组零散的函数：`config_get_bool`、`config_get_double`、`config_get_cstring`、`config_begin_map`、`config_next`、`config_end` …… 每个都要传入 `RimeConfig*` 指针、路径字符串、输出变量地址，还要处理返回值表示的「成功/失败」。SquirrelConfig 在这堆 C 函数之上套了一层 Swift 外壳，对外只暴露 `getBool("style/corner_radius")` 这样「给路径、拿值」的简洁接口。这就是门面模式——用一个干净的对象遮住底层杂乱的子系统。

**为什么需要回退（fallback）？**
Squirrel 允许「全局样式 + 方案特化样式」两层配置：`squirrel.yaml` 的 `style` 节定义所有方案共享的默认外观；某个方案（比如专门为写代码设计的方案）可以在自己的 schema 里只覆盖其中几项（比如只改 `corner_radius`），其余项继承全局默认。SquirrelConfig 的 `baseConfig` 字段就是为这种「子配置缺项时找父配置要」的链式查询而设计。

如果你还没读过 u2-l1（应用委托与全局状态）和 u2-l2（全局 librime 初始化），建议先看。本讲会引用其中关于 `rimeAPI`、`RimeConfig` 句柄的概念。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的地方 |
| --- | --- | --- |
| `sources/SquirrelConfig.swift` | **本讲主角**。librime 配置 API 的 Swift 门面，提供回退、缓存、颜色解析、应用选项读取。 | 全部精读 |
| `sources/SquirrelApplicationDelegate.swift` | 持有全局 `config: SquirrelConfig?`，在 `loadSettings()` 打开 base config，在 `loadSettings(for:)` 打开方案配置。 | 看 SquirrelConfig 如何被「使用方」构造与回退 |
| `sources/SquirrelTheme.swift` | 主题加载器。大量调用 `config.getColor / getString / getDouble`，并定义 `RimeColorSpace` 枚举。 | 看消费方如何用类型化读取接口；颜色解析的色空间来源 |
| `sources/SquirrelInputController.swift` | 调用 `config.getAppOptions(currentApp)` 读取应用级选项并喂给引擎。 | 看 `getAppOptions` 的真实调用点 |
| `data/squirrel.yaml` | 全局配置文件。提供颜色、布局、`app_options` 的真实样例。 | 用真实配置项佐证读取行为 |

## 4. 核心概念与源码讲解

### 4.1 配置的打开、关闭与 base config 回退

#### 4.1.1 概念说明

SquirrelConfig 用一个 Swift 类同时承载两种 librime 配置：

- **base config（基础配置）**：用 `openBaseConfig()` 打开，对应 `squirrel.yaml`。它是「父配置」，提供所有默认值。
- **schema config（方案配置）**：用 `open(schemaID:baseConfig:)` 打开，对应某个输入方案。它是「子配置」，可以只覆盖其中一部分键。

两者的关系是「子优先、父兜底」：读一个键时，先在当前（子）配置里找；找不到，就去 `baseConfig`（父）里找。这种「责任链」式的查询让方案只需声明自己想改的项。

#### 4.1.2 核心流程

打开与回退的伪代码如下：

```
openBaseConfig():
    close()                          # 先关掉可能残留的旧配置
    isOpen = config_open("squirrel") # 打开 squirrel.yaml
    # baseConfig 保持为 nil（自己就是根，没有更上层）

open(schemaID, baseConfig):
    close()
    isOpen = schema_open(schemaID)   # 打开方案配置
    if isOpen:
        self.baseConfig = baseConfig # 记住父配置，供回退使用

getXxx(option):
    if 当前配置命中 option: return 当前值
    else: return baseConfig?.getXxx(option)   # 递归向父配置要
```

注意 `getXxx` 里的回退是**递归**的：`baseConfig?.getXxx(option)` 会进入父 SquirrelConfig 实例的 `getXxx`，再走一遍「查缓存 → 查 librime → 回退」的流程。由于 base config 自身的 `baseConfig` 是 `nil`，递归到这一层就终止。

#### 4.1.3 源码精读

类的核心字段与两个 open 方法：

[sources/SquirrelConfig.swift:L10-L31](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelConfig.swift#L10-L31) —— `rimeAPI` 是从 `rime_get_api_stdbool()` 取出的 C 函数表（librime 的入口，u2-l2 讲过）；`config: RimeConfig` 是 librime 的不透明句柄结构，由 open 函数在内部填充；`baseConfig: SquirrelConfig?` 就是回退指针。`openBaseConfig` 调 `config_open("squirrel", ...)`，`open` 调 `schema_open` 并在成功时保存父配置。

关闭与析构，保证 C 句柄不泄漏：

[sources/SquirrelConfig.swift:L33-L43](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelConfig.swift#L33-L43) —— `close()` 在已打开时调 `config_close` 释放 librime 句柄、清空 `baseConfig`、置 `isOpen = false`；`deinit` 也调 `close()`，确保对象销毁时一定释放。

回退的真正发生地（以 `getBool` 为例，其余 getXxx 同构）：

[sources/SquirrelConfig.swift:L56-L66](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelConfig.swift#L56-L66) —— 第 65 行 `return baseConfig?.getBool(option)` 就是回退：当前配置没命中时，把请求转交给父配置。

再看使用方如何构造这条父子链。AppDelegate 的两个 `loadSettings`：

[sources/SquirrelApplicationDelegate.swift:L169-L199](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L169-L199) —— 这里有两层回退值得区分：

1. **整配置级回退**（第 190 行）：只有当方案配置 `schema.has(section: "style")` 为真，才用方案配置加载主题；否则直接用 base config 加载。这是「方案完全没声明 style 节就整体退回全局」。
2. **逐键回退**（SquirrelConfig 内部）：即使方案有 `style` 节，其中没覆盖的键也会通过 `baseConfig?.getXxx` 退回全局默认。

> 提示：`has(section:)` 的实现在 [sources/SquirrelConfig.swift:L45-L54](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelConfig.swift#L45-L54)，它用 `config_begin_map` 探测某节是否存在，探测完立刻 `config_end` 释放迭代器。

#### 4.1.4 代码实践

**实践目标**：用真实配置验证「逐键回退」确实发生。

**操作步骤**：

1. 打开 `data/squirrel.yaml`，找到 `style` 节下的 `corner_radius: 7`（[data/squirrel.yaml:L48](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L48)）。这是 base config 提供的默认值。
2. 假设某个方案 `my_schema` 在自己的 schema 文件里写了一个 `style` 节，但**只**覆盖了 `font_point`，没写 `corner_radius`。
3. 跟踪调用：`panel.load(config: schema, ...)` → `SquirrelTheme.load` 中 `cornerRadius ?= config.getDouble("style/corner_radius")`（[sources/SquirrelTheme.swift:L211](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L211)）→ `schema.getDouble("style/corner_radius")`。
4. 由于方案配置没有 `style/corner_radius`，`getDouble` 走到第 77 行 `return baseConfig?.getDouble(option)`，最终从 base config 拿到 `7`。

**需要观察的现象**：方案的 `style` 节即使只写了一项，整个外观也不会「塌掉」——缺的项都自动得到全局默认值。

**预期结果**：面板圆角仍是 7。若把 `squirrel.yaml` 的 `corner_radius` 改成 12（在 `~/Library/Rime/squirrel.yaml` 用户配置里改，不要改仓库源文件），所有未覆盖该键的方案都会跟着变 12。

**待本地验证**：在 macOS 上实际运行 Squirrel、修改用户配置目录下的 `squirrel.yaml` 后重新部署，观察面板圆角变化。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `open(schemaID:baseConfig:)` 里的 `baseConfig` 参数传成 `nil`，会有什么后果？

**答案**：方案配置里没覆盖的键，`getXxx` 回退到 `baseConfig?.getXxx(option)` 时因 `baseConfig` 为 `nil` 直接返回 `nil`。主题加载器读到 `nil` 后会落到各自的内建默认色（如 `backgroundColor` 默认 `windowBackgroundColor`），外观将不再继承 `squirrel.yaml` 的全局设置。

**练习 2**：`close()` 为什么要先判断 `if isOpen`？不判断直接调 `config_close` 会怎样？

**答案**：`config_close` 期望传入一个已打开的有效句柄。若 `isOpen == false`（从未打开或已关闭），`config` 句柄是未初始化/已释放的，对其调用 C 函数属于未定义行为，可能崩溃。`isOpen` 标志就是这道守卫。`deinit` 复用 `close()` 同样依赖这个判断，保证对象无论是否成功打开都能安全析构。

---

### 4.2 类型化读取与 cache 缓存

#### 4.2.1 概念说明

librime 的 C 读取函数返回的是「成功/失败」布尔，值要写进调用方传入的出参变量，且字符串是 C 风格 `UnsafePointer<CChar>`。直接在 Swift 业务代码里用很啰嗦、易错。SquirrelConfig 提供四个类型化方法，把这套机制藏起来：

| 方法 | 返回类型 | 底层 librime 函数 |
| --- | --- | --- |
| `getBool(_:)` | `Bool?` | `config_get_bool` |
| `getDouble(_:)` | `CGFloat?` | `config_get_double` |
| `getString(_:)` | `String?` | `config_get_cstring` |
| `getColor(_:inSpace:)` | `NSColor?` | 复用 `getString` + 自行解析 |

返回值统一用 **Optional**：`nil` 表示「这个配置项不存在」，非 `nil` 表示读到值。这让调用方可以用 `??` 给默认值、用 `?=`（项目自定义运算符，仅非 nil 才赋值）做条件赋值。

`cache: [String: Any]` 是一个进程内字典，**键是配置路径字符串**（如 `"style/font_point"`），**值是已读到的结果**。它的作用是避免对同一个键反复调用 C 函数——主题加载时一次 `load` 会读几十个颜色和参数，缓存把重复查询压到一次。

#### 4.2.2 核心流程

四个 `getXxx` 共用同一个三段式模板：

```
getXxx(option):
    1. 查 cache：命中且类型匹配 → 直接返回（最快路径）
    2. 调 librime C 函数读当前 config：
         成功 → 写入 cache → 返回值
         失败 → 进入第 3 步
    3. 回退：return baseConfig?.getXxx(option)
```

关键细节：

- **缓存键不区分父子**：缓存键就是 `option` 字符串本身，不带任何前缀。当前配置命中的值缓存进 `self.cache`；回退到父配置命中的值，缓存进 **父配置的** `cache`（因为递归调用是在 `baseConfig` 实例上执行的）。
- **生命周期**：`cache` 是 SquirrelConfig 实例的存储属性，实例销毁即失效。实例何时销毁？base config 在每次 `loadSettings()` 重新 `config = SquirrelConfig()` 时被替换（旧实例析构，缓存清空）；schema config 在每次 `loadSettings(for:)` 末尾 `schema.close()` 后随作用域结束析构。所以缓存的生命周期 ≈ 「一次样式重载」。

#### 4.2.3 源码精读

三段式读取的三个范本：

[sources/SquirrelConfig.swift:L56-L89](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelConfig.swift#L56-L89) —— `getBool` / `getDouble` / `getString` 结构完全一致：先 `cachedValue(of:forKey:)` 查缓存，再调底层 C 函数，成功就 `cache[option] = value` 后返回，否则 `return baseConfig?.getXxx(option)`。

`getString` 有一个值得注意的细节（第 85–86 行）：

```swift
cache[option] = String(cString: value)
return String(cString: value)
```

`value` 是 librime 返回的 C 字符串指针，指向 librime 内部缓冲区（可能在下一次配置操作后被释放或覆盖）。`String(cString: value)` 把它**拷贝**成 Swift String，这样即便原 C 缓冲区后续失效，缓存的 Swift String 仍然有效。这是 C 桥接里「拷贝所有权」的典型处理（详见 u5-l4 Swift/C 桥接约定）。

缓存查询的小工具：

[sources/SquirrelConfig.swift:L118-L120](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelConfig.swift#L118-L120) —— `cachedValue` 用 `as? T` 做类型转换。因为 `cache` 是 `[String: Any]`，不同方法可能用相同路径读不同类型（理论上），`as? T` 保证只返回类型匹配的缓存值，避免类型串扰。

消费方如何用这套接口——看 SquirrelTheme 的一次密集读取：

[sources/SquirrelTheme.swift:L210-L219](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L210-L219) —— 连续十几个 `?=` 配合 `getDouble` / `getString`。这里 `?=`（自定义运算符）的意思是「仅当右侧非 nil 才赋值」，正好和「方案配置缺项时保留上一层已设的值」语义吻合。若无缓存，这一串会触发十几次 C 调用；有缓存后，重复 `load`（亮、暗各一次，见 u3-l4）只有首次真正读 librime。

#### 4.2.4 代码实践

**实践目标**：确认缓存「命中即返回、不重复调 C 函数」，并理解缓存落在哪个实例上。

**操作步骤**：

1. 在 `getBool` 第 61 行 `if isOpen && rimeAPI.config_get_bool(...)` 内部，假设你临时加一行 `print("query librime for \(option)")`（仅作思考实验，不要真改源码）。
2. 推演一次 `panel.load(config: schema, forDarkMode: false)` 紧接着 `panel.load(config: schema, forDarkMode: true)`（这是 u3-l4 里亮/暗双加载的真实节奏）的输出。
3. 问自己：`style/corner_radius` 这一项，会打印几次？

**需要观察的现象（推演）**：

- 第一次 `load`：`schema.getDouble("style/corner_radius")` 查 `schema.cache`（空）→ 查方案 librime（假设没有）→ 回退 `baseConfig.getDouble` → 查 `baseConfig.cache`（空）→ 查 base librime（命中 7）→ 写入 **baseConfig.cache**。打印 1 次。
- 第二次 `load`（暗色）：`schema.getDouble` 查 `schema.cache`（仍空，因为值没缓存在 schema 这层）→ 查方案 librime（仍没有）→ 回退 `baseConfig.getDouble` → 查 `baseConfig.cache`（**命中 7**）→ 直接返回。打印 0 次。

**预期结果**：同一键在两次 `load` 中总共只触发 1 次 librime 调用，因为 base config 的缓存被两次共享。这正是把缓存放在「共享的 base config 实例」上的收益。

**待本地验证**：实际加日志运行确认（需 macOS 环境）。

#### 4.2.5 小练习与答案

**练习 1**：`getDouble` 返回 `CGFloat?`，但 librime 的 `config_get_double` 写入的是 `Double`。为什么要转成 `CGFloat`？

**答案**：因为消费方（AppKit / Core Graphics 的几何、颜色 API）用的是 `CGFloat`。在门面层就把类型转好，调用方就不用每次 `CGFloat(x)`。这也是「门面」的职责之一：把子系统友好的类型，转成业务侧友好的类型。

**练习 2**：如果同一个配置路径先被 `getString` 读、后被 `getBool` 读，`cachedValue` 会怎么表现？

**答案**：`cache[option]` 存的是第一次读到的 `String`。第二次 `getBool` 调 `cachedValue(of: Bool.self, forKey: option)`，`cache[option] as? Bool` 对一个 `String` 值返回 `nil`，于是跳过缓存继续走 librime。也就是说 `as? T` 的类型守卫避免了「用错类型读到上次的值」这种隐蔽 bug。实际项目中同一路径不会被两种类型读，这个守卫是防御性设计。

---

### 4.3 getColor 与 0xAABBGGRR 颜色解析

#### 4.3.1 概念说明

`getColor` 是四个读取方法里最特殊的一个：它不是 librime 直接提供的类型，而是 Squirrel 自己用「读字符串 + 解析」拼出来的。配置文件里颜色写成十六进制字符串，例如：

```yaml
text_color: 0x606060            # 6 位
back_color: 0xeeeceeee          # 8 位
hilited_candidate_back_color: 0xeefa3a0a   # 8 位
```

**关键陷阱——字节序是 `0xAABBGGRR`，不是直觉上的 `0xAARRGGBB`。** 也就是说，从左到右四个字节依次是：Alpha（透明度）、Blue（蓝）、Green（绿）、Red（红）。这是 Rime 历史遗留的约定，与 Web/CSS 的 `#RRGGBB`、Android 的 `#AARRGGBB` 都相反。SquirrelConfig 的正则就是按这个序解析的。

6 位形式 `0xBBGGRR` 省略了 Alpha，默认完全不透明（`0xFF`）。

#### 4.3.2 核心流程

解析分两步：

```
getColor(option, colorSpace):
    colorStr = getString(option)              # 复用已缓存的字符串读取
    if colorStr 解析成功:
        按 0xAABBGGRR 或 0xBBGGRR 拆字节
        转成 NSColor(colorSpace)
        写入 cache[option]                    # 缓存的是 NSColor
        return NSColor
    else:
        return baseConfig?.getColor(...)      # 回退
```

字节到归一化分量的换算：

\[ \text{component} = \frac{\text{byte}}{255} \in [0, 1] \]

对 8 位串 `0xAABBGGRR`：

| 字节位置 | 含义 | 示例 `0xeefa3a0a` |
| --- | --- | --- |
| 第 1 字节 | Alpha | `0xee` |
| 第 2 字节 | Blue | `0xfa` |
| 第 3 字节 | Green | `0x3a` |
| 第 4 字节 | Red | `0x0a` |

色空间分两种（由配色方案的 `color_space` 字段决定）：

- `display_p3`：广色域，适合现代 Mac 屏幕。
- 默认（未声明或其它值）：`sRGB`。

#### 4.3.3 源码精读

`getColor` 入口——复用 `getString` 再解析：

[sources/SquirrelConfig.swift:L91-L100](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelConfig.swift#L91-L100) —— 注意它先调 `getString(option)`，因此颜色字符串本身已被缓存；解析出的 `NSColor` 再单独缓存进 `cache[option]`（覆盖掉字符串值——但因为 `cachedValue(of: NSColor.self, ...)` 的类型守卫，下次读颜色仍能命中，不会与字符串路径冲突）。

颜色字符串解析的正则（Swift Regex Literals）：

[sources/SquirrelConfig.swift:L122-L132](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelConfig.swift#L122-L132) —— 两条正则：

- 8 位：`/0x([A-Fa-f0-9]{2})([A-Fa-f0-9]{2})([A-Fa-f0-9]{2})([A-Fa-f0-9]{2})/`，捕获组解构为 `(_, alpha, blue, green, red)`（首个 `_` 是 `0x` 前缀）。**注意捕获顺序就是 Alpha、Blue、Green、Red——这就是 `0xAABBGGRR` 的来源。**
- 6 位：`/0x([A-Fa-f0-9]{2})([A-Fa-f0-9]{2})([A-Fa-f0-9]{2})/`，解构为 `(_, blue, green, red)`，Alpha 固定 `255`。
- 都不匹配则返回 `nil`。

字节到 NSColor 的构造：

[sources/SquirrelConfig.swift:L134-L147](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelConfig.swift#L134-L147) —— 按 `colorSpace` 分派到 `NSColor(displayP3Red:green:blue:alpha:)` 或 `NSColor(srgbRed:green:blue:alpha:)`，分量都除以 255 归一化。

色空间枚举与从字符串到枚举的映射：

[sources/SquirrelTheme.swift:L19-L28](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L19-L28) —— `RimeColorSpace.from(name:)` 只认 `"display_p3"`，其余一律按 `sRGB` 处理。真实配置里的声明见 [data/squirrel.yaml:L329](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L329)（`color_space: display_p3`）。

消费方如何批量取色：

[sources/SquirrelTheme.swift:L234-L250](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L234-L250) —— 十几个 `getColor("\(prefix)/xxx_color", inSpace: colorSpace)` 调用，`prefix` 是当前配色方案在 `preset_color_schemes` 下的路径（如 `preset_color_schemes/aqua`）。

#### 4.3.4 代码实践

**实践目标**：亲手验证 `0xAABBGGRR` 字节序，避免日后配错颜色。

**操作步骤**：

1. 取 `data/squirrel.yaml` 里 aqua 配色的 `hilited_candidate_back_color: 0xeefa3a0a`（[data/squirrel.yaml:L88](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L88)）。
2. 按 `0xAABBGGRR` 拆：Alpha=`0xee`、Blue=`0xfa`、Green=`0x3a`、Red=`0x0a`。
3. 心算归一化：红 ≈ 10/255 ≈ 0.04，绿 ≈ 58/255 ≈ 0.23，蓝 ≈ 250/255 ≈ 0.98，透明度 ≈ 238/255 ≈ 0.93。
4. 对照直觉：这个颜色应是「接近不透明的亮蓝色」——因为蓝分量最大。
5. 反证：若误按 `0xAARRGGBB` 解，红=`0xfa`、绿=`0x3a`、蓝=`0x0a`，会得到「亮红色」，与实际渲染不符。

**需要观察的现象**：高亮候选词背景在 Squirrel 里显示为偏蓝的橙色之外的色——实际是亮蓝。这一步能帮你记住「Rime 的颜色字节序是反的」。

**预期结果**：按 `0xAABBGGRR` 解析得到的颜色描述（亮蓝、近不透明）与 macOS 上 Squirrel 实际渲染一致。

**待本地验证**：在 macOS 上选 aqua 配色、输入触发高亮候选，肉眼比对。

#### 4.3.5 小练习与答案

**练习 1**：`back_color: 0xeeeceeee` 是 8 位串。它的 Alpha、Blue、Green、Red 各是多少？接近什么色？

**答案**：Alpha=`0xee`（约 0.93，半透明偏不透明）、Blue=`0xec`、Green=`0xee`、Red=`0xee`。三个色分量几乎相等且都很高，所以是「接近纯白的浅灰、略偏蓝」。这正是 aqua 配色面板背景的观感。

**练习 2**：为什么 `getColor` 先调 `getString` 而不是直接调 librime 的字符串读取函数？

**答案**：为了复用 `getString` 已建立的缓存与回退机制。`getString` 内部已处理「查缓存 → 调 C → 写缓存 → 回退 baseConfig」，`getColor` 直接搭便车，避免重复实现这套逻辑。这也意味着同一颜色键的字符串形式和 NSColor 形式都不会重复查询 librime。

**练习 3**：如果用户在配置里把颜色写成 `0x606060`（6 位），Alpha 默认是多少？为什么需要这个默认？

**答案**：Alpha 默认 `255`（完全不透明）。需要这个默认是因为 6 位串只够编码 B、G、R 三个分量，大多数场景下颜色本就不需要透明度，省略 Alpha 让配置更简洁；8 位形式则给需要半透明效果的场景（如面板背景 `0xeeeceeee`）留出口。

---

### 4.4 getAppOptions 读取应用级选项

#### 4.4.1 概念说明

`app_options` 是 `squirrel.yaml` 里「按宿主应用定制输入行为」的一节，结构如下（节选自 [data/squirrel.yaml:L391-L409](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L391-L409)）：

```yaml
app_options:
  com.apple.Terminal:
    ascii_mode: true      # 进入该应用默认西文模式
    no_inline: true       # 不使用行内预编辑
  org.vim.MacVim:
    ascii_mode: true
    no_inline: true
    vim_mode: true        # 退出 VIM 插入模式自动切回
```

键是应用的 Bundle ID（如 `com.apple.Terminal`），值是一个「布尔开关」映射。`getAppOptions(_ appName:)` 的任务就是：给定当前前台应用的 Bundle ID，读出它的所有布尔开关，返回 `[String: Bool]`。

这些开关随后会被 `SquirrelInputController.updateAppOptions()` 通过 `rimeAPI.set_option(session, key, value)` 写进引擎，从而改变引擎行为（例如 `ascii_mode` 决定中/英文、`no_inline` 决定是否行内预编辑——这两个开关在 u2-l7 的 inline 联合判定里直接被读取）。

#### 4.4.2 核心流程

librime 遍历一个 map 节用「迭代器三段式」：

```
getAppOptions(appName):
    rootKey = "app_options/\(appName)"
    iterator = RimeConfigIterator()
    config_begin_map(iterator, config, rootKey)   # 开始遍历 rootKey 这个 map
    while config_next(iterator):                   # 逐项前进
        key   = iterator.key    # 形如 "ascii_mode"
        path  = iterator.path   # 形如 "app_options/com.apple.Terminal/ascii_mode"
        value = getBool(path)   # 用完整路径读布尔
        appOptions[key] = value
    config_end(iterator)                          # 释放迭代器
    return appOptions
```

迭代器三段式 `begin_map → next → end` 是 librime 的固定用法，**`config_end` 必须配对调用**，否则迭代器持有的 C 资源泄漏。

注意一个细节：`getAppOptions` 用 `iterator.path`（完整路径）去调 `getBool`，而不是 `iterator.key`。因为 `getBool` 内部用的是「从配置根开始的绝对路径」，而 `key` 只是叶子键名。

#### 4.4.3 源码精读

`getAppOptions` 实现：

[sources/SquirrelConfig.swift:L102-L114](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelConfig.swift#L102-L114) —— 第 103 行拼出 `app_options/<appName>` 作为根键；第 106 行 `config_begin_map` 开始遍历；第 107–110 行 `while config_next` 逐项取出 `key` 与 `path`，用 `path` 调 `getBool`（这里复用了第 4.2 节的缓存与回退机制）；第 112 行 `config_end` 收尾释放。

真实调用点——切到某应用时读出开关并喂给引擎：

[sources/SquirrelInputController.swift:L366-L375](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L366-L375) —— `NSApp.squirrelAppDelegate.config?.getAppOptions(currentApp)` 取到 `[String: Bool]`，然后 `for (key, value) in appOptions { rimeAPI.set_option(session, key, value) }` 把每个开关写进当前 librime session。注意这里用的 `config` 是 AppDelegate 持有的 **base config**——`app_options` 只在 `squirrel.yaml` 里定义，不在方案配置里，所以从 base config 读是正确的。

> 旁注：紧接其后的 `getBool("unsafe/report_bundleid")`（第 376 行）演示了 base config 里另一个路径 `unsafe/` 的读取，证明 `getBool` 支持任意深度的斜杠路径。

#### 4.4.4 代码实践

**实践目标**：亲手为「终端类应用」加一条应用级规则，并推演它如何影响输入行为。

**操作步骤**：

1. 把仓库的 `data/squirrel.yaml` 复制到你自己的用户配置目录 `~/Library/Rime/squirrel.yaml`（这是 Squirrel 官方推荐的定制方式，**不要改仓库源文件**）。
2. 找到 `app_options:` 节，为一个你常用的终端 Bundle ID（例如 iTerm2 的 `com.googlecode.iterm2`，已存在）确认它有 `ascii_mode: true` 和 `no_inline: true`。
3. 追踪 `no_inline` 的去向：
   - 切到 iTerm2 时，`updateAppOptions` 把 `no_inline=true` 经 `set_option` 写进引擎。
   - 后续在 u2-l7 会看到，`rimeUpdate` 里读取这个 option，参与 `inlinePreedit`/`inlineCandidate` 的联合判定：`no_inline` 为真时强制不走行内预编辑。
4. 推演：在 iTerm2 里输入时，预编辑文本不会嵌进终端的命令行（因为终端对 marked text 回显支持不好），而是显示在悬浮面板里。

**需要观察的现象**：进入 iTerm2 自动切西文模式（`ascii_mode`）；即便切到中文，候选也不会嵌进行内（`no_inline`），而是走悬浮面板。

**预期结果**：与上述推演一致。

**待本地验证**：在 macOS 上重新部署 Squirrel（`Squirrel --build` 或菜单「重新部署」），切到 iTerm2 实测。

#### 4.4.5 小练习与答案

**练习 1**：`getAppOptions` 里 `config_begin_map` 的返回值被 `_ =` 丢弃了。如果返回的是「该应用根本不在 app_options 里」，会发生什么？

**答案**：`config_begin_map` 对不存在的键返回 `false`，迭代器不进入有效状态。`while config_next(&iterator)` 第一次就返回 `false`，循环体一次都不执行，`config_end` 仍被调用（释放一个空迭代器，安全），最终返回空字典 `[:]`。调用方 `getAppOptions(currentApp)` 得到非 nil 的空字典，`updateAppOptions` 里 `for` 循环不执行任何 `set_option`——即该应用沿用默认行为。

**练习 2**：为什么 `getAppOptions` 必须在 AppDelegate 的 base config 上调用，而不能在方案 schema config 上调？

**答案**：因为 `app_options` 节只在 `squirrel.yaml`（base config）里定义，方案配置通常不重复声明。若在方案 config 上调，`config_begin_map` 找不到 `app_options/...`，又因方案 config 的 `baseConfig` 恰好是 base config，理论上也能通过回退读到——但项目选择直接在 base config 上调，语义更清晰，也避免依赖回退这种「隐式」路径。

**练习 3**：如果忘了写 `config_end(iterator)`，会有什么后果？

**答案**：迭代器是 librime 内部分配的资源（持有指向配置树内部节点的指针），不调用 `config_end` 就不会释放，造成 C 堆内存泄漏。由于 `getAppOptions` 在每次切换前台应用时都可能被调用，泄漏会随使用累积。`config_end` 与 `config_begin_map` 的配对，和第 4.1 节 `config_close` 与 open 的配对，是同一类「C 资源所有权」约定（详见 u5-l4）。

---

## 5. 综合实践

把本讲四个最小模块串起来，做一个「配置读取追踪」小任务。

**场景**：你想给一个新装的代码编辑器（Bundle ID 假设为 `com.example.CodeEdit`）定制输入行为——进入它时默认西文、且不用行内预编辑；同时你想给它配一个偏暗的候选面板背景色。

**任务**：

1. **回退理解（4.1）**：你打算只在某个方案 schema 里覆盖 `corner_radius`，其余外观继承 `squirrel.yaml`。写出 SquirrelConfig 如何保证「其余外观继承」——指出 `getXxx` 里负责回退的那一行代码（文件名 + 行号）。

2. **缓存理解（4.2）**：主题会先 `load(forDarkMode: false)` 再 `load(forDarkMode: true)` 读两遍。说明 `style/corner_radius` 在这两遍里总共触发几次 librime 的 `config_get_double`，并解释为什么把 base config 做成共享单例能让缓存收益最大化。

3. **颜色解析（4.3）**：你想让候选背景是「半透明（Alpha≈0.88）、偏深的蓝绿色」。按 `0xAABBGGRR` 字节序，写出对应的 8 位十六进制串（取 Alpha=`0xe0`、Blue=`0x10`、Green=`0x20`、Red=`0x08`，组合成什么？），并说明你会把它写到 `squirrel.yaml` 的哪个路径下（某个 `preset_color_schemes` 的哪个字段）。

4. **应用选项（4.4）**：在 `~/Library/Rime/squirrel.yaml` 的 `app_options` 下新增：

   ```yaml
     com.example.CodeEdit:
       ascii_mode: true
       no_inline: true
   ```

   追踪这两个开关如何最终影响引擎：写出从 `getAppOptions` 到 `rimeAPI.set_option` 的调用链（文件名 + 行号），并说明 `no_inline` 在 u2-l7 的 inline 联合判定里扮演什么角色。

**参考要点**：

1. 回退在 [sources/SquirrelConfig.swift:L65](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelConfig.swift#L65)（`return baseConfig?.getBool(option)`，getDouble 对应第 77 行）。
2. 共触发 1 次：第一遍 miss → 回退到 base config → 命中并写入 base config 的 cache；第二遍在 base config cache 命中。共享单例让多个 schema config 复用同一份 base cache。
3. `0xAABBGGRR` 串为 `0xe0102008`（Alpha=`e0`、Blue=`10`、Green=`20`、Red=`08`）。写到某配色方案的 `candidate_back_color` 字段（参考 aqua 的 [data/squirrel.yaml:L85](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/data/squirrel.yaml#L85)），并把该配色方案名设到 `style/color_scheme`。
4. 调用链：[sources/SquirrelInputController.swift:L370](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L370) 取 `[String: Bool]` → [L373](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L373) `set_option(session, key, value)`。`no_inline` 在 u2-l7 里对 `inlinePreedit`/`inlineCandidate` 起「否决」作用——为真时两者都强制为假。

## 6. 本讲小结

- **SquirrelConfig 是 librime C 配置 API 的 Swift 门面**：把 `config_open/schema_open/config_get_*/config_begin_map` 等零散 C 函数，包装成 `getBool/getDouble/getString/getColor/getAppOptions` 五个类型化接口，返回值统一用 Optional 表达「键不存在」。
- **base config 与 schema config 是父子回退关系**：`open(schemaID:baseConfig:)` 把父配置存进 `baseConfig` 字段；`getXxx` 在当前配置未命中时递归调 `baseConfig?.getXxx(option)`。使用方还有一层「整配置级回退」：方案没有 `style` 节就整体退回 base config。
- **四个 getXxx 共用三段式模板**：查缓存 → 调 librime（成功则写缓存）→ 回退 baseConfig。`cache: [String: Any]` 的键是路径字符串、值是已读结果，生命周期与 SquirrelConfig 实例相同（≈ 一次样式重载）。
- **缓存落在「哪个实例」上有讲究**：回退命中时值缓存在父（base config）实例里，因此共享的 base config 缓存能被所有 schema config 复用，把重复 C 调用压到最少。
- **颜色字节序是 `0xAABBGGRR`**（8 位）或 `0xBBGGRR`（6 位，Alpha 默认 255），与常见的 `#AARRGGBB` 相反；解析后按 `display_p3` 或 `sRGB` 色空间构造 NSColor。
- **`getAppOptions` 用迭代器三段式 `begin_map → next → end`** 遍历 `app_options/<app>` 节，返回 `[String: Bool]`，结果经 `SquirrelInputController` 的 `set_option` 写进引擎，驱动 `ascii_mode`/`no_inline` 等运行时行为。

## 7. 下一步学习建议

本讲把「配置怎么读」讲透了，但还没讲「配置文件长什么样」和「读出来的值怎么变成主题」。建议按顺序往下：

1. **u3-l2 squirrel.yaml 配置文件结构**：逐节走读 `data/squirrel.yaml` 的 `style`、`preset_color_schemes`、`app_options`，把本讲里的路径字符串（如 `style/corner_radius`、`preset_color_schemes/aqua/back_color`）与真实 YAML 节对应起来。
2. **u3-l3 SquirrelTheme 主题加载**：看 `SquirrelTheme.load(config:dark:)` 如何把本讲的 `getColor/getString/getDouble` 结果组装成一个完整的主题对象（全局 style → 配色方案覆盖的两层结构）。
3. **u3-l4 亮/暗主题与 schema 特化样式**：看 `loadSettings(for:)` 如何组合 base config 与 schema config、以及 `native` 配色方案的特殊处理——这正是本讲「父子回退」的真实使用场景。
4. 如果想深挖 C 桥接底层（`RimeConfig` 句柄的所有权、`config_get_cstring` 返回指针的生命周期、`?=` 运算符定义），跳到 **u5-l4 Swift/C 桥接约定**。
