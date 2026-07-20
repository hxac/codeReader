# Schema：输入方案

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 **Schema（输入方案）** 在 librime 中扮演的角色，以及它为什么是「换方案 = 换输入法，引擎不变」的关键。
- 理解 `Schema` 这个类本质上就是 `schema_id + Config` 的组合，并知道它额外缓存了哪些「高频配置项」。
- 看懂 `Schema` 的三种构造方式，以及 `new Schema("luna_pinyin")` 这一行背后是如何把 `luna_pinyin.schema.yaml` 读进来的。
- 读懂一个真实的方案文件 `*.schema.yaml` 的顶层结构（`schema` / `switches` / `engine` / `speller` / `translator` 等）。

本讲是 [u2-l2](u2-l2-service-and-session.md) 的直接延续：上一讲我们知道了 Session 独占一个 Engine，而 Engine 又持有一个 Schema。这一讲我们就把这个「Schema 到底是什么」补齐。

## 2. 前置知识

在进入源码前，先用大白话建立两个直觉。

**1）「方案」是 librime 的灵魂。**
RIME 之所以叫「中州韵输入法引擎」，重点在「引擎」二字：它本身不是某一种具体的输入法（拼音、双拼、仓颉、五笔……），而是一台**通用机器**。真正决定「我现在打的是拼音还是仓颉」的，是一份叫做**输入方案（schema）**的配置。换一份方案，同一套引擎就能变成完全不同的输入法。这一点在 [u1-l1](u1-l1-project-overview.md) 里已经提过。

**2）方案 = 一份 YAML 文件 + 内存里的一棵配置树。**
磁盘上，一个方案就是一个 `*.schema.yaml` 文件，比如 `luna_pinyin.schema.yaml`。运行时，librime 把这份 YAML 读进来，解析成一棵内存里的配置树（`Config`），再用一个 `Schema` 对象把这棵树的「句柄」和方案的身份证号 `schema_id` 一起封装起来。

> 术语提示：`Config` 是 librime 的配置数据模型（一棵由 `ConfigItem`/`ConfigValue`/`ConfigList`/`ConfigMap` 组成的树），它的细节属于配置系统单元（u4）。本讲你只需要把 `Config` 当成「一份能按路径取值的大字典」来用，例如 `config->GetInt("menu/page_size", &n)`。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [src/rime/schema.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/schema.h) | 定义 `Schema` 类与 `SchemaComponent`，是本讲的主角。 |
| [src/rime/schema.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/schema.cc) | `Schema` 的构造、`FetchUsefulConfigItems` 预提取、`SchemaComponent::Create` 的实现。 |
| [src/rime/core_module.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/core_module.cc) | 把 `"schema"` 这个名字注册成一个组件，是连接 `Schema` 与组件体系的桥梁。 |
| [data/minimal/luna_pinyin.schema.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml) | 一份真实的、最小可运行的拼音方案文件，本讲反复对照它。 |
| [data/minimal/default.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/default.yaml) | 全局默认配置，`menu/page_size` 等公共配置就来自这里。 |

辅助理解（非本讲重点，仅引用一两处）：

| 文件 | 作用 |
| --- | --- |
| [src/rime/config/config_component.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_component.h) | `Config` 与 `Config::Component` 的定义，`SchemaComponent` 就继承自它。 |
| [src/rime/engine.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc) | Engine 如何持有并消费 Schema。 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **Schema 是什么**：`schema_id + Config` 的组合。
2. **Schema 的构造与加载路径**：三种构造函数、`SchemaComponent`、以及 `"schema"` 组件的注册。
3. **FetchUsefulConfigItems**：为什么要把几个高频配置项「提前抄出来」。
4. **方案文件 `*.schema.yaml` 的结构**：用 `luna_pinyin.schema.yaml` 走一遍顶层布局。

### 4.1 Schema 是什么：schema_id + Config 的组合

#### 4.1.1 概念说明

`Schema` 是一个非常轻量的类。它的全部职责可以概括成一句话：

> **把「这个方案叫什么」(`schema_id`) 和「这个方案的配置内容」(`Config`) 打包在一起，并顺手缓存几个用得最多的配置项。**

注意它是**组合（has-a）**而不是继承：`Schema` 内部持有一个 `the<Config> config_`（`the<T>` 是 librime 对 `std::unique_ptr<T>` 的别名，见 [u1-l3](u1-l3-source-layout.md) 里对 `common.h` 的介绍），而不是继承自 `Config`。这是一个重要的设计选择——我们在练习里会讨论为什么。

#### 4.1.2 核心流程

`Schema` 对象对外暴露的「读」接口可以分成三组：

```
身份信息：   schema_id()   schema_name()
配置树句柄： config()
高频缓存项： page_size()   page_down_cycle()   select_keys()
```

- 身份信息：`schema_id` 是方案在系统里的唯一身份证号（如 `luna_pinyin`）；`schema_name` 是给人看的名字（如「朙月拼音」）。
- 配置树句柄：`config()` 返回内部的 `Config*`，引擎流水线后续要读 `engine/processors`、`speller/algebra` 等等，都是通过这个句柄。
- 高频缓存项：候选菜单的每页大小 `page_size`、翻页是否循环 `page_down_cycle`、选词键 `select_keys`。这三项在每次画候选菜单时几乎都要用，所以构造时就被「抄」进了成员变量，免得每次都去配置树里翻。

#### 4.1.3 源码精读

`Schema` 类的全部声明如下，注意它的成员构成：

```cpp
class Schema {
 public:
  Schema();
  explicit Schema(const string& schema_id);
  Schema(const string& schema_id, Config* config)
      : schema_id_(schema_id), config_(config) {}
  // ... 各种 getter / setter ...
 private:
  void FetchUsefulConfigItems();
  string schema_id_;
  string schema_name_;
  the<Config> config_;
  // frequently used config items
  int page_size_ = 5;
  bool page_down_cycle_ = false;
  string select_keys_;
};
```

参见 [src/rime/schema.h:15-43](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/schema.h#L15-L43)（这段定义了 `Schema` 类；`the<Config> config_` 表明它独占一棵配置树，`page_size_ = 5` 等是带默认值的高频缓存项）。

几个 getter，可以看到「只读」姿态：

参见 [src/rime/schema.h:22-31](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/schema.h#L22-L31)（`schema_id()`/`schema_name()`/`config()`/`page_size()`/`page_down_cycle()`/`select_keys()` 一组只读访问器，外加 `set_config`/`set_select_keys` 两个可写入口）。

#### 4.1.4 代码实践

**实践目标**：通过阅读源码，确认 `Schema` 持有哪些成员、各自的作用域。

**操作步骤**：

1. 打开 [src/rime/schema.h:15-43](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/schema.h#L15-L43)。
2. 把 `private:` 以下的 6 个成员抄到一张表里，标注：类型、默认值、由谁写入（构造函数 还是 `FetchUsefulConfigItems`）。
3. 数一下对外暴露了多少个 `const string&` 返回的 getter——它们返回的是引用而不是副本，思考这样做的性能含义。

**需要观察的现象 / 预期结果**：你会发现 `schema_id_`、`schema_name_`、`config_` 三个是「核心三件套」，而 `page_size_`、`page_down_cycle_`、`select_keys_` 是「缓存三件套」。整个类没有任何「业务逻辑」字段（没有候选、没有按键），它就是一个**数据载体**。

#### 4.1.5 小练习与答案

**练习 1**：`Schema` 为什么选择「组合」（内部持有 `the<Config>`）而不是「继承」（`class Schema : public Config`）？

> **参考答案**：因为 `Config` 是一棵**通用的配置树**，它会被方案、`default.yaml`、用户配置（`user_config`）等很多场景复用（见 [u4 配置系统](u4-l1-config-data-model.md)）。`Schema` 只是想「用」一棵 Config，并不想成为一棵 Config。组合让 `Schema` 保持单一职责（「我代表一个输入方案」），同时可以随时换掉内部那棵 Config（`set_config`），耦合更低。

**练习 2**：`page_size_` 的默认值是多少？为什么需要给默认值，而不是读不到就报错？

> **参考答案**：默认值是 `5`（见 `int page_size_ = 5;`）。因为「每页候选数」不是每个方案都必须显式写的——很多方案依赖 `default.yaml` 里的全局默认。读不到时给一个合理默认值，可以让一份最小的方案文件也能正常跑起来。

---

### 4.2 Schema 的构造与加载路径

#### 4.2.1 概念说明

光知道 `Schema` 是 `id + Config` 还不够，关键是搞清楚：**当我写下 `new Schema("luna_pinyin")` 时，那棵 `Config` 是从哪里来的？** 这一节就是回答这个问题。

答案牵涉到三个角色：

- **`Schema` 的构造函数**：根据 `schema_id` 的形态，决定走哪条加载通道。
- **`SchemaComponent`**：一个「适配器」，负责把「方案名」翻译成「文件名」。
- **`Config::Component`**：真正去磁盘读 YAML、做缓存的那个组件工厂（下一节 u4 会细讲）。

> 术语提示：「组件（Component）」是 librime 的核心扩展机制，[u5-l1](u5-l1-component-registry.md) 会系统讲。这里你只要知道：组件是按「名字」注册到一张全局表（Registry）里、用 `Class<T,Arg>::Require(name)` 按名取出并 `Create(arg)` 的工厂对象。

#### 4.2.2 核心流程

`Schema` 有三个构造函数，对应三种「获得一棵 Config」的方式：

```
Schema()                      -> schema_id = ".default"
                                 通过 "config" 组件加载 "default"
Schema(schema_id)             -> 若 schema_id 以 "." 开头：
                                    去掉 "."，走 "config" 组件   （内部方案，如 .default）
                                 否则：
                                    走 "schema" 组件             （普通方案，如 luna_pinyin）
Schema(schema_id, config)     -> 直接用调用方给的 config，不自己加载
```

其中 `"schema"` 组件的 `Create(id)` 会自动把 `id` 拼成 `id + ".schema"`，再去对应的资源目录找 `luna_pinyin.schema.yaml` 这样的文件。

加载完成（或拿到现成 config）后，三个构造函数最后都调用同一个 `FetchUsefulConfigItems()` 把高频项抄出来（见 4.3）。

#### 4.2.3 源码精读

先看 `Schema::Schema()`——默认构造，会加载全局的 `default` 配置：

```cpp
Schema::Schema() : schema_id_(".default") {
  config_.reset(Config::Require("config")->Create("default"));
  FetchUsefulConfigItems();
}
```

参见 [src/rime/schema.cc:12-15](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/schema.cc#L12-L15)（默认构造把 `schema_id_` 设成特殊值 `".default"`，并用 `"config"` 组件加载名为 `default` 的配置——也就是 `default.yaml`）。

再看带 `schema_id` 的构造函数，注意它根据「是否以 `.` 开头」分了两条路：

```cpp
Schema::Schema(const string& schema_id) : schema_id_(schema_id) {
  config_.reset(boost::starts_with(schema_id_, ".")
                    ? Config::Require("config")->Create(schema_id.substr(1))
                    : Config::Require("schema")->Create(schema_id));
  FetchUsefulConfigItems();
}
```

参见 [src/rime/schema.cc:17-22](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/schema.cc#L17-L22)（以 `.` 开头的 `schema_id` 是「内部方案」，去掉点后走 `config` 组件；普通方案走 `schema` 组件）。

那么 `"schema"` 组件又是什么？它由 `SchemaComponent` 实现，关键就是 `Create` 里那行 `schema_id + ".schema"`：

```cpp
Config* SchemaComponent::Create(const string& schema_id) {
  return config_component_->Create(schema_id + ".schema");
}
```

参见 [src/rime/schema.cc:40-42](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/schema.cc#L40-L42)（`SchemaComponent` 把方案名 `luna_pinyin` 拼成资源名 `luna_pinyin.schema`，再交给底层 `config_component_` 去找文件）。`SchemaComponent` 本身的声明见 [src/rime/schema.h:45-56](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/schema.h#L45-L56)——它继承自 `Config::Component`，内部持有一个**不拥有所有权**的 `config_component_` 指针。

最后看注册：`"schema"` 这个名字和 `"config"` 这个名字是在 `core_module.cc` 里一起注册的，而且 **`SchemaComponent` 复用了同一个 `config_loader`**：

```cpp
auto config_loader =
    new ConfigComponent<ConfigLoader, DeployedConfigResourceProvider>;
r.Register("config", config_loader);
r.Register("schema", new SchemaComponent(config_loader));
```

参见 [src/rime/core_module.cc:34-37](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/core_module.cc#L34-L37)（`"config"` 是直接读 YAML 的加载器；`"schema"` 包了一层 `SchemaComponent`，专门负责把方案名转成 `.schema.yaml` 文件名，底层复用同一个加载器）。这正对应本讲规格里强调的「`SchemaComponent` 如何复用 `Config::Component` 生产 `Config`」。

> 旁证：这两个名字在被使用时，确实就是通过 `Config::Require("config")` / `Config::Require("schema")` 取出的——回看 [src/rime/schema.cc:13-20](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/schema.cc#L13-L20) 即可印证。

#### 4.2.4 代码实践

**实践目标**：把 `new Schema("luna_pinyin")` 这一行背后的调用链画出来。

**操作步骤**：

1. 在 [src/rime/schema.cc:17-22](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/schema.cc#L17-L22) 找到分支判断，确认 `luna_pinyin` 不以 `.` 开头，走 `Config::Require("schema")`。
2. 在 [src/rime/core_module.cc:37](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/core_module.cc#L37) 确认 `"schema"` 指向一个 `SchemaComponent`。
3. 在 [src/rime/schema.cc:40-42](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/schema.cc#L40-L42) 看到 `Create` 把 `luna_pinyin` 拼成 `luna_pinyin.schema`，再交给 `config_loader`。

**需要观察的现象 / 预期结果**：你应该能画出这样一条链：

```
new Schema("luna_pinyin")
  -> Config::Require("schema")->Create("luna_pinyin")
     -> SchemaComponent::Create("luna_pinyin")
        -> config_component_->Create("luna_pinyin.schema")   // 加了 ".schema"
           -> 在资源目录里找 luna_pinyin.schema.yaml 并解析成 ConfigData
```

至于 `luna_pinyin.schema.yaml` 是从哪个磁盘目录被找到的，由 `ResourceResolver` 决定（属于 u4 配置系统的范畴，本讲标记为「待确认具体目录」，可在 u4-l2 继续追踪）。

#### 4.2.5 小练习与答案

**练习 1**：`schema_id` 以 `.` 开头（如 `.default`）时走的是哪个组件？为什么要单独区分？

> **参考答案**：走的是 `"config"` 组件（去掉前导 `.` 后直接当作配置名加载，例如 `.default` → 加载 `default`，对应 `default.yaml`）。原因是 `default.yaml` 这种「全局默认」并不是某个输入方案，它没有 `.schema.yaml` 后缀，不应该被 `SchemaComponent` 加上 `.schema`。用 `.` 前缀标记这类「内部/伪方案」，就能复用同一套加载器逻辑而不误加后缀。

**练习 2**：`SchemaComponent::Create` 为什么要做 `schema_id + ".schema"` 这步拼接，而不是直接用 `schema_id`？

> **参考答案**：因为底层 `config_component_` 是按「资源名 / 文件名」来查找的，而方案文件在磁盘上叫 `luna_pinyin.schema.yaml`。`SchemaComponent` 的职责正是把「方案逻辑名」(`luna_pinyin`) 翻译成「资源文件名」(`luna_pinyin.schema`)，从而让上层只关心方案名，不必知道文件命名约定。这也是一种命名空间的隔离：方案配置和普通配置共享同一套加载器，靠 `.schema` 后缀区分。

---

### 4.3 FetchUsefulConfigItems：预提取高频配置项

#### 4.3.1 概念说明

`Schema` 在构造完成后，会立刻做一件叫 `FetchUsefulConfigItems`（「抓取有用的配置项」）的事：把几个用得最频繁的配置值，从配置树里**读一次**，存到成员变量里。

为什么要这么做？因为画候选菜单这件事发生得**非常频繁**（每按一个键都可能刷新一次菜单），而 `menu/page_size`、选词键这种值几乎每次画菜单都要用。如果每次都从 `Config` 树里按路径查找，会有重复开销。所以构造时抄一次、之后直接读成员变量，是典型的「空间换时间」缓存。

#### 4.3.2 核心流程

```
FetchUsefulConfigItems():
  if 没有 config_:
      schema_name_ = schema_id_ + "?"        # 一个带问号的占位名，提示加载失败
      return
  从 "schema/name"           读 schema_name_  # 取不到就用 schema_id_ 兜底
  从 "menu/page_size"        读 page_size_    # 取不到或 < 1，强制改回 5
  从 "menu/alternative_select_keys" 读 select_keys_
  从 "menu/page_down_cycle"  读 page_down_cycle_
```

注意三条「兜底」规则：

- `schema_name` 读不到 → 用 `schema_id` 当名字。
- `page_size` 读不到或小于 1 → 强制回到默认 5。
- `select_keys` / `page_down_cycle` 读不到 → 保持 C++ 成员的初值（空串 / `false`）。

#### 4.3.3 源码精读

整个函数只有十几行，把上述流程一一对应：

```cpp
void Schema::FetchUsefulConfigItems() {
  if (!config_) {
    schema_name_ = schema_id_ + "?";
    return;
  }
  if (!config_->GetString("schema/name", &schema_name_)) {
    schema_name_ = schema_id_;
  }
  config_->GetInt("menu/page_size", &page_size_);
  if (page_size_ < 1) {
    page_size_ = 5;
  }
  config_->GetString("menu/alternative_select_keys", &select_keys_);
  config_->GetBool("menu/page_down_cycle", &page_down_cycle_);
}
```

参见 [src/rime/schema.cc:24-38](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/schema.cc#L24-L38)（这段就是预提取逻辑：先处理 `config_` 为空的异常情况；再依次读 `schema/name`、`menu/page_size`、`menu/alternative_select_keys`、`menu/page_down_cycle`，并对缺失或非法值做兜底）。

那么这些路径上的值来自哪里？`menu/page_size` 这种「公共菜单设置」通常来自全局的 `default.yaml`：

```yaml
menu:
  page_size: 5
```

参见 [data/minimal/default.yaml:25-26](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/default.yaml#L25-L26)（`default.yaml` 的 `menu` 段提供了 `page_size: 5` 这个全局默认；而 `alternative_select_keys`、`page_down_cycle` 这两项在最小数据集里没有给出，因此保持 C++ 初值——这也解释了为什么 `FetchUsefulConfigItems` 要给它们兜底）。

> 注意：`menu/page_size` 在 `luna_pinyin.schema.yaml` 里并没有写，它是通过配置系统的 `__include` / `import_preset` 机制从 `default.yaml` 合并进来的（见 [u4-l4](u4-l4-config-plugins-default.md)）。本讲你只需知道：`Schema` 最终拿到的是**合并后**的配置树，`FetchUsefulConfigItems` 读的是合并结果。

#### 4.3.4 代码实践

**实践目标**：对照真实数据，确认 `FetchUsefulConfigItems` 读到的四个值分别从哪来。

**操作步骤**：

1. 打开 [data/minimal/luna_pinyin.schema.yaml:4-16](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L4-L16)，找到 `schema/name` 的值（应该是「朙月拼音」）。
2. 在同文件里搜索 `menu`——你会发现**没有** `menu` 段。
3. 打开 [data/minimal/default.yaml:25-26](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/default.yaml#L25-L26)，确认 `page_size: 5` 在那里。
4. 把结果填进下表（第一行已示范）：

   | 配置路径 | 值 | 来源文件 |
   | --- | --- | --- |
   | `schema/name` | 朙月拼音 | `luna_pinyin.schema.yaml` |
   | `menu/page_size` | 5 | `default.yaml`（合并进来） |
   | `menu/alternative_select_keys` | （待本地验证：最小数据集未给出，应为空串） | — |
   | `menu/page_down_cycle` | （待本地验证：最小数据集未给出，应为 `false`） | — |

**需要观察的现象 / 预期结果**：你会直观地体会到——**一份方案文件的配置并不全写在自己身上**，很多公共项是从 `default.yaml` 合并来的。这正是 `FetchUsefulConfigItems` 要做兜底的原因：它无法假设这些项一定存在。

#### 4.3.5 小练习与答案

**练习 1**：如果某个方案把 `menu/page_size` 写成了 `0`，运行时每页会显示几个候选？

> **参考答案**：会显示 5 个。因为 [src/rime/schema.cc:33-35](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/schema.cc#L33-L35) 里有 `if (page_size_ < 1) { page_size_ = 5; }`，任何小于 1 的值都会被强制改回默认 5。这是一种防御式编程，避免无效配置导致菜单分页逻辑崩溃。

**练习 2**：什么情况下 `schema_name()` 会返回形如 `luna_pinyin?` 这样带问号的字符串？

> **参考答案**：当 `config_` 为空时（即方案配置根本没加载成功），`FetchUsefulConfigItems` 会执行 `schema_name_ = schema_id_ + "?"`，返回类似 `luna_pinyin?` 的占位名，用一个显眼的问号提示「这个方案加载失败了」。这是 librime 在「找不到/读不出方案」时给的可见信号。

---

### 4.4 方案文件 *.schema.yaml 的结构

#### 4.4.1 概念说明

前面三节讲的是「`Schema` 对象在内存里长什么样」，这一节反过来：**磁盘上的方案文件长什么样**。我们用最小数据集里的真实方案 `luna_pinyin.schema.yaml`（朙月拼音）作为样本。

方案文件就是一个普通 YAML，它的顶层键大致分两类：

- **元信息**：`schema`（身份证号、名字、版本、作者、描述）。
- **行为配置**：`switches`（开关）、`engine`（流水线装配）、`speller`（拼写）、`translator`（翻译器），以及若干给具体组件用的命名空间段（如 `pinyin`、`cangjie`、`zh_simp`）。

> 提示：`engine` 下那四组列表（processors/segmentors/translators/filters）是引擎流水线的「装配清单」，它们会在 [u6 按键处理流水线](u6-l1-pipeline-overview.md) 详细展开。本节你只要知道「方案里写了这些清单，引擎会照着装配」即可。

#### 4.4.2 核心流程

一个方案文件被 `Schema` 消费的顺序，可以粗略描述为：

```
*.schema.yaml  --(SchemaComponent + ConfigLoader)-->  Config 树
                                                              |
                       FetchUsefulConfigItems 读 schema/name、menu/* 等少量项
                                                              |
                       Engine::ApplySchema(schema) 接管：
                           - 读 engine/processors 等四张清单 -> 装配流水线组件
                           - 读 switches                      -> 初始化开关
                           - 读 speller/algebra               -> 喂给拼写代数
                           - 读 translator/dictionary         -> 找到对应词典
```

也就是说：`Schema` 自己只「预消费」很少几个键（菜单相关），**剩下绝大部分键是引擎和各组件按需读取的**。`Schema` 更像是一个「配置树的保管员」。

#### 4.4.3 源码精读

**元信息段 `schema`**——`FetchUsefulConfigItems` 读的 `schema/name` 就在这里：

```yaml
schema:
  schema_id: luna_pinyin
  name: 朙月拼音
  version: "0.15.test"
  author:
    - 佛振 <chen.sst@gmail.com>
  description: |
    Rime 預設的拼音輸入方案。
    ...
```

参见 [data/minimal/luna_pinyin.schema.yaml:4-16](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L4-L16)（`schema_id: luna_pinyin` 是身份证号，要和文件名 `luna_pinyin.schema.yaml` 的前缀一致；`name: 朙月拼音` 就是被 `FetchUsefulConfigItems` 抄进 `schema_name_` 的那个值）。

**开关段 `switches`**——定义中/英、半/全角、繁简等开关（[u9-l4](u9-l4-switcher-switches.md) 详讲）：

参见 [data/minimal/luna_pinyin.schema.yaml:18-37](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L18-L37)（注意有两种形态：带 `name:` 的「开关」（如 `ascii_mode`）和带 `options:` 的「单选组」（如 `zh_trad/zh_simp/zh_hk/zh_tw`））。

**核心装配段 `engine`**——四类组件清单：

```yaml
engine:
  processors:   [ ascii_composer, recognizer, key_binder, speller, ... ]
  segmentors:   [ ascii_segmentor, matcher, abc_segmentor, ... ]
  translators:  [ punct_translator, reverse_lookup_translator, script_translator, ... ]
  filters:      [ reverse_lookup_filter@cangjie_lookup, simplifier@zh_simp, ... ]
```

参见 [data/minimal/luna_pinyin.schema.yaml:39-68](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L39-L68)（引擎在 `Engine::ApplySchema` 时会照着这四张清单逐个 `Require` 并 `Create` 出组件实例——这正是 [u6-l1](u6-l1-pipeline-overview.md) 要讲的流水线装配过程）。

为印证「引擎确实通过 `schema->config()` 读这四张清单」，可以看 `engine.cc` 里的装配助手，它正是用 `config->GetList(config_key)` 去读 `engine/processors` 之类路径的：

参见 [src/rime/engine.cc:296-326](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L296-L326)（`CreateComponentsFromList` 接收 `config` 和一个 `config_key`（如 `"engine/processors"`），把列表里每一项当 `Ticket` 去实例化一个流水线组件）。

**拼写与翻译段**——`speller` 定义怎么切音节、`translator` 定义用哪本词典：

参见 [data/minimal/luna_pinyin.schema.yaml:70-91](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L70-L91)（`speller/algebra` 是拼写代数规则列表，[u7](u7-l2-calculus.md) 详讲；`translator/dictionary: luna_pinyin` 指向名为 `luna_pinyin` 的词典，[u8](u8-l1-dict-overview.md) 详讲）。这两段虽不是 `Schema` 自己预提取的，但它们决定了方案的「输入行为」，是方案文件最重要的部分之一。

#### 4.4.4 代码实践

**实践目标**（即本讲的指定实践任务）：阅读 `luna_pinyin.schema.yaml`，找出 `schema / switches / engine / speller / translator` 几个顶层键，并对照 `Schema::FetchUsefulConfigItems` 说明哪些配置会被预提取。

**操作步骤**：

1. 打开 [data/minimal/luna_pinyin.schema.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml)。
2. 列出所有顶层键：`schema`、`switches`、`engine`、`speller`、`translator`、`alphabet`、`cangjie`、`pinyin`、`cangjie_lookup`、`reverse_lookup`、`zh_simp`、`zh_tw`、`punctuator`、`key_binder`、`recognizer`。
3. 对照 [src/rime/schema.cc:24-38](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/schema.cc#L24-L38) 的 `FetchUsefulConfigItems`，圈出**会被预提取**的键。

**需要观察的现象 / 预期结果**：你会得到这样一张「预提取清单」：

| `FetchUsefulConfigItems` 读取的路径 | 在 `luna_pinyin.schema.yaml` 里是否直接给出 |
| --- | --- |
| `schema/name`（→ `schema_name_`） | ✅ 是，`name: 朙月拼音` |
| `menu/page_size`（→ `page_size_`） | ❌ 否，来自合并后的 `default.yaml` |
| `menu/alternative_select_keys`（→ `select_keys_`） | ❌ 否，保持空串 |
| `menu/page_down_cycle`（→ `page_down_cycle_`） | ❌ 否，保持 `false` |

也就是说，**方案文件有十几个顶层键，但 `Schema` 自己只预提取其中 `schema/name` 和（合并进来的）`menu/*` 这一小撮**；其余像 `engine`、`speller`、`translator` 这些大段配置，`Schema` 一个都不碰，统统交给引擎和各组件按需读取。这再次印证了 4.4.2 的结论：`Schema` 是「保管员」，不是「消费者」。

#### 4.4.5 小练习与答案

**练习 1**：`engine` 配置下有哪四类组件列表？它们分别对应引擎流水线的哪个阶段？

> **参考答案**：四类是 `processors`（按键处理，最先运行，决定按键是被吃掉还是放行）、`segmentors`（把输入串切成带 tag 的片段）、`translators`（把片段翻译成候选）、`filters`（对候选做过滤/转换，如繁简转换、去重）。这正是 [u6](u6-l1-pipeline-overview.md) 要讲的 Processor → Segmentor → Translator → Filter 流水线的四个阶段。

**练习 2**：`speller/algebra` 和 `translator/dictionary` 这两段配置，分别服务于什么？

> **参考答案**：`speller/algebra` 是一组「拼写代数」规则（如 `derive`、`abbrev`、`erase`），用来从基本拼写派生出模糊音、缩写等变体，供切分音节时使用（详见 [u7 拼写代数](u7-l2-calculus.md)）。`translator/dictionary` 则指明这个方案用哪本词典（如 `dictionary: luna_pinyin` 对应 `luna_pinyin.dict.yaml` 编译出的码表，详见 [u8 词典系统](u8-l1-dict-overview.md)）。简言之：前者管「输入串怎么理解成音节」，后者管「音节去哪本字典里查词」。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「跟踪一个方案从磁盘到内存」的小任务。

**任务**：假设用户在切换器里选中了 `luna_pinyin` 方案。请按时间顺序，把下列事实串成一个完整的故事：

1. 切换器调用 `new Schema("luna_pinyin")`（可参考 [src/rime/switcher.cc:174-196](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switcher.cc#L174-L196) 里 `Switcher::CreateSchema` 末尾的 `return new Schema(recent);`）。
2. 构造函数判断 `luna_pinyin` 不以 `.` 开头，于是走 `Config::Require("schema")`。
3. `"schema"` 组件即 `SchemaComponent`，把名字拼成 `luna_pinyin.schema`，复用底层 `config_loader` 读到 `luna_pinyin.schema.yaml`。
4. YAML 被解析成一棵 `Config` 树，并与 `default.yaml` 合并（于是 `menu/page_size` 等公共项也在其中）。
5. `FetchUsefulConfigItems` 抄出 `schema_name_=朙月拼音`、`page_size_=5`、其余缓存项保持默认。
6. 这个 `Schema` 被交给 `Engine::ApplySchema`（[src/rime/engine.cc:284-294](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L284-L294)），引擎读 `engine/processors` 等四张清单，装配出流水线。

**交付物**：画一张时序图或一张「数据流图」，标清每一步涉及的文件（schema.h / schema.cc / core_module.cc / luna_pinyin.schema.yaml / default.yaml / engine.cc）。如果在图中你能正确标出「`menu/page_size` 这一项第一次出现是在 `default.yaml`，最终被 `Schema::page_size_` 缓存」，就说明你真的把本讲吃透了。

**进阶（可选）**：仿照 `luna_pinyin.schema.yaml`，写一份最小自定义方案（**示例代码**，仅作练习，不要放进真实数据目录）：

```yaml
# 示例代码：my_test.schema.yaml —— 仅用于理解结构，并非项目原有文件
schema:
  schema_id: my_test
  name: 练习方案
engine:
  processors:
    - speller
    - express_editor
  segmentors:
    - abc_segmentor
    - fallback_segmentor
  translators: []
  filters: []
speller:
  alphabet: abcdefghijklmnopqrstuvwxyz
  delimiter: " "
```

对照本讲内容自查：这份示例里 `schema/name` 是什么？`menu/page_size` 没写时会取多少？`engine` 下四张清单分别有多少项？（运行验证「待本地验证」。）

## 6. 本讲小结

- `Schema` 是一个轻量数据载体，本质是 **`schema_id` + `Config`（配置树句柄）** 的组合，外加几个高频缓存项。
- `page_size_`（默认 5）、`page_down_cycle_`（默认 `false`）、`select_keys_`（默认空串）是构造时就被 `FetchUsefulConfigItems` 预提取的缓存项，用于频繁的菜单渲染。
- 三个构造函数覆盖三种来源：默认（`.default`）、按方案名（走 `"schema"` 组件）、外部直接给 Config。
- `"schema"` 组件 = `SchemaComponent`，它复用底层 `config_loader`，唯一职责是把方案名 `luna_pinyin` 拼成资源名 `luna_pinyin.schema`（对应磁盘上的 `luna_pinyin.schema.yaml`）。
- 一份方案文件的顶层键虽多（`schema`/`switches`/`engine`/`speller`/`translator`/…），但 `Schema` 自己只预消费 `schema/name` 和合并进来的 `menu/*`；其余大段配置交给引擎与各组件按需读取。
- `Schema` 被 `Engine::ApplySchema` 接管后，引擎读 `engine/{processors,segmentors,translators,filters}` 四张清单装配流水线——这就把本讲和下一讲 [u2-l4](u2-l4-engine-skeleton.md) 衔接起来了。

## 7. 下一步学习建议

- 下一篇 [u2-l4 引擎骨架](u2-l4-engine-skeleton.md)：看 `Engine` 如何持有 `Schema` 与 `Context`，以及 `Engine::ApplySchema` 的完整实现——这是本讲 4.4 提到的「装配流水线」的真正入口。
- 想搞清 `Config` 这棵树本身的结构和 `Config::Component` 工厂机制，跳到 [u4 配置系统](u4-l1-config-data-model.md)（数据模型）与 [u4-l2](u4-l2-config-component-loading.md)（加载与资源解析）。
- 想了解 `"schema"` 名字背后的组件注册体系，看 [u5-l1 组件体系](u5-l1-component-registry.md) 与 [u5-l3 模块机制](u5-l3-module-system.md)。
- 想看方案文件里的 `speller/algebra`、`translator/dictionary` 如何被真正使用，分别前往 [u7 拼写代数](u7-l2-calculus.md) 和 [u8 词典系统](u8-l1-dict-overview.md)。
