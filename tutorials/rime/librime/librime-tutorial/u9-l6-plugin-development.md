# 插件开发实战

## 1. 本讲目标

本讲是学习手册的收官篇。读完后你应当能够：

- 看懂 librime 自带的 `sample` 插件，并能说清「一个共享库如何变成引擎里可用的组件」。
- 用 `RIME_REGISTER_MODULE` 宏把一批自定义组件打包成一个**模块（module）**，在模块的 `initialize` 钩子里用 `Registry::Register` 把组件登记进全局注册表。
- 从零实现一个自定义 **Translator**：继承 `Translator`、实现 `Query`、用 `SimpleCandidate` + `UniqueTranslation` 产出候选。
- 编写一个测试方案（`*.schema.yaml`），用处方串（prescription）引用你注册的组件，让引擎在装配流水线时把它实例化。
- 掌握插件共享库的两种构建形态（合并进主库 / 独立共享库）以及命名约定（`librime-*` / `rime-*`）。

本讲把前面学到的组件体系（u5-l1/u5-l2）、Ticket 与外部插件加载（u5-l4）、引擎流水线（u6）、Translator 基类契约（u6-l4）全部收口到一个**可运行的最小工程**里。

## 2. 前置知识

在动手前，请确认你理解下面这些「积木」（它们都来自前置讲义）：

- **组件（Component）与注册表（Registry）**：librime 用「数据驱动装配」。方案 YAML 里写的是组件名字，引擎运行时按名字到 `Registry` 单例里查工厂，再用 `Create(arg)` 造出对象。组件基类用 `Class<T, Arg>` 模板定义 `Create` 接口，默认工厂 `Component<T>` 把它实现为 `new T(arg)`（详见 u5-l1）。
- **四大组件基类**：`Processor` / `Segmentor` / `Translator` / `Filter`，都继承 `Class<T, const Ticket&>`、以 `Ticket` 构造。本讲只写 `Translator`（详见 u5-l2、u6-l4）。
- **Ticket**：组件实例化的上下文包，含 `engine` / `schema` / `name_space` / `klass`。处方串形如 `klass@alias`，`@` 左是 Registry 里的类名，右覆盖默认命名空间（详见 u5-l4）。
- **Translator 契约**：唯一纯虚函数 `Query(input, segment) -> an<Translation>`，返回一个惰性的候选流；`segment.HasTag(...)` 用来门控「这段输入该不该我来翻译」（详见 u6-l4）。
- **Translation / Candidate**：`Translation` 是候选迭代器（`Peek` 取当前、`Next` 前进、`exhausted` 判空）；`Candidate` 是一条候选（`text` 上屏文字、`start/end` 区间、`type` 类型标签、`comment`/`preedit` 可选）（详见 u3-l3）。
- **模块（Module）**：一批组件的「打包注册单元」+ `initialize`/`finalize` 生命周期钩子，注册进 `ModuleManager`。`RIME_REGISTER_MODULE` 宏约定函数名 `rime_<name>_initialize` / `rime_<name>_finalize`，并借助编译器构造器属性在库加载时自动登记（详见 u5-l3）。

一句话回顾**插件（plugin）与模块（module）的关系**（来自 u5-l4）：插件是一份共享库（或一段被合并进主库的目标代码），它注册成一个模块；模块被加载时，其 `initialize` 钩子把组件塞进 `Registry`。本讲就把这条链路亲手搭一遍。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [sample/src/sample_module.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/src/sample_module.cc) | 插件的「模块入口」：定义 `rime_sample_initialize`，在里面注册 `trivial_translator` 组件，并用 `RIME_REGISTER_MODULE(sample)` 收尾。 |
| [sample/src/trivial_translator.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/src/trivial_translator.h) | 自定义 Translator 的声明：继承 `Translator`，内嵌一个 `map<string,string>` 当词典。 |
| [sample/src/trivial_translator.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/src/trivial_translator.cc) | Translator 的实现：构造期填词典，`Query` 做最长匹配把拼音翻译成中文数字并产出候选。 |
| [sample/tools/sample.schema.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/tools/sample.schema.yaml) | 测试方案：在 `engine/translators` 里用处方串 `trivial_translator` 引用我们的组件。 |
| [sample/tools/sample_console.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/tools/sample_console.cc) | 终端前端：用 `RIME_MODULE_LIST` 声明加载 `default` + `sample` 两个模块，端到端跑通。 |
| [sample/test/trivial_translator_test.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/test/trivial_translator_test.cc) | 单元测试：展示「`Require` → `Create` → `Query` → `Peek`」的标准验证套路。 |
| [rime-new-plugin.sh](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/rime-new-plugin.sh) | 官方脚手架脚本：一键生成插件目录、`CMakeLists.txt`、模块入口和示例 Processor。 |
| [plugins/plugins_module.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/plugins/plugins_module.cc) | 运行期插件加载器 `PluginManager`：扫描 `rime-plugins/` 目录、用 `boost::dll` 加载、按文件名推算模块名。 |
| [sample/CMakeLists.txt](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/CMakeLists.txt) | 插件构建脚本：演示「独立库（`BUILD_SAMPLE`）」与「合并插件」两种产物形态。 |
| [src/rime_api.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h) | 定义 `RIME_REGISTER_MODULE` / `RIME_MODULE_LIST` / `RIME_MODULE_INITIALIZER` 等关键宏。 |

## 4. 核心概念与源码讲解

### 4.1 模块入口：把组件塞进 Registry

#### 4.1.1 概念说明

「插件」最核心的一句话是：**插件 = 一份代码 + 一个模块入口**。

一份插件源码里通常会定义若干个自定义组件类（比如 `TrivialTranslator`）。但这些类本身不会自动被引擎发现——引擎装配流水线时，是拿着方案 YAML 里的**名字**去 `Registry` 里查工厂的。所以插件必须做一件事：**在某个时机，把这些组件类的工厂登记进 `Registry`，并绑定一个名字**。

librime 用「**模块（module）**」来组织这件事。一个模块就是「一组组件的注册时机」加上两个生命周期钩子：

- `rime_<name>_initialize()`：模块被加载时调用，**在这里注册组件**。
- `rime_<name>_finalize()`：模块被卸载时调用，通常为空。

而把「这段代码声明成一个模块」靠的是宏 `RIME_REGISTER_MODULE(name)`。这个宏会展开出一个**编译期构造器**：当包含它的共享库被进程加载时，构造器自动执行，把模块登记进 `ModuleManager`。换句话说，你不需要显式调用任何注册函数——**加载即登记**。

之后，引擎在 `initialize` 时会加载默认模块组（或前端指定的模块列表），被加载模块的 `initialize` 钩子随即执行，组件就进了 `Registry`。这条链路在 u5-l3 已详细讲过；本节我们看它在 sample 里具体长什么样。

#### 4.1.2 核心流程

把一个组件送进引擎，需要走完下面四步（前两步由插件作者写，后两步由 librime 运行时自动完成）：

```text
[作者编写]
  1. 定义组件类 TrivialTranslator : public Translator
  2. 写 rime_sample_initialize()，里面：
       Registry::instance().Register(
           "trivial_translator",              // 方案里要写的名字
           new Component<sample::TrivialTranslator>);  // 默认工厂: new T(ticket)
     末尾加 RIME_REGISTER_MODULE(sample)

[运行时自动]
  3. 共享库被加载 → 宏展开的构造器执行 → 模块 "sample" 登记
  4. 引擎 initialize 加载模块 "sample" → 调用 rime_sample_initialize →
     组件 "trivial_translator" 进 Registry
  5. 引擎装配流水线读方案 engine/translators 里的 "trivial_translator" →
     Require("trivial_translator") → Create(ticket) → 得到 TrivialTranslator 实例
```

关键认知：`Register` 的第二个参数是一个**工厂对象**（`ComponentBase*`），所有权归 `Registry`（同名重复注册会 `delete` 旧工厂）。`Component<sample::TrivialTranslator>` 是默认工厂模板，它把 `Create(arg)` 实现为 `new T(arg)`，所以你不必手写工厂类。

#### 4.1.3 源码精读

sample 的模块入口极其简短，全部逻辑就三段：

[sample/src/sample_module.cc:16-24](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/src/sample_module.cc#L16-L24) 是整个插件的「心脏」——`rime_sample_initialize` 拿到全局 `Registry` 单例，注册一个名为 `trivial_translator` 的组件，工厂是默认模板 `Component<sample::TrivialTranslator>`；`rime_sample_finalize` 留空；末行 `RIME_REGISTER_MODULE(sample)` 把这一切声明成模块。

注册这一行展开后等价于：**「以后任何人用名字 `trivial_translator` 查 Registry，都能拿到一个能 `new TrivialTranslator(ticket)` 的工厂」**。

`Registry::Register` / `Find` 的签名见 [src/rime/registry.h:21-22](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/registry.h#L21-L22)：`Register(name, component)` 与 `Find(name)`。默认工厂模板 `Component<T>` 见 [src/rime/component.h:34-38](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/component.h#L34-L38)，`Create` 就是 `new T(arg)`；而 `Class<T,Arg>::Require(name)` 见 [src/rime/component.h:29-31](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/component.h#L29-L31)，它先 `Find` 再 `dynamic_cast` 还原成带类型的工厂。

`RIME_REGISTER_MODULE(sample)` 展开做了什么？见 [src/rime_api.h:541-552](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L541-L552)：它定义一个占位函数 `rime_require_module_sample`（供 `boost::dll` 定位库用，见 4.4），再用 `RIME_MODULE_INITIALIZER` 注册一个**构造器**——该构造器在库加载时自动执行，构造一个静态 `RimeModule` 结构体（填好 `module_name="sample"`、`initialize=rime_sample_initialize`、`finalize=rime_sample_finalize`），并调用 `RimeRegisterModule` 登记。构造器本身的平台实现见 [src/rime_api.h:524-533](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L524-L533)：GCC 下用 `__attribute__((constructor))`，MSVC 下用 `.CRT$XCU` 段。这就解释了「加载即登记」的底层原理。

> 注意宏强制的命名约定：模块名 `sample` 决定了初始化函数必须叫 `rime_sample_initialize`、finalize 必须叫 `rime_sample_finalize`。这正是 4.4 里 `plugin_name_of` 要把文件名里的 `-` 替换成 `_` 的原因——文件名（去前缀后）必须等于模块名。

#### 4.1.4 代码实践

**实践目标**：验证「模块入口里注册的名字，就是方案里能用的名字」。

**操作步骤（源码阅读型）**：

1. 打开 [sample/src/sample_module.cc:19](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/src/sample_module.cc#L19)，记下 `Register` 的第一个参数 `"trivial_translator"`。
2. 打开 [sample/tools/sample.schema.yaml:24-27](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/tools/sample.schema.yaml#L24-L27)，确认 `engine/translators` 列表里出现的正是 `trivial_translator`。
3. 打开 [sample/test/trivial_translator_test.cc:20-23](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/test/trivial_translator_test.cc#L20-L23)，确认测试用 `Translator::Require("trivial_translator")` 拿到的就是上面注册的工厂。

**需要观察的现象**：三处出现的字符串完全一致；名字是组件在整个系统里的唯一身份证。

**预期结果**：你能讲清「改 `Register` 的名字就必须同步改方案与测试，否则 `Require` 返回 `NULL`，装配期引擎只记一条 ERROR 然后跳过（见 u5-l4 的容错装配）」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 [sample_module.cc:19](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/src/sample_module.cc#L19) 改成 `r.Register("my_translator", ...)`，方案不动，会发生什么？

**答案**：引擎装配到 `engine/translators` 里的 `trivial_translator` 时，`Translator::Require("trivial_translator")` 返回 `NULL`，`CreateComponentsFromList` 记一条 ERROR 并跳过该处方，整个流水线照常运行但少了这个翻译器——输入数字拼音不会有候选。组件本身没坏，只是「名字对不上」。

**练习 2**：为什么 sample 把注册逻辑写在 `rime_sample_initialize` 里，而不是直接写在文件作用域的全局对象构造函数里？

**答案**：因为模块的**加载时机**由 `ModuleManager` 控制。写在 `rime_<name>_initialize` 里，可以保证「先加载、后注册」的顺序——只有当前端在 `RimeTraits::modules` 里点名要 `sample`、或它属于被加载的默认模块组时，组件才进 Registry。若写在全局构造里，则只要库被加载就会注册，失去了按需加载的能力，也与 librime 的模块生命周期模型不一致。

---

### 4.2 实现一个自定义 Translator

#### 4.2.1 概念说明

注册只是「挂个招牌」；真正的活儿在组件类本身。本节我们实现一个 **Translator**——流水线四类组件里最常被自定义的一类（因为「换一种翻译逻辑」≈「造一个新输入法」）。

`Translator` 的契约极简，只有一个纯虚函数 `Query`（见 u6-l4）：

```cpp
virtual an<Translation> Query(const string& input, const Segment& segment) = 0;
```

它的语义是：**「给你这段输入 `input`（对应 `segment` 这个切片），你能产出候选吗？能就返回一个 `Translation`，不能就返回 `nullptr`。」**

`TrivialTranslator` 的「业务逻辑」很可爱：它内置一张「拼音→中文数字」的小词典（`yi→一`、`er→二`……`qian→千`、`wan→萬`），用最长匹配把一串拼音翻译成中文数字。比如输入 `yibaiershisanwansiqianlingwushiliu` 会得到 `一百二十三萬四千零五十六`。

它示范了写任何 Translator 的三件套：

1. **构造**：从 `Ticket` 拿到 `engine_` 与 `name_space_`（基类已替你做），再初始化自己的状态（这里填词典）。
2. **门控**：`Query` 开头先判断「这段该不该我来翻译」——这里要求 segment 带 `abc` 这个 tag（即由 `abc_segmentor` 切出的字母段，详见 u6-l3）。
3. **产出候选**：把翻译结果包成一个 `SimpleCandidate`，再用 `UniqueTranslation`（单候选的 `Translation`）返回。

#### 4.2.2 核心流程

`Query` 的内部流程是一段「过滤 → 翻译 → 包装」的直链：

```text
Query(input, segment):
  if not segment.HasTag("abc"):        # 门控：只认字母段
      return nullptr
  output = Translate(input)            # 最长匹配查词典
  if output.empty():
      return nullptr                   # 查不到就不产出
  candidate = New<SimpleCandidate>(    # 造一条候选
      type="trivial", start=segment.start, end=segment.end,
      text=output, comment=":-)")
  return New<UniqueTranslation>(candidate)   # 包成单候选 Translation
```

`Translate` 的最长匹配算法（伪代码）：

```text
Translate(input):
  result = ""
  i = 0
  while i < len(input):
      # 从长到短尝试子串，取第一个命中的词典键
      for len from min(kMaxPinyinLength=6, remaining) down to kMinPinyinLength=2:
          if input[i : i+len] in dictionary_:
              result += dictionary_[input[i:i+len]]
              i += len; break
      else:   # 没有任何长度命中
          return ""    # 整段翻译失败
  return result
```

> 关键设计：**只要有一个音节查不到，整段就返回空、不产出候选**。这是一种「要么全对、要么不干」的策略，避免了把半截拼音误翻成数字。`SimpleCandidate` 的 `start/end` 直接取自 `segment`，表示这条候选消费了输入串的哪一段——引擎据此推进光标与切分（见 u3-l2/u3-l3）。

#### 4.2.3 源码精读

**声明**（[sample/src/trivial_translator.h:21-32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/src/trivial_translator.h#L21-L32)）：`TrivialTranslator` 继承 `Translator`，重写 `Query`，私有成员是一个 `using TrivialDictionary = map<string, string>;`。注意它放在 `namespace sample` 里，避免与 librime 内部的同名类冲突。

**构造与词典**（[sample/src/trivial_translator.cc:16-32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/src/trivial_translator.cc#L16-L32)）：构造函数先把基类 `Translator(ticket)` 初始化好（基类会从 ticket 取出 `engine_` 与 `name_space_`），然后逐条填写词典。中文数字以 UTF-8 的十六进制转义存放（如 `"\xe4\xb8\x80"` 即「一」），这样源文件保持纯 ASCII、避免编码问题。

**Query 与门控**（[sample/src/trivial_translator.cc:34-47](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/src/trivial_translator.cc#L34-L47)）：第 36 行 `if (!segment.HasTag("abc")) return nullptr;` 就是门控——它只接手由 `abc_segmentor` 切出来的字母段（见 u6-l3，`abc_segmentor` 会给字母段打上 `abc` tag）。第 44-45 行用 `New<SimpleCandidate>(...)` 造候选，第五个参数 `":-)"` 是候选的注释（comment），会显示在候选窗右侧；最后 `New<UniqueTranslation>(candidate)` 把单条候选包成一个 `Translation` 返回。

> `New<T>(...)` 是 librime 的智能指针工厂别名（来自 `common.h`），等价于 `std::make_shared`/`std::make_unique` 视 `an<T>` 而定；`an<T>` 即组件间传递的共享指针（见 u4-l1）。

**最长匹配**（[sample/src/trivial_translator.cc:49-72](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/src/trivial_translator.cc#L49-L72)）：外层 `while` 推进游标 `i`，内层 `for` 从长到短尝试 `input.substr(i, len)`；命中就累加结果并推进，整轮无命中则返回空串。

涉及的两个基类契约也值得对照看：

- `Translator` 基类见 [src/rime/translator.h:22-36](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translator.h#L22-L36)：`Translator` 继承 `Class<Translator, const Ticket&>`（这是它能被 `Require`/`Create` 的原因），构造函数从 ticket 取 `engine_` 与 `name_space_`，`Query` 是纯虚。
- `SimpleCandidate` 构造签名见 [src/rime/candidate.h:56-68](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/candidate.h#L56-L68)：参数依次是 `type, start, end, text, comment, preedit`。`text()` 是纯虚（上屏文字），`comment()`/`preedit()` 默认空串。
- `UniqueTranslation` 见 [src/rime/translation.h:41-52](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translation.h#L41-L52)：它持单条候选，构造时若候选为空则立即 `exhausted`；`Peek` 返回候选、`Next` 后置为耗尽。这是「只产一个候选」的最简 `Translation`。
- `Ticket` 结构见 [src/rime/ticket.h:17-30](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/ticket.h#L17-L30)：含 `engine` / `schema` / `name_space` / `klass`。

**单元测试**把这些串起来验证（[sample/test/trivial_translator_test.cc:18-49](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/test/trivial_translator_test.cc#L18-L49)）：`Translator::Require("trivial_translator")` 拿工厂 → `component->Create(ticket)` 造对象 → 造一个带 `abc` tag 的 `Segment` → `translator->Query(...)` → `translation->Peek()` 取候选 → 断言 `text()` 等于期望的中文数字串。这是验证任何自定义 Translator 的标准模板。

#### 4.2.4 代码实践

**实践目标**：亲手写一个返回固定候选的 Translator，跑通「注册 → 查询 → 取候选」。

**操作步骤**（以下为**示例代码**，非项目原有文件；你可以参照 sample 的目录结构新建）：

1. 新建 `greeter_translator.h`，仿照 [trivial_translator.h:21-32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/src/trivial_translator.h#L21-L32) 声明一个 `GreeterTranslator : public Translator`，重写 `Query`。
2. 新建 `greeter_translator.cc`，`Query` 的实现只做一件事：当 `segment.HasTag("abc")` 且 `input == "hi"` 时，返回一个 `SimpleCandidate`（`text="你好"`、`comment="greeting"`）包成的 `UniqueTranslation`，否则返回 `nullptr`。关键骨架（示例代码）：

   ```cpp
   an<Translation> GreeterTranslator::Query(const string& input,
                                            const Segment& segment) {
     if (!segment.HasTag("abc") || input != "hi")
       return nullptr;
     auto candidate = New<SimpleCandidate>("greeter", segment.start,
                                           segment.end, "你好", "greeting");
     return New<UniqueTranslation>(candidate);
   }
   ```

3. 在模块入口（仿照 [sample_module.cc:16-24](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/src/sample_module.cc#L16-L24)）里 `r.Register("greeter_translator", new Component<GreeterTranslator>);`，末尾 `RIME_REGISTER_MODULE(greeter)`。
4. 编一个最小测试（仿照 [trivial_translator_test.cc:18-49](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/test/trivial_translator_test.cc#L18-L49)）：`Require("greeter_translator")` → `Create` → `Query("hi", seg)`（seg 带 `abc` tag）→ `Peek()->text()` 应为 `"你好"`。

**需要观察的现象**：输入恰好 `"hi"` 时 `Peek()` 拿到候选、`text()` 为 `"你好"`；输入 `"hello"` 时 `Query` 返回 `nullptr`。

**预期结果 / 待本地验证**：`EXPECT_EQ("你好", candidate->text());` 通过。由于本实践依赖完整的 librime 构建环境（Boost、GTest 等），具体编译命令参照 `sample/README.md` 的 `BUILD_SAMPLE=ON` 流程；若暂无环境，至少把「`Require` → `Create` → `Query` → `Peek`」这条调用链在纸上走一遍，明确每一步的输入输出类型。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Query` 第 36 行要做 `segment.HasTag("abc")` 门控？去掉会怎样？

**答案**：流水线会把**每一个** segment 都拿去问所有 translator（见 u6-l1 的 `TranslateSegments`）。没有门控的话，`TrivialTranslator` 会尝试翻译标点段（`punct` tag）、兜底段（`raw` tag）等所有切片，可能把不该翻译的内容也翻成数字、或与 `punct_translator` 抢同一段。门控确保它只处理字母段，这正是 segmentor 写入的 tag 与 translator 之间的契约（见 u6-l3/u6-l4）。

**练习 2**：`New<UniqueTranslation>(candidate)` 与直接 `return New<SimpleCandidate>(...)` 在类型上有什么不同？为什么必须包一层 `Translation`？

**答案**：`Query` 的返回类型是 `an<Translation>`（候选**流**），而 `SimpleCandidate` 是单条 `Candidate`，二者不是一回事。`Menu` 用 `Translation` 的 `Peek`/`Next` 迭代器协议按需拉取候选（拉模型，见 u3-l4）。`UniqueTranslation` 是「只装一条候选」的最简 `Translation` 适配器，负责把单条 `Candidate` 包装成满足迭代器协议的流。

**练习 3**：`SimpleCandidate` 构造的第 5 个参数（这里传 `":-)"`）对应候选的什么字段？它会影响上屏文字吗？

**答案**：对应 `comment`（注释），定义见 [candidate.h:59-68](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/candidate.h#L59-L68)。它只用于在候选窗右侧显示提示，**不会**进入 `text()`，因此不影响实际上屏内容。

---

### 4.3 把组件接入方案

#### 4.3.1 概念说明

组件注册好了，怎么让引擎真正用它？答案是**在方案（schema）的 `engine` 段里写它的名字**。这一步把「代码里的组件」与「配置里的处方」对接起来，是「数据驱动装配」的临门一脚（见 u5-l4、u6-l1）。

回顾处方串的语法（来自 u5-l4）：清单里每一项形如 `klass` 或 `klass@alias`。`klass` 就是你在 `Registry::Register` 时用的名字；`@alias` 可选，用来覆盖默认命名空间（让同一个组件类按不同配置节多次实例化）。本例最简单：直接写裸名字 `trivial_translator`。

引擎装配流水线时，模板 `CreateComponentsFromList` 会遍历 `engine/translators` 列表，对每一项造一个 `Ticket`（`klass` 取自处方串），调 `Translator::Require(klass)` 查工厂、`Create(ticket)` 实例化，把结果塞进 `translators_` 容器。如果某个名字查不到（`Require` 返回 `NULL`），只记 ERROR 跳过，不崩溃（见 u5-l4 的容错装配）。

#### 4.3.2 核心流程

```text
方案 sample.schema.yaml:
  engine:
    translators:
      - punct_translator        # 内置：标点
      - trivial_translator      # ← 我们注册的组件名
      - echo_translator         # 内置：原样回显（兜底）

引擎 ApplySchema → CreateComponentsFromList(engine/translators):
  for each 处方串 in 列表:
      ticket.klass = "trivial_translator"        # @alias 缺省
      factory = Translator::Require("trivial_translator")   # 查 Registry
      if factory == NULL: log ERROR; continue    # 容错
      translators_.push_back(factory->Create(ticket))       # new TrivialTranslator
```

运行期，用户每输入一段字母（被 `abc_segmentor` 切成带 `abc` tag 的段），引擎就把它依次喂给 `translators_` 里的每个翻译器；`TrivialTranslator::Query` 命中就产出中文数字候选，与其他翻译器的候选一起进 `Menu` 归并、过滤、分页（见 u3-l4、u6-l5）。

#### 4.3.3 源码精读

[sample/tools/sample.schema.yaml:13-27](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/tools/sample.schema.yaml#L13-L27) 是整个 `engine` 段。注意四类组件的清单（processors / segmentors / translators / filters）分别驱动流水线的四个阶段（见 u6-l1）。其中 [第 24-27 行](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/tools/sample.schema.yaml#L24-L27) 的 `translators` 列表里，`trivial_translator` 夹在 `punct_translator` 与 `echo_translator` 之间。

[sample/tools/sample.schema.yaml:29-31](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/tools/sample.schema.yaml#L29-L31) 的 `speller` 段也值得一看：`alphabet` 倒序写全字母表，`delimiter: " '"`。这告诉 `speller` 处理器哪些键是合法拼写键（详见 u6-l2）；没有它，`speller` 不会把字母追加进 `context->input`，`abc_segmentor` 也就无字母段可切、`trivial_translator` 永远拿不到带 `abc` tag 的段。这揭示了流水线各组件的**强耦合协作**：少配一个处理器，下游翻译器就收不到输入。

终端前端 [sample/tools/sample_console.cc:129-142](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/tools/sample_console.cc#L129-L142) 展示了**模块如何被加载**：第 129 行用 `RIME_MODULE_LIST(sample_modules, "default", "sample");` 声明要加载 `default`（core+dict+gears，提供所有内置组件）和 `sample`（我们的插件）两个模块；第 136 行 `traits.modules = sample_modules;` 把它交给 API。`initialize` 时 librime 依次加载这两个模块，`sample` 的 `rime_sample_initialize` 随即把 `trivial_translator` 注册进 Registry——之后引擎装配方案时才能 `Require` 到它。`RIME_MODULE_LIST` 宏见 [src/rime_api.h:576](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L576)，它展开成一个以 `NULL` 结尾的 `const char*[]`。

#### 4.3.4 代码实践

**实践目标**：理解处方串与 `@alias` 命名空间的对应关系（承接 u5-l4）。

**操作步骤（源码阅读型）**：

1. 在 [sample.schema.yaml:24-27](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/tools/sample.schema.yaml#L24-L27) 中，`trivial_translator` 是裸名字（无 `@alias`），故其 `Ticket::name_space` 默认为 `"translator"`，`TrivialTranslator` 读配置时会到方案的 `translator` 节找——但本例它不读任何配置，所以无影响。
2. 对照真实方案里的带 alias 用法：`script_translator@pinyin` 表示 `klass="script_translator"`、`name_space="pinyin"`，该实例读方案的 `pinyin` 节（见 u6-l4）。这正是同一组件类多次实例化、各读不同配置的机制。
3. 思考：若想让 `trivial_translator` 的内置词典可由方案配置覆盖，你会怎么改？（提示：在 `Query`/构造里通过 `engine_->schema()->Config()` 按 `name_space_` 读取，参考 `TranslatorOptions` 的做法。）

**需要观察的现象**：裸名字与 `klass@alias` 两种写法的区别仅在于 `name_space` 是否被覆盖。

**预期结果**：你能说清「`trivial_translator` 不带 alias 是因为它不依赖方案配置；一旦组件要读配置，就需要用 alias 区分多个实例」。

#### 4.3.5 小练习与答案

**练习 1**：方案里 `engine/segmentors` 没有写 `abc_segmentor` 会怎样影响 `trivial_translator`？

**答案**：`abc_segmentor` 负责把字母输入切成带 `abc` tag 的段（见 u6-l3）。若不装配它，输入的字母不会被切成 `abc` 段，`TrivialTranslator::Query` 的 `segment.HasTag("abc")` 永远为假、永远返回 `nullptr`，中文数字候选永远不会出现。本方案的 [segmentors 列表](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/tools/sample.schema.yaml#L20-L23) 第一项正是 `abc_segmentor`。

**练习 2**：为什么 `sample_console` 要在 `RIME_MODULE_LIST` 里同时写 `"default"` 和 `"sample"`？只写 `"sample"` 行不行？

**答案**：`default` 模块组 = core + dict + gears，提供 `speller`/`abc_segmentor`/`punct_translator`/`express_editor` 等所有内置组件（见 u5-l3）。方案 [sample.schema.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/tools/sample.schema.yaml) 的 `engine` 段大量引用这些内置组件。只写 `"sample"` 的话，内置组件都没注册，装配时全部 `Require` 失败，流水线空转。`"sample"` 只补一个 `trivial_translator`，必须与 `"default"` 并列。

---

### 4.4 脚手架与构建：插件的两副面孔

#### 4.4.1 概念说明

写完代码还要「编进 librime」。插件有**两种构建形态**（见 u5-l4、`sample/README.md`）：

1. **合并插件（merged）**：在编译期把插件的目标文件（`.o`）链进主库 `rime`，模块随主库加载自动登记，前端无感。由 CMake 选项 `BUILD_MERGED_PLUGINS=ON` 控制。
2. **外部插件（external）**：编成独立共享库（如 `rime-sample.so`），运行期由 `PluginManager` 用 `boost::dll` 从 `rime-plugins/` 目录动态加载。由 `ENABLE_EXTERNAL_PLUGINS=ON` 控制。

两种形态最终都把组件送进同一个 `Registry`，区别只在「何时、如何进」。

理解外部插件的加载约定是关键（见 u5-l4）：

- `PluginManager` 扫描主库所在目录下的 `rime-plugins/` 子目录（目录名由 `RIME_PLUGINS_DIR` 定义，默认 `"rime-plugins"`）。
- 对每个共享库文件，按文件名推算模块名：去掉 `librime-` 或 `rime-` 前缀，把 `-` 替换成 `_`。例如 `librime-char-codec.so` → 模块名 `char_codec`。
- **文件名（去前缀后）必须等于模块名**，否则 `ModuleManager::Find` 查不到、只报警告。这是因为模块名还决定了初始化函数名 `rime_<module>_initialize`（见 4.1）。

`rime-new-plugin.sh` 是官方脚手架，输入一个名字就生成符合上述约定的插件骨架：目录、`CMakeLists.txt`、模块入口、一个示例 Processor。

#### 4.4.2 核心流程

外部插件从磁盘到 Registry 的完整旅程（`PluginManager::LoadPlugins`）：

```text
启动期 rime_plugins_initialize():
  PluginManager.LoadPlugins(<主库目录>/rime-plugins):
    for 每个共享库文件 in 目录:
        plugin_name = plugin_name_of(file)        # 去前缀、- 转 _
        boost::dll::shared_library(file)          # 加载 → 触发构造器 → 模块登记
        module = ModuleManager.Find(plugin_name)  # 按名查刚登记的模块
        if module: ModuleManager.LoadModule(module)   # 调 rime_<name>_initialize
        else: WARNING("module not provided")
```

关键点：`boost::dll` 加载共享库时，库里的全局构造器（`RIME_MODULE_INITIALIZER`）会执行，模块因此被登记进 `ModuleManager`；随后 `LoadModule` 触发 `initialize`，组件进 Registry。若文件名推算出的 `plugin_name` 与库内 `RIME_REGISTER_MODULE(name)` 的 `name` 不一致，`Find` 返回空，插件虽被加载却不生效——只多一条 WARNING。

#### 4.4.3 源码精读

**脚手架脚本** [rime-new-plugin.sh:8-15](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/rime-new-plugin.sh#L8-L15) 做名字规范化：先把传入名字的 `_` 转 `-`、去掉 `rime-` 前缀得到 `plugin_name`（如 `my-cool`），再算出 `plugin_module = my_cool`（`-` 转 `_`）。这正对应「文件名用连字符、模块名用下划线」的约定。脚本随后 `mkdir` 生成目录，并用 heredoc 写入三份模板：

- [rime-new-plugin.sh:19-36](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/rime-new-plugin.sh#L19-L36)：插件 `CMakeLists.txt` 模板。它把源码编成一个 `OBJECT` 库（`rime-<name>-objs`），再向上层 PARENT_SCOPE 暴露四个变量：`plugin_name`（库目标名 `rime-<name>`）、`plugin_objs`（目标对象）、`plugin_deps`（依赖，默认 `${rime_library}`）、`plugin_modules`（模块名 `<name>`）。这种「对象库 + 上抛变量」的写法让插件既能被合并（上层收集 `plugin_objs` 链进主库），也能被编成独立库（见 `plugins/CMakeLists.txt`）。
- [rime-new-plugin.sh:40-58](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/rime-new-plugin.sh#L40-L58)：模块入口模板，结构与 [sample_module.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/src/sample_module.cc) 一模一样——`rime_<module>_initialize` 里 `Registry::Register("todo_processor", new Component<TodoProcessor>);`，末尾 `RIME_REGISTER_MODULE(<module>)`。
- [rime-new-plugin.sh:60-90](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/rime-new-plugin.sh#L60-L90)：一个示例 `TodoProcessor`（Processor 基类的最小骨架），演示如何订阅 `context->update_notifier()` 信号——这是写 Processor 的常见模式（见 u6-l2）。

**运行期加载器** [plugins/plugins_module.cc:35-70](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/plugins/plugins_module.cc#L35-L70)（`PluginManager::LoadPlugins`）：遍历目录，跳过非共享库后缀（第 43 行用 `boost::dll::shared_library::suffix()` 判断），用 `plugin_name_of` 算模块名，`boost::dll::shared_library(file)` 加载（第 52 行，加载即触发构造器登记模块），再 `ModuleManager::Find(plugin_name)` 查模块、`LoadModule` 触发其 `initialize`（第 60-61 行）。查不到则 WARNING（第 64-65 行）。

**名字推算** [plugins/plugins_module.cc:72-84](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/plugins/plugins_module.cc#L72-L84)（`plugin_name_of`）：取 `stem()`（去后缀），去掉 `librime-` 或 `rime-` 前缀，再把 `-` 替换成 `_`。注释明确指出：「插件名是模块初始化函数名的一部分」，故必须用合法的 C 标识符字符（下划线）。

**入口与定位** [plugins/plugins_module.cc:96-129](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/plugins/plugins_module.cc#L96-L129)：非 Windows 下用 `dladdr`（第 104-113 行）定位 `rime_require_module_plugins` 这个符号所在的文件路径，从而找到主库目录；`rime_plugins_initialize`（第 122-125 行）在主库目录旁拼出 `rime-plugins/` 子目录交给 `LoadPlugins`。最后 [第 129 行](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/plugins/plugins_module.cc#L129) `RIME_REGISTER_MODULE(plugins)` 把加载器自身声明成一个模块——只有前端加载了 `plugins` 模块，外部插件才会被扫描。这也是为什么外部插件形态需要前端在 `RIME_MODULE_LIST` 里额外加 `"plugins"`（或它已被纳入默认模块组）。

**两种产物形态**的切换见 [sample/CMakeLists.txt:13-48](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/CMakeLists.txt#L13-L48)：`BUILD_SAMPLE=ON` 时编成独立库 `rime-sample`（带 VERSION/SOVERSION、可 install），并附带 console 与 test；否则（作为标准插件放入 `plugins/`）编成 `OBJECT` 库，向上层暴露 `plugin_*` 变量，由 [plugins/CMakeLists.txt:42-66](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/plugins/CMakeLists.txt#L42-L66) 根据 `BUILD_MERGED_PLUGINS` 决定合并进主库还是编成独立库（输出到 `lib/rime-plugins/`）。

> 小结 librime 的插件发现机制：标准插件把源码目录放进 `plugins/`（或用 `install-plugins.sh` 从 GitHub 拉取，见 [sample/README.md:63-79](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/README.md#L63-L79)），构建系统自动扫到它，**无需改动任何 librime 源码或主 CMakeLists**——这是 librime 插件生态可扩展性的根基。

#### 4.4.4 代码实践

**实践目标**：用脚手架生成一个插件骨架，理解命名约定。

**操作步骤**：

1. 在仓库根目录运行 `bash rime-new-plugin.sh my_plugin`（**待本地验证**，需本地仓库写权限）。
2. 观察生成的 `plugins/my-plugin/` 目录：应有 `CMakeLists.txt`、`src/my_plugin_module.cc`、`src/todo_processor.h`。
3. 打开 `src/my_plugin_module.cc`，确认 `RIME_REGISTER_MODULE(my_plugin)` 的模块名与文件名推算结果（`my-plugin` → `my_plugin`）一致。
4. 对照 [plugins/plugins_module.cc:72-84](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/plugins/plugins_module.cc#L72-L84)，手算：若该插件编成 `librime-my-plugin.so` 放进 `rime-plugins/`，`plugin_name_of` 会得到什么？（应为 `my_plugin`。）

**需要观察的现象**：脚本输出的 `plugin_name: rime-my-plugin`、`plugin_module: my_plugin`；模块入口里的函数名为 `rime_my_plugin_initialize`。

**预期结果**：你能在不看代码的情况下说清「文件名 `librime-my-plugin` ↔ 模块名 `my_plugin` ↔ 函数 `rime_my_plugin_initialize`」三者的一一对应关系。若本地无运行环境，这一步可作为纯阅读理解练习：把 [rime-new-plugin.sh:8-11](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/rime-new-plugin.sh#L8-L11) 的变量替换手动推演一遍即可。

#### 4.4.5 小练习与答案

**练习 1**：假设你把插件源码里的 `RIME_REGISTER_MODULE(sample)` 改成 `RIME_REGISTER_MODULE(cool)`，但共享库文件名仍是 `librime-sample.so`，外部加载时会发生什么？

**答案**：`plugin_name_of("librime-sample.so")` 推算出 `plugin_name = "sample"`，于是 `ModuleManager::Find("sample")` 查不到（库内实际登记的模块名是 `"cool"`），触发 [plugins_module.cc:64-65](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/plugins/plugins_module.cc#L64-L65) 的 WARNING「module 'sample' is not provided」，插件虽被加载、模块 `"cool"` 虽已登记，却不会被 `LoadModule`、组件不会进 Registry。修法：让文件名（去前缀）与 `RIME_REGISTER_MODULE` 的名字保持一致。

**练习 2**：合并插件（`BUILD_MERGED_PLUGINS=ON`）与外部插件（`ENABLE_EXTERNAL_PLUGINS=ON`）在「何时注册组件」上有何不同？

**答案**：合并插件的目标文件在**编译期**链进主库 `rime`，其模块构造器随主库进程启动而执行、模块自动登记；若该模块属于默认模块组，组件在 `initialize` 时就进 Registry，前端无需任何额外操作。外部插件则在**运行期**由 `PluginManager` 用 `boost::dll` 从 `rime-plugins/` 动态加载，加载后才登记模块；且需要前端加载 `plugins` 模块（或把它纳入模块列表）才会触发扫描。两者最终都进同一个 Registry，区别是「编译期 vs 运行期」「自动 vs 显式」。

**练习 3**：为什么 `plugin_name_of` 要把 `-` 替换成 `_`？

**答案**：因为模块名会被拼进 C 标识符 `rime_<module>_initialize` / `rime_<module>_finalize`（`RIME_REGISTER_MODULE` 宏的约定，见 [rime_api.h:541-552](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api.h#L541-L552)）。C 标识符不能含连字符，必须用下划线。所以「文件名用连字符（`my-plugin`，符合 Unix 库命名习惯）、模块名用下划线（`my_plugin`，符合 C 标识符规则）」并由 `plugin_name_of` 在两者间转换。

## 5. 综合实践

**任务**：参照 `sample` 插件，从零造一个 `rime-greeter` 插件，提供一个 `greeter_translator`——输入 `hi` 时返回固定问候语候选「你好，世界！」，并在一个测试方案里跑通。

**建议步骤**：

1. **生成骨架**：运行 `bash rime-new-plugin.sh greeter`（或参照 [sample/](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/src/sample_module.cc) 手建目录），得到 `plugins/greeter/`。
2. **写 Translator**：仿照 [trivial_translator.h:21-32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/src/trivial_translator.h#L21-L32) 与 [trivial_translator.cc:34-47](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/src/trivial_translator.cc#L34-L47)，实现 `GreeterTranslator::Query`：当 `segment.HasTag("abc") && input == "hi"` 时，返回 `New<UniqueTranslation>(New<SimpleCandidate>("greeter", segment.start, segment.end, "你好，世界！", "greeting"));`，否则返回 `nullptr`。
3. **注册组件**：在 `greeter_module.cc` 的 `rime_greeter_initialize` 里写 `r.Register("greeter_translator", new Component<GreeterTranslator>);`，末尾 `RIME_REGISTER_MODULE(greeter)`（模板见 [rime-new-plugin.sh:40-58](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/rime-new-plugin.sh#L40-L58)）。
4. **编测试方案**：仿照 [sample.schema.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/tools/sample.schema.yaml)，写一个 `greeter.schema.yaml`，`engine/translators` 里列 `- greeter_translator`，并保留 `speller`、`abc_segmentor` 等保证字母输入能流到你的翻译器。
5. **构建并验证**：按 `sample/README.md` 的 `BUILD_SAMPLE=ON` 流程构建（**待本地验证**），或在合并插件形态下重新构建 librime，然后用 console 输入 `hi`，观察候选窗出现「你好，世界！」。
6. **（可选）写单元测试**：仿照 [trivial_translator_test.cc:18-49](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/sample/test/trivial_translator_test.cc#L18-L49)，断言 `Query("hi", seg_with_abc_tag)->Peek()->text() == "你好，世界！"`。

**验收标准**：能讲清这条链路——`rime_greeter_initialize`（注册）→ 方案处方串 `greeter_translator`（引用）→ `Require`/`Create`（实例化）→ `Query`（产出候选）→ `Menu`（归并显示）。若本地无构建环境，至少把 1-4 步的代码与配置写出来，并在纸上追踪一次 `hi` 输入的完整旅程。

## 6. 本讲小结

- **插件 = 一份代码 + 一个模块入口**：用 `RIME_REGISTER_MODULE(name)` 把代码声明成模块，在 `rime_<name>_initialize` 里用 `Registry::Register("组件名", new Component<你的类>)` 登记组件；宏借编译器构造器实现「加载即登记」。
- **模块名与文件名严格对应**：文件名去 `librime-`/`rime-` 前缀、`-` 转 `_` 后必须等于 `RIME_REGISTER_MODULE` 的名字，否则外部加载时 `ModuleManager::Find` 查不到、只报警告。
- **写 Translator 的三件套**：继承 `Translator` 重写 `Query`；用 `segment.HasTag(...)` 做门控；用 `New<SimpleCandidate>(...)` + `New<UniqueTranslation>(...)` 产出候选。`SimpleCandidate` 的 `type/start/end/text/comment` 五参数是候选的核心字段。
- **门控是组件协作的契约**：`trivial_translator` 只认 `abc` tag，依赖 `abc_segmentor` 先切段、`speller` 先收键——少配任一环，候选就出不来。
- **接入方案靠处方串**：在 `engine/translators` 列裸名字（或 `klass@alias`），引擎装配时 `Require`→`Create` 实例化；查不到名字只记 ERROR 跳过，不崩溃。
- **两种构建形态共享同一 Registry**：合并插件（`BUILD_MERGED_PLUGINS`）编译期链进主库、自动登记；外部插件（`ENABLE_EXTERNAL_PLUGINS`）运行期由 `PluginManager` 用 `boost::dll` 从 `rime-plugins/` 动态加载。`rime-new-plugin.sh` 一键生成符合约定的骨架。

## 7. 下一步学习建议

- **读真实插件**：`sample` 只是玩具；建议阅读社区插件 [librime-lua](https://github.com/hchunhui/librime-lua)（用 Lua 脚本写组件，`RIME_REGISTER_CUSTOM_MODULE` 的真实用例）、`librime-char-codec`、`librime-predict` 等，看它们如何实现 Processor / Filter / 用 `get_api` 扩展 C API。
- **写其他三类组件**：本讲只写了 Translator。仿照 [rime-new-plugin.sh:60-90](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/rime-new-plugin.sh#L60-L90) 的 `TodoProcessor` 写一个 Processor（订阅 `context->update_notifier()`），或写一个 Filter（用装饰器模式包装候选流，见 u6-l5 的 `simplifier`）。
- **深入组件基类**：回头精读 [translator.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translator.h)、[processor.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/processor.h)、[filter.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/filter.h)，理解四大基类的对称设计与 `Class<T, const Ticket&>` 模板，尝试写一个需要从方案配置读取参数的组件（用 `engine_->schema()->Config()` 按 `name_space_` 取值，参考 `gear/script_translator.cc` 的 `TranslatorOptions`）。
- **回顾整条主线**：至此你已走完「按键 → Processor → Segmentor → Translator → Filter → Menu → 候选」的完整流水线（u6），也理解了它依赖的配置系统（u4）、组件体系（u5）、算法层（u7）与词典层（u8）。可以尝试用本讲学的插件机制，替换流水线中任意一个环节，观察输入法行为的整体变化——这是把全书知识融会贯通的最佳方式。
