# u5-l3 Module 机制与模块组

## 1. 本讲目标

在上一篇 u5-l2 里，我们已经认识了引擎流水线依赖的四种组件基类（`Processor`/`Segmentor`/`Translator`/`Filter`），并知道它们都「以 `Ticket` 构造、由方案配置驱动实例化」。但还遗留两个关键问题：

1. 这些组件类（`Speller`、`ScriptTranslator`、`Simplifier`……）是**什么时候、被谁**注册进 `Registry`，从而让方案 YAML 里写一个名字就能 `Require` 到？
2. `rime_api` 的 `initialize` 只接收一个 `RimeTraits`，并没有让前端手动列举「我要哪些组件」，那 librime 是怎么知道该启用哪些组件的？

本讲就来回答这两个问题。读完本讲你应当能够：

- 说出 **Module（模块）** 与上一篇讲的 **Component（组件）** 的区别：模块是「一批组件的注册单元 + 一对 init/finalize 生命周期钩子」，组件是模块在 `initialize` 时往 `Registry` 里塞的一个个工厂。
- 读懂 `RIME_REGISTER_MODULE` / `RIME_REGISTER_CUSTOM_MODULE` / `RIME_REGISTER_MODULE_GROUP` 三个注册宏，并解释「库被加载时自动注册」是靠什么语言机制实现的。
- 画出「`RimeInitialize` → `LoadModules(kDefaultModules)` → `default` 模块组 → 依次加载 `core`/`dict`/`gears`」的完整加载链。
- 说出 `core`/`dict`/`gears`/`levers` 四个内置模块各注册了哪些组件，并能对照一份真实方案 YAML 印证这些组件名。

## 2. 前置知识

本讲建立在以下已建立的认知之上（不再重复展开）：

- **组件注册体系（u5-l1）**：`Registry` 是进程级单例，一张「名字 → 工厂」的表；`Class<T,Arg>::Require(name)` 按名取工厂，`new Component<MyClass>` 是最常见的注册方式。
- **四大组件基类（u5-l2）**：`Processor`/`Segmentor`/`Translator`/`Filter` 都继承 `Class<T, const Ticket&>`，构造契约统一，因此 `engine.cc` 用一个模板 `CreateComponentsFromList` 通吃四类装配。
- **自版本化结构体（u1-l4）**：跨 C 边界的结构体首字段为 `data_size`，由 `RIME_STRUCT_INIT` 填充，库据此判断字段是否存在。

还需要补充两个本讲会用到的 C/C++ 小知识：

- **构造期自动执行（constructor attribute / `.CRT$XCU` 段）**：在共享库被加载（或静态对象初始化）时，编译器/链接器会自动调用某些「构造函数」。librime 利用这一点，让每个 `*_module.cc` 在库一加载时就把自己注册好，前端无需手动调用。
- **Boost.Preprocessor 序列**：一种用预处理器做循环的技巧，librime 用它把一个模块名列表（如 `(core)(dict)(gears)`）展开成多次函数调用。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `src/rime_api.h` | 定义 `RimeModule` 结构体、`RimeRegisterModule`/`RimeFindModule`，以及三个注册宏 `RIME_REGISTER_MODULE`、`RIME_REGISTER_CUSTOM_MODULE`、`RIME_REGISTER_MODULE_GROUP` 和 `RIME_MODULE_LIST`。 |
| `src/rime/module.h` / `module.cc` | `ModuleManager` 单例：维护「名字 → 模块」表与「已加载模块」集合，提供 `Register`/`Find`/`LoadModule`/`UnloadModules`。 |
| `src/rime/setup.h` / `setup.cc` | 声明并定义 `kDefaultModules`/`kDeployerModules`/`kLegacyModules` 三张模块名表，定义 `default`/`deployer` 两个**模块组**，以及 `LoadModules(const char*[])` 入口。 |
| `src/rime_api.cc` | C 边界的 `RimeRegisterModule`/`RimeFindModule`，以及静态库构建时显式声明模块依赖的 `rime_declare_module_dependencies`。 |
| `src/rime_api_impl.h` | `RimeInitialize` 等弃用 C API 的实现，体现「`LoadModules(kDefaultModules)` → `StartService`」的加载时序。 |
| `src/rime/core_module.cc` | 注册 `core` 模块：`config_builder`/`config`/`schema`/`user_config` 四个配置类组件。 |
| `src/rime/gear/gears_module.cc` | 注册 `gears` 模块：流水线上几乎所有 Processor/Segmentor/Translator/Filter 的具体实现。 |
| `src/rime/dict/dict_module.cc` | 注册 `dict` 模块：词典、用户词典、各种 db 后端、纠错器。 |
| `src/rime/lever/levers_module.cc` | 注册 `levers` 模块（自定义模块示例）：全部部署任务组件，并导出 `get_api`。 |
| `data/minimal/luna_pinyin.schema.yaml` | 实践用的对照方案：它的 `engine/{processors,segmentors,translators,filters}` 用到的名字全部来自 `gears` 模块。 |

## 4. 核心概念与源码讲解

### 4.1 Module 抽象：RimeModule 结构体与 ModuleManager

#### 4.1.1 概念说明

先做一个区分，这是本讲最容易混淆的地方：

- **Component（组件）** 是引擎流水线里的「零件」，比如 `speller`、`script_translator`、`simplifier`。它们各自独立、按方案配置按需实例化，注册进的是 `Registry`。
- **Module（模块）** 是「一批组件的**打包注册单元**」，外加一对 `initialize()` / `finalize()` 生命周期钩子。模块本身**不是**引擎零件，它更像一个「插件包」：被加载时，它的 `initialize()` 会往 `Registry` 里塞一大把组件，被卸载时 `finalize()` 做清理。

为什么要引入 Module 这一层？因为组件太多了（光 `gears` 就注册了 30 多个），如果把注册代码散落在各处、靠全局对象构造顺序去保证，既不可控也不可扩展。Module 把「相关的一组组件」聚到一个翻译单元（一个 `*_module.cc`）里，由一个统一的 `ModuleManager` 负责加载与去重，于是：

- 核心/字典/齿轮/部署四类组件天然分到四个模块，对应四个文件，关注点分离；
- 前端只需声明「我要加载 `default` 这套模块」，不必关心里面有哪些组件；
- 外部插件（u5-l4 详讲）可以作为一个新模块挂进来，扩展点非常干净。

#### 4.1.2 核心流程

`RimeModule` 是一个跨 C 边界的 POD 结构体，关键字段如下（详见源码精读）：

```
RimeModule {
  data_size;          // 自版本化标记（同 u1-l4）
  module_name;        // 模块名，如 "core" / "gears"
  initialize();       // 加载时回调：通常在这里 Register 一批组件
  finalize();         // 卸载时回调
  get_api();          // 可选：导出模块自定义的 C API（仅自定义模块用）
}
```

`ModuleManager` 是个单例，它维护两张表：

- `map_`：`名字 → RimeModule*`，用于按名查找；
- `loaded_`：`已加载模块指针集合`，用于保证 `initialize()` 只被调用一次（幂等）。

加载一个模块的流程可以概括为：

```text
LoadModule(module):
  if module == nullptr 或 module 已在 loaded_ 中: 直接返回   # 幂等
  把 module 插入 loaded_
  if module.initialize 存在:
      module.initialize()          # ← 真正去 Registry::Register 组件的地方
  else:
      打印 WARNING（缺少 initialize）
```

#### 4.1.3 源码精读

先看 `RimeModule` 结构体本身（[src/rime_api.h:244-251](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L244-L251)）：

```cpp
typedef struct rime_module_t {
  int data_size;
  const char* module_name;
  void (*initialize)(void);
  void (*finalize)(void);
  RimeCustomApi* (*get_api)(void);
} RimeModule;
```

这正是上面流程里的字段表——名字、两个生命周期函数指针、一个可选的 `get_api`。`data_size` 是自版本化标记，和 `RimeTraits`、`RimeApi` 一样遵循 u1-l4 讲过的「首字段长度」约定。

再看 `ModuleManager` 的接口（[src/rime/module.h:18-37](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/module.h#L18-L37)）：

```cpp
class ModuleManager {
 public:
  void Register(const string& name, RimeModule* module);
  RimeModule* Find(const string& name);
  void LoadModule(RimeModule* module);
  void UnloadModules();
  static ModuleManager& instance();
 private:
  using ModuleMap = map<string, RimeModule*>;
  ModuleMap map_;                       // 名字 → 模块
  std::unordered_set<RimeModule*> loaded_;  // 已加载集合
};
```

最核心的 `LoadModule` 实现（[src/rime/module.cc:25-37](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/module.cc#L25-L37)）：

```cpp
void ModuleManager::LoadModule(RimeModule* module) {
  if (!module || loaded_.find(module) != loaded_.end()) {
    return;                    // 幂等：同一个模块重复 Load 只生效一次
  }
  DLOG(INFO) << "loading module: " << module->module_name;
  loaded_.insert(module);
  if (module->initialize != NULL) {
    module->initialize();      // 真正的注册动作发生在这里
  } else {
    LOG(WARNING) << "missing initialize() function in module: "
                 << module->module_name;
  }
}
```

注意三个细节：① 用**指针**而不是名字去重，避免同名模块被误判；② `initialize` 为空时只是 WARNING 不崩溃，体现容错；③ `LoadModule` **不**做按名查找，查找是 `Find` 的职责（见 [src/rime/module.cc:17-23](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/module.cc#L17-L23)）。卸载则遍历 `loaded_` 调每个模块的 `finalize`（[src/rime/module.cc:39-46](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/module.cc#L39-L46)）。

#### 4.1.4 代码实践

**目标**：理解 `Register`（登记）与 `LoadModule`（触发 initialize）是两个独立动作。

**步骤**：

1. 打开 [src/rime/module.cc:13-23](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/module.cc#L13-L23)，确认 `Register` 只做 `map_[name] = module`，**不**调用 `initialize`，也**不**写入 `loaded_`。
2. 对比 `LoadModule`：它只读 `loaded_`、写 `loaded_`，**不**碰 `map_`。

**需要观察的现象**：一个模块可以先被 `Register` 进 `map_` 而长时间不被 `LoadModule`，此时它的组件尚未注册进 `Registry`，方案装配会因 `Require` 失败而跳过。这就是「登记」与「激活」的分离。

**预期结果**：能用自己的话说出——`Register` 让模块「可被按名找到」，`LoadModule` 才让模块「真正干活」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `loaded_` 用 `unordered_set<RimeModule*>` 而不是 `set<string>`（按名字）去重？

**参考答案**：因为模块名在 `Register` 时才与指针关联，而 `LoadModule` 接收的是裸指针（可能来自 `Find` 也可能来自外部直接传入），用指针去重不依赖名字查找，更直接；同时也避免「同名但不同实例」的边界问题。

**练习 2**：如果一个模块的 `initialize` 函数指针为 `NULL`，`LoadModule` 会怎样？

**参考答案**：仍会把它加入 `loaded_`（标记为已加载），但打印一条 `missing initialize() function` 的 WARNING，不调用任何初始化逻辑，也不会崩溃。

---

### 4.2 注册宏机制：RIME_REGISTER_MODULE 与构造期自动注册

#### 4.2.1 概念说明

光有 `ModuleManager` 还不够——谁来「创建 `RimeModule` 实例并调用 `Register`」？如果让每个 `*_module.cc` 手写一个全局对象，跨编译单元的构造顺序是不可控的。librime 的方案是用一组宏，配合**库加载时的自动构造机制**，做到：

- 每个 `*_module.cc` 末尾只写一行 `RIME_REGISTER_MODULE(名字)`；
- 这一行展开后，会生成一个静态 `RimeModule` 实例，并在库被加载时自动调用 `RimeRegisterModule(&module)` 把它登记进 `ModuleManager`；
- 同时要求该文件提供一对约定名字的函数 `rime_<名字>_initialize()` 和 `rime_<名字>_finalize()`，它们会被填进 `module.initialize` / `module.finalize`。

#### 4.2.2 核心流程

先看「自动构造」是怎么落地的。`RIME_MODULE_INITIALIZER` 宏（[src/rime_api.h:523-533](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L523-L533)）在 GCC 下用 `__attribute__((constructor))`，在 MSVC 下把函数指针放进 `.CRT$XCU` 段。两种平台的效果一致：**在共享库被加载（或程序启动）时，无需任何人调用，该函数就会自动执行**。

基于它，`RIME_REGISTER_MODULE(name)`（[src/rime_api.h:541-552](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L541-L552)）展开后的等价伪代码是：

```cpp
// 1) 生成一个「要求链接该模块」的占位符号（静态库构建用，见 4.3）
void rime_require_module_<name>() {}

// 2) 一个构造期自动执行的注册函数
static void rime_register_module_<name>() {
  static RimeModule module = {0};          // 只构造一次
  if (!module.data_size) {                 // 首次进入才赋值
    RIME_STRUCT_INIT(RimeModule, module);  // 填 data_size
    module.module_name = #name;            // 字符串化形参
    module.initialize = rime_<name>_initialize;  // 约定命名
    module.finalize   = rime_<name>_finalize;
  }
  RimeRegisterModule(&module);             // 登记进 ModuleManager
}
```

所以一个模块的「完整身份」由三部分拼出来：① 宏参数 `name`；② 该文件定义的 `rime_<name>_initialize` / `rime_<name>_finalize` 两个函数；③ 构造期自动登记。

#### 4.2.3 源码精读

`RIME_REGISTER_MODULE` 的真实定义（[src/rime_api.h:541-552](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L541-L552)）：

```cpp
#define RIME_REGISTER_MODULE(name)                       \
  void rime_require_module_##name() {}                   \
  RIME_MODULE_INITIALIZER(rime_register_module_##name) { \
    static RimeModule module = {0};                      \
    if (!module.data_size) {                             \
      RIME_STRUCT_INIT(RimeModule, module);              \
      module.module_name = #name;                        \
      module.initialize = rime_##name##_initialize;      \
      module.finalize = rime_##name##_finalize;          \
    }                                                    \
    RimeRegisterModule(&module);                         \
  }
```

注意 `static RimeModule module` 加上 `if (!module.data_size)` 的组合：即使构造函数因为某些原因被调用多次（比如静态库显式 declare 依赖时），`module` 也只会在第一次被填充，保证幂等。

`RimeRegisterModule` 是 C 边界的一层薄封装（[src/rime_api.cc:63-68](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.cc#L63-L68)），它把校验（名字非空）后委托给 `ModuleManager::Register`：

```cpp
RIME_API Bool RimeRegisterModule(RimeModule* module) {
  if (!module || !module->module_name)
    return False;
  ModuleManager::instance().Register(module->module_name, module);
  return True;
}
```

`core_module.cc` 是最典型的用法：文件里定义 `rime_core_initialize`（往 `Registry` 塞组件），末尾一行 `RIME_REGISTER_MODULE(core)`（[src/rime/core_module.cc:49](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/core_module.cc#L49)）就完成了「构造期自动登记为 `core` 模块」。`initialize` 里的注册逻辑见 [src/rime/core_module.cc:19-43](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/core_module.cc#L19-L43)，下一节会展开。

还有一个增强版宏 `RIME_REGISTER_CUSTOM_MODULE`（[src/rime_api.h:557-571](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L557-L571)）：它多了一个 `rime_customize_module_<name>(RimeModule*)` 钩子，让你能在登记前给模块挂上 `get_api` 之类的额外字段。`levers` 模块就是用它导出部署专用的 C API（见 4.4）。

#### 4.2.4 代码实践

**目标**：通过「宏展开」理解一行宏如何变成「函数约定 + 自动登记」。

**步骤**：

1. 把 `RIME_REGISTER_MODULE(core)` 在脑中按 4.2.2 的伪代码展开。
2. 在 `core_module.cc` 里找到它**约定**的两个函数名：`rime_core_initialize`（[L19](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/core_module.cc#L19)）和 `rime_core_finalize`（[L45](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/core_module.cc#L45)）。
3. 验证命名规律：宏参数 `core` → `rime_core_initialize` / `rime_core_finalize` / `rime_require_module_core`。

**需要观察的现象**：`core_module.cc` 里**没有任何**显式调用 `RimeRegisterModule` 的代码，登记完全由宏在构造期自动完成。

**预期结果**：能解释「为什么前端从不调用任何 `register_module_core` 之类的函数，`core` 模块却已经能被 `Find("core")` 找到」——因为库一加载，构造函数就替你登记好了。

#### 4.2.5 小练习与答案

**练习 1**：如果某个 `*_module.cc` 忘了定义 `rime_<name>_initialize`，会发生什么？

**参考答案**：`RIME_REGISTER_MODULE` 展开时会引用 `rime_##name##_initialize` 这个符号，链接阶段就会报「未定义引用」错误——即在编译期/链接期就被拦下，而不是留到运行期。

**练习 2**：`RIME_REGISTER_MODULE` 与 `RIME_REGISTER_CUSTOM_MODULE` 的本质区别是什么？

**参考答案**：前者只填 `module_name`/`initialize`/`finalize` 三个字段；后者额外调用一个用户提供的 `rime_customize_module_<name>(&module)` 钩子，允许在登记前改写 `RimeModule` 的其他字段（典型是挂 `get_api`）。所以需要导出模块级 C API（如 `levers`）时用后者，普通模块用前者。

---

### 4.3 模块组与加载入口：default / deployer / legacy

#### 4.3.1 概念说明

有了模块，还需要回答「该加载哪些模块」。librime 用**模块名表**和**模块组**两层来组织：

- **模块名表**：一个以 `NULL` 结尾的 `const char*[]`，列出一串模块名。`kDefaultModules` 是运行时默认加载的表，`kDeployerModules` 是部署时用的表。
- **模块组（module group）**：一种「假模块」。它本身不注册任何组件，它的 `initialize` 只是去加载一组别的模块。`default` 组 = `core + dict + gears`，`deployer` 组 = `core + dict + levers`。

把 `default` 也建模成一个模块，带来一个巧妙的好处：前端只要说「加载 `default` 这一个名字」，`LoadModules` 就会顺着组的 `initialize` 把 `core`/`dict`/`gears` 全部加载完——统一的加载接口，无需特判「组」与「普通模块」。

#### 4.3.2 核心流程

`RIME_REGISTER_MODULE_GROUP(name, ...)` 宏（[src/rime_api.h:582-588](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L582-L588)）展开后的等价伪代码：

```cpp
// 一张以 NULL 结尾的名字表
static const char* rime_<name>_module_group[] = { __VA_ARGS__, NULL };

static void rime_<name>_initialize() {
  LoadModules(rime_<name>_module_group);   // 加载组里的每个模块
}
static void rime_<name>_finalize() {}
RIME_REGISTER_MODULE(name)                 // 复用普通模块宏登记自己
```

于是 `RIME_REGISTER_MODULE_GROUP(default, "core", "dict", "gears")`（[setup.cc:45](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/setup.cc#L45)）就造出一个叫 `default` 的模块，它被 `LoadModule` 时会回调 `rime_default_initialize`，进而 `LoadModules({"core","dict","gears",NULL})`。

加载入口 `LoadModules`（[src/rime/setup.cc:48-55](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/setup.cc#L48-L55)）非常简洁：

```cpp
void LoadModules(const char* module_names[]) {
  ModuleManager& mm(ModuleManager::instance());
  for (const char** m = module_names; *m; ++m) {     // 遍历到 NULL 为止
    if (RimeModule* module = mm.Find(*m)) {           // 按名找
      mm.LoadModule(module);                          // 加载（幂等）
    }
  }
}
```

注意它对「找不到的名字」**静默跳过**——这给了前端用 `traits->modules` 自定义模块列表时很大的弹性：可以列一个尚不存在的模块名而不报错。

完整的运行时加载链（从 C API 视角）：

```text
前端调用 rime_get_api()->initialize(traits)
  └─ RimeInitialize(traits)                      [rime_api_impl.h:51-56]
       ├─ SetupDeployer(traits)                  填各数据目录
       ├─ LoadModules( traits->modules ? traits->modules : kDefaultModules )
       │     └─ 对 "default" 调 LoadModule
       │           └─ default.initialize()
       │                 └─ LoadModules({"core","dict","gears"})
       │                       ├─ core.initialize()   → Register 配置类组件
       │                       ├─ dict.initialize()   → Register 词典组件
       │                       └─ gears.initialize()  → Register 流水线组件
       └─ Service::instance().StartService()
```

而部署期走的是另一张表 `kDeployerModules`（只有 `"deployer"` 一项，见 [setup.cc:42](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/setup.cc#L42)），它对应的 `deployer` 组（[setup.cc:46](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/setup.cc#L46)）= `core + dict + levers`——把 `gears` 换成 `levers`，因为部署期不需要流水线组件，却需要一堆部署任务。

#### 4.3.3 源码精读

三张模块名表的定义在 [src/rime/setup.cc:36-43](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/setup.cc#L36-L43)：

```cpp
RIME_DLL RIME_MODULE_LIST(kDefaultModules,
                          "default" _RIME_SEQ_FOR_EACH(_RIME_MODULE_STR,
                                                       ~,
                                                       RIME_EXTRA_MODULES));
RIME_DLL RIME_MODULE_LIST(kDeployerModules, "deployer");
RIME_MODULE_LIST(kLegacyModules, "legacy");
```

`RIME_MODULE_LIST(var, ...)`（[rime_api.h:576](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L576)）就是 `const char* var[] = {__VA_ARGS__, NULL}`。`kDefaultModules` 里那个 `_RIME_SEQ_FOR_EACH(...)` 是给打包发行版用的：构建时通过 `RIME_EXTRA_MODULES` 注入额外的插件模块名（如 `lua`、`charcode`），让它们随 `default` 一起加载。普通构建里 `RIME_EXTRA_MODULES` 为空，所以 `kDefaultModules` 实际就是 `{"default", NULL}`。

两个模块组的定义紧接着（[setup.cc:45-46](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/setup.cc#L45-L46)）：

```cpp
RIME_REGISTER_MODULE_GROUP(default, "core", "dict", "gears")
RIME_REGISTER_MODULE_GROUP(deployer, "core", "dict", "levers")
```

加载时机则在 C API 实现里。`RimeInitialize`（[src/rime_api_impl.h:51-56](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L51-L56)）用了一个三目表达式决定加载哪张表：

```cpp
void RimeInitialize(RimeTraits* traits) {
  SetupDeployer(traits);
  LoadModules(RIME_PROVIDED(traits, modules) ? traits->modules
                                             : kDefaultModules);
  Service::instance().StartService();
}
```

也就是说：前端如果在 `traits->modules` 里给了自定义列表（比如只要 `core`），就按前端的来；否则走默认的 `kDefaultModules`。`RIME_PROVIDED` 正是 u1-l4 讲过的「字段存在且非空」判断。部署入口 `RimeDeployerInitialize`（[rime_api_impl.h:107-111](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L107-L111)）和 `RimeStartMaintenance`（[rime_api_impl.h:66](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L66)）则用 `kDeployerModules`。

> **关于静态库的一个细节**：共享库里模块靠构造期自动登记；但静态库链接时，未被引用的目标文件可能被优化掉，导致「构造函数没执行、模块没登记」。为此静态库构建路径里有 `rime_declare_module_dependencies()`（[src/rime_api.cc:49-55](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.cc#L49-L55)），它显式调用每个 `rime_require_module_*()` 占位函数来「强制链接」四个内置模块。共享库构建时这个函数是空的（[rime_api.cc:22-23](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.cc#L22-L23)）。

#### 4.3.4 代码实践

**目标**：跟踪一条从 C API 到组件注册的完整加载链。

**步骤**：

1. 从 [rime_api_impl.h:51-56](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L51-L56) 的 `RimeInitialize` 出发。
2. 跟到 [setup.cc:48-55](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/setup.cc#L48-L55) 的 `LoadModules`，确认它遍历到 `NULL` 结束。
3. 注意它 `Find("default")` 后 `LoadModule`，触发 `default` 模块的 `initialize`，即 `LoadModules({"core","dict","gears"})`。

**需要观察的现象**：`gears` 的 `initialize`（`rime_gears_initialize`）正是在这条链的最末端被调用，那一刻所有流水线组件才进入 `Registry`。在此之前，方案 YAML 里写的 `speller`、`script_translator` 等名字都还「查无此人」。

**预期结果**：能用一句话概括——「`RimeInitialize` 决定加载哪张模块名表，模块组把多个模块收拢成一个名字，`LoadModules` 递归把它们的 `initialize` 全部跑一遍」。

**待本地验证**：若你在本地构建并运行 `tools/rime_api_console`（u1-l5），可以在日志里依次看到 `registering core components.`、`registering components from module 'dict'.`、`registering components from module 'gears'.` 三条 INFO，正好对应这条链的三个 `initialize`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `default` 组包含 `gears`，而 `deployer` 组用 `levers` 替换了 `gears`？

**参考答案**：运行时（输入会话）需要流水线组件（Processor/Segmentor/Translator/Filter），它们全在 `gears` 里；部署期不跑流水线，却需要 `levers` 里的一堆部署任务组件（`schema_update`、`workspace_update` 等）。两组都共享 `core`（配置系统）和 `dict`（词典），因为两边都要读写配置和词典。

**练习 2**：前端在 `traits->modules` 里传 `{"core", NULL}`（只要 core），会发生什么？`speller` 还能用吗？

**参考答案**：`LoadModules` 只加载 `core`，`Registry` 里只有配置类组件，没有 `speller`/`script_translator` 等。装配引擎时这些名字 `Require` 失败，`engine.cc` 会记 ERROR 并跳过，引擎基本无法正常工作。这印证了「模块加载决定了哪些组件可用」。

---

### 4.4 四大内置模块各注册了什么

#### 4.4.1 概念说明

现在把四个内置模块 `core`/`dict`/`gears`/`levers` 的注册清单摊开来看。它们的 `initialize` 函数都是「拿 `Registry::instance()`，然后一连串 `r.Register(name, new Component<...>)`」。理解这张清单的意义在于：**方案 YAML `engine` 段里出现的每一个组件名，都必须能在某个已加载模块的这张清单里找到，否则装配失败**。

#### 4.4.2 核心流程

四个模块的分工可以画成一张表：

| 模块 | 文件 | 主要注册的组件类别 | 典型组件名 |
| --- | --- | --- | --- |
| `core` | `core_module.cc` | 配置系统（`Config` 组件） | `config_builder`、`config`、`schema`、`user_config` |
| `dict` | `dict_module.cc` | 词典、用户词典、db 后端、纠错 | `dictionary`、`user_dictionary`、`userdb`、`tabledb`、`corrector` |
| `gears` | `gears_module.cc` | 流水线四类组件 + formatter | `speller`、`script_translator`、`simplifier`、`uniquifier`… |
| `levers` | `levers_module.cc` | 部署任务（`DeploymentTask` 组件） | `schema_update`、`workspace_update`、`user_dict_sync`… |

其中 `gears` 是最「热闹」的，按流水线四类分组，下面源码精读会完整列出。

#### 4.4.3 源码精读

**core 模块**（[src/rime/core_module.cc:19-43](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/core_module.cc#L19-L43)）注册的是 u4 系列讲过的配置系统。注意它注册的不是普通 `new Component<T>`，而是带资源提供者和插件 lambda 的工厂：

```cpp
auto config_builder = new ConfigComponent<ConfigBuilder>([&](ConfigBuilder* b) {
  b->InstallPlugin(new AutoPatchConfigPlugin);
  b->InstallPlugin(new DefaultConfigPlugin);
  // ... u4-l4 讲过的六个配置插件
});
r.Register("config_builder", config_builder);
r.Register("config", config_loader);
r.Register("schema", new SchemaComponent(config_loader));   // 复用 config_loader
r.Register("user_config", user_config);
```

`schema` 组件复用了 `config_loader`，这正是 u2-l3 讲过的 `SchemaComponent` 适配器。

**gears 模块**（[src/rime/gear/gears_module.cc:39-91](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/gears_module.cc#L39-L91)）按四类整理如下（与源码注释分组一致）：

| 类别 | 注册的组件名 |
| --- | --- |
| processors | `ascii_composer`、`chord_composer`、`express_editor`、`fluid_editor`、`fluency_editor`(`fluid_editor` 别名)、`key_binder`、`navigator`、`punctuator`、`recognizer`、`selector`、`speller`、`shape_processor` |
| segmentors | `abc_segmentor`、`affix_segmentor`、`ascii_segmentor`、`matcher`、`punct_segmentor`、`fallback_segmentor` |
| translators | `echo_translator`、`punct_translator`、`table_translator`、`script_translator`、`r10n_translator`(`script_translator` 别名)、`reverse_lookup_translator`、`schema_list_translator`、`switch_translator`、`history_translator` |
| filters | `simplifier`、`uniquifier`、`charset_filter`、`cjk_minifier`(`charset_filter` 别名)、`reverse_lookup_filter`、`single_char_filter` |
| formatters | `shape_formatter` |

两个值得注意的细节：① 注册 `charset_filter` 前先 `r.Find("charset_filter")` 判断（[gears_module.cc:82-84](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/gears_module.cc#L82-L84)），允许插件提供「改进版」覆盖默认实现；② 别名（`fluency_editor`→`FluidEditor`、`r10n_translator`→`ScriptTranslator`、`cjk_minifier`→`CharsetFilter`）让历史方案配置仍可工作。`simplifier` 用的是专用工厂 `SimplifierComponent` 而非 `new Component<Simplifier>`，因为它要按 `option_name`/`opencc_config` 等子配置实例化（详见 u6-l5）。

**dict 模块**（[src/rime/dict/dict_module.cc:22-43](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_module.cc#L22-L43)）注册了 db 后端（`tabledb`/`stabledb`/`plain_userdb`/`userdb`，其中 `userdb` 默认是 LevelDb 实现）、纠错器 `corrector`、门面 `dictionary`/`reverse_lookup_dictionary`/`user_dictionary`，以及维护任务 `userdb_recovery_task`。这些是 u8 词典系列的主角。

**levers 模块**（[src/rime/lever/levers_module.cc:15-31](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/levers_module.cc#L15-L31)）注册的全部是 `DeploymentTask` 组件：`detect_modifications`、`installation_update`、`workspace_update`、`schema_update`、`config_file_update`、`prebuild_all_schemas`、`user_dict_upgrade`、`cleanup_trash`、`user_dict_sync`、`backup_config_files`、`clean_old_log_files`——u9-1/u9-2 会逐一展开。它用的是 `RIME_REGISTER_CUSTOM_MODULE(levers)`（[levers_module.cc:38-40](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/levers_module.cc#L38-L40)），在钩子里挂了 `module->get_api = rime_levers_get_api`，从而导出部署专用的 C API——这是 4.2 讲的「自定义模块」的真实样例。

#### 4.4.4 代码实践

**目标**：印证「方案 YAML 里的组件名都能在某个模块的注册清单里找到」。

**步骤**：

1. 打开 [data/minimal/luna_pinyin.schema.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml) 的 `engine` 段（[L39-68](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L39-L68)）。
2. 取出 processors 列表：`ascii_composer`、`recognizer`、`key_binder`、`speller`、`punctuator`、`selector`、`navigator`、`express_editor`。
3. 到 [gears_module.cc:45-57](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/gears_module.cc#L45-L57) 的 processors 注释段逐一勾对。

**需要观察的现象**：八个 processor 名字在 `gears` 的注册清单里**全部命中**（`express_editor` 在 [L48](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/gears_module.cc#L48)）。

**预期结果**：你会得出结论——`luna_pinyin` 方案能跑，前提是 `gears` 模块已被加载。这也解释了为什么 `default` 组必须包含 `gears`。

#### 4.4.5 小练习与答案

**练习 1**：方案里写 `fluency_editor` 和 `fluid_editor` 效果一样吗？为什么 librime 要同时注册两个名字？

**参考答案**：效果完全一样，二者都映射到 `new Component<FluidEditor>`（[gears_module.cc:49-50](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/gears_module.cc#L49-L50)）。`fluency_editor` 是 `fluid_editor` 的别名，保留它是为了让历史上写过 `fluency_editor` 的旧方案配置继续可用。

**练习 2**：`levers` 模块为什么用 `RIME_REGISTER_CUSTOM_MODULE` 而不是普通的 `RIME_REGISTER_MODULE`？

**参考答案**：因为它需要导出模块级的 C API（`get_api`），普通宏不会设置该字段；自定义宏提供的 `rime_customize_module_levers(&module)` 钩子正好用来挂 `module->get_api = rime_levers_get_api`（[levers_module.cc:38-40](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/levers_module.cc#L38-L40)）。

---

## 5. 综合实践

把本讲主要内容串起来，做一个「**按四类整理 `gears` 注册的组件，并对照 `luna_pinyin` 方案印证**」的源码阅读型实践。

**实践目标**：亲手验证「方案 YAML 的 `engine` 配置 = 一份对 `gears` 模块所注册组件名的采购清单」，从而把 Module（注册源）↔ Component（被采购的零件）↔ 方案配置（采购单）三者的关系彻底打通。

**操作步骤**：

1. 打开 [src/rime/gear/gears_module.cc:39-91](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/gears_module.cc#L39-L91)，按源码里的四类注释（`// processors` / `// segmentors` / `// translators` / `// filters`），把每个 `r.Register(...)` 的第一个参数抄成四张清单。注意区分别名（`fluency_editor`、`r10n_translator`、`cjk_minifier`）和真正独立的类。

2. 打开 [data/minimal/luna_pinyin.schema.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml)，读 `engine` 段（[L39-68](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L39-L68)）。注意带 `@` 的条目，如 `affix_segmentor@pinyin`、`table_translator@cangjie`、`simplifier@zh_simp`——`@` 前是**组件名**（要在清单里找），`@` 后是 **namespace**（u5-l4 的 Ticket 会讲怎么拆）。

3. 用下表做对照（已为你填好 `luna_pinyin` 实际用到的名字，请你到清单里逐一勾对）：

   | 方案段落 | 用到的名字（去掉 `@xxx`） | 应命中清单 |
   | --- | --- | --- |
   | processors | ascii_composer, recognizer, key_binder, speller, punctuator, selector, navigator, express_editor | gears / processors |
   | segmentors | ascii_segmentor, matcher, abc_segmentor, affix_segmentor, punct_segmentor, fallback_segmentor | gears / segmentors |
   | translators | punct_translator, reverse_lookup_translator, script_translator, table_translator | gears / translators |
   | filters | reverse_lookup_filter, simplifier, uniquifier | gears / filters |

4. **反向检查**：在 `gears` 清单里挑一个 `luna_pinyin` **没有**用到的组件（比如 `chord_composer`、`history_translator`、`single_char_filter`），思考：如果想启用它，应该在方案 YAML 的哪一段加上它的名字？

**需要观察的现象**：

- 方案 `engine` 段里出现的所有组件名，去掉 `@` 后都能在 `gears` 模块对应的分类清单里找到，无一例外。
- 带别名的名字（如理论上可用 `r10n_translator` 代替 `script_translator`）同样能命中。
- `core`/`dict`/`levers` 模块注册的组件名**不会**出现在 `engine` 流水线配置里——它们分别服务于「配置加载」「词典查询」「部署任务」，由引擎的其它代码路径按需 `Require`，而不是写进方案的 `engine` 段。

**预期结果**：你能用自己的话总结出——**方案 YAML 的 `engine` 段是一张只面向 `gears` 模块的采购单**；而 `core`/`dict`/`levers` 的组件是「基础设施」，由 librime 内部按需取用。这正好解释了为什么运行时只要加载 `default = core + dict + gears` 就够了。

**待本地验证**：可选地，把 `engine/processors` 里某个名字（如 `speller`）故意拼错成 `spellerr`，重新部署并运行（u1-l5 的 console），观察日志里会出现一条 `ERROR` 级别的 `Required component 'spellerr' not found.` 类信息，引擎装配跳过该项——这反向印证了「名字必须命中模块注册清单」。

## 6. 本讲小结

- **Module ≠ Component**：模块是「一批组件的打包注册单元 + init/finalize 钩子」，注册进 `ModuleManager`；组件是模块在 `initialize` 时往 `Registry` 里塞的工厂，注册进 `Registry`。
- `RimeModule` 是跨 C 边界的 POD（`module_name`/`initialize`/`finalize`/`get_api`）；`ModuleManager` 用 `map_`（名字→模块）做查找、用 `loaded_`（指针集合）做幂等去重，`LoadModule` 只触发 `initialize`、不查找。
- `RIME_REGISTER_MODULE` 靠 `__attribute__((constructor))` / `.CRT$XCU` 段实现「库加载即自动登记」，并约定 `rime_<name>_initialize` / `rime_<name>_finalize` 两个函数名；`RIME_REGISTER_CUSTOM_MODULE` 额外提供挂 `get_api` 的钩子。
- 模块组是「假模块」：`RIME_REGISTER_MODULE_GROUP(default,"core","dict","gears")` 让 `default` 的 `initialize` 等价于加载这三个模块；运行时表 `kDefaultModules` 默认只含 `"default"`，部署表 `kDeployerModules` 含 `"deployer"`（= core + dict + levers）。
- 完整加载链：`RimeInitialize` → `LoadModules(kDefaultModules)` → `default.initialize` → 依次 `core`/`dict`/`gears` 的 `initialize` → 各组件进入 `Registry` → `StartService`。
- 四个内置模块分工清晰：`core`＝配置系统，`dict`＝词典与 db 后端，`gears`＝流水线四类组件，`levers`＝部署任务（且用自定义模块导出 `get_api`）。方案 YAML 的 `engine` 段只采购 `gears` 里的组件名。

## 7. 下一步学习建议

- **横向**：本讲只讲了「内置模块怎么注册」，下一讲 **u5-l4 Ticket 与外部插件加载** 会讲 `Ticket` 如何把 `affix_segmentor@pinyin` 这种带 namespace 的处方串拆解、以及 `PluginManager` 如何用 `boost::dll` 把 `librime-*` 外部共享库当作新模块加载——那是 Module 机制面向二次开发的延伸。
- **纵向**：进入第 6 单元 **按键处理流水线**，从 **u6-l1 引擎流水线总览** 开始，你会看到本讲注册的 `gears` 组件如何被 `engine.cc` 的 `CreateComponentsFromList` 装配成一条真正的按键处理流水线。
- **补读源码**：想看一个最小化的「自定义模块 + 自定义组件」范例，可以直接读 `sample/src/sample_module.cc`（u9-l6 会以此为模板），它用本讲学的 `RIME_REGISTER_MODULE` 注册了一个只含一个 `trivial_translator` 组件的模块，是理解「如何往 librime 里加自己的组件」的最短路径。
