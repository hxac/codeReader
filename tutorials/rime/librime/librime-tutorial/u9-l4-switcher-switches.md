# Switcher 与 Switches

## 1. 本讲目标

本讲讲解 librime 的「方案切换器」与「开关配置模型」。读完后你应该能够：

- 说清 `Switcher` 为什么是一个**同时继承 `Processor` 和 `Engine`** 的特殊对象，以及它如何以「第一个处理器」的身份嵌进主引擎、又如何用 `active_engine` 旁路把自己的菜单暴露给前端。
- 看懂方案 YAML 里 `switches` 段两种写法——`name:`（toggle，二态开关）与 `options:`（radio group，单选组）——在 `Switches` 类里是如何被统一解析成 `SwitchOption` 的。
- 解释开关状态的三个时刻：部署期不参与、运行期 `reset` 初始化、用户切换后经 `save_options` 持久化到 `user.yaml` 的 `var/option/*`。
- 理解 `SwitcherCommand` 这一命令模式如何把「选一个候选」翻译成「换方案 / 翻开关」的实际动作。

本讲是 [u9-l3 Customizer 与用户设置](u9-l3-customizer-settings.md) 的承接篇：u9-l3 讲的是用户如何用 `*.custom.yaml` 改配置，本讲讲的是用户在**输入过程中**如何用快捷键实时切换方案与开关，以及这些切换的状态如何被记住。

## 2. 前置知识

在进入源码前，先用通俗语言对齐几个概念。

- **方案（schema）与开关（option/switch）**：方案是「换一套输入法」（如从拼音切到仓颉），开关是「在当前输入法里翻一个状态位」（如中/英文、简/繁、全/半角）。两者都是运行期可切换的。
- **toggle（二态开关）与 radio group（单选组）**：toggle 像「电灯开关」，只有开/关两态，用一个布尔 option 表示（如 `ascii_mode`）。radio group 像「收音机波段」，一组里只能选中一个（如 `zh_trad / zh_simp / zh_hk / zh_tw` 四选一），用多个布尔 option 表示，选中其一时要同时关掉其余。
- **`active_engine` 旁路**：回顾 [u2-l2](u2-l2-service-and-session.md)，主引擎持有一个 `active_engine_` 指针，`Session::context()` 返回的是 `active_engine()->context()`。正常情况下 `active_engine_` 为空、`active_engine()` 返回主引擎自身；当切换器激活时，它把自己设为 `active_engine_`，于是前端读到的上下文就变成了切换器的菜单。这是「同一时刻只让一个引擎的界面露出来」的关键。
- **`user.yaml`**：用户级配置文件，由 `user_config` 组件加载，存放运行期产生的持久状态，键形如 `var/option/<name>`、`var/previously_selected_schema`。它和 [u9-l3](u9-l3-customizer-settings.md) 讲的 `*.custom.yaml` 不同：后者是用户主动写的配置补丁，前者是引擎自动写入的运行状态。
- **三态返回值**：回顾 [u5-l2](u5-l2-component-base-classes.md) / [u6-l1](u6-l1-pipeline-overview.md)，处理器 `ProcessKeyEvent` 返回 `kAccepted`（吃掉按键，结束本次派发）、`kRejected`（短路跳出，交还系统默认）、`kNoop`（放行给下一个处理器）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/rime/switcher.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switcher.h) | 声明 `Switcher`（多重继承 `Processor` + `Engine`）与抽象命令 `SwitcherCommand`。 |
| [src/rime/switcher.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switcher.cc) | 切换器的按键处理、激活/退出、菜单刷新、设置加载、组件装配。 |
| [src/rime/switches.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switches.h) | 声明 `Switches` 配置解析器、`SwitchOption` 结构、`SwitchType` 枚举。 |
| [src/rime/switches.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switches.cc) | 把 YAML `switches` 列表解析成 `SwitchOption`，状态标签读取。 |
| [src/rime/engine.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc) | 主引擎如何持有 `switcher_`、把它插到处理器链首位、在 `ApplySchema` 时通知它。 |
| [src/rime/gear/schema_list_translator.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/schema_list_translator.cc) | 「方案列表」翻译器，产出 `SchemaSelection` 候选（一种 `SwitcherCommand`）。 |
| [src/rime/gear/switch_translator.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/switch_translator.cc) | 「开关」翻译器，产出 `Switch` / `RadioOption` 候选，并真正执行 `set_option` 与持久化。 |
| [data/minimal/default.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/default.yaml) | 切换器自身的配置（`switcher/caption`、`hotkeys`、`save_options` 等）。 |
| [data/minimal/luna_pinyin.schema.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml) | 一个真实方案的 `switches` 段，本讲实践对照样本。 |

## 4. 核心概念与源码讲解

### 4.1 Switcher 的双重身份：既是 Processor 又是 Engine

#### 4.1.1 概念说明

`Switcher` 是 librime 里最「奇特」的对象：它同时是一个**处理器**（`Processor`）和一个**引擎**（`Engine`）。这个多重继承不是炫技，而是为了同时满足两个需求：

1. **作为 Processor**：它必须能截获按键——用户按 `Control+grave`（反引号）要能弹出方案菜单。所以它得嵌进主引擎的处理器链。
2. **作为 Engine**：它要能独立地「组词」——方案菜单本身就是一串候选词（每个方案、每个开关都是一条候选）。引擎的本质就是「按键 → 候选」，方案菜单恰好符合这个模型，所以切换器复用了整套引擎机制（自己的 `Context`、`Schema`、翻译器、菜单）。

换句话说，**切换器是一个跑在主引擎内部的迷你引擎**。

#### 4.1.2 核心流程

切换器的生命周期围绕 `active_` 标志展开：

```
[未激活 active_=false]
   用户按 hotkey (Control+grave / F4 ...)
        │ Switcher::ProcessKeyEvent 命中 hotkey
        ▼
   Activate()
     ├─ set_option("_fold_options", fold_options_)
     ├─ RefreshMenu()          // 让翻译器产出方案/开关候选
     ├─ engine_->set_active_engine(this)  // 旁路：前端改读切换器的上下文
     └─ active_ = true
        │
[已激活 active_=true]
   后续按键：
     ├─ 再按 hotkey → HighlightNextSchema()（循环高亮下一个方案）
     ├─ Space/Return → context_->ConfirmCurrentSelection()（选中并触发 OnSelect）
     ├─ Escape → Deactivate()
     └─ 其余键交给切换器自己的 processors_（key_binder/selector），多数返回 kAccepted（吃掉）
        │
   选中一条候选 → OnSelect → command->Apply(this) → DeactivateAndApply(...)
        │
[回到未激活] engine_->set_active_engine()  // 清空旁路，前端重新读主引擎
```

关键点：切换器激活后，**主引擎的按键流水线并没有停下**——`Switcher` 仍是处理器链的一员，只是它对几乎所有键都返回 `kAccepted`，把它们「吃掉」，于是排在它后面的 `speller` 等处理器收不到这些键，主引擎就不会去组词。同时通过 `set_active_engine(this)`，前端读 `Session::context()` 时拿到的是切换器的菜单上下文。

#### 4.1.3 源码精读

多重继承的声明：[src/rime/switcher.h:20](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switcher.h#L20)

```cpp
class Switcher : public Processor, public Engine {
```

这一行同时继承了两个基类。注意继承顺序——`Processor` 在前。于是 `Switcher` 同时拥有两组成员：

- 来自 `Processor`（见 [processor.h:37-38](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/processor.h#L37-L38)）：`engine_`（指向**主引擎**）、`name_space_`。
- 来自 `Engine`（见 [engine.h:42-45](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.h#L42-L45)）：`schema_`、`context_`、`sink_`、`active_engine_`（这是**切换器自己**的引擎状态）。

> 阅读切换器代码时务必区分这两个「引擎」：方法里写 `engine_->...` 指的是**主引擎**（Processor 的成员），而 `context_->...`、`schema_->...` 指的是**切换器自己**（Engine 的成员）。

构造函数：[src/rime/switcher.cc:23-32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switcher.cc#L23-L32)

```cpp
Switcher::Switcher(const Ticket& ticket) : Processor(ticket) {
  context_->set_option("dumb", true);  // not going to commit anything
  context_->select_notifier().connect([this](Context* ctx) { OnSelect(ctx); });
  user_config_.reset(Config::Require("user_config")->Create("user"));
  InitializeComponents();
  LoadSettings();
}
```

它显式初始化 `Processor` 基类（`Engine` 基类走默认构造），给自己 的 `context_` 打上 `dumb`（哑）选项——切换器的菜单**永远不会真正上屏提交**，选中候选走的是 `OnSelect` 信号而非 `commit`。`user_config_` 加载的是 `user.yaml`（`"user"` 资源）。注意构造期只做了「装配组件 + 读设置」，并没有激活。

主引擎在构造时直接 `new` 出切换器，**不走组件注册表**：[src/rime/engine.cc:87-89](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L87-L89)

```cpp
switcher_ = New<Switcher>(this);
// saved options should be loaded only once per input session
switcher_->RestoreSavedOptions();
```

这里 `New<Switcher>(this)` 把主引擎指针 `this` 传进去，靠 `Ticket(Engine*, ...)` 的隐式转换（[ticket.h:27-29](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/ticket.h#L27-L29)）变成 `Ticket`，于是切换器的 `Processor::engine_` 就指向了主引擎。切换器是**每个主引擎独占一个**（`an<Switcher> switcher_`，[engine.cc:55](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L55)），而非全局单例。

切换器在装配时被**插到处理器链最前**：[src/rime/engine.cc:336-337](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L336-L337)

```cpp
if (switcher_) {
  processors_.push_back(switcher_);
```

这保证任何按键都先经过切换器，它才能在第一时间截获 hotkey。

按键处理的核心：[src/rime/switcher.cc:51-81](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switcher.cc#L51-L81)

```cpp
ProcessResult Switcher::ProcessKeyEvent(const KeyEvent& key_event) {
  for (const KeyEvent& hotkey : hotkeys_) {
    if (key_event == hotkey) {
      if (!active_ && engine_)       Activate();
      else if (active_)              HighlightNextSchema();
      return kAccepted;
    }
  }
  if (active_) {
    for (auto& p : processors_) { ... }   // key_binder / selector
    if (key_event.release() || key_event.ctrl() || key_event.alt())
      return kAccepted;
    int ch = key_event.keycode();
    if (ch == XK_space || ch == XK_Return)  context_->ConfirmCurrentSelection();
    else if (ch == XK_Escape)               Deactivate();
    return kAccepted;                       // 吃掉其余键
  }
  return kNoop;                             // 未激活：放行给主引擎流水线
}
```

这正是 4.1.2 流程图的代码化身：未激活时对非 hotkey 一律 `kNoop`（让 `speller` 等正常工作）；激活后几乎全部 `kAccepted`（独占按键）。注意 `switcher.h:25-27` 把 `ProcessKey` 转发给 `ProcessKeyEvent`，是为了同时满足 `Processor::ProcessKey` 和 `Engine::ProcessKey` 两个虚函数签名。

激活与旁路：[src/rime/switcher.cc:243-249](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switcher.cc#L243-L249)

```cpp
void Switcher::Activate() {
  context_->set_option("_fold_options", fold_options_);
  RefreshMenu();
  engine_->set_active_engine(this);   // 关键：把主引擎的 active_engine_ 指向自己
  active_ = true;
}
```

`engine_->set_active_engine(this)` 这一句是旁路的开关。结合 [service.cc:59-65](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.cc#L59-L65)：

```cpp
Context* Session::context() const {
  return engine_ ? engine_->active_engine()->context() : NULL;
}
```

以及 [engine.h:34](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.h#L34) 的 `active_engine()` 定义（`active_engine_ ? active_engine_ : this`），就能看清：激活后前端拿到的 `Session::context()` 是**切换器自己的 `context_`**（方案菜单），而非主引擎的输入上下文。退出时 [switcher.cc:251-262](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switcher.cc#L251-L262) 调 `engine_->set_active_engine()`（传空）把旁路复位。

#### 4.1.4 代码实践

**实践目标**：跟踪切换器从「未激活」到「激活」再到「退出」时，主引擎 `active_engine_` 指针的变化，验证旁路机制。

**操作步骤**：

1. 打开 [src/rime/engine.cc:87](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L87)，确认主引擎构造期就创建了 `switcher_`，且 `active_engine_` 初值为 `nullptr`（[engine.h:45](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.h#L45)）。
2. 在 [switcher.cc:247](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switcher.cc#L247) 的 `engine_->set_active_engine(this);` 下方加一行日志：`LOG(INFO) << "switcher took over active_engine";`。
3. 在 [switcher.cc:253](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switcher.cc#L253) 的 `Deactivate()` 里 `engine_->set_active_engine();` 下方加：`LOG(INFO) << "switcher released active_engine";`。

**需要观察的现象**：运行 `rime_api_console`，按 `Control+grave` 弹出方案菜单时应看到「took over」日志；按 `Escape` 退出时应看到「released」日志。

**预期结果**：弹出菜单期间，`Session::context()` 返回的是切换器的上下文（菜单里有方案候选）；退出后恢复正常输入。**待本地验证**（需自行编译并连接 glog）。

#### 4.1.5 小练习与答案

**练习 1**：切换器为什么必须继承 `Engine`，而不只是 `Processor`？

> 参考答案：因为方案菜单本身就是一组候选词，需要完整的「组词」能力（`Context` + 翻译器 + 菜单）。继承 `Engine` 让切换器直接复用这套机制，把「列方案」「列开关」实现成普通翻译器产出的候选，而不必为切换器另写一套候选生成逻辑。

**练习 2**：切换器激活后，主引擎的 `speller` 还会收到字母键吗？为什么？

> 参考答案：不会。切换器在处理器链最前，激活后对字母键返回 `kAccepted`，主引擎的 `ProcessKey` 循环遇到 `kAccepted` 立即 `return true`（[engine.cc:106-107](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L106-L107)），后续处理器（含 `speller`）拿不到这些键。

### 4.2 方案切换菜单与 SwitcherCommand 命令模式

#### 4.2.1 概念说明

切换器激活后显示的菜单里有两类候选：

- **方案候选**（由 `schema_list_translator` 产出）：每条代表一个可切换的输入方案。
- **开关候选**（由 `switch_translator` 产出）：每条代表一个可翻转的开关。

这些候选有一个共同点：被选中时，都要执行一个**动作**（换方案 / 翻开关），而不是把文字上屏。librime 用 **`SwitcherCommand` 命令模式**统一这种「选中即执行」的语义：每条特殊候选同时是一个 `SwitcherCommand`，带一个 `Apply(Switcher*)` 虚函数。用户确认选中 → `OnSelect` 把候选 `As<SwitcherCommand>` 取出 → 调 `Apply` 执行实际动作。

#### 4.2.2 核心流程

```
Switcher::RefreshMenu()
  └─ 对 translators_ 里每个翻译器 Query("") → AddTranslation 进 Menu
       ├─ schema_list_translator → SchemaListTranslation → [SchemaSelection, ...]
       └─ switch_translator      → SwitchTranslation      → [Switch / RadioOption, ...]

用户按 Space/Return → context_->ConfirmCurrentSelection()
  → 触发 select_notifier → Switcher::OnSelect(ctx)
       └─ As<SwitcherCommand>(ctx->GetSelectedCandidate())->Apply(this)
             ├─ SchemaSelection::Apply → engine->ApplySchema(new Schema(id))
             └─ Switch::Apply          → engine->context()->set_option(...)
             └─ RadioOption::Apply     → RadioGroup::SelectOption(...)
```

注意 `Apply` 都通过 `DeactivateAndApply` 先把切换器退掉、复位 `active_engine`，再对**主引擎**执行动作——否则 `set_option` 会作用到切换器自己的 `context_` 上，毫无意义。

#### 4.2.3 源码精读

抽象命令基类：[src/rime/switcher.h:68-76](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switcher.h#L68-L76)

```cpp
class SwitcherCommand {
 public:
  SwitcherCommand(const string& keyword) : keyword_(keyword) {}
  virtual void Apply(Switcher* switcher) = 0;
  const string& keyword() const { return keyword_; }
 protected:
  string keyword_;
};
```

`keyword_` 对方案候选是 `schema_id`，对开关候选是 `option_name`。`Apply` 是纯虚函数，由各子类实现具体动作。

切换器的组件装配是**硬编码**的，不读方案 YAML：[src/rime/switcher.cc:293-322](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switcher.cc#L293-L322)

```cpp
void Switcher::InitializeComponents() {
  processors_.clear();
  translators_.clear();
  if (auto c = Processor::Require("key_binder"))   processors_.push_back(...);
  if (auto c = Processor::Require("selector"))     processors_.push_back(...);
  if (auto c = Translator::Require("schema_list_translator")) translators_.push_back(...);
  if (auto c = Translator::Require("switch_translator"))      translators_.push_back(...);
}
```

这与主引擎「从方案 `engine` 段读清单装配」不同——切换器的流水线是固定的：两个处理器（按键绑定、候选选择）+ 两个翻译器（方案列表、开关）。注意这里 `Require` 出来的组件创建时传的 `Ticket(this)` 里的 `this` 是**切换器自己**（因为 `InitializeComponents` 是 `Switcher` 的方法，`this` 是 `Switcher*`，可隐式转 `Engine*`），所以翻译器的 `engine_` 指向切换器，它们 `Query` 时拿到的是切换器的上下文。

菜单刷新：[src/rime/switcher.cc:225-241](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switcher.cc#L225-L241)

```cpp
void Switcher::RefreshMenu() {
  Composition& comp = context_->composition();
  if (comp.empty()) {
    Segment seg(0, 0);  // empty range
    seg.prompt = caption_;          // 菜单标题，如「〔方案選單〕」
    comp.AddSegment(seg);
  }
  auto menu = New<Menu>();
  comp.back().menu = menu;
  for (auto& translator : translators_) {
    if (auto t = translator->Query("", comp.back()))  // 空输入
      menu->AddTranslation(t);
  }
}
```

切换器用「空输入 + 一个空 Segment」作为查询条件，让翻译器凭自己的逻辑（而非用户输入）产出候选——方案列表来自 `default.yaml` 的 `schema_list`，开关列表来自当前方案的 `switches`。

方案候选的实现：[src/rime/gear/schema_list_translator.cc:19-35](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/schema_list_translator.cc#L19-L35)

```cpp
class SchemaSelection : public SimpleCandidate, public SwitcherCommand {
 public:
  SchemaSelection(Schema* schema)
      : SimpleCandidate("schema", 0, 0, schema->schema_name()),
        SwitcherCommand(schema->schema_id()) {}
  virtual void Apply(Switcher* switcher);
};

void SchemaSelection::Apply(Switcher* switcher) {
  switcher->DeactivateAndApply([this, switcher] {
    if (Engine* engine = switcher->attached_engine()) {
      if (keyword_ != engine->schema()->schema_id()) {
        engine->ApplySchema(new Schema(keyword_));   // 真正换方案
      }
    }
  });
}
```

这里又见到多重继承：`SchemaSelection` 同时是 `SimpleCandidate`（能进菜单显示）和 `SwitcherCommand`（能执行 `Apply`）。`attached_engine()` 返回的是主引擎（即 `Processor::engine_`）。`ApplySchema` 会重建主引擎的流水线（回顾 [u2-l4](u2-l4-engine-skeleton.md)）。

方案列表按「最近使用」排序：[src/rime/gear/schema_list_translator.cc:101-125](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/schema_list_translator.cc#L101-L125)，从 `user.yaml` 的 `var/schema_access_time/<id>` 读时间戳当 `quality`，再 `stable_sort`。`fix_schema_list_order` 为真时跳过排序（保持 `schema_list` 书写顺序）。

选中触发的统一入口：[src/rime/switcher.cc:218-223](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switcher.cc#L218-L223)

```cpp
void Switcher::OnSelect(Context* ctx) {
  LOG(INFO) << "a switcher option is selected.";
  if (auto command = As<SwitcherCommand>(ctx->GetSelectedCandidate())) {
    command->Apply(this);
  }
}
```

`GetSelectedCandidate()` 返回的候选若同时是 `SwitcherCommand`（方案/开关候选都是），就执行其 `Apply`；普通候选则什么都不做。

#### 4.2.4 代码实践

**实践目标**：理清一条方案候选被选中后，从 `OnSelect` 到主引擎 `ApplySchema` 的完整调用链。

**操作步骤**：

1. 从 [switcher.cc:218](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switcher.cc#L218) 的 `OnSelect` 出发。
2. 跟到 [schema_list_translator.cc:27](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/schema_list_translator.cc#L27) 的 `SchemaSelection::Apply`，注意它先 `DeactivateAndApply`。
3. 再跟到 [engine.cc:284](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L284) 的 `ConcreteEngine::ApplySchema`，看它如何 `schema_.reset(schema)` → `InitializeComponents()` → `switcher_->SetActiveSchema(...)`。

**需要观察的现象**：选中一个新方案后，主引擎的四组组件容器（processors/segmentors/translators/filters）被重建，切换器把新方案记进 `user.yaml`。

**预期结果**：`ApplySchema` 末尾发出 `message_sink_("schema", ...)` 通知（[engine.cc:293](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L293)），前端据此刷新状态栏。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `SchemaSelection::Apply` 要用 `DeactivateAndApply` 而不是先 `Apply` 再 `Deactivate`？

> 参考答案：`Apply` 里的 `engine->ApplySchema(...)` 作用对象是主引擎，而切换器仍处于激活态时，主引擎的 `active_engine_` 还指向切换器，状态混乱。`DeactivateAndApply` 先复位 `active_engine_`、清空切换器上下文，再在「干净的」主引擎上执行换方案，保证副作用落到正确对象。

**练习 2**：`schema_list_translator` 的 `Query` 用 `dynamic_cast<Switcher*>(engine_)` 判断（[schema_list_translator.cc:133](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/schema_list_translator.cc#L133)），这说明了什么？

> 参考答案：说明方案列表翻译器**只在切换器引擎里生效**。在普通输入会话的主引擎里，`engine_` 是 `ConcreteEngine*`，`dynamic_cast` 失败返回 `nullptr`，翻译器直接 `return nullptr` 不产出候选。这是「同一组件在不同引擎里行为不同」的典型守卫。

### 4.3 Switches 配置模型：toggle 与 radio group

#### 4.3.1 概念说明

`Switches` 类是把方案 YAML 里 `switches` 段**解析成统一数据结构**的工具。它的核心任务是把两种 YAML 写法归一成同一个 `SwitchOption`：

- **toggle（二态开关）**：用 `name:` 标识，配 `states: [状态0, 状态1]` 两个标签。例如 `ascii_mode`：关=`中文`，开=`ABC`。
- **radio group（单选组）**：用 `options: [选项0, 选项1, ...]` 列出一组互斥的 option 名，配 `states: [...]` 同样数量的标签。例如 `[zh_trad, zh_simp, zh_hk, zh_tw]`，同一时刻只有一个为真。

`Switches` 本身不存状态、不切换开关，它只是个**只读的配置视图**：给定 `Config*`，能按名字或下标查到某个开关的元信息（类型、所属单选组、reset 值、状态标签）。真正的「翻转」动作在 `switch_translator` 里（见 4.4）。

#### 4.3.2 核心流程

```
YAML:
  switches:
    - name: ascii_mode           ← 有 "name" 键 → kToggleOption
        reset: 0
        states: [中文, ABC]
    - options: [zh_trad, ...]    ← 有 "options" 键 → kRadioGroup
        states: [繁, 简, 港, 臺]

Switches::FindOption(callback)        // 遍历 switches 列表
  └─ FindOptionFromConfigItem(item)   // 逐项判断
       ├─ name.IsValue()    → 造 1 个 toggle 的 SwitchOption，回调
       └─ options.IsList()  → 逐个 option 造 SwitchOption(option_index=0,1,2...)，回调
            （回调返回 kFound 即停，kContinue 继续）
```

每个 `SwitchOption` 携带：`type`（toggle/radio）、`option_name`（option 字符串）、`reset_value`（reset 字段，无则 -1）、`switch_index`（在 switches 列表里的下标）、`option_index`（单选组内的下标，toggle 恒为 0）。

#### 4.3.3 源码精读

核心数据结构：[src/rime/switches.h:24-45](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switches.h#L24-L45)

```cpp
class Switches {
 public:
  explicit Switches(Config* config) : config_(config) {}
  enum SwitchType { kToggleOption, kRadioGroup };
  struct SwitchOption {
    an<ConfigMap> the_switch = nullptr;   // 指向原始 YAML 节点（取 states/abbrev 用）
    SwitchType type = kToggleOption;
    string option_name;
    int reset_value = -1;                 // reset 字段；-1 表未指定
    size_t switch_index = 0;              // 在 switches 列表的下标
    size_t option_index = 0;              // 单选组内下标（toggle 恒 0）
    bool found() const { return bool(the_switch); }
  };
  ...
};
```

`the_switch` 保存指向 YAML 原节点的 `ConfigMap` 引用，是为了后续能读同节点的 `states` / `abbrev` 列表来取状态标签。

解析一个开关项：[src/rime/switches.cc:12-38](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switches.cc#L12-L38)

```cpp
Switches::SwitchOption Switches::FindOptionFromConfigItem(
    ConfigItemRef& item, size_t switch_index,
    function<FindResult(SwitchOption)> callback) {
  auto the_switch = As<ConfigMap>(*item);
  auto name = item["name"];
  auto options = item["options"];
  if (name.IsValue()) {                       // toggle 分支
    SwitchOption option{the_switch, kToggleOption, name.ToString(),
                        reset_value(item), switch_index};
    if (callback(option) == kFound) return option;
  } else if (options.IsList()) {              // radio group 分支
    for (size_t option_index = 0; option_index < options.size(); ++option_index) {
      SwitchOption option{the_switch, kRadioGroup, options[option_index].ToString(),
                          reset_value(item), switch_index, option_index};
      if (callback(option) == kFound) return option;
    }
  }
  return {};
}
```

判定 toggle 还是 radio 的依据就是**有没有 `name` 键**：有 `name` 是 toggle（单个 option），没有 `name` 但有 `options` 列表是 radio group（每个元素是一个 option）。注意一个 radio group 项会**展开成多个 `SwitchOption`**（每个对应一个 option_name），它们共享同一个 `switch_index` 但 `option_index` 不同。`reset_value` 辅助函数见 [switches.cc:7-10](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switches.cc#L7-L10)：读 `reset` 键，没有就返回 -1。

`reset_value` 的取值语义在初始化时很关键（见 [switches.cc:92-108](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switches.cc#L92-L108) 的 `Reset`）：对 toggle，`reset:0`=关、`reset:1`=开；对 radio group，`reset:N`=选中第 N 个 option（即 `option_index==N` 的那个）。

回调驱动的遍历：[src/rime/switches.cc:40-55](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switches.cc#L40-L55)

```cpp
Switches::SwitchOption Switches::FindOption(function<FindResult(...)> callback) {
  auto switches = (*config_)["switches"];
  if (!switches.IsList()) return {};
  for (size_t switch_index = 0; switch_index < switches.size(); ++switch_index) {
    auto item = switches[switch_index];
    if (!item.IsMap()) continue;
    auto option = FindOptionFromConfigItem(item, switch_index, callback);
    if (option.found()) return option;
  }
  return {};
}
```

`FindOption` 用「回调返回 `kFound` 即提前返回、`kContinue` 则继续」的协议，使同一遍历既能「按名查找」（`OptionByName`，[switches.cc:57-61](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switches.cc#L57-L61)），也能「全量枚举」（`switch_translator` 和 `InitializeOptions` 都传始终 `kContinue` 的回调）。

状态标签读取：[src/rime/switches.cc:138-160](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switches.cc#L138-L160)

```cpp
StringSlice Switches::GetStateLabel(an<ConfigMap> the_switch,
                                    size_t state_index, bool abbreviated) {
  auto states = As<ConfigList>(the_switch->Get("states"));
  if (!states || states->size() <= state_index) return {nullptr, 0};
  if (abbreviated) {
    auto abbrev = As<ConfigList>(the_switch->Get("abbrev"));
    if (abbrev && abbrev->size() > state_index)
      return {...abbrev[state_index]...};           // 优先用 abbrev
    else
      return {...states[state_index] 首字符...};     // 退化：取 states 首字
  }
  return {...states[state_index]...};
}
```

`states` 是给用户看的状态文字（如「中文/ABC」），`abbrev` 是其缩写（如「中/Ａ」，见 [luna_pinyin.schema.yaml:22](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L22)）。折叠显示开关时（`fold_options=true`）用缩写节省菜单宽度。

对照真实方案 `luna_pinyin`：[data/minimal/luna_pinyin.schema.yaml:18-37](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L18-L37)

```yaml
switches:
  - name: ascii_mode          # toggle：1 个 option_name=ascii_mode
    reset: 0
    states: [ 中文, ABC ]
    abbrev: [ 中, Ａ ]
  - name: full_shape          # toggle：无 reset → 不自动复位
    states: [ 半寬文字, 全寬文字 ]
  - options: [ zh_trad, zh_simp, zh_hk, zh_tw ]   # radio group：4 个互斥 option
    states: [ 傳統漢字, 简化字, 香港字形, 臺灣字形 ]
    abbrev: [ 繁, 简, 港, 臺 ]
  - name: ascii_punct         # toggle
    states: [ 中文標點, 西文標點 ]
```

`ascii_mode` 是典型 toggle（有 `name`、`reset:0`）；`zh_trad...` 是典型 radio group（有 `options` 列表、无 `name`）。

#### 4.3.4 代码实践

**实践目标**：对照 `luna_pinyin.schema.yaml` 的 `switches` 段，手工模拟 `Switches::FindOption` 的遍历结果。

**操作步骤**：

1. 列出 `switches` 段的 4 个项，逐项判断它是 toggle 还是 radio group。
2. 对每项写出 `Switches::FindOptionFromConfigItem` 会造出几个 `SwitchOption`，各自的 `option_name` / `type` / `reset_value` / `switch_index` / `option_index` 是什么。
3. 用 `OptionByName("zh_simp")` 查找，画出遍历到第几项、第几个 option 时回调返回 `kFound`。

**需要观察的现象**：`zh_trad/zh_simp/zh_hk/zh_tw` 这一项会展开成 4 个 `SwitchOption`（`option_index` 分别为 0/1/2/3），共享 `switch_index=2`。

**预期结果**：

| switch_index | YAML 项 | 展开的 SwitchOption |
| --- | --- | --- |
| 0 | `ascii_mode` | 1 个 toggle，`option_name=ascii_mode`, `reset=0`, `option_index=0` |
| 1 | `full_shape` | 1 个 toggle，`option_name=full_shape`, `reset=-1`, `option_index=0` |
| 2 | `options:[zh_trad,...]` | 4 个 radio，`option_name` 分别为 `zh_trad/zh_simp/zh_hk/zh_tw`，`option_index=0/1/2/3` |
| 3 | `ascii_punct` | 1 个 toggle，`option_name=ascii_punct`, `reset=-1` |

#### 4.3.5 小练习与答案

**练习 1**：为什么 radio group 要展开成多个 `SwitchOption`，而 toggle 只有一个？

> 参考答案：radio group 里的每个选项都是一个独立的布尔 option（如 `zh_simp` 是一个 option），切换时要把选中的置真、其余置假，所以每个选项需要单独的 `SwitchOption` 记录其 `option_name` 和组内位置。toggle 只有一个 option，开/关都作用在它身上，故只需一个 `SwitchOption`。

**练习 2**：若一个 `switches` 项既没有 `name` 也没有 `options` 键，`FindOptionFromConfigItem` 会怎样？

> 参考答案：`name.IsValue()` 与 `options.IsList()` 都为假，两个分支都不进入，直接返回空的 `SwitchOption{}`（`the_switch` 为 `nullptr`，`found()` 为假），这一项被跳过。

### 4.4 开关状态的初始化、切换与持久化

#### 4.4.1 概念说明

一个开关有三个时刻的状态来源，分属不同代码路径，容易混淆：

1. **会话开始时恢复**：从 `user.yaml` 的 `var/option/<name>` 读上次保存的值（仅对 `save_options` 名单里的开关）。由 `Switcher::RestoreSavedOptions` 负责，**每个会话只做一次**。
2. **方案加载/切换时复位**：对带 `reset:` 字段的开关，强制设回 reset 值（覆盖恢复值）。由 `ConcreteEngine::InitializeOptions` 负责，每次 `ApplySchema` 都做。
3. **用户切换时写回**：用户在菜单里翻开关后，若该开关在 `save_options` 名单里，把新值写回 `user.yaml`。由 `switch_translator` 的 `Switch::Apply` / `RadioGroup::SelectOption` 负责。

`save_options` 名单本身来自 `default.yaml` 的 `switcher/save_options`，不是方案自己的 `switches` 段——这是个常见误区。

#### 4.4.2 核心流程

```
[会话开始] ConcreteEngine 构造
   ├─ switcher_->RestoreSavedOptions()        // 读 user.yaml var/option/*
   │      对 save_options_ 里每个 name：user_config_->GetBool("var/option/"+name)
   │      → engine_->context()->set_option(name, value)
   └─ InitializeOptions()                     // 复位带 reset 的开关
          Switches(config).FindOption(...):
            reset_value>=0 → set_option(name, reset 语义)

[换方案] ApplySchema(schema)
   ├─ InitializeOptions()                     // 再次复位（新方案的 reset）
   └─ switcher_->SetActiveSchema(id)          // 记 var/previously_selected_schema + 时间戳

[用户翻开关] Switch::Apply / RadioGroup::SelectOption
   ├─ engine->context()->set_option(name, ...)    // 改主引擎上下文
   └─ if (IsAutoSave(name)) user_config_->SetBool("var/option/"+name, value)  // 写回
```

注意复位（reset）与恢复（restore）的**先后**：构造期先 `RestoreSavedOptions`（恢复保存值），紧接着 `InitializeOptions`（复位）。所以一个带 `reset:0` 的开关，即使 `user.yaml` 里存了 `1`，也会被复位成 `0`——`reset` 优先级更高。这正是 `ascii_mode` 带 `reset:0` 的效果：每次进会话都回到中文态。

#### 4.4.3 源码精读

`save_options` 名单的加载：[src/rime/switcher.cc:280-288](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switcher.cc#L280-L288)

```cpp
if (auto options = config->GetList("switcher/save_options")) {
  save_options_.clear();
  for (auto it = options->begin(); it != options->end(); ++it) {
    auto option_name = As<ConfigValue>(*it);
    if (!option_name) continue;
    save_options_.insert(option_name->str());
  }
}
```

读的是 `switcher/save_options`，来自 `default.yaml`：[data/minimal/default.yaml:16-20](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/default.yaml#L16-L20)

```yaml
switcher:
  save_options:
    - full_shape
    - ascii_punct
    - simplification
    - extended_charset
```

注意 `ascii_mode` 和 `zh_*` 都**不在**这个名单里——所以它们默认不会被持久化到 `user.yaml`（除非用户在 `default.custom.yaml` 里追加，见 [u9-l3](u9-l3-customizer-settings.md)）。

会话开始恢复保存值：[src/rime/switcher.cc:40-49](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switcher.cc#L40-L49)

```cpp
void Switcher::RestoreSavedOptions() {
  if (user_config_) {
    for (const string& option_name : save_options_) {
      bool value = false;
      if (user_config_->GetBool("var/option/" + option_name, &value)) {
        engine_->context()->set_option(option_name, value);
      }
    }
  }
}
```

只对 `save_options_` 里的名字查 `var/option/<name>`，查到才恢复（查不到保持默认 false）。注意 `engine_->context()` 是**主引擎**的上下文（`engine_` 是 Processor 的成员）。

方案加载时复位：[src/rime/engine.cc:376-395](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L376-L395)

```cpp
void ConcreteEngine::InitializeOptions() {
  Config* config = schema_->config();
  Switches switches(config);
  switches.FindOption([this](Switches::SwitchOption option) {
    if (option.reset_value >= 0) {
      if (option.type == Switches::kToggleOption) {
        context_->set_option(option.option_name, (option.reset_value != 0));
      } else if (option.type == Switches::kRadioGroup) {
        context_->set_option(option.option_name,
                             static_cast<int>(option.option_index) == option.reset_value);
      }
    }
    return Switches::kContinue;
  });
}
```

这是 4.3 里 `Switches` 解析能力的实际消费者：对每个带 `reset>=0` 的开关，按类型设值。toggle 的复位值是布尔（`reset!=0`）；radio group 的复位是「`option_index` 等于 `reset` 值的那个 option 为真，其余为假」——注意这里只显式 `set_option` 了当前遍历到的那个 option，组内其他 option 靠「未设默认 false」保持关（回顾 [u3-l1](u3-l1-context.md)：未设 option 默认 false）。

是否自动保存的判定：[src/rime/switcher.cc:214-216](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switcher.cc#L214-L216)

```cpp
bool Switcher::IsAutoSave(const string& option) const {
  return save_options_.find(option) != save_options_.end();
}
```

用户翻转 toggle 时真正写盘：[src/rime/gear/switch_translator.cc:57-68](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/switch_translator.cc#L57-L68)

```cpp
void Switch::Apply(Switcher* switcher) {
  switcher->DeactivateAndApply([this, switcher] {
    if (Engine* engine = switcher->attached_engine()) {
      engine->context()->set_option(keyword_, target_state_);   // 改主引擎
    }
    if (auto_save_) {
      if (Config* user_config = switcher->user_config()) {
        user_config_->SetBool("var/option/" + keyword_, target_state_);  // 写回
      }
    }
  });
}
```

`auto_save_` 在候选构造时由 `switcher->IsAutoSave(option.option_name)` 决定（[switch_translator.cc:221-222](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/switch_translator.cc#L221-L222)）。radio group 的写回在 `RadioGroup::SelectOption`：[switch_translator.cc:121-136](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/switch_translator.cc#L121-L136)，对组内每个 option 都判定 `IsAutoSave`，命中才写。

`SetActiveSchema` 记录方案切换：[src/rime/switcher.cc:165-172](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switcher.cc#L165-L172)

```cpp
void Switcher::SetActiveSchema(const string& schema_id) {
  if (user_config_) {
    user_config_->SetString("var/previously_selected_schema", schema_id);
    user_config_->SetInt("var/schema_access_time/" + schema_id, time(NULL));
    user_config_->Save();
  }
}
```

`var/previously_selected_schema` 让下次启动时回到上次方案（`CreateSchema` 用它，[switcher.cc:174-196](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switcher.cc#L174-L196)），`var/schema_access_time/<id>` 是方案列表按最近使用排序的时间戳来源（4.2.3 已述）。

#### 4.4.4 代码实践

**实践目标**：解释 `luna_pinyin` 里 `ascii_mode`（toggle）与 `zh_trad/zh_simp/...`（radio group）在初始化与持久化上的区别。

**操作步骤**：

1. 查 [luna_pinyin.schema.yaml:19-21](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L19-L21)：`ascii_mode` 有 `reset: 0`。
2. 查 [default.yaml:16-20](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/default.yaml#L16-L20)：`save_options` 名单里**没有** `ascii_mode`，也**没有** `zh_trad/zh_simp/...`。
3. 推演：每次会话开始，`ascii_mode` 会经历什么？`zh_simp` 会经历什么？

**需要观察的现象**：

- `ascii_mode`：构造期 `RestoreSavedOptions` 不恢复（不在名单），紧接着 `InitializeOptions` 因 `reset:0` 而设成 false（中文）。结论：**每次进会话都强制回到中文态**，用户的临时切换不持久化。
- `zh_trad/...`：无 `reset`，所以 `InitializeOptions` 不复位；不在 `save_options` 名单，所以用户切换也不写回 `user.yaml`。结论：**每次会话默认全 false（即都不选，由 `simplifier` 等组件的默认行为决定繁简）**，切换只在当前会话有效。

**预期结果**：理解了「`reset` 控制**启动复位**、`save_options` 控制**运行期持久化**」是两个独立机制。要让 `ascii_mode` 跨会话保留，需在 `default.custom.yaml` 的 `switcher/save_options` 里追加 `ascii_mode`（回顾 [u9-l3](u9-l3-customizer-settings.md) 的 patch 机制）。

#### 4.4.5 小练习与答案

**练习 1**：若用户希望「英文模式跨会话保留」，该改哪里？

> 参考答案：在 `user_data_dir/default.custom.yaml` 里 patch `switcher/save_options`，追加 `ascii_mode`（用 `__patch` 或 `/+` 追加，见 [u4-l3](u4-l3-config-compiler-dsl.md)）。同时要**去掉** `luna_pinyin.schema.yaml` 里 `ascii_mode` 的 `reset: 0`（或在 `luna_pinyin.custom.yaml` 里 patch 掉），否则每次启动 `InitializeOptions` 都会把它复位成 false，覆盖保存值。

**练习 2**：radio group 里 `reset: 1` 表示什么？

> 参考答案：表示方案加载时，组内 `option_index == 1` 的那个 option 被设为真、其余为假。例如 `options: [zh_trad, zh_simp, zh_hk, zh_tw]` 配 `reset: 1`，启动后 `zh_simp` 为真，其余为假。

## 5. 综合实践

**任务**：模拟一次「按 hotkey 弹出菜单 → 翻一个开关 → 选中」的完整过程，画出涉及的对象与数据流。

请按以下步骤，在源码里走通这条链，并写出每一步发生在哪个对象的哪个方法：

1. 用户在 `rime_api_console` 里按 `Control+grave`。指出这个键如何被 `Switcher::ProcessKeyEvent`（[switcher.cc:51](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switcher.cc#L51)）识别为 hotkey（`hotkeys_` 从哪加载？见 [switcher.cc:271-279](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switcher.cc#L271-L279) 与 [default.yaml:12-15](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/default.yaml#L12-L15)）。
2. `Activate()` 后，前端调用 `Session::context()`（[service.cc:59](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/service.cc#L59)）拿到菜单。说明此时返回的是哪个 `Context`，`switch_translator` 如何用 `Switches::FindOption`（[switch_translator.cc:212-234](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/switch_translator.cc#L212-L234)）把 `switches` 段变成 `Switch` 候选。
3. 用户选中「半寬→全寬」那条 `full_shape` 开关候选。跟踪 `OnSelect`（[switcher.cc:218](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switcher.cc#L218)）→ `Switch::Apply`（[switch_translator.cc:57](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/switch_translator.cc#L57)）。
4. 解释 `full_shape` 切换后，为什么 `user.yaml` 里**不会**出现 `var/option/full_shape`... 等等，先别下结论——查 [default.yaml:16-20](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/default.yaml#L16-L20) 的 `save_options` 名单里有没有 `full_shape`，再回答它**会不会**被写回。

**交付物**：一张时序图（文字版即可），标出主引擎、切换器、`switch_translator`、`Switches`、`user.yaml` 五者的交互顺序与每一步调用的方法名。

## 6. 本讲小结

- `Switcher` 多重继承 `Processor` 与 `Engine`：作为 `Processor` 嵌进主引擎处理器链首位以截获 hotkey；作为 `Engine` 复用整套组词机制来生成「方案/开关」菜单。代码里 `engine_` 指主引擎、`context_`/`schema_` 指切换器自身，务必区分。
- 激活态靠 `engine_->set_active_engine(this)` 实现**旁路**：前端经 `Session::context()` → `active_engine()->context()` 读到的是切换器菜单；退出时复位。激活期间切换器对多数键返回 `kAccepted`，独占按键。
- 菜单内容由两个翻译器产出：`schema_list_translator`（方案候选，按最近使用排序）与 `switch_translator`（开关候选）。选中触发 `OnSelect` → `SwitcherCommand::Apply`，统一了「选方案」「翻开关」两类动作。
- `Switches` 是 `switches` 段的只读解析视图，按有无 `name` 键区分 toggle（单个 option）与 radio group（一组互斥 option），归一成 `SwitchOption`；用回调协议支持按名查找与全量枚举。
- 开关状态有三个来源、两条独立机制：`reset` 字段控制**启动/换方案时复位**（`InitializeOptions`，优先级高于恢复），`save_options` 名单控制**用户切换后持久化**到 `user.yaml` 的 `var/option/*`（`RestoreSavedOptions` 恢复、`Switch::Apply` 写回）。

## 7. 下一步学习建议

- **[u9-l5 Encoder：编码生成](u9-l5-encoder.md)**：本讲的 radio group / toggle 是「运行期状态切换」，下一讲进入词典构建期的「编码规则」，理解 `TableEncoder` 如何为多字词生成编码。
- **[u9-l6 插件开发实战](u9-l6-plugin-development.md)**：如果想自己写一个「自定义开关」或「特殊方案切换逻辑」，`SwitcherCommand` 命令模式是很好的模板——可参考它实现自己的命令候选。
- 继续阅读：[src/rime/gear/switch_translator.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/switch_translator.cc) 的 `FoldedOptions`（折叠显示）与 `RadioGroup` 互斥逻辑，是本讲未展开的细节，值得通读。
