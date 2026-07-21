# Processor 组件族

## 1. 本讲目标

本讲是「按键处理流水线」系列的第二篇。上一篇 u6-l1 搭好了骨架：方案 `engine/processors` 清单被装配成一条 `Processor` 链，`ConcreteEngine::ProcessKey` 依次调用每个处理器的 `ProcessKeyEvent`，靠三态返回值决定早退。本讲把镜头推进到这条链上的**具体零件**——逐一拆解 librime 内置的常用 Processor：

- `speller`：把字母按键追加进 `Context::input`；
- `ascii_composer`：管理中英文（ascii）模式与切换键；
- `recognizer`：用正则识别「命令式」输入串（如反查前缀）；
- `navigator`：在已输入串里移动光标；
- `selector`：翻页、选候选；
- `key_binder`：按上下文条件触发快捷键；
- `punctuator`：标点映射；
- `editor`（`express_editor` / `fluid_editor`）：确认、回退、提交等编辑动作。

读完本讲，你应当能够：

1. 说出每个 Processor **接受哪些按键、返回什么结果、在什么条件下早退**；
2. 解释 `speller` 如何把一个字母变成 `Context::PushInput`，以及 `auto_select` 等配置如何改变其行为；
3. 区分 `express_editor`（即时上屏）与 `fluid_editor`（流式编辑）的按键绑定差异；
4. 对照 `luna_pinyin.schema.yaml` 的 `engine/processors` 清单，画出一次按键穿过处理器链的全过程。

## 2. 前置知识

本讲默认你已掌握以下内容（来自前置讲义）：

- **KeyEvent 的双字段模型**（u2-l1）：`keycode`（键值）+ `modifier`（修饰位掩码），`key_event.ctrl()/alt()/shift()/super()/release()` 等便捷判断。
- **Context 是输入状态中央容器**（u3-l1）：`input()`、`caret_pos()`、`composition()`、`PushInput/PopInput`、`get_option/set_option`、各类 Notifier。
- **Segmentation 与 Segment**（u3-l2）：`GetCurrentEndPosition`、`GetConfirmedPosition`、`back()`、`selected_index`、`tags`。
- **Processor 基类契约**（u5-l2）：四类组件都以 `Ticket` 构造、持有 `engine_` 与 `name_space_`，核心是返回 `ProcessResult` 的 `ProcessKeyEvent`。
- **引擎流水线总览**（u6-l1）：`ProcessKey` 主循环的早退规则、`Compose` 的两阶段。

几个在本讲会反复出现的术语，先统一说明：

- **三态返回值**：`kAccepted`（吃掉按键，结束本次派发）、`kRejected`（短路跳出主循环，交给系统默认处理）、`kNoop`（我不处理，交给下一个 Processor）。
- **早退（early exit）**：Processor 在 `ProcessKeyEvent` 开头用一系列 `if (... ) return kNoop;` 把「不属于自己管辖」的按键快速放行，只对真正感兴趣的按键做深入处理。
- **KeyBindingProcessor**：一个模板工具类，把「按键 → 成员函数动作」的映射表（keymap）和配置加载逻辑抽出来复用，`Editor`/`Selector`/`Navigator` 都继承自它。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/rime/processor.h` | 定义 `ProcessResult` 三态枚举与 `Processor` 抽象基类。 |
| `src/rime/engine.cc` | `ConcreteEngine::ProcessKey` 驱动处理器链的主循环，定义早退语义。 |
| `src/rime/gear/speller.cc` / `.h` | 拼写处理器：把字母追加进输入串，并实现各种自动上屏逻辑。 |
| `src/rime/gear/recognizer.cc` / `.h` | 用正则识别命令式输入（反查、字母段等），命中即吃键并打 tag。 |
| `src/rime/gear/ascii_composer.cc` / `.h` | 管理 `ascii_mode` 选项与 Caps Lock / Shift 等切换键。 |
| `src/rime/gear/selector.cc` | 候选选择：翻页键、方向键、数字键、`select_keys`。 |
| `src/rime/gear/navigator.cc` | 光标移动：按字符 / 按音节左右跳、Home/End。 |
| `src/rime/gear/editor.cc` / `.h` | 编辑动作族：确认、回退、提交；含 `FluidEditor` 与 `ExpressEditor`。 |
| `src/rime/gear/key_binder.cc` | 条件快捷键：在特定上下文（composing/paging/…）下把按键重映射为动作。 |
| `src/rime/gear/punctuator.cc` | 标点映射处理器（本讲只做概览）。 |
| `src/rime/gear/key_binding_processor.h` / `_impl.h` | 复用的「按键→动作」绑定模板，被 Editor/Selector/Navigator 继承。 |
| `data/minimal/luna_pinyin.schema.yaml` | 实践任务对照的真实方案配置。 |

## 4. 核心概念与源码讲解

### 4.1 处理器流水线：三态返回值与早退规则

#### 4.1.1 概念说明

在拆具体 Processor 之前，必须先钉死「它们的返回值意味着什么」。librime 用一个枚举表达处理器对一次按键的裁决：

- `kAccepted`：**我处理了，吃掉这个按键**，引擎立刻结束本次 `ProcessKey`，按键不再往下传，也不会交给操作系统。
- `kRejected`：**明确拒绝**，引擎**跳出整个处理器循环**（不再问后续 Processor），让按键走操作系统的默认行为（比如把字母直接打到文本框）。
- `kNoop`：**与我无关**，引擎继续问下一个 Processor。

这套三态是所有 Processor 共同遵守的契约，理解了它，就理解了每个 Processor 源码里大量 `return kNoop;` 的来历——那都是「这键不归我管，放行」的早退出口。

#### 4.1.2 核心流程

引擎主循环的伪代码：

```
ConcreteEngine::ProcessKey(key):
    ret = kNoop
    for processor in processors_:        # 主处理器链
        ret = processor.ProcessKeyEvent(key)
        if ret == kRejected: break       # 短路跳出，交给系统默认
        if ret == kAccepted: return true # 吃掉，结束
    context_.commit_history().Push(key)  # 记录「未被吃掉」的按键
    for processor in post_processors_:   # 后处理器链
        ret = processor.ProcessKeyEvent(key)
        if ret == kRejected: break
        if ret == kAccepted: return true
    context_.unhandled_key_notifier()(key)  # 通知订阅者（如 speller 的重解释）
    return false
```

要点：

1. **顺序敏感**：`processors_` 是一个有序 `vector`，越靠前的 Processor 越先看到按键，因此方案 YAML 里 `engine/processors` 的书写顺序就是优先级。
2. **kAccepted 即终止**：任何一个 Processor 返回 `kAccepted`，后续 Processor 都看不到这个按键——这就是为什么 `speller` 要放在 `punctuator` 之前还是之后很关键。
3. **kNoop 才会入历史**：只有一路 `kNoop` 走完、没人吃的按键，才会被 `commit_history` 记录（用于标点/数字的上下文判断）。
4. **未被任何人接受的按键**最后会触发 `unhandled_key_notifier_`，给那些「想再抢救一下」的组件（如拼音串末尾的句号重解释）一次机会。

#### 4.1.3 源码精读

三态枚举定义在 `processor.h`，注释即语义：

[processor.h:18-22](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/processor.h#L18-L22) —— 定义 `kRejected`（走系统默认）、`kAccepted`（吃掉）、`kNoop`（交给下一个）。

引擎主循环在 [engine.cc:99-122](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L99-L122) —— `ProcessKey` 里依次调用 `processors_`，`kRejected` 用 `break`、`kAccepted` 用 `return true`，循环结束后才 `Push` 历史、跑后处理器、通知未处理按键。

`Processor` 基类本身非常薄，只持有 `engine_` 和 `name_space_`，默认 `ProcessKeyEvent` 返回 `kNoop`：[processor.h:24-39](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/processor.h#L24-L39)。

#### 4.1.4 代码实践

1. **实践目标**：用源码确认「顺序 + 三态」如何决定一次按键的命运。
2. **操作步骤**：
   - 打开 [engine.cc:99-122](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L99-L122)，逐行标注每个 `break` / `return true` 的触发条件。
   - 打开 `data/minimal/luna_pinyin.schema.yaml` 的 `engine/processors`，把 8 个名字抄下来，与 `processors_` 的运行顺序对应。
3. **需要观察的现象**：理解「为什么 `ascii_composer` 排在最前」——它要在任何拼写发生前就拦截英文字母（当处于 ASCII 模式时直接 `kRejected` 上屏）。
4. **预期结果**：你能口述「一个普通字母键 a，在中文模式下会依次穿过 `ascii_composer`(kNoop) → `recognizer`(kNoop) → `key_binder`(kNoop) → `speller`(kAccepted)」。
5. 若想看到真实日志，可在 `ENABLE_LOGGING=ON` 的构建里运行 `rime_api_console`，`DLOG`/`LOG(INFO)` 会打印每次 `process key:` 与各处理器的判断（**待本地验证**，取决于 glog 是否启用）。

#### 4.1.5 小练习与答案

**练习 1**：如果某个 Processor 既不 `return kAccepted` 也不 `return kRejected`，而是一直 `return kNoop`，引擎会怎样？

> **答案**：该按键会被传给后续每一个 Processor；若全部 `kNoop`，则进入 `commit_history`、再过 `post_processors_`，最后触发 `unhandled_key_notifier_`，`ProcessKey` 返回 `false`，按键交还操作系统。

**练习 2**：为什么 `kRejected` 用 `break` 而 `kAccepted` 用 `return true`？它们对「后处理器」的影响有何不同？

> **答案**：`kAccepted` 表示按键已被吃掉、本次派发彻底结束，所以直接 `return`；`kRejected` 只是退出主处理器链，但按键仍属于「未被接受」，因此还需要进入 `commit_history` 与后处理器流程，故用 `break` 让循环自然走到后续代码。

---

### 4.2 speller：把字母追加进输入串

> 这是本讲的三个**最小模块**之一，做最深入的拆解。

#### 4.2.1 概念说明

`speller` 是拼音/形码方案里最核心的处理器，职责非常聚焦：**当用户按下一个属于「字母表」的按键时，把它追加到 `Context::input` 里**，从而触发后续 `Compose`（切分 + 翻译）。它本身**不查词典、不生成候选**——那些是 Segmentor/Translator 的事。`speller` 只负责「让输入串变长一个字符」。

围绕这件小事，它还顺带处理了一系列「自动上屏」策略，这些策略由 `speller:` 配置项驱动：

- `alphabet`：哪些字符算「字母」（默认 `zyxwvutsrqponmlkjihgfedcba`，注意是**倒序**的，这是 RIME 的历史约定，用于内部排序）。
- `delimiter`：音节分隔符（如 `" '`），打到分隔符也算有效输入。
- `initials` / `finals`：声母 / 韵母字符集，用于判断「现在是否该期待一个新的声母」。
- `max_code_length`：最大编码长度，超过则自动上屏。
- `auto_select` / `auto_select_pattern`：唯一候选时自动确认。
- `auto_clear`：无候选时自动清空（`auto` / `manual` / `max_length`）。
- `use_space`：是否允许空格进入输入串。

#### 4.2.2 核心流程

`Speller::ProcessKeyEvent` 的判定流程（伪代码）：

```
ProcessKeyEvent(key):
    if 释放键 / Ctrl / Alt / Super:  return kNoop      # 早退：修饰键不归我
    ch = key.keycode()
    if ch < 0x20 或 ch >= 0x7f:       return kNoop      # 早退：非可见 ASCII
    if ch == 空格 且 (未开 use_space 或按了 Shift): return kNoop
    if ch 不在 alphabet 且 不在 delimiter: return kNoop  # 早退：不是字母/分隔符
    if 不是声母 且 当前期待声母:        return kNoop      # 早退：避免非法组合
    # —— 通过早退，确认这是一个合法拼写键 ——
    （可选）AutoSelectAtMaxCodeLength / AutoClear        # 达到上限先确认上一段
    备份 auto_select 下的 previous_segment
    ctx->PushInput(ch)                                   # 核心：输入串 += ch
    ctx->BeginEditing()
    （可选）AutoSelectPreviousMatch / AutoSelectUniqueCandidate / AutoClear
    return kAccepted                                     # 吃掉按键
```

关键洞察：**`speller` 的早退条件非常厚**，一连串 `return kNoop` 把所有「不是字母」的按键挡在门外，只有真正属于拼写字符的按键才会走到 `ctx->PushInput(ch)` 这一行。这是它能与其他处理器和平共处的基础。

#### 4.2.3 源码精读

构造函数从 `speller:` 配置节读取全部参数，未配置 `initials` 时用 `alphabet` 兜底：[speller.cc:71-98](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/speller.cc#L71-L98)。注意默认字母表是倒序字符串常量 [speller.cc:20](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/speller.cc#L20)。

核心 `ProcessKeyEvent`：[speller.cc:100-146](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/speller.cc#L100-L146)。重点看：

- 第 101-103 行：释放键 / Ctrl / Alt / Super 一律 `kNoop`，这就是「Shift+a 的大写 A 不被 speller 直接吃掉」（Shift 是修饰，但 `key_event.shift()` 不在排除列里——speller 允许 Shift 修饰的字母进入，用于大写触发的特殊行为，由上游 `ascii_composer` 决定模式）。
- 第 105-106 行：`ch < 0x20 || ch >= 0x7f` 把控制字符与非 ASCII 挡掉。
- 第 107-108 行：空格的特判——只有开启 `use_space` 且**没有**按 Shift 才放行。
- 第 109-110 行：字母表与分隔符的双重归属判断。
- 第 112-115 行：声母期待判断 `expecting_an_initial`，避免把一个不可能成为声母的字符接在另一个字符后面。
- 第 129-130 行：`ctx->PushInput(ch)` + `ctx->BeginEditing()`，这就是「输入串变长」的真正发生地；`PushInput` 会触发 `update_notifier_`，进而引发 `Engine::Compose`（见 u6-l1）。

自动上屏辅助函数：达到最大码长时确认当前选择 [speller.cc:148-160](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/speller.cc#L148-L160)；唯一候选自动确认 [speller.cc:162-187](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/speller.cc#L162-L187)。`AutoSelectUniqueCandidate` 用 `seg.menu->Prepare(2) == 1` 来判断「是否唯一候选」——只准备 2 条，若只得到 1 条即为唯一，这是惰性求值的典型用法（呼应 u3-l4 的 Menu 拉模型）。

#### 4.2.4 代码实践

1. **实践目标**：验证 `speller` 的字母表过滤与 `PushInput` 行为。
2. **操作步骤**：
   - 读 `data/minimal/luna_pinyin.schema.yaml` 的 `speller:` 段（[第 70-86 行](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L70-L86)），记下 `alphabet`、`delimiter`、`algebra`。
   - 在 `speller.cc:109` 处想象插入一行 `LOG(INFO) << "speller sees: " << (char)ch;`（**示例代码**，请勿真正修改源码），然后按 `n`、`i`、`h`、`a`、`o`、空格、`1` 这几个键推演哪些会打印日志。
3. **需要观察的现象**：
   - `n i h a o` 都属于 `alphabet`，会被 `PushInput`，日志打印 5 次。
   - 空格：`use_space` 未在 luna_pinyin 里开启（默认 false），且未按 Shift，命中 [speller.cc:107](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/speller.cc#L107) 的 `kNoop`，**不打印**。
   - 数字 `1`：不在 `alphabet` 也不在 `delimiter`，命中 [speller.cc:109](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/speller.cc#L109) 的 `kNoop`，**不打印**——它会落到后面的 `selector` 去选候选。
4. **预期结果**：只有 5 个字母键被 speller 吃掉，空格与数字被放行给后续处理器。
5. 真实日志输出**待本地验证**（依赖 glog 与控制台程序）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `speller` 的默认字母表是倒序的 `zyxwvutsrqponmlkjihgfedcba`？

> **答案**：这是 RIME 的内部约定。Prism（音节索引，见 u8-l2）按拼写建双数组 trie 时，字符的比较顺序会影响索引结构；倒序字母表是一种历史选择，使某些拼音方案的编码排序更合期望。对学习者而言只需记住：「`alphabet` 既定义了哪些字符合法，也定义了它们在 Prism 里的排序权重」。

**练习 2**：`AutoSelectUniqueCandidate` 里为什么是 `Prepare(2) == 1` 而不是 `Prepare(1) == 1`？

> **答案**：`Prepare(2)` 请求准备 2 条候选；若实际只返回 1 条，才能**证明**候选唯一。若用 `Prepare(1)`，即使存在多条候选也只会返回 1，无法区分「唯一」与「还有更多」。这是用「多取一条」来反证唯一性的标准技巧。

**练习 3**：按 `Shift+a`（大写 A）时，`speller` 会把它追加进输入串吗？

> **答案**：会尝试。`speller` 的早退只排除了 Ctrl/Alt/Super 与 release，**没有排除 Shift**（[speller.cc:101-103](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/speller.cc#L101-L103)）。`keycode` 是 `XK_a`，仍属字母表。但实际是否进入输入串，还取决于排在 `speller` **之前**的 `ascii_composer` 是否已经把 Shift+a 拦截用于切换模式——这正是处理器顺序的意义。

---

### 4.3 recognizer：用正则识别命令式输入

> 这是本讲的三个**最小模块**之一。

#### 4.3.1 概念说明

有些输入不是「逐字拼写」，而是「整串命令」。典型例子：

- 反查：按 `` ` `` 进入仓颉反查模式，输入 `` `abcd `` 查拼音对应的仓颉码；
- 字母段：按 `:` 进入纯西文输入段，输入 `:hello;`；
- 仓颉前缀：按 `C:` 切到仓颉。

这些串的共同点是**以某个前缀开头、以某个后缀（可省）结尾**。`recognizer` 的工作就是：每当用户按下属于这类串的字符时，用一组**正则表达式**去匹配「当前输入串 + 这个新字符」，若匹配成功就把字符追加进输入串并吃掉按键，让后续 segmentor 靠 tag 把这段输入交给专门的 translator。

`recognizer` 把「前缀/后缀」的识别能力**完全交给配置**，自己只负责「正则匹配 + 吃键」。这是数据驱动设计的典范：换一套 patterns 就能支持全新的命令语法，引擎代码不变。

#### 4.3.2 核心流程

```
Recognizer 构造:
    从 recognizer/patterns 读 {tag: 正则字符串}，编译成 boost::regex

ProcessKeyEvent(key):
    if 没有任何 pattern / Ctrl / Alt / Super / release:  return kNoop
    ch = key.keycode()
    if (use_space 且 ch==空格) 或 (可见 ASCII 字母数字符号):
        input = ctx->input() + ch              # 假装已经追加了
        match = patterns.GetMatch(input, composition)
        if match.found():
            ctx->PushInput(ch)                  # 真正追加
            return kAccepted
    return kNoop
```

`GetMatch` 的判定有三道关卡：

1. 正则能在「当前输入+新字符」里搜到匹配；
2. 匹配的**结尾必须恰好等于输入串末尾**（`end != input.length()` 则 `continue`）——即「这个字符让整串首次完整匹配」；
3. 匹配的**起点**要么是当前切分末尾 `GetCurrentEndPosition`，要么恰好是某个已有 Segment 的起点——保证识别出的片段与已有切分边界对齐。

#### 4.3.3 源码精读

`RecognizerPatterns::LoadConfig` 把 YAML 里的字符串编译成 `boost::regex`，正则错误时只记日志不崩溃：[recognizer.cc:18-37](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/recognizer.cc#L18-L37)。

匹配核心 `GetMatch`：[recognizer.cc:39-70](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/recognizer.cc#L39-L70)。注意三道关卡分别在第 51 行（结尾对齐）、第 53 行（起点=切分末尾）、第 58-66 行（起点=某段起点）。返回的 `RecognizerMatch{tag, start, end}` 里的 `tag` 就是 segmentor 后续用来分流的依据。

构造函数把默认命名空间从 `processor` 改写为 `recognizer`，并读取 `use_space`：[recognizer.cc:72-82](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/recognizer.cc#L72-L82)。

`ProcessKeyEvent`：[recognizer.cc:84-102](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/recognizer.cc#L84-L102)。关键细节：它**先用 `input += ch` 构造一个试探串**去匹配（第 93-94 行），匹配成功才真正 `ctx->PushInput(ch)`（第 97 行）。这种「先试后写」避免了「把字符加进去后发现不匹配、又要撤回」的麻烦。

luna_pinyin 的 patterns 配置（[schema 第 153-159 行](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L153-L159)）：

| tag | 正则 | 含义 |
| --- | --- | --- |
| `alphabet` | `(?<![A-Z]):[^;]*;?$` | `:` 开头的西文段 |
| `cangjie` | `C:[a-z']*;?$` | `C:` 开头的仓颉反查 |
| `pinyin` | `P:[a-z']*;?$` | `P:` 开头的拼音段 |
| `reverse_lookup` | `` `[a-z]*'?$ `` | `` ` `` 开头的反查 |

#### 4.3.4 代码实践

1. **实践目标**：验证 `recognizer` 的「前缀触发」与「先试后写」。
2. **操作步骤**：
   - 假设依次输入 `` ` ``、`a`、`b`、`'`，对照 [recognizer.cc:84-102](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/recognizer.cc#L84-L102) 推演每一步。
   - 第一个键 `` ` ``：试探串 = `` ` ``，正则 `` `[a-z]*'?$ `` 能匹配（`[a-z]*` 匹配空、`'?` 匹配空），结尾对齐 → `PushInput`，返回 `kAccepted`。
   - 第二个键 `a`：试探串 = `` `a ``，匹配 → 吃键。
   - 依此类推，`'` 也能被 `` '? `` 匹配。
3. **需要观察的现象**：整串 `` `ab' `` 全部由 `recognizer` 吃掉，`speller` 完全看不到这些键（因为 recognizer 排在 speller 之前，已 `kAccepted`）。
4. **预期结果**：这段输入会被打上 `reverse_lookup` tag，交给 `reverse_lookup_translator` 处理，而不是普通的拼音翻译器。
5. 真实运行**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `recognizer` 的 patterns 正则几乎都以 `$` 结尾或要求「结尾对齐」？

> **答案**：因为 `GetMatch` 要求匹配的 `end` 必须等于当前输入串长度（[recognizer.cc:51](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/recognizer.cc#L51)）。这意味着识别发生在「整串刚刚好成为一个完整命令」的时刻。`$` 或结尾对齐保证了「输入还在进行中也能逐步识别」，而不必等用户输入结束。

**练习 2**：如果两个 pattern 的正则有重叠（都能匹配同一串输入），会怎样？

> **答案**：`GetMatch` 遍历的是 `map<string, boost::regex>`，按 key（tag 名）字典序遍历（[recognizer.cc:46](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/recognizer.cc#L46)），**先匹配到的胜出**并立即 `return`。因此 tag 名的字典序隐含了优先级，配置时需注意命名。

---

### 4.4 ascii_composer：中英文模式与切换键

#### 4.4.1 概念说明

输入法最基本的状态切换是「中文模式 / 英文（ASCII）模式」。librime 用一个会话级 option `ascii_mode` 表示：`false` 是中文、`true` 是英文。`ascii_composer` 负责两件事：

1. **在 ASCII 模式下**：把字母键直接「穿透」上屏（`kRejected`，交给系统），或在内联编辑时把字母追加进输入串；
2. **响应模式切换键**：Shift、Ctrl、Alt、Super、Caps Lock、`XK_Eisu_toggle`（日文键盘的英数切换键）等，根据配置决定按下/弹起时是否切换 `ascii_mode`，以及切换时如何处理当前正在编辑的内容。

切换时的「善后」有 6 种风格（`AsciiModeSwitchStyle`）：`inline_ascii`（临时内联）、`commit_text`（确认当前候选）、`commit_code`（提交原始编码）、`clear`（清空）、`set_ascii_mode`、`unset_ascii_mode`。

#### 4.4.2 核心流程

```
ProcessKeyEvent(key):
    if 同时按了多个修饰键:  清状态, return kNoop           # 避免误触
    if 配置了 Caps Lock 行为:  先处理 Caps Lock 分支
    if 是 Eisu 键:  弹起时切换模式, return kAccepted/kRejected
    if 是 Shift/Ctrl/Alt/Super 单键:
        记录按下时刻; 弹起时若在 500ms 内则切换模式
        return kNoop                                          # 不吃修饰键本身
    if 是带 Ctrl/Alt/Super 的组合 或 Shift+空格:  return kNoop  # 留给别人
    if ascii_mode == true:
        if 未在编辑:  return kRejected                        # 直接上屏
        else:  PushInput(ch); return kAccepted                # 编辑 ASCII 串
    return kNoop
```

关键点：`ascii_composer` 排在 processors 链**最前面**，因此它在 ASCII 模式下能抢在 `speller` 之前把字母键 `kRejected`，让字母直接打到应用里。

#### 4.4.3 源码精读

风格枚举与字符串映射：[ascii_composer.cc:18-27](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/ascii_composer.cc#L18-L27)。

主处理函数：[ascii_composer.cc:60-141](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/ascii_composer.cc#L60-L141)。注意第 114 行的 `500ms` 时限——只有「快速单击」修饰键才触发切换，长按不触发，这是输入法的常见交互。第 130-139 行是 ASCII 模式的核心：未编辑时 `kRejected` 直接上屏，编辑中则 `PushInput`。

切换的实际执行 `SwitchAsciiMode`：[ascii_composer.cc:243-269](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/ascii_composer.cc#L243-L269)，按风格决定是 `ConfirmCurrentSelection` / `ClearNonConfirmedComposition` + `Commit` / `Clear`，最后 `set_option("ascii_mode", ...)`。

Caps Lock 分支：[ascii_composer.cc:143-186](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/ascii_composer.cc#L143-L186)，支持「good old caps lock」（让大写锁定仍输出大写字母）与作为模式切换两种用法。

配置加载 `LoadConfig`：[ascii_composer.cc:188-223](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/ascii_composer.cc#L188-L223)，从方案的 `ascii_composer/switch_key` 读按键绑定；缺失时回退到 `default.yaml` 的预设（呼应 u4-l4 的 `import_preset: default`）。

#### 4.4.4 代码实践

1. **实践目标**：理解 ASCII 模式下字母键为何「直接上屏」。
2. **操作步骤**：在 `rime_api_console` 里用 `set_option ascii_mode true`（或方案 switches 里切到 ABC），再按字母 `h`、`i`。
3. **需要观察的现象**：字母不进入候选编辑，而是直接 commit 上屏；对应源码 [ascii_composer.cc:131-133](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/ascii_composer.cc#L131-L133) 的 `kRejected`（未编辑时直接拒绝 → 系统默认上屏）。
4. **预期结果**：终端直接出现 `hi`，无候选窗。
5. **待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ascii_composer` 对单独的 Shift 键返回 `kNoop` 而不是 `kAccepted`？

> **答案**：因为它只想用 Shift 的「按下→弹起」事件来判断是否切换模式，但**不应该吃掉** Shift 本身——Shift 还要作为修饰键传给其他按键（如 Shift+字母）。返回 `kNoop` 让按键继续流传，同时内部记录按下/弹起时间（[ascii_composer.cc:87-118](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/ascii_composer.cc#L87-L118)）。

**练习 2**：`SwitchAsciiMode` 里 `style == kAsciiModeSwitchInline` 时为什么要 connect 一个 `update_notifier`？

> **答案**：`inline_ascii` 是「临时进入 ASCII 模式编辑一段西文，结束后自动退回中文」。它订阅 `update_notifier`，当检测到输入串被清空（`!ctx->IsComposing()`）时，自动把 `ascii_mode` 设回 `false`（[ascii_composer.cc:271-277](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/ascii_composer.cc#L271-L277)），实现「临时模式」的自动退出。

---

### 4.5 selector 与 navigator：候选选择与光标移动

这两个 Processor 都继承自 `KeyBindingProcessor`，结构高度相似，故合并讲解。

#### 4.5.1 概念说明

- **selector**：负责「在候选列表里移动高亮 / 翻页 / 选定」。它处理方向键、PageUp/PageDown、Home/End、数字键 `0-9`、小键盘数字、以及方案配置的 `select_keys`（如 `,.`/`[;` 等自定义选词键）。
- **navigator**：负责「在已输入串里移动光标」。它处理方向键（按字符或按音节跳）、Home/End，修改 `Context::caret_pos`。

两者都需要区分**横排 / 竖排**（`_vertical` option）与**线性 / 堆叠**（`_linear` / `_horizontal` option）两种候选窗布局，因为同一颗方向键在不同布局下含义不同（横排时 `→` 是下一个候选，竖排时 `↓` 才是）。`KeyBindingProcessor` 的模板参数 `N` 与 `keymap_selector` 正是为此设计：用多张 keymap 对应多种布局。

#### 4.5.2 核心流程

`Selector::ProcessKeyEvent`：

```
if release / Alt / Super:  return kNoop
if composition 为空 或 当前段无 menu 或 带 raw tag:  return kNoop
根据 _vertical / _linear 选 keymap_selector
result = KeyBindingProcessor.ProcessKeyEvent(...)       # 查方向键/翻页绑定
if result != kNoop:  return result
# 否则尝试数字键 / select_keys
if ch 是 select_keys 里的字符:  index = 其位置
else if ch 是 0-9 / 小键盘数字:  index = ((ch-'0')+9)%10  # 注意 0 映射到第 10 个
if index >= 0:  SelectCandidateAt(ctx, index); return kAccepted
return kNoop
```

`Navigator::ProcessKeyEvent`：

```
if release:  return kNoop
if 未在编辑:  return kNoop
根据 _vertical 选 keymap_selector
return KeyBindingProcessor.ProcessKeyEvent(..., FallbackOptions::All)
```

`navigator` 还订阅了 `select_notifier`，在用户选定候选后清空内部 `spans_`（音节边界缓存），保证下次移动时重新计算。

#### 4.5.3 源码精读

`Selector` 构造函数为 4 种布局组合（Horizontal/Vertical × Stacked/Linear）各建一张 keymap：[selector.cc:29-106](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/selector.cc#L29-L106)。

`Selector::ProcessKeyEvent`：[selector.cc:118-157](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/selector.cc#L118-L157)。数字键映射的精妙之处在第 148 行 `((ch - XK_0) + 9) % 10`：把 `1` 映射到 index 0、`0` 映射到 index 9（即「第 10 个候选」），符合直觉。

翻页 `NextPage` 会按需 `menu->Prepare(page_start + page_size)` 拉取下一页候选，并在末页根据 `page_down_cycle` 决定循环回首页或静默吃键：[selector.cc:171-193](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/selector.cc#L171-L193)。选定 `SelectCandidateAt` 把「页内索引」换算成「全局索引」再 `ctx->Select`：[selector.cc:255-265](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/selector.cc#L255-L265)。

`Navigator` 构造与按键绑定：[navigator.cc:36-82](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/navigator.cc#L36-L82)，`ProcessKeyEvent`：[navigator.cc:88-98](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/navigator.cc#L88-L98)。按音节跳 `JumpLeft`/`JumpRight` 利用 `Phrase::spans()`（候选词记录的音节边界）来定位：[navigator.cc:210-236](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/navigator.cc#L210-L236)。

`KeyBindingProcessor` 模板提供「按键→动作」查表与配置加载：[key_binding_processor.h:16-59](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/key_binding_processor.h#L16-L59)，`FallbackOptions` 支持 Shift 当 Ctrl 用、忽略 Shift等回退策略：[key_binding_processor.h:22-27](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/key_binding_processor.h#L22-L27)。匹配逻辑在 [key_binding_processor_impl.h:12-46](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/key_binding_processor_impl.h#L12-L46)。

#### 4.5.4 代码实践

1. **实践目标**：验证 selector 的数字键映射与翻页。
2. **操作步骤**：在 `rime_api_console` 输入 `ni`，候选菜单出现后按 `2`。
3. **需要观察的现象**：选中第 2 个候选（index 1），对应 [selector.cc:148](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/selector.cc#L148) 的 `((ch - XK_0) + 9) % 10` = `((50-49)+9)%10 = 10%10 = 0`？——注意这里要按 `2`，`XK_2` 的值是 `'2'`=50，`XK_0`='0'=48，`(50-48+9)%10 = 11%10 = 1`，即 index 1（第 2 个候选），正确。
4. **预期结果**：第 2 个候选被高亮并提交。
5. **待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么 selector 对「带 `raw` tag 的段」直接 `kNoop`？

> **答案**：`raw` tag 表示这段输入是「无法识别、原样上屏」的回退段（见 u6-l3 的 `fallback_segmentor`），它没有真正的候选菜单，方向键和数字键对它无意义，故放行（[selector.cc:125](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/selector.cc#L125)）。

**练习 2**：`Selector::NextPage` 里 `index >= candidate_count` 时为什么把 `index` 设为 `candidate_count - 1`？

> **答案**：当请求的页码超过实际候选总数（比如第 3 页只有 2 个候选），把高亮停在最后一个候选上，避免高亮悬空指向不存在的候选（[selector.cc:187-189](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/selector.cc#L187-L189)）。

---

### 4.6 editor：提交、回退与编辑动作族

> 这是本讲的三个**最小模块**之一。

#### 4.6.1 概念说明

`editor` 是处理器链的**最后一道关卡**，负责「把已经组织好的候选提交上屏」以及各种编辑动作：确认、回退、删除、取消等。它通常排在 `processors` 清单末尾（如 luna_pinyin 的 `express_editor`），兜底处理前面没人吃的编辑键。

librime 提供两种内置 editor：

- **`express_editor`**（即时编辑）：按字母键**直接上屏**（`DirectCommit`），适合「打字即出字」的简单场景；回车提交原始编码。
- **`fluid_editor`**（流式编辑）：按字母键**追加进输入串**（`AddToInput`），与 speller 协作进行流式拼写；回车确认当前候选。

两者的差异**只在两处**：默认 keymap 绑定与 `char_handler_`（字符处理策略）。这种「共享骨架、差异配置」的设计得益于 `KeyBindingProcessor`。

`Editor` 还定义了一张「动作表」`editor_action_definitions`，把字符串名（`confirm`/`revert`/`back`…）映射到成员函数，使方案 YAML 能用 `editor/bindings` 自定义按键。

#### 4.6.2 核心流程

`Editor::ProcessKeyEvent`：

```
if release:  return kRejected                    # 编辑器对释放键一律拒绝
if 正在编辑:
    result = KeyBindingProcessor.ProcessKeyEvent(..., FallbackOptions::All)
    if result != kNoop:  return result           # 命中绑定（如空格=确认）
if 配了 char_handler 且无修饰键 且是可见 ASCII:
    return char_handler_(ctx, ch)                # DirectCommit 或 AddToInput
return kNoop
```

注意第一行：editor 对**释放键返回 `kRejected`** 而非 `kNoop`。这是个有意的设计——编辑键的「按下」已被处理，「弹起」不应再触发任何事，直接拒绝交给系统默认，避免重复。

#### 4.6.3 源码精读

动作定义表：[editor.cc:19-32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/editor.cc#L19-L32)，含 `confirm`/`commit_composition`/`revert`/`back`/`delete`/`cancel` 等 13 个动作。

`Editor::ProcessKeyEvent`：[editor.cc:46-66](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/editor.cc#L46-L66)。注意第 47 行对 release 返回 `kRejected`，第 51-57 行先查 keymap（只在 `IsComposing()` 时），第 58-63 行回落到字符处理。

两个具体动作：确认 `Confirm`（先尝试确认当前选择，否则提交）：[editor.cc:87-90](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/editor.cc#L87-L90)；按音节回退 `BackToPreviousSyllable` 利用 `Phrase::spans().PreviousStop` 找音节边界：[editor.cc:155-160](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/editor.cc#L155-L160)。

两个字符处理器：`DirectCommit` 直接 `ctx->Commit()` 后 `kRejected`（让字符也上屏）：[editor.cc:178-181](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/editor.cc#L178-L181)；`AddToInput` 把字符追加进输入串：[editor.cc:183-187](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/editor.cc#L183-L187)。

两种 editor 的差异全在构造函数：`FluidEditor` 把空格绑 `Confirm`、回车绑 `CommitComposition`、`char_handler_ = AddToInput`：[editor.cc:189-203](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/editor.cc#L189-L203)；`ExpressEditor` 把回车绑 `CommitRawInput`、`char_handler_ = DirectCommit`：[editor.cc:205-218](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/editor.cc#L205-L218)。两者构造时分别传 `auto_commit = false/true`，从而设置 `_auto_commit` option（[editor.cc:41-44](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/editor.cc#L41-L44)），该 option 又被 speller 的 `AutoSelectPreviousMatch` 读取（见 4.2.3）。

类声明：[editor.h:20-58](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/editor.h#L20-L58)。

#### 4.6.4 代码实践

1. **实践目标**：对比 `express_editor` 与 `fluid_editor` 对同一字母键的不同处理。
2. **操作步骤**：
   - 在 luna_pinyin 里（用的是 `express_editor`），先按空格确认一个候选上屏，再按字母 `h`。
   - 推演：上屏后 `ctx->IsComposing()` 为 false，editor 走到 `char_handler_` 分支（[editor.cc:58-63](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/editor.cc#L58-L63)），`DirectCommit` 执行 `ctx->Commit()` 并返回 `kRejected`，字母 `h` 直接上屏。
   - 假想把方案改成 `fluid_editor`：同样场景下 `AddToInput` 会把 `h` 追加进输入串，开启新一轮拼写。
3. **需要观察的现象**：`express_editor` 下「确认后的字母」直接进文本；`fluid_editor` 下则进入候选编辑。
4. **预期结果**：理解为什么「即打即出」方案用 express，而「流式拼音」方案用 fluid。
5. **待本地验证**（改方案需重新部署）。

#### 4.6.5 小练习与答案

**练习 1**：为什么 `Editor::ProcessKeyEvent` 对 release 键返回 `kRejected` 而其他 Processor 多返回 `kNoop`？

> **答案**：编辑键（如回车、空格）的「按下」已经被 editor 处理（确认/提交），其「弹起」是同一个逻辑事件的尾声，不应再被任何 Processor 重新处理，也不应触发拼写。返回 `kRejected` 直接短路跳出整个处理器循环（[engine.cc:104-105](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L104-L105)），最干净地「吞掉」这次弹起。

**练习 2**：`FluidEditor` 与 `ExpressEditor` 都绑定了 `XK_space → Confirm`，但回车绑定不同。说出各自回车的语义。

> **答案**：`FluidEditor` 的回车是 `CommitComposition`（确认当前选择后提交组合文本，[editor.cc:194](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/editor.cc#L194)）；`ExpressEditor` 的回车是 `CommitRawInput`（清掉未确认组合、提交原始输入编码，[editor.cc:210](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/editor.cc#L210)）。前者保留翻译结果，后者丢弃翻译直接上屏原始字母。

---

### 4.7 key_binder 与 punctuator：条件快捷键与标点

这两个 Processor 在 luna_pinyin 里都通过 `import_preset: default` 从 `default.yaml` 取配置，本讲做概览。

#### 4.7.1 概念说明

- **`key_binder`**：一个「条件触发的快捷键重映射器」。它与 `Editor`/`Selector` 的按键绑定不同之处在于：它的绑定带有**触发条件**（`when`），只有在特定上下文才生效。条件有 5 档：`always` / `composing`（正在编辑）/ `has_menu`（有候选）/ `paging`（翻过页）/ `predicting`（联想中）。命中后可执行的动作包括：重发一串按键（`send`/`send_sequence`）、切换开关（`toggle`/`set_option`/`unset_option`）、切换方案（`select`）。
- **`punctuator`**：标点映射处理器。它把标点键（如 `.`、`,`）映射成中文标点（`。`、`，`），并处理「数字后的标点」等上下文（如 `3.14` 里的 `.` 保留为西文点）。标点本身会被 `punct_segmentor` 切成带 `punct` tag 的段，交给 `punct_translator`，但 punctuator 处理器负责一些前置的状态判断与 `use_space` 配置。

#### 4.7.2 核心流程

`KeyBinder::ProcessKeyEvent`：

```
if 正在重定向 / 无绑定:  return kNoop
if ReinterpretPagingKey 命中(句号重解释):  return kNoop
if 这个按键不在绑定表里:  return kNoop
收集当前满足的 conditions（always + composing/has_menu/paging/predicting）
for 该按键的每条绑定:
    if 绑定条件 ∈ 当前 conditions:
        执行动作（或重定向 ProcessKey 一串按键）
        return kAccepted
return kNoop
```

`ReinterpretPagingKey` 是个有意思的细节：当用户输入拼音后按 `.`（用于翻页或选词），紧接着又按字母，`key_binder` 会把那个 `.`「重新解释」为输入的一部分（`PushInput`），处理 `www.xxx` 这类带点的输入。

#### 4.7.3 源码精读

条件枚举：[key_binder.cc:21-28](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/key_binder.cc#L21-L28)。条件收集 `KeyBindingConditions` 根据当前 `Context` 状态插入对应条件：[key_binder.cc:248-269](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/key_binder.cc#L248-L269)。

主处理：[key_binder.cc:271-287](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/key_binder.cc#L271-L287)。动作执行 `PerformKeyBinding`：有 `action` 就直接调，否则置 `redirecting_ = true` 并递归调 `engine_->ProcessKey` 重发目标按键序列：[key_binder.cc:289-299](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/key_binder.cc#L289-L299)。`redirecting_` 标志防止重发时再次进入绑定造成无限递归。

`Punctuator` 构造读取 `punctuator/use_space` 并加载标点映射：[punctuator.cc:56-62](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/punctuator.cc#L56-L62)。标点映射分 `half_shape`/`full_shape`（半角/全角）与 `symbols`，按 `full_shape` option 动态切换：[punctuator.cc:23-49](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/punctuator.cc#L23-L49)。

#### 4.7.4 代码实践

1. **实践目标**：理解 `key_binder` 的条件触发。
2. **操作步骤**：读 `default.yaml`（不在 minimal 数据集，需从 plum 或 rime-prelude 获取）的 `key_binder/bindings`，找一条带 `when: has_menu` 或 `when: paging` 的绑定，推演它在什么时刻生效。
3. **需要观察的现象**：例如某些方案把 `comma`/`period` 在 `has_menu` 时绑定为「上一个/下一个候选」，而在非编辑状态下保持标点功能——这就是 `when` 条件的作用。
4. **预期结果**：能说出「同一颗键在不同上下文有不同行为」是由 `key_binder` 的条件机制实现的。
5. **待本地验证**（default.yaml 内容依发行版而异）。

#### 4.7.5 小练习与答案

**练习 1**：`key_binder` 的 `redirecting_` 标志有什么用？

> **答案**：当一条绑定的动作是「重发一串按键」（`send`/`send_sequence`）时，`PerformKeyBinding` 会递归调用 `engine_->ProcessKey`（[key_binder.cc:293-297](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/key_binder.cc#L293-L297)）。若不加保护，重发的按键又会匹配同一条绑定，导致无限递归。`redirecting_` 在重发前置 true、结束后置 false，`ProcessKeyEvent` 开头检查它（[key_binder.cc:272](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/key_binder.cc#L272)），重发期间直接 `kNoop` 放行，打破循环。

**练习 2**：`punctuator` 处理器与 `punct_segmentor`/`punct_translator` 是什么关系？

> **答案**：`punctuator` 处理器负责前置的状态管理（如 `use_space`、`full_shape` 切换时重新加载映射）；标点的实际切分与翻译由 `punct_segmentor`（打 `punct` tag）和 `punct_translator`（产出标点候选）完成。它们共享 `punctuator:` 配置节，是同一套标点机制在流水线不同阶段的分工（segmentor/translator 留待 u6-l3、u6-l4 详讲）。

---

## 5. 综合实践

**任务**：以「在 luna_pinyin 方案下输入拼音 `ni`，然后按数字 `2` 选第二个候选」为例，完整追踪这若干次按键各自穿过了哪些 Processor、谁吃了它、为什么。

**luna_pinyin 的 processors 清单**（[schema 第 40-48 行](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L40-L48)）：

```
1. ascii_composer
2. recognizer
3. key_binder
4. speller
5. punctuator
6. selector
7. navigator
8. express_editor
```

**步骤**：

1. **按键 `n`**（中文模式、无修饰）：
   - `ascii_composer`：`ascii_mode` 为 false → 不进 ASCII 分支 → `kNoop`。
   - `recognizer`：试探串 `n` 不匹配任何 pattern → `kNoop`。
   - `key_binder`：`n` 不在绑定表 → `kNoop`。
   - `speller`：`n` ∈ alphabet 且是声母 → `PushInput('n')` → `kAccepted`，**终止**。后续处理器看不到此键。
2. **按键 `i`**：同上，被 `speller` 吃掉，输入串变为 `ni`。
3. **按键 `2`**：
   - `ascii_composer`：`kNoop`（非 ASCII 模式）。
   - `recognizer`：`2` 不在 `0x21..0x7f` 的字母范围？实际 `2` 是 0x32，在 `>0x20 && <0x80` 内，但试探串 `ni2` 不匹配任何 pattern → `kNoop`。
   - `key_binder`：`kNoop`。
   - `speller`：`2` 不在 alphabet 也不在 delimiter → `kNoop`（[speller.cc:109](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/speller.cc#L109)）。
   - `punctuator`：`kNoop`（非标点）。
   - `selector`：`2` ∈ `0-9`，`index = ((50-48)+9)%10 = 1` → `SelectCandidateAt(ctx, 1)` → `kAccepted`，**终止**。第 2 个候选被选定并提交。

**产出要求**：用一张表格写出每个按键在每个 Processor 处的返回值（`kNoop` / `kAccepted` / `kRejected`），并在 `kAccepted` 的格子里注明「发生了什么副作用」（如 `PushInput`、`Select`）。这张表就是你对本讲全部 Processor 早退条件的总结。

**进阶**：把场景换成「ASCII 模式下按 `n`」，重新填表——你会看到 `ascii_composer` 在第 1 步就直接 `kRejected`，按键根本到不了 `speller`。

## 6. 本讲小结

- **三态返回值是处理器协作的语言**：`kAccepted` 吃键终止、`kRejected` 短路跳出交还系统、`kNoop` 放行给下一个；主循环在 [engine.cc:99-122](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L99-L122)。
- **speller** 用一连串早退过滤出「合法拼写键」，核心副作用是 `ctx->PushInput(ch)`，并附带 `auto_select`/`max_code_length`/`auto_clear` 等自动上屏策略。
- **recognizer** 用正则在「输入串+新字符」上做「先试后写」匹配，命中即吃键并隐式打 tag，把命令式输入（反查、字母段）交给专门翻译器。
- **ascii_composer** 排在链首，在 ASCII 模式下抢在 speller 前把字母 `kRejected` 直接上屏，并管理 Shift/Caps Lock 等 500ms 内的快速切换。
- **selector / navigator** 都基于 `KeyBindingProcessor`，分别管「候选高亮/翻页/选定」与「光标移动」，按候选窗布局（横竖/线性堆叠）切换 keymap。
- **editor** 是末尾兜底，`express_editor`（字母直接上屏）与 `fluid_editor`（字母追加进输入串）的差异只在 keymap 与 `char_handler_`；它对释放键返回 `kRejected` 以吞掉编辑键的弹起。
- **key_binder** 提供 `always/composing/has_menu/paging/predicting` 五档条件触发，`redirecting_` 防止重发递归；**punctuator** 管标点映射的半/全角切换。

## 7. 下一步学习建议

本讲把 processors 链上的零件讲完了，但每次「按键改变 input」之后真正发生的是 `Compose`——即 **Segmentor 切分 + Translator 翻译 + Filter 过滤**。建议接着学习：

- **u6-l3 Segmentor 组件族**：看 `speller` 追加进来的 `ni` 如何被 `abc_segmentor`/`affix_segmentor`/`fallback_segmentor` 切成带 tag 的 Segment，tag 又如何指导翻译器分流。
- **u6-l4 Translator 组件族**：看 `script_translator` 如何把切好的音节段查词典、产出候选，`selector` 选定的候选到底从哪来。
- **u6-l5 Filter 组件族**：看候选如何穿过 `simplifier`/`uniquifier` 过滤链变成最终上屏文字。

如果你对 `KeyBindingProcessor` 这个复用工具感兴趣，可以重读 [key_binding_processor.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/key_binding_processor.h) 与 [key_binding_processor_impl.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/key_binding_processor_impl.h)，它是理解 Editor/Selector/Navigator 三者共性的钥匙。若想动手改按键行为，可在测试方案里给 `editor/bindings` 或 `key_binder/bindings` 加一条自定义绑定，观察行为变化（注意修改方案后需重新部署，见 u9-l1）。
