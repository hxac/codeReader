# 架构取舍与二次开发

## 1. 本讲目标

本讲是整本手册的收束篇。前面五讲我们把 ibus-rime 拆成了入口、引擎、设置、渲染四块逐一精读；本讲退后一步，从「整体架构」的视角回答三个问题：

1. ibus-rime 为什么用不到一千行 C 代码就能支撑一个完整的中文输入法？它做了哪些**取舍**？
2. 哪些地方是**稳定边界**（不该轻易动），哪些地方是**扩展点**（鼓励你来动）？
3. 如果我想加一个自己的功能，从「用户改一个 YAML 配置」到「屏幕上的行为真的变了」，完整路径是怎样的？

学完后你应当能够：

- 用「薄前端 / 核心引擎」的二分视角，解释 ibus-rime 体量小却覆盖面广的原因；
- 说清 `RimeApi` 作为稳定边界的含义，以及它为什么让前端能独立于核心升级；
- 识别出 ibus-rime 的三类扩展点——配置项、状态栏按钮、按键/事件回调——并能分别指出对应源码位置；
- 独立完成一次「从 YAML 到引擎渲染」的小型改造，并画出完整改动流程图。

## 2. 前置知识

本讲假设你已读过 U1–U5。为照顾遗忘，用一句话复习三组关键术语：

- **薄前端（thin frontend）**：ibus-rime 自己不查词、不分词、不维护词库，只做「翻译转发」。所有算法都在 librime 里。
- **RimeApi**：librime 暴露给前端的 C 函数指针结构体，是两个项目之间的「合同」。ibus-rime 通过全局指针 `rime_api` 调用它。
- **GObject + IBusEngineClass**：ibus-rime 用 GObject 类型系统把「一个 Rime 会话」包装成一个 IBus 引擎对象，通过重写虚函数接入 IBus 的按键 / 焦点 / 属性事件。

如果对上面任一组仍觉陌生，建议先回看 [u2-l3](u2-l3-librime-init-deploy.md)、[u3-l1](u3-l1-engine-type-lifecycle.md)、[u5-l1](u5-l1-yaml-config-loading.md)。

## 3. 本讲源码地图

本讲贯穿前五个单元，涉及六个文件，但视角不同——我们不再逐行读，而是从「架构角色」看每个文件扮演什么：

| 文件 | 规模 | 架构角色 | 本讲关注点 |
| --- | --- | --- | --- |
| [rime_main.c](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c) | ~150 行 | 进程入口、生命周期编排 | 全局 `rime_api` 的取得与跨文件传递 |
| [rime_engine.c](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c) | ~610 行 | 引擎对象、UI 投影 | 虚函数表 = 扩展点清单 |
| [rime_engine.h](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.h) | ~12 行 | 类型对外契约 | `IBUS_TYPE_RIME_ENGINE` 宏 |
| [rime_settings.c](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c) | ~92 行 | 配置加载 | 「内置默认 + 读到才覆盖」模式 |
| [rime_settings.h](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.h) | ~40 行 | 配置结构契约 | `IBusRimeSettings` 与 `extern` 全局 |
| [ibus_rime.yaml](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/ibus_rime.yaml) | ~28 行 | 用户配置 | `style` 段 = 可扩展的开关集合 |

## 4. 核心概念与源码讲解

### 4.1 薄前端架构

#### 4.1.1 概念说明

「薄前端」是 ibus-rime 最根本的架构决策。它的含义可以浓缩成一句话：

> **ibus-rime 把 IBus 框架的 UI 原语翻译成 librime 的 API 调用，再把 librime 返回的状态翻译回 IBus 的 UI 原语。它不实现任何输入法逻辑。**

这条决策带来一个直接后果：代码量小，但「交互面」反而很大——它必须覆盖按键、焦点、属性按钮、预编辑文本、候选表、状态栏等几乎所有 IBus 能表达的概念，却几乎不必为任何一类算法写代码。

「薄」体现在三个「不做」上：

1. **不查词**：拼音到汉字的转换全在 librime，ibus-rime 只把按键转发给 `process_key`。
2. **不存状态**：会话、词库、用户词都由 librime 持有，ibus-rime 只缓存一个 `session_id` 句柄。
3. **不解析配置**：YAML 由 librime 解析，ibus-rime 只通过 `config_get_*` 按路径取值（见 [u5-l1](u5-l1-yaml-config-loading.md)）。

#### 4.1.2 核心流程

薄前端的工作可以建模成一个**投影（projection）**：前端维护的 UI 状态是 librime 内部状态的一个（可能滞后的）投影。形式化地，设 librime 在时刻 \(t\) 的内部状态为 \(S_t\)，前端渲染函数把 \(S_t\) 映射成一簇 IBus UI 原语：

\[
\mathrm{UI}_t = \mathrm{project}\bigl(\mathrm{snapshot}(S_t)\bigr)
\]

其中 \(\mathrm{snapshot}\) 是 librime 提供的三个只读取值函数（`get_status` / `get_commit` / `get_context`），\(\mathrm{project}\) 是前端把快照写进 IBus 控件的过程。关键性质是**单向、幂等、可重入**：

- **单向**：前端只读不写 librime 的内部状态（`set_option` 是仅有的少数反方向写，且写的是「选项」而非「词库」）。
- **幂等**：同一快照反复投影，UI 结果一致；这正是 `ibus_rime_update_status` 用三字段去重的前提。
- **可重入**：任何外部事件（按键、焦点、按钮、翻页）处理完后，都统一调用同一个 `ibus_rime_engine_update` 重新投影，不需要为每类事件写独立的刷新逻辑。

所有事件 → 同一个投影函数，这就是「薄」带来的代码复用。

#### 4.1.3 源码精读

「所有事件都汇聚到同一个投影函数」是可以在源码里直接数出来的事实。在 [rime_engine.c](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c) 中，`ibus_rime_engine_update` 是唯一的「刷新出口」，而下面这些回调**无一例外**都在末尾调用它：

- 按键处理 [rime_engine.c:540-543](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L540-L543)：`process_key` 之后立即 `update`。
- 焦点进入 [rime_engine.c:186-194](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L186-L194)：重报属性后 `update`。
- 状态栏三个按钮 [rime_engine.c:553-574](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L553-L574)：`deploy` / `sync` / `InputMode` 分支各自处理后都调用 `update`。
- 翻页、点选 [rime_engine.c:577-610](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L577-L610)：转发 `Page_Up/Down` 或 `select_candidate` 后 `update`。

注意这些回调**从不直接改 UI**：`InputMode` 按钮的处理（[rime_engine.c:569-574](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L569-L574)）只翻转 librime 的 `ascii_mode` 选项，UI 上图标怎么变交给随后的 `update` 去拉取。这种「**按钮改状态，状态驱动 UI**」的模式贯穿全文件。

#### 4.1.4 代码实践

**实践目标**：用「汇聚点计数」验证薄前端模型。

**操作步骤**：

1. 打开 [rime_engine.c](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c)。
2. 全文搜索 `ibus_rime_engine_update(rime_engine)` 的调用点。
3. 对每个调用点，记录它所在的回调函数名。

**需要观察的现象**：你应当找到至少 6 个调用点，分布在 `process_key_event`、`focus_in`、`reset`、`property_activate`（3 个分支）、`candidate_clicked`、`page_up`、`page_down` 中。

**预期结果**：所有改变 librime 状态的回调，都以 `ibus_rime_engine_update` 收尾；而 `focus_out` / `enable` 这两个**不改变状态**的回调则刻意留空（[rime_engine.c:196-218](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L196-L218)），连 `update` 都不调。这反过来印证了「投影只在状态可能变化时才需要重算」。

#### 4.1.5 小练习与答案

**练习 1**：`focus_out` 为什么可以留空？如果不留空、改成调用 `update`，会有什么副作用？

> **参考答案**：`focus_out` 表示输入框失去焦点，librime 内部状态并未改变，重新投影只会得到与上次相同的 UI，纯属浪费；更糟的是，某些 IBus 实现下向一个已失焦的引擎 `update_lookup_table` 可能导致候选面板闪现又消失。所以留空是「薄前端不重复劳动」的体现。

**练习 2**：如果要把「失焦时自动清空预编辑」做成功能，应该改 librime 还是改 ibus-rime？

> **参考答案**：改 ibus-rime。在 `focus_out` 里对会话调 `clear_composition`（参考 `reset` 的写法 [rime_engine.c:201-213](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L201-L213)），再 `update`。这是「会话级」的前端行为，不属于算法，所以放在前端合适。

---

### 4.2 RimeApi 稳定边界

#### 4.2.1 概念说明

`RimeApi` 是 librime 暴露给所有前端的 C 结构体，里面是一组**函数指针**（`initialize` / `process_key` / `get_context` / `config_open` ……）。它之所以被称为「稳定边界」，是因为：

- **版本协商**：结构体头部带 `data_size`，前端用 `RIME_STRUCT` 宏初始化时写明自己认识的版本，librime 据此做兼容（新 librime 对老前端只填它认得的字段）。
- **能力协商**：`RIME_API_AVAILABLE(rime_api, select_candidate)` 这类宏让前端能检测某个新接口是否存在，从而在不同 librime 版本下优雅降级。
- **可独立升级**：因为前端只依赖函数指针表，而不是 librime 的内部类，librime 的 C++ 实现可以任意重构，只要函数指针签名不变，前端代码就无需改动。

#### 4.2.2 核心流程

ibus-rime 获取并使用 `RimeApi` 的链路很短：

1. `main` 里 `rime_get_api()` 取得指针，存入全局变量 `rime_api`（初值 `NULL`）。
2. 其余两个 `.c` 文件用 `extern RimeApi *rime_api;` 声明同一个全局，共享这一份指针。
3. 所有对 librime 的调用都写成 `rime_api->xxx(...)`，从不直接链接 librime 的内部符号。

这个流程把「取得 API」和「使用 API」彻底解耦：取得只在入口发生一次，使用散落在引擎层与设置层各处。

#### 4.2.3 源码精读

全局指针在入口层定义并赋值：

- [rime_main.c:23](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L23) 定义 `RimeApi *rime_api = NULL;`，初值 `NULL`。
- [rime_main.c:147](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L147) `rime_api = rime_get_api();` 取得指针。

引擎层与设置层各自 `extern` 引用同一个全局：

- [rime_engine.c:10](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L10) `extern RimeApi *rime_api;`
- [rime_settings.c:6](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L6) `extern RimeApi *rime_api;`

能力协商的真实用例在候选点选回调里：[rime_engine.c:583](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L583) 用 `RIME_API_AVAILABLE(rime_api, select_candidate)` 判断当前 librime 是否支持鼠标点选，不支持则整个回调直接跳过——这就是「老前端 + 新功能」或「新前端 + 老 librime」都能安全运行的边界保障。

#### 4.2.4 代码实践

**实践目标**：体会「函数指针表」带来的解耦——前端代码里找不到任何对 librime 内部 C++ 符号的直接调用。

**操作步骤**：

1. 在仓库根目录用 `grep -rn "rime_api->" --include=*.c` 统计 librime 调用的写法。
2. 再用 `grep -rn "rime::" --include=*.c` 查找对 librime 内部命名空间的直接引用。

**需要观察的现象**：第一条会返回大量结果（`process_key`、`get_context`、`config_open` 等），第二条应当为零。

**预期结果**：前端只通过 `rime_api->` 这一个抽象点接触 librime。这意味着只要 librime 保持 `RimeApi` 的签名不变，它内部从 C 换成 C++、从某数据库换成另一个，ibus-rime 一行都不用改。

> 说明：上述 `grep` 命令需要本地执行确认；本讲不假定已运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `rime_api` 用「全局指针 + extern」而不是「每个文件各自 `rime_get_api()`」？

> **参考答案**：`rime_get_api()` 返回的是 librime 内部一个静态单例的指针，多次调用结果相同；用全局变量保存一次既避免重复调用，也明确表达「整个进程共用一份 API」的语义，并在 `ibus_rime_stop` 里可以集中判 `if (rime_api)` 做防御（[rime_main.c:83-87](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L83-L87)）。

**练习 2**：`RIME_API_AVAILABLE(rime_api, select_candidate)` 检测失败时，候选点选功能会怎样？

> **参考答案**：回调体直接被 `if` 跳过（[rime_engine.c:583-595](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L583-L595)），鼠标点选「没反应」，但不会崩溃，键盘选词不受影响。这是优雅降级。

---

### 4.3 GObject + IBusEngineClass 扩展点

#### 4.3.1 概念说明

IBus 的引擎是用 GObject 类型系统定义的「类」。要接入 IBus，ibus-rime 必须定义一个 `IBusEngine` 的子类（`IBusRimeEngine`），并把「自己关心的事件」挂到父类的**虚函数表**上。

这里的关键认知是：**这张虚函数表，就是 IBus 提供给前端的「扩展点清单」**。IBus 负责在合适的时机调用对应的虚函数（按下键 → `process_key_event`、获得焦点 → `focus_in`、点击状态栏 → `property_activate`……），前端只要决定「我重写哪几个、每个里面做什么」。

GObject 的注册是用一个宏 `G_DEFINE_TYPE` 完成的，它替你生成 `get_type` 函数和线程安全的首次注册逻辑——前端因此**不必手写任何类型注册样板**。

#### 4.3.2 核心流程

引擎类型的生命周期分四步：

1. **声明契约**：头文件暴露 `IBUS_TYPE_RIME_ENGINE` 宏与 `get_type` 声明，不暴露结构体细节。
2. **宏注册**：`.c` 里 `G_DEFINE_TYPE(...)` 一行完成注册框架，强制你实现 `class_init` 与 `init`。
3. **填虚函数表**：`class_init` 里逐个把父类虚函数指针指向自己的回调。
4. **挂载点触发**：`rime_main.c` 里 `ibus_factory_add_engine(factory, "rime", IBUS_TYPE_RIME_ENGINE)` 首次用到类型宏，触发惰性注册。

#### 4.3.3 源码精读

头文件只对外暴露一个宏和一个声明，这是「最小契约」原则：

- [rime_engine.h:6-9](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.h#L6-L9) 暴露 `IBUS_TYPE_RIME_ENGINE` 与 `ibus_rime_engine_get_type`。

实例结构体的**第一个成员必须是父类**（`IBusEngine parent`），这是 GObject 内存布局的硬性要求：

- [rime_engine.c:15-23](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L15-L23) 定义 `IBusRimeEngine`，持有 `session_id` / `status` / `table` / `props` 四个成员。

一行宏完成类型注册：

- [rime_engine.c:67](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L67) `G_DEFINE_TYPE (IBusRimeEngine, ibus_rime_engine, IBUS_TYPE_ENGINE)`。

虚函数表的挂载集中在 `class_init`：

- [rime_engine.c:69-87](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L69-L87) 重写了 `process_key_event` / `focus_in` / `focus_out` / `reset` / `enable` / `disable` / `property_activate` / `candidate_clicked` / `page_up` / `page_down` 共 10 个虚函数，并把析构钩子挂在更上层的 `IBusObjectClass->destroy`（[rime_engine.c:75](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L75)）。

惰性注册的触发点在入口层：

- [rime_main.c:109](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L109) `ibus_factory_add_engine(factory, "rime", IBUS_TYPE_RIME_ENGINE);`——首次展开 `IBUS_TYPE_RIME_ENGINE` 宏才真正触发 `class_init`（这一行在 `rime_main.c`，不在 `rime_engine.c`）。

#### 4.3.4 代码实践

**实践目标**：把虚函数表当成「菜单」来读，理解每项是一个独立扩展点。

**操作步骤**：

1. 打开 [rime_engine.c:69-87](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L69-L87) 的 `class_init`。
2. 列出所有 `engine_class->xxx = ...` 的赋值。
3. 对每一项，跳到对应回调函数的实现，用一句话写出「IBus 在什么时机会调它」。

**需要观察的现象**：你会得到一张 10 行的「事件 → 回调」表。

**预期结果**：例如 `process_key_event` ← 每次按键；`focus_in` ← 输入框获得焦点；`property_activate` ← 用户点击状态栏按钮；`page_up/down` ← 翻页键。这张表就是你想新增「某事件触发某行为」时首先要查阅的清单——**新增事件处理 = 往这张表里加一行**（前提是 IBus 父类提供了对应虚函数）。

#### 4.3.5 小练习与答案

**练习 1**：为什么析构 `destroy` 挂在 `IBusObjectClass` 而不是 `IBusEngineClass`？

> **参考答案**：`destroy` 是 GObject 生命周期里更上层的钩子（`IBusObject` 是 `IBusEngine` 的祖先），它在引用计数归零时触发，比引擎业务层的虚函数更靠后。把资源释放挂在这里，能保证「所有业务回调都已结束后」才释放 `session` / `table` / `props`，避免释放后被虚函数误用。

**练习 2**：如果我只想在「引擎被销毁时打一条日志」，最少要改哪一行？

> **参考答案**：不需要新挂虚函数——直接在已有的 `ibus_rime_engine_destroy` 开头（[rime_engine.c:155-183](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L155-L183)）加一行 `g_debug("engine destroyed");` 即可，因为它已经是 `destroy` 钩子的实现。

---

### 4.4 配置驱动的 UI

#### 4.4.1 概念说明

「配置驱动 UI」是薄前端的第二条腿：**前端不硬编码 UI 偏好，而是把它们放在 `ibus_rime.yaml` 里，运行时读取后填进一个全局结构体，渲染时按结构体的字段决定怎么画。**

这样做的好处是：用户改一个 YAML 字段就能换一种渲染风格，无需重新编译；开发者新增一个 UI 开关，也只要走完一条固定的「YAML → 全局结构体 → 渲染分支」流水线。这条流水线是本讲最重要的一条**改造模板**，4.5 节会照它实现一个真实扩展。

#### 4.4.2 核心流程

配置加载遵循一个严格的「**内置默认 + 读到才覆盖**」模式，保证幂等与缺省安全：

```
ibus_rime_load_settings()
  │
  ├─ 1. g_ibus_rime_settings = ibus_rime_settings_default;   // 整体复位到内置默认
  │
  ├─ 2. config_open("ibus_rime", &config)                    // 打开 YAML 句柄
  │
  ├─ 3. 逐项尝试读取：
  │     ├─ config_get_bool("style/inline_preedit", ...)      // 读到才覆盖
  │     ├─ config_get_cstring("style/preedit_style")         // 读到才覆盖
  │     ├─ config_get_cstring("style/cursor_type")
  │     ├─ config_get_bool("style/horizontal", ...)
  │     └─ config_get_cstring("style/color_scheme") + select_color_scheme()
  │
  └─ 4. config_close(&config)                                // 关闭句柄
```

「整体复位」是关键：每次加载都先把结构体重置回 `ibus_rime_settings_default`，再按 YAML 能读到的字段逐项覆盖。这样无论加载多少次、YAML 缺多少字段，结果都确定。

加载时机有两次（见 [u2-l3](u2-l3-librime-init-deploy.md)）：进程启动时一次，部署成功后一次。后者保证用户改完 YAML 并触发「部署」后，新配置能被重新读取生效。

#### 4.4.3 源码精读

配置结构体的契约在头文件，全局实例在 `.c`：

- [rime_settings.h:27-33](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.h#L27-L33) 定义 `struct IBusRimeSettings`，含 `embed_preedit_text` / `preedit_style` / `cursor_type` / `lookup_table_orientation` / `color_scheme` 五个字段。
- [rime_settings.h:35](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.h#L35) `extern struct IBusRimeSettings g_ibus_rime_settings;` 声明全局。

内置默认与全局实例：

- [rime_settings.c:16-22](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L16-L22) 硬编码默认值（`embed_preedit_text=TRUE`、`preedit_style=COMPOSITION`、`cursor_type=INSERT`、方向 `SYSTEM`、`color_scheme=NULL`）。
- [rime_settings.c:24](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L24) 定义全局实例 `g_ibus_rime_settings`。

加载函数本体是「复位 + 逐项覆盖」的范本：

- [rime_settings.c:42-92](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L42-L92) `ibus_rime_load_settings`。
- 复位在第 [45](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L45) 行：`g_ibus_rime_settings = ibus_rime_settings_default;`。
- 典型的「读到才覆盖」分支在 [rime_settings.c:53-57](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L53-L57)：只有 `config_get_bool` 返回真（即 YAML 里确有此项）才写入结构体，否则保持默认。

YAML 端的五个开关：

- [ibus_rime.yaml:5-27](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/ibus_rime.yaml#L5-L27) `style` 段下的 `horizontal` / `inline_preedit` / `preedit_style` / `cursor_type` / `color_scheme`。

渲染端如何消费这些字段，举两例：

- 候选表方向由 `lookup_table_orientation` 决定：[rime_engine.c:496-497](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L496-L497)。
- 内联预编辑是否启用、用哪种风格，由 `embed_preedit_text` 与 `preedit_style` 共同决定：[rime_engine.c:326-365](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L326-L365)。

#### 4.4.4 代码实践

**实践目标**：验证「YAML 改一个值 → 全局结构体跟着变 → 渲染分支走另一条路」的完整链路是通的。

**操作步骤**：

1. 打开本地用户配置 `~/.config/ibus/rime/ibus_rime.yaml`（若不存在，从仓库拷贝一份）。
2. 把 `color_scheme: ~` 改成 `color_scheme: aqua`。
3. 触发部署（点击状态栏「部署」按钮，或 `rm` 掉 `~/.config/ibus/rime/ibus_rime.yaml` 的编译缓存后重启引擎）。
4. 在一个有「选中片段」的输入过程中观察内联预编辑文字的高亮颜色。

**需要观察的现象**：选中片段应从「无高亮」变成 aqua 配色（前景白、背景深蓝，定义在 [rime_settings.c:9](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L9) `{ "aqua", 0xffffff, 0x0a3dfa }`）。

**预期结果**：确认 `select_color_scheme` 把 `g_ibus_rime_settings.color_scheme` 指向了 aqua 条目，进而让 [rime_engine.c:345-357](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L345-L357) 的着色分支真正生效。若颜色未变，先确认部署成功通知是否弹出（部署成功才会二次调用 `load_settings`，见 [rime_main.c:46-49](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_main.c#L46-L49)）。

> 说明：本实践需要本地 IBus 环境与图形界面，若无则标注为「待本地验证」，可降级为「阅读型实践」——只跟踪源码路径，确认字段流向。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ibus_rime_load_settings` 开头要先做一次整体赋值复位，而不是「只更新本次读到的字段」？

> **参考答案**：为了幂等。假设上次 YAML 里有 `color_scheme: aqua`，本次用户删掉了这行；若只「更新读到的字段」，`color_scheme` 会残留为 aqua。整体复位 + 逐项覆盖保证「结构体的值严格等于当前 YAML 的投影」，与历史无关。

**练习 2**：新增一个布尔型 style 选项时，需要在「默认结构体」里给它设初值吗？

> **参考答案**：必须设。因为复位是把整个结构体赋成 `ibus_rime_settings_default`（[rime_settings.c:45](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L45)）；新字段若不在默认结构体里初始化，复位后是未定义值（C 不会自动清零），渲染端用到时就是未定义行为。

---

### 4.5 二次开发改造路径

#### 4.5.1 概念说明

本模块把前四模块的结论收成一张「**改造模板**」。ibus-rime 的扩展几乎都落在三类扩展点上，每类有一条固定的改造路径：

| 扩展类型 | 触发方式 | 改造入口文件 | 典型实现手段 |
| --- | --- | --- | --- |
| **配置项** | 用户改 YAML | `ibus_rime.yaml` + `rime_settings.h` + `rime_settings.c` | 加字段 → 加读取分支 → 渲染处用 |
| **状态栏按钮** | 用户点按钮 | `rime_engine.c`（`init` 建按钮 + `property_activate` 分发） | `ibus_property_new` + `strcmp` 分支 |
| **按键/事件处理** | 系统事件 | `rime_engine.c`（`class_init` + 对应回调） | 重写虚函数或改已有回调 |

本模块以「新增一个配置项」为例，完整演示第一条路径，因为它最能体现「配置驱动 UI」的精髓，也是最容易上手的改造。

#### 4.5.2 核心流程

我们要实现的扩展是：**新增 `style/hide_comment` 布尔选项，为真时在候选表里隐藏候选词的注释**（即那串灰色的拼音/分类标注）。

现有候选表构建逻辑（[rime_engine.c:452-470](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L452-L470)）在 `comment` 非空时会把 `text` 与 `comment` 用空格拼成一行。我们要让它再受一个开关控制。

完整改造流程图：

```
┌─────────────────────┐      ┌──────────────────────┐      ┌─────────────────────────┐
│  ibus_rime.yaml     │      │   rime_settings.h    │      │      rime_settings.c    │
│  ─────────────      │      │   ──────────────     │      │      ──────────────     │
│  style:             │      │  struct IBusRimeSettings │   │  ibus_rime_load_settings│
│    hide_comment:true│  ①   │  {                   │  ②   │   ...                   │
│                     ├─────▶│    gboolean hide_comment; │ ─────▶│  Bool v=False;       │
│  （用户可见开关）   │      │    ...               │      │  config_get_bool(       │
│                     │      │  };                  │      │    &config,             │
└─────────────────────┘      │  (默认结构体也要加)  │      │    "style/hide_comment",│
                             │   hide_comment=FALSE │      │    &v) → 覆盖           │
                             └──────────┬───────────┘      └────────────┬────────────┘
                                        │                                │
                                        │   g_ibus_rime_settings.hide_comment
                                        │                                │
                                        ▼                                ▼
                             ┌──────────────────────────────────────────┐
                             │            rime_engine.c                 │
                             │            ────────────                  │
                             │  候选表循环 (line 452-470):              │
                             │   if (comment && !hide_comment) {        │
                             │     // 原: 拼接 text + " " + comment     │
                             │   } else {                               │
                             │     cand_text = ibus_text_new_from_string│
                             │       (text);   // 只显示候选词正文      │
                             │   }                                      │
                             └──────────────────────────────────────────┘
                                                 │
                                                 ▼
                                    屏幕上候选注释按开关显隐
```

三个文件、四处改动，缺一不可。顺序也很重要：先加字段（头文件），再加默认值与读取（设置层），最后才在渲染处使用（引擎层）。

#### 4.5.3 源码精读

改造前，先精读我们要触碰的「候选词与注释拼接」这段现有代码：

- [rime_engine.c:452-482](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L452-L482) 是候选表构建主循环。
- 其中 [rime_engine.c:456-469](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L456-L469) 是注释拼接分支：`comment` 非空时 `g_strconcat(text, " ", comment, NULL)`，并对注释区间着灰（`RIME_COLOR_DARK`，定义见 [rime_settings.h:8](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.h#L8)）；否则只显示 `text`。

我们要改的就是 [rime_engine.c:456](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L456) 的 `if (comment)` 这一行条件——把它改成同时考虑新开关。

读取布尔配置的标准写法，照抄 [rime_settings.c:53-57](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L53-L57) 的 `inline_preedit` 分支即可——这是项目里「加一个布尔 style 选项」的现成模板。

#### 4.5.4 代码实践

**实践目标**：亲手实现 `style/hide_comment` 扩展，跑通「YAML → 全局结构体 → 渲染」全链路。

> 重要约束：本任务是**作业**，不要真的修改源码仓库（本讲义只读源码）。请在一份**本地副本**上做，或仅在纸上画出 diff。下面的代码片段均为「示例代码」，不是项目原有代码。

**操作步骤**（四处改动）：

1. **YAML 加开关**（示例代码）：

   ```yaml
   # ibus_rime.yaml 的 style 段
   style:
     # ...
     # hide candidate comments (false|true).
     hide_comment: false
   ```

2. **头文件加字段**（示例代码），在 [rime_settings.h:27-33](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.h#L27-L33) 的结构体里加一项：

   ```c
   struct IBusRimeSettings {
     gboolean embed_preedit_text;
     gint preedit_style;
     gint cursor_type;
     gint lookup_table_orientation;
     struct ColorSchemeDefinition* color_scheme;
     gboolean hide_comment;   /* 新增字段 */
   };
   ```

3. **设置层加默认值与读取**（示例代码）。先在 [rime_settings.c:16-22](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_settings.c#L16-L22) 的默认结构体里加 `.hide_comment = FALSE,`，再在 `ibus_rime_load_settings` 里照搬 `inline_preedit` 的写法：

   ```c
   Bool hide_comment = False;
   if (rime_api->config_get_bool(
           &config, "style/hide_comment", &hide_comment)) {
     g_ibus_rime_settings.hide_comment = !!hide_comment;
   }
   ```

4. **引擎层使用开关**（示例代码），把 [rime_engine.c:456](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L456) 的条件从 `if (comment)` 改为：

   ```c
   if (comment && !g_ibus_rime_settings.hide_comment) {
     /* 原有拼接逻辑不变 */
   } else {
     cand_text = ibus_text_new_from_string(text);
   }
   ```

**需要观察的现象**：

- 改完重新编译部署，把 YAML 里 `hide_comment` 设为 `true`，候选词应只显示正文，不再带灰色注释；
- 设为 `false`（或删掉这行）恢复显示注释；
- 不写这行时，行为应与改造前完全一致（验证默认值生效）。

**预期结果**：四处改动串起来，构成一条完整的「配置 → 生效」链路，验证了 4.5.2 的流程图。若本地无 IBus 图形环境，**待本地验证**；可降级为「源码阅读型实践」：对照上面四段示例，确认每一处对应的真实源码位置与改动点。

**避坑提示**：

- 不要漏掉第 3 步里的「默认值」——否则复位后 `hide_comment` 是垃圾值（见练习 4.4-2）。
- 不要在 `class_init` 里读配置——配置属于全局设置层，引擎层只读 `g_ibus_rime_settings`。
- 改完记得点「部署」或重启，让 `load_settings` 的第二次调用（部署成功分支）把新值读进来。

#### 4.5.5 小练习与答案

**练习 1**：如果改成「新增一个状态栏按钮：点击后在候选表里临时切换注释显隐」，改造路径会变成什么？

> **参考答案**：改为「状态栏按钮」路径。① 在 `ibus_rime_engine_init` 里用 `ibus_property_new` 增加一个按钮（仿 [rime_engine.c:129-140](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L129-L140) 的「部署」按钮），挂到 `props`；② 在 `property_activate` 里加一个 `strcmp("toggle_comment", prop_name)` 分支（仿 [rime_engine.c:553-557](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L553-L557)），分支里翻转一个布尔并调用 `update`。注意按钮的 `key` 字符串必须与 `strcmp` 的字面量逐字符一致。

**练习 2**：本扩展为什么放在「配置项」路径，而不是「按键处理」路径？

> **参考答案**：因为它是「长期偏好」（用户希望所有候选词都不带注释），适合用配置表达、随部署持久化、对所有会话生效。按键路径适合「瞬时动作」（如临时翻页、临时切中英文）。区分「偏好」与「动作」是选对扩展路径的关键判断。

## 5. 综合实践

把 4.5 的扩展再往前推一步，设计一个贯穿本讲的综合任务：

> **任务**：为 ibus-rime 增加一个「候选词最小注释长度阈值」配置 `style/comment_min_length`（整数）。当候选注释的字符长度小于该阈值时，在候选表里隐藏该条注释（认为太短的注释没意义，例如单字符注音）。

这个任务同时考察三件事：

1. **架构判断**：它是「偏好」还是「动作」？是整型而非布尔，`config_get_int` 该怎么用？（提示：参照 librime API，librime 提供 `config_get_int`，写法与 `config_get_bool` 同构。）
2. **配置链路**：在 `ibus_rime.yaml` 加字段、在 `rime_settings.h` 加 `gint comment_min_length;`、在默认结构体设 `.comment_min_length = 0;`、在 `load_settings` 加读取分支。
3. **渲染使用**：在 [rime_engine.c:456-466](https://github.com/rime/ibus-rime/blob/ba8bfc3654c53d1723532907028ee6d59936b592/rime_engine.c#L456-L466) 的注释拼接分支里，先算 `int comment_len = g_utf8_strlen(comment, -1);`，再判断 `comment_len < g_ibus_rime_settings.comment_min_length` 时走「只显示正文」的 else 分支。

**交付物**：

- 一张改造流程图（仿 4.5.2）。
- 四处改动的 diff（YAML、头文件、设置层、引擎层）。
- 一份测试用例清单：阈值 0（全部显示，等价于原行为）、阈值 1（隐藏单字符注释）、阈值 999（全部隐藏）。

> 约束：仍在本仓库之外做改动，不修改被分析的源码仓库；图形验证**待本地验证**，可用「源码阅读 + 断言推演」替代：对每种阈值，写出候选表循环里 `cand_text` 的预期构造结果。

## 6. 本讲小结

- **薄前端**是 ibus-rime 的核心架构决策：只做 IBus 原语与 librime API 之间的翻译转发，不实现任何输入法算法；所有事件汇聚到唯一的投影函数 `ibus_rime_engine_update`，UI 是 librime 状态的单向、幂等投影。
- **`RimeApi` 是稳定边界**：通过「全局指针 + extern」共享，所有对 librime 的访问走 `rime_api->xxx`，配合 `RIME_STRUCT` / `RIME_API_AVAILABLE` 做版本与能力协商，使前端能独立于核心升级。
- **GObject + IBusEngineClass 的虚函数表就是扩展点清单**：`class_init` 里重写的每一项虚函数都是一个可挂载的事件入口，`G_DEFINE_TYPE` 一行宏免去手写类型注册样板。
- **配置驱动 UI** 遵循「内置默认 + 读到才覆盖」的幂等模式，`ibus_rime_load_settings` 在启动与部署成功时各调用一次，保证用户改完 YAML 后能重新生效。
- **三类扩展点**对应三条改造路径：配置项（YAML → 设置层 → 渲染）、状态栏按钮（`init` 建按钮 + `property_activate` 分发）、按键/事件（重写虚函数）；选对路径的关键是区分「长期偏好」与「瞬时动作」。
- **改造模板**可复用：新增一个 style 选项固定走「加 YAML 字段 → 加结构体字段 → 加默认值与读取分支 → 渲染处使用」四步，每步都有项目里的现成范例可照抄。

## 7. 下一步学习建议

本讲是入门到进阶路线的终点，但要真正吃透 ibus-rime，建议继续：

1. **横向对照其他 Rime 前端**：阅读 [squirrel](https://github.com/rime/squirrel)（macOS）或 [fcitx5-rime](https://github.com/fcitx/fcitx5-rime) 的源码，观察它们如何用各自平台的 UI 框架实现同一个「薄前端 + RimeApi」模型。这能反向加深你对「稳定边界」价值的理解。
2. **深入 librime**：从 `rime_api.h` 出发，读 librime 的 `RimeApi` 结构体定义，理解 `process_key` 之后到 `get_context` 之前引擎内部发生了什么（查词、排序、分页）。这才是「投影」的另一端。
3. **动手做一个真实扩展**：把综合实践的 `comment_min_length` 做成可发布的小补丁，跑通「编译 → 部署 → 验证」全流程；若愿意，再尝试加一个状态栏按钮（如「一键切换全/半角」），完整体验三类扩展点中最具交互性的那一条。
4. **回看本手册**：带着本讲的架构视角，重读 [u3-l2](u3-l2-session-and-key-event.md) 的按键主链路与 [u4-l3](u4-l3-candidate-table.md) 的候选表渲染，你会发现当初逐行读的代码，现在都能归位到「投影函数的某个分支」这一架构角色上。
