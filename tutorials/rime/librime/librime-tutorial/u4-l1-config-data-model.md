# Config 数据模型

## 1. 本讲目标

输入方案（schema）和 `default.yaml` 等配置文件都是用 YAML 写的，但引擎在运行时并不会反复解析 YAML 文本——它会先把 YAML 一次性转换成一棵**内存中的树形数据结构**，之后所有的读取、修改、合并都发生在这棵树上。这棵树就是本讲的主角：**Config 数据模型**。

学完本讲，你应该能够：

1. 说清楚 `ConfigItem`、`ConfigValue`、`ConfigList`、`ConfigMap` 四者之间的类型层次关系。
2. 解释为什么 `ConfigValue` 内部只用一个 `string` 就能表示布尔、整数、浮点和字符串四种标量。
3. 看懂 `ConfigItemRef` 这一组「引用代理」类如何用 `operator[]` 做路径寻址、用 `Is*/To*` 做类型检查与安全转换。
4. 给定一小段 YAML，能够画出它在内存中对应的 `ConfigItem` 树，并标注每个节点的具体子类型。

本讲只讲**数据模型本身**，不涉及 YAML 是如何被加载进来的（那是 u4-l2「Config::Component 与配置加载」的内容），也不涉及 `__include`/`__patch` 编译 DSL（那是 u4-l3 的内容）。

## 2. 前置知识

在进入源码之前，先建立三个直觉。

**第一，YAML 的三种基本节点。** 一段 YAML 文本最终只有三种「节点形态」：

- **标量（scalar）**：单个值，比如 `5`、`true`、`"luna_pinyin"`。YAML 不强制标量的类型，`5` 既可以当整数也可以当字符串。
- **序列（sequence）**：有序列表，用 `-` 引导，对应 YAML 的数组。
- **映射（mapping）**：键值对集合，用 `key: value` 表示。

另外还有一种特殊的「空（null）」节点，表示什么都没有。librime 的数据模型几乎是 YAML 这套节点形态的 C++ 镜像。

**第二，智能指针别名。** librime 在 `common.h` 里给标准库的智能指针起了简短别名，源码里几乎不用 `std::shared_ptr` 全称。你需要记住这几个：

- `the<T>` = `std::unique_ptr<T>`（独占所有权）
- `an<T>` = `std::shared_ptr<T>`（共享所有权，配置树节点几乎都用它）
- `of<T>` = `an<T>`（语义同 `an`，常用于声明容器元素类型，强调「这是一个 T 的实例」）
- `weak<T>` = `std::weak_ptr<T>`（弱引用，不增加引用计数）
- `New<T>(args...)` = `std::make_shared<T>(args...)`（构造一个 `an<T>`）
- `As<X>(ptr)` = `std::dynamic_pointer_cast<X>(ptr)`（把基类智能指针向下转型为子类）
- `Is<X>(ptr)` = 转型是否成功（等价于 `bool(As<X>(ptr))`）

这些别名定义在 [src/rime/common.h:57-79](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/common.h#L57-L79)。

**第三，多态与类型标签。** 配置树是异构的：一个节点可能是值、可能是列表、也可能是映射。librime 用 C++ 的「公共基类 + 虚函数 + 类型标签」来统一管理它们，而不是用 `std::variant`。下面进入源码时你会看到 `ConfigItem` 作为公共基类，用一个枚举 `type_` 记录每个节点到底是什么子类型。

## 3. 本讲源码地图

本讲涉及的文件很少，但它们是整个配置子系统的基石：

| 文件 | 作用 |
|------|------|
| [src/rime/config/config_types.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_types.h) | 定义 `ConfigItem` 基类、`ConfigValue`/`ConfigList`/`ConfigMap` 三种子类型，以及 `ConfigItemRef` 引用代理体系。本讲的核心文件。 |
| [src/rime/config/config_types.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_types.cc) | 上述类的实现，尤其是 `ConfigValue` 的标量解析逻辑与 `ConfigList`/`ConfigMap` 的增删查改。 |
| [src/rime/config.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config.h) | 一个「伞头文件」，只负责把 `config_component.h` 和 `config_types.h` 一起包含进来，本身不含任何类定义。外部代码只需 `#include <rime/config.h>`。 |
| [src/rime/config/config_data.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_data.h) | `ConfigData` 类，持有整棵配置树的根节点 `root`（类型为 `an<ConfigItem>`）。本讲把它当作「整棵树的容器」来引用，其加载细节留待 u4-l2。 |

一句话概括它们的层次关系：`ConfigData` 持有根节点 → 根节点是一个 `ConfigItem`（通常是 `ConfigMap`）→ 它的子节点们又各自是 `ConfigItem` 的某个子类型 → 如此递归形成整棵树。而 `ConfigItemRef` 则是「树中某个位置的代理」，用来安全地读写这些节点。

## 4. 核心概念与源码讲解

### 4.1 ConfigItem：所有配置节点的统一基类

#### 4.1.1 概念说明

配置树上的每一个节点——无论是一个数字、一串文字、一个列表还是一张映射表——在 C++ 层面都是 `ConfigItem` 的（直接或间接）实例。`ConfigItem` 是抽象的「统一入口」：它本身不存储任何具体数据，只记录「我属于哪一种节点类型」。这种设计让外部代码可以拿着一个 `an<ConfigItem>` 指针到处传递，需要时再用类型标签或 `dynamic_pointer_cast` 判断它到底是谁。

`ConfigItem` 定义了一个四值的类型枚举：

```cpp
enum ValueType { kNull, kScalar, kList, kMap };
```

- `kNull`：空节点（默认构造的 `ConfigItem` 就是空）。
- `kScalar`：标量，对应 `ConfigValue`。
- `kList`：列表，对应 `ConfigList`。
- `kMap`：映射，对应 `ConfigMap`。

#### 4.1.2 核心流程

`ConfigItem` 的生命周期非常简单：

1. 子类构造时，通过 protected 构造函数 `ConfigItem(ValueType type)` 把自己的 `type_` 成员设成对应的枚举值；默认构造（无参）则得到 `kNull`。
2. 外部通过公有方法 `type()` 读取这个标签，据此决定后续如何处理节点。
3. `empty()` 是个虚函数：基类版本判断「是不是 `kNull`」，子类可以重写它（例如 `ConfigValue` 改成判断「字符串是否为空」）。

由于 `type_` 是 protected 且只能由子类在构造时设定，运行期无法改变一个节点的类型——一个 `ConfigValue` 永远是标量，不会变成列表。这点很重要：要替换某位置上的节点类型，只能整体替换节点对象（这正是 `ConfigItemRef::SetItem` 的工作）。

#### 4.1.3 源码精读

`ConfigItem` 基类的完整定义：

```cpp
// config item base class
class ConfigItem {
 public:
  enum ValueType { kNull, kScalar, kList, kMap };

  ConfigItem() = default;  // null
  virtual ~ConfigItem() = default;

  ValueType type() const { return type_; }

  virtual bool empty() const { return type_ == kNull; }

 protected:
  ConfigItem(ValueType type) : type_(type) {}

  ValueType type_ = kNull;
};
```

见 [src/rime/config/config_types.h:16-32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_types.h#L16-L32)。

要点解读：

- `ConfigItem() = default;` 注释写着 `// null`——默认构造的节点类型是 `kNull`（成员初值 `type_ = kNull`）。
- 构造函数 `ConfigItem(ValueType type)` 是 `protected` 的，意味着外部不能直接 `new ConfigItem(kScalar)`，只有子类（`ConfigValue` 等）能在自己的构造初始化列表里调用它。
- 析构函数是 `virtual` 且 `= default`，保证通过基类指针 `delete` 时能正确调用子类析构——这是配置树节点能被 `shared_ptr` 安全管理的前提。
- `type()` 是内联的纯访问器，没有虚函数调用开销。

#### 4.1.4 代码实践

**实践目标：** 在源码层面确认「一个节点的类型一旦构造就固定不变」。

**操作步骤：**

1. 打开 [src/rime/config/config_types.h:16-32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_types.h#L16-L32)。
2. 注意 `type_` 的访问级别是 `protected`，且没有任何 `set_type()` 之类的公有方法。
3. 注意 `ConfigItem(ValueType type)` 这个构造函数也是 `protected` 的。

**需要观察的现象：** 你会发现整个类里没有任何方法能修改 `type_` 的值。

**预期结果：** 节点类型是「构造期决定、运行期只读」的。这说明 librime 选择「换类型 = 换对象」而不是「原地变形」。理解这一点之后，`ConfigItemRef` 里大量 `SetItem(...)` 的设计就有了动机。

#### 4.1.5 小练习与答案

**练习 1：** 默认构造一个 `ConfigItem`（即 `ConfigItem item;`），它的 `type()` 返回什么？`empty()` 返回什么？

**答案：** `type()` 返回 `kNull`（因为成员初值 `type_ = kNull`），`empty()` 返回 `true`（基类版本 `type_ == kNull` 成立）。

**练习 2：** 为什么 `ConfigItem` 的析构函数必须声明为 `virtual`？

**答案：** 因为配置树的节点是多态的：外部代码持有的是 `an<ConfigItem>`（基类 `shared_ptr`），实际指向的可能是 `ConfigValue`、`ConfigList` 或 `ConfigMap`。若析构不是虚函数，通过基类指针销毁时只会调用基类析构、跳过子类析构，可能造成子类资源（如 `string`、`vector`、`map`）泄漏。虚析构保证正确调用最派生类的析构链。

### 4.2 ConfigValue：标量节点（一切皆字符串）

#### 4.2.1 概念说明

`ConfigValue` 表示一个标量值。它的特别之处在于：**无论你存的是布尔、整数、浮点还是字符串，它内部都只用一个 `string value_` 来保存**。也就是说，所有标量在进入配置树时都会先被「文本化」，需要用时再按需解析回来。

为什么这样设计？因为 YAML 加载时，标量本就被读成字符串（参见 u4-l2 的 `ConvertFromYaml`，它会调用 `New<ConfigValue>(node.as<string>())`）。既然源头是字符串，干脆统一以字符串形式存储，避免在加载期猜测 `5` 到底该当 `int` 还是 `double` 还是 `string`。只有当调用方明确要 `int` 时，才用 `GetInt` 解析一次。这是一种「**惰性类型化**」策略。

#### 4.2.2 核心流程

写入一个标量的流程：

1. 调用 `ConfigValue` 的某个构造函数（或 `Set*` 方法），如 `New<ConfigValue>(42)`。
2. 构造函数内部转调对应的 `Set*` 方法：`SetInt(42)` 把 `42` 用 `std::to_string` 转成字符串 `"42"` 存进 `value_`。
3. 此后节点类型固定为 `kScalar`。

读取一个标量的流程（以 `GetInt` 为例）：

1. 检查出参指针非空且 `value_` 非空，否则返回 `false` 表示失败。
2. 若字符串以 `0x` 开头，按 16 进制解析（`strtoul`）。
3. 否则按 10 进制解析（`std::stoi`），解析失败（抛异常）则捕获并返回 `false`。
4. 成功则把值写入出参并返回 `true`。

注意所有 `Get*` 方法都用**出参 + 返回 bool** 的模式：返回值表示「转换是否成功」，真正的值通过指针写出。这样调用方可以写出 `if (value->GetInt(&n)) { /* 用 n */ }` 的安全代码。

#### 4.2.3 源码精读

`ConfigValue` 的声明：

```cpp
class ConfigValue : public ConfigItem {
 public:
  ConfigValue() : ConfigItem(kScalar) {}
  RIME_DLL ConfigValue(bool value);
  RIME_DLL ConfigValue(int value);
  RIME_DLL ConfigValue(double value);
  RIME_DLL ConfigValue(const char* value);
  RIME_DLL ConfigValue(const string& value);

  // schalar value accessors
  bool GetBool(bool* value) const;
  RIME_DLL bool GetInt(int* value) const;
  bool GetDouble(double* value) const;
  RIME_DLL bool GetString(string* value) const;
  bool SetBool(bool value);
  bool SetInt(int value);
  bool SetDouble(double value);
  bool SetString(const char* value);
  bool SetString(const string& value);

  const string& str() const { return value_; }

  bool empty() const override { return value_.empty(); }

 protected:
  string value_;
};
```

见 [src/rime/config/config_types.h:34-60](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_types.h#L34-L60)。注意 `value_` 是 `string` 类型——这是「一切皆字符串」的关键。注释里的拼写 `schalar` 是源码原有的笔误（应为 scalar），保留原样即可。

`GetInt` 的实现，展示了 16 进制与 10 进制的双路解析：

```cpp
bool ConfigValue::GetInt(int* value) const {
  if (!value || value_.empty())
    return false;
  // try to parse hex number
  if (boost::starts_with(value_, "0x")) {
    char* p = NULL;
    unsigned int hex = std::strtoul(value_.c_str(), &p, 16);
    if (*p == '\0') {
      *value = static_cast<int>(hex);
      return true;
    }
  }
  // decimal
  try {
    *value = std::stoi(value_);
  } catch (...) {
    return false;
  }
  return true;
}
```

见 [src/rime/config/config_types.cc:49-68](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_types.cc#L49-L68)。`strtoul` 的第二个参数 `p` 指向解析结束位置，若它指向字符串末尾（`*p == '\0'`）说明整串都是合法的 16 进制数字。

`GetBool` 的实现，展示了大小写不敏感的字符串匹配：

```cpp
bool ConfigValue::GetBool(bool* value) const {
  if (!value || value_.empty())
    return false;
  string bstr = value_;
  boost::to_lower(bstr);
  if ("true" == bstr) {
    *value = true;
    return true;
  } else if ("false" == bstr) {
    *value = false;
    return true;
  } else
    return false;
}
```

见 [src/rime/config/config_types.cc:34-47](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_types.cc#L34-L47)。注意它只认字面量 `true`/`false`（不区分大小写），不认 `1`/`0` 或 `yes`/`no`。

写入侧的构造函数把数值转成字符串：

```cpp
ConfigValue::ConfigValue(int value) : ConfigItem(kScalar) {
  SetInt(value);
}
// ...
bool ConfigValue::SetInt(int value) {
  value_ = std::to_string(value);
  return true;
}
```

见 [src/rime/config/config_types.cc:20-22](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_types.cc#L20-L22) 与 [src/rime/config/config_types.cc:93-96](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_types.cc#L93-L96)。

#### 4.2.4 代码实践

**实践目标：** 亲手验证「同一个字符串可以被不同类型安全读取」，并观察失败情形。

**操作步骤：**

1. 阅读上述 `GetInt` 与 `GetBool` 的源码，预测下列三个 `ConfigValue` 在调用 `GetInt`/`GetBool`/`GetString` 时分别返回什么。
2. （可选）若你已按 u1-l2 完成本地构建，可在 `tools/` 下写一个最小测试程序，构造三个值并打印结果；若暂无条件编译，标注「待本地验证」。
   - `v1 = New<ConfigValue>(string("0x1F"))`：`GetInt(&n)` 返回什么？`n` 是多少？
   - `v2 = New<ConfigValue>(string("TRUE"))`：`GetBool(&b)` 返回什么？`b` 是什么？
   - `v3 = New<ConfigValue>(string("3.14"))`：`GetInt(&n)` 返回什么？`GetDouble(&d)` 返回什么？

**需要观察的现象：**

- `v1`：`GetInt` 返回 `true`，`n == 31`（因为 `0x1F` 是 16 进制）。
- `v2`：`GetBool` 返回 `true`，`b == true`（大小写不敏感）。
- `v3`：`GetInt` 返回 `false`（`std::stoi` 抛异常被捕获），`GetDouble` 返回 `true`，`d == 3.14`。

**预期结果：** 同一个底层字符串 `value_`，按不同访问器解析会得到完全不同的结果；解析失败时返回 `false` 而不是抛异常或崩溃。这就是「一切皆字符串 + 惰性类型化」的安全之处。

> 若无法本地运行，明确标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 `ConfigValue` 不在构造时就确定并保存「原始类型」（比如记一个 `int_value_`）？

**答案：** 因为 YAML 标量本质是字符串，加载期无法也无须判断 `5` 该当 int 还是 string。统一存字符串可以无损承载任意标量，且避免「存进去是 int、读出来要 string」之类的类型冲突。需要具体类型时再惰性解析，解析失败有清晰的 `false` 反馈。

**练习 2：** 给定 `ConfigValue` 持有字符串 `"42"`，分别调用 `GetInt`、`GetDouble`、`GetString`、`GetBool`，哪些会成功？

**答案：** `GetInt` 成功（返回 `true`，值为 42）；`GetDouble` 成功（`std::stod` 能解析 `"42"`，值为 42.0）；`GetString` 成功（原样返回 `"42"`）；`GetBool` 失败（返回 `false`，因为 `"42"` 既不是 `"true"` 也不是 `"false"`）。

### 4.3 ConfigList 与 ConfigMap：容器节点

#### 4.3.1 概念说明

`ConfigList` 和 `ConfigMap` 是两种容器节点，分别对应 YAML 的序列和映射。它们都「持有若干个子 `ConfigItem`」，从而让配置能够递归地构成一棵树。

- `ConfigList` 内部是 `vector<of<ConfigItem>>`，即一个 `shared_ptr<ConfigItem>` 的动态数组，按下标访问。
- `ConfigMap` 内部是 `map<string, an<ConfigItem>>`，即以字符串为键的有序映射（`std::map` 按键字典序排列）。

源码注释明确指出 `ConfigMap` 的一个限制：**键必须是字符串，最好是字母数字**（见 [src/rime/config/config_types.h:86](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_types.h#L86)）。这一点和 YAML 本身允许任意键类型不同，是 librime 为了简化路径寻址（用 `/` 分隔键、用 `@n` 表示列表下标）而做的取舍。

#### 4.3.2 核心流程

`ConfigList` 的下标访问有一个「**自动扩张**」特性：当你 `SetAt(i, item)` 而 `i` 超出当前长度时，它不会报错，而是先用 `resize(i + 1)` 把数组补长（中间空位填 `nullptr`），再写入。这意味着列表可以稀疏，中间允许存在空节点。

`ConfigMap` 的 `Get(key)` 在键不存在时返回 `nullptr`（一个空的 `an<ConfigItem>`），`HasKey(key)` 正是借助「返回值是否为真」来判断的——它并不维护一个单独的键集合，而是直接看 `Get` 能否取到非空节点。注意这有一个隐含语义：**如果某个键存在但值是 `nullptr`，`HasKey` 也会返回 `false`**。

两者都提供 `begin()/end()` 迭代器，便于用范围 for 遍历直接子节点。

#### 4.3.3 源码精读

`ConfigList` 的声明：

```cpp
class ConfigList : public ConfigItem {
 public:
  using Sequence = vector<of<ConfigItem>>;
  using Iterator = Sequence::iterator;

  ConfigList() : ConfigItem(kList) {}
  RIME_DLL an<ConfigItem> GetAt(size_t i) const;
  RIME_DLL an<ConfigValue> GetValueAt(size_t i) const;
  RIME_DLL bool SetAt(size_t i, an<ConfigItem> element);
  bool Insert(size_t i, an<ConfigItem> element);
  RIME_DLL bool Append(an<ConfigItem> element);
  bool Resize(size_t size);
  RIME_DLL bool Clear();
  RIME_DLL size_t size() const;
  // ...
 protected:
  Sequence seq_;
};
```

见 [src/rime/config/config_types.h:62-84](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_types.h#L62-L84)。

`SetAt` 的自动扩张实现：

```cpp
bool ConfigList::SetAt(size_t i, an<ConfigItem> element) {
  if (i >= seq_.size())
    seq_.resize(i + 1);
  seq_[i] = element;
  return true;
}
```

见 [src/rime/config/config_types.cc:126-131](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_types.cc#L126-L131)。

`GetAt` 的越界保护：

```cpp
an<ConfigItem> ConfigList::GetAt(size_t i) const {
  if (i >= seq_.size())
    return nullptr;
  else
    return seq_[i];
}
```

见 [src/rime/config/config_types.cc:115-120](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_types.cc#L115-L120)。越界时返回 `nullptr` 而非崩溃，是配置数据「宽容读取」的一贯风格。

`ConfigMap` 的声明与键限制注释：

```cpp
// limitation: map keys have to be strings, preferably alphanumeric
class ConfigMap : public ConfigItem {
 public:
  using Map = map<string, an<ConfigItem>>;
  // ...
  RIME_DLL bool HasKey(const string& key) const;
  RIME_DLL an<ConfigItem> Get(const string& key) const;
  RIME_DLL an<ConfigValue> GetValue(const string& key) const;
  RIME_DLL bool Set(const string& key, an<ConfigItem> element);
  // ...
 protected:
  Map map_;
};
```

见 [src/rime/config/config_types.h:86-106](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_types.h#L86-L106)。

`HasKey` 与 `Get` 的实现，体现「以 `Get` 的返回值定义键是否存在」：

```cpp
bool ConfigMap::HasKey(const string& key) const {
  return bool(Get(key));
}

an<ConfigItem> ConfigMap::Get(const string& key) const {
  auto it = map_.find(key);
  if (it == map_.end())
    return nullptr;
  else
    return it->second;
}
```

见 [src/rime/config/config_types.cc:170-180](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_types.cc#L170-L180)。

两个容器还各自提供了类型化便捷访问器 `GetValueAt` 与 `GetValue`，它们都借助 `As<ConfigValue>` 把 `ConfigItem` 向下转型——若该位置存的不是标量（比如是个子列表），转型失败返回 `nullptr`。

```cpp
an<ConfigValue> ConfigList::GetValueAt(size_t i) const {
  return As<ConfigValue>(GetAt(i));
}
```

见 [src/rime/config/config_types.cc:122-124](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_types.cc#L122-L124)。

#### 4.3.4 代码实践

**实践目标：** 验证 `ConfigMap::HasKey` 在「键存在但值为空」时的行为。

**操作步骤：**

1. 想象执行：`auto m = New<ConfigMap>(); m->Set("k", nullptr);`
2. 预测 `m->HasKey("k")` 返回 `true` 还是 `false`。

**需要观察的现象：** 根据 `HasKey` 源码 `return bool(Get(key));`，`Get("k")` 会取到 `nullptr`，而 `bool(nullptr)` 是 `false`。

**预期结果：** `HasKey("k")` 返回 `false`。也就是说「键存在但值是空」在 librime 看来等价于「键不存在」。这是有意为之：配置中显式写出的 `key:` （值为空）会被视同未设置。若需本地确认，可在测试程序中复现，否则标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1：** 对一个空 `ConfigList lst`，调用 `lst->SetAt(5, New<ConfigValue>("x"))` 之后，`lst->size()` 是多少？`lst->GetAt(2)` 返回什么？

**答案：** `size()` 是 6（因为 `resize(5 + 1)`）。`GetAt(2)` 返回 `nullptr`（下标 0、1、2、3、4 中只有 5 被赋值，其余是 `resize` 填充的默认空指针）。

**练习 2：** 为什么 `ConfigMap` 用 `std::map` 而不是 `std::unordered_map`？

**答案：** `std::map` 按键的字典序有序排列，这让配置在序列化（写回 YAML）时输出顺序稳定、可读、可复现，也便于做确定性 diff 和测试。`unordered_map` 虽然查找更快，但遍历顺序不确定，会让生成的配置文件每次顺序都不同。配置数据的读多写少、规模有限，`std::map` 的性能完全够用，稳定性更重要。

### 4.4 ConfigItemRef：带类型检查与路径寻址的引用代理

#### 4.4.1 概念说明

直接操作 `ConfigItem` 树要写很多 `As<ConfigValue>`、`As<ConfigList>` 之类的转型代码，既啰嗦又容易出错。`ConfigItemRef` 这一组类就是为简化这类操作而设计的「**引用代理**」：它代表「配置树中某个位置上的节点」，提供两类便利：

1. **类型检查与安全转换**：`IsNull()/IsValue()/IsList()/IsMap()` 判断类型，`ToBool()/ToInt()/ToString()` 直接取出标量值，`AsList()/AsMap()` 取出容器。
2. **路径寻址**：重载 `operator[](size_t)` 和 `operator[](const string&)`，让你能像写 `config["menu"]["page_size"]` 这样链式深入到树的任意位置。

它有两个具体子类：`ConfigListEntryRef`（代理列表中的某个下标位置）和 `ConfigMapEntryRef`（代理映射中的某个键位置）。两者都继承自 `ConfigItemRef`，只各自实现 `GetItem()/SetItem()` 两个纯虚函数。

#### 4.4.2 核心流程

读取一个嵌套值 `config["menu"]["page_size"]` 的展开过程：

1. `config` 是一个 `ConfigItemRef`（实际是它的某个子类），`config["menu"]` 调用 `operator[](const string&)`。
2. 该操作符先 `AsMap()` 取得自己指向的 `ConfigMap`，再返回一个新的 `ConfigMapEntryRef(data_, map, "menu")`，它代理「`menu` 这个键的位置」。
3. 对这个临时对象再调用 `["page_size"]`，又返回一个新的 `ConfigMapEntryRef`，代理「`menu` 下 `page_size` 的位置」。
4. 最后把这个临时对象转成标量（比如隐式转 `an<ConfigItem>`，或调用 `ToInt()`），触发最内层的 `GetItem()`，逐级回溯到真正的 `ConfigValue`，取出整数 5。

每一步都通过 `AsMap()`/`AsList()` 自动判断中间节点类型；若中途某层不是预期的容器类型，`AsMap()` 还会**自动创建**一个空的 `ConfigMap` 并写回——这是为「写入路径」服务的便利特性（见下文源码）。

写入路径则反过来：`config["menu"]["page_size"] = 5` 触发 `operator=`，经 `AsConfigItem` 把字面量 `5` 包装成 `New<ConfigValue>(5)`，再调用最内层 `SetItem`，逐层回溯地把新节点挂到树上，并标记 `modified`。

#### 4.4.3 源码精读

`ConfigItemRef` 基类的关键部分：

```cpp
class ConfigItemRef {
 public:
  ConfigItemRef(ConfigData* data) : data_(data) {}
  virtual ~ConfigItemRef() = default;
  operator an<ConfigItem>() const { return GetItem(); }
  an<ConfigItem> operator*() const { return GetItem(); }
  template <class T>
  ConfigItemRef& operator=(const T& x) {
    SetItem(AsConfigItem(x, std::is_convertible<T, an<ConfigItem>>()));
    return *this;
  }
  ConfigListEntryRef operator[](size_t index);
  ConfigMapEntryRef operator[](const string& key);

  RIME_DLL bool IsNull() const;
  bool IsValue() const;
  RIME_DLL bool IsList() const;
  bool IsMap() const;

  RIME_DLL bool ToBool() const;
  RIME_DLL int ToInt() const;
  double ToDouble() const;
  RIME_DLL string ToString() const;

  RIME_DLL an<ConfigList> AsList();
  RIME_DLL an<ConfigMap> AsMap();
  // ...
 protected:
  virtual an<ConfigItem> GetItem() const = 0;
  virtual void SetItem(an<ConfigItem> item) = 0;

  ConfigData* data_;
};
```

见 [src/rime/config/config_types.h:126-168](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_types.h#L126-L168)。两个纯虚函数 `GetItem()/SetItem()` 是核心：子类通过它们决定「这个代理实际绑在哪个容器、哪个位置」。

`AsConfigItem` 模板，负责把赋值号右边的值包装成节点——已是指针则原样用，否则包成 `ConfigValue`：

```cpp
template <class T>
an<ConfigItem> AsConfigItem(const T& x, const std::false_type&) {
  return New<ConfigValue>(x);
};

template <class T>
an<ConfigItem> AsConfigItem(const T& x, const std::true_type&) {
  return x;
};
```

见 [src/rime/config/config_types.h:108-120](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_types.h#L108-L120)。它利用 `std::is_convertible<T, an<ConfigItem>>` 在编译期选择分支：若 `T` 本身就能转成节点指针（`true_type`），直接返回；否则（`false_type`）当成标量包成 `ConfigValue`。这就是 `= 5`、`= "abc"`、`= New<ConfigList>()` 都能工作的原因。

`operator[]` 两个重载，把代理链延伸一层：

```cpp
inline ConfigListEntryRef ConfigItemRef::operator[](size_t index) {
  return ConfigListEntryRef(data_, AsList(), index);
}

inline ConfigMapEntryRef ConfigItemRef::operator[](const string& key) {
  return ConfigMapEntryRef(data_, AsMap(), key);
}
```

见 [src/rime/config/config_types.h:206-212](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_types.h#L206-L212)。

两个具体子类，分别绑定列表下标与映射键：

```cpp
class ConfigListEntryRef : public ConfigItemRef {
  // ...
 protected:
  an<ConfigItem> GetItem() const { return list_->GetAt(index_); }
  void SetItem(an<ConfigItem> item) {
    list_->SetAt(index_, item);
    set_modified();
  }
 private:
  an<ConfigList> list_;
  size_t index_;
};

class ConfigMapEntryRef : public ConfigItemRef {
  // ...
 protected:
  an<ConfigItem> GetItem() const { return map_->Get(key_); }
  void SetItem(an<ConfigItem> item) {
    map_->Set(key_, item);
    set_modified();
  }
 private:
  an<ConfigMap> map_;
  string key_;
};
```

见 [src/rime/config/config_types.h:170-204](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_types.h#L170-L204)。注意两者的 `SetItem` 在写回后都调用了 `set_modified()`，把「脏标记」上报给 `ConfigData`（通过基类持有的 `data_` 指针），这是配置能被判定为「已修改、需要回存」的依据。

`AsMap()` 的自动创建逻辑——这是「写入时自动建容器」特性的来源：

```cpp
an<ConfigMap> ConfigItemRef::AsMap() {
  auto map = As<ConfigMap>(GetItem());
  if (!map)
    SetItem(map = New<ConfigMap>());
  return map;
}
```

见 [src/rime/config/config_types.cc:265-270](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_types.cc#L265-L270)。`AsList()` 同理（见 [src/rime/config/config_types.cc:258-263](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_types.cc#L258-L263)）。

类型检查方法的实现，统一「先取节点再判 type 标签」：

```cpp
bool ConfigItemRef::IsNull() const {
  auto item = GetItem();
  return !item || item->type() == ConfigItem::kNull;
}
// ...
bool ConfigItemRef::IsList() const {
  auto item = GetItem();
  return item && item->type() == ConfigItem::kList;
}
```

见 [src/rime/config/config_types.cc:206-224](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_types.cc#L206-L224)。注意 `IsNull()` 还额外处理了「节点不存在（指针为空）」的情况——空指针也算 `IsNull`，这保证了链式寻址到不存在的深路径时不会崩溃，而是被当作 null 安全处理。

`ToInt()` 的实现，展示「取值失败给默认值」的安全模式：

```cpp
int ConfigItemRef::ToInt() const {
  int value = 0;
  if (auto item = As<ConfigValue>(GetItem())) {
    item->GetInt(&value);
  }
  return value;
}
```

见 [src/rime/config/config_types.cc:234-240](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_types.cc#L234-L240)。与 `ConfigValue::GetInt`（返回 bool、值由出参带出）不同，`ConfigItemRef::ToInt` 直接返回数值，失败时返回默认值 0。两种 API 风格并存：需要知道「是否成功」用前者，只想要「拿一个值、失败就默认」用后者。

#### 4.4.4 代码实践

**实践目标：** 通过阅读源码，复现「用 `operator[]` 链式寻址并写入一个深路径节点」时发生的逐级调用。

**操作步骤：**

1. 假设有 `Config` 对象 `c`（其内部 `ConfigData::root` 是一个 `ConfigMap`）。
2. 阅读并推演执行 `c["menu"]["page_size"] = 5;` 时，每一层发生了什么。
3. 写出调用顺序清单。

**需要观察的现象：** 应能整理出如下序列：

1. `c["menu"]` → `ConfigItemRef::operator[](string("menu"))` → 内部 `AsMap()` 取得 root 这个 `ConfigMap` → 返回临时 `ConfigMapEntryRef(data_, rootMap, "menu")`。
2. 对临时对象 `["page_size"]` → 再次 `operator[](string("page_size"))` → `AsMap()` 取得「menu」对应的子 `ConfigMap`（若不存在则自动创建）→ 返回新的 `ConfigMapEntryRef` 代理 `page_size` 位置。
3. `= 5` → 最内层临时对象的 `operator=(5)` → `AsConfigItem(5, false_type)` 包装成 `New<ConfigValue>(5)` → `SetItem(...)` → `map_->Set("page_size", ConfigValue(5))` → `set_modified()` 上报脏标记。

**预期结果：** 树上 `menu → page_size` 处被写入一个值为 `"5"` 的 `ConfigValue`，且 `ConfigData` 的 `modified_` 被置为 `true`。读者若已构建 librime，可写一段最小程序验证 `c["menu"]["page_size"].ToInt()` 返回 5；否则标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1：** `ConfigItemRef::operator=` 是模板函数，靠 `std::is_convertible` 分派。请说明赋值 `= 5` 和赋值 `= New<ConfigList>()` 分别走哪个分支。

**答案：** `= 5`：`int` 不能隐式转成 `an<ConfigItem>`，走 `false_type` 分支，包成 `New<ConfigValue>(5)`。`= New<ConfigList>()`：`New<ConfigList>()` 的类型是 `an<ConfigList>`，能隐式转成 `an<ConfigItem>`（子类指针向基类指针的转换），走 `true_type` 分支，原样使用该指针。

**练习 2：** 为什么 `AsMap()` 在节点不是 `ConfigMap` 时要 `SetItem(New<ConfigMap>())`，而不是直接返回 `nullptr`？

**答案：** 因为 `operator[]` 紧接着会基于 `AsMap()` 的返回值构造新的 `ConfigMapEntryRef`。若 `AsMap()` 返回空，代理就无处可绑，链式写入 `config["a"]["b"] = 1` 在 `a` 不存在时会失败。`AsMap()` 自动创建并写回一个空 `ConfigMap`，使得「沿路径写入」永远可行——遇到不存在的中间节点就当场建出来。这正是 librime 配置 API 写起来像操作普通嵌套结构一样顺手的原因。

## 5. 综合实践

本任务把全讲四个模块串起来：给定一段真实风格的 YAML，画出它在内存中的 `ConfigItem` 树，并标注每个节点的具体子类型与关键内部状态。

下面这段 YAML 改编自仓库里的 `data/minimal/default.yaml`（见 [data/minimal/default.yaml:25-35](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/default.yaml#L25-L35)），包含标量、列表、映射三种节点，还有一层嵌套：

```yaml
menu:
  page_size: 5
switcher:
  caption: 〔方案選單〕
  hotkeys:
    - Control+grave
    - F4
```

**任务步骤：**

1. **建树：** 用一张树形图（或缩进列表）表示这段 YAML 对应的 `ConfigItem` 树。根节点应是一个 `ConfigMap`。
2. **标类型：** 给每个节点标注它的具体子类型（`ConfigMap` / `ConfigList` / `ConfigValue`）以及 `ConfigItem::type()` 的返回值（`kMap` / `kList` / `kScalar`）。
3. **标内部值：** 对每个 `ConfigValue`，写出它内部 `value_` 字符串的实际内容；并说明若对 `page_size` 那个节点调用 `GetInt`、`GetDouble`、`GetString`、`GetBool` 各会返回什么。
4. **设计寻址：** 写出用 `ConfigItemRef` 链式访问到「第二个 hotkey（即 `"F4"`）」的表达式（形如 `config["..."]["..."][...]`），并指出沿途经过几次 `ConfigMapEntryRef` 与 `ConfigListEntryRef`。
5. **对照源码验证：** 打开 `ConvertFromYaml`（[src/rime/config/config_data.cc:252-286](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_data.cc#L252-L286)），确认它的「YAML 节点类型 → `ConfigItem` 子类型」分派与你画出的树一致：Scalar→`ConfigValue`、Sequence→`ConfigList`、Map→`ConfigMap`、Null→`nullptr`。

**参考答案（树形结构）：**

```
root  (ConfigMap, type=kMap)
├─ "menu" -> ConfigMap (type=kMap)
│           └─ "page_size" -> ConfigValue (type=kScalar, value_="5")
└─ "switcher" -> ConfigMap (type=kMap)
                 ├─ "caption" -> ConfigValue (type=kScalar, value_="〔方案選單〕")
                 └─ "hotkeys" -> ConfigList (type=kList)
                                 ├─ [0] ConfigValue (type=kScalar, value_="Control+grave")
                                 └─ [1] ConfigValue (type=kScalar, value_="F4")
```

- 对 `page_size` 节点（`value_ == "5"`）：`GetInt` 成功返回 `true`、值为 5；`GetDouble` 成功、值为 5.0；`GetString` 成功、值为 `"5"`；`GetBool` 失败返回 `false`。
- 寻址第二个 hotkey：`config["switcher"]["hotkeys"][1].ToString()`。沿途经过 2 次 `ConfigMapEntryRef`（`"switcher"`、`"hotkeys"`）和 1 次 `ConfigListEntryRef`（下标 `1`），最后 `.ToString()` 取出 `"F4"`。

> 若你已按 u1-l2 完成本地构建，可写一段最小程序：构造 `ConfigData`，调用 `LoadFromStream` 读入上面的 YAML，再用上述路径表达式打印各节点，与参考答案对照。若暂无条件运行，标注「待本地验证」。

## 6. 本讲小结

- librime 的配置在内存中是一棵以 `ConfigItem` 为公共基类的多态树；`type()` 返回 `kNull/kScalar/kList/kMap` 之一，且类型在构造期决定、运行期只读。
- `ConfigValue`（标量）内部只用一个 `string value_` 存所有标量——布尔、整数、浮点都先文本化，读取时再按 `GetBool/GetInt/GetDouble/GetString` 惰性解析，失败返回 `false`，体现「一切皆字符串 + 惰性类型化」。
- `ConfigList`（`vector`）按下标访问并支持越界自动扩张，`ConfigMap`（`std::map`）按键字典序存储且键必须为字符串；两者对不存在的位置都返回 `nullptr` 而非崩溃。
- `ConfigItemRef` 及其两个子类 `ConfigListEntryRef`/`ConfigMapEntryRef` 是「位置代理」，通过 `operator[]` 实现链式路径寻址，通过 `Is*/To*` 做类型检查与安全取值；写入会自动上报 `modified` 脏标记。
- `AsConfigItem` 模板用 `std::is_convertible` 在编译期分派，使 `= 5`（包成 `ConfigValue`）与 `= New<ConfigList>()`（原样使用）都能正确赋值；`AsMap()/AsList()` 在节点类型不符时自动创建空容器，保证沿深路径写入永远可行。
- 本讲只讲静态数据模型；YAML 如何加载成这棵树、`__include`/`__patch` 如何在树上做编译，留待 u4-l2、u4-l3。

## 7. 下一步学习建议

- **下一篇 u4-l2「Config::Component 与配置加载」**：精读 `config_component.h` 与 `config_data.cc`，看 `ConfigData::LoadFromStream` 如何调用 `ConvertFromYaml` 把 YAML 文本变成本讲所讲的 `ConfigItem` 树，以及 `ConfigComponentBase` 如何用 `weak_ptr` 缓存 `ConfigData` 实现复用。
- **u4-l3「配置编译器：`__include` / `__patch` DSL」**：在本讲的树结构基础上，进一步学习 `ConfigCompiler` 如何在加载期对树做合并、引用、写时拷贝（COW）等变换。
- **回看 u2-l3「Schema」**：带着本讲的数据模型，重新理解 `Schema = schema_id + Config` 中那个 `Config` 句柄其实就是指向本讲这棵 `ConfigItem` 树（的根）的引用，会更有体会。
- **建议阅读的源码**：[src/rime/config/config_types.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_types.cc) 全文（不足 300 行，建议通读一遍），以及 [src/rime/config/config_data.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_data.cc) 中的 `ConvertFromYaml`，把「YAML → ConfigItem 树」的最后一环补齐。
