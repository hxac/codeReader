# 配置插件与 default.yaml

## 1. 本讲目标

本讲紧接 u4-l3（配置编译器与 `__include`/`__patch` DSL）。在 u4-l3 中我们看到，YAML 文件里的 `__include`、`__patch` 是用户**显式**写出来的指令。但 librime 还有一批「看不见的 include」：每一个方案的 `menu`、`punctuator`、`key_binder`、`recognizer` 等段落，常常并不在方案文件里完整定义，而是在部署期被**自动**从 `default.yaml` 注入进来。这些「自动注入」就是由**配置插件（Config Compiler Plugin）**完成的。

学完本讲，你应当能够：

- 说清楚 `ConfigCompilerPlugin` 这个两方法接口（`ReviewCompileOutput` / `ReviewLinkOutput`）的作用与触发时机。
- 复述 librime 内置的六个配置插件各自做什么、按什么顺序挂载、在哪一个编译阶段生效。
- 解释 `import_preset: default` 这一行「遗留语法」如何被 `LegacyPresetConfigPlugin` 翻译成一次 `__include`，以及方案自带的同名子键为什么不会被覆盖。
- 读懂 `data/minimal/default.yaml` 里每个顶层键的用途，并知道哪些段会被自动塞进每个方案。

---

## 2. 前置知识

本讲假设你已经掌握 u4-l1 ~ u4-l3 的内容，尤其：

- **配置树数据模型**（u4-l1）：`ConfigItem` / `ConfigValue` / `ConfigList` / `ConfigMap`，以及 `an<T>`、`As<T>`、`Is<T>` 等智能指针别名。
- **组件化加载**（u4-l2）：`ConfigBuilder`（注册名 `config_builder`，部署期用，支持编译 DSL）与 `ConfigLoader`（注册名 `config`/`schema`，运行时用，直接读 YAML）的区别；`ConfigData` 是配置树的实际载体。
- **编译器 DSL**（u4-l3）：`__include`/`__patch`/`__append`/`__merge` 四类指令、`/+` 追加后缀、依赖优先级 `kPendingChild < kInclude < kPatch`、`ConfigCowRef` 的写时拷贝（COW），以及 `Reference{resource_id, local_path, optional}` 三元组的含义。

补充两个本讲会反复用到、但属于 u4-l3 内核的概念，这里只做最小回顾：

- **`Cow(parent, key)`**：返回一个写时拷贝引用，指向 `parent` 下名为 `key` 的子节点；对该引用的写入只在副本上发生，不污染被多处共享的原始模板（见 [src/rime/config/config_cow_ref.h:82-87](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_cow_ref.h#L82-L87)）。
- **`Dependency::TargetedAt(target).Resolve(compiler)`**：把一个依赖（如 `IncludeReference`）锚定到某个目标节点并**立即**求解，而不是登记进依赖图延后求解（见 [src/rime/config/config_compiler_impl.h:27-31](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler_impl.h#L27-L31)）。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/rime/config/plugins.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/plugins.h) | 定义插件接口 `ConfigCompilerPlugin` 与六个内置插件类的声明。 |
| [src/rime/config/config_component.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_component.cc) | `ConfigBuilder` 持有插件列表；`MultiplePlugins` 适配器把多个插件串成一条链；`LoadConfig` 串联 `Compile`+`Link`。 |
| [src/rime/config/config_compiler.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.cc) | `Compile()` 末尾调 `ReviewCompileOutput`，`Link()` 末尾调 `ReviewLinkOutput`；并定义 `IncludeReference::Resolve` 的「先包含后覆盖」合并语义。 |
| [src/rime/core_module.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/core_module.cc) | 在 `core` 模块初始化时，按固定顺序把六个插件 `InstallPlugin` 到 `config_builder`。 |
| [src/rime/config/default_config_plugin.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/default_config_plugin.cc) | `DefaultConfigPlugin`：给每个 `*.schema` 注入 `menu`/`navigator`/`selector` 三个共享段。 |
| [src/rime/config/legacy_preset_config_plugin.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/legacy_preset_config_plugin.cc) | `LegacyPresetConfigPlugin`：把 `punctuator`/`key_binder`/`recognizer` 下的 `import_preset` 翻译成 `__include`。 |
| [src/rime/config/auto_patch_config_plugin.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/auto_patch_config_plugin.cc) | `AutoPatchConfigPlugin`：为每个配置自动挂一条「可选的 `<name>.custom` 补丁」。 |
| [src/rime/config/build_info_plugin.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/build_info_plugin.cc) | `BuildInfoPlugin`：写入 `__build_info`（rime 版本与各源文件时间戳）。 |
| [src/rime/config/save_output_plugin.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/save_output_plugin.cc) | `SaveOutputPlugin`：把编译产物落盘到 staging 目录，得到 `.yaml`。 |
| [src/rime/config/legacy_dictionary_config_plugin.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/legacy_dictionary_config_plugin.cc) | `LegacyDictionaryConfigPlugin`：占位实现（TODO，目前是空操作）。 |
| [data/minimal/default.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/default.yaml) | 全局默认配置，是上述自动注入的数据来源。 |

---

## 4. 核心概念与源码讲解

### 4.1 配置插件机制：接口、挂载与两阶段调用

#### 4.1.1 概念说明

u4-l3 讲的 `__include`/`__patch` 是**用户写的**指令，由 `ConfigCompiler` 在解析 YAML 时识别。但有一类需求是「无论用户写没写，引擎都要默认补上某些配置」——例如每个方案都该有 `menu/page_size`、每个拼音方案都该继承同一套标点映射。如果逼着每个方案作者都手写一遍 `__include: default:menu`，既啰嗦又容易漏。

**配置插件（Config Compiler Plugin）** 就是为这类「编译期的后处理钩子」设计的：它让引擎在配置被「编译」和「链接」完成之后，有机会再巡视一遍产物，**程序化地**补上或改写配置节点。你可以把它理解成编译器里的「AST 处理 pass」——源码（YAML）已经解析成内存树，插件在这棵树上做最后的改写。

#### 4.1.2 核心流程

一个插件只需要实现两个钩子，签名完全相同：

```text
bool ReviewCompileOutput(ConfigCompiler* compiler, an<ConfigResource> resource);
bool ReviewLinkOutput  (ConfigCompiler* compiler, an<ConfigResource> resource);
```

它们的触发点分布在 u4-l2 提到的「编译 → 链接」两阶段里：

```text
ConfigBuilder::LoadConfig(id)
  └─ ConfigCompiler compiler(resolver, &multiple_plugins);
     ├─ compiler.Compile(id)            // 阶段一：解析本文件 + 递归 __include/__patch
     │    └─ LoadFromFile(...)
     │    └─ plugin->ReviewCompileOutput(this, resource)   ← 每个资源编译完各调一次
     └─ compiler.Link(resource)         // 阶段二：求解依赖图，落实 include/patch
          └─ ResolveDependencies(...)
          └─ plugin->ReviewLinkOutput(this, target)        ← 链接完再调一次
```

两阶段的区别很关键：

- **`ReviewCompileOutput`** 在「这个文件刚解析完、它的 `__include`/`__patch` 还没求解」时调用，适合做「**追加**新的依赖」。`AutoPatchConfigPlugin` 就是在这里往依赖图里塞一条新的 patch 引用。
- **`ReviewLinkOutput`** 在「所有依赖都已求解、配置树已经定型」时调用，适合做「**就地改写**最终树」。`DefaultConfigPlugin`、`LegacyPresetConfigPlugin`、`BuildInfoPlugin`、`SaveOutputPlugin` 都在这里动手。

由于 `ConfigBuilder` 可以挂多个插件，编译器实际持有的是由 `MultiplePlugins` 适配器把多个插件**串成一条链**的复合插件；链上任何一个返回 `false` 都会短路，整体判定为失败。

#### 4.1.3 源码精读

**接口定义**——`ConfigCompilerPlugin` 是一个纯抽象类，用 typedef 把「审查函数签名」收拢成 `Review`，再声明两个纯虚钩子（[src/rime/config/plugins.h:15-23](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/plugins.h#L15-L23)）：

```cpp
class ConfigCompilerPlugin {
 public:
  typedef bool Review(ConfigCompiler* compiler, an<ConfigResource> resource);
  virtual Review ReviewCompileOutput = 0;
  virtual Review ReviewLinkOutput = 0;
};
```

> 注意：这里 `Review` 是一个**函数类型**别名，`virtual Review ReviewCompileOutput = 0;` 声明的是一个返回 `bool`、参数为 `(ConfigCompiler*, an<ConfigResource>)` 的纯虚函数。六个子类各自 `Review ReviewCompileOutput;`（非 virtual、`= default` 语义）提供具体实现。

**复合插件**——`MultiplePlugins` 持有插件容器的引用，把两个钩子都委托给 `ReviewedByAll`，逐个调用、遇 `false` 即停（[src/rime/config/config_component.cc:211-242](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_component.cc#L211-L242)）：

```cpp
template <class Container>
struct MultiplePlugins : ConfigCompilerPlugin {
  Container& plugins;
  bool ReviewedByAll(Reviewer reviewer, ConfigCompiler* compiler, an<ConfigResource> resource) {
    for (const auto& plugin : plugins)
      if (!((*plugin).*reviewer)(compiler, resource))
        return false;          // 任一插件否决则整体失败
    return true;
  }
};
```

`ConfigBuilder::LoadConfig` 创建这个复合插件并喂给 `ConfigCompiler`，然后跑 `Compile` + `Link`（[src/rime/config/config_component.cc:244-253](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_component.cc#L244-L253)）：

```cpp
MultiplePlugins<decltype(plugins_)> multiple_plugins(plugins_);
ConfigCompiler compiler(resource_resolver, &multiple_plugins);
auto resource = compiler.Compile(config_id);
if (resource->loaded && !compiler.Link(resource)) { /* 报错 */ }
```

**两个调用点**——分别在 `Compile()` 与 `Link()` 的末尾（[src/rime/config/config_compiler.cc:385-396](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.cc#L385-L396) 与 [src/rime/config/config_compiler.cc:556-565](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.cc#L556-L565)）：

```cpp
// Compile 末尾：解析完就审查
resource->loaded = resource->data->LoadFromFile(..., this);
if (plugin_) plugin_->ReviewCompileOutput(this, resource);

// Link 末尾：依赖求解完再审查
return ResolveDependencies(found->first + ":") &&
       (plugin_ ? plugin_->ReviewLinkOutput(this, target) : true);
```

**挂载点**——六个内置插件在 `core` 模块初始化时，按下面这个**固定顺序**安装到 `config_builder` 组件上（[src/rime/core_module.cc:23-32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/core_module.cc#L23-L32)）：

```cpp
auto config_builder = new ConfigComponent<ConfigBuilder>([&](ConfigBuilder* builder) {
  builder->InstallPlugin(new AutoPatchConfigPlugin);
  builder->InstallPlugin(new DefaultConfigPlugin);
  builder->InstallPlugin(new LegacyPresetConfigPlugin);
  builder->InstallPlugin(new LegacyDictionaryConfigPlugin);
  builder->InstallPlugin(new BuildInfoPlugin);
  builder->InstallPlugin(new SaveOutputPlugin);
});
r.Register("config_builder", config_builder);
```

顺序之所以重要，是因为 `MultiplePlugins` 按列表顺序逐个调用；先后顺序决定了「谁先改树」。例如 `SaveOutputPlugin` 排在最后，保证落盘的是被前面所有插件改写完毕的最终产物。

#### 4.1.4 代码实践

**实践目标**：把「插件链 → 两个钩子 → 两个调用点」这条调用链在源码里走通。

**操作步骤**：

1. 打开 [src/rime/core_module.cc:23-32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/core_module.cc#L23-L32)，记下六个 `InstallPlugin` 的顺序。
2. 跳到 [src/rime/config/config_component.cc:244-253](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_component.cc#L244-L253)，确认 `ConfigBuilder::LoadConfig` 把这串插件包成 `MultiplePlugins` 传给 `ConfigCompiler`。
3. 在 [src/rime/config/config_compiler.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.cc) 中分别找到 `Compile()`（约 385 行）与 `Link()`（约 556 行）里对 `ReviewCompileOutput` / `ReviewLinkOutput` 的调用。

**需要观察的现象**：`ReviewCompileOutput` 出现在 `LoadFromFile` **之后**、依赖求解**之前**；`ReviewLinkOutput` 出现在 `ResolveDependencies` **之后**。

**预期结果**：你能用一句话回答「为什么 `AutoPatchConfigPlugin` 用 `ReviewCompileOutput` 而 `DefaultConfigPlugin` 用 `ReviewLinkOutput`」——前者要在依赖求解**前**追加新依赖，后者要在依赖求解**后**就地改写。

#### 4.1.5 小练习与答案

**练习 1**：如果某个插件的 `ReviewLinkOutput` 返回 `false`，会发生什么？

**答案**：`MultiplePlugins::ReviewedByAll` 立即短路返回 `false`，后续插件（包括 `SaveOutputPlugin`）不再执行；`Link()` 返回 `false`，`ConfigBuilder::LoadConfig` 打印 `error building config` 日志，该配置构建失败。

**练习 2**：为什么 `SaveOutputPlugin` 必须排在 `InstallPlugin` 列表的最末尾？

**答案**：因为它负责把「最终配置树」落盘。若它在前，落盘的就会是还没被 `BuildInfoPlugin` 等后续插件改写过的中间产物，写出去的 `.yaml` 不完整。

---

### 4.2 DefaultConfigPlugin：方案共享段的自动注入

#### 4.2.1 概念说明

`menu`（候选分页大小）、`navigator`（光标移动按键）、`selector`（选词按键）这三段配置，几乎每个输入方案都需要，而且内容大同小异。`DefaultConfigPlugin` 的工作就是：**只要你在编译一个 `*.schema` 资源，就自动把 `default.yaml` 里的 `menu`/`navigator`/`selector` 三段 `__include` 进来**，免去每个方案都手写一遍。

回顾 u2-l3：`Schema::FetchUsefulConfigItems` 会预提取 `menu/page_size`。这个 `page_size`（默认 5）正是靠本插件从 `default.yaml` 的 `menu` 段注入到方案里的——这就是「方案文件里没写 `menu`，却有默认页大小」的根源。

#### 4.2.2 核心流程

```text
对每个 resource（在 ReviewLinkOutput 阶段）：
  若 resource_id 不以 ".schema" 结尾 → 直接返回 true（只对方案生效）
  否则依次注入三段：
    target = Cow(resource, "menu")            # 写时拷贝引用，指向方案的 menu 子节点
    ref = Reference{"default", "menu", true}  # 从 default 资源取 menu 段；optional=true
    IncludeReference{ref}.TargetedAt(target).Resolve(compiler)   # 立即求解
    # navigator、selector 同理
```

`Reference{"default", "menu", true}` 三元组的含义（u4-l3 已建立）：`resource_id="default"`（即 `default.yaml`）、`local_path="menu"`（取它的 `menu` 子段）、`optional=true`（找不到也不报错）。

`optional=true` 是一个关键细节：`data/minimal/default.yaml` 里**只有 `menu` 段，没有 `navigator` 和 `selector` 段**。靠 `optional=true`，`IncludeReference::Resolve` 在找不到目标时返回 `reference.optional`（即 `true`），从而容忍它们的缺失（参见 [src/rime/config/config_compiler.cc:69-71](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.cc#L69-L71)）。完整版的 `default.yaml`（来自 plum 等仓库）会补上 `navigator`/`selector`。

#### 4.2.3 源码精读

整个插件逻辑在 `ReviewLinkOutput` 里，`ReviewCompileOutput` 是空操作（[src/rime/config/default_config_plugin.cc:12-46](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/default_config_plugin.cc#L12-L46)）：

```cpp
bool DefaultConfigPlugin::ReviewLinkOutput(ConfigCompiler* compiler,
                                           an<ConfigResource> resource) {
  if (!boost::ends_with(resource->resource_id, ".schema"))
    return true;                       // 只对方案资源生效
  {
    auto target = Cow(resource, "menu");
    Reference reference{"default", "menu", true};
    if (!IncludeReference{reference}.TargetedAt(target).Resolve(compiler)) {
      LOG(ERROR) << "failed to include section " << reference;
      return false;
    }
  }
  // navigator、selector 两段结构完全相同，省略
  return true;
}
```

注意它与用户手写 `__include` 的等价性：这段代码等价于在方案的 `menu` 节点上写了一条 `__include: default:menu`，只是由插件在链接期自动补上。它也复用了 u4-l3 讲过的 `Cow` + `IncludeReference` + `TargetedAt` + `Resolve` 同一套原语——插件并不发明新机制，只是**程序化地驱动**已有机制。

#### 4.2.4 代码实践

**实践目标**：验证「方案没写 `menu`，却有默认 `page_size`」这一现象的来源。

**操作步骤**：

1. 打开 [data/minimal/luna_pinyin.schema.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml)，确认它的顶层**没有** `menu` 键。
2. 打开 [data/minimal/default.yaml:25-26](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/default.yaml#L25-L26)，确认 `menu/page_size: 5`。
3. 回顾 u2-l3 提到的 `Schema::FetchUsefulConfigItems`，它会读 `menu/page_size`。

**需要观察的现象**：方案文件本身不含 `menu`，但运行时 `Schema` 的 `page_size` 仍为 5。

**预期结果**：你能解释这条「隐形的数据流」——`DefaultConfigPlugin` 在部署期把 `default:menu` 注入方案，部署产物（staging 下的 `luna_pinyin.schema.yaml`）里就带上了 `menu/page_size: 5`，之后运行时 `ConfigLoader` 直接读这份产物即可。（部署产物的落盘由 4.4 的 `SaveOutputPlugin` 完成。）

#### 4.2.5 小练习与答案

**练习 1**：如果把 `Reference{"default", "menu", true}` 的第三个参数改成 `false`，在最小数据集下会怎样？

**答案**：`menu` 段在 `default.yaml` 中存在，所以仍能正常注入，行为不变。但若改的是 `navigator` 或 `selector` 那两条（它们在 minimal 版 `default.yaml` 中不存在），`optional=false` 会让 `IncludeReference::Resolve` 在找不到目标时返回 `false`，插件打印 `failed to include section` 并使整个方案构建失败。

**练习 2**：为什么这个插件只对 `*.schema` 资源生效，而不作用于 `default` 本身或 `*.custom`？

**答案**：因为 `menu`/`navigator`/`selector` 是「每个方案都该继承的全局段」，注入目标是方案。`default.yaml` 本身就是这些段的**源头**，再给它注入自己没有意义；`*.custom` 是用户补丁文件，也不该被强塞这些段。靠 `ends_with(..., ".schema")` 这一守卫把作用域限定在方案上。

---

### 4.3 LegacyPresetConfigPlugin：把 `import_preset` 编译成 `__include`

#### 4.3.1 概念说明

很多老方案里有这样一行写法：

```yaml
punctuator:
  import_preset: default
```

它的字面意思是「标点配置从 `default` 预设导入」。但 `import_preset` 并**不是** u4-l3 讲过的 `__include`/`__patch` DSL——它是更早年代遗留下来的一种「语法糖」。`LegacyPresetConfigPlugin` 的职责就是在编译期把它**翻译成一次真正的 `__include`**，让旧方案无需改写就能继续工作。

它处理三个段落：`key_binder`、`punctuator`、`recognizer`。三者都遵循「读到 `xxx/import_preset: <preset>`，就等价于 `__include: <preset>:xxx`」的规则，其中 `key_binder` 多了一层「保留方案自带绑定」的特殊处理。

#### 4.3.2 核心流程

```text
对每个 *.schema 资源（ReviewLinkOutput 阶段）：
  ── key_binder ──
  若存在 key_binder/import_preset（值 = preset，如 "default"）：
      若方案自带 key_binder/bindings：
          把自带 bindings 暂存到 key_binder/bindings/+ （追加位）
          清空 key_binder/bindings
      include default:key_binder 到方案的 key_binder
      # 结果：默认绑定在前，方案自带绑定被「追加」在后，二者合并而非覆盖

  ── punctuator ──
  若存在 punctuator/import_preset：
      include default:punctuator 到方案的 punctuator   # optional=false

  ── recognizer ──
  若存在 recognizer/import_preset：
      include default:recognizer 到方案的 recognizer   # optional=false
```

这里有两处与 u4-l3 衔接的关键语义：

1. **`/+` 追加位**：`key_binder/bindings/+` 用到了 u4-l3 讲过的 `/+` 后缀（列表追加）。把方案自带的绑定暂存到追加位，等默认 `bindings` 被 include 进来后，追加位上的内容会**续在默认列表之后**，从而实现「默认 + 自定义」的合并。

2. **`IncludeReference` 的「先包含、后覆盖」**：这是本讲最需要记住的一条。看 [src/rime/config/config_compiler.cc:66-80](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.cc#L66-L80)：

   ```cpp
   bool IncludeReference::Resolve(ConfigCompiler* compiler) {
     auto included = ResolveReference(compiler, reference);   // 取被引用的段
     if (!included) return reference.optional;
     auto overrides = As<ConfigMap>(**target);                // 记下目标当前自带的内容
     *target = included;                                      // 先整体换成被引用段
     if (overrides && !overrides->empty())
       MergeTree(target, overrides);                          // 再把自带内容合并回去
     return true;
   }
   ```

   也就是说，一次 include 会**先搬入引用段，再把目标原本自带的键合并（覆盖）回去**。所以方案里**已经显式写出的同名子键不会被 include 抹掉**——这正是 `luna_pinyin.schema.yaml` 里 `recognizer` 同时写了 `import_preset: default` 和自己的 `patterns`，最终两套 `patterns`（default 的 email/uppercase/url + 方案的 alphabet/cangjie/pinyin/reverse_lookup）能共存的原因。

#### 4.3.3 源码精读

完整实现见 [src/rime/config/legacy_preset_config_plugin.cc:18-75](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/legacy_preset_config_plugin.cc#L18-L75)。`key_binder` 分支的「保留自带绑定」逻辑（最精妙的一段）如下：

```cpp
if (auto preset = resource->data->Traverse("key_binder/import_preset")) {
  auto preset_config_id = As<ConfigValue>(preset)->str();     // "default"
  auto target = Cow(resource, "key_binder");
  auto map = As<ConfigMap>(**target);
  if (map && map->HasKey("bindings")) {                       // 方案自带了绑定
    auto appended = map->Get("bindings");                     // 取出方案自带绑定
    *Cow(target, "bindings/+") = appended;                    // 暂存到追加位 bindings/+
    (*target)["bindings"] = nullptr;                          // 清空 bindings
  }
  Reference reference{preset_config_id, "key_binder", false}; // include default:key_binder
  IncludeReference{reference}.TargetedAt(target).Resolve(compiler);
}
```

`punctuator` 与 `recognizer` 分支更直白——没有自带内容需要保留，直接 include（[src/rime/config/legacy_preset_config_plugin.cc:48-73](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/legacy_preset_config_plugin.cc#L48-L73)）：

```cpp
if (auto preset = resource->data->Traverse("punctuator/import_preset")) {
  auto preset_config_id = As<ConfigValue>(preset)->str();
  Reference reference{preset_config_id, "punctuator", false};
  IncludeReference{reference}
      .TargetedAt(Cow(resource, "punctuator"))
      .Resolve(compiler);
}
// recognizer 分支结构相同，local_path 换成 "recognizer"
```

注意三处的 `optional=false`（与 `DefaultConfigPlugin` 的 `true` 相反）：因为 `import_preset` 是用户**显式**声明的「我要从这个预设导入」，若该预设里找不到对应段，理应报错而非静默忽略。

把这套翻译对照真实方案看：[data/minimal/luna_pinyin.schema.yaml:147-159](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L147-L159) 里三段都用了 `import_preset: default`，其中 `recognizer` 还自带了 `patterns`：

```yaml
punctuator:
  import_preset: default
key_binder:
  import_preset: default
recognizer:
  import_preset: default
  patterns:
    alphabet: '(?<![A-Z]):[^;]*;?$'
    cangjie: "C:[a-z']*;?$"
    # ...
```

#### 4.3.4 代码实践

> 这是本讲的主实践任务：**对照 `luna_pinyin.schema.yaml` 中 `punctuator`/`key_binder`/`recognizer` 的 `import_preset: default`，说明这些配置最终如何从 `default.yaml` 合并进来。**

**实践目标**：把「`import_preset` 一行」到「最终配置树里出现完整段落」的变换，在三段上分别讲清楚。

**操作步骤**：

1. 在 [data/minimal/luna_pinyin.schema.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml) 中定位三处 `import_preset: default`（约 147–154 行）。
2. 在 [data/minimal/default.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/default.yaml) 中找到对应的源段：`punctuator`（28–95 行）、`key_binder`（97–129 行）、`recognizer`（131–135 行）。
3. 对照 [src/rime/config/legacy_preset_config_plugin.cc:18-75](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/legacy_preset_config_plugin.cc#L18-L75) 与 [src/rime/config/config_compiler.cc:66-80](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.cc#L66-L80)，按下表逐栏填写。

**需要观察的现象与预期结果**（填表）：

| 段 | 方案里写了什么 | 翻译成的 include | default 提供什么 | 最终合并结果 |
| --- | --- | --- | --- | --- |
| `punctator` | 仅 `import_preset: default` | `__include: default:punctuator` | `full_shape`/`half_shape` 两张标点映射 | 方案得到完整标点映射 |
| `key_binder` | 仅 `import_preset: default` | `__include: default:key_binder`（方案无自带 `bindings`，跳过追加逻辑） | Emacs 风格光标键、翻页键、开关热键等 `bindings` | 方案得到默认全部绑定 |
| `recognizer` | `import_preset: default` **+** 自带 `patterns` | `__include: default:recognizer`，再 MergeTree 覆盖回自带 `patterns` | `patterns: email/uppercase/url` | default 的 3 条 + 方案的 4 条 `patterns` **共存** |

**关键验证点**：`recognizer` 一栏体现了 4.3.2 讲的「先包含、后覆盖」——default 的 `patterns` 不会被方案自带的 `patterns` 整体替换，而是按键合并；方案的 `alphabet`/`cangjie`/`pinyin`/`reverse_lookup` 与 default 的 `email`/`uppercase`/`url` 最终都在同一张 `patterns` 表里。本表为依据源码与配置推导的结论，**实际部署产物可用 4.4 末尾的方法落盘后核对（待本地验证）**。

#### 4.3.5 小练习与答案

**练习 1**：`key_binder` 的追加逻辑（`bindings/+`）在 `luna_pinyin.schema.yaml` 这个具体例子里会触发吗？

**答案**：不会。因为 `luna_pinyin.schema.yaml` 的 `key_binder` 下只有 `import_preset: default`，并没有 `bindings` 键，所以 `map->HasKey("bindings")` 为假，跳过暂存步骤，直接整体 include 默认的 `key_binder`。追加逻辑只在「方案既 `import_preset: default`、又自己列了 `bindings`」时才生效。

**练习 2**：为什么 `LegacyPresetConfigPlugin` 的三条 `Reference` 都用 `optional=false`，而 `DefaultConfigPlugin` 用 `optional=true`？

**答案**：语义不同。`DefaultConfigPlugin` 注入的是「最好有、没有也能跑」的通用段（minimal 版 default 甚至没有 `navigator`/`selector`），所以宽容（`true`）。`LegacyPresetConfigPlugin` 处理的是用户**显式**声明的 `import_preset`——用户指名要某个预设的某段，找不到就是配置错误，应严格失败（`false`），避免「静默丢配置」导致难以排查的 bug。

---

### 4.4 其余内置插件：AutoPatch / BuildInfo / SaveOutput / LegacyDictionary

#### 4.4.1 概念说明

除了 4.2、4.3 两个「注入/翻译」插件，`core_module.cc` 还挂了四个插件，它们各管一件事：

- **`AutoPatchConfigPlugin`**：让 `*.custom.yaml` 用户补丁**自动生效**，而无需在方案里显式写 `__patch`。这是 Rime「写一个 `xxx.custom.yaml` 就能改官方方案」能力的底层支撑。
- **`BuildInfoPlugin`**：在产物里盖一个 `__build_info` 戳，记录 librime 版本和各源文件的修改时间，供增量部署判断「是否需要重建」。
- **`SaveOutputPlugin`**：把编译好的配置树落盘成 staging 目录下的 `.yaml`，即运行时 `ConfigLoader` 实际读取的文件。
- **`LegacyDictionaryConfigPlugin`**：占位类，目前是 TODO 空操作。

#### 4.4.2 核心流程

```text
AutoPatchConfigPlugin::ReviewCompileOutput（编译期，依赖求解前）：
  对每个非 .custom 资源：
    若根节点已有显式 __patch → 跳过（用户自己管）
    否则：登记一条「可选」依赖，指向 <name>.custom 的 /patch 段
    # 即自动等价于：__patch: <name>.custom:/patch?

BuildInfoPlugin::ReviewLinkOutput（链接期）：
  在产物根写入 __build_info/rime_version = RIME_VERSION
  遍历所有资源，记录每个源文件路径的 last_write_time 到 __build_info/timestamps

SaveOutputPlugin::ReviewLinkOutput（链接期，排在最后）：
  用 staging 资源解析器算出输出路径（<resource_id>.yaml）
  resource->data->SaveToFile(file_path)   # 落盘

LegacyDictionaryConfigPlugin：两钩子都直接 return true（未实现）
```

`AutoPatchConfigPlugin` 的精妙处在于它工作在 **`ReviewCompileOutput`**（编译期）：此时依赖图还在构造中，它能往里**追加**一条 `PatchReference`，让后续的 `ResolveDependencies` 自然把它求解掉。它生成的引用形如 `luna_pinyin.schema:/__patch: luna_pinyin.custom:/patch?`——末尾的 `?` 正是 u4-l3 讲过的 `optional` 标记，表示「用户没写 `.custom.yaml` 也不报错」。

#### 4.4.3 源码精读

**AutoPatchConfigPlugin**——见 [src/rime/config/auto_patch_config_plugin.cc:19-36](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/auto_patch_config_plugin.cc#L19-L36)，关键是它先查根节点是否已有显式 `__patch`（优先级 `>= kPatch`），有则不重复挂：

```cpp
if (boost::ends_with(resource->resource_id, ".custom")) return true;  // 不对补丁本身再打补丁
auto root_deps = compiler->GetDependencies(resource->resource_id + ":");
if (!root_deps.empty() && root_deps.back()->priority() >= kPatch) return true;  // 已有显式 __patch
auto patch_resource_id = remove_suffix(resource->resource_id, ".schema") + ".custom";
compiler->Push(resource);
compiler->AddDependency(New<PatchReference>(Reference{patch_resource_id, "patch", true}));
compiler->Pop();
```

**BuildInfoPlugin**——见 [src/rime/config/build_info_plugin.cc:19-44](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/build_info_plugin.cc#L19-L44)，写版本号 + 用 `std::filesystem::last_write_time` 记录每个源文件时间戳：

```cpp
auto build_info = (*resource)["__build_info"];
build_info["rime_version"] = RIME_VERSION;
compiler->EnumerateResources([&](an<ConfigResource> resource) {
  /* 对每个已加载且有磁盘路径的资源，记录其 mtime */
  timestamps[resource->resource_id] =
      (int)filesystem::to_time_t(std::filesystem::last_write_time(file_path));
});
```

**SaveOutputPlugin**——见 [src/rime/config/save_output_plugin.cc:13-30](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/save_output_plugin.cc#L13-L30)，构造时用 `Service` 建一个指向 staging 目录的解析器，链接期把树写出去：

```cpp
static const ResourceType kCompiledConfig = {"compiled_config", "", ".yaml"};
SaveOutputPlugin::SaveOutputPlugin()
    : resource_resolver_(
          Service::instance().CreateStagingResourceResolver(kCompiledConfig)) {}
bool SaveOutputPlugin::ReviewLinkOutput(ConfigCompiler* compiler, an<ConfigResource> resource) {
  auto file_path = resource_resolver_->ResolvePath(resource->resource_id);
  return resource->data->SaveToFile(file_path);
}
```

**LegacyDictionaryConfigPlugin**——见 [src/rime/config/legacy_dictionary_config_plugin.cc:10-22](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/legacy_dictionary_config_plugin.cc#L10-L22)，两个钩子都是 `return true;`（注释写明 `TODO: unimplemented`），目前纯占位。

#### 4.4.4 代码实践

**实践目标**：亲眼看到「部署产物」长什么样，验证前面几个插件的改写确实落盘了。

**操作步骤**：

1. 按 u1-l2 的方法构建 librime（`make` 或 `cmake`）。
2. 运行 `tools/rime_api_console`（u1-l5），它在启动时会触发部署，把编译产物写到用户数据目录的 staging/build 子目录下。
3. 在该目录中找到 `luna_pinyin.schema.yaml`，打开查看。

**需要观察的现象**：部署产物里的 `luna_pinyin.schema.yaml` 应当出现：原本没有的 `menu` 段（来自 4.2 的 `DefaultConfigPlugin`）、被展开的 `punctuator`/`key_binder`/`recognizer` 具体内容（来自 4.3 的 `LegacyPresetConfigPlugin`）、以及一个 `__build_info` 段（来自 `BuildInfoPlugin`）。

**预期结果**：产物文件里 `recognizer/patterns` 同时包含 `email`/`uppercase`/`url`（来自 default）和 `alphabet`/`cangjie`/`pinyin`/`reverse_lookup`（方案自带），印证 4.3.4 的合并结论。**若你无法在本机构建运行，此项标注为「待本地验证」**，可改为纯源码阅读：对照 `SaveOutputPlugin::ReviewLinkOutput` 与 `ConfigData::SaveToFile`，理解产物路径由 `CreateStagingResourceResolver` 决定。

#### 4.4.5 小练习与答案

**练习 1**：用户写了一个 `luna_pinyin.custom.yaml`，但没在方案里写任何 `__patch`，这个补丁会生效吗？为什么？

**答案**：会生效。`AutoPatchConfigPlugin` 在编译 `luna_pinyin.schema` 时（编译期），发现根节点没有显式 `__patch`，就自动登记一条指向 `luna_pinyin.custom:/patch` 的**可选** `PatchReference`（`optional=true`）。后续 `ResolveDependencies` 会把这条 patch 求解掉，用户补丁因而自动应用。

**练习 2**：`__build_info/timestamps` 有什么用？

**答案**：它记录了本次构建所依据的各源文件修改时间。部署器在下次启动时可以比较这些时间戳与磁盘文件的当前 `last_write_time`，若源文件未变则跳过重建（增量部署），这正是 u9-l1/u9-l2 部署任务「检测改动」判断的依据之一。

---

### 4.5 default.yaml：全局配置蓝本

#### 4.5.1 概念说明

经过 4.2 ~ 4.4，我们已经知道 `default.yaml` 是「自动注入的数据来源」。本模块从**数据视角**通览 [data/minimal/default.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/default.yaml) 的顶层结构，把每个键和「谁消费它、怎么进入方案」对应起来。

需要先建立一个区分：`default.yaml` 里的键有**三种**进入运行时的路径——

1. **被插件自动注入到每个方案**：`menu`（由 `DefaultConfigPlugin`）。
2. **被插件按 `import_preset` 翻译注入**：`punctuator`/`key_binder`/`recognizer`（由 `LegacyPresetConfigPlugin`，前提是方案写了 `import_preset: default`）。
3. **作为全局配置直接被相应子系统读取**：`schema_list`/`switcher`（方案切换器，u9-l4）等。

#### 4.5.2 核心流程（顶层键总览）

| 顶层键 | 行号 | 作用 | 进入方案的路径 |
| --- | --- | --- | --- |
| `config_version` | 4 | 配置版本号，用于判断是否需要重新部署 | 全局，部署器读取 |
| `schema_list` | 6-8 | 可用方案清单（minimal 版含 `luna_pinyin`、`cangjie5`） | 切换器 `Switcher` 据此列出方案（u9-l4） |
| `switcher` | 10-23 | 方案切换菜单本身的行为：热键、`save_options`、折叠/缩写选项 | 切换器引擎读取 |
| `menu` | 25-26 | 候选分页，最关键是 `page_size: 5` | `DefaultConfigPlugin` 自动 include 进每个方案；`Schema::FetchUsefulConfigItems` 读取 |
| `punctuator` | 28-95 | 标点映射（`full_shape`/`half_shape` 两套） | 方案写 `punctuator/import_preset: default` 时由 `LegacyPresetConfigPlugin` 翻译注入 |
| `key_binder` | 97-129 | 按键绑定（Emacs 光标键、翻页、开关热键等） | 方案写 `key_binder/import_preset: default` 时翻译注入 |
| `recognizer` | 131-135 | 正则识别模式（email/uppercase/url） | 方案写 `recognizer/import_preset: default` 时翻译注入 |
| `ascii_composer` | 137-142 | Shift/Control 等修饰键的切换行为 | `ascii_composer` 处理器读取（参见 u6-l2） |

#### 4.5.3 源码精读

挑三处最能体现「蓝本」价值的片段。

**`menu`——一行决定全局页大小**（[data/minimal/default.yaml:25-26](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/default.yaml#L25-L26)）：

```yaml
menu:
  page_size: 5
```

配合 4.2 的 `DefaultConfigPlugin`，这一行等价于「所有方案的默认候选页大小都是 5」。想全局改成 9，只需改这一处并重新部署，无需逐个方案修改。

**`switcher`——切换器自身的配置**（[data/minimal/default.yaml:10-23](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/default.yaml#L10-L23)）：

```yaml
switcher:
  caption: 〔方案選單〕
  hotkeys:
    - Control+grave
    - Control+Shift+grave
    - F4
  save_options:
    - full_shape
    - ascii_punct
    - simplification
    - extended_charset
```

`hotkeys` 是呼出方案菜单的快捷键；`save_options` 列出的开关状态会被持久化到 `user.yaml`（u9-l3/u9-l4 详述）。这些是**切换器**（一个特殊引擎）的配置，而非普通方案。

**`key_binder/bindings`——被注入的按键绑定样本**（[data/minimal/default.yaml:97-129](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/default.yaml#L97-L129)），摘录几条：

```yaml
key_binder:
  bindings:
    - { when: composing, accept: Control+p, send: Up }     # Emacs 风格光标上移
    - { when: paging, accept: minus, send: Page_Up }       # 减号翻上页
    - { when: always, accept: Control+Shift+2, toggle: ascii_mode }  # 中/英切换
```

每一项的 `when` 表示触发时机（`composing`/`paging`/`has_menu`/`predicting`/`always`），`accept` 是捕获的按键，`send`/`toggle`/`select` 是动作。这些绑定经 4.3 的翻译注入方案后，由 `key_binder` 处理器（u6-l2）消费。

#### 4.5.4 代码实践

**实践目标**：把 `default.yaml` 的每个顶层键标注出「消费者」与「进入路径」。

**操作步骤**：

1. 打开 [data/minimal/default.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/default.yaml)，逐个顶层键过一遍。
2. 对每个键，在本讲 4.5.2 的表格里找到它的「进入路径」。
3. 对 `menu`、`punctuator`、`key_binder`、`recognizer` 四个键，分别追溯到本讲对应的插件代码（4.2、4.3）确认注入逻辑。

**需要观察的现象**：`default.yaml` 并不是「一整块被原样读入」——它的不同段经由**不同机制**分头进入运行时。

**预期结果**：你能画出一张「`default.yaml` 的键 → 消费它的插件/子系统」映射图，并指出 `menu` 走 `DefaultConfigPlugin`、`punctuator/key_binder/recognizer` 走 `LegacyPresetConfigPlugin`、`schema_list/switcher` 走切换器。

#### 4.5.5 小练习与答案

**练习 1**：如果把 `default.yaml` 的 `menu/page_size` 从 5 改成 9，哪些方案会受影响？需要改方案文件吗？

**答案**：所有「没有在方案里显式覆盖 `menu/page_size`」的方案都会变成每页 9 个候选，**无需改任何方案文件**——因为 `DefaultConfigPlugin` 在部署期会把新的 `menu` 段注入每个方案。只有那些自己写了 `menu/page_size` 的方案不受影响（`IncludeReference` 的「后覆盖」语义让方案自带的值优先）。

**练习 2**：`ascii_composer` 段没有出现在 `DefaultConfigPlugin` 或 `LegacyPresetConfigPlugin` 的注入清单里，它如何起作用？

**答案**：它不是被插件自动注入的，而是由 `ascii_composer` 这个 Processor 组件在方案引擎里被装配后，按需读取（方案的 `engine/processors` 列了 `ascii_composer`，见 u6-l2）。`default.yaml` 里的 `ascii_composer` 段是「全局默认值」的约定位置，方案可通过 `__include` 或自身配置引用它，但本讲的两个插件不负责它。这说明 `default.yaml` 既是「自动注入源」也是「全局默认约定」的双重角色。

---

## 5. 综合实践

把本讲四个机制串起来，做一次「端到端追踪」。

**任务**：解释下面这个最小方案的 `punctuator` 段，在部署后会变成什么样，并指出是哪几个插件、按什么顺序把它改写成这样的。

```yaml
# my.schema.yaml （示例代码，非仓库原有文件）
schema:
  schema_id: my
  name: 我的方案
engine:
  processors: [speller, punctuator, selector, express_editor]
  segmentors: [abc_segmentor, punct_segmentor, fallback_segmentor]
  translators: [script_translator, punct_translator]
  filters: [uniquifier]
menu:
  page_size: 7          # 方案自带，想覆盖默认的 5
punctuator:
  import_preset: default # 复用默认标点映射
```

**要求**：

1. 指出 `DefaultConfigPlugin` 会不会处理这个方案，处理了哪些段，`menu/page_size` 最终是 5 还是 7，为什么。
2. 指出 `LegacyPresetConfigPlugin` 如何处理 `punctuator/import_preset: default`，最终 `punctuator` 段的内容来自哪里。
3. 列出 `AutoPatchConfigPlugin`、`BuildInfoPlugin`、`SaveOutputPlugin` 各自对这个方案的贡献。
4. 给出六个插件的执行顺序（区分 `ReviewCompileOutput` 与 `ReviewLinkOutput` 两个阶段）。

**参考分析要点**（先自己推导，再对照）：

1. `DefaultConfigPlugin` 会处理它（资源名以 `.schema` 结尾），注入 `default:menu`/`navigator`/`selector`。但由于方案自带了 `menu/page_size: 7`，依据 `IncludeReference` 的「先包含、后覆盖」，最终 `page_size` 为 **7**（方案自带值优先）。
2. `LegacyPresetConfigPlugin` 把 `punctuator/import_preset: default` 翻译成 `__include: default:punctuator`（`optional=false`），方案得到 `default.yaml` 的 `full_shape`/`half_shape` 两套标点映射。
3. `AutoPatchConfigPlugin` 会自动挂一条指向 `my.custom:/patch?` 的可选依赖（用户没写 `.custom.yaml` 也不报错）；`BuildInfoPlugin` 写入 `__build_info`；`SaveOutputPlugin` 把最终树落盘到 staging。
4. 顺序：**编译期** `ReviewCompileOutput`（仅 `AutoPatchConfigPlugin` 做事，其余返回 `true`）→ 依赖求解 → **链接期** `ReviewLinkOutput`（按 `AutoPatch → Default → LegacyPreset → LegacyDictionary → BuildInfo → SaveOutput` 顺序，其中 `AutoPatch`/`LegacyDictionary` 的 Link 钩子是空操作）。**落盘结果可在 staging 目录核对（待本地验证）**。

---

## 6. 本讲小结

- 配置插件是「编译期后处理钩子」，统一接口 `ConfigCompilerPlugin` 含两个方法：`ReviewCompileOutput`（编译完、依赖求解前）与 `ReviewLinkOutput`（链接完、依赖求解后）。
- 六个内置插件在 `core_module.cc` 按固定顺序挂到 `config_builder`，由 `MultiplePlugins` 串成链、遇 `false` 短路；`SaveOutputPlugin` 必须在最后以保证落盘的是最终树。
- `DefaultConfigPlugin` 给每个 `*.schema` 自动注入 `default` 的 `menu`/`navigator`/`selector` 三段（`optional=true` 容忍缺失），这是「方案没写 `menu` 却有 `page_size`」的根源。
- `LegacyPresetConfigPlugin` 把遗留语法 `import_preset: <preset>` 翻译成 `__include: <preset>:<段>`（`optional=false` 严格失败）；其中 `key_binder` 用 `bindings/+` 追加位保留方案自带绑定。
- 关键语义：`IncludeReference::Resolve` 是「**先整体搬入引用段，再把目标原自带的键合并（覆盖）回去**」，所以方案里显式写出的同名子键不会被 include 抹掉（`recognizer/patterns` 两套共存即此理）。
- `AutoPatchConfigPlugin` 让 `*.custom.yaml` 用户补丁自动生效；`BuildInfoPlugin` 盖版本/时间戳戳；`SaveOutputPlugin` 落盘产物；`LegacyDictionaryConfigPlugin` 暂为空实现。
- `default.yaml` 的各段经由**不同机制**进入运行时：`menu` 走自动注入，`punctuator`/`key_binder`/`recognizer` 走 `import_preset` 翻译，`schema_list`/`switcher` 走切换器，`ascii_composer` 走处理器自取。

---

## 7. 下一步学习建议

本讲把「配置如何被编译并改写」讲到了插件层。接下来的两个方向：

- **向下看运行时**：配置树装配完成后，是如何驱动引擎流水线的？建议进入 u5（组件与模块架构），特别是 u5-l2「四大组件基类」，看 `engine/{processors,segmentors,translators,filters}` 这四张清单如何被 `Engine::ApplySchema` 消费、实例化成流水线（对应 u2-l4 提到的 `CreateComponentsFromList`）。
- **向下看部署**：本讲反复提到「部署期」「staging 产物」「落盘」，这套流程的调度者是谁？建议跳到 u9-l1（Deployer 与 DeploymentTask）和 u9-l2（部署任务族），看 `ConfigBuilder` 是被哪个 `DeploymentTask` 调用、产物写进哪个目录、以及 `__build_info/timestamps` 如何参与「是否需要重建」的判断。

延伸阅读源码：想加深「先包含后覆盖」的合并细节，可精读 [src/rime/config/config_compiler.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.cc) 中 `MergeTree` 与 `IncludeReference::Resolve`、`PatchReference::Resolve` 的完整实现。
