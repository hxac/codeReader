# Config::Component 与配置加载

## 1. 本讲目标

上一篇（u4-l1）我们看清了配置在内存里是一棵 `ConfigItem` 多态树：标量（`ConfigValue`）、列表（`ConfigList`）、映射（`ConfigMap`）由统一基类 `ConfigItem` 串起来。但那棵树是凭空长出来的吗？它**从哪里来**、**什么时候建**、**建好之后存在哪**、**多次取同一份配置会不会重复解析**？本讲就回答这些「加载链路」问题。

学完本讲你应当能够：

1. 说清楚一次 `config_open(id)` 调用从 C API 到 YAML 文件、再到内存 `ConfigItem` 树的完整数据流。
2. 理解 `Config::Component` 作为**组件工厂**的角色：它把「按名字取配置」这件事变成组件注册表里的一个槽位。
3. 掌握 `ConfigComponentBase` 用 `weak_ptr` 缓存 `ConfigData` 的共享机制，以及为什么「同一 config id 多次打开会共享同一份配置数据」。
4. 区分 `ConfigLoader`（直接读 YAML）与 `ConfigBuilder`（带编译插件，支持 `__include`/`__patch` DSL）两条加载路径。
5. 理解 `ResourceResolver` 如何把一个抽象的 `config_id`（如 `"default"`）解析成磁盘上的具体文件路径（如 `staging_dir/default.yaml`）。

## 2. 前置知识

在进入源码前，先用三段大白话建立直觉。这些概念在 u4-l1、u2-l3、u5 的预备讲义里已部分出现，这里只做承上启下的最小重述。

### 2.1 组件（Component）回顾

librime 的所有可插拔能力都以「组件」形式存在。一个组件就是一个**工厂对象**，实现 `Create(arg)` 接口，由全局 `Registry` 单例按名字登记。调用方拿到名字后，用 `Class<T, Arg>::Require(name)` 查表得到工厂指针，再 `Create(arg)` 产出具体对象。

```cpp
// 简化自 src/rime/component.h
template <class T, class Arg>
struct Class {
  using Initializer = Arg;
  class Component { virtual T* Create(Initializer arg) = 0; };
  static Component* Require(const string& name) {
    return dynamic_cast<Component*>(Registry::instance().Find(name));
  }
};
```

配置体系正是这种模式的一个实例：`Config` 继承自 `Class<Config, const string&>`，所以「配置」也是一种组件，其 `arg` 就是要打开的配置名（`config_id`）。这部分会在 u5-l1 系统讲解，本讲只需知道「配置工厂也是组件」即可。

### 2.2 为什么要「资源解析」

YAML 文件分散在多个目录（共享数据目录、用户数据目录、部署暂存目录、预构建目录等）。同一份逻辑配置（比如默认方案配置 `"default"`），在不同运行阶段可能位于不同磁盘路径。直接在代码里写死路径既不灵活也难维护。于是 librime 引入 `ResourceResolver`：给它一个抽象的 `config_id`，它结合当前的目录布局算出具体文件路径。这样上层只关心「我要 `default` 这份配置」，目录细节交给解析器。

### 2.3 为什么要「缓存」

一份输入方案的配置（尤其是 `luna_pinyin.schema.yaml`）可能包含几百行、上千节点。如果每次 `Config::Require("schema")->Create(...)` 都重新解析 YAML，引擎在频繁切换方案时会做大量重复 IO 与解析。于是 librime 用一份 `weak_ptr` 缓存：解析过的 `ConfigData` 暂存在组件里，只要还有人持有它（比如某个 `Schema` 对象活着），下次再取就直接复用；没人持有时缓存自动失效、下次重新解析。

## 3. 本讲源码地图

| 文件 | 职责 | 本讲中的角色 |
|------|------|------------|
| [src/rime/resource.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/resource.h) | 定义 `ResourceType`、`ResourceResolver`、`FallbackResourceResolver` | 把 `config_id` → 文件路径 |
| [src/rime/resource.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/resource.cc) | 解析器的具体实现（`ToResourceId`/`ToFilePath`/`ResolvePath`） | 路径拼装与回退逻辑 |
| [src/rime/config/config_data.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_data.h) | `ConfigData` 类声明：配置树载体 | 持有 `root`、负责加载/保存 |
| [src/rime/config/config_data.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_data.cc) | `LoadFromFile` 与 `ConvertFromYaml` | YAML → `ConfigItem` 树 |
| [src/rime/config/config_component.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_component.h) | `ConfigComponentBase`、`ConfigComponent<>`、`ConfigLoader`、`ConfigBuilder` | 工厂 + 缓存 + 两条加载路径 |
| [src/rime/config/config_component.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_component.cc) | 上述类的实现 | `GetConfigData` 的缓存逻辑 |
| [src/rime/core_module.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/core_module.cc) | 注册 `config`/`config_builder`/`schema`/`user_config` 组件 | 把工厂登记进 `Registry` |
| [src/rime/service.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.cc) | 三种 `Create*ResourceResolver` | 绑定根目录与回退目录 |
| [src/rime_api_impl.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h) | C API `config_open` 实现 | 把 C 层入口接到组件工厂 |

> 智能指针别名速查（来自 [src/rime/common.h:58-64](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/common.h#L58-L64)）：`the<T>` = `unique_ptr<T>`、`an<T>` = `shared_ptr<T>`、`of<T>` 等同 `an<T>`、`weak<T>` = `weak_ptr<T>`，`New<T>(...)` 等同 `std::make_shared`。本讲频繁出现，务必记住。

---

## 4. 核心概念与源码讲解

### 4.1 资源解析：ResourceResolver 与 ResourceType

#### 4.1.1 概念说明

「配置」在 librime 里有两层身份：一个**逻辑名**（`config_id`，比如 `"default"`、`"luna_pinyin"`）和一份**磁盘文件**（比如 `default.yaml`）。两者之间的桥就是 `ResourceResolver`。

`ResourceResolver` 由三要素配置：

- `prefix`：文件名固定前缀（如方案文件可加 `xxx_` 前缀，配置体系一般留空）。
- `suffix`：文件名固定后缀（配置体系统一用 `.yaml`）。
- `root_path`：所在根目录。

给定一个 `config_id`，解析器能做两个方向的换算：

- `ResolvePath(id)`：`id` → 完整文件路径（拼 `root + prefix + id + suffix`），用于真正去读文件。
- `ToResourceId(path)`：完整文件路径 → `id`（剥掉前缀后缀），用于做**缓存键**——这样 `"default"` 和 `"default.yaml"` 会被识别为同一份配置。

#### 4.1.2 核心流程

```text
config_id  ──ResolvePath──►  绝对文件路径
  "default"        prefix="" suffix=".yaml"
              =  root_path / ("" + "default" + ".yaml")
              =  root_path / default.yaml

文件路径   ──ToResourceId──►  config_id
  ".../default.yaml"          剥 prefix(空) + 剥 suffix(.yaml)
              =  "default"
```

`ToResourceId` 之所以存在，是因为调用方有时传的是纯 id（`"default"`），有时传的是带后缀的文件名（`"default.yaml"`）。缓存要按「逻辑身份」去重，就得先归一化成 id。

#### 4.1.3 源码精读

`ResourceType` 描述一种资源的外貌特征（三字段：名字 + 前缀 + 后缀）：

```cpp
// src/rime/resource.h:16-20
struct ResourceType {
  string name;
  string prefix;
  string suffix;
};
```

[resource.h:22-35](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/resource.h#L22-L35) 定义 `ResourceResolver` 基类，持有 `type_` 与 `root_path_` 两个成员，并提供三组方法：`ResolvePath`（id→路径）、`ToResourceId`（路径→id）、`ToFilePath`（id→相对文件名）。

三个方法的具体实现都在 [resource.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/resource.cc)。`ResolvePath` 是读取时的核心：拼装后取绝对路径。

```cpp
// src/rime/resource.cc:29-32
path ResourceResolver::ResolvePath(const string& resource_id) {
  return std::filesystem::absolute(root_path_ /
                                   (type_.prefix + resource_id + type_.suffix));
}
```

`ToResourceId` 做归一化，剥掉前缀后缀（仅当确实存在时才剥）：

```cpp
// src/rime/resource.cc:12-19
string ResourceResolver::ToResourceId(const string& file_path) const {
  string string_path = path(file_path).generic_u8string();
  bool has_prefix = boost::starts_with(string_path, type_.prefix);
  bool has_suffix = boost::ends_with(string_path, type_.suffix);
  size_t start = (has_prefix ? type_.prefix.length() : 0);
  size_t end = string_path.length() - (has_suffix ? type_.suffix.length() : 0);
  return string_path.substr(start, end);
}
```

**回退解析器** `FallbackResourceResolver` 解决「优先用 A 目录的文件，A 没有就退回 B 目录」这种部署场景（比如先找用户数据目录的定制版本，没有再用共享目录的官方版本）：

```cpp
// src/rime/resource.cc:34-44
path FallbackResourceResolver::ResolvePath(const string& resource_id) {
  auto default_path = ResourceResolver::ResolvePath(resource_id);
  if (!std::filesystem::exists(default_path)) {
    auto fallback_path = std::filesystem::absolute(
        fallback_root_path_ / (type_.prefix + resource_id + type_.suffix));
    if (std::filesystem::exists(fallback_path)) {
      return fallback_path;
    }
  }
  return default_path;
}
```

注意它的策略是「主路径不存在时才尝试回退，回退存在就用，否则返回主路径（让上层报错）」，不做静默吞掉。

#### 4.1.4 代码实践

**实践目标**：理解 `ResourceType` 三字段如何决定路径拼装。

**操作步骤**：

1. 打开 [src/rime/resource.cc:29-32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/resource.cc#L29-L32)。
2. 假设有 `ResourceType{"foo", "pre_", ".yaml"}`、`root_path_ = "/data"`，手工计算 `ResolvePath("bar")` 的结果。
3. 再手工计算 `ToResourceId("pre_bar.yaml")` 与 `ToResourceId("bar")`，确认两者归一化后是否相等。

**预期结果**：

- `ResolvePath("bar")` = `/data/pre_bar.yaml` 的绝对路径。
- `ToResourceId("pre_bar.yaml")` = `"bar"`，`ToResourceId("bar")` = `"bar"`，二者相等 → 缓存会命中同一份。

> 待本地验证：以上结论基于静态推算，建议在你自己的环境里写一段最小 C++ 片段实际调用验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ResolvePath` 用的是 `root_path_ / (...)` 这种 `path` 除法，而 `ToResourceId` 用字符串 `substr`？

**答案**：`ResolvePath` 要拼「目录 + 文件名」的合法路径，交给 `std::filesystem::path::operator/` 能正确处理平台分隔符；`ToResourceId` 只是做前缀/后缀的纯文本裁剪，与路径分隔符无关，所以用字符串操作。

**练习 2**：若 `config_id = "a/b"`（含子目录），`ResolvePath` 会得到什么？

**答案**：得到 `root_path_ / (prefix + "a/b" + suffix)`，即 `root_path_/a/b.yaml`（当 suffix=`.yaml`）。`path::operator/` 能正确处理多层目录。

---

### 4.2 配置树载体：ConfigData

#### 4.2.1 概念说明

`ConfigData` 是**一棵配置树的实际拥有者**。上一篇 u4-l1 讲的 `ConfigItem` 是「节点」，`ConfigData` 是「整棵树的容器 + 它在磁盘上的元信息」。`Config` 对象（对外暴露的句柄）内部持有一个 `shared_ptr<ConfigData>`，多个 `Config` 可以共享同一份 `ConfigData`——这正是缓存复用的基础。

`ConfigData` 还承担「脏标记」与「自动保存」两件事：当配置被程序修改后打上 `modified_` 标记，若开启了 `auto_save_`，对象析构时会把改动写回 `file_path_`。

#### 4.2.2 核心流程

一次从文件加载的流程：

```text
LoadFromFile(path, compiler)
  ├─ 记录 file_path_，清 modified_，重置 root
  ├─ 检查文件是否存在；不存在则记 WARNING 返回 false
  ├─ YAML::LoadFile(path)  ──► YAML::Node（yaml-cpp 的中间表示）
  └─ ConvertFromYaml(node, compiler)  ──► ConfigItem 树，挂到 root
```

`ConvertFromYaml` 是关键：它递归地把 yaml-cpp 的节点树翻译成 librime 自己的 `ConfigItem` 树。当 `compiler` 为 `nullptr` 时（`ConfigLoader` 路径），它只做纯结构转换；当传入 `compiler` 时（`ConfigBuilder` 路径），会沿途把键交给 `compiler->Parse`，用于识别 `__include`/`__patch` 等 DSL 指令（详见 u4-l3）。

#### 4.2.3 源码精读

`ConfigData` 的字段精简到极致：一个对外可见的 `root`，三个受保护的元信息。

```cpp
// src/rime/config/config_data.h:17-50
class ConfigData {
 public:
  ConfigData() = default;
  ~ConfigData();
  bool Save();
  bool LoadFromStream(std::istream& stream);
  bool SaveToStream(std::ostream& stream);
  bool LoadFromFile(const path& file_path, ConfigCompiler* compiler);
  bool SaveToFile(const path& file_path);
  bool TraverseWrite(const string& path, an<ConfigItem> item);
  an<ConfigItem> Traverse(const string& path);
  // ... 路径工具方法省略
  const path& file_path() const { return file_path_; }
  bool modified() const { return modified_; }
  void set_modified() { modified_ = true; }
  void set_auto_save(bool auto_save) { auto_save_ = auto_save; }

  an<ConfigItem> root;       // 整棵树的根节点
 protected:
  path file_path_;          // 来源/去向文件
  bool modified_ = false;   // 是否被改过
  bool auto_save_ = false;  // 析构时是否自动写回
};
```

`LoadFromFile` 实现见 [config_data.cc:63-82](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_data.cc#L63-L82)：注意它对 `.custom.yaml` 这类「可选文件」做了静默处理（不打印 WARNING），因为这类文件允许不存在。

```cpp
// src/rime/config/config_data.cc:63-82
bool ConfigData::LoadFromFile(const path& file_path, ConfigCompiler* compiler) {
  file_path_ = file_path;
  modified_ = false;
  root.reset();
  if (!std::filesystem::exists(file_path)) {
    if (!boost::ends_with(file_path.u8string(), ".custom.yaml"))
      LOG(WARNING) << "nonexistent config file '" << file_path << "'.";
    return false;
  }
  LOG(INFO) << "loading config file '" << file_path << "'.";
  try {
    YAML::Node doc = YAML::LoadFile(file_path.string());
    root = ConvertFromYaml(doc, compiler);
  } catch (YAML::Exception& e) {
    LOG(ERROR) << "Error parsing YAML \"" << file_path << "\" : " << e.what();
    return false;
  }
  return true;
}
```

`ConvertFromYaml` 是 yaml-cpp 节点 → `ConfigItem` 的递归翻译器，见 [config_data.cc:252-290](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_data.cc#L252-L290)。核心是按 YAML 节点类型（Null/Scalar/Sequence/Map）分派：

```cpp
// src/rime/config/config_data.cc:252-290（节选关键分支）
an<ConfigItem> ConvertFromYaml(const YAML::Node& node, ConfigCompiler* compiler) {
  if (YAML::NodeType::Null == node.Type())      return nullptr;
  if (YAML::NodeType::Scalar == node.Type())    return New<ConfigValue>(node.as<string>());
  if (YAML::NodeType::Sequence == node.Type()) {
    auto config_list = New<ConfigList>();
    for (...) { /* 递归 ConvertFromYaml(*it, compiler) 后 Append */ }
    return config_list;
  } else if (YAML::NodeType::Map == node.Type()) {
    auto config_map = New<ConfigMap>();
    for (...) {
      string key = it->first.as<string>();
      auto value = ConvertFromYaml(it->second, compiler);
      if (!compiler || !compiler->Parse(key, value))  // DSL 钩子
        config_map->Set(key, value);
    }
    return config_map;
  }
  return nullptr;
}
```

这一段印证了 u4-l1 的结论：标量统一用 `string` 存（`New<ConfigValue>(node.as<string>())`），列表/映射分别落到 `ConfigList`/`ConfigMap`。注意 Map 分支里的 `compiler->Parse(key, value)`：若编译器「吃掉」了这个键（返回 true，说明它是 `__include` 之类指令），就**不**写入 `config_map`，留给编译器后续处理。这正是 `ConfigBuilder` 与 `ConfigLoader` 的分水岭——`ConfigLoader` 传 `nullptr`，所有键都原样进树。

最后看自动保存的钩子——析构函数：

```cpp
// src/rime/config/config_data.cc:24-27
ConfigData::~ConfigData() {
  if (auto_save_)
    Save();
}
```

`Save()` 仅在 `modified_ && !file_path_.empty()` 时才真正写盘（[config_data.cc:29-31](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_data.cc#L29-L31)），避免无谓 IO。

#### 4.2.4 代码实践

**实践目标**：理解 `ConvertFromYaml` 如何把一段 YAML 翻译成节点树。

**操作步骤**：

1. 准备一段最小 YAML（示例代码，非项目原文件）：

   ```yaml
   # 示例代码
   name: luna
   page_size: 5
   keys: [a, b, c]
   ```

2. 对照 [config_data.cc:252-290](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_data.cc#L252-L290)，手动画出 `ConvertFromYaml`（`compiler=nullptr`）产出的树。

**预期结果**：根是 `ConfigMap`，含四组键值：`name → ConfigValue("luna")`、`page_size → ConfigValue("5")`（注意是字符串 "5"，不是整数！）、`keys → ConfigList`（三个 `ConfigValue`）。所有标量在树里都是字符串，读取时再按需 `GetInt` 解析。

**需要观察的现象**：`compiler` 为 `nullptr` 时，每个 Map 键都会落入 `config_map->Set(key, value)` 分支，没有任何键被「吃掉」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ConvertFromYaml` 的 `Sequence` 分支不调用 `compiler->Parse`，而 `Map` 分支要调用？

**答案**：因为 librime 的配置 DSL（`__include`/`__patch` 等）是以「Map 的键」为载体的——指令本身就是映射里的特殊键。序列（列表）没有键，没有承载指令的位置，所以只需递归转换元素，不参与 DSL 解析。

**练习 2**：若 `LoadFromFile` 传入的文件路径不存在，返回什么？`root` 会是什么状态？

**答案**：返回 `false`；`root` 在函数开头被 `root.reset()` 置空，之后未赋值，保持为空。除 `.custom.yaml` 外会打印一条 WARNING。

---

### 4.3 组件工厂与缓存：ConfigComponentBase

#### 4.3.1 概念说明

`ConfigComponentBase` 是配置加载体系的中枢，它把「组件工厂」「资源解析器」「缓存」三者捏在一起。它实现了 `Config::Component` 接口（即 `Class<Config, const string&>::Component`），所以对外的入口就是 `Create(file_name) -> Config*`。

它的设计有三个亮点：

1. **工厂只产 `Config`，真正重的 `ConfigData` 走缓存**。`Create` 每次都 `new Config(...)`，但传入的 `ConfigData` 可能是从缓存复用的 `shared_ptr`，也可能新解析。
2. **用 `weak_ptr` 缓存，既共享又能回收**。`cache_` 存的是 `weak<ConfigData>`：若仍有人持有（`!expired()`），就 `lock()` 出 `shared_ptr` 共享；若没人持有（`expired()`），就重新解析并更新缓存。
3. **加载策略可替换**。具体「怎么从 id 变成 `ConfigData`」由子类用纯虚函数 `LoadConfig(config_id)` 决定，由此派生出 `ConfigLoader`（直接读 YAML）和 `ConfigBuilder`（带编译器）两条路径。

#### 4.3.2 核心流程

`Create` → `GetConfigData` 的完整流程：

```text
Create(file_name)
  └─ GetConfigData(file_name)
       ├─ config_id = resource_resolver_->ToResourceId(file_name)   // 归一化
       ├─ wp = cache_[config_id]                                    // 取/建 weak 引用
       ├─ if wp.expired():                                          // 缓存失效
       │     data = LoadConfig(config_id)                           // 子类实现：真正解析
       │     wp = data                                              // 写回缓存
       │     return data
       └─ else: return wp.lock()                                    // 共享已有副本
  └─ return new Config(data)                                        // 每次都新建薄壳
```

关键点：缓存键是**归一化后的 `config_id`**（剥掉前后缀），不是原始 `file_name`。所以 `Create("default")` 和 `Create("default.yaml")` 命中同一份 `ConfigData`。

#### 4.3.3 源码精读

`ConfigComponentBase` 定义在 [config_component.h:93-106](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_component.h#L93-L106)：

```cpp
// src/rime/config/config_component.h:93-106
class ConfigComponentBase : public Config::Component {
 public:
  ConfigComponentBase(ResourceResolver* resource_resolver);
  virtual ~ConfigComponentBase();
  Config* Create(const string& file_name);          // 对外工厂入口
 protected:
  virtual an<ConfigData> LoadConfig(const string& config_id) = 0;  // 子类实现
  the<ResourceResolver> resource_resolver_;
 private:
  an<ConfigData> GetConfigData(const string& file_name);            // 缓存逻辑
  map<string, weak<ConfigData>> cache_;                             // 核心：weak 缓存
};
```

`Create` 极薄，只把 `GetConfigData` 的结果包成 `Config`：

```cpp
// src/rime/config/config_component.cc:178-180
Config* ConfigComponentBase::Create(const string& file_name) {
  return new Config(GetConfigData(file_name));
}
```

而 `Config(an<ConfigData>)` 构造函数（[config_component.cc:24](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_component.cc#L24)）只是把传入的 `shared_ptr` 存起来——所以多个 `Config` 可以共享同一份 `ConfigData`，u4-l1 里讲的「`Config` 是个轻量句柄」在此落实。

缓存逻辑全在 `GetConfigData`，[config_component.cc:182-193](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_component.cc#L182-L193)：

```cpp
// src/rime/config/config_component.cc:182-193
an<ConfigData> ConfigComponentBase::GetConfigData(const string& file_name) {
  auto config_id = resource_resolver_->ToResourceId(file_name);
  // keep a weak reference to the shared config data in the component
  weak<ConfigData>& wp(cache_[config_id]);   // 注意：cache_[k] 不存在时会默认构造空 weak
  if (wp.expired()) {  // create a new copy and load it
    auto data = LoadConfig(config_id);
    wp = data;
    return data;
  }
  // obtain the shared copy
  return wp.lock();
}
```

读懂这段有三个细节：

1. `cache_[config_id]` 用 `[]` 访问：若键不存在，`std::map` 会**默认插入一个空的 `weak<ConfigData>`**，空 weak 的 `expired()` 返回 true，于是进入「加载」分支。这是一种惯用的「取引用或创建」写法。
2. `wp = data` 把刚解析的 `shared_ptr` 赋给 `weak`——此后只要这个 `data` 还被任何 `Config` 持有，`expired()` 就保持 false，后续调用直接 `lock()` 复用。
3. 当最后一个持有者释放（比如所有用到 `default` 配置的 `Schema` 都销毁），`weak` 自动过期，下次再请求时会重新解析。**缓存生命周期与使用方一致，无需手动清理**。

#### 4.3.4 代码实践

**实践目标**：验证「同一 config id 多次打开共享同一份 `ConfigData`」。

**操作步骤（源码阅读型实践）**：

1. 阅读 [config_component.cc:182-193](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_component.cc#L182-L193)。
2. 假设程序里有如下伪代码（示例代码）：

   ```cpp
   // 示例代码
   Config* a = Config::Require("config")->Create("default");
   Config* b = Config::Require("config")->Create("default.yaml");
   ```

3. 推演两次 `Create` 各自走的分支。

**需要观察的现象与预期结果**：

- 第一次 `Create("default")`：`ToResourceId("default")="default"`，`cache_["default"]` 不存在 → 插入空 weak → `expired()` 为 true → 调 `LoadConfig` 解析 → 写回 `cache_["default"]` → 返回 `data`（设其为对象 #1）。
- 第二次 `Create("default.yaml")`：`ToResourceId("default.yaml")="default"`（剥掉 `.yaml`），`cache_["default"]` 仍指向 #1 且 `a` 还持有它 → `expired()` 为 false → `lock()` 返回 #1。
- 结论：`a` 和 `b` 内部的 `data_` 指向**同一个 `ConfigData` 对象**。

> 待本地验证：可在测试里 `assert(a->GetItem().get() == b->GetItem().get())`（因为共享 root）来确认。

#### 4.3.5 小练习与答案

**练习 1**：如果改成用 `shared_ptr`（而非 `weak_ptr`）做缓存，会有什么后果？

**答案**：组件会永久持有 `ConfigData`，即使没有任何使用者，这份数据也不会被释放。配置在整个进程生命周期内常驻内存，内存占用只增不减，且文件被改后无法通过「无人引用→重解析」自动刷新。`weak_ptr` 让缓存「跟随使用者生命周期」，是更合理的选择。

**练习 2**：`GetConfigData` 为什么把 `LoadConfig` 设计成虚函数而不是直接在基类里调用 `ConfigData::LoadFromFile`？

**答案**：为了允许不同的加载策略。`ConfigLoader` 直接 `LoadFromFile`（传 `compiler=nullptr`），`ConfigBuilder` 则走 `ConfigCompiler` 流程支持 DSL。把差异下放到子类，基类只管「缓存 + 入口」这个公共职责，是模板方法模式的典型应用。

---

### 4.4 两种加载器：ConfigLoader vs ConfigBuilder

#### 4.4.1 概念说明

`ConfigComponentBase::LoadConfig` 是纯虚函数，由两个具体实现撑起两条路径：

| 加载器 | 做什么 | 何时用 | 是否支持 `__include/__patch` DSL |
|--------|--------|--------|--------------------------------|
| `ConfigLoader` | 直接读 YAML 文件 → `ConfigData` | 运行时取用已部署的配置 | 否（原样进树） |
| `ConfigBuilder` | 经 `ConfigCompiler` 编译，可挂多个插件 | 部署期把方案「编译」成最终配置 | 是 |

直觉上：**运行时**前端要的是「快」，直接读已编译好的 YAML 即可（`ConfigLoader`）；**部署时** librime 要处理用户写的 `__include`/`__patch`/`import_preset` 等指令，把它们展开合并（`ConfigBuilder` + 插件链）。这也是为什么 u9 部署子系统会用 `config_builder`。

此外还有一个 `ConfigComponent<>` 模板，把「加载器类型」与「资源提供者类型」组合成一个具体组件类，方便注册。

#### 4.4.2 核心流程

`ConfigLoader::LoadConfig`：

```text
LoadConfig(resolver, config_id)
  ├─ data = New<ConfigData>()
  ├─ data->LoadFromFile(resolver->ResolvePath(config_id), nullptr)  // compiler=nullptr
  └─ data->set_auto_save(auto_save_)
```

`ConfigBuilder::LoadConfig`：

```text
LoadConfig(resolver, config_id)
  ├─ 构造 MultiplePlugins（把 plugins_ 聚合成一个复合插件）
  ├─ ConfigCompiler compiler(resolver, &multiple_plugins)
  ├─ resource = compiler.Compile(config_id)        // 解析 + 应用 DSL
  ├─ if resource->loaded && !compiler.Link(resource): LOG(ERROR)
  └─ return resource->data                          // 编译产物即 ConfigData
```

#### 4.4.3 源码精读

`ConfigLoader` 极简，[config_component.cc:195-201](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_component.cc#L195-L201)：

```cpp
// src/rime/config/config_component.cc:195-201
an<ConfigData> ConfigLoader::LoadConfig(ResourceResolver* resource_resolver,
                                        const string& config_id) {
  auto data = New<ConfigData>();
  data->LoadFromFile(resource_resolver->ResolvePath(config_id), nullptr);
  data->set_auto_save(auto_save_);
  return data;
}
```

注意第二个参数传 `nullptr`，对应 `ConvertFromYaml` 的「不做 DSL 解析」分支。

`ConfigBuilder` 复杂得多，[config_component.cc:244-253](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_component.cc#L244-L253)：

```cpp
// src/rime/config/config_component.cc:244-253
an<ConfigData> ConfigBuilder::LoadConfig(ResourceResolver* resource_resolver,
                                         const string& config_id) {
  MultiplePlugins<decltype(plugins_)> multiple_plugins(plugins_);
  ConfigCompiler compiler(resource_resolver, &multiple_plugins);
  auto resource = compiler.Compile(config_id);
  if (resource->loaded && !compiler.Link(resource)) {
    LOG(ERROR) << "error building config: " << config_id;
  }
  return resource->data;
}
```

这里出现两个 u4-l3 才详讲的概念，先记结论即可：`ConfigCompiler::Compile` 负责「解析 + 收集 `__include/__patch` 依赖」，`compiler.Link` 负责「按依赖关系把所有资源合并成最终树」，`resource->data` 是编译产出的 `ConfigData`。`MultiplePlugins` 是把 `plugins_` 列表聚合成一个复合 `ConfigCompilerPlugin` 的内部模板（[config_component.cc:211-242](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_component.cc#L211-L242)），让 `ReviewCompileOutput`/`ReviewLinkOutput` 被所有插件依次审查。

把这些组装成一个可注册组件的是 `ConfigComponent` 模板，[config_component.h:108-126](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_component.h#L108-L126)：

```cpp
// src/rime/config/config_component.h:108-126
template <class Loader, class ResourceProvider = ConfigResourceProvider>
class ConfigComponent : public ConfigComponentBase {
 public:
  ConfigComponent(const ResourceType& resource_type =
                      ResourceProvider::kDefaultResourceType)
      : ConfigComponentBase(
            ResourceProvider::CreateResourceResolver(resource_type)) {}
  ConfigComponent(function<void(Loader* loader)> setup)
      : ConfigComponentBase(ResourceProvider::CreateResourceResolver(
            ResourceProvider::kDefaultResourceType)) {
    setup(&loader_);
  }
 private:
  an<ConfigData> LoadConfig(const string& config_id) override {
    return loader_.LoadConfig(resource_resolver_.get(), config_id);
  }
  Loader loader_;
};
```

两个构造函数分别对应「默认配置」与「需要 setup 钩子（比如装插件、设 auto_save）」。`LoadConfig` 委托给成员 `loader_`，把模板参数 `Loader` 接到基类的虚函数槽上。

#### 4.4.4 代码实践：印证三种注册的配置组件

**实践目标**：看清 `core_module.cc` 如何把上述零件注册成实际可用的组件名。

**操作步骤**：

1. 打开 [src/rime/core_module.cc:19-43](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/core_module.cc#L19-L43)。
2. 对照下表逐行确认每个注册项的「加载器 + 资源提供者」组合。

```cpp
// src/rime/core_module.cc:19-43
auto config_builder =
    new ConfigComponent<ConfigBuilder>([&](ConfigBuilder* builder) {
      builder->InstallPlugin(new AutoPatchConfigPlugin);
      builder->InstallPlugin(new DefaultConfigPlugin);
      builder->InstallPlugin(new LegacyPresetConfigPlugin);
      builder->InstallPlugin(new LegacyDictionaryConfigPlugin);
      builder->InstallPlugin(new BuildInfoPlugin);
      builder->InstallPlugin(new SaveOutputPlugin);
    });
r.Register("config_builder", config_builder);

auto config_loader =
    new ConfigComponent<ConfigLoader, DeployedConfigResourceProvider>;
r.Register("config", config_loader);
r.Register("schema", new SchemaComponent(config_loader));

auto user_config =
    new ConfigComponent<ConfigLoader, UserConfigResourceProvider>(
        [](ConfigLoader* loader) { loader->set_auto_save(true); });
r.Register("user_config", user_config);
```

**预期结论**：

| 组件名 | 加载器 | 资源提供者 | 资源类型名 | 目录 |
|--------|--------|-----------|-----------|------|
| `config_builder` | `ConfigBuilder` | `ConfigResourceProvider`（模板默认） | `config` | user_data_dir，回退 shared_data_dir |
| `config` | `ConfigLoader` | `DeployedConfigResourceProvider` | `compiled_config` | staging_dir，回退 prebuilt_data_dir |
| `schema` | （复用 `config_loader`，经 `SchemaComponent` 把 `xxx` → `xxx.schema`） | `DeployedConfigResourceProvider` | `compiled_config` | 同上 |
| `user_config` | `ConfigLoader`（开 `auto_save`） | `UserConfigResourceProvider` | `user_config` | user_data_dir（无回退） |

三种资源提供者的定义见 [config_component.cc:149-171](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_component.cc#L149-L171)：

```cpp
// src/rime/config/config_component.cc:149-171
const ResourceType ConfigResourceProvider::kDefaultResourceType = {"config", "", ".yaml"};
ResourceResolver* ConfigResourceProvider::CreateResourceResolver(const ResourceType& t) {
  return Service::instance().CreateResourceResolver(t);              // user → 回退 shared
}
const ResourceType DeployedConfigResourceProvider::kDefaultResourceType = {"compiled_config", "", ".yaml"};
ResourceResolver* DeployedConfigResourceProvider::CreateResourceResolver(const ResourceType& t) {
  return Service::instance().CreateDeployedResourceResolver(t);      // staging → 回退 prebuilt
}
const ResourceType UserConfigResourceProvider::kDefaultResourceType = {"user_config", "", ".yaml"};
ResourceResolver* UserConfigResourceProvider::CreateResourceResolver(const ResourceType& t) {
  return Service::instance().CreateUserSpecificResourceResolver(t);  // 仅 user
}
```

对应的目录绑定在 [service.cc:167-187](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.cc#L167-L187)，三个工厂方法分别建 `FallbackResourceResolver`（user/staging，带回退）或普通 `ResourceResolver`（user，无回退）。

> 一个容易踩坑的点：`"config"` 这个组件名对应的资源提供者是 `DeployedConfigResourceProvider`（去 `staging_dir`/`prebuilt_data_dir` 找 `xxx.yaml`），而**不是**模板默认的 `ConfigResourceProvider`（`user_data_dir`/`shared_data_dir`）。也就是说运行时 `Config::Require("config")->Create("default")` 拿到的是**已部署**的配置，而非用户手写的源文件。用户源文件的编译发生在部署期，由 `"config_builder"` 负责。

#### 4.4.5 小练习与答案

**练习 1**：`schema` 组件和 `config` 组件用的其实是同一个 `config_loader`（见 `r.Register("schema", new SchemaComponent(config_loader))`），它们有何不同？

**答案**：`config` 组件直接用 `config_id` 当资源 id；`SchemaComponent` 在外面包了一层，把方案名 `luna_pinyin` 拼成 `luna_pinyin.schema` 再委托给底层 `config_loader`（见 [schema.cc:40-42](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/schema.cc#L40-L42)）。于是资源解析时 `prefix="" + "luna_pinyin.schema" + ".yaml"` 得到 `luna_pinyin.schema.yaml`。两者底层共享同一套缓存与目录配置。

**练习 2**：为什么 `user_config` 要在构造时 `loader->set_auto_save(true)`，而 `config` 不要？

**答案**：`user_config` 存的是用户运行时状态（如开关记忆、用户词典首选项），需要程序修改后写回磁盘（`ConfigData` 析构时触发 `Save`）；而 `config` 取的是已部署的静态配置（只读消费），通常不应被运行时修改，故不开自动保存。

---

## 5. 综合实践：追踪一次 config_open 的完整旅程

本实践把前 4 个模块串起来，画出从 C API `config_open(id)` 到 `ConfigItem` 树的端到端调用链。这是本讲的核心实践任务。

### 5.1 起点：C API 入口

C API `config_open` 是 `RimeApi` 方法表里的一个槽位（[rime_api.h:355](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L355)），它被绑定到实现函数 `RimeConfigOpen`（[rime_api_impl.h:1172](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L1172)）。真正的逻辑在辅助函数 `open_config_in_component`（[rime_api_impl.h:553-566](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L553-L566)）：

```cpp
// src/rime_api_impl.h:553-574
static Bool open_config_in_component(const char* config_component,
                                     const char* config_id, RimeConfig* config) {
  if (!config_id || !config) return False;
  Config::Component* cc = Config::Require(config_component);  // 查注册表拿工厂
  if (!cc) return False;
  Config* c = cc->Create(config_id);                          // 工厂产 Config
  if (!c) return False;
  config->ptr = (void*)c;                                     // 裸指针交还 C 层
  return True;
}
RIME_DEPRECATED Bool RimeConfigOpen(const char* config_id, RimeConfig* config) {
  return open_config_in_component("config", config_id, config);  // 用 "config" 组件
}
```

注意 `config->ptr` 是 `void*`，把 C++ 对象以不透明指针形式交还给 C 层，后续 `RimeConfigGetBool` 等再用 `reinterpret_cast<Config*>` 取回（见 [rime_api_impl.h:584-587](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L584-L587) 的 `RimeConfigClose`）。这与 u1-l4 讲的「谁分配谁释放」契约一致：`config_open` 分配，`config_close` 释放。

### 5.2 完整调用链（以 config_open("default") 为例）

```text
[1] C API:     api->config_open("default", &rcfg)
[2]            RimeConfigOpen("default", &rcfg)
[3]            open_config_in_component("config", "default", &rcfg)
[4] 工厂查找:   cc = Config::Require("config")
                  └─ Registry::Find("config") → ConfigComponentBase 子类实例
                     （实际是 ConfigComponent<ConfigLoader, DeployedConfigResourceProvider>）
[5] 工厂生产:   c = cc->Create("default")
                  └─ ConfigComponentBase::Create("default")
                       └─ GetConfigData("default")
[6] 资源归一化: config_id = resolver->ToResourceId("default")
                  └─ DeployedConfigResourceProvider 的 resolver（compiled_config, ".yaml"）
                  └─ "default"（无前后缀可剥）→ config_id = "default"
[7] 缓存命中判断: wp = cache_["default"]; if (wp.expired()) ... else wp.lock()
                  └─ 首次：expired → 进入 LoadConfig
[8] 加载(Loader 路径):
                  └─ ConfigLoader::LoadConfig(resolver, "default")
                       ├─ data = New<ConfigData>()
                       ├─ path = resolver->ResolvePath("default")
                       │      └─ FallbackResourceResolver:
                       │            staging_dir/default.yaml 不存在?
                       │            → 退回 prebuilt_data_dir/default.yaml
                       ├─ data->LoadFromFile(path, /*compiler=*/nullptr)
                       │      ├─ YAML::LoadFile(path) → YAML::Node
                       │      └─ ConvertFromYaml(node, nullptr) → ConfigItem 树 → data->root
                       └─ data->set_auto_save(false)
[9] 写回缓存:    cache_["default"] = data
[10] 包装返回:  return new Config(data);  → rcfg.ptr = c
```

### 5.3 操作步骤（源码阅读型）

1. 按 5.2 的编号逐个打开对应源码链接，确认每一跳的代码行。
2. 重点关注 [6]→[7] 这段：`ToResourceId` 的归一化与 `cache_[config_id]` 的「取或建」。
3. 重点关注 [8] 的两条分支：`FallbackResourceResolver` 的目录回退（[resource.cc:34-44](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/resource.cc#L34-L44)）与 `ConvertFromYaml` 的递归翻译（[config_data.cc:252-290](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_data.cc#L252-L290)）。

### 5.4 需要观察的现象与预期结果

- 第二次 `config_open("default")`：[7] 处 `wp.expired()` 为 false（因为第一次的 `Config` 还活着，`ConfigData` 仍被持有），直接 `wp.lock()` 返回同一份 `ConfigData`，**不会**再次读 YAML 文件。
- 若把第一次的 `Config` 通过 `config_close` 释放，且没有其他持有者，则下次 `config_open("default")` 会因 `expired()` 重新走 [8] 解析。
- `config_open("default.yaml")` 与 `config_open("default")` 命中同一缓存键（[6] 归一化），共享同一份 `ConfigData`。

### 5.5 扩展思考

- 若调用的是 `config_builder` 组件（部署期），[8] 处会走 `ConfigBuilder::LoadConfig`，经 `ConfigCompiler::Compile` + `Link`，此时 `__include`/`__patch` 才被展开——这部分是 u4-l3 的主题。
- `Schema` 构造时调用 `Config::Require("schema")->Create(schema_id)`（[schema.cc:20](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/schema.cc#L20)），与本讲链条几乎一致，只是多了 `SchemaComponent` 把 `schema_id` 拼成 `schema_id.schema` 这一步。

## 6. 本讲小结

- **配置是一种组件**：`Config` 继承 `Class<Config, const string&>`，「取配置」= `Config::Require(name)->Create(id)`，与其他可插拔能力共用同一套注册表机制。
- **`ResourceResolver` 做 id↔路径换算**：`ResolvePath` 拼 `root + prefix + id + suffix` 读文件，`ToResourceId` 剥前后缀做归一化；`FallbackResourceResolver` 提供「主目录没有则退回备目录」的部署期查找。
- **`ConfigData` 是配置树的实际载体**：持有 `root`、记录 `file_path_`/`modified_`/`auto_save_`，`LoadFromFile` 经 yaml-cpp 解析后用 `ConvertFromYaml` 递归翻译成 `ConfigItem` 树。
- **`ConfigComponentBase` 用 `weak_ptr` 缓存**：`cache_` 以归一化后的 `config_id` 为键，`expired()` 决定复用还是重解析，使「同一份配置被多处引用时只解析一次，无人引用时自动回收」。
- **两条加载路径**：`ConfigLoader` 直接读 YAML（运行时用 `config`/`schema`/`user_config`），`ConfigBuilder` 经 `ConfigCompiler` 支持 `__include/__patch` DSL（部署期用 `config_builder`）。
- **资源提供者绑定目录**：`DeployedConfigResourceProvider`（`config`/`schema`）找 `staging_dir`→`prebuilt_data_dir`，`UserConfigResourceProvider`（`user_config`）只找 `user_data_dir` 并开自动保存。

## 7. 下一步学习建议

本讲把「配置怎么从文件变成内存树」讲透了，但刻意把 `ConfigCompiler` 当黑盒——它如何处理 `__include`/`__patch`、如何检测循环依赖、`ConfigCowRef` 的写时拷贝如何工作，都还没展开。**下一篇 u4-l3《配置编译器：__include / __patch DSL》**会拆开 `ConfigBuilder` 背后的编译器，正是 u5-l1 组件体系之前最值得吃透的配置进阶内容。

建议继续阅读的源码：

- [src/rime/config/config_compiler.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.h) 与 `config_compiler.cc`：编译器主体与依赖解析。
- [src/rime/config/config_cow_ref.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_cow_ref.h)：写时拷贝机制。
- [src/rime/config/plugins.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/plugins.h)：`ConfigBuilder` 挂载的插件族（u4-l4 会详讲）。
- [src/rime/registry.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/registry.h)：`Require` 背后的全局注册表（u5-l1）。
